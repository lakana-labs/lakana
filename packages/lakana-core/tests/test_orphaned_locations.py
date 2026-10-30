"""Table directories without a location record: listed on request, deleted only on request.

Older releases left directories under ``managed/`` that no record names. They are listed
(`CatalogService.find_orphaned_locations`) and deleted only by an explicit call
(`CatalogService.delete_orphaned_locations`), which takes a record for each one first and then
purges it as the location sweep purges, under the same lock and checks. Each test that deletes
has a neighbour for the nearest directory that must stay. These tests run on a SQLite file.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError, NoCredentialsError
from core_test_helpers import ActionAllowList, _earlier_layout
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_table_location_sweep import _claim, _events, _files, _location, _put, _record, _schema
from test_table_writes import _catalog, _ref

from lakana_core.auth import Action, PrincipalRef, Securable
from lakana_core.catalog import location_sweep
from lakana_core.catalog import orphaned_locations as orphaned_module
from lakana_core.catalog import service as service_module
from lakana_core.catalog.orphaned_locations import (
    HELD_PREFIX,
    KEPT_BUSY,
    KEPT_CLAIMED,
    KEPT_EMPTY,
    KEPT_FAILED,
    KEPT_NOT_REACHED,
    KEPT_RECENT,
    KEPT_UNJUDGED,
    KEPT_WAITING,
    OrphanedLocations,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import (
    NotFoundError,
    PermissionDenied,
    StorageRefusedError,
    StorageUnavailableError,
    ValidationError,
)
from lakana_core.models import (
    AuditEventType,
    Base,
    Environment,
    Namespace,
    Table,
    TableLocation,
    TableLocationState,
    TableType,
)
from lakana_core.storage.base import StoredObject
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver

_OLD = timedelta(days=40)


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
    """Create `dev` on `store` and return a catalog over it with allow-all authorization.

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


def _leftover(location: str, *, age: timedelta = _OLD, files: int = 2) -> str:
    """Write files into a directory and make them `age` old.

    Parameters
    ----------
    location
        An absolute ``file://`` location.
    age
        How old every file is.
    files
        How many files of 5 bytes to write.

    Returns
    -------
    str
        The location, relative to the store (what a report names).
    """
    moment = (datetime.now(UTC) - age).timestamp()
    for index in range(files):
        path = _put(location, f"data/{index}.parquet", b"12345")
        os.utime(path, (moment, moment))
    store = location.removeprefix("file://").split("/managed/", 1)[0]
    return location.removeprefix(f"file://{store}/")


def _listed(report: OrphanedLocations) -> list[str]:
    """Name the locations of a report.

    Parameters
    ----------
    report
        What a call returned.

    Returns
    -------
    list of str
        The root-relative locations, sorted.
    """
    return sorted(entry.location for entry in report.locations)


# --- what is listed -------------------------------------------------------------------------


def test_an_old_directory_without_a_record_is_listed_then_deleted(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """The listing names it with its files; the deletion removes them and audits it."""
    location = _location(store, "old")
    relative = _leftover(location)

    listed = catalog.find_orphaned_locations(principal, "dev")

    assert (listed.environment, listed.older_than_days, listed.complete) == ("dev", 30, True)
    (entry,) = listed.locations
    assert (entry.location, entry.files, entry.bytes, entry.recent) == (relative, 2, 10, False)
    assert entry.newest_file_at is not None
    assert entry.newest_file_at < datetime.now(UTC) - timedelta(days=39)
    assert (entry.deleted, entry.kept_because) == (False, None)
    assert _events(session, AuditEventType.ORPHANED_LOCATIONS_DELETED) == []

    deleted = catalog.delete_orphaned_locations(principal, "dev")

    (gone,) = deleted.locations
    assert (gone.location, gone.deleted, gone.files_deleted, gone.bytes_deleted) == (
        relative,
        True,
        2,
        10,
    )
    assert deleted.complete
    assert _files(location) == []
    assert _record(session, location) is None
    assert _events(session, AuditEventType.TABLE_PURGED) == []
    (event,) = _events(session, AuditEventType.ORPHANED_LOCATIONS_DELETED)
    assert event.principal_id == principal.id
    assert event.securable_environment == "dev"
    assert event.detail == {
        "environment": "dev",
        "older_than_days": 30,
        "count": 1,
        "files": 2,
        "bytes": 10,
        "locations": [relative],
        "more_locations": 0,
        "kept": 0,
        "complete": True,
    }
    assert catalog.find_orphaned_locations(principal, "dev").locations == []


def test_a_recent_directory_is_listed_as_recent_and_never_deleted(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A file newer than `older_than_days` keeps the whole directory."""
    location = _location(store, "fresh")
    _leftover(location, age=timedelta(days=2))

    (entry,) = catalog.find_orphaned_locations(principal, "dev").locations
    assert entry.recent
    assert (
        catalog.find_orphaned_locations(principal, "dev", older_than_days=1).locations[0].recent
        is False
    )

    (kept,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert (kept.deleted, kept.kept_because) == (False, KEPT_RECENT)
    assert len(_files(location)) == 2
    (event,) = _events(session, AuditEventType.ORPHANED_LOCATIONS_DELETED)
    assert (event.detail["count"], event.detail["kept"]) == (0, 1)


def test_one_new_file_keeps_an_otherwise_old_directory(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """The newest file decides."""
    location = _location(store, "mixed")
    _leftover(location)
    _put(location, "data/new.parquet")
    (entry,) = catalog.find_orphaned_locations(principal, "dev").locations
    assert entry.recent and entry.files == 3


def test_a_file_without_a_modification_time_counts_as_recent(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A backend that does not report the age never lets a directory be deleted."""
    location = _location(store, "ageless")
    _leftover(location)
    real = FsspecStorage.list_objects

    def ageless(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        for found in real(self, prefix):
            yield StoredObject(found.path, found.size, None)

    with mock.patch.object(FsspecStorage, "list_objects", ageless):
        (entry,) = catalog.find_orphaned_locations(principal, "dev").locations
        assert entry.recent and entry.newest_file_at is None
        (kept,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert kept.kept_because == KEPT_RECENT
    assert len(_files(location)) == 2


@pytest.mark.parametrize("state", list(TableLocationState))
def test_a_directory_a_record_names_is_never_listed(
    session: Session,
    catalog: CatalogService,
    store: Path,
    principal: PrincipalRef,
    state: TableLocationState,
) -> None:
    """Whatever the record's state: the sweep owns that directory."""
    location = _location(store, "recorded")
    _leftover(location)
    _claim(session, location, state=state, purge_after=datetime.now(UTC) + timedelta(days=9))

    assert catalog.find_orphaned_locations(principal, "dev").locations == []
    assert catalog.delete_orphaned_locations(principal, "dev").locations == []
    assert len(_files(location)) == 2


def test_a_directory_that_holds_a_table_here_or_in_another_environment_is_never_listed(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A table root nested inside, of this environment or of one sharing the root, keeps it."""
    catalog.create_environment(
        principal,
        name="test",
        storage=StorageConfig(scheme="file", root=f"file://{store}", options={}),
    )
    mine, theirs = _location(store, "mine"), _location(store, "theirs")
    for outer in (mine, theirs):
        _leftover(outer)
    namespaces = {
        name: session.scalars(
            select(Namespace.id)
            .join(Environment, Namespace.environment_id == Environment.id)
            .where(Environment.name == name)
        ).first()
        for name in ("dev", "test")
    }
    for outer, owner in ((mine, "dev"), (theirs, "test")):
        session.add(
            Table(
                id=uuid.uuid4(),
                namespace_id=namespaces[owner],
                name=f"nested_{owner}",
                table_type=TableType.ICEBERG,
                metadata_location=f"{outer}/t-{uuid.uuid4().hex}/metadata/00000-a.metadata.json",
            )
        )
    session.commit()

    assert catalog.find_orphaned_locations(principal, "dev").locations == []
    assert catalog.find_orphaned_locations(principal, "test").locations == []


def test_a_directory_inside_a_table_root_is_never_listed(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A table from an old release whose root has no id: what lies inside it is the table's."""
    legacy = f"file://{store}/managed/gold/legacy"
    namespace = session.scalars(select(Namespace.id)).first()
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=namespace,
            name="legacy",
            table_type=TableType.ICEBERG,
            metadata_location=f"{legacy}/metadata/00000-a.metadata.json",
        )
    )
    session.commit()
    inside = f"{legacy}/part-{uuid.uuid4().hex}"
    _leftover(inside)

    assert catalog.find_orphaned_locations(principal, "dev").locations == []


def test_a_directory_beside_a_metadata_file_without_a_root_is_never_listed(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A metadata file outside ``metadata/`` derives no root: its directory counts as one."""
    odd = f"file://{store}/managed/gold/odd"
    namespace = session.scalars(select(Namespace.id)).first()
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=namespace,
            name="odd",
            table_type=TableType.ICEBERG,
            metadata_location=f"{odd}/v1.metadata.json",
        )
    )
    session.commit()
    _leftover(f"{odd}/data-{uuid.uuid4().hex}")

    assert catalog.find_orphaned_locations(principal, "dev").locations == []


def test_a_directory_around_a_record_is_never_listed(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A tombstone, or any other record, inside the directory keeps it."""
    outer = _location(store, "outer")
    _leftover(outer)
    nested = f"{outer}/t-{uuid.uuid4().hex}"
    _leftover(nested)
    _claim(session, nested, state=TableLocationState.DROPPED, purge_after=datetime.now(UTC))

    assert catalog.find_orphaned_locations(principal, "dev").locations == []


def test_a_directory_without_a_table_id_or_outside_managed_is_never_listed(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Only the shape a table directory has, and only under managed/."""
    shapeless = f"file://{store}/managed/gold/notes"
    raw = f"file://{store}/raw/landing-{uuid.uuid4().hex}"
    artifacts = f"file://{store}/artifacts/gold/model-{uuid.uuid4().hex}"
    for location in (shapeless, raw, artifacts):
        _leftover(location)

    assert catalog.find_orphaned_locations(principal, "dev").locations == []
    catalog.delete_orphaned_locations(principal, "dev")
    assert all(len(_files(location)) == 2 for location in (shapeless, raw, artifacts))


def test_another_environment_whose_root_lies_under_managed_is_never_listed(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Its root, and everything in it, belong to that environment, whatever their names."""
    inner = f"file://{store}/managed/inner-{uuid.uuid4().hex}"
    nested = f"file://{store}/managed/gold/nest"
    for name, root in (("inner", inner), ("nest", nested)):
        _earlier_layout(catalog, principal, name, root)
    _leftover(f"{inner}/raw/x")
    _leftover(f"{nested}/artifacts/gold/model-{uuid.uuid4().hex}")
    mine = _leftover(_location(store, "mine"))

    assert _listed(catalog.find_orphaned_locations(principal, "dev")) == [mine]


@pytest.mark.parametrize("slash", ["", "/"])
def test_another_environment_whose_root_is_managed_itself_is_never_listed(
    catalog: CatalogService, store: Path, principal: PrincipalRef, slash: str
) -> None:
    """Everything in ``managed/`` then lies in that environment's root, its raw zone too."""
    _earlier_layout(catalog, principal, "other", f"file://{store}/managed{slash}")
    raw = f"file://{store}/managed/raw/exports/batch-{uuid.uuid4().hex}"
    mine = _location(store, "mine")
    for location in (raw, mine):
        _leftover(location)

    assert catalog.find_orphaned_locations(principal, "dev").locations == []
    catalog.delete_orphaned_locations(principal, "dev")
    assert [len(_files(location)) for location in (raw, mine)] == [2, 2]


def test_an_environment_rooted_at_another_ones_managed_lists_its_own(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """From the inner side, the enclosing environment's records still count, and nothing else."""
    inner = f"file://{store}/managed"
    _earlier_layout(catalog, principal, "other", inner)
    location = f"{inner}/managed/gold/t-{uuid.uuid4().hex}"
    _leftover(location)

    (entry,) = catalog.find_orphaned_locations(principal, "other").locations
    assert (entry.location, entry.kept_because) == (location.removeprefix(f"{inner}/"), None)


@pytest.mark.parametrize("zone", ["raw", "artifacts", "_artifacts/code"])
def test_an_environment_rooted_in_another_ones_unrecorded_files_keeps_everything(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef, zone: str
) -> None:
    """There the enclosing environment keeps files no record names: listed, kept."""
    inner = f"file://{store}/{zone}/inner"
    _earlier_layout(catalog, principal, "inner", inner)
    location = f"{inner}/managed/gold/t-{uuid.uuid4().hex}"
    _leftover(location)

    (entry,) = catalog.find_orphaned_locations(principal, "inner").locations
    assert entry.kept_because == HELD_PREFIX + f"file://{store}"
    assert "move this environment to a root of its own" in entry.kept_because
    assert "remove that location first" not in entry.kept_because
    (entry,) = catalog.delete_orphaned_locations(principal, "inner").locations
    assert not entry.deleted
    assert len(_files(location)) == 2


def test_an_environment_rooted_elsewhere_in_another_ones_root_lists_its_own(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Beside the enclosing environment's directories nothing of that environment lives."""
    inner = f"file://{store}/teams/inner"
    catalog.create_environment(
        principal, name="inner", storage=StorageConfig(scheme="file", root=inner, options={})
    )
    location = f"{inner}/managed/gold/t-{uuid.uuid4().hex}"
    _leftover(location)

    (entry,) = catalog.delete_orphaned_locations(principal, "inner").locations
    assert entry.deleted
    assert _files(location) == []


def test_an_environment_rooted_outside_managed_does_not_block_the_leftovers(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Its root inside this one's but beside ``managed/`` holds nothing of ``managed/``."""
    _earlier_layout(catalog, principal, "beside", f"file://{store}/raw/beside")
    location = _location(store, "mine")
    _leftover(location)

    (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert entry.deleted
    assert _files(location) == []


def test_an_environment_inside_another_ones_managed_lists_its_own(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """The enclosing environment's root does not hide the inner one's own leftovers."""
    inner = f"file://{store}/managed/gold/nest"
    _earlier_layout(catalog, principal, "nest", inner)
    location = f"{inner}/managed/gold/t-{uuid.uuid4().hex}"
    _leftover(location)
    (entry,) = catalog.find_orphaned_locations(principal, "nest").locations
    assert entry.location == location.removeprefix(f"{inner}/")


def test_a_deleted_environments_leftovers_are_listed_and_deleted(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """They are exactly the point: a deleted environment is accepted by name."""
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    catalog.delete_environment(principal, "dev")
    location = _location(store, "left")
    relative = _leftover(location)

    assert _listed(catalog.find_orphaned_locations(principal, "dev")) == [relative]
    (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert entry.deleted
    assert _files(location) == []


# --- the deletion's checks ------------------------------------------------------------------


def _with_extra_objects(extra: Callable[[str], list[StoredObject]]) -> object:
    """Patch the local storage's listing to report more objects than the disk holds.

    Parameters
    ----------
    extra
        Given the listed prefix, the objects to add (an object store reports siblings that
        share the prefix, and paths no disk can hold).

    Returns
    -------
    object
        The patch, a context manager.
    """
    real = FsspecStorage.list_objects

    def listing(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        yield from real(self, prefix)
        yield from extra(prefix)

    return mock.patch.object(FsspecStorage, "list_objects", listing)


def test_a_sibling_that_shares_the_name_is_not_counted(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A directory whose listing names only a sibling holds nothing to delete."""
    full = _leftover(_location(store, "full"))
    empty = _location(store, "empty")
    _leftover(empty)
    hollow = empty.removeprefix(f"file://{store}/")
    old = datetime.now(UTC) - _OLD

    def siblings(prefix: str) -> list[StoredObject]:
        return [StoredObject(f"{prefix}.bak", 7, old)]

    real = FsspecStorage.list_objects

    def listing(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        if prefix != hollow:
            yield from real(self, prefix)
        yield from siblings(prefix)

    with mock.patch.object(FsspecStorage, "list_objects", listing):
        found = catalog.find_orphaned_locations(principal, "dev")
    (entry,) = found.locations
    assert (entry.location, entry.files, entry.bytes) == (full, 2, 10)


def test_a_directory_emptied_before_its_delete_is_left_as_it_is(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Nothing to delete any more: no record is taken for it."""
    location = _location(store, "emptied")
    _leftover(location)
    real = orphaned_module._measure
    calls: list[str] = []

    def emptied(*args: object) -> object:
        calls.append("measured")
        if len(calls) == 2:
            for path in Path(location.removeprefix("file://")).rglob("*.parquet"):
                path.unlink()
        return real(*args)  # ty: ignore[invalid-argument-type]

    with mock.patch.object(orphaned_module, "_measure", emptied):
        (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert (entry.deleted, entry.kept_because) == (False, KEPT_EMPTY)


def test_a_database_error_in_the_check_before_a_delete_keeps_it(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """The second check fails: no record is taken, nothing is deleted, the call goes on."""
    location = _location(store, "unchecked")
    _leftover(location)
    real = orphaned_module._ineligible
    calls: list[str] = []

    def second_fails(*args: object) -> str | None:
        calls.append("checked")
        if len(calls) == 2:
            raise OSError("down")
        return real(*args)  # ty: ignore[invalid-argument-type]

    with mock.patch.object(orphaned_module, "_ineligible", second_fails):
        (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert entry.kept_because == KEPT_FAILED
    assert len(_files(location)) == 2
    assert _record(session, location) is None


def test_a_file_written_before_the_delete_keeps_the_directory(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """The age is measured again right before the record is taken."""
    location = _location(store, "woken")
    _leftover(location)
    real = orphaned_module._measure
    calls: list[str] = []

    def written(*args: object) -> object:
        calls.append("measured")
        if len(calls) == 2:
            _put(location, "data/late.parquet")
        return real(*args)  # ty: ignore[invalid-argument-type]

    with mock.patch.object(orphaned_module, "_measure", written):
        (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert (entry.kept_because, entry.files) == (KEPT_RECENT, 3)
    assert len(_files(location)) == 3
    assert _record(session, location) is None


def test_a_path_that_cannot_be_judged_keeps_the_directory(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """The sweep's purge leaves it alone and does not finish; the record is released."""
    location = _location(store, "odd")
    relative = _leftover(location)
    old = datetime.now(UTC) - _OLD

    def odd(prefix: str) -> list[StoredObject]:
        return [StoredObject(f"{relative}/a//b", 1, old)] if prefix == relative else []

    with _with_extra_objects(odd):  # ty: ignore[invalid-context-manager]
        (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert (entry.deleted, entry.kept_because, entry.files_deleted) == (False, KEPT_UNJUDGED, 2)
    assert _record(session, location) is None


def test_repeated_failures_stop_the_deletion(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """After the sweep's cap of failures in a row the call stops; the next one tries again."""
    count = location_sweep.MAX_CONSECUTIVE_FAILURES + 1
    for index in range(count):
        _leftover(_location(store, f"t{index}"))
    with mock.patch.object(location_sweep, "delete_quietly", side_effect=OSError("down")):
        report = catalog.delete_orphaned_locations(principal, "dev")
    assert not report.complete
    assert [entry.kept_because for entry in report.locations] == [KEPT_FAILED] * count
    assert session.scalars(select(TableLocation)).all() == []


def test_the_time_limit_before_a_delete_stops_the_call(
    session: Session, store: Path, catalog: CatalogService
) -> None:
    """The walk finished in its half; the limit passed before the first record was taken."""
    location = _location(store, "late")
    _leftover(location)
    real = orphaned_module._measure
    walked: list[str] = []

    def measured(*args: object) -> object:
        found = real(*args)  # ty: ignore[invalid-argument-type]
        walked.append("measured")
        return found

    with mock.patch.object(orphaned_module, "_measure", measured):
        stopped = orphaned_module.delete_orphaned_locations(
            session,
            StorageResolver(session).for_environment("dev").storage,
            environment=orphaned_module.target(session, "dev"),
            now=datetime.now(UTC),
            older_than_days=30,
            max_seconds=10.0,
            monotonic=lambda: 100.0 if walked else 0.0,
        )
    assert not stopped.complete
    (entry,) = stopped.locations
    assert entry.kept_because == KEPT_NOT_REACHED
    assert len(_files(location)) == 2


def test_a_claim_taken_after_the_listing_wins(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A record for the location that appears before the delete's own record keeps it."""
    location = _location(store, "raced")
    _leftover(location)
    real = orphaned_module._ineligible
    calls: list[str] = []

    def claim_then_check(*args: object, **kwargs: object) -> str | None:
        if len(calls) == 1:
            _claim(session, location)
        calls.append("checked")
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    with mock.patch.object(orphaned_module, "_ineligible", claim_then_check):
        (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations

    assert (entry.deleted, entry.kept_because) == (False, KEPT_BUSY)
    assert len(_files(location)) == 2
    record = _record(session, location)
    assert record is not None and record.state is TableLocationState.CLAIMED


def test_a_claim_that_beats_the_record_insert_wins(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """The location is unique: the second record for it is refused, and that is the delete's."""
    location = _location(store, "raced")
    _leftover(location)
    real = orphaned_module._ineligible
    calls: list[str] = []

    def check_then_claim(*args: object, **kwargs: object) -> str | None:
        answer = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        calls.append("checked")
        if len(calls) == 2:
            _claim(session, location)
        return answer

    with mock.patch.object(orphaned_module, "_ineligible", check_then_claim):
        (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations

    assert entry.kept_because == KEPT_CLAIMED
    assert len(_files(location)) == 2
    record = _record(session, location)
    assert record is not None and record.state is TableLocationState.CLAIMED


def _after_first_delete(change: Callable[[], object]) -> Callable[..., bool]:
    """Wrap the sweep's file delete so that another process acts after the first one.

    Parameters
    ----------
    change
        What the other process does.

    Returns
    -------
    callable
        A stand-in for `delete_quietly`.
    """
    real = location_sweep.delete_quietly
    done: list[bool] = []

    def wrapped(storage: object, path: str) -> bool:
        deleted = real(storage, path)  # ty: ignore[invalid-argument-type]
        if not done:
            done.append(True)
            change()
        return deleted

    return wrapped


@pytest.mark.parametrize(
    ("state", "reason"),
    [(TableLocationState.CLAIMED, KEPT_BUSY), (TableLocationState.DROPPED, KEPT_WAITING)],
)
def test_a_record_that_appears_inside_mid_deletion_stops_it(
    session: Session,
    catalog: CatalogService,
    store: Path,
    principal: PrincipalRef,
    state: TableLocationState,
    reason: str,
) -> None:
    """Each file is deleted under the record's lock after checking again, as the sweep does."""
    location = _location(store, "outer")
    _leftover(location, files=3)
    nested = f"{location}/t-{uuid.uuid4().hex}"
    change = _after_first_delete(
        lambda: _claim(session, nested, state=state, purge_after=datetime.now(UTC))
    )

    with mock.patch.object(location_sweep, "delete_quietly", change):
        (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations

    assert (entry.deleted, entry.kept_because, entry.files_deleted) == (False, reason, 1)
    assert len(_files(location)) == 2
    assert _record(session, location) is None
    (event,) = _events(session, AuditEventType.ORPHANED_LOCATIONS_DELETED)
    assert (event.detail["count"], event.detail["files"], event.detail["kept"]) == (0, 1, 1)


def test_a_storage_error_keeps_the_directory_and_releases_the_record(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Nothing is left behind for the sweep to finish unasked."""
    location = _location(store, "broken")
    _leftover(location)
    with mock.patch.object(location_sweep, "delete_quietly", side_effect=OSError("down")):
        (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert entry.kept_because == KEPT_FAILED
    assert _record(session, location) is None
    assert len(_files(location)) == 2


def test_a_listing_error_for_one_directory_keeps_it_and_goes_on(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Measuring again before the delete can fail too."""
    location = _location(store, "flaky")
    _leftover(location)
    real = orphaned_module._measure
    calls: list[str] = []

    def second_fails(*args: object) -> object:
        calls.append("measured")
        if len(calls) == 2:
            raise OSError("down")
        return real(*args)  # ty: ignore[invalid-argument-type]

    with mock.patch.object(orphaned_module, "_measure", second_fails):
        (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert entry.kept_because == KEPT_FAILED
    assert len(_files(location)) == 2


def test_a_record_that_cannot_be_released_is_left_for_the_sweep(
    session: Session,
    catalog: CatalogService,
    store: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Then the record says what the delete started, and the sweep finishes it."""
    location = _location(store, "stuck")
    _leftover(location)
    with (
        mock.patch.object(location_sweep, "delete_quietly", side_effect=OSError("down")),
        mock.patch.object(orphaned_module, "delete", side_effect=OSError("gone")),
    ):
        (entry,) = catalog.delete_orphaned_locations(principal, "dev").locations
    assert entry.kept_because == KEPT_FAILED
    record = _record(session, location)
    assert record is not None and record.state is TableLocationState.ABANDONED
    assert "could not release" in caplog.text


def test_the_time_limit_stops_the_deletion_and_the_next_call_resumes(
    session: Session, store: Path, catalog: CatalogService
) -> None:
    """Past the limit no further file is deleted; the directory is left without a record."""
    location = _location(store, "big")
    _leftover(location, files=3)
    second = _location(store, "next")
    _leftover(second)
    environment = orphaned_module.target(session, "dev")
    resolved = StorageResolver(session).for_environment("dev", include_deleted=True)

    def clock() -> float:
        """Run out of time once the first file of `big` is gone, whatever the listing order.

        Returns
        -------
        float
            Seconds: 0 while `big` is whole, then past the limit.
        """
        return 0.0 if len(_files(location)) == 3 else 1000.0

    stopped = orphaned_module.delete_orphaned_locations(
        session,
        resolved.storage,
        environment=environment,
        now=datetime.now(UTC),
        older_than_days=30,
        max_seconds=10.0,
        monotonic=clock,
    )

    assert not stopped.complete
    kept = [entry.kept_because for entry in stopped.locations]
    assert KEPT_NOT_REACHED in kept
    assert 0 < len(_files(location)) < 3
    assert session.scalars(select(TableLocation)).all() == []
    session.rollback()
    resumed = orphaned_module.delete_orphaned_locations(
        session,
        resolved.storage,
        environment=environment,
        now=datetime.now(UTC),
        older_than_days=30,
    )
    assert resumed.complete and all(entry.deleted for entry in resumed.locations)
    assert _files(location) == [] and _files(second) == []


def test_a_listing_past_the_time_limit_says_it_is_incomplete(
    session: Session, store: Path, catalog: CatalogService
) -> None:
    """The dry run reports what it found before the limit."""
    _leftover(_location(store, "a"))
    resolved = StorageResolver(session).for_environment("dev")
    found = orphaned_module.find_orphaned_locations(
        session,
        resolved.storage,
        environment=orphaned_module.target(session, "dev"),
        now=datetime.now(UTC),
        older_than_days=30,
        max_seconds=1.0,
        monotonic=iter([0.0, 5.0, 5.0, 5.0]).__next__,
    )
    assert not found.complete


# --- rights, arguments, audit ---------------------------------------------------------------


def test_both_need_manage_environment_on_the_instance(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """An environment-scoped right is not enough: the walk sees every environment's files."""
    _catalog(session, principal, store)
    authz = ActionAllowList(Action.READ_ENVIRONMENT)
    catalog = CatalogService(session, StorageResolver(session), authz)
    with pytest.raises(PermissionDenied):
        catalog.find_orphaned_locations(principal, "dev")
    with pytest.raises(PermissionDenied):
        catalog.delete_orphaned_locations(principal, "dev")
    assert authz.seen == [(Action.MANAGE_ENVIRONMENT, Securable.instance_scope())] * 2


def test_an_unknown_environment_is_not_found(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The message names the environment."""
    with pytest.raises(NotFoundError, match="nope"):
        catalog.find_orphaned_locations(principal, "nope")


@pytest.mark.parametrize("days", [0, 3651])
def test_older_than_days_out_of_range_is_refused(
    catalog: CatalogService, principal: PrincipalRef, days: int
) -> None:
    """At least a day: a younger directory may belong to a release still writing."""
    with pytest.raises(ValidationError, match="from 1 to 3650"):
        catalog.delete_orphaned_locations(principal, "dev", older_than_days=days)


def test_the_audit_event_caps_its_list_of_locations(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A long list is cut, with a count of the rest."""
    for name in ("a", "b", "c"):
        _leftover(_location(store, name))
    with mock.patch.object(service_module, "AUDITED_LOCATIONS", 2):
        catalog.delete_orphaned_locations(principal, "dev", older_than_days=7)
    (event,) = _events(session, AuditEventType.ORPHANED_LOCATIONS_DELETED)
    assert (event.detail["count"], event.detail["more_locations"]) == (3, 1)
    assert len(event.detail["locations"]) == 2
    assert event.detail["older_than_days"] == 7


def test_no_transaction_is_left_open(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """Either call leaves the session without a transaction."""
    _leftover(_location(store, "a"))
    catalog.find_orphaned_locations(principal, "dev")
    assert not session.in_transaction()
    catalog.delete_orphaned_locations(principal, "dev")
    assert not session.in_transaction()


# --- locations that cannot be compared as spelled -------------------------------------------


def _store_location(store: Path, suffix: str) -> str:
    """Spell a location under the store's `managed/gold`, as given.

    Parameters
    ----------
    store
        The storage root.
    suffix
        What follows ``managed/gold``, slashes included, exactly as written.

    Returns
    -------
    str
        The absolute location.
    """
    return f"file://{store}/managed/gold{suffix}"


def test_another_environment_root_spelled_with_a_doubled_slash_keeps_the_directory(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """``gold//t-<hex>`` is the same directory as ``gold/t-<hex>``: it is kept and named."""
    name = f"t-{uuid.uuid4().hex}"
    session.add(
        Environment(
            id=uuid.uuid4(),
            name="other",
            storage_root=_store_location(store, f"//{name}"),
            storage_options={},
        )
    )
    session.commit()
    location = _store_location(store, f"/{name}")
    _leftover(location)

    (entry,) = catalog.find_orphaned_locations(principal, "dev", older_than_days=1).locations
    assert entry.kept_because is not None and f"//{name}" in entry.kept_because
    (kept,) = catalog.delete_orphaned_locations(principal, "dev", older_than_days=1).locations
    assert (kept.deleted, kept.kept_because) == (False, entry.kept_because)
    assert len(_files(location)) == 2
    assert _record(session, location) is None


@pytest.mark.parametrize(
    "spelling", ["//{name}", "/x/../{name}", "/./{name}"], ids=["doubled", "dotdot", "dot"]
)
def test_a_record_spelled_so_that_it_cannot_be_compared_keeps_the_directory(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef, spelling: str
) -> None:
    """A record whose location names the directory in another spelling, in any state."""
    name = f"t-{uuid.uuid4().hex}"
    location = _store_location(store, f"/{name}")
    _leftover(location)
    _claim(
        session, _store_location(store, spelling.format(name=name)), state=TableLocationState.LIVE
    )

    (entry,) = catalog.find_orphaned_locations(principal, "dev", older_than_days=1).locations
    assert entry.kept_because is not None
    catalog.delete_orphaned_locations(principal, "dev", older_than_days=1)
    assert len(_files(location)) == 2


def test_a_table_whose_metadata_is_spelled_with_a_doubled_slash_keeps_the_directory(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """The table's files are in the directory although its metadata location says ``//``."""
    name = f"t-{uuid.uuid4().hex}"
    location = _store_location(store, f"/{name}")
    _leftover(location)
    namespace = session.scalars(select(Namespace.id)).first()
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=namespace,
            name="doubled",
            table_type=TableType.ICEBERG,
            metadata_location=_store_location(store, f"//{name}/metadata/00000-a.metadata.json"),
        )
    )
    session.commit()

    (entry,) = catalog.find_orphaned_locations(principal, "dev", older_than_days=1).locations
    assert entry.kept_because is not None
    catalog.delete_orphaned_locations(principal, "dev", older_than_days=1)
    assert len(_files(location)) == 2


def test_a_percent_escape_is_compared_as_written_and_decoded(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """``t%2D<hex>`` decodes to ``t-<hex>``: that one is kept, another name is not."""
    hex_kept = uuid.uuid4().hex
    kept = _store_location(store, f"/t-{hex_kept}")
    free = _store_location(store, f"/u-{uuid.uuid4().hex}")
    _leftover(kept)
    _leftover(free)
    _claim(session, f"file://{store}/managed/gold/t%2D{hex_kept}", state=TableLocationState.LIVE)
    _claim(session, f"file://{store}/managed/gold/a%41b-{uuid.uuid4().hex}")

    report = catalog.delete_orphaned_locations(principal, "dev", older_than_days=1)

    outcome = {entry.location: entry.deleted for entry in report.locations}
    assert outcome == {
        kept.removeprefix(f"file://{store}/"): False,
        free.removeprefix(f"file://{store}/"): True,
    }
    assert len(_files(kept)) == 2


def test_an_odd_spelling_on_other_storage_does_not_keep_anything(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A record on another bucket, however spelled, is far from this directory."""
    location = _store_location(store, f"/t-{uuid.uuid4().hex}")
    _leftover(location)
    _claim(session, f"s3://elsewhere//t-{uuid.uuid4().hex}", state=TableLocationState.LIVE)
    _claim(session, f"s3://elsewhere/x/../t%2D{uuid.uuid4().hex}", state=TableLocationState.LIVE)

    (entry,) = catalog.delete_orphaned_locations(principal, "dev", older_than_days=1).locations
    assert entry.deleted


def test_a_scheme_alias_counts_as_another_spelling(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """``FILE://`` names the same storage as ``file://``."""
    name = f"t-{uuid.uuid4().hex}"
    location = _store_location(store, f"/{name}")
    _leftover(location)
    _claim(session, f"FILE://{store}/managed/gold/{name}", state=TableLocationState.LIVE)

    (entry,) = catalog.find_orphaned_locations(principal, "dev", older_than_days=1).locations
    assert entry.kept_because is not None


# --- a doubled slash at the start of the path -----------------------------------------------


def _odd_environment(session: Session, store: Path) -> str:
    """Insert an environment whose root names the store with four slashes, as before the check.

    Parameters
    ----------
    session
        Database session; commits.
    store
        The storage root of `dev`.

    Returns
    -------
    str
        The odd spelling of the root, ``file:////...``.
    """
    root = f"file:///{store}"
    session.add(Environment(id=uuid.uuid4(), name="odd", storage_root=root, storage_options={}))
    session.commit()
    return root


def test_another_environment_on_the_same_root_with_a_leading_double_slash_keeps_its_table(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """``file:////store`` is ``file:///store`` on a local disk: that environment's table stays."""
    odd = _odd_environment(session, store)
    name = f"orders-{uuid.uuid4().hex}"
    _claim(session, f"{odd}/managed/gold/{name}", state=TableLocationState.LIVE, environment="odd")
    namespace = session.scalars(select(Namespace.id)).first()
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=namespace,
            name="orders",
            table_type=TableType.ICEBERG,
            metadata_location=f"{odd}/managed/gold/{name}/metadata/00000-a.metadata.json",
        )
    )
    session.commit()
    location = _store_location(store, f"/{name}")
    _leftover(location)
    metadata = _put(location, "metadata/00000-a.metadata.json", b"{}")
    moment = (datetime.now(UTC) - _OLD).timestamp()
    os.utime(metadata, (moment, moment))

    catalog.delete_orphaned_locations(principal, "dev", older_than_days=1)

    assert metadata.exists()
    assert len(_files(location)) == 3


@pytest.mark.parametrize("kind", ["record", "table"])
def test_a_location_with_a_leading_double_slash_keeps_the_directory(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef, kind: str
) -> None:
    """A live record, or a table's metadata file, spelled ``file:////...``."""
    name = f"t-{uuid.uuid4().hex}"
    odd = f"file:///{store}/managed/gold/{name}"
    if kind == "record":
        _claim(session, odd, state=TableLocationState.LIVE)
    else:
        namespace = session.scalars(select(Namespace.id)).first()
        session.add(
            Table(
                id=uuid.uuid4(),
                namespace_id=namespace,
                name="odd",
                table_type=TableType.ICEBERG,
                metadata_location=f"{odd}/metadata/00000-a.metadata.json",
            )
        )
        session.commit()
    location = _store_location(store, f"/{name}")
    _leftover(location)

    (entry,) = catalog.delete_orphaned_locations(principal, "dev", older_than_days=1).locations
    assert not entry.deleted and entry.kept_because is not None
    assert len(_files(location)) == 2


# --- the same directory under another name: relative roots, hosts, links, case ------------


def test_a_relative_root_of_another_environment_keeps_its_table(
    session: Session,
    catalog: CatalogService,
    store: Path,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``file://store`` was read against the working directory: it may be this store."""
    monkeypatch.chdir(store.parent)
    session.add(
        Environment(id=uuid.uuid4(), name="rel", storage_root="file://store", storage_options={})
    )
    session.commit()
    name = f"orders-{uuid.uuid4().hex}"
    _claim(
        session,
        f"file://store/managed/gold/{name}",
        state=TableLocationState.LIVE,
        environment="rel",
    )
    location = _store_location(store, f"/{name}")
    _leftover(location)

    (entry,) = catalog.delete_orphaned_locations(principal, "dev", older_than_days=1).locations

    assert not entry.deleted and entry.kept_because is not None
    assert len(_files(location)) == 2


def test_a_location_with_a_host_keeps_every_directory_on_local_disks(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """``file://localhost/...`` has no one reading: nothing on the local disk is judged safe."""
    location = _store_location(store, f"/t-{uuid.uuid4().hex}")
    _leftover(location)
    _claim(
        session, f"file://localhost/elsewhere/t-{uuid.uuid4().hex}", state=TableLocationState.LIVE
    )

    (entry,) = catalog.delete_orphaned_locations(principal, "dev", older_than_days=1).locations
    assert not entry.deleted and "localhost" in (entry.kept_because or "")


def test_a_root_reached_through_a_symbolic_link_keeps_its_table(
    session: Session,
    catalog: CatalogService,
    store: Path,
    principal: PrincipalRef,
    tmp_path: Path,
) -> None:
    """Another environment's root is a link to this store: its live table is this directory."""
    link = tmp_path / "link"
    link.symlink_to(store)
    catalog.create_environment(
        principal,
        name="linked",
        storage=StorageConfig(scheme="file", root=f"file://{link}", options={}),
    )
    name = f"orders-{uuid.uuid4().hex}"
    _claim(
        session,
        f"file://{link}/managed/gold/{name}",
        state=TableLocationState.LIVE,
        environment="linked",
    )
    location = _store_location(store, f"/{name}")
    _leftover(location)

    (entry,) = catalog.delete_orphaned_locations(principal, "dev", older_than_days=1).locations
    assert not entry.deleted and entry.kept_because is not None
    assert len(_files(location)) == 2


def test_a_location_in_another_case_keeps_the_directory(
    session: Session, catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A disk may ignore case: ``GOLD/T-<hex>`` is compared as ``gold/t-<hex>``."""
    hex_ = uuid.uuid4().hex
    location = _store_location(store, f"/t-{hex_}")
    _leftover(location)
    _claim(session, f"file://{store}/managed/GOLD/T-{hex_}", state=TableLocationState.LIVE)

    (entry,) = catalog.delete_orphaned_locations(principal, "dev", older_than_days=1).locations
    assert not entry.deleted and entry.kept_because is not None


@pytest.mark.parametrize(
    ("error", "kind", "said", "code"),
    [
        # What s3fs raises: wrong keys arrive translated, the rest as botocore raised them.
        (
            PermissionError("denied /secret/path"),
            StorageRefusedError,
            "refused access",
            "storage_refused",
        ),
        (NoCredentialsError(), StorageRefusedError, "refused access", "storage_refused"),
        (
            EndpointConnectionError(endpoint_url="http://seaweedfs:8333/secret"),
            StorageUnavailableError,
            "could not be reached",
            "storage_unreachable",
        ),
        (
            ConnectTimeoutError(endpoint_url="http://seaweedfs:8333/secret"),
            StorageUnavailableError,
            "could not be reached",
            "storage_unreachable",
        ),
        (
            ConnectionRefusedError("host:9000"),
            StorageUnavailableError,
            "could not be reached",
            "storage_unreachable",
        ),
    ],
)
def test_a_storage_that_refuses_or_does_not_answer_is_a_storage_error(
    catalog: CatalogService,
    store: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    kind: type[Exception],
    said: str,
    code: str,
) -> None:
    """The cause and the next step, never the storage's own text; the log line has the code."""
    _leftover(_location(store, "refused"))

    def failing(self: FsspecStorage, prefix: str) -> Iterator[tuple[str, bool]]:
        raise error

    with mock.patch.object(FsspecStorage, "list_children", failing):
        for call in (catalog.find_orphaned_locations, catalog.delete_orphaned_locations):
            with pytest.raises(kind) as raised:
                call(principal, "dev")
            assert type(raised.value) is kind
            message = str(raised.value)
            assert message.startswith(f"The storage of environment 'dev' {said}")
            assert "no directory was listed or deleted" in message
            assert str(error) not in message
    events = [getattr(r, "event", None) for r in caplog.records]
    assert events.count(code) == 2
    assert str(error) not in caplog.text


def test_another_storage_error_is_not_called_a_storage_refusal(
    catalog: CatalogService, store: Path, principal: PrincipalRef
) -> None:
    """A missing file is not the storage refusing or failing to answer: it propagates as it is."""
    _leftover(_location(store, "missing"))

    def missing(self: FsspecStorage, prefix: str) -> Iterator[tuple[str, bool]]:
        raise FileNotFoundError(prefix)

    with (
        mock.patch.object(FsspecStorage, "list_children", missing),
        pytest.raises(FileNotFoundError),
    ):
        catalog.find_orphaned_locations(principal, "dev")
