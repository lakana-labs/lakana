"""The SDK's `client.catalog`: the list of every readable table, its pages and its tree."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import (
    HealthSummary,
    TableListItem,
    TableMatch,
    TableOwner,
    TableTree,
    TreeEnvironment,
    TreeNamespace,
)
from lakana.resources.catalog import Catalog
from lakana.resources.tables import Tables

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")


def _item(name: str, **over: object) -> dict[str, Any]:
    """Build one table of a list answer.

    Parameters
    ----------
    name
        The table's name.
    **over
        Fields to replace.

    Returns
    -------
    dict
        The `TableListItemOut` JSON body.
    """
    return {
        "environment": "dev",
        "namespace": "gold",
        "name": name,
        "uuid": "00000000-0000-0000-0000-0000000000aa",
        "kind": "TABLE",
        "model_kind": "fact",
        "state": "published",
        "owner": {"name": "alice", "kind": "USER"},
        "created_at": "2026-11-01T12:00:00+00:00",
        "row_count": 7,
        "size_bytes": 99,
        "last_write_at": "2026-11-02T12:00:00+00:00",
        "last_read_at": None,
        "readers_7d": 2,
        "health_summary": {"state": "warning", "item": "late", "text": "Late", "others": 0},
        "match": None,
        "a_field_of_a_newer_server": 1,
        **over,
    }


def _transport(bodies: list[dict[str, Any]], sent: list[httpx.Request]) -> HttpTransport:
    """Answer successive requests with successive bodies; the last repeats.

    Parameters
    ----------
    bodies
        The JSON body of each answer.
    sent
        Where to record the requests.

    Returns
    -------
    HttpTransport
        The transport.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = bodies[min(len(sent), len(bodies) - 1)]
        sent.append(request)
        return httpx.Response(200, json=body)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return HttpTransport(_CONFIG, http_client=client)


def test_list_sends_only_the_given_filters_and_reads_the_page() -> None:
    sent: list[httpx.Request] = []
    page_body = {
        "items": [
            _item(
                "a",
                match={
                    "kind": "column",
                    "matched": "name",
                    "column": "order_id",
                    "column_type": "long",
                    "snippet": None,
                    "language": None,
                    "translated": False,
                },
            )
        ],
        "next_cursor": "abc",
        "capped": False,
    }
    catalog = Catalog(_transport([page_body], sent))
    page = catalog.list(environment="dev", q="order", sort="rows", limit=10)
    assert dict(sent[-1].url.params) == {
        "environment": "dev",
        "q": "order",
        "sort": "rows",
        "limit": "10",
    }
    item = page.items[0]
    assert isinstance(item, TableListItem)
    assert item.health_summary == HealthSummary("warning", "late", "Late", 0)
    assert item.owner == TableOwner("alice", "USER")
    assert item.match == TableMatch("column", "name", "order_id", "long", None, None, False)
    assert item.created_at == datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert item.last_read_at is None and item.readers_7d == 2
    assert (page.next_cursor, page.capped) == ("abc", False)


def test_list_sends_every_filter_and_always_sends_the_sort() -> None:
    sent: list[httpx.Request] = []
    catalog = Catalog(_transport([{"items": [], "next_cursor": None, "capped": True}], sent))
    page = catalog.list(
        environment="dev",
        namespace="gold",
        q="x",
        model_kind="fact",
        order="asc",
        cursor="c",
        lang="de",
    )
    assert dict(sent[-1].url.params) == {
        "environment": "dev",
        "namespace": "gold",
        "q": "x",
        "model_kind": "fact",
        "sort": "name",
        "order": "asc",
        "cursor": "c",
        "lang": "de",
    }
    assert page.capped is True and page.items == []


def test_a_table_with_no_owner_reads_as_none() -> None:
    sent: list[httpx.Request] = []
    body = {"items": [_item("a", owner=None)], "next_cursor": None, "capped": False}
    assert Catalog(_transport([body], sent)).list().items[0].owner is None


def test_iter_follows_next_cursor_until_the_last_page() -> None:
    sent: list[httpx.Request] = []
    pages = [
        {"items": [_item("a")], "next_cursor": "c1", "capped": False},
        {"items": [_item("b")], "next_cursor": None, "capped": False},
    ]
    catalog = Catalog(_transport(pages, sent))
    assert [i.name for i in catalog.iter(environment="dev")] == ["a", "b"]
    assert [r.url.params.get("cursor") for r in sent] == [None, "c1"]


def test_tree_reads_environments_and_namespaces() -> None:
    sent: list[httpx.Request] = []
    body = {
        "count": 2,
        "environments": [{"name": "dev", "count": 2, "namespaces": [{"path": "gold", "count": 2}]}],
    }
    assert Catalog(_transport([body], sent)).tree() == TableTree(
        2, [TreeEnvironment("dev", 2, [TreeNamespace("gold", 2)])]
    )
    assert sent[-1].url.path == "/api/v1/tables/tree"


def test_get_table_reads_owner_and_health_summary_and_tolerates_their_absence() -> None:
    sent: list[httpx.Request] = []
    base = {
        "name": "t",
        "namespace": "gold",
        "uuid": "u",
        "metadata_location": None,
        "schema": [],
    }
    new = {
        **base,
        "owner": {"name": "alice", "kind": "USER"},
        "created_at": "2026-11-01T12:00:00+00:00",
        "last_read_at": "2026-11-03T12:00:00+00:00",
        "readers_7d": 3,
        "health_summary": {"state": "healthy", "item": "healthy", "text": "Healthy", "others": 0},
    }
    tables = Tables(_transport([new, {**base, "name": "old"}], sent))
    table = tables.get("dev", "gold.t")
    assert table.owner == TableOwner("alice", "USER")
    assert table.health_summary == HealthSummary("healthy", "healthy", "Healthy", 0)
    assert table.created_at == datetime(2026, 11, 1, 12, tzinfo=UTC)
    assert table.last_read_at == datetime(2026, 11, 3, 12, tzinfo=UTC) and table.readers_7d == 3
    old = tables.get("dev", "gold.old")
    assert old.health_summary is None and old.owner is None
    assert (old.created_at, old.last_read_at, old.readers_7d) == (None, None, 0)
