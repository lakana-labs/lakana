"""Tests for the instance status Home shows: state, causes and the administrator's detail."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from lakana_core.runs.instance_status import (
    MEMORY_HIGH_SHARE,
    SLOTS_BY_TAKES,
    status_from,
)
from lakana_core.runs.service import WorkerView
from lakana_core.runs.workers import MemoryUse

_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_GIB = 1024**3


def alive(
    worker_id: str,
    *,
    status: str = "alive",
    takes: str = "all",
    query: bool = False,
    adhoc: bool = True,
    task: bool = False,
    deploy: bool = False,
    memory: MemoryUse | None = None,
) -> WorkerView:
    """Build the view of a worker, alive unless said otherwise.

    Parameters
    ----------
    worker_id
        The worker's id.
    status
        `alive`, or `lost` for a lost worker.
    takes
        What it takes.
    query
        Whether it runs a query.
    adhoc
        Whether that query is ad hoc (not a pipeline SQL task).
    task
        Whether it holds a task's lease.
    deploy
        Whether it runs a deploy.
    memory
        What it reported of its memory.

    Returns
    -------
    WorkerView
        The view.
    """
    return WorkerView(
        id=worker_id,
        host="box",
        version="1.0",
        started_at=_NOW,
        heartbeat_at=_NOW,
        status=status,
        takes=takes,
        query_run_id=uuid.uuid4() if query else None,
        deploy_run_id=uuid.uuid4() if deploy else None,
        task_run_id=uuid.uuid4() if task else None,
        query_run_is_adhoc=query and adhoc,
        memory=memory,
        memory_measured_at=None if memory is None else _NOW,
    )


def lost(worker_id: str) -> WorkerView:
    """Build the view of a lost worker.

    Parameters
    ----------
    worker_id
        The worker's id.

    Returns
    -------
    WorkerView
        The view.
    """
    return alive(worker_id, status="lost")


def test_healthy_with_live_workers_and_ready_checks() -> None:
    for admin in (False, True):
        s = status_from([], [alive("w1")], admin=admin)
        assert (s.state, s.causes, s.workers_alive, s.workers_total) == ("healthy", (), 1, 1)


def test_database_failure_is_down_and_named_for_admins_only() -> None:
    s = status_from(["database"], [alive("w1")], admin=False)
    assert (s.state, s.causes, s.detail) == ("down", (), None)
    assert status_from(["database"], [alive("w1")], admin=True).causes == ("database",)


def test_a_database_failure_does_not_name_the_storage_it_could_not_check() -> None:
    failed = ["database", "storage", "worker"]
    assert status_from(failed, [alive("w1")], admin=True).causes == ("database",)


def test_storage_failure_is_down_and_named_for_admins_only() -> None:
    assert status_from(["storage"], [alive("w1")], admin=False).state == "down"
    s = status_from(["storage"], [alive("w1")], admin=True)
    assert (s.state, s.causes) == ("down", ("storage",))


def test_no_alive_worker_is_down_for_everyone() -> None:
    for views in ([], [lost("w1")]):
        for admin in (False, True):
            s = status_from(["worker"], views, admin=admin)
            assert (s.state, s.causes) == ("down", ("no_worker",))
            assert s.workers_alive == 0


def test_a_lost_worker_beside_a_live_one_is_degraded() -> None:
    s = status_from([], [alive("w1"), lost("w2")], admin=False)
    assert (s.state, s.causes, s.workers_alive, s.workers_total) == (
        "degraded",
        ("worker_lost",),
        1,
        2,
    )
    assert status_from([], [alive("w1"), lost("w2")], admin=True).causes == ("worker_lost",)


def test_memory_at_ninety_percent_is_degraded_for_admins_only() -> None:
    limit = 10 * _GIB
    high = MemoryUse(used_bytes=int(limit * MEMORY_HIGH_SHARE), limit_bytes=limit)
    s = status_from([], [alive("w1", memory=high)], admin=True)
    assert (s.state, s.causes) == ("degraded", ("memory_high",))
    other = status_from([], [alive("w1", memory=high)], admin=False)
    assert (other.state, other.causes) == ("healthy", ())
    below = MemoryUse(used_bytes=int(limit * MEMORY_HIGH_SHARE) - 1, limit_bytes=limit)
    assert status_from([], [alive("w1", memory=below)], admin=True).state == "healthy"


def test_a_lost_workers_memory_does_not_count() -> None:
    lost_high = alive("w2", status="lost", memory=MemoryUse(used_bytes=10, limit_bytes=10))
    s = status_from([], [alive("w1"), lost_high], admin=True)
    assert s.causes == ("worker_lost",)
    assert s.detail is not None and s.detail.memory_used_bytes == 0


def test_every_cause_is_named_in_order_for_admins() -> None:
    high = MemoryUse(used_bytes=10, limit_bytes=10)
    s = status_from(["storage"], [alive("w1", memory=high), lost("w2")], admin=True)
    assert (s.state, s.causes) == ("down", ("storage", "worker_lost", "memory_high"))


def test_slots_follow_takes_and_running_work() -> None:
    d = status_from(
        [], [alive("a", takes="all", query=True), alive("b", takes="pipeline")], admin=True
    ).detail
    assert d is not None
    assert (d.slots_busy, d.slots_total) == (1, 3)


def test_a_pipeline_sql_task_takes_one_slot_of_an_all_worker() -> None:
    d = status_from([], [alive("a", query=True, adhoc=False, task=True)], admin=True).detail
    assert d is not None
    assert (d.slots_busy, d.slots_total) == (1, 2)


def test_each_kind_of_worker_has_its_slots() -> None:
    assert SLOTS_BY_TAKES == {"all": 2, "interactive": 1, "pipeline": 1}
    views = [
        alive("i", takes="interactive", query=True),
        alive("p", takes="pipeline", deploy=True),
        alive("a", takes="all", query=True, task=True),
        alive("x", takes="later-kind"),
        lost("l"),
    ]
    d = status_from([], views, admin=True).detail
    assert d is not None
    assert (d.slots_busy, d.slots_total) == (4, 5)
    assert [w.id for w in d.workers] == ["i", "p", "a", "x", "l"]


def test_memory_adds_up_over_the_alive_workers() -> None:
    views = [
        alive("a", memory=MemoryUse(used_bytes=_GIB, limit_bytes=4 * _GIB)),
        alive("b", memory=MemoryUse(used_bytes=2 * _GIB, limit_bytes=4 * _GIB)),
    ]
    d = status_from([], views, admin=True).detail
    assert d is not None
    assert (d.memory_used_bytes, d.memory_limit_bytes) == (3 * _GIB, 8 * _GIB)


def test_memory_limit_unknown_when_a_worker_reports_none() -> None:
    known = MemoryUse(used_bytes=_GIB, limit_bytes=4 * _GIB)
    for other in (None, MemoryUse(used_bytes=_GIB, limit_bytes=None)):
        d = status_from([], [alive("a", memory=known), alive("b", memory=other)], admin=True).detail
        assert d is not None
        assert d.memory_limit_bytes is None
    nobody = status_from(["worker"], [], admin=True).detail
    assert nobody is not None
    assert (nobody.memory_used_bytes, nobody.memory_limit_bytes) == (0, None)


def test_only_administrators_get_the_detail() -> None:
    assert status_from([], [alive("w1")], admin=False).detail is None
    assert status_from([], [alive("w1")], admin=True).detail is not None
