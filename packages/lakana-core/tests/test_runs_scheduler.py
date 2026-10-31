"""The scheduler tick: snapshot in, transitions out."""

from __future__ import annotations

import dataclasses
import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from lakana_core.models import RunStatus, TaskStatus
from lakana_core.runs.checks import (
    CHECKS_UNFINISHED_MESSAGE,
    RUN_CANCELLED_MESSAGE,
    RUN_STOPPED_MESSAGE,
    SET_BACK_LOST,
)
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.scheduler import (
    LOST_MESSAGE,
    MAX_BACKOFF_SECONDS,
    MAX_CHECK_CLAIMS,
    MAX_SET_BACK_CLAIMS,
    RunSnapshot,
    RunTransition,
    TaskSnapshot,
    TaskTransition,
    backoff_seconds,
    final_status,
    next_attempt_time,
    tick,
)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
RUN = uuid.UUID(int=1)


def _task(
    asset: str,
    status: TaskStatus = TaskStatus.PENDING,
    *,
    upstream: tuple[str, ...] = (),
    attempt: int = 0,
    max_attempts: int = 3,
    next_attempt_at: datetime | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    committed: bool = False,
    unchecked: bool = False,
    may_have_written: bool = False,
    baselined: bool = False,
    setting_back: bool = False,
    set_back_claims: int = 0,
) -> TaskSnapshot:
    """Build a task snapshot whose id is derived from its asset.

    Parameters
    ----------
    asset
        Dotted name.
    status
        Status.
    upstream
        Selected upstream assets.
    attempt
        Attempts so far.
    max_attempts
        Attempt limit.
    next_attempt_at
        Earliest next attempt.
    lease_owner
        Worker holding the lease.
    lease_expires_at
        Lease expiry.
    committed
        Whether the task's current write run has its evidence.
    unchecked
        Whether the task is written, has checks and has no verdict.
    may_have_written
        Whether a running Python task with checks has a baseline and no stamp.
    baselined
        Whether a task with checks has a baseline and no stamp, whatever its status.
    setting_back
        Whether the task is only to set its table back.
    set_back_claims
        Claims taken while it was setting back.

    Returns
    -------
    TaskSnapshot
        The snapshot.
    """
    return TaskSnapshot(
        id=uuid.uuid5(uuid.NAMESPACE_URL, asset),
        asset=asset,
        status=status,
        attempt=attempt,
        max_attempts=max_attempts,
        retry_delay_seconds=30,
        next_attempt_at=next_attempt_at,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        upstream=upstream,
        claim=attempt,
        committed=committed,
        unchecked=unchecked,
        may_have_written=may_have_written,
        baselined=baselined,
        setting_back=setting_back,
        set_back_claims=set_back_claims,
    )


def _run(
    *tasks: TaskSnapshot,
    status: RunStatus = RunStatus.RUNNING,
    cancel: bool = False,
    stopped: bool = False,
) -> RunSnapshot:
    """Build a run snapshot.

    Parameters
    ----------
    *tasks
        Its tasks.
    status
        Run status.
    cancel
        Whether cancel was requested.
    stopped
        Whether the run was failed by a code integrity check (its cancel flag is set too).

    Returns
    -------
    RunSnapshot
        The snapshot.
    """
    return RunSnapshot(
        id=RUN,
        status=status,
        cancel_requested=cancel or stopped,
        tasks=tasks,
        stopped_by_check=stopped,
    )


def _statuses(transitions: list) -> dict[str, TaskStatus]:
    """Index task transitions by the asset whose id they carry.

    Parameters
    ----------
    transitions
        `tick` output.

    Returns
    -------
    dict
        Asset name → new status (assets a..z only, as the tests use).
    """
    names = {uuid.uuid5(uuid.NAMESPACE_URL, n): n for n in ("a.a", "a.b", "a.c", "a.d")}
    return {names[t.task_id]: t.status for t in transitions if isinstance(t, TaskTransition)}


def _fixed(value: float = 0.5) -> float:
    """Return a deterministic random value: 0.5 means no jitter.

    Parameters
    ----------
    value
        The value returned.

    Returns
    -------
    float
        `value`.
    """
    return value


def test_queued_run_starts_and_roots_become_ready() -> None:
    """The first tick starts the run and readies every task without upstreams."""
    snapshot = _run(_task("a.a"), _task("a.b", upstream=("a.a",)), status=RunStatus.QUEUED)
    transitions = tick([snapshot], NOW, _fixed)
    assert transitions[0] == RunTransition(RUN, RunStatus.QUEUED, RunStatus.RUNNING)
    assert _statuses(transitions) == {"a.a": TaskStatus.READY}


def test_a_task_waits_for_every_selected_upstream() -> None:
    """Readiness needs every upstream succeeded."""
    snapshot = _run(
        _task("a.a", TaskStatus.SUCCEEDED),
        _task("a.b", TaskStatus.RUNNING, lease_owner="w", lease_expires_at=NOW + timedelta(60)),
        _task("a.c", upstream=("a.a", "a.b")),
        _task("a.d", upstream=("a.a",)),
    )
    assert _statuses(tick([snapshot], NOW, _fixed)) == {"a.d": TaskStatus.READY}


def test_a_retry_waits_for_its_next_attempt_time() -> None:
    """A pending retry becomes ready only once `next_attempt_at` has passed."""
    later = _run(_task("a.a", attempt=1, next_attempt_at=NOW + timedelta(seconds=1)))
    due = _run(_task("a.a", attempt=1, next_attempt_at=NOW))
    assert _statuses(tick([later], NOW, _fixed)) == {}
    assert _statuses(tick([due], NOW, _fixed)) == {"a.a": TaskStatus.READY}


def test_failure_cascades_upstream_failed_through_the_whole_downstream() -> None:
    """Spec: every downstream task of a failed one becomes upstream_failed, transitively."""
    snapshot = _run(
        _task("a.a", TaskStatus.FAILED),
        _task("a.b", upstream=("a.a",)),
        _task("a.c", TaskStatus.READY, upstream=("a.b",)),
        _task("a.d", TaskStatus.SUCCEEDED),
    )
    transitions = tick([snapshot], NOW, _fixed)
    assert _statuses(transitions) == {
        "a.b": TaskStatus.UPSTREAM_FAILED,
        "a.c": TaskStatus.UPSTREAM_FAILED,
    }
    assert transitions[-1] == RunTransition(
        RUN,
        RunStatus.RUNNING,
        RunStatus.FAILED,
        counts={"failed": 1, "succeeded": 1, "upstream_failed": 2},
    )


def test_expired_lease_is_reclaimed_with_backoff_and_counted() -> None:
    """A lost worker's task goes back to pending; the attempt counts; backoff applies."""
    snapshot = _run(
        _task(
            "a.a",
            TaskStatus.RUNNING,
            attempt=1,
            lease_owner="w1",
            lease_expires_at=NOW - timedelta(seconds=1),
        )
    )
    (transition,) = tick([snapshot], NOW, _fixed)
    assert transition == TaskTransition(
        run_id=RUN,
        task_id=_task("a.a").id,
        expected=TaskStatus.RUNNING,
        status=TaskStatus.PENDING,
        expected_owner="w1",
        expected_claim=1,
        next_attempt_at=NOW + timedelta(seconds=30),
        lost=True,
    )


def test_a_lease_expiring_exactly_now_is_still_held() -> None:
    """Expiry is strict: `lease_expires_at < now`."""
    snapshot = _run(
        _task("a.a", TaskStatus.RUNNING, attempt=1, lease_owner="w", lease_expires_at=NOW)
    )
    assert tick([snapshot], NOW, _fixed) == []


def test_lost_on_the_last_attempt_fails_and_cascades() -> None:
    """Max attempts reached: the task fails with the lost message."""
    snapshot = _run(
        _task(
            "a.a",
            TaskStatus.RUNNING,
            attempt=3,
            max_attempts=3,
            lease_owner="w1",
            lease_expires_at=NOW - timedelta(seconds=1),
        ),
        _task("a.b", upstream=("a.a",)),
    )
    transitions = tick([snapshot], NOW, _fixed)
    reclaimed = transitions[0]
    assert isinstance(reclaimed, TaskTransition)
    assert (reclaimed.status, reclaimed.lost, reclaimed.error_message) == (
        TaskStatus.FAILED,
        True,
        LOST_MESSAGE,
    )
    assert reclaimed.terminal
    assert _statuses(transitions)["a.b"] is TaskStatus.UPSTREAM_FAILED
    assert transitions[-1].status is RunStatus.FAILED


def test_cancel_cancels_waiting_tasks_and_leaves_running_ones_to_their_worker() -> None:
    """Spec: pending/ready → cancelled; a running task sees the flag at its next renewal."""
    snapshot = _run(
        _task("a.a", TaskStatus.RUNNING, lease_owner="w", lease_expires_at=NOW + timedelta(60)),
        _task("a.b", TaskStatus.READY),
        _task("a.c", upstream=("a.a",)),
        cancel=True,
    )
    transitions = tick([snapshot], NOW, _fixed)
    assert _statuses(transitions) == {"a.b": TaskStatus.CANCELLED, "a.c": TaskStatus.CANCELLED}
    assert not any(isinstance(t, RunTransition) for t in transitions)


def _cancel_texts(transitions: list) -> dict[str, str | None]:
    """Index the message of each cancelled task transition by asset.

    Parameters
    ----------
    transitions
        The tick's output.

    Returns
    -------
    dict
        Asset name to the transition's `error_message`.
    """
    ids = {uuid.uuid5(uuid.NAMESPACE_URL, name): name for name in ("a.a", "a.b", "a.c")}
    return {
        ids[t.task_id]: t.error_message
        for t in transitions
        if isinstance(t, TaskTransition) and t.status is TaskStatus.CANCELLED
    }


def test_the_cancel_loop_gives_a_cancelled_task_the_text_of_a_cancel() -> None:
    """A waiting task the cancel ends reads like a task whose worker stopped for the cancel."""
    snapshot = _run(_task("a.b", TaskStatus.READY), _task("a.c", upstream=("a.b",)), cancel=True)
    assert _cancel_texts(tick([snapshot], NOW, _fixed)) == {
        "a.b": RUN_CANCELLED_MESSAGE,
        "a.c": RUN_CANCELLED_MESSAGE,
    }


def test_the_cancel_loop_names_the_code_integrity_failure_in_a_run_failed_by_it() -> None:
    """Nobody cancelled such a run, so its waiting tasks do not read as cancelled by a user."""
    snapshot = _run(_task("a.b", TaskStatus.READY), stopped=True)
    assert _cancel_texts(tick([snapshot], NOW, _fixed)) == {"a.b": RUN_STOPPED_MESSAGE}


def test_a_lost_task_of_a_cancelled_run_is_cancelled_with_the_text_of_a_cancel() -> None:
    """The reclaim's cancel carries the same text as the cancel loop's."""
    lost = _task(
        "a.a",
        TaskStatus.RUNNING,
        attempt=1,
        lease_owner="w",
        lease_expires_at=NOW - timedelta(seconds=5),
    )
    assert _cancel_texts(tick([_run(lost, cancel=True)], NOW, _fixed)) == {
        "a.a": RUN_CANCELLED_MESSAGE
    }
    assert _cancel_texts(tick([_run(lost, stopped=True)], NOW, _fixed)) == {
        "a.a": RUN_STOPPED_MESSAGE
    }


def test_cancelled_run_with_an_expired_lease_cancels_the_task_and_finishes() -> None:
    """A lost task in a cancelled run is cancelled, not retried, and the run finalizes."""
    snapshot = _run(
        _task(
            "a.a",
            TaskStatus.RUNNING,
            attempt=1,
            lease_owner="w",
            lease_expires_at=NOW - timedelta(seconds=5),
        ),
        cancel=True,
    )
    transitions = tick([snapshot], NOW, _fixed)
    first = transitions[0]
    assert isinstance(first, TaskTransition)
    assert (first.status, first.lost) == (TaskStatus.CANCELLED, True)
    assert transitions[-1] == RunTransition(
        RUN, RunStatus.RUNNING, RunStatus.CANCELLED, counts={"cancelled": 1}
    )


def test_a_cancelled_upstream_cancels_its_downstream() -> None:
    """A task cancelled by its worker takes its waiting downstream with it."""
    snapshot = _run(_task("a.a", TaskStatus.CANCELLED), _task("a.b", upstream=("a.a",)))
    assert _statuses(tick([snapshot], NOW, _fixed)) == {"a.b": TaskStatus.CANCELLED}


def test_finished_runs_finalize_by_rule() -> None:
    """Failed beats cancelled beats succeeded."""
    ok = _run(_task("a.a", TaskStatus.SUCCEEDED), _task("a.b", TaskStatus.SUCCEEDED))
    assert tick([ok], NOW, _fixed) == [
        RunTransition(RUN, RunStatus.RUNNING, RunStatus.SUCCEEDED, counts={"succeeded": 2})
    ]
    assert final_status([TaskStatus.SUCCEEDED, TaskStatus.CANCELLED]) is RunStatus.CANCELLED
    assert final_status([TaskStatus.CANCELLED, TaskStatus.UPSTREAM_FAILED]) is RunStatus.FAILED
    assert final_status([]) is RunStatus.SUCCEEDED


def test_terminal_runs_produce_nothing() -> None:
    """Finished runs are not in the snapshot normally; if one is, it is left alone."""
    done = _run(_task("a.a", TaskStatus.SUCCEEDED), status=RunStatus.SUCCEEDED)
    assert tick([done], NOW, _fixed) == []


def test_runs_are_independent() -> None:
    """Two runs in one snapshot each get their own transitions."""
    other = RunSnapshot(
        id=uuid.UUID(int=2), status=RunStatus.RUNNING, cancel_requested=False, tasks=(_task("a.b"),)
    )
    transitions = tick([_run(_task("a.a")), other], NOW, _fixed)
    assert {t.run_id for t in transitions} == {RUN, uuid.UUID(int=2)}


@pytest.mark.parametrize(
    ("delay", "attempt", "rand", "expected"),
    [
        (30, 1, 0.5, 30.0),
        (30, 2, 0.5, 60.0),
        (30, 3, 0.5, 120.0),
        (30, 1, 0.0, 24.0),
        (30, 1, 1.0, 36.0),
        (30, 10, 0.5, MAX_BACKOFF_SECONDS),
        (0, 5, 1.0, 0.0),
        (1, 1000, 1.0, MAX_BACKOFF_SECONDS),
    ],
)
def test_backoff(delay: int, attempt: int, rand: float, expected: float) -> None:
    """`retry_delay · 2^(attempt-1)` ± 20 % jitter, capped at 10 minutes."""
    assert math.isclose(backoff_seconds(delay, attempt, lambda: rand), expected)


def test_next_attempt_time_adds_the_backoff() -> None:
    """The worker and the scheduler share one formula."""
    assert next_attempt_time(NOW, 10, 2, lambda: 0.5) == NOW + timedelta(seconds=20)


def test_a_readied_task_records_ready_at() -> None:
    """The scheduler claims FIFO by `ready_at`, so the ready transition carries `now`."""
    transitions = tick([_run(_task("a.a"))], NOW, _fixed)
    (transition,) = transitions
    assert isinstance(transition, TaskTransition)
    assert (transition.status, transition.ready_at) == (TaskStatus.READY, NOW)


def test_a_reclaimed_task_is_not_readied_in_the_same_tick() -> None:
    """Readiness reads the post-transition state, so the backoff just set holds."""
    snapshot = _run(
        _task(
            "a.a",
            TaskStatus.RUNNING,
            attempt=1,
            lease_owner="w",
            lease_expires_at=NOW - timedelta(seconds=1),
        )
    )
    transitions = tick([snapshot], NOW, _fixed)
    assert [t.status for t in transitions if isinstance(t, TaskTransition)] == [TaskStatus.PENDING]


@pytest.mark.parametrize(("leaf", "middle"), [("a.a", "a.b"), ("a.b", "a.a")])
def test_upstream_failed_wins_over_cancelled_whatever_the_asset_names(
    leaf: str, middle: str
) -> None:
    """Failure is propagated to its fixed point before cancellation, not in name order."""
    snapshot = _run(
        _task("a.c", TaskStatus.FAILED),
        _task("a.d", TaskStatus.CANCELLED),
        _task(middle, upstream=("a.c",)),
        _task(leaf, upstream=("a.d", middle)),
    )
    snapshot = RunSnapshot(
        id=RUN,
        status=snapshot.status,
        cancel_requested=False,
        tasks=tuple(sorted(snapshot.tasks, key=lambda task: task.asset)),
    )
    assert _statuses(tick([snapshot], NOW, _fixed)) == {
        middle: TaskStatus.UPSTREAM_FAILED,
        leaf: TaskStatus.UPSTREAM_FAILED,
    }


def test_a_deep_cascade_against_the_name_order_settles_in_one_tick() -> None:
    """A chain whose names sort leaf-first still reaches its fixed point in a single tick."""
    snapshot = _run(
        _task("a.a", upstream=("a.b",)),
        _task("a.b", upstream=("a.c",)),
        _task("a.c", upstream=("a.d",)),
        _task("a.d", TaskStatus.FAILED),
    )
    transitions = tick([snapshot], NOW, _fixed)
    assert _statuses(transitions) == dict.fromkeys(
        ("a.a", "a.b", "a.c"), TaskStatus.UPSTREAM_FAILED
    )
    assert isinstance(transitions[-1], RunTransition)
    assert transitions[-1].status is RunStatus.FAILED


def test_a_task_snapshot_must_carry_its_claim_counter() -> None:
    """No default: a snapshot built without `claim` would reclaim against claim 0."""
    fields = {f.name: f for f in dataclasses.fields(TaskSnapshot)}
    assert fields["claim"].default is dataclasses.MISSING
    assert fields["claim"].default_factory is dataclasses.MISSING


def _lost(attempt: int = 1, max_attempts: int = 3, *, committed: bool = False) -> TaskSnapshot:
    """Build a running task whose lease expired a second ago.

    Parameters
    ----------
    attempt
        Attempts so far.
    max_attempts
        Attempt limit.
    committed
        Whether the task's current write run has the evidence.

    Returns
    -------
    TaskSnapshot
        The task `a.a`, owned by `w1`.
    """
    return _task(
        "a.a",
        TaskStatus.RUNNING,
        attempt=attempt,
        max_attempts=max_attempts,
        lease_owner="w1",
        lease_expires_at=NOW - timedelta(seconds=1),
        committed=committed,
    )


@pytest.mark.parametrize("attempt", [1, 3])
def test_a_lost_task_whose_write_committed_succeeds_and_is_not_retried(attempt: int) -> None:
    """The evidence on the current write run ends the task SUCCEEDED, retries or not."""
    snapshot = _run(_lost(attempt, committed=True), _task("a.b", upstream=("a.a",)))
    transitions = tick([snapshot], NOW, _fixed)
    first = transitions[0]
    assert isinstance(first, TaskTransition) and first.terminal
    assert first == TaskTransition(
        run_id=RUN,
        task_id=_task("a.a").id,
        expected=TaskStatus.RUNNING,
        status=TaskStatus.SUCCEEDED,
        expected_owner="w1",
        expected_claim=attempt,
        lost=True,
    )
    assert _statuses(transitions) == {"a.a": TaskStatus.SUCCEEDED, "a.b": TaskStatus.READY}
    assert not any(isinstance(t, RunTransition) for t in transitions)


def test_a_lost_task_whose_write_committed_wins_over_a_cancel_request() -> None:
    """The data is there: the task is SUCCEEDED, not CANCELLED; what waits below it is cancelled."""
    snapshot = _run(_lost(committed=True), _task("a.b", upstream=("a.a",)), cancel=True)
    transitions = tick([snapshot], NOW, _fixed)
    assert _statuses(transitions) == {"a.a": TaskStatus.SUCCEEDED, "a.b": TaskStatus.CANCELLED}
    assert transitions[-1] == RunTransition(
        RUN, RunStatus.RUNNING, RunStatus.CANCELLED, counts={"cancelled": 1, "succeeded": 1}
    )


def test_a_lost_task_without_evidence_is_reclaimed_as_before() -> None:
    """No evidence: retried with attempts left, failed on the last, cancelled in a cancelled run."""
    assert _statuses(tick([_run(_lost())], NOW, _fixed)) == {"a.a": TaskStatus.PENDING}
    assert _statuses(tick([_run(_lost(3))], NOW, _fixed)) == {"a.a": TaskStatus.FAILED}
    assert _statuses(tick([_run(_lost(), cancel=True)], NOW, _fixed)) == {
        "a.a": TaskStatus.CANCELLED
    }


def test_evidence_does_not_end_a_task_whose_lease_is_still_held() -> None:
    """A live worker records its own outcome; the scheduler does nothing."""
    snapshot = _run(
        _task(
            "a.a",
            TaskStatus.RUNNING,
            attempt=1,
            lease_owner="w1",
            lease_expires_at=NOW,
            committed=True,
        )
    )
    assert tick([snapshot], NOW, _fixed) == []


def test_a_failed_source_check_stops_its_readers() -> None:
    """Spec 4: a source check is a task by its table's name; its readers wait for it."""
    failed = _run(
        _task("a.a", TaskStatus.FAILED),
        _task("a.b", upstream=("a.a",)),
        _task("a.c", upstream=("a.b",)),
    )
    assert _statuses(tick([failed], NOW, _fixed)) == {
        "a.b": TaskStatus.UPSTREAM_FAILED,
        "a.c": TaskStatus.UPSTREAM_FAILED,
    }
    passed = _run(
        _task("a.a", TaskStatus.SUCCEEDED),
        _task("a.b", upstream=("a.a",)),
        _task("a.c", upstream=("a.b",)),
    )
    assert _statuses(tick([passed], NOW, _fixed)) == {"a.b": TaskStatus.READY}


def _unchecked(
    attempt: int = 1, max_attempts: int = 1, *, committed: bool = True, unchecked: bool = True
) -> TaskSnapshot:
    """Build the running task `a.a` with an expired lease, written or not.

    Parameters
    ----------
    attempt
        Attempts so far.
    max_attempts
        Attempt limit.
    committed
        Whether its write run shows the evidence.
    unchecked
        Whether it is written, has checks and has no verdict.

    Returns
    -------
    TaskSnapshot
        The task.
    """
    return _task(
        "a.a",
        TaskStatus.RUNNING,
        attempt=attempt,
        max_attempts=max_attempts,
        lease_owner="w1",
        lease_expires_at=NOW - timedelta(seconds=1),
        committed=committed,
        unchecked=unchecked,
    )


def test_a_written_task_with_checks_is_put_back_not_succeeded() -> None:
    """A lost lease never counts an unchecked task as succeeded."""
    task = _unchecked()
    downstream = _task("a.b", TaskStatus.PENDING, upstream=("a.a",))
    transitions = tick([_run(task, downstream)], NOW, _fixed)

    moves = [
        (t.task_id, t.expected, t.status) for t in transitions if isinstance(t, TaskTransition)
    ]
    assert moves == [
        (task.id, TaskStatus.RUNNING, TaskStatus.PENDING),
        (task.id, TaskStatus.PENDING, TaskStatus.READY),
    ]
    first = transitions[0]
    assert isinstance(first, TaskTransition)
    assert (first.lost, first.unchecked, first.next_attempt_at) == (True, True, NOW)
    assert (first.expected_owner, first.expected_claim) == ("w1", 1)
    assert not any(isinstance(t, RunTransition) for t in transitions)


def test_an_unchecked_task_without_the_write_run_evidence_is_still_put_back() -> None:
    """The snapshot's flag alone decides here: the tick needs no evidence of its own to keep it."""
    transitions = tick([_run(_unchecked(committed=False))], NOW, _fixed)
    first = transitions[0]
    assert isinstance(first, TaskTransition)
    assert (first.status, first.unchecked) == (TaskStatus.PENDING, True)


@pytest.mark.parametrize("attempt", [1, 3])
def test_an_unchecked_task_is_not_failed_by_running_out_of_attempts(attempt: int) -> None:
    """Attempts left or not, it is put back and never FAILED."""
    transitions = tick([_run(_unchecked(attempt, 3))], NOW, _fixed)
    assert [t.status for t in transitions if isinstance(t, TaskTransition)] == [
        TaskStatus.PENDING,
        TaskStatus.READY,
    ]


def test_an_unchecked_task_in_a_cancelled_run_is_set_back_not_cancelled() -> None:
    """A cancel must not leave its unchecked rows: the task goes ready to set its table back."""
    task = _unchecked()
    downstream = _task("a.b", TaskStatus.PENDING, upstream=("a.a",))
    transitions = tick([_run(task, downstream, cancel=True)], NOW, _fixed)
    moves = [
        (t.task_id, t.expected, t.status, t.setting_back)
        for t in transitions
        if isinstance(t, TaskTransition)
    ]
    assert moves == [
        (task.id, TaskStatus.RUNNING, TaskStatus.READY, True),
        (downstream.id, TaskStatus.PENDING, TaskStatus.CANCELLED, False),
    ]
    assert not any(isinstance(t, RunTransition) for t in transitions)


@pytest.mark.parametrize("status", [TaskStatus.PENDING, TaskStatus.READY])
def test_an_unchecked_task_waiting_between_two_claims_is_set_back_by_a_cancel(
    status: TaskStatus,
) -> None:
    """A written task that waits for its checks again is readied to set back, not cancelled.

    Parameters
    ----------
    status
        The waiting status the snapshot saw.
    """
    waiting = _task("a.a", status, attempt=2, max_attempts=1, unchecked=True, committed=True)
    transitions = tick([_run(waiting, cancel=True)], NOW, _fixed)
    move = _only_move(transitions)
    assert (move.expected, move.status, move.setting_back, move.lost) == (
        status,
        TaskStatus.READY,
        True,
        False,
    )
    assert move.expected_claim == waiting.claim
    assert not any(isinstance(t, RunTransition) for t in transitions)


def test_a_pending_python_retry_with_a_baseline_is_set_back_by_a_cancel() -> None:
    """An earlier attempt may have left rows: the cancel readies the task to set back at once."""
    later = NOW + timedelta(minutes=5)
    retry = _task("a.a", TaskStatus.PENDING, attempt=1, next_attempt_at=later, baselined=True)
    below = _task("a.b", TaskStatus.PENDING, upstream=("a.a",))
    transitions = tick([_run(retry, below, cancel=True)], NOW, _fixed)
    moves = [
        (t.task_id, t.expected, t.status, t.setting_back, t.ready_at)
        for t in transitions
        if isinstance(t, TaskTransition)
    ]
    assert moves == [
        (retry.id, TaskStatus.PENDING, TaskStatus.READY, True, NOW),
        (below.id, TaskStatus.PENDING, TaskStatus.CANCELLED, False, None),
    ]
    assert not any(isinstance(t, RunTransition) for t in transitions)


@pytest.mark.parametrize("status", [TaskStatus.PENDING, TaskStatus.READY])
def test_a_waiting_task_without_unchecked_rows_is_still_cancelled(status: TaskStatus) -> None:
    """No stamp and no baseline, or no checks at all: the cancel ends the task as before.

    Parameters
    ----------
    status
        The waiting status the snapshot saw.
    """
    waiting = _task("a.a", status, attempt=1, committed=True)
    transitions = tick([_run(waiting, cancel=True)], NOW, _fixed)
    move = _only_move(transitions)
    assert (move.status, move.setting_back) == (TaskStatus.CANCELLED, False)
    assert transitions[-1] == RunTransition(
        RUN, RunStatus.RUNNING, RunStatus.CANCELLED, counts={"cancelled": 1}
    )


def test_a_pending_retry_with_a_baseline_waits_for_its_retry_without_a_cancel() -> None:
    """Without a cancel, a waiting task with a baseline is left to its retry."""
    later = NOW + timedelta(minutes=5)
    retry = _task("a.a", TaskStatus.PENDING, attempt=1, next_attempt_at=later, baselined=True)
    assert tick([_run(retry)], NOW, _fixed) == []


def test_a_written_task_without_checks_still_succeeds() -> None:
    """Without checks, the committed write is counted as before."""
    transitions = tick([_run(_unchecked(unchecked=False))], NOW, _fixed)
    first = transitions[0]
    assert isinstance(first, TaskTransition)
    assert (first.status, first.lost, first.unchecked) == (TaskStatus.SUCCEEDED, True, False)


def test_an_unwritten_task_with_checks_is_retried_as_before() -> None:
    """Checks but no stamp and no evidence: the three old outcomes, none of them 'unchecked'."""
    for snapshot, expected in (
        (_unchecked(1, 3, committed=False, unchecked=False), TaskStatus.PENDING),
        (_unchecked(3, 3, committed=False, unchecked=False), TaskStatus.FAILED),
    ):
        first = tick([_run(snapshot)], NOW, _fixed)[0]
        assert isinstance(first, TaskTransition)
        assert (first.status, first.unchecked) == (expected, False)
    cancelled = tick([_run(_unchecked(committed=False, unchecked=False), cancel=True)], NOW, _fixed)
    assert _statuses(cancelled) == {"a.a": TaskStatus.CANCELLED}


_PAST = NOW - timedelta(seconds=1)


def _expired(**kwargs: object) -> TaskSnapshot:
    """Build the running task `a.a` with an expired lease and the given facts.

    Parameters
    ----------
    **kwargs
        Passed on to `_task`.

    Returns
    -------
    TaskSnapshot
        The task.
    """
    return _task("a.a", TaskStatus.RUNNING, lease_owner="w1", lease_expires_at=_PAST, **kwargs)  # ty: ignore[invalid-argument-type]


def _only_move(transitions: list) -> TaskTransition:
    """Return the single task transition of the first step of a tick.

    Parameters
    ----------
    transitions
        `tick` output.

    Returns
    -------
    TaskTransition
        The first task transition.
    """
    return next(t for t in transitions if isinstance(t, TaskTransition))


def test_a_lost_python_task_that_may_have_written_on_its_last_attempt_is_put_back_to_set_back() -> (
    None
):
    """No attempts left and it may have written: ready with the flag, not failed."""
    task = _expired(attempt=3, max_attempts=3, may_have_written=True)
    out = tick([_run(task)], NOW, _fixed)
    moves = [t for t in out if isinstance(t, TaskTransition)]
    assert len(moves) == 1
    [move] = moves
    assert (move.status, move.setting_back, move.lost) == (TaskStatus.READY, True, True)
    assert (move.ready_at, move.expected_owner, move.expected_claim) == (NOW, "w1", 3)
    assert not any(isinstance(t, RunTransition) for t in out)


def test_a_lost_python_task_with_attempts_left_is_retried_as_today() -> None:
    """Attempts left: its next attempt starts from the baseline, so it goes to pending."""
    task = _expired(attempt=1, max_attempts=3, may_have_written=True)
    move = _only_move(tick([_run(task)], NOW, _fixed))
    assert (move.status, move.setting_back) == (TaskStatus.PENDING, False)


def test_a_lost_task_of_a_cancelled_run_that_may_have_written_is_set_back_not_cancelled() -> None:
    """A cancel is no reason to leave what the process wrote."""
    task = _expired(attempt=1, max_attempts=3, may_have_written=True)
    out = tick([_run(task, cancel=True)], NOW, _fixed)
    assert _statuses(out)["a.a"] is TaskStatus.READY
    assert _only_move(out).setting_back


def test_a_stamped_task_of_a_cancelled_run_is_set_back_instead_of_cancelled() -> None:
    """A stamped task of a cancelled run keeps its unchecked rows unless it is set back."""
    out = tick([_run(_unchecked(attempt=1, max_attempts=3), cancel=True)], NOW, _fixed)
    assert _statuses(out)["a.a"] is TaskStatus.READY
    assert any(isinstance(t, TaskTransition) and t.setting_back for t in out)


def test_committed_still_wins_over_may_have_written() -> None:
    """The write run shows the commit: the data is there and the task succeeded."""
    task = _expired(attempt=3, max_attempts=3, may_have_written=True, committed=True)
    move = _only_move(tick([_run(task)], NOW, _fixed))
    assert (move.status, move.setting_back) == (TaskStatus.SUCCEEDED, False)


def test_a_lost_set_back_claim_is_put_back_once_more() -> None:
    """The first lost claim is not the last: back to ready with the flag."""
    task = _expired(setting_back=True, set_back_claims=1)
    out = tick([_run(task)], NOW, _fixed)
    assert _statuses(out)["a.a"] is TaskStatus.READY
    assert _only_move(out).setting_back


def test_a_lost_set_back_claim_wins_over_the_other_branches() -> None:
    """Whatever else the snapshot says, a task that is setting back is set back again."""
    task = _expired(
        attempt=3,
        max_attempts=3,
        setting_back=True,
        set_back_claims=1,
        unchecked=True,
        committed=True,
        may_have_written=True,
    )
    move = _only_move(tick([_run(task, cancel=True)], NOW, _fixed))
    assert (move.status, move.setting_back) == (TaskStatus.READY, True)


def test_a_set_back_claim_lost_twice_ends_the_task_with_the_lost_set_back_message() -> None:
    """At the limit the task fails and tells the user what to do."""
    task = _expired(setting_back=True, set_back_claims=MAX_SET_BACK_CLAIMS)
    move = _only_move(tick([_run(task)], NOW, _fixed))
    assert move.status is TaskStatus.FAILED
    assert (move.error_message, move.setting_back, move.ends_set_back) == (
        SET_BACK_LOST,
        False,
        True,
    )


def test_a_set_back_claim_lost_twice_in_a_cancelled_run_ends_cancelled() -> None:
    """The cancel ends it; the message still says the table was not set back."""
    task = _expired(setting_back=True, set_back_claims=MAX_SET_BACK_CLAIMS)
    out = tick([_run(task, cancel=True)], NOW, _fixed)
    move = _only_move(out)
    assert move.status is TaskStatus.CANCELLED
    assert (move.error_message, move.ends_set_back) == (SET_BACK_LOST, True)


def test_the_cancel_loop_leaves_a_task_that_is_setting_back_alone() -> None:
    """Its set-back is the cancel's work, so the cancel does not end it."""
    task = _task("a.a", TaskStatus.READY, setting_back=True, set_back_claims=1)
    assert "a.a" not in _statuses(tick([_run(task, cancel=True)], NOW, _fixed))


def test_a_drained_task_that_is_setting_back_is_readied_in_a_cancelled_run() -> None:
    """A drain sends the set-back claim back to pending; the cancel readies it, never ends it."""
    task = _task("a.a", TaskStatus.PENDING, setting_back=True, set_back_claims=0)
    move = _only_move(tick([_run(task, cancel=True)], NOW, _fixed))
    assert (move.expected, move.status) == (TaskStatus.PENDING, TaskStatus.READY)


def test_the_cascades_wait_for_a_task_that_is_setting_back() -> None:
    """In a cancelled run, downstream is cancelled at once; the task setting back is left alone."""
    back = _task("a.a", TaskStatus.READY, setting_back=True, set_back_claims=1)
    below = _task("a.b", TaskStatus.PENDING, upstream=("a.a",))
    out = tick([_run(back, below, cancel=True)], NOW, _fixed)
    assert _statuses(out) == {"a.b": TaskStatus.CANCELLED}
    assert not any(isinstance(t, RunTransition) for t in out)


def test_a_task_put_back_in_this_tick_is_not_cancelled_in_it() -> None:
    """The cancel loop reads the state left by the reclaim, so it must skip the new flag too."""
    task = _expired(attempt=1, max_attempts=3, may_have_written=True)
    below = _task("a.b", TaskStatus.PENDING, upstream=("a.a",))
    out = tick([_run(task, below, cancel=True)], NOW, _fixed)
    assert _statuses(out) == {"a.a": TaskStatus.READY, "a.b": TaskStatus.CANCELLED}


# -- failure codes ------------------------------------------------------------


def _codes(transitions: list) -> dict[str, str | None]:
    """Index the failure code of each task transition that has one by asset.

    Parameters
    ----------
    transitions
        The tick's output.

    Returns
    -------
    dict
        Asset name to the transition's `failure_code`, for the transitions that carry one.
    """
    ids = {uuid.uuid5(uuid.NAMESPACE_URL, name): name for name in ("a.a", "a.b", "a.c")}
    return {
        ids[t.task_id]: t.failure_code
        for t in transitions
        if isinstance(t, TaskTransition) and t.failure_code is not None
    }


def _lost_task(attempt: int = 1, max_attempts: int = 3) -> TaskSnapshot:
    """Build a running task whose lease expired.

    Parameters
    ----------
    attempt
        Attempts so far.
    max_attempts
        Attempt limit.

    Returns
    -------
    TaskSnapshot
        The snapshot.
    """
    return _task(
        "a.a",
        TaskStatus.RUNNING,
        attempt=attempt,
        max_attempts=max_attempts,
        lease_owner="w1",
        lease_expires_at=NOW - timedelta(seconds=1),
    )


def test_a_task_that_lost_its_last_attempt_fails_with_worker_lost() -> None:
    """The reclaim that fails a task names why."""
    out = tick([_run(_lost_task(3, 3), _task("a.b", upstream=("a.a",)))], NOW, _fixed)

    assert _codes(out) == {"a.a": "worker_lost", "a.b": "upstream_failed"}


def test_a_task_lost_with_attempts_left_is_retried_without_a_code() -> None:
    """The retry is no failure of the task; the store gives the attempt its `worker_lost`."""
    assert _codes(tick([_run(_lost_task(1, 3))], NOW, _fixed)) == {}


def test_a_cancel_gives_the_cancelled_code_whichever_loop_ends_the_task() -> None:
    """A lost task, a waiting task and a task below a cancelled one all end `cancelled`."""
    out = tick(
        [
            _run(
                _lost_task(),
                _task("a.b", TaskStatus.READY),
                _task("a.c", upstream=("a.b",)),
                cancel=True,
            )
        ],
        NOW,
        _fixed,
    )

    assert _codes(out) == {"a.a": "cancelled", "a.b": "cancelled", "a.c": "cancelled"}


def test_a_lost_set_back_ends_with_worker_lost_or_cancelled() -> None:
    """The end of a set-back that kept being lost fails the task, or cancels it in a cancel."""
    task = _expired(setting_back=True, set_back_claims=MAX_SET_BACK_CLAIMS)

    assert _codes(tick([_run(task)], NOW, _fixed)) == {"a.a": "worker_lost"}
    assert _codes(tick([_run(task, cancel=True)], NOW, _fixed)) == {"a.a": "cancelled"}


def test_a_task_put_back_to_set_its_table_back_carries_no_code() -> None:
    """The next worker ends it with a code of its own."""
    task = _expired(setting_back=True, set_back_claims=0)

    assert _codes(tick([_run(task)], NOW, _fixed)) == {}


def _at_the_bound(task: TaskSnapshot, claims: int = MAX_CHECK_CLAIMS) -> TaskSnapshot:
    """Give a task the number of claims it took only to run its checks.

    Parameters
    ----------
    task
        The task.
    claims
        Its check claims.

    Returns
    -------
    TaskSnapshot
        The task with `check_claims` set.
    """
    return dataclasses.replace(task, check_claims=claims)


def test_a_task_lost_in_its_checks_at_the_bound_is_sent_to_set_its_table_back() -> None:
    """#99: the last check claim was lost too; the next worker sets the table back and fails it."""
    task = _at_the_bound(_unchecked())
    below = _task("a.b", TaskStatus.PENDING, upstream=("a.a",))
    out = tick([_run(task, below)], NOW, _fixed)

    move = _only_move(out)
    assert (move.expected, move.status, move.lost, move.setting_back) == (
        TaskStatus.RUNNING,
        TaskStatus.READY,
        True,
        True,
    )
    assert (move.expected_owner, move.expected_claim, move.ready_at) == ("w1", 1, NOW)
    assert move.error_message == CHECKS_UNFINISHED_MESSAGE
    assert move.failure_code is FailureCode.CHECKS_UNFINISHED
    assert move.checks_unfinished
    assert [t for t in out if isinstance(t, TaskTransition)] == [move]


def test_a_task_lost_in_its_checks_below_the_bound_is_still_put_back_for_its_checks() -> None:
    """One claim short of the bound: the checks run again, as before."""
    out = tick([_run(_at_the_bound(_unchecked(), MAX_CHECK_CLAIMS - 1))], NOW, _fixed)

    move = _only_move(out)
    assert (move.status, move.unchecked, move.setting_back) == (TaskStatus.PENDING, True, False)
    assert not move.checks_unfinished


def test_a_cancel_wins_over_the_bound_on_checks() -> None:
    """A cancelled run sets the table back for the cancel, with the cancel's own text."""
    out = tick([_run(_at_the_bound(_unchecked()), cancel=True)], NOW, _fixed)

    move = _only_move(out)
    assert (move.status, move.setting_back) == (TaskStatus.READY, True)
    assert (move.error_message, move.failure_code, move.checks_unfinished) == (None, None, False)


@pytest.mark.parametrize(
    ("status", "ready_at"), [(TaskStatus.PENDING, NOW), (TaskStatus.READY, None)]
)
def test_a_waiting_task_whose_checks_could_not_start_at_the_bound_is_sent_to_set_back(
    status: TaskStatus, ready_at: datetime | None
) -> None:
    """#99: the checks failed to start on the last check claim; the backoff is not waited out.

    Parameters
    ----------
    status
        The waiting status the snapshot saw.
    ready_at
        The ready time the move stamps: a pending task becomes ready now.
    """
    later = NOW + timedelta(minutes=10)
    waiting = _at_the_bound(
        _task("a.a", status, attempt=6, max_attempts=1, next_attempt_at=later, unchecked=True)
    )
    below = _task("a.b", TaskStatus.PENDING, upstream=("a.a",))
    out = tick([_run(waiting, below)], NOW, _fixed)

    move = _only_move(out)
    assert (move.expected, move.status, move.lost, move.setting_back) == (
        status,
        TaskStatus.READY,
        False,
        True,
    )
    assert (move.expected_claim, move.ready_at) == (waiting.claim, ready_at)
    assert (move.error_message, move.failure_code) == (
        CHECKS_UNFINISHED_MESSAGE,
        FailureCode.CHECKS_UNFINISHED,
    )
    assert move.checks_unfinished
    assert [t for t in out if isinstance(t, TaskTransition)] == [move]


def test_a_waiting_task_below_the_bound_or_already_setting_back_is_left_alone() -> None:
    """Only a task that awaits its checks, at the bound, and is not setting back yet is moved."""
    later = NOW + timedelta(minutes=10)
    below = _at_the_bound(
        _task("a.a", TaskStatus.PENDING, next_attempt_at=later, unchecked=True),
        MAX_CHECK_CLAIMS - 1,
    )
    setting = _at_the_bound(
        _task("a.b", TaskStatus.READY, unchecked=True, setting_back=True, set_back_claims=1)
    )
    without_write = _at_the_bound(_task("a.c", TaskStatus.PENDING, next_attempt_at=later))

    assert [t for t in tick([_run(below, setting, without_write)], NOW, _fixed) if t.run_id] == []
