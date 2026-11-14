"""Table maintenance: snapshot expiry and confined orphan removal."""

from __future__ import annotations

import itertools
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList, python_task_run
from pyiceberg.io import FileIO
from pyiceberg.table import StaticTable
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.refs import SnapshotRef, SnapshotRefType
from pyiceberg.table.update import RemoveSnapshotRefUpdate, SetSnapshotRefUpdate
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef
from lakana_core.catalog import service as service_module
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.maintenance import (
    FAILED_SNAPSHOT_KEEP_DAYS,
    MAX_ORPHAN_AGE_HOURS,
    MIN_ORPHAN_AGE_HOURS,
    MaintenanceStopped,
    old_failed_tags,
    referenced_paths,
    select_expired_snapshots,
    select_orphans,
)
from lakana_core.catalog.service import CatalogService, failed_snapshot_tag
from lakana_core.config import StorageConfig
from lakana_core.errors import ConflictError, PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    QueryRun,
    QueryRunStatus,
    Table,
    TableType,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.storage.base import StoredObject
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver

_LOCATION = "file:///lake/managed/gold/daily-0123"
_ROOT = "file:///lake/"


def _ref(dotted: str) -> TableRef:
    """Address a table in `dev`.

    Parameters
    ----------
    dotted
        The dotted name.

    Returns
    -------
    TableRef
        The reference.
    """
    return TableRef.from_dotted("dev", dotted)


def _ids(ids: list[int]) -> pa.Table:
    """Build one `id` column.

    Parameters
    ----------
    ids
        The ids.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table({"id": pa.array(ids, pa.int64())})


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` at `tmp_path` with `gold.daily`: a create and seven appends.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        An allow-all catalog.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    for index, mode in enumerate([WriteMode.CREATE, *[WriteMode.APPEND] * 7]):
        catalog.write_batches(
            principal,
            _ref("gold.daily"),
            mode=mode,
            schema=_ids([index]).schema,
            batches=_ids([index]).to_batches(),
        )
    return catalog


def _location(catalog: CatalogService, principal: PrincipalRef, dotted: str) -> Path:
    """Find a table's location on local disk.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.

    Returns
    -------
    Path
        Its directory.
    """
    location = catalog.load_table(principal, _ref(dotted)).metadata.location
    return Path(location.removeprefix("file://"))


def _age(path: Path, when: datetime) -> None:
    """Set a file's modification time.

    Parameters
    ----------
    path
        The file.
    when
        The time to set.
    """
    os.utime(path, (when.timestamp(), when.timestamp()))


def _later(days: int) -> datetime:
    """Return a moment `days` from now, to age every real file past a cutoff.

    Parameters
    ----------
    days
        Days ahead.

    Returns
    -------
    datetime
        The moment.
    """
    return datetime.now(UTC) + timedelta(days=days)


def test_expiry_keeps_the_last_n_the_current_and_every_ref(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Always kept — the most recent `keep_last`, the current, branch/tag heads."""
    loaded = catalog.load_table(principal, _ref("gold.daily"))
    oldest = min(loaded.metadata.snapshots, key=lambda s: s.timestamp_ms)

    expired = select_expired_snapshots(loaded.metadata, now=_later(30), keep_days=7, keep_last=5)
    catalog.commit_table(
        principal,
        _ref("gold.daily"),
        requirements=[],
        updates=[
            SetSnapshotRefUpdate(
                ref_name="audit", type=SnapshotRefType.TAG, snapshot_id=oldest.snapshot_id
            )
        ],
    )
    tagged = catalog.load_table(principal, _ref("gold.daily")).metadata

    assert len(expired) == 3
    assert oldest.snapshot_id in expired
    assert loaded.metadata.current_snapshot_id not in expired
    assert oldest.snapshot_id not in select_expired_snapshots(
        tagged, now=_later(30), keep_days=7, keep_last=5
    )


def test_a_table_without_an_old_failed_tag_is_selected_without_walking_its_history(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The history is walked only to protect an ancestor from an old tag's removal."""
    metadata = catalog.load_table(principal, _ref("gold.daily")).metadata
    oldest = min(metadata.snapshots, key=lambda s: s.timestamp_ms)
    by_age = sorted(metadata.snapshots, key=lambda s: s.timestamp_ms)

    def walked(self: TableMetadata, snapshot_id: int) -> None:
        """Fail the test: a snapshot was looked up by its id.

        Parameters
        ----------
        self
            The metadata.
        snapshot_id
            The snapshot asked for.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError(f"the history was walked (snapshot {snapshot_id})")

    monkeypatch.setattr(type(metadata), "snapshot_by_id", walked)

    expired = select_expired_snapshots(metadata, now=_later(30), keep_days=7, keep_last=5)

    assert expired == sorted(s.snapshot_id for s in by_age[:3])
    assert oldest.snapshot_id in expired
    assert metadata.current_snapshot_id not in expired


def test_nothing_younger_than_keep_days_expires(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Every snapshot here is minutes old, so a 7-day window keeps them all."""
    metadata = catalog.load_table(principal, _ref("gold.daily")).metadata

    assert select_expired_snapshots(metadata, now=datetime.now(UTC), keep_days=7, keep_last=1) == []


@pytest.mark.parametrize(("keep_days", "keep_last"), [(-1, 5), (7, 0)])
def test_keep_values_are_validated(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    keep_days: int,
    keep_last: int,
) -> None:
    """`keep_days` ≥ 0 and `keep_last` ≥ 1."""
    metadata = catalog.load_table(principal, _ref("gold.daily")).metadata

    with pytest.raises(ValidationError):
        select_expired_snapshots(
            metadata, now=datetime.now(UTC), keep_days=keep_days, keep_last=keep_last
        )


def _object(relative: str, *, age: timedelta = timedelta(days=2)) -> StoredObject:
    """Build a stored object under `_ROOT`.

    Parameters
    ----------
    relative
        Its root-relative path.
    age
        How long ago it was modified.

    Returns
    -------
    StoredObject
        The object, 10 bytes.
    """
    return StoredObject(path=relative, size=10, modified_at=datetime.now(UTC) - age)


def test_orphan_selection_keeps_everything_that_is_not_a_true_orphan() -> None:
    """Security: only unreferenced, old, confined data/metadata files are selected."""
    orphan = _object("managed/gold/daily-0123/data/orphan.parquet")
    candidates = [
        orphan,
        _object("managed/gold/daily-0123/data/live.parquet"),
        _object("managed/gold/daily-0123/metadata/00001-x.metadata.json"),
        _object("managed/gold/daily-0123/data/new.parquet", age=timedelta(minutes=10)),
        StoredObject(path="managed/gold/daily-0123/data/unknown.parquet", size=1, modified_at=None),
        _object("managed/gold/daily-0123/other/stray.bin"),
        _object("managed/gold/daily-0123/data/n-4567/data/nested.parquet"),
        _object("managed/gold/daily-01234/data/sibling.parquet"),
        _object("managed/gold/daily-0123/data/../../daily-9/data/escape.parquet"),
    ]

    selected = select_orphans(
        candidates,
        root_prefix=_ROOT,
        table_location=_LOCATION,
        nested_locations=[f"{_LOCATION}/data/n-4567"],
        referenced={f"{_LOCATION}/data/live.parquet"},
        cutoff=datetime.now(UTC) - timedelta(hours=MIN_ORPHAN_AGE_HOURS),
    )

    assert selected == [orphan]


def test_maintain_expires_snapshots_and_deletes_true_orphans(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Stray data files and `-m0` manifests go; live data stays readable."""
    location = _location(catalog, principal, "gold.daily")
    stray = location / "data" / "stray.parquet"
    stray.write_bytes(b"x" * 7)
    merged_manifest = location / "metadata" / "0000-abc-m0.avro"
    merged_manifest.write_bytes(b"y" * 3)
    fresh = location / "data" / "fresh.parquet"
    fresh.write_bytes(b"z")
    now = _later(30)
    _age(fresh, now - timedelta(minutes=10))

    report = catalog.maintain(principal, _ref("gold.daily"), now=now)

    assert len(report.snapshots_expired) == 3
    assert not stray.exists()
    assert not merged_manifest.exists()
    assert fresh.exists()
    assert report.files_deleted == 5
    loaded = catalog.load_table(principal, _ref("gold.daily"))
    assert len(loaded.metadata.snapshots) == 5
    rows = StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow()
    assert sorted(rows.column("id").to_pylist()) == list(range(8))
    assert any(location.glob("metadata/*.metadata.json"))


def test_a_replaced_tables_old_files_are_deleted_once_their_snapshot_expires(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Data only an expired snapshot referenced becomes an orphan."""
    before = set(_location(catalog, principal, "gold.daily").glob("data/*.parquet"))
    catalog.write_batches(
        principal,
        _ref("gold.daily"),
        mode=WriteMode.REPLACE,
        schema=_ids([99]).schema,
        batches=_ids([99]).to_batches(),
    )

    report = catalog.maintain(principal, _ref("gold.daily"), keep_last=1, now=_later(30))

    assert len(report.snapshots_expired) == 8
    assert not any(path.exists() for path in before)
    assert report.bytes_deleted > 0
    loaded = catalog.load_table(principal, _ref("gold.daily"))
    rows = StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow()
    assert rows.column("id").to_pylist() == [99]


def test_a_dry_run_changes_nothing_and_records_nothing(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Spec: a dry run records nothing and reports what would happen."""
    location = _location(catalog, principal, "gold.daily")
    files_before = sorted(location.rglob("*"))
    now = _later(30)

    dry = catalog.maintain(principal, _ref("gold.daily"), dry_run=True, now=now)

    assert sorted(location.rglob("*")) == files_before
    assert len(catalog.load_table(principal, _ref("gold.daily")).metadata.snapshots) == 8
    assert (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_MAINTAINED)
        ).first()
        is None
    )
    real = catalog.maintain(principal, _ref("gold.daily"), now=now)
    assert (dry.dry_run, real.dry_run) == (True, False)
    assert (dry.snapshots_expired, dry.files_deleted) == (
        real.snapshots_expired,
        real.files_deleted,
    )


def test_maintain_needs_maintain_table_and_nothing_more(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """MAINTAIN_TABLE is enough; WRITE_TABLE is neither needed nor sufficient."""
    maintainer = CatalogService(
        session, StorageResolver(session), ActionAllowList(Action.MAINTAIN_TABLE)
    )
    writer = CatalogService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))

    with pytest.raises(PermissionDenied, match="MAINTAIN_TABLE"):
        writer.maintain(principal, _ref("gold.daily"), now=_later(30))
    report = maintainer.maintain(principal, _ref("gold.daily"), now=_later(30))

    assert len(report.snapshots_expired) == 3


def test_maintaining_a_view_is_a_clear_no_op(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Spec: views have no files."""
    catalog.create_or_replace_view(
        principal,
        _ref("gold.v"),
        sql="SELECT 1 AS id",
        references=[],
        columns=[ColumnSpec(name="id", type="int", nullable=True)],
    )

    report = catalog.maintain(principal, _ref("gold.v"))

    assert report.note == "gold.v is a view; views have no files to maintain."
    assert (report.snapshots_expired, report.files_deleted) == ((), 0)


def test_a_nested_tables_files_are_never_deleted(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Security: a table living under another's `data/` keeps its files (most specific owner)."""
    outer = catalog.load_table(principal, _ref("gold.daily")).metadata.location
    outer_dir = outer.rsplit("/", 1)[-1]
    for parts in (["gold", outer_dir], ["gold", outer_dir, "data"]):
        catalog.create_namespace(principal, "dev", NamespacePath.from_parts(parts))
    nested = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold", outer_dir, "data"]), name="n"
    )
    catalog.write_batches(
        principal,
        nested,
        mode=WriteMode.CREATE,
        schema=_ids([1]).schema,
        batches=_ids([1]).to_batches(),
    )
    nested_location = catalog.load_table(principal, nested).metadata.location
    nested_files = sorted(Path(nested_location.removeprefix("file://")).rglob("*"))
    assert nested_location.startswith(f"{outer}/data/")

    catalog.maintain(principal, _ref("gold.daily"), keep_last=1, now=_later(30))

    assert sorted(Path(nested_location.removeprefix("file://")).rglob("*")) == nested_files
    rows = StaticTable.from_metadata(catalog.load_table(principal, nested).metadata_location)
    assert rows.scan().to_arrow().column("id").to_pylist() == [1]


def test_a_nested_dropped_tables_files_are_kept_for_its_undo_period(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A dropped table under another's `data/` is no orphan of it while it can come back."""
    outer = catalog.load_table(principal, _ref("gold.daily")).metadata.location
    outer_dir = outer.rsplit("/", 1)[-1]
    for parts in (["gold", outer_dir], ["gold", outer_dir, "data"]):
        catalog.create_namespace(principal, "dev", NamespacePath.from_parts(parts))
    nested = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold", outer_dir, "data"]), name="n"
    )
    catalog.write_batches(
        principal,
        nested,
        mode=WriteMode.CREATE,
        schema=_ids([1]).schema,
        batches=_ids([1]).to_batches(),
    )
    nested_location = catalog.load_table(principal, nested).metadata.location
    nested_files = sorted(Path(nested_location.removeprefix("file://")).rglob("*"))
    catalog.drop_table(principal, nested)

    catalog.maintain(principal, _ref("gold.daily"), keep_last=1, now=_later(30))

    assert sorted(Path(nested_location.removeprefix("file://")).rglob("*")) == nested_files
    catalog.undrop_table(principal, nested)


def test_a_nested_table_restored_while_maintenance_lists_locations_keeps_its_files(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undrop right after maintenance's first read of other tables' locations loses nothing.

    The other tables' locations are read in one statement, so the nested table is in that one
    snapshot as DROPPED or as LIVE, never in neither. The undrop is made to commit right after
    the first location read maintenance makes (whichever function makes it).
    """
    outer = catalog.load_table(principal, _ref("gold.daily")).metadata.location
    outer_dir = outer.rsplit("/", 1)[-1]
    for parts in (["gold", outer_dir], ["gold", outer_dir, "data"]):
        catalog.create_namespace(principal, "dev", NamespacePath.from_parts(parts))
    nested = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold", outer_dir, "data"]), name="n"
    )
    catalog.write_batches(
        principal,
        nested,
        mode=WriteMode.CREATE,
        schema=_ids([1]).schema,
        batches=_ids([1]).to_batches(),
    )
    nested_location = catalog.load_table(principal, nested).metadata.location
    nested_files = sorted(Path(nested_location.removeprefix("file://")).rglob("*"))
    catalog.drop_table(principal, nested)
    done: list[int] = []

    def undrop_after(read: Callable[..., list[str]]) -> Callable[..., list[str]]:
        def racing(*args: object, **kwargs: object) -> list[str]:
            found = read(*args, **kwargs)
            if not done:
                done.append(1)
                catalog.undrop_table(principal, nested)
            return found

        return racing

    for name in ("table_root_locations", "other_table_locations"):
        if hasattr(service_module, name):
            monkeypatch.setattr(service_module, name, undrop_after(getattr(service_module, name)))

    catalog.maintain(principal, _ref("gold.daily"), keep_last=1, now=_later(30))

    assert done == [1]
    assert sorted(Path(nested_location.removeprefix("file://")).rglob("*")) == nested_files
    rows = StaticTable.from_metadata(catalog.load_table(principal, nested).metadata_location)
    assert rows.scan().to_arrow().column("id").to_pylist() == [1]


def test_orphan_removal_waits_while_a_write_into_the_table_is_running(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A write longer than the cutoff has uncommitted files older than it."""
    location = _location(catalog, principal, "gold.daily")
    in_flight = location / "data" / "in-flight.parquet"
    in_flight.write_bytes(b"w")
    table_id = catalog.load_table(principal, _ref("gold.daily")).uuid
    env = session.execute(select(Environment)).scalar_one()
    session.add(
        QueryRun(
            id=uuid.uuid4(),
            environment_id=env.id,
            principal_id=principal.id,
            sql_text="SELECT 1",
            status=QueryRunStatus.RUNNING,
            write_mode=WriteMode.APPEND,
            target_name="gold.daily",
            target_table_id=table_id,
        )
    )
    session.commit()

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30))

    assert in_flight.exists()
    assert report.files_deleted == 0
    assert report.note is not None
    assert "running" in report.note
    assert len(report.snapshots_expired) == 3


def test_maintenance_is_audited(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`TABLE_MAINTAINED` records snapshots expired and files and bytes deleted."""
    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30))

    event = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_MAINTAINED)
    ).scalar_one()
    assert event.securable_table == "daily"
    assert event.detail == {**report.to_json(), "keep_days": 7, "keep_last": 5}


def test_nothing_to_expire_changes_no_snapshots(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Every snapshot is minutes old, so a real run expires nothing and commits nothing."""
    report = catalog.maintain(principal, _ref("gold.daily"))

    assert report.snapshots_expired == ()
    assert len(catalog.load_table(principal, _ref("gold.daily")).metadata.snapshots) == 8


def test_an_external_table_is_refused(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """An external table's files belong to whoever produced them."""
    row = catalog.load_table_row(principal, _ref("gold.daily"))
    row.table_type = TableType.EXTERNAL
    session.commit()

    with pytest.raises(ValidationError, match="managed tables"):
        catalog.maintain(principal, _ref("gold.daily"))


def test_a_table_predating_the_location_scheme_is_refused(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an id-suffixed location, ownership of files can't be established."""
    monkeypatch.setattr("lakana_core.catalog.service.is_id_suffixed_location", lambda *_: False)

    with pytest.raises(ValidationError, match="created by an older Lakana"):
        catalog.maintain(principal, _ref("gold.daily"))


def test_a_failed_delete_is_skipped_and_not_counted(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion is best-effort per file; only successes are counted."""
    location = _location(catalog, principal, "gold.daily")
    doomed = location / "data" / "doomed.parquet"
    stray = location / "data" / "stray.parquet"
    doomed.write_bytes(b"d")
    stray.write_bytes(b"s")
    original = FsspecStorage.delete

    def flaky(self: FsspecStorage, path: str) -> None:
        if path.endswith("doomed.parquet"):
            raise OSError("boom")
        original(self, path)

    monkeypatch.setattr(FsspecStorage, "delete", flaky)

    report = catalog.maintain(principal, _ref("gold.daily"), keep_last=8, now=_later(30))

    assert doomed.exists()
    assert not stray.exists()
    assert report.files_deleted == 1


def test_a_table_with_no_snapshot_has_nothing_to_expire(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A zero-row create has no current snapshot; selection must not trip over that."""
    empty = _ids([])
    catalog.write_batches(
        principal,
        _ref("gold.empty"),
        mode=WriteMode.CREATE,
        schema=empty.schema,
        batches=empty.to_batches(),
    )

    report = catalog.maintain(principal, _ref("gold.empty"), now=_later(30))

    assert report.snapshots_expired == ()


def test_expiry_commits_only_against_the_metadata_it_selected_from(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tag created after selection must survive, and nothing may be deleted."""
    location = _location(catalog, principal, "gold.daily")
    files_before = sorted(location.rglob("*"))
    oldest = min(
        catalog.load_table(principal, _ref("gold.daily")).metadata.snapshots,
        key=lambda s: s.timestamp_ms,
    )
    real = select_expired_snapshots

    def select_then_tag(
        metadata: TableMetadata, *, now: datetime, keep_days: int, keep_last: int, **rest: object
    ) -> list[int]:
        chosen = real(metadata, now=now, keep_days=keep_days, keep_last=keep_last, **rest)  # ty: ignore[invalid-argument-type]
        catalog.commit_table(
            principal,
            _ref("gold.daily"),
            requirements=[],
            updates=[
                SetSnapshotRefUpdate(
                    ref_name="late", type=SnapshotRefType.TAG, snapshot_id=oldest.snapshot_id
                )
            ],
        )
        return chosen

    monkeypatch.setattr("lakana_core.catalog.service.select_expired_snapshots", select_then_tag)
    files_before = sorted(location.rglob("*"))

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30))

    loaded = catalog.load_table(principal, _ref("gold.daily"))
    assert "late" in loaded.metadata.refs
    assert len(loaded.metadata.snapshots) == 8
    assert report.note == "table changed during maintenance, retry"
    assert report.conflict is True
    assert report.to_json()["conflict"] is True
    assert (report.snapshots_expired, report.files_deleted) == ((), 0)
    assert {p for p in location.rglob("*")} >= set(files_before)
    assert (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_MAINTAINED)
        ).first()
        is None
    )


def test_commit_table_refuses_a_stale_expected_base(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`expected_metadata_location` is a compare-and-swap on the exact base."""
    with pytest.raises(ConflictError):
        catalog.commit_table(
            principal,
            _ref("gold.daily"),
            requirements=[],
            updates=[],
            expected_metadata_location="file:///stale/metadata/00000-x.metadata.json",
        )


@pytest.mark.parametrize(
    "spelling",
    [
        "file:/lake/managed/gold/daily-0123/data/x.parquet",
        "s3a://lake/managed/gold/daily-0123/data/x.parquet",
        "%LOC%/data/a%20b.parquet",
        "%LOC%/data/../../other/x.parquet",
    ],
)
def test_a_foreign_spelled_reference_skips_orphan_removal(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
) -> None:
    """Fail closed: a reference we can't match by string could be live data."""
    location = _location(catalog, principal, "gold.daily")
    stray = location / "data" / "stray.parquet"
    stray.write_bytes(b"x")
    real = referenced_paths
    uri = spelling.replace("%LOC%", f"file://{location}")

    def with_foreign(metadata: TableMetadata, metadata_location: str, file_io: FileIO) -> set[str]:
        return {*real(metadata, metadata_location, file_io), uri}

    monkeypatch.setattr("lakana_core.catalog.service.referenced_paths", with_foreign)

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30))

    assert stray.exists()
    assert report.files_deleted == 0
    assert report.note == ("referenced paths outside the table location; orphan removal skipped.")


def test_the_default_minimum_age_is_three_days(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A file two days old survives the default; `older_than_hours=1` deletes it."""
    location = _location(catalog, principal, "gold.daily")
    stray = location / "data" / "stray.parquet"
    stray.write_bytes(b"x")
    _age(stray, datetime.now(UTC) - timedelta(days=2))

    kept = catalog.maintain(principal, _ref("gold.daily"))
    assert stray.exists()
    assert kept.files_deleted == 0

    catalog.maintain(principal, _ref("gold.daily"), older_than_hours=1)
    assert not stray.exists()


def test_older_than_hours_has_a_floor_of_one(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Anything below one hour is rejected."""
    with pytest.raises(ValidationError, match="older_than_hours"):
        catalog.maintain(principal, _ref("gold.daily"), older_than_hours=0)


def test_a_symlinked_data_directory_is_never_followed_or_deleted(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Security: `data` -> outside must neither list nor delete the outside files."""
    location = _location(catalog, principal, "gold.daily")
    outside = tmp_path_factory.mktemp("outside")
    victim = outside / "victim.parquet"
    victim.write_bytes(b"precious")
    _age(victim, datetime.now(UTC) - timedelta(days=30))
    real_data = location / "data"
    moved = location / "data-real"
    real_data.rename(moved)
    real_data.symlink_to(outside, target_is_directory=True)

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30))

    assert victim.exists()
    assert real_data.is_symlink()
    assert report.snapshots_expired


def test_a_symlinked_file_inside_data_is_skipped(
    session: Session, principal: PrincipalRef, catalog: CatalogService, tmp_path: Path
) -> None:
    """A symlink (file or directory) under `data/` is neither deleted nor followed."""
    location = _location(catalog, principal, "gold.daily")
    outside = tmp_path.parent / f"{tmp_path.name}-out"
    outside.mkdir()
    (outside / "f.parquet").write_bytes(b"precious")
    link = location / "data" / "link.parquet"
    link.symlink_to(outside / "f.parquet")
    dirlink = location / "data" / "dirlink"
    dirlink.symlink_to(outside, target_is_directory=True)

    catalog.maintain(principal, _ref("gold.daily"), now=_later(30))

    assert (outside / "f.parquet").exists()
    assert link.is_symlink()
    assert dirlink.is_symlink()


def test_a_successful_run_reports_no_conflict(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`conflict` is the machine-readable failure; an ordinary run leaves it false."""
    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30))

    assert report.conflict is False
    assert report.to_json()["conflict"] is False


def test_orphan_removal_is_skipped_when_the_metadata_pointer_moved(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A commit landing after orphan selection may reference a selected file: delete nothing.

    The pointer is re-read immediately before deletion. The expiry already
    committed, so the run still succeeds, is audited, and says why nothing
    was deleted — it is not a conflict.
    """
    location = _location(catalog, principal, "gold.daily")
    stray = location / "data" / "stray.parquet"
    stray.write_bytes(b"x")
    current = catalog.load_table(principal, _ref("gold.daily")).metadata.current_snapshot_id
    assert current is not None
    real = select_orphans

    def select_then_commit(*args: object, **kwargs: object) -> list[StoredObject]:
        chosen = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        catalog.commit_table(
            principal,
            _ref("gold.daily"),
            requirements=[],
            updates=[
                SetSnapshotRefUpdate(ref_name="late", type=SnapshotRefType.TAG, snapshot_id=current)
            ],
        )
        return chosen

    monkeypatch.setattr("lakana_core.catalog.service.select_orphans", select_then_commit)

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30))

    assert stray.exists()
    assert report.files_deleted == 0
    assert len(report.snapshots_expired) == 3
    assert report.conflict is False
    assert report.note == "the table changed before orphan removal; orphan removal skipped."
    assert (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_MAINTAINED)
        ).first()
        is not None
    )


def test_older_than_hours_is_capped_at_ten_years(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Above the cap is refused up front; the cap itself is accepted."""
    with pytest.raises(ValidationError, match=f"at most {MAX_ORPHAN_AGE_HOURS}"):
        catalog.maintain(principal, _ref("gold.daily"), older_than_hours=MAX_ORPHAN_AGE_HOURS + 1)

    report = catalog.maintain(principal, _ref("gold.daily"), older_than_hours=MAX_ORPHAN_AGE_HOURS)
    assert report.conflict is False
    assert MAX_ORPHAN_AGE_HOURS == 87600


def test_an_overflowing_older_than_hours_never_leaves_an_unaudited_expiry(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A cutoff too large for `datetime` once overflowed only after the expiry committed."""
    before = len(catalog.load_table(principal, _ref("gold.daily")).metadata.snapshots)

    with pytest.raises(ValidationError, match="older_than_hours"):
        catalog.maintain(principal, _ref("gold.daily"), older_than_hours=10**9, now=_later(30))

    assert len(catalog.load_table(principal, _ref("gold.daily")).metadata.snapshots) == before


_DAY_MS = 24 * 60 * 60 * 1000
_RUN = uuid.UUID("00000000-0000-0000-0000-0000000000a1")


def _tag(
    catalog: CatalogService,
    principal: PrincipalRef,
    dotted: str,
    name: str,
    snapshot_id: int,
    ref_type: SnapshotRefType = SnapshotRefType.TAG,
) -> None:
    """Point a tag (or, for a test of the name filter, a branch) at a snapshot.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.
    name
        The ref's name.
    snapshot_id
        The snapshot it names.
    ref_type
        Tag or branch.
    """
    catalog.commit_table(
        principal,
        _ref(dotted),
        requirements=[],
        updates=[SetSnapshotRefUpdate(ref_name=name, type=ref_type, snapshot_id=snapshot_id)],
        # Only the rollback sets a ref with the name of a failed check's tag.
        internal_rollback=True,
    )


def _oldest(catalog: CatalogService, principal: PrincipalRef, dotted: str = "gold.daily") -> int:
    """Return the id of a table's oldest snapshot.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.

    Returns
    -------
    int
        The snapshot id.
    """
    metadata = catalog.load_table(principal, _ref(dotted)).metadata
    return min(metadata.snapshots, key=lambda s: s.timestamp_ms).snapshot_id


def _refs(catalog: CatalogService, principal: PrincipalRef, dotted: str = "gold.daily") -> set[str]:
    """Return the names of a table's refs.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.

    Returns
    -------
    set of str
        The ref names.
    """
    return set(catalog.load_table(principal, _ref(dotted)).metadata.refs)


def test_a_tag_is_old_only_when_its_snapshot_is_more_than_seven_days_old(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Exactly seven days is not old yet; one millisecond more is."""
    snapshot_id = _oldest(catalog, principal)
    tag = failed_snapshot_tag(_RUN)
    _tag(catalog, principal, "gold.daily", tag, snapshot_id)
    metadata = catalog.load_table(principal, _ref("gold.daily")).metadata
    tagged = metadata.snapshot_by_id(snapshot_id)
    assert tagged is not None
    stamp = tagged.timestamp_ms
    limit = stamp + FAILED_SNAPSHOT_KEEP_DAYS * _DAY_MS

    assert old_failed_tags(metadata, now_ms=limit - 1) == []
    assert old_failed_tags(metadata, now_ms=limit) == []
    assert old_failed_tags(metadata, now_ms=limit + 1) == [tag]


def test_a_tag_younger_than_seven_days_stays(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Six days after the failure the tag and its snapshot are still there."""
    snapshot_id = _oldest(catalog, principal)
    tag = failed_snapshot_tag(_RUN)
    _tag(catalog, principal, "gold.daily", tag, snapshot_id)

    report = catalog.maintain(principal, _ref("gold.daily"), keep_days=0, now=_later(6))

    assert report.tags_removed == ()
    assert tag in _refs(catalog, principal)
    assert snapshot_id not in report.snapshots_expired


def _append(catalog: CatalogService, principal: PrincipalRef, dotted: str, value: int) -> None:
    """Append one row to a table.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.
    value
        The `id` of the row.
    """
    catalog.write_batches(
        principal,
        _ref(dotted),
        mode=WriteMode.APPEND,
        schema=_ids([value]).schema,
        batches=_ids([value]).to_batches(),
    )


@dataclass(frozen=True)
class _RolledBack:
    """A table whose second snapshot failed a check and was set back, as the rollback leaves it.

    Parameters
    ----------
    good
        The snapshot `main` went back to.
    failed
        The failed snapshot, kept under `tag`.
    failed_files
        The data files only the failed snapshot wrote.
    tag
        The failed check's tag.
    """

    good: int
    failed: int
    failed_files: frozenset[Path]
    tag: str


def _rolled_back(catalog: CatalogService, principal: PrincipalRef, dotted: str) -> _RolledBack:
    """Create a table with the row 1, append the row 2 and set `main` back, tagging the failure.

    The tag and the move of `main` go in one metadata commit, which is what the rollback does;
    the rollback itself needs a task run, which this module has no use for.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table to create.

    Returns
    -------
    _RolledBack
        The two snapshots, the failed one's files and its tag.
    """
    ref = _ref(dotted)
    seen: set[Path] = set()
    failed_files: set[Path] = set()
    for rows, mode in (([1], WriteMode.CREATE), ([2], WriteMode.APPEND)):
        catalog.write_batches(
            principal, ref, mode=mode, schema=_ids(rows).schema, batches=_ids(rows).to_batches()
        )
        location = _location(catalog, principal, dotted)
        failed_files = set(location.glob("data/*.parquet")) - seen
        seen |= failed_files
    assert len(failed_files) == 1
    good, failed = sorted(
        catalog.load_table(principal, ref).metadata.snapshots, key=lambda s: s.timestamp_ms
    )
    tag = failed_snapshot_tag(_RUN)
    catalog.commit_table(
        principal,
        ref,
        requirements=[],
        updates=[
            SetSnapshotRefUpdate(
                ref_name=tag, type=SnapshotRefType.TAG, snapshot_id=failed.snapshot_id
            ),
            SetSnapshotRefUpdate(
                ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=good.snapshot_id
            ),
        ],
        internal_rollback=True,
    )
    return _RolledBack(good.snapshot_id, failed.snapshot_id, frozenset(failed_files), tag)


def _ids_of_main(catalog: CatalogService, principal: PrincipalRef, dotted: str) -> list[int]:
    """Read the `id` column of a table's current snapshot, sorted.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.

    Returns
    -------
    list of int
        The ids.
    """
    loaded = catalog.load_table(principal, _ref(dotted))
    rows = StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow()
    return sorted(rows.column("id").to_pylist())


def _snapshot_ids(catalog: CatalogService, principal: PrincipalRef, dotted: str) -> set[int]:
    """Return the ids of the snapshots a table still has.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.

    Returns
    -------
    set of int
        The snapshot ids.
    """
    return {s.snapshot_id for s in catalog.load_table(principal, _ref(dotted)).metadata.snapshots}


@pytest.mark.parametrize("keep_days", [7, 30])
def test_an_old_tag_goes_and_its_snapshot_with_it(
    session: Session, principal: PrincipalRef, catalog: CatalogService, keep_days: int
) -> None:
    """Eight days on, the failed snapshot goes with its tag though the keep values would hold it."""
    rolled = _rolled_back(catalog, principal, "gold.checked")
    _append(catalog, principal, "gold.checked", 3)
    location = _location(catalog, principal, "gold.checked")
    kept_files = set(location.glob("data/*.parquet")) - rolled.failed_files

    report = catalog.maintain(
        principal, _ref("gold.checked"), keep_days=keep_days, now=_later(max(8, keep_days + 1))
    )

    assert report.tags_removed == (rolled.tag,)
    assert report.snapshots_expired == (rolled.failed,)
    assert rolled.tag not in _refs(catalog, principal, "gold.checked")
    assert rolled.failed not in _snapshot_ids(catalog, principal, "gold.checked")
    assert not any(path.exists() for path in rolled.failed_files)
    assert all(path.exists() for path in kept_files)
    assert _ids_of_main(catalog, principal, "gold.checked") == [1, 3]


def test_a_dry_run_selects_the_failed_snapshot_and_writes_nothing(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The dry run reports the tag and the failed snapshot; the table and its files stay."""
    rolled = _rolled_back(catalog, principal, "gold.checked")
    _append(catalog, principal, "gold.checked", 3)
    location = _location(catalog, principal, "gold.checked")
    before = sorted(location.rglob("*"))
    pointer = catalog.load_table(principal, _ref("gold.checked")).metadata_location

    dry = catalog.maintain(principal, _ref("gold.checked"), dry_run=True, now=_later(8))

    assert (dry.tags_removed, dry.snapshots_expired) == ((rolled.tag,), (rolled.failed,))
    assert sorted(location.rglob("*")) == before
    assert catalog.load_table(principal, _ref("gold.checked")).metadata_location == pointer


def test_an_old_tag_goes_and_its_snapshot_stays_when_another_ref_names_it(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A second tag on the failed snapshot keeps it: only the failed check's tag is removed."""
    rolled = _rolled_back(catalog, principal, "gold.checked")
    _append(catalog, principal, "gold.checked", 3)
    _tag(catalog, principal, "gold.checked", "audit", rolled.failed)

    report = catalog.maintain(principal, _ref("gold.checked"), now=_later(8))

    assert report.tags_removed == (rolled.tag,)
    assert report.snapshots_expired == ()
    assert _refs(catalog, principal, "gold.checked") == {"main", "audit"}
    assert rolled.failed in _snapshot_ids(catalog, principal, "gold.checked")
    assert all(path.exists() for path in rolled.failed_files)


def test_an_old_tag_goes_and_its_snapshot_stays_when_it_is_an_ancestor_of_a_remaining_ref(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A snapshot that a branch built on keeps its data: only the tag is removed.

    A rolled-back snapshot is never in `main`'s history, so the branch is made by hand:
    `main` is moved onto the failed snapshot, a row is appended there, and the new snapshot
    becomes the branch `side` while `main` goes back.
    """
    rolled = _rolled_back(catalog, principal, "gold.checked")
    ref = _ref("gold.checked")
    catalog.commit_table(
        principal,
        ref,
        requirements=[],
        updates=[
            SetSnapshotRefUpdate(
                ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=rolled.failed
            )
        ],
    )
    _append(catalog, principal, "gold.checked", 3)
    built_on_it = catalog.load_table(principal, ref).metadata.current_snapshot_id
    assert built_on_it is not None
    catalog.commit_table(
        principal,
        ref,
        requirements=[],
        updates=[
            SetSnapshotRefUpdate(
                ref_name="side", type=SnapshotRefType.BRANCH, snapshot_id=built_on_it
            ),
            SetSnapshotRefUpdate(
                ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=rolled.good
            ),
        ],
    )
    metadata = catalog.load_table(principal, ref).metadata
    child = metadata.snapshot_by_id(built_on_it)
    assert child is not None
    assert child.parent_snapshot_id == rolled.failed

    report = catalog.maintain(principal, ref, now=_later(8))

    assert report.tags_removed == (rolled.tag,)
    assert report.snapshots_expired == ()
    assert _refs(catalog, principal, "gold.checked") == {"main", "side"}
    assert rolled.failed in _snapshot_ids(catalog, principal, "gold.checked")


def test_a_prefixed_ref_without_a_snapshot_is_skipped_not_a_crash(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Metadata that names a snapshot it does not hold neither crashes nor removes the ref."""
    metadata = catalog.load_table(principal, _ref("gold.daily")).metadata
    dangling = SnapshotRef(snapshot_id=12345, snapshot_ref_type=SnapshotRefType.TAG)
    broken = metadata.model_copy(
        update={"refs": {**metadata.refs, failed_snapshot_tag(_RUN): dangling}}
    )

    assert old_failed_tags(broken, now_ms=2**62) == []
    expired = select_expired_snapshots(broken, now=_later(30), keep_days=7, keep_last=5)
    assert expired  # the table's own old snapshots are still chosen
    assert expired == select_expired_snapshots(
        metadata, now=_later(30), keep_days=7, keep_last=5
    )  # the dangling tag changes nothing


def test_a_tag_of_another_name_is_never_removed(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Only the name `failed_snapshot_tag` gives, and only as a tag, is ever removed."""
    snapshot_id = _oldest(catalog, principal)
    names = ["audit", "lakana-check-failed", "keep-lakana-check-failed-1"]
    for name in names:
        _tag(catalog, principal, "gold.daily", name, snapshot_id)
    _tag(
        catalog,
        principal,
        "gold.daily",
        failed_snapshot_tag(_RUN),
        snapshot_id,
        SnapshotRefType.BRANCH,
    )

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30))

    assert report.tags_removed == ()
    assert _refs(catalog, principal) >= {*names, failed_snapshot_tag(_RUN)}
    assert snapshot_id not in report.snapshots_expired


def test_a_dry_run_reports_the_tags_and_removes_nothing(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A dry run names the tag it would remove and changes and records nothing."""
    tag = failed_snapshot_tag(_RUN)
    _tag(catalog, principal, "gold.daily", tag, _oldest(catalog, principal))
    before = catalog.load_table(principal, _ref("gold.daily")).metadata_location

    dry = catalog.maintain(principal, _ref("gold.daily"), dry_run=True, now=_later(8))

    assert dry.tags_removed == (tag,)
    assert catalog.load_table(principal, _ref("gold.daily")).metadata_location == before
    assert tag in _refs(catalog, principal)
    assert (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_MAINTAINED)
        ).first()
        is None
    )


def test_the_report_and_the_audit_event_list_the_tags_removed(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`tags_removed` is in the report's JSON and in the event's detail."""
    tag = failed_snapshot_tag(_RUN)
    _tag(catalog, principal, "gold.daily", tag, _oldest(catalog, principal))

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(8))

    assert report.to_json()["tags_removed"] == [tag]
    event = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_MAINTAINED)
    ).scalar_one()
    assert event.detail["tags_removed"] == [tag]


def test_a_maintenance_commit_may_remove_only_the_tag_of_a_failed_check(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Besides snapshot expiry, the one ref a maintenance commit removes is a failed check's tag."""
    tag = failed_snapshot_tag(_RUN)
    _tag(catalog, principal, "gold.daily", tag, _oldest(catalog, principal))
    _tag(catalog, principal, "gold.daily", "audit", _oldest(catalog, principal))

    for name in ("audit", "main"):
        with pytest.raises(ValidationError, match="may only expire snapshots or remove"):
            catalog.commit_table(
                principal,
                _ref("gold.daily"),
                requirements=[],
                updates=[RemoveSnapshotRefUpdate(ref_name=name)],
                action=Action.MAINTAIN_TABLE,
            )
    catalog.commit_table(
        principal,
        _ref("gold.daily"),
        requirements=[],
        updates=[RemoveSnapshotRefUpdate(ref_name=tag)],
        action=Action.MAINTAIN_TABLE,
    )

    assert _refs(catalog, principal) == {"audit", "main"}


def _table_id(session: Session, name: str = "daily") -> uuid.UUID:
    """Return the id of a table in `gold`.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    return session.scalars(select(Table.id).where(Table.name == name)).one()


def _task_needing(
    session: Session,
    principal: PrincipalRef,
    *,
    status: TaskStatus = TaskStatus.RUNNING,
    **values: object,
) -> TaskRun:
    """Insert a task run of `gold.daily` and record the snapshot it may set its table back to.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The run's principal.
    status
        The task run's status.
    **values
        Columns to set: `written_table_id` with `written_snapshot_before`, or
        `baseline_snapshot_id`.

    Returns
    -------
    TaskRun
        The task run.
    """
    task = python_task_run(session, principal.id, asset="gold.daily", kind="sql", status=status)
    session.execute(update(TaskRun).where(TaskRun.id == task.id).values(**values))
    session.commit()
    return task


def test_a_running_tasks_before_snapshot_survives_maintenance(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Retention would expire it, but the running task needs it to set its table back."""
    oldest = _oldest(catalog, principal)
    _task_needing(
        session, principal, written_table_id=_table_id(session), written_snapshot_before=oldest
    )

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30), keep_last=5)

    assert len(report.snapshots_expired) == 2
    assert oldest not in report.snapshots_expired
    assert oldest in _snapshot_ids(catalog, principal, "gold.daily")


@pytest.mark.parametrize("status", sorted(TaskStatus))
def test_the_before_snapshot_goes_once_the_task_has_ended(
    session: Session, principal: PrincipalRef, catalog: CatalogService, status: TaskStatus
) -> None:
    """Only a task run that is not over yet keeps it: pending, ready and running."""
    oldest = _oldest(catalog, principal)
    _task_needing(
        session,
        principal,
        status=status,
        written_table_id=_table_id(session),
        written_snapshot_before=oldest,
    )

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30), keep_last=5)

    ended = status in {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.UPSTREAM_FAILED,
        TaskStatus.CANCELLED,
    }
    assert (oldest in report.snapshots_expired) is ended


def test_a_task_run_of_another_table_spares_nothing_here(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The before snapshot counts only for the table the task run wrote."""
    oldest = _oldest(catalog, principal)
    _task_needing(session, principal, written_table_id=uuid.uuid4(), written_snapshot_before=oldest)

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30), keep_last=5)

    assert oldest in report.snapshots_expired


def test_a_running_tasks_baseline_survives_maintenance(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A Python task's baseline names no table; its snapshot id alone keeps it."""
    oldest = _oldest(catalog, principal)
    _task_needing(session, principal, baseline_snapshot_id=oldest)

    report = catalog.maintain(principal, _ref("gold.daily"), now=_later(30), keep_last=5)

    assert oldest not in report.snapshots_expired
    assert oldest in _snapshot_ids(catalog, principal, "gold.daily")


def test_the_removal_of_an_old_failed_tag_does_not_take_a_spared_snapshot(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The tag goes after seven days; the snapshot stays while a running task names it."""
    rolled = _rolled_back(catalog, principal, "gold.checked")
    _task_needing(session, principal, baseline_snapshot_id=rolled.failed)

    report = catalog.maintain(principal, _ref("gold.checked"), now=_later(8))

    assert report.tags_removed == (rolled.tag,)
    assert report.snapshots_expired == ()
    assert rolled.failed in _snapshot_ids(catalog, principal, "gold.checked")


def test_a_spared_snapshot_is_never_selected(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Neither by age nor through an old tag of a failed check."""
    rolled = _rolled_back(catalog, principal, "gold.checked")
    checked = catalog.load_table(principal, _ref("gold.checked")).metadata
    daily = catalog.load_table(principal, _ref("gold.daily")).metadata
    oldest = _oldest(catalog, principal)
    later = _later(30)

    assert rolled.failed in select_expired_snapshots(checked, now=later, keep_days=7, keep_last=1)
    assert oldest in select_expired_snapshots(daily, now=later, keep_days=7, keep_last=1)
    assert rolled.failed not in select_expired_snapshots(
        checked, now=later, keep_days=7, keep_last=1, spare=[rolled.failed]
    )
    assert oldest not in select_expired_snapshots(
        daily, now=later, keep_days=7, keep_last=1, spare={oldest}
    )


def _stops_after(checks: int) -> Callable[[], bool]:
    """Build a stop signal that lets `checks` questions pass and then says stop.

    Parameters
    ----------
    checks
        How many times it answers `False` first.

    Returns
    -------
    Callable
        The signal.
    """
    asked = itertools.count(1)
    return lambda: next(asked) > checks


def test_a_stop_before_anything_ran_changes_nothing_and_a_later_run_completes(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A deadline that is already over leaves the table as it was, and says so."""
    ref = _ref("gold.daily")
    location = _location(catalog, principal, "gold.daily")
    stray = location / "data" / "stray.parquet"
    stray.write_bytes(b"x")

    with pytest.raises(MaintenanceStopped) as stopped:
        catalog.maintain(principal, ref, now=_later(30), should_stop=_stops_after(0))

    assert stopped.value.report.snapshots_expired == ()
    assert stopped.value.report.files_deleted == 0
    assert stray.exists()
    assert len(catalog.load_table(principal, ref).metadata.snapshots) == 8
    report = catalog.maintain(principal, ref, now=_later(30))
    assert len(report.snapshots_expired) == 3
    assert not stray.exists()


def test_a_stopped_dry_run_writes_no_audit_row(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A dry run records nothing, stopped or not."""
    with pytest.raises(MaintenanceStopped):
        catalog.maintain(
            principal, _ref("gold.daily"), dry_run=True, now=_later(30), should_stop=_stops_after(0)
        )

    assert (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_MAINTAINED)
        ).first()
        is None
    )


def test_a_stop_after_the_expiry_skips_the_orphans_and_the_next_run_deletes_them(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """The expiry is one commit; stopping after it leaves a valid table with its orphans."""
    ref = _ref("gold.daily")
    stray = _location(catalog, principal, "gold.daily") / "data" / "stray.parquet"
    stray.write_bytes(b"x")

    with pytest.raises(MaintenanceStopped) as stopped:
        catalog.maintain(principal, ref, now=_later(30), should_stop=_stops_after(1))

    assert len(stopped.value.report.snapshots_expired) == 3
    assert stopped.value.report.files_deleted == 0
    assert stray.exists()
    loaded = catalog.load_table(principal, ref)
    assert len(loaded.metadata.snapshots) == 5
    assert StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow().num_rows == 8
    event = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_MAINTAINED)
    ).scalar_one()
    assert event.detail["stopped"] is True
    resumed = catalog.maintain(principal, ref, now=_later(30))
    assert resumed.snapshots_expired == ()
    assert not stray.exists()


def test_a_stop_between_files_keeps_what_was_deleted_and_the_next_run_finishes(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Orphans go one at a time; a stop between two leaves the rest for the next run."""
    ref = _ref("gold.daily")
    data = _location(catalog, principal, "gold.daily") / "data"
    strays = [data / f"stray-{index}.parquet" for index in range(4)]
    for stray in strays:
        stray.write_bytes(b"x")

    with pytest.raises(MaintenanceStopped) as stopped:
        catalog.maintain(principal, ref, now=_later(30), should_stop=_stops_after(4))

    assert stopped.value.report.files_deleted == 2
    assert stopped.value.report.note is not None
    assert "the next run removes the rest" in stopped.value.report.note
    assert sum(stray.exists() for stray in strays) == 2
    report = catalog.maintain(principal, ref, now=_later(30))
    assert not any(stray.exists() for stray in strays)
    assert report.files_deleted >= 2
    loaded = catalog.load_table(principal, ref)
    assert StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow().num_rows == 8
