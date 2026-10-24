"""`client.runs` and `client.scheduler` against the in-process server."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import lakana
from lakana.errors import Conflict, RunFailed, ValidationError
from lakana_core.models import Run, RunStatus, TaskRun, TaskStatus
from tests.api.platform.test_deployments_run_as import _deploy
from tests.conftest import AUTH_HEADER


def _finish(client: TestClient, run_id: str, run: RunStatus, task: TaskStatus) -> None:
    """Finish a run and its tasks as the scheduler would.

    Parameters
    ----------
    client
        The test client.
    run_id
        The run.
    run
        Its final status.
    task
        Every task's final status.
    """
    with Session(client.app.state.engine) as session:
        row = session.get(Run, uuid.UUID(run_id))
        assert row is not None
        row.status = run
        for t in session.query(TaskRun).filter(TaskRun.run_id == row.id):
            t.status = task
            t.error_message = "boom" if task is TaskStatus.FAILED else None
        session.commit()


def test_start_get_list_log_and_wait(lakana_client: lakana.Client, client: TestClient) -> None:
    """The happy path through every read."""
    _deploy(client)
    run = lakana_client.runs.start("dev", "analytics", select=["silver.*"])
    assert (run.kind, run.status, [t.asset for t in run.tasks]) == (
        "selection",
        "queued",
        ["silver.clean"],
    )
    assert lakana_client.runs.get("dev", run.id).id == run.id
    assert [r.id for r in lakana_client.runs.list("dev", repository="analytics")] == [run.id]
    assert [r.id for r in lakana_client.runs.list("dev", limit=1)] == [run.id]
    with pytest.raises(ValidationError):
        lakana_client.runs.list("dev", limit=0)
    assert lakana_client.runs.log("dev", run.id, "silver.clean").text == ""
    seen: list[str] = []
    _finish(client, run.id, RunStatus.SUCCEEDED, TaskStatus.SUCCEEDED)
    done = lakana_client.runs.wait(
        "dev", run.id, timeout=5, on_update=lambda r: seen.append(r.status)
    )
    assert done.status == "succeeded"
    assert seen == ["succeeded"]
    assert lakana_client.scheduler.status().active_runs == 0


def test_wait_raises_run_failed_and_times_out(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    """A failed or cancelled run raises `RunFailed`; an unfinished one times out."""
    _deploy(client)
    run = lakana_client.runs.start("dev", "analytics")
    with pytest.raises(Conflict):
        lakana_client.runs.start("dev", "analytics")
    with pytest.raises(TimeoutError):
        lakana_client.runs.wait("dev", run.id, timeout=0.2, poll_interval=0.05)
    assert lakana_client.runs.cancel("dev", run.id).cancel_requested
    _finish(client, run.id, RunStatus.FAILED, TaskStatus.FAILED)
    with pytest.raises(RunFailed, match="failed") as info:
        lakana_client.runs.wait("dev", run.id, timeout=5)
    assert info.value.run.status == "failed"
    resumed = lakana_client.runs.resume("dev", run.id)
    assert resumed.resumed_from_run_id == run.id


def test_a_running_runs_tasks_say_why_they_wait(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    """A task of a running run carries its reason through the SDK; a queued run's does not."""
    _deploy(client)
    run = lakana_client.runs.start("dev", "analytics")
    assert [t.waiting_for for t in lakana_client.runs.get("dev", run.id).tasks] == [None]
    with Session(client.app.state.engine) as session:
        row = session.get(Run, uuid.UUID(run.id))
        assert row is not None
        row.status = RunStatus.RUNNING
        session.commit()
    (task,) = lakana_client.runs.get("dev", run.id).tasks
    assert task.waiting_for is not None
    assert (task.waiting_for.code, task.waiting_for.workers) == ("no_worker", 0)


def test_the_query_list_returns_entries_without_sql(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    """`client.queries.list` gives typed entries: no field for the SQL, the reason parsed."""
    client.post(
        "/api/v1/environments",
        json={"name": "dev", "storage": {"scheme": "file", "root": "file:///tmp/sdk-list"}},
        headers=AUTH_HEADER,
    )
    first = lakana_client.queries.submit("SELECT 'needle_77'", "dev")
    second = lakana_client.queries.submit("SELECT 2", "dev")
    entries = lakana_client.queries.list("dev")
    assert [e.id for e in entries] == [first.id, second.id]
    assert entries[0].submitter == "admin"
    assert (entries[0].kind, entries[0].target, entries[0].status) == ("read", None, "QUEUED")
    assert entries[1].waiting_for is not None
    assert (entries[1].waiting_for.code, entries[1].waiting_for.ahead) == ("no_worker", None)
    assert "needle_77" not in repr(entries)
    assert [e.id for e in lakana_client.queries.list("dev", limit=1)] == [first.id]
    first.cancel()
    assert [e.id for e in lakana_client.queries.list("dev")] == [second.id]
    ended = lakana_client.queries.list("dev", include_ended=True)
    assert {e.id for e in ended} == {first.id, second.id}
    with pytest.raises(ValidationError):
        lakana_client.queries.list("dev", limit=0)


def test_the_worker_list_returns_typed_entries(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    """`client.workers.list` gives the workers with their state; others are refused."""
    from datetime import UTC, datetime, timedelta

    from lakana_core.models import Worker

    now = datetime.now(UTC)
    with Session(client.app.state.engine) as session:
        session.add(
            Worker(
                id="w-1",
                host="box",
                version="1.0",
                started_at=now - timedelta(hours=1),
                heartbeat_at=now,
                takes="interactive",
            )
        )
        session.commit()
    (worker,) = lakana_client.workers.list()
    assert worker.takes == "interactive"
    assert (worker.id, worker.host, worker.version, worker.status) == (
        "w-1",
        "box",
        "1.0",
        "alive",
    )
    assert worker.heartbeat_at >= worker.started_at
    assert (worker.query_run_id, worker.deploy_run_id, worker.task_run_id) == (None, None, None)


@pytest.mark.slow
def test_checks_are_typed(lakana_client: lakana.Client, client: TestClient) -> None:
    """Both methods, `iter`, and the task fields come back typed; an older server's body works."""
    from datetime import datetime

    from lakana.resources.runs import run_from_json
    from tests.api.platform.test_runs import _store_checks

    _deploy(client)
    run = lakana_client.runs.start("dev", "analytics")
    assert [(t.kind, t.checks) for t in run.tasks] == [("asset", ())]
    _store_checks(client, run.id, "a", "b", "c")

    (task,) = lakana_client.runs.get("dev", run.id).tasks
    assert [c.check for c in task.checks] == ["a", "b", "c"]
    assert isinstance(task.checks, tuple)

    for_run = lakana_client.checks.for_run("dev", run.id)
    assert [c.name for c in for_run] == ["silver.clean/a", "silver.clean/b", "silver.clean/c"]
    first = for_run[0]
    assert (first.run_id, first.task, first.table, first.outcome) == (
        run.id,
        "silver.clean",
        "silver.clean",
        "passed",
    )
    assert (first.on_failure, first.failing_rows, first.policy_note) == ("block", 0, None)
    assert isinstance(first.started_at, datetime)
    assert isinstance(first.finished_at, datetime)

    page = lakana_client.checks.list("dev", limit=2)
    assert [c.check for c in page.items] == ["c", "b"]
    assert page.next_cursor is not None
    rest = lakana_client.checks.list("dev", limit=2, cursor=page.next_cursor)
    assert ([c.check for c in rest.items], rest.next_cursor) == (["a"], None)
    assert lakana_client.checks.list("dev", table="gold.none").items == []

    assert [c.check for c in lakana_client.checks.iter("dev")] == ["c", "b", "a"]
    assert [c.check for c in lakana_client.checks.iter("dev", table="silver.clean")] == [
        "c",
        "b",
        "a",
    ]

    body = {
        "id": run.id,
        "environment": "dev",
        "repository": "analytics",
        "project_version_id": run.project_version_id,
        "commit_sha": "a" * 40,
        "kind": "full",
        "status": "queued",
        "cancel_requested": False,
        "triggered_by": None,
        "run_as": None,
        "resumed_from_run_id": None,
        "selection": {},
        "error_message": None,
        "created_at": "2026-10-11T12:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "tasks": [
            {
                "asset": "silver.clean",
                "status": "pending",
                "attempt": 0,
                "max_attempts": 1,
                "timeout_seconds": 60,
                "next_attempt_at": None,
                "started_at": None,
                "finished_at": None,
                "error_message": None,
                "query_run_id": None,
                "rows_written": None,
            }
        ],
    }
    (old,) = run_from_json(body).tasks
    assert (old.kind, old.checks, old.kept_check) == ("asset", (), False)
    body["tasks"][0].update(kind="source_check", kept_check=True)
    (kept,) = run_from_json(body).tasks
    assert (kept.kind, kept.kept_check) == ("source_check", True)


def test_the_rows_of_a_check_come_back_as_the_handle_of_a_query(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    """`checks.rows` returns what `queries.submit` returns, queued under the caller's name."""
    from lakana.resources.queries import QueryRun
    from tests.api.platform.test_runs import _CHECK, _store_failed_check

    _deploy(client)
    run = lakana_client.runs.start("dev", "analytics")
    _store_failed_check(client, run.id)

    handle = lakana_client.checks.rows(run.id, _CHECK, environment="dev")

    assert isinstance(handle, QueryRun)
    assert (handle.environment, handle.status().status) == ("dev", "QUEUED")


def test_the_rows_of_a_check_raise_the_sdks_errors(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    """A check the run lacks is `NotFound`; one without a snapshot is `Conflict`."""
    from lakana.errors import NotFound
    from tests.api.platform.test_runs import _CHECK, _store_failed_check

    _deploy(client)
    run = lakana_client.runs.start("dev", "analytics")

    with pytest.raises(NotFound, match="has no result for the check"):
        lakana_client.checks.rows(run.id, _CHECK, environment="dev")
    _store_failed_check(client, run.id, snapshot=None)
    with pytest.raises(Conflict, match="has no rows to show"):
        lakana_client.checks.rows(run.id, _CHECK, environment="dev")


def test_a_failed_tasks_failure_and_a_runs_failed_task_count_reach_the_sdk(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    """The server's code, cause and next step come back typed, for the task and its attempt."""
    from lakana_core.models import AttemptOutcome, TaskAttempt
    from lakana_core.runs.failures import FAILURES, FailureCode

    _deploy(client)
    run = lakana_client.runs.start("dev", "analytics")
    assert [t.failure for t in run.tasks] == [None]
    with Session(client.app.state.engine) as session:
        task = session.query(TaskRun).filter(TaskRun.run_id == uuid.UUID(run.id)).one()
        task.status = TaskStatus.FAILED
        task.error_message = "timed out after 10 s"
        task.failure_code = "timeout"
        session.add(
            TaskAttempt(
                id=uuid.uuid4(),
                task_run_id=task.id,
                attempt=1,
                worker="w1",
                started_at=datetime.now(UTC),
                outcome=AttemptOutcome.TIMED_OUT,
                failure_code="timeout",
            )
        )
        session.commit()

    cause, next_step = FAILURES[FailureCode.TIMEOUT]
    shown = lakana_client.runs.get("dev", run.id)
    (failed,) = shown.tasks
    assert failed.failure is not None
    assert (failed.failure.code, failed.failure.cause, failed.failure.next_step) == (
        "timeout",
        cause,
        next_step,
    )
    assert failed.error_message == "timed out after 10 s"
    assert shown.failed_tasks == 1
    assert [r.failed_tasks for r in lakana_client.runs.list("dev")] == [1]
    (attempt,) = lakana_client.runs.log("dev", run.id, "silver.clean").attempts
    assert attempt.failure is not None and attempt.failure.code == "timeout"
