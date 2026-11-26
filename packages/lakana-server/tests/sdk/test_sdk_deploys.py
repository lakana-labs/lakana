"""`client.deploys` and friends against the in-process server."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import lakana
from lakana.errors import Conflict, DeployFailed, NotFound
from lakana_core.models import DeployRun, DeployStatus
from tests.server_test_helpers import create_environment


def test_create_get_list_and_wait_for_failure(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    """A run the worker fails surfaces as `DeployFailed` with the run attached."""
    create_environment(client, "dev")
    lakana_client.repositories.create("analytics", "https", "https://github.com/acme/analytics.git")
    accepted = lakana_client.deploys.create("dev", "analytics", kind="dry_run", ref="main")
    assert accepted.status == "queued"
    assert lakana_client.deploys.get("dev", accepted.id).repository == "analytics"
    assert [d.id for d in lakana_client.deploys.list("dev")] == [accepted.id]
    with Session(client.app.state.engine) as session:
        row = session.get(DeployRun, uuid.UUID(accepted.id))
        assert row is not None
        row.status = DeployStatus.FAILED
        row.error_message = "boom"
        session.commit()
    with pytest.raises(DeployFailed, match="boom") as info:
        lakana_client.deploys.wait("dev", accepted.id, timeout=5)
    assert info.value.deploy.status == "failed"


def test_wait_times_out(lakana_client: lakana.Client, client: TestClient) -> None:
    """A run nobody claims times out."""
    create_environment(client, "dev")
    lakana_client.repositories.create("analytics", "https", "https://github.com/acme/analytics.git")
    accepted = lakana_client.deploys.create("dev", "analytics")
    with pytest.raises(TimeoutError):
        lakana_client.deploys.wait("dev", accepted.id, timeout=0.3, poll_interval=0.1)


def test_read_resources(lakana_client: lakana.Client, client: TestClient) -> None:
    """Deployments, orphans and a missing project version."""
    create_environment(client, "dev")
    assert lakana_client.deployments.list("dev") == []
    assert lakana_client.orphans.list("dev").orphans == []
    with pytest.raises(NotFound):
        lakana_client.project_versions.get("dev", str(uuid.uuid4()))


def test_run_as_and_undeploy(lakana_client: lakana.Client, client: TestClient) -> None:
    """Bind, clear and undeploy through the SDK."""
    from tests.api.platform.test_deployments_run_as import _deploy

    _deploy(client)
    lakana_client.principals.create("etl", "SERVICE_ACCOUNT")
    assert lakana_client.deployments.set_run_as("dev", "analytics", "etl").run_as == "etl"
    assert lakana_client.deployments.list("dev")[0].run_as == "etl"
    assert lakana_client.deployments.clear_run_as("dev", "analytics").run_as is None
    (deployment,) = lakana_client.deployments.list("dev")
    with pytest.raises(Conflict, match="changed since you read it"):
        lakana_client.deployments.undeploy(
            "dev", "analytics", expected_project_version_id=str(uuid.uuid4())
        )
    lakana_client.deployments.undeploy(
        "dev", "analytics", expected_project_version_id=str(deployment.project_version_id)
    )
    assert lakana_client.deployments.list("dev") == []
    with pytest.raises(NotFound):
        lakana_client.deployments.undeploy("dev", "analytics")


def test_a_typed_deploy_carries_its_checks(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    """`Deploy.checks` is the listing the server stores; a run without checks reads `None`."""
    create_environment(client, "dev")
    lakana_client.repositories.create("analytics", "https", "https://github.com/acme/analytics.git")
    plain = lakana_client.deploys.create("dev", "analytics", kind="dry_run", ref="main")
    checked = lakana_client.deploys.create("dev", "analytics", kind="dry_run", ref="main")
    listing = {"silver.clean": [{"check": "id_present", "on_failure": "warn"}]}
    with Session(client.app.state.engine) as session:
        row = session.get(DeployRun, uuid.UUID(checked.id))
        assert row is not None
        row.checks_json = listing
        session.commit()
    assert lakana_client.deploys.get("dev", checked.id).checks == listing
    assert lakana_client.deploys.get("dev", plain.id).checks is None
