"""`lakana load`: load a local file into a table of the bronze layer, once.

For exploration, testing and teaching. Anything that should run again belongs in an asset; the
server's load route therefore writes only into `bronze` and refuses every other layer before it
reads the body. This command holds no copy of that rule: it shows the server's refusal as it is.

CSV, Parquet and Arrow files and newline-delimited JSON are read with pyarrow, which every
install has; a JSON array and spreadsheets are read by the files helper's reader, which needs
the ``assets`` extra (polars, fastexcel).
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.ipc as ipc
import pyarrow.json as pajson
import pyarrow.parquet as pq
import typer

from lakana._arrow import cast_timestamp_ns_columns_to_us, normalize_view_columns, schema_to_columns
from lakana.cli import _files, _output
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
"""Rows decoded at a time from a Parquet file, and from a batch with a dictionary column, so a
read stops soon after the limit."""


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
    data = _files.strict_utf8(path, path.read_bytes())
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

    data = _files.strict_utf8(path, data)
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


def _pieces(batch: pa.RecordBatch) -> Iterable[pa.RecordBatch]:
    """Split a batch with a dictionary column into decoded pieces of a few rows.

    A dictionary column takes little room encoded and may take far more decoded (one long
    value behind many small indices), so it is decoded a few rows at a time.

    Parameters
    ----------
    batch
        The batch as read.

    Returns
    -------
    Iterable[pa.RecordBatch]
        The batch itself when it has no dictionary column, else its decoded pieces.
    """
    if not any(pa.types.is_dictionary(kind) for kind in batch.schema.types):
        return (batch,)
    return (
        _decoded(batch.slice(start, _BATCH_ROWS)) for start in range(0, batch.num_rows, _BATCH_ROWS)
    )


def _bounded(
    path: Path, schema: pa.Schema, batches: Iterable[pa.RecordBatch], limit: int | None
) -> pa.Table:
    """Collect decoded batches, stopping as soon as they pass the upload limit.

    Dictionary columns are decoded before they are counted, so the count is of the values the
    table will hold.

    Parameters
    ----------
    path
        The file, for the message.
    schema
        The batches' schema.
    batches
        The batches, decoded one at a time.
    limit
        The upload limit, or `None` when the server does not say.

    Returns
    -------
    pa.Table
        The rows.
    """
    plain = pa.schema(
        [
            field.with_type(field.type.value_type) if pa.types.is_dictionary(field.type) else field
            for field in schema
        ]
    )
    kept: list[pa.RecordBatch] = []
    size = 0
    for batch in batches:
        for piece in _pieces(batch):
            size += piece.nbytes
            _check_size(path, size, limit)
            kept.append(piece)
    return pa.Table.from_batches(kept, plain)


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
        return _bounded(path, reader.schema, batches, limit)


def _parquet(path: Path, limit: int | None) -> pa.Table:
    """Read a Parquet file a few rows at a time, after an estimate from its metadata.

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
    _check_size(path, _parquet_size(path), limit)
    with pq.ParquetFile(path) as parquet:
        return _bounded(path, parquet.schema_arrow, parquet.iter_batches(_BATCH_ROWS), limit)


def _parquet_size(path: Path) -> int:
    """Estimate, from a Parquet file's metadata, how many bytes its rows take once read.

    A small file can hold many rows of repeated values, which its encoding and compression
    shrink to almost nothing; reading it would build the whole table in memory. The estimate is
    the larger of the column chunks' uncompressed sizes and the rows times the columns' widths
    (a column without a fixed width counted at 4 bytes a row, its offsets).

    Parameters
    ----------
    path
        The file.

    Returns
    -------
    int
        The estimated size in bytes.
    """
    metadata = pq.read_metadata(path)
    schema = metadata.schema.to_arrow_schema()
    width = 0
    for field in schema:
        try:
            width += max(field.type.bit_width // 8, 1)
        except ValueError:
            width += 4
    chunks = sum(
        group.column(index).total_uncompressed_size
        for group in (metadata.row_group(i) for i in range(metadata.num_row_groups))
        for index in range(group.num_columns)
    )
    return max(chunks, metadata.num_rows * width)


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
        as its decoded rows pass it (a Parquet file is first estimated from its metadata); a
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
    except (pa.ArrowException, OSError, struct.error) as exc:
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
