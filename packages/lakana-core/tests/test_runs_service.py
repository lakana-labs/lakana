"""`RunService`: the API side of runs."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from core_test_helpers import ActionAllowList, _config, _prepared_catalog, python_task_run
from sqlalchemy import ClauseElement, delete, event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from test_projects_dag import make_asset
from test_projects_deploys import _deployed, _repo

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    Securable,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.db import db_now
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    CheckOutcome,
    CheckResult,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    RollbackOutcome,
    Run,
    RunStatus,
    SchedulerLease,
    Table,
    TaskAttempt,
    TaskRun,
    TaskStatus,
    Worker,
)
from lakana_core.projects.run_policy import MAX_RETRY_DELAY_SECONDS, RunCaps
from lakana_core.runs import workers
from lakana_core.runs.checks import ASSET, SOURCE_CHECK
from lakana_core.runs.failures import UNKNOWN_FAILURE, FailureCode, failure_of
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService, task_log_path
from lakana_core.storage.resolver import StorageResolver

_BRONZE = NamespacePath.from_parts(["bronze"])
_ASSETS = (
    make_asset("silver.clean", ("bronze.events",)),
    make_asset("gold.daily", ("silver.clean",)),
    make_asset("gold.weekly", ("silver.clean",)),
)


@pytest.fixture
def version(session: Session, tmp_path: Path, principal: PrincipalRef) -> JobVersion:
    """`dev` with `bronze.events` and `analytics` deployed with three dependent assets.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The admin.

    Returns
    -------
    Project version
        The current version.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[_BRONZE],
        tables=[TableRef("dev", _BRONZE, "events")],
    )
    return _deployed(session, principal, _repo(session), "a" * 40, *_ASSETS)


def _check_entry(table: str, name: str) -> dict[str, str]:
    """Build one manifest check entry.

    Parameters
    ----------
    table
        The checked table.
    name
        The check's short name.

    Returns
    -------
    dict
        The entry as a stored manifest holds it.
    """
    return {
        "name": f"{table}/{name}",
        "table": table,
        "check": name,
        "path": f"checks/{table.replace('.', '/')}/{name}.sql",
        "sql": f"SELECT * FROM {table}",
        "on_failure": "block",
    }


@pytest.fixture
def checked_version(session: Session, tmp_path: Path, principal: PrincipalRef) -> JobVersion:
    """`dev` with `silver.a` (reads `raw.events`) and `gold.b` (reads `silver.a`), two checks.

    The checks are `silver.a/unique` (a built table) and `raw.events/not_empty` (a table the
    project only reads).

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The admin.

    Returns
    -------
    Project version
        The current version.
    """
    _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    deployed = _deployed(
        session,
        principal,
        _repo(session),
        "b" * 40,
        make_asset("silver.a", ("raw.events",)),
        make_asset("gold.b", ("silver.a",)),
    )
    stored = session.get(JobVersion, deployed.id)
    assert stored is not None
    stored.manifest_json = {
        **stored.manifest_json,
        "checks": [_check_entry("silver.a", "unique"), _check_entry("raw.events", "not_empty")],
    }
    session.commit()
    return stored


@pytest.fixture
def kept_version(session: Session, tmp_path: Path, principal: PrincipalRef) -> JobVersion:
    """`dev` with `silver.orders` (a check) and `gold.report` reading it; the table exists.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The admin.

    Returns
    -------
    Project version
        The current version.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[NamespacePath.from_parts(["silver"])],
        tables=[TableRef.from_dotted("dev", "silver.orders")],
    )
    deployed = _deployed(
        session,
        principal,
        _repo(session),
        "c" * 40,
        make_asset("silver.orders", ("bronze.src",)),
        make_asset("gold.report", ("silver.orders",)),
        make_asset("gold.other", ("bronze.src",)),
    )
    stored = session.get(JobVersion, deployed.id)
    assert stored is not None
    stored.manifest_json = {
        **stored.manifest_json,
        "checks": [_check_entry("silver.orders", "unique_id")],
    }
    session.commit()
    return stored


def _keep(session: Session, run_id: uuid.UUID | None = None) -> uuid.UUID:
    """Put the note of a kept table on `silver.orders`.

    Parameters
    ----------
    session
        Database session; commits.
    run_id
        The run the note names; a new id when `None`.

    Returns
    -------
    uuid.UUID
        The run the note names.
    """
    run_id = run_id or uuid.uuid4()
    session.execute(
        update(Table)
        .where(Table.name == "orders")
        .values(
            kept_run_id=run_id, kept_task_run_id=uuid.uuid4(), kept_check="silver.orders/unique_id"
        )
    )
    session.commit()
    return run_id


def _service(session: Session, authz: AuthorizationService | None = None) -> RunService:
    """Build a run service.

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization; allow-all when `None`.

    Returns
    -------
    RunService
        The service, with a 600 s timeout cap.
    """
    return RunService(
        session,
        authz or AllowAllAuthorization(),
        caps=RunCaps(max_retries=5, max_timeout_seconds=600),
    )


def _service_account(session: Session, name: str = "etl", *, active: bool = True) -> Principal:
    """Persist a service account.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.
    active
        Whether it is active.

    Returns
    -------
    Principal
        The row.
    """
    row = Principal(
        id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name=name, is_active=active
    )
    session.add(row)
    session.commit()
    return row


def _protect(session: Session) -> None:
    """Mark `dev` protected.

    Parameters
    ----------
    session
        Database session.
    """
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    env.is_protected = True
    session.commit()


def _bind(session: Session, principal_id: uuid.UUID) -> None:
    """Bind a run principal to the deployment directly.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The principal.
    """
    row = session.scalars(select(EnvironmentDeployment)).one()
    row.run_as_principal_id = principal_id
    session.commit()


def test_full_run_creates_one_pending_task_per_asset_with_its_policy(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Run + task runs in one transaction, RUN_STARTED audited."""
    view = _service(session).start(principal, "dev", "analytics", Selection())
    assert (view.status, view.kind, view.run_as, view.triggered_by) == (
        "queued",
        "full",
        "admin",
        "admin",
    )
    assert [t.asset for t in view.tasks] == ["gold.daily", "gold.weekly", "silver.clean"]
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    assert (task.status, task.max_attempts, task.timeout_seconds, task.retry_delay_seconds) == (
        TaskStatus.PENDING,
        3,
        600,
        30,
    )
    assert task.upstream_json == ["silver.clean"]
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.RUN_STARTED)
    ).one()
    assert event.detail["repository"] == "analytics"
    assert event.detail["tasks"] == 3


def test_a_pre_m5c1_manifest_runs_with_the_defaults(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A manifest without policy keys runs with retries 2, cap timeout."""
    stored = session.get(JobVersion, version.id)
    assert stored is not None
    stored.manifest_json = {
        **stored.manifest_json,
        "assets": [
            {
                k: v
                for k, v in entry.items()
                if k not in {"retries", "retry_delay_seconds", "timeout_seconds"}
            }
            for entry in stored.manifest_json["assets"]
        ],
    }
    session.commit()
    _service(session).start(principal, "dev", "analytics", Selection())
    attempts = set(session.scalars(select(TaskRun.max_attempts)))
    timeouts = set(session.scalars(select(TaskRun.timeout_seconds)))
    assert (attempts, timeouts) == ({3}, {600})


def test_selection_with_upstream_waits_only_for_selected_upstreams(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A selection stores its patterns; upstream_json lists only selected upstreams."""
    view = _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("gold.daily",))
    )
    assert view.kind == "selection"
    assert view.selection == {"patterns": ["gold.daily"], "upstream": False, "downstream": False}
    task = session.scalars(select(TaskRun)).one()
    assert (task.asset, task.upstream_json) == ("gold.daily", [])


def test_unknown_pattern_is_422_and_creates_nothing(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A selection that matches no asset is refused."""
    with pytest.raises(ValidationError, match="matches no asset"):
        _service(session).start(principal, "dev", "analytics", Selection(patterns=("nope.*",)))
    assert session.scalars(select(Run)).all() == []


def test_a_second_start_is_409_with_the_active_run_id(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """At most one active run per deployment; the loser learns the winner's id."""
    first = _service(session).start(principal, "dev", "analytics", Selection())
    with pytest.raises(ConflictError, match=str(first.id)):
        _service(session).start(principal, "dev", "analytics", Selection())
    assert len(session.scalars(select(Run)).all()) == 1


def test_start_needs_run_and_a_deployment(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """403 without RUN (checked first); 404 when the repository is not deployed."""
    with pytest.raises(PermissionDenied):
        _service(session, ActionAllowList(Action.READ_ENVIRONMENT)).start(
            principal, "dev", "analytics", Selection()
        )
    _repo(session, "other")
    with pytest.raises(NotFoundError, match="not deployed"):
        _service(session).start(principal, "dev", "other", Selection())


def test_protected_environment_needs_a_bound_principal(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Protected → the bound principal or 409 with the hint."""
    _protect(session)
    with pytest.raises(ConflictError, match="lakana deployments set-run-as"):
        _service(session).start(principal, "dev", "analytics", Selection())
    etl = _service_account(session)
    _bind(session, etl.id)
    view = _service(session).start(principal, "dev", "analytics", Selection())
    assert (view.run_as, view.triggered_by) == ("etl", "admin")


def test_unprotected_prefers_the_bound_principal(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Unprotected: the bound principal if set, else the caller."""
    etl = _service_account(session)
    _bind(session, etl.id)
    assert _service(session).start(principal, "dev", "analytics", Selection()).run_as == "etl"


def test_a_deactivated_bound_principal_is_409(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Spec: a deactivated or deleted principal → 409."""
    etl = _service_account(session, active=False)
    _bind(session, etl.id)
    with pytest.raises(ConflictError, match="deactivated"):
        _service(session).start(principal, "dev", "analytics", Selection())


def test_an_inactive_caller_cannot_be_the_run_principal(
    session: Session, version: JobVersion
) -> None:
    """In an unprotected environment without a binding the caller must be an active principal."""
    ghost = PrincipalRef(id=uuid.uuid4(), name="ghost", type=PrincipalType.USER)
    with pytest.raises(ConflictError, match="deactivated"):
        _service(session).start(ghost, "dev", "analytics", Selection())


def test_get_and_list_and_their_authorization(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """READ_ENVIRONMENT reads and lists; RUN alone reads but does not list."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    assert _service(session).get(principal, "dev", started.id).id == started.id
    listed = _service(session).list_runs(principal, "dev", repository="analytics")
    assert [r.id for r in listed] == [started.id]
    assert listed[0].tasks == ()
    runner = _service(session, ActionAllowList(Action.RUN))
    assert runner.get(principal, "dev", started.id).id == started.id
    with pytest.raises(PermissionDenied):
        runner.list_runs(principal, "dev")
    with pytest.raises(PermissionDenied):
        _service(session, ActionAllowList()).get(principal, "dev", started.id)
    with pytest.raises(NotFoundError):
        _service(session).get(principal, "dev", uuid.uuid4())
    with pytest.raises(NotFoundError):
        _service(session).list_runs(principal, "dev", repository="ghost")


def test_cancel_requests_and_audits_once(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Cancel sets the flag; a finished run is 409."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    assert _service(session).cancel(principal, "dev", started.id).cancel_requested is True
    assert session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.RUN_CANCEL_REQUESTED)
    ).one().detail == {"run_id": str(started.id), "repository": "analytics"}
    run = session.get(Run, started.id)
    assert run is not None
    run.status = RunStatus.CANCELLED
    session.commit()
    with pytest.raises(ConflictError, match="already cancelled"):
        _service(session).cancel(principal, "dev", started.id)
    with pytest.raises(PermissionDenied):
        _service(session, ActionAllowList(Action.READ_ENVIRONMENT)).cancel(
            principal, "dev", started.id
        )


def _finish(
    session: Session, run_id: uuid.UUID, statuses: dict[str, TaskStatus], run_status: RunStatus
) -> None:
    """Set task and run statuses as a finished run would have them.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.
    statuses
        Asset → final status.
    run_status
        The run's final status.
    """
    for task in session.scalars(select(TaskRun).where(TaskRun.run_id == run_id)):
        task.status = statuses[task.asset]
    run = session.get(Run, run_id)
    assert run is not None
    run.status = run_status
    session.commit()


def test_resume_covers_failed_upstream_failed_and_cancelled_plus_downstream(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A new run of the same project version over what did not succeed."""
    first = _service(session).start(principal, "dev", "analytics", Selection())
    _finish(
        session,
        first.id,
        {
            "silver.clean": TaskStatus.SUCCEEDED,
            "gold.daily": TaskStatus.FAILED,
            "gold.weekly": TaskStatus.SUCCEEDED,
        },
        RunStatus.FAILED,
    )
    resumed = _service(session).resume(principal, "dev", first.id)
    assert (resumed.kind, resumed.resumed_from_run_id, resumed.job_version_id) == (
        "resume",
        first.id,
        version.id,
    )
    assert [t.asset for t in resumed.tasks] == ["gold.daily"]


def _tasks_of(session: Session, run_id: uuid.UUID) -> dict[str, TaskRun]:
    """Read a run's task runs by name.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.

    Returns
    -------
    dict
        Task name to row.
    """
    return {t.asset: t for t in session.scalars(select(TaskRun).where(TaskRun.run_id == run_id))}


def test_a_run_gets_one_source_check_task_per_read_table_with_checks(
    session: Session, checked_version: JobVersion, principal: PrincipalRef
) -> None:
    """Spec 4: a table the project only reads, with checks, becomes a task its readers wait for."""
    view = _service(session).start(principal, "dev", "analytics", Selection())
    tasks = _tasks_of(session, view.id)
    assert sorted(tasks) == ["gold.b", "raw.events", "silver.a"]
    source = tasks["raw.events"]
    assert (source.kind, source.has_checks, source.upstream_json) == ("source_check", True, [])
    assert (source.status, source.attempt, source.max_attempts) == (TaskStatus.PENDING, 0, 3)
    assert (source.timeout_seconds, source.retry_delay_seconds) == (600, 30)
    built = tasks["silver.a"]
    assert (built.kind, built.has_checks, built.upstream_json) == ("asset", True, ["raw.events"])
    last = tasks["gold.b"]
    assert (last.kind, last.has_checks, last.upstream_json) == ("asset", False, ["silver.a"])
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.RUN_STARTED)
    ).one()
    assert event.detail["tasks"] == 3


def test_a_run_reads_the_checks_of_its_manifest_once(
    session: Session,
    checked_version: JobVersion,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest's checks are parsed once for a run, not once per question asked of them."""
    import lakana_core.projects.checks as checks_module
    import lakana_core.runs.service as service_module

    parsed: list[object] = []
    real = checks_module.read_manifest_checks

    def counting(manifest: object) -> object:
        parsed.append(manifest)
        return real(manifest)

    monkeypatch.setattr(checks_module, "read_manifest_checks", counting)
    monkeypatch.setattr(service_module, "read_manifest_checks", counting)
    _service(session).start(principal, "dev", "analytics", Selection())
    assert len(parsed) == 1


def test_an_entry_that_cannot_be_read_gives_its_table_a_task_with_checks(
    session: Session, checked_version: JobVersion, principal: PrincipalRef
) -> None:
    """The task goes through the check phase, which fails closed; none is made unchecked."""
    damaged = _check_entry("gold.b", "broken")
    del damaged["sql"]
    checked_version.manifest_json = {
        **checked_version.manifest_json,
        "checks": [*checked_version.manifest_json["checks"], damaged],
    }
    session.commit()

    view = _service(session).start(principal, "dev", "analytics", Selection())

    tasks = _tasks_of(session, view.id)
    assert {name: t.has_checks for name, t in tasks.items()} == {
        "gold.b": True,
        "raw.events": True,
        "silver.a": True,
    }


def test_an_entry_that_names_no_table_gives_every_asset_task_checks(
    session: Session, checked_version: JobVersion, principal: PrincipalRef
) -> None:
    """It may belong to any table, so every task fails closed; the run still starts."""
    checked_version.manifest_json = {
        **checked_version.manifest_json,
        "checks": [*checked_version.manifest_json["checks"], 7],
    }
    session.commit()

    view = _service(session).start(principal, "dev", "analytics", Selection())

    tasks = _tasks_of(session, view.id)
    assert {name: t.has_checks for name, t in tasks.items()} == {
        "gold.b": True,
        "raw.events": True,
        "silver.a": True,
    }


def test_a_selection_runs_the_source_checks_of_what_it_builds(
    session: Session, checked_version: JobVersion, principal: PrincipalRef
) -> None:
    """Spec 4: the source checks of every table a selected asset reads, and no others."""
    first = _service(session).start(principal, "dev", "analytics", Selection(patterns=("gold.b",)))
    assert sorted(_tasks_of(session, first.id)) == ["gold.b"]
    run = session.get(Run, first.id)
    assert run is not None
    run.status = RunStatus.CANCELLED
    session.commit()
    second = _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("silver.a",))
    )
    tasks = _tasks_of(session, second.id)
    assert sorted(tasks) == ["raw.events", "silver.a"]
    assert tasks["silver.a"].upstream_json == ["raw.events"]


def test_a_run_of_a_project_without_checks_is_unchanged(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """No checks: the tasks are what they were, every one an asset without checks."""
    view = _service(session).start(principal, "dev", "analytics", Selection())
    tasks = _tasks_of(session, view.id)
    assert sorted(tasks) == ["gold.daily", "gold.weekly", "silver.clean"]
    assert {(t.kind, t.has_checks) for t in tasks.values()} == {("asset", False)}
    assert {name: t.upstream_json for name, t in tasks.items()} == {
        "gold.daily": ["silver.clean"],
        "gold.weekly": ["silver.clean"],
        "silver.clean": [],
    }
    assert {(t.max_attempts, t.timeout_seconds, t.retry_delay_seconds) for t in tasks.values()} == {
        (3, 600, 30)
    }


def test_resume_builds_and_checks_again(
    session: Session, checked_version: JobVersion, principal: PrincipalRef
) -> None:
    """Spec 4: a failed source check is a failed task: its readers run again, and it with them."""
    first = _service(session).start(principal, "dev", "analytics", Selection())
    _finish(
        session,
        first.id,
        {
            "raw.events": TaskStatus.FAILED,
            "silver.a": TaskStatus.UPSTREAM_FAILED,
            "gold.b": TaskStatus.UPSTREAM_FAILED,
        },
        RunStatus.FAILED,
    )
    resumed = _service(session).resume(principal, "dev", first.id)
    tasks = _tasks_of(session, resumed.id)
    assert {name: (t.kind, t.upstream_json) for name, t in tasks.items()} == {
        "raw.events": ("source_check", []),
        "silver.a": ("asset", ["raw.events"]),
        "gold.b": ("asset", ["silver.a"]),
    }
    run = session.get(Run, resumed.id)
    assert run is not None
    run.status = RunStatus.CANCELLED
    session.commit()
    second = _service(session).start(principal, "dev", "analytics", Selection())
    _finish(
        session,
        second.id,
        {
            "raw.events": TaskStatus.SUCCEEDED,
            "silver.a": TaskStatus.FAILED,
            "gold.b": TaskStatus.UPSTREAM_FAILED,
        },
        RunStatus.FAILED,
    )
    again = _service(session).resume(principal, "dev", second.id)
    assert sorted(_tasks_of(session, again.id)) == ["gold.b", "raw.events", "silver.a"]


def test_resume_refusals(session: Session, version: JobVersion, principal: PrincipalRef) -> None:
    """Active → 409; nothing failed → 422; redeployed since → 409 'start a fresh run'."""
    first = _service(session).start(principal, "dev", "analytics", Selection())
    with pytest.raises(ConflictError, match="still active"):
        _service(session).resume(principal, "dev", first.id)
    _finish(
        session,
        first.id,
        dict.fromkeys(("silver.clean", "gold.daily", "gold.weekly"), TaskStatus.SUCCEEDED),
        RunStatus.SUCCEEDED,
    )
    with pytest.raises(ValidationError, match="nothing to resume"):
        _service(session).resume(principal, "dev", first.id)
    for task in session.scalars(select(TaskRun).where(TaskRun.run_id == first.id)):
        task.status = TaskStatus.CANCELLED
    session.commit()
    _deployed(session, principal, session.scalars(select(Repository)).one(), "b" * 40, *_ASSETS)
    with pytest.raises(ConflictError, match="start a fresh run"):
        _service(session).resume(principal, "dev", first.id)


def test_task_log_returns_text_and_attempts(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """The free-text log from storage plus the structured attempts, oldest first."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    session.add(
        TaskAttempt(
            id=uuid.uuid4(),
            task_run_id=task.id,
            attempt=1,
            worker="w1",
            started_at=datetime.now(UTC),
            outcome=AttemptOutcome.FAILED,
            error_message="boom",
        )
    )
    session.commit()
    storage = StorageResolver(session).for_environment("dev").storage
    storage.write(task_log_path(started.id, "gold.daily"), b"attempt 1 on w1: failed: boom\n")
    log = _service(session).task_log(principal, "dev", started.id, "gold.daily")
    assert log.text == "attempt 1 on w1: failed: boom\n"
    assert [(a.attempt, a.outcome, a.error_message) for a in log.attempts] == [
        (1, "failed", "boom")
    ]
    empty = _service(session).task_log(principal, "dev", started.id, "gold.weekly")
    assert (empty.text, empty.attempts) == ("", ())
    with pytest.raises(NotFoundError):
        _service(session).task_log(principal, "dev", started.id, "gold.ghost")


def _log_of(session: Session, run_id: uuid.UUID, data: bytes) -> None:
    """Store `data` as the log of ``gold.daily``.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.
    data
        The log's bytes.
    """
    StorageResolver(session).for_environment("dev").storage.write(
        task_log_path(run_id, "gold.daily"), data
    )


def test_task_log_without_an_offset_is_the_whole_log_with_its_end(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """No offset: all of the text, `next_offset` is its byte length, nothing is cut."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _log_of(session, started.id, "ä line\n".encode())
    log = _service(session).task_log(principal, "dev", started.id, "gold.daily")
    assert (log.text, log.next_offset, log.more, log.reset) == ("ä line\n", 8, False, False)
    assert log.complete is False


def test_task_log_follows_appends_and_reads_each_byte_once(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Offset 0 is the whole log; continuing from `next_offset` yields only what was appended."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    service = _service(session)
    first = service.task_log(principal, "dev", started.id, "gold.daily", offset=0)
    assert (first.text, first.next_offset) == ("", 0)
    seen = ""
    offset = 0
    for block in ("one\n", "zwei ä\n", "", "drei\n"):
        _log_of(session, started.id, (seen + block).encode())
        part = service.task_log(principal, "dev", started.id, "gold.daily", offset=offset)
        assert part.text == block
        seen += block
        offset = part.next_offset
        assert offset == len(seen.encode())


def test_task_log_never_splits_a_character(
    session: Session,
    version: JobVersion,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk ends before a half character; an offset inside one starts at its first byte."""
    monkeypatch.setattr("lakana_core.runs.service.LOG_CHUNK_BYTES", 4)
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _log_of(session, started.id, "abcä€xyz".encode())  # a=0 b=1 c=2 ä=3-4 €=5-7 x=8
    service = _service(session)
    first = service.task_log(principal, "dev", started.id, "gold.daily", offset=0)
    assert (first.text, first.next_offset, first.more) == ("abc", 3, True)
    middle = service.task_log(principal, "dev", started.id, "gold.daily", offset=3)
    assert (middle.text, middle.next_offset, middle.more) == ("ä", 5, True)
    second = service.task_log(principal, "dev", started.id, "gold.daily", offset=5)
    assert (second.text, second.next_offset, second.more) == ("€x", 9, True)
    third = service.task_log(principal, "dev", started.id, "gold.daily", offset=9)
    assert (third.text, third.next_offset, third.more) == ("yz", 11, False)
    inside = service.task_log(principal, "dev", started.id, "gold.daily", offset=6)
    assert (inside.text, inside.next_offset) == ("€x", 9)


def test_a_secret_never_shows_in_any_chunk_of_a_log_read_in_pieces(
    session: Session,
    version: JobVersion,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The log is stored redacted, so no chunk boundary can reveal what was cut out of it."""
    secret = "tok-123456"
    stored = "connect with token=<redacted> now\n"
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _log_of(session, started.id, stored.encode())
    for size in range(1, len(stored) + 1):
        monkeypatch.setattr("lakana_core.runs.service.LOG_CHUNK_BYTES", size)
        chunks: list[str] = []
        offset = 0
        more = True
        while more:
            part = _service(session).task_log(
                principal, "dev", started.id, "gold.daily", offset=offset
            )
            chunks.append(part.text)
            offset, more = part.next_offset, part.more
        assert "".join(chunks) == stored
        assert not any(secret in chunk for chunk in chunks)


def test_task_log_beyond_the_end_is_empty_and_says_the_log_shrank(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """An offset past the end is not an error: empty text, the real length, `reset`."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _log_of(session, started.id, b"abc\n")
    log = _service(session).task_log(principal, "dev", started.id, "gold.daily", offset=99)
    assert (log.text, log.next_offset, log.reset) == ("", 4, True)
    at_end = _service(session).task_log(principal, "dev", started.id, "gold.daily", offset=4)
    assert (at_end.text, at_end.next_offset, at_end.reset) == ("", 4, False)


def test_task_log_is_complete_once_the_task_is_finished_and_everything_was_read(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """`complete` needs a terminal task and the end of the log in this response."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _log_of(session, started.id, b"abcdefgh")
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    task.status = TaskStatus.SUCCEEDED
    session.commit()
    service = _service(session)
    whole = service.task_log(principal, "dev", started.id, "gold.daily", offset=0)
    assert (whole.complete, whole.more) == (True, False)
    task.status = TaskStatus.RUNNING
    session.commit()
    running = service.task_log(principal, "dev", started.id, "gold.daily", offset=8)
    assert running.complete is False


def test_task_log_is_not_complete_while_more_is_left(
    session: Session,
    version: JobVersion,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished task whose log is read in several chunks is complete only in the last."""
    monkeypatch.setattr("lakana_core.runs.service.LOG_CHUNK_BYTES", 4)
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _log_of(session, started.id, b"abcdefgh")
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    task.status = TaskStatus.FAILED
    session.commit()
    service = _service(session)
    first = service.task_log(principal, "dev", started.id, "gold.daily", offset=0)
    assert (first.text, first.complete, first.more) == ("abcd", False, True)
    last = service.task_log(principal, "dev", started.id, "gold.daily", offset=4)
    assert (last.text, last.complete, last.more) == ("efgh", True, False)


def test_task_log_with_a_storage_failure_keeps_the_offset(
    session: Session,
    version: JobVersion,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable storage returns no text and does not move the client's offset."""
    started = _service(session).start(principal, "dev", "analytics", Selection())

    class Broken:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def for_environment(self, name: str) -> object:
            raise OSError("storage down")

    monkeypatch.setattr("lakana_core.runs.service.StorageResolver", Broken)
    log = _service(session).task_log(principal, "dev", started.id, "gold.daily", offset=7)
    assert (log.text, log.next_offset, log.reset, log.complete) == ("", 7, False, False)


def test_scheduler_status_counts_and_needs_admin(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Holder, heartbeat and queue lengths; MANAGE_ENVIRONMENT at instance scope."""
    _service(session).start(principal, "dev", "analytics", Selection())
    status = _service(session).scheduler_status(principal)
    assert (status.holder, status.active_runs, status.pending, status.ready, status.running) == (
        None,
        1,
        3,
        0,
        0,
    )
    session.add(SchedulerLease(id=1, holder="w1"))
    session.commit()
    assert _service(session).scheduler_status(principal).holder == "w1"
    with pytest.raises(PermissionDenied):
        _service(session, ActionAllowList(Action.READ_ENVIRONMENT)).scheduler_status(principal)


def test_a_bound_user_principal_is_409(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Only a service account may be the run principal, even if a user row got bound."""
    user = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="bob", is_active=True)
    session.add(user)
    session.commit()
    _bind(session, user.id)
    with pytest.raises(ConflictError, match="not a service account"):
        _service(session).start(principal, "dev", "analytics", Selection())
    assert session.scalars(select(Run)).all() == []


def test_policy_comes_from_the_manifest_clamped_to_the_caps(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Task policy is `policy_from_manifest` under the current caps."""
    stored = session.get(JobVersion, version.id)
    assert stored is not None
    stored.manifest_json = {
        **stored.manifest_json,
        "assets": [
            {**e, "retries": 50, "timeout_seconds": 99999, "retry_delay_seconds": 99999}
            for e in stored.manifest_json["assets"]
        ],
    }
    session.commit()
    _service(session).start(principal, "dev", "analytics", Selection())
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    assert (task.max_attempts, task.timeout_seconds) == (6, 600)
    assert task.retry_delay_seconds == MAX_RETRY_DELAY_SECONDS


def test_selection_upstream_json_only_names_selected_upstreams(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """An upstream outside the selection never appears (else the task never becomes ready)."""
    _service(session).start(principal, "dev", "analytics", Selection(patterns=("gold.*",)))
    ups = {t.asset: t.upstream_json for t in session.scalars(select(TaskRun))}
    assert ups == {"gold.daily": [], "gold.weekly": []}
    run = session.scalars(select(Run)).one()
    run.status = RunStatus.CANCELLED
    session.commit()
    _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("silver.clean", "gold.daily"))
    )
    new = session.scalars(select(Run).where(Run.id != run.id)).one()
    ups = {
        t.asset: t.upstream_json
        for t in session.scalars(select(TaskRun).where(TaskRun.run_id == new.id))
    }
    assert ups == {"silver.clean": [], "gold.daily": ["silver.clean"]}


def test_resume_upstream_json_only_names_resumed_upstreams(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A resume omits the succeeded upstream from `upstream_json`."""
    first = _service(session).start(principal, "dev", "analytics", Selection())
    _finish(
        session,
        first.id,
        {
            "silver.clean": TaskStatus.SUCCEEDED,
            "gold.daily": TaskStatus.FAILED,
            "gold.weekly": TaskStatus.SUCCEEDED,
        },
        RunStatus.FAILED,
    )
    resumed = _service(session).resume(principal, "dev", first.id)
    task = session.scalars(select(TaskRun).where(TaskRun.run_id == resumed.id)).one()
    assert (task.asset, task.upstream_json) == ("gold.daily", [])
    run = session.get(Run, resumed.id)
    assert run is not None
    run.status = RunStatus.CANCELLED
    session.commit()
    second = _service(session).resume(principal, "dev", first.id)
    third_tasks = {
        t.asset: t.upstream_json
        for t in session.scalars(select(TaskRun).where(TaskRun.run_id == second.id))
    }
    assert third_tasks == {"gold.daily": []}


def test_resume_of_a_failed_root_runs_the_downstream_with_its_upstream(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Failed root plus upstream_failed dependants: all three, wired together."""
    first = _service(session).start(principal, "dev", "analytics", Selection())
    _finish(
        session,
        first.id,
        {
            "silver.clean": TaskStatus.FAILED,
            "gold.daily": TaskStatus.UPSTREAM_FAILED,
            "gold.weekly": TaskStatus.UPSTREAM_FAILED,
        },
        RunStatus.FAILED,
    )
    resumed = _service(session).resume(principal, "dev", first.id)
    ups = {
        t.asset: t.upstream_json
        for t in session.scalars(select(TaskRun).where(TaskRun.run_id == resumed.id))
    }
    assert ups == {
        "silver.clean": [],
        "gold.daily": ["silver.clean"],
        "gold.weekly": ["silver.clean"],
    }


def test_resume_rechecks_the_run_principal(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A principal deactivated after the first run blocks the resume."""
    etl = _service_account(session)
    _bind(session, etl.id)
    first = _service(session).start(principal, "dev", "analytics", Selection())
    _finish(
        session,
        first.id,
        dict.fromkeys(("silver.clean", "gold.daily", "gold.weekly"), TaskStatus.FAILED),
        RunStatus.FAILED,
    )
    etl.is_active = False
    session.commit()
    with pytest.raises(ConflictError, match="deactivated"):
        _service(session).resume(principal, "dev", first.id)


def test_start_loses_to_an_undeploy_that_lands_after_the_lookup(
    session: Session, version: JobVersion, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pointer is locked in the Run's transaction: a vanished pointer is a 409, no run."""
    service = _service(session)
    original = service._choose_run_as

    def undeploy_then_choose(
        p: PrincipalRef, env: Environment, repo: Repository, row: EnvironmentDeployment
    ) -> object:
        chosen = original(p, env, repo, row)
        session.execute(delete(EnvironmentDeployment))
        session.commit()
        return chosen

    monkeypatch.setattr(service, "_choose_run_as", undeploy_then_choose)
    with pytest.raises(ConflictError, match="deployment changed"):
        service.start(principal, "dev", "analytics", Selection())
    assert session.scalars(select(Run)).all() == []


def test_an_unrecordable_run_is_a_retryable_409(session: Session, version: JobVersion) -> None:
    """An insert failure with no active run (here: the caller row vanished) is a 409."""
    etl = _service_account(session)
    _bind(session, etl.id)
    ghost = PrincipalRef(id=uuid.uuid4(), name="ghost", type=PrincipalType.USER)
    with pytest.raises(ConflictError, match="could not be recorded"):
        _service(session).start(ghost, "dev", "analytics", Selection())
    assert session.scalars(select(Run)).all() == []


def test_a_deleted_trigger_principal_shows_as_none(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """`triggered_by` is nullable (principal deletion sets it NULL)."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    run = session.get(Run, started.id)
    assert run is not None
    run.triggered_by = None
    run.run_as_principal_id = None
    session.commit()
    view = _service(session).get(principal, "dev", started.id)
    assert (view.triggered_by, view.run_as) == (None, None)


def test_task_log_survives_a_storage_failure(
    session: Session, version: JobVersion, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable storage yields empty text, not an error; attempts still come back."""
    started = _service(session).start(principal, "dev", "analytics", Selection())

    class Broken:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def for_environment(self, name: str) -> object:
            raise OSError("storage down")

    monkeypatch.setattr("lakana_core.runs.service.StorageResolver", Broken)
    log = _service(session).task_log(principal, "dev", started.id, "gold.daily")
    assert (log.text, log.attempts) == ("", ())


def test_a_run_of_another_environment_is_404(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Run ids do not leak across (existing) environments; cancel changes nothing."""
    session.add(
        Environment(
            id=uuid.uuid4(), name="prod", storage_root="file:///dev/null", storage_options={}
        )
    )
    session.commit()
    started = _service(session).start(principal, "dev", "analytics", Selection())
    service = _service(session)
    with pytest.raises(NotFoundError):
        service.get(principal, "prod", started.id)
    with pytest.raises(NotFoundError):
        service.cancel(principal, "prod", started.id)
    with pytest.raises(NotFoundError):
        service.resume(principal, "prod", started.id)
    with pytest.raises(NotFoundError):
        service.task_log(principal, "prod", started.id, "gold.daily")
    run = session.get(Run, started.id)
    assert run is not None
    session.refresh(run)
    assert run.cancel_requested is False


def test_authorization_is_checked_before_the_environment_exists(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A denied caller gets 403 for a nonexistent environment, never a 404 oracle."""
    denied = _service(session, ActionAllowList())
    with pytest.raises(PermissionDenied):
        denied.start(principal, "ghost", "analytics", Selection())
    with pytest.raises(PermissionDenied):
        denied.get(principal, "ghost", uuid.uuid4())
    with pytest.raises(PermissionDenied):
        denied.cancel(principal, "ghost", uuid.uuid4())
    with pytest.raises(PermissionDenied):
        denied.resume(principal, "ghost", uuid.uuid4())
    with pytest.raises(PermissionDenied):
        denied.task_log(principal, "ghost", uuid.uuid4(), "a.b")
    with pytest.raises(PermissionDenied):
        denied.list_runs(principal, "ghost")


def test_a_second_cancel_is_idempotent_and_not_re_audited(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Cancelling an already-cancel-requested active run returns it without a new audit event."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _service(session).cancel(principal, "dev", started.id)
    again = _service(session).cancel(principal, "dev", started.id)
    assert again.cancel_requested is True
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.RUN_CANCEL_REQUESTED)
    ).all()
    assert len(events) == 1


def _after_choice(
    service: RunService, monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]
) -> None:
    """Run `change` right after the run principal is chosen, before the lock step.

    Parameters
    ----------
    service
        The service under test.
    monkeypatch
        Patches the service.
    change
        The concurrent write, committed before `start` continues.
    """
    original = service._choose_run_as

    def choose(
        p: PrincipalRef, env: Environment, repo: Repository, row: EnvironmentDeployment
    ) -> object:
        chosen = original(p, env, repo, row)
        change()
        return chosen

    monkeypatch.setattr(service, "_choose_run_as", choose)


def test_a_rebind_between_choice_and_commit_is_409(
    session: Session, version: JobVersion, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent set-run-as / clear changes the pointer's binding: the run is refused."""
    etl, other = _service_account(session), _service_account(session, "other")
    _bind(session, etl.id)

    def rebind() -> None:
        session.execute(update(EnvironmentDeployment).values(run_as_principal_id=other.id))
        session.commit()

    service = _service(session)
    _after_choice(service, monkeypatch, rebind)
    with pytest.raises(ConflictError, match="changed"):
        service.start(principal, "dev", "analytics", Selection())
    assert session.scalars(select(Run)).all() == []


def test_a_protection_flip_between_choice_and_commit_is_409(
    session: Session, version: JobVersion, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Protecting the environment after an unprotected choice (caller as principal) refuses."""
    service = _service(session)
    _after_choice(service, monkeypatch, lambda: _protect(session))
    with pytest.raises(ConflictError, match="changed"):
        service.start(principal, "dev", "analytics", Selection())
    assert session.scalars(select(Run)).all() == []


def test_a_deactivation_between_choice_and_commit_is_409(
    session: Session, version: JobVersion, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chosen principal is locked as active in the Run's transaction."""
    etl = _service_account(session)
    _bind(session, etl.id)

    def deactivate() -> None:
        session.execute(update(Principal).where(Principal.id == etl.id).values(is_active=False))
        session.commit()

    service = _service(session)
    _after_choice(service, monkeypatch, deactivate)
    with pytest.raises(ConflictError, match="deactivated"):
        service.start(principal, "dev", "analytics", Selection())
    assert session.scalars(select(Run)).all() == []


@pytest.mark.parametrize("asset", ["../../managed/x", "a.b/../c", "nodots", "a..b", ""])
def test_task_log_path_refuses_a_non_identifier_asset(asset: str) -> None:
    """Manifests read back are untrusted: only dotted identifiers become a storage path."""
    with pytest.raises(ValidationError):
        task_log_path(uuid.uuid4(), asset)


def test_task_log_refuses_a_traversing_asset_from_a_tampered_task_run(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A task row carrying `../` is a 422, never a storage read outside the run's folder."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    task.asset = "../../managed/secret"
    session.commit()
    with pytest.raises(ValidationError):
        _service(session).task_log(principal, "dev", started.id, "../../managed/secret")


@pytest.mark.parametrize("error", [ValidationError("bad"), ConflictError("x"), OSError("down")])
def test_task_log_survives_any_storage_resolution_failure(
    session: Session,
    version: JobVersion,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A LakanaError from the resolver (bad root, undecryptable secret) also yields empty text."""
    started = _service(session).start(principal, "dev", "analytics", Selection())

    class Broken:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def for_environment(self, name: str) -> object:
            raise error

    monkeypatch.setattr("lakana_core.runs.service.StorageResolver", Broken)
    log = _service(session).task_log(principal, "dev", started.id, "gold.daily")
    assert log.text == ""


def test_list_runs_without_a_repository_lists_every_repository_newest_first(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """No filter: all of the environment's runs, limited."""
    first = _service(session).start(principal, "dev", "analytics", Selection())
    run = session.get(Run, first.id)
    assert run is not None
    run.status = RunStatus.SUCCEEDED
    session.commit()
    second = _service(session).start(principal, "dev", "analytics", Selection())
    assert [r.id for r in _service(session).list_runs(principal, "dev")] == [second.id, first.id]
    assert [r.id for r in _service(session).list_runs(principal, "dev", limit=1)] == [second.id]


def test_start_locks_environment_and_principal_for_share_and_leaves_updated_at(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """Locking reads (FOR SHARE on Postgres), not writes: no `updated_at` bump per start.

    The unit tier cannot interleave two Postgres transactions (the integration tests race
    this on real Postgres), so the generated SQL is checked instead.
    """
    etl = _service_account(session)
    _bind(session, etl.id)
    before = session.execute(
        select(Principal.updated_at).where(Principal.id == etl.id)
    ).scalar_one()
    seen: list[str] = []

    def capture(
        conn: object,
        clauseelement: ClauseElement,
        multiparams: object,
        params: object,
        execution_options: object,
    ) -> None:
        seen.append(str(clauseelement.compile(dialect=postgresql.dialect())))

    bind = session.get_bind()
    event.listen(bind, "before_execute", capture)
    try:
        _service(session).start(principal, "dev", "analytics", Selection())
    finally:
        event.remove(bind, "before_execute", capture)
    shares = [sql for sql in seen if sql.rstrip().endswith("FOR SHARE")]
    assert any("FROM environments" in sql for sql in shares)
    assert any("FROM principals" in sql for sql in shares)
    assert not any(sql.startswith("UPDATE principals") for sql in seen)
    after = session.execute(select(Principal.updated_at).where(Principal.id == etl.id)).scalar_one()
    assert after == before


def test_a_task_without_a_write_run_shows_the_rows_of_its_succeeded_attempt(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A Python task has no `query_run_id`; its rows are on the attempt that succeeded."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    other = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.weekly")).one()
    for owner, attempt, outcome, rows in (
        (task, 1, AttemptOutcome.FAILED, 99),
        (task, 2, AttemptOutcome.SUCCEEDED, 7),
        (other, 1, AttemptOutcome.FAILED, 5),
    ):
        session.add(
            TaskAttempt(
                id=uuid.uuid4(),
                task_run_id=owner.id,
                attempt=attempt,
                worker="w1",
                started_at=datetime.now(UTC),
                outcome=outcome,
                rows_written=rows,
            )
        )
    session.commit()
    view = _service(session).get(principal, "dev", started.id)
    rows = {t.asset: t.rows_written for t in view.tasks}
    assert rows == {"gold.daily": 7, "gold.weekly": None, "silver.clean": None}


def test_a_task_claimed_again_for_its_checks_never_shows_an_attempt_above_the_limit(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A claim that only runs the checks of a written task is not an attempt.

    Its counter still grows (every claim opens an attempt row), so `lakana runs show` printed
    ``attempt 3/2``. The view caps what it shows at the limit, for the REST API and the CLI alike.
    """
    started = _service(session).start(principal, "dev", "analytics", Selection())
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    task.max_attempts, task.attempt = 2, 3
    session.commit()
    view = _service(session).get(principal, "dev", started.id)
    shown = {t.asset: (t.attempt, t.max_attempts) for t in view.tasks}
    assert shown["gold.daily"] == (2, 2)


# -- wait reasons ----------------------------------------------------


def _running_run(session: Session, run_id: uuid.UUID, **states: TaskStatus) -> None:
    """Make a run `RUNNING` and put its tasks into the given statuses.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.
    **states
        Status per asset, with `.` written as `__` (`silver__clean=TaskStatus.READY`).
    """
    run = session.get(Run, run_id)
    assert run is not None
    run.status = RunStatus.RUNNING
    for key, status in states.items():
        task = session.scalars(
            select(TaskRun).where(TaskRun.run_id == run_id, TaskRun.asset == key.replace("__", "."))
        ).one()
        task.status = status
    session.commit()


def _reasons(
    session: Session, principal: PrincipalRef, run_id: uuid.UUID
) -> dict[str, dict[str, Any] | None]:
    """Read a run and return each task's reason body, keyed by asset.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    run_id
        The run.

    Returns
    -------
    dict
        Asset to the body of its `waiting_for`, or `None`.
    """
    view = _service(session).get(principal, "dev", run_id)
    return {t.asset: None if t.waiting_for is None else t.waiting_for.to_json() for t in view.tasks}


def test_each_waiting_task_of_a_run_carries_its_reason(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(
        session,
        started.id,
        silver__clean=TaskStatus.READY,
        gold__daily=TaskStatus.PENDING,
        gold__weekly=TaskStatus.PENDING,
    )
    weekly = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.weekly")).one()
    weekly.upstream_json = []
    weekly.attempt = 2
    weekly.next_attempt_at = db_now(session) + timedelta(seconds=200)
    session.commit()
    reasons = _reasons(session, principal, started.id)
    assert {a: (r or {}).get("code") for a, r in reasons.items()} == {
        "silver.clean": "no_worker",
        "gold.daily": "upstream",
        "gold.weekly": "retry",
    }
    assert (reasons["gold.daily"] or {})["upstream"] == ["silver.clean"]
    assert (reasons["gold.weekly"] or {})["attempt"] == 2
    _running_run(session, started.id, silver__clean=TaskStatus.SUCCEEDED)
    daily = _reasons(session, principal, started.id)["gold.daily"]
    assert daily is not None and daily["code"] == "no_worker"
    assert _reasons(session, principal, started.id)["silver.clean"] is None


class _ReadsOnly(AllowAllAuthorization):
    """Allows everything but READ_TABLE, which it allows on the listed table names only.

    Parameters
    ----------
    *tables
        The dotted names the reader may read.
    """

    def __init__(self, *tables: str) -> None:
        self.tables = frozenset(tables)

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Refuse READ_TABLE on any table not listed.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            `False` for READ_TABLE on an unlisted table.
        """
        if action is not Action.READ_TABLE or securable.table is None:
            return True
        assert securable.namespace is not None
        return f"{securable.namespace.joined()}.{securable.table}" in self.tables


def test_a_wait_reason_names_only_the_upstream_tables_the_reader_may_read(
    session: Session, checked_version: JobVersion, principal: PrincipalRef
) -> None:
    """An unreadable upstream is counted, never named; missing and unreadable look the same."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(
        session,
        started.id,
        raw__events=TaskStatus.READY,
        silver__a=TaskStatus.PENDING,
        gold__b=TaskStatus.PENDING,
    )

    def reasons(*tables: str) -> dict[str, dict[str, Any]]:
        view = _service(session, _ReadsOnly(*tables)).get(principal, "dev", started.id)
        return {t.asset: t.waiting_for.to_json() for t in view.tasks if t.waiting_for}

    hidden = reasons("gold.b")["gold.b"]
    assert hidden["message"] == "Waiting for 1 upstream table you may not read."
    assert (hidden["upstream"], hidden["hidden_upstream"], hidden["upstream_count"]) == ([], 1, 1)
    assert "silver.a" not in str(hidden)
    shown = reasons("gold.b", "silver.a")["gold.b"]
    assert shown["message"] == "Waiting for upstream tasks: silver.a."
    assert shown["upstream"] == ["silver.a"]
    assert "hidden_upstream" not in shown


def test_wait_reasons_of_source_checks_and_of_a_written_task_that_waits_again(
    session: Session, checked_version: JobVersion, principal: PrincipalRef
) -> None:
    """The existing reasons read true for the new tasks, and no new code exists for them.

    A reader waits for the source check by its table's name (`upstream`); a written task whose
    checks wait for another claim is `ready` (no worker) or, after a retryable failure,
    `retry` with the sentence of a built table whose checks run again.
    """
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(
        session,
        started.id,
        raw__events=TaskStatus.READY,
        silver__a=TaskStatus.PENDING,
        gold__b=TaskStatus.PENDING,
    )
    reasons = _reasons(session, principal, started.id)
    assert {a: (r or {}).get("code") for a, r in reasons.items()} == {
        "raw.events": "no_worker",
        "silver.a": "upstream",
        "gold.b": "upstream",
    }
    assert (reasons["silver.a"] or {})["upstream"] == ["raw.events"]
    assert (reasons["silver.a"] or {})["message"] == "Waiting for upstream tasks: raw.events."
    _running_run(session, started.id, raw__events=TaskStatus.SUCCEEDED)
    built = session.scalars(select(TaskRun).where(TaskRun.asset == "silver.a")).one()
    built.status = TaskStatus.PENDING
    built.attempt = 4
    built.written_table_id = uuid.uuid4()
    built.next_attempt_at = db_now(session) + timedelta(seconds=200)
    session.commit()
    again = _reasons(session, principal, started.id)["silver.a"] or {}
    assert again["code"] == "retry"
    assert str(again["message"]).startswith("The table is built; its checks could not finish")
    assert "attempt" not in again
    built.written_table_id = None  # rolled back: the stamp is gone, the write's evidence stays
    session.commit()
    plain = _reasons(session, principal, started.id)["silver.a"] or {}
    assert str(plain["message"]).startswith("Attempt 3 of 3 failed;")
    run_row = session.get(Run, started.id)
    assert run_row is not None
    write = QueryRun(
        id=uuid.uuid4(),
        environment_id=run_row.environment_id,
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.SUCCEEDED,
        task_run_id=built.id,
        rows_written=2,
    )
    session.add(write)
    session.flush()
    built.query_run_id = write.id
    session.commit()
    rolled = _reasons(session, principal, started.id)["silver.a"] or {}
    assert str(rolled["message"]).startswith("Attempt 3 of 3 failed;")  # set back, not "built"
    built.status = TaskStatus.READY
    built.next_attempt_at = None
    session.commit()
    ready = _reasons(session, principal, started.id)["silver.a"] or {}
    assert ready["code"] == "no_worker"


def test_a_task_on_an_instance_of_interactive_workers_says_no_worker_of_kind(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(session, started.id, silver__clean=TaskStatus.READY)
    now = db_now(session)
    for worker_id in ("i1", "i2"):
        session.add(
            Worker(
                id=worker_id,
                host="h",
                version="1",
                started_at=now,
                heartbeat_at=now,
                takes="interactive",
            )
        )
    session.commit()
    reason = _reasons(session, principal, started.id)["silver.clean"]
    assert reason == {
        "code": "no_worker_of_kind",
        "message": (
            "No worker that takes pipeline work is running (2 workers take interactive work "
            "only), so this task cannot start. Ask the operator to start a worker with "
            "LAKANA_WORKER__TAKES=pipeline or all."
        ),
        "workers": 0,
        "other_workers": 2,
    }
    session.add(
        Worker(id="p", host="h", version="1", started_at=now, heartbeat_at=now, takes="pipeline")
    )
    session.commit()
    busy = _reasons(session, principal, started.id)["silver.clean"]
    assert busy is not None
    assert busy["code"] == "starting"  # a pipeline worker is free


def test_a_task_names_only_the_upstream_tasks_that_have_not_succeeded(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(
        session,
        started.id,
        silver__clean=TaskStatus.SUCCEEDED,
        gold__weekly=TaskStatus.RUNNING,
        gold__daily=TaskStatus.PENDING,
    )
    daily = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    daily.upstream_json = ["silver.clean", "gold.weekly"]
    session.commit()
    reason = _reasons(session, principal, started.id)["gold.daily"]
    assert reason is not None
    assert reason["upstream"] == ["gold.weekly"]


def test_an_upstream_outside_the_run_is_not_waited_for(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(
        session, started.id, silver__clean=TaskStatus.SUCCEEDED, gold__daily=TaskStatus.PENDING
    )
    daily = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    daily.upstream_json = ["silver.clean", "bronze.events", "not.in.this.run"]
    session.commit()
    reason = _reasons(session, principal, started.id)["gold.daily"]
    assert reason is not None
    assert reason["code"] == "no_worker"


def test_a_task_of_a_run_that_is_not_running_shows_no_reason(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    assert set(_reasons(session, principal, started.id).values()) == {None}
    _running_run(session, started.id)
    assert _reasons(session, principal, started.id)["gold.daily"] is not None
    run = session.get(Run, started.id)
    assert run is not None
    run.cancel_requested = True
    session.commit()
    assert set(_reasons(session, principal, started.id).values()) == {None}
    run.cancel_requested = False
    run.status = RunStatus.CANCELLED
    session.commit()
    assert set(_reasons(session, principal, started.id).values()) == {None}


@pytest.mark.parametrize("cancel_requested", [False, True])
def test_a_ready_task_that_is_setting_back_shows_its_reason_also_in_a_cancelled_run(
    session: Session, version: JobVersion, principal: PrincipalRef, cancel_requested: bool
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(session, started.id, silver__clean=TaskStatus.READY, gold__daily=TaskStatus.READY)
    run = session.get(Run, started.id)
    assert run is not None
    run.cancel_requested = cancel_requested
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "silver.clean")).one()
    task.setting_back = True
    session.commit()
    no_worker = _reasons(session, principal, started.id)["silver.clean"]
    assert (no_worker or {}).get("code") == "no_worker"  # starting a worker comes first
    _register(session, "w1", 0)
    session.commit()
    reasons = _reasons(session, principal, started.id)
    assert (reasons["silver.clean"] or {}).get("code") == "setting_back"
    other = reasons["gold.daily"]
    assert (other is None) == cancel_requested


def test_the_task_view_says_whether_the_task_is_setting_back(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(session, started.id, silver__clean=TaskStatus.RUNNING)
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "silver.clean")).one()
    task.setting_back = True
    session.commit()
    view = _service(session).get(principal, "dev", started.id)
    flags = {t.asset: t.setting_back for t in view.tasks}
    assert flags["silver.clean"] is True
    assert flags["gold.daily"] is False


def _statements_of(session: Session, call: Callable[[], object]) -> list[str]:
    """Run `call` and return the SQL statements it issued.

    Parameters
    ----------
    session
        Database session.
    call
        What to run.

    Returns
    -------
    list of str
        The statements.
    """
    seen: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(bind, "before_cursor_execute", record)
    return seen


def test_the_snapshot_is_read_once_per_run_not_per_task(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    now = db_now(session)
    session.add(Worker(id="w", host="h", version="1", started_at=now, heartbeat_at=now))
    _running_run(session, started.id)
    service = _service(session)
    few = _statements_of(session, lambda: service.get(principal, "dev", started.id))
    for n in range(20):
        session.add(
            TaskRun(
                id=uuid.uuid4(),
                run_id=started.id,
                asset=f"gold.extra{n}",
                status=TaskStatus.PENDING,
                max_attempts=1,
                timeout_seconds=60,
                retry_delay_seconds=0,
                upstream_json=["silver.clean"],
            )
        )
    session.commit()
    many = _statements_of(session, lambda: service.get(principal, "dev", started.id))
    assert len(many) == len(few)
    assert len([q for q in many if "FROM workers" in q]) == 1


def test_a_run_without_a_waiting_task_reads_no_snapshot(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(
        session,
        started.id,
        silver__clean=TaskStatus.SUCCEEDED,
        gold__daily=TaskStatus.RUNNING,
        gold__weekly=TaskStatus.FAILED,
    )
    service = _service(session)
    issued = _statements_of(session, lambda: service.get(principal, "dev", started.id))
    assert [q for q in issued if "FROM workers" in q] == []


def test_list_runs_computes_no_reasons(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(session, started.id)
    service = _service(session)
    issued = _statements_of(session, lambda: service.list_runs(principal, "dev"))
    assert [q for q in issued if "FROM workers" in q] == []


def test_the_run_service_judges_alive_by_its_lease(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(session, started.id, silver__clean=TaskStatus.READY)
    beat = db_now(session) - timedelta(seconds=10)
    session.add(Worker(id="w", host="h", version="1", started_at=beat, heartbeat_at=beat))
    session.commit()

    def reason(lease_seconds: int) -> object:
        """Read the reason of `silver.clean` with a lease.

        Parameters
        ----------
        lease_seconds
            The service's lease.

        Returns
        -------
        object
            The code.
        """
        view = RunService(
            session,
            AllowAllAuthorization(),
            caps=RunCaps(max_retries=5, max_timeout_seconds=600),
            lease_seconds=lease_seconds,
        ).get(principal, "dev", started.id)
        task = next(t for t in view.tasks if t.asset == "silver.clean")
        return task.waiting_for.code if task.waiting_for else None

    assert reason(5) == "no_worker"
    assert reason(20) == "starting"


@pytest.mark.parametrize(
    "dead", [TaskStatus.FAILED, TaskStatus.UPSTREAM_FAILED, TaskStatus.CANCELLED]
)
def test_a_task_behind_a_dead_upstream_shows_no_reason(
    session: Session, version: JobVersion, principal: PrincipalRef, dead: TaskStatus
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(session, started.id, silver__clean=dead, gold__daily=TaskStatus.PENDING)
    assert _reasons(session, principal, started.id)["gold.daily"] is None


# -- the worker list -----------------------------------------


def _register(
    session: Session, worker_id: str, age: int, host: str = "host-a", takes: str = "all"
) -> None:
    """Insert a worker whose last heartbeat is `age` seconds old by the database's clock.

    Parameters
    ----------
    session
        Database session.
    worker_id
        Its id.
    age
        Seconds since its heartbeat.
    host
        Its host name.
    takes
        What it takes.
    """
    beat = db_now(session) - timedelta(seconds=age)
    session.add(
        Worker(
            id=worker_id,
            host=host,
            version="1.2.3",
            started_at=beat - timedelta(hours=1),
            heartbeat_at=beat,
            takes=takes,
        )
    )
    session.commit()


def test_list_workers_needs_manage_environment_on_the_instance(
    session: Session, principal: PrincipalRef
) -> None:
    authz = ActionAllowList(Action.MANAGE_ENVIRONMENT)
    assert _service(session, authz).list_workers(principal) == []
    assert authz.seen == [(Action.MANAGE_ENVIRONMENT, Securable.instance_scope())]
    for denied in (Action.READ_ENVIRONMENT, Action.SUBMIT_QUERY):
        with pytest.raises(PermissionDenied):
            _service(session, ActionAllowList(denied)).list_workers(principal)


def test_an_alive_worker_and_a_lost_one(session: Session, principal: PrincipalRef) -> None:
    _register(session, "fresh", 20)
    _register(session, "stale", 61)
    _register(session, "edge", 60)
    views = {v.id: v for v in _service(session).list_workers(principal)}
    assert {k: v.status for k, v in views.items()} == {
        "fresh": "alive",
        "stale": "lost",
        "edge": "lost",
    }
    assert (views["fresh"].host, views["fresh"].version) == ("host-a", "1.2.3")
    assert views["fresh"].heartbeat_at > views["stale"].heartbeat_at


def test_the_list_judges_alive_by_the_services_lease(
    session: Session, principal: PrincipalRef
) -> None:
    _register(session, "w", 61)
    service = RunService(session, AllowAllAuthorization(), lease_seconds=120)
    assert service.list_workers(principal)[0].status == "alive"


def test_what_a_worker_is_doing_is_derived(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    for name in ("idle", "busy", "lost"):
        _register(session, name, 5 if name != "lost" else 100)
    dev = session.scalars(select(Environment)).one()
    query = QueryRun(
        id=uuid.uuid4(),
        environment_id=dev.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.RUNNING,
        worker_id="busy",
    )
    repo = session.scalars(select(Repository)).one()
    deploy = DeployRun(
        id=uuid.uuid4(),
        environment_id=dev.id,
        repository_id=repo.id,
        kind=DeployKind.DEPLOY,
        status=DeployStatus.RUNNING,
        worker_id="busy",
    )
    queued = QueryRun(
        id=uuid.uuid4(),
        environment_id=dev.id,
        sql_text="SELECT 2",
        status=QueryRunStatus.QUEUED,
    )
    done = QueryRun(
        id=uuid.uuid4(),
        environment_id=dev.id,
        sql_text="SELECT 3",
        status=QueryRunStatus.SUCCEEDED,
        worker_id="idle",
    )
    session.add_all([query, deploy, queued, done])
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _running_run(session, started.id, silver__clean=TaskStatus.RUNNING)
    task = session.scalars(select(TaskRun).where(TaskRun.status == TaskStatus.RUNNING)).one()
    task.lease_owner = "busy"
    session.add(
        QueryRun(
            id=uuid.uuid4(),
            environment_id=dev.id,
            sql_text="SELECT 4",
            status=QueryRunStatus.RUNNING,
            worker_id="lost",
        )
    )
    session.commit()
    views = {v.id: v for v in _service(session).list_workers(principal)}
    assert (views["idle"].query_run_id, views["idle"].deploy_run_id, views["idle"].task_run_id) == (
        None,
        None,
        None,
    )
    assert (views["busy"].query_run_id, views["busy"].deploy_run_id, views["busy"].task_run_id) == (
        query.id,
        deploy.id,
        task.id,
    )
    assert views["lost"].query_run_id is not None  # the stuck run, until the sweep ends it


def test_a_worker_with_several_runs_shows_the_oldest(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    _register(session, "w", 5)
    dev = session.scalars(select(Environment)).one()
    now = datetime.now(UTC)
    runs = [
        QueryRun(
            id=uuid.uuid4(),
            environment_id=dev.id,
            sql_text="SELECT 1",
            status=QueryRunStatus.RUNNING,
            worker_id="w",
            started_at=now - timedelta(seconds=n * 10),
        )
        for n in (1, 3, 2)
    ]
    session.add_all(runs)
    session.commit()
    (view,) = _service(session).list_workers(principal)
    assert view.query_run_id == runs[1].id


def test_workers_are_listed_by_id(session: Session, principal: PrincipalRef) -> None:
    for name in ("w3", "w1", "w2"):
        _register(session, name, 5)
    assert [v.id for v in _service(session).list_workers(principal)] == ["w1", "w2", "w3"]


def test_the_worker_list_costs_the_same_statements_for_one_and_for_many_workers(
    session: Session, principal: PrincipalRef
) -> None:
    _register(session, "w0", 5)
    one = _statements_of(session, lambda: _service(session).list_workers(principal))
    for n in range(1, 30):
        _register(session, f"w{n:02}", 5)
    many = _statements_of(session, lambda: _service(session).list_workers(principal))
    assert len(many) == len(one) <= 6


def test_the_worker_list_shows_what_each_worker_takes(
    session: Session, principal: PrincipalRef
) -> None:
    _register(session, "a", 5, takes="interactive")
    _register(session, "b", 5, takes="pipeline")
    _register(session, "c", 5)
    views = {v.id: v.takes for v in _service(session).list_workers(principal)}
    assert views == {"a": "interactive", "b": "pipeline", "c": "all"}


# -- check results ---------------------------------------------------


def _result(
    session: Session,
    run_id: uuid.UUID,
    asset: str,
    check: str,
    *,
    outcome: CheckOutcome = CheckOutcome.PASSED,
    finished_at: datetime | None = None,
    policy_applied: bool = False,
    rollback_outcome: RollbackOutcome | None = None,
) -> CheckResult:
    """Insert one check result for the task of `asset` in a run.

    Parameters
    ----------
    session
        Database session; commits.
    run_id
        The run.
    asset
        The task's asset, which is also the checked table.
    check
        The check's short name.
    outcome
        What happened.
    finished_at
        When it ended; now when `None`.
    policy_applied
        Whether a policy applied.
    rollback_outcome
        What the rollback did, if there was one.

    Returns
    -------
    CheckResult
        The row.
    """
    run = session.get(Run, run_id)
    assert run is not None
    task = session.scalars(
        select(TaskRun).where(TaskRun.run_id == run_id, TaskRun.asset == asset)
    ).one()
    now = finished_at or db_now(session)
    row = CheckResult(
        id=uuid.uuid4(),
        run_id=run_id,
        task_run_id=task.id,
        environment_id=run.environment_id,
        check_name=f"{asset}/{check}",
        check_path=f"checks/{asset.replace('.', '/')}/{check}.sql",
        table_name=asset,
        on_failure="block",
        outcome=outcome,
        failing_rows=0 if outcome is CheckOutcome.PASSED else 3,
        policy_applied=policy_applied,
        rollback_outcome=rollback_outcome,
        started_at=now,
        finished_at=now,
    )
    session.add(row)
    session.commit()
    return row


def test_a_run_carries_its_check_results_per_task(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    clean = session.scalars(select(TaskRun).where(TaskRun.asset == "silver.clean")).one()
    clean.kind = "source_check"
    session.commit()
    _result(session, started.id, "gold.daily", "b_check", outcome=CheckOutcome.FAILED)
    _result(session, started.id, "gold.daily", "a_check")
    _result(session, started.id, "silver.clean", "only")
    view = _service(session).get(principal, "dev", started.id)
    by_asset = {t.asset: t for t in view.tasks}
    assert [c.name for c in by_asset["gold.daily"].checks] == [
        "gold.daily/a_check",
        "gold.daily/b_check",
    ]
    assert [(c.check, c.outcome, c.task) for c in by_asset["gold.daily"].checks] == [
        ("a_check", "passed", "gold.daily"),
        ("b_check", "failed", "gold.daily"),
    ]
    assert by_asset["silver.clean"].kind == "source_check"
    assert [c.name for c in by_asset["silver.clean"].checks] == ["silver.clean/only"]
    assert (by_asset["gold.daily"].kind, by_asset["gold.weekly"].kind) == ("asset", "asset")
    assert by_asset["gold.weekly"].checks == ()


def test_a_listing_carries_no_checks_and_reads_no_check_results(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _result(session, started.id, "gold.daily", "a_check")
    service = _service(session)
    issued = _statements_of(session, lambda: service.list_runs(principal, "dev"))
    assert [q for q in issued if "check_results" in q] == []
    assert service.list_runs(principal, "dev")[0].tasks == ()


def test_the_run_view_costs_one_more_statement_however_many_checks(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    service = _service(session)
    _result(session, started.id, "gold.daily", "c0")
    one = _statements_of(session, lambda: service.get(principal, "dev", started.id))
    for n in range(1, 20):
        _result(session, started.id, "gold.weekly", f"c{n:02}")
    many = _statements_of(session, lambda: service.get(principal, "dev", started.id))
    assert len(many) == len(one)
    assert len([q for q in many if "FROM check_results" in q]) == 1


def test_whoever_may_read_the_run_may_read_its_checks(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _result(session, started.id, "gold.daily", "a_check")
    for action in (Action.READ_ENVIRONMENT, Action.RUN):
        views = _service(session, ActionAllowList(action)).checks(principal, "dev", started.id)
        assert [v.name for v in views] == ["gold.daily/a_check"]
    with pytest.raises(PermissionDenied) as denied:
        _service(session, ActionAllowList()).checks(principal, "dev", started.id)
    with pytest.raises(PermissionDenied) as same:
        _service(session, ActionAllowList()).get(principal, "dev", started.id)
    assert str(denied.value) == str(same.value)
    with pytest.raises(NotFoundError) as missing:
        _service(session).checks(principal, "dev", uuid.uuid4())
    with pytest.raises(NotFoundError) as same_missing:
        _service(session).get(principal, "dev", uuid.uuid4())
    assert type(missing.value) is type(same_missing.value)
    with pytest.raises(PermissionDenied):
        _service(session, ActionAllowList()).list_checks(principal, "dev")
    with pytest.raises(PermissionDenied):
        _service(session, ActionAllowList(Action.RUN)).list_checks(principal, "dev")


def test_list_checks_pages_newest_first_and_filters_by_table(
    session: Session, version: JobVersion, principal: PrincipalRef, tmp_path: Path
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    start = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    tables = ["gold.daily", "gold.weekly", "gold.daily", "gold.weekly", "gold.daily"]
    for n, table in enumerate(tables):
        _result(session, started.id, table, f"c{n}", finished_at=start + timedelta(minutes=n))
    CatalogService(session, StorageResolver(session), AllowAllAuthorization()).create_environment(
        principal, name="prod", storage=_config(tmp_path / "prod")
    )
    other = python_task_run(session, principal.id, environment="prod", asset="gold.daily")
    _result(session, other.run_id, "gold.daily", "elsewhere", finished_at=start + timedelta(days=1))

    service = _service(session)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page, cursor, _ = service.list_checks(principal, "dev", limit=2, cursor=cursor)
        pages += 1
        seen += [v.check for v in page]
        if cursor is None:
            break
    assert (pages, seen) == (3, ["c4", "c3", "c2", "c1", "c0"])

    only, _, _ = service.list_checks(principal, "dev", table="gold.daily")
    assert [v.check for v in only] == ["c4", "c2", "c0"]
    nothing, nxt, _ = service.list_checks(principal, "dev", table="gold.nothing")
    assert (nothing, nxt) == ([], None)
    with pytest.raises(ValidationError, match="cursor is not valid"):
        service.list_checks(principal, "dev", cursor="!!not-a-cursor")


def test_list_checks_breaks_a_tie_on_the_time_by_id(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    at = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    for n in range(4):
        _result(session, started.id, "gold.daily", f"c{n}", finished_at=at)
    service = _service(session)
    first, cursor, _ = service.list_checks(principal, "dev", limit=2)
    second, last, _ = service.list_checks(principal, "dev", limit=2, cursor=cursor)
    assert last is None
    assert len({v.name for v in first + second}) == 4


def test_a_result_with_a_policy_carries_the_note(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _result(session, started.id, "gold.daily", "filtered", policy_applied=True)
    _result(session, started.id, "gold.daily", "plain")
    views = {v.check: v for v in _service(session).checks(principal, "dev", started.id)}
    assert views["filtered"].policy_note == (
        "checked under a row filter or column mask of admin: "
        "rows and values it cannot see were not checked"
    )
    assert views["plain"].policy_note is None
    listed, _, _ = _service(session).list_checks(principal, "dev")
    assert {v.check: v.policy_note is not None for v in listed} == {
        "filtered": True,
        "plain": False,
    }


def test_a_result_carries_what_the_rollback_did(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _result(
        session,
        started.id,
        "gold.daily",
        "a",
        outcome=CheckOutcome.FAILED,
        rollback_outcome=RollbackOutcome.CHANGED_SINCE,
    )
    (view,) = _service(session).checks(principal, "dev", started.id)
    assert view.rollback_outcome == "changed_since"


@pytest.mark.parametrize(
    "cursor",
    [
        "!!not-base64",
        base64.urlsafe_b64encode(b'"a string"').decode(),
        base64.urlsafe_b64encode(b'{"k": "checks", "o": 1, "i": "x"}').decode(),
        base64.urlsafe_b64encode(
            b'{"k": "checks", "o": "2026-10-11T12:00:00", "i": "' + b"0" * 32 + b'"}'
        ).decode(),
        base64.urlsafe_b64encode(b'{"k": "checks", "o": "not a time", "i": "x"}').decode(),
        base64.urlsafe_b64encode(
            b'{"k": "checks", "o": "0001-01-01T00:00:00+23:00", "i": "' + b"0" * 32 + b'"}'
        ).decode(),
    ],
)
def test_a_cursor_that_was_not_issued_is_refused(
    session: Session, version: JobVersion, principal: PrincipalRef, cursor: str
) -> None:
    with pytest.raises(ValidationError, match="Start the listing again"):
        _service(session).list_checks(principal, "dev", cursor=cursor)


@pytest.mark.parametrize("limit", [0, -1])
def test_list_checks_refuses_a_limit_below_one(
    session: Session, version: JobVersion, principal: PrincipalRef, limit: int
) -> None:
    with pytest.raises(ValidationError, match="at least 1"):
        _service(session).list_checks(principal, "dev", limit=limit)


def test_a_later_run_checks_a_kept_table_before_its_readers(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    _keep(session)
    view = _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("gold.report",))
    )
    tasks = _tasks_of(session, view.id)
    assert sorted(tasks) == ["gold.report", "silver.orders"]
    check = tasks["silver.orders"]
    assert (check.kind, check.has_checks, check.upstream_json) == (SOURCE_CHECK, True, [])
    assert (check.status, check.max_attempts) == (TaskStatus.PENDING, 3)
    assert tasks["gold.report"].upstream_json == ["silver.orders"]
    assert tasks["gold.report"].kind == ASSET
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.RUN_STARTED)
    ).one()
    assert event.detail["tasks"] == 2


def test_a_kept_table_without_a_check_entry_still_gets_its_task(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    kept_version.manifest_json = {**kept_version.manifest_json, "checks": []}
    session.commit()
    _keep(session)
    view = _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("gold.report",))
    )
    check = _tasks_of(session, view.id)["silver.orders"]
    assert (check.kind, check.has_checks) == (SOURCE_CHECK, True)


def test_a_run_that_rebuilds_the_kept_table_gets_no_extra_task(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    _keep(session)
    view = _service(session).start(principal, "dev", "analytics", Selection())
    tasks = _tasks_of(session, view.id)
    assert sorted(tasks) == ["gold.other", "gold.report", "silver.orders"]
    assert {name: t.kind for name, t in tasks.items()} == dict.fromkeys(tasks, ASSET)
    assert tasks["gold.report"].upstream_json == ["silver.orders"]


def test_a_kept_table_that_no_selected_asset_reads_gets_no_task(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    _keep(session)
    view = _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("gold.other",))
    )
    assert sorted(_tasks_of(session, view.id)) == ["gold.other"]


def test_a_run_without_kept_tables_is_created_as_before(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    seen: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        view = _service(session).start(
            principal, "dev", "analytics", Selection(patterns=("gold.report",))
        )
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert sorted(_tasks_of(session, view.id)) == ["gold.report"]
    on_tables = [s for s in seen if "FROM tables" in s or "JOIN tables" in s]
    assert len(on_tables) == 1  # the one query for the notes, and no other read of `tables`


def test_resume_of_a_failed_check_task_of_a_kept_table_builds_the_table(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    """`resume` rebuilds the kept table, the first way out the message names."""
    _keep(session)
    first = _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("gold.report",))
    )
    _finish(
        session,
        first.id,
        {"silver.orders": TaskStatus.FAILED, "gold.report": TaskStatus.UPSTREAM_FAILED},
        RunStatus.FAILED,
    )
    resumed = _service(session).resume(principal, "dev", first.id)
    tasks = _tasks_of(session, resumed.id)
    assert {name: (t.kind, t.upstream_json) for name, t in tasks.items()} == {
        "silver.orders": (ASSET, []),
        "gold.report": (ASSET, ["silver.orders"]),
    }


def test_the_view_tells_the_check_task_of_a_kept_table_from_that_of_a_source(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    """`kept_check` is true for a check task whose table the project builds, never for a source."""
    manifest = kept_version.manifest_json
    kept_version.manifest_json = {
        **manifest,
        "assets": [
            {**entry, "references": [*entry.get("references", []), "raw.events"]}
            if entry["table"] == "gold.other"
            else entry
            for entry in manifest["assets"]
        ],
        "checks": [*manifest["checks"], _check_entry("raw.events", "not_empty")],
    }
    session.commit()
    _keep(session)

    view = _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("gold.report", "gold.other"))
    )

    assert {t.asset: (t.kind, t.kept_check) for t in view.tasks} == {
        "gold.other": (ASSET, False),
        "gold.report": (ASSET, False),
        "raw.events": (SOURCE_CHECK, False),
        "silver.orders": (SOURCE_CHECK, True),
    }
    shown = _service(session).get(principal, "dev", view.id)
    assert {t.asset: t.kept_check for t in shown.tasks}["silver.orders"] is True


def test_the_kind_of_a_check_task_is_unknown_when_the_job_version_is_gone(
    session: Session,
    kept_version: JobVersion,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the manifest nobody can tell a kept table from a source, so `kept_check` is null."""
    _keep(session)
    view = _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("gold.report",))
    )
    real_get = session.get

    def get(entity: type, ident: object, *args: object, **kwargs: object) -> object:
        return None if entity is JobVersion else real_get(entity, ident, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(session, "get", get)

    shown = _service(session).get(principal, "dev", view.id)

    assert {t.asset: t.kept_check for t in shown.tasks} == {
        "gold.report": False,
        "silver.orders": None,
    }


def test_a_reader_that_reaches_the_kept_table_through_an_unselected_view_waits_for_its_check(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    manifest = kept_version.manifest_json
    kept_version.manifest_json = {
        **manifest,
        "assets": [
            {**entry, "references": ["silver.v"]} if entry["table"] == "gold.report" else entry
            for entry in manifest["assets"]
        ]
        + [{"table": "silver.v", "references": ["silver.orders"], "materialize": "view"}],
    }
    session.commit()
    _keep(session)

    view = _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("gold.report",))
    )

    tasks = _tasks_of(session, view.id)
    assert sorted(tasks) == ["gold.report", "silver.orders"]
    assert tasks["silver.orders"].kind == SOURCE_CHECK
    assert tasks["gold.report"].upstream_json == ["silver.orders"]


def test_a_selected_asset_read_through_an_unselected_view_is_ordered_before_its_reader(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    """`--select silver.orders gold.report`, `gold.report` reads the unselected view over it.

    Without a note on any table: the reader must not start while the table is rebuilt.
    """
    manifest = kept_version.manifest_json
    kept_version.manifest_json = {
        **manifest,
        "assets": [
            {**entry, "references": ["silver.v"]} if entry["table"] == "gold.report" else entry
            for entry in manifest["assets"]
        ]
        + [{"table": "silver.v", "references": ["silver.orders"], "materialize": "view"}],
        "edges": [["silver.orders", "silver.v"], ["silver.v", "gold.report"]],
    }
    session.commit()

    view = _service(session).start(
        principal, "dev", "analytics", Selection(patterns=("silver.orders", "gold.report"))
    )

    tasks = _tasks_of(session, view.id)
    assert sorted(tasks) == ["gold.report", "silver.orders"]
    assert tasks["gold.report"].upstream_json == ["silver.orders"]
    assert tasks["silver.orders"].upstream_json == []


def test_a_rebuilt_kept_table_is_an_upstream_of_what_reads_it_through_an_unselected_view(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    """`--select silver.orders gold.report`, and `gold.report` reads the unselected view.

    The table is selected, so it gets no check task; without the extra upstream the reader
    could start while the table is being rebuilt and be refused it.
    """
    manifest = kept_version.manifest_json
    kept_version.manifest_json = {
        **manifest,
        "assets": [
            {**entry, "references": ["silver.v"]} if entry["table"] == "gold.report" else entry
            for entry in manifest["assets"]
        ]
        + [{"table": "silver.v", "references": ["silver.orders"], "materialize": "view"}],
        "edges": [["silver.orders", "silver.v"], ["silver.v", "gold.report"]],
    }
    session.commit()
    selection = Selection(patterns=("silver.orders", "gold.report"))

    before = _service(session).start(principal, "dev", "analytics", selection)
    unordered = _tasks_of(session, before.id)
    assert unordered["gold.report"].upstream_json == ["silver.orders"]
    _finish(session, before.id, dict.fromkeys(unordered, TaskStatus.SUCCEEDED), RunStatus.SUCCEEDED)
    _keep(session)
    view = _service(session).start(principal, "dev", "analytics", selection)

    tasks = _tasks_of(session, view.id)
    assert sorted(tasks) == ["gold.report", "silver.orders"]
    assert {name: task.kind for name, task in tasks.items()} == dict.fromkeys(tasks, ASSET)
    assert tasks["gold.report"].upstream_json == ["silver.orders"]
    assert tasks["silver.orders"].upstream_json == []


def test_a_direct_reader_of_a_rebuilt_kept_table_lists_it_once(
    session: Session, kept_version: JobVersion, principal: PrincipalRef
) -> None:
    _keep(session)

    view = _service(session).start(principal, "dev", "analytics", Selection())

    assert _tasks_of(session, view.id)["gold.report"].upstream_json == ["silver.orders"]


# -- failure codes ------------------------------------------------------------


def _fail_task(
    session: Session,
    run_id: uuid.UUID,
    asset: str,
    status: TaskStatus,
    *,
    code: str | None = None,
    message: str | None = None,
) -> None:
    """Put a task of a run into a status, with the text and code of its failure.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.
    asset
        The task's asset.
    status
        Its new status.
    code
        The stored failure code, if any.
    message
        The stored error text, if any.
    """
    task = session.scalars(
        select(TaskRun).where(TaskRun.run_id == run_id, TaskRun.asset == asset)
    ).one()
    task.status = status
    task.failure_code = code
    task.error_message = message
    session.commit()


def test_a_failed_task_shows_its_code_with_the_cause_and_the_next_step(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """The view carries the failure; the free text stays as the error message."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _fail_task(
        session, started.id, "gold.daily", TaskStatus.FAILED, code="timeout", message="too slow"
    )

    view = _service(session).get(principal, "dev", started.id)
    tasks = {t.asset: t for t in view.tasks}

    failed = tasks["gold.daily"]
    assert failed.error_message == "too slow"
    assert failed.failure == failure_of(FailureCode.TIMEOUT)
    assert tasks["silver.clean"].failure is None


def test_an_old_failed_task_without_a_code_is_unknown(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A row from before the codes shows `unknown`, with a generic cause and next step."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _fail_task(session, started.id, "gold.daily", TaskStatus.FAILED, message="old text")

    view = _service(session).get(principal, "dev", started.id)

    assert {t.asset: t.failure for t in view.tasks}["gold.daily"] == UNKNOWN_FAILURE


def test_a_succeeded_task_with_a_note_has_no_failure(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A warning note on a success is not an error."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _fail_task(session, started.id, "gold.daily", TaskStatus.SUCCEEDED, message="a check warned")

    view = _service(session).get(principal, "dev", started.id)

    assert {t.asset: t.failure for t in view.tasks}["gold.daily"] is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TaskStatus.UPSTREAM_FAILED, FailureCode.UPSTREAM_FAILED),
        (TaskStatus.CANCELLED, FailureCode.CANCELLED),
    ],
)
def test_an_old_task_that_never_ran_is_named_by_its_status(
    session: Session,
    version: JobVersion,
    principal: PrincipalRef,
    status: TaskStatus,
    expected: FailureCode,
) -> None:
    """The status says it exactly, so no guess is needed for a row without a code."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _fail_task(session, started.id, "gold.daily", status)

    view = _service(session).get(principal, "dev", started.id)

    assert {t.asset: t.failure for t in view.tasks}["gold.daily"] == failure_of(expected)


def test_a_run_counts_its_failed_tasks_in_the_view_and_in_the_list(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """`failed_tasks` counts tasks that failed themselves, not those that never ran."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    _fail_task(session, started.id, "silver.clean", TaskStatus.FAILED, code="memory")
    _fail_task(session, started.id, "gold.daily", TaskStatus.UPSTREAM_FAILED)

    assert _service(session).get(principal, "dev", started.id).failed_tasks == 1
    listed = _service(session).list_runs(principal, "dev")
    assert [run.failed_tasks for run in listed] == [1]


def test_task_log_attempts_carry_their_failure(
    session: Session, version: JobVersion, principal: PrincipalRef
) -> None:
    """A failed attempt shows its code, an old one `unknown`, a good one nothing."""
    started = _service(session).start(principal, "dev", "analytics", Selection())
    task = session.scalars(select(TaskRun).where(TaskRun.asset == "gold.daily")).one()
    for number, outcome, code in (
        (1, AttemptOutcome.TIMED_OUT, "timeout"),
        (2, AttemptOutcome.FAILED, None),
        (3, AttemptOutcome.SUCCEEDED, None),
    ):
        session.add(
            TaskAttempt(
                id=uuid.uuid4(),
                task_run_id=task.id,
                attempt=number,
                worker="w1",
                started_at=datetime.now(UTC),
                outcome=outcome,
                failure_code=code,
            )
        )
    session.commit()

    log = _service(session).task_log(principal, "dev", started.id, "gold.daily")

    assert [a.failure for a in log.attempts] == [
        failure_of(FailureCode.TIMEOUT),
        UNKNOWN_FAILURE,
        None,
    ]


def test_the_list_shows_the_cache_sizes_a_worker_stored(
    session: Session, principal: PrincipalRef
) -> None:
    _register(session, "measured", 5)
    _register(session, "new", 5)
    sizes = workers.CacheSizes(environments_bytes=5, packages_bytes=6, tasks_bytes=7, complete=True)
    workers.beat(session, "measured", caches=sizes)
    session.commit()
    views = {v.id: v for v in _service(session).list_workers(principal)}
    assert views["measured"].caches == sizes
    assert views["measured"].caches_measured_at is not None
    assert (views["new"].caches, views["new"].caches_measured_at) == (None, None)
