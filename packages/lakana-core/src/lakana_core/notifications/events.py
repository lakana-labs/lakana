"""Deciding the event of a finished run of a job, and queuing its notification.

Both run inside the transaction that finishes the run (`lakana_core.runs.store`), so the
notification is queued exactly when the run is finished and not otherwise.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from lakana_core.models import (
    JobVersion,
    NotificationDelivery,
    NotificationState,
    Run,
    RunStatus,
    TaskRun,
)
from lakana_core.policy.schema_change import changed_tables
from lakana_core.projects.manifest import JobNotify, job_notify

FAILED = "failed"
"""The event of a run of a job that ended failed."""

RECOVERED = "recovered"
"""The event of a run that succeeded after the previous finished run failed."""

COLUMNS_CHANGED = "columns_changed"
"""The event of a run that succeeded, reports no other event, and whose writes removed or replaced
a column (`TaskRun.schema_change_json`). Sent whether or not a row filter or column mask uses the
column: the endpoint is no administrator, so the message must not tell that a policy exists."""


def decide_event(session: Session, run: Run) -> str | None:
    """Decide which event a finished run of a job reports.

    A failed run reports `FAILED`, whether or not a blocking check failed in it: the one message
    names the check. A succeeded run reports `RECOVERED` when the previous finished run of the same
    job (same environment and repository, ordered by `created_at`, then `finished_at`) failed;
    cancelled runs are looked through, and the first run of a job has no previous one. Any other
    succeeded run reports `COLUMNS_CHANGED` when a task's write removed or replaced a column; a
    failed or recovered run names those tables in its own message instead. A `warn`
    check does not fail a run, so it changes nothing here.

    Parameters
    ----------
    session
        Database session.
    run
        The run, with its final status.

    Returns
    -------
    str or None
        `FAILED`, `RECOVERED`, `COLUMNS_CHANGED`, or `None` for an ad-hoc run (no job), a
        cancelled run, and a success without a column change that follows a success or nothing.
    """
    if run.job is None:
        return None
    if run.status is RunStatus.FAILED:
        return FAILED
    if run.status is not RunStatus.SUCCEEDED:
        return None
    earlier = Run.created_at < run.created_at
    if run.finished_at is not None:
        # Equal creation times (two clocks, or one coarse one) fall back to the finish order.
        earlier = or_(
            earlier, and_(Run.created_at == run.created_at, Run.finished_at < run.finished_at)
        )
    previous = session.scalars(
        select(Run.status)
        .where(
            Run.environment_id == run.environment_id,
            Run.repository_id == run.repository_id,
            Run.job == run.job,
            Run.id != run.id,
            earlier,
            Run.status.in_((RunStatus.SUCCEEDED, RunStatus.FAILED)),
        )
        .order_by(Run.created_at.desc(), Run.finished_at.desc(), Run.id)
        .limit(1)
    ).first()
    if previous is RunStatus.FAILED:
        return RECOVERED
    return COLUMNS_CHANGED if columns_changed_tables(session, run.id) else None


def columns_changed_tables(session: Session, run_id: uuid.UUID) -> tuple[str, ...]:
    """Name the tables whose columns a run's writes removed or replaced.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.

    Returns
    -------
    tuple of str
        The tables, sorted, each once; empty when there are none.
    """
    stored = session.scalars(
        select(TaskRun.schema_change_json).where(TaskRun.run_id == run_id)
    ).all()
    return tuple(sorted({table for entry in stored for table in changed_tables(entry)}))


def new_delivery(
    notify: JobNotify,
    event: str,
    now: datetime,
    *,
    run_id: uuid.UUID | None,
    environment_id: uuid.UUID,
    repository_id: uuid.UUID,
    job: str,
) -> NotificationDelivery:
    """Build a delivery that is due at once, with its first attempt still to come.

    Parameters
    ----------
    notify
        The job's `notify:`.
    event
        ``failed``, ``recovered``, ``columns_changed`` or ``test``.
    now
        When the first attempt is due.
    run_id
        The run; `None` for a test.
    environment_id
        The environment.
    repository_id
        The repository.
    job
        The job.

    Returns
    -------
    NotificationDelivery
        The new row, not yet added to a session.
    """
    return NotificationDelivery(
        id=uuid.uuid4(),
        run_id=run_id,
        environment_id=environment_id,
        repository_id=repository_id,
        job=job,
        event=event,
        format=notify.format,
        secret_name=notify.webhook,
        state=NotificationState.PENDING,
        attempts=0,
        next_attempt_at=now,
    )


def queue_notification(session: Session, run: Run, now: datetime) -> NotificationDelivery | None:
    """Queue the notification of a finished run, when its job declares one.

    The job's `notify` is read from the project version the run ran under, so a run started under
    a version without it queues nothing, whatever the current version says. The existing row for
    (`run_id`, `event`) is looked for first, under the lock the run's finishing took on the run,
    which makes a repeated call a no-op on any database.

    Parameters
    ----------
    session
        Database session, inside the transaction that finishes the run.
    run
        The run, with its final status.
    now
        The time; the delivery is due at once.

    Returns
    -------
    NotificationDelivery or None
        The queued row; `None` when the run has no job, the job declares no notification, the run
        reports no event, or the event is already queued.
    """
    if run.job is None:
        return None
    version = session.get(JobVersion, run.job_version_id)
    notify = job_notify(version.manifest_json, run.job) if version is not None else None
    if notify is None:
        return None
    event = decide_event(session, run)
    if event is None:
        return None
    queued = session.scalars(
        select(NotificationDelivery.id).where(
            NotificationDelivery.run_id == run.id, NotificationDelivery.event == event
        )
    ).first()
    if queued is not None:
        return None
    row = new_delivery(
        notify,
        event,
        now,
        run_id=run.id,
        environment_id=run.environment_id,
        repository_id=run.repository_id,
        job=run.job,
    )
    session.add(row)
    session.flush()
    return row
