"""`lakana login` through a browser, against the real app (#32, PR 3).

The browser is played by the test: it confirms in a signed-in session, as the interface's
confirmation page does, and brings the code back to the command line's real loopback listener;
for `--device` it types the code the command line was given.
"""

from __future__ import annotations

import http.client
import threading
import tomllib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

import lakana
import lakana_server.ui
from lakana._models import DeviceSignIn
from lakana.cli._state import CliState
from lakana_core.models import CliGrant
from lakana_server.app import create_app
from lakana_server.settings import Settings, UiConfig
from tests.cli.conftest import run_cli as run
from tests.conftest import AUTH_HEADER, PUBLIC, write_ui_dist
from tests.server_test_helpers import session_headers, signed_in


@pytest.fixture
def client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide the app with the interface on, the person `kim`, and no `Origin`.

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
        A started client, as the command line talks to the server.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    app = create_app(settings.model_copy(update={"ui": UiConfig(enabled=True, public_url=PUBLIC)}))
    with TestClient(app, base_url=PUBLIC) as c:
        body = {"name": "kim", "type": "USER"}
        assert c.post("/api/v1/principals", headers=AUTH_HEADER, json=body).status_code == 201
        yield c


def _kim(client: TestClient) -> dict[str, str]:
    """Sign `kim` in with a fresh touch, as the browser's request headers.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    dict of str to str
        The session's headers and the public `Origin`.
    """
    touched = datetime.now(UTC) - timedelta(minutes=1)
    return {**session_headers(*signed_in(client, "kim", touched_at=touched)), "Origin": PUBLIC}


def _state(client: TestClient, tmp_path: Path) -> CliState:
    """Build a command line that talks to the app, on the computer `laptop-7`.

    Parameters
    ----------
    client
        The client.
    tmp_path
        Where the profile file lives.

    Returns
    -------
    CliState
        The state.
    """
    return CliState(
        login_client_factory=lambda url, tok: lakana.Client(url=url, token=tok, http_client=client),
        is_interactive=lambda: False,
        config_path=tmp_path / "lakana" / "config.toml",
        hostname=lambda: "laptop-7",
        login_timeout=10.0,
    )


def _bring_back(port: int, query: dict[str, str]) -> None:
    """Send the browser to the loopback address, as the confirmation page does.

    Parameters
    ----------
    port
        The listener's port.
    query
        The code (or error) and the state.
    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", "/?" + urlencode(query))
    connection.getresponse().read()
    connection.close()


def test_login_through_the_browser_saves_a_working_token(
    client: TestClient, tmp_path: Path
) -> None:
    state = _state(client, tmp_path)
    threads: list[threading.Thread] = []

    def confirm(url: str) -> bool:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert url.startswith(f"{PUBLIC}/sign-in/cli?")
        body = {
            "port": int(query["port"]),
            "code_challenge": query["challenge"],
            "host": query["host"],
        }
        approved = client.post("/api/v1/sign-in/cli/approvals", headers=_kim(client), json=body)
        assert approved.status_code == 200, approved.text
        answer = {"code": approved.json()["code"], "state": query["state"]}
        thread = threading.Thread(target=_bring_back, args=(approved.json()["port"], answer))
        thread.start()
        threads.append(thread)
        return True

    state.browser_opener = confirm
    result = run(state, "login", "--url", PUBLIC, "--browser")
    for thread in threads:
        thread.join()

    assert result.exit_code == 0, result.stderr
    assert f"Logged in as kim (USER) at {PUBLIC}" in result.stdout
    token = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]["token"]
    assert token not in result.stdout + result.stderr
    who = client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {token}"})
    assert who.json()["name"] == "kim"
    listed = client.get("/api/v1/principals/kim/tokens", headers=AUTH_HEADER).json()
    assert [row["name"] for row in listed] == ["lakana login on laptop-7"]


def test_login_with_a_device_code_saves_a_working_token(client: TestClient, tmp_path: Path) -> None:
    state = _state(client, tmp_path)
    started: list[DeviceSignIn] = []
    real = state.login_client_factory

    def factory(url: str, token: str) -> lakana.Client:
        built = real(url, token)
        start = built.sign_in.start_device

        def recorded(host: str) -> DeviceSignIn:
            started.append(start(host))
            return started[-1]

        built.sign_in.start_device = recorded  # type: ignore[method-assign]
        return built

    def wait(seconds: float) -> None:
        app = cast(FastAPI, client.app)
        with Session(app.state.engine) as session:
            session.execute(update(CliGrant).values(last_polled_at=None))
            session.commit()
            waiting = session.execute(select(CliGrant.approved_at)).scalar_one()
        if waiting is None:
            code = {"user_code": started[0].user_code}
            browser = _kim(client)
            assert client.post(
                "/api/v1/sign-in/cli/device/describe", headers=browser, json=code
            ).json() == {"host": "laptop-7"}
            approved = client.post("/api/v1/sign-in/cli/device/approve", headers=browser, json=code)
            assert approved.status_code == 204, approved.text

    state.login_client_factory = factory
    state.sleep = wait
    result = run(state, "login", "--url", PUBLIC, "--device")

    assert result.exit_code == 0, result.stderr
    assert f"{PUBLIC}/sign-in/device" in result.stderr
    assert started[0].user_code in result.stderr
    token = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]["token"]
    assert (
        client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {token}"}).json()["name"]
        == "kim"
    )
