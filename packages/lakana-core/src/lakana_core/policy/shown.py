"""How a listing shows a row filter or column mask: its current column names, and `bound`.

A bound policy follows its columns by field id, so the text stored with it may name a column
by a name the table no longer has. A listing shows the names the table has now; the stored text
is untouched. This is display only: a read judges the policy itself
(`field_binding.resolve_expression`).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from pyiceberg.types import NestedField
from sqlalchemy.orm import Session

from lakana_core.access.managed import guard_change
from lakana_core.auth import Action, AuthorizationService, PrincipalRef
from lakana_core.errors import ValidationError
from lakana_core.models import ColumnMask, RowFilter
from lakana_core.policy.columns import ColumnSource
from lakana_core.policy.field_binding import (
    BOUND_STATES,
    FIELD_IDS,
    PolicySchema,
    resolve_expression,
)


@dataclass(frozen=True)
class ShownPolicy:
    """A policy as a listing shows it.

    Parameters
    ----------
    expression
        The expression in the table's current column names; as written when the policy is not
        bound, its field is gone, or the table's schema could not be read.
    column
        A mask's column in the table's current name, as written in the same cases; `None` for a
        row filter.
    bound
        Whether the policy follows its columns (a raw table's by name); `False` for one made
        by an earlier release that the worker could not bind.
    """

    expression: str
    column: str | None
    bound: bool


def _shown(row: RowFilter | ColumnMask, fields: Sequence[NestedField] | None) -> ShownPolicy:
    """Show one policy.

    Parameters
    ----------
    row
        The policy.
    fields
        The top-level fields of its table's current schema; `None` when they cannot be read.

    Returns
    -------
    ShownPolicy
        The policy in current names when it is bound by field id, every field is there and a
        column was renamed; otherwise as written.
    """
    column = row.column if isinstance(row, ColumnMask) else None
    as_written = ShownPolicy(row.expression, column, row.binding in BOUND_STATES)
    if row.binding != FIELD_IDS or fields is None:
        return as_written
    try:
        resolved = resolve_expression(
            row.expression,
            row.field_ids,
            column=column,
            column_field_id=row.column_field_id if isinstance(row, ColumnMask) else None,
            schema=PolicySchema.of(fields),
        )
    except ValidationError:
        return as_written
    if resolved.gone:
        return as_written
    if all(now.casefold() == written.casefold() for written, now in resolved.names.items()):
        # Nothing was renamed (a case-only difference is the same column): the text as written.
        return as_written
    return ShownPolicy(
        resolved.expression, None if column is None else resolved.names[column], True
    )


def shown_policies(
    rows: Sequence[RowFilter | ColumnMask],
    fields_of: Callable[[uuid.UUID], Sequence[NestedField] | None],
) -> dict[uuid.UUID, ShownPolicy]:
    """Show policies in the current column names of their tables.

    Parameters
    ----------
    rows
        The policies to show.
    fields_of
        A table's current top-level fields, or `None` when they cannot be read; called once
        per table.

    Returns
    -------
    dict
        Each row's id to how it is shown.
    """
    read: dict[uuid.UUID, Sequence[NestedField] | None] = {}
    found: dict[uuid.UUID, ShownPolicy] = {}
    for row in rows:
        fields = None
        if row.binding == FIELD_IDS and row.table_id is not None:
            if row.table_id not in read:
                read[row.table_id] = fields_of(row.table_id)
            fields = read[row.table_id]
        found[row.id] = _shown(row, fields)
    return found


class PolicyShowing:
    """The methods the row filter and column mask services share, written once."""

    _catalog: ColumnSource
    _session: Session
    _authz: AuthorizationService

    def _guard(self, p: PrincipalRef, environment: str, change: str) -> None:
        """Guard a change of a policy in a scope an access file may manage (#46).

        Parameters
        ----------
        p
            The acting principal, already authorized to manage policies.
        environment
            The policy's environment.
        change
            What is changed.
        """
        guard_change(
            self._session,
            p,
            environment=environment,
            change=change,
            authz=self._authz,
            action=Action.MANAGE_POLICY,
        )

    def shown(
        self, p: PrincipalRef, rows: Sequence[RowFilter] | Sequence[ColumnMask]
    ) -> dict[uuid.UUID, ShownPolicy]:
        """Show policies in the current column names of their tables.

        Parameters
        ----------
        p
            The acting principal, who listed the rows; reading the columns checks `MANAGE_POLICY`
            again.
        rows
            The row filters or the column masks to show.

        Returns
        -------
        dict
            Each row's id to how a listing shows it (`ShownPolicy`).

        Raises
        ------
        PermissionDenied
            If `p` may not manage policies.
        """
        return shown_policies(rows, partial(self._catalog.policy_fields, p))
