"""The location sweep: claims that never published are resolved, and only then their files go.

Each thing the sweep deletes has a test for the nearest thing it must not delete: a claim whose
run still runs, a claim inside its grace period, a location a table names, a location nested in
another one, a sibling directory that shares its name as a prefix, and a directory that has no
record at all. These tests run on a SQLite file; a second connection plays the other process.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from core_test_helpers import _earlier_layout, python_task_run
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_table_writes import _catalog, _ref

from lakana_core.auth import PrincipalRef
from lakana_core.catalog import location_sweep as sweep_module
from lakana_core.catalog.location_sweep import (
    UNRECORDED_MESSAGE,
    LocationSweepReport,
    sweep_table_locations,
)
from lakana_core.catalog.locations import UNRECORDED_ZONES, publish_location
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    Environment,
    Namespace,
    QueryOrigin,
    QueryRun,
    QueryRunStatus,
    Table,
    TableLocation,
    TableLocationState,
    TableType,
    TaskStatus,
)
from lakana_core.projects.code_sweep import CODE_PREFIX
from lakana_core.storage.resolver import StorageResolver

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_GRACE = 24


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    """Provide a session on a SQLite file (overrides the in-memory one).

    Parameters
    ----------
    tmp_path
        Holds the database file and, under `store/`, the environment's storage.

    Yields
    ------
    Session
        A session whose schema has been created from the models.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'catalog.db'}"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Return the storage root of `dev`.

    Parameters
    ----------
    tmp_path
        The test's directory.

    Returns
    -------
    Path
        The directory.
    """
    root = tmp_path / "store"
    root.mkdir()
    return root


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, store: Path) -> CatalogService:
    """Create `dev` on `store` and return a catalog over it.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    store
        The storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return _catalog(session, principal, store)


def _env_id(session: Session, name: str = "dev") -> uuid.UUID:
    """Return an environment's id.

    Parameters
    ----------
    session
        Database session.
    name
        The environment.

    Returns
    -------
    uuid.UUID
        The id.
    """
    found = session.scalars(select(Environment.id).where(Environment.name == name)).one()
    session.rollback()
    return found


def _location(store: Path, name: str = "orders", namespace: str = "gold") -> str:
    """Build a fresh id-suffixed location under `managed/` of the store.

    Parameters
    ----------
    store
        The storage root.
    name
        The table's name.
    namespace
        The namespace directory, ``/``-joined.

    Returns
    -------
    str
        The absolute location.
    """
    return f"file://{store}/managed/{namespace}/{name}-{uuid.uuid4().hex}"


def _claim(
    session: Session,
    location: str,
    *,
    state: TableLocationState = TableLocationState.CLAIMED,
    holder: uuid.UUID | None = None,
    claimed_at: datetime = _NOW - timedelta(hours=_GRACE + 1),
    purge_after: datetime | None = None,
    environment: str = "dev",
) -> uuid.UUID:
    """Insert a location record directly.

    Parameters
    ----------
    session
        Database session; commits.
    location
        The absolute location.
    state
        The record's state.
    holder
        Its holder run.
    claimed_at
        When it was claimed; past the grace period by default.
    purge_after
        From when it may be purged.
    environment
        The environment it belongs to.

    Returns
    -------
    uuid.UUID
        The record's id.
    """
    record_id = uuid.uuid4()
    session.add(
        TableLocation(
            id=record_id,
            environment_id=_env_id(session, environment),
            location=location,
            state=state,
            holder_run_id=holder,
            claimed_at=claimed_at,
            purge_after=purge_after,
        )
    )
    session.commit()
    return record_id


def _put(location: str, relative: str, data: bytes = b"x") -> Path:
    """Write a file below a location.

    Parameters
    ----------
    location
        An absolute ``file://`` location.
    relative
        The file's path below it.
    data
        Its bytes.

    Returns
    -------
    Path
        The file.
    """
    path = Path(location.removeprefix("file://")) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _files(location: str) -> list[str]:
    """List the files below a location.

    Parameters
    ----------
    location
        An absolute ``file://`` location.

    Returns
    -------
    list of str
        Paths relative to it, sorted.
    """
    root = Path(location.removeprefix("file://"))
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _record(session: Session, location: str) -> TableLocation | None:
    """Read a record by location, fresh from the database.

    Parameters
    ----------
    session
        Database session.
    location
        The location.

    Returns
    -------
    TableLocation or None
        The record, or `None`.
    """
    session.rollback()
    found = session.scalars(
        select(TableLocation)
        .where(TableLocation.location == location)
        .execution_options(populate_existing=True)
    ).one_or_none()
    session.rollback()
    return found


def _sweep(session: Session, now: datetime = _NOW, **options: object) -> LocationSweepReport:
    """Run the sweep once.

    Parameters
    ----------
    session
        Database session.
    now
        The sweep's time.
    **options
        Passed through.

    Returns
    -------
    LocationSweepReport
        What it did.
    """
    return sweep_table_locations(
        session,
        StorageResolver(session),
        now=now,
        grace_hours=_GRACE,
        **options,  # ty: ignore[invalid-argument-type]
    )


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one kind.

    Parameters
    ----------
    session
        Database session.
    kind
        The event type.

    Returns
    -------
    list of AuditEvent
        Oldest first.
    """
    found = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == kind)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )
    session.rollback()
    return found


def _query_run(session: Session, status: QueryRunStatus) -> uuid.UUID:
    """Insert a query run in a status.

    Parameters
    ----------
    session
        Database session; commits.
    status
        Its status.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    run_id = uuid.uuid4()
    session.add(
        QueryRun(
            origin=QueryOrigin.WRITTEN,
            id=run_id,
            environment_id=_env_id(session),
            sql_text="SELECT 1",
            status=status,
        )
    )
    session.commit()
    return run_id


def _schema() -> Schema:
    """Build a one-column schema.

    Returns
    -------
    Schema
        ``id bigint``.
    """
    return Schema(NestedField(1, "id", LongType(), required=False))


# --- step 2: claims -------------------------------------------------------------------------


@pytest.mark.parametrize("status", [QueryRunStatus.QUEUED, QueryRunStatus.RUNNING])
def test_a_claim_whose_query_run_still_runs_is_kept(
    session: Session, catalog: CatalogService, store: Path, status: QueryRunStatus
) -> None:
    """However old: the run may still be writing data files."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(
        session, location, holder=_query_run(session, status), claimed_at=_NOW - timedelta(days=30)
    )

    report = _sweep(session)

    assert report.claims_kept == 1
    record = _record(session, location)
    assert record is not None and record.state is TableLocationState.CLAIMED
    assert _files(location) == ["data/a.parquet"]


def test_a_claim_whose_task_run_still_runs_is_kept(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A Python task's create holds its claim by its task run."""
    task = python_task_run(session, principal.id, status=TaskStatus.READY)
    location = _location(store)
    _claim(session, location, holder=task.id)

    assert _sweep(session).claims_kept == 1
    record = _record(session, location)
    assert record is not None and record.state is TableLocationState.CLAIMED


@pytest.mark.parametrize(
    "status", [QueryRunStatus.SUCCEEDED, QueryRunStatus.FAILED, QueryRunStatus.CANCELLED]
)
def test_a_claim_whose_query_run_ended_is_abandoned_and_purged(
    session: Session, catalog: CatalogService, store: Path, status: QueryRunStatus
) -> None:
    """No grace period applies once the holder has ended: no late commit can land any more."""
    location = _location(store)
    _put(location, "metadata/00000-a.metadata.json", b"{}")
    _put(location, "data/a.parquet", b"12345")
    run_id = _query_run(session, status)
    _claim(session, location, holder=run_id, claimed_at=_NOW - timedelta(minutes=5))

    report = _sweep(session)

    assert (report.claims_abandoned, report.locations_purged) == (1, 1)
    assert (report.files_deleted, report.bytes_deleted) == (2, 7)
    assert _files(location) == []
    assert _record(session, location) is None
    (abandoned,) = _events(session, AuditEventType.TABLE_LOCATION_ABANDONED)
    assert abandoned.principal_id is None
    assert abandoned.securable_environment == "dev"
    relative = location.removeprefix(f"file://{store}/")
    assert abandoned.detail == {
        "location": relative,
        "holder_run_id": str(run_id),
        "claimed_at": (_NOW - timedelta(minutes=5)).isoformat(),
    }
    (purged,) = _events(session, AuditEventType.TABLE_PURGED)
    assert purged.principal_id is None
    assert purged.detail == {
        "location": relative,
        "state": "ABANDONED",
        "files": 2,
        "bytes": 7,
        "complete": True,
    }


def test_a_claim_whose_task_run_ended_is_abandoned(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A failed Python task's create is resolved at once."""
    task = python_task_run(session, principal.id, status=TaskStatus.FAILED)
    location = _location(store)
    _claim(session, location, holder=task.id, claimed_at=_NOW)

    assert _sweep(session).claims_abandoned == 1


def test_a_claim_without_a_holder_waits_for_the_grace_period(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """A person's create has no run to end: only time tells that it is over."""
    young, old = _location(store, "young"), _location(store, "old")
    _put(young, "metadata/a.json")
    _put(old, "metadata/a.json")
    _claim(session, young, claimed_at=_NOW - timedelta(hours=_GRACE) + timedelta(seconds=1))
    _claim(session, old, claimed_at=_NOW - timedelta(hours=_GRACE))

    report = _sweep(session)

    assert (report.claims_kept, report.claims_abandoned) == (1, 1)
    young_record = _record(session, young)
    assert young_record is not None and young_record.state is TableLocationState.CLAIMED
    assert _files(young) == ["metadata/a.json"]
    assert _record(session, old) is None
    assert _files(old) == []


def test_a_holder_the_catalog_does_not_know_waits_for_the_grace_period(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """A run row that is gone proves nothing: the claim is treated as one without a holder."""
    location = _location(store)
    _claim(session, location, holder=uuid.uuid4(), claimed_at=_NOW - timedelta(hours=1))
    assert _sweep(session).claims_kept == 1
    assert _sweep(session, _NOW + timedelta(hours=_GRACE)).claims_abandoned == 1


def test_an_in_doubt_create_that_landed_late_is_made_live_and_keeps_its_files(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A table row names the location: the claim becomes LIVE, nothing is deleted."""
    loaded = catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    record = session.scalars(select(TableLocation)).one()
    table_id = record.table_id
    session.execute(
        update(TableLocation).values(
            state=TableLocationState.CLAIMED, table_id=None, claimed_at=_NOW
        )
    )  # as a create in doubt leaves it; claimed on the test's clock, not the wall clock
    session.commit()
    files = _files(record.location)

    report = _sweep(session, _NOW + timedelta(days=2))

    assert (report.claims_published, report.claims_abandoned) == (1, 0)
    found = _record(session, record.location)
    assert found is not None
    assert (found.state, found.table_id) == (TableLocationState.LIVE, table_id)
    assert _files(record.location) == files
    assert catalog.load_table(principal, _ref("gold.orders")).metadata == loaded.metadata


def test_an_in_doubt_create_that_did_not_land_is_abandoned_and_removed(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """No table row names it after the grace period: the files go, then the record."""
    location = _location(store)
    _put(location, "metadata/00000-x.metadata.json", b"{}")
    _claim(session, location)

    report = _sweep(session)

    assert (report.claims_abandoned, report.locations_purged) == (1, 1)
    assert _files(location) == []
    assert _record(session, location) is None


def test_a_claim_published_between_the_listing_and_the_lock_is_left_live(
    session: Session, catalog: CatalogService, store: Path, tmp_path: Path
) -> None:
    """The race on SQLite: the publish commits first; under the lock the sweep sees LIVE."""
    location = _location(store)
    _put(location, "metadata/a.json")
    _claim(session, location)
    table_id = uuid.uuid4()

    calls: list[int] = []

    with Session(session.get_bind()) as publisher:

        def publish_once() -> bool:
            calls.append(1)
            # The first check is the run's own; the second comes after the claims were listed.
            if len(calls) == 2 and publish_location(
                publisher, location=location, table_id=table_id
            ):
                publisher.commit()
            return False

        report = _sweep(session, should_stop=publish_once)

    assert (report.claims_abandoned, report.locations_purged) == (0, 0)
    record = _record(session, location)
    assert record is not None and record.state is TableLocationState.LIVE
    assert _files(location) == ["metadata/a.json"]


def test_a_claim_abandoned_by_the_sweep_refuses_a_later_publish(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """The other order: once the sweep committed ABANDONED, the publish fence moves nothing."""
    location = _location(store)
    _claim(session, location, purge_after=None)
    with mock.patch.object(sweep_module._Run, "purge", lambda self, record_id: None):
        assert _sweep(session).claims_abandoned == 1
    with Session(session.get_bind()) as publisher:
        assert not publish_location(publisher, location=location, table_id=uuid.uuid4())
        publisher.rollback()


# --- step 3: purges -------------------------------------------------------------------------


def test_a_dropped_location_is_purged_like_an_abandoned_one(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """#94 only adds the drop: a DROPPED record past `purge_after` goes the same way."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.DROPPED, purge_after=_NOW)

    report = _sweep(session)

    assert report.locations_purged == 1
    assert _record(session, location) is None
    (purged,) = _events(session, AuditEventType.TABLE_PURGED)
    assert purged.detail["state"] == "DROPPED"


def test_a_purgeable_location_waits_for_purge_after(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """Before its time, and without a time at all, nothing of it goes."""
    later, never = _location(store, "later"), _location(store, "never")
    for location in (later, never):
        _put(location, "data/a.parquet")
    _claim(
        session,
        later,
        state=TableLocationState.DROPPED,
        purge_after=_NOW + timedelta(seconds=1),
    )
    _claim(session, never, state=TableLocationState.DROPPED, purge_after=None)

    report = _sweep(session)

    assert report.locations_purged == 0
    assert _files(later) == _files(never) == ["data/a.parquet"]


def test_the_purge_deletes_the_files_first_and_the_record_last_and_resumes(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """Stopped half-way, the record stays and the next run finishes the files, then the record."""
    location = _location(store)
    for index in range(4):
        _put(location, f"data/{index}.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    seen: list[str] = []
    real = sweep_module.delete_quietly

    def stop_after_two(storage: object, path: str) -> bool:
        seen.append(path)
        return real(storage, path)  # ty: ignore[invalid-argument-type]

    calls: list[int] = []

    def should_stop() -> bool:
        calls.append(1)
        return len(seen) >= 2

    with mock.patch.object(sweep_module, "delete_quietly", stop_after_two):
        first = _sweep(session, should_stop=should_stop)

    assert first.stopped_early
    assert first.files_deleted == 2
    assert len(_files(location)) == 2
    record = _record(session, location)
    assert record is not None and record.state is TableLocationState.ABANDONED
    (partial,) = _events(session, AuditEventType.TABLE_PURGED)
    assert (partial.detail["files"], partial.detail["complete"]) == (2, False)

    second = _sweep(session)

    assert (second.files_deleted, second.locations_purged) == (2, 1)
    assert _files(location) == []
    assert _record(session, location) is None
    assert [e.detail["complete"] for e in _events(session, AuditEventType.TABLE_PURGED)] == [
        False,
        True,
    ]


def test_a_failed_delete_keeps_the_record(
    session: Session, catalog: CatalogService, store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that could not be deleted keeps the record, so the next run tries again."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    caplog.set_level(logging.WARNING)
    with mock.patch.object(sweep_module, "delete_quietly", side_effect=PermissionError("no")):
        report = _sweep(session)

    assert report.locations_purged == 0
    assert report.failed_records == 1
    assert _record(session, location) is not None
    assert "PermissionError" in caplog.text
    assert _sweep(session).locations_purged == 1


def test_a_nested_live_table_blocks_the_purge(
    session: Session,
    catalog: CatalogService,
    store: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A namespace named after the abandoned directory holds a live table: nothing goes."""
    outer_name = f"x-{uuid.uuid4().hex}"
    outer = f"file://{store}/managed/gold/{outer_name}"
    _put(outer, "data/a.parquet")
    _claim(session, outer, state=TableLocationState.ABANDONED, purge_after=_NOW)
    catalog.create_namespace(principal, "dev", _ref(f"gold.{outer_name}.t").namespace)
    catalog.create_table(principal, _ref(f"gold.{outer_name}.inner"), schema=_schema())
    inner_files = [p for p in _files(outer) if p.startswith("inner-")]
    caplog.set_level(logging.WARNING)

    report = _sweep(session)

    assert (report.purges_blocked, report.locations_purged, report.files_deleted) == (1, 0, 0)
    assert "data/a.parquet" in _files(outer)
    assert [p for p in _files(outer) if p.startswith("inner-")] == inner_files
    assert _record(session, outer) is not None
    assert "uses this location or one inside it" in caplog.text


def test_a_nested_claim_or_table_row_blocks_the_purge_too(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """A create still writing inside it, or a table row without a record, protect it as well."""
    outer = _location(store, "outer")
    _put(outer, "data/a.parquet")
    _claim(session, outer, state=TableLocationState.ABANDONED, purge_after=_NOW)
    _claim(
        session,
        f"{outer}/nested-{uuid.uuid4().hex}",
        holder=_query_run(session, QueryRunStatus.RUNNING),
    )
    assert _sweep(session).purges_blocked == 1

    session.execute(
        update(TableLocation)
        .where(TableLocation.location != outer)
        .values(state=TableLocationState.ABANDONED, purge_after=_NOW + timedelta(days=9))
    )
    namespace = session.scalars(select(Namespace.id)).first()
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=namespace,
            name="unrecorded",
            table_type=TableType.ICEBERG,
            metadata_location=f"{outer}/deeper/metadata/00000-a.metadata.json",
        )
    )
    session.commit()
    assert _sweep(session).purges_blocked == 1
    assert "data/a.parquet" in _files(outer)


def _after_first_delete(location: str, change: Callable[[], None]) -> Callable[[], bool]:
    """Build a stop check that makes a change once the first file of a location is gone.

    The check runs before every decision, outside the record's lock, so another connection can
    write then (on SQLite the lock is the whole database's).

    Parameters
    ----------
    location
        The location being purged.
    change
        What another process does then.

    Returns
    -------
    callable
        The check; it never asks the run to stop.
    """
    total = len(_files(location))
    done: list[int] = []

    def check() -> bool:
        if not done and len(_files(location)) < total:
            done.append(1)
            change()
        return False

    return check


def test_a_table_created_inside_during_the_purge_stops_it(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """The check runs again before each delete, not only once."""
    outer = _location(store, "outer")
    for index in range(3):
        _put(outer, f"data/{index}.parquet")
    _claim(session, outer, state=TableLocationState.ABANDONED, purge_after=_NOW)

    def claim_inside() -> None:
        with Session(session.get_bind()) as other:
            other.add(
                TableLocation(
                    id=uuid.uuid4(),
                    environment_id=_env_id(other),
                    location=f"{outer}/new-{uuid.uuid4().hex}",
                    state=TableLocationState.CLAIMED,
                    claimed_at=_NOW,
                )
            )
            other.commit()

    report = _sweep(session, should_stop=_after_first_delete(outer, claim_inside))

    assert report.purges_blocked == 1
    assert len(_files(outer)) == 2
    assert _record(session, outer) is not None
    (partial,) = _events(session, AuditEventType.TABLE_PURGED)
    assert (partial.detail["files"], partial.detail["complete"]) == (1, False)


def test_a_record_that_changed_during_the_purge_stops_it(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """An undo that made the location LIVE again between two files keeps the rest."""
    location = _location(store)
    for index in range(3):
        _put(location, f"data/{index}.parquet")
    _claim(session, location, state=TableLocationState.DROPPED, purge_after=_NOW)

    def undo() -> None:
        with Session(session.get_bind()) as other:
            other.execute(update(TableLocation).values(state=TableLocationState.LIVE))
            other.commit()

    report = _sweep(session, should_stop=_after_first_delete(location, undo))

    assert report.locations_purged == 0
    assert len(_files(location)) == 2
    record = _record(session, location)
    assert record is not None and record.state is TableLocationState.LIVE


def test_the_purge_stays_inside_the_exact_location(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """A sibling that shares the location as a text prefix, and its parent, are never touched."""
    location = _location(store)
    _put(location, "data/a.parquet")
    sibling = f"{location}x"
    _put(sibling, "data/b.parquet")
    parent_file = Path(location.removeprefix("file://")).parent / "keep.txt"
    parent_file.write_bytes(b"keep")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)

    report = _sweep(session)

    assert report.locations_purged == 1
    assert _files(sibling) == ["data/b.parquet"]
    assert parent_file.read_bytes() == b"keep"


def test_a_listing_that_names_something_outside_the_location_is_left_alone(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """A backend that answers a prefix with a sibling's keys (an object store) deletes none."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    relative = location.removeprefix(f"file://{store}/")
    storage = StorageResolver(session).for_environment("dev").storage
    real = type(storage).list_objects

    def list_with_strangers(self: object, prefix: str) -> Iterator[object]:
        yield from real(self, prefix)  # ty: ignore[invalid-argument-type]
        for path in (f"{relative}x/data/b.parquet", f"{relative}/../evil", f"{relative}//y"):
            yield sweep_module.StoredObject(path=path, size=1, modified_at=None)

    with mock.patch.object(type(storage), "list_objects", list_with_strangers):
        report = _sweep(session)

    assert report.left_alone == 3
    assert report.files_deleted == 1
    assert report.locations_purged == 0  # what it could not judge keeps the record
    assert _record(session, location) is not None


@pytest.mark.parametrize(
    "where",
    [
        "file:///elsewhere/managed/gold/t-{hex}",
        "{root}/raw/gold/t-{hex}",
        "{root}/managed/gold/legacy",
        "{root}/managed/gold/../../x-{hex}",
        "{root}/managed",
    ],
)
def test_a_location_outside_managed_or_without_an_id_is_never_purged(
    session: Session,
    catalog: CatalogService,
    store: Path,
    where: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only an id-suffixed directory under `managed/` of the environment's root is purged."""
    location = where.format(root=f"file://{store}", hex=uuid.uuid4().hex)
    if location.startswith(f"file://{store}") and ".." not in location:
        _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    caplog.set_level(logging.WARNING)

    report = _sweep(session)

    assert (report.locations_refused, report.files_deleted, report.locations_purged) == (1, 0, 0)
    assert _record(session, location) is not None
    assert "is not a table directory" in caplog.text


# --- directories without a record -----------------------------------------------------------


def test_a_directory_with_no_record_is_never_deleted_and_is_reported_once(
    session: Session,
    catalog: CatalogService,
    store: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A restored catalog may have forgotten it: it is counted and logged, never removed."""
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    forgotten = [_location(store, "a"), _location(store, "b", "silver/deep")]
    for location in forgotten:
        _put(location, "data/x.parquet")
    empty = Path(_location(store, "empty").removeprefix("file://"))
    empty.mkdir(parents=True)  # what a purge leaves on a local disk
    caplog.set_level(logging.WARNING)

    report = _sweep(session)

    assert report.unrecorded_directories == 2
    assert all(_files(location) == ["data/x.parquet"] for location in forgotten)
    lines = [r.getMessage() for r in caplog.records if "no location record" in r.getMessage()]
    assert lines == [UNRECORDED_MESSAGE % (2, "dev")]


def test_environments_on_one_storage_root_count_a_directory_once(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A directory another environment on the same root records is not unrecorded."""
    catalog.create_environment(
        principal,
        name="test",
        storage=StorageConfig(scheme="file", root=f"file://{store}", options={}),
    )
    theirs = _location(store)
    _put(theirs, "data/x.parquet")
    _claim(session, theirs, state=TableLocationState.LIVE, environment="test")
    forgotten = _location(store, "forgotten")
    _put(forgotten, "data/x.parquet")

    assert _sweep(session).unrecorded_directories == 1


def test_a_live_location_in_another_environment_on_the_root_blocks_a_purge(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Environments can share a root: the in-use check looks at every environment."""
    catalog.create_environment(
        principal,
        name="test",
        storage=StorageConfig(scheme="file", root=f"file://{store}", options={}),
    )
    outer = _location(store, "outer")
    _put(outer, "data/a.parquet")
    _claim(session, outer, state=TableLocationState.ABANDONED, purge_after=_NOW)
    _claim(
        session, f"{outer}/t-{uuid.uuid4().hex}", state=TableLocationState.LIVE, environment="test"
    )

    assert _sweep(session).purges_blocked == 1


# --- bounds ---------------------------------------------------------------------------------


def test_the_deadline_stops_the_run(session: Session, catalog: CatalogService, store: Path) -> None:
    """Past `max_seconds` no further decision is taken; the next run continues."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location)
    ticks = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0])

    report = _sweep(session, max_seconds=10.0, monotonic=lambda: next(ticks, 1000.0))

    assert report.stopped_early
    assert _sweep(session).locations_purged == 1


def test_repeated_failures_give_an_environment_up_for_the_run(
    session: Session, catalog: CatalogService, store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """After `MAX_CONSECUTIVE_FAILURES` records in a row fail, the environment is skipped."""
    for index in range(sweep_module.MAX_CONSECUTIVE_FAILURES + 2):
        location = _location(store, f"t{index}")
        _put(location, "data/a.parquet")
        _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    caplog.set_level(logging.WARNING)
    with mock.patch.object(sweep_module, "delete_quietly", side_effect=OSError("down")):
        report = _sweep(session)

    assert report.failed_records == sweep_module.MAX_CONSECUTIVE_FAILURES
    assert report.failed_environments == 1
    assert "repeated failures" in caplog.text


def test_an_environment_whose_storage_cannot_be_resolved_is_skipped(
    session: Session, catalog: CatalogService, store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One environment's broken storage does not stop the others."""
    caplog.set_level(logging.WARNING)
    with mock.patch.object(StorageResolver, "for_environment", side_effect=OSError("gone")):
        report = _sweep(session)
    assert report.failed_environments == 1
    assert "skipped environment dev (OSError)" in caplog.text


def test_the_start_rotates_between_environments_and_records(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Each run starts one environment and one record further, so no backlog always waits."""
    catalog.create_environment(
        principal,
        name="test",
        storage=StorageConfig(scheme="file", root=f"file://{store}/t", options={}),
    )
    order: list[str] = []
    real = sweep_module._Run.environment

    def record(
        self: sweep_module._Run, environment_id: uuid.UUID, rotate: int, expiries: int
    ) -> None:
        order.append(self._environment)
        real(self, environment_id, rotate, expiries)

    with mock.patch.object(sweep_module._Run, "environment", record):
        _sweep(session, rotation=0)
        _sweep(session, rotation=1)
    assert order == ["dev", "test", "test", "dev"]


def test_a_run_that_did_nothing_logs_nothing(
    session: Session, catalog: CatalogService, caplog: pytest.LogCaptureFixture
) -> None:
    """An idle sweep is silent; a busy one writes one summary line."""
    caplog.set_level(logging.INFO, logger=sweep_module.__name__)
    assert _sweep(session) == LocationSweepReport()
    assert caplog.records == []


def test_a_busy_run_writes_one_summary_line(
    session: Session, catalog: CatalogService, store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The line says what was abandoned and purged."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location)
    caplog.set_level(logging.INFO, logger=sweep_module.__name__)
    _sweep(session)
    (line,) = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert line.startswith(
        "Table location sweep: 0 tables expired, 0 expiries postponed, 1 claims abandoned"
    )


def test_no_transaction_is_left_open(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """Whatever happened, the session has no transaction afterwards."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location)
    _sweep(session)
    assert not session.in_transaction()


def test_symlinks_and_files_under_managed_are_not_followed(
    session: Session, catalog: CatalogService, store: Path, tmp_path: Path
) -> None:
    """The walk for unrecorded directories never leaves the root by a link."""
    outside = tmp_path / "outside" / f"t-{uuid.uuid4().hex}"
    outside.mkdir(parents=True)
    (outside / "x").write_bytes(b"x")
    managed = store / "managed"
    managed.mkdir(exist_ok=True)
    os.symlink(outside.parent, managed / "linked")
    (managed / "stray.txt").write_bytes(b"x")
    assert _sweep(session).unrecorded_directories == 0
    assert (outside / "x").exists()


def _on_call(number: int, change: Callable[[], None]) -> Callable[[], bool]:
    """Build a stop check that makes a change on its n-th call and never stops the run.

    Parameters
    ----------
    number
        Which call (1 is the run's own check before the first environment).
    change
        What another process does then.

    Returns
    -------
    callable
        The check.
    """
    calls: list[int] = []

    def check() -> bool:
        calls.append(1)
        if len(calls) == number:
            change()
        return False

    return check


def _set_state(session: Session, state: TableLocationState | None) -> Callable[[], None]:
    """Build a change that sets every record's state, or deletes every record, from elsewhere.

    Parameters
    ----------
    session
        The sweep's session; its engine is used, not its connection.
    state
        The new state; `None` deletes the records.

    Returns
    -------
    callable
        The change.
    """

    def change() -> None:
        with Session(session.get_bind()) as other:
            if state is None:
                other.execute(sa_delete(TableLocation))
            else:
                other.execute(update(TableLocation).values(state=state))
            other.commit()

    return change


@pytest.mark.parametrize("state", [None, TableLocationState.LIVE])
def test_a_claim_or_record_that_changed_after_the_listing_is_left_alone(
    session: Session, catalog: CatalogService, store: Path, state: TableLocationState | None
) -> None:
    """Released by its create, or published, between the listing and the step: no action."""
    claimed, purgeable = _location(store, "claimed"), _location(store, "purgeable")
    _claim(session, claimed)
    assert _sweep(session, should_stop=_on_call(2, _set_state(session, state))) == (
        LocationSweepReport()
    )
    session.execute(sa_delete(TableLocation))
    session.commit()
    _claim(session, purgeable, state=TableLocationState.ABANDONED, purge_after=_NOW)
    _put(purgeable, "data/a.parquet")
    report = _sweep(session, should_stop=_on_call(2, _set_state(session, state)))
    assert (report.files_deleted, report.locations_purged) == (0, 0)
    assert _files(purgeable) == ["data/a.parquet"]


def test_a_record_that_changed_before_it_was_forgotten_is_kept(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """Every file is gone, then an undo makes it LIVE: the record stays, the pass is recorded."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.DROPPED, purge_after=_NOW)

    report = _sweep(
        session,
        should_stop=_after_first_delete(location, _set_state(session, TableLocationState.LIVE)),
    )

    assert (report.files_deleted, report.locations_purged) == (1, 0)
    record = _record(session, location)
    assert record is not None and record.state is TableLocationState.LIVE
    (partial,) = _events(session, AuditEventType.TABLE_PURGED)
    assert partial.detail["complete"] is False


def test_a_table_created_inside_before_the_record_is_forgotten_keeps_it(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """The last check, under the lock that deletes the record, sees a claim made inside."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)

    def claim_inside() -> None:
        with Session(session.get_bind()) as other:
            other.add(
                TableLocation(
                    id=uuid.uuid4(),
                    environment_id=_env_id(other),
                    location=f"{location}/new-{uuid.uuid4().hex}",
                    state=TableLocationState.CLAIMED,
                    claimed_at=_NOW,
                )
            )
            other.commit()

    report = _sweep(session, should_stop=_after_first_delete(location, claim_inside))

    assert (report.purges_blocked, report.locations_purged) == (1, 0)
    assert _record(session, location) is not None


def test_a_file_already_gone_is_not_counted(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """Another process deleted it first: nothing to count, and the record still goes."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    with mock.patch.object(sweep_module, "delete_quietly", return_value=False):
        report = _sweep(session)
    assert (report.files_deleted, report.locations_purged) == (0, 1)


def test_a_part_way_purge_that_cannot_be_recorded_is_logged(
    session: Session, catalog: CatalogService, store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The audit of a stopped pass failing does not hide the stop."""
    location = _location(store)
    for index in range(2):
        _put(location, f"data/{index}.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    stop = _after_first_delete(location, lambda: None)
    stopped: list[int] = []

    def stop_after_one() -> bool:
        stop()
        if len(_files(location)) < 2:
            stopped.append(1)
        return bool(stopped)

    caplog.set_level(logging.WARNING)
    with mock.patch.object(sweep_module._Run, "_audit_purge", side_effect=RuntimeError("audit")):
        report = _sweep(session, should_stop=stop_after_one)
    assert report.stopped_early
    assert "could not record a part-way purge (RuntimeError)" in caplog.text


def test_a_run_asked_to_stop_at_once_does_nothing(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """Shutdown before the first environment: stopped early, nothing touched."""
    location = _location(store)
    _claim(session, location)
    report = _sweep(session, should_stop=lambda: True)
    assert report == LocationSweepReport(stopped_early=True)


def test_a_table_nested_inside_a_claim_does_not_resolve_it(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Only the table whose own location is the claim's makes it LIVE; a nested one blocks only.

    A namespace named like the claimed directory holds a live table: the claim of the create that
    failed is still abandoned (it names no table), and its purge stays blocked by the table.
    """
    outer_name = f"x-{uuid.uuid4().hex}"
    outer = f"file://{store}/managed/gold/{outer_name}"
    _put(outer, "metadata/00000-a.metadata.json", b"{}")
    _claim(session, outer, holder=_query_run(session, QueryRunStatus.FAILED))
    catalog.create_namespace(principal, "dev", _ref(f"gold.{outer_name}.t").namespace)
    catalog.create_table(principal, _ref(f"gold.{outer_name}.inner"), schema=_schema())

    report = _sweep(session)

    assert (report.claims_published, report.claims_abandoned) == (0, 1)
    assert (report.purges_blocked, report.locations_purged) == (1, 0)
    record = _record(session, outer)
    assert record is not None
    assert (record.state, record.table_id) == (TableLocationState.ABANDONED, None)
    assert "metadata/00000-a.metadata.json" in _files(outer)


# --- dropped tables (#94) --------------------------------------------------------------------


def test_the_purge_is_marked_started_under_the_lock_before_its_first_delete(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """An undrop that comes after the mark sees it; the mark is committed before any file goes."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.DROPPED, purge_after=_NOW)
    marks: list[datetime | None] = []
    real = sweep_module.delete_quietly

    def look_first(storage: object, path: str) -> bool:
        with Session(session.get_bind()) as other:
            marks.append(
                other.scalar(
                    select(TableLocation.purge_started_at).where(TableLocation.location == location)
                )
            )
        return real(storage, path)  # ty: ignore[invalid-argument-type]

    with mock.patch.object(sweep_module, "delete_quietly", look_first):
        report = _sweep(session)

    assert report.locations_purged == 1
    assert marks == [_NOW]


def test_a_record_no_longer_due_when_the_purge_starts_is_left_alone(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """Undropped and dropped again with a new period after the listing: nothing goes, no mark."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.DROPPED, purge_after=_NOW)
    real = sweep_module._Run._in_use

    def dropped_again(run: object, checked: str) -> bool:
        with Session(session.get_bind()) as other:
            other.execute(update(TableLocation).values(purge_after=_NOW + timedelta(days=7)))
            other.commit()
        return real(run, checked)  # ty: ignore[invalid-argument-type]

    with mock.patch.object(sweep_module._Run, "_in_use", dropped_again):
        report = _sweep(session)

    assert (report.locations_purged, report.files_deleted) == (0, 0)
    assert _files(location) == ["data/a.parquet"]
    record = _record(session, location)
    assert record is not None and record.purge_started_at is None


def test_a_record_undropped_when_the_purge_starts_is_left_alone(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """The record went back to LIVE after the listing: the mark is not set, nothing goes."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.DROPPED, purge_after=_NOW)

    def undropped(run: object, checked: str) -> bool:
        with Session(session.get_bind()) as other:
            other.execute(update(TableLocation).values(state=TableLocationState.LIVE))
            other.commit()
        return False

    with mock.patch.object(sweep_module._Run, "_in_use", undropped):
        report = _sweep(session)

    assert report.files_deleted == 0
    assert _files(location) == ["data/a.parquet"]


def _metadata_root(catalog: CatalogService, principal: PrincipalRef, dotted: str) -> str:
    """Return a table's location, from its metadata file.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The reader.
    dotted
        The table.

    Returns
    -------
    str
        Its root location.
    """
    metadata_location = catalog.load_table(principal, _ref(dotted)).metadata_location
    return metadata_location.rsplit("/metadata/", 1)[0]


def test_a_dropped_table_is_purged_after_its_undo_period_and_not_before(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Before `purge_after` everything stays; after it the files and the record go, audited."""
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    location = _metadata_root(catalog, principal, "gold.orders")
    _put(location, "data/a.parquet")
    catalog.drop_table(principal, _ref("gold.orders"))
    record = _record(session, location)
    assert record is not None and record.purge_after is not None

    early = _sweep(session, now=record.purge_after - timedelta(seconds=1))

    assert early.files_deleted == 0
    assert "data/a.parquet" in _files(location)

    late = _sweep(session, now=record.purge_after)

    assert late.locations_purged == 1
    assert _files(location) == []
    assert _record(session, location) is None
    (purged,) = _events(session, AuditEventType.TABLE_PURGED)
    assert purged.detail["state"] == "DROPPED"
    assert purged.detail["complete"] is True
    assert purged.detail["files"] == 2


def test_an_undropped_table_is_never_purged(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Restored within the period, the record is LIVE again and the sweep leaves it."""
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    location = _metadata_root(catalog, principal, "gold.orders")
    catalog.drop_table(principal, _ref("gold.orders"))
    catalog.undrop_table(principal, _ref("gold.orders"))

    report = _sweep(session, now=_NOW + timedelta(days=3650))

    assert report.files_deleted == 0
    assert len(_files(location)) == 1
    catalog.load_table(principal, _ref("gold.orders"))


def test_with_no_undo_period_the_next_sweep_purges_a_drop(
    session: Session, store: Path, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`LAKANA_TABLES__UNDO_DAYS=0`: due at the drop's own time."""
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    location = _metadata_root(catalog, principal, "gold.orders")
    immediate = CatalogService(session, StorageResolver(session), catalog._authz, undo_days=0)
    immediate.drop_table(principal, _ref("gold.orders"))
    record = _record(session, location)
    assert record is not None and record.purge_after is not None
    assert record.purge_after == record.dropped_at

    assert _sweep(session, now=record.purge_after).locations_purged == 1
    assert _files(location) == []


def test_an_external_tables_files_are_never_purged(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """It has no record, so no sweep, however late, deletes its raw files."""
    raw = store / "raw" / "events" / "a.csv"
    raw.parent.mkdir(parents=True)
    raw.write_text("id\n1\n")
    ref = _ref("bronze.events")
    catalog.create_external_table(principal, ref, location_pattern="raw/events/*.csv", format="csv")
    catalog.drop_table(principal, ref, purge=True)

    report = _sweep(session, now=_NOW + timedelta(days=3650))

    assert report.files_deleted == 0
    assert raw.read_text() == "id\n1\n"


def _outer_and_inner(catalog: CatalogService, principal: PrincipalRef) -> tuple[str, str, str]:
    """Create `gold.outer` and a table nested in its directory, through a namespace of its name.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The creator.

    Returns
    -------
    tuple of str
        The outer location, the inner location and the inner table's dotted name.
    """
    catalog.create_table(principal, _ref("gold.outer"), schema=_schema())
    outer = _metadata_root(catalog, principal, "gold.outer")
    directory = outer.rsplit("/", 1)[1]
    catalog.create_namespace(principal, "dev", _ref(f"gold.{directory}.t").namespace)
    dotted = f"gold.{directory}.inner"
    catalog.create_table(principal, _ref(dotted), schema=_schema())
    inner = _metadata_root(catalog, principal, dotted)
    assert inner.startswith(outer + "/")
    return outer, inner, dotted


def test_a_dropped_table_nested_inside_blocks_the_purge_until_it_is_restored(
    session: Session,
    catalog: CatalogService,
    store: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its undo period is honoured: the outer purge waits, and the inner table can come back."""
    outer, inner, dotted = _outer_and_inner(catalog, principal)
    inner_files = _files(inner)
    catalog.drop_table(principal, _ref(dotted))
    catalog.drop_table(principal, _ref("gold.outer"), purge=True)
    record = _record(session, outer)
    assert record is not None and record.purge_after is not None
    caplog.set_level(logging.WARNING)

    report = _sweep(session, now=record.purge_after + timedelta(seconds=1))

    assert (report.purges_waiting, report.files_deleted, report.locations_purged) == (1, 0, 0)
    assert _files(inner) == inner_files
    waiting = _record(session, outer)
    assert waiting is not None and waiting.purge_started_at is None
    assert "waits for a dropped table inside it" in caplog.text
    restored = catalog.undrop_table(principal, _ref(dotted))
    assert restored.metadata_location.startswith(inner + "/")
    assert _files(inner) == inner_files


def test_the_outer_purge_follows_once_the_nested_drop_is_purged_itself(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """The outer record stays due and is purged by the first sweep after the inner one's."""
    outer, inner, dotted = _outer_and_inner(catalog, principal)
    catalog.drop_table(principal, _ref(dotted))
    catalog.drop_table(principal, _ref("gold.outer"), purge=True)
    inner_record = _record(session, inner)
    assert inner_record is not None and inner_record.purge_after is not None
    late = inner_record.purge_after + timedelta(seconds=1)

    first = _sweep(session, now=late)

    assert (first.purges_waiting, first.locations_purged) == (1, 1)
    assert _record(session, inner) is None
    assert _record(session, outer) is not None

    second = _sweep(session, now=late)

    assert (second.purges_waiting, second.locations_purged) == (0, 1)
    assert _record(session, outer) is None
    assert _files(outer) == []


def test_a_table_dropped_inside_during_the_purge_stops_it(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """A tombstone that appears inside between two deletes stops the pass; the rest stays."""
    location = _location(store)
    for index in range(3):
        _put(location, f"data/{index}.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)

    def drop_inside() -> None:
        with Session(session.get_bind()) as other:
            other.add(
                TableLocation(
                    id=uuid.uuid4(),
                    environment_id=_env_id(other),
                    location=f"{location}/nested-{uuid.uuid4().hex}",
                    state=TableLocationState.DROPPED,
                    claimed_at=_NOW,
                    purge_after=_NOW + timedelta(days=7),
                    revoked_policy_ids=[],
                )
            )
            other.commit()

    report = _sweep(session, should_stop=_after_first_delete(location, drop_inside))

    assert (report.locations_purged, report.purges_waiting) == (0, 1)
    assert len(_files(location)) == 2


def test_an_abandoned_location_nested_inside_does_not_block_the_purge(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """Nobody can restore an abandoned create, so its leftovers may go with the outer purge."""
    outer = _location(store, "outer")
    _put(outer, "data/a.parquet")
    nested = f"{outer}/nested-{uuid.uuid4().hex}"
    _put(nested, "data/b.parquet")
    _claim(session, outer, state=TableLocationState.DROPPED, purge_after=_NOW)
    _claim(
        session, nested, state=TableLocationState.ABANDONED, purge_after=_NOW + timedelta(days=1)
    )

    report = _sweep(session)

    assert (report.purges_waiting, report.locations_purged) == (0, 1)
    assert _files(outer) == []


def test_a_table_dropped_inside_before_the_record_is_forgotten_keeps_it(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """The last check under the lock sees the tombstone: the record stays, the purge waits."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    done: list[int] = []

    def drop_inside_once_empty() -> bool:
        if not done and not _files(location):
            done.append(1)
            with Session(session.get_bind()) as other:
                other.add(
                    TableLocation(
                        id=uuid.uuid4(),
                        environment_id=_env_id(other),
                        location=f"{location}/nested-{uuid.uuid4().hex}",
                        state=TableLocationState.DROPPED,
                        claimed_at=_NOW,
                        purge_after=_NOW + timedelta(days=7),
                        revoked_policy_ids=[],
                    )
                )
                other.commit()
        return False

    report = _sweep(session, should_stop=drop_inside_once_empty)

    assert (report.locations_purged, report.purges_waiting) == (0, 1)
    assert _record(session, location) is not None


def _existing_environment(session: Session, name: str, root: str) -> None:
    """Insert an environment as an older release accepted it, root spelled as given.

    Creating one with such a root is refused now (`check_storage_root`); the location checks
    still meet the ones that exist.

    Parameters
    ----------
    session
        Database session; commits.
    name
        Its name.
    root
        Its storage root.
    """
    session.add(Environment(id=uuid.uuid4(), name=name, storage_root=root, storage_options={}))
    session.commit()


# --- other environments and locations spelled differently -----------------------------------


def test_another_environment_root_spelled_with_a_doubled_slash_blocks_a_purge(
    session: Session,
    catalog: CatalogService,
    store: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``gold//t-<hex>`` is the purged directory in another spelling: nothing is deleted."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    head, name = location.rsplit("/", 1)
    _existing_environment(session, "other", f"{head}//{name}")
    caplog.set_level(logging.WARNING)

    report = _sweep(session)

    assert (report.purges_blocked, report.files_deleted) == (1, 0)
    assert _files(location) == ["data/a.parquet"]
    assert "written another way" in caplog.text


def test_another_environment_root_inside_a_location_blocks_a_purge(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """An environment rooted inside the directory owns what is there, record or not."""
    location = _location(store)
    _put(location, "inner/raw/a.csv")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    _earlier_layout(catalog, principal, "inner", f"{location}/inner")

    assert _sweep(session).purges_blocked == 1
    assert _files(location) == ["inner/raw/a.csv"]


@pytest.mark.parametrize("slash", ["", "/"])
def test_another_environment_rooted_at_managed_blocks_a_purge(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef, slash: str
) -> None:
    """Every directory in ``managed/`` then lies in that environment's root."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    _earlier_layout(catalog, principal, "other", f"file://{store}/managed{slash}")

    assert _sweep(session).purges_blocked == 1
    assert _files(location) == ["data/a.parquet"]


@pytest.mark.parametrize("zone", ["raw", "artifacts", "_artifacts/code"])
def test_a_root_in_another_environments_unrecorded_files_blocks_a_purge(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef, zone: str
) -> None:
    """The enclosing environment keeps files there that no record names."""
    inner = f"file://{store}/{zone}/inner"
    _earlier_layout(catalog, principal, "inner", inner)
    location = f"{inner}/managed/gold/t-{uuid.uuid4().hex}"
    _put(location, "data/a.parquet")
    _claim(
        session,
        location,
        state=TableLocationState.ABANDONED,
        purge_after=_NOW,
        environment="inner",
    )

    assert _sweep(session).purges_blocked == 1
    assert _files(location) == ["data/a.parquet"]


def test_the_unrecorded_zones_hold_the_code_archives() -> None:
    """A new directory without records must be added to the zones: code archives are."""
    assert CODE_PREFIX.split("/", 1)[0] in UNRECORDED_ZONES


@pytest.mark.parametrize("inner", ["managed/gold/nest", "raw/beside"])
def test_an_environment_rooted_inside_this_one_away_from_the_location_does_not_block(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef, inner: str
) -> None:
    """Inside ``managed/`` beside the location, or beside ``managed/``: nothing of it is there."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    _earlier_layout(catalog, principal, "inner", f"file://{store}/{inner}")

    assert _sweep(session).locations_purged == 1
    assert _files(location) == []


@pytest.mark.parametrize("spelling", ["{head}//{name}/n-{hex}", "{head}/x/../{name}/n-{hex}"])
def test_a_live_record_spelled_differently_inside_blocks_a_purge(
    session: Session, catalog: CatalogService, store: Path, spelling: str
) -> None:
    """A live table whose record names a place inside the directory in another spelling."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    head, name = location.rsplit("/", 1)
    _claim(
        session,
        spelling.format(head=head, name=name, hex=uuid.uuid4().hex),
        state=TableLocationState.LIVE,
    )

    assert _sweep(session).purges_blocked == 1
    assert _files(location) == ["data/a.parquet"]


def test_a_glob_character_in_a_name_elsewhere_does_not_block_a_purge(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A storage names files literally: ``[`` in another table's name aliases nothing.

    Nor does a root that cannot be normalised on another bucket.
    """
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    session.add(
        Environment(
            id=uuid.uuid4(),
            name="far",
            storage_root="s3://elsewhere/x/../y",
            storage_options={},
            is_protected=False,
            properties={},
        )
    )
    session.commit()
    _claim(session, _location(store, "odd[1]"), state=TableLocationState.LIVE)

    assert _sweep(session).locations_purged == 1


def test_a_percent_escape_is_compared_as_written_and_decoded(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """A live record that decodes to a place inside blocks; ``a%41b`` elsewhere blocks nothing.

    Not even its own purge: a record never blocks itself.
    """
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    head, name = location.rsplit("/", 1)
    _claim(
        session,
        f"{head}/{name.replace('-', '%2D')}/n-{uuid.uuid4().hex}",
        state=TableLocationState.LIVE,
    )
    assert _sweep(session).purges_blocked == 1

    session.execute(sa_delete(TableLocation).where(TableLocation.state == TableLocationState.LIVE))
    session.commit()
    odd = _location(store, "a%41b")
    _put(odd, "data/a.parquet")
    _claim(session, odd, state=TableLocationState.DROPPED, purge_after=_NOW)

    report = _sweep(session)

    assert (report.locations_purged, report.purges_blocked) == (2, 0)
    assert _files(location) == [] and _files(odd) == []


def test_a_relative_root_of_another_environment_blocks_a_purge(
    session: Session, catalog: CatalogService, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``file://store`` resolves against a working directory: it may be the same place."""
    monkeypatch.chdir(store.parent)
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    _existing_environment(session, "rel", "file://store")
    _claim(
        session,
        location.replace(f"file://{store}", "file://store") + f"/n-{uuid.uuid4().hex}",
        state=TableLocationState.LIVE,
        environment="rel",
    )

    assert _sweep(session).purges_blocked == 1
    assert _files(location) == ["data/a.parquet"]


def test_a_root_through_a_symbolic_link_inside_a_location_blocks_a_purge(
    session: Session, catalog: CatalogService, store: Path, tmp_path: Path
) -> None:
    """A link elsewhere that points into the directory is the directory."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    link = tmp_path / "link"
    link.symlink_to(Path(location.removeprefix("file://")))
    _claim(session, f"file://{link}/n-{uuid.uuid4().hex}", state=TableLocationState.LIVE)

    assert _sweep(session).purges_blocked == 1


def test_another_environment_root_with_a_dotdot_segment_is_resolved(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """On a local disk ``x/../`` resolves exactly: a root into the location blocks, others not."""
    location = _location(store)
    _put(location, "data/a.parquet")
    _claim(session, location, state=TableLocationState.ABANDONED, purge_after=_NOW)
    _existing_environment(session, "elsewhere", f"file://{store}/x/../y")
    head, name = location.rsplit("/", 1)
    _existing_environment(session, "into", f"{head}/x/../{name}/inner")

    assert _sweep(session).purges_blocked == 1
    session.execute(sa_delete(Environment).where(Environment.name == "into"))
    session.commit()
    assert _sweep(session).locations_purged == 1


def test_a_purge_under_a_root_with_a_percent_escape_proceeds(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """The environment's own root has a percent escape: compared under both readings, it is fine."""
    root = f"file://{store}/r%41"
    _existing_environment(session, "escaped", root)
    location = f"{root}/managed/gold/t-{uuid.uuid4().hex}"
    _put(location, "data/a.parquet")
    _claim(
        session,
        location,
        state=TableLocationState.ABANDONED,
        purge_after=_NOW,
        environment="escaped",
    )

    assert _sweep(session).locations_purged == 1
    assert _files(location) == []


def test_near_other_location_reads_what_it_can_and_keeps_the_rest(
    session: Session, catalog: CatalogService, store: Path
) -> None:
    """Unreadable locations block only on their own scheme; an unreadable target blocks itself."""
    from lakana_core.catalog.locations import Nearby, near_other_location

    _existing_environment(session, "rel", "file://relative")
    _claim(session, "file://relative/managed/gold/t", state=TableLocationState.LIVE)
    _claim(session, "s3://", state=TableLocationState.LIVE)
    target = f"s3://bucket/managed/gold/t-{uuid.uuid4().hex}"

    assert near_other_location(session, target, root="s3://bucket") is None
    assert near_other_location(session, target, root="no-scheme") == Nearby(
        target, environment_root=False
    )
    _claim(session, "no-scheme/at/all", state=TableLocationState.LIVE)
    assert near_other_location(session, target, root="s3://bucket") == Nearby(
        "no-scheme/at/all", environment_root=False
    )


_HEX = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    ("target", "other"),
    [
        (f"s3://bucket/managed/gold/t-{_HEX}", f"s3://BUCKET/managed/gold/t-{_HEX}"),
        (f"s3://bucket/managed/gold/t-{_HEX}", f"s3://bucket/managed/GOLD/T-{_HEX}"),
        (f"s3://bucket/managed/gold/t-{_HEX}", f"s3://bucket//managed/gold/t-{_HEX}/n"),
        (f"s3://bucket/managed/gold/t-{_HEX}", f"s3://bucket/managed/x/../gold/t-{_HEX}"),
        (f"s3://bucket/managed/gold/t-{_HEX}", f"s3a://bucket/managed/gold/t-{_HEX}/"),
        (f"s3://bucket/managed/gold/t-{_HEX}", f"s3://bucket/managed/gold/t%252D{_HEX}"),
        (
            f"abfs://lake/managed/gold/t-{_HEX}",
            f"abfss://Lake@account.dfs.core.windows.net/managed/gold/t-{_HEX}",
        ),
        ("file:///data/managed/caf\u00e9/t", "file:///data/managed/cafe\u0301/t"),
        ("file:///data/managed/gold/t", "FILE:///data/./managed/gold//t/"),
    ],
)
def test_two_spellings_of_one_place_are_compared_as_one(
    session: Session, catalog: CatalogService, target: str, other: str
) -> None:
    """Case, aliases, doubled slashes, ``..``, double escapes, accounts, Unicode forms."""
    from lakana_core.catalog.locations import Nearby, near_other_location

    root = target.split("/managed/", 1)[0]
    _claim(session, other, state=TableLocationState.LIVE)

    assert near_other_location(session, target, root=root) == Nearby(other, environment_root=False)


def test_places_that_only_look_alike_are_not_compared_as_one(
    session: Session, catalog: CatalogService
) -> None:
    """Another bucket, another scheme, a sibling sharing a prefix: nothing is kept for them."""
    from lakana_core.catalog.locations import near_other_location

    target = f"s3://bucket/managed/gold/t-{_HEX}"
    for other in (
        f"s3://other/managed/gold/t-{_HEX}",
        f"gs://bucket/managed/gold/t-{_HEX}",
        f"s3://bucket/managed/gold/t-{_HEX}2",
    ):
        _claim(session, other, state=TableLocationState.LIVE)
    assert near_other_location(session, target, root="s3://bucket") is None


@pytest.mark.parametrize("root", ["file:///", "file://"])
def test_a_location_under_the_root_of_the_disk_is_judged_like_any_other(
    session: Session, catalog: CatalogService, root: str
) -> None:
    """``file:///`` is a root like any other: its ``managed/`` is ``file:///managed``.

    ``file://`` is how the sweep holds that root, without its trailing slash.
    """
    from lakana_core.catalog.locations import Nearby, near_other_location

    target = f"file:///managed/gold/t-{_HEX}"
    assert near_other_location(session, target, root=root) is None

    _existing_environment(session, "inner", "FILE:///managed/")
    assert near_other_location(session, target, root=root) == Nearby(
        "FILE:///managed/", environment_root=True
    )


def test_an_unreadable_root_beside_the_root_of_the_disk_still_keeps_the_location(
    session: Session, catalog: CatalogService
) -> None:
    """A relative ``file`` root on the same scheme cannot be compared, so it keeps everything."""
    from lakana_core.catalog.locations import Nearby, near_other_location

    _existing_environment(session, "rel", "file://relative")
    target = f"file:///managed/gold/t-{_HEX}"
    assert near_other_location(session, target, root="file:///") == Nearby(
        "file://relative", environment_root=True
    )


def test_the_sweep_confines_a_location_under_the_root_of_the_disk(session: Session) -> None:
    """Its own root's ``managed/`` and the root-relative path, for an environment at ``file:///``."""
    run = sweep_module._Run(
        session,
        mock.Mock(),
        "file:///",
        "disk",
        _NOW,
        timedelta(hours=1),
        7,
        lambda: 0.0,
        1.0,
        lambda: False,
    )
    assert run._confined(f"file:///managed/gold/t-{_HEX}") == f"managed/gold/t-{_HEX}"
    assert run._confined(f"file:///raw/gold/t-{_HEX}") is None
    assert run._confined(f"file:///managed//gold/t-{_HEX}") is None
