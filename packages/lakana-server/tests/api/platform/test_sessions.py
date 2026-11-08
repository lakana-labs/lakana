"""Tests for the session routes: sign out, the list of sessions, and the sign-in settings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, AuditEventType, BrowserSession
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import session_headers, signed_in


@pytest.fixture
def client(ui_client: TestClient) -> TestClient:
    """Provide the client of an app with the interface on.

    Parameters
    ----------
    ui_client
        The shared client with the interface on.

    Returns
    -------
    TestClient
        The same client.
    """
    r = ui_client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"}
    )
    assert r.status_code == 201, r.text
    return ui_client


def _rows(client: TestClient) -> dict[str, BrowserSession]:
    """Read every session row by its key id.

    Parameters
    ----------
    client
        A started client.

    Returns
    -------
    dict of str to BrowserSession
        The rows, detached.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine, expire_on_commit=False) as session:
        return {r.key_id: r for r in session.execute(select(BrowserSession)).scalars()}


def _key(value: str) -> str:
    """Read a session cookie's key id.

    Parameters
    ----------
    value
        The cookie's value.

    Returns
    -------
    str
        The part before the first ``_``.
    """
    return value.split("_", 1)[0]


def test_sign_out_clears_the_cookie_and_ends_the_session(client: TestClient) -> None:
    """Sign out ends the row at once and tells the browser to drop the cookie."""
    name, value, csrf = signed_in(client, "alice")
    r = client.delete("/api/v1/sign-in/session", headers=session_headers(name, value, csrf))
    assert r.status_code == 204, r.text
    assert "max-age=0" in r.headers["set-cookie"].lower()
    assert _rows(client)[_key(value)].end_reason == "sign_out"


def test_sign_out_with_a_token_has_nothing_to_end(client: TestClient) -> None:
    """The session routes describe a browser session; a token caller gets 409."""
    for method in ("GET", "DELETE"):
        r = client.request(method, "/api/v1/sign-in/session", headers=AUTH_HEADER)
        assert r.status_code == 409, r.text
        assert "a token has none" in r.json()["detail"]


def test_describe_the_session(client: TestClient) -> None:
    """The interface reads who is signed in, the CSRF token and what the session still needs."""
    touched = datetime.now(UTC)
    name, value, csrf = signed_in(client, "alice", touched_at=touched, via="invite")
    body = client.get("/api/v1/sign-in/session", headers=session_headers(name, value)).json()
    assert body["name"] == "alice" and body["type"] == "USER" and body["via"] == "invite"
    assert body["csrf_token"] == csrf
    assert body["needs_passkey"] is True and body["offer_passkey"] is False
    assert body["step_up_until"] is not None and body["absolute_expires_at"]
    _, local, _ = signed_in(client, "alice", via="local")
    body = client.get("/api/v1/sign-in/session", headers=session_headers(name, local)).json()
    assert body["needs_passkey"] is False and body["offer_passkey"] is True
    assert body["step_up_until"] is None


def test_sign_out_everywhere_ends_every_own_session(client: TestClient) -> None:
    """A person ends all their own sessions without step-up; the cookie is cleared too."""
    name, first, csrf = signed_in(client, "alice")
    _, second, _ = signed_in(client, "alice")
    _, admins, _ = signed_in(client, "admin")
    r = client.delete(
        "/api/v1/principals/alice/sessions", headers=session_headers(name, first, csrf)
    )
    assert r.status_code == 204, r.text
    assert "max-age=0" in r.headers["set-cookie"].lower()
    rows = _rows(client)
    assert {rows[_key(v)].end_reason for v in (first, second)} == {"sign_out_everywhere"}
    assert rows[_key(admins)].ended_at is None


def test_ending_another_persons_sessions_needs_step_up(client: TestClient) -> None:
    """An administrator ends someone else's sessions only with a fresh passkey touch, audited."""
    _, alices, _ = signed_in(client, "alice")
    name, stale, stale_csrf = signed_in(client, "admin")
    r = client.delete(
        "/api/v1/principals/alice/sessions", headers=session_headers(name, stale, stale_csrf)
    )
    assert r.status_code == 403 and r.json()["type"] == "StepUpRequired"
    _, fresh, fresh_csrf = signed_in(client, "admin", touched_at=datetime.now(UTC))
    r = client.delete(
        "/api/v1/principals/alice/sessions", headers=session_headers(name, fresh, fresh_csrf)
    )
    assert r.status_code == 204, r.text
    assert "set-cookie" not in r.headers
    assert _rows(client)[_key(alices)].end_reason == "ended_by_admin"
    with Session(cast(FastAPI, client.app).state.engine) as session:
        ended = session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.SESSION_ENDED)
        ).scalar_one()
    assert ended.detail["actor"] == "admin" and ended.detail["subject"] == "alice"


def test_a_person_lists_their_own_sessions_without_secrets(client: TestClient) -> None:
    """Each open session is listed once, the current one marked; no hash appears."""
    name, value, _ = signed_in(client, "alice")
    _, other, _ = signed_in(client, "alice")
    r = client.get("/api/v1/principals/alice/sessions", headers=session_headers(name, value))
    assert r.status_code == 200, r.text
    listed = r.json()
    assert [s["current"] for s in listed] == [True, False]
    assert set(listed[0]) == {
        "id",
        "via",
        "created_at",
        "last_seen_at",
        "absolute_expires_at",
        "user_agent",
        "peer",
        "current",
    }
    for row in _rows(client).values():
        assert row.secret_hash not in r.text
    assert _key(value) not in r.text and _key(other) not in r.text


def test_a_non_administrator_cannot_list_another_persons_sessions(client: TestClient) -> None:
    """Listing someone else's sessions needs the ADMIN role."""
    name, value, _ = signed_in(client, "alice")
    r = client.get("/api/v1/principals/admin/sessions", headers=session_headers(name, value))
    assert r.status_code == 403, r.text
    assert client.get("/api/v1/principals/alice/sessions", headers=AUTH_HEADER).status_code == 200


def test_every_signed_in_person_reads_the_sign_in_settings(client: TestClient) -> None:
    """The defaults and the bounds, in the units the interface shows."""
    name, value, _ = signed_in(client, "alice")
    r = client.get("/api/v1/settings/sign-in", headers=session_headers(name, value))
    assert r.status_code == 200, r.text
    assert r.json() == {
        "idle_timeout_minutes": 720,
        "absolute_lifetime_days": 7,
        "bounds": {
            "idle_timeout_minutes_min": 15,
            "idle_timeout_minutes_max": 1440,
            "absolute_lifetime_days_min": 1,
            "absolute_lifetime_days_max": 30,
        },
    }


@pytest.mark.parametrize(
    ("body", "bound"),
    [
        ({"idle_timeout_minutes": 10}, "at least 15 minutes"),
        ({"idle_timeout_minutes": 1441}, "at most 24 hours"),
        ({"absolute_lifetime_days": 31}, "at most 30 days"),
    ],
)
def test_settings_outside_the_bounds_name_the_bound(
    client: TestClient, body: dict[str, int], bound: str
) -> None:
    """A value outside the bounds is refused with 422 naming the bound."""
    r = client.patch("/api/v1/settings/sign-in", headers=AUTH_HEADER, json=body)
    assert r.status_code == 422, r.text
    assert bound in r.text


def test_an_administrator_changes_the_settings_with_step_up(client: TestClient) -> None:
    """Changing the lengths needs ADMIN and, from a browser, a fresh passkey touch."""
    body = {"idle_timeout_minutes": 60, "absolute_lifetime_days": 2}
    name, alices, alice_csrf = signed_in(client, "alice", touched_at=datetime.now(UTC))
    r = client.patch(
        "/api/v1/settings/sign-in", headers=session_headers(name, alices, alice_csrf), json=body
    )
    assert r.status_code == 403, r.text
    _, stale, stale_csrf = signed_in(client, "admin")
    r = client.patch(
        "/api/v1/settings/sign-in", headers=session_headers(name, stale, stale_csrf), json=body
    )
    assert r.status_code == 403 and r.json()["type"] == "StepUpRequired"
    _, fresh, fresh_csrf = signed_in(client, "admin", touched_at=datetime.now(UTC))
    r = client.patch(
        "/api/v1/settings/sign-in", headers=session_headers(name, fresh, fresh_csrf), json=body
    )
    assert r.status_code == 200, r.text
    assert r.json()["idle_timeout_minutes"] == 60 and r.json()["absolute_lifetime_days"] == 2
    assert (
        client.get("/api/v1/settings/sign-in", headers=AUTH_HEADER).json()["idle_timeout_minutes"]
        == 60
    )
