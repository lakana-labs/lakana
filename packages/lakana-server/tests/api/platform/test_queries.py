"""Tests for the query submission API."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.errors import LakanaError
from lakana_core.models import QueryOrigin, QueryRun, QueryRunStatus, WriteMode
from lakana_core.storage.resolver import StorageResolver
from lakana_server.api.platform import queries as queries_module
from lakana_server.api.platform.schemas import (
    MaskedColumnOut,
    PreviewOut,
    ResultColumnOut,
    ResultPageOut,
)
from lakana_server.result_json import json_page
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, person, query_run_columns


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
    """Create an environment with a file store under the test's own directory.

    Parameters
    ----------
    client
        Test client for the API.
    name
        The environment's name.
    """
    create_environment(client, name)


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
    bob = person(client, "bob", "ANALYST")
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
    bob = person(client, "bob", "ANALYST")
    carol = person(client, "carol", "ADMIN")
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


def test_a_managers_cancel_of_anothers_run_does_not_return_its_sql(client: TestClient) -> None:
    """The cancel answer carries the SQL to the submitter only, never to a manager."""
    _environment(client)
    bob = person(client, "bob", "ANALYST")
    carol = person(client, "carol", "ADMIN")
    by_manager = _submit(client, bob)
    by_owner = _submit(client, bob)

    managed = client.post(_cancel_url(by_manager), headers=carol).json()
    owned = client.post(_cancel_url(by_owner), headers=bob).json()

    assert managed["origin"] == "written"
    assert managed["sql"] is None
    assert owned["sql"] is not None


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
    bob = person(client, "bob", "ANALYST")
    carol = person(client, "carol", "ANALYST")
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
    bob = person(client, "bob", "ANALYST")
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
    bob = person(client, "bob", "ANALYST")
    carol = person(client, "carol", "ADMIN")
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
    reader = person(client, "reader", "ANALYST")
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
            "environment",
            "origin",
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
    bob = person(client, "bob", "ANALYST")
    carol = person(client, "carol", "ADMIN")
    dave = person(client, "dave", "ANALYST")
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
    outsider = person(client, "outsider", "ADMIN", env="other")
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


def test_the_status_of_a_query_carries_its_resources(client: TestClient) -> None:
    """The three figures of a finished query are in its status; an unmeasured one is `null`."""
    import uuid

    from sqlalchemy.orm import Session

    from lakana_core.models import QueryRun

    client.post(
        "/api/v1/environments",
        json={
            "name": "devr",
            "storage": {"scheme": "file", "root": "file:///tmp/r", "options": {}},
        },
        headers=AUTH_HEADER,
    )
    url = "/api/v1/environments/devr/queries"
    query_id = client.post(url, json={"sql": "SELECT 1"}, headers=AUTH_HEADER).json()["query_id"]
    before = client.get(f"{url}/{query_id}", headers=AUTH_HEADER).json()
    assert (before["peak_memory_bytes"], before["cpu_seconds"], before["bytes_read"]) == (
        None,
        None,
        None,
    )
    with Session(client.app.state.engine) as session:
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        run.peak_memory_bytes, run.cpu_seconds, run.bytes_read = 432013312, 3.1, 1288490188
        session.commit()
    after = client.get(f"{url}/{query_id}", headers=AUTH_HEADER).json()
    assert (after["peak_memory_bytes"], after["cpu_seconds"], after["bytes_read"]) == (
        432013312,
        3.1,
        1288490188,
    )


# --- The JSON page of a result ----------------------------------------------------------

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
"""A fixed start time for a run whose duration a test reads back."""


def result_url(qid: str, query: str = "") -> str:
    """Build the result route of a query in `dev`.

    Parameters
    ----------
    qid
        The query's id.
    query
        The query string without its ``?``, or empty.

    Returns
    -------
    str
        The URL.
    """
    url = f"/api/v1/environments/dev/queries/{qid}/result"
    return f"{url}?{query}" if query else url


def stored_result(client: TestClient, env: str, table: pa.Table, **run_fields: object) -> str:
    """Submit a query as the administrator and finish it with `table` as its stored result.

    The worker is bypassed as in `test_get_result_of_succeeded_query_returns_arrow_bytes`: the
    run is set to `SUCCEEDED` and the result written as an Arrow IPC file through the
    environment's storage.

    Parameters
    ----------
    client
        Test client for the API.
    env
        An existing environment.
    table
        The result.
    **run_fields
        Further attributes of the run, such as `preview_table_id` or `masked_columns`.

    Returns
    -------
    str
        The query's id.
    """
    submitted = client.post(
        f"/api/v1/environments/{env}/queries", json={"sql": "SELECT 1"}, headers=AUTH_HEADER
    )
    assert submitted.status_code == 202, submitted.text
    qid = submitted.json()["query_id"]
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    path = f"_artifacts/queries/{qid}/result.arrow"
    with Session(client.app.state.engine) as session:
        StorageResolver(session).for_environment(env).storage.write(
            path, sink.getvalue().to_pybytes()
        )
        run = session.get(QueryRun, uuid.UUID(qid))
        assert run is not None
        run.status = QueryRunStatus.SUCCEEDED
        run.result_location = path
        run.lineage_json = {}
        for name, value in run_fields.items():
            setattr(run, name, value)
        session.commit()
    return qid


def set_status(client: TestClient, qid: str, status: str) -> str:
    """Set a run's status as the worker would, and hand its id back.

    Parameters
    ----------
    client
        Test client for the API.
    qid
        The query's id.
    status
        A `QueryRunStatus` value.

    Returns
    -------
    str
        `qid`, so a call can sit inside a URL.
    """
    _put_status(client, qid, status)
    return qid


def raising(*args: object, **kwargs: object) -> bytes:
    """Stand in for `json_page` and fail as a conversion would.

    Parameters
    ----------
    *args
        Ignored.
    **kwargs
        Ignored.

    Raises
    ------
    LakanaError
        Always; the server maps an unmapped `LakanaError` to 500.
    """
    raise LakanaError("conversion failed")


def test_the_json_page_carries_masked_columns_and_duration(client: TestClient) -> None:
    _environment(client)
    qid = stored_result(
        client,
        "dev",
        pa.table({"i": [1]}),
        masked_columns=[{"table": "gold.orders", "column": "email"}],
        started_at=T0,
        finished_at=T0 + timedelta(milliseconds=1500),
    )
    response = client.get(result_url(qid, "format=json"), headers=AUTH_HEADER)
    assert response.headers["content-type"] == "application/json"
    page = response.json()
    assert (page["masked_columns"], page["duration_ms"]) == (
        [{"table": "gold.orders", "column": "email"}],
        1500,
    )
    assert (page["columns"], page["rows"], page["next_offset"]) == (
        [{"name": "i", "type": "int64"}],
        [["1"]],
        None,
    )


def test_a_json_page_of_an_unfinished_run_is_409(client: TestClient) -> None:
    _environment(client)
    qid = stored_result(client, "dev", pa.table({"i": [1]}))
    for status in ("QUEUED", "RUNNING", "CANCELLED", "FAILED"):
        response = client.get(
            result_url(set_status(client, qid, status), "format=json"), headers=AUTH_HEADER
        )
        assert response.status_code == 409, status
        assert f"status: {status}" in response.json()["detail"]


def test_a_json_page_of_a_write_is_the_409_of_the_arrow_result(client: TestClient) -> None:
    _environment(client)
    qid = stored_result(client, "dev", pa.table({"i": [1]}), result_location=None)
    arrow = client.get(result_url(qid), headers=AUTH_HEADER)
    page = client.get(result_url(qid, "format=json"), headers=AUTH_HEADER)
    assert (page.status_code, page.json()) == (409, arrow.json())


def test_a_run_from_before_this_release_has_masked_columns_null(client: TestClient) -> None:
    _environment(client)
    page = client.get(
        result_url(stored_result(client, "dev", pa.table({"i": [1]})), "format=json"),
        headers=AUTH_HEADER,
    ).json()
    assert (page["masked_columns"], page["preview"], page["duration_ms"]) == (None, None, None)


def test_a_json_page_follows_limit_and_offset(client: TestClient) -> None:
    _environment(client)
    qid = stored_result(client, "dev", pa.table({"i": pa.array(range(250), pa.int32())}))
    first = client.get(result_url(qid, "format=json"), headers=AUTH_HEADER).json()
    assert (len(first["rows"]), first["rows"][0], first["next_offset"]) == (100, [0], 100)
    last = client.get(result_url(qid, "format=json&offset=240&limit=20"), headers=AUTH_HEADER)
    assert (last.json()["rows"], last.json()["next_offset"]) == (
        [[i] for i in range(240, 250)],
        None,
    )
    assert last.json()["row_count"] == 250


@pytest.mark.parametrize(
    ("query", "name", "bound"),
    [
        ("format=json&limit=0", "limit", "greater than or equal to 1"),
        ("format=json&limit=1001", "limit", "less than or equal to 1000"),
        ("format=json&offset=-1", "offset", "greater than or equal to 0"),
    ],
)
def test_a_limit_or_offset_out_of_range_is_422_naming_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, query: str, name: str, bound: str
) -> None:
    _environment(client)
    qid = stored_result(client, "dev", pa.table({"i": [1]}))
    monkeypatch.setattr(queries_module, "json_page", raising)
    response = client.get(result_url(qid, query), headers=AUTH_HEADER)
    assert response.status_code == 422
    (problem,) = response.json()["detail"]
    assert (problem["loc"], bound in problem["msg"]) == (["query", name], True)


def test_limit_or_offset_with_arrow_is_422(client: TestClient) -> None:
    _environment(client)
    qid = stored_result(client, "dev", pa.table({"i": [1]}))
    for query in ("limit=5", "offset=0", "format=arrow&limit=5"):
        response = client.get(result_url(qid, query), headers=AUTH_HEADER)
        assert response.status_code == 422, query
        assert response.json()["detail"] == (
            "limit and offset apply to format=json; arrow, csv and xlsx are always the whole "
            "result."
        )


def test_an_unknown_format_is_422(client: TestClient) -> None:
    _environment(client)
    qid = stored_result(client, "dev", pa.table({"i": [1]}))
    response = client.get(result_url(qid, "format=parquet"), headers=AUTH_HEADER)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "format"]


def test_arrow_stays_the_default(client: TestClient) -> None:
    _environment(client)
    table = pa.table({"i": [1, 2]})
    qid = stored_result(client, "dev", table)
    response = client.get(result_url(qid), headers=AUTH_HEADER)
    assert response.headers["content-type"] == "application/vnd.apache.arrow.file"
    assert pa.ipc.open_file(pa.BufferReader(response.content)).read_all().equals(table)


def test_a_full_json_limiter_answers_503_with_retry_after(client: TestClient) -> None:
    _environment(client)
    qid = stored_result(client, "dev", pa.table({"i": [1]}))
    lim = client.app.state.json_page_limiter
    for _ in range(4):
        assert lim.try_acquire(uuid.uuid4()) is None
    response = client.get(result_url(qid, "format=json"), headers=AUTH_HEADER)
    assert (response.status_code, response.headers["Retry-After"], response.json()) == (
        503,
        "1",
        {
            "detail": "The server is preparing other result pages; try again in a moment.",
            "type": "ResultPageLimitError",
        },
    )


def test_a_principal_with_a_page_in_progress_is_told_to_wait_for_it(client: TestClient) -> None:
    _environment(client)
    qid = stored_result(client, "dev", pa.table({"i": [1]}))
    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    assert client.app.state.json_page_limiter.try_acquire(admin) is None
    response = client.get(result_url(qid, "format=json"), headers=AUTH_HEADER)
    assert (response.status_code, response.headers["Retry-After"]) == (503, "1")
    assert response.json()["detail"] == (
        "You already have a result page being prepared; wait for it, then try again."
    )


def test_the_arrow_result_takes_no_json_slot(client: TestClient) -> None:
    _environment(client)
    qid = stored_result(client, "dev", pa.table({"i": [1]}))
    lim = client.app.state.json_page_limiter
    for _ in range(4):
        lim.try_acquire(uuid.uuid4())
    assert client.get(result_url(qid), headers=AUTH_HEADER).status_code == 200


def test_the_slot_is_released_after_a_page_and_after_a_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environment(client)
    qid = stored_result(client, "dev", pa.table({"i": [1]}))
    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    assert client.get(result_url(qid, "format=json"), headers=AUTH_HEADER).status_code == 200
    monkeypatch.setattr(queries_module, "json_page", raising)
    assert client.get(result_url(qid, "format=json"), headers=AUTH_HEADER).status_code == 500
    assert client.app.state.json_page_limiter.try_acquire(admin) is None


def test_the_result_route_documents_every_format(client: TestClient) -> None:
    spec = client.app.openapi()
    op = spec["paths"]["/api/v1/environments/{env}/queries/{query_id}/result"]["get"]
    content = op["responses"]["200"]["content"]
    assert set(content) == {
        "application/vnd.apache.arrow.file",
        "application/json",
        "text/csv; charset=utf-8",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    assert content["application/json"]["schema"] == {"$ref": "#/components/schemas/ResultPageOut"}
    assert {code for code in op["responses"]} == {"200", "401", "403", "404", "409", "422", "503"}
    limit = next(p for p in op["parameters"] if p["name"] == "limit")
    assert (limit["schema"]["anyOf"][0]["minimum"], limit["schema"]["anyOf"][0]["maximum"]) == (
        1,
        1000,
    )


def test_the_page_schema_declares_every_key_the_writer_emits() -> None:
    wide = pa.struct([(f"field_{i}", pa.int8()) for i in range(40)])
    body = json_page(
        pa.table({"s": pa.array([None], wide)}),
        offset=0,
        limit=1,
        row_cap=None,
        duration_ms=1,
        masked_columns=[{"table": "gold.orders", "column": "email"}],
        preview_table="x" * 2000,
    )
    page = json.loads(body)
    assert page["columns"][0]["type_cut"] is True
    assert page["preview"]["cut"] is True
    assert set(page) == set(ResultPageOut.model_fields)
    assert set(page["columns"][0]) == set(ResultColumnOut.model_fields)
    assert set(page["masked_columns"][0]) == set(MaskedColumnOut.model_fields)
    assert set(page["preview"]) == set(PreviewOut.model_fields)
    ResultPageOut.model_validate(page)


def _submit_in(client: TestClient, env: str, sql: str = "SELECT 1") -> str:
    """Create environment `env` and submit `sql` in it.

    Parameters
    ----------
    client
        Test client for the API.
    env
        Name of the environment to create.
    sql
        The query to submit.

    Returns
    -------
    str
        The new query's id.
    """
    client.post(
        "/api/v1/environments",
        json={"name": env, "storage": {"scheme": "file", "root": "file:///tmp/s", "options": {}}},
        headers=AUTH_HEADER,
    )
    submitted = client.post(
        f"/api/v1/environments/{env}/queries", json={"sql": sql}, headers=AUTH_HEADER
    )
    assert submitted.status_code == 202
    return submitted.json()["query_id"]


def _edit_run(client: TestClient, query_id: str, **values: object) -> None:
    """Set attributes of a query run straight in the app's database.

    Parameters
    ----------
    client
        Test client for the API.
    query_id
        The query's id.
    **values
        Attribute names of `QueryRun` and the values to set.
    """
    with Session(client.app.state.engine) as session:  # type: ignore[attr-defined]
        run = session.get(QueryRun, uuid.UUID(query_id))
        assert run is not None
        for name, value in values.items():
            setattr(run, name, value)
        session.commit()


def test_the_status_of_a_written_query_has_its_sql_origin_and_times(client: TestClient) -> None:
    """A written query's status carries the SQL as submitted, its origin and its times."""
    query_id = _submit_in(client, "st1", "SELECT 1 AS one")
    started = datetime(2026, 11, 1, 10, 0, 1, tzinfo=UTC)
    finished = datetime(2026, 11, 1, 10, 0, 2, tzinfo=UTC)
    _edit_run(client, query_id, started_at=started, finished_at=finished)

    body = client.get(f"/api/v1/environments/st1/queries/{query_id}", headers=AUTH_HEADER).json()

    assert body["origin"] == "written"
    assert body["sql"] == "SELECT 1 AS one"
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None
    assert datetime.fromisoformat(body["started_at"]) == started
    assert datetime.fromisoformat(body["finished_at"]) == finished
    assert body["result_rows"] is None
    assert body["result_bytes"] is None
    assert body["error_line"] is None
    assert body["error_column"] is None


def test_the_status_of_a_write_has_its_sql_and_a_server_made_run_has_none(
    client: TestClient,
) -> None:
    """Only `written` and `write` runs give back their SQL; every other origin says `null`."""
    query_id = _submit_in(client, "st2", "SELECT 1")
    shown = {"written": True, "write": True}
    for origin in QueryOrigin:
        _edit_run(client, query_id, origin=origin)
        body = client.get(
            f"/api/v1/environments/st2/queries/{query_id}", headers=AUTH_HEADER
        ).json()
        assert body["origin"] == origin.value
        assert (body["sql"] is not None) is shown.get(origin.value, False)


def test_the_status_of_a_preview_has_no_sql(client: TestClient) -> None:
    """A preview's SQL is the server's own text; the status read says `null`."""
    query_id = _submit_in(client, "st3", "SELECT 1")
    _edit_run(client, query_id, origin=QueryOrigin.PREVIEW)

    body = client.get(f"/api/v1/environments/st3/queries/{query_id}", headers=AUTH_HEADER).json()

    assert body["origin"] == "preview"
    assert body["sql"] is None


def test_the_status_of_a_succeeded_read_has_its_result_rows_and_bytes(
    client: TestClient,
) -> None:
    """Once a read succeeded, its row count and the stored file's size are in the status."""
    query_id = _submit_in(client, "st4")
    _edit_run(client, query_id, status=QueryRunStatus.SUCCEEDED, result_rows=3, result_bytes=1234)

    body = client.get(f"/api/v1/environments/st4/queries/{query_id}", headers=AUTH_HEADER).json()

    assert (body["result_rows"], body["result_bytes"]) == (3, 1234)


def test_the_status_of_a_failed_query_has_its_error_position(client: TestClient) -> None:
    """A failed query's status gives the line and column the worker recorded."""
    query_id = _submit_in(client, "st5", "SELECT\n  FROM")
    _edit_run(
        client,
        query_id,
        status=QueryRunStatus.FAILED,
        error_message="bad",
        error_cause="invalid_query",
        error_line=2,
        error_column=3,
    )

    body = client.get(f"/api/v1/environments/st5/queries/{query_id}", headers=AUTH_HEADER).json()

    assert (body["error_line"], body["error_column"]) == (2, 3)


# -- the list across environments ------------------------------------

_ALL = "/api/v1/queries"


def test_get_all_queries_answers_items_and_next_cursor(client: TestClient) -> None:
    """The cross-environment list has the body of a new paged list, not the header."""
    _environment(client)
    _environment(client, "other")
    first = _submit(client)
    second = _submit(client, env="other")
    third = _submit(client)
    page = client.get(_ALL, params={"limit": 2}, headers=AUTH_HEADER)
    assert page.status_code == 200, page.text
    assert "X-Next-Cursor" not in page.headers
    body = page.json()
    assert set(body) == {"items", "next_cursor"}
    assert [row["id"] for row in body["items"]] == [first, second]
    assert body["next_cursor"] is not None
    rest = client.get(
        _ALL, params={"limit": 2, "cursor": body["next_cursor"]}, headers=AUTH_HEADER
    ).json()
    assert ([row["id"] for row in rest["items"]], rest["next_cursor"]) == ([third], None)
    only = client.get(_ALL, params={"environment": "other"}, headers=AUTH_HEADER).json()
    assert [row["id"] for row in only["items"]] == [second]
    per_env = client.get(_list_url(), params={"limit": 1}, headers=AUTH_HEADER)
    refused = client.get(
        _ALL, params={"cursor": per_env.headers["X-Next-Cursor"]}, headers=AUTH_HEADER
    )
    assert refused.status_code == 422


def test_get_all_queries_with_an_unknown_environment_is_404(client: TestClient) -> None:
    response = client.get(_ALL, params={"environment": "nowhere"}, headers=AUTH_HEADER)
    assert response.status_code == 404
    assert "No environment named" in response.json()["detail"]


def test_get_all_queries_of_an_environment_without_a_right_is_403_as_its_own_list(
    client: TestClient,
) -> None:
    _environment(client)
    reader = person(client, "reader", "VIEWER")
    own = client.get(_list_url(), headers=reader)
    across = client.get(_ALL, params={"environment": "dev"}, headers=reader)
    assert (own.status_code, across.status_code) == (403, 403)
    assert across.json()["detail"] == own.json()["detail"]


def test_a_deleted_environments_runs_never_appear_across_environments(client: TestClient) -> None:
    _environment(client)
    _environment(client, "other")
    kept = _submit(client)
    gone = _submit(client, env="other")
    _put_status(client, gone, "SUCCEEDED")
    deleted = client.delete("/api/v1/environments/other", headers=AUTH_HEADER)
    assert deleted.status_code in (200, 204), deleted.text
    listed = client.get(_ALL, params={"include_ended": "true"}, headers=AUTH_HEADER).json()
    assert [row["id"] for row in listed["items"]] == [kept]


@pytest.mark.parametrize("route", ["one", "all"])
def test_since_alone_lists_the_runs_that_ended_since(client: TestClient, route: str) -> None:
    """`since` without `include_ended` still lists ended runs: a filter never does nothing."""
    _environment(client)
    old = _submit(client)
    _put_status(client, old, "SUCCEEDED")
    _edit_run(client, old, finished_at=datetime.now(UTC) - timedelta(days=3))
    older = _submit(client)
    _put_status(client, older, "SUCCEEDED")
    _edit_run(client, older, finished_at=datetime.now(UTC) - timedelta(days=10))
    week = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    if route == "one":
        rows = client.get(_list_url(), params={"since": week}, headers=AUTH_HEADER).json()
    else:
        rows = client.get(_ALL, params={"since": week}, headers=AUTH_HEADER).json()["items"]
    assert [row["id"] for row in rows] == [old]


def test_get_all_queries_without_any_right_is_an_empty_page(client: TestClient) -> None:
    """A caller with no right anywhere gets an empty page, never a 403 or a 404."""
    _environment(client)
    _environment(client, "other")
    _submit(client)
    reader = person(client, "reader", "VIEWER")
    listed = client.get(_ALL, headers=reader)
    assert listed.status_code == 200, listed.text
    assert listed.json() == {"items": [], "next_cursor": None}
    assert client.get(f"{_ALL}/environments", headers=reader).json() == []
    assert client.get(_ALL).status_code == 401


def test_rows_carry_environment_and_origin(client: TestClient) -> None:
    _environment(client)
    written = _submit(client)
    task = _submit(client)
    _edit_run(client, task, origin=QueryOrigin.TASK)
    rows = client.get(_list_url(), headers=AUTH_HEADER).json()
    assert [(row["id"], row["environment"], row["origin"]) for row in rows] == [
        (written, "dev", "written"),
        (task, "dev", "task"),
    ]
    across = client.get(_ALL, params={"origin": "task"}, headers=AUTH_HEADER).json()["items"]
    assert [(row["id"], row["environment"], row["origin"]) for row in across] == [
        (task, "dev", "task")
    ]
    by_origin = client.get(_list_url(), params={"origin": "written,task"}, headers=AUTH_HEADER)
    assert len(by_origin.json()) == 2
    unknown = client.get(_list_url(), params={"origin": "typed"}, headers=AUTH_HEADER)
    assert unknown.status_code == 422
    assert "`origin` must be one of written, preview" in unknown.text
    _put_status(client, written, "SUCCEEDED")
    _edit_run(client, written, finished_at=datetime.now(UTC) - timedelta(days=3))
    params = {"include_ended": "true", "status": "succeeded"}
    assert client.get(_list_url(), params=params, headers=AUTH_HEADER).json() == []
    week = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    old = client.get(_ALL, params={**params, "since": week}, headers=AUTH_HEADER).json()
    assert [row["id"] for row in old["items"]] == [written]
    naive = client.get(_list_url(), params={"since": "2026-10-18T09:30:00"}, headers=AUTH_HEADER)
    assert naive.status_code == 422
    assert "`since` has no time zone" in naive.text


def test_get_query_environments(client: TestClient) -> None:
    """The environments where the caller may submit a query, by name, with their protection."""
    _environment(client, "prod")
    _environment(client)
    _environment(client, "stage")
    from lakana_core.models import Environment

    with Session(client.app.state.engine) as session:  # type: ignore[attr-defined]
        prod = session.query(Environment).filter_by(name="prod").one()
        prod.is_protected = True
        session.commit()
    answer = client.get(f"{_ALL}/environments", headers=AUTH_HEADER)
    assert answer.status_code == 200, answer.text
    assert answer.json() == [
        {"name": "dev", "protected": False},
        {"name": "prod", "protected": True},
        {"name": "stage", "protected": False},
    ]
    analyst = person(client, "ana", "ANALYST", env="stage")
    assert client.get(f"{_ALL}/environments", headers=analyst).json() == [
        {"name": "stage", "protected": False}
    ]


# --- Whole-result files ---------------------------------------------------------------------------

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
THREE_ROWS = pa.table({"name": ["a,b", "é", None], "n": [1, 2, 3]})
"""A small result; its rows are written as `result_rows` where a test records sizes."""


def _arrow_size(table: pa.Table) -> int:
    """Return the length of the Arrow IPC file `stored_result` writes for a table.

    Parameters
    ----------
    table
        The result.

    Returns
    -------
    int
        The stored file's bytes.
    """
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return len(sink.getvalue())


def _sized(client: TestClient, table: pa.Table = THREE_ROWS) -> str:
    """Store a result in `dev` whose run records its rows and size, as the worker does.

    Parameters
    ----------
    client
        Test client for the API.
    table
        The result.

    Returns
    -------
    str
        The query's id.
    """
    return stored_result(
        client, "dev", table, result_rows=table.num_rows, result_bytes=_arrow_size(table)
    )


def _exports(client: TestClient) -> list[tuple[object, dict[str, object]]]:
    """List the `QUERY_RESULT_EXPORTED` audit events as (principal id, detail).

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

    from lakana_core.models import AuditEvent, AuditEventType

    with Session(client.app.state.engine) as session:
        rows = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.QUERY_RESULT_EXPORTED)
            .order_by(AuditEvent.occurred_at)
        ).all()
        return [(row.principal_id, dict(row.detail)) for row in rows]


def test_csv_result_streams_with_disposition_and_size_headers(client: TestClient) -> None:
    _environment(client)
    qid = _sized(client)
    response = client.get(result_url(qid, "format=csv"), headers=AUTH_HEADER)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == f'attachment; filename="query-{qid}.csv"'
    assert response.headers["x-result-rows"] == "3"
    assert response.headers["x-result-arrow-bytes"] == str(_arrow_size(THREE_ROWS))
    assert "content-length" not in response.headers
    assert response.content == b'"name","n"\r\n"a,b",1\r\n"\xc3\xa9",2\r\n,3\r\n'


def test_xlsx_result_is_a_zip_with_the_spreadsheet_media_type(client: TestClient) -> None:
    _environment(client)
    qid = _sized(client)
    response = client.get(result_url(qid, "format=xlsx"), headers=AUTH_HEADER)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == XLSX
    assert response.headers["content-disposition"] == f'attachment; filename="query-{qid}.xlsx"'
    assert "content-length" not in response.headers
    assert response.content.startswith(b"PK\x03\x04")


def test_arrow_result_now_has_the_disposition_and_still_the_same_bytes(client: TestClient) -> None:
    _environment(client)
    qid = _sized(client)
    response = client.get(result_url(qid, "format=arrow"), headers=AUTH_HEADER)
    assert response.headers["content-type"] == "application/vnd.apache.arrow.file"
    assert response.headers["content-disposition"] == f'attachment; filename="query-{qid}.arrow"'
    assert response.headers["x-result-rows"] == "3"
    assert "content-length" not in response.headers
    assert pa.ipc.open_file(pa.BufferReader(response.content)).read_all().equals(THREE_ROWS)


def test_a_whole_result_download_writes_one_export_event_per_format(client: TestClient) -> None:
    _environment(client)
    qid = _sized(client)
    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    assert client.get(result_url(qid, "format=json"), headers=AUTH_HEADER).status_code == 200
    assert _exports(client) == []
    for result_format in ("arrow", "csv", "xlsx"):
        got = client.get(result_url(qid, f"format={result_format}"), headers=AUTH_HEADER)
        assert got.status_code == 200, got.text
    detail = {"query_id": qid, "rows": 3, "size_bytes": _arrow_size(THREE_ROWS)}
    assert _exports(client) == [
        (admin, {**detail, "format": "arrow"}),
        (admin, {**detail, "format": "csv"}),
        (admin, {**detail, "format": "xlsx"}),
    ]


def test_a_download_of_another_principals_run_is_404_and_writes_no_export_event(
    client: TestClient,
) -> None:
    _environment(client)
    qid = _sized(client)
    bob = person(client, "bob", "ANALYST")
    carol = person(client, "carol", "ADMIN")
    for headers in (bob, carol):
        for result_format in ("arrow", "csv", "xlsx"):
            refused = client.get(result_url(qid, f"format={result_format}"), headers=headers)
            assert refused.status_code == 404, (result_format, refused.text)
    assert _exports(client) == []


def test_a_bound_task_token_writes_no_export_event(client: TestClient) -> None:
    from lakana_core.auth import BoundTask, PrincipalRef, TaskScope
    from lakana_core.models import PrincipalType
    from lakana_server.deps import get_principal

    _environment(client)
    token_id = uuid.uuid4()
    qid = stored_result(client, "dev", THREE_ROWS, task_token_id=token_id)
    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    assert isinstance(admin, uuid.UUID)
    scope = TaskScope(
        environment="dev",
        asset_scope=[],
        token_id=token_id,
        bound=BoundTask(uuid.uuid4(), 1, "gold.scores", ("bronze.events",), ("bronze.events",)),
    )
    bound = PrincipalRef(id=admin, name="admin", type=PrincipalType.USER, task_scope=scope)
    client.app.dependency_overrides[get_principal] = lambda: bound
    try:
        for result_format in ("arrow", "csv", "xlsx"):
            got = client.get(result_url(qid, f"format={result_format}"), headers=AUTH_HEADER)
            assert got.status_code == 200, (result_format, got.text)
    finally:
        del client.app.dependency_overrides[get_principal]
    assert _exports(client) == []


def test_an_xlsx_refusal_is_422_with_the_sentence_and_writes_no_export_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_server import result_files

    monkeypatch.setattr(result_files, "XLSX_MAX_ROWS", 2)
    _environment(client)
    qid = _sized(client)
    refused = client.get(result_url(qid, "format=xlsx"), headers=AUTH_HEADER)
    assert refused.status_code == 422
    assert refused.json() == {
        "detail": (
            "This result has 3 rows; a spreadsheet holds at most 2 below its header row. "
            f"Download it as CSV instead: lakana query result {qid} --env dev --format csv "
            f"--out query-{qid}.csv"
        ),
        "type": "XlsxRefused",
    }
    assert _exports(client) == []
    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    assert client.app.state.result_file_limiter.try_acquire(admin) is None


def test_paging_parameters_with_a_file_format_are_422(client: TestClient) -> None:
    _environment(client)
    qid = _sized(client)
    for result_format in ("arrow", "csv", "xlsx"):
        for paging in ("limit=5", "offset=0"):
            response = client.get(
                result_url(qid, f"format={result_format}&{paging}"), headers=AUTH_HEADER
            )
            assert response.status_code == 422, (result_format, paging)
            assert response.json()["detail"] == queries_module.WHOLE_RESULT_TAKES_NO_PAGING
    assert _exports(client) == []


def test_a_second_file_of_the_same_person_at_once_is_503_with_retry_after(
    client: TestClient,
) -> None:
    _environment(client)
    qid = _sized(client)
    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    lim = client.app.state.result_file_limiter
    assert lim.try_acquire(admin) is None
    for result_format in ("csv", "xlsx"):
        busy = client.get(result_url(qid, f"format={result_format}"), headers=AUTH_HEADER)
        assert (busy.status_code, busy.headers["Retry-After"], busy.json()) == (
            503,
            "1",
            {
                "detail": (
                    "You already have a result file being prepared; wait for it, then try again."
                ),
                "type": "ResultFileLimitError",
            },
        )
    lim.release(admin)
    others = [uuid.uuid4(), uuid.uuid4()]
    for other in others:
        assert lim.try_acquire(other) is None
    full = client.get(result_url(qid, "format=csv"), headers=AUTH_HEADER)
    assert (full.status_code, full.json()["detail"]) == (
        503,
        "The server is preparing other result files; try again in a moment.",
    )
    assert _exports(client) == []


def test_the_file_slot_is_given_back_when_the_body_ends_and_when_preparing_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _environment(client)
    qid = _sized(client)
    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    lim = client.app.state.result_file_limiter
    for result_format in ("csv", "xlsx"):
        got = client.get(result_url(qid, f"format={result_format}"), headers=AUTH_HEADER)
        assert got.status_code == 200, (result_format, got.text)
        assert lim.try_acquire(admin) is None, result_format
        lim.release(admin)
    exported = len(_exports(client))
    # A conversion that fails on a value fails while the file is prepared: before the 200
    # and before the export is recorded.
    monkeypatch.setattr(queries_module, "write_csv", raising)
    monkeypatch.setattr(queries_module, "write_xlsx", raising)
    for result_format in ("csv", "xlsx"):
        failed = client.get(result_url(qid, f"format={result_format}"), headers=AUTH_HEADER)
        assert failed.status_code == 500, result_format
        assert lim.try_acquire(admin) is None, result_format
        lim.release(admin)
    assert len(_exports(client)) == exported


class _Spooled:
    """Record the temporary files the result route opens, to see that each is closed."""

    def __init__(self) -> None:
        import tempfile

        self.files: list[IO[bytes]] = []
        self._open = tempfile.TemporaryFile

    def __call__(self) -> IO[bytes]:
        """Open a real temporary file and remember it.

        Returns
        -------
        file object
            The temporary file.
        """
        opened = self._open()
        self.files.append(opened)
        return opened


def _download_over_asgi(
    client: TestClient, qid: str, result_format: str, fail_on: str
) -> tuple[bool, list[str]]:
    """Fetch a result file straight through ASGI, with a client that goes away.

    Parameters
    ----------
    client
        Test client for the API; its app has run its lifespan.
    qid
        The query's id.
    result_format
        ``csv`` or ``xlsx``.
    fail_on
        The ASGI message type whose send fails as a client that went away:
        ``http.response.start`` (the body never starts) or ``http.response.body``.

    Returns
    -------
    tuple
        Whether the caller's file slot was free when the response started, and the types of
        the messages sent before the failure.
    """
    import asyncio
    import contextlib

    from starlette.requests import ClientDisconnect

    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    lim = client.app.state.result_file_limiter
    free_at_start: list[bool] = []
    sent: list[str] = []
    path = f"/api/v1/environments/dev/queries/{qid}/result"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": f"format={result_format}".encode(),
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", AUTH_HEADER["Authorization"].encode()),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    requested: list[bool] = []

    async def receive() -> dict[str, Any]:
        if not requested:
            requested.append(True)
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        return {}  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start" and not free_at_start:
            free = lim.try_acquire(admin) is None
            if free:
                lim.release(admin)
            free_at_start.append(free)
        if message["type"] == fail_on:
            raise OSError("the client went away")
        sent.append(message["type"])
        await asyncio.sleep(0)

    async def go() -> None:
        with contextlib.suppress(OSError, ClientDisconnect):
            await client.app(scope, receive, send)

    asyncio.run(go())
    return free_at_start == [True], sent


@pytest.mark.parametrize("result_format", ["csv", "xlsx"])
@pytest.mark.parametrize("fail_on", ["http.response.start", "http.response.body"])
def test_a_file_streams_without_a_slot_and_its_temporary_file_closes_when_the_client_goes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, result_format: str, fail_on: str
) -> None:
    """A paused or vanished reader holds no file slot; the spooled file closes on every path."""
    _environment(client)
    qid = _sized(client)
    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    spooled = _Spooled()
    monkeypatch.setattr(queries_module.tempfile, "TemporaryFile", spooled)
    free_at_start, sent = _download_over_asgi(client, qid, result_format, fail_on)
    assert free_at_start
    assert "http.response.body" not in sent
    assert len(spooled.files) == 1
    assert spooled.files[0].closed
    assert client.app.state.result_file_limiter.try_acquire(admin) is None


def test_a_result_without_recorded_sizes_downloads_without_the_size_headers(
    client: TestClient,
) -> None:
    _environment(client)
    qid = stored_result(client, "dev", THREE_ROWS)
    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    for result_format in ("csv", "arrow"):
        response = client.get(result_url(qid, f"format={result_format}"), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        assert "x-result-rows" not in response.headers
        assert "x-result-arrow-bytes" not in response.headers
    csv_detail = {"query_id": qid, "rows": 3, "size_bytes": _arrow_size(THREE_ROWS)}
    assert _exports(client) == [
        (admin, {**csv_detail, "format": "csv"}),
        (admin, {"query_id": qid, "rows": None, "size_bytes": None, "format": "arrow"}),
    ]


def test_a_column_no_file_holds_is_422_before_any_export_or_byte(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A type the flattening does not turn into one a file holds is refused, not cut mid-body."""
    from lakana_server import result_files

    _environment(client)
    bignum = pa.opaque(pa.binary(), "bignum", "DuckDB")
    qid = stored_result(client, "dev", pa.table({"big": pa.array([b"\x05"], bignum)}))
    admin = query_run_columns(client, qid, "principal_id")["principal_id"]
    assert client.get(result_url(qid, "format=csv"), headers=AUTH_HEADER).content == (
        b'"big"\r\n"\\x05"\r\n'
    )
    exported = len(_exports(client))
    monkeypatch.setattr(result_files, "_cell_column", lambda column: column)
    for result_format in ("csv", "xlsx"):
        refused = client.get(result_url(qid, f"format={result_format}"), headers=AUTH_HEADER)
        assert refused.status_code == 422, (result_format, refused.text)
        assert refused.json()["type"] == "ResultFileRefused"
        assert f"--format arrow --out query-{qid}.arrow" in refused.json()["detail"]
    assert len(_exports(client)) == exported
    assert client.app.state.result_file_limiter.try_acquire(admin) is None
