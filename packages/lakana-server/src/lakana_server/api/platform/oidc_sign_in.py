"""Sign-in through the organisation's identity provider (OIDC, #32).

The flow is the authorization-code flow with PKCE against the one provider of
`LAKANA_OIDC__ISSUER`:

1. `POST /api/v1/sign-in/oidc/start` opens a flow (`lakana_core.identity.oidc_flows`), binds it
   to the browser with a short-lived cookie (`__Host-lakana_oidc`, `SameSite=Lax`, 10 minutes)
   and answers the provider's authorization URL.
2. The provider sends the browser to `GET /api/v1/sign-in/oidc/callback`, which runs every
   check (`lakana_server.oidc_flow.complete_callback`), writes the outcome on the flow and always
   answers 303 to `<LAKANA_UI__PUBLIC_URL>/sign-in/oidc`, whatever happened: a redirect never
   carries anything the request named.
3. That page calls `POST /api/v1/sign-in/oidc/finish` (same origin), which takes the flow once,
   starts the browser session as a passkey sign-in does and returns the CSRF token.

None of them takes a credential, every one answers 409 while the interface is off, and `start`
and `finish` read at most 16 KiB of body. `start` answers at most
`LAKANA_UI__PASSKEY_OPTIONS_PER_IP_PER_MINUTE` calls per client address and
`LAKANA_UI__PASSKEY_OPTIONS_PER_NETWORK_PER_MINUTE` per network and minute, with limiters of its
own. A refusal is audited as `SIGN_IN_OIDC_REFUSED` with its reason, rate limited like the other
refused sign-ins. The access log drops the callback's query (`lakana_server.log_config`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.errors import OidcOff, OidcRefusal, OidcSignInRefused
from lakana_core.identity.oidc_flows import (
    OidcFlowStore,
    ProviderIdentity,
    principal_for,
    safe_return_to,
    sign_in_outcome,
)
from lakana_core.identity.passkeys import OPEN_BODY_LIMIT
from lakana_core.identity.sessions import SessionService
from lakana_core.models import AuditEventType
from lakana_core.oidc.provider import OidcProvider, ProviderUnavailable
from lakana_server.api.platform.schemas import (
    OidcInfoOut,
    OidcSignInOut,
    OidcStartIn,
    OidcStartOut,
)
from lakana_server.api.platform.sign_in import (
    TOO_MANY_ATTEMPTS,
    OpenSignInRoute,
    client_bucket,
    network_bucket,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.browser_session import (
    clear_flow_cookie,
    cookie_name,
    flow_cookie_name,
    require_interface,
    set_flow_cookie,
    set_session_cookie,
)
from lakana_server.deps import get_clock, get_session, get_session_service, record_refused_oidc
from lakana_server.errors import TooManyAttemptsError
from lakana_server.oidc_flow import (
    MAX_PARAMETER,
    authorization_url,
    complete_callback,
    landing_url,
    redirect_uri,
)

OIDC_OFF = (
    "Sign-in through an identity provider is off on this server: an operator sets "
    "LAKANA_OIDC__ISSUER, LAKANA_OIDC__CLIENT_ID and LAKANA_OIDC__CLIENT_SECRET to turn it on. "
    "Sign in with a passkey instead."
)
"""The 409 detail of `start` while OIDC sign-in is off."""


class OidcOpenRoute(OpenSignInRoute):
    """`OpenSignInRoute` with the OIDC sign-in's 413 detail."""

    too_large = f"An OIDC sign-in request is at most {OPEN_BODY_LIMIT} bytes; send only return_to."
    """The 413 detail."""


router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/sign-in/oidc",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(409),
)

open_router = APIRouter(
    route_class=OidcOpenRoute,
    prefix="/api/v1/sign-in/oidc",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(409, 413),
)


def _provider(request: Request) -> OidcProvider:
    """Read the app's provider client.

    Parameters
    ----------
    request
        The request.

    Returns
    -------
    OidcProvider
        The client.

    Raises
    ------
    OidcOff
        409, while `LAKANA_OIDC__ISSUER` is unset.
    """
    provider: OidcProvider | None = request.app.state.oidc_provider
    if provider is None:
        raise OidcOff(OIDC_OFF)
    return provider


@router.get("", response_model=OidcInfoOut)
def get_oidc_info(request: Request) -> OidcInfoOut:
    """Say whether people sign in through an identity provider, and its name.

    Needs no credential: the sign-in page asks before anyone is signed in, to show "Sign in
    with <provider>". Without `LAKANA_OIDC__ISSUER` the answer is `enabled: false` and the
    interface does not mention OIDC.

    Parameters
    ----------
    request
        The request, for the settings.

    Returns
    -------
    OidcInfoOut
        Whether OIDC sign-in is on, and the provider's name for the button.
    """
    provider: OidcProvider | None = request.app.state.oidc_provider
    if provider is None:
        return OidcInfoOut(enabled=False, display_name=None)
    return OidcInfoOut(enabled=True, display_name=provider.config.shown_name)


@open_router.post("/start", response_model=OidcStartOut, responses=errors(403, 429))
def start_oidc_sign_in(
    body: OidcStartIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> OidcStartOut:
    """Start signing in through the identity provider.

    Needs no credential. Opens a flow that works once, for 10 minutes, binds it to this browser
    with a cookie, and answers the provider's sign-in page: send the browser there. The page to
    open afterwards (`return_to`) stays on the server; anything but a path inside the
    interface becomes `/`. Answers 409 while OIDC sign-in is off, 403 `OidcSignInRefused`
    (`reason: provider_unreachable`) when the provider cannot be reached, and 429 past
    `LAKANA_UI__PASSKEY_OPTIONS_PER_IP_PER_MINUTE` calls per client address or
    `LAKANA_UI__PASSKEY_OPTIONS_PER_NETWORK_PER_MINUTE` per network and minute.

    Parameters
    ----------
    body
        ``{"return_to"}``, optional.
    request
        The request, for the client's address and the provider client.
    response
        The response, to set the flow cookie on.
    session
        The request's database session.

    Returns
    -------
    OidcStartOut
        The provider's authorization URL for this flow.

    Raises
    ------
    TooManyAttemptsError
        429 past either rate.
    OidcSignInRefused
        403 ``provider_unreachable`` when the provider's metadata cannot be read.
    """
    provider = _provider(request)
    client = request.client.host if request.client else "unknown"
    admitted, _ = request.app.state.oidc_start_limiter.admit(client_bucket(client))
    if admitted:
        network = request.app.state.oidc_start_network_limiter
        admitted, _ = network.admit(network_bucket(client))
    if not admitted:
        raise TooManyAttemptsError(TOO_MANY_ATTEMPTS)
    meta = None
    try:
        meta = provider.metadata()
    except ProviderUnavailable:
        record_refused_oidc(request, session, "provider_unreachable")
    if meta is None:
        raise OidcSignInRefused("provider_unreachable")
    origin = request.app.state.settings.public_origin()
    opened = OidcFlowStore(session, clock=get_clock(request)).open(
        "sign_in", safe_return_to(body.return_to)
    )
    session.commit()
    config = provider.config
    url = authorization_url(
        meta,
        client_id=config.client_id or "",
        redirect_uri=redirect_uri(origin),
        scopes=config.scopes,
        state=opened.state,
        nonce=opened.nonce,
        code_verifier=opened.code_verifier,
    )
    set_flow_cookie(response, origin, opened.cookie_value)
    return OidcStartOut(authorization_url=url)


@router.get(
    "/callback",
    response_class=RedirectResponse,
    status_code=303,
    responses={303: {"description": "Always to `<LAKANA_UI__PUBLIC_URL>/sign-in/oidc`."}},
)
def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Come back from the identity provider.

    The provider sends the browser here; nothing else should. Checks the flow (its `state` and
    this browser's flow cookie), exchanges the code with the client secret and verifies the ID
    token, writes what it found on the flow, and always answers 303 to
    `<LAKANA_UI__PUBLIC_URL>/sign-in/oidc`, whatever happened. That page finishes the sign-in
    with `POST /api/v1/sign-in/oidc/finish`. A flow is called back once.

    Parameters
    ----------
    request
        The request, for the flow cookie, the provider client and the clock.
    code
        The authorization code the provider issued.
    state
        The flow's state, as the provider sends it back.
    error
        The provider's error, when it refused.
    session
        The request's database session.

    Returns
    -------
    RedirectResponse
        303 to the interface's page that finishes the sign-in.
    """
    complete_callback(request, session, code=code, state=state, error=error)
    origin = request.app.state.settings.public_origin()
    return RedirectResponse(landing_url(origin), status_code=303)


@open_router.post("/finish", response_model=OidcSignInOut, responses=errors(403))
def finish_oidc_sign_in(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    sessions: SessionService = Depends(get_session_service),
) -> OidcSignInOut:
    """Finish signing in through the identity provider, after the callback.

    Needs no credential: the browser's flow cookie and the callback's checks are the proof. The
    interface's page at `/sign-in/oidc` calls this once. A person whose provider identity is
    linked to their account gets a browser session exactly as a passkey sign-in gives one: a
    fresh session id (a session the browser had is ended) and its cookie. A provider sign-in is
    no passkey touch: an action that needs step-up asks for one. Otherwise 403
    `OidcSignInRefused` with `reason`: `not_linked` (ask an administrator for an invite),
    `deactivated`, `token_invalid`, `provider_unreachable`, `provider_refused`, or
    `flow_expired` (no flow cookie, or a flow that was used, never called back or older than 10
    minutes). A flow is finished once, whatever the answer.

    Parameters
    ----------
    request
        The request, for the cookies, the client's address and the `User-Agent`.
    response
        The response, to set the session cookie and clear the flow cookie on.
    session
        The request's database session.
    sessions
        The session service.

    Returns
    -------
    OidcSignInOut
        Who is signed in, the session's CSRF token and the page to open.

    Raises
    ------
    OidcSignInRefused
        403 with the reason, when nobody is signed in.
    """
    _provider(request)
    origin = request.app.state.settings.public_origin()
    clock = get_clock(request)
    browser = request.cookies.get(flow_cookie_name(origin)) or ""
    flow = OidcFlowStore(session, clock=clock).finish(browser[:MAX_PARAMETER])
    found = sign_in_outcome(flow.outcome if flow is not None else None)
    session.commit()
    if not isinstance(found, ProviderIdentity):
        # A refusal the callback found is audited there already.
        if flow is None or flow.outcome is None:
            record_refused_oidc(request, session, found)
        raise OidcSignInRefused(found)
    principal = principal_for(session, found)
    if principal is None or not principal.is_active:
        reason: OidcRefusal = "not_linked" if principal is None else "deactivated"
        record_refused_oidc(request, session, reason, iss=found.issuer, sub=found.subject)
        raise OidcSignInRefused(reason)
    peer = request.client.host if request.client else "unknown"
    started = sessions.start(
        principal.id,
        via="oidc",
        replacing=request.cookies.get(cookie_name(origin)) or None,
        peer=peer,
        user_agent=request.headers.get("user-agent", ""),
        # A provider can answer silently from its own session, so a provider sign-in proves
        # no fresh presence and is no step-up touch; provider step-up (prompt=login, max_age,
        # auth_time) is PR 2's.
        touched=False,
    )
    now = clock()
    principal.last_provider_sign_in_at = now
    principal.oidc_email, principal.oidc_name = found.email, found.name
    AuditService(session).record(
        principal.id,
        AuditEventType.SIGN_IN_OIDC,
        detail={"iss": found.issuer, "sub": found.subject, "key_id": started.key_id, "peer": peer},
    )
    session.commit()
    lifetime = started.absolute_expires_at - now
    set_session_cookie(
        response, origin, started.cookie_value, max(int(lifetime.total_seconds()), 0)
    )
    clear_flow_cookie(response, origin)
    return OidcSignInOut(
        name=started.principal.name,
        csrf_token=started.csrf_token,
        return_to=flow.return_to if flow is not None else "/",
    )
