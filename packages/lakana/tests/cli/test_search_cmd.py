"""Tests for `lakana search`."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from conftest import FakeApi, invoke, make_state
from typer.testing import Result

_ROUTE = "/api/v1/environments/dev/search"


def _hit(kind: str, name: str, matched: str = "name", **fields: Any) -> dict[str, Any]:  # ruff: ignore[any-type]
    """Build one hit as the server sends it.

    Parameters
    ----------
    kind
        The kind of hit.
    name
        Its display name.
    matched
        The field that matched.
    **fields
        More fields.

    Returns
    -------
    dict
        The JSON object.
    """
    empty = dict.fromkeys(
        (
            "snippet",
            "namespace",
            "table",
            "table_kind",
            "column",
            "column_type",
            "run_id",
            "job",
            "repository",
            "status",
            "principal",
            "principal_type",
        )
    )
    return {"kind": kind, "name": name, "matched": matched, **empty, **fields}


def _plain(text: str) -> str:
    """Return CLI output without ANSI codes or Rich box borders, whitespace collapsed.

    Parameters
    ----------
    text
        Raw stdout or stderr.

    Returns
    -------
    str
        The plain, single-spaced text.
    """
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for border in "│╭╮╰╯─┃┏┓┗┛━┡┩╇":
        plain = plain.replace(border, " ")
    return re.sub(r"\s+", " ", plain)


def _search(tmp_path: Path, body: dict[str, Any], *args: str) -> tuple[FakeApi, Result]:
    """Run `lakana search` against a canned answer.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    body
        The route's JSON answer.
    *args
        Arguments for the command.

    Returns
    -------
    tuple
        The fake API and the result.
    """
    api = FakeApi()
    api.on("GET", _ROUTE, json=body)
    return api, invoke(make_state(tmp_path, api), "search", *args, "--env", "dev")


def _page(*results: dict[str, Any], has_more: bool = False) -> dict[str, Any]:
    """Wrap hits in a page.

    Parameters
    ----------
    *results
        The hits.
    has_more
        Whether the server says more follow.

    Returns
    -------
    dict
        The page.
    """
    return {"results": list(results), "limit": 20, "offset": 0, "has_more": has_more}


def test_search_lists_each_kind_with_what_matched(tmp_path: Path) -> None:
    body = _page(
        _hit("table", "silver.orders", "description", snippet="Purchases by customers"),
        _hit("column", "silver.orders.status", column_type="string"),
        _hit("run", "r-1", "job", run_id="r-1", job="nightly", repository="analytics"),
        _hit("principal", "etl-bot", principal="etl-bot"),
    )

    api, result = _search(tmp_path, body, "order")

    assert result.exit_code == 0, result.stderr
    request = api.requests[-1]
    assert request.url.params["q"] == "order"
    assert request.url.params["limit"] == "20"
    assert "kind" not in request.url.params
    plain = _plain(result.stdout)
    for text in (
        "silver.orders",
        "description",
        "Purchases by customers",
        "silver.orders.status",
        "nightly",
        "etl-bot",
    ):
        assert text in plain


def test_search_sends_kind_and_limit(tmp_path: Path) -> None:
    api, result = _search(tmp_path, _page(), "x", "--kind", "column", "--limit", "5")

    assert result.exit_code == 0, result.stderr
    assert dict(api.requests[-1].url.params) == {
        "q": "x",
        "kind": "column",
        "limit": "5",
        "offset": "0",
    }


def test_search_with_no_hits_says_so_and_how_to_widen_it(tmp_path: Path) -> None:
    _, result = _search(tmp_path, _page(), "zzz")

    assert result.exit_code == 0, result.stderr
    plain = _plain(result.stdout)
    assert "No match for 'zzz'" in plain
    assert "table names, column names" in plain


def test_search_names_the_next_page_exactly_when_more_hits_follow(tmp_path: Path) -> None:
    _, result = _search(
        tmp_path,
        _page(_hit("table", "silver.orders"), has_more=True),
        "my text",
        "--kind",
        "column",
        "--limit",
        "5",
        "--offset",
        "10",
    )

    assert (
        "More results: lakana search 'my text' --kind column --limit 5 --offset 15 --env dev"
        in (_plain(result.stdout))
    )


def test_search_hint_for_text_that_starts_with_a_dash_runs_and_asks_the_same_question(
    tmp_path: Path,
) -> None:
    body = _page(_hit("table", "silver.orders"), has_more=True)
    api = FakeApi()
    api.on("GET", _ROUTE, json=body)
    first = invoke(make_state(tmp_path, api), "search", "--env", "dev", "--", "-x")
    assert first.exit_code == 0, first.stderr
    argv = shlex.split(_plain(first.stdout).split("More results: ")[1].strip())
    assert argv[0] == "lakana" and "--" in argv

    api = FakeApi()
    api.on("GET", _ROUTE, json=_page())
    again = invoke(make_state(tmp_path, api), *argv[1:])

    assert again.exit_code == 0, again.stderr
    assert api.requests[-1].url.params["q"] == "-x"
    assert api.requests[-1].url.params["offset"] == "20"


def test_search_keeps_the_default_limit_out_of_the_hint(tmp_path: Path) -> None:
    _, result = _search(tmp_path, _page(_hit("table", "silver.orders"), has_more=True), "o")

    assert "More results: lakana search o --offset 20 --env dev" in _plain(result.stdout)


def test_search_gives_no_hint_when_nothing_follows(tmp_path: Path) -> None:
    _, result = _search(tmp_path, _page(_hit("table", "silver.orders")), "o")

    assert "More results" not in _plain(result.stdout)


def test_search_passes_the_offset_on_and_refuses_a_negative_one(tmp_path: Path) -> None:
    api, result = _search(tmp_path, _page(), "x", "--offset", "40")
    assert api.requests[-1].url.params["offset"] == "40"

    refused = invoke(make_state(tmp_path, FakeApi()), "search", "x", "--offset", "-1")
    assert result.exit_code == 0 and refused.exit_code == 2


def test_search_refuses_a_limit_outside_the_range_and_an_unknown_kind(tmp_path: Path) -> None:
    api = FakeApi()
    state = make_state(tmp_path, api)

    for args in (("x", "--limit", "0"), ("x", "--limit", "101"), ("x", "--kind", "nope")):
        result = invoke(state, "search", *args, "--env", "dev")
        assert result.exit_code == 2, args
    assert api.requests == []


def test_search_as_json_gives_the_whole_page(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _ROUTE, json=_page(_hit("table", "silver.orders"), has_more=True))

    result = invoke(
        make_state(tmp_path, api), "--output", "json", "search", "order", "--env", "dev"
    )

    body = json.loads(result.stdout)
    assert body["has_more"] is True
    assert [(h["kind"], h["name"]) for h in body["results"]] == [("table", "silver.orders")]


def test_search_shows_a_tables_kind_after_its_name_and_sends_model_kind(tmp_path: Path) -> None:
    body = _page(
        _hit("table", "gold.dim_store", model_kind="dimension", table_kind="TABLE"),
        _hit("table", "gold.plain", table_kind="TABLE"),
    )

    api, result = _search(tmp_path, body, "store", "--model-kind", "dimension")

    assert result.exit_code == 0, result.stderr
    assert api.requests[-1].url.params["model_kind"] == "dimension"
    plain = _plain(result.stdout)
    assert "gold.dim_store [dimension]" in plain
    assert "gold.plain [" not in plain


def test_search_without_model_kind_sends_none_and_the_hint_keeps_it(tmp_path: Path) -> None:
    api, _ = _search(tmp_path, _page(), "x")
    assert "model_kind" not in api.requests[-1].url.params
    _, result = _search(
        tmp_path, _page(_hit("table", "a.b"), has_more=True), "x", "--model-kind", "fact"
    )
    assert "--model-kind fact" in _plain(result.stdout)


def test_search_refuses_an_unknown_model_kind(tmp_path: Path) -> None:
    api = FakeApi()
    refused = invoke(make_state(tmp_path, api), "search", "x", "--model-kind", "cube")
    assert refused.exit_code != 0
    assert api.requests == []


def test_search_sends_lang_and_shows_which_language_matched(tmp_path: Path) -> None:
    body = _page(
        _hit("table", "gold.sales", "label", snippet="Ventes", language="fr", translated=True),
        _hit("table", "gold.orders"),
        has_more=True,
    )

    api, result = _search(tmp_path, body, "ventes", "--lang", "fr")

    assert result.exit_code == 0, result.stderr
    assert api.requests[-1].url.params["lang"] == "fr"
    plain = _plain(result.stdout)
    assert "label [fr]" in plain
    assert "name [" not in plain
    assert "--lang fr" in plain.split("More results:")[1]
    api, _ = _search(tmp_path, _page(), "x")
    assert "lang" not in api.requests[-1].url.params


def test_search_shows_no_language_for_a_text_in_the_tables_main_language(tmp_path: Path) -> None:
    body = _page(
        _hit(
            "table",
            "gold.fact_sales",
            "grain",
            snippet="One row per store, product and calendar day.",
            language="en",
            translated=False,
        )
    )

    _, result = _search(tmp_path, body, "calendar")

    plain = _plain(result.stdout)
    assert "grain [" not in plain and " grain " in plain
    assert "One row per store, product and calendar day." in plain


def test_search_shows_the_language_of_a_translated_text(tmp_path: Path) -> None:
    body = _page(
        _hit("column", "gold.sales.store_id", "label", language="fr", translated=True),
        _hit("table", "gold.sales", "label", language="en"),
    )

    _, result = _search(tmp_path, body, "magasin", "--lang", "fr")

    plain = _plain(result.stdout)
    assert plain.count("label [fr]") == 1
    assert "label [en]" not in plain
