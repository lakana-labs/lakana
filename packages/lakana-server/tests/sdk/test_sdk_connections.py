"""`client.connections`, `client.secrets.bind` to a connection and the audit `kind` filter.

Against the in-process server.
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

import lakana
from lakana._models import Connection, DeclaringAsset
from lakana.errors import Conflict, NotFound, PermissionDenied, ValidationError

_VALUE = "s3cr3t-value"


def test_connection_lifecycle(lakana_client: lakana.Client, storage_root: str) -> None:
    """Create, get, update the settings, list and delete; the model holds no secret value."""
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    connections = lakana_client.connections

    created = connections.create(
        "dev",
        "crm-db",
        kind="postgresql",
        address={"host": "db.example.com", "database": "crm"},
        settings={"user": "reader"},
        secret_values={"password": _VALUE},
    )

    assert isinstance(created, Connection)
    assert (created.name, created.kind, created.secret) == (
        "crm-db",
        "postgresql",
        "crm-db-connection",
    )
    assert created.address == {"host": "db.example.com", "port": 5432, "database": "crm"}
    assert {field.name for field in dataclasses.fields(Connection)} == {
        "name",
        "kind",
        "address",
        "settings",
        "secret",
        "declared_by",
        "created_by",
        "created_at",
        "updated_at",
        "environment",
        "last_test",
        "actions",
    }
    assert (created.environment, created.last_test) == ("dev", None)
    assert created.actions == ("test", "update", "delete", "name_secret")
    assert _VALUE not in repr(created)
    assert connections.get("dev", "crm-db").created_at.tzinfo is not None
    updated = connections.update("dev", "crm-db", settings={"user": "loader"})
    assert updated.settings["user"] == "loader"
    page = connections.list("dev")
    assert ([c.name for c in page.items], page.next_cursor) == (["crm-db"], None)
    assert [c.name for c in connections.iter("dev")] == ["crm-db"]
    cleared = connections.update("dev", "crm-db", clear=["secret"])
    assert cleared.secret is None
    with pytest.raises(Conflict, match="already has a connection `crm-db`"):
        connections.create("dev", "crm-db", kind="directory", address={"root": "r", "path": "p"})
    connections.delete("dev", "crm-db")
    with pytest.raises(NotFound, match="`lakana connection list --env dev` lists them"):
        connections.get("dev", "crm-db")


def test_iter_walks_every_page(lakana_client: lakana.Client, storage_root: str) -> None:
    """`list` returns one page; `iter` follows `next_cursor` to the end."""
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    for name in ("a", "b", "c"):
        lakana_client.connections.create(
            "dev", name, kind="directory", address={"root": "r", "path": name}
        )

    first = lakana_client.connections.list("dev", limit=2)

    assert len(first.items) == 2
    assert first.next_cursor is not None
    assert sorted(c.name for c in lakana_client.connections.iter("dev", page_size=1)) == [
        "a",
        "b",
        "c",
    ]


def test_validation_and_permission_errors(
    lakana_client: lakana.Client, client: TestClient, storage_root: str
) -> None:
    """The server's 422 and 403 arrive as the SDK's errors, with their text."""
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    with pytest.raises(ValidationError, match=r"address\.base_url"):
        lakana_client.connections.create(
            "dev", "api", kind="http", address={"base_url": "http://api.example.com"}
        )
    lakana_client.principals.create("bob", "USER")
    token = lakana_client.tokens.issue("bob", "laptop").token
    with (
        lakana.Client(url="http://testserver", token=token, http_client=client) as bob,
        pytest.raises(PermissionDenied),
    ):
        bob.connections.create("dev", "x", kind="directory", address={"root": "r", "path": "p"})


def test_a_secret_is_bound_to_a_connection_and_its_events_are_listed_by_kind(
    lakana_client: lakana.Client, storage_root: str
) -> None:
    """`secrets.bind(env, name, "connection", conn)` and `audit_events.list(kind=...)`."""
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    lakana_client.connections.create(
        "dev",
        "crm-db",
        kind="postgresql",
        address={"host": "db.example.com", "database": "crm"},
        settings={"user": "reader"},
    )
    lakana_client.secrets.create("dev", "crm-pw", {"password": "pw"})

    bound = lakana_client.secrets.bind("dev", "crm-pw", "connection", "crm-db")

    assert (bound.use, bound.use_target) == ("connection", "crm-db")
    assert lakana_client.connections.get("dev", "crm-db").secret == "crm-pw"
    events = lakana_client.audit_events.list(kind="connection").items
    assert [event.event_type for event in events] == ["CONNECTION_CREATED"]
    assert [e.event_type for e in lakana_client.audit_events.iter(kind="connection")] == [
        "CONNECTION_CREATED"
    ]
    assert DeclaringAsset("analytics", "customers").asset == "customers"
