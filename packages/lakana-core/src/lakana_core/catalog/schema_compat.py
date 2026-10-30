"""Schema compatibility between a write's result and its target table.

Pure functions over Arrow and Iceberg schemas, with no session and no
storage, so every rule is unit-testable on its own:
which Arrow types a Lakana table can store, which type changes
are Iceberg-safe promotions, the readable diff a failed write reports, and
the new schema an evolving write commits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pyarrow as pa
from pyiceberg.io.pyarrow import UTC_ALIASES
from pyiceberg.schema import Schema
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    FloatType,
    IcebergType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestampType,
    TimestamptzType,
    TimeType,
    UUIDType,
)
from sqlglot import exp

from lakana_core.catalog.json_schema import ColumnSpec, type_name
from lakana_core.errors import ValidationError

_SIMPLE_TYPES: tuple[tuple[Callable[[pa.DataType], bool], IcebergType], ...] = (
    (pa.types.is_int8, IntegerType()),
    (pa.types.is_int16, IntegerType()),
    (pa.types.is_int32, IntegerType()),
    (pa.types.is_uint8, IntegerType()),
    (pa.types.is_uint16, IntegerType()),
    (pa.types.is_int64, LongType()),
    (pa.types.is_uint32, LongType()),
    (pa.types.is_float32, FloatType()),
    (pa.types.is_float64, DoubleType()),
    (pa.types.is_boolean, BooleanType()),
    (pa.types.is_string, StringType()),
    (pa.types.is_large_string, StringType()),
    (pa.types.is_string_view, StringType()),
    (pa.types.is_binary, BinaryType()),
    (pa.types.is_large_binary, BinaryType()),
    (pa.types.is_date32, DateType()),
    (pa.types.is_date64, DateType()),
)
"""Arrow type predicates with a single Iceberg counterpart, checked in order."""


def arrow_to_iceberg_type(arrow_type: pa.DataType, column: str) -> IcebergType:
    """Map one result column's Arrow type to the Iceberg type a table stores it as.

    Parameters
    ----------
    arrow_type
        The column's Arrow type, as DuckDB (or an Arrow upload) produced it.
    column
        The column's name, for the error message.

    Returns
    -------
    IcebergType
        The Iceberg type.

    Raises
    ------
    ValidationError
        If a Lakana table can't store `arrow_type` — an unsigned 64-bit
        integer, a timestamp in any unit but microseconds or with a
        non-UTC time zone, or a nested, interval or other type. The message
        names the column and suggests a CAST; for an untyped `NULL`
        (`SELECT NULL AS x`, Arrow `null` on DuckDB 2) it spells out the
        CAST to write.
    """
    if pa.types.is_null(arrow_type):
        alias = exp.to_identifier(column).sql(dialect="duckdb")
        raise ValidationError(
            f"Column {column!r} is NULL without a type, which a Lakana table can't store; "
            f"write `CAST(NULL AS <type>) AS {alias}`, for example `CAST(NULL AS VARCHAR)`."
        )
    for predicate, iceberg_type in _SIMPLE_TYPES:
        if predicate(arrow_type):
            return iceberg_type
    if pa.types.is_decimal128(arrow_type):
        return DecimalType(arrow_type.precision, arrow_type.scale)
    if pa.types.is_time64(arrow_type) and arrow_type.unit == "us":
        return TimeType()
    if pa.types.is_timestamp(arrow_type) and arrow_type.unit == "us":
        if arrow_type.tz is None:
            return TimestampType()
        if arrow_type.tz in UTC_ALIASES:
            return TimestamptzType()
    if arrow_type == pa.uuid():
        return UUIDType()
    raise ValidationError(
        f"Column {column!r} has type {arrow_type}, which a Lakana table can't store; "
        "CAST it in the SELECT (for example to BIGINT, DOUBLE, VARCHAR or TIMESTAMP)."
    )


def arrow_to_iceberg_schema(arrow_schema: pa.Schema) -> Schema:
    """Convert a result's Arrow schema into an Iceberg schema.

    Parameters
    ----------
    arrow_schema
        The result's schema.

    Returns
    -------
    Schema
        Field ids `1..n` in column order; a non-nullable Arrow field becomes
        a required Iceberg field. `schema_id` is 0.

    Raises
    ------
    ValidationError
        If the result has no columns, repeats a column name, or has a
        column of a type `arrow_to_iceberg_type` refuses.
    """
    if not arrow_schema.names:
        raise ValidationError("The result has no columns.")
    seen: set[str] = set()
    fields: list[NestedField] = []
    for field_id, field in enumerate(arrow_schema, start=1):
        if field.name in seen:
            raise ValidationError(f"Column {field.name!r} appears more than once in the result.")
        seen.add(field.name)
        fields.append(
            NestedField(
                field_id,
                field.name,
                arrow_to_iceberg_type(field.type, field.name),
                required=not field.nullable,
            )
        )
    return Schema(*fields)


def is_promotion(source: IcebergType, target: IcebergType) -> bool:
    """Decide whether values of `source` can be read as `target` without loss.

    Parameters
    ----------
    source
        The narrower type.
    target
        The wider type.

    Returns
    -------
    bool
        `True` for int → long, float → double, and decimal precision
        widening at an unchanged scale — the Iceberg-safe promotions the
        spec names. `False` for equal types and everything else.
    """
    if isinstance(source, IntegerType) and isinstance(target, LongType):
        return True
    if isinstance(source, FloatType) and isinstance(target, DoubleType):
        return True
    if isinstance(source, DecimalType) and isinstance(target, DecimalType):
        return source.scale == target.scale and source.precision < target.precision
    return False


@dataclass(frozen=True)
class ColumnType:
    """A column added to or dropped from a table.

    Parameters
    ----------
    name
        The column's name.
    type
        Its Iceberg type, as `str(IcebergType)` spells it.
    """

    name: str
    type: str


@dataclass(frozen=True)
class RenamedColumn:
    """A dropped and an added column of the same type, which look like a rename.

    Parameters
    ----------
    old
        The table's column name.
    new
        The result's column name.
    type
        Their shared Iceberg type.
    """

    old: str
    new: str
    type: str


@dataclass(frozen=True)
class RetypedColumn:
    """A column whose type the table would have to change.

    Parameters
    ----------
    name
        The column's name.
    old
        The table's type.
    new
        The result's type.
    """

    name: str
    old: str
    new: str


@dataclass(frozen=True)
class SchemaDiff:
    """How a write's result differs from its target's schema.

    Parameters
    ----------
    added
        Result columns the table does not have.
    dropped
        Table columns the result does not have.
    renamed
        Dropped/added pairs of the same type, diagnostic only.
    retyped
        Columns whose type is neither equal nor a promotion into the
        table's type.
    """

    added: tuple[ColumnType, ...] = ()
    dropped: tuple[ColumnType, ...] = ()
    renamed: tuple[RenamedColumn, ...] = ()
    retyped: tuple[RetypedColumn, ...] = ()

    def is_empty(self) -> bool:
        """Report whether the result matches the table.

        Returns
        -------
        bool
            `True` when nothing was added, dropped, renamed or retyped.
        """
        return not (self.added or self.dropped or self.renamed or self.retyped)

    def to_json(self) -> dict[str, list[dict[str, str]]]:
        """Render the diff as the JSON stored on a run and returned by the API.

        Returns
        -------
        dict
            ``{"added": [{"name", "type"}], "dropped": [...], "renamed":
            [{"old", "new", "type"}], "retyped": [{"name", "old", "new"}]}``.
        """
        return {
            "added": [{"name": c.name, "type": c.type} for c in self.added],
            "dropped": [{"name": c.name, "type": c.type} for c in self.dropped],
            "renamed": [{"old": c.old, "new": c.new, "type": c.type} for c in self.renamed],
            "retyped": [{"name": c.name, "old": c.old, "new": c.new} for c in self.retyped],
        }

    def describe(self) -> str:
        """Render the diff as one readable line.

        Returns
        -------
        str
            For example ``"added x (long); dropped y (string); renamed-looking
            a -> b (long); retyped c: int -> string"``.
        """
        parts = [f"added {c.name} ({c.type})" for c in self.added]
        parts += [f"dropped {c.name} ({c.type})" for c in self.dropped]
        parts += [f"renamed-looking {c.old} -> {c.new} ({c.type})" for c in self.renamed]
        parts += [f"retyped {c.name}: {c.old} -> {c.new}" for c in self.retyped]
        return "; ".join(parts)


EVOLVE_ADVICE = "Pass evolve_schema (--evolve-schema) to let the table adopt the result's schema."
"""The next step `SchemaMismatchError` gives a caller of the API, the CLI or the SDK. An asset
spells the option elsewhere, or cannot set it, so the worker and the task harness replace this
sentence with the step that asset can take."""


class SchemaMismatchError(ValidationError):
    """A write's result is incompatible with its target and `evolve_schema` is off.

    Parameters
    ----------
    table_name
        The target's dotted name.
    diff
        What differs, kept on the exception for callers that want it.
    """

    def __init__(self, table_name: str, diff: SchemaDiff) -> None:
        super().__init__(
            f"The result does not match {table_name}'s schema ({diff.describe()}). {EVOLVE_ADVICE}"
        )
        self.diff = diff


@dataclass(frozen=True)
class SchemaPlan:
    """What a write must do to its target's schema.

    Parameters
    ----------
    schema
        The schema to add and make current in the write's own commit, or
        `None` when the table's schema stays as it is.
    diff
        What differed. Empty exactly when `schema` is `None`.
    """

    schema: Schema | None
    diff: SchemaDiff


def diff_schemas(table: Schema, result: Schema) -> SchemaDiff:
    """Compare a result's schema with its target's, by column name.

    Parameters
    ----------
    table
        The target's current schema.
    result
        The result's schema (`arrow_to_iceberg_schema`).

    Returns
    -------
    SchemaDiff
        Added, dropped, renamed-looking and retyped columns. A result type
        that promotes into the table's type is not a difference; a
        dropped/added pair of the same type is reported as renamed-looking
        instead of as either.
    """
    table_fields = {f.name: f for f in table.fields}
    result_fields = {f.name: f for f in result.fields}
    added = [
        ColumnType(f.name, str(f.field_type)) for f in result.fields if f.name not in table_fields
    ]
    dropped = [
        ColumnType(f.name, str(f.field_type)) for f in table.fields if f.name not in result_fields
    ]
    retyped = [
        RetypedColumn(name, str(existing.field_type), str(candidate.field_type))
        for name, existing in table_fields.items()
        if (candidate := result_fields.get(name)) is not None
        and candidate.field_type != existing.field_type
        and not is_promotion(candidate.field_type, existing.field_type)
    ]
    renamed: list[RenamedColumn] = []
    for old in list(dropped):
        match = next((new for new in added if new.type == old.type), None)
        if match is not None:
            renamed.append(RenamedColumn(old.name, match.name, old.type))
            dropped.remove(old)
            added.remove(match)
    return SchemaDiff(tuple(added), tuple(dropped), tuple(renamed), tuple(retyped))


def plan_schema(
    table: Schema,
    result: Schema,
    *,
    evolve: bool,
    last_column_id: int,
    next_schema_id: int,
    table_name: str,
) -> SchemaPlan:
    """Decide the schema a `replace` or `append` commits, or refuse the write.

    Parameters
    ----------
    table
        The target's current schema.
    result
        The result's schema.
    evolve
        Whether the caller passed `evolve_schema`.
    last_column_id
        The table metadata's `last_column_id`; new fields are numbered after
        it, never reusing a dropped column's id.
    next_schema_id
        The id the new schema gets, one past every existing schema id.
    table_name
        The target's dotted name, for the error message.

    Returns
    -------
    SchemaPlan
        `schema=None` when nothing differs. Without `evolve`, the table's
        own fields plus any added (always optional) columns. With `evolve`,
        the result's columns in the result's order: a column equal to or
        promoting into the table's keeps the table's field; a column the
        table must widen keeps its field id with the wider type; anything
        else gets a fresh id.

    Raises
    ------
    SchemaMismatchError
        If `evolve` is off and the result drops, renames or retypes a
        column, or adds a required one.
    """
    diff = diff_schemas(table, result)
    if diff.is_empty():
        return SchemaPlan(schema=None, diff=diff)
    table_fields = {f.name: f for f in table.fields}
    result_fields = {f.name: f for f in result.fields}
    next_field_id = last_column_id + 1
    if not evolve:
        if (
            diff.dropped
            or diff.renamed
            or diff.retyped
            or any(result_fields[c.name].required for c in diff.added)
        ):
            raise SchemaMismatchError(table_name, diff)
        fields = list(table.fields)
        for column in diff.added:
            source = result_fields[column.name]
            fields.append(
                NestedField(next_field_id, source.name, source.field_type, required=False)
            )
            next_field_id += 1
        return SchemaPlan(
            schema=Schema(
                *fields,
                schema_id=next_schema_id,
                identifier_field_ids=list(table.identifier_field_ids),
            ),
            diff=diff,
        )
    evolved: list[NestedField] = []
    for source in result.fields:
        existing = table_fields.get(source.name)
        if existing is not None and (
            existing.field_type == source.field_type
            or is_promotion(source.field_type, existing.field_type)
        ):
            evolved.append(existing)
        elif existing is not None and is_promotion(existing.field_type, source.field_type):
            evolved.append(
                NestedField(
                    existing.field_id, existing.name, source.field_type, required=existing.required
                )
            )
        else:
            evolved.append(
                NestedField(next_field_id, source.name, source.field_type, required=False)
            )
            next_field_id += 1
    return SchemaPlan(schema=Schema(*evolved, schema_id=next_schema_id), diff=diff)


def view_columns(schema: pa.Schema) -> list[ColumnSpec]:
    """Describe a view's planned output columns in the API's column form.

    A view stores no data, so a type a table can't store (a list, a
    struct) is still a valid view column; it is reported by its Arrow
    spelling instead of refused.

    Parameters
    ----------
    schema
        The planned result schema (`describe_query`).

    Returns
    -------
    list of ColumnSpec
        One per column, in order.
    """
    columns: list[ColumnSpec] = []
    for field in schema:
        try:
            name = type_name(arrow_to_iceberg_type(field.type, field.name))
        except ValidationError:
            name = str(field.type)
        columns.append(ColumnSpec(name=field.name, type=name, nullable=field.nullable))
    return columns
