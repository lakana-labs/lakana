"""Tests for the browser session as a credential of `get_principal`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana_server.ui
from lakana_core.auth import AllowAllAuthorization
from lakana_core.identity.pats import PatService
from lakana_core.identity.sessions import csrf_token_for
from lakana_core.models import AuditEvent, AuditEventType, BrowserSession, Principal
from lakana_server.api.platform.schemas import BrowserSessionOut, SessionOut
from lakana_server.app import create_app
from lakana_server.browser_session import (
    BACKGROUND_HEADER,
    CSRF_HEADER,
    clear_session_cookie,
    cookie_name,
    set_session_cookie,
)
from lakana_server.settings import AuthAuditConfig, Settings, UiConfig
from tests.conftest import AUTH_HEADER, PUBLIC, SettableClock, write_ui_dist
from tests.server_test_helpers import session_headers, signed_in

HTTPS = "https://lakana.example"
"""The public address of the HTTPS test app."""


@pytest.fixture
def client(ui_client: TestClient) -> TestClient:
    """Provide the client of an app with the interface on.

    Parameters
    ----------
    ui_client
        The shared client with the interface on, at `http://localhost:8000`.

    Returns
    -------
    TestClient
        The same client.
    """
    return ui_client


@pytest.fixture
def client_ui_off(settings: Settings) -> Iterator[TestClient]:
    """Provide a client of an app with the interface off, at the same address.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.

    Yields
    ------
    TestClient
        A started client.
    """
    with TestClient(create_app(settings), base_url=PUBLIC, headers={"Origin": PUBLIC}) as c:
        yield c


@pytest.fixture
def https_client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide a client of an app with the interface on at an HTTPS address.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        Pytest's per-test directory, for the stand-in build.
    monkeypatch
        Points `lakana_server.ui.UI_DIST` at the stand-in.

    Yields
    ------
    TestClient
        A started client at `https://lakana.example`.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    ui = UiConfig(enabled=True, public_url=HTTPS)
    app = create_app(settings.model_copy(update={"ui": ui}))
    with TestClient(app, base_url=HTTPS, headers={"Origin": HTTPS}) as c:
        yield c


def _add_user(client: TestClient, name: str) -> None:
    """Create a person through the API, as the bootstrap administrator.

    Parameters
    ----------
    client
        A started client.
    name
        The new principal's name.
    """
    r = client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    assert r.status_code == 201, r.text


def _pat_of(client: TestClient, name: str) -> str:
    """Issue a personal access token for a principal directly through the service.

    Parameters
    ----------
    client
        A started client.
    name
        The principal's name.

    Returns
    -------
    str
        The full token.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        row = session.execute(select(Principal).where(Principal.name == name)).scalar_one()
        from lakana_core.auth import PrincipalRef

        ref = PrincipalRef(id=row.id, name=row.name, type=row.type)
        return PatService(session, AllowAllAuthorization()).issue(ref, name, name="t").token


def _rows(client: TestClient) -> list[BrowserSession]:
    """Read every session row.

    Parameters
    ----------
    client
        A started client.

    Returns
    -------
    list of BrowserSession
        The rows, detached.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine, expire_on_commit=False) as session:
        return list(session.execute(select(BrowserSession)).scalars())


def _set_cookie(response: object) -> str:
    """Read the one `Set-Cookie` header of a response.

    Parameters
    ----------
    response
        An httpx response or a Starlette `Response`.

    Returns
    -------
    str
        The header's value.
    """
    headers = response.headers  # ty: ignore[unresolved-attribute]
    values = headers.get_list("set-cookie") if hasattr(headers, "get_list") else None
    if values is None:
        values = [v for k, v in headers.items() if k.lower() == "set-cookie"]
    assert len(values) == 1, values
    return values[0]


def test_cookie_authenticates_api_v1(client: TestClient) -> None:
    """A session cookie on `/api/v1` authenticates as the person it belongs to."""
    name, value, _ = signed_in(client, "admin")
    r = client.get("/api/v1/whoami", headers=session_headers(name, value))
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "admin"


def test_cookie_is_ignored_outside_api_v1(client: TestClient) -> None:
    """The Iceberg REST routes never read the cookie: only a token answers there."""
    name, value, _ = signed_in(client, "admin")
    assert client.get("/v1/dev/namespaces", headers=AUTH_HEADER).status_code != 401
    r = client.get("/v1/dev/namespaces", headers=session_headers(name, value))
    assert r.status_code == 401


def test_cookie_is_ignored_with_the_interface_off(client_ui_off: TestClient) -> None:
    """With `LAKANA_UI__ENABLED=false` a valid session cookie authenticates nothing."""
    name, value, _ = signed_in(client_ui_off, "admin")
    assert (
        client_ui_off.get("/api/v1/whoami", headers=session_headers(name, value)).status_code == 401
    )


@pytest.mark.parametrize("headers", [{}, AUTH_HEADER])
def test_sign_in_routes_answer_409_with_the_interface_off(
    client_ui_off: TestClient, headers: dict[str, str]
) -> None:
    """Every sign-in route names the setting that turns the interface on, before any credential."""
    r = client_ui_off.get("/api/v1/sign-in/session", headers=headers)
    assert r.status_code == 409, r.text
    assert "LAKANA_UI__ENABLED" in r.json()["detail"]


@pytest.mark.parametrize(
    ("method", "path"), [("POST", "/api/v1/task-tokens"), ("DELETE", "/api/v1/task-tokens/k")]
)
def test_token_only_routes_ignore_the_cookie(client: TestClient, method: str, path: str) -> None:
    """Task tokens are minted and revoked with a token only, never from a browser."""
    name, value, csrf = signed_in(client, "admin", touched_at=None)
    body = {"environment": "dev", "asset_scope": [], "ttl_seconds": 300}
    r = client.request(
        method,
        path,
        headers=session_headers(name, value, csrf),
        json=body if method == "POST" else None,
    )
    assert r.status_code == 403, r.text
    assert r.json()["type"] == "TokenOnlyRoute"
    assert "lakana" in r.json()["detail"]


def test_csrf_token_survives_a_second_tab(client: TestClient) -> None:
    """Reading the session again never rotates its CSRF token, so other tabs keep working."""
    name, value, _ = signed_in(client, "admin")
    first = client.get("/api/v1/sign-in/session", headers=session_headers(name, value))
    second = client.get("/api/v1/sign-in/session", headers=session_headers(name, value))
    assert first.status_code == 200, first.text
    assert first.json()["csrf_token"] == second.json()["csrf_token"]
    r = client.patch(
        "/api/v1/whoami",
        headers=session_headers(name, value, first.json()["csrf_token"]),
        json={"language": "de"},
    )
    assert r.status_code == 200, r.text


def test_bearer_wins_over_the_cookie_and_skips_csrf(client: TestClient) -> None:
    """With a Bearer header the cookie is not read, so no CSRF token is asked for."""
    _add_user(client, "alice")
    _add_user(client, "bob")
    name, value, _ = signed_in(client, "alice")
    bearer = {"Authorization": f"Bearer {_pat_of(client, 'bob')}"}
    cookie = session_headers(name, value)
    assert client.get("/api/v1/whoami", headers={**cookie, **bearer}).json()["name"] == "bob"
    r = client.patch("/api/v1/whoami", headers={**cookie, **bearer}, json={"language": "de"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "bob"


@pytest.mark.parametrize(
    "authorization",
    ["Bearer lakana_pat_nokey_nosecret", "Bearer not-a-token", "Basic YWRtaW46eA==", ""],
)
def test_any_authorization_header_never_falls_back_to_the_cookie(
    client: TestClient, authorization: str
) -> None:
    """A failing `Authorization` header is a 401 even beside a valid session cookie."""
    name, value, csrf = signed_in(client, "admin")
    headers = {**session_headers(name, value, csrf), "Authorization": authorization}
    r = client.get("/api/v1/whoami", headers=headers)
    assert r.status_code == 401, r.text


def test_unsafe_cookie_request_needs_the_csrf_token(client: TestClient) -> None:
    """A change sent with the cookie and without the CSRF header is refused."""
    name, value, _ = signed_in(client, "admin")
    r = client.patch(
        "/api/v1/whoami", headers=session_headers(name, value), json={"language": "de"}
    )
    assert r.status_code == 403, r.text
    assert r.json()["type"] == "CsrfRefused"
    assert "Reload the page and try again" in r.json()["detail"]


def test_csrf_token_of_another_session_is_refused(client: TestClient) -> None:
    """The CSRF token is bound to its own session."""
    name, value, _ = signed_in(client, "admin")
    _, _, other_csrf = signed_in(client, "admin")
    r = client.patch(
        "/api/v1/whoami", headers=session_headers(name, value, other_csrf), json={"language": "de"}
    )
    assert r.status_code == 403
    assert r.json()["type"] == "CsrfRefused"


def test_cookie_request_is_authenticated_before_the_body_is_read(client: TestClient) -> None:
    """A malformed body gets 401 without a working cookie, 403 without CSRF, 422 only then.

    The CSRF token is checked before the session's row: a forged cookie needs its own token
    (derivable by whoever forged it) to reach the 401.
    """
    name, value, csrf = signed_in(client, "admin")
    bad = '{"language": '
    json_type = {"Content-Type": "application/json"}
    forged = value.split("_")[0] + "_wrong"
    unknown = session_headers(name, forged, csrf_token_for(forged))
    assert (
        client.patch("/api/v1/whoami", headers={**unknown, **json_type}, content=bad).status_code
        == 401
    )
    no_csrf = session_headers(name, value)
    assert (
        client.patch("/api/v1/whoami", headers={**no_csrf, **json_type}, content=bad).status_code
        == 403
    )
    good = session_headers(name, value, csrf)
    assert (
        client.patch("/api/v1/whoami", headers={**good, **json_type}, content=bad).status_code
        == 422
    )


def test_ended_session_names_the_reason(client: TestClient) -> None:
    """A request after sign-out says why it failed and what to do."""
    name, value, csrf = signed_in(client, "admin")
    assert (
        client.delete(
            "/api/v1/sign-in/session", headers=session_headers(name, value, csrf)
        ).status_code
        == 204
    )
    r = client.get("/api/v1/whoami", headers=session_headers(name, value))
    assert r.status_code == 401 and r.json()["type"] == "SessionEndedError"
    assert "sign in again" in r.json()["detail"].lower()
    assert r.json()["reason"] == "sign_out"


def test_an_unknown_session_id_is_never_adopted(client: TestClient) -> None:
    """A cookie value the server never issued authenticates nothing and creates no session."""
    name = cookie_name(cast(FastAPI, client.app).state.settings.public_origin())
    forged = session_headers(name, "0" * 32 + "_chosen-by-the-attacker")
    assert client.get("/api/v1/whoami", headers=forged).status_code == 401
    assert client.get("/api/v1/sign-in/session", headers=forged).status_code == 401
    assert _rows(client) == []


def test_background_requests_do_not_extend_the_session(
    client: TestClient, clock: SettableClock
) -> None:
    """Only requests the person started keep the session alive; polls never do."""
    cast(FastAPI, client.app).state.clock = clock
    name, polled, _ = signed_in(client, "admin")
    _, used, _ = signed_in(client, "admin")
    clock.advance(timedelta(hours=11))
    background = {BACKGROUND_HEADER: "1"}
    assert (
        client.get(
            "/api/v1/whoami", headers={**session_headers(name, polled), **background}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/whoami", headers=session_headers(name, used)).status_code == 200
    clock.advance(timedelta(hours=2))
    r = client.get("/api/v1/whoami", headers={**session_headers(name, polled), **background})
    assert r.status_code == 401 and r.json()["reason"] == "idle"
    assert client.get("/api/v1/whoami", headers=session_headers(name, used)).status_code == 200


def test_failed_cookie_attempts_are_rate_limited_in_the_audit_log(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused cookies are audited like refused tokens, at most the per-address limit a minute."""
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    app = create_app(
        settings.model_copy(
            update={
                "ui": UiConfig(enabled=True),
                "auth_audit": AuthAuditConfig(per_ip_per_minute=2),
            }
        )
    )
    with TestClient(app, base_url=PUBLIC, headers={"Origin": PUBLIC}) as c:
        name = cookie_name(app.state.settings.public_origin())
        for i in range(5):
            bad = session_headers(name, f"{i:032x}_wrong")
            assert c.get("/api/v1/whoami", headers=bad).status_code == 401
        with Session(app.state.engine) as session:
            failed = (
                session.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == AuditEventType.AUTHENTICATION_FAILED
                    )
                )
                .scalars()
                .all()
            )
    assert len(failed) == 2
    assert failed[0].detail == {"key_id": f"{0:032x}", "credential": "session"}


def test_a_session_success_is_audited_once_per_window(client: TestClient) -> None:
    """Successes go through the sign-in window like a token's, keyed by the session."""
    name, value, _ = signed_in(client, "admin")
    for _ in range(3):
        assert client.get("/api/v1/whoami", headers=session_headers(name, value)).status_code == 200
    with Session(cast(FastAPI, client.app).state.engine) as session:
        rows = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.AUTHENTICATION_SUCCEEDED
                )
            )
            .scalars()
            .all()
        )
    key_id = value.split("_")[0]
    assert [r.detail for r in rows if r.detail.get("key_id") == key_id] == [
        {"key_id": key_id, "credential": "session"}
    ]


def test_deactivation_ends_sessions_at_once(client: TestClient) -> None:
    """Deactivating a person ends their sessions in the same request."""
    _add_user(client, "alice")
    name, value, _ = signed_in(client, "alice")
    assert client.delete("/api/v1/principals/alice", headers=AUTH_HEADER).status_code == 204
    assert [r.end_reason for r in _rows(client)] == ["deactivated"]
    r = client.get("/api/v1/whoami", headers=session_headers(name, value))
    assert r.status_code == 401 and r.json()["reason"] == "deactivated"


def test_cookie_attributes_over_http_loopback(client: TestClient) -> None:
    """On loopback HTTP the cookie is named by port, HttpOnly, SameSite=Strict, not Secure."""
    origin = cast(FastAPI, client.app).state.settings.public_origin()
    response = Response()
    set_session_cookie(response, origin, "k_s", 3600)
    header = _set_cookie(response)
    assert header.startswith("lakana_session_8000=k_s;")
    parts = {p.strip().split("=")[0].lower() for p in header.split(";")}
    assert {"httponly", "path", "samesite", "max-age"} <= parts
    assert "secure" not in parts and "domain" not in parts
    assert "samesite=strict" in header.lower() and "path=/;" in header.lower() + ";"
    name, value, csrf = signed_in(client, "admin")
    out = client.delete("/api/v1/sign-in/session", headers=session_headers(name, value, csrf))
    cleared = _set_cookie(out).lower()
    assert cleared.startswith("lakana_session_8000=") and "max-age=0" in cleared
    assert "httponly" in cleared and "samesite=strict" in cleared and "secure" not in cleared


def test_cookie_attributes_over_https(https_client: TestClient) -> None:
    """Over HTTPS the cookie is `__Host-lakana_session`, Secure, Path=/ and without Domain."""
    origin = cast(FastAPI, https_client.app).state.settings.public_origin()
    assert cookie_name(origin) == "__Host-lakana_session"
    response = Response()
    set_session_cookie(response, origin, "k_s", 3600)
    header = _set_cookie(response)
    lowered = header.lower()
    assert header.startswith("__Host-lakana_session=k_s;")
    assert "; secure" in lowered and "httponly" in lowered and "samesite=strict" in lowered
    assert "path=/" in lowered and "domain" not in lowered
    cleared = Response()
    clear_session_cookie(cleared, origin)
    lowered = _set_cookie(cleared).lower()
    assert lowered.startswith("__host-lakana_session=") and "; secure" in lowered
    assert "max-age=0" in lowered and "path=/" in lowered
    name, value, _ = signed_in(https_client, "admin")
    r = https_client.get("/api/v1/whoami", headers=session_headers(name, value))
    assert r.status_code == 200, r.text


def test_no_session_output_carries_a_secret() -> None:
    """No REST schema of a session has a field for its secret or its hash."""
    for model in (SessionOut, BrowserSessionOut):
        assert not [f for f in model.model_fields if "hash" in f or "secret" in f]
    assert CSRF_HEADER == "X-Lakana-CSRF"
