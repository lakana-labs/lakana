"""The deploy resources of the platform API."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.models import (
    DeployRun,
    DeployStatus,
    Environment,
    JobVersion,
    Namespace,
    Repository,
    Table,
    TableType,
    Worker,
)
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment


def _token_for(client: TestClient, name: str) -> dict[str, str]:
    """Create a service account with a PAT and return its auth header.

    Parameters
    ----------
    client
        The test client.
    name
        Principal name.

    Returns
    -------
    dict of str to str
        An Authorization header.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "SERVICE_ACCOUNT"}
    )
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _setup(client: TestClient) -> None:
    """Create `dev` and an https repository.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client, "dev")
    client.post(
        "/api/v1/repositories",
        headers=AUTH_HEADER,
        json={"name": "analytics", "kind": "https", "url": "https://github.com/acme/analytics.git"},
    )


def test_create_returns_202_and_the_run_is_readable(client: TestClient) -> None:
    """A deploy is accepted, queued and visible in history."""
    _setup(client)
    accepted = client.post(
        "/api/v1/environments/dev/deploys",
        headers=AUTH_HEADER,
        json={"repository": "analytics", "ref": "main"},
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["status"] == "queued"
    deploy_id = accepted.json()["id"]
    shown = client.get(f"/api/v1/environments/dev/deploys/{deploy_id}", headers=AUTH_HEADER).json()
    assert (shown["repository"], shown["kind"], shown["requested_ref"]) == (
        "analytics",
        "deploy",
        "main",
    )
    history = client.get("/api/v1/environments/dev/deploys", headers=AUTH_HEADER).json()
    assert [d["id"] for d in history] == [deploy_id]
    assert client.get("/api/v1/environments/dev/deployments", headers=AUTH_HEADER).json() == []
    assert client.get("/api/v1/environments/dev/orphans", headers=AUTH_HEADER).json() == {
        "orphans": [],
        "hidden": 0,
    }


def test_shape_errors_are_422(client: TestClient) -> None:
    """Bad refs and malformed rollbacks are validation errors."""
    _setup(client)
    for body in (
        {"repository": "analytics", "ref": "--upload-pack=x"},
        {"repository": "analytics", "kind": "rollback"},
        {"repository": "analytics", "kind": "rollback", "to_sha": "a" * 40},
        {"repository": "analytics", "kind": "explode"},
    ):
        response = client.post("/api/v1/environments/dev/deploys", headers=AUTH_HEADER, json=body)
        assert response.status_code == 422, body


def test_principal_without_deploy_is_refused(client: TestClient) -> None:
    """Exit criterion: no DEPLOY on the environment → 403."""
    _setup(client)
    header = _token_for(client, "intruder")
    response = client.post(
        "/api/v1/environments/dev/deploys", headers=header, json={"repository": "analytics"}
    )
    assert response.status_code == 403


def test_read_side_needs_read_environment(client: TestClient) -> None:
    """History, deployments, project versions and orphans are 403 without READ_ENVIRONMENT."""
    _setup(client)
    header = _token_for(client, "nobody")
    for path in (
        "/api/v1/environments/dev/deploys",
        f"/api/v1/environments/dev/deploys/{uuid.uuid4()}",
        "/api/v1/environments/dev/deployments",
        f"/api/v1/environments/dev/project-versions/{uuid.uuid4()}",
        "/api/v1/environments/dev/orphans",
    ):
        assert client.get(path, headers=header).status_code == 403, path


def test_unknown_ids_are_404(client: TestClient) -> None:
    """A deploy or project version id that does not exist here is a 404."""
    _setup(client)
    assert (
        client.get(
            f"/api/v1/environments/dev/deploys/{uuid.uuid4()}", headers=AUTH_HEADER
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/environments/dev/project-versions/{uuid.uuid4()}", headers=AUTH_HEADER
        ).status_code
        == 404
    )


def _insert_job_version(
    client: TestClient, environment: str, manifest: dict | None = None
) -> uuid.UUID:
    """Insert a project version with one asset that references a table.

    Parameters
    ----------
    client
        The test client.
    environment
        Environment name.
    manifest
        The manifest to store; one clean asset by default.

    Returns
    -------
    uuid.UUID
        The new project version's id.
    """
    with Session(client.app.state.engine) as session:
        env = session.scalars(select(Environment).where(Environment.name == environment)).one()
        repo = session.scalars(select(Repository)).one()
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json=manifest
            or {
                "version": 1,
                "assets": [{"table": "silver.clean", "sql": "SELECT 1", "references": []}],
                "edges": [],
                "external_inputs": [],
            },
            code_uri="s3://b/x.tar.gz",
            code_sha256="0" * 64,
        )
        session.add(version)
        session.commit()
        return version.id


@pytest.mark.slow
def test_job_version_is_readable_and_scoped_to_its_environment(client: TestClient) -> None:
    """A project version shows in its own environment and is a 404 through any other."""
    _setup(client)
    create_environment(client, "prod")
    version_id = _insert_job_version(client, "dev")
    shown = client.get(
        f"/api/v1/environments/dev/project-versions/{version_id}", headers=AUTH_HEADER
    )
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert (body["repository"], body["environment"]) == ("analytics", "dev")
    assert body["manifest"]["assets"][0]["hidden_references"] == 0
    other = client.get(
        f"/api/v1/environments/prod/project-versions/{version_id}", headers=AUTH_HEADER
    )
    assert other.status_code == 404


def test_history_does_not_leak_across_environments(client: TestClient) -> None:
    """A deploy queued in dev is absent from prod's history and a 404 there."""
    _setup(client)
    create_environment(client, "prod")
    accepted = client.post(
        "/api/v1/environments/dev/deploys", headers=AUTH_HEADER, json={"repository": "analytics"}
    ).json()
    assert client.get("/api/v1/environments/prod/deploys", headers=AUTH_HEADER).json() == []
    leaked = client.get(f"/api/v1/environments/prod/deploys/{accepted['id']}", headers=AUTH_HEADER)
    assert leaked.status_code == 404


def test_deploy_only_principal_can_poll_but_not_list(client: TestClient) -> None:
    """The CI recipe: a raw DEPLOY grant submits and polls its run, nothing more."""
    _setup(client)
    header = _token_for(client, "ci")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "ci",
            "action": "DEPLOY",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert granted.status_code in (200, 201), granted.text
    accepted = client.post(
        "/api/v1/environments/dev/deploys", headers=header, json={"repository": "analytics"}
    )
    assert accepted.status_code == 202, accepted.text
    polled = client.get(f"/api/v1/environments/dev/deploys/{accepted.json()['id']}", headers=header)
    assert polled.status_code == 200, polled.text
    assert client.get("/api/v1/environments/dev/deploys", headers=header).status_code == 403
    assert client.get("/api/v1/environments/dev/deployments", headers=header).status_code == 403


def _reader(client: TestClient) -> dict[str, str]:
    """Create a principal holding only READ_ENVIRONMENT on `dev`.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    dict of str to str
        Its auth header.
    """
    header = _token_for(client, "reader")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "reader",
            "action": "READ_ENVIRONMENT",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert granted.status_code in (200, 201), granted.text
    return header


def test_a_reader_without_table_access_sees_hidden_references_over_rest(
    client: TestClient,
) -> None:
    """The manifest's SQL and edges are withheld and counted for a READ_ENVIRONMENT-only reader."""
    _setup(client)
    manifest = {
        "version": 1,
        "assets": [
            {"table": "silver.a", "sql": "SELECT 1", "references": []},
            {
                "table": "gold.b",
                "sql": "SELECT * FROM silver.a, bronze.secret",
                "references": ["silver.a", "bronze.secret"],
            },
        ],
        "edges": [["silver.a", "gold.b"]],
        "external_inputs": ["bronze.secret"],
    }
    version_id = _insert_job_version(client, "dev", manifest)
    header = _reader(client)
    body = client.get(
        f"/api/v1/environments/dev/project-versions/{version_id}", headers=header
    ).json()
    by_table = {a["table"]: a for a in body["manifest"]["assets"]}
    assert by_table["gold.b"]["hidden_references"] == 2
    assert "sql" not in by_table["gold.b"]
    assert body["manifest"]["external_inputs"] == []
    assert body["manifest"]["hidden_external_inputs"] == 1
    assert body["manifest"]["edges"] == []
    assert body["manifest"]["hidden_edges"] == 1
    admin = client.get(
        f"/api/v1/environments/dev/project-versions/{version_id}", headers=AUTH_HEADER
    ).json()
    assert admin["manifest"]["edges"] == [["silver.a", "gold.b"]]
    assert admin["manifest"]["hidden_edges"] == 0


def test_adopted_tables_are_hidden_from_a_reader_over_rest(client: TestClient) -> None:
    """The deploy body carries `hidden_adopted` and no unreadable adopted names."""
    _setup(client)
    accepted = client.post(
        "/api/v1/environments/dev/deploys", headers=AUTH_HEADER, json={"repository": "analytics"}
    ).json()
    with Session(client.app.state.engine) as session:
        run = session.get(DeployRun, uuid.UUID(accepted["id"]))
        assert run is not None
        run.diff_json = {"added": [], "adopted": ["bronze.secret"]}
        session.commit()
    header = _reader(client)
    seen = client.get(f"/api/v1/environments/dev/deploys/{accepted['id']}", headers=header).json()
    assert seen["diff"]["adopted"] == []
    assert seen["hidden_adopted"] == 1
    listed = client.get("/api/v1/environments/dev/deploys", headers=header).json()
    assert listed[0]["hidden_adopted"] == 1
    full = client.get(
        f"/api/v1/environments/dev/deploys/{accepted['id']}", headers=AUTH_HEADER
    ).json()
    assert full["diff"]["adopted"] == ["bronze.secret"]
    assert full["hidden_adopted"] == 0


def test_a_deploy_body_carries_warnings_for_a_removed_kept_table(client: TestClient) -> None:
    """`warnings` is empty by default and names a removed asset whose table is kept."""
    _setup(client)
    accepted = client.post(
        "/api/v1/environments/dev/deploys", headers=AUTH_HEADER, json={"repository": "analytics"}
    ).json()
    url = f"/api/v1/environments/dev/deploys/{accepted['id']}"
    assert client.get(url, headers=AUTH_HEADER).json()["warnings"] == []
    with Session(client.app.state.engine) as session:
        run = session.get(DeployRun, uuid.UUID(accepted["id"]))
        assert run is not None
        run.diff_json = {"removed": ["silver.clean"]}
        env_id = session.execute(
            select(Environment.id).where(Environment.name == "dev")
        ).scalar_one()
        namespace = session.execute(
            select(Namespace).where(Namespace.environment_id == env_id, Namespace.path == "silver")
        ).scalar_one_or_none()
        if namespace is None:
            namespace = Namespace(
                id=uuid.uuid4(), environment_id=env_id, path="silver", properties={}
            )
            session.add(namespace)
            session.flush()
        session.add(
            Table(
                id=uuid.uuid4(),
                namespace_id=namespace.id,
                name="clean",
                table_type=TableType.ICEBERG,
                kept_run_id=uuid.uuid4(),
            )
        )
        session.commit()

    warnings = client.get(url, headers=AUTH_HEADER).json()["warnings"]

    assert len(warnings) == 1
    assert "`silver.clean`" in warnings[0]
    assert (
        client.get("/api/v1/environments/dev/deploys", headers=AUTH_HEADER).json()[0]["warnings"]
        == warnings
    )


def test_a_deploy_run_shows_its_checks(client: TestClient) -> None:
    """`checks` is the stored listing, and `null` for a run without checks."""
    _setup(client)
    plain = client.post(
        "/api/v1/environments/dev/deploys", headers=AUTH_HEADER, json={"repository": "analytics"}
    ).json()
    checked = client.post(
        "/api/v1/environments/dev/deploys", headers=AUTH_HEADER, json={"repository": "analytics"}
    ).json()
    listing = {"silver.clean": [{"check": "id_present", "on_failure": "warn"}]}
    with Session(client.app.state.engine) as session:
        run = session.get(DeployRun, uuid.UUID(checked["id"]))
        assert run is not None
        run.checks_json = listing
        session.commit()
    shown = client.get(f"/api/v1/environments/dev/deploys/{checked['id']}", headers=AUTH_HEADER)
    assert shown.json()["checks"] == listing
    bare = client.get(f"/api/v1/environments/dev/deploys/{plain['id']}", headers=AUTH_HEADER)
    assert bare.json()["checks"] is None
    listed = client.get("/api/v1/environments/dev/deploys", headers=AUTH_HEADER).json()
    assert {item["id"]: item["checks"] for item in listed} == {
        checked["id"]: listing,
        plain["id"]: None,
    }


def test_a_deploy_body_names_the_jobs_it_started(client: TestClient) -> None:
    """`job_starts` lists each job's outcome; empty for a deploy that starts no job."""
    _setup(client)
    plain = client.post(
        "/api/v1/environments/dev/deploys", headers=AUTH_HEADER, json={"repository": "analytics"}
    ).json()
    started = client.post(
        "/api/v1/environments/dev/deploys", headers=AUTH_HEADER, json={"repository": "analytics"}
    ).json()
    run_id, active_id = uuid.uuid4(), uuid.uuid4()
    with Session(client.app.state.engine) as session:
        run = session.get(DeployRun, uuid.UUID(started["id"]))
        assert run is not None
        run.status = DeployStatus.SUCCEEDED
        run.finished_at = db_now(session)
        run.job_starts_json = [
            {"job": "a", "outcome": "started", "run_id": str(run_id)},
            {"job": "b", "outcome": "waiting", "waits_for_run_id": str(active_id)},
            {
                "job": "c",
                "outcome": "not_started",
                "reason": "lacks_run",
                "message": "job `c` was not started: admin may not RUN in `dev`.",
            },
            {"job": "d", "outcome": "pending"},
        ]
        session.commit()

    body = client.get(f"/api/v1/environments/dev/deploys/{started['id']}", headers=AUTH_HEADER)

    empty = {"run_id": None, "waits_for_run_id": None, "reason": None, "message": None}
    assert body.json()["job_starts"] == [
        {**empty, "job": "a", "outcome": "started", "run_id": str(run_id)},
        {**empty, "job": "b", "outcome": "waiting", "waits_for_run_id": str(active_id)},
        {
            **empty,
            "job": "c",
            "outcome": "not_started",
            "reason": "lacks_run",
            "message": "job `c` was not started: admin may not RUN in `dev`.",
        },
        {**empty, "job": "d", "outcome": "pending"},
    ]
    bare = client.get(f"/api/v1/environments/dev/deploys/{plain['id']}", headers=AUTH_HEADER)
    assert bare.json()["job_starts"] == []


def _queue_deploy(client: TestClient) -> str:
    """Queue a deploy of `analytics` and return its id.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    str
        The run's id.
    """
    accepted = client.post(
        "/api/v1/environments/dev/deploys",
        headers=AUTH_HEADER,
        json={"repository": "analytics", "ref": "main"},
    )
    assert accepted.status_code == 202, accepted.text
    return accepted.json()["id"]


def _worker(client: TestClient, takes: str) -> None:
    """Register a worker whose heartbeat is current.

    Parameters
    ----------
    client
        The test client.
    takes
        What it takes (`all`, `interactive` or `pipeline`).
    """
    with Session(client.app.state.engine) as session:
        now = db_now(session)
        session.add(
            Worker(id="w", host="h", version="1", started_at=now, heartbeat_at=now, takes=takes)
        )
        session.commit()


def test_a_queued_deploy_says_why_it_waits_and_a_finished_one_says_nothing(
    client: TestClient,
) -> None:
    """The status route carries `waiting_for`; the history does not compute it."""
    _setup(client)
    deploy_id = _queue_deploy(client)
    url = f"/api/v1/environments/dev/deploys/{deploy_id}"

    queued = client.get(url, headers=AUTH_HEADER).json()["waiting_for"]
    assert queued == {
        "code": "no_worker",
        "message": (
            "No worker is running, so this deploy cannot start. Ask the operator to start a "
            "worker; on a local instance, start it again with 'lakana server start --local'."
        ),
        "workers": 0,
    }
    history = client.get("/api/v1/environments/dev/deploys", headers=AUTH_HEADER).json()
    assert history[0]["waiting_for"] is None
    with Session(client.app.state.engine) as session:
        run = session.get(DeployRun, uuid.UUID(deploy_id))
        assert run is not None
        run.status = DeployStatus.SUCCEEDED
        session.commit()
    assert client.get(url, headers=AUTH_HEADER).json()["waiting_for"] is None


def test_a_deploy_on_interactive_workers_only_has_no_worker_of_kind(client: TestClient) -> None:
    _setup(client)
    _worker(client, "interactive")
    deploy_id = _queue_deploy(client)
    body = client.get(f"/api/v1/environments/dev/deploys/{deploy_id}", headers=AUTH_HEADER).json()
    assert body["waiting_for"]["code"] == "no_worker_of_kind"
    assert "LAKANA_WORKER__TAKES=pipeline" in body["waiting_for"]["message"]


def test_a_deploy_with_a_free_worker_is_starting(client: TestClient) -> None:
    _setup(client)
    _worker(client, "all")
    deploy_id = _queue_deploy(client)
    body = client.get(f"/api/v1/environments/dev/deploys/{deploy_id}", headers=AUTH_HEADER).json()
    assert body["waiting_for"]["code"] == "starting"
