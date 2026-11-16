"""Describing a SQL syntax error for the person who wrote the SQL.

Everything sqlglot reports can echo the caller's SQL back at any length, and no query
timeout interrupts a running regular expression. So every string is cut to a fixed length
before it is processed, and nothing here uses a pattern or a loop whose cost grows faster
than linearly with that (already bounded) input.
"""

from __future__ import annotations

import unicodedata
from typing import NamedTuple

import sqlglot.errors
from sqlglot import exp

from lakana_core.errors import SqlPosition, SqlRefusal

SQL_SYNTAX_ERRORS = (sqlglot.errors.ParseError, sqlglot.errors.TokenError, RecursionError)
"""What a parse raises for SQL it cannot read; a fault of the query, not of the platform.

`RecursionError` is the parser's own limit on nesting (a few hundred parentheses)."""

MAX_MESSAGE_CHARS = 300
"""The longest message `describe_syntax_error` returns."""

_MAX_INPUT_CHARS = 2000
"""How much of any string from sqlglot is looked at at all."""
_MAX_DESCRIPTION_CHARS = 120
_MAX_EXCERPT_CHARS = 40
_BLANKED = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
_DROPPED = frozenset({"Mn", "Me"})


def _clean(text: object, limit: int) -> str:
    """Make text from sqlglot safe to store and print: one line, no control characters, bounded.

    Parameters
    ----------
    text
        Anything sqlglot reported; it can contain the user's own SQL.
    limit
        The most characters kept; a longer text ends in an ellipsis.

    Returns
    -------
    str
        The text with every control, format, separator and private-use character
        (including ESC, BEL, the C1 range and line or paragraph separators) replaced by a
        space, combining marks dropped, whitespace collapsed, and the length bounded.
    """
    kept = []
    for char in str(text)[:_MAX_INPUT_CHARS]:
        category = unicodedata.category(char)
        if category in _DROPPED:
            continue
        kept.append(" " if category in _BLANKED else char)
    collapsed = " ".join("".join(kept).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _reduce_class_names(text: str) -> str:
    """Reduce each ``<class 'a.b.C'>`` to ``C``, in one left-to-right pass.

    Parameters
    ----------
    text
        The text; only its first `_MAX_INPUT_CHARS` characters are looked at.

    Returns
    -------
    str
        The text with Python class paths shortened to the class name.
    """
    opener, closer = "<class '", "'>"
    parts = []
    rest = text[:_MAX_INPUT_CHARS]
    while True:
        before, found, after = rest.partition(opener)
        parts.append(before)
        if not found:
            return "".join(parts)
        name, closed, rest = after.partition(closer)
        if not closed:
            parts.append(found + name)
            return "".join(parts)
        parts.append(name.rsplit(".", 1)[-1])


def _readable_description(description: object) -> str:
    """Turn sqlglot's description into a phrase a user can read.

    Parameters
    ----------
    description
        sqlglot's own description, which can embed a ``<Token …>`` dump with the offending
        text and a Python class name.

    Returns
    -------
    str
        The description without the token dump (``Expected table name but got <Token …>``
        reads ``Expected table name``), class names reduced to their last part, cleaned,
        bounded and without a trailing full stop; ``invalid SQL`` when nothing is left.
    """
    text = str(description or "")[:_MAX_INPUT_CHARS].partition("<Token")[0].rstrip()
    text = text.removesuffix("but got").rstrip()
    text = _clean(_reduce_class_names(text), _MAX_DESCRIPTION_CHARS)
    return text.rstrip(". ") or "invalid SQL"


class SyntaxProblem(NamedTuple):
    """What is known about a syntax error, each part already cleaned and bounded.

    Parameters
    ----------
    reason
        What is wrong, as a phrase without a trailing full stop.
    line
        The line in the SQL, from 1, when known.
    column
        The column in that line, from 1, when known.
    near
        A short excerpt where the parser stopped; empty when unknown.
    """

    reason: str
    """What is wrong, as a phrase without a trailing full stop."""
    line: int | None
    """The line in the SQL, from 1, when known."""
    column: int | None
    """The column in that line, from 1, when known."""
    near: str
    """A short excerpt where the parser stopped; empty when unknown."""


_MAX_POSITION_DIGITS = 9


def _unclosed_quote_position(
    exc: sqlglot.errors.TokenError, sql: str | None
) -> tuple[int, int] | None:
    """Find where an unclosed quote opens, from the tokenizer's own error.

    The tokenizer reports ``Missing ' from <line>:<offset>`` on the error it chains to the
    one it raises, where the offset counts characters from the start of the whole text.

    Parameters
    ----------
    exc
        The tokenizer's error.
    sql
        The text that was tokenized.

    Returns
    -------
    tuple of int or None
        The line and column, both from 1, or `None` when the SQL is not known or the
        tokenizer's report is not in the expected shape.
    """
    cause = exc.__cause__
    if sql is None or not isinstance(cause, sqlglot.errors.TokenError):
        return None
    head, _, tail = str(cause)[:_MAX_INPUT_CHARS].rpartition(" from ")
    line_text, colon, offset_text = tail.partition(":")
    digits = (line_text, offset_text)
    if not head.startswith("Missing ") or not colon:
        return None
    if not all(d.isascii() and d.isdigit() and len(d) <= _MAX_POSITION_DIGITS for d in digits):
        return None
    offset = int(offset_text)
    if offset > len(sql):
        return None
    line = sql.count("\n", 0, offset) + 1
    return line, offset - (sql.rfind("\n", 0, offset) + 1) + 1


def syntax_problem(
    exc: sqlglot.errors.SqlglotError, sql: str | None = None
) -> SyntaxProblem | None:
    """Read what sqlglot reports about a syntax error, cleaned and bounded.

    Parameters
    ----------
    exc
        The parser's or tokenizer's error.
    sql
        The text that was parsed; lets an unclosed quote be placed by line and column.

    Returns
    -------
    SyntaxProblem or None
        The reason, position and excerpt; `None` when sqlglot reported nothing structured.
    """
    if isinstance(exc, sqlglot.errors.TokenError):
        position = _unclosed_quote_position(exc, sql)
        if position is None:
            return None
        return SyntaxProblem(
            "a quote is opened here and never closed", position[0], position[1], ""
        )
    errors = exc.errors if isinstance(exc, sqlglot.errors.ParseError) else []
    if not errors:
        return None
    first = errors[0]
    line, column = first.get("line"), first.get("col")
    return SyntaxProblem(
        _readable_description(first.get("description")),
        line if isinstance(line, int) and isinstance(column, int) else None,
        column if isinstance(line, int) and isinstance(column, int) else None,
        _clean(first.get("highlight") or "", _MAX_EXCERPT_CHARS),
    )


def describe_syntax_error(
    exc: sqlglot.errors.ParseError | sqlglot.errors.TokenError | RecursionError,
    sql: str | None = None,
) -> str:
    """Describe a SQL syntax error with the parser's reason and position.

    Built only from sqlglot's structured fields, each cut, cleaned and bounded (the
    offending text comes from the caller's SQL and can be long or carry terminal escape
    codes), so the whole message is at most `MAX_MESSAGE_CHARS` characters of printable text.

    Parameters
    ----------
    exc
        The parser's, tokenizer's or recursion-limit error. Only for SQL the reader wrote: it quotes
        the text where the parser stopped, so a caller that parses stored or generated
        text must catch the error and say nothing of it.
    sql
        The text that was parsed. Without it an unclosed quote has no position.

    Returns
    -------
    str
        A message naming what was expected, the line and column when known, a short
        quoted excerpt where the parser stopped, and what to do next.
    """
    if isinstance(exc, RecursionError):
        return (
            "SQL syntax error: the query is nested too deeply. "
            "Simplify it (fewer nested parentheses or subqueries) and submit it again."
        )
    problem = syntax_problem(exc, sql)
    if problem is None:
        return (
            "SQL syntax error: the query could not be read. "
            "Check its quoting and punctuation, then submit it again."
        )
    where = f" at line {problem.line}, column {problem.column}" if problem.line is not None else ""
    near_text = f" near {problem.near!r}" if problem.near else ""
    message = (
        f"SQL syntax error{where}: {problem.reason}{near_text}. "
        "Fix the query's syntax and submit it again."
    )
    return _clean(message, MAX_MESSAGE_CHARS)


def _position_at(sql: str, offset: int) -> SqlPosition | None:
    """Turn a 0-based character offset into a line and a column.

    Parameters
    ----------
    sql
        The text the offset refers to.
    offset
        The 0-based character offset.

    Returns
    -------
    SqlPosition or None
        The line and column, both from 1; `None` when the offset is outside the text.
    """
    if not 0 <= offset <= len(sql):
        return None
    line = sql.count("\n", 0, offset) + 1
    return SqlPosition(line, offset - (sql.rfind("\n", 0, offset) + 1) + 1)


def node_position(node: exp.Expr, sql: str) -> SqlPosition | None:
    """Find where a parsed node starts in the SQL it was parsed from.

    Parameters
    ----------
    node
        A node of the tree parsed from `sql`.
    sql
        The text that was parsed.

    Returns
    -------
    SqlPosition or None
        The line and column of the node's own ``meta["start"]``, else of the smallest
        ``start`` among its descendants; `None` when none of them has one (a placeholder).
    """
    starts = [
        start for candidate in node.walk() if isinstance(start := candidate.meta.get("start"), int)
    ]
    own = node.meta.get("start")
    offset = own if isinstance(own, int) else min(starts, default=None)
    return None if offset is None else _position_at(sql, offset)


def error_position(exc: BaseException, sql: str) -> SqlPosition | None:
    """Find where in the SQL an error is, when it knows.

    Parameters
    ----------
    exc
        A guard refusal or a parse error.
    sql
        The submitted SQL.

    Returns
    -------
    SqlPosition or None
        `SqlRefusal.position`, else the line and column of the syntax problem the parser
        reported; `None` otherwise.
    """
    if isinstance(exc, SqlRefusal):
        return exc.position
    if isinstance(exc, sqlglot.errors.SqlglotError):
        problem = syntax_problem(exc, sql)
        if problem is not None and problem.line is not None and problem.column is not None:
            return SqlPosition(problem.line, problem.column)
    return None
