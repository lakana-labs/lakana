"""Tests for the principals resource of the platform API."""

from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADER


def _create(client: TestClient, name: str, kind: str = "USER") -> None:
    """Create a principal as the bootstrap administrator.

    Parameters
    ----------
    client
        The client.
    name
        The new principal's name.
    kind
        `USER` or `SERVICE_ACCOUNT`.
    """
    r = client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": kind})
    assert r.status_code == 201, r.text


def _grant(client: TestClient, **body: str) -> str:
    """Grant something on the instance as the bootstrap administrator.

    Parameters
    ----------
    client
        The client.
    **body
        The grant's fields besides `kind`.

    Returns
    -------
    str
        The grant's id.
    """
    r = client.post("/api/v1/grants", headers=AUTH_HEADER, json={**body, "kind": "INSTANCE"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _token(client: TestClient, name: str) -> dict[str, str]:
    """Issue a token for a principal.

    Parameters
    ----------
    client
        The client.
    name
        The principal.

    Returns
    -------
    dict of str to str
        Its Authorization header.
    """
    r = client.post(f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": "t"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_held_admin_marks_current_and_former_administrators(client: TestClient) -> None:
    """A former administrator stays marked, so the interface offers the operator's command."""
    for name in ("carol", "dave", "erin"):
        _create(client, name)
    _grant(client, principal="carol", role="ADMIN")
    revoked = client.delete(
        f"/api/v1/grants/{_grant(client, principal='dave', role='ADMIN')}", headers=AUTH_HEADER
    )
    assert revoked.status_code == 204, revoked.text
    listed = client.get("/api/v1/principals", headers=AUTH_HEADER).json()
    held = {row["name"]: row["held_admin"] for row in listed}
    assert held == {"admin": True, "carol": True, "dave": True, "erin": False}
    for name, expected in held.items():
        one = client.get(f"/api/v1/principals/{name}", headers=AUTH_HEADER).json()
        assert one["held_admin"] is expected


def test_held_admin_is_only_for_administrators(client: TestClient) -> None:
    """Who is or was an administrator is not shown to someone who only manages principals."""
    _create(client, "hr-bot", "SERVICE_ACCOUNT")
    _grant(client, principal="hr-bot", action="MANAGE_PRINCIPAL")
    headers = _token(client, "hr-bot")
    listed = client.get("/api/v1/principals", headers=headers)
    assert listed.status_code == 200, listed.text
    assert {row["held_admin"] for row in listed.json()} == {None}
    assert client.get("/api/v1/principals/admin", headers=headers).json()["held_admin"] is None


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
        "held_admin": False,
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
