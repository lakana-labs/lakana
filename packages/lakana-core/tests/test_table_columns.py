"""The stored columns of a table: written on every schema change, filled for older tables."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import _config
from pyiceberg.io import FileIO
from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.update import AddSchemaUpdate, SetCurrentSchemaUpdate
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.catalog import columns
from lakana_core.catalog.columns import (
    backfill_table_columns,
    stored_columns,
    sync_table_columns,
)
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Table, TableColumn, TableType
from lakana_core.storage.resolver import StorageResolver

_SILVER = NamespacePath.from_parts(["silver"])
_ORDERS = TableRef("dev", _SILVER, "orders")
_SCHEMA = Schema(
    NestedField(1, "id", LongType(), required=True),
    NestedField(2, "status", StringType(), required=False),
)


def _stored(session: Session, name: str) -> list[tuple[str, str, int]]:
    """Read the stored columns of a table.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name.

    Returns
    -------
    list of tuple
        ``(name, type, position)`` per column, in position order.
    """
    rows = session.scalars(
        select(TableColumn)
        .join(Table, Table.id == TableColumn.table_id)
        .where(Table.name == name)
        .order_by(TableColumn.position)
    )
    return [(r.name, r.type, r.position) for r in rows]


@pytest.fixture
def silver(catalog: CatalogService, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` with its medallion namespaces and `raw`.

    Parameters
    ----------
    catalog
        The allow-all catalog.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    return catalog


def test_create_table_stores_its_columns(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    silver.create_table(principal, _ORDERS, schema=_SCHEMA)

    assert _stored(session, "orders") == [("id", "long", 0), ("status", "string", 1)]


def test_a_commit_that_adds_a_column_stores_it_and_one_that_drops_it_removes_it(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    silver.create_table(principal, _ORDERS, schema=_SCHEMA)
    current = silver.load_table(principal, _ORDERS).metadata.schema()
    widened = Schema(
        *current.fields,
        NestedField(3, "amount", LongType(), required=False),
        schema_id=current.schema_id + 1,
    )
    silver.commit_table(
        principal,
        _ORDERS,
        requirements=[],
        updates=[AddSchemaUpdate(schema_=widened), SetCurrentSchemaUpdate(schema_id=-1)],
    )
    assert _stored(session, "orders") == [
        ("id", "long", 0),
        ("status", "string", 1),
        ("amount", "long", 2),
    ]

    narrowed = Schema(current.fields[0], schema_id=current.schema_id + 2)
    silver.commit_table(
        principal,
        _ORDERS,
        requirements=[],
        updates=[AddSchemaUpdate(schema_=narrowed), SetCurrentSchemaUpdate(schema_id=-1)],
    )
    assert _stored(session, "orders") == [("id", "long", 0)]


def test_a_view_stores_its_columns_and_a_replacement_changes_them(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    ref = TableRef("dev", NamespacePath.from_parts(["gold"]), "v")
    silver.create_or_replace_view(
        principal,
        ref,
        sql="SELECT 1 AS a",
        references=[],
        columns=[ColumnSpec("a", "long", True)],
    )
    assert _stored(session, "v") == [("a", "long", 0)]

    silver.create_or_replace_view(
        principal,
        ref,
        sql="SELECT 1 AS b",
        references=[],
        columns=[ColumnSpec("b", "string", False), ColumnSpec("a", "long", True)],
    )
    assert _stored(session, "v") == [("b", "string", 0), ("a", "long", 1)]


def test_an_external_table_stores_its_inferred_columns(
    session: Session, principal: PrincipalRef, silver: CatalogService, tmp_path: Path
) -> None:
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1], "name": ["a"]}), tmp_path / "raw" / "events" / "p.parquet")

    silver.create_external_table(
        principal,
        TableRef("dev", NamespacePath.from_parts(["raw"]), "events"),
        location_pattern="raw/events/*.parquet",
        format="parquet",
    )

    assert [(n, p) for n, _, p in _stored(session, "events")] == [("id", 0), ("name", 1)]


def test_sync_keeps_the_rows_it_can_and_is_idempotent(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    silver.create_table(principal, _ORDERS, schema=_SCHEMA)
    table_id = session.scalars(select(Table.id).where(Table.name == "orders")).one()
    before = {r.name: r.id for r in session.scalars(select(TableColumn))}

    sync_table_columns(
        session, table_id, [ColumnSpec("status", "string", True), ColumnSpec("id", "long", False)]
    )
    session.commit()

    assert _stored(session, "orders") == [("status", "string", 0), ("id", "long", 1)]
    assert {r.name: r.id for r in session.scalars(select(TableColumn))} == before


def test_stored_columns_reads_names_and_types_in_position_order(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    """A table's rows in position order, `{}` without rows, `None` for no such table."""
    silver.create_table(principal, _ORDERS, schema=_SCHEMA)
    silver.create_table(principal, TableRef("dev", _SILVER, "bare"), schema=_SCHEMA)
    table_id = session.scalars(select(Table.id).where(Table.name == "orders")).one()
    sync_table_columns(
        session, table_id, [ColumnSpec("status", "string", True), ColumnSpec("id", "long", False)]
    )
    bare_id = session.scalars(select(Table.id).where(Table.name == "bare")).one()
    session.execute(delete(TableColumn).where(TableColumn.table_id == bare_id))
    session.commit()

    stored = stored_columns(session, "dev", "silver.orders")
    assert stored == {"status": "string", "id": "long"}
    assert list(stored or {}) == ["status", "id"]
    assert stored_columns(session, "dev", "silver.bare") == {}
    assert stored_columns(session, "dev", "silver.missing") is None
    assert stored_columns(session, "dev", "nodot") is None


def test_backfill_fills_a_table_without_rows_and_skips_a_filled_one(
    session: Session, principal: PrincipalRef, silver: CatalogService, tmp_path: Path
) -> None:
    silver.create_table(principal, _ORDERS, schema=_SCHEMA)
    silver.create_table(principal, TableRef("dev", _SILVER, "items"), schema=_SCHEMA)
    gold = NamespacePath.from_parts(["gold"])
    silver.create_or_replace_view(
        principal,
        TableRef("dev", gold, "v"),
        sql="SELECT 1 AS a",
        references=[],
        columns=[ColumnSpec("a", "long", True)],
    )
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "p.parquet")
    silver.create_external_table(
        principal,
        TableRef("dev", NamespacePath.from_parts(["raw"]), "events"),
        location_pattern="raw/events/*.parquet",
        format="parquet",
    )
    items_id = session.scalars(select(Table.id).where(Table.name == "items")).one()
    session.execute(delete(TableColumn).where(TableColumn.table_id != items_id))
    session.execute(delete(TableColumn).where(TableColumn.table_id == items_id))
    sync_table_columns(session, items_id, [ColumnSpec("kept", "long", True)])
    session.commit()

    filled = backfill_table_columns(session, StorageResolver(session))

    assert filled == 3
    assert _stored(session, "orders") == [("id", "long", 0), ("status", "string", 1)]
    assert _stored(session, "v") == [("a", "long", 0)]
    assert [(n, p) for n, _, p in _stored(session, "events")] == [("id", 0)]
    assert _stored(session, "items") == [("kept", "long", 0)]
    assert backfill_table_columns(session, StorageResolver(session)) == 0


def test_backfill_takes_at_most_limit_tables_per_pass(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    for name in ("a", "b", "c"):
        silver.create_table(principal, TableRef("dev", _SILVER, name), schema=_SCHEMA)
    session.execute(delete(TableColumn))
    session.commit()

    assert backfill_table_columns(session, StorageResolver(session), limit=2) == 2
    assert backfill_table_columns(session, StorageResolver(session), limit=2) == 1
    assert backfill_table_columns(session, StorageResolver(session), limit=2) == 0


def test_backfill_skips_a_table_it_cannot_read_and_fills_the_others(
    session: Session,
    principal: PrincipalRef,
    silver: CatalogService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    silver.create_table(principal, _ORDERS, schema=_SCHEMA)
    silver.create_table(principal, TableRef("dev", _SILVER, "items"), schema=_SCHEMA)
    session.execute(delete(TableColumn))
    session.commit()
    real = columns.read_metadata
    calls = {"n": 0}

    def flaky(file_io: FileIO, location: str) -> TableMetadata:
        """Fail the first read, like an unreachable metadata file.

        Parameters
        ----------
        file_io
            Passed through.
        location
            Passed through.

        Returns
        -------
        object
            The metadata, from the second read on.

        Raises
        ------
        OSError
            On the first read.
        """
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("gone")
        return real(file_io, location)

    with mock.patch("lakana_core.catalog.columns.read_metadata", flaky):
        assert backfill_table_columns(session, StorageResolver(session)) == 1

    assert "Could not read the columns" in caplog.text
    assert len(session.scalars(select(TableColumn)).all()) == 2


def test_backfill_ignores_artifacts_and_tables_without_metadata(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    silver.create_table(principal, _ORDERS, schema=_SCHEMA)
    row = session.scalars(select(Table)).one()
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=row.namespace_id,
            name="blob",
            table_type=TableType.ARTIFACT,
        )
    )
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=row.namespace_id,
            name="bare",
            table_type=TableType.ICEBERG,
        )
    )
    session.execute(delete(TableColumn))
    session.commit()

    assert backfill_table_columns(session, StorageResolver(session)) == 1
