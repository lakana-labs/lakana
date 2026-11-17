"""Tests for `lakana query saved *`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import FakeApi, invoke, make_state

_ID = "00000000-0000-0000-0000-0000000000aa"
_BASE = "/api/v1/environments/dev/saved-queries"
_SQL = "SELECT status, count(*) FROM gold.sales.orders GROUP BY status"


def _item(**changes: Any) -> dict[str, Any]:  # ruff: ignore[any-type]
    """Build a saved query body as the server answers it.

    Parameters
    ----------
    **changes
        Fields to replace.

    Returns
    -------
    dict of str to Any
        The body.
    """
    body: dict[str, Any] = {
        "id": _ID,
        "environment": "dev",
        "name": "Orders by status",
        "sql": _SQL,
        "visibility": "private",
        "owner": "alice",
        "former_owner": False,
        "actions": ["update", "delete"],
        "created_at": "2026-11-16T12:00:00+00:00",
        "updated_at": "2026-11-16T12:30:00+00:00",
    }
    body.update(changes)
    return {k: v for k, v in body.items() if v is not ...}


def test_create_prints_the_name_and_id(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_item())

    result = invoke(
        make_state(tmp_path, api),
        "query",
        "saved",
        "create",
        "Orders by status",
        _SQL,
        "--env",
        "dev",
        "--everyone",
    )

    assert result.exit_code == 0, result.stderr
    assert f"Saved query Orders by status ({_ID})" in result.stdout
    assert json.loads(api.requests[0].content) == {
        "name": "Orders by status",
        "sql": _SQL,
        "visibility": "everyone",
    }
    as_json = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "query",
        "saved",
        "create",
        "N",
        "S",
        "--env",
        "dev",
    )
    assert json.loads(as_json.stdout)["id"] == _ID
    assert json.loads(api.requests[1].content)["visibility"] == "private"


def test_create_reads_the_sql_from_stdin(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_item())

    result = invoke(
        make_state(tmp_path, api),
        "query",
        "saved",
        "create",
        "Orders",
        "-",
        "--env",
        "dev",
        input="SELECT 7\n",
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[0].content)["sql"] == "SELECT 7\n"


def test_list_all_environments_adds_the_environment_column_and_former_owner(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    rows = [
        _item(environment="prod", visibility="everyone", owner=None, former_owner=True),
        _item(id="b" * 8 + _ID[8:], environment="stage"),
    ]
    api.on("GET", "/api/v1/saved-queries", json={"items": rows, "next_cursor": "n1"})

    result = invoke(
        make_state(tmp_path, api), "query", "saved", "list", "--all-environments", "--limit", "5"
    )

    assert result.exit_code == 0, result.stderr
    header = next(line for line in result.stdout.splitlines() if " id " in line)
    assert header.index("owner") < header.index("environment")
    assert "former owner" in result.stdout
    assert "prod" in result.stdout and "stage" in result.stdout
    assert "More results: lakana query saved list --all-environments --limit 5 --cursor n1" in (
        result.stdout
    )
    both = invoke(
        make_state(tmp_path, api), "query", "saved", "list", "--all-environments", "--env", "dev"
    )
    assert both.exit_code == 2


def test_list_in_one_environment_has_no_environment_column(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json={"items": [_item()], "next_cursor": None})
    empty = FakeApi()
    empty.on("GET", _BASE, json={"items": [], "next_cursor": None})

    result = invoke(make_state(tmp_path, api), "query", "saved", "list", "--env", "dev")
    nothing = invoke(make_state(tmp_path, empty), "query", "saved", "list", "--env", "dev")

    header = next(line for line in result.stdout.splitlines() if " id " in line)
    assert "environment" not in header
    assert "alice" in result.stdout
    assert "No saved queries in dev." in nothing.stdout


def test_show_sql_prints_only_the_sql(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/{_ID}", json=_item())

    only = invoke(make_state(tmp_path, api), "query", "saved", "show", _ID, "--env", "dev", "--sql")
    full = invoke(make_state(tmp_path, api), "query", "saved", "show", _ID, "--env", "dev")

    assert only.exit_code == 0, only.stderr
    assert only.stdout == _SQL + "\n"
    assert "Orders by status" in full.stdout and _SQL in full.stdout


def test_show_without_readable_sql_says_so(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/{_ID}", json=_item(sql=..., actions=["delete"]))

    result = invoke(make_state(tmp_path, api), "query", "saved", "show", _ID, "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "You may not read the SQL of this query." in result.stdout
    assert "Orders by status" in result.stdout


def test_show_sql_without_readable_sql_exits_1(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/{_ID}", json=_item(sql=..., actions=["delete"]))

    result = invoke(
        make_state(tmp_path, api), "query", "saved", "show", _ID, "--env", "dev", "--sql"
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "You may not read the SQL of this query." in result.stderr


def test_show_sql_refuses_sql_with_hidden_characters(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/{_ID}", json=_item(sql="SELECT 1 -- \u202e\x1b[2J"))

    result = invoke(
        make_state(tmp_path, api), "query", "saved", "show", _ID, "--env", "dev", "--sql"
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "holds control or invisible characters; read it with --output json" in result.stderr


def test_update_reads_first_and_puts_the_merged_fields(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/{_ID}", json=_item())
    api.on("PUT", f"{_BASE}/{_ID}", json=_item(name="Renamed", visibility="everyone"))

    result = invoke(
        make_state(tmp_path, api),
        "query",
        "saved",
        "update",
        _ID,
        "--env",
        "dev",
        "--name",
        "Renamed",
        "--everyone",
    )

    assert result.exit_code == 0, result.stderr
    assert [r.method for r in api.requests] == ["GET", "PUT"]
    assert json.loads(api.requests[1].content) == {
        "name": "Renamed",
        "sql": _SQL,
        "visibility": "everyone",
    }
    both = invoke(
        make_state(tmp_path, api),
        "query",
        "saved",
        "update",
        _ID,
        "--env",
        "dev",
        "--private",
        "--everyone",
    )
    assert both.exit_code == 2
    none = invoke(make_state(tmp_path, api), "query", "saved", "update", _ID, "--env", "dev")
    assert none.exit_code == 2


def test_delete_asks_first_and_yes_skips_it(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("DELETE", f"{_BASE}/{_ID}", status=204)

    refused = invoke(make_state(tmp_path, api), "query", "saved", "delete", _ID, "--env", "dev")
    assert refused.exit_code == 2
    assert api.requests == []
    declined = invoke(
        make_state(tmp_path, api, interactive=True),
        "query",
        "saved",
        "delete",
        _ID,
        "--env",
        "dev",
        input="n\n",
    )
    assert declined.exit_code == 2
    assert api.requests == []

    done = invoke(
        make_state(tmp_path, api), "query", "saved", "delete", _ID, "--env", "dev", "--yes"
    )

    assert done.exit_code == 0, done.stderr
    assert f"Deleted saved query {_ID}" in done.stdout
    assert [r.method for r in api.requests] == ["DELETE"]


def test_a_server_refusal_prints_its_sentence(tmp_path: Path) -> None:
    sentence = (
        "Only the owner of a saved query can change it; save a copy under your own name instead."
    )
    api = FakeApi()
    api.on("GET", f"{_BASE}/{_ID}", json=_item(owner="bob", actions=[]))
    api.on("PUT", f"{_BASE}/{_ID}", status=403, json={"detail": sentence})

    result = invoke(
        make_state(tmp_path, api), "query", "saved", "update", _ID, "--env", "dev", "--name", "X"
    )

    assert result.exit_code != 0
    assert sentence in " ".join(result.stderr.split())
