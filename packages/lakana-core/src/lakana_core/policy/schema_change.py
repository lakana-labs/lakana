"""Row filters and column masks that a write's schema change leaves out of date (#376).

A write with `evolve_schema` can remove a column, or replace it with a new column of the same
name when its type changes in a way that does not widen. A row filter or column mask that uses
such a column then refuses the reads of the people it applies to until an administrator creates
it again (`lakana_core.policy.renames`). The write still lands; this module finds those policies
when the write is planned, so that the run can say so at once rather than when readers' queries
fail.

A task run stores, for its writes that removed or replaced a column, the tables and the policies
found (`merge_schema_change`): ``{"tables": [...], "policies": [BrokenPolicy.to_json(), ...]}``.
Only a principal who may manage policies learns from it that a policy exists
(`schema_change_for_reader`): `lakana table get` shows no policies, and listing them needs that
right. Everyone else sees one neutral line, the same whether or not a policy is affected, on every
run that removed or replaced a column. The expression is never stored here.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import sqlglot.errors
from pyiceberg.schema import Schema
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.identifiers import shown
from lakana_core.models import ColumnMask, RowFilter
from lakana_core.policy.expressions import columns_of

ROW_FILTER = "row_filter"
"""The kind of a row filter."""
COLUMN_MASK = "column_mask"
"""The kind of a column mask."""
REMOVED = "removed"
"""A column the write removed from the table."""
REPLACED = "replaced"
"""A column the write replaced with a new column of the same name."""

_KINDS = {
    ROW_FILTER: ("Row filter", "row filter", "filter"),
    COLUMN_MASK: ("Column mask", "column mask", "mask"),
}
"""Per kind: the name at the start of a sentence, inside one, and the short name."""

_CHANGES = {
    REMOVED: "removed from the table",
    REPLACED: (
        "replaced: its type changed in a way that does not widen, so it is a new column under "
        "the same name"
    ),
}
"""What each change reads as, after "which this run"."""

_REVOKE_FIRST = {
    ROW_FILTER: "revoking it first shows those people every row until the new one exists",
    COLUMN_MASK: "revoking it first shows those people the real values until the new one exists",
}
"""Why the order matters, per kind."""

_FIELDS = ("table", "kind", "policy_id", "column", "change")

COLUMNS_CHANGED_NOTE = (
    "This run changed the table's columns; an administrator has been told what to check."
)
"""What a reader who may not manage policies is shown for a task whose write removed or replaced a
column, whether or not a row filter or column mask uses it: its presence must not tell that one
exists."""


@dataclass(frozen=True)
class BrokenPolicy:
    """A row filter or column mask that uses a column a write removed or replaced.

    Parameters
    ----------
    table
        The table's dotted name.
    kind
        `ROW_FILTER` or `COLUMN_MASK`.
    policy_id
        The policy's id.
    column
        The column, as the policy spells it.
    change
        `REMOVED` or `REPLACED`.
    """

    table: str
    kind: str
    policy_id: uuid.UUID
    column: str
    change: str

    def to_json(self) -> dict[str, str]:
        """Render it as stored on the task run and in the audit event.

        Returns
        -------
        dict
            ``{"table", "kind", "policy_id", "column", "change"}``, all text.
        """
        return {
            "table": self.table,
            "kind": self.kind,
            "policy_id": str(self.policy_id),
            "column": self.column,
            "change": self.change,
        }

    @staticmethod
    def from_json(data: object) -> BrokenPolicy | None:
        """Read a stored entry back.

        Parameters
        ----------
        data
            One stored entry.

        Returns
        -------
        BrokenPolicy or None
            The entry, or `None` when it is not one this module wrote.
        """
        if not isinstance(data, Mapping) or not all(
            isinstance(data.get(key), str) for key in _FIELDS
        ):
            return None
        if data["kind"] not in _KINDS or data["change"] not in _CHANGES:
            return None
        try:
            policy_id = uuid.UUID(data["policy_id"])
        except ValueError:
            return None
        return BrokenPolicy(data["table"], data["kind"], policy_id, data["column"], data["change"])


@dataclass(frozen=True)
class PolicyWarningView:
    """A policy warning of a task, as a reader who may manage policies is shown it.

    Parameters
    ----------
    kind
        ``row_filter`` or ``column_mask``.
    column
        The column the policy uses.
    change
        ``removed`` or ``replaced``.
    policy_id
        The policy's id; only a reader who may manage policies is shown a warning at all.
    message
        One paragraph: what happened and the next step.
    """

    kind: str
    column: str
    change: str
    policy_id: uuid.UUID
    message: str


def changed_columns(before: Schema, after: Schema) -> dict[str, str]:
    """Name the columns of a table whose field a new schema does not keep under their name.

    Names are compared without regard to letter case, as the database reads a policy's columns.

    Parameters
    ----------
    before
        The table's current schema.
    after
        The schema the write makes current.

    Returns
    -------
    dict
        The case-folded name of each such column, to `REMOVED` (no column of that name is left)
        or `REPLACED` (a column of that name has another field id). A widened column keeps its
        field id and is not listed.
    """
    kept = {field.name.casefold(): field.field_id for field in after.fields}
    changed: dict[str, str] = {}
    for field in before.fields:
        name = field.name.casefold()
        if name not in kept:
            changed[name] = REMOVED
        elif kept[name] != field.field_id:
            changed[name] = REPLACED
    return changed


def _read_columns(expression: str) -> set[str]:
    """Name the columns a stored expression reads, or none when it does not parse.

    A stored policy passed validation when it was created, but one of an earlier release may not
    parse today; the write must not fail for that.

    Parameters
    ----------
    expression
        The stored expression.

    Returns
    -------
    set of str
        The columns, as the expression spells them.
    """
    try:
        return columns_of(expression)
    except sqlglot.errors.SqlglotError:
        return set()


def broken_policies(
    session: Session, table_id: uuid.UUID, table: str, changed: Mapping[str, str]
) -> tuple[BrokenPolicy, ...]:
    """Find the active row filters and column masks a schema change leaves out of date.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table's id: policies bind to it, never to the name.
    table
        The table's dotted name, for the warnings.
    changed
        The removed or replaced columns (`changed_columns`).

    Returns
    -------
    tuple of BrokenPolicy
        One per policy and column, column masks first, then by column and id; empty without a
        removed or replaced column (no policy is read then).
    """
    if not changed:
        return ()
    used: list[tuple[str, uuid.UUID, set[str]]] = [
        (ROW_FILTER, row.id, _read_columns(row.expression))
        for row in session.scalars(
            select(RowFilter).where(RowFilter.table_id == table_id, RowFilter.revoked_at.is_(None))
        )
    ]
    used += [
        (COLUMN_MASK, row.id, {row.column, *_read_columns(row.expression)})
        for row in session.scalars(
            select(ColumnMask).where(
                ColumnMask.table_id == table_id, ColumnMask.revoked_at.is_(None)
            )
        )
    ]
    found = [
        BrokenPolicy(table, kind, policy_id, column, changed[column.casefold()])
        for kind, policy_id, columns in used
        for column in columns
        if column.casefold() in changed
    ]
    return tuple(sorted(found, key=lambda b: (b.kind, b.column.casefold(), str(b.policy_id))))


def _message(warning: BrokenPolicy) -> str:
    """Word one warning for a reader who may manage policies.

    Parameters
    ----------
    warning
        The warning.

    Returns
    -------
    str
        What happened, what it means for reads, and the next step.
    """
    title, name, short = _KINDS[warning.kind]
    happened = (
        f"uses column `{shown(warning.column)}`, which this run {_CHANGES[warning.change]}. "
        f"Reads of the table by the people the {short} applies to are refused until it is "
        "created again."
    )
    table = f"`{shown(warning.table)}`"
    command = f"lakana {name.replace(' ', '-')} revoke {warning.policy_id}"
    return (
        f"{title} {warning.policy_id} on {table} {happened} Next: create the {short} again for "
        f"the column it is meant for, then revoke this one (`{command}`), in that order: "
        f"{_REVOKE_FIRST[warning.kind]}."
    )


def merge_schema_change(
    held: object, table: str, policies: Sequence[Mapping[str, str]]
) -> dict[str, list]:
    """Add one write's schema change to what its task run holds.

    Parameters
    ----------
    held
        `TaskRun.schema_change_json` before the write; anything but this module's shape counts as
        nothing.
    table
        The table whose columns the write removed or replaced.
    policies
        The policies it left out of date, as stored (`BrokenPolicy.to_json`).

    Returns
    -------
    dict
        ``{"tables": [...], "policies": [...]}``, each entry once, in the order first seen.
    """
    tables = changed_tables(held)
    old = held.get("policies") if isinstance(held, Mapping) else None
    merged: list = list(old) if isinstance(old, list) else []
    merged += [dict(policy) for policy in policies if dict(policy) not in merged]
    return {"tables": [*tables, *([] if table in tables else [table])], "policies": merged}


def changed_tables(stored: object) -> tuple[str, ...]:
    """Name the tables whose columns a task's writes removed or replaced.

    Parameters
    ----------
    stored
        `TaskRun.schema_change_json`.

    Returns
    -------
    tuple of str
        The tables, in stored order; empty for nothing or anything this module did not write.
    """
    tables = stored.get("tables") if isinstance(stored, Mapping) else None
    if not isinstance(tables, list):
        return ()
    return tuple(table for table in tables if isinstance(table, str))


def schema_change_for_reader(
    stored: object, *, manages_policies: bool
) -> tuple[str | None, tuple[PolicyWarningView, ...]]:
    """Show a task's schema change to one reader of its run.

    Parameters
    ----------
    stored
        `TaskRun.schema_change_json`; entries this module did not write are skipped.
    manages_policies
        Whether the reader may manage policies. Only then are the policies shown, with their ids:
        nobody else may learn that a policy exists.

    Returns
    -------
    tuple of (str or None, tuple of PolicyWarningView)
        `COLUMNS_CHANGED_NOTE` when the task removed or replaced a column (`None` otherwise), the
        same for every reader; and the warnings, empty unless `manages_policies`.
    """
    if not changed_tables(stored):
        return None, ()
    if not manages_policies:
        return COLUMNS_CHANGED_NOTE, ()
    assert isinstance(stored, Mapping)  # nosec B101 -- changed_tables found a mapping
    policies = stored.get("policies")
    views: list[PolicyWarningView] = []
    for entry in policies if isinstance(policies, list) else ():
        warning = BrokenPolicy.from_json(entry)
        if warning is None:
            continue
        views.append(
            PolicyWarningView(
                kind=warning.kind,
                column=warning.column,
                change=warning.change,
                policy_id=warning.policy_id,
                message=_message(warning),
            )
        )
    return COLUMNS_CHANGED_NOTE, tuple(views)
