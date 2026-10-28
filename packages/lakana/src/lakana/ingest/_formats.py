"""Parse the bytes of one flat file into a polars DataFrame, and combine several.

Used by `lakana.ingest.files` inside a task and, later, by ``lakana load`` for a local file.
polars (and fastexcel for spreadsheets) are imported when a file is parsed, never when this
module is imported, so the CLI can import it without the ``assets`` extra.
"""

from __future__ import annotations

import importlib
import io
import re
import struct
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from lakana.ingest._errors import FileShapeError, UsageError, safe_name

if TYPE_CHECKING:
    import polars as pl

FORMATS = ("csv", "parquet", "json", "ndjson", "xlsx", "xlsm", "xlsb", "xls", "ods")
"""The formats `parse` reads."""
SPREADSHEETS = frozenset({"xlsx", "xlsm", "xlsb", "xls", "ods"})
"""The formats with sheets, read with the calamine engine (fastexcel)."""
ZIPPED = frozenset({"xlsx", "xlsm", "xlsb", "ods"})
"""The spreadsheet formats that are zip archives, whose unpacked size `unpacked_size` reads."""
_WITH_HEADER = SPREADSHEETS | {"csv"}
_ALIASES = {"jsonl": "ndjson"}
_PROBLEM_CHARS = 500
MAX_NESTING = 100
"""The deepest nesting of arrays and objects a JSON document may have. A deeper one is refused
before it is parsed into columns: polars and pyarrow recurse on the C stack and crash the task at
about 10,000 levels, and no table gains anything from more than a hundred."""
_JSON_STRING = re.compile(rb'"(?:[^"\\]++|\\.?)*+(?:"|\Z)', re.DOTALL)
"""A JSON string, or an unterminated one to the end: possessive, so it never backtracks."""
_NOT_BRACKET = bytes(b for b in range(256) if b not in b"[]{}")
_STEPS = bytes.maketrans(b"[{]}", b"\x01\x01\xff\xff")


_COMPRESSED = (
    (b"\x1f\x8b", "gzip"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    *((b"\x78" + level, "zlib") for level in (b"\x01", b"\x5e", b"\x9c", b"\xda")),
)
"""The first bytes of compressed data. Polars inflates gzip, zlib and zstd input it recognises
this way, past the byte caps and the nesting check, which count the compressed bytes; bzip2 and
xz are refused too, in case a later polars learns them."""
_TEXT = frozenset({"csv", "json", "ndjson"})
"""The formats read as text, which must be uncompressed UTF-8."""


def check_text(data: bytes, name: str) -> None:
    """Refuse a text file that is compressed or not UTF-8, before any parser sees it.

    Parameters
    ----------
    data
        The file's bytes.
    name
        The file, for messages (already escaped).

    Raises
    ------
    FileShapeError
        For data that starts like a compressed file, or that is not strict UTF-8 (a UTF-8 byte
        order mark is allowed).
    """
    for magic, kind in _COMPRESSED:
        if data.startswith(magic):
            raise FileShapeError(
                f"`{name}`: is {kind}-compressed, and Lakana reads CSV, JSON and NDJSON files "
                "uncompressed only: decompress it at the source, or export it as Parquet"
            )
    try:
        data.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    except UnicodeDecodeError as exc:
        at = exc.start + (3 if data.startswith(b"\xef\xbb\xbf") else 0)
        raise FileShapeError(
            f"`{name}`: is not UTF-8 text (byte {data[at]:#04x} at position {at}); save or "
            "export it as UTF-8"
        ) from None


def format_of(name: str, given: str | None) -> str:
    """Decide a file's format from `format=` or its extension.

    Parameters
    ----------
    name
        The file's name as messages show it (``<connection>/<path>``).
    given
        The asset's ``format=``, or `None`.

    Returns
    -------
    str
        One of `FORMATS`.

    Raises
    ------
    UsageError
        For an unknown ``format=`` or, without one, an extension that is not a format.
    """
    known = ", ".join(FORMATS)
    name = safe_name(name)
    if given is not None:
        found = _ALIASES.get(given.lower(), given.lower())
        if found not in FORMATS:
            raise UsageError(f"format={given!r} is not a format files.read reads ({known})")
        return found
    extension = name.rsplit("/", 1)[-1].rpartition(".")[2].lower()
    found = _ALIASES.get(extension, extension)
    if "." not in name.rsplit("/", 1)[-1] or found not in FORMATS:
        raise UsageError(f"cannot tell the format of `{name}`; pass format= ({known})")
    return found


_CHUNK = 64 * 1024
"""How much of an archive entry is inflated at a time while it is counted."""
_LOCAL_HEADER = struct.Struct("<4s2B4HL2L2H")


def unpacked_size(data: bytes, limit: int) -> tuple[int, bool] | None:
    """Count what a zip archive really inflates to, stopping once it passes a limit.

    The central directory is the sender's claim and a reader inflates by the compressed stream,
    so every deflated entry is inflated here in chunks and its real output counted; memory stays
    at one chunk however large the entry is. An entry's declared sizes are used for entries
    that are stored, not deflated.

    Parameters
    ----------
    data
        The file's bytes.
    limit
        The most bytes of output to count; counting stops when it is passed.

    Returns
    -------
    tuple or None
        The bytes counted and whether `limit` was passed (then the count is a lower bound);
        `None` when the bytes are no zip archive or an entry cannot be inflated (the reader then
        gives its own error).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
        total = 0
        for item in entries:
            if item.compress_type != zipfile.ZIP_DEFLATED:
                total += max(item.file_size, item.compress_size)
            else:
                header = _LOCAL_HEADER.unpack_from(data, item.header_offset)
                position = item.header_offset + _LOCAL_HEADER.size + header[-2] + header[-1]
                inflater = zlib.decompressobj(-15)
                while not inflater.eof and position < len(data):
                    pending = data[position : position + _CHUNK]
                    position += len(pending)
                    while pending and not inflater.eof:
                        out = inflater.decompress(pending, _CHUNK)
                        total += len(out)
                        if total > limit:
                            return total, True
                        pending = inflater.unconsumed_tail
            if total > limit:
                return total, True
        return total, False
    except (zipfile.BadZipFile, zlib.error, struct.error):
        return None


def nested_deeper(data: bytes, limit: int = MAX_NESTING) -> bool:
    """Say whether JSON nests arrays and objects deeper than a limit, without parsing it.

    Strings are dropped first (a bracket inside one does not count); what is left is one step
    per bracket, whose running sum is the depth. Every step runs in C, so a large file costs
    little more than a copy of it.

    Parameters
    ----------
    data
        JSON or NDJSON, valid or not.
    limit
        The deepest nesting allowed.

    Returns
    -------
    bool
        True when some point lies more than `limit` arrays and objects deep.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    if data.count(b"[") + data.count(b"{") <= limit:
        return False
    steps = _JSON_STRING.sub(b"", data).translate(_STEPS, _NOT_BRACKET)
    signed = pa.Array.from_buffers(pa.int8(), len(steps), [None, pa.py_buffer(steps)])
    depths = pc.cumulative_sum(signed.cast(pa.int64()))  # ty: ignore[unresolved-attribute]
    deepest = pc.max(depths).as_py()  # ty: ignore[unresolved-attribute]
    return deepest is not None and deepest > limit


def _problem(exc: BaseException) -> str:
    """Keep the first line of a reader's message: the problem, not the reader's own advice.

    Parameters
    ----------
    exc
        What polars or fastexcel raised.

    Returns
    -------
    str
        At most 500 characters.
    """
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:_PROBLEM_CHARS]


def parse(
    data: bytes,
    *,
    name: str,
    format: str,
    sheet: str | None = None,
    header_row: int = 1,
    schema: Mapping[str, Any] | None = None,
    max_rows: int | None = None,
) -> pl.DataFrame:
    """Parse one file.

    Parameters
    ----------
    data
        The file's bytes.
    name
        ``<connection>/<path>``, for messages.
    format
        One of `FORMATS` (`format_of`).
    sheet
        A spreadsheet's sheet by name; the first sheet when `None`.
    header_row
        The 1-based row that holds the column names of a CSV file or a sheet; ``0`` for none
        (the columns are then ``column_1``, ...).
    schema
        Column name to polars type for the columns whose type you set; the others are
        inferred. Every column named must be in the file.
    max_rows
        The most rows to read: CSV, Parquet, NDJSON and spreadsheet readers stop after them
        (a Parquet file decodes no further), a JSON array is cut after parsing; `None` for all.

    Returns
    -------
    polars.DataFrame
        The rows, at most `max_rows`.

    Raises
    ------
    UsageError
        For ``sheet=`` on a file without sheets, or ``header_row`` other than 1 on a file
        without a header row.
    FileShapeError
        When the file cannot be parsed, a CSV, JSON or NDJSON file is compressed or not UTF-8
        (`check_text`), or a JSON file nests deeper than `MAX_NESTING`; the message names the
        file and the problem.
    """
    import polars as pl

    name = safe_name(name)
    check_schema(schema)
    if sheet is not None and format not in SPREADSHEETS:
        raise UsageError(f"sheet= applies to spreadsheets; `{name}` is {format}")
    if header_row != 1 and format not in _WITH_HEADER:
        raise UsageError(f"header_row= applies to CSV files and spreadsheets; `{name}` is {format}")
    if header_row < 0:
        raise UsageError("header_row= is the 1-based row of the column names, or 0 for none")
    overrides = dict(schema) if schema else None
    if format in SPREADSHEETS:
        # A missing engine is the worker's fault, not the file's: let the ImportError out.
        importlib.import_module("fastexcel")

    if format in _TEXT:
        check_text(data, name)
    if format in ("json", "ndjson") and nested_deeper(data):
        raise FileShapeError(
            f"`{name}`: nests arrays and objects deeper than {MAX_NESTING} levels, which Lakana "
            "does not read; flatten the file at the source, or read it with connections.get and "
            "your own parser"
        )
    source = io.BytesIO(data)
    try:
        if format == "csv":
            has_header, skip_rows = header_row > 0, max(header_row - 1, 0)
            # Polars 1 applies an override whose name is not in the header to a column by
            # position; the header is read first so a missing name is refused on every version.
            header = pl.read_csv(
                io.BytesIO(data), n_rows=0, has_header=has_header, skip_rows=skip_rows
            ).columns
            _require_columns(name, overrides, header)
            frame = pl.read_csv(
                source,
                has_header=has_header,
                skip_rows=skip_rows,
                schema_overrides=overrides,
                infer_schema_length=10_000,
                n_rows=max_rows,
            )
        elif format == "parquet":
            frame = pl.read_parquet(source, n_rows=max_rows)
            present = {k: v for k, v in (overrides or {}).items() if k in frame.columns}
            frame = frame.cast(present, strict=True) if present else frame  # ty: ignore[invalid-argument-type]
        elif format == "json":
            # A JSON array is parsed whole (no reader stops early); its bytes are capped.
            frame = pl.read_json(source, schema_overrides=overrides)
            frame = frame if max_rows is None else frame.head(max_rows)
        elif format == "ndjson":
            frame = pl.read_ndjson(source, schema_overrides=overrides, n_rows=max_rows)
        else:
            options: dict[str, Any] = {"header_row": header_row - 1} if header_row > 0 else {}
            if max_rows is not None:
                options["n_rows"] = max_rows
            frame = pl.read_excel(
                source,
                sheet_name=sheet,
                engine="calamine",
                has_header=header_row > 0,
                read_options=options,
                schema_overrides=overrides,
            )
    except BaseException as exc:  # a reader's own error, a KeyError, or a native panic
        # Not the file's fault: a request to stop, a worker that is out of resources, a missing
        # module, or the error already made for the file.
        if isinstance(
            exc,
            KeyboardInterrupt
            | SystemExit
            | GeneratorExit
            | MemoryError
            | RecursionError
            | ImportError
            | FileShapeError,
        ):
            raise
        # polars reports a missing column its own way (a KeyError for sheets); say what it is.
        if overrides and format != "csv" and (cols := _columns_of(data, format, sheet, header_row)):
            _require_columns(name, overrides, cols)
        raise FileShapeError(
            f"`{name}`: {safe_name(_problem(exc))}; check the file, or pass schema= with the "
            "column's type"
        ) from None
    _require_columns(name, overrides, frame.columns)
    return frame


def _columns_of(data: bytes, format: str, sheet: str | None, header_row: int) -> list[str]:
    """Read a file's column names without ``schema=``, for the message of a failed read.

    Parameters
    ----------
    data
        The file's bytes.
    format
        The file's format (not ``csv`` or ``parquet``, which check their names first).
    sheet
        A spreadsheet's sheet, or `None`.
    header_row
        The 1-based header row of a sheet.

    Returns
    -------
    list of str
        The columns; empty when the file cannot be read at all (the caller's own error is
        the better message then).
    """
    import polars as pl

    source = io.BytesIO(data)
    try:
        if format == "json":
            return pl.read_json(source).columns
        if format == "ndjson":
            return pl.read_ndjson(source).columns
        return pl.read_excel(
            source,
            sheet_name=sheet,
            engine="calamine",
            has_header=header_row > 0,
            read_options={"header_row": header_row - 1} if header_row > 0 else {},
        ).columns
    except Exception:
        return []


def check_schema(schema: Mapping[str, Any] | None) -> None:
    """Refuse a ``schema=`` value that is not a Polars type.

    Parameters
    ----------
    schema
        ``schema=``, or `None`.

    Raises
    ------
    FileShapeError
        Naming the first column whose value is not a Polars type.
    """
    import polars as pl

    for column, kind in (schema or {}).items():
        if not (
            isinstance(kind, pl.DataType)
            or (isinstance(kind, type) and issubclass(kind, pl.DataType))
        ):
            raise FileShapeError(
                f"schema= maps a column to a Polars type such as pl.Int64; {column!r} has {kind!r}"
            )


def _require_columns(name: str, schema: Mapping[str, Any] | None, columns: Sequence[str]) -> None:
    """Refuse a column ``schema=`` names that the file does not have.

    Parameters
    ----------
    name
        The file, for the message.
    schema
        ``schema=``, or `None`.
    columns
        The file's columns.

    Raises
    ------
    FileShapeError
        Naming the file and the first missing column.
    """
    missing = [column for column in schema or {} if column not in columns]
    if missing:
        raise FileShapeError(f"`{name}` has no column {missing[0]!r}, which schema= names")


def _fits(dtypes: Sequence[pl.DataType]) -> bool:
    """Say whether a column's types in several files may be combined.

    Only the same type, signed integers of different widths (they widen to the widest) and
    floats of different widths are combined; anything else would change what a value means.

    Parameters
    ----------
    dtypes
        The column's type in each file that has values for it.

    Returns
    -------
    bool
        Whether the files' types fit.
    """
    first = dtypes[0]
    if all(d == first for d in dtypes):
        return True
    return all(d.is_signed_integer() for d in dtypes) or all(d.is_float() for d in dtypes)


def combine(frames: Sequence[pl.DataFrame], names: Sequence[str]) -> pl.DataFrame:
    """Combine the files' rows, filling a column a file lacks with nulls.

    A column keeps one type family across files: the same type, signed integers (widened to
    the widest) or floats (to the widest) combine, and a file in which the column is all null
    takes the type of the others. Nothing else is converted: a date and a number, text and a
    number, an integer and a float are refused.

    Parameters
    ----------
    frames
        One frame per file, in file order.
    names
        The files' names, for the message.

    Returns
    -------
    polars.DataFrame
        All rows; a column's type is the widest of its files' types.

    Raises
    ------
    FileShapeError
        When a column's types in the files do not fit; names each file and type, and says to
        pass ``schema=``.
    """
    import polars as pl

    names = [safe_name(n) for n in names]
    if len(frames) == 1:
        return frames[0]
    conflicts: list[str] = []
    adapted = list(frames)
    columns = dict.fromkeys(column for frame in frames for column in frame.columns)
    for column in columns:
        holders = [i for i, frame in enumerate(frames) if column in frame.columns]
        valued = [i for i in holders if frames[i][column].null_count() < frames[i].height]
        if not valued:
            continue
        if not _fits([frames[i].schema[column] for i in valued]):
            where: dict[str, list[str]] = {}
            for i in valued:
                where.setdefault(str(frames[i].schema[column]), []).append(names[i])
            conflicts.append(
                f"column {column!r} is "
                + ", ".join(
                    f"{dtype} in {', '.join(f'`{n}`' for n in found)}"
                    for dtype, found in where.items()
                )
            )
            continue
        target = frames[valued[0]].schema[column]
        for i in holders:
            if i not in valued and frames[i].schema[column] != target:
                adapted[i] = adapted[i].with_columns(pl.col(column).cast(target))
    if conflicts:
        raise FileShapeError(
            f"the files cannot be combined: {'; '.join(conflicts)}; pass schema= with one type "
            "for each such column, or narrow the pattern"
        )
    try:
        return pl.concat(adapted, how="diagonal_relaxed")
    except pl.exceptions.PolarsError as failure:
        raise FileShapeError(
            f"the files cannot be combined: {safe_name(_problem(failure))}; pass schema= with "
            "one type for each such column, or narrow the pattern"
        ) from None
