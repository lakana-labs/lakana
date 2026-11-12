"""The hourly table health sweep: freshness, and the signals past their kept period (#340).

The scheduler's housekeeping (`lakana_worker.__main__`) calls `sweep_health` once per sweep
interval. It takes a slice of at most `MAX_SWEEP_TABLES` tables that have health signal rows, the
next slice on each call (`rotation`), and stops when asked or after `HEALTH_SWEEP_MAX_SECONDS`.

It first observes the freshness of each table of the slice: the time since the table's last
commit that added data (one with a ``volume`` row; a compaction records only ``schema``, so
maintenance cannot hide a stale source), against the schedules of the jobs that build the table,
or its own history (`health_signals.freshness_signal`). A table whose building jobs are
all scheduled and paused records ``off``; one that a job without a schedule also builds is
judged by its history. A table with no such commit recorded since signals exist is not
judged. A row is recorded only when the state, the reason of an ``off`` or the last commit
changed, so a table that stays late or on time adds nothing; a fired row is told to the notify
targets of the building jobs once (`health_jobs.queue_health_notifications`). On Postgres each
table is checked under a transaction-scoped advisory lock (`_lock_table`), skipped when another
sweep holds it.

Then, with the time left, it deletes the slice's rows observed before the kept period
(``LAKANA_HEALTH__RETENTION_DAYS``), at most `RETENTION_BATCH` per statement. The newest row of
each signal and column that no set-back marked is kept however old, so a table's current state
never goes, and so are the ``volume`` rows of the newest `HISTORY` + 1 commits that added data,
which freshness is judged from; a marked row goes once it is older than the kept period.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Integer, Select, and_, cast, delete, exists, func, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from lakana_core.catalog.health_jobs import (
    building_jobs,
    deployed_manifests,
    queue_health_notifications,
    rows_to_tell,
)
from lakana_core.catalog.health_read import newest_freshness
from lakana_core.catalog.health_record import NEWEST_COMMIT, NOT_SET_BACK, signal_rows
from lakana_core.catalog.health_signals import (
    HISTORY,
    HealthEffects,
    Observation,
    data_point_indexes,
    freshness_signal,
    schedule_threshold,
)
from lakana_core.catalog.health_summary import refresh_health_key
from lakana_core.db import rowcount
from lakana_core.identifiers import NamespacePath, shown
from lakana_core.models import JobSchedule, Namespace, Table, TableHealthSignal
from lakana_core.runs.pools import on_postgres

logger = logging.getLogger(__name__)

FRESHNESS_ROW_CAP = 1_000
"""The most ``volume`` rows of one table that freshness and its retention read: the newest
`HISTORY` + 1 data points of a table whose runs commit many times, within a bound."""

MAX_SWEEP_TABLES = 500
"""The most tables one sweep looks at; the next sweep takes the next slice."""

HEALTH_SWEEP_MAX_SECONDS = 60.0
"""How long one sweep may run."""

RETENTION_BATCH = 5_000
"""The most rows one delete statement removes."""

HEALTH_LOCK_KEY = 0x6C6B6873  # "lkhs"; the second key is 32 bits of the table's id
"""The first of the two keys of a table's freshness lock: a key space apart from other locks."""


@dataclass(frozen=True)
class HealthSweep:
    """What one sweep did.

    Parameters
    ----------
    evaluated
        Tables whose freshness was observed.
    recorded
        Freshness rows recorded.
    deleted
        Rows deleted past the kept period.
    """

    evaluated: int
    recorded: int
    deleted: int


@dataclass(frozen=True)
class _Deployed:
    """What an environment's deployments say about the jobs that build its tables.

    Parameters
    ----------
    manifests
        From `deployed_manifests`.
    paused
        (repository id, job) of each paused job.
    """

    manifests: list[tuple[uuid.UUID, object]]
    paused: frozenset[tuple[uuid.UUID, str]]


def _candidates(session: Session, rotation: int) -> list[uuid.UUID]:
    """Pick this sweep's slice of the tables that have health signal rows.

    Parameters
    ----------
    session
        Database session.
    rotation
        How many sweeps this process ran before; each takes the next slice.

    Returns
    -------
    list of uuid.UUID
        At most `MAX_SWEEP_TABLES` table ids, by id, wrapping around to the first ones; no table
        twice.
    """
    has_rows = exists().where(TableHealthSignal.table_id == Table.id)
    total = session.scalar(select(func.count()).select_from(Table).where(has_rows)) or 0
    if not total:
        return []
    offset = (rotation * MAX_SWEEP_TABLES) % total
    ids = select(Table.id).where(has_rows).order_by(Table.id)
    found = list(session.scalars(ids.offset(offset).limit(MAX_SWEEP_TABLES)))
    if len(found) < MAX_SWEEP_TABLES and offset:
        found += session.scalars(ids.limit(min(MAX_SWEEP_TABLES - len(found), offset)))
    return found


def _data_commits(table_id: uuid.UUID) -> Select[uuid.UUID, datetime, int | None]:
    """Select a table's newest commits that added data, which freshness is judged from.

    A commit that adds data records a ``volume`` row; a compaction records only ``schema``.

    Parameters
    ----------
    table_id
        The table.

    Returns
    -------
    Select
        ``id``, ``observed_at`` and ``snapshot_id`` of the newest `FRESHNESS_ROW_CAP` ``volume``
        rows that no set-back marked, newest first (`NEWEST_COMMIT`): enough for `HISTORY` + 1
        data points of a table whose runs commit several times (`data_point_indexes`).
    """
    return (
        select(TableHealthSignal.id, TableHealthSignal.observed_at, TableHealthSignal.snapshot_id)
        .where(
            TableHealthSignal.table_id == table_id,
            TableHealthSignal.signal == "volume",
            TableHealthSignal.sequence_number.is_not(None),
            NOT_SET_BACK,
        )
        .order_by(*NEWEST_COMMIT)
        .limit(FRESHNESS_ROW_CAP)
    )


def _lock_table(session: Session, table_id: uuid.UUID) -> bool:
    """Try to take a table's freshness lock for the transaction; nothing off Postgres.

    Housekeeping runs on the leader only, but a leader that lost its role finishes its sweep: two
    sweeps of one table would both find no previous row and both record and tell it. The lock
    ends with the transaction that records the table's row. Two tables whose ids share 32 bits
    skip each other for that moment, no more. SQLite has one process.

    Parameters
    ----------
    session
        Database session; the lock is held until its transaction ends.
    table_id
        The table.

    Returns
    -------
    bool
        Whether this sweep may check the table (always off Postgres).
    """
    if not on_postgres(session):
        return True
    key = int.from_bytes(table_id.bytes[:4], "big", signed=True)
    lock = func.pg_try_advisory_xact_lock(
        cast(literal(HEALTH_LOCK_KEY), Integer), cast(literal(key), Integer)
    )
    return bool(session.scalar(select(lock)))


def _delete_old(
    session: Session, table_id: uuid.UUID, cutoff: datetime, stopped: Callable[[], bool]
) -> int:
    """Delete a table's rows from before the kept period, but its current state.

    A row goes when it is older than `cutoff` and either a set-back marked it, or a newer row of
    the same signal and column that no set-back marked exists. The first commit of each of the
    newest `HISTORY` + 1 data points (`data_point_indexes` of `_data_commits`) keeps its
    ``volume`` row however old, so the freshness of a table that stopped being written keeps its
    history. Commits after each batch.

    Parameters
    ----------
    session
        Database session; committed here.
    table_id
        The table.
    cutoff
        Rows observed before this go.
    stopped
        Asked after each full batch.

    Returns
    -------
    int
        How many rows were deleted.
    """
    old, newer = aliased(TableHealthSignal), aliased(TableHealthSignal)
    superseded = exists().where(
        newer.table_id == old.table_id,
        newer.signal == old.signal,
        or_(
            newer.column_name == old.column_name,
            and_(newer.column_name.is_(None), old.column_name.is_(None)),
        ),
        newer.set_back_at.is_(None),
        or_(
            newer.observed_at > old.observed_at,
            and_(newer.observed_at == old.observed_at, newer.id > old.id),
        ),
    )
    commits = session.execute(_data_commits(table_id)).all()
    points = data_point_indexes([at for _, at, _ in commits])[: HISTORY + 1]
    spared = [commits[index][0] for index in points]
    batch = (
        select(old.id)
        .where(
            old.table_id == table_id,
            old.observed_at < cutoff,
            or_(old.set_back_at.is_not(None), superseded),
            old.id.not_in(spared),
        )
        .limit(RETENTION_BATCH)
    )
    deleted = 0
    while True:
        gone = rowcount(
            session.execute(
                delete(TableHealthSignal)
                .where(TableHealthSignal.id.in_(batch))
                .execution_options(synchronize_session=False)
            )
        )
        session.commit()
        deleted += gone
        if gone < RETENTION_BATCH or stopped():
            return deleted


def _deployed(session: Session, environment_id: uuid.UUID) -> _Deployed:
    """Read an environment's current manifests and paused jobs.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.

    Returns
    -------
    _Deployed
        Both.
    """
    paused = session.execute(
        select(JobSchedule.repository_id, JobSchedule.job).where(
            JobSchedule.environment_id == environment_id, JobSchedule.paused.is_(True)
        )
    )
    return _Deployed(
        deployed_manifests(session, environment_id),
        frozenset((repository_id, job) for repository_id, job in paused),
    )


def _thresholds(
    deployed: _Deployed, table: str, last_commit_at: datetime
) -> tuple[list[tuple[float, str]], bool]:
    """Find the limits that the schedules of a table's building jobs set.

    Parameters
    ----------
    deployed
        The environment's deployments.
    table
        The dotted table name.
    last_commit_at
        The table's last commit.

    Returns
    -------
    tuple
        (seconds, job) of each scheduled job that is not paused and whose schedule fires twice
        (empty falls back to the table's history), and whether there are building jobs and every
        one of them is scheduled and paused.
    """
    building = building_jobs(deployed.manifests, table)
    scheduled = [(job, job.schedule) for job in building if job.schedule is not None]
    running = [
        (job.job, schedule)
        for job, schedule in scheduled
        if (job.repository_id, job.job) not in deployed.paused
    ]
    found = []
    for name, (cron, zone) in running:
        limit = schedule_threshold(cron, zone, last_commit_at)
        if limit is not None:
            found.append((limit, name))
    # A job without a schedule can write the table at any time: never paused then.
    return found, bool(scheduled) and len(scheduled) == len(building) and not running


def _check_freshness(
    session: Session,
    *,
    table_id: uuid.UUID,
    environment_id: uuid.UUID,
    table: str,
    effects: HealthEffects,
    now: datetime,
    deployments: dict[uuid.UUID, _Deployed],
) -> bool | None:
    """Observe one table's freshness and record it when it changed.

    Parameters
    ----------
    session
        Database session; rows are added, not committed.
    table_id
        The table.
    environment_id
        Its environment.
    table
        Its dotted name.
    effects
        Its effects.
    now
        The sweep's time.
    deployments
        The environments' deployments read so far, by id; filled in here.

    Returns
    -------
    bool or None
        Whether a row was recorded; `None` for a table without a commit to judge.
    """
    commits: Sequence[tuple[uuid.UUID, datetime, int | None]] = session.execute(
        _data_commits(table_id)
    ).all()
    if not commits:
        return None
    _, last_commit_at, snapshot_id = commits[0]
    effect = effects.of("freshness")
    deployed = None
    if effect == "off":
        observation = Observation("freshness", None, "off", None, None)
    else:
        deployed = deployments.get(environment_id) or _deployed(session, environment_id)
        deployments[environment_id] = deployed
        thresholds, paused = _thresholds(deployed, table, last_commit_at)
        observation = freshness_signal(
            now=now,
            commit_times=[at for _, at, _ in commits],
            thresholds=thresholds,
            paused=paused,
        )
    previous = newest_freshness(session, table_id)
    if (
        previous is not None
        and previous.state == observation.state
        and previous.snapshot_id == snapshot_id
        and (observation.state != "off" or previous.observed == observation.observed)
    ):
        return False
    # The table's row first, as every write that changes its stored word takes it: a commit and a
    # check result lock it before they add rows that refer to it.
    session.execute(select(Table.id).where(Table.id == table_id).with_for_update())
    rows = signal_rows(
        [(observation, effect)],
        environment_id=environment_id,
        table_id=table_id,
        snapshot_id=snapshot_id,
        sequence_number=None,
        now=now,
    )
    session.add_all(rows)
    session.flush()
    refresh_health_key(session, table_id)
    session.add_all(
        queue_health_notifications(
            session,
            environment_id=environment_id,
            table=table,
            rows=rows_to_tell(rows),
            now=now,
            manifests=None if deployed is None else deployed.manifests,
        )
    )
    return True


def sweep_health(
    session: Session,
    *,
    now: datetime,
    retention_days: int,
    rotation: int,
    should_stop: Callable[[], bool],
) -> HealthSweep:
    """Delete the health signals past their kept period and observe freshness, for one slice.

    Parameters
    ----------
    session
        Database session; committed after each batch and each table.
    now
        The scheduler step's time.
    retention_days
        The kept period (``LAKANA_HEALTH__RETENTION_DAYS``).
    rotation
        How many sweeps this process ran before; picks the slice of tables.
    should_stop
        Set at shutdown; asked before each table and after each full batch.

    Returns
    -------
    HealthSweep
        What it did.

    Raises
    ------
    sqlalchemy.exc.SQLAlchemyError
        If the database failed; the housekeeping logs it. Any other error of one table's
        freshness is logged and the table skipped.
    """
    deadline = time.monotonic() + HEALTH_SWEEP_MAX_SECONDS

    def stopped() -> bool:
        """Say whether the sweep must end now.

        Returns
        -------
        bool
            When asked to stop, or past the deadline.
        """
        return should_stop() or time.monotonic() > deadline

    candidates = _candidates(session, rotation)
    evaluated = recorded = 0
    deployments: dict[uuid.UUID, _Deployed] = {}
    for table_id in candidates:
        if stopped():
            break
        found = session.execute(
            select(Table.name, Table.health_effects, Namespace.path, Namespace.environment_id)
            .join(Namespace, Namespace.id == Table.namespace_id)
            .where(Table.id == table_id)
        ).one_or_none()
        if found is None:
            continue
        name, raw_effects, path, environment_id = found
        table = f"{NamespacePath.parse(path)}.{name}"
        if not _lock_table(session, table_id):
            session.rollback()
            continue
        try:
            outcome = _check_freshness(
                session,
                table_id=table_id,
                environment_id=environment_id,
                table=table,
                effects=HealthEffects.from_flat(raw_effects),
                now=now,
                deployments=deployments,
            )
        except SQLAlchemyError:
            raise
        except Exception as exc:  # one table must not stop the others
            session.rollback()
            logger.warning(
                "The freshness of %s could not be checked (%s).",
                shown(table),
                type(exc).__name__,
                exc_info=True,
            )
            continue
        session.commit()
        evaluated += outcome is not None
        recorded += bool(outcome)
    cutoff = now - timedelta(days=retention_days)
    deleted = 0
    for table_id in candidates:
        if stopped():
            break
        deleted += _delete_old(session, table_id, cutoff, stopped)
    return HealthSweep(evaluated=evaluated, recorded=recorded, deleted=deleted)
