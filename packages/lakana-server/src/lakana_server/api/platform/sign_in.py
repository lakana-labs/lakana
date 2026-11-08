"""Sign-in of the platform API: links, passkey sign-in and step-up, and the operator's links.

`POST /api/v1/sign-in/links/describe` and `/links/redeem` need no credential: the link is the
proof. The guard (`lakana_server.browser_guard`) checks their `Host` and `Origin` before they
run, a refused use is audited through the per-address limiter of failed sign-ins, and neither
route nor the access log ever writes a request body, so the secret never reaches a log. A
wrong secret answers exactly like an unknown link (410, `state: unknown`).

`POST /api/v1/sign-in/setup-link` is the server operator's (the bootstrap token's) and
`POST /api/v1/sign-in/local-link` a person's own token's, for `lakana server start --local`.

`POST /api/v1/sign-in/passkey/options` and `/passkey` sign in with a passkey and need no
credential either: the passkey is the proof. They read at most `OPEN_BODY_LIMIT` (16 KiB) of
body, the options route answers at most `LAKANA_UI__PASSKEY_OPTIONS_PER_IP_PER_MINUTE` calls
per client address (IPv6: per /64) and minute (429 past that), a refused sign-in is audited like
a refused link, and neither the browser's answer nor the challenge is logged.
`POST /api/v1/sign-in/step-up/options` and `/step-up` confirm the caller's own browser session
with a passkey touch.
Every route answers 409 while the interface is off.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.errors import AuthenticationError, LinkUnusableError
from lakana_core.identity.links import LinkService
from lakana_core.identity.passkeys import OPEN_BODY_LIMIT, PasskeyService
from lakana_core.identity.sessions import STEP_UP_WINDOW, SessionService
from lakana_server.api.platform.schemas import (
    IssuedLinkOut,
    LinkDescriptionOut,
    LinkIdIn,
    LinkRedeemIn,
    PasskeyAnswerIn,
    PasskeyOptionsOut,
    SignInOut,
    StepUpOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.browser_session import cookie_name, require_interface, set_session_cookie
from lakana_server.deps import (
    get_clock,
    get_link_service,
    get_passkey_service,
    get_principal,
    get_session,
    get_session_service,
    record_failed_link,
    record_failed_passkey,
)
from lakana_server.errors import PayloadTooLargeError, TooManyAttemptsError


def client_bucket(host: str) -> str:
    """Name the rate-limit bucket of a client address.

    Parameters
    ----------
    host
        The client's address as the server sees it.

    Returns
    -------
    str
        The address itself, or for IPv6 its /64 network: one network hands out its addresses
        freely, so counting each would give a client unlimited buckets.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return host


TOO_MANY_ATTEMPTS = "Too many sign-in attempts from this address; wait a minute."
"""The 429 detail of the passkey sign-in options route past its per-client rate."""


class OpenSignInRoute(AuthenticateFirstRoute):
    """A route that takes no credential and reads at most `OPEN_BODY_LIMIT` of body.

    The body is counted before FastAPI decodes it; the app's own limit
    (`LAKANA_REQUEST_BODY__MAX_BYTES`) has already bounded what was buffered.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the handler in the body check.

        Returns
        -------
        Callable
            A handler that answers 413 for a body over `OPEN_BODY_LIMIT`, then runs the original.
        """
        original = super().get_route_handler()

        async def limited(request: Request) -> Response:
            """Refuse a large body, then run the original handler.

            Parameters
            ----------
            request
                The incoming request.

            Returns
            -------
            Response
                The original handler's response.

            Raises
            ------
            PayloadTooLargeError
                Mapped to 413, when the body is over `OPEN_BODY_LIMIT`.
            """
            if len(await request.body()) > OPEN_BODY_LIMIT:
                raise PayloadTooLargeError(
                    f"A passkey sign-in request is at most {OPEN_BODY_LIMIT} bytes; send only "
                    "ceremony_id and the browser's credential."
                )
            return await original(request)

        return limited


router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/sign-in",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(409),
)

open_router = APIRouter(
    route_class=OpenSignInRoute,
    prefix="/api/v1/sign-in",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(409, 413),
)


@router.post("/links/describe", response_model=LinkDescriptionOut, responses=errors(403, 410))
def describe_link(
    body: LinkIdIn,
    request: Request,
    links: LinkService = Depends(get_link_service),
    session: Session = Depends(get_session),
) -> LinkDescriptionOut:
    """Describe a sign-in link before it is used.

    Needs no credential and never uses the link: the sign-in page shows "Sign in as X" (or
    "Join as X") from this, so a link preview or a mail scanner opening the page cannot use
    it. Takes only the public id (`l=` after the `#`), never the secret. A used, expired,
    withdrawn or unknown link answers 410 with `state` and what to do next.

    Parameters
    ----------
    body
        ``{"link_id"}``: the link's public id.
    request
        The request, for the audit limiter.
    links
        The link service for this request.
    session
        The request's database session, for the audit row of a refusal.

    Returns
    -------
    LinkDescriptionOut
        The kind, the name it signs in as and ``state: open``.

    Raises
    ------
    LinkUnusableError
        410 when the link cannot be used, with its state.
    """
    try:
        described = links.describe(body.link_id)
        if described.state != "open":
            raise LinkUnusableError(described.state)
    except LinkUnusableError:
        record_failed_link(request, session, body.link_id)
        raise
    return LinkDescriptionOut(
        kind=described.kind,
        name=described.name,
        display_name=described.display_name,
        state=described.state,
    )


@router.post("/links/redeem", response_model=SignInOut, responses=errors(403, 410))
def redeem_link(
    body: LinkRedeemIn,
    request: Request,
    response: Response,
    links: LinkService = Depends(get_link_service),
    sessions: SessionService = Depends(get_session_service),
    session: Session = Depends(get_session),
) -> SignInOut:
    """Sign in with a link.

    Needs no credential: the link is the proof, and it works once. Starts a browser session
    with a fresh id (a session the browser had is ended) and sets its cookie. An invite
    creates the account first, with no rights. After a setup, invite or recovery link on an
    account without a passkey, `needs_passkey` is true: register one now. After a local link
    `offer_passkey` may be true instead. A local link is not used while the browser is signed
    in (409); a used, expired, withdrawn or unknown link, or a wrong secret, answers 410 with
    `state`.

    Parameters
    ----------
    body
        ``{"link_id", "secret"}``: both halves from after the `#` of the link.
    request
        The request, for the cookie, the client's address and the `User-Agent`.
    response
        The response, to set the session cookie on.
    links
        The link service for this request.
    sessions
        The session service, to tell whether the browser's session is open.
    session
        The request's database session, for the audit row of a refusal.

    Returns
    -------
    SignInOut
        Who is signed in, the passkey flags and the session's CSRF token.

    Raises
    ------
    LinkUnusableError
        410 when the link cannot be used, with its state.
    """
    origin = request.app.state.settings.public_origin()
    presented = request.cookies.get(cookie_name(origin)) or None
    has_session = False
    if presented is not None:
        try:
            sessions.verify(presented, extend=False)
            has_session = True
        except AuthenticationError:
            has_session = False
    try:
        redeemed = links.redeem(
            body.link_id,
            body.secret,
            replacing=presented,
            peer=request.client.host if request.client else "unknown",
            user_agent=request.headers.get("user-agent", ""),
            has_session=has_session,
        )
    except LinkUnusableError:
        record_failed_link(request, session, body.link_id)
        raise
    started = redeemed.started
    lifetime = started.absolute_expires_at - get_clock(request)()
    set_session_cookie(
        response, origin, started.cookie_value, max(int(lifetime.total_seconds()), 0)
    )
    return SignInOut(
        name=started.principal.name,
        needs_passkey=redeemed.needs_passkey,
        offer_passkey=redeemed.offer_passkey,
        csrf_token=started.csrf_token,
    )


@router.post("/setup-link", response_model=IssuedLinkOut, responses=errors(401, 403))
def issue_setup_link(
    principal: PrincipalRef = Depends(get_principal),
    links: LinkService = Depends(get_link_service),
) -> IssuedLinkOut:
    """Issue the setup link that signs the first administrator in.

    Only the server operator's bootstrap token (the one the server wrote to
    `LAKANA_BOOTSTRAP_TOKEN_FILE` or was given) may ask, and only while the first
    administrator has no passkey; afterwards 409 names a recovery link instead. The link
    works once, for 10 minutes; earlier open setup links are withdrawn.

    Parameters
    ----------
    principal
        The authenticated caller.
    links
        The link service for this request.

    Returns
    -------
    IssuedLinkOut
        The link, shown only here.
    """
    issued = links.issue_setup(principal)
    return IssuedLinkOut(url=issued.url, expires_at=issued.expires_at)


@router.post("/local-link", response_model=IssuedLinkOut, responses=errors(401, 403, 404))
def issue_local_link(
    principal: PrincipalRef = Depends(get_principal),
    links: LinkService = Depends(get_link_service),
) -> IssuedLinkOut:
    """Issue a sign-in link for yourself, with your own token, on your own machine.

    Only while local sign-in is on (`LAKANA_UI__LOCAL_SIGN_IN`, which `lakana server start
    --local` sets; 404 otherwise), only with a person's own personal access token (never a
    task, CI or service-account token, and never a browser session). The link works once, for
    10 minutes, signs in without a passkey touch, and its session ends when the token is
    revoked.

    Parameters
    ----------
    principal
        The authenticated caller.
    links
        The link service for this request.

    Returns
    -------
    IssuedLinkOut
        The link, shown only here.
    """
    issued = links.issue_local(principal)
    return IssuedLinkOut(url=issued.url, expires_at=issued.expires_at)


@open_router.post("/passkey/options", response_model=PasskeyOptionsOut, responses=errors(429))
def passkey_sign_in_options(
    request: Request, passkeys: PasskeyService = Depends(get_passkey_service)
) -> PasskeyOptionsOut:
    """Start signing in with a passkey.

    Needs no credential and no name: the options list no passkeys (`allowCredentials` is
    empty), so the browser offers the passkeys it has for this address and the person picks one
    with a touch and a PIN, fingerprint or face. The challenge works once, for 5 minutes. At most
    `LAKANA_UI__PASSKEY_OPTIONS_PER_IP_PER_MINUTE` (60) calls per client address (IPv6: per /64)
    and minute; past that 429.

    Parameters
    ----------
    request
        The request, for the client's address.
    passkeys
        The passkey service for this request.

    Returns
    -------
    PasskeyOptionsOut
        The ceremony id and the options for `navigator.credentials.get`.

    Raises
    ------
    TooManyAttemptsError
        429 past the per-client rate.
    """
    client = request.client.host if request.client else "unknown"
    admitted, _ = request.app.state.passkey_options_limiter.admit(client_bucket(client))
    if not admitted:
        raise TooManyAttemptsError(TOO_MANY_ATTEMPTS)
    ceremony_id, options = passkeys.sign_in_options()
    return PasskeyOptionsOut(ceremony_id=ceremony_id, public_key=options)


@open_router.post("/passkey", response_model=SignInOut, responses=errors(401))
def passkey_sign_in(
    body: PasskeyAnswerIn,
    request: Request,
    response: Response,
    passkeys: PasskeyService = Depends(get_passkey_service),
    session: Session = Depends(get_session),
) -> SignInOut:
    """Sign in with a passkey.

    Send the browser's `credential.toJSON()` for the ceremony the options route started. A
    passkey that verifies starts a browser session with a fresh id (a session the browser had
    is ended), sets its cookie, and counts as a passkey touch for 15 minutes of step-up
    actions. An unknown, disabled or copied passkey, an answer from another address or without
    user verification, or a used or expired challenge answers 401 and is recorded in the audit
    log. A passkey whose signature counter went backwards is disabled (it may have been
    copied) and the person and every administrator are told.

    Parameters
    ----------
    body
        ``{"ceremony_id", "credential"}``.
    request
        The request, for the cookie, the client's address and the `User-Agent`.
    response
        The response, to set the session cookie on.
    passkeys
        The passkey service for this request.
    session
        The request's database session, for the audit row of a refusal.

    Returns
    -------
    SignInOut
        Who is signed in and the session's CSRF token; a passkey sign-in never needs another
        passkey.

    Raises
    ------
    AuthenticationError
        401 when the passkey does not sign in.
    """
    origin = request.app.state.settings.public_origin()
    try:
        started = passkeys.sign_in(
            body.ceremony_id,
            json.dumps(body.answer),
            replacing=request.cookies.get(cookie_name(origin)) or None,
            peer=request.client.host if request.client else "unknown",
            user_agent=request.headers.get("user-agent", ""),
        )
    except AuthenticationError:
        record_failed_passkey(request, session)
        raise
    lifetime = started.absolute_expires_at - get_clock(request)()
    set_session_cookie(
        response, origin, started.cookie_value, max(int(lifetime.total_seconds()), 0)
    )
    return SignInOut(
        name=started.principal.name,
        needs_passkey=False,
        offer_passkey=False,
        csrf_token=started.csrf_token,
    )


@router.post("/step-up/options", response_model=PasskeyOptionsOut, responses=errors(401))
def step_up_options(
    principal: PrincipalRef = Depends(get_principal),
    passkeys: PasskeyService = Depends(get_passkey_service),
) -> PasskeyOptionsOut:
    """Start confirming your browser session with a passkey touch (step-up).

    Some actions in the web interface need a passkey touch no older than 15 minutes, such as
    adding or removing a passkey, issuing invites and recovery links and ending other people's
    sessions; without one they answer 403 `StepUpRequired`. The options list your own
    passkeys. A token has no browser session, so it gets 409: step-up never applies to a
    token's requests.

    Parameters
    ----------
    principal
        The authenticated caller.
    passkeys
        The passkey service for this request.

    Returns
    -------
    PasskeyOptionsOut
        The ceremony id and the options for `navigator.credentials.get`.
    """
    ceremony_id, options = passkeys.step_up_options(principal)
    return PasskeyOptionsOut(ceremony_id=ceremony_id, public_key=options)


@router.post("/step-up", response_model=StepUpOut, responses=errors(401, 403))
def step_up(
    body: PasskeyAnswerIn,
    principal: PrincipalRef = Depends(get_principal),
    passkeys: PasskeyService = Depends(get_passkey_service),
) -> StepUpOut:
    """Confirm your browser session with a touch of one of your passkeys.

    Send the browser's `credential.toJSON()` for the ceremony `/step-up/options` started. Only
    a passkey of the account the session is signed in as counts; another person's, an answer
    from another address or without user verification, or a used or expired challenge answers
    403 `PasskeyRefused` and the session stays as it was.

    Parameters
    ----------
    body
        ``{"ceremony_id", "credential"}``.
    principal
        The authenticated caller.
    passkeys
        The passkey service for this request.

    Returns
    -------
    StepUpOut
        Until when step-up actions need no new touch.
    """
    touched = passkeys.step_up(principal, body.ceremony_id, json.dumps(body.answer))
    return StepUpOut(step_up_until=touched + STEP_UP_WINDOW)
