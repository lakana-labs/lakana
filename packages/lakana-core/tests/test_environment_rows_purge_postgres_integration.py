"""Purging only the rows of a deleted environment, on Postgres (#261).

The purge deletes from several tables in one transaction: the environment's location records,
under their row locks, then the row itself, whose delete cascades through its namespaces to
the artifacts left and to its secrets and runs. Postgres checks the foreign keys and the
``FOR UPDATE`` locks that SQLite ignores.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_environment_rows_purge import _old, _records
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig, StorageConfig
from lakana_core.db import create_engine
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    Environment,
    Namespace,
    Table,
    TableLocation,
)
from lakana_core.storage.resolver import StorageResolver

pytestmark = pytest.mark.integration


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


def test_the_rows_go_and_the_name_is_created_again(
    engine: sqlalchemy.Engine, tmp_path: Path
) -> None:
    """The row, its records, namespaces and artifacts go in one commit; the name is free."""
    with Session(engine) as session:
        principal = ensure_bootstrap_principal(session, "admin")
        env = _old(session, principal)
        env_id = env.id
        deleted = _catalog(session).delete_environment(principal, "old")
        assert deleted.deleted_at is not None
        _records(session, env, deleted.deleted_at)

    with Session(engine) as session:
        assert _catalog(session).purge_environment_rows(principal, "old") == 2

    with Session(engine) as session:
        assert session.scalars(select(Environment).where(Environment.id == env_id)).all() == []
        assert session.scalars(select(TableLocation)).all() == []
        assert session.scalars(select(Namespace)).all() == []
        assert session.scalars(select(Table)).all() == []
        (event,) = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.ENVIRONMENT_PURGED)
        ).all()
        assert (event.principal_id, event.detail["rows_only"]) == (principal.id, True)
        storage = StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})
        created = _catalog(session).create_environment(principal, name="old", storage=storage)
        assert created.id != env_id
