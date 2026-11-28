"""`JobService`: start, pause, resume and list jobs."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import ActionAllowList, _prepared_catalog
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from test_projects_deploys import _deployed_jobs, _job, _repo

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
    SecurableKind,
)
from lakana_core.db import db_now
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.jobs.cron import next_fire_after
from lakana_core.jobs.service import JobService, JobView, sorted_jobs
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    EnvironmentDeployment,
    Grant,
    JobSchedule,
    JobVersion,
    NotificationDelivery,
    NotificationState,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Run,
    RunStatus,
    RunTrigger,
)
from lakana_core.projects.run_policy import RunCaps
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService

_BRONZE = NamespacePath.from_parts(["bronze"])


@pytest.fixture
def deployed(session: Session, tmp_path: Path, principal: PrincipalRef) -> Repository:
    """Deploy `analytics` to `dev` with a scheduled job `daily` and a manual one `manual`.

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
    Repository
        The repository.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[_BRONZE],
        tables=[TableRef("dev", _BRONZE, "events")],
    )
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, {"daily": _job(), "manual": _job(cron=None)})
    _grant_admin(session, principal.id)
    return repo


def _service(session: Session, authz: object | None = None) -> JobService:
    """Build a job service.

    Parameters
    ----------
    session
        Database session.
    authz
        Authorization; allow-all when `None`.

    Returns
    -------
    JobService
        The service.
    """
    return JobService(
        session,
        authz or AllowAllAuthorization(),  # ty: ignore[invalid-argument-type]
        caps=RunCaps(max_retries=5, max_timeout_seconds=600),
    )


def _grant_admin(session: Session, principal_id: uuid.UUID) -> None:
    """Give a principal the ADMIN role on the instance, so that it may `RUN` everywhere.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The principal.
    """
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=principal_id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=principal_id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()


def _person(session: Session, name: str, *, admin: bool = False) -> PrincipalRef:
    """Persist a user, with the ADMIN role or without any grant.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.
    admin
        Give it the ADMIN role.

    Returns
    -------
    PrincipalRef
        A reference to the row.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name, is_active=True)
    session.add(row)
    session.commit()
    if admin:
        _grant_admin(session, row.id)
    return PrincipalRef(id=row.id, name=name, type=row.type)


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one type, oldest first.

    Parameters
    ----------
    session
        Database session.
    kind
        The type.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    session.expire_all()
    return list(
        session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == kind).order_by(AuditEvent.occurred_at)
        )
    )


def _wait_with(session: Session, job: str, **pending: object) -> None:
    """Store a waiting start on a job's schedule row.

    Parameters
    ----------
    session
        Database session.
    job
        The job.
    **pending
        Keys of the stored start; the rest are filled in as a manual start of one request.
    """
    row = _row(session, job)
    row.pending_start = {
        "trigger": "manual",
        "requested_at": datetime(2026, 10, 15, 3, 0, tzinfo=UTC).isoformat(),
        "requested_by": None,
        "scheduled_for": None,
        "missed": 0,
        "merged": 1,
        **pending,
    }
    session.commit()


def _account(session: Session, name: str = "etl") -> Principal:
    """Persist a service account.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    Principal
        The row.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name=name, is_active=True)
    session.add(row)
    session.commit()
    return row


def _bind(session: Session, principal_id: uuid.UUID) -> None:
    """Bind a run principal to the deployment.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The principal.
    """
    session.execute(update(EnvironmentDeployment).values(run_as_principal_id=principal_id))
    session.commit()


def _row(session: Session, job: str) -> JobSchedule:
    """Read a job's schedule row afresh.

    Parameters
    ----------
    session
        Database session.
    job
        The job.

    Returns
    -------
    JobSchedule
        The row.
    """
    session.expire_all()
    return session.scalars(select(JobSchedule).where(JobSchedule.job == job)).one()


def test_start_runs_the_jobs_selection_as_the_caller(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A manual start creates a run with the job's selection, job and trigger `manual`."""
    result = _service(session).start(principal, "dev", "analytics", "manual")
    assert result.waiting is None and result.run is not None
    run = result.run
    assert (run.job, run.trigger, run.scheduled_for, run.missed) == ("manual", "manual", None, 0)
    assert run.selection["patterns"] == ["silver.*"]
    assert run.triggered_by == "admin" and run.run_as == "admin"
    assert [t.asset for t in run.tasks] == ["silver.clean"]


def test_a_service_account_start_has_the_trigger_api(
    session: Session, deployed: Repository
) -> None:
    """The trigger is `api` when the caller is a service account."""
    account = _account(session)
    caller = PrincipalRef(id=account.id, name=account.name, type=account.type)
    run = _service(session).start(caller, "dev", "analytics", "manual").run
    assert run is not None and run.trigger == "api"


def test_run_started_carries_the_four_fields(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The audit detail has `job`, `trigger`, `scheduled_for` and `missed`."""
    _service(session).start(principal, "dev", "analytics", "daily")
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.RUN_STARTED)
    ).one()
    assert event.detail["job"] == "daily"
    assert event.detail["trigger"] == "manual"
    assert event.detail["scheduled_for"] is None
    assert event.detail["missed"] == 0


def test_an_unknown_job_is_a_404_that_lists_the_jobs(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The message names the jobs that exist."""
    with pytest.raises(NotFoundError, match="daily, manual"):
        _service(session).start(principal, "dev", "analytics", "nope")
    with pytest.raises(NotFoundError, match="daily"):
        _service(session).pause(principal, "dev", "analytics", "nope")


def test_starting_needs_run_and_pausing_needs_deploy(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """RUN starts, DEPLOY pauses and resumes; each is checked on the environment."""
    run_only = ActionAllowList(Action.RUN)
    assert _service(session, run_only).start(principal, "dev", "analytics", "manual").run
    assert run_only.seen[0] == (Action.RUN, Securable.environment_scope("dev"))
    for method in ("pause", "resume"):
        with pytest.raises(PermissionDenied):
            getattr(_service(session, run_only), method)(principal, "dev", "analytics", "daily")
    deploy_only = ActionAllowList(Action.DEPLOY)
    with pytest.raises(PermissionDenied):
        _service(session, deploy_only).start(principal, "dev", "analytics", "daily")
    _service(session, deploy_only).pause(principal, "dev", "analytics", "daily")
    assert deploy_only.seen[-1] == (Action.DEPLOY, Securable.environment_scope("dev"))


def test_a_busy_project_gives_one_waiting_start_that_merges(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A second start waits for the active run; a third merges into it."""
    service = _service(session)
    first = service.start(principal, "dev", "analytics", "manual").run
    assert first is not None
    second = service.start(principal, "dev", "analytics", "daily")
    assert second.run is None and second.waiting is not None
    assert second.waiting.waits_for_run_id == first.id
    assert (second.waiting.merged, second.waiting.job) == (1, "daily")
    third = service.start(principal, "dev", "analytics", "daily")
    assert third.waiting is not None and third.waiting.merged == 2
    row = _row(session, "daily")
    assert row.pending_start is not None and row.pending_start["merged"] == 2
    assert session.scalars(select(Run)).all() == [session.get(Run, first.id)]


def test_an_ad_hoc_run_still_answers_409_when_busy(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """`lakana run` is refused with the plain conflict, not a waiting start."""
    runs = RunService(session, AllowAllAuthorization(), caps=RunCaps())
    runs.start(principal, "dev", "analytics", Selection())
    with pytest.raises(ConflictError, match="already active") as caught:
        runs.start(principal, "dev", "analytics", Selection())
    assert type(caught.value) is ConflictError


def test_protected_environment_needs_a_bound_principal(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """Protected and unbound: the existing message; bound: the bound account runs it."""
    session.execute(update(Environment).values(is_protected=True))
    session.commit()
    with pytest.raises(ConflictError, match="set-run-as"):
        _service(session).start(principal, "dev", "analytics", "manual")
    account = _account(session)
    _bind(session, account.id)
    run = _service(session).start(principal, "dev", "analytics", "manual").run
    assert run is not None and (run.run_as, run.triggered_by) == ("etl", "admin")


def test_a_scheduled_start_uses_only_the_bound_principal(
    session: Session, deployed: Repository
) -> None:
    """The trigger `schedule` needs no caller, no `RUN`, and refuses without a binding."""
    nobody = ActionAllowList()
    scheduled_for = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    with pytest.raises(ConflictError, match="set-run-as"):
        _service(session, nobody).start(
            None, "dev", "analytics", "daily", trigger=RunTrigger.SCHEDULE
        )
    account = _account(session)
    _bind(session, account.id)
    run = (
        _service(session, nobody)
        .start(
            None,
            "dev",
            "analytics",
            "daily",
            trigger=RunTrigger.SCHEDULE,
            scheduled_for=scheduled_for,
            missed=2,
        )
        .run
    )
    assert run is not None
    assert (run.run_as, run.triggered_by) == ("etl", "etl")
    assert (run.trigger, run.scheduled_for, run.missed) == ("schedule", scheduled_for, 2)
    assert nobody.seen == []


def test_pause_and_resume_are_audited_and_resume_recomputes_the_next_fire(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """Pausing keeps a waiting start; resuming moves the next fire to after now."""
    service = _service(session)
    service.start(principal, "dev", "analytics", "manual")
    service.start(principal, "dev", "analytics", "daily")
    stale = datetime(2020, 1, 1, tzinfo=UTC)
    session.execute(
        update(JobSchedule).where(JobSchedule.job == "daily").values(next_fire_at=stale)
    )
    session.commit()
    paused = service.pause(principal, "dev", "analytics", "daily")
    # the waiting start is the nearer event, so it names the state; `paused` still says paused
    assert paused.state == "waiting" and paused.paused
    row = _row(session, "daily")
    assert row.paused and row.paused_by == principal.id and row.paused_at is not None
    assert row.pending_start is not None
    before = datetime.now(UTC) - timedelta(seconds=5)
    resumed = service.resume(principal, "dev", "analytics", "daily")
    row = _row(session, "daily")
    assert not row.paused and row.paused_by is None and row.pending_start is not None
    assert row.next_fire_at is not None and row.next_fire_at > before
    assert row.next_fire_at == next_fire_after("0 3 * * *", "UTC", row.next_fire_at - timedelta(1))
    assert resumed.state == "waiting"
    events = session.scalars(
        select(AuditEvent.event_type).where(
            AuditEvent.event_type.in_([AuditEventType.JOB_PAUSED, AuditEventType.JOB_RESUMED])
        )
    ).all()
    assert sorted(e.value for e in events) == ["JOB_PAUSED", "JOB_RESUMED"]


def test_pausing_a_job_without_a_schedule_is_refused(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """There is nothing to pause; the message says so and what to do."""
    with pytest.raises(ConflictError, match="no schedule"):
        _service(session).pause(principal, "dev", "analytics", "manual")


def test_list_shows_the_state_and_the_next_time(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The states `scheduled`, `paused`, `waiting`, `no schedule` and `no run principal`."""
    service = _service(session)
    account = _account(session)
    views = {v.name: v for v in service.list(principal, "dev")}
    assert views["daily"].state == "no run principal"
    assert views["manual"].state == "no schedule"
    _bind(session, account.id)
    views = {v.name: v for v in service.list(principal, "dev")}
    daily = views["daily"]
    assert daily.state == "scheduled"
    assert (daily.cron, daily.timezone) == ("0 3 * * *", "UTC")
    assert daily.next_run_at is not None and daily.next_run_local is not None
    assert daily.next_run_local.startswith(daily.next_run_at.isoformat()[:10])
    assert views["manual"].next_run_at is None
    service.pause(principal, "dev", "analytics", "daily")
    assert {v.name: v.state for v in service.list(principal, "dev")}["daily"] == "paused"
    service.resume(principal, "dev", "analytics", "daily")
    first = service.start(principal, "dev", "analytics", "manual").run
    assert first is not None
    service.start(principal, "dev", "analytics", "daily")
    listed = {v.name: v for v in service.list(principal, "dev", "analytics")}
    assert listed["daily"].state == "waiting"
    assert listed["daily"].waiting_for is not None
    assert listed["daily"].waiting_for.code == "job_waits"
    assert str(first.id) in listed["daily"].waiting_for.message
    assert listed["manual"].last_run_id == first.id
    assert listed["manual"].last_run_trigger == "manual"


def test_a_job_view_carries_its_notify_commit_and_source_link(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A job names its notification, its deployed commit and its file on the git host."""
    deployed.url = "https://github.com/acme/analytics"
    version = session.scalars(select(JobVersion)).one()
    manifest = dict(version.manifest_json)
    jobs = {name: dict(entry) for name, entry in manifest["jobs"].items()}
    jobs["daily"]["notify"] = {"webhook": "alerts-webhook", "format": "slack"}
    manifest["jobs"] = jobs
    version.manifest_json = manifest
    session.commit()
    views = {v.name: v for v in _service(session).list(principal, "dev")}
    daily = views["daily"]
    assert daily.notify is not None
    assert (daily.notify.webhook, daily.notify.format) == ("alerts-webhook", "slack")
    assert daily.commit_sha == "a" * 40
    assert daily.source_url is not None
    assert daily.source_url.endswith(f"/blob/{'a' * 40}/lakana.yaml#L1")
    assert views["manual"].notify is None


def test_a_local_repository_has_no_source_link(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A repository on no known host gives the commit but no link."""
    deployed.kind = RepositoryKind.LOCAL
    session.commit()
    view = _service(session).get(principal, "dev", "daily")
    assert view.commit_sha == "a" * 40
    assert view.source_url is None


def test_a_damaged_notify_is_none(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A webhook that is not text reads as no notification."""
    version = session.scalars(select(JobVersion)).one()
    manifest = dict(version.manifest_json)
    jobs = {name: dict(entry) for name, entry in manifest["jobs"].items()}
    jobs["daily"]["notify"] = {"webhook": 5, "format": "slack"}
    manifest["jobs"] = jobs
    version.manifest_json = manifest
    session.commit()
    assert _service(session).get(principal, "dev", "daily").notify is None


def test_list_in_the_time_zone_of_the_job(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The local time is written in the job's zone, with its offset."""
    session.execute(
        update(JobSchedule)
        .where(JobSchedule.job == "daily")
        .values(next_fire_at=datetime(2026, 7, 1, 1, 0, tzinfo=UTC))
    )
    session.commit()
    from lakana_core.models import JobVersion

    version = session.scalars(select(JobVersion)).one()
    manifest = {**version.manifest_json}
    manifest["jobs"] = {
        **manifest["jobs"],
        "daily": {
            **manifest["jobs"]["daily"],
            "schedule": {"cron": "0 3 * * *", "timezone": "Europe/Berlin"},
        },
    }
    version.manifest_json = manifest
    session.commit()
    daily = {v.name: v for v in _service(session).list(principal, "dev")}["daily"]
    assert daily.next_run_local == "2026-07-01T03:00:00+02:00"
    assert daily.timezone == "Europe/Berlin"


def test_list_needs_read_environment(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The list is filtered as the deploy views are."""
    with pytest.raises(PermissionDenied):
        _service(session, ActionAllowList(Action.RUN)).list(principal, "dev")
    allowed = ActionAllowList(Action.READ_ENVIRONMENT)
    assert _service(session, allowed).list(principal, "dev")
    assert allowed.seen[0] == (Action.READ_ENVIRONMENT, Securable.environment_scope("dev"))


def test_start_waiting_starts_a_waiting_start_of_a_free_project(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The run takes the trigger, scheduled time and missed count; the start is cleared."""
    service = _service(session)
    first = service.start(principal, "dev", "analytics", "manual").run
    assert first is not None
    service.start(principal, "dev", "analytics", "daily")
    schedule = session.scalars(select(JobSchedule).where(JobSchedule.job == "daily")).one()
    assert JobService.start_waiting(session, schedule) is None
    assert _row(session, "daily").pending_start is not None
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    schedule = _row(session, "daily")
    run = JobService.start_waiting(session, schedule)
    assert run is not None and run.job == "daily"
    assert (run.trigger, run.triggered_by) == (RunTrigger.MANUAL, principal.id)
    assert _row(session, "daily").pending_start is None
    assert JobService.start_waiting(session, _row(session, "daily")) is None


def test_start_waiting_copies_a_scheduled_start(session: Session, deployed: Repository) -> None:
    """A waiting scheduled start runs as the bound principal with its time and count."""
    account = _account(session)
    _bind(session, account.id)
    scheduled_for = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    row = _row(session, "daily")
    row.pending_start = {
        "trigger": "schedule",
        "requested_at": datetime(2026, 10, 15, 3, 0, 1, tzinfo=UTC).isoformat(),
        "requested_by": None,
        "scheduled_for": scheduled_for.isoformat(),
        "missed": 3,
        "merged": 1,
    }
    session.commit()
    run = JobService.start_waiting(session, _row(session, "daily"))
    assert run is not None
    assert (run.trigger, run.scheduled_for, run.missed) == (RunTrigger.SCHEDULE, scheduled_for, 3)
    assert run.run_as_principal_id == account.id
    assert _row(session, "daily").pending_start is None


def test_a_start_by_hand_adds_to_a_waiting_scheduled_start_and_a_schedule_counts_missed(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A scheduled start keeps its identity when a person asks too; a later fire counts the old."""
    account = _account(session)
    _bind(session, account.id)
    service = _service(session)
    assert service.start(principal, "dev", "analytics", "manual").run is not None
    first = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    second = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)
    waiting = service.start(
        None, "dev", "analytics", "daily", trigger=RunTrigger.SCHEDULE, scheduled_for=first
    ).waiting
    assert waiting is not None and (waiting.trigger, waiting.merged) == ("schedule", 1)
    waiting = service.start(principal, "dev", "analytics", "daily").waiting
    assert waiting is not None
    assert (waiting.trigger, waiting.scheduled_for, waiting.merged) == ("schedule", first, 2)
    waiting = service.start(
        None, "dev", "analytics", "daily", trigger=RunTrigger.SCHEDULE, scheduled_for=second
    ).waiting
    assert waiting is not None
    assert (waiting.scheduled_for, waiting.missed, waiting.merged) == (second, 1, 3)


# -- later additions -----------------------------------------------------------------------------


def test_a_removed_job_is_not_brought_back_by_a_start_that_waited(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """After the lock the manifest is read again: a job a deploy removed gives a 404, no row."""
    service = _service(session)
    service.start(principal, "dev", "analytics", "manual")
    version = session.scalars(select(JobVersion)).one()
    manifest = {**version.manifest_json}
    manifest["jobs"] = {"manual": manifest["jobs"]["manual"]}
    version.manifest_json = manifest
    session.execute(delete(JobSchedule).where(JobSchedule.job == "daily"))
    session.commit()
    with pytest.raises(NotFoundError, match="`daily` was removed from `analytics` in `dev`"):
        service._park(principal, "dev", "analytics", "daily", RunTrigger.MANUAL, None, 0)
    assert session.scalars(select(JobSchedule).where(JobSchedule.job == "daily")).all() == []
    with pytest.raises(NotFoundError, match="manual"):
        service.pause(principal, "dev", "analytics", "daily")
    with pytest.raises(NotFoundError, match="manual"):
        service.resume(principal, "dev", "analytics", "daily")
    assert session.scalars(select(JobSchedule).where(JobSchedule.job == "daily")).all() == []


def test_a_job_without_its_row_is_not_created_outside_a_deploy(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A manifest job with no row (never after a deploy) is a 404, not a new row."""
    session.execute(delete(JobSchedule).where(JobSchedule.job == "daily"))
    session.commit()
    with pytest.raises(NotFoundError, match="removed"):
        _service(session).pause(principal, "dev", "analytics", "daily")
    assert session.scalars(select(JobSchedule).where(JobSchedule.job == "daily")).all() == []


def test_a_move_of_the_pointer_while_a_lock_waits_is_tried_again(
    session: Session,
    deployed: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DeploymentMoved` from the lock is retried, then gives up with a 409 that says retry."""
    from lakana_core.projects import deploys

    real = deploys.DeployService.lock_deployment
    calls: list[int] = []

    def moved_once(self: object, *args: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            session.rollback()
            raise deploys.DeploymentMoved("The deployment changed meanwhile; retry.")
        real(self, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(deploys.DeployService, "lock_deployment", moved_once)
    assert _service(session).pause(principal, "dev", "analytics", "daily").paused
    assert len(calls) == 2

    def always(self: object, *args: object) -> None:
        session.rollback()
        raise deploys.DeploymentMoved("The deployment changed meanwhile; retry.")

    monkeypatch.setattr(deploys.DeployService, "lock_deployment", always)
    with pytest.raises(deploys.DeploymentMoved):
        _service(session).resume(principal, "dev", "analytics", "daily")


def test_a_waiting_start_is_audited_with_what_it_replaced(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """`JOB_START_WAITING` names the caller, the run waited for, the count and the replaced one."""
    other = _person(session, "other", admin=True)
    service = _service(session)
    first = service.start(principal, "dev", "analytics", "manual").run
    assert first is not None
    service.start(principal, "dev", "analytics", "daily")
    waiting = service.start(other, "dev", "analytics", "daily").waiting
    assert waiting is not None and waiting.requested_by == "other"
    one, two = _events(session, AuditEventType.JOB_START_WAITING)
    assert (one.principal_id, one.detail["merged"], one.detail["replaced"]) == (
        principal.id,
        1,
        None,
    )
    assert two.principal_id == other.id
    assert two.detail["job"] == "daily" and two.detail["repository"] == "analytics"
    assert two.detail["trigger"] == "manual" and two.detail["merged"] == 2
    assert two.detail["waits_for_run_id"] == str(first.id)
    assert two.detail["replaced"] == {"trigger": "manual", "requested_by": str(principal.id)}


def test_the_run_of_a_waiting_start_says_how_many_requests_it_stands_for(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """`RUN_STARTED` carries `merged`, 1 for an immediate start."""
    service = _service(session)
    first = service.start(principal, "dev", "analytics", "manual").run
    assert first is not None
    assert _events(session, AuditEventType.RUN_STARTED)[-1].detail["merged"] == 1
    for _ in range(2):
        service.start(principal, "dev", "analytics", "daily")
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    run = JobService.start_waiting(session, _row(session, "daily"))
    assert run is not None
    assert _events(session, AuditEventType.RUN_STARTED)[-1].detail["merged"] == 2


def test_the_latest_of_two_people_replaces_the_waiting_start(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The run is attributed to the person who asked last."""
    other = _person(session, "other", admin=True)
    service = _service(session)
    service.start(principal, "dev", "analytics", "manual")
    service.start(principal, "dev", "analytics", "daily")
    service.start(other, "dev", "analytics", "daily")
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    run = JobService.start_waiting(session, _row(session, "daily"))
    assert run is not None and run.triggered_by == other.id


def test_a_scheduled_start_replaces_a_waiting_manual_one(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A manual request then a schedule: the schedule's identity, `merged` 2, nothing missed."""
    account = _account(session)
    _bind(session, account.id)
    service = _service(session)
    service.start(principal, "dev", "analytics", "manual")
    service.start(principal, "dev", "analytics", "daily")
    at = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    waiting = service.start(
        None, "dev", "analytics", "daily", trigger=RunTrigger.SCHEDULE, scheduled_for=at
    ).waiting
    assert waiting is not None
    assert (waiting.trigger, waiting.merged, waiting.missed, waiting.scheduled_for) == (
        "schedule",
        2,
        0,
        at,
    )
    (_, replaced) = _events(session, AuditEventType.JOB_START_WAITING)
    assert replaced.detail["replaced"]["trigger"] == "manual"


@pytest.mark.parametrize(
    ("trigger", "caller"), [(RunTrigger.SCHEDULE, True), (RunTrigger.API, False)]
)
def test_the_trigger_schedule_and_the_caller_go_together(
    session: Session,
    deployed: Repository,
    principal: PrincipalRef,
    trigger: RunTrigger,
    caller: bool,
) -> None:
    """A caller cannot carry `schedule` (it would skip RUN); `schedule` cannot have a caller."""
    with pytest.raises(TypeError, match="schedule"):
        _service(session).start(
            principal if caller else None, "dev", "analytics", "daily", trigger=trigger
        )
    with pytest.raises(TypeError, match="caller"):
        _service(session).start(None, "dev", "analytics", "daily")


def test_prepare_start_refuses_a_start_with_no_one_to_check(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The internal contract: a caller, a selection, and a caller to check."""
    runs = RunService(session, AllowAllAuthorization(), caps=RunCaps())
    with pytest.raises(TypeError, match="caller"):
        runs.prepare_start(None, "dev", "analytics", Selection())
    with pytest.raises(TypeError, match="checked"):
        runs.prepare_start(None, "dev", "analytics", Selection(), bound_only=True)
    with pytest.raises(TypeError, match="selection"):
        runs.prepare_start(principal, "dev", "analytics", None)


def test_a_start_that_finds_the_project_free_meanwhile_starts_again(
    session: Session,
    deployed: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_park` that sees no active run lets the start try again; endless misses give a 409."""
    service = _service(session)
    assert service.start(principal, "dev", "analytics", "manual").run is not None
    real = service._active_run
    seen: list[int] = []

    def free_once(env_id: uuid.UUID, repo_id: uuid.UUID) -> uuid.UUID | None:
        seen.append(1)
        return None if len(seen) == 1 else real(env_id, repo_id)

    monkeypatch.setattr(service, "_active_run", free_once)
    assert service.start(principal, "dev", "analytics", "daily").waiting is not None
    assert len(seen) == 2
    monkeypatch.setattr(service, "_active_run", lambda env_id, repo_id: None)
    with pytest.raises(ConflictError, match="kept changing"):
        service.start(principal, "dev", "analytics", "daily")


def _drop_reason(session: Session) -> str:
    """Read the reason of the one `JOB_START_DROPPED` event.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    str
        Its `reason`.
    """
    (event,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert event.detail["job"] == "daily" and event.detail["repository"] == "analytics"
    assert event.principal_id is None
    return event.detail["reason"]


def test_a_waiting_start_of_a_requester_who_is_inactive_is_dropped(
    session: Session, deployed: Repository
) -> None:
    """The request of a principal an admin cut off does not run, whatever is bound."""
    gone = _person(session, "gone", admin=True)
    account = _account(session)
    _bind(session, account.id)
    _wait_with(session, "daily", requested_by=str(gone.id))
    session.execute(update(Principal).where(Principal.id == gone.id).values(is_active=False))
    session.commit()
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _row(session, "daily").pending_start is None
    assert session.scalars(select(Run)).all() == []
    assert _drop_reason(session) == "requester_inactive"
    (event,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert event.detail["requested_by"] == str(gone.id)


def test_a_waiting_start_of_an_unknown_requester_is_dropped(
    session: Session, deployed: Repository
) -> None:
    """A requester id that is no principal any more counts as inactive."""
    _wait_with(session, "daily", requested_by=str(uuid.uuid4()))
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _drop_reason(session) == "requester_inactive"
    _wait_with(session, "daily", requested_by="not-an-id")
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert [e.detail["reason"] for e in _events(session, AuditEventType.JOB_START_DROPPED)] == [
        "requester_inactive",
        "requester_inactive",
    ]


def test_a_waiting_start_of_a_requester_who_lost_run_is_dropped(
    session: Session, deployed: Repository
) -> None:
    """A principal with no RUN grant any more cannot have its request carried out."""
    plain = _person(session, "plain")
    _wait_with(session, "daily", requested_by=str(plain.id))
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _drop_reason(session) == "requester_lacks_run"
    assert session.scalars(select(Run)).all() == []


def test_a_waiting_start_that_has_no_run_principal_any_more_is_dropped(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A protected environment with no binding, or a scheduled start with none, is dropped."""
    session.execute(update(Environment).values(is_protected=True))
    session.commit()
    _wait_with(session, "daily", requested_by=str(principal.id))
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _drop_reason(session) == "no_run_principal"
    _wait_with(session, "manual", trigger="schedule", scheduled_for="2026-10-15T03:00:00+00:00")
    assert JobService.start_waiting(session, _row(session, "manual")) is None
    assert _row(session, "manual").pending_start is None
    assert len(_events(session, AuditEventType.JOB_START_DROPPED)) == 2


@pytest.mark.parametrize("fault", ["inactive", "user"])
def test_a_waiting_start_whose_bound_principal_is_unusable_says_so(
    session: Session, deployed: Repository, fault: str
) -> None:
    """An inactive or non-service-account principal is its own reason, not `no_run_principal`."""
    if fault == "inactive":
        account = _account(session)
        session.execute(update(Principal).where(Principal.id == account.id).values(is_active=False))
        session.commit()
        bound = account.id
    else:
        bound = _person(session, "human").id
    _bind(session, bound)
    _wait_with(session, "daily", trigger="schedule", scheduled_for="2026-10-15T03:00:00+00:00")
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _row(session, "daily").pending_start is None
    assert _drop_reason(session) == "run_principal_unusable"


def test_a_job_in_a_project_that_declares_none_says_so(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The 404 for an unknown job tells a project without a `jobs:` section how to add one."""
    version = session.scalars(select(JobVersion)).one()
    manifest = {**version.manifest_json}
    manifest["jobs"] = {}
    version.manifest_json = manifest
    session.commit()
    with pytest.raises(NotFoundError, match="declares no job"):
        _service(session).start(principal, "dev", "analytics", "daily")


def test_a_waiting_start_of_a_job_the_manifest_lost_is_dropped(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The job is gone from the current project version: dropped with that reason."""
    version = session.scalars(select(JobVersion)).one()
    manifest = {**version.manifest_json}
    manifest["jobs"] = {"manual": manifest["jobs"]["manual"]}
    version.manifest_json = manifest
    session.commit()
    _wait_with(session, "daily", requested_by=str(principal.id))
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _drop_reason(session) == "job_removed"


def test_a_waiting_start_that_cannot_be_read_is_dropped(
    session: Session, deployed: Repository
) -> None:
    """A stored start with an unknown trigger is dropped rather than kept forever."""
    _wait_with(session, "daily", trigger="telepathy")
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _drop_reason(session) == "unreadable"


def test_a_dropped_start_does_not_block_the_other_waiting_starts(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The scheduler goes on to the next start after one is dropped."""
    plain = _person(session, "plain")
    _wait_with(session, "daily", requested_by=str(plain.id))
    _wait_with(session, "manual", requested_by=str(principal.id))
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    run = JobService.start_waiting(session, _row(session, "manual"))
    assert run is not None and run.job == "manual"


def test_start_waiting_leaves_a_start_alone_while_the_project_is_busy(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """Busy is not a reason to drop; the start stays and no event is written."""
    service = _service(session)
    service.start(principal, "dev", "analytics", "manual")
    plain = _person(session, "plain")
    _wait_with(session, "daily", requested_by=str(plain.id))
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _row(session, "daily").pending_start is not None
    assert _events(session, AuditEventType.JOB_START_DROPPED) == []


def test_start_waiting_with_nothing_to_do(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """No start, an undeployed project, or a start another process cleared: None."""
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    _wait_with(session, "daily", requested_by=str(principal.id))
    stale = _row(session, "daily")
    session.expunge(stale)
    session.execute(
        update(JobSchedule).where(JobSchedule.job == "daily").values(pending_start=None)
    )
    session.commit()
    assert stale.pending_start is not None
    assert JobService.start_waiting(session, stale) is None
    assert session.scalars(select(Run)).all() == []
    _wait_with(session, "daily", requested_by=str(principal.id))
    session.execute(delete(EnvironmentDeployment))
    session.commit()
    assert JobService.start_waiting(session, _row(session, "daily")) is None


def test_a_paused_jobs_waiting_start_still_starts(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """Pausing stops the schedule, not the start that already waits."""
    service = _service(session)
    service.start(principal, "dev", "analytics", "manual")
    service.start(principal, "dev", "analytics", "daily")
    service.pause(principal, "dev", "analytics", "daily")
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    run = JobService.start_waiting(session, _row(session, "daily"))
    assert run is not None and run.job == "daily"
    assert _row(session, "daily").paused


def test_the_pause_and_resume_events_name_the_actor_and_the_job(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """Actor, environment and detail; repeating a pause or a resume writes nothing."""
    service = _service(session)
    service.pause(principal, "dev", "analytics", "daily")
    service.pause(principal, "dev", "analytics", "daily")
    service.resume(principal, "dev", "analytics", "daily")
    service.resume(principal, "dev", "analytics", "daily")
    (paused,) = _events(session, AuditEventType.JOB_PAUSED)
    (resumed,) = _events(session, AuditEventType.JOB_RESUMED)
    for event in (paused, resumed):
        assert event.principal_id == principal.id
        assert event.securable_environment == "dev"
        assert event.detail == {"repository": "analytics", "job": "daily"}


def test_resume_pins_the_first_fire_after_now(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The next fire is the first one after the moment of the call, not a day later."""
    service = _service(session)
    service.pause(principal, "dev", "analytics", "daily")
    before = db_now(session)
    service.resume(principal, "dev", "analytics", "daily")
    after = db_now(session)
    fire = _row(session, "daily").next_fire_at
    assert fire in {
        next_fire_after("0 3 * * *", "UTC", before),
        next_fire_after("0 3 * * *", "UTC", after),
    }


def test_a_paused_job_shows_no_next_run(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A paused schedule will not fire, so the list does not promise a time."""
    account = _account(session)
    _bind(session, account.id)
    service = _service(session)
    paused = service.pause(principal, "dev", "analytics", "daily")
    assert (paused.state, paused.next_run_at, paused.next_run_local) == ("paused", None, None)
    assert {v.name: v for v in service.list(principal, "dev")}["daily"].next_run_at is None


def test_a_waiting_start_is_shown_also_when_no_run_is_active(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """Between the end of the run and the next scheduler tick the start is still listed."""
    _wait_with(session, "daily", requested_by=str(principal.id))
    daily = {v.name: v for v in _service(session).list(principal, "dev")}["daily"]
    assert daily.state == "waiting to start"
    assert daily.waiting_for is not None and daily.waiting_for.code == "job_starts_soon"
    assert daily.waits_for_run_id is None


def test_the_waiting_sentence_names_the_next_step(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The sentence says which run to follow, in which environment."""
    service = _service(session)
    first = service.start(principal, "dev", "analytics", "manual").run
    assert first is not None
    waiting = service.start(principal, "dev", "analytics", "daily").waiting
    assert waiting is not None
    assert f"lakana runs show {first.id} --env dev" in waiting.message


def test_resume_of_a_run_keeps_its_job_and_takes_the_trigger_of_the_resumer(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A resumed job run still counts as a run of the job; ad hoc stays without a job."""
    runs = RunService(session, AllowAllAuthorization(), caps=RunCaps())
    run = _service(session).start(principal, "dev", "analytics", "manual").run
    assert run is not None
    for task in session.scalars(select(Run).where(Run.id == run.id)):
        task.status = RunStatus.FAILED
    from lakana_core.models import TaskRun, TaskStatus

    session.execute(update(TaskRun).values(status=TaskStatus.FAILED))
    session.commit()
    robot = _account(session, "robot")
    resumer = PrincipalRef(id=robot.id, name="robot", type=robot.type)
    resumed = runs.resume(resumer, "dev", run.id)
    assert (resumed.job, resumed.trigger) == ("manual", "api")
    session.execute(update(Run).values(status=RunStatus.FAILED))
    adhoc = runs.start(principal, "dev", "analytics", Selection())
    session.execute(update(Run).values(status=RunStatus.FAILED))
    session.execute(update(TaskRun).values(status=TaskStatus.FAILED))
    session.commit()
    again = runs.resume(principal, "dev", adhoc.id)
    assert (again.job, again.trigger) == (None, None)


def test_a_waiting_start_whose_selection_matches_nothing_any_more_is_dropped(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A redeployed project version without the assets the job selects drops the start."""
    version = session.scalars(select(JobVersion)).one()
    manifest = {**version.manifest_json}
    manifest["jobs"] = {
        **manifest["jobs"],
        "daily": {**manifest["jobs"]["daily"], "select": ["nothing.*"]},
    }
    version.manifest_json = manifest
    session.commit()
    _wait_with(session, "daily", requested_by=str(principal.id))
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _drop_reason(session) == "selection_invalid"


def test_a_start_whose_pointer_moves_is_tried_again(
    session: Session,
    deployed: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`DeploymentMoved` from the insert makes the start read the deployment again."""
    from lakana_core.projects.deploys import DeploymentMoved

    service = _service(session)
    real = service._runs.start_job
    calls: list[int] = []

    def moved_once(*args: object, **kwargs: object) -> object:
        calls.append(1)
        if len(calls) == 1:
            raise DeploymentMoved("The deployment changed meanwhile; retry.")
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(service._runs, "start_job", moved_once)
    assert service.start(principal, "dev", "analytics", "manual").run is not None
    assert len(calls) == 2
    monkeypatch.setattr(
        service._runs,
        "start_job",
        lambda *a, **k: (_ for _ in ()).throw(DeploymentMoved("again")),
    )
    with pytest.raises(ConflictError, match="kept changing"):
        service.start(principal, "dev", "analytics", "daily")


def test_a_manual_start_that_names_no_requester_is_dropped(
    session: Session, deployed: Repository
) -> None:
    """A stored manual start without a requester cannot be attributed to anyone."""
    _wait_with(session, "daily", requested_by=None)
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _drop_reason(session) == "requester_inactive"


@pytest.mark.parametrize("by", ["schedule", "hand"])
def test_a_merge_or_replace_keeps_the_first_request_time(
    session: Session, deployed: Repository, principal: PrincipalRef, by: str
) -> None:
    """The wait is counted from the first request, so a frequent job cannot go to the back."""
    _bind(session, _account(session).id)
    service = _service(session)
    assert service.start(principal, "dev", "analytics", "manual").run is not None
    first = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
    _wait_with(session, "daily", requested_at=first.isoformat(), requested_by=str(principal.id))
    if by == "schedule":
        service.start(
            None,
            "dev",
            "analytics",
            "daily",
            trigger=RunTrigger.SCHEDULE,
            scheduled_for=datetime(2026, 10, 15, 3, 0, tzinfo=UTC),
        )
    else:
        service.start(principal, "dev", "analytics", "daily")
    pending = _row(session, "daily").pending_start
    assert pending is not None
    assert (pending["requested_at"], pending["merged"]) == (first.isoformat(), 2)


def test_a_waiting_start_with_a_malformed_scheduled_time_is_dropped_as_unreadable(
    session: Session, deployed: Repository
) -> None:
    """It is dropped with a reason, not retried (and logged) on every tick."""
    _bind(session, _account(session).id)
    _wait_with(session, "daily", trigger="schedule", scheduled_for="garbage")
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    assert _row(session, "daily").pending_start is None
    assert _drop_reason(session) == "unreadable"


@pytest.mark.parametrize("fault", ["inactive", "user"])
def test_the_list_says_when_the_bound_principal_cannot_run_the_job(
    session: Session, deployed: Repository, principal: PrincipalRef, fault: str
) -> None:
    """An inactive or non-service-account principal is not shown as `scheduled`."""
    if fault == "inactive":
        account = _account(session)
        session.execute(update(Principal).where(Principal.id == account.id).values(is_active=False))
        session.commit()
        _bind(session, account.id)
    else:
        _bind(session, _person(session, "human").id)
    daily = {v.name: v for v in _service(session).list(principal, "dev")}["daily"]
    assert daily.state == "run principal unusable"
    assert daily.waiting_for is not None and daily.waiting_for.code == "run_principal_unusable"
    assert "set-run-as" in daily.waiting_for.message


def test_a_malformed_request_time_does_not_break_the_list(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """Only a hand-edited row can hold one; the list still answers."""
    _wait_with(session, "daily", requested_at="garbage", requested_by=None)
    views = {v.name: v for v in _service(session).list(principal, "dev")}
    assert views["daily"].waiting_for is not None


def test_a_park_through_a_malformed_request_time_still_answers(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A garbage `requested_at` of a stored start turns neither a park nor its view into a 500."""
    _bind(session, _account(session).id)
    service = _service(session)
    assert service.start(principal, "dev", "analytics", "manual").run is not None
    _wait_with(session, "daily", requested_at="garbage", requested_by=str(principal.id))
    waiting = service.start(principal, "dev", "analytics", "daily").waiting
    assert waiting is not None and waiting.merged == 2


_COMMON = {"repository", "job", "trigger", "requested_by", "scheduled_for", "missed", "merged"}


def test_every_waiting_and_dropped_event_carries_the_same_fields(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """Park, replace, merge and every drop of a waiting start: one set of keys."""
    _bind(session, _account(session).id)
    service = _service(session)
    assert service.start(principal, "dev", "analytics", "manual").run is not None
    at = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    service.start(principal, "dev", "analytics", "daily")
    service.start(None, "dev", "analytics", "daily", trigger=RunTrigger.SCHEDULE, scheduled_for=at)
    service.start(principal, "dev", "analytics", "daily")
    for event in _events(session, AuditEventType.JOB_START_WAITING):
        assert set(event.detail) >= _COMMON | {"waits_for_run_id", "replaced"}
    last = _events(session, AuditEventType.JOB_START_WAITING)[-1]
    assert (last.detail["scheduled_for"], last.detail["missed"]) == (at.isoformat(), 0)
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()
    plain = _person(session, "plain")
    _wait_with(session, "daily", requested_by=str(plain.id), scheduled_for=at.isoformat(), missed=4)
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    (dropped,) = _events(session, AuditEventType.JOB_START_DROPPED)
    assert set(dropped.detail) >= _COMMON | {"reason"}
    assert (dropped.detail["scheduled_for"], dropped.detail["missed"]) == (at.isoformat(), 4)


def test_a_waiting_event_records_the_trigger_of_the_request_that_was_parked(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A request by hand that merges into a scheduled start: `trigger` is the start's, not its."""
    _bind(session, _account(session).id)
    service = _service(session)
    assert service.start(principal, "dev", "analytics", "manual").run is not None
    service.start(
        None,
        "dev",
        "analytics",
        "daily",
        trigger=RunTrigger.SCHEDULE,
        scheduled_for=datetime(2026, 10, 15, 3, 0, tzinfo=UTC),
    )
    service.start(principal, "dev", "analytics", "daily")
    first, second = _events(session, AuditEventType.JOB_START_WAITING)
    assert (first.detail["request_trigger"], second.detail["request_trigger"]) == (
        "schedule",
        "manual",
    )
    assert second.detail["trigger"] == "schedule"


@pytest.fixture
def notifying(session: Session, tmp_path: Path, principal: PrincipalRef) -> Repository:
    """Deploy `analytics` to `dev` with a notifying job `alerting` and a silent one `quiet`.

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
    Repository
        The repository.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[_BRONZE],
        tables=[TableRef("dev", _BRONZE, "events")],
    )
    repo = _repo(session)
    jobs = {"alerting": _job(cron=None, notify="team-alerts"), "quiet": _job(cron=None)}
    _deployed_jobs(session, principal, repo, "a" * 40, jobs)
    _grant_admin(session, principal.id)
    return repo


def test_notify_test_queues_a_test_row_and_audits_it_without_the_secret(
    session: Session, notifying: Repository, principal: PrincipalRef
) -> None:
    """The row has the event `test`, no run, is due now; the audit names the delivery only."""
    delivery_id = _service(session).notify_test(principal, "dev", "analytics", "alerting")
    row = session.get(NotificationDelivery, delivery_id)
    assert row is not None
    assert (row.event, row.run_id, row.state, row.attempts) == ("test", None, "pending", 0)
    assert (row.job, row.secret_name, row.format) == ("alerting", "team-alerts", "json")
    assert row.next_attempt_at is not None and row.next_attempt_at <= db_now(session)
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.NOTIFICATION_TESTED)
    ).one()
    assert event.principal_id == principal.id
    assert event.detail == {
        "repository": "analytics",
        "job": "alerting",
        "environment": "dev",
        "delivery_id": str(delivery_id),
    }


def test_several_test_rows_of_one_job_do_not_collide(
    session: Session, notifying: Repository, principal: PrincipalRef
) -> None:
    """The unique key (run_id, event) treats the null run ids as different."""
    service = _service(session)
    ids = {service.notify_test(principal, "dev", "analytics", "alerting") for _ in range(3)}
    assert len(ids) == 3
    assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 3


def test_notify_test_needs_the_deploy_right(
    session: Session, notifying: Repository, principal: PrincipalRef
) -> None:
    """RUN alone is refused, before anything is read; DEPLOY on the environment is checked."""
    run_only = ActionAllowList(Action.RUN)
    with pytest.raises(PermissionDenied):
        _service(session, run_only).notify_test(principal, "dev", "analytics", "alerting")
    assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
    deploy_only = ActionAllowList(Action.DEPLOY)
    _service(session, deploy_only).notify_test(principal, "dev", "analytics", "alerting")
    assert deploy_only.seen[0] == (Action.DEPLOY, Securable.environment_scope("dev"))


def test_notify_test_of_an_unknown_job_lists_the_jobs(
    session: Session, notifying: Repository, principal: PrincipalRef
) -> None:
    """A 404 that names the jobs that exist."""
    with pytest.raises(NotFoundError, match="alerting, quiet"):
        _service(session).notify_test(principal, "dev", "analytics", "nope")


def test_notify_test_of_a_job_without_notify_says_what_to_do(
    session: Session, notifying: Repository, principal: PrincipalRef
) -> None:
    """A 409 that names the job, the environment and the next step; nothing is queued."""
    with pytest.raises(
        ConflictError,
        match=r"Job `quiet` has no usable `notify:` in `dev`; add or fix it in "
        r"lakana.yaml and deploy\.",
    ):
        _service(session).notify_test(principal, "dev", "analytics", "quiet")
    assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0


def test_a_delivery_can_be_read_by_whoever_may_test_or_read_the_environment(
    session: Session, notifying: Repository, principal: PrincipalRef
) -> None:
    """The deployer who queued it and a reader see its state; a caller with neither is refused."""
    delivery_id = _service(session).notify_test(principal, "dev", "analytics", "alerting")
    view = _service(session).delivery(principal, "dev", delivery_id)
    assert (view.id, view.job, view.event, view.state) == (
        delivery_id,
        "alerting",
        "test",
        "pending",
    )
    assert (view.attempts, view.max_attempts, view.last_result) == (0, 5, None)
    for action in (Action.DEPLOY, Action.READ_ENVIRONMENT, Action.RUN):
        assert _service(session, ActionAllowList(action)).delivery(principal, "dev", delivery_id)
    with pytest.raises(PermissionDenied):
        _service(session, ActionAllowList()).delivery(principal, "dev", delivery_id)
    with pytest.raises(NotFoundError, match="delivery"):
        _service(session).delivery(principal, "dev", uuid.uuid4())


def test_a_delivery_is_not_readable_through_another_environment(
    session: Session, notifying: Repository, principal: PrincipalRef
) -> None:
    """A delivery queued in `dev` is not found by id through `prod`, even for an administrator."""
    session.add(Environment(id=uuid.uuid4(), name="prod", storage_root="file:///tmp/prod"))
    session.commit()
    delivery_id = _service(session).notify_test(principal, "dev", "analytics", "alerting")
    assert _service(session).delivery(principal, "dev", delivery_id).id == delivery_id
    with pytest.raises(NotFoundError, match="no delivery"):
        _service(session).delivery(principal, "prod", delivery_id)


def test_a_run_view_lists_its_notifications_and_a_stranger_gets_nothing(
    session: Session, notifying: Repository, principal: PrincipalRef
) -> None:
    """`RunService.get` carries the run's rows, never a test row; reading needs the run rights."""
    started = _service(session).start(principal, "dev", "analytics", "alerting").run
    assert started is not None
    run = session.get(Run, started.id)
    assert run is not None
    run_delivery_id = uuid.uuid4()
    session.add(
        NotificationDelivery(
            id=run_delivery_id,
            run_id=run.id,
            environment_id=run.environment_id,
            repository_id=run.repository_id,
            job="alerting",
            event="failed",
            format="json",
            secret_name="team-alerts",
            state=NotificationState.PENDING,
            attempts=2,
            last_result="the server answered 503",
        )
    )
    session.commit()
    _service(session).notify_test(principal, "dev", "analytics", "alerting")
    runs = RunService(session, AllowAllAuthorization(), caps=RunCaps())
    (shown,) = runs.get(principal, "dev", run.id).notifications
    assert shown.id == run_delivery_id
    assert (shown.event, shown.state, shown.attempts, shown.max_attempts) == (
        "failed",
        "pending",
        2,
        5,
    )
    assert shown.last_result == "the server answered 503" and shown.delivered_at is None
    assert runs.list_runs(principal, "dev")[0].notifications == ()
    with pytest.raises(PermissionDenied):
        RunService(session, ActionAllowList(), caps=RunCaps()).get(principal, "dev", run.id)


def test_notify_test_of_a_damaged_notify_entry_is_refused_like_a_missing_one(
    session: Session, notifying: Repository, principal: PrincipalRef
) -> None:
    """A stored `notify` that cannot be read gives the same 409, and queues nothing."""
    version = session.scalars(select(JobVersion)).one()
    manifest = {**version.manifest_json}
    manifest["jobs"] = {
        **manifest["jobs"],
        "alerting": {**manifest["jobs"]["alerting"], "notify": {"webhook": 7}},
    }
    version.manifest_json = manifest
    session.commit()
    with pytest.raises(ConflictError, match="no usable `notify:`"):
        _service(session).notify_test(principal, "dev", "analytics", "alerting")
    assert session.scalar(select(func.count()).select_from(NotificationDelivery)) == 0


def test_get_shows_one_job_and_says_what_is_wrong_otherwise(
    session: Session,
    deployed: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One job comes back as the list shows it; a miss or a clash names the next step."""
    service = _service(session)
    listed = {v.name: v for v in service.list(principal, "dev")}
    assert service.get(principal, "dev", "daily") == listed["daily"]
    assert service.get(principal, "dev", "daily", "analytics") == listed["daily"]
    with pytest.raises(NotFoundError, match=r"in 'dev'\. Its jobs: daily, manual"):
        service.get(principal, "dev", "nope")
    with pytest.raises(NotFoundError, match="in 'analytics' in 'dev'"):
        service.get(principal, "dev", "nope", "analytics")
    views = list(service.list(principal, "dev"))
    twin = dataclasses.replace(views[0], repository="other")
    monkeypatch.setattr(JobService, "list", lambda *_args: [*views, twin])
    with pytest.raises(ConflictError, match=r"analytics, other.*--repo"):
        service.get(principal, "dev", views[0].name)
    monkeypatch.setattr(JobService, "list", lambda *_args: [])
    with pytest.raises(NotFoundError, match="No job is deployed there"):
        service.get(principal, "dev", "daily")


def _job_view(name: str, repository: str = "analytics", **facts: object) -> JobView:
    """Build a job as the list shows it, with only the facts a sort reads set.

    Parameters
    ----------
    name
        The job's name.
    repository
        Its repository.
    **facts
        `last_run_status`, `last_run_at`, `next_run_at`, `cron`, `timezone` or `environment`.

    Returns
    -------
    JobView
        The view.
    """
    base = JobView(
        name=name,
        environment="dev",
        repository=repository,
        select=(),
        upstream=False,
        downstream=False,
        cron=None,
        timezone=None,
        state="no schedule",
        paused=False,
        next_run_at=None,
        next_run_local=None,
        waiting_for=None,
        waits_for_run_id=None,
        last_run_id=None,
        last_run_status=None,
        last_run_trigger=None,
        last_run_at=None,
    )
    return dataclasses.replace(base, **facts)  # type: ignore[arg-type]


def test_jobs_sort_by_each_field_with_the_unknown_last() -> None:
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    beta = _job_view("Beta", last_run_status="succeeded", last_run_at=t0, next_run_at=t0)
    alpha = _job_view("alpha", last_run_status="failed", last_run_at=t0 + timedelta(hours=1))
    gamma = _job_view("gamma", next_run_at=t0 - timedelta(hours=1))
    other = _job_view("alpha", "ml", last_run_status="failed", last_run_at=t0)
    jobs = [gamma, beta, other, alpha]

    def names(sort: str | None, order: str | None = None) -> list[tuple[str, str]]:
        """Sort the jobs and name them.

        Parameters
        ----------
        sort
            The field.
        order
            The direction.

        Returns
        -------
        list of tuple of (str, str)
            Each job's name and repository, in order.
        """
        return [(v.name, v.repository) for v in sorted_jobs(jobs, sort, order)]

    assert names(None) == [(v.name, v.repository) for v in jobs]
    assert names("name") == [
        ("alpha", "analytics"),
        ("alpha", "ml"),
        ("Beta", "analytics"),
        ("gamma", "analytics"),
    ]
    assert names("name", "desc") == [
        ("gamma", "analytics"),
        ("Beta", "analytics"),
        ("alpha", "analytics"),
        ("alpha", "ml"),
    ]
    assert names("status") == [
        ("alpha", "analytics"),
        ("alpha", "ml"),
        ("Beta", "analytics"),
        ("gamma", "analytics"),
    ]
    assert names("status", "desc") == [
        ("Beta", "analytics"),
        ("alpha", "analytics"),
        ("alpha", "ml"),
        ("gamma", "analytics"),
    ]
    assert names("last_run") == [
        ("alpha", "analytics"),
        ("alpha", "ml"),
        ("Beta", "analytics"),
        ("gamma", "analytics"),
    ]
    assert names("last_run", "asc") == [
        ("alpha", "ml"),
        ("Beta", "analytics"),
        ("alpha", "analytics"),
        ("gamma", "analytics"),
    ]
    assert names("next_run") == [
        ("gamma", "analytics"),
        ("Beta", "analytics"),
        ("alpha", "analytics"),
        ("alpha", "ml"),
    ]
    with pytest.raises(
        ValidationError,
        match="`sort` must be one of: environment, last_run, name, next_run, repository, status",
    ):
        sorted_jobs(jobs, "trigger", None)


def test_jobs_sort_by_repository_and_environment_and_not_by_schedule() -> None:
    nightly = _job_view("nightly", "Shop", cron="0 2 * * *", timezone="UTC", environment="prod")
    hourly = _job_view("hourly", "analytics", cron="0 * * * *", timezone="Europe/Berlin")
    by_hand = _job_view("backfill", "ml", environment="Dev2")
    jobs = [nightly, hourly, by_hand]

    def names(sort: str, order: str | None = None) -> list[str]:
        """Sort the jobs and name them.

        Parameters
        ----------
        sort
            The field.
        order
            The direction.

        Returns
        -------
        list of str
            Each job's name, in order.
        """
        return [v.name for v in sorted_jobs(jobs, sort, order)]

    assert names("repository") == ["hourly", "backfill", "nightly"]
    assert names("repository", "desc") == ["nightly", "backfill", "hourly"]
    assert names("environment") == ["hourly", "backfill", "nightly"]
    # Cron text has no order that means something: the schedule does not sort.
    with pytest.raises(ValidationError, match="got 'schedule'"):
        sorted_jobs(jobs, "schedule", None)


def test_list_takes_the_sort(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    service = _service(session)
    assert [v.name for v in service.list(principal, "dev", sort="name", order="desc")] == sorted(
        (v.name for v in service.list(principal, "dev")), key=str.casefold, reverse=True
    )


def test_a_waiting_job_says_since_when_and_how_it_was_first_asked_for(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A later request merges into the waiting start and keeps its first time and trigger (#786)."""
    service = _service(session)
    first = service.start(principal, "dev", "analytics", "manual").run
    assert first is not None
    asked = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    _wait_with(session, "daily", trigger="api", requested_at=asked.isoformat())
    service.start(principal, "dev", "analytics", "daily")
    daily = service.get(principal, "dev", "daily")
    assert (daily.state, daily.waits_for_run_id) == ("waiting", first.id)
    assert (daily.waiting_since, daily.waiting_trigger) == (asked, "api")
    row = _row(session, "daily").pending_start
    assert row is not None and row["merged"] == 2 and row["trigger"] == "manual"


def test_several_jobs_wait_for_one_run_each_with_its_own_request(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A run of no job holds the project; both jobs wait for it, each with its own request."""
    runs = RunService(session, AllowAllAuthorization(), caps=RunCaps())
    blocker = runs.start(principal, "dev", "analytics", Selection())
    service = _service(session)
    assert service.start(principal, "dev", "analytics", "daily").waiting is not None
    _wait_with(
        session,
        "manual",
        trigger="api",
        requested_at=datetime(2026, 10, 15, 4, 0, tzinfo=UTC).isoformat(),
    )
    views = {v.name: v for v in service.list(principal, "dev")}
    assert {name: (v.state, v.waits_for_run_id) for name, v in views.items()} == {
        "daily": ("waiting", blocker.id),
        "manual": ("waiting", blocker.id),
    }
    assert views["daily"].waiting_trigger == "manual"
    assert views["daily"].waiting_since is not None
    assert (views["manual"].waiting_since, views["manual"].waiting_trigger) == (
        datetime(2026, 10, 15, 4, 0, tzinfo=UTC),
        "api",
    )


def test_a_schedule_that_merges_into_a_start_by_hand_keeps_the_first_request(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The run that begins is the scheduled one, but the start was first asked for by hand.

    The view names the first request: its time and its trigger, never the schedule's trigger
    with the time of the start by hand.
    """
    account = _account(session)
    _bind(session, account.id)
    service = _service(session)
    service.start(principal, "dev", "analytics", "manual")
    service.start(principal, "dev", "analytics", "daily")
    asked = _row(session, "daily").pending_start
    assert asked is not None
    at = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    service.start(None, "dev", "analytics", "daily", trigger=RunTrigger.SCHEDULE, scheduled_for=at)
    daily = service.get(principal, "dev", "daily")
    assert daily.waiting_since == datetime.fromisoformat(asked["requested_at"])
    assert daily.waiting_trigger == "manual"
    row = _row(session, "daily").pending_start
    assert row is not None and row["trigger"] == "schedule"


def test_an_older_merged_start_without_its_first_trigger_shows_none(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A start stored before #786 that merged several requests cannot tell which came first."""
    _wait_with(session, "daily", trigger="schedule", merged=2)
    daily = _service(session).get(principal, "dev", "daily")
    assert daily.waiting_since is not None and daily.waiting_trigger is None
    _wait_with(session, "daily", trigger="schedule", merged=1)
    assert _service(session).get(principal, "dev", "daily").waiting_trigger == "schedule"
    # A count that cannot be read says nothing of how many merged, so nothing of which came first.
    _wait_with(session, "daily", trigger="schedule", merged="several")
    damaged = _service(session).get(principal, "dev", "daily")
    assert (damaged.state, damaged.waiting_trigger) == ("waiting to start", None)


def test_a_waiting_job_keeps_its_request_once_its_run_has_ended(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """After the blocking run ends the start still waits for the scheduler, with its request."""
    _wait_with(session, "daily", trigger="schedule", requested_by=None)
    daily = _service(session).get(principal, "dev", "daily")
    assert (daily.state, daily.waits_for_run_id) == ("waiting to start", None)
    assert daily.waiting_since == datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
    assert daily.waiting_trigger == "schedule"


def test_a_job_that_never_ran_nor_waits_has_no_request(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A job never run shows no last run and no waiting start."""
    manual = _service(session).get(principal, "dev", "manual")
    assert (manual.last_run_id, manual.last_run_status) == (None, None)
    assert (manual.waiting_since, manual.waiting_trigger, manual.waits_for_run_id) == (
        None,
        None,
        None,
    )


def test_a_damaged_waiting_start_shows_no_time_rather_than_failing(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A stored start whose time or trigger cannot be read still lists the job, without them."""
    _wait_with(session, "daily", requested_at="not a time", trigger=["manual"])
    daily = _service(session).get(principal, "dev", "daily")
    assert daily.state == "waiting to start"
    assert (daily.waiting_since, daily.waiting_trigger) == (None, None)


def test_a_reader_sees_the_waiting_start_but_not_who_asked(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """READ_ENVIRONMENT alone shows the start's time and trigger, never the requester."""
    _wait_with(session, "daily", requested_by=str(principal.id))
    reader = _service(session, ActionAllowList(Action.READ_ENVIRONMENT))
    daily = reader.get(principal, "dev", "daily")
    assert daily.waiting_trigger == "manual"
    fields = {f.name for f in dataclasses.fields(daily)}
    assert not {"requested_by", "waiting_requested_by"} & fields


def test_cancelling_the_queued_run_a_job_waits_for_leaves_it_about_to_start(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """A queued run another job waits for is cancelled: that job then waits for the scheduler.

    Its request is kept; the cancelled job shows its run as cancelled, and once the waiting start
    has begun, the job no longer waits.
    """
    service = _service(session)
    first = service.start(principal, "dev", "analytics", "manual").run
    assert first is not None and first.status == "queued"
    service.start(principal, "dev", "analytics", "daily")
    RunService(session, AllowAllAuthorization(), caps=RunCaps()).cancel(principal, "dev", first.id)
    # Asked to cancel, the run holds the project until the scheduler has ended it.
    asked = service.get(principal, "dev", "daily")
    assert (asked.state, asked.waits_for_run_id) == ("waiting", first.id)
    session.execute(update(Run).values(status=RunStatus.CANCELLED))
    session.commit()
    views = {v.name: v for v in service.list(principal, "dev")}
    daily = views["daily"]
    assert (daily.state, daily.waits_for_run_id) == ("waiting to start", None)
    assert daily.waiting_since == asked.waiting_since and daily.waiting_trigger == "manual"
    assert views["manual"].last_run_status == "cancelled"
    assert views["manual"].waiting_since is None
    started = JobService.start_waiting(session, _row(session, "daily"), caps=RunCaps())
    assert started is not None
    daily = service.get(principal, "dev", "daily")
    assert (daily.waiting_since, daily.waiting_trigger, daily.waits_for_run_id) == (None,) * 3
    assert daily.state == "no run principal"
    assert (daily.last_run_id, daily.last_run_status) == (started.id, "queued")


def test_a_paused_job_names_who_paused_it_and_since_when(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """The pause's principal by name and its time, in the answer and the list; gone on resume."""
    service = _service(session)
    paused = service.pause(principal, "dev", "analytics", "daily")
    row = _row(session, "daily")
    assert (paused.paused_by, paused.paused_at) == (principal.name, row.paused_at)
    listed = {v.name: v for v in service.list(principal, "dev")}
    assert (listed["daily"].paused_by, listed["daily"].paused_at) == (principal.name, row.paused_at)
    assert (listed["manual"].paused_by, listed["manual"].paused_at) == (None, None)
    resumed = service.resume(principal, "dev", "analytics", "daily")
    assert (resumed.paused_by, resumed.paused_at) == (None, None)


def test_a_pause_whose_principal_was_deleted_keeps_its_time_without_a_name(
    session: Session, deployed: Repository, principal: PrincipalRef
) -> None:
    """Deleting the principal sets the row's `paused_by` to NULL; the pause and its time stay."""
    service = _service(session)
    pauser = _person(session, "pauser", admin=True)
    service.pause(pauser, "dev", "analytics", "daily")
    assert _row(session, "daily").paused_by == pauser.id
    session.execute(delete(Principal).where(Principal.id == pauser.id))
    session.commit()
    session.expire_all()
    assert session.get(Principal, pauser.id) is None
    assert _row(session, "daily").paused_by is None
    daily = {v.name: v for v in service.list(principal, "dev")}["daily"]
    assert daily.paused and daily.paused_by is None
    assert daily.paused_at == _row(session, "daily").paused_at
