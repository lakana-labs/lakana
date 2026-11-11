"""Tests for `GET /api/v1/home/attention` and `GET /api/v1/home/work`."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.models import Environment, Worker
from tests.api.platform.test_whoami import _new_person
from tests.conftest import AUTH_HEADER

_ATTENTION = "/api/v1/home/attention"
_WORK = "/api/v1/home/work"


@pytest.mark.parametrize("path", [_ATTENTION, _WORK])
def test_home_routes_answer_401_without_credential(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_an_administrator_sees_a_lost_worker(client: TestClient) -> None:
    with Session(client.app.state.engine) as session:
        beat = db_now(session) - timedelta(minutes=10)
        session.add(Worker(id="w-1", host="box", version="1.0", started_at=beat, heartbeat_at=beat))
        session.commit()
    response = client.get(_ATTENTION, headers=AUTH_HEADER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["truncated"] is False
    assert [
        (item["kind"], item["environment"], item["title"], item["ref"]) for item in body["items"]
    ] == [("worker_lost", None, "box", "w-1")]
    assert body["items"][0]["at"]


def test_a_person_without_access_has_nothing_to_attend_to_and_no_work(
    client: TestClient,
) -> None:
    header = _new_person(client, "kim")
    assert client.get(_ATTENTION, headers=header).json() == {"items": [], "truncated": False}
    assert client.get(_WORK, headers=header).json() == {"repositories": [], "jobs": []}


def test_an_administrator_sees_the_repositories_they_created(client: TestClient) -> None:
    with Session(client.app.state.engine) as session:
        session.add(
            Environment(
                id=uuid.uuid4(), name="dev", storage_root="file:///dev/null", storage_options={}
            )
        )
        session.commit()
    created = client.post(
        "/api/v1/repositories",
        headers=AUTH_HEADER,
        json={"name": "shop", "kind": "https", "url": "https://git.example.com/shop.git"},
    )
    assert created.status_code == 201, created.text
    body = client.get(_WORK, headers=AUTH_HEADER).json()
    assert body == {"repositories": [{"name": "shop", "environments": []}], "jobs": []}
