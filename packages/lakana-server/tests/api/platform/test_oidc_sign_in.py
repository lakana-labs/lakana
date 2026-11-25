"""The OIDC sign-in routes: info, start, callback and finish, against a fake provider.

The provider (`tests.oidc_fake`) serves discovery, keys and a token endpoint without the network;
its client is the server's real one (`OidcProvider`), so the checks run as in production.
"""

from __future__ import annotations

import base64
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote_plus, urlsplit

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana_server.ui
from lakana_core.models import AuditEvent, AuditEventType, OidcFlow, Passkey, Principal
from lakana_core.net.https_json import FetchFailed
from lakana_core.oidc.provider import LOG_THROTTLE, OidcProvider
from lakana_server.app import create_app
from lakana_server.auth_rate_limit import AuthAuditLimiter
from lakana_server.browser_session import flow_cookie_name, set_flow_cookie
from lakana_server.settings import PublicOrigin, Settings, UiConfig
from tests.conftest import AUTH_HEADER, PUBLIC, SettableClock, write_ui_dist
from tests.oidc_fake import (
    AUTHORIZE,
    CLIENT_ID,
    CLIENT_SECRET,
    DISCOVERY,
    ISSUER,
    TOKEN,
    FakeProvider,
    config,
)
from tests.server_test_helpers import session_headers, signed_in

INFO = "/api/v1/sign-in/oidc"
START = "/api/v1/sign-in/oidc/start"
CALLBACK = "/api/v1/sign-in/oidc/callback"
FINISH = "/api/v1/sign-in/oidc/finish"
LANDING = f"{PUBLIC}/sign-in/oidc"
FLOW_COOKIE = "lakana_oidc_8000"


def _app(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build an app with the interface on at `PUBLIC` and OIDC sign-in on.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        For the stand-in build.
    monkeypatch
        Points the interface at the stand-in.

    Returns
    -------
    FastAPI
        The app, not started.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    return create_app(
        settings.model_copy(
            update={"ui": UiConfig(enabled=True, public_url=PUBLIC), "oidc": config()}
        )
    )


@pytest.fixture
def fake() -> FakeProvider:
    """Provide the fake provider.

    Returns
    -------
    FakeProvider
        The provider.
    """
    return FakeProvider()


@pytest.fixture
def client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeProvider
) -> Iterator[TestClient]:
    """Provide a browser at the public address of an app whose provider is the fake.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        For the stand-in build.
    monkeypatch
        Points the interface at the stand-in.
    fake
        The fake provider.

    Yields
    ------
    TestClient
        A started client whose requests carry the public `Origin`.
    """
    app = _app(settings, tmp_path, monkeypatch)
    with TestClient(app, base_url=PUBLIC, headers={"Origin": PUBLIC}) as c:
        app.state.oidc_provider = fake.provider()
        yield c


def _start(client: TestClient, return_to: str | None = None) -> str:
    """Start a flow as the sign-in page does.

    Parameters
    ----------
    client
        The browser.
    return_to
        The page to come back to.

    Returns
    -------
    str
        The authorization URL.
    """
    r = client.post(START, json={} if return_to is None else {"return_to": return_to})
    assert r.status_code == 200, r.text
    return str(r.json()["authorization_url"])


def _callback(client: TestClient, **params: str) -> None:
    """Come back from the provider, checking the redirect.

    Parameters
    ----------
    client
        The browser.
    **params
        The query.
    """
    r = client.get(CALLBACK, params=params, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == LANDING


def _flows(client: TestClient) -> list[OidcFlow]:
    """Read every flow, oldest first.

    Parameters
    ----------
    client
        The client of the app.

    Returns
    -------
    list of OidcFlow
        The rows, detached.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine, expire_on_commit=False) as session:
        return list(session.execute(select(OidcFlow).order_by(OidcFlow.created_at)).scalars())


def _outcome(client: TestClient) -> dict[str, Any] | None:
    """Read the outcome of the newest flow.

    Parameters
    ----------
    client
        The client of the app.

    Returns
    -------
    dict or None
        The outcome.
    """
    return _flows(client)[-1].outcome


def _events(client: TestClient, kind: AuditEventType) -> list[dict[str, Any]]:
    """Read the details of every audit event of one type.

    Parameters
    ----------
    client
        The client of the app.
    kind
        The type.

    Returns
    -------
    list of dict
        The details, oldest first.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        rows = session.execute(
            select(AuditEvent.detail)
            .where(AuditEvent.event_type == kind)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
        return [dict(row) for row in rows]


def _signed_in_at_provider(client: TestClient, fake: FakeProvider, **claims: object) -> None:
    """Run start, the provider's sign-in and the callback.

    Parameters
    ----------
    client
        The browser.
    fake
        The provider.
    **claims
        Claims of the ID token to change.
    """
    code, state = fake.authorize(_start(client), **claims)
    _callback(client, code=code, state=state)


# -- info and start ---------------------------------------------------------------------------


def test_info_is_off_without_an_issuer(ui_client: TestClient) -> None:
    r = ui_client.get(INFO)
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "display_name": None}
    r = ui_client.post(START, json={})
    assert (r.status_code, r.json()["type"]) == (409, "OidcOff")
    assert "LAKANA_OIDC__ISSUER" in r.json()["detail"]


def test_info_names_the_provider(client: TestClient) -> None:
    assert client.get(INFO).json() == {"enabled": True, "display_name": "idp.example.com"}


def test_the_app_builds_the_provider_when_oidc_is_on(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(settings, tmp_path, monkeypatch)
    with TestClient(app, base_url=PUBLIC):
        assert isinstance(app.state.oidc_provider, OidcProvider)
        assert app.state.oidc_provider.config.client_id == CLIENT_ID


def test_start_sets_the_flow_cookie_and_answers_the_authorization_url(
    client: TestClient,
) -> None:
    r = client.post(START, json={"return_to": "/catalog/prod"})
    assert r.status_code == 200, r.text
    url = urlsplit(r.json()["authorization_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == AUTHORIZE
    query = {name: values[0] for name, values in parse_qs(url.query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == f"{PUBLIC}/api/v1/sign-in/oidc/callback"
    assert query["scope"] == "openid profile email"
    assert query["code_challenge_method"] == "S256"
    assert len(query["state"]) >= 43 and len(query["nonce"]) >= 43
    assert len(query["code_challenge"]) == 43
    assert "return_to" not in r.json()["authorization_url"]
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{FLOW_COOKIE}=")
    attributes = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert {"httponly", "samesite=lax", "max-age=600", "path=/"} <= attributes
    assert "secure" not in attributes
    [flow] = _flows(client)
    assert flow.return_to == "/catalog/prod"
    assert flow.code_verifier not in r.text


def test_start_is_409_when_off(client: TestClient) -> None:
    cast(FastAPI, client.app).state.oidc_provider = None
    r = client.post(START, json={})
    assert (r.status_code, r.json()["type"]) == (409, "OidcOff")


def test_start_is_rate_limited(client: TestClient) -> None:
    app = cast(FastAPI, client.app)
    app.state.oidc_start_limiter = AuthAuditLimiter(2)
    assert client.post(START, json={}).status_code == 200
    assert client.post(START, json={}).status_code == 200
    r = client.post(START, json={})
    assert r.status_code == 429
    assert r.headers["retry-after"]
    app.state.oidc_start_limiter = AuthAuditLimiter(10)
    app.state.oidc_start_network_limiter = AuthAuditLimiter(1)
    assert client.post(START, json={}).status_code == 200
    assert client.post(START, json={}).status_code == 429


def test_start_with_an_unreachable_provider_is_refused(
    client: TestClient, fake: FakeProvider
) -> None:
    fake.documents[DISCOVERY] = FetchFailed("the host does not resolve")
    cast(FastAPI, client.app).state.oidc_provider = fake.provider()
    r = client.post(START, json={})
    assert r.status_code == 403
    assert (r.json()["type"], r.json()["reason"]) == ("OidcSignInRefused", "provider_unreachable")
    assert [e["reason"] for e in _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)] == [
        "provider_unreachable"
    ]
    assert _flows(client) == []


def test_start_reads_at_most_16_kib(client: TestClient) -> None:
    r = client.post(START, json={"return_to": "/" + "a" * 17000})
    assert r.status_code == 413
    assert "return_to" in r.json()["detail"]


# -- the callback -----------------------------------------------------------------------------


def test_callback_always_redirects_to_the_public_url(
    client: TestClient, fake: FakeProvider
) -> None:
    code, state = fake.authorize(_start(client, "https://evil.example/"))
    _callback(client, code=code, state=state)
    assert _outcome(client) == {
        "ok": True,
        "iss": ISSUER,
        "sub": "248289761001",
        "email": "alice@example.com",
        "name": "Alice Example",
        "auth_time": None,
    }
    _callback(client, error="access_denied", state=state)
    _callback(client, state="garbage", code="x")
    _callback(client)
    _callback(client, state="s" * 5000, code="c" * 5000)


def test_the_provider_refusing_is_provider_refused(client: TestClient) -> None:
    url = _start(client)
    state = parse_qs(urlsplit(url).query)["state"][0]
    _callback(client, error="access_denied", state=state)
    assert _outcome(client) == {"ok": False, "reason": "provider_refused"}
    events = _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)
    assert [e["reason"] for e in events] == ["provider_refused"]


def test_a_callback_without_a_code_is_provider_refused(client: TestClient) -> None:
    state = parse_qs(urlsplit(_start(client)).query)["state"][0]
    _callback(client, state=state)
    assert _outcome(client) == {"ok": False, "reason": "provider_refused"}


def test_callback_with_another_browsers_state_is_refused(
    client: TestClient,
    fake: FakeProvider,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The attacker starts a flow in their own browser and signs in at the provider...
    code, state = fake.authorize(_start(client))
    attackers_flow = _flows(client)[0].id
    # ...then makes the victim's browser, which has a flow of its own, open the callback.
    client.cookies.clear()
    _start(client)
    _callback(client, code=code, state=state)
    flows = {flow.id: flow for flow in _flows(client)}
    assert all(flow.called_back_at is None and flow.outcome is None for flow in flows.values())
    assert fake.token_requests == []
    events = _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)
    assert [e["reason"] for e in events] == ["flow_expired"]
    # A browser with no flow cookie at all gets nothing either.
    client.cookies.clear()
    _callback(client, code=code, state=state)
    assert _flows(client)[0].id == attackers_flow
    assert all(flow.called_back_at is None for flow in _flows(client))


def test_a_replayed_callback_is_refused(client: TestClient, fake: FakeProvider) -> None:
    code, state = fake.authorize(_start(client))
    _callback(client, code=code, state=state)
    assert (_outcome(client) or {})["ok"] is True
    _callback(client, code=code, state=state)
    assert (_outcome(client) or {})["ok"] is True
    assert len(fake.token_requests) == 1
    events = _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)
    assert [e["reason"] for e in events] == ["flow_expired"]


def test_nonce_mismatch_is_token_invalid(client: TestClient, fake: FakeProvider) -> None:
    _signed_in_at_provider(client, fake, nonce="a-replayed-id-tokens-nonce")
    assert _outcome(client) == {"ok": False, "reason": "token_invalid"}


@pytest.mark.parametrize(
    "claims",
    [
        {"typ": "Bearer", "aud": "account", "azp": None},
        {"aud": "another-client", "azp": None},
        {"aud": [CLIENT_ID, "another-client"], "azp": "another-client"},
        {"aud": [CLIENT_ID, "another-client"], "azp": None},
        {"iss": "https://other.example"},
        {"exp": 1},
    ],
    ids=["access-token", "other-aud", "other-azp", "two-aud-no-azp", "other-iss", "expired"],
)
def test_token_confusion_is_token_invalid(
    client: TestClient, fake: FakeProvider, claims: dict[str, object]
) -> None:
    _signed_in_at_provider(client, fake, **claims)
    assert _outcome(client) == {"ok": False, "reason": "token_invalid"}


def test_unreachable_provider_is_a_reason_not_an_error(
    client: TestClient, fake: FakeProvider
) -> None:
    url = _start(client)
    code, state = fake.authorize(url)
    fake.token_answer = FetchFailed("the server answered 500", status=500)
    _callback(client, code=code, state=state)
    assert _outcome(client) == {"ok": False, "reason": "provider_unreachable"}
    for failure in (
        FetchFailed("the server answered with a redirect, which is not followed"),
        FetchFailed("the answer is larger than 64 KiB"),
        FetchFailed("the host is a loopback, link-local or metadata address"),
        FetchFailed("the request timed out"),
    ):
        fake.token_answer = failure
        code, state = fake.authorize(_start(client))
        _callback(client, code=code, state=state)
        assert _outcome(client) == {"ok": False, "reason": "provider_unreachable"}


def test_a_provider_whose_keys_go_away_is_unreachable(
    client: TestClient, fake: FakeProvider
) -> None:
    fake.documents[DISCOVERY] = {**cast(dict[str, object], fake.documents[DISCOVERY])}
    code, state = fake.authorize(_start(client))
    provider = cast(FastAPI, client.app).state.oidc_provider
    fake.documents.clear()
    # A new client has nothing cached and cannot fetch.
    cast(FastAPI, client.app).state.oidc_provider = fake.provider(provider.config)
    _callback(client, code=code, state=state)
    assert _outcome(client) == {"ok": False, "reason": "provider_unreachable"}


def test_code_rejected_is_provider_refused(client: TestClient, fake: FakeProvider) -> None:
    url = _start(client)
    _, state = fake.authorize(url)
    _callback(client, code="a-code-the-provider-never-issued", state=state)
    assert _outcome(client) == {"ok": False, "reason": "provider_refused"}
    for answer in ({"access_token": "only"}, {"id_token": 7}, ["not", "an", "object"]):
        fake.token_answer = answer
        code, state = fake.authorize(_start(client))
        _callback(client, code=code, state=state)
        assert _outcome(client) == {"ok": False, "reason": "provider_refused"}


def test_the_client_secret_is_sent_basic_and_never_logged(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _signed_in_at_provider(client, fake, nonce="wrong")
    url, form, headers = fake.token_requests[0]
    assert url == TOKEN
    scheme, _, encoded = headers["Authorization"].partition(" ")
    assert scheme == "Basic"
    user, _, password = base64.b64decode(encoded).decode().partition(":")
    assert (unquote_plus(user), unquote_plus(password)) == (CLIENT_ID, CLIENT_SECRET)
    assert form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == f"{PUBLIC}/api/v1/sign-in/oidc/callback"
    assert "client_secret" not in form
    assert len(form["code_verifier"]) >= 43
    events = _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)
    shown = caplog.text + repr(events) + repr(_flows(client))
    assert CLIENT_SECRET not in shown
    assert encoded not in shown
    assert client.get(INFO).text.find(CLIENT_SECRET) == -1


def test_the_client_secret_is_posted_when_basic_is_not_offered(
    client: TestClient, fake: FakeProvider
) -> None:
    document = dict(cast(dict[str, object], fake.documents[DISCOVERY]))
    fake.documents[DISCOVERY] = {
        **document,
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }
    cast(FastAPI, client.app).state.oidc_provider = fake.provider()
    _signed_in_at_provider(client, fake)
    _, form, headers = fake.token_requests[0]
    assert "Authorization" not in headers
    assert (form["client_id"], form["client_secret"]) == (CLIENT_ID, CLIENT_SECRET)
    assert (_outcome(client) or {})["ok"] is True


# -- every failure the operator can fix writes one line to the log (#810) -------------------------


UNUSABLE = f"could not use the OIDC sign-in provider {ISSUER}: the identity provider "


@pytest.fixture(autouse=True)
def _fresh_throttle() -> None:
    """Start each test with every failure line allowed again."""
    LOG_THROTTLE.reset()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Read what the sign-in wrote at warning level or above.

    Parameters
    ----------
    caplog
        The captured log.

    Returns
    -------
    list of str
        The messages, oldest first.
    """
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def _lakana_log(caplog: pytest.LogCaptureFixture) -> str:
    """Read everything Lakana logged, not the test client's own request lines.

    Parameters
    ----------
    caplog
        The captured log.

    Returns
    -------
    str
        The messages, one per line.
    """
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("lakana"))


def _discovery_with(fake: FakeProvider, client: TestClient, **changes: object) -> None:
    """Change the discovery document and give the app a provider client that reads it afresh.

    Parameters
    ----------
    fake
        The provider.
    client
        The client of the app.
    **changes
        Members to set.
    """
    document = dict(cast(dict[str, object], fake.documents[DISCOVERY]))
    fake.documents[DISCOVERY] = {**document, **changes}
    cast(FastAPI, client.app).state.oidc_provider = fake.provider()


def _fail_at_the_token_endpoint(
    client: TestClient, fake: FakeProvider, failure: FetchFailed
) -> None:
    """Run a sign-in whose token request fails.

    Parameters
    ----------
    client
        The browser.
    fake
        The provider.
    failure
        What the token request raises.
    """
    code, state = fake.authorize(_start(client))
    fake.token_answer = failure
    _callback(client, code=code, state=state)


@pytest.mark.parametrize(
    "reason", ["the host is not one this request may reach", "the host name does not resolve"]
)
def test_a_token_endpoint_that_cannot_be_reached_is_logged(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture, reason: str
) -> None:
    _fail_at_the_token_endpoint(client, fake, FetchFailed(reason))
    assert _outcome(client) == {"ok": False, "reason": "provider_unreachable"}
    (line,) = _warnings(caplog)
    assert f"could not reach the OIDC sign-in token endpoint of {ISSUER}: {reason}" in line
    assert "LAKANA_OIDC__ALLOW_HOSTS" in line


def test_a_token_endpoint_that_answers_5xx_is_logged(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    _fail_at_the_token_endpoint(client, fake, FetchFailed("the server answered 503", status=503))
    assert _outcome(client) == {"ok": False, "reason": "provider_unreachable"}
    (line,) = _warnings(caplog)
    assert f"the OIDC sign-in token endpoint of {ISSUER} answered 503" in line
    assert "the provider's own log" in line


def test_a_token_endpoint_that_refuses_the_code_is_logged(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    _fail_at_the_token_endpoint(client, fake, FetchFailed("the server answered 401", status=401))
    assert _outcome(client) == {"ok": False, "reason": "provider_refused"}
    (line,) = _warnings(caplog)
    assert f"the OIDC sign-in token endpoint of {ISSUER} refused the code (401)" in line
    assert "LAKANA_OIDC__CLIENT_SECRET" in line


def test_a_token_endpoint_that_answers_no_id_token_is_logged(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    code, state = fake.authorize(_start(client))
    fake.token_answer = {"access_token": "an-access-token-sentinel"}
    _callback(client, code=code, state=state)
    assert _outcome(client) == {"ok": False, "reason": "provider_refused"}
    (line,) = _warnings(caplog)
    assert f"the OIDC sign-in token endpoint of {ISSUER} answered no ID token" in line
    assert "openid" in line
    assert "an-access-token-sentinel" not in caplog.text


def test_a_provider_that_sends_the_browser_back_with_an_error_is_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    state = parse_qs(urlsplit(_start(client)).query)["state"][0]
    _callback(client, error="invalid_scope", state=state)
    assert _outcome(client) == {"ok": False, "reason": "provider_refused"}
    (line,) = [r.getMessage() for r in caplog.records if r.name == "lakana_server.oidc_flow"]
    assert (
        f"OIDC sign-in provider {ISSUER} sent the browser back with the error invalid_scope" in line
    )
    assert _warnings(caplog) == []


def test_a_provider_error_that_is_no_known_code_is_not_quoted(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    state = parse_qs(urlsplit(_start(client)).query)["state"][0]
    _callback(client, error="forged\nline", state=state)
    shown = _lakana_log(caplog)
    assert (
        f"the OIDC sign-in provider {ISSUER} sent the browser back with an error of its own"
        in shown
    )
    assert "forged" not in shown


def test_a_callback_without_a_code_is_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    state = parse_qs(urlsplit(_start(client)).query)["state"][0]
    _callback(client, state=state)
    (line,) = [r.getMessage() for r in caplog.records if r.name == "lakana_server.oidc_flow"]
    assert f"OIDC sign-in provider {ISSUER} sent the browser back without a code" in line


def test_a_provider_with_no_supported_signing_algorithm_is_logged(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    _discovery_with(fake, client, id_token_signing_alg_values_supported=["ES256"])
    assert client.post(START, json={}).json()["reason"] == "provider_unreachable"
    (line,) = _warnings(caplog)
    assert f"{UNUSABLE}signs ID tokens with no algorithm Lakana supports" in line
    assert "RS256" in line


def test_a_provider_with_no_client_authentication_by_secret_is_logged(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    _discovery_with(fake, client, token_endpoint_auth_methods_supported=["private_key_jwt"])
    assert client.post(START, json={}).json()["reason"] == "provider_unreachable"
    (line,) = _warnings(caplog)
    assert f"{UNUSABLE.rstrip()}'s token endpoint accepts neither client_secret_basic" in line
    assert "client_secret_post" in line


def test_an_endpoint_on_a_host_that_is_not_allowed_is_logged(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    _discovery_with(fake, client, token_endpoint="https://evil.example.net/token")
    assert client.post(START, json={}).json()["reason"] == "provider_unreachable"
    (line,) = _warnings(caplog)
    assert f"{UNUSABLE}names a token_endpoint that is not https" in line
    assert "LAKANA_OIDC__ALLOW_HOSTS" in line
    assert "evil.example.net" not in line


def test_a_discovery_that_cannot_be_fetched_is_logged_once(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    fake.documents[DISCOVERY] = FetchFailed("the host name does not resolve")
    cast(FastAPI, client.app).state.oidc_provider = fake.provider()
    assert client.post(START, json={}).json()["reason"] == "provider_unreachable"
    (line,) = _warnings(caplog)
    assert f"could not fetch the OIDC sign-in keys of {ISSUER}: the host name" in line


def test_a_burst_of_forged_callbacks_logs_a_bounded_number_of_lines(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    for _ in range(12):
        state = parse_qs(urlsplit(_start(client)).query)["state"][0]
        _callback(client, code="a-made-up-code", state=state)
    assert len(_warnings(caplog)) == 1
    for _ in range(12):
        state = parse_qs(urlsplit(_start(client)).query)["state"][0]
        _callback(client, error="access_denied", state=state)
    lines = [r for r in caplog.records if r.name == "lakana_server.oidc_flow"]
    assert len(lines) == 2


def test_several_starts_against_an_unusable_discovery_document_log_one_warning(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    _discovery_with(fake, client, id_token_signing_alg_values_supported=["ES256"])
    for _ in range(5):
        assert client.post(START, json={}).json()["reason"] == "provider_unreachable"
    assert len(_warnings(caplog)) == 1


def test_a_failure_line_comes_again_after_the_interval(
    client: TestClient,
    fake: FakeProvider,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(LOG_THROTTLE, "_clock", lambda: now[0])
    for second in (0.0, 1.0, 61.0):
        now[0] = second
        _fail_at_the_token_endpoint(
            client, fake, FetchFailed("the server answered 503", status=503)
        )
    assert len(_warnings(caplog)) == 2


def test_no_failure_line_holds_a_secret_a_code_a_state_or_a_token(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    original = dict(cast(dict[str, object], fake.documents[DISCOVERY]))
    for failure in (
        FetchFailed("the host name does not resolve"),
        FetchFailed("the server answered 503", status=503),
        FetchFailed("the server answered 400", status=400),
    ):
        code, state = fake.authorize(_start(client))
        seen += [code, state]
        fake.token_answer = failure
        _callback(client, code=code, state=state)
    code, state = fake.authorize(_start(client))
    seen += [code, state]
    fake.token_answer = {"id_token": "", "access_token": "an-access-token-sentinel"}
    _callback(client, code=code, state=state)
    for changes in (
        {"id_token_signing_alg_values_supported": ["ES256"]},
        {"token_endpoint_auth_methods_supported": ["private_key_jwt"]},
        {"token_endpoint": "https://evil.example.net/token"},
    ):
        fake.documents[DISCOVERY] = original
        _discovery_with(fake, client, **changes)
        client.post(START, json={})
    fake.documents[DISCOVERY] = original
    cast(FastAPI, client.app).state.oidc_provider = fake.provider()
    fake.token_answer = None
    for params in ({"error": "invalid_scope"}, {"error": "forged"}, {}):
        state = parse_qs(urlsplit(_start(client)).query)["state"][0]
        _callback(client, state=state, **params)
    shown = _lakana_log(caplog)
    assert shown.count("\n") >= 9
    assert ISSUER in shown
    for forbidden in [CLIENT_SECRET, basic, "an-access-token-sentinel", *seen]:
        assert forbidden not in shown


def test_routes_answer_409_while_the_interface_is_off(settings: Settings) -> None:
    with TestClient(create_app(settings)) as off:
        for method, path in [("GET", INFO), ("POST", START), ("GET", CALLBACK), ("POST", FINISH)]:
            r = off.request(method, path, json={} if method == "POST" else None)
            assert r.status_code == 409, path
            assert "LAKANA_UI__ENABLED" in r.json()["detail"]


def test_a_callback_while_oidc_is_off_changes_nothing(ui_client: TestClient) -> None:
    r = ui_client.get(CALLBACK, params={"code": "c", "state": "s"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, LANDING)
    assert _events(ui_client, AuditEventType.SIGN_IN_OIDC_REFUSED) == []


def test_the_flow_cookie_over_https() -> None:
    origin = PublicOrigin(
        scheme="https",
        host="lakana.example.com",
        port=443,
        origin="https://lakana.example.com",
        loopback=False,
    )
    assert flow_cookie_name(origin) == "__Host-lakana_oidc"
    response = Response()
    set_flow_cookie(response, origin, "browser-secret")
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-lakana_oidc=browser-secret;")
    attributes = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert {"secure", "httponly", "path=/", "samesite=lax", "max-age=600"} <= attributes
    assert not flow_cookie_name(origin).lower().startswith(("lakana_session", "__host-lakana_s"))


# -- finish -----------------------------------------------------------------------------------


def _link(
    client: TestClient, name: str, *, subject: str = "248289761001", **fields: object
) -> None:
    """Create a person and link them to a provider identity.

    Parameters
    ----------
    client
        The client of the app.
    name
        The person's name.
    subject
        The provider's ``sub``; empty leaves the person unlinked.
    **fields
        Further columns to set.
    """
    r = client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    assert r.status_code == 201, r.text
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        person = session.execute(select(Principal).where(Principal.name == name)).scalar_one()
        if subject:
            person.oidc_issuer, person.oidc_subject = ISSUER, subject
        for column, value in fields.items():
            setattr(person, column, value)
        session.commit()


def _person(client: TestClient, name: str) -> Principal:
    """Read a person's row.

    Parameters
    ----------
    client
        The client of the app.
    name
        The name.

    Returns
    -------
    Principal
        The row, detached.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine, expire_on_commit=False) as session:
        return session.execute(select(Principal).where(Principal.name == name)).scalar_one()


def _refused(r: Any, reason: str) -> None:  # ruff: ignore[any-type]
    """Check a finish was refused with a reason and set no session.

    Parameters
    ----------
    r
        The response.
    reason
        The expected reason.
    """
    assert r.status_code == 403, r.text
    assert (r.json()["type"], r.json()["reason"]) == ("OidcSignInRefused", reason)
    assert "lakana_session" not in r.headers.get("set-cookie", "")


@pytest.fixture
def clock(client: TestClient) -> SettableClock:
    """Install a settable clock on the app.

    Parameters
    ----------
    client
        The client of the app.

    Returns
    -------
    SettableClock
        The clock, at the current time.
    """
    settable = SettableClock(datetime.now(UTC))
    cast(FastAPI, client.app).state.clock = settable
    return settable


def test_linked_person_is_signed_in(
    client: TestClient, fake: FakeProvider, clock: SettableClock
) -> None:
    _link(client, "alice")
    code, state = fake.authorize(_start(client, "/catalog/prod?x=1"), auth_time=1_700_000_000)
    _callback(client, code=code, state=state)
    r = client.post(FINISH, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["name"], body["return_to"]) == ("alice", "/catalog/prod?x=1")
    cookies = r.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("lakana_session_8000="))
    value = session_cookie.split(";")[0].split("=", 1)[1]
    assert any(
        c.startswith(f'{FLOW_COOKIE}=""') or "Max-Age=0" in c for c in cookies if FLOW_COOKIE in c
    )
    headers = session_headers("lakana_session_8000", value, body["csrf_token"])
    r = client.get("/api/v1/sign-in/session", headers=headers)
    assert r.status_code == 200, r.text
    current = r.json()
    assert (current["name"], current["via"]) == ("alice", "oidc")
    # A provider sign-in may be silent (the provider's own session answers), so it is no
    # step-up touch: that needs a fresh provider sign-in with prompt=login (#32, PR 2).
    assert current["step_up_until"] is None
    person = _person(client, "alice")
    assert person.last_provider_sign_in_at == clock.now
    assert (person.oidc_email, person.oidc_name) == ("alice@example.com", "Alice Example")
    [event] = _events(client, AuditEventType.SIGN_IN_OIDC)
    assert (event["iss"], event["sub"]) == (ISSUER, "248289761001")
    assert event["key_id"] and event["peer"]
    assert body["csrf_token"] not in repr(event)


def test_unlinked_identity_is_not_linked(client: TestClient, fake: FakeProvider) -> None:
    _link(client, "alice", subject="someone-else")
    _signed_in_at_provider(client, fake)
    r = client.post(FINISH, json={})
    _refused(r, "not_linked")
    assert "invite" in r.json()["detail"]
    [event] = _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)
    assert event == {
        "reason": "not_linked",
        "iss": ISSUER,
        "sub": "248289761001",
        "peer": "testclient",
    }
    assert _events(client, AuditEventType.SESSION_STARTED) == []


def test_same_email_on_another_account_does_not_match(
    client: TestClient, fake: FakeProvider
) -> None:
    _link(client, "mallory", subject="", oidc_email="alice@example.com", oidc_name="Alice Example")
    _signed_in_at_provider(client, fake)
    _refused(client.post(FINISH, json={}), "not_linked")
    assert _events(client, AuditEventType.SESSION_STARTED) == []


def test_deactivated_person_is_refused(client: TestClient, fake: FakeProvider) -> None:
    _link(client, "alice", is_active=False)
    _signed_in_at_provider(client, fake)
    _refused(client.post(FINISH, json={}), "deactivated")
    assert [e["reason"] for e in _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)] == [
        "deactivated"
    ]


def test_finish_without_the_flow_cookie_is_refused(client: TestClient, fake: FakeProvider) -> None:
    _link(client, "alice")
    _signed_in_at_provider(client, fake)
    client.cookies.clear()
    _refused(client.post(FINISH, json={}), "flow_expired")
    assert [e["reason"] for e in _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)] == [
        "flow_expired"
    ]


def test_finish_before_the_callback_is_refused(client: TestClient) -> None:
    _start(client)
    _refused(client.post(FINISH, json={}), "flow_expired")


def test_finish_works_once(client: TestClient, fake: FakeProvider) -> None:
    _link(client, "alice")
    _signed_in_at_provider(client, fake)
    flow_cookie = client.cookies.get(FLOW_COOKIE)
    assert client.post(FINISH, json={}).status_code == 200
    client.cookies.clear()
    client.cookies.set(FLOW_COOKIE, flow_cookie or "")
    _refused(client.post(FINISH, json={}), "flow_expired")
    assert len(_events(client, AuditEventType.SIGN_IN_OIDC)) == 1


def test_a_finish_after_ten_minutes_is_refused(
    client: TestClient, fake: FakeProvider, clock: SettableClock
) -> None:
    _link(client, "alice")
    _signed_in_at_provider(client, fake)
    clock.advance(timedelta(minutes=10, seconds=1))
    _refused(client.post(FINISH, json={}), "flow_expired")


def test_a_refusal_the_callback_found_is_answered_once_and_not_audited_twice(
    client: TestClient, fake: FakeProvider
) -> None:
    _signed_in_at_provider(client, fake, nonce="wrong")
    _refused(client.post(FINISH, json={}), "token_invalid")
    _refused(client.post(FINISH, json={}), "flow_expired")
    reasons = [e["reason"] for e in _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)]
    assert reasons == ["token_invalid", "flow_expired"]


def test_finish_replaces_the_presented_session(client: TestClient, fake: FakeProvider) -> None:
    _link(client, "alice")
    name, old_value, _ = signed_in(client, "alice")
    old_key = old_value.split("_")[0]
    client.cookies.set(name, old_value)
    _signed_in_at_provider(client, fake)
    r = client.post(FINISH, json={})
    assert r.status_code == 200, r.text
    [event] = _events(client, AuditEventType.SIGN_IN_OIDC)
    assert event["key_id"] != old_key
    ended = [e for e in _events(client, AuditEventType.SESSION_ENDED) if e["key_id"] == old_key]
    assert [e["reason"] for e in ended] == ["replaced"]
    flows = _flows(client)
    assert flows[-1].finished_at is not None


def test_refusal_rows_are_rate_limited(client: TestClient) -> None:
    seconds = [0.0]
    limiter = AuthAuditLimiter(10, clock=lambda: seconds[0])
    cast(FastAPI, client.app).state.auth_audit_limiter = limiter
    for _ in range(11):
        _refused(client.post(FINISH, json={}), "flow_expired")
    events = _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)
    assert len(events) == 10
    assert all("suppressed" not in event for event in events)
    seconds[0] += 6.0
    _refused(client.post(FINISH, json={}), "flow_expired")
    assert _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)[-1]["suppressed"] == 1


def test_a_session_answers_like_the_persons_token(client: TestClient, fake: FakeProvider) -> None:
    _link(client, "alice")
    _signed_in_at_provider(client, fake)
    body = client.post(FINISH, json={}).json()
    value = client.cookies.get("lakana_session_8000") or ""
    client.cookies.clear()
    by_session = session_headers("lakana_session_8000", value, body["csrf_token"])
    # The administrator issues the person's token: minting one in the session needs step-up.
    r = client.post("/api/v1/principals/alice/tokens", headers=AUTH_HEADER, json={"name": "laptop"})
    assert r.status_code == 201, r.text
    by_token = {"Authorization": f"Bearer {r.json()['token']}"}
    for path in ("/api/v1/whoami", "/api/v1/principals/alice", "/api/v1/environments"):
        a, b = client.get(path, headers=by_session), client.get(path, headers=by_token)
        assert (a.status_code, a.json()) == (b.status_code, b.json()), path


@pytest.mark.parametrize("auth_time", [None, 1_700_000_000, "now"], ids=["none", "old", "fresh"])
def test_a_provider_sign_in_is_no_step_up_touch(
    client: TestClient, fake: FakeProvider, auth_time: object
) -> None:
    _link(client, "alice")
    if auth_time == "now":
        auth_time = int(datetime.now(UTC).timestamp())
    _signed_in_at_provider(client, fake, auth_time=auth_time)
    body = client.post(FINISH, json={}).json()
    value = client.cookies.get("lakana_session_8000") or ""
    headers = session_headers("lakana_session_8000", value, body["csrf_token"])
    r = client.get("/api/v1/sign-in/session", headers=headers)
    assert (r.status_code, r.json()["step_up_until"]) == (200, None)
    # Minting a token needs step-up: the provider sign-in did not grant it.
    r = client.post("/api/v1/principals/alice/tokens", headers=headers, json={"name": "t"})
    assert (r.status_code, r.json()["type"]) == (403, "StepUpRequired"), r.text


# -- invites and recovery links through the provider (#32, PR 2) -------------------------------

DESCRIBE = "/api/v1/sign-in/links/describe"


def _now() -> int:
    """Read the time as an ``auth_time``: a sign-in at the provider just now.

    Returns
    -------
    int
        Seconds since the epoch.
    """
    return int(datetime.now(UTC).timestamp())


def _halves(url: str) -> tuple[str, str]:
    """Split a sign-in link into its public id and secret.

    Parameters
    ----------
    url
        The link.

    Returns
    -------
    tuple of (str, str)
        The ``l=`` and ``s=`` halves after the ``#``.
    """
    values = dict(item.split("=", 1) for item in url.split("#", 1)[1].split("&"))
    return values["l"], values["s"]


def _invite(client: TestClient, name: str = "carol") -> tuple[str, str, str]:
    """Issue an invite as the operator.

    Parameters
    ----------
    client
        The browser.
    name
        The invited name.

    Returns
    -------
    tuple of (str, str, str)
        The invite's row id, public id and secret.
    """
    r = client.post("/api/v1/invites", headers=AUTH_HEADER, json={"name": name})
    assert r.status_code == 201, r.text
    public_id, secret = _halves(r.json()["url"])
    return r.json()["id"], public_id, secret


def _start_with_link(client: TestClient, purpose: str, public_id: str, secret: str) -> Any:  # ruff: ignore[any-type]
    """Start a flow with an invite or a recovery link.

    Parameters
    ----------
    client
        The browser.
    purpose
        ``invite`` or ``recovery``.
    public_id
        The link's public id.
    secret
        The link's secret.

    Returns
    -------
    Any
        The response.
    """
    return client.post(
        START,
        json={"purpose": purpose, "link_id": public_id, "secret": secret, "return_to": "/x"},
    )


def test_invite_through_the_provider_signs_in_and_links(
    client: TestClient, fake: FakeProvider, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _, public_id, secret = _invite(client)
    r = _start_with_link(client, "invite", public_id, secret)
    assert r.status_code == 200, r.text
    [flow] = _flows(client)
    assert (flow.purpose, flow.link_id is not None, flow.return_to) == ("invite", True, "/x")
    code, state = fake.authorize(r.json()["authorization_url"], auth_time=_now())
    _callback(client, code=code, state=state)
    r = client.post(FINISH, json={})
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["return_to"]) == ("carol", "/x")
    value = client.cookies.get("lakana_session_8000") or ""
    session = client.get(
        "/api/v1/sign-in/session",
        headers=session_headers("lakana_session_8000", value, r.json()["csrf_token"]),
    ).json()
    assert (session["via"], session["needs_passkey"], session["step_up_until"]) == (
        "oidc",
        False,
        None,
    )
    assert _person(client, "carol").oidc_subject == "248289761001"
    [event] = _events(client, AuditEventType.SIGN_IN_OIDC)
    assert event["link_id"] == str(flow.link_id)
    # The link's secret is never stored with the flow, audited or logged.
    stored = repr([getattr(flow, c.key) for c in OidcFlow.__table__.columns])
    assert secret not in stored
    assert all(secret not in repr(e) for e in _events(client, AuditEventType.SIGN_IN_LINK_USED))
    assert secret not in caplog.text


def test_start_with_a_wrong_secret_is_like_unknown(client: TestClient) -> None:
    _, public_id, _ = _invite(client)
    r = _start_with_link(client, "invite", public_id, "wrong")
    assert (r.status_code, r.json()["state"]) == (410, "unknown")
    r = _start_with_link(client, "invite", "nope", "wrong")
    assert (r.status_code, r.json()["state"]) == (410, "unknown")
    assert _flows(client) == []
    assert len(_events(client, AuditEventType.AUTHENTICATION_FAILED)) == 2


def test_start_with_a_link_needs_both_halves_and_its_kind(client: TestClient) -> None:
    _, public_id, secret = _invite(client)
    r = client.post(START, json={"purpose": "invite", "link_id": public_id})
    assert r.status_code == 422, r.text
    assert "secret" in r.json()["detail"]
    r = _start_with_link(client, "recovery", public_id, secret)
    assert r.status_code == 422, r.text
    assert "invite" in r.json()["detail"]
    assert _flows(client) == []


def test_invite_withdrawn_after_start_creates_nobody(
    client: TestClient, fake: FakeProvider
) -> None:
    invite_id, public_id, secret = _invite(client)
    r = _start_with_link(client, "invite", public_id, secret)
    code, state = fake.authorize(r.json()["authorization_url"], auth_time=_now())
    _callback(client, code=code, state=state)
    assert client.delete(f"/api/v1/invites/{invite_id}", headers=AUTH_HEADER).status_code == 204
    _refused(client.post(FINISH, json={}), "link_unusable")
    names = {p.name for p in _all_people(client)}
    assert "carol" not in names
    [event] = _events(client, AuditEventType.SIGN_IN_OIDC_REFUSED)
    assert (event["reason"], event["sub"]) == ("link_unusable", "248289761001")


def test_recovery_of_a_linked_person_through_the_provider(
    client: TestClient, fake: FakeProvider
) -> None:
    _link(client, "alice")
    r = client.post("/api/v1/principals/alice/recovery-link", headers=AUTH_HEADER)
    assert r.status_code == 201, r.text
    public_id, secret = _halves(r.json()["url"])
    r = _start_with_link(client, "recovery", public_id, secret)
    code, state = fake.authorize(
        r.json()["authorization_url"], sub="someone-else", auth_time=_now()
    )
    _callback(client, code=code, state=state)
    _refused(client.post(FINISH, json={}), "identity_mismatch")
    r = _start_with_link(client, "recovery", public_id, secret)
    code, state = fake.authorize(r.json()["authorization_url"], auth_time=_now())
    _callback(client, code=code, state=state)
    r = client.post(FINISH, json={})
    assert (r.status_code, r.json()["name"]) == (200, "alice"), r.text


def test_describe_offers_the_provider(client: TestClient) -> None:
    _, public_id, _ = _invite(client)
    r = client.post(DESCRIBE, json={"link_id": public_id})
    assert (r.json()["offers_provider"], r.json()["provider_only"]) == (True, False)
    cast(FastAPI, client.app).state.oidc_provider = None
    r = client.post(DESCRIBE, json={"link_id": public_id})
    assert (r.json()["offers_provider"], r.json()["provider_only"]) == (False, False)


def _all_people(client: TestClient) -> list[Principal]:
    """Read every principal.

    Parameters
    ----------
    client
        The client of the app.

    Returns
    -------
    list of Principal
        The rows, detached.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine, expire_on_commit=False) as session:
        return list(session.execute(select(Principal)).scalars())


# -- linking from the profile, and provider step-up (#32, PR 2) --------------------------------

LINK_START = "/api/v1/sign-in/oidc/link/start"
STEP_UP_START = "/api/v1/sign-in/oidc/step-up/start"
FINISH_IN_SESSION = "/api/v1/sign-in/oidc/finish-in-session"
CONFIRMED = f"{PUBLIC}/sign-in/oidc/confirmed"


def _in_session(
    client: TestClient, name: str, *, touched_at: datetime | None = None
) -> dict[str, str]:
    """Sign a person in directly and put the session cookie into the browser.

    Parameters
    ----------
    client
        The browser.
    name
        The person.
    touched_at
        The session's last step-up touch.

    Returns
    -------
    dict of str to str
        The CSRF header the page sends.
    """
    cookie, value, csrf = signed_in(client, name, touched_at=touched_at)
    client.cookies.set(cookie, value)
    return {"X-Lakana-CSRF": csrf}


def _person_with_passkey(client: TestClient, name: str, *, admin: bool = False) -> None:
    """Create an unlinked person with a working passkey.

    Parameters
    ----------
    client
        The client of the app.
    name
        The name.
    admin
        Whether to grant them ADMIN on the instance.
    """
    _link(client, name, subject="")
    if admin:
        grant = {"principal": name, "role": "ADMIN", "kind": "INSTANCE"}
        assert client.post("/api/v1/grants", headers=AUTH_HEADER, json=grant).status_code == 201
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        person = session.execute(select(Principal).where(Principal.name == name)).scalar_one()
        session.add(
            Passkey(
                id=uuid.uuid4(),
                principal_id=person.id,
                credential_id=uuid.uuid4().hex,
                public_key=b"k",
                sign_count=0,
                rp_id="localhost",
                name="Passkey",
                backup_eligible=False,
                created_at=datetime.now(UTC),
                created_user_agent="pytest",
            )
        )
        session.commit()


def _callback_to(client: TestClient, url: str, **claims: object) -> str:
    """Sign in at the provider and come back, returning where the callback sent the browser.

    Parameters
    ----------
    client
        The browser.
    url
        The authorization URL.
    **claims
        Claims of the ID token to change.

    Returns
    -------
    str
        The callback's `Location`.
    """
    code, state = fake_of(client).authorize(url, **claims)
    r = client.get(CALLBACK, params={"code": code, "state": state}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"]


def fake_of(client: TestClient) -> FakeProvider:
    """Read the fake provider the client's app talks to.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    FakeProvider
        The provider.
    """
    return cast(FastAPI, client.app).state.fake_provider


@pytest.fixture
def linking(client: TestClient, fake: FakeProvider, clock: SettableClock) -> TestClient:
    """Provide the browser with the fake provider reachable from the client, and a clock.

    Parameters
    ----------
    client
        The browser.
    fake
        The provider.
    clock
        The app's clock.

    Returns
    -------
    TestClient
        The browser.
    """
    cast(FastAPI, client.app).state.fake_provider = fake
    return client


def test_link_from_profile_needs_a_fresh_passkey_touch(
    linking: TestClient, clock: SettableClock
) -> None:
    _person_with_passkey(linking, "bob")
    headers = _in_session(linking, "bob", touched_at=clock.now - timedelta(minutes=16))
    r = linking.post(LINK_START, headers=headers, json={})
    assert (r.status_code, r.json()["type"]) == (403, "StepUpRequired"), r.text
    assert _flows(linking) == []


def test_link_from_profile_needs_a_browser(linking: TestClient) -> None:
    r = linking.post(LINK_START, headers=AUTH_HEADER, json={})
    assert r.status_code == 409, r.text
    assert "browser" in r.json()["detail"]


def test_link_from_profile_links_and_keeps_passkeys_stored(
    linking: TestClient, clock: SettableClock
) -> None:
    _person_with_passkey(linking, "bob")
    headers = _in_session(linking, "bob", touched_at=clock.now)
    r = linking.post(LINK_START, headers=headers, json={"return_to": "/me"})
    assert r.status_code == 200, r.text
    [flow] = _flows(linking)
    assert (flow.purpose, flow.session_id is not None) == ("link", True)
    assert (
        _callback_to(linking, r.json()["authorization_url"], sub="bob-sub", auth_time=_now())
        == CONFIRMED
    )
    r = linking.post(FINISH_IN_SESSION, headers=headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["return_to"] == "/me"
    assert r.json()["step_up_until"] is not None
    bob = _person(linking, "bob")
    assert (bob.oidc_issuer, bob.oidc_subject) == (ISSUER, "bob-sub")
    app = cast(FastAPI, linking.app)
    with Session(app.state.engine) as session:
        assert (
            len(session.execute(select(Passkey).where(Passkey.principal_id == bob.id)).all()) == 1
        )
    # Linking ends no session: the same one still answers.
    assert linking.get("/api/v1/sign-in/session", headers=headers).json()["name"] == "bob"
    [linked] = _events(linking, AuditEventType.PRINCIPAL_OIDC_LINKED)
    assert linked["via"] == "profile"


def test_link_from_profile_refusals(linking: TestClient, clock: SettableClock) -> None:
    _link(linking, "alice")
    headers = _in_session(linking, "alice", touched_at=clock.now)
    _refused(linking.post(LINK_START, headers=headers, json={}), "already_linked")
    headers = _in_session(linking, "admin", touched_at=clock.now)
    _refused(linking.post(LINK_START, headers=headers, json={}), "cannot_link_operator")


def test_the_last_unlinked_admin_is_told_before_the_provider(
    linking: TestClient, clock: SettableClock
) -> None:
    _person_with_passkey(linking, "ann", admin=True)
    headers = _in_session(linking, "ann", touched_at=clock.now)
    _refused(linking.post(LINK_START, headers=headers, json={}), "last_unlinked_admin")
    assert _flows(linking) == []


def test_link_finish_from_another_session_is_refused(
    linking: TestClient, clock: SettableClock
) -> None:
    _person_with_passkey(linking, "bob")
    headers = _in_session(linking, "bob", touched_at=clock.now)
    r = linking.post(LINK_START, headers=headers, json={})
    _callback_to(linking, r.json()["authorization_url"], sub="bob-sub", auth_time=_now())
    other = _in_session(linking, "bob", touched_at=clock.now)
    _refused(linking.post(FINISH_IN_SESSION, headers=other, json={}), "flow_expired")
    assert _person(linking, "bob").oidc_subject is None


def test_step_up_start_asks_prompt_login_and_max_age_zero(
    linking: TestClient, clock: SettableClock
) -> None:
    _link(linking, "alice")
    headers = _in_session(linking, "alice")
    r = linking.post(STEP_UP_START, headers=headers, json={})
    assert r.status_code == 200, r.text
    query = parse_qs(urlsplit(r.json()["authorization_url"]).query)
    assert (query["prompt"], query["max_age"]) == (["login"], ["0"])
    [flow] = _flows(linking)
    assert flow.purpose == "step_up"


def test_step_up_start_needs_a_linked_person(linking: TestClient) -> None:
    _person_with_passkey(linking, "bob")
    r = linking.post(STEP_UP_START, headers=_in_session(linking, "bob"), json={})
    assert r.status_code == 409, r.text
    assert "passkey" in r.json()["detail"]
    r = linking.post(STEP_UP_START, headers=AUTH_HEADER, json={})
    assert r.status_code == 409, r.text


def _stepped_up(linking: TestClient, clock: SettableClock, **claims: object) -> Any:  # ruff: ignore[any-type]
    """Run a provider step-up for Alice, linked, in a session without a touch.

    Parameters
    ----------
    linking
        The browser.
    clock
        The app's clock.
    **claims
        Claims of the ID token to change.

    Returns
    -------
    Any
        The finish's response.
    """
    headers = _in_session(linking, "alice")
    r = linking.post(STEP_UP_START, headers=headers, json={"return_to": "/tokens"})
    claims.setdefault("auth_time", int(clock.now.timestamp()))
    assert _callback_to(linking, r.json()["authorization_url"], **claims) == CONFIRMED
    return linking.post(FINISH_IN_SESSION, headers=headers, json={})


def test_provider_step_up_sets_the_touch(linking: TestClient, clock: SettableClock) -> None:
    _link(linking, "alice")
    r = _stepped_up(linking, clock)
    assert r.status_code == 200, r.text
    assert r.json()["return_to"] == "/tokens"
    until = datetime.fromisoformat(r.json()["step_up_until"])
    assert until == clock.now + timedelta(minutes=15)
    headers = {"X-Lakana-CSRF": r.request.headers["X-Lakana-CSRF"]}
    session = linking.get("/api/v1/sign-in/session", headers=headers).json()
    assert session["step_up_until"] is not None
    # A step-up action now goes through, as after a passkey touch.
    r = linking.post("/api/v1/principals/alice/tokens", headers=headers, json={"name": "t"})
    assert r.status_code == 201, r.text
    assert _person(linking, "alice").last_provider_sign_in_at == clock.now
    [event] = _events(linking, AuditEventType.SIGN_IN_OIDC)
    assert event["purpose"] == "step_up"


def test_provider_step_up_with_old_auth_time_is_refused(
    linking: TestClient, clock: SettableClock
) -> None:
    _link(linking, "alice")
    _refused(_stepped_up(linking, clock, auth_time=int(clock.now.timestamp()) - 61), "auth_too_old")
    _refused(_stepped_up(linking, clock, auth_time=None), "auth_too_old")
    reasons = [e["reason"] for e in _events(linking, AuditEventType.SIGN_IN_OIDC_REFUSED)]
    assert reasons == ["auth_too_old", "auth_too_old"]


def test_provider_step_up_as_someone_else_is_refused(
    linking: TestClient, clock: SettableClock
) -> None:
    _link(linking, "alice")
    _refused(_stepped_up(linking, clock, sub="mallory"), "identity_mismatch")


def test_callback_redirects_link_flows_to_confirmed(
    linking: TestClient, clock: SettableClock
) -> None:
    _link(linking, "alice")
    r = linking.post(STEP_UP_START, headers=_in_session(linking, "alice"), json={})
    assert _callback_to(linking, r.json()["authorization_url"]) == CONFIRMED
    linking.cookies.clear()
    assert _callback_to(linking, _start(linking)) == LANDING


def test_finish_and_finish_in_session_refuse_the_other_purposes(
    linking: TestClient, clock: SettableClock
) -> None:
    _link(linking, "alice")
    headers = _in_session(linking, "alice")
    r = linking.post(STEP_UP_START, headers=headers, json={})
    _callback_to(linking, r.json()["authorization_url"], auth_time=int(clock.now.timestamp()))
    r = linking.post(FINISH, json={})
    assert (r.status_code, r.json()["type"]) == (409, "ConflictError"), r.text
    # The flow was left for its own finish.
    assert linking.post(FINISH_IN_SESSION, headers=headers, json={}).status_code == 200
    _callback_to(linking, _start(linking))
    r = linking.post(FINISH_IN_SESSION, headers=headers, json={})
    assert (r.status_code, r.json()["type"]) == (409, "ConflictError"), r.text
    assert linking.post(FINISH, json={}).status_code == 200
    r = linking.post(FINISH_IN_SESSION, headers=AUTH_HEADER, json={})
    assert r.status_code == 409, r.text


def test_a_refused_flow_finishes_in_session_once(linking: TestClient, clock: SettableClock) -> None:
    _link(linking, "alice")
    headers = _in_session(linking, "alice")
    r = linking.post(STEP_UP_START, headers=headers, json={})
    _callback_to(linking, r.json()["authorization_url"], nonce="wrong")
    _refused(linking.post(FINISH_IN_SESSION, headers=headers, json={}), "token_invalid")
    _refused(linking.post(FINISH_IN_SESSION, headers=headers, json={}), "flow_expired")


def test_every_link_flow_asks_the_provider_for_a_fresh_sign_in(
    linking: TestClient, clock: SettableClock
) -> None:
    """An invite, a recovery link and a profile link send prompt=login; a sign-in does not."""

    def prompt(url: str) -> list[str] | None:
        """Read the prompt of an authorization URL.

        Parameters
        ----------
        url
            The URL.

        Returns
        -------
        list of str or None
            The values of ``prompt``.
        """
        return parse_qs(urlsplit(url).query).get("prompt")

    _, public_id, secret = _invite(linking)
    assert prompt(
        _start_with_link(linking, "invite", public_id, secret).json()["authorization_url"]
    ) == ["login"]
    _link(linking, "alice")
    r = linking.post("/api/v1/principals/alice/recovery-link", headers=AUTH_HEADER)
    public_id, secret = _halves(r.json()["url"])
    assert prompt(
        _start_with_link(linking, "recovery", public_id, secret).json()["authorization_url"]
    ) == ["login"]
    _person_with_passkey(linking, "bob")
    headers = _in_session(linking, "bob", touched_at=clock.now)
    assert prompt(
        linking.post(LINK_START, headers=headers, json={}).json()["authorization_url"]
    ) == ["login"]
    linking.cookies.clear()
    assert prompt(_start(linking)) is None


def test_a_link_without_a_fresh_provider_sign_in_is_refused(
    linking: TestClient, fake: FakeProvider, clock: SettableClock
) -> None:
    """A provider may ignore prompt=login: an invite or a profile link checks auth_time."""
    _, public_id, secret = _invite(linking)
    r = _start_with_link(linking, "invite", public_id, secret)
    query = parse_qs(urlsplit(r.json()["authorization_url"]).query)
    assert (query["prompt"], query["max_age"]) == (["login"], ["0"])
    code, state = fake.authorize(r.json()["authorization_url"], auth_time=_now() - 3600)
    _callback(linking, code=code, state=state)
    _refused(linking.post(FINISH, json={}), "auth_too_old")
    assert "carol" not in {p.name for p in _all_people(linking)}
    linking.cookies.clear()
    _person_with_passkey(linking, "bob")
    headers = _in_session(linking, "bob", touched_at=clock.now)
    r = linking.post(LINK_START, headers=headers, json={})
    _callback_to(linking, r.json()["authorization_url"], sub="bob-sub")
    _refused(linking.post(FINISH_IN_SESSION, headers=headers, json={}), "auth_too_old")
    assert _person(linking, "bob").oidc_subject is None
    reasons = [e["reason"] for e in _events(linking, AuditEventType.SIGN_IN_OIDC_REFUSED)]
    assert reasons == ["auth_too_old", "auth_too_old"]
