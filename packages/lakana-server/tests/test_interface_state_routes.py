"""Tests for a person's interface state and recently opened list over REST."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import Environment, Namespace, Table, TableType
from tests.api.platform.test_whoami import _new_person, _task_token_header
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_STATE = "/api/v1/principals/admin/interface-state"
_RECENT = "/api/v1/principals/admin/recent"


def _table(client: TestClient, name: str = "sales") -> str:
    """Insert a table into `dev`'s `gold` namespace.

    Parameters
    ----------
    client
        The test client; its app's engine is used.
    name
        The table's name.

    Returns
    -------
    str
        Its dotted name.
    """
    with Session(client.app.state.engine) as session:
        namespace = session.execute(
            select(Namespace.id)
            .join(Environment, Environment.id == Namespace.environment_id)
            .where(Environment.name == "dev", Namespace.path == "gold")
        ).scalar_one()
        session.add(
            Table(id=uuid.uuid4(), namespace_id=namespace, name=name, table_type=TableType.ICEBERG)
        )
        session.commit()
    return f"gold.{name}"


def test_interface_state_defaults_and_changes_only_what_is_sent(client: TestClient) -> None:
    defaults = {"tour_seen": False, "get_started_hidden": False, "update_note_seen": None}
    assert client.get(_STATE, headers=AUTH_HEADER).json() == defaults
    changed = client.patch(_STATE, headers=AUTH_HEADER, json={"tour_seen": True})
    assert changed.status_code == 200, changed.text
    assert changed.json() == {**defaults, "tour_seen": True}
    client.patch(_STATE, headers=AUTH_HEADER, json={"update_note_seen": "0.23.0"})
    assert client.patch(_STATE, headers=AUTH_HEADER, json={}).json() == {
        **defaults,
        "tour_seen": True,
        "update_note_seen": "0.23.0",
    }
    cleared = client.patch(
        _STATE, headers=AUTH_HEADER, json={"update_note_seen": None, "get_started_hidden": True}
    )
    assert cleared.json() == {**defaults, "tour_seen": True, "get_started_hidden": True}


def test_a_development_version_is_422_and_changes_nothing(client: TestClient) -> None:
    refused = client.patch(
        _STATE, headers=AUTH_HEADER, json={"tour_seen": True, "update_note_seen": "0.22.0.dev3"}
    )
    assert refused.status_code == 422
    assert "x.y.z" in refused.json()["detail"]
    assert client.get(_STATE, headers=AUTH_HEADER).json()["tour_seen"] is False
    # The tour on its own is never held up by a version.
    alone = client.patch(_STATE, headers=AUTH_HEADER, json={"tour_seen": True})
    assert alone.json()["tour_seen"] is True


def test_another_persons_state_and_list_are_refused(client: TestClient) -> None:
    header = _new_person(client, "kim")
    for response in (
        client.get(_STATE, headers=header),
        client.patch(_STATE, headers=header, json={"tour_seen": True}),
        client.get(_RECENT, headers=header),
        client.post(
            _RECENT, headers=header, json={"kind": "table", "environment": "dev", "ref": "a.b"}
        ),
    ):
        assert response.status_code == 403
        assert "your own" in response.json()["detail"]
    assert client.get("/api/v1/principals/kim/interface-state", headers=header).status_code == 200


def test_a_task_token_may_not_read_or_change_interface_state(client: TestClient) -> None:
    header = _task_token_header(client)
    assert client.get(_STATE, headers=header).status_code == 403
    response = client.patch(_STATE, headers=header, json={"tour_seen": True})
    assert response.status_code == 403
    assert "personal access token" in response.json()["detail"]


def test_interface_state_needs_a_credential(client: TestClient) -> None:
    assert client.get(_STATE).status_code == 401
    assert client.get(_RECENT).status_code == 401
    assert client.get("/api/v1/home/get-started").status_code == 401


def test_recent_records_and_lists_what_the_person_may_read(client: TestClient) -> None:
    create_environment(client, "dev")
    table = _table(client)
    recorded = client.post(
        _RECENT, headers=AUTH_HEADER, json={"kind": "table", "environment": "dev", "ref": table}
    )
    assert recorded.status_code == 204, recorded.text
    rows = client.get(_RECENT, headers=AUTH_HEADER).json()
    assert [(r["kind"], r["environment"], r["ref"]) for r in rows] == [("table", "dev", table)]
    assert rows[0]["opened_at"]


def test_recent_refuses_what_the_person_cannot_read_as_404(client: TestClient) -> None:
    create_environment(client, "dev")
    table = _table(client)
    header = _new_person(client, "kim")
    response = client.post(
        "/api/v1/principals/kim/recent",
        headers=header,
        json={"kind": "table", "environment": "dev", "ref": table},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "No table gold.sales in dev that you can read."


def test_recent_refuses_an_unknown_kind_and_a_long_ref(client: TestClient) -> None:
    for body in (
        {"kind": "chart", "environment": "dev", "ref": "x"},
        {"kind": "table", "environment": "dev", "ref": "x" * 1025},
        {"kind": "table", "environment": "d" * 256, "ref": "x"},
    ):
        assert client.post(_RECENT, headers=AUTH_HEADER, json=body).status_code == 422
