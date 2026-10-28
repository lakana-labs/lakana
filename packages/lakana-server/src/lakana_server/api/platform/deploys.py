"""Deploys, deployments, project versions and orphans of the platform API.

The server only queues and reads: the worker fetches, validates and records.
"""

from __future__ import annotations

import dataclasses
import uuid

from fastapi import APIRouter, Depends, Query, Response

from lakana_core.auth import PrincipalRef
from lakana_core.models import DeployKind
from lakana_core.projects.deploys import DeploymentView, DeployRunView, DeployService
from lakana_server.api.platform.listing import set_next_cursor
from lakana_server.api.platform.schemas import (
    DeployAccepted,
    DeployCreate,
    DeployJobStartOut,
    DeploymentOut,
    DeployOut,
    OrphanOut,
    OrphansOut,
    ProjectVersionOut,
    RunAsIn,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_deploy_service, get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}",
    tags=["deploys"],
    responses=errors(401),
)


def _to_out(view: DeployRunView) -> DeployOut:
    """Build a response body.

    Parameters
    ----------
    view
        The run's view.

    Returns
    -------
    DeployOut
        The body.
    """
    return DeployOut(
        id=view.id,
        environment=view.environment,
        repository=view.repository,
        kind=view.kind.value,
        status=view.status.value,
        requested_ref=view.requested_ref,
        resolved_sha=view.resolved_sha,
        rollback_to_project_version_id=view.rollback_to_job_version_id,
        error_message=view.error_message,
        diff=view.diff,
        project_version_id=view.job_version_id,
        principal_id=view.principal_id,
        created_at=view.created_at,
        started_at=view.started_at,
        finished_at=view.finished_at,
        hidden_adopted=view.hidden_adopted,
        checks=view.checks,
        warnings=view.warnings,
        waiting_for=None if view.waiting_for is None else view.waiting_for.to_json(),
        job_starts=[DeployJobStartOut(**dataclasses.asdict(start)) for start in view.job_starts],
    )


@router.post("/deploys", response_model=DeployAccepted, status_code=202, responses=errors(403, 404))
def create_deploy(
    env: str,
    body: DeployCreate,
    principal: PrincipalRef = Depends(get_principal),
    service: DeployService = Depends(get_deploy_service),
) -> DeployAccepted:
    """Queue a deploy, dry run or rollback.

    Queues the work for a worker and answers 202 with the deploy's id; poll it to see how it
    went. Needs `DEPLOY` on the environment. A CI token may queue only one deploy or dry run
    of its own repository and signed commit. Answers 404 when the environment or repository
    does not exist and 422 when the request is malformed or a rollback has no target.

    Parameters
    ----------
    env
        Environment name.
    body
        The request.
    principal
        The authenticated caller.
    service
        The deploy service.

    Returns
    -------
    DeployAccepted
        The queued run's id and status.
    """
    view = service.submit(
        principal,
        env,
        repository=body.repository,
        kind=DeployKind(body.kind),
        ref=body.ref,
        to_sha=body.to_sha,
        to_previous=body.to_previous,
    )
    return DeployAccepted(id=view.id, status=view.status.value)


@router.get("/deploys", response_model=list[DeployOut], responses=errors(403, 404))
def list_deploys(
    env: str,
    response: Response,
    repository: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: DeployService = Depends(get_deploy_service),
) -> list[DeployOut]:
    """List an environment's deploys.

    Lists the deploy history, newest first, one page at a time. The cursor of the next page
    is in the `X-Next-Cursor` response header and is absent on the last page; send the same
    `repository` with every page. Needs `READ_ENVIRONMENT`. Answers 404 when the environment
    or repository does not exist and 422 for a `limit` below 1 or a `cursor` this server did
    not issue.

    Parameters
    ----------
    env
        Environment name.
    response
        The response, to set the cursor header on.
    repository
        Only this repository's deploys.
    limit
        Most deploys returned (1 to 200).
    cursor
        The `X-Next-Cursor` of the previous page.
    principal
        The authenticated caller.
    service
        The deploy service.

    Returns
    -------
    list of DeployOut
        Newest first.
    """
    views, next_cursor = service.history_page(
        principal, env, repository=repository, limit=limit, cursor=cursor
    )
    set_next_cursor(response, next_cursor)
    return [_to_out(view) for view in views]


@router.get("/deploys/{deploy_id}", response_model=DeployOut, responses=errors(403, 404))
def get_deploy(
    env: str,
    deploy_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: DeployService = Depends(get_deploy_service),
) -> DeployOut:
    """Show one deploy.

    Needs `DEPLOY` or `READ_ENVIRONMENT`. A CI token can see only the deploys it queued.
    Answers 404 when the deploy does not exist in the environment.

    Parameters
    ----------
    env
        Environment name.
    deploy_id
        The deploy's id, as returned when it was queued.
    principal
        The authenticated caller.
    service
        The deploy service.

    Returns
    -------
    DeployOut
        The run.
    """
    return _to_out(service.get(principal, env, deploy_id))


def _deployment_out(view: DeploymentView) -> DeploymentOut:
    """Build a deployment body.

    Parameters
    ----------
    view
        The deployment.

    Returns
    -------
    DeploymentOut
        The body.
    """
    return DeploymentOut(
        repository=view.repository,
        project_version_id=view.job_version_id,
        commit_sha=view.commit_sha,
        updated_at=view.updated_at,
        run_as=view.run_as,
    )


@router.get("/deployments", response_model=list[DeploymentOut], responses=errors(403, 404))
def list_deployments(
    env: str,
    principal: PrincipalRef = Depends(get_principal),
    service: DeployService = Depends(get_deploy_service),
) -> list[DeploymentOut]:
    """List what is deployed in an environment.

    Lists, per repository, the project version that is currently deployed. Needs
    `READ_ENVIRONMENT`. Answers 404 when the environment does not exist.

    Parameters
    ----------
    env
        Environment name.
    principal
        The authenticated caller.
    service
        The deploy service.

    Returns
    -------
    list of DeploymentOut
        Sorted by repository.
    """
    return [_deployment_out(row) for row in service.deployments(principal, env)]


@router.put("/deployments/{repo}/run-as", response_model=DeploymentOut, responses=errors(403, 404))
def set_run_as(
    env: str,
    repo: str,
    body: RunAsIn,
    principal: PrincipalRef = Depends(get_principal),
    service: DeployService = Depends(get_deploy_service),
) -> DeploymentOut:
    """Set the service account a deployment runs as.

    Binds the service account whose rights the runs of this deployment use to write. Needs
    `MANAGE_ENVIRONMENT` on the environment. Answers 404 when the environment, repository,
    deployment or service account does not exist (one message for a missing or non-service
    account) and 422 when the service account is deactivated.

    Parameters
    ----------
    env
        Environment name.
    repo
        Repository name.
    body
        The principal to bind.
    principal
        The authenticated caller.
    service
        The deploy service.

    Returns
    -------
    DeploymentOut
        The deployment with its binding.
    """
    return _deployment_out(service.set_run_as(principal, env, repo, body.principal))


@router.delete(
    "/deployments/{repo}/run-as", response_model=DeploymentOut, responses=errors(403, 404)
)
def clear_run_as(
    env: str,
    repo: str,
    principal: PrincipalRef = Depends(get_principal),
    service: DeployService = Depends(get_deploy_service),
) -> DeploymentOut:
    """Remove the service account of a deployment.

    Removes the binding. Needs `MANAGE_ENVIRONMENT` on the environment. Answers 404 when the
    environment, repository or deployment does not exist.

    Parameters
    ----------
    env
        Environment name.
    repo
        Repository name.
    principal
        The authenticated caller.
    service
        The deploy service.

    Returns
    -------
    DeploymentOut
        The deployment without a binding.
    """
    return _deployment_out(service.clear_run_as(principal, env, repo))


@router.delete("/deployments/{repo}", status_code=204, responses=errors(403, 404, 409))
def undeploy(
    env: str,
    repo: str,
    principal: PrincipalRef = Depends(get_principal),
    service: DeployService = Depends(get_deploy_service),
) -> Response:
    """Remove a deployment.

    Removes the repository's deployment and its binding in the environment; the tables and
    data stay. Needs `DEPLOY` on the environment; a CI token is always refused. Answers 404
    when the environment, repository or deployment does not exist and 409 while a run of the
    repository is queued or running.

    Parameters
    ----------
    env
        Environment name.
    repo
        Repository name.
    principal
        The authenticated caller.
    service
        The deploy service.

    Returns
    -------
    Response
        204, no body.
    """
    service.undeploy(principal, env, repo)
    return Response(status_code=204)


@router.get(
    "/project-versions/{project_version_id}",
    response_model=ProjectVersionOut,
    responses=errors(403, 404),
)
def get_project_version(
    env: str,
    project_version_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: DeployService = Depends(get_deploy_service),
) -> ProjectVersionOut:
    """Show a project version.

    Shows the version's manifest, limited to what the caller may see. Needs
    `READ_ENVIRONMENT`. Answers 404 when the environment has no such project version.

    Parameters
    ----------
    env
        Environment name.
    project_version_id
        The project version's id.
    principal
        The authenticated caller.
    service
        The deploy service.

    Returns
    -------
    ProjectVersionOut
        The project version.
    """
    view = service.job_version(principal, env, project_version_id)
    return ProjectVersionOut(
        id=view.id,
        environment=view.environment,
        repository=view.repository,
        commit_sha=view.commit_sha,
        code_uri=view.code_uri,
        code_sha256=view.code_sha256,
        deployed_by=view.deployed_by,
        created_at=view.created_at,
        manifest=view.manifest,
    )


@router.get("/orphans", response_model=OrphansOut, responses=errors(403, 404))
def list_orphans(
    env: str,
    principal: PrincipalRef = Depends(get_principal),
    service: DeployService = Depends(get_deploy_service),
) -> OrphansOut:
    """List tables no project produces any more.

    Lists the tables that earlier project versions produced and the current ones do not.
    Needs `READ_ENVIRONMENT`. Answers 404 when the environment does not exist.

    Parameters
    ----------
    env
        Environment name.
    principal
        The authenticated caller.
    service
        The deploy service.

    Returns
    -------
    OrphansOut
        Readable orphans and a hidden count.
    """
    report = service.orphans(principal, env)
    return OrphansOut(
        orphans=[OrphanOut(repository=o.repository, table=o.table) for o in report.orphans],
        hidden=report.hidden,
    )
