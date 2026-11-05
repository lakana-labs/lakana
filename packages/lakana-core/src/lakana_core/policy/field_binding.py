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

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pyiceberg.types import NestedField

from lakana_core.errors import ValidationError

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
