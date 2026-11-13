"""The scheduler fires due jobs and starts waiting starts."""

from __future__ import annotations

import itertools
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_jobs_service import (
    _account,
    _bind,
    _grant_admin,
    _person,
    _row,
    _service,
    _wait_with,
    deployed,  # ruff: ignore[unused-import] -- fixture
)
from test_projects_deploys import _deployed_jobs, _job, _repo

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.errors import ConflictError, NotFoundError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.jobs import scheduler as scheduler_module
from lakana_core.jobs.scheduler import (
    TICK_BUDGET_SECONDS,
    FireOutcome,
    fire_due,
    start_waiting_jobs,
    sweep_job_fires,
)
from lakana_core.jobs.service import JobService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    EnvironmentDeployment,
    JobFire,
    JobSchedule,
    JobVersion,
    Principal,
    Repository,
    Run,
    RunStatus,
    RunTrigger,
)
from lakana_core.projects import deploys as deploys_module
from lakana_core.projects.deploys import DeploymentMoved, DeployService
from lakana_core.projects.run_policy import RunCaps

NOW = datetime(2026, 10, 15, 3, 0, 30, tzinfo=UTC)
FIRE = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
TOMORROW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)


def _due(session: Session, job: str = "daily", at: datetime = FIRE) -> None:
    """Make a job's schedule due at a time.

    Parameters
    ----------
    session
        Database session.
    job
        The job.
    at
        Its `next_fire_at`.
    """
    session.execute(update(JobSchedule).where(JobSchedule.job == job).values(next_fire_at=at))
    session.commit()


def _dropped(session: Session) -> list[AuditEvent]:
    """Read the `JOB_START_DROPPED` events, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    session.expire_all()
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.JOB_START_DROPPED)
            .order_by(AuditEvent.occurred_at)
        )
    )


def _far(session: Session) -> None:
    """Move every schedule's next time far away, so that only the test's own times are due.

    Parameters
    ----------
    session
        Database session.
    """
    session.execute(update(JobSchedule).values(next_fire_at=datetime(2030, 1, 1, tzinfo=UTC)))
    session.commit()


def _end_runs(session: Session) -> None:
    """End every run, so that the project is free.

    Parameters
    ----------
    session
        Database session.
    """
    session.execute(update(Run).values(status=RunStatus.SUCCEEDED))
    session.commit()


@pytest.fixture
def two_scheduled(session: Session, tmp_path: Path, principal: PrincipalRef) -> Repository:
    """Deploy `analytics` with two scheduled jobs, `daily` and `nightly`.

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
        namespaces=[NamespacePath.from_parts(["bronze"])],
        tables=[TableRef("dev", NamespacePath.from_parts(["bronze"]), "events")],
    )
    repo = _repo(session)
    _deployed_jobs(session, principal, repo, "a" * 40, {"daily": _job(), "nightly": _job()})
    _grant_admin(session, principal.id)
    return repo


@pytest.mark.usefixtures("deployed")
def test_a_due_job_starts_a_scheduled_run_and_moves_to_the_next_time(session: Session) -> None:
    """The run carries the trigger and the scheduled time; the fire is booked and kept."""
    account = _account(session)
    _bind(session, account.id)
    _due(session)
    (fire,) = fire_due(session, NOW)
    assert (fire.job, fire.outcome, fire.scheduled_for, fire.missed) == (
        "daily",
        FireOutcome.STARTED,
        FIRE,
        0,
    )
    run = session.scalars(select(Run)).one()
    assert (run.trigger, run.scheduled_for, run.missed, run.job) == (
        RunTrigger.SCHEDULE,
        FIRE,
        0,
        "daily",
    )
    row = _row(session, "daily")
    assert (row.next_fire_at, row.last_fired_for) == (TOMORROW, FIRE)
    booked = session.scalars(select(JobFire)).one()
    assert (booked.scheduled_for, booked.run_id) == (FIRE, run.id)
    assert fire_due(session, NOW) == []


@pytest.mark.usefixtures("deployed")
def test_after_downtime_one_run_starts_for_the_latest_time_and_counts_the_rest(
    session: Session,
) -> None:
    """Catch-up is off: three missed days are one run with `missed` 3... and the latest time."""
    _bind(session, _account(session).id)
    _due(session, at=datetime(2026, 10, 12, 3, 0, tzinfo=UTC))
    (fire,) = fire_due(session, NOW)
    assert (fire.scheduled_for, fire.missed) == (FIRE, 3)
    run = session.scalars(select(Run)).one()
    assert (run.scheduled_for, run.missed) == (FIRE, 3)
    assert _row(session, "daily").next_fire_at == TOMORROW


@pytest.mark.usefixtures("deployed")
def test_a_job_that_is_not_due_or_is_paused_does_not_fire(session: Session) -> None:
    """A paused job keeps its `next_fire_at`; a future one waits."""
    _bind(session, _account(session).id)
    _far(session)
    assert fire_due(session, NOW - timedelta(days=1)) == []
    _due(session)
    session.execute(update(JobSchedule).where(JobSchedule.job == "daily").values(paused=True))
    session.commit()
    assert fire_due(session, NOW) == []
    assert _row(session, "daily").next_fire_at == FIRE
    assert session.scalars(select(Run)).all() == []


@pytest.mark.usefixtures("deployed")
def test_a_busy_project_parks_the_fire_and_it_starts_when_the_run_ends(
    session: Session, principal: PrincipalRef
) -> None:
    """The fire is consumed, waits, and the waiting-start step runs it once the project is free."""
    _bind(session, _account(session).id)
    assert _service(session).start(principal, "dev", "analytics", "manual").run is not None
    _due(session)
    (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.WAITING and fire.run_id is None
    assert _row(session, "daily").pending_start is not None
    assert _row(session, "daily").next_fire_at == TOMORROW
    assert start_waiting_jobs(session) == []
    _end_runs(session)
    (started,) = start_waiting_jobs(session)
    assert (started.job, started.trigger, started.scheduled_for) == (
        "daily",
        RunTrigger.SCHEDULE,
        FIRE,
    )
    assert _row(session, "daily").pending_start is None


@pytest.mark.usefixtures("deployed")
def test_without_a_run_principal_the_time_is_consumed_and_binding_later_gives_no_burst(
    session: Session,
) -> None:
    """Nothing starts, the state says why, and only the next time fires after a binding."""
    _far(session)
    _due(session)
    (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.NO_RUN_PRINCIPAL and fire.run_id is None
    assert session.scalars(select(Run)).all() == []
    row = _row(session, "daily")
    assert (row.next_fire_at, row.last_fired_for) == (TOMORROW, FIRE)
    assert session.scalars(select(JobFire)).one().run_id is None
    _bind(session, _account(session).id)
    assert fire_due(session, NOW) == []
    assert session.scalars(select(Run)).all() == []
    (next_fire,) = fire_due(session, TOMORROW + timedelta(seconds=5))
    assert (next_fire.outcome, next_fire.scheduled_for) == (FireOutcome.STARTED, TOMORROW)


@pytest.mark.usefixtures("deployed")
def test_a_job_removed_from_the_project_version_does_not_fire(session: Session) -> None:
    """The manifest is read under the lock; a removed job's row is left alone."""
    _bind(session, _account(session).id)
    version = session.scalars(select(JobVersion)).one()
    manifest = {**version.manifest_json}
    manifest["jobs"] = {"manual": manifest["jobs"]["manual"]}
    version.manifest_json = manifest
    session.commit()
    _due(session)
    assert fire_due(session, NOW) == []
    assert session.scalars(select(Run)).all() == []
    assert session.scalars(select(JobFire)).all() == []


@pytest.mark.usefixtures("deployed")
def test_a_job_that_lost_its_schedule_does_not_fire(session: Session) -> None:
    """A stale due row of a job whose manifest entry has no schedule is not fired."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session, "manual")
    assert fire_due(session, NOW) == []
    assert session.scalars(select(JobFire)).all() == []


@pytest.mark.usefixtures("deployed")
def test_a_time_that_is_not_a_fire_time_is_moved_on_without_a_run(session: Session) -> None:
    """A `next_fire_at` between fire times (a stale row) is replaced by the next real one."""
    _bind(session, _account(session).id)
    _due(session, at=datetime(2026, 10, 15, 3, 0, 10, tzinfo=UTC))
    assert fire_due(session, NOW) == []
    assert _row(session, "daily").next_fire_at == TOMORROW
    assert session.scalars(select(Run)).all() == []
    assert session.scalars(select(JobFire)).all() == []


def test_one_failing_schedule_does_not_stop_the_others(
    session: Session,
    two_scheduled: Repository,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The error is logged with the job's name, and the other job still fires."""
    _bind(session, _account(session).id)
    _due(session, "daily")
    _due(session, "nightly")
    real = JobService.start

    def start(self: JobService, p: object, env: str, repo: str, job: str, **kw: object) -> object:
        if job == "daily":
            raise RuntimeError("boom")
        return real(self, p, env, repo, job, **kw)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(JobService, "start", start)
    with caplog.at_level(logging.ERROR):
        fires = fire_due(session, NOW)
    assert {f.job: f.outcome for f in fires} == {
        "nightly": FireOutcome.STARTED,
        "daily": FireOutcome.NOT_STARTED,
    }
    assert "daily" in caplog.text and "boom" in caplog.text
    assert _dropped(session)[0].detail["reason"] == "error"
    assert _row(session, "daily").next_fire_at == TOMORROW


def test_a_failed_booking_is_logged_and_the_others_still_fire(
    session: Session,
    two_scheduled: Repository,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An error while booking rolls back that schedule only."""
    _bind(session, _account(session).id)
    _due(session, "daily")
    _due(session, "nightly")
    real = JobService.book_fire.__func__

    def book(cls: type[JobService], s: Session, schedule_id: uuid.UUID, now: datetime) -> object:
        schedule = s.get(JobSchedule, schedule_id)
        assert schedule is not None
        if schedule.job == "daily":
            raise RuntimeError("no booking")
        return real(cls, s, schedule_id, now)

    monkeypatch.setattr(JobService, "book_fire", classmethod(book))
    with caplog.at_level(logging.ERROR):
        fires = fire_due(session, NOW)
    assert [f.job for f in fires] == ["nightly"]
    assert "no booking" in caplog.text
    assert _row(session, "daily").next_fire_at == FIRE


def test_the_time_budget_stops_the_step_between_schedules(
    session: Session, two_scheduled: Repository
) -> None:
    """When the budget is spent, the rest keep their due time for the next tick."""
    _bind(session, _account(session).id)
    _due(session, "daily", at=datetime(2026, 10, 14, 3, 0, tzinfo=UTC))
    _due(session, "nightly")
    ticks = itertools.chain([0.0, 0.0], itertools.repeat(100.0))
    fires = fire_due(session, NOW, time_limit=5.0, monotonic=lambda: next(ticks))
    assert [f.job for f in fires] == ["daily"]
    assert (
        _row(session, "nightly").next_fire_at == FIRE and not _row(session, "daily").pending_start
    )


def test_waiting_starts_of_one_project_start_one_at_a_time_oldest_first(
    session: Session, two_scheduled: Repository, principal: PrincipalRef
) -> None:
    """The second keeps waiting while the first runs."""
    _wait_with(
        session, "nightly", requested_by=str(principal.id), requested_at="2026-10-15T03:00:02+00:00"
    )
    _wait_with(
        session, "daily", requested_by=str(principal.id), requested_at="2026-10-15T03:00:09+00:00"
    )
    (first,) = start_waiting_jobs(session)
    assert first.job == "nightly"
    assert _row(session, "daily").pending_start is not None
    assert start_waiting_jobs(session) == []
    _end_runs(session)
    (second,) = start_waiting_jobs(session)
    assert second.job == "daily"


def test_a_dropped_start_does_not_block_the_next_one(
    session: Session, two_scheduled: Repository, principal: PrincipalRef
) -> None:
    """The oldest start is dropped (its requester lost `RUN`); the next starts in the same step."""
    plain = _person(session, "plain")
    _wait_with(
        session, "nightly", requested_by=str(plain.id), requested_at="2026-10-15T03:00:02+00:00"
    )
    _wait_with(
        session, "daily", requested_by=str(principal.id), requested_at="2026-10-15T03:00:09+00:00"
    )
    (started,) = start_waiting_jobs(session)
    assert started.job == "daily"
    assert _row(session, "nightly").pending_start is None


def test_a_stuck_start_is_retried_next_time_and_does_not_block_the_next_one(
    session: Session,
    two_scheduled: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `ConflictError` leaves the start waiting; the next job's start goes ahead."""
    _wait_with(
        session, "nightly", requested_by=str(principal.id), requested_at="2026-10-15T03:00:02+00:00"
    )
    _wait_with(
        session, "daily", requested_by=str(principal.id), requested_at="2026-10-15T03:00:09+00:00"
    )
    real = JobService.start_waiting.__func__
    stuck = [True]

    def start_waiting(
        cls: type[JobService],
        s: Session,
        schedule: JobSchedule,
        caps: RunCaps | None = None,
        lease_seconds: int = 60,
    ) -> Run | None:
        if schedule.job == "nightly" and stuck:
            s.rollback()
            raise ConflictError("the principal changed")
        return real(cls, s, schedule, caps=caps, lease_seconds=lease_seconds)

    monkeypatch.setattr(JobService, "start_waiting", classmethod(start_waiting))
    (started,) = start_waiting_jobs(session)
    assert started.job == "daily"
    assert _row(session, "nightly").pending_start is not None
    stuck.clear()
    _end_runs(session)
    (again,) = start_waiting_jobs(session)
    assert again.job == "nightly"


def test_an_error_in_one_waiting_start_is_logged_and_the_next_one_starts(
    session: Session,
    two_scheduled: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any other error is logged with the job's name; the project's next start still runs."""
    _wait_with(
        session, "nightly", requested_by=str(principal.id), requested_at="2026-10-15T03:00:02+00:00"
    )
    _wait_with(
        session, "daily", requested_by=str(principal.id), requested_at="2026-10-15T03:00:09+00:00"
    )
    real = JobService.start_waiting.__func__

    def start_waiting(
        cls: type[JobService],
        s: Session,
        schedule: JobSchedule,
        caps: RunCaps | None = None,
        lease_seconds: int = 60,
    ) -> Run | None:
        if schedule.job == "nightly":
            raise RuntimeError("broken start")
        return real(cls, s, schedule, caps=caps, lease_seconds=lease_seconds)

    monkeypatch.setattr(JobService, "start_waiting", classmethod(start_waiting))
    with caplog.at_level(logging.ERROR):
        (started,) = start_waiting_jobs(session)
    assert started.job == "daily"
    assert "nightly" in caplog.text and "broken start" in caplog.text


def test_waiting_starts_stop_at_the_time_budget(
    session: Session, two_scheduled: Repository, principal: PrincipalRef
) -> None:
    """With no time left, no waiting start is looked at."""
    _wait_with(session, "daily", requested_by=str(principal.id))
    ticks = itertools.chain([0.0], itertools.repeat(100.0))
    assert start_waiting_jobs(session, time_limit=5.0, monotonic=lambda: next(ticks)) == []
    assert _row(session, "daily").pending_start is not None


def test_a_busy_project_is_skipped_by_the_waiting_start_step(
    session: Session, two_scheduled: Repository, principal: PrincipalRef
) -> None:
    """No audit and no change while a run is active."""
    assert _service(session).start(principal, "dev", "analytics", "daily").run is not None
    _wait_with(session, "nightly", requested_by=str(principal.id))
    assert start_waiting_jobs(session) == []
    assert _row(session, "nightly").pending_start is not None
    dropped = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.JOB_START_DROPPED)
    ).all()
    assert dropped == []


@pytest.mark.usefixtures("deployed")
def test_the_sweep_deletes_fires_older_than_the_last_fire_of_their_schedule(
    session: Session,
) -> None:
    """The latest fire of each schedule stays: it is the one a race would collide with."""
    other = _row(session, "manual")
    schedule = _row(session, "daily")
    schedule.last_fired_for = FIRE
    older = FIRE - timedelta(days=2)
    session.add_all(
        [
            JobFire(id=uuid.uuid4(), job_schedule_id=schedule.id, scheduled_for=older),
            JobFire(id=uuid.uuid4(), job_schedule_id=schedule.id, scheduled_for=FIRE),
        ]
    )
    session.add(JobFire(id=uuid.uuid4(), job_schedule_id=other.id, scheduled_for=older))
    session.commit()
    assert sweep_job_fires(session) == 1
    left = session.scalars(select(JobFire)).all()
    assert sorted(f.scheduled_for for f in left) == [older, FIRE]
    assert sweep_job_fires(session) == 0


@pytest.mark.parametrize("error", [ValidationError("matches nothing"), NotFoundError("gone")])
@pytest.mark.usefixtures("deployed")
def test_a_start_that_is_refused_after_booking_is_logged_and_the_time_stays_consumed(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    """The selection matched nothing or a deploy removed the job: nothing starts or repeats."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session)

    def refuse(self: JobService, *args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(JobService, "start", refuse)
    with caplog.at_level(logging.WARNING):
        (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.NOT_STARTED and fire.run_id is None
    assert str(error) in caplog.text
    assert _row(session, "daily").next_fire_at == TOMORROW
    assert fire_due(session, NOW) == []
    (event,) = _dropped(session)
    reason = "selection_invalid" if isinstance(error, ValidationError) else "job_removed"
    assert event.principal_id is None
    assert (event.detail["reason"], event.detail["trigger"]) == (reason, "schedule")
    assert (event.detail["job"], event.detail["scheduled_for"]) == ("daily", FIRE.isoformat())


@pytest.mark.usefixtures("deployed")
def test_an_inactive_run_principal_consumes_the_time_and_is_not_called_unbound(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal names the principal; the outcome is not `no_run_principal`."""
    account = _account(session)
    _bind(session, account.id)
    session.execute(update(Principal).where(Principal.id == account.id).values(is_active=False))
    session.commit()
    _far(session)
    _due(session)
    with caplog.at_level(logging.WARNING):
        (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.NOT_STARTED
    assert account.name in caplog.text
    assert session.scalars(select(Run)).all() == []
    (event,) = _dropped(session)
    assert event.principal_id is None and event.detail["reason"] == "run_principal_unusable"


@pytest.mark.usefixtures("deployed")
def test_a_time_another_leader_booked_is_not_started_again_and_the_schedule_moves_on(
    session: Session,
) -> None:
    """The unique key on the fire refuses the second booking; `next_fire_at` still advances."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session)
    session.add(
        JobFire(id=uuid.uuid4(), job_schedule_id=_row(session, "daily").id, scheduled_for=FIRE)
    )
    session.commit()
    assert fire_due(session, NOW) == []
    assert session.scalars(select(Run)).all() == []
    assert len(session.scalars(select(JobFire)).all()) == 1
    row = _row(session, "daily")
    assert (row.next_fire_at, row.last_fired_for) == (TOMORROW, FIRE)


@pytest.mark.usefixtures("deployed")
def test_booking_a_schedule_that_is_gone_does_nothing(session: Session) -> None:
    """A row a deploy deleted between the listing and the booking is skipped."""
    assert JobService.book_fire(session, uuid.uuid4(), NOW) is None


def test_a_project_whose_waiting_starts_are_all_dropped_starts_nothing(
    session: Session, two_scheduled: Repository
) -> None:
    """Every candidate is looked at and dropped; the step ends without a run."""
    plain = _person(session, "plain")
    _wait_with(session, "nightly", requested_by=str(plain.id))
    _wait_with(session, "daily", requested_by=str(plain.id))
    assert start_waiting_jobs(session) == []
    assert _row(session, "daily").pending_start is None
    assert _row(session, "nightly").pending_start is None


def test_a_waiting_start_whose_row_a_deploy_deleted_meanwhile_is_skipped(
    session: Session,
    two_scheduled: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is gone when the step comes to it; the next start of the project goes ahead."""
    _wait_with(
        session, "nightly", requested_by=str(principal.id), requested_at="2026-10-15T03:00:02+00:00"
    )
    _wait_with(
        session, "daily", requested_by=str(principal.id), requested_at="2026-10-15T03:00:09+00:00"
    )
    doomed = _row(session, "nightly").id
    real = scheduler_module._expired
    deleted: list[bool] = []

    def expired(monotonic: object, deadline: float) -> bool:
        if not deleted:
            deleted.append(True)
            session.execute(delete(JobSchedule).where(JobSchedule.id == doomed))
            session.commit()
        return real(monotonic, deadline)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(scheduler_module, "_expired", expired)
    (started,) = start_waiting_jobs(session)
    assert started.job == "daily"


@pytest.mark.usefixtures("deployed")
def test_a_selection_that_matches_nothing_is_audited_when_the_fire_cannot_start(
    session: Session,
) -> None:
    """The real refusal of the start, not a stand-in: the audit says `selection_invalid`."""
    _bind(session, _account(session).id)
    _far(session)
    version = session.scalars(select(JobVersion)).one()
    manifest = {**version.manifest_json}
    manifest["jobs"] = {
        **manifest["jobs"],
        "daily": {**manifest["jobs"]["daily"], "select": ["nothing.*"]},
    }
    version.manifest_json = manifest
    session.commit()
    _due(session)
    (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.NOT_STARTED
    (event,) = _dropped(session)
    assert event.detail["reason"] == "selection_invalid"


@pytest.mark.usefixtures("deployed")
def test_a_time_without_a_run_principal_is_not_audited_the_list_shows_it(
    session: Session,
) -> None:
    """An unbound deployment gives `no_run_principal` and no event on every fire."""
    _far(session)
    _due(session)
    (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.NO_RUN_PRINCIPAL
    assert _dropped(session) == []


@pytest.mark.usefixtures("deployed")
def test_a_binding_cleared_while_the_start_ran_is_reported_as_no_run_principal(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The binding is read after the lock: a cleared one is not an unusable one."""
    account = _account(session)
    _bind(session, account.id)
    session.execute(update(Principal).where(Principal.id == account.id).values(is_active=False))
    session.commit()
    _wait_with(session, "daily", trigger="schedule", scheduled_for=FIRE.isoformat())
    real = DeployService.lock_deployment

    def lock(self: DeployService, *args: object, **kwargs: object) -> None:
        real(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]
        self._session.execute(
            update(EnvironmentDeployment)
            .values(run_as_principal_id=None)
            .execution_options(synchronize_session=False)
        )

    monkeypatch.setattr(DeployService, "lock_deployment", lock)
    assert JobService.start_waiting(session, _row(session, "daily")) is None
    (event,) = _dropped(session)
    assert event.detail["reason"] == "no_run_principal"


@pytest.mark.usefixtures("deployed")
def test_a_job_paused_after_the_listing_is_not_booked(session: Session) -> None:
    """The pause is checked again under the lock; the time stays due."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session)
    schedule_id = _row(session, "daily").id
    session.execute(update(JobSchedule).where(JobSchedule.id == schedule_id).values(paused=True))
    session.commit()
    assert JobService.book_fire(session, schedule_id, NOW) is None
    assert session.scalars(select(JobFire)).all() == []
    assert _row(session, "daily").next_fire_at == FIRE


@pytest.mark.usefixtures("deployed")
def test_a_resumed_job_fires_once_with_nothing_missed(session: Session) -> None:
    """The window starts at `next_fire_at`: days of `last_fired_for` before it are not counted."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session)
    session.execute(
        update(JobSchedule)
        .where(JobSchedule.job == "daily")
        .values(last_fired_for=FIRE - timedelta(days=5))
    )
    session.commit()
    (fire,) = fire_due(session, NOW)
    assert (fire.scheduled_for, fire.missed) == (FIRE, 0)


@pytest.mark.usefixtures("deployed")
def test_a_parked_fire_that_runs_later_is_linked_to_its_run(
    session: Session, principal: PrincipalRef
) -> None:
    """`job_fires.run_id` is set when the waiting start begins, so null means no run so far."""
    _bind(session, _account(session).id)
    assert _service(session).start(principal, "dev", "analytics", "manual").run is not None
    _far(session)
    _due(session)
    (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.WAITING
    assert session.scalars(select(JobFire)).one().run_id is None
    _end_runs(session)
    (started,) = start_waiting_jobs(session)
    session.expire_all()
    assert session.scalars(select(JobFire)).one().run_id == started.id


def test_oldest_first_compares_times_not_text(
    session: Session, two_scheduled: Repository, principal: PrincipalRef
) -> None:
    """A request made at 03:00 UTC written with an offset sorts before 03:00:09 UTC."""
    _wait_with(
        session, "daily", requested_by=str(principal.id), requested_at="2026-10-15T03:00:09+00:00"
    )
    _wait_with(
        session, "nightly", requested_by=str(principal.id), requested_at="2026-10-15T05:00:00+02:00"
    )
    (first,) = start_waiting_jobs(session)
    assert first.job == "nightly"


def _lock_timeout() -> OperationalError:
    """Build the error Postgres raises when a lock wait gives up.

    Returns
    -------
    OperationalError
        The error.
    """
    return OperationalError(
        "UPDATE x", {}, Exception("canceling statement due to lock timeout 55P03")
    )


@pytest.mark.usefixtures("deployed")
def test_a_lock_timeout_while_booking_leaves_the_time_due_and_logs_a_warning_once(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing is consumed; the next tick tries again; the log is not repeated every second."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session)

    def timeout(cls: type[JobService], s: Session, schedule_id: uuid.UUID, now: datetime) -> None:
        raise _lock_timeout()

    monkeypatch.setattr(JobService, "book_fire", classmethod(timeout))
    clock = itertools.count(0.0, 1.0)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert fire_due(session, NOW, monotonic=lambda: next(clock)) == []
    assert _row(session, "daily").next_fire_at == FIRE
    warnings = [r for r in caplog.records if "lock" in r.getMessage().lower()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert not [r for r in caplog.records if r.exc_info]


@pytest.mark.usefixtures("deployed")
def test_a_lock_timeout_in_the_start_after_booking_is_audited_as_an_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The time is consumed, so the audit says the start failed."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session)

    def timeout(self: JobService, *args: object, **kwargs: object) -> object:
        raise _lock_timeout()

    monkeypatch.setattr(JobService, "start", timeout)
    (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.NOT_STARTED
    (event,) = _dropped(session)
    assert event.detail["reason"] == "error"


def test_a_lock_timeout_in_a_waiting_start_keeps_the_start_and_leaves_the_project_to_the_next_tick(
    session: Session,
    two_scheduled: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The start stays waiting, one warning is logged, and its project is left for the next tick."""
    _wait_with(
        session, "nightly", requested_by=str(principal.id), requested_at="2026-10-15T03:00:02+00:00"
    )
    _wait_with(
        session, "daily", requested_by=str(principal.id), requested_at="2026-10-15T03:00:09+00:00"
    )
    real = JobService.start_waiting.__func__

    def start_waiting(
        cls: type[JobService],
        s: Session,
        schedule: JobSchedule,
        caps: RunCaps | None = None,
        lease_seconds: int = 60,
    ) -> Run | None:
        if schedule.job == "nightly":
            raise _lock_timeout()
        return real(cls, s, schedule, caps=caps, lease_seconds=lease_seconds)

    monkeypatch.setattr(JobService, "start_waiting", classmethod(start_waiting))
    with caplog.at_level(logging.WARNING):
        assert start_waiting_jobs(session) == []
    assert _row(session, "nightly").pending_start is not None
    assert _row(session, "daily").pending_start is not None
    assert not [r for r in caplog.records if r.exc_info]


def test_a_failing_booking_logs_its_traceback_once_a_minute(
    session: Session,
    two_scheduled: Repository,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A schedule that fails on every tick fills the log once, not once a second."""
    _far(session)
    _due(session, "daily")

    def broken(cls: type[JobService], s: Session, schedule_id: uuid.UUID, now: datetime) -> None:
        raise RuntimeError("zone vanished")

    monkeypatch.setattr(JobService, "book_fire", classmethod(broken))
    now = [1000.0]
    with caplog.at_level(logging.ERROR):
        for _ in range(5):
            now[0] += 1
            fire_due(session, NOW, monotonic=lambda: now[0])
        assert len([r for r in caplog.records if r.exc_info]) == 1
        now[0] += 120
        fire_due(session, NOW, monotonic=lambda: now[0])
    assert len([r for r in caplog.records if r.exc_info]) == 2


@pytest.mark.usefixtures("deployed")
def test_a_deploy_computes_the_next_time_from_the_database_clock(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deploy's `now` is the database's, the one the tick compares with."""
    fixed = datetime(2031, 1, 1, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(deploys_module, "db_now", lambda _session: fixed)
    other = _repo(session, "other")
    _deployed_jobs(session, principal, other, "b" * 40, {"daily": _job()}, table="silver.other")
    row = session.scalars(select(JobSchedule).where(JobSchedule.repository_id == other.id)).one()
    assert row.next_fire_at == datetime(2031, 1, 1, 3, 0, tzinfo=UTC)


def test_on_postgres_every_transaction_of_a_session_begins_with_a_lock_timeout(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SET LOCAL` is issued at each begin, and the listener is gone afterwards."""
    seen: list[str] = []

    @event.listens_for(session.get_bind(), "before_cursor_execute", retval=True)
    def record(conn, cursor, statement, parameters, context, executemany):  # ruff: ignore[missing-type-function-argument, missing-return-type-private-function]
        if statement.startswith("SET LOCAL"):
            seen.append(statement)
            return "SELECT 1", parameters
        return statement, parameters

    monkeypatch.setattr(session.get_bind().dialect, "name", "postgresql")
    with scheduler_module.bounded_lock_waits(session, 1.5):
        session.execute(select(1))
        session.commit()
        session.execute(select(1))
        session.rollback()
    assert seen == ["SET LOCAL lock_timeout = '1500ms'"] * 2
    session.execute(select(1))
    assert len(seen) == 2


@pytest.mark.usefixtures("deployed")
def test_a_failing_audit_of_an_unstarted_fire_is_logged_and_does_not_stop_the_tick(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Recording the drop is best effort; the next schedule still gets its turn."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session)

    def refuse(self: JobService, *args: object, **kwargs: object) -> object:
        raise RuntimeError("start broke")

    def broken(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError("audit broke")

    monkeypatch.setattr(JobService, "start", refuse)
    monkeypatch.setattr(scheduler_module.AuditService, "record", broken)
    with caplog.at_level(logging.ERROR):
        (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.NOT_STARTED
    assert "audit broke" in caplog.text


def test_a_waiting_start_with_an_unreadable_request_time_goes_last(
    session: Session, two_scheduled: Repository, principal: PrincipalRef
) -> None:
    """Its turn comes after the readable ones; `start_waiting` drops it when it gets there."""
    _wait_with(session, "daily", requested_by=str(principal.id), requested_at="not a time")
    _wait_with(
        session, "nightly", requested_by=str(principal.id), requested_at="2026-10-15T03:00:09"
    )
    (first,) = start_waiting_jobs(session)
    assert first.job == "nightly"


def _other_project(session: Session, principal: PrincipalRef) -> Repository:
    """Deploy a second repository, `other`, with the scheduled job `daily`.

    Parameters
    ----------
    session
        Database session.
    principal
        The deployer.

    Returns
    -------
    Repository
        The repository.
    """
    other = _repo(session, "other")
    _deployed_jobs(session, principal, other, "b" * 40, {"daily": _job()}, "silver.other")
    return other


def _set_times(session: Session, repo: Repository, at: datetime) -> None:
    """Set `next_fire_at` of every schedule of a repository.

    Parameters
    ----------
    session
        Database session.
    repo
        The repository.
    at
        The time.
    """
    session.execute(
        update(JobSchedule).where(JobSchedule.repository_id == repo.id).values(next_fire_at=at)
    )
    session.commit()


def _wait_in(
    session: Session, repo: Repository, job: str, requested_at: str, requester: PrincipalRef
) -> None:
    """Store a waiting start on a job of a repository.

    Parameters
    ----------
    session
        Database session.
    repo
        The repository.
    job
        The job.
    requested_at
        When it was requested.
    requester
        Who asked.
    """
    row = session.scalars(
        select(JobSchedule).where(JobSchedule.repository_id == repo.id, JobSchedule.job == job)
    ).one()
    row.pending_start = {
        "trigger": "manual",
        "requested_at": requested_at,
        "requested_by": str(requester.id),
        "scheduled_for": None,
        "missed": 0,
        "merged": 1,
    }
    session.commit()


def test_a_stuck_project_does_not_use_up_the_tick_of_the_others(
    session: Session,
    two_scheduled: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a lock timeout on one project its other schedules wait; another project fires."""
    other = _other_project(session, principal)
    _bind(session, _account(session).id)
    _set_times(session, two_scheduled, datetime(2026, 10, 14, 3, 0, tzinfo=UTC))
    _set_times(session, other, FIRE)
    real = JobService.book_fire.__func__
    stuck: list[uuid.UUID] = []

    def book(cls: type[JobService], s: Session, schedule_id: uuid.UUID, now: datetime) -> object:
        row = s.get(JobSchedule, schedule_id)
        assert row is not None
        if row.repository_id == two_scheduled.id:
            stuck.append(schedule_id)
            s.rollback()
            raise _lock_timeout()
        return real(cls, s, schedule_id, now)

    monkeypatch.setattr(JobService, "book_fire", classmethod(book))
    fires = fire_due(session, NOW)
    assert [(f.repository, f.job, f.outcome) for f in fires] == [
        ("other", "daily", FireOutcome.STARTED)
    ]
    assert len(stuck) == 1


def test_a_stuck_project_does_not_use_up_the_waiting_starts_of_the_others(
    session: Session,
    two_scheduled: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock timeout on the oldest project's start skips its other starts; another project runs."""
    other = _other_project(session, principal)
    _wait_in(session, two_scheduled, "nightly", "2026-10-15T03:00:01+00:00", principal)
    _wait_in(session, two_scheduled, "daily", "2026-10-15T03:00:02+00:00", principal)
    _wait_in(session, other, "daily", "2026-10-15T03:00:09+00:00", principal)
    real = JobService.start_waiting.__func__
    stuck: list[str] = []

    def start_waiting(
        cls: type[JobService],
        s: Session,
        schedule: JobSchedule,
        caps: RunCaps | None = None,
        lease_seconds: int = 60,
    ) -> Run | None:
        if schedule.repository_id == two_scheduled.id:
            stuck.append(schedule.job)
            s.rollback()
            raise _lock_timeout()
        return real(cls, s, schedule, caps=caps, lease_seconds=lease_seconds)

    monkeypatch.setattr(JobService, "start_waiting", classmethod(start_waiting))
    (started,) = start_waiting_jobs(session)
    assert started.repository_id == other.id
    assert stuck == ["nightly"]


@pytest.mark.usefixtures("deployed")
def test_a_failure_to_link_the_run_to_its_fire_is_logged_and_the_tick_goes_on(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The run has started; the link is a bookkeeping step that cannot end the loop."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session)

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("link broke")

    monkeypatch.setattr(scheduler_module, "update", broken)
    with caplog.at_level(logging.ERROR):
        (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.STARTED
    assert "link broke" in caplog.text
    assert len(session.scalars(select(Run)).all()) == 1


def test_a_failure_to_link_one_fire_does_not_stop_the_next_schedule(
    session: Session,
    two_scheduled: Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both schedules are booked and both are started although every link fails."""
    _bind(session, _account(session).id)
    _due(session, "daily")
    _due(session, "nightly")

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("link broke")

    monkeypatch.setattr(scheduler_module, "update", broken)
    fires = fire_due(session, NOW)
    assert sorted(f.job for f in fires) == ["daily", "nightly"]
    assert len(session.scalars(select(JobFire)).all()) == 2


@pytest.mark.usefixtures("deployed")
def test_a_failure_to_classify_the_refusal_still_audits_the_drop_as_an_error(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If reading the binding fails, the event says `error` and the tick is not aborted."""
    _far(session)
    _due(session)

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("binding unreadable")

    monkeypatch.setattr(scheduler_module, "run_principal_problem", broken)
    (fire,) = fire_due(session, NOW)
    assert fire.outcome is FireOutcome.NOT_STARTED
    (event,) = _dropped(session)
    assert event.detail["reason"] == "error"


def test_the_log_limit_is_kept_per_schedule_not_per_fire(
    session: Session,
    two_scheduled: Repository,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A schedule whose starts fail every day is logged once a minute, however many fires."""
    _bind(session, _account(session).id)

    def refuse(self: JobService, *args: object, **kwargs: object) -> object:
        raise RuntimeError("start broke")

    monkeypatch.setattr(JobService, "start", refuse)
    scheduler_module._LAST_LOGGED.clear()
    clock = [5000.0]
    with caplog.at_level(logging.ERROR):
        for day in range(3):
            _far(session)
            _due(session, "daily", datetime(2026, 10, 15 + day, 3, 0, tzinfo=UTC))
            fire_due(
                session,
                datetime(2026, 10, 15 + day, 3, 0, 30, tzinfo=UTC),
                monotonic=lambda: clock[0],
            )
            clock[0] += 1
    assert len([r for r in caplog.records if r.exc_info]) == 1


def test_the_log_limit_does_not_grow_without_bound() -> None:
    """Entries older than the window are dropped when a new one is added."""
    scheduler_module._LAST_LOGGED.clear()
    for n in range(2000):
        scheduler_module._LAST_LOGGED["start", uuid.uuid4()] = float(n)
    assert scheduler_module._may_log("start", uuid.uuid4(), 100000.0) is True
    assert len(scheduler_module._LAST_LOGGED) == 1


_FIELDS = {"repository", "job", "trigger", "requested_by", "scheduled_for", "missed", "merged"}


@pytest.mark.usefixtures("deployed")
def test_the_event_of_an_unstarted_fire_has_the_fields_of_every_other_path(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same keys on the scheduler's drop as on the drops of a waiting start."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session)
    monkeypatch.setattr(
        JobService, "start", lambda self, *a, **k: (_ for _ in ()).throw(ValidationError("x"))
    )
    fire_due(session, NOW)
    (event,) = _dropped(session)
    assert set(event.detail) >= _FIELDS | {"reason"}
    assert (event.detail["scheduled_for"], event.detail["missed"]) == (FIRE.isoformat(), 0)


def _stuck_setup(
    session: Session,
    two_scheduled: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    clock: list[float],
) -> Repository:
    """Make one project's pointer time out (taking 2 s each time) and another one healthy.

    Parameters
    ----------
    session
        Database session.
    two_scheduled
        The stuck project.
    principal
        The deployer and requester.
    monkeypatch
        The fixture.
    clock
        The fake monotonic clock, advanced by every timeout.

    Returns
    -------
    Repository
        The healthy project.
    """
    other = _other_project(session, principal)
    _bind(session, _account(session).id)
    _set_times(session, two_scheduled, datetime(2026, 10, 14, 3, 0, tzinfo=UTC))
    _set_times(session, other, datetime(2030, 1, 1, tzinfo=UTC))
    _wait_in(session, two_scheduled, "nightly", "2026-10-15T03:00:01+00:00", principal)
    _wait_in(session, other, "daily", "2026-10-15T03:00:09+00:00", principal)
    real = JobService.start_waiting.__func__

    def book(cls: type[JobService], s: Session, schedule_id: uuid.UUID, now: datetime) -> object:
        s.rollback()
        clock[0] += 2
        raise _lock_timeout()

    def start_waiting(
        cls: type[JobService],
        s: Session,
        schedule: JobSchedule,
        caps: RunCaps | None = None,
        lease_seconds: int = 60,
    ) -> Run | None:
        if schedule.repository_id == two_scheduled.id:
            s.rollback()
            clock[0] += 2
            raise _lock_timeout()
        return real(cls, s, schedule, caps=caps, lease_seconds=lease_seconds)

    monkeypatch.setattr(JobService, "book_fire", classmethod(book))
    monkeypatch.setattr(JobService, "start_waiting", classmethod(start_waiting))
    return other


def test_the_projects_found_stuck_by_the_fires_are_skipped_by_the_waiting_starts(
    session: Session,
    two_scheduled: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stuck project's timeouts use up the time; the other project's start still begins."""
    clock = [0.0]
    other = _stuck_setup(session, two_scheduled, principal, monkeypatch, clock)
    stuck: set[tuple[uuid.UUID, uuid.UUID]] = set()
    fire_due(session, NOW, monotonic=lambda: clock[0], stuck=stuck)
    assert clock[0] > 1 and len(stuck) == 1
    started = start_waiting_jobs(
        session, time_limit=TICK_BUDGET_SECONDS - clock[0], monotonic=lambda: clock[0], skip=stuck
    )
    assert [r.repository_id for r in started] == [other.id]


def test_a_project_that_was_stuck_goes_last_on_the_next_ticks(
    session: Session,
    two_scheduled: Repository,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the set, the memory of a recent timeout puts the stuck project behind the rest."""
    clock = [1000.0]
    other = _stuck_setup(session, two_scheduled, principal, monkeypatch, clock)
    scheduler_module._RECENTLY_STUCK.clear()
    fire_due(session, NOW, monotonic=lambda: clock[0])
    started = start_waiting_jobs(
        session, time_limit=TICK_BUDGET_SECONDS - 2.0, monotonic=lambda: clock[0]
    )
    assert [r.repository_id for r in started] == [other.id]


@pytest.mark.usefixtures("deployed")
def test_a_pointer_that_kept_moving_is_logged_as_a_wait_without_a_traceback(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """It is a busy project, not a failure."""
    _bind(session, _account(session).id)
    _far(session)
    _due(session)

    def moved(cls: type[JobService], s: Session, schedule_id: uuid.UUID, now: datetime) -> None:
        raise DeploymentMoved("moved")

    monkeypatch.setattr(JobService, "book_fire", classmethod(moved))
    scheduler_module._LAST_LOGGED.clear()
    with caplog.at_level(logging.WARNING):
        assert fire_due(session, NOW) == []
    assert not [r for r in caplog.records if r.exc_info]
    assert [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "kept moving" in r.getMessage()
    ]
