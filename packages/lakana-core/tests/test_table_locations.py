"""A new table's location is claimed before its first file, and published behind a fence.

Every path that writes a new managed location (`create_table`, which the Iceberg REST create
calls, and a create-mode write) commits a `CLAIMED` record before the first metadata or data
file. The commit that inserts the `tables` row moves that record to `LIVE` in the same
transaction, and rolls back when the record is no longer `CLAIMED`. These tests run on a SQLite
file and look through a second connection, as another process would.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from pyiceberg.io import FileIO, InputFile, OutputFile
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from test_table_writes import _catalog, _files, _orders, _ref, _write

from lakana_core.auth import PrincipalRef, RunContext, WriteFence
from lakana_core.catalog import service as service_module
from lakana_core.catalog import writes as writes_module
from lakana_core.catalog.locations import (
    CLAIM_RELEASED_MESSAGE,
    claim_holder,
    lock_location,
    other_table_locations,
    publish_location,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError, ValidationError, WriteOutcomeUnknownError
from lakana_core.models import (
    Base,
    Environment,
    PrincipalType,
    Table,
    TableLocation,
    TableLocationState,
    TableType,
    WriteMode,
)
from lakana_core.storage.resolver import ResolvedStorage, StorageResolver


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    """Provide the writer's session on a SQLite file (overrides the in-memory one).

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
    """Return the environment's storage root, apart from the database file.

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
def other(session: Session) -> Iterator[Session]:
    """Provide a second connection to the same file: another process looking at the catalog.

    Parameters
    ----------
    session
        The writer's session; its engine is shared, its connection is not.

    Yields
    ------
    Session
        A session on its own connection.
    """
    with Session(session.get_bind()) as s:
        yield s


def _records(observer: Session) -> list[TableLocation]:
    """Read every location record through the observer's connection.

    Parameters
    ----------
    observer
        The second connection.

    Returns
    -------
    list of TableLocation
        The records, detached from the observer's transaction.
    """
    observer.rollback()
    found = list(observer.scalars(select(TableLocation).order_by(TableLocation.location)))
    observer.expunge_all()
    observer.rollback()
    return found


class _Watching(FileIO):
    """A FileIO that reports every file before it is written, then writes it.

    Parameters
    ----------
    inner
        The real FileIO.
    seen
        Called with each location before its file is opened for writing.
    """

    def __init__(self, inner: FileIO, seen: Callable[[str], None]) -> None:
        super().__init__(inner.properties)
        self._inner = inner
        self._seen = seen

    def new_input(self, location: str) -> InputFile:
        """Open a file for reading.

        Parameters
        ----------
        location
            The file.

        Returns
        -------
        InputFile
            The real FileIO's.
        """
        return self._inner.new_input(location)

    def new_output(self, location: str) -> OutputFile:
        """Report the file, then open it for writing.

        Parameters
        ----------
        location
            The file.

        Returns
        -------
        OutputFile
            The real FileIO's.
        """
        self._seen(location)
        return self._inner.new_output(location)

    def delete(self, location: str | InputFile | OutputFile) -> None:
        """Delete a file.

        Parameters
        ----------
        location
            The file.
        """
        self._inner.delete(location)


def _watch_writes(observer: Session) -> tuple[list[tuple[str, list[str]]], mock._patch]:
    """Patch the storage so every file write first records what another process sees.

    Parameters
    ----------
    observer
        The second connection.

    Returns
    -------
    tuple
        The list that fills with ``(file, states of the records whose location holds it)``
        in write order, and the patch to use as a context manager.
    """
    writes: list[tuple[str, list[str]]] = []
    real = StorageResolver.for_environment

    def seen(path: str) -> None:
        states = [
            record.state.value
            for record in _records(observer)
            if path.startswith(record.location + "/")
        ]
        writes.append((path, states))

    def for_environment(self: StorageResolver, environment: str) -> ResolvedStorage:
        resolved = real(self, environment)
        return dataclasses.replace(resolved, file_io=_Watching(resolved.file_io, seen))

    return writes, mock.patch.object(StorageResolver, "for_environment", for_environment)


def _schema() -> Schema:
    """Build a one-column schema.

    Returns
    -------
    Schema
        ``id bigint``.
    """
    return Schema(NestedField(1, "id", LongType(), required=False))


def _publish_commit(session: Session, *, before: bool) -> contextlib.AbstractContextManager[object]:
    """Make the commit that publishes a table fail, before or after it was applied.

    Every other commit (the claim's, the clean-up's) goes through. The publishing commit is
    the first one after the publish fence ran.

    Parameters
    ----------
    session
        The writer's session.
    before
        Fail without applying the commit (`True`), or apply it and then fail (`False`).

    Returns
    -------
    contextlib.AbstractContextManager
        The patches.
    """
    real_commit, real_fence = session.commit, service_module.publish_location
    armed: list[int] = []

    def fence(*args: object, **kwargs: object) -> bool:
        armed.append(1)
        return real_fence(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    def commit() -> None:
        if armed and before:
            armed.clear()
            raise RuntimeError("inside the commit")
        real_commit()
        if armed:
            armed.clear()
            raise RuntimeError("after the commit")

    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(service_module, "publish_location", fence))
    stack.enter_context(mock.patch.object(session, "commit", commit))
    return stack


def test_create_table_commits_its_claim_before_the_metadata_file(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """The Iceberg REST create and the platform's create both go through `create_table`."""
    catalog = _catalog(session, principal, store)
    writes, patch = _watch_writes(other)
    with patch:
        catalog.create_table(principal, _ref("gold.orders"), schema=_schema())

    assert writes, "no file was written"
    assert writes[0][1] == ["CLAIMED"], writes  # committed, seen by another connection
    (record,) = _records(other)
    table_id = other.scalars(select(Table.id).where(Table.name == "orders")).one()
    assert record.state is TableLocationState.LIVE
    assert record.table_id == table_id
    assert record.location.endswith(f"/managed/gold/orders-{table_id.hex}")
    assert record.holder_run_id is None


def test_a_create_mode_write_commits_its_claim_before_the_first_data_file(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """Data files are written before the row exists, so the claim comes before them."""
    catalog = _catalog(session, principal, store)
    run_id = uuid.uuid4()
    writes, patch = _watch_writes(other)
    with patch:
        catalog.write_batches(
            principal,
            _ref("gold.daily"),
            mode=WriteMode.CREATE,
            schema=_orders([1], ["eu"]).schema,
            batches=iter(_orders([1, 2], ["eu", "us"]).to_batches()),
            query_run_id=run_id,
        )

    assert any(path.endswith(".parquet") for path, _ in writes)
    assert all(states == ["CLAIMED"] for _, states in writes), writes
    (record,) = _records(other)
    assert record.state is TableLocationState.LIVE
    assert record.holder_run_id == run_id


def test_the_holder_is_the_run_when_there_is_one() -> None:
    """A query run first, then the worker's fence, then the task run of a bound token."""
    query_run, fenced, task_run, attempt = (uuid.uuid4() for _ in range(4))
    person = PrincipalRef(id=uuid.uuid4(), name="ana", type=PrincipalType.USER)
    task = dataclasses.replace(person, run_context=RunContext(task_run_id=task_run, claim=1))
    worker = dataclasses.replace(
        person,
        run_context=RunContext(task_run_id=task_run, query_run_id=attempt),
        write_fence=WriteFence(query_run_id=fenced, worker_id="w"),
    )
    assert claim_holder(person, None) is None
    assert claim_holder(person, query_run) == query_run
    assert claim_holder(task, None) == task_run
    assert claim_holder(worker, None) == fenced
    assert claim_holder(dataclasses.replace(worker, write_fence=None), None) == attempt


def test_a_refused_create_claims_nothing(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """Validation comes first: a create that cannot happen leaves no record."""
    catalog = _catalog(session, principal, store)
    with pytest.raises(ValidationError, match=r"write\.data\.path"):
        catalog.create_table(
            principal,
            _ref("gold.orders"),
            schema=_schema(),
            properties={"write.data.path": "file:///elsewhere"},
        )
    assert _records(other) == []


def _abandon(observer: Session) -> None:
    """Move every claim to ABANDONED through the observer, as the sweep would.

    Parameters
    ----------
    observer
        The second connection.
    """
    observer.rollback()
    observer.execute(
        update(TableLocation)
        .where(TableLocation.state == TableLocationState.CLAIMED)
        .values(state=TableLocationState.ABANDONED)
    )
    observer.commit()


def test_the_fence_refuses_a_create_whose_claim_was_abandoned_meanwhile(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """The sweep abandoned the claim while the file was written: no table, no file, no record."""
    catalog = _catalog(session, principal, store)
    real = service_module.write_metadata

    def write_then_lose_the_claim(*args: object, **kwargs: object) -> str:
        written = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        _abandon(other)
        return written

    with (
        mock.patch.object(service_module, "write_metadata", write_then_lose_the_claim),
        pytest.raises(ConflictError) as raised,
    ):
        catalog.create_table(principal, _ref("gold.orders"), schema=_schema())

    assert str(raised.value) == CLAIM_RELEASED_MESSAGE.format(table="gold.orders")
    assert other.scalars(select(Table.id).where(Table.name == "orders")).all() == []
    assert [path for path in _files(store) if path.startswith("managed/")] == []
    assert list(store.rglob("*.metadata.json")) == []
    assert _records(other) == []  # files gone, so the record went last
    assert not session.in_transaction()


def test_the_fence_refuses_a_create_mode_write_whose_claim_was_abandoned(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """Same for a write: its data files go with it."""
    catalog = _catalog(session, principal, store)
    real = service_module.write_metadata

    def write_then_lose_the_claim(*args: object, **kwargs: object) -> str:
        written = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        _abandon(other)
        return written

    with (
        mock.patch.object(service_module, "write_metadata", write_then_lose_the_claim),
        pytest.raises(ConflictError),
    ):
        _write(catalog, principal, "gold.daily", WriteMode.CREATE, _orders([1], ["eu"]))

    assert other.scalars(select(Table.id).where(Table.name == "daily")).all() == []
    assert not list(store.rglob("*.parquet"))
    assert _records(other) == []


def test_a_published_claim_cannot_be_abandoned(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """The other side of the race: once LIVE, the sweep's conditional move changes nothing."""
    catalog = _catalog(session, principal, store)
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    _abandon(other)
    (record,) = _records(other)
    assert record.state is TableLocationState.LIVE


def test_publish_location_moves_only_a_claim(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """The fence statement changes a CLAIMED record and nothing else."""
    _catalog(session, principal, store)
    env_id = session.scalars(select(Environment.id)).one()
    for state in TableLocationState:
        session.add(
            TableLocation(
                id=uuid.uuid4(),
                environment_id=env_id,
                location=f"file:///x/{state.value}",
                state=state,
                claimed_at=datetime.now(UTC),
            )
        )
    session.commit()
    table_id = uuid.uuid4()
    moved = {
        state: publish_location(session, location=f"file:///x/{state.value}", table_id=table_id)
        for state in TableLocationState
    }
    session.commit()
    assert moved == {state: state is TableLocationState.CLAIMED for state in TableLocationState}
    claim = session.scalars(
        select(TableLocation).where(TableLocation.location == "file:///x/CLAIMED")
    ).one()
    assert (claim.state, claim.table_id) == (TableLocationState.LIVE, table_id)
    assert not publish_location(session, location="file:///x/none", table_id=table_id)


def test_lock_location_reads_the_record_again(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """Under the lock the record is read from the database, never from the identity map."""
    catalog = _catalog(session, principal, store)
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    record = session.scalars(select(TableLocation)).one()
    session.rollback()
    other.execute(update(TableLocation).values(state=TableLocationState.DROPPED))
    other.commit()
    locked = lock_location(session, record.id)
    assert locked is not None
    assert locked.state is TableLocationState.DROPPED
    session.rollback()
    assert lock_location(session, uuid.uuid4()) is None
    session.rollback()


@pytest.mark.slow
def test_a_create_that_failed_before_its_commit_deletes_its_files_and_its_claim(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """Known not to have landed: as before, the location is emptied, and the record goes too."""
    catalog = _catalog(session, principal, store)
    with (
        mock.patch.object(
            service_module.ProtectedWriteGuard,
            "record_pending",
            side_effect=RuntimeError("before the commit"),
        ),
        pytest.raises(RuntimeError, match="before the commit"),
    ):
        catalog.create_table(principal, _ref("gold.orders"), schema=_schema())

    assert list(store.rglob("*.metadata.json")) == []
    assert _records(other) == []


def test_a_create_that_lost_the_name_race_deletes_its_files_and_its_claim(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """The unique name decides a create/create race; the loser leaves nothing behind."""
    catalog = _catalog(session, principal, store)
    winner = CatalogService(other, StorageResolver(other), catalog._authz)
    real = service_module.write_metadata
    raced: list[int] = []

    def write_while_another_creates(*args: object, **kwargs: object) -> str:
        written = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        if not raced:
            raced.append(1)
            winner.create_table(principal, _ref("gold.orders"), schema=_schema())
        return written

    with (
        mock.patch.object(service_module, "write_metadata", write_while_another_creates),
        pytest.raises(service_module.AlreadyExistsError),
    ):
        catalog.create_table(principal, _ref("gold.orders"), schema=_schema())

    (record,) = _records(other)
    assert record.state is TableLocationState.LIVE  # the winner's
    assert len(list(store.rglob("*.metadata.json"))) == 1


def test_a_failed_clean_up_leaves_the_claim_abandoned_for_the_sweep(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """A file that cannot be deleted keeps its record, so the sweep can finish the work."""
    catalog = _catalog(session, principal, store)
    with (
        mock.patch.object(
            service_module.ProtectedWriteGuard,
            "record_pending",
            side_effect=RuntimeError("before the commit"),
        ),
        mock.patch.object(service_module, "cleanup_location", return_value=False),
        pytest.raises(RuntimeError),
    ):
        catalog.create_table(principal, _ref("gold.orders"), schema=_schema())

    (record,) = _records(other)
    assert record.state is TableLocationState.ABANDONED
    assert record.purge_after is not None


def test_a_create_that_failed_inside_its_commit_keeps_its_claim(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """In doubt: the record stays CLAIMED, and the message says the clean-up decides later."""
    catalog = _catalog(session, principal, store)
    with (
        _publish_commit(session, before=True),
        pytest.raises(WriteOutcomeUnknownError) as raised,
    ):
        catalog.create_table(principal, _ref("gold.orders"), schema=_schema())

    (record,) = _records(other)
    assert record.state is TableLocationState.CLAIMED
    assert len(list(store.rglob("*.metadata.json"))) == 1
    message = str(raised.value)
    assert "may or may not" in message
    assert "proven that the commit did not land" in message


def test_a_create_whose_commit_landed_although_it_raised_returns_the_table(
    session: Session,
    other: Session,
    principal: PrincipalRef,
    store: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`create_table` reads the table again, as a write does, and reports it created."""
    catalog = _catalog(session, principal, store)
    with _publish_commit(session, before=False), caplog.at_level(logging.WARNING):
        loaded = catalog.create_table(principal, _ref("gold.orders"), schema=_schema())

    assert "committed although it raised (RuntimeError)" in caplog.text

    assert loaded.metadata_location.endswith(".metadata.json")
    (record,) = _records(other)
    assert record.state is TableLocationState.LIVE


def test_a_live_record_found_after_a_failure_keeps_every_file(
    session: Session,
    other: Session,
    principal: PrincipalRef,
    store: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Belt and braces: the clean-up never deletes a location whose record says LIVE."""
    catalog = _catalog(session, principal, store)
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    record = _records(other)[0]
    resolved = StorageResolver(session).for_environment("dev")
    prepared = mock.Mock(location=record.location, resolved=resolved)
    caplog.set_level(logging.WARNING)
    catalog._release_location(prepared)
    assert len(list(store.rglob("*.metadata.json"))) == 1
    assert _records(other)[0].state is TableLocationState.LIVE
    assert "is recorded as in use" in caplog.text


def test_a_clean_up_whose_database_fails_keeps_the_files(
    session: Session,
    other: Session,
    principal: PrincipalRef,
    store: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the claim cannot be released, nothing is deleted; the sweep decides later."""
    catalog = _catalog(session, principal, store)
    with (
        mock.patch.object(
            service_module.ProtectedWriteGuard,
            "record_pending",
            side_effect=RuntimeError("before the commit"),
        ),
        mock.patch.object(
            service_module, "release_claim", side_effect=RuntimeError("database gone")
        ),
        pytest.raises(RuntimeError, match="before the commit"),
        caplog.at_level(logging.WARNING),
    ):
        catalog.create_table(principal, _ref("gold.orders"), schema=_schema())

    assert len(list(store.rglob("*.metadata.json"))) == 1
    (record,) = _records(other)
    assert record.state is TableLocationState.CLAIMED
    assert "database gone" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_a_record_that_cannot_be_removed_after_the_files_is_left_to_the_sweep(
    session: Session,
    other: Session,
    principal: PrincipalRef,
    store: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The files are gone; the ABANDONED record stays and the sweep removes it."""
    catalog = _catalog(session, principal, store)
    with (
        mock.patch.object(
            service_module.ProtectedWriteGuard,
            "record_pending",
            side_effect=RuntimeError("before the commit"),
        ),
        mock.patch.object(service_module, "forget_location", side_effect=RuntimeError("gone")),
        pytest.raises(RuntimeError, match="before the commit"),
        caplog.at_level(logging.WARNING),
    ):
        catalog.create_table(principal, _ref("gold.orders"), schema=_schema())

    assert list(store.rglob("*.metadata.json")) == []
    (record,) = _records(other)
    assert record.state is TableLocationState.ABANDONED
    assert "could not be removed (RuntimeError)" in caplog.text


def _resolved(session: Session) -> ResolvedStorage:
    """Resolve the storage of `dev`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    ResolvedStorage
        The storage.
    """
    return StorageResolver(session).for_environment("dev")


def test_cleanup_location_deletes_only_inside_the_location_and_says_so(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """A sibling sharing the text is kept, a file already gone counts as gone."""
    _catalog(session, principal, store)
    resolved = _resolved(session)
    location = f"file://{store}/managed/gold/t-{uuid.uuid4().hex}"
    for path in (f"{location}/data/a", f"{location}x/data/b"):
        file = Path(path.removeprefix("file://"))
        file.parent.mkdir(parents=True)
        file.write_bytes(b"x")
    relative = location.removeprefix(f"file://{store}/")
    real = type(resolved.storage).list

    def listing(self: object, prefix: str) -> Iterator[str]:
        yield from real(self, prefix)  # ty: ignore[invalid-argument-type]
        yield f"{relative}x/data/b"  # what an object store's prefix listing could add
        yield f"{relative}/data/gone"

    with mock.patch.object(type(resolved.storage), "list", listing):
        assert writes_module.cleanup_location(resolved, location) is True
    assert not Path(f"{location}/data/a".removeprefix("file://")).exists()
    assert Path(f"{location}x/data/b".removeprefix("file://")).exists()


def test_cleanup_location_reports_what_it_could_not_do(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """Outside the root, a failed listing and a failed delete each answer `False`."""
    _catalog(session, principal, store)
    resolved = _resolved(session)
    assert writes_module.cleanup_location(resolved, "file:///elsewhere/t") is False
    location = f"file://{store}/managed/gold/t-{uuid.uuid4().hex}"
    file = Path(f"{location}/data/a".removeprefix("file://"))
    file.parent.mkdir(parents=True)
    file.write_bytes(b"x")
    storage = type(resolved.storage)
    with mock.patch.object(storage, "list", side_effect=OSError("down")):
        assert writes_module.cleanup_location(resolved, location) is False
    with mock.patch.object(storage, "delete", side_effect=PermissionError("no")):
        assert writes_module.cleanup_location(resolved, location) is False
    assert file.exists()


def test_deleting_an_environment_keeps_its_records_as_tombstones_and_every_file(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """Every record stays, `DROPPED`, and the files with it until the undo period has passed."""
    catalog = _catalog(session, principal, store)
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    catalog.create_table(principal, _ref("gold.customers"), schema=_schema())
    catalog.drop_table(principal, _ref("gold.orders"))
    catalog.delete_environment(principal, "dev")
    records = _records(other)
    assert [r.state for r in records] == [TableLocationState.DROPPED] * 2
    assert all(r.purge_after is not None and r.purge_started_at is None for r in records)
    assert len(list(store.rglob("*.metadata.json"))) == 2


def test_a_drop_makes_the_record_a_tombstone_in_its_own_transaction(
    session: Session, other: Session, principal: PrincipalRef, store: Path
) -> None:
    """Seen from another connection, the row is gone and the record DROPPED at the same time."""
    catalog = _catalog(session, principal, store)
    created = catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    catalog.drop_table(principal, _ref("gold.orders"))
    (record,) = _records(other)
    assert record.state is TableLocationState.DROPPED
    assert record.table_id == created.uuid
    assert other.scalar(select(Table.id)) is None


def test_other_table_locations_reads_records_and_rows_and_skips_what_does_not_derive(
    session: Session, principal: PrincipalRef, store: Path
) -> None:
    """Records and row roots, itself left out; a row with no metadata directory adds nothing."""
    catalog = _catalog(session, principal, store)
    own = catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    other = catalog.create_table(principal, _ref("gold.daily"), schema=_schema())
    own_location = own.metadata_location.rsplit("/metadata/", 1)[0]
    other_location = other.metadata_location.rsplit("/metadata/", 1)[0]
    namespace_id = session.scalars(select(Table.namespace_id).where(Table.id == own.uuid)).one()
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=namespace_id,
            name="odd",
            table_type=TableType.ICEBERG,
            metadata_location=f"file://{store}/managed/gold/odd.metadata.json",
        )
    )
    session.commit()
    environment_id = session.scalars(select(Environment.id)).one()

    found = other_table_locations(
        session, environment_id=environment_id, table_id=own.uuid, location=own_location
    )

    assert sorted(set(found)) == [other_location]
