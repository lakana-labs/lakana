"""A server that cannot be reached: the error names the profile, its URL and the next step."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest
from conftest import FakeApi, invoke, make_state

from lakana.cli import _profiles
from lakana.cli._state import CliState

_INVITES = "/api/v1/invites"


@pytest.fixture(autouse=True)
def _no_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the SDK's retries from waiting (`invoke` already clears `LAKANA_*`).

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch fixture.
    """
    monkeypatch.setattr("lakana._http.time.sleep", lambda _seconds: None)


def _unreachable(tmp_path: Path, cause: BaseException, *profiles: tuple[str, str]) -> CliState:
    """Build a state whose server fails with `cause`, and a profile file with `profiles`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    cause
        What the operating system said (the cause of httpx's error).
    *profiles
        Each profile's name and URL; the first is the default.

    Returns
    -------
    CliState
        The state.
    """
    api = FakeApi()

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(str(cause), request=request) from cause

    api.routes["GET", _INVITES] = fail
    state = make_state(tmp_path, api)
    for name, url in profiles:
        _profiles.set_profile(
            state.profile_path(), name, url=url, token="lakana_pat_k_s", default_environment=None
        )
    return state


_REFUSED = ConnectionRefusedError(111, "Connection refused")


def test_an_unreachable_profile_is_named_with_the_next_step(tmp_path: Path) -> None:
    """The default profile `team` is down while `local` runs: the error says how to use `local`."""
    state = _unreachable(
        tmp_path, _REFUSED, ("team", "http://127.0.0.1:18999"), ("local", "http://localhost:8000")
    )

    result = invoke(state, "invite", "list")

    assert result.exit_code == 11
    text = " ".join(result.stderr.split())
    assert "Could not reach profile 'team' at http://127.0.0.1:18999 (connection refused)." in text
    assert "Start that instance, or use another profile: lakana --profile local" in text
    assert "lakana profile use local" in text
    assert "lakana profile list" in text


def test_the_next_step_is_in_the_json_error_too(tmp_path: Path) -> None:
    """JSON output carries no hint line, so the message itself names the next step."""
    state = _unreachable(
        tmp_path, _REFUSED, ("team", "http://127.0.0.1:18999"), ("local", "http://localhost:8000")
    )

    result = invoke(state, "--output", "json", "invite", "list")

    error = json.loads(result.stderr)["error"]
    assert error["type"] == "LakanaConnectionError"
    assert "profile 'team'" in error["message"] and "--profile local" in error["message"]


def test_the_only_profile_points_at_login(tmp_path: Path) -> None:
    """With no other profile, the next step is starting it or logging in to another instance."""
    state = _unreachable(tmp_path, _REFUSED, ("team", "http://127.0.0.1:18999"))

    result = invoke(state, "--profile", "team", "invite", "list")

    text = " ".join(result.stderr.split())
    assert "Could not reach profile 'team' at http://127.0.0.1:18999" in text
    assert "lakana login --profile NAME --url URL" in text


def test_a_url_from_the_environment_is_named_as_such(tmp_path: Path) -> None:
    """`LAKANA_URL` chose the server, not a profile: the error says so."""
    state = _unreachable(tmp_path, socket.gaierror(-2, "Name or service not known"))

    result = invoke(
        state,
        "invite",
        "list",
        env={"LAKANA_URL": "http://lakana.example:8000", "LAKANA_TOKEN": "lakana_pat_k_s"},
    )

    text = " ".join(result.stderr.split())
    assert (
        "Could not reach http://lakana.example:8000 (from LAKANA_URL) (host name not found)."
        in (text)
    )
    assert "Check the URL and that the server is running." in text
    assert "profile" not in text


def test_url_given_on_the_command_line_is_named_as_such(tmp_path: Path) -> None:
    """`--url` chose the server; a cause the CLI has no words for keeps httpx's name."""
    state = _unreachable(tmp_path, TimeoutError("timed out"), ("team", "http://127.0.0.1:18999"))

    result = invoke(state, "--url", "http://127.0.0.1:18999/", "invite", "list")

    text = " ".join(result.stderr.split())
    assert "Could not reach http://127.0.0.1:18999/ (from --url) (ConnectError)." in text


def test_no_profile_and_no_url_keeps_the_sdks_words(tmp_path: Path) -> None:
    """Nothing names the server (a client built some other way): the SDK's text and a check."""
    state = _unreachable(tmp_path, _REFUSED)

    result = invoke(state, "invite", "list")

    text = " ".join(result.stderr.split())
    assert "Could not reach http://lakana.test: ConnectError (connection refused)." in text
    assert "Check the URL and that the server is running." in text
