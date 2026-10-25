"""Parse the bytes of one flat file into a polars DataFrame, and combine several.

Used by `lakana.ingest.files` inside a task and, later, by ``lakana load`` for a local file.
polars (and fastexcel for spreadsheets) are imported when a file is parsed, never when this
module is imported, so the CLI can import it without the ``assets`` extra.
"""

from __future__ import annotations

import importlib
import io
import zipfile
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from lakana.ingest._errors import FileShapeError, IngestError, safe_name

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
    IngestError
        For an unknown ``format=`` or, without one, an extension that is not a format.
    """
    known = ", ".join(FORMATS)
    name = safe_name(name)
    if given is not None:
        found = _ALIASES.get(given.lower(), given.lower())
        if found not in FORMATS:
            raise IngestError(f"format={given!r} is not a format files.read reads ({known})")
        return found
    extension = name.rsplit("/", 1)[-1].rpartition(".")[2].lower()
    found = _ALIASES.get(extension, extension)
    if "." not in name.rsplit("/", 1)[-1] or found not in FORMATS:
        raise IngestError(f"cannot tell the format of `{name}`; pass format= ({known})")
    return found


def unpacked_size(data: bytes) -> int | None:
    """Add up what a zip archive unpacks to, from its central directory, without unpacking it.

    Parameters
    ----------
    data
        The file's bytes.

    Returns
    -------
    int or None
        The sum of the entries' uncompressed sizes; `None` when the bytes are no zip archive
        (the reader then gives its own error).
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return sum(item.file_size for item in archive.infolist())
    except zipfile.BadZipFile:
        return None


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

    Returns
    -------
    polars.DataFrame
        The rows.

    Raises
    ------
    IngestError
        For ``sheet=`` on a file without sheets, or ``header_row`` other than 1 on a file
        without a header row.
    FileShapeError
        When the file cannot be parsed; the message names the file and the problem.
    """
    import polars as pl

    name = safe_name(name)
    check_schema(schema)
    if sheet is not None and format not in SPREADSHEETS:
        raise IngestError(f"sheet= applies to spreadsheets; `{name}` is {format}")
    if header_row != 1 and format not in _WITH_HEADER:
        raise IngestError(
            f"header_row= applies to CSV files and spreadsheets; `{name}` is {format}"
        )
    if header_row < 0:
        raise IngestError("header_row= is the 1-based row of the column names, or 0 for none")
    overrides = dict(schema) if schema else None
    if format in SPREADSHEETS:
        # A missing engine is the worker's fault, not the file's: let the ImportError out.
        importlib.import_module("fastexcel")

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
            )
        elif format == "parquet":
            frame = pl.read_parquet(source)
            present = {k: v for k, v in (overrides or {}).items() if k in frame.columns}
            frame = frame.cast(present, strict=True) if present else frame  # ty: ignore[invalid-argument-type]
        elif format == "json":
            frame = pl.read_json(source, schema_overrides=overrides)
        elif format == "ndjson":
            frame = pl.read_ndjson(source, schema_overrides=overrides)
        else:
            frame = pl.read_excel(
                source,
                sheet_name=sheet,
                engine="calamine",
                has_header=header_row > 0,
                read_options={"header_row": header_row - 1} if header_row > 0 else {},
                schema_overrides=overrides,
            )
    except BaseException as exc:  # a reader's own error, a KeyError, or a native panic
        if isinstance(exc, KeyboardInterrupt | SystemExit | GeneratorExit | FileShapeError):
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


def combine(frames: Sequence[pl.DataFrame], names: Sequence[str]) -> pl.DataFrame:
    """Combine the files' rows, filling a column a file lacks with nulls.

    Parameters
    ----------
    frames
        One frame per file, in file order.
    names
        The files' names, for the message.

    Returns
    -------
    polars.DataFrame
        All rows; a column's type is the common type of its files' types.

    Raises
    ------
    FileShapeError
        When a column's types in two files have no common type; names the file and the column.
    """
    import polars as pl

    names = [safe_name(n) for n in names]
    try:
        return pl.concat(frames, how="diagonal_relaxed")
    except pl.exceptions.PolarsError as failure:
        first = failure
    combined = frames[0]
    for frame, name in zip(frames[1:], names[1:], strict=True):
        for column in frame.columns:
            if column not in combined.columns:
                continue
            try:
                pl.concat([combined.select(column), frame.select(column)], how="vertical_relaxed")
            except pl.exceptions.PolarsError:
                raise FileShapeError(
                    f"`{name}`: column {column!r} is {frame.schema[column]}, which does not fit "
                    f"{combined.schema[column]} in the files before it; pass schema= with one "
                    "type for it, or narrow the pattern"
                ) from None
        try:
            combined = pl.concat([combined, frame], how="diagonal_relaxed")
        except pl.exceptions.PolarsError:
            break
    raise FileShapeError(_not_combinable(frames, names, first))


def _not_combinable(
    frames: Sequence[pl.DataFrame], names: Sequence[str], failure: BaseException
) -> str:
    """Say which columns differ in type, and in which files, when no one pair is to blame.

    Parameters
    ----------
    frames
        One frame per file.
    names
        The files' names.
    failure
        What the combination raised.

    Returns
    -------
    str
        The message: columns that differ with their types per file, and the next step.
    """
    kinds: dict[str, dict[str, list[str]]] = {}
    for frame, name in zip(frames, names, strict=True):
        for column, dtype in frame.schema.items():
            kinds.setdefault(column, {}).setdefault(str(dtype), []).append(name)
    differing = [
        f"column {column!r} is "
        + ", ".join(
            f"{dtype} in {', '.join(f'`{n}`' for n in where)}" for dtype, where in found.items()
        )
        for column, found in kinds.items()
        if len(found) > 1
    ]
    detail = "; ".join(differing) or safe_name(_problem(failure))
    return (
        f"the files cannot be combined: {detail}; pass schema= with one type for each such "
        "column, or narrow the pattern"
    )
