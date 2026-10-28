"""`JobService`: start, pause, resume and list the jobs of a project.

A job is a named selection in the manifest of the project version; the platform keeps one
`JobSchedule` row for it per environment (when it fires next, whether it is paused, the one start
that waits). A start goes through the same code as an ad-hoc run (`RunService`), so it has the same
authorization, run principal rules and checks; what it adds is that a busy project makes the start
wait instead of being refused.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.ci_oidc.scope import refuse_ci_token
from lakana_core.db import db_now
from lakana_core.errors import ConflictError, NotFoundError, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.jobs.audit import start_detail
from lakana_core.jobs.cron import fire_times_between, next_fire_after
from lakana_core.jobs.tz import load_zone
from lakana_core.models import (
    AuditEventType,
    Environment,
    EnvironmentDeployment,
    JobFire,
    JobSchedule,
    JobVersion,
    NotificationDelivery,
    Principal,
    PrincipalType,
    Repository,
    Run,
    RunKind,
    RunTrigger,
)
from lakana_core.models.run import ACTIVE_RUN_STATUSES
from lakana_core.notifications.events import new_delivery
from lakana_core.notifications.views import DeliveryView, delivery_view
from lakana_core.projects.deploys import DeploymentMoved, DeployService
from lakana_core.projects.lookups import deployment_row, environment_by_name, repository_by_name
from lakana_core.projects.manifest import job_notify, job_on_deploy, job_schedule, manifest_jobs
from lakana_core.projects.run_policy import RunCaps
from lakana_core.runs.service import (
    ActiveRunError,
    RunService,
    RunView,
    authorize_run_read,
    job_missing_message,
    trigger_of,
)
from lakana_core.runs.waiting import (
    JOB_STARTS_SOON,
    JOB_STARTS_SOON_MESSAGE,
    JOB_WAITS,
    JOB_WAITS_MESSAGE,
    NO_RUN_PRINCIPAL,
    NO_RUN_PRINCIPAL_JOB_MESSAGE,
    RUN_PRINCIPAL_UNUSABLE,
    RUN_PRINCIPAL_UNUSABLE_JOB_MESSAGE,
    WaitReason,
)

logger = logging.getLogger(__name__)

STATE_SCHEDULED = "scheduled"
STATE_PAUSED = "paused"
STATE_WAITING = "waiting"
STATE_STARTING = "waiting to start"
STATE_NO_SCHEDULE = "no schedule"
STATE_NO_RUN_PRINCIPAL = "no run principal"
STATE_RUN_PRINCIPAL_UNUSABLE = "run principal unusable"

_ATTEMPTS = 3
"""How often a start tries again when the run it waited for ended before the wait was recorded,
or when a deploy moved the deployment's pointer while it waited for the lock."""

DROP_REQUESTER_INACTIVE = "requester_inactive"
DROP_REQUESTER_LACKS_RUN = "requester_lacks_run"
DROP_NO_RUN_PRINCIPAL = "no_run_principal"
DROP_RUN_PRINCIPAL_UNUSABLE = "run_principal_unusable"
DROP_JOB_REMOVED = "job_removed"
DROP_SELECTION_INVALID = "selection_invalid"
DROP_UNREADABLE = "unreadable"
"""The reasons a waiting start is dropped when it comes to start (`JOB_START_DROPPED`)."""


@dataclass(frozen=True)
class WaitingStart:
    """The one start of a job that waits for the active run of its project.

    Parameters
    ----------
    job
        The job.
    environment
        Environment name.
    waits_for_run_id
        The active run it waits for.
    trigger
        ``schedule``, ``api``, ``manual`` or ``deploy``: the latest request's, unless a scheduled
        start waits (a start by hand or by a deploy then adds to it).
    requested_at
        When it was first requested (a later request that merges into it does not change this).
    requested_by
        Who asked, if that principal still exists; `None` for a scheduled start.
    scheduled_for
        For a scheduled start, the time the schedule named.
    missed
        How many earlier scheduled times the start stands in for.
    merged
        How many starts it stands for: 1 for one request, more as later ones merged into it.
    """

    job: str
    environment: str
    waits_for_run_id: uuid.UUID
    trigger: str
    requested_at: datetime
    requested_by: str | None
    scheduled_for: datetime | None
    missed: int
    merged: int

    @property
    def message(self) -> str:
        """Say what the start waits for.

        Returns
        -------
        str
            Two sentences, for a person: what it waits for, and how to follow it.
        """
        return (
            f"`{self.job}` waits for run {self.waits_for_run_id} to finish; it starts then. "
            f"Follow that run with `lakana runs show {self.waits_for_run_id} --env "
            f"{self.environment}`; `lakana job list --env {self.environment}` shows this job."
        )


@dataclass(frozen=True)
class BookedFire:
    """A scheduled time that was consumed and now waits to be started.

    Parameters
    ----------
    fire_id
        The `JobFire` row that holds the time.
    job
        The job.
    environment
        Environment name.
    repository
        Repository name.
    scheduled_for
        The latest scheduled time that was due.
    missed
        How many older scheduled times it stands in for.
    environment_id
        The environment's id.
    repository_id
        The repository's id.
    schedule_id
        The job's schedule row.
    """

    schedule_id: uuid.UUID
    fire_id: uuid.UUID
    job: str
    environment: str
    repository: str
    scheduled_for: datetime
    missed: int
    environment_id: uuid.UUID
    repository_id: uuid.UUID


@dataclass(frozen=True)
class StartResult:
    """What a start of a job did.

    Parameters
    ----------
    run
        The run that was queued, or `None` when the start waits.
    waiting
        The waiting start, or `None` when a run was queued.
    """

    run: RunView | None
    waiting: WaitingStart | None


@dataclass(frozen=True)
class JobView:
    """One job, with the platform's state for it.

    Parameters
    ----------
    name
        The job's name.
    environment
        Environment name.
    repository
        Repository name.
    select
        The selection's patterns.
    upstream
        Whether ancestors are added.
    downstream
        Whether descendants are added.
    cron
        The cron expression, or `None` for a job without a schedule here.
    timezone
        The schedule's IANA zone, or `None` without a schedule.
    state
        ``scheduled``, ``paused``, ``waiting``, ``waiting to start``, ``no schedule`` or
        ``no run principal`` or ``run principal unusable``.
    paused
        Whether the schedule is paused (also when ``waiting`` is shown).
    next_run_at
        The next scheduled time (UTC), or `None`: none for a paused job, which will not fire.
    next_run_local
        The same moment in the job's zone, ISO 8601 with the offset; `None` with it.
    waiting_for
        Why a start waits (`job_waits`) or why the schedule cannot start (`no_run_principal`).
    waits_for_run_id
        For ``waiting``, the run it waits for.
    last_run_id
        The job's latest run, if any.
    last_run_status
        Its status.
    last_run_trigger
        What started it.
    last_run_at
        When it was created.
    on_deploy
        Whether the job starts after each deploy that changes the project version here.
    """

    name: str
    environment: str
    repository: str
    select: tuple[str, ...]
    upstream: bool
    downstream: bool
    cron: str | None
    timezone: str | None
    state: str
    paused: bool
    next_run_at: datetime | None
    next_run_local: str | None
    waiting_for: WaitReason | None
    waits_for_run_id: uuid.UUID | None
    last_run_id: uuid.UUID | None
    last_run_status: str | None
    last_run_trigger: str | None
    last_run_at: datetime | None
    on_deploy: bool = False


PROBLEM_NONE = "none"
PROBLEM_UNUSABLE = "unusable"
"""What `run_principal_problem` answers: nothing is bound, or the bound principal cannot run."""


def run_principal_problem(session: Session, env_id: uuid.UUID, repo_id: uuid.UUID) -> str | None:
    """Say why a scheduled run of a deployment cannot be started as its bound principal.

    The binding is read from the database, so inside a transaction that holds the deployment's
    pointer it is the value the start decided on.

    Parameters
    ----------
    session
        Database session.
    env_id
        The environment.
    repo_id
        The repository.

    Returns
    -------
    str or None
        `PROBLEM_NONE` when no principal is bound, `PROBLEM_UNUSABLE` when the bound one no
        longer exists, is inactive or is not a service account, else `None`.
    """
    bound = session.execute(
        select(EnvironmentDeployment.run_as_principal_id).where(
            EnvironmentDeployment.environment_id == env_id,
            EnvironmentDeployment.repository_id == repo_id,
        )
    ).scalar_one_or_none()
    if bound is None:
        return PROBLEM_NONE
    principal = session.get(Principal, bound)
    if (
        principal is None
        or not principal.is_active
        or principal.type is not PrincipalType.SERVICE_ACCOUNT
    ):
        return PROBLEM_UNUSABLE
    return None


def _requested_at(pending: Mapping[str, Any]) -> datetime:
    """Read when a waiting start was requested, for the view.

    Parameters
    ----------
    pending
        The stored ``pending_start``.

    Returns
    -------
    datetime
        The time; now when it is missing or unreadable, so that a damaged row cannot turn a
        park or a list into an error.
    """
    return _readable(pending.get("requested_at")) or datetime.now(UTC)


def _readable(text: object) -> datetime | None:
    """Read a stored time for a view, or `None` when it cannot be read.

    Parameters
    ----------
    text
        The stored ISO 8601 text.

    Returns
    -------
    datetime or None
        The time, or `None` when it is missing or damaged.
    """
    try:
        return _aware(text)
    except (ValueError, TypeError):
        return None


def _aware(text: object) -> datetime | None:
    """Read an ISO 8601 time from a stored waiting start.

    Parameters
    ----------
    text
        The stored value.

    Returns
    -------
    datetime or None
        The aware time, or `None` when there is none.
    """
    if not isinstance(text, str):
        return None
    value = datetime.fromisoformat(text)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class JobService:
    """Jobs of the repositories deployed in an environment.

    Parameters
    ----------
    session
        Database session; mutating methods commit.
    authz
        Authorization service.
    caps
        Operator maximums applied to each task's policy; `RunCaps()` when `None`.
    lease_seconds
        `LAKANA_RUN__LEASE_SECONDS`, as for `RunService`.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        *,
        caps: RunCaps | None = None,
        lease_seconds: int = 60,
    ) -> None:
        self._session = session
        self._authz = authz
        self._runs = RunService(session, authz, caps=caps, lease_seconds=lease_seconds)

    # -- rows --------------------------------------------------------------

    def _active_run(self, env_id: uuid.UUID, repo_id: uuid.UUID) -> uuid.UUID | None:
        """Find the active run of a repository in an environment.

        Parameters
        ----------
        env_id
            The environment.
        repo_id
            The repository.

        Returns
        -------
        uuid.UUID or None
            The run, or `None` when the project is free.
        """
        return self._session.execute(
            select(Run.id).where(
                Run.environment_id == env_id,
                Run.repository_id == repo_id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
            )
        ).scalar_one_or_none()

    def _lock_job(
        self, env: Environment, repo: Repository, job: str, *, removed: bool = False
    ) -> tuple[Mapping[str, Any], JobSchedule]:
        """Lock the deployment's pointer, read the job from its manifest, lock its row.

        The order of a deploy (pointer first, rows after), so that none of them can deadlock with
        a deploy, and the manifest is read only after the pointer is ours: a job that a deploy
        removed is not found, and a row is never created here, because only a deploy creates
        them. If the pointer moves while the lock is awaited, it is read again.

        Parameters
        ----------
        env
            The environment.
        repo
            The repository.
        job
            The job's name.
        removed
            The caller knew the job a moment ago, so a job that is gone now was removed by a
            deploy meanwhile, and the 404 says so.

        Returns
        -------
        tuple
            The job's entry in the current manifest and its schedule row, locked ``FOR UPDATE``.
            The pointer stays locked until the caller commits or rolls back.

        Raises
        ------
        NotFoundError
            If the repository is not deployed there, or the job is not in its current manifest
            or has no row.
        DeploymentMoved
            If the pointer kept moving.
        """
        env_id, repo_id, env_name, repo_name = env.id, repo.id, env.name, repo.name
        attempt = 0
        while True:
            attempt += 1
            pointer = deployment_row(self._session, env, repo)
            version_id = pointer.current_job_version_id
            try:
                DeployService(self._session, self._authz).lock_deployment(
                    env_id, repo_id, version_id
                )
            except DeploymentMoved:
                if attempt >= _ATTEMPTS:
                    raise
                continue
            version = self._session.get(JobVersion, version_id)
            manifest = version.manifest_json if version is not None else {}
            entry = manifest_jobs(manifest).get(job)
            row = (
                None
                if entry is None
                else self._session.execute(
                    select(JobSchedule)
                    .where(
                        JobSchedule.environment_id == env_id,
                        JobSchedule.repository_id == repo_id,
                        JobSchedule.job == job,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).scalar_one_or_none()
            )
            if entry is None or row is None:
                self._session.rollback()
                if removed or entry is not None:
                    raise NotFoundError(
                        f"Job `{job}` was removed from `{repo_name}` in `{env_name}` by a deploy "
                        f"meanwhile. `lakana job list --env {env_name}` shows the jobs it has "
                        "now; to run its assets anyway, use `lakana run --select`."
                    )
                raise NotFoundError(job_missing_message(manifest, job, repo_name, env_name))
            return entry, row

    # -- start -------------------------------------------------------------

    def start(
        self,
        p: PrincipalRef | None,
        environment: str,
        repository: str,
        job: str,
        *,
        trigger: RunTrigger | None = None,
        scheduled_for: datetime | None = None,
        missed: int = 0,
    ) -> StartResult:
        """Start a job now, or wait for the active run of its project to end.

        The authorization, the run principal, the selection and the checks are those of
        `RunService.start`. A start by hand also works on a paused job. Only one start waits per
        job: another start while one waits merges into it (the count of `merged` goes up), and
        parking a start is audited (`JOB_START_WAITING`).

        Parameters
        ----------
        p
            The caller; `None` exactly for the trigger ``schedule``.
        environment
            Environment name.
        repository
            Repository name.
        job
            The job's name.
        trigger
            ``schedule`` for the scheduler, ``deploy`` for a start a deploy makes as its
            deployer (checked for `RUN` like any start with a caller). Left out, it follows the
            caller: ``api`` for a service account, ``manual`` otherwise.
        scheduled_for
            For a scheduled start, the time the schedule named.
        missed
            For a scheduled start, how many earlier scheduled times it stands in for.

        Returns
        -------
        StartResult
            The queued run, or the waiting start and the run it waits for.

        Raises
        ------
        PermissionDenied
            Without RUN on the environment (checked first; not for the trigger ``schedule``).
        NotFoundError
            If the environment, repository or job does not exist or is not deployed, or a
            deploy removed the job meanwhile; the message lists the jobs that exist.
        ConflictError
            No run principal in a protected environment (or for a scheduled start), an inactive
            one, or the project kept changing while the start was recorded.
        ValidationError
            If the job's selection matches nothing in this project version.
        TypeError
            If a caller carries the trigger ``schedule`` (it would skip the `RUN` check), the
            trigger ``schedule`` has a caller, or there is neither trigger nor caller.
        """
        if trigger is None:
            if p is None:
                raise TypeError("a start without a caller must have the trigger `schedule`")
            trigger = trigger_of(p)
        if (p is None) != (trigger is RunTrigger.SCHEDULE):
            raise TypeError(
                "the trigger `schedule` is for a start with no caller, and a start with a caller "
                "must not have it: a caller is checked for `RUN`, the schedule is not"
            )
        for _ in range(_ATTEMPTS):
            try:
                run = self._runs.start_job(
                    p,
                    environment,
                    repository,
                    job,
                    trigger=trigger,
                    scheduled_for=scheduled_for,
                    missed=missed,
                )
                return StartResult(run=run, waiting=None)
            except DeploymentMoved:
                continue
            except ActiveRunError:
                waiting = self._park(
                    p, environment, repository, job, trigger, scheduled_for, missed
                )
                if waiting is not None:
                    return StartResult(run=None, waiting=waiting)
        raise ConflictError(
            f"The runs of {repository!r} in {environment!r} kept changing while the start of "
            f"{job!r} was recorded; start it again."
        )

    def _park(
        self,
        p: PrincipalRef | None,
        environment: str,
        repository: str,
        job: str,
        trigger: RunTrigger,
        scheduled_for: datetime | None,
        missed: int,
    ) -> WaitingStart | None:
        """Record a start as the waiting start of its job, merging into one that waits.

        Takes the locks in a deploy's order (`_lock_job`). A scheduled start replaces the
        waiting one and counts the scheduled time it replaces as missed; a start by hand that
        meets a waiting scheduled start only adds to its `merged`; any other start replaces the
        waiting one, so the latest requester is the one the run is attributed to. The request is
        audited in the same transaction (`JOB_START_WAITING`), with what it replaced.

        Parameters
        ----------
        p
            The caller, or `None` for a scheduled start.
        environment
            Environment name.
        repository
            Repository name.
        job
            The job.
        trigger
            What started it.
        scheduled_for
            For a scheduled start, the time the schedule named.
        missed
            For a scheduled start, how many earlier scheduled times it stands in for.

        Returns
        -------
        WaitingStart or None
            The waiting start; `None` when no run is active any more, so the caller starts again.

        Raises
        ------
        NotFoundError
            If a deploy removed the job meanwhile.
        """
        env, repo = (
            environment_by_name(self._session, environment),
            repository_by_name(self._session, repository),
        )
        env_id, repo_id = env.id, repo.id
        _, row = self._lock_job(env, repo, job, removed=True)
        active = self._active_run(env_id, repo_id)
        if active is None:
            self._session.rollback()
            return None
        now = db_now(self._session)
        requested_by = str(p.id) if p is not None else None
        fresh: dict[str, Any] = {
            "trigger": trigger.value,
            "requested_at": now.isoformat(),
            "requested_by": requested_by,
            "scheduled_for": scheduled_for.isoformat() if scheduled_for is not None else None,
            "missed": missed,
            "merged": 1,
        }
        old = row.pending_start
        replaced: dict[str, Any] | None = None
        if old is None:
            pending = fresh
        else:
            merged = int(old.get("merged", 1)) + 1
            if trigger is RunTrigger.SCHEDULE:
                earlier = int(old.get("missed", 0)) + (1 if old.get("scheduled_for") else 0)
                pending = {**fresh, "missed": missed + earlier, "merged": merged}
                replaced = {"trigger": old.get("trigger"), "requested_by": old.get("requested_by")}
            elif old.get("trigger") == RunTrigger.SCHEDULE.value:
                pending = {**old, "merged": merged}
            else:
                pending = {**fresh, "merged": merged}
                replaced = {"trigger": old.get("trigger"), "requested_by": old.get("requested_by")}
            # The wait counts from the first request: a job that is asked for often must not go
            # to the back of the queue each time, or a slower job would always go before it.
            pending = {**pending, "requested_at": old.get("requested_at", fresh["requested_at"])}
        row.pending_start = pending
        actor = p.id if p is not None else self._bound_principal(env_id, repo_id)
        AuditService(self._session).record(
            actor,
            AuditEventType.JOB_START_WAITING,
            securable=Securable.environment_scope(environment),
            detail=start_detail(
                repository,
                job,
                pending,
                waits_for_run_id=str(active),
                request_trigger=trigger.value,
                replaced=replaced,
            ),
        )
        self._session.commit()
        return self._waiting(job, environment, active, pending)

    def _bound_principal(self, env_id: uuid.UUID, repo_id: uuid.UUID) -> uuid.UUID | None:
        """Read the run principal bound to a deployment.

        Parameters
        ----------
        env_id
            The environment.
        repo_id
            The repository.

        Returns
        -------
        uuid.UUID or None
            The bound principal, or `None`.
        """
        return self._session.execute(
            select(EnvironmentDeployment.run_as_principal_id).where(
                EnvironmentDeployment.environment_id == env_id,
                EnvironmentDeployment.repository_id == repo_id,
            )
        ).scalar_one_or_none()

    def _waiting(
        self, job: str, environment: str, run_id: uuid.UUID, pending: Mapping[str, Any]
    ) -> WaitingStart:
        """Build the view of a stored waiting start.

        Parameters
        ----------
        job
            The job.
        environment
            Environment name.
        run_id
            The run it waits for.
        pending
            The stored ``pending_start``.

        Returns
        -------
        WaitingStart
            The view; the requester's name is read if that principal still exists.
        """
        by = pending.get("requested_by")
        name = None
        if isinstance(by, str):
            person = self._session.get(Principal, uuid.UUID(by))
            name = person.name if person is not None else None
        return WaitingStart(
            job=job,
            environment=environment,
            waits_for_run_id=run_id,
            trigger=str(pending["trigger"]),
            requested_at=_requested_at(pending),
            requested_by=name,
            scheduled_for=_readable(pending.get("scheduled_for")),
            missed=int(pending.get("missed", 0)),
            merged=int(pending.get("merged", 1)),
        )

    @classmethod
    def book_fire(
        cls, session: Session, schedule_id: uuid.UUID, now: datetime
    ) -> BookedFire | None:
        """Consume the due scheduled time of a job, in a transaction of its own.

        The first half of a scheduled start. It takes the locks in a deploy's order (the
        deployment's pointer, then the schedule row), checks again under them that the job is
        still in the current project version with a schedule, is not paused and is due, and then
        records the fire: the `job_fires` row, `last_fired_for` and the next `next_fire_at`. The
        time is consumed whatever happens to the start afterwards, which is a transaction of its
        own, so that the row is not held while the run is created (a deploy would deadlock with
        it). The window of times starts at `next_fire_at`, so a time that was consumed without a
        run (no run principal) is never counted as missed later.

        Parameters
        ----------
        session
            Database session; commits when it books or moves a stale time on.
        schedule_id
            The job's schedule row.
        now
            The database's current time.

        Returns
        -------
        BookedFire or None
            The fire to start, or `None` when nothing is due any more (a deploy removed the job,
            it was paused, another leader booked it first, or its stored time named no fire).

        Raises
        ------
        DeploymentMoved
            If the deployment's pointer kept moving while its lock was awaited; the caller
            leaves that project for the next tick.
        """
        row = session.get(JobSchedule, schedule_id)
        if row is None:
            return None
        env = session.execute(
            select(Environment).where(Environment.id == row.environment_id)
        ).scalar_one()
        repo = session.execute(
            select(Repository).where(Repository.id == row.repository_id)
        ).scalar_one()
        env_id, repo_id, env_name, repo_name, job = env.id, repo.id, env.name, repo.name, row.job
        service = cls(session, GrantAuthorization(session))
        try:
            entry, row = service._lock_job(env, repo, job)
        except NotFoundError:
            session.rollback()
            return None
        due = row.next_fire_at
        found = job_schedule(entry)
        if row.paused or found is None or due is None or due > now:
            session.rollback()
            return None
        cron, zone = found
        window = fire_times_between(cron, zone, due - timedelta(microseconds=1), now)
        if not window.times:
            row.next_fire_at = next_fire_after(cron, zone, now)
            session.commit()
            return None
        latest = window.times[-1]
        fire_id: uuid.UUID | None = uuid.uuid4()
        try:
            with session.begin_nested():
                session.add(JobFire(id=fire_id, job_schedule_id=row.id, scheduled_for=latest))
        except IntegrityError:
            # Another leader booked this time; its state is not ours to redo, but ours must move on.
            fire_id = None
        row.last_fired_for = latest
        row.next_fire_at = next_fire_after(cron, zone, latest)
        session.commit()
        if fire_id is None:
            return None
        return BookedFire(
            schedule_id=row.id,
            fire_id=fire_id,
            job=job,
            environment=env_name,
            repository=repo_name,
            scheduled_for=latest,
            missed=window.count - 1,
            environment_id=env_id,
            repository_id=repo_id,
        )

    @classmethod
    def start_waiting(
        cls,
        session: Session,
        schedule: JobSchedule,
        *,
        caps: RunCaps | None = None,
        lease_seconds: int = 60,
    ) -> Run | None:
        """Start the waiting start of a job whose project is free, after checking it again.

        For the scheduler, which calls it on every job with a waiting start. One transaction:
        the deployment's pointer, then the schedule row (a deploy's order), then the checks, then
        the run and the clearing of the start. A start by hand or through the API is checked as
        it was when requested: the requester must still exist and be active and may still `RUN`
        in the environment (`GrantAuthorization`), and the run principal is chosen again
        (`RunService.prepare_start`). A scheduled start uses only the bound principal. A start
        that fails a check is dropped, not kept: it is cleared and `JOB_START_DROPPED` records
        the reason, so one stuck start cannot hold up the others. A busy project leaves the start
        alone.

        The caller must hold no lock on the schedule row. A `ConflictError` from the insert (the
        environment's protection or the principal changed in the last instant) propagates, with
        the start still waiting; the next call tries again.

        Parameters
        ----------
        session
            Database session; commits when it starts a run or drops a start.
        schedule
            The job's schedule row.
        caps
            Operator maximums applied to each task's policy.
        lease_seconds
            As for `RunService`.

        Returns
        -------
        Run or None
            The run, or `None` when nothing waits, the project is busy or undeployed, the start
            was dropped, or another process cleared it first.
        """
        if schedule.pending_start is None:
            return None
        schedule_id, job = schedule.id, schedule.job
        env = session.execute(
            select(Environment).where(Environment.id == schedule.environment_id)
        ).scalar_one()
        repo = session.execute(
            select(Repository).where(Repository.id == schedule.repository_id)
        ).scalar_one()
        env_id, repo_id, env_name, repo_name = env.id, repo.id, env.name, repo.name
        service = cls(session, GrantAuthorization(session), caps=caps, lease_seconds=lease_seconds)
        try:
            pointer = deployment_row(session, env, repo)
            version_id = pointer.current_job_version_id
            DeployService(session, service._authz).lock_deployment(env_id, repo_id, version_id)
        except (NotFoundError, DeploymentMoved):
            session.rollback()
            return None
        row = session.execute(
            select(JobSchedule)
            .where(JobSchedule.id == schedule_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None or row.pending_start is None:
            session.rollback()
            return None
        pending = dict(row.pending_start)
        if service._active_run(env_id, repo_id) is not None:
            session.rollback()
            return None

        def drop(reason: str) -> None:
            """Clear the start and record why.

            The system drops it, so no principal is the actor; the requester stays in the detail.

            Parameters
            ----------
            reason
                One of the ``DROP_`` constants.
            """
            row.pending_start = None
            AuditService(session).record(
                None,
                AuditEventType.JOB_START_DROPPED,
                securable=Securable.environment_scope(env_name),
                detail=start_detail(repo_name, job, pending, reason=reason),
            )
            session.commit()

        try:
            trigger = RunTrigger(str(pending["trigger"]))
            merged = int(pending.get("merged", 1))
            missed = int(pending.get("missed", 0))
            scheduled_for = _aware(pending.get("scheduled_for"))
        except (KeyError, ValueError, TypeError):
            drop(DROP_UNREADABLE)
            return None
        requester: PrincipalRef | None = None
        if trigger is not RunTrigger.SCHEDULE:
            person = None
            by = pending.get("requested_by")
            if isinstance(by, str):
                try:
                    person = session.get(Principal, uuid.UUID(by))
                except ValueError:
                    person = None
            if person is None or not person.is_active:
                drop(DROP_REQUESTER_INACTIVE)
                return None
            requester = PrincipalRef(id=person.id, name=person.name, type=person.type)
            if not service._authz.permits(
                requester, Action.RUN, Securable.environment_scope(env_name)
            ):
                drop(DROP_REQUESTER_LACKS_RUN)
                return None
        try:
            prepared = service._runs.prepare_start(
                requester,
                env_name,
                repo_name,
                None,
                job=job,
                authorize=False,
                bound_only=requester is None,
            )
        except NotFoundError:
            drop(DROP_JOB_REMOVED)
            return None
        except ConflictError:
            drop(
                DROP_NO_RUN_PRINCIPAL
                if run_principal_problem(session, env_id, repo_id) == PROBLEM_NONE
                else DROP_RUN_PRINCIPAL_UNUSABLE
            )
            return None
        except ValidationError:
            drop(DROP_SELECTION_INVALID)
            return None
        run = service._runs.insert_run(
            prepared.actor,
            prepared.env,
            prepared.repo,
            prepared.version,
            prepared.choice,
            prepared.assets,
            kind=RunKind.SELECTION if prepared.selection.patterns else RunKind.FULL,
            selection=prepared.selection,
            resumed_from=None,
            job=job,
            trigger=trigger,
            scheduled_for=scheduled_for,
            missed=missed,
            merged=merged,
        )
        row.pending_start = None
        if trigger is RunTrigger.SCHEDULE and scheduled_for is not None:
            session.execute(
                update(JobFire)
                .where(
                    JobFire.job_schedule_id == schedule_id, JobFire.scheduled_for == scheduled_for
                )
                .values(run_id=run.id)
            )
        session.commit()
        return run

    def _deployer_job(
        self, p: PrincipalRef, environment: str, repository: str, job: str, what: str
    ) -> tuple[Environment, Repository, Mapping[str, Any], JobSchedule]:
        """Authorize a deployer's change of a job, then look the job up and lock its schedule.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        repository
            Repository name.
        job
            The job's name.
        what
            What is asked, for the refusal of a CI token (``pause a job``).

        Returns
        -------
        tuple of (Environment, Repository, Mapping, JobSchedule)
            The environment, the repository, the job's manifest entry and its locked schedule.

        Raises
        ------
        PermissionDenied
            For a CI token, always; otherwise without DEPLOY on the environment (checked first;
            DEPLOY is environment-scoped, so it covers every repository there).
        NotFoundError
            If the environment, repository or job does not exist or is not deployed.
        """
        refuse_ci_token(self._authz, p, environment, what)
        self._authz.authorize(p, Action.DEPLOY, Securable.environment_scope(environment))
        env, repo = (
            environment_by_name(self._session, environment),
            repository_by_name(self._session, repository),
        )
        entry, row = self._lock_job(env, repo, job)
        return env, repo, entry, row

    # -- pause and resume ----------------------------------------------------

    def pause(self, p: PrincipalRef, environment: str, repository: str, job: str) -> JobView:
        """Stop a job's schedule from firing until it is resumed.

        A waiting start is kept and still happens; a start by hand still works. Pausing a paused
        job changes nothing and records no event.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        repository
            Repository name.
        job
            The job's name.

        Returns
        -------
        JobView
            The job.

        Raises
        ------
        PermissionDenied
            For a CI token, always; otherwise without DEPLOY on the environment (checked first;
            DEPLOY is environment-scoped, so it covers every repository there).
        NotFoundError
            If the environment, repository or job does not exist or is not deployed.
        ConflictError
            If the job has no schedule here, so there is nothing to pause.
        """
        env, repo, entry, row = self._deployer_job(p, environment, repository, job, "pause a job")
        if job_schedule(entry) is None:
            self._session.rollback()
            raise ConflictError(
                f"Job {job!r} has no schedule in {environment!r}, so there is nothing to pause. "
                "Add a `schedule:` to the job in lakana.yaml and deploy it."
            )
        if not row.paused:
            row.paused = True
            row.paused_by = p.id
            row.paused_at = db_now(self._session)
            AuditService(self._session).record(
                p.id,
                AuditEventType.JOB_PAUSED,
                securable=Securable.environment_scope(environment),
                detail={"repository": repository, "job": job},
            )
        view = self._view(env, repo, job, entry, row)
        self._session.commit()
        return view

    def resume(self, p: PrincipalRef, environment: str, repository: str, job: str) -> JobView:
        """Let a paused job's schedule fire again, from now.

        The next fire time is computed from now, so the times missed while paused do not fire.
        Resuming a job that is not paused changes nothing and records no event.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        repository
            Repository name.
        job
            The job's name.

        Returns
        -------
        JobView
            The job.

        Raises
        ------
        PermissionDenied
            A CI token is always refused, before anything else; any other caller without
            DEPLOY on the environment (checked first, like pause).
        NotFoundError
            If the environment, repository or job does not exist or is not deployed.
        """
        env, repo, entry, row = self._deployer_job(p, environment, repository, job, "resume a job")
        if row.paused:
            wanted = job_schedule(entry)
            row.paused = False
            row.paused_by = None
            row.paused_at = None
            row.next_fire_at = (
                None if wanted is None else next_fire_after(*wanted, db_now(self._session))
            )
            AuditService(self._session).record(
                p.id,
                AuditEventType.JOB_RESUMED,
                securable=Securable.environment_scope(environment),
                detail={"repository": repository, "job": job},
            )
        view = self._view(env, repo, job, entry, row)
        self._session.commit()
        return view

    # -- notify-test -------------------------------------------------------

    def notify_test(
        self, p: PrincipalRef, environment: str, repository: str, job: str
    ) -> uuid.UUID:
        """Queue a test message to a job's webhook, with clearly fake run data.

        The worker delivers it like any other notification, so the answer is in the delivery
        (`delivery`), not here. The job's `notify` is read from the current project version.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        repository
            Repository name.
        job
            The job's name.

        Returns
        -------
        uuid.UUID
            The delivery's id.

        Raises
        ------
        PermissionDenied
            For a CI token, always; otherwise without DEPLOY on the environment (checked first,
            like pause and resume).
        NotFoundError
            If the environment, repository or job does not exist or is not deployed; the
            message lists the jobs that exist.
        ConflictError
            If the job has no usable `notify:` in the environment (none, or a damaged one).
        """
        env, repo, entry, _ = self._deployer_job(
            p, environment, repository, job, "send a test message"
        )
        notify = job_notify({"jobs": {job: entry}}, job)
        if notify is None:
            self._session.rollback()
            raise ConflictError(
                f"Job `{job}` has no usable `notify:` in `{environment}`; "
                "add or fix it in lakana.yaml and deploy."
            )
        row = new_delivery(
            notify,
            "test",
            db_now(self._session),
            run_id=None,
            environment_id=env.id,
            repository_id=repo.id,
            job=job,
        )
        self._session.add(row)
        AuditService(self._session).record(
            p.id,
            AuditEventType.NOTIFICATION_TESTED,
            securable=Securable.environment_scope(environment),
            detail={
                "repository": repository,
                "job": job,
                "environment": environment,
                "delivery_id": str(row.id),
            },
        )
        self._session.commit()
        return row.id

    def delivery(self, p: PrincipalRef, environment: str, delivery_id: uuid.UUID) -> DeliveryView:
        """Show one delivery, so that a test can be waited for.

        A test delivery belongs to no run, so the right to read runs does not cover it by
        itself: whoever may test (DEPLOY) may read it, and so may whoever may read a run.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        delivery_id
            The delivery.

        Returns
        -------
        DeliveryView
            Its state, attempts and short result.

        Raises
        ------
        PermissionDenied
            For a CI token, always; otherwise with none of DEPLOY, RUN and READ_ENVIRONMENT on
            the environment.
        NotFoundError
            If the delivery does not exist in this environment.
        """
        refuse_ci_token(self._authz, p, environment, "read a notification delivery")
        scope = Securable.environment_scope(environment)
        if self._authz.permits(p, Action.DEPLOY, scope):
            self._authz.authorize(p, Action.DEPLOY, scope)
            env = environment_by_name(self._session, environment)
        else:
            env = authorize_run_read(self._session, self._authz, p, environment)
        row = self._session.get(NotificationDelivery, delivery_id)
        if row is None or row.environment_id != env.id:
            raise NotFoundError(
                f"There is no delivery {delivery_id} in `{environment}`. A delivery id comes "
                "from `lakana job notify-test` or from `lakana runs show`."
            )
        repo = self._session.get(Repository, row.repository_id)
        return delivery_view(row, repo.name if repo is not None else "")

    # -- views -------------------------------------------------------------

    def _view(
        self,
        env: Environment,
        repo: Repository,
        job: str,
        entry: Mapping[str, Any],
        row: JobSchedule | None,
    ) -> JobView:
        """Build a job's view from its manifest entry and its row.

        Parameters
        ----------
        env
            The environment.
        repo
            The repository.
        job
            The job.
        entry
            Its entry in the current manifest.
        row
            Its schedule row, if it has one.

        Returns
        -------
        JobView
            The view.
        """
        wanted = job_schedule(entry)
        paused = row is not None and row.paused
        pending = row.pending_start if row is not None else None
        problem = run_principal_problem(self._session, env.id, repo.id)
        active = self._active_run(env.id, repo.id)
        waits_for = active if pending is not None else None
        reason: WaitReason | None = None
        if pending is not None and active is not None:
            state = STATE_WAITING
            reason = WaitReason(code=JOB_WAITS, message=JOB_WAITS_MESSAGE.format(run=active))
        elif pending is not None:
            state = STATE_STARTING
            reason = WaitReason(code=JOB_STARTS_SOON, message=JOB_STARTS_SOON_MESSAGE)
        elif paused:
            state = STATE_PAUSED
        elif wanted is None:
            state = STATE_NO_SCHEDULE
        elif problem == PROBLEM_NONE:
            state = STATE_NO_RUN_PRINCIPAL
            reason = WaitReason(code=NO_RUN_PRINCIPAL, message=NO_RUN_PRINCIPAL_JOB_MESSAGE)
        elif problem == PROBLEM_UNUSABLE:
            state = STATE_RUN_PRINCIPAL_UNUSABLE
            reason = WaitReason(
                code=RUN_PRINCIPAL_UNUSABLE, message=RUN_PRINCIPAL_UNUSABLE_JOB_MESSAGE
            )
        else:
            state = STATE_SCHEDULED
        next_at = (
            row.next_fire_at if row is not None and wanted is not None and not paused else None
        )
        local = None
        if next_at is not None and wanted is not None:
            local = next_at.astimezone(load_zone(wanted[1])).isoformat()
        last = self._session.execute(
            select(Run.id, Run.status, Run.trigger, Run.created_at)
            .where(
                Run.environment_id == env.id,
                Run.repository_id == repo.id,
                Run.job == job,
            )
            .order_by(Run.created_at.desc(), Run.id)
            .limit(1)
        ).first()
        patterns = entry.get("select")
        return JobView(
            name=job,
            environment=env.name,
            repository=repo.name,
            select=tuple(x for x in patterns if isinstance(x, str))
            if isinstance(patterns, list)
            else (),
            upstream=entry.get("upstream") is True,
            downstream=entry.get("downstream") is True,
            cron=wanted[0] if wanted else None,
            timezone=wanted[1] if wanted else None,
            state=state,
            paused=paused,
            next_run_at=next_at,
            next_run_local=local,
            waiting_for=reason,
            waits_for_run_id=waits_for,
            last_run_id=last[0] if last else None,
            last_run_status=last[1].value if last else None,
            last_run_trigger=last[2].value if last and last[2] is not None else None,
            last_run_at=last[3] if last else None,
            on_deploy=job_on_deploy(entry),
        )

    def list(
        self, p: PrincipalRef, environment: str, repository: str | None = None
    ) -> Sequence[JobView]:
        """List the jobs of the deployed repositories of an environment.

        Filtered as the deploy views are: anyone who may read the environment sees every job in it.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        repository
            Only this repository's jobs.

        Returns
        -------
        Sequence of JobView
            By repository name, then job name.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT.
        NotFoundError
            If the environment (or the repository asked for) does not exist.
        """
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment))
        env = environment_by_name(self._session, environment)
        query = (
            select(Repository, JobVersion)
            .join(EnvironmentDeployment, EnvironmentDeployment.repository_id == Repository.id)
            .join(JobVersion, JobVersion.id == EnvironmentDeployment.current_job_version_id)
            .where(EnvironmentDeployment.environment_id == env.id)
            .order_by(Repository.name)
        )
        if repository is not None:
            query = query.where(Repository.id == repository_by_name(self._session, repository).id)
        rows = {
            (row.repository_id, row.job): row
            for row in self._session.scalars(
                select(JobSchedule)
                .where(JobSchedule.environment_id == env.id)
                .execution_options(populate_existing=True)
            )
        }
        views: list[JobView] = []
        for repo, version in self._session.execute(query).all():
            for name, entry in sorted(manifest_jobs(version.manifest_json).items()):
                views.append(self._view(env, repo, name, entry, rows.get((repo.id, name))))
        return views

    def get(
        self, p: PrincipalRef, environment: str, job: str, repository: str | None = None
    ) -> JobView:
        """Show one job of the deployed repositories of an environment.

        Parameters
        ----------
        p
            The caller.
        environment
            Environment name.
        job
            The job's name.
        repository
            The repository; needed only when several repositories declare a job of this name.

        Returns
        -------
        JobView
            The job, as `list` shows it.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT.
        NotFoundError
            If the environment or repository does not exist, or no deployed repository declares
            the job; the message lists the jobs that exist.
        ConflictError
            If several repositories declare the job and none was named.
        """
        views = self.list(p, environment, repository)
        found = [view for view in views if view.name == job]
        if not found:
            names = sorted({view.name for view in views})
            where = f"in {repository!r} in {environment!r}" if repository else f"in {environment!r}"
            listing = f"Its jobs: {', '.join(names)}." if names else "No job is deployed there."
            raise NotFoundError(f"No job {job!r} {where}. {listing}")
        if len(found) > 1:
            repos = ", ".join(sorted(view.repository for view in found))
            raise ConflictError(
                f"Several repositories declare job {job!r} in {environment!r}: {repos}. "
                "Name one with the `repository` parameter (`--repo` on the command line)."
            )
        return found[0]
