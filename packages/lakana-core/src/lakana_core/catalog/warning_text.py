"""Names in warnings about what a change breaks: escaped, short, and safe to paste (#374).

A deploy's impact warning and a retyping run's warning name tables and columns from manifests
and schemas, which are untrusted: a control or format character could hide or forge text in a
terminal or a CI log.
"""

from __future__ import annotations

import shlex
import unicodedata

NAMED_CHARS = 100
"""The most characters of a removed name a warning shows, and puts into a command."""

UNSAFE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
"""Characters a warning writes as escapes: control, format and surrogate characters, and line
and paragraph separators, which could hide or forge text in a terminal or a CI log."""


def named(name: str) -> str:
    """Show a removed name in a warning: as it is, but for escaped unsafe characters, and short.

    Parameters
    ----------
    name
        A table or column name from a manifest; untrusted.

    Returns
    -------
    str
        `name` with each `UNSAFE` character ASCII-escaped (non-ASCII letters stay as they
        are), cut at `NAMED_CHARS` characters with ``…``.
    """
    text = "".join(ascii(c)[1:-1] if unicodedata.category(c) in UNSAFE else c for c in name)
    return text if len(text) <= NAMED_CHARS else text[:NAMED_CHARS] + "…"


def pastable(name: str) -> bool:
    """Say whether a name can go into a command as it is: short, and without `UNSAFE` characters.

    Parameters
    ----------
    name
        A table or column name from a manifest; untrusted.

    Returns
    -------
    bool
        The answer.
    """
    return len(name) <= NAMED_CHARS and named(name) == name


def typed(table: str, column: str | None = None) -> str:
    """Write the arguments of the `lakana graph impact` hint, quoted for a shell.

    Parameters
    ----------
    table
        The removed column's table, or the removed table.
    column
        The removed column; `None` for a table.

    Returns
    -------
    str
        ``TABLE --column COLUMN``, each part shell-quoted only where it needs it; the table
        alone for a table and for a column that is not `pastable`; the table as `named`
        shows it when it is not `pastable` itself.
    """
    if not pastable(table):
        return named(table)
    if column is None or not pastable(column):
        return shlex.quote(table)
    return f"{shlex.quote(table)} --column {shlex.quote(column)}"
