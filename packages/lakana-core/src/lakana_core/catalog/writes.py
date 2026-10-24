"""Data files and snapshot staging for governed table writes.

`CatalogService.write_batches` is the orchestration: authorization, schema
planning, the single commit through `commit_table`, the audit event and the
lineage edges. This module holds what needs no session: turning a stream of
Arrow record batches into Parquet data files under a table's own location,
one file at a time, and staging the one snapshot that publishes
them.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

import pyarrow as pa
from pyiceberg.catalog.noop import NoopCatalog
from pyiceberg.expressions import AlwaysTrue
from pyiceberg.io import FileIO
from pyiceberg.io.pyarrow import bin_pack_record_batches, schema_to_pyarrow, write_file
from pyiceberg.manifest import DataFile, DataFileContent
from pyiceberg.table import StaticTable, TableProperties, WriteTask
from pyiceberg.table.locations import load_location_provider
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.snapshots import Snapshot
from pyiceberg.table.update import AddSnapshotUpdate, TableRequirement, TableUpdate
from pyiceberg.utils.properties import property_as_int

from lakana_core.catalog.append import root_relative_path
from lakana_core.catalog.schema_compat import SchemaDiff
from lakana_core.errors import NotFoundError, UnsupportedTableError, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.storage.resolver import ResolvedStorage

logger = logging.getLogger(__name__)

DEFAULT_WRITE_BIN_BYTES = 128 * 1024 * 1024
"""In-memory Arrow bytes per data file when the table sets no target size.

pyiceberg's own default (512 MiB) is sized for a large distributed executor; a worker
holds one bin in memory next to DuckDB's own `memory_limit`, so a smaller
bin keeps a write's peak well inside a small deployment's memory."""


@dataclass(frozen=True)
class LineageSource:
    """A table a write read from, for its lineage edge.

    Parameters
    ----------
    table_id
        The source's id.
    name
        Its dotted name at write time.
    snapshot_id
        The source's current snapshot when the write resolved it, or `None`
        for a view, an external table, or a table with no snapshot.
    """

    table_id: uuid.UUID
    name: str
    snapshot_id: int | None


@dataclass(frozen=True)
class SnapshotPin:
    """The target snapshot a write's input was computed from.

    An incremental append's watermark, and the rows of a query that reads its
    own target, are only correct against the snapshot they were read from. A
    write carrying a pin commits only while the target is still that table at
    that snapshot, so a concurrent writer can neither be duplicated nor
    overwritten unread.

    Parameters
    ----------
    table_id
        The target's id when it was read; a dropped and re-created namesake
        does not match.
    snapshot_id
        The target's current snapshot when it was read, or `None` if it had
        none yet.
    """

    table_id: uuid.UUID
    snapshot_id: int | None


def max_rows_message(max_rows: int) -> str:
    """Say that a write has more rows than `QueryLimitsConfig.max_write_rows`.

    Parameters
    ----------
    max_rows
        The limit.

    Returns
    -------
    str
        The message, naming the setting and that nothing was committed.
    """
    return f"The write exceeds max_write_rows ({max_rows}); nothing was committed."


ExpectedSnapshot = int | Literal["none"]
"""What a writer says the target's current snapshot is: an id, or ``none`` for "no snapshot"."""

_SNAPSHOT_ID = re.compile(r"\A(?:0|[1-9][0-9]{0,18})\Z")
_MAX_SNAPSHOT_ID = 2**63 - 1

_MAX_SOURCE_VERSION = 2**31 - 1
MAX_SOURCE_NAME = 1024 + 1 + 255
"""Longest legal dotted name: `Namespace.path` (1024) + a dot + `Table.name` (255).

No narrower limit exists in the code; a longer name cannot name any table."""
MAX_SOURCE_VERSIONS = 64
"""Most `source_version` values one write may carry.

A task's declared inputs are a handful; the bound keeps the lookups one request
can cause small, whatever a hand-made client sends."""
_SOURCE_MESSAGE = (
    "A source must be a table or artifact name as 'namespace.name', for example "
    "'gold.sales'; this one is longer than any name can be."
)
_SOURCE_VERSION_MESSAGE = (
    "source_version must be '<version>:<namespace.name>', for example '3:models.churn_model'."
)
_SOURCE_VERSION_NUMBER_HINT = (
    " The version is a whole number from 1 to 2147483647, written without leading zeros."
)


def check_sources(values: Sequence[str]) -> None:
    """Bound the `source` request parameters of an Arrow write.

    Each source costs an authorization, a lookup and, for an Iceberg table, a metadata read, so
    one request may name at most `MAX_SOURCE_VERSIONS` of them, the bound `source_version` has.
    A name longer than `MAX_SOURCE_NAME` cannot name any table and is refused without
    being echoed. Whether a name exists is for `CatalogService.resolve_lineage_sources`.

    Parameters
    ----------
    values
        The sources as the request gave them; untrusted.

    Raises
    ------
    ValidationError
        If there are more than `MAX_SOURCE_VERSIONS` values, or one is not a string of at most
        `MAX_SOURCE_NAME` characters.
    """
    if len(values) > MAX_SOURCE_VERSIONS:
        raise ValidationError(
            f"source may be given at most {MAX_SOURCE_VERSIONS} times; "
            "name only the tables and artifacts the write read."
        )
    for value in values:
        if not isinstance(value, str) or len(value) > MAX_SOURCE_NAME:
            raise ValidationError(_SOURCE_MESSAGE)


def parse_source_versions(values: Sequence[str]) -> dict[str, int]:
    """Parse the `source_version` request parameters of an Arrow write.

    The values come from a request and are untrusted. The version is plain
    ASCII digits (`int()` and `str.isdigit()` would also accept signs, spaces,
    underscores and other scripts' digits), without a leading zero, between 1
    and 2**31 - 1, and is checked for length before it is converted, so a value
    thousands of digits long is refused without reaching `int()`. The name has
    at most `MAX_SOURCE_NAME` characters and no control, format, surrogate,
    unassigned, private-use, space or separator character, so nothing
    downstream can echo an unprintable or oversized name. It must parse
    as a dotted table name and is keyed as `TableRef.dotted` writes it, the
    form `CatalogService.resolve_lineage_sources` looks names up by.

    Parameters
    ----------
    values
        Each ``<version>:<dotted name>``.

    Returns
    -------
    dict
        Dotted name to version.

    Raises
    ------
    ValidationError
        If a value is not a string, a version, a colon and a dotted name; the
        version is zero, has a leading zero or exceeds 2**31 - 1 (the message
        then adds what a version is); a name is given twice,
        or there are more than `MAX_SOURCE_VERSIONS` values.
    """
    if len(values) > MAX_SOURCE_VERSIONS:
        raise ValidationError(
            f"source_version may be given at most {MAX_SOURCE_VERSIONS} times; "
            "name only the artifacts the task read."
        )
    versions: dict[str, int] = {}
    for value in values:
        # The length is checked first: nothing below may cost more than a legal name does.
        if not isinstance(value, str) or len(value) > MAX_SOURCE_NAME + 11:
            raise ValidationError(_SOURCE_VERSION_MESSAGE)
        head, colon, raw_name = value.partition(":")
        if not colon or not (head.isascii() and head.isdigit()):
            raise ValidationError(_SOURCE_VERSION_MESSAGE)
        if (
            len(raw_name) > MAX_SOURCE_NAME
            or not raw_name
            or any(unicodedata.category(char)[0] in "CZ" for char in raw_name)
        ):
            raise ValidationError(_SOURCE_VERSION_MESSAGE)
        if head.startswith("0") or len(head) > 10 or int(head) > _MAX_SOURCE_VERSION:
            raise ValidationError(_SOURCE_VERSION_MESSAGE + _SOURCE_VERSION_NUMBER_HINT)
        try:
            name = TableRef.from_dotted("-", raw_name).dotted()
        except ValidationError:
            raise ValidationError(_SOURCE_VERSION_MESSAGE) from None
        if name in versions:
            raise ValidationError(_SOURCE_VERSION_MESSAGE)
        versions[name] = int(head)
    return versions


def parse_expected_snapshot(value: str | None) -> ExpectedSnapshot | None:
    """Parse the `expected_snapshot_id` request parameter.

    Parameters
    ----------
    value
        The raw parameter, or `None` when it was not sent.

    Returns
    -------
    int or "none" or None
        The snapshot id, the literal ``none``, or `None` for "no precondition".

    Raises
    ------
    ValidationError
        Unless `value` is ``none`` or a canonical decimal id (no leading
        zeros) that fits a signed 64-bit integer.
    """
    if value is None:
        return None
    if value == "none":
        return "none"
    if _SNAPSHOT_ID.match(value) is None or int(value) > _MAX_SNAPSHOT_ID:
        raise ValidationError("expected_snapshot_id must be a snapshot id or 'none'.")
    return int(value)


def expected_snapshot_value(expected: ExpectedSnapshot) -> int | None:
    """Turn an expectation into the snapshot id Iceberg's requirement compares.

    Parameters
    ----------
    expected
        An id or ``none``.

    Returns
    -------
    int or None
        The id, or `None` for ``none``.
    """
    return None if expected == "none" else expected


def target_changed_message(dotted: str) -> str:
    """Say that a write's target changed under it.

    Parameters
    ----------
    dotted
        The target's dotted name.

    Returns
    -------
    str
        The message a conflicting write fails with.
    """
    return f"{dotted} changed during the write; retry."


@dataclass(frozen=True)
class WriteResult:
    """What one governed write did.

    Parameters
    ----------
    table_id
        The target's id.
    rows
        Rows written.
    files
        Data files written.
    snapshot_id
        The snapshot committed, or `None` when nothing was committed (an
        append of zero rows, a create of zero rows).
    schema_diff
        How the target's schema changed; empty when it didn't.
    """

    table_id: uuid.UUID
    rows: int
    files: int
    snapshot_id: int | None
    schema_diff: SchemaDiff


class DataFileWriter:
    """Write a stream of record batches as Parquet data files under a table's location.

    One bin (`write.target-file-size-bytes`, else `DEFAULT_WRITE_BIN_BYTES`)
    is held in memory and written as one file before the next is read, so
    a 100-million-row result never sits in memory at once. Every data file
    path is recorded in `attempted_paths` *before* it is written, so a
    caller can delete a partial file after a failure.

    Parameters
    ----------
    metadata
        The target's metadata, already carrying the schema the files are
        written with (after any schema change this write makes).
    file_io
        The environment's resolved `FileIO`.
    max_rows
        Most rows to accept, or `None` for no bound (an Arrow upload,
        already bounded by its byte caps).
    """

    def __init__(self, metadata: TableMetadata, file_io: FileIO, *, max_rows: int | None) -> None:
        self._metadata = metadata
        self._file_io = file_io
        self._max_rows = max_rows
        self.rows = 0
        self.attempted_paths: list[str] = []

    def write(self, batches: Iterable[pa.RecordBatch]) -> list[DataFile]:
        """Write every batch and return the data files written.

        Parameters
        ----------
        batches
            The rows, each batch holding at least every column of the
            metadata's current schema.

        Returns
        -------
        list of DataFile
            One per file written, in order.

        Raises
        ------
        ValidationError
            If the table is partitioned or doesn't write Parquet; if the
            stream exceeds `max_rows`; or if a column the table requires to
            be non-null holds a null. Raised mid-stream for the last two,
            after earlier files were written — see `attempted_paths`.
        """
        if not self._metadata.spec().is_unpartitioned():
            raise ValidationError("Writes into a partitioned table are not supported yet.")
        file_format = self._metadata.properties.get(
            TableProperties.WRITE_FILE_FORMAT, TableProperties.WRITE_FILE_FORMAT_DEFAULT
        )
        if file_format.lower() != "parquet":
            raise ValidationError(f"Writes need a Parquet table; this one writes {file_format}.")
        schema = self._metadata.schema()
        target = schema_to_pyarrow(schema)
        bin_bytes = (
            property_as_int(self._metadata.properties, TableProperties.WRITE_TARGET_FILE_SIZE_BYTES)
            or DEFAULT_WRITE_BIN_BYTES
        )
        provider = load_location_provider(
            table_location=self._metadata.location, table_properties=self._metadata.properties
        )
        write_uuid = uuid.uuid4()
        data_files: list[DataFile] = []
        for task_id, bin_batches in enumerate(
            bin_pack_record_batches(self._conformed(batches, target), bin_bytes)
        ):
            task = WriteTask(
                write_uuid=write_uuid, task_id=task_id, record_batches=bin_batches, schema=schema
            )
            self.attempted_paths.append(
                provider.new_data_location(task.generate_data_file_filename("parquet"))
            )
            data_files.extend(write_file(self._file_io, self._metadata, iter([task])))
        return data_files

    def _conformed(
        self, batches: Iterable[pa.RecordBatch], target: pa.Schema
    ) -> Iterator[pa.RecordBatch]:
        """Count, check and cast each batch to the table's Arrow schema.

        Parameters
        ----------
        batches
            The incoming batches.
        target
            `schema_to_pyarrow` of the table schema, carrying field ids.

        Yields
        ------
        pa.RecordBatch
            Each non-empty batch, reordered to the table's column order and
            cast. A batch with zero rows is counted (so `max_rows` still
            sees it) but never yielded, so an empty batch never becomes an
            empty Parquet file (a zero-row write commits no data
            file).

        Raises
        ------
        ValidationError
            If `max_rows` is exceeded, or a required column holds a null.
        """
        required = [field.name for field in target if not field.nullable]
        for batch in batches:
            self.rows += batch.num_rows
            if batch.num_rows == 0:
                continue
            if self._max_rows is not None and self.rows > self._max_rows:
                raise ValidationError(max_rows_message(self._max_rows))
            for name in required:
                if batch.column(name).null_count:
                    raise ValidationError(
                        f"Column {name!r} contains a null value, but the table requires it to "
                        "be non-null."
                    )
            yield batch.select(target.names).cast(target)


def live_data_files(metadata: TableMetadata, file_io: FileIO) -> list[DataFile]:
    """List the live data files of a table's current snapshot, as manifest entries carry them.

    The `DataFile` objects come from the manifests, not from a writer: the
    overwrite producer reads each file's `spec_id`, which only a
    manifest-read `DataFile` has.

    Parameters
    ----------
    metadata
        The table's metadata.
    file_io
        Used to read the manifests.

    Returns
    -------
    list of DataFile
        Empty for a table with no snapshot.

    Raises
    ------
    UnsupportedTableError
        If the current snapshot has a live row-level delete file.
    """
    snapshot = metadata.current_snapshot()
    if snapshot is None:
        return []
    files: list[DataFile] = []
    for manifest in snapshot.manifests(file_io):
        for entry in manifest.fetch_manifest_entry(file_io, discard_deleted=True):
            if entry.data_file.content != DataFileContent.DATA:
                raise UnsupportedTableError(
                    "The target has row-level delete files, which Lakana can't replace yet."
                )
            files.append(entry.data_file)
    return files


def stage_snapshot(
    metadata: TableMetadata,
    metadata_location: str,
    file_io: FileIO,
    data_files: Sequence[DataFile],
    *,
    replace: bool,
) -> tuple[Sequence[TableRequirement], Sequence[TableUpdate], Snapshot]:
    """Stage the one snapshot that publishes a write, without a catalog behind it.

    Reads `Transaction`'s private `_requirements`/`_updates`, as
    `lakana_core.catalog.append.build_transaction` already does, because a
    `StaticTable`'s `NoopCatalog` refuses to commit; `commit_table` replays
    them through the governed compare-and-swap.

    Parameters
    ----------
    metadata
        The metadata the snapshot builds on (after any schema change).
    metadata_location
        The current metadata file's location; only an identifier here.
    file_io
        Used to write the manifests and manifest list.
    data_files
        The new data files.
    replace
        `True` for one `OVERWRITE` snapshot that deletes every live data
        file and adds `data_files`; `False` for a fast append.

    Returns
    -------
    tuple
        `(requirements, updates, snapshot)` — the staged snapshot is the
        one inside the `AddSnapshotUpdate`.
    """
    static = StaticTable(
        identifier=("lakana", metadata_location),
        metadata=metadata,
        metadata_location=metadata_location,
        io=file_io,
        catalog=NoopCatalog("lakana"),
    )
    tx = static.transaction()
    if replace:
        live = live_data_files(metadata, file_io)
        with tx.update_snapshot().overwrite() as overwrite:
            overwrite.delete_by_predicate(AlwaysTrue())
            for data_file in live:
                overwrite.delete_data_file(data_file)
            for data_file in data_files:
                overwrite.append_data_file(data_file)
    else:
        with tx.update_snapshot().fast_append() as fast_append:
            for data_file in data_files:
                fast_append.append_data_file(data_file)
    requirements, updates = tx._requirements, tx._updates
    snapshot = next(u.snapshot for u in updates if isinstance(u, AddSnapshotUpdate))
    return requirements, updates, snapshot


def cleanup_location(resolved: ResolvedStorage, location: str) -> bool:
    """Best-effort delete everything under a location nothing references yet.

    Only for a brand-new table's id-suffixed location after a failed
    create: nothing else can live there. Every failure is logged by its
    exception class alone and swallowed, so it never replaces the caller's
    real error. Only objects strictly below ``location/`` are deleted: a
    listing that also names a sibling sharing the text as a prefix leaves it.

    Parameters
    ----------
    resolved
        The environment's resolved storage.
    location
        The absolute location to empty.

    Returns
    -------
    bool
        Whether the location is known to be empty now: `False` when it is not under the
        storage root, the listing failed or a file could not be deleted.
    """
    relative = root_relative_path(resolved.config.root, location)
    if relative is None:
        return False
    try:
        paths = list(resolved.storage.list(relative))
    except Exception as exc:
        logger.warning("Failed to list a failed create's files: %s", type(exc).__name__)
        return False
    emptied = True
    for path in paths:
        if not path.startswith(relative.rstrip("/") + "/"):
            continue
        try:
            resolved.storage.delete(path)
        except NotFoundError:
            continue  # gone already
        except Exception as exc:
            emptied = False
            logger.warning("Failed to delete a failed create's file: %s", type(exc).__name__)
    return emptied
