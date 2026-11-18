"""`grant list` without a filter, `access explain`, `--search` on principals and groups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from conftest import FakeApi, invoke, make_state

_GRANT = {
    "id": "g1",
    "principal_id": "p1",
    "group_id": None,
    "role": "VIEWER",
    "action": None,
    "kind": "INSTANCE",
    "environment": None,
    "namespace": None,
    "table": None,
    "granted_by": "p0",
    "granted_at": "2026-10-01T12:00:00+00:00",
    "note": None,
}


def _paged(path: str, rows: list[dict[str, Any]], cursor: str | None) -> FakeApi:
    """Build a fake that answers `path` with `rows` and, if given, a next-page cursor.

    Parameters
    ----------
    path
        The list's path.
    rows
        The rows of the page.
    cursor
        The `X-Next-Cursor` header, or `None` for the last page.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()

    def respond(request: httpx.Request) -> httpx.Response:
        headers = {} if cursor is None else {"X-Next-Cursor": cursor}
        return httpx.Response(200, json=rows, headers=headers)

    api.routes["GET", path] = respond
    return api


def test_grant_list_sends_every_filter_and_names_the_next_page(tmp_path: Path) -> None:
    api = _paged("/api/v1/grants", [_GRANT], "next-1")
    result = invoke(
        make_state(tmp_path, api),
        "grant",
        "list",
        "--group",
        "team",
        "--action",
        "read_table",
        "--env",
        "dev",
        "--ns",
        "gold",
        "--table",
        "orders",
        "--limit",
        "1",
    )
    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[0].url.params) == {
        "group_name": "team",
        "action": "READ_TABLE",
        "environment": "dev",
        "namespace": "gold",
        "table": "orders",
        "limit": "1",
    }
    assert (
        "More results: lakana grant list --group team --action READ_TABLE --env dev --ns gold "
        "--table orders --limit 1 --cursor next-1" in result.stdout
    )


def test_grant_list_by_kind_lists_one_level_and_keeps_it_on_the_next_page(tmp_path: Path) -> None:
    api = _paged("/api/v1/grants", [_GRANT], "next-1")
    result = invoke(make_state(tmp_path, api), "grant", "list", "--kind", "instance")
    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[0].url.params) == {"kind": "INSTANCE"}
    assert "More results: lakana grant list --kind INSTANCE --cursor next-1" in result.stdout


def test_grant_list_without_a_filter_sends_none(tmp_path: Path) -> None:
    api = _paged("/api/v1/grants", [_GRANT], None)
    api.on("GET", "/api/v1/principals", json=[])
    api.on("GET", "/api/v1/groups", json=[])
    result = invoke(make_state(tmp_path, api), "grant", "list", "--cursor", "c1")
    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[0].url.params) == {"cursor": "c1"}
    assert "More results" not in result.stdout


def test_principal_and_group_lists_search_and_page(tmp_path: Path) -> None:
    people = _paged(
        "/api/v1/principals", [{"name": "alice", "type": "USER", "is_active": True}], "p2"
    )
    result = invoke(make_state(tmp_path, people), "principal", "list", "--search", "ali")
    assert result.exit_code == 0, result.stderr
    assert dict(people.requests[0].url.params) == {"q": "ali"}
    assert "alice" in result.stdout
    assert "More results: lakana principal list --search ali --cursor p2" in result.stdout

    teams = _paged("/api/v1/groups", [{"name": "team"}], None)
    result = invoke(
        make_state(tmp_path, teams),
        "group",
        "list",
        "--search",
        "te",
        "--limit",
        "5",
        "--cursor",
        "g",
    )
    assert result.exit_code == 0, result.stderr
    assert dict(teams.requests[0].url.params) == {"q": "te", "limit": "5", "cursor": "g"}
    assert "More results" not in result.stdout


def _explained(**more: object) -> dict[str, object]:
    """Build an answer of the permissions route.

    Parameters
    ----------
    **more
        Fields to replace.

    Returns
    -------
    dict
        The body.
    """
    env = {"kind": "ENVIRONMENT", "environment": "dev", "namespace": None, "table": None}
    table = {"kind": "TABLE", "environment": "dev", "namespace": "gold", "table": "orders"}
    return {
        "principal": "alice",
        "active": True,
        "scope": table,
        "permissions": [
            {
                "action": "READ_TABLE",
                "allowed": True,
                "via": [
                    {
                        "kind": "role",
                        "grant_id": "g1",
                        "group": None,
                        "role": "VIEWER",
                        "scope": env,
                    },
                    {
                        "kind": "group",
                        "grant_id": "g2",
                        "group": "team",
                        "role": None,
                        "scope": table,
                    },
                ],
                "reason": None,
            },
            {
                "action": "DROP_TABLE",
                "allowed": False,
                "via": [],
                "reason": "'alice' may not DROP_TABLE this.",
            },
        ],
        "note": None,
        **more,
    }


def test_access_explain_prints_action_allowed_and_via(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/principals/alice/permissions", json=_explained())
    result = invoke(
        make_state(tmp_path, api), "access", "explain", "alice", "--scope", "dev/gold/orders"
    )
    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[0].url.params) == {
        "environment": "dev",
        "namespace": "gold",
        "table": "orders",
    }
    out = result.stdout
    assert out.splitlines()[0] == "What alice may do on table gold.orders in environment dev:"
    assert "role VIEWER on environment dev" in out
    assert "group team: READ_TABLE granted on table gold.orders in environment dev" in out
    assert "DROP_TABLE" in out and "no" in out
    assert "lakana grant create" in out


def test_access_explain_scopes(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/principals/alice/permissions",
        json=_explained(
            scope={"kind": "INSTANCE", "environment": None, "namespace": None, "table": None},
            active=False,
            note="This principal is deactivated.",
        ),
    )
    instance = invoke(make_state(tmp_path, api), "access", "explain", "alice")
    assert instance.exit_code == 0, instance.stderr
    assert dict(api.requests[-1].url.params) == {}
    assert instance.stdout.splitlines()[0] == "What alice may do on the instance:"
    assert "This principal is deactivated." in instance.stdout
    invoke(make_state(tmp_path, api), "access", "explain", "alice", "--scope", "dev")
    assert dict(api.requests[-1].url.params) == {"environment": "dev"}
    invoke(make_state(tmp_path, api), "access", "explain", "alice", "--scope", "dev/gold.eu")
    assert dict(api.requests[-1].url.params) == {"environment": "dev", "namespace": "gold.eu"}
    bad = invoke(make_state(tmp_path, api), "access", "explain", "alice", "--scope", "dev//x")
    assert bad.exit_code == 2
    assert "ENV/NAMESPACE/TABLE" in bad.stderr


def test_access_explain_json_is_the_answer(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/principals/alice/permissions", json=_explained())
    result = invoke(make_state(tmp_path, api), "--output", "json", "access", "explain", "alice")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == _explained()


def test_group_list_by_member_and_grant_list_by_role_send_them(tmp_path: Path) -> None:
    teams = _paged("/api/v1/groups", [{"name": "team"}], "g2")
    result = invoke(make_state(tmp_path, teams), "group", "list", "--member", "maria")
    assert result.exit_code == 0, result.stderr
    assert dict(teams.requests[0].url.params) == {"member": "maria"}
    assert "More results: lakana group list --member maria --cursor g2" in result.stdout

    api = _paged("/api/v1/grants", [_GRANT], "next-1")
    result = invoke(make_state(tmp_path, api), "grant", "list", "--role", "analyst")
    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[0].url.params) == {"role": "ANALYST"}
    assert "More results: lakana grant list --role ANALYST --cursor next-1" in result.stdout
