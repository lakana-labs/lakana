"""Browser sessions of the platform API: the caller's own session, sign out, and the list.

`/api/v1/sign-in/session` describes and ends the browser session the request came with; a
token has none (409). `/api/v1/principals/{name}/sessions` lists and ends a person's open
sessions: one's own, or anyone's for an administrator, who ends other people's sessions only
with a fresh passkey touch (step-up). No answer carries a session's secret or its hash.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.errors import ConflictError
from lakana_core.identity.links import has_passkey
from lakana_core.identity.passkeys import needs_passkey
from lakana_core.identity.sessions import STEP_UP_WINDOW, SessionService, csrf_token_for
from lakana_core.models import BrowserSession, Principal
from lakana_server.api.platform.schemas import BrowserSessionOut, SessionOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.browser_session import (
    clear_session_cookie,
    read_session_cookie,
    require_interface,
)
from lakana_server.deps import get_principal, get_session, get_session_service

NO_SESSION = "This route describes a browser session; a token has none."
"""The 409 detail of the session routes for a caller authenticated with a token."""

sign_in_router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/sign-in",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(401, 409),
)

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/principals",
    tags=["principals"],
    responses=errors(401),
)


def _own_session(principal: PrincipalRef, request: Request) -> tuple[str, str]:
    """Read the session the request came with.

    Parameters
    ----------
    principal
        The authenticated caller.
    request
        The request, for its cookie.

    Returns
    -------
    tuple of (str, str)
        The session's key id and the cookie's value.

    Raises
    ------
    ConflictError
        If the caller authenticated with a token (409).
    """
    cookie = read_session_cookie(request)
    if principal.session is None or cookie is None:
        raise ConflictError(NO_SESSION)
    return principal.session.key_id, cookie


@sign_in_router.get("/session", response_model=SessionOut)
def describe_session(
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    session: Session = Depends(get_session),
) -> SessionOut:
    """Describe your browser session.

    Answers who is signed in, the session's CSRF token (the same for the whole session, so
    every tab can use it), until when step-up actions need no new passkey touch, and whether
    a passkey must or may be registered now. A request with a token gets 409: a token has no
    session. Answers 409 while the web interface is off.

    Parameters
    ----------
    request
        The request, for its cookie.
    principal
        The authenticated caller.
    session
        The request's database session.

    Returns
    -------
    SessionOut
        The session, without its secret.
    """
    _, cookie = _own_session(principal, request)
    context = principal.session
    assert context is not None  # nosec B101 -- _own_session refused a token caller
    row = session.get(BrowserSession, context.session_id)
    assert row is not None  # nosec B101 -- get_principal just read it
    rp_id = request.app.state.settings.public_origin().host
    usable = has_passkey(session, principal.id, rp_id=rp_id)
    person = session.get(Principal, principal.id)
    return SessionOut(
        name=principal.name,
        display_name=person.display_name if person is not None else None,
        type=principal.type.value,
        via=context.via,
        csrf_token=csrf_token_for(cookie),
        step_up_until=(
            context.last_touch_at + STEP_UP_WINDOW if context.last_touch_at is not None else None
        ),
        needs_passkey=needs_passkey(session, principal, rp_id),
        offer_passkey=not usable and context.via == "local",
        absolute_expires_at=row.absolute_expires_at,
    )


@sign_in_router.delete("/session", status_code=204, response_class=Response)
def sign_out(
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    sessions: SessionService = Depends(get_session_service),
) -> Response:
    """Sign out.

    Ends the browser session the request came with, at once, and tells the browser to drop
    its cookie. A request with a token gets 409: a token has no session to end (revoke the
    token instead).

    Parameters
    ----------
    request
        The request, for its cookie and the settings.
    principal
        The authenticated caller.
    sessions
        The session service for this request.

    Returns
    -------
    Response
        204, clearing the session cookie.
    """
    key_id, _ = _own_session(principal, request)
    sessions.end(principal, key_id, reason="sign_out")
    response = Response(status_code=204)
    clear_session_cookie(response, request.app.state.settings.public_origin())
    return response


@router.get("/{name}/sessions", response_model=list[BrowserSessionOut], responses=errors(403, 404))
def list_sessions(
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    sessions: SessionService = Depends(get_session_service),
) -> list[BrowserSessionOut]:
    """List a person's open browser sessions.

    Anyone may list their own; listing another person's needs the ADMIN role on the instance
    (403 otherwise). The session the request came with is marked `current`. A session past its
    idle timeout or lifetime is not listed.

    Parameters
    ----------
    name
        The person's principal name.
    principal
        The authenticated caller.
    sessions
        The session service for this request.

    Returns
    -------
    list of BrowserSessionOut
        The open sessions, oldest first.
    """
    current = principal.session.session_id if principal.session is not None else None
    return [
        BrowserSessionOut(
            id=row.id,
            via=row.via,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            absolute_expires_at=row.absolute_expires_at,
            user_agent=row.user_agent,
            peer=row.peer,
            current=row.id == current,
        )
        for row in sessions.list_sessions(principal, name)
    ]


@router.delete(
    "/{name}/sessions", status_code=204, response_class=Response, responses=errors(403, 404)
)
def end_sessions(
    name: str,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    sessions: SessionService = Depends(get_session_service),
) -> Response:
    """End all of a person's browser sessions.

    Your own: sign out everywhere, including this browser. Another person's: needs the ADMIN
    role on the instance and, from the web interface, a passkey touch within the last 15
    minutes (403 `StepUpRequired` otherwise); each ended session is recorded in the audit log
    with your name.

    Parameters
    ----------
    name
        The person's principal name.
    request
        The request, for the settings.
    principal
        The authenticated caller.
    sessions
        The session service for this request.

    Returns
    -------
    Response
        204; it also clears the session cookie when you end your own sessions from a browser.
    """
    sessions.end_all(principal, name)
    response = Response(status_code=204)
    if principal.session is not None and name == principal.name:
        clear_session_cookie(response, request.app.state.settings.public_origin())
    return response
