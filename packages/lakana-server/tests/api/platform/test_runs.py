"""Runs over REST."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.models import Run, RunStatus, TaskRun, TaskStatus
from lakana_core.runs.service import task_log_path
from lakana_core.storage.resolver import StorageResolver
from tests.api.platform.test_deployments_run_as import _deploy
from tests.api.platform.test_deploys import _token_for
from tests.conftest import AUTH_HEADER

_RUNS = "/api/v1/environments/dev/runs"


def test_start_show_list_and_log(client: TestClient) -> None:
    """A run starts queued with its tasks, shows, lists and has an (empty) task log."""
    _deploy(client)
    started = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"})
    assert started.status_code == 202, started.text
    body = started.json()
    assert (body["status"], body["kind"], body["run_as"]) == ("queued", "full", "admin")
    assert [t["asset"] for t in body["tasks"]] == ["silver.clean"]
    shown = client.get(f"{_RUNS}/{body['id']}", headers=AUTH_HEADER).json()
    assert shown["tasks"][0]["status"] == "pending"
    listed = client.get(_RUNS, headers=AUTH_HEADER, params={"repository": "analytics"}).json()
    assert [r["id"] for r in listed] == [body["id"]]
    assert listed[0]["tasks"] == []
    log = client.get(f"{_RUNS}/{body['id']}/tasks/silver.clean/log", headers=AUTH_HEADER)
    assert log.status_code == 200
    assert log.json() == {
        "run_id": body["id"],
        "asset": "silver.clean",
        "text": "",
        "attempts": [],
        "next_offset": 0,
        "more": False,
        "complete": False,
        "reset": False,
    }


def test_task_log_from_an_offset(client: TestClient) -> None:
    """`offset` returns the text from that byte and where to continue; bad values are 422."""
    _deploy(client)
    run = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    url = f"{_RUNS}/{run['id']}/tasks/silver.clean/log"
    with Session(client.app.state.engine) as session:
        StorageResolver(session).for_environment("dev").storage.write(
            task_log_path(uuid.UUID(run["id"]), "silver.clean"), "ä one\ntwo\n".encode()
        )
    whole = client.get(url, headers=AUTH_HEADER).json()
    assert (whole["text"], whole["next_offset"]) == ("ä one\ntwo\n", 11)
    tail = client.get(url, headers=AUTH_HEADER, params={"offset": 7}).json()
    assert (tail["text"], tail["next_offset"], tail["reset"]) == ("two\n", 11, False)
    zero = client.get(url, headers=AUTH_HEADER, params={"offset": 0}).json()
    assert zero["text"] == whole["text"]
    past = client.get(url, headers=AUTH_HEADER, params={"offset": 50})
    assert past.status_code == 200
    assert (past.json()["text"], past.json()["next_offset"], past.json()["reset"]) == ("", 11, True)
    for bad in ("-1", "abc", "1.5"):
        assert client.get(url, headers=AUTH_HEADER, params={"offset": bad}).status_code == 422
    assert client.get(url).status_code == 401


def test_second_start_is_409_and_cancel_then_resume(client: TestClient) -> None:
    """One active run; cancel flags it; resume of a cancelled run creates a resume run."""
    _deploy(client)
    first = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    again = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"})
    assert again.status_code == 409
    assert first["id"] in again.text
    cancelled = client.post(f"{_RUNS}/{first['id']}/cancel", headers=AUTH_HEADER)
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is True
    with Session(client.app.state.engine) as session:
        run = session.get(Run, uuid.UUID(first["id"]))
        assert run is not None
        run.status = RunStatus.CANCELLED
        for task in session.query(TaskRun).filter(TaskRun.run_id == run.id):
            task.status = TaskStatus.CANCELLED
        session.commit()
    resumed = client.post(f"{_RUNS}/{first['id']}/resume", headers=AUTH_HEADER)
    assert resumed.status_code == 202, resumed.text
    assert (resumed.json()["kind"], resumed.json()["resumed_from_run_id"]) == (
        "resume",
        first["id"],
    )


def test_request_errors(client: TestClient) -> None:
    """Bad selection 422, unknown run 404, malformed body 422, no RUN 403."""
    _deploy(client)
    assert (
        client.post(
            _RUNS, headers=AUTH_HEADER, json={"repository": "analytics", "select": ["x.*"]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            _RUNS, headers=AUTH_HEADER, json={"repository": "analytics", "bogus": 1}
        ).status_code
        == 422
    )
    assert client.post(_RUNS, headers=AUTH_HEADER, json={"repository": ""}).status_code == 422
    assert client.get(f"{_RUNS}/{uuid.uuid4()}", headers=AUTH_HEADER).status_code == 404
    assert client.get(f"{_RUNS}/not-a-uuid", headers=AUTH_HEADER).status_code == 422
    outsider = _token_for(client, "outsider")
    assert client.post(_RUNS, headers=outsider, json={"repository": "analytics"}).status_code == 403
    assert client.get(_RUNS, headers=outsider).status_code == 403


def test_scheduler_status(client: TestClient) -> None:
    """Admin sees holder and queue lengths; others are refused."""
    _deploy(client)
    client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"})
    status = client.get("/api/v1/scheduler", headers=AUTH_HEADER)
    assert status.status_code == 200
    assert status.json() == {
        "holder": None,
        "heartbeat_at": None,
        "active_runs": 1,
        "pending": 1,
        "ready": 0,
        "running": 0,
    }
    assert client.get("/api/v1/scheduler", headers=_token_for(client, "nosy")).status_code == 403


def test_the_worker_list_is_refused_like_the_scheduler_status(client: TestClient) -> None:
    """Admin sees the workers, with their state and run ids only; others get the 403."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.orm import Session

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
            )
        )
        session.commit()
    listed = client.get("/api/v1/workers", headers=AUTH_HEADER)
    assert listed.status_code == 200, listed.text
    (row,) = listed.json()
    assert set(row) == {
        "id",
        "host",
        "version",
        "started_at",
        "heartbeat_at",
        "status",
        "takes",
        "query_run_id",
        "deploy_run_id",
        "task_run_id",
        "caches",
    }
    assert row["caches"] is None  # not measured yet
    assert row["takes"] == "all"  # the column's default: a worker that did not say
    assert (row["id"], row["host"], row["version"], row["status"]) == ("w-1", "box", "1.0", "alive")
    assert (row["query_run_id"], row["deploy_run_id"], row["task_run_id"]) == (None, None, None)
    nosy = _token_for(client, "nosy")
    refused = client.get("/api/v1/workers", headers=nosy)
    assert refused.status_code == 403
    assert refused.json() == client.get("/api/v1/scheduler", headers=nosy).json()
    assert client.get("/api/v1/workers").status_code == 401


def test_the_worker_list_has_takes(client: TestClient) -> None:
    """Each worker is listed with the work it takes."""
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from lakana_core.models import Worker

    now = datetime.now(UTC)
    with Session(client.app.state.engine) as session:
        for worker_id, takes in (("w-1", "pipeline"), ("w-2", "interactive")):
            session.add(
                Worker(
                    id=worker_id,
                    host="box",
                    version="1.0",
                    started_at=now,
                    heartbeat_at=now,
                    takes=takes,
                )
            )
        session.commit()
    listed = client.get("/api/v1/workers", headers=AUTH_HEADER)
    assert {row["id"]: row["takes"] for row in listed.json()} == {
        "w-1": "pipeline",
        "w-2": "interactive",
    }


def test_the_worker_list_has_the_sizes_of_the_caches(client: TestClient) -> None:
    """A worker that measured its caches is listed with their sizes and when it measured them."""
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from lakana_core.models import Worker

    now = datetime.now(UTC)
    with Session(client.app.state.engine) as session:
        session.add(
            Worker(
                id="w-1",
                host="box",
                version="1.0",
                started_at=now,
                heartbeat_at=now,
                cache_environments_bytes=10,
                cache_packages_bytes=20,
                cache_tasks_bytes=30,
                cache_complete=False,
                cache_measured_at=now,
            )
        )
        session.commit()
    (row,) = client.get("/api/v1/workers", headers=AUTH_HEADER).json()
    caches = row["caches"]
    assert datetime.fromisoformat(caches.pop("measured_at")) == now
    assert caches == {
        "environments_bytes": 10,
        "packages_bytes": 20,
        "tasks_bytes": 30,
        "complete": False,
    }


def test_list_limit_is_bounded_before_it_reaches_the_database(client: TestClient) -> None:
    """A zero, negative or oversized `limit` is 422 (never a Postgres 500); a valid one works."""
    _deploy(client)
    for bad in ("0", "-1", "201", "x"):
        response = client.get(_RUNS, headers=AUTH_HEADER, params={"limit": bad})
        assert response.status_code == 422, bad
    assert client.get(_RUNS, headers=AUTH_HEADER, params={"limit": "200"}).status_code == 200


def test_no_schema_accepts_run_context_or_break_glass(client: TestClient) -> None:
    """Run context, task_run_id and break_glass_reason in a body are refused with 422."""
    _deploy(client)
    for extra in (
        {"run_context": {"run_id": str(uuid.uuid4())}},
        {"task_run_id": str(uuid.uuid4())},
        {"break_glass_reason": "because"},
    ):
        body = {"repository": "analytics", **extra}
        assert client.post(_RUNS, headers=AUTH_HEADER, json=body).status_code == 422, extra


def test_a_runs_tasks_carry_waiting_for(client: TestClient) -> None:
    """A task of a running run says why it waits; a queued run's tasks say nothing."""
    _deploy(client)
    started = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    assert [t["waiting_for"] for t in started["tasks"]] == [None]
    shown = client.get(f"{_RUNS}/{started['id']}", headers=AUTH_HEADER).json()
    assert [t["waiting_for"] for t in shown["tasks"]] == [None]
    with Session(client.app.state.engine) as session:
        run = session.get(Run, uuid.UUID(started["id"]))
        assert run is not None
        run.status = RunStatus.RUNNING
        session.commit()
    shown = client.get(f"{_RUNS}/{started['id']}", headers=AUTH_HEADER).json()
    (task,) = shown["tasks"]
    assert task["waiting_for"] == {
        "code": "no_worker",
        "message": (
            "No worker is running, so this task cannot start. Ask the operator to start a "
            "worker; on a local instance, start it again with 'lakana server start --local'."
        ),
        "workers": 0,
    }


def test_a_task_that_is_setting_its_table_back_says_so(client: TestClient) -> None:
    """The task body has `setting_back`, and a ready task of a cancelled run says why it waits.

    A worker is alive: without one the task would say `no_worker` first, because starting a
    worker is what the user has to do.
    """
    from datetime import timedelta

    from lakana_core.db import db_now
    from lakana_core.models import Worker

    _deploy(client)
    started = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    assert [t["setting_back"] for t in started["tasks"]] == [False]
    with Session(client.app.state.engine) as session:
        beat = db_now(session)
        session.add(
            Worker(
                id="w-1",
                host="box",
                version="1.0",
                started_at=beat - timedelta(hours=1),
                heartbeat_at=beat,
            )
        )
        run = session.get(Run, uuid.UUID(started["id"]))
        assert run is not None
        run.status = RunStatus.RUNNING
        run.cancel_requested = True
        task = session.query(TaskRun).filter(TaskRun.run_id == run.id).one()
        task.status = TaskStatus.READY
        task.setting_back = True
        session.commit()
    shown = client.get(f"{_RUNS}/{started['id']}", headers=AUTH_HEADER).json()
    (task_body,) = shown["tasks"]
    assert task_body["setting_back"] is True
    assert task_body["waiting_for"] == {
        "code": "setting_back",
        "message": (
            "This task's table may hold rows no check has passed; the next free worker sets the "
            "table back."
        ),
    }


@pytest.mark.parametrize("ended", [TaskStatus.FAILED, TaskStatus.CANCELLED])
def test_a_task_that_ended_after_its_set_back_is_not_setting_back(
    client: TestClient, ended: TaskStatus
) -> None:
    """`setting_back` is `false` once the task has ended, although the stored flag stays set.

    Parameters
    ----------
    client
        The test client.
    ended
        The status the set-back claim ended the task with.
    """
    _deploy(client)
    started = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    with Session(client.app.state.engine) as session:
        run = session.get(Run, uuid.UUID(started["id"]))
        assert run is not None
        run.status = RunStatus.RUNNING
        task = session.query(TaskRun).filter(TaskRun.run_id == run.id).one()
        task.status = ended
        task.setting_back = True
        session.commit()
    shown = client.get(f"{_RUNS}/{started['id']}", headers=AUTH_HEADER).json()
    (task_body,) = shown["tasks"]
    assert task_body["status"] == ended.value
    assert task_body["setting_back"] is False


def _store_checks(
    client: TestClient, run_id: str, *names: str, generated: frozenset[str] = frozenset()
) -> None:
    """Insert one passed check result per name for the run's only task.

    Parameters
    ----------
    client
        The test client; its engine is written to directly.
    run_id
        The run.
    *names
        The checks' short names; the n-th ends n minutes after a fixed start.
    generated
        The names whose result is marked as generated from the asset's header.
    """
    from datetime import UTC, datetime, timedelta

    from lakana_core.models import CheckOutcome, CheckResult

    start = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    with Session(client.app.state.engine) as session:
        run = session.get(Run, uuid.UUID(run_id))
        assert run is not None
        task = session.query(TaskRun).filter(TaskRun.run_id == run.id).one()
        for n, name in enumerate(names):
            at = start + timedelta(minutes=n)
            session.add(
                CheckResult(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    task_run_id=task.id,
                    environment_id=run.environment_id,
                    check_name=f"silver.clean/{name}",
                    check_path=f"checks/silver/clean/{name}.sql",
                    table_name="silver.clean",
                    on_failure="block",
                    outcome=CheckOutcome.PASSED,
                    failing_rows=0,
                    started_at=at,
                    finished_at=at,
                    generated=name in generated,
                )
            )
        session.commit()


def test_the_run_route_has_checks_and_kind_per_task(client: TestClient) -> None:
    """A task carries its kind and its check results, by name."""
    _deploy(client)
    run = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    assert (run["tasks"][0]["kind"], run["tasks"][0]["checks"]) == ("asset", [])
    assert run["tasks"][0]["kept_check"] is False
    _store_checks(client, run["id"], "b", "a")
    shown = client.get(f"{_RUNS}/{run['id']}", headers=AUTH_HEADER).json()
    (task,) = shown["tasks"]
    assert [c["check"] for c in task["checks"]] == ["a", "b"]
    first = task["checks"][0]
    assert first["outcome"] == "passed"
    assert first["policy_note"] is None
    assert first["run_id"] == run["id"]
    assert first["table"] == "silver.clean"


def test_the_run_checks_route(client: TestClient) -> None:
    """The checks of a run, with every field of a result."""
    _deploy(client)
    run = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    _store_checks(client, run["id"], "a")
    answer = client.get(f"{_RUNS}/{run['id']}/checks", headers=AUTH_HEADER)
    assert answer.status_code == 200, answer.text
    (result,) = answer.json()
    assert set(result) == {
        "run_id",
        "task",
        "name",
        "check",
        "path",
        "table",
        "table_id",
        "on_failure",
        "outcome",
        "failing_rows",
        "error",
        "checked_snapshot_id",
        "rollback_outcome",
        "policy_applied",
        "policy_note",
        "query_run_id",
        "started_at",
        "finished_at",
        "generated",
    }
    assert (result["name"], result["task"], result["failing_rows"]) == (
        "silver.clean/a",
        "silver.clean",
        0,
    )


def test_the_check_routes_mark_a_check_generated_from_the_header(client: TestClient) -> None:
    """A result from `key:` or `references:` says so; a hand-written one does not."""
    _deploy(client)
    run = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    _store_checks(client, run["id"], "key_unique", "a", generated=frozenset({"key_unique"}))
    of_run = client.get(f"{_RUNS}/{run['id']}/checks", headers=AUTH_HEADER).json()
    assert {c["check"]: c["generated"] for c in of_run} == {"key_unique": True, "a": False}
    listed = client.get("/api/v1/environments/dev/checks", headers=AUTH_HEADER).json()
    assert {c["check"]: c["generated"] for c in listed["items"]} == {
        "key_unique": True,
        "a": False,
    }


def test_the_checks_route_pages_and_filters(client: TestClient) -> None:
    """Newest first, a cursor continues, a table filters, a bad cursor is a 422."""
    _deploy(client)
    run = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    _store_checks(client, run["id"], "a", "b", "c")
    url = "/api/v1/environments/dev/checks"
    first = client.get(url, headers=AUTH_HEADER, params={"limit": 2}).json()
    assert [c["check"] for c in first["items"]] == ["c", "b"]
    second = client.get(
        url, headers=AUTH_HEADER, params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert ([c["check"] for c in second["items"]], second["next_cursor"]) == (["a"], None)
    none = client.get(url, headers=AUTH_HEADER, params={"table": "gold.other"}).json()
    assert none == {"items": [], "next_cursor": None, "hidden": 0}
    some = client.get(url, headers=AUTH_HEADER, params={"table": "silver.clean"}).json()
    assert len(some["items"]) == 3
    assert client.get(url, headers=AUTH_HEADER, params={"cursor": "!!"}).status_code == 422
    assert client.get(url, headers=AUTH_HEADER, params={"limit": 0}).status_code == 422
    assert client.get(url, headers=AUTH_HEADER, params={"limit": 201}).status_code == 422


def test_the_check_routes_refuse_like_the_run_route(client: TestClient) -> None:
    """No token is a 401; a caller without rights gets the answer the run route gives."""
    _deploy(client)
    run = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    one = f"{_RUNS}/{run['id']}"
    many = "/api/v1/environments/dev/checks"
    assert client.get(f"{one}/checks").status_code == 401
    assert client.get(many).status_code == 401
    outsider = _token_for(client, "outsider")
    assert client.get(one, headers=outsider).status_code == 403
    assert client.get(f"{one}/checks", headers=outsider).status_code == 403
    assert client.get(many, headers=outsider).status_code == 403
    assert client.get(f"{_RUNS}/{uuid.uuid4()}/checks", headers=AUTH_HEADER).status_code == 404


_ROWS_SQL = "SELECT id FROM silver.clean GROUP BY id HAVING count(*) > 1"
_CHECK = "silver.clean/unique_id"


def _store_failed_check(
    client: TestClient,
    run_id: str,
    *,
    snapshot: int | None = 4242,
    sql: str | None = _ROWS_SQL,
) -> uuid.UUID:
    """Store a failed check of the run's one task, and its SQL in the run's project version.

    Parameters
    ----------
    client
        The test client; its engine is written to directly.
    run_id
        The run.
    snapshot
        The snapshot the check read; `None` for a check that read none.
    sql
        The check's SQL in the project version's manifest; `None` leaves the check out.

    Returns
    -------
    uuid.UUID
        The result's id.
    """
    from datetime import UTC, datetime

    from lakana_core.models import CheckOutcome, CheckResult, JobVersion

    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    with Session(client.app.state.engine) as session:
        run = session.get(Run, uuid.UUID(run_id))
        assert run is not None
        task = session.query(TaskRun).filter(TaskRun.run_id == run.id).one()
        result = CheckResult(
            id=uuid.uuid4(),
            run_id=run.id,
            task_run_id=task.id,
            environment_id=run.environment_id,
            check_name=_CHECK,
            check_path="checks/silver/clean/unique_id.sql",
            table_name="silver.clean",
            on_failure="block",
            outcome=CheckOutcome.FAILED,
            failing_rows=2,
            checked_snapshot_id=snapshot,
            started_at=now,
            finished_at=now,
        )
        session.add(result)
        version = session.get(JobVersion, run.job_version_id)
        assert version is not None
        entry = {
            "name": _CHECK,
            "table": "silver.clean",
            "check": "unique_id",
            "path": "checks/silver/clean/unique_id.sql",
            "sql": sql,
            "on_failure": "block",
        }
        version.manifest_json = {**version.manifest_json, "checks": [] if sql is None else [entry]}
        session.commit()
        return result.id


def _rows_route(run_id: str) -> str:
    """Name the route that queues the query which shows the rows of a check.

    Parameters
    ----------
    run_id
        The run.

    Returns
    -------
    str
        The path.
    """
    return f"{_RUNS}/{run_id}/checks/rows"


def _started_run(client: TestClient) -> str:
    """Deploy, start a run and return its id.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    str
        The run's id.
    """
    _deploy(client)
    return client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()["id"]


def test_the_rows_of_a_check_are_queued_as_a_query_of_the_caller(client: TestClient) -> None:
    """The answer is the one `POST .../queries` gives, and the run row is bound to the result."""
    from lakana_core.models import QueryRun

    run_id = _started_run(client)
    result_id = _store_failed_check(client, run_id)

    answer = client.post(_rows_route(run_id), headers=AUTH_HEADER, json={"check": _CHECK})

    assert answer.status_code == 202, answer.text
    assert set(answer.json()) == {"query_id"}
    with Session(client.app.state.engine) as session:
        query = session.get(QueryRun, uuid.UUID(answer.json()["query_id"]))
        assert query is not None
        assert (query.sql_text, query.check_result_id) == (_ROWS_SQL, result_id)
        assert query.task_run_id is None
    status = client.get(
        f"/api/v1/environments/dev/queries/{answer.json()['query_id']}", headers=AUTH_HEADER
    )
    assert (status.status_code, status.json()["status"]) == (200, "QUEUED")


def test_a_check_the_run_does_not_have_is_404_with_the_way_to_list_them(
    client: TestClient,
) -> None:
    from lakana_core.runs.checks import check_not_in_run_message

    run_id = _started_run(client)

    answer = client.post(_rows_route(run_id), headers=AUTH_HEADER, json={"check": _CHECK})

    assert answer.status_code == 404
    assert answer.json()["detail"] == check_not_in_run_message(uuid.UUID(run_id), _CHECK, "dev")
    elsewhere = _rows_route(str(uuid.uuid4()))
    assert client.post(elsewhere, headers=AUTH_HEADER, json={"check": _CHECK}).status_code == 404


def test_a_check_without_a_checked_snapshot_is_409_with_the_reason(client: TestClient) -> None:
    from lakana_core.runs.checks import no_checked_snapshot_message

    run_id = _started_run(client)
    _store_failed_check(client, run_id, snapshot=None)

    answer = client.post(_rows_route(run_id), headers=AUTH_HEADER, json={"check": _CHECK})

    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"] == no_checked_snapshot_message(uuid.UUID(run_id))


@pytest.mark.slow
def test_a_check_whose_sql_is_no_longer_stored_is_409_with_the_way_to_the_count(
    client: TestClient,
) -> None:
    from lakana_core.runs.checks import check_sql_gone_message

    run_id = _started_run(client)
    _store_failed_check(client, run_id, sql=None)

    answer = client.post(_rows_route(run_id), headers=AUTH_HEADER, json={"check": _CHECK})

    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"] == check_sql_gone_message(uuid.UUID(run_id), "dev")


def test_an_unbound_task_token_is_refused_the_rows_of_a_check(client: TestClient) -> None:
    """A task token minted over REST and bound to no task is a task."""
    run_id = _started_run(client)
    _store_failed_check(client, run_id)
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={"principal": "admin", "environment": "dev", "asset_scope": [], "ttl_seconds": 300},
    )
    assert minted.status_code == 201, minted.text
    headers = {"Authorization": f"Bearer {minted.json()['token']}"}

    refused = client.post(_rows_route(run_id), headers=headers, json={"check": _CHECK})

    assert refused.status_code == 403, refused.text
    assert "never to a pipeline" in refused.text


def test_the_rows_of_a_check_need_the_right_to_read_the_run_and_to_submit_queries(
    client: TestClient,
) -> None:
    """Each right alone is a 403 that tells nothing about the run; both are a 202 with READ."""
    run_id = _started_run(client)
    _store_failed_check(client, run_id)
    body = {"check": _CHECK}
    assert client.post(_rows_route(run_id), json=body).status_code == 401

    def holder(name: str, *actions: str) -> dict[str, str]:
        header = _token_for(client, name)
        for action in actions:
            granted = client.post(
                "/api/v1/grants",
                headers=AUTH_HEADER,
                json={
                    "principal": name,
                    "action": action,
                    "kind": "ENVIRONMENT",
                    "environment": "dev",
                },
            )
            assert granted.status_code in (200, 201), granted.text
        return header

    for name, actions in (
        ("none", ()),
        ("reads", ("READ_ENVIRONMENT",)),
        ("submits", ("SUBMIT_QUERY",)),
    ):
        header = holder(name, *actions)
        refused = client.post(_rows_route(run_id), headers=header, json=body)
        assert refused.status_code == 403, (name, refused.text)
    both = holder("both", "READ_ENVIRONMENT", "SUBMIT_QUERY")
    no_table = client.post(_rows_route(run_id), headers=both, json=body)
    assert no_table.status_code == 403
    assert "may not read every table this check reads" in no_table.text
    reader = holder("reader", "READ_ENVIRONMENT", "SUBMIT_QUERY", "READ_TABLE")
    assert client.post(_rows_route(run_id), headers=reader, json=body).status_code == 202


def test_the_rows_route_refuses_a_body_that_is_not_one_check(client: TestClient) -> None:
    run_id = _started_run(client)
    route = _rows_route(run_id)
    assert client.post(route, headers=AUTH_HEADER, json={}).status_code == 422
    assert client.post(route, headers=AUTH_HEADER, json={"check": ""}).status_code == 422
    extra = {"check": _CHECK, "check_result_id": str(uuid.uuid4())}
    assert client.post(route, headers=AUTH_HEADER, json=extra).status_code == 422


@pytest.mark.slow
def test_no_request_can_bind_a_query_to_a_check_result(client: TestClient) -> None:
    """No request schema of the API has a field that names a check result or a snapshot."""
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    requested: set[str] = set()

    def collect(node: object) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[-1]
                if name not in requested:
                    requested.add(name)
                    collect(schemas[name])
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    for ops in spec["paths"].values():
        for operation in ops.values():
            if isinstance(operation, dict):
                collect(operation.get("requestBody", {}))
    assert {"QuerySubmit", "CheckRowsRequest"} <= requested
    for name in requested:
        fields = set(schemas[name].get("properties", {}))
        assert not {"check_result_id", "snapshot_id"} & fields, name


def test_a_failed_task_carries_failure_with_the_detail_kept(client: TestClient) -> None:
    """`failure` has the code, the cause and the next step; the text stays in `error_message`."""
    from lakana_core.models import AttemptOutcome, TaskAttempt
    from lakana_core.runs.failures import FAILURES, FailureCode

    _deploy(client)
    started = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    assert [t["failure"] for t in started["tasks"]] == [None]
    assert started["failed_tasks"] == 0
    with Session(client.app.state.engine) as session:
        run = session.get(Run, uuid.UUID(started["id"]))
        assert run is not None
        task = session.query(TaskRun).filter(TaskRun.run_id == run.id).one()
        task.status = TaskStatus.FAILED
        task.error_message = "timed out after 10 s"
        task.failure_code = "timeout"
        session.add(
            TaskAttempt(
                id=uuid.uuid4(),
                task_run_id=task.id,
                attempt=1,
                worker="w1",
                started_at=run.created_at,
                outcome=AttemptOutcome.TIMED_OUT,
                error_message="timed out after 10 s",
                failure_code="timeout",
            )
        )
        session.commit()

    cause, next_step = FAILURES[FailureCode.TIMEOUT]
    shown = client.get(f"{_RUNS}/{started['id']}", headers=AUTH_HEADER).json()
    (task_body,) = shown["tasks"]
    assert task_body["error_message"] == "timed out after 10 s"
    assert task_body["failure"] == {"code": "timeout", "cause": cause, "next_step": next_step}
    assert shown["failed_tasks"] == 1
    listed = client.get(_RUNS, headers=AUTH_HEADER).json()
    assert [(r["id"], r["failed_tasks"]) for r in listed] == [(started["id"], 1)]
    log = client.get(f"{_RUNS}/{started['id']}/tasks/silver.clean/log", headers=AUTH_HEADER).json()
    (attempt,) = log["attempts"]
    assert attempt["failure"] == {"code": "timeout", "cause": cause, "next_step": next_step}


def test_an_old_failed_task_without_a_code_is_unknown_over_rest(client: TestClient) -> None:
    """A row from before the codes is `unknown`, with a generic cause and next step."""
    _deploy(client)
    started = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    with Session(client.app.state.engine) as session:
        task = session.query(TaskRun).filter(TaskRun.run_id == uuid.UUID(started["id"])).one()
        task.status = TaskStatus.FAILED
        task.error_message = "boom"
        session.commit()

    shown = client.get(f"{_RUNS}/{started['id']}", headers=AUTH_HEADER).json()

    (task_body,) = shown["tasks"]
    assert task_body["failure"]["code"] == "unknown"
    assert task_body["failure"]["next_step"]
    assert task_body["error_message"] == "boom"


def test_a_schema_change_shows_policy_details_only_to_an_administrator(client: TestClient) -> None:
    """Non-administrators get one neutral line, the same with or without a policy on the column."""
    _deploy(client)
    started = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    policy_id = "12345678-1234-1234-1234-123456789abc"
    broken = {
        "table": "silver.clean",
        "kind": "row_filter",
        "policy_id": policy_id,
        "column": "region",
        "change": "replaced",
    }

    def store(policies: list[dict[str, str]]) -> None:
        with Session(client.app.state.engine) as session:
            task = session.query(TaskRun).filter(TaskRun.run_id == uuid.UUID(started["id"])).one()
            task.schema_change_json = {"tables": ["silver.clean"], "policies": policies}
            session.commit()

    viewer = _token_for(client, "viewer")
    outsider = _token_for(client, "outsider")
    for body in (
        {"principal": "viewer", "role": "VIEWER", "kind": "ENVIRONMENT", "environment": "dev"},
        {
            "principal": "outsider",
            "action": "READ_ENVIRONMENT",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    ):
        created = client.post("/api/v1/grants", headers=AUTH_HEADER, json=body)
        assert created.status_code == 201, created.text

    def task_of(headers: dict[str, str]) -> dict[str, Any]:
        shown = client.get(f"{_RUNS}/{started['id']}", headers=headers)
        assert shown.status_code == 200, shown.text
        (task_body,) = shown.json()["tasks"]
        return task_body

    neutral = "This run changed the table's columns; an administrator has been told what to check."
    store([broken])
    with_policy = {"viewer": task_of(viewer), "outsider": task_of(outsider)}
    admin = task_of(AUTH_HEADER)
    store([])
    without = {"viewer": task_of(viewer), "outsider": task_of(outsider)}

    assert with_policy == without
    for task_body in with_policy.values():
        assert (task_body["schema_note"], task_body["policy_warnings"]) == (neutral, [])
    assert admin["schema_note"] == neutral
    (warning,) = admin["policy_warnings"]
    assert (warning["policy_id"], warning["column"], warning["kind"]) == (
        policy_id,
        "region",
        "row_filter",
    )
