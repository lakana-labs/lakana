"""Reading a table's health signals: the current state, the history, and what a reader sees.

A `null_rate` or `range` row is **hidden** — `observed`, `band` and `alert` are `None`,
`values_hidden` is `True` — when a row filter applies to the reader (every column), or when a
column mask applies to the reader on that column (compared case-insensitively, because the query
engine reads names that way), or when the reader's policies cannot be read (fail closed).
`volume`, `freshness` and `schema` are never hidden: row counts are already in `table get`, and
the column list is the table's schema.

Rows keep the name a column had when they were recorded, and a mask names the column as it is
now, so a masked column is hidden under every name its field has had in the table's schemas, and
under any name the current schema does not have (history whose column cannot be told). A mask
whose column was renamed since it was written is refused by the query path
(`policy.renames.renamed_policy_column`); here every value of the table is hidden instead, as
for a row filter, and so it is when the table's metadata cannot be read, or the table has none.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pyiceberg.table.metadata import TableMetadata
from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.health_record import ranked
from lakana_core.catalog.health_signals import COLUMN_SIGNALS, SIGNALS, alert_text
from lakana_core.errors import StorageUnavailableError, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import TableHealthSignal
from lakana_core.pagination import Page, newest_first
from lakana_core.policy.renames import renamed_policy_column
from lakana_core.policy.resolver import PolicyResolver

HISTORY_KIND = "health_signals"
"""The cursor kind of the history (`pagination.encode_cursor`)."""

UNKNOWN_SIGNAL_MESSAGE = (
    "There is no health signal `{signal}`; use freshness, volume, null_rate, range or schema."
)


@dataclass(frozen=True)
class SignalView:
    """One health signal row as a reader sees it.

    Parameters
    ----------
    id
        The row's id.
    signal
        One of `SIGNALS`.
    column
        The column, for a column signal.
    state
        One of `STATES`.
    effect
        The effect that applied.
    snapshot_id
        The snapshot observed.
    observed_at
        When it was recorded.
    observed
        The observed values; `None` when hidden.
    band
        The usual band; `None` when hidden.
    alert
        The alert of the row, or of its last firing for the current state; `None` when hidden.
    values_hidden
        Whether `observed`, `band` and `alert` were left out for this reader.
    last_fired_at
        When this signal and column last fired; only for the current state.
    """

    id: uuid.UUID
    signal: str
    column: str | None
    state: str
    effect: str
    snapshot_id: int | None
    observed_at: datetime
    observed: dict[str, Any] | None
    band: dict[str, Any] | None
    alert: str | None
    values_hidden: bool
    last_fired_at: datetime | None = None


@dataclass(frozen=True)
class Visibility:
    """Which values of a table's signals one reader may not see.

    Parameters
    ----------
    masked
        The columns a mask applies to for the reader, case-folded.
    filtered
        Whether a row filter applies to the reader, or the policies could not be read.
    columns
        The current schema's column names, case-folded, when a mask applies: a column row of
        another name is hidden too. `None` hides by `masked` alone.
    """

    masked: frozenset[str] = frozenset()
    filtered: bool = False
    columns: frozenset[str] | None = None

    def hides(self, signal: str, column: str | None) -> bool:
        """Say whether a row's values are left out for this reader.

        Parameters
        ----------
        signal
            The row's signal.
        column
            The row's column.

        Returns
        -------
        bool
            True for a ``null_rate`` or ``range`` row under a row filter, of a masked column, or
            of a column `columns` does not list.
        """
        if signal not in COLUMN_SIGNALS:
            return False
        if self.filtered:
            return True
        if column is None:
            return False
        name = column.casefold()
        return name in self.masked or (self.columns is not None and name not in self.columns)


def visibility_for(
    session: Session,
    p: PrincipalRef,
    ref: TableRef,
    table_id: uuid.UUID,
    *,
    metadata: Callable[[], TableMetadata | None],
) -> Visibility:
    """Find which values a reader may not see, from the policies that apply to them.

    Parameters
    ----------
    session
        Database session.
    p
        The reader.
    ref
        The table.
    table_id
        Its id.
    metadata
        Reads the table's current metadata, `None` for a table without any; called only when a
        mask applies and no row filter does, to find every name of a masked column.

    Returns
    -------
    Visibility
        Everything value-revealing hidden when the policies cannot be read, when a column a
        mask uses was renamed since the mask was written, or when no metadata says which names
        a masked column has had (none, or unreadable).
    """
    try:
        policy = PolicyResolver(session).resolve(p, ref, table_id=table_id)
    except ValidationError:
        return Visibility(filtered=True)
    masked = frozenset(column.casefold() for column in policy.column_masks)
    if policy.row_filters:
        return Visibility(masked=masked, filtered=True)
    if not masked:
        return Visibility()
    try:
        current = metadata()
    except StorageUnavailableError:
        return Visibility(masked=masked, filtered=True)
    if current is None or renamed_policy_column(current, policy.policies) is not None:
        return Visibility(masked=masked, filtered=True)
    return Visibility(
        masked=_every_name(current, masked),
        columns=frozenset(name.casefold() for name in current.schema().column_names),
    )


def _every_name(metadata: TableMetadata, masked: frozenset[str]) -> frozenset[str]:
    """Add every name the masked columns' fields have had in the table's schemas.

    Parameters
    ----------
    metadata
        The table's metadata.
    masked
        The masked columns, case-folded, as the current schema names them.

    Returns
    -------
    frozenset of str
        `masked` and each earlier name of the same field ids, case-folded.
    """
    schema = metadata.schema()
    ids = set()
    for name in masked:
        try:
            ids.add(schema.find_field(name, case_sensitive=False).field_id)
        except ValueError:
            continue  # a mask on a column the table no longer has hides that name only
    names = set(masked)
    for each in metadata.schemas:
        for field_id in ids:
            name = each.find_column_name(field_id)
            if name is not None:
                names.add(name.casefold())
    return frozenset(names)


def _alert(table: str, row: TableHealthSignal) -> str | None:
    """Write a fired row's alert.

    Parameters
    ----------
    table
        The table's dotted name.
    row
        The row.

    Returns
    -------
    str or None
        The text; `None` for a row that did not fire, or whose stored values are damaged.
    """
    try:
        return alert_text(
            table,
            row.signal,
            row.column_name,
            row.state,
            row.observed,
            row.band,
            snapshot_id=row.snapshot_id,
            observed_at=row.observed_at,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _view(
    row: TableHealthSignal,
    table: str,
    visibility: Visibility,
    *,
    last_fired: TableHealthSignal | None = None,
    current: bool = False,
) -> SignalView:
    """Show one row to a reader.

    Parameters
    ----------
    row
        The row.
    table
        The table's dotted name.
    visibility
        What the reader may not see.
    last_fired
        The newest fired row of the same signal and column, for the current state.
    current
        Whether the row is the current state: its alert and `last_fired_at` then come from
        `last_fired`.

    Returns
    -------
    SignalView
        The row, its values left out when `visibility` hides them.
    """
    hidden = visibility.hides(row.signal, row.column_name)
    source = last_fired if current else row
    alert = None if hidden or source is None else _alert(table, source)
    return SignalView(
        id=row.id,
        signal=row.signal,
        column=row.column_name,
        state=row.state,
        effect=row.effect,
        snapshot_id=row.snapshot_id,
        observed_at=row.observed_at,
        observed=None if hidden else row.observed,
        band=None if hidden else row.band,
        alert=alert,
        values_hidden=hidden,
        last_fired_at=last_fired.observed_at if current and last_fired is not None else None,
    )


def _newest(
    session: Session, table_id: uuid.UUID, *conditions: ColumnElement[bool]
) -> dict[tuple[str, str | None], TableHealthSignal]:
    """Read the newest row of each signal and column of a table.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    *conditions
        Which rows take part.

    Returns
    -------
    dict
        (signal, column) → its newest row.
    """
    window = ranked(TableHealthSignal.table_id == table_id, *conditions)
    rows = session.scalars(
        select(TableHealthSignal)
        .join(window, window.c.id == TableHealthSignal.id)
        .where(window.c.position == 1)
    )
    return {(row.signal, row.column_name): row for row in rows}


def current_signals(
    session: Session, table_id: uuid.UUID, table: str, visibility: Visibility
) -> list[SignalView]:
    """Show the current state of each signal of a table, with the last time each fired.

    Schema, volume and freshness are each their newest row. Null rate and range are the rows
    of the newest commit that wrote any of them, which a commit reading the added files does for
    every column it observed: a signal switched off there shows as its off row, and a column it
    no longer observed (a dropped one, say) is left out. A compaction or a commit that adds no
    data files (a delete) writes none, so the commit before it is shown.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    table
        Its dotted name, for the alerts.
    visibility
        What the reader may not see.

    Returns
    -------
    list of SignalView
        In the order of `SIGNALS`, then by column.
    """
    newest = _newest(session, table_id)
    fired = _newest(session, table_id, TableHealthSignal.state == "fired")
    column_at = max(
        (row.observed_at for row in newest.values() if row.signal in COLUMN_SIGNALS),
        default=None,
    )
    shown = [
        row
        for row in newest.values()
        if row.signal not in COLUMN_SIGNALS or row.observed_at == column_at
    ]
    shown.sort(key=lambda row: (SIGNALS.index(row.signal), row.column_name or ""))
    return [
        _view(
            row,
            table,
            visibility,
            last_fired=fired.get((row.signal, row.column_name)),
            current=True,
        )
        for row in shown
    ]


def signal_history(
    session: Session,
    table_id: uuid.UUID,
    table: str,
    visibility: Visibility,
    *,
    since: datetime,
    signal: str | None,
    column: str | None,
    limit: int,
    cursor: str | None,
) -> Page[SignalView]:
    """List a table's health signal rows, newest first.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    table
        Its dotted name, for the alerts.
    visibility
        What the reader may not see.
    since
        The oldest time shown.
    signal
        Only this signal.
    column
        Only this column.
    limit
        The most rows of the page.
    cursor
        Where the previous page ended.

    Returns
    -------
    Page of SignalView
        One page; a fired row carries its own alert.

    Raises
    ------
    ValidationError
        If `limit` is below 1 or the cursor is not one of this list.
    """
    query = select(TableHealthSignal).where(
        TableHealthSignal.table_id == table_id, TableHealthSignal.observed_at >= since
    )
    if signal is not None:
        query = query.where(TableHealthSignal.signal == signal)
    if column is not None:
        query = query.where(TableHealthSignal.column_name == column)
    page = newest_first(
        session,
        query,
        kind=HISTORY_KIND,
        at=TableHealthSignal.observed_at,
        row_id=TableHealthSignal.id,
        at_of=lambda row: row.observed_at,
        limit=limit,
        cursor=cursor,
    )
    return Page([_view(row, table, visibility) for row in page.items], page.next_cursor)
