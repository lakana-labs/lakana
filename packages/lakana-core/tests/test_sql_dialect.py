"""Tests for Lakana's reading of DuckDB SQL (`lakana_core.sql_dialect`)."""

from __future__ import annotations

import pytest
import sqlglot.expressions as exp

from lakana_core import sql_dialect
from lakana_core.sql_dialect import KEYWORD_LAMBDA, LakanaDuckDB


def _lambdas(sql: str) -> list[bool]:
    """Say, for each lambda of `sql` in document order, whether it is marked as keyword.

    Parameters
    ----------
    sql
        A statement with lambdas.

    Returns
    -------
    list of bool
        Whether each `exp.Lambda` carries `KEYWORD_LAMBDA`.
    """
    parsed = sql_dialect.parse_one(sql)
    return [bool(node.meta.get(KEYWORD_LAMBDA)) for node in parsed.find_all(exp.Lambda, bfs=False)]


@pytest.mark.parametrize(
    ("sql", "marked"),
    [
        ("SELECT upper(lambda j: 'a')", [True]),
        ("SELECT upper(j -> 'a')", [False]),
        ("SELECT upper(\"lambda\" -> 'a')", [False]),
        ("SELECT list_reduce(l, lambda x, y: x + y)", [True]),
        ("SELECT list_transform(l, lambda x: list_transform(x, y -> y))", [True, False]),
        ("SELECT list_transform(l, x -> list_transform(x, lambda y: y))", [False, True]),
    ],
)
def test_a_lambda_written_with_the_keyword_is_marked(sql: str, marked: list[bool]) -> None:
    """Sqlglot no longer records the spelling; the Lakana parser does, for each lambda.

    Parameters
    ----------
    sql
        A statement with lambdas.
    marked
        For each lambda in document order, whether it was written with `LAMBDA`.
    """
    assert _lambdas(sql) == marked


def test_a_column_named_lambda_is_not_a_lambda() -> None:
    """`lambda` as a plain name parses as sqlglot reads it, with nothing marked."""
    assert _lambdas('SELECT "lambda" FROM t') == []


@pytest.mark.parametrize(
    ("sql", "node"),
    [
        ("SELECT a ~ 'b'", exp.RegexpFullMatch),
        ("SELECT regexp_full_match(a, 'b')", exp.RegexpFullMatch),
        ("SELECT regexp_matches(a, 'b')", exp.RegexpLike),
    ],
)
def test_a_tilde_is_a_whole_text_match(sql: str, node: type[exp.Expr]) -> None:
    """`~` reads as `regexp_full_match`, as Lakana documents; `regexp_matches` stays partial.

    Parameters
    ----------
    sql
        A statement with one regular-expression match.
    node
        The node it must parse to.
    """
    parsed = sql_dialect.parse_one(sql)

    assert isinstance(parsed, exp.Select)
    assert type(parsed.selects[0]) is node


def test_a_negated_tilde_is_a_negated_whole_text_match() -> None:
    """`!~` is `NOT` of the same whole-text match."""
    parsed = sql_dialect.parse_one("SELECT a !~ 'b'")

    assert isinstance(parsed, exp.Select)
    match = parsed.selects[0]
    assert isinstance(match, exp.Not)
    assert isinstance(match.this, exp.RegexpFullMatch)


def test_only_duckdb_is_read_the_lakana_way() -> None:
    """Another dialect is passed to sqlglot unchanged."""
    assert sql_dialect.reading_dialect("duckdb") is LakanaDuckDB
    assert sql_dialect.reading_dialect("postgres") == "postgres"


def test_every_statement_of_a_script_is_read_the_lakana_way() -> None:
    """`parse` reads each statement with the same parser as `parse_one`."""
    trees = sql_dialect.parse("SELECT a ~ 'b'; SELECT a ~ 'c'")

    assert [type(tree.selects[0]) for tree in trees if isinstance(tree, exp.Select)] == [
        exp.RegexpFullMatch,
        exp.RegexpFullMatch,
    ]
