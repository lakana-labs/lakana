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
