"""Bind `:name` parameters into a driver's placeholder form without touching literals.

Values never enter the query text: the driver binds them. The tokenizer skips single-quoted
strings (with `''`, and backslash escapes for `E'...'` or where the database uses them),
double-quoted identifiers or strings, backtick identifiers, PostgreSQL's dollar quotes
(`$$...$$`, `$tag$...$tag$`), `--` and `/* */` comments (nested for PostgreSQL only), MariaDB's
`#` comments (``pyformat``), and `::` casts. `code_text` keeps only what is left: the code.

Where the tokenizer and the database could read a quote or a comment differently, it errs
towards "literal": a placeholder it skips reaches the database unchanged and fails there, but
it never puts a value where the database reads code. That matters for ``pyformat`` only, where
PyMySQL writes the escaped value into the text; PostgreSQL and SQLite bind on the server. So
``pyformat`` reads the text as MariaDB does: no dollar quotes and no ``E'...'`` strings, ``--``
starts a comment only before a space or a control character (DEL included), block comments do
not nest, a double quote encloses an identifier without escapes under ``ANSI_QUOTES``, and an
executable comment (``/*! ... */``, ``/*M! ... */``), whose text MariaDB runs or skips by its
version, is refused.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from lakana.ingest._errors import UsageError

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _word(char: str) -> bool:
    """Say whether a character continues an identifier.

    Parameters
    ----------
    char
        One character.

    Returns
    -------
    bool
        For letters, digits, `_` and `$`.
    """
    return char.isalnum() or char in "_$"


def _quoted_end(text: str, start: int, quote: str, backslash: bool) -> int:
    """Find the end of a quoted literal or identifier.

    Parameters
    ----------
    text
        The query.
    start
        The opening quote's index.
    quote
        The quote character.
    backslash
        Whether a backslash escapes the next character.

    Returns
    -------
    int
        The index after the closing quote (the text's end when unterminated).
    """
    i = start + 1
    while i < len(text):
        if backslash and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            if i + 1 < len(text) and text[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return len(text)


def _comment_end(text: str, start: int, *, nested: bool) -> int:
    """Find the end of a block comment.

    Parameters
    ----------
    text
        The query.
    start
        The index of its `/*`.
    nested
        Whether a `/*` inside opens a nested comment (PostgreSQL), or not (MariaDB).

    Returns
    -------
    int
        The index after its `*/` (the text's end when unterminated).
    """
    if not nested:
        close = text.find("*/", start + 2)
        return len(text) if close < 0 else close + 2
    depth, i = 0, start
    while i < len(text):
        if text.startswith("/*", i):
            depth, i = depth + 1, i + 2
        elif text.startswith("*/", i):
            depth, i = depth - 1, i + 2
            if depth == 0:
                return i
        else:
            i += 1
    return len(text)


def _line_comment(text: str, start: int, *, mariadb: bool) -> bool:
    """Say whether a line comment starts at an index.

    Parameters
    ----------
    text
        The query.
    start
        The index.
    mariadb
        Whether the text is read as MariaDB reads it: ``#`` starts a comment, and ``--`` only
        before a space or a control character, DEL (0x7F) included (``1--1`` is one minus minus
        one).

    Returns
    -------
    bool
        Whether the rest of the line is a comment.

    Raises
    ------
    UsageError
        For ``--`` right after a number's exponent, where MySQL reads no comment.
    """
    if not mariadb:
        return text.startswith("--", start)
    if text.startswith("#", start):
        return True
    after = start + 2
    if not text.startswith("--", start) or not (
        after >= len(text) or ord(text[after]) <= 32 or text[after] == "\x7f"
    ):
        return False
    if (
        start >= 2
        and text[start - 1] in "eE"
        and (text[start - 2].isdigit() or text[start - 2] == ".")
    ):
        # MySQL reads `1.5e--` as the exponent sign and a minus sign, not as a comment.
        raise UsageError(
            "the query has `--` right after a number's exponent (such as 1.5e--), which MySQL "
            "and MariaDB read differently; write a space between the number and --"
        )
    return True


def translate(
    query: str,
    params: Mapping[str, object] | None,
    *,
    style: Literal["numeric", "qmark", "pyformat"],
    backslash_escapes: bool = False,
    ansi_quotes: bool = False,
) -> tuple[str, list[object] | dict[str, object]]:
    """Rewrite `:name` placeholders into the driver's form and order the values.

    Parameters
    ----------
    query
        The asset's SQL.
    params
        Name → value; `None` for none.
    style
        ``numeric`` (`$1`, PostgreSQL), ``qmark`` (`?`, SQLite) or ``pyformat`` (`%(name)s`,
        PyMySQL, which also needs every literal `%` doubled).
    backslash_escapes
        Whether a backslash escapes inside quotes (MariaDB's default).
    ansi_quotes
        Whether a double quote encloses an identifier, where a backslash escapes nothing
        (MariaDB's ``ANSI_QUOTES``).

    Returns
    -------
    tuple
        The rewritten query and the values: a list for ``numeric`` and ``qmark``, a dict for
        ``pyformat``.
    """
    sql, values, _ = _scan(
        query,
        params,
        style=style,
        backslash_escapes=backslash_escapes,
        ansi_quotes=ansi_quotes,
        bind=True,
    )
    return sql, values


def code_text(
    query: str,
    *,
    style: Literal["numeric", "qmark", "pyformat"],
    backslash_escapes: bool = False,
    ansi_quotes: bool = False,
) -> str:
    """Blank out a query's literals, quoted names, comments and placeholders.

    What is left is what the database reads as code, read with the same rules as `translate`,
    so a check of keywords (the first statement's, ``INTO OUTFILE``) sees what runs.

    Parameters
    ----------
    query
        The asset's SQL.
    style
        As for `translate`.
    backslash_escapes
        As for `translate`.
    ansi_quotes
        As for `translate`.

    Returns
    -------
    str
        The code, each blanked part a single space.
    """
    return _scan(
        query,
        None,
        style=style,
        backslash_escapes=backslash_escapes,
        ansi_quotes=ansi_quotes,
        bind=False,
    )[2]


def _scan(
    query: str,
    params: Mapping[str, object] | None,
    *,
    style: Literal["numeric", "qmark", "pyformat"],
    backslash_escapes: bool,
    ansi_quotes: bool,
    bind: bool,
) -> tuple[str, list[object] | dict[str, object], str]:
    """Read a query once: rewrite its placeholders and keep its code.

    Parameters
    ----------
    query
        The asset's SQL.
    params
        Name → value; `None` for none.
    style
        As for `translate`.
    backslash_escapes
        As for `translate`.
    ansi_quotes
        As for `translate`.
    bind
        Whether placeholders are matched with `params` (else only blanked in the code).

    Returns
    -------
    tuple
        The rewritten query, the values, and the code (`code_text`).

    Raises
    ------
    UsageError
        For a placeholder without a value, a value without a placeholder, a `None` value, or
        text this tokenizer cannot read the way MariaDB does.
    """
    given = dict(params or {})
    for name, value in given.items():
        if value is None:
            raise UsageError(
                f"parameter {name} is None, so the comparison would match no row. On a first run "
                "ctx.watermark() is None: give the start value with ctx.watermark(default=...)"
            )
    out: list[str] = []
    code: list[str] = []
    numbers: dict[str, int] = {}
    ordered: list[object] = []
    used: set[str] = set()
    mariadb, postgres = style == "pyformat", style == "numeric"
    i, n = 0, len(query)
    while i < n:
        char = query[i]
        kept = " "
        if char in "'\"`":
            escaped = backslash_escapes and char != "`" and not (char == '"' and ansi_quotes)
            if (
                postgres
                and char == "'"
                and i > 0
                and query[i - 1] in "eE"
                and (i < 2 or not _word(query[i - 2]))
            ):
                escaped = True
            end = _quoted_end(query, i, char, escaped)
        elif _line_comment(query, i, mariadb=mariadb):
            newline = query.find("\n", i)
            end = n if newline < 0 else newline
        elif query.startswith("/*", i):
            if mariadb and query.startswith(("/*!", "/*M!"), i):
                raise UsageError(
                    "the query holds an executable comment (/*! ... */), whose text MariaDB "
                    "runs or skips by its version; sql.read cannot tell which, so write the "
                    "statement without it"
                )
            end = _comment_end(query, i, nested=postgres)
        elif (
            postgres
            and char == "$"
            and (i == 0 or not _word(query[i - 1]))
            and (match := _DOLLAR.match(query, i))
        ):
            close = query.find(match.group(0), match.end())
            end = n if close < 0 else close + len(match.group(0))
        elif query.startswith("::", i):
            end, kept = i + 2, "::"
        elif char == ":" and (match := _NAME.match(query, i + 1)):
            name = match.group(0)
            code.append(" ")
            i = match.end()
            if not bind:
                continue
            if name not in given:
                raise UsageError(f"placeholder :{name} has no value in params")
            used.add(name)
            if style == "numeric":
                numbers.setdefault(name, len(numbers) + 1)
                if numbers[name] == len(ordered) + 1:
                    ordered.append(given[name])
                out.append(f"${numbers[name]}")
            elif style == "qmark":
                ordered.append(given[name])
                out.append("?")
            else:
                out.append(f"%({name})s")
            continue
        elif char == "%" and style == "pyformat":
            out.append("%%")
            code.append("%")
            i += 1
            continue
        else:
            end, kept = i + 1, char
        chunk = query[i:end]
        out.append(chunk.replace("%", "%%") if style == "pyformat" else chunk)
        code.append(kept)
        i = end
    unused = sorted(set(given) - used) if bind else []
    if unused:
        raise UsageError(f"params has {', '.join(unused)}, which the query does not use")
    values: list[object] | dict[str, object] = (
        {name: given[name] for name in sorted(used)} if style == "pyformat" else ordered
    )
    return "".join(out), values, "".join(code)
