"""Claiming task runs, renewing leases and recording attempt outcomes.

The worker side of the queue, DuckDB-free. Every write is a compare-and-swap
: the claim on `status = 'READY'`, renewal and outcome on
`(id, lease_owner, status = 'RUNNING', claim_count)`, so a worker whose lease
the scheduler reclaimed, or a zombie attempt of a task since re-claimed, writes
nothing — `claim_count` only grows, where `attempt` is taken back by an
uncounted retry and so could repeat — on Postgres as on SQLite. Every
timestamp written here is the database's (`db_now`), never a worker's wall
clock.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.orm import Session

from lakana_core.db import db_now, rowcount
from lakana_core.kept_mark import KeptSeen
from lakana_core.models import (
    OWN_TIP_NO_SNAPSHOT,
    AttemptOutcome,
    QueryRun,
    Run,
    RunStatus,
    Table,
    TaskAttempt,
    TaskRun,
    TaskStatus,
)
from lakana_core.models.run import ACTIVE_RUN_STATUSES
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.scheduler import next_attempt_time
from lakana_core.runs.store import awaiting_checks, may_have_written

_CLAIM_CANDIDATES = 5
"""How many lost races one claim call tolerates before reporting an empty queue."""


class FailureKind(enum.StrEnum):
    """How an attempt failed, which decides what happens next."""

    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    CONFLICT = "conflict"
    PERMANENT = "permanent"
    CANCELLED = "cancelled"
    RELEASED = "released"


class LeaseState(enum.StrEnum):
    """What a lease renewal found."""

    HELD = "held"
    CANCEL_REQUESTED = "cancel_requested"
    LOST = "lost"


@dataclass(frozen=True)
class ClaimedTask:
    """A task run this worker now holds.

    Parameters
    ----------
    task_run_id
        The task run.
    run_id
        Its run.
    asset
        Dotted table name.
    attempt
        The attempt number just started.
    timeout_seconds
        The asset's per-attempt limit.
    claim
        The task's claim counter after this claim: what `renew_lease` and
        `record_attempt` are conditional on. Never repeats for a task.
    kind
        ``asset`` or ``source_check`` (`TaskRun.kind`).
    has_checks
        Whether the task's asset has checks (`TaskRun.has_checks`).
    max_attempts
        The task's attempt limit. `attempt` can exceed it for a task that was written and not
        yet checked: such a task is claimed again without being counted out.
    setting_back
        Whether this claim only sets the task's table back (`TaskRun.setting_back`). Such a claim
        does not count an attempt: `attempt` is the number of the lost attempt it follows.
    """

    task_run_id: uuid.UUID
    run_id: uuid.UUID
    asset: str
    attempt: int
    timeout_seconds: int
    claim: int
    kind: str = "asset"
    has_checks: bool = False
    max_attempts: int = 1
    setting_back: bool = False


def _take(
    session: Session, task_run_id: uuid.UUID, worker: str, now: datetime, lease_seconds: int
) -> bool:
    """Move one ready task run to running under this worker's lease.

    Refuses a task whose run has a cancel request or is no longer running,
    so a worker cannot start work the scheduler is about to cancel. A task that is to set its
    table back passes a cancel request, because that is the cancel's own work, but not a run
    that is over; its claim adds no attempt and counts as a set-back claim. A claim of a task
    that already awaits its checks counts as a check claim (`TaskRun.check_claims`). Clears
    `query_run_id`, so a superseded attempt's QueryRun no longer
    matches the lockdown's run-context check.

    Parameters
    ----------
    session
        Database session, inside the claim's transaction.
    task_run_id
        The candidate.
    worker
        This worker's id.
    now
        The database's current time.
    lease_seconds
        Lease length.

    Returns
    -------
    bool
        Whether the candidate was still ready in a live run (and is now ours).
    """
    result = session.execute(
        update(TaskRun)
        .where(
            TaskRun.id == task_run_id,
            TaskRun.status == TaskStatus.READY,
            exists().where(
                Run.id == TaskRun.run_id,
                # Setting the table back is the cancel's own work, so it may run under the request.
                or_(Run.cancel_requested.is_(False), TaskRun.setting_back.is_(True)),
                Run.status == RunStatus.RUNNING,
            ),
        )
        .values(
            status=TaskStatus.RUNNING,
            # A set-back claim follows the lost attempt without being one, so the stored
            # attempts stay true.
            attempt=case(
                (TaskRun.setting_back.is_(True), TaskRun.attempt), else_=TaskRun.attempt + 1
            ),
            set_back_claims=case(
                (TaskRun.setting_back.is_(True), TaskRun.set_back_claims + 1),
                else_=TaskRun.set_back_claims,
            ),
            # A claim of a task that already awaits its checks runs only them; the scheduler
            # stops sending such claims at `MAX_CHECK_CLAIMS`. A set-back claim runs no check.
            check_claims=case(
                (
                    and_(TaskRun.setting_back.is_(False), awaiting_checks()),
                    TaskRun.check_claims + 1,
                ),
                else_=TaskRun.check_claims,
            ),
            claim_count=TaskRun.claim_count + 1,
            lease_owner=worker,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            started_at=func.coalesce(TaskRun.started_at, now),
            query_run_id=None,
        )
        .execution_options(synchronize_session=False)
    )
    return rowcount(result) == 1


def claim_task(session: Session, worker: str, lease_seconds: int) -> ClaimedTask | None:
    """Claim the next ready task run and open its attempt.

    `SELECT … FOR UPDATE SKIP LOCKED` keeps two Postgres workers off the same
    row; the conditional update makes the claim safe where that clause is a
    no-op (SQLite) or a candidate was taken between the two statements.
    Ready tasks are claimed first-in-first-out by `ready_at`, then by their
    run's creation time, then id. The lease runs from the database's clock.

    Parameters
    ----------
    session
        Database session; the claim commits.
    worker
        This worker's id.
    lease_seconds
        Lease length.

    Returns
    -------
    ClaimedTask or None
        The claim, or `None` when no task is ready.
    """
    for _ in range(_CLAIM_CANDIDATES):
        now = db_now(session)
        candidate = session.execute(
            select(TaskRun.id)
            .join(Run, Run.id == TaskRun.run_id)
            .where(
                TaskRun.status == TaskStatus.READY,
                # Setting the table back is the cancel's own work, so it may run under the request.
                or_(Run.cancel_requested.is_(False), TaskRun.setting_back.is_(True)),
                Run.status == RunStatus.RUNNING,
            )
            .order_by(TaskRun.ready_at.nulls_first(), Run.created_at, TaskRun.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=TaskRun)
        ).scalar_one_or_none()
        if candidate is None:
            session.rollback()
            return None
        if not _take(session, candidate, worker, now, lease_seconds):
            session.rollback()
            continue
        task = session.execute(
            select(TaskRun).where(TaskRun.id == candidate).execution_options(populate_existing=True)
        ).scalar_one()
        session.add(
            TaskAttempt(
                id=uuid.uuid4(),
                task_run_id=task.id,
                attempt=task.attempt,
                worker=worker,
                started_at=now,
            )
        )
        claimed = ClaimedTask(
            task_run_id=task.id,
            run_id=task.run_id,
            asset=task.asset,
            attempt=task.attempt,
            timeout_seconds=task.timeout_seconds,
            claim=task.claim_count,
            kind=task.kind,
            has_checks=task.has_checks,
            max_attempts=task.max_attempts,
            setting_back=task.setting_back,
        )
        session.commit()
        return claimed
    return None


@dataclass(frozen=True)
class Written:
    """What a task's write did to its table, as the task run records it.

    Parameters
    ----------
    table_id
        The table written.
    snapshot_before
        The table's current snapshot when the write was staged; `None` when it had none.
    snapshot_after
        The snapshot the write made; `None` when it wrote no rows.
    publishes
        Whether the write created the table or found it not published.
    """

    table_id: uuid.UUID
    snapshot_before: int | None
    snapshot_after: int | None
    publishes: bool


def read_written(session: Session, task_run_id: uuid.UUID) -> Written | None:
    """Read what a task run's write did, from the committed columns.

    A column select, never the ORM object: the caller may hold a stale copy of the task run.

    Parameters
    ----------
    session
        Database session. Starts no write.
    task_run_id
        The task run.

    Returns
    -------
    Written or None
        The record, or `None` while the task run has none (no checks, no write yet, or the
        rollback cleared it).
    """
    row = session.execute(
        select(
            TaskRun.written_table_id,
            TaskRun.written_snapshot_before,
            TaskRun.written_snapshot_after,
            TaskRun.written_publishes,
        ).where(TaskRun.id == task_run_id)
    ).one_or_none()
    if row is None or row.written_table_id is None:
        return None
    return Written(
        row.written_table_id,
        row.written_snapshot_before,
        row.written_snapshot_after,
        bool(row.written_publishes),
    )


def read_written_with_note(
    session: Session, task_run_id: uuid.UUID
) -> tuple[Written | None, KeptSeen]:
    """Read what a task run's write did, and what the table it wrote says about failed data.

    `read_written` with more columns of the same statement (a left join onto the written
    table), so the check phase learns whether the table has a note and which one
    (`Table.kept_run_id`, `kept_task_run_id`, `kept_snapshot_id`) without a statement of its own.

    Parameters
    ----------
    session
        Database session. Starts no write.
    task_run_id
        The task run.

    Returns
    -------
    tuple
        The record, or `None` while the task run has none, and the mark of the written table as
        seen (`KeptSeen`; no mark without a record).
    """
    row = session.execute(
        select(
            TaskRun.written_table_id,
            TaskRun.written_snapshot_before,
            TaskRun.written_snapshot_after,
            TaskRun.written_publishes,
            Table.kept_run_id,
            Table.kept_task_run_id,
            Table.kept_snapshot_id,
        )
        .outerjoin(Table, Table.id == TaskRun.written_table_id)
        .where(TaskRun.id == task_run_id)
    ).one_or_none()
    if row is None or row.written_table_id is None:
        return None, KeptSeen(None, None, None)
    return (
        Written(
            row.written_table_id,
            row.written_snapshot_before,
            row.written_snapshot_after,
            bool(row.written_publishes),
        ),
        KeptSeen(row.kept_run_id, row.kept_task_run_id, row.kept_snapshot_id),
    )


def mark_written(
    session: Session, task_run_id: uuid.UUID, worker: str, claim: int, written: Written
) -> bool:
    """Record what a Python task's write did, after its process has ended.

    One conditional `UPDATE`: the task is still this worker's `RUNNING` task of this claim and
    its asset has checks. A SQL task is stamped by the write's own commit instead.

    Parameters
    ----------
    session
        Database session; commits on success, rolls back otherwise.
    task_run_id
        The task run.
    worker
        This worker's id.
    claim
        `ClaimedTask.claim`.
    written
        What the write did.

    Returns
    -------
    bool
        `False` (nothing written) if the task is no longer this worker's running task of this
        claim, or its asset has no checks.
    """
    result = session.execute(
        update(TaskRun)
        .where(
            TaskRun.id == task_run_id,
            TaskRun.lease_owner == worker,
            TaskRun.status == TaskStatus.RUNNING,
            TaskRun.claim_count == claim,
            TaskRun.has_checks.is_(True),
        )
        .values(
            written_table_id=written.table_id,
            written_snapshot_before=written.snapshot_before,
            written_snapshot_after=written.snapshot_after,
            written_publishes=written.publishes,
        )
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) != 1:
        session.rollback()
        return False
    session.commit()
    return True


@dataclass(frozen=True)
class Baseline:
    """What a Python asset's table was before its task run's first process started.

    Parameters
    ----------
    snapshot_id
        The table's current snapshot then; `None` when it had none or did not exist.
    publishes
        Whether a write would publish the table: it did not exist, or was not published.
    """

    snapshot_id: int | None
    publishes: bool


def read_baseline(session: Session, task_run_id: uuid.UUID) -> Baseline | None:
    """Read a task run's recorded baseline, from the committed columns.

    Parameters
    ----------
    session
        Database session. Starts no write.
    task_run_id
        The task run.

    Returns
    -------
    Baseline or None
        The baseline, or `None` while none is recorded (always, for a SQL task and for a task
        without checks).
    """
    row = session.execute(
        select(TaskRun.baseline_snapshot_id, TaskRun.baseline_publishes).where(
            TaskRun.id == task_run_id
        )
    ).one_or_none()
    if row is None or row.baseline_publishes is None:
        return None
    return Baseline(row.baseline_snapshot_id, bool(row.baseline_publishes))


@dataclass(frozen=True)
class OwnTip:
    """The tip of a task run's own chain of writes.

    Parameters
    ----------
    snapshot_id
        The snapshot the task run's last own write made; `None` when that write made none (a
        table created without rows).
    """

    snapshot_id: int | None


def read_own_tip(session: Session, task_run_id: uuid.UUID) -> OwnTip | None:
    """Read the tip of a task run's own chain of writes, from the committed column.

    The write guard advances it in the commit of each write of the task run that started from
    the task run's baseline or from the tip before (`ProtectedWriteGuard.record_pending`). A
    write that started from anything else leaves it: somebody else changed the table in
    between, and what follows is not provably this task run's alone.

    Parameters
    ----------
    session
        Database session. Starts no write.
    task_run_id
        The task run.

    Returns
    -------
    OwnTip or None
        The tip; `None` when the task run wrote nothing of its own (always, for a SQL task
        and for a task without checks).
    """
    stored = session.execute(
        select(TaskRun.own_snapshot_id).where(TaskRun.id == task_run_id)
    ).scalar_one_or_none()
    if stored is None:
        return None
    return OwnTip(None if stored == OWN_TIP_NO_SNAPSHOT else stored)


def record_baseline(
    session: Session, task_run_id: uuid.UUID, worker: str, claim: int, baseline: Baseline
) -> Baseline | None:
    """Record a task run's baseline once, and hand back the one that is stored.

    Called before every process of a Python table asset with checks. Only the first call of a
    task run writes: an earlier attempt may have written to the table and then failed or lost
    its worker, so what a later attempt finds is no longer what the table was before the run.
    One conditional `UPDATE` (the claim's conditions, the asset has checks, and no baseline
    yet), then the stored baseline is read back under the same conditions.

    Parameters
    ----------
    session
        Database session; commits.
    task_run_id
        The task run.
    worker
        This worker's id.
    claim
        `ClaimedTask.claim`.
    baseline
        The table's state as this attempt found it; stored only if none is recorded.

    Returns
    -------
    Baseline or None
        The stored baseline, whoever stored it; `None` (nothing written) if the task is no
        longer this worker's running task of this claim, or its asset has no checks.
    """
    held = (
        TaskRun.id == task_run_id,
        TaskRun.lease_owner == worker,
        TaskRun.status == TaskStatus.RUNNING,
        TaskRun.claim_count == claim,
        TaskRun.has_checks.is_(True),
    )
    session.execute(
        update(TaskRun)
        .where(*held, TaskRun.baseline_publishes.is_(None))
        .values(baseline_snapshot_id=baseline.snapshot_id, baseline_publishes=baseline.publishes)
        .execution_options(synchronize_session=False)
    )
    row = session.execute(
        select(TaskRun.baseline_snapshot_id, TaskRun.baseline_publishes).where(*held)
    ).one_or_none()
    if row is None:
        session.rollback()
        return None
    session.commit()
    return Baseline(row.baseline_snapshot_id, bool(row.baseline_publishes))


def awaits_checks(session: Session, task_run_id: uuid.UUID) -> bool:
    """Say whether a task run has checks, its write is in the table, and it is not ended.

    The definition is `lakana_core.runs.store.awaiting_checks`: the written stamp or the
    evidence on the task's current write run. A column select, never the ORM object: the caller
    may hold a stale copy of the task run.

    Parameters
    ----------
    session
        Database session. Starts no write.
    task_run_id
        The task run.

    Returns
    -------
    bool
        `True` while the task awaits the verdict of its checks.
    """
    return (
        session.execute(
            select(TaskRun.id).where(TaskRun.id == task_run_id, awaiting_checks())
        ).first()
        is not None
    )


def renew_lease(
    session: Session, task_run_id: uuid.UUID, worker: str, claim: int, lease_seconds: int
) -> LeaseState:
    """Extend this worker's lease and look for a cancel request.

    The lease is extended to the database's now plus `lease_seconds`.

    Parameters
    ----------
    session
        Database session; commits.
    task_run_id
        The running task run.
    worker
        This worker's id.
    claim
        `ClaimedTask.claim`; a zombie of an earlier claim of a re-claimed
        task gets `LOST`, even when the attempt number repeated.
    lease_seconds
        Lease length.

    Returns
    -------
    LeaseState
        `LOST` if the task is no longer this worker's running task;
        `CANCEL_REQUESTED` if its run asked to cancel, unless the claim only sets the table
        back; else `HELD`.
    """
    result = session.execute(
        update(TaskRun)
        .where(
            TaskRun.id == task_run_id,
            TaskRun.lease_owner == worker,
            TaskRun.status == TaskStatus.RUNNING,
            TaskRun.claim_count == claim,
        )
        .values(lease_expires_at=db_now(session) + timedelta(seconds=lease_seconds))
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) != 1:
        session.rollback()
        return LeaseState.LOST
    cancel, setting_back = session.execute(
        select(Run.cancel_requested, TaskRun.setting_back)
        .join(TaskRun, TaskRun.run_id == Run.id)
        .where(TaskRun.id == task_run_id)
    ).one()
    session.commit()
    # Setting the table back is the cancel's own work, so the request is no signal to stop it.
    return LeaseState.CANCEL_REQUESTED if cancel and not setting_back else LeaseState.HELD


def _must_be_set_back(
    session: Session, task: TaskRun, kind: FailureKind | None, *, stopped: bool
) -> bool:
    """Say whether ending a task this way would leave rows in its table that no check passed.

    Decided on the locked row, by the database forms of the facts (`awaiting_checks`,
    `may_have_written`), never on what the caller saw earlier. Two endings are covered:

    - a cancel, when the task's write is in the table (stamped, or its write run shows a
      commit) or the task may have written (a Python table task with checks has recorded a
      baseline and has no stamp). A task that never recorded either has nothing to set back. A
      task that did record a baseline but wrote nothing is handed back too, and its set-back
      says that nothing was written.
    - the memory stop (`stopped`), when it ends the task for good (a certain blame, or the last
      attempt) and the task may have written. A stop that leaves attempts goes to the next
      attempt, which starts from the baseline; a task whose write is stamped waits for its
      checks instead of ending.

    Parameters
    ----------
    session
        Database session; the task's row is locked by the caller.
    task
        The locked task run.
    kind
        How the attempt ended, after the caller's conversions.
    stopped
        Whether the memory stop ends it.

    Returns
    -------
    bool
        Whether the task is to be handed back for a set-back claim.
    """
    if kind is None or kind is FailureKind.RELEASED:
        return False
    if kind is not FailureKind.CANCELLED and not stopped:
        return False
    awaiting, wrote = session.execute(
        select(awaiting_checks(), may_have_written()).where(TaskRun.id == task.id)
    ).one()
    if kind is FailureKind.CANCELLED:
        return bool(awaiting or wrote)
    ends_for_good = kind is FailureKind.PERMANENT or task.attempt >= task.max_attempts
    return bool(wrote and ends_for_good)


def _stored_code(kind: FailureKind | None, code: FailureCode | None) -> str | None:
    """Pick the failure code an ending stores.

    Parameters
    ----------
    kind
        How the attempt ended, after the caller's conversions.
    code
        The code the caller gave.

    Returns
    -------
    str or None
        `None` for a success and a release; ``cancelled`` for a cancel; the caller's code for any
        other failure, or ``internal`` when it gave none.
    """
    if kind is None or kind is FailureKind.RELEASED:
        return None
    if kind is FailureKind.CANCELLED:
        return FailureCode.CANCELLED.value
    return (code or FailureCode.INTERNAL).value


def record_attempt(
    session: Session,
    task_run_id: uuid.UUID,
    worker: str,
    claim: int,
    *,
    kind: FailureKind | None,
    rand: Callable[[], float],
    error_message: str | None = None,
    failure_code: FailureCode | None = None,
    rows_written: int | None = None,
    on_committed: str | None = None,
    stopped: bool = False,
    in_transaction: Callable[[Session], None] | None = None,
    check_message_state: Mapping[str, Any] | None = None,
) -> bool:
    """Record how an attempt ended and move the task run on.

    Finish and retry times are the database's now.

    A task that awaits its checks (`store.awaiting_checks`: its asset has checks and its
    write is in the table, stamped or shown by the write run's evidence) cannot run out of
    attempts: its build is done and the checks have no verdict, so a retryable failure
    (transient, timeout)
    puts it back to ``pending`` with backoff whatever `attempt` and `TaskRun.max_attempts` say,
    and the next claim runs the checks only. A permanent failure still fails it (the worker's
    verdict on a failed check), a cancel cancels it and a release releases it.

    A claim that only sets the table back (`TaskRun.setting_back`) is recorded as ``cancelled``,
    ``released`` or ``failed`` and as nothing else: any other `kind` is recorded as ``failed``
    with the same message, since a retry would only set the table back again. A release keeps
    `attempt` (the claim added none) and gives back the claim's count against the limit on
    set-back claims; a release of a claim that only ran the checks gives back its check claim
    (`TaskRun.check_claims`) the same way. The memory stop (`stopped`) records nothing for such
    a claim: it ends no build, so the scheduler takes the claim over when its lease runs out.

    An ending that would leave rows in the table that no check has passed is not recorded as an
    ending (`_must_be_set_back`): a cancel after the task's write, and a memory stop that ends
    for good a task that may have written. The task goes back as ``ready`` with `setting_back`,
    no attempt counted, exactly as the scheduler puts back a task whose worker was lost, and the
    attempt closes as ``cancelled`` or ``failed``. The next worker sets the table back and ends
    the task, with the stop's message (stored in `TaskRun.error_message`) or the cancel's.

    Parameters
    ----------
    session
        Database session; commits on success.
    task_run_id
        The task run.
    worker
        This worker's id; the update is conditional on still holding the lease.
    claim
        `ClaimedTask.claim`; the update is conditional on it still being the
        task's current claim. The attempt number is read from the locked row.
    kind
        `None` for success, else how it failed.
    rand
        Jitter source for the backoff.
    error_message
        The safe, redacted failure text.
    failure_code
        Why it failed, stored with the text on the task run and on the attempt. Dropped for a
        success or a release, which are no failure; always ``cancelled`` for a cancel. A failure
        recorded without one is ``internal``: only a failure nobody expected may be.
    rows_written
        Rows the write wrote.
    on_committed
        For an attempt that is ended from outside its own thread (the memory stop), or whose
        recording failed: when given, and the task's current write run (`TaskRun.query_run_id`)
        shows the evidence of a commit, a failure `kind` is replaced by success, with this text
        as the message and the evidence as `rows_written`. The write is in the table, and a retry
        would write it twice. The evidence is read after the task's row is locked, so a write
        that is committing is waited for (its guard holds that row `FOR SHARE`) and seen. Never
        for a task whose asset has checks (`TaskRun.has_checks`): its success is the verdict of
        its checks, which only the worker that ran them records (`kind=None`), and a failure
        that the worker records stays a failure.

    stopped
        The attempt is ended by the memory stop, whose ``PERMANENT`` is blame for the memory,
        not the worker's verdict on a check. For a task that awaits its checks it is recorded
        as ``TRANSIENT`` instead, decided here on the locked row: such a task is never failed
        by the stop, it waits for another claim.
    in_transaction
        Work that must land with the attempt or not at all (a code integrity failure fails the
        run and audits it). Called with the session after the task run and its attempt are
        updated and before the commit, and never when the claim is lost; it must not commit. An
        exception from it rolls the whole record back.
    check_message_state
        What the check phase built `error_message` from besides the results
        (`CheckMessageState.to_json`), stored with the text on the task run and on the attempt;
        `None` for a text the check phase did not build. A release keeps the task run's, as it
        keeps its text; a text replaced by `on_committed` gets none.

    Returns
    -------
    bool
        `False` (nothing written) if the task is no longer this worker's
        running task of this claim — the scheduler reclaimed it. `True` also when the task was
        handed back to set its table back.

    Raises
    ------
    RuntimeError
        If the lease is held but the attempt's open row is missing (the
        transaction is rolled back, so nothing is half-written).
    """
    task = session.execute(
        select(TaskRun)
        .where(TaskRun.id == task_run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if (
        task is None
        or task.status is not TaskStatus.RUNNING
        or task.lease_owner != worker
        or task.claim_count != claim
    ):
        session.rollback()
        return False
    setting_back = task.setting_back
    if setting_back and stopped:
        # The memory stop blames a build; this claim runs none. The lease runs out and the
        # scheduler decides again, bounded by the limit on set-back claims.
        session.rollback()
        return False
    if setting_back and kind not in {FailureKind.CANCELLED, FailureKind.RELEASED}:
        # A claim that only sets the table back ends the task, failed, whatever it was asked to
        # record: a stamped task keeps its stamp when the set-back is refused or the table has
        # changed, so a retryable ending would send it round again; and the commit that the
        # lost attempt's write run shows is exactly what this claim sets back.
        kind = FailureKind.PERMANENT
        on_committed = None
    awaiting = awaits_checks(session, task_run_id)
    if stopped and awaiting and kind is FailureKind.PERMANENT:
        kind = FailureKind.TRANSIENT
    if on_committed is not None and kind is not None and not task.has_checks:
        written = session.execute(
            select(QueryRun.rows_written).where(QueryRun.id == task.query_run_id)
        ).scalar_one_or_none()
        if written is not None:
            kind, error_message, rows_written = None, on_committed, written
            check_message_state = None
    attempt = task.attempt
    now = db_now(session)
    stored_code = _stored_code(kind, failure_code)
    values: dict[str, object] = {
        "lease_owner": None,
        "lease_expires_at": None,
        "error_message": error_message,
        "failure_code": stored_code,
        "check_message_state": check_message_state,
    }
    hand_back = not setting_back and _must_be_set_back(session, task, kind, stopped=stopped)
    if hand_back:
        # The table may hold rows no check has passed, and this ending would leave them. The task
        # goes back as a lost worker's does (ready, `setting_back`, no attempt counted), under
        # this claim, and the next worker sets the table back and ends it. The text of the end
        # waits in the row for that worker (a run that is cancelled by then shows the cancel's).
        values.update(
            status=TaskStatus.READY,
            setting_back=True,
            ready_at=now,
            error_message=error_message,
        )
        outcome = (
            AttemptOutcome.CANCELLED if kind is FailureKind.CANCELLED else AttemptOutcome.FAILED
        )
    elif kind is None:
        values.update(status=TaskStatus.SUCCEEDED, finished_at=now)
        outcome = AttemptOutcome.SUCCEEDED
    elif kind is FailureKind.CANCELLED:
        values.update(status=TaskStatus.CANCELLED, finished_at=now)
        outcome = AttemptOutcome.CANCELLED
    elif kind is FailureKind.RELEASED:
        if setting_back:
            # The claim added no attempt, and a drain is no loss: it must not count against the
            # limit on set-back claims. The task stays flagged and is claimed again as before.
            values.update(
                status=TaskStatus.PENDING,
                set_back_claims=max(task.set_back_claims - 1, 0),
                next_attempt_at=now,
            )
            # The stored text is the reason the task will end with (a handed-back task keeps
            # the stop's); a drain has none of its own. The same holds for its code.
            del values["error_message"]
            del values["failure_code"]
            del values["check_message_state"]
        else:
            values.update(status=TaskStatus.PENDING, attempt=attempt - 1, next_attempt_at=now)
            if awaiting:
                # Nor is a drain of a claim that only ran the checks a try of them: it gives back
                # its check claim, so restarts of the workers never use up `MAX_CHECK_CLAIMS`.
                values["check_claims"] = max(task.check_claims - 1, 0)
        outcome = AttemptOutcome.RELEASED
    elif kind is FailureKind.CONFLICT and not task.conflict_retried:
        values.update(
            status=TaskStatus.PENDING,
            attempt=attempt - 1,
            next_attempt_at=now,
            conflict_retried=True,
        )
        outcome = AttemptOutcome.FAILED
    elif kind is FailureKind.PERMANENT or (attempt >= task.max_attempts and not awaiting):
        values.update(status=TaskStatus.FAILED, finished_at=now)
        outcome = AttemptOutcome.TIMED_OUT if kind is FailureKind.TIMEOUT else AttemptOutcome.FAILED
    else:
        values.update(
            status=TaskStatus.PENDING,
            next_attempt_at=next_attempt_time(now, task.retry_delay_seconds, attempt, rand),
        )
        outcome = AttemptOutcome.TIMED_OUT if kind is FailureKind.TIMEOUT else AttemptOutcome.FAILED
    result = session.execute(
        update(TaskRun)
        .where(
            TaskRun.id == task_run_id,
            TaskRun.status == TaskStatus.RUNNING,
            TaskRun.lease_owner == worker,
            TaskRun.claim_count == claim,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) != 1:
        session.rollback()
        return False
    closed = session.execute(
        update(TaskAttempt)
        .where(
            TaskAttempt.task_run_id == task_run_id,
            TaskAttempt.attempt == attempt,
            TaskAttempt.worker == worker,
            TaskAttempt.finished_at.is_(None),
        )
        .values(
            outcome=outcome,
            finished_at=now,
            rows_written=rows_written,
            error_message=error_message,
            failure_code=stored_code,
            check_message_state=check_message_state,
        )
        .execution_options(synchronize_session=False)
    )
    if rowcount(closed) != 1:
        session.rollback()
        raise RuntimeError(f"task run {task_run_id} attempt {attempt} has no open attempt row")
    if in_transaction is not None:
        try:
            in_transaction(session)
        except BaseException:
            session.rollback()
            raise
    session.commit()
    return True


def fail_run(session: Session, run_id: uuid.UUID, message: str, *, commit: bool = True) -> None:
    """Fail a whole run from a task: record why and let the scheduler cancel the rest.

    A run that already finished is left untouched.

    Parameters
    ----------
    session
        Database session; commits.
    run_id
        The run.
    message
        The run-level error (code integrity).
    commit
        Whether to commit. `False` leaves the update in the caller's transaction, so it lands
        with the caller's other writes or not at all (`record_attempt`'s `in_transaction`).
    """
    session.execute(
        update(Run)
        .where(Run.id == run_id, Run.status.in_(ACTIVE_RUN_STATUSES))
        .values(error_message=message, cancel_requested=True)
        .execution_options(synchronize_session=False)
    )
    if commit:
        session.commit()
