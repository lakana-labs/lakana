"""Deleting an environment leaves tombstones for an undo period; `restore_environment` undoes it.

A deletion drops every managed table of the environment through the drop path (`reason:
environment_deleted`), removes its views and external tables' rows, revokes what is bound to
the environment, and marks the row deleted with `purge_after`. The row stays, hidden from every
lookup by name, until the location sweep has purged the last tombstone; then the sweep deletes
it. These tests run on a SQLite file.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Generator, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import ActionAllowList, FakeColumnCatalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import Select, event, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.sql import ClauseElement
from test_table_writes import _orders, _ref, _write

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    SecurableKind,
)
from lakana_core.catalog import environment_deletion
from lakana_core.catalog.environment_deletion import (
    DELETED_HINT_MESSAGE,
    ENVIRONMENT_GONE_MESSAGE,
    NAME_HELD_MESSAGE,
    NOT_DELETED_MESSAGE,
    RESTORE_FILES_GONE_MESSAGE,
    RESTORE_PERIOD_PASSED_MESSAGE,
    RESTORE_PURGE_STARTED_MESSAGE,
)
from lakana_core.catalog.location_sweep import LocationSweepReport, sweep_table_locations
from lakana_core.catalog.locations import claim_location
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
)
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    Environment,
    Grant,
    Namespace,
    Principal,
    PrincipalType,
    RowFilter,
    Table,
    TableLocation,
    TableLocationState,
    TableType,
    WriteMode,
)
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.projects.lookups import live_environment_names
from lakana_core.storage.resolver import StorageResolver

_BRONZE = NamespacePath.from_parts(["bronze"])


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
    _catalog(session).create_environment(principal, name="dev", storage=_storage(store))
    return _catalog(session)


def _storage(root: Path) -> StorageConfig:
    """Describe a local storage root.

    Parameters
    ----------
    root
        The directory.

    Returns
    -------
    StorageConfig
        A ``file://`` root.
    """
    return StorageConfig(scheme="file", root=f"file://{root}", options={})


def _catalog(session: Session, authz: object | None = None, **options: int) -> CatalogService:
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


def _user(session: Session, name: str) -> PrincipalRef:
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


def _table(catalog: CatalogService, principal: PrincipalRef, dotted: str) -> uuid.UUID:
    """Create a managed table with three rows.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The creator.
    dotted
        Its name in `dev`.

    Returns
    -------
    uuid.UUID
        The table's id.
    """
    data = _orders([1, 2, 3], ["eu", "us", "eu"])
    return _write(catalog, principal, dotted, WriteMode.CREATE, data).table_id


def _env(session: Session, name: str = "dev") -> Environment:
    """Read an environment row fresh, deleted or not.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    Environment
        The row.
    """
    session.rollback()
    found = session.scalars(
        select(Environment)
        .where(Environment.name == name)
        .execution_options(populate_existing=True)
    ).one()
    session.rollback()
    return found


def _record(session: Session, table_id: uuid.UUID) -> TableLocation | None:
    """Read a table's location record fresh.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.

    Returns
    -------
    TableLocation or None
        The record; `None` once it is gone.
    """
    session.rollback()
    found = session.scalars(
        select(TableLocation)
        .where(TableLocation.table_id == table_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
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


def _files(root: Path) -> list[Path]:
    """List every file under a directory.

    Parameters
    ----------
    root
        The directory.

    Returns
    -------
    list of Path
        Sorted.
    """
    return sorted(p for p in root.rglob("*") if p.is_file())


def _sweep(session: Session, now: datetime) -> LocationSweepReport:
    """Run the location sweep once.

    Parameters
    ----------
    session
        Database session.
    now
        The sweep's time.

    Returns
    -------
    LocationSweepReport
        What it did.
    """
    return sweep_table_locations(
        session,
        StorageResolver(session),
        now=now,
        grace_hours=24,
    )


def _purge_after(session: Session, name: str = "dev") -> datetime:
    """Read a deleted environment's `purge_after`.

    Parameters
    ----------
    session
        Database session.
    name
        The environment.

    Returns
    -------
    datetime
        The time.
    """
    found = _env(session, name).purge_after
    assert found is not None
    return found


def _grants(session: Session, admin: PrincipalRef, ref: TableRef) -> tuple[Grant, Grant, RowFilter]:
    """Give `alice` a table grant, an environment grant and a row filter on `ref`.

    Parameters
    ----------
    session
        Database session.
    admin
        Who creates them.
    ref
        The table.

    Returns
    -------
    tuple of (Grant, Grant, RowFilter)
        The table grant, the environment grant and the row filter.
    """
    if session.scalar(select(Principal).where(Principal.name == "alice")) is None:
        _user(session, "alice")
    allow = AllowAllAuthorization()
    grants = GrantService(session, allow)
    on_table = grants.grant(
        admin,
        principal="alice",
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment=ref.environment,
        namespace=ref.namespace.joined(),
        table=ref.name,
    )
    on_environment = grants.grant(
        admin,
        principal="alice",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    row_filter = RowFilterService(session, allow, FakeColumnCatalog()).create(
        admin,
        principal="alice",
        environment=ref.environment,
        namespace=ref.namespace.joined(),
        table=ref.name,
        expression="region = 'eu'",
    )
    return on_table, on_environment, row_filter


def _revoked_at(
    session: Session, model: type[Grant] | type[RowFilter], row_id: uuid.UUID
) -> object:
    """Read when a grant or policy was revoked.

    Parameters
    ----------
    session
        Database session.
    model
        `Grant` or `RowFilter`.
    row_id
        Its id.

    Returns
    -------
    datetime or None
        `revoked_at`.
    """
    session.rollback()
    revoked = session.scalar(select(model.revoked_at).where(model.id == row_id))
    session.rollback()
    return revoked


# --- the deletion -----------------------------------------------------------------------------


def test_a_deletion_tombstones_every_managed_table_and_keeps_the_files(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """DROPPED with the undo period, `reason: environment_deleted`, audited, files untouched."""
    orders = _table(catalog, principal, "gold.orders")
    customers = _table(catalog, principal, "silver.customers")
    files = _files(store)

    deleted = catalog.delete_environment(principal, "dev")

    env = _env(session)
    assert env.deleted_at is not None and env.purge_after is not None
    assert env.deleted_by == principal.id
    assert env.purge_after - env.deleted_at == timedelta(days=7)
    assert deleted.purge_after == env.purge_after
    for table_id in (orders, customers):
        record = _record(session, table_id)
        assert record is not None and record.state is TableLocationState.DROPPED
        assert (record.dropped_at, record.purge_after) == (env.deleted_at, env.purge_after)
    records = [_record(session, t) for t in (orders, customers)]
    assert sorted(env.dropped_location_ids or []) == sorted(str(r.id) for r in records if r)
    assert _files(store) == files
    session.rollback()
    assert session.scalars(select(Table)).all() == []
    dropped = _events(session, AuditEventType.TABLE_DROPPED)
    assert sorted(str(e.securable_table) for e in dropped) == ["customers", "orders"]
    assert {e.detail["reason"] for e in dropped} == {"environment_deleted"}
    assert {e.principal_id for e in dropped} == {principal.id}
    (event,) = _events(session, AuditEventType.ENVIRONMENT_DELETED)
    assert event.detail == {
        "tables": 2,
        "tables_without_tombstone": 0,
        "purge_after": env.purge_after.isoformat(),
        "purge": False,
    }


def test_a_deletion_revokes_the_tables_policies_into_their_tombstones(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """As a drop does: each table's own policies go on its tombstone, the rest on the row."""
    table_id = _table(catalog, principal, "gold.orders")
    on_table, on_environment, row_filter = _grants(session, principal, _ref("gold.orders"))

    catalog.delete_environment(principal, "dev")

    env = _env(session)
    record = _record(session, table_id)
    assert record is not None
    assert {(e["kind"], e["id"]) for e in record.revoked_policy_ids} == {
        ("grant", str(on_table.id)),
        ("row_filter", str(row_filter.id)),
    }
    assert env.revoked_policy_ids == [{"kind": "grant", "id": str(on_environment.id)}]
    for model, row_id in ((Grant, on_table.id), (Grant, on_environment.id)):
        assert _revoked_at(session, model, row_id) == env.deleted_at
    assert _revoked_at(session, RowFilter, row_filter.id) == env.deleted_at


def test_views_and_external_tables_are_removed_and_external_files_kept(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """Their rows go, audited; an external table's files were never Lakana's to delete."""
    raw = store / "raw" / "events" / "a.parquet"
    raw.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), raw)
    catalog.create_external_table(
        principal,
        TableRef("dev", _BRONZE, "events"),
        location_pattern="raw/events/*.parquet",
        format="parquet",
    )
    catalog.create_or_replace_view(
        principal, _ref("gold.v"), sql="SELECT 1 AS x", references=[], columns=[]
    )

    catalog.delete_environment(principal, "dev")

    session.rollback()
    assert session.scalars(select(Table)).all() == []
    assert raw.exists()
    kinds = {
        (e.securable_table, e.detail["kind"])
        for e in _events(session, AuditEventType.TABLE_DROPPED)
    }
    assert kinds == {("events", "EXTERNAL"), ("v", "VIEW")}
    assert _env(session).dropped_location_ids == []
    assert _sweep(session, _purge_after(session) + timedelta(days=1)).files_deleted == 0
    assert raw.exists()


def test_an_artifact_stays_with_the_deleted_environment(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Its row is kept, hidden with the environment; a restore brings it back unchanged."""
    namespace = session.scalars(select(Namespace).where(Namespace.path == "gold")).one()
    artifact_id = uuid.uuid4()
    session.add(
        Table(
            id=artifact_id,
            namespace_id=namespace.id,
            name="model",
            table_type=TableType.ARTIFACT,
            owner_id=principal.id,
        )
    )
    session.commit()

    catalog.delete_environment(principal, "dev")
    session.rollback()
    assert session.get(Table, artifact_id) is not None

    catalog.restore_environment(principal, "dev")
    session.rollback()
    assert session.get(Table, artifact_id) is not None


def test_a_deleted_environment_is_hidden_and_answers_like_a_missing_one(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Not listed, not found by name; the message names the restore for who may make it."""
    _table(catalog, principal, "gold.orders")
    catalog.delete_environment(principal, "dev")
    purge_after = _purge_after(session)

    with pytest.raises(NotFoundError) as info:
        catalog.get_environment(principal, "dev")
    assert str(info.value) == DELETED_HINT_MESSAGE.format(
        name="dev", purge_after=purge_after.isoformat(timespec="seconds")
    )
    assert "lakana env restore dev" in str(info.value)
    assert catalog.list_environments(principal) == []
    with pytest.raises(NotFoundError):
        catalog.load_table(principal, _ref("gold.orders"))
    with pytest.raises(NotFoundError):
        catalog.list_tables(principal, "dev", NamespacePath.from_parts(["gold"]))
    with pytest.raises(NotFoundError):
        catalog.list_namespaces(principal, "dev")
    with pytest.raises(NotFoundError):
        catalog.undrop_table(principal, _ref("gold.orders"))
    with pytest.raises(NotFoundError):
        catalog.update_environment_storage(principal, "dev", options={"s3.endpoint": "elsewhere"})
    with pytest.raises(NotFoundError):
        catalog.set_environment_protection(principal, "dev", True)
    with pytest.raises(NotFoundError):
        catalog.set_environment_retention(principal, "dev", 1)
    with pytest.raises(NotFoundError, match="No environment named 'dev'"):
        StorageResolver(session).for_environment("dev")
    assert StorageResolver(session).for_environment("dev", include_deleted=True)
    reader = _catalog(session, ActionAllowList(Action.READ_ENVIRONMENT))
    with pytest.raises(NotFoundError) as plain:
        reader.get_environment(principal, "dev")
    assert str(plain.value) == "No environment named 'dev'."


def test_the_deleted_environments_are_listed_with_their_purge_date(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`list_deleted_environments` shows them; one the caller may not read is left out."""
    catalog.delete_environment(principal, "dev")

    (listed,) = catalog.list_deleted_environments(principal)
    assert (listed.name, listed.purge_after) == ("dev", _purge_after(session))
    assert _catalog(session, ActionAllowList()).list_deleted_environments(principal) == []


def test_the_name_of_a_deleted_environment_is_not_free(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """A create of the name is refused, naming the restore and the early purge."""
    catalog.delete_environment(principal, "dev")

    with pytest.raises(AlreadyExistsError) as info:
        catalog.create_environment(principal, name="dev", storage=_storage(store))

    assert str(info.value) == NAME_HELD_MESSAGE.format(
        name="dev",
        purge_after=_purge_after(session).isoformat(timespec="seconds"),
    )
    assert "lakana env restore dev" in str(info.value)
    assert "lakana env delete dev --purge" in str(info.value)


def test_a_deletion_needs_manage_environment_and_keeps_its_refusals(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Without the right nothing changes; deleting a deleted one again is not found."""
    table_id = _table(catalog, principal, "gold.orders")
    with pytest.raises(PermissionDenied):
        _catalog(session, ActionAllowList(Action.READ_ENVIRONMENT)).delete_environment(
            principal, "dev"
        )
    assert _env(session).deleted_at is None
    record = _record(session, table_id)
    assert record is not None and record.state is TableLocationState.LIVE

    catalog.delete_environment(principal, "dev")
    with pytest.raises(NotFoundError, match="lakana env restore dev"):
        catalog.delete_environment(principal, "dev")


# --- races with a create and an undrop --------------------------------------------------------


def test_a_create_whose_claim_came_before_the_deletion_is_not_published(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """The deletion abandons the claim under its lock; the publish finds the environment gone."""
    ref = _ref("gold.late")
    prepared = catalog._prepare_table(
        ref,
        schema=Schema(NestedField(1, "id", LongType(), required=False)),
        partition_spec=None,
        sort_order=None,
        properties=None,
        location=None,
        holder_run_id=None,
    )
    catalog.delete_environment(principal, "dev")
    claim = session.scalars(
        select(TableLocation).where(TableLocation.location == prepared.location)
    ).one()
    assert claim.state is TableLocationState.ABANDONED
    assert claim.purge_after == _purge_after(session)
    session.rollback()

    with pytest.raises(NotFoundError) as info:
        catalog._publish_table(prepared, prepared.metadata, owner_id=principal.id)

    assert str(info.value) == ENVIRONMENT_GONE_MESSAGE.format(table="gold.late", environment="dev")
    session.rollback()
    assert session.get(Table, prepared.table_id) is None


def test_a_claim_made_after_the_deletion_is_never_published(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A create that found the environment before the deletion, and claims after it, is fenced."""
    ref = _ref("gold.later")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("lakana_core.catalog.service.claim_location", lambda *a, **k: None)
        prepared = catalog._prepare_table(
            ref,
            schema=Schema(NestedField(1, "id", LongType(), required=False)),
            partition_spec=None,
            sort_order=None,
            properties=None,
            location=None,
            holder_run_id=None,
        )
    catalog.delete_environment(principal, "dev")
    claim_location(
        session,
        environment_id=prepared.environment_id,
        location=prepared.location,
        holder_run_id=None,
    )

    with pytest.raises(NotFoundError):
        catalog._publish_table(prepared, prepared.metadata, owner_id=principal.id)

    session.rollback()
    assert session.get(Table, prepared.table_id) is None
    claim = session.scalars(
        select(TableLocation).where(TableLocation.location == prepared.location)
    ).one()
    assert claim.state is TableLocationState.CLAIMED
    session.rollback()
    report = _sweep(session, _purge_after(session) + timedelta(days=2))
    assert report.claims_abandoned == 1 and report.claims_published == 0


def test_a_claim_in_a_deleted_environment_is_never_made_live(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """Even when a row names its location, the claims step abandons it in a deleted environment."""
    env_id = _env(session).id
    location = f"file://{store}/managed/gold/ghost-{uuid.uuid4().hex}"
    claim_location(session, environment_id=env_id, location=location, holder_run_id=None)
    catalog.delete_environment(principal, "dev")
    session.execute(
        update(TableLocation)
        .where(TableLocation.location == location)
        .values(state=TableLocationState.CLAIMED)
    )
    namespace = session.scalars(select(Namespace).where(Namespace.path == "gold")).one()
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=namespace.id,
            name="ghost",
            table_type=TableType.ICEBERG,
            metadata_location=f"{location}/metadata/00000-x.metadata.json",
        )
    )
    session.commit()

    report = _sweep(session, _purge_after(session) - timedelta(days=6))

    assert (report.claims_published, report.claims_abandoned) == (0, 1)
    state = session.scalar(select(TableLocation.state).where(TableLocation.location == location))
    assert state is TableLocationState.ABANDONED


# --- the restore ------------------------------------------------------------------------------


def test_a_restore_brings_back_exactly_this_deletions_tables_and_policies(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Same ids and metadata, the policies the deletion revoked; an earlier drop stays dropped."""
    orders = _table(catalog, principal, "gold.orders")
    earlier = _table(catalog, principal, "gold.earlier")
    on_table, on_environment, row_filter = _grants(session, principal, _ref("gold.orders"))
    metadata = catalog.load_table(principal, _ref("gold.orders")).metadata_location
    catalog.drop_table(principal, _ref("gold.earlier"))
    catalog.delete_environment(principal, "dev")

    restored = catalog.restore_environment(principal, "dev")

    assert restored.deleted_at is None and restored.purge_after is None
    env = _env(session)
    assert (env.deleted_at, env.deleted_by, env.purge_after) == (None, None, None)
    assert (env.dropped_location_ids, env.revoked_policy_ids) == (None, None)
    loaded = catalog.load_table(principal, _ref("gold.orders"))
    assert loaded.metadata_location == metadata
    session.rollback()
    assert session.scalars(select(Table.id).where(Table.name == "orders")).one() == orders
    record = _record(session, orders)
    assert record is not None and record.state is TableLocationState.LIVE
    for model, row_id in ((Grant, on_table.id), (Grant, on_environment.id)):
        assert _revoked_at(session, model, row_id) is None
    assert _revoked_at(session, RowFilter, row_filter.id) is None
    still = _record(session, earlier)
    assert still is not None and still.state is TableLocationState.DROPPED
    (undropped,) = _events(session, AuditEventType.TABLE_UNDROPPED)
    assert undropped.detail["reason"] == "environment_restored"
    (event,) = _events(session, AuditEventType.ENVIRONMENT_RESTORED)
    assert event.principal_id == principal.id
    assert event.detail["tables"] == 1
    assert event.detail["restored_policies"] == 3
    catalog.undrop_table(principal, _ref("gold.earlier"))
    assert catalog.load_table(principal, _ref("gold.earlier"))


def test_a_restore_needs_manage_environment(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The right a deletion needs; without it nothing comes back."""
    catalog.delete_environment(principal, "dev")
    authz = ActionAllowList(Action.READ_ENVIRONMENT)

    with pytest.raises(PermissionDenied):
        _catalog(session, authz).restore_environment(principal, "dev")

    assert authz.seen[0][0] is Action.MANAGE_ENVIRONMENT
    assert _env(session).deleted_at is not None


def test_a_restore_of_an_environment_that_is_not_deleted_is_refused(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """There is nothing to restore; an unknown name is not found."""
    with pytest.raises(ConflictError) as info:
        catalog.restore_environment(principal, "dev")
    assert str(info.value) == NOT_DELETED_MESSAGE.format(name="dev")
    with pytest.raises(NotFoundError, match="lakana env list --deleted"):
        catalog.restore_environment(principal, "nope")


def test_a_restore_after_the_undo_period_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`purge_after` passed: the sweep may delete the files at any moment."""
    _table(catalog, principal, "gold.orders")
    catalog.delete_environment(principal, "dev")
    past = _env(session).deleted_at
    assert past is not None
    session.execute(update(Environment).where(Environment.name == "dev").values(purge_after=past))
    session.commit()

    with pytest.raises(ConflictError) as info:
        catalog.restore_environment(principal, "dev")

    assert str(info.value) == RESTORE_PERIOD_PASSED_MESSAGE.format(
        name="dev", purge_after=past.isoformat(timespec="seconds")
    )
    assert _env(session).deleted_at is not None


def test_a_restore_after_a_purge_started_is_refused_and_restores_nothing(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """One table already being deleted: the environment cannot come back whole."""
    first = _table(catalog, principal, "gold.orders")
    second = _table(catalog, principal, "gold.second")
    catalog.delete_environment(principal, "dev")
    record = _record(session, second)
    assert record is not None
    session.execute(
        update(TableLocation)
        .where(TableLocation.id == record.id)
        .values(purge_started_at=record.dropped_at)
    )
    session.commit()

    with pytest.raises(ConflictError) as info:
        catalog.restore_environment(principal, "dev")

    assert str(info.value) == RESTORE_PURGE_STARTED_MESSAGE.format(name="dev")
    assert _env(session).deleted_at is not None
    kept = _record(session, first)
    assert kept is not None and kept.state is TableLocationState.DROPPED
    session.rollback()
    assert session.scalars(select(Table)).all() == []


def test_a_restore_whose_record_is_gone_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A record the sweep already removed counts as a purge that started."""
    table_id = _table(catalog, principal, "gold.orders")
    catalog.delete_environment(principal, "dev")
    record = _record(session, table_id)
    assert record is not None
    session.delete(session.get(TableLocation, record.id))
    session.commit()

    with pytest.raises(ConflictError, match="has started deleting"):
        catalog.restore_environment(principal, "dev")


def test_a_restore_whose_metadata_file_is_gone_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Never a row that points at nothing."""
    _table(catalog, principal, "gold.orders")
    metadata = catalog.load_table(principal, _ref("gold.orders")).metadata_location
    catalog.delete_environment(principal, "dev")
    Path(metadata.removeprefix("file://")).unlink()

    with pytest.raises(ConflictError) as info:
        catalog.restore_environment(principal, "dev")

    assert str(info.value) == RESTORE_FILES_GONE_MESSAGE.format(name="dev", table="gold.orders")
    assert _env(session).deleted_at is not None


# --- the purge --------------------------------------------------------------------------------


def test_the_sweep_purges_after_the_undo_period_then_removes_the_row(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """Nothing before `purge_after`; then the files, the records and last the row, audited."""
    table_id = _table(catalog, principal, "gold.orders")
    catalog.delete_environment(principal, "dev")
    purge_after = _purge_after(session)

    early = _sweep(session, purge_after - timedelta(seconds=1))

    assert (early.files_deleted, early.environments_purged) == (0, 0)
    assert _record(session, table_id) is not None

    late = _sweep(session, purge_after)

    assert (late.locations_purged, late.environments_purged) == (1, 1)
    assert _record(session, table_id) is None
    assert not list((store / "managed").rglob("*.parquet"))
    session.rollback()
    assert session.scalars(select(Environment)).all() == []
    assert session.scalars(select(Namespace)).all() == []
    (event,) = _events(session, AuditEventType.ENVIRONMENT_PURGED)
    assert event.securable_environment == "dev"
    assert event.principal_id is None
    catalog.create_environment(principal, name="dev", storage=_storage(store))


def test_the_row_stays_until_the_last_record_is_gone(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """A table dropped before the deletion with a longer undo period keeps the row."""
    _catalog(session).create_environment(principal, name="dev", storage=_storage(store))
    long_undo = _catalog(session, undo_days=30)
    earlier = _table(long_undo, principal, "gold.earlier")
    _table(long_undo, principal, "gold.orders")
    long_undo.drop_table(principal, _ref("gold.earlier"))
    _catalog(session).delete_environment(principal, "dev")
    purge_after = _purge_after(session)

    first = _sweep(session, purge_after)

    assert (first.locations_purged, first.environments_purged) == (1, 0)
    assert _record(session, earlier) is not None
    assert _env(session).deleted_at is not None
    later = _record(session, earlier)
    assert later is not None and later.purge_after is not None

    second = _sweep(session, later.purge_after)

    assert (second.locations_purged, second.environments_purged) == (1, 1)
    session.rollback()
    assert session.scalars(select(Environment)).all() == []


def test_purge_makes_the_next_sweep_purge_everything(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """`--purge` on a live environment: due at once, no restore, the row goes with the sweep."""
    table_id = _table(catalog, principal, "gold.orders")

    catalog.delete_environment(principal, "dev", purge=True)

    env = _env(session)
    assert env.purge_after is not None and env.purge_after == env.deleted_at
    with pytest.raises(ConflictError):
        catalog.restore_environment(principal, "dev")
    report = _sweep(session, env.purge_after)
    assert (report.locations_purged, report.environments_purged) == (1, 1)
    assert _record(session, table_id) is None
    (event,) = _events(session, AuditEventType.ENVIRONMENT_DELETED)
    assert event.detail["purge"] is True


def test_purge_on_a_deleted_environment_makes_it_and_every_tombstone_due(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The deletion's tombstones and earlier drops alike; audited as a second deletion event."""
    earlier = _table(catalog, principal, "gold.earlier")
    orders = _table(catalog, principal, "gold.orders")
    catalog.drop_table(principal, _ref("gold.earlier"))
    catalog.delete_environment(principal, "dev")

    catalog.delete_environment(principal, "dev", purge=True)

    env = _env(session)
    assert env.purge_after is not None and env.deleted_at is not None
    assert env.purge_after - env.deleted_at < timedelta(minutes=1)
    for table_id in (earlier, orders):
        record = _record(session, table_id)
        assert record is not None and record.purge_after == env.purge_after
    report = _sweep(session, env.purge_after)
    assert (report.locations_purged, report.environments_purged) == (2, 1)
    events = _events(session, AuditEventType.ENVIRONMENT_DELETED)
    assert [e.detail.get("purge") for e in events] == [False, True]


def test_purge_needs_manage_environment(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The right of a deletion; refused, nothing changes."""
    catalog.delete_environment(principal, "dev")
    before = _purge_after(session)

    with pytest.raises(PermissionDenied):
        _catalog(session, ActionAllowList()).delete_environment(principal, "dev", purge=True)

    assert _purge_after(session) == before


def test_a_live_table_in_another_environment_on_the_root_is_never_purged(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """Environments can share a root: a table of `test` nested in a tombstone blocks its purge."""
    catalog.create_environment(principal, name="test", storage=_storage(store))
    _table(catalog, principal, "gold.outer")
    outer = catalog.load_table(principal, _ref("gold.outer")).metadata_location
    outer_root = outer.rsplit("/metadata/", 1)[0]
    directory = outer_root.rsplit("/", 1)[1]
    nested = NamespacePath.from_parts(["gold", directory])
    catalog.create_namespace(principal, "test", nested)
    inner_ref = TableRef("test", nested, "inner")
    catalog.create_table(
        principal, inner_ref, schema=Schema(NestedField(1, "id", LongType(), required=False))
    )
    inner = catalog.load_table(principal, inner_ref).metadata_location
    assert inner.startswith(outer_root + "/")
    catalog.delete_environment(principal, "dev")

    report = _sweep(session, _purge_after(session) + timedelta(days=1))

    assert (report.purges_blocked, report.environments_purged) == (1, 0)
    assert Path(inner.removeprefix("file://")).exists()
    assert Path(outer.removeprefix("file://")).exists()
    assert catalog.load_table(principal, inner_ref).metadata_location == inner


def test_retention_never_expires_a_table_in_a_deleted_environment(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A due table left in a deleted environment (none should be) is not dropped by an expiry."""
    table_id = _table(catalog, principal, "gold.orders")
    catalog.set_table_retention(principal, _ref("gold.orders"), days=1)
    session.execute(
        update(Environment)
        .where(Environment.name == "dev")
        .values(deleted_at=Environment.created_at, purge_after=Environment.created_at)
    )
    session.commit()
    deleted_at = _env(session).deleted_at
    assert deleted_at is not None

    report = _sweep(session, deleted_at + timedelta(days=400))

    assert (report.tables_expired, report.environments_purged) == (0, 0)
    session.rollback()
    assert session.get(Table, table_id) is not None


# --- what the deletion meets on its way --------------------------------------------------------


def test_a_claim_whose_create_landed_is_made_live_and_then_tombstoned(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A create in doubt whose row exists: its claim is its table's, which the deletion drops."""
    table_id = _table(catalog, principal, "gold.orders")
    session.execute(
        update(TableLocation)
        .where(TableLocation.table_id == table_id)
        .values(state=TableLocationState.CLAIMED, table_id=None)
    )
    session.commit()

    catalog.delete_environment(principal, "dev")

    record = _record(session, table_id)
    assert record is not None and record.state is TableLocationState.DROPPED
    assert _env(session).dropped_location_ids == [str(record.id)]


def test_a_claim_or_a_table_gone_before_its_lock_is_skipped(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """Released by its create, or dropped at the same moment: nothing to do for it."""
    claim_location(
        session,
        environment_id=_env(session).id,
        location=f"file://{store}/managed/gold/gone-{uuid.uuid4().hex}",
        holder_run_id=None,
    )
    _table(catalog, principal, "gold.orders")
    with (
        mock.patch.object(environment_deletion, "lock_location", return_value=None),
        mock.patch.object(environment_deletion, "lock_row", return_value=None),
    ):
        catalog.delete_environment(principal, "dev")

    env = _env(session)
    assert env.deleted_at is not None and env.dropped_location_ids == []


def test_a_managed_table_without_its_own_record_is_counted_and_only_uncatalogued(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """As a drop leaves such a table: no tombstone, its files stay, the count says so."""
    table_id = _table(catalog, principal, "gold.orders")
    record = _record(session, table_id)
    assert record is not None
    session.execute(sa_delete(TableLocation).where(TableLocation.id == record.id))
    session.commit()
    files = _files(store)

    catalog.delete_environment(principal, "dev")

    (event,) = _events(session, AuditEventType.ENVIRONMENT_DELETED)
    assert (event.detail["tables"], event.detail["tables_without_tombstone"]) == (0, 1)
    assert _files(store) == files


def test_a_restore_whose_name_is_taken_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A row of the name in the namespace (a view that slipped in): nothing is restored."""
    _table(catalog, principal, "gold.orders")
    catalog.delete_environment(principal, "dev")
    namespace = session.scalars(select(Namespace).where(Namespace.path == "gold")).one()
    session.add(
        Table(id=uuid.uuid4(), namespace_id=namespace.id, name="orders", table_type=TableType.VIEW)
    )
    session.commit()

    with pytest.raises(ConflictError, match="name is taken"):
        catalog.restore_environment(principal, "dev")

    assert _env(session).deleted_at is not None


@contextlib.contextmanager
def _deleted_just_before_the_lock(session: Session) -> Generator[None]:
    """Mark `dev` deleted, from another transaction, right before its row lock is read.

    Parameters
    ----------
    session
        The session the catalog uses.

    Yields
    ------
    None
        While the hook is installed.
    """

    def interleave(state: ORMExecuteState) -> None:
        query = state.statement
        if (
            isinstance(query, Select)
            and query._for_update_arg is not None
            and query.column_descriptions[0]["entity"] is Environment
        ):
            state.session.connection().execute(
                update(Environment)
                .where(Environment.name == "dev")
                .values(deleted_at=Environment.created_at, purge_after=Environment.created_at)
            )

    event.listen(session, "do_orm_execute", interleave)
    try:
        yield
    finally:
        event.remove(session, "do_orm_execute", interleave)


def test_an_external_table_created_while_the_environment_is_deleted_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """The create's lock of the row sees the deletion: no row in a deleted environment."""
    raw = store / "raw" / "events" / "a.parquet"
    raw.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), raw)
    with (
        _deleted_just_before_the_lock(session),
        pytest.raises(NotFoundError, match="No environment named 'dev'"),
    ):
        catalog.create_external_table(
            principal,
            TableRef("dev", _BRONZE, "events"),
            location_pattern="raw/events/*.parquet",
            format="parquet",
        )
    session.rollback()
    assert session.scalars(select(Table)).all() == []


def test_an_undrop_while_the_environment_is_deleted_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The undrop's share lock of the row sees the deletion before it takes the tombstone."""
    table_id = _table(catalog, principal, "gold.orders")
    catalog.drop_table(principal, _ref("gold.orders"))

    with _deleted_just_before_the_lock(session), pytest.raises(NotFoundError):
        catalog.undrop_table(principal, _ref("gold.orders"))

    record = _record(session, table_id)
    assert record is not None and record.state is TableLocationState.DROPPED


def test_the_environment_names_a_read_check_walks_leave_out_a_deleted_one(
    session: Session, catalog: CatalogService, principal: PrincipalRef, store: Path
) -> None:
    """A grant on a deleted environment lets nobody read the repository or index registry."""
    catalog.create_environment(principal, name="test", storage=_storage(store / "test"))
    catalog.delete_environment(principal, "dev")
    assert live_environment_names(session) == ["test"]


@contextlib.contextmanager
def _statements_as_postgres(session: Session) -> Generator[list[str]]:
    """Record what `session` executes, compiled for PostgreSQL, and mark each delete of a table.

    A `Table` deleted through the unit of work is flushed, not executed, so a flush that
    deletes one appends ``DELETE tables`` in its place.

    Parameters
    ----------
    session
        Database session.

    Yields
    ------
    list of str
        The statements, in the order they ran.
    """
    seen: list[str] = []

    def record(state: ORMExecuteState) -> None:
        """Keep one statement.

        Parameters
        ----------
        state
            The statement about to run.
        """
        seen.append(
            str(cast("ClauseElement", state.statement).compile(dialect=postgresql.dialect()))
        )

    def flushed(flushing: Session, context: object, instances: object) -> None:
        """Mark a flush that deletes a table.

        Parameters
        ----------
        flushing
            The session.
        context
            The flush context.
        instances
            Unused.
        """
        if any(isinstance(row, Table) for row in flushing.deleted):
            seen.append("DELETE tables")

    event.listen(session, "do_orm_execute", record)
    event.listen(session, "before_flush", flushed)
    try:
        yield seen
    finally:
        event.remove(session, "do_orm_execute", record)
        event.remove(session, "before_flush", flushed)


def _table_lock(seen: list[str]) -> int:
    """Find the statement that locks the tables, and check it locks them in id order.

    Parameters
    ----------
    seen
        The statements, from `_statements_as_postgres`.

    Returns
    -------
    int
        Its position.
    """
    locks = [
        i
        for i, sql in enumerate(seen)
        if sql.startswith("SELECT tables.id") and sql.rstrip().endswith("FOR UPDATE")
    ]
    assert locks, "no SELECT tables.id ... FOR UPDATE"
    assert "ORDER BY tables.id FOR UPDATE" in seen[locks[0]]
    return locks[0]


def test_a_deletion_locks_its_tables_in_id_order_before_anything_else(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The order read recording takes them, so a completion and the deletion cannot deadlock.

    The table locks come before every other table or location statement (#558): a completion
    of a query that read two of these tables locks them in id order too.
    """
    for name in ("gold.zeta", "gold.alpha", "silver.mid"):
        _table(catalog, principal, name)
    session.rollback()

    with _statements_as_postgres(session) as seen:
        catalog.delete_environment(principal, "dev")

    lock = _table_lock(seen)
    later = [
        i
        for i, sql in enumerate(seen)
        if i != lock and re.search(r"\b(tables|table_locations)\b", sql)
    ]
    assert "DELETE tables" in seen
    assert all(i > lock for i in later), seen[: lock + 1]
    session.rollback()
    assert session.scalars(select(Table)).all() == []


def test_the_last_purge_locks_the_remaining_tables_in_id_order_before_the_delete(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The artifacts the deletion kept go with the row: their locks come first, in id order."""
    namespace_id = session.scalars(select(Namespace.id).where(Namespace.path == "gold")).one()
    for name in ("model_b", "model_a"):
        session.add(
            Table(
                id=uuid.uuid4(),
                namespace_id=namespace_id,
                name=name,
                table_type=TableType.ARTIFACT,
                owner_id=principal.id,
            )
        )
    session.commit()
    catalog.delete_environment(principal, "dev")
    env = _env(session)
    purge_after = _purge_after(session)

    with _statements_as_postgres(session) as seen:
        assert environment_deletion.purge_environment(session, env.id, now=purge_after)

    lock = _table_lock(seen)
    delete = next(i for i, sql in enumerate(seen) if sql.startswith("DELETE FROM environments"))
    assert lock < delete
    assert session.scalars(select(Table)).all() == []
