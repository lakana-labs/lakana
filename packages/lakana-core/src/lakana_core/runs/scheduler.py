"""The scheduler tick: a pure function from a snapshot of run state to transitions.

`lakana_core.runs.store` loads the snapshot and applies the transitions in one
transaction, each as a compare-and-swap on the state the snapshot saw; this
module touches no database, clock or random source of its own, which is what
the unit tests exercise.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from lakana_core.models import RunStatus, TaskStatus
from lakana_core.runs.checks import (
    CHECKS_UNFINISHED_MESSAGE,
    MAX_CHECK_CLAIMS,
    RUN_CANCELLED_MESSAGE,
    RUN_STOPPED_MESSAGE,
    SET_BACK_LOST,
)
from lakana_core.runs.failures import FailureCode

_ENDING_CODES: Mapping[TaskStatus, FailureCode] = {
    TaskStatus.CANCELLED: FailureCode.CANCELLED,
    TaskStatus.UPSTREAM_FAILED: FailureCode.UPSTREAM_FAILED,
}
"""The code of a task the scheduler ends without a failure of its own."""

MAX_BACKOFF_SECONDS = 600.0
"""No retry waits longer than ten minutes."""

JITTER = 0.2
"""Backoff is multiplied by a factor in ``[1 - JITTER, 1 + JITTER]``."""

LOST_MESSAGE = (
    "the worker running this task stopped renewing its lease on the last attempt. If it was "
    "stopped for using too much memory, see the worker's memory limit in the documentation "
    "(LAKANA_WORKER__MEMORY_LIMIT, or LAKANA_WORKER_MEMORY in the Compose bundle's .env)."
)
"""`error_message` of a task failed because its last attempt was lost."""

COMMITTED_BEFORE_LOST_MESSAGE = (
    "The worker was lost after this task's write had been committed. The task counts as "
    "succeeded and was not run again."
)
"""`error_message` of a task, and of its attempt, that ended `succeeded` because the write it
had committed was found when its lease was lost. A task ended this way has no watermark
or schema diff, because the worker never got to record them, and its finish time is the moment
the commit was found."""

COMMITTED_BEFORE_STOPPED_MESSAGE = (
    "The worker was stopped for using too much memory after this task's write had been "
    "committed. The task counts as succeeded and was not run again. If this keeps happening, "
    "see the worker's memory limit in the documentation (LAKANA_WORKER__MEMORY_LIMIT, or "
    "LAKANA_WORKER_MEMORY in the Compose bundle's .env)."
)
"""The same, when the memory stop recorded it (`lakana_worker.memory`)."""

COMMITTED_BEFORE_RECORDED_MESSAGE = (
    "This task's write had been committed when recording its details failed afterwards. The "
    "task counts as succeeded and was not run again."
)
"""The same, when the worker is alive and its own recording of the outcome failed or was
interrupted after the commit (`lakana_worker.tasks._finish`)."""

MAX_SET_BACK_CLAIMS = 2
"""Claims a task may take to set its table back. A worker lost in both leaves the table as it
is and ends the task with `SET_BACK_LOST`, so a set-back that kills its worker cannot loop."""

_MAX_EXPONENT = 20
"""Exponents above this are capped anyway; bounding it keeps the integer small."""

_WAITING = frozenset({TaskStatus.PENDING, TaskStatus.READY})
_LIVE = frozenset({TaskStatus.PENDING, TaskStatus.READY, TaskStatus.RUNNING})
_BROKEN = frozenset({TaskStatus.FAILED, TaskStatus.UPSTREAM_FAILED})


def backoff_seconds(retry_delay_seconds: int, attempt: int, rand: Callable[[], float]) -> float:
    """Return the delay before the next attempt.

    Parameters
    ----------
    retry_delay_seconds
        The asset's first retry delay.
    attempt
        The attempt that just failed (1 for the first).
    rand
        Returns a float in ``[0, 1]``; 0.5 means no jitter.

    Returns
    -------
    float
        ``retry_delay · 2^(attempt-1)`` times the jitter factor, at most
        `MAX_BACKOFF_SECONDS`.
    """
    exponent = min(max(attempt - 1, 0), _MAX_EXPONENT)
    factor = 1 + JITTER * (2 * rand() - 1)
    return min(retry_delay_seconds * (2**exponent) * factor, MAX_BACKOFF_SECONDS)


def next_attempt_time(
    now: datetime, retry_delay_seconds: int, attempt: int, rand: Callable[[], float]
) -> datetime:
    """Return when a retry may start.

    Parameters
    ----------
    now
        The current time.
    retry_delay_seconds
        The asset's first retry delay.
    attempt
        The attempt that just failed.
    rand
        Jitter source, as for `backoff_seconds`.

    Returns
    -------
    datetime
        `now` plus the backoff.
    """
    return now + timedelta(seconds=backoff_seconds(retry_delay_seconds, attempt, rand))


@dataclass(frozen=True)
class TaskSnapshot:
    """A task run as the scheduler sees it.

    Parameters
    ----------
    id
        Task run id.
    asset
        Dotted table name.
    status
        Current status.
    attempt
        Attempts started so far (counted ones).
    claim
        The task's claim counter (`TaskRun.claim_count`), which never goes back.
    max_attempts
        Retries + 1.
    retry_delay_seconds
        First retry delay.
    next_attempt_at
        Earliest time a retry may become ready, or `None`.
    lease_owner
        Worker holding the lease while running.
    lease_expires_at
        When that lease lapses.
    upstream
        Selected upstream assets inside the same run.
    committed
        Whether the task's current write run (`TaskRun.query_run_id`) shows the evidence of a
        committed write (`rows_written` is not null, `0` included). Only a write run of the
        current attempt counts: that link is cleared when a task is claimed again.
    may_have_written
        Whether the task is running, has checks, and recorded a baseline (a Python table task
        does, before its process starts) but has no stamp and no committed write run: its process
        may have written rows no check has passed.
    baselined
        Whether the task has checks, recorded a baseline (a Python table task does, before its
        first process starts) and has no stamp, whatever its status: an earlier attempt may have
        left rows in its table that no check has passed. A cancel sets such a waiting task back
        instead of ending it.
    setting_back
        Whether the task is only to set its table back before it ends.
    set_back_claims
        Claims taken while it was setting back.
    check_claims
        Claims taken while it already awaited its checks (`TaskRun.check_claims`). At
        `MAX_CHECK_CLAIMS` the scheduler sends a task that still awaits them to set its table back.
    unchecked
        Whether the task's asset has checks, its write is in the table (its commit stamped it, or
        its write run shows the evidence: a rollback clears the stamp but not the evidence) and
        it is not ended, i.e. it has no verdict yet. Nobody but the worker that runs its
        checks ends such a task.
    """

    id: uuid.UUID
    asset: str
    status: TaskStatus
    attempt: int
    max_attempts: int
    retry_delay_seconds: int
    next_attempt_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    upstream: tuple[str, ...]
    claim: int
    committed: bool = False
    unchecked: bool = False
    may_have_written: bool = False
    baselined: bool = False
    setting_back: bool = False
    set_back_claims: int = 0
    check_claims: int = 0


@dataclass(frozen=True)
class RunSnapshot:
    """An active run and its task runs.

    Parameters
    ----------
    id
        Run id.
    status
        ``queued`` or ``running``.
    cancel_requested
        Whether a cancel was requested.
    tasks
        Every task run of the run.
    stopped_by_check
        Whether the run was failed by a code integrity check (`fail_run`), which sets the
        cancel flag too; the tasks it ends then say so instead of "cancelled".
    """

    id: uuid.UUID
    status: RunStatus
    cancel_requested: bool
    tasks: tuple[TaskSnapshot, ...]
    stopped_by_check: bool = False


@dataclass(frozen=True)
class TaskTransition:
    """Move one task run from `expected` to `status`.

    Parameters
    ----------
    run_id
        Its run.
    task_id
        The task run.
    expected
        The status the snapshot saw; the update is conditional on it.
    status
        The new status.
    expected_owner
        For a reclaimed lease, the owner the snapshot saw (also conditional).
    expected_claim
        For a reclaimed lease, the claim counter the snapshot saw (also
        conditional: a task re-claimed since is a different claim, even when
        an uncounted retry gave it the same attempt number).
    next_attempt_at
        For a retry, when it may become ready.
    lost
        Whether this transition reclaims an expired lease. To ``succeeded`` it is the reclaim
        of a task whose write had committed (the attempt is recorded as ``succeeded``);
        to anything else it records the attempt as ``lost``. The statement carries the
        evidence as the snapshot saw it: present for ``succeeded``, absent for the rest, and
        for an `unchecked` reclaim that the task awaits its checks (`store.awaiting_checks`: the
        *written* stamp or the evidence) instead.
    error_message
        For a failure, why.
    failure_code
        For a failure, a cancel or an upstream failure, the code that goes with `error_message`;
        `None` for any other move (a retry is no ending: the lost attempt gets its code in the
        store).
    ready_at
        For a task becoming ready, the moment it did (the FIFO claim key).
    unchecked
        Marks the reclaim of a written task with checks and no verdict: it goes back to
        ``pending`` at once and uncounted, and its statement requires the condition to still
        hold.
    setting_back
        Marks the reclaim of a task that may have written its table after its worker was lost
        (or whose set-back claim was lost): it goes straight to ``ready`` and uncounted, with
        the flag set, so that the next worker only sets the table back. Its statement requires
        the evidence of one of the set-back paths. Without `lost` it is the cancel loop's: a
        waiting task of a cancelled run whose table may hold rows no check has passed goes to
        ``ready`` with the flag, and its statement requires those facts and the claim it saw.
    ends_set_back
        Marks the end of a task whose set-back claims were lost up to the limit: it ends
        ``failed``, or ``cancelled`` in a run asked to cancel, with the lost set-back text as
        payload. Its statement requires that the task still is setting back.
    checks_unfinished
        Marks a `setting_back` move of a task whose checks reached `MAX_CHECK_CLAIMS` without a
        verdict, lost or waiting: it carries `CHECKS_UNFINISHED_MESSAGE` and its code for the
        claim that sets the table back, and its statement requires that bound (and, for a
        waiting task, that it still awaits its checks and is not setting back).
    """

    run_id: uuid.UUID
    task_id: uuid.UUID
    expected: TaskStatus
    status: TaskStatus
    expected_owner: str | None = None
    expected_claim: int | None = None
    next_attempt_at: datetime | None = None
    lost: bool = False
    error_message: str | None = None
    failure_code: FailureCode | None = None
    ready_at: datetime | None = None
    unchecked: bool = False
    setting_back: bool = False
    ends_set_back: bool = False
    checks_unfinished: bool = False

    @property
    def terminal(self) -> bool:
        """Whether the new status is final (sets `finished_at`).

        Returns
        -------
        bool
            `True` for succeeded, failed, upstream_failed and cancelled.
        """
        return self.status not in _LIVE


@dataclass(frozen=True)
class RunTransition:
    """Move one run from `expected` to `status`.

    Parameters
    ----------
    run_id
        The run.
    expected
        The status the snapshot saw.
    status
        The new status.
    counts
        For a finalization, task counts per status (for `RUN_FINISHED`).
    """

    run_id: uuid.UUID
    expected: RunStatus
    status: RunStatus
    counts: Mapping[str, int] | None = None


Transition = TaskTransition | RunTransition
"""One element of `tick`'s result."""


def final_status(statuses: Iterable[TaskStatus]) -> RunStatus:
    """Return a finished run's status from its tasks'.

    Parameters
    ----------
    statuses
        Every task's terminal status.

    Returns
    -------
    RunStatus
        ``failed`` if any task failed or is upstream_failed; else
        ``cancelled`` if any was cancelled; else ``succeeded``.
    """
    seen = set(statuses)
    if seen & _BROKEN:
        return RunStatus.FAILED
    if TaskStatus.CANCELLED in seen:
        return RunStatus.CANCELLED
    return RunStatus.SUCCEEDED


def _tick_run(run: RunSnapshot, now: datetime, rand: Callable[[], float]) -> list[Transition]:
    """Compute one run's transitions.

    Parameters
    ----------
    run
        An active run.
    now
        The current time.
    rand
        Jitter source.

    Returns
    -------
    list of Transition
        In application order.
    """
    out: list[Transition] = []
    if run.status is RunStatus.QUEUED:
        out.append(RunTransition(run.id, RunStatus.QUEUED, RunStatus.RUNNING))
    status = {task.asset: task.status for task in run.tasks}
    retry_at = {task.asset: task.next_attempt_at for task in run.tasks}
    setting = {task.asset for task in run.tasks if task.setting_back}
    stop_text = RUN_STOPPED_MESSAGE if run.stopped_by_check else RUN_CANCELLED_MESSAGE

    def put_back(task: TaskSnapshot, *, unfinished: bool = False) -> None:
        """Put a lost task back to ready, to set its table back, without counting an attempt.

        Parameters
        ----------
        task
            The task.
        unfinished
            Whether its checks reached `MAX_CHECK_CLAIMS`: the set-back then ends the task with
            `CHECKS_UNFINISHED_MESSAGE`.
        """
        move(
            task,
            TaskStatus.READY,
            expected_owner=task.lease_owner,
            expected_claim=task.claim,
            lost=True,
            ready_at=now,
            setting_back=True,
            error_message=CHECKS_UNFINISHED_MESSAGE if unfinished else None,
            failure_code=FailureCode.CHECKS_UNFINISHED if unfinished else None,
            checks_unfinished=unfinished,
        )

    def move(
        task: TaskSnapshot,
        new: TaskStatus,
        *,
        expected_owner: str | None = None,
        expected_claim: int | None = None,
        next_attempt_at: datetime | None = None,
        lost: bool = False,
        error_message: str | None = None,
        failure_code: FailureCode | None = None,
        ready_at: datetime | None = None,
        unchecked: bool = False,
        setting_back: bool = False,
        ends_set_back: bool = False,
        checks_unfinished: bool = False,
    ) -> None:
        """Record a transition from the task's current working status.

        Parameters
        ----------
        task
            The task.
        new
            Its new status.
        expected_owner
            As for `TaskTransition`.
        expected_claim
            As for `TaskTransition`.
        next_attempt_at
            As for `TaskTransition`.
        lost
            As for `TaskTransition`.
        error_message
            As for `TaskTransition`.
        failure_code
            As for `TaskTransition`; a task that ends ``cancelled`` or ``upstream_failed`` gets
            the code of that ending when none is given.
        ready_at
            As for `TaskTransition`.
        unchecked
            As for `TaskTransition`.
        setting_back
            As for `TaskTransition`.
        ends_set_back
            As for `TaskTransition`.
        checks_unfinished
            As for `TaskTransition`.
        """
        failure_code = failure_code or _ENDING_CODES.get(new)
        out.append(
            TaskTransition(
                run.id,
                task.id,
                status[task.asset],
                new,
                expected_owner=expected_owner,
                expected_claim=expected_claim,
                next_attempt_at=next_attempt_at,
                lost=lost,
                error_message=error_message,
                failure_code=failure_code,
                ready_at=ready_at,
                unchecked=unchecked,
                setting_back=setting_back,
                ends_set_back=ends_set_back,
                checks_unfinished=checks_unfinished,
            )
        )
        status[task.asset] = new
        retry_at[task.asset] = next_attempt_at
        if setting_back:
            setting.add(task.asset)

    for task in run.tasks:
        if task.status is not TaskStatus.RUNNING:
            continue
        if task.lease_expires_at is not None and task.lease_expires_at >= now:
            continue
        if task.setting_back:
            # Its set-back claim was lost. Setting the table back is what is left to do, so it
            # is claimed again, up to a limit; at the limit the table stays as it is.
            if task.set_back_claims < MAX_SET_BACK_CLAIMS:
                put_back(task)
            else:
                move(
                    task,
                    TaskStatus.CANCELLED if run.cancel_requested else TaskStatus.FAILED,
                    expected_owner=task.lease_owner,
                    expected_claim=task.claim,
                    lost=True,
                    error_message=SET_BACK_LOST,
                    failure_code=(
                        FailureCode.CANCELLED if run.cancel_requested else FailureCode.WORKER_LOST
                    ),
                    ends_set_back=True,
                )
        elif task.unchecked and run.cancel_requested:
            # The cancel would end it with the rows of its write unchecked in the table. The
            # next worker sets them back first, and the task ends cancelled after that.
            put_back(task)
        elif task.unchecked and task.check_claims >= MAX_CHECK_CLAIMS:
            # Its checks were claimed again up to the bound and lost each time. Another claim
            # would most likely be lost too, so the next one only sets the table back, which runs
            # no check, and the task fails saying why.
            put_back(task, unfinished=True)
        elif task.unchecked:
            # Written and not yet checked: nobody but the worker that runs its checks ends it,
            # not as succeeded (downstream would build on unchecked data), not as failed (the
            # attempts count the build, which is done), and not by this reclaim. It goes back at
            # once and uncounted; the next claim runs the checks, never the build. A cancel
            # that lands while it waits sends it to set back from `pending` (the cancel loop).
            move(
                task,
                TaskStatus.PENDING,
                expected_owner=task.lease_owner,
                expected_claim=task.claim,
                next_attempt_at=now,
                lost=True,
                unchecked=True,
            )
        elif task.committed:
            # The task's write is in the table: whatever else is true (a cancel request, attempts
            # left or not), running it again would write it twice, and the data is there.
            move(
                task,
                TaskStatus.SUCCEEDED,
                expected_owner=task.lease_owner,
                expected_claim=task.claim,
                lost=True,
            )
        elif task.may_have_written and (run.cancel_requested or task.attempt >= task.max_attempts):
            # Its process may have written rows no check has passed, and no later attempt will
            # start from the baseline: the next worker sets the table back and ends the task.
            put_back(task)
        elif run.cancel_requested:
            move(
                task,
                TaskStatus.CANCELLED,
                expected_owner=task.lease_owner,
                expected_claim=task.claim,
                lost=True,
                error_message=stop_text,
            )
        elif task.attempt < task.max_attempts:
            move(
                task,
                TaskStatus.PENDING,
                expected_owner=task.lease_owner,
                expected_claim=task.claim,
                next_attempt_at=next_attempt_time(
                    now, task.retry_delay_seconds, task.attempt, rand
                ),
                lost=True,
            )
        else:
            move(
                task,
                TaskStatus.FAILED,
                expected_owner=task.lease_owner,
                expected_claim=task.claim,
                lost=True,
                error_message=LOST_MESSAGE,
                failure_code=FailureCode.WORKER_LOST,
            )

    if run.cancel_requested:
        for task in run.tasks:
            if status[task.asset] not in _WAITING or task.asset in setting:
                continue
            if task.unchecked or task.baselined:
                # Its table may hold rows no check has passed: a write that waits for its
                # checks, or an earlier attempt of a task with a baseline. Ending it now would
                # leave them, so it goes to set the table back first, as a lost task does.
                move(
                    task,
                    TaskStatus.READY,
                    expected_claim=task.claim,
                    ready_at=now if status[task.asset] is TaskStatus.PENDING else None,
                    setting_back=True,
                )
            else:
                move(task, TaskStatus.CANCELLED, error_message=stop_text)

    for task in run.tasks:
        if (
            status[task.asset] in _WAITING
            and task.asset not in setting
            and task.unchecked
            and task.check_claims >= MAX_CHECK_CLAIMS
        ):
            # Its last check claim could not start the checks (or the memory stop ended it):
            # trying again after the backoff would only repeat that. The table is set back now.
            move(
                task,
                TaskStatus.READY,
                expected_claim=task.claim,
                ready_at=now if status[task.asset] is TaskStatus.PENDING else None,
                setting_back=True,
                error_message=CHECKS_UNFINISHED_MESSAGE,
                failure_code=FailureCode.CHECKS_UNFINISHED,
                checks_unfinished=True,
            )

    # Failure first, to its own fixed point, and only then cancellation: in
    # one interleaved pass a task below both would take whichever its asset
    # name happened to meet first.
    for blocking, outcome in (
        (_BROKEN, TaskStatus.UPSTREAM_FAILED),
        (frozenset({TaskStatus.CANCELLED}), TaskStatus.CANCELLED),
    ):
        changed = True
        while changed:
            changed = False
            for task in run.tasks:
                if status[task.asset] not in _WAITING or task.asset in setting:
                    continue
                if any(status.get(name) in blocking for name in task.upstream):
                    move(task, outcome)
                    changed = True

    for task in run.tasks:
        if status[task.asset] is not TaskStatus.PENDING:
            continue
        due = retry_at[task.asset]
        if due is not None and due > now:
            continue
        if all(status.get(name) is TaskStatus.SUCCEEDED for name in task.upstream):
            move(task, TaskStatus.READY, ready_at=now)

    if not any(s in _LIVE for s in status.values()):
        counts = dict(sorted(Counter(s.value for s in status.values()).items()))
        out.append(
            RunTransition(run.id, RunStatus.RUNNING, final_status(status.values()), counts=counts)
        )
    return out


def tick(runs: Sequence[RunSnapshot], now: datetime, rand: Callable[[], float]) -> list[Transition]:
    """Compute every transition one scheduler tick makes.

    Order per run: start a queued run; reclaim expired leases, the first matching rule winning:

    - a task that is setting back (its set-back claim was lost) goes back to ready to set back
      again, or ends with `SET_BACK_LOST` (``cancelled`` in a cancelled run) at
      `MAX_SET_BACK_CLAIMS`;
    - a task that is written and has checks but no verdict goes back to ready to set its table
      back in a cancelled run or once it took `MAX_CHECK_CLAIMS` claims only for its checks (the
      set-back then ends it with `CHECKS_UNFINISHED_MESSAGE`), and otherwise to pending at once
      and uncounted;
    - a task whose write run shows the evidence of a commit succeeds, whatever else is true;
    - a task that may have written (checks, a baseline, no stamp) goes back to ready to set its
      table back in a cancelled run or on its last attempt;
    - otherwise the task is cancelled in a cancelled run, retried with backoff, or failed on its
      last attempt.

    Then, in a cancelled run, each waiting task that is not setting back is cancelled, unless
    its table may hold rows no check has passed (it awaits its checks, or it has checks, a
    baseline and no stamp): such a task goes to ready to set its table back first. A waiting
    task that awaits its checks and took `MAX_CHECK_CLAIMS` claims for them (its checks could not
    start) goes to ready to set its table back too, without waiting out its backoff. Then
    propagate failure (``upstream_failed``) downstream to a fixed point and then cancellation to
    its own, so a task below both is ``upstream_failed`` whatever its asset's name; a task that
    is setting back is left out of both, and of the cancel. Ready every pending task whose
    upstreams all succeeded and whose retry time has come (stamping ``ready_at``); finalize a
    run with no pending, ready or running task left. Each step reads the state left by the
    earlier ones within the tick, so a task reclaimed with backoff is not readied again at once.

    Parameters
    ----------
    runs
        Snapshots of the active runs.
    now
        The current time (injected).
    rand
        Jitter source returning floats in ``[0, 1]`` (injected).

    Returns
    -------
    list of Transition
        To be applied in order, in one transaction.
    """
    out: list[Transition] = []
    for run in runs:
        if run.status not in (RunStatus.QUEUED, RunStatus.RUNNING):
            continue
        out.extend(_tick_run(run, now, rand))
    return out
