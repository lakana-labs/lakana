"""A query run keeps the figures its query child measured, whichever way it ended."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest
from check_run_helpers import _drain, _outside_table
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from task_run_helpers import World, _entry, _start, _task

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.models import (
    Environment,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    TaskAttempt,
    TaskStatus,
)
from lakana_core.runs.resources import AttemptResources
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import pipeline
from lakana_worker.pipeline import run_one
from lakana_worker.resources import current_collector


@pytest.fixture
def principal(file_session: Session, tmp_path: Path) -> PrincipalRef:
    """Persist an admin and an environment `dev`.

    Parameters
    ----------
    file_session
        Database session on a file.
    tmp_path
        Storage root.

    Returns
    -------
    PrincipalRef
        The admin.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    file_session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    file_session.commit()
    CatalogService(
        file_session, StorageResolver(file_session), AllowAllAuthorization()
    ).create_environment(
        ref,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path / 'storage'}", options={}),
    )
    return ref


def _queued(session: Session, principal: PrincipalRef, sql: str) -> uuid.UUID:
    """Queue a read query.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    sql
        The query.

    Returns
    -------
    uuid.UUID
        The run's id.
    """
    env = session.scalars(select(Environment)).one()
    run = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=principal.id,
        sql_text=sql,
        status=QueryRunStatus.QUEUED,
    )
    session.add(run)
    session.commit()
    return run.id


def _figures(session: Session, run_id: uuid.UUID) -> AttemptResources:
    """Read the stored figures of a run.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.

    Returns
    -------
    AttemptResources
        What the run row holds.
    """
    session.expire_all()
    run = session.get(QueryRun, run_id)
    assert run is not None
    return AttemptResources(run.peak_memory_bytes, run.cpu_seconds, run.bytes_read)


def _measuring(monkeypatch: pytest.MonkeyPatch, figures: AttemptResources) -> list[object]:
    """Make `execute_query` stand in for a child that measured ``figures``.

    Parameters
    ----------
    monkeypatch
        Patches the executor.
    figures
        What the pretend child adds to the current collector.

    Returns
    -------
    list
        The collectors seen, one per call.
    """
    seen: list[object] = []

    def execute(*args: object, **kwargs: object) -> object:
        """Add the figures to the collector and fail like bad SQL after the child started.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        ValueError
            Always, to end the run as failed.
        """
        collector = current_collector()
        seen.append(collector)
        assert collector is not None
        collector.add(figures)
        raise ValueError("bad sql")

    monkeypatch.setattr(pipeline, "execute_query", execute)
    return seen


def test_a_run_that_failed_after_its_child_started_keeps_what_was_measured(
    file_session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _measuring(monkeypatch, AttemptResources(4096, 0.5, None))
    run_id = _queued(file_session, principal, "SELECT 1")
    run_one(file_session, run_id, AllowAllAuthorization())
    assert seen
    assert _figures(file_session, run_id) == AttemptResources(4096, 0.5, None)
    assert current_collector() is None


def test_a_run_that_never_started_a_child_keeps_null(
    file_session: Session, principal: PrincipalRef
) -> None:
    run_id = _queued(file_session, principal, "SELECT * FROM missing_schema.nothing")
    run_one(file_session, run_id, AllowAllAuthorization())
    assert _figures(file_session, run_id) == AttemptResources(None, None, None)


def test_a_refused_run_keeps_null(file_session: Session, principal: PrincipalRef) -> None:
    run_id = _queued(file_session, principal, "SELECT 1")
    file_session.execute(
        update(Principal).where(Principal.id == principal.id).values(is_active=False)
    )
    file_session.commit()
    run_one(file_session, run_id, AllowAllAuthorization())
    assert _figures(file_session, run_id) == AttemptResources(None, None, None)


def test_each_run_gets_its_own_collector(
    file_session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _measuring(monkeypatch, AttemptResources(10, 1.0, 5))
    first = _queued(file_session, principal, "SELECT 1")
    second = _queued(file_session, principal, "SELECT 2")
    run_one(file_session, first, AllowAllAuthorization())
    run_one(file_session, second, AllowAllAuthorization())
    assert seen[0] is not seen[1]
    assert _figures(file_session, second) == AttemptResources(10, 1.0, 5)
    assert threading.current_thread() is threading.main_thread()


def test_a_succeeded_run_keeps_the_figures_of_its_real_child(
    file_session: Session, principal: PrincipalRef
) -> None:
    run_id = _queued(file_session, principal, "SELECT 1 AS x")
    assert run_one(file_session, run_id, AllowAllAuthorization()) is None
    stored = _figures(file_session, run_id)
    assert stored.cpu_seconds is not None and stored.cpu_seconds >= 0
    assert (stored.peak_memory_bytes or 1) > 0


def test_a_sql_task_attempt_carries_the_figures_of_its_write_run(world: World) -> None:
    """End to end through `_finish`: no figures are passed, so `record_attempt` falls back."""
    _outside_table(world, "bronze.events", [1, 2])
    world.set_manifest(_entry("gold.copy", "SELECT id FROM bronze.events"))
    _start(world)
    _drain(world)
    assert _task(world, "gold.copy").status is TaskStatus.SUCCEEDED
    with world.session() as session:
        attempt = session.scalars(select(TaskAttempt)).one()
        write = session.scalars(select(QueryRun).where(QueryRun.task_run_id.is_not(None))).one()
        assert write.cpu_seconds is not None
        assert attempt.cpu_seconds == write.cpu_seconds
        assert attempt.peak_memory_bytes == write.peak_memory_bytes
