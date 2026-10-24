"""Policies on tables that Lakana's own writers cannot produce, written with pyiceberg.

Lakana's writers refuse nested types and never promote a column, but a table made outside
Lakana can have both. These tests pin what a reader sees from such a table under a row filter
or a column mask: a struct column read whole, a struct column masked whole, a struct field
refused by name, and a float column promoted to double.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc
import pyarrow.parquet
import pytest
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.types import (
    DoubleType,
    FloatType,
    LongType,
    NestedField,
    StringType,
    StructType,
)
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.config import StorageConfig
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Principal, PrincipalType, QueryRun, QueryRunStatus
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.pipeline import run_one

_ADDRESS = StructType(
    NestedField(10, "country", StringType(), required=False),
    NestedField(11, "city", StringType(), required=False),
)
_ADDRESS_ARROW = pa.struct([pa.field("country", pa.string()), pa.field("city", pa.string())])


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, with a persisted row.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The administrator, named ``admin``.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    return PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide a catalog with a `dev` environment rooted in `tmp_path`.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The environment's storage root.

    Returns
    -------
    CatalogService
        The catalog; it allows everything.
    """
    service = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    service.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    return service


def _ref(name: str) -> TableRef:
    """Name a table of `gold`.

    Parameters
    ----------
    name
        The table's name.

    Returns
    -------
    TableRef
        The reference in ``dev``.
    """
    return TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name=name)


def _append(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    table: LoadedTable,
    data: pa.Table,
) -> LoadedTable:
    """Append rows with pyiceberg and commit the result through the catalog.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    ref
        The table.
    table
        The table as it is now.
    data
        Rows whose Arrow schema fits the table's schema, nullability included.

    Returns
    -------
    LoadedTable
        The table after the commit.
    """
    transaction = StaticTable.from_metadata(table.metadata_location).transaction()
    transaction.append(data)
    return catalog.commit_table(
        principal, ref, requirements=transaction._requirements, updates=transaction._updates
    )


def _query(session: Session, principal: PrincipalRef, sql: str) -> QueryRun:
    """Submit a query and run it in the worker.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    sql
        The query.

    Returns
    -------
    QueryRun
        The finished run, refreshed.
    """
    run = QueryService(session, AllowAllAuthorization()).submit_query(principal, "dev", sql)
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    return run


def _result(tmp_path: Path, run: QueryRun) -> pa.Table:
    """Read the rows a successful run wrote.

    Parameters
    ----------
    tmp_path
        The environment's storage root.
    run
        The successful run.

    Returns
    -------
    pyarrow.Table
        The result.
    """
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.result_location is not None
    with pyarrow.ipc.open_file(tmp_path / run.result_location) as reader:
        return reader.read_all()


def _customers(catalog: CatalogService, principal: PrincipalRef) -> tuple[TableRef, LoadedTable]:
    """Create `gold.customers` with a struct column and three rows.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple of TableRef and LoadedTable
        The table and its state after the rows were committed.
    """
    ref = _ref("customers")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "region", StringType(), required=False),
            NestedField(3, "address", _ADDRESS, required=False),
        ),
    )
    data = pa.table(
        {
            "id": [1, 2, 3],
            "region": ["EU", "US", "EU"],
            "address": [
                {"country": "DE", "city": "Berlin"},
                {"country": "US", "city": "Austin"},
                {"country": "FR", "city": "Paris"},
            ],
        },
        schema=pa.schema(
            [
                pa.field("id", pa.int64(), nullable=False),
                pa.field("region", pa.string()),
                pa.field("address", _ADDRESS_ARROW),
            ]
        ),
    )
    return ref, _append(catalog, principal, ref, created, data)


def test_a_row_filter_on_a_top_level_column_reads_a_table_with_a_struct_column(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The struct comes through whole, and a field of it can be read by the query itself."""
    _customers(catalog, principal)
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="customers",
        expression="region = 'EU'",
    )

    whole = _result(tmp_path, _query(session, principal, "SELECT id, address FROM gold.customers"))
    field = _result(
        tmp_path,
        _query(session, principal, "SELECT id, address.country AS c FROM gold.customers"),
    )

    assert sorted(whole.column("id").to_pylist()) == [1, 3]
    assert sorted(row["city"] for row in whole.column("address").to_pylist()) == ["Berlin", "Paris"]
    assert sorted(field.column("c").to_pylist()) == ["DE", "FR"]


@pytest.mark.slow
def test_a_column_mask_can_cover_a_whole_struct_column(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The struct reads as NULL, and so does a field of it; the other columns are untouched."""
    _customers(catalog, principal)
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="customers",
        column="address",
        expression="NULL",
    )

    whole = _result(tmp_path, _query(session, principal, "SELECT id, address FROM gold.customers"))
    star = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.customers"))
    field = _result(
        tmp_path,
        _query(session, principal, "SELECT address.country AS c FROM gold.customers"),
    )
    qualified = _result(
        tmp_path,
        _query(session, principal, "SELECT customers.address FROM gold.customers"),
    )

    assert whole.column("address").to_pylist() == [None, None, None]
    assert star.column("address").to_pylist() == [None, None, None]
    assert qualified.column("address").to_pylist() == [None, None, None]
    assert field.column("c").to_pylist() == [None, None, None]
    for spelling in ("customers.address.country", "gold.customers.address.country"):
        try:
            result = _result(
                tmp_path,
                _query(session, principal, f"SELECT {spelling} AS c FROM gold.customers"),
            )
        except AssertionError:
            continue  # refused: nothing is shown, which is also fine
        assert result.column("c").to_pylist() == [None, None, None]
    assert sorted(whole.column("id").to_pylist()) == [1, 2, 3]


def test_a_row_filter_naming_a_struct_field_stays_refused(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A field cannot be named by a policy; the message says to use the column as a whole."""
    _customers(catalog, principal)

    with pytest.raises(ValidationError, match="struct fields") as raised:
        RowFilterService(session, AllowAllAuthorization(), catalog).create(
            principal,
            principal="admin",
            environment="dev",
            namespace="gold",
            table="customers",
            expression="address.country = 'DE'",
        )

    assert "Use a column of the table as a whole" in str(raised.value)


def test_a_column_mask_naming_a_struct_field_stays_refused(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A mask expression that reads a field of a struct is refused the same way."""
    _customers(catalog, principal)

    with pytest.raises(ValidationError, match="struct fields"):
        ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
            principal,
            principal="admin",
            environment="dev",
            namespace="gold",
            table="customers",
            column="region",
            expression="address.country",
        )


def _promoted_prices(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> TableRef:
    """Create `gold.prices` with a float column, append, promote it to double, append.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    catalog
        The catalog.

    Returns
    -------
    TableRef
        The table. Its first file holds 32-bit floats, its second 64-bit ones.
    """
    ref = _ref("prices")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "price", FloatType(), required=False),
        ),
    )
    old = pa.table(
        {"id": [1, 2], "price": pa.array([1.1, 2.5], pa.float32())},
        schema=pa.schema(
            [pa.field("id", pa.int64(), nullable=False), pa.field("price", pa.float32())]
        ),
    )
    after_old = _append(catalog, principal, ref, created, old)
    transaction = StaticTable.from_metadata(after_old.metadata_location).transaction()
    with transaction.update_schema() as change:
        change.update_column("price", DoubleType())
    promoted = catalog.commit_table(
        principal, ref, requirements=transaction._requirements, updates=transaction._updates
    )
    new = pa.table(
        {"id": [3, 4], "price": pa.array([1.1, 2.5], pa.float64())},
        schema=pa.schema(
            [pa.field("id", pa.int64(), nullable=False), pa.field("price", pa.float64())]
        ),
    )
    _append(catalog, principal, ref, promoted, new)
    return ref


def _ids(session: Session, principal: PrincipalRef, tmp_path: Path, sql: str) -> list[int]:
    """Run a query and list the `id` column of its rows, sorted.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    tmp_path
        The environment's storage root.
    sql
        The query; it selects ``id``.

    Returns
    -------
    list of int
        The ids.
    """
    return sorted(_result(tmp_path, _query(session, principal, sql)).column("id").to_pylist())


@pytest.mark.slow
def test_a_float_equality_on_a_column_promoted_to_double(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Characterisation: what `price = 1.1` matches once `price` went from float to double.

    The file written before the promotion holds 32-bit floats, the one written after holds
    64-bit doubles, and the read gives both as double through the field-id map, in either file
    order. The float 1.1 read as a double is 1.100000023841858, so on DuckDB 2.0 `price = 1.1`
    matches only the new file's row, and every form of the equality agrees with it. (DuckDB 1.5
    pushed the equality down into the older file as a 32-bit comparison, so `price = 1.1`
    matched both files and `price = 1.1 OR id = 99` only the new one.) The range and the
    rounded value match both rows, which is what the documentation advises.
    """
    _promoted_prices(session, principal, catalog)

    def ids(where: str) -> list[int]:
        return _ids(session, principal, tmp_path, f"SELECT id FROM gold.prices WHERE {where}")

    ranged = ids("price > 1.09 AND price < 1.11")
    rounded = ids("round(price, 2) = 1.1")
    equal = ids("price = 1.1")
    equal_or = ids("price = 1.1 OR id = 99")
    not_equal = ids("price <> 1.1")
    negated = ids("NOT (price = 1.1 AND id > 0)")
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="prices",
        expression="price > 1.09 AND price < 1.11",
    )
    filtered = _ids(session, principal, tmp_path, "SELECT id FROM gold.prices")

    assert ranged == [1, 3]
    assert rounded == [1, 3]
    assert filtered == [1, 3]
    assert equal == [3]
    assert equal_or == [3]
    assert not_equal == [1, 2, 4]
    assert negated == [1, 2, 4]


@pytest.mark.parametrize(
    "expression",
    [
        "born < seen",
        "seen > born",
        "born < TIMESTAMP '2026-01-01 00:00:00'",
        "TIMESTAMP '2026-01-01 00:00:00' > born",
    ],
)
def test_a_date_outside_the_timestamp_range_never_fails_a_filtered_read(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    expression: str,
) -> None:
    """A filter comparing a date with a timestamp reads rows with any date, both ways round (#178).

    The years 300000 and 300000 BC and the ends of DuckDB's date range lie outside the timestamp
    range. Before #178 the engine's conversion of such a date failed every read the filter
    covered. The table is a raw-zone table: pyiceberg cannot write such a date (its statistics
    go through Python's `date`); the Iceberg column types are covered in
    `test_query_rewrite_execution.py`.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    catalog
        The catalog.
    tmp_path
        The environment's storage root.
    expression
        The row filter.
    """
    days = [10_957, 21_915, 108_854_000, -108_854_000, 2_147_483_646, -2_147_483_646]
    directory = tmp_path / "raw" / "people"
    directory.mkdir(parents=True)
    pyarrow.parquet.write_table(
        pa.table(
            {
                "id": pa.array(range(1, len(days) + 1), pa.int64()),
                "born": pa.array(days, pa.int32()).cast(pa.date32()),
                "seen": pa.array([datetime(2026, 1, 1)] * len(days), pa.timestamp("us")),
            }
        ),
        directory / "a.parquet",
    )
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="people"),
        location_pattern="raw/people/*.parquet",
        format="parquet",
    )
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="raw",
        table="people",
        expression=expression,
    )

    ids = sorted(
        _result(tmp_path, _query(session, principal, "SELECT id FROM raw.people"))
        .column("id")
        .to_pylist()
    )

    # Row 1 (2000-01-01) passes; row 2 (2030-01-01) does not; the dates outside the range are
    # hidden, or, against a constant, compared as the dates they are (rows 4 and 6 are earlier).
    assert ids in ([1], [1, 4, 6])
    if "TIMESTAMP" not in expression:
        assert ids == [1]
