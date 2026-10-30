"""A drop leaves a tombstone for an undo period; `undrop_table` restores the table exactly.

The drop keeps the table's files and its location record, which becomes `DROPPED` with the row's
snapshot and the ids of exactly the grants, row filters and column masks the drop revoked. The
sweep deletes the files once `purge_after` has passed. Within the period, `undrop_table` brings
the same table back: the same id, the same metadata, and those policies. These tests run on a
SQLite file.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import ActionAllowList, FakeColumnCatalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_table_writes import _orders, _ref, _rows, _write

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
    SecurableKind,
)
from lakana_core.catalog.model_metadata import (
    incoming_references,
    read_table_model,
    write_model_entry,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.tombstones import (
    DEFAULT_UNDO_DAYS,
    FILES_GONE_MESSAGE,
    NAME_TAKEN_MESSAGE,
    NAMESPACE_GONE_MESSAGE,
    NO_TOMBSTONE_MESSAGE,
    PERIOD_PASSED_MESSAGE,
    PURGE_STARTED_MESSAGE,
)
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import AlreadyExistsError, ConflictError, NotFoundError, PermissionDenied
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    ColumnDescription,
    ColumnMask,
    Grant,
    Namespace,
    Principal,
    PrincipalType,
    RowFilter,
    Table,
    TableColumn,
    TableLocation,
    TableLocationState,
    TableType,
    WriteMode,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.runs.lockdown import PROTECTED_MESSAGE
from lakana_core.storage.resolver import StorageResolver

_GOLD = NamespacePath.from_parts(["gold"])
_M = TypeVar("_M")


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    """Provide a session on a SQLite file (overrides the in-memory one).

    Parameters
    ----------
    tmp_path
        Holds the database file and, under `store/`, the environment's storage.

    Yields
    ------
    Session
        A session whose schema has been created from the models.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'catalog.db'}"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Return the storage root of `dev`.

    Parameters
    ----------
    tmp_path
        The test's directory.

    Returns
    -------
    Path
        The directory.
    """
    root = tmp_path / "store"
    root.mkdir()
    return root


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, store: Path) -> CatalogService:
    """Create `dev` on `store` and return a catalog over it with allow-all authorization.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    store
        The storage root.

    Returns
    -------
    CatalogService
        The catalog, with the default undo period.
    """
    _catalog(session).create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{store}", options={}),
    )
    return _catalog(session)


def _catalog(
    session: Session,
    authz: object | None = None,
    **options: int,
) -> CatalogService:
    """Build a catalog on `session`.

    Parameters
    ----------
    session
        Database session.
    authz
        Its authorization; allow-all when `None`.
    **options
        Passed to `CatalogService` (`undo_days`).

    Returns
    -------
    CatalogService
        The catalog.
    """
    return CatalogService(
        session,
        StorageResolver(session),
        authz or AllowAllAuthorization(),  # ty: ignore[invalid-argument-type]
        **options,  # ty: ignore[invalid-argument-type]
    )


def _principal(session: Session, name: str) -> PrincipalRef:
    """Insert a user.

    Parameters
    ----------
    session
        Database session; commits.
    name
        The user's name.

    Returns
    -------
    PrincipalRef
        The user.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _policies(
    session: Session, admin: PrincipalRef, ref: TableRef, subject: str = "alice"
) -> tuple[Grant, RowFilter, ColumnMask]:
    """Give `subject` a grant, a row filter and a column mask on `ref`.

    Parameters
    ----------
    session
        Database session.
    admin
        Who creates them.
    ref
        The table.
    subject
        The principal they apply to; created when missing.

    Returns
    -------
    tuple of (Grant, RowFilter, ColumnMask)
        The three rows.
    """
    if session.scalar(select(Principal).where(Principal.name == subject)) is None:
        _principal(session, subject)
    allow = AllowAllAuthorization()
    grant = GrantService(session, allow).grant(
        admin,
        principal=subject,
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment=ref.environment,
        namespace=ref.namespace.joined(),
        table=ref.name,
    )
    common = {
        "principal": subject,
        "environment": ref.environment,
        "namespace": ref.namespace.joined(),
        "table": ref.name,
    }
    row_filter = RowFilterService(session, allow, FakeColumnCatalog()).create(
        admin, expression="region = 'eu'", **common
    )
    mask = ColumnMaskService(session, allow, FakeColumnCatalog()).create(
        admin, column="region", expression="'x'", **common
    )
    return grant, row_filter, mask


def _revoked_policies(session: Session, admin: PrincipalRef, ref: TableRef) -> Grant:
    """Give `bob` a grant, a row filter and a column mask on `ref`, and revoke all three.

    Parameters
    ----------
    session
        Database session.
    admin
        Who creates and revokes them.
    ref
        The table.

    Returns
    -------
    Grant
        The revoked grant.
    """
    grant, row_filter, mask = _policies(session, admin, ref, subject="bob")
    allow = AllowAllAuthorization()
    GrantService(session, allow).revoke(admin, grant.id)
    RowFilterService(session, allow, FakeColumnCatalog()).revoke(admin, row_filter.id)
    ColumnMaskService(session, allow, FakeColumnCatalog()).revoke(admin, mask.id)
    return grant


def _record(session: Session, table_id: uuid.UUID) -> TableLocation:
    """Read a table's location record fresh from the database.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.

    Returns
    -------
    TableLocation
        The record.
    """
    session.rollback()
    found = session.scalars(
        select(TableLocation)
        .where(TableLocation.table_id == table_id)
        .execution_options(populate_existing=True)
    ).one()
    session.rollback()
    return found


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one kind, oldest first.

    Parameters
    ----------
    session
        Database session.
    kind
        The event type.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    found = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == kind)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )
    session.rollback()
    return found


def _fresh(session: Session, model: type[_M], row_id: uuid.UUID) -> _M:
    """Read a row again from the database.

    Parameters
    ----------
    session
        Database session.
    model
        The row's model.
    row_id
        Its id.

    Returns
    -------
    _M
        The row as stored now.
    """
    session.rollback()
    found = session.get(model, row_id, populate_existing=True)
    assert found is not None
    return found


def _dropped_at(session: Session, table_id: uuid.UUID) -> datetime:
    """Read when a table was dropped.

    Parameters
    ----------
    session
        Database session.
    table_id
        The dropped table.

    Returns
    -------
    datetime
        Its tombstone's `dropped_at`.
    """
    dropped_at = _record(session, table_id).dropped_at
    assert dropped_at is not None
    return dropped_at


def _orders_table(catalog: CatalogService, principal: PrincipalRef) -> uuid.UUID:
    """Create `gold.orders` with three rows.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The creator.

    Returns
    -------
    uuid.UUID
        The table's id.
    """
    result = _write(
        catalog, principal, "gold.orders", WriteMode.CREATE, _orders([1, 2, 3], ["eu", "us", "eu"])
    )
    return result.table_id


def _expire(session: Session, table_id: uuid.UUID, *, started: bool = False) -> None:
    """Move a tombstone's `purge_after` into the past, or mark its purge as started.

    Parameters
    ----------
    session
        Database session; commits.
    table_id
        The dropped table.
    started
        Mark the purge as started instead, leaving `purge_after` in the future.
    """
    session.rollback()
    record = _record(session, table_id)
    assert record.dropped_at is not None
    values = (
        {"purge_started_at": record.dropped_at}
        if started
        else {"purge_after": record.dropped_at - timedelta(seconds=1)}
    )
    session.execute(update(TableLocation).where(TableLocation.id == record.id).values(**values))
    session.commit()


# --- the drop ---------------------------------------------------------------------------------


def test_a_drop_leaves_a_tombstone_with_the_row_and_exactly_the_policies_it_revoked(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """DROPPED, `purge_after` seven days on, the snapshot, the ids, the files kept, audited."""
    table_id = _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    earlier = _revoked_policies(session, principal, ref)
    grant, row_filter, mask = _policies(session, principal, ref)
    metadata_location = catalog.load_table(principal, ref).metadata_location
    files = sorted(p for p in store.rglob("*") if p.is_file())

    catalog.drop_table(principal, ref)

    record = _record(session, table_id)
    assert record.state is TableLocationState.DROPPED
    assert record.dropped_at is not None and record.purge_after is not None
    assert DEFAULT_UNDO_DAYS == 7
    assert record.purge_after - record.dropped_at == timedelta(days=7)
    assert record.purge_started_at is None
    by_kind = {(entry["kind"], entry["id"]) for entry in record.revoked_policy_ids}
    assert by_kind == {
        ("grant", str(grant.id)),
        ("row_filter", str(row_filter.id)),
        ("column_mask", str(mask.id)),
    }
    assert str(earlier.id) not in {entry["id"] for entry in record.revoked_policy_ids}
    snapshot = record.dropped_table
    assert snapshot is not None
    assert snapshot["row"]["id"] == str(table_id)
    assert snapshot["row"]["name"] == "orders"
    assert snapshot["row"]["metadata_location"] == metadata_location
    assert snapshot["row"]["owner_id"] == str(principal.id)
    assert snapshot["namespace"] == ["gold"]
    assert sorted(p for p in store.rglob("*") if p.is_file()) == files
    (event,) = _events(session, AuditEventType.TABLE_DROPPED)
    assert event.principal_id == principal.id
    assert event.securable_table == "orders"
    assert event.detail == {
        "reason": "user",
        "table_id": str(table_id),
        "kind": "TABLE",
        "purge_after": record.purge_after.isoformat(),
        "revoked_policies": 3,
    }


def test_purge_makes_the_tombstone_due_at_once(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`purge=True` sets `purge_after` to the drop's time and says so in the audit event."""
    table_id = _orders_table(catalog, principal)

    catalog.drop_table(principal, _ref("gold.orders"), purge=True)

    record = _record(session, table_id)
    assert record.state is TableLocationState.DROPPED
    assert record.dropped_at is not None
    assert record.purge_after == record.dropped_at
    (event,) = _events(session, AuditEventType.TABLE_DROPPED)
    assert event.detail["reason"] == "purge"
    assert event.detail["purge_after"] == record.dropped_at.isoformat()


def test_an_undo_period_of_zero_days_makes_every_drop_due_at_once(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`LAKANA_TABLES__UNDO_DAYS=0`: the next sweep deletes the files; no undrop."""
    table_id = _orders_table(catalog, principal)

    _catalog(session, undo_days=0).drop_table(principal, _ref("gold.orders"))

    record = _record(session, table_id)
    assert record.purge_after is not None
    assert record.purge_after == record.dropped_at
    assert _events(session, AuditEventType.TABLE_DROPPED)[0].detail["reason"] == "user"
    with pytest.raises(ConflictError) as info:
        catalog.undrop_table(principal, _ref("gold.orders"))
    assert str(info.value) == PERIOD_PASSED_MESSAGE.format(
        table="gold.orders", purge_after=record.purge_after.isoformat(timespec="seconds")
    )


def test_an_undo_period_comes_from_the_catalog(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A longer period is honoured as given."""
    table_id = _orders_table(catalog, principal)

    _catalog(session, undo_days=30).drop_table(principal, _ref("gold.orders"))

    record = _record(session, table_id)
    assert record.purge_after is not None and record.dropped_at is not None
    assert record.purge_after - record.dropped_at == timedelta(days=30)


def test_an_external_table_and_a_view_leave_no_tombstone_but_are_audited(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """Their files are never Lakana's to delete: no record, `purge_after` null, files kept."""
    raw = store / "raw" / "events" / "a.parquet"
    raw.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), raw)
    catalog.create_external_table(
        principal,
        TableRef("dev", NamespacePath.from_parts(["bronze"]), "events"),
        location_pattern="raw/events/*.parquet",
        format="parquet",
    )
    catalog.create_or_replace_view(
        principal, _ref("gold.v"), sql="SELECT 1 AS x", references=[], columns=[]
    )
    before = set(session.scalars(select(TableLocation.id)))

    catalog.drop_table(principal, TableRef("dev", NamespacePath.from_parts(["bronze"]), "events"))
    catalog.drop_table(principal, _ref("gold.v"), purge=True)

    session.rollback()
    assert set(session.scalars(select(TableLocation.id))) == before
    assert raw.exists()
    events = _events(session, AuditEventType.TABLE_DROPPED)
    assert [(e.securable_table, e.detail["kind"], e.detail["purge_after"]) for e in events] == [
        ("events", "EXTERNAL", None),
        ("v", "VIEW", None),
    ]
    with pytest.raises(NotFoundError):
        catalog.undrop_table(
            principal, TableRef("dev", NamespacePath.from_parts(["bronze"]), "events")
        )


def test_a_table_without_a_record_keeps_its_files(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """A table whose location has no record of its own (an old shared one) is only uncatalogued."""
    table_id = _orders_table(catalog, principal)
    session.execute(
        update(TableLocation).where(TableLocation.table_id == table_id).values(table_id=None)
    )
    session.commit()
    files = sorted(p for p in store.rglob("*") if p.is_file())

    catalog.drop_table(principal, _ref("gold.orders"), purge=True)

    session.rollback()
    (record,) = session.scalars(select(TableLocation)).all()
    assert record.state is TableLocationState.LIVE
    assert sorted(p for p in store.rglob("*") if p.is_file()) == files
    assert _events(session, AuditEventType.TABLE_DROPPED)[0].detail["purge_after"] is None


def test_a_drop_of_a_table_sharing_a_dropped_location_never_shortens_it(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """An old table in a directory whose record is already DROPPED pushes its purge out."""
    table_id = _orders_table(catalog, principal)
    catalog.drop_table(principal, _ref("gold.orders"), purge=True)
    record = _record(session, table_id)
    twin_id = uuid.uuid4()
    gold = session.scalars(select(Namespace).where(Namespace.path == "gold")).one()
    session.add(
        Table(
            id=twin_id,
            namespace_id=gold.id,
            name="twin",
            table_type=TableType.ICEBERG,
            metadata_location=f"{record.location}/metadata/00009-z.metadata.json",
        )
    )
    session.commit()

    _catalog(session, undo_days=3).drop_table(principal, _ref("gold.twin"))

    later = _record(session, table_id)
    assert later.purge_after is not None and later.dropped_at is not None
    assert later.purge_after - later.dropped_at >= timedelta(days=3) - timedelta(seconds=2)
    assert _events(session, AuditEventType.TABLE_DROPPED)[-1].detail["purge_after"] is None


# --- undrop -----------------------------------------------------------------------------------


def test_undrop_restores_the_same_table_and_exactly_the_policies_its_drop_revoked(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Same id, metadata, owner, descriptions, columns; the three policies active again."""
    table_id = _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    catalog.set_table_description(principal, ref, "One row per order.")
    catalog.set_column_description(principal, ref, "region", "Where it was sold.")
    earlier = _revoked_policies(session, principal, ref)
    grant, row_filter, mask = _policies(session, principal, ref)
    before = catalog.load_table(principal, ref)
    row_before = _fresh(session, Table, table_id)
    snapshot_before = {
        column.key: getattr(row_before, column.key) for column in Table.__table__.columns
    }
    columns_before = sorted(
        (c.name, c.type, c.position)
        for c in session.scalars(select(TableColumn).where(TableColumn.table_id == table_id))
    )
    session.rollback()
    catalog.drop_table(principal, ref)

    restored = catalog.undrop_table(principal, ref)

    assert restored.uuid == table_id
    assert restored.metadata_location == before.metadata_location
    assert _rows(catalog, principal, "gold.orders") == [
        {"id": 1, "region": "eu"},
        {"id": 2, "region": "us"},
        {"id": 3, "region": "eu"},
    ]
    row = _fresh(session, Table, table_id)
    restored_columns = {column.key: getattr(row, column.key) for column in Table.__table__.columns}
    # The restore is the table's last activity (its retention counts from it), so only
    # `updated_at` moves on.
    assert restored_columns.pop("updated_at") >= snapshot_before.pop("updated_at")
    assert restored_columns == snapshot_before
    assert (
        sorted(
            (c.name, c.type, c.position)
            for c in session.scalars(select(TableColumn).where(TableColumn.table_id == table_id))
        )
        == columns_before
    )
    (described,) = session.scalars(
        select(ColumnDescription).where(ColumnDescription.table_id == table_id)
    ).all()
    assert (described.column, described.description_edited) == ("region", "Where it was sold.")
    for model, row_id in ((Grant, grant.id), (RowFilter, row_filter.id), (ColumnMask, mask.id)):
        back = _fresh(session, model, row_id)
        assert back.revoked_at is None
        assert back.revoked_by is None
        assert back.table_id == table_id
    still = _fresh(session, Grant, earlier.id)
    assert still.revoked_at is not None
    record = _record(session, table_id)
    assert record.state is TableLocationState.LIVE
    assert (record.dropped_at, record.purge_after, record.dropped_table) == (None, None, None)
    assert record.revoked_policy_ids == []
    (event,) = _events(session, AuditEventType.TABLE_UNDROPPED)
    assert event.principal_id == principal.id
    assert event.securable_table == "orders"
    assert event.detail["table_id"] == str(table_id)
    assert sorted(event.detail["restored_policies"], key=lambda e: e["kind"]) == [
        {"kind": "column_mask", "id": str(mask.id)},
        {"kind": "grant", "id": str(grant.id)},
        {"kind": "row_filter", "id": str(row_filter.id)},
    ]
    assert event.detail["name"] == "orders"


def test_a_policy_revoked_again_after_the_drop_stays_revoked(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Only a row exactly as the drop left it comes back; one stamped later stays revoked."""
    table_id = _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    grant, row_filter, _ = _policies(session, principal, ref)
    catalog.drop_table(principal, ref)
    record = _record(session, table_id)
    assert record.dropped_at is not None
    session.execute(
        update(Grant)
        .where(Grant.id == grant.id)
        .values(revoked_at=record.dropped_at + timedelta(minutes=5))
    )
    session.commit()

    catalog.undrop_table(principal, ref)

    assert _fresh(session, Grant, grant.id).revoked_at is not None
    assert _fresh(session, RowFilter, row_filter.id).revoked_at is None


def test_undrop_as_another_name_moves_the_policies_display_names_too(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`--as`: the table comes back under the new name, with the same id and policies."""
    table_id = _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    grant, row_filter, mask = _policies(session, principal, ref)
    catalog.drop_table(principal, ref)
    catalog.create_table(principal, ref, schema=_schema())

    restored = catalog.undrop_table(principal, ref, new_name="orders_before")

    assert restored.uuid == table_id
    assert restored.ref.name == "orders_before"
    for model, row_id in ((Grant, grant.id), (RowFilter, row_filter.id), (ColumnMask, mask.id)):
        back = _fresh(session, model, row_id)
        assert back.table == "orders_before"
        assert back.namespace == "gold"
        assert back.revoked_at is None
    assert _events(session, AuditEventType.TABLE_UNDROPPED)[0].detail["name"] == "orders_before"
    assert catalog.load_table(principal, ref).uuid != table_id


def _schema() -> Schema:
    """Build a one-column schema.

    Returns
    -------
    Schema
        ``id bigint``.
    """
    return Schema(NestedField(1, "id", LongType(), required=False))


def test_undrop_is_refused_when_the_name_is_taken(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The message names `--as`; nothing changes."""
    table_id = _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    catalog.drop_table(principal, ref)
    catalog.create_table(principal, ref, schema=_schema())

    with pytest.raises(AlreadyExistsError) as info:
        catalog.undrop_table(principal, ref)

    assert str(info.value) == NAME_TAKEN_MESSAGE.format(
        table="gold.orders", name="orders", namespace="gold", holder="a table"
    )
    assert "--as" in str(info.value)
    assert _record(session, table_id).state is TableLocationState.DROPPED


def test_undrop_is_refused_when_the_namespace_is_gone(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Dropped with its namespace: the message says to create the namespace again."""
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["gold", "eu"]))
    _write(catalog, principal, "gold.eu.orders", WriteMode.CREATE, _orders([1], ["eu"]))
    ref = _ref("gold.eu.orders")
    catalog.drop_table(principal, ref)
    catalog.drop_namespace(principal, "dev", NamespacePath.from_parts(["gold", "eu"]))

    with pytest.raises(NotFoundError) as info:
        catalog.undrop_table(principal, ref)

    assert str(info.value) == NAMESPACE_GONE_MESSAGE.format(
        table="gold.eu.orders", namespace="gold.eu"
    )


def test_undrop_into_a_namespace_created_again_works(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The namespace is found by its path, so creating it again is the way back."""
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["gold", "eu"]))
    result = _write(catalog, principal, "gold.eu.orders", WriteMode.CREATE, _orders([1], ["eu"]))
    ref = _ref("gold.eu.orders")
    catalog.drop_table(principal, ref)
    catalog.drop_namespace(principal, "dev", NamespacePath.from_parts(["gold", "eu"]))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["gold", "eu"]))

    assert catalog.undrop_table(principal, ref).uuid == result.table_id


def test_undrop_is_refused_after_the_undo_period(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`purge_after` has passed: the files are about to go, or are gone."""
    table_id = _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    catalog.drop_table(principal, ref)
    _expire(session, table_id)
    record = _record(session, table_id)
    assert record.purge_after is not None

    with pytest.raises(ConflictError) as info:
        catalog.undrop_table(principal, ref)

    assert str(info.value) == PERIOD_PASSED_MESSAGE.format(
        table="gold.orders", purge_after=record.purge_after.isoformat(timespec="seconds")
    )
    assert _record(session, table_id).state is TableLocationState.DROPPED


def test_undrop_is_refused_once_the_purge_has_started(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The sweep marked the record before its first delete: some files may be gone."""
    table_id = _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    catalog.drop_table(principal, ref)
    _expire(session, table_id, started=True)

    with pytest.raises(ConflictError) as info:
        catalog.undrop_table(principal, ref)

    assert str(info.value) == PURGE_STARTED_MESSAGE.format(table="gold.orders")


def test_undrop_of_a_name_without_a_tombstone_is_not_found(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The message points at the listing of dropped tables; another name's tombstone is no match."""
    _orders_table(catalog, principal)
    catalog.drop_table(principal, _ref("gold.orders"))

    with pytest.raises(NotFoundError) as info:
        catalog.undrop_table(principal, _ref("gold.nothing"))

    assert str(info.value) == NO_TOMBSTONE_MESSAGE.format(name="nothing", namespace="gold")


def test_undrop_takes_the_newest_tombstone_of_a_name(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Dropped twice: the newest comes back first, the older one under another name."""
    first = _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    catalog.drop_table(principal, ref)
    second = _write(
        catalog, principal, "gold.orders", WriteMode.CREATE, _orders([9], ["us"])
    ).table_id
    catalog.drop_table(principal, ref)
    session.execute(
        update(TableLocation)
        .where(TableLocation.table_id == first)
        .values(dropped_at=_dropped_at(session, second) - timedelta(minutes=1))
    )
    session.commit()

    assert catalog.undrop_table(principal, ref).uuid == second
    assert catalog.undrop_table(principal, ref, new_name="orders_old").uuid == first


def test_undrop_refuses_a_bad_new_name(catalog: CatalogService, principal: PrincipalRef) -> None:
    """The new name is checked like a create's."""
    _orders_table(catalog, principal)
    catalog.drop_table(principal, _ref("gold.orders"))

    with pytest.raises(Exception, match="must not contain"):
        catalog.undrop_table(principal, _ref("gold.orders"), new_name="a.b")


def test_an_owner_deleted_meanwhile_leaves_the_table_without_one(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """As the foreign key would have done had the row stayed."""
    owner = _principal(session, "owner")
    result = _write(catalog, owner, "gold.orders", WriteMode.CREATE, _orders([1], ["eu"]))
    catalog.set_column_description(owner, _ref("gold.orders"), "region", "Where.")
    catalog.drop_table(principal, _ref("gold.orders"))
    session.execute(
        update(ColumnDescription)
        .values(updated_by=None)
        .execution_options(synchronize_session=False)
    )
    session.delete(session.get(Principal, owner.id))
    session.commit()

    catalog.undrop_table(principal, _ref("gold.orders"))

    assert _fresh(session, Table, result.table_id).owner_id is None


def test_undrop_needs_drop_and_create_on_the_namespace(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Either right alone is refused, before anything is looked up."""
    _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    catalog.drop_table(principal, ref)

    for allowed in ((Action.DROP_TABLE,), (Action.CREATE_TABLE,)):
        authz = ActionAllowList(*allowed)
        with pytest.raises(PermissionDenied):
            _catalog(session, authz).undrop_table(principal, ref)
    both = ActionAllowList(Action.DROP_TABLE, Action.CREATE_TABLE)
    _catalog(session, both).undrop_table(principal, ref, new_name="back")
    securables = [(action, securable.kind, securable.table) for action, securable in both.seen]
    assert securables[:2] == [
        (Action.DROP_TABLE, SecurableKind.NAMESPACE, None),
        (Action.CREATE_TABLE, SecurableKind.TABLE, "back"),
    ]


def test_undrop_in_a_protected_environment_is_refused_like_a_create(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The protected-environment guard refuses it; a break-glass reason lets it through."""
    _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    catalog.drop_table(principal, ref)
    catalog.set_environment_protection(principal, "dev", True)

    with pytest.raises(PermissionDenied) as info:
        catalog.undrop_table(principal, ref)

    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")
    (denied,) = _events(session, AuditEventType.PROTECTED_WRITE_DENIED)
    assert denied.detail["operation"] == "create_table"
    glass = dataclasses.replace(principal, break_glass_reason="incident 7: restore a dropped table")
    catalog.undrop_table(glass, ref)
    (event,) = _events(session, AuditEventType.BREAK_GLASS_WRITE)
    assert event.detail["target"] == "gold.orders"


def test_the_namespace_dropped_while_an_undrop_runs_is_a_conflict(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """An insert that fails on an integrity error that is no name clash says to try again."""
    _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    catalog.drop_table(principal, ref)

    def fail() -> None:
        raise IntegrityError("INSERT", {}, Exception("FOREIGN KEY constraint failed"))

    with (
        mock.patch.object(catalog._guard, "record_pending", fail),
        pytest.raises(ConflictError, match="try again"),
    ):
        catalog.undrop_table(principal, ref)


def test_a_name_taken_at_the_same_moment_is_reported_as_taken(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The unique constraint arbitrates a create that lands between the check and the insert."""
    table_id = _orders_table(catalog, principal)
    ref = _ref("gold.orders")
    catalog.drop_table(principal, ref)
    catalog.create_table(principal, ref, schema=_schema())

    with (
        mock.patch.object(catalog, "_lookup_table", return_value=None),
        pytest.raises(AlreadyExistsError, match="--as"),
    ):
        catalog.undrop_table(principal, ref)

    assert _record(session, table_id).state is TableLocationState.DROPPED


# --- the listing ------------------------------------------------------------------------------


def test_the_dropped_tables_of_a_namespace_are_listed_with_their_purge_date(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Newest first; whether each can still be restored; only this namespace's."""
    first = _orders_table(catalog, principal)
    catalog.drop_table(principal, _ref("gold.orders"))
    second = _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    catalog.drop_table(principal, _ref("gold.daily"), purge=True)
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["silver", "x"]))
    _write(catalog, principal, "silver.x.other", WriteMode.CREATE, _orders([1], ["eu"]))
    catalog.drop_table(principal, _ref("silver.x.other"))
    session.execute(
        update(TableLocation)
        .where(TableLocation.table_id == first)
        .values(dropped_at=_dropped_at(session, second.table_id) - timedelta(minutes=1))
    )
    session.commit()

    listed = catalog.list_dropped_tables(principal, "dev", _GOLD)

    assert [(d.name, d.table_id, d.restorable) for d in listed] == [
        ("daily", second.table_id, False),
        ("orders", first, True),
    ]
    record = _record(session, first)
    assert (listed[1].dropped_at, listed[1].purge_after) == (record.dropped_at, record.purge_after)
    assert listed[1].kind == "TABLE"


def test_listing_dropped_tables_needs_read_on_the_namespace(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The rule of the table list: `READ_TABLE` on the namespace, and it must exist."""
    authz = ActionAllowList()
    with pytest.raises(PermissionDenied):
        _catalog(session, authz).list_dropped_tables(principal, "dev", _GOLD)
    assert [(a, s.kind) for a, s in authz.seen] == [(Action.READ_TABLE, SecurableKind.NAMESPACE)]
    with pytest.raises(NotFoundError):
        catalog.list_dropped_tables(principal, "dev", NamespacePath.from_parts(["nope"]))


class _HidesOneName(AllowAllAuthorization):
    """Allows everything, but would refuse to read one table name (as a narrowed token does)."""

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Refuse `READ_TABLE` on a table named `daily`.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            `False` only for that one table.
        """
        return not (action is Action.READ_TABLE and securable.table == "daily")


def test_the_listing_leaves_out_a_dropped_table_the_caller_could_not_read(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """As the table list does: only what the caller may read by its name is named."""
    _orders_table(catalog, principal)
    catalog.drop_table(principal, _ref("gold.orders"))
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    catalog.drop_table(principal, _ref("gold.daily"))

    listed = _catalog(session, _HidesOneName()).list_dropped_tables(principal, "dev", _GOLD)

    assert [d.name for d in listed] == ["orders"]


def test_a_table_whose_location_has_no_record_at_all_keeps_its_files(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """No record at its root (a restored database, a table from before records): files stay."""
    table_id = _orders_table(catalog, principal)
    session.execute(sa_delete(TableLocation).where(TableLocation.table_id == table_id))
    session.commit()
    files = sorted(p for p in store.rglob("*") if p.is_file())

    catalog.drop_table(principal, _ref("gold.orders"))

    session.rollback()
    assert session.scalars(select(TableLocation)).all() == []
    assert sorted(p for p in store.rglob("*") if p.is_file()) == files
    assert _events(session, AuditEventType.TABLE_DROPPED)[0].detail["purge_after"] is None


def test_a_dropped_record_without_a_snapshot_is_neither_listed_nor_restored(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """A DROPPED record no drop of this release wrote has nothing to restore from."""
    session.add(
        TableLocation(
            id=uuid.uuid4(),
            environment_id=session.scalars(select(Namespace.environment_id)).first(),
            location=f"file://{store}/managed/gold/orders-{uuid.uuid4().hex}",
            state=TableLocationState.DROPPED,
            claimed_at=datetime.now(UTC),
            dropped_at=datetime.now(UTC),
            purge_after=datetime.now(UTC) + timedelta(days=7),
            revoked_policy_ids=[],
        )
    )
    session.commit()

    assert catalog.list_dropped_tables(principal, "dev", _GOLD) == []
    with pytest.raises(NotFoundError):
        catalog.undrop_table(principal, _ref("gold.orders"))


def test_a_table_without_an_owner_or_descriptions_comes_back_without_them(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Its owner was deleted before the drop; nothing names a principal to look up."""
    owner = _principal(session, "owner")
    result = _write(catalog, owner, "gold.orders", WriteMode.CREATE, _orders([1], ["eu"]))
    session.delete(session.get(Principal, owner.id))
    session.commit()
    catalog.drop_table(principal, _ref("gold.orders"))

    catalog.undrop_table(principal, _ref("gold.orders"))

    assert _fresh(session, Table, result.table_id).owner_id is None


def test_an_entry_of_an_unknown_kind_in_a_tombstone_is_ignored(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Only the three policy models are restored; anything else in the list changes nothing."""
    table_id = _orders_table(catalog, principal)
    grant, _, _ = _policies(session, principal, _ref("gold.orders"))
    catalog.drop_table(principal, _ref("gold.orders"))
    record = _record(session, table_id)
    session.execute(
        update(TableLocation)
        .where(TableLocation.id == record.id)
        .values(
            revoked_policy_ids=[
                *record.revoked_policy_ids,
                {"kind": "secret", "id": str(uuid.uuid4())},
            ]
        )
    )
    session.commit()

    catalog.undrop_table(principal, _ref("gold.orders"))

    assert _fresh(session, Grant, grant.id).revoked_at is None
    (event,) = _events(session, AuditEventType.TABLE_UNDROPPED)
    assert {entry["kind"] for entry in event.detail["restored_policies"]} == {
        "grant",
        "row_filter",
        "column_mask",
    }


def test_a_table_dropped_at_the_same_moment_is_not_found(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The row lock finds no row: the other drop won, and this one says the table is gone."""
    with pytest.raises(NotFoundError, match="not found"):
        catalog._lock_table_row(_ref("gold.orders"), uuid.uuid4())


def test_an_undrop_whose_metadata_file_is_gone_is_refused_and_restores_nothing(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Never a row that points at nothing: the files must still be there."""
    table_id = _orders_table(catalog, principal)
    metadata_location = catalog.load_table(principal, _ref("gold.orders")).metadata_location
    catalog.drop_table(principal, _ref("gold.orders"))
    Path(metadata_location.removeprefix("file://")).unlink()

    with pytest.raises(ConflictError) as info:
        catalog.undrop_table(principal, _ref("gold.orders"))

    assert str(info.value) == FILES_GONE_MESSAGE.format(table="gold.orders")
    session.rollback()
    assert session.get(Table, table_id) is None
    assert _record(session, table_id).state is TableLocationState.DROPPED


_MODEL_ENTRY = {
    "write": "replace",
    "key": ["id"],
    "model": {
        "kind": "fact",
        "grain": "One row per order.",
        "references": [{"column": "region", "target": "gold.regions", "target_column": "code"}],
    },
}


def test_undrop_restores_the_model_and_its_references(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The model columns come back with the row, and the reference rows with them."""
    table_id = _orders_table(catalog, principal)
    write_model_entry(session, _fresh(session, Table, table_id), _MODEL_ENTRY)
    session.commit()
    environment_id = _fresh(
        session, Namespace, _fresh(session, Table, table_id).namespace_id
    ).environment_id
    before = read_table_model(session, _fresh(session, Table, table_id))
    assert before is not None

    catalog.drop_table(principal, _ref("gold.orders"))
    assert incoming_references(session, environment_id, "gold.regions") == []
    catalog.undrop_table(principal, _ref("gold.orders"))

    assert read_table_model(session, _fresh(session, Table, table_id)) == before
    assert incoming_references(session, environment_id, "gold.regions") == [
        (table_id, "region", "code")
    ]


def test_undrop_of_a_tombstone_from_before_references_restores_the_model_without_them(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A tombstone written before this release has no `references` key; the row comes back.

    Its model columns come back with it, and no reference row is made up.
    """
    table_id = _orders_table(catalog, principal)
    write_model_entry(session, _fresh(session, Table, table_id), _MODEL_ENTRY)
    session.commit()
    environment_id = _fresh(
        session, Namespace, _fresh(session, Table, table_id).namespace_id
    ).environment_id
    catalog.drop_table(principal, _ref("gold.orders"))
    record = _record(session, table_id)
    snapshot = dict(record.dropped_table or {})
    snapshot.pop("references", None)
    record.dropped_table = snapshot
    session.commit()

    catalog.undrop_table(principal, _ref("gold.orders"))

    model = read_table_model(session, _fresh(session, Table, table_id))
    assert model is not None
    assert (model.kind, model.key, model.references) == ("fact", ("id",), ())
    assert incoming_references(session, environment_id, "gold.regions") == []
