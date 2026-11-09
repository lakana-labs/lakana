"""Tests for `lakana invite`, `recovery`, `session`, `passkey` and `sign-in` commands."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_ID = "0b6f7e9e-3c39-4d55-9d3a-2a7c1d5e4f10"
_WHEN = "2026-11-08T10:00:00Z"
_LINK = "https://lakana.example.com/sign-in/abc#s=topsecret"
_SETTINGS = {
    "idle_timeout_minutes": 720,
    "absolute_lifetime_minutes": 10080,
    "bounds": {
        "idle_timeout_minutes_min": 15,
        "idle_timeout_minutes_max": 1440,
        "absolute_lifetime_minutes_min": 60,
        "absolute_lifetime_minutes_max": 43200,
    },
}


def _one_line(text: str) -> str:
    """Join wrapped output into one line.

    Parameters
    ----------
    text
        The output.

    Returns
    -------
    str
        Its words separated by single spaces.
    """
    return " ".join(text.split())


def test_invite_create_prints_the_link_and_the_confirm_advice(tmp_path: Path) -> None:
    """The link goes to stdout alone; the advice names the person, the lifetime and roles."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/invites",
        status=201,
        json={
            "id": _ID,
            "name": "alice",
            "display_name": "Alice A.",
            "url": _LINK,
            "expires_at": _WHEN,
        },
    )
    result = invoke(
        make_state(tmp_path, api), "invite", "create", "alice", "--display-name", "Alice A."
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout == f"{_LINK}\n"
    assert json.loads(api.requests[0].content) == {"name": "alice", "display_name": "Alice A."}
    assert (
        "Send this link to alice yourself; it works once, for 7 days. Confirm with them before "
        "granting roles." in _one_line(result.stderr)
    )


def test_recovery_create_prints_the_link_and_the_one_hour_advice(tmp_path: Path) -> None:
    """A recovery link lasts one hour, and the advice says so."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/principals/bob/recovery-link",
        status=201,
        json={"id": _ID, "name": "bob", "url": _LINK, "expires_at": _WHEN},
    )
    result = invoke(make_state(tmp_path, api), "recovery", "create", "bob")

    assert result.exit_code == 0, result.stderr
    assert result.stdout == f"{_LINK}\n"
    assert (
        "Send this link to bob yourself; it works once, for 1 hour. Confirm with them before "
        "granting roles." in _one_line(result.stderr)
    )


def test_recovery_of_an_admin_names_the_operator_route(tmp_path: Path) -> None:
    """The server's 403 reaches the person as it is, with its next step."""
    detail = (
        "Recovering an administrator is for the server operator: run `lakana recovery create "
        "<name>` with the bootstrap token where the server runs."
    )
    api = FakeApi()
    api.on("POST", "/api/v1/principals/carol/recovery-link", status=403, json={"detail": detail})
    result = invoke(make_state(tmp_path, api), "recovery", "create", "carol")

    assert result.exit_code == 5
    assert result.stdout == ""
    assert detail in _one_line(result.stderr)


def test_invite_and_recovery_lists_show_no_link_and_withdraw_by_id(tmp_path: Path) -> None:
    """Lists name the person and the expiry; withdraw sends a DELETE by id."""
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/invites",
        json=[
            {
                "id": _ID,
                "name": "alice",
                "display_name": None,
                "created_at": _WHEN,
                "expires_at": _WHEN,
            }
        ],
    )
    api.on(
        "GET",
        "/api/v1/recovery-links",
        json=[{"id": _ID, "name": "bob", "created_at": _WHEN, "expires_at": _WHEN}],
    )
    api.on("DELETE", f"/api/v1/invites/{_ID}", status=204)
    api.on("DELETE", f"/api/v1/recovery-links/{_ID}", status=204)
    state = make_state(tmp_path, api)

    invites = invoke(state, "invite", "list")
    links = invoke(state, "recovery", "list")
    withdrawn = invoke(state, "invite", "withdraw", _ID)
    recovered = invoke(state, "recovery", "withdraw", _ID)

    assert invites.exit_code == 0 and "alice" in invites.stdout, invites.stderr
    assert links.exit_code == 0 and "bob" in links.stdout, links.stderr
    assert withdrawn.exit_code == 0 and "Withdrew" in withdrawn.stdout, withdrawn.stderr
    assert recovered.exit_code == 0 and "Withdrew" in recovered.stdout, recovered.stderr
    assert ("DELETE", f"/api/v1/recovery-links/{_ID}") in [
        (r.method, r.url.path) for r in api.requests
    ]


def test_session_list_defaults_to_yourself_and_end_asks_first(tmp_path: Path) -> None:
    """Without a name the caller's own sessions; `end` needs --yes without a terminal."""
    api = FakeApi()
    api.on("GET", "/api/v1/whoami", json={"name": "alice", "type": "USER", "task_token": None})
    api.on(
        "GET",
        "/api/v1/principals/alice/sessions",
        json=[
            {
                "id": _ID,
                "via": "passkey",
                "created_at": _WHEN,
                "last_seen_at": _WHEN,
                "absolute_expires_at": _WHEN,
                "user_agent": "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Firefox/158.0",
                "browser": "Firefox 158 on Linux",
                "peer": "127.0.0.1",
                "current": False,
            }
        ],
    )
    api.on("DELETE", "/api/v1/principals/bob/sessions", status=204)
    state = make_state(tmp_path, api)

    listed = invoke(state, "session", "list")
    refused = invoke(state, "session", "end", "bob")
    ended = invoke(state, "session", "end", "bob", "--yes")

    assert listed.exit_code == 0 and "Firefox 158 on Linux" in listed.stdout, listed.stderr
    assert "Mozilla/5.0" not in listed.stdout
    assert refused.exit_code == 2 and "--yes" in refused.stderr
    assert ended.exit_code == 0 and "bob" in ended.stdout, ended.stderr
    assert [(r.method, r.url.path) for r in api.requests][-1] == (
        "DELETE",
        "/api/v1/principals/bob/sessions",
    )


def test_passkey_list_of_another_person_and_remove(tmp_path: Path) -> None:
    """`passkey list NAME` and `passkey remove NAME ID --yes`."""
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/principals/bob/passkeys",
        json=[
            {
                "id": _ID,
                "name": "Laptop",
                "created_at": _WHEN,
                "last_used_at": None,
                "backup_eligible": True,
                "disabled_reason": None,
                "usable_here": True,
            }
        ],
    )
    api.on("DELETE", f"/api/v1/principals/bob/passkeys/{_ID}", status=204)
    state = make_state(tmp_path, api)

    listed = invoke(state, "passkey", "list", "bob")
    removed = invoke(state, "passkey", "remove", "bob", _ID, "--yes")

    assert listed.exit_code == 0 and "Laptop" in listed.stdout, listed.stderr
    assert removed.exit_code == 0 and "Removed" in removed.stdout, removed.stderr


def test_settings_without_options_shows_the_lengths_in_minutes(tmp_path: Path) -> None:
    """The lengths and their bounds, in minutes."""
    api = FakeApi()
    api.on("GET", "/api/v1/settings/sign-in", json=_SETTINGS)
    result = invoke(make_state(tmp_path, api), "sign-in", "settings")

    assert result.exit_code == 0, result.stderr
    out = _one_line(result.stdout)
    assert "Idle timeout: 720 minutes (15 to 1440)" in out
    assert "Absolute lifetime: 10080 minutes (60 to 43200)" in out


def test_settings_changes_only_the_given_length(tmp_path: Path) -> None:
    """`--idle-timeout` alone PATCHes the idle timeout alone, in minutes."""
    api = FakeApi()
    api.on("PATCH", "/api/v1/settings/sign-in", json={**_SETTINGS, "idle_timeout_minutes": 60})
    result = invoke(make_state(tmp_path, api), "sign-in", "settings", "--idle-timeout", "60")

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[0].content) == {"idle_timeout_minutes": 60}
    assert "Idle timeout: 60 minutes" in _one_line(result.stdout)
    assert "new sessions" in _one_line(result.stderr)


def test_settings_refuses_values_outside_the_bounds(tmp_path: Path) -> None:
    """Zero is refused before any request; the server's bound reaches the person as it is."""
    detail = "The idle timeout is 15 to 1440 minutes (24 hours); 2000 is outside."
    api = FakeApi()
    api.on("PATCH", "/api/v1/settings/sign-in", status=422, json={"detail": detail})
    state = make_state(tmp_path, api)

    zero = invoke(state, "sign-in", "settings", "--absolute-lifetime", "0")
    too_long = invoke(state, "sign-in", "settings", "--idle-timeout", "2000")

    assert zero.exit_code == 2 and "--absolute-lifetime" in zero.stderr
    assert too_long.exit_code == 8
    assert detail in _one_line(too_long.stderr)
    assert len(api.requests) == 1


def test_setup_link_prints_only_the_link(tmp_path: Path) -> None:
    """The setup link goes to stdout alone, so a script can pipe it to a browser."""
    api = FakeApi()
    api.on("POST", "/api/v1/sign-in/setup-link", json={"url": _LINK, "expires_at": _WHEN})
    result = invoke(make_state(tmp_path, api), "sign-in", "setup-link")

    assert result.exit_code == 0, result.stderr
    assert result.stdout == f"{_LINK}\n"
    assert "10 minutes" in _one_line(result.stderr)


def test_setup_link_after_setup_names_the_next_step(tmp_path: Path) -> None:
    """Once the first administrator has a passkey, the 409 names the recovery command."""
    detail = (
        "Already set up: the first administrator has a passkey, even if it stopped working "
        "after a change of the public address. The server operator recovers the account: run "
        "`lakana recovery create admin` with the bootstrap token where the server runs."
    )
    api = FakeApi()
    api.on("POST", "/api/v1/sign-in/setup-link", status=409, json={"detail": detail})
    result = invoke(make_state(tmp_path, api), "sign-in", "setup-link")

    assert result.exit_code == 7
    assert result.stdout == ""
    assert "lakana recovery create" in _one_line(result.stderr)


def test_json_output_has_the_whole_answer(tmp_path: Path) -> None:
    """`--output json`: the setup link and the settings come back as objects."""
    api = FakeApi()
    api.on("POST", "/api/v1/sign-in/setup-link", json={"url": _LINK, "expires_at": _WHEN})
    api.on("GET", "/api/v1/settings/sign-in", json=_SETTINGS)
    state = make_state(tmp_path, api)

    link = invoke(state, "--output", "json", "sign-in", "setup-link")
    settings = invoke(state, "--output", "json", "sign-in", "settings")

    assert json.loads(link.stdout)["url"] == _LINK
    assert json.loads(settings.stdout)["bounds"]["idle_timeout_minutes_max"] == 1440
