"""Deciding the event of a finished job run, and queuing its notification."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    Environment,
    JobVersion,
    NotificationDelivery,
    NotificationState,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    RunTrigger,
    TaskRun,
    TaskStatus,
)
from lakana_core.notifications.events import decide_event, queue_notification

T0 = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
NOTIFY = {"webhook": "alerts", "format": "slack"}


class Project:
    """An environment, a repository and a project version whose manifest names one job.

    Parameters
    ----------
    session
        Database session.
    notify
        The ``notify`` entry of the job ``daily``; `None` leaves it out.
    """

    def __init__(self, session: Session, notify: dict | None = NOTIFY) -> None:
        self.session = session
        self.environment = Environment(id=uuid.uuid4(), name="prod", storage_root="file:///tmp/p")
        self.repository = Repository(
            id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a.git"
        )
        session.add_all([self.environment, self.repository])
        session.flush()
        self.version = self.add_version(notify)
        self.clock = 0

    def add_version(self, notify: dict | None, sha: str = "a") -> JobVersion:
        """Add a project version.

        Parameters
        ----------
        notify
            The ``notify`` entry of the job ``daily``; `None` leaves it out.
        sha
            One character, repeated to the commit.

        Returns
        -------
        JobVersion
            The row.
        """
        entry: dict = {"select": ["x.*"]}
        if notify is not None:
            entry["notify"] = notify
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=self.repository.id,
            environment_id=self.environment.id,
            commit_sha=sha * 40,
            manifest_json={"version": 6, "assets": [], "edges": [], "jobs": {"daily": entry}},
            code_uri="file:///tmp/p/code.tar.gz",
            code_sha256="0" * 64,
        )
        self.session.add(version)
        self.session.flush()
        return version

    def run(
        self,
        status: RunStatus,
        *,
        job: str | None = "daily",
        trigger: RunTrigger | None = RunTrigger.SCHEDULE,
        kind: RunKind = RunKind.FULL,
        version: JobVersion | None = None,
    ) -> Run:
        """Add a run created one minute after the one before.

        Parameters
        ----------
        status
            Its status.
        job
            Its job; `None` for an ad-hoc run.
        trigger
            What started it.
        kind
            Its kind.
        version
            The project version it ran under; the first one by default.

        Returns
        -------
        Run
            The row.
        """
        self.clock += 1
        run = Run(
            id=uuid.uuid4(),
            environment_id=self.environment.id,
            repository_id=self.repository.id,
            job_version_id=(version or self.version).id,
            kind=kind,
            selection_json={},
            status=status,
            created_at=T0 + timedelta(minutes=self.clock),
            job=job,
            trigger=trigger,
        )
        self.session.add(run)
        self.session.flush()
        return run


@pytest.fixture
def project(session: Session) -> Project:
    """Provide a project whose job `daily` notifies.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    Project
        The rows.
    """
    return Project(session)


def _check(project: Project, run: Run, on_failure: str, outcome: CheckOutcome) -> None:
    """Add a task and a check result to a run.

    Parameters
    ----------
    project
        The project.
    run
        The run.
    on_failure
        ``block``, ``keep`` or ``warn``.
    outcome
        The check's outcome.
    """
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=run.id,
        asset="silver.clean",
        status=TaskStatus.FAILED,
        max_attempts=1,
        timeout_seconds=60,
        retry_delay_seconds=0,
    )
    project.session.add(task)
    project.session.flush()
    project.session.add(
        CheckResult(
            id=uuid.uuid4(),
            run_id=run.id,
            task_run_id=task.id,
            environment_id=project.environment.id,
            check_name="no_nulls",
            check_path="checks/no_nulls.sql",
            table_name="silver.clean",
            on_failure=on_failure,
            outcome=outcome,
            failing_rows=3,
            started_at=T0,
            finished_at=T0,
        )
    )
    project.session.flush()


def test_a_failed_run_gives_failed(project: Project, session: Session) -> None:
    assert decide_event(session, project.run(RunStatus.FAILED)) == "failed"


@pytest.mark.parametrize(
    ("on_failure", "outcome"),
    [("block", CheckOutcome.FAILED), ("keep", CheckOutcome.ERRORED)],
)
def test_a_failed_run_with_a_failed_blocking_check_gives_the_one_failed_event(
    project: Project, session: Session, on_failure: str, outcome: CheckOutcome
) -> None:
    run = project.run(RunStatus.FAILED)
    _check(project, run, on_failure, outcome)
    assert decide_event(session, run) == "failed"
    queue_notification(session, run, T0)
    assert [d.event for d in session.scalars(select(NotificationDelivery))] == ["failed"]


def test_a_warn_check_sends_nothing(project: Project, session: Session) -> None:
    run = project.run(RunStatus.SUCCEEDED)
    _check(project, run, "warn", CheckOutcome.WARNED)
    assert decide_event(session, run) is None


def test_a_success_after_a_failure_gives_recovered(project: Project, session: Session) -> None:
    project.run(RunStatus.FAILED)
    assert decide_event(session, project.run(RunStatus.SUCCEEDED)) == "recovered"


def test_a_success_after_a_success_and_the_first_run_give_nothing(
    project: Project, session: Session
) -> None:
    assert decide_event(session, project.run(RunStatus.SUCCEEDED)) is None
    assert decide_event(session, project.run(RunStatus.SUCCEEDED)) is None


def test_a_cancel_after_a_success_does_not_make_the_next_success_recovered(
    project: Project, session: Session
) -> None:
    project.run(RunStatus.SUCCEEDED)
    project.run(RunStatus.CANCELLED)
    assert decide_event(session, project.run(RunStatus.SUCCEEDED)) is None


def test_a_cancel_between_a_failure_and_a_success_is_looked_through(
    project: Project, session: Session
) -> None:
    project.run(RunStatus.FAILED)
    project.run(RunStatus.CANCELLED)
    assert decide_event(session, project.run(RunStatus.SUCCEEDED)) == "recovered"


def test_a_cancelled_run_gives_nothing(project: Project, session: Session) -> None:
    assert decide_event(session, project.run(RunStatus.CANCELLED)) is None


def test_only_the_same_job_environment_and_repository_count(
    project: Project, session: Session
) -> None:
    project.run(RunStatus.FAILED, job="other")
    other_repo = Repository(
        id=uuid.uuid4(), name="b", kind=RepositoryKind.HTTPS, url="https://h/b.git"
    )
    session.add(other_repo)
    session.flush()
    foreign = project.run(RunStatus.FAILED)
    foreign.repository_id = other_repo.id
    other_env = Environment(id=uuid.uuid4(), name="dev", storage_root="file:///tmp/d")
    session.add(other_env)
    session.flush()
    elsewhere = project.run(RunStatus.FAILED)
    elsewhere.environment_id = other_env.id
    project.run(RunStatus.FAILED, job=None, trigger=None)
    session.flush()
    assert decide_event(session, project.run(RunStatus.SUCCEEDED)) is None


def test_a_later_run_does_not_count(project: Project, session: Session) -> None:
    first = project.run(RunStatus.SUCCEEDED)
    project.run(RunStatus.FAILED)
    assert decide_event(session, first) is None


@pytest.mark.parametrize(
    ("trigger", "kind"),
    [
        (RunTrigger.MANUAL, RunKind.FULL),
        (RunTrigger.API, RunKind.FULL),
        (RunTrigger.SCHEDULE, RunKind.RESUME),
    ],
)
def test_manual_api_and_resumed_runs_count(
    project: Project, session: Session, trigger: RunTrigger, kind: RunKind
) -> None:
    project.run(RunStatus.FAILED, trigger=trigger, kind=kind)
    run = project.run(RunStatus.SUCCEEDED, trigger=trigger, kind=kind)
    assert decide_event(session, run) == "recovered"
    assert decide_event(session, project.run(RunStatus.FAILED, trigger=trigger)) == "failed"


def test_an_ad_hoc_run_gives_no_event_and_no_row(project: Project, session: Session) -> None:
    run = project.run(RunStatus.FAILED, job=None, trigger=None)
    assert decide_event(session, run) is None
    assert queue_notification(session, run, T0) is None
    assert session.scalars(select(NotificationDelivery)).all() == []


def test_a_cancelled_run_of_a_notifying_job_queues_no_row(
    project: Project, session: Session
) -> None:
    """The job declares `notify`, but a cancelled run reports no event, so nothing is queued."""
    run = project.run(RunStatus.CANCELLED)
    assert queue_notification(session, run, T0) is None
    assert session.scalars(select(NotificationDelivery)).all() == []


def test_a_job_without_notify_gives_no_row(session: Session) -> None:
    project = Project(session, notify=None)
    assert queue_notification(session, project.run(RunStatus.FAILED), T0) is None
    assert session.scalars(select(NotificationDelivery)).all() == []


def test_a_run_under_an_older_version_without_notify_gives_no_row(
    project: Project, session: Session
) -> None:
    old = project.add_version(None, "b")
    assert queue_notification(session, project.run(RunStatus.FAILED, version=old), T0) is None
    assert queue_notification(session, project.run(RunStatus.FAILED), T0) is not None


def test_the_row_is_pending_and_due_at_once_and_names_where_it_goes(
    project: Project, session: Session
) -> None:
    run = project.run(RunStatus.FAILED)
    row = queue_notification(session, run, T0)
    assert row is not None
    session.commit()
    session.expire_all()
    row = session.scalars(select(NotificationDelivery)).one()
    assert (row.run_id, row.environment_id, row.repository_id, row.job) == (
        run.id,
        project.environment.id,
        project.repository.id,
        "daily",
    )
    assert (row.event, row.format, row.secret_name) == ("failed", "slack", "alerts")
    assert (row.state, row.attempts, row.next_attempt_at) == (NotificationState.PENDING, 0, T0)
    assert (row.lease_owner, row.lease_expires_at, row.last_result, row.delivered_at) == (
        None,
        None,
        None,
        None,
    )


def test_queuing_twice_gives_one_row(project: Project, session: Session) -> None:
    run = project.run(RunStatus.FAILED)
    first = queue_notification(session, run, T0)
    second = queue_notification(session, run, T0)
    session.commit()
    assert first is not None and second is None
    assert len(session.scalars(select(NotificationDelivery)).all()) == 1


def test_a_tie_in_created_at_is_broken_by_finished_at(project: Project, session: Session) -> None:
    failed = project.run(RunStatus.FAILED)
    failed.finished_at = T0 + timedelta(minutes=10)
    earlier = project.run(RunStatus.SUCCEEDED)
    earlier.created_at = failed.created_at
    earlier.finished_at = T0 + timedelta(minutes=5)
    current = project.run(RunStatus.SUCCEEDED)
    current.created_at = failed.created_at
    current.finished_at = T0 + timedelta(minutes=20)
    session.flush()
    assert decide_event(session, current) == "recovered"


def test_a_success_with_a_failing_warn_check_still_recovers(
    project: Project, session: Session
) -> None:
    project.run(RunStatus.FAILED)
    run = project.run(RunStatus.SUCCEEDED)
    _check(project, run, "warn", CheckOutcome.FAILED)
    assert decide_event(session, run) == "recovered"


def _changed(project: Project, run: Run) -> None:
    """Add a succeeded task whose write removed or retyped a column.

    Parameters
    ----------
    project
        The project.
    run
        The run.
    """
    project.session.add(
        TaskRun(
            id=uuid.uuid4(),
            run_id=run.id,
            asset="gold.orders",
            status=TaskStatus.SUCCEEDED,
            max_attempts=1,
            timeout_seconds=60,
            retry_delay_seconds=0,
            schema_change_json={"tables": ["gold.orders"], "policies": []},
        )
    )
    project.session.flush()


def test_a_success_that_removed_or_retyped_a_column_gives_columns_changed(
    project: Project, session: Session
) -> None:
    run = project.run(RunStatus.SUCCEEDED)
    _changed(project, run)
    assert decide_event(session, run) == "columns_changed"
    queue_notification(session, run, T0)
    assert [d.event for d in session.scalars(select(NotificationDelivery))] == ["columns_changed"]


def test_a_failure_or_a_recovery_that_changed_columns_keeps_its_own_event(
    project: Project, session: Session
) -> None:
    failed = project.run(RunStatus.FAILED)
    _changed(project, failed)
    assert decide_event(session, failed) == "failed"
    recovered = project.run(RunStatus.SUCCEEDED)
    _changed(project, recovered)
    assert decide_event(session, recovered) == "recovered"
