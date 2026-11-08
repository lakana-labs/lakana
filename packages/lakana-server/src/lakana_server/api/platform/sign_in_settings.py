"""The sign-in settings of the platform API: how long browser sessions last.

The lengths live in the metadata database as instance settings, not in the environment: every
signed-in person may read them, an administrator changes them (from the web interface with a
fresh passkey touch). A change applies to sessions that begin afterwards.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends

from lakana_core.auth import PrincipalRef
from lakana_core.identity.sessions import SESSION_BOUNDS, SessionLimits, SessionService
from lakana_server.api.platform.schemas import (
    SignInBoundsOut,
    SignInSettingsIn,
    SignInSettingsOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import get_principal, get_session_service

_MINUTE = 60
_DAY = 86400

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/settings/sign-in",
    tags=["settings"],
    responses=errors(401),
)


def _out(limits: SessionLimits) -> SignInSettingsOut:
    """Write the limits in the units the interface shows.

    Parameters
    ----------
    limits
        The limits in force.

    Returns
    -------
    SignInSettingsOut
        Minutes and whole days, with the bounds.
    """
    b = SESSION_BOUNDS
    return SignInSettingsOut(
        idle_timeout_minutes=int(limits.idle.total_seconds()) // _MINUTE,
        absolute_lifetime_days=int(limits.absolute.total_seconds()) // _DAY,
        bounds=SignInBoundsOut(
            idle_timeout_minutes_min=int(b.idle_min.total_seconds()) // _MINUTE,
            idle_timeout_minutes_max=int(b.idle_max.total_seconds()) // _MINUTE,
            absolute_lifetime_days_min=max(1, -(-int(b.absolute_min.total_seconds()) // _DAY)),
            absolute_lifetime_days_max=int(b.absolute_max.total_seconds()) // _DAY,
        ),
    )


@router.get("", response_model=SignInSettingsOut)
def get_sign_in_settings(
    principal: PrincipalRef = Depends(get_principal),
    sessions: SessionService = Depends(get_session_service),
) -> SignInSettingsOut:
    """Show how long browser sessions last.

    The idle timeout (default 12 hours) and the absolute lifetime (default 7 days), with the
    bounds an administrator may choose between. Anyone signed in may read them.

    Parameters
    ----------
    principal
        The authenticated caller.
    sessions
        The session service for this request.

    Returns
    -------
    SignInSettingsOut
        The lengths in force for new sessions.
    """
    return _out(sessions.get_limits())


@router.patch("", response_model=SignInSettingsOut, responses=errors(403))
def set_sign_in_settings(
    body: SignInSettingsIn,
    principal: PrincipalRef = Depends(get_principal),
    sessions: SessionService = Depends(get_session_service),
) -> SignInSettingsOut:
    """Change how long browser sessions last.

    Needs the ADMIN role on the instance and, from the web interface, a passkey touch within
    the last 15 minutes. The idle timeout may be 15 minutes to 24 hours, the absolute lifetime
    1 to 30 days and not shorter than the idle timeout; a value outside answers 422 naming the
    bound. Sessions already open keep their lengths. Recorded in the audit log.

    Parameters
    ----------
    body
        The new lengths; a field left out keeps its value.
    principal
        The authenticated caller.
    sessions
        The session service for this request.

    Returns
    -------
    SignInSettingsOut
        The lengths now in force for new sessions.
    """
    limits = sessions.set_limits(
        principal,
        idle=(
            timedelta(minutes=body.idle_timeout_minutes)
            if body.idle_timeout_minutes is not None
            else None
        ),
        absolute=(
            timedelta(days=body.absolute_lifetime_days)
            if body.absolute_lifetime_days is not None
            else None
        ),
    )
    return _out(limits)
