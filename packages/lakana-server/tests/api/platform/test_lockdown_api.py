"""The lockdown at the HTTP edge: platform writes, break-glass and Iceberg REST."""

from __future__ import annotations

import io

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_table

_REASON = "incident 42: fix one row by hand"
_TABLES = "/api/v1/environments/prod/namespaces/bronze/tables"
_MESSAGE = "Environment 'prod' is protected: only runs of the deployed project version may write."
_ARROW = "application/vnd.apache.arrow.stream"


def _protected(client: TestClient) -> None:
    """Create `prod` with `bronze.events` and protect it.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client, "prod")
    create_table(client, "prod", "bronze", "events")
    response = client.patch(
        "/api/v1/environments/prod", headers=AUTH_HEADER, json={"is_protected": True}
    )
    assert response.status_code == 200, response.text


def _ipc() -> bytes:
    """Serialize one row matching `create_table`'s helper schema as an Arrow IPC stream.

    Returns
    -------
    bytes
        The stream.
    """
    table = pa.table(
        {"id": pa.array([1], pa.int64())},
        schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
    )
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        (
            "POST",
            _TABLES,
            {"name": "t2", "schema": [{"name": "id", "type": "long", "nullable": False}]},
        ),
        (
            "POST",
            f"{_TABLES}/external",
            {"name": "ext", "location_pattern": "raw/x/*.parquet", "format": "parquet"},
        ),
        ("DELETE", f"{_TABLES}/events", None),
        ("POST", "/api/v1/environments/prod/namespaces", {"path": "bronze.eu"}),
        ("DELETE", "/api/v1/environments/prod/namespaces/gold", None),
        (
            "POST",
            "/api/v1/environments/prod/queries",
            {"sql": "SELECT 1 AS id", "target": {"table": "bronze.events", "mode": "append"}},
        ),
    ],
)
def test_platform_writes_are_403_with_the_lockdown_message(
    client: TestClient, method: str, path: str, body: dict | None
) -> None:
    """Every JSON write endpoint refuses in a protected environment."""
    _protected(client)
    response = client.request(method, path, headers=AUTH_HEADER, json=body)
    assert response.status_code == 403, response.text
    assert _MESSAGE in response.json()["detail"]


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_arrow_writes_are_403(client: TestClient, method: str) -> None:
    """Append and replace (Arrow bodies) refuse too."""
    _protected(client)
    response = client.request(
        method,
        f"{_TABLES}/events/data",
        headers={**AUTH_HEADER, "Content-Type": _ARROW},
        content=_ipc(),
    )
    assert response.status_code == 403, response.text


@pytest.mark.slow
def test_break_glass_append_and_drop_succeed(client: TestClient) -> None:
    """A break-glass reason as query parameter or body field lets exactly that write through."""
    _protected(client)
    appended = client.post(
        f"{_TABLES}/events/data",
        headers={**AUTH_HEADER, "Content-Type": _ARROW},
        params={"break_glass_reason": _REASON},
        content=_ipc(),
    )
    assert appended.status_code == 200, appended.text
    created = client.post(
        _TABLES,
        headers=AUTH_HEADER,
        json={
            "name": "t2",
            "schema": [{"name": "id", "type": "long", "nullable": False}],
            "break_glass_reason": _REASON,
        },
    )
    assert created.status_code == 201, created.text
    dropped = client.delete(
        f"{_TABLES}/t2", headers=AUTH_HEADER, params={"break_glass_reason": _REASON}
    )
    assert dropped.status_code == 204
    events = client.get(
        "/api/v1/audit-events", headers=AUTH_HEADER, params={"event_type": "BREAK_GLASS_WRITE"}
    ).json()["items"]
    assert {e["detail"]["target"] for e in events} == {"bronze.events", "bronze.t2"}


def test_a_short_reason_is_422(client: TestClient) -> None:
    """The reason must be 10-500 characters."""
    _protected(client)
    response = client.delete(
        f"{_TABLES}/events", headers=AUTH_HEADER, params={"break_glass_reason": "short"}
    )
    assert response.status_code == 422


def test_iceberg_rest_writes_are_refused(client: TestClient) -> None:
    """Iceberg REST commits, creates and drops are refused; reads still work."""
    _protected(client)
    created = client.post(
        "/v1/prod/namespaces",
        headers=AUTH_HEADER,
        json={"namespace": ["widgets"], "properties": {}},
    )
    assert created.status_code == 403
    commit = client.post(
        "/v1/prod/namespaces/bronze/tables/events",
        headers=AUTH_HEADER,
        json={"requirements": [], "updates": []},
    )
    assert commit.status_code == 403
    assert _MESSAGE in commit.text
    assert (
        client.get("/v1/prod/namespaces/bronze/tables/events", headers=AUTH_HEADER).status_code
        == 200
    )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/prod/namespaces", {"namespace": ["widgets"], "properties": {}}),
        (
            "POST",
            "/v1/prod/namespaces/bronze/properties",
            {"removals": [], "updates": {"owner": "me"}},
        ),
        ("DELETE", "/v1/prod/namespaces/bronze", None),
        (
            "POST",
            "/v1/prod/namespaces/bronze/tables",
            {
                "name": "t2",
                "schema": {
                    "type": "struct",
                    "schema-id": 0,
                    "fields": [{"id": 1, "name": "id", "required": True, "type": "long"}],
                },
            },
        ),
        ("POST", "/v1/prod/namespaces/bronze/tables/events", {"requirements": [], "updates": []}),
        ("DELETE", "/v1/prod/namespaces/bronze/tables/events", None),
        (
            "POST",
            "/v1/prod/tables/rename",
            {
                "source": {"namespace": ["bronze"], "name": "events"},
                "destination": {"namespace": ["bronze"], "name": "moved"},
            },
        ),
    ],
)
def test_every_iceberg_write_route_is_refused_even_with_a_reason(
    client: TestClient, method: str, path: str, body: dict | None
) -> None:
    """No Iceberg REST write works in a protected environment, and no break-glass exists there."""
    _protected(client)
    for params in ({}, {"break_glass_reason": _REASON}):
        response = client.request(method, path, headers=AUTH_HEADER, json=body, params=params)
        assert response.status_code == 403, response.text
        assert _MESSAGE in response.text
    audit = client.get(
        "/api/v1/audit-events", headers=AUTH_HEADER, params={"event_type": "BREAK_GLASS_WRITE"}
    ).json()["items"]
    assert audit == []


def test_break_glass_without_manage_environment_is_403_and_leaves_no_audit_row(
    client: TestClient,
) -> None:
    """A data engineer's reason does not open the lockdown, and nothing is recorded as done."""
    _protected(client)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "eve", "type": "USER"})
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "eve",
            "role": "DATA_ENGINEER",
            "kind": "ENVIRONMENT",
            "environment": "prod",
        },
    )
    assert granted.status_code == 201, granted.text
    token = client.post(
        "/api/v1/principals/eve/tokens", headers=AUTH_HEADER, json={"name": "eve"}
    ).json()["token"]
    response = client.delete(
        f"{_TABLES}/events",
        headers={"Authorization": f"Bearer {token}"},
        params={"break_glass_reason": _REASON},
    )
    assert response.status_code == 403, response.text
    audit = client.get(
        "/api/v1/audit-events", headers=AUTH_HEADER, params={"event_type": "BREAK_GLASS_WRITE"}
    ).json()["items"]
    assert audit == []
