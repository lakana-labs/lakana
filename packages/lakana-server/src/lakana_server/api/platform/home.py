"""Home in the web interface: the caller's Get started list.

Any authenticated caller may ask; the answer is about the caller and the instance's own state
(a repository exists, a deploy succeeded), so the route asks no authorization and records
nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.grants.service import GrantService
from lakana_core.home.get_started import get_started
from lakana_server.api.platform.schemas import GetStartedOut, GetStartedStepOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_grant_service, get_principal, get_session

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/home",
    tags=["home"],
    responses=errors(401),
)


@router.get("/get-started", response_model=GetStartedOut)
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
    folder, open a table and run a query. Each step is ticked from the server's state.

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
