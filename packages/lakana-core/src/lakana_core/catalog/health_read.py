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

import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pyiceberg.schema import index_by_name
from pyiceberg.table.metadata import TableMetadata
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.health_record import NEWEST_COMMIT, ranked
from lakana_core.catalog.health_signals import COLUMN_SIGNALS, SIGNALS, alert_text
from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef, shown
from lakana_core.models import TableHealthSignal
from lakana_core.pagination import Page, newest_first
from lakana_core.policy.renames import renamed_policy_column
from lakana_core.policy.resolver import PolicyResolver

logger = logging.getLogger(__name__)

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
        a masked column has had: none, or a read that failed for any reason but the database's
        (storage that did not answer, a missing or refused file, a damaged one).

    Raises
    ------
    sqlalchemy.exc.SQLAlchemyError
        If the database failed while the metadata was read.
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
    except SQLAlchemyError:
        raise
    except Exception as exc:  # fail closed: a broken table must not fail its health
        logger.warning(
            "The metadata of %s could not be read for its health signals (%s); every value is "
            "hidden from a masked reader.",
            shown(ref.dotted()),
            type(exc).__name__,
        )
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
    # Compared case-folded on both sides: pyiceberg's case-insensitive lookup lowers instead,
    # and `"Maß".lower()` is not `"Maß".casefold()`. A mask on a column the table no longer has
    # finds no field and hides that name only.
    ids = {
        field_id
        for name, field_id in index_by_name(metadata.schema()).items()
        if name.casefold() in masked
    }
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


def _last_fired(
    session: Session, table_id: uuid.UUID
) -> dict[tuple[str, str | None], TableHealthSignal]:
    """Read the newest fired row of each signal and column of a table.

    Only the fired rows are ranked, found through the index on ``(table_id, state, signal,
    column_name)``.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.

    Returns
    -------
    dict
        (signal, column) → its newest fired row.
    """
    window = ranked(TableHealthSignal.table_id == table_id, TableHealthSignal.state == "fired")
    rows = session.scalars(
        select(TableHealthSignal)
        .join(window, window.c.id == TableHealthSignal.id)
        .where(window.c.position == 1)
    )
    return {(row.signal, row.column_name): row for row in rows}


def _newest_of(session: Session, table_id: uuid.UUID, signal: str) -> TableHealthSignal | None:
    """Read the row of a signal that the table's newest commit to record it wrote.

    One ordered seek of the index on ``(table_id, signal, sequence_number, …)``.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    signal
        The signal.

    Returns
    -------
    TableHealthSignal or None
        The row, `NEWEST_COMMIT` first; `None` when the signal has none.
    """
    return session.scalars(
        select(TableHealthSignal)
        .where(
            TableHealthSignal.table_id == table_id,
            TableHealthSignal.signal == signal,
            TableHealthSignal.sequence_number.is_not(None),
        )
        .order_by(*NEWEST_COMMIT)
        .limit(1)
    ).first()


def _commit_rows(
    session: Session, table_id: uuid.UUID, anchors: list[TableHealthSignal]
) -> list[TableHealthSignal]:
    """Read the column rows of the newest commit that wrote any.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    anchors
        The newest row of each column signal.

    Returns
    -------
    list of TableHealthSignal
        Every ``null_rate`` and ``range`` row of the newest anchor's commit: its sequence
        number, snapshot and time.
    """
    if not anchors:
        return []
    newest = max(anchors, key=lambda row: (row.sequence_number, row.observed_at, row.id))
    return list(
        session.scalars(
            select(TableHealthSignal).where(
                TableHealthSignal.table_id == table_id,
                TableHealthSignal.signal.in_(COLUMN_SIGNALS),
                TableHealthSignal.sequence_number == newest.sequence_number,
                TableHealthSignal.snapshot_id == newest.snapshot_id,
                TableHealthSignal.observed_at == newest.observed_at,
            )
        )
    )


def current_signals(
    session: Session, table_id: uuid.UUID, table: str, visibility: Visibility
) -> list[SignalView]:
    """Show the current state of each signal of a table, with the last time each fired.

    Schema, volume and freshness are each their newest row. Null rate and range are the rows
    of the newest commit that wrote any of them, which a commit reading the added files does for
    every column it observed: a signal switched off there shows as its off row, and a column it
    no longer observed (a dropped one, say) is left out. A compaction or a commit that adds no
    data files (a delete) writes none, so the commit before it is shown. "Newest" is
    `NEWEST_COMMIT`: by the snapshot's sequence number, then time, so two commits that share a
    time never mix. Each is found with one ordered seek, so the work does not grow with the
    table's history.

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

    def newest(signals: Iterable[str]) -> list[TableHealthSignal]:
        """Read the newest row of each signal.

        Parameters
        ----------
        signals
            The signals.

        Returns
        -------
        list of TableHealthSignal
            One row per signal that has any.
        """
        found = (_newest_of(session, table_id, signal) for signal in signals)
        return [row for row in found if row is not None]

    rows = newest([signal for signal in SIGNALS if signal not in COLUMN_SIGNALS])
    rows += _commit_rows(session, table_id, newest(COLUMN_SIGNALS))
    rows.sort(key=lambda row: (SIGNALS.index(row.signal), row.column_name or ""))
    fired = _last_fired(session, table_id)
    return [
        _view(
            row,
            table,
            visibility,
            last_fired=fired.get((row.signal, row.column_name)),
            current=True,
        )
        for row in rows
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
