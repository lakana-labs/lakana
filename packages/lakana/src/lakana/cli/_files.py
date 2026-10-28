"""Reading data files for `table append` and writing query results for `--out`."""

from __future__ import annotations

import csv
import re
import uuid
from collections.abc import Sequence
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.ipc as ipc
import pyarrow.parquet as pq

from lakana._arrow import json_to_arrow_type
from lakana._models import Column
from lakana.cli._output import inline
from lakana.cli._state import CliError
from lakana.errors import ValidationError

FORMATS = ("parquet", "csv", "arrow")
_EXTENSIONS = {
    ".parquet": "parquet",
    ".pq": "parquet",
    ".csv": "csv",
    ".arrow": "arrow",
    ".ipc": "arrow",
    ".feather": "arrow",
}


def infer_format(path: Path, explicit: str | None) -> str:
    """Pick a file format from `--format` or the extension.

    Parameters
    ----------
    path
        The file.
    explicit
        The `--format` value, if given.

    Returns
    -------
    str
        One of `FORMATS`.

    Raises
    ------
    CliError
        If `--format` is invalid or the extension is unknown (exit 2).
    """
    if explicit is not None:
        if explicit not in FORMATS:
            raise CliError(f"--format must be one of {', '.join(FORMATS)}; got {explicit!r}")
        return explicit
    fmt = _EXTENSIONS.get(path.suffix.lower())
    if fmt is None:
        raise CliError(f"Can't tell the format of {path.name}; pass --format parquet|csv|arrow")
    return fmt


_CSV_COLUMN_ERROR = re.compile(r"In CSV column #(\d+): (.*)", re.DOTALL)


def _csv_parse_type(target: pa.DataType) -> pa.DataType:
    """Return the type a CSV column is parsed as, for a table column of type `target`.

    Most types are parsed straight into the target type. uuid has no CSV
    parser, and a timestamptz parser refuses text without a zone offset, so
    both are read as text and converted exactly by `_cast`. A float32
    (`float`) target is parsed as float64 instead of float32: pyarrow's CSV
    reader parses text straight into whatever type `ConvertOptions` names,
    with no overflow check of its own, so parsing straight into float32
    would turn a value too large for it (e.g. `1e39`) into `inf` during the
    read itself. Parsing into float64 and letting `_cast` narrow it to
    float32 afterwards keeps `_check_no_new_overflow` in the loop, since
    `_cast` only runs that guard when the source type differs from the
    target.

    Parameters
    ----------
    target
        The table column's Arrow type.

    Returns
    -------
    pa.DataType
        `target`, `pa.string()` for uuid and zoned timestamps, or
        `pa.float64()` for a float32 target.
    """
    if target == pa.uuid() or (pa.types.is_timestamp(target) and target.tz is not None):
        return pa.string()
    if target == pa.float32():
        return pa.float64()
    return target


def _csv_column_types(columns: Sequence[Column]) -> dict[str, pa.DataType]:
    """Map each table column to the type its CSV text is parsed as.

    Parameters
    ----------
    columns
        The target table's columns.

    Returns
    -------
    dict of str to pa.DataType
        Parse types by column name. A column whose type has no Arrow
        mapping is left out, so pyarrow infers it and `conform` reports it.
    """
    types: dict[str, pa.DataType] = {}
    for column in columns:
        try:
            types[column.name] = _csv_parse_type(json_to_arrow_type(column.type))
        except ValidationError:
            continue
    return types


def _csv_header(path: Path) -> list[str]:
    """Read a CSV file's header row, to name a column pyarrow only numbers.

    Parameters
    ----------
    path
        The CSV file.

    Returns
    -------
    list of str
        The column names, or an empty list if the file can't be read.
    """
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            return next(csv.reader(handle), [])
    except OSError:
        return []


def _csv_error(path: Path, exc: pa.ArrowInvalid, columns: Sequence[Column]) -> CliError:
    """Turn a CSV parse failure into an error naming the column where possible.

    Parameters
    ----------
    path
        The CSV file.
    exc
        pyarrow's error, e.g. `"In CSV column #1: CSV conversion error ..."`.
    columns
        The target table's columns.

    Returns
    -------
    CliError
        `"Column 'x' can't be read as <type>: ..."` when the column can be
        identified, else pyarrow's own message quoted (exit 8 either way).
    """
    match = _CSV_COLUMN_ERROR.match(str(exc))
    if match:
        header = _csv_header(path)
        index = int(match.group(1))
        by_name = {column.name: column for column in columns}
        column = by_name.get(header[index]) if index < len(header) else None
        if column is not None:
            return CliError(
                f"Column {column.name!r} can't be read as {column.type}: {match.group(2)}",
                8,
                kind="SchemaError",
            )
    return CliError(f"Could not read {path} as csv: {exc}", 8, kind="FileError")


def strict_utf8(path: Path, data: bytes) -> bytes:
    """Hold text to the helpers' strict UTF-8 rule, dropping a byte order mark.

    Parameters
    ----------
    path
        The file, for messages.
    data
        Its bytes.

    Returns
    -------
    bytes
        The bytes without a byte order mark.

    Raises
    ------
    CliError
        For bytes that are not UTF-8 (exit 8).
    """
    data = data.removeprefix(b"\xef\xbb\xbf")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError(
            f"{inline(path.name)} is not UTF-8 (byte {data[exc.start]:#04x} at position "
            f"{exc.start}); save it as UTF-8 and load it again",
            8,
            kind="FileError",
        ) from None
    return data


def read_table(path: Path, fmt: str, csv_columns: Sequence[Column] | None = None) -> pa.Table:
    """Read a Parquet, CSV or Arrow IPC file.

    Parameters
    ----------
    path
        The file.
    fmt
        One of `FORMATS`.
    csv_columns
        The target table's columns. A CSV's text is then parsed straight
        into their types rather than inferred, so nothing inference would
        change (leading zeros, wide decimals, timestamp-like text) is lost.
        Ignored for Parquet and Arrow, which carry their own types.

    Returns
    -------
    pa.Table
        Its contents.

    Raises
    ------
    CliError
        If the file is missing or can't be read in that format, or a CSV
        value doesn't parse as its column's type (exit 8, naming the column
        where it can be identified).
    """
    try:
        if fmt == "parquet":
            return pq.read_table(path)
        if fmt == "csv":
            convert_options = pacsv.ConvertOptions(
                column_types=_csv_column_types(csv_columns or []),
                strings_can_be_null=True,
                null_values=[""],
            )
            data = strict_utf8(path, path.read_bytes())
            try:
                return pacsv.read_csv(pa.BufferReader(data), convert_options=convert_options)
            except pa.ArrowInvalid as exc:
                error = _csv_error(path, exc, csv_columns or [])
                raise error from exc
        with pa.memory_map(str(path)) as source:
            try:
                return ipc.open_file(source).read_all()
            except pa.ArrowInvalid:
                source.seek(0)
                return ipc.open_stream(source).read_all()
    except FileNotFoundError as exc:
        raise CliError(f"No such file: {path}", 8, kind="FileError") from exc
    except (pa.ArrowException, OSError) as exc:
        raise CliError(f"Could not read {path} as {fmt}: {exc}", 8, kind="FileError") from exc


def write_table(table: pa.Table, path: Path, fmt: str) -> None:
    """Write a table as Parquet, CSV or an Arrow IPC file.

    Parameters
    ----------
    table
        The data.
    path
        The destination.
    fmt
        One of `FORMATS`.

    Raises
    ------
    CliError
        If writing fails (exit 8).
    """
    try:
        if fmt == "parquet":
            pq.write_table(table, path)
        elif fmt == "csv":
            pacsv.write_csv(table, path)
        else:
            with ipc.new_file(str(path), table.schema) as writer:
                writer.write_table(table)
    except (pa.ArrowException, OSError) as exc:
        raise CliError(f"Could not write {path} as {fmt}: {exc}", 8, kind="FileError") from exc


def _first_offender(source: pa.ChunkedArray, mask: pa.ChunkedArray) -> object:
    """Return the first value of `source` where `mask` is true.

    Parameters
    ----------
    source
        The column values to pick from.
    mask
        A same-length boolean (possibly null) array; nulls are excluded,
        matching `pc.filter`'s default `null_selection_behavior`.

    Returns
    -------
    object
        The first matching value.
    """
    return pc.filter(source, mask).to_pylist()[0]  # ty: ignore[unresolved-attribute]


def _check_decimal_exact(source: pa.ChunkedArray, target: pa.DataType) -> None:
    """Refuse a floating-point-to-decimal cast that rounded a value away.

    Only a floating-point source needs this check. pyarrow's own decimal
    cast already refuses a lossy rescale for every other source type: it
    raises `ArrowInvalid` ("Rescaling Decimal value would cause data loss")
    narrowing an existing decimal, and it parses a string's exact text and
    raises the same way if the string itself has more scale than `target`
    allows (both pinned by tests, not re-implemented here). A `float32` or
    `float64` source is the one case `.cast()` accepts silently: it
    computes the exact decimal expansion of the binary float and rounds
    that to `target`'s scale, discarding any digits beyond it without
    complaint.

    Vectorized with `pyarrow.compute`, not a Python loop over
    `to_pylist()` (the loop cost ~0.7s per
    1M-row column). `pc.round` rounds within the source's own float type,
    which is also why this must not go through `Decimal(repr(value))`
    per-element (an earlier approach): `float32(19.99)` is actually
    `19.989999771118164`, and comparing that against the double-precision
    text `"19.99"` would wrongly reject it. Rounding and comparing in the
    source's own type makes a value compare equal to its own rounding
    noise, which is what floating-point values sourced from a text-exact
    decimal *should* do.

    Parameters
    ----------
    source
        The column as read, before casting. Must be `float32` or `float64`.
    target
        The decimal type `source` is being cast to.

    Raises
    ------
    ValueError
        If any non-null value would round under `target`'s scale, naming
        the first such value and `target`.
    """
    rounded = pc.round(source, ndigits=target.scale)  # ty: ignore[unresolved-attribute]
    mismatch = pc.not_equal(rounded, source)  # ty: ignore[unresolved-attribute]
    if pc.any(mismatch, min_count=0).as_py():  # ty: ignore[unresolved-attribute]
        bad = _first_offender(source, mismatch)
        raise ValueError(f"{bad!r} has more precision or scale than {target} allows")


def _check_no_new_overflow(
    source: pa.ChunkedArray, cast_result: pa.ChunkedArray, target: pa.DataType
) -> None:
    """Refuse a float cast that overflowed to infinity, rather than losing ordinary precision.

    `double -> float32` losing precision (e.g. `0.1` becoming
    `0.10000000149011612`) is expected and must pass; a value becoming
    `inf` or `-inf` because it no longer fits `target`'s range is data
    loss and must not. Vectorized with `pyarrow.compute` rather than a
    Python loop, for the same reason as `_check_decimal_exact`.

    Parameters
    ----------
    source
        The column as read, before casting.
    cast_result
        `source` cast to `target`.
    target
        The floating-point type `source` was cast to.

    Raises
    ------
    ValueError
        If any value became `inf` or `-inf` in `cast_result` without
        already being infinite in `source`, naming the first such value
        and `target`.
    """
    became_inf = pc.is_inf(cast_result)  # ty: ignore[unresolved-attribute]
    if pa.types.is_floating(source.type):
        is_source_inf = pc.is_inf(source)  # ty: ignore[unresolved-attribute]
        became_inf = pc.and_(became_inf, pc.invert(is_source_inf))  # ty: ignore[unresolved-attribute]
    if pc.any(became_inf, min_count=0).as_py():  # ty: ignore[unresolved-attribute]
        bad = _first_offender(source, became_inf)
        raise ValueError(f"{bad!r} overflows {target}")


def _check_boolean_digits(source: pa.ChunkedArray) -> None:
    """Refuse a number-to-boolean cast of anything but 0 and 1.

    pyarrow's cast maps every non-zero number to `True`, so a 2 (or -1, or
    0.5) would silently become `True`. Vectorized like the other checks;
    nulls compare as null and are skipped.

    Parameters
    ----------
    source
        The column as read, before casting; integer or floating-point.

    Raises
    ------
    ValueError
        If any non-null value is neither 0 nor 1, naming the first.
    """
    zero = pa.scalar(0, source.type)
    one = pa.scalar(1, source.type)
    other = pc.and_(pc.not_equal(source, zero), pc.not_equal(source, one))  # ty: ignore[unresolved-attribute]
    if pc.any(other, min_count=0).as_py():  # ty: ignore[unresolved-attribute]
        bad = _first_offender(source, other)
        raise ValueError(f"{bad!r} isn't a boolean; only 0 and 1 are accepted")


def _cast(source: pa.ChunkedArray, target: pa.DataType) -> pa.ChunkedArray:
    """Cast one column, with the conversions a plain `cast` can't do.

    Parameters
    ----------
    source
        The column as read.
    target
        The table's Arrow type for it.

    Returns
    -------
    pa.ChunkedArray
        The cast column.

    Raises
    ------
    ValueError
        If casting to a decimal would round away precision or scale the
        source value actually had, casting to a floating-point type would
        overflow a finite value to infinity, or a number other than 0 or 1
        would become a boolean.
    """
    if target == pa.uuid() and source.type != pa.uuid():
        values = [None if v is None else uuid.UUID(str(v)).bytes for v in source.to_pylist()]
        storage = pa.array(values, type=pa.binary(16))
        return pa.chunked_array([pa.ExtensionArray.from_storage(pa.uuid(), storage)])
    if pa.types.is_timestamp(target) and target.tz is not None and pa.types.is_string(source.type):
        try:
            return source.cast(target)
        except pa.ArrowInvalid:
            # Text without a zone offset: parse it as a naive timestamp, which
            # is then read as UTC below, the same as a naive Parquet column.
            source = source.cast(pa.timestamp(target.unit))
    if (
        pa.types.is_timestamp(target)
        and target.tz is not None
        and pa.types.is_timestamp(source.type)
        and source.type.tz is None
    ):
        source = pc.assume_timezone(source, target.tz)  # ty: ignore[unresolved-attribute]
    if pa.types.is_boolean(target) and (
        pa.types.is_integer(source.type) or pa.types.is_floating(source.type)
    ):
        _check_boolean_digits(source)
    cast_result = source.cast(target)
    if pa.types.is_decimal(target) and pa.types.is_floating(source.type):
        _check_decimal_exact(source, target)
    elif pa.types.is_floating(target) and source.type != target:
        _check_no_new_overflow(source, cast_result, target)
    return cast_result


def conform(table: pa.Table, columns: Sequence[Column]) -> pa.Table:
    """Reorder, cast and nullability-check data against a table's columns.

    Parameters
    ----------
    table
        The data as read from a file.
    columns
        The target table's columns (`Table.columns`).

    Returns
    -------
    pa.Table
        The data in the table's column order, types and nullability.

    Raises
    ------
    CliError
        If columns are duplicated, missing or unexpected; a required column
        has nulls; or a value can't be cast (exit 8, naming the column).
    """
    have = table.column_names
    if len(set(have)) != len(have):
        raise CliError("The file has duplicate column names.", 8, kind="SchemaError")
    names = [c.name for c in columns]
    missing = [n for n in names if n not in have]
    extra = [n for n in have if n not in names]
    if missing or extra:
        raise CliError(
            f"The file's columns don't match the table: missing {missing}; unexpected {extra}",
            8,
            kind="SchemaError",
        )
    arrays: list[pa.ChunkedArray] = []
    fields: list[pa.Field] = []
    for column in columns:
        source = table.column(column.name)
        if not column.nullable and source.null_count:
            raise CliError(
                f"Column {column.name!r} is required but has {source.null_count} empty values.",
                8,
                kind="SchemaError",
            )
        try:
            target = json_to_arrow_type(column.type)
            arrays.append(_cast(source, target))
        except (pa.ArrowException, ValueError, ValidationError) as exc:
            raise CliError(
                f"Column {column.name!r} can't be read as {column.type}: {exc}",
                8,
                kind="SchemaError",
            ) from exc
        fields.append(pa.field(column.name, target, nullable=column.nullable))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))
