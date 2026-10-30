"""Start the jobs with ``on_deploy: true`` after a successful deploy, as its deployer.

The worker calls `start_jobs_after_deploy` right after `DeployService.record_deploy` committed. Each
pending job is started through `JobService.start`, the call behind
``POST .../jobs/{job}/runs``: the same `RUN` check (an auditing authorization records a refusal as
`AUTHORIZATION_DENIED`), the same run principal rules (a protected environment needs a bound
service account), the same waiting start when the project is busy and the same audit events
(`RUN_STARTED`, `JOB_START_WAITING`), with the trigger ``deploy``. There is no other path from a
deploy into the run engine. A start that is refused leaves the deploy as it is; the deploy's record
says why and what to do (`lakana_core.projects.deploy_jobs`).
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.jobs.audit import start_detail
from lakana_core.jobs.service import (
    DROP_NOT_ON_DEPLOY,
    DROP_REQUESTER_INACTIVE,
    DROP_REQUESTER_LACKS_RUN,
    JobService,
    deploy_jobs_scope,
)
from lakana_core.models import (
    AuditEventType,
    DeployRun,
    DeployStatus,
    Environment,
    Principal,
    Repository,
    RunTrigger,
)
from lakana_core.projects.deploy_jobs import (
    OUTCOME_NOT_STARTED,
    OUTCOME_PENDING,
    OUTCOME_STARTED,
    OUTCOME_WAITING,
    REASON_ERROR,
    REASON_LACKS_RUN,
    REASON_REFUSED,
    DeployJobStart,
    run_it_yourself,
)
from lakana_core.projects.run_policy import RunCaps
from lakana_core.runs.service import NotOnDeploy

logger = logging.getLogger(__name__)


def _lacks_run_message(job: str, principal: str, repository: str, environment: str) -> str:
    """Say that the deployer may not start the job, and the two ways on.

    Parameters
    ----------
    job
        The job.
    principal
        The name of the principal that deployed.
    repository
        The repository.
    environment
        The environment.

    Returns
    -------
    str
        The cause, the grant that fixes it, and the start by hand.
    """
    return (
        f"job `{job}` was not started: {principal} may not RUN in `{environment}`. Grant RUN "
        f"with `lakana grant create --principal {principal} --action RUN --kind ENVIRONMENT "
        f"--env {environment}`, or start the job yourself with "
        f"{run_it_yourself(job, repository, environment)}."
    )


def _ci_token_message(
    job: str, principal: str, repository: str, environment: str, rule_id: uuid.UUID
) -> str:
    """Say that a CI deploy's rule does not start its jobs, and the two ways on.

    A CI token may only deploy unless its rule opts in (`start_on_deploy_jobs`), whatever its
    account's grants, so granting `RUN` would not help.

    Parameters
    ----------
    job
        The job.
    principal
        The name of the token's service account.
    repository
        The repository.
    environment
        The environment.
    rule_id
        The CI trust rule the token was issued under.

    Returns
    -------
    str
        The cause, the setting that changes it, and the start by hand.
    """
    return (
        f"job `{job}` was not started: {principal} deployed with a CI token, and its CI trust "
        "rule does not start the deploy's jobs. An instance administrator can turn that on with "
        f"`lakana ci-trust update {rule_id} --start-on-deploy-jobs`; or start the job with "
        f"{run_it_yourself(job, repository, environment)} and a stored token that holds RUN."
    )


def _audit_dropped(
    session: Session, p: PrincipalRef, environment: str, repository: str, job: str, reason: str
) -> None:
    """Record that a start of the deploy's job was dropped, as a dropped waiting start is.

    The system drops it, so the event has no actor; the requester, and for a CI deploy the
    rule, are in the detail. Does not commit: the caller commits it with the job's outcome.

    Parameters
    ----------
    session
        Database session.
    p
        The deployer, with the CI scope of its token for a CI deploy.
    environment
        Environment name.
    repository
        Repository name.
    job
        The job.
    reason
        One of the ``DROP_`` constants of `lakana_core.jobs.service`.
    """
    stored: dict[str, str] = {"trigger": RunTrigger.DEPLOY.value, "requested_by": str(p.id)}
    if p.ci_scope is not None:
        stored["ci_rule_id"] = str(p.ci_scope.rule_id)
    AuditService(session).record(
        None,
        AuditEventType.JOB_START_DROPPED,
        securable=Securable.environment_scope(environment),
        detail=start_detail(repository, job, stored, reason=reason),
    )


def _start_one(
    session: Session, jobs: JobService, p: PrincipalRef, environment: str, repository: str, job: str
) -> DeployJobStart:
    """Start one job as the deployer, turning every refusal into an outcome.

    Parameters
    ----------
    session
        The job service's session, rolled back after a refusal so that the next start begins
        clean.
    jobs
        The job service.
    p
        The principal that deployed.
    environment
        Environment name.
    repository
        Repository name.
    job
        The job.

    Returns
    -------
    DeployJobStart
        The run, the waiting start, or why the job was not started.
    """
    try:
        result = jobs.start(p, environment, repository, job, trigger=RunTrigger.DEPLOY)
    except NotOnDeploy as exc:
        session.rollback()
        _audit_dropped(session, p, environment, repository, job, DROP_NOT_ON_DEPLOY)
        session.commit()
        return DeployJobStart(
            job=job,
            outcome=OUTCOME_NOT_STARTED,
            reason=REASON_REFUSED,
            message=(
                f"job `{job}` was not started: {exc}. Start it with "
                f"{run_it_yourself(job, repository, environment)} and a stored token that holds "
                "RUN."
            ),
        )
    except PermissionDenied:
        session.rollback()
        return DeployJobStart(
            job=job,
            outcome=OUTCOME_NOT_STARTED,
            reason=REASON_LACKS_RUN,
            message=(
                _ci_token_message(job, p.name, repository, environment, p.ci_scope.rule_id)
                if p.ci_scope is not None
                else _lacks_run_message(job, p.name, repository, environment)
            ),
        )
    except (ConflictError, NotFoundError, ValidationError) as exc:
        session.rollback()
        return DeployJobStart(
            job=job,
            outcome=OUTCOME_NOT_STARTED,
            reason=REASON_REFUSED,
            message=f"job `{job}` was not started: {exc}",
        )
    except Exception:
        session.rollback()
        logger.exception("Could not start job %r after a deploy of %r.", job, repository)
        return DeployJobStart(
            job=job,
            outcome=OUTCOME_NOT_STARTED,
            reason=REASON_ERROR,
            message=(
                f"job `{job}` was not started because of an internal error; the worker's log has "
                f"the details. Start it with {run_it_yourself(job, repository, environment)}."
            ),
        )
    if result.run is not None:
        return DeployJobStart(job=job, outcome=OUTCOME_STARTED, run_id=result.run.id)
    assert result.waiting is not None  # nosec B101 -- a start either queued a run or waits
    return DeployJobStart(
        job=job, outcome=OUTCOME_WAITING, waits_for_run_id=result.waiting.waits_for_run_id
    )


@dataclasses.dataclass(frozen=True)
class _Refusal:
    """Why every start the deploy has left is refused, found before one is made.

    Parameters
    ----------
    cause
        The cause, for the job's message.
    reason
        The outcome's reason (`REASON_REFUSED` or `REASON_LACKS_RUN`).
    dropped
        The reason of the `JOB_START_DROPPED` event.
    """

    cause: str
    reason: str
    dropped: str


_RULE_GONE = (
    "the CI trust rule of the deploy's token was changed, disabled or deleted since the deploy "
    "began, or no longer covers this repository"
)


def _recheck(
    session: Session,
    p: PrincipalRef,
    ci_token_id: uuid.UUID | None,
    repository_id: uuid.UUID,
) -> tuple[PrincipalRef, _Refusal | None]:
    """Check the deployer again right before one start: still active, and its rule still allows.

    Parameters
    ----------
    session
        Database session; reads only.
    p
        The deployer, with the CI scope resolved when the deploy was claimed, if any.
    ci_token_id
        The CI token that queued the deploy, if any.
    repository_id
        The deployed repository.

    Returns
    -------
    tuple of (PrincipalRef, _Refusal or None)
        The principal to start the job as (for a CI deploy, with the scope read now, never the
        one resolved when the deploy was claimed), and why the start is refused, or `None`.
    """
    active = session.execute(select(Principal.is_active).where(Principal.id == p.id)).scalar()
    if not active:
        return p, _Refusal(f"{p.name} is no longer active", REASON_REFUSED, DROP_REQUESTER_INACTIVE)
    if p.ci_scope is None:
        return p, None
    # The one place a CI scope asks for more than DEPLOY: its rule's opt-in decides.
    scope = deploy_jobs_scope(session, ci_token_id, p.id, repository_id)
    if scope is None:
        return p, _Refusal(_RULE_GONE, REASON_LACKS_RUN, DROP_REQUESTER_LACKS_RUN)
    return dataclasses.replace(p, ci_scope=scope), None


def _write_outcome(session: Session, run_id: uuid.UUID, outcome: DeployJobStart) -> None:
    """Replace a job's pending entry on the deploy run with what came of its start.

    Parameters
    ----------
    session
        Database session; commits.
    run_id
        The deploy run.
    outcome
        What came of the start.
    """
    run = session.execute(
        select(DeployRun)
        .where(DeployRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if run is None or not isinstance(run.job_starts_json, list):
        session.rollback()
        return
    run.job_starts_json = [
        outcome.to_json() if isinstance(entry, dict) and entry.get("job") == outcome.job else entry
        for entry in run.job_starts_json
    ]
    session.commit()


def start_jobs_after_deploy(
    session: Session,
    authz: AuthorizationService,
    run_id: uuid.UUID,
    p: PrincipalRef,
    *,
    caps: RunCaps | None = None,
) -> Sequence[DeployJobStart]:
    """Start each job a successful deploy left pending, as the principal that deployed.

    Never raises for a start: a refusal or an error becomes the job's outcome, and the next job
    is started all the same. Each outcome is written to the deploy run as soon as it is known; one
    that cannot be written is logged and its entry stays ``pending`` (shown as lost once the run
    lease has passed). Only an entry still ``pending`` is started, so a second call starts nothing
    twice. Right before each start the deployer is checked again: one deactivated since the
    deploy was claimed starts nothing, also where the run would write as a bound service
    account, and for a CI deploy the trust rule is read again, so a rule changed while the jobs
    start stops the jobs not started yet. Such a refusal is recorded as `JOB_START_DROPPED`
    with its reason, as a dropped waiting start is.

    Parameters
    ----------
    session
        Database session; each start and each outcome commits.
    authz
        The worker's authorization service, the auditing one in production.
    run_id
        The deploy run, after `DeployService.record_deploy` committed it.
    p
        The principal that deployed, with the scope of the CI token that queued the deploy, if any:
        each start is authorized with the rights the deploy request had. A CI token may start
        the jobs only when its trust rule opts in (`start_on_deploy_jobs`); the start is then
        allowed by the rule, not by the account's grants, and its audit event names the rule.
    caps
        The operator's maximums for each task's policy; `RunCaps()` when `None`.

    Returns
    -------
    sequence of DeployJobStart
        What came of each start made, by job name; empty when the deploy failed, started no job,
        or its starts were made already.
    """
    run = session.get(DeployRun, run_id)
    if run is None or run.status is not DeployStatus.SUCCEEDED:
        return []
    pending = [
        entry["job"]
        for entry in run.job_starts_json or []
        if isinstance(entry, dict)
        and entry.get("outcome") == OUTCOME_PENDING
        and isinstance(entry.get("job"), str)
    ]
    if not pending:
        return []
    environment = session.execute(
        select(Environment.name).where(Environment.id == run.environment_id)
    ).scalar_one()
    repository = session.execute(
        select(Repository.name).where(Repository.id == run.repository_id)
    ).scalar_one()
    ci_token_id, repository_id = run.ci_token_id, run.repository_id
    session.commit()
    jobs = JobService(session, authz, caps=caps)
    outcomes: list[DeployJobStart] = []
    for job in pending:
        actor, refusal = _recheck(session, p, ci_token_id, repository_id)
        session.commit()
        outcome = (
            _start_one(session, jobs, actor, environment, repository, job)
            if refusal is None
            else DeployJobStart(
                job=job,
                outcome=OUTCOME_NOT_STARTED,
                reason=refusal.reason,
                message=(
                    f"job `{job}` was not started: {refusal.cause}. Start it with "
                    f"{run_it_yourself(job, repository, environment)}."
                ),
            )
        )
        outcomes.append(outcome)
        try:
            if refusal is not None:
                _audit_dropped(session, p, environment, repository, job, refusal.dropped)
            _write_outcome(session, run_id, outcome)
        except Exception:
            session.rollback()
            logger.warning(
                "Deploy %s: could not record the outcome %r of the start of job %r.",
                run_id,
                outcome.outcome,
                job,
                exc_info=True,
            )
    return outcomes
