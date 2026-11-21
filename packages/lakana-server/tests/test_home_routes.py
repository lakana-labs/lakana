"""Tests for `GET /api/v1/home/attention` and `GET /api/v1/home/work`."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.models import (
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    Repository,
    RepositoryKind,
    Worker,
)
from tests.api.platform.test_runs import _failed_run
from tests.api.platform.test_whoami import _new_person, _task_token_header
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
    assert (body["items"][0]["count"], body["items"][0]["since"]) == (1, None)


def test_failed_deploys_in_a_row_are_one_row_with_their_count(client: TestClient) -> None:
    with Session(client.app.state.engine) as session:
        env_id, repo_id = uuid.uuid4(), uuid.uuid4()
        session.add(
            Environment(id=env_id, name="prod", storage_root="file:///dev/null", storage_options={})
        )
        session.add(
            Repository(
                id=repo_id,
                name="analytics",
                kind=RepositoryKind.HTTPS,
                url="https://git.example.com/analytics.git",
            )
        )
        session.flush()
        now = db_now(session)
        deploys = []
        for hours in (3, 1):
            deploy = DeployRun(
                id=uuid.uuid4(),
                environment_id=env_id,
                repository_id=repo_id,
                kind=DeployKind.DEPLOY,
                status=DeployStatus.FAILED,
                error_message="lakana.yaml: line 3: unknown key",
                created_at=now - timedelta(hours=hours),
                finished_at=now - timedelta(hours=hours),
            )
            session.add(deploy)
            deploys.append(deploy)
        session.commit()
        newest = str(deploys[-1].id)
    body = client.get(_ATTENTION, headers=AUTH_HEADER).json()
    assert [
        (item["kind"], item["environment"], item["title"], item["ref"], item["count"])
        for item in body["items"]
    ] == [("deploy_failed", "prod", "analytics", newest, 2)]
    assert body["items"][0]["since"] is not None


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


def test_a_task_token_of_an_administrator_gets_no_home(client: TestClient) -> None:
    header = _task_token_header(client)
    for path in (_ATTENTION, _WORK, "/api/v1/home/get-started"):
        response = client.get(path, headers=header)
        assert response.status_code == 403, path
        assert "personal access token" in response.json()["detail"]
    status = client.get("/api/v1/status", headers=header)
    assert (status.status_code, status.json()["detail"]) == (200, None)


def test_attention_lists_failed_runs_for_a_reader(client: TestClient) -> None:
    run_id = _failed_run(client)
    header = _new_person(client, "rita")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "rita",
            "action": "READ_ENVIRONMENT",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert granted.status_code in (200, 201), granted.text
    body = client.get(_ATTENTION, headers=header).json()
    assert [(item["kind"], item["environment"], item["ref"]) for item in body["items"]] == [
        ("run_failed", "dev", run_id)
    ]
    failed = client.get("/api/v1/runs/failed", headers=header).json()
    assert [row["run"]["id"] for row in failed["items"]] == [run_id]
