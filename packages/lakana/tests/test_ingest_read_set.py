"""`lakana.ingest._read_set`: the files an incremental `files.read` may record."""

from __future__ import annotations

import datetime as dt

import pytest

from lakana import context
from lakana.ingest import FileShapeError, _read_set
from lakana.ingest._directory import FileEntry

_MARK = dt.datetime(2026, 11, 2, 12, tzinfo=dt.UTC)
_NARROW = "narrow the pattern the asset passes to files.read so that it does not match"


def _state(**fields: object) -> context._TaskState:
    """Build a task state without a client.

    Parameters
    ----------
    **fields
        Fields to set besides the required ones.

    Returns
    -------
    _TaskState
        The state.
    """
    return context._TaskState(
        client=object(),  # ty: ignore[invalid-argument-type]
        environment="dev",
        asset="bronze.orders",
        declared=frozenset({"exports"}),
        connections={},
        **fields,  # ty: ignore[invalid-argument-type]
    )


def _file(name: str, modified: dt.datetime = _MARK) -> context.ReadFile:
    """Describe one file read.

    Parameters
    ----------
    name
        Its name.
    modified
        Its time.

    Returns
    -------
    ReadFile
        The file of `exports`.
    """
    return context.ReadFile("exports", name, 1, modified)


def test_a_file_the_record_can_hold_passes() -> None:
    _read_set.check_storable("exports", [_file("a" * 1024), _file("naïve-日本.csv")])


def test_a_name_longer_than_the_record_takes_is_refused() -> None:
    with pytest.raises(FileShapeError) as caught:
        _read_set.check_storable("exports", [_file("a" * 1025)])
    assert str(caught.value) == (
        "`exports`: names a file whose path is longer than 1024 characters (beginning "
        f"{'a' * 64!r}); move it to a shorter path, or {_NARROW}"
    )


def test_a_nul_in_a_name_is_refused_escaped() -> None:
    with pytest.raises(FileShapeError) as caught:
        _read_set.check_storable("exports", [_file("a\x00b.csv")])
    assert str(caught.value) == (
        "`exports`: names a file Lakana cannot store ('a\\x00b.csv'): its name holds a NUL "
        "character or is not valid UTF-8; rename the file; Lakana stores file names as UTF-8 text"
    )


def test_a_name_that_was_not_utf8_is_refused_escaped() -> None:
    """A non-UTF-8 local name arrives as a lone surrogate (`os.fsdecode`); on every platform."""
    with pytest.raises(FileShapeError) as caught:
        _read_set.check_storable("exports", [_file("bad\udcff.csv")])
    assert str(caught.value) == (
        "`exports`: names a file Lakana cannot store ('bad\\udcff.csv'): its name holds a NUL "
        "character or is not valid UTF-8; rename the file; Lakana stores file names as UTF-8 text"
    )


@pytest.mark.parametrize(
    "modified",
    [
        dt.datetime.min.replace(tzinfo=dt.UTC) + dt.timedelta(days=29),
        dt.datetime.max.replace(tzinfo=dt.UTC) - dt.timedelta(days=29),
    ],
)
def test_a_time_near_the_limits_is_refused(modified: dt.datetime) -> None:
    with pytest.raises(FileShapeError) as caught:
        _read_set.check_storable("exports", [_file("a.csv", modified)])
    assert str(caught.value) == (
        f"`exports`: gives 'a.csv' the time {modified.isoformat()}, within 30 days of the "
        "earliest or latest time Lakana can store (years 1 and 9999); correct the file's "
        f"modification time (or the date in its name), or {_NARROW}"
    )


def test_a_window_reaching_before_year_one_does_not_overflow() -> None:
    """A `since` or a watermark days after year 1 opens the window at the earliest time."""
    early = dt.datetime.min.replace(tzinfo=dt.UTC) + dt.timedelta(days=1)
    entry = FileEntry("a.csv", 1, early)
    state = _state(recorded=frozenset(), target_watermark=early)
    choice = _read_set.choose(
        "exports", [(entry, early)], after=early, lookback=dt.timedelta(days=30), state=state
    )
    assert (choice.chosen, choice.already, choice.inside) == ([], 1, 1)
    assert choice.reported == [context.ReadFile("exports", "a.csv", 1, early)]


def _busy(days: int, per_day: int) -> list[tuple[FileEntry, dt.datetime]]:
    """List a busy source's files before `_MARK`, newest first.

    Parameters
    ----------
    days
        How many days back.
    per_day
        Files a day.

    Returns
    -------
    list of tuple
        Each file with its time.
    """
    step = dt.timedelta(days=1) / per_day
    timed = []
    for n in range(days * per_day):
        modified = _MARK - n * step
        timed.append((FileEntry(f"f{n}.csv", 10, modified), modified))
    return timed


def test_seeding_without_a_lookback_records_no_old_file() -> None:
    """Seeding takes every old file as read, but records only the call's own window."""
    state = _state(recorded=frozenset(), target_watermark=_MARK)
    choice = _read_set.choose(
        "exports", _busy(40, 4000), after=_MARK, lookback=dt.timedelta(0), state=state
    )
    assert (len(choice.chosen), len(choice.reported), choice.inside) == (0, 0, 0)


def test_seeding_a_busy_source_records_only_the_last_day() -> None:
    """4,000 files a day for 30 days: the default 24 h lookback records one day of them."""
    state = _state(recorded=frozenset(), target_watermark=_MARK)
    choice = _read_set.choose(
        "exports", _busy(30, 4000), after=_MARK, lookback=_read_set.DEFAULT_LOOKBACK, state=state
    )
    assert choice.chosen == []
    assert len(choice.reported) == 4000
    assert min(f.modified for f in choice.reported) > _MARK - dt.timedelta(hours=24)


def test_a_horizon_takes_every_older_file_as_read() -> None:
    """A busy record loaded only in part: files at or before its horizon are not read again."""
    horizon = _MARK - dt.timedelta(hours=2)
    late, older, new = (
        FileEntry("late.csv", 1, _MARK - dt.timedelta(hours=1)),
        FileEntry("older.csv", 1, _MARK - dt.timedelta(hours=3)),
        FileEntry("new.csv", 1, _MARK + dt.timedelta(hours=1)),
    )
    state = _state(
        recorded=frozenset({_file("x.csv")}), target_watermark=_MARK, record_horizon=horizon
    )
    choice = _read_set.choose(
        "exports",
        [(f, f.modified) for f in (late, older, new) if f.modified is not None],
        after=_MARK,
        lookback=_read_set.DEFAULT_LOOKBACK,
        state=state,
    )
    assert [entry.path for entry, _ in choice.chosen] == ["late.csv", "new.csv"]
    assert (choice.late, choice.new, choice.inside) == (1, 1, 2)


def test_the_log_line_names_the_files_taken_as_read_at_the_horizon() -> None:
    """Strict over silent: files skipped because of the horizon are counted, with the reason."""
    horizon = _MARK - dt.timedelta(hours=2)
    entries = [
        FileEntry("older.csv", 1, _MARK - dt.timedelta(hours=3)),
        FileEntry("oldest.csv", 1, _MARK - dt.timedelta(hours=30)),
        FileEntry("new.csv", 1, _MARK + dt.timedelta(hours=1)),
    ]
    state = _state(
        recorded=frozenset({_file("x.csv")}), target_watermark=_MARK, record_horizon=horizon
    )
    choice = _read_set.choose(
        "exports",
        [(f, f.modified) for f in entries if f.modified is not None],
        after=_MARK,
        lookback=_read_set.DEFAULT_LOOKBACK,
        state=state,
    )
    assert choice.horizon_skipped == 1
    assert _read_set.summary("exports", choice, _read_set.DEFAULT_LOOKBACK) == (
        "files.read: `exports`: 1 new file, 0 late files inside the 24 h lookback, 0 already "
        f"read, 1 file at or before the record's horizon {horizon.isoformat()} taken as read "
        "(the table's record of files read is full); see 'A busy table'"
    )


def test_a_horizon_later_than_since_still_takes_older_files_as_read() -> None:
    """A recorded file the worker did not load (before the horizon) is never read again."""
    horizon = _MARK - dt.timedelta(minutes=30)
    entries = [
        FileEntry("old_read.csv", 1, _MARK - dt.timedelta(hours=1)),
        FileEntry("a.csv", 1, horizon),
        FileEntry("new.csv", 1, _MARK + dt.timedelta(hours=1)),
    ]
    state = _state(
        recorded=frozenset({_file("a.csv", horizon)}),
        target_watermark=_MARK,
        record_horizon=horizon,
    )
    choice = _read_set.choose(
        "exports",
        [(f, f.modified) for f in entries if f.modified is not None],
        after=_MARK - dt.timedelta(hours=2),
        lookback=_read_set.DEFAULT_LOOKBACK,
        state=state,
    )
    assert [entry.path for entry, _ in choice.chosen] == ["new.csv"]
    assert choice.horizon_skipped == 2


def test_a_quiet_connection_below_the_horizon_is_not_taken_as_new() -> None:
    """The worker names every connection with a row in the window: its late files are read."""
    horizon = _MARK - dt.timedelta(days=1)
    late = FileEntry("ylate.csv", 1, _MARK - dt.timedelta(hours=3))
    state = _state(
        recorded=frozenset({_file("x.csv")}),
        target_watermark=_MARK,
        record_horizon=horizon,
        recorded_connections=frozenset({"exports", "quiet"}),
    )
    choice = _read_set.choose(
        "quiet",
        [(late, _MARK - dt.timedelta(hours=3))],
        after=_MARK,
        lookback=_read_set.DEFAULT_LOOKBACK,
        state=state,
    )
    assert [entry.path for entry, _ in choice.chosen] == ["ylate.csv"]
    unnamed = _state(
        recorded=frozenset({_file("x.csv")}), target_watermark=_MARK, record_horizon=horizon
    )
    seeded = _read_set.choose(
        "quiet",
        [(late, _MARK - dt.timedelta(hours=3))],
        after=_MARK,
        lookback=_read_set.DEFAULT_LOOKBACK,
        state=unnamed,
    )
    assert seeded.chosen == []  # an older worker: the connection is seeded as before


def test_a_trimmed_record_keeps_the_newest_file_of_every_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quieter connection trimmed out entirely keeps one file, so it is not taken as new."""
    busy = [_file(f"x{n}", _MARK + dt.timedelta(hours=n)) for n in range(4)]
    quiet = [
        context.ReadFile("quiet", f"q{n}", 1, _MARK - dt.timedelta(hours=9 - n)) for n in range(2)
    ]
    monkeypatch.setattr(context, "RECORD_BUDGET_BYTES", 2 * len(context._line(busy[0].to_json())))
    state = _state(files_read=[*busy, *quiet])
    context._set(state)
    try:
        payload = context.files_read_payload()
    finally:
        context._set(None)
    assert payload is not None
    names = [line.split(b'"name": "')[1].split(b'"')[0] for line in payload.splitlines()[1:]]
    assert names == [b"x2", b"x3", b"q1"]
