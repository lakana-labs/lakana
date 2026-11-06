"""`GET .../jobs/{job}/tasks/{task}/attempts`: one task's attempts across runs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.models import AttemptOutcome, Run, TaskAttempt, TaskRun
from tests.api.platform.test_deployments_run_as import _deploy
from tests.api.platform.test_deploys import _token_for
from tests.conftest import AUTH_HEADER

_RUNS = "/api/v1/environments/dev/runs"
_URL = "/api/v1/environments/dev/jobs/nightly/tasks/silver.clean/attempts"
_T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _attempts(client: TestClient, count: int) -> str:
    """Start a run of job ``nightly`` and give its task `count` attempts.

    Parameters
    ----------
    client
        The test client.
    count
        How many attempts to add.

    Returns
    -------
    str
        The run's id.
    """
    _deploy(client)
    started = client.post(_RUNS, headers=AUTH_HEADER, json={"repository": "analytics"}).json()
    with Session(client.app.state.engine) as session:
        run = session.get(Run, uuid.UUID(started["id"]))
        assert run is not None
        run.job = "nightly"
        task = session.query(TaskRun).filter(TaskRun.run_id == run.id).one()
        for number in range(1, count + 1):
            session.add(
                TaskAttempt(
                    id=uuid.uuid4(),
                    task_run_id=task.id,
                    attempt=number,
                    worker="w",
                    started_at=_T0 + timedelta(minutes=number),
                    finished_at=_T0 + timedelta(minutes=number, seconds=12),
                    outcome=AttemptOutcome.SUCCEEDED if number % 2 else AttemptOutcome.FAILED,
                    failure_code=None if number % 2 else "out_of_memory",
                    peak_memory_bytes=number * 1000 if number > 1 else None,
                )
            )
        session.commit()
    return started["id"]


def test_the_history_is_a_page_of_attempts_with_a_cursor(client: TestClient) -> None:
    """Newest first, the figures and failure code in each entry, `next_cursor` to go on."""
    run = _attempts(client, 3)
    page = client.get(_URL, headers=AUTH_HEADER, params={"limit": 2})
    assert page.status_code == 200, page.text
    body = page.json()
    first, second = body["attempts"]
    assert (first["run_id"], first["attempt"], first["status"]) == (run, 3, "succeeded")
    assert (first["duration_seconds"], first["peak_memory_bytes"]) == (12.0, 3000)
    assert (second["attempt"], second["status"], second["failure_code"]) == (
        2,
        "failed",
        "out_of_memory",
    )
    rest = client.get(_URL, headers=AUTH_HEADER, params={"cursor": body["next_cursor"]}).json()
    assert [a["attempt"] for a in rest["attempts"]] == [1]
    assert (rest["attempts"][0]["peak_memory_bytes"], rest["next_cursor"]) == (None, None)


def test_a_limit_outside_1_to_200_is_422(client: TestClient) -> None:
    """The message says the range and names no command-line flag."""
    _attempts(client, 1)
    for limit in (0, 201):
        answer = client.get(_URL, headers=AUTH_HEADER, params={"limit": limit})
        assert answer.status_code == 422, answer.text
        assert "between 1 and 200" in answer.text
        assert "--" not in answer.text


def test_unknown_job_or_task_is_404_and_names_the_next_step(client: TestClient) -> None:
    """Both names, and `lakana job list`."""
    _attempts(client, 1)
    answer = client.get(_URL.replace("nightly", "nope"), headers=AUTH_HEADER)
    assert answer.status_code == 404
    assert "'nope'" in answer.text
    assert "lakana job list" in answer.text
    assert client.get(_URL.replace("silver.clean", "ghost"), headers=AUTH_HEADER).status_code == 404


def test_without_the_right_to_read_runs_it_is_403(client: TestClient) -> None:
    """Needs RUN or READ_ENVIRONMENT."""
    _attempts(client, 1)
    assert client.get(_URL, headers=_token_for(client, "outsider")).status_code == 403


def test_the_repository_parameter_narrows_the_history(client: TestClient) -> None:
    """An unknown repository is 404; the right one gives the attempts."""
    _attempts(client, 1)
    ok = client.get(_URL, headers=AUTH_HEADER, params={"repository": "analytics"})
    assert [a["attempt"] for a in ok.json()["attempts"]] == [1]
    assert client.get(_URL, headers=AUTH_HEADER, params={"repository": "x"}).status_code == 404


def test_a_declared_task_that_never_ran_is_an_empty_page(client: TestClient) -> None:
    """The project declares job and task, no run yet: 200 and nothing; a typo stays 404."""
    from lakana_core.models import JobVersion

    _deploy(client)
    with Session(client.app.state.engine) as session:
        for version in session.query(JobVersion):
            version.manifest_json = {**version.manifest_json, "jobs": {"nightly": {}}}
        session.commit()
    answer = client.get(_URL, headers=AUTH_HEADER)
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"attempts": [], "next_cursor": None}
    assert client.get(_URL.replace("silver.clean", "ghost"), headers=AUTH_HEADER).status_code == 404
