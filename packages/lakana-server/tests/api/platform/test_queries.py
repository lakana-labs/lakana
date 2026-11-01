"""Tests for the query submission API."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lakana_core.models import WriteMode
from tests.conftest import AUTH_HEADER


def test_submit_then_get_returns_queued(client: TestClient) -> None:
    """A submitted query can be read back before the worker ever runs it."""
    client.post(
        "/api/v1/environments",
        json={"name": "dev", "storage": {"scheme": "file", "root": "file:///tmp/x", "options": {}}},
        headers=AUTH_HEADER,
    )

    submitted = client.post(
        "/api/v1/environments/dev/queries",
        json={"sql": "SELECT 1"},
        headers=AUTH_HEADER,
    )
    assert submitted.status_code == 202
    query_id = submitted.json()["query_id"]

    fetched = client.get(f"/api/v1/environments/dev/queries/{query_id}", headers=AUTH_HEADER)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "QUEUED"
    assert fetched.json()["error_message"] is None
    assert fetched.json()["error_cause"] is None
    assert fetched.json()["lineage"] is None


def test_result_is_409_before_the_query_finishes(client: TestClient) -> None:
    """The result endpoint refuses to serve a result that does not exist yet."""
    client.post(
        "/api/v1/environments",
        json={
            "name": "dev2",
            "storage": {"scheme": "file", "root": "file:///tmp/y", "options": {}},
        },
        headers=AUTH_HEADER,
    )
    submitted = client.post(
        "/api/v1/environments/dev2/queries",
        json={"sql": "SELECT 1"},
        headers=AUTH_HEADER,
    )
    query_id = submitted.json()["query_id"]

    result = client.get(
        f"/api/v1/environments/dev2/queries/{query_id}/result",
        headers=AUTH_HEADER,
    )
    assert result.status_code == 409


def test_submit_to_unknown_environment_is_404(client: TestClient) -> None:
    """Submitting to a nonexistent environment is a 404, not a 500."""
    response = client.post(
        "/api/v1/environments/nope/queries",
        json={"sql": "SELECT 1"},
        headers=AUTH_HEADER,
    )
    assert response.status_code == 404


def test_get_status_of_unknown_query_is_404(client: TestClient) -> None:
    """Fetching an unknown query id is a 404, not a 500."""
    client.post(
        "/api/v1/environments",
        json={
            "name": "dev3",
            "storage": {"scheme": "file", "root": "file:///tmp/z", "options": {}},
        },
        headers=AUTH_HEADER,
    )
    response = client.get(
        "/api/v1/environments/dev3/queries/00000000-0000-0000-0000-000000000000",
        headers=AUTH_HEADER,
    )
    assert response.status_code == 404


def test_get_result_of_succeeded_query_returns_arrow_bytes(client: TestClient) -> None:
    """A finished query's result is served back as raw Arrow IPC bytes.

    Bypasses the worker (the worker owns running queries) by driving `QueryRun`
    straight to `SUCCEEDED` through the same session-backed engine the
    `client` fixture's app uses, then writing the exact bytes the endpoint
    is expected to read back unmodified through `Storage.read`.
    """
    import uuid

    from sqlalchemy.orm import Session

    from lakana_core.models import QueryRun, QueryRunStatus
    from lakana_core.storage.resolver import StorageResolver

    client.post(
        "/api/v1/environments",
        json={
            "name": "dev4",
            "storage": {"scheme": "file", "root": "file:///tmp/w", "options": {}},
        },
        headers=AUTH_HEADER,
    )
    submitted = client.post(
        "/api/v1/environments/dev4/queries",
        json={"sql": "SELECT 1"},
        headers=AUTH_HEADER,
    )
    query_id = submitted.json()["query_id"]

    engine = client.app.state.engine
    result_path = f"_artifacts/queries/{query_id}/result.arrow"
    with Session(engine) as session:
        resolved = StorageResolver(session).for_environment("dev4")
        resolved.storage.write(result_path, b"arrow-bytes")
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        run.status = QueryRunStatus.SUCCEEDED
        run.result_location = result_path
        run.lineage_json = {}
        session.commit()

    result = client.get(
        f"/api/v1/environments/dev4/queries/{query_id}/result",
        headers=AUTH_HEADER,
    )
    assert result.status_code == 200
    assert result.content == b"arrow-bytes"
    assert result.headers["content-type"] == "application/vnd.apache.arrow.file"


def test_get_status_of_succeeded_query_includes_lineage(client: TestClient) -> None:
    """Once SUCCEEDED, the status response carries the recorded lineage."""
    import uuid

    from sqlalchemy.orm import Session

    from lakana_core.models import QueryRun, QueryRunStatus

    client.post(
        "/api/v1/environments",
        json={
            "name": "dev5",
            "storage": {"scheme": "file", "root": "file:///tmp/v", "options": {}},
        },
        headers=AUTH_HEADER,
    )
    submitted = client.post(
        "/api/v1/environments/dev5/queries",
        json={"sql": "SELECT 1"},
        headers=AUTH_HEADER,
    )
    query_id = submitted.json()["query_id"]

    engine = client.app.state.engine
    with Session(engine) as session:
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        run.status = QueryRunStatus.SUCCEEDED
        run.lineage_json = {"out": [{"source": "a.b"}]}
        session.commit()

    fetched = client.get(f"/api/v1/environments/dev5/queries/{query_id}", headers=AUTH_HEADER)
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["status"] == "SUCCEEDED"
    assert body["lineage"] == {"out": [{"source": "a.b"}]}


def test_get_status_of_failed_query_includes_error_message(client: TestClient) -> None:
    """Once FAILED, the status response carries the recorded error message."""
    import uuid

    from sqlalchemy.orm import Session

    from lakana_core.models import QueryRun, QueryRunStatus

    client.post(
        "/api/v1/environments",
        json={
            "name": "dev6",
            "storage": {"scheme": "file", "root": "file:///tmp/u", "options": {}},
        },
        headers=AUTH_HEADER,
    )
    submitted = client.post(
        "/api/v1/environments/dev6/queries",
        json={"sql": "SELECT 1"},
        headers=AUTH_HEADER,
    )
    query_id = submitted.json()["query_id"]

    engine = client.app.state.engine
    with Session(engine) as session:
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        run.status = QueryRunStatus.FAILED
        run.error_message = "boom"
        run.error_cause = "not_found"
        session.commit()

    fetched = client.get(f"/api/v1/environments/dev6/queries/{query_id}", headers=AUTH_HEADER)
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["status"] == "FAILED"
    assert body["error_message"] == "boom"
    assert body["error_cause"] == "not_found"
    assert body["lineage"] is None


def test_another_principals_query_is_404_on_status_and_result(
    client: TestClient, tmp_path: Path
) -> None:
    """Bob, with SUBMIT_QUERY in the environment, cannot read the admin's run.

    Both read paths go through `QueryService.get_query_run`. The run is
    driven to SUCCEEDED with a real result file, so a 404 on `/result`
    cannot be a 409 "not finished" in disguise, and the admin's own 200
    proves the file is really there.

    Parameters
    ----------
    client
        Test client for the API.
    tmp_path
        This test's own temp directory, used as the environment's storage
        root so the run's result file is written and cleaned up there
        instead of at a fixed, shared path outside pytest's control.
    """
    import uuid

    from sqlalchemy.orm import Session

    from lakana_core.models import QueryRun, QueryRunStatus
    from lakana_core.storage.resolver import StorageResolver

    client.post(
        "/api/v1/environments",
        json={"name": "dev64", "storage": {"scheme": "file", "root": f"file://{tmp_path}"}},
        headers=AUTH_HEADER,
    )
    query_id = client.post(
        "/api/v1/environments/dev64/queries", json={"sql": "SELECT 1"}, headers=AUTH_HEADER
    ).json()["query_id"]
    result_path = f"_artifacts/queries/{query_id}/result.arrow"
    with Session(client.app.state.engine) as session:
        StorageResolver(session).for_environment("dev64").storage.write(result_path, b"secret")
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        run.status = QueryRunStatus.SUCCEEDED
        run.result_location = result_path
        run.lineage_json = {}
        session.commit()

    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "SERVICE_ACCOUNT"}
    )
    token = client.post(
        "/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "bob"}
    ).json()["token"]
    bob = {"Authorization": f"Bearer {token}"}
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "bob", "role": "ANALYST", "kind": "ENVIRONMENT", "environment": "dev64"},
    )
    assert granted.status_code == 201, granted.text

    base = f"/api/v1/environments/dev64/queries/{query_id}"
    assert client.get(base, headers=bob).status_code == 404
    assert client.get(f"{base}/result", headers=bob).status_code == 404
    assert client.get(base, headers=AUTH_HEADER).status_code == 200
    assert client.get(f"{base}/result", headers=AUTH_HEADER).content == b"secret"


# --- Cancel ---------------------------------------------------------------------------


def _environment(client: TestClient, name: str = "dev") -> None:
    """Create an environment with a file store.

    Parameters
    ----------
    client
        Test client for the API.
    name
        The environment's name.
    """
    created = client.post(
        "/api/v1/environments",
        json={"name": name, "storage": {"scheme": "file", "root": "file:///tmp/cancel"}},
        headers=AUTH_HEADER,
    )
    assert created.status_code == 201, created.text


def _person(client: TestClient, name: str, role: str, env: str = "dev") -> dict[str, str]:
    """Create a service account with a role on an environment, and return its header.

    Parameters
    ----------
    client
        Test client for the API.
    name
        The principal's name.
    role
        The role granted on the environment.
    env
        The environment.

    Returns
    -------
    dict of str to str
        The principal's Authorization header.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "SERVICE_ACCOUNT"}
    )
    token = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    ).json()["token"]
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": name, "role": role, "kind": "ENVIRONMENT", "environment": env},
    )
    assert granted.status_code == 201, granted.text
    return {"Authorization": f"Bearer {token}"}


def _submit(client: TestClient, headers: dict[str, str] = AUTH_HEADER, env: str = "dev") -> str:
    """Submit a query and return its id.

    Parameters
    ----------
    client
        Test client for the API.
    headers
        The submitter's Authorization header.
    env
        The environment.

    Returns
    -------
    str
        The query's id.
    """
    response = client.post(
        f"/api/v1/environments/{env}/queries", json={"sql": "SELECT 1"}, headers=headers
    )
    assert response.status_code == 202, response.text
    return response.json()["query_id"]


def _row(client: TestClient, query_id: str) -> dict[str, object]:
    """Read the columns of a query run that the cancel tests look at.

    Parameters
    ----------
    client
        Test client for the API.
    query_id
        The query's id.

    Returns
    -------
    dict of str to object
        `status`, `cancel_requested`, `error_message`, `error_cause`.
    """
    import uuid

    from sqlalchemy.orm import Session

    from lakana_core.models import QueryRun

    with Session(client.app.state.engine) as session:
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        return {
            "status": run.status.value,
            "cancel_requested": run.cancel_requested,
            "error_message": run.error_message,
            "error_cause": run.error_cause,
        }


def _put_status(client: TestClient, query_id: str, status: str) -> None:
    """Set a query run's status directly, as the worker would.

    Parameters
    ----------
    client
        Test client for the API.
    query_id
        The query's id.
    status
        A `QueryRunStatus` value.
    """
    import uuid
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from lakana_core.models import QueryRun, QueryRunStatus

    with Session(client.app.state.engine) as session:
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        run.status = QueryRunStatus(status)
        if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
            run.finished_at = datetime.now(UTC)  # every real writer of a final status sets it
        session.commit()


def _cancel_events(client: TestClient) -> list[tuple[object, dict[str, object]]]:
    """List the `QUERY_CANCELLED` audit events as (principal id, detail).

    Parameters
    ----------
    client
        Test client for the API.

    Returns
    -------
    list of tuple
        One entry per event, oldest first.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from lakana_core.models import AuditEvent, AuditEventType

    with Session(client.app.state.engine) as session:
        rows = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.QUERY_CANCELLED)
            .order_by(AuditEvent.occurred_at)
        ).all()
        return [(row.principal_id, dict(row.detail)) for row in rows]


def _cancel_url(query_id: str, env: str = "dev") -> str:
    """Return the cancel route's path.

    Parameters
    ----------
    query_id
        The query's id.
    env
        The environment.

    Returns
    -------
    str
        The path.
    """
    return f"/api/v1/environments/{env}/queries/{query_id}/cancel"


def test_cancel_a_queued_query_answers_cancelled(client: TestClient) -> None:
    """A queued query is cancelled at once, and a read says the same."""
    _environment(client)
    query_id = _submit(client)

    cancelled = client.post(_cancel_url(query_id), headers=AUTH_HEADER)

    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "CANCELLED"
    assert body["error_cause"] == "cancelled"
    assert body["error_message"] == "The query was cancelled."
    read = client.get(f"/api/v1/environments/dev/queries/{query_id}", headers=AUTH_HEADER)
    assert read.json() == body


def test_cancel_a_running_query_answers_running_and_sets_the_flag(client: TestClient) -> None:
    """A running query is only asked to stop: the worker decides its final status."""
    _environment(client)
    query_id = _submit(client)
    _put_status(client, query_id, "RUNNING")

    cancelled = client.post(_cancel_url(query_id), headers=AUTH_HEADER)

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "RUNNING"
    assert _row(client, query_id)["cancel_requested"] is True


def test_cancel_a_query_that_has_ended_answers_its_status(client: TestClient) -> None:
    """A cancel of a finished query changes nothing and is no error."""
    _environment(client)
    for status in ("SUCCEEDED", "FAILED"):
        query_id = _submit(client)
        _put_status(client, query_id, status)
        before = _row(client, query_id)

        cancelled = client.post(_cancel_url(query_id), headers=AUTH_HEADER)

        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == status
        assert _row(client, query_id) == before
    assert _cancel_events(client) == []


def test_cancel_by_another_principal_is_the_404_of_a_status_read(client: TestClient) -> None:
    """Bob learns nothing from a cancel that a status read would not tell him."""
    _environment(client)
    bob = _person(client, "bob", "ANALYST")
    query_id = _submit(client)
    before = _row(client, query_id)
    unknown = "00000000-0000-0000-0000-000000000000"

    cancel = client.post(_cancel_url(query_id), headers=bob)
    read = client.get(f"/api/v1/environments/dev/queries/{query_id}", headers=bob)
    cancel_unknown = client.post(_cancel_url(unknown), headers=bob)

    assert cancel.status_code == read.status_code == 404
    assert cancel.json() == read.json()
    assert cancel_unknown.status_code == 404
    assert cancel_unknown.json()["detail"].replace(unknown, "ID") == cancel.json()[
        "detail"
    ].replace(query_id, "ID")
    assert _row(client, query_id) == before
    assert _cancel_events(client) == []


def test_a_holder_of_manage_environment_cancels_anyones_query(client: TestClient) -> None:
    """An environment admin cancels bob's query; the audit event names the admin."""
    _environment(client)
    bob = _person(client, "bob", "ANALYST")
    carol = _person(client, "carol", "ADMIN")
    query_id = _submit(client, bob)

    cancelled = client.post(_cancel_url(query_id), headers=carol)

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from lakana_core.models import Principal

    with Session(client.app.state.engine) as session:
        principals = {p.name: str(p.id) for p in session.scalars(select(Principal))}
    (event,) = _cancel_events(client)
    assert str(event[0]) == principals["carol"]
    assert event[1]["query_id"] == query_id
    assert event[1]["status"] == "QUEUED"
    assert event[1]["submitted_by"] == principals["bob"]


def test_cancel_needs_a_token(client: TestClient) -> None:
    """Without credentials the route answers 401."""
    _environment(client)
    query_id = _submit(client)

    assert client.post(_cancel_url(query_id)).status_code == 401
    assert _row(client, query_id)["status"] == "QUEUED"


def test_cancel_of_a_pipeline_tasks_write_is_a_422_that_names_the_run(client: TestClient) -> None:
    """The write of a pipeline task is cancelled through its run, and the answer says so."""
    import uuid

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from lakana_core.models import (
        Environment,
        JobVersion,
        QueryRun,
        Repository,
        RepositoryKind,
        Run,
        RunKind,
        RunStatus,
        TaskRun,
        TaskStatus,
    )

    _environment(client)
    query_id = _submit(client)
    with Session(client.app.state.engine) as session:
        env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
        query = session.get(QueryRun, uuid.UUID(query_id))
        assert query is not None
        repo = Repository(
            id=uuid.uuid4(), name="repo-cancel", kind=RepositoryKind.HTTPS, url="https://h/a.git"
        )
        session.add(repo)
        session.flush()
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json={"version": 3, "assets": [], "edges": []},
            code_uri="file:///x",
            code_sha256="0" * 64,
        )
        session.add(version)
        session.flush()
        run = Run(
            id=uuid.uuid4(),
            environment_id=env.id,
            repository_id=repo.id,
            job_version_id=version.id,
            run_as_principal_id=query.principal_id,
            kind=RunKind.FULL,
            status=RunStatus.RUNNING,
            selection_json={},
        )
        session.add(run)
        session.flush()
        task = TaskRun(
            id=uuid.uuid4(),
            run_id=run.id,
            asset="gold.t",
            status=TaskStatus.RUNNING,
            claim_count=1,
            attempt=1,
            lease_owner="w1",
            max_attempts=3,
            timeout_seconds=60,
            retry_delay_seconds=0,
            upstream_json=[],
        )
        session.add(task)
        session.flush()
        query.task_run_id = task.id
        session.commit()
        pipeline_run = run.id

    refused = client.post(_cancel_url(query_id), headers=AUTH_HEADER)

    assert refused.status_code == 422
    assert f"lakana runs cancel {pipeline_run} --env dev" in refused.text
    assert _row(client, query_id)["status"] == "QUEUED"


def test_cancel_with_an_id_that_is_not_a_uuid_is_a_422(client: TestClient) -> None:
    """The path parameter is a UUID."""
    _environment(client)

    assert client.post(_cancel_url("not-a-uuid"), headers=AUTH_HEADER).status_code == 422


def test_cancel_by_a_caller_who_may_not_read_queries_is_the_403_of_a_status_read(
    client: TestClient,
) -> None:
    """A principal with no right on the environment gets what a status read gives, a 403."""
    _environment(client)
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "eve", "type": "SERVICE_ACCOUNT"}
    )
    token = client.post(
        "/api/v1/principals/eve/tokens", headers=AUTH_HEADER, json={"name": "eve"}
    ).json()["token"]
    eve = {"Authorization": f"Bearer {token}"}
    query_id = _submit(client)
    before = _row(client, query_id)

    cancel = client.post(_cancel_url(query_id), headers=eve)
    read = client.get(f"/api/v1/environments/dev/queries/{query_id}", headers=eve)

    assert cancel.status_code == read.status_code == 403
    assert cancel.json() == read.json()
    assert _row(client, query_id) == before


def test_a_task_token_does_not_take_the_managers_path(client: TestClient) -> None:
    """A task token of an administrator cancels its own queries, and gets a 404 for the rest."""
    _environment(client)
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.*"],
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text
    token = {"Authorization": f"Bearer {minted.json()['token']}"}
    pat_query = _submit(client)
    own_query = _submit(client, token)
    before = _row(client, pat_query)

    refused = client.post(_cancel_url(pat_query), headers=token)
    read = client.get(f"/api/v1/environments/dev/queries/{pat_query}", headers=token)
    own = client.post(_cancel_url(own_query), headers=token)

    assert refused.status_code == read.status_code == 404
    assert refused.json() == read.json()
    assert _row(client, pat_query) == before
    assert own.status_code == 200, own.text
    assert own.json()["status"] == "CANCELLED"


def test_the_status_of_a_queued_query_has_waiting_for_and_a_finished_one_has_null(
    client: TestClient,
) -> None:
    """A queued query says why it waits; any other status says nothing, and so does a cancel."""
    _environment(client)
    query_id = _submit(client)
    url = f"/api/v1/environments/dev/queries/{query_id}"

    queued = client.get(url, headers=AUTH_HEADER).json()["waiting_for"]
    assert queued == {
        "code": "no_worker",
        "message": (
            "No worker is running, so this query cannot start. Ask the operator to start a "
            "worker; on a local instance, start it again with 'lakana server start --local'."
        ),
        "workers": 0,
    }
    for status in ("RUNNING", "SUCCEEDED", "FAILED"):
        _put_status(client, query_id, status)
        body = client.get(url, headers=AUTH_HEADER).json()
        assert body["waiting_for"] is None
    other = _submit(client)
    cancelled = client.post(_cancel_url(other), headers=AUTH_HEADER).json()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["waiting_for"] is None


def _alive_worker(client: TestClient, worker_id: str = "w", takes: str = "all") -> None:
    """Register a worker whose heartbeat is current.

    Parameters
    ----------
    client
        Test client for the API.
    worker_id
        The worker's id.
    takes
        What it takes (`all`, `interactive` or `pipeline`).
    """
    from sqlalchemy.orm import Session

    from lakana_core.db import db_now
    from lakana_core.models import Worker

    with Session(client.app.state.engine) as session:
        now = db_now(session)
        session.add(
            Worker(
                id=worker_id,
                host="h",
                version="1",
                started_at=now,
                heartbeat_at=now,
                takes=takes,
            )
        )
        session.commit()


_OWN_AT_1 = (
    "You have 1 query running, which is the limit per user here; this one can start when it "
    "ends. 'lakana query list' shows it."
)


def test_the_status_of_a_capped_query_has_user_limit(client: TestClient) -> None:
    """One worker takes interactive work, so `auto` allows one running query per user."""
    _environment(client)
    _alive_worker(client)
    running = _submit(client)
    _put_status(client, running, "RUNNING")
    queued = _submit(client)
    body = client.get(f"/api/v1/environments/dev/queries/{queued}", headers=AUTH_HEADER).json()
    assert body["waiting_for"] == {
        "code": "user_limit",
        "message": _OWN_AT_1,
        "running": 1,
        "limit": 1,
    }


def test_the_status_of_a_query_on_pipeline_workers_only_has_no_worker_of_kind(
    client: TestClient,
) -> None:
    _environment(client)
    _alive_worker(client, takes="pipeline")
    queued = _submit(client)
    body = client.get(f"/api/v1/environments/dev/queries/{queued}", headers=AUTH_HEADER).json()
    assert body["waiting_for"]["code"] == "no_worker_of_kind"
    assert body["waiting_for"]["workers"] == 0
    assert body["waiting_for"]["other_workers"] == 1
    assert body["waiting_for"]["message"].startswith(
        "No worker that takes interactive work is running (1 worker takes pipeline work only)"
    )


def test_a_stranger_gets_the_same_404_for_a_capped_query(client: TestClient) -> None:
    """Whoever may not read the run learns neither that it exists nor that its owner is capped."""
    import uuid

    _environment(client)
    _alive_worker(client)
    bob = _person(client, "bob", "ANALYST")
    carol = _person(client, "carol", "ANALYST")
    running = _submit(client, bob)
    _put_status(client, running, "RUNNING")
    capped = _submit(client, bob)
    missing = str(uuid.uuid4())
    refused = client.get(f"/api/v1/environments/dev/queries/{capped}", headers=carol)
    absent = client.get(f"/api/v1/environments/dev/queries/{missing}", headers=carol)
    assert refused.status_code == absent.status_code == 404
    assert refused.text.replace(capped, "ID") == absent.text.replace(missing, "ID")
    assert "user_limit" not in refused.text


def test_a_managers_cancel_of_another_principals_capped_query_answers_no_reason(
    client: TestClient,
) -> None:
    """The cancel answers with the run `CANCELLED`, which has no reason: no count leaks."""
    _environment(client)
    _alive_worker(client)
    bob = _person(client, "bob", "ANALYST")
    _put_status(client, _submit(client, bob), "RUNNING")
    capped = _submit(client, bob)
    before = client.get(f"/api/v1/environments/dev/queries/{capped}", headers=bob).json()
    assert before["waiting_for"]["code"] == "user_limit"
    answer = client.post(_cancel_url(capped), headers=AUTH_HEADER)
    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] == "CANCELLED"
    assert answer.json()["waiting_for"] is None


def test_a_managers_listing_never_shows_how_many_another_principal_runs(
    client: TestClient,
) -> None:
    _environment(client)
    _alive_worker(client)
    bob = _person(client, "bob", "ANALYST")
    carol = _person(client, "carol", "ADMIN")
    for _ in range(3):  # three running, a limit of 1: above it, so the count is not the limit
        _put_status(client, _submit(client, bob), "RUNNING")
    queued = _submit(client, bob)
    manager_row = next(
        r for r in client.get(_list_url(), headers=carol).json() if r["id"] == queued
    )
    assert manager_row["waiting_for"] == {
        "code": "user_limit",
        "message": (
            "Its submitter has reached the limit of 1 running query per user; it can start "
            "when none of theirs is running."
        ),
        "limit": 1,
    }
    assert "running" not in manager_row["waiting_for"]
    assert "3" not in str(manager_row["waiting_for"])
    own_row = next(r for r in client.get(_list_url(), headers=bob).json() if r["id"] == queued)
    assert (own_row["waiting_for"]["running"], own_row["waiting_for"]["limit"]) == (3, 1)


def test_a_queued_query_of_a_stranger_is_still_not_found_and_says_nothing(
    client: TestClient,
) -> None:
    """The reason travels inside the status read: whoever may not read the run gets a 404."""
    _environment(client)
    query_id = _submit(client)
    reader = _person(client, "reader", "ANALYST")
    response = client.get(f"/api/v1/environments/dev/queries/{query_id}", headers=reader)
    assert response.status_code == 404
    assert "waiting_for" not in response.text


# -- the list --------------------------------------------------

_NEEDLE = "needle_column_8c3f"


def _list_url(env: str = "dev") -> str:
    """Return the list route's path.

    Parameters
    ----------
    env
        The environment.

    Returns
    -------
    str
        The path.
    """
    return f"/api/v1/environments/{env}/queries"


def _orphan(client: TestClient, query_id: str) -> None:
    """Delete the submitter of a query, as deleting the principal would (`principal_id` NULL).

    Parameters
    ----------
    client
        Test client for the API.
    query_id
        The query's id.
    """
    import uuid

    from sqlalchemy.orm import Session

    from lakana_core.models import QueryRun

    with Session(client.app.state.engine) as session:
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        run.principal_id = None
        session.commit()


def test_the_list_never_contains_sql(client: TestClient) -> None:
    """No key for the SQL, an error message or lineage, and the SQL text is nowhere in the body."""
    _environment(client)
    submitted = client.post(
        _list_url(), json={"sql": f"SELECT 1 AS {_NEEDLE}"}, headers=AUTH_HEADER
    ).json()["query_id"]
    failed = _submit(client)
    _put_status(client, failed, "FAILED")
    import uuid

    from sqlalchemy.orm import Session

    from lakana_core.models import QueryRun

    with Session(client.app.state.engine) as session:
        run = session.get(QueryRun, uuid.UUID(failed))
        assert run is not None
        run.error_message = f"boom {_NEEDLE}"
        run.error_cause = "invalid_query"
        run.lineage_json = {"c": [{"table": _NEEDLE, "column": "c"}]}
        run.result_location = f"results/{_NEEDLE}"
        session.commit()
    listed = client.get(_list_url(), params={"include_ended": "true"}, headers=AUTH_HEADER)
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert {row["id"] for row in rows} == {submitted, failed}
    for row in rows:
        assert set(row) == {
            "id",
            "submitter",
            "kind",
            "target",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "error_cause",
            "waiting_for",
        }
    assert _NEEDLE not in listed.text
    assert "worker_id" not in listed.text


def test_the_list_shows_a_manager_another_users_row_and_the_status_read_still_refuses_it(
    client: TestClient,
) -> None:
    """A manager sees bob's row in the list, without SQL, and gets a 404 on bob's status."""
    _environment(client)
    bob = _person(client, "bob", "ANALYST")
    carol = _person(client, "carol", "ADMIN")
    dave = _person(client, "dave", "ANALYST")
    _environment(client, "other")
    query_id = _submit(client, bob)
    orphan = _submit(client, bob)
    foreign = _submit(client, env="other")
    _orphan(client, orphan)
    manager_rows = client.get(_list_url(), headers=carol).json()
    assert [(row["id"], row["submitter"], row["kind"]) for row in manager_rows] == [
        (query_id, "bob", "read"),
        (orphan, None, "read"),
    ]
    assert foreign not in {row["id"] for row in manager_rows}
    assert client.get(f"{_list_url()}/{query_id}", headers=carol).status_code == 404
    assert client.get(_list_url(), headers=dave).json() == []
    assert [row["id"] for row in client.get(_list_url(), headers=bob).json()] == [query_id]
    assert [row["id"] for row in client.get(_list_url("other"), headers=AUTH_HEADER).json()] == [
        foreign
    ]


def test_the_list_of_another_environments_manager_is_the_status_reads_403(
    client: TestClient,
) -> None:
    """Admin of `other` has no right on `dev`: the list answers what the status read answers."""
    _environment(client)
    _environment(client, "other")
    query_id = _submit(client)
    outsider = _person(client, "outsider", "ADMIN", env="other")
    listed = client.get(_list_url(), headers=outsider)
    read = client.get(f"{_list_url()}/{query_id}", headers=outsider)
    assert listed.status_code == read.status_code == 403
    assert listed.json() == read.json()
    assert client.get(_list_url("nowhere"), headers=outsider).status_code == 403


def test_the_list_of_an_unknown_environment_is_404_for_the_instance_administrator(
    client: TestClient,
) -> None:
    assert client.get(_list_url("nowhere"), headers=AUTH_HEADER).status_code == 404


def test_the_list_needs_a_token(client: TestClient) -> None:
    _environment(client)
    assert client.get(_list_url()).status_code == 401


def test_a_task_token_lists_only_its_own_queries(client: TestClient) -> None:
    _environment(client)
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.*"],
            "ttl_seconds": 300,
        },
    )
    token = {"Authorization": f"Bearer {minted.json()['token']}"}
    _submit(client)
    own = _submit(client, token)
    assert [row["id"] for row in client.get(_list_url(), headers=token).json()] == [own]
    assert len(client.get(_list_url(), headers=AUTH_HEADER).json()) == 2


def test_limit_is_bounded(client: TestClient) -> None:
    _environment(client)
    for bad in ("0", "-1", "201", "x"):
        assert (
            client.get(_list_url(), params={"limit": bad}, headers=AUTH_HEADER).status_code == 422
        )
    for good in ("1", "200"):
        assert (
            client.get(_list_url(), params={"limit": good}, headers=AUTH_HEADER).status_code == 200
        )
    for _ in range(3):
        _submit(client)
    assert len(client.get(_list_url(), params={"limit": 2}, headers=AUTH_HEADER).json()) == 2


def test_a_queued_row_carries_its_reason_and_the_default_leaves_out_ended_runs(
    client: TestClient,
) -> None:
    _environment(client)
    done = _submit(client)
    queued = _submit(client)
    _put_status(client, done, "SUCCEEDED")
    rows = client.get(_list_url(), headers=AUTH_HEADER).json()
    assert [row["id"] for row in rows] == [queued]
    assert rows[0]["waiting_for"]["code"] == "no_worker"
    assert rows[0]["status"] == "QUEUED"
    with_ended = client.get(_list_url(), params={"include_ended": "true"}, headers=AUTH_HEADER)
    assert {row["id"]: row["waiting_for"] for row in with_ended.json()}[done] is None


_NO_RESULT_CLAUSE = {
    None: "has no stored result",
    WriteMode.CREATE: "wrote to a table",
    WriteMode.REPLACE: "wrote to a table",
    WriteMode.APPEND: "wrote to a table",
    WriteMode.MERGE: "wrote to a table",
    WriteMode.HISTORY: "wrote to a table",
    WriteMode.VIEW: "created a view",
    WriteMode.MAINTAIN: "was a maintenance run",
}


@pytest.mark.parametrize("mode", [None, *WriteMode], ids=lambda mode: str(mode))
def test_result_of_a_succeeded_run_without_rows_is_409_whose_clause_fits_its_mode(
    client: TestClient, mode: WriteMode | None
) -> None:
    """Every write mode has a true clause, and the 409 names the status command."""
    import uuid

    from sqlalchemy.orm import Session

    from lakana_core.models import QueryRun, QueryRunStatus

    assert set(_NO_RESULT_CLAUSE) == {None, *WriteMode}, "a new write mode needs a clause here"
    name = f"dev375{mode.value if mode else 'none'}"
    client.post(
        "/api/v1/environments",
        json={
            "name": name,
            "storage": {"scheme": "file", "root": f"file:///tmp/w{name}", "options": {}},
        },
        headers=AUTH_HEADER,
    )
    submitted = client.post(
        f"/api/v1/environments/{name}/queries",
        json={"sql": "SELECT 1"},
        headers=AUTH_HEADER,
    )
    query_id = submitted.json()["query_id"]
    with Session(client.app.state.engine) as session:
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        run.status = QueryRunStatus.SUCCEEDED
        run.write_mode = mode
        session.commit()

    result = client.get(
        f"/api/v1/environments/{name}/queries/{query_id}/result",
        headers=AUTH_HEADER,
    )
    assert result.status_code == 409
    assert result.json()["detail"] == (
        f"Query {query_id} {_NO_RESULT_CLAUSE[mode]}, so it has no result rows to download. "
        f"See what it did with: lakana query status {query_id} --env {name}"
    )


def test_a_conflict_error_from_cancel_query_is_answered_409_with_its_message(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `ConflictError` raised by `cancel_query` (stubbed here) is mapped to 409 with its text.

    This tests the route's error mapping only. That the service raises it when the row stays
    locked is tested in lakana-core.
    """
    from lakana_core.catalog.query_service import QueryService
    from lakana_core.errors import ConflictError

    message = "Query q is being started or is finishing right now, so it cannot be cancelled."

    def refuse(self: QueryService, *args: object, **kwargs: object) -> None:
        raise ConflictError(message)

    monkeypatch.setattr(QueryService, "cancel_query", refuse)
    client.post(
        "/api/v1/environments",
        json={
            "name": "dev374",
            "storage": {"scheme": "file", "root": "file:///tmp/w374", "options": {}},
        },
        headers=AUTH_HEADER,
    )
    submitted = client.post(
        "/api/v1/environments/dev374/queries", json={"sql": "SELECT 1"}, headers=AUTH_HEADER
    )
    query_id = submitted.json()["query_id"]

    response = client.post(
        f"/api/v1/environments/dev374/queries/{query_id}/cancel", headers=AUTH_HEADER
    )

    assert response.status_code == 409
    assert response.json()["detail"] == message
