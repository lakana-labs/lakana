"""A CI trust rule's opt-in to starting `on_deploy` jobs, on a real PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from ci_trust_test_helpers import add_rule, ci_world
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from test_ci_deploy_jobs import _ci_deploy, _events, _runs, _start
from testcontainers.postgres import PostgresContainer

from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.models import AuditEventType, Base, RunTrigger
from lakana_core.projects.deploy_jobs import OUTCOME_NOT_STARTED, OUTCOME_STARTED

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


@pytest.mark.parametrize("opted_in", [True, False])
def test_a_ci_deploy_starts_its_on_deploy_job_only_when_its_rule_opts_in(
    session: Session, tmp_path: Path, opted_in: bool
) -> None:
    world = ci_world(session, tmp_path)
    rule_id = add_rule(session, world, start_on_deploy_jobs=opted_in)
    ci, run = _ci_deploy(session)

    (outcome,) = _start(session, ci, run)

    if not opted_in:
        assert outcome.outcome == OUTCOME_NOT_STARTED
        assert outcome.message is not None and "--start-on-deploy-jobs" in outcome.message
        assert _runs(session) == []
        return
    (started,) = _runs(session)
    assert (outcome.outcome, outcome.run_id) == (OUTCOME_STARTED, started.id)
    assert started.trigger is RunTrigger.DEPLOY
    (event,) = _events(session, AuditEventType.RUN_STARTED)
    assert (event.detail["ci_rule_id"], event.detail["allowed_by"]) == (
        str(rule_id),
        "start_on_deploy_jobs",
    )
