"""The access advisory lock on a real PostgreSQL: an apply runs alone (#46)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.access.managed import lock_access
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine

pytestmark = pytest.mark.integration

_LOCK_NOT_AVAILABLE = "55P03"
"""The SQLSTATE of a lock wait that ran past `lock_timeout`."""


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Start one Postgres.

    Yields
    ------
    Engine
        An engine on it.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        yield engine
        engine.dispose()


def _blocked(engine: Engine, *, shared: bool) -> bool:
    """Try to take the lock in a second session, waiting at most a moment.

    Parameters
    ----------
    engine
        The engine.
    shared
        Whether to ask for the shared lock.

    Returns
    -------
    bool
        True when the wait ran out (SQLSTATE 55P03); False when the lock was taken.
    """
    with Session(engine) as other:
        other.execute(text("SET LOCAL lock_timeout = '200ms'"))
        try:
            lock_access(other, shared=shared)
        except DBAPIError as exc:
            other.rollback()
            return _LOCK_NOT_AVAILABLE in str(exc.orig)
        other.rollback()
        return False


def test_an_exclusive_lock_blocks_every_other_until_the_transaction_ends(engine: Engine) -> None:
    """Task 5 review M2: an apply's lock blocks another apply and a guarded change."""
    with Session(engine) as first:
        lock_access(first, shared=False)
        assert _blocked(engine, shared=False)
        assert _blocked(engine, shared=True)
        first.commit()
    assert not _blocked(engine, shared=False)


def test_shared_locks_run_beside_each_other_and_block_an_apply(engine: Engine) -> None:
    """Guarded changes share the lock; an apply waits for them."""
    with Session(engine) as first:
        lock_access(first, shared=True)
        assert not _blocked(engine, shared=True)
        assert _blocked(engine, shared=False)
        first.rollback()
    assert not _blocked(engine, shared=False)
