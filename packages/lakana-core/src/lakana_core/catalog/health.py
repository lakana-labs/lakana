"""A table's health: the numbers of its snapshot, its last maintenance, its checks and its readers.

Everything here is read from rows the platform already keeps, except the reads: the worker adds
one to a table's count for the day each time a query that read it succeeds
(`record_table_reads`), and `sweep_table_reads` removes the days past
``LAKANA_TABLE_READS__KEEP_DAYS``. A Python task reads its inputs through the query API, so its
reads are counted the same way.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pyiceberg.table.metadata import TableMetadata
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from lakana_core.catalog.identity import resolve_table_id
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath
from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    Principal,
    QueryRun,
    QueryRunStatus,
    RollbackOutcome,
    TableRead,
    WriteMode,
)

__all__ = [
    "CheckHealth",
    "LastMaintenance",
    "Reader",
    "SnapshotStats",
    "TableHealth",
    "last_maintenance",
    "latest_checks",
    "readers",
    "record_table_reads",
    "snapshot_stats",
    "sweep_table_reads",
]


@dataclass(frozen=True)
class SnapshotStats:
    """What the current snapshot of a table says about it.

    Parameters
    ----------
    last_write_at
        When the snapshot was committed; `None` without a snapshot.
    row_count
        The rows of the table (``total-records``); `None` when the summary does not say.
    size_bytes
        The size of its data files (``total-files-size``); `None` when the summary does not say.
    file_count
        Its data files (``total-data-files``); `None` when the summary does not say.
    """

    last_write_at: datetime | None
    row_count: int | None
    size_bytes: int | None
    file_count: int | None


def _whole_number(value: object) -> int | None:
    """Read a summary entry as a count.

    The summary is written by the committing client, so it is not trusted to be a number.

    Parameters
    ----------
    value
        The entry.

    Returns
    -------
    int or None
        The value as a whole number of zero or more; `None` for a missing, non-numeric or
        negative entry.
    """
    try:
        number = int(str(value))
    except ValueError:
        return None
    return number if number >= 0 else None


def snapshot_stats(metadata: TableMetadata) -> SnapshotStats:
    """Read the numbers a table's current snapshot carries.

    Parameters
    ----------
    metadata
        The table's current metadata.

    Returns
    -------
    SnapshotStats
        All `None` for a table without a snapshot.
    """
    snapshot = (
        None
        if metadata.current_snapshot_id is None
        else metadata.snapshot_by_id(metadata.current_snapshot_id)
    )
    if snapshot is None:
        return SnapshotStats(None, None, None, None)
    summary: Mapping[str, Any] = {} if snapshot.summary is None else snapshot.summary
    try:
        last_write_at = datetime.fromtimestamp(snapshot.timestamp_ms / 1000, tz=UTC)
    except (ValueError, OverflowError, OSError):
        last_write_at = None
    return SnapshotStats(
        last_write_at=last_write_at,
        row_count=_whole_number(summary.get("total-records")),
        size_bytes=_whole_number(summary.get("total-files-size")),
        file_count=_whole_number(summary.get("total-data-files")),
    )


@dataclass(frozen=True)
class LastMaintenance:
    """The last maintenance run that changed a table.

    Parameters
    ----------
    run_id
        The query run that did it.
    finished_at
        When it ended.
    status
        ``complete``, or ``stopped`` when it ran past its time limit and stopped at a safe point
        (running maintenance again continues).
    snapshots_expired
        How many snapshots it expired.
    files_deleted
        How many orphaned files it deleted.
    bytes_deleted
        Their total size.
    tags_removed
        How many tags of failed checks it removed.
    """

    run_id: uuid.UUID
    finished_at: datetime
    status: str
    snapshots_expired: int
    files_deleted: int
    bytes_deleted: int
    tags_removed: int


def _count(report: Mapping[str, Any], key: str) -> int:
    """Read a count from a stored maintenance report.

    Parameters
    ----------
    report
        The report.
    key
        The entry; a list counts its items.

    Returns
    -------
    int
        The count; 0 when the entry is missing.
    """
    value = report.get(key, 0)
    return len(value) if isinstance(value, list) else int(value or 0)


def last_maintenance(session: Session, table_id: uuid.UUID) -> LastMaintenance | None:
    """Find the newest maintenance run of a table that changed something.

    A dry run changed nothing, a run that ended on a conflict applied nothing, and a run that
    failed for any other cause is not maintenance either. A run that stopped at its time limit
    did remove what its report says, so it counts, as ``stopped``.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.

    Returns
    -------
    LastMaintenance or None
        The newest such run by end time; `None` if there is none.
    """
    runs = session.execute(
        select(QueryRun.id, QueryRun.status, QueryRun.finished_at, QueryRun.maintenance)
        .where(
            QueryRun.write_mode == WriteMode.MAINTAIN,
            QueryRun.target_table_id == table_id,
            QueryRun.finished_at.is_not(None),
            QueryRun.status.in_([QueryRunStatus.SUCCEEDED, QueryRunStatus.FAILED]),
        )
        .order_by(QueryRun.finished_at.desc(), QueryRun.id)
        .execution_options(yield_per=50)
    )
    for run_id, status, finished_at, maintenance in runs:
        report: Mapping[str, Any] = (maintenance or {}).get("report") or {}
        stopped = bool(report.get("stopped"))
        if not report or report.get("dry_run") or report.get("conflict"):
            continue
        if status is QueryRunStatus.SUCCEEDED or stopped:
            return LastMaintenance(
                run_id=run_id,
                finished_at=cast("datetime", finished_at),
                status="stopped" if stopped else "complete",
                snapshots_expired=_count(report, "snapshots_expired"),
                files_deleted=_count(report, "files_deleted"),
                bytes_deleted=_count(report, "bytes_deleted"),
                tags_removed=_count(report, "tags_removed"),
            )
    return None


@dataclass(frozen=True)
class CheckHealth:
    """The newest result of one check of a table.

    Parameters
    ----------
    name
        ``<table>/<check>``.
    check
        The check's short name.
    run_id
        The run it belongs to.
    outcome
        ``passed``, ``failed``, ``warned`` or ``errored``.
    on_failure
        ``block``, ``keep`` or ``warn``.
    failing_rows
        The count of rows that are wrong, when the check ran.
    error
        Why the check could not run.
    rollback_outcome
        What the rollback after a failed blocking check did, if there was one.
    kept
        The check failed or errored and its `keep` setting left the data in the table.
    not_published
        The rollback left the table not published.
    finished_at
        When the check ended.
    generated
        Whether the check was generated from the asset's header (`key:`, `references:`)
        rather than written under `checks/`.
    """

    name: str
    check: str
    run_id: uuid.UUID
    outcome: str
    on_failure: str
    failing_rows: int | None
    error: str | None
    rollback_outcome: str | None
    kept: bool
    not_published: bool
    finished_at: datetime
    generated: bool


def latest_checks(session: Session, table_id: uuid.UUID) -> list[CheckHealth]:
    """List the newest result of each check of a table.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.

    Returns
    -------
    list of CheckHealth
        One per check, by check name. Of two results that ended together, the one with the
        greater id.
    """
    # Only the newest row of each check is read, not the table's whole check history.
    ranked = (
        select(
            CheckResult.id,
            func.row_number()
            .over(
                partition_by=CheckResult.check_name,
                order_by=(CheckResult.finished_at.desc(), CheckResult.id.desc()),
            )
            .label("position"),
        )
        .where(CheckResult.table_id == table_id)
        .subquery()
    )
    rows = session.scalars(
        select(CheckResult)
        .join(ranked, ranked.c.id == CheckResult.id)
        .where(ranked.c.position == 1)
        .execution_options(populate_existing=True)
    )
    newest = {row.check_name: row for row in rows}
    return [
        CheckHealth(
            name=row.check_name,
            check=row.check_name.rpartition("/")[2],
            run_id=row.run_id,
            outcome=row.outcome.value,
            on_failure=row.on_failure,
            failing_rows=row.failing_rows,
            error=row.error,
            rollback_outcome=None if row.rollback_outcome is None else row.rollback_outcome.value,
            kept=row.on_failure == "keep"
            and row.outcome in {CheckOutcome.FAILED, CheckOutcome.ERRORED},
            not_published=row.rollback_outcome is RollbackOutcome.UNPUBLISHED,
            finished_at=row.finished_at,
            generated=row.generated,
        )
        for _, row in sorted(newest.items())
    ]


@dataclass(frozen=True)
class Reader:
    """A principal's use of a table over a window.

    Parameters
    ----------
    principal
        The principal's name.
    read_count
        How many of its succeeded queries named the table.
    last_read_at
        When the last of them completed.
    """

    principal: str
    read_count: int
    last_read_at: datetime


def readers(session: Session, table_id: uuid.UUID, *, days: int, now: datetime) -> list[Reader]:
    """List who read a table in the last `days` UTC days, today included.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    days
        The length of the window in days, at least 1.
    now
        The present time.

    Returns
    -------
    list of Reader
        Most reads first, then by name.
    """
    first_day = now.astimezone(UTC).date() - timedelta(days=days - 1)
    total = func.sum(TableRead.read_count)
    rows = session.execute(
        select(Principal.name, total, func.max(TableRead.last_read_at))
        .join(Principal, Principal.id == TableRead.principal_id)
        .where(TableRead.table_id == table_id, TableRead.day >= first_day)
        .group_by(Principal.id, Principal.name)
        .order_by(total.desc(), Principal.name)
    ).all()
    return [Reader(name, int(count), last) for name, count, last in rows]


@dataclass(frozen=True)
class TableHealth:
    """What `CatalogService.table_health` returns.

    Parameters
    ----------
    days
        The window the readers cover.
    last_maintenance
        The last maintenance that changed the table, or `None`.
    checks
        The newest result of each check the caller may see.
    readers
        Who read the table; `None` for a caller who may not see the table's grants.
    hidden_checks
        How many results were left out of `checks`: generated reference checks whose target the
        caller may not read.
    """

    days: int
    last_maintenance: LastMaintenance | None
    checks: list[CheckHealth]
    readers: list[Reader] | None
    hidden_checks: int = 0


def record_table_reads(
    session: Session,
    *,
    environment: str,
    principal_id: uuid.UUID,
    dotted_refs: list[str],
    now: datetime,
) -> None:
    """Add one read, for today, to each table a succeeded query named.

    The caller commits, in the transaction that completes the query. A table counts once per
    query however often the query names it. A name that is not ``namespace.table`` or matches
    no table of the environment counts nothing.

    Parameters
    ----------
    session
        Database session; nothing is committed.
    environment
        The environment the query ran in.
    principal_id
        The principal that submitted the query.
    dotted_refs
        The dotted table names the query's text names (the ``tables_touched`` of its
        ``QUERY_COMPLETED`` event).
    now
        The present time.
    """
    table_ids: set[uuid.UUID] = set()
    for dotted in dotted_refs:
        *namespace, name = dotted.split(".")
        if not namespace:
            continue
        try:
            joined = NamespacePath.from_parts(namespace).joined()
        except ValidationError:
            continue
        table_id = resolve_table_id(session, environment, joined, name)
        if table_id is not None:
            table_ids.add(table_id)
    if not table_ids:
        return
    insert = postgresql_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    for table_id in sorted(table_ids):
        statement = insert(TableRead).values(
            id=uuid.uuid4(),
            table_id=table_id,
            principal_id=principal_id,
            day=now.astimezone(UTC).date(),
            read_count=1,
            last_read_at=now,
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["table_id", "principal_id", "day"],
                set_={
                    "read_count": TableRead.read_count + 1,
                    "last_read_at": statement.excluded.last_read_at,
                },
            )
        )


def sweep_table_reads(session: Session, *, keep_days: int, now: datetime) -> int:
    """Delete the counts of days that are past the kept period.

    The `keep_days` UTC days up to and including today stay.

    Parameters
    ----------
    session
        Database session; commits.
    keep_days
        The kept period in days (``LAKANA_TABLE_READS__KEEP_DAYS``).
    now
        The present time.

    Returns
    -------
    int
        How many rows were deleted.
    """
    first_kept = now.astimezone(UTC).date() - timedelta(days=keep_days - 1)
    deleted = session.execute(
        delete(TableRead).where(TableRead.day < first_kept).returning(TableRead.id)
    ).all()
    session.commit()
    return len(deleted)
