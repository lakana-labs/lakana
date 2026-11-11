"""The workers of an instance: who registered, who is alive.

Every time written here is the database's (`db_now`), never a worker's wall clock, and "alive"
is only ever "heartbeat younger than the lease" by that same clock, so skew between hosts can
neither keep a dead worker alive nor end the runs of a live one.

None of the functions commits; the caller does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from lakana_core.db import db_now, rowcount
from lakana_core.models import DeployRun, QueryRun, Worker

WORKER_ROW_KEEP_SECONDS = 3600
"""How long the row of a worker that stopped beating is kept (and listed as `lost`)."""


@dataclass(frozen=True)
class CacheSizes:
    """What a worker measured its own caches to be.

    Parameters
    ----------
    environments_bytes
        The Python environments of projects (``venvs``).
    packages_bytes
        uv's package download cache (``uv``).
    tasks_bytes
        The working directories of running and leftover Python tasks (``attempts``).
    complete
        Whether the measurement finished; `False` when its time limit cut it off, and the
        figures are then a floor.
    """

    environments_bytes: int
    packages_bytes: int
    tasks_bytes: int
    complete: bool


def _cache_values(caches: CacheSizes, now: datetime) -> dict[str, object]:
    """Map cache sizes to the columns of the worker's row.

    Parameters
    ----------
    caches
        The sizes.
    now
        The database's time, stored as when they were measured.

    Returns
    -------
    dict of str to object
        By attribute name.
    """
    return {
        "cache_environments_bytes": caches.environments_bytes,
        "cache_packages_bytes": caches.packages_bytes,
        "cache_tasks_bytes": caches.tasks_bytes,
        "cache_complete": caches.complete,
        "cache_measured_at": now,
    }


def cache_sizes(row: Worker) -> CacheSizes | None:
    """Read the cache sizes a worker stored.

    Parameters
    ----------
    row
        The worker's row.

    Returns
    -------
    CacheSizes or None
        `None` before the worker's first measurement.
    """
    if row.cache_measured_at is None:
        return None
    return CacheSizes(
        environments_bytes=row.cache_environments_bytes or 0,
        packages_bytes=row.cache_packages_bytes or 0,
        tasks_bytes=row.cache_tasks_bytes or 0,
        complete=bool(row.cache_complete),
    )


@dataclass(frozen=True)
class MemoryUse:
    """What a worker measured of its memory.

    Parameters
    ----------
    used_bytes
        The resident memory of the worker and of every process it started.
    limit_bytes
        Its limit (`LAKANA_WORKER__MEMORY_LIMIT`, or what it derived from its container's);
        `None` when it has none.
    """

    used_bytes: int
    limit_bytes: int | None


def _memory_values(memory: MemoryUse, now: datetime) -> dict[str, object]:
    """Map a memory use to the columns of the worker's row.

    Parameters
    ----------
    memory
        The use.
    now
        The database's time, stored as when it was measured.

    Returns
    -------
    dict of str to object
        By attribute name.
    """
    return {
        "memory_used_bytes": memory.used_bytes,
        "memory_limit_bytes": memory.limit_bytes,
        "memory_measured_at": now,
    }


def memory_use(row: Worker) -> MemoryUse | None:
    """Read the memory use a worker stored.

    Parameters
    ----------
    row
        The worker's row.

    Returns
    -------
    MemoryUse or None
        `None` before the worker's first report.
    """
    if row.memory_measured_at is None:
        return None
    return MemoryUse(used_bytes=row.memory_used_bytes or 0, limit_bytes=row.memory_limit_bytes)


def _heartbeat_is_younger_than(cutoff: datetime) -> ColumnElement[bool]:
    """Build the one definition of "alive": the heartbeat is younger than the cutoff.

    Parameters
    ----------
    cutoff
        `now - lease`; a heartbeat exactly this old is not alive.

    Returns
    -------
    ColumnElement[bool]
        A condition on `Worker.heartbeat_at`.
    """
    return Worker.heartbeat_at > cutoff


def register(
    session: Session,
    worker_id: str,
    *,
    host: str,
    version: str,
    started_at: datetime | None = None,
    takes: str = "all",
    caches: CacheSizes | None = None,
    memory: MemoryUse | None = None,
) -> datetime:
    """Insert the worker's row, or refresh it.

    Parameters
    ----------
    session
        Database session; the caller commits.
    worker_id
        The worker's id.
    host
        The host name, cut to the column's 255 characters.
    version
        The worker's version, cut to the column's 64 characters.
    started_at
        The start time of a worker that comes back after its row was removed; ignored when the
        row exists. The database's time when omitted.
    takes
        The work the worker takes (`all`, `interactive` or `pipeline`); written on insert and
        on refresh.
    caches
        The sizes of its caches, when it has measured them; a refresh without leaves the
        stored ones.
    memory
        Its memory use, when it has measured it; a refresh without leaves the stored one.

    Returns
    -------
    datetime
        The row's `started_at`.
    """
    now = db_now(session)
    row = session.get(Worker, worker_id)
    if row is None:
        row = Worker(
            id=worker_id,
            host=host[:255],
            version=version[:64],
            started_at=started_at or now,
            heartbeat_at=now,
            takes=takes,
        )
        session.add(row)
    else:
        row.host, row.version, row.heartbeat_at, row.takes = host[:255], version[:64], now, takes
    values = {
        **({} if caches is None else _cache_values(caches, now)),
        **({} if memory is None else _memory_values(memory, now)),
    }
    for key, value in values.items():
        setattr(row, key, value)
    return row.started_at


def beat(
    session: Session,
    worker_id: str,
    caches: CacheSizes | None = None,
    memory: MemoryUse | None = None,
) -> bool:
    """Set the worker's heartbeat to the database's time, and its caches and memory when given.

    The time is read in the same transaction and bound, the way a task lease is renewed, not
    written as `CURRENT_TIMESTAMP`: SQLite's is text without a fraction.

    Parameters
    ----------
    session
        Database session; the caller commits.
    worker_id
        The worker's id.
    caches
        The sizes of its caches it measured since its last beat; `None` leaves the stored ones.
    memory
        Its memory use, when it changed enough to be written; `None` leaves the stored one.

    Returns
    -------
    bool
        `False` when no row matched (it was removed), `True` otherwise.
    """
    now = db_now(session)
    result = session.execute(
        update(Worker)
        .where(Worker.id == worker_id)
        .values(
            heartbeat_at=now,
            **({} if caches is None else _cache_values(caches, now)),
            **({} if memory is None else _memory_values(memory, now)),
        )
        .execution_options(synchronize_session=False)
    )
    return rowcount(result) == 1


def remove(session: Session, worker_id: str) -> None:
    """Delete the worker's row (a clean stop).

    Parameters
    ----------
    session
        Database session; the caller commits.
    worker_id
        The worker's id.
    """
    session.execute(delete(Worker).where(Worker.id == worker_id))


def forget_lost(session: Session, now: datetime, lease_seconds: int = 0) -> int:
    """Delete the rows of workers older than the larger of the keep time and the lease.

    A lease above `WORKER_ROW_KEEP_SECONDS` must not delete the row of a worker that is merely
    stalled and still counts as alive.

    Parameters
    ----------
    session
        Database session; the caller commits.
    now
        The database's current time.
    lease_seconds
        The lease length (`LAKANA_RUN__LEASE_SECONDS`); `0` leaves the keep time alone.

    Returns
    -------
    int
        The number of rows deleted.
    """
    cutoff = now - timedelta(seconds=max(WORKER_ROW_KEEP_SECONDS, lease_seconds))
    return rowcount(session.execute(delete(Worker).where(Worker.heartbeat_at < cutoff)))


def alive_ids(session: Session, now: datetime, lease_seconds: int) -> set[str]:
    """Return the ids of the workers whose heartbeat is younger than the lease.

    Parameters
    ----------
    session
        Database session.
    now
        The database's current time.
    lease_seconds
        The lease length (`LAKANA_RUN__LEASE_SECONDS`).

    Returns
    -------
    set[str]
        The alive workers' ids.
    """
    cutoff = now - timedelta(seconds=lease_seconds)
    return set(session.scalars(select(Worker.id).where(_heartbeat_is_younger_than(cutoff))))


def any_alive(session: Session, now: datetime, lease_seconds: int) -> bool:
    """Say whether at least one worker's heartbeat is younger than the lease.

    Parameters
    ----------
    session
        Database session.
    now
        The database's current time.
    lease_seconds
        The lease length (`LAKANA_RUN__LEASE_SECONDS`).

    Returns
    -------
    bool
        Whether a worker is alive; one indexed statement that reads at most one row.
    """
    cutoff = now - timedelta(seconds=lease_seconds)
    found = session.scalars(select(Worker.id).where(_heartbeat_is_younger_than(cutoff)).limit(1))
    return found.first() is not None


def alive_on_host(session: Session, host: str, now: datetime, lease_seconds: int) -> bool:
    """Say whether a worker that registered from this host is alive.

    Parameters
    ----------
    session
        Database session.
    host
        The host name, as the worker passed it to `register` (cut to 255 characters the same
        way).
    now
        The database's current time.
    lease_seconds
        The lease length (`LAKANA_RUN__LEASE_SECONDS`).

    Returns
    -------
    bool
        Whether a worker of that host has a heartbeat younger than the lease.
    """
    cutoff = now - timedelta(seconds=lease_seconds)
    found = session.scalars(
        select(Worker.id)
        .where(Worker.host == host[:255], _heartbeat_is_younger_than(cutoff))
        .limit(1)
    )
    return found.first() is not None


def alive_takes(session: Session, now: datetime, lease_seconds: int) -> dict[str, str]:
    """Return what each alive worker takes, by worker id, in one statement.

    Parameters
    ----------
    session
        Database session.
    now
        The database's current time.
    lease_seconds
        The lease length (`LAKANA_RUN__LEASE_SECONDS`).

    Returns
    -------
    dict[str, str]
        The ids of the workers whose heartbeat is younger than the lease, each with its `takes`.
    """
    cutoff = now - timedelta(seconds=lease_seconds)
    rows = session.execute(
        select(Worker.id, Worker.takes).where(_heartbeat_is_younger_than(cutoff))
    )
    return {worker_id: takes for worker_id, takes in rows}


def without_alive_worker(
    model: type[QueryRun] | type[DeployRun], now: datetime, lease_seconds: int
) -> ColumnElement[bool]:
    """Build the condition "no alive worker holds this run" for a query or deploy run.

    A run with a worker id is held while that worker's heartbeat is younger than the lease. A
    run without one was claimed by a release that did not record it: it counts as held until it
    started a lease ago. That age compares the run's `started_at`, which a worker stamps with its
    own wall clock at the claim, with a cutoff from the database's clock; a skewed worker clock
    can make such a run look younger or older by the skew, which only moves the end of a run
    that no new release produces.

    There is no status filter: a `QUEUED` run has no worker id and an old `created_at`, so it
    would match. The caller adds `status == RUNNING`.

    Parameters
    ----------
    model
        `QueryRun` or `DeployRun`.
    now
        The database's current time.
    lease_seconds
        The lease length (`LAKANA_RUN__LEASE_SECONDS`).

    Returns
    -------
    ColumnElement[bool]
        A condition to put in a `WHERE` clause.
    """
    cutoff = now - timedelta(seconds=lease_seconds)
    alive = (
        select(Worker.id)
        .where(Worker.id == model.worker_id, _heartbeat_is_younger_than(cutoff))
        .exists()
    )
    return or_(
        and_(model.worker_id.is_not(None), ~alive),
        and_(
            model.worker_id.is_(None),
            func.coalesce(model.started_at, model.created_at) <= cutoff,
        ),
    )
