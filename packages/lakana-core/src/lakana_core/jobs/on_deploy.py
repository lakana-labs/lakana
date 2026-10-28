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

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AuthorizationService, PrincipalRef
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.jobs.service import JobService
from lakana_core.models import (
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
    except PermissionDenied:
        session.rollback()
        return DeployJobStart(
            job=job,
            outcome=OUTCOME_NOT_STARTED,
            reason=REASON_LACKS_RUN,
            message=_lacks_run_message(job, p.name, repository, environment),
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
    twice. A deployer deactivated since the deploy was claimed starts nothing, also where the run
    would write as a bound service account, as a waiting start of a deactivated requester is
    dropped.

    Parameters
    ----------
    session
        Database session; each start and each outcome commits.
    authz
        The worker's authorization service, the auditing one in production.
    run_id
        The deploy run, after `DeployService.record_deploy` committed it.
    p
        The principal that deployed (without a CI token's scope: the deploy's table checks use the
        account the same way).
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
    deployer = session.get(Principal, p.id)
    active = deployer is not None and deployer.is_active
    session.commit()
    jobs = JobService(session, authz, caps=caps)
    outcomes: list[DeployJobStart] = []
    for job in pending:
        outcome = (
            _start_one(session, jobs, p, environment, repository, job)
            if active
            else DeployJobStart(
                job=job,
                outcome=OUTCOME_NOT_STARTED,
                reason=REASON_REFUSED,
                message=(
                    f"job `{job}` was not started: {p.name} is no longer active. Start it with "
                    f"{run_it_yourself(job, repository, environment)}."
                ),
            )
        )
        outcomes.append(outcome)
        try:
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
