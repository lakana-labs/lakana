"""`RunService`: start, read, cancel and resume runs; task logs; scheduler status.

The API side only: the server creates runs and reads their state, the worker
executes them (rule 2). Every mutation commits with its audit event.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NamedTuple, cast

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.db import db_now
from lakana_core.errors import (
    ConflictError,
    LakanaError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AttemptOutcome,
    AuditEventType,
    CheckResult,
    DeployRun,
    DeployStatus,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    NotificationDelivery,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    Run,
    RunKind,
    RunStatus,
    RunTrigger,
    SchedulerLease,
    TaskAttempt,
    TaskRun,
    TaskStatus,
    Worker,
)
from lakana_core.models.run import (
    ACTIVE_RUN_STATUSES,
    SCHEDULER_LEASE_ID,
    TERMINAL_TASK_STATUSES,
)
from lakana_core.notifications.views import NotificationView, notification_view
from lakana_core.pagination import decode_cursor, encode_cursor, parse_choices
from lakana_core.projects.checks import (
    kept_check_tables,
    may_read_every,
    read_manifest_checks,
    source_check_tables,
    view_readers,
)
from lakana_core.projects.deploys import DeployService
from lakana_core.projects.lookups import deployment_row, environment_by_name, repository_by_name
from lakana_core.projects.manifest import job_on_deploy, manifest_assets, manifest_jobs
from lakana_core.projects.run_policy import RunCaps, policy_from_manifest
from lakana_core.runs.check_message import message_for_reader
from lakana_core.runs.checks import (
    ASSET,
    SOURCE_CHECK,
    CheckResultView,
    view_of,
)
from lakana_core.runs.failures import Failure, attempt_failure, task_failure
from lakana_core.runs.hidden_checks import ReferenceCheckFilter
from lakana_core.runs.kept import kept_tables
from lakana_core.runs.selection import (
    ManifestGraph,
    Selection,
    downstream_closure,
    resolve_selection,
    selected_upstreams,
)
from lakana_core.runs.store import upstream_names
from lakana_core.runs.waiting import (
    WaitingTask,
    WaitReason,
    alive_workers,
    attempts_used,
    task_wait_reason,
    with_work,
)
from lakana_core.runs.workers import CacheSizes, alive_ids, cache_sizes
from lakana_core.secrets.keys import KeyProvider
from lakana_core.storage.resolver import StorageResolver

NO_RUN_PRINCIPAL_MESSAGE = (
    "No run principal is bound to {repository} in protected environment {environment!r}; an "
    "admin can set one with `lakana deployments set-run-as`."
)
_INACTIVE_MESSAGE = (
    "The run principal {name!r} is deactivated or deleted; an admin can bind another with "
    "`lakana deployments set-run-as`."
)
_NOT_SERVICE_ACCOUNT_MESSAGE = (
    "The run principal {name!r} is not a service account; an admin can bind one with "
    "`lakana deployments set-run-as`."
)
_RESUMABLE = frozenset({TaskStatus.FAILED, TaskStatus.UPSTREAM_FAILED, TaskStatus.CANCELLED})


class ActiveRunError(ConflictError):
    """Another run of the repository is active in the environment.

    A subclass of `ConflictError`, so the REST API answers 409 as it always did. A start of a job
    catches it to wait for that run instead of being refused.

    Parameters
    ----------
    message
        The sentence for the caller.
    run_id
        The active run, or `None` when it ended before it could be read.
    """

    def __init__(self, message: str, run_id: uuid.UUID | None) -> None:
        super().__init__(message)
        self.run_id = run_id


class _RunAs(NamedTuple):
    """The run principal choice and the state it was made from.

    Parameters
    ----------
    principal
        Who the run writes as.
    bound_id
        The deployment's binding when chosen (`None` when the caller was used).
    protected
        The environment's protection when chosen.
    """

    principal: Principal
    bound_id: uuid.UUID | None
    protected: bool


LOG_CHUNK_BYTES = 1024 * 1024
"""The most bytes of a task's log one request with an offset returns."""


def _char_start(data: bytes, index: int) -> int:
    """Move `index` back to the first byte of the UTF-8 character it falls in.

    Parameters
    ----------
    data
        The log's bytes.
    index
        A byte position.

    Returns
    -------
    int
        `index`, or the position of the lead byte when `index` is inside a character.
    """
    while 0 < index < len(data) and data[index] & 0xC0 == 0x80:
        index -= 1
    return index


def log_window(data: bytes, offset: int | None) -> tuple[str, int, int]:
    """Cut the part of a stored log a request reads, never inside a character.

    Parameters
    ----------
    data
        The whole stored log.
    offset
        The byte to start from, or `None` for the whole log.

    Returns
    -------
    tuple of str, int and int
        The text, the byte it starts at (before `offset` only when `offset` fell inside a
        character) and the byte after it. Stored logs are valid UTF-8; if one is not, the bad
        bytes become U+FFFD.
    """
    start = 0 if offset is None else _char_start(data, min(offset, len(data)))
    end = (
        len(data) if offset is None else _char_start(data, min(start + LOG_CHUNK_BYTES, len(data)))
    )
    return data[start:end].decode("utf-8", errors="replace"), start, end


def task_log_path(run_id: uuid.UUID, asset: str) -> str:
    """Return the environment-relative path of a task's free-text log.

    Parameters
    ----------
    run_id
        The run.
    asset
        The task's dotted table name; validated as ``namespace.table`` identifiers.

    Returns
    -------
    str
        ``_artifacts/runs/<run_id>/<asset>.log``.

    Raises
    ------
    ValidationError
        If `asset` is not a dotted identifier (manifests read back are untrusted).
    """
    TableRef.from_dotted("-", asset)
    return f"_artifacts/runs/{run_id}/{asset}.log"


@dataclass(frozen=True)
class TaskAttemptView:
    """One attempt, as the API shows it.

    Parameters
    ----------
    attempt
        Attempt number.
    worker
        The worker that ran it.
    started_at
        Start.
    finished_at
        End, or `None` while running.
    outcome
        ``succeeded``, ``failed``, ``timed_out``, ``lost``, ``released``,
        ``cancelled``, or `None` while running.
    rows_written
        Rows written, for a successful write.
    error_message
        Why it failed, redacted.
    failure
        The code, cause and next step of a failed attempt; ``unknown`` for one recorded before
        the codes existed; `None` for an attempt that did not fail.
    """

    attempt: int
    worker: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    rows_written: int | None
    error_message: str | None
    failure: Failure | None = None


@dataclass(frozen=True)
class TaskRunView:
    """One task run, as the API shows it.

    Parameters
    ----------
    asset
        Dotted table name.
    status
        Task status.
    attempt
        Attempts used, never above `max_attempts`: a claim that only runs the checks of a task
        whose write is in the table is not a new attempt, though it counts in the stored number.
    max_attempts
        Retries + 1.
    timeout_seconds
        Per-attempt limit.
    next_attempt_at
        When a pending retry may start.
    started_at
        First claim.
    finished_at
        Final state reached.
    error_message
        Last failure: the free text, the detail of `failure`.
    query_run_id
        The M5a write that executed the last attempt.
    rows_written
        Rows that write wrote.
    waiting_for
        Why a `pending` or `ready` task of a running run has not started; `None` for any other
        task, and for every task of a run that is not running (queued, cancelled or ended).
    kind
        ``asset`` for a table the run builds, ``source_check`` for a table it only checks.
    checks
        The results of the task's checks, by name; empty when it has none (and in listings).
        A generated reference check whose target the reader may not read is left out.
    hidden_checks
        How many results were left out of `checks` for that reason.
    kept_check
        Whether this ``source_check`` task checks a table the project builds, because that table
        was kept after a failed check, rather than a table the project only reads;
        `None` when the run's project version is gone, so that nothing can tell which.
    setting_back
        Whether the task's worker was lost after it may have written its table (or its run was
        cancelled while it waited with rows no check had passed), so that its next claim only
        sets the table back. `False` once the task has ended, although the stored flag stays.
    failure
        Why the task failed, was cancelled or never ran: the code with its cause and next step.
        `None` for a task with no error; ``unknown`` for a task that failed before the codes
        existed.
    """

    asset: str
    status: str
    attempt: int
    max_attempts: int
    timeout_seconds: int
    next_attempt_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    query_run_id: uuid.UUID | None
    rows_written: int | None
    waiting_for: WaitReason | None = None
    kind: str = ASSET
    checks: tuple[CheckResultView, ...] = ()
    hidden_checks: int = 0
    kept_check: bool | None = False
    setting_back: bool = False
    failure: Failure | None = None


@dataclass(frozen=True)
class RunView:
    """One run, as the API shows it.

    Parameters
    ----------
    id
        Run id.
    environment
        Environment name.
    repository
        Repository name.
    job_version_id
        The project version it runs.
    commit_sha
        Its commit.
    kind
        ``full``, ``selection`` or ``resume``.
    status
        Run status.
    cancel_requested
        Whether a cancel was requested.
    triggered_by
        Who started it, if that principal still exists.
    run_as
        Who it writes as, if that principal still exists.
    resumed_from_run_id
        The run a resume continues.
    selection
        The stored selection.
    error_message
        A run-level failure (code integrity).
    created_at
        Created.
    started_at
        First tick.
    finished_at
        Finalized.
    tasks
        Task runs sorted by asset; empty in listings.
    job
        The job it runs; `None` for an ad-hoc run.
    trigger
        ``schedule``, ``api`` or ``manual``; `None` for an ad-hoc run.
    scheduled_for
        For a scheduled run, the time the schedule named.
    missed
        How many earlier scheduled times this run stands in for.
    notifications
        The notifications the run queued, oldest first; empty in listings.
    failed_tasks
        How many of its tasks ended ``failed`` (not ``upstream_failed`` or ``cancelled``), also
        in listings.
    """

    id: uuid.UUID
    environment: str
    repository: str
    job_version_id: uuid.UUID
    commit_sha: str
    kind: str
    status: str
    cancel_requested: bool
    triggered_by: str | None
    run_as: str | None
    resumed_from_run_id: uuid.UUID | None
    selection: dict
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    tasks: tuple[TaskRunView, ...]
    job: str | None = None
    trigger: str | None = None
    scheduled_for: datetime | None = None
    missed: int = 0
    notifications: tuple[NotificationView, ...] = ()
    failed_tasks: int = 0


class PreparedStart(NamedTuple):
    """What a start has decided before it takes the deployment's lock.

    Parameters
    ----------
    env
        The environment.
    repo
        The repository.
    version
        The project version to run.
    choice
        Who the run writes as.
    selection
        What to run.
    assets
        The selected assets.
    actor
        Who the run and its audit event are attributed to.
    """

    env: Environment
    repo: Repository
    version: JobVersion
    choice: _RunAs
    selection: Selection
    assets: Sequence[str]
    actor: PrincipalRef


@dataclass(frozen=True)
class TaskLogView:
    """A task's logs.

    Parameters
    ----------
    run_id
        The run.
    asset
        The task.
    text
        The free-text log, empty when none was written (or storage failed).
    attempts
        The structured log, oldest first.
    next_offset
        The byte to continue from: where the text ends in the stored log.
    more
        Whether stored bytes remain after `next_offset` (the chunk limit cut the text).
    complete
        Whether the task has finished and the text reaches the end of its log.
    reset
        Whether the request's offset lay beyond the end of the log, which only happens when
        the log is shorter than before (it was removed); the client starts again from
        `next_offset`.
    """

    run_id: uuid.UUID
    asset: str
    text: str
    attempts: tuple[TaskAttemptView, ...]
    next_offset: int = 0
    more: bool = False
    complete: bool = False
    reset: bool = False


@dataclass(frozen=True)
class SchedulerView:
    """The scheduler role's holder and the queue.

    Parameters
    ----------
    holder
        The worker holding the role, or `None`.
    heartbeat_at
        Its last heartbeat.
    active_runs
        Queued and running runs.
    pending
        Pending task runs of active runs.
    ready
        Ready task runs.
    running
        Running task runs.
    """

    holder: str | None
    heartbeat_at: datetime | None
    active_runs: int
    pending: int
    ready: int
    running: int


@dataclass(frozen=True)
class WorkerView:
    """A registered worker, and what it is doing.

    What it is doing is derived from the runs that carry its id, not stored.

    Parameters
    ----------
    id
        The worker's id.
    host
        Its host name.
    version
        The Lakana version it runs.
    started_at
        When it registered.
    heartbeat_at
        Its last heartbeat (the database's time).
    status
        `alive` (the heartbeat is younger than the lease) or `lost`.
    takes
        The work it takes: `all`, `interactive` or `pipeline`.
    query_run_id
        Its oldest `RUNNING` query run, or `None`.
    deploy_run_id
        Its oldest `RUNNING` deploy run, or `None`.
    task_run_id
        The `RUNNING` task run whose lease it holds, or `None`.
    caches
        The sizes of its caches it last measured; `None` before its first measurement.
    caches_measured_at
        When it stored them (the database's time); `None` with `caches`.
    """

    id: str
    host: str
    version: str
    started_at: datetime
    heartbeat_at: datetime
    status: str
    takes: str
    query_run_id: uuid.UUID | None
    deploy_run_id: uuid.UUID | None
    task_run_id: uuid.UUID | None
    caches: CacheSizes | None = None
    caches_measured_at: datetime | None = None


def _entries(manifest: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    """Index a manifest's asset entries by table, skipping malformed ones.

    Parameters
    ----------
    manifest
        A project version's manifest.

    Returns
    -------
    dict
        Table → entry.
    """
    raw = manifest.get("assets")
    return {
        entry["table"]: entry
        for entry in (raw if isinstance(raw, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("table"), str)
    }


class NotOnDeploy(PermissionDenied):
    """A start a CI trust rule allows names a job that is not ``on_deploy`` in the version run.

    The rule's opt-in (`start_on_deploy_jobs`) covers only the deployed project's jobs with
    ``on_deploy: true``; a deploy by hand that moved the pointer meanwhile may have changed that.
    """


def _require_on_deploy(p: PrincipalRef | None, version: JobVersion, job: str, env: str) -> None:
    """Refuse a rule-allowed CI start of a job that is not ``on_deploy`` in the version it runs.

    Parameters
    ----------
    p
        The caller.
    version
        The project version the start would run.
    job
        The job.
    env
        The environment's name, for the message.

    Raises
    ------
    NotOnDeploy
        When the caller's CI scope starts the deploy's jobs and the job lacks ``on_deploy: true``
        in `version`.
    """
    scope = p.ci_scope if p is not None else None
    if scope is None or not scope.starting_deploy_jobs:
        return
    if not job_on_deploy(manifest_jobs(version.manifest_json).get(job, {})):
        raise NotOnDeploy(
            f"it no longer has `on_deploy: true` in the project version now current in `{env}`, "
            "and the CI trust rule allows only those"
        )


def _ci_rule_detail(p: PrincipalRef) -> dict[str, str]:
    """Name the CI trust rule that allowed a start, for its audit event.

    Parameters
    ----------
    p
        The principal that started the run.

    Returns
    -------
    dict
        ``ci_rule_id`` and ``allowed_by: start_on_deploy_jobs`` when the start is one of a CI
        deploy's ``on_deploy`` jobs, allowed by its rule's opt-in; empty otherwise.
    """
    scope = p.ci_scope
    if scope is None or not scope.starting_deploy_jobs:
        return {}
    return {"ci_rule_id": str(scope.rule_id), "allowed_by": "start_on_deploy_jobs"}


def trigger_of(p: PrincipalRef) -> RunTrigger:
    """Say what a start by this caller counts as.

    Parameters
    ----------
    p
        The caller.

    Returns
    -------
    RunTrigger
        ``api`` for a service account (a script or another system calling the API), ``manual``
        for anyone else.
    """
    return RunTrigger.API if p.type is PrincipalType.SERVICE_ACCOUNT else RunTrigger.MANUAL


def job_missing_message(manifest: Mapping[str, object], job: str, repo: str, env: str) -> str:
    """Say that a job is not declared, and which jobs are.

    Parameters
    ----------
    manifest
        The manifest of the repository's current project version.
    job
        The name asked for.
    repo
        The repository's name.
    env
        The environment's name.

    Returns
    -------
    str
        A sentence naming the cause and the jobs that exist, or how to declare one.
    """
    names = sorted(manifest_jobs(manifest))
    if not names:
        return (
            f"{repo!r} declares no job in {env!r}. Add a `jobs:` section to lakana.yaml and "
            "deploy it."
        )
    return f"No job {job!r} in {repo!r} in {env!r}. Its jobs: {', '.join(names)}."


def authorize_run_read(
    session: Session, authz: AuthorizationService, p: PrincipalRef, environment: str
) -> Environment:
    """Authorize reading one run of an environment: RUN or READ_ENVIRONMENT.

    One definition for everything that shows a run or something of it: `RunService`, and the
    query that shows the rows a check of a run found (`lakana_core.runs.check_rows`).

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization service.
    p
        The acting principal.
    environment
        Environment name.

    Returns
    -------
    Environment
        The row.

    Raises
    ------
    PermissionDenied
        With neither action (READ_ENVIRONMENT's denial is the one audited).
    NotFoundError
        If the environment does not exist.
    """
    scope = Securable.environment_scope(environment)
    if authz.permits(p, Action.RUN, scope):
        authz.authorize(p, Action.RUN, scope)
    else:
        authz.authorize(p, Action.READ_ENVIRONMENT, scope)
    return environment_by_name(session, environment)


class RunService:
    """Runs of deployed project versions.

    Parameters
    ----------
    session
        Database session; mutating methods commit.
    authz
        Authorization service.
    key_provider
        Decrypts a secret-backed environment's storage credentials (task logs).
    caps
        Operator maximums applied to each task's policy; `RunCaps()` when `None`.
    lease_seconds
        `LAKANA_RUN__LEASE_SECONDS`: a worker whose heartbeat is older is not alive, which is
        how the wait reasons of waiting tasks count workers. The server passes the configured
        value; the default is the setting's own.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        *,
        key_provider: KeyProvider | None = None,
        caps: RunCaps | None = None,
        lease_seconds: int = 60,
    ) -> None:
        self._session = session
        self._authz = authz
        self._key_provider = key_provider
        self._caps = caps or RunCaps()
        self._lease_seconds = lease_seconds

    # -- lookups -----------------------------------------------------------

    def _environment(self, name: str) -> Environment:
        """Look an environment up by name (shared with `DeployService`).

        Parameters
        ----------
        name
            Environment name.

        Returns
        -------
        Environment
            The row.
        """
        return environment_by_name(self._session, name)

    def _repository(self, name: str) -> Repository:
        """Look a repository up by name (shared with `DeployService`).

        Parameters
        ----------
        name
            Repository name.

        Returns
        -------
        Repository
            The row.
        """
        return repository_by_name(self._session, name)

    def _run(self, env: Environment, run_id: uuid.UUID) -> Run:
        """Find a run of an environment.

        Parameters
        ----------
        env
            The environment.
        run_id
            The run.

        Returns
        -------
        Run
            The row, freshly read.

        Raises
        ------
        NotFoundError
            If no such run exists in this environment.
        """
        run = self._session.execute(
            select(Run)
            .where(Run.id == run_id, Run.environment_id == env.id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if run is None:
            raise NotFoundError(f"No run {run_id} in {env.name}.")
        return run

    def _authorize_read(self, p: PrincipalRef, environment: str) -> Environment:
        """Authorize reading one run: RUN or READ_ENVIRONMENT.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.

        Returns
        -------
        Environment
            The row.

        Raises
        ------
        PermissionDenied
            With neither action (READ_ENVIRONMENT's denial is the one audited).
        NotFoundError
            If the environment does not exist.
        """
        return authorize_run_read(self._session, self._authz, p, environment)

    def _name(self, principal_id: uuid.UUID | None) -> str | None:
        """Return a principal's name.

        Parameters
        ----------
        principal_id
            The principal, or `None`.

        Returns
        -------
        str or None
            Its name, or `None` if unset or deleted.
        """
        if principal_id is None:
            return None
        row = self._session.get(Principal, principal_id)
        return row.name if row is not None else None

    def _waiting(
        self, run: Run, tasks: Sequence[TaskRun], reader: PrincipalRef, environment: str
    ) -> dict[uuid.UUID, WaitReason]:
        """Say why each waiting task of a run has not started.

        Only a running run that has not been asked to stop gets reasons: the tasks of a queued
        run wait for the scheduler to start it, and those of a cancelled run are about to be
        cancelled, so a reason for either would be a guess. The one exception is a ready task
        that is setting its table back: its set-back claim is taken also in a run asked to
        stop, so it is told what it waits for. A pending task with an upstream that
        failed, was cancelled or is `upstream_failed` gets none either: the scheduler is about
        to mark it. The workers are read once for the
        whole run (at most five statements), and only when a task is `pending` or `ready`; the
        tasks of the run are already loaded, and no other statement depends on their number.

        An upstream task is named only when `reader` may read its table, asked by name before
        anything else, so a missing table and an unreadable one look the same; the others are
        counted (`WaitReason.hidden_upstream`), as the project-version view drops their edges.

        Parameters
        ----------
        run
            The run.
        tasks
            Its task runs.
        reader
            Who reads the run.
        environment
            The run's environment name.

        Returns
        -------
        dict
            A reason per waiting task's id; a task that does not wait has no entry.
        """
        waiting = [t for t in tasks if t.status in (TaskStatus.PENDING, TaskStatus.READY)]
        if run.status != RunStatus.RUNNING:
            return {}
        if run.cancel_requested:
            waiting = [t for t in waiting if t.setting_back and t.status == TaskStatus.READY]
        if not waiting:
            return {}
        now = db_now(self._session)
        counts = with_work(
            self._session, alive_workers(self._session, now, self._lease_seconds)
        ).for_task()
        in_run = {t.asset for t in tasks}
        succeeded = {t.asset for t in tasks if t.status == TaskStatus.SUCCEEDED}
        dead = {
            t.asset
            for t in tasks
            if t.status in (TaskStatus.FAILED, TaskStatus.UPSTREAM_FAILED, TaskStatus.CANCELLED)
        }
        readable: dict[str, bool] = {}

        def may_read(name: str) -> bool:
            """Ask, once per name, whether the reader may read an upstream table.

            Parameters
            ----------
            name
                The upstream task's table, dotted.

            Returns
            -------
            bool
                Whether a READ_TABLE grant covers the name.
            """
            if name not in readable:
                readable[name] = may_read_every(self._authz, reader, environment, [name])
            return readable[name]

        reasons: dict[uuid.UUID, WaitReason] = {}
        for task in waiting:
            upstream = [name for name in upstream_names(task.upstream_json) if name in in_run]
            unfinished = [name for name in upstream if name not in succeeded]
            named = [name for name in unfinished if may_read(name)]
            reason = task_wait_reason(
                WaitingTask(
                    status=task.status.value,
                    attempt=task.attempt,
                    max_attempts=task.max_attempts,
                    next_attempt_at=task.next_attempt_at,
                    unfinished_upstream=named,
                    blocked=any(name in dead for name in unfinished),
                    checking=task.has_checks and task.written_table_id is not None,
                    setting_back=task.setting_back,
                    hidden_upstream=len(unfinished) - len(named),
                ),
                counts,
                now,
            )
            if reason is not None:
                reasons[task.id] = reason
        return reasons

    def _failed_tasks(self, run_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Count the tasks that ended ``failed``, per run, in one query.

        Parameters
        ----------
        run_ids
            The runs.

        Returns
        -------
        dict
            Run id to the count; 0 for a run with none.
        """
        counts = dict.fromkeys(run_ids, 0)
        for run_id, count in self._session.execute(
            select(TaskRun.run_id, func.count())
            .where(TaskRun.run_id.in_(run_ids), TaskRun.status == TaskStatus.FAILED)
            .group_by(TaskRun.run_id)
        ):
            counts[run_id] = count
        return counts

    def _view(
        self,
        run: Run,
        env: Environment,
        reader: PrincipalRef,
        *,
        with_tasks: bool,
        failed: int | None = None,
    ) -> RunView:
        """Build a run's view.

        Parameters
        ----------
        run
            The run.
        env
            Its environment.
        reader
            Who reads it: a generated reference check whose target they may not read is left out
            of its task's checks and counted (`ReferenceCheckFilter`).
        with_tasks
            Include the task runs.
        failed
            The run's failed-task count when the caller counted a page of runs in one query;
            counted here when `None`.

        Returns
        -------
        RunView
            The view.
        """
        repo = self._session.get(Repository, run.repository_id)
        version = self._session.get(JobVersion, run.job_version_id)
        tasks: tuple[TaskRunView, ...] = ()
        notifications: tuple[NotificationView, ...] = ()
        if with_tasks:
            notifications = tuple(
                notification_view(row)
                for row in self._session.scalars(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.run_id == run.id)
                    .order_by(NotificationDelivery.created_at, NotificationDelivery.id)
                    .execution_options(populate_existing=True)
                )
            )
            rows = self._session.execute(
                select(TaskRun, QueryRun.rows_written)
                .outerjoin(QueryRun, QueryRun.id == TaskRun.query_run_id)
                .where(TaskRun.run_id == run.id)
                .order_by(TaskRun.asset)
                .execution_options(populate_existing=True)
            ).all()
            attempted = {
                task_run_id: written
                for task_run_id, written in self._session.execute(
                    select(TaskAttempt.task_run_id, TaskAttempt.rows_written)
                    .join(TaskRun, TaskRun.id == TaskAttempt.task_run_id)
                    .where(
                        TaskRun.run_id == run.id,
                        TaskRun.query_run_id.is_(None),
                        TaskAttempt.outcome == AttemptOutcome.SUCCEEDED,
                    )
                    .order_by(TaskAttempt.attempt)
                )
            }
            reasons = self._waiting(run, [task for task, _ in rows], reader, env.name)
            built = set(manifest_assets(version.manifest_json)) if version is not None else set()
            run_as = self._name(run.run_as_principal_id)
            asset_of = {task.id: task.asset for task, _ in rows}
            checks: dict[uuid.UUID, list[CheckResultView]] = {}
            hidden: dict[uuid.UUID, set[str]] = {}
            every: dict[uuid.UUID, list[CheckResult]] = {}
            withheld = ReferenceCheckFilter(self._session, self._authz, reader, env.name)
            for result in self._session.scalars(
                select(CheckResult)
                .where(CheckResult.run_id == run.id)
                .order_by(CheckResult.check_name)
                .execution_options(populate_existing=True)
            ):
                every.setdefault(result.task_run_id, []).append(result)
                if withheld.hides(result.run_id, result.check_name, result.generated):
                    hidden.setdefault(result.task_run_id, set()).add(result.check_name)
                    continue
                checks.setdefault(result.task_run_id, []).append(
                    view_of(result, asset_of[result.task_run_id], run_as)
                )
            tasks = tuple(
                TaskRunView(
                    asset=task.asset,
                    status=task.status.value,
                    attempt=attempts_used(task.attempt, task.max_attempts),
                    max_attempts=task.max_attempts,
                    timeout_seconds=task.timeout_seconds,
                    next_attempt_at=task.next_attempt_at,
                    started_at=task.started_at,
                    finished_at=task.finished_at,
                    error_message=message_for_reader(
                        task.error_message,
                        task.check_message_state,
                        every.get(task.id, ()),
                        hidden.get(task.id, set()),
                        table=task.asset,
                        run_id=run.id,
                        environment=env.name,
                    ),
                    query_run_id=task.query_run_id,
                    rows_written=(
                        rows_written if task.query_run_id is not None else attempted.get(task.id)
                    ),
                    waiting_for=reasons.get(task.id),
                    kind=task.kind,
                    checks=tuple(checks.get(task.id, ())),
                    hidden_checks=len(hidden.get(task.id, ())),
                    kept_check=(
                        None
                        if version is None and task.kind == SOURCE_CHECK
                        else task.kind == SOURCE_CHECK and task.asset in built
                    ),
                    setting_back=task.setting_back and task.status not in TERMINAL_TASK_STATUSES,
                    failure=task_failure(task),
                )
                for task, rows_written in rows
            )
        return RunView(
            id=run.id,
            environment=env.name,
            repository=repo.name if repo is not None else "",
            job_version_id=run.job_version_id,
            commit_sha=version.commit_sha if version is not None else "",
            kind=run.kind.value,
            status=run.status.value,
            cancel_requested=run.cancel_requested,
            triggered_by=self._name(run.triggered_by),
            run_as=self._name(run.run_as_principal_id),
            resumed_from_run_id=run.resumed_from_run_id,
            selection=dict(run.selection_json or {}),
            error_message=run.error_message,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            tasks=tasks,
            job=run.job,
            trigger=run.trigger.value if run.trigger is not None else None,
            scheduled_for=run.scheduled_for,
            missed=run.missed,
            notifications=notifications,
            failed_tasks=failed if failed is not None else self._failed_tasks([run.id])[run.id],
        )

    # -- start -------------------------------------------------------------

    def _deployment(
        self, env: Environment, repo: Repository
    ) -> tuple[EnvironmentDeployment, JobVersion]:
        """Return the deployment pointer and its project version.

        Parameters
        ----------
        env
            The environment.
        repo
            The repository.

        Returns
        -------
        tuple of (EnvironmentDeployment, JobVersion)
            The pointer and the version it names.

        Raises
        ------
        NotFoundError
            If the repository is not deployed there or its version is gone.
        """
        row = deployment_row(self._session, env, repo)
        version = self._session.get(JobVersion, row.current_job_version_id)
        if version is None:  # pragma: no cover -- the pointer's FK guarantees the version
            raise NotFoundError(f"Repository {repo.name!r} is not deployed in {env.name!r}.")
        return row, version

    def _choose_run_as(
        self,
        p: PrincipalRef | None,
        env: Environment,
        repo: Repository,
        row: EnvironmentDeployment,
        *,
        bound_only: bool = False,
    ) -> _RunAs:
        """Choose who the run writes as.

        Parameters
        ----------
        p
            The caller.
        env
            The environment.
        repo
            The repository.
        row
            Its deployment pointer.
        bound_only
            Never fall back to the caller, also in an unprotected environment: a scheduled run
            has no caller. `p` may then be `None`.

        Returns
        -------
        _RunAs
            The bound principal, or (unprotected only, and not `bound_only`) the caller, with the
            binding and protection it was chosen under (re-verified under lock).

        Raises
        ------
        ConflictError
            Protected (or `bound_only`) without a binding, the bound principal is not a service
            account, or the chosen principal is deactivated or deleted.
        """
        if row.run_as_principal_id is not None:
            bound = self._session.get(Principal, row.run_as_principal_id)
            if bound is None or not bound.is_active:
                name = bound.name if bound is not None else str(row.run_as_principal_id)
                raise ConflictError(_INACTIVE_MESSAGE.format(name=name))
            if bound.type is not PrincipalType.SERVICE_ACCOUNT:
                raise ConflictError(_NOT_SERVICE_ACCOUNT_MESSAGE.format(name=bound.name))
            return _RunAs(bound, bound.id, env.is_protected)
        if env.is_protected or bound_only or p is None:
            raise ConflictError(
                NO_RUN_PRINCIPAL_MESSAGE.format(repository=repo.name, environment=env.name)
            )
        caller = self._session.get(Principal, p.id)
        if caller is None or not caller.is_active:
            raise ConflictError(_INACTIVE_MESSAGE.format(name=p.name))
        return _RunAs(caller, None, False)

    def _reverify_choice(
        self,
        env_id: uuid.UUID,
        repo_id: uuid.UUID,
        choice: _RunAs,
        run_as_id: uuid.UUID,
        run_as_name: str,
    ) -> None:
        """Re-check the run principal choice after the pointer is locked.

        Lock order is pointer (`lock_deployment`), then environment, then
        principal. The environment and principal rows are read `FOR SHARE`:
        on Postgres that blocks a concurrent protection change (an `UPDATE` of
        the environments row) and a principal deactivation until this
        transaction ends, and re-evaluates the `WHERE` after any wait, so a
        change either commits before the read (and is seen) or waits for this
        run to commit. Concurrent starts do not serialise on the shared lock.
        SQLite serialises writers anyway.

        Parameters
        ----------
        env_id
            The environment.
        repo_id
            The repository.
        choice
            The choice and what it was made from.
        run_as_id
            The chosen principal's id.
        run_as_name
            The chosen principal's name.

        Raises
        ------
        ConflictError
            If the binding or protection moved, or the principal is no longer
            active; the transaction is rolled back.
        """
        bound_id = self._session.execute(
            select(EnvironmentDeployment.run_as_principal_id).where(
                EnvironmentDeployment.environment_id == env_id,
                EnvironmentDeployment.repository_id == repo_id,
            )
        ).scalar_one()
        protected = self._session.execute(
            select(Environment.is_protected)
            .where(Environment.id == env_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).scalar_one()
        if bound_id != choice.bound_id or protected != choice.protected:
            self._session.rollback()
            raise ConflictError(
                "The run principal binding or the environment's protection changed meanwhile; "
                "retry."
            )
        locked = select(Principal.id).where(
            Principal.id == run_as_id, Principal.is_active.is_(True)
        )
        if choice.bound_id is not None:
            locked = locked.where(Principal.type == PrincipalType.SERVICE_ACCOUNT)
        if self._session.execute(locked.with_for_update(read=True)).first() is None:
            self._session.rollback()
            raise ConflictError(_INACTIVE_MESSAGE.format(name=run_as_name))

    def insert_run(
        self,
        p: PrincipalRef,
        env: Environment,
        repo: Repository,
        version: JobVersion,
        choice: _RunAs,
        assets: Sequence[str],
        *,
        kind: RunKind,
        selection: Selection,
        resumed_from: uuid.UUID | None,
        job: str | None = None,
        trigger: RunTrigger | None = None,
        scheduled_for: datetime | None = None,
        missed: int = 0,
        merged: int = 1,
    ) -> Run:
        """Insert a run and its task runs and audit it, without committing.

        A task run is inserted per selected asset, marked `has_checks` when the manifest has
        checks for its table, and one ``source_check`` task run per checked table that the
        project only reads and a selected asset reads. The latter has no upstream and
        is an upstream of each of its readers, so the scheduler's existing rules do the rest.
        A table that keeps data that failed a ``keep`` check (it has a note, `kept_tables`) and
        that a selected asset reads, without the run building it, gets the same kind of task
        (`kept_check_tables`): its readers wait for the table's checks to pass again.
        A selected asset is listed as an upstream of the selected assets that read it only
        through unselected views (`view_readers`), so that they come after it.
        The notes are read after the run's row is inserted, under the deployment's lock: the
        insert admits one active run of the deployment, so an earlier run of the project has
        set its notes before this run looks for them.

        Parameters
        ----------
        p
            The caller.
        env
            The environment.
        repo
            The repository.
        version
            The project version to run.
        choice
            Who the run writes as, and what that was decided from.
        assets
            The selected assets, non-empty.
        kind
            Run kind.
        selection
            What was asked for, stored for display.
        resumed_from
            The run a resume continues.
        job
            The job the run belongs to; `None` for an ad-hoc run.
        trigger
            What started it; `None` for an ad-hoc run.
        scheduled_for
            For a scheduled run, the time the schedule named.
        missed
            How many earlier scheduled times the run stands in for.
        merged
            How many requests the run stands for: 1, or more for a waiting start that others
            merged into.

        Returns
        -------
        Run
            The queued run, flushed and not committed. The deployment's lock is held, so a caller
            that must change a row in the same transaction takes its own lock after this call
            (the order of a deploy: the pointer first).

        Raises
        ------
        ActiveRunError
            If another run of this deployment is active; the transaction is rolled back.
        ConflictError
            If the pointer, its
            run principal binding, the environment's protection or the chosen
            principal's activity changed since the choice; nothing is written.
        """
        graph = ManifestGraph.from_manifest(version.manifest_json)
        entries = _entries(version.manifest_json)
        upstreams = selected_upstreams(graph, assets)
        manifest_checks = read_manifest_checks(version.manifest_json)
        sources = source_check_tables(version.manifest_json, assets, manifest_checks)
        # What reads a selected asset through an unselected view has no edge to it among the
        # selected assets, and must still come after it is rebuilt. Computed before the lock
        # that admits one active run, so that the walk does not lengthen the time it is held.
        through_views = view_readers(version.manifest_json, assets)
        env_id, repo_id, env_name, repo_name = env.id, repo.id, env.name, repo.name
        run_as = choice.principal
        run_as_id, run_as_name = run_as.id, run_as.name
        DeployService(self._session, self._authz).lock_deployment(env_id, repo_id, version.id)
        self._reverify_choice(env_id, repo_id, choice, run_as_id, run_as_name)
        run = Run(
            id=uuid.uuid4(),
            environment_id=env_id,
            repository_id=repo_id,
            job_version_id=version.id,
            triggered_by=p.id,
            run_as_principal_id=run_as_id,
            kind=kind,
            resumed_from_run_id=resumed_from,
            selection_json=selection.to_json(),
            status=RunStatus.QUEUED,
            cancel_requested=False,
            job=job,
            trigger=trigger,
            scheduled_for=scheduled_for,
            missed=missed,
        )
        self._session.add(run)
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            active = self._session.execute(
                select(Run.id).where(
                    Run.environment_id == env_id,
                    Run.repository_id == repo_id,
                    Run.status.in_(ACTIVE_RUN_STATUSES),
                )
            ).scalar_one_or_none()
            if active is None:
                raise ConflictError(
                    f"The run of {repo_name!r} in {env_name!r} could not be recorded "
                    "(the deployment, the run principal or a concurrent run changed); retry."
                ) from None
            raise ActiveRunError(
                f"Run {active} of {repo_name!r} is already active in {env_name!r}.", active
            ) from None
        # Read only now, after the insert that admits one active run of the deployment. Only a
        # run of this project sets a note on its tables, and a task sets the note before its
        # outcome is recorded; no other run of the project is active by now, so the note of an
        # earlier run is committed and this read sees it.
        kept = kept_tables(self._session, env_id)
        if kept:
            # A table that keeps data that failed a check is checked before what reads it
            # is built, unless this run builds it.
            sources.update(kept_check_tables(version.manifest_json, assets, kept.keys()))
        for asset in assets:
            policy = policy_from_manifest(entries.get(asset, {}), self._caps)
            self._session.add(
                TaskRun(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    asset=asset,
                    status=TaskStatus.PENDING,
                    attempt=0,
                    max_attempts=policy.retries + 1,
                    timeout_seconds=policy.timeout_seconds,
                    retry_delay_seconds=policy.retry_delay_seconds,
                    upstream_json=sorted(
                        {
                            *upstreams.get(asset, ()),
                            *(t for t, rs in sources.items() if asset in rs),
                            *(t for t, rs in through_views.items() if asset in rs),
                        }
                    ),
                    conflict_retried=False,
                    kind=ASSET,
                    has_checks=bool(manifest_checks.of(asset))
                    or manifest_checks.unreadable_for(asset),
                )
            )
        for table in sources:
            policy = policy_from_manifest({}, self._caps)
            self._session.add(
                TaskRun(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    asset=table,
                    status=TaskStatus.PENDING,
                    attempt=0,
                    max_attempts=policy.retries + 1,
                    timeout_seconds=policy.timeout_seconds,
                    retry_delay_seconds=policy.retry_delay_seconds,
                    upstream_json=[],
                    conflict_retried=False,
                    kind=SOURCE_CHECK,
                    has_checks=True,
                )
            )
        AuditService(self._session).record(
            p.id,
            AuditEventType.RUN_STARTED,
            securable=Securable.environment_scope(env_name),
            detail={
                "run_id": str(run.id),
                "repository": repo_name,
                "project_version_id": str(version.id),
                "kind": kind.value,
                "tasks": len(assets) + len(sources),
                "run_as": run_as_name,
                "resumed_from_run_id": str(resumed_from) if resumed_from else None,
                "job": job,
                "trigger": trigger.value if trigger is not None else None,
                "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
                "missed": missed,
                "merged": merged,
                **_ci_rule_detail(p),
            },
        )
        return run

    def _create(
        self,
        p: PrincipalRef,
        env: Environment,
        repo: Repository,
        version: JobVersion,
        choice: _RunAs,
        assets: Sequence[str],
        *,
        kind: RunKind,
        selection: Selection,
        resumed_from: uuid.UUID | None,
        job: str | None = None,
        trigger: RunTrigger | None = None,
        scheduled_for: datetime | None = None,
        missed: int = 0,
        merged: int = 1,
    ) -> RunView:
        """Insert a run and its task runs in one transaction and audit it.

        Parameters
        ----------
        p
            The caller.
        env
            The environment.
        repo
            The repository.
        version
            The project version to run.
        choice
            Who the run writes as, and what that was decided from.
        assets
            The selected assets, non-empty.
        kind
            Run kind.
        selection
            What was asked for, stored for display.
        resumed_from
            The run a resume continues.
        job
            The job the run belongs to; `None` for an ad-hoc run.
        trigger
            What started it; `None` for an ad-hoc run.
        scheduled_for
            For a scheduled run, the time the schedule named.
        missed
            How many earlier scheduled times the run stands in for.
        merged
            How many requests the run stands for.

        Returns
        -------
        RunView
            The queued run with its tasks.

        Raises
        ------
        ActiveRunError
            If another run of this deployment is active.
        ConflictError
            If the pointer, its run principal binding, the environment's protection or the chosen
            principal's activity changed since the choice; nothing is written.
        """
        run = self.insert_run(
            p,
            env,
            repo,
            version,
            choice,
            assets,
            kind=kind,
            selection=selection,
            resumed_from=resumed_from,
            job=job,
            trigger=trigger,
            scheduled_for=scheduled_for,
            missed=missed,
            merged=merged,
        )
        self._session.commit()
        return self._view(run, env, p, with_tasks=True)

    def _job_selection(
        self, env: Environment, repo: Repository, version: JobVersion, job: str
    ) -> Selection:
        """Read a job's selection from the manifest of a project version.

        Parameters
        ----------
        env
            The environment.
        repo
            The repository.
        version
            The project version that is current.
        job
            The job's name.

        Returns
        -------
        Selection
            The job's patterns and closure flags.

        Raises
        ------
        NotFoundError
            If the version declares no job of that name; the message lists the ones it declares.
        """
        entry = manifest_jobs(version.manifest_json).get(job)
        if entry is None:
            raise NotFoundError(
                job_missing_message(version.manifest_json, job, repo.name, env.name)
            )
        patterns = entry.get("select")
        return Selection(
            patterns=tuple(x for x in patterns if isinstance(x, str))
            if isinstance(patterns, list)
            else (),
            upstream=entry.get("upstream") is True,
            downstream=entry.get("downstream") is True,
        )

    def prepare_start(
        self,
        p: PrincipalRef | None,
        environment: str,
        repository: str,
        selection: Selection | None,
        *,
        job: str | None = None,
        authorize: bool = True,
        bound_only: bool = False,
    ) -> PreparedStart:
        """Authorize a start and decide what it runs and who it writes as.

        The steps of every start, ad hoc or of a job, in one place: `RUN` first, the
        deployment, the job's selection (when it is a job), the run principal, the selection
        resolved against the DAG.

        Parameters
        ----------
        p
            The caller; `None` only when `authorize` is false and `bound_only` is true.
        environment
            Environment name.
        repository
            Repository name.
        selection
            What an ad-hoc run runs; `None` for a job, whose selection the manifest holds.
        job
            The job to run, or `None` for an ad-hoc run.
        authorize
            Check `RUN` first. False only for a start that was authorized when it was requested
            (a waiting start) or that has no caller (the schedule's: the deploy that declared the
            schedule and the binding of the run principal are the authorization).
        bound_only
            Only the bound principal may run it, also in an unprotected environment, and it is
            also the actor; for a start that has no caller to fall back to.

        Returns
        -------
        PreparedStart
            The decisions, to pass on to `_create`.

        Raises
        ------
        PermissionDenied
            Without RUN on the environment (checked first).
        NotFoundError
            If the environment, repository or job does not exist or is not deployed.
        ConflictError
            No run principal in a protected environment, an inactive one.
        ValidationError
            If the selection is malformed or matches nothing.
        TypeError
            If there is no caller and `bound_only` is false, or an ad-hoc start has no selection.
        """
        if p is None and not bound_only:
            raise TypeError("a start that is not the schedule's needs its caller")
        if authorize:
            if p is None:
                raise TypeError("a start that is checked needs its caller")
            self._authz.authorize(p, Action.RUN, Securable.environment_scope(environment))
        env, repo = self._environment(environment), self._repository(repository)
        row, version = self._deployment(env, repo)
        if job is not None:
            selection = self._job_selection(env, repo, version, job)
            _require_on_deploy(p, version, job, env.name)
        elif selection is None:
            raise TypeError("an ad-hoc start needs a selection")
        choice = (
            self._choose_run_as(p, env, repo, row, bound_only=True)
            if bound_only
            else self._choose_run_as(p, env, repo, row)
        )
        assets = resolve_selection(ManifestGraph.from_manifest(version.manifest_json), selection)
        actor = (
            PrincipalRef(
                id=choice.principal.id, name=choice.principal.name, type=choice.principal.type
            )
            if p is None or bound_only
            else p
        )
        return PreparedStart(env, repo, version, choice, selection, assets, actor)

    def start(
        self, p: PrincipalRef, environment: str, repository: str, selection: Selection
    ) -> RunView:
        """Start an ad-hoc run of a repository's current project version.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        repository
            Repository name.
        selection
            Patterns and closure flags; empty patterns run the whole DAG.

        Returns
        -------
        RunView
            The queued run.

        Raises
        ------
        PermissionDenied
            Without RUN on the environment (checked first).
        NotFoundError
            If the environment or repository does not exist or is not deployed.
        ConflictError
            No run principal in a protected environment, an inactive one,
            or another active run (an ad-hoc run never waits).
        ValidationError
            If the selection is malformed or matches nothing.
        """
        prepared = self.prepare_start(p, environment, repository, selection)
        try:
            return self._create(
                prepared.actor,
                prepared.env,
                prepared.repo,
                prepared.version,
                prepared.choice,
                prepared.assets,
                kind=RunKind.SELECTION if selection.patterns else RunKind.FULL,
                selection=selection,
                resumed_from=None,
            )
        except ActiveRunError as exc:
            raise ConflictError(str(exc)) from None

    def start_job(
        self,
        p: PrincipalRef | None,
        environment: str,
        repository: str,
        job: str,
        *,
        trigger: RunTrigger,
        scheduled_for: datetime | None = None,
        missed: int = 0,
    ) -> RunView:
        """Start a run of a job of a repository's current project version.

        Parameters
        ----------
        p
            The caller; `None` for the trigger ``schedule``.
        environment
            Environment name.
        repository
            Repository name.
        job
            The job's name.
        trigger
            What starts the run. ``schedule`` needs no caller and only the bound principal.
        scheduled_for
            For a scheduled run, the time the schedule named.
        missed
            How many earlier scheduled times the run stands in for.

        Returns
        -------
        RunView
            The queued run.

        Raises
        ------
        PermissionDenied
            Without RUN on the environment (checked first; not for ``schedule``).
        NotFoundError
            If the environment, repository or job does not exist or is not deployed.
        ActiveRunError
            If another run of the repository is active in the environment.
        ConflictError
            No run principal, or an inactive one.
        ValidationError
            If the job's selection matches nothing in this project version.
        """
        scheduled = trigger is RunTrigger.SCHEDULE
        prepared = self.prepare_start(
            p,
            environment,
            repository,
            None,
            job=job,
            authorize=not scheduled,
            bound_only=scheduled,
        )
        selection = prepared.selection
        return self._create(
            prepared.actor,
            prepared.env,
            prepared.repo,
            prepared.version,
            prepared.choice,
            prepared.assets,
            kind=RunKind.SELECTION if selection.patterns else RunKind.FULL,
            selection=selection,
            resumed_from=None,
            job=job,
            trigger=trigger,
            scheduled_for=scheduled_for,
            missed=missed,
        )

    # -- reads -------------------------------------------------------------

    def get(self, p: PrincipalRef, environment: str, run_id: uuid.UUID) -> RunView:
        """Show one run with its tasks.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        run_id
            The run.

        Returns
        -------
        RunView
            The run.

        Raises
        ------
        PermissionDenied
            Without RUN or READ_ENVIRONMENT.
        NotFoundError
            If the run does not exist in this environment.
        """
        env = self._authorize_read(p, environment)
        return self._view(self._run(env, run_id), env, p, with_tasks=True)

    def list_runs(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        repository: str | None = None,
        limit: int = 100,
    ) -> list[RunView]:
        """List an environment's runs, newest first, without tasks.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        repository
            Only this repository's runs.
        limit
            Most runs returned.

        Returns
        -------
        list of RunView
            Newest first.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT.
        NotFoundError
            If the environment or the repository does not exist.
        """
        return self.list_runs_page(p, environment, repository=repository, limit=limit)[0]

    def list_runs_page(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        repository: str | None = None,
        status: Sequence[str] = (),
        trigger: Sequence[str] = (),
        job: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[RunView], str | None]:
        """List an environment's runs, newest first, one page at a time, without tasks.

        Every filter narrows the list; they combine with AND. Runs are ordered by creation time,
        then id, newest first; a page continues strictly after the last run of the previous one.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        repository
            Only this repository's runs.
        status
            Only runs in one of these statuses (``queued``, ``running``, ``succeeded``,
            ``failed``, ``cancelled``); none means any.
        trigger
            Only runs started by one of these (``schedule``, ``api``, ``manual``); none means
            any. An ad-hoc run has no trigger and matches none of them.
        job
            Only runs of this job.
        since
            Only runs created at or after this time (timezone-aware).
        limit
            Most runs returned.
        cursor
            The `next_cursor` of the previous page. Send the same filters with every page.

        Returns
        -------
        tuple of (list of RunView, str or None)
            The page, and the cursor of the next one, or `None` on the last.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT.
        NotFoundError
            If the environment or the repository does not exist.
        ValidationError
            If `limit` is below 1, a status or trigger is unknown, `since` has no time zone, or
            `cursor` is not one this server issued for the runs list.
        """
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment))
        if limit < 1:
            raise ValidationError(f"limit must be at least 1, got {limit}.")
        statuses = parse_choices("status", status, RunStatus)
        triggers = parse_choices("trigger", trigger, RunTrigger)
        if since is not None and since.tzinfo is None:
            raise ValidationError(
                "`since` has no time zone. Write it with one, for example 2026-10-18T00:00:00Z "
                "or 2026-10-18T00:00:00+02:00."
            )
        position = None if cursor is None else decode_cursor("runs", cursor)
        env = self._environment(environment)
        query = select(Run).where(Run.environment_id == env.id)
        if repository is not None:
            query = query.where(Run.repository_id == self._repository(repository).id)
        if statuses:
            query = query.where(Run.status.in_(statuses))
        if triggers:
            query = query.where(Run.trigger.in_(triggers))
        if job is not None:
            query = query.where(Run.job == job)
        if since is not None:
            query = query.where(Run.created_at >= since)
        if position is not None:
            query = query.where(
                or_(
                    Run.created_at < position.at,
                    and_(Run.created_at == position.at, Run.id < position.id),
                )
            )
        runs = list(
            self._session.scalars(
                query.order_by(Run.created_at.desc(), Run.id.desc()).limit(limit + 1)
            )
        )
        page = runs[:limit]
        next_cursor = None
        if len(runs) > limit:
            next_cursor = encode_cursor("runs", page[-1].created_at, page[-1].id)
        failed = self._failed_tasks([run.id for run in page])
        views = [self._view(run, env, p, with_tasks=False, failed=failed[run.id]) for run in page]
        return views, next_cursor

    # -- check results -------------------------------------------------------

    def checks(self, p: PrincipalRef, environment: str, run_id: uuid.UUID) -> list[CheckResultView]:
        """List the check results of one run.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        run_id
            The run.

        Returns
        -------
        list of CheckResultView
            By task, then by check name. A generated reference check whose target `p` may not
            read is left out; `get` counts it on its task.

        Raises
        ------
        PermissionDenied
            Without RUN or READ_ENVIRONMENT.
        NotFoundError
            If the run does not exist in this environment.
        """
        env = self._authorize_read(p, environment)
        run = self._run(env, run_id)
        run_as = self._name(run.run_as_principal_id)
        rows = self._session.execute(
            select(CheckResult, TaskRun.asset)
            .join(TaskRun, TaskRun.id == CheckResult.task_run_id)
            .where(CheckResult.run_id == run.id)
            .order_by(TaskRun.asset, CheckResult.check_name)
            .execution_options(populate_existing=True)
        ).all()
        withheld = ReferenceCheckFilter(self._session, self._authz, p, env.name)
        return [
            view_of(result, asset, run_as)
            for result, asset in rows
            if not withheld.hides(result.run_id, result.check_name, result.generated)
        ]

    def list_checks(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        table: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[CheckResultView], str | None, int]:
        """List the check results of an environment, newest first, one page at a time.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        table
            Only the results for this dotted table name.
        limit
            Most results returned.
        cursor
            The cursor of the previous page. Send the same `table` with every page.

        Returns
        -------
        tuple of (list of CheckResultView, str or None, int)
            The page, ordered by finish time and then id, newest first; the cursor of the
            next page, or `None` on the last; and how many results of the page were left out
            because they are generated reference checks whose target `p` may not read. A page
            can therefore hold fewer than `limit` results and still have a next one.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT.
        NotFoundError
            If the environment does not exist.
        ValidationError
            If `limit` is below 1 or `cursor` is not one this server issued.
        """
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment))
        if limit < 1:
            raise ValidationError(f"limit must be at least 1, got {limit}.")
        env = self._environment(environment)
        query = (
            select(CheckResult, TaskRun.asset, Principal.name)
            .join(TaskRun, TaskRun.id == CheckResult.task_run_id)
            .join(Run, Run.id == CheckResult.run_id)
            .outerjoin(Principal, Principal.id == Run.run_as_principal_id)
            .where(CheckResult.environment_id == env.id)
        )
        if table is not None:
            query = query.where(CheckResult.table_name == table)
        if cursor is not None:
            position = decode_cursor("checks", cursor)
            query = query.where(
                or_(
                    CheckResult.finished_at < position.at,
                    and_(CheckResult.finished_at == position.at, CheckResult.id < position.id),
                )
            )
        rows = self._session.execute(
            query.order_by(CheckResult.finished_at.desc(), CheckResult.id.desc())
            .limit(limit + 1)
            .execution_options(populate_existing=True)
        ).all()
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1][0]
            next_cursor = encode_cursor("checks", last.finished_at, last.id)
        withheld = ReferenceCheckFilter(self._session, self._authz, p, env.name)
        shown = [
            view_of(result, asset, run_as)
            for result, asset, run_as in page
            if not withheld.hides(result.run_id, result.check_name, result.generated)
        ]
        return shown, next_cursor, len(page) - len(shown)

    # -- cancel and resume -------------------------------------------------

    def cancel(self, p: PrincipalRef, environment: str, run_id: uuid.UUID) -> RunView:
        """Request cancellation; the scheduler and the workers carry it out.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        run_id
            The run.

        Returns
        -------
        RunView
            The run with `cancel_requested` set.

        Raises
        ------
        PermissionDenied
            Without RUN (checked first).
        NotFoundError
            If the run does not exist in this environment.
        ConflictError
            If the run already finished.
        """
        self._authz.authorize(p, Action.RUN, Securable.environment_scope(environment))
        env = self._environment(environment)
        run = self._run(env, run_id)
        result = self._session.execute(
            update(Run)
            .where(
                Run.id == run.id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
                Run.cancel_requested.is_(False),
            )
            .values(cancel_requested=True)
            .execution_options(synchronize_session=False)
        )
        if cast("CursorResult[Any]", result).rowcount != 1:
            self._session.rollback()
            current = self._run(env, run_id)
            if current.status in ACTIVE_RUN_STATUSES:
                return self._view(current, env, p, with_tasks=True)
            raise ConflictError(f"Run {run_id} is already {current.status.value}.")
        repo = self._session.get(Repository, run.repository_id)
        AuditService(self._session).record(
            p.id,
            AuditEventType.RUN_CANCEL_REQUESTED,
            securable=Securable.environment_scope(environment),
            detail={"run_id": str(run_id), "repository": repo.name if repo is not None else None},
        )
        self._session.commit()
        return self._view(self._run(env, run_id), env, p, with_tasks=True)

    def resume(self, p: PrincipalRef, environment: str, run_id: uuid.UUID) -> RunView:
        """Start a new run of the same project version over what did not succeed.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        run_id
            The finished run to resume.

        Returns
        -------
        RunView
            The new run (`kind=resume`).

        Raises
        ------
        PermissionDenied
            Without RUN (checked first).
        NotFoundError
            If the run does not exist here or the repository is no longer deployed.
        ConflictError
            If the run is still active, the current project version changed, or as
            for `start`.
        ValidationError
            If no task failed, was upstream_failed or was cancelled.
        """
        self._authz.authorize(p, Action.RUN, Securable.environment_scope(environment))
        env = self._environment(environment)
        source = self._run(env, run_id)
        if source.status in ACTIVE_RUN_STATUSES:
            raise ConflictError(
                f"Run {run_id} is still active; cancel it or wait for it to finish."
            )
        repo = self._session.get(Repository, source.repository_id)
        if repo is None:  # pragma: no cover -- runs cascade with their repository
            raise NotFoundError(f"No run {run_id} in {environment}.")
        row, version = self._deployment(env, repo)
        if version.id != source.job_version_id:
            raise ConflictError(
                f"The deployed project version of {repo.name!r} changed since run {run_id}; "
                "start a fresh run (a run never mixes code versions)."
            )
        seeds = list(
            self._session.scalars(
                select(TaskRun.asset).where(
                    TaskRun.run_id == source.id, TaskRun.status.in_(_RESUMABLE)
                )
            )
        )
        assets = downstream_closure(ManifestGraph.from_manifest(version.manifest_json), seeds)
        if not assets:
            raise ValidationError(f"Run {run_id} has nothing to resume.")
        choice = self._choose_run_as(p, env, repo, row)
        return self._create(
            p,
            env,
            repo,
            version,
            choice,
            assets,
            kind=RunKind.RESUME,
            selection=Selection(patterns=tuple(sorted(seeds)), downstream=True),
            resumed_from=source.id,
            job=source.job,
            trigger=trigger_of(p) if source.job is not None else None,
        )

    # -- logs and status -----------------------------------------------------

    def task_log(
        self,
        p: PrincipalRef,
        environment: str,
        run_id: uuid.UUID,
        asset: str,
        offset: int | None = None,
    ) -> TaskLogView:
        """Return a task's free-text log and its attempts.

        The stored log is already redacted (the worker redacts before it appends), so cutting
        it at a byte never splits a secret into two readable halves; an offset counts the
        bytes of what the caller receives.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        run_id
            The run.
        asset
            The task's dotted table name.
        offset
            The byte to read from; `None` returns the whole log. With an offset at most
            `LOG_CHUNK_BYTES` come back, ending before a half character.

        Returns
        -------
        TaskLogView
            Text (empty when none was written or storage failed), where to continue, and
            attempts.

        Raises
        ------
        PermissionDenied
            Without RUN or READ_ENVIRONMENT.
        NotFoundError
            If the run or the task does not exist.
        ValidationError
            If the task's stored name is not a dotted identifier.
        """
        env = self._authorize_read(p, environment)
        run = self._run(env, run_id)
        task = self._session.execute(
            select(TaskRun).where(TaskRun.run_id == run.id, TaskRun.asset == asset)
        ).scalar_one_or_none()
        if task is None:
            raise NotFoundError(f"Run {run_id} has no task {asset!r}.")
        results = list(
            self._session.scalars(select(CheckResult).where(CheckResult.task_run_id == task.id))
        )
        withheld = ReferenceCheckFilter(self._session, self._authz, p, env.name)
        hidden = {
            r.check_name for r in results if withheld.hides(r.run_id, r.check_name, r.generated)
        }
        attempts = tuple(
            TaskAttemptView(
                attempt=a.attempt,
                worker=a.worker,
                started_at=a.started_at,
                finished_at=a.finished_at,
                outcome=a.outcome.value if a.outcome is not None else None,
                rows_written=a.rows_written,
                error_message=message_for_reader(
                    a.error_message,
                    a.check_message_state,
                    results,
                    hidden,
                    table=asset,
                    run_id=run.id,
                    environment=env.name,
                ),
                failure=attempt_failure(a),
            )
            for a in self._session.scalars(
                select(TaskAttempt)
                .where(TaskAttempt.task_run_id == task.id)
                .order_by(TaskAttempt.attempt, TaskAttempt.started_at)
            )
        )
        path = task_log_path(run.id, asset)
        text = ""
        next_offset = offset or 0
        more = reset = read = False
        try:
            storage = (
                StorageResolver(self._session, self._key_provider).for_environment(env.name).storage
            )
            data = storage.read(path) if storage.exists(path) else b""
            text, _, next_offset = log_window(data, offset)
            more = next_offset < len(data)
            reset = offset is not None and offset > len(data)
            read = True
        except (OSError, LakanaError):
            pass
        return TaskLogView(
            run_id=run.id,
            asset=asset,
            text=text,
            attempts=attempts,
            next_offset=next_offset,
            more=more,
            complete=read and not more and task.status in TERMINAL_TASK_STATUSES,
            reset=reset,
        )

    def scheduler_status(self, p: PrincipalRef) -> SchedulerView:
        """Report the scheduler holder and queue lengths (admin).

        Parameters
        ----------
        p
            The caller.

        Returns
        -------
        SchedulerView
            Holder, heartbeat and counts.

        Raises
        ------
        PermissionDenied
            Without MANAGE_ENVIRONMENT at instance scope.
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.instance_scope())
        lease = self._session.get(SchedulerLease, SCHEDULER_LEASE_ID)
        active = self._session.scalar(
            select(func.count()).select_from(Run).where(Run.status.in_(ACTIVE_RUN_STATUSES))
        )
        counts = Counter(
            {
                status: count
                for status, count in self._session.execute(
                    select(TaskRun.status, func.count())
                    .join(Run, Run.id == TaskRun.run_id)
                    .where(Run.status.in_(ACTIVE_RUN_STATUSES))
                    .group_by(TaskRun.status)
                )
            }
        )
        return SchedulerView(
            holder=lease.holder if lease is not None else None,
            heartbeat_at=lease.heartbeat_at if lease is not None else None,
            active_runs=int(active or 0),
            pending=counts[TaskStatus.PENDING],
            ready=counts[TaskStatus.READY],
            running=counts[TaskStatus.RUNNING],
        )

    def list_workers(self, p: PrincipalRef) -> list[WorkerView]:
        """List the registered workers, by id, with their state and what each is doing (admin).

        A worker is `alive` when its heartbeat is younger than the lease, by the database's
        clock and the same comparison every wait reason and sweep uses, and `lost` otherwise
        (its row is kept an hour after its last heartbeat). What it is doing comes from three
        reads, one for all workers each: `RUNNING` query runs and `RUNNING` deploy runs that
        carry its id, `RUNNING` task runs whose lease it holds; with several, the oldest. Only
        run ids are shown, never SQL, names or other principals' data. Six statements whatever
        the number of workers.

        Parameters
        ----------
        p
            The caller.

        Returns
        -------
        list of WorkerView
            One per row of `workers`, by id.

        Raises
        ------
        PermissionDenied
            Without MANAGE_ENVIRONMENT at instance scope (as `scheduler_status`).
        """
        self._authz.authorize(p, Action.MANAGE_ENVIRONMENT, Securable.instance_scope())
        workers = list(self._session.scalars(select(Worker).order_by(Worker.id)))
        alive = alive_ids(self._session, db_now(self._session), self._lease_seconds)
        query_runs = self._oldest_per_worker(
            self._session.execute(
                select(QueryRun.worker_id, QueryRun.id)
                .where(QueryRun.status == QueryRunStatus.RUNNING, QueryRun.worker_id.is_not(None))
                .order_by(func.coalesce(QueryRun.started_at, QueryRun.created_at), QueryRun.id)
            )
        )
        deploy_runs = self._oldest_per_worker(
            self._session.execute(
                select(DeployRun.worker_id, DeployRun.id)
                .where(DeployRun.status == DeployStatus.RUNNING, DeployRun.worker_id.is_not(None))
                .order_by(func.coalesce(DeployRun.started_at, DeployRun.created_at), DeployRun.id)
            )
        )
        task_runs = self._oldest_per_worker(
            self._session.execute(
                select(TaskRun.lease_owner, TaskRun.id)
                .where(TaskRun.status == TaskStatus.RUNNING, TaskRun.lease_owner.is_not(None))
                .order_by(TaskRun.started_at, TaskRun.id)
            )
        )
        return [
            WorkerView(
                id=w.id,
                host=w.host,
                version=w.version,
                started_at=w.started_at,
                heartbeat_at=w.heartbeat_at,
                status="alive" if w.id in alive else "lost",
                takes=w.takes,
                query_run_id=query_runs.get(w.id),
                deploy_run_id=deploy_runs.get(w.id),
                task_run_id=task_runs.get(w.id),
                caches=cache_sizes(w),
                caches_measured_at=w.cache_measured_at,
            )
            for w in workers
        ]

    @staticmethod
    def _oldest_per_worker(rows: Iterable[tuple[str, uuid.UUID]]) -> dict[str, uuid.UUID]:
        """Keep the first run id per worker id of rows ordered oldest first.

        Parameters
        ----------
        rows
            `(worker id, run id)` pairs, oldest first.

        Returns
        -------
        dict
            Run id by worker id.
        """
        found: dict[str, uuid.UUID] = {}
        for worker_id, run_id in rows:
            found.setdefault(worker_id, run_id)
        return found
