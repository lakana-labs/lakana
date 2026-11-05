"""Reads of a table whose schema changed between its data files, written with pyiceberg.

Every Iceberg table is read by field id, so the five schema changes Iceberg allows on a
top-level column read as the current schema says, under no policy, a row filter and a column
mask alike. A field inside a nested column is read by name, so a change to one fails closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.ipc
import pyarrow.parquet as pq
import pytest
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.table.update import SetCurrentSchemaUpdate
from pyiceberg.table.update.schema import UpdateSchema
from pyiceberg.types import (
    DecimalType,
    DoubleType,
    FloatType,
    IcebergType,
    IntegerType,
    ListType,
    LongType,
    MapType,
    NestedField,
    StringType,
    StructType,
    TimestamptzType,
)
from sqlalchemy.orm import Session

import lakana_worker.pipeline as pipeline_module
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.field_ids import duckdb_type_of
from lakana_core.catalog.query_service import QueryService, WriteMode, WriteTarget
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.config import StorageConfig
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ColumnMask,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    RowFilter,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.field_binding import (
    PENDING,
    UNBOUND,
    UNREADABLE_POLICY_MESSAGE,
    PolicySchema,
)
from lakana_core.policy.resolver import PolicyResolver, ResolvedPolicy
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.pipeline import run_one


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


def _optional(field_id: int, name: str, field_type: IcebergType) -> NestedField:
    """Build an optional column.

    Parameters
    ----------
    field_id
        Its field id.
    name
        Its name.
    field_type
        Its type.

    Returns
    -------
    NestedField
        The column.
    """
    return NestedField(field_id, name, field_type, required=False)


def _append(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    table: LoadedTable,
    data: pa.Table,
) -> LoadedTable:
    """Append rows with pyiceberg and commit them through the catalog, as an Iceberg client does.

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
        Rows that fit the table's current schema.

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


def _evolve(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    table: LoadedTable,
    change: Callable[[UpdateSchema], object],
) -> LoadedTable:
    """Change the table's schema with pyiceberg and commit it through the catalog.

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
    change
        What to do to the schema.

    Returns
    -------
    LoadedTable
        The table after the commit.
    """
    transaction = StaticTable.from_metadata(table.metadata_location).transaction()
    with transaction.update_schema() as update:
        change(update)
    return catalog.commit_table(
        principal, ref, requirements=transaction._requirements, updates=transaction._updates
    )


def _created(
    catalog: CatalogService, principal: PrincipalRef, name: str, third: NestedField, rows: pa.Table
) -> tuple[TableRef, LoadedTable]:
    """Create `gold.<name>` with `id`, `region` and one more column, holding two rows.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    name
        The table's name.
    third
        The column the test changes, field id 3.
    rows
        Ids 1 and 2, regions EU and US, and the third column.

    Returns
    -------
    tuple of TableRef and LoadedTable
        The table and its state after the first append.
    """
    ref = _ref(name)
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(_optional(1, "id", LongType()), _optional(2, "region", StringType()), third),
    )
    return ref, _append(catalog, principal, ref, created, rows)


def _rows(ids: list[int], **columns: pa.Array) -> pa.Table:
    """Build rows with ids, alternating regions and the given columns.

    Parameters
    ----------
    ids
        The ids.
    **columns
        Further columns, in order.

    Returns
    -------
    pa.Table
        The rows.
    """
    regions = ["EU" if index % 2 == 0 else "US" for index in range(len(ids))]
    return pa.table({"id": pa.array(ids, pa.int64()), "region": regions, **columns})


def _added(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, list[object]]:
    """Add a column after the first file; it reads NULL there.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The changed column and its values for ids 1 to 4.
    """
    ref = _ref("added")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(_optional(1, "id", LongType()), _optional(2, "region", StringType())),
    )
    table = _append(catalog, principal, ref, created, _rows([1, 2]))
    table = _evolve(catalog, principal, ref, table, lambda u: u.add_column("tier", StringType()))
    _append(catalog, principal, ref, table, _rows([3, 4], tier=pa.array(["gold", "silver"])))
    return "tier", [None, None, "gold", "silver"]


def _renamed(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, list[object]]:
    """Rename a column after the first file; it keeps its data under the new name.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The changed column and its values for ids 1 to 4.
    """
    third = _optional(3, "label", StringType())
    ref, table = _created(
        catalog, principal, "renamed", third, _rows([1, 2], label=pa.array(["a", "b"]))
    )
    table = _evolve(catalog, principal, ref, table, lambda u: u.rename_column("label", "name"))
    _append(catalog, principal, ref, table, _rows([3, 4], name=pa.array(["c", "d"])))
    return "name", ["a", "b", "c", "d"]


def _readded(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, list[object]]:
    """Drop a column and add it again; it is a new column, NULL in the old file.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The changed column and its values for ids 1 to 4.
    """
    third = _optional(3, "tier", StringType())
    rows = _rows([1, 2], tier=pa.array(["x-old", "y-old"]))
    ref, table = _created(catalog, principal, "dropped_and_added_again", third, rows)
    table = _evolve(catalog, principal, ref, table, lambda u: u.delete_column("tier"))
    table = _evolve(catalog, principal, ref, table, lambda u: u.add_column("tier", LongType()))
    _append(catalog, principal, ref, table, _rows([3, 4], tier=pa.array([30, 40], pa.int64())))
    return "tier", [None, None, 30, 40]


def _int_to_long(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, list[object]]:
    """Widen an int column to long; it reads as long in both files.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The changed column and its values for ids 1 to 4.
    """
    third = _optional(3, "qty", IntegerType())
    ref, table = _created(
        catalog, principal, "int_to_long", third, _rows([1, 2], qty=pa.array([5, 6], pa.int32()))
    )
    table = _evolve(catalog, principal, ref, table, lambda u: u.update_column("qty", LongType()))
    _append(catalog, principal, ref, table, _rows([3, 4], qty=pa.array([7, 8], pa.int64())))
    return "qty", [5, 6, 7, 8]


def _float_to_double(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, list[object]]:
    """Widen a float column to double; it reads as double in both files.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The changed column and its values for ids 1 to 4.
    """
    third = _optional(3, "price", FloatType())
    rows = _rows([1, 2], price=pa.array([1.5, 2.5], pa.float32()))
    ref, table = _created(catalog, principal, "float_to_double", third, rows)
    table = _evolve(
        catalog, principal, ref, table, lambda u: u.update_column("price", DoubleType())
    )
    _append(catalog, principal, ref, table, _rows([3, 4], price=pa.array([3.5, 4.5], pa.float64())))
    return "price", [1.5, 2.5, 3.5, 4.5]


_CHANGES = {
    "added": _added,
    "renamed": _renamed,
    "dropped_and_added_again": _readded,
    "int_to_long": _int_to_long,
    "float_to_double": _float_to_double,
}
_WIDENED = {"int_to_long": pa.int64(), "float_to_double": pa.float64()}


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
        The run, which must have succeeded.

    Returns
    -------
    pa.Table
        The result.
    """
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.result_location is not None
    with pyarrow.ipc.open_file(tmp_path / run.result_location) as reader:
        return reader.read_all()


@pytest.mark.parametrize("order", ["listed", "reversed"])
@pytest.mark.parametrize("policy", ["none", "row_filter", "column_mask"])
@pytest.mark.parametrize("change", sorted(_CHANGES))
def test_a_read_follows_the_schema_change_by_field_id(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    change: str,
    policy: str,
    order: str,
) -> None:
    """Each change, read plainly, through a row filter and through a mask on the changed column.

    Parameters
    ----------
    monkeypatch
        Replaces the file listing.
    session
        Database session.
    principal
        The reader, also the policies' principal.
    catalog
        The catalog.
    tmp_path
        The storage root.
    change
        The schema change between the two files.
    policy
        What applies to the reader.
    order
        Whether the data files are read in the order the manifests list them or the reverse; by-name
        reads used to take a column's type and presence from the first file.
    """
    column, values = _CHANGES[change](catalog, principal)
    listings: list[tuple[list[str], list[str]]] = []
    if order == "reversed":
        real = pipeline_module.list_data_files

        def reverse(*args: object, **kwargs: object) -> list[str]:
            listed = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
            listings.append((listed, list(reversed(listed))))
            return list(reversed(listed))

        monkeypatch.setattr(pipeline_module, "list_data_files", reverse)
    if policy == "row_filter":
        RowFilterService(session, AllowAllAuthorization(), catalog).create(
            principal,
            principal="admin",
            environment="dev",
            namespace="gold",
            table=change,
            expression="region = 'EU'",
        )
    if policy == "column_mask":
        ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
            principal,
            principal="admin",
            environment="dev",
            namespace="gold",
            table=change,
            column=column,
            expression="NULL",
        )

    read = _result(tmp_path, _query(session, principal, f"SELECT * FROM gold.{change}"))

    # The reversed order must really differ from the listed one, or the parameter tests nothing.
    assert all(listed != reversed_ for listed, reversed_ in listings)
    assert bool(listings) == (order == "reversed")

    rows = sorted(read.to_pylist(), key=lambda row: row["id"])
    ids = [1, 3] if policy == "row_filter" else [1, 2, 3, 4]
    assert [row["id"] for row in rows] == ids
    expected = [None] * len(ids) if policy == "column_mask" else [values[i - 1] for i in ids]
    assert [row[column] for row in rows] == expected
    if policy != "column_mask" and change in _WIDENED:
        assert read.schema.field(column).type == _WIDENED[change]


_ADDRESS = StructType(
    NestedField(10, "country", StringType(), required=False),
    NestedField(11, "city", StringType(), required=False),
)


def test_a_renamed_field_inside_a_struct_column_fails_closed_until_the_rows_are_written_again(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """DuckDB matches a struct's fields by name: the old file would read `town` as NULL."""
    ref = _ref("customers")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(_optional(1, "id", LongType()), _optional(2, "address", _ADDRESS)),
    )
    city = pa.struct([pa.field("country", pa.string()), pa.field("city", pa.string())])
    town = pa.struct([pa.field("country", pa.string()), pa.field("town", pa.string())])
    old = pa.table(
        {
            "id": pa.array([1], pa.int64()),
            "address": pa.array([{"country": "DE", "city": "Berlin"}], city),
        }
    )
    table = _append(catalog, principal, ref, created, old)
    table = _evolve(
        catalog, principal, ref, table, lambda u: u.rename_column("address.city", "town")
    )
    new = pa.table(
        {
            "id": pa.array([2], pa.int64()),
            "address": pa.array([{"country": "FR", "town": "Paris"}], town),
        }
    )
    table = _append(catalog, principal, ref, table, new)

    refused = _query(session, principal, "SELECT * FROM gold.customers")

    assert refused.status == QueryRunStatus.FAILED
    assert refused.error_message == (
        "'gold.customers' cannot be read: the field `town` of its column `address` was renamed, "
        "moved, or dropped and added again after some of its data files were written, and Lakana "
        "reads the fields inside a nested column by name. Ask the owner of the table to write all "
        "of its rows again (an overwrite), so that every data file has the new layout."
    )
    assert str(tmp_path) not in refused.error_message
    assert ".parquet" not in refused.error_message

    rows = pa.table(
        {
            "id": pa.array([1, 2], pa.int64()),
            "address": pa.array(
                [{"country": "DE", "town": "Berlin"}, {"country": "FR", "town": "Paris"}], town
            ),
        }
    )
    transaction = StaticTable.from_metadata(table.metadata_location).transaction()
    transaction.overwrite(rows)
    catalog.commit_table(
        principal, ref, requirements=transaction._requirements, updates=transaction._updates
    )

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.customers"))
    assert sorted(row["address"]["town"] for row in read.to_pylist()) == ["Berlin", "Paris"]


def test_a_struct_whose_fields_never_changed_reads_whole_after_its_column_was_renamed(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The column's own rename is a field-id matter; its fields keep their names."""
    ref = _ref("places")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(_optional(1, "id", LongType()), _optional(2, "address", _ADDRESS)),
    )
    city = pa.struct([pa.field("country", pa.string()), pa.field("city", pa.string())])
    old = pa.table(
        {
            "id": pa.array([1], pa.int64()),
            "address": pa.array([{"country": "DE", "city": "Berlin"}], city),
        }
    )
    table = _append(catalog, principal, ref, created, old)
    _evolve(catalog, principal, ref, table, lambda u: u.rename_column("address", "home"))

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.places"))

    assert read.to_pylist() == [{"id": 1, "home": {"country": "DE", "city": "Berlin"}}]


@pytest.mark.parametrize(
    "field_type",
    [
        DecimalType(38, 9),
        DecimalType(10, 0),
        TimestamptzType(),
        StructType(
            NestedField(10, "a b", IntegerType(), required=False),
            NestedField(11, 'q"x', StringType(), required=False),
        ),
        ListType(20, MapType(30, StringType(), 31, DoubleType())),
        ListType(
            40, StructType(NestedField(41, "inner list", ListType(42, LongType()), required=False))
        ),
    ],
)
def test_every_duckdb_type_the_map_names_round_trips_through_duckdb(
    field_type: IcebergType,
) -> None:
    """DuckDB reads the rendered type back as the same type, with nothing dropped.

    DuckDB's `::` cast ignores text after a complete type, so a mis-rendered type would be cut
    short without an error; comparing `typeof` with the rendered text shows any such cut.

    Parameters
    ----------
    field_type
        An Iceberg type.
    """
    duckdb_type = duckdb_type_of(field_type)
    assert duckdb_type is not None
    row = duckdb.connect().execute(f"SELECT typeof(NULL::{duckdb_type})").fetchone()
    assert row is not None
    assert row[0].replace(" ", "") == duckdb_type.replace(" ", "")


def _renamed_refusal(table: str, column: str) -> str:
    """Give the text of the refusal of a read after a rename touched a legacy policy's column.

    Parameters
    ----------
    table
        The table's dotted name.
    column
        The policy's column.

    Returns
    -------
    str
        The message.
    """
    return (
        f"`{table}` has a row filter or column mask that uses column `{column}`, and a column "
        "was renamed or replaced since the policy was created, so the policy may now cover a "
        "different column. Nothing is shown. An administrator must review the table's row "
        "filters and column masks, create each one that uses a renamed or replaced column "
        "again for the column it is meant for, and then revoke the old one."
    )


def _column_gone(table: str, column: str) -> str:
    """Give the text of the refusal of a read whose bound policy's column is gone.

    Parameters
    ----------
    table
        The table's dotted name.
    column
        The policy's column, as written.

    Returns
    -------
    str
        The message.
    """
    return (
        f"`{table}` has a row filter or column mask on a column that was dropped or replaced "
        f"(created for `{column}`); an administrator creates it again for the column it is "
        "meant for, or revokes it"
    )


def _unbind(session: Session, policy_id: uuid.UUID, binding: str) -> None:
    """Make a policy one of an earlier release: not bound to field ids.

    Parameters
    ----------
    session
        Database session.
    policy_id
        The row filter's or column mask's id.
    binding
        `pending` or `unbound`.
    """
    row = session.get(ColumnMask, policy_id) or session.get(RowFilter, policy_id)
    assert row is not None
    row.binding = binding
    row.field_ids = None
    if isinstance(row, ColumnMask):
        row.column_field_id = None
    session.commit()


def _people(catalog: CatalogService, principal: PrincipalRef, name: str) -> tuple[TableRef, str]:
    """Create `gold.<name>` with `id`, `dept`, `ssn` and `email`, holding two rows.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    name
        The table's name.

    Returns
    -------
    tuple of TableRef and str
        The table, and the metadata location after the append.
    """
    ref = _ref(name)
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            _optional(1, "id", LongType()),
            _optional(2, "dept", StringType()),
            _optional(3, "ssn", StringType()),
            _optional(4, "email", StringType()),
        ),
    )
    rows = pa.table(
        {
            "id": pa.array([1, 2], pa.int64()),
            "dept": ["a", "b"],
            "ssn": ["111", "222"],
            "email": ["x@example.org", "y@example.org"],
        }
    )
    return ref, _append(catalog, principal, ref, created, rows).metadata_location


def _rename(
    catalog: CatalogService, principal: PrincipalRef, ref: TableRef, *pairs: tuple[str, str]
) -> None:
    """Rename columns one after the other, each as a commit of its own.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    ref
        The table.
    *pairs
        Each column's old and new name.
    """
    for old, new in pairs:
        table = catalog.load_table(principal, ref)
        _evolve(catalog, principal, ref, table, lambda u, o=old, n=new: u.rename_column(o, n))


def _mask(
    session: Session, catalog: CatalogService, principal: PrincipalRef, table: str, column: str
) -> uuid.UUID:
    """Mask a column of `gold.<table>` for the admin, with the text `hidden`.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog.
    principal
        The acting principal.
    table
        The table.
    column
        The column to mask.

    Returns
    -------
    uuid.UUID
        The mask's id.
    """
    return (
        ColumnMaskService(session, AllowAllAuthorization(), catalog)
        .create(
            principal,
            principal="admin",
            environment="dev",
            namespace="gold",
            table=table,
            column=column,
            expression="'hidden'",
        )
        .id
    )


def _assert_refused(run: QueryRun, message: str) -> None:
    """Assert a run failed with the given refusal and wrote no result.

    Parameters
    ----------
    run
        The finished run.
    message
        The refusal: `_renamed_refusal` or `_column_gone`.
    """
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == message
    assert run.result_location is None


def _filter(
    session: Session, catalog: CatalogService, principal: PrincipalRef, expression: str
) -> uuid.UUID:
    """Filter `gold.staff` for the admin.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog.
    principal
        The acting principal.
    expression
        The row filter.

    Returns
    -------
    uuid.UUID
        The filter's id.
    """
    return (
        RowFilterService(session, AllowAllAuthorization(), catalog)
        .create(
            principal,
            principal="admin",
            environment="dev",
            namespace="gold",
            table="staff",
            expression=expression,
        )
        .id
    )


def test_a_mask_follows_its_column_when_another_takes_its_name(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """`ssn` -> `ssn_old`, `email` -> `ssn`: the mask stays on field 3, now `ssn_old`."""
    ref, _ = _people(catalog, principal, "staff")
    _mask(session, catalog, principal, "staff", "ssn")
    _rename(catalog, principal, ref, ("ssn", "ssn_old"), ("email", "ssn"))

    rows = sorted(
        _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff")).to_pylist(),
        key=lambda row: row["id"],
    )

    assert [row["ssn_old"] for row in rows] == ["hidden", "hidden"]
    assert [row["ssn"] for row in rows] == ["x@example.org", "y@example.org"]


def test_a_rename_that_touches_no_policy_column_reads_as_before(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A mask on `ssn`; `email` -> `email2` leaves it covering the same column."""
    ref, _ = _people(catalog, principal, "staff")
    _mask(session, catalog, principal, "staff", "ssn")
    _rename(catalog, principal, ref, ("email", "email2"))

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))

    rows = sorted(read.to_pylist(), key=lambda row: row["id"])
    assert [row["ssn"] for row in rows] == ["hidden", "hidden"]
    assert [row["email2"] for row in rows] == ["x@example.org", "y@example.org"]


def test_a_row_filter_follows_its_renamed_column(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """`dept = 'a'`, `dept` -> `department`: one row, id 1."""
    ref, _ = _people(catalog, principal, "staff")
    _filter(session, catalog, principal, "dept = 'a'")
    _rename(catalog, principal, ref, ("dept", "department"))

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))

    assert [(row["id"], row["department"]) for row in read.to_pylist()] == [(1, "a")]


def test_a_column_dropped_and_added_again_under_a_policy_name_refuses_the_read(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The name `ssn` now belongs to another field id."""
    ref, _ = _people(catalog, principal, "staff")
    _mask(session, catalog, principal, "staff", "ssn")
    table = catalog.load_table(principal, ref)
    table = _evolve(catalog, principal, ref, table, lambda u: u.delete_column("ssn"))
    _evolve(catalog, principal, ref, table, lambda u: u.add_column("ssn", StringType()))

    run = _query(session, principal, "SELECT * FROM gold.staff")

    _assert_refused(run, _column_gone("gold.staff", "ssn"))


def test_a_dropped_masked_column_refuses_the_read(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Drop `ssn`: the mask's field is gone, so even a read of `id` alone is refused."""
    ref, _ = _people(catalog, principal, "staff")
    _mask(session, catalog, principal, "staff", "ssn")
    table = catalog.load_table(principal, ref)
    _evolve(catalog, principal, ref, table, lambda u: u.delete_column("ssn"))

    run = _query(session, principal, "SELECT id FROM gold.staff")

    _assert_refused(run, _column_gone("gold.staff", "ssn"))


def test_a_retype_by_replacement_refuses_the_read(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A write with `evolve_schema` turns `ssn` from string to long: a new field, refused.

    The mask is bob's, so the admin's write is not refused for a policy of its own.
    """
    bob_id = uuid.uuid4()
    session.add(Principal(id=bob_id, type=PrincipalType.USER, name="bob"))
    session.commit()
    bob = PrincipalRef(id=bob_id, name="bob", type=PrincipalType.USER)
    _people(catalog, principal, "staff")
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="bob",
        environment="dev",
        namespace="gold",
        table="staff",
        column="ssn",
        expression="'hidden'",
    )
    write = QueryService(session, AllowAllAuthorization()).submit_write(
        principal,
        "dev",
        "SELECT id, dept, CAST(ssn AS BIGINT) AS ssn, email FROM gold.staff",
        WriteTarget(table="gold.staff", mode=WriteMode.REPLACE, evolve_schema=True),
    )
    run_one(session, write.id, AllowAllAuthorization())
    session.refresh(write)
    assert write.status == QueryRunStatus.SUCCEEDED, write.error_message

    _assert_refused(
        _query(session, bob, "SELECT id FROM gold.staff"), _column_gone("gold.staff", "ssn")
    )


def test_a_column_read_by_a_mask_expression_follows_its_rename(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Mask on `ssn` = `dept || '-x'`; `dept` -> `team`: applied with `team`'s values."""
    ref, _ = _people(catalog, principal, "staff")
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="staff",
        column="ssn",
        expression="dept || '-x'",
    )
    _rename(catalog, principal, ref, ("dept", "team"))

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))

    rows = sorted(read.to_pylist(), key=lambda row: row["id"])
    assert [row["ssn"] for row in rows] == ["a-x", "b-x"]


def test_a_policy_created_after_the_rename_reads(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The policy records the schema it was written against, so the earlier rename is not its."""
    ref, _ = _people(catalog, principal, "staff")
    _rename(catalog, principal, ref, ("ssn", "ssn_old"), ("email", "ssn"))
    _mask(session, catalog, principal, "staff", "ssn")

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))

    assert {row["ssn"] for row in read.to_pylist()} == {"hidden"}
    assert {row["ssn_old"] for row in read.to_pylist()} == {"111", "222"}


def test_the_documented_order_after_a_rename_never_shows_a_value_in_clear(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A legacy mask: rename, create for the new name, revoke the old one: refused or masked."""
    ref, _ = _people(catalog, principal, "staff")
    stale = _mask(session, catalog, principal, "staff", "ssn")
    _unbind(session, stale, UNBOUND)
    _rename(catalog, principal, ref, ("ssn", "social"))

    _assert_refused(
        _query(session, principal, "SELECT * FROM gold.staff"),
        _renamed_refusal("gold.staff", "ssn"),
    )
    _mask(session, catalog, principal, "staff", "social")
    # The old mask is still in force and still touched, so the read is still refused.
    _assert_refused(
        _query(session, principal, "SELECT * FROM gold.staff"),
        _renamed_refusal("gold.staff", "ssn"),
    )
    ColumnMaskService(session, AllowAllAuthorization(), catalog).revoke(principal, stale)

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))
    assert {row["social"] for row in read.to_pylist()} == {"hidden"}
    assert {row["email"] for row in read.to_pylist()} == {"x@example.org", "y@example.org"}


def test_a_schema_set_back_to_the_one_a_policy_was_written_for_reads_again(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A column replaced by a new one of its name refuses the read; the set-back undoes it."""
    ref, _ = _people(catalog, principal, "staff")
    _mask(session, catalog, principal, "staff", "ssn")
    original = catalog.load_table(principal, ref)
    table = _evolve(catalog, principal, ref, original, lambda u: u.delete_column("ssn"))
    table = _evolve(catalog, principal, ref, table, lambda u: u.add_column("ssn", StringType()))
    _assert_refused(
        _query(session, principal, "SELECT * FROM gold.staff"), _column_gone("gold.staff", "ssn")
    )

    first = original.metadata.current_schema_id
    catalog.commit_table(
        principal,
        ref,
        requirements=[],
        updates=[SetCurrentSchemaUpdate(schema_id=first)],
    )

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))
    assert {row["ssn"] for row in read.to_pylist()} == {"hidden"}


def test_a_schema_set_back_before_the_policys_column_refuses_the_read(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Review Focus 2: add `tier`, mask it, set the schema back to one without it: gone."""
    ref, _ = _people(catalog, principal, "staff")
    original = catalog.load_table(principal, ref)
    _evolve(catalog, principal, ref, original, lambda u: u.add_column("tier", StringType()))
    _mask(session, catalog, principal, "staff", "tier")

    catalog.commit_table(
        principal,
        ref,
        requirements=[],
        updates=[SetCurrentSchemaUpdate(schema_id=original.metadata.current_schema_id)],
    )

    _assert_refused(
        _query(session, principal, "SELECT * FROM gold.staff"),
        _column_gone("gold.staff", "tier"),
    )


def test_a_column_renamed_and_renamed_back_reads_as_before(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Spec "rename undone": the mask covers `ssn` again."""
    ref, _ = _people(catalog, principal, "staff")
    _mask(session, catalog, principal, "staff", "ssn")
    _rename(catalog, principal, ref, ("ssn", "x"), ("x", "ssn"))

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))

    assert {row["ssn"] for row in read.to_pylist()} == {"hidden"}


def test_creating_the_policy_again_after_the_refusal_reads(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A legacy mask: the administrator revokes it and creates it for the column it is meant for."""
    ref, _ = _people(catalog, principal, "staff")
    stale = _mask(session, catalog, principal, "staff", "ssn")
    _unbind(session, stale, UNBOUND)
    _rename(catalog, principal, ref, ("ssn", "ssn_old"))
    _assert_refused(
        _query(session, principal, "SELECT * FROM gold.staff"),
        _renamed_refusal("gold.staff", "ssn"),
    )

    ColumnMaskService(session, AllowAllAuthorization(), catalog).revoke(principal, stale)
    _mask(session, catalog, principal, "staff", "ssn_old")

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))
    assert {row["ssn_old"] for row in read.to_pylist()} == {"hidden"}


def test_a_policy_without_a_recorded_schema_is_judged_from_the_oldest_schema(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A policy of an earlier release has no schema id; a rename in the history touches it."""
    ref, _ = _people(catalog, principal, "staff")
    _rename(catalog, principal, ref, ("email", "mail"))
    mask = _mask(session, catalog, principal, "staff", "mail")
    _unbind(session, mask, UNBOUND)
    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))
    assert {row["mail"] for row in read.to_pylist()} == {"hidden"}

    row = session.get(ColumnMask, mask)
    assert row is not None
    row.schema_id = None
    session.commit()

    _assert_refused(
        _query(session, principal, "SELECT * FROM gold.staff"),
        _renamed_refusal("gold.staff", "mail"),
    )


def test_an_old_release_mask_on_a_column_added_later_is_refused_after_a_takeover(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """The mask names no schema and `ssn` was added after the table was made, then taken over."""
    ref = _ref("staff")
    created = catalog.create_table(principal, ref, schema=Schema(_optional(1, "id", LongType())))
    table = _append(
        catalog, principal, ref, created, pa.table({"id": pa.array([1, 2], pa.int64())})
    )
    table = _evolve(catalog, principal, ref, table, lambda u: u.add_column("ssn", StringType()))
    mask = _mask(session, catalog, principal, "staff", "ssn")
    _unbind(session, mask, UNBOUND)
    row = session.get(ColumnMask, mask)
    assert row is not None
    row.schema_id = None
    session.commit()
    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))
    assert {r["ssn"] for r in read.to_pylist()} == {"hidden"}

    table = _evolve(catalog, principal, ref, table, lambda u: u.rename_column("ssn", "ssn_old"))
    _evolve(catalog, principal, ref, table, lambda u: u.add_column("ssn", StringType()))

    _assert_refused(
        _query(session, principal, "SELECT * FROM gold.staff"),
        _renamed_refusal("gold.staff", "ssn"),
    )


@pytest.mark.parametrize("binding", [PENDING, UNBOUND])
def test_a_policy_not_bound_by_id_keeps_the_rename_rule(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    binding: str,
) -> None:
    """A `pending` or `unbound` mask on `ssn`, then the swap: today's refusal, not a follow."""
    ref, _ = _people(catalog, principal, "staff")
    _unbind(session, _mask(session, catalog, principal, "staff", "ssn"), binding)
    _rename(catalog, principal, ref, ("ssn", "ssn_old"), ("email", "ssn"))

    _assert_refused(
        _query(session, principal, "SELECT * FROM gold.staff"),
        _renamed_refusal("gold.staff", "ssn"),
    )


def test_the_policy_is_resolved_against_the_fields_the_read_projects(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review Focus 5: the projected fields and the same metadata's current schema reach it."""
    ref, _ = _people(catalog, principal, "staff")
    _mask(session, catalog, principal, "staff", "ssn")
    _rename(catalog, principal, ref, ("dept", "team"))
    schemas: list[PolicySchema | None] = []
    projected: list[tuple[NestedField, ...]] = []
    real_resolve = PolicyResolver.resolve
    real_read_fields = pipeline_module.read_fields

    def resolve(self: PolicyResolver, *args: object, **kwargs: object) -> ResolvedPolicy:
        schema = kwargs.get("schema")
        assert schema is None or isinstance(schema, PolicySchema)
        schemas.append(schema)
        return real_resolve(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    def read_fields(table: str, fields: list[NestedField]) -> object:
        projected.append(tuple(fields))
        return real_read_fields(table, fields)

    monkeypatch.setattr(PolicyResolver, "resolve", resolve)
    monkeypatch.setattr(pipeline_module, "read_fields", read_fields)

    _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))

    [schema] = schemas
    assert schema is not None
    assert schema.read == projected[0]
    assert schema.current == tuple(catalog.load_table(principal, ref).metadata.schema().fields)
    assert [field.name for field in schema.read] == ["id", "team", "ssn", "email"]


def test_a_policy_of_another_reader_does_not_refuse_this_reader(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Only a policy that applies to the reader can make the read stale."""
    session.add(Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="bob"))
    session.commit()
    ref, _ = _people(catalog, principal, "staff")
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="bob",
        environment="dev",
        namespace="gold",
        table="staff",
        column="ssn",
        expression="'hidden'",
    )
    _rename(catalog, principal, ref, ("ssn", "ssn_old"), ("email", "ssn"))

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM gold.staff"))

    assert {row["ssn"] for row in read.to_pylist()} == {"x@example.org", "y@example.org"}


def test_a_read_of_an_external_table_is_not_affected_by_the_rule(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """Raw files have no field ids; a mask binds by name and the files are read by name."""
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(
        pa.table({"id": [1], "tier": ["gold"]}), tmp_path / "raw" / "events" / "a.parquet"
    )
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events"),
        location_pattern="raw/events/*.parquet",
        format="parquet",
    )
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="raw",
        table="events",
        column="tier",
        expression="'hidden'",
    )

    read = _result(tmp_path, _query(session, principal, "SELECT * FROM raw.events"))

    assert read.to_pylist() == [{"id": 1, "tier": "hidden"}]


@pytest.mark.parametrize("order", ["listed", "reversed"])
def test_an_external_table_whose_files_lack_a_declared_column_fails_under_a_row_filter(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    order: str,
) -> None:
    """The first file has `tier` (the schema is inferred from it), the second lacks it.

    The read fails with the raw-zone text in either order of the files, and shows nothing.
    """
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    directory = tmp_path / "raw" / "events"
    directory.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1], "tier": ["gold"]}), directory / "a-with-tier.parquet")
    pq.write_table(pa.table({"id": [2]}), directory / "b-without-tier.parquet")
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events"),
        location_pattern="raw/events/*.parquet",
        format="parquet",
    )
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="raw",
        table="events",
        expression="tier = 'gold'",
    )
    seen: list[list[str]] = []
    real = FsspecStorage.list

    def listing(self: FsspecStorage, prefix: str) -> list[str]:
        files = list(real(self, prefix))
        seen.append(files)
        return list(reversed(files)) if order == "reversed" else files

    monkeypatch.setattr(FsspecStorage, "list", listing)

    run = _query(session, principal, "SELECT * FROM raw.events")

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith("The data files of `raw.events` do not all have the column")
    assert "`tier`" in run.error_message
    assert run.result_location is None
    first = [name for name in seen[0] if name.endswith(".parquet")]
    assert len(first) == 2


@pytest.mark.parametrize(
    "corrupt", [{"field_ids": []}, {"column_field_id": None}, {"binding": "names2"}]
)
def test_a_corrupt_bound_policy_names_itself_unreadable(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    corrupt: dict[str, object],
) -> None:
    """A bound mask whose stored binding cannot be read: the reader is told so, nothing shown."""
    _people(catalog, principal, "staff")
    mask = session.get(ColumnMask, _mask(session, catalog, principal, "staff", "ssn"))
    assert mask is not None
    for key, value in corrupt.items():
        setattr(mask, key, value)
    session.commit()

    _assert_refused(
        _query(session, principal, "SELECT id FROM gold.staff"), UNREADABLE_POLICY_MESSAGE
    )
