"""The worker's write path under the lockdown: run context and break-glass."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from worker_test_helpers import ActionAllowList

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    BoundTask,
    PrincipalRef,
    RunContext,
    TaskScope,
)
from lakana_core.catalog import service as catalog_service
from lakana_core.catalog.health import StoredStats
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    JobVersion,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.runs.store import WORKER_LOST_WRITE_MESSAGE
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import pipeline
from lakana_worker.pipeline import _run_context, run_one

_REASON = "incident 42: hand fix of one row"


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Persist an admin principal.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The principal.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    return ref


@pytest.fixture
def prod(session: Session, principal: PrincipalRef, tmp_path: Path) -> None:
    """Build a protected `prod` with `bronze.events`.

    Parameters
    ----------
    session
        Database session.
    principal
        The admin.
    tmp_path
        Storage root.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="prod",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path / 'storage'}", options={}),
    )
    catalog.create_table(
        principal,
        TableRef("prod", NamespacePath.from_parts(["bronze"]), "events"),
        schema=Schema(NestedField(1, "id", LongType(), required=False)),
    )
    env = session.scalars(select(Environment).where(Environment.name == "prod")).one()
    env.is_protected = True
    session.commit()


def _write(
    session: Session,
    principal: PrincipalRef,
    *,
    task_run_id: uuid.UUID | None = None,
    reason: str | None = None,
    authz: AuthorizationService | None = None,
) -> QueryRun:
    """Insert a write run into `bronze.events` as the worker or the API would, and run it.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    task_run_id
        The task run context, if any.
    reason
        A break-glass reason, if any.
    authz
        The worker's authorization; allow-all by default.

    Returns
    -------
    QueryRun
        The finished run.
    """
    authz = AllowAllAuthorization() if authz is None else authz
    env = session.scalars(select(Environment).where(Environment.name == "prod")).one()
    run = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=principal.id,
        sql_text="SELECT 1::BIGINT AS id",
        status=QueryRunStatus.RUNNING,
        write_mode=WriteMode.APPEND,
        target_name="bronze.events",
        task_run_id=task_run_id,
        break_glass_reason=reason,
    )
    session.add(run)
    session.commit()
    if task_run_id is not None:
        task = session.get(TaskRun, task_run_id)
        assert task is not None
        task.query_run_id = run.id
        session.commit()
    run_one(session, run.id, authz)
    session.refresh(run)
    return run


def _running_task(session: Session, principal: PrincipalRef, asset: str) -> uuid.UUID:
    """Insert a running task run for `asset` in a run in `prod`.

    Parameters
    ----------
    session
        Database session.
    principal
        The principal the run is bound to.
    asset
        The task's asset.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    env = session.scalars(select(Environment).where(Environment.name == "prod")).one()
    repo = Repository(
        id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a.git"
    )
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=env.id,
        commit_sha="a" * 40,
        manifest_json={},
        code_uri="file:///x",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    run = Run(
        id=uuid.uuid4(),
        environment_id=env.id,
        repository_id=repo.id,
        job_version_id=version.id,
        run_as_principal_id=principal.id,
        kind=RunKind.FULL,
        status=RunStatus.RUNNING,
        selection_json={},
    )
    session.add(run)
    session.flush()
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=run.id,
        asset=asset,
        status=TaskStatus.RUNNING,
        max_attempts=1,
        timeout_seconds=60,
        retry_delay_seconds=0,
        upstream_json=[],
    )
    session.add(task)
    session.commit()
    return task.id


def test_a_write_without_a_run_context_fails_with_the_lockdown_message(
    session: Session, principal: PrincipalRef, prod: None
) -> None:
    """Exit criterion: refused in a protected environment."""
    run = _write(session, principal)
    assert run.status is QueryRunStatus.FAILED
    assert "is protected: only runs of the deployed project version may write" in (
        run.error_message or ""
    )


def test_the_same_write_inside_a_running_task_succeeds(
    session: Session, principal: PrincipalRef, prod: None
) -> None:
    """Exit criterion: the same write inside a run succeeds."""
    run = _write(session, principal, task_run_id=_running_task(session, principal, "bronze.events"))
    assert run.status is QueryRunStatus.SUCCEEDED, run.error_message
    assert run.rows_written == 1


def test_a_break_glass_write_succeeds_and_is_audited_once(
    session: Session, principal: PrincipalRef, prod: None
) -> None:
    """Exit criterion: break-glass succeeds and is audited (once, though two choke points pass)."""
    run = _write(session, principal, reason=_REASON)
    assert run.status is QueryRunStatus.SUCCEEDED, run.error_message
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.BREAK_GLASS_WRITE)
    ).all()
    assert [e.detail["target"] for e in events] == ["bronze.events"]


def test_a_queued_break_glass_write_is_reauthorized_when_the_worker_executes_it(
    session: Session, principal: PrincipalRef, prod: None
) -> None:
    """Submission is not trusted: without MANAGE_ENVIRONMENT now, the write fails unaudited."""
    authz = ActionAllowList(Action.WRITE_TABLE, Action.READ_TABLE, Action.SUBMIT_QUERY)
    run = _write(session, principal, reason=_REASON, authz=authz)
    assert run.status is QueryRunStatus.FAILED
    assert Action.MANAGE_ENVIRONMENT in {a for a, _ in authz.seen}
    assert not session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.BREAK_GLASS_WRITE)
    ).all()


@pytest.fixture
def open_prod(session: Session, prod: None) -> None:
    """Turn `prod` back into an unprotected environment.

    Parameters
    ----------
    session
        Database session.
    prod
        The environment, built protected.
    """
    env = session.scalars(select(Environment).where(Environment.name == "prod")).one()
    env.is_protected = False
    session.commit()


def _reclaim(session: Session, task_run_id: uuid.UUID) -> None:
    """Do what the scheduler's lost-lease reclaim does, in its own commit.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        The task whose lease expired.
    """
    session.execute(
        update(TaskRun)
        .where(TaskRun.id == task_run_id)
        .values(status=TaskStatus.PENDING, query_run_id=None)
    )
    session.execute(
        update(QueryRun)
        .where(QueryRun.task_run_id == task_run_id, QueryRun.status == QueryRunStatus.RUNNING)
        .values(status=QueryRunStatus.FAILED, error_message=WORKER_LOST_WRITE_MESSAGE)
        .execution_options(synchronize_session=False)
    )
    session.commit()


def _location(session: Session, principal: PrincipalRef) -> str | None:
    """Return `prod.bronze.events`' current metadata location.

    Parameters
    ----------
    session
        Database session.
    principal
        The admin.

    Returns
    -------
    str | None
        The location.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    ref = TableRef("prod", NamespacePath.from_parts(["bronze"]), "events")
    return catalog.load_table(principal, ref).metadata_location


def test_a_zombie_write_in_an_unprotected_environment_lands_nothing(
    session: Session,
    principal: PrincipalRef,
    open_prod: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I1: the task is reclaimed before the commit; the write and the reclaim's stamp stand."""
    task_run_id = _running_task(session, principal, "bronze.events")
    before = _location(session, principal)
    original = catalog_service.commit_metadata_pointer

    def reclaim_first(
        pointer_session: Session,
        table_id: uuid.UUID,
        *,
        expected_location: str | None,
        new_location: str,
        publish: bool = False,
        stats: StoredStats | None = None,
    ) -> None:
        with Session(pointer_session.get_bind()) as scheduler:
            _reclaim(scheduler, task_run_id)
        original(
            pointer_session,
            table_id,
            expected_location=expected_location,
            new_location=new_location,
            publish=publish,
            stats=stats,
        )

    monkeypatch.setattr(catalog_service, "commit_metadata_pointer", reclaim_first)
    run = _write(session, principal, task_run_id=task_run_id)
    assert run.status is QueryRunStatus.FAILED
    assert run.error_message == WORKER_LOST_WRITE_MESSAGE
    assert _location(session, principal) == before
    denied = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PROTECTED_WRITE_DENIED)
    ).one()
    assert denied.detail["protected"] is False


def test_a_reclaim_stamp_is_never_overwritten_by_the_attempt_it_failed(
    session: Session,
    principal: PrincipalRef,
    open_prod: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stamp the reclaim put on the run stands over the outcome of the attempt it ended.

    The stamp is simulated by hand, as that of a write without evidence; with evidence the real
    reclaim stamps `SUCCEEDED` and ends the task (`test_runs_store.py`).
    """
    task_run_id = _running_task(session, principal, "bronze.events")
    original = pipeline._run_write

    def reclaim_after_commit(*args: object, **kwargs: object) -> int:
        rows = original(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        with Session(session.get_bind()) as scheduler:
            _reclaim(scheduler, task_run_id)
        return rows

    monkeypatch.setattr(pipeline, "_run_write", reclaim_after_commit)
    run = _write(session, principal, task_run_id=task_run_id)
    assert run.status is QueryRunStatus.FAILED
    assert run.error_message == WORKER_LOST_WRITE_MESSAGE
    completed = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
    ).all()
    assert completed[-1].detail["status"] == "FAILED"


def test_a_write_reclaimed_before_it_starts_is_left_as_the_reclaim_stamped_it(
    session: Session, principal: PrincipalRef, open_prod: None
) -> None:
    """The reclaim lands between the attempt's preparation and `run_one`: nothing executes."""
    task_run_id = _running_task(session, principal, "bronze.events")
    before = _location(session, principal)
    env = session.scalars(select(Environment).where(Environment.name == "prod")).one()
    write = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=principal.id,
        sql_text="SELECT 1::BIGINT AS id",
        status=QueryRunStatus.RUNNING,
        write_mode=WriteMode.APPEND,
        target_name="bronze.events",
        task_run_id=task_run_id,
    )
    session.add(write)
    session.commit()
    _reclaim(session, task_run_id)
    assert run_one(session, write.id, AllowAllAuthorization()) is None
    session.refresh(write)
    assert (write.status, write.error_message) == (
        QueryRunStatus.FAILED,
        WORKER_LOST_WRITE_MESSAGE,
    )
    assert write.started_at is None
    assert _location(session, principal) == before


def test_run_context_for_a_bound_query_is_the_claim_form() -> None:
    task_run_id, query_id, token_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    bound = BoundTask(task_run_id, 3, "gold.scores", ("silver.a",), ("silver.a",))
    scope = TaskScope(environment="dev", asset_scope=[], token_id=token_id, bound=bound)
    stamped = QueryRun(id=query_id, task_run_id=task_run_id, task_token_id=token_id)
    assert _run_context(stamped, scope) == RunContext(task_run_id=task_run_id, claim=3)
    worker_write = QueryRun(id=query_id, task_run_id=task_run_id)
    assert _run_context(worker_write, None) == RunContext(
        task_run_id=task_run_id, query_run_id=query_id
    )
    assert _run_context(QueryRun(id=query_id), None) is None
    unbound = dataclasses.replace(scope, bound=None)
    assert _run_context(QueryRun(id=query_id, task_token_id=token_id), unbound) is None
    corrupt = QueryRun(id=query_id, task_run_id=task_run_id, task_token_id=token_id)
    assert _run_context(corrupt, unbound) is None
