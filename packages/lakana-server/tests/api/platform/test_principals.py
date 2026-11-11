"""Tests for the principals resource of the platform API."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADER


def test_create_and_get_principal(client: TestClient) -> None:
    """A created principal is fetchable by name and starts active."""
    response = client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
    )
    assert response.status_code == 201, response.text
    assert response.json() == {
        "name": "ci-bot",
        "type": "SERVICE_ACCOUNT",
        "is_active": True,
        "picture": None,
    }

    fetched = client.get("/api/v1/principals/ci-bot", headers=AUTH_HEADER)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "ci-bot"


def test_create_principal_rejects_a_duplicate_name(client: TestClient) -> None:
    """A second principal with the same name is a 409."""
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
    )
    response = client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
    )
    assert response.status_code == 409


def test_get_principal_404s_when_missing(client: TestClient) -> None:
    """Fetching a name nobody created is a 404."""
    response = client.get("/api/v1/principals/nobody", headers=AUTH_HEADER)
    assert response.status_code == 404


def test_list_principals_includes_the_bootstrap_admin(client: TestClient) -> None:
    """The bootstrap admin, created at startup, appears in the list."""
    response = client.get("/api/v1/principals", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert "admin" in {p["name"] for p in response.json()}


def test_delete_principal_deactivates_rather_than_removes(client: TestClient) -> None:
    """Deletion is a 204 that sets is_active=False, not a hard delete."""
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
    )
    response = client.delete("/api/v1/principals/ci-bot", headers=AUTH_HEADER)
    assert response.status_code == 204

    fetched = client.get("/api/v1/principals/ci-bot", headers=AUTH_HEADER)
    assert fetched.json()["is_active"] is False


def test_a_lone_surrogate_in_a_body_is_a_422_that_does_not_echo_it(client: TestClient) -> None:
    """A-I5: platform-wide, older than the artifact store."""
    refused = client.post(
        "/api/v1/principals",
        headers={**AUTH_HEADER, "Content-Type": "application/json"},
        content=b'{"name": "ok", "type": "a\\ud800b"}',
    )
    assert refused.status_code == 422, refused.text
    assert [tuple(error["loc"]) for error in refused.json()["detail"]] == [("body", "type")]
    assert "input" not in refused.text
