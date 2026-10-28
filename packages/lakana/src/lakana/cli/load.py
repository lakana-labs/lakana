"""`lakana load`: load a local file into a table of the bronze layer, once.

For exploration, testing and teaching. Anything that should run again belongs in an asset; the
server's load route therefore writes only into `bronze` and refuses every other layer before it
reads the body. This command holds no copy of that rule: it shows the server's refusal as it is.

CSV, Parquet and Arrow files and newline-delimited JSON are read with pyarrow, which every
install has; a JSON array and spreadsheets are read by the files helper's reader, which needs
the ``assets`` extra (polars, fastexcel).
"""

from __future__ import annotations

import mmap
import struct
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.ipc as ipc
import pyarrow.json as pajson
import pyarrow.parquet as pq
import typer

from lakana._arrow import cast_timestamp_ns_columns_to_us, normalize_view_columns, schema_to_columns
from lakana.cli import _files, _output, _parquet_pages
from lakana.cli._break_glass import BREAK_GLASS, REASON, break_glass_reason
from lakana.cli._state import CliError, confirm, resolve_environment, state_of
from lakana.errors import NotFound, ValidationError

_EXTENSIONS = {
    "csv": "csv",
    "parquet": "parquet",
    "pq": "parquet",
    "arrow": "arrow",
    "ipc": "arrow",
    "feather": "arrow",
    "json": "json",
    "ndjson": "ndjson",
    "jsonl": "ndjson",
    "xlsx": "xlsx",
    "xlsm": "xlsm",
    "xlsb": "xlsb",
    "xls": "xls",
    "ods": "ods",
}
FORMATS = ("csv", "parquet", "arrow", "json", "ndjson", "xlsx", "xlsm", "xlsb", "xls", "ods")
"""The formats `lakana load` reads."""
_WITH_EXTRA = frozenset({"json", "xlsx", "xlsm", "xlsb", "xls", "ods"})
"""Read by the files helper's reader, which needs the assets extra."""
_SPREADSHEETS = frozenset({"xlsx", "xlsm", "xlsb", "xls", "ods"})
_MIB = 1024 * 1024
_BATCH_ROWS = 1024
"""The most rows decoded at a time from a Parquet file, so a read stops soon after the limit."""


def bronze_name(table: str) -> str:
    """Give a bare table name its layer: `customers` means `bronze.customers`.

    Parameters
    ----------
    table
        TABLE as given.

    Returns
    -------
    str
        The dotted target; a dotted name is kept as it is (the server judges its layer).
    """
    return table if "." in table else f"bronze.{table}"


def _format(path: Path, given: str | None) -> str:
    """Decide the file's format from `--format` or its extension.

    Parameters
    ----------
    path
        The file.
    given
        `--format`, or `None`.

    Returns
    -------
    str
        One of `FORMATS`.

    Raises
    ------
    CliError
        For an unknown format (exit 2).
    """
    if given is not None:
        found = _EXTENSIONS.get(given.lower())
        if found is None:
            raise CliError(f"--format must be one of {', '.join(FORMATS)}; got {given!r}")
        return found
    found = _EXTENSIONS.get(path.suffix.lower().lstrip("."))
    if found is None:
        raise CliError(
            f"Can't tell the format of {_output.inline(path.name)}; pass --format "
            f"({', '.join(FORMATS)})"
        )
    return found


def _csv(path: Path, header_row: int) -> pa.Table:
    """Read a CSV file with pyarrow, inferring each column's type over the whole file.

    As `lakana table append` reads it. Text decodes to about its own size, and the file's size
    is checked against the upload limit before it is read, so it is read whole.

    Parameters
    ----------
    path
        The file.
    header_row
        The 1-based row of the column names; ``0`` for none (columns ``column_1``, ...).

    Returns
    -------
    pa.Table
        The rows.
    """
    data = path.read_bytes()
    _files.strict_utf8(path, data)
    read = pacsv.ReadOptions(
        skip_rows=max(header_row - 1, 0), autogenerate_column_names=not header_row
    )
    convert = pacsv.ConvertOptions(strings_can_be_null=True, null_values=[""])
    table = pacsv.read_csv(pa.BufferReader(data), read_options=read, convert_options=convert)
    if not header_row:
        table = table.rename_columns([f"column_{i + 1}" for i in range(table.num_columns)])
    return table


def _ndjson(path: Path, data: bytes) -> pa.Table:
    """Read newline-delimited JSON with pyarrow, after the helpers' encoding and depth checks.

    Each field's type is inferred over the whole file; like a CSV file, it is read whole.

    Parameters
    ----------
    path
        The file, for messages.
    data
        Its bytes.

    Returns
    -------
    pa.Table
        The rows.

    Raises
    ------
    CliError
        For text that is not UTF-8 or nests deeper than the helpers read (exit 8).
    """
    from lakana.ingest._formats import MAX_NESTING, nested_deeper

    _files.strict_utf8(path, data)
    if nested_deeper(data):
        raise CliError(
            f"{_output.inline(path.name)} nests arrays and objects deeper than {MAX_NESTING} "
            "levels, which Lakana does not read; flatten it first",
            8,
            kind="FileError",
        )
    return pajson.read_json(pa.BufferReader(data))


def _with_extra(path: Path, data: bytes, fmt: str, sheet: str | None, header_row: int) -> pa.Table:
    """Read a JSON array or a spreadsheet with the files helper's reader.

    Parameters
    ----------
    path
        The file.
    data
        Its bytes.
    fmt
        Its format.
    sheet
        `--sheet`.
    header_row
        `--header-row`.

    Returns
    -------
    pa.Table
        The rows.

    Raises
    ------
    CliError
        Without the assets extra (exit 2), or when the file cannot be parsed (exit 8).
    """
    from lakana.ingest import _formats
    from lakana.ingest._errors import IngestError

    try:
        import polars  # ruff: ignore[unused-import] (presence check)

        if fmt in _SPREADSHEETS:
            import fastexcel  # ruff: ignore[unused-import] (presence check)
    except ImportError:
        raise CliError(
            f"reading .{fmt} needs the assets extra: pip install 'lakana[assets]'"
        ) from None
    if fmt in _formats.ZIPPED:
        counted = _formats.unpacked_size(data, 16 * len(data) + _MIB)
        if counted is not None and counted[1]:
            raise CliError(
                f"{_output.inline(path.name)} unpacks to more than 16 times its size; split the "
                "workbook",
                8,
                kind="FileError",
            )
    try:
        frame = _formats.parse(data, name=path.name, format=fmt, sheet=sheet, header_row=header_row)
    except IngestError as exc:
        raise CliError(str(exc), 8, kind="FileError") from None
    return frame.to_arrow()


def _decoded(batch: pa.RecordBatch) -> pa.RecordBatch:
    """Replace each dictionary column of a batch by its values, which a table stores.

    Parameters
    ----------
    batch
        A few rows.

    Returns
    -------
    pa.RecordBatch
        The rows with plain columns.
    """
    return pa.RecordBatch.from_arrays(
        [
            column.cast(column.type.value_type) if pa.types.is_dictionary(column.type) else column
            for column in batch.columns
        ],
        names=batch.schema.names,
    )


def _plain(schema: pa.Schema) -> pa.Schema:
    """Give each dictionary column of a schema its values' type, which a table stores.

    Parameters
    ----------
    schema
        The schema as read.

    Returns
    -------
    pa.Schema
        The schema with plain columns.
    """
    return pa.schema(
        [
            field.with_type(field.type.value_type) if pa.types.is_dictionary(field.type) else field
            for field in schema
        ]
    )


def _column_size(column: pa.Array) -> int:
    """Count the bytes a column takes once its dictionary is decoded, without decoding it.

    A dictionary column takes little room encoded and may take far more decoded (one long
    value behind many small indices), so its decoded size is added up from its values' lengths.

    Parameters
    ----------
    column
        A column of a batch.

    Returns
    -------
    int
        Its size in bytes; a text view column as plain text; any other column as it is.
    """
    if pa.types.is_string_view(column.type) or pa.types.is_binary_view(column.type):
        return _view_size(column)
    if not pa.types.is_dictionary(column.type):
        return column.nbytes
    values = column.dictionary
    rows = len(column)
    bitmap = (rows + 7) // 8
    if pa.types.is_large_string(values.type) or pa.types.is_large_binary(values.type):
        offsets = 8 * (rows + 1)
    elif pa.types.is_string(values.type) or pa.types.is_binary(values.type):
        offsets = 4 * (rows + 1)
    else:
        try:
            return rows * max(values.type.bit_width // 8, 1) + bitmap
        except ValueError:
            return rows * values.nbytes + bitmap
    lengths = pc.take(pc.binary_length(values), column.indices)  # ty: ignore[unresolved-attribute]
    return (pc.sum(lengths).as_py() or 0) + offsets + bitmap  # ty: ignore[unresolved-attribute]


def _view_size(column: pa.Array) -> int:
    """Count the bytes a text view column takes once converted to plain text.

    Many views can point at one long value, which the buffers hold once, so the lengths are
    read from the views themselves (each 16 bytes, starting with its value's length).

    Parameters
    ----------
    column
        A `string_view` or `binary_view` column.

    Returns
    -------
    int
        Its values' lengths added up, with offsets and a validity bitmap.
    """
    rows = len(column)
    views = pa.Array.from_buffers(
        pa.int32(), 4 * rows, [None, column.buffers()[1]], offset=4 * column.offset
    )
    lengths = pc.if_else(column.is_valid(), views[::4], 0)  # ty: ignore[unresolved-attribute]
    total = pc.sum(lengths).as_py() or 0  # ty: ignore[unresolved-attribute]
    return total + 4 * (rows + 1) + (rows + 7) // 8


class _Budget:
    """Decoded rows read so far, refused as soon as they pass the upload limit.

    Parameters
    ----------
    path
        The file, for the message.
    limit
        The upload limit, or `None` when the server does not say.
    """

    def __init__(self, path: Path, limit: int | None) -> None:
        self.path = path
        self.limit = limit
        self.size = 0
        self.kept: list[pa.RecordBatch] = []

    def rows(self, per_row: int) -> int:
        """Say how many rows a batch may hold so that, at worst, it fits in what is left.

        Parameters
        ----------
        per_row
            The most bytes one row can take once decoded.

        Returns
        -------
        int
            Between 1 and `_BATCH_ROWS`; `_BATCH_ROWS` without a limit.
        """
        if self.limit is None:
            return _BATCH_ROWS
        return min(max((self.limit - self.size) // per_row, 1), _BATCH_ROWS)

    def add(self, batch: pa.RecordBatch) -> None:
        """Count a batch, refusing it before its dictionary columns are decoded if it can't fit.

        Parameters
        ----------
        batch
            The rows as read.
        """
        size = sum(map(_column_size, batch.columns))
        _check_size(self.path, self.size + size, self.limit)
        self.size += size
        self.kept.append(_decoded(batch))

    def table(self, schema: pa.Schema) -> pa.Table:
        """Join the rows read.

        Parameters
        ----------
        schema
            The batches' schema as read.

        Returns
        -------
        pa.Table
            The rows, with plain columns.
        """
        return pa.Table.from_batches(self.kept, _plain(schema))


def _slot(buf: bytes, table: int, index: int) -> int | None:
    """Find a field of a FlatBuffers table.

    Parameters
    ----------
    buf
        The encoded bytes.
    table
        The table's position.
    index
        The field's index in the table's declaration.

    Returns
    -------
    int or None
        The field's position, or `None` when the table does not have it.
    """
    vtable = table - struct.unpack_from("<i", buf, table)[0]
    at = 4 + 2 * index
    if at + 2 > struct.unpack_from("<H", buf, vtable)[0]:
        return None
    offset = struct.unpack_from("<H", buf, vtable + at)[0]
    return table + offset if offset else None


def _child(buf: bytes, table: int, index: int) -> int | None:
    """Follow a FlatBuffers table field to the table it points at.

    Parameters
    ----------
    buf
        The encoded bytes.
    table
        The table's position.
    index
        The field's index in the table's declaration.

    Returns
    -------
    int or None
        The child table's position, or `None` when the field is absent.
    """
    field = _slot(buf, table, index)
    return None if field is None else field + struct.unpack_from("<I", buf, field)[0]


def _arrow_compressed(path: Path) -> bool:
    """Tell, from the messages' headers alone, whether an Arrow IPC file is compressed.

    A compressed batch is decompressed to its full size when it is read, so a small file can
    take far more memory than the upload limit before anything can be measured. The server
    refuses compressed IPC for the same reason. The check reads each record batch's (or
    dictionary batch's) `compression` field from its FlatBuffers header, never the body:
    `Message.header` is field 2, `DictionaryBatch.data` field 1, `RecordBatch.compression`
    field 3 of the Arrow format.

    Parameters
    ----------
    path
        The file, in the file or the stream format.

    Returns
    -------
    bool
        Whether any batch declares compression.
    """
    with pa.memory_map(str(path)) as source:
        source.seek(8 if source.read(6) == b"ARROW1" else 0)
        for message in ipc.MessageReader.open_stream(source):
            if message.type not in ("record batch", "dictionary"):
                continue
            buf = message.metadata.to_pybytes()
            batch = _child(buf, struct.unpack_from("<I", buf, 0)[0], 2)
            if batch is not None and message.type == "dictionary":
                batch = _child(buf, batch, 1)
            if batch is not None and _slot(buf, batch, 3) is not None:
                return True
    return False


def _arrow(path: Path, limit: int | None) -> pa.Table:
    """Read an uncompressed Arrow IPC file or stream batch by batch.

    Parameters
    ----------
    path
        The file.
    limit
        The upload limit the batches must stay within, or `None`.

    Returns
    -------
    pa.Table
        The rows.

    Raises
    ------
    CliError
        For a compressed file (exit 8).
    """
    if _arrow_compressed(path):
        raise CliError(
            f"{_output.inline(path.name)} is compressed, which Lakana does not read: write it "
            "uncompressed (or as Parquet) and load it again",
            8,
            kind="FileError",
        )
    with pa.memory_map(str(path)) as source:
        try:
            reader = ipc.open_file(source)
            batches: Iterable[pa.RecordBatch] = (
                reader.get_batch(i) for i in range(reader.num_record_batches)
            )
        except pa.ArrowInvalid:
            source.seek(0)
            reader = ipc.open_stream(source)
            batches = reader
        budget = _Budget(path, limit)
        for batch in batches:
            budget.add(batch)
        return budget.table(reader.schema)


def _parquet(path: Path, limit: int | None) -> pa.Table:
    """Read a Parquet file row group by row group, bounded by its metadata and page headers.

    A column with more than one value a row (a nested type, or an extension type stored as
    one, such as a tensor) is refused first: a table stores none, and its rows have no bound.
    The whole file is then estimated from them. Then each row group is read in batches of only
    as many rows as fit in what is left of the limit, counting each row at its worst: a text
    column's value at the size of its largest page, which no value decodes larger than (one
    value repeated by a dictionary or by shared prefixes takes only a few bytes in the file).

    Parameters
    ----------
    path
        The file.
    limit
        The upload limit the rows must stay within, or `None`.

    Returns
    -------
    pa.Table
        The rows.
    """
    with pq.ParquetFile(path) as parquet:
        schema = _plain(parquet.schema_arrow)
        metadata = parquet.metadata
        repeated = [
            metadata.schema.column(i).path
            for i in range(metadata.num_columns)
            if metadata.schema.column(i).max_repetition_level > 0
        ]
        nested = [
            field
            for field in schema
            if pa.types.is_nested(field.type)
            or any(leaf == field.name or leaf.startswith(f"{field.name}.") for leaf in repeated)
        ]
        _storable(path, pa.schema(nested).empty_table())
        pages = _page_headers(path, metadata)
        widths = [_width(field) for field in schema]
        text = [
            metadata.schema.column(i).physical_type == "BYTE_ARRAY"
            for i in range(metadata.num_columns)
        ]
        _check_size(path, _parquet_size(metadata, widths, pages), limit)
        budget = _Budget(path, limit)
        for group, chunks in enumerate(pages):
            per_row = sum(
                chunk.largest + 4 if is_text else width
                for chunk, is_text, width in zip(chunks, text, widths, strict=True)
            )
            for batch in parquet.iter_batches(budget.rows(per_row), row_groups=[group]):
                budget.add(batch)
        return budget.table(parquet.schema_arrow)


def _width(field: pa.Field) -> int:
    """Give the bytes a row of a column takes at least: its width, or 4 (offsets) for text.

    Parameters
    ----------
    field
        The column, with a plain type.

    Returns
    -------
    int
        The bytes.
    """
    try:
        return max(field.type.bit_width // 8, 1)
    except ValueError:
        return 4


def _page_headers(path: Path, metadata: pq.FileMetaData) -> list[list[_parquet_pages.ChunkPages]]:
    """Read the page headers of every column chunk, as the reader will find them.

    A chunk starts at its dictionary page if it has one, else at its first data page, and
    spans its compressed size, plus up to 100 bytes while values are missing, as the reader
    reads the files of old writers (`_parquet_pages.chunk_pages`).

    Parameters
    ----------
    path
        The file.
    metadata
        Its metadata.

    Returns
    -------
    list of list of ChunkPages
        Per row group, per column.
    """
    with path.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as buf:
        found: list[list[_parquet_pages.ChunkPages]] = []
        for index in range(metadata.num_row_groups):
            group = metadata.row_group(index)
            chunks: list[_parquet_pages.ChunkPages] = []
            for column in (group.column(i) for i in range(group.num_columns)):
                start = column.data_page_offset
                if column.has_dictionary_page and column.dictionary_page_offset:
                    start = min(start, column.dictionary_page_offset)
                chunks.append(
                    _parquet_pages.chunk_pages(
                        buf, start, column.total_compressed_size, column.num_values
                    )
                )
            found.append(chunks)
        return found


def _parquet_size(
    metadata: pq.FileMetaData, widths: list[int], pages: list[list[_parquet_pages.ChunkPages]]
) -> int:
    """Estimate how many bytes a Parquet file's rows take once read.

    A small file can hold many rows of repeated values, which its encoding and compression
    shrink to almost nothing; reading it would build the whole table in memory. The estimate is
    the largest of the column chunks' uncompressed sizes (as the metadata gives them and as the
    page headers add up) and the rows times the columns' widths.

    Parameters
    ----------
    metadata
        The file's metadata.
    widths
        Each column's width (`_width`).
    pages
        The chunks' page headers (`_page_headers`).

    Returns
    -------
    int
        The estimated size in bytes.
    """
    chunks = sum(
        group.column(index).total_uncompressed_size
        for group in (metadata.row_group(i) for i in range(metadata.num_row_groups))
        for index in range(group.num_columns)
    )
    walked = sum(chunk.total for group in pages for chunk in group)
    return max(chunks, walked, metadata.num_rows * sum(widths))


def read_file(
    path: Path, fmt: str, *, sheet: str | None, header_row: int, limit: int | None = None
) -> pa.Table:
    """Read a local file into an Arrow table the platform can store.

    Parameters
    ----------
    path
        The file.
    fmt
        One of `FORMATS`.
    sheet
        `--sheet` (spreadsheets only).
    header_row
        `--header-row` (CSV and spreadsheets only).
    limit
        The server's upload limit: a Parquet or Arrow file is read in pieces and refused as soon
        as its decoded rows pass it (a Parquet file is first estimated from its metadata and page
        headers); a
        text file, whose size was checked before, is measured once read, by the caller.

    Returns
    -------
    pa.Table
        The rows, with view and nanosecond columns converted.

    Raises
    ------
    CliError
        For an option the format does not take, an unreadable, compressed Arrow or empty file,
        rows over the limit, a repeated column name, an empty column, or a column type a table
        cannot store.
    """
    if sheet is not None and fmt not in _SPREADSHEETS:
        raise CliError(f"--sheet applies to spreadsheets; {_output.inline(path.name)} is {fmt}")
    if header_row != 1 and fmt not in _SPREADSHEETS | {"csv"}:
        raise CliError(
            f"--header-row applies to CSV files and spreadsheets; {_output.inline(path.name)} "
            f"is {fmt}"
        )
    if header_row < 0:
        raise CliError("--header-row is the 1-based row of the column names, or 0 for none")
    try:
        if fmt == "csv":
            table = _csv(path, header_row)
        elif fmt == "parquet":
            table = _parquet(path, limit)
        elif fmt == "arrow":
            table = _arrow(path, limit)
        elif fmt == "ndjson":
            table = _ndjson(path, path.read_bytes())
        else:
            table = _with_extra(path, path.read_bytes(), fmt, sheet, header_row)
    except (pa.ArrowException, OSError, struct.error, _parquet_pages.PageHeaderError) as exc:
        raise CliError(
            f"Could not read {_output.inline(path.name)} as {fmt}: {_output.inline(str(exc))}",
            8,
            kind="FileError",
        ) from None
    if not table.num_columns:
        raise CliError(
            f"{_output.inline(path.name)} has no columns; a load needs at least one column and "
            "one row: add them and load it again",
            8,
            kind="FileError",
        )
    if not table.num_rows and fmt not in ("parquet", "arrow"):
        raise CliError(
            f"{_output.inline(path.name)} has no rows, so there is nothing to load and no column "
            "types to infer; add rows and load it again",
            8,
            kind="FileError",
        )
    seen: set[str] = set()
    for name in table.column_names:
        if name in seen:
            raise CliError(
                f"{_output.inline(path.name)} has the column `{_output.inline(name)}` more than "
                "once; rename one and load it again",
                8,
                kind="FileError",
            )
        seen.add(name)
    for field in table.schema:
        if pa.types.is_null(field.type):
            raise CliError(
                f"{_output.inline(path.name)}: column `{_output.inline(field.name)}` has no "
                "values in any row, so its type cannot be inferred; fill it or drop it, then load "
                "it again",
                8,
                kind="FileError",
            )
    return _storable(path, table)


def _storable(path: Path, table: pa.Table) -> pa.Table:
    """Convert view and nanosecond columns, and refuse a column type a table cannot store.

    Parameters
    ----------
    path
        The file, for the message.
    table
        The rows.

    Returns
    -------
    pa.Table
        The rows with view and nanosecond columns converted.

    Raises
    ------
    CliError
        For a column type a table cannot store (exit 8).
    """
    table = normalize_view_columns(cast_timestamp_ns_columns_to_us(table))
    try:
        schema_to_columns(table.schema)
    except ValidationError as exc:
        raise CliError(
            f"{_output.inline(path.name)}: {str(exc).rstrip('.')}; convert the column before "
            "loading",
            8,
            kind="FileError",
        ) from None
    return table


def _check_size(path: Path, size: int, limit: int | None) -> None:
    """Refuse a file larger than one upload may be.

    Parameters
    ----------
    path
        The file.
    size
        What would be sent, or the file's size.
    limit
        The server's upload limit; `None` when the server does not say (its 413 applies).

    Raises
    ------
    CliError
        Exit 2, naming the limit and the way to load the file instead.
    """
    if limit is None or size <= limit:
        return
    shown = f"{limit / _MIB:g} MiB" if limit % _MIB == 0 else f"{limit} bytes"
    raise CliError(
        f"{_output.inline(path.name)} ({size} bytes) is larger than one upload may be "
        f"({shown}); load it with a Python asset and a directory or object-store connection"
    )


def _ipc_size(table: pa.Table) -> int:
    """Count the bytes the table takes as the Arrow stream the SDK sends.

    Parameters
    ----------
    table
        The rows.

    Returns
    -------
    int
        The stream's size.
    """
    sink = pa.MockOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.size()


def load(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Argument(help="The local file: CSV, Parquet, Arrow, JSON, NDJSON or a spreadsheet."),
    ],
    table: Annotated[
        str, typer.Argument(help="The bronze table; a bare name means bronze.<name>.")
    ],
    env: Annotated[
        str | None, typer.Option("--env", help="Environment (default: the profile's).")
    ] = None,
    write: Annotated[
        str,
        typer.Option(
            "--write",
            help="replace (the default) or append, once the table exists; a first load creates it.",
        ),
    ] = "replace",
    file_format: Annotated[
        str | None,
        typer.Option("--format", help="The file's format, if the extension doesn't say."),
    ] = None,
    sheet: Annotated[
        str | None, typer.Option("--sheet", help="A spreadsheet's sheet (default: the first).")
    ] = None,
    header_row: Annotated[
        int,
        typer.Option("--header-row", help="Row of the column names in a CSV or sheet; 0 for none."),
    ] = 1,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Load FILE into TABLE of the bronze layer, once.

    Shows the schema and the row count and asks before it writes. A first load creates the
    table; later loads replace its rows or, with --write append, add to them, and fail
    clearly if someone changed the table in between. For anything that should run again,
    write a Python asset instead.

    Parameters
    ----------
    ctx
        The Typer context.
    file
        The local file.
    table
        The target; `name` means `bronze.name`.
    env
        `--env`.
    write
        `--write`: ``replace`` or ``append``.
    file_format
        `--format`.
    sheet
        `--sheet`.
    header_row
        `--header-row`.
    yes
        `--yes`: skip the question; required, with break glass, in a protected environment.
    break_glass
        `--break-glass`.
    reason
        `--reason`.

    Raises
    ------
    CliError
        For a bad option, an unreadable file, a file over the upload limit, a protected
        environment without --yes and break glass, or a declined confirmation.
    """
    if write not in ("replace", "append"):
        raise CliError(f"--write must be replace or append; got {write!r}")
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    target = bronze_name(table)
    fmt = _format(file, file_format)
    if not file.is_file():
        raise CliError(f"No such file: {_output.inline(str(file))}", 8, kind="FileError")
    client = state.client()
    if client.environments.get(environment).is_protected and not (yes and break_glass):
        raise CliError(
            f"`{_output.inline(environment)}` is protected: a load there needs --yes "
            "--break-glass --reason REASON (MANAGE_ENVIRONMENT, audited); recurring loads belong "
            "in an asset"
        )
    glass = break_glass_reason(break_glass, reason, environment)
    limit = client.tables.upload_limit()
    size = file.stat().st_size
    _check_size(file, size, limit)
    data = read_file(file, fmt, sheet=sheet, header_row=header_row, limit=limit)
    if limit is not None:
        _check_size(file, _ipc_size(data), limit)
    try:
        current = client.tables.get(environment, target).snapshot_id
    except NotFound:
        current = None
    pin: int | str = "none" if current is None else current
    checked = client.tables.load(
        environment,
        target,
        data.schema.empty_table(),
        write=write,
        expected_snapshot_id=pin,
        break_glass_reason=glass,
        dry_run=True,
    )
    lines = [f"{_output.inline(file.name)}: {data.num_rows} rows, {data.num_columns} columns"]
    lines += [f"  {_output.inline(field.name)}: {field.type}" for field in data.schema]
    typer.echo("\n".join(lines), err=True)
    action = "create" if checked.created else write
    confirm(
        state,
        f"Load {data.num_rows} rows into {environment}/{target} ({action})?",
        yes,
    )
    result = client.tables.load(
        environment,
        target,
        data,
        write=write,
        expected_snapshot_id=pin,
        break_glass_reason=glass,
    )
    made = " (created the table)" if result.created else ""
    _output.done(
        state,
        f"Loaded {result.rows} rows into {_output.inline(environment)}/{_output.inline(target)}"
        f"{made}. To clean it up or load it again on a schedule, write an asset that reads it.",
        result,
    )


def register(app: typer.Typer) -> None:
    """Add `load` to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.command("load")(load)
