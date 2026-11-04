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


_IMPACT = "/api/v1/environments/prod/graph/impact"


def test_impact_prints_reasons_and_marks_may_use(tmp_path: Path) -> None:
    api = FakeApi()
    fact = {
        "id": "asset:s/f",
        "kind": "asset",
        "name": "gold.fact_sales",
        "distance": 1,
        "reason": "downstream of gold.dim_store",
        "may_use": True,
        "repository": "sales",
    }
    root = {**_ROOT, "reason": None, "may_use": False}
    api.on("GET", _IMPACT, json={"nodes": [root, fact], "edges": [], "next_cursor": None})
    result = invoke(
        make_state(tmp_path, api), "graph", "impact", "gold.dim_store", "--key", "--env", "prod"
    )
    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if "gold.fact_sales" in line)
    assert "downstream of gold.dim_store" in row and "yes" in row.split()
    start = next(line for line in result.output.splitlines() if "gold.dim_store" in line)
    assert "yes" not in start.split()
    assert "may_use" in result.output and "SQL is not parsed" in result.output
    assert dict(api.requests[0].url.params) == {"node": "gold.dim_store", "key": "true"}


def test_impact_of_a_column_sends_the_column_form(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _IMPACT, json={"nodes": [_ROOT], "edges": [], "next_cursor": None})
    invoke(
        make_state(tmp_path, api),
        "graph",
        "impact",
        "gold.dim_store",
        "--column",
        "store_id",
        "--env",
        "prod",
    )
    assert dict(api.requests[0].url.params) == {"node": "column:gold.dim_store/store_id"}


def test_impact_with_nothing_depending_says_so(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _IMPACT, json={"nodes": [_ROOT], "edges": [], "next_cursor": None})
    result = invoke(make_state(tmp_path, api), "graph", "impact", "gold.dim_store", "--env", "prod")
    assert result.exit_code == 0, result.output
    assert result.output.strip() == (
        "Nothing the projects declare depends on gold.dim_store, as far as you may see."
    )


def test_impact_refuses_column_and_key_together(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "graph",
        "impact",
        "gold.dim_store",
        "--column",
        "a",
        "--key",
        "--env",
        "prod",
    )
    assert result.exit_code == 2
    assert "--column and --key ask different questions; give one of them." in result.output
    assert api.requests == []


_JOIN = "/api/v1/environments/prod/graph/join-path"
_JOIN_STEP = {
    "reference": "reference:1",
    "from_table": "table:1",
    "from_name": "gold.dim_store",
    "to_table": "table:2",
    "to_name": "gold.fact_sales",
    "from_columns": ["store_id"],
    "to_columns": ["store_id"],
    "direction": "one_to_many",
    "fans_out": True,
    "from_model_kind": "dimension",
    "from_grain": "g",
    "to_model_kind": "fact",
    "to_grain": "h",
}


def test_join_path_prints_each_step_and_the_reason_when_there_is_none(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET", _JOIN, json={"paths": [{"steps": [_JOIN_STEP]}], "reason": None, "more_paths": False}
    )
    args = ("graph", "join-path", "gold.dim_store", "gold.fact_sales", "--env", "prod")
    result = invoke(make_state(tmp_path, api), *args)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "Path 1:",
        "  gold.dim_store → gold.fact_sales on store_id = store_id (one to many, fans out: "
        "dimension → fact)",
    ]
    none = FakeApi()
    reason = "No declared path of at most 4 references joins `a` and `b`."
    none.on("GET", _JOIN, json={"paths": [], "reason": reason, "more_paths": False})
    result = invoke(make_state(tmp_path, none), "graph", "join-path", "a", "b", "--env", "prod")
    assert result.output.strip() == reason


def test_join_path_says_when_more_paths_exist(tmp_path: Path) -> None:
    back = {
        **_JOIN_STEP,
        "from_name": "gold.fact_sales",
        "to_name": "gold.dim_store",
        "direction": "many_to_one",
        "fans_out": False,
        "from_model_kind": None,
        "to_model_kind": "dimension",
    }
    api = FakeApi()
    api.on(
        "GET",
        _JOIN,
        json={"paths": [{"steps": [back]}, {"steps": [back]}], "reason": None, "more_paths": True},
    )
    result = invoke(make_state(tmp_path, api), "graph", "join-path", "a", "b", "--env", "prod")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[1] == (
        "  gold.fact_sales → gold.dim_store on store_id = store_id (many to one: ? → dimension)"
    )
    assert lines[2] == "Path 2:"
    assert (
        lines[-1] == "More paths of this length exist; ask a narrower pair of tables to see them."
    )


def test_join_path_json_output_has_the_paths(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET", _JOIN, json={"paths": [{"steps": [_JOIN_STEP]}], "reason": None, "more_paths": False}
    )
    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "graph",
        "join-path",
        "a",
        "b",
        "--env",
        "prod",
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["more_paths"] is False and data["reason"] is None
    assert data["paths"][0]["steps"][0]["from_columns"] == ["store_id"]
