"""Describing a SQL syntax error."""

from __future__ import annotations

import time
import unicodedata

import pytest
import sqlglot
import sqlglot.errors
from sqlglot import exp

from lakana_core.errors import SqlPosition, SqlRefusal
from lakana_core.sql_syntax import (
    MAX_MESSAGE_CHARS,
    SQL_SYNTAX_ERRORS,
    _clean,
    _reduce_class_names,
    describe_syntax_error,
    error_position,
    node_position,
    syntax_problem,
)


def _error(sql: str) -> sqlglot.errors.ParseError | sqlglot.errors.TokenError | RecursionError:
    """Parse `sql`, which must fail, and return the error.

    Parameters
    ----------
    sql
        Invalid SQL.

    Returns
    -------
    ParseError, TokenError or RecursionError
        What sqlglot raised.
    """
    with pytest.raises(SQL_SYNTAX_ERRORS) as info:
        sqlglot.parse_one(sql, dialect="duckdb")
    return info.value


def test_a_parse_error_names_the_reason_the_position_and_the_token() -> None:
    """The message carries sqlglot's description, line, column and token, with no escape codes."""
    message = describe_syntax_error(_error("SELEC 1"))

    assert message.startswith("SQL syntax error at line 1, column 7: ")
    assert "Invalid expression / Unexpected token" in message
    assert "near '1'" in message
    assert "\x1b" not in message
    assert message.endswith("submit it again.")


def test_a_tokenizer_error_has_no_position_and_still_says_what_to_do() -> None:
    """A `TokenError` carries no structured position; the message still names the next step."""
    message = describe_syntax_error(_error("SELECT 'unterminated"))

    assert message.startswith("SQL syntax error: the query could not be read.")
    assert "quoting" in message


def test_an_error_without_a_position_omits_it() -> None:
    """A parse error whose entry lacks a line and column still reads cleanly."""
    exc = sqlglot.errors.ParseError("x", errors=[{"description": "Bad thing"}])

    assert describe_syntax_error(exc) == (
        "SQL syntax error: Bad thing. Fix the query's syntax and submit it again."
    )


def test_an_error_without_a_description_says_invalid_sql() -> None:
    """A missing description falls back to a generic reason."""
    exc = sqlglot.errors.ParseError("x", errors=[{"line": 2, "col": 3}])

    assert "at line 2, column 3: invalid SQL." in describe_syntax_error(exc)


def _unsafe(message: str) -> list[str]:
    """List the characters of a message that are not plain printable text.

    Parameters
    ----------
    message
        The message.

    Returns
    -------
    list of str
        Every control, format, separator or private-use character in it.
    """
    return [c for c in message if unicodedata.category(c) in {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t JOIN $$\x1b[2J\x07" + "z" * 3000 + "$$ +",
        "SELECT * FROM t JOIN " + "1" * 50000,
        "SELECT * FROM t JOIN e'\x1b[2J\u2028\x85" + "z" * 3000 + "' +",
        "SELECT * FROM t JOIN 1" + "\n2" * 5,
    ],
)
def test_a_hostile_query_gives_a_bounded_message_of_printable_text(sql: str) -> None:
    """The offending text is cut, and no control or separator character survives."""
    message = describe_syntax_error(_error(sql))

    assert len(message) <= MAX_MESSAGE_CHARS
    assert _unsafe(message) == []
    assert message.startswith("SQL syntax error at line 1, column ")


def test_the_token_dump_is_replaced_by_a_plain_phrase() -> None:
    """``Expected table name but got <Token …>`` reads ``Expected table name``."""
    message = describe_syntax_error(_error("SELECT * FROM ("))

    assert "<Token" not in message
    assert "TokenType" not in message
    assert "Expected table name" in message


def test_a_class_name_in_the_description_is_reduced_to_its_last_part() -> None:
    """A Python module path means nothing to the person who wrote the SQL."""
    message = describe_syntax_error(_error("SELECT 1 +"))

    assert "sqlglot.expressions" not in message
    assert "Add" in message


def test_an_ordinary_typo_keeps_a_helpful_message() -> None:
    """A common mistake still names the reason, line, column and the token."""
    assert describe_syntax_error(_error("SELEC 1")) == (
        "SQL syntax error at line 1, column 7: Invalid expression / Unexpected token near '1'. "
        "Fix the query's syntax and submit it again."
    )


def test_a_huge_whitespace_operator_returns_at_once_and_stays_bounded() -> None:
    """N1: no pattern may cost more than linear time on text sqlglot echoes back."""
    error = _error("SELECT 1 FROM t |> '" + " " * 200_000 + "'")

    started = time.monotonic()
    message = describe_syntax_error(error)

    assert time.monotonic() - started < 2
    assert len(message) <= MAX_MESSAGE_CHARS
    assert _unsafe(message) == []


def test_no_full_stop_precedes_the_excerpt() -> None:
    """The description's own trailing full stop is dropped before ``near``."""
    message = describe_syntax_error(_error("SELECT 1 FROM t |> 'abc'"))

    assert ". near" not in message
    assert " near " in message
    assert message.count("near") == 1


def test_combining_marks_are_dropped_from_the_excerpt() -> None:
    """N4: dozens of stacked marks on one character do not reach the message."""
    stacked = "e" + "\u0301" * 40
    message = describe_syntax_error(_error(f"SELECT * FROM t JOIN {stacked} +"))

    assert not any(unicodedata.category(c) in {"Mn", "Me"} for c in message)


def test_combining_marks_are_removed_by_the_cleaning_step() -> None:
    """N4, directly: marks (Mn, Me) vanish and the base character stays."""
    assert _clean("e" + "́" * 40 + "⃝" + "x", 40) == "ex"


def test_class_names_are_reduced_and_an_unclosed_one_is_left_alone() -> None:
    """A closed ``<class '...'>`` becomes its last part; an unclosed opener is kept as text."""
    assert _reduce_class_names("missing for <class 'a.b.Add'> here") == "missing for Add here"
    assert _reduce_class_names("odd <class 'a.b and more") == "odd <class 'a.b and more"


def test_a_recursion_error_reads_as_nesting_too_deep() -> None:
    """B: the parser's recursion limit is the query's depth, said plainly."""
    message = describe_syntax_error(RecursionError("maximum recursion depth exceeded"))

    assert message == (
        "SQL syntax error: the query is nested too deeply. "
        "Simplify it (fewer nested parentheses or subqueries) and submit it again."
    )
    assert isinstance(RecursionError(), SQL_SYNTAX_ERRORS)


def test_reducing_class_names_costs_the_same_whatever_the_caller_cut() -> None:
    """D: the function bounds its own input, so a caller that forgets to cut is still safe."""
    started = time.monotonic()
    reduced = _reduce_class_names("<class 'a.B'>" * 400_000)

    assert time.monotonic() - started < 2
    assert len(reduced) <= 2000


@pytest.mark.parametrize(
    ("sql", "line", "column"),
    [
        ("SELECT 'abc FROM t", 1, 8),
        ("SELECT 1\n  FROM 'abc", 2, 8),
        ('SELECT "a\nFROM t', 1, 8),
    ],
)
def test_an_unclosed_quote_is_placed_by_line_and_column(sql: str, line: int, column: int) -> None:
    """With the SQL at hand, the position of the opening quote is named."""
    message = describe_syntax_error(_error(sql), sql)

    assert message.startswith(f"SQL syntax error at line {line}, column {column}: ")
    assert "never closed" in message
    assert message.endswith("submit it again.")


def test_an_unclosed_quote_without_the_sql_stays_generic() -> None:
    """A caller that cannot name the text gets no position, never a wrong one."""
    message = describe_syntax_error(_error("SELECT 'abc"))

    assert "could not be read" in message
    assert "line" not in message


@pytest.mark.parametrize("report", ["Missing ' from x:5", "Missing ' from 1:99999", "Missing '"])
def test_a_tokenizer_report_in_an_unexpected_shape_gives_no_position(report: str) -> None:
    """A changed or out-of-range report from sqlglot falls back to the generic message."""
    exc = sqlglot.errors.TokenError("Error tokenizing 'x'")
    exc.__cause__ = sqlglot.errors.TokenError(report)

    assert syntax_problem(exc, "SELECT 'a") is None


def test_an_unterminated_comment_has_no_position() -> None:
    """An unterminated comment surfaces as a bare `IndexError`; there is no position."""
    sql = "SELECT 1 /* never ends"

    assert "could not be read" in describe_syntax_error(_error(sql), sql)


def test_node_position_uses_the_start_offset_not_the_end_column() -> None:
    """The node starts where its own text does, not where the tokenizer's column ends."""
    sql = "SELECT *\nFROM read_csv('raw/x.csv')"
    node = sqlglot.parse_one(sql, dialect="duckdb").find(exp.ReadCSV)
    assert node is not None
    assert node_position(node, sql) == SqlPosition(2, 6)


def test_node_position_falls_back_to_the_first_descendant() -> None:
    """A node with no offset of its own starts where its earliest descendant does."""
    sql = "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t)\nSELECT * FROM t"
    node = sqlglot.parse_one(sql, dialect="duckdb").find(exp.CTE)
    assert node is not None
    assert "start" not in node.meta
    assert node_position(node, sql) == SqlPosition(1, 16)


def test_a_node_without_any_offset_has_no_position() -> None:
    """A placeholder, and everything under it, has nothing to point at."""
    assert node_position(exp.Placeholder(), "SELECT ?") is None


def test_error_position_of_a_parse_error_is_the_parsers() -> None:
    """The position is the one the message names."""
    sql = "SELECT 1 FROM"
    exc = _error(sql)
    assert isinstance(exc, sqlglot.errors.ParseError)
    problem = syntax_problem(exc, sql)
    assert problem is not None
    assert error_position(exc, sql) == SqlPosition(problem.line or 0, problem.column or 0)
    assert f"line {problem.line}, column {problem.column}" in describe_syntax_error(exc, sql)


def test_a_node_from_a_longer_text_has_no_position_in_a_shorter_one() -> None:
    """An offset past the end of the given SQL (a node parsed from other text) points nowhere."""
    longer = "SELECT 1;\n" * 5 + "SELECT * FROM read_csv('raw/x.csv')"
    node = sqlglot.parse(longer, dialect="duckdb")[-1]
    assert node is not None
    node = node.find(exp.ReadCSV)
    assert node is not None
    assert node_position(node, longer) == SqlPosition(6, 15)
    assert node_position(node, "SELECT 1") is None


def test_error_position_of_a_parse_error_without_a_line_is_none() -> None:
    """A parse error that names no line, or an unclosed quote without the SQL, has no position."""
    unplaced = sqlglot.errors.ParseError("x", errors=[{"description": "Bad thing"}])
    assert error_position(unplaced, "SELECT 1") is None
    quote = _error("SELECT 'never closed")
    assert isinstance(quote, sqlglot.errors.TokenError)
    assert error_position(quote, "") is None


def test_error_position_of_a_refusal_is_its_own_and_of_anything_else_is_none() -> None:
    """A refusal carries its position; an error that is neither refusal nor syntax has none."""
    assert error_position(SqlRefusal("no", SqlPosition(3, 4)), "x") == SqlPosition(3, 4)
    assert error_position(SqlRefusal("no"), "x") is None
    assert error_position(ValueError("x"), "x") is None
