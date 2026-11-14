"""On PostgreSQL the landed check of a rollback filters its audit rows by their detail (#100)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.audit.service import AuditService
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, Securable
from lakana_core.catalog.service import CatalogService
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEventType, Base, RollbackOutcome
from lakana_core.storage.resolver import StorageResolver

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


def test_the_landed_check_matches_task_run_claim_and_outcome_on_postgres(
    session: Session, principal: PrincipalRef
) -> None:
    namespace = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=namespace, name="orders")
    securable = Securable.table_scope("dev", namespace, "orders")
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    task_run_id = uuid.uuid4()
    audit = AuditService(session)

    def record(**detail: object) -> None:
        base: dict[str, object] = {
            "task_run_id": str(task_run_id),
            "claim": 1,
            "outcome": "rolled_back",
        }
        audit.record(
            principal.id,
            AuditEventType.TABLE_ROLLED_BACK,
            securable=securable,
            detail={**base, **detail},
        )
        session.commit()

    def landed(claim: int | None = 1) -> RollbackOutcome | None:
        return catalog._rollback_landed(ref, task_run_id, claim, uuid.uuid4())

    record(claim=0)
    record(outcome="failed")
    record(task_run_id=str(uuid.uuid4()))
    assert landed() is None
    record(outcome="unpublished")
    assert landed() is RollbackOutcome.UNPUBLISHED
    assert landed(claim=2) is None
    assert landed(claim=None) is None
    record(claim=None, outcome="rolled_back")
    assert landed(claim=None) is RollbackOutcome.ROLLED_BACK
