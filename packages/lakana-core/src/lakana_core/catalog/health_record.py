"""Recording the health signals of a snapshot as it is committed (#340).

`CatalogService.commit_table` and `CatalogService._publish_table` call `commit_observations`
after every refusal and before the metadata pointer moves, and add the `signal_rows` to the
transaction that moves it, so a snapshot and its signals land together or not at all. Every read
happens before the swap: first the data files the commit adds (those the field-id check already
read, or the manifests read again with its bounds, `field_ids.MAX_CHECKED_FILES` and
`HEALTH_DEADLINE_SECONDS`), then the history. What cannot be read, and any error that is not the
database's, is recorded as ``no_metrics``. Nothing here fails a write.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pyiceberg.io import FileIO
from pyiceberg.manifest import DataFile
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.snapshots import Operation, Snapshot
from sqlalchemy import ColumnElement, Subquery, and_, func, not_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import InstrumentedAttribute, Session

from lakana_core.catalog import field_ids
from lakana_core.catalog.field_ids import added_data_files
from lakana_core.catalog.health_signals import (
    COMMIT_SIGNALS,
    HISTORY,
    HealthEffects,
    Observation,
    apply_effects,
    column_metrics,
    columns_of,
    null_rate_signal,
    range_signal,
    schema_signal,
    volume_signal,
)
from lakana_core.errors import StorageUnavailableError, ValidationError
from lakana_core.identifiers import shown
from lakana_core.models import TableHealthSignal

logger = logging.getLogger(__name__)

HEALTH_DEADLINE_SECONDS = 5.0
"""How long one commit may spend reading the manifests of its new snapshots for the metrics."""

_LEARNED_STATES = ("ok", "fired", "no_history")
"""The states whose observed values a band is learned from."""

_BY_COLUMN: tuple[InstrumentedAttribute[Any], ...] = (
    TableHealthSignal.signal,
    TableHealthSignal.column_name,
)


def ranked(
    *conditions: ColumnElement[bool],
    partition: Sequence[InstrumentedAttribute[Any]] = _BY_COLUMN,
) -> Subquery:
    """Rank the health signal rows newest first within each partition.

    The one window over `table_health_signals` that the history, the current state of a
    table and the retention share: rows are ordered by ``observed_at`` then ``id``, both
    descending, so position 1 is the newest row and ties break the same way everywhere.

    Parameters
    ----------
    *conditions
        Which rows take part, as `where` conditions.
    partition
        The columns that start a new count; by default signal and column.

    Returns
    -------
    Subquery
        Columns ``id`` (a row's id) and ``position`` (1 for the newest of its partition).
    """
    return (
        select(
            TableHealthSignal.id,
            func.row_number()
            .over(
                partition_by=tuple(partition),
                order_by=(TableHealthSignal.observed_at.desc(), TableHealthSignal.id.desc()),
            )
            .label("position"),
        )
        .where(*conditions)
        .subquery()
    )


def recent_observations(
    session: Session, table_id: uuid.UUID
) -> dict[tuple[str, str | None], list[dict[str, Any]]]:
    """Read the newest observed values of each commit signal and column of a table.

    Only rows observed since the (2 x `HISTORY`)-th newest ``schema`` row are ranked: every
    commit records one ``schema`` row, so that reaches back far enough for `HISTORY` values
    of each signal while a table's whole retention is never ranked on a commit.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.

    Returns
    -------
    dict
        (signal, column) → observed values, newest first, at most `HISTORY` each, from rows in
        state ``ok``, ``fired`` or ``no_history``; a fired row whose effect was ``block`` is left
        out, because its data was set back.
    """
    since = session.scalar(
        select(TableHealthSignal.observed_at)
        .where(TableHealthSignal.table_id == table_id, TableHealthSignal.signal == "schema")
        .order_by(TableHealthSignal.observed_at.desc(), TableHealthSignal.id.desc())
        .offset(2 * HISTORY - 1)
        .limit(1)
    )
    conditions = [
        TableHealthSignal.table_id == table_id,
        TableHealthSignal.signal.in_(("volume", "null_rate", "range")),
        TableHealthSignal.state.in_(_LEARNED_STATES),
        not_(and_(TableHealthSignal.state == "fired", TableHealthSignal.effect == "block")),
    ]
    if since is not None:
        conditions.append(TableHealthSignal.observed_at >= since)
    window = ranked(*conditions)
    rows = session.execute(
        select(TableHealthSignal.signal, TableHealthSignal.column_name, TableHealthSignal.observed)
        .join(window, window.c.id == TableHealthSignal.id)
        .where(window.c.position <= HISTORY)
        .order_by(TableHealthSignal.observed_at.desc(), TableHealthSignal.id.desc())
    )
    found: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    for signal, column, observed in rows:
        found[signal, column].append(observed or {})
    return dict(found)


def _previous_columns(before: TableMetadata | None) -> list[list[str]] | None:
    """Give the columns the table's current snapshot was written with, before the commit.

    Parameters
    ----------
    before
        The metadata before the commit; `None` for a table being created.

    Returns
    -------
    list or None
        ``[[name, type], ...]``; `None` for a table without a snapshot (nothing to compare).
    """
    if before is None or before.current_snapshot_id is None:
        return None
    snapshot = before.snapshot_by_id(before.current_snapshot_id)
    schema_id = None if snapshot is None else snapshot.schema_id
    schema = before.schema_by_id(schema_id) if schema_id is not None else None
    return columns_of(schema if schema is not None else before.schema())


def _read_added_files(
    snapshots: Sequence[Snapshot], metadata: TableMetadata, file_io: FileIO, table: str
) -> list[DataFile] | None:
    """Read the data files the snapshots of a commit add, within the health bounds.

    Parameters
    ----------
    snapshots
        Every snapshot the commit adds, in commit order.
    metadata
        The table's metadata before the commit, or after it for a new table.
    file_io
        Reads the table's storage.
    table
        The dotted name.

    Returns
    -------
    list of DataFile or None
        `None` when the manifests could not be read, were too many or took too long.
    """
    deadline = field_ids._clock() + HEALTH_DEADLINE_SECONDS
    try:
        return added_data_files(snapshots, metadata, file_io, table, deadline)
    except (ValidationError, StorageUnavailableError):
        return None


def _observe(
    session: Session,
    *,
    table: str,
    table_id: uuid.UUID | None,
    before: TableMetadata | None,
    after: TableMetadata,
    snapshot: Snapshot,
    snapshots: Sequence[Snapshot],
    file_io: FileIO,
    files: Sequence[DataFile] | None,
) -> list[Observation]:
    """Observe the commit signals of a new snapshot.

    The files are read before the history, so no manifest read waits on an open transaction
    that this function started.

    Parameters
    ----------
    session
        Database session, for the history.
    table
        The dotted name.
    table_id
        The table; `None` for a table being created.
    before
        The metadata before the commit; `None` for a table being created.
    after
        The metadata the commit makes current.
    snapshot
        Its current snapshot.
    snapshots
        Every snapshot the commit adds, in commit order.
    file_io
        Reads the table's storage.
    files
        The data files the commit adds when they were read already; `None` reads them.

    Returns
    -------
    list of Observation
        Volume, schema, then each column's null rate and range; only schema for a compaction
        (a ``replace`` snapshot rewrites rows without changing them).
    """
    found_schema = schema_signal(_previous_columns(before), columns_of(after.schema()))
    if snapshot.summary is not None and snapshot.summary.operation == Operation.REPLACE:
        return [found_schema]
    if files is None:
        files = _read_added_files(snapshots, before or after, file_io, table)
    history = {} if table_id is None else recent_observations(session, table_id)
    found = [volume_signal(snapshot.summary, history.get(("volume", None), [])), found_schema]
    if files is None:
        found += [
            Observation(signal, None, "no_metrics", {"reason": "unreadable"}, None)
            for signal in ("null_rate", "range")
        ]
        return found
    for column, metrics in (column_metrics(files, after.schema()) if files else {}).items():
        found.append(null_rate_signal(column, metrics, history.get(("null_rate", column), [])))
        ranged = range_signal(column, metrics, history.get(("range", column), []))
        if ranged is not None:
            found.append(ranged)
    return found


def commit_observations(
    session: Session,
    *,
    table: str,
    table_id: uuid.UUID | None,
    before: TableMetadata | None,
    after: TableMetadata,
    snapshots: Sequence[Snapshot],
    file_io: FileIO,
    effects: HealthEffects,
    files: Sequence[DataFile] | None = None,
) -> list[tuple[Observation, str]]:
    """Observe a new snapshot's commit signals and apply the table's effects.

    Parameters
    ----------
    session
        Database session; nothing is written.
    table
        The dotted name, for the log.
    table_id
        The table; `None` for a table being created, which has no history.
    before
        The metadata before the commit; `None` for a table being created.
    after
        The metadata the commit makes current.
    snapshots
        Every snapshot the commit adds, in commit order: a parent among them is not read as
        added.
    file_io
        Reads the table's storage.
    effects
        The table's effects.
    files
        The data files the commit adds, when the field-id check read them already; `None`
        reads them here.

    Returns
    -------
    list of tuple
        Each observation with its effect (`apply_effects`); none when `after` has no current
        snapshot. An error that is not the database's gives a ``no_metrics`` observation with
        reason ``error`` per commit signal.

    Raises
    ------
    sqlalchemy.exc.SQLAlchemyError
        If reading the history failed; the commit would fail anyway.
    """
    current = after.current_snapshot_id
    snapshot = None if current is None else after.snapshot_by_id(current)
    if snapshot is None:
        return []
    try:
        found = _observe(
            session,
            table=table,
            table_id=table_id,
            before=before,
            after=after,
            snapshot=snapshot,
            snapshots=snapshots,
            file_io=file_io,
            files=files,
        )
    except SQLAlchemyError:
        raise
    except Exception as exc:  # a signal must never fail a write
        logger.warning(
            "The health signals of a commit to %s could not be computed (%s).",
            shown(table),
            type(exc).__name__,
        )
        found = [
            Observation(signal, None, "no_metrics", {"reason": "error"}, None)
            for signal in COMMIT_SIGNALS
        ]
    return apply_effects(found, effects)


def signal_rows(
    pairs: Sequence[tuple[Observation, str]],
    *,
    environment_id: uuid.UUID,
    table_id: uuid.UUID,
    snapshot_id: int | None,
    now: datetime,
) -> list[TableHealthSignal]:
    """Build the rows of a commit's observations.

    Parameters
    ----------
    pairs
        From `commit_observations`.
    environment_id
        The table's environment.
    table_id
        The table.
    snapshot_id
        The new snapshot.
    now
        The database's time.

    Returns
    -------
    list of TableHealthSignal
        Not yet added to a session.
    """
    return [
        TableHealthSignal(
            id=uuid.uuid4(),
            environment_id=environment_id,
            table_id=table_id,
            signal=o.signal,
            column_name=o.column,
            snapshot_id=snapshot_id,
            observed=o.observed,
            band=o.band,
            state=o.state,
            effect=effect,
            observed_at=now,
        )
        for o, effect in pairs
    ]
