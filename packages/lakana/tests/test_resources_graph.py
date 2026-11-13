"""The SDK's `graph` resource: what it sends and how it reads the answers."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import GraphEdge
from lakana.errors import InvalidResponse, NotFound
from lakana.resources.graph import MetadataGraph

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_NODE: dict[str, Any] = {
    "id": "table:1",
    "kind": "table",
    "name": "gold.dim_store",
    "distance": 0,
    "key": ["store_id"],
    "new_field": 1,
}
_EDGE = {"kind": "builds", "source": "asset:stores/gold.dim_store", "target": "table:1"}


def _graph(answers: list[Any], sent: list[httpx.Request]) -> MetadataGraph:
    """Build the resource over a transport that answers each request with the next body.

    Parameters
    ----------
    answers
        The JSON bodies the fake server sends, one per request, in order.
    sent
        Receives every request.

    Returns
    -------
    MetadataGraph
        The resource.
    """
    bodies = iter(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=next(bodies))

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_CONFIG.url)
    return MetadataGraph(HttpTransport(_CONFIG, http_client=client))


def _graph_status(status: int, body: Any) -> MetadataGraph:  # ruff: ignore[any-type]
    """Build the resource over a transport that answers every request with `status` and `body`.

    Parameters
    ----------
    status
        The HTTP status.
    body
        The JSON body.

    Returns
    -------
    MetadataGraph
        The resource.
    """
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)),
        base_url=_CONFIG.url,
    )
    return MetadataGraph(HttpTransport(_CONFIG, http_client=client))


def test_lineage_reads_every_page_into_one_graph() -> None:
    sent: list[httpx.Request] = []
    first = {"nodes": [_NODE], "edges": [], "next_cursor": "c1"}
    second = {
        "nodes": [{**_NODE, "id": "asset:stores/gold.dim_store", "kind": "asset", "distance": 1}],
        "edges": [_EDGE],
        "next_cursor": None,
    }
    graph = _graph([first, second], sent).lineage(
        "dev", "gold.dim_store", direction="up", depth=2, lang="fr"
    )
    assert [r.url.path for r in sent] == ["/api/v1/environments/dev/graph/lineage"] * 2
    assert dict(sent[0].url.params) == {
        "node": "gold.dim_store",
        "direction": "up",
        "depth": "2",
        "lang": "fr",
    }
    assert dict(sent[1].url.params)["cursor"] == "c1"
    assert [n.id for n in graph.nodes] == ["table:1", "asset:stores/gold.dim_store"]
    assert graph.nodes[0].key == ("store_id",)
    assert graph.edges == (GraphEdge("builds", "asset:stores/gold.dim_store", "table:1"),)


def test_neighbours_repeats_the_kinds_and_sends_the_page_size() -> None:
    sent: list[httpx.Request] = []
    _graph([{"nodes": [_NODE], "edges": [], "next_cursor": None}], sent).neighbours(
        "dev", "gold.dim_store", kinds=["checks", "filters"], page_size=50
    )
    assert sent[0].url.path == "/api/v1/environments/dev/graph/neighbours"
    assert sent[0].url.params.get_list("kinds") == ["checks", "filters"]
    assert sent[0].url.params["limit"] == "50"


def test_lineage_reads_jobs_and_connections_with_their_kind() -> None:
    asset = {"id": "asset:ingest/bronze.raw", "kind": "asset", "name": "bronze.raw", "distance": 1}
    job = {"id": "job:ingest/load", "kind": "job", "name": "ingest/load", "distance": 1}
    pg = {"id": "connection:pg-sales", "kind": "connection", "name": "pg-sales", "distance": 1}
    edges = [
        {"kind": "runs", "source": job["id"], "target": asset["id"]},
        {"kind": "uses", "source": asset["id"], "target": pg["id"]},
    ]
    body = {
        "nodes": [_NODE, asset, {**pg, "connection_kind": "postgresql"}, job],
        "edges": edges,
        "next_cursor": None,
    }
    graph = _graph([body], []).lineage("dev", "gold.dim_store")
    shown = {n.id: (n.kind, getattr(n, "connection_kind", "absent")) for n in graph.nodes}
    assert shown["connection:pg-sales"] == ("connection", "postgresql")
    assert shown["job:ingest/load"] == ("job", None)
    assert {e.kind for e in graph.edges} == {"runs", "uses"}
    older = {"nodes": [pg], "edges": [], "next_cursor": None}
    assert _graph([older], []).neighbours("dev", pg["id"]).nodes[0].connection_kind is None


def test_a_node_without_a_repository_may_be_null() -> None:
    body = {"nodes": [{**_NODE, "repository": None, "key": None}], "edges": [], "next_cursor": None}
    graph = _graph([body], []).neighbours("dev", "t")
    assert graph.nodes[0].repository is None and graph.nodes[0].key is None


@pytest.mark.parametrize(
    "body",
    [
        {"nodes": "x", "edges": [], "next_cursor": None},
        {"nodes": [_NODE], "edges": "x", "next_cursor": None},
        {"nodes": [], "edges": [], "next_cursor": 5},
        {"nodes": [], "edges": []},
        {"nodes": [{"id": "x"}], "edges": [], "next_cursor": None},
        {"nodes": [], "edges": [5], "next_cursor": None},
    ],
)
def test_an_odd_answer_is_invalid(body: dict[str, Any]) -> None:
    with pytest.raises(InvalidResponse, match="the graph"):
        _graph([body], []).lineage("dev", "t")


def test_a_bare_404_says_to_upgrade() -> None:
    with pytest.raises(NotFound) as caught:
        _graph_status(404, {"detail": "Not Found"}).neighbours("dev", "t")
    assert "This server has no metadata graph routes." in str(caught.value)


def test_a_404_with_a_detail_passes_through() -> None:
    with pytest.raises(NotFound) as caught:
        _graph_status(404, {"detail": "There is no node `t` in environment `dev`."}).lineage(
            "dev", "t"
        )
    assert "There is no node `t`" in str(caught.value)
    assert "no metadata graph routes" not in str(caught.value)


def test_a_cursor_the_server_repeats_is_invalid() -> None:
    page = {"nodes": [_NODE], "edges": [], "next_cursor": "c1"}
    with pytest.raises(InvalidResponse, match="same page cursor twice"):
        _graph([page, page, page], []).lineage("dev", "t")


def test_impact_sends_the_key_flag_only_when_set() -> None:
    sent: list[httpx.Request] = []
    body = {
        "nodes": [{**_NODE, "reason": None, "may_use": False}],
        "edges": [],
        "next_cursor": None,
    }
    graph = _graph([body], sent).impact("dev", "gold.dim_store", key=True, lang="de", page_size=9)
    _graph([body], sent).impact("dev", "gold.dim_store")
    assert dict(sent[0].url.params) == {
        "node": "gold.dim_store",
        "key": "true",
        "lang": "de",
        "limit": "9",
    }
    assert dict(sent[1].url.params) == {"node": "gold.dim_store"}
    assert sent[0].url.path == "/api/v1/environments/dev/graph/impact"
    assert (graph.nodes[0].reason, graph.nodes[0].may_use) == (None, False)


_STEP: dict[str, Any] = {
    "reference": "reference:1",
    "from_table": "table:1",
    "from_name": "gold.f",
    "to_table": "table:2",
    "to_name": "gold.d",
    "from_columns": ["k"],
    "to_columns": ["k"],
    "direction": "many_to_one",
    "fans_out": False,
    "from_model_kind": "fact",
    "from_grain": "g",
    "to_model_kind": "dimension",
    "to_grain": "h",
    "new": 1,
}


def test_join_path_reads_the_paths() -> None:
    sent: list[httpx.Request] = []
    body = {"paths": [{"steps": [_STEP]}], "reason": None, "more_paths": False}
    answer = _graph([body], sent).join_path("dev", "gold.f", "gold.d")
    _graph([body], sent).join_path("dev", "gold.f", "gold.d", lang="de")
    assert sent[0].url.path == "/api/v1/environments/dev/graph/join-path"
    assert dict(sent[0].url.params) == {"from": "gold.f", "to": "gold.d"}
    assert dict(sent[1].url.params)["lang"] == "de"
    step = answer.paths[0].steps[0]
    assert (step.from_columns, step.to_columns, step.fans_out) == (("k",), ("k",), False)
    assert (answer.reason, answer.more_paths) == (None, False)


def test_join_path_without_a_path_reads_the_reason() -> None:
    body = {"paths": [], "reason": "No declared path.", "more_paths": False}
    answer = _graph([body], []).join_path("dev", "a", "b")
    assert (answer.paths, answer.reason) == ((), "No declared path.")


@pytest.mark.parametrize(
    "body",
    [
        {"paths": "x", "reason": None, "more_paths": False},
        {"paths": [{"steps": [["x"]]}], "reason": None, "more_paths": False},
        {"paths": [{"steps": [{"reference": "r"}]}], "reason": None, "more_paths": False},
        {"paths": [{}], "reason": None, "more_paths": False},
        {"paths": []},
        [],
    ],
)
def test_an_odd_join_path_answer_is_invalid(body: Any) -> None:  # ruff: ignore[any-type]
    with pytest.raises(InvalidResponse, match="the join paths"):
        _graph([body], []).join_path("dev", "a", "b")


def test_join_path_on_a_bare_404_says_to_upgrade_and_passes_a_detail_through() -> None:
    with pytest.raises(NotFound, match=r"This server has no metadata graph routes\."):
        _graph_status(404, {"detail": "Not Found"}).join_path("dev", "a", "b")
    with pytest.raises(NotFound) as caught:
        _graph_status(404, {"detail": "There is no node `a`."}).join_path("dev", "a", "b")
    assert "no metadata graph routes" not in str(caught.value)
