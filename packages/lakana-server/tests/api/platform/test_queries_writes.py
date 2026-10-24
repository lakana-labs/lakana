"""Write targets on the queries API and the table kind on the tables API."""

from __future__ import annotations

import io
import uuid

import pyarrow as pa
import pyarrow.ipc
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.grants.authorization import GrantAuthorization
from lakana_worker.pipeline import run_one
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_QUERIES = "/api/v1/environments/dev/queries"


def _setup(client: TestClient) -> None:
    """Create `dev` with `bronze.src` holding ids 1 and 2.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    created = client.post(
        "/api/v1/environments/dev/namespaces/bronze/tables",
        headers=AUTH_HEADER,
        json={"name": "src", "schema": [{"name": "id", "type": "long", "nullable": True}]},
    )
    assert created.status_code == 201, created.text
    sink = io.BytesIO()
    data = pa.table({"id": pa.array([1, 2], pa.int64())})
    with pyarrow.ipc.new_stream(sink, data.schema) as writer:
        writer.write_table(data)
    appended = client.post(
        "/api/v1/environments/dev/namespaces/bronze/tables/src/data",
        headers={**AUTH_HEADER, "Content-Type": "application/vnd.apache.arrow.stream"},
        content=sink.getvalue(),
    )
    assert appended.status_code == 200, appended.text


def _submit_and_run(client: TestClient, body: dict[str, object]) -> dict[str, object]:
    """Submit a query, run it in-process, and return its status body.

    Parameters
    ----------
    client
        The test client.
    body
        The submission body.

    Returns
    -------
    dict
        The `QueryStatusOut` JSON.
    """
    submitted = client.post(_QUERIES, headers=AUTH_HEADER, json=body)
    assert submitted.status_code == 202, submitted.text
    query_id = submitted.json()["query_id"]
    with Session(client.app.state.engine) as session:
        run_one(session, uuid.UUID(query_id), GrantAuthorization(session))
    return client.get(f"{_QUERIES}/{query_id}", headers=AUTH_HEADER).json()


def test_a_write_run_reports_what_it_wrote(client: TestClient) -> None:
    """A `target` makes a write run; its status carries the write's outcome."""
    _setup(client)

    status = _submit_and_run(
        client,
        {"sql": "SELECT id FROM bronze.src", "target": {"table": "gold.daily", "mode": "create"}},
    )

    assert status["status"] == "SUCCEEDED", status
    assert status["write_mode"] == "create"
    assert status["target"] == "gold.daily"
    assert status["rows_written"] == 2
    assert isinstance(status["snapshot_id"], int)
    assert status["watermark"] is None
    assert status["schema_diff"] is None


def test_a_read_run_reports_no_write_fields(client: TestClient) -> None:
    """A plain query's status is unchanged apart from null write fields."""
    _setup(client)

    status = _submit_and_run(client, {"sql": "SELECT id FROM bronze.src"})

    assert status["status"] == "SUCCEEDED", status
    assert (status["write_mode"], status["rows_written"]) == (None, None)


@pytest.mark.parametrize(
    "target",
    [
        {"table": "gold.daily", "mode": "replace", "incremental_key": "id"},
        {"table": "gold.daily", "mode": "create", "evolve_schema": True},
        {"table": "gold.daily", "mode": "create", "location": "s3://elsewhere"},
    ],
)
def test_a_malformed_target_is_a_422(client: TestClient, target: dict[str, object]) -> None:
    """Forbidden combinations, unknown modes and unknown fields are refused."""
    _setup(client)

    response = client.post(
        _QUERIES, headers=AUTH_HEADER, json={"sql": "SELECT 1", "target": target}
    )

    assert response.status_code == 422


def test_a_view_is_listed_with_its_kind_sql_and_references(client: TestClient) -> None:
    """`table get` on a view shows its SQL, references and columns; list shows `kind`."""
    _setup(client)
    _submit_and_run(
        client,
        {"sql": "SELECT id FROM bronze.src", "target": {"table": "gold.daily", "mode": "create"}},
    )
    status = _submit_and_run(
        client,
        {"sql": "SELECT id FROM bronze.src", "target": {"table": "gold.v", "mode": "view"}},
    )
    assert status["status"] == "SUCCEEDED", status

    view = client.get("/api/v1/environments/dev/namespaces/gold/tables/v", headers=AUTH_HEADER)
    listed = client.get("/api/v1/environments/dev/namespaces/gold/tables", headers=AUTH_HEADER)

    assert view.status_code == 200, view.text
    body = view.json()
    assert body["kind"] == "VIEW"
    assert body["metadata_location"] is None
    assert body["sql"] == "SELECT id FROM bronze.src"
    assert [reference["name"] for reference in body["references"]] == ["bronze.src"]
    assert body["schema"] == [
        {
            "name": "id",
            "type": "long",
            "nullable": True,
            "description": None,
            "description_source": None,
        }
    ]
    assert {(t["name"], t["kind"]) for t in listed.json()} == {("daily", "TABLE"), ("v", "VIEW")}


def _reader_headers(client: TestClient) -> dict[str, str]:
    """Create `reader`, granted `READ_TABLE` on the `gold` namespace only.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    dict
        Authorization headers for `reader`.
    """
    created = client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "reader", "type": "USER"}
    )
    assert created.status_code == 201, created.text
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "reader",
            "action": "READ_TABLE",
            "kind": "NAMESPACE",
            "environment": "dev",
            "namespace": "gold",
        },
    )
    assert granted.status_code == 201, granted.text
    issued = client.post(
        "/api/v1/principals/reader/tokens", headers=AUTH_HEADER, json={"name": "reader-pat"}
    )
    assert issued.status_code == 201, issued.text
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def test_a_view_reader_who_cant_read_a_reference_gets_no_sql_or_references(
    client: TestClient,
) -> None:
    """READ on the view alone withholds its SQL and references and reports a count."""
    _setup(client)
    status = _submit_and_run(
        client,
        {"sql": "SELECT id FROM bronze.src", "target": {"table": "gold.v", "mode": "view"}},
    )
    assert status["status"] == "SUCCEEDED", status
    reader = _reader_headers(client)
    path = "/api/v1/environments/dev/namespaces/gold/tables"

    restricted = client.get(f"{path}/v", headers=reader)
    listed = client.get(path, headers=reader)
    full = client.get(f"{path}/v", headers=AUTH_HEADER)

    assert restricted.status_code == 200, restricted.text
    body = restricted.json()
    assert body["kind"] == "VIEW"
    assert body["sql"] is None
    assert body["references"] == []
    assert body["hidden_references"] == 1
    assert [c["name"] for c in body["schema"]] == ["id"]
    assert listed.json()[0]["hidden_references"] == 1
    assert "bronze.src" not in restricted.text
    assert full.json()["sql"] == "SELECT id FROM bronze.src"
    assert full.json()["hidden_references"] == 0


def test_a_merge_target_is_a_422_that_names_what_merges(client: TestClient) -> None:
    _setup(client)

    response = client.post(
        _QUERIES,
        headers=AUTH_HEADER,
        json={"sql": "SELECT 1 AS id", "target": {"table": "gold.daily", "mode": "merge"}},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "merge is written by assets (`write: merge`) and by `client.tables.merge`; a query "
        "writes with create, replace or append"
    )
