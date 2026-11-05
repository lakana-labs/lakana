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
