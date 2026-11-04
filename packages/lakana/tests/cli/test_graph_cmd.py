"""`lakana graph *`."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_LINEAGE = "/api/v1/environments/prod/graph/lineage"
_NEAR = "/api/v1/environments/prod/graph/neighbours"
_ROOT = {
    "id": "table:1",
    "kind": "table",
    "name": "gold.dim_store",
    "distance": 0,
    "repository": "stores",
}


def test_lineage_prints_the_nodes_and_sends_the_column_form(tmp_path: Path) -> None:
    api = FakeApi()
    other = {**_ROOT, "id": "asset:s/x", "kind": "asset", "name": "gold.fact_sales", "distance": 1}
    api.on("GET", _LINEAGE, json={"nodes": [_ROOT, other], "edges": [], "next_cursor": None})
    result = invoke(
        make_state(tmp_path, api),
        "graph",
        "lineage",
        "gold.dim_store",
        "--column",
        "store_id",
        "--direction",
        "down",
        "--depth",
        "2",
        "--env",
        "prod",
    )
    assert result.exit_code == 0, result.output
    assert "gold.fact_sales" in result.output and "asset" in result.output
    assert dict(api.requests[0].url.params) == {
        "node": "column:gold.dim_store/store_id",
        "direction": "down",
        "depth": "2",
    }


def test_a_table_id_with_a_column_keeps_the_id(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _LINEAGE, json={"nodes": [_ROOT], "edges": [], "next_cursor": None})
    invoke(
        make_state(tmp_path, api), "graph", "lineage", "table:1", "--column", "c", "--env", "prod"
    )
    assert api.requests[0].url.params["node"] == "column:1/c"


def test_a_node_without_a_repository_prints(tmp_path: Path) -> None:
    api = FakeApi()
    bare = {
        "id": "term:1",
        "kind": "term",
        "name": "net_revenue",
        "distance": 1,
        "repository": None,
    }
    api.on("GET", _LINEAGE, json={"nodes": [_ROOT, bare], "edges": [], "next_cursor": None})
    result = invoke(
        make_state(tmp_path, api), "graph", "lineage", "gold.dim_store", "--env", "prod"
    )
    assert result.exit_code == 0, result.output
    assert "net_revenue" in result.output


def test_neighbours_alone_says_so_and_names_the_kind_filter(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _NEAR, json={"nodes": [_ROOT], "edges": [], "next_cursor": None})
    result = invoke(
        make_state(tmp_path, api),
        "graph",
        "neighbours",
        "gold.dim_store",
        "--kind",
        "reads",
        "--env",
        "prod",
    )
    assert result.output.strip() == (
        "No neighbours of gold.dim_store that you may see with --kind reads; "
        "drop --kind to see every kind."
    )
    plain = invoke(
        make_state(tmp_path, api), "graph", "neighbours", "gold.dim_store", "--env", "prod"
    )
    assert plain.output.strip() == "No neighbours of gold.dim_store that you may see."


def test_neighbours_prints_what_is_next_to_the_node(tmp_path: Path) -> None:
    api = FakeApi()
    other = {**_ROOT, "id": "asset:s/x", "kind": "asset", "name": "gold.fact_sales", "distance": 1}
    api.on("GET", _NEAR, json={"nodes": [_ROOT, other], "edges": [], "next_cursor": None})
    result = invoke(
        make_state(tmp_path, api), "graph", "neighbours", "gold.dim_store", "--env", "prod"
    )
    assert "gold.fact_sales" in result.output


def test_an_empty_answer_names_the_node_as_typed(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _NEAR, json={"nodes": [], "edges": [], "next_cursor": None})
    result = invoke(make_state(tmp_path, api), "graph", "neighbours", "gold.x", "--env", "prod")
    assert result.output.strip() == "No neighbours of gold.x that you may see."


def test_json_output_has_the_nodes_and_the_edges(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _NEAR, json={"nodes": [_ROOT], "edges": [], "next_cursor": None})
    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "graph",
        "neighbours",
        "table:1",
        "--env",
        "prod",
    )
    assert set(json.loads(result.output)) == {"nodes", "edges"}


def test_lineage_leaves_the_depth_to_the_server(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _LINEAGE, json={"nodes": [_ROOT], "edges": [], "next_cursor": None})
    invoke(make_state(tmp_path, api), "graph", "lineage", "gold.dim_store", "--env", "prod")
    assert "depth" not in api.requests[0].url.params
