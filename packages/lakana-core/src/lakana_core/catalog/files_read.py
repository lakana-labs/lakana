"""The record of the files an incremental `files.read` handed to an asset (#344).

A run of a deployed asset that reads files incrementally sends, with its upload, the list of
files `files.read` gave it: the Arrow schema metadata key `lakana.files_read`, newline-delimited
JSON. The first line is the header ``{"version": 1, "watermark": ...}``, and each line after it
is one file ``{"connection", "name", "size", "modified", "etag"}``. The commit of an append,
replace or merge carrying one records its files in `incremental_files_read`, in the transaction
that moves the table, so the record and the snapshot land together or not at all. A replace
deletes the table's earlier rows, since none of the old files' rows remain, and a check that sets
snapshots back deletes their rows.

Before the next run of the asset, the worker loads the files recorded within 30 days of its
watermark (at most the newest `MAX_FILES_READ`, with a horizon past that: `TaskRecord`) and hands
them to the task, so that `files.read` skips a file it already read and
still reads one that arrived late with an older time. The record is internal: no route lists it.

Only a task's bound token may send a record, naming only connections its asset declares. The
record is bounded before any of it is parsed: at most `MAX_RECORD_BYTES`, at most
`MAX_FILES_READ` files plus the header (the lines are counted first), and no line over
`MAX_LINE_BYTES`. So each `json.loads` sees one small object, and a hostile record costs at most
one line's objects at a time. Every field is bounded after: a connection name of at most 63
characters, a path of at most `MAX_NAME_LENGTH`, a size that fits a signed 64-bit integer,
and an ETag of at most 512.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import delete, insert, select, tuple_

from lakana_core.catalog.watermark import Watermark
from lakana_core.db import db_now
from lakana_core.errors import ValidationError
from lakana_core.models import IncrementalFileRead

if TYPE_CHECKING:
    import pyarrow as pa
    from sqlalchemy.orm import Session

    from lakana_core.auth import PrincipalRef

FILES_READ_METADATA_KEY = b"lakana.files_read"
"""The Arrow schema metadata key of the record; `lakana.resources.tables` writes the same."""
MAX_FILES_READ = 100_000
"""The most files a report may list, and the most rows a task gets; the SDK has the same cap."""
MAX_RECORD_BYTES = 64 * 1024 * 1024
"""The largest record accepted, checked before it is parsed.

An entry is about 120 bytes plus its path and ETag, so 64 MiB holds `MAX_FILES_READ` entries
whose paths and ETags average about 550 characters together: real paths with room to spare.
It is a quarter of the largest upload.
"""
MAX_LINE_BYTES = 32 * 1024
"""The longest line, checked before it is parsed.

Above every legal entry: a file with every field at its cap in emoji, escaped as `json.dumps`
does by default (twelve bytes a character), is about 19 KB. So the cap refuses only a malformed
line, and keeps each `json.loads` to one small object.
"""
RECORD_BUDGET_BYTES = 60 * 1024 * 1024
"""The most bytes of entries a run sends: the SDK keeps its newest files under it, below
`MAX_RECORD_BYTES` with room for the header. A record never makes the server refuse a run."""
MAX_NAME_LENGTH = 1024
"""The longest path of a file, in characters, as the artifact manifest bounds its paths."""
RECORD_WINDOW = dt.timedelta(days=30)
"""How far before the watermark the record is kept: the largest `lookback=`."""
_MAX_CONNECTION = 63
_MAX_ETAG = 512
_MAX_SIZE = 2**63 - 1
_NOT_A_TASK = (
    "Only a run of a deployed asset may send a record of the files it read (the Arrow schema "
    "metadata `lakana.files_read`); remove it from the upload."
)
_NARROW = "narrow the pattern the asset passes to files.read"
_EARLIEST = dt.datetime.min.replace(tzinfo=dt.UTC) + RECORD_WINDOW
_LATEST = dt.datetime.max.replace(tzinfo=dt.UTC) - RECORD_WINDOW
_NEAR_THE_LIMITS = (
    "within 30 days of the earliest or latest time Lakana can store (years 1 and 9999)"
)


@dataclass(frozen=True)
class FileRead:
    """One file `files.read` handed to an asset.

    Attributes
    ----------
    connection : str
        The connection it was read from.
    name : str
        Its path relative to the connection.
    size : int
        Its size in bytes, as listed.
    modified : datetime.datetime
        The aware UTC time `files.read` used for it.
    etag : str or None
        Its ETag, where the store reports one.
    """

    connection: str
    name: str
    size: int
    modified: dt.datetime
    etag: str | None

    def to_json(self) -> dict[str, object]:
        """Render the file as the record's JSON carries it.

        Returns
        -------
        dict[str, object]
            The five fields, the time in ISO 8601.
        """
        return {
            "connection": self.connection,
            "name": self.name,
            "size": self.size,
            "modified": self.modified.isoformat(),
            "etag": self.etag,
        }


@dataclass(frozen=True)
class FilesReadReport:
    """The record a run sent with its upload.

    Attributes
    ----------
    watermark : datetime.datetime or None
        The run's starting watermark, aware UTC; the rows older than it less
        `RECORD_WINDOW` are pruned. ``None`` on a first run.
    files : tuple[FileRead, ...]
        The files read, each once.
    """

    watermark: dt.datetime | None
    files: tuple[FileRead, ...]


def _malformed(what: str) -> str:
    """Spell the refusal of a record that the SDK of this release would not send.

    Parameters
    ----------
    what
        The part that is wrong.

    Returns
    -------
    str
        The refusal, with the next step.
    """
    return (
        "The record of the files read (Arrow schema metadata `lakana.files_read`) is malformed: "
        f"{what}. Use the same release of `lakana` in the project as on the server."
    )


def _time(value: object) -> dt.datetime | None:
    """Read an ISO 8601 time.

    Parameters
    ----------
    value
        The JSON value.

    Returns
    -------
    datetime.datetime or None
        The time as written, naive or aware; ``None`` if `value` is no time.
    """
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _in_range(value: dt.datetime) -> dt.datetime | None:
    """Convert an aware time to UTC, if it is far enough from the limits to subtract 30 days.

    Parameters
    ----------
    value
        An aware time.

    Returns
    -------
    datetime.datetime or None
        The time in UTC; ``None`` if it is within `RECORD_WINDOW` of year 1 or 9999.
    """
    try:
        utc = value.astimezone(dt.UTC)
    except OverflowError:
        return None
    return utc if _EARLIEST <= utc <= _LATEST else None


def _storable(text: str) -> bool:
    """Tell whether Postgres can store a text: no NUL, and valid UTF-8 (no lone surrogate).

    Parameters
    ----------
    text
        The text.

    Returns
    -------
    bool
        Whether it can be stored.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return "\x00" not in text


def entry_bytes(file: FileRead) -> int:
    """Count the bytes of a file's line in a record, as the SDK encodes it.

    Parameters
    ----------
    file
        The file.

    Returns
    -------
    int
        Its JSON, unescaped UTF-8, and the newline.
    """
    return len(json.dumps(file.to_json(), ensure_ascii=False).encode()) + 1


def _window_start(mark: dt.datetime) -> dt.datetime | None:
    """Find the start of the record's window before a mark.

    Parameters
    ----------
    mark
        The watermark, aware.

    Returns
    -------
    datetime.datetime or None
        `mark` less `RECORD_WINDOW`; ``None`` if that is before year 1, where nothing is older.
    """
    try:
        return mark - RECORD_WINDOW
    except OverflowError:
        return None


def _file(item: object, number: int) -> FileRead:
    """Check one entry of the record.

    Parameters
    ----------
    item
        The JSON value.
    number
        Its position, from 1, for the error.

    Returns
    -------
    FileRead
        The file, its time in UTC.

    Raises
    ------
    ValidationError
        If a field is missing, of the wrong type, out of its bounds, or not storable text.
    """
    wrong = _malformed(f"file {number} is not a connection, name, size, modified time and ETag")
    if not isinstance(item, dict):
        raise ValidationError(wrong)
    connection, name, size = item.get("connection"), item.get("name"), item.get("size")
    modified, etag = _time(item.get("modified")), item.get("etag")
    if (
        not isinstance(connection, str)
        or not 0 < len(connection) <= _MAX_CONNECTION
        or not _storable(connection)
        or not isinstance(name, str)
        or not name
        or not isinstance(size, int)
        or isinstance(size, bool)
        or modified is None
        or modified.tzinfo is None
        or not (etag is None or isinstance(etag, str))
    ):
        raise ValidationError(wrong)
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(
            "The record of the files read names a file whose path is longer than "
            f"{MAX_NAME_LENGTH} characters (file {number}, beginning {name[:64]!r}); move it to "
            f"a shorter path, or {_NARROW} so that it does not match."
        )
    if not _storable(name):
        raise ValidationError(
            f"The record of the files read names a file Lakana cannot store (file {number}, "
            f"{name[:64]!r}): its name holds a NUL character or is not valid UTF-8; rename the "
            "file; Lakana stores file names as UTF-8 text."
        )
    if not 0 <= size <= _MAX_SIZE:
        raise ValidationError(
            _malformed(f"file {number} has the size {size}, outside 0 to {_MAX_SIZE} bytes")
        )
    if etag is not None and len(etag) > _MAX_ETAG:
        raise ValidationError(
            _malformed(f"file {number} has an ETag longer than {_MAX_ETAG} characters")
        )
    if etag is not None and not _storable(etag):
        raise ValidationError(
            _malformed(f"file {number} has an ETag that is not UTF-8 text without NUL characters")
        )
    utc = _in_range(modified)
    if utc is None:
        raise ValidationError(
            f"The record of the files read gives file {number} the time {item['modified']}, "
            f"{_NEAR_THE_LIMITS}; correct the file's modification time (or the date in its name), "
            f"or {_NARROW} so that it does not match."
        )
    return FileRead(connection, name, size, utc, etag)


def _watermark(value: object) -> dt.datetime | None:
    """Check the record's watermark.

    Parameters
    ----------
    value
        The JSON value.

    Returns
    -------
    datetime.datetime or None
        The watermark in UTC, or ``None`` if the run had none.

    Raises
    ------
    ValidationError
        If it is no time, a time without a zone, or within `RECORD_WINDOW` of the limits.
    """
    if value is None:
        return None
    parsed = _time(value)
    if parsed is None:
        raise ValidationError(_malformed("watermark must be null or a time with a zone"))
    if parsed.tzinfo is None:
        raise ValidationError(_malformed(f"watermark {value} has no time zone; it must be UTC"))
    utc = _in_range(parsed)
    if utc is None:
        raise ValidationError(
            f"The record of the files read has the watermark {value}, {_NEAR_THE_LIMITS}; correct "
            "that time in the target table's watermark column, then run the asset again."
        )
    return utc


def split_files_read(data: pa.Table) -> tuple[pa.Table, bytes | None]:
    """Take the record off an upload, so the rows are written without it.

    Parameters
    ----------
    data
        The upload.

    Returns
    -------
    tuple[pa.Table, bytes or None]
        The upload without the key, its other metadata kept; and the raw record, or ``None``
        if it carries none (then the upload is returned unchanged).
    """
    metadata = dict(data.schema.metadata or {})
    raw = metadata.pop(FILES_READ_METADATA_KEY, None)
    if raw is None:
        return data, None
    return data.replace_schema_metadata(metadata or None), raw


def _lines(raw: bytes) -> list[bytes]:
    """Split a record into its lines, bounding their number and length before parsing.

    Parameters
    ----------
    raw
        The record, at most `MAX_RECORD_BYTES`.

    Returns
    -------
    list[bytes]
        The header and file lines, without a final empty line.

    Raises
    ------
    ValidationError
        If it lists more than `MAX_FILES_READ` files, or a line is over `MAX_LINE_BYTES`.
    """
    count = raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1)
    if count - 1 > MAX_FILES_READ:
        raise ValidationError(
            f"The record of the files read lists {count - 1} files, more than "
            f"{MAX_FILES_READ}; {_NARROW}."
        )
    lines = raw.split(b"\n")
    if len(lines) > 1 and not lines[-1]:
        lines.pop()
    for number, line in enumerate(lines, 1):
        if len(line) > MAX_LINE_BYTES:
            raise ValidationError(
                _malformed(f"line {number} is {len(line)} bytes, more than {MAX_LINE_BYTES}")
            )
    return lines


def _load(line: bytes, number: int) -> object:
    """Parse one line of a record.

    Parameters
    ----------
    line
        The line, at most `MAX_LINE_BYTES`.
    number
        Its number, from 1, for the error.

    Returns
    -------
    object
        The JSON value.

    Raises
    ------
    ValidationError
        If it is not JSON.
    """
    try:
        return json.loads(line)
    except ValueError:  # UnicodeDecodeError is a ValueError
        raise ValidationError(_malformed(f"line {number} is not JSON")) from None


def parse_files_read(raw: bytes, p: PrincipalRef) -> FilesReadReport:
    """Check the record a task sent, and who sent it.

    Parameters
    ----------
    raw
        Newline-delimited JSON: the header ``{"version": 1, "watermark": ...}``, then one
        file per line. A final newline is optional.
    p
        The uploading principal; only a task's bound token may send a record.

    Returns
    -------
    FilesReadReport
        The record, each file once.

    Raises
    ------
    ValidationError
        If `p` is not a bound task, the record is larger than `MAX_RECORD_BYTES`, lists more
        than `MAX_FILES_READ` files or has a line over `MAX_LINE_BYTES`, any part is
        malformed, or it names a connection the asset does not declare.
    """
    bound = p.task_scope.bound if p.task_scope is not None else None
    if bound is None:
        raise ValidationError(_NOT_A_TASK)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValidationError(
            f"The record of the files read is {len(raw)} bytes, more than {MAX_RECORD_BYTES}; "
            f"{_NARROW}."
        )
    lines = _lines(raw)
    header = _load(lines[0], 1)
    # `type(...) is int`: `true` and `1.0` equal 1 in Python, but are not the version.
    if (
        not isinstance(header, dict)
        or type(header.get("version")) is not int
        or header["version"] != 1
    ):
        raise ValidationError(_malformed("the first line must be a header with version 1"))
    watermark = _watermark(header.get("watermark"))
    # Each line is parsed and checked in turn, so a line's objects are gone before the next.
    found = dict.fromkeys(
        _file(_load(line, number + 1), number) for number, line in enumerate(lines[1:], 1)
    )
    for file in found:
        if file.connection not in bound.connections:
            raise ValidationError(
                f"The record of the files read names the connection {file.connection!r}, which "
                "the asset does not declare; add it to @asset(connections=[...]) and deploy again."
            )
    return FilesReadReport(watermark, tuple(found))


_DEDUPE_CHUNK = 1000
"""How many files one look-up of `_not_yet_recorded` asks about."""


def _not_yet_recorded(
    session: Session, table_id: uuid.UUID, files: tuple[FileRead, ...]
) -> list[FileRead]:
    """Drop the files the table has already recorded, so that no file is recorded twice.

    A seeded run, or a known file reported again, would otherwise add a second row. The files
    are looked up by their exact keys, `_DEDUPE_CHUNK` at a time, so a request holds at most
    that many keys' rows however large the table's record is.

    Parameters
    ----------
    session
        Database session.
    table_id
        The target table.
    files
        The run's files.

    Returns
    -------
    list of FileRead
        Those not recorded yet, in order.
    """
    known: set[FileRead] = set()
    for at in range(0, len(files), _DEDUPE_CHUNK):
        chunk = files[at : at + _DEDUPE_CHUNK]
        rows = session.execute(
            select(
                IncrementalFileRead.connection,
                IncrementalFileRead.name,
                IncrementalFileRead.size,
                IncrementalFileRead.modified,
                IncrementalFileRead.etag,
            ).where(
                IncrementalFileRead.table_id == table_id,
                tuple_(
                    IncrementalFileRead.connection,
                    IncrementalFileRead.name,
                    IncrementalFileRead.modified,
                ).in_([(f.connection, f.name, f.modified) for f in chunk]),
            )
        )
        known.update(FileRead(*row) for row in rows)
    return [f for f in files if f not in known]


def record_files_read(
    session: Session, table_id: uuid.UUID, snapshot_id: int, report: FilesReadReport
) -> None:
    """Record a run's files with the snapshot it committed, and prune the old rows.

    Uncommitted: the caller's transaction commits them, with the snapshot.

    Parameters
    ----------
    session
        Database session.
    table_id
        The target table.
    snapshot_id
        The snapshot the commit adds.
    report
        The run's record. Its watermark, if any, prunes the table's rows older than it less
        `RECORD_WINDOW`.
    """
    files = _not_yet_recorded(session, table_id, report.files)
    if files:
        read_at = db_now(session)
        session.execute(
            insert(IncrementalFileRead),
            [
                {
                    "id": uuid.uuid4(),
                    "table_id": table_id,
                    "connection": f.connection,
                    "name": f.name,
                    "size": f.size,
                    "modified": f.modified,
                    "etag": f.etag,
                    "snapshot_id": snapshot_id,
                    "read_at": read_at,
                }
                for f in files
            ],
        )
    start = None if report.watermark is None else _window_start(report.watermark)
    if start is not None:
        session.execute(
            delete(IncrementalFileRead)
            .where(
                IncrementalFileRead.table_id == table_id,
                IncrementalFileRead.modified < start,
            )
            .execution_options(synchronize_session=False)
        )


def forget_files_read(session: Session, table_id: uuid.UUID, snapshot_ids: Collection[int]) -> None:
    """Delete the rows of snapshots a check set back, so their files are read again.

    Uncommitted: the caller's transaction commits them.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    snapshot_ids
        The snapshots set back; none deletes nothing.
    """
    if snapshot_ids:
        session.execute(
            delete(IncrementalFileRead)
            .where(
                IncrementalFileRead.table_id == table_id,
                IncrementalFileRead.snapshot_id.in_(list(snapshot_ids)),
            )
            .execution_options(synchronize_session=False)
        )


def forget_table_files_read(session: Session, table_id: uuid.UUID) -> None:
    """Delete every row of a table, as a replace does: none of the old files' rows remain.

    Uncommitted: the caller's transaction commits them, with the replace's snapshot.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    """
    session.execute(
        delete(IncrementalFileRead)
        .where(IncrementalFileRead.table_id == table_id)
        .execution_options(synchronize_session=False)
    )


@dataclass(frozen=True)
class TaskRecord:
    """The record a task gets: what its `files.read` must not read again.

    Attributes
    ----------
    files : list[dict[str, object]]
        The files, as `FileRead.to_json` renders them, ordered by connection, name and time.
    connections : list[str]
        The connections with any row in the window, loaded or not, so that one whose rows all
        fall at or before the horizon is not taken as new.
    horizon : datetime.datetime or None
        Set when `MAX_FILES_READ` or more files are recorded within the window, or more than
        `RECORD_BUDGET_BYTES` less `MAX_LINE_BYTES` of entries (the SDK keeps a run's newest
        under the budget, so a run that dropped files always leaves this many): the time of
        the oldest file loaded. Every recorded file after it is in `files`; `files.read` takes
        every file at or before it as read, so nothing is read twice and the asset keeps
        running, but a late file at or before it is not caught. ``None`` otherwise.
    """

    files: list[dict[str, object]]
    horizon: dt.datetime | None = None
    connections: list[str] = field(default_factory=list)


def files_read_for_task(
    session: Session, table_id: uuid.UUID, watermark: str | None
) -> TaskRecord | None:
    """Load the files a task's `files.read` must not read again.

    At most the newest `MAX_FILES_READ` are loaded, so a busy table never stops its asset; past
    that the record carries a horizon (`TaskRecord.horizon`).

    Parameters
    ----------
    session
        Database session.
    table_id
        The asset's target table.
    watermark
        The task's starting watermark as the run stores it (``"timestamptz:..."``), or
        ``None`` for every recorded row. A naive ``timestamp`` watermark is UTC.

    Returns
    -------
    TaskRecord or None
        The files recorded within `RECORD_WINDOW` of the watermark; ``None`` if the watermark
        is no time (an integer, decimal or date key), which has no record.

    Raises
    ------
    ValidationError
        If `watermark` is no watermark.
    """
    where = [IncrementalFileRead.table_id == table_id]
    if watermark is not None:
        value = Watermark.decode(watermark).value
        if not isinstance(value, dt.datetime):
            return None
        mark = value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)
        start = _window_start(mark)
        if start is not None:
            where.append(IncrementalFileRead.modified > start)
    rows = list(
        session.scalars(
            select(IncrementalFileRead)
            .where(*where)
            .order_by(IncrementalFileRead.modified.desc())
            .limit(MAX_FILES_READ)
        )
    )
    # A full load may have left older rows out, and a run that read more than the record takes
    # (in files or in bytes) recorded only its newest: every file at or before the oldest
    # loaded counts as read.
    horizon = rows[-1].modified if len(rows) >= MAX_FILES_READ else None
    total = 0
    for kept, row in enumerate(rows, start=1):
        total += entry_bytes(FileRead(row.connection, row.name, row.size, row.modified, row.etag))
        if total > RECORD_BUDGET_BYTES - MAX_LINE_BYTES:
            rows, horizon = rows[:kept], row.modified
            break
    rows.sort(key=lambda r: (r.connection, r.name, r.modified))
    files = [FileRead(r.connection, r.name, r.size, r.modified, r.etag).to_json() for r in rows]
    connections = session.scalars(
        select(IncrementalFileRead.connection)
        .where(*where)
        .distinct()
        .order_by(IncrementalFileRead.connection)
    )
    return TaskRecord(files, horizon, list(connections))
