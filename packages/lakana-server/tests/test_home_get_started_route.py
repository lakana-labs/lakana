"""Tests for `GET /api/v1/home/get-started`."""

from __future__ import annotations

from fastapi.testclient import TestClient

from lakana_server.settings import UiConfig
from tests.api.platform.test_whoami import _new_person
from tests.conftest import AUTH_HEADER

_GET_STARTED = "/api/v1/home/get-started"


def test_an_administrator_gets_the_team_steps(client: TestClient) -> None:
    response = client.get(_GET_STARTED, headers=AUTH_HEADER)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "audience": "admin",
        "local": False,
        "steps": [
            {"id": "connect_repository", "done": False},
            {"id": "invite_team", "done": False},
            {"id": "add_passkey", "done": False},
            {"id": "deploy_project", "done": False},
            {"id": "ci_deploy", "done": False},
        ],
        "hidden": False,
        "complete": False,
    }


def test_local_mode_gets_the_local_steps(client: TestClient) -> None:
    settings = client.app.state.settings
    client.app.state.settings = settings.model_copy(update={"ui": UiConfig(local_sign_in=True)})
    body = client.get(_GET_STARTED, headers=AUTH_HEADER).json()
    assert (body["local"], [step["id"] for step in body["steps"]]) == (
        True,
        ["add_passkey", "deploy_project_local", "open_table", "run_query"],
    )


def test_hiding_get_started_shows_in_the_answer(client: TestClient) -> None:
    client.patch(
        "/api/v1/principals/admin/interface-state",
        headers=AUTH_HEADER,
        json={"get_started_hidden": True},
    )
    assert client.get(_GET_STARTED, headers=AUTH_HEADER).json()["hidden"] is True


def test_a_person_without_access_gets_no_steps(client: TestClient) -> None:
    body = client.get(_GET_STARTED, headers=_new_person(client, "kim")).json()
    assert (body["audience"], body["steps"], body["complete"]) == ("none", [], False)
