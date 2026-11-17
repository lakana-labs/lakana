r"""The whole result of a query as a file: CSV or a spreadsheet, every value exact.

A download is always the whole result, never a page, and it never holds fewer rows or
other values than the result. Both formats are written from the same flattened table
(`file_cells`): a nested value becomes its compact JSON text, a binary value ``\x``
and two lower-case hex digits per byte, a UUID its canonical text, and SQL NULL an
empty field or an empty cell.

- **CSV** (`csv_pieces`) is RFC 4180 in UTF-8 without a byte-order mark, written by
  pyarrow in pieces of 10,000 rows. Values are verbatim: a text starting with ``=`` is
  not escaped, since a spreadsheet user takes xlsx.
- **xlsx** (`write_xlsx`) keeps a number a number only where a spreadsheet holds it
  exactly; a larger integer, a long decimal, NaN and infinity are text. What a sheet
  cannot hold at all (too many rows or columns, a value longer than a cell) is refused
  with `XlsxRefused` before a byte is written, naming the CSV command instead.
"""

from __future__ import annotations

import io
import json
import math
import shlex
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import date, datetime, time
from decimal import Decimal
from typing import BinaryIO

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pa_csv
import xlsxwriter
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send
from xlsxwriter.format import Format
from xlsxwriter.worksheet import Worksheet

from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown

XLSX_MAX_ROWS = 1_048_575
"""The most rows of one sheet below its header row."""

XLSX_MAX_COLUMNS = 16_384
"""The most columns of one sheet."""

XLSX_MAX_CELL_CHARS = 32_767
"""The most characters one cell holds."""

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
"""The media type of a CSV result."""

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
"""The media type of a spreadsheet result."""

CSV_BATCH_ROWS = 10_000
"""The rows of one CSV piece."""

FILE_PIECE_BYTES = 1024 * 1024
"""The bytes of one piece of a finished spreadsheet sent to the client."""

EXACT_INTEGER = 2**53 - 1
"""The largest integer a spreadsheet's number holds exactly, either sign."""

EXACT_DIGITS = 15
"""The most significant digits of a decimal that a spreadsheet's number shows exactly."""

NUMBER_DIGITS_FORMAT = ".16G"
"""How XlsxWriter writes a number into the sheet; a float this does not hold exactly is text."""

_EXTENSIONS = {"arrow": "arrow", "csv": "csv", "xlsx": "xlsx"}
_EXCEL_FIRST_DAY = date(1900, 1, 1)
_DATE_FORMAT = "yyyy-mm-dd"
_TIMESTAMP_FORMAT = "yyyy-mm-dd hh:mm:ss"
_UTC_FORMAT = 'yyyy-mm-dd hh:mm:ss "UTC"'


class XlsxRefused(ValidationError):
    """A result a spreadsheet cannot hold without losing rows, columns or characters.

    Raised before the first byte of the file is written; mapped to 422. The message names
    the command that downloads the result as CSV instead.
    """


def result_file_name(query_id: str, result_format: str) -> str:
    """Name the file of a downloaded result.

    Parameters
    ----------
    query_id
        The query's id.
    result_format
        ``arrow``, ``csv`` or ``xlsx``.

    Returns
    -------
    str
        ``query-{id}.{extension}``.
    """
    return f"query-{query_id}.{_EXTENSIONS[result_format]}"


# --- The flattened cells -----------------------------------------------------------------------


def _is_binary(kind: pa.DataType) -> bool:
    """Say whether a type holds raw bytes.

    Parameters
    ----------
    kind : pyarrow.DataType
        Any type.

    Returns
    -------
    bool
        True for binary, large binary, binary view and fixed-size binary.
    """
    return (
        pa.types.is_binary(kind)
        or pa.types.is_large_binary(kind)
        or pa.types.is_binary_view(kind)
        or pa.types.is_fixed_size_binary(kind)
    )


def _is_nested(kind: pa.DataType) -> bool:
    """Say whether a type is a list, struct, map or union.

    Parameters
    ----------
    kind : pyarrow.DataType
        Any type.

    Returns
    -------
    bool
        True for every type whose value is made of other values.
    """
    return (
        pa.types.is_list(kind)
        or pa.types.is_large_list(kind)
        or pa.types.is_fixed_size_list(kind)
        or pa.types.is_list_view(kind)
        or pa.types.is_large_list_view(kind)
        or pa.types.is_struct(kind)
        or pa.types.is_map(kind)
        or pa.types.is_union(kind)
    )


def _hex(value: bytes) -> str:
    r"""Write bytes as DuckDB prints a blob, every byte escaped.

    Parameters
    ----------
    value
        The bytes.

    Returns
    -------
    str
        ``\x`` and two lower-case hex digits per byte; empty for no bytes.
    """
    if not value:
        return ""
    return "\\x" + value.hex("\\").replace("\\", "\\x")


def _plain(value: object) -> object:
    """Turn one value inside a nested value into what its JSON text holds exactly.

    Parameters
    ----------
    value
        A value `pyarrow` made; a map arrives as a list of key and value pairs.

    Returns
    -------
    object
        A JSON value: bytes as `_hex` text, NaN and infinities as text, and a decimal,
        date, time, UUID or interval as its text.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, bytes):
        return _hex(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)


def _nested_plain(value: object, kind: pa.DataType) -> object:
    """Turn a nested value into JSON values, following its type for maps.

    Parameters
    ----------
    value
        The value `pyarrow` made of one cell.
    kind : pyarrow.DataType
        Its type.

    Returns
    -------
    object
        As `_plain`, with each map entry an object of ``key`` and ``value``.
    """
    if value is None:
        return None
    if pa.types.is_dictionary(kind):
        return _nested_plain(value, kind.value_type)
    if pa.types.is_map(kind):
        pairs = value if isinstance(value, list) else []
        return [
            {
                "key": _nested_plain(key, kind.key_type),
                "value": _nested_plain(item, kind.item_type),
            }
            for key, item in pairs
        ]
    if pa.types.is_struct(kind) and isinstance(value, dict):
        return {field.name: _nested_plain(value.get(field.name), field.type) for field in kind}
    if _is_nested(kind) and not pa.types.is_union(kind) and isinstance(value, list):
        return [_nested_plain(item, kind.value_type) for item in value]
    return _plain(value)


def _as_text(column: pa.ChunkedArray, text: Callable[[object], str]) -> pa.ChunkedArray:
    """Write every value of a column as text, keeping NULL.

    Parameters
    ----------
    column : pyarrow.ChunkedArray
        The column.
    text
        Writes one valid value.

    Returns
    -------
    pyarrow.ChunkedArray
        A large string column.
    """
    return pa.chunked_array(
        [
            pa.array(
                [None if value is None else text(value) for value in chunk.to_pylist()],
                pa.large_string(),
            )
            for chunk in column.chunks
        ],
        pa.large_string(),
    )


def _cell_column(column: pa.ChunkedArray) -> pa.ChunkedArray:
    """Flatten one column into what a file cell holds.

    Parameters
    ----------
    column : pyarrow.ChunkedArray
        A column of the result.

    Returns
    -------
    pyarrow.ChunkedArray
        The column itself when a file writes it as it is, otherwise its text.
    """
    kind = column.type
    if pa.types.is_dictionary(kind):
        return _cell_column(column.cast(kind.value_type))
    if isinstance(kind, pa.UuidType):
        return _as_text(column, _uuid_text)
    if _is_binary(kind):
        return _as_text(column, lambda value: _hex(value) if isinstance(value, bytes) else "")
    if _is_nested(kind):
        return _as_text(column, lambda value: _json_text(_nested_plain(value, kind)))
    if pa.types.is_string_view(kind):
        return column.cast(pa.large_string())
    if pa.types.is_time(kind):
        return column.cast(pa.string())
    if pa.types.is_interval(kind):
        return _as_text(column, _interval_text)
    return column


def _json_text(value: object) -> str:
    """Write a JSON value compactly, in UTF-8 rather than ASCII escapes.

    Parameters
    ----------
    value
        A value `_plain` made.

    Returns
    -------
    str
        The JSON text.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _interval_text(value: object) -> str:
    """Write a month, day and nanosecond interval as a JSON object of its parts.

    Parameters
    ----------
    value
        The interval `pyarrow` made.

    Returns
    -------
    str
        ``{"months":…,"days":…,"nanoseconds":…}``.
    """
    parts = ("months", "days", "nanoseconds")
    return _json_text({part: getattr(value, part, None) for part in parts})


def file_cells(table: pa.Table) -> pa.Table:
    """Flatten a result into the cells of a CSV or spreadsheet file (R4).

    Parameters
    ----------
    table : pyarrow.Table
        The result.

    Returns
    -------
    pyarrow.Table
        The same rows and column names. A nested value is its compact JSON text (UTF-8, not
        ASCII-escaped, a map's entries as objects of ``key`` and ``value``), a binary value
        `_hex` text, a UUID its canonical text, a dictionary its values, a time its text and
        an interval a JSON object of its parts; every other column is unchanged.
    """
    return pa.Table.from_arrays(
        [_cell_column(column) for column in table.columns], names=table.column_names
    )


# --- CSV ---------------------------------------------------------------------------------------


def csv_pieces(table: pa.Table) -> Iterator[bytes]:
    """Write a result as CSV, one piece at a time (R6).

    Parameters
    ----------
    table : pyarrow.Table
        The result.

    Yields
    ------
    bytes
        The header row first, then one piece per `CSV_BATCH_ROWS` rows: RFC 4180 with CRLF row
        ends, UTF-8 without a byte-order mark, NULL as an empty field, values verbatim.
    """
    sink = io.BytesIO()
    options = pa_csv.WriteOptions(include_header=True, eol="\r\n", quoting_style="needed")
    schema = file_cells(table.slice(0, 0)).schema
    with pa_csv.CSVWriter(sink, schema, write_options=options) as writer:
        yield _drain(sink)
        for start in range(0, table.num_rows, CSV_BATCH_ROWS):
            writer.write_table(file_cells(table.slice(start, CSV_BATCH_ROWS)))
            yield _drain(sink)


def _drain(sink: io.BytesIO) -> bytes:
    """Take what a writer put into a buffer so far, and empty it.

    Parameters
    ----------
    sink
        The buffer.

    Returns
    -------
    bytes
        Its contents.
    """
    piece = sink.getvalue()
    sink.seek(0)
    sink.truncate()
    return piece


# --- xlsx --------------------------------------------------------------------------------------


def _instead(query_id: str, environment: str) -> str:
    """Name the command that downloads a refused result as CSV.

    Parameters
    ----------
    query_id
        The query's id.
    environment
        The environment's name; quoted for a shell.

    Returns
    -------
    str
        The sentence's end, ``Download it as CSV instead: …``.
    """
    return (
        f"Download it as CSV instead: lakana query result {query_id} "
        f"--env {shlex.quote(environment)} --format csv --out {result_file_name(query_id, 'csv')}"
    )


def _longest_text(cells: pa.Table) -> tuple[str, int] | None:
    """Find a column name or text value longer than a cell holds.

    Parameters
    ----------
    cells : pyarrow.Table
        The flattened result.

    Returns
    -------
    tuple of (str, int) or None
        The first such column and the length of its longest value, or `None`.
    """
    for name, column in zip(cells.column_names, cells.columns, strict=True):
        longest = len(name)
        if pa.types.is_string(column.type) or pa.types.is_large_string(column.type):
            lengths = pc.call_function("utf8_length", [column])
            values = pc.call_function("max", [lengths]).as_py()
            longest = max(longest, values or 0)
        if longest > XLSX_MAX_CELL_CHARS:
            return name, longest
    return None


def _refuse_what_a_sheet_cannot_hold(cells: pa.Table, query_id: str, environment: str) -> None:
    """Refuse a result one sheet cannot hold whole (R5).

    Parameters
    ----------
    cells : pyarrow.Table
        The flattened result.
    query_id
        The query's id, for the CSV command.
    environment
        The environment's name, for the CSV command.

    Raises
    ------
    XlsxRefused
        For more rows than `XLSX_MAX_ROWS`, more columns than `XLSX_MAX_COLUMNS`, or a value
        longer than `XLSX_MAX_CELL_CHARS`.
    """
    instead = _instead(query_id, environment)
    if cells.num_rows > XLSX_MAX_ROWS:
        raise XlsxRefused(
            f"This result has {cells.num_rows:,} rows; a spreadsheet holds at most "
            f"{XLSX_MAX_ROWS:,} below its header row. {instead}"
        )
    if cells.num_columns > XLSX_MAX_COLUMNS:
        raise XlsxRefused(
            f"This result has {cells.num_columns:,} columns; a spreadsheet holds at most "
            f"{XLSX_MAX_COLUMNS:,}. {instead}"
        )
    too_long = _longest_text(cells)
    if too_long is not None:
        column, length = too_long
        raise XlsxRefused(
            f"A value in column {shown(column)!r} is {length:,} characters long; a spreadsheet "
            f"cell holds at most {XLSX_MAX_CELL_CHARS:,}. {instead}"
        )


_Put = Callable[[Worksheet, int, int, object], object]
"""Writes one valid value into a cell: the sheet, row, column and value."""


def _put_text(sheet: Worksheet, row: int, col: int, value: object) -> object:
    """Write a value as text, never as a formula.

    Parameters
    ----------
    sheet
        The sheet.
    row
        The row, from 0.
    col
        The column, from 0.
    value
        The value; its `str` is written.

    Returns
    -------
    object
        What `write_string` returns.
    """
    return sheet.write_string(row, col, value if isinstance(value, str) else str(value))


def _put_integer(sheet: Worksheet, row: int, col: int, value: object) -> object:
    """Write an integer as a number when a sheet holds it exactly, otherwise as text.

    Parameters
    ----------
    sheet
        The sheet.
    row
        The row, from 0.
    col
        The column, from 0.
    value
        The integer.

    Returns
    -------
    object
        What the write returns.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return _put_text(sheet, row, col, value)
    if -EXACT_INTEGER <= value <= EXACT_INTEGER:
        return sheet.write_number(row, col, value)
    return sheet.write_string(row, col, str(value))


def _put_float(sheet: Worksheet, row: int, col: int, value: object) -> object:
    """Write a float as a number when the sheet holds it exactly, otherwise as its text.

    NaN and the infinities are text, and so is a double whose 16 significant digits
    (`NUMBER_DIGITS_FORMAT`) do not read back as the same double.

    Parameters
    ----------
    sheet
        The sheet.
    row
        The row, from 0.
    col
        The column, from 0.
    value
        The float.

    Returns
    -------
    object
        What the write returns.
    """
    if not isinstance(value, float):
        return _put_text(sheet, row, col, value)
    number = value
    if math.isnan(number):
        return sheet.write_string(row, col, "NaN")
    if math.isinf(number):
        return sheet.write_string(row, col, "Infinity" if number > 0 else "-Infinity")
    written = float(format(number, NUMBER_DIGITS_FORMAT))
    if written != number:
        # XlsxWriter writes a number's 16 significant digits; this double needs 17.
        return sheet.write_string(row, col, repr(number))
    return sheet.write_number(row, col, number)


def _put_decimal(sheet: Worksheet, row: int, col: int, value: object) -> object:
    """Write a decimal as a number up to 15 significant digits, otherwise as its text.

    Parameters
    ----------
    sheet
        The sheet.
    row
        The row, from 0.
    col
        The column, from 0.
    value
        The `Decimal`.

    Returns
    -------
    object
        What the write returns.
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        return _put_text(sheet, row, col, value)
    if len(value.normalize().as_tuple().digits) > EXACT_DIGITS:
        return sheet.write_string(row, col, str(value))
    return sheet.write_number(row, col, float(value))


def _put_moment(number_format: Format) -> _Put:
    """Make a writer of dates or timestamps in a number format.

    A value before 1900, which a spreadsheet has no date for, is written as ISO text.

    Parameters
    ----------
    number_format
        The workbook's format for the column.

    Returns
    -------
    callable
        Writes one `date` or naive `datetime`.
    """

    def put(sheet: Worksheet, row: int, col: int, value: object) -> object:
        """Write one date or timestamp.

        Parameters
        ----------
        sheet
            The sheet.
        row
            The row, from 0.
        col
            The column, from 0.
        value
            A `date` or a naive `datetime` (in UTC for a column with a time zone).

        Returns
        -------
        object
            What the write returns.
        """
        if not isinstance(value, date):
            return _put_text(sheet, row, col, value)
        day = value.date() if isinstance(value, datetime) else value
        if day < _EXCEL_FIRST_DAY:
            text = value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
            return sheet.write_string(row, col, text)
        moment = value if isinstance(value, datetime) else datetime.combine(value, time())
        return sheet.write_datetime(row, col, moment, number_format)

    return put


def _put_boolean(sheet: Worksheet, row: int, col: int, value: object) -> object:
    """Write a boolean.

    Parameters
    ----------
    sheet
        The sheet.
    row
        The row, from 0.
    col
        The column, from 0.
    value
        The boolean.

    Returns
    -------
    object
        What `write_boolean` returns.
    """
    return sheet.write_boolean(row, col, bool(value))


def _sheet_column(
    column: pa.ChunkedArray, formats: dict[str, Format]
) -> tuple[pa.ChunkedArray, _Put]:
    """Choose how a flattened column is written into a sheet.

    Parameters
    ----------
    column : pyarrow.ChunkedArray
        A column of `file_cells`.
    formats
        The workbook's number formats, by name: ``date``, ``timestamp`` and ``utc``.

    Returns
    -------
    tuple
        The column as it is read row by row, and the writer of one of its values. A timestamp
        with a time zone is read in UTC; one in nanoseconds that a microsecond cannot hold is
        written as its text, and so is any type without a writer of its own.
    """
    kind = column.type
    if pa.types.is_integer(kind):
        return column, _put_integer
    if pa.types.is_floating(kind):
        return column.cast(pa.float64()), _put_float
    if pa.types.is_decimal(kind):
        return column, _put_decimal
    if pa.types.is_boolean(kind):
        return column, _put_boolean
    if pa.types.is_date(kind):
        return column, _put_moment(formats["date"])
    if pa.types.is_timestamp(kind):
        try:
            micro = column.cast(pa.timestamp("us"), safe=True)
        except pa.ArrowInvalid:
            return column.cast(pa.string()), _put_text
        return micro, _put_moment(formats["timestamp" if kind.tz is None else "utc"])
    if pa.types.is_duration(kind):
        return column.cast(pa.int64()), _put_integer
    return column, _put_text


def write_xlsx(table: pa.Table, target: BinaryIO, *, query_id: str, environment: str) -> None:
    """Write a result as one spreadsheet, or refuse it before writing anything (R4, R5).

    The workbook is built with XlsxWriter's constant memory mode, one row at a time; its
    temporary files follow `TMPDIR`. The header row holds the column names.

    Parameters
    ----------
    table : pyarrow.Table
        The result.
    target
        A seekable binary file the finished workbook is written to.
    query_id
        The query's id, for the refusal's CSV command.
    environment
        The environment's name, for the refusal's CSV command.

    Raises
    ------
    XlsxRefused
        If one sheet cannot hold the result whole; nothing is written to `target`.
    """
    cells = file_cells(table)
    _refuse_what_a_sheet_cannot_hold(cells, query_id, environment)
    workbook = xlsxwriter.Workbook(target, {"constant_memory": True})
    try:
        sheet = workbook.add_worksheet()
        formats: dict[str, Format] = {
            "date": workbook.add_format({"num_format": _DATE_FORMAT}),
            "timestamp": workbook.add_format({"num_format": _TIMESTAMP_FORMAT}),
            "utc": workbook.add_format({"num_format": _UTC_FORMAT}),
        }
        columns = [_sheet_column(column, formats) for column in cells.columns]
        for col, name in enumerate(cells.column_names):
            sheet.write_string(0, col, name)
        row = 1
        for start in range(0, cells.num_rows, CSV_BATCH_ROWS):
            values = [column.slice(start, CSV_BATCH_ROWS).to_pylist() for column, _ in columns]
            puts = [put for _, put in columns]
            for offset in range(len(values[0]) if values else 0):
                for col, put in enumerate(puts):
                    value = values[col][offset]
                    if value is not None:
                        put(sheet, row, col, value)
                row += 1
    finally:
        workbook.close()


def _uuid_text(value: object) -> str:
    """Write a UUID in its canonical text.

    Parameters
    ----------
    value
        A `uuid.UUID` or its 16 bytes.

    Returns
    -------
    str
        The canonical text.
    """
    if isinstance(value, bytes):
        return str(uuid.UUID(bytes=value))
    return str(value)


# --- The response ------------------------------------------------------------------------------


def file_pieces(target: BinaryIO) -> Iterator[bytes]:
    """Read a finished file from its start in pieces of `FILE_PIECE_BYTES`.

    Parameters
    ----------
    target
        The file `write_xlsx` wrote.

    Yields
    ------
    bytes
        The next piece; never empty.
    """
    target.seek(0)
    while piece := target.read(FILE_PIECE_BYTES):
        yield piece


def _next_piece(pieces: Iterator[bytes]) -> bytes | None:
    """Make the next piece of a file, on a worker thread.

    Parameters
    ----------
    pieces
        The file's pieces.

    Returns
    -------
    bytes or None
        The piece, or `None` at the end.
    """
    return next(pieces, None)


async def _body(pieces: Iterator[bytes]) -> AsyncIterator[bytes]:
    """Send a file's pieces, each made in the threadpool.

    Parameters
    ----------
    pieces
        The file's pieces; reading, converting or writing one may take a while.

    Yields
    ------
    bytes
        The next piece.
    """
    while (piece := await run_in_threadpool(_next_piece, pieces)) is not None:
        yield piece


class ResultFileResponse(StreamingResponse):
    """A whole query result as a file, streamed, with what it is and how big the run said it was.

    No `Content-Length` is promised: CSV and xlsx are made while they are sent. The
    response closes what it read from and gives back its file slot exactly once, when the body
    ends, fails, or is never sent because the client went away.

    Parameters
    ----------
    pieces
        The file's pieces.
    media_type
        Its media type.
    file_name
        The name `Content-Disposition` offers.
    rows
        The run's `result_rows`; the `X-Result-Rows` header is left out when `None`.
    arrow_bytes
        The run's `result_bytes`; the `X-Result-Arrow-Bytes` header is left out when `None`.
    close
        Closes what the pieces are read from (a storage iterator, a temporary file).
    release
        Gives back the file slot the route took, or does nothing for a result that takes none.
    """

    def __init__(
        self,
        pieces: Iterator[bytes],
        *,
        media_type: str,
        file_name: str,
        rows: int | None,
        arrow_bytes: int | None,
        close: Callable[[], object],
        release: Callable[[], None],
    ) -> None:
        headers = {"Content-Disposition": f'attachment; filename="{file_name}"'}
        if rows is not None:
            headers["X-Result-Rows"] = str(rows)
        if arrow_bytes is not None:
            headers["X-Result-Arrow-Bytes"] = str(arrow_bytes)
        super().__init__(_body(pieces), media_type=media_type, headers=headers)
        self._pieces = pieces
        self._close = close
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the response, then close its source and give back its slot.

        Parameters
        ----------
        scope
            The ASGI scope.
        receive
            The ASGI receive channel.
        send
            The ASGI send channel.
        """
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                close = getattr(self._pieces, "close", None)
                if close is not None:
                    close()
                self._close()
            finally:
                self._release()
