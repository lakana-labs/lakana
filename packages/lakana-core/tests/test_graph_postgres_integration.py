"""The metadata graph's lineage, neighbours and impact on a real PostgreSQL (#374)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from graph_test_helpers import KEY_IMPACT, Grants, build_star
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.graph_model import NodeKind
from lakana_core.catalog.graph_service import GraphService
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import Base

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


def test_lineage_and_neighbours_read_the_same_on_postgres(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    star = build_star(session, tmp_path, principal)
    service = GraphService(session, Grants(policies=True), "en")
    page = service.lineage(principal, "dev", "gold.dim_store", depth=1)
    assert {(n.kind, n.name) for n in page.nodes} == {
        (NodeKind.TABLE, "gold.dim_store"),
        (NodeKind.ASSET, "gold.dim_store"),
        (NodeKind.TABLE, "bronze.stores_raw"),
        (NodeKind.ASSET, "gold.fact_sales"),
        (NodeKind.TABLE, "gold.fact_sales"),
    }
    near = service.neighbours(principal, "dev", "column:gold.dim_store/store_id")
    assert star.reference("gold.fact_sales", "store_id") in {n.id for n in near.nodes}


def test_impact_reads_the_same_on_postgres(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    star = build_star(session, tmp_path, principal)
    star.add_row_filter(principal, "gold.dim_store")
    page = GraphService(session, Grants(policies=True), "en").impact(
        principal, "dev", "gold.dim_store", key=True
    )
    assert {(n.kind, n.name, n.reason, n.may_use) for n in page.nodes} == KEY_IMPACT
