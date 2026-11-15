"""Tests for `lakana ns *`."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeApi, invoke, make_state
from test_list_filters_cmd import _paged

from lakana.cli import _profiles


def test_ns_commands_use_the_profiles_default_environment(tmp_path: Path) -> None:
    """Without `--env`, `ns list` uses the profile's `default_environment`."""
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/environments/dev/namespaces",
        json=[{"path": "bronze", "properties": {}}],
    )
    state = make_state(tmp_path, api)
    _profiles.set_profile(state.profile_path(), "p", url="u", token="t", default_environment="dev")

    result = invoke(state, "ns", "list")

    assert result.exit_code == 0, result.stderr
    assert "bronze" in result.stdout


def test_ns_without_any_environment_is_a_usage_error(tmp_path: Path) -> None:
    """No `--env` and no profile default: exit 2, no request."""
    api = FakeApi()

    result = invoke(make_state(tmp_path, api), "ns", "list")

    assert result.exit_code == 2
    assert "No environment" in result.stderr
    assert api.requests == []


def test_ns_get(tmp_path: Path) -> None:
    """`ns get` shows a single namespace, resolved against `--env`."""
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/environments/dev/namespaces/bronze",
        json={"path": "bronze", "properties": {"o": "x"}},
    )

    result = invoke(
        make_state(tmp_path, api), "--output", "json", "ns", "get", "bronze", "--env", "dev"
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"path": "bronze", "properties": {"o": "x"}}


def test_ns_create_get_and_delete(tmp_path: Path) -> None:
    """Create sends properties; delete asks first and names the environment.

    `namespaces.create/delete` pass the dotted namespace path straight
    through as a path parameter. `HttpTransport._format_path` quotes it with
    `urllib.parse.quote(value, safe="")`, which never escapes `.` (an
    RFC 3986 unreserved character), so a multi-level path like `bronze.eu`
    appears in the URL unchanged, not as `bronze%1Feu` (the plan's guess).
    """
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/environments/dev/namespaces",
        status=201,
        json={"path": "bronze.eu", "properties": {"o": "x"}},
    )
    api.on("DELETE", "/api/v1/environments/dev/namespaces/bronze.eu", status=204)

    created = invoke(
        make_state(tmp_path, api),
        "ns",
        "create",
        "bronze.eu",
        "--env",
        "dev",
        "--property",
        "o=x",
    )
    deleted = invoke(
        make_state(tmp_path, api), "ns", "delete", "bronze.eu", "--env", "dev", "--yes"
    )

    assert created.exit_code == 0, created.stderr
    assert "Created namespace dev/bronze.eu" in created.stdout
    assert json.loads(api.requests[0].content)["properties"] == {"o": "x"}
    assert deleted.exit_code == 0, deleted.stderr
    assert "Deleted namespace dev/bronze.eu" in deleted.stdout


def test_ns_list_sends_parent_depth_and_pages(tmp_path: Path) -> None:
    """`--parent`, `--depth`, `--limit` and `--cursor` reach the route; a next cursor is named."""
    api = _paged(
        "/api/v1/environments/dev/namespaces", [{"path": "a.b", "properties": {}}], "next-1"
    )

    result = invoke(
        make_state(tmp_path, api),
        "ns",
        "list",
        "--env",
        "dev",
        "--parent",
        "a",
        "--depth",
        "1",
        "--limit",
        "1",
        "--cursor",
        "c0",
    )

    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[0].url.params) == {
        "parent": "a",
        "depth": "1",
        "limit": "1",
        "cursor": "c0",
    }
    assert (
        "More results: lakana ns list --env dev --parent a --depth 1 --limit 1 --cursor next-1"
        in " ".join(result.stdout.split())
    )
