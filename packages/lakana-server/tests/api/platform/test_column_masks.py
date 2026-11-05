"""Tests for the column-masks resource of the platform API."""

import io
import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc
from fastapi.testclient import TestClient
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import ColumnMask, Principal, QueryRun, QueryRunStatus
from lakana_core.storage.resolver import StorageResolver
from lakana_server.settings import Settings
from lakana_worker.pipeline import run_one
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import (
    create_environment,
    create_table,
    create_table_with_fields,
    rename_fields,
)


def test_create_a_column_mask_and_list_it_for_the_principal(client: TestClient) -> None:
    """A created column mask shows up when listing that principal's masks."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    created = client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "column": "id",
            "expression": "NULL",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["column"] == "id"
    assert body["expression"] == "NULL"

    listed = client.get(
        "/api/v1/column-masks", headers=AUTH_HEADER, params={"principal_name": "alice"}
    )
    assert listed.status_code == 200
    assert [m["id"] for m in listed.json()] == [body["id"]]


def test_create_accepts_the_broader_mask_grammar(client: TestClient) -> None:
    """substr/||/md5 are accepted for a mask expression, matching the broader mask grammar."""
    create_environment(client)
    made = client.post(
        "/v1/dev/namespaces/gold/tables",
        headers=AUTH_HEADER,
        json={
            "name": "customers",
            "schema": {
                "type": "struct",
                "schema-id": 0,
                "fields": [
                    {"id": 1, "name": "id", "type": "long", "required": True},
                    {"id": 2, "name": "ssn", "type": "string", "required": False},
                ],
            },
            "properties": {},
        },
    )
    assert made.status_code == 200, made.text
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    response = client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "column": "ssn",
            "expression": "substr(ssn, 1, 3) || '***'",
        },
    )
    assert response.status_code == 201, response.text


def test_create_rejects_a_function_outside_the_mask_allowlist(client: TestClient) -> None:
    """sha256 is not in the mask allowlist (only substr/substring, ||, md5 are) — 422."""
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    response = client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "column": "ssn",
            "expression": "sha256(ssn)",
        },
    )
    assert response.status_code == 422


def test_create_requires_exactly_one_of_principal_or_group(client: TestClient) -> None:
    """Passing neither principal nor group is a 422."""
    response = client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "column": "ssn",
            "expression": "NULL",
        },
    )
    assert response.status_code == 422


def test_list_by_table_requires_all_three_fields_together(client: TestClient) -> None:
    """Listing by table needs environment, namespace, and table all given."""
    response = client.get(
        "/api/v1/column-masks", headers=AUTH_HEADER, params={"environment": "dev"}
    )
    assert response.status_code == 422


def test_list_by_table_is_an_exact_match(client: TestClient) -> None:
    """Listing by table finds only the exact environment/namespace/table given."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "column": "id",
            "expression": "NULL",
        },
    )
    matching = client.get(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        params={"environment": "dev", "namespace": "gold", "table": "customers"},
    )
    assert len(matching.json()) == 1
    different_table = client.get(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        params={"environment": "dev", "namespace": "gold", "table": "orders"},
    )
    assert different_table.json() == []


def test_list_column_masks_for_a_group(client: TestClient) -> None:
    """Listing by group_name shows that group's masks."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/groups", headers=AUTH_HEADER, json={"name": "admins"})
    client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={
            "group": "admins",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "column": "id",
            "expression": "NULL",
        },
    )
    listed = client.get(
        "/api/v1/column-masks", headers=AUTH_HEADER, params={"group_name": "admins"}
    )
    assert len(listed.json()) == 1


def test_revoke_a_column_mask(client: TestClient) -> None:
    """A revoked column mask no longer appears when listing the principal's masks."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    created = client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "column": "id",
            "expression": "NULL",
        },
    )
    column_mask_id = created.json()["id"]

    revoked = client.delete(f"/api/v1/column-masks/{column_mask_id}", headers=AUTH_HEADER)
    assert revoked.status_code == 204

    listed = client.get(
        "/api/v1/column-masks", headers=AUTH_HEADER, params={"principal_name": "alice"}
    )
    assert listed.json() == []


def test_renaming_a_masked_table_keeps_the_mask(client: TestClient, tmp_path: Path) -> None:
    """A mask follows its table through a rename.

    Before the id binding, masks matched by table *name*, so a principal subject to a
    mask on `gold.customers` could rename it to `gold.customers2` and read
    raw values.
    """
    engine = client.app.state.engine
    ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
    )

    with Session(engine) as session:
        admin_row = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
        admin = PrincipalRef(id=admin_row.id, name=admin_row.name, type=admin_row.type)
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        catalog.create_environment(
            admin, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
        )
        catalog.create_table(
            admin,
            ref,
            schema=Schema(
                NestedField(1, "id", LongType(), required=True),
                NestedField(2, "ssn", StringType(), required=False),
            ),
        )
        arrow_schema = pa.schema(
            [pa.field("id", pa.int64(), nullable=False), pa.field("ssn", pa.string())]
        )
        data = pa.table({"id": [1], "ssn": ["111-11-1111"]}, schema=arrow_schema)
        catalog.append(admin, ref, data)

    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"})
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "bob",
            "role": "DATA_ENGINEER",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    mask_created = client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={
            "principal": "bob",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "column": "ssn",
            "expression": "'***'",
        },
    )
    assert mask_created.status_code == 201, mask_created.text

    issued = client.post("/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "bob"})
    bob_headers = {"Authorization": f"Bearer {issued.json()['token']}"}

    renamed = client.post(
        "/v1/dev/tables/rename",
        headers=bob_headers,
        json={
            "source": {"namespace": ["gold"], "name": "customers"},
            "destination": {"namespace": ["gold"], "name": "customers2"},
        },
    )
    assert renamed.status_code == 200, renamed.text

    submitted = client.post(
        "/api/v1/environments/dev/queries",
        headers=bob_headers,
        json={"sql": "SELECT ssn FROM gold.customers2"},
    )
    assert submitted.status_code == 202, submitted.text
    query_id = uuid.UUID(submitted.json()["query_id"])

    with Session(engine) as session:
        run_one(session, query_id, GrantAuthorization(session))
        run = session.get(QueryRun, query_id)
        assert run is not None
        assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
        assert run.result_location is not None
        raw = StorageResolver(session).for_environment("dev").storage.read(run.result_location)

    with pyarrow.ipc.open_file(io.BytesIO(raw)) as reader:
        result = reader.read_all()

    assert result.column("ssn").to_pylist() == ["***"]


def test_create_rejects_a_column_the_table_does_not_have(client: TestClient) -> None:
    """A mask on a column that is not in the table is a 422 naming it and the real columns."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    response = client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "column": "no_such_column",
            "expression": "NULL",
        },
    )
    assert response.status_code == 422
    assert "has no column 'no_such_column'" in response.text
    assert "gold.customers" in response.text
    listed = client.get(
        "/api/v1/column-masks", headers=AUTH_HEADER, params={"principal_name": "alice"}
    )
    assert listed.json() == []


def test_create_refuses_a_mask_of_another_type_than_its_column(client: TestClient) -> None:
    """A text mask on a long column is a 422 that names both types and a value that fits."""
    create_environment(client)
    create_table(client, "dev", "gold", "customers")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    response = client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={
            "principal": "alice",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "column": "id",
            "expression": "'***'",
        },
    )
    assert response.status_code == 422, response.text
    assert (
        "`id` is long, but the mask is text. Use a value of its own type: 0 or NULL."
        in response.json()["detail"]
    )


def test_a_listed_mask_shows_the_current_column_name_and_bound(
    client: TestClient, settings: Settings
) -> None:
    """A bound mask is listed on the column's current name; an unbound one as written."""
    create_environment(client)
    fields = {1: "id", 2: "ssn"}
    create_table_with_fields(client, "dev", "gold", "people", fields)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "USER"})
    where = {"environment": "dev", "namespace": "gold", "table": "people"}
    created = client.post(
        "/api/v1/column-masks",
        headers=AUTH_HEADER,
        json={"principal": "alice", **where, "column": "ssn", "expression": "'***'"},
    )
    assert created.status_code == 201, created.text
    assert (created.json()["column"], created.json()["bound"]) == ("ssn", True)
    rename_fields(client, "people", fields, {1: "id", 2: "tax_id"})

    listed = client.get("/api/v1/column-masks", headers=AUTH_HEADER, params=where).json()

    assert [(m["column"], m["bound"]) for m in listed] == [("tax_id", True)]

    engine = create_engine(settings.database)
    with Session(engine) as session:
        session.execute(update(ColumnMask).values(binding="unbound"))
        session.commit()
    engine.dispose()
    listed = client.get("/api/v1/column-masks", headers=AUTH_HEADER, params=where).json()

    assert [(m["column"], m["bound"]) for m in listed] == [("ssn", False)]
