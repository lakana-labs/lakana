"""Merge: update the rows whose key matches, insert the rest, in one governed commit.

pyiceberg's `Transaction.upsert` decides what changes: it reads the target's rows whose key
the batch has into this process, compares them, rewrites the data files that hold the matched
rows and writes the updated and the new rows. It stages that as up to three snapshots, each
moving `main`, and the first of them is a table without the updated rows. `stage_upsert` folds
them into one snapshot on the target's current one, from the data files pyiceberg wrote, so
`CatalogService.merge` commits one snapshot through the governed compare-and-swap: a reader
sees the table before or after, never a half, and no in-between snapshot exists to travel back
to. No row is deleted.
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass

import pyarrow as pa
from pyiceberg.catalog.noop import NoopCatalog
from pyiceberg.expressions import And, In
from pyiceberg.io import FileIO
from pyiceberg.manifest import DataFile
from pyiceberg.table import DataScan, StaticTable, Transaction
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.snapshots import Snapshot
from pyiceberg.table.update import AddSnapshotUpdate, TableRequirement, TableUpdate
from pyiceberg.table.upsert_util import create_match_filter

from lakana_core.catalog.append import new_data_file_paths, new_manifest_paths
from lakana_core.errors import ValidationError

logger = logging.getLogger(__name__)

COMPOSITE_KEY_MAX_ROWS = 5_000
"""Most rows a merge on a key of several columns takes per batch.

pyiceberg's upsert matches such a key with one filter term per row, and its time grows with the
square of the batch: 5,000 rows take about ten seconds, 20,000 more than five minutes. It also
grows with the table: each scan applies that filter to every row that file pruning cannot skip,
about 1.5 seconds per million such rows for a full batch (5,000 rows into 5 million took about
12 seconds), so the cap bounds the batch, not the whole cost.
"""


@dataclass(frozen=True)
class MergeAdvice:
    """What a refused merge tells its caller to do: each kind of caller sends its rows its own way.

    Parameters
    ----------
    repeats
        A key that repeats in the rows; formatted with ``key`` (the columns named),
        ``rows`` (the count), ``subset`` (the columns as Python strings) and ``columns`` (the
        bare names).
    too_many
        A key of several columns with more rows than `COMPOSITE_KEY_MAX_ROWS`; formatted with
        ``cap`` and ``rows``, both with thousands separators.
    """

    repeats: str
    too_many: str


SDK_ADVICE = MergeAdvice(
    repeats=(
        "key {key} repeats in {rows} rows of this batch; make it unique "
        "(for example with `unique(subset=[{subset}], keep='last')`)"
    ),
    too_many=(
        "A merge on a key of several columns takes at most {cap} rows per batch, and this batch "
        "has {rows}. Merge on one key column (a column that holds the whole key), or send batches "
        "of at most {cap} rows."
    ),
)
"""For a caller of the SDK, the CLI or the REST API: it builds and sends the batches."""
SQL_ADVICE = MergeAdvice(
    repeats=(
        "key {key} repeats in {rows} rows of the asset's result; keep one row per key in its "
        "SELECT, for example with `QUALIFY ROW_NUMBER() OVER (PARTITION BY {columns} ORDER BY "
        "<the column that says which row is newest> DESC) = 1`"
    ),
    too_many=(
        "A merge on a key of several columns takes at most {cap} rows per run, and the asset's "
        "result has {rows}. Merge on one key column (a column that holds the whole key), or give "
        "the asset a `watermark:` so that each run merges only the rows changed since the last "
        "one; a first run reads every row, so load the history in parts of at most {cap} rows."
    ),
)
"""For a SQL asset: its SELECT is the batch, one per run."""


@dataclass(frozen=True)
class MergeCounts:
    """What a merge changed.

    Parameters
    ----------
    rows_inserted
        Rows whose key the table did not have.
    rows_updated
        Rows whose key matched and whose values changed.
    """

    rows_inserted: int
    rows_updated: int


@dataclass(frozen=True)
class StagedMerge:
    """A merge staged against a table, ready for `CatalogService.commit_table`.

    Parameters
    ----------
    requirements
        What must still hold at the commit.
    updates
        At most one `AddSnapshotUpdate` and the move of `main` to it; empty when nothing
        changed.
    counts
        The rows inserted and updated.
    discarded
        Files pyiceberg wrote for the snapshots that were folded into one: no snapshot of the
        commit names them, and they are deleted whether or not it lands.
    """

    requirements: Sequence[TableRequirement]
    updates: Sequence[TableUpdate]
    counts: MergeCounts
    discarded: Sequence[str] = ()


@dataclass(frozen=True)
class MergeResult:
    """What `CatalogService.merge` committed.

    Parameters
    ----------
    snapshot_id
        The new current snapshot; `None` when nothing changed.
    rows_inserted
        Rows whose key the table did not have.
    rows_updated
        Rows whose key matched and whose values changed.
    created
        Whether the merge created the table.
    table_id
        The table's id; `None` only where a caller built the result itself.
    """

    snapshot_id: int | None
    rows_inserted: int
    rows_updated: int
    created: bool = False
    table_id: uuid.UUID | None = None


def _shown(key: Sequence[str]) -> str:
    """Name key columns for a message.

    Parameters
    ----------
    key
        The key columns.

    Returns
    -------
    str
        Each in backticks, comma-separated.
    """
    return ", ".join(f"`{column}`" for column in key)


def _repeated_rows(data: pa.Table, key: Sequence[str]) -> int:
    """Count the rows whose key another row has too.

    Parameters
    ----------
    data
        The rows; carries every key column.
    key
        The key columns.

    Returns
    -------
    int
        The rows in groups of two or more.
    """
    if data.num_rows == 0:
        return 0
    counts = data.select(list(key)).group_by(list(key)).aggregate([([], "count_all")])
    return sum(count for count in counts.column("count_all").to_pylist() if count > 1)


def check_merge_batch(
    data: pa.Table,
    key: Sequence[str],
    *,
    table_columns: Collection[str],
    table_name: str,
    advice: MergeAdvice = SDK_ADVICE,
    composite_cap: bool = True,
) -> None:
    """Refuse a batch a merge cannot apply, naming counts and never values.

    Parameters
    ----------
    data
        The rows.
    key
        The key columns.
    table_columns
        The target's column names.
    table_name
        The target's dotted name, for the message.
    advice
        How the caller is told to fix a repeated key or too many rows.
    composite_cap
        Refuse a key of several columns with more than `COMPOSITE_KEY_MAX_ROWS` rows. A history
        write passes `False`: it joins such a key in pyarrow, with no term per row.

    Raises
    ------
    ValidationError
        For an empty key, a column named twice in it, a key column missing from the table or
        the data, a key column of a nested type, a key of several columns with more than
        `COMPOSITE_KEY_MAX_ROWS` rows (unless `composite_cap` is off), a null key, or a key that
        repeats within the batch.
    """
    if not key:
        raise ValidationError("A merge needs at least one key column.")
    for column in key:
        if list(key).count(column) > 1:
            raise ValidationError(f"key column `{column}` is named twice")
        if column not in table_columns:
            raise ValidationError(f"key column `{column}` is not a column of {table_name}")
        indices = data.schema.get_all_field_indices(column)
        if not indices:
            raise ValidationError(f"key column `{column}` is not a column of the rows sent")
        if len(indices) > 1:
            raise ValidationError(f"Column {column!r} appears more than once in the data.")
        kind = data.schema.field(indices[0]).type
        if pa.types.is_nested(kind):
            raise ValidationError(
                f"key column `{column}` has type {kind}; a key column must hold single values "
                "(a number, a string, a date or a timestamp)"
            )
    if composite_cap and len(key) > 1 and data.num_rows > COMPOSITE_KEY_MAX_ROWS:
        raise ValidationError(
            advice.too_many.format(cap=f"{COMPOSITE_KEY_MAX_ROWS:,}", rows=f"{data.num_rows:,}")
        )
    for column in key:
        nulls = data.column(column).null_count
        if nulls:
            raise ValidationError(f"key column `{column}` is null in {nulls} rows")
    rows = _repeated_rows(data, key)
    if rows:
        raise ValidationError(
            advice.repeats.format(
                key=_shown(key),
                rows=rows,
                subset=", ".join(repr(column) for column in key),
                columns=", ".join(key),
            )
        )


def _check_target_keys(
    metadata: TableMetadata,
    file_io: FileIO,
    data: pa.Table,
    key: Sequence[str],
    table_name: str,
) -> None:
    """Refuse a merge whose keys match more than one row of the table.

    pyiceberg would delete every matched row and write one, collapsing the repeats without a
    word (and it checks only within each batch it reads). Only the key columns of the matched
    rows are read.

    Parameters
    ----------
    metadata
        The target's current metadata.
    file_io
        The environment's `FileIO`.
    data
        The batch, checked by `check_merge_batch`.
    key
        The key columns.
    table_name
        The target's dotted name, for the message.

    Raises
    ------
    ValidationError
        If a key of the batch matches two or more rows of the table.
    """
    if metadata.current_snapshot_id is None:
        return
    columns = list(key)
    if len(columns) == 1:
        matched = DataScan(
            table_metadata=metadata,
            io=file_io,
            row_filter=create_match_filter(data, columns),
            selected_fields=tuple(columns),
        ).to_arrow()
    else:
        # One `In` per column selects a superset of the matched rows and the join keeps the
        # exact matches: pyiceberg's own filter for a key of several columns, one `And` per
        # row, takes time that grows with the square of the batch.
        candidates = DataScan(
            table_metadata=metadata,
            io=file_io,
            row_filter=functools.reduce(
                And,
                (In(term=column, literals=data[column].unique().to_pylist()) for column in columns),
            ),
            selected_fields=tuple(columns),
        ).to_arrow()
        wanted = data.select(columns).group_by(columns).aggregate([])
        matched = candidates.join(
            wanted.cast(candidates.select(columns).schema), keys=columns, join_type="left semi"
        )
    rows = _repeated_rows(matched, key)
    if rows:
        listed = ", ".join(key)
        # The SELECT is advice for the reader of the message; nothing runs it.
        raise ValidationError(
            f"key {_shown(key)} repeats in {rows} rows of {table_name} that this batch matches; "  # nosec B608
            "a merge needs one row per key. Find them with "
            f"`SELECT {listed}, count(*) FROM {table_name} GROUP BY {listed} "
            "HAVING count(*) > 1` and remove the repeats (for example by replacing the table) "
            "before you merge."
        )


def _live_files(metadata: TableMetadata, file_io: FileIO) -> dict[str, DataFile]:
    """List the data files of a table's current snapshot.

    Parameters
    ----------
    metadata
        The metadata.
    file_io
        To read the manifests.

    Returns
    -------
    dict
        Each live data file by its path.
    """
    snapshot = metadata.current_snapshot()
    if snapshot is None:
        return {}
    return {
        entry.data_file.file_path: entry.data_file
        for manifest in snapshot.manifests(file_io)
        for entry in manifest.fetch_manifest_entry(file_io, discard_deleted=True)
    }


def _static(metadata: TableMetadata, metadata_location: str, file_io: FileIO) -> StaticTable:
    """Wrap metadata in a `StaticTable` that cannot commit.

    Parameters
    ----------
    metadata
        The metadata.
    metadata_location
        Its location; an identifier here.
    file_io
        The environment's `FileIO`.

    Returns
    -------
    StaticTable
        The table.
    """
    return StaticTable(
        identifier=("lakana", metadata_location),
        metadata=metadata,
        metadata_location=metadata_location,
        io=file_io,
        catalog=NoopCatalog("lakana"),
    )


def stage_upsert(
    metadata: TableMetadata,
    metadata_location: str,
    file_io: FileIO,
    data: pa.Table,
    key: Sequence[str],
    *,
    table_name: str,
) -> StagedMerge:
    """Stage a merge as one snapshot, without a catalog behind it.

    pyiceberg's upsert runs first and stages up to three snapshots. When it staged more than
    one, the data files of its last snapshot are compared with the target's: one overwrite on
    the target's current snapshot deletes the files that went and adds the ones that came. The
    files of the folded snapshots (their manifests, manifest lists and any data file the last
    one does not keep) are returned as `discarded`. Reads `Transaction`'s private
    `_requirements`/`_updates`, as `build_transaction` and `stage_snapshot` do, because a
    `StaticTable`'s `NoopCatalog` refuses to commit.

    Parameters
    ----------
    metadata
        The target's current metadata.
    metadata_location
        Its location; an identifier here.
    file_io
        The environment's `FileIO`.
    data
        Rows `check_merge_batch` accepted, metadata stripped, schema validated.
    key
        The key columns.
    table_name
        The target's dotted name, for messages.

    Returns
    -------
    StagedMerge
        What to commit, the counts, and the files to delete afterwards.

    Raises
    ------
    ValidationError
        If a key of the batch matches two or more rows of the table.
    """
    _check_target_keys(metadata, file_io, data, key, table_name)
    tx = _static(metadata, metadata_location, file_io).transaction()
    try:
        return _upsert_folded(tx, metadata, metadata_location, file_io, data, key)
    except Exception:
        _delete_staged(tx, file_io)
        raise


def _delete_staged(tx: Transaction, file_io: FileIO) -> None:
    """Best-effort delete the files of the snapshots a failed staging left behind.

    Called while the staging's own error is being raised, so nothing here may replace it: each
    snapshot and each file is tried on its own, and a failure is logged by its class alone.

    Parameters
    ----------
    tx
        The transaction pyiceberg's upsert staged into.
    file_io
        The environment's `FileIO`.
    """
    paths: list[str] = []
    for update in tx._updates:
        if not isinstance(update, AddSnapshotUpdate):
            continue
        # Every snapshot is listed before any file is deleted: a later snapshot reads the
        # manifests an earlier one wrote.
        try:
            paths.extend(new_data_file_paths(update.snapshot, file_io))
            paths.extend(new_manifest_paths(update.snapshot, file_io))
        # Deliberately broad: the caller is already raising the real error.
        except Exception as exc:
            logger.warning(
                "Could not list the files of a failed merge's staged snapshot: %s",
                type(exc).__name__,
            )
            paths.append(update.snapshot.manifest_list)
    for path in dict.fromkeys(paths):
        try:
            file_io.delete(path)
        # Deliberately broad, as above.
        except Exception as exc:
            logger.warning(
                "Failed to delete a file while cleaning up a failed merge: %s",
                type(exc).__name__,
            )


def _upsert_folded(
    tx: Transaction,
    metadata: TableMetadata,
    metadata_location: str,
    file_io: FileIO,
    data: pa.Table,
    key: Sequence[str],
) -> StagedMerge:
    """Run pyiceberg's upsert and fold what it staged into one snapshot.

    Parameters
    ----------
    tx
        A transaction on the target's `StaticTable`.
    metadata
        The target's current metadata.
    metadata_location
        Its location.
    file_io
        The environment's `FileIO`.
    data
        The checked rows.
    key
        The key columns.

    Returns
    -------
    StagedMerge
        As `stage_upsert`.
    """
    result = tx.upsert(data, join_cols=list(key))
    counts = MergeCounts(rows_inserted=result.rows_inserted, rows_updated=result.rows_updated)
    staged: list[Snapshot] = [u.snapshot for u in tx._updates if isinstance(u, AddSnapshotUpdate)]
    if len(staged) <= 1:
        return StagedMerge(requirements=tx._requirements, updates=tx._updates, counts=counts)

    before, after = _live_files(metadata, file_io), _live_files(tx.table_metadata, file_io)
    folded = _static(metadata, metadata_location, file_io).transaction()
    with folded.update_snapshot().overwrite() as overwrite:
        for path in before.keys() - after.keys():
            overwrite.delete_data_file(before[path])
        for path in after.keys() - before.keys():
            overwrite.append_data_file(after[path])
    discarded: list[str] = []
    for snapshot in staged:
        discarded.extend(new_manifest_paths(snapshot, file_io))
        discarded.extend(p for p in new_data_file_paths(snapshot, file_io) if p not in after)
    return StagedMerge(
        requirements=folded._requirements,
        updates=folded._updates,
        counts=counts,
        discarded=discarded,
    )
