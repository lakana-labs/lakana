"""Tests for the passkey routes: sign-in, step-up, add, list, remove, and the passkey-first rule.

Every attestation and assertion comes from a software authenticator
(`scripts.test_support.webauthn_test_helpers`), verified by the real WebAuthn library.
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from scripts.test_support.webauthn_test_helpers import SoftAuthenticator

import lakana_server.ui
from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_server.api.platform.sign_in import client_bucket, network_bucket
from lakana_server.app import create_app
from lakana_server.auth_rate_limit import AuthAuditLimiter
from lakana_server.deps import PASSKEY_FIRST_ROUTES
from lakana_server.settings import Settings, UiConfig
from tests.conftest import _TEST_MASTER_KEY, AUTH_HEADER, PUBLIC, write_ui_dist
from tests.server_test_helpers import session_headers, signed_in
from tests.test_session_parity import _all_operations, _route_app

OPEN = ("/api/v1/sign-in/passkey/options", "/api/v1/sign-in/passkey")

PASSKEY_ROUTES = [
    ("POST", "/api/v1/sign-in/passkey/options"),
    ("POST", "/api/v1/sign-in/passkey"),
    ("POST", "/api/v1/sign-in/step-up/options"),
    ("POST", "/api/v1/sign-in/step-up"),
    ("GET", "/api/v1/principals/alice/passkeys"),
    ("POST", "/api/v1/principals/alice/passkeys/options"),
    ("POST", "/api/v1/principals/alice/passkeys"),
    ("DELETE", "/api/v1/principals/alice/passkeys/00000000-0000-0000-0000-000000000000"),
]


def _person(client: TestClient, name: str) -> None:
    """Create a person with no rights.

    Parameters
    ----------
    client
        A started client.
    name
        The name.
    """
    r = client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    assert r.status_code == 201, r.text


def _register(
    client: TestClient, name: str, headers: dict[str, str], auth: SoftAuthenticator | None = None
) -> tuple[SoftAuthenticator, dict[str, Any]]:
    """Add a passkey through the routes, as the interface does.

    Parameters
    ----------
    client
        A started client.
    name
        The person.
    headers
        Their session's cookie and CSRF headers.
    auth
        The authenticator; a new one by default.

    Returns
    -------
    tuple of (SoftAuthenticator, dict)
        The authenticator and the stored passkey's description.
    """
    auth = auth or SoftAuthenticator(origin=PUBLIC)
    r = client.post(f"/api/v1/principals/{name}/passkeys/options", headers=headers)
    assert r.status_code == 200, r.text
    options = r.json()
    r = client.post(
        f"/api/v1/principals/{name}/passkeys",
        headers=headers,
        json={
            "ceremony_id": options["ceremony_id"],
            "credential": json.loads(auth.create(options["public_key"])),
            "name": "laptop",
        },
    )
    assert r.status_code == 201, r.text
    return auth, r.json()


def _answer(auth: SoftAuthenticator, options: dict[str, Any], **get: Any) -> dict[str, Any]:  # ruff: ignore[any-type]
    """Build the body that answers authentication options.

    Parameters
    ----------
    auth
        The authenticator.
    options
        What the options route answered.
    **get
        Overrides for `SoftAuthenticator.get`.

    Returns
    -------
    dict
        ``{"ceremony_id", "credential"}``.
    """
    return {
        "ceremony_id": options["ceremony_id"],
        "credential": json.loads(auth.get(options["public_key"], **get)),
    }


@pytest.fixture
def client(ui_client: TestClient) -> TestClient:
    """Provide an app with the interface on and alice, a person with no rights.

    Parameters
    ----------
    ui_client
        A client of an app with the interface on.

    Returns
    -------
    TestClient
        The same client.
    """
    _person(ui_client, "alice")
    return ui_client


@pytest.fixture
def alice(client: TestClient) -> dict[str, str]:
    """Sign alice in with an invite link and give her a passkey.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    dict of str to str
        Her session's headers.
    """
    name, value, csrf = signed_in(client, "alice", via="invite")
    return session_headers(name, value, csrf)


def test_full_browser_sign_in_sets_the_cookie(client: TestClient, alice: dict[str, str]) -> None:
    """Register after an invite, then sign in with only the passkey, as a browser does."""
    auth, _ = _register(client, "alice", alice)
    r = client.post("/api/v1/sign-in/passkey/options")
    assert r.status_code == 200, r.text
    assert r.json()["public_key"]["allowCredentials"] == []
    r = client.post("/api/v1/sign-in/passkey", json=_answer(auth, r.json()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "alice" and body["needs_passkey"] is False
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("lakana_session_8000=") and "httponly" in cookie.lower()
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    r = client.get("/api/v1/sign-in/session", headers=session_headers("lakana_session_8000", value))
    assert r.status_code == 200 and r.json()["via"] == "passkey"
    assert r.json()["step_up_until"] is not None


def test_a_refused_passkey_sign_in_answers_401(client: TestClient, alice: dict[str, str]) -> None:
    """A forged assertion is refused, with no cookie."""
    auth, _ = _register(client, "alice", alice)
    options = client.post("/api/v1/sign-in/passkey/options").json()
    r = client.post("/api/v1/sign-in/passkey", json=_answer(auth, options, forge=True))
    assert r.status_code == 401 and "set-cookie" not in r.headers


def test_adding_a_passkey_without_step_up_is_refused(
    client: TestClient, alice: dict[str, str]
) -> None:
    """A second passkey needs a fresh touch; a passkey session without one gets 403."""
    _register(client, "alice", alice)
    name, value, csrf = signed_in(client, "alice", via="passkey")
    r = client.post(
        "/api/v1/principals/alice/passkeys/options", headers=session_headers(name, value, csrf)
    )
    assert r.status_code == 403 and r.json()["type"] == "StepUpRequired"


def test_step_up_through_the_routes(client: TestClient, alice: dict[str, str]) -> None:
    """A touch through the step-up routes lets the session add another passkey."""
    auth, _ = _register(client, "alice", alice)
    name, value, csrf = signed_in(client, "alice", via="passkey")
    headers = session_headers(name, value, csrf)
    options = client.post("/api/v1/sign-in/step-up/options", headers=headers).json()
    r = client.post("/api/v1/sign-in/step-up", headers=headers, json=_answer(auth, options))
    assert r.status_code == 200, r.text
    until = datetime.fromisoformat(r.json()["step_up_until"])
    assert until > datetime.now(UTC) + timedelta(minutes=14)
    _register(client, "alice", headers)


def test_a_failed_step_up_answers_403(client: TestClient, alice: dict[str, str]) -> None:
    """A refused touch is 403, so the interface keeps its session."""
    auth, _ = _register(client, "alice", alice)
    options = client.post("/api/v1/sign-in/step-up/options", headers=alice).json()
    r = client.post("/api/v1/sign-in/step-up", headers=alice, json=_answer(auth, options, uv=False))
    assert r.status_code == 403 and r.json()["type"] == "PasskeyRefused"


def test_a_passkey_is_added_only_to_ones_own_account(
    client: TestClient, alice: dict[str, str]
) -> None:
    """Even an administrator cannot start a ceremony for someone else."""
    _register(client, "alice", alice)
    name, value, csrf = signed_in(client, "admin", touched_at=datetime.now(UTC))
    r = client.post(
        "/api/v1/principals/alice/passkeys/options", headers=session_headers(name, value, csrf)
    )
    assert r.status_code == 403 and "own account" in r.json()["detail"]


def test_a_token_cannot_add_a_passkey(client: TestClient) -> None:
    """Ceremonies belong to a browser session; the bootstrap token gets 409."""
    r = client.post("/api/v1/principals/admin/passkeys/options", headers=AUTH_HEADER)
    assert r.status_code == 409 and "browser session" in r.json()["detail"]


def test_list_and_remove_as_admin_ends_the_persons_sessions(
    client: TestClient, alice: dict[str, str]
) -> None:
    """The bootstrap token lists and removes alice's passkey; her session ends."""
    _, stored = _register(client, "alice", alice)
    r = client.get("/api/v1/principals/alice/passkeys", headers=AUTH_HEADER)
    assert r.status_code == 200
    [listed] = r.json()
    assert listed["id"] == stored["id"] and listed["name"] == "laptop"
    assert listed["usable_here"] is True and listed["disabled_reason"] is None
    assert set(listed) == {
        "id",
        "name",
        "created_at",
        "last_used_at",
        "backup_eligible",
        "disabled_reason",
        "usable_here",
    }
    r = client.delete(f"/api/v1/principals/alice/passkeys/{stored['id']}", headers=AUTH_HEADER)
    assert r.status_code == 204, r.text
    r = client.get("/api/v1/sign-in/session", headers=alice)
    assert r.status_code == 401 and r.json()["reason"] == "passkey_removed"


def test_usable_here_is_false_after_the_public_url_changes(
    client: TestClient,
    alice: dict[str, str],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passkey belongs to the address it was registered at."""
    _register(client, "alice", alice)
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path / "moved"))
    moved = settings.model_copy(
        update={"ui": UiConfig(enabled=True, public_url="https://lakana.example.org")}
    )
    with TestClient(create_app(moved)) as other:
        r = other.get("/api/v1/principals/alice/passkeys", headers=AUTH_HEADER)
    assert r.status_code == 200, r.text
    assert [p["usable_here"] for p in r.json()] == [False]


@pytest.mark.parametrize(("method", "path"), PASSKEY_ROUTES)
def test_every_passkey_route_answers_409_with_the_interface_off(
    settings: Settings, method: str, path: str
) -> None:
    """With `LAKANA_UI__ENABLED=false` there is no sign-in at all."""
    with TestClient(create_app(settings)) as off:
        body = None if method in {"GET", "DELETE"} else {}
        r = off.request(method, path, headers=AUTH_HEADER, json=body)
    assert r.status_code == 409, r.text


@pytest.mark.parametrize("path", OPEN)
def test_open_passkey_routes_refuse_a_large_body(client: TestClient, path: str) -> None:
    """At most 16 KiB, like the OIDC exchange."""
    body = {"ceremony_id": "x", "credential": {"pad": "a" * (17 * 1024)}}
    r = client.post(path, json=body)
    assert r.status_code == 413, r.text


def test_options_route_is_limited_per_client(client: TestClient) -> None:
    """Past the per-address rate: 429 with the reason and when to retry."""
    client.app.state.passkey_options_limiter = AuthAuditLimiter(2)
    for _ in range(2):
        assert client.post("/api/v1/sign-in/passkey/options").status_code == 200
    r = client.post("/api/v1/sign-in/passkey/options")
    assert r.status_code == 429
    assert r.json()["detail"] == "Too many sign-in attempts from this address; wait a minute."
    assert r.headers["retry-after"] == "60"


def test_the_options_limit_is_its_own_setting(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LAKANA_UI__PASSKEY_OPTIONS_PER_IP_PER_MINUTE`, 60 by default, apart from the audit rate."""
    assert UiConfig().passkey_options_per_ip_per_minute == 60
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    configured = settings.model_copy(
        update={"ui": UiConfig(enabled=True, passkey_options_per_ip_per_minute=3)}
    )
    configured.auth_audit.per_ip_per_minute = 1
    with TestClient(create_app(configured), base_url=PUBLIC, headers={"Origin": PUBLIC}) as c:
        codes = [c.post("/api/v1/sign-in/passkey/options").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("2001:db8:1:2::1", "2001:db8:1:2:ffff::9", True),
        ("2001:db8:1:2::1", "2001:db8:1:3::1", False),
        ("192.0.2.1", "192.0.2.2", False),
        ("testclient", "testclient", True),
    ],
)
def test_an_ipv6_client_is_limited_by_its_64(first: str, second: str, same: bool) -> None:
    """One /64 is one bucket: an attacker cannot rotate through its addresses."""
    assert (client_bucket(first) == client_bucket(second)) is same


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        ("2001:db8:1:2::1", "2001:db8:1:ff::1", True),
        ("2001:db8:1:2::1", "2001:db8:2:2::1", False),
        ("192.0.2.1", "192.0.2.200", True),
        ("192.0.2.1", "192.0.3.1", False),
        ("testclient", "testclient", True),
    ],
)
def test_a_network_is_limited_by_its_48_or_24(first: str, second: str, same: bool) -> None:
    """The coarser bucket: an IPv6 /48 or an IPv4 /24."""
    assert (network_bucket(first) == network_bucket(second)) is same


def test_the_options_route_is_also_limited_per_network(client: TestClient) -> None:
    """Past the network's rate: 429, even with addresses to spare."""
    assert UiConfig().passkey_options_per_network_per_minute == 240
    client.app.state.passkey_options_network_limiter = AuthAuditLimiter(2)
    codes = [client.post("/api/v1/sign-in/passkey/options").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_no_log_record_carries_the_credential_or_the_challenge(
    client: TestClient, alice: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Neither the browser's answer nor the challenge reaches a log, refused or not."""
    caplog.set_level(logging.DEBUG)
    auth, _ = _register(client, "alice", alice)
    options = client.post("/api/v1/sign-in/passkey/options").json()
    good = _answer(auth, options)
    client.post("/api/v1/sign-in/passkey", json=good)
    bad = _answer(auth, client.post("/api/v1/sign-in/passkey/options").json(), forge=True)
    client.post("/api/v1/sign-in/passkey", json=bad)
    secrets = [
        options["public_key"]["challenge"],
        good["credential"]["response"]["signature"],
        good["credential"]["response"]["clientDataJSON"],
        bad["credential"]["response"]["signature"],
    ]
    for record in caplog.records:
        text = record.getMessage()
        assert not any(secret in text for secret in secrets), text


def test_after_registering_the_session_needs_no_passkey(
    client: TestClient, alice: dict[str, str]
) -> None:
    """The session description reads real passkeys."""
    before = client.get("/api/v1/sign-in/session", headers=alice).json()
    assert before["needs_passkey"] is True
    _register(client, "alice", alice)
    after = client.get("/api/v1/sign-in/session", headers=alice).json()
    assert after["needs_passkey"] is False and after["offer_passkey"] is False


@pytest.fixture(scope="module")
def pending(
    tmp_path_factory: pytest.TempPathFactory, catalog_template: Path
) -> Iterator[tuple[TestClient, dict[str, str], list[tuple[str, str]]]]:
    """Start one app and a session from an invite link of a person without a passkey.

    Parameters
    ----------
    tmp_path_factory
        Pytest's session-wide temporary directory factory.
    catalog_template
        The read-only database with the schema already created.

    Yields
    ------
    tuple
        The client, the session's headers, and every `/api/v1` operation of the app.
    """
    root = tmp_path_factory.mktemp("pending")
    database = root / "lakana.db"
    shutil.copyfile(catalog_template, database)
    database.chmod(0o644)
    settings = Settings(
        database=DatabaseConfig(url=f"sqlite:///{database}"),
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="testsecret",
        secrets=SecretsConfig(master_key=_TEST_MASTER_KEY),
        ui=UiConfig(enabled=True),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(root))
        app = create_app(settings)
        with TestClient(app, base_url=PUBLIC, headers={"Origin": PUBLIC}) as c:
            _person(c, "alice")
            name, value, csrf = signed_in(c, "alice", via="recovery")
            yield c, session_headers(name, value, csrf), _all_operations(app)


_OPERATIONS = _all_operations(_route_app())
_OPEN_OPERATIONS = {
    ("POST", "/api/v1/auth/oidc/token"),
    ("POST", "/api/v1/sign-in/links/describe"),
    ("POST", "/api/v1/sign-in/links/redeem"),
    ("POST", "/api/v1/sign-in/passkey/options"),
    ("POST", "/api/v1/sign-in/passkey"),
}
"""Operations that take no credential, so the session plays no part."""

_GATED = [op for op in _OPERATIONS if op not in PASSKEY_FIRST_ROUTES | _OPEN_OPERATIONS]


@pytest.mark.parametrize(("method", "path"), _GATED, ids=[f"{m} {p}" for m, p in _GATED])
def test_a_session_that_needs_a_passkey_may_do_nothing_else(
    pending: tuple[TestClient, dict[str, str], list[tuple[str, str]]], method: str, path: str
) -> None:
    """Every other route answers 403 naming the next step: add a passkey."""
    client, headers, _ = pending
    url = path.replace("{name}", "alice")
    for part in [p for p in url.split("/") if p.startswith("{")]:
        url = url.replace(part, "00000000-0000-0000-0000-000000000000")
    body = None if method in {"GET", "DELETE", "HEAD"} else {}
    r = client.request(method, url, headers=headers, json=body)
    assert r.status_code == 403, r.text
    expected = "TokenOnlyRoute" if "/task-tokens" in path else "PasskeyRequired"
    assert r.json()["type"] == expected
    if expected == "PasskeyRequired":
        assert "Add a passkey first" in r.json()["detail"]


def test_the_gate_covers_every_operation_the_app_has(
    pending: tuple[TestClient, dict[str, str], list[tuple[str, str]]],
) -> None:
    """The parametrized list is the running app's, and the allowed routes exist."""
    _, _, operations = pending
    assert operations == _OPERATIONS
    assert set(operations) >= PASSKEY_FIRST_ROUTES


def test_a_session_that_needs_a_passkey_may_add_one_and_sign_out(
    client: TestClient, alice: dict[str, str]
) -> None:
    """The allowed routes work; once a passkey exists everything is open again."""
    assert client.get("/api/v1/whoami", headers=alice).status_code == 403
    _register(client, "alice", alice)
    assert client.get("/api/v1/whoami", headers=alice).status_code == 200
    r = client.delete("/api/v1/sign-in/session", headers=alice)
    assert r.status_code == 204
