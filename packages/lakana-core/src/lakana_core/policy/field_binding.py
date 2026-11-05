"""Binding a row filter's or column mask's column names to Iceberg field ids.

A policy is written with names; an Iceberg table is read by field id. At creation each name the
policy uses is bound to the id of the one top-level field of that name, and stored beside the
text (`field_ids`, and a mask's `column_field_id`). A read maps the ids back to the names of the
schema it reads and rewrites only the column references, so a policy follows its column across
renames and refuses when its field is gone.

Every policy row has a binding state: `FIELD_IDS` (bound here), `BY_NAME` (a raw table, whose
files are read by name), `PENDING` (made by a release before the binding, not yet looked at by
the worker's backfill) or `UNBOUND` (could not be bound exactly). The last two keep the rename
rule of `policy.renames`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import sqlglot
import sqlglot.expressions as exp
from pyiceberg.types import NestedField

from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown
from lakana_core.sql_syntax import SQL_SYNTAX_ERRORS

FIELD_IDS = "field_ids"
"""Bound to field ids at creation or by the upgrade's backfill."""

BY_NAME = "names"
"""A policy on a raw table: its files are read by name, and so is the policy."""

PENDING = "pending"
"""Made before the binding existed and not yet looked at by the backfill."""

UNBOUND = "unbound"
"""Made before the binding existed and not bindable exactly; refused after a rename."""

BOUND_STATES = frozenset({FIELD_IDS, BY_NAME})
"""The states shown as `bound`."""

UNREADABLE_POLICY_MESSAGE = (
    "A row filter or column mask stored on this table could not be read. "
    "Ask an administrator to correct or remove it."
)
"""Shown to a reader when a stored policy no longer parses; it names no policy text, which the
reader may not be allowed to see."""

POLICY_COLUMN_GONE = (
    "`{table}` has a row filter or column mask on a column that was dropped or replaced "
    "(created for `{column}`); an administrator creates it again for the column it is meant "
    "for, or revokes it"
)
"""Why a read is refused when a column a bound policy uses is no longer in the table."""


class AmbiguousPolicyColumnError(ValidationError):
    """A policy names a column that two columns of the table match without regard to case."""


@dataclass(frozen=True)
class PolicyBinding:
    """What a new policy stores beside its text.

    Parameters
    ----------
    binding
        `FIELD_IDS` or `BY_NAME`.
    field_ids
        Each column name the policy uses, as written, to its field id; `None` for `BY_NAME`.
    column_field_id
        A mask's own column's field id; `None` for a row filter and for `BY_NAME`.
    """

    binding: str
    field_ids: dict[str, int] | None
    column_field_id: int | None


def match_column(fields: Sequence[NestedField], name: str) -> int | None:
    """Find the top-level field a policy's column name means.

    The name matches a field equal to it under `str.lower()`, the comparison the read uses
    (`catalog.field_ids.read_fields`). A second field equal to it under `str.casefold()` makes
    the name ambiguous (`Maß` and `MASS`), which is refused rather than guessed. Nested fields
    never match: a policy cannot name one.

    Parameters
    ----------
    fields
        The top-level fields of the schema the policy is checked against.
    name
        The name as the policy writes it.

    Returns
    -------
    int or None
        The field id; `None` when no field matches.

    Raises
    ------
    AmbiguousPolicyColumnError
        If another field matches the name under casefold.
    """
    lowered = [field for field in fields if field.name.lower() == name.lower()]
    if not lowered:
        return None
    folded = sorted(field.name for field in fields if field.name.casefold() == name.casefold())
    if len(folded) > 1:
        listed = " and ".join(repr(each) for each in folded)
        raise AmbiguousPolicyColumnError(
            f"The policy names column {name!r}, and the table has the columns {listed}, which "
            "Lakana cannot tell apart for a policy. Ask the owner of the table to rename one of "
            "them, then create the policy again."
        )
    return lowered[0].field_id


def bind_policy(
    fields: Sequence[NestedField], columns: Iterable[str], *, masked_column: str | None = None
) -> PolicyBinding:
    """Bind the names a new policy uses to field ids.

    Parameters
    ----------
    fields
        The top-level fields of the schema whose id the policy records.
    columns
        The names its expression reads (`policy.expressions.columns_of`).
    masked_column
        A mask's own column, matched exactly as `column_masks._check_column_exists` checked it.

    Returns
    -------
    PolicyBinding
        `FIELD_IDS`, with every name bound.

    Raises
    ------
    ValidationError
        If a name matches no field (the creation checks refuse that first; this keeps a policy
        from being stored half-bound).
    AmbiguousPolicyColumnError
        If a name matches two fields under casefold.
    """
    ids: dict[str, int] = {}
    for name in sorted({*columns, *([masked_column] if masked_column else [])}):
        # Every name is looked up under casefold too, the masked column included.
        field_id = match_column(fields, name)
        if name == masked_column:
            # A mask names its own column exactly.
            field_id = next((f.field_id for f in fields if f.name == name), None)
        if field_id is None:
            raise ValidationError(f"The table has no column {name!r} to bind the policy to.")
        ids[name] = field_id
    column_id = ids[masked_column] if masked_column is not None else None
    return PolicyBinding(FIELD_IDS, ids, column_id)


def stored_field_ids(value: object) -> dict[str, int]:
    """Read the field ids stored with a policy, failing closed on anything else.

    Parameters
    ----------
    value
        The stored column value.

    Returns
    -------
    dict of str to int
        A copy of the map from column name to field id.

    Raises
    ------
    ValidationError
        If `value` is not a `dict` of `str` keys to `int` (not `bool`) values.
    """
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(each, int) and not isinstance(each, bool)
        for key, each in value.items()
    ):
        raise ValidationError(UNREADABLE_POLICY_MESSAGE)
    return dict(value)


def bound_field_ids(
    field_ids: object, *, column: str | None, column_field_id: object
) -> dict[str, int]:
    """Read the ids a bound policy follows, its own column's included, failing closed.

    Parameters
    ----------
    field_ids
        The stored `field_ids` of a row whose binding is `FIELD_IDS`.
    column
        A mask's own column as written; `None` for a row filter.
    column_field_id
        A mask's stored `column_field_id`; ignored for a row filter.

    Returns
    -------
    dict of str to int
        Each name as written to its field id; for a mask, its column to `column_field_id`.

    Raises
    ------
    ValidationError
        `UNREADABLE_POLICY_MESSAGE` if `field_ids` is not a map of names to ids, a mask has no
        integer id of its own, or its expression's key for its own column holds another id: a
        bound row whose ids do not add up is refused, never skipped.
    """
    ids = stored_field_ids(field_ids)
    if column is None:
        return ids
    if not isinstance(column_field_id, int) or isinstance(column_field_id, bool):
        raise ValidationError(UNREADABLE_POLICY_MESSAGE)
    if ids.get(column, column_field_id) != column_field_id:
        raise ValidationError(UNREADABLE_POLICY_MESSAGE)
    return {**ids, column: column_field_id}


@dataclass(frozen=True)
class PolicySchema:
    """The schema a read uses and the table's current one, both as top-level fields.

    Parameters
    ----------
    read
        The fields the read projects: the current schema, or a check's snapshot's.
    current
        The table's current schema's fields, from the same metadata as `read`.
    """

    read: tuple[NestedField, ...]
    current: tuple[NestedField, ...]

    @classmethod
    def of(cls, fields: Sequence[NestedField]) -> PolicySchema:
        """Describe a read of the current schema.

        Parameters
        ----------
        fields
            The current schema's top-level fields.

        Returns
        -------
        PolicySchema
            `read` and `current` both `fields`.
        """
        return cls(tuple(fields), tuple(fields))


@dataclass(frozen=True)
class ResolvedNames:
    """A bound policy's names in the schema read.

    Parameters
    ----------
    names
        Each name as written to the name its field has in the read (or, absent there, now).
    gone
        The names as written whose field the current schema does not have, sorted.
    """

    names: dict[str, str]
    gone: tuple[str, ...]


def resolve_names(field_ids: Mapping[str, int], schema: PolicySchema) -> ResolvedNames:
    """Map a bound policy's field ids to the names of the schema read.

    Whether a field is gone is judged against the current schema only: a field the table has
    now but a check's snapshot lacks is named by its current name, and one the table no
    longer has is gone even when the snapshot still has it.

    Parameters
    ----------
    field_ids
        The stored binding: name as written to field id.
    schema
        The read's and the current schema's top-level fields.

    Returns
    -------
    ResolvedNames
        The names, and the written names whose field is gone.
    """
    read = {field.field_id: field.name for field in schema.read}
    now = {field.field_id: field.name for field in schema.current}
    names: dict[str, str] = {}
    gone: list[str] = []
    for written, field_id in field_ids.items():
        if field_id not in now:
            gone.append(written)
        else:
            names[written] = read.get(field_id, now[field_id])
    return ResolvedNames(names, tuple(sorted(gone, key=lambda name: (name.casefold(), name))))


def rename_columns(expression: str, names: Mapping[str, str]) -> str:
    """Rewrite the column references of a stored expression to the names of the schema read.

    Only `Column` nodes change; literals, `has_group` arguments and every other node keep
    their text. Each column is written quoted, so the read binds exactly the field's name.

    Parameters
    ----------
    expression
        The stored row filter or mask expression.
    names
        Name as written to the name in the schema read (`resolve_names`).

    Returns
    -------
    str
        The expression in DuckDB's dialect.

    Raises
    ------
    ValidationError
        `UNREADABLE_POLICY_MESSAGE` if it does not parse, qualifies a column, or names a column
        the binding does not have: the policy is never applied as written instead.
    """
    try:
        parsed = sqlglot.parse_one(expression, dialect="duckdb", into=exp.Condition)
    except SQL_SYNTAX_ERRORS as exc:
        raise ValidationError(UNREADABLE_POLICY_MESSAGE) from exc
    for column in list(parsed.find_all(exp.Column)):
        if column.table or column.name not in names:
            raise ValidationError(UNREADABLE_POLICY_MESSAGE)
        column.set("this", exp.to_identifier(names[column.name], quoted=True))
    return parsed.sql(dialect="duckdb")


@dataclass(frozen=True)
class ResolvedExpression:
    """A bound policy's expression in the names of the schema read.

    Parameters
    ----------
    expression
        The rewritten expression; the text as written when a field is gone.
    names
        Each name as written to its name in the read (`ResolvedNames.names`); empty when a
        field is gone.
    gone
        The names as written whose field the table no longer has, sorted.
    """

    expression: str
    names: dict[str, str]
    gone: tuple[str, ...]


def resolve_expression(
    expression: str,
    field_ids: object,
    *,
    column: str | None,
    column_field_id: object,
    schema: PolicySchema,
) -> ResolvedExpression:
    """Resolve a bound row filter or column mask against the schema read.

    The one place every reader of a bound policy goes through: the ids it follows
    (`bound_field_ids`) are mapped to the read's names (`resolve_names`) and, when none is
    gone, its column references are rewritten to them (`rename_columns`).

    Parameters
    ----------
    expression
        The stored expression.
    field_ids
        The stored `field_ids`.
    column
        A mask's own column as written; `None` for a row filter.
    column_field_id
        A mask's stored `column_field_id`; ignored for a row filter.
    schema
        The read's and the current schema's top-level fields.

    Returns
    -------
    ResolvedExpression
        The expression and names in the schema read, or the written names that are gone.

    Raises
    ------
    ValidationError
        `UNREADABLE_POLICY_MESSAGE` if the stored ids or the expression cannot be read.
    """
    ids = bound_field_ids(field_ids, column=column, column_field_id=column_field_id)
    resolved = resolve_names(ids, schema)
    if resolved.gone:
        return ResolvedExpression(expression, {}, resolved.gone)
    return ResolvedExpression(
        rename_columns(expression, resolved.names), resolved.names, resolved.gone
    )


def policy_column_gone_message(column: str, table: str) -> str:
    """Word the refusal of a read whose bound policy uses a column the table no longer has.

    Parameters
    ----------
    column
        The policy's column, as the policy spells it.
    table
        The table's dotted name, as the query addressed it.

    Returns
    -------
    str
        `POLICY_COLUMN_GONE` with both names shown.
    """
    return POLICY_COLUMN_GONE.format(table=shown(table), column=shown(column))
