"""Deleting an environment: tombstones for an undo period, the restore, and the last purge.

A deletion (`CatalogService.delete_environment`) runs in one transaction under the environment's
row lock:

- every `CLAIMED` location record of the environment is resolved under its own lock: `LIVE`
  when a table row names it (the create landed, and its table is dropped below with the
  others), else `ABANDONED` with the deletion's `purge_after`;
- every managed table is dropped through the drop path (`tombstones.drop_row`, ``reason:
  environment_deleted``): its grants, row filters and column masks are revoked onto its
  tombstone, which keeps the files until `purge_after`; views and external tables are dropped
  the same way and leave no tombstone (an external table's files are never Lakana's to delete);
  artifacts stay, hidden with the environment;
- what is still bound to the environment (its own grants, its namespaces', its artifacts'
  policies) is revoked, and listed on the row; task tokens scoped to it are revoked;
- the row is marked deleted (`deleted_at`, `deleted_by`, `purge_after`, the tombstones it made).

A deleted environment is hidden from every lookup by name (`LIVE_ENVIRONMENT`) and its name
stays taken. A create that claimed a location before the deletion finds its claim abandoned;
one that claims after it is refused by `lock_live_environment`, which the publish takes before
the fence, as an undrop does before it takes a tombstone's lock. The lock order is always the
environment's row first, then a table's, then a location record's. The deletion and the last purge
lock every table they delete at once, in id order (`lock_tables_in_id_order`): the order in which a
query's completion locks the tables it read, so the two never deadlock.

`restore_environment` undoes the deletion within the undo period, under the environment's lock
and every one of its tombstones' locks: the same tables with the same ids and metadata, exactly
the policies the deletion revoked, and the row unmarked. A table dropped before the deletion
stays a tombstone. Once the sweep has started purging one of the deletion's tombstones, or the
undo period has passed, it is refused whole.

The location sweep purges the tombstones as it purges any (it resolves a deleted environment's
storage with ``include_deleted``). `purge_environment` then deletes the row, once its
`purge_after` has passed and no location record of it is left in any state; the database
cascades the rest (namespaces, artifact rows, secrets, runs). An artifact's files are left in
storage then, as a deletion always left them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, exists, select, update
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import PrincipalRef, Securable
from lakana_core.catalog.locations import lock_location, own_table_id
from lakana_core.catalog.retention import lock_row
from lakana_core.catalog.table_locks import lock_tables_in_id_order
from lakana_core.catalog.tombstones import (
    drop_row,
    refresh_restored,
    restore_policies,
    restore_table,
    revive,
    unrevoke,
)
from lakana_core.errors import ConflictError
from lakana_core.grants.revocation import revoke_attached
from lakana_core.identifiers import NamespacePath, TableRef, shown
from lakana_core.identity.task_tokens import revoke_task_tokens_for_environment
from lakana_core.models import (
    AuditEventType,
    Environment,
    Namespace,
    Table,
    TableLocation,
    TableLocationState,
    TableType,
)
from lakana_core.models.table_location import PURGEABLE_STATES

DELETED_HINT_MESSAGE = (
    "No environment named {name!r}. It was deleted, and it can be restored with `lakana env "
    "restore {name}` until {purge_after} (UTC); `lakana env list --deleted` lists the deleted "
    "environments."
)
"""The `NotFoundError` of a deleted environment, for a caller who may restore it.

`{name}`, `{purge_after}`. Anyone else is told only that no environment has the name.
"""

NAME_HELD_MESSAGE = (
    "Environment {name!r} cannot be created: an environment of that name was deleted, and its "
    "tables' files are kept so that it can be restored until {purge_after} (UTC). Restore it "
    "with `lakana env restore {name}`, or delete its files now with `lakana env delete {name} "
    "--purge`; the name is free again once the worker's clean-up has deleted them."
)
"""The `AlreadyExistsError` of a create whose name a deleted environment holds."""

NOT_DELETED_MESSAGE = "Environment {name!r} is not deleted, so there is nothing to restore."
"""The `ConflictError` of a restore of an environment that exists (`{name}`)."""

NO_DELETED_MESSAGE = (
    "No deleted environment named {name!r} can be restored: none was deleted under that name, "
    "or its files are already deleted. `lakana env list --deleted` lists the deleted "
    "environments and until when each can be restored."
)
"""The `NotFoundError` of a restore that finds no environment of the name (`{name}`)."""

RESTORE_PERIOD_PASSED_MESSAGE = (
    "Environment {name!r} cannot be restored: its undo period ended at {purge_after} (UTC), and "
    "the worker's clean-up deletes its tables' files. A deleted environment can be restored for "
    "LAKANA_TABLES__UNDO_DAYS days (7 by default); one deleted with --purge cannot be restored."
)
"""The `ConflictError` of a restore after ``purge_after`` (`{name}`, `{purge_after}`)."""

RESTORE_PURGE_STARTED_MESSAGE = (
    "Environment {name!r} cannot be restored: the worker's clean-up has started deleting the "
    "files of one of its tables, so the environment can no longer be brought back whole. "
    "Nothing was restored."
)
"""The `ConflictError` of a restore after the sweep began purging a tombstone (`{name}`)."""

RESTORE_FILES_GONE_MESSAGE = (
    "Environment {name!r} cannot be restored: the files of its table {table} are gone (its "
    "current metadata file no longer exists in the environment's storage), so nothing was "
    "restored. Restore the files from a backup of the storage if you have one, then try again."
)
"""The `ConflictError` of a restore whose table's metadata file is missing (`{name}`, `{table}`)."""

ENVIRONMENT_GONE_MESSAGE = (
    "{table} was not created: environment {environment!r} was deleted while the create ran. "
    "Nothing was published, and the worker's clean-up removes the files it wrote."
)
"""The `NotFoundError` of a create whose environment was deleted before its commit."""


def lock_live_environment(session: Session, environment_id: uuid.UUID) -> bool:
    """Take a share lock on an environment's row and say whether it is still there, not deleted.

    The fence a create's publish and an undrop take before they make a table appear: a
    deletion holds the row's exclusive lock while it drops the tables, so either it waits for
    the commit and then drops the new table too, or it came first and this sees `deleted_at`.
    On SQLite, which ignores the lock, the caller must already have written in the
    transaction, so the database's write lock orders the two.

    Parameters
    ----------
    session
        Database session; the transaction that adds the table.
    environment_id
        The environment.

    Returns
    -------
    bool
        `True` while the environment exists and is not deleted.
    """
    found = session.execute(
        select(Environment.deleted_at)
        .where(Environment.id == environment_id)
        .with_for_update(read=True)
    ).one_or_none()
    return found is not None and found[0] is None


def _in_environment(environment_id: uuid.UUID) -> ColumnElement[bool]:
    """Select the tables of an environment, without joining (a lock takes the tables' rows only).

    Parameters
    ----------
    environment_id
        The environment.

    Returns
    -------
    ColumnElement
        ``tables.namespace_id IN (the environment's namespaces)``.
    """
    return Table.namespace_id.in_(
        select(Namespace.id).where(Namespace.environment_id == environment_id)
    )


def _resolve_claims(session: Session, env: Environment, purge_after: datetime) -> None:
    """Resolve every claim of an environment being deleted, each under its record's lock.

    Parameters
    ----------
    session
        Database session; the deletion's transaction.
    env
        The environment, locked.
    purge_after
        The deletion's `purge_after`, given to an abandoned claim.
    """
    claims = session.scalars(
        select(TableLocation.id).where(
            TableLocation.environment_id == env.id,
            TableLocation.state == TableLocationState.CLAIMED,
        )
    ).all()
    for record_id in claims:
        record = lock_location(session, record_id)
        if record is None or record.state is not TableLocationState.CLAIMED:
            continue
        table_id = own_table_id(session, record.location)
        if table_id is not None:
            record.state = TableLocationState.LIVE  # it landed; the table is dropped below
            record.table_id = table_id
        else:
            record.state = TableLocationState.ABANDONED
            record.purge_after = purge_after
    session.flush()


def delete_rows(
    session: Session,
    actor: PrincipalRef,
    env: Environment,
    *,
    now: datetime,
    purge_after: datetime,
    purge: bool,
) -> dict[str, Any]:
    """Turn a locked environment into a deleted one, inside the caller's transaction.

    Parameters
    ----------
    session
        Database session; the caller holds the environment's row lock and commits.
    actor
        Who deletes it.
    env
        The environment, locked and not deleted.
    now
        The deletion's time; every revocation and tombstone is stamped with it.
    purge_after
        From when on the files may be deleted.
    purge
        Whether the caller asked for the files to be due at once (``--purge``).

    Returns
    -------
    dict
        The `ENVIRONMENT_DELETED` event's detail: ``tables`` (tombstones made),
        ``tables_without_tombstone`` (managed tables whose location had no record of their
        own, only uncatalogued), ``purge_after`` and ``purge``.
    """
    # Every table it drops, in id order, before anything else: the order a query's completion
    # locks the tables it read, and table rows before location records (#558).
    lock_tables_in_id_order(
        session, _in_environment(env.id), Table.table_type != TableType.ARTIFACT
    )
    _resolve_claims(session, env, purge_after)
    found = session.execute(
        select(Table.id, Namespace.path)
        .join(Namespace, Table.namespace_id == Namespace.id)
        .where(Namespace.environment_id == env.id, Table.table_type != TableType.ARTIFACT)
        .order_by(Namespace.path, Table.name)
    ).all()
    dropped: list[str] = []
    without = 0
    for table_id, path in found:
        row = lock_row(session, Table, table_id)
        if row is None:
            continue
        ref = TableRef(env.name, NamespacePath.parse(path), row.name)
        managed = row.table_type is TableType.ICEBERG
        record = drop_row(
            session,
            actor,
            ref,
            row,
            now=now,
            purge_after=purge_after,
            reason="environment_deleted",
        )
        if record is not None:
            dropped.append(str(record.id))
        elif managed:
            without += 1
    revoked = revoke_attached(session, actor, environment_id=env.id, now=now)
    revoke_task_tokens_for_environment(session, actor, env.name)
    env.deleted_at = now
    env.deleted_by = actor.id
    env.purge_after = purge_after
    env.dropped_location_ids = dropped
    env.revoked_policy_ids = [policy.as_json() for policy in revoked]
    session.flush()
    return {
        "tables": len(dropped),
        "tables_without_tombstone": without,
        "purge_after": purge_after.isoformat(),
        "purge": purge,
    }


def purge_early(session: Session, env: Environment, *, now: datetime) -> dict[str, Any]:
    """Make a deleted environment and every tombstone of it due now, inside the transaction.

    Parameters
    ----------
    session
        Database session; the caller holds the environment's row lock and commits.
    env
        The deleted environment, locked.
    now
        The time from which on everything is due.

    Returns
    -------
    dict
        The `ENVIRONMENT_DELETED` event's detail of the early purge.
    """
    assert env.purge_after is not None  # nosec B101 -- a deleted environment has one
    now = min(env.purge_after, now)
    env.purge_after = now
    session.execute(
        update(TableLocation)
        .where(
            TableLocation.environment_id == env.id,
            TableLocation.state.in_(PURGEABLE_STATES),
            TableLocation.purge_started_at.is_(None),
            TableLocation.purge_after > now,
        )
        .values(purge_after=now)
        .execution_options(synchronize_session=False)
    )
    session.flush()
    return {"purge_after": now.isoformat(), "purge": True, "already_deleted": True}


def restore_rows(
    session: Session,
    actor: PrincipalRef,
    env: Environment,
    *,
    now: datetime,
    file_exists: Callable[[str], bool],
) -> dict[str, Any]:
    """Undo a locked environment's deletion, inside the caller's transaction.

    Every check comes before the first change, so a refusal restores nothing.

    Parameters
    ----------
    session
        Database session; the caller holds the environment's row lock and commits.
    actor
        Who restores it.
    env
        The deleted environment, locked.
    now
        The restore's time.
    file_exists
        Says whether a metadata file is still in the environment's storage.

    Returns
    -------
    dict
        The `ENVIRONMENT_RESTORED` event's detail: ``tables``, ``restored_policies`` and
        ``deleted_at``.

    Raises
    ------
    ConflictError
        After the undo period, once a purge of one of the tombstones has started (or its
        record is gone), when a table's metadata file is missing, or when a name is taken.
    """
    assert env.deleted_at is not None and env.purge_after is not None  # nosec B101
    if now >= env.purge_after:
        raise ConflictError(
            RESTORE_PERIOD_PASSED_MESSAGE.format(
                name=env.name, purge_after=env.purge_after.isoformat(timespec="seconds")
            )
        )
    records = []
    for record_id in env.dropped_location_ids or []:
        record = lock_location(session, uuid.UUID(record_id))
        if (
            record is None
            or record.state is not TableLocationState.DROPPED
            or record.purge_started_at is not None
            or record.dropped_at != env.deleted_at
            or not record.dropped_table
        ):
            raise ConflictError(RESTORE_PURGE_STARTED_MESSAGE.format(name=env.name))
        records.append(record)
    for record in records:
        snapshot = record.dropped_table or {}
        if not file_exists(snapshot["row"]["metadata_location"]):
            dotted = ".".join([*snapshot["namespace"], snapshot["row"]["name"]])
            raise ConflictError(
                RESTORE_FILES_GONE_MESSAGE.format(name=env.name, table=shown(dotted))
            )
    restored_policies: list[dict[str, str]] = []
    for record in records:
        restored_policies += _restore_one(session, actor, env, record, now=now)
    restored_policies += unrevoke(session, env.revoked_policy_ids or [], revoked_at=env.deleted_at)
    detail = {
        "tables": len(records),
        "restored_policies": len(restored_policies),
        "deleted_at": env.deleted_at.isoformat(),
    }
    env.deleted_at = None
    env.deleted_by = None
    env.purge_after = None
    env.dropped_location_ids = None
    env.revoked_policy_ids = None
    session.flush()
    return detail


def _restore_one(
    session: Session, actor: PrincipalRef, env: Environment, record: TableLocation, *, now: datetime
) -> list[dict[str, str]]:
    """Bring one of a deletion's tables back from its locked tombstone.

    Parameters
    ----------
    session
        Database session; the restore's transaction.
    actor
        Who restores it.
    env
        Its environment.
    record
        The tombstone, locked and checked.
    now
        The restore's time.

    Returns
    -------
    list of dict
        The policies restored with it.

    Raises
    ------
    ConflictError
        If its namespace is gone or its name is taken.
    """
    snapshot = record.dropped_table or {}
    namespace = NamespacePath.from_parts(snapshot["namespace"])
    name = snapshot["row"]["name"]
    namespace_id = session.scalar(
        select(Namespace.id).where(
            Namespace.environment_id == env.id, Namespace.path == namespace.joined()
        )
    )
    taken = namespace_id is not None and session.scalar(
        select(exists().where(Table.namespace_id == namespace_id, Table.name == name))
    )
    if namespace_id is None or taken:
        raise ConflictError(
            f"Environment {env.name!r} cannot be restored: {shown(f'{namespace}.{name}')} has no "
            "namespace or its name is taken. Nothing was restored."
        )
    row = restore_table(session, snapshot, namespace_id=namespace_id, name=name)
    retention_cleared = refresh_restored(row, actor.id, now)
    restored = restore_policies(
        session, record, table_id=row.id, namespace=namespace.joined(), name=name
    )
    assert record.dropped_at is not None  # nosec B101 -- checked by the caller
    AuditService(session).record(
        actor.id,
        AuditEventType.TABLE_UNDROPPED,
        securable=Securable.table_scope(env.name, namespace, name),
        detail={
            "reason": "environment_restored",
            "table_id": str(row.id),
            "name": name,
            "dropped_name": name,
            "dropped_at": record.dropped_at.isoformat(),
            "restored_policies": restored,
            "retention_cleared": retention_cleared,
        },
    )
    revive(record)
    return restored


def purge_environment(session: Session, environment_id: uuid.UUID, *, now: datetime) -> bool:
    """Delete a deleted environment's row once nothing of it is left to purge, and commit.

    Under the row's lock, after deciding again: the environment is still deleted, its
    `purge_after` has passed and no location record of it is left in any state. Anything still
    bound to it is revoked first; the database cascades the rest.

    Parameters
    ----------
    session
        Database session, with no transaction open; left without one.
    environment_id
        The environment.
    now
        The sweep's time.

    Returns
    -------
    bool
        Whether the row was deleted.
    """
    try:
        env = lock_row(session, Environment, environment_id)
        if (
            env is None
            or env.deleted_at is None
            or env.purge_after is None
            or env.purge_after > now
        ):
            return False
        left = session.scalar(
            select(exists().where(TableLocation.environment_id == environment_id))
        )
        if left:
            return False
        # The row's delete cascades through the namespaces to the tables left (the artifacts)
        # and their read counts; locked first, in id order, like a query's completion (#558).
        lock_tables_in_id_order(session, _in_environment(environment_id))
        revoke_attached(session, None, environment_id=environment_id, now=now)
        AuditService(session).record(
            None,
            AuditEventType.ENVIRONMENT_PURGED,
            securable=Securable.environment_scope(env.name),
            detail={
                "deleted_at": env.deleted_at.isoformat(),
                "purge_after": env.purge_after.isoformat(),
            },
        )
        session.execute(
            delete(Environment)
            .where(Environment.id == environment_id)
            .execution_options(synchronize_session=False)
        )
        session.commit()
        return True
    finally:
        session.rollback()
