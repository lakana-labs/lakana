"""Purging only the rows of a deleted environment whose storage cannot be resolved (#261).

An environment whose root is in a scheme Lakana cannot read (`local://`, `memory://`) is
skipped by the location sweep, so its row, and with it its name, would stay for ever. An
administrator removes the row and what the sweep would have removed with it; no file is
touched. Refused while the storage can still be resolved, so a normal deletion keeps its undo
period and its file purge.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import ActionAllowList
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_environment_deletion import _statements_as_postgres, _table_lock

from lakana_core.auth import Action, PrincipalRef, Securable
from lakana_core.catalog import CatalogService
from lakana_core.catalog.environment_deletion import (
    ROWS_PURGE_MISSING_MESSAGE,
    ROWS_PURGE_NOT_DELETED_MESSAGE,
    ROWS_PURGE_REACHABLE_MESSAGE,
)
from lakana_core.config import StorageConfig
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    LakanaError,
    NotFoundError,
    PermissionDenied,
)
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    Namespace,
    PackageIndex,
    Secret,
    SecretUseKind,
    Table,
    TableLocation,
    TableLocationState,
    TableType,
)
from lakana_core.storage.resolver import StorageResolver


def _old(
    session: Session, principal: PrincipalRef, name: str = "old", root: str = "local:///data"
) -> Environment:
    """Insert an environment as an older release accepted it, on a root it cannot read.

    It has a namespace with two artifacts, whose name order is the reverse of their id order.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The artifacts' owner.
    name
        Its name.
    root
        Its storage root.

    Returns
    -------
    Environment
        The row.
    """
    env = Environment(id=uuid.uuid4(), name=name, storage_root=root, storage_options={})
    namespace = Namespace(id=uuid.uuid4(), environment_id=env.id, path="gold")
    session.add_all([env, namespace])
    ids = sorted(uuid.uuid4() for _ in range(2))
    for table_id, table_name in zip(ids, ("model_b", "model_a"), strict=True):
        session.add(
            Table(
                id=table_id,
                namespace_id=namespace.id,
                name=table_name,
                table_type=TableType.ARTIFACT,
                owner_id=principal.id,
            )
        )
    session.commit()
    return env


def _records(session: Session, env: Environment, now: datetime) -> None:
    """Give a deleted environment a dropped table's record and an abandoned claim.

    Parameters
    ----------
    session
        Database session; commits.
    env
        The environment.
    now
        Their times.
    """
    for state in (TableLocationState.DROPPED, TableLocationState.ABANDONED):
        session.add(
            TableLocation(
                id=uuid.uuid4(),
                environment_id=env.id,
                location=f"local:///data/managed/gold/t-{uuid.uuid4().hex}",
                state=state,
                claimed_at=now,
                purge_after=now,
            )
        )
    session.commit()


def _deleted_old(session: Session, catalog: CatalogService, principal: PrincipalRef) -> Environment:
    """Make `old`, delete it, and give it two location records.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog.
    principal
        The deleter.

    Returns
    -------
    Environment
        The deleted row.
    """
    env = _old(session, principal)
    deleted = catalog.delete_environment(principal, "old")
    assert deleted.deleted_at is not None
    _records(session, env, deleted.deleted_at)
    return env


def _catalog(session: Session, authz: ActionAllowList) -> CatalogService:
    """Build a catalog with a recording authorization.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return CatalogService(
        session,
        StorageResolver(session),
        authz,
    )


def test_the_rows_go_and_the_name_is_free_again(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The row, its records, namespaces and artifacts go, audited; the name can be created."""
    _deleted_old(session, catalog, principal)

    assert catalog.purge_environment_rows(principal, "old") == 2

    session.rollback()
    assert session.scalars(select(Environment)).all() == []
    assert session.scalars(select(TableLocation)).all() == []
    assert session.scalars(select(Namespace)).all() == []
    assert session.scalars(select(Table)).all() == []
    (event,) = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.ENVIRONMENT_PURGED)
    ).all()
    assert event.principal_id == principal.id
    assert event.securable_environment == "old"
    assert event.detail["rows_only"] is True
    assert event.detail["locations"] == 2
    assert event.detail.get("storage_root") == "local:///data"
    assert event.detail["deleted_at"]
    session.rollback()
    storage = StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})
    assert catalog.create_environment(principal, name="old", storage=storage).name == "old"


def test_an_environment_whose_storage_resolves_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Its files are the sweep's to delete; the row stays, and the message names the way."""
    storage = StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})
    catalog.create_environment(principal, name="dev", storage=storage)
    deleted = catalog.delete_environment(principal, "dev")
    assert deleted.purge_after is not None

    with pytest.raises(ConflictError) as info:
        catalog.purge_environment_rows(principal, "dev")

    until = deleted.purge_after.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    assert str(info.value) == ROWS_PURGE_REACHABLE_MESSAGE.format(name="dev", purge_after=until)
    assert "lakana env delete dev --purge" in str(info.value)
    session.rollback()
    assert session.scalars(select(Environment.name)).all() == ["dev"]


def test_an_environment_that_is_not_deleted_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Even on a root that cannot be resolved: delete it first."""
    _old(session, principal)

    with pytest.raises(ConflictError) as info:
        catalog.purge_environment_rows(principal, "old")

    assert str(info.value) == ROWS_PURGE_NOT_DELETED_MESSAGE.format(name="old")
    assert "lakana env delete old" in str(info.value)
    session.rollback()
    assert session.scalars(select(Environment.deleted_at)).all() == [None]


def test_an_unknown_environment_is_not_found(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The message names where the deleted environments are listed."""
    with pytest.raises(NotFoundError) as info:
        catalog.purge_environment_rows(principal, "nope")

    assert str(info.value) == ROWS_PURGE_MISSING_MESSAGE.format(name="nope")
    assert "lakana env list --deleted" in str(info.value)


def test_it_needs_manage_environment_on_the_instance(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Checked first, at instance scope, never against the environment's own name."""
    _deleted_old(session, catalog, principal)
    refused = ActionAllowList()

    with pytest.raises(PermissionDenied):
        _catalog(session, refused).purge_environment_rows(principal, "old")

    assert refused.seen == [(Action.MANAGE_ENVIRONMENT, Securable.instance_scope())]
    session.rollback()
    assert session.scalars(select(Environment.name)).all() == ["old"]
    allowed = ActionAllowList(Action.MANAGE_ENVIRONMENT)
    assert _catalog(session, allowed).purge_environment_rows(principal, "old") == 2
    assert allowed.seen == [(Action.MANAGE_ENVIRONMENT, Securable.instance_scope())]


def test_it_locks_the_tables_then_the_records_in_id_order(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The order a deletion and a query's completion take them (#558, #585), then the row."""
    _deleted_old(session, catalog, principal)
    session.rollback()

    with _statements_as_postgres(session) as seen:
        catalog.purge_environment_rows(principal, "old")

    tables = _table_lock(seen)
    records = next(
        i
        for i, sql in enumerate(seen)
        if sql.startswith("SELECT table_locations.id") and sql.rstrip().endswith("FOR UPDATE")
    )
    assert "ORDER BY table_locations.id FOR UPDATE" in seen[records]
    removed = next(i for i, sql in enumerate(seen) if sql.startswith("DELETE FROM table_locations"))
    row = next(i for i, sql in enumerate(seen) if sql.startswith("DELETE FROM environments"))
    assert tables < records < removed < row


def test_a_create_of_the_held_name_names_the_rows_purge(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The refusal of the name says how it is freed when the storage cannot be reached."""
    _deleted_old(session, catalog, principal)
    storage = StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})

    with pytest.raises(AlreadyExistsError) as info:
        catalog.create_environment(principal, name="old", storage=storage)

    assert "`lakana env purge old --rows-only`" in str(info.value)


@pytest.mark.parametrize(
    "root", ["local:///data", "memory://", "gs://bucket/lake", "file:///data/lake::memory://x"]
)
def test_a_root_no_process_can_read_is_purged(
    session: Session, catalog: CatalogService, principal: PrincipalRef, root: str
) -> None:
    """An unsupported scheme or an fsspec chain: no clean-up will ever reach its files."""
    _old(session, principal, root=root)
    catalog.delete_environment(principal, "old")

    assert catalog.purge_environment_rows(principal, "old") == 0

    session.rollback()
    assert session.scalars(select(Environment)).all() == []


def _object_store_with_secret(
    session: Session, catalog: CatalogService, principal: PrincipalRef, *, bound: bool
) -> str:
    """Delete an `s3` environment whose storage secret cannot be used, and say until when.

    Parameters
    ----------
    session
        Database session; commits.
    catalog
        The catalog.
    principal
        The deleter.
    bound
        Give it a secret bound to a package index; otherwise name a secret that does not exist.

    Returns
    -------
    str
        Its `purge_after`, as the refusal writes it.
    """
    env = _old(session, principal, root="s3://bucket/prod")
    env.storage_secret_ref = "corp-token"
    if bound:
        index = PackageIndex(id=uuid.uuid4(), name="corp", url="https://corp.example.org/simple")
        session.add(index)
        session.add(
            Secret(
                id=uuid.uuid4(),
                environment_id=env.id,
                name="corp-token",
                ciphertext=b"x",
                nonce=b"n" * 12,
                wrapped_data_key=b"k",
                format_version=2,
                key_id="env",
                created_by=principal.id,
                created_at=datetime.now(UTC),
                use_kind=SecretUseKind.PACKAGE_INDEX,
                use_target_id=index.id,
            )
        )
    session.commit()
    deleted = catalog.delete_environment(principal, "old")
    assert deleted.purge_after is not None
    return deleted.purge_after.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


@pytest.mark.parametrize("bound", [True, False], ids=["bound-elsewhere", "missing"])
def test_an_object_store_whose_secret_cannot_be_used_keeps_its_row(
    session: Session, catalog: CatalogService, principal: PrincipalRef, bound: bool
) -> None:
    """A secret is a setting to fix, not a storage that is gone: its files may all be there."""
    until = _object_store_with_secret(session, catalog, principal, bound=bound)

    with pytest.raises(LakanaError) as info:
        catalog.purge_environment_rows(principal, "old")

    assert str(info.value) == ROWS_PURGE_REACHABLE_MESSAGE.format(name="old", purge_after=until)
    session.rollback()
    assert session.scalars(select(Environment.name)).all() == ["old"]
