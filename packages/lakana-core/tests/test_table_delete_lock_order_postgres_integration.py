"""A delete of several tables and a query's read recording, on Postgres, in two sessions.

Read recording (`record_table_reads`) locks the tables a query read in id order. A deletion of
an environment drops every table of it; when it locked them in another order (by name), it
could hold one table the completion wanted while waiting for one the completion held, and
Postgres ended one of them with a deadlock (#558). The completion here holds the first table
by id, waits until the deletion is blocked, and then records its read of the second. In the
second sequence a later read of the first table was recorded already, so the update of its
``last_read_at`` locks nothing; the table lock of its own keeps that case in order too.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.health import record_table_reads
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.models import Base, Namespace, Table, TableRead, TableType
from lakana_core.storage.resolver import StorageResolver

pytestmark = pytest.mark.integration

_JOIN_SECONDS = 60.0


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


def _catalog(session: Session) -> CatalogService:
    """Build a catalog with allow-all authorization.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return CatalogService(session, StorageResolver(session), AllowAllAuthorization())


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
    raise AssertionError("the deletion never waited for a table's row lock")


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
        """Run the work and keep what it gave."""
        try:
            outcome["result"] = work()
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def _two_views(
    session: Session, principal: PrincipalRef, environment_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """Add two views to ``gold`` whose name order is the reverse of their id order.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        Their owner.
    environment_id
        The environment whose ``gold`` namespace takes them.

    Returns
    -------
    tuple
        The lower id (named ``zz_first``) and the higher id (named ``aa_second``).
    """
    namespace_id = session.scalars(
        select(Namespace.id).where(
            Namespace.environment_id == environment_id, Namespace.path == "gold"
        )
    ).one()
    low = uuid.UUID(int=uuid.uuid4().int >> 8)
    high = uuid.UUID(int=(0xFF << 120) | (uuid.uuid4().int >> 8))
    for table_id, name in ((low, "zz_first"), (high, "aa_second")):
        session.add(
            Table(
                id=table_id,
                namespace_id=namespace_id,
                name=name,
                table_type=TableType.VIEW,
                owner_id=principal.id,
            )
        )
    session.commit()
    return low, high


def test_a_deletion_and_a_completion_that_read_two_of_its_tables_do_not_deadlock(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    """Both commit: the deletion waits for the completion instead of holding the second table."""
    with Session(engine) as session:
        principal = ensure_bootstrap_principal(session, "admin")
        environment = _catalog(session).create_environment(
            principal,
            name="dev",
            storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
        )
        _two_views(session, principal, environment.id)

    with Session(engine) as completion:
        now = datetime.now(UTC)
        record_table_reads(
            completion,
            environment="dev",
            principal_id=principal.id,
            dotted_refs=["gold.zz_first"],
            now=now,
        )

        def delete() -> None:
            """Delete the environment in a session of its own."""
            with Session(engine) as deletion:
                _catalog(deletion).delete_environment(principal, "dev")

        thread, outcome = _in_thread(delete)
        try:
            _wait_until_blocked(engine)
            record_table_reads(
                completion,
                environment="dev",
                principal_id=principal.id,
                dotted_refs=["gold.aa_second"],
                now=now,
            )
            completion.commit()
        finally:
            completion.rollback()
            thread.join(_JOIN_SECONDS)

    assert not thread.is_alive(), "the deletion never finished"
    assert "error" not in outcome, outcome.get("error")
    with Session(engine) as session:
        assert session.scalars(select(Table.id)).all() == []
        assert session.scalars(select(TableRead.id)).all() == []


def test_a_completion_whose_read_was_already_recorded_later_still_locks_the_table_first(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    """Both commit, although the update of ``last_read_at`` skips the first table.

    Another completion recorded a later read of the first table. This completion's update of it
    then matches no row and locks nothing, while its count's upsert holds the table's
    ``table_reads`` row. Without a lock of its own, the deletion took both tables, waited in its
    cascade for that count, and the completion waited for the second table: a deadlock.
    """
    with Session(engine) as session:
        principal = ensure_bootstrap_principal(session, "admin")
        environment = _catalog(session).create_environment(
            principal,
            name="test",
            storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
        )
        _two_views(session, principal, environment.id)
        now = datetime.now(UTC)
        record_table_reads(
            session,
            environment="test",
            principal_id=principal.id,
            dotted_refs=["gold.zz_first"],
            now=now + timedelta(seconds=1),
        )
        session.commit()

    with Session(engine) as completion:
        record_table_reads(
            completion,
            environment="test",
            principal_id=principal.id,
            dotted_refs=["gold.zz_first"],
            now=now,
        )

        def delete() -> None:
            """Delete the environment in a session of its own."""
            with Session(engine) as deletion:
                _catalog(deletion).delete_environment(principal, "test")

        thread, outcome = _in_thread(delete)
        try:
            _wait_until_blocked(engine)
            record_table_reads(
                completion,
                environment="test",
                principal_id=principal.id,
                dotted_refs=["gold.aa_second"],
                now=now,
            )
            completion.commit()
        finally:
            completion.rollback()
            thread.join(_JOIN_SECONDS)

    assert not thread.is_alive(), "the deletion never finished"
    assert "error" not in outcome, outcome.get("error")
    with Session(engine) as session:
        assert session.scalars(select(Table.id)).all() == []
        assert session.scalars(select(TableRead.id)).all() == []
