"""The instance status Home and `lakana status` show: healthy, degraded or down, and why.

The status is derived, never stored: from the readiness checks the server already runs and
caches (`database`, `storage`), and from the workers' rows (alive or lost, what each is doing,
the memory each last reported). `status_from` is pure; `RunService.instance_status` reads the
workers and asks who the caller is.

Everyone sees the state. Only an administrator (who may manage environments on the instance,
as for `lakana worker list`) sees every cause and the detail; anyone else sees the causes that
change what they can do: that no worker runs, or that one of them is lost.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from lakana_core.runs.service import WorkerView

MEMORY_HIGH_SHARE = 0.9
"""The share of its limit at which an alive worker's memory makes the instance degraded."""

SLOTS_BY_TAKES = {"all": 2, "interactive": 1, "pipeline": 1}
"""The task slots of a worker by what it takes: an interactive one and a pipeline one.

An interactive slot is busy while the worker runs an ad-hoc query (a pipeline SQL task's query
belongs to its task, not to this slot); a pipeline slot while it holds a task or runs a deploy.
A worker that takes a kind this release does not know has one slot.
"""

Cause = Literal["database", "storage", "no_worker", "worker_lost", "memory_high"]
"""Why the instance is not healthy.

- `database`: the catalog database does not answer (down).
- `storage`: the storage of a checked environment does not answer (down).
- `no_worker`: no worker is alive (down).
- `worker_lost`: a worker stopped reporting while another is alive (degraded).
- `memory_high`: an alive worker uses `MEMORY_HIGH_SHARE` or more of its limit (degraded).
"""

_DOWN: frozenset[Cause] = frozenset({"database", "storage", "no_worker"})
_EVERYONE: frozenset[Cause] = frozenset({"no_worker", "worker_lost"})
"""The causes a caller who is no administrator sees, and the only ones that set their state."""


@dataclass(frozen=True)
class StatusDetail:
    """What an administrator sees beyond the state: compute, memory and the workers.

    Parameters
    ----------
    slots_busy
        The busy task slots of the alive workers.
    slots_total
        All task slots of the alive workers (`SLOTS_BY_TAKES`).
    memory_used_bytes
        The memory the alive workers last reported, added up.
    memory_limit_bytes
        Their limits, added up; `None` when an alive worker reports no limit or no memory yet,
        or when none is alive.
    workers
        Every registered worker, alive and lost (replaced ones too, marked `replaced`), by id.
    """

    slots_busy: int
    slots_total: int
    memory_used_bytes: int
    memory_limit_bytes: int | None
    workers: tuple[WorkerView, ...]


@dataclass(frozen=True)
class InstanceStatus:
    """Whether the instance works, as one caller may see it.

    Parameters
    ----------
    state
        `down` (nothing gets done), `degraded` (work gets done, slower or at risk) or `healthy`.
    causes
        Why, in the order of `Cause`; empty when healthy, and empty for a caller who is no
        administrator when only an administrator's cause applies.
    workers_alive
        The workers whose heartbeat is younger than the lease.
    workers_total
        Every registered worker, alive and lost, except lost ones a restart on their host
        replaced (`replaced_ids`).
    detail
        Compute, memory and the workers; `None` unless the caller is an administrator.
    """

    state: Literal["healthy", "degraded", "down"]
    causes: tuple[Cause, ...]
    workers_alive: int
    workers_total: int
    detail: StatusDetail | None


def replaced_ids(views: Sequence[WorkerView]) -> frozenset[str]:
    """Find the lost workers that a restart on their host replaced.

    A worker that exits without a clean stop (its memory watchdog ended it, it crashed, its
    container was killed) leaves its row, shown as lost for an hour, and comes back with a new id.
    A lost worker counts as replaced when an alive worker on the same host started after the
    lost one's last heartbeat: nothing is missing, so the status does not count it as down.

    Parameters
    ----------
    views
        Every registered worker.

    Returns
    -------
    frozenset of str
        The ids of the replaced lost workers.
    """
    return frozenset(
        lost.id
        for lost in views
        if lost.status != "alive"
        and any(
            other.status == "alive"
            and other.host == lost.host
            and other.started_at > lost.heartbeat_at
            for other in views
        )
    )


def _causes(failed: Sequence[str], alive: Sequence[WorkerView], lost: int) -> tuple[Cause, ...]:
    """Find every cause, in the order of `Cause`.

    Parameters
    ----------
    failed
        The failed readiness checks. With `database` the storage was not checked, so it is not
        named; the readiness check's `worker` is ignored: the workers' rows say more.
    alive
        The alive workers.
    lost
        How many workers are lost and not replaced.

    Returns
    -------
    tuple of Cause
        The causes found.
    """
    found: list[Cause] = []
    if "database" in failed:
        found.append("database")
    elif "storage" in failed:
        found.append("storage")
    if not alive:
        found.append("no_worker")
    elif lost:
        found.append("worker_lost")
    if any(_memory_high(view) for view in alive):
        found.append("memory_high")
    return tuple(found)


def _memory_high(view: WorkerView) -> bool:
    """Say whether a worker uses `MEMORY_HIGH_SHARE` or more of its limit.

    Parameters
    ----------
    view
        The worker.

    Returns
    -------
    bool
        `False` without a report or without a limit.
    """
    memory = view.memory
    if memory is None or memory.limit_bytes is None:
        return False
    return memory.used_bytes >= memory.limit_bytes * MEMORY_HIGH_SHARE


def _busy_slots(view: WorkerView) -> int:
    """Count the busy task slots of one worker.

    Parameters
    ----------
    view
        The worker.

    Returns
    -------
    int
        At most its slots by `SLOTS_BY_TAKES`.
    """
    interactive = view.query_run_is_adhoc
    pipeline = view.task_run_id is not None or view.deploy_run_id is not None
    if view.takes == "interactive":
        return int(interactive)
    if view.takes == "pipeline":
        return int(pipeline)
    if view.takes == "all":
        return int(interactive) + int(pipeline)
    return int(interactive or pipeline)


def _detail(views: Sequence[WorkerView], alive: Sequence[WorkerView]) -> StatusDetail:
    """Add up compute and memory over the alive workers.

    Parameters
    ----------
    views
        Every worker.
    alive
        The alive ones.

    Returns
    -------
    StatusDetail
        The administrator's detail.
    """
    limits = [None if v.memory is None else v.memory.limit_bytes for v in alive]
    known = [limit for limit in limits if limit is not None]
    return StatusDetail(
        slots_busy=sum(_busy_slots(v) for v in alive),
        slots_total=sum(SLOTS_BY_TAKES.get(v.takes, 1) for v in alive),
        memory_used_bytes=sum(v.memory.used_bytes for v in alive if v.memory is not None),
        memory_limit_bytes=sum(known) if known and len(known) == len(limits) else None,
        workers=tuple(views),
    )


def status_from(
    failed: Sequence[str], views: Sequence[WorkerView], *, admin: bool
) -> InstanceStatus:
    """Derive the instance status from the failed readiness checks and the workers.

    Down for `database`, `storage` or no alive worker; degraded for a lost worker beside an
    alive one or an alive worker at or above `MEMORY_HIGH_SHARE` of its limit; healthy
    otherwise. A lost worker that a restart on its host replaced (`replaced_ids`) is neither
    counted nor a cause; the administrator's detail still lists it. A caller who is no
    administrator gets the state of the causes they may see, except that a failed database or
    storage keeps it down without naming the cause.

    Parameters
    ----------
    failed
        The readiness checks that fail (`database`, `storage`, `worker`).
    views
        Every registered worker (`RunService` reads them).
    admin
        Whether the caller may see every cause and the detail.

    Returns
    -------
    InstanceStatus
        The status for this caller.
    """
    alive = [v for v in views if v.status == "alive"]
    counted_total = len(views) - len(replaced_ids(views))
    causes = _causes(failed, alive, counted_total - len(alive))
    shown = causes if admin else tuple(c for c in causes if c in _EVERYONE)
    counted = causes if admin else tuple(c for c in causes if c != "memory_high")
    state: Literal["healthy", "degraded", "down"] = (
        "down" if _DOWN & set(counted) else "degraded" if counted else "healthy"
    )
    return InstanceStatus(
        state=state,
        causes=shown,
        workers_alive=len(alive),
        workers_total=counted_total,
        detail=_detail(views, alive) if admin else None,
    )
