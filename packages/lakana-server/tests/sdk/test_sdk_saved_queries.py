"""The SDK's saved queries against the real app, in process."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import lakana
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, person


def _sdk(client: TestClient, header: dict[str, str]) -> lakana.Client:
    """Build an SDK client for the principal whose Authorization header this is.

    Parameters
    ----------
    client
        The in-process test client the SDK sends through.
    header
        The principal's Authorization header.

    Returns
    -------
    lakana.Client
        The SDK client.
    """
    token = header["Authorization"].removeprefix("Bearer ")
    return lakana.Client(url="http://testserver", token=token, http_client=client)


def _no_role(client: TestClient, name: str) -> dict[str, str]:
    """Create a service account with no grant, and return its header.

    Parameters
    ----------
    client
        Test client for the API.
    name
        The principal's name.

    Returns
    -------
    dict of str to str
        The principal's Authorization header.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "SERVICE_ACCOUNT"}
    )
    token = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_save_share_and_delete_against_the_real_app(client: TestClient) -> None:
    """Alice shares a query; bob, who may submit, reads it; carol, who may not, gets NotFound."""
    create_environment(client, "dev")
    alice = _sdk(client, person(client, "alice", "ANALYST"))
    bob = _sdk(client, person(client, "bob", "ANALYST"))
    carol = _sdk(client, _no_role(client, "carol"))

    saved = alice.saved_queries.create("dev", "Orders", "SELECT 1", visibility="everyone")

    assert (saved.owner, saved.sql, saved.actions) == ("alice", "SELECT 1", ("update", "delete"))
    assert [q.id for q in bob.saved_queries.list("dev").items] == [saved.id]
    assert [q.id for q in bob.saved_queries.iter()] == [saved.id]
    read = bob.saved_queries.get("dev", saved.id)
    assert (read.sql, read.actions, read.former_owner) == ("SELECT 1", (), False)
    with pytest.raises(lakana.NotFound):
        carol.saved_queries.get("dev", saved.id)
    assert carol.saved_queries.list().items == []

    renamed = alice.saved_queries.update(
        "dev", saved.id, name="Orders 2", sql="SELECT 2", visibility="private"
    )
    assert (renamed.name, renamed.visibility) == ("Orders 2", "private")
    assert bob.saved_queries.list().items == []
    alice.saved_queries.delete("dev", saved.id)
    assert alice.saved_queries.list("dev").items == []


def test_an_admin_sees_a_private_query_without_sql_against_the_real_app(
    client: TestClient,
) -> None:
    """The administrator reads the name and the owner of a private query, and `sql` is `None`."""
    create_environment(client, "dev")
    alice = _sdk(client, person(client, "alice", "ANALYST"))
    admin = _sdk(client, AUTH_HEADER)
    saved = alice.saved_queries.create("dev", "Mine", "SELECT secret FROM t")

    seen = admin.saved_queries.get("dev", saved.id)
    listed = admin.saved_queries.list().items

    assert (seen.name, seen.owner, seen.sql, seen.actions) == ("Mine", "alice", None, ("delete",))
    assert [(q.id, q.sql) for q in listed] == [(saved.id, None)]
    admin.saved_queries.delete("dev", saved.id)
    assert alice.saved_queries.list("dev").items == []
