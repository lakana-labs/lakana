"""The rollback of a table whose blocking check failed."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList, python_task_run
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.update import (
    AddSchemaUpdate,
    AssertRefSnapshotId,
    AssertTableUUID,
    RemoveSnapshotsUpdate,
    SetCurrentSchemaUpdate,
    SetPropertiesUpdate,
)
from pyiceberg.types import NestedField, StringType
from sqlalchemy import event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import InvalidRequestError, OperationalError
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, RunContext, Securable
from lakana_core.catalog import service as service_module
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService, Rollback, failed_snapshot_tag
from lakana_core.catalog.writes import WriteResult
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import LakanaError, PermissionDenied
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    CheckOutcome,
    CheckResult,
    Environment,
    QueryRun,
    QueryRunStatus,
    RollbackOutcome,
    Run,
    Table,
    TaskRun,
    WriteMode,
)
from lakana_core.models.audit_event import SecurableKind
from lakana_core.runs.checks import failure_message
from lakana_core.runs.kept import kept_message
from lakana_core.runs.lockdown import OTHER_ASSET_MESSAGE, SUPERSEDED_MESSAGE
from lakana_core.runs.tasks import Written, read_written
from lakana_core.storage.resolver import StorageResolver

_TABLE = "gold.orders"
_FAILED = "gold.orders/unique_id"
_KEEP_MS = 7 * 24 * 60 * 60 * 1000


@pytest.fixture
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    """Provide a SQLite file engine, so that a second connection sees only what is committed.

    Parameters
    ----------
    tmp_path_factory
        Makes the directory of the database file (apart from the storage root).

    Yields
    ------
    Engine
        The engine, with the schema created.
    """
    built = create_engine(
        DatabaseConfig(url=f"sqlite:///{tmp_path_factory.mktemp('meta') / 'catalog.db'}")
    )
    Base.metadata.create_all(built)
    yield built
    built.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Provide the session under test (this module's own, on the file engine).

    Parameters
    ----------
    engine
        The file engine.

    Yields
    ------
    Session
        A session.
    """
    with Session(engine) as s:
        yield s


@pytest.fixture
def second_session(engine: Engine) -> Iterator[Session]:
    """Provide a second connection to the same database.

    Parameters
    ----------
    engine
        The file engine.

    Yields
    ------
    Session
        A session on its own connection.
    """
    with Session(engine) as s:
        yield s


def _catalog(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    authz: AllowAllAuthorization | ActionAllowList | None = None,
) -> CatalogService:
    """Create `dev` at `tmp_path` and return a catalog over it.

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


def _ref(dotted: str = _TABLE) -> TableRef:
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


def _write(
    catalog: CatalogService,
    principal: PrincipalRef,
    rows: pa.Table,
    mode: WriteMode,
    *,
    dotted: str = _TABLE,
    evolve_schema: bool = False,
) -> WriteResult:
    """Write `rows` into a table and return the write's result.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    rows
        The rows.
    mode
        The write mode.
    dotted
        The target.
    evolve_schema
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
        schema=rows.schema,
        batches=rows.to_batches(),
        evolve_schema=evolve_schema,
    )


def _task(session: Session, principal: PrincipalRef, asset: str = _TABLE) -> TaskRun:
    """Insert a running SQL task of an asset, with checks.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The run's principal.
    asset
        The task's asset.

    Returns
    -------
    TaskRun
        The task run.
    """
    task = python_task_run(session, principal.id, asset=asset, kind="sql")
    task.has_checks = True
    session.commit()
    return task


def _stamp(session: Session, task: TaskRun, written: Written) -> None:
    """Stamp a task run *written*, as its write's commit does.

    Parameters
    ----------
    session
        Database session; commits.
    task
        The task run.
    written
        What its write did.
    """
    session.execute(
        update(TaskRun)
        .where(TaskRun.id == task.id)
        .values(
            written_table_id=written.table_id,
            written_snapshot_before=written.snapshot_before,
            written_snapshot_after=written.snapshot_after,
            written_publishes=written.publishes,
        )
    )
    session.commit()


def _roll_back(
    catalog: CatalogService,
    principal: PrincipalRef,
    task: TaskRun,
    written: Written,
    ref: TableRef | None = None,
) -> Rollback:
    """Call `roll_back` for a task with one failed check.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    task
        The task run.
    written
        What its write did.
    ref
        The table; `gold.orders` when `None`.

    Returns
    -------
    Rollback
        The result.
    """
    return catalog.roll_back(
        principal,
        ref or _ref(),
        run_id=task.run_id,
        task_run_id=task.id,
        written=written,
        failed_checks=[_FAILED],
    )


def _ids(catalog: CatalogService, principal: PrincipalRef) -> list[int]:
    """Read the `id` column of the table's current snapshot, sorted.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    list of int
        The ids, `[]` while the table has no current snapshot.
    """
    loaded = catalog.load_table(principal, _ref())
    if loaded.metadata.current_snapshot_id is None:
        return []
    rows = StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow()
    return sorted(rows.column("id").to_pylist())


def _row(session: Session, name: str = "orders") -> Table:
    """Read a table row afresh.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name.

    Returns
    -------
    Table
        The row, never a cached one.
    """
    session.rollback()
    session.expire_all()
    return session.scalars(select(Table).where(Table.name == name)).one()


def _stored_metadata(session: Session) -> TableMetadata:
    """Read the metadata file the table's row points at, without going through a reader.

    A table that is not published refuses `load_table`; its metadata is read from the file.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    TableMetadata
        The current metadata.
    """
    location = _row(session).metadata_location
    assert location is not None
    return StaticTable.from_metadata(location).metadata


def _events(session: Session) -> list[AuditEvent]:
    """Return the rollback audit events, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.TABLE_ROLLED_BACK)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )


def test_a_replaced_table_goes_back_to_the_snapshot_before(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1, 2], ["eu", "us"]), WriteMode.CREATE).snapshot_id
    after = _write(catalog, principal, _orders([3], ["eu"]), WriteMode.REPLACE).snapshot_id
    task = _task(session, principal)
    table_id = catalog.load_table(principal, _ref()).uuid
    written = Written(table_id, before, after, publishes=False)
    _stamp(session, task, written)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.ROLLED_BACK
    loaded = catalog.load_table(principal, _ref())
    assert loaded.metadata.current_snapshot_id == before
    assert _ids(catalog, principal) == [1, 2]
    tag = loaded.metadata.refs[failed_snapshot_tag(task.run_id)]
    assert (tag.snapshot_id, str(tag.snapshot_ref_type), tag.max_ref_age_ms) == (
        after,
        "tag",
        _KEEP_MS,
    )
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_ROLLED_BACK)
    ).one()
    assert event.detail == {
        "run_id": str(task.run_id),
        "table": _TABLE,
        "before": before,
        "after": after,
        "outcome": "rolled_back",
        "checks": [_FAILED],
        "schema_before": 0,
        "schema_after": 0,
        "task_run_id": str(task.id),
        "claim": 1,
    }
    assert read_written(session, task.id) is None
    assert _row(session).unpublished_run_id is None


def test_a_second_writers_commit_is_never_discarded(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The requirement on the current snapshot is the whole protection."""
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    after = _write(catalog, principal, _orders([2], ["eu"]), WriteMode.APPEND).snapshot_id
    other = _write(catalog, principal, _orders([3], ["us"]), WriteMode.APPEND).snapshot_id
    task = _task(session, principal)
    table_id = catalog.load_table(principal, _ref()).uuid
    written = Written(table_id, before, after, publishes=False)
    _stamp(session, task, written)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.CHANGED_SINCE
    loaded = catalog.load_table(principal, _ref())
    assert loaded.metadata.current_snapshot_id == other
    assert _ids(catalog, principal) == [1, 2, 3]
    assert failed_snapshot_tag(task.run_id) not in loaded.metadata.refs
    assert read_written(session, task.id) == written


def test_a_first_build_is_kept_and_not_published(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    after = _write(catalog, principal, _orders([1, 1], ["eu", "eu"]), WriteMode.CREATE).snapshot_id
    task = _task(session, principal)
    row = _row(session)
    written = Written(row.id, None, after, publishes=True)
    _stamp(session, task, written)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.UNPUBLISHED
    stored = _row(session)
    assert stored.unpublished_run_id == task.run_id
    described = catalog.describe_table(principal, _ref())  # shows the state, does not refuse
    assert described.snapshot_id is None
    assert described.state == "not_published"
    metadata = _stored_metadata(session)
    assert metadata.refs[failed_snapshot_tag(task.run_id)].snapshot_id == after
    assert read_written(session, task.id) is None  # the build runs again on the next claim


def test_a_table_that_is_not_published_loses_its_old_tag_and_stays_not_published(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    after = _write(catalog, principal, _orders([1, 1], ["eu", "eu"]), WriteMode.CREATE).snapshot_id
    task = _task(session, principal)
    written = Written(_row(session).id, None, after, publishes=True)
    _stamp(session, task, written)
    _roll_back(catalog, principal, task, written)
    tag = failed_snapshot_tag(task.run_id)
    assert tag in _stored_metadata(session).refs
    location = Path(_stored_metadata(session).location.removeprefix("file://"))
    failed_files = set(location.glob("data/*.parquet"))
    assert failed_files

    young = catalog.maintain(principal, _ref(), now=datetime.now(UTC) + timedelta(days=6))
    old = catalog.maintain(principal, _ref(), now=datetime.now(UTC) + timedelta(days=8))

    assert young.tags_removed == ()
    assert old.tags_removed == (tag,)
    assert old.snapshots_expired == (after,)
    assert old.conflict is False
    metadata = _stored_metadata(session)
    assert tag not in metadata.refs
    assert metadata.current_snapshot_id is None
    assert metadata.snapshots == []
    assert not any(path.exists() for path in failed_files)
    assert _row(session).unpublished_run_id == task.run_id
    assert catalog.describe_table(principal, _ref()).state == "not_published"


def test_a_first_build_after_the_failed_snapshot_was_removed_publishes_the_table(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    after = _write(catalog, principal, _orders([1, 1], ["eu", "eu"]), WriteMode.CREATE).snapshot_id
    task = _task(session, principal)
    written = Written(_row(session).id, None, after, publishes=True)
    _stamp(session, task, written)
    _roll_back(catalog, principal, task, written)
    catalog.maintain(principal, _ref(), now=datetime.now(UTC) + timedelta(days=8))
    assert _stored_metadata(session).snapshots == []

    built = _write(catalog, principal, _orders([2, 3], ["eu", "us"]), WriteMode.REPLACE)

    assert _row(session).unpublished_run_id is None
    loaded = catalog.load_table(principal, _ref())
    assert loaded.metadata.current_snapshot_id == built.snapshot_id
    assert _ids(catalog, principal) == [2, 3]


def test_a_write_that_evolved_the_schema_takes_the_schema_back(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    wider = pa.table(
        {
            "id": pa.array([2], pa.int64()),
            "region": pa.array(["us"], pa.string()),
            "note": pa.array(["x"], pa.string()),
        }
    )
    after = _write(catalog, principal, wider, WriteMode.APPEND, evolve_schema=True).snapshot_id
    assert [f.name for f in catalog.load_table(principal, _ref()).metadata.schema().fields] == [
        "id",
        "region",
        "note",
    ]
    task = _task(session, principal)
    written = Written(_row(session).id, before, after, publishes=False)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.ROLLED_BACK
    loaded = catalog.load_table(principal, _ref())
    assert [f.name for f in loaded.metadata.schema().fields] == ["id", "region"]
    detail = _events(session)[0].detail
    assert detail["schema_before"] != detail["schema_after"]


def test_a_set_back_that_takes_the_schema_back_drops_the_tasks_column_change(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The note, the warnings and the notification line read it: none may outlive the change."""
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    narrower = pa.table({"id": pa.array([2], pa.int64())})
    after = _write(catalog, principal, narrower, WriteMode.REPLACE, evolve_schema=True).snapshot_id
    changed = {"tables": [_TABLE], "policies": []}
    task = _task(session, principal)
    other = _task(session, principal)
    session.execute(
        update(TaskRun)
        .where(TaskRun.id.in_([task.id, other.id]))
        .values(schema_change_json=changed)
    )
    session.commit()

    result = _roll_back(catalog, principal, task, Written(_row(session).id, before, after, False))

    assert result.outcome is RollbackOutcome.ROLLED_BACK
    loaded = catalog.load_table(principal, _ref())
    assert [f.name for f in loaded.metadata.schema().fields] == ["id", "region"]
    session.expire_all()
    stored = dict(
        session.execute(
            select(TaskRun.id, TaskRun.schema_change_json).where(
                TaskRun.id.in_([task.id, other.id])
            )
        ).all()
    )
    assert stored == {task.id: None, other.id: changed}


def test_a_set_back_that_keeps_the_schema_keeps_the_tasks_column_change(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    after = _write(catalog, principal, _orders([2], ["us"]), WriteMode.APPEND).snapshot_id
    task = _task(session, principal)
    changed = {"tables": [_TABLE], "policies": []}
    task.schema_change_json = changed
    session.commit()

    _roll_back(catalog, principal, task, Written(_row(session).id, before, after, False))

    session.expire_all()
    stored = session.scalar(select(TaskRun.schema_change_json).where(TaskRun.id == task.id))
    assert stored == changed


def test_an_empty_existing_table_goes_back_to_empty_and_stays_published(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, _orders([], []), WriteMode.CREATE)
    after = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.APPEND).snapshot_id
    task = _task(session, principal)
    written = Written(_row(session).id, None, after, publishes=False)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.ROLLED_BACK
    loaded = catalog.load_table(principal, _ref())
    assert loaded.metadata.current_snapshot_id is None
    assert loaded.metadata.refs[failed_snapshot_tag(task.run_id)].snapshot_id == after
    assert _row(session).unpublished_run_id is None


def test_an_increment_without_new_rows_changes_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    snapshot = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    task = _task(session, principal)
    location = catalog.load_table(principal, _ref()).metadata_location

    result = _roll_back(catalog, principal, task, Written(_row(session).id, snapshot, None, False))

    assert result.outcome is RollbackOutcome.NOTHING_WRITTEN
    assert catalog.load_table(principal, _ref()).metadata_location == location
    assert [e.detail["outcome"] for e in _events(session)] == ["nothing_written"]


def test_a_first_build_without_rows_is_not_published_and_has_no_tag(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, _orders([], []), WriteMode.CREATE)
    task = _task(session, principal)
    written = Written(_row(session).id, None, None, publishes=True)
    _stamp(session, task, written)
    location = catalog.load_table(principal, _ref()).metadata_location

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.UNPUBLISHED
    assert _row(session).unpublished_run_id == task.run_id
    assert _row(session).metadata_location == location
    refs = _stored_metadata(session).refs
    assert not [name for name in refs if name.startswith("lakana-check-failed")]
    assert read_written(session, task.id) is None


def test_a_view_has_nothing_to_roll_back(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    catalog.create_or_replace_view(
        principal,
        _ref(),
        sql="SELECT 1 AS id",
        references=[],
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    task = _task(session, principal)
    row = _row(session)
    definition = dict(row.view_definition or {})

    result = _roll_back(catalog, principal, task, Written(row.id, None, None, False))

    assert result.outcome is RollbackOutcome.NOT_A_TABLE
    assert _row(session).view_definition == definition


def test_a_dropped_or_recreated_table_is_left_alone(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    after = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    task = _task(session, principal)
    written = Written(_row(session).id, None, after, publishes=True)
    catalog.drop_table(principal, _ref())

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.CHANGED_SINCE

    again = _write(catalog, principal, _orders([7], ["us"]), WriteMode.CREATE).snapshot_id
    location = catalog.load_table(principal, _ref()).metadata_location
    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.CHANGED_SINCE
    loaded = catalog.load_table(principal, _ref())
    assert (loaded.metadata_location, loaded.metadata.current_snapshot_id) == (location, again)
    assert _row(session).unpublished_run_id is None


def test_a_republished_table_that_fails_again_is_not_published_again(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    first = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    task_one = _task(session, principal)
    table_id = _row(session).id
    assert (
        _roll_back(catalog, principal, task_one, Written(table_id, None, first, True)).outcome
        is RollbackOutcome.UNPUBLISHED
    )
    assert _row(session).unpublished_run_id == task_one.run_id

    second = _write(catalog, principal, _orders([2], ["us"]), WriteMode.REPLACE).snapshot_id
    assert _row(session).unpublished_run_id is None
    task_two = _task(session, principal)
    written = Written(table_id, None, second, publishes=True)

    result = _roll_back(catalog, principal, task_two, written)

    assert result.outcome is RollbackOutcome.UNPUBLISHED
    assert _row(session).unpublished_run_id == task_two.run_id
    metadata = _stored_metadata(session)
    assert metadata.current_snapshot_id is None
    assert metadata.refs[failed_snapshot_tag(task_two.run_id)].snapshot_id == second
    assert metadata.refs[failed_snapshot_tag(task_one.run_id)].snapshot_id == first


def test_a_before_snapshot_that_maintenance_expired_fails_the_rollback(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    after = _write(catalog, principal, _orders([2], ["eu"]), WriteMode.REPLACE).snapshot_id
    assert before is not None
    catalog.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[RemoveSnapshotsUpdate(snapshot_ids=[before])],
        action=Action.MAINTAIN_TABLE,
    )
    task = _task(session, principal)
    written = Written(_row(session).id, before, after, publishes=False)
    _stamp(session, task, written)

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(
        RollbackOutcome.FAILED,
        "the snapshot the table had before this run no longer exists "
        "(table maintenance expired it)",
    )
    assert catalog.load_table(principal, _ref()).metadata.current_snapshot_id == after
    assert [e.detail["outcome"] for e in _events(session)] == ["failed"]
    assert read_written(session, task.id) == written


def test_maintenance_keeps_the_before_snapshot_so_the_rollback_still_works(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    after = _write(catalog, principal, _orders([2], ["eu"]), WriteMode.REPLACE).snapshot_id
    assert before is not None
    task = _task(session, principal)
    written = Written(_row(session).id, before, after, publishes=False)
    _stamp(session, task, written)

    report = catalog.maintain(
        principal, _ref(), keep_days=0, keep_last=1, now=datetime.now(UTC) + timedelta(days=30)
    )
    result = _roll_back(catalog, principal, task, written)

    assert report.snapshots_expired == ()
    assert result.outcome is RollbackOutcome.ROLLED_BACK
    assert catalog.load_table(principal, _ref()).metadata.current_snapshot_id == before
    assert _ids(catalog, principal) == [1]


def _move_the_pointer(
    monkeypatch: pytest.MonkeyPatch,
    second_session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    *,
    every_pass: bool,
) -> list[int]:
    """Make another writer change a property of the table while the rollback is deciding.

    The change is committed through a second service on a second connection right after the
    rollback has read the table's metadata, which is before the rollback's first write.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.
    second_session
        The other writer's connection.
    principal
        The other writer.
    tmp_path
        The storage root of `dev`.
    every_pass
        Do it in every pass of the rollback, not only the first.

    Returns
    -------
    list of int
        One entry per property change made.
    """
    other = CatalogService(second_session, StorageResolver(second_session), AllowAllAuthorization())
    real = service_module.read_metadata
    reads: list[int] = []
    moved: list[int] = []
    busy: list[bool] = []

    def read_then_move(*args: object, **kwargs: object) -> TableMetadata:
        result = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        if busy:
            return result
        reads.append(1)
        if len(reads) in ((1, 3) if every_pass else (1,)):
            busy.append(True)
            try:
                other.commit_table(
                    principal,
                    _ref(),
                    requirements=[],
                    updates=[SetPropertiesUpdate(updates={"moved": str(len(moved))})],
                )
            finally:
                busy.clear()
            moved.append(1)
        return result

    monkeypatch.setattr(service_module, "read_metadata", read_then_move)
    return moved


def test_a_pointer_that_moved_without_a_new_snapshot_is_tried_once_more(
    session: Session,
    second_session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    after = _write(catalog, principal, _orders([2], ["eu"]), WriteMode.REPLACE).snapshot_id
    task = _task(session, principal)
    written = Written(_row(session).id, before, after, publishes=False)
    moved = _move_the_pointer(monkeypatch, second_session, principal, tmp_path, every_pass=False)

    result = _roll_back(catalog, principal, task, written)

    assert (result.outcome, moved) == (RollbackOutcome.ROLLED_BACK, [1])
    loaded = catalog.load_table(principal, _ref())
    assert loaded.metadata.current_snapshot_id == before
    assert loaded.metadata.properties["moved"] == "0"
    assert [e.detail["outcome"] for e in _events(session)] == ["rolled_back"]


def test_a_pointer_that_keeps_moving_fails_the_rollback(
    session: Session,
    second_session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    after = _write(catalog, principal, _orders([2], ["eu"]), WriteMode.REPLACE).snapshot_id
    task = _task(session, principal)
    written = Written(_row(session).id, before, after, publishes=False)
    _stamp(session, task, written)
    moved = _move_the_pointer(monkeypatch, second_session, principal, tmp_path, every_pass=True)

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(RollbackOutcome.FAILED, "the table's metadata kept changing")
    assert moved == [1, 1]
    assert catalog.load_table(principal, _ref()).metadata.current_snapshot_id == after
    assert read_written(session, task.id) == written
    assert [e.detail["outcome"] for e in _events(session)] == ["failed"]


def test_a_commit_that_lands_before_a_first_builds_marker_is_not_hidden(
    session: Session,
    second_session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker of a rollback with no pointer move is conditional on the pointer it read."""
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, _orders([], []), WriteMode.CREATE)
    task = _task(session, principal)
    written = Written(_row(session).id, None, None, publishes=True)
    other = CatalogService(second_session, StorageResolver(second_session), AllowAllAuthorization())
    real = service_module.read_metadata
    fired: list[int] = []

    def read_then_write(*args: object, **kwargs: object) -> TableMetadata:
        result = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        if not fired:
            fired.append(1)
            _write(other, principal, _orders([5], ["eu"]), WriteMode.APPEND)
        return result

    monkeypatch.setattr(service_module, "read_metadata", read_then_write)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.CHANGED_SINCE
    assert _row(session).unpublished_run_id is None
    assert _ids(catalog, principal) == [5]


def test_a_row_that_is_read_again_between_the_decision_and_the_commit_still_loses(
    session: Session,
    second_session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit's own read of the table must not be able to adopt a newer writer's pointer."""
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    after = _write(catalog, principal, _orders([2], ["eu"]), WriteMode.APPEND).snapshot_id
    task = _task(session, principal)
    written = Written(_row(session).id, before, after, publishes=False)
    other = CatalogService(second_session, StorageResolver(second_session), AllowAllAuthorization())
    real = service_module.read_metadata
    fired: list[int] = []

    def read_then_write(*args: object, **kwargs: object) -> TableMetadata:
        result = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        if not fired:
            fired.append(1)
            _write(other, principal, _orders([3], ["us"]), WriteMode.APPEND)
            session.expire_all()  # the commit's own read of the row now sees the newer pointer
        return result

    monkeypatch.setattr(service_module, "read_metadata", read_then_write)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.CHANGED_SINCE
    assert _ids(catalog, principal) == [1, 2, 3]


def _seed_results(session: Session, task: TaskRun) -> list[CheckResult]:
    """Insert three results for a task: a failed blocking, a warned and a passed one.

    Parameters
    ----------
    session
        Database session; commits.
    task
        The task run.

    Returns
    -------
    list of CheckResult
        The failed blocking, the warned and the passed result, in that order.
    """
    run = session.get(Run, task.run_id)
    assert run is not None
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    rows = [
        CheckResult(
            id=uuid.uuid4(),
            run_id=task.run_id,
            task_run_id=task.id,
            environment_id=run.environment_id,
            check_name=f"gold.orders/{name}",
            check_path=f"checks/gold/orders/{name}.sql",
            table_name=_TABLE,
            on_failure=on_failure,
            outcome=outcome,
            started_at=now,
            finished_at=now,
        )
        for name, on_failure, outcome in (
            ("unique_id", "block", CheckOutcome.FAILED),
            ("few_nulls", "warn", CheckOutcome.WARNED),
            ("positive", "block", CheckOutcome.PASSED),
        )
    ]
    session.add_all(rows)
    session.commit()
    return rows


_Arrange = Callable[[CatalogService, Session, PrincipalRef], Written]


def _arrange_rolled_back(catalog: CatalogService, session: Session, p: PrincipalRef) -> Written:
    """Write twice, so that the second write can be set back.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    p
        The acting principal.

    Returns
    -------
    Written
        What the run's write did.
    """
    before = _write(catalog, p, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    after = _write(catalog, p, _orders([2], ["eu"]), WriteMode.REPLACE).snapshot_id
    return Written(_row(session).id, before, after, publishes=False)


def _arrange_unpublished(catalog: CatalogService, session: Session, p: PrincipalRef) -> Written:
    """Create the table, which is a first build.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    p
        The acting principal.

    Returns
    -------
    Written
        What the run's write did.
    """
    after = _write(catalog, p, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    return Written(_row(session).id, None, after, publishes=True)


def _arrange_nothing_written(catalog: CatalogService, session: Session, p: PrincipalRef) -> Written:
    """Create the table and claim an increment that wrote nothing.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    p
        The acting principal.

    Returns
    -------
    Written
        What the run's write did.
    """
    before = _write(catalog, p, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    return Written(_row(session).id, before, None, publishes=False)


def _arrange_not_a_table(catalog: CatalogService, session: Session, p: PrincipalRef) -> Written:
    """Make the asset a view.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    p
        The acting principal.

    Returns
    -------
    Written
        What the run's write did.
    """
    catalog.create_or_replace_view(
        p,
        _ref(),
        sql="SELECT 1 AS id",
        references=[],
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    return Written(_row(session).id, None, None, publishes=False)


def _arrange_changed_since(catalog: CatalogService, session: Session, p: PrincipalRef) -> Written:
    """Let a second write land after the one the run made.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    p
        The acting principal.

    Returns
    -------
    Written
        What the run's write did.
    """
    written = _arrange_rolled_back(catalog, session, p)
    _write(catalog, p, _orders([3], ["us"]), WriteMode.APPEND)
    return written


def _arrange_failed(catalog: CatalogService, session: Session, p: PrincipalRef) -> Written:
    """Expire the snapshot the run's write replaced.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    p
        The acting principal.

    Returns
    -------
    Written
        What the run's write did.
    """
    written = _arrange_rolled_back(catalog, session, p)
    assert written.snapshot_before is not None
    catalog.commit_table(
        p,
        _ref(),
        requirements=[],
        updates=[RemoveSnapshotsUpdate(snapshot_ids=[written.snapshot_before])],
        action=Action.MAINTAIN_TABLE,
    )
    return written


_ARRANGEMENTS: dict[RollbackOutcome, _Arrange] = {
    RollbackOutcome.ROLLED_BACK: _arrange_rolled_back,
    RollbackOutcome.UNPUBLISHED: _arrange_unpublished,
    RollbackOutcome.NOTHING_WRITTEN: _arrange_nothing_written,
    RollbackOutcome.NOT_A_TABLE: _arrange_not_a_table,
    RollbackOutcome.CHANGED_SINCE: _arrange_changed_since,
    RollbackOutcome.FAILED: _arrange_failed,
}


@pytest.mark.parametrize("outcome", list(RollbackOutcome))
def test_the_rollback_sets_the_outcome_on_the_failed_blocking_results(
    session: Session, principal: PrincipalRef, tmp_path: Path, outcome: RollbackOutcome
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _ARRANGEMENTS[outcome](catalog, session, principal)
    task = _task(session, principal)
    failed, warned, passed = _seed_results(session, task)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is outcome
    session.expire_all()
    stored = {r.id: r.rollback_outcome for r in session.scalars(select(CheckResult))}
    assert stored == {failed.id: outcome, warned.id: None, passed.id: None}


@pytest.mark.parametrize("outcome", list(RollbackOutcome))
def test_every_outcome_is_audited_once(
    session: Session, principal: PrincipalRef, tmp_path: Path, outcome: RollbackOutcome
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _ARRANGEMENTS[outcome](catalog, session, principal)
    task = _task(session, principal)

    _roll_back(catalog, principal, task, written)

    (event,) = _events(session)
    assert event.principal_id == principal.id
    assert (event.securable_kind, event.securable_environment, event.securable_table) == (
        SecurableKind.TABLE,
        "dev",
        "orders",
    )
    assert event.detail["outcome"] == outcome.value
    assert event.detail["checks"] == [_FAILED]


def test_the_rollback_needs_the_write_grant(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    location = catalog.load_table(principal, _ref()).metadata_location
    reader = CatalogService(
        session, StorageResolver(session), ActionAllowList(Action.READ_TABLE, Action.MAINTAIN_TABLE)
    )

    with pytest.raises(PermissionDenied):
        _roll_back(reader, principal, task, written)

    assert catalog.load_table(principal, _ref()).metadata_location == location
    assert _events(session) == []


def _bound(principal: PrincipalRef, task: TaskRun, claim: int) -> PrincipalRef:
    """Return the principal as authentication builds it for a bound task token.

    Parameters
    ----------
    principal
        The run's principal.
    task
        The task run the token is bound to.
    claim
        The claim the token carries.

    Returns
    -------
    PrincipalRef
        With the claim-form run context.
    """
    return dataclasses.replace(principal, run_context=RunContext(task_run_id=task.id, claim=claim))


@pytest.mark.parametrize(
    "outcome", [RollbackOutcome.ROLLED_BACK, RollbackOutcome.NOTHING_WRITTEN], ids=lambda o: o.value
)
def test_the_rollback_of_a_superseded_claim_is_refused(
    session: Session, principal: PrincipalRef, tmp_path: Path, outcome: RollbackOutcome
) -> None:
    """Refused up front, also where no commit follows: no audit event, no recorded outcome."""
    catalog = _catalog(session, principal, tmp_path)
    written = _ARRANGEMENTS[outcome](catalog, session, principal)
    task = _task(session, principal)
    failed, _, _ = _seed_results(session, task)
    session.execute(update(TaskRun).where(TaskRun.id == task.id).values(claim_count=2))
    session.commit()
    location = catalog.load_table(principal, _ref()).metadata_location

    with pytest.raises(PermissionDenied) as caught:
        _roll_back(catalog, _bound(principal, task, claim=1), task, written)

    assert str(caught.value) == SUPERSEDED_MESSAGE
    assert catalog.load_table(principal, _ref()).metadata_location == location
    assert _events(session) == []
    session.expire_all()
    stored = session.get(CheckResult, failed.id)
    assert stored is not None and stored.rollback_outcome is None


def test_the_rollback_under_a_live_claim_in_a_protected_environment_passes(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    env.is_protected = True
    session.commit()

    result = _roll_back(catalog, _bound(principal, task, claim=1), task, written)

    assert result.outcome is RollbackOutcome.ROLLED_BACK
    assert (
        catalog.load_table(principal, _ref()).metadata.current_snapshot_id
        == written.snapshot_before
    )


@pytest.mark.parametrize(
    "outcome", [RollbackOutcome.ROLLED_BACK, RollbackOutcome.NOTHING_WRITTEN], ids=lambda o: o.value
)
def test_a_claim_cannot_roll_back_another_asset(
    session: Session, principal: PrincipalRef, tmp_path: Path, outcome: RollbackOutcome
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _ARRANGEMENTS[outcome](catalog, session, principal)
    other = _task(session, principal, asset="gold.other")
    location = catalog.load_table(principal, _ref()).metadata_location

    with pytest.raises(PermissionDenied) as caught:
        _roll_back(catalog, _bound(principal, other, claim=1), other, written)

    assert str(caught.value) == OTHER_ASSET_MESSAGE.format(
        asset="gold.other", operation="commit", target="gold.orders"
    )
    assert catalog.load_table(principal, _ref()).metadata_location == location
    assert _events(session) == []


def test_a_claim_superseded_while_the_rollback_decides_is_refused_not_recorded(
    session: Session,
    second_session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal inside the commit is a refusal, not a `failed` outcome with an audit event."""
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    stamped = Written(written.table_id, written.snapshot_before, written.snapshot_after, False)
    _stamp(session, task, stamped)
    real = service_module.read_metadata
    fired: list[int] = []

    def read_then_supersede(*args: object, **kwargs: object) -> TableMetadata:
        result = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        if not fired:
            fired.append(1)
            second_session.execute(
                update(TaskRun).where(TaskRun.id == task.id).values(claim_count=2)
            )
            second_session.commit()
        return result

    monkeypatch.setattr(service_module, "read_metadata", read_then_supersede)

    with pytest.raises(PermissionDenied) as caught:
        _roll_back(catalog, _bound(principal, task, claim=1), task, written)

    assert str(caught.value) == SUPERSEDED_MESSAGE
    assert (
        catalog.load_table(principal, _ref()).metadata.current_snapshot_id == written.snapshot_after
    )
    assert _events(session) == []
    assert read_written(session, task.id) == stamped


def _after_the_first_read(monkeypatch: pytest.MonkeyPatch, action: Callable[[], None]) -> None:
    """Run `action` once, right after the rollback's first read of the table's metadata.

    That read is before any write of the rollback, so `action` may commit through another
    connection.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.
    action
        What the other writer does.
    """
    real = service_module.read_metadata
    fired: list[int] = []

    def read_then_act(*args: object, **kwargs: object) -> TableMetadata:
        result = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        if not fired:
            fired.append(1)
            action()
        return result

    monkeypatch.setattr(service_module, "read_metadata", read_then_act)


def test_no_row_is_locked_before_the_metadata_file_is_written(
    session: Session,
    engine: Engine,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write locks `tables` and then `task_runs`, and never holds a row across the upload."""
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _seed_results(session, task)
    _stamp(session, task, written)
    log: list[str] = []
    real = service_module.write_metadata

    def write_and_note(*args: object, **kwargs: object) -> str:
        log.append("<metadata file written>")
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    def note(conn: object, cursor: object, statement: str, *rest: object) -> None:
        log.append(" ".join(statement.split()))

    monkeypatch.setattr(service_module, "write_metadata", write_and_note)
    event.listen(engine, "before_cursor_execute", note)
    try:
        assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.ROLLED_BACK
    finally:
        event.remove(engine, "before_cursor_execute", note)

    cut = log.index("<metadata file written>")
    writes = ("UPDATE", "INSERT", "DELETE")
    assert [s for s in log[:cut] if s.startswith(writes)] == []
    after = [s for s in log[cut:] if s.startswith(writes)]
    tables_at = next(i for i, s in enumerate(after) if s.startswith("UPDATE tables"))
    runs_at = next(i for i, s in enumerate(after) if s.startswith("UPDATE task_runs"))
    assert tables_at < runs_at


def test_a_database_error_is_a_failed_rollback_and_leaves_the_session_usable(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _stamp(session, task, written)
    location = catalog.load_table(principal, _ref()).metadata_location

    def deadlock(*args: object, **kwargs: object) -> None:
        raise OperationalError("UPDATE tables", {}, Exception("deadlock detected"))

    monkeypatch.setattr(service_module, "commit_metadata_pointer", deadlock)

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(
        RollbackOutcome.FAILED, "the database refused or interrupted the rollback"
    )
    monkeypatch.undo()
    assert catalog.load_table(principal, _ref()).metadata_location == location
    assert [e.detail["outcome"] for e in _events(session)] == ["failed"]
    assert read_written(session, task.id) == written
    assert _row(session).unpublished_run_id is None


def test_the_audit_row_and_the_pointer_move_land_together_or_not_at_all(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_unpublished(catalog, session, principal)
    task = _task(session, principal)
    _seed_results(session, task)
    _stamp(session, task, written)
    location = catalog.load_table(principal, _ref()).metadata_location
    real = catalog._guard.record_pending
    calls: list[int] = []

    def fail_once() -> None:
        calls.append(1)
        if len(calls) == 1:  # after the swap, the marker, the stamp and the audit row
            raise OperationalError("COMMIT", {}, Exception("connection lost"))
        real()

    monkeypatch.setattr(catalog._guard, "record_pending", fail_once)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.FAILED
    assert catalog.load_table(principal, _ref()).metadata_location == location
    assert _row(session).unpublished_run_id is None
    assert read_written(session, task.id) == written
    assert [e.detail["outcome"] for e in _events(session)] == ["failed"]


def test_an_outcome_that_commits_nothing_still_checks_the_claim_before_it_records(
    session: Session,
    second_session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_nothing_written(catalog, session, principal)
    task = _task(session, principal)
    failed, _, _ = _seed_results(session, task)

    def supersede() -> None:
        second_session.execute(update(TaskRun).where(TaskRun.id == task.id).values(claim_count=2))
        second_session.commit()

    _after_the_first_read(monkeypatch, supersede)

    with pytest.raises(PermissionDenied) as caught:
        _roll_back(catalog, _bound(principal, task, claim=1), task, written)

    assert str(caught.value) == SUPERSEDED_MESSAGE
    assert _events(session) == []
    session.expire_all()
    stored = session.get(CheckResult, failed.id)
    assert stored is not None and stored.rollback_outcome is None


def test_the_requirements_of_the_rollback_commit_are_pinned(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    seen: list[dict[str, object]] = []
    real = catalog.commit_table

    def spy(*args: object, **kwargs: object) -> object:
        seen.append(kwargs)
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(catalog, "commit_table", spy)

    _roll_back(catalog, principal, task, written)

    (call,) = seen
    assert call["requirements"] == [
        AssertTableUUID(uuid=written.table_id),
        AssertRefSnapshotId(ref="main", snapshot_id=written.snapshot_after),
    ]
    assert call["internal_rollback"] is True


def test_a_schema_changed_after_the_write_is_not_set_back(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    wider = pa.table(
        {
            "id": pa.array([2], pa.int64()),
            "region": pa.array(["us"], pa.string()),
            "note": pa.array(["x"], pa.string()),
        }
    )
    after = _write(catalog, principal, wider, WriteMode.APPEND, evolve_schema=True).snapshot_id
    current = catalog.load_table(principal, _ref()).metadata
    newest = Schema(
        *current.schema().fields,
        NestedField(current.last_column_id + 1, "extra", StringType(), required=False),
        schema_id=current.current_schema_id + 1,
    )
    catalog.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[AddSchemaUpdate(schema_=newest), SetCurrentSchemaUpdate(schema_id=-1)],
    )
    task = _task(session, principal)

    result = _roll_back(
        catalog, principal, task, Written(_row(session).id, before, after, publishes=False)
    )

    assert result.outcome is RollbackOutcome.ROLLED_BACK
    loaded = catalog.load_table(principal, _ref()).metadata
    assert loaded.current_snapshot_id == before
    assert loaded.current_schema_id == current.current_schema_id + 1
    assert [f.name for f in loaded.schema().fields] == ["id", "region", "note", "extra"]


def test_a_table_dropped_during_the_rollback_is_changed_since(
    session: Session,
    second_session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    other = CatalogService(second_session, StorageResolver(second_session), AllowAllAuthorization())
    _after_the_first_read(monkeypatch, lambda: other.drop_table(principal, _ref()))

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(
        RollbackOutcome.CHANGED_SINCE, "the table was dropped while the rollback was prepared"
    )
    assert [e.detail["outcome"] for e in _events(session)] == ["changed_since"]


def _query_run_context(session: Session, principal: PrincipalRef, task: TaskRun) -> PrincipalRef:
    """Return the principal as the worker builds it for a task's write run (the M5c1 form).

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The run's principal.
    task
        The task run.

    Returns
    -------
    PrincipalRef
        With a run context of the task run and a running query run.
    """
    run = session.get(Run, task.run_id)
    assert run is not None
    query = QueryRun(
        id=uuid.uuid4(),
        environment_id=run.environment_id,
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.RUNNING,
        task_run_id=task.id,
    )
    session.add(query)
    session.flush()
    task.query_run_id = query.id
    session.commit()
    return dataclasses.replace(
        principal, run_context=RunContext(task_run_id=task.id, query_run_id=query.id)
    )


def test_a_context_of_another_task_cannot_roll_back_this_ones_write(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    mine = _task(session, principal)
    other = _task(session, principal)
    location = catalog.load_table(principal, _ref()).metadata_location

    with pytest.raises(PermissionDenied) as caught:
        _roll_back(catalog, _bound(principal, other, claim=1), mine, written)

    assert str(caught.value) == OTHER_ASSET_MESSAGE.format(
        asset="gold.orders", operation="commit", target="gold.orders"
    )
    assert catalog.load_table(principal, _ref()).metadata_location == location
    assert _events(session) == []


@pytest.mark.parametrize("protected", [False, True])
def test_the_query_run_form_rolls_back_only_its_own_asset(
    session: Session, principal: PrincipalRef, tmp_path: Path, protected: bool
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    other = _task(session, principal, asset="gold.other")
    own = _task(session, principal)
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    env.is_protected = protected
    session.commit()
    location = catalog.load_table(principal, _ref()).metadata_location

    with pytest.raises(PermissionDenied):
        _roll_back(catalog, _query_run_context(session, principal, other), other, written)
    assert catalog.load_table(principal, _ref()).metadata_location == location
    assert _events(session) == []

    result = _roll_back(catalog, _query_run_context(session, principal, own), own, written)
    assert result.outcome is RollbackOutcome.ROLLED_BACK


def test_the_query_run_form_is_refused_another_asset_with_the_asset_message_when_unprotected(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    other = _task(session, principal, asset="gold.other")

    with pytest.raises(PermissionDenied) as caught:
        _roll_back(catalog, _query_run_context(session, principal, other), other, written)

    assert str(caught.value) == OTHER_ASSET_MESSAGE.format(
        asset="gold.other", operation="commit", target="gold.orders"
    )


_SEC = Securable.table_scope("dev", NamespacePath.from_parts(["gold"]), "orders")
_REFUSED = "the database refused or interrupted the rollback"
_NOT_KNOWN = (
    "the rollback was interrupted and its outcome could not be read back; whether it took "
    "effect is not known: look at the table with `lakana table get`"
)


def _lose_the_commit(
    session: Session, *, applied: bool, error: Exception | None = None, always: bool = False
) -> None:
    """Make the session's commit fail with a connection error, once (or every time).

    Parameters
    ----------
    session
        The session under test; its `commit` is patched for the rest of the test.
    applied
        Apply the commit first, as a server does whose acknowledgement is lost.
    error
        What is raised; a driver error of a lost connection when `None`.
    always
        Fail every commit, not only the first.
    """
    real = session.commit
    calls: list[int] = []

    def commit() -> None:
        if applied and not calls:
            real()
        if always or not calls:
            calls.append(1)
            raise error or OperationalError("COMMIT", None, ConnectionResetError("reset"))
        real()

    session.commit = commit  # ty: ignore[invalid-assignment]


def _results_outcomes(session: Session) -> set[RollbackOutcome | None]:
    """Read the rollback outcome of every result afresh.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    set
        The distinct `rollback_outcome` values.
    """
    session.rollback()
    session.expire_all()
    return {r.rollback_outcome for r in session.scalars(select(CheckResult))}


def _blocking_result(session: Session, task: TaskRun) -> CheckResult:
    """Insert one failed blocking result for a task.

    Parameters
    ----------
    session
        Database session; commits.
    task
        The task run.

    Returns
    -------
    CheckResult
        The row.
    """
    return _seed_results(session, task)[0]


@pytest.mark.parametrize("publishes", [False, True])
def test_a_lost_acknowledgement_of_the_swap_keeps_the_committed_outcome(
    session: Session, principal: PrincipalRef, tmp_path: Path, publishes: bool
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = (_arrange_unpublished if publishes else _arrange_rolled_back)(
        catalog, session, principal
    )
    task = _task(session, principal)
    _blocking_result(session, task)
    _stamp(session, task, written)
    outcome = RollbackOutcome.UNPUBLISHED if publishes else RollbackOutcome.ROLLED_BACK
    _lose_the_commit(session, applied=True)

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(outcome)
    assert [e.detail["outcome"] for e in _events(session)] == [outcome.value]
    assert _results_outcomes(session) == {outcome, None}
    assert read_written(session, task.id) is None


def test_a_lost_acknowledgement_of_the_marker_only_path_keeps_the_committed_outcome(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, _orders([], []), WriteMode.CREATE)
    task = _task(session, principal)
    _blocking_result(session, task)
    written = Written(_row(session).id, None, None, publishes=True)
    _lose_the_commit(session, applied=True)

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(RollbackOutcome.UNPUBLISHED)
    assert [e.detail["outcome"] for e in _events(session)] == ["unpublished"]
    assert _row(session).unpublished_run_id == task.run_id


def test_a_commit_that_failed_without_applying_is_failed_and_recorded_once(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _blocking_result(session, task)
    _stamp(session, task, written)
    location = catalog.load_table(principal, _ref()).metadata_location
    _lose_the_commit(session, applied=False)

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(RollbackOutcome.FAILED, _REFUSED)
    assert catalog.load_table(principal, _ref()).metadata_location == location
    assert [e.detail["outcome"] for e in _events(session)] == ["failed"]
    assert _results_outcomes(session) == {RollbackOutcome.FAILED, None}
    assert read_written(session, task.id) == written


@pytest.mark.parametrize("fault", ["storage", "database", "invalid_request"])
def test_when_the_reread_fails_the_outcome_is_not_known_and_nothing_is_recorded(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _blocking_result(session, task)
    real_commit = session.commit
    errors = {
        "storage": PermissionError("denied"),
        "database": OperationalError("SELECT", None, OSError()),
        "invalid_request": InvalidRequestError("This session is in 'prepared' state"),
    }

    def commit_then_break() -> None:
        real_commit()
        monkeypatch.setattr(
            catalog, "_rollback_landed", lambda *a, **k: (_ for _ in ()).throw(errors[fault])
        )
        raise OperationalError("COMMIT", None, ConnectionResetError("reset"))

    session.commit = commit_then_break  # ty: ignore[invalid-assignment]

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(RollbackOutcome.FAILED, _NOT_KNOWN)
    monkeypatch.undo()
    session.commit = real_commit  # ty: ignore[invalid-assignment]
    assert [e.detail["outcome"] for e in _events(session)] == ["rolled_back"]
    assert _results_outcomes(session) == {RollbackOutcome.ROLLED_BACK, None}


def test_a_raw_driver_error_at_the_swap_is_a_failed_rollback_and_is_logged(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """pg8000 raises a raw `OSError` after a server-side cut; it is not wrapped by SQLAlchemy."""
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)

    def cut(*args: object, **kwargs: object) -> None:
        raise ConnectionResetError("SECRET-SQL deadlock")

    monkeypatch.setattr(service_module, "commit_metadata_pointer", cut)
    with caplog.at_level(logging.WARNING, logger="lakana_core.catalog.service"):
        result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(RollbackOutcome.FAILED, _REFUSED)
    records = [r for r in caplog.records if "rollback" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "ConnectionResetError" in message and "gold.orders" in message
    assert "SECRET-SQL" not in message
    monkeypatch.undo()
    assert [e.detail["outcome"] for e in _events(session)] == ["failed"]


def test_a_raw_driver_error_at_the_commit_keeps_the_committed_outcome(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _lose_the_commit(session, applied=True, error=BrokenPipeError("pipe"))

    assert _roll_back(catalog, principal, task, written) == Rollback(RollbackOutcome.ROLLED_BACK)
    assert [e.detail["outcome"] for e in _events(session)] == ["rolled_back"]


def test_a_rollback_that_fails_inside_the_cleanup_does_not_mask_the_outcome(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    real_rollback = session.rollback
    calls: list[int] = []

    def rollback() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("ROLLBACK", None, ConnectionResetError("dead"))
        real_rollback()

    def cut(*args: object, **kwargs: object) -> None:
        raise OperationalError("UPDATE tables", None, Exception("deadlock detected"))

    monkeypatch.setattr(service_module, "commit_metadata_pointer", cut)
    monkeypatch.setattr(session, "rollback", rollback)

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(RollbackOutcome.FAILED, _REFUSED)


def test_an_outcome_that_commits_nothing_survives_a_failing_record(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_nothing_written(catalog, session, principal)
    task = _task(session, principal)
    _lose_the_commit(session, applied=False, always=True)

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.NOTHING_WRITTEN


@pytest.mark.parametrize("where", ["read", "write"])
def test_a_storage_error_is_not_reported_as_a_database_error(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    where: str,
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)

    def broken(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied: s3://secret-bucket/key")

    monkeypatch.setattr(
        service_module, "read_metadata" if where == "read" else "write_metadata", broken
    )
    with caplog.at_level(logging.WARNING, logger="lakana_core.catalog.service"):
        result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(
        RollbackOutcome.FAILED,
        "the table's metadata could not be read or written: PermissionError",
    )
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_rollback_is_landed_only_by_its_own_audit_row(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_unpublished(catalog, session, principal)
    task = _task(session, principal)
    audit = AuditService(session)

    def landed(claim: int = 1, task_run_id: uuid.UUID | None = None) -> RollbackOutcome | None:
        return catalog._rollback_landed(_ref(), task_run_id or task.id, claim, written.table_id)

    assert landed() is None
    detail = {"task_run_id": str(task.id), "claim": 0, "outcome": "unpublished"}
    audit.record(principal.id, AuditEventType.TABLE_ROLLED_BACK, securable=_SEC, detail=detail)
    session.commit()
    assert landed(claim=1) is None  # an earlier claim's row is not this claim's
    audit.record(
        principal.id,
        AuditEventType.TABLE_ROLLED_BACK,
        securable=_SEC,
        detail={**detail, "claim": 1, "outcome": "failed"},
    )
    session.commit()
    assert landed(claim=1) is None  # a `failed` row is not a landed rollback
    audit.record(
        principal.id,
        AuditEventType.TABLE_ROLLED_BACK,
        securable=_SEC,
        detail={**detail, "claim": 1},
    )
    session.commit()
    assert landed(claim=1) is RollbackOutcome.UNPUBLISHED
    assert landed(claim=1, task_run_id=uuid.uuid4()) is None
    assert not session.in_transaction()


def test_on_postgres_the_landed_check_sets_a_lock_timeout_first(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    real = session.execute
    sent: list[str] = []

    def execute(statement: object, *args: object, **kwargs: object) -> object:
        if str(statement).startswith("SET LOCAL"):
            sent.append(str(statement))
            return None
        return real(statement, *args, **kwargs)  # ty: ignore[no-matching-overload]

    with (
        mock.patch.object(session.get_bind().dialect, "name", "postgresql"),
        mock.patch.object(session, "execute", execute),
    ):
        catalog._rollback_landed(_ref(), uuid.uuid4(), 1, written.table_id)

    assert sent == [f"SET LOCAL lock_timeout = '{service_module.LOCK_TIMEOUT_SECONDS}s'"]


def test_a_storage_error_of_an_ordinary_commit_is_not_converted(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    _arrange_rolled_back(catalog, session, principal)

    def broken(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(service_module, "read_metadata", broken)
    with pytest.raises(PermissionError):
        catalog.commit_table(principal, _ref(), requirements=[], updates=[])


def test_a_landed_rollback_stays_landed_when_another_writer_commits_before_the_reread(
    session: Session,
    second_session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _blocking_result(session, task)
    other = CatalogService(second_session, StorageResolver(second_session), AllowAllAuthorization())
    real = session.commit
    calls: list[int] = []

    def commit() -> None:
        real()
        if not calls:
            calls.append(1)
            _write(other, principal, _orders([9], ["us"]), WriteMode.APPEND)
            raise OperationalError("COMMIT", None, ConnectionResetError("reset"))

    session.commit = commit  # ty: ignore[invalid-assignment]

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(RollbackOutcome.ROLLED_BACK)
    assert [e.detail["outcome"] for e in _events(session)] == ["rolled_back"]
    assert _results_outcomes(session) == {RollbackOutcome.ROLLED_BACK, None}


def test_a_marker_only_commit_that_did_not_apply_is_not_landed_by_an_earlier_attempts_marker(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    _write(catalog, principal, _orders([], []), WriteMode.CREATE)
    task = _task(session, principal)
    _blocking_result(session, task)
    written = Written(_row(session).id, None, None, publishes=True)
    _stamp(session, task, written)
    row = _row(session)
    row.unpublished_run_id = task.run_id  # left by an earlier attempt of this run
    session.commit()
    _lose_the_commit(session, applied=False)

    result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(RollbackOutcome.FAILED, _REFUSED)
    assert [e.detail["outcome"] for e in _events(session)] == ["failed"]
    assert read_written(session, task.id) == written


def test_a_stale_claim_whose_commit_failed_records_nothing(
    session: Session,
    second_session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    failed, _, _ = _seed_results(session, task)
    real_rollback = session.rollback

    real_commit = session.commit
    calls: list[int] = []

    def commit() -> None:
        if calls:
            real_commit()
            return
        calls.append(1)
        real_rollback()  # the hung commit gave up; the lock is gone
        second_session.execute(update(TaskRun).where(TaskRun.id == task.id).values(claim_count=2))
        second_session.execute(
            update(CheckResult)
            .where(CheckResult.id == failed.id)
            .values(rollback_outcome=RollbackOutcome.ROLLED_BACK)
        )
        second_session.commit()
        raise OperationalError("COMMIT", None, ConnectionResetError("reset"))

    session.commit = commit  # ty: ignore[invalid-assignment]

    with pytest.raises(PermissionDenied) as caught:
        _roll_back(catalog, _bound(principal, task, claim=1), task, written)

    assert str(caught.value) == SUPERSEDED_MESSAGE
    del session.commit
    assert _events(session) == []  # the refusal is audited as PROTECTED_WRITE_DENIED only
    session.expire_all()
    stored = session.get(CheckResult, failed.id)
    assert stored is not None and stored.rollback_outcome is RollbackOutcome.ROLLED_BACK


def test_a_driver_error_at_the_storage_lookup_is_a_database_error(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`for_environment` reads the database; its failure is not a storage failure."""
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)

    def cut(*args: object, **kwargs: object) -> None:
        raise ConnectionResetError("reset")

    monkeypatch.setattr(StorageResolver, "for_environment", cut)

    result = _roll_back(catalog, principal, task, written)
    monkeypatch.undo()

    assert result == Rollback(RollbackOutcome.FAILED, _REFUSED)


@pytest.mark.parametrize("publishes", [False, True])
def test_a_change_that_is_not_the_task_runs_own_is_recorded_and_nothing_is_touched(
    session: Session, principal: PrincipalRef, tmp_path: Path, publishes: bool
) -> None:
    """N1: the task run wrote nothing of its own, so whatever is in the table is someone else's.

    Without the signal the same `Written` would read as "nothing written" (or, with
    `publishes`, could mark a table somebody else created as not published).
    """
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    other = _write(catalog, principal, _orders([3], ["us"]), WriteMode.APPEND).snapshot_id
    task = _task(session, principal)
    table_id = catalog.load_table(principal, _ref()).uuid
    written = Written(table_id, None if publishes else before, None, publishes=publishes)

    result = catalog.roll_back(
        principal,
        _ref(),
        run_id=task.run_id,
        task_run_id=task.id,
        written=written,
        failed_checks=[],
        foreign_change=True,
    )

    assert result == Rollback(
        RollbackOutcome.CHANGED_SINCE, "another writer changed it during this run"
    )
    loaded = catalog.load_table(principal, _ref())
    assert loaded.metadata.current_snapshot_id == other
    assert _ids(catalog, principal) == [1, 3]
    assert failed_snapshot_tag(task.run_id) not in loaded.metadata.refs
    assert _row(session).unpublished_run_id is None
    (event,) = _events(session)
    assert event.detail["outcome"] == "changed_since"
    assert (event.detail["task_run_id"], event.detail["checks"]) == (str(task.id), [])


def test_a_failure_text_this_module_did_not_word_never_reaches_the_reason(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A task's message is read by whoever may read the run: no storage location in it."""
    catalog = _catalog(session, principal, tmp_path)
    task = _task(session, principal)
    written = _arrange_rolled_back(catalog, session, principal)
    _stamp(session, task, written)
    location = "s3://bucket/gold/orders-1f/metadata/00002.metadata.json"

    def unreadable(*args: object, **kwargs: object) -> object:
        raise LakanaError(f"Metadata at {location!r} could not be parsed")

    monkeypatch.setattr(service_module, "read_metadata", unreadable)
    with caplog.at_level(logging.WARNING, logger="lakana_core.catalog.service"):
        result = _roll_back(catalog, principal, task, written)

    assert result == Rollback(
        RollbackOutcome.FAILED, "the table's metadata could not be read or written: LakanaError"
    )
    message = failure_message(
        [],
        table=_TABLE,
        run_id=task.run_id,
        environment="dev",
        rollback=result,
    )
    assert "s3://" not in message and "metadata.json" not in message
    logged = [record.getMessage() for record in caplog.records]
    assert any(location in line and _TABLE in line for line in logged)  # the operator's log
    (event,) = _events(session)
    assert "s3://" not in repr(event.detail)


def _keep_note(session: Session, run_id: uuid.UUID) -> None:
    """Put the note of a table that was kept after a failed check on `orders`.

    Parameters
    ----------
    session
        Database session; commits.
    run_id
        The run the note names.
    """
    session.execute(
        update(Table)
        .where(Table.name == "orders")
        .values(kept_run_id=run_id, kept_task_run_id=uuid.uuid4(), kept_check=_FAILED)
    )
    session.commit()


@pytest.mark.parametrize("case", ["replaced", "first-build", "first-build-without-rows"])
def test_a_rollback_leaves_the_note(
    session: Session, principal: PrincipalRef, tmp_path: Path, case: str
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    if case == "replaced":
        written = _arrange_rolled_back(catalog, session, principal)
    elif case == "first-build":
        written = _arrange_unpublished(catalog, session, principal)
    else:
        _write(catalog, principal, _orders([], []), WriteMode.CREATE)
        written = Written(_row(session).id, None, None, publishes=True)
    task = _task(session, principal)
    _stamp(session, task, written)
    kept_run = uuid.uuid4()
    _keep_note(session, kept_run)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome in (RollbackOutcome.ROLLED_BACK, RollbackOutcome.UNPUBLISHED)
    row = _row(session)
    assert (row.kept_run_id, row.kept_check) == (kept_run, _FAILED)
    assert not session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_KEPT_CLEARED)
    ).all()


def test_a_rollback_that_changed_nothing_leaves_the_note(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_nothing_written(catalog, session, principal)
    task = _task(session, principal)
    run_id = uuid.uuid4()
    _keep_note(session, run_id)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.NOTHING_WRITTEN
    assert _row(session).kept_run_id == run_id


def _entry(
    run_id: uuid.UUID, check: str, snapshot: int | None, *, unchecked: bool = False
) -> dict[str, object]:
    """Build a stacked earlier mark, as `note_kept` stores it.

    Parameters
    ----------
    run_id
        The run the mark names.
    check
        The text the state message shows for the check.
    snapshot
        The snapshot the mark was a verdict on.
    unchecked
        Whether the mark names a check that could not run.

    Returns
    -------
    dict
        The entry.
    """
    return {
        "run_id": str(run_id),
        "task_run_id": str(uuid.uuid4()),
        "check": check,
        "unchecked": unchecked,
        "snapshot_id": snapshot,
    }


def _mark(
    session: Session,
    run_id: uuid.UUID,
    earlier: list[dict[str, object]],
    *,
    task_run_id: uuid.UUID | None = None,
    snapshot: int | None = None,
    check: str = _FAILED,
) -> None:
    """Put a mark and a stack of earlier marks on `orders`.

    Parameters
    ----------
    session
        Database session; commits.
    run_id
        The run the mark names.
    earlier
        The stack, the newest entry last.
    task_run_id
        The task run the mark names; a new id when `None`.
    snapshot
        The snapshot the mark is a verdict on.
    check
        The text of the mark's check.
    """
    session.execute(
        update(Table)
        .where(Table.name == "orders")
        .values(
            kept_run_id=run_id,
            kept_task_run_id=task_run_id or uuid.uuid4(),
            kept_check=check,
            kept_earlier=earlier,
            kept_snapshot_id=snapshot,
        )
    )
    session.commit()


def test_a_set_back_of_the_data_the_mark_names_restores_the_mark_before(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _stamp(session, task, written)
    earlier = _entry(uuid.uuid4(), "gold.orders/old", written.snapshot_before, unchecked=True)
    _mark(session, task.run_id, [{"snapshot_id": None}, earlier], task_run_id=task.id)

    result = _roll_back(catalog, principal, task, written)

    assert result.outcome is RollbackOutcome.ROLLED_BACK
    row = _row(session)
    assert (str(row.kept_run_id), str(row.kept_task_run_id), row.kept_check) == (
        earlier["run_id"],
        earlier["task_run_id"],
        "gold.orders/old",
    )
    assert (row.kept_unchecked, row.kept_snapshot_id) == (True, written.snapshot_before)
    assert row.kept_earlier == [{"snapshot_id": None}]
    (event,) = _events(session)
    assert event.detail["kept_restored"] == earlier


def test_a_set_back_to_data_without_a_mark_lets_readers_in_again(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _stamp(session, task, written)
    _mark(
        session,
        task.run_id,
        [{"snapshot_id": written.snapshot_before}],
        task_run_id=task.id,
        snapshot=written.snapshot_after,
    )

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.ROLLED_BACK

    row = _row(session)
    assert (row.kept_run_id, row.kept_task_run_id, row.kept_check) == (None, None, None)
    assert (row.kept_unchecked, row.kept_earlier) == (False, [])
    assert row.kept_snapshot_id == written.snapshot_before
    (event,) = _events(session)
    assert event.detail["kept_restored"] is None


def test_a_set_back_to_a_snapshot_no_entry_describes_marks_the_checks_unknown(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The unmarked state was a verdict on other data than the table holds after the set-back."""
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _stamp(session, task, written)
    assert written.snapshot_before is not None
    stack: list[dict[str, object]] = [{"snapshot_id": written.snapshot_before + 1}]
    _mark(session, task.run_id, stack, task_run_id=task.id)

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.ROLLED_BACK

    row = _row(session)
    assert (row.kept_run_id, row.kept_earlier) == (task.run_id, stack)
    assert (row.kept_unknown, row.kept_snapshot_id) == (True, written.snapshot_before)
    (event,) = _events(session)
    assert event.detail["kept_unknown"] is True


def test_a_chain_of_kept_writes_is_set_back_twice_one_mark_at_a_time(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    s1 = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    s2 = _write(catalog, principal, _orders([2], ["eu"]), WriteMode.REPLACE).snapshot_id
    s3 = _write(catalog, principal, _orders([3], ["eu"]), WriteMode.REPLACE).snapshot_id
    table_id = _row(session).id
    task_b, task_c = _task(session, principal), _task(session, principal)
    run_a = uuid.uuid4()
    mark_a = _entry(run_a, "gold.orders/a", s1)
    mark_b = _entry(task_b.run_id, "gold.orders/b", s2)
    mark_b["task_run_id"] = str(task_b.id)
    _mark(
        session,
        task_c.run_id,
        [{"snapshot_id": None}, mark_a, mark_b],
        task_run_id=task_c.id,
        snapshot=s3,
    )
    written_c, written_b = Written(table_id, s2, s3, False), Written(table_id, s1, s2, False)
    _stamp(session, task_c, written_c)
    _stamp(session, task_b, written_b)

    assert _roll_back(catalog, principal, task_c, written_c).outcome is RollbackOutcome.ROLLED_BACK
    row = _row(session)
    assert (str(row.kept_run_id), row.kept_check) == (str(task_b.run_id), "gold.orders/b")
    assert row.kept_earlier == [{"snapshot_id": None}, mark_a]

    assert _roll_back(catalog, principal, task_b, written_b).outcome is RollbackOutcome.ROLLED_BACK
    row = _row(session)
    assert (str(row.kept_run_id), row.kept_check) == (str(run_a), "gold.orders/a")
    assert row.kept_earlier == [{"snapshot_id": None}]
    assert _ids(catalog, principal) == [1]


@pytest.mark.parametrize("case", ["other-run", "other-task-of-the-run"])
def test_a_set_back_of_data_the_mark_does_not_name_leaves_mark_and_stack(
    session: Session, principal: PrincipalRef, tmp_path: Path, case: str
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _stamp(session, task, written)
    run = uuid.uuid4() if case == "other-run" else task.run_id
    stack: list[dict[str, object]] = [{"snapshot_id": written.snapshot_before}]
    _mark(session, run, stack)  # a task run of its own

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.ROLLED_BACK

    row = _row(session)
    assert (row.kept_run_id, row.kept_earlier) == (run, stack)
    (event,) = _events(session)
    assert "kept_restored" not in event.detail and "kept_not_restored" not in event.detail


def test_a_mark_without_stacked_history_becomes_one_of_unknown_checks_when_set_back(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A mark from before the stack existed has nothing to restore: readers stay refused."""
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _stamp(session, task, written)
    _mark(session, task.run_id, [], task_run_id=task.id)

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.ROLLED_BACK

    row = _row(session)
    assert (row.kept_run_id, row.kept_unknown, row.kept_earlier) == (task.run_id, True, [])
    (event,) = _events(session)
    assert event.detail["kept_unknown"] is True
    assert "not known" in (kept_message(row) or "")


def test_a_set_back_that_changed_nothing_leaves_mark_and_stack(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    before = _write(catalog, principal, _orders([1], ["eu"]), WriteMode.CREATE).snapshot_id
    after = _write(catalog, principal, _orders([2], ["eu"]), WriteMode.REPLACE).snapshot_id
    _write(catalog, principal, _orders([3], ["eu"]), WriteMode.REPLACE)  # someone else's
    task = _task(session, principal)
    written = Written(_row(session).id, before, after, publishes=False)
    _stamp(session, task, written)
    stack: list[dict[str, object]] = [{"snapshot_id": before}]
    _mark(session, task.run_id, stack, task_run_id=task.id)

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.CHANGED_SINCE

    row = _row(session)
    assert (row.kept_run_id, row.kept_earlier) == (task.run_id, stack)


def test_a_set_back_that_does_not_commit_leaves_mark_and_stack(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _stamp(session, task, written)
    stack: list[dict[str, object]] = [{"snapshot_id": written.snapshot_before}]
    _mark(session, task.run_id, stack, task_run_id=task.id)
    real = catalog._guard.record_pending
    calls: list[int] = []

    def fail_once() -> None:
        calls.append(1)
        if len(calls) == 1:  # after the swap and the restore, before the commit
            raise OperationalError("COMMIT", {}, Exception("connection lost"))
        real()

    monkeypatch.setattr(catalog._guard, "record_pending", fail_once)

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.FAILED

    row = _row(session)
    assert (row.kept_run_id, row.kept_earlier) == (task.run_id, stack)


@pytest.mark.parametrize("case", ["first-build", "first-build-without-rows"])
def test_a_first_build_that_was_kept_is_unpublished_and_keeps_its_mark(
    session: Session, principal: PrincipalRef, tmp_path: Path, case: str
) -> None:
    """There is no snapshot to set back to, so no entry describes it: the mark stays."""
    catalog = _catalog(session, principal, tmp_path)
    if case == "first-build":
        written = _arrange_unpublished(catalog, session, principal)
    else:
        _write(catalog, principal, _orders([], []), WriteMode.CREATE)
        written = Written(_row(session).id, None, None, publishes=True)
    task = _task(session, principal)
    _stamp(session, task, written)
    stack: list[dict[str, object]] = [{"snapshot_id": None}]
    _mark(session, task.run_id, stack, task_run_id=task.id)

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.UNPUBLISHED

    row = _row(session)
    assert (row.kept_run_id, row.kept_earlier) == (task.run_id, stack)
    assert str(row.unpublished_run_id) == str(task.run_id)
    (event,) = _events(session)
    assert event.detail["kept_not_restored"] is True


def _unmarked(session: Session, earlier: list[dict[str, object]], snapshot: int | None) -> None:
    """Put a stack of earlier marks and a verdict snapshot on `orders`, with no mark.

    Parameters
    ----------
    session
        Database session; commits.
    earlier
        The stack, the newest entry last.
    snapshot
        The snapshot the unmarked state is a verdict on.
    """
    session.execute(
        update(Table)
        .where(Table.name == "orders")
        .values(kept_earlier=earlier, kept_snapshot_id=snapshot)
    )
    session.commit()


def test_a_set_back_after_another_runs_clear_puts_the_mark_of_the_old_data_back(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A passing check of another run cleared the mark and judged the data now set back."""
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _stamp(session, task, written)
    mark_a = _entry(uuid.uuid4(), "gold.orders/a", written.snapshot_before)
    _unmarked(session, [{"snapshot_id": None}, mark_a], written.snapshot_after)

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.ROLLED_BACK

    row = _row(session)
    assert (str(row.kept_run_id), row.kept_check) == (mark_a["run_id"], "gold.orders/a")
    assert (row.kept_snapshot_id, row.kept_earlier) == (
        written.snapshot_before,
        [{"snapshot_id": None}],
    )
    (event,) = _events(session)
    assert event.detail["kept_restored"] == mark_a


def test_a_set_back_after_another_runs_clear_without_history_marks_the_checks_unknown(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _stamp(session, task, written)
    _unmarked(session, [], written.snapshot_after)

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.ROLLED_BACK

    row = _row(session)
    assert (row.kept_run_id, row.kept_task_run_id) == (task.run_id, task.id)
    assert (row.kept_unknown, row.kept_snapshot_id) == (True, written.snapshot_before)
    (event,) = _events(session)
    assert event.detail["kept_unknown"] is True
    described = catalog.describe_table(principal, _ref())
    assert described.state == "kept" and "not known" in (described.state_message or "")


def test_a_set_back_to_data_the_verdict_already_describes_changes_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    written = _arrange_rolled_back(catalog, session, principal)
    task = _task(session, principal)
    _stamp(session, task, written)
    stack: list[dict[str, object]] = [{"snapshot_id": None}]
    _unmarked(session, stack, written.snapshot_before)

    assert _roll_back(catalog, principal, task, written).outcome is RollbackOutcome.ROLLED_BACK

    row = _row(session)
    assert (row.kept_run_id, row.kept_unknown, row.kept_earlier) == (None, False, stack)
    (event,) = _events(session)
    assert not {"kept_restored", "kept_not_restored", "kept_unknown"} & set(event.detail)
