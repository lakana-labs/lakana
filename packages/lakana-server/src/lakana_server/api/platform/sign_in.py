"""Sign-in links of the platform API: describe and use a link, and the operator's own links.

`POST /api/v1/sign-in/links/describe` and `/links/redeem` need no credential: the link is the
proof. The guard (`lakana_server.browser_guard`) checks their `Host` and `Origin` before they
run, a refused use is audited through the per-address limiter of failed sign-ins, and neither
route nor the access log ever writes a request body, so the secret never reaches a log. A
wrong secret answers exactly like an unknown link (410, `state: unknown`).

`POST /api/v1/sign-in/setup-link` is the server operator's (the bootstrap token's) and
`POST /api/v1/sign-in/local-link` a person's own token's, for `lakana server start --local`.
Every route answers 409 while the interface is off.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.errors import AuthenticationError, LinkUnusableError
from lakana_core.identity.links import LinkService
from lakana_core.identity.sessions import SessionService
from lakana_server.api.platform.schemas import (
    IssuedLinkOut,
    LinkDescriptionOut,
    LinkIdIn,
    LinkRedeemIn,
    SignInOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.browser_session import cookie_name, require_interface, set_session_cookie
from lakana_server.deps import (
    get_clock,
    get_link_service,
    get_principal,
    get_session,
    get_session_service,
    record_failed_link,
)

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/sign-in",
    tags=["sign-in"],
    dependencies=[Depends(require_interface)],
    responses=errors(409),
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
