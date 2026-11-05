"""Which listed files an incremental `files.read` reads: the lookback window and the record.

A run reads the files modified after ``since`` less ``lookback`` that the target has not recorded as
read. A connection the target has no record for, while the target has a time watermark, is
seeded: its files at or before the watermark (or ``since``, if earlier) count as read, and
those inside the call's own window (after ``since`` less ``lookback``, and within 30 days of the
watermark) are reported as read, so that later runs know them too. A longer ``lookback`` later
reads the files of the extra window once if they were never recorded. When the worker loaded
only the newest files of a busy record it sends a horizon (`_TaskState.record_horizon`), and
every file at or before it counts as read.
A file is the same file when its connection, name, size and time match; an object store's
ETag tells an overwritten object apart when both runs saw one. With ``since=None`` nothing is
filtered: every file is read and reported.

The record the worker sent is applied only when there is one (`_TaskState.recorded`); without
it a call reads strictly after ``since``, as before the lookback existed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from lakana.context import RECORD_WINDOW, ReadFile, _TaskState
from lakana.ingest._directory import FileEntry
from lakana.ingest._errors import FileShapeError, UsageError

DEFAULT_LOOKBACK = dt.timedelta(hours=24)
"""The default of `files.read`'s ``lookback``."""
MAX_NAME_LENGTH = 1024
"""The longest path the record holds, in characters; the server refuses a longer one."""
_EARLIEST = dt.datetime.min.replace(tzinfo=dt.UTC) + RECORD_WINDOW
_LATEST = dt.datetime.max.replace(tzinfo=dt.UTC) - RECORD_WINDOW
_NARROW = "narrow the pattern the asset passes to files.read so that it does not match"


@dataclass
class Choice:
    """What one call reads and how it counted the files inside its window.

    Parameters
    ----------
    chosen
        The files to read, with the time used for each, in listing order.
    new
        Files modified after ``since``.
    late
        Files inside the lookback, at or before ``since``.
    already
        Files inside the window the target recorded as read, or that seeding takes as read.
    inside
        Every file inside the window, read or not: what ``max_files`` counts.
    reported
        Every file to report as read: the chosen ones and the seeded ones.
    horizon
        The record's horizon when it applied (`_TaskState.record_horizon`), else `None`.
    horizon_skipped
        Files inside the window without the horizon that it took as read.
    """

    chosen: list[tuple[FileEntry, dt.datetime]] = field(default_factory=list)
    new: int = 0
    late: int = 0
    already: int = 0
    inside: int = 0
    reported: list[ReadFile] = field(default_factory=list)
    horizon: dt.datetime | None = None
    horizon_skipped: int = 0


def check_lookback(lookback: object) -> dt.timedelta:
    """Check ``lookback=``.

    Parameters
    ----------
    lookback
        The value passed.

    Returns
    -------
    datetime.timedelta
        The lookback.

    Raises
    ------
    UsageError
        For a value that is no `datetime.timedelta`, a negative one, or one over 30 days.
    """
    if not isinstance(lookback, dt.timedelta):
        raise UsageError(
            "lookback= takes a datetime.timedelta, such as timedelta(hours=24), not "
            f"{type(lookback).__name__}"
        )
    if lookback < dt.timedelta(0):
        raise UsageError("lookback= cannot be negative; pass timedelta(0) to turn the lookback off")
    if lookback > RECORD_WINDOW:
        raise UsageError(
            f"lookback= is at most 30 days, not {lookback}; for a file later than that, copy it "
            "into the folder again so that it gets a new time"
        )
    return lookback


def storable(text: str) -> bool:
    """Tell whether the record can hold a text: no NUL, and valid UTF-8 (no lone surrogate).

    Parameters
    ----------
    text
        The text.

    Returns
    -------
    bool
        Whether the server stores it.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return "\x00" not in text


def check_storable(connection: str, files: Iterable[ReadFile]) -> None:
    """Refuse, before any file is read, a file the record cannot hold.

    The server refuses the whole record for such a file, so the run would fail at its upload;
    the same refusal here names the file before anything is read.

    Parameters
    ----------
    connection
        The connection, for the message.
    files
        The files to report.

    Raises
    ------
    FileShapeError
        For a name over `MAX_NAME_LENGTH` characters, a name with a NUL or that is not valid
        UTF-8 (a local name's undecodable bytes arrive as lone surrogates), or a time within
        30 days of year 1 or year 9999.
    """
    for file in files:
        if len(file.name) > MAX_NAME_LENGTH:
            raise FileShapeError(
                f"`{connection}`: names a file whose path is longer than {MAX_NAME_LENGTH} "
                f"characters (beginning {file.name[:64]!r}); move it to a shorter path, or "
                f"{_NARROW}"
            )
        if not storable(file.name):
            raise FileShapeError(
                f"`{connection}`: names a file Lakana cannot store ({file.name[:64]!r}): its "
                "name holds a NUL character or is not valid UTF-8; rename the file; Lakana "
                "stores file names as UTF-8 text"
            )
        if not _EARLIEST <= file.modified <= _LATEST:
            raise FileShapeError(
                f"`{connection}`: gives {file.name[:64]!r} the time {file.modified.isoformat()}, "
                "within 30 days of the earliest or latest time Lakana can store (years 1 and "
                "9999); correct the file's modification time (or the date in its name), or "
                f"{_NARROW}"
            )


def _utc(value: dt.datetime) -> dt.datetime:
    """Make a time UTC.

    Parameters
    ----------
    value
        A time; a naive one is UTC.

    Returns
    -------
    datetime.datetime
        UTC-aware.
    """
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _minus(value: dt.datetime, span: dt.timedelta) -> dt.datetime:
    """Subtract a span from an aware time, stopping at the earliest time.

    Parameters
    ----------
    value
        An aware UTC time.
    span
        The span.

    Returns
    -------
    datetime.datetime
        `value` less `span`, or the earliest time if that is before year 1.
    """
    try:
        return value - span
    except OverflowError:
        return dt.datetime.min.replace(tzinfo=dt.UTC)


def _index(
    recorded: Iterable[ReadFile],
) -> dict[tuple[str, str, int, dt.datetime], set[str | None]]:
    """Index a record by connection, name, size and time, to the ETags recorded for each.

    Parameters
    ----------
    recorded
        The files the target recorded as read.

    Returns
    -------
    dict
        ``(connection, name, size, modified)`` to the set of its recorded ETags (`None` for
        none).
    """
    index: dict[tuple[str, str, int, dt.datetime], set[str | None]] = {}
    for file in recorded:
        index.setdefault((file.connection, file.name, file.size, file.modified), set()).add(
            file.etag
        )
    return index


def _known(
    file: ReadFile,
    index: dict[tuple[str, str, int, dt.datetime], set[str | None]],
    use_etag: bool,
) -> bool:
    """Tell whether the record holds a file.

    Connection, name, size and time must match. The ETag takes part only for an object store,
    and only when both the file and the record have one: a store that sends one on one run and
    not on the next does not make a file new.

    Parameters
    ----------
    file
        The listed file.
    index
        The record, from `_index`.
    use_etag
        Whether the source's ETags identify a file (S3, GCS, Azure; not an HTTPS server, which
        may vary them for an unchanged file).

    Returns
    -------
    bool
        Whether the file was recorded as read.
    """
    etags = index.get((file.connection, file.name, file.size, file.modified))
    if etags is None:
        return False
    return not use_etag or file.etag is None or None in etags or file.etag in etags


def choose(
    connection: str,
    timed: Sequence[tuple[FileEntry, dt.datetime]],
    *,
    after: dt.datetime | None,
    lookback: dt.timedelta,
    state: _TaskState,
    use_etag: bool = False,
) -> Choice:
    """Choose the files one call with ``since=`` reads, and the ones it reports as read.

    Parameters
    ----------
    connection
        The connection's name.
    timed
        The listed files, each with the time `files.read` uses for it (UTC), in listing order.
    after
        ``since`` in UTC; `None` reads every file.
    lookback
        How far before `after` a file not recorded as read is still read; never further than
        30 days before the target's watermark, where the worker's record ends.
    state
        The task's state: its record and the target's watermark.
    use_etag
        Whether ETags take part in a file's identity (an object store's).

    Returns
    -------
    Choice
        The files to read and the counts.
    """
    choice = Choice()
    if after is None:
        for entry, modified in timed:
            choice.inside += 1
            choice.new += 1
            choice.chosen.append((entry, modified))
            choice.reported.append(
                ReadFile(connection, entry.path, entry.size, modified, entry.etag)
            )
        return choice
    recorded = state.recorded
    mark = None if state.target_watermark is None else _utc(state.target_watermark)
    start = after if recorded is None else _minus(after, lookback)
    if recorded is not None and mark is not None:
        # Before the record's start a file read earlier is no longer known: never look there.
        start = max(start, min(after, _minus(mark, RECORD_WINDOW)))
    unclamped = start
    if recorded is not None and state.record_horizon is not None:
        # The worker loaded only the newest files of a busy record: older ones count as read.
        choice.horizon = _utc(state.record_horizon)
        # Whatever `since` is: a recorded file it left out was not loaded, so must not be read.
        start = max(start, choice.horizon)
    seed = None
    seed_report = after
    if (
        recorded is not None
        and mark is not None
        and not any(file.connection == connection for file in recorded)
    ):
        # Up to `since` only: a file after it is read today, so it must not be taken as read.
        seed = min(mark, after)
        # Seeding records only the files this call's window holds: the next runs look no further.
        seed_report = max(_minus(after, lookback), _minus(mark, RECORD_WINDOW))
    index = _index(recorded or ())
    for entry, modified in timed:
        file = ReadFile(connection, entry.path, entry.size, modified, entry.etag)
        seeded = False
        if seed is not None and modified <= seed:
            seeded = True
            if seed_report < modified:
                choice.reported.append(file)
        if modified <= start:
            if unclamped < modified:
                choice.horizon_skipped += 1
            continue
        choice.inside += 1
        if seeded or _known(file, index, use_etag):
            choice.already += 1
            continue
        if modified <= after:
            choice.late += 1
        else:
            choice.new += 1
        choice.chosen.append((entry, modified))
        choice.reported.append(file)
    return choice


def _files(count: int, what: str) -> str:
    """Count files in words.

    Parameters
    ----------
    count
        How many.
    what
        The singular, such as ``new file``.

    Returns
    -------
    str
        Such as ``1 new file`` or ``3 new files``.
    """
    return f"{count} {what}" + ("" if count == 1 else "s")


def _span(lookback: dt.timedelta) -> str:
    """Name a lookback in hours, minutes or seconds.

    Parameters
    ----------
    lookback
        The lookback, more than zero.

    Returns
    -------
    str
        Such as ``24 h``, ``90 min`` or ``1.5 s``.
    """
    seconds = lookback.total_seconds()
    if seconds % 3600 == 0:
        return f"{int(seconds // 3600)} h"
    if seconds % 60 == 0:
        return f"{int(seconds // 60)} min"
    return f"{seconds:g} s"


def window_words(lookback: dt.timedelta | None) -> str:
    """Name the window a call looks at, for its messages.

    Parameters
    ----------
    lookback
        The lookback; `None` or zero when no window applied.

    Returns
    -------
    str
        Such as ``inside the 24 h lookback window``, or ``after since=``.
    """
    if not lookback:
        return "after since="
    return f"inside the {_span(lookback)} lookback window"


def summary(connection: str, choice: Choice, lookback: dt.timedelta | None) -> str:
    """Write the line a call prints on standard error, to the task's log.

    Parameters
    ----------
    connection
        The connection's name.
    choice
        What the call chose.
    lookback
        The lookback; `None` or zero when no window applied (no record, or ``since=None``).

    Returns
    -------
    str
        Such as ``files.read: `exports`: 3 new files, 2 late files inside the 24 h lookback,
        41 already read``.
    """
    parts = [_files(choice.new, "new file")]
    if lookback:
        parts.append(f"{_files(choice.late, 'late file')} inside the {_span(lookback)} lookback")
    parts.append(f"{choice.already} already read")
    if choice.horizon_skipped and choice.horizon is not None:
        parts.append(
            f"{_files(choice.horizon_skipped, 'file')} at or before the record's horizon "
            f"{choice.horizon.isoformat()} taken as read (the table's record of files read is "
            "full); see 'A busy table'"
        )
    return f"files.read: `{connection}`: " + ", ".join(parts)
