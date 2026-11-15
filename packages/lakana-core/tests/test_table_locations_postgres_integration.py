"""The publish fence and the undrop against the sweep, on Postgres, in two sessions and threads.

The create's commit moves its claim to LIVE in the transaction that inserts the table; the sweep
moves a claim to ABANDONED under the same row lock. An undrop moves a tombstone back to LIVE
under that lock too, and the sweep marks a purge as started under it before the first delete.
Whichever takes the lock first wins, and the other sees its result: these tests hold one side
inside its transaction, wait until the other side is blocked on the row lock, and then let the
first side commit.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
import sqlalchemy
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog import location_sweep as sweep_module
from lakana_core.catalog.location_sweep import LocationSweepReport, sweep_table_locations
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.tombstones import PURGE_STARTED_MESSAGE
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError
from lakana_core.identifiers import TableRef
from lakana_core.models import Base, Table, TableLocation, TableLocationState
from lakana_core.runs.lockdown import ProtectedWriteGuard
from lakana_core.storage.resolver import StorageResolver

pytestmark = pytest.mark.integration

_REF = TableRef.from_dotted("dev", "gold.orders")
_LATER = datetime.now(UTC) + timedelta(days=30)


@pytest.fixture(scope="module")
def engine() -> Iterator[sqlalchemy.Engine]:
    """Start Postgres once for the module and create the schema.

    Yields
    ------
    sqlalchemy.Engine
        An engine on the container.
    """
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        built = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(built)
        yield built
        built.dispose()


@pytest.fixture
def catalog(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> Iterator[tuple[CatalogService, PrincipalRef]]:
    """Create a fresh environment on a fresh storage root and return a catalog over it.

    Parameters
    ----------
    engine
        The Postgres engine.
    tmp_path
        The storage root.

    Yields
    ------
    tuple
        The creator's catalog and the acting principal.
    """
    with Session(engine) as session:
        session.execute(text("DELETE FROM table_locations"))
        session.execute(text("DELETE FROM tables"))
        session.execute(text("DELETE FROM namespaces"))
        session.execute(text("DELETE FROM environments"))
        session.commit()
        service = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        principal = ensure_bootstrap_principal(session, "admin")
        service.create_environment(
            principal,
            name="dev",
            storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
        )
        yield service, principal


def _wait_until_blocked(engine: sqlalchemy.Engine, timeout: float = 30.0) -> None:
    """Wait until some backend waits for a lock.

    Parameters
    ----------
    engine
        The Postgres engine.
    timeout
        Seconds before giving up.

    Raises
    ------
    AssertionError
        If no backend waited for a lock within `timeout`.
    """
    deadline = time.monotonic() + timeout
    with engine.connect() as connection:
        while time.monotonic() < deadline:
            waiting = connection.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'")
            ).scalar_one()
            if waiting:
                return
            connection.rollback()
            time.sleep(0.05)
    raise AssertionError("the other side never waited for the row lock")


def _in_thread(work: Callable[[], object]) -> tuple[threading.Thread, dict[str, object]]:
    """Start work in a thread and keep its result or its exception.

    Parameters
    ----------
    work
        What to run.

    Returns
    -------
    tuple
        The started thread and the dict that receives ``result`` or ``error``.
    """
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = work()
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _sweep(engine: sqlalchemy.Engine) -> LocationSweepReport:
    """Run the sweep in a session of its own, long past every grace period.

    Parameters
    ----------
    engine
        The Postgres engine.

    Returns
    -------
    LocationSweepReport
        What it did.
    """
    with Session(engine) as session:
        return sweep_table_locations(session, StorageResolver(session), now=_LATER, grace_hours=24)


def _schema() -> Schema:
    """Build a one-column schema.

    Returns
    -------
    Schema
        ``id bigint``.
    """
    return Schema(NestedField(1, "id", LongType(), required=False))


def _state(engine: sqlalchemy.Engine) -> list[TableLocationState]:
    """Read every record's state.

    Parameters
    ----------
    engine
        The Postgres engine.

    Returns
    -------
    list of TableLocationState
        The states.
    """
    with Session(engine) as session:
        return list(session.scalars(select(TableLocation.state)))


def test_a_create_that_takes_the_lock_first_is_published_and_the_sweep_waits(
    engine: sqlalchemy.Engine, catalog: tuple[CatalogService, PrincipalRef]
) -> None:
    """The fence holds the row lock until its commit; the sweep then sees LIVE and keeps it."""
    service, principal = catalog
    real = ProtectedWriteGuard.record_pending
    swept: dict[str, object] = {}

    def fence_held_then_sweep(guard: ProtectedWriteGuard) -> None:
        thread, outcome = _in_thread(lambda: _sweep(engine))
        swept["thread"], swept["outcome"] = thread, outcome
        _wait_until_blocked(engine)
        real(guard)

    with mock.patch.object(ProtectedWriteGuard, "record_pending", fence_held_then_sweep):
        service.create_table(principal, _REF, schema=_schema())

    thread = swept["thread"]
    assert isinstance(thread, threading.Thread)
    thread.join(timeout=60)
    outcome = swept["outcome"]
    assert isinstance(outcome, dict)
    assert "error" not in outcome, outcome
    report = outcome["result"]
    assert isinstance(report, LocationSweepReport)
    assert (report.claims_abandoned, report.locations_purged, report.files_deleted) == (0, 0, 0)
    assert _state(engine) == [TableLocationState.LIVE]


def test_a_sweep_that_takes_the_lock_first_abandons_and_the_create_rolls_back(
    engine: sqlalchemy.Engine, catalog: tuple[CatalogService, PrincipalRef]
) -> None:
    """The sweep holds the row lock; the fence waits, finds no claim, and nothing is published."""
    _, principal = catalog
    real = sweep_module.abandon_claim
    created: dict[str, object] = {}

    with Session(engine) as session:
        creator = CatalogService(session, StorageResolver(session), AllowAllAuthorization())

        def abandon_then_let_the_create_try(*args: object, **kwargs: object) -> None:
            real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
            thread, outcome = _in_thread(
                lambda: creator._publish_table(prepared, prepared.metadata, owner_id=principal.id)
            )
            created["thread"], created["outcome"] = thread, outcome
            _wait_until_blocked(engine)

        prepared = creator._prepare_table(
            _REF,
            schema=_schema(),
            partition_spec=None,
            sort_order=None,
            properties=None,
            location=None,
            holder_run_id=uuid.uuid4(),  # a run the catalog does not know: the grace decides
            refuse_declared_for=None,
        )
        with mock.patch.object(sweep_module, "abandon_claim", abandon_then_let_the_create_try):
            report = _sweep(engine)

        thread = created["thread"]
        assert isinstance(thread, threading.Thread)
        thread.join(timeout=60)
        outcome = created["outcome"]
        assert isinstance(outcome, dict)
        assert isinstance(outcome.get("error"), ConflictError), outcome
        assert report.claims_abandoned == 1
        session.rollback()
        assert session.scalars(select(Table.id).where(Table.name == "orders")).all() == []


def _files(root: Path) -> list[Path]:
    """List every file below a directory.

    Parameters
    ----------
    root
        The directory.

    Returns
    -------
    list of Path
        Sorted.
    """
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_an_undrop_that_takes_the_lock_first_wins_and_the_purge_never_starts(
    engine: sqlalchemy.Engine, catalog: tuple[CatalogService, PrincipalRef], tmp_path: Path
) -> None:
    """The undrop holds the lock; the sweep waits, then sees LIVE and deletes nothing."""
    service, principal = catalog
    service.create_table(principal, _REF, schema=_schema())
    service.drop_table(principal, _REF)
    before = _files(tmp_path)
    real = ProtectedWriteGuard.record_pending
    swept: dict[str, object] = {}

    def undrop_held_then_sweep(guard: ProtectedWriteGuard) -> None:
        thread, outcome = _in_thread(lambda: _sweep(engine))
        swept["thread"], swept["outcome"] = thread, outcome
        _wait_until_blocked(engine)
        real(guard)

    with mock.patch.object(ProtectedWriteGuard, "record_pending", undrop_held_then_sweep):
        service.undrop_table(principal, _REF)

    thread = swept["thread"]
    assert isinstance(thread, threading.Thread)
    thread.join(timeout=60)
    outcome = swept["outcome"]
    assert isinstance(outcome, dict)
    assert "error" not in outcome, outcome
    report = outcome["result"]
    assert isinstance(report, LocationSweepReport)
    assert (report.locations_purged, report.files_deleted) == (0, 0)
    assert _state(engine) == [TableLocationState.LIVE]
    assert _files(tmp_path) == before
    service.load_table(principal, _REF)


def test_a_purge_that_takes_the_lock_first_wins_and_the_undrop_is_refused(
    engine: sqlalchemy.Engine, catalog: tuple[CatalogService, PrincipalRef], tmp_path: Path
) -> None:
    """The sweep marks the purge and deletes under the lock; the undrop waits, then is refused."""
    service, principal = catalog
    service.create_table(principal, _REF, schema=_schema())
    service.drop_table(principal, _REF)
    real = sweep_module.delete_quietly
    restored: dict[str, object] = {}

    with Session(engine) as session:
        restorer = CatalogService(session, StorageResolver(session), AllowAllAuthorization())

        def delete_while_an_undrop_waits(*args: object, **kwargs: object) -> bool:
            if "thread" not in restored:
                thread, outcome = _in_thread(lambda: restorer.undrop_table(principal, _REF))
                restored["thread"], restored["outcome"] = thread, outcome
                _wait_until_blocked(engine)
            return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

        with mock.patch.object(sweep_module, "delete_quietly", delete_while_an_undrop_waits):
            report = _sweep(engine)

        thread = restored["thread"]
        assert isinstance(thread, threading.Thread)
        thread.join(timeout=60)
        outcome = restored["outcome"]
        assert isinstance(outcome, dict)
        error = outcome.get("error")
        assert isinstance(error, ConflictError), outcome
        assert str(error) == PURGE_STARTED_MESSAGE.format(table="gold.orders")
        assert report.locations_purged == 1
        session.rollback()
        assert session.scalars(select(Table.id).where(Table.name == "orders")).all() == []
    assert _files(tmp_path / "managed") == []
