"""A commit through the catalog that adds a data file without Iceberg field ids is refused."""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import _config
from pyiceberg.avro.file import AvroFile
from pyiceberg.io import FileIO, InputFile, OutputFile
from pyiceberg.io.pyarrow import PyArrowFileIO
from pyiceberg.manifest import (
    DataFile,
    DataFileContent,
    FileFormat,
    ManifestContent,
    ManifestEntry,
    ManifestEntryStatus,
    ManifestFile,
    write_manifest,
    write_manifest_list,
)
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.snapshots import Operation, Snapshot, Summary
from pyiceberg.table.update import AddSnapshotUpdate, SetSnapshotRefUpdate
from pyiceberg.typedef import Record
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.catalog import field_ids as field_ids_module
from lakana_core.catalog import health_record
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.errors import ConflictError, StorageUnavailableError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef, shown_path
from lakana_core.models import Table, TableHealthSignal, WriteMode

_SCHEMA = Schema(
    NestedField(1, "id", LongType(), required=False),
    NestedField(2, "name", StringType(), required=False),
)


def _created(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> tuple[TableRef, LoadedTable]:
    """Create `dev` and an empty `gold.sales`.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    tuple of TableRef and LoadedTable
        The table.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    return ref, catalog.create_table(principal, ref, schema=_SCHEMA)


def _parquet(table: LoadedTable, name: str, *, field_ids: bool) -> str:
    """Write a one-row Parquet file under the table's data directory.

    Parameters
    ----------
    table
        The table whose location the file goes under.
    name
        The file's name.
    field_ids
        Whether its columns carry Iceberg field ids.

    Returns
    -------
    str
        The file's URI.
    """

    def column(column_name: str, arrow_type: pa.DataType, field_id: int) -> pa.Field:
        metadata = {b"PARQUET:field_id": str(field_id).encode()} if field_ids else None
        return pa.field(column_name, arrow_type, metadata=metadata)

    uri = f"{table.metadata.location}/data/{name}"
    path = Path(uri.removeprefix("file://"))
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([column("id", pa.int64(), 1), column("name", pa.string(), 2)])
    pq.write_table(pa.table({"id": [1], "name": ["x"]}, schema=schema), path)
    return uri


def _data_file(uri: str, file_format: FileFormat = FileFormat.PARQUET) -> DataFile:
    """Describe a written file as a manifest entry's data file.

    Parameters
    ----------
    uri
        The file's URI.
    file_format
        The format the entry claims.

    Returns
    -------
    DataFile
        An unpartitioned data file without column statistics.
    """
    path = Path(uri.removeprefix("file://"))
    return DataFile.from_args(
        content=DataFileContent.DATA,
        file_path=uri,
        file_format=file_format,
        partition=Record(),
        record_count=1,
        file_size_in_bytes=path.stat().st_size if path.exists() else 1,
        column_sizes={},
        value_counts={},
        null_value_counts={},
        nan_value_counts={},
        lower_bounds={},
        upper_bounds={},
        key_metadata=None,
        split_offsets=None,
        equality_ids=None,
        sort_order_id=None,
    )


def _snapshot(
    metadata: TableMetadata,
    parent: Snapshot | None,
    uris: list[str],
    snapshot_id: int,
    *,
    manifest_content: ManifestContent = ManifestContent.DATA,
    file_format: FileFormat = FileFormat.PARQUET,
    missing_manifest: bool = False,
) -> Snapshot:
    """Write a snapshot that adds `uris` on top of `parent`, as an Iceberg client does.

    Parameters
    ----------
    metadata
        The table's metadata, for its location, spec and schema.
    parent
        The snapshot to build on, or `None` for the first one.
    uris
        The data files to add, in manifest order.
    snapshot_id
        The new snapshot's id.
    manifest_content
        What the manifest list claims the new manifest holds.
    file_format
        The format the entries claim.
    missing_manifest
        Whether the manifest list names a manifest that was never written.

    Returns
    -------
    Snapshot
        The snapshot; its manifest list is written.
    """
    io = PyArrowFileIO()
    sequence = 1 if parent is None else (parent.sequence_number or 0) + 1
    manifest_path = f"{metadata.location}/metadata/{snapshot_id}-m0.avro"
    with write_manifest(
        format_version=metadata.format_version,
        spec=metadata.spec(),
        schema=metadata.schema(),
        output_file=io.new_output(manifest_path if not missing_manifest else manifest_path + ".w"),
        snapshot_id=snapshot_id,
        avro_compression="null",
    ) as writer:
        for uri in uris:
            writer.add(
                ManifestEntry.from_args(
                    status=ManifestEntryStatus.ADDED,
                    snapshot_id=snapshot_id,
                    sequence_number=sequence,
                    file_sequence_number=sequence,
                    data_file=_data_file(uri, file_format),
                )
            )
    new_manifest = writer.to_manifest_file()
    new_manifest[3] = manifest_content
    if missing_manifest:
        new_manifest[0] = manifest_path
    manifests = [*([] if parent is None else parent.manifests(io)), new_manifest]
    list_path = f"{metadata.location}/metadata/snap-{snapshot_id}.avro"
    with write_manifest_list(
        format_version=metadata.format_version,
        output_file=io.new_output(list_path),
        snapshot_id=snapshot_id,
        parent_snapshot_id=None if parent is None else parent.snapshot_id,
        sequence_number=sequence,
        avro_compression="null",
    ) as list_writer:
        list_writer.add_manifests(manifests)
    return Snapshot(
        snapshot_id=snapshot_id,
        parent_snapshot_id=None if parent is None else parent.snapshot_id,
        sequence_number=sequence,
        timestamp_ms=int(time.time() * 1000),
        manifest_list=list_path,
        summary=Summary(operation=Operation.APPEND),
        schema_id=metadata.current_schema_id,
    )


def _commit_snapshots(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    snapshots: list[Snapshot],
    *,
    own_data_files: bool = False,
    release_session: bool = True,
) -> LoadedTable:
    """Commit snapshots, the last one becoming `main`.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    ref
        The table.
    snapshots
        The snapshots to add, in order.
    own_data_files
        Whether to skip the field-id check, as Lakana's own writes do.
    release_session
        Whether to ask for the database session to be released during the check, as the REST
        route does.

    Returns
    -------
    LoadedTable
        The table after the commit.
    """
    return catalog.commit_table(
        principal,
        ref,
        requirements=[],
        updates=[
            *(AddSnapshotUpdate(snapshot=snapshot) for snapshot in snapshots),
            SetSnapshotRefUpdate(
                ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=snapshots[-1].snapshot_id
            ),
        ],
        own_data_files=own_data_files,
        release_session=release_session,
    )


def _commit_files(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    table: LoadedTable,
    uris: list[str],
    snapshot_id: int,
    *,
    manifest_content: ManifestContent = ManifestContent.DATA,
    file_format: FileFormat = FileFormat.PARQUET,
    missing_manifest: bool = False,
) -> LoadedTable:
    """Commit a snapshot that adds `uris` on top of the current one.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    ref
        The table.
    table
        The table as it is now.
    uris
        The data files to add, in manifest order.
    snapshot_id
        The new snapshot's id.
    manifest_content
        What the manifest list claims the new manifest holds.
    file_format
        The format the entries claim.
    missing_manifest
        Whether the manifest list names a manifest that was never written.

    Returns
    -------
    LoadedTable
        The table after the commit.
    """
    snapshot = _snapshot(
        table.metadata,
        table.metadata.current_snapshot(),
        uris,
        snapshot_id,
        manifest_content=manifest_content,
        file_format=file_format,
        missing_manifest=missing_manifest,
    )
    return _commit_snapshots(catalog, principal, ref, [snapshot])


def _no_field_ids(uri: str) -> str:
    """Write out the refusal of a file without field ids.

    Parameters
    ----------
    uri
        The file; it appears as messages show it, escaped and, when long, cut in the middle, which a
        long temporary directory (macOS's) reaches.

    Returns
    -------
    str
        The message.
    """
    return (
        f"The commit adds `{shown_path(uri)}` to `gold.sales`, which has no Iceberg field ids. "
        "Lakana reads Iceberg data by field id, as the Iceberg specification requires. Write "
        "the file with a client that sets field ids."
    )


def test_a_commit_that_adds_a_file_without_field_ids_is_refused_whole(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Pyiceberg's `add_files` registers files without field ids; nothing is applied."""
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "plain.parquet", field_ids=False)
    transaction = StaticTable.from_metadata(created.metadata_location).transaction()
    transaction.add_files([uri])

    with pytest.raises(ValidationError) as caught:
        catalog.commit_table(
            principal, ref, requirements=transaction._requirements, updates=transaction._updates
        )

    assert str(caught.value) == _no_field_ids(uri)
    assert catalog.load_table(principal, ref).metadata_location == created.metadata_location


def test_a_long_storage_root_keeps_the_file_name_in_the_refusal(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """With a 300-character root the message stays short and still names the file."""
    root = tmp_path.joinpath(*["r" * 100] * 3)
    ref, created = _created(catalog, principal, root)
    uri = _parquet(created, "plain-file-name.parquet", field_ids=False)
    assert len(uri) > 300
    transaction = StaticTable.from_metadata(created.metadata_location).transaction()
    transaction.add_files([uri])

    with pytest.raises(ValidationError) as caught:
        catalog.commit_table(
            principal, ref, requirements=transaction._requirements, updates=transaction._updates
        )

    message = str(caught.value)
    assert "…/plain-file-name.parquet`" in message
    assert uri not in message
    assert len(message) < 600


def test_a_commit_that_adds_files_with_field_ids_is_accepted(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uris = [_parquet(created, f"f{i}.parquet", field_ids=True) for i in range(3)]

    committed = _commit_files(catalog, principal, ref, created, uris, snapshot_id=900_000_001)

    assert committed.metadata.current_snapshot_id == 900_000_001


def test_lakanas_own_writes_read_no_footer(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pyiceberg writes field ids for Lakana's own appends and replaces; nothing is read again."""
    ref, _created_table = _created(catalog, principal, tmp_path)

    def refuse(*_args: object) -> bool:
        raise AssertionError("a footer was read for a write Lakana made itself")

    monkeypatch.setattr(field_ids_module, "has_field_ids", refuse)
    rows = pa.table({"id": pa.array([1], pa.int64()), "name": ["x"]})

    catalog.append(principal, ref, rows)
    catalog.write_batches(
        principal, ref, mode=WriteMode.REPLACE, schema=rows.schema, batches=rows.to_batches()
    )

    assert catalog.load_table(principal, ref).metadata.current_snapshot_id is not None


def test_a_commit_of_many_files_names_the_first_without_field_ids_and_reads_each_footer_once(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two hundred files on top of a parent: the parent's file is never read, none twice."""
    ref, created = _created(catalog, principal, tmp_path)
    parent_uri = _parquet(created, "parent.parquet", field_ids=True)
    parent = _commit_files(catalog, principal, ref, created, [parent_uri], snapshot_id=900_000_010)
    uris = [_parquet(created, f"f{i:03}.parquet", field_ids=True) for i in range(200)]
    uris[137] = _parquet(created, "f137-plain.parquet", field_ids=False)
    uris[150] = _parquet(created, "f150-plain.parquet", field_ids=False)
    reads: Counter[str] = Counter()
    real = field_ids_module.has_field_ids

    def counting(file_io: object, path: str) -> bool:
        reads[path] += 1
        return real(file_io, path)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(field_ids_module, "has_field_ids", counting)

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, parent, uris, snapshot_id=900_000_011)

    assert str(caught.value) == _no_field_ids(uris[137])
    assert reads[parent_uri] == 0
    assert max(reads.values()) == 1
    assert reads[uris[137]] == 1

    reads.clear()
    good = [_parquet(created, f"g{i:03}.parquet", field_ids=True) for i in range(200)]
    _commit_files(catalog, principal, ref, parent, good, snapshot_id=900_000_012)
    assert sorted(reads) == sorted(good)
    assert set(reads.values()) == {1}


def test_a_file_that_is_not_parquet_is_refused_without_the_readers_error_text(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The message names the file the caller committed and nothing the reader said."""
    ref, created = _created(catalog, principal, tmp_path)
    uri = f"{created.metadata.location}/data/broken.parquet"
    path = Path(uri.removeprefix("file://"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a parquet file")

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], snapshot_id=900_000_020)

    assert str(caught.value) == (
        f"The commit adds `{shown_path(uri)}` to `gold.sales`, which could not be read as a "
        "Parquet file, so nothing was applied. Write the data file before the commit, as Parquet "
        "with Iceberg field ids."
    )
    assert "magic" not in str(caught.value).lower()


def test_a_file_outside_the_table_is_never_opened(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit check reads only under the table's location; a read refuses the table later."""
    ref, created = _created(catalog, principal, tmp_path)
    outside = tmp_path / "elsewhere" / "x.parquet"
    outside.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), outside)
    opened: list[str] = []

    def record(_file_io: object, path: str) -> bool:
        opened.append(path)
        return True

    monkeypatch.setattr(field_ids_module, "has_field_ids", record)

    _commit_files(catalog, principal, ref, created, [f"file://{outside}"], snapshot_id=900_000_030)

    assert opened == []


def test_a_data_file_in_a_manifest_labelled_as_deletes_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A read walks every manifest, so the check does too, whatever the list entry claims."""
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "hidden.parquet", field_ids=False)

    with pytest.raises(ValidationError) as caught:
        _commit_files(
            catalog,
            principal,
            ref,
            created,
            [uri],
            900_000_040,
            manifest_content=ManifestContent.DELETES,
        )

    assert str(caught.value) == _no_field_ids(uri)


def test_an_orc_file_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.orc", field_ids=True)

    with pytest.raises(ValidationError) as caught:
        _commit_files(
            catalog, principal, ref, created, [uri], 900_000_050, file_format=FileFormat.ORC
        )

    assert str(caught.value) == (
        f"The commit adds `{shown_path(uri)}` to `gold.sales` as ORC, so nothing was applied. "
        "Lakana reads Parquet data files only; write the file as Parquet."
    )


def test_a_missing_manifest_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_060, missing_manifest=True)

    assert str(caught.value) == (
        "A manifest of the commit's snapshot for `gold.sales` could not be read, so nothing was "
        "applied. Write the manifests and the manifest list before the commit."
    )


def test_a_missing_data_file_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = f"{created.metadata.location}/data/never-written.parquet"

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_070)

    assert "could not be read as a Parquet file" in str(caught.value)
    assert shown_path(uri) in str(caught.value)


def test_a_file_name_with_a_line_break_is_escaped_in_the_refusal(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The client's own path is shown as the rest of the codebase shows one, never raw."""
    ref, created = _created(catalog, principal, tmp_path)
    uri = f"{created.metadata.location}/data/a\nb.parquet"

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_071)

    assert "\n" not in str(caught.value)
    assert shown_path(uri) in str(caught.value)


def test_a_commit_of_too_many_files_is_refused_before_any_footer_is_read(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    monkeypatch.setattr(field_ids_module, "MAX_CHECKED_FILES", 3)
    uris = [_parquet(created, f"f{i}.parquet", field_ids=True) for i in range(4)]

    def refuse(*_args: object) -> bool:
        raise AssertionError("a footer was read")

    monkeypatch.setattr(field_ids_module, "has_field_ids", refuse)

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, uris, 900_000_080)

    assert str(caught.value) == (
        "The commit adds more than 3 data files to `gold.sales`; Lakana checks at most 3 per "
        "commit. Split the load into smaller commits."
    )


def test_a_check_that_outlasts_its_deadline_refuses_the_commit(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)
    ticks = iter([0.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr(field_ids_module, "_clock", lambda: next(ticks))

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_090)

    assert str(caught.value) == (
        "Checking the data files the commit adds to `gold.sales` for Iceberg field ids took "
        "longer than 60 seconds, so nothing was applied. Split the commit into smaller ones."
    )


def test_a_footer_above_the_limit_is_refused(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)
    monkeypatch.setattr(field_ids_module, "MAX_FOOTER_BYTES", 10)

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_100)

    assert str(caught.value) == (
        f"The commit adds `{shown_path(uri)}` to `gold.sales`, whose Parquet footer is larger than "
        "16 MiB, so nothing was applied. Write the file with fewer columns and row groups."
    )


def test_a_storage_failure_is_not_blamed_on_the_client(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)

    def fail(*_args: object) -> bool:
        raise OSError("AWS Error SLOW_DOWN for https://bucket.example/secret?X-Amz-Signature=abc")

    monkeypatch.setattr(field_ids_module, "has_field_ids", fail)

    with pytest.raises(StorageUnavailableError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_110)

    assert "Try the commit again" in str(caught.value)
    assert "secret" not in str(caught.value)


def test_a_storage_failure_while_reading_a_manifest_is_not_blamed_on_the_client(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)

    def fail(*_args: object, **_kwargs: object) -> list[object]:
        raise OSError("AWS Error SLOW_DOWN for https://bucket.example/secret?X-Amz-Signature=abc")

    monkeypatch.setattr(field_ids_module, "_live_entries", fail)

    with pytest.raises(StorageUnavailableError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_111)

    assert "Try the commit again" in str(caught.value)
    assert "secret" not in str(caught.value)


def test_a_footer_read_that_outlasts_the_deadline_refuses_the_commit(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that never answers is abandoned when the deadline passes."""
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)
    stuck = threading.Event()

    def hang(*_args: object) -> str:
        stuck.wait(timeout=30)
        return "ok"

    monkeypatch.setattr(field_ids_module, "_footer", hang)
    monkeypatch.setattr(field_ids_module, "CHECK_DEADLINE_SECONDS", 1.0)

    try:
        with pytest.raises(ValidationError) as caught:
            _commit_files(catalog, principal, ref, created, [uri], 900_000_112)
    finally:
        stuck.set()

    assert "took longer than 1 seconds" in str(caught.value)


def test_only_the_snapshots_of_the_commit_are_checked(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy file without ids stays unchecked when a commit adds two snapshots on top."""
    ref, created = _created(catalog, principal, tmp_path)
    legacy_uri = _parquet(created, "legacy.parquet", field_ids=False)
    legacy = _snapshot(created.metadata, None, [legacy_uri], 900_000_120)
    after_legacy = _commit_snapshots(catalog, principal, ref, [legacy], own_data_files=True)
    first_uri = _parquet(created, "one.parquet", field_ids=True)
    second_uri = _parquet(created, "two.parquet", field_ids=True)
    parent = after_legacy.metadata.current_snapshot()
    first = _snapshot(after_legacy.metadata, parent, [first_uri], 900_000_121)
    second = _snapshot(after_legacy.metadata, first, [second_uri], 900_000_122)
    reads: Counter[str] = Counter()
    real = field_ids_module.has_field_ids

    def counting(file_io: object, path: str) -> bool:
        reads[path] += 1
        return real(file_io, path)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(field_ids_module, "has_field_ids", counting)

    committed = _commit_snapshots(catalog, principal, ref, [first, second])

    assert committed.metadata.current_snapshot_id == 900_000_122
    assert reads == Counter({first_uri: 1, second_uri: 1})


def test_the_health_signals_read_every_snapshot_of_the_commit_in_order(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit of two snapshots reads only the files they add, the first one's included (#340).

    With only the last snapshot, its parent would be unknown and every manifest it lists would
    be read as added, the earlier file included.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    tmp_path
        The storage root.
    monkeypatch
        Records the health signals' manifest reads.
    """
    ref, created = _created(catalog, principal, tmp_path)
    earlier = _snapshot(
        created.metadata, None, [_parquet(created, "e.parquet", field_ids=True)], 900_000_123
    )
    before = _commit_snapshots(catalog, principal, ref, [earlier], own_data_files=True)
    first = _snapshot(
        before.metadata, earlier, [_parquet(created, "f.parquet", field_ids=True)], 900_000_124
    )
    second = _snapshot(
        before.metadata, first, [_parquet(created, "s.parquet", field_ids=True)], 900_000_125
    )
    reads: list[tuple[list[int], list[str]]] = []
    real = health_record.added_data_files

    def recording(snapshots: list[Snapshot], *args: object) -> list[DataFile]:
        """Note which snapshots are read and what they add.

        Parameters
        ----------
        snapshots
            The snapshots to read.
        *args
            The read's other arguments.

        Returns
        -------
        list of DataFile
            What the real read returns.
        """
        files = real(snapshots, *args)  # ty: ignore[invalid-argument-type]
        reads.append(([s.snapshot_id for s in snapshots], [Path(f.file_path).name for f in files]))
        return files

    monkeypatch.setattr(health_record, "added_data_files", recording)

    _commit_snapshots(catalog, principal, ref, [first, second], own_data_files=True)

    assert reads == [([900_000_124, 900_000_125], ["f.parquet", "s.parquet"])]


def _forged_parent(snapshot: Snapshot, parent_id: int) -> Snapshot:
    """Copy a snapshot with another parent id, as a client may commit one.

    Parameters
    ----------
    snapshot
        The snapshot to copy.
    parent_id
        The parent id to claim.

    Returns
    -------
    Snapshot
        The copy; its manifest list is the original's.
    """
    return snapshot.model_copy(update={"parent_snapshot_id": parent_id})


def test_a_snapshot_that_names_itself_as_its_parent_is_checked_in_full(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "plain.parquet", field_ids=False)
    own = _snapshot(created.metadata, None, [uri], 900_000_130)

    with pytest.raises(ValidationError) as caught:
        _commit_snapshots(catalog, principal, ref, [_forged_parent(own, 900_000_130)])

    assert str(caught.value) == _no_field_ids(uri)


def test_a_snapshot_whose_parent_comes_later_in_the_commit_is_checked_in_full(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "plain.parquet", field_ids=False)
    other = _parquet(created, "other.parquet", field_ids=True)
    first = _snapshot(created.metadata, None, [uri], 900_000_140)
    second = _snapshot(created.metadata, first, [other], 900_000_141)

    with pytest.raises(ValidationError) as caught:
        _commit_snapshots(catalog, principal, ref, [_forged_parent(first, 900_000_141), second])

    assert str(caught.value) == _no_field_ids(uri)


def test_a_snapshot_with_an_unknown_parent_is_checked_in_full(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "plain.parquet", field_ids=False)
    own = _snapshot(created.metadata, None, [uri], 900_000_150)

    with pytest.raises(ValidationError) as caught:
        _commit_snapshots(catalog, principal, ref, [_forged_parent(own, 424_242)])

    assert str(caught.value) == _no_field_ids(uri)


@pytest.mark.parametrize(
    "content",
    [
        b"PAR1" + b"x" * 20 + (20).to_bytes(4, "little") + b"PAR1",
        b"PAR1" + (0).to_bytes(4, "little") + b"PAR1",
    ],
    ids=["garbage-footer", "empty-footer"],
)
def test_a_garbled_footer_is_the_files_fault_not_a_storage_failure(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, content: bytes
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = f"{created.metadata.location}/data/garbled.parquet"
    path = Path(uri.removeprefix("file://"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    with pytest.raises(ValidationError) as caught:
        _commit_files(catalog, principal, ref, created, [uri], 900_000_160)

    assert "could not be read as a Parquet file" in str(caught.value)
    assert shown_path(uri) in str(caught.value)


def test_collecting_stops_once_the_cap_is_passed(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second snapshot's manifest is never opened once the first one passed the cap."""
    ref, created = _created(catalog, principal, tmp_path)
    monkeypatch.setattr(field_ids_module, "MAX_CHECKED_FILES", 2)
    first = _snapshot(
        created.metadata,
        None,
        [_parquet(created, f"a{i}.parquet", field_ids=True) for i in range(3)],
        900_000_170,
    )
    second = _snapshot(
        created.metadata,
        first,
        [_parquet(created, f"b{i}.parquet", field_ids=True) for i in range(3)],
        900_000_171,
    )
    fetched: list[str] = []
    real = field_ids_module._live_entries

    def counting(file_io: object, manifest: ManifestFile, *rest: object) -> Iterator[ManifestEntry]:
        fetched.append(manifest.manifest_path)
        return real(file_io, manifest, *rest)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(field_ids_module, "_live_entries", counting)

    with pytest.raises(ValidationError, match="Lakana checks at most 2 per commit"):
        _commit_snapshots(catalog, principal, ref, [first, second])

    assert len(fetched) == 1


def test_the_deadline_also_covers_the_manifest_reads(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "a.parquet", field_ids=True)
    ticks = iter([0.0, 1000.0, 1000.0])
    monkeypatch.setattr(field_ids_module, "_clock", lambda: next(ticks))

    opened: list[str] = []

    def record(_file_io: object, path: str) -> bool:
        opened.append(path)
        return True

    monkeypatch.setattr(field_ids_module, "has_field_ids", record)

    with pytest.raises(ValidationError, match="took longer than 60 seconds"):
        _commit_files(catalog, principal, ref, created, [uri], 900_000_180)

    assert opened == []


def _watching_manifest_reads(
    monkeypatch: pytest.MonkeyPatch, on_entry: Callable[[], None]
) -> list[int]:
    """Wrap the manifest entry stream so each entry it yields is counted and announced.

    Parameters
    ----------
    monkeypatch
        Replaces `_live_entries` for the test.
    on_entry
        Called each time an entry is about to be handed to the check.

    Returns
    -------
    list of int
        Holds one element, the number of entries yielded so far.
    """
    real = field_ids_module._live_entries
    count = [0]

    def watching(*args: object) -> Iterator[ManifestEntry]:
        for entry in real(*args):  # ty: ignore[invalid-argument-type]
            count[0] += 1
            on_entry()
            yield entry

    monkeypatch.setattr(field_ids_module, "_live_entries", watching)
    return count


def test_no_database_session_is_open_while_manifests_and_footers_are_read(
    catalog: CatalogService,
    principal: PrincipalRef,
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uris = [_parquet(created, f"s{i}.parquet", field_ids=True) for i in range(3)]
    open_during_entries: list[bool] = []
    open_during_footers: list[bool] = []
    _watching_manifest_reads(
        monkeypatch, lambda: open_during_entries.append(session.in_transaction())
    )
    real = field_ids_module.has_field_ids

    def watching(file_io: object, path: str) -> bool:
        open_during_footers.append(session.in_transaction())
        return real(file_io, path)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(field_ids_module, "has_field_ids", watching)

    _commit_files(catalog, principal, ref, created, uris, 900_000_190)

    assert open_during_entries == [False] * 3
    assert open_during_footers == [False] * 3
    assert catalog.load_table(principal, ref).metadata.current_snapshot_id == 900_000_190


def test_the_health_signals_reuse_the_files_the_check_read(
    catalog: CatalogService,
    principal: PrincipalRef,
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client commit's health signals come from the files the check read (#340).

    The manifests are read once, while the session is released; the signals' history is
    read afterwards.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    session
        Database session.
    tmp_path
        The storage root.
    monkeypatch
        Records a second manifest read.
    """
    ref, created = _created(catalog, principal, tmp_path)
    uris = [_parquet(created, f"h{i}.parquet", field_ids=True) for i in range(2)]
    second_reads: list[object] = []
    monkeypatch.setattr(
        health_record, "added_data_files", lambda *args: second_reads.append(args) or []
    )

    _commit_files(catalog, principal, ref, created, uris, 900_000_191)

    signals = {(r.signal, r.column_name) for r in session.scalars(select(TableHealthSignal))}
    assert signals == {
        ("volume", None),
        ("schema", None),
        ("null_rate", "id"),
        ("null_rate", "name"),
        ("range", "id"),
    }
    assert second_reads == []


def test_the_cap_applies_while_a_manifest_is_being_read(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    monkeypatch.setattr(field_ids_module, "MAX_CHECKED_FILES", 2)
    uris = [_parquet(created, f"c{i}.parquet", field_ids=True) for i in range(6)]
    count = _watching_manifest_reads(monkeypatch, lambda: None)

    with pytest.raises(ValidationError, match="Lakana checks at most 2 per commit"):
        _commit_files(catalog, principal, ref, created, uris, 900_000_191)

    assert count == [3]


def test_the_deadline_applies_while_a_manifest_is_being_read(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uris = [_parquet(created, f"d{i}.parquet", field_ids=True) for i in range(6)]
    snapshot = _snapshot(created.metadata, None, uris, 900_000_192)
    now = [0.0]

    def clock() -> float:
        now[0] += 10.0
        return now[0]

    monkeypatch.setattr(field_ids_module, "_clock", clock)
    count = _watching_manifest_reads(monkeypatch, lambda: None)

    with pytest.raises(ValidationError, match="took longer than 60 seconds"):
        _commit_snapshots(catalog, principal, ref, [snapshot])

    assert 0 < count[0] < 6


def test_a_table_changed_while_its_files_were_checked_refuses_the_commit_cleanly(
    catalog: CatalogService,
    principal: PrincipalRef,
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "r.parquet", field_ids=True)
    changed = [False]

    def another_writer_commits_first() -> None:
        if changed[0]:
            return
        changed[0] = True
        session.execute(
            update(Table)
            .where(Table.id == created.uuid)
            .values(metadata_location=created.metadata_location + ".other")
        )
        session.commit()

    _watching_manifest_reads(monkeypatch, another_writer_commits_first)

    with pytest.raises(ConflictError, match="another writer committed first"):
        _commit_files(catalog, principal, ref, created, [uri], 900_000_193)

    location = session.execute(
        select(Table.metadata_location).where(Table.id == created.uuid)
    ).scalar_one()
    assert location == created.metadata_location + ".other"


def _commit_with_a_flushed_write(
    catalog: CatalogService,
    principal: PrincipalRef,
    session: Session,
    tmp_path: Path,
    write: Callable[[Session, LoadedTable], None],
) -> None:
    """Commit a snapshot, asking for the session release, after `write` ran in the transaction.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    session
        The catalog's session.
    tmp_path
        The storage root.
    write
        Writes to the database through the session; the transaction is then left open.
    """
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "w.parquet", field_ids=True)
    write(session, created)
    _commit_files(catalog, principal, ref, created, [uri], 900_000_194)


def test_the_release_refuses_after_a_flushed_orm_write(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    def write(open_session: Session, created: LoadedTable) -> None:
        row = open_session.get_one(Table, created.uuid)
        row.previous_metadata_location = "touched"
        open_session.flush()

    with pytest.raises(RuntimeError, match="written in the transaction"):
        _commit_with_a_flushed_write(catalog, principal, session, tmp_path, write)


def test_the_release_refuses_after_a_core_statement_that_writes(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    def write(open_session: Session, created: LoadedTable) -> None:
        open_session.execute(
            update(Table)
            .where(Table.id == created.uuid)
            .values(previous_metadata_location="touched")
        )

    with pytest.raises(RuntimeError, match="written in the transaction"):
        _commit_with_a_flushed_write(catalog, principal, session, tmp_path, write)


def test_a_write_that_was_committed_does_not_stop_the_release(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    def write(open_session: Session, created: LoadedTable) -> None:
        open_session.execute(
            update(Table)
            .where(Table.id == created.uuid)
            .values(previous_metadata_location="touched")
        )
        open_session.commit()

    _commit_with_a_flushed_write(catalog, principal, session, tmp_path, write)


def test_a_caller_that_does_not_ask_keeps_its_session_during_the_reads(
    catalog: CatalogService,
    principal: PrincipalRef,
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "k.parquet", field_ids=True)
    open_during_entries: list[bool] = []
    _watching_manifest_reads(
        monkeypatch, lambda: open_during_entries.append(session.in_transaction())
    )
    snapshot = _snapshot(created.metadata, None, [uri], 900_000_195)

    _commit_snapshots(catalog, principal, ref, [snapshot], release_session=False)

    assert open_during_entries == [True]


def test_a_manifest_above_the_byte_limit_is_refused_before_it_is_read(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "m.parquet", field_ids=True)
    snapshot = _snapshot(created.metadata, None, [uri], 900_000_196)
    monkeypatch.setattr(field_ids_module, "MAX_MANIFEST_BYTES", 10)

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a manifest was read")

    monkeypatch.setattr(AvroFile, "__enter__", refuse)

    with pytest.raises(ValidationError) as caught:
        _commit_snapshots(catalog, principal, ref, [snapshot])

    message = str(caught.value)
    assert shown_path(snapshot.manifest_list) in message
    assert "64 MiB" in message
    assert "nothing was applied" in message


class _EndlessStream:
    """A storage stream with more bytes than anyone asks for; it counts what it served."""

    def __init__(self) -> None:
        self.served = 0

    def read(self, size: int = 0) -> bytes:
        """Serve `size` bytes of a file that never ends.

        Parameters
        ----------
        size
            How many bytes the reader wants; `0` is served as a large block, so a reader
            that asks for everything is caught by the count.

        Returns
        -------
        bytes
            That many bytes.
        """
        size = size or 10**9
        self.served += size
        return b"x" * size

    def __enter__(self) -> _EndlessStream:
        """Enter the `with` block.

        Returns
        -------
        _EndlessStream
            Itself.
        """
        return self

    def __exit__(self, *_exc: object) -> None:
        """Leave the `with` block.

        Parameters
        ----------
        *_exc
            The exception, if any.
        """


class _SwappedInput(InputFile):
    """An input file whose size is small when asked and whose content is not."""

    def __init__(self, location: str, stream: _EndlessStream) -> None:
        super().__init__(location)
        self.stream = stream

    def __len__(self) -> int:
        """Report a small size, as a client that swapped the file afterwards would see.

        Returns
        -------
        int
            One byte.
        """
        return 1

    def exists(self) -> bool:
        """Say that the file exists.

        Returns
        -------
        bool
            Always `True`.
        """
        return True

    def open(self, seekable: bool = True) -> _EndlessStream:  # ty: ignore[invalid-method-override]
        """Open the endless stream.

        Parameters
        ----------
        seekable
            Ignored.

        Returns
        -------
        _EndlessStream
            The stream.
        """
        return self.stream


class _SwappedIO(FileIO):
    """A FileIO that serves `_SwappedInput` for every path."""

    stream = _EndlessStream()

    def new_input(self, location: str) -> InputFile:
        """Open a file that reports a small size but holds more.

        Parameters
        ----------
        location
            The file's URI.

        Returns
        -------
        InputFile
            The swapped file.
        """
        return _SwappedInput(location, self.stream)

    def new_output(self, location: str) -> OutputFile:
        """Refuse: the check only reads.

        Parameters
        ----------
        location
            The file's URI.

        Raises
        ------
        NotImplementedError
            Always.
        """
        raise NotImplementedError

    def delete(self, location: str | InputFile | OutputFile) -> None:
        """Refuse: the check only reads.

        Parameters
        ----------
        location
            The file's URI.

        Raises
        ------
        NotImplementedError
            Always.
        """
        raise NotImplementedError


def test_a_manifest_list_that_grows_after_its_size_check_is_cut_at_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#171: the limit holds on the bytes read, not on a size asked for in another request.

    The same limit applies to the parent's manifest list, which the client did not send.
    """
    monkeypatch.setattr(field_ids_module, "MAX_MANIFEST_BYTES", 100_000)
    io = _SwappedIO()
    location = "s3://bucket/gold/sales/metadata/snap-1.avro"

    with pytest.raises(ValidationError) as caught:
        list(field_ids_module._listed_manifests(io, location, "gold.sales", time.monotonic() + 60))

    assert "larger than the 64 MiB" in str(caught.value)
    assert shown_path(location) in str(caught.value)
    assert 100_000 < io.stream.served <= 100_001 + 65_536


def test_the_bytes_the_check_read_are_what_pyiceberg_opens() -> None:
    """The held file reports the bytes it holds and opens them as a seekable stream."""
    held = field_ids_module._HeldFile("s3://b/m.avro", b"abc")
    assert (len(held), held.exists(), held.location) == (3, True, "s3://b/m.avro")
    with held.open() as stream:
        assert stream.read() == b"abc"
        stream.seek(1)
        assert stream.read(1) == b"b"


def test_a_manifest_read_that_outlasts_the_deadline_refuses_the_commit(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "h.parquet", field_ids=True)
    snapshot = _snapshot(created.metadata, None, [uri], 900_000_197)
    stuck = threading.Event()

    def hang(*_args: object, **_kwargs: object) -> object:
        stuck.wait(timeout=30)
        raise OSError("gone")

    monkeypatch.setattr(AvroFile, "__enter__", hang)
    monkeypatch.setattr(field_ids_module, "CHECK_DEADLINE_SECONDS", 0.05)

    try:
        with pytest.raises(ValidationError, match=r"took longer than 0\.05 seconds"):
            _commit_snapshots(catalog, principal, ref, [snapshot])
    finally:
        stuck.set()


@pytest.mark.parametrize("version", [1, 2])
def test_the_entry_stream_yields_what_pyiceberg_reads(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    version: int,
) -> None:
    """`_live_entries` and pyiceberg's own `fetch_manifest_entry` agree on a real manifest."""
    _, created = _created(catalog, principal, tmp_path)
    io = PyArrowFileIO()
    path = f"{created.metadata.location}/metadata/eq-{version}.avro"
    statuses = [
        ManifestEntryStatus.ADDED,
        ManifestEntryStatus.EXISTING,
        ManifestEntryStatus.DELETED,
    ]
    with write_manifest(
        format_version=version,  # ty: ignore[invalid-argument-type]
        spec=created.metadata.spec(),
        schema=created.metadata.schema(),
        output_file=io.new_output(path),
        snapshot_id=900_000_198,
        avro_compression="null",
    ) as writer:
        for index, status in enumerate(statuses):
            entry = ManifestEntry.from_args(
                status=status,
                snapshot_id=900_000_198,
                sequence_number=1,
                file_sequence_number=1,
                data_file=_data_file(f"{created.metadata.location}/data/e{index}.parquet"),
            )
            if status is ManifestEntryStatus.ADDED:
                writer.add(entry)
            elif status is ManifestEntryStatus.EXISTING:
                writer.existing(entry)
            else:
                writer.delete(entry)
    manifest = writer.to_manifest_file()

    streamed = list(
        field_ids_module._live_entries(io, manifest, "gold.sales", field_ids_module._clock() + 3600)
    )
    read = manifest.fetch_manifest_entry(io, discard_deleted=True)

    def summary(entries: list[ManifestEntry]) -> list[tuple[object, object, str]]:
        return [(e.status, e.data_file.content, e.data_file.file_path) for e in entries]

    assert len(streamed) == 2
    assert summary(streamed) == summary(read)


def test_the_deadline_applies_while_the_manifest_list_is_being_iterated(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "l.parquet", field_ids=True)
    snapshot = _snapshot(created.metadata, None, [uri], 900_000_199)
    # The deadline is set and the list opened in time; the first listed manifest is too late.
    ticks = iter([0.0, 0.0])
    monkeypatch.setattr(field_ids_module, "_clock", lambda: next(ticks, 1000.0))

    with pytest.raises(ValidationError, match="took longer than 60 seconds"):
        _commit_snapshots(catalog, principal, ref, [snapshot])


def test_a_file_listed_twice_in_a_manifest_is_checked_once(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    ref, created = _created(catalog, principal, tmp_path)
    uri = _parquet(created, "twice.parquet", field_ids=True)

    _commit_files(catalog, principal, ref, created, [uri, uri], 900_000_200)

    assert catalog.load_table(principal, ref).metadata.current_snapshot_id == 900_000_200
