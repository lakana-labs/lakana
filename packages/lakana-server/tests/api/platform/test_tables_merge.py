"""The Arrow merge route: `POST …/tables/{name}/merge?key=…`."""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import AuditEvent, AuditEventType
from lakana_server.app import create_app
from lakana_server.settings import AppendConfig, Settings
from lakana_worker.pipeline import run_one
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_HEADERS = {**AUTH_HEADER, "Content-Type": "application/vnd.apache.arrow.stream"}
_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"
_SCHEMA = pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("name", pa.string())])


def _ipc(table: pa.Table) -> bytes:
    """Serialize a table as an uncompressed Arrow IPC stream.

    Parameters
    ----------
    table
        The rows.

    Returns
    -------
    bytes
        The stream.
    """
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _rows(rows: list[tuple[int, str]]) -> pa.Table:
    """Build rows of `gold.customers`.

    Parameters
    ----------
    rows
        ``(id, name)`` pairs.

    Returns
    -------
    pa.Table
        The rows.
    """
    ids, names = zip(*rows, strict=True) if rows else ((), ())
    return pa.table(
        {"id": pa.array(list(ids), pa.int64()), "name": pa.array(list(names), pa.string())},
        schema=_SCHEMA,
    )


def _setup(client: TestClient) -> None:
    """Create `dev` with `gold.customers` holding ids 1 and 2.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    created = client.post(
        _TABLES,
        headers=AUTH_HEADER,
        json={
            "name": "customers",
            "schema": [
                {"name": "id", "type": "long", "nullable": False},
                {"name": "name", "type": "string", "nullable": True},
            ],
        },
    )
    assert created.status_code == 201, created.text
    appended = client.post(
        f"{_TABLES}/customers/data", headers=_HEADERS, content=_ipc(_rows([(1, "a"), (2, "b")]))
    )
    assert appended.status_code == 200, appended.text


def _contents(client: TestClient, table: str) -> list[tuple[int, str]]:
    """Read a table's rows through the queries API and an in-process worker.

    Parameters
    ----------
    client
        The test client.
    table
        The dotted table.

    Returns
    -------
    list of tuple
        ``(id, name)``, ordered by id.
    """
    submitted = client.post(
        "/api/v1/environments/dev/queries",
        headers=AUTH_HEADER,
        json={"sql": f"SELECT id, name FROM {table} ORDER BY id"},
    )
    query_id = submitted.json()["query_id"]
    with Session(client.app.state.engine) as session:
        run_one(session, uuid.UUID(query_id), GrantAuthorization(session))
    result = client.get(f"/api/v1/environments/dev/queries/{query_id}/result", headers=AUTH_HEADER)
    with pyarrow.ipc.open_file(pa.BufferReader(result.content)) as reader:
        found = reader.read_all()
    return list(zip(found.column("id").to_pylist(), found.column("name").to_pylist(), strict=True))


def _current_snapshot(client: TestClient, name: str = "customers") -> int | None:
    """Read a table's current snapshot id.

    Parameters
    ----------
    client
        The test client.
    name
        The table in `gold`.

    Returns
    -------
    int or None
        The snapshot id.
    """
    return client.get(f"{_TABLES}/{name}", headers=AUTH_HEADER).json()["snapshot_id"]


def test_merge_updates_and_inserts_and_answers_the_counts(client: TestClient) -> None:
    """Id 2 is updated, id 3 inserted; the answer is `MergeOut`."""
    _setup(client)

    response = client.post(
        f"{_TABLES}/customers/merge",
        headers=_HEADERS,
        params={"key": "id"},
        content=_ipc(_rows([(2, "B"), (3, "c")])),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "snapshot_id": _current_snapshot(client),
        "rows_inserted": 1,
        "rows_updated": 1,
    }
    assert _contents(client, "gold.customers") == [(1, "a"), (2, "B"), (3, "c")]


def test_a_missing_key_is_422_before_the_body_is_read(client: TestClient) -> None:
    """Without `key` the route refuses at once, whatever the body holds."""
    _setup(client)
    before = _current_snapshot(client)

    response = client.post(
        f"{_TABLES}/customers/merge", headers=_HEADERS, content=b"not an arrow stream"
    )

    assert response.status_code == 422, response.text
    assert "key" in response.text
    assert _current_snapshot(client) == before


def test_a_repeated_key_in_the_batch_is_422_with_the_count(client: TestClient) -> None:
    """The fixed text, with the count and no value."""
    _setup(client)

    response = client.post(
        f"{_TABLES}/customers/merge",
        headers=_HEADERS,
        params={"key": "id"},
        content=_ipc(_rows([(9, "x"), (9, "y")])),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "key `id` repeats in 2 rows of this batch; make it unique "
        "(for example with `unique(subset=['id'], keep='last')`)"
    )


def test_a_composite_key_is_sent_as_a_repeated_parameter(client: TestClient) -> None:
    """`key=id&key=name`: a row with id 1 and another name is new."""
    _setup(client)

    response = client.post(
        f"{_TABLES}/customers/merge",
        headers=_HEADERS,
        params=[("key", "id"), ("key", "name")],
        content=_ipc(_rows([(1, "z")])),
    )

    assert response.status_code == 200, response.text
    assert (response.json()["rows_inserted"], response.json()["rows_updated"]) == (1, 0)


def test_a_key_column_the_table_does_not_have_is_422(client: TestClient) -> None:
    """The fixed text names the column and the table."""
    _setup(client)

    response = client.post(
        f"{_TABLES}/customers/merge",
        headers=_HEADERS,
        params={"key": "customer_id"},
        content=_ipc(_rows([(1, "z")])),
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "key column `customer_id` is not a column of gold.customers"
    )


def test_a_schema_mismatch_is_422_naming_the_column(client: TestClient) -> None:
    """A batch without `name` does not fit the table."""
    _setup(client)
    data = pa.table(
        {"id": pa.array([1], pa.int64())},
        schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
    )

    response = client.post(
        f"{_TABLES}/customers/merge", headers=_HEADERS, params={"key": "id"}, content=_ipc(data)
    )

    assert response.status_code == 422, response.text
    assert "name" in response.json()["detail"]


def test_a_stale_snapshot_is_409(client: TestClient) -> None:
    """`expected_snapshot_id` of an older snapshot conflicts and writes nothing."""
    _setup(client)
    old = _current_snapshot(client)
    client.post(f"{_TABLES}/customers/data", headers=_HEADERS, content=_ipc(_rows([(5, "e")])))
    before = _current_snapshot(client)

    response = client.post(
        f"{_TABLES}/customers/merge",
        headers=_HEADERS,
        params={"key": "id", "expected_snapshot_id": str(old)},
        content=_ipc(_rows([(1, "z")])),
    )

    assert response.status_code == 409, response.text
    assert _current_snapshot(client) == before


def test_none_creates_a_missing_table_from_the_batch(client: TestClient) -> None:
    """`expected_snapshot_id=none` on a missing table creates it with the rows."""
    _setup(client)

    response = client.post(
        f"{_TABLES}/accounts/merge",
        headers=_HEADERS,
        params={"key": "id", "expected_snapshot_id": "none"},
        content=_ipc(_rows([(1, "a"), (2, "b")])),
    )

    assert response.status_code == 200, response.text
    assert response.json()["rows_inserted"] == 2
    assert _contents(client, "gold.accounts") == [(1, "a"), (2, "b")]


def test_a_missing_table_without_none_is_404(client: TestClient) -> None:
    """Without `none` a merge never creates."""
    _setup(client)

    response = client.post(
        f"{_TABLES}/accounts/merge",
        headers=_HEADERS,
        params={"key": "id"},
        content=_ipc(_rows([(1, "a")])),
    )

    assert response.status_code == 404, response.text


def _restricted(client: TestClient, name: str, actions: list[str]) -> dict[str, str]:
    """Create a user holding only `actions` on `dev` and return its Arrow request headers.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    actions
        Actions granted at environment scope.

    Returns
    -------
    dict
        `Authorization` and the Arrow `Content-Type`.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    for action in actions:
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={"principal": name, "action": action, "kind": "ENVIRONMENT", "environment": "dev"},
        )
        assert granted.status_code < 300, granted.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {
        "Authorization": f"Bearer {issued.json()['token']}",
        "Content-Type": "application/vnd.apache.arrow.stream",
    }


def test_without_write_table_it_is_403_before_the_body_is_read(client: TestClient) -> None:
    """A reader is refused, and the refusal is audited; a garbage body is never decoded."""
    _setup(client)
    reader = _restricted(client, "reader", ["READ_TABLE"])

    response = client.post(
        f"{_TABLES}/customers/merge",
        headers=reader,
        params={"key": "id"},
        content=b"not an arrow stream",
    )

    assert response.status_code == 403, response.text
    assert "WRITE_TABLE" in response.text
    with Session(client.app.state.engine) as session:
        denied = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
        ).all()
    assert any("WRITE_TABLE" in str(event.detail) for event in denied)


def test_a_merge_needs_read_table_too(client: TestClient) -> None:
    """A merge compares the rows with the table's: `WRITE_TABLE` alone is refused."""
    _setup(client)
    writer = _restricted(client, "writer", ["WRITE_TABLE"])
    before = _current_snapshot(client)

    response = client.post(
        f"{_TABLES}/customers/merge",
        headers=writer,
        params={"key": "id"},
        content=b"not an arrow stream",
    )

    assert response.status_code == 403, response.text
    assert "READ_TABLE" in response.text
    assert _current_snapshot(client) == before


def test_a_filtered_writer_is_refused_with_the_reason(client: TestClient) -> None:
    """A row filter on the table for the caller: 422 naming the cause, before the body."""
    _setup(client)
    writer = _restricted(client, "writer", ["WRITE_TABLE", "READ_TABLE"])
    created = client.post(
        "/api/v1/row-filters",
        headers=AUTH_HEADER,
        json={
            "principal": "writer",
            "environment": "dev",
            "namespace": "gold",
            "table": "customers",
            "expression": "id < 2",
        },
    )
    assert created.status_code < 300, created.text

    response = client.post(
        f"{_TABLES}/customers/merge",
        headers=writer,
        params={"key": "id"},
        content=b"not an arrow stream",
    )

    assert response.status_code == 422, response.text
    assert "row filter or column mask applies to you" in response.json()["detail"]


def test_creating_by_merge_needs_create_table(client: TestClient) -> None:
    """`WRITE_TABLE` and `READ_TABLE` merge into a table but do not create one."""
    _setup(client)
    writer = _restricted(client, "writer", ["WRITE_TABLE", "READ_TABLE"])
    url = f"{_TABLES}/accounts/merge"
    params = {"key": "id", "expected_snapshot_id": "none"}

    refused = client.post(url, headers=writer, params=params, content=_ipc(_rows([(1, "a")])))
    merged = client.post(
        f"{_TABLES}/customers/merge",
        headers=writer,
        params={"key": "id"},
        content=_ipc(_rows([(1, "z")])),
    )

    assert refused.status_code == 403, refused.text
    assert "CREATE_TABLE" in refused.text
    assert merged.status_code == 200, merged.text


def test_a_body_over_the_cap_is_413_and_writes_nothing(settings: Settings, tmp_path: Path) -> None:
    """`LAKANA_APPEND__MAX_BYTES` bounds a merge body as it bounds an append."""
    capped = settings.model_copy(update={"append": AppendConfig(max_bytes=1024)})
    with TestClient(create_app(capped)) as client:
        _setup(client)
        before = _current_snapshot(client)
        big = _rows([(i, "x" * 100) for i in range(2000)])

        response = client.post(
            f"{_TABLES}/customers/merge", headers=_HEADERS, params={"key": "id"}, content=_ipc(big)
        )

        assert response.status_code == 413, response.text
        assert _current_snapshot(client) == before
