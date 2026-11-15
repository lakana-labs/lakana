"""The one DuckDB dialect Lakana reads SQL with.

sqlglot's DuckDB dialect follows the newest DuckDB, and since sqlglot 30.25 two of its readings
differ from what Lakana promises its users, so every place that parses SQL Lakana runs or checks
reads it through `parse_one` and `parse` here:

- `~` and `!~` match the whole text, as `regexp_full_match` does (`docs/site/reference/
  sql-in-queries.md`). sqlglot now reads them as the partial match DuckDB 2.0 gives them
  (`regexp_matches`), which would silently widen every query and asset written against the
  documented meaning.
- A lambda written with the `LAMBDA` keyword is marked (`KEYWORD_LAMBDA` in its `meta`).
  sqlglot no longer records the spelling, so `upper(lambda j: 'a')` and `upper(j -> 'a')` became
  one tree; the guard turns only the arrow into the JSON extract DuckDB reads it as
  (`query_rewrite._json_arrows_outside_lambda_functions`), and needs the mark to tell them apart.

Only the parser differs. SQL is rendered with sqlglot's own `"duckdb"` dialect, which writes a
`RegexpFullMatch` as `REGEXP_FULL_MATCH(...)` and every lambda in the keyword form.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp
from sqlglot.dialects.duckdb import DuckDB
from sqlglot.parser import binary_range_parser
from sqlglot.parsers.duckdb import DuckDBParser

if TYPE_CHECKING:
    from sqlglot.dialects.dialect import DialectType

KEYWORD_LAMBDA = "lakana_keyword_lambda"
"""The `meta` key set on an `exp.Lambda` the text wrote with the `LAMBDA` keyword."""

_DUCKDB = "duckdb"

_parse_full_match = binary_range_parser(exp.RegexpFullMatch)


class _LakanaDuckDBParser(DuckDBParser):
    """sqlglot's DuckDB parser with Lakana's reading of `~` and of keyword lambdas."""

    def _parse_tilde(self, this: exp.Expr | None) -> exp.Expr | None:
        """Read `a ~ b` (and `a !~ b`) as a whole-text match.

        Parameters
        ----------
        this
            The left operand.

        Returns
        -------
        sqlglot.expressions.Expr or None
            `RegexpFullMatch(this, <right operand>)`.
        """
        return _parse_full_match(self, this)

    def _parse_lambda(self, alias: bool = False) -> exp.Expr | None:
        """Parse a lambda as sqlglot does, marking one written with the `LAMBDA` keyword.

        sqlglot's DuckDB parser builds a lambda from the keyword form only after matching the
        `LAMBDA` keyword at the current token, and otherwise returns `None` or hands over to the
        generic parser, which never matches that keyword. So a lambda returned while the current
        token reads `LAMBDA` is the keyword form.

        Parameters
        ----------
        alias
            Passed through to sqlglot's parser.

        Returns
        -------
        sqlglot.expressions.Expr or None
            What sqlglot's parser returns; an `exp.Lambda` from the keyword form carries
            `KEYWORD_LAMBDA` in its `meta`.
        """
        current = self._curr
        keyword = current is not None and current.text.upper() == "LAMBDA"
        node = super()._parse_lambda(alias=alias)
        if keyword and isinstance(node, exp.Lambda):
            node.meta[KEYWORD_LAMBDA] = True
        return node


class LakanaDuckDB(DuckDB):
    """sqlglot's DuckDB dialect with `_LakanaDuckDBParser`; registered as `lakanaduckdb`."""

    Parser = _LakanaDuckDBParser


def reading_dialect(dialect: DialectType) -> DialectType:
    """Name the dialect to parse with: Lakana's reading for DuckDB, any other one unchanged.

    Parameters
    ----------
    dialect
        The dialect a caller asked for.

    Returns
    -------
    sqlglot.dialects.dialect.DialectType
        `LakanaDuckDB` for `"duckdb"`, otherwise `dialect`.
    """
    return LakanaDuckDB if dialect == _DUCKDB else dialect


def parse_one(
    sql: str, dialect: DialectType = _DUCKDB, into: exp.IntoType | None = None
) -> exp.Expr:
    """Parse one statement, as `sqlglot.parse_one` does, with Lakana's reading of DuckDB.

    Parameters
    ----------
    sql
        The text to parse.
    dialect
        The dialect; `"duckdb"` reads with `LakanaDuckDB`.
    into
        Passed to `sqlglot.parse_one`: the node type(s) to parse the text as.

    Returns
    -------
    sqlglot.expressions.Expr
        The parsed statement.
    """
    return sqlglot.parse_one(sql, dialect=reading_dialect(dialect), into=into)


def parse(sql: str, dialect: DialectType = _DUCKDB) -> list[exp.Expr | None]:
    """Parse every statement of `sql`, as `sqlglot.parse` does, with Lakana's reading of DuckDB.

    Parameters
    ----------
    sql
        The text to parse.
    dialect
        The dialect; `"duckdb"` reads with `LakanaDuckDB`.

    Returns
    -------
    list of sqlglot.expressions.Expr or None
        One tree per statement.
    """
    return sqlglot.parse(sql, dialect=reading_dialect(dialect))
