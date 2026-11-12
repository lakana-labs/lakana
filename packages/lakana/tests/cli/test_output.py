"""Tests for CLI output rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path

import pyarrow as pa
import pytest
from conftest import FakeApi, make_state
from rich.cells import cell_len

from lakana._models import Environment, TaskTokenScope, Whoami
from lakana.cli import _output
from lakana.cli._state import CliState


@dataclass(frozen=True)
class _Row:
    """A test row.

    Parameters
    ----------
    name
        A name.
    when
        A timestamp.
    """

    name: str
    when: datetime


def _state(tmp_path: Path, output: str) -> CliState:
    """Build a state in the given output mode.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    output
        `"table"` or `"json"`.

    Returns
    -------
    CliState
        The state.
    """
    state = make_state(tmp_path, FakeApi())
    state.output = output
    return state


def test_to_jsonable_converts_nested_values() -> None:
    """Dataclasses, datetimes, decimals and bytes become plain JSON values."""
    value = {"row": _Row("a", datetime(2026, 1, 1, tzinfo=UTC)), "d": Decimal("1.50"), "b": b"\x00"}

    assert _output.to_jsonable(value) == {
        "row": {"name": "a", "when": "2026-01-01T00:00:00+00:00"},
        "d": "1.50",
        "b": "AA==",
    }


def test_emit_json_lists_dataclasses_with_their_field_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON mode prints the SDK dataclass fields verbatim."""
    env = Environment(
        name="dev",
        storage_root="file:///d",
        storage_secret_ref=None,
        is_protected=False,
        properties={},
    )

    _output.emit(_state(tmp_path, "json"), [env])

    assert json.loads(capsys.readouterr().out) == [
        {
            "name": "dev",
            "storage_root": "file:///d",
            "storage_secret_ref": None,
            "is_protected": False,
            "properties": {},
            "retention_days": None,
            "deleted_at": None,
            "purge_after": None,
        }
    ]


def test_emit_table_renders_markup_literally(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A value containing Rich markup is printed as text, never interpreted."""
    _output.emit(_state(tmp_path, "table"), [_Row("[red]x[/red]", datetime(2026, 1, 1))])

    assert "[red]x[/red]" in capsys.readouterr().out


@dataclass(frozen=True)
class _Wide:
    """A row with two long values and two short ones.

    Parameters
    ----------
    key
        A short key.
    stamp
        A long timestamp.
    note
        A short note.
    ident
        A long identifier.
    """

    key: int
    stamp: str
    note: str
    ident: str


_ROW = _Wide(1, "2026-10-04T12:00:00.123456Z", "n", "3f2c9d1e-8b7a-4c55-9e0a-1b2c3d4e5f60")


@pytest.mark.parametrize("width", [40, 70])
def test_emit_table_folds_a_long_value_instead_of_cutting_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    """A value wider than its column wraps; no listing ends a value in an ellipsis."""
    monkeypatch.setenv("COLUMNS", str(width))
    _output.emit(_state(tmp_path, "table"), [_ROW])

    out = capsys.readouterr().out
    assert "…" not in out
    cells = [line.split("│")[1:-1] for line in out.splitlines() if line.startswith("│")]
    assert "".join(cell[1].strip() for cell in cells) == _ROW.stamp
    assert "".join(cell[3].strip() for cell in cells) == _ROW.ident


def test_emit_table_drops_the_named_columns_only_when_the_terminal_is_too_narrow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Droppable columns go one at a time, in order, and the output says which."""
    state = _state(tmp_path, "table")
    monkeypatch.setenv("COLUMNS", "200")
    _output.emit(state, [_ROW], droppable=["note", "key"])
    wide = capsys.readouterr().out
    assert "note" in wide
    assert "Not shown" not in wide

    monkeypatch.setenv("COLUMNS", "72")
    _output.emit(state, [_ROW], droppable=["missing", "note", "key"])
    narrow = capsys.readouterr().out
    assert _ROW.stamp in narrow
    assert _ROW.ident in narrow
    header = narrow.splitlines()[1]
    assert "key" not in header
    assert "note" not in header
    said = "".join(narrow.partition("Not shown")[1:]).split()
    assert " ".join(said) == "Not shown: note, key. Use --output json to see them."

    monkeypatch.setenv("COLUMNS", "78")
    _output.emit(state, [_ROW], droppable=["note", "key"])
    middle = capsys.readouterr().out
    assert "key" in middle.splitlines()[1]
    assert "note" not in middle.splitlines()[1]
    assert "Not shown: note. Use --output json to see it." in middle


def test_emit_table_says_none_for_an_empty_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty listing prints `(none)` rather than an empty frame."""
    _output.emit(_state(tmp_path, "table"), [])

    assert "(none)" in capsys.readouterr().out


def test_done_prints_a_message_or_a_status_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mutation with no result object prints `{"status": "ok", "message": ...}` in JSON mode."""
    _output.done(_state(tmp_path, "table"), "Deleted dev")
    assert capsys.readouterr().out == "Deleted dev\n"

    _output.done(_state(tmp_path, "json"), "Deleted dev")
    assert json.loads(capsys.readouterr().out) == {"status": "ok", "message": "Deleted dev"}


def test_json_output_never_wraps_long_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review focus 5: JSON bypasses Rich, so a 500-character value survives intact."""
    long = "x" * 500

    _output.emit(_state(tmp_path, "json"), _Row(long, datetime(2026, 1, 1)))

    out = capsys.readouterr().out
    assert json.loads(out)["name"] == long
    assert "\x1b[" not in out


def test_emit_arrow_caps_rows_and_counts_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Table mode shows at most `max_rows` rows and says how many were hidden."""
    table = pa.table({"id": list(range(5))})

    _output.emit_arrow(_state(tmp_path, "table"), table, max_rows=2)

    out = capsys.readouterr().out
    assert "3 more rows not shown" in out
    assert "5 rows" in out


def test_emit_arrow_shows_no_hidden_count_when_nothing_is_hidden(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When `max_rows` covers every row, no "more rows" line is printed."""
    _output.emit_arrow(_state(tmp_path, "table"), pa.table({"id": [1, 2]}), max_rows=5)

    out = capsys.readouterr().out
    assert "more rows not shown" not in out
    assert "2 rows" in out


def test_emit_arrow_table_mode_renders_a_bracketed_column_name_literally(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A query result column name containing Rich markup is printed as text.

    Column aliases come straight from the SQL a caller submitted (e.g.
    `SELECT 1 AS "[red]x[/red]"`), so `Table` must never interpret them
    as markup.
    """
    _output.emit_arrow(_state(tmp_path, "table"), pa.table({"[red]x[/red]": [1]}), max_rows=5)

    assert "[red]x[/red]" in capsys.readouterr().out


def test_emit_arrow_json_prints_every_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON mode prints all rows as objects, ignoring the display cap."""
    _output.emit_arrow(_state(tmp_path, "json"), pa.table({"id": [1, 2, 3]}), max_rows=1)

    assert json.loads(capsys.readouterr().out) == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_print_secret_writes_the_value_once_and_the_notice_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Table mode prints the secret on its own line; the warning goes to stderr."""
    _output.print_secret(_state(tmp_path, "table"), "Token", "lakana_pat_k_abc")

    captured = capsys.readouterr()
    assert captured.out.count("lakana_pat_k_abc") == 1
    assert "can't be shown again" in captured.err


def test_print_secret_in_json_mode_only_writes_the_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON mode skips the stdout write; the caller prints the secret itself."""
    _output.print_secret(_state(tmp_path, "json"), "Token", "lakana_pat_k_abc")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "can't be shown again" in captured.err


class _Color(Enum):
    """A plain (non-`str`) enum, for `to_jsonable`'s enum branch.

    Parameters
    ----------
    RED
        The only member.
    """

    RED = "red"


def test_to_jsonable_converts_an_enum() -> None:
    """An enum member becomes its `.value`."""
    assert _output.to_jsonable(_Color.RED) == "red"


def test_cell_renders_none_as_an_empty_string() -> None:
    """A `None` field renders as an empty table cell, not the string `None`."""
    assert _output._cell(None).plain == ""


def test_cell_renders_dicts_and_lists_as_compact_json() -> None:
    """A dict or list field is rendered as compact JSON text, not Python repr."""
    assert _output._cell({"a": 1}).plain == '{"a": 1}'
    assert _output._cell([1, 2]).plain == "[1, 2]"


def test_emit_a_single_dataclass_renders_a_key_value_panel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A single dataclass (not a list) renders as a field/value panel in table mode."""
    _output.emit(_state(tmp_path, "table"), _Row("solo", datetime(2026, 1, 1)))

    out = capsys.readouterr().out
    assert "name" in out
    assert "solo" in out


def test_emit_a_plain_value_prints_it_as_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A value that is neither a list nor a dataclass is printed as plain text."""
    _output.emit(_state(tmp_path, "table"), "just a string")

    assert "just a string" in capsys.readouterr().out


def test_cell_renders_a_nested_dataclass_as_json() -> None:
    """Final review minor 4: a dataclass inside a value renders as JSON, not a Python repr."""
    scope = TaskTokenScope(environment="dev", asset_scope=["gold.*"])

    assert _output._cell(scope).plain == '{"environment": "dev", "asset_scope": ["gold.*"]}'


def test_whoami_table_mode_renders_the_task_token_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review minor 4: whoami's `task_token` field is JSON in the key/value panel."""
    who = Whoami(
        name="bot",
        type="SERVICE_ACCOUNT",
        task_token=TaskTokenScope(environment="dev", asset_scope=["gold.*"]),
    )

    _output.emit(_state(tmp_path, "table"), who)

    out = capsys.readouterr().out
    assert '"environment": "dev"' in out
    assert "TaskTokenScope(" not in out
    assert "'environment'" not in out


def test_emit_arrow_json_writes_non_finite_floats_as_null(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Final review minor 5: NaN and ±inf become `null`, so stdout is strict JSON."""
    table = pa.table({"x": pa.array([1.5, float("nan"), float("inf"), float("-inf")])})

    _output.emit_arrow(_state(tmp_path, "json"), table, max_rows=50)

    out = capsys.readouterr().out
    assert json.loads(out, parse_constant=lambda c: pytest.fail(f"non-JSON constant {c}")) == [
        {"x": 1.5},
        {"x": None},
        {"x": None},
        {"x": None},
    ]


def test_print_json_refuses_to_emit_invalid_json(capsys: pytest.CaptureFixture[str]) -> None:
    """`allow_nan=False` is the safety net: a non-finite float never reaches stdout as `NaN`."""
    _output.print_json({"nested": [{"x": float("nan")}]})

    assert json.loads(capsys.readouterr().out) == {"nested": [{"x": None}]}


def test_emit_arrow_table_mode_still_shows_non_finite_floats(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only JSON needs `null`; a table cell shows `nan`/`inf` as they are."""
    table = pa.table({"x": pa.array([float("nan"), float("-inf")])})

    _output.emit_arrow(_state(tmp_path, "table"), table, max_rows=50)

    out = capsys.readouterr().out
    assert "nan" in out
    assert "-inf" in out
    assert "None" not in out


@dataclass(frozen=True)
class _Item:
    """A listing row with the kinds of value a real listing has.

    Parameters
    ----------
    id
        A 36-character identifier.
    repository
        A name, here with a non-ASCII character.
    title
        Free text.
    created_at
        A timestamp with an offset.
    """

    id: str
    repository: str
    title: str
    created_at: str


_ITEM = _Item(
    "3f2c9d1e-8b7a-4c55-9e0a-1b2c3d4e5f60",
    "daten-zürich",
    "a long free text that can wrap onto a second line without losing a word",
    "2026-10-04T12:00:00.123456+00:00",
)


def _terminal(width: int, *, terminal: bool) -> _output.Console:
    """Build the console `emit` should use.

    Parameters
    ----------
    width
        Its width in columns.
    terminal
        Whether it counts as a terminal.

    Returns
    -------
    Console
        A console without styles that writes to the captured stdout.
    """
    return _output.Console(
        file=_output.sys.stdout,
        width=width,
        force_terminal=terminal,
        color_system=None,
        highlight=False,
    )


@pytest.mark.parametrize("width", [110, 120])
def test_an_id_and_a_time_stay_whole_while_free_text_wraps(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    width: int,
) -> None:
    """Columns named `id` or `*_at` never wrap or fold; the free text gives way."""
    monkeypatch.setattr(_output, "_console", lambda stderr=False: _terminal(width, terminal=True))
    _output.emit(_state(tmp_path, "table"), [_ITEM, _ITEM])
    out = capsys.readouterr().out
    assert out.count(_ITEM.id) == 2
    assert out.count(_ITEM.created_at) == 2
    assert "…" not in out
    header = out.splitlines()[1]
    for word in ("id", "repository", "title", "created_at"):
        assert word in header
    assert "Not shown" not in out


def test_a_terminal_is_told_to_widen_it_and_a_pipe_is_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where there is no terminal to widen, the hint names only `--output json`."""
    state = _state(tmp_path, "table")
    monkeypatch.setattr(_output, "_console", lambda stderr=False: _terminal(80, terminal=True))
    _output.emit(state, [_ITEM], droppable=["title", "repository"])
    on_terminal = capsys.readouterr().out
    assert (
        "Not shown at this width: title, repository. Widen the terminal or use --output json."
        in " ".join(on_terminal.split())
    )

    monkeypatch.setattr(_output, "_console", lambda stderr=False: _terminal(80, terminal=False))
    _output.emit(state, [_ITEM], droppable=["title", "repository"])
    piped = " ".join(capsys.readouterr().out.split())
    assert "Not shown: title, repository. Use --output json to see them." in piped
    assert "Widen" not in piped


def test_a_table_that_cannot_fit_even_without_free_text_folds_rather_than_cuts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrower than an id: the id folds, never ends in an ellipsis or a silent cut."""
    monkeypatch.setattr(_output, "_console", lambda stderr=False: _terminal(30, terminal=True))
    _output.emit(_state(tmp_path, "table"), [_ITEM])
    out = capsys.readouterr().out
    assert "…" not in out
    cells = [line.split("│")[1:-1] for line in out.splitlines() if line.startswith("│")]
    assert "".join(cell[0].strip() for cell in cells) == _ITEM.id


def test_a_header_is_never_cut_in_the_middle_of_a_word(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A column squeezed by a long id still shows its whole header."""
    monkeypatch.setattr(_output, "_console", lambda stderr=False: _terminal(100, terminal=True))
    _output.emit(_state(tmp_path, "table"), [_ITEM])
    header = capsys.readouterr().out.splitlines()[1]
    for word in ("id", "repository", "title", "created_at"):
        assert word in header


@dataclass(frozen=True)
class _Named:
    """A row with an identifier, a free name and a time.

    Parameters
    ----------
    id
        A 36-character identifier.
    name
        A name that may hold no space at all.
    created_at
        A timestamp with an offset.
    """

    id: str
    name: str
    created_at: str


@pytest.mark.parametrize("name", ["a" * 60, "ü" * 30 + "日本語" * 10], ids=["ascii", "non-ascii"])
def test_a_long_unbroken_value_folds_in_its_own_cell_not_in_the_id_or_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """One 60-character word must not make the id and the time fold."""
    monkeypatch.setattr(_output, "_console", lambda stderr=False: _terminal(80, terminal=True))
    item = _Named(_ITEM.id, name, "2026-10-04T12:00:00+00:00")
    _output.emit(_state(tmp_path, "table"), [item])
    out = capsys.readouterr().out
    assert out.count(item.id) == 1
    assert out.count(item.created_at) == 1
    assert "…" not in out
    assert all(cell_len(line) <= 80 for line in out.splitlines())
    cells = [line.split("│")[1:-1] for line in out.splitlines() if line.startswith("│")]
    assert len(cells) > 1
    assert "".join(cell[1].strip() for cell in cells) == name


def test_a_control_character_in_a_cell_is_shown_as_an_escape() -> None:
    """An escape or a bidirectional override never reaches the terminal raw."""
    assert _output._cell("a\x1b[31mb\u202e").plain == "a\\x1b[31mb\\u202e"
    assert _output._cell("x\ny\tz\x7f\x9b").plain == "x\ny    z\\x7f\\x9b"
    assert _output._cell("x\x0b\x0c\x85y").plain == "x\\x0b\\x0c\\x85y"
    assert _output._cell("\u061c\u200e\u200f\u2066\u2069").plain == (
        "\\u061c\\u200e\\u200f\\u2066\\u2069"
    )
    assert _output._cell({"k": "\u202e"}).plain == '{"k": "\\u202e"}'
    assert _output._cell("Zürich 日本 é").plain == "Zürich 日本 é"


def test_a_line_break_is_kept_and_a_tab_becomes_spaces() -> None:
    """A task's two-line error reads as two lines; a Windows or old-Mac break is one break."""
    assert _output._cell("one\ntwo").plain == "one\ntwo"
    assert _output._cell("one\r\ntwo\rthree").plain == "one\ntwo\nthree"
    assert _output._cell("a\tb").plain == "a    b"
    assert _output._cell("a\n\x1b[2Jb").plain == "a\n\\x1b[2Jb"
    assert _output._cell("a\r\x1b[2Jb").plain == "a\n\\x1b[2Jb"
    assert _output._cell({"k": "a\nb"}).plain == '{"k": "a\\nb"}'


def test_a_line_break_in_a_name_stays_inside_the_cell_of_its_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row's other cells stay on the row's first line; no second row, no raw ESC."""
    monkeypatch.setattr(_output, "_console", lambda stderr=False: _terminal(120, terminal=True))
    item = _Named(_ITEM.id, "first\n\x1b[2J| fake | row |", "2026-10-04T12:00:00+00:00")
    other = _Named("b" * 36, "second", "2026-10-05T12:00:00+00:00")
    _output.emit(_state(tmp_path, "table"), [item, other])
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\\x1b[2J| fake | row |" in out
    lines = out.splitlines()
    rule = next(line for line in lines if line.startswith("┡"))
    borders = [i for i, char in enumerate(rule) if char in "┡╇┩"]
    body = [line for line in lines if line.startswith("│")]
    assert len(body) == 3  # the first row has two lines, the second one
    for line in body:  # every line keeps its borders where the header's are
        assert [line[i] for i in borders] == ["│"] * len(borders)
    assert item.id in body[0] and item.created_at in body[0] and "first" in body[0]
    assert item.id not in body[1] and item.created_at not in body[1]
    assert "fake" in body[1]
    assert other.id in body[2] and "second" in body[2]


def test_a_table_never_prints_a_raw_control_character(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing and a key/value panel carry the escape, not the character."""
    monkeypatch.setattr(_output, "_console", lambda stderr=False: _terminal(200, terminal=True))
    item = _Named(_ITEM.id, "a\x1b[31mb\u202e", "2026-10-04T12:00:00+00:00")
    _output.emit(_state(tmp_path, "table"), [item])
    _output.emit(_state(tmp_path, "table"), item)
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\u202e" not in out
    assert out.count("a\\x1b[31mb") >= 2


def test_json_output_of_a_control_character_is_valid_json_without_a_raw_escape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON escapes the control character itself; it is not touched here."""
    item = _Named(_ITEM.id, "a\x1b[31mb", "2026-10-04T12:00:00+00:00")
    _output.emit(_state(tmp_path, "json"), [item])
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert json.loads(out)[0]["name"] == "a\x1b[31mb"


def test_a_run_of_blank_lines_in_a_cell_is_one_line_break() -> None:
    r"""`a\n\n\n\nb` shows as two lines, and a line of spaces only counts as blank."""
    assert _output._cell("a\n\n\n\nb").plain == "a\nb"
    assert _output._cell("a\r\n\r\n\r\nb").plain == "a\nb"
    assert _output._cell("a\n  \n\t\nb").plain == "a\nb"
    assert _output._cell("a\n  indented\nb").plain == "a\n  indented\nb"


def test_line_breaks_at_the_start_and_end_of_a_cell_are_stripped() -> None:
    """A value is not shown with an empty line above or below it."""
    assert _output._cell("\n\n a\nb\n\n").plain == " a\nb"
    assert _output._cell("\n\n\n").plain == ""


def test_a_cell_is_capped_at_fifty_lines_with_the_true_count_of_the_rest() -> None:
    """Beyond 50 lines the rest is one last line that names how many there are."""
    fifty = "\n".join(f"l{i}" for i in range(50))
    assert _output._cell(fifty).plain == fifty
    shown = _output._cell("\n".join(f"l{i}" for i in range(120))).plain.split("\n")
    assert shown[:50] == [f"l{i}" for i in range(50)]
    assert shown[50:] == ["… (70 more lines; use --output json to see all)"]
    one_more = _output._cell("\n".join(f"l{i}" for i in range(51))).plain.split("\n")
    assert one_more[-1] == "… (1 more lines; use --output json to see all)"


def test_a_value_of_five_thousand_line_breaks_is_one_line() -> None:
    """Thousands of line breaks, with text around them or not, do not make thousands of lines."""
    assert _output._cell("a" + "\n" * 5000 + "b").plain == "a\nb"
    assert _output._cell("\n" * 5000).plain == ""
    assert _output._cell("\n \n" * 5000).plain == ""


def test_a_normal_multi_line_task_error_is_unchanged() -> None:
    """Two or three lines of a traceback tail read as they were written."""
    error = "ValueError: bad input\n  in model.py, line 3\nCaused by: KeyError('x')"
    assert _output._cell(error).plain == error


def test_json_output_keeps_every_line_break(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cap is for tables only: JSON carries the whole value."""
    text = "a" + "\n" * 60 + "b"
    env = Environment(
        name="dev",
        storage_root="file:///d",
        storage_secret_ref=None,
        is_protected=False,
        properties={"note": text},
    )

    _output.emit(_state(tmp_path, "json"), [env])

    assert json.loads(capsys.readouterr().out)[0]["properties"] == {"note": text}


_HOSTILE = "a\x1b[31mb\n2026 ERROR forged"


def test_done_writes_no_raw_control_character_and_keeps_its_own_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A confirmation is escaped as a whole; a message of several lines keeps them."""
    _output.done(_state(tmp_path, "table"), f"Deleted {_HOSTILE}\nKept 2 files.")
    out = capsys.readouterr().out
    assert out == "Deleted a\\x1b[31mb\n2026 ERROR forged\nKept 2 files.\n"
    _output.done(_state(tmp_path, "table"), "tab\tand\r\nbreak\x07")
    assert capsys.readouterr().out == "tab    and\nbreak\\x07\n"


def test_a_name_put_through_inline_is_escaped_once_by_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`inline` first and `done` after is one escaping, not two."""
    _output.done(_state(tmp_path, "table"), f"Deleted {_output.inline(_HOSTILE)}")
    assert capsys.readouterr().out == "Deleted a\\x1b[31mb\\n2026 ERROR forged\n"


def test_done_in_json_mode_keeps_its_message_as_the_caller_built_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message is the human sentence (names through `inline`); data fields are untouched."""
    message = f"Deleted {_output.inline(_HOSTILE)}"
    _output.done(_state(tmp_path, "json"), message)
    assert json.loads(capsys.readouterr().out)["message"] == message
    assert "\x1b" not in message and "\n" not in message.replace("\\n", "")
    item = {"name": _HOSTILE}
    _output.done(_state(tmp_path, "json"), message, item)
    assert json.loads(capsys.readouterr().out) == item


def test_emit_of_a_plain_string_escapes_controls_and_keeps_the_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A view's SQL has several lines; an escape sequence in it never reaches a terminal."""
    _output.emit(_state(tmp_path, "table"), "select 1\n  from t -- \x1b[2J\n\n")
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert out.startswith("select 1\n  from t -- \\x1b[2J\n")


def test_a_line_or_paragraph_separator_is_escaped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = f"a{chr(0x2028)}b{chr(0x2029)}c"
    assert _output.inline(text) == "a\\u2028b\\u2029c"
    _output.done(_state(tmp_path, "table"), text)
    assert capsys.readouterr().out == "a\\u2028b\\u2029c\n"


def test_a_flexible_column_does_not_take_the_room_of_the_columns_next_to_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long sentence counts as 30 characters when columns are left out, and prints whole."""
    state = _state(tmp_path, "table")
    monkeypatch.setattr(_output, "_console", lambda stderr=False: _terminal(100, terminal=False))
    _output.emit(state, [_ITEM], columns=["id", "repository", "title"], droppable=["repository"])
    plain = capsys.readouterr().out
    assert "repository" not in plain.splitlines()[1]
    assert "Not shown: repository." in plain

    _output.emit(
        state,
        [_ITEM],
        columns=["id", "repository", "title"],
        droppable=["repository"],
        flexible=["title"],
    )
    flexible = capsys.readouterr().out
    assert "repository" in flexible.splitlines()[1]
    assert "Not shown" not in flexible
    text = " ".join(" ".join(line.strip("│ ") for line in flexible.splitlines()[3:-1]).split())
    assert text.endswith("without losing a word")


def test_safe_source_escapes_ascii_controls_but_keeps_tab_and_line_feed() -> None:
    text = "".join(chr(c) for c in range(0x20)) + "\x7f"
    expected = (
        "".join(chr(c) if c in (0x09, 0x0A) else f"\\x{c:02x}" for c in range(0x20)) + "\\x7f"
    )
    assert _output.safe_source(text) == expected


def test_safe_source_escapes_c1_and_format_characters_and_separators() -> None:
    text = "\x80\x9b\x9f\u00ad\u061c\u200b\u200e\u202a\u202e\u2028\u2029\u2066\u2069\ufeff"
    assert _output.safe_source(text) == (
        "\\x80\\x9b\\x9f\\xad\\u061c\\u200b\\u200e\\u202a\\u202e\\u2028\\u2029\\u2066\\u2069\\ufeff"
    )


def test_safe_source_escapes_a_lone_surrogate() -> None:
    assert _output.safe_source("a\ud800b") == "a\\ud800b"


def test_safe_source_keeps_printable_text_tabs_and_lines_exactly() -> None:
    text = "SELECT\t'\u00e9\u20ac\u65e5\u672c \U0001f469 [bold]x[/bold]'\n\n  FROM x  \n"
    assert _output.safe_source(text) == text


def test_safe_source_escapes_a_zero_width_joiner_and_keeps_the_emoji() -> None:
    assert _output.safe_source("\U0001f469\u200d\U0001f4bb") == "\U0001f469\\u200d\U0001f4bb"
