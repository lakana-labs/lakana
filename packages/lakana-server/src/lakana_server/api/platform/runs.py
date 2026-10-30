"""Runs and the scheduler status of the platform API.

The server creates runs and reads them; the worker executes them (rule 2).
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from lakana_core.auth import AuthorizationService, PrincipalRef
from lakana_core.runs.check_rows import submit_check_rows
from lakana_core.runs.checks import CheckResultView
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService, RunView, TaskLogView, TaskRunView
from lakana_server.api.platform.listing import set_next_cursor, split_values
from lakana_server.api.platform.schemas import (
    CheckResultOut,
    CheckResultPage,
    CheckRowsRequest,
    NotificationOut,
    QuerySubmitOut,
    RunCreate,
    RunOut,
    SchedulerOut,
    TaskAttemptOut,
    TaskLogOut,
    TaskRunOut,
    WorkerCachesOut,
    WorkerOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_grant_authorization, get_principal, get_run_service, get_session

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}",
    tags=["runs"],
    responses=errors(401),
)
scheduler_router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1", tags=["runs"], responses=errors(401)
)


def _task_out(task: TaskRunView) -> TaskRunOut:
    """Build a task body, with the reason as `WaitReason.to_json` writes it.

    Parameters
    ----------
    task
        The task.

    Returns
    -------
    TaskRunOut
        The body. `waiting_for` holds only the fields its code uses, which
        `dataclasses.asdict` would not give.
    """
    fields = dataclasses.asdict(task)
    fields["waiting_for"] = None if task.waiting_for is None else task.waiting_for.to_json()
    return TaskRunOut(**fields)


def run_out(view: RunView) -> RunOut:
    """Build a run body.

    Parameters
    ----------
    view
        The run.

    Returns
    -------
    RunOut
        The body.
    """
    fields = dataclasses.asdict(view)
    fields["project_version_id"] = fields.pop("job_version_id")
    fields["tasks"] = [_task_out(task) for task in view.tasks]
    fields["notifications"] = [NotificationOut(**vars(n)) for n in view.notifications]
    return RunOut(**fields)


def _log_out(view: TaskLogView) -> TaskLogOut:
    """Build a task-log body.

    Parameters
    ----------
    view
        The log.

    Returns
    -------
    TaskLogOut
        The body.
    """
    return TaskLogOut(
        run_id=view.run_id,
        asset=view.asset,
        text=view.text,
        attempts=[TaskAttemptOut(**dataclasses.asdict(a)) for a in view.attempts],
        next_offset=view.next_offset,
        more=view.more,
        complete=view.complete,
        reset=view.reset,
    )


def _check_out(view: CheckResultView) -> CheckResultOut:
    """Build a check-result body.

    Parameters
    ----------
    view
        The result.

    Returns
    -------
    CheckResultOut
        The body.
    """
    return CheckResultOut(**dataclasses.asdict(view))


@router.post("/runs", response_model=RunOut, status_code=202, responses=errors(403, 404, 409))
def start_run(
    env: str,
    body: RunCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> RunOut:
    """Start a run.

    Starts a run of the repository's deployed project version and answers 202. Needs `RUN`
    on the environment. Answers 404 when the environment or repository does not exist or is
    not deployed, 409 when another run of the deployment is active, there is no run service
    account in a protected environment, or the one bound is inactive, and 422 when the
    selection is malformed or matches nothing.

    Parameters
    ----------
    env
        Environment name.
    body
        Repository and selection.
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    RunOut
        The queued run.
    """
    selection = Selection(
        patterns=tuple(body.select), upstream=body.upstream, downstream=body.downstream
    )
    return run_out(service.start(principal, env, body.repository, selection))


@router.get("/runs", response_model=list[RunOut], responses=errors(403, 404))
def list_runs(
    env: str,
    response: Response,
    repository: str | None = Query(default=None, max_length=255),
    status: list[str] = Query(default=[], max_length=16),
    trigger: list[str] = Query(default=[], max_length=16),
    job: str | None = Query(default=None, max_length=255),
    since: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> list[RunOut]:
    """List runs.

    Lists runs, newest first, one page at a time. The cursor of the next page is in the
    `X-Next-Cursor` response header and is absent on the last page; send the same filters
    with every page. Needs `READ_ENVIRONMENT`. Answers 404 when the environment or
    repository does not exist and 422 for a `limit` below 1, an unknown status or trigger, a
    `since` without a time zone, or a `cursor` this server did not issue.

    Parameters
    ----------
    env
        Environment name.
    response
        The response, to set the cursor header on.
    repository
        Only this repository's runs.
    status
        Only runs in these statuses (`queued`, `running`, `succeeded`, `failed`,
        `cancelled`); repeat the parameter or separate the values with commas.
    trigger
        Only runs started this way (`schedule`, `api`, `manual`, `deploy`); repeat or separate
        with commas.
    job
        Only runs of this job.
    since
        Only runs created at or after this ISO 8601 time, with a time zone
        (`2026-10-18T00:00:00Z`).
    limit
        Most runs returned (1 to 200).
    cursor
        The `X-Next-Cursor` of the previous page.
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    list of RunOut
        Without tasks.
    """
    views, next_cursor = service.list_runs_page(
        principal,
        env,
        repository=repository,
        status=split_values(status),
        trigger=split_values(trigger),
        job=job,
        since=since,
        limit=limit,
        cursor=cursor,
    )
    set_next_cursor(response, next_cursor)
    return [run_out(v) for v in views]


@router.get("/runs/{run_id}", response_model=RunOut, responses=errors(403, 404))
def get_run(
    env: str,
    run_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> RunOut:
    """Show a run.

    Shows the run with its tasks. Needs `RUN` or `READ_ENVIRONMENT`. Answers 404 when the
    run does not exist in the environment.

    Parameters
    ----------
    env
        Environment name.
    run_id
        The run's id.
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    RunOut
        The run.
    """
    return run_out(service.get(principal, env, run_id))


@router.get(
    "/runs/{run_id}/checks", response_model=list[CheckResultOut], responses=errors(403, 404)
)
def get_run_checks(
    env: str,
    run_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> list[CheckResultOut]:
    """List the check results of a run.

    Needs `RUN` or `READ_ENVIRONMENT`. Answers 404 when the run does not exist in the
    environment.

    Parameters
    ----------
    env
        Environment name.
    run_id
        The run's id.
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    list of CheckResultOut
        By task, then by check name.
    """
    return [_check_out(v) for v in service.checks(principal, env, run_id)]


@router.post(
    "/runs/{run_id}/checks/rows",
    response_model=QuerySubmitOut,
    status_code=202,
    responses=errors(403, 404, 409),
)
def show_check_rows(
    env: str,
    run_id: uuid.UUID,
    body: CheckRowsRequest,
    principal: PrincipalRef = Depends(get_principal),
    session: Session = Depends(get_session),
    authorization: AuthorizationService = Depends(get_grant_authorization),
) -> QuerySubmitOut:
    """Queue a query for the rows a check found.

    Queues an ordinary query of the caller that shows the rows a check found in a run, and
    answers 202 like the query submit route. It runs the check's SQL under the caller's own
    grants, row filters and column masks, and reads the table at the snapshot the check
    read. The body names the check only. Needs `RUN` or `READ_ENVIRONMENT`, and
    `SUBMIT_QUERY`; a task is refused. Answers 404 when the environment or run does not
    exist or the run has no result for the check, and 409 when the check read no snapshot or
    the run's project version no longer holds its SQL.

    Parameters
    ----------
    env
        Environment name.
    run_id
        The run's id.
    body
        The check, ``<namespace>.<table>/<check>``.
    principal
        The authenticated caller.
    session
        The request's database session.
    authorization
        The authorization service.

    Returns
    -------
    QuerySubmitOut
        The queued query's id; poll it and fetch its result with the query routes.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, without either right, or for a task.
    lakana_core.errors.NotFoundError
        Mapped to 404: no such environment or run, or the run has no result for the check.
    lakana_core.errors.ConflictError
        Mapped to 409: the check read no snapshot, so it has no rows to show, or the run's
        project version no longer holds its SQL.
    """
    run = submit_check_rows(
        session, authorization, principal, environment=env, run_id=run_id, check=body.check
    )
    return QuerySubmitOut(query_id=str(run.id))


@router.get("/checks", response_model=CheckResultPage, responses=errors(403, 404))
def list_checks(
    env: str,
    table: str | None = Query(default=None, max_length=1024),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> CheckResultPage:
    """List the check results of an environment.

    Lists check results, newest first, one page at a time. Needs `READ_ENVIRONMENT`. Answers
    404 when the environment does not exist and 422 for a `limit` below 1 or a `cursor` this
    server did not issue.

    Parameters
    ----------
    env
        Environment name.
    table
        Only this table's results (dotted name).
    limit
        Most results returned (1 to 200).
    cursor
        The `next_cursor` of the previous page; send the same `table` with every page.
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    CheckResultPage
        The page and the cursor of the next one.
    """
    items, next_cursor, hidden = service.list_checks(
        principal, env, table=table, limit=limit, cursor=cursor
    )
    return CheckResultPage(
        items=[_check_out(v) for v in items], next_cursor=next_cursor, hidden=hidden
    )


@router.post("/runs/{run_id}/cancel", response_model=RunOut, responses=errors(403, 404, 409))
def cancel_run(
    env: str,
    run_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> RunOut:
    """Cancel a run.

    Asks for the run to stop. Needs `RUN`. Answers 404 when the run does not exist in the
    environment and 409 when it has finished.

    Parameters
    ----------
    env
        Environment name.
    run_id
        The run's id.
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    RunOut
        The run with `cancel_requested`.
    """
    return run_out(service.cancel(principal, env, run_id))


@router.post(
    "/runs/{run_id}/resume", response_model=RunOut, status_code=202, responses=errors(403, 404, 409)
)
def resume_run(
    env: str,
    run_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> RunOut:
    """Resume a run.

    Starts the failed, upstream-failed or cancelled part of a finished run again, and
    answers 202. Needs `RUN`. Answers 404 when the run does not exist or its repository is
    no longer deployed, 409 when the run is still active, the current project version
    changed, or as for starting a run, and 422 when no task failed, was upstream failed or
    was cancelled.

    Parameters
    ----------
    env
        Environment name.
    run_id
        The run to resume.
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    RunOut
        The new run.
    """
    return run_out(service.resume(principal, env, run_id))


@router.get(
    "/runs/{run_id}/tasks/{asset}/log", response_model=TaskLogOut, responses=errors(403, 404)
)
def task_log(
    env: str,
    run_id: uuid.UUID,
    asset: str,
    offset: int | None = Query(
        None, ge=0, description="Read the log from this byte; omit for the whole log."
    ),
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> TaskLogOut:
    """Show a task's log.

    Returns the task's log text and its attempts. With `offset`, only the text from that
    byte on (at most 1 MiB, ending before a half character), and the `next_offset` to
    continue from. Needs `RUN` or `READ_ENVIRONMENT`. Answers 404 when the run or task does
    not exist.

    Parameters
    ----------
    env
        Environment name.
    run_id
        The run's id.
    asset
        The task's dotted table name.
    offset
        The byte to read from.
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    TaskLogOut
        Text, where to continue, and attempts.
    """
    return _log_out(service.task_log(principal, env, run_id, asset, offset))


@scheduler_router.get("/scheduler", response_model=SchedulerOut, responses=errors(403))
def scheduler_status(
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> SchedulerOut:
    """Show the scheduler.

    Reports which server holds the scheduler and how long the queues are. Needs
    `MANAGE_ENVIRONMENT` on the instance.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    SchedulerOut
        Holder, heartbeat and counts.
    """
    return SchedulerOut(**dataclasses.asdict(service.scheduler_status(principal)))


@scheduler_router.get("/workers", response_model=list[WorkerOut], responses=errors(403))
def list_workers(
    principal: PrincipalRef = Depends(get_principal),
    service: RunService = Depends(get_run_service),
) -> list[WorkerOut]:
    """List the workers.

    Lists the registered workers and what each is doing. Needs `MANAGE_ENVIRONMENT` on the
    instance.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The run service.

    Returns
    -------
    list of WorkerOut
        By id. `status` is `alive` or `lost`; `takes` is `all`, `interactive` or `pipeline`;
        the three run ids say what it is doing; `caches` the sizes of its caches.
    """
    return [
        WorkerOut(
            id=w.id,
            host=w.host,
            version=w.version,
            started_at=w.started_at,
            heartbeat_at=w.heartbeat_at,
            status=w.status,
            takes=w.takes,
            query_run_id=None if w.query_run_id is None else str(w.query_run_id),
            deploy_run_id=None if w.deploy_run_id is None else str(w.deploy_run_id),
            task_run_id=None if w.task_run_id is None else str(w.task_run_id),
            caches=None
            if w.caches is None or w.caches_measured_at is None
            else WorkerCachesOut(**dataclasses.asdict(w.caches), measured_at=w.caches_measured_at),
        )
        for w in service.list_workers(principal)
    ]
