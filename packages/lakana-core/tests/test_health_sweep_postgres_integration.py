"""The table health sweep on a real PostgreSQL: its retention delete and a freshness row (#340)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from health_test_helpers import _seed, health_catalog
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import PrincipalRef
from lakana_core.catalog import health_sweep
from lakana_core.catalog.health_sweep import sweep_health
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import Base, TableHealthSignal

pytestmark = pytest.mark.integration


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


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Give each test a fresh schema.

    Parameters
    ----------
    engine
        The engine.

    Yields
    ------
    Session
        A session on the fresh schema.
    """
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_retention_keeps_the_newest_unmarked_row_of_each_key_on_postgres(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_sweep, "RETENTION_BATCH", 2)
    health_catalog(session, tmp_path, principal)
    _seed(session, "schema", {"columns": [["id", "long"]]}, count=9)
    _seed(session, "range", {}, count=1)
    _seed(session, "range", {}, count=3, column="id")
    session.execute(
        update(TableHealthSignal)
        .where(TableHealthSignal.signal == "schema", TableHealthSignal.snapshot_id == 9)
        .values(set_back_at=datetime(2026, 10, 10, tzinfo=UTC))
    )
    session.commit()
    swept = sweep_health(
        session,
        now=datetime(2027, 3, 1, tzinfo=UTC),
        retention_days=90,
        rotation=0,
        should_stop=lambda: False,
    )
    remaining = session.execute(
        select(
            TableHealthSignal.signal, TableHealthSignal.column_name, TableHealthSignal.snapshot_id
        )
    ).all()
    assert sorted(remaining, key=str) == sorted(
        [("schema", None, 8), ("range", None, 1), ("range", "id", 3), ("freshness", None, 8)],
        key=str,
    )
    assert (swept.deleted, swept.evaluated, swept.recorded) == (10, 1, 1)
