"""Job model additions: the state of a job's schedule, the fires, and how a run started."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.models import (
    Environment,
    JobFire,
    JobSchedule,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    RunTrigger,
)

NOW = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)


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
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="ada")
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
        manifest_json={"version": 5, "assets": [], "edges": []},
        code_uri="file:///tmp/prod/_artifacts/code/a.tar.gz",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.commit()
    return environment, repository, principal, version


def _schedule(environment: Environment, repository: Repository, job: str = "daily") -> JobSchedule:
    """Build an unsaved job schedule.

    Parameters
    ----------
    environment
        Its environment.
    repository
        Its repository.
    job
        The job name.

    Returns
    -------
    JobSchedule
        The row.
    """
    return JobSchedule(
        id=uuid.uuid4(), environment_id=environment.id, repository_id=repository.id, job=job
    )


def test_a_new_job_schedule_is_not_paused_and_has_nothing_waiting(session: Session) -> None:
    environment, repository, _, _ = _seed(session)
    session.add(_schedule(environment, repository))
    session.commit()
    row = session.query(JobSchedule).one()
    assert (row.next_fire_at, row.paused, row.paused_by, row.paused_at) == (None, False, None, None)
    assert (row.pending_start, row.last_fired_for) == (None, None)


def test_a_job_schedule_keeps_its_times_in_utc_and_its_waiting_start(session: Session) -> None:
    environment, repository, principal, _ = _seed(session)
    waiting = {
        "trigger": "schedule",
        "requested_at": NOW.isoformat(),
        "requested_by": None,
        "scheduled_for": NOW.isoformat(),
        "missed": 2,
        "merged": 3,
    }
    row = _schedule(environment, repository)
    row.next_fire_at = NOW
    row.paused, row.paused_by, row.paused_at = True, principal.id, NOW
    row.pending_start = waiting
    row.last_fired_for = NOW
    session.add(row)
    session.commit()
    session.expire_all()
    found = session.query(JobSchedule).one()
    assert found.next_fire_at == NOW
    assert found.next_fire_at is not None and found.next_fire_at.tzinfo is not None
    assert (found.paused, found.paused_by, found.paused_at) == (True, principal.id, NOW)
    assert found.pending_start == waiting
    assert found.last_fired_for == NOW


def test_a_job_has_one_schedule_row_per_environment_and_repository(session: Session) -> None:
    environment, repository, _, _ = _seed(session)
    session.add(_schedule(environment, repository))
    session.commit()
    session.add(_schedule(environment, repository, job="hourly"))
    session.commit()
    session.add(_schedule(environment, repository))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_the_pausing_principal_keeps_the_row(session: Session) -> None:
    environment, repository, principal, _ = _seed(session)
    row = _schedule(environment, repository)
    row.paused, row.paused_by = True, principal.id
    session.add(row)
    session.commit()
    session.delete(principal)
    session.commit()
    session.refresh(row)
    assert (row.paused, row.paused_by) == (True, None)


def test_a_scheduled_time_can_fire_only_once(session: Session) -> None:
    environment, repository, _, _ = _seed(session)
    schedule = _schedule(environment, repository)
    session.add(schedule)
    session.commit()
    session.add(JobFire(id=uuid.uuid4(), job_schedule_id=schedule.id, scheduled_for=NOW))
    session.commit()
    fire = session.query(JobFire).one()
    assert (fire.run_id, fire.created_at.tzinfo) == (None, UTC)
    session.add(JobFire(id=uuid.uuid4(), job_schedule_id=schedule.id, scheduled_for=NOW))
    with pytest.raises(IntegrityError):
        session.commit()


def test_deleting_a_run_keeps_its_fire_and_deleting_the_schedule_removes_it(
    session: Session,
) -> None:
    environment, repository, _, version = _seed(session)
    schedule = _schedule(environment, repository)
    run = Run(
        id=uuid.uuid4(),
        environment_id=environment.id,
        repository_id=repository.id,
        job_version_id=version.id,
        kind=RunKind.FULL,
        status=RunStatus.SUCCEEDED,
        selection_json={},
    )
    session.add_all([schedule, run])
    session.commit()
    session.add(
        JobFire(id=uuid.uuid4(), job_schedule_id=schedule.id, scheduled_for=NOW, run_id=run.id)
    )
    session.commit()
    session.delete(run)
    session.commit()
    assert session.query(JobFire).one().run_id is None
    session.delete(schedule)
    session.commit()
    assert session.query(JobFire).count() == 0


def test_a_run_has_no_job_and_no_trigger_unless_it_is_given_one(session: Session) -> None:
    environment, repository, _, version = _seed(session)
    base = {
        "environment_id": environment.id,
        "repository_id": repository.id,
        "job_version_id": version.id,
        "kind": RunKind.FULL,
        "selection_json": {},
    }
    session.add(Run(id=uuid.uuid4(), status=RunStatus.SUCCEEDED, **base))
    session.add(
        Run(
            id=uuid.uuid4(),
            status=RunStatus.RUNNING,
            job="daily",
            trigger=RunTrigger.SCHEDULE,
            scheduled_for=NOW,
            missed=4,
            **base,
        )
    )
    session.commit()
    adhoc = session.query(Run).filter(Run.job.is_(None)).one()
    assert (adhoc.trigger, adhoc.scheduled_for, adhoc.missed) == (None, None, 0)
    job = session.query(Run).filter(Run.job == "daily").one()
    assert (job.trigger, job.scheduled_for, job.missed) == (RunTrigger.SCHEDULE, NOW, 4)


def test_the_triggers_are_schedule_api_manual_and_deploy() -> None:
    assert {t.value for t in RunTrigger} == {"schedule", "api", "manual", "deploy"}


def test_a_cleared_waiting_start_is_sql_null_not_a_json_null(session: Session) -> None:
    environment, repository, _, _ = _seed(session)
    row = _schedule(environment, repository)
    row.pending_start = {"trigger": "schedule", "missed": 0, "merged": 1}
    session.add(row)
    session.commit()
    assert session.query(JobSchedule).filter(JobSchedule.pending_start.is_not(None)).count() == 1
    row.pending_start = None
    session.commit()
    assert session.query(JobSchedule).filter(JobSchedule.pending_start.is_(None)).count() == 1
    assert session.query(JobSchedule).filter(JobSchedule.pending_start.is_not(None)).count() == 0
