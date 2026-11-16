"""`DeployService.get`: why a queued deploy has not started (`waiting_for`)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog, python_task_run
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_projects_deploys import _repo

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.db import db_now
from lakana_core.models import (
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    QueryOrigin,
    QueryRun,
    QueryRunStatus,
    Worker,
)
from lakana_core.projects.deploys import DeployService
from lakana_core.runs.waiting import WaitReason


@pytest.fixture
def env(session: Session, tmp_path: Path) -> Environment:
    """Create `dev`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    Environment
        The row.
    """
    _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    return session.scalars(select(Environment)).one()


def _queued(session: Session, env: Environment, *, ago: int = 0) -> DeployRun:
    """Insert a queued deploy of a new repository.

    Parameters
    ----------
    session
        Database session; commits.
    env
        The environment.
    ago
        Seconds before now it was queued.

    Returns
    -------
    DeployRun
        The row.
    """
    run = DeployRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        repository_id=_repo(session, f"r{uuid.uuid4().hex[:8]}").id,
        kind=DeployKind.DEPLOY,
        status=DeployStatus.QUEUED,
        created_at=db_now(session) - timedelta(seconds=ago),
    )
    session.add(run)
    session.commit()
    return run


def _worker(session: Session, worker_id: str, takes: str = "all", age: int = 5) -> None:
    """Insert a worker whose heartbeat is `age` seconds old by the database's clock.

    Parameters
    ----------
    session
        Database session; commits.
    worker_id
        Its id.
    takes
        What it takes (`all`, `interactive` or `pipeline`).
    age
        Seconds since the heartbeat.
    """
    beat = db_now(session) - timedelta(seconds=age)
    session.add(
        Worker(id=worker_id, host="h", version="1", started_at=beat, heartbeat_at=beat, takes=takes)
    )
    session.commit()


def _reason(
    session: Session, run: DeployRun, principal: PrincipalRef, lease: int = 60
) -> WaitReason | None:
    """Read the deploy through the service and return its wait reason.

    Parameters
    ----------
    session
        Database session.
    run
        The deploy.
    principal
        The reader.
    lease
        The run lease in seconds.

    Returns
    -------
    WaitReason or None
        `waiting_for` of the view.
    """
    service = DeployService(session, AllowAllAuthorization(), lease_seconds=lease)
    return service.get(principal, "dev", run.id).waiting_for


def test_no_worker_alive(session: Session, env: Environment, principal: PrincipalRef) -> None:
    reason = _reason(session, _queued(session, env), principal)
    assert reason is not None
    assert reason.code == "no_worker"
    assert "this deploy cannot start" in reason.message


def test_a_worker_whose_lease_is_over_does_not_count(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    _worker(session, "w", age=61)
    run = _queued(session, env)
    reason = _reason(session, run, principal)
    assert reason is not None and reason.code == "no_worker"
    later = _reason(session, run, principal, lease=120)
    assert later is not None and later.code == "starting"


def test_only_interactive_workers_alive(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    _worker(session, "w", takes="interactive")
    reason = _reason(session, _queued(session, env), principal)
    assert reason is not None
    assert reason.code == "no_worker_of_kind"
    assert "LAKANA_WORKER__TAKES=pipeline" in reason.message
    assert "this deploy cannot start" in reason.message


def test_a_free_pipeline_worker_means_starting(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    _worker(session, "w", takes="pipeline")
    reason = _reason(session, _queued(session, env), principal)
    assert reason is not None
    assert (reason.code, reason.ahead) == ("starting", 0)
    assert "the deploy starts within about a second" in reason.message


def test_every_pipeline_worker_busy_and_queued_work_ahead(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    _worker(session, "w", takes="pipeline")
    session.add(
        QueryRun(
            origin=QueryOrigin.WRITTEN,
            id=uuid.uuid4(),
            environment_id=env.id,
            sql_text="SELECT 1",
            status=QueryRunStatus.RUNNING,
            worker_id="w",
        )
    )
    session.commit()
    _queued(session, env, ago=30)
    run = _queued(session, env)
    reason = _reason(session, run, principal)
    assert reason is not None
    assert (reason.code, reason.ahead, reason.workers) == ("workers_busy", 1, 1)
    assert "is busy" in reason.message


@pytest.mark.parametrize("status", [DeployStatus.RUNNING, DeployStatus.SUCCEEDED])
def test_a_deploy_that_is_not_queued_has_no_reason(
    session: Session, env: Environment, principal: PrincipalRef, status: DeployStatus
) -> None:
    run = _queued(session, env)
    run.status = status
    session.commit()
    assert _reason(session, run, principal) is None


def test_the_history_does_not_compute_reasons(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    _queued(session, env)
    service = DeployService(session, AllowAllAuthorization())
    assert [v.waiting_for for v in service.history_page(principal, "dev")[0]] == [None]


def test_a_pipeline_worker_that_holds_a_task_is_busy_for_a_deploy(
    session: Session, env: Environment, principal: PrincipalRef
) -> None:
    """The poller claims no deploy while it supervises a task, so the deploy waits."""
    _worker(session, "w1", takes="pipeline")
    python_task_run(session, principal.id)
    reason = _reason(session, _queued(session, env), principal)
    assert reason is not None
    assert (reason.code, reason.workers) == ("workers_busy", 1)
