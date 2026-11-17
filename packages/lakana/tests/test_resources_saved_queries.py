"""The SDK's saved queries resource: its routes, bodies and answers."""

from __future__ import annotations

import json
from typing import Any

import httpx

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana.resources.saved_queries import SavedQueries

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_ID = "00000000-0000-0000-0000-0000000000aa"
_ITEM: dict[str, Any] = {
    "id": _ID,
    "environment": "dev",
    "name": "Orders by status",
    "sql": "SELECT 1",
    "visibility": "private",
    "owner": "alice",
    "former_owner": False,
    "actions": ["update", "delete"],
    "created_at": "2026-11-16T12:00:00+00:00",
    "updated_at": "2026-11-16T12:30:00+00:00",
}


def _resource(answers: list[tuple[int, Any]], seen: list[httpx.Request]) -> SavedQueries:
    """Build the resource over a transport that answers successive requests in turn.

    Parameters
    ----------
    answers
        The status and JSON body of each answer; the last repeats.
    seen
        Where to record the requests.

    Returns
    -------
    SavedQueries
        The resource.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = answers[min(len(seen), len(answers) - 1)]
        seen.append(request)
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return SavedQueries(HttpTransport(_CONFIG, http_client=client))


def test_list_without_environment_uses_the_cross_environment_route() -> None:
    seen: list[httpx.Request] = []
    resource = _resource([(200, {"items": [_ITEM], "next_cursor": "n1"})], seen)

    page = resource.list(limit=7, cursor="c0")

    assert seen[0].url.path == "/api/v1/saved-queries"
    assert dict(seen[0].url.params) == {"limit": "7", "cursor": "c0"}
    assert (page.next_cursor, [q.id for q in page.items]) == ("n1", [_ID])
    assert page.items[0].actions == ("update", "delete")
    assert page.items[0].created_at.isoformat() == "2026-11-16T12:00:00+00:00"
    resource.list("dev")
    assert seen[1].url.path == "/api/v1/environments/dev/saved-queries"


def test_iter_follows_next_cursor() -> None:
    seen: list[httpx.Request] = []
    resource = _resource(
        [
            (200, {"items": [_ITEM], "next_cursor": "c1"}),
            (200, {"items": [_ITEM], "next_cursor": None}),
        ],
        seen,
    )

    assert len(list(resource.iter("dev"))) == 2
    assert [r.url.params.get("cursor") for r in seen] == [None, "c1"]


def test_create_sends_name_sql_and_visibility() -> None:
    seen: list[httpx.Request] = []
    resource = _resource([(201, _ITEM)], seen)

    created = resource.create("dev", "Orders by status", "SELECT 1", visibility="everyone")

    assert (seen[0].method, seen[0].url.path) == (
        "POST",
        "/api/v1/environments/dev/saved-queries",
    )
    assert json.loads(seen[0].content) == {
        "name": "Orders by status",
        "sql": "SELECT 1",
        "visibility": "everyone",
    }
    assert created.name == "Orders by status"
    resource.create("dev", "N", "SELECT 2")
    assert json.loads(seen[1].content)["visibility"] == "private"


def test_get_reads_a_missing_sql_as_none() -> None:
    seen: list[httpx.Request] = []
    without_sql = {k: v for k, v in _ITEM.items() if k != "sql"} | {"actions": ["delete"]}
    resource = _resource([(200, without_sql)], seen)

    saved = resource.get("dev", _ID)

    assert seen[0].url.path == f"/api/v1/environments/dev/saved-queries/{_ID}"
    assert saved.sql is None
    assert saved.actions == ("delete",)


def test_update_puts_every_field() -> None:
    seen: list[httpx.Request] = []
    resource = _resource([(200, _ITEM)], seen)

    resource.update("dev", _ID, name="New", sql="SELECT 2", visibility="everyone")

    assert (seen[0].method, seen[0].url.path) == (
        "PUT",
        f"/api/v1/environments/dev/saved-queries/{_ID}",
    )
    assert json.loads(seen[0].content) == {
        "name": "New",
        "sql": "SELECT 2",
        "visibility": "everyone",
    }


def test_delete_sends_delete() -> None:
    seen: list[httpx.Request] = []
    resource = _resource([(204, None)], seen)

    assert resource.delete("dev", _ID) is None
    assert (seen[0].method, seen[0].url.path) == (
        "DELETE",
        f"/api/v1/environments/dev/saved-queries/{_ID}",
    )
