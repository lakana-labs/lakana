"""Jobs of the platform API: list them, start one, pause and resume its schedule.

The server records and reads; the scheduler of the worker fires the schedules. A start by hand
or through the API goes through the same core code as `POST .../runs`, with the same permission
(`RUN`), but a busy project makes it wait (202) instead of refusing it (409).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response

from lakana_core.auth import PrincipalRef
from lakana_core.jobs.service import JobService, JobView, StartResult, WaitingStart
from lakana_core.notifications.views import DeliveryView
from lakana_server.api.platform.runs import run_out
from lakana_server.api.platform.schemas import (
    DeliveryOut,
    DeployTriggerOut,
    JobOut,
    JobRunRequest,
    JobStartOut,
    NotifyTestOut,
    OnDemandTriggerOut,
    ScheduleTriggerOut,
    WaitingStartOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_job_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}",
    tags=["jobs"],
    responses=errors(401),
)

_JOB = Annotated[str, Path(min_length=1, max_length=255)]


def _job_out(view: JobView) -> JobOut:
    """Build a job body, with the reason as `WaitReason.to_json` writes it.

    Parameters
    ----------
    view
        The job.

    Returns
    -------
    JobOut
        The body.
    """
    fields = dataclasses.asdict(view)
    del fields["on_deploy"]
    fields["select"] = list(view.select)
    fields["waiting_for"] = None if view.waiting_for is None else view.waiting_for.to_json()
    triggers: list[ScheduleTriggerOut | DeployTriggerOut | OnDemandTriggerOut] = []
    if view.cron is not None and view.timezone is not None:
        triggers.append(
            ScheduleTriggerOut(
                cron=view.cron,
                timezone=view.timezone,
                paused=view.paused,
                next_run_at=None if view.paused else view.next_run_at,
            )
        )
    if view.on_deploy:
        triggers.append(DeployTriggerOut())
    triggers.append(OnDemandTriggerOut())
    return JobOut(**fields, triggers=triggers)


def _waiting_out(waiting: WaitingStart) -> WaitingStartOut:
    """Build a waiting-start body.

    Parameters
    ----------
    waiting
        The waiting start.

    Returns
    -------
    WaitingStartOut
        The body, with its sentence.
    """
    return WaitingStartOut(**dataclasses.asdict(waiting), message=waiting.message)


def _start_out(result: StartResult) -> JobStartOut:
    """Build a start body.

    Parameters
    ----------
    result
        What the start did.

    Returns
    -------
    JobStartOut
        The run, or the waiting start.
    """
    return JobStartOut(
        run=None if result.run is None else run_out(result.run),
        waiting=None if result.waiting is None else _waiting_out(result.waiting),
    )


@router.get("/jobs", response_model=list[JobOut], responses=errors(403, 404))
def list_jobs(
    env: str,
    repository: str | None = Query(default=None, max_length=255),
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> list[JobOut]:
    """List jobs.

    Lists the jobs of the repositories deployed in the environment. Needs
    `READ_ENVIRONMENT`. Answers 404 when the environment or the repository asked for does
    not exist.

    Parameters
    ----------
    env
        Environment name.
    repository
        Only this repository's jobs.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    list of JobOut
        By repository, then job name.
    """
    return [_job_out(view) for view in service.list(principal, env, repository)]


@router.get("/jobs/{job}", response_model=JobOut, responses=errors(403, 404, 409))
def get_job(
    env: str,
    job: _JOB,
    repository: str | None = Query(default=None, max_length=255),
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> JobOut:
    """Show a job.

    Shows the job with its triggers. Needs `READ_ENVIRONMENT`. Answers 404 when the
    environment or repository does not exist or no deployed repository declares the job (the
    message lists the jobs that exist), and 409 when several repositories declare it and
    none was named.

    Parameters
    ----------
    env
        Environment name.
    job
        The job's name.
    repository
        The repository; needed only when several repositories declare the job.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    JobOut
        The job, as the list shows it.
    """
    return _job_out(service.get(principal, env, job, repository))


@router.post(
    "/jobs/{job}/runs",
    response_model=JobStartOut,
    status_code=201,
    responses={
        202: {"model": JobStartOut, "description": "The project is busy: the start waits."},
        **errors(403, 404, 409),
    },
)
def start_job(
    env: str,
    job: _JOB,
    body: JobRunRequest,
    response: Response,
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> JobStartOut:
    """Start a job.

    Starts a run of the job. Answers 201 with the queued run, or 202 with the waiting start
    and the run it waits for when the project is busy. A paused job can be started. Needs
    `RUN` on the environment. Answers 404 when the environment, repository or job does not
    exist or is not deployed (the message lists the jobs that exist), 409 when there is no
    run service account in a protected environment or it is inactive, and 422 when the job's
    selection matches nothing.

    Parameters
    ----------
    env
        Environment name.
    job
        The job's name.
    body
        The repository.
    response
        The response, to set 202.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    JobStartOut
        The run or the waiting start.
    """
    result = service.start(principal, env, body.repository, job)
    if result.waiting is not None:
        response.status_code = 202
    return _start_out(result)


@router.post("/jobs/{job}/pause", response_model=JobOut, responses=errors(403, 404, 409))
def pause_job(
    env: str,
    job: _JOB,
    body: JobRunRequest,
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> JobOut:
    """Pause a job's schedule.

    Needs `DEPLOY` on the environment; a CI token is always refused. Answers 404 when the
    environment, repository or job does not exist or is not deployed, and 409 when the job
    has no schedule here.

    Parameters
    ----------
    env
        Environment name.
    job
        The job's name.
    body
        The repository.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    JobOut
        The job.
    """
    return _job_out(service.pause(principal, env, body.repository, job))


@router.post("/jobs/{job}/resume", response_model=JobOut, responses=errors(403, 404))
def resume_job(
    env: str,
    job: _JOB,
    body: JobRunRequest,
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> JobOut:
    """Resume a job's schedule.

    The schedule continues from now. Needs `DEPLOY` on the environment; a CI token is always
    refused. Answers 404 when the environment, repository or job does not exist or is not
    deployed.

    Parameters
    ----------
    env
        Environment name.
    job
        The job's name.
    body
        The repository.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    JobOut
        The job.
    """
    return _job_out(service.resume(principal, env, body.repository, job))


@router.post(
    "/jobs/{job}/notify-test",
    response_model=NotifyTestOut,
    status_code=202,
    responses=errors(403, 404, 409),
)
def notify_test(
    env: str,
    job: _JOB,
    body: JobRunRequest,
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> NotifyTestOut:
    """Send a test message to a job's webhook.

    Queues a test message and answers 202 with a delivery id; a worker sends it, and the
    delivery shows how it went. Needs `DEPLOY` on the environment; a CI token is always
    refused. Answers 404 when the environment, repository or job does not exist or is not
    deployed, and 409 when the job has no usable `notify:` setting in the environment.

    Parameters
    ----------
    env
        Environment name.
    job
        The job's name.
    body
        The repository.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    NotifyTestOut
        The delivery id.
    """
    return NotifyTestOut(delivery_id=service.notify_test(principal, env, body.repository, job))


@router.get("/notifications/{delivery_id}", response_model=DeliveryOut, responses=errors(403, 404))
def get_notification(
    env: str,
    delivery_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: JobService = Depends(get_job_service),
) -> DeliveryOut:
    """Show one notification delivery.

    A test has no run, so the right to read a run does not reach it; whoever may test a
    webhook may read the result. Needs `DEPLOY`, `RUN` or `READ_ENVIRONMENT`; a CI token is
    always refused. Answers 404 when the delivery does not exist in the environment.

    Parameters
    ----------
    env
        Environment name.
    delivery_id
        The notification delivery's id, as returned when the test was queued.
    principal
        The authenticated caller.
    service
        The job service.

    Returns
    -------
    DeliveryOut
        Its state, attempts and short result.
    """
    view: DeliveryView = service.delivery(principal, env, delivery_id)
    return DeliveryOut(**vars(view))
