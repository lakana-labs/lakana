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

import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from pyiceberg.io import FileIO
from pyiceberg.manifest import DataFile
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.snapshots import Operation, Snapshot
from sqlalchemy import ColumnElement, Subquery, and_, delete, func, not_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.elements import UnaryExpression

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
from lakana_core.db import db_now
from lakana_core.errors import StorageUnavailableError, ValidationError
from lakana_core.identifiers import shown
from lakana_core.models import TableHealthSignal
from lakana_core.secrets.keys import KeyProvider

logger = logging.getLogger(__name__)

_MAX_BIGINT = 2**63 - 1
"""The largest number a `BigInteger` column holds."""

HEALTH_DEADLINE_SECONDS = 5.0
"""How long one commit may spend reading the manifests of its new snapshots for the metrics."""

_LEARNED_STATES = ("ok", "fired", "no_history")
"""The states whose observed values a band is learned from."""

_BY_COLUMN: tuple[InstrumentedAttribute[Any], ...] = (
    TableHealthSignal.signal,
    TableHealthSignal.column_name,
)

NOT_SET_BACK: ColumnElement[bool] = TableHealthSignal.set_back_at.is_(None)
"""The rows that are still the table's data: a set-back (`forget_set_back`) marks the others,
which only the history shows."""

NEWEST_COMMIT: tuple[UnaryExpression[Any], ...] = (
    TableHealthSignal.sequence_number.desc(),
    TableHealthSignal.observed_at.desc(),
    TableHealthSignal.id.desc(),
)
"""Newest first, for rows with a ``sequence_number``: the Iceberg sequence number orders a
table's commits, which ``observed_at`` cannot (two commits can share a time, on SQLite within
the same second); ``observed_at`` breaks the tie of a format v1 table, whose numbers are all 0.
Served by the index on ``(table_id, signal, sequence_number, observed_at, id)``."""


def ranked(
    *conditions: ColumnElement[bool],
    partition: Sequence[InstrumentedAttribute[Any]] = _BY_COLUMN,
) -> Subquery:
    """Rank the health signal rows newest first within each partition.

    The one window over `table_health_signals` that the baseline and the last firings share:
    rows are ordered as `NEWEST_COMMIT` says, a row without a sequence number last, so position
    1 is the newest row and ties break the same way everywhere.

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
                order_by=(
                    TableHealthSignal.sequence_number.desc().nulls_last(),
                    *NEWEST_COMMIT[1:],
                ),
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

    Only rows observed since the (2 x `HISTORY`)-th newest ``schema`` row are ranked, so a
    table's whole retention is never ranked on a commit. Every commit records one ``schema``
    row, but compactions and commits whose signals failed record no other: on a table compacted
    more often than appended to, the window can hold fewer than `HISTORY` values of a signal
    (fewer than `MIN_HISTORY` leaves it at ``no_history``). The rows it misses are the oldest.

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
        state ``ok``, ``fired`` or ``no_history``. A row whose snapshot was set back is left out,
        and does not count towards the window either; so is a fired row whose effect was
        ``block``, whose data is set back unless the set-back failed.
    """
    since = session.scalar(
        select(TableHealthSignal.observed_at)
        .where(
            TableHealthSignal.table_id == table_id,
            TableHealthSignal.signal == "schema",
            TableHealthSignal.sequence_number.is_not(None),
            NOT_SET_BACK,
        )
        .order_by(*NEWEST_COMMIT)
        .offset(2 * HISTORY - 1)
        .limit(1)
    )
    conditions = [
        TableHealthSignal.table_id == table_id,
        TableHealthSignal.signal.in_(("volume", "null_rate", "range")),
        TableHealthSignal.state.in_(_LEARNED_STATES),
        not_(and_(TableHealthSignal.state == "fired", TableHealthSignal.effect == "block")),
        NOT_SET_BACK,
    ]
    if since is not None:
        conditions.append(TableHealthSignal.observed_at >= since)
    window = ranked(*conditions)
    rows = session.execute(
        select(TableHealthSignal.signal, TableHealthSignal.column_name, TableHealthSignal.observed)
        .join(window, window.c.id == TableHealthSignal.id)
        .where(window.c.position <= HISTORY)
        .order_by(window.c.position)
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


def _is_compaction(snapshot: Snapshot) -> bool:
    """Say whether a snapshot is a compaction, which rewrites rows without changing them.

    Parameters
    ----------
    snapshot
        The snapshot.

    Returns
    -------
    bool
        Whether its summary's operation is ``replace``.
    """
    return snapshot.summary is not None and snapshot.summary.operation == Operation.REPLACE


def _read_added_files(
    snapshots: Sequence[Snapshot],
    metadata: TableMetadata,
    file_io: FileIO,
    table: str,
    skip: Collection[int],
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
    skip
        The compactions among `snapshots`, whose files are left out.

    Returns
    -------
    list of DataFile or None
        `None` when the manifests could not be read, were too many or took too long.
    """
    deadline = field_ids._clock() + HEALTH_DEADLINE_SECONDS
    try:
        return added_data_files(snapshots, metadata, file_io, table, deadline, skip)
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
        Its current snapshot, standing for the commit's snapshots when `snapshots` is empty.
    snapshots
        Every snapshot the commit adds, in commit order.
    file_io
        Reads the table's storage.
    files
        The data files the commit adds when they were read already; `None` reads them.

    Returns
    -------
    list of Observation
        Volume, schema, then each column's null rate and range. A compaction (a ``replace``
        snapshot rewrites rows without changing them) is decided per snapshot: its files and
        counts are left out, and a commit of compactions only records the schema. Volume is the
        newest other snapshot's count.
    """
    found_schema = schema_signal(_previous_columns(before), columns_of(after.schema()))
    added = list(snapshots) or [snapshot]
    compactions = {s.snapshot_id for s in added if _is_compaction(s)}
    counted = [s for s in added if s.snapshot_id not in compactions]
    if not counted:
        return [found_schema]
    if files is None or compactions:
        # The field-id check's files include the compactions'; they are read again without.
        files = _read_added_files(added, before or after, file_io, table, compactions)
    history = {} if table_id is None else recent_observations(session, table_id)
    newest = counted[-1].summary
    found = [
        volume_signal(newest, history.get(("volume", None), []), no_data_files=files == []),
        found_schema,
    ]
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
    sequence_number: int | None,
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
    sequence_number
        Its sequence number.
    now
        The database's time.

    Returns
    -------
    list of TableHealthSignal
        Not yet added to a session; none when the snapshot id or sequence number does not fit
        a 64-bit column, logged. An observation whose values JSON cannot store (an infinity,
        say, which Postgres `json` refuses at the flush and so fails the commit) is kept as
        ``no_metrics`` without values.
    """
    if any(n is not None and not 0 <= n <= _MAX_BIGINT for n in (snapshot_id, sequence_number)):
        # The commit refuses such numbers (`CatalogService.commit_table`); should one get past,
        # its rows could not be stored and would fail the commit, so it records none.
        logger.warning(
            "The health signals of a commit were not recorded: its snapshot id or sequence "
            "number is out of range for the database."
        )
        return []
    rows = []
    for o, effect in pairs:
        stored = _storable(o.observed, o.band)
        rows.append(
            TableHealthSignal(
                id=uuid.uuid4(),
                environment_id=environment_id,
                table_id=table_id,
                signal=o.signal,
                column_name=o.column,
                snapshot_id=snapshot_id,
                sequence_number=sequence_number,
                observed=o.observed if stored else None,
                band=o.band if stored else None,
                state=o.state if stored else "no_metrics",
                effect=effect,
                observed_at=now,
            )
        )
    return rows


def _storable(*values: object) -> bool:
    """Say whether strict JSON can hold the values.

    Parameters
    ----------
    *values
        An observation's observed values and band.

    Returns
    -------
    bool
        `False` for an infinity, a NaN or anything JSON has no type for.
    """
    try:
        json.dumps(values, allow_nan=False)
    except (ValueError, TypeError):
        return False
    return True


def own_chain(metadata: TableMetadata, after: int | None, until: int | None) -> list[int]:
    """List the snapshots from a tip back to, not including, the one before them.

    The chain a task run wrote: what a set-back takes out of ``main`` (`forget_set_back`), and
    what a blocking health check judges (`lakana_worker.task_checks._health_result`).

    The walk also stops at the first snapshot whose sequence number is no newer than that of
    `until`: when another writer set ``main`` back between two of the run's commits, the later
    commit's parents are older than the run, and their signals are not the run's. A format v1
    table numbers every snapshot 0, and an `until` the metadata no longer holds has no number;
    then only `until` itself stops the walk.

    Parameters
    ----------
    metadata
        The table's metadata, holding the tip.
    after
        The tip; `None` gives nothing.
    until
        The snapshot before the chain, which is not part of it; `None` walks every ancestor.

    Returns
    -------
    list of int
        The tip first, then each parent; empty when the tip is not in the metadata.
    """
    chain: list[int] = []
    before = None if until is None else metadata.snapshot_by_id(until)
    floor = before.sequence_number if before is not None and before.sequence_number else None
    snapshot = None if after is None else metadata.snapshot_by_id(after)
    while (
        snapshot is not None
        and snapshot.snapshot_id != until
        and snapshot.snapshot_id not in chain
        and (floor is None or (snapshot.sequence_number or 0) > floor)
    ):
        chain.append(snapshot.snapshot_id)
        parent = snapshot.parent_snapshot_id
        snapshot = None if parent is None else metadata.snapshot_by_id(parent)
    return chain


def forget_set_back(
    session: Session,
    table_id: uuid.UUID,
    metadata: TableMetadata,
    *,
    after: int | None,
    until: int | None,
) -> None:
    """Mark the signals of the snapshots a set-back takes out of ``main``, uncommitted.

    A check that sets a table back (`CatalogService.roll_back`) makes them no longer the table's
    data: they must neither show as its current state nor feed a later baseline (`NOT_SET_BACK`).
    The history keeps them, marked with the time: a fired one is why a blocking health check
    set the table back, and a notification names it. Retention deletes them like any row.

    A first build that is left not published (`until` is `None`) has its rows deleted instead:
    its data is refused to every reader until a build publishes it, so its numbers are not kept.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    metadata
        The table's metadata before the set-back.
    after
        The snapshot that is set back; `None` sets nothing back.
    until
        The snapshot ``main`` goes back to, whose rows stay; `None` when ``main`` is removed and
        every ancestor of `after` goes.
    """
    gone = own_chain(metadata, after, until)
    rows = (TableHealthSignal.table_id == table_id, TableHealthSignal.snapshot_id.in_(gone))
    if until is None:
        statement = delete(TableHealthSignal).where(*rows)
    else:
        statement = (
            update(TableHealthSignal).where(*rows, NOT_SET_BACK).values(set_back_at=db_now(session))
        )
    session.execute(statement.execution_options(synchronize_session=False))


def current_columns(
    session: Session, key_provider: KeyProvider | None, environment: str, table: str
) -> list[str] | None:
    """Read the names of a managed table's columns, in its current schema's order.

    A deploy counts the columns a commit judges (`MAX_HEALTH_COLUMNS`) in this order for a
    write that keeps the table's schema. Names only: no rows, no files, no statistics.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Decrypts the storage secret.
    environment
        The environment's name.
    table
        The dotted table name.

    Returns
    -------
    list of str or None
        The names; `None` for a table that does not exist, is not a managed table, or has no
        metadata yet.

    Raises
    ------
    Exception
        Whatever reading the storage raises; the caller decides.
    """
    from lakana_core.catalog.identity import resolve_table_id
    from lakana_core.catalog.metadata import read_metadata
    from lakana_core.models import Table, TableType
    from lakana_core.storage.resolver import StorageResolver

    table_id = resolve_table_id(session, environment, *table.split("."))
    row = None if table_id is None else session.get(Table, table_id)
    if row is None or row.table_type is not TableType.ICEBERG or not row.metadata_location:
        return None
    resolved = StorageResolver(session, key_provider).for_environment(environment)
    metadata = read_metadata(resolved.file_io, row.metadata_location)
    return [field.name for field in metadata.schema().fields]
