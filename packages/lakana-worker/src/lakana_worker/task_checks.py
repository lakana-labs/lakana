"""The check phase of a task run: count every check, record the results, act on the verdict.

A task whose table has data quality checks has a second phase (`check_phase`):
after the write, every check of the table is counted as a query of the run's principal
(`lakana_worker.checks.count_check`), the results are recorded, and the verdict ends the task.
Nothing blocking: the task succeeded. A blocking check failed or could not run: the catalog
rolls the table back (`CatalogService.roll_back`, under this claim's run context) and the task
fails for good. This module is the only caller of that rollback and the only code that says
a checked table passed. What the phase must never do:

- **build twice.** A claim that finds the task *written* (`read_written`) runs the checks
  only; a claim that finds a rollback of this task run already landed ends the task failed
  from the recorded results (`before_build`).
- **fail a written task for good without a verdict.** Whatever the phase cannot do for a
  reason of its own is a retryable failure: the task goes back to pending with its stamp, and
  the next claim checks again. An unexpected error while one check is counted makes that
  check `errored`, which blocks like a failed one.
- **act for a claim it no longer holds, or after a cancel.** Both are looked for after every
  count and after a refused rollback, in the database, not only by the keeper.
- **roll back twice.** One call per claim; an outcome the catalog could not read back is
  reported as not known and never retried here.

A task of kind ``source_check`` has only this phase, and no rollback: the run did not write
the table. It is made for a table the project only reads, and for a table the project builds
that was kept after a failed check and is read without being rebuilt; the latter
never sets or moves a note, clears it when its checks pass, and fails with the message of
`kept_check_message` when they do not.

A Python asset can commit and then fail, so its stamp cannot land with its commit. Before the
first process of a task run with checks the table's state is recorded once as the task run's
baseline (`lakana_core.runs.tasks.record_baseline`); every attempt's *written* record starts
from it, and a failure that ends the task while the table is no longer at the baseline sets
the table back to it (`_after_a_failed_build`). While an attempt is left nothing is set back:
the next attempt replaces the table, which holds unchecked data until then. A worker lost
after the write on its last attempt, or in a run that is cancelled, is followed by a claim
that only sets the table back (`set_back_only`): it builds and checks nothing.

What such a rollback undoes is the task run's own chain of writes and nothing else. The
write guard records the tip of that chain in each write's own commit
(`task_runs.own_snapshot_id`, `ProtectedWriteGuard._advance_own_tip`), and the rollback names
the tip as "after", never what the table happens to be at. A commit by anyone else, before,
between or after the task run's writes, makes the catalog answer ``changed_since``; a task run
without a tip whose table is not at its baseline did not change it (`foreign_change`).
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef, RunContext
from lakana_core.catalog.identity import resolve_table_id, resolve_table_with_note
from lakana_core.catalog.metadata import read_metadata
from lakana_core.catalog.service import ROLLBACK_NOT_KNOWN_REASON, CatalogService, Rollback
from lakana_core.db import db_now
from lakana_core.errors import KeptAfterCheckError, NotPublishedError, PermissionDenied
from lakana_core.identifiers import TableRef, shown
from lakana_core.kept_mark import KeptSeen
from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    Environment,
    JobVersion,
    Principal,
    RollbackOutcome,
    Run,
    Table,
    TableType,
    TaskRun,
    TaskStatus,
)
from lakana_core.projects.checks import CheckSpec, read_manifest_checks
from lakana_core.projects.manifest import manifest_assets
from lakana_core.runs.checks import (
    ERROR_KEPT,
    ERROR_NO_CHECKS,
    ERROR_NO_PRINCIPAL,
    ERROR_NOT_PUBLISHED,
    ERROR_UNREADABLE_CHECK,
    RETRY_MESSAGE,
    RUN_CANCELLED_MESSAGE,
    SET_BACK_NOTHING,
    SOURCE_CHECK,
    build_failure_message,
    error_of_exception,
    error_text,
    kept_check_message,
    kept_failure_message,
    kept_no_checks_error,
    mark_rollback_failed,
    record_result,
    refused_source_message,
    results_of_task,
    run_stop_message,
    set_back_ending,
    set_back_failed_message,
    set_back_result_message,
    unchecked_retry_message,
    verdict,
    warning_note,
)
from lakana_core.runs.checks import failure_message as check_failure_message
from lakana_core.runs.failures import FailureCode, check_failure_code
from lakana_core.runs.kept import (
    KeptNote,
    clear_kept_in_attempt,
    kept_run_of,
    kept_unchecked_of,
    kept_unknown_of,
    note_kept,
    record_clean_in_attempt,
)
from lakana_core.runs.model_precheck import model_problem
from lakana_core.runs.scheduler import COMMITTED_BEFORE_RECORDED_MESSAGE, LOST_MESSAGE
from lakana_core.runs.tasks import (
    Baseline,
    ClaimedTask,
    FailureKind,
    Written,
    mark_written,
    read_baseline,
    read_own_tip,
    read_written,
    read_written_with_note,
)
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import checks
from lakana_worker.activity import ACTIVITY
from lakana_worker.executor import cancellation
from lakana_worker.task_integrity import check_manifest_version
from lakana_worker.task_lease import LeaseKeeper, LeaseLost, cancel_kind, keeper_for
from lakana_worker.task_outcome import (
    NO_PRINCIPAL_MESSAGE,
    NO_RUN_MESSAGE,
    RUN_ERROR_MESSAGE,
    TaskFailure,
    TaskOutcome,
    classify,
    failure_message,
    stop_code,
)

if TYPE_CHECKING:
    from lakana_worker.tasks import RunEngine

logger = logging.getLogger("lakana_worker.tasks")
"""The name `lakana_worker.tasks` logged under before the split, kept for log filters."""


_NOT_RECORDED_REASON = "what this run wrote was not recorded"
"""`Rollback.reason` when a built task has no *written* record to roll back by."""


_LANDED = frozenset({RollbackOutcome.ROLLED_BACK, RollbackOutcome.UNPUBLISHED})
"""The rollback outcomes that took the run's write out of the table (and cleared the stamp)."""


class _SetBackFailed(Exception):
    """A failed build's table could not be set back to its baseline; the build's failure stands."""


def _held(session: Session, engine: RunEngine, claimed: ClaimedTask) -> bool:
    """Read whether the task is still this worker's running task of this claim.

    The keeper learns of a lost lease only at a renewal; the check phase asks the database
    before it calls a failed count a failed check, and after a refused rollback.

    Parameters
    ----------
    session
        Database session; rolled back first, so the read is current.
    engine
        The run engine.
    claimed
        The claim.

    Returns
    -------
    bool
        Whether the claim is held.
    """
    session.rollback()
    held = session.execute(
        select(TaskRun.id).where(
            TaskRun.id == claimed.task_run_id,
            TaskRun.status == TaskStatus.RUNNING,
            TaskRun.lease_owner == engine.settings.worker_id,
            TaskRun.claim_count == claimed.claim,
        )
    ).first()
    session.rollback()
    return held is not None


def _check_lines(results: Sequence[CheckResult]) -> tuple[str, ...]:
    """Build the log lines of an attempt's checks.

    Parameters
    ----------
    results
        The results of the task run, by check name.

    Returns
    -------
    tuple of str
        One line per check.
    """
    lines = []
    for result in results:
        name = shown(result.check_name.rpartition("/")[2])
        if result.outcome is CheckOutcome.PASSED:
            found = "passed"
        elif result.outcome is CheckOutcome.ERRORED:
            found = f"errored: {result.error}"
        else:
            rows = "1 row" if result.failing_rows == 1 else f"{result.failing_rows} rows"
            found = f"{result.outcome.value}, {rows}"
        lines.append(f"  check {name}: {found}")
    return tuple(lines)


def before_build(
    session: Session, engine: RunEngine, claimed: ClaimedTask, environment: str | None
) -> TaskOutcome | None:
    """Decide whether a claim of a task with checks has anything to build.

    Three claims build nothing:

    - a ``source_check`` task: the project does not build that table;
    - a task that is *written*: its write is in the table and only its checks are missing.
      Building again could write an increment twice;
    - a task without a stamp whose recorded results say that its rollback landed
      (``rolled_back`` or ``unpublished``): a worker was lost between the rollback's commit,
      which clears the stamp, and the recording of the failure. The task ends failed from
      those results, with the message the verdict gave, and is neither built nor checked.
      Results belong to a task run, so an earlier run's never count here.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim, of a task with checks or of kind ``source_check``.
    environment
        Environment name, or `None` when the run is gone.

    Returns
    -------
    TaskOutcome or None
        `None` when the task must be built. Otherwise the outcome so far: a success to hand
        to the check phase, the permanent failure of a task that was rolled back already, or a
        transient failure when the task's state could not be read (nothing is built blind).
    """
    try:
        session.rollback()
        if claimed.kind == SOURCE_CHECK:
            return TaskOutcome(None, None, environment)
        if read_written(session, claimed.task_run_id) is not None:
            return TaskOutcome(None, None, environment)
        return _landed_rollback(session, claimed, environment)
    except Exception as exc:
        logger.exception(
            "Reading the state of task run %s failed (%s); nothing was built.",
            claimed.task_run_id,
            type(exc).__name__,
        )
        return TaskOutcome(
            FailureKind.TRANSIENT, RUN_ERROR_MESSAGE, environment, failure_code=FailureCode.INTERNAL
        )
    finally:
        session.rollback()


def _landed_rollback(
    session: Session, claimed: ClaimedTask, environment: str | None
) -> TaskOutcome | None:
    """End a task from its recorded results when its failed-check rollback already landed.

    A worker lost between the rollback's commit, which clears the stamp, and the recording of
    the failure leaves a task with no stamp whose blocking results say ``rolled_back`` or
    ``unpublished``. The verdict stands: the task ends failed with the message it gave.

    Parameters
    ----------
    session
        Database session.
    claimed
        The claim.
    environment
        Environment name, or `None` when the run is gone.

    Returns
    -------
    TaskOutcome or None
        The permanent failure, or `None` when no blocking result has a landed rollback.
    """
    results = results_of_task(session, claimed.task_run_id)
    blocking = verdict(results).blocking
    outcome = next((r.rollback_outcome for r in blocking if r.rollback_outcome in _LANDED), None)
    if outcome is None:
        return None
    message = check_failure_message(
        blocking,
        table=claimed.asset,
        run_id=claimed.run_id,
        environment=environment or "",
        rollback=Rollback(outcome),
    )
    return TaskOutcome(
        FailureKind.PERMANENT,
        message,
        environment,
        checks=(*_check_lines(results), f"  rollback: {outcome.value}"),
        failure_code=check_failure_code(blocking),
    )


def _run_stop(session: Session, run_id: uuid.UUID) -> str | None:
    """Read whether the run was asked to stop and the text that says why, on a fresh snapshot.

    Parameters
    ----------
    session
        Database session; rolled back before and after the read.
    run_id
        The run.

    Returns
    -------
    str or None
        The text for a task that ends because of it (`run_stop_message`), or `None` when the run
        does not exist or its cancel flag is not set.
    """
    session.rollback()
    found = session.execute(
        select(Run.cancel_requested, Run.error_message).where(Run.id == run_id)
    ).one_or_none()
    session.rollback()
    if found is None or not found[0]:
        return None
    return run_stop_message(found[1])


def _counted_outcome(spec: CheckSpec, rows: int) -> CheckOutcome:
    """Say what a count means for its check.

    Parameters
    ----------
    spec
        The check.
    rows
        The rows it returned.

    Returns
    -------
    CheckOutcome
        ``passed`` for no rows; otherwise ``failed`` for a `block` or `keep` check, ``warned``
        for a `warn` one.
    """
    if rows == 0:
        return CheckOutcome.PASSED
    return CheckOutcome.WARNED if spec.on_failure == "warn" else CheckOutcome.FAILED


def _stop_if_asked(session: Session, keeper: LeaseKeeper, run_id: uuid.UUID) -> None:
    """End the check phase if the run was cancelled or the worker drains.

    Parameters
    ----------
    session
        Database session.
    keeper
        The phase's lease keeper.
    run_id
        The run, whose cancel flag is re-read.

    Raises
    ------
    TaskFailure
        ``CANCELLED`` or ``RELEASED``. Nothing is rolled back: the table holds what the run
        wrote.
    """
    stop = cancel_kind(session, keeper, run_id)
    if stop is not None:
        raise TaskFailure(stop[0], stop[1] or RUN_CANCELLED_MESSAGE, stop_code(stop[0]))


def _count_one(
    session: Session,
    engine: RunEngine,
    claimed: ClaimedTask,
    keeper: LeaseKeeper,
    spec: CheckSpec,
    environment_id: uuid.UUID,
    principal: PrincipalRef,
) -> tuple[checks.Counted | None, CheckOutcome, str | None]:
    """Count one check and say what it means, or end the phase.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim.
    keeper
        The phase's running lease keeper.
    spec
        The check.
    environment_id
        The run's environment.
    principal
        The run's principal: who the check is counted as.

    Returns
    -------
    tuple
        The count (`None` when counting raised), the check's outcome, and the error text of
        an ``errored`` check. A check is ``errored`` when its count failed for a reason a
        retry would repeat, when it failed transiently on the last attempt, and when counting
        raised something that is not a database error: data that could not be checked must
        reach the verdict, never a bare failure of the task. A database error raised while
        counting (the insert of the count's run, the read of its status) is a transient
        failure like any other: a blip must not roll good data back.

    Raises
    ------
    LeaseLost
        If the claim is no longer held, or the worker is being stopped for memory (the
        watchdog records the attempt itself).
    TaskFailure
        ``CANCELLED`` or ``RELEASED`` when a failed count is really a stop; ``TRANSIENT``
        with `RETRY_MESSAGE` when the count failed transiently and an attempt is left.
    """
    counted: checks.Counted | None = None
    crash: str | None = None
    retryable = False
    try:
        with cancellation(keeper.cancel):
            counted = checks.count_check(
                session,
                spec=spec,
                environment_id=environment_id,
                principal_id=principal.id,
                task_run_id=claimed.task_run_id,
                worker_id=engine.settings.worker_id,
                authz=engine.authz_factory(session),
                key_provider=engine.key_provider,
                limits=engine.settings.limits,
            )
    except Exception as exc:
        logger.exception(
            "Counting a check of task run %s failed unexpectedly (%s).",
            claimed.task_run_id,
            type(exc).__name__,
        )
        crash = error_of_exception(exc)
        retryable = isinstance(exc, SQLAlchemyError)
    if keeper.lost or ACTIVITY.stopping:
        raise LeaseLost
    if counted is not None and counted.failure is None and counted.rows is not None:
        return counted, _counted_outcome(spec, counted.rows), None
    if not _held(session, engine, claimed):
        raise LeaseLost
    _stop_if_asked(session, keeper, claimed.run_id)
    cause = "internal"
    if counted is not None:
        retryable = classify(counted.failure) in {FailureKind.TRANSIENT, FailureKind.CONFLICT}
        cause = counted.error_cause or cause
    if retryable and claimed.attempt < claimed.max_attempts:
        raise TaskFailure(
            FailureKind.TRANSIENT,
            RETRY_MESSAGE.format(check=shown(spec.name), cause=cause),
            FailureCode.CHECK_ERROR,
        )
    if counted is None:
        return None, CheckOutcome.ERRORED, crash
    if isinstance(counted.failure, NotPublishedError):
        return counted, CheckOutcome.ERRORED, ERROR_NOT_PUBLISHED
    if isinstance(counted.failure, KeptAfterCheckError):
        # Whoever may read this run reads a check's `error`, and the refusal names a check
        # and a run of the project that builds the kept table. So the `error` names none of
        # them; the refusal in full is the message of the count's query run.
        return counted, CheckOutcome.ERRORED, ERROR_KEPT
    return (
        counted,
        CheckOutcome.ERRORED,
        error_text(counted.error_cause, counted.error_message, principal.name),
    )


def _set_back(
    session: Session,
    engine: RunEngine,
    claimed: ClaimedTask,
    keeper: LeaseKeeper,
    ref: TableRef,
    principal: PrincipalRef,
    written: Written | None,
    failed_checks: Sequence[str],
    foreign: bool = False,
    unchanged: bool = False,
) -> tuple[Rollback, bool]:
    """Ask the catalog, once, to set the task's table back to what it was before the run.

    The call runs under this claim's run context (the claim form), so the write guard holds it
    to this task, this claim and this asset, in a protected environment too. A refusal that
    is neither a lost claim nor a cancel is a refused rollback, noted on the failed results
    here because the catalog recorded nothing. `ROLLBACK_NOT_KNOWN_REASON` is handed on as it
    is and never followed by a second call: the first commit may still be landing.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim.
    keeper
        The phase's lease keeper.
    ref
        The task's table.
    principal
        The run's principal, without a run context.
    written
        What the task's write did; `None` when no record of it is left.
    failed_checks
        The names of the failed blocking checks; none when a failed build is set back.
    foreign
        The task run has no write of its own, and the table is not what it was before the run:
        the catalog records ``changed_since`` and touches nothing (`foreign_change`).
    unchanged
        The build committed nothing and `written` is `None` because there is no write: the
        outcome is ``nothing_written``, noted on the failed results without asking the catalog.

    Returns
    -------
    tuple of (Rollback, bool)
        What the rollback did, and whether the catalog refused it. A refusal can come after
        the catalog found nothing to set back, so it says only that the rollback changed
        nothing.

    Raises
    ------
    LeaseLost
        If the claim is no longer held.
    TaskFailure
        ``CANCELLED`` or ``RELEASED`` when the refusal is really a stop.
    """
    worker = engine.settings.worker_id
    refused = False
    if unchanged:
        rollback = Rollback(RollbackOutcome.NOTHING_WRITTEN)
    elif written is None:
        rollback = Rollback(RollbackOutcome.FAILED, _NOT_RECORDED_REASON)
    else:
        actor = dataclasses.replace(
            principal,
            run_context=RunContext(task_run_id=claimed.task_run_id, claim=claimed.claim),
        )
        catalog = CatalogService(
            session, StorageResolver(session, engine.key_provider), engine.authz_factory(session)
        )
        try:
            return (
                catalog.roll_back(
                    actor,
                    ref,
                    run_id=claimed.run_id,
                    task_run_id=claimed.task_run_id,
                    written=written,
                    failed_checks=list(failed_checks),
                    foreign_change=foreign,
                ),
                False,
            )
        except PermissionDenied as exc:
            if keeper.lost or not _held(session, engine, claimed):
                raise LeaseLost from None
            _stop_if_asked(session, keeper, claimed.run_id)
            rollback = Rollback(RollbackOutcome.FAILED, failure_message(exc, str(exc)))
            refused = True
    if not mark_rollback_failed(
        session,
        task_run_id=claimed.task_run_id,
        worker=worker,
        claim=claimed.claim,
        outcome=rollback.outcome,
    ):
        raise LeaseLost
    return rollback, refused


def _rollback_word(rollback: Rollback, refused: bool) -> str:
    """Name a rollback's outcome for the task log.

    Parameters
    ----------
    rollback
        What the rollback did.
    refused
        Whether the catalog refused it.

    Returns
    -------
    str
        ``refused``, ``not known``, or the outcome's value.
    """
    if refused:
        return "refused"
    if rollback.reason is ROLLBACK_NOT_KNOWN_REASON:
        return "not known"
    return rollback.outcome.value


@dataclass(frozen=True)
class _Phase:
    """What the check phase read about its run, as plain values.

    The counts and the rollback commit on the phase's session, so no ORM object is kept.

    Parameters
    ----------
    environment_id
        The run's environment.
    environment
        Its name.
    ref
        The task's table.
    principal
        The run's principal, without a run context.
    active
        Whether that principal is active.
    specs
        The checks of the task's table in the run's project version that can be read.
    unreadable
        Whether the project version holds a check entry that cannot be read and may be this
        table's.
    is_asset
        Whether the project version builds the task's table. A ``source_check`` task of such a
        table is the check task of a table that was kept after a failed check.
    """

    environment_id: uuid.UUID
    environment: str
    ref: TableRef
    principal: PrincipalRef
    active: bool
    specs: tuple[CheckSpec, ...]
    unreadable: bool
    is_asset: bool


def _read_phase(session: Session, claimed: ClaimedTask) -> _Phase:
    """Read what the check phase needs: the run, its project version's checks and its principal.

    The principal is the run's (`runs.run_as_principal_id`), never its trigger or the
    deployer: the counts are authorized and filtered as it, and the rollback is its write.

    Parameters
    ----------
    session
        Database session.
    claimed
        The claim.

    Returns
    -------
    _Phase
        The plain values.

    Raises
    ------
    TaskFailure
        Permanent, if the run or its project version is gone, the manifest's version is not one
        this worker reads, the run has no principal, or (for a ``source_check`` task, which
        wrote nothing) the principal is deactivated. `check_phase` makes it retryable for a
        task that built a table.
    """
    run = session.get(Run, claimed.run_id)
    env = session.get(Environment, run.environment_id) if run is not None else None
    version = session.get(JobVersion, run.job_version_id) if run is not None else None
    if run is None or env is None or version is None:
        raise TaskFailure(FailureKind.PERMANENT, NO_RUN_MESSAGE, FailureCode.NOT_FOUND)
    check_manifest_version(version.manifest_json)
    row = session.get(Principal, run.run_as_principal_id) if run.run_as_principal_id else None
    if row is None or (claimed.kind == SOURCE_CHECK and not row.is_active):
        raise TaskFailure(
            FailureKind.PERMANENT, NO_PRINCIPAL_MESSAGE, FailureCode.PERMISSION_DENIED
        )
    read = read_manifest_checks(version.manifest_json)
    phase = _Phase(
        environment_id=env.id,
        environment=env.name,
        ref=TableRef.from_dotted(env.name, claimed.asset),
        principal=PrincipalRef(id=row.id, name=row.name, type=row.type),
        active=row.is_active,
        specs=read.of(claimed.asset),
        unreadable=read.unreadable_for(claimed.asset),
        is_asset=claimed.asset in manifest_assets(version.manifest_json),
    )
    session.rollback()
    return phase


def _ends_the_task(session: Session, claimed: ClaimedTask, kind: FailureKind) -> bool:
    """Say whether recording this failure of a build ends the task, as `record_attempt` decides.

    Parameters
    ----------
    session
        Database session.
    claimed
        The claim, of a task that does not await its checks.
    kind
        How the build failed; not a cancel or a release.

    Returns
    -------
    bool
        `True` for a permanent failure, and for a retryable one on the last attempt (a
        conflict only once its free retry is used).
    """
    if kind is FailureKind.PERMANENT:
        return True
    if claimed.attempt < claimed.max_attempts:
        return False
    if kind is not FailureKind.CONFLICT:
        return True
    retried = session.execute(
        select(TaskRun.conflict_retried).where(TaskRun.id == claimed.task_run_id)
    ).scalar_one_or_none()
    session.rollback()
    return bool(retried)


def _table_now(
    session: Session, engine: RunEngine, ref: TableRef
) -> tuple[uuid.UUID, int | None, bool] | None:
    """Read a managed table's id, current snapshot and not-published mark, in-process.

    Parameters
    ----------
    session
        Database session; rolled back afterwards.
    engine
        The run engine.
    ref
        The table.

    Returns
    -------
    tuple or None
        The id, the current snapshot and whether the table is marked not published; `None`
        when there is no managed table of that name.
    """
    try:
        table_id = resolve_table_id(session, ref.environment, ref.namespace.joined(), ref.name)
        row = session.get(Table, table_id) if table_id is not None else None
        if row is None or row.table_type is not TableType.ICEBERG or row.metadata_location is None:
            return None
        resolved = StorageResolver(session, engine.key_provider).for_environment(ref.environment)
        metadata = read_metadata(resolved.file_io, row.metadata_location)
        return row.id, metadata.current_snapshot_id, row.unpublished_run_id is not None
    finally:
        session.rollback()


@dataclass(frozen=True)
class _BaselineSetBack:
    """What setting a table back to its baseline did.

    Parameters
    ----------
    phase
        What was read about the run on the way.
    rollback
        What the catalog did.
    refused
        Whether the catalog refused the rollback.
    """

    phase: _Phase
    rollback: Rollback
    refused: bool


def _set_back_to_baseline(
    session: Session,
    engine: RunEngine,
    claimed: ClaimedTask,
    keeper: LeaseKeeper,
    baseline: Baseline,
) -> _BaselineSetBack | None:
    """Set a Python asset's table back to its recorded baseline, unless it is there already.

    Reads the run, the table now and the task run's own tip, and then asks the catalog once.
    What is set back is the task run's own chain of writes and nothing else: with a tip the
    catalog compares the table with it, and a task run without one did not change the table,
    so a table that is not at the baseline was changed by another writer.

    Parameters
    ----------
    session
        Database session; rolled back afterwards.
    engine
        The run engine.
    claimed
        The claim.
    keeper
        The phase's running lease keeper.
    baseline
        The table's recorded state before the task run's first process.

    Returns
    -------
    _BaselineSetBack or None
        What was done; `None` when there is nothing to set back (no managed table, or the
        table is at the baseline and the task run has no tip).

    Raises
    ------
    LeaseLost
        If the claim is no longer held.
    TaskFailure
        A cancel or a drain found while the catalog refused, or what the run's own state
        lacks (`_read_phase`).
    """
    phase = _read_phase(session, claimed)
    now = _table_now(session, engine, phase.ref)
    if now is None:
        return None
    table_id, snapshot_id, unpublished = now
    tip = read_own_tip(session, claimed.task_run_id)
    session.rollback()
    at_baseline = snapshot_id == baseline.snapshot_id and (unpublished or not baseline.publishes)
    if tip is None and at_baseline:
        return None
    rollback, refused = _set_back(
        session,
        engine,
        claimed,
        keeper,
        phase.ref,
        phase.principal,
        Written(
            table_id,
            baseline.snapshot_id,
            None if tip is None else tip.snapshot_id,
            baseline.publishes,
        ),
        [],
        foreign=tip is None,
    )
    return _BaselineSetBack(phase, rollback, refused)


def _after_a_failed_build(
    session: Session,
    engine: RunEngine,
    claimed: ClaimedTask,
    built: TaskOutcome,
    keeper: LeaseKeeper,
) -> TaskOutcome:
    """Set a Python asset's table back to its baseline when a failed build ends the task.

    A Python asset can commit and then fail, in this attempt or in an earlier one of the same
    task run. While an attempt is left the next one replaces the table. When the failure ends
    the task, nobody would: the table would keep data that no check has seen. So the table is
    set back to the baseline that was recorded before the task run's first process
    (`read_baseline`), through the same `roll_back` as after a failed check, with no failed
    check to name. What is set back is the task run's own chain of writes and nothing else:
    the rollback names the chain's tip (`read_own_tip`), so the catalog answers
    ``changed_since`` when the table is not exactly as this task run left it; and a task run
    without a tip whose table is not at the baseline did not change it, so only that fact is
    recorded. A SQL asset has no baseline (its stamp lands with its commit) and a task
    without checks neither: both leave here unchanged.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim, of an asset task with checks whose build of this claim failed.
    built
        How the build failed; not a cancel or a release, and the task is not *written*.
    keeper
        The phase's running lease keeper.

    Returns
    -------
    TaskOutcome
        The build's outcome: unchanged when there is nothing to set back (no baseline, an
        attempt is left, the table is at the baseline); otherwise with the message that says
        what the failure was and what became of the table.

    Raises
    ------
    LeaseLost
        If the claim is no longer held.
    TaskFailure
        A cancel or a drain: nothing is set back.
    _SetBackFailed
        If the set-back could not be prepared or carried out for any other reason.
    """
    baseline = read_baseline(session, claimed.task_run_id)
    session.rollback()
    kind = built.kind
    if baseline is None or kind is None or not _ends_the_task(session, claimed, kind):
        return built
    try:
        _stop_if_asked(session, keeper, claimed.run_id)
        done = _set_back_to_baseline(session, engine, claimed, keeper, baseline)
        if done is None:
            return built
    except LeaseLost:
        raise
    except TaskFailure as failure:
        if failure.kind in {FailureKind.CANCELLED, FailureKind.RELEASED}:
            raise
        raise _SetBackFailed from failure
    except Exception as exc:
        logger.exception(
            "Setting the table of task run %s back after its failed build failed (%s).",
            claimed.task_run_id,
            type(exc).__name__,
        )
        raise _SetBackFailed from exc
    rollback, refused = done.rollback, done.refused
    phase = done.phase
    message = build_failure_message(
        built.message,
        table=claimed.asset,
        environment=phase.environment,
        rollback=rollback,
        refused=refused,
    )
    session.rollback()
    return dataclasses.replace(
        built, message=message, checks=(f"  rollback: {_rollback_word(rollback, refused)}",)
    )


def set_back_only(
    engine: RunEngine, claimed: ClaimedTask, keeper: LeaseKeeper
) -> TaskOutcome | None:
    """Set the table of a task back whose worker was lost after it may have written it.

    This claim builds nothing, runs no check, starts no process and mints no token, and a
    request to cancel the run does not stop it: setting the table back is the cancel's own
    work. The task is stamped when its write was recorded as *written*, and the table is set
    back from that record; otherwise from the baseline recorded before its first process,
    through the same steps as after a failed build. A set-back that already landed in an
    earlier claim left the table at its baseline and no tip, so it finds nothing to do.

    The task ends failed, or cancelled when the run was asked to cancel (read here before and
    again after the set-back, because the lease keeper does not report that for this claim),
    with the message of a lost worker or of the cancel, followed by what became of the table.
    A task that a live worker handed back (a memory stop that ends it for good after it may have
    written, a cancel seen after its write) ends the same way; the stop's own message, which
    `record_attempt` stored in the row, takes the place of the lost worker's (a run that is
    cancelled shows the cancel's message instead).
    A task without a stamp whose failed-check rollback already landed (its worker was lost
    before it recorded the failure) is not set back again: it ends failed from its recorded
    results, as `before_build` ends it.

    Parameters
    ----------
    engine
        The run engine.
    claimed
        The claim, with `setting_back` set.
    keeper
        The running lease keeper.

    Returns
    -------
    TaskOutcome or None
        What to record: ``failed``, ``cancelled`` or, for a worker that drains, ``released``.
        `None` when nothing is to be recorded (the claim is lost, or the database failed):
        the scheduler then finds the lease expired and decides again.
    """
    with engine.session_factory() as session:
        try:
            return _set_back_only(session, engine, claimed, keeper)
        except LeaseLost:
            return None
        except SQLAlchemyError as exc:
            logger.warning(
                "Setting the table of task run %s back failed on the database (%s); the "
                "scheduler decides again.",
                claimed.task_run_id,
                type(exc).__name__,
            )
            return None
        finally:
            session.rollback()


def _set_back_only(
    session: Session, engine: RunEngine, claimed: ClaimedTask, keeper: LeaseKeeper
) -> TaskOutcome:
    """Do the work of `set_back_only` on one session.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim, with `setting_back` set.
    keeper
        The running lease keeper.

    Returns
    -------
    TaskOutcome
        What to record.

    Raises
    ------
    LeaseLost
        If the claim is no longer held.
    SQLAlchemyError
        If the database failed.
    """
    found = session.execute(
        select(Environment.name, Run.cancel_requested, Run.error_message)
        .join(Run, Run.environment_id == Environment.id)
        .where(Run.id == claimed.run_id)
    ).one_or_none()
    session.rollback()
    environment, cancelled = (None, False) if found is None else (found[0], found[1])
    first_text = run_stop_message(found[2]) if cancelled and found is not None else None
    released = TaskOutcome(FailureKind.RELEASED, None, environment)
    if keeper.reason is FailureKind.RELEASED:
        return released
    done: _BaselineSetBack | None = None
    could_not = False
    try:
        written = read_written(session, claimed.task_run_id)
        baseline = read_baseline(session, claimed.task_run_id)
        landed = None if written is not None else _landed_rollback(session, claimed, environment)
        session.rollback()
        if landed is not None:
            # A failed-check rollback landed before the worker was lost: the table is set back
            # already, and the verdict is what the task ends with.
            return landed
        if written is not None:
            phase = _read_phase(session, claimed)
            tip = read_own_tip(session, claimed.task_run_id)
            session.rollback()
            rollback, refused = _set_back(
                session,
                engine,
                claimed,
                keeper,
                phase.ref,
                phase.principal,
                written,
                [],
                foreign=baseline is not None and tip is None,
            )
            done = _BaselineSetBack(phase, rollback, refused)
        elif baseline is not None:
            done = _set_back_to_baseline(session, engine, claimed, keeper, baseline)
    except (LeaseLost, SQLAlchemyError):
        raise
    except TaskFailure as failure:
        if failure.kind is FailureKind.RELEASED:
            return released
        logger.warning(
            "Setting the table of task run %s back failed: %s", claimed.task_run_id, failure.message
        )
        could_not = True
    except Exception as exc:
        logger.exception(
            "Setting the table of task run %s back failed (%s).",
            claimed.task_run_id,
            type(exc).__name__,
        )
        could_not = True
    finally:
        session.rollback()
    # Read the flag again: a cancel that lands while the table is set back ends the task
    # cancelled, as one that landed before the claim does.
    stop_text = _run_stop(session, claimed.run_id) or first_text
    cancelled = cancelled or stop_text is not None
    kind = FailureKind.CANCELLED if cancelled else FailureKind.PERMANENT
    # A task handed back by a worker that stopped itself for memory carries the stop's text; a
    # lost worker leaves none. A run with `cancel_requested` shows the text of its stop whatever
    # was stored: a cancel by a user, or a run `fail_run` failed (it sets the flag too).
    stored = session.execute(
        select(TaskRun.error_message, TaskRun.failure_code).where(TaskRun.id == claimed.task_run_id)
    ).one()
    session.rollback()
    reason = stop_text if stop_text is not None else (stored.error_message or LOST_MESSAGE)
    code = _set_back_code(kind, stored.failure_code)
    if could_not:
        return TaskOutcome(kind, set_back_failed_message(reason), environment, failure_code=code)
    if done is None:
        return TaskOutcome(
            kind,
            set_back_ending(reason, SET_BACK_NOTHING),
            environment,
            failure_code=code,
        )
    return TaskOutcome(
        kind,
        set_back_result_message(
            reason,
            done.rollback,
            done.refused,
            table=claimed.asset,
            environment=done.phase.environment,
        ),
        environment,
        checks=(f"  rollback: {_rollback_word(done.rollback, done.refused)}",),
        failure_code=code,
    )


def _set_back_code(kind: FailureKind, stored: str | None) -> FailureCode:
    """Pick the code a task ends with after its table was set back.

    Parameters
    ----------
    kind
        ``CANCELLED`` when the run was cancelled, else ``PERMANENT``.
    stored
        The code stored on the task run: the stop's, when a live worker handed the task back (a
        memory stop); none when the scheduler put it back because its worker was lost.

    Returns
    -------
    FailureCode
        ``cancelled`` for a cancel; otherwise the stored code, or ``worker_lost`` when there is
        none.
    """
    if kind is FailureKind.CANCELLED:
        return FailureCode.CANCELLED
    return FailureCode(stored) if stored is not None else FailureCode.WORKER_LOST


@dataclass
class _Progress:
    """What the check phase has found out so far, for the message of a failure of its own.

    Parameters
    ----------
    written
        Whether the task's write is in the table and waits for its checks: the build
        succeeded, the claim found the task *written*, or a build reported as failed for good
        turned out to be stamped. Never for a ``source_check`` task, which writes nothing.
    """

    written: bool = False


def _stand_in(table: str, label: str) -> CheckSpec:
    """Build the blocking check that stands for a check the project version cannot give.

    Parameters
    ----------
    table
        The task's table.
    label
        ``(none)`` or ``(unreadable)``, which a deploy refuses as the name of a check file.

    Returns
    -------
    CheckSpec
        A blocking check with no SQL, whose result is an error and whose name says which.
    """
    return CheckSpec(
        name=f"{table}/{label}",
        table=table,
        check=label,
        path=f"checks/{table.replace('.', '/')}/",
        on_failure="block",
        description=None,
        sql="",
    )


def _source_failure(
    session: Session,
    claimed: ClaimedTask,
    phase: _Phase,
    failing: Sequence[CheckResult],
    noted_table: uuid.UUID | None,
    refused: Mapping[str, str],
) -> str:
    """Build the message of a ``source_check`` task whose checks failed.

    A task of a table the project does not build says so. A task of a table the project builds
    is the check task of a kept table: it says what the note says, and of a check
    whose count was refused another project's kept table that it could not run, never that the
    table "still fails". A task of a table the project does not build whose count was refused
    a kept table says that, with the refusal's own text: the project cannot check a table that
    another project has to repair.

    Parameters
    ----------
    session
        Database session.
    claimed
        The claim.
    phase
        What `_read_phase` read.
    failing
        The failed results of the table's checks, blocking or `keep`.
    noted_table
        The table's id when it had a note when the phase began, else `None`.
    refused
        Check name to the text of the refusal, for each check of this phase whose count was
        refused a kept table.

    Returns
    -------
    str
        The message.
    """
    if not phase.is_asset:
        names = sorted(result.check_name for result in failing if result.check_name in refused)
        if names:
            return refused_source_message(
                [result for result in failing if result.check_name not in refused],
                table=claimed.asset,
                refusals=list(dict.fromkeys(refused[name] for name in names)),
            )
        return check_failure_message(
            failing,
            table=claimed.asset,
            run_id=claimed.run_id,
            environment=phase.environment,
            rollback=None,
        )
    kept_run = None if noted_table is None else kept_run_of(session, noted_table)
    unchecked = noted_table is not None and kept_unchecked_of(session, noted_table)
    unknown = noted_table is not None and kept_unknown_of(session, noted_table)
    session.rollback()
    return kept_check_message(
        [result for result in failing if result.check_name not in refused],
        table=claimed.asset,
        run_id=claimed.run_id,
        environment=phase.environment,
        kept_run_id=kept_run,
        without_checks=not phase.specs and not phase.unreadable,
        refused=[result for result in failing if result.check_name in refused],
        unchecked=unchecked,
        unknown=unknown,
    )


def _checks_under(
    session: Session,
    engine: RunEngine,
    claimed: ClaimedTask,
    built: TaskOutcome,
    keeper: LeaseKeeper,
    progress: _Progress,
) -> TaskOutcome:
    """Run the check phase of one claim under its lease keeper.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim.
    built
        How the build ended: a success, or a failure that is not a cancel or a release. A
        failed build is looked at for a write that is in the table all the same.
    keeper
        The phase's running lease keeper.
    progress
        Set to "written" once the phase knows that the table holds the task's write.

    Returns
    -------
    TaskOutcome
        What to record. After a success: the build's outcome when nothing blocks or is kept (with
        the warning note, if a `warn` check failed), else the permanent failure of the verdict;
        a failed `keep` check fails the task without a rollback.
        After a failed build: see `_after_a_failed_build`; a build that is *written* and
        failed for good is checked like a success, and one that is *written* and may be tried
        again is returned as it is (the next claim checks).

    Raises
    ------
    LeaseLost
        If the claim is no longer held, or the worker is being stopped: nothing is recorded.
    TaskFailure
        A cancel or a drain; a transient count failure with an attempt left; and whatever
        the phase needs and cannot read (the run, the manifest, the principal).
    """
    worker = engine.settings.worker_id
    source = claimed.kind == SOURCE_CHECK
    session.rollback()
    if built.kind is not None:
        if read_written(session, claimed.task_run_id) is None:
            return _after_a_failed_build(session, engine, claimed, built, keeper)
        if built.kind is not FailureKind.PERMANENT:
            return built
        # The build was reported failed for good, but its write is in the table: only the
        # checks may end this task.
        built = dataclasses.replace(
            built, kind=None, message=COMMITTED_BEFORE_RECORDED_MESSAGE, failure_code=None
        )
    progress.written = not source
    if built.written is not None:
        # A Python asset: the stamp names the tip of the task run's own writes, not what the
        # table was at when the worker looked. A commit by anyone else is then never "after".
        tip = read_own_tip(session, claimed.task_run_id)
        stamp = dataclasses.replace(
            built.written, snapshot_after=None if tip is None else tip.snapshot_id
        )
        if not mark_written(session, claimed.task_run_id, worker, claimed.claim, stamp):
            raise LeaseLost
    _stop_if_asked(session, keeper, claimed.run_id)
    phase = _read_phase(session, claimed)
    ref, principal = phase.ref, phase.principal
    written, seen = read_written_with_note(session, claimed.task_run_id)
    if written is not None:
        table_id = written.table_id
    else:
        found = resolve_table_with_note(
            session, phase.environment, ref.namespace.joined(), ref.name
        )
        table_id, seen = (None, KeptSeen(None, None, None)) if found is None else found
    noted_table = table_id if seen.noted else None
    session.rollback()
    # A task created with checks never succeeds without one: a project version that lists none that
    # can be read gives one blocking result that says so, and so does an entry that cannot be
    # read, beside the checks that can. A stand-in is run in place of a check.
    refusals: dict[str, str] = {}
    # The checks whose count was refused a kept table, with the refusal's text. Told by the
    # type of what failed the count, in the claim that counted: every check is counted again
    # in every check phase, so this never has to be read back from a stored text.
    kept_refusals: dict[str, str] = {}
    specs = list(phase.specs)
    if phase.unreadable:
        stand_in = _stand_in(claimed.asset, "(unreadable)")
        refusals[stand_in.name] = ERROR_UNREADABLE_CHECK
        specs.append(stand_in)
    elif not specs:
        stand_in = _stand_in(claimed.asset, "(none)")
        # The check task of the project's own kept table, when the project has removed the
        # table's checks since: the project version is as it should be, so the result says what
        # holds the readers back and that only a rebuild ends it. A note that is gone by now
        # leaves the general text.
        kept_run = (
            kept_run_of(session, noted_table)
            if source and phase.is_asset and noted_table is not None
            else None
        )
        kept_unchecked = (
            kept_run is not None
            and noted_table is not None
            and kept_unchecked_of(session, noted_table)
        )
        kept_unknown = (
            kept_run is not None
            and noted_table is not None
            and kept_unknown_of(session, noted_table)
        )
        session.rollback()
        refusals[stand_in.name] = (
            ERROR_NO_CHECKS
            if kept_run is None
            else kept_no_checks_error(
                table=claimed.asset,
                kept_run_id=kept_run,
                environment=phase.environment,
                unchecked=kept_unchecked,
                unknown=kept_unknown,
            )
        )
        specs.append(stand_in)
    for spec in specs:
        started = db_now(session)
        session.rollback()
        if spec.name in refusals:
            counted, outcome, error = None, CheckOutcome.ERRORED, refusals[spec.name]
        elif (
            phase.active
            and spec.generated is not None
            and (
                problem := model_problem(
                    session, engine.authz_factory(session), principal, phase.environment, spec
                )
            )
            is not None
        ):
            # A generated check whose columns or types are wrong is not counted: the count of
            # a float key would pass, and a join of two types that differ may not compare.
            session.rollback()
            counted, outcome, error = None, CheckOutcome.ERRORED, problem
        elif phase.active:
            counted, outcome, error = _count_one(
                session, engine, claimed, keeper, spec, phase.environment_id, principal
            )
            if counted is not None and isinstance(counted.failure, KeptAfterCheckError):
                kept_refusals[spec.name] = str(counted.failure)
        else:
            # Deactivated after the write: no check can run as it, and that blocks.
            counted, outcome, error = None, CheckOutcome.ERRORED, ERROR_NO_PRINCIPAL
        if not record_result(
            session,
            task_run_id=claimed.task_run_id,
            worker=worker,
            claim=claimed.claim,
            spec=spec,
            outcome=outcome,
            failing_rows=None if counted is None else counted.rows,
            error=error,
            table_id=table_id,
            checked_snapshot_id=None if counted is None else counted.checked_snapshot_id,
            policy_applied=counted is not None and counted.policy_applied,
            query_run_id=None if counted is None else counted.query_run_id,
            started_at=started if counted is None else counted.started_at,
        ):
            raise LeaseLost
    results = results_of_task(session, claimed.task_run_id)
    # Detached, so that the rollback's commit does not expire what the message is built from.
    session.expunge_all()
    session.rollback()
    found = verdict(results)
    lines = _check_lines(results)
    if not found.blocking and found.kept:
        # Only `keep` checks failed: the run's data stays in the table, and the task fails.
        # A `keep` check that could not run because a table it reads is kept says nothing
        # about this table: noting it would pass the note on from table to table. The task
        # fails all the same, and what reads its table is not built. The exception is a table
        # that carries a note already: this build replaced the data the note names, and
        # no check could run on the new data, so the note moves to this run and names the
        # checks as not run. The message is built from the same split as the note, so that it
        # never says the table keeps data that no note stands for.
        failed = [result for result in found.kept if result.check_name not in kept_refusals]
        # Only a task that built the table sets a note. A source-kind task only reads it: a
        # project must not mark a table it does not build for everyone, and a source table is
        # checked again in every run of the project.
        noted = KeptNote.NOTED
        if not source and table_id is not None and (failed or noted_table is not None):
            noted = note_kept(
                session,
                task_run_id=claimed.task_run_id,
                worker=worker,
                claim=claimed.claim,
                table_id=table_id,
                kept=failed or found.kept,
                unchecked=not failed,
            )
            if noted is KeptNote.NOT_HELD:
                raise LeaseLost
        dropped = noted is KeptNote.GONE
        message = (
            _source_failure(session, claimed, phase, found.kept, noted_table, kept_refusals)
            if source
            else kept_failure_message(
                kept=failed,
                run_id=claimed.run_id,
                environment=phase.environment,
                refused=[r for r in found.kept if r.check_name in kept_refusals],
                dropped=dropped,
            )
        )
        return dataclasses.replace(
            built,
            kind=FailureKind.PERMANENT,
            message=message,
            checks=lines,
            failure_code=check_failure_code(found.kept),
        )
    if not found.blocking:
        # The checks passed. The record of the task's success carries what follows, so that it
        # lands with the success or not at all: a worker lost between a clear and the record
        # leaves a task that is set back to data that had a note.
        judged = {r.checked_snapshot_id for r in results if r.checked_snapshot_id is not None}
        snapshot = judged.pop() if len(judged) == 1 else None
        task_run_id = claimed.task_run_id
        in_attempt = built.in_transaction
        if noted_table is not None and (not source or phase.is_asset):
            # A source-kind task of a table its project builds (a kept table checked again)
            # clears the note when its checks pass; a source check of a table another project
            # builds never does: only a run of that project clears it. Only the note the phase
            # began with is cleared: a note another run set while the counts ran is about data
            # these checks did not count.
            cleared_table = noted_table

            def clear(attempt: Session) -> None:
                """Clear the note in the transaction that records the success.

                Parameters
                ----------
                attempt
                    The session of `record_attempt`.
                """
                cleared = clear_kept_in_attempt(
                    attempt,
                    task_run_id=task_run_id,
                    table_id=cleared_table,
                    why="checks_passed",
                    snapshot_id=snapshot,
                    seen=seen,
                )
                if not cleared and snapshot is not None:
                    # Another run cleared the mark first, after judging some other snapshot.
                    # These checks passed on this one, so the table's verdict is theirs; an
                    # unmarked table only (a mark that landed since is left alone).
                    record_clean_in_attempt(attempt, cleared_table, snapshot)

            in_attempt = clear
        elif (
            not source
            and table_id is not None
            and snapshot is not None
            and seen.snapshot_id is not None
        ):
            # An unmarked table that has a history keeps the snapshot its last verdict was on
            # current, so that a later set-back to this data can be shown to be a verdict.
            clean_table = table_id

            def record_clean(attempt: Session) -> None:
                """Record the passing snapshot in the transaction that records the success.

                Parameters
                ----------
                attempt
                    The session of `record_attempt`.
                """
                record_clean_in_attempt(attempt, clean_table, snapshot)

            in_attempt = record_clean
        note = warning_note(found.warned) if found.warned else built.message
        return dataclasses.replace(built, message=note, checks=lines, in_transaction=in_attempt)
    if source:
        # A `keep` check that failed beside the blocking ones is named too: the task writes
        # nothing, so it is the same failure to the reader.
        message = _source_failure(
            session, claimed, phase, [*found.blocking, *found.kept], noted_table, kept_refusals
        )
    else:
        # A build that committed nothing and has no record of an earlier write left the table
        # as it was: there is nothing of this run to set back.
        unchanged = built.wrote_nothing and written is None
        # A Python asset (it has a baseline) without a tip wrote nothing of its own: whatever
        # is in the table and failed the check is another writer's, and stays.
        foreign = (
            not unchanged
            and read_baseline(session, claimed.task_run_id) is not None
            and read_own_tip(session, claimed.task_run_id) is None
        )
        session.rollback()
        rollback, refused = _set_back(
            session,
            engine,
            claimed,
            keeper,
            ref,
            principal,
            written,
            [result.check_name for result in found.blocking],
            foreign=foreign,
            unchanged=unchanged,
        )
        lines = (*lines, f"  rollback: {_rollback_word(rollback, refused)}")
        message = check_failure_message(
            found.blocking,
            table=claimed.asset,
            run_id=claimed.run_id,
            environment=phase.environment,
            rollback=rollback,
            refused=refused,
        )
    session.rollback()
    return dataclasses.replace(
        built,
        kind=FailureKind.PERMANENT,
        message=message,
        checks=lines,
        failure_code=check_failure_code(
            [*found.blocking, *found.kept] if source else found.blocking
        ),
    )


def check_phase(
    session: Session,
    engine: RunEngine,
    claimed: ClaimedTask,
    environment: str | None,
    built: TaskOutcome,
) -> TaskOutcome | None:
    """Count every check of the task's table, record the results, and act on the verdict.

    The second phase of a task with checks, under a lease keeper of
    its own: the build's keeper is stopped by then. Never raises for the task's own failure.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim, of a task with checks or of kind ``source_check``.
    environment
        Environment name for the log, or `None` when the run is gone.
    built
        How the build ended (a success with nothing built, for a claim that only checks; or a
        failure that is not a cancel or a release).

    Returns
    -------
    TaskOutcome or None
        What to record, or `None` when the attempt is no longer this worker's (a lost claim,
        a worker that is being stopped): then nothing is recorded.

        A task whose table holds the run's data never gets a permanent failure here except by
        the verdict: whatever the phase itself cannot do (read the run, the manifest or the
        principal; an unexpected error of its own bookkeeping) is a transient failure, so the
        task goes back to pending with its stamp and the next claim runs the checks again.
        Its message then also says that the table holds unchecked data and how to stop the
        run (`unchecked_retry_message`). A ``source_check`` task wrote nothing, and fails for
        good as a build would.
    """
    keeper = keeper_for(engine, claimed)
    keeper.start()
    progress = _Progress()
    try:
        outcome = _checks_under(session, engine, claimed, built, keeper, progress)
        keeper.stop()
        return None if keeper.lost else outcome
    except LeaseLost:
        return None
    except _SetBackFailed:
        # The build's own failure stands, with its kind; the table may hold what it wrote.
        return dataclasses.replace(built, message=set_back_failed_message(built.message))
    except TaskFailure as failure:
        kind, message = failure.kind, failure.message
        if kind is FailureKind.PERMANENT and claimed.kind != SOURCE_CHECK:
            # Only a task whose write is in the table gets here: what the phase needs is
            # read after that is known.
            kind = FailureKind.TRANSIENT
            message = unchecked_retry_message(message, claimed.run_id, environment or "")
        return dataclasses.replace(built, kind=kind, message=message, failure_code=failure.code)
    except Exception as exc:
        logger.exception(
            "Checking task run %s failed unexpectedly (%s); the checks run again.",
            claimed.task_run_id,
            type(exc).__name__,
        )
        message = RUN_ERROR_MESSAGE
        if progress.written:
            message = unchecked_retry_message(message, claimed.run_id, environment or "")
        return dataclasses.replace(
            built, kind=FailureKind.TRANSIENT, message=message, failure_code=FailureCode.INTERNAL
        )
    finally:
        keeper.stop()
