"""`lakana term *`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import FakeApi, invoke, make_state

_BASE = "/api/v1/environments/prod/terms"
_TERM: dict[str, Any] = {
    "key": "net_revenue",
    "id": "5f1c0000-0000-0000-0000-000000000001",
    "name": "Net revenue",
    "definition": "Revenue after returns.",
    "synonyms": ["revenue", "sales"],
    "owner": "finance",
    "repository": "shop",
    "language": "en",
    "links": [
        {"table": "silver.gone", "column": None, "kind": "table", "missing": True},
        {"table": "silver.orders", "column": "order_id", "kind": "column", "missing": False},
    ],
    "related": [{"key": "gross_revenue", "name": "Gross revenue", "missing": False}],
    "related_by": ["gross_revenue"],
}


def test_term_list_prints_a_table_and_the_empty_case(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json={"items": [_TERM], "next_cursor": None})
    result = invoke(make_state(tmp_path, api), "term", "list", "--env", "prod")
    assert result.exit_code == 0, result.output
    assert "net_revenue" in result.output and "Net revenue" in result.output
    assert "finance" in result.output
    empty = FakeApi()
    empty.on("GET", _BASE, json={"items": [], "next_cursor": None})
    result = invoke(make_state(tmp_path, empty), "term", "list", "--env", "prod")
    assert result.output.strip() == (
        "No terms in prod. A repository defines them in glossary/*.yaml: "
        "https://lakana-labs.github.io/lakana/reference/glossary/"
    )


def test_term_list_with_output_json_prints_the_terms(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json={"items": [], "next_cursor": None})
    result = invoke(make_state(tmp_path, api), "--output", "json", "term", "list", "--env", "prod")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_term_get_prints_the_term(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/net_revenue", json=_TERM)
    result = invoke(make_state(tmp_path, api), "term", "get", "net_revenue", "--env", "prod")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "Term:        net_revenue — Net revenue",
        "Definition:  Revenue after returns.",
        "Synonyms:    revenue, sales",
        "Owner:       finance",
        "Repository:  shop",
        "Language:    en",
        "Links:",
        "  silver.gone (missing)",
        "  silver.orders.order_id (column)",
        "Related:     gross_revenue — Gross revenue",
        "Named by:    gross_revenue",
    ]


def test_term_get_shows_what_is_absent(tmp_path: Path) -> None:
    bare = {
        **_TERM,
        "synonyms": [],
        "links": [],
        "related": [{"key": "ghost", "name": None, "missing": True}],
        "related_by": [],
    }
    api = FakeApi()
    api.on("GET", f"{_BASE}/net_revenue", json=bare)
    result = invoke(make_state(tmp_path, api), "term", "get", "net_revenue", "--env", "prod")
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "Synonyms:    (none)" in lines and "Links:       (none you may read)" in lines
    assert "Related:     ghost (missing)" in lines and "Named by:    (none)" in lines


def test_term_get_with_output_json_prints_the_answer(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/net_revenue", json=_TERM)
    result = invoke(
        make_state(tmp_path, api), "--output", "json", "term", "get", "net_revenue", "--env", "prod"
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["key"] == "net_revenue"


def test_term_list_names_the_filters_that_left_nothing(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json={"items": [], "next_cursor": None})
    state = make_state(tmp_path, api)

    owner = invoke(state, "term", "list", "--owner", "typo", "--env", "prod")
    both = invoke(state, "term", "list", "--owner", "o", "--repository", "r\x1b", "--env", "prod")

    assert owner.output.strip() == (
        "No terms in prod with owner 'typo'. Drop --owner to see every term."
    )
    assert both.output.strip() == (
        "No terms in prod with owner 'o' and repository 'r\\x1b'. "
        "Drop --owner and --repository to see every term."
    )


def test_term_get_keeps_a_multi_line_definition_under_its_label(tmp_path: Path) -> None:
    term = {
        **_TERM,
        "definition": "Revenue after returns.\nOwner:       security-team\r\nLinks:  (none)",
    }
    api = FakeApi()
    api.on("GET", f"{_BASE}/net_revenue", json=term)
    result = invoke(make_state(tmp_path, api), "term", "get", "net_revenue", "--env", "prod")
    assert result.output.splitlines()[1:4] == [
        "Definition:  Revenue after returns.",
        "             Owner:       security-team",
        "             Links:  (none)",
    ]


def test_term_get_keeps_every_other_value_on_its_line(tmp_path: Path) -> None:
    forged = "\nOwner:       forged"
    term = {
        **_TERM,
        "key": "k" + forged,
        "name": "Net" + forged,
        "synonyms": ["a" + forged],
        "owner": "o" + forged,
        "repository": "r" + forged,
        "language": "en" + forged,
        "related": [{"key": "g" + forged, "name": "G" + forged, "missing": False}],
        "related_by": ["b" + forged],
    }
    api = FakeApi()
    api.on("GET", f"{_BASE}/net_revenue", json=term)
    result = invoke(make_state(tmp_path, api), "term", "get", "net_revenue", "--env", "prod")
    lines = result.output.splitlines()
    assert len(lines) == 11, lines
    assert not [line for line in lines if line.startswith("Owner:       forged")]
    assert "Owner:       o\\nOwner:       forged" in lines
