"""The SDK's `client.catalog`: the list of every readable table, its pages and its tree."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import (
    DefinedBy,
    HealthSummary,
    TableListItem,
    TableMatch,
    TableOwner,
    TableTree,
    TreeEnvironment,
    TreeNamespace,
)
from lakana.resources.artifacts import artifact_from_json, version_from_json
from lakana.resources.catalog import Catalog
from lakana.resources.deploys import OrphanTables
from lakana.resources.tables import Tables, _dropped_from_json

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


def test_tree_reads_actions_and_defaults_them_for_an_older_server() -> None:
    new = {
        "count": 0,
        "environments": [
            {
                "name": "dev",
                "count": 0,
                "actions": ["create_namespace"],
                "namespaces": [{"path": "gold", "count": 0, "actions": ["drop"]}],
            }
        ],
    }
    old = {
        "count": 0,
        "environments": [{"name": "dev", "count": 0, "namespaces": [{"path": "gold", "count": 0}]}],
    }
    tree = Catalog(_transport([new], [])).tree()
    assert tree.environments[0].actions == ("create_namespace",)
    assert tree.environments[0].namespaces[0].actions == ("drop",)
    older = Catalog(_transport([old], [])).tree()
    assert older.environments[0].actions == ()
    assert older.environments[0].namespaces[0].actions == ()


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


_FILE = {"repository": "analytics", "path": "assets/gold/t.sql"}


def test_a_table_without_the_new_fields_still_reads() -> None:
    sent: list[httpx.Request] = []
    base = {"name": "t", "namespace": "gold", "uuid": "u", "metadata_location": None, "schema": []}
    new = {**base, "defined_by": _FILE, "actions": ["drop", "rebuild"]}
    tables = Tables(_transport([new, base], sent))

    table = tables.get("dev", "gold.t")
    assert table.defined_by == DefinedBy("analytics", "assets/gold/t.sql")
    assert table.actions == ("drop", "rebuild")
    old = tables.get("dev", "gold.t")
    assert (old.defined_by, old.actions) == (None, ())


def test_a_list_item_reads_defined_by_and_actions_and_tolerates_their_absence() -> None:
    sent: list[httpx.Request] = []
    body = {
        "items": [_item("a", defined_by=_FILE, actions=["drop"]), _item("b")],
        "next_cursor": None,
        "capped": False,
    }
    first, second = Catalog(_transport([body], sent)).list().items
    assert (first.defined_by, first.actions) == (
        DefinedBy("analytics", "assets/gold/t.sql"),
        ("drop",),
    )
    assert (second.defined_by, second.actions) == (None, ())


def test_the_other_rows_read_their_actions_and_tolerate_their_absence() -> None:
    dropped = {
        "name": "t",
        "namespace": "gold",
        "uuid": "u",
        "kind": "TABLE",
        "dropped_at": "2026-11-01T12:00:00+00:00",
        "purge_after": "2026-11-08T12:00:00+00:00",
        "restorable": True,
    }
    assert _dropped_from_json({**dropped, "actions": ["undrop"]}).actions == ("undrop",)
    assert _dropped_from_json(dropped).actions == ()
    artifact = {
        "name": "churn",
        "namespace": "models",
        "uuid": "u",
        "kind": "ARTIFACT",
        "current_version": 1,
        "versions": 1,
        "size_bytes": 3,
        "created_at": "2026-11-01T12:00:00+00:00",
    }
    assert artifact_from_json({**artifact, "actions": ["drop"]}).actions == ("drop",)
    assert artifact_from_json(artifact).actions == ()
    version = {
        "version": 1,
        "manifest_sha256": "0" * 64,
        "size_bytes": 3,
        "file_count": 1,
        "is_directory": False,
        "created_at": "2026-11-01T12:00:00+00:00",
    }
    assert version_from_json({**version, "actions": ["delete"]}).actions == ("delete",)
    assert version_from_json(version).actions == ()
    sent: list[httpx.Request] = []
    orphan = {"repository": "analytics", "table": "silver.old"}
    body = {"orphans": [{**orphan, "actions": ["drop"]}, orphan], "hidden": 0}
    report = OrphanTables(_transport([body], sent)).list("dev")
    assert [o.actions for o in report.orphans] == [("drop",), ()]
