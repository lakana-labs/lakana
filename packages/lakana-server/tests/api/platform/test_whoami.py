"""Tests for `GET /api/v1/whoami` and `PATCH /api/v1/whoami`."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADER


def test_whoami_describes_a_pat_caller(client: TestClient) -> None:
    """A PAT caller gets its principal's name and type, and no task-token scope."""
    response = client.get("/api/v1/whoami", headers=AUTH_HEADER)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "admin",
        "type": "USER",
        "task_token": None,
        "ci_token": None,
        "language": None,
    }


def test_whoami_describes_a_task_token_callers_scope(client: TestClient) -> None:
    """A task-token caller also gets the token's environment and asset scope."""
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.sales"],
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text

    response = client.get(
        "/api/v1/whoami", headers={"Authorization": f"Bearer {minted.json()['token']}"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "admin",
        "type": "USER",
        "task_token": {"environment": "dev", "asset_scope": ["gold.sales"]},
        "ci_token": None,
        "language": None,
    }


def test_whoami_requires_authentication(client: TestClient) -> None:
    """No credential is a 401, like every other platform route."""
    assert client.get("/api/v1/whoami").status_code == 401


def _task_token_header(client: TestClient) -> dict[str, str]:
    """Mint a task token for the admin and return its `Authorization` header.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    dict
        The header.
    """
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.sales"],
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text
    return {"Authorization": f"Bearer {minted.json()['token']}"}


def test_a_person_sets_their_language_and_whoami_shows_it(client: TestClient) -> None:
    """The tag is stored canonically, shown by whoami and removed by null."""
    response = client.patch("/api/v1/whoami", headers=AUTH_HEADER, json={"language": "fr-ch"})
    assert (response.status_code, response.json()["language"]) == (200, "fr-CH")
    assert client.get("/api/v1/whoami", headers=AUTH_HEADER).json()["language"] == "fr-CH"
    reset = client.patch("/api/v1/whoami", headers=AUTH_HEADER, json={"language": None})
    assert reset.json()["language"] is None


def test_a_task_token_may_not_set_a_language(client: TestClient) -> None:
    """A task token is refused with the token to use instead."""
    response = client.patch(
        "/api/v1/whoami", headers=_task_token_header(client), json={"language": "fr"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == (
        "a task or CI token cannot change its principal's profile; use a personal access token"
    )


def test_a_language_that_is_not_a_tag_is_422(client: TestClient) -> None:
    """A value outside the tag subset is refused with the tag rule."""
    response = client.patch("/api/v1/whoami", headers=AUTH_HEADER, json={"language": "French"})
    assert response.status_code == 422
    assert "is not a language tag Lakana takes" in response.json()["detail"]


def test_the_language_field_must_be_sent(client: TestClient) -> None:
    """An empty body changes nothing and is refused."""
    assert client.patch("/api/v1/whoami", headers=AUTH_HEADER, json={}).status_code == 422
