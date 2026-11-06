"""A task run that the memory watchdog stops: what is recorded and what is retried."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from task_run_helpers import World, _claim, _engine, _start, _task

import lakana_worker.memory as memory_module
import lakana_worker.tasks as tasks_module
from lakana_core.models import (
    AttemptOutcome,
    Environment,
    QueryRun,
    QueryRunStatus,
    TaskAttempt,
    TaskRun,
    TaskStatus,
)
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.tasks import (
    ClaimedTask,
    FailureKind,
    record_attempt,
)
from lakana_worker.task_outcome import (
    TaskOutcome,
)


def test_a_claimed_task_is_registered_as_in_flight_while_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memory watchdog ends the attempt of a task it finds registered."""
    from types import SimpleNamespace

    from lakana_worker.activity import ACTIVITY, TaskClaim

    seen: list[object] = []
    monkeypatch.setattr(
        tasks_module,
        "_run_claimed",
        lambda engine, claimed, serve: seen.append(ACTIVITY.snapshot()[1]),
    )
    task_id = uuid.uuid4()
    engine: Any = SimpleNamespace(settings=SimpleNamespace(worker_id="w1"))
    claimed: Any = SimpleNamespace(task_run_id=task_id, claim=7)
    tasks_module.run_claimed(engine, claimed)
    assert seen == [(TaskClaim(task_id, "w1", 7),)]
    assert ACTIVITY.snapshot()[1] == ()


def _stopped_for_memory(
    world: World,
    *,
    unrelated_query: bool = False,
    peak: bool = False,
    committed: int | None = None,
    has_checks: bool = False,
    written: bool = False,
) -> tuple[ClaimedTask, uuid.UUID, uuid.UUID, uuid.UUID | None]:
    """Claim a task that has a registered write run and an unregistered `RUNNING` one, then stop.

    The registry is filled like the worker does, and `record_memory_failure` runs against the
    real database, as the watchdog does.

    Parameters
    ----------
    world
        The world.
    unrelated_query
        Also register an ad-hoc query run that belongs to no task.
    peak
        Record as on macOS, where the measurement is a peak.
    committed
        Make the registered write run the task's current one and give it this evidence
        (`rows_written`): the write had landed when the worker was stopped.
    has_checks
        Mark the task's asset as having checks.
    written
        Stamp the task run *written*, as the write's commit does for a task with checks.

    Returns
    -------
    tuple
        The claim, the registered write run, the unregistered run and the ad-hoc run, if any.
    """
    from lakana_worker.activity import Activity, TaskClaim
    from lakana_worker.memory import Limit, record_memory_failure

    _start(world)
    claimed = _claim(world)
    registered, unregistered, adhoc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with world.session() as session:
        env = session.scalars(select(Environment)).one()
        for run_id, task_run_id in (
            (registered, claimed.task_run_id),
            (unregistered, claimed.task_run_id),
            (adhoc, None),
        ):
            if run_id == adhoc and not unrelated_query:
                continue
            session.add(
                QueryRun(
                    id=run_id,
                    environment_id=env.id,
                    principal_id=world.admin.id,
                    sql_text="SELECT 1",
                    status=QueryRunStatus.RUNNING,
                    task_run_id=task_run_id,
                    worker_id="w1",
                    rows_written=committed if run_id == registered else None,
                )
            )
        if committed is not None:
            session.flush()
            session.execute(
                update(TaskRun)
                .where(TaskRun.id == claimed.task_run_id)
                .values(query_run_id=registered)
            )
        if has_checks or written:
            session.execute(
                update(TaskRun)
                .where(TaskRun.id == claimed.task_run_id)
                .values(
                    has_checks=has_checks,
                    written_table_id=uuid.uuid4() if written else None,
                    written_snapshot_before=11 if written else None,
                    written_snapshot_after=12 if written else None,
                    written_publishes=False if written else None,
                )
            )
        session.commit()
    activity = Activity()
    limit = Limit(4 * 1024**3, "4GB", "x")
    with (
        activity.task(TaskClaim(claimed.task_run_id, "w1", claimed.claim)),
        activity.query(registered),
    ):
        if unrelated_query:
            with activity.query(adhoc):
                record_memory_failure(world.session, limit, lambda: 0.0, "w1", activity, peak=peak)
        else:
            record_memory_failure(world.session, limit, lambda: 0.0, "w1", activity, peak=peak)
    return claimed, registered, unregistered, adhoc if unrelated_query else None


def _run_status(world: World, run_id: uuid.UUID) -> tuple[QueryRunStatus, str | None]:
    """Read a query run's status and cause.

    Parameters
    ----------
    world
        The world.
    run_id
        The run.

    Returns
    -------
    tuple
        The status and `error_cause`.
    """
    with world.session() as session:
        run = session.get(QueryRun, run_id)
        assert run is not None
        return run.status, run.error_cause


def test_a_worker_stopped_for_memory_fails_its_task_and_every_query_run_of_it(
    world: World,
) -> None:
    """The attempt ends, the lease is released, both runs fail; a second call is a no-op.

    The write run the task created before `run_one` registered it is failed too, and with only
    this task's own runs in flight the blame is certain, so the task is not retried.
    """
    claimed, registered, unregistered, _ = _stopped_for_memory(world)
    task = _task(world, claimed.asset)
    assert (task.status, task.lease_owner, task.lease_expires_at) == (TaskStatus.FAILED, None, None)
    assert task.error_message is not None and "memory limit (4GB)" in task.error_message
    with world.session() as session:
        attempt = session.scalars(select(TaskAttempt)).one()
        assert (attempt.outcome, attempt.finished_at is not None) == (AttemptOutcome.FAILED, True)
    for run_id in (registered, unregistered):
        assert _run_status(world, run_id) == (QueryRunStatus.FAILED, "out_of_memory")
    from lakana_worker.activity import Activity
    from lakana_worker.memory import Limit, record_memory_failure

    before = _task(world, claimed.asset)
    record_memory_failure(world.session, Limit(1, "x", "x"), lambda: 0.0, "w1", Activity())
    assert _task(world, claimed.asset).error_message == before.error_message


def test_another_workers_running_write_run_is_not_touched_by_a_stop(world: World) -> None:
    """#171: an entry kept after its thread left cannot fail a run another worker holds.

    The task run was claimed again by another worker, whose write run is `RUNNING` under the
    same `task_run_id`; the stale claim and a stale query id still sit in this worker's registry.
    """
    from lakana_worker.activity import Activity, TaskClaim
    from lakana_worker.memory import Limit, record_memory_failure

    _start(world)
    claimed = _claim(world)
    theirs_by_task, theirs_by_id = uuid.uuid4(), uuid.uuid4()
    with world.session() as session:
        env = session.scalars(select(Environment)).one()
        for run_id, task_run_id in ((theirs_by_task, claimed.task_run_id), (theirs_by_id, None)):
            session.add(
                QueryRun(
                    id=run_id,
                    environment_id=env.id,
                    principal_id=world.admin.id,
                    sql_text="SELECT 1",
                    status=QueryRunStatus.RUNNING,
                    task_run_id=task_run_id,
                    worker_id="other-worker",
                )
            )
        session.commit()
    activity = Activity()
    with (
        activity.task(TaskClaim(claimed.task_run_id, "w1", claimed.claim)),
        activity.query(theirs_by_id),
    ):
        record_memory_failure(world.session, Limit(1, "1B", "x"), lambda: 0.0, "w1", activity)
    for run_id in (theirs_by_task, theirs_by_id):
        assert _run_status(world, run_id) == (QueryRunStatus.RUNNING, None)


@pytest.mark.parametrize("rows", [0, 3])
def test_a_worker_stopped_for_memory_after_its_tasks_write_committed_does_not_retry_it(
    world: World, rows: int
) -> None:
    """The write landed, so the attempt succeeds; a retry would append the rows twice."""
    claimed, registered, unregistered, _ = _stopped_for_memory(world, committed=rows)
    task = _task(world, claimed.asset)
    assert (task.status, task.attempt, task.lease_owner) == (TaskStatus.SUCCEEDED, 1, None)
    with world.session() as session:
        attempt = session.scalars(select(TaskAttempt)).one()
        assert (attempt.outcome, attempt.rows_written) == (AttemptOutcome.SUCCEEDED, rows)
        assert attempt.error_message is not None and "memory limit" in attempt.error_message
    assert _run_status(world, registered) == (QueryRunStatus.SUCCEEDED, None)
    assert _run_status(world, unregistered) == (QueryRunStatus.FAILED, "out_of_memory")


@pytest.mark.parametrize(
    ("peak", "written"), [(False, True), (True, True), (False, False), (True, False)]
)
def test_the_memory_stop_does_not_succeed_a_written_unchecked_task(
    world: World, peak: bool, written: bool
) -> None:
    """The write landed and the checks have no verdict, so the task waits again.

    Not succeeded (the rule for a landed write does not apply to a task with checks), and not
    failed either, whatever the blame: the next claim runs its checks only.
    """
    from lakana_worker.memory import COMMITTED_BEFORE_STOPPED_MESSAGE

    claimed, registered, unregistered, _ = _stopped_for_memory(
        world, committed=3, has_checks=True, written=written, peak=peak
    )
    task = _task(world, claimed.asset)
    assert (task.status, task.attempt, task.lease_owner) == (TaskStatus.PENDING, 1, None)
    assert task.finished_at is None and (task.written_table_id is not None) is written
    assert task.error_message is not None and "memory limit" in task.error_message
    assert task.error_message != COMMITTED_BEFORE_STOPPED_MESSAGE
    with world.session() as session:
        attempt = session.scalars(select(TaskAttempt)).one()
        assert attempt.outcome is AttemptOutcome.FAILED
    assert _run_status(world, registered) == (QueryRunStatus.SUCCEEDED, None)
    assert _run_status(world, unregistered) == (QueryRunStatus.FAILED, "out_of_memory")


def test_the_memory_stop_still_fails_a_task_with_checks_that_is_not_written(
    world: World,
) -> None:
    """Checks but no stamp: the write did not commit, so the stop ends it as it always did."""
    claimed, *_ = _stopped_for_memory(world, has_checks=True)
    assert _task(world, claimed.asset).status is TaskStatus.FAILED


def test_an_unrelated_query_in_flight_makes_the_blame_uncertain_so_the_task_is_retried(
    world: World,
) -> None:
    """With an ad-hoc query in flight too, the task keeps its attempts."""
    claimed, registered, _, adhoc = _stopped_for_memory(world, unrelated_query=True)
    task = _task(world, claimed.asset)
    assert task.status is TaskStatus.PENDING and task.attempt == 1
    assert adhoc is not None
    for run_id in (registered, adhoc):
        assert _run_status(world, run_id) == (QueryRunStatus.FAILED, "out_of_memory")


def test_where_the_measurement_is_a_peak_the_task_is_always_retried(world: World) -> None:
    """The blame is never certain, so a lone task still keeps its attempts."""
    claimed, *_ = _stopped_for_memory(world, peak=True)
    assert _task(world, claimed.asset).status is TaskStatus.PENDING


def _add_query_run(
    world: World, run_id: uuid.UUID, status: QueryRunStatus = QueryRunStatus.RUNNING
) -> None:
    """Add an ad-hoc query run.

    Parameters
    ----------
    world
        The world.
    run_id
        The run's id.
    status
        Its status.
    """
    with world.session() as session:
        env = session.scalars(select(Environment)).one()
        session.add(
            QueryRun(
                id=run_id,
                environment_id=env.id,
                principal_id=world.admin.id,
                sql_text="SELECT 1",
                status=status,
                worker_id="w1",
            )
        )
        session.commit()


def _watchdog_over_twice(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    kill: Callable[[], None],
    second_reading: int = 5 * 1024**3,
    ended_task: uuid.UUID | None = None,
) -> tuple[Any, list[str]]:
    """Build the real watchdog of `start_watchdog` on Linux that measures "over", then again.

    The kill runs `kill` and says it ended a query; the worker's stop runs only the record step.

    Parameters
    ----------
    world
        The world (its sessions record the failure).
    monkeypatch
        Replaces the measurement, the kill and the stop.
    kill
        What happens at the kill (the executor's thread leaving, say).
    second_reading
        What the measurement after the kill reads.
    ended_task
        The task run the kill ends, or `None` when it ends a query.

    Returns
    -------
    tuple
        The watchdog and the list that notes each call of the worker's stop.
    """
    from lakana_worker import memory

    stops: list[str] = []
    readings = iter([5 * 1024**3, second_reading])
    monkeypatch.setattr(memory.MemoryWatchdog, "start", lambda self: None)
    monkeypatch.setattr(memory, "tools", lambda platform: (lambda: next(readings), lambda: None))

    def stop_largest_consumer(total: int, limit: memory.Limit) -> memory.Ended:
        """End the "largest query": run `kill`.

        Parameters
        ----------
        total
            Ignored.
        limit
            Ignored.

        Returns
        -------
        memory.Ended
            What was ended.
        """
        kill()
        return memory.Ended(1, ended_task, 4096)

    def stop_worker(total: int, limit: memory.Limit, **steps: Callable[[], None]) -> None:
        """Note the stop and run its record step, without exiting.

        Parameters
        ----------
        total
            Ignored.
        limit
            Ignored.
        **steps
            The stop's steps.
        """
        stops.append("stop")
        steps["record"]()

    monkeypatch.setattr(memory, "stop_largest_consumer", stop_largest_consumer)
    monkeypatch.setattr(memory, "stop_worker", stop_worker)
    dog = memory.start_watchdog("4GB", world.session, lambda: 0.0, "w1", "linux")
    assert dog is not None
    return dog, stops


def test_a_query_that_left_the_registry_between_the_kill_and_the_stop_is_still_recorded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review C1: the killed query's thread may be gone before the worker records.

    The executor's thread leaves the registry between the kill and the stop (ordered by
    events). The run stays `RUNNING` there (the stopping worker records nothing of its own),
    so the watchdog's earlier snapshot must reach it; the Python task beside it is retried,
    because an unrelated query was in flight.
    """
    from lakana_worker.activity import ACTIVITY, TaskClaim

    _start(world)
    claimed = _claim(world)
    run_id = uuid.uuid4()
    _add_query_run(world, run_id)
    in_flight, killed, left = threading.Event(), threading.Event(), threading.Event()

    def executor_thread() -> None:
        """Run a query, and leave the registry once it was killed."""
        with ACTIVITY.query(run_id):
            in_flight.set()
            killed.wait(10)
        left.set()

    def kill() -> None:
        """End the query: tell its thread and wait until it has left."""
        killed.set()
        assert left.wait(10)

    dog, stops = _watchdog_over_twice(world, monkeypatch, kill)
    thread = threading.Thread(target=executor_thread)
    with ACTIVITY.task(TaskClaim(claimed.task_run_id, "w1", claimed.claim)):
        thread.start()
        assert in_flight.wait(10)
        assert dog.check_once() is True
    thread.join(10)
    assert stops == ["stop"]
    assert _run_status(world, run_id) == (QueryRunStatus.FAILED, "out_of_memory")
    task = _task(world, claimed.asset)
    assert task.status is TaskStatus.PENDING and task.attempt == 1  # transient, not permanent


def _task_ended_by_the_watchdog_then_the_worker_stops(
    world: World, monkeypatch: pytest.MonkeyPatch, own_record: Callable[[ClaimedTask], None] | None
) -> tuple[ClaimedTask, list[str]]:
    """End a task for memory, let its thread leave, and stop the worker (events, no sleeps).

    The task's thread leaves `ACTIVITY` between the kill and the stop, as the real one does
    once its process is gone; with `own_record` it first records its outcome through the
    normal path.

    Parameters
    ----------
    world
        The world.
    monkeypatch
        Replaces the measurement, the kill and the stop.
    own_record
        What the task's own thread records before it leaves, or `None` when the stop began
        first and the thread records nothing.

    Returns
    -------
    tuple
        The claim and the list that notes each call of the worker's stop.
    """
    from lakana_worker.activity import ACTIVITY, TaskClaim

    _start(world)
    claimed = _claim(world)
    in_flight, killed, left = threading.Event(), threading.Event(), threading.Event()

    def task_thread() -> None:
        """Run the task, and leave once it was ended."""
        with ACTIVITY.task(TaskClaim(claimed.task_run_id, "w1", claimed.claim)):
            in_flight.set()
            killed.wait(10)
            if own_record is not None:
                own_record(claimed)
        left.set()

    def kill() -> None:
        """End the task: tell its thread and wait until it has left."""
        killed.set()
        assert left.wait(10)

    dog, stops = _watchdog_over_twice(world, monkeypatch, kill, ended_task=claimed.task_run_id)
    thread = threading.Thread(target=task_thread)
    thread.start()
    assert in_flight.wait(10)
    assert dog.check_once() is True
    thread.join(10)
    return claimed, stops


def test_a_task_the_watchdog_ended_is_recorded_by_the_stop_when_its_thread_left_unrecorded(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker stops after ending a task: its thread records nothing (`stopping`) and left.

    The snapshot taken before the kill reaches the task: it gets the transient failure with the
    "ended" message (not the certain blame, though it was the only task), so it is retried.
    """
    memory = memory_module

    claimed, stops = _task_ended_by_the_watchdog_then_the_worker_stops(world, monkeypatch, None)
    assert stops == ["stop"]
    task = _task(world, claimed.asset)
    assert task.status is TaskStatus.PENDING and task.attempt == 1
    assert task.error_message == memory.task_ended_message(memory.resolve_limit("4GB"))
    with world.session() as session:  # the size the watchdog chose it by travels to the attempt
        assert session.scalars(select(TaskAttempt.peak_memory_bytes)).one() == 4096


def test_a_task_the_watchdog_ended_and_recorded_itself_is_not_recorded_again_by_the_stop(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task's own thread recorded first: the stop leaves that record alone, one attempt."""
    calls: list[bool] = []
    real = memory_module.record_attempt

    def counting(
        session: Session,
        task_run_id: uuid.UUID,
        worker: str,
        claim: int,
        *,
        kind: FailureKind | None,
        rand: Callable[[], float],
        error_message: str | None = None,
        failure_code: FailureCode | None = None,
        on_committed: str | None = None,
        stopped: bool = False,
        resources: object = None,
    ) -> bool:
        """Pass on and note whether the record was written.

        Parameters
        ----------
        session
            Passed on.
        task_run_id
            Passed on.
        worker
            Passed on.
        claim
            Passed on.
        kind
            Passed on.
        rand
            Passed on.
        error_message
            Passed on.
        failure_code
            Passed on.
        on_committed
            Passed on.
        stopped
            Passed on.
        resources
            Passed on.

        Returns
        -------
        bool
            What `record_attempt` answered.
        """
        written = real(
            session,
            task_run_id,
            worker,
            claim,
            kind=kind,
            rand=rand,
            error_message=error_message,
            failure_code=failure_code,
            on_committed=on_committed,
            stopped=stopped,
            resources=resources,  # ty: ignore[invalid-argument-type]
        )
        calls.append(written)
        return written

    monkeypatch.setattr(memory_module, "record_attempt", counting)

    def own_record(claimed: ClaimedTask) -> None:
        """Record the transient failure the task's own thread records.

        Parameters
        ----------
        claimed
            The claim.
        """
        with world.session() as session:
            assert record_attempt(
                session,
                claimed.task_run_id,
                "w1",
                claimed.claim,
                kind=FailureKind.TRANSIENT,
                rand=lambda: 0.0,
                error_message="own thread's message",
            )

    claimed, stops = _task_ended_by_the_watchdog_then_the_worker_stops(
        world, monkeypatch, own_record
    )
    assert stops == ["stop"]
    assert calls == [False]  # the stop tried once and wrote nothing
    task = _task(world, claimed.asset)
    assert task.status is TaskStatus.PENDING
    assert task.error_message == "own thread's message"
    with world.session() as session:
        assert len(session.scalars(select(TaskAttempt)).all()) == 1


def test_a_task_the_watchdog_ended_that_is_still_in_flight_is_recorded_once_as_transient(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop began before the task's thread left: one claim, the "ended" message, retried."""
    from lakana_worker.activity import Activity, TaskClaim

    _start(world)
    claimed = _claim(world)
    claim = TaskClaim(claimed.task_run_id, "w1", claimed.claim)
    limit = memory_module.resolve_limit("4GB")
    activity = Activity()
    writes: list[bool] = []
    real = memory_module.record_attempt
    monkeypatch.setattr(
        memory_module,
        "record_attempt",
        lambda *a, **k: writes.append(real(*a, **k)) or writes[-1],
    )
    with activity.task(claim):
        memory_module.record_memory_failure(
            world.session, limit, lambda: 0.0, "w1", activity, earlier_tasks=(claim,)
        )
    assert writes == [True]
    task = _task(world, claimed.asset)
    assert task.status is TaskStatus.PENDING
    assert task.error_message == memory_module.task_ended_message(limit)
    assert task.failure_code == "memory"
    with world.session() as session:
        assert len(session.scalars(select(TaskAttempt)).all()) == 1


def test_the_attempt_of_a_task_the_watchdog_ended_keeps_the_size_it_was_ended_at(
    world: World,
) -> None:
    """The ended task's attempt gets the size it was ended at; no other figure is made up.

    A peak recorded for another task run (one that is not ended here) changes nothing.
    """
    from lakana_worker.activity import Activity, TaskClaim

    _start(world)
    claimed = _claim(world)
    claim = TaskClaim(claimed.task_run_id, "w1", claimed.claim)
    limit = memory_module.resolve_limit("4GB")
    memory_module.record_memory_failure(
        world.session,
        limit,
        lambda: 0.0,
        "w1",
        Activity(),
        earlier_tasks=(claim,),
        earlier_peaks={claim.task_run_id: 123456, uuid.uuid4(): 1},
    )
    with world.session() as session:
        attempt = session.scalars(select(TaskAttempt)).one()
        assert attempt.peak_memory_bytes == 123456
        assert attempt.cpu_seconds is None and attempt.bytes_read is None


def test_a_task_ended_by_an_earlier_check_does_not_count_against_a_later_lone_task(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note of an ended task is reset at every check.

    Check one ends a task and that helped. Check two finds nothing endable and stops the worker
    while a different task runs alone: it is judged as a lone task (certain blame), and the
    first task's stale claim is not recorded against.
    """
    from lakana_worker.activity import ACTIVITY, TaskClaim

    _start(world)
    claimed = _claim(world)
    stale = TaskClaim(uuid.uuid4(), "w1", 1)
    answers = iter([memory_module.Ended(1, stale.task_run_id), None])
    readings = iter([5 * 1024**3, 1, 5 * 1024**3, 5 * 1024**3])
    recorded: list[uuid.UUID] = []
    real = memory_module.record_attempt

    def spying(
        session: Session,
        task_run_id: uuid.UUID,
        worker: str,
        claim: int,
        *,
        kind: FailureKind | None,
        rand: Callable[[], float],
        error_message: str | None = None,
        failure_code: FailureCode | None = None,
        on_committed: str | None = None,
        stopped: bool = False,
        resources: object = None,
    ) -> bool:
        """Note which task is recorded against, and pass on.

        Parameters
        ----------
        session
            Passed on.
        task_run_id
            The task.
        worker
            Passed on.
        claim
            Passed on.
        kind
            Passed on.
        rand
            Passed on.
        error_message
            Passed on.
        failure_code
            Passed on.
        on_committed
            Passed on.
        stopped
            Passed on.
        resources
            Passed on.

        Returns
        -------
        bool
            What `record_attempt` answered.
        """
        recorded.append(task_run_id)
        return real(
            session,
            task_run_id,
            worker,
            claim,
            kind=kind,
            rand=rand,
            error_message=error_message,
            failure_code=failure_code,
            on_committed=on_committed,
            stopped=stopped,
            resources=resources,  # ty: ignore[invalid-argument-type]
        )

    monkeypatch.setattr(memory_module.MemoryWatchdog, "start", lambda self: None)
    monkeypatch.setattr(
        memory_module, "tools", lambda platform: (lambda: next(readings), lambda: None)
    )
    monkeypatch.setattr(memory_module, "stop_largest_consumer", lambda total, limit: next(answers))
    monkeypatch.setattr(memory_module, "record_attempt", spying)
    monkeypatch.setattr(
        memory_module, "stop_worker", lambda total, limit, **steps: steps["record"]()
    )
    dog = memory_module.start_watchdog("4GB", world.session, lambda: 0.0, "w1", "linux")
    assert dog is not None
    with ACTIVITY.task(stale):
        assert dog.check_once() is True  # ends the first task; the worker goes on
    with ACTIVITY.task(TaskClaim(claimed.task_run_id, "w1", claimed.claim)):
        assert dog.check_once() is True  # nothing endable: the worker stops
    assert recorded == [claimed.task_run_id]
    assert _task(world, claimed.asset).status is TaskStatus.FAILED  # a lone task: certain


def test_a_kill_that_helped_records_nothing(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review C1 (c): when ending the query was enough, the stop path never runs."""
    from lakana_worker.activity import ACTIVITY

    run_id = uuid.uuid4()
    _add_query_run(world, run_id)
    dog, stops = _watchdog_over_twice(world, monkeypatch, lambda: None, second_reading=1)
    with ACTIVITY.query(run_id):
        assert dog.check_once() is True
    assert stops == []
    assert _run_status(world, run_id) == (QueryRunStatus.RUNNING, None)


def test_a_run_the_executor_already_finished_is_not_touched_by_the_earlier_snapshot(
    world: World,
) -> None:
    """Review C1: the update only touches `RUNNING` rows."""
    from lakana_worker.activity import Activity
    from lakana_worker.memory import Limit, record_memory_failure

    finished, running = uuid.uuid4(), uuid.uuid4()
    _add_query_run(world, finished, QueryRunStatus.FAILED)
    _add_query_run(world, running)
    record_memory_failure(
        world.session,
        Limit(4 * 1024**3, "4GB", "x"),
        lambda: 0.0,
        "w1",
        Activity(),
        earlier=(finished, running),
    )
    assert _run_status(world, finished) == (QueryRunStatus.FAILED, None)
    assert _run_status(world, running) == (QueryRunStatus.FAILED, "out_of_memory")


def _signal_outcome() -> TaskOutcome:
    """Build the outcome the main thread records when a task's process was killed.

    Returns
    -------
    TaskOutcome
        A transient failure with the signal text.
    """
    return TaskOutcome(
        kind=FailureKind.TRANSIENT,
        message="the task process was killed by signal 9",
        environment=None,
    )


def test_a_task_killed_by_the_watchdog_ends_with_the_memory_outcome_not_the_signal_text(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The main thread wakes on the dead pipe; with the worker stopping it writes nothing.

    The dispatch stands in for the runner: the watchdog's steps run (flag, recording), then the
    runner reports the signal-9 outcome, exactly the order that used to be a race.
    """
    from lakana_worker.activity import ACTIVITY
    from lakana_worker.memory import Limit, record_memory_failure

    _start(world)
    claimed = _claim(world)
    monkeypatch.setattr(ACTIVITY, "stopping", False)

    def dispatch(*args: object, **kwargs: object) -> TaskOutcome:
        ACTIVITY.stopping = True
        record_memory_failure(world.session, Limit(4 * 1024**3, "4GB", "x"), lambda: 0.0, "w1")
        return _signal_outcome()

    monkeypatch.setattr(tasks_module, "_dispatch", dispatch)
    engine = _engine(world)
    tasks_module.run_claimed(engine, claimed)
    task = _task(world, claimed.asset)
    assert task.error_message is not None
    assert "memory limit (4GB)" in task.error_message and "signal 9" not in task.error_message
    assert task.status is TaskStatus.FAILED


def test_the_main_thread_records_the_signal_outcome_when_the_worker_is_not_stopping(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for the test above: without the flag the signal text is what gets recorded."""
    from lakana_worker.activity import ACTIVITY

    _start(world)
    claimed = _claim(world)
    monkeypatch.setattr(ACTIVITY, "stopping", False)
    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: _signal_outcome())
    tasks_module.run_claimed(_engine(world), claimed)
    task = _task(world, claimed.asset)
    assert task.error_message == "the task process was killed by signal 9"


def test_a_stopping_worker_records_no_attempt_of_its_own(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag set, `run_claimed` leaves the task `RUNNING` for the watchdog to finish."""
    from lakana_worker.activity import ACTIVITY

    _start(world)
    claimed = _claim(world)
    monkeypatch.setattr(ACTIVITY, "stopping", True)
    monkeypatch.setattr(tasks_module, "_dispatch", lambda *a, **k: _signal_outcome())
    tasks_module.run_claimed(_engine(world), claimed)
    assert _task(world, claimed.asset).status is TaskStatus.RUNNING
