"""Needs your attention's failures in a row on a real PostgreSQL.

`failure_streaks` ranks each job's runs, each repository's deploys and each check's results
with a running window sum (``ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW``); the unit tests
run it on SQLite, and this one runs the same rows on PostgreSQL, where the server runs it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from core_test_helpers import ensure_table
from home_test_helpers import (
    check_result,
    deployed_job,
    finished_deploy,
    finished_run,
    grant,
    person,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core import migrate
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.home.attention import AttentionItem, AttentionService
from lakana_core.models import CheckOutcome, DeployStatus, RunStatus

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Start one Postgres, migrated to head.

    Yields
    ------
    Engine
        An engine on it.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url)
        engine = create_engine(DatabaseConfig(url=url))
        yield engine
        engine.dispose()


def test_failures_in_a_row_are_counted_on_postgres(engine: Engine) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    with Session(engine) as session:
        admin = person(session, "ada")
        grant(session, admin, role=Role.ADMIN)
        repo, version = deployed_job(session, "prod", "analytics", "nightly_ranking")
        _, table_id = ensure_table(session, "prod", "gold", "station_ranking")
        session.commit()
        ok = finished_run(
            session,
            repo,
            version,
            job="nightly_ranking",
            status=RunStatus.SUCCEEDED,
            finished_at=now - timedelta(hours=40),
        )
        check = "gold.station_ranking/not_null_station_id"
        check_result(
            session,
            ok,
            table_id,
            name=check,
            outcome=CheckOutcome.PASSED,
            finished_at=now - timedelta(hours=40),
        )
        runs = []
        for hours in (30, 20, 10):
            run = finished_run(
                session,
                repo,
                version,
                job="nightly_ranking",
                status=RunStatus.FAILED,
                finished_at=now - timedelta(hours=hours),
            )
            check_result(
                session,
                run,
                table_id,
                name=check,
                outcome=CheckOutcome.FAILED,
                finished_at=now - timedelta(hours=hours),
            )
            runs.append(run)
        finished_deploy(
            session, repo, version, status=DeployStatus.FAILED, finished_at=now - timedelta(hours=9)
        )
        deploy = finished_deploy(
            session, repo, version, status=DeployStatus.FAILED, finished_at=now - timedelta(hours=8)
        )
        newest, deploy_id = str(runs[-1].id), str(deploy.id)
        service = AttentionService(session, GrantAuthorization(session), lease_seconds=60)
        items, truncated = service.for_caller(admin)
    assert truncated is False
    assert items == [
        AttentionItem(
            "deploy_failed",
            "prod",
            "analytics",
            deploy_id,
            now - timedelta(hours=8),
            count=2,
            since=now - timedelta(hours=9),
        ),
        AttentionItem(
            "run_failed",
            "prod",
            "nightly_ranking",
            newest,
            now - timedelta(hours=10),
            count=3,
            since=now - timedelta(hours=30),
        ),
        AttentionItem(
            "check_failed",
            "prod",
            check,
            newest,
            now - timedelta(hours=10),
            count=3,
            since=now - timedelta(hours=30),
        ),
    ]


def test_two_jobs_of_one_repository_count_their_own_failures_on_postgres(engine: Engine) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    with Session(engine) as session:
        admin = person(session, "bo")
        grant(session, admin, role=Role.ADMIN)
        repo, version = deployed_job(session, "stage", "shop", "daily_orders")
        plan = (
            ("daily_orders", RunStatus.FAILED, 30),
            ("weekly_ledger", RunStatus.FAILED, 25),
            ("daily_orders", RunStatus.FAILED, 20),
            ("weekly_ledger", RunStatus.SUCCEEDED, 15),
            (None, RunStatus.FAILED, 12),
            ("daily_orders", RunStatus.FAILED, 10),
            ("weekly_ledger", RunStatus.FAILED, 5),
        )
        ids = {}
        for job, status, hours in plan:
            run = finished_run(
                session,
                repo,
                version,
                job=job,
                status=status,
                finished_at=now - timedelta(hours=hours),
            )
            ids[job] = str(run.id)
        service = AttentionService(session, GrantAuthorization(session), lease_seconds=60)
        items, _ = service.for_caller(admin)
    stage = [item for item in items if item.environment == "stage"]
    assert stage == [
        AttentionItem(
            "run_failed", "stage", "weekly_ledger", ids["weekly_ledger"], now - timedelta(hours=5)
        ),
        AttentionItem(
            "run_failed",
            "stage",
            "daily_orders",
            ids["daily_orders"],
            now - timedelta(hours=10),
            count=3,
            since=now - timedelta(hours=30),
        ),
        AttentionItem("run_failed", "stage", "shop", ids[None], now - timedelta(hours=12)),
    ]
