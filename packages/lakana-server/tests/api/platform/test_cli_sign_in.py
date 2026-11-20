"""`lakana login` through a browser: the loopback and device routes (#32, PR 3).

The browser's routes (`/api/v1/sign-in/cli/...`) need a session, and a fresh touch to approve;
the command line's routes (`/api/v1/auth/cli/...`) need no credential and no `Origin`, and are
rate limited. A code works once, only with its verifier; a device grant answers its token once.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana_server.api.platform.cli_sign_in as cli_sign_in
import lakana_server.ui
from lakana_core.models import AuditEvent, AuditEventType, CliGrant
from lakana_server.api.platform.cli_sign_in import WrongCodeLimit
from lakana_server.app import create_app
from lakana_server.auth_rate_limit import AuthAuditLimiter
from lakana_server.errors import TooManyAttemptsError
from lakana_server.settings import Settings, UiConfig
from tests.conftest import AUTH_HEADER, PUBLIC, write_ui_dist
from tests.server_test_helpers import session_headers, signed_in
from tests.test_step_up_routes import NO_STEP_UP, STEP_UP_ROUTES

APPROVALS = "/api/v1/sign-in/cli/approvals"
DESCRIBE = "/api/v1/sign-in/cli/device/describe"
APPROVE = "/api/v1/sign-in/cli/device/approve"
DENY = "/api/v1/sign-in/cli/device/deny"
TOKEN = "/api/v1/auth/cli/token"
DEVICE = "/api/v1/auth/cli/device"
POLL = "/api/v1/auth/cli/device/token"

VERIFIER = "Vq3" * 15
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=")


def _app(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ui: bool
) -> FastAPI:
    """Build an app with the interface on or off.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        For the stand-in build.
    monkeypatch
        Points the interface at the stand-in.
    ui
        Whether the interface is on.

    Returns
    -------
    FastAPI
        The app.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    return create_app(
        settings.model_copy(update={"ui": UiConfig(enabled=ui, public_url=PUBLIC if ui else None)})
    )


@pytest.fixture
def client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide a browser at the public address, with the person `kim`.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        For the stand-in build.
    monkeypatch
        Points the interface at the stand-in.

    Yields
    ------
    TestClient
        A started client that sends the public `Origin`.
    """
    app = _app(settings, tmp_path, monkeypatch, ui=True)
    with TestClient(app, base_url=PUBLIC, headers={"Origin": PUBLIC}) as c:
        body = {"name": "kim", "type": "USER"}
        assert c.post("/api/v1/principals", headers=AUTH_HEADER, json=body).status_code == 201
        yield c


@pytest.fixture
def program(client: TestClient) -> TestClient:
    """Provide the command line: the same app, without `Origin` or any credential.

    Parameters
    ----------
    client
        The started browser.

    Returns
    -------
    TestClient
        A client that sends neither.
    """
    return TestClient(client.app, base_url=PUBLIC)


def _browser(client: TestClient, *, stale: bool = False) -> dict[str, str]:
    """Sign `kim` in, with a fresh or a 16-minute-old touch.

    Parameters
    ----------
    client
        The client.
    stale
        Whether the touch is too old for step-up.

    Returns
    -------
    dict of str to str
        The session's headers.
    """
    touched = datetime.now(UTC) - timedelta(minutes=16 if stale else 1)
    return session_headers(*signed_in(client, "kim", touched_at=touched))


def _approve(client: TestClient, headers: dict[str, str], host: str = "laptop-7") -> str:
    """Confirm a loopback sign-in and read its code.

    Parameters
    ----------
    client
        The client.
    headers
        The session's headers.
    host
        The host name.

    Returns
    -------
    str
        The code.
    """
    body = {"port": 49152, "code_challenge": CHALLENGE.decode(), "host": host}
    r = client.post(APPROVALS, headers=headers, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["port"] == 49152
    return str(r.json()["code"])


def _events(client: TestClient, kind: AuditEventType) -> list[dict]:
    """Read the details of every event of one type.

    Parameters
    ----------
    client
        The client.
    kind
        The type.

    Returns
    -------
    list of dict
        The details.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        return [
            dict(detail)
            for detail in session.execute(
                select(AuditEvent.detail).where(AuditEvent.event_type == kind)
            ).scalars()
        ]


# -- loopback -----------------------------------------------------------------------------------


def test_approval_answers_a_code_for_a_session(client: TestClient) -> None:
    code = _approve(client, _browser(client))

    assert len(code) >= 43
    assert _events(client, AuditEventType.SIGN_IN_CLI_APPROVED)[0]["host"] == "laptop-7"


def test_approval_with_a_token_is_refused(client: TestClient) -> None:
    body = {"port": 49152, "code_challenge": CHALLENGE.decode(), "host": "h"}

    r = client.post(APPROVALS, headers=AUTH_HEADER, json=body)

    assert r.status_code == 403, r.text
    assert "an API token cannot confirm one" in r.json()["detail"]


def test_approval_needs_step_up(client: TestClient) -> None:
    body = {"port": 49152, "code_challenge": CHALLENGE.decode(), "host": "h"}

    r = client.post(APPROVALS, headers=_browser(client, stale=True), json=body)

    assert (r.status_code, r.json()["type"]) == (403, "StepUpRequired")


@pytest.mark.parametrize("port", [0, 80, 1023, 65536])
def test_approval_refuses_a_port_outside_1024_to_65535(client: TestClient, port: int) -> None:
    body = {"port": port, "code_challenge": CHALLENGE.decode(), "host": "h"}

    assert client.post(APPROVALS, headers=_browser(client), json=body).status_code == 422


def test_exchange_answers_the_token_once(client: TestClient, program: TestClient) -> None:
    code = _approve(client, _browser(client))

    r = program.post(TOKEN, json={"code": code, "code_verifier": VERIFIER})

    assert r.status_code == 200, r.text
    assert r.json()["name"] == "lakana login on laptop-7"
    who = program.get("/api/v1/whoami", headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert who.json()["name"] == "kim"
    again = program.post(TOKEN, json={"code": code, "code_verifier": VERIFIER})
    assert again.status_code == 400
    assert again.json()["type"] == "CliGrantRefused"


def test_code_without_the_right_verifier_is_refused_and_burnt(
    client: TestClient, program: TestClient
) -> None:
    code = _approve(client, _browser(client))

    wrong = program.post(TOKEN, json={"code": code, "code_verifier": "w" * 43})
    right = program.post(TOKEN, json={"code": code, "code_verifier": VERIFIER})

    assert (wrong.status_code, right.status_code) == (400, 400)
    assert "Run lakana login again" in wrong.json()["detail"]


def test_exchange_is_rate_limited(client: TestClient, program: TestClient) -> None:
    app = cast(FastAPI, client.app)
    app.state.cli_sign_in_limiter = AuthAuditLimiter(1)
    body = {"code": "x", "code_verifier": VERIFIER}

    assert program.post(TOKEN, json=body).status_code == 400
    r = program.post(TOKEN, json=body)

    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"


def test_the_network_rate_counts_too(client: TestClient, program: TestClient) -> None:
    app = cast(FastAPI, client.app)
    app.state.cli_sign_in_network_limiter = AuthAuditLimiter(1)
    body = {"code": "x", "code_verifier": VERIFIER}

    program.post(TOKEN, json=body)

    assert program.post(TOKEN, json=body).status_code == 429


def test_a_large_body_is_refused(program: TestClient) -> None:
    r = program.post(TOKEN, content=b'{"code": "' + b"x" * 20000 + b'"}')

    assert r.status_code == 413
    assert "command-line sign-in request" in r.json()["detail"]


def test_routes_never_log_the_code_or_verifier(
    client: TestClient, program: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    code = _approve(client, _browser(client))
    token = program.post(TOKEN, json={"code": code, "code_verifier": VERIFIER}).json()["token"]
    started = program.post(DEVICE, json={"host": "box"}).json()

    logged = caplog.text
    audited = str(_events(client, AuditEventType.SIGN_IN_CLI_APPROVED))
    audited += str(_events(client, AuditEventType.SIGN_IN_CLI_TOKEN_ISSUED))
    for secret in (code, VERIFIER, token, started["device_code"], started["user_code"]):
        assert secret not in logged
        assert secret not in audited


def test_new_routes_are_classified_for_step_up() -> None:
    assert ("POST", APPROVALS) in STEP_UP_ROUTES
    assert ("POST", APPROVE) in STEP_UP_ROUTES
    for route in (DESCRIBE, DENY, TOKEN, DEVICE, POLL):
        assert ("POST", route) in NO_STEP_UP


def test_every_route_answers_409_while_the_interface_is_off(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(settings, tmp_path, monkeypatch, ui=False)
    with TestClient(app) as c:
        for path in (APPROVALS, DESCRIBE, APPROVE, DENY, TOKEN, DEVICE, POLL):
            assert c.post(path, headers=AUTH_HEADER, json={}).status_code == 409, path


# -- device -------------------------------------------------------------------------------------


def _start(program: TestClient, host: str = "build-box") -> dict[str, str]:
    """Start a device grant.

    Parameters
    ----------
    program
        The command line.
    host
        Its host name.

    Returns
    -------
    dict of str to str
        The answer.
    """
    r = program.post(DEVICE, json={"host": host})
    assert r.status_code == 200, r.text
    return r.json()


def _rewind_poll(client: TestClient) -> None:
    """Move every grant's last poll 10 seconds back, as if the interval had passed.

    Parameters
    ----------
    client
        The client.
    """
    app = cast(FastAPI, client.app)
    with Session(app.state.engine) as session:
        for grant in session.execute(select(CliGrant)).scalars():
            if grant.last_polled_at is not None:
                grant.last_polled_at -= timedelta(seconds=10)
        session.commit()


def test_device_start_answers_url_code_and_interval(program: TestClient) -> None:
    started = _start(program)

    assert started["verification_url"] == f"{PUBLIC}/sign-in/device"
    assert (started["expires_in"], started["interval"]) == (600, 5)
    assert len(started["user_code"]) == 9


def test_device_flow_end_to_end(client: TestClient, program: TestClient) -> None:
    started = _start(program)
    browser = _browser(client)
    code = {"user_code": started["user_code"].lower().replace("-", "")}

    pending = program.post(POLL, json={"device_code": started["device_code"]})
    described = client.post(DESCRIBE, headers=browser, json=code)
    approved = client.post(APPROVE, headers=browser, json=code)
    _rewind_poll(client)
    polled = program.post(POLL, json={"device_code": started["device_code"]})

    assert pending.json() == {"state": "pending", "token": None}
    assert described.json() == {"host": "build-box"}
    assert approved.status_code == 204, approved.text
    assert polled.json()["state"] == "approved"
    assert polled.json()["token"]["name"] == "lakana login on build-box"
    _rewind_poll(client)
    after = program.post(POLL, json={"device_code": started["device_code"]})
    assert after.json() == {"state": "expired", "token": None}


def test_device_approval_needs_step_up_and_a_session(
    client: TestClient, program: TestClient
) -> None:
    code = {"user_code": _start(program)["user_code"]}

    stale = client.post(APPROVE, headers=_browser(client, stale=True), json=code)
    by_token = client.post(APPROVE, headers=AUTH_HEADER, json=code)

    assert (stale.status_code, stale.json()["type"]) == (403, "StepUpRequired")
    assert by_token.status_code == 403


def test_wrong_user_codes_are_limited(client: TestClient, program: TestClient) -> None:
    started = _start(program)
    browser = _browser(client)
    for _ in range(5):
        r = client.post(DESCRIBE, headers=browser, json={"user_code": "BBBB-BBBB"})
        assert r.status_code == 404, r.text
        assert "No sign-in is waiting for this code" in r.json()["detail"]

    sixth = client.post(DESCRIBE, headers=browser, json={"user_code": "BBBB-BBBB"})
    right = client.post(APPROVE, headers=browser, json={"user_code": started["user_code"]})

    assert sixth.status_code == 429
    assert right.status_code == 429
    assert "wait 10 minutes" in right.json()["detail"]


def test_poll_slow_down_is_429(program: TestClient) -> None:
    started = _start(program)
    program.post(POLL, json={"device_code": started["device_code"]})

    r = program.post(POLL, json={"device_code": started["device_code"]})

    assert r.status_code == 429
    assert r.json()["error"] == "slow_down"
    assert r.headers["Retry-After"] == "5"


def test_unknown_device_code_is_400(program: TestClient) -> None:
    r = program.post(POLL, json={"device_code": "nothing"})

    assert (r.status_code, r.json()["type"]) == (400, "CliGrantRefused")


def test_denied_grant_never_yields_a_token(client: TestClient, program: TestClient) -> None:
    started = _start(program)
    browser = _browser(client)

    denied = client.post(DENY, headers=browser, json={"user_code": started["user_code"]})
    polled = program.post(POLL, json={"device_code": started["device_code"]})
    late = client.post(APPROVE, headers=browser, json={"user_code": started["user_code"]})

    assert denied.status_code == 204, denied.text
    assert polled.json() == {"state": "denied", "token": None}
    assert late.status_code == 404


def test_device_start_is_rate_limited(client: TestClient, program: TestClient) -> None:
    cast(FastAPI, client.app).state.cli_sign_in_limiter = AuthAuditLimiter(2)

    statuses = [program.post(DEVICE, json={"host": "h"}).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]


# -- the wrong-code limit -----------------------------------------------------------------------


class _Ticks:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Read the clock.

        Returns
        -------
        float
            The time set.
        """
        return self.now


def test_wrong_codes_are_forgotten_after_the_window() -> None:
    ticks = _Ticks()
    limit = WrongCodeLimit(2, 600.0, ticks)
    limit.miss("kim")
    limit.miss("kim")
    with pytest.raises(TooManyAttemptsError):
        limit.check("kim")
    limit.check("bob")

    ticks.now = 600.0

    limit.check("kim")


def test_the_oldest_person_is_forgotten_past_the_key_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_sign_in, "_MAX_KEYS", 1)
    limit = WrongCodeLimit(1, 600.0, _Ticks())
    limit.miss("kim")
    limit.miss("bob")

    limit.check("kim")
