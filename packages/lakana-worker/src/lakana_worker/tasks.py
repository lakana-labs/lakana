"""Execute claimed task runs: integrity, run principal, the asset, leases, logs.

A SQL task runs as a `QueryRun` the worker inserts already `RUNNING` with
`task_run_id` set (so no poller claims it), executed by `pipeline.run_one`
inside a cancellation block; a Python task is handed to
`RunEngine.python`, which runs it as a subprocess while this loop keeps
serving query runs. Either way a `LeaseKeeper` thread renews the lease and
watches for a cancel request, a lost lease and the SIGTERM drain; it
interrupts DuckDB, or has the subprocess stopped, when one comes. Nothing a
task does propagates out of `run_claimed`.

A task whose table has data quality checks, and a task of kind ``source_check``, has a second
phase after the write: `lakana_worker.task_checks` counts the checks, records the results and
decides the task; `_run_claimed` hands the task to it (`check_phase`) and decides, with
`before_build`, whether a claim has anything to build.
"""

from __future__ import annotations

import logging
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import (
    Action,
    AuthorizationService,
    PrincipalRef,
    RunContext,
    Securable,
)
from lakana_core.catalog.identity import (
    namespace_exists,
    resolve_table_id,
    resolve_table_with_note,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.config import QueryLimitsConfig
from lakana_core.db import db_now, rowcount
from lakana_core.errors import (
    AlreadyExistsError,
    LakanaError,
    ValidationError,
)
from lakana_core.identifiers import NamespacePath, TableRef, shown
from lakana_core.log_events import LogEvent, log_event
from lakana_core.models import (
    AuditEventType,
    Environment,
    JobVersion,
    Principal,
    QueryRun,
    QueryRunStatus,
    Run,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.projects.asset_model import WRITE_CHOICES
from lakana_core.projects.checks import read_manifest_checks, source_check_tables
from lakana_core.projects.manifest import asset_kind, entry_write
from lakana_core.runs.checks import (
    ASSET,
    SOURCE_CHECK,
)
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.kept import clear_kept
from lakana_core.runs.scheduler import COMMITTED_BEFORE_RECORDED_MESSAGE
from lakana_core.runs.selection import ManifestGraph, unselected_upstreams
from lakana_core.runs.tasks import (
    ClaimedTask,
    FailureKind,
    fail_run,
    record_attempt,
)
from lakana_core.secrets.keys import KeyProvider
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.activity import ACTIVITY, TaskClaim
from lakana_worker.executor import (
    cancellation,
)
from lakana_worker.pipeline import run_one
from lakana_worker.python_support import CANNOT_RUN
from lakana_worker.task_checks import before_build, check_phase, set_back_only
from lakana_worker.task_integrity import check_manifest_version, verify_code
from lakana_worker.task_lease import LeaseKeeper, LeaseLost, cancel_kind, keeper_for
from lakana_worker.task_logs import write_task_log
from lakana_worker.task_outcome import (
    INTERNAL_ERROR_MESSAGE,
    NO_PRINCIPAL_MESSAGE,
    NO_RUN_MESSAGE,
    RUN_ERROR_MESSAGE,
    KeptTable,
    TaskFailure,
    TaskOutcome,
    classify,
    failure_code_of,
    failure_message,
    stop_code,
)

logger = logging.getLogger(__name__)

_MAX_VERIFIED = 4096
"""Runs a worker remembers having verified before it starts over."""
_NO_RUNTIME = CANNOT_RUN.format(reason="this worker has no Python runtime")


@dataclass(frozen=True)
class TaskSettings:
    """Worker-level task execution settings.

    Parameters
    ----------
    worker_id
        This worker's id (lease owner, attempt worker, scheduler holder).
    lease_seconds
        `LAKANA_RUN__LEASE_SECONDS`.
    renew_seconds
        `LAKANA_RUN__RENEW_SECONDS`.
    drain_grace_seconds
        `LAKANA_RUN__DRAIN_GRACE_SECONDS`.
    limits
        `LAKANA_QUERY_LIMITS__*`; a task's timeout is the smaller of its own and
        `limits.timeout_seconds`.
    """

    worker_id: str
    lease_seconds: int = 60
    renew_seconds: float = 20.0
    drain_grace_seconds: float = 20.0
    limits: QueryLimitsConfig = field(default_factory=QueryLimitsConfig)


def _system_random() -> Callable[[], float]:
    """Return the jitter source used in production.

    Returns
    -------
    callable
        `secrets.SystemRandom().random` (bandit-clean randomness).
    """
    return secrets.SystemRandom().random


@dataclass
class RunEngine:
    """Everything the dequeue loop needs to execute tasks.

    Parameters
    ----------
    session_factory
        Opens a fresh session.
    authz_factory
        Builds the authorization service for a session.
    settings
        Task settings.
    key_provider
        Decrypts secret-backed storage.
    drain
        Set on SIGTERM: stop claiming; release a running task after the grace.
    verified
        Runs whose project version tarball this process has verified; checked once
        per run, so a tarball tampered with between runs is caught.
    rand
        Jitter source.
    python
        Runs Python assets; `None` on a worker without a Python runtime.
    """

    session_factory: Callable[[], Session]
    authz_factory: Callable[[Session], AuthorizationService]
    settings: TaskSettings
    key_provider: KeyProvider | None = None
    drain: threading.Event = field(default_factory=threading.Event)
    verified: set[uuid.UUID] = field(default_factory=set)
    rand: Callable[[], float] = field(default_factory=_system_random)
    python: PythonAttempts | None = None


class PythonAttempts(Protocol):
    """What runs a claimed Python task; `lakana_worker.python_tasks.PythonRuntime` implements it."""

    def attempt(
        self,
        engine: RunEngine,
        claimed: ClaimedTask,
        keeper: LeaseKeeper,
        environment: str | None,
        serve_queries: Callable[[], bool] | None,
    ) -> TaskOutcome | None:
        """Run one attempt of a Python asset to its outcome.

        Parameters
        ----------
        engine
            The run engine.
        claimed
            The claim.
        keeper
            The running lease keeper.
        environment
            Environment name for the log, or `None` when the run is gone.
        serve_queries
            Runs one query-only poll iteration; called while the task's process runs.

        Returns
        -------
        TaskOutcome or None
            What to record, or `None` when the attempt is no longer this worker's.
        """
        ...


def manifest_entry(manifest: Mapping[str, object], asset: str) -> Mapping[str, object] | None:
    """Find an asset's manifest entry, tolerating a malformed manifest.

    Parameters
    ----------
    manifest
        The project version's manifest.
    asset
        The dotted name.

    Returns
    -------
    Mapping or None
        The entry, or `None`.
    """
    raw = manifest.get("assets")
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict) and entry.get("table") == asset:
            return entry
    return None


@dataclass(frozen=True)
class _Prepared:
    """What execution needs after preparation.

    Parameters
    ----------
    query_run_id
        The inserted write run.
    mode
        Its write mode.
    environment
        Environment name.
    kept_table
        The target and its note when it exists and keeps data that failed a check (it has a
        note), else `None`.
    """

    query_run_id: uuid.UUID
    mode: WriteMode
    environment: str
    kept_table: KeptTable | None = None


def check_integrity(
    session: Session, engine: RunEngine, run: Run, env: Environment, version: JobVersion
) -> None:
    """Verify the run's code tarball once per run on this worker.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    run
        The run.
    env
        Its environment.
    version
        Its project version.

    Raises
    ------
    TaskFailure
        Permanent when the tarball cannot be trusted; it carries the work that fails the run and
        audits `CODE_INTEGRITY_FAILED`, which lands in the transaction that records the
        attempt. Transient when storage cannot be read.
    """
    if run.id in engine.verified:
        return
    try:
        problem = verify_code(session, engine.key_provider, env, version)
    except OSError as exc:
        raise TaskFailure(
            FailureKind.TRANSIENT,
            "the code tarball could not be read (storage error)",
            FailureCode.INTERNAL,
        ) from exc
    if problem is not None:
        message = f"code integrity check failed: {problem}"
        event = {
            "run_id": str(run.id),
            "project_version_id": str(version.id),
            "problem": problem,
        }
        triggered_by = run.triggered_by
        scope = Securable.environment_scope(env.name)
        run_id = run.id

        def fail_with_attempt(inner: Session) -> None:
            """Audit the failure and fail the run, in the transaction of the attempt's record.

            Parameters
            ----------
            inner
                The session that records the attempt.
            """
            AuditService(inner).record(
                triggered_by, AuditEventType.CODE_INTEGRITY_FAILED, securable=scope, detail=event
            )
            fail_run(inner, run_id, message, commit=False)

        session.rollback()
        raise TaskFailure(
            FailureKind.PERMANENT, message, FailureCode.CODE_CHANGED, fail_with_attempt
        )
    if len(engine.verified) >= _MAX_VERIFIED:
        engine.verified.clear()
    engine.verified.add(run.id)


def _close_write(session: Session, query_run_id: uuid.UUID, message: str) -> None:
    """End a write run that will not go on, so it is not left `RUNNING`.

    A run whose write had already committed (it has `rows_written`) ends `SUCCEEDED`, the shape
    of the memory stop: two statements, each with the evidence in its own `WHERE`, so that a
    commit landing between them leaves the run `RUNNING` for the sweep, never `FAILED`. Any
    other run ends `FAILED` with `message`.

    Parameters
    ----------
    session
        Database session; commits.
    query_run_id
        The write run.
    message
        Its safe error text.
    """
    session.rollback()
    now = db_now(session)
    running = (QueryRun.id == query_run_id, QueryRun.status == QueryRunStatus.RUNNING)
    session.execute(
        update(QueryRun)
        .where(*running, QueryRun.rows_written.is_not(None))
        .values(status=QueryRunStatus.SUCCEEDED, finished_at=now)
        .execution_options(synchronize_session=False)
    )
    session.execute(
        update(QueryRun)
        .where(*running, QueryRun.rows_written.is_(None))
        .values(status=QueryRunStatus.FAILED, error_message=message, finished_at=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()


def checked_write(
    entry: Mapping[str, object], asset: str
) -> tuple[str, tuple[str, ...], str | None]:
    """Read how a run writes an asset's table, refusing what no run may do.

    Parameters
    ----------
    entry
        The asset's manifest entry; untrusted.
    asset
        The asset, for the message.

    Returns
    -------
    tuple
        ``(write, key, watermark)`` as `entry_write` reads them.

    Raises
    ------
    TaskFailure
        Permanent for a write mode this release does not know (never run as a replace,
        which would drop rows) and for a merge without a key.
    """
    written, merge_key, watermark = entry_write(entry)
    if written not in WRITE_CHOICES:
        raise TaskFailure(
            FailureKind.PERMANENT,
            f"the project version's write mode {written!r} for {asset} is not valid; "
            "deploy it again with this release",
            FailureCode.CODE_CHANGED,
        )
    if written == "merge" and not merge_key:
        raise TaskFailure(
            FailureKind.PERMANENT,
            f"the project version merges {asset} without a key; deploy it again with this release",
            FailureCode.CODE_CHANGED,
        )
    return written, merge_key, watermark


def ensure_namespaces(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    environment: str,
    parts: Sequence[str],
) -> None:
    """Create the asset's missing namespaces, tolerating a sibling task winning the race.

    Two tasks of one run can both find `silver` missing; the loser's create then
    fails with `AlreadyExistsError` (or the unique index's `IntegrityError`). Either
    means the namespace is there now, so the task carries on.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog service, bound to the run's authorization.
    principal
        The run principal, with its run context.
    environment
        Environment name.
    parts
        The asset's namespace components.

    Raises
    ------
    IntegrityError
        If the insert failed and the namespace still does not exist.
    """
    for depth in range(1, len(parts) + 1):
        path = NamespacePath.from_parts(parts[:depth])
        if namespace_exists(session, environment, path.joined()):
            continue
        try:
            catalog.create_namespace(principal, environment, path)
        except AlreadyExistsError:
            continue
        except IntegrityError:
            session.rollback()
            if not namespace_exists(session, environment, path.joined()):
                raise


def may_read_name(
    authz: AuthorizationService, principal: PrincipalRef, environment: str, dotted: str
) -> bool:
    """Say whether the run principal may read a name, whether or not it exists.

    The question the server asks for a read (`READ_TABLE` on the table-scope
    securable of the name), and it does not depend on the object existing.

    Parameters
    ----------
    authz
        The engine's authorization.
    principal
        The run principal.
    environment
        Environment name.
    dotted
        The input's dotted name, from a manifest (untrusted).

    Returns
    -------
    bool
        `False` for a name the principal may not read and for one that is no valid name.
    """
    try:
        ref = TableRef.from_dotted(environment, dotted)
    except ValidationError:
        return False
    return authz.permits(
        principal, Action.READ_TABLE, Securable.table_scope(environment, ref.namespace, ref.name)
    )


def require_upstreams(
    session: Session,
    run: Run,
    environment: str,
    version: JobVersion,
    asset: str,
    *,
    may_read: Callable[[str], bool],
) -> None:
    """Fail the task if an upstream asset outside the run's selection has never been built.

    An upstream the run principal may not read is not looked up: whether
    it exists must not show, so the check is left to the later read, which answers
    one way for an object that is absent and one that may not be read. There is no
    default for `may_read`: a caller that forgot it would reveal existence.

    Parameters
    ----------
    session
        Database session.
    run
        The run.
    environment
        Environment name.
    version
        The run's project version.
    asset
        The task's asset.
    may_read
        Says whether the run principal may read a name (keyword only, required).

    Raises
    ------
    TaskFailure
        Permanent, naming the first upstream table that does not exist.
    """
    graph = ManifestGraph.from_manifest(version.manifest_json)
    selected = session.scalars(select(TaskRun.asset).where(TaskRun.run_id == run.id)).all()
    for upstream in unselected_upstreams(graph, asset, selected):
        if not may_read(upstream):
            continue
        up = TableRef.from_dotted(environment, upstream)
        if resolve_table_id(session, environment, up.namespace.joined(), up.name) is None:
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"upstream table '{upstream}' has not been built",
                FailureCode.UPSTREAM_MISSING,
            )


def _prepare(session: Session, engine: RunEngine, claimed: ClaimedTask) -> _Prepared:
    """Verify, resolve, bind the attempt's write run, then create the namespaces.

    The write run is inserted and set as the task's `query_run_id` first,
    guarded by the lease owner and claim counter, so everything after it - the
    namespaces included - runs under a run context that only the current
    attempt can hold.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim.

    Returns
    -------
    _Prepared
        The write run to execute.

    Raises
    ------
    TaskFailure
        For every expected failure, with its kind.
    LeaseLost
        If the task is no longer this worker's running attempt.
    LakanaError
        From namespace creation (classified by the caller).
    """
    run = session.get(Run, claimed.run_id)
    env = session.get(Environment, run.environment_id) if run is not None else None
    version = session.get(JobVersion, run.job_version_id) if run is not None else None
    if run is None or env is None or version is None:
        raise TaskFailure(FailureKind.PERMANENT, NO_RUN_MESSAGE, FailureCode.NOT_FOUND)
    check_integrity(session, engine, run, env, version)
    check_manifest_version(version.manifest_json)
    row = session.get(Principal, run.run_as_principal_id) if run.run_as_principal_id else None
    if row is None or not row.is_active:
        raise TaskFailure(
            FailureKind.PERMANENT, NO_PRINCIPAL_MESSAGE, FailureCode.PERMISSION_DENIED
        )
    entry = manifest_entry(version.manifest_json, claimed.asset)
    sql = entry.get("sql") if entry is not None else None
    if entry is None or not isinstance(sql, str) or not sql.strip():
        raise TaskFailure(
            FailureKind.PERMANENT,
            f"the project version has no SQL for {claimed.asset}",
            FailureCode.CODE_CHANGED,
        )
    authz = engine.authz_factory(session)
    reader = PrincipalRef(id=row.id, name=row.name, type=row.type)
    require_upstreams(
        session,
        run,
        env.name,
        version,
        claimed.asset,
        may_read=lambda name: may_read_name(authz, reader, env.name, name),
    )
    ref = TableRef.from_dotted(env.name, claimed.asset)
    found = resolve_table_with_note(session, env.name, ref.namespace.joined(), ref.name)
    exists = found is not None
    kept_table = KeptTable(found[0], found[1]) if found is not None and found[1].noted else None
    mode_name = entry.get("write_mode" if exists else "first_write_mode")
    if not exists and entry.get("materialize") == "incremental" and mode_name == "append":
        # A project version from an older server says `append` for the first run, which could only
        # fail: there is no table to read a watermark from. The first run creates it.
        mode_name = WriteMode.CREATE.value
    try:
        mode = WriteMode(mode_name)
    except ValueError:
        raise TaskFailure(
            FailureKind.PERMANENT,
            f"the project version's write mode {mode_name!r} for {claimed.asset} is not valid",
            FailureCode.CODE_CHANGED,
        ) from None
    written, merge_key, watermark = checked_write(entry, claimed.asset)
    if written == "merge" and mode not in (WriteMode.MERGE, WriteMode.CREATE):
        # `write` and `write_mode` disagree: never run a merge asset as a replace or append.
        raise TaskFailure(
            FailureKind.PERMANENT,
            f"the project version's write mode {mode_name!r} for {claimed.asset} is not valid",
            FailureCode.CODE_CHANGED,
        )
    write = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=row.id,
        sql_text=sql,
        status=QueryRunStatus.RUNNING,
        write_mode=mode,
        target_name=claimed.asset,
        incremental_key=watermark,
        merge_key=list(merge_key) if written == "merge" else None,
        evolve_schema=bool(entry.get("evolve_schema")),
        task_run_id=claimed.task_run_id,
        worker_id=engine.settings.worker_id,
    )
    session.add(write)
    session.flush()
    bound = session.execute(
        update(TaskRun)
        .where(
            TaskRun.id == claimed.task_run_id,
            TaskRun.status == TaskStatus.RUNNING,
            TaskRun.lease_owner == engine.settings.worker_id,
            TaskRun.claim_count == claimed.claim,
        )
        .values(query_run_id=write.id)
        .execution_options(synchronize_session=False)
    )
    if rowcount(bound) != 1:
        session.rollback()
        raise LeaseLost
    write_id, environment = write.id, env.name
    principal = PrincipalRef(
        id=row.id,
        name=row.name,
        type=row.type,
        run_context=RunContext(task_run_id=claimed.task_run_id, query_run_id=write_id),
    )
    session.commit()
    try:
        catalog = CatalogService(
            session, StorageResolver(session, engine.key_provider), engine.authz_factory(session)
        )
        ensure_namespaces(session, catalog, principal, environment, ref.namespace.parts)
    except Exception as exc:
        _close_write(
            session,
            write_id,
            failure_message(exc, str(exc))
            if isinstance(exc, LakanaError)
            else INTERNAL_ERROR_MESSAGE,
        )
        raise
    return _Prepared(
        query_run_id=write_id, mode=mode, environment=environment, kept_table=kept_table
    )


def _finish(
    session: Session, engine: RunEngine, claimed: ClaimedTask, outcome: TaskOutcome
) -> None:
    """Append the attempt's log block and record the attempt, in that order.

    The block is written inside the recording, before its commit (see `record_attempt`), so a
    finished task never has a log that is still to come. A claim that was lost writes none.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim.
    outcome
        How the attempt ended.
    """
    session.rollback()
    in_transaction = outcome.in_transaction

    def store_log_then(recording: Session) -> None:
        """Append the attempt's log block, then do the outcome's own work.

        Called by `record_attempt` once the claim is known to be held and before it commits,
        so a reader that sees the task finished finds the block in its log. Writing the log
        never fails the recording (`write_task_log` only logs a warning).

        Parameters
        ----------
        recording
            The session of `record_attempt`.
        """
        if outcome.environment is not None:
            write_task_log(
                recording,
                engine.key_provider,
                outcome.environment,
                claimed.run_id,
                claimed.asset,
                _log_block(recording, engine, claimed, outcome),
            )
        if in_transaction is not None:
            in_transaction(recording)

    record_attempt(
        session,
        claimed.task_run_id,
        engine.settings.worker_id,
        claimed.claim,
        kind=outcome.kind,
        rand=engine.rand,
        error_message=outcome.message,
        failure_code=outcome.failure_code,
        rows_written=outcome.rows,
        on_committed=COMMITTED_BEFORE_RECORDED_MESSAGE,
        in_transaction=store_log_then,
    )


def _log_block(
    session: Session, engine: RunEngine, claimed: ClaimedTask, outcome: TaskOutcome
) -> str:
    """Build the text one attempt appends to its task's log.

    Parameters
    ----------
    session
        Database session (for the database's time).
    engine
        The run engine.
    claimed
        The claim.
    outcome
        How the attempt ended.

    Returns
    -------
    str
        The block, ending in a line break.
    """
    mode = outcome.mode
    lines = [
        f"attempt {claimed.attempt} on {engine.settings.worker_id} at "
        f"{db_now(session).isoformat()}: "
        f"{'succeeded' if outcome.kind is None else outcome.kind.value}",
        f"  write mode: {mode.value if mode is not None else '-'}",
        f"  watermark: {outcome.watermark or '-'}",
        f"  rows: {outcome.rows if outcome.rows is not None else '-'}",
    ]
    if outcome.snapshot_id is not None:
        lines.append(f"  snapshot: {outcome.snapshot_id}")
    if outcome.artifact_version is not None:
        lines.append(f"  artifact version: {outcome.artifact_version}")
    lines.extend(outcome.checks)
    if outcome.message:
        lines.append(f"  error: {outcome.message}")
    if outcome.output:
        lines.append("  output:")
        lines.extend(f"    {line}" for line in outcome.output.splitlines())
    return "\n".join(lines) + "\n"


def _execute(
    session: Session,
    engine: RunEngine,
    claimed: ClaimedTask,
    prepared: _Prepared,
    keeper: LeaseKeeper,
) -> TaskOutcome | None:
    """Run the prepared write under the lease keeper and classify the result.

    A write that completed counts as succeeded whatever the keeper saw;
    otherwise a cancel or release (seen by the keeper or found on the run)
    wins over the failure's type, and only the stored redacted message is used.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim.
    prepared
        The prepared write run.
    keeper
        The running lease keeper.

    Returns
    -------
    TaskOutcome or None
        The outcome, or `None` when the lease was lost (nothing to record).
    """
    settings = engine.settings
    limits = settings.limits.model_copy(
        update={
            "timeout_seconds": min(float(claimed.timeout_seconds), settings.limits.timeout_seconds)
        }
    )
    failure: BaseException | None = None
    crashed = False
    try:
        with cancellation(keeper.cancel):
            failure = run_one(
                session,
                prepared.query_run_id,
                engine.authz_factory(session),
                engine.key_provider,
                limits=limits,
                worker_id=settings.worker_id,
            )
    except Exception:
        logger.exception(
            "Running task run %s failed unexpectedly.",
            claimed.task_run_id,
            extra=log_event(LogEvent.INTERNAL_ERROR),
        )
        crashed = True
        _close_write(session, prepared.query_run_id, INTERNAL_ERROR_MESSAGE)
    finally:
        keeper.stop()
    if keeper.lost:
        return None
    write = session.execute(
        select(QueryRun)
        .where(QueryRun.id == prepared.query_run_id)
        .execution_options(populate_existing=True)
    ).scalar_one()
    succeeded = write.status is QueryRunStatus.SUCCEEDED
    stored, watermark, rows = write.error_message, write.watermark, write.rows_written
    kind: FailureKind | None
    message: str | None
    code: FailureCode | None = None
    if succeeded:
        kind, message = None, None
    elif (stop := cancel_kind(session, keeper, claimed.run_id)) is not None:
        kind, message, code = stop[0], stop[1] or stored, stop_code(stop[0])
    elif crashed:
        kind, message, code = FailureKind.TRANSIENT, INTERNAL_ERROR_MESSAGE, FailureCode.INTERNAL
    else:
        kind, message = classify(failure), failure_message(failure, stored)
        code = failure_code_of(failure, write.error_cause)
    return TaskOutcome(
        kind,
        message,
        prepared.environment,
        prepared.mode,
        watermark,
        rows,
        kept_table=prepared.kept_table if succeeded else None,
        built_snapshot_id=write.snapshot_id if succeeded and prepared.kept_table else None,
        failure_code=code,
        wrote_nothing=succeeded and write.snapshot_id is None and not rows,
    )


def _attempt(
    session: Session,
    engine: RunEngine,
    claimed: ClaimedTask,
    keeper: LeaseKeeper,
    environment: str | None,
) -> TaskOutcome | None:
    """Prepare and execute one claimed attempt; never raises for the task's own failure.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim.
    keeper
        The running lease keeper.
    environment
        Environment name for the log, or `None` when the run is gone.

    Returns
    -------
    TaskOutcome or None
        What to record, or `None` when this attempt is no longer ours.
    """
    try:
        prepared = _prepare(session, engine, claimed)
    except LeaseLost:
        return None
    except TaskFailure as failure:
        return TaskOutcome(
            failure.kind,
            failure.message,
            environment,
            in_transaction=failure.in_transaction,
            failure_code=failure.code,
        )
    except LakanaError as exc:
        stop = cancel_kind(session, keeper, claimed.run_id)
        if stop is not None:
            return TaskOutcome(stop[0], stop[1], environment, failure_code=stop_code(stop[0]))
        return TaskOutcome(
            classify(exc),
            failure_message(exc, str(exc)),
            environment,
            failure_code=failure_code_of(exc),
        )
    except Exception:
        logger.exception(
            "Preparing task run %s failed unexpectedly.",
            claimed.task_run_id,
            extra=log_event(LogEvent.INTERNAL_ERROR),
        )
        return TaskOutcome(
            FailureKind.TRANSIENT,
            INTERNAL_ERROR_MESSAGE,
            environment,
            failure_code=FailureCode.INTERNAL,
        )
    return _execute(session, engine, claimed, prepared, keeper)


def _manifest_of(session: Session, claimed: ClaimedTask) -> object:
    """Read the manifest of a claimed task's run, once per dispatch.

    Parameters
    ----------
    session
        Database session.
    claimed
        The claim.

    Returns
    -------
    object
        The project version's manifest, or `None` when the run or its project version is gone.
    """
    return session.execute(
        select(JobVersion.manifest_json)
        .join(Run, Run.job_version_id == JobVersion.id)
        .where(Run.id == claimed.run_id)
    ).scalar_one_or_none()


def _is_python(manifest: object, claimed: ClaimedTask) -> bool:
    """Say whether a claimed task's asset is a Python asset.

    Parameters
    ----------
    manifest
        The run's manifest, from `_manifest_of`.
    claimed
        The claim.

    Returns
    -------
    bool
        Whether the manifest declares the asset with ``kind: python``;
        `False` when the run, its project version or the entry is gone (the SQL
        path then reports that), and for an entry deployed before Python assets existed,
        which has no `kind`.
    """
    entry = manifest_entry(manifest, claimed.asset) if isinstance(manifest, dict) else None
    return entry is not None and asset_kind(entry) == "python"


_OLDER_SERVER = (
    "This run was started by a server that does not know checks, but the project has checks "
    "for {tables}. Upgrade the server to the version of the workers, then run again."
)
"""The message of a task that an older server created for a project with checks."""


def _older_server_refusal(
    session: Session, claimed: ClaimedTask, environment: str | None, manifest: object
) -> TaskOutcome | None:
    """Refuse a task that says it has no checks while its project version lists some.

    A server older than the worker neither sets `has_checks` nor creates the `source_check`
    tasks, so a run it starts for a project with checks would build unchecked. The worker
    reads the same manifest the server would have, and fails the task before anything is
    written. Both gaps are certain: a table that has checks and that the asset reads, without a
    `source_check` task in this run, can only come from a server that did not create one.

    Parameters
    ----------
    session
        Database session.
    claimed
        The claim, of a task whose `has_checks` is false.
    environment
        Environment name for the log, or `None` when the run is gone.
    manifest
        The run's manifest, from `_manifest_of`.

    Returns
    -------
    TaskOutcome or None
        A permanent failure naming the tables, or `None` when the task has no checks it
        should have had, or when the run or its manifest cannot be read (the build then
        reports that, as before). A manifest with no check at all returns before the run's tasks
        are read: that is every task of a project without checks.
    """
    read = read_manifest_checks(manifest)
    if not (read.specs or read.unreadable_tables or read.unplaced):
        return None
    tables = []
    if read.of(claimed.asset) or read.unreadable_for(claimed.asset):
        tables.append(f"`{shown(claimed.asset)}`")
    tasks = session.execute(
        select(TaskRun.asset, TaskRun.kind).where(TaskRun.run_id == claimed.run_id)
    ).all()
    sources = source_check_tables(manifest, [asset for asset, kind in tasks if kind == ASSET])
    have = {asset for asset, kind in tasks if kind == SOURCE_CHECK}
    tables.extend(
        f"`{shown(table)}`, which `{shown(claimed.asset)}` reads"
        for table, readers in sources.items()
        if claimed.asset in readers and table not in have
    )
    if not tables:
        return None
    return TaskOutcome(
        FailureKind.PERMANENT,
        _OLDER_SERVER.format(tables=" and ".join(tables)),
        environment,
        failure_code=FailureCode.NO_RUNTIME,
    )


def _dispatch(
    session: Session,
    engine: RunEngine,
    claimed: ClaimedTask,
    keeper: LeaseKeeper,
    environment: str | None,
    serve_queries: Callable[[], bool] | None,
) -> TaskOutcome | None:
    """Run one attempt by its asset's kind; never raises for the task's own failure.

    Parameters
    ----------
    session
        `run_claimed`'s session; rolled back before a Python attempt, which
        uses its own.
    engine
        The run engine.
    claimed
        The claim.
    keeper
        The running lease keeper.
    environment
        Environment name for the log, or `None` when the run is gone.
    serve_queries
        Runs one query-only poll iteration.

    Returns
    -------
    TaskOutcome or None
        What to record, or `None` when the attempt is no longer ours. An
        error while finding the kind or starting a Python attempt (the
        supervisor thread could not be started, say) is a generic transient
        failure, as an unexpected error on the SQL path is.
    """
    try:
        manifest = _manifest_of(session, claimed)
        if not claimed.has_checks:
            refusal = _older_server_refusal(session, claimed, environment, manifest)
            if refusal is not None:
                return refusal
        if not _is_python(manifest, claimed):
            return _attempt(session, engine, claimed, keeper, environment)
        session.rollback()
        if engine.python is None:
            return TaskOutcome(
                FailureKind.PERMANENT, _NO_RUNTIME, environment, failure_code=FailureCode.NO_RUNTIME
            )
        return engine.python.attempt(engine, claimed, keeper, environment, serve_queries)
    except Exception:
        logger.exception(
            "Running task run %s failed unexpectedly.",
            claimed.task_run_id,
            extra=log_event(LogEvent.INTERNAL_ERROR),
        )
        return TaskOutcome(
            FailureKind.TRANSIENT,
            RUN_ERROR_MESSAGE,
            environment,
            failure_code=FailureCode.INTERNAL,
        )


def run_claimed(
    engine: RunEngine, claimed: ClaimedTask, serve_queries: Callable[[], bool] | None = None
) -> None:
    """Execute one claimed task run, marked as in flight for the memory watchdog.

    Parameters
    ----------
    engine
        The run engine.
    claimed
        The claim.
    serve_queries
        Passed to `_run_claimed`.
    """
    task = TaskClaim(claimed.task_run_id, engine.settings.worker_id, claimed.claim)
    with ACTIVITY.task(task):
        _run_claimed(engine, claimed, serve_queries)


def _clear_rebuilt(
    session: Session,
    engine: RunEngine,
    claimed: ClaimedTask,
    kept: KeptTable,
    snapshot_id: int | None,
) -> None:
    """Clear the note of a table that a task without checks has just built.

    The project has no check left for the table, so nothing could fail the data that this
    build put in place. The clear is not atomic with the write, so a note survives a
    successful rebuild without checks in four cases: the clear raises; the claim is lost
    (`clear_kept` then writes nothing); the worker dies after the write's commit and before
    this call; or a write that landed is reported failed and the task ends so. The build is
    not failed for it: a retry would write an increment twice. The note stays, and the next
    build of the table clears it. A failure here is logged.

    Parameters
    ----------
    session
        Database session.
    engine
        The run engine.
    claimed
        The claim.
    kept
        The table the task built and the note it carried before the build: only that note is
        cleared, not one another run set since.
    snapshot_id
        The snapshot the build made; the data of a table without checks counts as having
        passed.
    """
    try:
        clear_kept(
            session,
            task_run_id=claimed.task_run_id,
            worker=engine.settings.worker_id,
            claim=claimed.claim,
            table_id=kept.table_id,
            why="rebuilt_without_checks",
            snapshot_id=snapshot_id,
            seen=kept.seen,
        )
    except Exception:
        logger.exception(
            "Clearing the note of the table that task run %s built failed; it stays.",
            claimed.task_run_id,
        )
        session.rollback()


def _run_claimed(
    engine: RunEngine, claimed: ClaimedTask, serve_queries: Callable[[], bool] | None = None
) -> None:
    """Execute one claimed task run to its recorded outcome; never raises for it.

    The lease keeper starts first, so reading and hashing a large tarball cannot
    outlive the lease, and is stopped (joined) before the outcome is recorded.
    A Python attempt gets its own session in its own thread: this function's
    session is rolled back first, so no transaction stays open across the run,
    and is used again only to record the outcome.

    A task with checks, and a ``source_check`` task, then run the check phase under a
    second keeper, which is stopped before the outcome is recorded as well. Such a task is
    built only when `before_build` finds something to build; a build that succeeded goes to
    the phase, and so does one that failed without being stopped: its write may be in the
    table all the same (a SQL write that landed before the failure was reported; a Python
    process that committed and then failed).

    A claim that only sets the table back (`ClaimedTask.setting_back`) goes to `set_back_only`
    before anything else: no build, no check, no process and no token.

    Parameters
    ----------
    engine
        The run engine.
    claimed
        The claim.
    serve_queries
        Runs one query-only poll iteration; a Python task's own input queries
        are served through it while its process runs.
    """
    keeper = keeper_for(engine, claimed)
    keeper.start()
    try:
        with engine.session_factory() as session:
            environment = session.execute(
                select(Environment.name)
                .join(Run, Run.environment_id == Environment.id)
                .where(Run.id == claimed.run_id)
            ).scalar_one_or_none()
            checked = claimed.has_checks or claimed.kind == SOURCE_CHECK
            try:
                if claimed.setting_back:
                    # The task's worker was lost after it may have written its table: nothing
                    # is built, checked or started; only the table is set back.
                    session.rollback()  # no transaction stays open across the catalog call
                    outcome = set_back_only(engine, claimed, keeper)
                    build = False
                else:
                    outcome = (
                        before_build(session, engine, claimed, environment) if checked else None
                    )
                    build = outcome is None
                if build:
                    outcome = _dispatch(
                        session, engine, claimed, keeper, environment, serve_queries
                    )
            finally:
                keeper.stop()
            if outcome is None or keeper.lost:
                logger.warning(
                    "Task run %s is no longer ours; its outcome is dropped.",
                    claimed.task_run_id,
                    extra=log_event(LogEvent.LEASE_LOST),
                )
                return
            if ACTIVITY.stopping:
                # The watchdog is stopping the worker and records the outcome itself. It has no
                # `in_transaction` to run, so a code integrity failure that meets a stop between
                # the check and this line fails only the task; the next attempt checks again.
                return
            if claimed.setting_back:
                _finish(session, engine, claimed, outcome)
                return
            if checked and (
                outcome.kind is None
                or (build and outcome.kind not in {FailureKind.CANCELLED, FailureKind.RELEASED})
            ):
                outcome = check_phase(session, engine, claimed, environment, outcome)
                if outcome is None:
                    logger.warning(
                        "Task run %s is no longer ours; the outcome of its checks is dropped.",
                        claimed.task_run_id,
                        extra=log_event(LogEvent.LEASE_LOST),
                    )
                    return
                if ACTIVITY.stopping:
                    return
            elif outcome.kind is None and outcome.kept_table is not None:
                _clear_rebuilt(
                    session,
                    engine,
                    claimed,
                    outcome.kept_table,
                    outcome.built_snapshot_id
                    if outcome.built_snapshot_id is not None
                    else outcome.snapshot_id,
                )
            _finish(session, engine, claimed, outcome)
    finally:
        keeper.stop()
