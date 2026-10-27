"""The load route of `lakana load`: `POST …/tables/{name}/load`, bronze only."""

from __future__ import annotations

import io

import httpx
import pyarrow as pa
import pyarrow.ipc
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, AuditEventType
from lakana_server.app import create_app
from lakana_server.settings import AppendConfig, Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_HEADERS = {**AUTH_HEADER, "Content-Type": "application/vnd.apache.arrow.stream"}
_ENV = "/api/v1/environments/dev"
_ROWS = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "name": pa.array(["a", "b", "c"])})


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


def _load(client: TestClient, dotted: str, body: bytes, **params: str) -> httpx.Response:
    """Send a load.

    Parameters
    ----------
    client
        The test client.
    dotted
        The target, ``namespace.table``.
    body
        The Arrow stream.
    **params
        Query parameters.

    Returns
    -------
    httpx.Response
        The response.
    """
    namespace, name = dotted.rsplit(".", 1)
    return client.post(
        f"{_ENV}/namespaces/{namespace}/tables/{name}/load",
        headers=_HEADERS,
        params=params,
        content=body,
    )


def _writes(client: TestClient) -> int:
    """Count the write events in the audit log.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    int
        `TABLE_WRITE` and `TABLE_APPENDED` events.
    """
    with Session(client.app.state.engine) as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.event_type.in_(
                        [AuditEventType.TABLE_WRITE, AuditEventType.TABLE_APPENDED]
                    )
                )
            )
            or 0
        )


_SILVER = (
    "`lakana load` loads into bronze only: load into `bronze.customers` and transform it into "
    "`silver.customers` with an asset."
)


def test_a_target_outside_bronze_is_422_with_the_fixed_text(client: TestClient) -> None:
    """A plain HTTP client gets the same refusal as the CLI."""
    create_environment(client)

    response = _load(client, "silver.customers", _ipc(_ROWS), expected_snapshot_id="none")

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == _SILVER
    assert _writes(client) == 0


def test_the_refusal_comes_before_the_body_is_read(settings: Settings) -> None:
    """A body over the cap, or no Arrow at all, still gets the bronze text, not 413."""
    capped = settings.model_copy(update={"append": AppendConfig(max_bytes=1024)})
    with TestClient(create_app(capped)) as client:
        create_environment(client)
        big = pa.table({"id": pa.array(range(10_000), pa.int64())})

        over = _load(client, "silver.customers", _ipc(big), expected_snapshot_id="none")
        garbage = _load(client, "silver.customers", b"not arrow", dry_run="true")

    assert over.status_code == 422, over.text
    assert over.json()["detail"] == _SILVER
    assert garbage.status_code == 422, garbage.text
    assert garbage.json()["detail"] == _SILVER


def test_a_first_load_creates_the_bronze_table(client: TestClient) -> None:
    """`none` creates the table with the rows, whatever `write` says."""
    create_environment(client)

    response = _load(
        client, "bronze.customers", _ipc(_ROWS), write="append", expected_snapshot_id="none"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rows"] == 3
    assert body["created"] is True
    assert body["dry_run"] is False
    table = client.get(f"{_ENV}/namespaces/bronze/tables/customers", headers=AUTH_HEADER).json()
    assert table["snapshot_id"] == body["snapshot_id"]


def test_a_deeper_bronze_namespace_is_accepted(client: TestClient) -> None:
    """`bronze.raw.x` is bronze too."""
    create_environment(client)
    made = client.post(f"{_ENV}/namespaces", headers=AUTH_HEADER, json={"path": "bronze.raw"})
    assert made.status_code == 201, made.text

    response = _load(client, "bronze.raw.customers", _ipc(_ROWS), expected_snapshot_id="none")

    assert response.status_code == 200, response.text
    assert response.json()["created"] is True


def test_a_missing_bronze_namespace_is_404_naming_the_command(client: TestClient) -> None:
    """The next step is the command that creates the namespace."""
    create_environment(client)
    dropped = client.delete(f"{_ENV}/namespaces/bronze", headers=AUTH_HEADER)
    assert dropped.status_code == 204, dropped.text

    response = _load(client, "bronze.customers", _ipc(_ROWS), expected_snapshot_id="none")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == (
        "Namespace `bronze` does not exist in 'dev': create it with "
        "`lakana ns create bronze --env dev`, then load again."
    )


def test_a_dry_run_checks_and_writes_nothing(client: TestClient) -> None:
    """No table, no snapshot, no write event; the answer says it would create the table."""
    create_environment(client)

    response = _load(
        client,
        "bronze.customers",
        _ipc(_ROWS.schema.empty_table()),
        expected_snapshot_id="none",
        dry_run="true",
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"snapshot_id": None, "rows": 0, "created": True, "dry_run": True}
    missing = client.get(f"{_ENV}/namespaces/bronze/tables/customers", headers=AUTH_HEADER)
    assert missing.status_code == 404
    assert _writes(client) == 0


def test_a_dry_run_refuses_a_schema_the_append_would_refuse(client: TestClient) -> None:
    """The schema of the empty stream is checked against the existing table."""
    create_environment(client)
    first = _load(client, "bronze.customers", _ipc(_ROWS), expected_snapshot_id="none").json()
    other = pa.schema([pa.field("id", pa.string())])

    response = _load(
        client,
        "bronze.customers",
        _ipc(other.empty_table()),
        write="append",
        expected_snapshot_id=str(first["snapshot_id"]),
        dry_run="true",
    )

    assert response.status_code == 422, response.text
    assert _writes(client) == 1


def test_append_with_the_current_snapshot_adds_rows(client: TestClient) -> None:
    """A later load appends, pinned to the snapshot the caller read."""
    create_environment(client)
    first = _load(client, "bronze.customers", _ipc(_ROWS), expected_snapshot_id="none").json()

    response = _load(
        client,
        "bronze.customers",
        _ipc(_ROWS),
        write="append",
        expected_snapshot_id=str(first["snapshot_id"]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["rows"] == 3
    assert response.json()["created"] is False


def test_a_stale_snapshot_is_409(client: TestClient) -> None:
    """A concurrent change is a clear conflict, not a silent overwrite."""
    create_environment(client)
    _load(client, "bronze.customers", _ipc(_ROWS), expected_snapshot_id="none")

    again = _load(client, "bronze.customers", _ipc(_ROWS), expected_snapshot_id="none")

    assert again.status_code == 409, again.text


def test_the_generic_data_routes_still_accept_any_layer(client: TestClient) -> None:
    """Assets write silver through `PUT …/data`; the bronze rule is the load route's alone."""
    create_environment(client)

    response = client.put(
        f"{_ENV}/namespaces/silver/tables/customers/data",
        headers=_HEADERS,
        params={"expected_snapshot_id": "none"},
        content=_ipc(_ROWS),
    )

    assert response.status_code == 200, response.text


def test_without_create_table_a_first_load_is_403_before_the_body(client: TestClient) -> None:
    """`WRITE_TABLE` alone cannot create the table; the garbage body is never decoded."""
    create_environment(client)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "w", "type": "USER"})
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "w",
            "action": "WRITE_TABLE",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert granted.status_code < 300, granted.text
    token = client.post("/api/v1/principals/w/tokens", headers=AUTH_HEADER, json={"name": "w"})
    headers = {
        "Authorization": f"Bearer {token.json()['token']}",
        "Content-Type": "application/vnd.apache.arrow.stream",
    }

    response = client.post(
        f"{_ENV}/namespaces/bronze/tables/customers/load",
        headers=headers,
        params={"expected_snapshot_id": "none"},
        content=b"not arrow",
    )

    assert response.status_code == 403, response.text
    assert "CREATE_TABLE" in response.text


def test_replace_with_the_current_snapshot_replaces_the_rows(client: TestClient) -> None:
    """The default write replaces every row, pinned to the snapshot the caller read."""
    create_environment(client)
    first = _load(client, "bronze.customers", _ipc(_ROWS), expected_snapshot_id="none").json()

    response = _load(
        client,
        "bronze.customers",
        _ipc(_ROWS.slice(0, 1)),
        expected_snapshot_id=str(first["snapshot_id"]),
    )

    assert response.status_code == 200, response.text
    assert response.json()["rows"] == 1
    assert response.json()["snapshot_id"] != first["snapshot_id"]


_REASON = "INC-7: load the corrected export once"


def _protect(client: TestClient) -> None:
    """Make `dev` a protected environment.

    Parameters
    ----------
    client
        The test client.
    """
    protected = client.patch(f"{_ENV}", headers=AUTH_HEADER, json={"is_protected": True})
    assert protected.status_code == 200, protected.text


def _break_glass_events(client: TestClient) -> int:
    """Count the break-glass events in the audit log.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    int
        `BREAK_GLASS_WRITE` events.
    """
    with Session(client.app.state.engine) as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == AuditEventType.BREAK_GLASS_WRITE)
            )
            or 0
        )


def test_a_protected_environment_refuses_the_dry_run_before_the_body(
    client: TestClient,
) -> None:
    """Without break glass the check fails before a byte is read, as the write would."""
    create_environment(client)
    _protect(client)

    dry = _load(
        client, "bronze.customers", b"not arrow", expected_snapshot_id="none", dry_run="true"
    )
    real = _load(client, "bronze.customers", b"not arrow", expected_snapshot_id="none")

    for response in (dry, real):
        assert response.status_code == 403, response.text
        assert "is protected" in response.json()["detail"]
    assert _writes(client) == 0


def test_break_glass_without_manage_environment_is_refused_before_the_body(
    client: TestClient,
) -> None:
    """A reason alone does not open a protected environment."""
    create_environment(client)
    _protect(client)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "w", "type": "USER"})
    for action in ("WRITE_TABLE", "CREATE_TABLE"):
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={"principal": "w", "action": action, "kind": "ENVIRONMENT", "environment": "dev"},
        )
        assert granted.status_code < 300, granted.text
    token = client.post("/api/v1/principals/w/tokens", headers=AUTH_HEADER, json={"name": "w"})
    headers = {
        "Authorization": f"Bearer {token.json()['token']}",
        "Content-Type": "application/vnd.apache.arrow.stream",
    }

    response = client.post(
        f"{_ENV}/namespaces/bronze/tables/customers/load",
        headers=headers,
        params={"expected_snapshot_id": "none", "break_glass_reason": _REASON, "dry_run": "true"},
        content=b"not arrow",
    )

    assert response.status_code == 403, response.text
    assert "MANAGE_ENVIRONMENT" in response.text


def test_break_glass_passes_the_dry_run_and_is_recorded_only_by_the_write(
    client: TestClient,
) -> None:
    """The dry run records nothing; the load records one break-glass write."""
    create_environment(client)
    _protect(client)

    dry = _load(
        client,
        "bronze.customers",
        _ipc(_ROWS.schema.empty_table()),
        expected_snapshot_id="none",
        break_glass_reason=_REASON,
        dry_run="true",
    )
    assert dry.status_code == 200, dry.text
    assert _break_glass_events(client) == 0

    real = _load(
        client,
        "bronze.customers",
        _ipc(_ROWS),
        expected_snapshot_id="none",
        break_glass_reason=_REASON,
    )

    assert real.status_code == 200, real.text
    assert real.json()["created"] is True
    assert _break_glass_events(client) == 1


def test_a_missing_table_without_a_pin_says_to_pass_none(client: TestClient) -> None:
    """A client that sends no pin learns how a first load creates the table."""
    create_environment(client)

    response = _load(client, "bronze.customers", b"not arrow")

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == (
        "Table `bronze.customers` does not exist in 'dev': a first load passes "
        "expected_snapshot_id=none, which creates it."
    )
