"""The server's configuration, read-only: Settings, Configuration in the web interface."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from lakana_core.auth import AuthorizationService, PrincipalRef
from lakana_core.identity.sessions import require_principal_admin
from lakana_server.api.platform.schemas import ConfigurationSettingOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.configuration import describe_settings
from lakana_server.deps import get_grant_authorization, get_principal, get_session

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/settings/configuration",
    tags=["settings"],
    responses=errors(401),
)


@router.get("", response_model=list[ConfigurationSettingOut], responses=errors(403))
def get_configuration(
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    session: Session = Depends(get_session),
    authz: AuthorizationService = Depends(get_grant_authorization),
) -> list[ConfigurationSettingOut]:
    """Show the running server's configuration (administrator only).

    One entry per setting, in the order of the settings reference, with its environment variable,
    its value and whether the environment gave it (`set`) or it is the default. A secret (a key,
    a password, a database URL, a proxy or index URL that may carry a password) never carries a
    value: `value` is `null` and `has_value` says whether it is set. Only the server's own
    settings are shown. Needs the ADMIN role through a personal credential; a task or CI token
    is refused with 403. Reading it records nothing.

    Parameters
    ----------
    request
        The current request, for the server's settings.
    principal
        The authenticated caller.
    session
        The request's database session.
    authz
        The request's authorization service.

    Returns
    -------
    list of ConfigurationSettingOut
        The settings.
    """
    require_principal_admin(session, authz, principal, doing="read the server's configuration")
    return [
        ConfigurationSettingOut(**vars(item))
        for item in describe_settings(request.app.state.settings)
    ]
