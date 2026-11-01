"""The Arrow history route: `POST …/tables/{name}/history?key=…`."""

from __future__ import annotations

import httpx
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.config import RunConfig
from lakana_core.models import AuditEvent, AuditEventType
from lakana_server.api.platform import tables as tables_module
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.api.platform.test_tables_merge import _HEADERS, _TABLES, _ipc, _restricted
from tests.server_test_helpers import create_environment


def _customers(rows: list[tuple[str, str]]) -> bytes:
    """Serialize a dimension's current state.

    Parameters
    ----------
    rows
        ``(customer_id, region)`` pairs.

    Returns
    -------
    bytes
        The Arrow stream.
    """
    return _ipc(
        pa.table(
            {"customer_id": [r[0] for r in rows], "region": [r[1] for r in rows]},
            schema=pa.schema([("customer_id", pa.string()), ("region", pa.string())]),
        )
    )


def _history(client: TestClient, rows: list[tuple[str, str]], **params: str) -> httpx.Response:
    """Post one history write of `gold.dim_customer`.

    Parameters
    ----------
    client
        The test client.
    rows
        The batch.
    **params
        More query parameters.

    Returns
    -------
    httpx.Response
        The response.
    """
    return client.post(
        f"{_TABLES}/dim_customer/history",
        headers=_HEADERS,
        params={"key": "customer_id", **params},
        content=_customers(rows),
    )


def test_the_first_write_creates_and_later_ones_answer_the_counts(client: TestClient) -> None:
    create_environment(client)

    created = _history(client, [("A", "north"), ("B", "north")], expected_snapshot_id="none")
    assert created.status_code == 200, created.text
    assert created.json()["created"] is True
    assert created.json()["versions_opened"] == 2

    changed = _history(client, [("A", "south")], close_missing="true")
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert (body["versions_opened"], body["versions_closed"], body["closed_missing"]) == (1, 2, 1)
    assert body["created"] is False and body["snapshot_id"] is not None

    same = _history(client, [("A", "south")])
    assert same.json() == {
        "snapshot_id": None,
        "versions_opened": 0,
        "versions_closed": 0,
        "closed_missing": 0,
        "created": False,
    }
    with Session(client.app.state.engine) as session:
        modes = session.scalars(
            select(AuditEvent.detail).where(AuditEvent.event_type == AuditEventType.TABLE_WRITE)
        ).all()
    assert [d["mode"] for d in modes] == ["create", "history"]


def test_a_missing_key_is_422_before_the_body_is_read(client: TestClient) -> None:
    create_environment(client)
    response = client.post(
        f"{_TABLES}/dim_customer/history", headers=_HEADERS, content=b"not an arrow stream"
    )
    assert response.status_code == 422, response.text
    assert "key" in response.text


def test_a_refused_batch_is_422_with_the_text(client: TestClient) -> None:
    create_environment(client)
    response = _history(client, [], expected_snapshot_id="none", close_missing="true")
    assert response.status_code == 422, response.text
    assert "The asset's result is empty, and `close_missing: true`" in response.text


def test_a_missing_table_without_none_is_404_and_a_stale_snapshot_409(client: TestClient) -> None:
    create_environment(client)
    assert _history(client, [("A", "north")]).status_code == 404
    first = _history(client, [("A", "north")], expected_snapshot_id="none").json()
    _history(client, [("A", "south")])
    stale = _history(client, [("A", "east")], expected_snapshot_id=str(first["snapshot_id"]))
    assert stale.status_code == 409, stale.text


def test_without_read_table_it_is_403_before_the_body_is_read(client: TestClient) -> None:
    create_environment(client)
    _history(client, [("A", "north")], expected_snapshot_id="none")
    writer = _restricted(client, "writer", ["WRITE_TABLE"])
    response = client.post(
        f"{_TABLES}/dim_customer/history",
        headers=writer,
        params={"key": "customer_id"},
        content=b"never read",
    )
    assert response.status_code == 403, response.text


def test_the_servers_open_versions_cap_applies(settings: Settings) -> None:
    """`LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS` refuses on the REST path as in a run."""
    capped = settings.model_copy(update={"run": RunConfig(max_history_open_versions=1)})
    with TestClient(create_app(capped)) as client:
        create_environment(client)
        refused = _history(client, [("A", "north"), ("B", "north")], expected_snapshot_id="none")

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"].startswith(
        "gold.dim_customer would start with 2 open versions, more than "
        "LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS (1) allows."
    )


def test_a_history_write_gives_its_memory_back_whether_or_not_it_lands(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the write ran, refused or not; a request refused before the body is read never trims."""
    trimmed: list[str] = []
    monkeypatch.setattr(tables_module, "trim_heap", lambda: trimmed.append("trim"))
    create_environment(client)

    assert _history(client, [("A", "north")]).status_code == 404
    assert trimmed == []
    refused = _history(client, [], expected_snapshot_id="none", close_missing="true")
    assert refused.status_code == 422, refused.text
    assert trimmed == ["trim"]
    assert _history(client, [("A", "north")], expected_snapshot_id="none").status_code == 200
    assert trimmed == ["trim", "trim"]
