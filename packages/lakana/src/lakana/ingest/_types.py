"""Arrow types of a SQL result: PostgreSQL's as ADBC hands them over.

PostgreSQL (ADBC 1.13 and newer) gives every field its type name and type modifier in the
field's metadata (``POSTGRESQL:type``, ``POSTGRESQL:typmod``). `postgres_schema` turns that into
the table's schema once, before a row is read, and refuses a column with no faithful Arrow form,
naming the column and the cast that fixes it; `normalize_postgres` converts each batch to it.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

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
