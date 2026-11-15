"""Governed writes from Arrow batches: create, replace and append."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import ActionAllowList
from pyiceberg.manifest import DataFileContent
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable, TableProperties
from pyiceberg.table.snapshots import Operation
from pyiceberg.table.update import (
    RemoveSnapshotRefUpdate,
    RemoveSnapshotsUpdate,
    SetSnapshotRefUpdate,
)
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, AuthorizationService, PrincipalRef
from lakana_core.catalog import service as service_module
from lakana_core.catalog.schema_compat import SchemaMismatchError
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.writes import LineageSource, WriteResult, cleanup_location, live_data_files
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    UnsupportedTableError,
    ValidationError,
)
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    LineageEdge,
    Principal,
    PrincipalType,
    Table,
    WriteMode,
)
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver


def _catalog(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    authz: AuthorizationService | None = None,
) -> CatalogService:
    """Create `dev` at `tmp_path` (which creates `bronze`, `silver`, `gold`) and return a catalog.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The environment's storage root.
    authz
        Authorization for the returned catalog; allow-all when `None`.

    Returns
    -------
    CatalogService
        A catalog over the prepared environment.
    """
    setup = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    setup.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    return CatalogService(session, StorageResolver(session), authz or AllowAllAuthorization())


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


def _orders(ids: list[int], regions: list[str]) -> pa.Table:
    """Build an orders result as DuckDB would: every column nullable.

    Parameters
    ----------
    ids
        The `id` values.
    regions
        The `region` values.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table({"id": pa.array(ids, pa.int64()), "region": pa.array(regions, pa.string())})


def _batches(data: pa.Table) -> Iterator[pa.RecordBatch]:
    """Stream a table as small batches, the way the worker streams DuckDB's.

    Parameters
    ----------
    data
        The rows.

    Yields
    ------
    pa.RecordBatch
        Two rows at a time.
    """
    yield from data.to_batches(max_chunksize=2)


def _write(
    catalog: CatalogService,
    principal: PrincipalRef,
    dotted: str,
    mode: WriteMode,
    data: pa.Table,
    *,
    evolve_schema: bool = False,
    max_rows: int | None = None,
    sources: Sequence[LineageSource] = (),
    watermark: str | None = None,
) -> WriteResult:
    """Call `write_batches` with `data`'s schema and batches.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    dotted
        The target.
    mode
        The write mode.
    data
        The rows.
    evolve_schema
        Passed through.
    max_rows
        Passed through.
    sources
        Passed through.
    watermark
        Passed through.

    Returns
    -------
    WriteResult
        What was written.
    """
    return catalog.write_batches(
        principal,
        _ref(dotted),
        mode=mode,
        schema=data.schema,
        batches=_batches(data),
        evolve_schema=evolve_schema,
        max_rows=max_rows,
        sources=sources,
        watermark=watermark,
    )


def _rows(
    catalog: CatalogService, principal: PrincipalRef, dotted: str, snapshot_id: int | None = None
) -> list[dict[str, object]]:
    """Read a table (or one of its snapshots) back, sorted by `id`.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.
    snapshot_id
        A snapshot to time-travel to, or `None` for the current one.

    Returns
    -------
    list of dict
        The rows.
    """
    loaded = catalog.load_table(principal, _ref(dotted))
    scan = StaticTable.from_metadata(loaded.metadata_location).scan(snapshot_id=snapshot_id)
    return sorted(scan.to_arrow().to_pylist(), key=lambda row: row["id"])


def _files(tmp_path: Path) -> set[str]:
    """List every data and manifest file under the storage root, ignoring metadata JSON.

    Parameters
    ----------
    tmp_path
        The storage root.

    Returns
    -------
    set of str
        Root-relative paths.
    """
    return {
        str(path.relative_to(tmp_path))
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.name.endswith(".metadata.json")
    }


def test_create_publishes_the_table_with_its_first_snapshot(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`create` makes the table from the result's schema, holding exactly the result."""
    catalog = _catalog(session, principal, tmp_path)

    result = _write(
        catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1, 2, 3], ["eu", "us", "eu"])
    )

    loaded = catalog.load_table(principal, _ref("gold.daily"))
    assert result.rows == 3
    assert result.snapshot_id == loaded.metadata.current_snapshot_id
    assert result.table_id == loaded.uuid
    assert [(f.name, f.required) for f in loaded.metadata.schema().fields] == [
        ("id", False),
        ("region", False),
    ]
    assert _rows(catalog, principal, "gold.daily") == [
        {"id": 1, "region": "eu"},
        {"id": 2, "region": "us"},
        {"id": 3, "region": "eu"},
    ]


def test_create_with_zero_rows_creates_an_empty_table(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An empty result still creates the table, with no snapshot."""
    catalog = _catalog(session, principal, tmp_path)

    result = _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([], []))

    assert result.rows == 0
    assert result.snapshot_id is None
    assert catalog.load_table(principal, _ref("gold.daily")).metadata.current_snapshot_id is None


def test_create_refuses_an_existing_name_before_writing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A second `create` fails and leaves no files behind."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    before = _files(tmp_path)

    with pytest.raises(AlreadyExistsError):
        _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([2], ["us"]))

    assert _files(tmp_path) == before


def test_a_create_that_fails_to_publish_leaves_no_table_and_no_files(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A failed create is atomic — nothing is visible, nothing is left on disk."""
    catalog = _catalog(session, principal, tmp_path)

    with (
        mock.patch.object(
            CatalogService, "_publish_table", side_effect=ConflictError("lost the insert")
        ),
        pytest.raises(ConflictError),
    ):
        _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))

    assert session.execute(select(Table).where(Table.name == "daily")).first() is None
    assert [path for path in _files(tmp_path) if path.startswith("managed/")] == []
    assert (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_WRITE)
        ).first()
        is None
    )


def test_replace_swaps_all_rows_in_one_overwrite_snapshot_and_keeps_history(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Spec: `replace` is one snapshot; time travel still reads the old data."""
    catalog = _catalog(session, principal, tmp_path)
    first = _write(
        catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1, 2], ["eu", "us"])
    )

    second = _write(catalog, principal, "gold.daily", WriteMode.REPLACE, _orders([9], ["apac"]))

    metadata = catalog.load_table(principal, _ref("gold.daily")).metadata
    assert len(metadata.snapshots) == 2
    current = metadata.current_snapshot()
    assert current is not None
    assert current.snapshot_id == second.snapshot_id
    assert current.summary is not None
    assert current.summary.operation == Operation.OVERWRITE
    assert _rows(catalog, principal, "gold.daily") == [{"id": 9, "region": "apac"}]
    assert _rows(catalog, principal, "gold.daily", first.snapshot_id) == [
        {"id": 1, "region": "eu"},
        {"id": 2, "region": "us"},
    ]


def test_replace_with_zero_rows_empties_the_table_in_one_snapshot(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An empty full refresh leaves an empty table, not the old rows."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))

    result = _write(catalog, principal, "gold.daily", WriteMode.REPLACE, _orders([], []))

    assert result.snapshot_id is not None
    assert _rows(catalog, principal, "gold.daily") == []


def test_a_replace_whose_commit_fails_leaves_the_old_snapshot_and_no_stray_files(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Security: `replace` is atomic — a lost commit changes nothing readers can see."""
    catalog = _catalog(session, principal, tmp_path)
    created = _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    before = _files(tmp_path)
    audit_before = len(session.execute(select(AuditEvent)).scalars().all())

    with (
        mock.patch.object(
            service_module, "commit_metadata_pointer", side_effect=ConflictError("raced")
        ),
        pytest.raises(ConflictError),
    ):
        _write(
            catalog,
            principal,
            "gold.daily",
            WriteMode.REPLACE,
            _orders([7, 8], ["us", "us"]),
            sources=catalog.resolve_lineage_sources(principal, "dev", ["gold.daily"]),
        )

    loaded = catalog.load_table(principal, _ref("gold.daily"))
    assert loaded.metadata.current_snapshot_id == created.snapshot_id
    assert _rows(catalog, principal, "gold.daily") == [{"id": 1, "region": "eu"}]
    assert _files(tmp_path) == before
    assert len(session.execute(select(AuditEvent)).scalars().all()) == audit_before
    assert session.execute(select(LineageEdge)).first() is None


def test_append_adds_a_fast_append_snapshot(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`append` keeps the old rows and adds the new ones."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))

    result = _write(
        catalog, principal, "gold.daily", WriteMode.APPEND, _orders([2, 3], ["us", "us"])
    )

    assert result.rows == 2
    current = catalog.load_table(principal, _ref("gold.daily")).metadata.current_snapshot()
    assert current is not None
    assert current.summary is not None
    assert current.summary.operation == Operation.APPEND
    assert [row["id"] for row in _rows(catalog, principal, "gold.daily")] == [1, 2, 3]


def test_an_append_of_zero_rows_commits_nothing_but_is_audited(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """No empty snapshot, but the write still leaves an audit trail."""
    catalog = _catalog(session, principal, tmp_path)
    created = _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))

    result = _write(catalog, principal, "gold.daily", WriteMode.APPEND, _orders([], []))

    assert result.rows == 0
    assert result.snapshot_id is None
    metadata = catalog.load_table(principal, _ref("gold.daily")).metadata
    assert metadata.current_snapshot_id == created.snapshot_id
    events = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_WRITE)
    ).scalars()
    assert [event.detail["rows"] for event in events] == [1, 0]


def test_an_incompatible_result_fails_before_any_file_is_written(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Without `evolve_schema`, a dropped column fails with the diff and writes nothing."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    before = _files(tmp_path)

    with pytest.raises(SchemaMismatchError, match="dropped region"):
        _write(
            catalog,
            principal,
            "gold.daily",
            WriteMode.REPLACE,
            pa.table({"id": pa.array([5], pa.int64())}),
        )

    assert _files(tmp_path) == before


def test_evolve_schema_commits_the_new_schema_with_the_data(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """With `evolve_schema` the table takes the result schema in the same commit."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))

    result = _write(
        catalog,
        principal,
        "gold.daily",
        WriteMode.REPLACE,
        pa.table({"id": pa.array([5], pa.int64()), "amount": pa.array([2.5])}),
        evolve_schema=True,
    )

    metadata = catalog.load_table(principal, _ref("gold.daily")).metadata
    assert [f.name for f in metadata.schema().fields] == ["id", "amount"]
    assert result.schema_diff.to_json()["renamed"] == []
    assert result.schema_diff.to_json()["added"] == [{"name": "amount", "type": "double"}]
    assert _rows(catalog, principal, "gold.daily") == [{"id": 5, "amount": 2.5}]


def test_an_extra_nullable_column_is_added_without_evolve_and_reported(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Spec: extra nullable result columns are compatible, and are added."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    wider = _orders([2], ["us"]).append_column("n", pa.array([4], pa.int64()))

    result = _write(catalog, principal, "gold.daily", WriteMode.APPEND, wider)

    assert result.schema_diff.to_json()["added"] == [{"name": "n", "type": "long"}]
    assert _rows(catalog, principal, "gold.daily") == [
        {"id": 1, "region": "eu", "n": None},
        {"id": 2, "region": "us", "n": 4},
    ]


def test_max_rows_fails_the_write_and_removes_every_file_it_wrote(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Spec: exceeding `max_write_rows` fails the run and cleans up."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    before = _files(tmp_path)

    with pytest.raises(ValidationError, match="max_write_rows"):
        _write(
            catalog,
            principal,
            "gold.daily",
            WriteMode.APPEND,
            _orders([1, 2, 3, 4, 5], ["a", "b", "c", "d", "e"]),
            max_rows=3,
        )

    assert _files(tmp_path) == before


def test_a_null_in_a_required_column_fails_the_write(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A nullable result may feed a required column only if it holds no nulls."""
    catalog = _catalog(session, principal, tmp_path)
    catalog.create_table(
        principal,
        _ref("gold.strict"),
        schema=Schema(NestedField(1, "id", LongType(), required=True)),
    )
    before = _files(tmp_path)

    with pytest.raises(ValidationError, match="'id' contains a null"):
        _write(
            catalog,
            principal,
            "gold.strict",
            WriteMode.APPEND,
            pa.table({"id": pa.array([1, None], pa.int64())}),
        )

    assert _files(tmp_path) == before


def test_a_partitioned_target_is_refused(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Partitioned writes are a non-goal: refused, never written wrong."""
    catalog = _catalog(session, principal, tmp_path)
    catalog.create_table(
        principal,
        _ref("gold.parts"),
        schema=Schema(
            NestedField(1, "id", LongType(), required=False),
            NestedField(2, "region", StringType(), required=False),
        ),
        partition_spec=PartitionSpec(
            PartitionField(source_id=2, field_id=1000, transform=IdentityTransform(), name="region")
        ),
    )

    with pytest.raises(ValidationError, match="partitioned"):
        _write(catalog, principal, "gold.parts", WriteMode.APPEND, _orders([1], ["eu"]))


@pytest.mark.parametrize(
    ("mode", "needed"),
    [
        (WriteMode.CREATE, Action.CREATE_TABLE),
        (WriteMode.REPLACE, Action.WRITE_TABLE),
        (WriteMode.APPEND, Action.WRITE_TABLE),
    ],
)
def test_every_mode_authorizes_its_own_action_before_writing(
    session: Session, principal: PrincipalRef, tmp_path: Path, mode: WriteMode, needed: Action
) -> None:
    """Security: `create` needs CREATE_TABLE, `replace`/`append` need WRITE_TABLE."""
    setup = _catalog(session, principal, tmp_path)
    if mode is not WriteMode.CREATE:
        _write(setup, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    before = _files(tmp_path)
    everything_else = [action for action in Action if action is not needed]
    denied = CatalogService(session, StorageResolver(session), ActionAllowList(*everything_else))

    with pytest.raises(PermissionDenied, match=needed.value):
        _write(denied, principal, "gold.daily", mode, _orders([2], ["us"]))

    assert _files(tmp_path) == before
    allowed = CatalogService(session, StorageResolver(session), ActionAllowList(needed))
    _write(allowed, principal, "gold.daily", mode, _orders([2], ["us"]))


def test_a_write_is_audited_and_records_one_edge_per_source(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`TABLE_WRITE` carries the spec's fields; edges carry both snapshot ids."""
    catalog = _catalog(session, principal, tmp_path)
    source = _write(catalog, principal, "bronze.orders", WriteMode.CREATE, _orders([1], ["eu"]))
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    sources = catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"])

    result = _write(
        catalog,
        principal,
        "gold.daily",
        WriteMode.APPEND,
        _orders([2], ["us"]),
        sources=sources,
        watermark="long:1",
    )

    events = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_WRITE)
    ).scalars()
    event = next(event for event in events if event.detail["mode"] == "append")
    assert event.principal_id == principal.id
    assert event.securable_table == "daily"
    assert event.detail == {
        "mode": "append",
        "target": "gold.daily",
        "snapshot_id": result.snapshot_id,
        "rows": 1,
        "files": 1,
        "watermark": "long:1",
        "schema_diff": None,
        "run_id": None,
        "principal": principal.name,
    }
    edge = session.execute(select(LineageEdge)).scalar_one()
    assert (edge.source_table_id, edge.source_name) == (source.table_id, "bronze.orders")
    assert edge.source_snapshot_id == source.snapshot_id
    assert (edge.target_table_id, edge.target_name) == (result.table_id, "gold.daily")
    assert edge.target_snapshot_id == result.snapshot_id
    assert edge.query_run_id is None


def test_resolve_lineage_sources_requires_read_and_existence(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Every declared source must exist and be READ-able."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "bronze.orders", WriteMode.CREATE, _orders([1], ["eu"]))
    no_read = CatalogService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))

    with pytest.raises(NotFoundError):
        catalog.resolve_lineage_sources(principal, "dev", ["bronze.missing"])
    with pytest.raises(PermissionDenied):
        no_read.resolve_lineage_sources(principal, "dev", ["bronze.orders"])
    resolved = catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders", "bronze.orders"])
    assert [source.name for source in resolved] == ["bronze.orders"]


def test_commit_table_authorizes_the_action_it_is_given(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Maintenance commits under MAINTAIN_TABLE, never needing WRITE_TABLE."""
    setup = _catalog(session, principal, tmp_path)
    _write(setup, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    maintainer = CatalogService(
        session, StorageResolver(session), ActionAllowList(Action.MAINTAIN_TABLE)
    )

    maintainer.commit_table(
        principal, _ref("gold.daily"), requirements=[], updates=[], action=Action.MAINTAIN_TABLE
    )
    with pytest.raises(PermissionDenied):
        maintainer.commit_table(principal, _ref("gold.daily"), requirements=[], updates=[])
    with pytest.raises(ValueError, match="READ_TABLE"):
        setup.commit_table(
            principal, _ref("gold.daily"), requirements=[], updates=[], action=Action.READ_TABLE
        )


def test_write_batches_refuses_a_non_data_mode(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`view` and `maintain` are not data writes."""
    catalog = _catalog(session, principal, tmp_path)

    with pytest.raises(ValidationError, match="not a data write"):
        _write(catalog, principal, "gold.v", WriteMode.VIEW, _orders([1], ["eu"]))


# The tests below close specific branch-coverage gaps the other tests leave in `writes.py`
# and `service.py` (a non-Parquet target, a required column that holds no
# null, replacing a snapshot-less table, an external source/target, and the
# storage-failure paths `cleanup_location` and `_rollback_quietly` swallow),
# needed to hold the coverage ratchet at its 100% baseline (CLAUDE.md rule
# 5), following the same failure-injection style already used in
# `test_append.py`.


def _external_orders(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, name: str
) -> TableRef:
    """Register a tiny external (raw-zone) table, for tests needing a non-managed table.

    Parameters
    ----------
    catalog
        The catalog to register under.
    principal
        The acting principal.
    tmp_path
        The environment's storage root.
    name
        The table's name, also its raw subdirectory.

    Returns
    -------
    TableRef
        The registered table, addressable as ``f"raw.{name}"``.
    """
    with contextlib.suppress(AlreadyExistsError):
        catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / name).mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / name / "part-0.parquet")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name=name)
    catalog.create_external_table(
        principal, ref, location_pattern=f"raw/{name}/*.parquet", format="parquet"
    )
    return ref


def test_writing_into_a_non_parquet_table_is_refused(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`DataFileWriter` refuses a table whose write format isn't Parquet."""
    catalog = _catalog(session, principal, tmp_path)
    catalog.create_table(
        principal,
        _ref("gold.avro"),
        schema=Schema(NestedField(1, "id", LongType(), required=False)),
        properties={TableProperties.WRITE_FILE_FORMAT: "avro"},
    )

    with pytest.raises(ValidationError, match="Parquet"):
        _write(
            catalog,
            principal,
            "gold.avro",
            WriteMode.APPEND,
            pa.table({"id": pa.array([1], pa.int64())}),
        )


def test_appending_a_required_column_with_no_null_succeeds(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A batch that satisfies a required column commits normally (the non-failing path)."""
    catalog = _catalog(session, principal, tmp_path)
    catalog.create_table(
        principal,
        _ref("gold.strict2"),
        schema=Schema(NestedField(1, "id", LongType(), required=True)),
    )

    result = _write(
        catalog,
        principal,
        "gold.strict2",
        WriteMode.APPEND,
        pa.table({"id": pa.array([1, 2], pa.int64())}),
    )

    assert result.rows == 2


def test_replacing_a_freshly_created_empty_table_produces_one_snapshot(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`replace` on a table with no current snapshot still stages one overwrite snapshot."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([], []))

    result = _write(catalog, principal, "gold.daily", WriteMode.REPLACE, _orders([1], ["eu"]))

    assert result.snapshot_id is not None
    assert _rows(catalog, principal, "gold.daily") == [{"id": 1, "region": "eu"}]


def test_resolve_lineage_sources_of_an_external_table_has_no_snapshot(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An external table resolves as a lineage source with no snapshot id."""
    catalog = _catalog(session, principal, tmp_path)
    _external_orders(catalog, principal, tmp_path, "orders")

    sources = catalog.resolve_lineage_sources(principal, "dev", ["raw.orders"])

    assert [(source.name, source.snapshot_id) for source in sources] == [("raw.orders", None)]


def test_write_batches_refuses_an_external_target(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`replace`/`append` refuse a non-managed (external) target by name."""
    catalog = _catalog(session, principal, tmp_path)
    _external_orders(catalog, principal, tmp_path, "orders")

    with pytest.raises(ValidationError, match="only managed tables can be written"):
        _write(catalog, principal, "raw.orders", WriteMode.APPEND, _orders([1], ["eu"]))


def test_a_failed_replace_with_a_failing_rollback_still_raises_the_original_error(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A rollback failure after a lost commit race is logged, not raised (mirrors test_append)."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))

    with (
        mock.patch.object(
            service_module, "commit_metadata_pointer", side_effect=ConflictError("raced")
        ),
        mock.patch.object(session, "rollback", side_effect=RuntimeError("connection lost")),
        pytest.raises(ConflictError),
    ):
        _write(catalog, principal, "gold.daily", WriteMode.REPLACE, _orders([9], ["apac"]))


def test_cleanup_location_ignores_a_location_outside_the_storage_root(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A location outside the environment's root is left untouched."""
    _catalog(session, principal, tmp_path)
    resolved = StorageResolver(session).for_environment("dev")

    with mock.patch.object(FsspecStorage, "list", side_effect=AssertionError("should not run")):
        cleanup_location(resolved, "file:///elsewhere/table-abc")


def test_cleanup_location_swallows_a_listing_failure(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A failure listing a failed create's files is logged, not raised."""
    _catalog(session, principal, tmp_path)
    resolved = StorageResolver(session).for_environment("dev")

    with mock.patch.object(FsspecStorage, "list", side_effect=RuntimeError("storage outage")):
        cleanup_location(resolved, f"file://{tmp_path}/managed/gold/daily-abc")


def test_cleanup_location_swallows_a_delete_failure(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A failure deleting one of a failed create's files is logged, not raised."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    loaded = catalog.load_table(principal, _ref("gold.daily"))
    resolved = StorageResolver(session).for_environment("dev")

    with mock.patch.object(FsspecStorage, "delete", side_effect=RuntimeError("disk on fire")):
        cleanup_location(resolved, loaded.metadata.location)


def test_live_data_files_refuses_a_table_with_delete_files() -> None:
    """A row-level delete file in the current snapshot is refused, not silently dropped."""
    fake_entry = mock.Mock()
    fake_entry.data_file.content = DataFileContent.POSITION_DELETES
    fake_manifest = mock.Mock()
    fake_manifest.fetch_manifest_entry.return_value = [fake_entry]
    fake_snapshot = mock.Mock()
    fake_snapshot.manifests.return_value = [fake_manifest]
    fake_metadata = mock.Mock()
    fake_metadata.current_snapshot.return_value = fake_snapshot

    with pytest.raises(UnsupportedTableError, match="row-level delete"):
        live_data_files(fake_metadata, mock.Mock())


# A stream that contains an
# explicit zero-row `RecordBatch` (as a real Arrow client can send — DuckDB's
# own `to_arrow_reader` never yields one, but `pa.RecordBatch.from_pylist([],
# schema)` does) used to still be written as an empty Parquet file, breaking
# the rule for `create` and leaking an orphan file on `append`.


def test_create_with_an_explicit_empty_batch_creates_an_empty_table_and_writes_no_files(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An explicit zero-row batch must not itself produce a data file."""
    catalog = _catalog(session, principal, tmp_path)
    before = _files(tmp_path)
    schema = _orders([], []).schema
    empty = pa.RecordBatch.from_pylist([], schema=schema)

    result = catalog.write_batches(
        principal, _ref("gold.daily"), mode=WriteMode.CREATE, schema=schema, batches=iter([empty])
    )

    assert result.rows == 0
    assert result.snapshot_id is None
    assert catalog.load_table(principal, _ref("gold.daily")).metadata.current_snapshot_id is None
    assert _files(tmp_path) == before


def test_append_with_an_explicit_empty_batch_commits_nothing_and_writes_no_files(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An append that is entirely one explicit empty batch commits nothing on disk."""
    catalog = _catalog(session, principal, tmp_path)
    created = _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    before = _files(tmp_path)
    schema = _orders([], []).schema
    empty = pa.RecordBatch.from_pylist([], schema=schema)

    result = catalog.write_batches(
        principal, _ref("gold.daily"), mode=WriteMode.APPEND, schema=schema, batches=iter([empty])
    )

    assert result.rows == 0
    assert result.snapshot_id is None
    metadata = catalog.load_table(principal, _ref("gold.daily")).metadata
    assert metadata.current_snapshot_id == created.snapshot_id
    assert _files(tmp_path) == before


def test_append_skips_an_explicit_empty_batch_mixed_with_real_rows(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An empty batch mixed into a real append's stream contributes no file of its own."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    schema = _orders([], []).schema
    empty = pa.RecordBatch.from_pylist([], schema=schema)
    real = pa.RecordBatch.from_pylist([{"id": 2, "region": "us"}], schema=schema)

    result = catalog.write_batches(
        principal,
        _ref("gold.daily"),
        mode=WriteMode.APPEND,
        schema=schema,
        batches=iter([empty, real]),
    )

    assert result.rows == 1
    assert result.files == 1
    assert [row["id"] for row in _rows(catalog, principal, "gold.daily")] == [1, 2]


def test_replace_with_an_explicit_empty_batch_produces_one_snapshot_and_writes_no_data_file(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Replace with an explicit empty batch still empties the table in one snapshot.

    The empty batch itself must not add a data file — the pre-existing file from the earlier
    create is untouched (an overwrite commit records a file as no longer live; it does not
    physically delete it).
    """
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1, 2], ["eu", "us"]))
    loaded = catalog.load_table(principal, _ref("gold.daily"))
    data_dir = Path(loaded.metadata.location.removeprefix("file://")) / "data"
    before = set(data_dir.glob("*.parquet"))
    schema = _orders([], []).schema
    empty = pa.RecordBatch.from_pylist([], schema=schema)

    result = catalog.write_batches(
        principal, _ref("gold.daily"), mode=WriteMode.REPLACE, schema=schema, batches=iter([empty])
    )

    assert result.rows == 0
    assert result.snapshot_id is not None
    assert _rows(catalog, principal, "gold.daily") == []
    assert set(data_dir.glob("*.parquet")) == before


def test_check_can_replace_refuses_an_external_target(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A replace is refused before any data is read when the target is not a managed table."""
    catalog = _catalog(session, principal, tmp_path)
    _external_orders(catalog, principal, tmp_path, "orders")

    with pytest.raises(ValidationError, match="only supported on managed"):
        catalog.check_can_replace(principal, TableRef.from_dotted("dev", "raw.orders"))


def _last_note(spy: mock.MagicMock) -> tuple[object, object, object]:
    """Return `snapshot_before`, `publishes` and `snapshot_id` of the last `note_result` call.

    Parameters
    ----------
    spy
        The spy on the guard's `note_result`.

    Returns
    -------
    tuple
        The three keyword values.
    """
    kwargs = spy.call_args.kwargs
    return kwargs["snapshot_before"], kwargs["publishes"], kwargs["snapshot_id"]


def test_a_write_notes_what_the_table_was_before(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    guard = catalog._guard
    spy = mock.patch.object(guard, "note_result", wraps=guard.note_result)
    with spy as noted:
        _noted_writes(session, principal, catalog, noted)


def _noted_writes(
    session: Session, principal: PrincipalRef, catalog: CatalogService, noted: mock.MagicMock
) -> None:
    """Make the writes of `test_a_write_notes_what_the_table_was_before` and check each note.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    catalog
        The catalog whose guard `noted` spies on.
    noted
        The spy on `note_result`.
    """
    created = _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    assert _last_note(noted) == (None, True, created.snapshot_id)

    replaced = _write(catalog, principal, "gold.daily", WriteMode.REPLACE, _orders([2], ["eu"]))
    assert _last_note(noted) == (created.snapshot_id, False, replaced.snapshot_id)

    _write(catalog, principal, "gold.daily", WriteMode.APPEND, _orders([], []))
    assert _last_note(noted) == (replaced.snapshot_id, False, None)

    _write(catalog, principal, "gold.empty", WriteMode.CREATE, _orders([], []))
    row = session.scalars(select(Table).where(Table.name == "empty")).one()
    row.unpublished_run_id = uuid.uuid4()
    session.commit()
    _write(catalog, principal, "gold.empty", WriteMode.REPLACE, _orders([3], ["us"]))
    assert _last_note(noted)[:2] == (None, True)


@pytest.fixture
def two_sessions(tmp_path: Path) -> Iterator[tuple[Session, Session, PrincipalRef]]:
    """Provide a writer and a second connection on one SQLite file, and a persisted principal.

    Parameters
    ----------
    tmp_path
        Holds the database file.

    Yields
    ------
    tuple of (Session, Session, PrincipalRef)
        The writer's session, the observer's session and the principal.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'rules.db'}"))
    Base.metadata.create_all(engine)
    with Session(engine) as writer, Session(engine) as observer:
        ref = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
        writer.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
        writer.commit()
        yield writer, observer, ref
    engine.dispose()


def _mark_unpublished(session: Session, name: str) -> None:
    """Set the not-published marker of a table by hand.

    Parameters
    ----------
    session
        Database session; commits.
    name
        The table's name.
    """
    row = session.scalars(select(Table).where(Table.name == name)).one()
    row.unpublished_run_id = uuid.uuid4()
    session.commit()


def _stored(session: Session, name: str) -> tuple[uuid.UUID | None, str | None]:
    """Read a table's marker and metadata location afresh.

    Parameters
    ----------
    session
        Database session, typically a second connection.
    name
        The table's name.

    Returns
    -------
    tuple
        `unpublished_run_id` and `metadata_location`.
    """
    session.rollback()
    row = session.execute(
        select(Table.unpublished_run_id, Table.metadata_location).where(Table.name == name)
    ).one()
    return row[0], row[1]


_NO_REMOVAL = (
    "A commit cannot remove a table's current snapshot: readers would see an empty table. "
    "Write the rows it should hold, or start gold.daily again: `lakana table rebuild gold.daily "
    '--env dev --reason "…"` if a deployed project builds it, otherwise `lakana table delete '
    "gold.daily --env dev`."
)


def test_a_commit_cannot_remove_the_current_snapshot(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    _write(catalog, principal, "gold.hollow", WriteMode.CREATE, _orders([], []))
    before = catalog.load_table(principal, _ref("gold.daily"))

    with pytest.raises(ValidationError) as caught:
        catalog.commit_table(
            principal,
            _ref("gold.daily"),
            requirements=[],
            updates=[RemoveSnapshotRefUpdate(ref_name="main")],
        )

    assert str(caught.value) == _NO_REMOVAL
    after = catalog.load_table(principal, _ref("gold.daily"))
    assert after.metadata_location == before.metadata_location
    assert after.metadata.current_snapshot_id == before.metadata.current_snapshot_id
    # A table that has no current snapshot may have the reference removed: nothing is lost.
    catalog.commit_table(
        principal,
        _ref("gold.hollow"),
        requirements=[],
        updates=[RemoveSnapshotRefUpdate(ref_name="main")],
    )


def test_a_commit_that_gives_a_snapshot_publishes(
    two_sessions: tuple[Session, Session, PrincipalRef], tmp_path: Path
) -> None:
    writer, observer, principal = two_sessions
    catalog = _catalog(writer, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([], []))
    _mark_unpublished(writer, "daily")
    marker, location = _stored(observer, "daily")
    assert marker is not None

    _write(catalog, principal, "gold.daily", WriteMode.APPEND, _orders([1], ["eu"]))

    new_marker, new_location = _stored(observer, "daily")
    assert new_marker is None
    assert new_location != location


def _failed_first_build(catalog: CatalogService, principal: PrincipalRef, session: Session) -> int:
    """Leave `gold.daily` as a rolled-back first build: a tag on the snapshot, no `main`, a marker.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    session
        Database session.

    Returns
    -------
    int
        The tagged snapshot's id.
    """
    created = _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    assert created.snapshot_id is not None
    catalog.commit_table(
        principal,
        _ref("gold.daily"),
        requirements=[],
        updates=[
            SetSnapshotRefUpdate(
                ref_name="lakana-check-failed-x", type="tag", snapshot_id=created.snapshot_id
            ),
            RemoveSnapshotRefUpdate(ref_name="main"),
        ],
        internal_rollback=True,
    )
    _mark_unpublished(session, "daily")
    return created.snapshot_id


def test_an_iceberg_commit_cannot_publish_by_pointing_at_the_failed_snapshot(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    failed = _failed_first_build(catalog, principal, session)
    marker, location = _stored(session, "daily")

    with pytest.raises(ValidationError) as caught:
        catalog.commit_table(
            principal,
            _ref("gold.daily"),
            requirements=[],
            updates=[SetSnapshotRefUpdate(ref_name="main", type="branch", snapshot_id=failed)],
        )

    assert str(caught.value) == (
        "gold.daily is not published: its first build did not finish, or failed a check. A "
        "commit can publish it only with a new snapshot, not by pointing at an existing one. "
        "Run its pipeline again."
    )
    assert _stored(session, "daily") == (marker, location)
    assert catalog.describe_table(principal, _ref("gold.daily")).snapshot_id is None


def test_a_maintenance_commit_does_not_publish(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    _mark_unpublished(session, "daily")  # a marker on a table that has a snapshot
    marker, _ = _stored(session, "daily")

    catalog.commit_table(
        principal,
        _ref("gold.daily"),
        requirements=[],
        updates=[RemoveSnapshotsUpdate(snapshot_ids=[])],
        action=Action.MAINTAIN_TABLE,
    )

    assert _stored(session, "daily")[0] == marker


def test_a_maintenance_commit_cannot_remove_the_current_snapshot_either(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    created = _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))
    assert created.snapshot_id is not None
    location = catalog.load_table(principal, _ref("gold.daily")).metadata_location

    with pytest.raises(ValidationError) as caught:
        catalog.commit_table(
            principal,
            _ref("gold.daily"),
            requirements=[],
            updates=[RemoveSnapshotsUpdate(snapshot_ids=[created.snapshot_id])],
            action=Action.MAINTAIN_TABLE,
        )

    assert str(caught.value) == _NO_REMOVAL
    assert catalog.load_table(principal, _ref("gold.daily")).metadata_location == location
