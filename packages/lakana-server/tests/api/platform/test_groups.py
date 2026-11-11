"""Tests for the groups resource of the platform API."""

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, SecurableKind
from lakana_core.models import Grant, Principal
from tests.conftest import AUTH_HEADER


def test_create_group_and_add_member(client: TestClient) -> None:
    """A created group accepts a member and lists it."""
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "alice", "type": "USER"},
    )
    create = client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "engineers"})
    assert create.status_code == 201, create.text

    add = client.put("/api/v1/groups/engineers/members/alice", headers=AUTH_HEADER)
    assert add.status_code in (200, 204)

    listed = client.get("/api/v1/groups", headers=AUTH_HEADER)
    assert {g["name"] for g in listed.json()} == {"engineers"}


def test_list_members_returns_principals_sorted_by_name(client: TestClient) -> None:
    """`GET /api/v1/groups/{name}/members` lists each member as a principal."""
    client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "engineers"})
    for name in ("bob", "alice"):
        client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
        client.put(f"/api/v1/groups/engineers/members/{name}", headers=AUTH_HEADER)

    response = client.get("/api/v1/groups/engineers/members", headers=AUTH_HEADER)

    assert response.status_code == 200, response.text
    assert response.json() == [
        {"name": "alice", "type": "USER", "is_active": True, "picture": None},
        {"name": "bob", "type": "USER", "is_active": True, "picture": None},
    ]


def test_list_members_404s_for_an_unknown_group(client: TestClient) -> None:
    """Listing the members of a group that doesn't exist is a 404."""
    response = client.get("/api/v1/groups/nobody/members", headers=AUTH_HEADER)
    assert response.status_code == 404, response.text


def test_remove_member_404s_when_not_a_member(client: TestClient) -> None:
    """Removing a non-member is a 404."""
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "alice", "type": "USER"},
    )
    client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "engineers"})
    response = client.delete("/api/v1/groups/engineers/members/alice", headers=AUTH_HEADER)
    assert response.status_code == 404


def test_delete_group(client: TestClient) -> None:
    """A deleted group no longer appears in the list."""
    client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "engineers"})
    response = client.delete("/api/v1/groups/engineers", headers=AUTH_HEADER)
    assert response.status_code == 204
    assert client.get("/api/v1/groups", headers=AUTH_HEADER).json() == []


def test_delete_group_with_an_active_grant_is_409(client: TestClient, storage_root: str) -> None:
    """A group holding an active grant cannot be deleted through the API.

    Before this fix, deleting a group cascaded away its grant with no
    audit trail; the API must now refuse with 409, and the group must
    still be listed afterward.
    """
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={"name": "dev", "storage": {"scheme": "file", "root": storage_root}},
    )
    client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "analysts"})
    grant = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "group": "analysts",
            "role": "ANALYST",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert grant.status_code == 201, grant.text

    response = client.delete("/api/v1/groups/analysts", headers=AUTH_HEADER)

    assert response.status_code == 409
    remaining = client.get("/api/v1/groups", headers=AUTH_HEADER).json()
    assert "analysts" in [g["name"] for g in remaining]


def _holder_of(client: TestClient, action: Action) -> dict[str, str]:
    """Create the principal `bob` with a token and one single-action grant at instance scope.

    Parameters
    ----------
    client
        The test client of the app; its engine receives the grant row.
    action
        The single action to grant at instance scope.

    Returns
    -------
    dict[str, str]
        The `Authorization` header carrying bob's token.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "SERVICE_ACCOUNT"}
    )
    issued = client.post("/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "t"})
    with Session(client.app.state.engine) as session:
        bob = session.execute(select(Principal).where(Principal.name == "bob")).scalar_one()
        session.add(
            Grant(
                id=uuid.uuid4(),
                principal_id=bob.id,
                action=action,
                kind=SecurableKind.INSTANCE,
                granted_by=bob.id,
                granted_at=datetime.now(UTC),
            )
        )
        session.commit()
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def test_manage_group_as_a_single_action_is_a_403_on_every_route(client: TestClient) -> None:
    header = _holder_of(client, Action.MANAGE_GROUP)
    assert (
        client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "team"}).status_code == 201
    )
    answers = [
        client.post("/api/v1/groups", headers=header, json={"name": "mine"}),
        client.get("/api/v1/groups", headers=header),
        client.put("/api/v1/groups/team/members/bob", headers=header),
        client.get("/api/v1/groups/team/members", headers=header),
        client.delete("/api/v1/groups/team/members/bob", headers=header),
        client.delete("/api/v1/groups/team", headers=header),
    ]
    for answer in answers:
        assert answer.status_code == 403, (answer.request.method, str(answer.request.url))
        assert "MANAGE_GROUP needs the ADMIN role on the instance" in answer.text
    assert client.put("/api/v1/groups/team/members/bob", headers=AUTH_HEADER).status_code == 204
