"""`POST …/tables/{name}/rebuild`: drop a project table and queue a run of its asset."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.catalog.rebuild import NOT_BUILT_BY_A_PROJECT
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_table, deploy_file

URL = "/api/v1/environments/dev/namespaces/silver/tables/clean/rebuild"
REASON = "the source was corrected upstream"


def _deployed_table(client: TestClient) -> None:
    """Create `dev.silver.clean` and deploy `analytics`, whose one SQL asset builds it.

    Parameters
    ----------
    client
        Test client for the API.
    """
    create_environment(client)
    create_table(client, "dev", "silver", "clean")
    with Session(client.app.state.engine) as session:
        deploy_file(session, "dev", "silver.clean", "SELECT 1")


def test_rebuild_answers_202_with_the_run(client: TestClient) -> None:
    _deployed_table(client)

    response = client.post(URL, json={"reason": REASON}, headers=AUTH_HEADER)

    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"table", "run_id", "repository", "path"}
    assert (body["table"], body["repository"], body["path"]) == (
        "silver.clean",
        "analytics",
        "assets/silver/clean.sql",
    )
    run = client.get(f"/api/v1/environments/dev/runs/{body['run_id']}", headers=AUTH_HEADER)
    assert run.status_code == 200, run.text
    gone = client.get(
        "/api/v1/environments/dev/namespaces/silver/tables/clean", headers=AUTH_HEADER
    )
    assert gone.status_code == 404


def test_rebuild_of_an_unbuilt_table_is_409_pointing_to_drop(client: TestClient) -> None:
    create_environment(client)
    create_table(client, "dev", "silver", "clean")

    response = client.post(URL, json={"reason": REASON}, headers=AUTH_HEADER)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == NOT_BUILT_BY_A_PROJECT.format(
        name="silver.clean", environment="dev"
    )


def test_a_short_reason_is_422(client: TestClient) -> None:
    _deployed_table(client)

    response = client.post(URL, json={"reason": "too short"}, headers=AUTH_HEADER)

    assert response.status_code == 422, response.text
    assert "The rebuild reason" in response.text


def test_an_unknown_body_field_is_422(client: TestClient) -> None:
    _deployed_table(client)

    response = client.post(URL, json={"reason": REASON, "purge": True}, headers=AUTH_HEADER)

    assert response.status_code == 422, response.text
