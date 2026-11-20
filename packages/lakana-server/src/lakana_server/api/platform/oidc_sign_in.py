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

import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import PrincipalRef
from lakana_core.errors import (
    ConflictError,
    LinkUnusableError,
    OidcOff,
    OidcRefusal,
    OidcSignInRefused,
    ValidationError,
)
from lakana_core.identity.links import LinkService
from lakana_core.identity.oidc_flows import (
    IN_SESSION_PURPOSES,
    LINK_PURPOSES,
    SIGN_IN_PURPOSES,
    FlowPurpose,
    OidcFlowStore,
    ProviderIdentity,
    principal_for,
    safe_return_to,
    sign_in_outcome,
)
from lakana_core.identity.oidc_links import OidcLinkService, in_browser_session, is_linked
from lakana_core.identity.passkeys import OPEN_BODY_LIMIT
from lakana_core.identity.sessions import (
    STEP_UP_WINDOW,
    SessionService,
    StartedSession,
    require_step_up,
)
from lakana_core.models import AuditEventType, Principal, SignInLink
from lakana_core.oidc.provider import OidcProvider, ProviderUnavailable
from lakana_server.api.platform.schemas import (
    OidcInfoOut,
    OidcInSessionOut,
    OidcInSessionStartIn,
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
from lakana_server.deps import (
    get_clock,
    get_link_service,
    get_oidc_link_service,
    get_principal,
    get_session,
    get_session_service,
    record_failed_link,
    record_refused_oidc,
)
from lakana_server.errors import TooManyAttemptsError
from lakana_server.oidc_flow import (
    MAX_PARAMETER,
    STEP_UP_PARAMETERS,
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

NOT_LINKED_STEP_UP = (
    "Your account signs in with a passkey, not through the identity provider: confirm with your "
    "passkey instead (POST /api/v1/sign-in/step-up/options)."
)
"""The 409 detail of the provider step-up for a person who is not linked."""

SIGN_IN_FLOW = (
    "This flow signs a person in, so it finishes without a session: POST "
    "/api/v1/sign-in/oidc/finish."
)
"""The 409 detail of `finish-in-session` for a flow of `SIGN_IN_PURPOSES`."""

AUTH_TIME_SKEW = timedelta(seconds=60)
"""How much earlier than the step-up's start the provider's ``auth_time`` may be, for clocks
that differ a little."""

IN_SESSION_FLOW = (
    "This flow links or confirms a signed-in person, so it finishes in that person's session: "
    "POST /api/v1/sign-in/oidc/finish-in-session."
)
"""The 409 detail of `finish` for a flow of `IN_SESSION_PURPOSES`."""


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


def _open_flow(
    request: Request,
    response: Response,
    session: Session,
    purpose: FlowPurpose,
    return_to: str | None,
    *,
    principal_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    link_id: uuid.UUID | None = None,
    extra: Mapping[str, str] | None = None,
) -> OidcStartOut:
    """Open a flow, bind it to the browser and build the provider's authorization URL.

    Parameters
    ----------
    request
        The request, for the provider client, the settings and the clock.
    response
        The response, to set the flow cookie on.
    session
        The request's database session; committed here.
    purpose
        What the flow is for.
    return_to
        The page to come back to; anything but a path inside the interface becomes ``/``.
    principal_id
        The person the flow is for, when known.
    session_id
        The browser session the flow belongs to, for a link or a step-up.
    link_id
        The sign-in link's row id, for an invite or a recovery link.
    extra
        Further authorization parameters (``prompt``, ``max_age``).

    Returns
    -------
    OidcStartOut
        The provider's authorization URL for this flow.

    Raises
    ------
    OidcSignInRefused
        403 ``provider_unreachable`` when the provider's metadata cannot be read.
    """
    provider = _provider(request)
    meta = None
    try:
        meta = provider.metadata()
    except ProviderUnavailable:
        record_refused_oidc(request, session, "provider_unreachable")
    if meta is None:
        raise OidcSignInRefused("provider_unreachable")
    origin = request.app.state.settings.public_origin()
    opened = OidcFlowStore(session, clock=get_clock(request)).open(
        purpose,
        safe_return_to(return_to),
        principal_id=principal_id,
        session_id=session_id,
        link_id=link_id,
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
        extra=extra,
    )
    set_flow_cookie(response, origin, opened.browser_secret)
    return OidcStartOut(authorization_url=url)


def _checked_link(
    request: Request, session: Session, links: LinkService, body: OidcStartIn
) -> SignInLink:
    """Check the invite or recovery link a start names, without using it.

    Parameters
    ----------
    request
        The request, for the audit limiter.
    session
        The request's database session.
    links
        The link service.
    body
        The start's body, with `purpose` ``invite`` or ``recovery``.

    Returns
    -------
    SignInLink
        The open link.

    Raises
    ------
    ValidationError
        422 without both halves of the link, or for a link of another kind than `purpose`.
    LinkUnusableError
        410 as `/links/redeem` answers: ``unknown`` for an unknown link or a wrong secret.
    """
    if not body.link_id or not body.secret:
        raise ValidationError(
            f"purpose {body.purpose} needs the link's link_id and secret (the l= and s= halves "
            "after the # of the link)."
        )
    try:
        row = links.check_open(body.link_id, body.secret)
    except LinkUnusableError:
        record_failed_link(request, session, body.link_id)
        raise
    if row.kind != body.purpose:
        raise ValidationError(
            f"This is no {body.purpose} link: only an invite (purpose invite) or a recovery "
            "link (purpose recovery) can be used through the identity provider."
        )
    return row


@open_router.post("/start", response_model=OidcStartOut, responses=errors(403, 410, 429))
def start_oidc_sign_in(
    body: OidcStartIn,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    links: LinkService = Depends(get_link_service),
) -> OidcStartOut:
    """Start signing in through the identity provider.

    Needs no credential. Opens a flow that works once, for 10 minutes, binds it to this browser
    with a cookie, and answers the provider's sign-in page: send the browser there. The page to
    open afterwards (`return_to`) stays on the server; anything but a path inside the
    interface becomes `/`. With `purpose` ``invite`` or ``recovery`` and the link's
    `link_id` and `secret`, the flow accepts that invite or uses that recovery link: the link
    is checked here (410 with `state` as `/links/redeem` answers, a wrong secret like an
    unknown link) and used only at the finish. Answers 409 while OIDC sign-in is off, 403
    `OidcSignInRefused` (`reason: provider_unreachable`) when the provider cannot be reached,
    422 for a link of another kind, and 429 past
    `LAKANA_UI__PASSKEY_OPTIONS_PER_IP_PER_MINUTE` calls per client address or
    `LAKANA_UI__PASSKEY_OPTIONS_PER_NETWORK_PER_MINUTE` per network and minute.

    Parameters
    ----------
    body
        ``{"return_to", "purpose", "link_id", "secret"}``, each optional.
    request
        The request, for the client's address and the provider client.
    response
        The response, to set the flow cookie on.
    session
        The request's database session.
    links
        The link service, to check an invite or a recovery link.

    Returns
    -------
    OidcStartOut
        The provider's authorization URL for this flow.

    Raises
    ------
    TooManyAttemptsError
        429 past either rate.
    """
    _provider(request)
    client = request.client.host if request.client else "unknown"
    admitted, _ = request.app.state.oidc_start_limiter.admit(client_bucket(client))
    if admitted:
        network = request.app.state.oidc_start_network_limiter
        admitted, _ = network.admit(network_bucket(client))
    if not admitted:
        raise TooManyAttemptsError(TOO_MANY_ATTEMPTS)
    if body.purpose == "sign_in":
        return _open_flow(request, response, session, "sign_in", body.return_to)
    row = _checked_link(request, session, links, body)
    return _open_flow(
        request,
        response,
        session,
        body.purpose,
        body.return_to,
        principal_id=row.principal_id,
        link_id=row.id,
    )


@router.get(
    "/callback",
    response_class=RedirectResponse,
    status_code=303,
    responses={
        303: {
            "description": (
                "To `<LAKANA_UI__PUBLIC_URL>/sign-in/oidc`, or `/sign-in/oidc/confirmed` for a "
                "link from the profile or a provider step-up."
            )
        }
    },
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
    with `POST /api/v1/sign-in/oidc/finish`. A flow that links a signed-in person or confirms
    their session goes to `<LAKANA_UI__PUBLIC_URL>/sign-in/oidc/confirmed` instead, which
    finishes it with `POST /api/v1/sign-in/oidc/finish-in-session`. Both are fixed pages of the
    public address. A flow is called back once.

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
    purpose = complete_callback(request, session, code=code, state=state, error=error)
    origin = request.app.state.settings.public_origin()
    return RedirectResponse(landing_url(origin, purpose), status_code=303)


@open_router.post("/finish", response_model=OidcSignInOut, responses=errors(403))
def finish_oidc_sign_in(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    sessions: SessionService = Depends(get_session_service),
    links: LinkService = Depends(get_link_service),
) -> OidcSignInOut:
    """Finish signing in through the identity provider, after the callback.

    Needs no credential: the browser's flow cookie and the callback's checks are the proof. The
    interface's page at `/sign-in/oidc` calls this once. A person whose provider identity is
    linked to their account gets a browser session exactly as a passkey sign-in gives one: a
    fresh session id (a session the browser had is ended) and its cookie. A flow started with
    an invite creates the account and links it; one started with a recovery link signs its
    person in (a linked person only as the same identity, `identity_mismatch` otherwise) and
    links an unlinked one. The link is used here, so one withdrawn or used since the start
    answers `link_unusable` and changes nothing. A provider sign-in is no passkey touch: an
    action that needs step-up asks for one. Otherwise 403 `OidcSignInRefused` with `reason`:
    `not_linked` (ask an administrator for an invite), `deactivated`, `token_invalid`,
    `provider_unreachable`, `provider_refused`, `flow_expired` (no flow cookie, or a flow that
    was used, never called back or older than 10 minutes), or when linking `identity_taken`,
    `last_unlinked_admin` or `link_unusable`. A flow is finished once, whatever the answer. 409
    for a flow that links or confirms a signed-in person: that one finishes at
    `/finish-in-session`.

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
    links
        The link service, for an invite or a recovery link.

    Returns
    -------
    OidcSignInOut
        Who is signed in, the session's CSRF token and the page to open.

    Raises
    ------
    OidcSignInRefused
        403 with the reason, when nobody is signed in.
    ConflictError
        409 for a flow that finishes in a session (`IN_SESSION_FLOW`); it stays usable there.
    """
    _provider(request)
    origin = request.app.state.settings.public_origin()
    clock = get_clock(request)
    browser = (request.cookies.get(flow_cookie_name(origin)) or "")[:MAX_PARAMETER]
    store = OidcFlowStore(session, clock=clock)
    if store.pending_purpose(browser) in IN_SESSION_PURPOSES:
        raise ConflictError(IN_SESSION_FLOW)
    flow = store.finish(browser, purposes=SIGN_IN_PURPOSES)
    found = sign_in_outcome(flow.outcome if flow is not None else None)
    session.commit()
    if not isinstance(found, ProviderIdentity):
        # A refusal the callback found is audited there already.
        if flow is None or flow.outcome is None:
            record_refused_oidc(request, session, found)
        raise OidcSignInRefused(found)
    assert flow is not None  # nosec B101 -- an identity comes only from a flow's outcome
    peer = request.client.host if request.client else "unknown"
    replacing = request.cookies.get(cookie_name(origin)) or None
    user_agent = request.headers.get("user-agent", "")
    if flow.purpose in LINK_PURPOSES:
        try:
            started = links.claim_for_provider(
                flow.link_id, found, replacing=replacing, peer=peer, user_agent=user_agent
            ).started
        except OidcSignInRefused as refused:
            record_refused_oidc(
                request, session, refused.reason, iss=found.issuer, sub=found.subject
            )
            raise
    else:
        started = _sign_in(request, session, sessions, found, replacing=replacing, peer=peer)
    now = clock()
    detail: dict[str, str] = {
        "iss": found.issuer,
        "sub": found.subject,
        "key_id": started.key_id,
        "peer": peer,
    }
    if flow.link_id is not None:
        detail["link_id"] = str(flow.link_id)
    AuditService(session).record(started.principal.id, AuditEventType.SIGN_IN_OIDC, detail=detail)
    session.commit()
    lifetime = started.absolute_expires_at - now
    set_session_cookie(
        response, origin, started.cookie_value, max(int(lifetime.total_seconds()), 0)
    )
    clear_flow_cookie(response, origin)
    return OidcSignInOut(
        name=started.principal.name, csrf_token=started.csrf_token, return_to=flow.return_to
    )


def _sign_in(
    request: Request,
    session: Session,
    sessions: SessionService,
    found: ProviderIdentity,
    *,
    replacing: str | None,
    peer: str,
) -> StartedSession:
    """Start the session of the person linked to a verified identity; the caller commits.

    Parameters
    ----------
    request
        The request, for the audit limiter and the `User-Agent`.
    session
        The request's database session.
    sessions
        The session service.
    found
        The identity the provider verified.
    replacing
        The session cookie the browser presented, if any.
    peer
        The client's address.

    Returns
    -------
    StartedSession
        The new session.

    Raises
    ------
    OidcSignInRefused
        ``not_linked`` or ``deactivated``, audited.
    """
    principal = principal_for(session, found)
    if principal is None or not principal.is_active:
        reason: OidcRefusal = "not_linked" if principal is None else "deactivated"
        record_refused_oidc(request, session, reason, iss=found.issuer, sub=found.subject)
        raise OidcSignInRefused(reason)
    started = sessions.start(
        principal.id,
        via="oidc",
        replacing=replacing,
        peer=peer,
        user_agent=request.headers.get("user-agent", ""),
        # A provider can answer silently from its own session, so a provider sign-in proves
        # no fresh presence and is no step-up touch; that is the provider step-up's
        # (prompt=login, max_age, auth_time).
        touched=False,
    )
    principal.last_provider_sign_in_at = get_clock(request)()
    principal.oidc_email, principal.oidc_name = found.email, found.name
    return started


@router.post(
    "/link/start",
    response_model=OidcStartOut,
    responses=errors(401, 403),
)
def start_oidc_link(
    body: OidcInSessionStartIn,
    request: Request,
    response: Response,
    principal: PrincipalRef = Depends(get_principal),
    session: Session = Depends(get_session),
    links: OidcLinkService = Depends(get_oidc_link_service),
) -> OidcStartOut:
    """Start linking your account to the identity provider, from your profile.

    Needs a browser session with a passkey touch from the last 15 minutes (403
    `StepUpRequired`). Answers the provider's sign-in page: send the browser there; the
    provider sends it back to `<LAKANA_UI__PUBLIC_URL>/sign-in/oidc/confirmed`, which finishes
    with `POST /api/v1/sign-in/oidc/finish-in-session`. Once linked you sign in only through
    the provider: your passkeys stay stored, unused, and no session ends. 403
    `OidcSignInRefused` with `reason` `already_linked`, `cannot_link_operator` (the server
    operator's account) or `last_unlinked_admin` (the last administrator who signs in with a
    passkey, who keeps the way in when the provider is down); 409 for a token.

    Parameters
    ----------
    body
        ``{"return_to"}``, optional.
    request
        The request, for the provider client and the clock.
    response
        The response, to set the flow cookie on.
    principal
        The caller.
    session
        The request's database session.
    links
        The link service.

    Returns
    -------
    OidcStartOut
        The provider's authorization URL for this flow.

    Raises
    ------
    OidcSignInRefused
        403 when the caller cannot be linked.
    """
    _provider(request)
    context = in_browser_session(principal)
    require_step_up(principal)
    person = session.get(Principal, principal.id)
    assert person is not None  # nosec B101 -- a verified session's person exists
    refusal = links.refusal_for(person)
    if refusal is not None:
        raise refusal
    return _open_flow(
        request,
        response,
        session,
        "link",
        body.return_to,
        principal_id=principal.id,
        session_id=context.session_id,
    )


@router.post("/step-up/start", response_model=OidcStartOut, responses=errors(401, 403))
def start_oidc_step_up(
    body: OidcInSessionStartIn,
    request: Request,
    response: Response,
    principal: PrincipalRef = Depends(get_principal),
    session: Session = Depends(get_session),
) -> OidcStartOut:
    """Start confirming your browser session at the identity provider (provider step-up).

    For a person linked to the provider, who signs in only there: it stands in for the passkey
    touch that actions such as creating a token ask for (403 `StepUpRequired`). The provider is
    asked for a fresh sign-in (`prompt=login`, `max_age=0`), and the finish checks the
    `auth_time` it reports. Then press the action again: for 15 minutes it needs no other
    confirmation. 409 for a token, or for a person who is not linked (confirm with a passkey,
    `POST /api/v1/sign-in/step-up/options`).

    Parameters
    ----------
    body
        ``{"return_to"}``, optional: the page to come back to.
    request
        The request, for the provider client and the clock.
    response
        The response, to set the flow cookie on.
    principal
        The caller.
    session
        The request's database session.

    Returns
    -------
    OidcStartOut
        The provider's authorization URL for this flow.

    Raises
    ------
    ConflictError
        409 for a person who is not linked.
    """
    _provider(request)
    context = in_browser_session(principal)
    person = session.get(Principal, principal.id)
    if person is None or not is_linked(person):
        raise ConflictError(NOT_LINKED_STEP_UP)
    return _open_flow(
        request,
        response,
        session,
        "step_up",
        body.return_to,
        principal_id=principal.id,
        session_id=context.session_id,
        extra=STEP_UP_PARAMETERS,
    )


@router.post(
    "/finish-in-session",
    response_model=OidcInSessionOut,
    responses=errors(401, 403),
)
def finish_oidc_in_session(
    request: Request,
    response: Response,
    principal: PrincipalRef = Depends(get_principal),
    session: Session = Depends(get_session),
    sessions: SessionService = Depends(get_session_service),
    links: OidcLinkService = Depends(get_oidc_link_service),
) -> OidcInSessionOut:
    """Finish a link from the profile or a provider step-up, in the session that started it.

    The interface's page at `/sign-in/oidc/confirmed` calls this once, with the session's
    CSRF token: the session stays, and so does its token. A link links the caller to the
    identity the provider verified (403 `OidcSignInRefused` `identity_taken` when another
    account holds it, `last_unlinked_admin`). A step-up needs the caller's own identity
    (`identity_mismatch` otherwise) and an `auth_time` no earlier than the start, less 60
    seconds (`auth_too_old`, also when the provider sent none); it then counts as the
    session's step-up touch, and as a provider sign-in for the token window. Also 403
    `flow_expired` for a flow of another session, and the callback's refusals. 409 for a token
    and for a sign-in flow, which finishes at `/finish`.

    Parameters
    ----------
    request
        The request, for the flow cookie and the clock.
    response
        The response, to clear the flow cookie on.
    principal
        The caller.
    session
        The request's database session.
    sessions
        The session service, to record the step-up touch.
    links
        The link service.

    Returns
    -------
    OidcInSessionOut
        The page to open and until when step-up actions need no other confirmation.

    Raises
    ------
    ConflictError
        409 for a sign-in, invite or recovery flow; it stays usable at `/finish`.
    OidcSignInRefused
        403 with the reason.
    """
    _provider(request)
    context = in_browser_session(principal)
    origin = request.app.state.settings.public_origin()
    clock = get_clock(request)
    browser = (request.cookies.get(flow_cookie_name(origin)) or "")[:MAX_PARAMETER]
    store = OidcFlowStore(session, clock=clock)
    if store.pending_purpose(browser) in SIGN_IN_PURPOSES:
        raise ConflictError(SIGN_IN_FLOW)
    flow = store.finish(browser, purposes=IN_SESSION_PURPOSES)
    found = sign_in_outcome(flow.outcome if flow is not None else None)
    session.commit()
    if not isinstance(found, ProviderIdentity):
        if flow is None or flow.outcome is None:
            record_refused_oidc(request, session, found)
        raise OidcSignInRefused(found)
    assert flow is not None  # nosec B101 -- an identity comes only from a flow's outcome
    if flow.session_id != context.session_id or flow.principal_id != principal.id:
        record_refused_oidc(request, session, "flow_expired", iss=found.issuer, sub=found.subject)
        raise OidcSignInRefused("flow_expired")
    touched = context.last_touch_at
    try:
        if flow.purpose == "link":
            links.link(principal.id, found, via="profile")
        else:
            touched = _confirm(request, session, sessions, principal, found, flow.created_at)
    except OidcSignInRefused as refused:
        record_refused_oidc(request, session, refused.reason, iss=found.issuer, sub=found.subject)
        raise
    session.commit()
    clear_flow_cookie(response, origin)
    return OidcInSessionOut(
        return_to=flow.return_to,
        step_up_until=touched + STEP_UP_WINDOW if touched is not None else None,
    )


def _confirm(
    request: Request,
    session: Session,
    sessions: SessionService,
    principal: PrincipalRef,
    found: ProviderIdentity,
    started_at: datetime,
) -> datetime:
    """Record a provider step-up as the session's touch; the caller commits.

    Parameters
    ----------
    request
        The request, for the clock and the client's address.
    session
        The request's database session.
    sessions
        The session service.
    principal
        The caller, in their browser session.
    found
        The identity the provider verified.
    started_at
        When the step-up flow was opened.

    Returns
    -------
    datetime
        The touch time.

    Raises
    ------
    OidcSignInRefused
        ``identity_mismatch`` for another identity than the caller's, ``auth_too_old`` without
        an `auth_time` from the start on (less `AUTH_TIME_SKEW`).
    """
    person = session.get(Principal, principal.id)
    assert person is not None  # nosec B101 -- a verified session's person exists
    context = in_browser_session(principal)
    if (person.oidc_issuer, person.oidc_subject) != (found.issuer, found.subject):
        raise OidcSignInRefused("identity_mismatch")
    if found.auth_time is None or found.auth_time < started_at - AUTH_TIME_SKEW:
        raise OidcSignInRefused("auth_too_old")
    now = get_clock(request)()
    sessions.touch(context.key_id)
    person.last_provider_sign_in_at = now
    person.oidc_email, person.oidc_name = found.email, found.name
    AuditService(session).record(
        principal.id,
        AuditEventType.SIGN_IN_OIDC,
        detail={
            "iss": found.issuer,
            "sub": found.subject,
            "key_id": context.key_id,
            "peer": request.client.host if request.client else "unknown",
            "purpose": "step_up",
        },
    )
    return now
