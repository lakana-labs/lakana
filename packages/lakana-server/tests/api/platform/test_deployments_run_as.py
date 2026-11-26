"""Run principal binding and undeploy over REST."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.models import (
    DeployRun,
    DeployStatus,
    EnvironmentDeployment,
    JobVersion,
    Run,
    RunKind,
    RunStatus,
)
from tests.api.platform.test_deploys import _insert_job_version, _setup, _token_for
from tests.conftest import AUTH_HEADER

_BASE = "/api/v1/environments/dev/deployments/analytics"


def _deploy(client: TestClient) -> uuid.UUID:
    """Create dev, the repository and a current deployment of one project version.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    uuid.UUID
        The project version id.
    """
    _setup(client)
    version_id = _insert_job_version(client, "dev")
    with Session(client.app.state.engine) as session:
        version = session.get(JobVersion, version_id)
        assert version is not None
        session.add(
            EnvironmentDeployment(
                environment_id=version.environment_id,
                repository_id=version.repository_id,
                current_job_version_id=version.id,
            )
        )
        session.commit()
    return version_id


def test_bind_list_and_clear(client: TestClient) -> None:
    """PUT binds a service account, the list shows it, DELETE clears it."""
    _deploy(client)
    _token_for(client, "etl")
    bound = client.put(f"{_BASE}/run-as", headers=AUTH_HEADER, json={"principal": "etl"})
    assert bound.status_code == 200, bound.text
    assert bound.json()["run_as"] == "etl"
    listed = client.get("/api/v1/environments/dev/deployments", headers=AUTH_HEADER).json()
    assert listed[0]["run_as"] == "etl"
    cleared = client.delete(f"{_BASE}/run-as", headers=AUTH_HEADER)
    assert cleared.status_code == 200
    assert cleared.json()["run_as"] is None


def test_binding_errors(client: TestClient) -> None:
    """A user and an unknown principal answer alike (404), a bad body 422, a non-manager 403."""
    _deploy(client)
    user = client.put(f"{_BASE}/run-as", headers=AUTH_HEADER, json={"principal": "admin"})
    ghost = client.put(f"{_BASE}/run-as", headers=AUTH_HEADER, json={"principal": "ghost"})
    assert (user.status_code, ghost.status_code) == (404, 404)
    assert user.json()["detail"].replace("'admin'", "X") == ghost.json()["detail"].replace(
        "'ghost'", "X"
    )
    assert "not found or not a service account" in user.json()["detail"]
    assert (
        client.put(f"{_BASE}/run-as", headers=AUTH_HEADER, json={"principal": ""}).status_code
        == 422
    )
    assert client.put(f"{_BASE}/run-as", headers=AUTH_HEADER, json={}).status_code == 422
    outsider = _token_for(client, "outsider")
    assert (
        client.put(f"{_BASE}/run-as", headers=outsider, json={"principal": "outsider"}).status_code
        == 403
    )


def test_undeploy_and_its_conflict(client: TestClient) -> None:
    """DELETE removes the deployment (204); an active run makes it 409; a second DELETE is 404."""
    version_id = _deploy(client)
    with Session(client.app.state.engine) as session:
        version = session.get(JobVersion, version_id)
        assert version is not None
        run = Run(
            id=uuid.uuid4(),
            environment_id=version.environment_id,
            repository_id=version.repository_id,
            job_version_id=version.id,
            kind=RunKind.FULL,
            status=RunStatus.QUEUED,
            selection_json={},
        )
        session.add(run)
        session.commit()
        run_id = run.id
    refused = client.delete(_BASE, headers=AUTH_HEADER)
    assert refused.status_code == 409
    assert str(run_id) in refused.json()["detail"]
    with Session(client.app.state.engine) as session:
        stored = session.get(Run, run_id)
        assert stored is not None
        stored.status = RunStatus.CANCELLED
        session.commit()
    assert client.delete(_BASE, headers=AUTH_HEADER).status_code == 204
    assert client.get("/api/v1/environments/dev/deployments", headers=AUTH_HEADER).json() == []
    assert client.delete(_BASE, headers=AUTH_HEADER).status_code == 404
    assert client.delete("/api/v1/repositories/analytics", headers=AUTH_HEADER).status_code == 204


def test_undeploy_is_refused_while_a_deploy_is_queued(client: TestClient) -> None:
    """409 with a sentence naming the queued deploy; once it has ended, the DELETE goes."""
    _deploy(client)
    queued = client.post(
        "/api/v1/environments/dev/deploys",
        headers=AUTH_HEADER,
        json={"repository": "analytics", "kind": "deploy", "ref": "main"},
    )
    assert queued.status_code == 202, queued.text
    deploy_id = queued.json()["id"]
    refused = client.delete(_BASE, headers=AUTH_HEADER)
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        f"Deploy {deploy_id} of 'analytics' is queued in 'dev'; wait for it to finish before "
        "undeploying."
    )
    with Session(client.app.state.engine) as session:
        run = session.get(DeployRun, uuid.UUID(deploy_id))
        assert run is not None
        run.status = DeployStatus.FAILED
        session.commit()
    assert client.delete(_BASE, headers=AUTH_HEADER).status_code == 204


def test_undeploy_takes_the_version_the_caller_saw(client: TestClient) -> None:
    """Optional: another version is 409, a repeated or malformed one 422, the right one 204."""
    version_id = _deploy(client)
    other = uuid.uuid4()
    changed = client.delete(
        _BASE, headers=AUTH_HEADER, params={"expected_project_version_id": str(other)}
    )
    assert changed.status_code == 409
    assert changed.json()["detail"] == (
        "The deployment of 'analytics' in 'dev' changed since you read it: it is no longer "
        f"project version {other}. Look at it again before undeploying."
    )
    repeated = client.delete(
        f"{_BASE}?expected_project_version_id={version_id}&expected_project_version_id={other}",
        headers=AUTH_HEADER,
    )
    assert repeated.status_code == 422
    assert "at most once" in repeated.text
    malformed = client.delete(
        _BASE, headers=AUTH_HEADER, params={"expected_project_version_id": "v1"}
    )
    assert malformed.status_code == 422
    assert client.get("/api/v1/environments/dev/deployments", headers=AUTH_HEADER).json() != []
    seen = client.delete(
        _BASE, headers=AUTH_HEADER, params={"expected_project_version_id": str(version_id)}
    )
    assert seen.status_code == 204
    assert client.get("/api/v1/environments/dev/deployments", headers=AUTH_HEADER).json() == []


def test_undeploy_needs_deploy(client: TestClient) -> None:
    """A principal without DEPLOY is refused."""
    _deploy(client)
    assert client.delete(_BASE, headers=_token_for(client, "nobody")).status_code == 403


def test_binding_body_is_explicit_fields_only(client: TestClient) -> None:
    """No mass assignment: unknown keys (run context, break-glass, ids) are refused."""
    _deploy(client)
    _token_for(client, "etl")
    for extra in ({"run_context": "task"}, {"break_glass_reason": "x"}, {"task_run_id": "1"}):
        body = {"principal": "etl", **extra}
        assert client.put(f"{_BASE}/run-as", headers=AUTH_HEADER, json=body).status_code == 422


def test_binding_denial_does_not_disclose_existence(client: TestClient) -> None:
    """Without MANAGE_ENVIRONMENT, an unknown environment and a known one both answer 403."""
    _deploy(client)
    outsider = _token_for(client, "outsider")
    known = client.put(f"{_BASE}/run-as", headers=outsider, json={"principal": "x"})
    unknown = client.put(
        "/api/v1/environments/nope/deployments/ghost/run-as",
        headers=outsider,
        json={"principal": "x"},
    )
    assert known.status_code == unknown.status_code == 403
    assert client.delete(f"{_BASE}/run-as", headers=outsider).status_code == 403
    gone = client.delete("/api/v1/environments/nope/deployments/ghost", headers=outsider)
    assert gone.status_code == 403
