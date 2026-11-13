"""Tests for the row-filters resource of the platform API."""

from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from lakana_core.db import create_engine
from lakana_core.models import RowFilter
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import (
    create_environment,
    create_table,
    create_table_with_fields,
    rename_fields,
)


def test_create_a_row_filter_and_list_it_for_the_principal(client: TestClient) -> None:
    """A created row filter shows up when listing that principal's filters."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    created = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "id > 5",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["expression"] == "id > 5"
    assert body["namespace"] == "gold"

    listed = client.get(
        "/api/v1/row-filters", headers=AUTH_HEADER, params={"principal_name": "alice"}
    )
    assert listed.status_code == 200
    assert [f["id"] for f in listed.json()] == [body["id"]]


def test_create_requires_exactly_one_of_role_or_action(client: TestClient) -> None:
    """Passing neither principal nor group is a 422."""
    response = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "TRUE",
        },
    )
    assert response.status_code == 422


def test_create_rejects_an_expression_outside_the_restricted_subset(client: TestClient) -> None:
    """An out-of-grammar expression is a 422, not stored."""
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    response = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "(SELECT 1)",
        },
    )
    assert response.status_code == 422


def test_list_by_table_requires_all_three_fields_together(client: TestClient) -> None:
    """Listing by table needs environment, namespace, and table all given."""
    response = client.get("/api/v1/row-filters", headers=AUTH_HEADER, params={"environment": "dev"})
    assert response.status_code == 422


def test_list_by_table_is_an_exact_match(client: TestClient) -> None:
    """Listing by table finds only the exact environment/namespace/table given."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "TRUE",
        },
    )
    matching = client.get(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        params={"environment": "dev", "namespace": "gold", "table": "customers"},
    )
    assert len(matching.json()) == 1
    different_table = client.get(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        params={"environment": "dev", "namespace": "gold", "table": "orders"},
    )
    assert different_table.json() == []


def test_list_row_filters_for_a_group(client: TestClient) -> None:
    """Listing by group_name shows that group's filters."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "admins"})
    client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "group": "admins",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "TRUE",
        },
    )
    listed = client.get("/api/v1/row-filters", headers=AUTH_HEADER, params={"group_name": "admins"})
    assert len(listed.json()) == 1


def test_revoke_a_row_filter(client: TestClient) -> None:
    """A revoked row filter no longer appears when listing the principal's filters."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    created = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "TRUE",
        },
    )
    row_filter_id = created.json()["id"]

    revoked = client.delete(f"/api/v1/row-filters/{row_filter_id}", headers=AUTH_HEADER)
    assert revoked.status_code == 204

    listed = client.get(
        "/api/v1/row-filters", headers=AUTH_HEADER, params={"principal_name": "alice"}
    )
    assert listed.json() == []


def test_create_refuses_a_comparison_across_types_with_the_fix(client: TestClient) -> None:
    """A number-against-text comparison is a 422 that names the column, both types and the fix."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    response = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "id = 'EU'",
        },
    )
    assert response.status_code == 422, response.text
    assert "`id` is long; compare it with a number, not 'EU'." in response.json()["detail"]
    listed = client.get(
        "/api/v1/row-filters", headers=AUTH_HEADER, params={"principal_name": "alice"}
    )
    assert listed.json() == []


def test_a_listed_filter_shows_the_current_column_name_and_bound(
    client: TestClient, settings: Settings
) -> None:
    """A filter bound to a field id follows a rename in the listing; one that is not does not."""
    create_environment(client)
    fields = {1: "id", 2: "dept"}
    create_table_with_fields(client, "dev", "gold", "people", fields)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    where = {"environment": "dev", "namespace": "gold", "table": "people"}
    created = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={"principal": "alice", **where, "expression": "dept = 'a'"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["bound"] is True
    rename_fields(client, "people", fields, {1: "id", 2: "team"})

    listed = client.get("/api/v1/row-filters", headers=AUTH_HEADER, params=where).json()

    assert [(f["expression"], f["bound"]) for f in listed] == [("\"team\" = 'a'", True)]

    engine = create_engine(settings.database)
    with Session(engine) as session:
        session.execute(update(RowFilter).values(binding="unbound"))
        session.commit()
    engine.dispose()
    listed = client.get("/api/v1/row-filters", headers=AUTH_HEADER, params=where).json()

    assert [(f["expression"], f["bound"]) for f in listed] == [("dept = 'a'", False)]


def test_a_row_filter_names_its_subject(client: TestClient) -> None:
    """A row filter carries the name of its person or group, on create and on list."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
    )
    client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "analysts"})
    where = {
        "environment": "dev",
        "namespace": "gold",
        "table": "customers",
        "expression": "id > 5",
    }

    mine = client.post(
        "/api/v1/row-filters", headers=AUTH_HEADER, json={"principal": "ci-bot", **where}
    ).json()
    theirs = client.post(
        "/api/v1/row-filters", headers=AUTH_HEADER, json={"group": "analysts", **where}
    ).json()

    assert mine["principal"] == {
        "id": mine["principal_id"],
        "name": "ci-bot",
        "principal_type": "service",
    }
    assert mine["group"] is None
    assert theirs["principal"] is None
    assert theirs["group"] == "analysts"
    listed = client.get("/api/v1/row-filters", headers=AUTH_HEADER, params=where).json()
    names = {(row["principal"] or {}).get("name"): row["group"] for row in listed}
    assert names == {"ci-bot": None, None: "analysts"}
