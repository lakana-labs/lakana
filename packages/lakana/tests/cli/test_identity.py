"""Tests for `lakana principal|group|token|task-token *`."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_BOB = {"name": "bob", "type": "SERVICE_ACCOUNT", "is_active": True, "held_admin": False}


def test_principal_create_maps_the_type_flag(tmp_path: Path) -> None:
    """`--type service-account` is sent as `SERVICE_ACCOUNT`."""
    api = FakeApi()
    api.on("POST", "/api/v1/principals", status=201, json=_BOB)

    result = invoke(
        make_state(tmp_path, api), "principal", "create", "bob", "--type", "service-account"
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[0].content) == {"name": "bob", "type": "SERVICE_ACCOUNT"}


def test_principal_list_get_and_deactivate(tmp_path: Path) -> None:
    """Listing and reading need no confirmation; deactivation does."""
    api = FakeApi()
    api.on("GET", "/api/v1/principals", json=[_BOB])
    api.on("GET", "/api/v1/principals/bob", json=_BOB)
    api.on("DELETE", "/api/v1/principals/bob", status=204)

    assert "bob" in invoke(make_state(tmp_path, api), "principal", "list").stdout
    assert (
        json.loads(
            invoke(make_state(tmp_path, api), "--output", "json", "principal", "get", "bob").stdout
        )
        == _BOB
    )
    assert invoke(make_state(tmp_path, api), "principal", "deactivate", "bob").exit_code == 2
    assert (
        invoke(make_state(tmp_path, api), "principal", "deactivate", "bob", "--yes").exit_code == 0
    )


def test_group_membership_commands(tmp_path: Path) -> None:
    """`members` lists principals; `remove-member` asks first."""
    api = FakeApi()
    api.on("POST", "/api/v1/groups", status=201, json={"name": "eng"})
    api.on("PUT", "/api/v1/groups/eng/members/bob", status=204)
    api.on("GET", "/api/v1/groups/eng/members", json=[_BOB])
    api.on("DELETE", "/api/v1/groups/eng/members/bob", status=204)

    assert invoke(make_state(tmp_path, api), "group", "create", "eng").exit_code == 0
    assert (
        "Added bob to group eng"
        in invoke(make_state(tmp_path, api), "group", "add-member", "eng", "bob").stdout
    )
    assert "bob" in invoke(make_state(tmp_path, api), "group", "members", "eng").stdout
    assert invoke(make_state(tmp_path, api), "group", "remove-member", "eng", "bob").exit_code == 2
    assert (
        invoke(make_state(tmp_path, api), "group", "remove-member", "eng", "bob", "--yes").exit_code
        == 0
    )


def test_group_list_and_delete(tmp_path: Path) -> None:
    """`group list` shows groups; `group delete` asks first."""
    api = FakeApi()
    api.on("GET", "/api/v1/groups", json=[{"name": "eng"}])
    api.on("DELETE", "/api/v1/groups/eng", status=204)

    assert "eng" in invoke(make_state(tmp_path, api), "group", "list").stdout
    assert invoke(make_state(tmp_path, api), "group", "delete", "eng").exit_code == 2
    assert invoke(make_state(tmp_path, api), "group", "delete", "eng", "--yes").exit_code == 0


def test_task_token_mint_json_is_one_valid_document(tmp_path: Path) -> None:
    """`--output json` stdout parses as one JSON object with the token."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/task-tokens",
        status=201,
        json={"key_id": "t1", "token": "lakana_task_t1_newsecret"},
    )

    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "task-token",
        "mint",
        "bob",
        "--scope",
        "bronze.*",
        "--ttl",
        "300",
        "--env",
        "dev",
    )

    assert json.loads(result.stdout) == {"key_id": "t1", "token": "lakana_task_t1_newsecret"}
    assert "can't be shown again" in result.stderr


def test_token_issue_prints_the_token_exactly_once(tmp_path: Path) -> None:
    """Table mode: the full token appears once on stdout, the warning on stderr."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/principals/bob/tokens",
        status=201,
        json={"key_id": "k9", "name": "ci", "token": "lakana_pat_k9_newsecret"},
    )

    result = invoke(make_state(tmp_path, api), "token", "issue", "bob", "ci")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.count("lakana_pat_k9_newsecret") == 1
    assert "can't be shown again" in result.stderr


def test_token_issue_json_is_one_valid_document(tmp_path: Path) -> None:
    """Review focus 4: `--output json` stdout parses as one JSON object with the token."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/principals/bob/tokens",
        status=201,
        json={"key_id": "k9", "name": "ci", "token": "lakana_pat_k9_newsecret"},
    )

    result = invoke(make_state(tmp_path, api), "--output", "json", "token", "issue", "bob", "ci")

    assert json.loads(result.stdout) == {
        "key_id": "k9",
        "name": "ci",
        "token": "lakana_pat_k9_newsecret",
    }
    assert "can't be shown again" in result.stderr


def test_token_list_and_revoke(tmp_path: Path) -> None:
    """Listing never shows secrets; revoking asks first."""
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/principals/bob/tokens",
        json=[{"key_id": "k9", "name": "ci", "last_used_at": None}],
    )
    api.on("DELETE", "/api/v1/principals/bob/tokens/k9", status=204)

    assert "k9" in invoke(make_state(tmp_path, api), "token", "list", "bob").stdout
    assert invoke(make_state(tmp_path, api), "token", "revoke", "bob", "k9").exit_code == 2
    assert invoke(make_state(tmp_path, api), "token", "revoke", "bob", "k9", "--yes").exit_code == 0


def test_task_token_mint_and_revoke(tmp_path: Path) -> None:
    """`mint` sends scope, TTL and the environment; `revoke` asks first."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/task-tokens",
        status=201,
        json={"key_id": "t1", "token": "lakana_task_t1_newsecret"},
    )
    api.on("DELETE", "/api/v1/task-tokens/t1", status=204)

    minted = invoke(
        make_state(tmp_path, api),
        "task-token",
        "mint",
        "bob",
        "--scope",
        "bronze.*",
        "--scope",
        "gold.sales",
        "--ttl",
        "300",
        "--env",
        "dev",
    )

    assert minted.exit_code == 0, minted.stderr
    assert minted.stdout.count("lakana_task_t1_newsecret") == 1
    assert json.loads(api.requests[0].content) == {
        "principal": "bob",
        "environment": "dev",
        "asset_scope": ["bronze.*", "gold.sales"],
        "ttl_seconds": 300,
    }
    assert invoke(make_state(tmp_path, api), "task-token", "revoke", "t1").exit_code == 2
    assert invoke(make_state(tmp_path, api), "task-token", "revoke", "t1", "--yes").exit_code == 0


def test_a_group_name_with_an_escape_and_a_line_break_is_shown_escaped(tmp_path: Path) -> None:
    """What the user typed is echoed with `done`, which escapes it."""
    name = "ops\x1b[2J\nforged line"
    api = FakeApi()
    api.on("POST", "/api/v1/groups", status=201, json={"name": name})
    result = invoke(make_state(tmp_path, api), "group", "create", name)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "Created group ops\\x1b[2J\\nforged line\n"
