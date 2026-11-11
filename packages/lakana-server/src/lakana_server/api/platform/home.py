"""Home in the web interface: the caller's Get started list, Needs your attention and Your work.

Any authenticated person may ask; a task or CI token is refused (403). Get started is about the
caller and the instance's own state (a repository exists, a deploy succeeded), so it asks no
authorization. Needs your attention and Your work show only what the caller may read now, each
row asked with `permits`. None of the routes records anything.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from lakana_core.auth import AuthorizationService, PrincipalRef
from lakana_core.grants.service import GrantService
from lakana_core.home.attention import AttentionService
from lakana_core.home.get_started import get_started
from lakana_core.home.work import your_work
from lakana_core.jobs.service import JobService
from lakana_server.api.platform.schemas import (
    AttentionItemOut,
    AttentionOut,
    GetStartedOut,
    GetStartedStepOut,
    WorkEnvironmentOut,
    WorkJobOut,
    WorkOut,
    WorkRepositoryOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import (
    get_attention_service,
    get_grant_authorization,
    get_grant_service,
    get_job_service,
    get_principal,
    get_session,
)

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/home",
    tags=["home"],
    responses=errors(401),
)


@router.get("/get-started", response_model=GetStartedOut, responses=errors(403))
def read_get_started(
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    session: Session = Depends(get_session),
    grants: GrantService = Depends(get_grant_service),
) -> GetStartedOut:
    """Show your Get started list.

    The steps follow what you may do: an administrator connects a repository, invites the team,
    adds a passkey, deploys the first project and lets CI deploy; a person who may deploy
    deploys, opens a table and runs a query; a person who may read opens a table and runs a
    query; a person without access yet gets no steps. In local mode
    (`lakana server start --local`) an administrator gets add a passkey, deploy from a local
    folder, open a table and run a query. Each step is ticked from the server's state. A task
    or CI token is refused with 403.

    Parameters
    ----------
    request
        The current request, for the server's settings (local mode).
    principal
        The authenticated caller.
    session
        The request's database session.
    grants
        The grant service, asked about the caller only.

    Returns
    -------
    GetStartedOut
        The audience, the steps with whether each is done, and whether the list is hidden or
        complete.
    """
    answer = get_started(
        session, grants, principal, local=request.app.state.settings.ui.local_sign_in
    )
    return GetStartedOut(
        audience=answer.audience,
        local=answer.local,
        steps=[GetStartedStepOut(id=step, done=done) for step, done in answer.steps],
        hidden=answer.hidden,
        complete=answer.complete,
    )


@router.get("/attention", response_model=AttentionOut, responses=errors(403))
def read_attention(
    principal: PrincipalRef = Depends(get_principal),
    service: AttentionService = Depends(get_attention_service),
) -> AttentionOut:
    """Show what needs your attention.

    An administrator sees the jobs whose newest finished run failed in the last seven days, the
    late tables, the checks whose newest result failed or errored, the open recovery links, the
    lost workers and their own queries that failed in that time. Anyone else sees the late
    tables and failed checks among the tables they read in the last seven days or opened, and
    their own queries that failed in that time. A row you may no longer read is left out, never
    refused. At most 50 rows, newest first. A task or CI token is refused with 403: this is a
    person's list.

    Parameters
    ----------
    principal
        The authenticated caller.
    service
        The attention service.

    Returns
    -------
    AttentionOut
        The rows and whether more were left out.
    """
    items, truncated = service.for_caller(principal)
    return AttentionOut(
        items=[
            AttentionItemOut(
                kind=item.kind,
                environment=item.environment,
                title=item.title,
                ref=item.ref,
                at=item.at,
            )
            for item in items
        ],
        truncated=truncated,
    )


@router.get("/work", response_model=WorkOut, responses=errors(403))
def read_work(
    principal: PrincipalRef = Depends(get_principal),
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
    jobs: JobService = Depends(get_job_service),
) -> WorkOut:
    """Show your work: the repositories you created or deployed, and their jobs.

    Only for a person who may deploy somewhere; anyone else gets two empty lists. Environments
    and jobs are shown where you may read the environment. At most 20 of each. A task or CI
    token is refused with 403.

    Parameters
    ----------
    principal
        The authenticated caller.
    session
        The request's database session.
    authz
        The grant-backed authorization service, asked with `permits` only.
    jobs
        The job service.

    Returns
    -------
    WorkOut
        The repositories and their jobs.
    """
    repositories, found = your_work(session, authz, jobs, principal)
    return WorkOut(
        repositories=[
            WorkRepositoryOut(
                name=repo.name,
                environments=[
                    WorkEnvironmentOut(
                        environment=env, last_deploy_status=status, last_deploy_at=at
                    )
                    for env, status, at in repo.environments
                ],
            )
            for repo in repositories
        ],
        jobs=[
            WorkJobOut(
                environment=job.environment,
                repository=job.repository,
                job=job.job,
                last_run_status=job.last_run_status,
                last_run_at=job.last_run_at,
            )
            for job in found
        ],
    )
