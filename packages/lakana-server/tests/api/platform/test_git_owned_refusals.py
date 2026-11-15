"""The platform routes refuse to drop, or to create under, what a deployed project defines (409)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.catalog.git_owned import (
    GIT_OWNED_ARTIFACT_DROP,
    GIT_OWNED_CREATE,
    GIT_OWNED_DROP,
)
from lakana_core.models import EnvironmentDeployment, JobVersion
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import (
    create_environment,
    create_namespace,
    create_table,
    deploy_file,
    upload_artifact,
)


def _session(client: TestClient) -> Session:
    """Open a session on the app's database.

    Parameters
    ----------
    client
        Test client for the API.

    Returns
    -------
    Session
        A new session; the caller closes it.
    """
    return Session(client.app.state.engine)


def _deploy_artifact(client: TestClient, dotted: str, path: str) -> None:
    """Deploy `analytics` to `dev` with one Python asset that produces the artifact `dotted`.

    Parameters
    ----------
    client
        Test client for the API.
    dotted
        The artifact's dotted name.
    path
        The asset's file.
    """
    with _session(client) as session:
        deploy_file(session, "dev", dotted, "")
        version = session.scalars(
            select(JobVersion).join(
                EnvironmentDeployment,
                EnvironmentDeployment.current_job_version_id == JobVersion.id,
            )
        ).one()
        version.manifest_json = {
            "assets": [{"table": dotted, "path": path, "kind": "python", "output": "artifact"}]
        }
        session.commit()


def test_delete_of_a_project_table_is_409_naming_the_file(client: TestClient) -> None:
    create_environment(client)
    create_table(client, "dev", "silver", "clean")
    with _session(client) as session:
        deploy_file(session, "dev", "silver.clean", "SELECT 1")

    response = client.delete(
        "/api/v1/environments/dev/namespaces/silver/tables/clean", headers=AUTH_HEADER
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == GIT_OWNED_DROP.format(
        name="silver.clean",
        repository="analytics",
        path="assets/silver/clean.sql",
        environment="dev",
    )
    kept = client.get(
        "/api/v1/environments/dev/namespaces/silver/tables/clean", headers=AUTH_HEADER
    )
    assert kept.status_code == 200


def test_delete_of_a_produced_artifact_is_409(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    upload_artifact(client, "dev", "models.churn", {"m": b"one"})
    _deploy_artifact(client, "models.churn", "assets/churn.py")
    base = "/api/v1/environments/dev/namespaces/models/artifacts/churn"

    response = client.delete(base, headers=AUTH_HEADER)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == GIT_OWNED_ARTIFACT_DROP.format(
        name="models.churn", repository="analytics", path="assets/churn.py", environment="dev"
    )
    assert client.get(base, headers=AUTH_HEADER).status_code == 200


def test_create_external_under_a_declared_name_is_409(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "raw")
    with _session(client) as session:
        deploy_file(session, "dev", "raw.events", "SELECT 1")

    response = client.post(
        "/api/v1/environments/dev/namespaces/raw/tables/external",
        json={"name": "events", "location_pattern": "raw/events/*.parquet", "format": "parquet"},
        headers=AUTH_HEADER,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == GIT_OWNED_CREATE.format(
        name="raw.events", repository="analytics", path="assets/raw/events.sql", environment="dev"
    )
