"""Tests for the access flags of `GET /api/v1/whoami`: `may_deploy`."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.platform.test_whoami import _new_person
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment


def _grant(client: TestClient, body: dict[str, str]) -> None:
    """Create a grant for kim in `dev`.

    Parameters
    ----------
    client
        The test client.
    body
        The role or action of the grant.
    """
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "kim", "kind": "ENVIRONMENT", "environment": "dev", **body},
    )
    assert granted.status_code == 201, granted.text


def test_whoami_says_may_deploy_for_a_deploy_grant(client: TestClient) -> None:
    create_environment(client, "dev")
    header = _new_person(client, "kim")
    assert client.get("/api/v1/whoami", headers=header).json()["may_deploy"] is False
    _grant(client, {"role": "DATA_ENGINEER"})
    assert client.get("/api/v1/whoami", headers=header).json()["may_deploy"] is False
    _grant(client, {"action": "DEPLOY"})
    body = client.get("/api/v1/whoami", headers=header).json()
    assert (body["admin"], body["has_access"], body["may_deploy"]) == (False, True, True)


def test_an_administrator_may_deploy_once_an_environment_exists(client: TestClient) -> None:
    assert client.get("/api/v1/whoami", headers=AUTH_HEADER).json()["may_deploy"] is False
    create_environment(client, "dev")
    assert client.get("/api/v1/whoami", headers=AUTH_HEADER).json()["may_deploy"] is True
