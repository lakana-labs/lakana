"""Tests for `GET /api/v1/status`: the instance status Home and `lakana status` show."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.models import Worker
from lakana_core.readiness import DatabaseFindings
from lakana_server.readiness import ReadinessCheck
from tests.api.platform.test_deploys import _token_for
from tests.conftest import AUTH_HEADER

_STATUS = "/api/v1/status"
_GIB = 1024**3


def _add_worker(
    client: TestClient,
    worker_id: str,
    *,
    age_seconds: int = 0,
    used: int | None = None,
    limit: int | None = None,
) -> None:
    """Insert a worker whose heartbeat is this old, with its memory figures.

    Parameters
    ----------
    client
        The test client; its app's engine is used.
    worker_id
        The worker's id.
    age_seconds
        The age of the heartbeat; 60 s or more makes the worker lost.
    used
        The memory it reported using, if any.
    limit
        Its memory limit.
    """
    with Session(client.app.state.engine) as session:
        beat = db_now(session) - timedelta(seconds=age_seconds)
        session.add(
            Worker(
                id=worker_id,
                host="box",
                version="1.0",
                started_at=beat,
                heartbeat_at=beat,
                memory_used_bytes=used,
                memory_limit_bytes=limit,
                memory_measured_at=None if used is None else beat,
            )
        )
        session.commit()


def _readiness(client: TestClient, failed: tuple[str, ...] = ()) -> list[int]:
    """Put a readiness check with a counting database read in place of the real one.

    Parameters
    ----------
    client
        The test client.
    failed
        `("database",)` to make the read fail; otherwise every check passes.

    Returns
    -------
    list of int
        One element, the number of database reads so far.
    """
    reads = [0]

    def read() -> DatabaseFindings:
        reads[0] += 1
        if "database" in failed:
            raise OSError("refused")
        return DatabaseFindings(worker_alive=True, storages=())

    client.app.state.readiness = ReadinessCheck(
        read=read, probe=lambda storage: None, timeout_seconds=5.0, cache_seconds=60.0
    )
    return reads


def test_status_answers_a_reader_without_detail(client: TestClient) -> None:
    _readiness(client)
    _add_worker(client, "w-1", used=10 * _GIB, limit=10 * _GIB)
    _add_worker(client, "w-2", age_seconds=120)
    response = client.get(_STATUS, headers=_token_for(client, "reader"))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "state": "degraded",
        "causes": ["worker_lost"],  # memory_high is for administrators only
        "workers_alive": 1,
        "workers_total": 2,
        "detail": None,
    }
    assert client.get(_STATUS).status_code == 401


def test_status_gives_admins_slots_memory_and_workers(client: TestClient) -> None:
    _readiness(client)
    _add_worker(client, "w-1", used=_GIB, limit=4 * _GIB)
    _add_worker(client, "w-2", used=2 * _GIB, limit=4 * _GIB)
    body = client.get(_STATUS, headers=AUTH_HEADER).json()
    assert (body["state"], body["causes"]) == ("healthy", [])
    detail = body["detail"]
    assert {key: detail[key] for key in detail if key != "workers"} == {
        "slots_busy": 0,
        "slots_total": 4,
        "memory_used_bytes": 3 * _GIB,
        "memory_limit_bytes": 8 * _GIB,
    }
    assert [w["id"] for w in detail["workers"]] == ["w-1", "w-2"]
    assert detail["workers"][0]["memory"]["used_bytes"] == _GIB


def test_status_names_a_failed_check_to_admins(client: TestClient) -> None:
    _readiness(client, failed=("database",))
    _add_worker(client, "w-1")
    body = client.get(_STATUS, headers=AUTH_HEADER).json()
    assert (body["state"], body["causes"]) == ("down", ["database"])
    reader = client.get(_STATUS, headers=_token_for(client, "reader")).json()
    assert (reader["state"], reader["causes"]) == ("down", [])


def test_status_reuses_the_readiness_answer(client: TestClient) -> None:
    reads = _readiness(client)
    _add_worker(client, "w-1")
    for _ in range(2):
        assert client.get(_STATUS, headers=AUTH_HEADER).status_code == 200
    assert reads == [1]


def test_worker_list_carries_memory(client: TestClient) -> None:
    _add_worker(client, "w-1", used=_GIB, limit=None)
    _add_worker(client, "w-2")
    rows = client.get("/api/v1/workers", headers=AUTH_HEADER).json()
    memory = {row["id"]: row["memory"] for row in rows}
    assert memory["w-2"] is None
    assert memory["w-1"] is not None
    assert (memory["w-1"]["used_bytes"], memory["w-1"]["limit_bytes"]) == (_GIB, None)
    assert memory["w-1"]["measured_at"] is not None
