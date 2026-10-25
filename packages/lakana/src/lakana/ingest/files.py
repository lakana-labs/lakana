"""Read flat files through a connection the asset declares, inside a Lakana task.

Example::

    from lakana import asset
    from lakana.ingest import files


    @asset(connections=["exports"], write="append", watermark="_source_modified")
    def orders(ctx):
        return files.read("exports", "orders/*.csv", since=ctx.watermark())

`files.read` returns a `pyarrow.Table`: CSV, Parquet, JSON, NDJSON and spreadsheets, from a
``directory`` connection (below an ingestion root, never following a symbolic link). With
``since=`` it reads only the files modified after it and adds the column ``_source_modified``.
"""

from __future__ import annotations

import builtins
import contextlib
import datetime as dt
import sys
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol

from lakana.ingest import _check, _directory, _formats
from lakana.ingest._connections import FetchedConnection, fetch
from lakana.ingest._errors import CapExceeded, IngestError, safe_name

if TYPE_CHECKING:
    import pyarrow as pa

DEFAULT_MAX_BYTES = 256 * 1024 * 1024
"""The default of `max_file_bytes` and `max_total_bytes`: the server's default write cap."""
SOURCE_MODIFIED = "_source_modified"
"""The column `read` adds with ``since=``: each row's file's modification time, UTC."""
KINDS = ("directory",)
"""The connection kinds `read` and `list` read."""
_OTHER_HELPER = {
    "postgresql": "sql.read",
    "mariadb": "sql.read",
    "sqlite-file": "sql.read",
    "tcp": "connections.get with the team's own driver",
    "http": "connections.get with the team's own client",
    "s3": "connections.get with the team's own client",
    "gcs": "connections.get with the team's own client",
    "azure": "connections.get with the team's own client",
}


class _Unset:
    """The type of `UNSET`."""

    def __repr__(self) -> str:
        """Name the sentinel.

        Returns
        -------
        str
            ``UNSET``.
        """
        return "UNSET"


UNSET: Final = _Unset()
"""``since=`` left out: read every file and add no ``_source_modified``."""


class _Source(Protocol):
    """Where the files of a connection are; one per connection kind."""

    def list(self, pattern: str, max_files: int) -> _directory.Listing:
        """List the files that match a pattern.

        Parameters
        ----------
        pattern
            A glob relative to the connection's path or prefix.
        max_files
            The most files to return.

        Returns
        -------
        Listing
            The files, sorted by path, and what was skipped.
        """
        ...

    def read(self, path: str, max_bytes: int) -> bytes:
        """Read one listed file.

        Parameters
        ----------
        path
            Its path as listed.
        max_bytes
            The most bytes to read.

        Returns
        -------
        bytes
            Its content.
        """
        ...


@dataclass(frozen=True)
class _DirectorySource:
    """A ``directory`` connection: a path below an ingestion root.

    Parameters
    ----------
    root_dir
        The root as the task sees it.
    base
        The connection's path below it.
    """

    root_dir: str
    base: str

    def list(self, pattern: str, max_files: int) -> _directory.Listing:
        """List through the walker.

        Parameters
        ----------
        pattern
            The glob.
        max_files
            The cap.

        Returns
        -------
        Listing
            What the walk found.
        """
        return _directory.walk(self.root_dir, self.base, pattern, max_files=max_files)

    def read(self, path: str, max_bytes: int) -> bytes:
        """Read through the walker.

        Parameters
        ----------
        path
            The listed path.
        max_bytes
            The cap.

        Returns
        -------
        bytes
            The content.
        """
        return _directory.read(self.root_dir, self.base, path, max_bytes=max_bytes)


def _source(conn: FetchedConnection) -> _Source:
    """Choose the source of a connection's kind.

    Parameters
    ----------
    conn
        The fetched connection with its routing.

    Returns
    -------
    _Source
        Its source.

    Raises
    ------
    IngestError
        For a kind `read` does not read, naming the helper that does, or a ``directory``
        connection the task description has no root for.
    """
    if conn.kind not in KINDS:
        kinds = ", ".join(KINDS)
        helper = _OTHER_HELPER.get(conn.kind)
        hint = f": use {helper}" if helper else ""
        raise IngestError(
            f"files.read reads {kinds} connections; `{conn.name}` is {conn.kind}{hint}"
        )
    root_dir, base = conn.routing.get("root_dir"), conn.routing.get("path")
    if not isinstance(root_dir, str) or not isinstance(base, str):
        raise IngestError(
            f"the task description has no ingestion root for `{conn.name}`; check that the "
            "worker runs the server's version of Lakana"
        )
    return _DirectorySource(root_dir, base)


@contextlib.contextmanager
def _naming(conn: FetchedConnection) -> Generator[None]:
    """Name the connection in a source's error, and give a missing root the fixed root text.

    Parameters
    ----------
    conn
        The connection.

    Yields
    ------
    None
        While the source is used.

    Raises
    ------
    RootUnavailable
        With the fixed root text, for a root this worker does not have.
    DirectoryError
        The source's own class (`DirectoryIOError` too), prefixed with the connection's name.
    CapExceeded
        The source's own, prefixed with the connection's name.
    """
    try:
        yield
    except _directory.RootUnavailable:
        root = str(conn.routing.get("root", conn.address.get("root", "")))
        raise _directory.RootUnavailable(
            _check.MISSING_ROOT.format(connection=conn.name, root=root)
        ) from None
    except (_directory.DirectoryError, CapExceeded) as exc:
        # The same class, so its `retryable` survives (an I/O error may pass; a missing path not).
        raise type(exc)(f"`{conn.name}`: {exc}") from None


def _positive(**caps: object) -> None:
    """Refuse a cap that is not a positive whole number.

    Parameters
    ----------
    **caps
        Argument name to the value the asset passed.

    Raises
    ------
    IngestError
        Naming the argument.
    """
    for argument, value in caps.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise IngestError(f"{argument}= must be a positive whole number, not {value!r}")


def _as_utc(since: object) -> dt.datetime | None:
    """Read ``since=``: `None`, or a datetime (a naive one is UTC).

    Parameters
    ----------
    since
        The value.

    Returns
    -------
    datetime or None
        UTC-aware, or `None`.

    Raises
    ------
    IngestError
        For anything else.
    """
    if since is None:
        return None
    if not isinstance(since, dt.datetime):
        raise IngestError(
            f"since= takes a datetime or None, not {type(since).__name__}; pass "
            "ctx.watermark() of an asset with watermark='_source_modified'"
        )
    return since.replace(tzinfo=dt.UTC) if since.tzinfo is None else since.astimezone(dt.UTC)


def _name_time(path: str, pattern: str) -> dt.datetime | None:
    """Read the date or time in a file's name, as UTC.

    Parameters
    ----------
    path
        The file's path; only its name (after the last ``/``) is read.
    pattern
        A `datetime.strptime` pattern, such as ``export-%Y-%m-%d.csv``.

    Returns
    -------
    datetime or None
        The time (midnight for a date), UTC; `None` when the name does not fit.
    """
    try:
        found = dt.datetime.strptime(path.rsplit("/", 1)[-1], pattern)
    except ValueError:
        return None
    return found.replace(tzinfo=dt.UTC) if found.tzinfo is None else found.astimezone(dt.UTC)


def _empty(schema: Mapping[str, Any] | None, with_since: bool) -> pa.Table:
    """Build the table of a read that found no file to read.

    Parameters
    ----------
    schema
        ``schema=``, whose columns the table has; `None` for no columns.
    with_since
        Whether ``since=`` was passed (adds ``_source_modified``).

    Returns
    -------
    pyarrow.Table
        Zero rows.
    """
    import polars as pl

    columns: dict[str, Any] = dict(schema) if schema else {}
    if with_since and columns:
        columns[SOURCE_MODIFIED] = pl.Datetime("us", "UTC")
    return pl.DataFrame(schema=columns).to_arrow()


def read(
    connection: str,
    pattern: str,
    *,
    format: str | None = None,
    sheet: str | None = None,
    header_row: int = 1,
    schema: Mapping[str, Any] | None = None,
    since: dt.datetime | _Unset | None = UNSET,
    modified_from: str = "mtime",
    max_file_bytes: int = DEFAULT_MAX_BYTES,
    max_total_bytes: int = DEFAULT_MAX_BYTES,
    max_files: int = 10_000,
) -> pa.Table:
    """Read the files of a connection that match a pattern into one table.

    Parameters
    ----------
    connection
        A connection the asset declares (kind ``directory``).
    pattern
        A glob relative to the connection's path: ``*``, ``?`` and ``[...]`` within a name,
        ``**`` for any number of directories, such as ``"orders/*.csv"``. ``*`` matches names
        that start with a dot, matching is case-sensitive, and a hard link is read as a file.
    format
        ``csv``, ``parquet``, ``json``, ``ndjson`` (or ``jsonl``), ``xlsx``, ``xlsm``,
        ``xlsb``, ``xls`` or ``ods``; from each file's extension when `None`.
    sheet
        A spreadsheet's sheet by name; the first sheet when `None`.
    header_row
        The 1-based row of the column names in a CSV file or a sheet; ``0`` for none.
    schema
        Column name to polars type, such as ``{"id": pl.Int64}``; other columns are inferred.
    since
        Read only the files modified after this time (strictly after; a naive time is UTC),
        and add the column ``_source_modified``. `None` reads every file and adds the column
        (the first run of an asset with ``watermark="_source_modified"``). Left out: every
        file, no column.
    modified_from
        ``"mtime"`` (the file's modification time), or a `datetime.strptime` pattern for a
        date in the file's name, such as ``"export-%Y-%m-%d.csv"`` (midnight UTC); a name that
        does not fit is skipped with a line on standard error. Only with ``since=``.
    max_file_bytes
        The largest file read.
    max_total_bytes
        The most bytes read in all.
    max_files
        The most files read.

    Returns
    -------
    pyarrow.Table
        Every file's rows; a column a file lacks is null in its rows. No file to read gives
        zero rows: with ``schema=`` its columns (and ``_source_modified`` with ``since=``),
        else no columns.

    Raises
    ------
    ConnectionNotDeclared
        For a connection the asset does not declare.
    IngestError
        For a connection of another kind, a bad argument, or a path or pattern the walker
        refuses (`DirectoryError`, not retried).
    CapExceeded
        When a cap is passed; the message names the cap and how to narrow.
    SourceError
        When a file cannot be parsed or does not fit the others; the message names the file.
    """
    _positive(max_files=max_files, max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes)
    _formats.check_schema(schema)
    if modified_from != "mtime" and since is UNSET:
        raise IngestError("modified_from= applies only with since=")
    conn = fetch(connection)
    source = _source(conn)
    with _naming(conn):
        # The cap counts the files that will be read, so it applies after `since` filters.
        listing = source.list(pattern, _directory.MAX_ENTRIES)
    for skipped in listing.skipped:
        print(
            f"files.read: `{connection}`: skipped {skipped!r} (a symbolic link, not a regular "
            "file, or too deep)",
            file=sys.stderr,
        )
    with_since = not isinstance(since, _Unset)
    after = _as_utc(since) if with_since else None
    chosen: builtins.list[tuple[_directory.FileEntry, dt.datetime | None]] = []
    for entry in listing.files:
        modified = entry.modified
        if with_since and modified_from == "mtime" and modified is None:
            raise IngestError(
                f"`{connection}` does not say when {entry.path!r} was modified; pass "
                "modified_from= with the date pattern of its name"
            )
        if modified_from != "mtime":
            named = _name_time(entry.path, modified_from)
            if named is None:
                print(
                    f"files.read: `{connection}`: skipped {entry.path!r}, whose name does not "
                    f"fit modified_from={modified_from!r}",
                    file=sys.stderr,
                )
                continue
            modified = named
        if after is None or (modified is not None and modified > after):
            if len(chosen) == max_files:
                advice = "narrow the pattern" + ("" if with_since else " or pass since=")
                raise CapExceeded(
                    f"`{connection}`: the pattern {pattern!r} matches more than max_files "
                    f"({max_files}) files to read; {advice}"
                )
            chosen.append((entry, modified))
    if not chosen:
        return _empty(schema, with_since)
    import polars as pl

    too_many = CapExceeded(
        f"`{connection}`: the files matching {pattern!r} hold more than max_total_bytes "
        f"({max_total_bytes}); narrow the pattern or pass since= to read fewer files"
    )
    if sum(entry.size for entry, _ in chosen) > max_total_bytes:
        raise too_many
    frames, names, total = [], [], 0
    for entry, modified in chosen:
        with _naming(conn):
            data = source.read(entry.path, max_file_bytes)
        name = safe_name(f"{connection}/{entry.path}")
        kind = _formats.format_of(name, format)
        counted = _formats.unpacked_size(data, max_file_bytes) if kind in _formats.ZIPPED else None
        unpacked = None if counted is None else counted[0]
        if counted is not None and counted[1]:
            raise CapExceeded(
                f"`{name}` ({len(data)} bytes as a file) unpacks to more than max_file_bytes "
                f"({max_file_bytes}) bytes; split the workbook or pass a larger max_file_bytes"
            )
        total += max(len(data), unpacked or 0)
        if total > max_total_bytes:
            if unpacked is not None and unpacked > len(data):
                raise CapExceeded(
                    f"`{name}` unpacks to {unpacked} bytes ({len(data)} bytes as a file); with "
                    f"the files before it that is more than max_total_bytes ({max_total_bytes}); "
                    "narrow the pattern or pass since= to read fewer files"
                )
            raise too_many
        frame = _formats.parse(
            data,
            name=name,
            format=kind,
            sheet=sheet,
            header_row=header_row,
            schema=schema,
        )
        if with_since:
            frame = frame.with_columns(
                pl.lit(modified, dtype=pl.Datetime("us", "UTC")).alias(SOURCE_MODIFIED)
            )
        frames.append(frame)
        names.append(name)
    return _formats.combine(frames, names).to_arrow()


def list(connection: str, pattern: str, *, max_files: int = 10_000) -> pa.Table:
    """List the files of a connection that match a pattern, without reading them.

    Parameters
    ----------
    connection
        A connection the asset declares (kind ``directory``).
    pattern
        A glob, as for `read`.
    max_files
        The most files listed.

    Returns
    -------
    pyarrow.Table
        ``name`` (the path relative to the connection's path), ``size`` (int64, bytes) and
        ``modified`` (timestamp, UTC), sorted by name.

    Raises
    ------
    IngestError
        As `read`.
    CapExceeded
        For more matches than `max_files`.
    """
    import pyarrow as pa

    _positive(max_files=max_files)
    conn = fetch(connection)
    source = _source(conn)
    with _naming(conn):
        listing = source.list(pattern, max_files)
    return pa.table(
        {
            "name": pa.array([f.path for f in listing.files], pa.string()),
            "size": pa.array([f.size for f in listing.files], pa.int64()),
            "modified": pa.array([f.modified for f in listing.files], pa.timestamp("us", tz="UTC")),
        }
    )
