"""`lakana raw ls` (#672)."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_BASE = "/api/v1/environments/prod/raw"
_PAGE = {
    "path": "raw",
    "items": [
        {"name": "a.csv", "kind": "file", "size": 5, "claimed_by": None},
        {
            "name": "orders",
            "kind": "folder",
            "size": None,
            "claimed_by": {"table": "bronze.orders"},
        },
        {"name": "secret", "kind": "folder", "size": None, "claimed_by": {"table": None}},
    ],
    "next_cursor": "c1",
}


def test_raw_ls_prints_the_folder_and_the_next_page(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json=_PAGE)
    result = invoke(make_state(tmp_path, api), "raw", "ls", "--env", "prod", "--limit", "3")
    assert result.exit_code == 0, result.output
    rows = [[c for c in line.split() if c not in "│┃"] for line in result.output.splitlines()]
    assert ["a.csv", "file", "5"] in rows
    assert ["orders/", "folder", "bronze.orders"] in rows
    assert ["secret/", "folder", "(a", "table", "you", "may", "not", "read)"] in rows
    assert "More results: lakana raw ls raw --env prod --limit 3 --cursor c1" in result.output
    assert dict(api.requests[-1].url.params) == {"path": "raw", "limit": "3"}


def test_raw_ls_of_a_folder_sends_it_and_says_when_it_is_empty(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json={"path": "raw/orders", "items": [], "next_cursor": None})
    result = invoke(make_state(tmp_path, api), "raw", "ls", "raw/orders/", "--env", "prod")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "Nothing in raw/orders."
    assert dict(api.requests[-1].url.params) == {"path": "raw/orders/"}


def test_raw_ls_with_output_json_prints_the_entries(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json={**_PAGE, "next_cursor": None})
    result = invoke(make_state(tmp_path, api), "--output", "json", "raw", "ls", "--env", "prod")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[1] == {
        "name": "orders",
        "kind": "folder",
        "size": None,
        "claimed_by": {"table": "bronze.orders"},
    }
