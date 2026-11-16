"""Model additions: runs, task runs, attempts, the scheduler lease, actions, events."""

from __future__ import annotations

import dataclasses
import uuid
import warnings
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError, SAWarning
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, RunContext
from lakana_core.grants.authorization import TASK_TOKEN_ACTIONS
from lakana_core.grants.roles import ROLE_ACTIONS, Role
from lakana_core.models import (
    AttemptOutcome,
    AuditEventType,
    Base,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    PrincipalType,
    QueryOrigin,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    SchedulerLease,
    TaskAttempt,
    TaskRun,
    TaskStatus,
)
from lakana_core.models.run import (
    ACTIVE_RUN_STATUSES,
    SCHEDULER_LEASE_ID,
    TERMINAL_TASK_STATUSES,
)


def _seed(session: Session) -> tuple[Environment, Repository, Principal, JobVersion]:
    """Insert an environment, a principal, a repository and a project version.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple
        The inserted rows.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name="etl")
    environment = Environment(id=uuid.uuid4(), name="prod", storage_root="file:///tmp/prod")
    repository = Repository(
        id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a.git"
    )
    session.add_all([principal, environment, repository])
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repository.id,
        environment_id=environment.id,
        commit_sha="a" * 40,
        manifest_json={"version": 1, "assets": [], "edges": []},
        code_uri="file:///tmp/prod/_artifacts/code/a.tar.gz",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.commit()
    return environment, repository, principal, version


def _run(
    environment: Environment, repository: Repository, version: JobVersion, **kw: object
) -> Run:
    """Build an unsaved run.

    Parameters
    ----------
    environment
        Its environment.
    repository
        Its repository.
    version
        Its project version.
    **kw
        Column overrides.

    Returns
    -------
    Run
        The row.
    """
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "environment_id": environment.id,
        "repository_id": repository.id,
        "job_version_id": version.id,
        "kind": RunKind.FULL,
        "status": RunStatus.QUEUED,
        "selection_json": {"patterns": [], "upstream": False, "downstream": False},
    }
    values.update(kw)
    return Run(**values)


def test_run_task_and_attempt_round_trip(session: Session) -> None:
    """A run with one task and one attempt persists with the documented defaults."""
    environment, repository, principal, version = _seed(session)
    run = _run(environment, repository, version, run_as_principal_id=principal.id)
    session.add(run)
    session.flush()
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=run.id,
        asset="gold.daily",
        status=TaskStatus.PENDING,
        max_attempts=3,
        timeout_seconds=900,
        retry_delay_seconds=30,
        upstream_json=["silver.clean"],
        ready_at=datetime.now(UTC),
    )
    session.add(task)
    session.flush()
    session.add(
        TaskAttempt(
            id=uuid.uuid4(),
            task_run_id=task.id,
            attempt=1,
            worker="w1",
            started_at=datetime.now(UTC),
            outcome=AttemptOutcome.LOST,
        )
    )
    session.commit()
    session.expire_all()
    loaded = session.get(TaskRun, task.id)
    assert loaded is not None
    assert loaded.ready_at is not None
    assert loaded.ready_at.tzinfo is not None
    assert (loaded.attempt, loaded.conflict_retried, loaded.upstream_json) == (
        0,
        False,
        ["silver.clean"],
    )
    stored = session.get(Run, run.id)
    assert stored is not None
    assert stored.cancel_requested is False
    assert stored.created_at.tzinfo is not None


def test_task_asset_is_unique_per_run(session: Session) -> None:
    """`(run_id, asset)` is unique."""
    environment, repository, _, version = _seed(session)
    run = _run(environment, repository, version)
    session.add(run)
    session.flush()
    for _ in range(2):
        session.add(
            TaskRun(
                id=uuid.uuid4(),
                run_id=run.id,
                asset="gold.daily",
                status=TaskStatus.PENDING,
                max_attempts=1,
                timeout_seconds=1,
                retry_delay_seconds=0,
                upstream_json=[],
            )
        )
    with pytest.raises(IntegrityError):
        session.commit()


def test_one_active_run_per_deployment(session: Session) -> None:
    """The partial unique index admits one queued/running run, any number of finished ones."""
    environment, repository, _, version = _seed(session)
    session.add(_run(environment, repository, version, status=RunStatus.SUCCEEDED))
    session.add(_run(environment, repository, version, status=RunStatus.FAILED))
    session.add(_run(environment, repository, version, status=RunStatus.QUEUED))
    session.commit()
    session.add(_run(environment, repository, version, status=RunStatus.RUNNING))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_the_environment_cascades_to_runs_and_tasks(session: Session) -> None:
    """Runs, task runs and attempts go with their environment."""
    environment, repository, _, version = _seed(session)
    run = _run(environment, repository, version)
    session.add(run)
    session.flush()
    session.add(
        TaskRun(
            id=uuid.uuid4(),
            run_id=run.id,
            asset="a.b",
            status=TaskStatus.PENDING,
            max_attempts=1,
            timeout_seconds=1,
            retry_delay_seconds=0,
            upstream_json=[],
        )
    )
    session.commit()
    session.delete(environment)
    session.commit()
    assert session.query(Run).count() == 0
    assert session.query(TaskRun).count() == 0


def test_deployment_binding_and_query_run_context_columns(session: Session) -> None:
    """`run_as_principal_id`, `task_run_id` and `break_glass_reason` are nullable columns."""
    environment, repository, principal, version = _seed(session)
    session.add(
        EnvironmentDeployment(
            environment_id=environment.id,
            repository_id=repository.id,
            current_job_version_id=version.id,
            run_as_principal_id=principal.id,
        )
    )
    run = QueryRun(
        origin=QueryOrigin.WRITTEN,
        id=uuid.uuid4(),
        environment_id=environment.id,
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.QUEUED,
        break_glass_reason="hotfix for incident 42",
    )
    session.add(run)
    session.commit()
    assert run.task_run_id is None
    assert run.break_glass_reason == "hotfix for incident 42"


def test_scheduler_lease_row(session: Session) -> None:
    """The lease is a one-row table keyed by `SCHEDULER_LEASE_ID`."""
    session.add(SchedulerLease(id=SCHEDULER_LEASE_ID))
    session.commit()
    lease = session.get(SchedulerLease, SCHEDULER_LEASE_ID)
    assert lease is not None
    assert (lease.holder, lease.heartbeat_at) == (None, None)


def test_status_sets() -> None:
    """The active and terminal sets name the states the scheduler and the API rely on."""
    assert frozenset({RunStatus.QUEUED, RunStatus.RUNNING}) == ACTIVE_RUN_STATUSES
    assert (
        frozenset(
            {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.UPSTREAM_FAILED,
                TaskStatus.CANCELLED,
            }
        )
        == TERMINAL_TASK_STATUSES
    )


def test_run_action_goes_only_to_admin_and_never_to_task_tokens() -> None:
    """`RUN` is an administrator action, and a task token never carries it."""
    assert Action.RUN not in TASK_TOKEN_ACTIONS
    assert Action.RUN in ROLE_ACTIONS[Role.ADMIN]
    for role in (Role.VIEWER, Role.ANALYST, Role.DATA_SCIENTIST, Role.DATA_ENGINEER):
        assert Action.RUN not in ROLE_ACTIONS[role]


def test_principal_ref_carries_no_run_context_by_default() -> None:
    """A principal from authentication has neither a run context nor break-glass."""
    ref = PrincipalRef(id=uuid.uuid4(), name="u", type=PrincipalType.USER)
    assert (ref.run_context, ref.break_glass_reason) == (None, None)
    task = uuid.uuid4()
    narrowed = dataclasses.replace(ref, run_context=RunContext(task_run_id=task, query_run_id=task))
    assert narrowed.run_context == RunContext(task_run_id=task, query_run_id=task)


def test_new_audit_event_types_exist() -> None:
    """The nine event types of the run engine exist."""
    assert {
        AuditEventType.RUN_STARTED,
        AuditEventType.RUN_FINISHED,
        AuditEventType.RUN_CANCEL_REQUESTED,
        AuditEventType.CODE_INTEGRITY_FAILED,
        AuditEventType.PROTECTED_WRITE_DENIED,
        AuditEventType.BREAK_GLASS_WRITE,
        AuditEventType.RUN_AS_BOUND,
        AuditEventType.RUN_AS_CLEARED,
        AuditEventType.UNDEPLOYED,
    } <= set(AuditEventType)


def test_sorting_the_tables_raises_no_cycle_warning() -> None:
    """`query_runs.task_run_id` is `use_alter`, so the query_runs/task_runs cycle sorts cleanly."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        names = [table.name for table in Base.metadata.sorted_tables]
    assert {"query_runs", "task_runs"} <= set(names)
