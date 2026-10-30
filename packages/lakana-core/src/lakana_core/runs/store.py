"""Load the scheduler snapshot and apply a tick's transitions in per-run transactions.

Every update is a compare-and-swap on the state the snapshot saw:
`UPDATE … WHERE id = :id AND status = :expected [AND lease_owner = :owner]`,
checked by `rowcount`. A run's transitions are applied in one transaction: if
any of them misses — a worker finished a task or a user cancelled meanwhile —
that run's transaction is rolled back and the next tick re-reads it. Runs are
independent, so each commits on its own: a run that keeps losing its race (a
busy task finishing every tick) must not discard the progress of every other
run, which one transaction per tick would do. That holds on Postgres under
READ COMMITTED (the row lock makes the loser re-evaluate its condition) as on
SQLite.

The sweep of lost query runs (`end_lost_runs`) is a compare-and-swap like every other
update here. Candidates are read with a lock that skips locked rows, but the statement that
ends a run decides on its own: its `WHERE` repeats `status = 'RUNNING'`, the evidence as it was
read (`rows_written` null or not), "no alive worker holds this run" and "not a SQL task's write
under a live task lease", and `rowcount` says whether it won. A run that changed in between is
left for the next tick.

The candidate lock is `FOR NO KEY UPDATE SKIP LOCKED`, not `FOR UPDATE`, on purpose. A run is
passed by only while another transaction holds the row with a lock that conflicts with it: the
write fence's `UPDATE`, from the fence to the commit (microseconds, no I/O). While a write
uploads its metadata file, the lineage edge's foreign key holds `FOR KEY SHARE` on the run's
row, and a worker frozen there (a stopped process keeps its connection) would otherwise keep
its run `RUNNING` until it woke. `FOR KEY SHARE` does not conflict with `FOR NO KEY UPDATE` or
with the `UPDATE` that ends the run, which changes no key column. Proved on Postgres by
`test_lost_workers_integration.py::test_a_write_held_before_its_commit_is_ended_and_then_changes_nothing`.

Who ends a `RUNNING` query run, in one place (each statement requires `RUNNING`, so a run is
ended once, and each carries the evidence rule in its own `WHERE`):

- the sweep (`end_lost_runs`, the leader's tick): a run no alive worker holds;
- the idle worker (`end_orphaned_runs`, from `poll_forever`): a run that still carries the
  worker's own id although it runs nothing, i.e. whose final status could not be written;
- the task path (`_apply_task`): every `RUNNING` run of a task whose lease is lost: `SUCCEEDED`
  when it shows the evidence, otherwise `FAILED` with the lease message and no cause;
- the memory stop (`lakana_worker.memory.record_memory_failure`) and the cancel of a lost run
  (`QueryService`), which use the same predicate (`committed_write`).

A SQL task's own write is the task path's while its task run is `RUNNING` with a live lease and
the write is the task's current one (`held_by_task_lease`): the sweep and the idle worker leave
it alone whatever the heartbeat says (the lease is the stronger signal; ending it would fail the
task for good with its rows in the table). Once no live lease holds it, the sweep and the idle
worker apply the same rule to it: `SUCCEEDED` with the evidence (and no `QUERY_COMPLETED`; a
live worker records that itself), the lease message without. Every other `RUNNING` run with no
alive worker (an ad-hoc query, a Python task's input query, maintenance) is the sweep's.

A task whose lease is lost while its current write run (`task_runs.query_run_id`) shows the
evidence is ended `succeeded` and not retried: the tick decides it from the snapshot
(`TaskSnapshot.committed`), and `_apply_task` repeats the evidence in the `WHERE` of every
reclaim, `EXISTS` for the success and `NOT EXISTS` for the others, after a statement of its own
that locks the task's row (see there for why). The task, its open attempt and its write run end
together in one transaction.

The sweep ends each run in its own transaction (`_end_each`, the shape of `_apply_per_run`): a
run that raises while it is ended is rolled back alone and logged with its id and the exception's
type, the others are ended, and the tick returns normally, so one such run can neither block the
sweep nor make the scheduler loop back off. The same holds for deploy runs and for the idle
worker's ender.

The leader pauses the sweep, but not the task path, after a gap in its own ticks
(`SchedulerLoop`, see `lakana_worker.scheduler`), so that healthy workers can beat first.

The sweep ends deploy runs the same way (`_end_deploy_run`): a `RUNNING` deploy, dry run or
rollback with no alive worker is `FAILED` with `DEPLOY_LOST_MESSAGE`. It needs no evidence rule:
a deploy finishes in one transaction that first locks the run and checks it is still `RUNNING`
under the caller's worker id, so a lost deploy either finished completely or changed nothing.

A task whose asset has checks is an exception to it once its write is in the table:
it is stamped *written* or its write run shows the evidence (`TaskSnapshot.unchecked`,
`awaiting_checks`; the evidence outlives the stamp, which a rollback clears). Until the worker that
runs its checks records the verdict, nobody else ends it as `succeeded` (downstream would build on
unchecked data) or as `failed` (it cannot run out of attempts). Its lost lease puts it back to
`pending` at once and uncounted, the attempt `lost`, the stamp (if any) kept; the next claim runs
the checks only. Its statement requires that condition, and every other reclaim requires its
absence, so a write that lands between the snapshot and the statement turns the stale reclaim
into a miss. A cancel does not end it either: the table holds unchecked rows, so the task goes
to `ready` with `setting_back`, uncounted, and the claim after it only sets the table back. That
holds whether the cancel lands while its worker is lost (the reclaim does it) or while the task
waits in `pending` or `ready` for its next claim (the cancel loop does it). A live
worker hands its task back the same way from `record_attempt` (a cancel after the write, a memory
stop that ends the task for good), and the reclaim clears `error_message`, which that hand-back
uses for the stop's text.

A Python task with checks that may have written (`TaskSnapshot.may_have_written`: a baseline is
recorded and no stamp is) is handled the same way when its lease is lost and either no attempt is
left or its run is asked to cancel; a waiting task with checks, a baseline and no stamp
(`TaskSnapshot.baselined`: an earlier attempt may have written) is sent to set back by a cancel
too, instead of being ended. A claim that is itself lost while it sets back is put back
again (`task_runs.set_back_claims` counts them, at most `MAX_SET_BACK_CLAIMS`); after the limit
the task ends with `SET_BACK_LOST` through `TaskTransition.ends_set_back`. Each of these paths
repeats its own evidence in the `WHERE` of its statement, and every other reclaim requires
`NOT setting_back`, so a stale snapshot misses. A Python task without checks records no
baseline, so a lost one is not set back.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, Select, and_, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lakana_core.audit.content import stored_query_text
from lakana_core.audit.service import AuditService
from lakana_core.auth import Securable
from lakana_core.db import rowcount
from lakana_core.log_events import LogEvent, log_event
from lakana_core.models import (
    AttemptOutcome,
    AuditEventType,
    DeployRun,
    DeployStatus,
    Environment,
    QueryErrorCause,
    QueryRun,
    QueryRunStatus,
    Repository,
    Run,
    RunStatus,
    TaskAttempt,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.models.run import ACTIVE_RUN_STATUSES, TERMINAL_TASK_STATUSES
from lakana_core.notifications.events import queue_notification
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.scheduler import (
    COMMITTED_BEFORE_LOST_MESSAGE,
    RunSnapshot,
    RunTransition,
    TaskSnapshot,
    TaskTransition,
    Transition,
    tick,
)
from lakana_core.runs.workers import forget_lost, without_alive_worker

logger = logging.getLogger(__name__)

LEASE_EXPIRED_MESSAGE = (
    "lease expired: the worker stopped renewing it. If it was stopped for using too much "
    "memory, see the worker's memory limit in the documentation (LAKANA_WORKER__MEMORY_LIMIT, "
    "or LAKANA_WORKER_MEMORY in the Compose bundle's .env)."
)
"""`error_message` of an attempt the scheduler reclaims."""

WORKER_LOST_WRITE_MESSAGE = (
    "the worker running this task stopped renewing its lease. If it was stopped for using too "
    "much memory, see the worker's memory limit in the documentation (LAKANA_WORKER__MEMORY_LIMIT, "
    "or LAKANA_WORKER_MEMORY in the Compose bundle's .env)."
)
"""`error_message` of a task's query run (write or read) left `RUNNING` by a lost worker."""

CHECK_LOST_MESSAGE = (
    "The worker counting this check of a pipeline task was lost, so the count was ended. The "
    "check is counted again when the task is taken again."
)
"""`error_message` of the count of a check whose worker the sweep found lost.

A count is a task's own query run and not its write: it has no write mode and is not the
task's `query_run_id`.
"""

QUERY_LOST_MESSAGE = "The worker running this query was lost, so the query was ended. Run it again."
"""`error_message` of a lost read."""
WRITE_LOST_MESSAGE = (
    "The worker running this write was lost before it was committed, so the write was ended. "
    "Nothing was written to {target}. Run it again."
)
"""`error_message` of a lost write without evidence (`{target}` is the target's name)."""
MAINTENANCE_LOST_MESSAGE = (
    "The worker running this maintenance was lost. Part of it may be done; running it again "
    "is safe."
)
"""`error_message` of lost maintenance."""

DEPLOY_LOST_MESSAGE = (
    "The worker running this deploy was lost, so the deploy was ended. Nothing was activated. "
    "Deploy again."
)
"""`error_message` of a deploy run (deploy, dry run or rollback) whose worker was lost."""

SWEEP_BATCH = 100
"""Most lost runs of one kind (query or deploy) one tick ends; the rest wait for the next tick."""


def upstream_names(value: object) -> tuple[str, ...]:
    """Read `upstream_json`, tolerating a malformed value.

    Parameters
    ----------
    value
        The stored JSON.

    Returns
    -------
    tuple of str
        The string elements of a list; empty otherwise.
    """
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def load_snapshots(session: Session) -> list[RunSnapshot]:
    """Read every active run and its task runs, in two queries however many runs there are.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of RunSnapshot
        Oldest run first.
    """
    runs = session.scalars(
        select(Run)
        .where(Run.status.in_(ACTIVE_RUN_STATUSES))
        .order_by(Run.created_at, Run.id)
        .execution_options(populate_existing=True)
    ).all()
    if not runs:
        return []
    by_run: dict[uuid.UUID, list[tuple[TaskRun, bool]]] = {run.id: [] for run in runs}
    # One query for the tasks, with the evidence of each one's CURRENT write run
    # (`task_runs.query_run_id`; an earlier attempt's run is not linked any more).
    for task, rows_written in session.execute(
        select(TaskRun, QueryRun.rows_written)
        .outerjoin(QueryRun, QueryRun.id == TaskRun.query_run_id)
        .where(TaskRun.run_id.in_(by_run))
        .order_by(TaskRun.run_id, TaskRun.asset)
        .execution_options(populate_existing=True)
    ):
        by_run[task.run_id].append((task, rows_written is not None))
    return [
        RunSnapshot(
            id=run.id,
            status=run.status,
            cancel_requested=run.cancel_requested,
            stopped_by_check=run.error_message is not None,
            tasks=tuple(
                TaskSnapshot(
                    id=task.id,
                    asset=task.asset,
                    status=task.status,
                    attempt=task.attempt,
                    claim=task.claim_count,
                    max_attempts=task.max_attempts,
                    retry_delay_seconds=task.retry_delay_seconds,
                    next_attempt_at=task.next_attempt_at,
                    lease_owner=task.lease_owner,
                    lease_expires_at=task.lease_expires_at,
                    upstream=upstream_names(task.upstream_json),
                    committed=committed,
                    unchecked=task.has_checks
                    and task.status not in TERMINAL_TASK_STATUSES
                    and (task.written_table_id is not None or committed),
                    may_have_written=task.has_checks
                    and task.status is TaskStatus.RUNNING
                    and task.baseline_publishes is not None
                    and task.written_table_id is None
                    and not committed,
                    baselined=task.has_checks
                    and task.baseline_publishes is not None
                    and task.written_table_id is None,
                    setting_back=task.setting_back,
                    set_back_claims=task.set_back_claims,
                )
                for task, committed in by_run[run.id]
            ),
        )
        for run in runs
    ]


def _task_write_committed() -> ColumnElement[bool]:
    """Build "the task's current write run shows the evidence of a commit", for a `TaskRun` update.

    The link is `task_runs.query_run_id`, the same one the sweep's shield uses: an earlier
    attempt's write run is not linked any more and proves nothing about the current attempt.

    Returns
    -------
    ColumnElement[bool]
        An `EXISTS` correlated to the `TaskRun` being updated.
    """
    return (
        select(QueryRun.id)
        .where(QueryRun.id == TaskRun.query_run_id, QueryRun.rows_written.is_not(None))
        .exists()
    )


def awaiting_checks() -> ColumnElement[bool]:
    """Build "this task run has checks, its write is in the table, and nobody has ended it".

    The one definition of a task that awaits its verdict (`TaskSnapshot.unchecked`,
    `record_attempt`, the memory stop and the exhaustion rule all mean this). Its write is in the
    table when the write's commit stamped it (`written_table_id`) or when the task's current write
    run shows the evidence of a commit (`_task_write_committed`). The second half matters after a
    rollback, which clears the stamp in its own transaction before the worker records the
    verdict: a worker lost in between must not leave a task that counts as succeeded. A task
    without checks is never held by it.

    Returns
    -------
    ColumnElement[bool]
        A condition on the `TaskRun` being updated.
    """
    return and_(
        TaskRun.has_checks.is_(True),
        TaskRun.status.not_in(TERMINAL_TASK_STATUSES),
        or_(TaskRun.written_table_id.is_not(None), _task_write_committed()),
    )


def may_have_written() -> ColumnElement[bool]:
    """Build "this running task run may have written rows to its table that no check has passed".

    The database form of `TaskSnapshot.may_have_written`: it has checks, it recorded a
    baseline (a Python table task does before its process starts), its write was not stamped
    and its current write run shows no commit. A statement that carries it misses when the
    stamp landed after the snapshot was read.

    Returns
    -------
    ColumnElement[bool]
        A condition on the `TaskRun` being updated.
    """
    return and_(
        TaskRun.has_checks.is_(True),
        TaskRun.status == TaskStatus.RUNNING,
        TaskRun.baseline_publishes.is_not(None),
        TaskRun.written_table_id.is_(None),
        ~_task_write_committed(),
    )


def _baselined() -> ColumnElement[bool]:
    """Build "this task run has checks, recorded a baseline and is not stamped", any status.

    The database form of `TaskSnapshot.baselined`: an earlier attempt may have left rows in the
    table that no check has passed.

    Returns
    -------
    ColumnElement[bool]
        A condition on the `TaskRun` being updated.
    """
    return and_(
        TaskRun.has_checks.is_(True),
        TaskRun.baseline_publishes.is_not(None),
        TaskRun.written_table_id.is_(None),
    )


def _waiting_with_unchecked_rows() -> ColumnElement[bool]:
    """Build "a cancel must set this waiting task's table back instead of ending the task".

    Its run is asked to cancel, it is not setting back yet, and its table may hold rows no check
    has passed: its write awaits its checks (`awaiting_checks`) or it has a baseline and no stamp
    (`_baselined`). The cancel loop's set-back requires it, and its plain cancel requires its
    absence, so a snapshot that is stale either way misses.

    Returns
    -------
    ColumnElement[bool]
        A condition on the `TaskRun` being updated.
    """
    return and_(
        _run_asked_to_cancel(),
        TaskRun.setting_back.is_(False),
        or_(awaiting_checks(), _baselined()),
    )


def _run_asked_to_cancel() -> ColumnElement[bool]:
    """Build "the run of this task run was asked to cancel", for a `TaskRun` update.

    Returns
    -------
    ColumnElement[bool]
        An `EXISTS` correlated to the `TaskRun` being updated.
    """
    return select(Run.id).where(Run.id == TaskRun.run_id, Run.cancel_requested.is_(True)).exists()


def _set_back_evidence() -> ColumnElement[bool]:
    """Build the evidence a reclaim that puts a task back to set its table back must still find.

    One alternative per way the scheduler takes: the task is already setting back (its
    claim was lost), or it awaits its checks in a run asked to cancel, or it may have written
    and the run is asked to cancel or no attempts are left.

    The alternatives are not tied to the path the snapshot took, and that is safe: while a task
    is running under a lapsed lease the facts only move one way (a stamp lands, a cancel
    arrives, and only the scheduler sets `setting_back`), and every alternative admits the
    same transition, so the statement never applies one that a fresh tick would not choose.

    Returns
    -------
    ColumnElement[bool]
        A condition on the `TaskRun` being updated.
    """
    return or_(
        TaskRun.setting_back.is_(True),
        and_(awaiting_checks(), _run_asked_to_cancel()),
        and_(
            may_have_written(),
            or_(_run_asked_to_cancel(), TaskRun.attempt >= TaskRun.max_attempts),
        ),
    )


def _apply_task(session: Session, transition: TaskTransition, now: datetime) -> bool:
    """Apply one task transition as a compare-and-swap.

    A reclaim of an expired lease (`lost`) decides on the evidence of a commit, in the
    statement itself: a reclaim to ``succeeded`` requires the task's current write run to show
    it, every other reclaim requires that it does not. A task that is written and has checks
    (`awaiting_checks`) is the exception to both: its reclaim (``unchecked``, to ``pending``)
    requires it, and no other reclaim may match it, whatever its write run shows, so a
    stamp that lands after the snapshot was read makes the stale reclaim miss and the next tick
    puts the task back instead. A reclaim that puts a task back to set its table back
    (``setting_back``) requires the evidence of a set-back path (`_set_back_evidence`), and every
    reclaim that does not requires that the task is not setting back: only the end of a lost
    set-back (``ends_set_back``) requires that it is. A retry or a cancel also misses a task that
    may have written in a run asked to cancel, so a cancel that lands after the snapshot sends
    the next tick down the set-back path. The cancel of a waiting task works the same way: the
    transition that sends it to set its table back requires `_waiting_with_unchecked_rows`, and
    the one that ends it requires its absence (with the claim counter it saw, a claim and a
    drain since the snapshot also miss). Before those statements the task's row is locked in a
    statement of its own. A write holds the task's row `FOR SHARE` from its fence to
    its commit (`ProtectedWriteGuard`), so the lock waits for a write that is committing, and the
    statements after it, which are new statements with new snapshots, see its evidence. Without
    that, a statement that waited for the lock would judge the evidence by the snapshot it took
    before the wait (Postgres re-checks a waited-for row only when it changed). Once the lock
    is held no write can commit for this task: its guard finds the task no longer running.

    Parameters
    ----------
    session
        Database session, inside the tick's transaction.
    transition
        The transition.
    now
        The tick's time.

    Returns
    -------
    bool
        Whether the row still was in the expected state.
    """
    values: dict[str, object] = {"status": transition.status}
    if transition.next_attempt_at is not None:
        values["next_attempt_at"] = transition.next_attempt_at
    if transition.error_message is not None:
        values["error_message"] = transition.error_message
    if transition.failure_code is not None:
        values["failure_code"] = transition.failure_code.value
    if transition.ready_at is not None:
        values["ready_at"] = transition.ready_at
    if transition.setting_back:
        values["setting_back"] = True
        # The reason the next worker ends the task with is its own: a lost worker, or the cancel.
        # Whatever an earlier attempt stored is not it (a hand-back stores the stop's text).
        values["error_message"] = transition.error_message
        values["failure_code"] = None
    if transition.terminal:
        values["finished_at"] = now
    committed = transition.lost and transition.status is TaskStatus.SUCCEEDED
    if transition.lost:
        values["lease_owner"] = None
        values["lease_expires_at"] = None
        if committed:
            values["error_message"] = COMMITTED_BEFORE_LOST_MESSAGE
    if "error_message" in values:
        # The state of a check phase's text describes that text only (`runs.check_message`).
        values["check_message_state"] = None
        session.execute(
            select(TaskRun.id).where(TaskRun.id == transition.task_id).with_for_update()
        )
    statement = update(TaskRun).where(
        TaskRun.id == transition.task_id, TaskRun.status == transition.expected
    )
    if transition.lost:
        # Mirror `tick`: only a lease that is still expired may be reclaimed. A late
        # renewal, or the task re-claimed since the snapshot, leaves it valid again.
        if transition.setting_back:
            evidence = _set_back_evidence()
        elif transition.ends_set_back:
            evidence = TaskRun.setting_back.is_(True)
        elif transition.unchecked:
            evidence = and_(awaiting_checks(), TaskRun.setting_back.is_(False))
        elif committed:
            evidence = and_(
                _task_write_committed(), ~awaiting_checks(), TaskRun.setting_back.is_(False)
            )
        else:
            evidence = and_(
                ~_task_write_committed(),
                ~awaiting_checks(),
                TaskRun.setting_back.is_(False),
                ~and_(may_have_written(), _run_asked_to_cancel()),
            )
        statement = statement.where(
            or_(TaskRun.lease_expires_at.is_(None), TaskRun.lease_expires_at < now),
            evidence,
        )
    elif transition.setting_back:
        statement = statement.where(_waiting_with_unchecked_rows())
    elif transition.status is TaskStatus.CANCELLED:
        statement = statement.where(~_waiting_with_unchecked_rows())
    if transition.expected_claim is not None:
        # Clock-independent ABA guard: a re-claim since the snapshot has a higher
        # claim counter, even when an uncounted retry repeated the attempt number.
        statement = statement.where(TaskRun.claim_count == transition.expected_claim)
    if transition.lost or transition.expected_owner is not None:
        # `== None` renders `IS NULL`: a reclaim that saw no owner expects none.
        statement = statement.where(TaskRun.lease_owner == transition.expected_owner)
    result = session.execute(
        statement.values(**values).execution_options(synchronize_session=False)
    )
    if rowcount(result) != 1:
        return False
    if transition.lost:
        outcome: dict[str, object] = {
            "outcome": AttemptOutcome.LOST,
            "finished_at": now,
            "error_message": LEASE_EXPIRED_MESSAGE,
            "failure_code": FailureCode.WORKER_LOST.value,
        }
        if committed:
            outcome = {
                "outcome": AttemptOutcome.SUCCEEDED,
                "finished_at": now,
                "error_message": COMMITTED_BEFORE_LOST_MESSAGE,
                "rows_written": select(QueryRun.rows_written)
                .join(TaskRun, TaskRun.query_run_id == QueryRun.id)
                .where(TaskRun.id == transition.task_id)
                .scalar_subquery(),
            }
        session.execute(
            update(TaskAttempt)
            .where(
                TaskAttempt.task_run_id == transition.task_id,
                TaskAttempt.finished_at.is_(None),
            )
            .values(**outcome)
            .execution_options(synchronize_session=False)
        )
        # A run of this task with the evidence landed, whichever attempt's it is; the rest is
        # left running by a worker that is gone.
        session.execute(
            update(QueryRun)
            .where(
                QueryRun.task_run_id == transition.task_id,
                QueryRun.status == QueryRunStatus.RUNNING,
                QueryRun.rows_written.is_not(None),
            )
            .values(status=QueryRunStatus.SUCCEEDED, finished_at=now)
            .execution_options(synchronize_session=False)
        )
        session.execute(
            update(QueryRun)
            .where(
                QueryRun.task_run_id == transition.task_id,
                QueryRun.status == QueryRunStatus.RUNNING,
            )
            .values(
                status=QueryRunStatus.FAILED,
                error_message=WORKER_LOST_WRITE_MESSAGE,
                finished_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    return True


def _queue_notification(session: Session, run_id: uuid.UUID, job: str, now: datetime) -> None:
    """Queue the notification of a run that just finished, without ever stopping the finish.

    It is written in the finishing transaction, so a rolled-back finish queues nothing. The queuing
    runs in a savepoint: a database error rolls back only the notification, is logged, and the run
    still ends. A webhook never changes a run.

    Parameters
    ----------
    session
        Database session, inside the tick's transaction.
    run_id
        The run.
    job
        Its job, for the log.
    now
        The tick's time.
    """
    try:
        with session.begin_nested():
            # Read again: the compare-and-swap did not touch an object the session may hold.
            run = session.get(Run, run_id, populate_existing=True)
            if run is not None:
                queue_notification(session, run, now)
    except SQLAlchemyError:
        logger.warning(
            "could not queue the notification of run %s (job %s); the run finished without it",
            run_id,
            job,
        )


def _apply_run(session: Session, transition: RunTransition, now: datetime) -> bool:
    """Apply one run transition as a compare-and-swap, auditing a finalization.

    Parameters
    ----------
    session
        Database session, inside the tick's transaction.
    transition
        The transition.
    now
        The tick's time.

    Returns
    -------
    bool
        Whether the run still was in the expected state.
    """
    values: dict[str, object] = {"status": transition.status}
    if transition.status is RunStatus.RUNNING:
        values["started_at"] = now
    if transition.counts is not None:
        values["finished_at"] = now
    result = session.execute(
        update(Run)
        .where(Run.id == transition.run_id, Run.status == transition.expected)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if rowcount(result) != 1:
        return False
    if transition.counts is not None:
        row = session.execute(
            select(Run.triggered_by, Environment.name, Repository.name, Run.job)
            .join(Environment, Environment.id == Run.environment_id)
            .join(Repository, Repository.id == Run.repository_id)
            .where(Run.id == transition.run_id)
        ).one()
        AuditService(session).record(
            row[0],
            AuditEventType.RUN_FINISHED,
            securable=Securable.environment_scope(row[1]),
            detail={
                "run_id": str(transition.run_id),
                "repository": row[2],
                "status": transition.status.value,
                "counts": dict(transition.counts),
            },
        )
        if row[3] is not None:
            _queue_notification(session, transition.run_id, row[3], now)
    return True


def _apply_group(session: Session, transitions: Sequence[Transition], now: datetime) -> bool:
    """Apply one run's transitions in a single transaction.

    Parameters
    ----------
    session
        Database session.
    transitions
        One run's transitions, in order.
    now
        The tick's time.

    Returns
    -------
    bool
        `True` if every compare-and-swap matched and the transaction
        committed; `False` after a rollback.
    """
    for transition in transitions:
        if isinstance(transition, TaskTransition):
            applied = _apply_task(session, transition, now)
        else:
            applied = _apply_run(session, transition, now)
        if not applied:
            session.rollback()
            return False
    session.commit()
    return True


def _apply_per_run(session: Session, transitions: Sequence[Transition], now: datetime) -> int:
    """Apply each run's transitions as its own all-or-nothing transaction.

    Parameters
    ----------
    session
        Database session.
    transitions
        `tick`'s output; a run's transitions keep their relative order.
    now
        The tick's time.

    Returns
    -------
    int
        Transitions in the runs that committed.
    """
    by_run: dict[uuid.UUID, list[Transition]] = {}
    for transition in transitions:
        by_run.setdefault(transition.run_id, []).append(transition)
    return sum(len(group) for group in by_run.values() if _apply_group(session, group, now))


def apply_transitions(session: Session, transitions: Sequence[Transition], now: datetime) -> bool:
    """Apply a tick's transitions, all or nothing per run.

    Parameters
    ----------
    session
        Database session; each run's group is committed on success and rolled
        back on a miss.
    transitions
        `tick`'s output, in order.
    now
        The tick's time.

    Returns
    -------
    bool
        `True` if every run's compare-and-swaps matched. `False` if any run
        missed; the other runs have still been applied.
    """
    return _apply_per_run(session, transitions, now) == len(transitions)


@dataclass(frozen=True)
class LostOutcome:
    """How a query run whose worker is gone ends.

    Attributes
    ----------
    status
        `SUCCEEDED` or `FAILED`.
    message
        The failure's `error_message`; `None` for `SUCCEEDED`.
    audited
        Whether the end is recorded as `QUERY_COMPLETED`.
    """

    status: QueryRunStatus
    message: str | None
    audited: bool


def has_committed(*, write_mode: WriteMode | None, rows_written: int | None) -> bool:
    """Say whether a run's write is known to have committed (the one shared predicate).

    The mark is `rows_written IS NOT NULL` and nothing else: the fence writes it, `0` included
    (an empty append, a view, an empty create), in the transaction of the commit
    (`ProtectedWriteGuard.record_pending`). `snapshot_id` is null for some committed writes and
    `target_table_id` is set at submission, so neither is evidence. A maintenance run is never
    fenced, so it never counts. A SQL task's own write counts like any other once nobody
    holds it by a live task lease (callers decide that: `held_by_task_lease`).
    `committed_write` is the same predicate for a `WHERE` clause.

    Parameters
    ----------
    write_mode
        The run's write mode.
    rows_written
        The run's `rows_written`.

    Returns
    -------
    bool
        Whether the run is to be ended `SUCCEEDED` when its worker is lost or stopped.
    """
    return rows_written is not None and write_mode is not WriteMode.MAINTAIN


def task_write_condition() -> ColumnElement[bool]:
    """Build the condition "this run is a SQL task's own write" (no task token is bound).

    Returns
    -------
    ColumnElement[bool]
        A condition on `QueryRun`.
    """
    return and_(QueryRun.task_run_id.is_not(None), QueryRun.task_token_id.is_(None))


def is_check_count(session: Session, run: QueryRun) -> bool:
    """Say whether a task's own query run is the count of a check, not the task's write.

    A write has a write mode and a target, and a SQL task's write is bound as its task's
    `query_run_id`; a count has none of them. Any mark makes the run a write, so a real write
    is never taken for a count. The refusal of a kept table (`lakana_core.runs.kept`) rests
    on this same definition.

    Parameters
    ----------
    session
        Database session; read only.
    run
        A query run with a task run and no task token.

    Returns
    -------
    bool
        Whether the run has no write mode and no target and is not its task's current write
        run.
    """
    if run.write_mode is not None or run.target_name is not None:
        return False
    bound = session.execute(
        select(TaskRun.query_run_id).where(TaskRun.id == run.task_run_id)
    ).scalar_one_or_none()
    return bound != run.id


def held_by_task_lease(now: datetime) -> ColumnElement[bool]:
    """Build the condition "this is a SQL task's current write and the task's lease is live".

    Only the task's current write counts (`TaskRun.query_run_id` is this run): the write of an
    earlier attempt that was left `RUNNING` is not shielded by a later attempt's lease. The
    task's lease owns such a run (`_apply_task` ends it once the lease is lost); the
    worker's heartbeat does not decide. Ending it by the heartbeat under a live lease made the
    task fail for good with its rows in the table (I2). The lease is judged by the database's
    clock, as `_apply_task` does (`lease_expires_at < now` is expired).

    Parameters
    ----------
    now
        The database's current time.

    Returns
    -------
    ColumnElement[bool]
        A condition on `QueryRun`.
    """
    return and_(
        task_write_condition(),
        select(TaskRun.id)
        .where(
            TaskRun.id == QueryRun.task_run_id,
            TaskRun.query_run_id == QueryRun.id,
            TaskRun.status == TaskStatus.RUNNING,
            TaskRun.lease_expires_at >= now,
        )
        .exists(),
    )


def committed_write() -> ColumnElement[bool]:
    """Build `has_committed` for a `WHERE` clause.

    Returns
    -------
    ColumnElement[bool]
        A condition on `QueryRun`: evidence, and not maintenance.
    """
    return and_(
        QueryRun.rows_written.is_not(None),
        QueryRun.write_mode.is_distinct_from(WriteMode.MAINTAIN),
    )


def lost_outcome(
    *,
    write_mode: WriteMode | None,
    target_name: str | None,
    rows_written: int | None,
    task_write: bool,
    check: bool = False,
) -> LostOutcome:
    """Decide how a query run whose worker is gone ends.

    A write that has its evidence did commit: it ends `SUCCEEDED`, because "failed, run it
    again" would be untrue and would append twice. A SQL task's own write ends the same way
    (and the task follows, `_apply_task`), but with no `QUERY_COMPLETED` in either case: its
    worker records that when it records the task's outcome. Without evidence a task's write
    is failed with the lease message and no event; the task's lease decides about a retry.

    Parameters
    ----------
    write_mode
        The run's write mode (`None` for a read).
    target_name
        The write's target as submitted.
    rows_written
        The run's evidence.
    task_write
        Whether the run is a task's own run (no task token is bound): a SQL task's write, or
        the count of a check.
    check
        Whether such a run is the count of a check and not the task's write; it then
        gets a text that does not speak of a write. Nothing else differs.

    Returns
    -------
    LostOutcome
        The status, the message and whether to audit.
    """
    if has_committed(write_mode=write_mode, rows_written=rows_written):
        return LostOutcome(QueryRunStatus.SUCCEEDED, None, audited=not task_write)
    if task_write:
        message = CHECK_LOST_MESSAGE if check else WORKER_LOST_WRITE_MESSAGE
        return LostOutcome(QueryRunStatus.FAILED, message, audited=False)
    if write_mode is None:
        return LostOutcome(QueryRunStatus.FAILED, QUERY_LOST_MESSAGE, audited=True)
    if write_mode is WriteMode.MAINTAIN:
        return LostOutcome(QueryRunStatus.FAILED, MAINTENANCE_LOST_MESSAGE, audited=True)
    return LostOutcome(
        QueryRunStatus.FAILED, WRITE_LOST_MESSAGE.format(target=target_name), audited=True
    )


def _lost_query_runs_statement(now: datetime, lease_seconds: int) -> Select[QueryRun]:
    """Build the read of the `RUNNING` query runs no alive worker holds.

    Parameters
    ----------
    now
        The database's current time.
    lease_seconds
        The lease length.

    Returns
    -------
    Select
        Oldest first, at most `SWEEP_BATCH`, `FOR NO KEY UPDATE SKIP LOCKED`.
    """
    return (
        select(QueryRun)
        .where(
            QueryRun.status == QueryRunStatus.RUNNING,
            without_alive_worker(QueryRun, now, lease_seconds),
            ~held_by_task_lease(now),
        )
        .order_by(QueryRun.created_at, QueryRun.id)
        .limit(SWEEP_BATCH)
        .with_for_update(key_share=True, skip_locked=True, of=QueryRun)
        .execution_options(populate_existing=True)
    )


def _lost_query_runs(session: Session, now: datetime, lease_seconds: int) -> list[QueryRun]:
    """Read the `RUNNING` query runs no alive worker holds, oldest first, locked.

    `FOR NO KEY UPDATE SKIP LOCKED`: a run whose row a write holds exclusively at this instant
    (the fence, a few statements before its commit) is passed by and looked at again on the next
    tick. The scheduler therefore never waits for a row lock. A `FOR KEY SHARE` lock (the lineage
    edge's foreign key, during a write's metadata upload) does not make it skip the row: a worker
    frozen there is swept, and its late commit meets a run that is no longer `RUNNING`.

    Parameters
    ----------
    session
        Database session.
    now
        The database's current time.
    lease_seconds
        The lease length.

    Returns
    -------
    list[QueryRun]
        The candidates; the decision is made by the `UPDATE` that ends each one.
    """
    return list(session.scalars(_lost_query_runs_statement(now, lease_seconds)))


def _end_query_run(session: Session, run: QueryRun, now: datetime, lease_seconds: int) -> bool:
    """End one lost run by a compare-and-swap on everything the decision was read from.

    The `WHERE` holds the status, the evidence as it was read, "still no alive worker" and "not
    a SQL task's write under a live task lease"; nothing is decided from the ORM object alone.
    The statement sets status, message, cause and `finished_at` and never the fence's fields
    (`rows_written`, `snapshot_id`, `target_table_id`).

    Parameters
    ----------
    session
        Database session; the caller commits.
    run
        A candidate from `_lost_query_runs`.
    now
        The database's current time.
    lease_seconds
        The lease length.

    Returns
    -------
    bool
        Whether this call ended the run; `False` leaves it for the next tick.
    """
    return _end_where(
        session,
        run,
        now,
        and_(without_alive_worker(QueryRun, now, lease_seconds), ~held_by_task_lease(now)),
    )


def _end_where(
    session: Session, run: QueryRun, now: datetime, holder_gone: ColumnElement[bool]
) -> bool:
    """End one run by the evidence rule, if `holder_gone` and the rest still hold at the UPDATE.

    Parameters
    ----------
    session
        Database session; the caller commits.
    run
        The candidate, read earlier.
    now
        The database's current time.
    holder_gone
        The condition, repeated in the `WHERE`, that says nobody is running this run.

    Returns
    -------
    bool
        Whether this call ended the run.
    """
    task_write = run.task_run_id is not None and run.task_token_id is None
    outcome = lost_outcome(
        write_mode=run.write_mode,
        target_name=run.target_name,
        rows_written=run.rows_written,
        task_write=task_write,
        check=task_write and is_check_count(session, run),
    )
    values: dict[str, object] = {"status": outcome.status, "finished_at": now}
    if outcome.status is QueryRunStatus.FAILED:
        values["error_message"] = outcome.message
        values["error_cause"] = QueryErrorCause.WORKER_LOST.value
    same_evidence = (
        QueryRun.rows_written.is_(None)
        if run.rows_written is None
        else QueryRun.rows_written.is_not(None)
    )
    ended = session.execute(
        update(QueryRun)
        .where(
            QueryRun.id == run.id,
            QueryRun.status == QueryRunStatus.RUNNING,
            holder_gone,
            same_evidence,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if rowcount(ended) != 1:
        return False
    if outcome.audited:
        record_query_completed(
            session,
            run,
            status=outcome.status,
            row_count=run.rows_written if outcome.status is QueryRunStatus.SUCCEEDED else None,
        )
    return True


def _own_running_runs(
    session: Session, worker_id: str, now: datetime, run_id: uuid.UUID | None = None
) -> list[QueryRun]:
    """Read the `RUNNING` query runs that carry one worker's id, except those a task lease holds.

    Parameters
    ----------
    session
        Database session.
    worker_id
        The worker's id.
    now
        The database's current time.
    run_id
        Read only this run (if it is still a candidate); `None` reads the batch.

    Returns
    -------
    list[QueryRun]
        Oldest first, at most `SWEEP_BATCH`, `FOR NO KEY UPDATE SKIP LOCKED`.
    """
    statement = (
        select(QueryRun)
        .where(
            QueryRun.status == QueryRunStatus.RUNNING,
            QueryRun.worker_id == worker_id,
            ~held_by_task_lease(now),
        )
        .order_by(QueryRun.created_at, QueryRun.id)
        .limit(SWEEP_BATCH)
        .with_for_update(key_share=True, skip_locked=True, of=QueryRun)
        .execution_options(populate_existing=True)
    )
    if run_id is not None:
        statement = statement.where(QueryRun.id == run_id)
    return list(session.scalars(statement))


def _end_orphan(session: Session, run: QueryRun, worker_id: str, now: datetime) -> bool:
    """End one run that carries this worker's own id, by the sweep's statement and rule.

    Parameters
    ----------
    session
        Database session; the caller commits.
    run
        A candidate from `_own_running_runs`.
    worker_id
        The worker's id, repeated in the `WHERE`.
    now
        The database's current time.

    Returns
    -------
    bool
        Whether this call ended the run.
    """
    return _end_where(
        session, run, now, and_(QueryRun.worker_id == worker_id, ~held_by_task_lease(now))
    )


def end_orphaned_runs(session: Session, worker_id: str, now: datetime) -> int:
    """End the `RUNNING` query and deploy runs that carry this worker's id while it holds none (I3).

    A worker calls this when its main loop is idle, so nothing it started is still running: a
    run that still carries its id is one whose final status could not be written (a dropped
    connection, a database outage longer than the lease). The sweep never takes it, because the
    worker is alive. It ends by the sweep's rule and statement: `SUCCEEDED` when the write left
    its evidence, otherwise `FAILED` with the cause `worker_lost` and the lost-worker message
    that fits the kind of run, with the same single `QUERY_COMPLETED`. Each run ends in its own
    transaction (`_end_each`): one that raises is logged and left for the next call. A SQL task's
    own write is
    included unless a live task lease holds it (`held_by_task_lease`, the sweep's one
    definition): then the task path owns it; otherwise (the task already recorded its outcome
    and the write was left `RUNNING`) it ends by the same evidence rule, `SUCCEEDED` or the lease
    message, with no `QUERY_COMPLETED` either way.

    A `RUNNING` deploy run (deploy, dry run or rollback) under this id is ended `FAILED` with
    `DEPLOY_LOST_MESSAGE` by the sweep's statement (`_end_deploy_where`), with no audit event and
    in its own transaction. A deploy finishes in one transaction, so a final write that failed
    published nothing and the message ("Nothing was activated") is true. A deploy in flight is
    never at risk: it runs inline in the worker's poll loop, which calls this only between items.

    Parameters
    ----------
    session
        Database session; every run is committed (or rolled back) on its own here.
    worker_id
        The calling worker's id. Runs of any other worker are never touched.
    now
        The database's current time.

    Returns
    -------
    int
        The number of query and deploy runs ended.
    """
    candidate_ids = [run.id for run in _own_running_runs(session, worker_id, now)]
    deploy_ids = [run.id for run in _own_running_deploys(session, worker_id)]
    session.rollback()  # the candidate locks end here; each run is read again below

    def end_one(run_id: uuid.UUID) -> bool:
        """End one orphan, read again under its own lock.

        Parameters
        ----------
        run_id
            The candidate's id.

        Returns
        -------
        bool
            Whether this call ended the run.
        """
        return any(
            _end_orphan(session, run, worker_id, now)
            for run in _own_running_runs(session, worker_id, now, run_id)
        )

    def end_deploy(run_id: uuid.UUID) -> bool:
        """End one leftover deploy, read again under its own lock.

        Parameters
        ----------
        run_id
            The candidate's id.

        Returns
        -------
        bool
            Whether this call ended the run.
        """
        return any(
            _end_deploy_orphan(session, run, worker_id, now)
            for run in _own_running_deploys(session, worker_id, run_id)
        )

    return _end_each(session, candidate_ids, end_one) + _end_each(session, deploy_ids, end_deploy)


def query_completed_detail(
    run: QueryRun, *, tables_touched: list[str], row_count: int | None, status: QueryRunStatus
) -> dict[str, object]:
    """Build the detail of a query run's `QUERY_COMPLETED` event.

    One builder for everyone who ends a query run (its worker, the sweep, a cancel), so that
    the event reads the same whoever wrote it. A query that shows the rows of a check reads a
    snapshot that an ordinary query of the table cannot reach (a table that is not published,
    or a state that was set back), so its event names the check result. The key is left out
    for every other query, whose event is then exactly what it always was.

    Parameters
    ----------
    run
        The query run (its `sql_text` and `check_result_id` are read).
    tables_touched
        The dotted names the query's own text reads; empty when they were never extracted.
    row_count
        The rows returned or written; `None` for a run that did not succeed.
    status
        The final status.

    Returns
    -------
    dict
        The detail: `sql_text` (as `LAKANA_AUDIT__QUERY_TEXT` says: the text, its SHA-256 and
        length, or `None`; `lakana_core.audit.content.stored_query_text`), `tables_touched`,
        `row_count`, `status`, and `check_result_id`
        (the id as text) only for a run bound to a check result.
    """
    detail: dict[str, object] = {
        "sql_text": stored_query_text(run.sql_text),
        "tables_touched": tables_touched,
        "row_count": row_count,
        "status": status.value,
    }
    if run.check_result_id is not None:
        detail["check_result_id"] = str(run.check_result_id)
    return detail


def record_query_completed(
    session: Session, run: QueryRun, *, status: QueryRunStatus, row_count: int | None
) -> None:
    """Record the `QUERY_COMPLETED` event of an ad-hoc run that someone other than its worker ended.

    There is one such event per run, written by whoever ended it: the sweep, a cancel of a lost
    run, or the worker itself. A run whose submitter was deleted has nobody to attribute it to
    and records nothing.

    Parameters
    ----------
    session
        Database session; the caller commits.
    run
        The run (its `principal_id`, `environment_id`, `sql_text` and `check_result_id` are
        read).
    status
        The final status.
    row_count
        The rows written, for `SUCCEEDED`.
    """
    if run.principal_id is None:
        return
    environment = session.execute(
        select(Environment.name).where(Environment.id == run.environment_id)
    ).scalar_one()
    AuditService(session).record(
        run.principal_id,
        AuditEventType.QUERY_COMPLETED,
        securable=Securable.environment_scope(environment),
        detail=query_completed_detail(run, tables_touched=[], row_count=row_count, status=status),
    )


def end_lost_run(session: Session, run_id: uuid.UUID, now: datetime, lease_seconds: int) -> bool:
    """End one `RUNNING` run no alive worker holds, as the sweep would (a cancel uses this).

    Parameters
    ----------
    session
        Database session; the caller commits.
    run_id
        The run.
    now
        The database's current time.
    lease_seconds
        The lease length.

    Returns
    -------
    bool
        Whether this call ended the run; `False` when it is not lost, was ended meanwhile, or a
        worker holds its row at this instant (the sweep takes it on a later tick).
    """
    candidates = session.scalars(
        _lost_query_runs_statement(now, lease_seconds).where(QueryRun.id == run_id)
    )
    return any(_end_query_run(session, run, now, lease_seconds) for run in candidates)


def _lost_deploy_runs_statement(now: datetime, lease_seconds: int) -> Select[DeployRun]:
    """Build the read of the `RUNNING` deploy runs no alive worker holds.

    Parameters
    ----------
    now
        The database's current time.
    lease_seconds
        The lease length.

    Returns
    -------
    Select
        Oldest first, at most `SWEEP_BATCH`, `FOR NO KEY UPDATE SKIP LOCKED`.
    """
    return (
        select(DeployRun)
        .where(
            DeployRun.status == DeployStatus.RUNNING,
            without_alive_worker(DeployRun, now, lease_seconds),
        )
        .order_by(DeployRun.created_at, DeployRun.id)
        .limit(SWEEP_BATCH)
        .with_for_update(key_share=True, skip_locked=True, of=DeployRun)
        .execution_options(populate_existing=True)
    )


def _lost_deploy_runs(
    session: Session, now: datetime, lease_seconds: int, run_id: uuid.UUID | None = None
) -> list[DeployRun]:
    """Read the `RUNNING` deploy runs no alive worker holds, oldest first, locked.

    `FOR NO KEY UPDATE SKIP LOCKED`, like the query runs: a deploy that is finishing holds its
    run's row (`DeployService._hold`, `FOR UPDATE`) from the check to the commit, and the sweep
    passes that row by and finds the run finished on the next tick. No foreign key points at
    `deploy_runs`, so no other lock on the row can make the sweep skip a run for long.

    Parameters
    ----------
    session
        Database session.
    now
        The database's current time.
    lease_seconds
        The lease length.
    run_id
        Read only this run (if it is still a candidate); `None` reads the batch.

    Returns
    -------
    list[DeployRun]
        The candidates; the decision is made by the `UPDATE` that ends each one.
    """
    statement = _lost_deploy_runs_statement(now, lease_seconds)
    if run_id is not None:
        statement = statement.where(DeployRun.id == run_id)
    return list(session.scalars(statement))


def _end_deploy_where(
    session: Session, run: DeployRun, now: datetime, holder_gone: ColumnElement[bool]
) -> bool:
    """End one deploy run by a compare-and-swap on its status and `holder_gone`.

    The one statement the sweep and the idle worker share. A deploy has no evidence rule,
    unlike a write. Pointer swap, project version, status and audit event of a deploy are one
    transaction (`DeployService.record_deploy`, `record_dry_run`, `execute_rollback`), and that
    transaction first locks the run and checks it is still `RUNNING` under the caller's worker
    id. A deploy therefore either finished completely (its run is then not `RUNNING`, and this
    statement matches nothing) or has changed nothing visible, and `holder_gone` is enough to end
    it. That holds also when the worker is alive and could not write its final status: the
    finishing transaction failed as a whole, so nothing was published. No audit event is
    written: `DeployService.fail` writes none either.

    Parameters
    ----------
    session
        Database session; the caller commits.
    run
        A candidate read earlier.
    now
        The database's current time.
    holder_gone
        The condition, repeated in the `WHERE`, that says nobody is running this deploy.

    Returns
    -------
    bool
        Whether this call ended the run; `False` leaves it as it is.
    """
    ended = session.execute(
        update(DeployRun)
        .where(DeployRun.id == run.id, DeployRun.status == DeployStatus.RUNNING, holder_gone)
        .values(
            status=DeployStatus.FAILED,
            error_message=DEPLOY_LOST_MESSAGE,
            finished_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    return rowcount(ended) == 1


def _end_deploy_run(session: Session, run: DeployRun, now: datetime, lease_seconds: int) -> bool:
    """End one lost deploy run: no alive worker holds it (the sweep).

    Parameters
    ----------
    session
        Database session; the caller commits.
    run
        A candidate from `_lost_deploy_runs`.
    now
        The database's current time.
    lease_seconds
        The lease length.

    Returns
    -------
    bool
        Whether this call ended the run; `False` leaves it for the next tick.
    """
    return _end_deploy_where(session, run, now, without_alive_worker(DeployRun, now, lease_seconds))


def _own_running_deploys_statement(worker_id: str) -> Select[DeployRun]:
    """Build the read of the `RUNNING` deploy runs that carry one worker's id.

    Parameters
    ----------
    worker_id
        The worker's id.

    Returns
    -------
    Select
        Oldest first, at most `SWEEP_BATCH`, `FOR NO KEY UPDATE SKIP LOCKED`.
    """
    return (
        select(DeployRun)
        .where(DeployRun.status == DeployStatus.RUNNING, DeployRun.worker_id == worker_id)
        .order_by(DeployRun.created_at, DeployRun.id)
        .limit(SWEEP_BATCH)
        .with_for_update(key_share=True, skip_locked=True, of=DeployRun)
        .execution_options(populate_existing=True)
    )


def _own_running_deploys(
    session: Session, worker_id: str, run_id: uuid.UUID | None = None
) -> list[DeployRun]:
    """Read the `RUNNING` deploy runs that carry one worker's id, locked.

    Parameters
    ----------
    session
        Database session.
    worker_id
        The worker's id.
    run_id
        Read only this run (if it is still a candidate); `None` reads the batch.

    Returns
    -------
    list[DeployRun]
        The candidates; the decision is made by the `UPDATE` that ends each one.
    """
    statement = _own_running_deploys_statement(worker_id)
    if run_id is not None:
        statement = statement.where(DeployRun.id == run_id)
    return list(session.scalars(statement))


def _end_deploy_orphan(session: Session, run: DeployRun, worker_id: str, now: datetime) -> bool:
    """End one deploy run that carries this worker's own id, by the sweep's statement.

    Parameters
    ----------
    session
        Database session; the caller commits.
    run
        A candidate from `_own_running_deploys`.
    worker_id
        The worker's id, repeated in the `WHERE`.
    now
        The database's current time.

    Returns
    -------
    bool
        Whether this call ended the run.
    """
    return _end_deploy_where(session, run, now, DeployRun.worker_id == worker_id)


def _end_each(
    session: Session, run_ids: Sequence[uuid.UUID], end_one: Callable[[uuid.UUID], bool]
) -> int:
    """End each run in its own transaction; one that raises is logged and left for the next tick.

    The shape of `_apply_per_run`, with one addition: an exception rolls back that run only. It
    is logged with the run's id and the exception's type (never the message, which can carry
    SQL) and the next run goes on, so a run that raises on every tick neither stops the others
    nor makes the tick fail (the scheduler loop would back off). The candidate locks belong to
    the reading transaction and are released before the first run: `end_one` reads its run again
    under a lock of its own, and the ending `UPDATE` carries every condition in its `WHERE`.

    Parameters
    ----------
    session
        Database session, with no transaction open.
    run_ids
        The candidates' ids, oldest first.
    end_one
        Ends one run without committing; returns whether it did.

    Returns
    -------
    int
        The number of runs that were ended and committed.
    """
    ended = 0
    for run_id in run_ids:
        try:
            won = end_one(run_id)
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.warning(
                "Ending run %s failed (%s); it stays as it is and is tried again on the "
                "next tick. Look for the cause in the database's log.",
                run_id,
                type(exc).__name__,
            )
            continue
        ended += won
    return ended


def end_lost_runs(session: Session, now: datetime, lease_seconds: int) -> int:
    """End the `RUNNING` query and deploy runs no alive worker holds; returns how many.

    Each run ends in its own transaction (`_end_each`): a run that raises while it is ended is
    logged and stays for the next tick; the others are ended and the call returns normally.

    Parameters
    ----------
    session
        Database session; every run is committed (or rolled back) on its own here.
    now
        The database's current time (never a worker's clock).
    lease_seconds
        The lease length (`LAKANA_RUN__LEASE_SECONDS`).

    Returns
    -------
    int
        The number of runs this call ended.
    """
    query_ids = [run.id for run in _lost_query_runs(session, now, lease_seconds)]
    deploy_ids = [run.id for run in _lost_deploy_runs(session, now, lease_seconds)]
    session.rollback()  # the candidate locks end here; each run is read again below

    def end_deploy(run_id: uuid.UUID) -> bool:
        """End one deploy run, read again under its own lock.

        Parameters
        ----------
        run_id
            The candidate's id.

        Returns
        -------
        bool
            Whether this call ended the run.
        """
        return any(
            _end_deploy_run(session, run, now, lease_seconds)
            for run in _lost_deploy_runs(session, now, lease_seconds, run_id)
        )

    ended = _end_each(
        session, query_ids, lambda run_id: end_lost_run(session, run_id, now, lease_seconds)
    ) + _end_each(session, deploy_ids, end_deploy)
    if ended:
        logger.warning(
            "Ended %d query or deploy run(s) whose worker stopped reporting for %d s "
            "(LAKANA_RUN__LEASE_SECONDS); they show the code worker_lost. Look at that "
            "worker's log and memory limit.",
            ended,
            lease_seconds,
            extra=log_event(LogEvent.WORKER_LOST),
        )
    return ended


def scheduler_tick(
    session: Session,
    now: datetime,
    rand: Callable[[], float],
    lease_seconds: int | None = None,
    *,
    forget: bool = True,
) -> int:
    """Run one scheduler tick against the database.

    Applies the task transitions, then, when a lease is given, sweeps the query and deploy runs
    of lost workers and forgets the rows of workers silent for an hour. The sweep runs also when
    there was nothing to do for tasks.

    Parameters
    ----------
    session
        Database session.
    now
        The current time (the database's, in production).
    rand
        Jitter source.
    lease_seconds
        `LAKANA_RUN__LEASE_SECONDS`. `None` runs no sweep (the default keeps existing callers
        as they were; the worker passes the setting, and withholds it for a while after a gap
        in its own ticks).
    forget
        Whether a sweeping tick also deletes the rows of workers silent for an hour; the worker
        asks for it at most once a minute.

    Returns
    -------
    int
        Transitions applied (0 when there was nothing to do or every run's
        snapshot went stale); the sweep is not counted.
    """
    transitions = tick(load_snapshots(session), now, rand)
    applied = 0
    if transitions:
        applied = _apply_per_run(session, transitions, now)
    else:
        session.rollback()
    if lease_seconds is not None:
        end_lost_runs(session, now, lease_seconds)
        if forget:
            forget_lost(session, now, lease_seconds)
        session.commit()
    return applied
