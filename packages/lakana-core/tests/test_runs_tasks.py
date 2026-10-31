"""Claims, lease renewal and attempt outcomes.

Lease and outcome times are the database's (`db_now`); the `clock` fixture pins
that clock so the tests can assert exact instants.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session
from test_projects_dag import make_asset
from test_projects_deploys import _deployed, _repo

import lakana_core.runs.tasks as tasks_module
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    OWN_TIP_NO_SNAPSHOT,
    AttemptOutcome,
    QueryRun,
    QueryRunStatus,
    Run,
    RunStatus,
    TaskAttempt,
    TaskRun,
    TaskStatus,
)
from lakana_core.runs.checks import RUN_CANCELLED_MESSAGE, RUN_STOPPED_MESSAGE
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService
from lakana_core.runs.store import scheduler_tick
from lakana_core.runs.tasks import (
    Baseline,
    ClaimedTask,
    FailureKind,
    LeaseState,
    OwnTip,
    Written,
    awaits_checks,
    claim_task,
    fail_run,
    mark_written,
    read_baseline,
    read_own_tip,
    read_written,
    record_attempt,
    record_baseline,
    renew_lease,
)

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
_BRONZE = NamespacePath.from_parts(["bronze"])


class Clock:
    """A settable stand-in for the database clock."""

    def __init__(self) -> None:
        """Start at `NOW`."""
        self.now = NOW


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Pin `db_now` as seen by the task functions.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.

    Returns
    -------
    Clock
        The clock; set `.now` to move time.
    """
    pinned = Clock()
    monkeypatch.setattr(tasks_module, "db_now", lambda session: pinned.now)
    return pinned


def _half() -> float:
    """No jitter.

    Returns
    -------
    float
        0.5.
    """
    return 0.5


@pytest.fixture
def run_id(session: Session, tmp_path: Path, principal: PrincipalRef, clock: Clock) -> uuid.UUID:
    """Start a run whose two independent tasks are ready.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The admin.
    clock
        The pinned database clock.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[_BRONZE],
        tables=[TableRef("dev", _BRONZE, "events")],
    )
    _deployed(
        session,
        principal,
        _repo(session),
        "a" * 40,
        make_asset("silver.a", ("bronze.events",)),
        make_asset("silver.b", ("bronze.events",)),
    )
    run = RunService(session, AllowAllAuthorization()).start(
        principal, "dev", "analytics", Selection()
    )
    scheduler_tick(session, clock.now, _half)
    return run.id


def _task(session: Session, task_run_id: uuid.UUID) -> TaskRun:
    """Re-read a task run.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        Its id.

    Returns
    -------
    TaskRun
        The fresh row.
    """
    session.expire_all()
    task = session.get(TaskRun, task_run_id)
    assert task is not None
    return task


def _attempts(session: Session, task_run_id: uuid.UUID) -> list[TaskAttempt]:
    """Return a task run's attempts, oldest first.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        Its id.

    Returns
    -------
    list of TaskAttempt
        The rows.
    """
    session.expire_all()
    return list(
        session.scalars(
            select(TaskAttempt)
            .where(TaskAttempt.task_run_id == task_run_id)
            .order_by(TaskAttempt.started_at, TaskAttempt.attempt)
        )
    )


def _claimed(session: Session) -> ClaimedTask:
    """Claim one ready task as `w1`, and park its sibling under `w2`.

    Parking the sibling keeps the re-readied task the only candidate in tests
    that put it back on the queue.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    ClaimedTask
        The `w1` claim.
    """
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    assert claim_task(session, "w2", 60) is not None
    return claimed


def _requeue(session: Session, task_run_id: uuid.UUID, **values: object) -> None:
    """Put a task run back to READY the way the scheduler would.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        The task run.
    **values
        Extra columns to set.
    """
    session.execute(
        update(TaskRun).where(TaskRun.id == task_run_id).values(status=TaskStatus.READY, **values)
    )
    session.commit()


def test_claim_takes_each_ready_task_once(session: Session, run_id: uuid.UUID) -> None:
    """Two claims get the two ready tasks; a third finds nothing."""
    first = claim_task(session, "w1", 60)
    second = claim_task(session, "w2", 60)
    assert first is not None and second is not None
    assert {first.asset, second.asset} == {"silver.a", "silver.b"}
    assert claim_task(session, "w3", 60) is None
    task = _task(session, first.task_run_id)
    assert (
        task.status,
        task.attempt,
        task.lease_owner,
        task.lease_expires_at,
        task.started_at,
    ) == (
        TaskStatus.RUNNING,
        1,
        "w1",
        NOW + timedelta(seconds=60),
        NOW,
    )
    (attempt,) = _attempts(session, first.task_run_id)
    assert (attempt.attempt, attempt.worker, attempt.started_at, attempt.outcome) == (
        1,
        "w1",
        NOW,
        None,
    )
    assert first == ClaimedTask(
        first.task_run_id, run_id, first.asset, 1, first.timeout_seconds, 1, max_attempts=3
    )


def test_claim_is_fifo_by_ready_at(session: Session, run_id: uuid.UUID) -> None:
    """The task that became ready first is claimed first, whatever its id."""
    tasks = list(session.scalars(select(TaskRun).where(TaskRun.run_id == run_id)))
    older, newer = sorted(tasks, key=lambda task: task.id, reverse=True)
    older.ready_at = NOW - timedelta(minutes=5)
    newer.ready_at = NOW
    session.commit()
    first = claim_task(session, "w1", 60)
    assert first is not None and first.task_run_id == older.id


def test_claim_leases_from_the_database_clock(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """The lease runs from `db_now`, not from any worker-supplied time."""
    clock.now = NOW + timedelta(hours=1)
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    task = _task(session, claimed.task_run_id)
    assert task.lease_expires_at == clock.now + timedelta(seconds=60)


def test_a_reclaim_clears_the_previous_attempts_query_run(
    session: Session, run_id: uuid.UUID
) -> None:
    """Zombie protection: the lockdown's run-context check no longer matches the old QueryRun."""
    task = session.scalars(select(TaskRun).where(TaskRun.run_id == run_id)).first()
    run = session.get(Run, run_id)
    assert task is not None and run is not None
    stale = QueryRun(
        id=uuid.uuid4(),
        environment_id=run.environment_id,
        sql_text="select 1",
        status=QueryRunStatus.RUNNING,
        task_run_id=task.id,
    )
    session.add(stale)
    session.flush()
    task.query_run_id = stale.id
    session.commit()
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    assert _task(session, claimed.task_run_id).query_run_id is None


def test_a_lost_claim_race_moves_on_to_the_next_candidate(
    session: Session, run_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the conditional update matches nothing (another worker won), the next one is tried."""
    real = tasks_module._take
    calls: list[uuid.UUID] = []

    def lose_first(
        session: Session, task_run_id: uuid.UUID, worker: str, now: datetime, lease_seconds: int
    ) -> bool:
        calls.append(task_run_id)
        if len(calls) == 1:
            return False
        return real(session, task_run_id, worker, now, lease_seconds)

    monkeypatch.setattr(tasks_module, "_take", lose_first)
    assert claim_task(session, "w1", 60) is not None
    assert len(calls) == 2


def test_a_claim_that_keeps_losing_gives_up(
    session: Session, run_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After `_CLAIM_CANDIDATES` lost races the call reports an empty queue for this poll."""
    monkeypatch.setattr(tasks_module, "_take", lambda *args: False)
    assert claim_task(session, "w1", 60) is None


def test_take_refuses_a_task_that_is_no_longer_ready(session: Session, run_id: uuid.UUID) -> None:
    """The claim is a compare-and-swap on `status = ready`."""
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    assert tasks_module._take(session, claimed.task_run_id, "w2", NOW, 60) is False
    session.rollback()


def test_renewal_extends_reports_cancel_and_detects_a_lost_lease(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """HELD extends the lease; a cancel request is reported; another owner means LOST."""
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    clock.now = NOW + timedelta(seconds=20)
    later = clock.now
    assert renew_lease(session, claimed.task_run_id, "w1", claimed.claim, 60) is LeaseState.HELD
    assert _task(session, claimed.task_run_id).lease_expires_at == later + timedelta(seconds=60)
    run = session.get(Run, run_id)
    assert run is not None
    run.cancel_requested = True
    session.commit()
    assert (
        renew_lease(session, claimed.task_run_id, "w1", claimed.claim, 60)
        is LeaseState.CANCEL_REQUESTED
    )
    assert renew_lease(session, claimed.task_run_id, "w2", claimed.claim, 60) is LeaseState.LOST


def test_a_zombie_attempt_cannot_renew_a_reclaimed_task(
    session: Session, run_id: uuid.UUID
) -> None:
    """Same worker id re-claimed the task: the old attempt's renewal is LOST."""
    claimed = _claimed(session)
    record_attempt(
        session, claimed.task_run_id, "w1", claimed.claim, kind=FailureKind.TRANSIENT, rand=_half
    )
    _requeue(session, claimed.task_run_id)
    again = claim_task(session, "w1", 60)
    assert again is not None and again.attempt == claimed.attempt + 1
    before = _task(session, claimed.task_run_id).lease_expires_at
    assert renew_lease(session, claimed.task_run_id, "w1", claimed.claim, 60) is LeaseState.LOST
    assert _task(session, claimed.task_run_id).lease_expires_at == before
    assert renew_lease(session, claimed.task_run_id, "w1", again.claim, 60) is LeaseState.HELD


def test_success_finishes_the_task_and_the_attempt(session: Session, run_id: uuid.UUID) -> None:
    """kind=None is success: succeeded, lease cleared, rows recorded."""
    claimed = _claimed(session)
    assert record_attempt(
        session, claimed.task_run_id, "w1", claimed.claim, kind=None, rand=_half, rows_written=3
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.finished_at, task.lease_owner, task.error_message) == (
        TaskStatus.SUCCEEDED,
        NOW,
        None,
        None,
    )
    attempt = _attempts(session, claimed.task_run_id)[0]
    assert (attempt.outcome, attempt.rows_written, attempt.finished_at) == (
        AttemptOutcome.SUCCEEDED,
        3,
        NOW,
    )


def test_transient_failure_retries_with_backoff_then_fails(
    session: Session, run_id: uuid.UUID
) -> None:
    """Counted retries with `retry_delay · 2^(attempt-1)` until `max_attempts`, then failed."""
    claimed = _claimed(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.TRANSIENT,
        rand=_half,
        error_message="storage hiccup",
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.attempt, task.next_attempt_at, task.error_message) == (
        TaskStatus.PENDING,
        1,
        NOW + timedelta(seconds=30),
        "storage hiccup",
    )
    _requeue(session, claimed.task_run_id, attempt=task.max_attempts - 1)
    again = claim_task(session, "w1", 60)
    assert again is not None and again.task_run_id == claimed.task_run_id
    record_attempt(
        session,
        again.task_run_id,
        "w1",
        again.claim,
        kind=FailureKind.TIMEOUT,
        rand=_half,
        error_message="too slow",
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.finished_at) == (TaskStatus.FAILED, NOW)
    assert _attempts(session, claimed.task_run_id)[-1].outcome is AttemptOutcome.TIMED_OUT


def test_a_transient_failure_backs_off_from_the_database_clock(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """`next_attempt_at` is `db_now` plus the backoff, not a worker-supplied time."""
    claimed = _claimed(session)
    clock.now = NOW + timedelta(minutes=2)
    record_attempt(
        session, claimed.task_run_id, "w1", claimed.claim, kind=FailureKind.TRANSIENT, rand=_half
    )
    assert _task(session, claimed.task_run_id).next_attempt_at == clock.now + timedelta(seconds=30)


def test_permanent_failure_is_never_retried(session: Session, run_id: uuid.UUID) -> None:
    """Validation, permission and integrity failures fail on the first attempt."""
    claimed = _claimed(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.PERMANENT,
        rand=_half,
        error_message="bad SQL",
    )
    assert _task(session, claimed.task_run_id).status is TaskStatus.FAILED


def test_conflict_gets_one_immediate_uncounted_retry(session: Session, run_id: uuid.UUID) -> None:
    """The first ConflictError retries now, uncounted; the second backs off, counted."""
    claimed = _claimed(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.CONFLICT,
        rand=_half,
        error_message="changed",
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.attempt, task.next_attempt_at, task.conflict_retried) == (
        TaskStatus.PENDING,
        0,
        NOW,
        True,
    )
    _requeue(session, claimed.task_run_id)
    again = claim_task(session, "w1", 60)
    assert again is not None
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        again.claim,
        kind=FailureKind.CONFLICT,
        rand=_half,
        error_message="changed",
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.attempt, task.next_attempt_at) == (
        TaskStatus.PENDING,
        1,
        NOW + timedelta(seconds=30),
    )
    first, second = _attempts(session, claimed.task_run_id)
    assert (first.attempt, first.outcome, first.finished_at) == (1, AttemptOutcome.FAILED, NOW)
    assert (second.attempt, second.outcome, second.finished_at) == (1, AttemptOutcome.FAILED, NOW)


def test_release_is_uncounted_and_immediately_due(session: Session, run_id: uuid.UUID) -> None:
    """A drained worker releases its task; the attempt is not counted."""
    claimed = _claimed(session)
    record_attempt(
        session, claimed.task_run_id, "w1", claimed.claim, kind=FailureKind.RELEASED, rand=_half
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.attempt, task.next_attempt_at, task.lease_owner) == (
        TaskStatus.PENDING,
        0,
        NOW,
        None,
    )
    assert _attempts(session, claimed.task_run_id)[0].outcome is AttemptOutcome.RELEASED


def test_cancelled_is_terminal(session: Session, run_id: uuid.UUID) -> None:
    """A task interrupted by a cancel request ends cancelled."""
    claimed = _claimed(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.CANCELLED,
        rand=_half,
        error_message="cancelled",
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.finished_at) == (TaskStatus.CANCELLED, NOW)
    assert _attempts(session, claimed.task_run_id)[0].outcome is AttemptOutcome.CANCELLED


def test_a_worker_that_lost_its_lease_records_nothing(session: Session, run_id: uuid.UUID) -> None:
    """After a reclaim the old worker's outcome is dropped."""
    claimed = _claimed(session)
    task = _task(session, claimed.task_run_id)
    task.lease_owner = "w2"
    session.commit()
    assert (
        record_attempt(session, claimed.task_run_id, "w1", claimed.claim, kind=None, rand=_half)
        is False
    )
    assert _task(session, claimed.task_run_id).status is TaskStatus.RUNNING
    assert record_attempt(session, uuid.uuid4(), "w1", 1, kind=None, rand=_half) is False


def test_a_zombie_attempt_cannot_finish_a_reclaimed_task(
    session: Session, run_id: uuid.UUID
) -> None:
    """Same worker re-claimed the task: the first attempt's late outcome is dropped."""
    claimed = _claimed(session)
    record_attempt(
        session, claimed.task_run_id, "w1", claimed.claim, kind=FailureKind.TRANSIENT, rand=_half
    )
    _requeue(session, claimed.task_run_id)
    again = claim_task(session, "w1", 60)
    assert again is not None and again.attempt == claimed.attempt + 1
    assert (
        record_attempt(session, claimed.task_run_id, "w1", claimed.claim, kind=None, rand=_half)
        is False
    )
    assert _task(session, claimed.task_run_id).status is TaskStatus.RUNNING


def test_an_outcome_is_dropped_when_the_lease_is_lost_mid_write(
    session: Session, run_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the row changes between the read and the conditional update, nothing is written."""
    claimed = _claimed(session)

    def steal(*args: object) -> datetime:
        session.execute(
            update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(lease_owner="w2")
        )
        return NOW

    monkeypatch.setattr(tasks_module, "next_attempt_time", steal)
    assert (
        record_attempt(
            session,
            claimed.task_run_id,
            "w1",
            claimed.claim,
            kind=FailureKind.TRANSIENT,
            rand=_half,
        )
        is False
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "w1")
    assert _attempts(session, claimed.task_run_id)[0].outcome is None


def test_fail_run_flags_the_run_for_cancellation(session: Session, run_id: uuid.UUID) -> None:
    """An integrity failure fails the run through the scheduler."""
    fail_run(session, run_id, "code integrity check failed")
    run = session.get(Run, run_id)
    assert run is not None
    session.refresh(run)
    assert (run.error_message, run.cancel_requested) == ("code integrity check failed", True)


def test_the_waiting_tasks_of_a_run_failed_by_an_integrity_check_say_so(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """The scheduler reads the run's own error message, so nobody is said to have cancelled."""
    fail_run(session, run_id, "code integrity check failed")
    scheduler_tick(session, clock.now, _half)
    tasks = session.scalars(
        select(TaskRun).where(TaskRun.run_id == run_id).execution_options(populate_existing=True)
    ).all()
    assert {(t.status, t.error_message) for t in tasks} == {
        (TaskStatus.CANCELLED, RUN_STOPPED_MESSAGE)
    }


def test_the_waiting_tasks_of_a_cancelled_run_say_it_was_cancelled(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """A cancel by a user sets only the flag, and its tasks read the cancel text."""
    session.execute(update(Run).where(Run.id == run_id).values(cancel_requested=True))
    session.commit()
    scheduler_tick(session, clock.now, _half)
    tasks = session.scalars(
        select(TaskRun).where(TaskRun.run_id == run_id).execution_options(populate_existing=True)
    ).all()
    assert {(t.status, t.error_message) for t in tasks} == {
        (TaskStatus.CANCELLED, RUN_CANCELLED_MESSAGE)
    }


def test_in_transaction_work_lands_with_the_attempt(session: Session, run_id: uuid.UUID) -> None:
    """Work handed to `record_attempt` commits with the attempt."""
    claimed = _claimed(session)
    assert record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.PERMANENT,
        rand=_half,
        error_message="code integrity check failed",
        in_transaction=lambda inner: fail_run(
            inner, run_id, "code integrity check failed", commit=False
        ),
    )
    run = session.get(Run, run_id)
    assert run is not None
    session.refresh(run)
    assert (run.cancel_requested, _task(session, claimed.task_run_id).status) == (
        True,
        TaskStatus.FAILED,
    )


def test_in_transaction_work_that_breaks_leaves_the_attempt_open(
    session: Session, run_id: uuid.UUID
) -> None:
    """When the work raises, neither it nor the attempt's record is written."""
    claimed = _claimed(session)

    def breaks(inner: Session) -> None:
        fail_run(inner, run_id, "code integrity check failed", commit=False)
        raise RuntimeError("the database went away")

    with pytest.raises(RuntimeError, match="went away"):
        record_attempt(
            session,
            claimed.task_run_id,
            "w1",
            claimed.claim,
            kind=FailureKind.PERMANENT,
            rand=_half,
            in_transaction=breaks,
        )
    run = session.get(Run, run_id)
    assert run is not None
    session.refresh(run)
    assert run.cancel_requested is False
    assert _task(session, claimed.task_run_id).status is TaskStatus.RUNNING
    assert _attempts(session, claimed.task_run_id)[0].outcome is None


def test_in_transaction_work_is_not_run_for_a_lost_claim(
    session: Session, run_id: uuid.UUID
) -> None:
    """A worker whose claim was taken over fails nothing."""
    claimed = _claimed(session)
    called: list[bool] = []
    assert not record_attempt(
        session,
        claimed.task_run_id,
        "someone-else",
        claimed.claim,
        kind=FailureKind.PERMANENT,
        rand=_half,
        in_transaction=lambda inner: called.append(True),
    )
    assert called == []


def test_claim_skips_runs_with_a_cancel_request(session: Session, run_id: uuid.UUID) -> None:
    """A READY task of a cancel-requested run is not claimable before the scheduler cancels it."""
    fail_run(session, run_id, "integrity")
    assert claim_task(session, "w1", 60) is None


def test_claim_skips_finished_runs(session: Session, run_id: uuid.UUID) -> None:
    """A task of a run that is no longer running is not claimable."""
    session.execute(update(Run).where(Run.id == run_id).values(status=RunStatus.CANCELLED))
    session.commit()
    assert claim_task(session, "w1", 60) is None


def _set_back(session: Session, run_id: uuid.UUID) -> uuid.UUID:
    """Mark one ready task of the run as setting its table back, and cancel-request the run.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.

    Returns
    -------
    uuid.UUID
        The task run marked.
    """
    task_id = session.scalars(
        select(TaskRun.id).where(TaskRun.run_id == run_id).order_by(TaskRun.asset)
    ).first()
    assert task_id is not None
    session.execute(update(TaskRun).where(TaskRun.id == task_id).values(setting_back=True))
    session.execute(update(Run).where(Run.id == run_id).values(cancel_requested=True))
    session.commit()
    return task_id


def test_a_task_setting_its_table_back_is_claimed_in_a_cancel_requested_run(
    session: Session, run_id: uuid.UUID
) -> None:
    """Setting the table back is the cancel's own work: only that task passes the request."""
    task_id = _set_back(session, run_id)
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    assert (claimed.task_run_id, claimed.setting_back) == (task_id, True)
    assert claim_task(session, "w2", 60) is None
    task = _task(session, task_id)
    assert (task.attempt, task.claim_count, task.set_back_claims) == (0, 1, 1)
    assert claimed.attempt == 0
    (attempt,) = _attempts(session, task_id)
    assert (attempt.attempt, attempt.worker, attempt.outcome) == (0, "w1", None)


def test_a_normal_claim_is_not_a_set_back_claim(session: Session, run_id: uuid.UUID) -> None:
    """The flag is read from the row: an ordinary claim counts an attempt and no set-back claim."""
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    assert claimed.setting_back is False
    task = _task(session, claimed.task_run_id)
    assert (task.attempt, task.claim_count, task.set_back_claims) == (1, 1, 0)


def _awaiting_checks(
    session: Session, run_id: uuid.UUID, *, setting_back: bool = False
) -> uuid.UUID:
    """Stamp the first ready task of the run written, with checks, as its write's commit would.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.
    setting_back
        Whether the task is also to set its table back.

    Returns
    -------
    uuid.UUID
        The task run.
    """
    task_id = session.scalars(
        select(TaskRun.id).where(TaskRun.run_id == run_id).order_by(TaskRun.asset)
    ).first()
    assert task_id is not None
    session.execute(
        update(TaskRun)
        .where(TaskRun.id == task_id)
        .values(has_checks=True, written_table_id=uuid.uuid4(), setting_back=setting_back)
    )
    session.commit()
    return task_id


def test_a_claim_of_a_task_that_awaits_its_checks_counts_a_check_claim(
    session: Session, run_id: uuid.UUID
) -> None:
    """#99: such a claim only runs the checks; it is counted for the bound, a build claim is not."""
    task_id = _awaiting_checks(session, run_id)
    claims = [claim_task(session, worker, 60) for worker in ("w1", "w2")]
    assert {c.task_run_id for c in claims if c is not None} >= {task_id}
    assert {_task(session, c.task_run_id).check_claims for c in claims if c is not None} == {0, 1}
    assert _task(session, task_id).check_claims == 1


def test_a_set_back_claim_of_a_task_that_awaits_its_checks_is_no_check_claim(
    session: Session, run_id: uuid.UUID
) -> None:
    """A claim that only sets the table back runs no check, so the bound does not count it."""
    task_id = _awaiting_checks(session, run_id, setting_back=True)
    claims = [claim_task(session, worker, 60) for worker in ("w1", "w2")]
    assert task_id in {c.task_run_id for c in claims if c is not None}
    task = _task(session, task_id)
    assert (task.check_claims, task.set_back_claims) == (0, 1)


def test_a_task_setting_its_table_back_is_not_claimed_in_a_finished_run(
    session: Session, run_id: uuid.UUID
) -> None:
    """The exception covers the cancel request only, never a run that is over."""
    _set_back(session, run_id)
    session.execute(update(Run).where(Run.id == run_id).values(status=RunStatus.CANCELLED))
    session.commit()
    assert claim_task(session, "w1", 60) is None


def test_the_cancel_request_still_stops_a_task_not_setting_back(
    session: Session, run_id: uuid.UUID
) -> None:
    """A ready task without the flag in a cancel-requested run is not claimed."""
    _set_back(session, run_id)
    session.execute(update(TaskRun).values(setting_back=False))
    session.commit()
    assert claim_task(session, "w1", 60) is None


def test_take_claims_a_set_back_task_across_a_cancel_request(
    session: Session, run_id: uuid.UUID
) -> None:
    """The conditional update itself lets the set-back task through and not the others."""
    task_id = _set_back(session, run_id)
    other = session.scalars(
        select(TaskRun.id).where(TaskRun.run_id == run_id, TaskRun.id != task_id)
    ).one()
    assert tasks_module._take(session, other, "w1", NOW, 60) is False
    session.rollback()
    assert tasks_module._take(session, task_id, "w1", NOW, 60) is True
    session.rollback()


def test_a_lease_renewal_of_a_set_back_claim_hears_no_cancel(
    session: Session, run_id: uuid.UUID
) -> None:
    """The set-back claim keeps going under a cancel request; a lost claim is still lost."""
    _set_back(session, run_id)
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None and claimed.setting_back
    assert renew_lease(session, claimed.task_run_id, "w1", claimed.claim, 60) is LeaseState.HELD
    assert renew_lease(session, claimed.task_run_id, "w2", claimed.claim, 60) is LeaseState.LOST
    assert renew_lease(session, claimed.task_run_id, "w1", 99, 60) is LeaseState.LOST


def _set_back_claim(session: Session, run_id: uuid.UUID) -> ClaimedTask:
    """Claim a task that follows a lost first attempt and only sets its table back.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.

    Returns
    -------
    ClaimedTask
        The claim; the task's attempt is 1 and its set-back claims 1.
    """
    task_id = _set_back(session, run_id)
    session.execute(update(TaskRun).where(TaskRun.id == task_id).values(attempt=1))
    session.commit()
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None and claimed.setting_back
    assert _task(session, task_id).set_back_claims == 1
    return claimed


def test_a_drained_set_back_claim_keeps_the_attempt_and_the_flag_and_is_not_a_loss(
    session: Session, run_id: uuid.UUID
) -> None:
    """A release is no loss: the claim added no attempt, so none is given back either."""
    claimed = _set_back_claim(session, run_id)
    record_attempt(
        session, claimed.task_run_id, "w1", claimed.claim, kind=FailureKind.RELEASED, rand=_half
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.attempt, task.setting_back, task.set_back_claims) == (
        TaskStatus.PENDING,
        1,
        True,
        0,
    )
    assert (task.next_attempt_at, task.lease_owner) == (NOW, None)
    assert _attempts(session, claimed.task_run_id)[-1].outcome is AttemptOutcome.RELEASED


@pytest.mark.parametrize(
    "kind",
    [None, FailureKind.TRANSIENT, FailureKind.TIMEOUT, FailureKind.CONFLICT],
)
def test_a_set_back_claim_ends_only_failed_or_cancelled(
    session: Session, run_id: uuid.UUID, kind: FailureKind | None
) -> None:
    """Any other kind would send a stamped task back to pending to be set back again."""
    claimed = _set_back_claim(session, run_id)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=kind,
        rand=_half,
        error_message="the set-back text",
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.attempt, task.error_message, task.conflict_retried) == (
        TaskStatus.FAILED,
        1,
        "the set-back text",
        False,
    )
    assert _attempts(session, claimed.task_run_id)[-1].outcome is AttemptOutcome.FAILED


def test_a_set_back_claim_is_not_turned_into_a_success_by_a_commit_that_was_found(
    session: Session, run_id: uuid.UUID
) -> None:
    """The write run of the lost attempt shows a commit; the set-back claim still ends failed."""
    claimed = _set_back_claim(session, run_id)
    _link_write(session, claimed, 3)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.PERMANENT,
        rand=_half,
        error_message="text",
        on_committed="committed",
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.error_message) == (TaskStatus.FAILED, "text")


@pytest.mark.parametrize("kind", [FailureKind.PERMANENT, FailureKind.TRANSIENT])
def test_a_set_back_claim_stopped_by_the_memory_stop_records_nothing(
    session: Session, run_id: uuid.UUID, kind: FailureKind
) -> None:
    """The stop's blame is for a build; the scheduler takes the claim over, bounded by its limit."""
    claimed = _set_back_claim(session, run_id)
    _link_write(session, claimed, 3)
    assert not record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=kind,
        rand=_half,
        error_message="memory",
        on_committed="committed",
        stopped=True,
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.lease_owner, task.error_message) == (TaskStatus.RUNNING, "w1", None)
    assert _attempts(session, claimed.task_run_id)[-1].outcome is None


def test_a_drained_set_back_claim_keeps_the_text_the_task_was_handed_back_with(
    session: Session, run_id: uuid.UUID
) -> None:
    """The stop's message is the reason the task ends with; a drain must not erase it."""
    task = _running_task(session, has_checks=True, baseline=True, max_attempts=1)
    assert _ended_by_the_stop(session, task, FailureKind.PERMANENT, "the memory text")
    claimed = claim_task(session, _WORKER, 60)
    assert claimed is not None and claimed.setting_back
    assert record_attempt(
        session, claimed.task_run_id, _WORKER, claimed.claim, kind=FailureKind.RELEASED, rand=_half
    )
    stored = _reload(session, task.id)
    assert (stored.status, stored.setting_back, stored.error_message) == (
        TaskStatus.PENDING,
        True,
        "the memory text",
    )


def test_a_set_back_claim_ends_cancelled(session: Session, run_id: uuid.UUID) -> None:
    """The cancel's own work ends the task cancelled."""
    claimed = _set_back_claim(session, run_id)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.CANCELLED,
        rand=_half,
        error_message="cancelled",
    )
    assert _task(session, claimed.task_run_id).status is TaskStatus.CANCELLED


def test_a_lease_renewal_of_an_ordinary_claim_still_hears_the_cancel(
    session: Session, run_id: uuid.UUID
) -> None:
    """Without the flag the cancel request reaches the keeper as before."""
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    session.execute(update(Run).where(Run.id == run_id).values(cancel_requested=True))
    session.commit()
    assert (
        renew_lease(session, claimed.task_run_id, "w1", claimed.claim, 60)
        is LeaseState.CANCEL_REQUESTED
    )


def test_take_refuses_when_cancel_lands_between_select_and_update(
    session: Session, run_id: uuid.UUID
) -> None:
    """The CAS itself checks the run, so a cancel after the candidate SELECT wins."""
    task_id = session.scalars(select(TaskRun.id).where(TaskRun.run_id == run_id)).first()
    assert task_id is not None
    session.execute(update(Run).where(Run.id == run_id).values(cancel_requested=True))
    assert tasks_module._take(session, task_id, "w1", NOW, 60) is False
    session.rollback()
    assert _task(session, task_id).status is TaskStatus.READY


def test_claim_order_is_the_same_with_null_ready_at(session: Session, run_id: uuid.UUID) -> None:
    """NULLs sort first on every database, so the order does not depend on the backend."""
    older, newer = list(session.scalars(select(TaskRun).where(TaskRun.run_id == run_id)))
    older.ready_at = None
    newer.ready_at = NOW - timedelta(hours=1)
    session.commit()
    first = claim_task(session, "w1", 60)
    assert first is not None and first.task_run_id == older.id


def test_a_missing_open_attempt_row_fails_loudly(session: Session, run_id: uuid.UUID) -> None:
    """Without its open attempt row the outcome is not silently dropped: raise and roll back."""
    claimed = _claimed(session)
    session.execute(delete(TaskAttempt).where(TaskAttempt.task_run_id == claimed.task_run_id))
    session.commit()
    with pytest.raises(RuntimeError, match="open attempt"):
        record_attempt(session, claimed.task_run_id, "w1", claimed.claim, kind=None, rand=_half)
    assert _task(session, claimed.task_run_id).status is TaskStatus.RUNNING


def test_fail_run_leaves_a_finished_run_alone(session: Session, run_id: uuid.UUID) -> None:
    """A late integrity failure must not rewrite a finished run."""
    session.execute(
        update(Run).where(Run.id == run_id).values(status=RunStatus.SUCCEEDED, error_message=None)
    )
    session.commit()
    fail_run(session, run_id, "too late")
    run = session.get(Run, run_id)
    assert run is not None
    session.refresh(run)
    assert (run.error_message, run.cancel_requested) == (None, False)


def test_a_zombie_of_an_uncounted_attempt_holds_nothing_after_the_re_claim(
    session: Session, run_id: uuid.UUID
) -> None:
    """ABA: a released attempt is not counted, so the re-claim has the same attempt number.

    Only the claim counter, which never goes back, tells the two claims apart.
    """
    claimed = _claimed(session)
    record_attempt(
        session, claimed.task_run_id, "w1", claimed.claim, kind=FailureKind.RELEASED, rand=_half
    )
    _requeue(session, claimed.task_run_id)
    again = claim_task(session, "w1", 60)
    assert again is not None
    assert (again.attempt, again.claim) == (claimed.attempt, claimed.claim + 1)
    before = _task(session, claimed.task_run_id).lease_expires_at
    assert renew_lease(session, claimed.task_run_id, "w1", claimed.claim, 60) is LeaseState.LOST
    assert _task(session, claimed.task_run_id).lease_expires_at == before
    assert (
        record_attempt(session, claimed.task_run_id, "w1", claimed.claim, kind=None, rand=_half)
        is False
    )
    assert _task(session, claimed.task_run_id).status is TaskStatus.RUNNING
    assert renew_lease(session, claimed.task_run_id, "w1", again.claim, 60) is LeaseState.HELD
    assert record_attempt(session, claimed.task_run_id, "w1", again.claim, kind=None, rand=_half)


def _link_write(session: Session, claimed: ClaimedTask, rows: int | None) -> None:
    """Give a claimed task a current write run with that evidence.

    Parameters
    ----------
    session
        Database session.
    claimed
        The claim.
    rows
        The write run's `rows_written`.
    """
    pipeline = session.get(Run, claimed.run_id)
    assert pipeline is not None
    write = QueryRun(
        id=uuid.uuid4(),
        environment_id=pipeline.environment_id,
        principal_id=pipeline.run_as_principal_id,
        sql_text="SELECT 1",
        status=QueryRunStatus.RUNNING,
        task_run_id=claimed.task_run_id,
        rows_written=rows,
    )
    session.add(write)
    session.flush()
    session.execute(
        update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(query_run_id=write.id)
    )
    session.commit()


@pytest.mark.parametrize("kind", list(FailureKind))
@pytest.mark.parametrize("rows", [0, 3])
def test_a_failure_of_an_attempt_whose_write_committed_is_recorded_as_success(
    session: Session, run_id: uuid.UUID, clock: Clock, kind: FailureKind, rows: int
) -> None:
    """Whatever the outcome (retry, release, failure, cancel), the evidence wins."""
    claimed = _claimed(session)
    _link_write(session, claimed, rows)
    assert record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=kind,
        rand=_half,
        error_message="boom",
        on_committed="it was committed",
    )
    task = _task(session, claimed.task_run_id)
    assert (task.status, task.attempt, task.error_message, task.next_attempt_at) == (
        TaskStatus.SUCCEEDED,
        1,
        "it was committed",
        None,
    )
    attempt = _attempts(session, claimed.task_run_id)[0]
    assert (attempt.outcome, attempt.rows_written, attempt.error_message) == (
        AttemptOutcome.SUCCEEDED,
        rows,
        "it was committed",
    )


@pytest.mark.parametrize("kind", [FailureKind.TRANSIENT, FailureKind.RELEASED])
def test_without_the_evidence_or_the_message_a_failure_is_recorded_as_before(
    session: Session, run_id: uuid.UUID, clock: Clock, kind: FailureKind
) -> None:
    """No evidence, or evidence but no `on_committed`, or no current write run: unchanged."""
    claimed = _claimed(session)
    _link_write(session, claimed, None)
    assert record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=kind,
        rand=_half,
        on_committed="it was committed",
    )
    assert _task(session, claimed.task_run_id).status is TaskStatus.PENDING


def test_evidence_alone_does_not_change_a_failure_recorded_without_the_message(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """`on_committed` is what asks for the rule."""
    claimed = _claimed(session)
    _link_write(session, claimed, 3)
    assert record_attempt(
        session, claimed.task_run_id, "w1", claimed.claim, kind=FailureKind.TRANSIENT, rand=_half
    )
    assert _task(session, claimed.task_run_id).status is TaskStatus.PENDING


def test_a_run_of_an_earlier_attempt_is_not_evidence_for_this_one(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """The link `task_runs.query_run_id` is the current attempt's; others prove nothing."""
    claimed = _claimed(session)
    pipeline = session.get(Run, claimed.run_id)
    assert pipeline is not None
    session.add(
        QueryRun(
            id=uuid.uuid4(),
            environment_id=pipeline.environment_id,
            principal_id=pipeline.run_as_principal_id,
            sql_text="SELECT 1",
            status=QueryRunStatus.FAILED,
            task_run_id=claimed.task_run_id,
            rows_written=9,
        )
    )
    session.commit()
    assert record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.TRANSIENT,
        rand=_half,
        on_committed="it was committed",
    )
    assert _task(session, claimed.task_run_id).status is TaskStatus.PENDING


def _with_checks(session: Session, task_run_id: uuid.UUID, has_checks: bool = True) -> None:
    """Set whether a task run's asset has checks.

    Parameters
    ----------
    session
        Database session; commits.
    task_run_id
        The task run.
    has_checks
        The value.
    """
    session.execute(update(TaskRun).where(TaskRun.id == task_run_id).values(has_checks=has_checks))
    session.commit()


def test_mark_written_needs_the_claim_and_checks(session: Session, run_id: uuid.UUID) -> None:
    claimed = _claimed(session)
    _with_checks(session, claimed.task_run_id)
    written = Written(uuid.uuid4(), 11, 12, publishes=True)

    assert not mark_written(session, claimed.task_run_id, "w2", claimed.claim, written)
    assert not mark_written(session, claimed.task_run_id, "w1", claimed.claim + 1, written)
    assert read_written(session, claimed.task_run_id) is None
    assert mark_written(session, claimed.task_run_id, "w1", claimed.claim, written)
    task = _task(session, claimed.task_run_id)
    assert (
        task.written_table_id,
        task.written_snapshot_before,
        task.written_snapshot_after,
        task.written_publishes,
    ) == (written.table_id, 11, 12, True)


def test_mark_written_refuses_a_task_that_is_not_running_or_has_no_checks(
    session: Session, run_id: uuid.UUID
) -> None:
    claimed = _claimed(session)
    written = Written(uuid.uuid4(), None, 12, publishes=False)
    assert not mark_written(session, claimed.task_run_id, "w1", claimed.claim, written)  # no checks
    _with_checks(session, claimed.task_run_id)
    session.execute(
        update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(status=TaskStatus.PENDING)
    )
    session.commit()
    assert not mark_written(session, claimed.task_run_id, "w1", claimed.claim, written)
    assert read_written(session, claimed.task_run_id) is None


def test_read_written_reads_committed_columns(session: Session, run_id: uuid.UUID) -> None:
    claimed = _claimed(session)
    assert read_written(session, claimed.task_run_id) is None
    table_id = uuid.uuid4()
    session.execute(
        update(TaskRun)
        .where(TaskRun.id == claimed.task_run_id)
        .values(
            written_table_id=table_id,
            written_snapshot_before=None,
            written_snapshot_after=7,
            written_publishes=False,
        )
    )
    session.commit()
    assert read_written(session, claimed.task_run_id) == Written(table_id, None, 7, False)


# --- a task that is written and has checks is ended only by the worker's verdict ---------

_WORKER = "w1"


def _running_task(
    session: Session,
    *,
    has_checks: bool,
    written: bool = False,
    write_run_rows: int | None = None,
    max_attempts: int | None = None,
    baseline: bool = False,
) -> TaskRun:
    """Claim a task as `_WORKER` and give it checks, a *written* stamp and a write run.

    Parameters
    ----------
    session
        Database session.
    has_checks
        Whether its asset has checks.
    written
        Whether the write's commit stamped it.
    write_run_rows
        When not `None`, the `rows_written` of a bound write run (the evidence of a commit).
    max_attempts
        When given, the task's attempt limit.
    baseline
        Whether a baseline was recorded (a Python table task with checks, before its process):
        without a stamp the task may then have written rows no check has passed.

    Returns
    -------
    TaskRun
        The running task run, with an open attempt.
    """
    claimed = _claimed(session)
    values: dict[str, object] = {"has_checks": has_checks}
    if baseline:
        values.update(baseline_snapshot_id=7, baseline_publishes=False)
    if written:
        values.update(
            written_table_id=uuid.uuid4(),
            written_snapshot_before=11,
            written_snapshot_after=12,
            written_publishes=False,
        )
    if max_attempts is not None:
        values["max_attempts"] = max_attempts
    session.execute(update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(**values))
    session.commit()
    if write_run_rows is not None:
        _link_write(session, claimed, write_run_rows)
    return _task(session, claimed.task_run_id)


def _reload(session: Session, task_run_id: uuid.UUID) -> TaskRun:
    """Re-read a task run.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        Its id.

    Returns
    -------
    TaskRun
        The row as committed.
    """
    return _task(session, task_run_id)


def _attempt(session: Session, task_run_id: uuid.UUID) -> TaskAttempt:
    """Return a task run's only attempt.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        Its id.

    Returns
    -------
    TaskAttempt
        The row.
    """
    (attempt,) = _attempts(session, task_run_id)
    return attempt


def test_record_attempt_never_turns_a_failed_check_into_success(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """The evidence of the write must not undo the worker's verdict."""
    task = _running_task(session, has_checks=True, written=True, write_run_rows=4)
    recorded = record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=FailureKind.PERMANENT,
        rand=_half,
        error_message="Check `gold.orders/unique_id` failed: 3 rows",
        on_committed="recording failed after the commit",
    )
    assert recorded
    stored = _reload(session, task.id)
    assert (stored.status, stored.error_message) == (
        TaskStatus.FAILED,
        "Check `gold.orders/unique_id` failed: 3 rows",
    )
    assert _attempt(session, task.id).outcome is AttemptOutcome.FAILED


def test_a_task_without_checks_still_counts_a_committed_write(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """The same call with `has_checks` false is recorded as success."""
    task = _running_task(session, has_checks=False, written=True, write_run_rows=4)
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=FailureKind.PERMANENT,
        rand=_half,
        error_message="boom",
        on_committed="recording failed after the commit",
    )
    stored = _reload(session, task.id)
    assert (stored.status, stored.error_message) == (
        TaskStatus.SUCCEEDED,
        "recording failed after the commit",
    )
    assert _attempt(session, task.id).rows_written == 4


@pytest.mark.parametrize("kind", [FailureKind.TRANSIENT, FailureKind.TIMEOUT, FailureKind.CONFLICT])
def test_a_retryable_failure_of_an_unchecked_task_never_fails_it(
    session: Session, run_id: uuid.UUID, clock: Clock, kind: FailureKind
) -> None:
    """Out of attempts or not, a written task with checks goes back to pending."""
    task = _running_task(session, has_checks=True, written=True, max_attempts=1)
    session.execute(update(TaskRun).where(TaskRun.id == task.id).values(conflict_retried=True))
    session.commit()
    stamp = (task.written_table_id, task.written_snapshot_before, task.written_snapshot_after)
    assert task.attempt == task.max_attempts == 1
    assert record_attempt(
        session, task.id, _WORKER, task.claim_count, kind=kind, rand=_half, error_message="x"
    )
    stored = _reload(session, task.id)
    assert (stored.status, stored.attempt, stored.finished_at) == (TaskStatus.PENDING, 1, None)
    assert stored.next_attempt_at == clock.now + timedelta(seconds=30)
    assert (
        stored.written_table_id,
        stored.written_snapshot_before,
        stored.written_snapshot_after,
    ) == stamp
    expected = AttemptOutcome.TIMED_OUT if kind is FailureKind.TIMEOUT else AttemptOutcome.FAILED
    assert _attempt(session, task.id).outcome is expected


@pytest.mark.parametrize("kind", [FailureKind.TRANSIENT, FailureKind.TIMEOUT])
def test_a_retryable_failure_of_a_task_without_checks_fails_it_on_its_last_attempt(
    session: Session, run_id: uuid.UUID, clock: Clock, kind: FailureKind
) -> None:
    """The same situation with `has_checks` false: out of attempts is a failure, as before."""
    task = _running_task(session, has_checks=False, written=True, max_attempts=1)
    assert record_attempt(
        session, task.id, _WORKER, task.claim_count, kind=kind, rand=_half, error_message="x"
    )
    assert _reload(session, task.id).status is TaskStatus.FAILED


def test_an_unwritten_task_with_checks_fails_on_its_last_attempt(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """No stamp, no verdict owed: running out of attempts fails it, as today."""
    task = _running_task(session, has_checks=True, written=False, max_attempts=1)
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=FailureKind.TRANSIENT,
        rand=_half,
        error_message="x",
    )
    stored = _reload(session, task.id)
    assert (stored.status, stored.finished_at) == (TaskStatus.FAILED, clock.now)


@pytest.mark.parametrize(
    "kind", [FailureKind.TRANSIENT, FailureKind.PERMANENT, FailureKind.TIMEOUT]
)
def test_the_evidence_never_succeeds_a_task_with_checks_whoever_asks(
    session: Session, run_id: uuid.UUID, clock: Clock, kind: FailureKind
) -> None:
    """The memory stop asks with `on_committed` too: a task with checks is not flipped."""
    task = _running_task(session, has_checks=True, written=True, write_run_rows=2)
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=kind,
        rand=_half,
        error_message="stopped",
        on_committed="it was committed",
    )
    stored = _reload(session, task.id)
    assert stored.status is not TaskStatus.SUCCEEDED
    assert stored.error_message == "stopped"
    assert _attempt(session, task.id).outcome is not AttemptOutcome.SUCCEEDED


@pytest.mark.parametrize(
    ("kind", "status", "attempt"),
    [
        (FailureKind.RELEASED, TaskStatus.PENDING, 0),
        (FailureKind.CANCELLED, TaskStatus.READY, 1),
    ],
)
def test_a_cancel_and_a_release_of_an_unchecked_task(
    session: Session,
    run_id: uuid.UUID,
    clock: Clock,
    kind: FailureKind,
    status: TaskStatus,
    attempt: int,
) -> None:
    """A release puts it back uncounted, with its stamp; a cancel hands it back."""
    task = _running_task(session, has_checks=True, written=True, write_run_rows=2)
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=kind,
        rand=_half,
        on_committed="it was committed",
    )
    stored = _reload(session, task.id)
    assert (stored.status, stored.attempt) == (status, attempt)
    assert stored.written_table_id is not None
    assert _attempt(session, task.id).outcome is (
        AttemptOutcome.RELEASED if kind is FailureKind.RELEASED else AttemptOutcome.CANCELLED
    )


def test_success_of_a_task_with_checks_is_the_workers_to_record(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """The worker that ran the checks ends it with `kind=None`: that is still success."""
    task = _running_task(session, has_checks=True, written=True)
    assert record_attempt(
        session, task.id, _WORKER, task.claim_count, kind=None, rand=_half, rows_written=4
    )
    assert _reload(session, task.id).status is TaskStatus.SUCCEEDED


def test_a_claim_carries_kind_checks_and_attempts(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """The worker decides its phases from what the claim says about the row."""
    session.execute(update(TaskRun).values(kind="source_check", has_checks=True, max_attempts=5))
    session.commit()
    claimed = claim_task(session, "w1", 60)
    assert claimed is not None
    assert (claimed.kind, claimed.has_checks, claimed.max_attempts) == ("source_check", True, 5)
    session.execute(update(TaskRun).values(kind="asset", has_checks=False, max_attempts=3))
    session.commit()
    again = claim_task(session, "w2", 60)
    assert again is not None
    assert (again.kind, again.has_checks, again.max_attempts) == ("asset", False, 3)
    assert ClaimedTask(uuid.uuid4(), uuid.uuid4(), "a.b", 1, 60, 1).kind == "asset"


@pytest.mark.parametrize(
    ("has_checks", "written", "rows", "expected"),
    [
        (True, True, None, True),
        (True, False, None, False),
        (False, True, None, False),
        (False, False, None, False),
        (True, False, 0, True),  # a rolled-back task: no stamp, the write's evidence stays
        (True, False, 4, True),
        (False, False, 4, False),  # a task without checks
    ],
)
def test_awaits_checks_needs_the_checks_and_the_stamp_or_the_evidence(
    session: Session,
    run_id: uuid.UUID,
    has_checks: bool,
    written: bool,
    rows: int | None,
    expected: bool,
) -> None:
    """One definition: checks, and the stamp or the write run's `rows_written` (0 included)."""
    task = _running_task(session, has_checks=has_checks, written=written, write_run_rows=rows)
    assert awaits_checks(session, task.id) is expected


@pytest.mark.parametrize("kind", [FailureKind.TRANSIENT, FailureKind.TIMEOUT])
def test_a_rolled_back_task_is_not_failed_or_succeeded_by_a_retryable_failure(
    session: Session, run_id: uuid.UUID, clock: Clock, kind: FailureKind
) -> None:
    """The stamp is gone but the write's evidence stays, so the task still awaits."""
    task = _running_task(session, has_checks=True, written=False, write_run_rows=4, max_attempts=1)
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=kind,
        rand=_half,
        error_message="x",
        on_committed="it was committed",
    )
    stored = _reload(session, task.id)
    assert (stored.status, stored.finished_at, stored.error_message) == (
        TaskStatus.PENDING,
        None,
        "x",
    )


def test_a_rolled_back_task_without_checks_twin_still_counts_the_write(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """The no-checks twin: the same evidence and call end succeeded."""
    task = _running_task(session, has_checks=False, written=False, write_run_rows=4, max_attempts=1)
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=FailureKind.TRANSIENT,
        rand=_half,
        error_message="x",
        on_committed="it was committed",
    )
    assert _reload(session, task.id).status is TaskStatus.SUCCEEDED


@pytest.mark.parametrize("written", [True, False])
def test_a_stopped_attempt_of_a_task_awaiting_its_checks_is_never_a_verdict(
    session: Session, run_id: uuid.UUID, clock: Clock, written: bool
) -> None:
    """The memory stop's blame (`PERMANENT`) is decided on the locked row: pending, not failed."""
    task = _running_task(session, has_checks=True, written=written, write_run_rows=2)
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=FailureKind.PERMANENT,
        rand=_half,
        error_message="stopped",
        on_committed="it was committed",
        stopped=True,
    )
    stored = _reload(session, task.id)
    assert (stored.status, stored.error_message) == (TaskStatus.PENDING, "stopped")
    assert _attempt(session, task.id).outcome is AttemptOutcome.FAILED


def test_a_stopped_attempt_of_a_task_not_awaiting_keeps_its_blame(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """Checks but no write: the blame stands. No checks, evidence: the success stands."""
    task = _running_task(session, has_checks=True, written=False)
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=FailureKind.PERMANENT,
        rand=_half,
        error_message="stopped",
        stopped=True,
    )
    assert _reload(session, task.id).status is TaskStatus.FAILED


def _ended_by_the_stop(
    session: Session, task: TaskRun, kind: FailureKind, message: str = "memory"
) -> bool:
    """Record the memory stop's end of a task, as the watchdog does.

    Parameters
    ----------
    session
        Database session.
    task
        The running task run.
    kind
        The blame: ``PERMANENT`` when certain, else ``TRANSIENT``.
    message
        The stop's message.

    Returns
    -------
    bool
        What `record_attempt` returned.
    """
    return record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=kind,
        rand=_half,
        error_message=message,
        failure_code=FailureCode.MEMORY,
        on_committed="it was committed",
        stopped=True,
    )


@pytest.mark.parametrize(
    ("kind", "attempts_left"),
    [
        (FailureKind.PERMANENT, False),
        (FailureKind.PERMANENT, True),
        (FailureKind.TRANSIENT, False),
    ],
)
def test_a_memory_stop_that_ends_a_task_that_may_have_written_hands_it_back(
    session: Session, run_id: uuid.UUID, kind: FailureKind, attempts_left: bool
) -> None:
    """Ready with `setting_back`, uncounted, the stop's message kept for the next worker."""
    task = _running_task(
        session, has_checks=True, baseline=True, max_attempts=2 if attempts_left else 1
    )
    assert _ended_by_the_stop(session, task, kind)
    stored = _reload(session, task.id)
    assert (stored.status, stored.setting_back, stored.attempt, stored.set_back_claims) == (
        TaskStatus.READY,
        True,
        task.attempt,
        0,
    )
    assert (stored.error_message, stored.lease_owner, stored.finished_at) == ("memory", None, None)
    assert stored.ready_at is not None
    assert _attempt(session, task.id).outcome is AttemptOutcome.FAILED


def test_a_memory_stop_with_attempts_left_and_uncertain_blame_still_retries(
    session: Session, run_id: uuid.UUID
) -> None:
    """The next attempt starts from the baseline, so nothing is handed back."""
    task = _running_task(session, has_checks=True, baseline=True, max_attempts=2)
    assert _ended_by_the_stop(session, task, FailureKind.TRANSIENT)
    stored = _reload(session, task.id)
    assert (stored.status, stored.setting_back) == (TaskStatus.PENDING, False)


@pytest.mark.parametrize(
    "setup",
    [
        {"has_checks": False},
        {"has_checks": True},
    ],
)
def test_a_memory_stop_of_a_task_that_never_wrote_ends_it_as_before(
    session: Session, run_id: uuid.UUID, setup: dict[str, bool]
) -> None:
    """No checks, or no baseline: nothing can have been left in the table."""
    task = _running_task(session, max_attempts=1, **setup)
    assert _ended_by_the_stop(session, task, FailureKind.PERMANENT)
    stored = _reload(session, task.id)
    assert (stored.status, stored.setting_back) == (TaskStatus.FAILED, False)


@pytest.mark.parametrize("written", [True, False])
def test_a_cancel_seen_after_a_write_hands_the_task_back(
    session: Session, run_id: uuid.UUID, written: bool
) -> None:
    """Stamped, or a baseline without a stamp: ready with `setting_back`, not cancelled."""
    task = _running_task(session, has_checks=True, written=written, baseline=not written)
    session.execute(update(TaskRun).where(TaskRun.id == task.id).values(error_message="old text"))
    session.commit()
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=FailureKind.CANCELLED,
        rand=_half,
        error_message="cancelled",
    )
    stored = _reload(session, task.id)
    assert (stored.status, stored.setting_back, stored.attempt) == (
        TaskStatus.READY,
        True,
        task.attempt,
    )
    # The cancel's own text is the stored reason; the earlier attempt's text is gone.
    assert (stored.error_message, stored.lease_owner, stored.finished_at) == (
        "cancelled",
        None,
        None,
    )
    assert _attempt(session, task.id).outcome is AttemptOutcome.CANCELLED


@pytest.mark.parametrize("has_checks", [True, False])
def test_a_cancel_before_any_write_ends_the_task_cancelled(
    session: Session, run_id: uuid.UUID, has_checks: bool
) -> None:
    """No baseline and no stamp (or no checks): nothing to set back."""
    task = _running_task(session, has_checks=has_checks)
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=FailureKind.CANCELLED,
        rand=_half,
        error_message="cancelled",
    )
    stored = _reload(session, task.id)
    assert (stored.status, stored.setting_back) == (TaskStatus.CANCELLED, False)


def test_a_stopped_attempt_of_a_task_without_checks_still_counts_a_committed_write(
    session: Session, run_id: uuid.UUID, clock: Clock
) -> None:
    """The twin: `stopped` changes nothing for a task without checks."""
    task = _running_task(session, has_checks=False, write_run_rows=2)
    assert record_attempt(
        session,
        task.id,
        _WORKER,
        task.claim_count,
        kind=FailureKind.PERMANENT,
        rand=_half,
        error_message="stopped",
        on_committed="it was committed",
        stopped=True,
    )
    assert _reload(session, task.id).status is TaskStatus.SUCCEEDED


def test_a_task_that_is_ended_awaits_nothing(session: Session, run_id: uuid.UUID) -> None:
    """The condition says "not ended": a verdict recorded, nothing is awaited any more."""
    task = _running_task(session, has_checks=True, written=True)
    session.execute(update(TaskRun).where(TaskRun.id == task.id).values(status=TaskStatus.FAILED))
    session.commit()
    assert awaits_checks(session, task.id) is False


def test_the_baseline_is_recorded_once_and_handed_back_to_every_later_claim(
    session: Session, run_id: uuid.UUID
) -> None:
    """I1: the table's state before the task run's first process; a retry does not move it."""
    claimed = _claimed(session)
    _with_checks(session, claimed.task_run_id)
    assert read_baseline(session, claimed.task_run_id) is None

    first = record_baseline(session, claimed.task_run_id, "w1", claimed.claim, Baseline(7, False))
    again = record_baseline(session, claimed.task_run_id, "w1", claimed.claim, Baseline(9, True))

    assert first == again == Baseline(7, False)
    session.rollback()
    assert read_baseline(session, claimed.task_run_id) == Baseline(7, False)
    task = _task(session, claimed.task_run_id)
    assert (task.baseline_snapshot_id, task.baseline_publishes) == (7, False)


def test_a_baseline_without_a_snapshot_is_a_recorded_baseline(
    session: Session, run_id: uuid.UUID
) -> None:
    """`baseline_publishes` says "recorded": a table that did not exist has no snapshot."""
    claimed = _claimed(session)
    _with_checks(session, claimed.task_run_id)

    stored = record_baseline(
        session, claimed.task_run_id, "w1", claimed.claim, Baseline(None, True)
    )
    later = record_baseline(session, claimed.task_run_id, "w1", claimed.claim, Baseline(5, False))

    assert stored == later == Baseline(None, True)


@pytest.mark.parametrize("case", ["wrong-worker", "wrong-claim", "pending", "no-checks"])
def test_a_stale_claim_records_no_baseline_and_cannot_overwrite_one(
    session: Session, run_id: uuid.UUID, case: str
) -> None:
    claimed = _claimed(session)
    worker, claim = "w1", claimed.claim
    if case != "no-checks":
        _with_checks(session, claimed.task_run_id)
    if case == "wrong-worker":
        worker = "w2"
    elif case == "wrong-claim":
        claim += 1
    elif case == "pending":
        session.execute(
            update(TaskRun)
            .where(TaskRun.id == claimed.task_run_id)
            .values(status=TaskStatus.PENDING)
        )
        session.commit()

    assert record_baseline(session, claimed.task_run_id, worker, claim, Baseline(7, False)) is None
    session.rollback()
    assert read_baseline(session, claimed.task_run_id) is None

    if case in {"wrong-worker", "wrong-claim"}:
        assert record_baseline(
            session, claimed.task_run_id, "w1", claimed.claim, Baseline(3, True)
        ) == Baseline(3, True)
        assert (
            record_baseline(session, claimed.task_run_id, worker, claim, Baseline(7, False)) is None
        )
        session.rollback()
        assert read_baseline(session, claimed.task_run_id) == Baseline(3, True)


def test_the_tip_of_a_task_runs_own_writes_is_read_from_the_committed_column(
    session: Session, run_id: uuid.UUID
) -> None:
    """N1: null is "never wrote"; 0 is "wrote, and the commit made no snapshot"; else a snapshot."""
    claimed = _claimed(session)
    assert read_own_tip(session, claimed.task_run_id) is None
    assert read_own_tip(session, uuid.uuid4()) is None
    for stored, expected in ((OWN_TIP_NO_SNAPSHOT, OwnTip(None)), (41, OwnTip(41))):
        session.execute(
            update(TaskRun).where(TaskRun.id == claimed.task_run_id).values(own_snapshot_id=stored)
        )
        session.commit()
        assert read_own_tip(session, claimed.task_run_id) == expected
    assert OWN_TIP_NO_SNAPSHOT == 0


# -- failure codes ------------------------------------------------------------


def _codes(session: Session, task_run_id: uuid.UUID) -> tuple[str | None, str | None]:
    """Read the stored failure codes of a task run and of its latest attempt.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        The task run.

    Returns
    -------
    tuple
        The task run's code and the latest attempt's.
    """
    return _task(session, task_run_id).failure_code, _attempts(session, task_run_id)[
        -1
    ].failure_code


def test_a_failure_stores_its_code_on_the_task_and_on_the_attempt(
    session: Session, run_id: uuid.UUID
) -> None:
    """A failure that ends the task stores the code next to the text, on both rows."""
    claimed = _claimed(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.PERMANENT,
        rand=_half,
        error_message="bad SQL",
        failure_code=FailureCode.QUERY_INVALID,
    )

    assert _codes(session, claimed.task_run_id) == ("query_invalid", "query_invalid")


def test_a_retried_failure_keeps_the_code_of_its_attempt(
    session: Session, run_id: uuid.UUID
) -> None:
    """A task that goes back to pending shows the code of the failure it will try again after."""
    claimed = _claimed(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.TIMEOUT,
        rand=_half,
        error_message="too slow",
        failure_code=FailureCode.TIMEOUT,
    )

    assert _task(session, claimed.task_run_id).status is TaskStatus.PENDING
    assert _codes(session, claimed.task_run_id) == ("timeout", "timeout")


def test_a_later_success_clears_the_code(session: Session, run_id: uuid.UUID) -> None:
    """A success stores no code, and a code passed with it is dropped."""
    claimed = _claimed(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=None,
        rand=_half,
        failure_code=FailureCode.INTERNAL,
    )

    assert _codes(session, claimed.task_run_id) == (None, None)


def test_a_failure_without_a_code_is_internal(session: Session, run_id: uuid.UUID) -> None:
    """Only an unexpected failure may arrive without a code, and it is recorded as `internal`."""
    claimed = _claimed(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.PERMANENT,
        rand=_half,
        error_message="what",
    )

    assert _codes(session, claimed.task_run_id) == ("internal", "internal")


def test_a_cancel_is_always_the_cancelled_code(session: Session, run_id: uuid.UUID) -> None:
    """A cancel needs no code from the caller."""
    claimed = _claimed(session)
    record_attempt(
        session, claimed.task_run_id, "w1", claimed.claim, kind=FailureKind.CANCELLED, rand=_half
    )

    assert _codes(session, claimed.task_run_id) == ("cancelled", "cancelled")


def test_a_release_is_no_failure_and_stores_no_code(session: Session, run_id: uuid.UUID) -> None:
    """A drain hands the task back; nothing failed."""
    claimed = _claimed(session)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.RELEASED,
        rand=_half,
        failure_code=FailureCode.INTERNAL,
    )

    assert _codes(session, claimed.task_run_id) == (None, None)


def test_a_failure_whose_write_committed_stores_no_code(
    session: Session, run_id: uuid.UUID
) -> None:
    """The commit evidence turns the failure into a success, and the code goes with it."""
    claimed = _claimed(session)
    _link_write(session, claimed, 3)
    record_attempt(
        session,
        claimed.task_run_id,
        "w1",
        claimed.claim,
        kind=FailureKind.TRANSIENT,
        rand=_half,
        error_message="boom",
        failure_code=FailureCode.INTERNAL,
        on_committed="it was committed",
    )

    assert _codes(session, claimed.task_run_id) == (None, None)


def test_a_drained_set_back_claim_keeps_the_code_the_task_was_handed_back_with(
    session: Session, run_id: uuid.UUID
) -> None:
    """The stop's code is the reason the task ends with; a drain must not erase it."""
    task = _running_task(session, has_checks=True, baseline=True, max_attempts=1)
    assert _ended_by_the_stop(session, task, FailureKind.PERMANENT, "the memory text")
    claimed = claim_task(session, _WORKER, 60)
    assert claimed is not None and claimed.setting_back
    record_attempt(
        session, claimed.task_run_id, _WORKER, claimed.claim, kind=FailureKind.RELEASED, rand=_half
    )

    assert _reload(session, task.id).failure_code == "memory"
