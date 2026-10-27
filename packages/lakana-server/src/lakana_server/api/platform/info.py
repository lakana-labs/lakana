"""The info resource of the platform API.

Tells an authenticated caller which version of the platform it is talking to, and how large an
artifact version may be (the operator's `LAKANA_ARTIFACTS__MAX_*`, so a client can refuse a tree
that is over a limit before it sends anything). It performs no authorization check, like
`whoami`: the version is no more than any authenticated caller can learn from a refused deploy,
and the limits are numbers an upload's refusal names anyway. It is deliberately not on the
unauthenticated `/healthz`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Depends, Request

from lakana_core.auth import PrincipalRef
from lakana_server.api.platform.schemas import ArtifactLimitsOut, InfoOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal

router = APIRouter(
    route_class=AuthenticateFirstRoute, prefix="/api/v1/info", tags=["info"], responses=errors(401)
)


def platform_version() -> str:
    """Return the version of the running server package.

    Returns
    -------
    str
        The installed `lakana-server` distribution's version, which every package of a
        release shares (derived from the git tag, never written by hand); ``0+unknown``
        when the package is not installed (a bare source checkout).
    """
    try:
        return version("lakana-server")
    except PackageNotFoundError:
        return "0+unknown"


@router.get("", response_model=InfoOut)
def info(request: Request, principal: PrincipalRef = Depends(get_principal)) -> InfoOut:
    """Describe the instance.

    Shows the platform version of the running server the limits it applies to artifacts and the
    largest Arrow upload it accepts.
    Any authenticated caller may ask, a task token included.

    Parameters
    ----------
    request
        The current request, for the operator's artifact limits.
    principal
        The authenticated caller; any principal, including a task token.

    Returns
    -------
    InfoOut
        The platform version, the artifact limits and the upload limit.
    """
    limits = request.app.state.settings.artifacts
    return InfoOut(
        version=platform_version(),
        artifact_limits=ArtifactLimitsOut(
            max_files=limits.max_files,
            max_file_bytes=limits.max_file_bytes,
            max_bytes=limits.max_bytes,
        ),
        upload_max_bytes=request.app.state.settings.append.max_bytes,
    )
