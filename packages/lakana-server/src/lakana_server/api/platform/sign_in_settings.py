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

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/settings/sign-in",
    tags=["settings"],
    responses=errors(401),
)


def _minutes(value: timedelta) -> int:
    """Count the whole minutes of a duration.

    Parameters
    ----------
    value
        The duration.

    Returns
    -------
    int
        Its minutes.
    """
    return int(value.total_seconds()) // _MINUTE


def _out(limits: SessionLimits) -> SignInSettingsOut:
    """Write the limits in minutes, the unit every bound is expressible in.

    Parameters
    ----------
    limits
        The limits in force.

    Returns
    -------
    SignInSettingsOut
        Both lengths and their bounds, in minutes.
    """
    b = SESSION_BOUNDS
    return SignInSettingsOut(
        idle_timeout_minutes=_minutes(limits.idle),
        absolute_lifetime_minutes=_minutes(limits.absolute),
        bounds=SignInBoundsOut(
            idle_timeout_minutes_min=_minutes(b.idle_min),
            idle_timeout_minutes_max=_minutes(b.idle_max),
            absolute_lifetime_minutes_min=_minutes(b.absolute_min),
            absolute_lifetime_minutes_max=_minutes(b.absolute_max),
        ),
    )


@router.get("", response_model=SignInSettingsOut)
def get_sign_in_settings(
    principal: PrincipalRef = Depends(get_principal),
    sessions: SessionService = Depends(get_session_service),
) -> SignInSettingsOut:
    """Show how long browser sessions last.

    The idle timeout (default 12 hours, 720 minutes) and the absolute lifetime (default 7 days,
    10080 minutes), in minutes, with the bounds an administrator may choose between. Anyone
    signed in may read them.

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
    the last 15 minutes. Both are in minutes: the idle timeout 15 to 1440 (24 hours), the
    absolute lifetime 60 (1 hour) to 43200 (30 days) and not shorter than the idle timeout; a
    value outside answers 422 naming the bound. Sessions already open keep their lengths.
    Recorded in the audit log.

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
            timedelta(minutes=body.absolute_lifetime_minutes)
            if body.absolute_lifetime_minutes is not None
            else None
        ),
    )
    return _out(limits)
