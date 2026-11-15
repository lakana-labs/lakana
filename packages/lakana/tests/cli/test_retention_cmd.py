"""Tests for retention in the CLI: `table retention`, `table expiring`, `env retention`.

Also the warning in `table list` and `table get`, and `--expire-after` on `table create` and on
`sql --into ... --mode create`.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import FakeApi, invoke, make_state
from test_list_filters_cmd import _paged

from lakana.cli import _retention

_BASE = "/api/v1/environments/dev/namespaces/gold/tables"
_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
_TABLE = {
    "name": "orders",
    "namespace": "gold",
    "uuid": "u1",
    "metadata_location": "file:///m.json",
    "schema": [{"name": "id", "type": "long", "nullable": True}],
}
_RETENTION = {
    "name": "orders",
    "namespace": "gold",
    "uuid": "u1",
    "source": "table",
    "days": 30,
    "on": None,
    "never": False,
    "set_by": "alice",
    "set_at": "2026-10-01T09:00:00Z",
    "last_activity_at": "2026-10-01T09:00:00Z",
    "expires_at": "2026-10-31T09:00:00Z",
    "kept": None,
}


def _plain(text: str) -> str:
    """Return CLI output without ANSI codes or box borders, whitespace collapsed.

    Parameters
    ----------
    text
        Raw output.

    Returns
    -------
    str
        The plain, single-spaced text.
    """
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for border in "│╭╮╰╯─":
        plain = plain.replace(border, " ")
    return " ".join(plain.split())


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the CLI's clock for the "expires in" text.

    Parameters
    ----------
    monkeypatch
        Replaces `_retention._now`.
    """
    monkeypatch.setattr(_retention, "_now", lambda: _NOW)


def _bodies(api: FakeApi, method: str) -> list[object]:
    """Read the JSON bodies of the requests of one method.

    Parameters
    ----------
    api
        The fake.
    method
        The HTTP method.

    Returns
    -------
    list
        The decoded bodies, in order.
    """
    return [json.loads(r.content) for r in api.requests if r.method == method]


@pytest.mark.parametrize(
    ("moment", "text"),
    [
        ("2026-10-23T12:00:00Z", "expires in 3 days (2026-10-23 12:00 UTC)"),
        ("2026-10-21T12:30:00Z", "expires in 1 day (2026-10-21 12:30 UTC)"),
        ("2026-10-20T18:00:00Z", "expires in less than a day (2026-10-20 18:00 UTC)"),
        ("2026-10-19T00:00:00Z", "is due to expire at the next clean-up"),
    ],
)
def test_the_expiry_is_said_in_days_and_the_date(moment: str, text: str) -> None:
    assert _retention.expiry_text(datetime.fromisoformat(moment)) == text


def test_table_retention_without_an_option_shows_it(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/orders/retention", json=_RETENTION)
    result = invoke(make_state(tmp_path, api), "table", "retention", "gold.orders", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    text = _plain(result.stdout)
    assert "dev/gold.orders expires in 10 days (2026-10-31 09:00 UTC)" in text
    assert "30 days after its last activity" in text
    assert "set on the table by alice" in text


@pytest.mark.parametrize(
    ("body", "line"),
    [
        ({"source": None, "days": None}, "has no retention: it does not expire"),
        ({"source": "environment"}, "the retention of environment dev"),
        ({"never": True, "days": None, "expires_at": None}, "never expires"),
        ({"expires_at": None, "kept": "protected"}, "protected environment"),
        ({"expires_at": None, "kept": "project_asset"}, "deployed project"),
        ({"days": None, "on": "2026-11-02", "expires_at": "2026-11-02T00:00:00Z"}, "on 2026-11-02"),
    ],
)
def test_table_retention_explains_each_case(
    tmp_path: Path, body: dict[str, object], line: str
) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/orders/retention", json={**_RETENTION, **body})
    result = invoke(make_state(tmp_path, api), "table", "retention", "gold.orders", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    assert line in _plain(result.stdout)


def test_table_retention_sets_days_a_day_or_never(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("PUT", f"{_BASE}/orders/retention", json=_RETENTION)
    state = make_state(tmp_path, api)
    days = invoke(state, "table", "retention", "gold.orders", "--days", "30", "--env", "dev")
    on = invoke(state, "table", "retention", "gold.orders", "--on", "2026-11-02", "--env", "dev")
    never = invoke(state, "table", "retention", "gold.orders", "--never", "--env", "dev")
    for result in (days, on, never):
        assert result.exit_code == 0, result.stderr
    assert _bodies(api, "PUT") == [
        {"days": 30, "on": None, "never": False},
        {"days": None, "on": "2026-11-02", "never": False},
        {"days": None, "on": None, "never": True},
    ]
    assert "Set the retention of dev/gold.orders" in _plain(days.stdout)
    assert "expires in 10 days" in _plain(days.stdout)


def test_table_retention_refuses_two_options_before_sending(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "retention",
        "gold.orders",
        "--days",
        "3",
        "--never",
        "--env",
        "dev",
    )
    assert result.exit_code == 2
    assert "one of --days, --on or --never" in _plain(result.stderr + result.stdout)
    assert api.requests == []


def test_table_retention_refuses_a_malformed_day(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "retention",
        "gold.orders",
        "--on",
        "soon",
        "--env",
        "dev",
    )
    assert result.exit_code == 2
    assert "YYYY-MM-DD" in _plain(result.stderr + result.stdout)


def test_table_expiring_lists_what_is_due_with_the_rule(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/environments/dev/expiring-tables",
        json=[_RETENTION, {**_RETENTION, "name": "daily", "source": "environment", "set_by": None}],
    )
    result = invoke(
        make_state(tmp_path, api), "table", "expiring", "--within", "14d", "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    text = _plain(result.stdout)
    assert "gold.orders" in text and "gold.daily" in text
    assert "in 10 days" in text and "alice" in text
    assert "30 days after last activity (table)" in text
    assert "30 days after last activity (environment)" in text
    assert str(api.requests[0].url.query, "ascii") == "within_days=14"


def test_table_expiring_says_when_nothing_is_due(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/expiring-tables", json=[])
    result = invoke(make_state(tmp_path, api), "table", "expiring", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    assert "No table in dev expires within the warning window" in _plain(result.stdout)
    assert str(api.requests[0].url.query, "ascii") == ""


@pytest.mark.parametrize("within", ["0", "x", "7w", "3651d"])
def test_table_expiring_refuses_a_bad_window(tmp_path: Path, within: str) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api), "table", "expiring", "--within", within, "--env", "dev"
    )
    assert result.exit_code == 2
    assert "--within" in _plain(result.stderr + result.stdout)


def test_table_list_shows_an_expires_column_only_when_one_expires_soon(tmp_path: Path) -> None:
    api = FakeApi()
    soon = {**_TABLE, "expires_at": "2026-10-23T12:00:00Z", "expires_soon": True}
    api.on("GET", _BASE, json=[soon, {**_TABLE, "name": "other"}])
    listed = invoke(make_state(tmp_path, api), "table", "list", "gold", "--env", "dev")
    assert listed.exit_code == 0, listed.stderr
    assert "expires" in _plain(listed.stdout) and "in 3 days" in _plain(listed.stdout)

    calm = FakeApi()
    calm.on("GET", _BASE, json=[_TABLE])
    plain = invoke(make_state(tmp_path, calm), "table", "list", "gold", "--env", "dev")
    assert "expires" not in _plain(plain.stdout)


def test_table_get_warns_of_an_expiry(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        f"{_BASE}/orders",
        json={**_TABLE, "expires_at": "2026-10-23T12:00:00Z", "expires_soon": True},
    )
    result = invoke(make_state(tmp_path, api), "table", "get", "gold.orders", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    text = _plain(result.stdout)
    assert "This table expires in 3 days (2026-10-23 12:00 UTC)" in text
    assert "lakana table retention gold.orders --env dev --never" in text


def test_table_create_with_expire_after_sets_the_retention(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_TABLE)
    api.on("PUT", f"{_BASE}/orders/retention", json=_RETENTION)
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "create",
        "gold.orders",
        "--column",
        "id:long",
        "--expire-after",
        "30",
        "--env",
        "dev",
    )
    assert result.exit_code == 0, result.stderr
    assert _bodies(api, "PUT") == [{"days": 30, "on": None, "never": False}]
    assert "expires 30 days after its last activity" in _plain(result.stdout)


def test_expire_after_that_fails_says_the_table_exists_and_how_to_retry(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_TABLE)
    api.on("PUT", f"{_BASE}/orders/retention", status=403, json={"detail": "may not"})
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "create",
        "gold.orders",
        "--column",
        "id:long",
        "--expire-after",
        "30",
        "--env",
        "dev",
    )
    assert result.exit_code == 1
    text = _plain(result.stderr + result.stdout)
    assert "Created table dev/gold.orders, but could not set its retention" in text
    assert "lakana table retention gold.orders --days 30 --env dev" in text


def test_expire_after_refuses_break_glass(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "create",
        "gold.orders",
        "--column",
        "id:long",
        "--expire-after",
        "30",
        "--break-glass",
        "--reason",
        "fixing the broken load",
        "--env",
        "dev",
    )
    assert result.exit_code == 2
    assert "never expire" in _plain(result.stderr + result.stdout)
    assert api.requests == []


def test_sql_into_create_with_expire_after_sets_the_retention(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/queries", status=202, json={"query_id": "q1"})
    api.on(
        "GET",
        "/api/v1/environments/dev/queries/q1",
        json={
            "query_id": "q1",
            "status": "SUCCEEDED",
            "error_message": None,
            "lineage": None,
            "write_mode": "create",
            "target": "gold.orders",
            "rows_written": 2,
            "snapshot_id": 5,
            "watermark": None,
            "schema_diff": None,
        },
    )
    api.on("PUT", f"{_BASE}/orders/retention", json=_RETENTION)
    result = invoke(
        make_state(tmp_path, api),
        "sql",
        "SELECT 1 AS id",
        "--into",
        "gold.orders",
        "--mode",
        "create",
        "--expire-after",
        "7",
        "--env",
        "dev",
    )
    assert result.exit_code == 0, result.stderr
    assert _bodies(api, "PUT") == [{"days": 7, "on": None, "never": False}]
    assert "expires 7 days after its last activity" in _plain(result.stdout)


def test_sql_expire_after_refuses_break_glass(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "sql",
        "SELECT 1 AS id",
        "--into",
        "gold.orders",
        "--mode",
        "create",
        "--expire-after",
        "7",
        "--break-glass",
        "--reason",
        "fixing the broken load",
        "--env",
        "dev",
    )
    assert result.exit_code == 2
    assert "never expire" in _plain(result.stderr + result.stdout)
    assert api.requests == []


@pytest.mark.parametrize("mode", ["append", None])
def test_sql_expire_after_needs_mode_create(tmp_path: Path, mode: str | None) -> None:
    api = FakeApi()
    args = ["sql", "SELECT 1", "--expire-after", "7", "--env", "dev"]
    if mode is not None:
        args += ["--into", "gold.orders", "--mode", mode]
    result = invoke(make_state(tmp_path, api), *args)
    assert result.exit_code == 2
    assert "--expire-after needs --mode create" in _plain(result.stderr + result.stdout)
    assert api.requests == []


def test_env_retention_sets_shows_and_turns_off(tmp_path: Path) -> None:
    api = FakeApi()
    env = {
        "name": "dev",
        "storage_root": "file:///lake",
        "storage_secret_ref": None,
        "is_protected": False,
        "properties": {},
        "retention_days": 30,
    }
    api.on("PUT", "/api/v1/environments/dev/retention", json=env)
    api.on("GET", "/api/v1/environments/dev", json=env)
    state = make_state(tmp_path, api)
    set_ = invoke(state, "env", "retention", "dev", "--days", "30", "--yes")
    shown = invoke(state, "env", "retention", "dev")
    api.on("PUT", "/api/v1/environments/dev/retention", json={**env, "retention_days": None})
    off = invoke(state, "env", "retention", "dev", "--off")
    for result in (set_, shown, off):
        assert result.exit_code == 0, result.stderr
    assert _bodies(api, "PUT") == [{"days": 30}, {"days": None}]
    assert "Tables in dev that set no retention of their own now expire 30 days" in _plain(
        set_.stdout
    )
    assert "Tables in dev that set no retention of their own expire 30 days" in _plain(shown.stdout)
    assert "dev has no retention" in _plain(off.stdout)


def test_env_retention_refuses_both_options(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(make_state(tmp_path, api), "env", "retention", "dev", "--days", "3", "--off")
    assert result.exit_code == 2
    assert "--days or --off" in _plain(result.stderr + result.stdout)


def test_the_clock_is_utc_and_an_empty_expiry_is_an_empty_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    assert _retention._now().tzinfo is UTC
    assert _retention.expires_cell(None) == ""


def test_json_output_prints_the_retention_objects(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/orders/retention", json=_RETENTION)
    api.on(
        "GET",
        "/api/v1/environments/dev",
        json={
            "name": "dev",
            "storage_root": "file:///lake",
            "storage_secret_ref": None,
            "is_protected": False,
            "properties": {},
            "retention_days": None,
        },
    )
    state = make_state(tmp_path, api)
    table = invoke(state, "--output", "json", "table", "retention", "gold.orders", "--env", "dev")
    env = invoke(state, "--output", "json", "env", "retention", "dev")
    assert json.loads(table.stdout)["days"] == 30
    assert json.loads(env.stdout)["retention_days"] is None


def test_table_get_says_nothing_of_an_expiry_outside_the_window(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/orders", json={**_TABLE, "expires_at": "2026-12-01T00:00:00Z"})
    result = invoke(make_state(tmp_path, api), "table", "get", "gold.orders", "--env", "dev")
    assert result.exit_code == 0, result.stderr
    assert "expires" not in _plain(result.stdout)


def test_table_expiring_sends_namespace_and_pages(tmp_path: Path) -> None:
    api = _paged("/api/v1/environments/dev/expiring-tables", [_RETENTION], "e2")
    result = invoke(
        make_state(tmp_path, api),
        "table",
        "expiring",
        "--env",
        "dev",
        "--namespace",
        "gold",
        "--limit",
        "1",
        "--cursor",
        "e1",
    )
    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[0].url.params) == {"namespace": "gold", "limit": "1", "cursor": "e1"}
    assert (
        "More results: lakana table expiring --env dev --namespace gold --limit 1 --cursor e2"
        in _plain(result.stdout)
    )
