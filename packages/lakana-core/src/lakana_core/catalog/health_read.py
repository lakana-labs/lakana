"""Reading a table's health signals: the current state, the history, and what a reader sees.

A `null_rate` or `range` row is **hidden** — `observed`, `band` and `alert` are `None`,
`values_hidden` is `True` — when a row filter applies to the reader (every column), or when a
column mask applies to the reader on that column (compared case-insensitively, because the query
engine reads names that way), or when the reader's policies cannot be read (fail closed).
`volume`, `freshness` and `schema` are never hidden: row counts are already in `table get`, and
the column list is the table's schema.

The rows compare column names only, so a mask whose column was renamed since it was written would
sit on a name the rows no longer use. The query path refuses such a read
(`policy.renames.renamed_policy_column`); here every value of the table is hidden instead, as
for a row filter, and so it is when the table's metadata cannot be read to rule a rename out.
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

_COUNTED = ("volume", "null_rate", "range")
"""The signals a commit records only when it adds rows; a compaction records just the schema."""


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
    """

    masked: frozenset[str] = frozenset()
    filtered: bool = False

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
            True for a ``null_rate`` or ``range`` row under a row filter, or of a masked column.
        """
        if signal not in COLUMN_SIGNALS:
            return False
        return self.filtered or (column is not None and column.casefold() in self.masked)


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
        mask applies and no row filter does, to rule out a renamed masked column.

    Returns
    -------
    Visibility
        Everything value-revealing hidden when the policies cannot be read, when a column a
        mask uses was renamed since the mask was written, or when the metadata that would show
        it could not be read.
    """
    try:
        policy = PolicyResolver(session).resolve(p, ref, table_id=table_id)
    except ValidationError:
        return Visibility(filtered=True)
    masked = frozenset(column.casefold() for column in policy.column_masks)
    if policy.row_filters:
        return Visibility(masked=masked, filtered=True)
    if masked:
        try:
            current = metadata()
        except StorageUnavailableError:
            return Visibility(masked=masked, filtered=True)
        if current is not None and renamed_policy_column(current, policy.policies) is not None:
            return Visibility(masked=masked, filtered=True)
    return Visibility(masked=masked)


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

    The schema is the newest commit's and freshness the newest sweep's. Volume, null rate and
    range are the newest commit's that recorded them, which every commit adding rows does: a
    signal switched off there shows as its off row, and a column it no longer observed (a
    dropped one, say) is left out. A compaction records only the schema, so the commit before it
    is shown.

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
    volume = newest.get(("volume", None))
    counted_at = None if volume is None else volume.observed_at
    shown = [
        row
        for row in newest.values()
        if row.signal not in _COUNTED or row.observed_at == counted_at
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
