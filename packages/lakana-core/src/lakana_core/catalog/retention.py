"""Retention: when a managed table expires, and the sweep step that drops the tables that have.

A table's retention is its own (`Table.retention_days`, `retention_on` or `retention_never`), or
else its environment's (`Environment.retention_days`). Nothing has one unless someone sets it.

**The clock.** A table's *last activity* is the later of its last change in the catalog (the
row's `updated_at`: every commit of a write or of maintenance moves it, and so does a change to
its description or its retention), its creation, and the last read the worker recorded in
`table_reads`. Reads only ever move it later. `tables.last_read_at` is a display key for the
catalog list and its sorts; retention never reads it, only `table_reads`. The snapshot's own
timestamp is not used: the committing client writes it, so it is not trusted to decide a deletion;
the catalog's time of the commit is never earlier. Reads through the Iceberg REST catalog are not
recorded, and reads older than ``LAKANA_TABLE_READS__KEEP_DAYS`` are swept away, so neither keeps a
table.

- ``days``: the table expires `days` after its last activity, and never earlier than `days` after
  the retention was set (for an environment's retention, after it was set there), so turning a
  retention on gives every table its full period.
- ``on``: the table expires from midnight UTC of that day on, whatever its activity.

**Never expired:** a table in a protected environment (even one whose retention was set before
the protection) or in a deleted one, a table the current project version of a deployment in its
environment declares as an asset, a table a queued or running query writes (by id or by name) or
whose pipeline task has not ended, a table whose location record is not its own `LIVE` one (an
expiry must leave a tombstone), and anything but a managed table.

**The expiry.** `expire_due` is step 1 of `lakana_core.catalog.location_sweep`. For each due
table, under the environment's and the table's row locks, it decides again with what is stored
then and drops the table through `lakana_core.catalog.tombstones.drop_row` (``reason:
retention``, no principal): a tombstone with the undo period (at least one day), which `lakana
table undrop` restores. A restored table counts as active at the restore, and a fixed day that
has passed is removed, so the next sweep does not drop it again.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, TypeVar

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from lakana_core.catalog.tombstones import drop_row
from lakana_core.db import rowcount
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Namespace,
    Principal,
    QueryRun,
    QueryRunStatus,
    Run,
    Table,
    TableLocation,
    TableLocationState,
    TableRead,
    TableType,
    TaskRun,
)
from lakana_core.models.run import TERMINAL_TASK_STATUSES
from lakana_core.projects.manifest import manifest_assets
from lakana_core.storage.resolver import table_root_from_metadata_location

_M = TypeVar("_M", Table, Environment)

MAX_RETENTION_DAYS = 3650
"""The longest retention, in days, and the widest window of the expiring listing."""

DEFAULT_EXPIRY_WARNING_DAYS = 7
"""The default of ``LAKANA_TABLES__EXPIRY_WARNING_DAYS``."""

DEFAULT_MAX_EXPIRIES = 100
"""The default of ``LAKANA_TABLES__MAX_EXPIRIES_PER_SWEEP``."""

MIN_EXPIRY_UNDO_DAYS = 1
"""Days an expired table can be restored at least, even with ``LAKANA_TABLES__UNDO_DAYS=0``."""

ONE_RULE_MESSAGE = (
    "Give exactly one of --days N, --on DATE or --never (REST: `days`, `on` or `never`). "
    "Without one, `lakana table retention NS.TABLE` shows the current retention."
)
"""The `ValidationError` of a retention change that gives none or several rules."""

DAYS_MESSAGE = "{what} must be a whole number of days from 1 to {most} (got {days})."
"""The `ValidationError` of a number of days out of range (`{what}`, `{most}`, `{days}`)."""

PAST_DAY_MESSAGE = (
    "The day a table expires on must be after today ({today}, UTC); got {day}. To drop the "
    "table now, use `lakana table delete`."
)
"""The `ValidationError` of an `--on` day that is today or earlier (`{today}`, `{day}`)."""

NOT_MANAGED_MESSAGE = (
    "{table} is not a managed table: only managed tables have a retention. Views, external "
    "tables and artifacts never expire."
)
"""The `ValidationError` of a retention on anything but a managed table (`{table}`)."""

PROTECTED_TABLE_MESSAGE = (
    "Environment '{environment}' is protected, and its tables never expire, so no retention can "
    "be set there. `--never` is still accepted, to remove one set before the protection."
)
"""The `ValidationError` of `--days` or `--on` in a protected environment (`{environment}`)."""

PROTECTED_ENVIRONMENT_MESSAGE = (
    "Environment '{environment}' is protected, and its tables never expire, so it cannot have a "
    "retention. Unprotect it first with `lakana env unprotect {environment}`, or leave the "
    "retention off."
)
"""The `ValidationError` of an environment retention in a protected environment."""

Source = Literal["table", "environment"]
"""Where a table's retention comes from."""

Kept = Literal["protected", "project_asset"]
"""Why a table that has a retention never expires."""


@dataclass(frozen=True)
class TableRetention:
    """A managed table's retention, and when it expires.

    Parameters
    ----------
    ref
        The table.
    table_id
        Its id.
    source
        ``table`` when the table sets its own, ``environment`` when its environment's applies;
        `None` when it has none.
    days
        The days after its last activity it expires.
    on
        The day it expires on.
    never
        Whether the table says it never expires.
    set_by
        Who set the retention that applies; `None` when nobody did or that principal is gone.
    set_at
        When it was set.
    last_activity_at
        The table's last activity: the later of its last change and its last recorded read.
    expires_at
        When it expires; `None` when it never does.
    kept
        Why a table that has a retention never expires: ``protected`` (its environment) or
        ``project_asset`` (a deployed project declares it); `None` otherwise.
    """

    ref: TableRef
    table_id: uuid.UUID
    source: Source | None
    days: int | None
    on: date | None
    never: bool
    set_by: str | None
    set_at: datetime | None
    last_activity_at: datetime
    expires_at: datetime | None
    kept: Kept | None = None


@dataclass(frozen=True)
class _Rule:
    """The retention that applies to a table, before exclusions.

    Parameters
    ----------
    source
        Where it comes from; `None` for none.
    days
        Its days.
    on
        Its day.
    set_by
        Who set it.
    set_at
        When.
    """

    source: Source | None
    days: int | None = None
    on: date | None = None
    set_by: uuid.UUID | None = None
    set_at: datetime | None = None


def check_days(days: int, what: str) -> None:
    """Refuse a number of days outside 1 to `MAX_RETENTION_DAYS`.

    Parameters
    ----------
    days
        The days.
    what
        What they are, for the message.

    Raises
    ------
    ValidationError
        If out of range.
    """
    if not 1 <= days <= MAX_RETENTION_DAYS:
        raise ValidationError(DAYS_MESSAGE.format(what=what, most=MAX_RETENTION_DAYS, days=days))


def check_rule(*, days: int | None, on: date | None, never: bool, today: date) -> None:
    """Refuse a retention change that is not exactly one valid rule.

    Parameters
    ----------
    days
        `--days`.
    on
        `--on`.
    never
        `--never`.
    today
        The database's day (UTC).

    Raises
    ------
    ValidationError
        Unless exactly one is given, the days are in range and the day is after today.
    """
    if sum((days is not None, on is not None, never)) != 1:
        raise ValidationError(ONE_RULE_MESSAGE)
    if days is not None:
        check_days(days, "The retention")
    if on is not None and on <= today:
        raise ValidationError(PAST_DAY_MESSAGE.format(today=today, day=on))


def expiry_time(
    *, days: int | None, on: date | None, activity: datetime, floor: datetime | None
) -> datetime | None:
    """Compute when a rule makes a table expire.

    Parameters
    ----------
    days
        Days after the activity (or the floor, when later).
    on
        A fixed day; it wins over `days`.
    activity
        The table's last activity.
    floor
        No expiry by days is earlier than this plus the days: when the rule was set.

    Returns
    -------
    datetime or None
        Midnight UTC of `on`; else the later of `activity` and `floor` plus `days`; `None`
        without either.
    """
    if on is not None:
        return datetime.combine(on, time(), UTC)
    if days is None:
        return None
    start = activity if floor is None else max(activity, floor)
    return start + timedelta(days=days)


def _rule(row: Table, env: Environment) -> _Rule:
    """Find the retention that applies to a table.

    Parameters
    ----------
    row
        The table.
    env
        Its environment.

    Returns
    -------
    _Rule
        The table's own when it has one (`never` included, as a rule of no expiry), else the
        environment's, else none.
    """
    if row.retention_never or row.retention_days is not None or row.retention_on is not None:
        return _Rule(
            "table",
            row.retention_days,
            row.retention_on,
            row.retention_set_by,
            row.retention_set_at,
        )
    if env.retention_days is not None:
        return _Rule(
            "environment", env.retention_days, None, env.retention_set_by, env.retention_set_at
        )
    return _Rule(None)


def last_reads(session: Session, table_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """Read the last recorded read of each of some tables.

    Parameters
    ----------
    session
        Database session.
    table_ids
        The tables.

    Returns
    -------
    dict
        Table id → its last read; a table without a recorded read is missing.
    """
    wanted = list(table_ids)
    if not wanted:
        return {}
    rows = session.execute(
        select(TableRead.table_id, func.max(TableRead.last_read_at))
        .where(TableRead.table_id.in_(wanted))
        .group_by(TableRead.table_id)
    ).all()
    return {table_id: _aware(moment) for table_id, moment in rows if moment is not None}


def _aware(moment: datetime) -> datetime:
    """Read a time as UTC, whatever the backend returned.

    Parameters
    ----------
    moment
        A time; naive from an aggregate on SQLite.

    Returns
    -------
    datetime
        The same instant, aware.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def last_activity(row: Table, last_read: datetime | None) -> datetime:
    """Compute a table's last activity.

    Parameters
    ----------
    row
        The table.
    last_read
        Its last recorded read, if any.

    Returns
    -------
    datetime
        The latest of its creation, its last change and its last read.
    """
    moments = [row.created_at, row.updated_at, last_read]
    return max(_aware(m) for m in moments if m is not None)


def deployed_assets(session: Session, environment_id: uuid.UUID) -> frozenset[str]:
    """List the dotted names the current project versions of an environment declare as assets.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.

    Returns
    -------
    frozenset of str
        Every asset's dotted name, across the environment's deployments.
    """
    manifests = session.scalars(
        select(JobVersion.manifest_json)
        .join(
            EnvironmentDeployment,
            EnvironmentDeployment.current_job_version_id == JobVersion.id,
        )
        .where(EnvironmentDeployment.environment_id == environment_id)
    )
    return frozenset(name for manifest in manifests for name in manifest_assets(manifest))


def _names(session: Session, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    """Read the names of some principals.

    Parameters
    ----------
    session
        Database session.
    ids
        Principal ids; `None` is skipped.

    Returns
    -------
    dict
        Id → name, for the principals that still exist.
    """
    wanted = {found for found in ids if found is not None}
    if not wanted:
        return {}
    rows = session.execute(select(Principal.id, Principal.name).where(Principal.id.in_(wanted)))
    return {principal_id: name for principal_id, name in rows.all()}


def retention_of(
    row: Table,
    env: Environment,
    ref: TableRef,
    *,
    deployed: frozenset[str],
    last_read: datetime | None,
    names: Mapping[uuid.UUID, str],
) -> TableRetention:
    """Describe a managed table's retention and when it expires.

    Parameters
    ----------
    row
        The table.
    env
        Its environment.
    ref
        Its reference.
    deployed
        `deployed_assets` of the environment.
    last_read
        Its last recorded read.
    names
        Principal names, for who set the retention.

    Returns
    -------
    TableRetention
        The retention; `expires_at` is `None` when it never expires.
    """
    rule = _rule(row, env)
    activity = last_activity(row, last_read)
    expires = expiry_time(days=rule.days, on=rule.on, activity=activity, floor=rule.set_at)
    kept: Kept | None = None
    if expires is not None and env.is_protected:
        kept = "protected"
    elif expires is not None and ref.dotted() in deployed:
        kept = "project_asset"
    return TableRetention(
        ref=ref,
        table_id=row.id,
        source=rule.source,
        days=rule.days,
        on=rule.on,
        never=row.retention_never,
        set_by=None if rule.set_by is None else names.get(rule.set_by),
        set_at=rule.set_at,
        last_activity_at=activity,
        expires_at=None if kept is not None else expires,
        kept=kept,
    )


def planned_expiry(row: Table, env: Environment, last_read: datetime | None) -> datetime | None:
    """Compute when a table's retention would make it expire, before any exclusion.

    Parameters
    ----------
    row
        The table.
    env
        Its environment.
    last_read
        Its last recorded read.

    Returns
    -------
    datetime or None
        The expiry; `None` when no rule gives one.
    """
    rule = _rule(row, env)
    return expiry_time(
        days=rule.days, on=rule.on, activity=last_activity(row, last_read), floor=rule.set_at
    )


def retention_candidates(session: Session, env: Environment) -> list[tuple[Table, TableRef]]:
    """List the managed tables of an environment that a retention may make expire.

    Parameters
    ----------
    session
        Database session.
    env
        The environment.

    Returns
    -------
    list of (Table, TableRef)
        Every managed table that does not say `never` and has a rule of its own, or every one
        when the environment has a retention.
    """
    query = (
        select(Table, Namespace.path)
        .join(Namespace, Table.namespace_id == Namespace.id)
        .where(
            Namespace.environment_id == env.id,
            Table.table_type == TableType.ICEBERG,
            Table.retention_never.is_(False),
        )
        .order_by(Namespace.path, Table.name)
    )
    if env.retention_days is None:
        query = query.where(or_(Table.retention_days.is_not(None), Table.retention_on.is_not(None)))
    return [
        (row, TableRef(env.name, NamespacePath.parse(path), row.name))
        for row, path in session.execute(query).all()
    ]


def describe_retentions(
    session: Session, env: Environment, tables: list[tuple[Table, TableRef]]
) -> list[TableRetention]:
    """Describe the retention of several tables of one environment, with few queries.

    Parameters
    ----------
    session
        Database session.
    env
        Their environment.
    tables
        The tables, as `retention_candidates` lists them.

    Returns
    -------
    list of TableRetention
        In the same order.
    """
    reads = last_reads(session, (row.id for row, _ in tables))
    deployed = deployed_assets(session, env.id)
    names = _names(
        session,
        [env.retention_set_by, *(row.retention_set_by for row, _ in tables)],
    )
    return [
        retention_of(row, env, ref, deployed=deployed, last_read=reads.get(row.id), names=names)
        for row, ref in tables
    ]


def describe_retention(
    session: Session, env: Environment, row: Table, ref: TableRef
) -> TableRetention:
    """Describe one managed table's retention.

    Parameters
    ----------
    session
        Database session.
    env
        Its environment.
    row
        The table.
    ref
        Its reference.

    Returns
    -------
    TableRetention
        The retention.
    """
    (found,) = describe_retentions(session, env, [(row, ref)])
    return found


def lock_row(session: Session, model: type[_M], row_id: uuid.UUID) -> _M | None:
    """Take a table's or an environment's row lock for the transaction and read it again.

    A no-op `UPDATE` that keeps `updated_at` (a row lock on Postgres, the write lock on SQLite),
    so a commit or a change of the row's retention waits for the transaction, or comes first
    and is seen.

    Parameters
    ----------
    session
        Database session.
    model
        `Table` or `Environment`.
    row_id
        The row.

    Returns
    -------
    Table or Environment or None
        The row as it is now; `None` when it is gone.
    """
    locked = session.execute(
        update(model)
        .where(model.id == row_id)
        .values(name=model.name, updated_at=model.updated_at)
        .execution_options(synchronize_session=False)
    )
    if rowcount(locked) != 1:
        return None
    return session.scalars(
        select(model).where(model.id == row_id).execution_options(populate_existing=True)
    ).one()


def write_running(session: Session, row: Table, ref: TableRef, environment_id: uuid.UUID) -> bool:
    """Say whether a write to a table is queued or running.

    Parameters
    ----------
    session
        Database session.
    row
        The table.
    ref
        Its reference.
    environment_id
        Its environment.

    Returns
    -------
    bool
        `True` for a queued or running write, view or maintenance query that names the table by
        id or by name, or a pipeline task of the table's name in the environment that has not
        ended.
    """
    dotted = ref.dotted()
    query = (
        select(QueryRun.id)
        .where(
            QueryRun.environment_id == environment_id,
            QueryRun.status.in_([QueryRunStatus.QUEUED, QueryRunStatus.RUNNING]),
            QueryRun.write_mode.is_not(None),
            or_(QueryRun.target_table_id == row.id, QueryRun.target_name == dotted),
        )
        .exists()
    )
    task = (
        select(TaskRun.id)
        .join(Run, TaskRun.run_id == Run.id)
        .where(
            Run.environment_id == environment_id,
            TaskRun.asset == dotted,
            TaskRun.status.not_in(TERMINAL_TASK_STATUSES),
        )
        .exists()
    )
    return bool(session.scalar(select(or_(query, task))))


def owns_live_record(session: Session, row: Table) -> bool:
    """Say whether a table's location record is its own and `LIVE`, so a drop leaves a tombstone.

    Parameters
    ----------
    session
        Database session.
    row
        The table.

    Returns
    -------
    bool
        `True` if the record at the table's root location is `LIVE` and names this table.
    """
    location = (
        None
        if row.metadata_location is None
        else table_root_from_metadata_location(row.metadata_location)
    )
    if location is None:
        return False
    record = session.execute(
        select(TableLocation.state, TableLocation.table_id).where(
            TableLocation.location == location
        )
    ).one_or_none()
    return record is not None and record[0] is TableLocationState.LIVE and record[1] == row.id


def expire_table(
    session: Session,
    table_id: uuid.UUID,
    *,
    environment_id: uuid.UUID,
    now: datetime,
    undo_days: int,
) -> bool | None:
    """Drop one table whose retention has passed, after deciding again under its locks.

    Commits when it drops; rolls back otherwise.

    Parameters
    ----------
    session
        Database session, with no transaction open.
    table_id
        The table.
    environment_id
        Its environment.
    now
        The sweep's time.
    undo_days
        ``LAKANA_TABLES__UNDO_DAYS``; at least `MIN_EXPIRY_UNDO_DAYS` is kept.

    Returns
    -------
    bool or None
        `True` when it was dropped; `False` when it is due but kept for now (a write runs, or
        its location record is not its own `LIVE` one); `None` when it is no longer due, no
        longer exists, or is excluded.
    """
    env = lock_row(session, Environment, environment_id)
    row = None if env is None else lock_row(session, Table, table_id)
    if (
        env is None
        or row is None
        or env.is_protected
        or env.deleted_at is not None
        or row.table_type is not TableType.ICEBERG
    ):
        session.rollback()
        return None
    path = session.scalars(select(Namespace.path).where(Namespace.id == row.namespace_id)).one()
    ref = TableRef(env.name, NamespacePath.parse(path), row.name)
    retention = describe_retention(session, env, row, ref)
    if retention.expires_at is None or retention.expires_at > now:
        session.rollback()
        return None
    if write_running(session, row, ref, env.id) or not owns_live_record(session, row):
        session.rollback()
        return False
    drop_row(
        session,
        None,
        ref,
        row,
        now=now,
        purge_after=now + timedelta(days=max(undo_days, MIN_EXPIRY_UNDO_DAYS)),
        reason="retention",
        detail={
            "retention": {
                "source": retention.source,
                "days": retention.days,
                "on": None if retention.on is None else retention.on.isoformat(),
                "set_by": retention.set_by,
                "last_activity_at": retention.last_activity_at.isoformat(),
                "expires_at": retention.expires_at.isoformat(),
            }
        },
    )
    session.commit()
    return True


@dataclass(frozen=True)
class ExpiryCounts:
    """What one sweep's expiry step did in one environment.

    Parameters
    ----------
    expired
        Tables dropped.
    postponed
        Due tables kept for now: a write runs, or their location record is not their own.
    """

    expired: int = 0
    postponed: int = 0


def expire_due(
    session: Session,
    *,
    environment_id: uuid.UUID,
    now: datetime,
    undo_days: int,
    limit: int,
    tick: Callable[[], None],
    guarded: Callable[[uuid.UUID, Callable[[uuid.UUID], None]], None],
) -> ExpiryCounts:
    """Drop the due tables of one environment, the most overdue first, up to `limit`.

    Parameters
    ----------
    session
        Database session; left without a transaction.
    environment_id
        The environment.
    now
        The sweep's time.
    undo_days
        ``LAKANA_TABLES__UNDO_DAYS``.
    limit
        The most tables to drop.
    tick
        Called before each decision; raises to stop the run at its deadline.
    guarded
        Runs one table's step and turns its failure into a count (the sweep's `_guarded`).

    Returns
    -------
    ExpiryCounts
        What was done.
    """
    env = session.get(Environment, environment_id, populate_existing=True)
    if env is None or env.is_protected or env.deleted_at is not None or limit <= 0:
        session.rollback()
        return ExpiryCounts()
    found = describe_retentions(session, env, retention_candidates(session, env))
    due = sorted(
        (entry for entry in found if entry.expires_at is not None and entry.expires_at <= now),
        key=lambda entry: (entry.expires_at, str(entry.table_id)),
    )
    session.rollback()
    expired = postponed = 0

    def one(table_id: uuid.UUID) -> None:
        """Expire one table and count what happened.

        Parameters
        ----------
        table_id
            The table.
        """
        nonlocal expired, postponed
        outcome = expire_table(
            session, table_id, environment_id=environment_id, now=now, undo_days=undo_days
        )
        if outcome is True:
            expired += 1
        elif outcome is False:
            postponed += 1

    for entry in due:
        if expired >= limit:
            break
        tick()
        guarded(entry.table_id, one)
    return ExpiryCounts(expired=expired, postponed=postponed)
