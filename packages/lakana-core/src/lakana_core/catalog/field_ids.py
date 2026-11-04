"""The field-id map of an Iceberg read.

Lakana reads an Iceberg table's data files by field id, as the Iceberg specification requires:
DuckDB's `read_parquet(..., schema = MAP {field_id: {name, type, default_value}})` projects every
file onto the schema the read uses, so a renamed or re-added column reads its own data, a
widened one reads in its new type and a column a file lacks reads as NULL. The map covers
top-level columns. DuckDB matches the fields inside a struct, list or map by name, so a read
whose nested fields were renamed, moved or replaced since some of its files were written is
refused instead (`refuse_changed_nested_fields`).

A commit through the catalog that adds a data file without field ids is refused
(`check_added_files_have_field_ids`): such a file would read as NULL in every column.
"""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import TypeVar, cast

import pyarrow as pa
import pyarrow.parquet as pq
from pyiceberg.avro.file import AvroFile
from pyiceberg.io import FileIO, InputFile, InputStream
from pyiceberg.manifest import (
    DEFAULT_READ_VERSION,
    MANIFEST_ENTRY_SCHEMAS,
    MANIFEST_LIST_FILE_SCHEMAS,
    DataFile,
    DataFileContent,
    FileFormat,
    ManifestContent,
    ManifestEntry,
    ManifestEntryStatus,
    ManifestFile,
    PartitionFieldSummary,
)
from pyiceberg.schema import Schema, index_name_by_id
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.snapshots import Snapshot
from pyiceberg.typedef import StructProtocol
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    FixedType,
    FloatType,
    IcebergType,
    IntegerType,
    ListType,
    LongType,
    MapType,
    NestedField,
    StringType,
    StructType,
    TimestampType,
    TimestamptzType,
    TimeType,
    UUIDType,
)

from lakana_core.errors import StorageUnavailableError, UnsupportedTableError, ValidationError
from lakana_core.identifiers import shown, shown_path
from lakana_core.query_rewrite import ReadField
from lakana_core.storage.resolver import is_within_location

logger = logging.getLogger(__name__)

_Record = TypeVar("_Record", bound=StructProtocol)

_PRIMITIVES: dict[type[IcebergType], str] = {
    BooleanType: "BOOLEAN",
    IntegerType: "INTEGER",
    LongType: "BIGINT",
    FloatType: "FLOAT",
    DoubleType: "DOUBLE",
    DateType: "DATE",
    TimeType: "TIME",
    TimestampType: "TIMESTAMP",
    TimestamptzType: "TIMESTAMP WITH TIME ZONE",
    StringType: "VARCHAR",
    UUIDType: "UUID",
    BinaryType: "BLOB",
}
"""The DuckDB type of each Iceberg primitive. The nanosecond timestamps, `unknown` and the
geospatial types of format version 3 have none here: a table with one is refused."""


def _quoted(name: str) -> str:
    """Quote a field name for a DuckDB type text.

    Parameters
    ----------
    name
        The field's name, which another Iceberg client may have chosen freely.

    Returns
    -------
    str
        The name in double quotes, its own double quotes doubled.
    """
    return '"' + name.replace('"', '""') + '"'


def duckdb_type_of(field_type: IcebergType) -> str | None:
    """Spell an Iceberg type as DuckDB's type parser reads it.

    Parameters
    ----------
    field_type
        The type of a column or of a field inside one.

    Returns
    -------
    str or None
        The DuckDB type (`BIGINT`, `DECIMAL(10, 2)`, `STRUCT("a" INTEGER)`, `BIGINT[]`,
        `MAP(VARCHAR, DOUBLE)`), or `None` when it is, or holds, a type DuckDB has none for.
    """
    if isinstance(field_type, DecimalType):
        return f"DECIMAL({field_type.precision}, {field_type.scale})"
    if isinstance(field_type, FixedType):
        return "BLOB"
    if isinstance(field_type, StructType):
        parts = []
        for nested in field_type.fields:
            inner = duckdb_type_of(nested.field_type)
            if inner is None:
                return None
            parts.append(f"{_quoted(nested.name)} {inner}")
        return f"STRUCT({', '.join(parts)})"
    if isinstance(field_type, ListType):
        element = duckdb_type_of(field_type.element_type)
        return None if element is None else f"{element}[]"
    if isinstance(field_type, MapType):
        key = duckdb_type_of(field_type.key_type)
        value = duckdb_type_of(field_type.value_type)
        return None if key is None or value is None else f"MAP({key}, {value})"
    return _PRIMITIVES.get(type(field_type))


def read_fields(dotted: str, fields: Sequence[NestedField]) -> tuple[ReadField, ...]:
    """Build the field-id map of a read from the columns it reads.

    Parameters
    ----------
    dotted
        The table's dotted name, as the query names it.
    fields
        The columns the read returns, in order, from the schema the read uses.

    Returns
    -------
    tuple of ReadField
        One entry per column.

    Raises
    ------
    UnsupportedTableError
        If a column has a type DuckDB has none for. Leaving the column out would hide it
        without a word, so the read is refused, naming the column and its type. Also if two
        columns differ only in letter case, which DuckDB cannot read under a map.
    """
    entries = []
    first: dict[str, str] = {}
    for column in fields:
        earlier = first.setdefault(column.name.lower(), column.name)
        if earlier != column.name:
            raise UnsupportedTableError(
                f"{dotted!r} has the columns {earlier!r} and {column.name!r}, which differ only "
                "in letter case, and Lakana compares column names without regard to case. Ask "
                "the owner of the table to rename one of them."
            )
        duckdb_type = duckdb_type_of(column.field_type)
        if duckdb_type is None:
            raise UnsupportedTableError(
                f"{dotted!r} has the column {column.name!r} of type {column.field_type}, which "
                "Lakana cannot read yet. Ask the owner of the table to store the column as "
                "another type."
            )
        entries.append(ReadField(column.field_id, column.name, duckdb_type))
    return tuple(entries)


def _nested_layout(fields: Sequence[NestedField]) -> dict[int, tuple[int, str]]:
    """Place every field inside a nested column by its column's id and its path in the column.

    Parameters
    ----------
    fields
        Top-level columns.

    Returns
    -------
    dict
        Field id of each nested field to the id of its top-level column and its dotted path
        inside that column (`city`, `element`, `key`). A top-level rename leaves it unchanged.
    """
    layout: dict[int, tuple[int, str]] = {}
    for column in fields:
        for field_id, name in index_name_by_id(Schema(column)).items():
            if field_id != column.field_id:
                layout[field_id] = (column.field_id, name.split(".", 1)[1])
    return layout


def _schemas_of_live_files(metadata: TableMetadata, snapshot_id: int) -> list[Schema]:
    """List the schemas the data files of a snapshot may have been written with.

    The snapshot and its ancestors back to the first one that added all of its files
    (its summary's `added-data-files` equals `total-data-files`: an overwrite or the first
    write). Where an ancestor is missing or names no schema, every schema the table has had.

    Parameters
    ----------
    metadata
        The table's metadata.
    snapshot_id
        The snapshot the read reads.

    Returns
    -------
    list of Schema
        The schemas, without repeats.
    """
    by_id = {schema.schema_id: schema for schema in metadata.schemas}
    found: dict[int, None] = {}
    snapshot = metadata.snapshot_by_id(snapshot_id)
    while True:
        if snapshot is None or snapshot.schema_id is None or snapshot.schema_id not in by_id:
            return list(by_id.values())
        found[snapshot.schema_id] = None
        summary = snapshot.summary
        if summary is not None:
            added = summary.get("added-data-files")
            if added is not None and added == summary.get("total-data-files"):
                break
        if snapshot.parent_snapshot_id is None:
            break
        snapshot = metadata.snapshot_by_id(snapshot.parent_snapshot_id)
    return [by_id[schema_id] for schema_id in found]


def refuse_changed_nested_fields(
    dotted: str, metadata: TableMetadata, snapshot_id: int, fields: Sequence[NestedField]
) -> None:
    """Refuse a read whose nested fields changed since some of its data files were written.

    Parameters
    ----------
    dotted
        The table's dotted name, as the query names it.
    metadata
        The table's metadata.
    snapshot_id
        The snapshot the read reads.
    fields
        The columns the read returns, from the schema the read uses.

    Raises
    ------
    UnsupportedTableError
        If a field inside a struct, list or map column has another place in a schema the
        snapshot's files may have been written with, or its place another field. The message
        names the table, the column and the field, and no file.
    """
    now = _nested_layout(fields)
    if not now:
        return
    by_place = {place: field_id for field_id, place in now.items()}
    names = {column.field_id: column.name for column in fields}
    for schema in _schemas_of_live_files(metadata, snapshot_id):
        for field_id, place in _nested_layout(schema.fields).items():
            moved = field_id in now and now[field_id] != place
            replaced = place in by_place and by_place[place] != field_id
            if moved or replaced:
                column_id, path = now[field_id] if moved else place
                raise UnsupportedTableError(
                    f"{dotted!r} cannot be read: the field `{path}` of its column "
                    f"`{names[column_id]}` was renamed, moved, or dropped and added again after "
                    "some of its data files were written, and Lakana reads the fields inside a "
                    "nested column by name. Ask the owner of the table to write all of its rows "
                    "again (an overwrite), so that every data file has the new layout."
                )


_FIELD_ID = b"PARQUET:field_id"
"""The Arrow field metadata key under which Parquet carries an Iceberg field id."""

_FOOTER_READERS = 8
"""How many data-file footers one commit reads at the same time."""

MAX_CHECKED_FILES = 2_000
"""The most added data files one commit may have; more are refused, not left unchecked."""

CHECK_DEADLINE_SECONDS = 60.0
"""How long all footer reads of one commit may take together before the commit is refused."""

MAX_FOOTER_BYTES = 16 * 1024 * 1024
"""The largest Parquet footer the check reads; the trailer names its size before it is read."""

MAX_MANIFEST_BYTES = 64 * 1024 * 1024
"""The largest manifest or manifest list the check reads, the parent's included; pyiceberg
reads such a file whole, so the check reads it itself, through a stream that stops at this many
bytes plus one, and hands pyiceberg those bytes."""

_READ_CHUNK = 1024 * 1024
"""How much of a manifest one read asks for."""

_clock = time.monotonic
"""The clock the deadline runs on; a test replaces it instead of waiting."""

_NO_FIELD_IDS = (
    "The commit adds `{file}` to `{table}`, which has no Iceberg field ids. Lakana reads Iceberg "
    "data by field id, as the Iceberg specification requires. Write the file with a client that "
    "sets field ids."
)
_UNREADABLE_FILE = (
    "The commit adds `{file}` to `{table}`, which could not be read as a Parquet file, so "
    "nothing was applied. Write the data file before the commit, as Parquet with Iceberg field "
    "ids."
)
_NOT_PARQUET = (
    "The commit adds `{file}` to `{table}` as {format}, so nothing was applied. Lakana reads "
    "Parquet data files only; write the file as Parquet."
)
_UNREADABLE_MANIFEST = (
    "A manifest of the commit's snapshot for `{table}` could not be read, so nothing was "
    "applied. Write the manifests and the manifest list before the commit."
)
_MANIFEST_TOO_LARGE = (
    "The commit's snapshot for `{table}` names `{file}`, a manifest file "
    "larger than the 64 MiB Lakana reads, so nothing was applied. Split the "
    "commit into smaller ones, so that each manifest lists fewer data files."
)
_TOO_MANY_FILES = (
    "The commit adds more than {limit:,} data files to `{table}`; Lakana checks at most "
    "{limit:,} per commit. Split the load into smaller commits."
)
_TIMED_OUT = (
    "Checking the data files the commit adds to `{table}` for Iceberg field ids took longer "
    "than {seconds:g} seconds, so nothing was applied. Split the commit into smaller ones."
)
_FOOTER_TOO_LARGE = (
    "The commit adds `{file}` to `{table}`, whose Parquet footer is larger than 16 MiB, so "
    "nothing was applied. Write the file with fewer columns and row groups."
)
_STORAGE_UNAVAILABLE = (
    "Storage could not be read right now, so the data files the commit adds to `{table}` were "
    "not checked and nothing was applied. Try the commit again."
)


class _FooterTooLarge(Exception):
    """A Parquet trailer names a footer above `MAX_FOOTER_BYTES`."""


class _NotParquet(Exception):
    """A file is not a readable Parquet file: no magic bytes, or a footer that does not parse."""


def has_field_ids(file_io: FileIO, path: str) -> bool:
    """Tell whether every top-level column of a Parquet file carries an Iceberg field id.

    Parameters
    ----------
    file_io
        Reads the table's storage.
    path
        The data file's URI.

    Returns
    -------
    bool
        Whether each column of the footer's schema has ``PARQUET:field_id`` metadata. Only
        the trailer and the footer are read.

    Raises
    ------
    _NotParquet
        If the file does not end in the Parquet magic bytes, or its footer cannot be parsed.
    _FooterTooLarge
        If the trailer names a footer above `MAX_FOOTER_BYTES`.
    """
    with file_io.new_input(path).open() as stream:
        stream.seek(-8, 2)
        trailer = stream.read(8)
        if len(trailer) != 8 or trailer[4:] != b"PAR1":
            raise _NotParquet
        if int.from_bytes(trailer[:4], "little") > MAX_FOOTER_BYTES:
            raise _FooterTooLarge
        stream.seek(0)
        try:
            schema = pq.read_schema(stream)
        except Exception as exc:  # the file exists and ends in the magic: its footer is bad
            raise _NotParquet from exc
    return all(field.metadata is not None and _FIELD_ID in field.metadata for field in schema)


def _is_unavailable(exc: Exception) -> bool:
    """Tell whether a read failure is storage being unavailable rather than the file being wrong.

    Parameters
    ----------
    exc
        What a storage read raised.

    Returns
    -------
    bool
        Whether it is an `OSError` that is not a missing file and not a malformed one.
    """
    return (
        isinstance(exc, OSError)
        and not isinstance(exc, FileNotFoundError)
        and not isinstance(exc, pa.ArrowInvalid)
    )


def _footer(file_io: FileIO, path: str, table: str) -> str:
    """Read one footer for the commit check, without letting its error text out.

    Parameters
    ----------
    file_io
        Reads the table's storage.
    path
        The data file's URI.
    table
        The table's dotted name, for the log.

    Returns
    -------
    str
        `"ok"`, `"no_ids"`, `"unreadable"` (missing, not Parquet or malformed), `"too_large"`
        or `"unavailable"` (storage failed); the log has the exception's class and the
        table, never its text, which can name storage details.
    """
    try:
        return "ok" if has_field_ids(file_io, path) else "no_ids"
    except _FooterTooLarge:
        return "too_large"
    except _NotParquet:
        return "unreadable"
    except Exception as exc:  # any reader failure refuses the commit with a fixed text
        logger.warning(
            "A data file of a commit to %s could not be read (%s).", table, type(exc).__name__
        )
        return "unavailable" if _is_unavailable(exc) else "unreadable"


def _timed_out(table: str) -> str:
    """Word the refusal for a check that outlasted its deadline.

    Parameters
    ----------
    table
        The table's dotted name.

    Returns
    -------
    str
        The refusal's text.
    """
    return _TIMED_OUT.format(table=shown(table), seconds=CHECK_DEADLINE_SECONDS)


class _HeldFile(InputFile):
    """A file whose bytes the check has already read, in the form pyiceberg opens."""

    def __init__(self, location: str, data: bytes) -> None:
        super().__init__(location)
        self._data = data

    def __len__(self) -> int:
        """Report the length of the bytes held.

        Returns
        -------
        int
            The number of bytes.
        """
        return len(self._data)

    def exists(self) -> bool:
        """Say that the file exists: its bytes are here.

        Returns
        -------
        bool
            Always `True`.
        """
        return True

    def open(self, seekable: bool = True) -> InputStream:
        """Open the bytes held.

        Parameters
        ----------
        seekable
            Ignored; the stream always seeks.

        Returns
        -------
        InputStream
            A stream over the bytes.
        """
        return cast(InputStream, io.BytesIO(self._data))


def _read_bounded(input_file: InputFile) -> bytes | None:
    """Read a file through a stream that stops one byte past `MAX_MANIFEST_BYTES`.

    Parameters
    ----------
    input_file
        The file in storage.

    Returns
    -------
    bytes or None
        The content, or `None` when it has more than `MAX_MANIFEST_BYTES` bytes (at most one
        block past the limit was read).
    """
    chunks: list[bytes] = []
    total = 0
    with input_file.open(seekable=False) as stream:
        while total <= MAX_MANIFEST_BYTES:
            chunk = stream.read(min(_READ_CHUNK, MAX_MANIFEST_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    return None if total > MAX_MANIFEST_BYTES else b"".join(chunks)


def _open_avro(
    file_io: FileIO,
    path: str,
    table: str,
    deadline: float,
    make: Callable[[InputFile], AvroFile[_Record]],
) -> AvroFile[_Record]:
    """Open a manifest or a manifest list: read it within the size limit and the deadline.

    pyiceberg reads such a file whole when it is opened, and decodes it entry by entry
    afterwards. The file is therefore read here, through a stream that stops at
    `MAX_MANIFEST_BYTES` plus one byte (a size asked for in a separate request would let a
    client that can write under the table's location swap the file in between), and pyiceberg
    opens those bytes. The read runs in its own thread and is abandoned, like a footer read,
    when the deadline passes.

    Parameters
    ----------
    file_io
        Reads the table's storage.
    path
        The file's URI.
    table
        The table's dotted name, for the message.
    deadline
        When the check's time is up, on the clock `_clock` reads.
    make
        Opens the file as a pyiceberg `AvroFile`.

    Returns
    -------
    AvroFile
        The opened file, whose bytes are in memory; the caller closes it.

    Raises
    ------
    ValidationError
        If the file is larger than `MAX_MANIFEST_BYTES`, or the deadline passes before it was
        read.
    """
    remaining = deadline - _clock()
    if remaining <= 0:
        raise ValidationError(_timed_out(table))

    def read() -> AvroFile[_Record]:
        data = _read_bounded(file_io.new_input(path))
        if data is None:
            raise ValidationError(
                _MANIFEST_TOO_LARGE.format(table=shown(table), file=shown_path(path))
            )
        return make(_HeldFile(path, data))

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(read).result(timeout=remaining)
    except FutureTimeout:
        raise ValidationError(_timed_out(table)) from None
    finally:
        # A read that is stuck is not waited for: the commit is refused now.
        pool.shutdown(wait=False, cancel_futures=True)


def _listed_manifests(
    file_io: FileIO, manifest_list: str, table: str, deadline: float
) -> Iterator[ManifestFile]:
    """List the manifests of a manifest list, checking the deadline before each.

    Parameters
    ----------
    file_io
        Reads the table's storage.
    manifest_list
        The manifest list's URI.
    table
        The table's dotted name, for the message.
    deadline
        When the check's time is up, on the clock `_clock` reads.

    Yields
    ------
    ManifestFile
        Each manifest of the list, in order. The list file is read whole (at most
        `MAX_MANIFEST_BYTES`, within the deadline) and decoded as it is consumed.

    Raises
    ------
    ValidationError
        If the list is too large, or the deadline has passed before it was read or before a
        manifest is yielded.
    """
    reader = _open_avro(
        file_io,
        manifest_list,
        table,
        deadline,
        lambda input_file: AvroFile[ManifestFile](
            input_file,
            MANIFEST_LIST_FILE_SCHEMAS[DEFAULT_READ_VERSION],
            read_types={-1: ManifestFile, 508: PartitionFieldSummary},
            read_enums={517: ManifestContent},
        ).__enter__(),
    )
    try:
        for manifest in reader:
            if _clock() >= deadline:
                raise ValidationError(_timed_out(table))
            yield manifest
    finally:
        reader.__exit__(None, None, None)


def _live_entries(
    file_io: FileIO, manifest: ManifestFile, table: str, deadline: float
) -> Iterator[ManifestEntry]:
    """List the live entries of a manifest, checking the deadline before each.

    Parameters
    ----------
    file_io
        Reads the table's storage.
    manifest
        The manifest to read.
    table
        The table's dotted name, for the message.
    deadline
        When the check's time is up, on the clock `_clock` reads.

    Yields
    ------
    ManifestEntry
        Each entry that is not `DELETED`, in order. The manifest file is read whole (at most
        `MAX_MANIFEST_BYTES`, within the deadline) and decoded as it is consumed.

    Raises
    ------
    ValidationError
        If the manifest is too large, or the deadline has passed before it was read or before
        an entry is yielded.
    """
    reader = _open_avro(
        file_io,
        manifest.manifest_path,
        table,
        deadline,
        lambda input_file: AvroFile[ManifestEntry](
            input_file,
            MANIFEST_ENTRY_SCHEMAS[DEFAULT_READ_VERSION],
            read_types={-1: ManifestEntry, 2: DataFile},
            read_enums={0: ManifestEntryStatus, 101: FileFormat, 134: DataFileContent},
        ).__enter__(),
    )
    try:
        for entry in reader:
            if _clock() >= deadline:
                raise ValidationError(_timed_out(table))
            if entry.status != ManifestEntryStatus.DELETED:
                yield entry
    finally:
        reader.__exit__(None, None, None)


def added_data_files(
    snapshots: Sequence[Snapshot],
    metadata: TableMetadata,
    file_io: FileIO,
    table: str,
    deadline: float,
    skip: Collection[int] = frozenset(),
) -> list[DataFile]:
    """List the data files the snapshots of one commit add, from manifests their parents lack.

    A parent is looked up among the snapshots the table had before the commit and the ones
    that come earlier in this commit. A parent that is the snapshot itself, comes later or is
    unknown counts as none, so all of that snapshot's manifests are read.

    Parameters
    ----------
    snapshots
        The snapshots a commit adds, in commit order; each manifest list lies under the
        table's location.
    metadata
        The table's metadata before the commit.
    file_io
        Reads the table's storage.
    table
        The table's dotted name, for the message.
    deadline
        When the check's time is up, on the clock `_clock` reads.
    skip
        Ids of snapshots whose files are left out (a compaction's, for the health signals);
        they still count as parents of the snapshots after them.

    Returns
    -------
    list of DataFile
        The live entries of status `ADDED` and content data, in manifest order, each file
        once. A manifest outside the table's location is not opened: a read refuses such a
        table anyway. A manifest is read whatever content its list entry claims, as a read
        does; only an entry's own content decides what is a data file.

    Raises
    ------
    ValidationError
        If the manifest list or a manifest cannot be read, there are more than
        `MAX_CHECKED_FILES` files (the rest are not read), or the deadline passes.
    StorageUnavailableError
        If storage failed while reading them.
    """
    earlier: dict[int, Snapshot] = {}
    seen_manifests: set[str] = set()
    seen_files: set[str] = set()
    added: list[DataFile] = []
    for snapshot in snapshots:
        parent_id = snapshot.parent_snapshot_id
        parent = (
            None
            if parent_id is None
            else earlier.get(parent_id) or metadata.snapshot_by_id(parent_id)
        )
        earlier[snapshot.snapshot_id] = snapshot
        if snapshot.snapshot_id in skip:
            continue
        try:
            known: set[str] = set()
            if parent is not None:
                for listed in _listed_manifests(file_io, parent.manifest_list, table, deadline):
                    known.add(listed.manifest_path)
            for manifest in _listed_manifests(file_io, snapshot.manifest_list, table, deadline):
                if (
                    manifest.manifest_path in known
                    or manifest.manifest_path in seen_manifests
                    or not is_within_location(metadata.location, manifest.manifest_path)
                ):
                    continue
                seen_manifests.add(manifest.manifest_path)
                for entry in _live_entries(file_io, manifest, table, deadline):
                    if (
                        entry.status == ManifestEntryStatus.ADDED
                        and entry.data_file.content == DataFileContent.DATA
                        and entry.data_file.file_path not in seen_files
                    ):
                        seen_files.add(entry.data_file.file_path)
                        added.append(entry.data_file)
                        if len(added) > MAX_CHECKED_FILES:
                            raise ValidationError(
                                _TOO_MANY_FILES.format(table=shown(table), limit=MAX_CHECKED_FILES)
                            )
        except ValidationError:
            raise
        except Exception as exc:  # any reader failure refuses the commit with a fixed text
            logger.warning(
                "A manifest of a commit to %s could not be read (%s).", table, type(exc).__name__
            )
            if _is_unavailable(exc):
                raise StorageUnavailableError(
                    _STORAGE_UNAVAILABLE.format(table=shown(table))
                ) from None
            raise ValidationError(_UNREADABLE_MANIFEST.format(table=shown(table))) from exc
    return added


def check_added_files_have_field_ids(
    snapshots: Sequence[Snapshot], metadata: TableMetadata, file_io: FileIO, table: str
) -> list[DataFile]:
    """Refuse snapshots that add a data file without Iceberg field ids.

    Only files the commit's own snapshots add are read, each footer once, at most
    `_FOOTER_READERS` at a time, within `CHECK_DEADLINE_SECONDS` in all, and only under the
    table's location: a file elsewhere, or one whose path holds a glob character, is not
    opened, and a read refuses the table for it. At most `MAX_CHECKED_FILES` files are
    checked per commit; a commit with more is refused. Files committed before this check
    existed are not looked at, nor are files a parent snapshot already lists. An entry a
    client marks `EXISTING` in a new manifest is not looked at either; such a file reads as
    NULL, which hides nothing a policy protects.

    Parameters
    ----------
    snapshots
        The snapshots the commit adds, in commit order.
    metadata
        The table's metadata before the commit.
    file_io
        Reads the table's storage.
    table
        The table's dotted name, for the message.

    Returns
    -------
    list of DataFile
        The data files the snapshots add (`added_data_files`), for the commit's health
        signals to reuse instead of reading the manifests again.

    Raises
    ------
    ValidationError
        For the first added file, in manifest order, that is not Parquet; else for the first
        one that cannot be read, has a footer above `MAX_FOOTER_BYTES` or lacks field ids; or
        if a manifest cannot be read, there are more than `MAX_CHECKED_FILES` files, or the
        reads outlast `CHECK_DEADLINE_SECONDS`. The message names that file and the table,
        nothing else.
    StorageUnavailableError
        If storage failed while reading a footer or a manifest.
    """
    deadline = _clock() + CHECK_DEADLINE_SECONDS
    files = added_data_files(snapshots, metadata, file_io, table, deadline)
    for data_file in files:
        if data_file.file_format != FileFormat.PARQUET:
            raise ValidationError(
                _NOT_PARQUET.format(
                    file=shown_path(data_file.file_path),
                    table=shown(table),
                    format=data_file.file_format.value,
                )
            )
    paths = [f.file_path for f in files if is_within_location(metadata.location, f.file_path)]
    pool = ThreadPoolExecutor(max_workers=_FOOTER_READERS)
    try:
        futures = [pool.submit(_footer, file_io, path, table) for path in paths]
        for path, future in zip(paths, futures, strict=True):
            remaining = deadline - _clock()
            try:
                outcome = future.result(timeout=max(remaining, 0.0)) if remaining > 0 else None
            except FutureTimeout:
                outcome = None
            if outcome is None:
                raise ValidationError(_timed_out(table))
            if outcome == "unavailable":
                raise StorageUnavailableError(_STORAGE_UNAVAILABLE.format(table=shown(table)))
            if outcome == "unreadable":
                raise ValidationError(
                    _UNREADABLE_FILE.format(file=shown_path(path), table=shown(table))
                )
            if outcome == "too_large":
                raise ValidationError(
                    _FOOTER_TOO_LARGE.format(file=shown_path(path), table=shown(table))
                )
            if outcome == "no_ids":
                raise ValidationError(
                    _NO_FIELD_IDS.format(file=shown_path(path), table=shown(table))
                )
    finally:
        # A read that is stuck is not waited for: the commit is refused now.
        pool.shutdown(wait=False, cancel_futures=True)
    return files
