"""Loading the scheduler snapshot and applying transitions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from sqlalchemy import delete, event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_projects_dag import make_asset
from test_projects_deploys import _deployed, _repo

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.db import db_now
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    JobVersion,
    NotificationDelivery,
    NotificationState,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunStatus,
    TaskAttempt,
    TaskRun,
    TaskStatus,
    Worker,
    WriteMode,
)
from lakana_core.runs import store, workers
from lakana_core.runs.checks import CHECKS_UNFINISHED_MESSAGE, MAX_CHECK_CLAIMS, SET_BACK_LOST
from lakana_core.runs.scheduler import LOST_MESSAGE, RunTransition, TaskTransition, tick
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import RunService
from lakana_core.runs.store import (
    CHECK_LOST_MESSAGE,
    COMMITTED_BEFORE_LOST_MESSAGE,
    DEPLOY_LOST_MESSAGE,
    LEASE_EXPIRED_MESSAGE,
    MAINTENANCE_LOST_MESSAGE,
    QUERY_LOST_MESSAGE,
    SWEEP_BATCH,
    WORKER_LOST_WRITE_MESSAGE,
    WRITE_LOST_MESSAGE,
    LostOutcome,
    apply_transitions,
    end_lost_runs,
    load_snapshots,
    lost_outcome,
    scheduler_tick,
)
from lakana_core.runs.tasks import claim_task, record_attempt

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
_BRONZE = NamespacePath.from_parts(["bronze"])


def _half() -> float:
    """No jitter.

    Returns
    -------
    float
        0.5.
    """
    return 0.5


@pytest.fixture
def run_id(session: Session, tmp_path: Path, principal: PrincipalRef) -> uuid.UUID:
    """Queue a run of `silver.clean` → `gold.daily`.

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
    uuid.UUID
        The run's id.
    """
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[_BRONZE],
        tables=[TableRef("dev", _BRONZE, "events")],
    )
    _deployed(
        session,
        principal,
        _repo(session),
        "a" * 40,
        make_asset("silver.clean", ("bronze.events",)),
        make_asset("gold.daily", ("silver.clean",)),
    )
    return (
        RunService(session, AllowAllAuthorization())
        .start(principal, "dev", "analytics", Selection())
        .id
    )


def _task(session: Session, asset: str) -> TaskRun:
    """Return a task run by asset.

    Parameters
    ----------
    session
        Database session.
    asset
        Its asset.

    Returns
    -------
    TaskRun
        The row, refreshed.
    """
    session.expire_all()
    return session.scalars(select(TaskRun).where(TaskRun.asset == asset)).one()


def test_snapshot_mirrors_the_rows(session: Session, run_id: uuid.UUID) -> None:
    """Only active runs are loaded; tasks carry their upstreams."""
    (snapshot,) = load_snapshots(session)
    assert (snapshot.id, snapshot.status, snapshot.cancel_requested) == (
        run_id,
        RunStatus.QUEUED,
        False,
    )
    upstreams = {t.asset: t.upstream for t in snapshot.tasks}
    assert upstreams == {"gold.daily": ("silver.clean",), "silver.clean": ()}


def test_a_tick_starts_the_run_and_readies_the_root(session: Session, run_id: uuid.UUID) -> None:
    """End to end over the database: queued → running, root task ready."""
    assert scheduler_tick(session, NOW, _half) == 2
    run = session.get(Run, run_id)
    assert run is not None
    session.refresh(run)
    assert (run.status, run.started_at) == (RunStatus.RUNNING, NOW)
    assert _task(session, "silver.clean").status is TaskStatus.READY
    assert scheduler_tick(session, NOW, _half) == 0


def test_a_lost_lease_closes_the_open_attempt(session: Session, run_id: uuid.UUID) -> None:
    """Reclaiming records the attempt as lost and clears the lease."""
    scheduler_tick(session, NOW, _half)
    task = _task(session, "silver.clean")
    task.status = TaskStatus.RUNNING
    task.attempt = 1
    task.lease_owner = "w1"
    task.lease_expires_at = NOW - timedelta(seconds=1)
    session.add(
        TaskAttempt(id=uuid.uuid4(), task_run_id=task.id, attempt=1, worker="w1", started_at=NOW)
    )
    session.commit()
    scheduler_tick(session, NOW, _half)
    task = _task(session, "silver.clean")
    assert (task.status, task.lease_owner, task.lease_expires_at, task.attempt) == (
        TaskStatus.PENDING,
        None,
        None,
        1,
    )
    assert task.next_attempt_at == NOW + timedelta(seconds=30)
    attempt = session.scalars(select(TaskAttempt)).one()
    assert (attempt.outcome, attempt.finished_at, attempt.error_message) == (
        AttemptOutcome.LOST,
        NOW,
        LEASE_EXPIRED_MESSAGE,
    )


def test_a_lost_lease_fails_the_orphaned_write_run(session: Session, run_id: uuid.UUID) -> None:
    """The dead worker's RUNNING write run is failed with the fixed message, in the same tick."""
    scheduler_tick(session, NOW, _half)
    task = _task(session, "silver.clean")
    run = session.get(Run, run_id)
    assert run is not None
    task.status = TaskStatus.RUNNING
    task.attempt = 1
    task.lease_owner = "w1"
    task.lease_expires_at = NOW - timedelta(seconds=1)
    session.add(
        TaskAttempt(id=uuid.uuid4(), task_run_id=task.id, attempt=1, worker="w1", started_at=NOW)
    )
    write_id, other_id = uuid.uuid4(), uuid.uuid4()
    for query_run_id, status, task_run_id in (
        (write_id, QueryRunStatus.RUNNING, task.id),
        (other_id, QueryRunStatus.RUNNING, None),
    ):
        session.add(
            QueryRun(
                id=query_run_id,
                environment_id=run.environment_id,
                principal_id=run.run_as_principal_id,
                sql_text="SELECT 1",
                status=status,
                task_run_id=task_run_id,
            )
        )
    session.commit()
    scheduler_tick(session, NOW, _half)
    session.expire_all()
    write = session.get(QueryRun, write_id)
    other = session.get(QueryRun, other_id)
    assert write is not None and other is not None
    assert (write.status, write.error_message, write.finished_at) == (
        QueryRunStatus.FAILED,
        WORKER_LOST_WRITE_MESSAGE,
        NOW,
    )
    assert other.status is QueryRunStatus.RUNNING


def test_a_stale_snapshot_applies_nothing(session: Session, run_id: uuid.UUID) -> None:
    """One missed compare-and-swap rolls back that run's transitions."""
    task = _task(session, "silver.clean")
    transitions = [
        RunTransition(run_id, RunStatus.QUEUED, RunStatus.RUNNING),
        TaskTransition(run_id, task.id, TaskStatus.RUNNING, TaskStatus.READY),
    ]
    assert apply_transitions(session, transitions, NOW) is False
    run = session.get(Run, run_id)
    assert run is not None
    session.refresh(run)
    assert run.status is RunStatus.QUEUED


def test_a_reclaim_whose_owner_changed_is_stale(session: Session, run_id: uuid.UUID) -> None:
    """The lease owner is part of the compare-and-swap of a reclaim."""
    task = _task(session, "silver.clean")
    task.status = TaskStatus.RUNNING
    task.lease_owner = "w2"
    session.commit()
    stale = TaskTransition(
        run_id, task.id, TaskStatus.RUNNING, TaskStatus.PENDING, expected_owner="w1", lost=True
    )
    assert apply_transitions(session, [stale], NOW) is False


def test_finalization_records_run_finished_and_the_counts(
    session: Session, run_id: uuid.UUID, principal: PrincipalRef
) -> None:
    """A finished run gets `finished_at` and a RUN_FINISHED audit event with counts."""
    scheduler_tick(session, NOW, _half)
    for asset, status in (("silver.clean", TaskStatus.FAILED), ("gold.daily", TaskStatus.PENDING)):
        task = _task(session, asset)
        task.status = status
        session.commit()  # `_task` expires the session, which would discard a pending change
    scheduler_tick(session, NOW, _half)
    run = session.get(Run, run_id)
    assert run is not None
    session.refresh(run)
    assert (run.status, run.finished_at) == (RunStatus.FAILED, NOW)
    assert _task(session, "gold.daily").finished_at == NOW
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.RUN_FINISHED)
    ).one()
    assert event.detail == {
        "run_id": str(run_id),
        "repository": "analytics",
        "status": "failed",
        "counts": {"failed": 1, "upstream_failed": 1},
    }
    assert (event.principal_id, event.securable_environment) == (principal.id, "dev")


def _fail_a_running_job_run(session: Session, run_id: uuid.UUID, *, job: str | None) -> None:
    """Make the run a run of `job` that notifies, start it and fail its first task.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.
    job
        Its job; `None` leaves it ad hoc.
    """
    run = session.get(Run, run_id)
    assert run is not None
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    version.manifest_json = {
        **version.manifest_json,
        "jobs": {"daily": {"select": ["x.*"], "notify": {"webhook": "alerts", "format": "json"}}},
    }
    run.job = job
    session.commit()
    scheduler_tick(session, NOW, _half)
    for asset, status in (("silver.clean", TaskStatus.FAILED), ("gold.daily", TaskStatus.PENDING)):
        task = _task(session, asset)
        task.status = status
        session.commit()


def test_finalizing_a_job_run_queues_its_notification_in_the_same_transaction(
    session: Session, run_id: uuid.UUID
) -> None:
    """The row is written with the finished run: rolled back, neither exists.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.
    """
    _fail_a_running_job_run(session, run_id, job="daily")
    transition = RunTransition(
        run_id, RunStatus.RUNNING, RunStatus.FAILED, counts={"failed": 1, "upstream_failed": 1}
    )
    assert store._apply_run(session, transition, NOW) is True
    session.rollback()
    session.expire_all()
    run = session.get(Run, run_id)
    assert run is not None
    assert (run.status, run.finished_at) == (RunStatus.RUNNING, None)
    assert session.scalars(select(NotificationDelivery)).all() == []

    scheduler_tick(session, NOW, _half)
    session.expire_all()
    row = session.scalars(select(NotificationDelivery)).one()
    assert (row.run_id, row.job, row.event, row.format, row.secret_name) == (
        run_id,
        "daily",
        "failed",
        "json",
        "alerts",
    )
    assert (row.state, row.attempts, row.next_attempt_at) == (NotificationState.PENDING, 0, NOW)


def test_finalizing_twice_queues_one_notification(session: Session, run_id: uuid.UUID) -> None:
    _fail_a_running_job_run(session, run_id, job="daily")
    transition = RunTransition(
        run_id, RunStatus.RUNNING, RunStatus.FAILED, counts={"failed": 1, "upstream_failed": 1}
    )
    assert apply_transitions(session, [transition], NOW) is True
    assert apply_transitions(session, [transition], NOW) is False
    assert len(session.scalars(select(NotificationDelivery)).all()) == 1


def test_a_database_error_while_queuing_never_stops_the_run_from_finishing(
    session: Session,
    run_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The run ends, is audited, and nothing is queued; the log names the run and the job.

    Parameters
    ----------
    session
        Database session.
    run_id
        The run.
    monkeypatch
        Makes the queuing fail.
    caplog
        Captures the warning.
    """

    def fail(*_args: object) -> None:
        raise IntegrityError("INSERT", {}, Exception("secret-looking-detail"))

    _fail_a_running_job_run(session, run_id, job="daily")
    monkeypatch.setattr(store, "queue_notification", fail)
    with caplog.at_level("WARNING", logger=store.logger.name):
        scheduler_tick(session, NOW, _half)
    session.expire_all()
    run = session.get(Run, run_id)
    assert run is not None
    assert (run.status, run.finished_at) == (RunStatus.FAILED, NOW)
    assert (
        len(
            session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == AuditEventType.RUN_FINISHED)
            ).all()
        )
        == 1
    )
    assert session.scalars(select(NotificationDelivery)).all() == []
    message = " ".join(r.getMessage() for r in caplog.records)
    assert str(run_id) in message and "daily" in message
    assert "secret-looking-detail" not in message


def test_queuing_for_a_run_that_is_gone_does_nothing(session: Session) -> None:
    """A run deleted between the swap and the queuing leaves no row and raises nothing."""
    store._queue_notification(session, uuid.uuid4(), "daily", NOW)
    assert session.scalars(select(NotificationDelivery)).all() == []


def test_finalizing_an_ad_hoc_run_queues_nothing(session: Session, run_id: uuid.UUID) -> None:
    _fail_a_running_job_run(session, run_id, job=None)
    scheduler_tick(session, NOW, _half)
    run = session.get(Run, run_id)
    assert run is not None
    session.refresh(run)
    assert run.status is RunStatus.FAILED
    assert session.scalars(select(NotificationDelivery)).all() == []


def test_malformed_upstream_json_is_ignored(session: Session, run_id: uuid.UUID) -> None:
    """A non-list or non-string upstream value reads as no upstreams, never raises."""
    task = _task(session, "gold.daily")
    task.upstream_json = {"x": 1}  # ty: ignore[invalid-assignment]
    session.commit()
    (snapshot,) = load_snapshots(session)
    assert {t.asset: t.upstream for t in snapshot.tasks}["gold.daily"] == ()
    task = _task(session, "gold.daily")
    task.upstream_json = ["silver.clean", 3]
    session.commit()
    (snapshot,) = load_snapshots(session)
    assert {t.asset: t.upstream for t in snapshot.tasks}["gold.daily"] == ("silver.clean",)


def test_ready_transitions_persist_ready_at(session: Session, run_id: uuid.UUID) -> None:
    """The moment a task became ready is stored (the FIFO claim key)."""
    scheduler_tick(session, NOW, _half)
    task = _task(session, "silver.clean")
    assert (task.status, task.ready_at) == (TaskStatus.READY, NOW)
    assert _task(session, "gold.daily").ready_at is None


def test_a_miss_in_one_run_does_not_stall_the_others(session: Session, run_id: uuid.UUID) -> None:
    """Transitions apply per run: a stale run rolls back alone and reports False."""
    stale = RunTransition(uuid.uuid4(), RunStatus.QUEUED, RunStatus.RUNNING)
    good = RunTransition(run_id, RunStatus.QUEUED, RunStatus.RUNNING)
    assert apply_transitions(session, [stale, good], NOW) is False
    run = session.get(Run, run_id)
    assert run is not None
    session.refresh(run)
    assert run.status is RunStatus.RUNNING


def test_scheduler_tick_counts_only_what_was_applied(
    session: Session, run_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot gone stale for one run still counts the other run's transitions."""
    stale = RunTransition(uuid.uuid4(), RunStatus.QUEUED, RunStatus.RUNNING)
    good = RunTransition(run_id, RunStatus.QUEUED, RunStatus.RUNNING)
    monkeypatch.setattr(store, "tick", lambda *args: [stale, good])
    assert scheduler_tick(session, NOW, _half) == 1


def test_a_none_owner_means_expect_a_null_owner(session: Session, run_id: uuid.UUID) -> None:
    """`expected_owner=None` on a reclaim matches only a NULL owner; it is not 'any owner'."""
    task = _task(session, "silver.clean")
    task.status = TaskStatus.RUNNING
    task.lease_owner = "w1"
    session.commit()
    reclaim = TaskTransition(
        run_id, task.id, TaskStatus.RUNNING, TaskStatus.PENDING, expected_owner=None, lost=True
    )
    assert apply_transitions(session, [reclaim], NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING
    task = _task(session, "silver.clean")
    task.lease_owner = None
    session.commit()
    assert apply_transitions(session, [reclaim], NOW) is True
    assert _task(session, "silver.clean").status is TaskStatus.PENDING


def test_a_non_reclaim_does_not_check_the_owner(session: Session, run_id: uuid.UUID) -> None:
    """Without `lost` and without an expected owner, the lease owner is not compared."""
    task = _task(session, "silver.clean")
    task.status = TaskStatus.RUNNING
    task.lease_owner = "w1"
    session.commit()
    cancel = TaskTransition(run_id, task.id, TaskStatus.RUNNING, TaskStatus.CANCELLED)
    assert apply_transitions(session, [cancel], NOW) is True
    assert _task(session, "silver.clean").status is TaskStatus.CANCELLED


def test_a_lost_last_attempt_fails_the_task_with_its_message(
    session: Session, run_id: uuid.UUID
) -> None:
    """Reclaiming the last attempt records the failure message on the task."""
    scheduler_tick(session, NOW, _half)
    task = _task(session, "silver.clean")
    task.status = TaskStatus.RUNNING
    task.attempt = task.max_attempts
    task.lease_owner = "w1"
    task.lease_expires_at = NOW - timedelta(seconds=1)
    session.commit()
    scheduler_tick(session, NOW, _half)
    task = _task(session, "silver.clean")
    assert (task.status, task.error_message, task.finished_at) == (
        TaskStatus.FAILED,
        LOST_MESSAGE,
        NOW,
    )


def test_a_text_the_scheduler_writes_clears_the_check_message_state(
    session: Session, run_id: uuid.UUID
) -> None:
    """The state stored with a check phase's text never describes a text written over it."""
    scheduler_tick(session, NOW, _half)
    task = _task(session, "silver.clean")
    task.status = TaskStatus.RUNNING
    task.attempt = task.max_attempts
    task.lease_owner = "w1"
    task.lease_expires_at = NOW - timedelta(seconds=1)
    task.error_message = "1 check warned: `positive` (2 rows)."
    task.check_message_state = {"branch": "passed"}
    session.commit()
    scheduler_tick(session, NOW, _half)
    task = _task(session, "silver.clean")
    assert (task.error_message, task.check_message_state) == (LOST_MESSAGE, None)


def _running_with_open_attempt(session: Session, expires: datetime) -> TaskRun:
    """Put `silver.clean` in `running` under `w1` with an open attempt.

    Parameters
    ----------
    session
        Database session.
    expires
        The lease expiry to store.

    Returns
    -------
    TaskRun
        The task.
    """
    task = _task(session, "silver.clean")
    task.status = TaskStatus.RUNNING
    task.attempt = 1
    task.claim_count = 1
    task.lease_owner = "w1"
    task.lease_expires_at = expires
    session.add(
        TaskAttempt(id=uuid.uuid4(), task_run_id=task.id, attempt=1, worker="w1", started_at=NOW)
    )
    session.commit()
    return task


def test_a_reclaim_of_a_since_renewed_lease_is_stale(session: Session, run_id: uuid.UUID) -> None:
    """The snapshot saw an expired lease; w1 renewed before apply: nothing is reclaimed."""
    task = _running_with_open_attempt(session, NOW - timedelta(seconds=1))
    reclaim = TaskTransition(
        run_id, task.id, TaskStatus.RUNNING, TaskStatus.PENDING, expected_owner="w1", lost=True
    )
    task = _task(session, "silver.clean")
    task.lease_expires_at = NOW + timedelta(seconds=60)
    session.commit()
    assert apply_transitions(session, [reclaim], NOW) is False
    task = _task(session, "silver.clean")
    assert (task.status, task.lease_owner) == (TaskStatus.RUNNING, "w1")
    assert session.scalars(select(TaskAttempt)).one().finished_at is None


def test_a_stale_reclaim_does_not_close_a_reclaimed_attempt(
    session: Session, run_id: uuid.UUID
) -> None:
    """ABA: reclaimed, readied and claimed again by the same worker; the old reclaim misses."""
    task = _running_with_open_attempt(session, NOW - timedelta(seconds=1))
    reclaim = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.PENDING,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
    )
    assert apply_transitions(session, [reclaim], NOW) is True
    task = _task(session, "silver.clean")
    task.status = TaskStatus.RUNNING
    # An uncounted retry (a conflict) took the attempt back: the number repeats.
    task.attempt = 1
    task.claim_count = 2
    task.lease_owner = "w1"
    # Expired again (a skewed clock): only the claim counter tells the claims apart.
    task.lease_expires_at = NOW - timedelta(seconds=1)
    session.add(
        TaskAttempt(
            id=uuid.uuid4(),
            task_run_id=task.id,
            attempt=1,
            worker="w1",
            started_at=NOW + timedelta(seconds=1),
        )
    )
    session.commit()
    assert apply_transitions(session, [reclaim], NOW) is False
    attempts = session.scalars(select(TaskAttempt).order_by(TaskAttempt.started_at)).all()
    assert [a.finished_at is None for a in attempts] == [False, True]


def test_snapshots_load_in_a_fixed_number_of_queries(session: Session, run_id: uuid.UUID) -> None:
    """One query for the runs and one for all their tasks, however many runs are active."""
    first = session.get(Run, run_id)
    assert first is not None
    tasks = session.scalars(select(TaskRun).where(TaskRun.run_id == run_id)).all()
    for offset in (1, 2):
        repo = Repository(
            id=uuid.uuid4(), name=f"r{offset}", kind=RepositoryKind.HTTPS, url="https://h/r.git"
        )
        session.add(repo)
        session.flush()
        clone = Run(
            id=uuid.uuid4(),
            environment_id=first.environment_id,
            repository_id=repo.id,
            job_version_id=first.job_version_id,
            kind=first.kind,
            status=RunStatus.RUNNING,
            selection_json={},
            created_at=first.created_at + timedelta(seconds=offset),
        )
        session.add(clone)
        session.flush()
        for task in tasks:
            session.add(
                TaskRun(
                    id=uuid.uuid4(),
                    run_id=clone.id,
                    asset=task.asset,
                    status=TaskStatus.PENDING,
                    max_attempts=1,
                    timeout_seconds=60,
                    retry_delay_seconds=0,
                    upstream_json=task.upstream_json,
                )
            )
    session.commit()
    statements: list[str] = []

    def count(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        snapshots = load_snapshots(session)
    finally:
        event.remove(bind, "before_cursor_execute", count)
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 2
    assert snapshots[0].id == run_id
    assert [s.status for s in snapshots] == [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.RUNNING]
    assert [[t.asset for t in s.tasks] for s in snapshots] == [["gold.daily", "silver.clean"]] * 3


def test_an_active_run_without_tasks_still_has_a_snapshot(
    session: Session, run_id: uuid.UUID
) -> None:
    """The batched task query must not drop a run it returns no rows for."""
    session.execute(delete(TaskRun).where(TaskRun.run_id == run_id))
    session.commit()
    (snapshot,) = load_snapshots(session)
    assert (snapshot.id, snapshot.tasks) == (run_id, ())


def test_a_second_tick_at_the_same_instant_changes_nothing(
    session: Session, run_id: uuid.UUID
) -> None:
    """Tick idempotence: applied transitions leave nothing for the same snapshot time."""
    assert scheduler_tick(session, NOW, _half) == 2
    before = [(t.asset, t.status, t.ready_at) for t in session.scalars(select(TaskRun))]
    assert scheduler_tick(session, NOW, _half) == 0
    session.expire_all()
    assert [(t.asset, t.status, t.ready_at) for t in session.scalars(select(TaskRun))] == before


def test_a_missed_reclaim_leaves_the_tasks_write_run_running(
    session: Session, run_id: uuid.UUID
) -> None:
    """The negative of the orphaned-write stamp: a reclaim that misses must not fail the write."""
    task = _running_with_open_attempt(session, NOW - timedelta(seconds=1))
    run = session.get(Run, run_id)
    assert run is not None
    write_id = uuid.uuid4()
    session.add(
        QueryRun(
            id=write_id,
            environment_id=run.environment_id,
            principal_id=run.run_as_principal_id,
            sql_text="SELECT 1",
            status=QueryRunStatus.RUNNING,
            task_run_id=task.id,
        )
    )
    session.commit()
    reclaim = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.PENDING,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
    )
    task = _task(session, "silver.clean")
    task.lease_expires_at = NOW + timedelta(seconds=60)
    session.commit()
    assert apply_transitions(session, [reclaim], NOW) is False
    session.expire_all()
    write = session.get(QueryRun, write_id)
    assert write is not None
    assert (write.status, write.error_message, write.finished_at) == (
        QueryRunStatus.RUNNING,
        None,
        None,
    )


def test_no_active_run_means_no_snapshot_and_no_task_query(
    session: Session, run_id: uuid.UUID
) -> None:
    """A finished run is not loaded, and its tasks are not queried."""
    run = session.get(Run, run_id)
    assert run is not None
    run.status = RunStatus.SUCCEEDED
    session.commit()
    statements: list[str] = []

    def count(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        assert load_snapshots(session) == []
    finally:
        event.remove(bind, "before_cursor_execute", count)
    assert not [s for s in statements if "task_runs" in s]


def test_the_lost_worker_messages_name_the_likeliest_cause_and_the_next_step() -> None:
    """A worker killed for memory leaves only these two lines for the user to read."""
    for message in (LEASE_EXPIRED_MESSAGE, WORKER_LOST_WRITE_MESSAGE, LOST_MESSAGE):
        assert "LAKANA_WORKER__MEMORY_LIMIT" in message
        assert "LAKANA_WORKER_MEMORY in the Compose bundle's .env" in message
        assert "too much memory" in message
    assert LEASE_EXPIRED_MESSAGE.startswith("lease expired: the worker stopped renewing it")


@pytest.fixture
def environment_id(session: Session) -> uuid.UUID:
    """Insert one environment and return its id.

    Parameters
    ----------
    session
        Database session to insert into.

    Returns
    -------
    uuid.UUID
        The environment's id.
    """
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana-test",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(env)
    session.flush()
    return env.id


@pytest.fixture
def principal_id(principal: PrincipalRef) -> uuid.UUID:
    """Return the id of the persisted principal.

    Parameters
    ----------
    principal
        The persisted admin.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    return principal.id


def _run(session: Session, environment_id: uuid.UUID, **fields: object) -> QueryRun:
    """Insert a `RUNNING` query run, with the given fields over the defaults.

    Parameters
    ----------
    session
        Database session to insert into.
    environment_id
        The run's environment.
    **fields
        Column values that override the defaults.

    Returns
    -------
    QueryRun
        The flushed run.
    """
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "environment_id": environment_id,
        "sql_text": "SELECT 1",
        "status": QueryRunStatus.RUNNING,
    }
    values.update(fields)
    run = QueryRun(**values)
    session.add(run)
    session.flush()
    return run


def _worker(session: Session, worker_id: str, age_seconds: int) -> None:
    """Insert a worker whose last heartbeat is that old by the database's clock.

    Parameters
    ----------
    session
        Database session to insert into.
    worker_id
        The worker's id.
    age_seconds
        Age of the heartbeat.
    """
    beat = db_now(session) - timedelta(seconds=age_seconds)
    session.add(Worker(id=worker_id, host="h", version="1", started_at=beat, heartbeat_at=beat))
    session.flush()


def _events(session: Session, event_type: AuditEventType) -> list[AuditEvent]:
    """Return the audit events of one type, oldest first.

    Parameters
    ----------
    session
        Database session.
    event_type
        The type to select.

    Returns
    -------
    list[AuditEvent]
        Matching events in time order.
    """
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == event_type)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )


def _stored(session: Session, run: QueryRun) -> QueryRun:
    """Reload a run from the database.

    Parameters
    ----------
    session
        Database session.
    run
        The run.

    Returns
    -------
    QueryRun
        The stored row.
    """
    session.expire_all()
    return session.get(QueryRun, run.id) or run


@pytest.mark.parametrize(
    ("fields", "status", "message", "audited"),
    [
        ({}, "FAILED", QUERY_LOST_MESSAGE, True),
        (
            {"write_mode": WriteMode.APPEND, "target_name": "sales.orders"},
            "FAILED",
            "The worker running this write was lost before it was committed, so the write was "
            "ended. Nothing was written to sales.orders. Run it again.",
            True,
        ),
        (
            {"write_mode": WriteMode.MAINTAIN, "target_name": "sales.orders"},
            "FAILED",
            "The worker running this maintenance was lost. Part of it may be done; running it "
            "again is safe.",
            True,
        ),
        (
            {"write_mode": WriteMode.APPEND, "target_name": "s.o", "rows_written": 3},
            "SUCCEEDED",
            None,
            True,
        ),
        (
            {"write_mode": WriteMode.APPEND, "target_name": "s.o", "rows_written": 0},
            "SUCCEEDED",
            None,
            True,
        ),
        (
            {"write_mode": WriteMode.VIEW, "target_name": "s.v", "rows_written": 0},
            "SUCCEEDED",
            None,
            True,
        ),
    ],
)
def test_a_lost_run_ends_by_its_kind(
    session: Session,
    environment_id: uuid.UUID,
    principal_id: uuid.UUID,
    fields: dict[str, object],
    status: str,
    message: str | None,
    audited: bool,
) -> None:
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    run = _run(session, environment_id, worker_id="dead", principal_id=principal_id, **fields)
    session.commit()
    assert end_lost_runs(session, now, 60) == 1
    session.refresh(run)
    assert (run.status.value, run.error_message, run.finished_at) == (status, message, now)
    assert run.error_cause == (None if status == "SUCCEEDED" else "worker_lost")
    events = _events(session, AuditEventType.QUERY_COMPLETED)
    assert [e.detail["status"] for e in events] == [status]
    assert events[0].detail == {
        "sql_text": "SELECT 1",
        "tables_touched": [],
        "row_count": fields.get("rows_written") if status == "SUCCEEDED" else None,
        "status": status,
    }


def test_the_event_of_a_lost_query_that_showed_the_rows_of_a_check_names_the_result(
    session: Session, environment_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    """A read of a checked snapshot is told from an ordinary query in the trail, also when lost."""
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    result_id = uuid.UUID("5d1e7c0a-9b1f-4a3e-8c2d-7f6b5a4c3d2e")
    _run(
        session,
        environment_id,
        worker_id="dead",
        principal_id=principal_id,
        check_result_id=result_id,
    )
    session.commit()

    assert end_lost_runs(session, now, 60) == 1

    events = _events(session, AuditEventType.QUERY_COMPLETED)
    assert [e.detail for e in events] == [
        {
            "sql_text": "SELECT 1",
            "tables_touched": [],
            "row_count": None,
            "status": "FAILED",
            "check_result_id": "5d1e7c0a-9b1f-4a3e-8c2d-7f6b5a4c3d2e",
        }
    ]


def test_the_sweep_never_writes_the_evidence_and_adds_no_details(
    session: Session, environment_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    """R4.7.9 and R4.7.10: a swept SUCCEEDED run has no watermark, diff or lineage."""
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    table_id = uuid.uuid4()
    run = _run(
        session,
        environment_id,
        worker_id="dead",
        principal_id=principal_id,
        write_mode=WriteMode.APPEND,
        target_name="s.o",
        rows_written=3,
        snapshot_id=7,
        target_table_id=table_id,
    )
    open_ = _run(
        session,
        environment_id,
        worker_id="dead",
        principal_id=principal_id,
        write_mode=WriteMode.APPEND,
        target_name="s.o",
        target_table_id=table_id,
    )
    session.commit()
    assert end_lost_runs(session, now, 60) == 2
    done, failed = _stored(session, run), _stored(session, open_)
    assert (done.rows_written, done.snapshot_id, done.target_table_id) == (3, 7, table_id)
    assert (done.watermark, done.schema_diff, done.lineage_json) == (None, None, None)
    assert done.finished_at is not None
    assert (failed.rows_written, failed.snapshot_id, failed.target_table_id) == (
        None,
        None,
        table_id,
    )


def test_a_heartbeat_that_arrives_before_the_update_keeps_the_run(
    session: Session, environment_id: uuid.UUID
) -> None:
    now = db_now(session)
    _worker(session, "slow", age_seconds=61)
    run = _run(session, environment_id, worker_id="slow")
    session.commit()
    (candidate,) = store._lost_query_runs(session, now, 60)
    workers.beat(session, "slow")  # between the read and the update
    assert store._end_query_run(session, candidate, now, 60) is False
    session.commit()
    session.refresh(run)
    assert run.status is QueryRunStatus.RUNNING


def test_evidence_that_arrives_before_the_update_keeps_the_run_for_the_next_tick(
    session: Session, environment_id: uuid.UUID
) -> None:
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    run = _run(
        session, environment_id, worker_id="dead", write_mode=WriteMode.APPEND, target_name="s.o"
    )
    session.commit()
    (candidate,) = store._lost_query_runs(session, now, 60)
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run.id)
        .values(rows_written=2)
        .execution_options(synchronize_session=False)  # the candidate must not see it (R4.2)
    )
    assert candidate.rows_written is None
    assert store._end_query_run(session, candidate, now, 60) is False  # not FAILED over a commit
    session.commit()
    assert end_lost_runs(session, now, 60) == 1
    session.refresh(run)
    assert run.status is QueryRunStatus.SUCCEEDED


def test_a_run_that_ended_before_the_update_is_not_touched(
    session: Session, environment_id: uuid.UUID
) -> None:
    """R4.7.1: the status stands in the UPDATE's WHERE (an ended or queued run is not touched)."""
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    run = _run(session, environment_id, worker_id="dead")
    session.commit()
    (candidate,) = store._lost_query_runs(session, now, 60)
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run.id)
        .values(status=QueryRunStatus.CANCELLED, error_message="kept")
        .execution_options(synchronize_session=False)
    )
    assert store._end_query_run(session, candidate, now, 60) is False
    session.commit()
    stored = _stored(session, run)
    assert (stored.status, stored.error_message) == (QueryRunStatus.CANCELLED, "kept")
    assert _events(session, AuditEventType.QUERY_COMPLETED) == []


def test_the_run_of_an_alive_worker_is_never_ended(
    session: Session, environment_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    now = db_now(session)
    _worker(session, "alive", age_seconds=20)
    _worker(session, "dead", age_seconds=600)
    alive = _run(session, environment_id, worker_id="alive", principal_id=principal_id)
    others = [
        _run(session, environment_id, worker_id="dead", status=status)
        for status in (QueryRunStatus.QUEUED, QueryRunStatus.SUCCEEDED, QueryRunStatus.FAILED)
    ]
    queued_old = _run(
        session,
        environment_id,
        status=QueryRunStatus.QUEUED,
        created_at=now - timedelta(seconds=600),
    )
    session.commit()
    assert end_lost_runs(session, now, 60) == 0
    for run in (alive, *others, queued_old):
        assert _stored(session, run).status is run.status
    assert _events(session, AuditEventType.QUERY_COMPLETED) == []


def test_a_running_run_without_a_worker_id_is_ended_once_it_started_a_lease_ago(
    session: Session, environment_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    now = db_now(session)
    run = _run(session, environment_id, worker_id=None, started_at=now - timedelta(seconds=60))
    session.commit()
    assert end_lost_runs(session, now, 60) == 1
    assert _stored(session, run).status is QueryRunStatus.FAILED
    (line,) = [r for r in caplog.records if getattr(r, "event", None) == "worker_lost"]
    assert "Ended 1 query or deploy run(s)" in line.getMessage() and "60 s" in line.getMessage()


def test_a_sweep_that_ends_nothing_logs_nothing(
    session: Session, environment_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    assert end_lost_runs(session, db_now(session), 60) == 0
    assert caplog.records == []


def test_a_running_run_without_a_worker_id_is_kept_before_that(
    session: Session, environment_id: uuid.UUID
) -> None:
    now = db_now(session)
    run = _run(session, environment_id, worker_id=None, started_at=now - timedelta(seconds=59))
    session.commit()
    assert end_lost_runs(session, now, 60) == 0
    assert _stored(session, run).status is QueryRunStatus.RUNNING


def test_the_sweeps_clock_is_the_databases(session: Session, environment_id: uuid.UUID) -> None:
    """R4.3: the age of a run without a worker id is judged at the `now` the tick was given.

    The tick's `now` comes from `db_now` in production; a worker clock that runs ahead cannot
    make a young run old, because the run's own `started_at` is the only worker-side value.
    """
    now = db_now(session)
    run = _run(session, environment_id, worker_id=None, started_at=now - timedelta(seconds=30))
    session.commit()
    assert end_lost_runs(session, now, 60) == 0
    assert end_lost_runs(session, now + timedelta(seconds=31), 60) == 1
    assert _stored(session, run).status is QueryRunStatus.FAILED


def test_a_run_whose_workers_row_is_gone_is_ended_at_once(
    session: Session, environment_id: uuid.UUID
) -> None:
    now = db_now(session)
    run = _run(session, environment_id, worker_id="gone", started_at=now)
    session.commit()
    assert end_lost_runs(session, now, 60) == 1
    assert _stored(session, run).status is QueryRunStatus.FAILED


def _task_write(
    session: Session, run_id: uuid.UUID, token: uuid.UUID | None, **fields: object
) -> QueryRun:
    """Insert a `RUNNING` query run bound to `silver.clean`'s task run, of a dead worker.

    Parameters
    ----------
    session
        Database session.
    run_id
        The pipeline run.
    token
        The run's `task_token_id` (`None` for a SQL task's own write).
    **fields
        More column values.

    Returns
    -------
    QueryRun
        The committed run.
    """
    pipeline = session.get(Run, run_id)
    assert pipeline is not None
    task = _task(session, "silver.clean")
    if session.get(Worker, "dead") is None:
        _worker(session, "dead", age_seconds=61)
    run = _run(
        session,
        pipeline.environment_id,
        worker_id="dead",
        principal_id=pipeline.run_as_principal_id,
        task_run_id=task.id,
        task_token_id=token,
        **fields,
    )
    if token is None:  # a SQL task's own write is the task's current write
        task.query_run_id = run.id
    session.commit()
    return run


def test_a_sql_tasks_own_write_without_a_live_lease_ends_by_the_evidence(
    session: Session, run_id: uuid.UUID
) -> None:
    """Without evidence the task's message; with evidence SUCCEEDED. Never an event."""
    now = db_now(session)
    plain = _task_write(session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o")
    committed = _task_write(
        session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o", rows_written=4
    )
    assert end_lost_runs(session, now, 60) == 2
    stored = _stored(session, plain)
    assert (stored.status, stored.error_cause, stored.error_message) == (
        QueryRunStatus.FAILED,
        "worker_lost",
        WORKER_LOST_WRITE_MESSAGE,
    )
    stored = _stored(session, committed)
    assert (stored.status, stored.error_cause, stored.error_message, stored.rows_written) == (
        QueryRunStatus.SUCCEEDED,
        None,
        None,
        4,
    )
    assert _events(session, AuditEventType.QUERY_COMPLETED) == []


def test_the_query_of_a_python_task_is_ended_like_any_other(
    session: Session, run_id: uuid.UUID
) -> None:
    now = db_now(session)
    run = _task_write(session, run_id, uuid.uuid4())
    assert end_lost_runs(session, now, 60) == 1
    stored = _stored(session, run)
    assert (stored.status, stored.error_message) == (QueryRunStatus.FAILED, QUERY_LOST_MESSAGE)
    assert len(_events(session, AuditEventType.QUERY_COMPLETED)) == 1


def test_a_run_of_a_lost_task_is_ended_once_by_the_task_path_then_the_sweep_finds_nothing(
    session: Session, run_id: uuid.UUID
) -> None:
    """R4.7.8: `_apply_task` owns the rows of a task whose lease is lost; the sweep skips them."""
    scheduler_tick(session, NOW, _half)
    task = _task(session, "silver.clean")
    task.status = TaskStatus.RUNNING
    task.attempt = 1
    task.claim_count = 1
    task.lease_owner = "w1"
    task.lease_expires_at = NOW - timedelta(seconds=1)
    session.add(
        TaskAttempt(id=uuid.uuid4(), task_run_id=task.id, attempt=1, worker="w1", started_at=NOW)
    )
    session.commit()
    own = _task_write(session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o")
    python = _task_write_second(session, run_id)
    now = db_now(session)
    assert scheduler_tick(session, now, _half, 60) >= 1
    first, second = _stored(session, own), _stored(session, python)
    assert (first.status, first.error_cause, first.error_message) == (
        QueryRunStatus.FAILED,
        None,
        WORKER_LOST_WRITE_MESSAGE,
    )
    assert second.status is QueryRunStatus.FAILED
    assert second.error_message == WORKER_LOST_WRITE_MESSAGE
    assert _events(session, AuditEventType.QUERY_COMPLETED) == []


def _task_write_second(session: Session, run_id: uuid.UUID) -> QueryRun:
    """Insert a Python task's input query of the same task run, of the same dead worker.

    Parameters
    ----------
    session
        Database session.
    run_id
        The pipeline run.

    Returns
    -------
    QueryRun
        The committed run.
    """
    first = session.scalars(select(QueryRun).where(QueryRun.task_run_id.is_not(None))).first()
    assert first is not None
    run = _run(
        session,
        first.environment_id,
        worker_id="dead",
        principal_id=first.principal_id,
        task_run_id=first.task_run_id,
        task_token_id=uuid.uuid4(),
    )
    session.commit()
    return run


def test_a_lost_run_of_a_deleted_principal_is_ended_without_an_event(
    session: Session, environment_id: uuid.UUID
) -> None:
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    run = _run(session, environment_id, worker_id="dead", principal_id=None)
    session.commit()
    assert end_lost_runs(session, now, 60) == 1
    assert _stored(session, run).status is QueryRunStatus.FAILED
    assert _events(session, AuditEventType.QUERY_COMPLETED) == []


def test_a_living_worker_counted_lost_is_ended_cleanly_and_its_heartbeat_changes_nothing(
    session: Session, environment_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    """R4.5 a, R4.7.11: a seconds-old run of a worker the database sees as too old.

    The worker's heartbeat arrives afterwards: the run keeps its one final status, and the
    next tick adds no second event.
    """
    now = db_now(session)
    _worker(session, "slow", age_seconds=61)
    run = _run(session, environment_id, worker_id="slow", principal_id=principal_id, started_at=now)
    session.commit()
    assert end_lost_runs(session, now, 60) == 1
    workers.beat(session, "slow")
    session.commit()
    assert end_lost_runs(session, now + timedelta(seconds=120), 60) == 0
    stored = _stored(session, run)
    assert (stored.status, stored.error_cause) == (QueryRunStatus.FAILED, "worker_lost")
    assert len(_events(session, AuditEventType.QUERY_COMPLETED)) == 1


def test_every_lost_message_says_what_happened_and_to_run_it_again() -> None:
    for message in (QUERY_LOST_MESSAGE, WRITE_LOST_MESSAGE, MAINTENANCE_LOST_MESSAGE):
        assert "was lost" in message
        assert message.endswith(("Run it again.", "running it again is safe."))


def test_lost_outcome_is_a_pure_table() -> None:
    def outcome(**fields: object) -> LostOutcome:
        values: dict[str, object] = {
            "write_mode": None,
            "target_name": None,
            "rows_written": None,
            "task_write": False,
        }
        values.update(fields)
        return lost_outcome(**values)  # ty: ignore[invalid-argument-type]

    failed = QueryRunStatus.FAILED
    assert outcome() == LostOutcome(failed, QUERY_LOST_MESSAGE, True)
    assert outcome(write_mode=WriteMode.CREATE, target_name="a.b") == LostOutcome(
        failed, WRITE_LOST_MESSAGE.format(target="a.b"), True
    )
    assert outcome(write_mode=WriteMode.MAINTAIN) == LostOutcome(
        failed, MAINTENANCE_LOST_MESSAGE, True
    )
    for mode in (WriteMode.APPEND, WriteMode.VIEW):
        assert outcome(write_mode=mode, rows_written=0) == LostOutcome(
            QueryRunStatus.SUCCEEDED, None, True
        )
    # A maintenance run is never "committed" by this evidence.
    assert outcome(write_mode=WriteMode.MAINTAIN, rows_written=1).status is failed
    # A task's own write: the lease message without evidence, SUCCEEDED with it; no event.
    assert outcome(task_write=True, write_mode=WriteMode.APPEND) == LostOutcome(
        failed, WORKER_LOST_WRITE_MESSAGE, False
    )
    assert outcome(task_write=True, write_mode=WriteMode.APPEND, rows_written=0) == LostOutcome(
        QueryRunStatus.SUCCEEDED, None, False
    )
    # The count of a check is a task's own run, and not its write: a text of its own.
    assert outcome(task_write=True, check=True) == LostOutcome(failed, CHECK_LOST_MESSAGE, False)
    assert CHECK_LOST_MESSAGE == (
        "The worker counting this check of a pipeline task was lost, so the count was ended. "
        "The check is counted again when the task is taken again."
    )
    assert "write" not in CHECK_LOST_MESSAGE


def test_the_count_of_a_check_is_swept_with_its_own_text_and_a_write_with_the_writes(
    session: Session, run_id: uuid.UUID
) -> None:
    """A count has no write mode and is not the task's `query_run_id`; a write keeps its text."""
    _running_task(session, lease_left=50)
    task = _task(session, "silver.clean")
    pipeline = session.get(Run, run_id)
    assert pipeline is not None
    _worker(session, "dead", age_seconds=61)
    count = _run(
        session,
        pipeline.environment_id,
        worker_id="dead",
        principal_id=pipeline.run_as_principal_id,
        task_run_id=task.id,
    )
    session.commit()
    assert task.query_run_id is None

    assert end_lost_runs(session, db_now(session), 60) == 1

    stored = _stored(session, count)
    assert (stored.status, stored.error_cause, stored.error_message) == (
        QueryRunStatus.FAILED,
        "worker_lost",
        CHECK_LOST_MESSAGE,
    )
    assert _events(session, AuditEventType.QUERY_COMPLETED) == []


def test_the_sweep_takes_a_bounded_batch_oldest_first(
    session: Session, environment_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert SWEEP_BATCH == 100
    monkeypatch.setattr(store, "SWEEP_BATCH", 2)
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    runs = [
        _run(
            session,
            environment_id,
            worker_id="dead",
            created_at=now - timedelta(seconds=300 - 10 * i),
        )
        for i in range(3)
    ]
    session.commit()
    assert end_lost_runs(session, now, 60) == 2
    assert [_stored(session, r).status for r in runs] == [
        QueryRunStatus.FAILED,
        QueryRunStatus.FAILED,
        QueryRunStatus.RUNNING,
    ]
    assert end_lost_runs(session, now, 60) == 1


def test_the_sweep_passes_a_locked_run_by() -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    statement = store._lost_query_runs_statement(now, 60)
    assert (
        str(statement.compile(dialect=postgresql.dialect()))
        .rstrip()
        .endswith("FOR NO KEY UPDATE OF query_runs SKIP LOCKED")
    )


def test_scheduler_tick_sweeps_when_given_a_lease_also_without_a_transition(
    session: Session, environment_id: uuid.UUID
) -> None:
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    run = _run(session, environment_id, worker_id="dead")
    session.commit()
    assert scheduler_tick(session, now, _half, 60) == 0
    assert _stored(session, run).status is QueryRunStatus.FAILED


def test_scheduler_tick_without_a_lease_ends_nothing(
    session: Session, environment_id: uuid.UUID
) -> None:
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    run = _run(session, environment_id, worker_id="dead")
    session.commit()
    assert scheduler_tick(session, now, _half) == 0
    assert _stored(session, run).status is QueryRunStatus.RUNNING


def test_scheduler_tick_forgets_workers_silent_for_an_hour(session: Session) -> None:
    now = db_now(session)
    _worker(session, "old", age_seconds=3601)
    _worker(session, "recent", age_seconds=3500)
    session.commit()
    scheduler_tick(session, now, _half, 60)
    assert set(session.scalars(select(Worker.id))) == {"recent"}


def test_scheduler_tick_forgets_with_the_lease_not_below_it(session: Session) -> None:
    """A lease longer than an hour must not delete the row of a worker that still counts alive."""
    now = db_now(session)
    _worker(session, "stalled", age_seconds=3700)
    session.commit()
    scheduler_tick(session, now, _half, 7200)
    assert set(session.scalars(select(Worker.id))) == {"stalled"}


def test_runs_of_alive_workers_do_not_fill_the_batch(
    session: Session, environment_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The candidate read already leaves out the alive: they must not starve the lost."""
    monkeypatch.setattr(store, "SWEEP_BATCH", 1)
    now = db_now(session)
    _worker(session, "alive", age_seconds=1)
    _worker(session, "dead", age_seconds=61)
    old = now - timedelta(seconds=300)
    _run(session, environment_id, worker_id="alive", created_at=old)
    lost = _run(session, environment_id, worker_id="dead", created_at=now)
    session.commit()
    assert end_lost_runs(session, now, 60) == 1
    assert _stored(session, lost).status is QueryRunStatus.FAILED


def _deploy_run(session: Session, environment_id: uuid.UUID, **fields: object) -> DeployRun:
    """Insert a `RUNNING` deploy run, with the given fields over the defaults.

    Parameters
    ----------
    session
        Database session to insert into.
    environment_id
        The run's environment.
    **fields
        Column values that override the defaults.

    Returns
    -------
    DeployRun
        The flushed run.
    """
    repository = session.scalars(select(Repository)).first()
    if repository is None:
        repository = Repository(
            id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a"
        )
        session.add(repository)
        session.flush()
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "environment_id": environment_id,
        "repository_id": repository.id,
        "kind": DeployKind.DEPLOY,
        "status": DeployStatus.RUNNING,
    }
    values.update(fields)
    run = DeployRun(**values)
    session.add(run)
    session.flush()
    return run


def _stored_deploy(session: Session, run: DeployRun) -> DeployRun:
    """Reload a deploy run from the database.

    Parameters
    ----------
    session
        Database session.
    run
        The run.

    Returns
    -------
    DeployRun
        The stored row.
    """
    session.expire_all()
    return session.get(DeployRun, run.id) or run


def test_a_lost_deploy_run_is_failed_with_its_message(
    session: Session, environment_id: uuid.UUID
) -> None:
    """The sweep fails a `running` deploy no alive worker holds: message, finish time, no event."""
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    run = _deploy_run(session, environment_id, worker_id="dead", started_at=now)
    session.commit()
    assert end_lost_runs(session, now, 60) == 1
    stored = _stored_deploy(session, run)
    assert (stored.status, stored.error_message) == (DeployStatus.FAILED, DEPLOY_LOST_MESSAGE)
    assert stored.finished_at is not None
    assert DEPLOY_LOST_MESSAGE == (
        "The worker running this deploy was lost, so the deploy was ended. "
        "Nothing was activated. Deploy again."
    )
    assert session.query(AuditEvent).count() == 0


def test_the_deploy_of_an_alive_worker_is_never_ended(
    session: Session, environment_id: uuid.UUID
) -> None:
    """An alive worker's deploy, and the queued, succeeded and failed ones, stay as they are."""
    now = db_now(session)
    _worker(session, "alive", age_seconds=20)
    _worker(session, "dead", age_seconds=600)
    alive = _deploy_run(session, environment_id, worker_id="alive", started_at=now)
    others = [
        _deploy_run(session, environment_id, worker_id="dead", status=status)
        for status in (DeployStatus.QUEUED, DeployStatus.SUCCEEDED, DeployStatus.FAILED)
    ]
    queued_old = _deploy_run(
        session,
        environment_id,
        status=DeployStatus.QUEUED,
        created_at=now - timedelta(seconds=600),
    )
    session.commit()
    assert end_lost_runs(session, now, 60) == 0
    for run in (alive, *others, queued_old):
        assert _stored_deploy(session, run).status is run.status
        assert _stored_deploy(session, run).error_message is None


def test_a_running_deploy_without_a_worker_id_is_ended_once_it_started_a_lease_ago(
    session: Session, environment_id: uuid.UUID
) -> None:
    """A run from before the upgrade has no id: it is judged by its age by the database's clock."""
    now = db_now(session)
    young = _deploy_run(session, environment_id, started_at=now - timedelta(seconds=59))
    old = _deploy_run(session, environment_id, started_at=now - timedelta(seconds=60))
    session.commit()
    assert end_lost_runs(session, now, 60) == 1
    assert _stored_deploy(session, young).status is DeployStatus.RUNNING
    assert _stored_deploy(session, old).status is DeployStatus.FAILED


def test_a_heartbeat_before_the_update_keeps_the_deploy(
    session: Session, environment_id: uuid.UUID
) -> None:
    """The statement that ends a deploy repeats "no alive worker": a late beat saves the run."""
    now = db_now(session)
    _worker(session, "slow", age_seconds=61)
    run = _deploy_run(session, environment_id, worker_id="slow", started_at=now)
    session.commit()
    (candidate,) = store._lost_deploy_runs(session, now, 60)
    workers.beat(session, "slow")  # between the read and the update
    assert store._end_deploy_run(session, candidate, now, 60) is False
    session.commit()
    assert _stored_deploy(session, run).status is DeployStatus.RUNNING


def test_a_deploy_that_ended_before_the_update_is_not_touched(
    session: Session, environment_id: uuid.UUID
) -> None:
    """The status stands in the statement's WHERE: a deploy that finished meanwhile is kept."""
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    run = _deploy_run(session, environment_id, worker_id="dead", started_at=now)
    session.commit()
    (candidate,) = store._lost_deploy_runs(session, now, 60)
    session.execute(
        update(DeployRun)
        .where(DeployRun.id == run.id)
        .values(status=DeployStatus.SUCCEEDED)
        .execution_options(synchronize_session=False)
    )
    assert store._end_deploy_run(session, candidate, now, 60) is False
    session.commit()
    stored = _stored_deploy(session, run)
    assert (stored.status, stored.error_message) == (DeployStatus.SUCCEEDED, None)


def test_the_sweep_of_deploys_passes_a_locked_run_by() -> None:
    """The candidates are read `FOR NO KEY UPDATE`, skipping a row a finishing deploy holds."""
    now = datetime(2026, 10, 9, tzinfo=UTC)
    statement = store._lost_deploy_runs_statement(now, 60)
    assert (
        str(statement.compile(dialect=postgresql.dialect()))
        .rstrip()
        .endswith("FOR NO KEY UPDATE OF deploy_runs SKIP LOCKED")
    )


def test_the_batch_counts_query_and_deploy_runs_alike(
    session: Session, environment_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch is per kind of run; the count adds both."""
    monkeypatch.setattr(store, "SWEEP_BATCH", 1)
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    _run(session, environment_id, worker_id="dead")
    _deploy_run(session, environment_id, worker_id="dead", started_at=now)
    session.commit()
    assert end_lost_runs(session, now, 60) == 2


def test_scheduler_tick_ends_a_lost_deploy(session: Session, environment_id: uuid.UUID) -> None:
    """The tick that sweeps queries sweeps deploys; without a lease it sweeps neither."""
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    run = _deploy_run(session, environment_id, worker_id="dead", started_at=now)
    session.commit()
    assert scheduler_tick(session, now, _half) == 0
    assert _stored_deploy(session, run).status is DeployStatus.RUNNING
    assert scheduler_tick(session, now, _half, 60) == 0
    assert _stored_deploy(session, run).status is DeployStatus.FAILED


def test_deploys_of_alive_workers_and_queued_ones_do_not_fill_the_batch(
    session: Session, environment_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The candidate read already leaves out the alive and the queued: they must not starve."""
    monkeypatch.setattr(store, "SWEEP_BATCH", 1)
    now = db_now(session)
    _worker(session, "alive", age_seconds=1)
    _worker(session, "dead", age_seconds=61)
    old = now - timedelta(seconds=300)
    _deploy_run(session, environment_id, worker_id="alive", created_at=old, started_at=now)
    _deploy_run(session, environment_id, status=DeployStatus.QUEUED, created_at=old)
    lost = _deploy_run(session, environment_id, worker_id="dead", created_at=now, started_at=now)
    session.commit()
    assert end_lost_runs(session, now, 60) == 1
    assert _stored_deploy(session, lost).status is DeployStatus.FAILED


def _running_task(session: Session, lease_left: int) -> None:
    """Put `silver.clean` in `running` under `dead` with an open attempt and that much lease left.

    Parameters
    ----------
    session
        Database session.
    lease_left
        Seconds until the task's lease expires, by the database's clock (negative: expired).
    """
    task = _task(session, "silver.clean")
    task.status = TaskStatus.RUNNING
    task.attempt = 1
    task.claim_count = 1
    task.lease_owner = "dead"
    task.lease_expires_at = db_now(session) + timedelta(seconds=lease_left)
    session.add(
        TaskAttempt(id=uuid.uuid4(), task_run_id=task.id, attempt=1, worker="dead", started_at=NOW)
    )
    session.commit()


@pytest.mark.parametrize("rows", [None, 4])
def test_the_sweep_leaves_a_sql_tasks_write_to_the_tasks_live_lease(
    session: Session, run_id: uuid.UUID, rows: int | None
) -> None:
    """I2: under a live task lease the heartbeat does not decide; the lease owns the run."""
    scheduler_tick(session, NOW, _half)
    _running_task(session, lease_left=50)
    write = _task_write(
        session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o", rows_written=rows
    )
    now = db_now(session)
    assert store._lost_query_runs(session, now, 60) == []
    assert end_lost_runs(session, now, 60) == 0
    assert scheduler_tick(session, now, _half, 60) == 0
    assert _stored(session, write).status is QueryRunStatus.RUNNING
    assert _events(session, AuditEventType.QUERY_COMPLETED) == []


def test_the_ending_update_carries_the_task_lease_condition_too(
    session: Session, run_id: uuid.UUID
) -> None:
    """R4.7.1: a lease renewed between the read and the update keeps the write run."""
    _running_task(session, lease_left=-5)
    write = _task_write(session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o")
    now = db_now(session)
    (candidate,) = store._lost_query_runs(session, now, 60)
    task = _task(session, "silver.clean")
    session.execute(
        update(TaskRun)
        .where(TaskRun.id == task.id)
        .values(lease_expires_at=now + timedelta(seconds=50))
        .execution_options(synchronize_session=False)
    )
    assert store._end_query_run(session, candidate, now, 60) is False
    session.commit()
    assert _stored(session, write).status is QueryRunStatus.RUNNING


def test_a_python_tasks_query_is_swept_also_under_a_live_task_lease(
    session: Session, run_id: uuid.UUID
) -> None:
    _running_task(session, lease_left=50)
    run = _task_write(session, run_id, uuid.uuid4())
    assert end_lost_runs(session, db_now(session), 60) == 1
    assert _stored(session, run).status is QueryRunStatus.FAILED


@pytest.mark.parametrize("rows", [None, 4])
def test_after_the_task_lease_expired_the_task_path_ends_the_write_once(
    session: Session, run_id: uuid.UUID, rows: int | None
) -> None:
    scheduler_tick(session, NOW, _half)
    _running_task(session, lease_left=-1)
    write = _task_write(
        session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o", rows_written=rows
    )
    now = db_now(session)
    assert scheduler_tick(session, now, _half, 60) >= 1
    ended = _stored(session, write)
    assert (ended.status, ended.error_cause, ended.error_message) == (
        (QueryRunStatus.FAILED, None, WORKER_LOST_WRITE_MESSAGE)  # the task path's stamp
        if rows is None
        else (QueryRunStatus.SUCCEEDED, None, None)  # the evidence: the task is not repeated
    )
    assert _task(session, "silver.clean").status is (
        TaskStatus.PENDING if rows is None else TaskStatus.SUCCEEDED
    )
    assert scheduler_tick(session, now, _half, 60) == 0
    assert _events(session, AuditEventType.QUERY_COMPLETED) == []


def test_a_workers_orphans_are_ended_by_the_evidence_rule_and_nothing_else(
    session: Session, environment_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    """I3: at idle, a worker ends the `RUNNING` runs that carry its own id, alive or not."""
    now = db_now(session)
    _worker(session, "me", age_seconds=1)  # alive: the sweep would never take these
    _worker(session, "other", age_seconds=600)  # dead: not this path's to take

    def make(worker: str, **fields: object) -> QueryRun:
        return _run(session, environment_id, worker_id=worker, principal_id=principal_id, **fields)

    read = make("me")
    committed = make("me", write_mode=WriteMode.APPEND, target_name="s.o", rows_written=3)
    uncommitted = make("me", write_mode=WriteMode.APPEND, target_name="s.o")
    foreign = make("other")
    queued = make("me", status=QueryRunStatus.QUEUED)
    done = make("me", status=QueryRunStatus.SUCCEEDED)
    session.commit()
    assert store.end_orphaned_runs(session, "me", now) == 3
    assert _stored(session, read).status is QueryRunStatus.FAILED
    assert _stored(session, read).error_message == QUERY_LOST_MESSAGE
    assert _stored(session, read).error_cause == "worker_lost"
    assert _stored(session, committed).status is QueryRunStatus.SUCCEEDED
    assert _stored(session, uncommitted).status is QueryRunStatus.FAILED
    assert (_stored(session, uncommitted).error_message or "").startswith(
        "The worker running this write was lost before it was committed"
    )
    for untouched, status in (
        (foreign, QueryRunStatus.RUNNING),
        (queued, QueryRunStatus.QUEUED),
        (done, QueryRunStatus.SUCCEEDED),
    ):
        assert _stored(session, untouched).status is status
    assert sorted(e.detail["status"] for e in _events(session, AuditEventType.QUERY_COMPLETED)) == [
        "FAILED",
        "FAILED",
        "SUCCEEDED",
    ]
    assert store.end_orphaned_runs(session, "me", now) == 0  # ended once, one event each
    assert len(_events(session, AuditEventType.QUERY_COMPLETED)) == 3


@pytest.mark.parametrize("rows", [None, 3])
@pytest.mark.parametrize("lease_left", [50, None])
def test_an_idle_worker_ends_a_sql_tasks_write_unless_a_live_lease_holds_it(
    session: Session, run_id: uuid.UUID, lease_left: int | None, rows: int | None
) -> None:
    """N3: the same predicate as the sweep decides; no separate exclusion."""
    scheduler_tick(session, NOW, _half)
    if lease_left is not None:
        _running_task(session, lease_left=lease_left)
    write = _task_write(
        session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o", rows_written=rows
    )
    session.execute(update(QueryRun).where(QueryRun.id == write.id).values(worker_id="me"))
    session.commit()
    ended = store.end_orphaned_runs(session, "me", db_now(session))
    stored = _stored(session, write)
    if lease_left is None:  # the task is no longer running: nobody else would end it
        assert ended == 1
        assert (stored.status, stored.error_message) == (
            (QueryRunStatus.FAILED, WORKER_LOST_WRITE_MESSAGE)
            if rows is None
            else (QueryRunStatus.SUCCEEDED, None)
        )
        assert _events(session, AuditEventType.QUERY_COMPLETED) == []
    else:
        assert ended == 0 and stored.status is QueryRunStatus.RUNNING


def test_an_orphan_that_changed_before_the_update_is_left_alone(
    session: Session, environment_id: uuid.UUID
) -> None:
    """R4.7.1 for the orphan path: the update carries the status and the evidence itself."""
    now = db_now(session)
    run = _run(session, environment_id, worker_id="me", write_mode=WriteMode.APPEND)
    session.commit()
    (candidate,) = store._own_running_runs(session, "me", now)
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run.id)
        .values(rows_written=1)
        .execution_options(synchronize_session=False)
    )
    assert store._end_orphan(session, candidate, "me", now) is False
    session.commit()
    assert store.end_orphaned_runs(session, "me", now) == 1
    assert _stored(session, run).status is QueryRunStatus.SUCCEEDED


def test_an_orphan_that_another_worker_holds_by_the_update_is_left_alone(
    session: Session, environment_id: uuid.UUID
) -> None:
    now = db_now(session)
    run = _run(session, environment_id, worker_id="me")
    session.commit()
    (candidate,) = store._own_running_runs(session, "me", now)
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run.id)
        .values(worker_id="other")
        .execution_options(synchronize_session=False)
    )
    assert store._end_orphan(session, candidate, "me", now) is False
    session.commit()
    assert _stored(session, run).status is QueryRunStatus.RUNNING


def test_other_workers_runs_do_not_fill_the_orphan_batch(
    session: Session, environment_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "SWEEP_BATCH", 1)
    now = db_now(session)
    _run(session, environment_id, worker_id="other", created_at=now - timedelta(seconds=300))
    mine = _run(session, environment_id, worker_id="me", created_at=now)
    session.commit()
    assert store.end_orphaned_runs(session, "me", now) == 1
    assert _stored(session, mine).status is QueryRunStatus.FAILED


@pytest.mark.parametrize("kind", list(DeployKind))
def test_an_idle_worker_ends_its_own_leftover_deploys_and_no_others(
    session: Session, environment_id: uuid.UUID, kind: DeployKind
) -> None:
    """A deploy of any kind still `running` under this id is failed; nothing else is."""
    now = db_now(session)
    _worker(session, "me", age_seconds=1)  # alive: the sweep never takes these
    _worker(session, "other", age_seconds=1)
    mine = _deploy_run(session, environment_id, kind=kind, worker_id="me", started_at=now)
    foreign = _deploy_run(session, environment_id, kind=kind, worker_id="other", started_at=now)
    unclaimed = _deploy_run(session, environment_id, kind=kind, started_at=now)
    finished = [
        _deploy_run(session, environment_id, kind=kind, worker_id="me", status=status)
        for status in (DeployStatus.QUEUED, DeployStatus.SUCCEEDED, DeployStatus.FAILED)
    ]
    session.commit()
    assert store.end_orphaned_runs(session, "me", now) == 1
    stored = _stored_deploy(session, mine)
    assert (stored.status, stored.error_message) == (DeployStatus.FAILED, DEPLOY_LOST_MESSAGE)
    assert stored.finished_at is not None
    for untouched in (foreign, unclaimed, *finished):
        assert _stored_deploy(session, untouched).status is untouched.status
        assert _stored_deploy(session, untouched).error_message is None
    assert session.query(AuditEvent).count() == 0  # the deploy sweep writes none either
    assert store.end_orphaned_runs(session, "me", now) == 0


def test_the_count_of_the_idle_ender_adds_query_and_deploy_runs(
    session: Session, environment_id: uuid.UUID, principal_id: uuid.UUID
) -> None:
    """One call ends both kinds, each in its own transaction."""
    now = db_now(session)
    _run(session, environment_id, worker_id="me", principal_id=principal_id)
    _deploy_run(session, environment_id, worker_id="me", started_at=now)
    session.commit()
    assert store.end_orphaned_runs(session, "me", now) == 2


def test_a_leftover_deploy_that_changed_before_the_update_is_left_alone(
    session: Session, environment_id: uuid.UUID
) -> None:
    """The statement carries the status and the worker id itself, as the sweep's does."""
    now = db_now(session)
    run = _deploy_run(session, environment_id, worker_id="me", started_at=now)
    session.commit()
    (candidate,) = store._own_running_deploys(session, "me")
    session.execute(
        update(DeployRun)
        .where(DeployRun.id == run.id)
        .values(worker_id="other")
        .execution_options(synchronize_session=False)
    )
    assert store._end_deploy_orphan(session, candidate, "me", now) is False
    session.execute(
        update(DeployRun)
        .where(DeployRun.id == run.id)
        .values(worker_id="me", status=DeployStatus.SUCCEEDED)
        .execution_options(synchronize_session=False)
    )
    assert store._end_deploy_orphan(session, candidate, "me", now) is False
    session.commit()
    assert _stored_deploy(session, run).status is DeployStatus.SUCCEEDED


def test_the_leftover_deploys_are_read_for_no_key_update_skipping_locked_rows() -> None:
    """A deploy that is finishing holds its row; the ender passes it by."""
    statement = store._own_running_deploys_statement("me")
    assert (
        str(statement.compile(dialect=postgresql.dialect()))
        .rstrip()
        .endswith("FOR NO KEY UPDATE OF deploy_runs SKIP LOCKED")
    )


def test_other_workers_deploys_do_not_fill_the_leftover_batch(
    session: Session, environment_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "SWEEP_BATCH", 1)
    now = db_now(session)
    _deploy_run(session, environment_id, worker_id="other", created_at=now - timedelta(seconds=300))
    mine = _deploy_run(session, environment_id, worker_id="me", created_at=now)
    session.commit()
    assert store.end_orphaned_runs(session, "me", now) == 1
    assert _stored_deploy(session, mine).status is DeployStatus.FAILED


def test_scheduler_tick_forgets_nothing_when_told_not_to(session: Session) -> None:
    now = db_now(session)
    _worker(session, "old", age_seconds=3601)
    session.commit()
    scheduler_tick(session, now, _half, 60, forget=False)
    assert set(session.scalars(select(Worker.id))) == {"old"}


def test_a_sql_tasks_earlier_write_is_not_shielded_by_a_later_attempts_lease(
    session: Session, run_id: uuid.UUID
) -> None:
    """N2: only the task's CURRENT write is held by its lease."""
    scheduler_tick(session, NOW, _half)
    _running_task(session, lease_left=50)
    earlier = _task_write(session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o")
    current = _task_write(session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o")
    now = db_now(session)
    assert end_lost_runs(session, now, 60) == 1
    assert _stored(session, earlier).status is QueryRunStatus.FAILED
    assert _stored(session, current).status is QueryRunStatus.RUNNING


def test_a_task_that_is_not_running_holds_no_write_even_with_a_future_lease(
    session: Session, run_id: uuid.UUID
) -> None:
    """N5: the lease only counts while the task run is `running`."""
    scheduler_tick(session, NOW, _half)
    write = _task_write(session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o")
    task = _task(session, "silver.clean")
    task.lease_expires_at = db_now(session) + timedelta(seconds=50)
    session.commit()
    assert task.status is not TaskStatus.RUNNING
    assert end_lost_runs(session, db_now(session), 60) == 1
    assert _stored(session, write).status is QueryRunStatus.FAILED


def _poison(monkeypatch: pytest.MonkeyPatch, name: str, poisoned: uuid.UUID) -> None:
    """Make `store.<name>` raise for one run and behave as before for every other run.

    Parameters
    ----------
    monkeypatch
        The test's patcher.
    name
        The module attribute to wrap (`record_query_completed` or `_end_deploy_run`).
    poisoned
        The id of the run whose handling raises.
    """
    real = getattr(store, name)

    def wrapper(session: Session, run: QueryRun | DeployRun, *args: object, **kw: object) -> object:
        if run.id == poisoned:
            raise RuntimeError("boom SELECT secret FROM somewhere")
        return real(session, run, *args, **kw)

    monkeypatch.setattr(store, name, wrapper)


def _three_lost_runs(
    session: Session, environment_id: uuid.UUID, principal_id: uuid.UUID, **fields: object
) -> list[QueryRun]:
    """Insert three `RUNNING` runs, oldest first, that a worker with the given id would hold.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    principal_id
        The submitter.
    **fields
        More column values (the worker id).

    Returns
    -------
    list[QueryRun]
        The committed runs, oldest first.
    """
    now = db_now(session)
    runs = [
        _run(
            session,
            environment_id,
            principal_id=principal_id,
            created_at=now - timedelta(seconds=300 - 10 * i),
            **fields,
        )
        for i in range(3)
    ]
    session.commit()
    return runs


def test_one_run_that_raises_while_it_is_ended_blocks_neither_the_others_nor_the_tick(
    session: Session,
    environment_id: uuid.UUID,
    principal_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I1: each run ends in its own transaction; the raising one stays, the tick returns."""
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    first, second, third = _three_lost_runs(session, environment_id, principal_id, worker_id="dead")
    deploy = _deploy_run(session, environment_id, worker_id="dead", started_at=now)
    session.commit()
    _poison(monkeypatch, "record_query_completed", second.id)
    with caplog.at_level("WARNING", logger=store.logger.name):
        assert scheduler_tick(session, now, _half, 60) == 0  # returns, does not raise
        assert scheduler_tick(session, now, _half, 60) == 0  # the next tick is no different
    assert [_stored(session, r).status for r in (first, second, third)] == [
        QueryRunStatus.FAILED,
        QueryRunStatus.RUNNING,
        QueryRunStatus.FAILED,
    ]
    assert _stored_deploy(session, deploy).status is DeployStatus.FAILED  # the deploy half ran
    assert len(_events(session, AuditEventType.QUERY_COMPLETED)) == 2
    lines = [r.getMessage() for r in caplog.records if str(second.id) in r.getMessage()]
    assert len(lines) == 2  # one line per tick
    assert all("RuntimeError" in line and "SELECT" not in line for line in lines)


def test_one_deploy_that_raises_while_it_is_ended_blocks_neither_the_others_nor_the_tick(
    session: Session,
    environment_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I1, the deploy half: the same isolation."""
    now = db_now(session)
    _worker(session, "dead", age_seconds=61)
    runs = [
        _deploy_run(
            session,
            environment_id,
            worker_id="dead",
            started_at=now,
            created_at=now - timedelta(seconds=300 - 10 * i),
        )
        for i in range(3)
    ]
    session.commit()
    _poison(monkeypatch, "_end_deploy_run", runs[1].id)
    with caplog.at_level("WARNING", logger=store.logger.name):
        assert end_lost_runs(session, now, 60) == 2
    assert [_stored_deploy(session, r).status for r in runs] == [
        DeployStatus.FAILED,
        DeployStatus.RUNNING,
        DeployStatus.FAILED,
    ]
    assert len([r for r in caplog.records if str(runs[1].id) in r.getMessage()]) == 1


def test_one_orphan_that_raises_while_it_is_ended_blocks_neither_the_others_nor_the_caller(
    session: Session,
    environment_id: uuid.UUID,
    principal_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """I1, the idle worker's ender: the same isolation."""
    now = db_now(session)
    first, second, third = _three_lost_runs(session, environment_id, principal_id, worker_id="me")
    _poison(monkeypatch, "record_query_completed", second.id)
    with caplog.at_level("WARNING", logger=store.logger.name):
        assert store.end_orphaned_runs(session, "me", now) == 2
    assert [_stored(session, r).status for r in (first, second, third)] == [
        QueryRunStatus.FAILED,
        QueryRunStatus.RUNNING,
        QueryRunStatus.FAILED,
    ]
    assert len(_events(session, AuditEventType.QUERY_COMPLETED)) == 2
    assert len([r for r in caplog.records if str(second.id) in r.getMessage()]) == 1


def test_an_orphans_ending_update_carries_the_task_lease_condition_too(
    session: Session, run_id: uuid.UUID
) -> None:
    """A task's write whose lease turns live between the read and the update is not ended."""
    scheduler_tick(session, NOW, _half)
    _running_task(session, lease_left=-5)
    write = _task_write(session, run_id, None, write_mode=WriteMode.APPEND, target_name="s.o")
    session.execute(update(QueryRun).where(QueryRun.id == write.id).values(worker_id="me"))
    session.commit()
    now = db_now(session)
    (candidate,) = store._own_running_runs(session, "me", now)
    task = _task(session, "silver.clean")
    session.execute(
        update(TaskRun)
        .where(TaskRun.id == task.id)
        .values(lease_expires_at=now + timedelta(seconds=50))
        .execution_options(synchronize_session=False)
    )
    assert store._end_orphan(session, candidate, "me", now) is False
    session.commit()
    assert _stored(session, write).status is QueryRunStatus.RUNNING


# --- A task whose write committed before its worker was lost is not run again ---------------


def _lost_task_with_write(
    session: Session,
    run_id: uuid.UUID,
    *,
    rows: int | None,
    attempt: int = 1,
    expires: datetime = NOW - timedelta(seconds=1),
    mode: WriteMode = WriteMode.APPEND,
) -> tuple[TaskRun, QueryRun]:
    """Start the run, put `silver.clean` in `running` with an expired lease and its write run.

    Parameters
    ----------
    session
        Database session.
    run_id
        The pipeline run.
    rows
        The write's evidence (`rows_written`), or `None` for a write that did not commit.
    attempt
        The attempt that is running (also its claim counter).
    expires
        The lease expiry to store.
    mode
        The write mode of the task's write run.

    Returns
    -------
    tuple of (TaskRun, QueryRun)
        The task and its current write run.
    """
    scheduler_tick(session, NOW, _half)
    task = _task(session, "silver.clean")
    pipeline = session.get(Run, run_id)
    assert pipeline is not None
    task.status = TaskStatus.RUNNING
    task.attempt = attempt
    task.claim_count = attempt
    task.lease_owner = "w1"
    task.lease_expires_at = expires
    task.max_attempts = 3
    session.add(
        TaskAttempt(
            id=uuid.uuid4(), task_run_id=task.id, attempt=attempt, worker="w1", started_at=NOW
        )
    )
    write = QueryRun(
        id=uuid.uuid4(),
        environment_id=pipeline.environment_id,
        principal_id=pipeline.run_as_principal_id,
        sql_text="SELECT 1",
        status=QueryRunStatus.RUNNING,
        write_mode=mode,
        target_name="silver.clean",
        task_run_id=task.id,
        worker_id="w1",
        rows_written=rows,
    )
    session.add(write)
    session.flush()
    task.query_run_id = write.id
    session.commit()
    return task, write


@pytest.mark.parametrize("mode", [WriteMode.APPEND, WriteMode.REPLACE])
@pytest.mark.parametrize("rows", [0, 4])
def test_a_lost_task_whose_write_committed_is_succeeded_once_and_not_retried(
    session: Session, run_id: uuid.UUID, mode: WriteMode, rows: int
) -> None:
    """The reclaim sees the evidence; task, attempt and write all end SUCCEEDED."""
    task, write = _lost_task_with_write(session, run_id, rows=rows, mode=mode)
    assert scheduler_tick(session, NOW, _half) >= 1
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.attempt, stored.finished_at) == (TaskStatus.SUCCEEDED, 1, NOW)
    assert (stored.lease_owner, stored.lease_expires_at, stored.next_attempt_at) == (None,) * 3
    assert stored.error_message == COMMITTED_BEFORE_LOST_MESSAGE
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert (attempt.outcome, attempt.finished_at, attempt.rows_written) == (
        AttemptOutcome.SUCCEEDED,
        NOW,
        rows,
    )
    assert attempt.error_message == COMMITTED_BEFORE_LOST_MESSAGE
    ended = _stored(session, write)
    assert (ended.status, ended.error_message, ended.error_cause, ended.finished_at) == (
        QueryRunStatus.SUCCEEDED,
        None,
        None,
        NOW,
    )
    assert ended.rows_written == rows  # the evidence is untouched
    assert _task(session, "gold.daily").status is TaskStatus.READY  # downstream proceeds
    assert _events(session, AuditEventType.QUERY_COMPLETED) == []
    assert scheduler_tick(session, NOW, _half) == 0  # nothing left to do, no second attempt
    assert len(session.scalars(select(TaskAttempt)).all()) == 1


def test_a_lost_task_without_evidence_is_still_put_back(
    session: Session, run_id: uuid.UUID
) -> None:
    """No evidence, no change: the attempt is lost, the write failed, the task waits to retry."""
    task, write = _lost_task_with_write(session, run_id, rows=None)
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.next_attempt_at) == (
        TaskStatus.PENDING,
        NOW + timedelta(seconds=30),
    )
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert attempt.outcome is AttemptOutcome.LOST
    assert _stored(session, write).status is QueryRunStatus.FAILED


def test_a_lost_last_attempt_whose_write_committed_does_not_fail_the_task(
    session: Session, run_id: uuid.UUID
) -> None:
    """On the last attempt the evidence still wins over `failed`; the run succeeds."""
    _lost_task_with_write(session, run_id, rows=2, attempt=3)
    for _ in range(2):
        scheduler_tick(session, NOW, _half)
    assert _task(session, "silver.clean").status is TaskStatus.SUCCEEDED
    assert _task(session, "gold.daily").status is TaskStatus.READY


def test_a_cancel_does_not_turn_a_committed_task_into_cancelled(
    session: Session, run_id: uuid.UUID
) -> None:
    """A run being cancelled while the lease expires: the data is there, the task is SUCCEEDED."""
    _, write = _lost_task_with_write(session, run_id, rows=3)
    pipeline = session.get(Run, run_id)
    assert pipeline is not None
    pipeline.cancel_requested = True
    session.commit()
    scheduler_tick(session, NOW, _half)
    assert _task(session, "silver.clean").status is TaskStatus.SUCCEEDED
    assert _task(session, "gold.daily").status is TaskStatus.CANCELLED
    assert _stored(session, write).status is QueryRunStatus.SUCCEEDED
    session.refresh(pipeline)
    assert pipeline.status is RunStatus.CANCELLED


def test_the_evidence_of_an_earlier_attempts_write_does_not_end_a_later_attempt(
    session: Session, run_id: uuid.UUID
) -> None:
    """Only the task's CURRENT write run counts (`task_runs.query_run_id`)."""
    task, current = _lost_task_with_write(session, run_id, rows=None, attempt=2)
    earlier = QueryRun(
        id=uuid.uuid4(),
        environment_id=current.environment_id,
        principal_id=current.principal_id,
        sql_text="SELECT 1",
        status=QueryRunStatus.FAILED,
        write_mode=WriteMode.APPEND,
        target_name="silver.clean",
        task_run_id=task.id,
        rows_written=9,
    )
    session.add(earlier)
    session.commit()
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.attempt) == (TaskStatus.PENDING, 2)
    assert _stored(session, current).status is QueryRunStatus.FAILED


def test_a_later_attempt_that_is_running_is_left_alone_whatever_an_earlier_one_committed(
    session: Session, run_id: uuid.UUID
) -> None:
    """An earlier attempt reclaimed before the evidence was seen: the running one is not touched."""
    task, current = _lost_task_with_write(
        session, run_id, rows=None, attempt=2, expires=NOW + timedelta(seconds=50)
    )
    session.add(
        QueryRun(
            id=uuid.uuid4(),
            environment_id=current.environment_id,
            principal_id=current.principal_id,
            sql_text="SELECT 1",
            status=QueryRunStatus.FAILED,
            write_mode=WriteMode.APPEND,
            target_name="silver.clean",
            task_run_id=task.id,
            rows_written=9,
        )
    )
    session.commit()
    assert scheduler_tick(session, NOW, _half) == 0
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING
    assert _stored(session, current).status is QueryRunStatus.RUNNING


def test_evidence_that_appears_between_the_read_and_the_write_stops_the_retry(
    session: Session, run_id: uuid.UUID
) -> None:
    """The reclaim to PENDING carries "no evidence" in its own statement; a miss changes nothing."""
    task, write = _lost_task_with_write(session, run_id, rows=None)
    stale = tick(load_snapshots(session), NOW, _half)
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == write.id)
        .values(rows_written=5)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    assert apply_transitions(session, stale, NOW) is False
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.lease_owner, stored.next_attempt_at) == (
        TaskStatus.RUNNING,
        "w1",
        None,
    )
    assert _stored(session, write).status is QueryRunStatus.RUNNING
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert attempt.finished_at is None
    scheduler_tick(session, NOW, _half)  # the next tick reads the evidence
    assert _task(session, "silver.clean").status is TaskStatus.SUCCEEDED


@pytest.mark.parametrize("outcome", [TaskStatus.FAILED, TaskStatus.CANCELLED])
def test_a_failure_or_cancel_reclaim_also_requires_no_evidence(
    session: Session, run_id: uuid.UUID, outcome: TaskStatus
) -> None:
    """Every reclaim that is not a success is conditional on the write having no evidence."""
    task, _ = _lost_task_with_write(session, run_id, rows=1)
    reclaim = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        outcome,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
    )
    assert apply_transitions(session, [reclaim], NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING


def test_a_success_reclaim_requires_the_evidence_at_the_write(
    session: Session, run_id: uuid.UUID
) -> None:
    """Without evidence in the database a "committed" reclaim misses and changes nothing."""
    task, write = _lost_task_with_write(session, run_id, rows=None)
    succeed = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
    )
    assert apply_transitions(session, [succeed], NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING
    assert _stored(session, write).status is QueryRunStatus.RUNNING


def test_a_success_reclaim_of_a_since_renewed_lease_misses(
    session: Session, run_id: uuid.UUID
) -> None:
    """The worker renewed after all: the reclaim to SUCCEEDED misses, as any reclaim."""
    task, _ = _lost_task_with_write(session, run_id, rows=2)
    stale = tick(load_snapshots(session), NOW, _half)
    task = _task(session, "silver.clean")
    task.lease_expires_at = NOW + timedelta(seconds=60)
    session.commit()
    assert apply_transitions(session, stale, NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING


def test_a_worker_that_records_its_outcome_first_wins_and_the_reclaim_misses(
    session: Session, run_id: uuid.UUID
) -> None:
    """The worker is alive after all: one of the two writes the outcome, once."""
    task, _ = _lost_task_with_write(session, run_id, rows=2)
    stale = tick(load_snapshots(session), NOW, _half)
    assert record_attempt(session, task.id, "w1", 1, kind=None, rand=_half, rows_written=2)
    assert apply_transitions(session, stale, NOW) is False
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.error_message) == (TaskStatus.SUCCEEDED, None)
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert (attempt.outcome, attempt.error_message) == (AttemptOutcome.SUCCEEDED, None)


def test_a_reclaim_that_wins_first_leaves_the_worker_nothing_to_record(
    session: Session, run_id: uuid.UUID
) -> None:
    """The other order of the same race: the scheduler wins, the late worker's record is refused."""
    task, _ = _lost_task_with_write(session, run_id, rows=2)
    scheduler_tick(session, NOW, _half)
    assert not record_attempt(session, task.id, "w1", 1, kind=None, rand=_half, rows_written=2)
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert attempt.error_message == COMMITTED_BEFORE_LOST_MESSAGE


def test_two_schedulers_end_the_task_once(session: Session, run_id: uuid.UUID) -> None:
    """A leader change: both read the same snapshot; the second finds the task already ended."""
    _lost_task_with_write(session, run_id, rows=2)
    transitions = tick(load_snapshots(session), NOW, _half)
    assert apply_transitions(session, transitions, NOW) is True
    assert apply_transitions(session, transitions, NOW) is False
    assert len(session.scalars(select(TaskAttempt)).all()) == 1
    assert _task(session, "silver.clean").status is TaskStatus.SUCCEEDED


def test_the_snapshot_says_which_running_tasks_have_the_evidence(
    session: Session, run_id: uuid.UUID
) -> None:
    """`TaskSnapshot.committed` mirrors `rows_written IS NOT NULL` on the current write run."""
    _lost_task_with_write(session, run_id, rows=0)
    (snapshot,) = load_snapshots(session)
    committed = {t.asset: t.committed for t in snapshot.tasks}
    assert committed == {"silver.clean": True, "gold.daily": False}


def test_a_lost_tasks_other_running_runs_end_by_their_own_evidence(
    session: Session, run_id: uuid.UUID
) -> None:
    """An earlier attempt's run left RUNNING: SUCCEEDED with evidence, FAILED without."""
    task, current = _lost_task_with_write(session, run_id, rows=None)
    others = []
    for rows in (7, None):
        other = QueryRun(
            id=uuid.uuid4(),
            environment_id=current.environment_id,
            principal_id=current.principal_id,
            sql_text="SELECT 1",
            status=QueryRunStatus.RUNNING,
            write_mode=WriteMode.APPEND,
            target_name="silver.clean",
            task_run_id=task.id,
            rows_written=rows,
        )
        session.add(other)
        others.append(other)
    session.commit()
    scheduler_tick(session, NOW, _half)
    assert [_stored(session, o).status for o in others] == [
        QueryRunStatus.SUCCEEDED,
        QueryRunStatus.FAILED,
    ]
    assert _stored(session, current).status is QueryRunStatus.FAILED


# --- a written task that has checks and no verdict is never ended by the sweep -------------


def _stamp(
    session: Session, task: TaskRun, *, has_checks: bool = True, stamped: bool = True
) -> None:
    """Mark a task run as having checks and/or as written, as the write's commit would.

    Parameters
    ----------
    session
        Database session; committed here.
    task
        The task run.
    has_checks
        Whether its asset has checks.
    stamped
        Whether the write's commit stamped it written.
    """
    stored = session.get(TaskRun, task.id)
    assert stored is not None
    stored.has_checks = has_checks
    if stamped:
        stored.written_table_id = uuid.uuid4()
        stored.written_snapshot_before = 11
        stored.written_snapshot_after = 12
        stored.written_publishes = False
    session.commit()


def _written(session: Session, task: TaskRun) -> tuple[object, ...]:
    """Read the four columns of a task run's *written* stamp.

    Parameters
    ----------
    session
        Database session.
    task
        The task run.

    Returns
    -------
    tuple
        Table id, snapshot before, snapshot after, publishes.
    """
    stored = session.execute(
        select(
            TaskRun.written_table_id,
            TaskRun.written_snapshot_before,
            TaskRun.written_snapshot_after,
            TaskRun.written_publishes,
        ).where(TaskRun.id == task.id)
    ).one()
    return tuple(stored)


def test_the_reclaim_of_an_unchecked_task_leaves_it_pending_with_its_stamp(
    session: Session, run_id: uuid.UUID
) -> None:
    """Lease lost after the write, with checks: back to the queue, uncounted."""
    task, write = _lost_task_with_write(session, run_id, rows=3)
    _stamp(session, task)
    before = _written(session, task)
    assert scheduler_tick(session, NOW, _half) >= 1
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.attempt, stored.claim_count) == (TaskStatus.READY, 1, 1)
    assert (stored.lease_owner, stored.lease_expires_at, stored.finished_at) == (None,) * 3
    assert stored.next_attempt_at == NOW
    assert stored.error_message is None
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert (attempt.outcome, attempt.error_message) == (AttemptOutcome.LOST, LEASE_EXPIRED_MESSAGE)
    assert _written(session, task) == before
    assert _stored(session, write).status is QueryRunStatus.SUCCEEDED
    assert _task(session, "gold.daily").status is TaskStatus.PENDING
    assert scheduler_tick(session, NOW, _half) == 0


def test_an_unchecked_task_is_put_back_whatever_attempts_are_left(
    session: Session, run_id: uuid.UUID
) -> None:
    """A written task cannot fail by running out of attempts: the third attempt is lost, too."""
    task, _ = _lost_task_with_write(session, run_id, rows=2, attempt=3)
    _stamp(session, task)
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.attempt, stored.error_message) == (TaskStatus.READY, 3, None)
    assert _task(session, "gold.daily").status is TaskStatus.PENDING


def test_a_written_task_without_checks_still_succeeds_in_the_store(
    session: Session, run_id: uuid.UUID
) -> None:
    """The stamp alone does not hold a task whose asset has no checks."""
    task, _ = _lost_task_with_write(session, run_id, rows=3)
    _stamp(session, task, has_checks=False)
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.error_message) == (
        TaskStatus.SUCCEEDED,
        COMMITTED_BEFORE_LOST_MESSAGE,
    )
    assert _task(session, "gold.daily").status is TaskStatus.READY


def test_a_task_with_checks_and_no_stamp_is_reclaimed_as_before(
    session: Session, run_id: uuid.UUID
) -> None:
    """Checks, but the write did not commit: retried (the build is repeated, as today)."""
    task, write = _lost_task_with_write(session, run_id, rows=None)
    _stamp(session, task, stamped=False)
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.next_attempt_at) == (
        TaskStatus.PENDING,
        NOW + timedelta(seconds=30),
    )
    assert _stored(session, write).status is QueryRunStatus.FAILED


def test_an_unchecked_task_of_a_cancelled_run_is_set_back_in_one_tick(
    session: Session, run_id: uuid.UUID
) -> None:
    """A cancel does not leave its unchecked rows: the task is ready to set them back."""
    task, _ = _lost_task_with_write(session, run_id, rows=3)
    _stamp(session, task)
    pipeline = session.get(Run, run_id)
    assert pipeline is not None
    pipeline.cancel_requested = True
    session.commit()
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back, stored.finished_at, stored.lease_owner) == (
        TaskStatus.READY,
        True,
        None,
        None,
    )
    assert (stored.attempt, stored.claim_count, stored.set_back_claims) == (1, 1, 0)
    assert stored.ready_at == NOW
    assert _task(session, "gold.daily").status is TaskStatus.CANCELLED
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert attempt.outcome is AttemptOutcome.LOST
    session.refresh(pipeline)
    assert pipeline.status is RunStatus.RUNNING
    assert scheduler_tick(session, NOW, _half) == 0


@pytest.mark.parametrize("status", [TaskStatus.PENDING, TaskStatus.READY])
def test_a_cancel_sets_back_an_unchecked_task_that_waits_between_two_claims(
    session: Session, run_id: uuid.UUID, status: TaskStatus
) -> None:
    """No worker holds it, and its rows wait for their checks: the cancel sets the table back.

    Parameters
    ----------
    session
        Database session.
    run_id
        The pipeline run.
    status
        The waiting status of the task.
    """
    task, _ = _lost_task_with_write(session, run_id, rows=3, attempt=2)
    _stamp(session, task)
    waiting = _task(session, "silver.clean")
    waiting.status = status
    waiting.lease_owner = waiting.lease_expires_at = None
    session.commit()
    _cancel(session, run_id)
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back, stored.finished_at) == (
        TaskStatus.READY,
        True,
        None,
    )
    assert (stored.attempt, stored.claim_count, stored.set_back_claims) == (2, 2, 0)
    assert _task(session, "gold.daily").status is TaskStatus.CANCELLED
    pipeline = session.get(Run, run_id)
    assert pipeline is not None
    session.refresh(pipeline)
    assert pipeline.status is RunStatus.RUNNING
    assert scheduler_tick(session, NOW, _half) == 0


def test_a_stamp_that_lands_while_the_reclaim_waits_is_seen(
    session: Session, run_id: uuid.UUID
) -> None:
    """The snapshot saw no write; it commits (evidence and stamp) before the reclaim: it misses."""
    task, write = _lost_task_with_write(session, run_id, rows=None)
    _stamp(session, task, stamped=False)
    stale = tick(load_snapshots(session), NOW, _half)
    first = stale[0]
    assert isinstance(first, TaskTransition)
    assert (first.status, first.unchecked) == (TaskStatus.PENDING, False)
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == write.id)
        .values(rows_written=3)
        .execution_options(synchronize_session=False)
    )
    _stamp(session, task)  # the write's commit leaves the evidence and the stamp together
    assert apply_transitions(session, stale, NOW) is False
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.lease_owner) == (TaskStatus.RUNNING, "w1")
    assert _stored(session, write).status is QueryRunStatus.RUNNING
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert attempt.finished_at is None
    scheduler_tick(session, NOW, _half)  # the next tick reads the stamp
    assert _task(session, "silver.clean").status is TaskStatus.READY
    assert _task(session, "gold.daily").status is TaskStatus.PENDING


@pytest.mark.parametrize("outcome", [TaskStatus.FAILED, TaskStatus.PENDING, TaskStatus.CANCELLED])
def test_a_reclaim_that_is_not_the_unchecked_one_misses_a_stamped_task(
    session: Session, run_id: uuid.UUID, outcome: TaskStatus
) -> None:
    """Every other reclaim also requires that the task is not awaiting its checks."""
    task, _ = _lost_task_with_write(session, run_id, rows=None)
    _stamp(session, task)
    reclaim = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        outcome,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
    )
    assert apply_transitions(session, [reclaim], NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING


def test_an_unchecked_reclaim_misses_a_task_that_is_neither_stamped_nor_committed(
    session: Session, run_id: uuid.UUID
) -> None:
    """The unchecked reclaim carries its evidence in its statement, too."""
    task, _ = _lost_task_with_write(session, run_id, rows=None)
    _stamp(session, task, stamped=False)
    reclaim = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.PENDING,
        expected_owner="w1",
        expected_claim=1,
        next_attempt_at=NOW,
        lost=True,
        unchecked=True,
    )
    assert apply_transitions(session, [reclaim], NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING


def test_load_snapshots_says_which_tasks_are_unchecked(session: Session, run_id: uuid.UUID) -> None:
    """Checks, and the write in the table (the stamp or the evidence), and not ended."""
    task, write = _lost_task_with_write(session, run_id, rows=0)
    write_id = write.id

    def unchecked() -> bool:
        """Read the flag of `silver.clean` from a fresh snapshot.

        Returns
        -------
        bool
            Its `TaskSnapshot.unchecked`.
        """
        (snapshot,) = load_snapshots(session)
        return {t.asset: t.unchecked for t in snapshot.tasks}["silver.clean"]

    assert unchecked() is False
    assert unchecked() is False  # evidence, but the asset has no checks
    _stamp(session, task, has_checks=True, stamped=True)
    assert unchecked() is True
    stored = session.get(TaskRun, task.id)
    assert stored is not None
    stored.has_checks = False
    session.commit()
    assert unchecked() is False  # a stamp without checks
    stored.has_checks = True
    stored.written_table_id = None
    session.commit()
    assert unchecked() is True  # checks, no stamp, but the write's evidence (rows 0 or more)
    stored.has_checks = False
    session.commit()
    assert unchecked() is False  # the same task without checks is the plain case
    stored.has_checks = True
    stored.query_run_id = None
    session.commit()
    assert unchecked() is False  # claimed again: the evidence is unlinked
    stored.query_run_id = write_id
    stored.status = TaskStatus.SUCCEEDED
    session.commit()
    assert unchecked() is False  # ended: nothing to wait for


def test_a_rolled_back_task_lost_before_its_verdict_is_put_back_not_succeeded(
    session: Session, run_id: uuid.UUID
) -> None:
    """The rollback cleared the stamp, the evidence stays: still awaiting its verdict."""
    task, write = _lost_task_with_write(session, run_id, rows=3)
    _stamp(session, task, stamped=False)
    for _ in range(2):
        scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.attempt, stored.error_message) == (TaskStatus.READY, 1, None)
    assert stored.next_attempt_at == NOW
    assert _written(session, task) == (None,) * 4
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert attempt.outcome is AttemptOutcome.LOST
    assert _stored(session, write).status is QueryRunStatus.SUCCEEDED
    assert _task(session, "gold.daily").status is TaskStatus.PENDING


def test_a_claimed_again_rolled_back_task_that_is_lost_again_is_never_succeeded(
    session: Session, run_id: uuid.UUID
) -> None:
    """The claim unlinks the evidence; a second loss is an ordinary lost attempt.

    The task is retried (built again, then stamped and checked) or failed by the lost message,
    never succeeded.
    """
    task, _ = _lost_task_with_write(session, run_id, rows=3)
    _stamp(session, task, stamped=False)
    scheduler_tick(session, NOW, _half)
    claimed = claim_task(session, "w2", 60)
    assert claimed is not None and claimed.asset == "silver.clean"
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.attempt, stored.query_run_id) == (TaskStatus.RUNNING, 2, None)
    stored.lease_expires_at = NOW - timedelta(seconds=1)
    session.commit()
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.attempt) == (TaskStatus.PENDING, 2)
    assert stored.next_attempt_at == NOW + timedelta(seconds=60)
    assert _task(session, "gold.daily").status is TaskStatus.PENDING


@pytest.mark.parametrize("stamped", [False, True])
def test_a_stale_success_reclaim_of_a_task_with_checks_misses(
    session: Session, run_id: uuid.UUID, stamped: bool
) -> None:
    """The only statement that writes SUCCEEDED requires that the task awaits no verdict.

    The snapshot was read before the write landed (or before the stamp did): its success
    transition is stale. The task has checks and its write run has rows, so it awaits its
    checks, and the statement matches nothing.
    """
    task, write = _lost_task_with_write(session, run_id, rows=3)
    _stamp(session, task, stamped=stamped)
    succeed = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
    )
    assert apply_transitions(session, [succeed], NOW) is False
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.lease_owner, stored.error_message) == (
        TaskStatus.RUNNING,
        "w1",
        None,
    )
    assert _stored(session, write).status is QueryRunStatus.RUNNING
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert attempt.finished_at is None


# --- a lost task that may have written is put back to set its table back ------------------------


def _baseline(session: Session, task: TaskRun) -> None:
    """Record a baseline on a task run with checks, as a Python table task does before its process.

    Parameters
    ----------
    session
        Database session; committed here.
    task
        The task run.
    """
    stored = session.get(TaskRun, task.id)
    assert stored is not None
    stored.has_checks = True
    stored.baseline_snapshot_id = 5
    stored.baseline_publishes = False
    session.commit()


def _cancel(session: Session, run_id: uuid.UUID) -> None:
    """Ask the run to cancel.

    Parameters
    ----------
    session
        Database session; committed here.
    run_id
        The run.
    """
    pipeline = session.get(Run, run_id)
    assert pipeline is not None
    pipeline.cancel_requested = True
    session.commit()


def test_the_snapshot_says_a_running_task_with_a_baseline_and_no_stamp_may_have_written(
    session: Session, run_id: uuid.UUID
) -> None:
    """The baseline is recorded before the process starts; the stamp and the evidence end it."""
    task, write = _lost_task_with_write(session, run_id, rows=None)
    _baseline(session, task)

    def facts() -> tuple[bool, bool, int]:
        (snapshot,) = [
            t for r in load_snapshots(session) for t in r.tasks if t.asset == "silver.clean"
        ]
        return snapshot.may_have_written, snapshot.setting_back, snapshot.set_back_claims

    assert facts() == (True, False, 0)
    stored = _task(session, "silver.clean")
    stored.setting_back = True
    stored.set_back_claims = 1
    session.commit()
    assert facts() == (True, True, 1)
    stored.setting_back = False
    stored.written_table_id = uuid.uuid4()
    session.commit()
    assert facts()[0] is False
    stored.written_table_id = None
    session.execute(update(QueryRun).where(QueryRun.id == write.id).values(rows_written=2))
    session.commit()
    assert facts()[0] is False


def test_a_task_without_a_baseline_may_not_have_written(
    session: Session, run_id: uuid.UUID
) -> None:
    """A SQL task, or one without checks, records no baseline: nothing to set back."""
    _lost_task_with_write(session, run_id, rows=None)
    (snapshot,) = [t for r in load_snapshots(session) for t in r.tasks if t.asset == "silver.clean"]
    assert snapshot.may_have_written is False


def test_a_lost_task_that_may_have_written_on_its_last_attempt_is_put_back_in_the_store(
    session: Session, run_id: uuid.UUID
) -> None:
    """Ready with the flag, attempts and claims untouched, the open attempt closed as lost."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=3)
    _baseline(session, task)
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back, stored.error_message) == (
        TaskStatus.READY,
        True,
        None,
    )
    assert (stored.attempt, stored.claim_count, stored.set_back_claims) == (3, 3, 0)
    assert (stored.lease_owner, stored.lease_expires_at, stored.finished_at) == (None,) * 3
    assert stored.ready_at == NOW
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert (attempt.outcome, attempt.error_message) == (AttemptOutcome.LOST, LEASE_EXPIRED_MESSAGE)
    assert _task(session, "gold.daily").status is TaskStatus.PENDING
    assert scheduler_tick(session, NOW, _half) == 0


def test_a_lost_task_that_may_have_written_in_a_cancelled_run_is_set_back_not_cancelled(
    session: Session, run_id: uuid.UUID
) -> None:
    """With attempts left, a cancel still leaves what the process wrote: set back first."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=1)
    _baseline(session, task)
    _cancel(session, run_id)
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back, stored.attempt) == (TaskStatus.READY, True, 1)
    assert _task(session, "gold.daily").status is TaskStatus.CANCELLED


def test_a_stamp_that_lands_while_a_set_back_reclaim_waits_makes_it_miss(
    session: Session, run_id: uuid.UUID
) -> None:
    """The snapshot said may_have_written; the worker stamped it since: the reclaim misses."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=3)
    _baseline(session, task)
    stale = tick(load_snapshots(session), NOW, _half)
    first = stale[0]
    assert isinstance(first, TaskTransition)
    assert (first.status, first.setting_back) == (TaskStatus.READY, True)
    _stamp(session, task)
    assert apply_transitions(session, stale, NOW) is False
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back, stored.lease_owner) == (
        TaskStatus.RUNNING,
        False,
        "w1",
    )
    (attempt,) = session.scalars(select(TaskAttempt).where(TaskAttempt.task_run_id == task.id))
    assert attempt.finished_at is None


def test_a_set_back_reclaim_misses_when_the_evidence_of_its_path_is_gone(
    session: Session, run_id: uuid.UUID
) -> None:
    """Path 2 needs a stamp and a cancel, path 5 a baseline with no stamp and a reason."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=1)
    reclaim = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.READY,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
        ready_at=NOW,
        setting_back=True,
    )
    assert apply_transitions(session, [reclaim], NOW) is False  # nothing written
    _stamp(session, task)
    assert apply_transitions(session, [reclaim], NOW) is False  # stamped, but nobody cancelled
    _cancel(session, run_id)
    assert apply_transitions(session, [reclaim], NOW) is True
    assert _task(session, "silver.clean").setting_back is True


def test_a_baseline_with_attempts_left_and_no_cancel_misses_a_set_back_reclaim(
    session: Session, run_id: uuid.UUID
) -> None:
    """Path 5 needs a reason: a run not cancelled and an attempt left means a retry instead."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=1)
    _baseline(session, task)
    reclaim = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.READY,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
        ready_at=NOW,
        setting_back=True,
    )
    assert apply_transitions(session, [reclaim], NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING


@pytest.mark.parametrize("outcome", [TaskStatus.FAILED, TaskStatus.PENDING, TaskStatus.CANCELLED])
def test_a_normal_reclaim_misses_a_task_that_is_setting_back(
    session: Session, run_id: uuid.UUID, outcome: TaskStatus
) -> None:
    """Every other reclaim also requires that the task is not setting its table back."""
    task, _ = _lost_task_with_write(session, run_id, rows=None)
    stored = _task(session, "silver.clean")
    stored.setting_back = True
    session.commit()
    reclaim = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        outcome,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
    )
    assert apply_transitions(session, [reclaim], NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING


def test_a_lost_set_back_claim_is_put_back_and_then_ended_at_the_limit(
    session: Session, run_id: uuid.UUID
) -> None:
    """The first lost claim goes back to ready; the second one ends the task."""
    _lost_task_with_write(session, run_id, rows=None, attempt=3)
    stored = _task(session, "silver.clean")
    stored.setting_back = True
    stored.set_back_claims = 1
    session.commit()
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back, stored.set_back_claims) == (
        TaskStatus.READY,
        True,
        1,
    )
    stored.status = TaskStatus.RUNNING
    stored.lease_owner = "w1"
    stored.lease_expires_at = NOW - timedelta(seconds=1)
    stored.set_back_claims = 2
    session.commit()
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.error_message, stored.finished_at) == (
        TaskStatus.FAILED,
        SET_BACK_LOST,
        NOW,
    )
    assert _task(session, "gold.daily").status is TaskStatus.UPSTREAM_FAILED


def test_the_end_of_a_lost_set_back_misses_a_task_that_is_not_setting_back(
    session: Session, run_id: uuid.UUID
) -> None:
    """Ending a task with the lost set-back text requires that it still is setting back."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=3)
    end = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
        expected_owner="w1",
        expected_claim=3,
        lost=True,
        error_message=SET_BACK_LOST,
        ends_set_back=True,
    )
    assert apply_transitions(session, [end], NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING


@pytest.mark.parametrize("cancelled", [False, True])
def test_the_end_of_a_lost_set_back_applies_whatever_its_text_says(
    session: Session, run_id: uuid.UUID, cancelled: bool
) -> None:
    """The end is keyed on its flag: failed in a normal run, cancelled in a cancelled one."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=3)
    stored = _task(session, "silver.clean")
    stored.setting_back = True
    stored.set_back_claims = 2
    session.commit()
    if cancelled:
        _cancel(session, run_id)
    end = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED if cancelled else TaskStatus.FAILED,
        expected_owner="w1",
        expected_claim=3,
        lost=True,
        error_message="the text is payload only",
        ends_set_back=True,
    )
    assert apply_transitions(session, [end], NOW) is True
    stored = _task(session, "silver.clean")
    assert stored.status is (TaskStatus.CANCELLED if cancelled else TaskStatus.FAILED)
    assert stored.error_message == "the text is payload only"


@pytest.mark.parametrize("cancelled", [False, True])
def test_the_scheduler_ends_a_lost_set_back_with_the_lost_text(
    session: Session, run_id: uuid.UUID, cancelled: bool
) -> None:
    """Through a whole tick: the stored end carries SET_BACK_LOST in either kind of run."""
    _lost_task_with_write(session, run_id, rows=None, attempt=3)
    stored = _task(session, "silver.clean")
    stored.setting_back = True
    stored.set_back_claims = 2
    session.commit()
    if cancelled:
        _cancel(session, run_id)
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert stored.status is (TaskStatus.CANCELLED if cancelled else TaskStatus.FAILED)
    assert stored.error_message == SET_BACK_LOST


def test_a_cancel_that_lands_before_a_retry_reclaim_makes_it_miss(
    session: Session, run_id: uuid.UUID
) -> None:
    """Snapshot: may have written, attempts left, no cancel. The cancel lands: the retry misses.

    The next tick then takes the set-back path instead of cancelling a task whose process may
    have written.
    """
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=1)
    _baseline(session, task)
    stale = tick(load_snapshots(session), NOW, _half)
    first = stale[0]
    assert isinstance(first, TaskTransition)
    assert (first.status, first.setting_back) == (TaskStatus.PENDING, False)
    _cancel(session, run_id)
    assert apply_transitions(session, stale, NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back, stored.attempt) == (TaskStatus.READY, True, 1)


def test_a_cancel_reclaim_misses_a_task_that_may_have_written(
    session: Session, run_id: uuid.UUID
) -> None:
    """A plain cancel of a lost task is refused while it may have written: set back first."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=1)
    _baseline(session, task)
    _cancel(session, run_id)
    cancel = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
    )
    assert apply_transitions(session, [cancel], NOW) is False


def test_a_stale_snapshot_of_a_stamped_task_in_a_cancelled_run_misses_when_the_lease_is_renewed(
    session: Session, run_id: uuid.UUID
) -> None:
    """Snapshot: stamped plus cancel. A late renewal makes the lease valid again: it misses."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=1)
    _stamp(session, task)
    _cancel(session, run_id)
    stale = tick(load_snapshots(session), NOW, _half)
    first = stale[0]
    assert isinstance(first, TaskTransition)
    assert (first.status, first.setting_back) == (TaskStatus.READY, True)
    _task(session, "silver.clean").lease_expires_at = NOW + timedelta(seconds=30)
    session.commit()
    assert apply_transitions(session, stale, NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING


def test_a_stale_snapshot_of_a_stamped_task_in_a_cancelled_run_misses_when_the_stamp_is_gone(
    session: Session, run_id: uuid.UUID
) -> None:
    """A rollback cleared the stamp and the write left no evidence: nothing is left to set back."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=1)
    _stamp(session, task)
    _cancel(session, run_id)
    stale = tick(load_snapshots(session), NOW, _half)
    stored = _task(session, "silver.clean")
    stored.written_table_id = None
    session.commit()
    assert apply_transitions(session, stale, NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.RUNNING


def _waiting_retry(session: Session, run_id: uuid.UUID, *, baseline: bool) -> TaskRun:
    """Leave `silver.clean` pending for its next attempt after a first one, as a lost reclaim does.

    Parameters
    ----------
    session
        Database session; committed here.
    run_id
        The pipeline run.
    baseline
        Whether the task recorded a baseline, as a Python table task with checks does.

    Returns
    -------
    TaskRun
        The task.
    """
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=1)
    if baseline:
        _baseline(session, task)
    stored = _task(session, "silver.clean")
    stored.status = TaskStatus.PENDING
    stored.lease_owner = stored.lease_expires_at = None
    stored.next_attempt_at = NOW + timedelta(minutes=5)
    session.commit()
    return stored


def test_the_snapshot_says_which_waiting_tasks_have_a_baseline_and_no_stamp(
    session: Session, run_id: uuid.UUID
) -> None:
    """`baselined` holds for a waiting task too, and a stamp or no checks ends it."""
    task = _waiting_retry(session, run_id, baseline=True)

    def baselined() -> bool:
        (snapshot,) = [
            t for r in load_snapshots(session) for t in r.tasks if t.asset == "silver.clean"
        ]
        return snapshot.baselined

    assert baselined() is True
    _stamp(session, task)
    assert baselined() is False
    stored = _task(session, "silver.clean")
    stored.written_table_id = None
    stored.has_checks = False
    session.commit()
    assert baselined() is False


def test_a_cancel_sets_back_a_pending_retry_with_a_baseline(
    session: Session, run_id: uuid.UUID
) -> None:
    """The first attempt may have left rows: the retry is readied to set back, not cancelled."""
    _waiting_retry(session, run_id, baseline=True)
    _cancel(session, run_id)
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back, stored.ready_at) == (TaskStatus.READY, True, NOW)
    assert (stored.attempt, stored.claim_count, stored.set_back_claims) == (1, 1, 0)
    assert _task(session, "gold.daily").status is TaskStatus.CANCELLED


def test_a_cancel_still_ends_a_pending_retry_without_a_baseline(
    session: Session, run_id: uuid.UUID
) -> None:
    """Nothing can have been left in the table: the cancel ends the task as before."""
    _waiting_retry(session, run_id, baseline=False)
    _cancel(session, run_id)
    scheduler_tick(session, NOW, _half)
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back) == (TaskStatus.CANCELLED, False)


def _cancel_set_back(run_id: uuid.UUID, task: TaskRun, claim: int) -> TaskTransition:
    """Build the cancel loop's transition that sends a pending task to set its table back.

    Parameters
    ----------
    run_id
        The run.
    task
        The task.
    claim
        The claim counter the snapshot saw.

    Returns
    -------
    TaskTransition
        The transition.
    """
    return TaskTransition(
        run_id,
        task.id,
        TaskStatus.PENDING,
        TaskStatus.READY,
        expected_claim=claim,
        ready_at=NOW,
        setting_back=True,
    )


def test_the_cancel_set_back_requires_its_evidence_at_the_write(
    session: Session, run_id: uuid.UUID
) -> None:
    """A cancel, unchecked rows (a stamp awaiting checks or a baseline), the same claim."""
    task = _waiting_retry(session, run_id, baseline=False)
    move = _cancel_set_back(run_id, task, 1)
    assert apply_transitions(session, [move], NOW) is False  # nothing in the table
    _baseline(session, task)
    assert apply_transitions(session, [move], NOW) is False  # nobody cancelled
    _cancel(session, run_id)
    assert apply_transitions(session, [_cancel_set_back(run_id, task, 2)], NOW) is False
    assert apply_transitions(session, [move], NOW) is True
    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back) == (TaskStatus.READY, True)


def test_the_cancel_set_back_takes_a_stamp_awaiting_checks_as_evidence(
    session: Session, run_id: uuid.UUID
) -> None:
    """A SQL task records no baseline: its stamp is the evidence."""
    task = _waiting_retry(session, run_id, baseline=False)
    _stamp(session, task)
    _cancel(session, run_id)
    assert apply_transitions(session, [_cancel_set_back(run_id, task, 1)], NOW) is True


def test_the_cancel_set_back_misses_a_task_that_is_setting_back_already(
    session: Session, run_id: uuid.UUID
) -> None:
    """Only the scheduler sets the flag, once; a second transition that sets it misses."""
    task = _waiting_retry(session, run_id, baseline=True)
    stored = _task(session, "silver.clean")
    stored.setting_back = True
    session.commit()
    _cancel(session, run_id)
    assert apply_transitions(session, [_cancel_set_back(run_id, task, 1)], NOW) is False


def test_a_stale_cancel_set_back_misses_when_the_rows_were_set_back_since(
    session: Session, run_id: uuid.UUID
) -> None:
    """Snapshot: stamped and waiting. The stamp is gone with no evidence left: it misses."""
    task = _waiting_retry(session, run_id, baseline=False)
    _stamp(session, task)
    _cancel(session, run_id)
    stale = tick(load_snapshots(session), NOW, _half)
    (first,) = [t for t in stale if isinstance(t, TaskTransition) and t.task_id == task.id]
    assert (first.status, first.setting_back) == (TaskStatus.READY, True)
    stored = _task(session, "silver.clean")
    stored.written_table_id = None
    session.commit()
    assert apply_transitions(session, stale, NOW) is False
    assert _task(session, "silver.clean").status is TaskStatus.PENDING


def test_a_stale_cancel_set_back_misses_a_task_claimed_again_since(
    session: Session, run_id: uuid.UUID
) -> None:
    """A claim and a drain since the snapshot leave it pending, under a later claim: it misses."""
    _waiting_retry(session, run_id, baseline=True)
    _cancel(session, run_id)
    stale = tick(load_snapshots(session), NOW, _half)
    stored = _task(session, "silver.clean")
    stored.claim_count = 2
    session.commit()
    assert apply_transitions(session, stale, NOW) is False
    assert _task(session, "silver.clean").setting_back is False


def test_a_stale_cancel_misses_a_waiting_task_that_has_unchecked_rows_since(
    session: Session, run_id: uuid.UUID
) -> None:
    """Snapshot: no baseline, so a plain cancel. A baseline since makes it miss for a set-back."""
    task = _waiting_retry(session, run_id, baseline=False)
    _cancel(session, run_id)
    stale = tick(load_snapshots(session), NOW, _half)
    (first,) = [t for t in stale if isinstance(t, TaskTransition) and t.task_id == task.id]
    assert first.status is TaskStatus.CANCELLED
    _baseline(session, task)
    assert apply_transitions(session, stale, NOW) is False
    assert scheduler_tick(session, NOW, _half) > 0
    assert _task(session, "silver.clean").setting_back is True


def test_a_reclaimed_lease_gives_its_attempt_the_worker_lost_code(
    session: Session, run_id: uuid.UUID
) -> None:
    """A retry is no failure of the task, but the lost attempt says why it did not finish."""
    task = _running_with_open_attempt(session, NOW - timedelta(seconds=1))
    scheduler_tick(session, NOW, _half)

    attempt = session.scalars(select(TaskAttempt)).one()
    session.refresh(task)
    assert (task.status, task.failure_code) == (TaskStatus.PENDING, None)
    assert (attempt.outcome, attempt.failure_code) == (AttemptOutcome.LOST, "worker_lost")


def test_a_task_that_runs_out_of_attempts_after_a_lost_worker_stores_worker_lost(
    session: Session, run_id: uuid.UUID
) -> None:
    """The failed task and its attempt carry the code; the task below it is `upstream_failed`."""
    task = _running_with_open_attempt(session, NOW - timedelta(seconds=1))
    task.attempt = task.max_attempts
    session.commit()
    scheduler_tick(session, NOW, _half)

    attempt = session.scalars(select(TaskAttempt)).one()
    session.refresh(task)
    assert (task.status, task.failure_code, attempt.failure_code) == (
        TaskStatus.FAILED,
        "worker_lost",
        "worker_lost",
    )
    assert _task(session, "gold.daily").failure_code == "upstream_failed"


def test_a_cancelled_run_stores_the_cancelled_code_on_the_tasks_it_ends(
    session: Session, run_id: uuid.UUID
) -> None:
    """The cancel loop ends the waiting tasks with the `cancelled` code."""
    run = session.get(Run, run_id)
    assert run is not None
    run.cancel_requested = True
    session.commit()
    scheduler_tick(session, NOW, _half)

    assert _task(session, "silver.clean").failure_code == "cancelled"
    assert _task(session, "gold.daily").failure_code == "cancelled"


def test_a_set_back_reclaim_clears_the_code_an_earlier_attempt_stored(
    session: Session, run_id: uuid.UUID
) -> None:
    """The next worker ends the task with a reason of its own; an earlier failure's code goes."""
    task, _ = _lost_task_with_write(session, run_id, rows=None, attempt=1)
    _stamp(session, task)
    _cancel(session, run_id)
    stored = _task(session, "silver.clean")
    stored.failure_code = "timeout"
    stored.error_message = "too slow"
    session.commit()
    reclaim = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.READY,
        expected_owner="w1",
        expected_claim=1,
        lost=True,
        ready_at=NOW,
        setting_back=True,
    )

    assert apply_transitions(session, [reclaim], NOW) is True

    stored = _task(session, "silver.clean")
    assert (stored.error_message, stored.failure_code) == (None, None)


# --- #99: checks that never finish are bounded, set back and announced ----------------------


def _check_claims(session: Session, task: TaskRun, claims: int) -> None:
    """Store how many claims a task took only to run its checks.

    Parameters
    ----------
    session
        Database session; committed here.
    task
        The task run.
    claims
        Its check claims.
    """
    stored = session.get(TaskRun, task.id)
    assert stored is not None
    stored.check_claims = claims
    session.commit()


def _waiting_for_checks(session: Session, run_id: uuid.UUID, claims: int) -> TaskRun:
    """Leave `silver.clean` stamped and pending again after a check claim that could not start.

    Parameters
    ----------
    session
        Database session.
    run_id
        The pipeline run.
    claims
        Its check claims.

    Returns
    -------
    TaskRun
        The task.
    """
    task, _ = _lost_task_with_write(session, run_id, rows=3, attempt=6)
    _stamp(session, task)
    waiting = _task(session, "silver.clean")
    waiting.status = TaskStatus.PENDING
    waiting.lease_owner = waiting.lease_expires_at = None
    waiting.next_attempt_at = NOW + timedelta(minutes=10)
    waiting.check_claims = claims
    session.commit()
    return waiting


def test_the_snapshot_carries_the_check_claims(session: Session, run_id: uuid.UUID) -> None:
    """The tick reads the counter from the row."""
    _check_claims(session, _lost_task_with_write(session, run_id, rows=3)[0], 4)

    (snapshot,) = load_snapshots(session)
    assert {t.asset: t.check_claims for t in snapshot.tasks} == {
        "silver.clean": 4,
        "gold.daily": 0,
    }


def test_a_task_lost_in_its_last_check_claim_is_set_back_saying_why(
    session: Session, run_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    """#99, loop 1: at the bound the next claim only sets the table back; the reason waits for it.

    Parameters
    ----------
    session
        Database session.
    run_id
        The pipeline run.
    caplog
        Captures the operator's log line.
    """
    task, _ = _lost_task_with_write(session, run_id, rows=3, attempt=6)
    _stamp(session, task)
    _check_claims(session, task, MAX_CHECK_CLAIMS)

    with caplog.at_level("WARNING", logger=store.logger.name):
        scheduler_tick(session, NOW, _half)

    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back, stored.ready_at, stored.finished_at) == (
        TaskStatus.READY,
        True,
        NOW,
        None,
    )
    assert (stored.error_message, stored.failure_code) == (
        CHECKS_UNFINISHED_MESSAGE,
        "checks_unfinished",
    )
    assert (stored.attempt, stored.set_back_claims) == (6, 0)
    assert _task(session, "gold.daily").status is TaskStatus.PENDING
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "checks_unfinished"]
    assert str(task.id) in record.getMessage() and "silver.clean" in record.getMessage()


def test_a_waiting_task_whose_checks_could_not_start_is_set_back_at_the_bound(
    session: Session, run_id: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    """#99, loop 2: the backoff is not waited out; the task goes to set its table back now.

    Parameters
    ----------
    session
        Database session.
    run_id
        The pipeline run.
    caplog
        Captures the operator's log line.
    """
    waiting = _waiting_for_checks(session, run_id, MAX_CHECK_CLAIMS)

    with caplog.at_level("WARNING", logger=store.logger.name):
        scheduler_tick(session, NOW, _half)

    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back, stored.ready_at) == (TaskStatus.READY, True, NOW)
    assert (stored.error_message, stored.failure_code) == (
        CHECKS_UNFINISHED_MESSAGE,
        "checks_unfinished",
    )
    assert stored.claim_count == waiting.claim_count
    assert [r for r in caplog.records if getattr(r, "event", None) == "checks_unfinished"]


def test_a_waiting_task_below_the_bound_keeps_its_backoff(
    session: Session, run_id: uuid.UUID
) -> None:
    """One check claim short: nothing moves before its retry time."""
    _waiting_for_checks(session, run_id, MAX_CHECK_CLAIMS - 1)

    scheduler_tick(session, NOW, _half)

    stored = _task(session, "silver.clean")
    assert (stored.status, stored.setting_back) == (TaskStatus.PENDING, False)


def test_a_stale_unfinished_set_back_misses_a_task_that_no_longer_awaits_its_checks(
    session: Session, run_id: uuid.UUID
) -> None:
    """The waiting set-back repeats its evidence: a task whose stamp and evidence are gone misses.

    Parameters
    ----------
    session
        Database session.
    run_id
        The pipeline run.
    """
    waiting = _waiting_for_checks(session, run_id, MAX_CHECK_CLAIMS)
    transitions = tick(load_snapshots(session), NOW, _half)
    session.rollback()
    stored = session.get(TaskRun, waiting.id)
    assert stored is not None
    stored.written_table_id = None
    stored.query_run_id = None
    session.commit()

    assert apply_transitions(session, transitions, NOW) is False
    assert _task(session, "silver.clean").setting_back is False


def test_a_stale_lost_reclaim_at_the_bound_misses_a_task_below_it(
    session: Session, run_id: uuid.UUID
) -> None:
    """The lost set-back repeats the bound: below it, the statement does not match."""
    task, _ = _lost_task_with_write(session, run_id, rows=3, attempt=6)
    _stamp(session, task)
    reclaim = TaskTransition(
        run_id,
        task.id,
        TaskStatus.RUNNING,
        TaskStatus.READY,
        expected_owner="w1",
        expected_claim=6,
        lost=True,
        ready_at=NOW,
        setting_back=True,
        checks_unfinished=True,
    )
    _check_claims(session, task, MAX_CHECK_CLAIMS - 1)
    assert apply_transitions(session, [reclaim], NOW) is False
    _check_claims(session, task, MAX_CHECK_CLAIMS)
    assert apply_transitions(session, [reclaim], NOW) is True
