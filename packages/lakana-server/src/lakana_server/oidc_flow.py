"""The OIDC sign-in's protocol steps: the authorization URL, the code exchange, the callback.

Authlib builds the protocol messages, and only here (an import contract keeps it out of every
other package): the authorization URL with its PKCE challenge (S256), and the body of the token
request. Its HTTP client is never used: the token request goes through the provider client's
guarded POST (`lakana_core.oidc.provider.OidcProvider.post`, over
`lakana_core.net.https_json.post_form_json`), like discovery and the keys.

The callback runs every check, in order, and writes what it found on the flow; it never starts a
session (that is `finish`, a same-origin POST that can return the CSRF token):

1. the flow: the ``state`` and the browser's flow cookie must name one open, unexpired flow that
   was not called back (`lakana_core.identity.oidc_flows.OidcFlowStore.called_back`); it is
   marked called back and committed at once, so a replayed callback finds nothing;
2. the provider's ``error``, if it sent one;
3. the code exchange, with the client secret (``client_secret_basic`` unless the provider lists
   only ``client_secret_post``) and the PKCE verifier;
4. the ID token, by the shared verifier with the sign-in's rules
   (`lakana_core.oidc.provider.OidcProvider.verify`): signature, ``iss``, ``aud``, ``azp``,
   ``exp``, ``iat``, ``nonce`` and ``typ``. Only the ``id_token`` member is ever read.

Each refusal is audited (`lakana_server.deps.record_refused_oidc`, rate limited) with its reason.
No step logs the code, a token, the state or the client secret.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, quote_plus

from authlib.oauth2.rfc6749.parameters import prepare_grant_uri, prepare_token_request
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import Request
from sqlalchemy.orm import Session

from lakana_core.errors import OidcRefusal, OidcSignInRefused
from lakana_core.identity.oidc_flows import IN_SESSION_PURPOSES, OidcFlowStore
from lakana_core.net.https_json import FetchFailed
from lakana_core.oidc.config import OidcSignInConfig
from lakana_core.oidc.errors import IdTokenRefused
from lakana_core.oidc.provider import OidcProvider, ProviderMetadata, ProviderUnavailable
from lakana_server.browser_session import flow_cookie_name
from lakana_server.deps import get_clock, record_refused_oidc
from lakana_server.settings import PublicOrigin

_log = logging.getLogger(__name__)

PROVIDER_ERRORS = frozenset(
    {
        "invalid_request",
        "unauthorized_client",
        "access_denied",
        "unsupported_response_type",
        "invalid_scope",
        "server_error",
        "temporarily_unavailable",
        "interaction_required",
        "login_required",
        "account_selection_required",
        "consent_required",
        "invalid_request_uri",
        "invalid_request_object",
        "request_not_supported",
        "request_uri_not_supported",
        "registration_not_supported",
    }
)
"""The error codes of RFC 6749 and OpenID Connect Core that the callback names in its log line;
any other value of the ``error`` parameter is the browser's to forge, so it is never quoted."""

CALLBACK_PATH = "/api/v1/sign-in/oidc/callback"
"""Where the provider sends the browser back; registered with the provider as the redirect URI."""

LANDING_PATH = "/sign-in/oidc"
"""The interface's page that finishes a sign-in, an invite or a recovery link."""

CONFIRMED_PATH = "/sign-in/oidc/confirmed"
"""The interface's page that finishes a link from the profile or a provider step-up."""

LINK_PARAMETERS: dict[str, str] = {"prompt": "login", "max_age": "0"}
"""What a flow that links an identity adds to the authorization request (an invite, a recovery
link, a link from the profile): a fresh sign-in at the provider, so the person sees which
identity they bind, never one a shared browser's provider session answers silently. A provider
may ignore the request, so the finish checks ``auth_time`` as provider step-up does."""

STEP_UP_PARAMETERS: dict[str, str] = {"prompt": "login", "max_age": "0"}
"""What a provider step-up adds to the authorization request: a fresh sign-in at the provider,
never one its own session answers silently; the finish checks ``auth_time``."""

MAX_PARAMETER = 4096
"""The longest ``code``, ``state`` or ``error`` the callback reads; a longer one is absent."""

TOKEN_UNREACHABLE = (  # nosec B105 -- a sentence for an operator, not a credential
    "the identity provider's token endpoint could not be reached; check the server's outbound "
    "https access to it and the server's log"
)


def redirect_uri(origin: PublicOrigin) -> str:
    """Name the redirect URI registered with the provider.

    Parameters
    ----------
    origin
        The public address (`LAKANA_UI__PUBLIC_URL`).

    Returns
    -------
    str
        ``<public_url>/api/v1/sign-in/oidc/callback``.
    """
    return origin.origin + CALLBACK_PATH


def landing_url(origin: PublicOrigin, purpose: str | None = None) -> str:
    """Name the interface's page a callback redirects to.

    Parameters
    ----------
    origin
        The public address.
    purpose
        The flow's purpose, when the callback found one.

    Returns
    -------
    str
        ``<public_url>/sign-in/oidc/confirmed`` for a link or a step-up, otherwise
        ``<public_url>/sign-in/oidc``: either a constant, never anything the request named.
    """
    if purpose in IN_SESSION_PURPOSES:
        return origin.origin + CONFIRMED_PATH
    return origin.origin + LANDING_PATH


def authorization_url(
    meta: ProviderMetadata,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    nonce: str,
    code_verifier: str,
    extra: Mapping[str, str] | None = None,
) -> str:
    """Build the provider's sign-in URL for one flow (authorization-code flow with PKCE).

    Parameters
    ----------
    meta
        The provider's checked metadata.
    client_id
        The client id.
    redirect_uri
        The registered redirect URI.
    scopes
        The scopes to ask for.
    state
        The flow's state.
    nonce
        The flow's nonce.
    code_verifier
        The flow's PKCE verifier; only its S256 challenge goes into the URL.
    extra
        Further parameters, such as ``prompt`` and ``max_age``.

    Returns
    -------
    str
        The authorization endpoint with ``response_type=code``, the client id, the redirect URI,
        the scopes, the state, the nonce and the challenge.
    """
    url: str = prepare_grant_uri(
        meta.authorization_endpoint,
        client_id,
        "code",
        redirect_uri=redirect_uri,
        scope=" ".join(scopes),
        state=state,
        nonce=nonce,
        code_challenge=create_s256_code_challenge(code_verifier),
        code_challenge_method="S256",
        **dict(extra or {}),
    )
    return url


def _basic(client_id: str, client_secret: str) -> str:
    """Encode the client's credentials for ``client_secret_basic`` (RFC 6749, 2.3.1).

    Parameters
    ----------
    client_id
        The client id.
    client_secret
        The client secret.

    Returns
    -------
    str
        ``Basic <base64(urlencoded id ":" urlencoded secret)>``.
    """
    pair = f"{quote_plus(client_id)}:{quote_plus(client_secret)}"
    return "Basic " + base64.b64encode(pair.encode("utf-8")).decode("ascii")


def _log_token_failure(config: OidcSignInConfig, reason: str, status: int | None) -> None:
    """Log why the token request failed, once, with the next step.

    Parameters
    ----------
    config
        The sign-in settings, for the issuer.
    reason
        The failure's fixed reason (`lakana_core.net.https_json.FetchFailed.reason`); never a
        URL, a code, a token or the client secret.
    status
        The status of the answer, or `None` when there was none.
    """
    if status is not None and 400 <= status < 500:
        _log.warning(
            "the OIDC sign-in token endpoint of %s refused the code (%d); check that "
            "LAKANA_OIDC__CLIENT_ID and LAKANA_OIDC__CLIENT_SECRET are the client's at the "
            "provider and that the redirect URI of this server is registered with it",
            config.issuer,
            status,
        )
    elif status is not None:
        _log.warning(
            "the OIDC sign-in token endpoint of %s answered %d; the provider failed, so check "
            "the provider's own log and try again",
            config.issuer,
            status,
        )
    else:
        _log.warning(
            "could not reach the OIDC sign-in token endpoint of %s: %s; check "
            "LAKANA_OIDC__ALLOW_HOSTS (its host must be the issuer's or listed), the server's "
            "name resolution and its outbound https access (LAKANA_OIDC__PROXY, "
            "LAKANA_OIDC__CA_BUNDLE)",
            config.issuer,
            reason,
        )


def exchange_code(
    provider: OidcProvider,
    config: OidcSignInConfig,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> str:
    """Exchange an authorization code for the ID token, at the provider's token endpoint.

    Parameters
    ----------
    provider
        The provider client.
    config
        The sign-in settings, for the client id and secret.
    code
        The callback's code.
    code_verifier
        The flow's PKCE verifier.
    redirect_uri
        The registered redirect URI, sent again as the token request requires.

    Returns
    -------
    str
        The answer's ``id_token``; nothing else of the answer is read.

    Raises
    ------
    ProviderUnavailable
        If the metadata cannot be read or the token endpoint cannot be reached or answers
        anything but 200 or a 4xx.
    OidcSignInRefused
        ``provider_refused``, if the token endpoint refuses the code (4xx) or answers no ID
        token.
    """
    meta = provider.metadata()
    body = prepare_token_request(
        "authorization_code", code=code, redirect_uri=redirect_uri, code_verifier=code_verifier
    )
    form = dict(parse_qsl(body, keep_blank_values=True))
    headers: dict[str, str] = {}
    client_id = config.client_id or ""
    secret = config.client_secret.get_secret_value() if config.client_secret else ""
    if meta.token_auth == "client_secret_basic":  # nosec B105 -- a method name, not a secret
        headers["Authorization"] = _basic(client_id, secret)
    else:
        form.update({"client_id": client_id, "client_secret": secret})
    answer: object = None
    status: int | None = None
    reason = ""
    failed = False
    try:
        answer = provider.post(meta.token_endpoint, form, headers)
    except FetchFailed as error:
        failed, status, reason = True, error.status, error.reason
    if failed:
        _log_token_failure(config, reason, status)
    if failed and status is not None and 400 <= status < 500:
        raise OidcSignInRefused("provider_refused")
    if failed:
        raise ProviderUnavailable(TOKEN_UNREACHABLE)
    id_token = answer.get("id_token") if isinstance(answer, dict) else None
    if not isinstance(id_token, str) or not id_token:
        _log.warning(
            "the OIDC sign-in token endpoint of %s answered no ID token; check that the client "
            "may ask for the openid scope (LAKANA_OIDC__SCOPES) at the provider",
            config.issuer,
        )
        raise OidcSignInRefused("provider_refused")
    return id_token


def _parameter(value: str | None) -> str:
    """Read one callback parameter, bounded.

    Parameters
    ----------
    value
        The query parameter, or `None`.

    Returns
    -------
    str
        The value, or empty when it is missing or longer than `MAX_PARAMETER`.
    """
    if value is None or len(value) > MAX_PARAMETER:
        return ""
    return value


def _shown(claims: Mapping[str, object], name: str, limit: int) -> str | None:
    """Read a claim kept for showing only.

    Parameters
    ----------
    claims
        The verified claims.
    name
        ``email`` or ``name``.
    limit
        The column's length.

    Returns
    -------
    str or None
        The text, cut to `limit`; `None` when it is not a text.
    """
    value = claims.get(name)
    return value[:limit] if isinstance(value, str) and value else None


def _auth_time(claims: Mapping[str, object]) -> int | None:
    """Read ``auth_time``, when the provider sent one.

    Parameters
    ----------
    claims
        The verified claims.

    Returns
    -------
    int or None
        Seconds since the epoch, or `None`.
    """
    value = claims.get("auth_time")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _verified_outcome(
    provider: OidcProvider,
    *,
    code: str,
    code_verifier: str,
    nonce: str,
    redirect: str,
    now: datetime,
) -> dict[str, Any]:
    """Exchange the code and verify the ID token; say what was found.

    Parameters
    ----------
    provider
        The provider client.
    code
        The callback's code.
    code_verifier
        The flow's PKCE verifier.
    nonce
        The flow's nonce.
    redirect
        The registered redirect URI.
    now
        The time of the check.

    Returns
    -------
    dict
        ``{"ok": true, "iss", "sub", "email", "name", "auth_time"}``, or ``{"ok": false,
        "reason"}`` with ``provider_unreachable``, ``provider_refused`` or ``token_invalid``.
    """
    reason: OidcRefusal = "token_invalid"
    verified = None
    try:
        id_token = exchange_code(
            provider, provider.config, code=code, code_verifier=code_verifier, redirect_uri=redirect
        )
        verified = provider.verify(id_token, nonce=nonce, now=now)
    except ProviderUnavailable:
        reason = "provider_unreachable"
    except OidcSignInRefused as refused:
        reason = refused.reason
    except IdTokenRefused:
        reason = "token_invalid"
    if verified is None:
        return {"ok": False, "reason": reason}
    claims = verified.claims
    return {
        "ok": True,
        "iss": verified.issuer,
        "sub": verified.subject,
        "email": _shown(claims, "email", 320),
        "name": _shown(claims, "name", 255),
        "auth_time": _auth_time(claims),
    }


def _log_provider_refusal(config: OidcSignInConfig, error: str) -> None:
    """Log that the provider sent the browser back with an error or no code.

    This is a person who declined, or a client the provider does not accept, so it is an
    information line, not a warning.

    Parameters
    ----------
    config
        The sign-in settings, for the issuer.
    error
        The ``error`` parameter; quoted only when it is a code of `PROVIDER_ERRORS`.
    """
    if error in PROVIDER_ERRORS:
        _log.info(
            "the OIDC sign-in provider %s sent the browser back with the error %s; the person "
            "may have declined, otherwise check the client's settings at the provider",
            config.issuer,
            error,
        )
    elif error:
        _log.info(
            "the OIDC sign-in provider %s sent the browser back with an error of its own",
            config.issuer,
        )
    else:
        _log.info(
            "the OIDC sign-in provider %s sent the browser back without a code", config.issuer
        )


def complete_callback(
    request: Request,
    session: Session,
    *,
    code: str | None,
    state: str | None,
    error: str | None,
) -> str | None:
    """Run the callback's checks and write the outcome on the flow.

    Parameters
    ----------
    request
        The callback request: its flow cookie, the provider client and the clock.
    session
        The request's database session; committed here.
    code
        The ``code`` parameter.
    state
        The ``state`` parameter.
    error
        The ``error`` parameter, when the provider refused.

    Returns
    -------
    str or None
        The purpose of the flow called back, for the page to send the browser to; `None` when
        no flow matched.
    """
    provider: OidcProvider | None = request.app.state.oidc_provider
    if provider is None:
        return None
    origin = request.app.state.settings.public_origin()
    clock = get_clock(request)
    store = OidcFlowStore(session, clock=clock)
    browser = request.cookies.get(flow_cookie_name(origin)) or ""
    flow = store.called_back(_parameter(state), browser[:MAX_PARAMETER])
    if flow is None:
        record_refused_oidc(request, session, "flow_expired")
        return None
    purpose = flow.purpose
    session.commit()
    given = _parameter(code)
    sent = _parameter(error)
    if sent or not given:
        _log_provider_refusal(provider.config, sent)
        outcome: dict[str, Any] = {"ok": False, "reason": "provider_refused"}
    else:
        outcome = _verified_outcome(
            provider,
            code=given,
            code_verifier=flow.code_verifier,
            nonce=flow.nonce,
            redirect=redirect_uri(origin),
            now=clock(),
        )
    store.record(flow.id, outcome)
    session.commit()
    if not outcome["ok"]:
        record_refused_oidc(request, session, outcome["reason"])
    return purpose
