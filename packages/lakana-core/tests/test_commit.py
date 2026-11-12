"""Tests for the compare-and-swap metadata pointer commit."""

import uuid
from dataclasses import astuple
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pytest
from core_test_helpers import _prepared_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.commit import commit_metadata_pointer
from lakana_core.catalog.health import StoredStats, stored_stats
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.usage import measure_table_sizes
from lakana_core.errors import ConflictError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Environment, Namespace, Table, TableType
from lakana_core.storage.resolver import StorageResolver


def _table(session: Session, *, metadata_location: str | None) -> Table:
    """Insert an environment, a namespace and a table.

    Parameters
    ----------
    session
        Database session.
    metadata_location
        The table's starting pointer value.

    Returns
    -------
    Table
        The persisted table row.
    """
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana",
        storage_options={},
        is_protected=False,
        properties={},
    )
    ns = Namespace(id=uuid.uuid4(), environment_id=env.id, path="gold", properties={})
    tbl = Table(
        id=uuid.uuid4(),
        namespace_id=ns.id,
        name="sales",
        table_type=TableType.ICEBERG,
        metadata_location=metadata_location,
        previous_metadata_location=None,
    )
    session.add_all([env, ns, tbl])
    session.commit()
    return tbl


def test_swap_from_null_succeeds(session: Session) -> None:
    """The first commit moves the pointer off NULL."""
    tbl = _table(session, metadata_location=None)

    commit_metadata_pointer(
        session, tbl.id, expected_location=None, new_location="file:///m/00000-x.json"
    )

    session.expire_all()
    stored = session.execute(select(Table).where(Table.id == tbl.id)).scalar_one()
    assert stored.metadata_location == "file:///m/00000-x.json"
    assert stored.previous_metadata_location is None


def test_swap_from_a_value_succeeds_and_records_the_previous(session: Session) -> None:
    """A subsequent commit keeps the location it replaced."""
    tbl = _table(session, metadata_location="file:///m/00000-a.json")

    commit_metadata_pointer(
        session,
        tbl.id,
        expected_location="file:///m/00000-a.json",
        new_location="file:///m/00001-b.json",
    )

    session.expire_all()
    stored = session.execute(select(Table).where(Table.id == tbl.id)).scalar_one()
    assert stored.metadata_location == "file:///m/00001-b.json"
    assert stored.previous_metadata_location == "file:///m/00000-a.json"


def test_a_stale_expectation_raises_conflict(session: Session) -> None:
    """The losing writer of a race is told, not silently ignored."""
    tbl = _table(session, metadata_location="file:///m/00001-current.json")

    with pytest.raises(ConflictError):
        commit_metadata_pointer(
            session,
            tbl.id,
            expected_location="file:///m/00000-stale.json",
            new_location="file:///m/00002-mine.json",
        )


def test_a_lost_race_leaves_the_pointer_untouched(session: Session) -> None:
    """A failed CAS must not be a partial write."""
    tbl = _table(session, metadata_location="file:///m/00001-current.json")

    with pytest.raises(ConflictError):
        commit_metadata_pointer(
            session,
            tbl.id,
            expected_location="file:///m/00000-stale.json",
            new_location="file:///m/00002-mine.json",
        )

    session.expire_all()
    stored = session.execute(select(Table).where(Table.id == tbl.id)).scalar_one()
    assert stored.metadata_location == "file:///m/00001-current.json"


def test_expecting_null_when_a_value_is_present_raises_conflict(session: Session) -> None:
    """Two creators racing: the second must lose, not overwrite."""
    tbl = _table(session, metadata_location="file:///m/00000-theirs.json")

    with pytest.raises(ConflictError):
        commit_metadata_pointer(
            session, tbl.id, expected_location=None, new_location="file:///m/00000-mine.json"
        )


def test_expecting_a_value_when_null_is_present_raises_conflict(session: Session) -> None:
    """The NULL comparison must work in both directions."""
    tbl = _table(session, metadata_location=None)

    with pytest.raises(ConflictError):
        commit_metadata_pointer(
            session,
            tbl.id,
            expected_location="file:///m/00000-a.json",
            new_location="file:///m/00001-b.json",
        )


def test_swap_only_touches_the_addressed_table(session: Session) -> None:
    """The CAS must match on table id, not on location alone.

    Two tables sharing a namespace can legitimately be at the same metadata
    location text (e.g. both freshly created). Addressing must not depend on
    the comparison to tell them apart.
    """
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana",
        storage_options={},
        is_protected=False,
        properties={},
    )
    ns = Namespace(id=uuid.uuid4(), environment_id=env.id, path="gold", properties={})
    target = Table(
        id=uuid.uuid4(),
        namespace_id=ns.id,
        name="sales",
        table_type=TableType.ICEBERG,
        metadata_location="file:///m/00000-a.json",
        previous_metadata_location=None,
    )
    other = Table(
        id=uuid.uuid4(),
        namespace_id=ns.id,
        name="orders",
        table_type=TableType.ICEBERG,
        metadata_location="file:///m/00000-a.json",
        previous_metadata_location=None,
    )
    session.add_all([env, ns, target, other])
    session.commit()

    commit_metadata_pointer(
        session,
        target.id,
        expected_location="file:///m/00000-a.json",
        new_location="file:///m/00001-b.json",
    )

    session.expire_all()
    stored_other = session.execute(select(Table).where(Table.id == other.id)).scalar_one()
    assert stored_other.metadata_location == "file:///m/00000-a.json"
    assert stored_other.previous_metadata_location is None


def test_an_unknown_table_raises_conflict(session: Session) -> None:
    """No row matched, so the swap did not happen — the caller must know."""
    with pytest.raises(ConflictError):
        commit_metadata_pointer(
            session, uuid.uuid4(), expected_location=None, new_location="file:///m/x.json"
        )


def test_the_pointer_swap_clears_the_marker_only_when_asked(session: Session) -> None:
    tbl = _table(session, metadata_location="loc0")
    tbl.unpublished_run_id = uuid.uuid4()
    session.commit()
    marker = tbl.unpublished_run_id

    commit_metadata_pointer(session, tbl.id, expected_location="loc0", new_location="loc1")
    session.commit()
    session.refresh(tbl)
    assert (tbl.metadata_location, tbl.unpublished_run_id) == ("loc1", marker)

    commit_metadata_pointer(
        session, tbl.id, expected_location="loc1", new_location="loc2", publish=True
    )
    session.commit()
    session.refresh(tbl)
    assert (tbl.metadata_location, tbl.unpublished_run_id) == ("loc2", None)


def test_a_swap_with_stats_stores_them_and_the_file_they_were_read_from(session: Session) -> None:
    tbl = _table(session, metadata_location="loc0")
    written = datetime(2026, 11, 12, 8, 0, tzinfo=UTC)

    commit_metadata_pointer(
        session,
        tbl.id,
        expected_location="loc0",
        new_location="loc1",
        stats=StoredStats(row_count=7, size_bytes=900, last_write_at=written),
    )
    session.commit()
    session.expire_all()
    stored = session.get(Table, tbl.id)
    assert stored is not None
    assert (stored.row_count, stored.size_bytes, stored.last_write_at) == (7, 900, written)
    assert stored.size_metadata_location == "loc1"


def test_a_swap_without_stats_leaves_the_stored_ones(session: Session) -> None:
    tbl = _table(session, metadata_location="loc0")
    tbl.row_count = 5
    session.commit()

    commit_metadata_pointer(session, tbl.id, expected_location="loc0", new_location="loc1")
    session.commit()
    session.expire_all()
    stored = session.get(Table, tbl.id)
    assert stored is not None
    assert (stored.row_count, stored.size_metadata_location) == (5, None)


_SCHEMA = Schema(NestedField(1, "id", LongType(), required=True))
_ROWS = pa.table(
    {"id": pa.array([1, 2, 3], pa.int64())},
    schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
)
_REF = TableRef("dev", NamespacePath.from_parts(["gold"]), "sales")


def _appended(session: Session, tmp_path: Path, principal: PrincipalRef) -> CatalogService:
    """Build a catalog whose `gold.sales` has had three rows appended.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The storage root.
    principal
        The acting principal.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[_REF.namespace]
    )
    catalog.create_table(principal, _REF, schema=_SCHEMA)
    catalog.append(principal, _REF, _ROWS)
    return catalog


def _stored(session: Session) -> Table:
    """Read `gold.sales` afresh.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    Table
        The row.
    """
    session.expire_all()
    return session.execute(select(Table).where(Table.name == "sales")).scalar_one()


def test_a_created_table_stores_no_rows_zero_bytes_and_the_file_they_describe(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[_REF.namespace]
    )
    catalog.create_table(principal, _REF, schema=_SCHEMA)

    row = _stored(session)
    assert (row.row_count, row.size_bytes, row.last_write_at) == (None, 0, None)
    assert row.size_metadata_location == row.metadata_location is not None


def test_a_commit_stores_the_stats_of_its_snapshot(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _appended(session, tmp_path, principal)
    stats = stored_stats(catalog.load_table(principal, _REF).metadata)

    row = _stored(session)
    assert (row.row_count, row.size_bytes, row.last_write_at) == astuple(stats)
    assert row.row_count == 3
    assert row.size_metadata_location == row.metadata_location


def test_commit_and_sweep_store_the_same_values_for_the_same_metadata(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    _appended(session, tmp_path, principal)
    row = _stored(session)
    after_commit = (row.row_count, row.size_bytes, row.last_write_at)
    session.execute(
        update(Table).values(
            row_count=None, size_bytes=None, last_write_at=None, size_metadata_location=None
        )
    )
    session.commit()

    assert measure_table_sizes(session, StorageResolver(session)) == 1
    row = _stored(session)
    assert (row.row_count, row.size_bytes, row.last_write_at) == after_commit


def test_a_publishing_swap_that_lost_the_race_keeps_the_marker(session: Session) -> None:
    tbl = _table(session, metadata_location="loc0")
    tbl.unpublished_run_id = uuid.uuid4()
    session.commit()
    marker = tbl.unpublished_run_id

    with pytest.raises(ConflictError):
        commit_metadata_pointer(
            session, tbl.id, expected_location="stale", new_location="loc1", publish=True
        )
    session.rollback()
    session.refresh(tbl)
    assert tbl.unpublished_run_id == marker
