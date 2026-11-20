"""The OIDC sign-in routes: info, start, callback and finish, against a fake provider.

The provider (`tests.oidc_fake`) serves discovery, keys and a token endpoint without the network;
its client is the server's real one (`OidcProvider`), so the checks run as in production.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote_plus, urlsplit

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana_server.ui
from lakana_core.models import AuditEvent, AuditEventType, OidcFlow
from lakana_core.net.https_json import FetchFailed
from lakana_core.oidc.provider import OidcProvider
from lakana_server.app import create_app
from lakana_server.auth_rate_limit import AuthAuditLimiter
from lakana_server.browser_session import flow_cookie_name, set_flow_cookie
from lakana_server.settings import PublicOrigin, Settings, UiConfig
from tests.conftest import PUBLIC, write_ui_dist
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

INFO = "/api/v1/sign-in/oidc"
START = "/api/v1/sign-in/oidc/start"
CALLBACK = "/api/v1/sign-in/oidc/callback"
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


def test_routes_answer_409_while_the_interface_is_off(settings: Settings) -> None:
    with TestClient(create_app(settings)) as off:
        for method, path in [("GET", INFO), ("POST", START), ("GET", CALLBACK)]:
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
