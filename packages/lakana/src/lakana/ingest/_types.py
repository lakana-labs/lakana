"""Arrow types of a SQL result: PostgreSQL's as ADBC hands them over, MariaDB's from the result.

PostgreSQL (ADBC 1.13 and newer) gives every field its type name and type modifier in the
field's metadata (``POSTGRESQL:type``, ``POSTGRESQL:typmod``). `postgres_schema` turns that into
the table's schema once, before a row is read, and refuses a column with no faithful Arrow form,
naming the column and the cast that fixes it; `normalize_postgres` converts each batch to it.

MariaDB's schema comes from the result's field descriptions (`mariadb_schema`), never from the
values; `mariadb_batch` converts rows to it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from lakana.ingest._errors import UsageError, safe_name

if TYPE_CHECKING:
    import pyarrow as pa

PG_TYPE = b"POSTGRESQL:type"
"""The field metadata key ADBC writes the PostgreSQL type name under."""
PG_TYPMOD = b"POSTGRESQL:typmod"
"""The field metadata key ADBC writes the type modifier under (``-1`` for none)."""
MAX_PRECISION = 38
"""The largest precision of `pyarrow.decimal128`."""

_CASTS = {
    "money": "CAST({col} AS numeric(19, 2))",
    "interval": "EXTRACT(EPOCH FROM {col})",
}
"""PostgreSQL types with an Arrow form that changes their meaning, and the cast to use."""


def _refuse(connection: str, column: str, kind: str, cast: str) -> UsageError:
    """Make the error of a column `sql.read` cannot turn into a faithful Arrow column.

    Parameters
    ----------
    connection
        The connection.
    column
        The column's name, as the result names it.
    kind
        The source's type, such as ``numeric`` or ``inet``.
    cast
        The cast that fixes it, with ``{col}`` for the column.

    Returns
    -------
    UsageError
        Naming the column, its type and the cast.
    """
    name = safe_name(column)
    return UsageError(
        f"`{connection}`: column `{name}` is {kind}, which sql.read cannot read as it is; cast "
        f"it in the query, for example {cast.format(col=name)}"
    )


def _decimal(typmod: int) -> tuple[int, int] | None:
    """Read the precision and scale of a ``numeric`` column from its type modifier.

    Parameters
    ----------
    typmod
        PostgreSQL's ``atttypmod``: ``((precision << 16) | scale) + 4``, or ``-1``.

    Returns
    -------
    tuple of int or None
        Precision and scale; `None` for an unconstrained ``numeric`` or a negative scale.
    """
    if typmod < 4:
        return None
    precision, scale = ((typmod - 4) >> 16) & 0xFFFF, (typmod - 4) & 0x7FF
    if scale & 0x400:  # PostgreSQL 15 and newer allow a negative scale: no Arrow decimal.
        return None
    return precision, scale


def _plain(field: pa.Field) -> bool:
    """Say whether a field's type needs no conversion, nested types included.

    Parameters
    ----------
    field
        The field.

    Returns
    -------
    bool
        False for an extension type or an interval anywhere in it.
    """
    import pyarrow as pa

    kind = field.type
    if isinstance(kind, pa.BaseExtensionType) or pa.types.is_interval(kind):
        return False
    if pa.types.is_list(kind) or pa.types.is_large_list(kind):
        return _plain(kind.value_field)
    if pa.types.is_struct(kind):
        return all(_plain(kind.field(i)) for i in range(kind.num_fields))
    return True


def _bare(kind: pa.DataType) -> pa.DataType:
    """Drop the field metadata ADBC puts on the children of a nested type.

    Parameters
    ----------
    kind
        A plain type (`_plain`).

    Returns
    -------
    pyarrow.DataType
        The same type without metadata at any depth.
    """
    import pyarrow as pa

    if pa.types.is_list(kind):
        return pa.list_(pa.field(kind.value_field.name, _bare(kind.value_type)))
    if pa.types.is_large_list(kind):
        return pa.large_list(pa.field(kind.value_field.name, _bare(kind.value_type)))
    if pa.types.is_struct(kind):
        return pa.struct([pa.field(f.name, _bare(f.type)) for f in kind])
    return kind


def postgres_schema(schema: pa.Schema, connection: str) -> pa.Schema:
    """Decide the table's schema from the schema ADBC reports, before any row is read.

    Parameters
    ----------
    schema
        The reader's schema, with ADBC's field metadata.
    connection
        The connection, for messages.

    Returns
    -------
    pyarrow.Schema
        ``uuid``, ``json`` and ``jsonb`` as strings, ``numeric(p, s)`` as ``decimal128(p, s)``,
        every other column as ADBC gives it; no field metadata.

    Raises
    ------
    UsageError
        For an unconstrained ``numeric``, one wider than 38 digits or with a negative scale,
        ``money``, ``interval``, an array of a converted type, or any type ADBC hands over as an
        opaque value (``inet``, ``point``, ``tsvector``, ...), naming the column and a cast.
    """
    import pyarrow as pa

    fields = []
    for field in schema:
        meta = field.metadata or {}
        kind = meta.get(PG_TYPE, b"").decode(errors="replace")
        column = field.name
        if kind in _CASTS:
            error = _refuse(connection, column, kind, _CASTS[kind])
            raise error
        if kind == "numeric":
            found = _decimal(int(meta.get(PG_TYPMOD, b"-1")))
            if found is None:
                error = _refuse(
                    connection,
                    column,
                    "numeric without a precision",
                    "CAST({col} AS numeric(38, 9))",
                )
                raise error
            precision, scale = found
            if precision > MAX_PRECISION:
                error = _refuse(
                    connection,
                    column,
                    f"numeric({precision}, {scale})",
                    f"CAST({{col}} AS numeric(38, {min(scale, MAX_PRECISION)}))",
                )
                raise error
            fields.append(pa.field(column, pa.decimal128(precision, scale)))
        elif kind in ("uuid", "json", "jsonb"):
            fields.append(pa.field(column, pa.string()))
        elif _plain(field):
            fields.append(pa.field(column, _bare(field.type)))
        else:
            error = _refuse(connection, column, kind or str(field.type), "CAST({col} AS text)")
            raise error
    return pa.schema(fields)


def _uuid_strings(array: pa.Array) -> pa.Array:
    """Spell a column of 16-byte UUIDs in their canonical text form.

    Parameters
    ----------
    array
        An ``arrow.uuid`` column (or its 16-byte storage).

    Returns
    -------
    pyarrow.Array
        Strings such as ``0b8e...-...``, nulls kept.
    """
    import pyarrow as pa

    storage = array.storage if isinstance(array.type, pa.BaseExtensionType) else array
    return pa.array(
        [None if v is None else str(uuid.UUID(bytes=v)) for v in storage.to_pylist()],
        pa.string(),
    )


def normalize_postgres(batch: pa.RecordBatch, schema: pa.Schema, connection: str) -> pa.RecordBatch:
    """Convert one batch to the schema `postgres_schema` decided.

    Parameters
    ----------
    batch
        A batch as ADBC delivers it.
    schema
        The table's schema.
    connection
        The connection, for messages.

    Returns
    -------
    pyarrow.RecordBatch
        The batch in `schema`.

    Raises
    ------
    UsageError
        For a ``numeric`` value a decimal cannot hold (``NaN``, ``Infinity``), naming the column.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    columns = []
    for index, field in enumerate(schema):
        array = batch.column(index)
        if pa.types.is_decimal(field.type):
            storage = array.storage if isinstance(array.type, pa.BaseExtensionType) else array
            try:
                array = pc.cast(storage, field.type)
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
                raise UsageError(
                    f"`{connection}`: column `{safe_name(field.name)}` holds a value that "
                    f"{field.type} cannot hold (NaN or Infinity); filter it out in the query, or "
                    "cast the column to double precision"
                ) from None
        elif pa.types.is_string(field.type) and not pa.types.is_string(array.type):
            meta = (batch.schema.field(index).metadata or {}).get(PG_TYPE, b"")
            array = _uuid_strings(array) if meta == b"uuid" else array.storage
        columns.append(array)
    # A nested column's children keep ADBC's metadata in the array; the schema drops it.
    return pa.RecordBatch.from_arrays(columns, schema=schema)


# --- MariaDB and MySQL ------------------------------------------------------------------------

_UNSIGNED = 32
"""`pymysql.constants.FLAG.UNSIGNED`."""
_BINARY_CHARSET = 63
"""The character set number of binary strings and BLOBs."""


def mariadb_schema(fields: Sequence[Any], connection: str) -> pa.Schema:
    """Decide the table's schema from the result's field descriptions, never from the values.

    Parameters
    ----------
    fields
        PyMySQL's field descriptions (``cursor._result.fields``): ``name``, ``type_code``,
        ``flags``, ``scale``, ``length`` and ``charsetnr`` of each column.
    connection
        The connection, for messages.

    Returns
    -------
    pyarrow.Schema
        The schema.

    Raises
    ------
    UsageError
        For ``GEOMETRY``, ``BIT`` or a type this helper does not know, naming the column and a
        cast.
    """
    import pyarrow as pa

    out = []
    for field in fields:
        code, unsigned = field.type_code, bool(field.flags & _UNSIGNED)
        name = field.name
        if code in (1, 13):  # TINY, YEAR
            kind = pa.int16()
        elif code in (2, 9):  # SHORT, INT24
            kind = pa.int32()
        elif code == 3:  # LONG
            kind = pa.int64()
        elif code == 8:  # LONGLONG
            kind = pa.decimal128(20, 0) if unsigned else pa.int64()
        elif code in (0, 246):  # DECIMAL, NEWDECIMAL
            scale = field.scale
            precision = field.length - (1 if scale > 0 else 0) - (0 if unsigned else 1)
            if precision > MAX_PRECISION:
                error = _refuse(
                    connection, name, f"DECIMAL({precision}, {scale})", "CAST({col} AS DOUBLE)"
                )
                raise error
            kind = pa.decimal128(precision, scale)
        elif code == 4:  # FLOAT
            kind = pa.float32()
        elif code == 5:  # DOUBLE
            kind = pa.float64()
        elif code in (10, 14):  # DATE, NEWDATE
            kind = pa.date32()
        elif code == 12:  # DATETIME
            kind = pa.timestamp("us")
        elif code == 7:  # TIMESTAMP: the session's time zone is UTC
            kind = pa.timestamp("us", "UTC")
        elif code == 11:  # TIME
            kind = pa.time64("us")
        elif code in (15, 245, 247, 248, 253, 254, 249, 250, 251, 252, 6):
            # VARCHAR, JSON, ENUM, SET, VAR_STRING, STRING, the BLOBs, NULL. MySQL describes
            # JSON with the binary character set, and PyMySQL decodes it as text.
            binary = field.charsetnr == _BINARY_CHARSET and code not in (6, 245)
            kind = pa.binary() if binary else pa.string()
        elif code == 255:
            error = _refuse(connection, name, "GEOMETRY", "ST_AsText({col})")
            raise error
        elif code == 16:
            error = _refuse(connection, name, "BIT", "CAST({col} AS UNSIGNED)")
            raise error
        else:
            error = _refuse(connection, name, f"of type code {code}", "CAST({col} AS CHAR)")
            raise error
        out.append(pa.field(name, kind))
    return pa.schema(out)


def _time(value: object) -> object:
    """Turn PyMySQL's `timedelta` for a ``TIME`` into a time of day.

    Parameters
    ----------
    value
        The value.

    Returns
    -------
    object
        A `datetime.time`, `None`, or the value unchanged (refused by the caller).
    """
    if isinstance(value, dt.timedelta) and dt.timedelta(0) <= value < dt.timedelta(days=1):
        return (dt.datetime.min + value).time()
    return value


def mariadb_batch(
    rows: Sequence[Sequence[object]], schema: pa.Schema, connection: str
) -> pa.RecordBatch:
    """Convert rows as PyMySQL returns them to a batch of `schema`.

    Parameters
    ----------
    rows
        The rows.
    schema
        The schema `mariadb_schema` decided.
    connection
        The connection, for messages.

    Returns
    -------
    pyarrow.RecordBatch
        The batch.

    Raises
    ------
    UsageError
        For a value its column's type cannot hold (a zero date such as ``0000-00-00``, a
        ``TIME`` outside one day), naming the column.
    """
    import pyarrow as pa

    columns = []
    for index, field in enumerate(schema):
        values = [row[index] for row in rows]
        if pa.types.is_time(field.type):
            values = [_time(v) for v in values]
        try:
            columns.append(pa.array(values, type=field.type))
        except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
            bad = next((v for v in values if v is not None and not _fits(v, field.type)), None)
            shown = safe_name(repr(bad))[:100]
            column = safe_name(field.name)
            fix = (
                f"TIME_TO_SEC({column}) for a time outside one day"
                if pa.types.is_time(field.type)
                else f"NULLIF({column}, '0000-00-00') for a zero date"
            )
            raise UsageError(
                f"`{connection}`: column `{column}` holds {shown}, which {field.type} cannot "
                f"hold; convert or filter it in the query (for example {fix})"
            ) from None
    return pa.RecordBatch.from_arrays(columns, schema=schema)


def _fits(value: object, kind: pa.DataType) -> bool:
    """Say whether one value converts to an Arrow type.

    Parameters
    ----------
    value
        The value.
    kind
        The type.

    Returns
    -------
    bool
        Whether `pyarrow.array` takes it.
    """
    import pyarrow as pa

    try:
        pa.array([value], type=kind)
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
        return False
    return True
