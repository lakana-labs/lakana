"""Rendering command results as Rich tables or JSON, and errors to stderr."""

from __future__ import annotations

import base64
import json
import math
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from rich.console import Console
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    import pyarrow as pa

    from lakana.cli._state import CliState


def to_jsonable(value: Any) -> Any:  # ruff: ignore[any-type]
    """Convert SDK results into plain JSON-compatible values.

    Parameters
    ----------
    value
        A dataclass, container, or scalar.

    Returns
    -------
    Any
        Dicts, lists, strings, numbers, booleans and `None` only. A NaN or
        infinite float becomes `None`, since JSON has no spelling for it.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, Enum):
        return value.value
    return value


def print_json(value: Any) -> None:  # ruff: ignore[any-type]
    """Write `value` to stdout as indented JSON, bypassing Rich.

    `allow_nan=False` backs up `to_jsonable`'s non-finite-float conversion:
    if one ever slipped through, this raises rather than print `NaN`, which
    no strict JSON parser accepts.

    Parameters
    ----------
    value
        Anything `to_jsonable` accepts.
    """
    text = json.dumps(to_jsonable(value), indent=2, ensure_ascii=False, allow_nan=False)
    sys.stdout.write(text + "\n")


def _console(stderr: bool = False) -> Console:
    """Build a Rich console on the current stdout or stderr.

    Parameters
    ----------
    stderr
        Write to stderr instead of stdout.

    Returns
    -------
    Console
        A console without syntax highlighting.
    """
    return Console(file=sys.stderr if stderr else sys.stdout, highlight=False)


_UNSAFE = re.compile(
    r"[\x00-\x09\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
"""C0 and C1 controls except the line feed (`_visible` turns a tab into spaces and makes every
other line break a line feed first), DEL and the bidirectional controls."""

_TAB_SPACES = "    "
"""What a tab becomes in a cell."""

_BLANK_LINES = re.compile(r"\n(?: *\n)+")
"""A line break followed by lines of nothing but spaces: a run of blank lines."""

_MAX_CELL_LINES = 50
"""The most lines a cell shows; the rest is counted in one last line."""


def _escaped(match: re.Match[str]) -> str:
    r"""Spell one unsafe character the way Python writes it.

    Parameters
    ----------
    match
        The character.

    Returns
    -------
    str
        `\x1b` for a code point below 256, `\u202e` above.
    """
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def safe_text(text: str) -> str:
    r"""Make a message safe to print, keeping its lines whole.

    Parameters
    ----------
    text
        A message, or text a server sent, such as a view's SQL.

    Returns
    -------
    str
        `text` with each line break a line feed, each tab four spaces, and every other control
        and bidirectional control character written as an escape (`_visible` does the same but
        also limits the lines); text that is already escaped is unchanged.
    """
    flat = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", _TAB_SPACES)
    return _UNSAFE.sub(_escaped, flat)


def _visible(text: str) -> str:
    r"""Make a value safe to print in a table.

    A value is data, not instructions to the terminal: an escape sequence could rewrite what
    the screen shows, and a bidirectional override could reorder it. A line break is kept (a
    task's error is meant to read on several lines, and Rich keeps the lines inside the
    cell's borders), with ``\r\n`` and a lone ``\r`` made a line feed so that no carriage
    return moves the cursor back; a tab becomes four spaces.

    Parameters
    ----------
    text
        The value as text.

    Returns
    -------
    str
        `text` with each line break a line feed, each tab four spaces, and every other control
        and bidirectional control character written as an escape; other characters, non-ASCII
        letters included, are unchanged; at most `_MAX_CELL_LINES` lines and, when there are
        more, a last line ``… (N more lines; use --output json to see all)``.
    """
    flat = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", _TAB_SPACES)
    lines = _BLANK_LINES.sub("\n", _UNSAFE.sub(_escaped, flat)).strip("\n").split("\n")
    if len(lines) <= _MAX_CELL_LINES:
        return "\n".join(lines)
    more = len(lines) - _MAX_CELL_LINES
    return "\n".join(
        [*lines[:_MAX_CELL_LINES], f"… ({more} more lines; use --output json to see all)"]
    )


_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def _bytes_text(count: int) -> str:
    """Write a byte count in binary units.

    Parameters
    ----------
    count
        The count, at least 0.

    Returns
    -------
    str
        ``0 B`` for zero; whole bytes below 1 KiB; above that one decimal, none from 100 on
        (``1.2 GiB``, ``412 MiB``).
    """
    value = float(count)
    unit = 0
    while unit < len(_UNITS) - 1 and round(value, 0 if value >= 100 else 1) >= 1024:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{count} B"
    return (
        f"{value:.0f} {_UNITS[unit]}" if round(value, 1) >= 100 else f"{value:.1f} {_UNITS[unit]}"
    )


def _cpu_text(seconds: float) -> str:
    """Write processor time.

    Parameters
    ----------
    seconds
        The time, at least 0.

    Returns
    -------
    str
        ``3.1 s CPU`` below a minute, ``1 m 5 s CPU`` from 60 s.
    """
    if round(seconds, 1) < 60:
        return f"{seconds:.1f} s CPU"
    whole = round(seconds)
    return f"{whole // 60} m {whole % 60} s CPU"


def resources_text(
    peak_memory_bytes: int | None, cpu_seconds: float | None, bytes_read: int | None
) -> str:
    """Write what an attempt or a query used, on one line.

    Parameters
    ----------
    peak_memory_bytes
        Peak memory in bytes, or `None` if not measured.
    cpu_seconds
        Processor time in seconds, or `None` if not measured.
    bytes_read
        Bytes read, or `None` if unknown.

    Returns
    -------
    str
        For example ``412 MiB · 3.1 s CPU · 1.2 GiB read``. A part that is unknown is left
        out (zero is a measurement and stays); with none known, ``—``.
    """
    parts = []
    if peak_memory_bytes is not None:
        parts.append(_bytes_text(peak_memory_bytes))
    if cpu_seconds is not None:
        parts.append(_cpu_text(cpu_seconds))
    if bytes_read is not None:
        parts.append(f"{_bytes_text(bytes_read)} read")
    return " \u00b7 ".join(parts) or "\u2014"


def inline(text: str, limit: int = 200) -> str:
    r"""Make one untrusted value safe to put inside a line of a message.

    Parameters
    ----------
    text
        The value, such as a name a server or a user gave.
    limit
        The most characters kept, before a ``…``.

    Returns
    -------
    str
        `text` on one line: control and bidirectional control characters written as escapes
        (as in a table cell), each line break written ``\n``, cut at `limit` characters.
    """
    flat = _visible(text).replace("\n", "\\n")
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _cell(value: Any) -> Text:  # ruff: ignore[any-type]
    """Render one value as literal text for a table cell.

    Parameters
    ----------
    value
        The value.

    Returns
    -------
    Text
        Literal text; markup is never interpreted and no control character reaches the
        terminal (`_visible`).
    """
    if value is None:
        return Text("")
    if isinstance(value, float):
        return Text(str(value))
    plain = to_jsonable(value)
    if isinstance(plain, (dict, list)):
        return Text(_visible(json.dumps(plain, ensure_ascii=False)))
    return Text(_visible(str(plain)))


_UNBOUNDED_WIDTH = 100_000
"""A width no table needs; `Console.measure` caps a table's width at the console's own."""

_WHOLE_SUFFIXES = ("_id", "_at", "_sha", "_sha256")

_FLEXIBLE_WIDTH = 30
"""How wide a `flexible` column counts when `emit` decides which columns to leave out."""


def _is_whole(name: str) -> bool:
    """Say whether a column holds an identifier, a time or a digest.

    Such a value is only useful whole: it is copied, compared or read as a time, so a
    listing never wraps, folds or cuts it while the table can fit it.

    Parameters
    ----------
    name
        The field name.

    Returns
    -------
    bool
        `True` for `id`, `uuid` and names ending in `_id`, `_at`, `_sha` or `_sha256`.
    """
    return name in {"id", "uuid"} or name.endswith(_WHOLE_SUFFIXES)


def _list_table(
    rows: Sequence[Any],
    names: Sequence[str],
    *,
    whole: Sequence[str] | None,
    derived: Mapping[str, Callable[[Any], Any]],
    headers_only: bool = False,
    flexible: Sequence[str] = (),
) -> Table:
    """Build the table of a listing.

    Parameters
    ----------
    rows
        The dataclasses, one per row.
    names
        The columns to show, in order.
    whole
        The columns that never wrap, or `None` for none.
    derived
        Columns that are not a field: the function that reads each from a row.
    headers_only
        Leave the cells of every column that may wrap empty, so that the table measures as
        wide as the columns that never wrap plus the other headers.
    flexible
        Columns of free text that wrap well: for a measurement their cells are cut to
        `_FLEXIBLE_WIDTH` characters, so that a long sentence does not decide which other
        columns are left out. Only for measuring; the table that is printed is never cut.

    Returns
    -------
    Table
        A table whose other columns fold a long value onto more lines rather than cutting
        it, and whose headers are never cut in a word.
    """
    kept = whole or ()
    table = Table()
    for name in names:
        table.add_column(Text(name), overflow="fold", no_wrap=name in kept, min_width=len(name))
    for row in rows:
        cells = []
        for name in names:
            if headers_only and name not in kept:
                cells.append(_cell(None))
                continue
            cell = _cell(derived[name](row) if name in derived else getattr(row, name))
            cells.append(Text(cell.plain[:_FLEXIBLE_WIDTH]) if name in flexible else cell)
        table.add_row(*cells)
    return table


def emit(
    state: CliState,
    value: Any,  # ruff: ignore[any-type]
    *,
    columns: Sequence[str] | None = None,
    droppable: Sequence[str] = (),
    derived: Mapping[str, Callable[[Any], Any]] | None = None,
    whole: Sequence[str] = (),
    flexible: Sequence[str] = (),
    footnotes: Mapping[str, str] | None = None,
) -> None:
    """Print a command's result: a table, a key/value panel, or JSON.

    A listing never ends a value in an ellipsis. A column named `id`, `uuid` or ending in
    `_id`, `_at`, `_sha` or `_sha256` is never wrapped while the table can hold it with
    every other column at the width of its header; other values wrap onto more lines, a long
    unbroken one folds inside its own cell. When even that does not fit, everything wraps.

    Parameters
    ----------
    state
        The CLI state (for the output mode).
    value
        A list of dataclasses, one dataclass, or a string. A string is printed as it is, line
        breaks kept (a view's SQL is shown this way), with every control character written as
        an escape (`safe_text`); a name inside it is made one line with `inline` first.
    columns
        The dataclass fields to show in table mode; all fields by default.
    droppable
        Fields of a list that may be left out, least useful first, when the terminal is
        too narrow for every column at its natural width. Each is dropped only if the table
        still does not fit, and a line below the table names what was left out and how to
        see it.
    derived
        Table-mode columns that are not a field of the rows (or of the one dataclass, for a
        single result): the function that reads each from a row. They appear only in
        `columns`; JSON output never has them.
    whole
        More columns that never wrap, next to the identifier, time and digest ones.
    flexible
        Columns of free text, such as a sentence, that wrap well. When columns have to be left
        out, such a column counts as 30 characters wide, not as wide as its longest value, so
        that one long sentence does not take the room of the columns next to it. It is still
        printed whole, wrapped.
    footnotes
        For a list in table mode: a line to print under the table, for each column that is
        shown in it, that explains what the column's cells mean. Never printed for JSON.
    """
    if state.output == "json":
        print_json(value)
        return
    console = _console()
    if isinstance(value, list):
        if not value:
            console.print("(none)")
            return
        names = list(columns or [f.name for f in fields(value[0])])
        read = derived or {}
        hidden: list[str] = []
        unbounded = console.options.update_width(_UNBOUNDED_WIDTH)
        for name in droppable:
            natural = console.measure(
                _list_table(value, names, whole=None, derived=read, flexible=flexible),
                options=unbounded,
            ).maximum
            if natural <= console.width:
                break
            if name in names:
                names.remove(name)
                hidden.append(name)
        kept = [name for name in names if _is_whole(name) or name in whole]
        narrowest = console.measure(
            _list_table(value, names, whole=kept, derived=read, headers_only=True),
            options=unbounded,
        ).maximum
        console.print(
            _list_table(
                value, names, whole=kept if narrowest <= console.width else None, derived=read
            )
        )
        if hidden:
            console.print(Text(_hidden_hint(hidden, terminal=console.is_terminal)), soft_wrap=True)
        for name in names:
            if footnotes and name in footnotes:
                console.print(Text(footnotes[name]), soft_wrap=True)
    elif is_dataclass(value):
        read = derived or {}
        panel = Table(show_header=False)
        panel.add_column("field", style="bold")
        panel.add_column("value")
        for name in columns or [f.name for f in fields(value)]:
            panel.add_row(name, _cell(read[name](value) if name in read else getattr(value, name)))
        console.print(panel)
    else:
        console.print(Text(safe_text(str(value))))


def _hidden_hint(hidden: Sequence[str], *, terminal: bool) -> str:
    """Write the line that says which columns a listing left out.

    Parameters
    ----------
    hidden
        The columns left out.
    terminal
        Whether standard output is a terminal: only then can the reader widen it.

    Returns
    -------
    str
        The line, true for the situation it is printed in.
    """
    names = ", ".join(hidden)
    if terminal:
        return f"Not shown at this width: {names}. Widen the terminal or use --output json."
    pronoun = "it" if len(hidden) == 1 else "them"
    return f"Not shown: {names}. Use --output json to see {pronoun}."


def emit_arrow(state: CliState, table: pa.Table, max_rows: int) -> None:
    """Print a query result: capped Rich table, or every row as JSON objects.

    Parameters
    ----------
    state
        The CLI state.
    table
        The result.
    max_rows
        The most rows table mode shows.
    """
    if state.output == "json":
        print_json(table.to_pylist())
        return
    shown = table.slice(0, max_rows)
    rendered = Table()
    for name in table.column_names:
        rendered.add_column(Text(name))
    for row in zip(*(column.to_pylist() for column in shown.columns), strict=True):
        rendered.add_row(*(_cell(v) for v in row))
    console = _console()
    console.print(rendered)
    hidden = table.num_rows - shown.num_rows
    if hidden > 0:
        console.print(f"… {hidden} more rows not shown (use --max-rows or --out)")
    console.print(f"{table.num_rows} rows")


def emit_text_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    """Print rows of text as a table; no control character reaches the terminal.

    Parameters
    ----------
    headers
        The column names.
    rows
        The rows, each one string per column. Text is printed literally: Rich markup is never
        read, and every control character is written as an escape (`safe_text`).
    """
    rendered = Table()
    for header in headers:
        rendered.add_column(Text(safe_text(header)))
    for row in rows:
        rendered.add_row(*(Text(safe_text(cell)) for cell in row))
    _console().print(rendered)


def done(state: CliState, message: str, value: Any = None) -> None:  # ruff: ignore[any-type]
    """Report a completed mutation.

    Parameters
    ----------
    state
        The CLI state.
    message
        The confirmation for table mode. Put each name in it through `inline` first: the whole
        text then has every control character written as an escape (`safe_text`), line breaks
        kept for a message that has more than one line, and text that `inline` has escaped is
        left as it is, so nothing is escaped twice.
    value
        The SDK result to print in JSON mode; `{"status": "ok", "message": ...}`
        when there is none.
    """
    if state.output == "json":
        print_json(value if value is not None else {"status": "ok", "message": message})
    else:
        sys.stdout.write(safe_text(message) + "\n")


def print_secret(state: CliState, label: str, secret: str) -> None:
    """Print a newly issued secret exactly once, with the notice on stderr.

    Written with `sys.stdout.write`, not Rich, so the value is never
    wrapped across lines.

    Parameters
    ----------
    state
        The CLI state.
    label
        What the secret is, e.g. `"Token"`.
    secret
        The full value.
    """
    if state.output != "json":
        sys.stdout.write(f"{label}: {secret}\n")
    sys.stderr.write(f"Store this {label.lower()} now: it can't be shown again.\n")


def next_step(state: CliState, text: str) -> None:
    """Tell the reader what to do after a command, on stderr, in table mode only.

    Parameters
    ----------
    state
        The CLI state.
    text
        The sentence; put each name in it through `inline` first.
    """
    if state.output != "json":
        sys.stderr.write(safe_text(text) + "\n")


def error(state: CliState, kind: str, message: str, hint: str | None) -> None:
    """Report a failure on stderr.

    Parameters
    ----------
    state
        The CLI state.
    kind
        The error type name reported in JSON mode.
    message
        What went wrong.
    hint
        An optional follow-up suggestion.
    """
    if state.output == "json":
        sys.stderr.write(json.dumps({"error": {"type": kind, "message": message}}) + "\n")
        return
    console = _console(stderr=True)
    console.print(Text.assemble(("Error: ", "red"), message))
    if hint:
        console.print(Text(f"Hint: {hint}"))
