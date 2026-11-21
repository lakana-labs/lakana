"""The sign-in settings of the platform API: how long browser sessions last, and the token window.

The lengths live in the metadata database as instance settings, not in the environment: every
signed-in person may read them, an administrator changes them (from the web interface with a
fresh passkey touch). A change of the lengths applies to sessions that begin afterwards; the
token window of people linked to the identity provider applies to the next request with a token.
An administrator also sees the provider and its counts (#32).
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.grants.service import GrantService
from lakana_core.identity.oidc_links import linked_count, unlinked_admin_count
from lakana_core.identity.sessions import (
    SESSION_BOUNDS,
    TOKEN_WINDOW_BOUNDS,
    SessionLimits,
    SessionService,
)
from lakana_core.oidc.provider import OidcProvider
from lakana_server.api.platform.schemas import (
    SignInBoundsOut,
    SignInProviderOut,
    SignInSettingsIn,
    SignInSettingsOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.deps import (
    get_grant_service,
    get_principal,
    get_session,
    get_session_service,
)

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


def _out(
    request: Request,
    principal: PrincipalRef,
    limits: SessionLimits,
    sessions: SessionService,
    session: Session,
    grants: GrantService,
) -> SignInSettingsOut:
    """Write the limits in minutes, the unit every bound is expressible in, and the window.

    Parameters
    ----------
    request
        The request, for the provider client.
    principal
        The caller: the provider block is an administrator's.
    limits
        The limits in force.
    sessions
        The session service, for the token window.
    session
        The request's database session, for the counts.
    grants
        The grant service, asked whether the caller is an administrator.

    Returns
    -------
    SignInSettingsOut
        Both lengths and their bounds, in minutes; the token window in days; the provider.
    """
    b = SESSION_BOUNDS
    low, high = TOKEN_WINDOW_BOUNDS
    provider: OidcProvider | None = request.app.state.oidc_provider
    shown = None
    if provider is not None and grants.is_admin(principal):
        shown = SignInProviderOut(
            display_name=provider.config.shown_name,
            issuer=provider.config.issuer or "",
            linked_people=linked_count(session),
            unlinked_admins=unlinked_admin_count(session),
        )
    return SignInSettingsOut(
        idle_timeout_minutes=_minutes(limits.idle),
        absolute_lifetime_minutes=_minutes(limits.absolute),
        bounds=SignInBoundsOut(
            idle_timeout_minutes_min=_minutes(b.idle_min),
            idle_timeout_minutes_max=_minutes(b.idle_max),
            absolute_lifetime_minutes_min=_minutes(b.absolute_min),
            absolute_lifetime_minutes_max=_minutes(b.absolute_max),
            token_provider_window_days_min=low,
            token_provider_window_days_max=high,
        ),
        token_provider_window_days=sessions.get_token_window(),
        provider=shown,
    )


@router.get("", response_model=SignInSettingsOut)
def get_sign_in_settings(
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    sessions: SessionService = Depends(get_session_service),
    session: Session = Depends(get_session),
    grants: GrantService = Depends(get_grant_service),
) -> SignInSettingsOut:
    """Show how long browser sessions last.

    The idle timeout (default 12 hours, 720 minutes) and the absolute lifetime (default 7 days,
    10080 minutes), in minutes, with the bounds an administrator may choose between, and the
    token window of people linked to the identity provider (default 30 days). Anyone signed in
    may read them. An administrator also gets `provider`, while OIDC sign-in is on: its name and
    issuer, how many people are linked and how many administrators can sign in without it.

    Parameters
    ----------
    request
        The request, for the provider client.
    principal
        The authenticated caller.
    sessions
        The session service for this request.
    session
        The request's database session, for the provider's counts.
    grants
        The grant service, asked whether the caller is an administrator.

    Returns
    -------
    SignInSettingsOut
        The lengths in force for new sessions and the token window.
    """
    return _out(request, principal, sessions.get_limits(), sessions, session, grants)


@router.patch("", response_model=SignInSettingsOut, responses=errors(403))
def set_sign_in_settings(
    body: SignInSettingsIn,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    sessions: SessionService = Depends(get_session_service),
    session: Session = Depends(get_session),
    grants: GrantService = Depends(get_grant_service),
) -> SignInSettingsOut:
    """Change how long browser sessions last.

    Needs the ADMIN role on the instance and, from the web interface, a passkey touch within
    the last 15 minutes. Both are in minutes: the idle timeout 15 to 1440 (24 hours), the
    absolute lifetime 60 (1 hour) to 43200 (30 days) and not shorter than the idle timeout; the
    token window of people linked to the identity provider 1 to 365 days. A value outside
    answers 422 naming the bound. Sessions already open keep their lengths; a new token window
    applies to the next request with a token. Recorded in the audit log.

    Parameters
    ----------
    body
        The new values; a field left out keeps its value.
    request
        The request, for the provider client.
    principal
        The authenticated caller.
    sessions
        The session service for this request.
    session
        The request's database session, for the provider's counts.
    grants
        The grant service, asked whether the caller is an administrator.

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
        token_window_days=body.token_provider_window_days,
    )
    return _out(request, principal, limits, sessions, session, grants)
