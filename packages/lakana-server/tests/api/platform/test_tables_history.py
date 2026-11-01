"""The Arrow history route: `POST …/tables/{name}/history?key=…`."""

from __future__ import annotations

import httpx
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.catalog.writes import max_rows_message
from lakana_core.config import QueryLimitsConfig, RunConfig
from lakana_core.models import AuditEvent, AuditEventType
from lakana_server.api.platform import tables as tables_module
from lakana_server.app import create_app
from lakana_server.concurrency import AppendConcurrencyLimiter
from lakana_server.settings import AppendConfig, Settings
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


def test_the_servers_write_row_cap_applies(settings: Settings) -> None:
    """`LAKANA_QUERY_LIMITS__MAX_WRITE_ROWS` refuses a history write over it, as for an append."""
    capped = settings.model_copy(update={"query_limits": QueryLimitsConfig(max_write_rows=1)})
    with TestClient(create_app(capped)) as client:
        create_environment(client)
        refused = _history(client, [("A", "north"), ("B", "north")], expected_snapshot_id="none")

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == max_rows_message(1)


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


def _free_slots(limiter: AppendConcurrencyLimiter, most: int) -> int:
    """Count the slots a limiter grants right now, then give them back.

    Parameters
    ----------
    limiter
        An `AppendConcurrencyLimiter`.
    most
        The limiter's capacity.

    Returns
    -------
    int
        How many slots were free.
    """
    granted = sum(1 for _ in range(most) if limiter.try_acquire())
    for _ in range(granted):
        limiter.release()
    return granted


def test_the_history_slot_defaults_to_one_and_reads_the_environment(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LAKANA_APPEND__MAX_CONCURRENT_HISTORY` is one by default and must be positive."""
    assert AppendConfig().max_concurrent_history == 1
    monkeypatch.setenv("LAKANA_APPEND__MAX_CONCURRENT_HISTORY", "3")
    loaded = Settings(database=settings.database, secrets=settings.secrets)
    assert loaded.append.max_concurrent_history == 3
    with pytest.raises(ValueError, match="greater than 0"):
        AppendConfig(max_concurrent_history=0)


def test_a_history_write_past_the_history_slot_is_503_and_frees_the_shared_slot(
    client: TestClient,
) -> None:
    """Refused at once, naming the setting; the shared append slot and a later write are fine."""
    create_environment(client)
    history = client.app.state.history_concurrency_limiter  # type: ignore[attr-defined]
    appends = client.app.state.append_concurrency_limiter  # type: ignore[attr-defined]
    before = _free_slots(appends, 64)
    assert history.try_acquire()

    refused = _history(client, [("A", "north")], expected_snapshot_id="none")

    assert refused.status_code == 503, refused.text
    assert refused.headers["Retry-After"] == "1"
    assert refused.json()["type"] == "AppendConcurrencyLimitError"
    assert "LAKANA_APPEND__MAX_CONCURRENT_HISTORY" in refused.json()["detail"]
    assert _free_slots(appends, 64) == before
    history.release()
    assert _history(client, [("A", "north")], expected_snapshot_id="none").status_code == 200


def test_a_failed_history_write_frees_both_slots(client: TestClient) -> None:
    """A write that fails (a refused batch, 422) gives the history and shared slots back."""
    create_environment(client)
    history = client.app.state.history_concurrency_limiter  # type: ignore[attr-defined]
    appends = client.app.state.append_concurrency_limiter  # type: ignore[attr-defined]
    before = _free_slots(appends, 64)

    refused = _history(client, [], expected_snapshot_id="none", close_missing="true")
    assert refused.status_code == 422, refused.text
    assert _free_slots(history, 1) == 1
    assert _free_slots(appends, 64) == before


def test_a_history_write_refused_by_the_shared_slot_frees_the_history_slot(
    settings: Settings,
) -> None:
    """With the shared append slots all taken, the history slot is not leaked."""
    one = settings.model_copy(update={"append": AppendConfig(max_concurrent=1)})
    with TestClient(create_app(one)) as client:
        create_environment(client)
        appends = client.app.state.append_concurrency_limiter  # type: ignore[attr-defined]
        assert appends.try_acquire()

        refused = _history(client, [("A", "north")], expected_snapshot_id="none")

        assert refused.status_code == 503, refused.text
        assert _free_slots(client.app.state.history_concurrency_limiter, 1) == 1  # type: ignore[attr-defined]
        appends.release()
