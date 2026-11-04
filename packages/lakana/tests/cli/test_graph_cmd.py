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


def _row(output: str, *words: str) -> str:
    """Find the one line of a table that holds every word.

    Parameters
    ----------
    output
        The command's output.
    *words
        What the line holds.

    Returns
    -------
    str
        The line.
    """
    lines = [line for line in output.splitlines() if all(w in line.split() for w in words)]
    assert len(lines) == 1, (words, output)
    return lines[0]


def test_lineage_says_which_side_of_the_start_each_node_is_on(tmp_path: Path) -> None:
    def node(node_id: str, kind: str, name: str, distance: int) -> dict[str, object]:
        return {"id": node_id, "kind": kind, "name": name, "distance": distance}

    nodes = [
        node("table:s", "table", "silver.sales", 0),
        node("asset:b/silver.sales", "asset", "silver.sales", 1),
        node("table:b", "table", "bronze.sales_raw", 1),
        node("asset:f/gold.fact", "asset", "gold.fact", 1),
        node("table:f", "table", "gold.fact", 1),
        node("reference:r", "reference", "silver.sales.id_→_gold.dim.id", 1),
    ]
    edges = [
        {"kind": "builds", "source": "asset:b/silver.sales", "target": "table:s"},
        {"kind": "reads", "source": "asset:b/silver.sales", "target": "table:b"},
        {"kind": "reads", "source": "asset:f/gold.fact", "target": "table:s"},
        {"kind": "builds", "source": "asset:f/gold.fact", "target": "table:f"},
        {"kind": "references", "source": "reference:r", "target": "table:s"},
    ]
    api = FakeApi()
    api.on("GET", _LINEAGE, json={"nodes": nodes, "edges": edges, "next_cursor": None})
    result = invoke(make_state(tmp_path, api), "graph", "lineage", "silver.sales", "--env", "prod")
    assert result.exit_code == 0, result.output
    assert "side" in result.output.splitlines()[1]
    assert "start" in _row(result.output, "table", "silver.sales").split()
    assert "up" in _row(result.output, "asset", "silver.sales").split()
    assert "up" in _row(result.output, "bronze.sales_raw").split()
    assert "down" in _row(result.output, "asset", "gold.fact").split()
    assert "down" in _row(result.output, "table", "gold.fact").split()
    assert "join" in _row(result.output, "reference").split()


def test_a_node_upstream_and_downstream_at_once_says_both(tmp_path: Path) -> None:
    nodes = [
        {"id": "table:s", "kind": "table", "name": "s.loop", "distance": 0},
        {"id": "asset:x/s.loop", "kind": "asset", "name": "s.loop", "distance": 1},
    ]
    edges = [
        {"kind": "builds", "source": "asset:x/s.loop", "target": "table:s"},
        {"kind": "reads", "source": "asset:x/s.loop", "target": "table:s"},
    ]
    api = FakeApi()
    api.on("GET", _LINEAGE, json={"nodes": nodes, "edges": edges, "next_cursor": None})
    result = invoke(make_state(tmp_path, api), "graph", "lineage", "s.loop", "--env", "prod")
    assert "both" in _row(result.output, "asset", "s.loop").split()


def test_lang_shows_the_labels_in_table_mode(tmp_path: Path) -> None:
    api = FakeApi()
    labelled = {**_ROOT, "label": "Magasins"}
    other = {**_ROOT, "id": "asset:s/x", "kind": "asset", "name": "gold.fact_sales", "distance": 1}
    api.on("GET", _NEAR, json={"nodes": [labelled, other], "edges": [], "next_cursor": None})
    result = invoke(
        make_state(tmp_path, api),
        "graph",
        "neighbours",
        "gold.dim_store",
        "--lang",
        "fr",
        "--env",
        "prod",
    )
    assert result.exit_code == 0, result.output
    assert "label" in result.output and "Magasins" in result.output
    assert api.requests[0].url.params["lang"] == "fr"
