"""The catalog list: `GET /api/v1/tables` and `GET /api/v1/tables/tree`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, AuditEventType, Principal, Table
from lakana_core.pagination import encode_sorted_cursor
from tests.api.platform.test_tables_descriptions import _person
from tests.api.platform.test_tables_health import _add_reads, _health, _table
from tests.conftest import AUTH_HEADER

_TABLE = "/api/v1/environments/dev/namespaces/gold/tables/t"


def _activity_viewed(client: TestClient) -> list[AuditEvent]:
    """Read the `ACTIVITY_VIEWED` audit events.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    list[AuditEvent]
        The events, oldest first.
    """
    with Session(client.app.state.engine) as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == AuditEventType.ACTIVITY_VIEWED)
                .order_by(AuditEvent.occurred_at)
            )
        )


def _principal_id(client: TestClient, name: str) -> uuid.UUID:
    """Look up a principal's id.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.

    Returns
    -------
    uuid.UUID
        The id.
    """
    with Session(client.app.state.engine) as session:
        return session.scalars(select(Principal.id).where(Principal.name == name)).one()


def test_the_list_pages_with_next_cursor_and_capped(client: TestClient) -> None:
    _table(client, "a")
    _table(client, "b")
    body = client.get("/api/v1/tables", params={"limit": 1}, headers=AUTH_HEADER).json()
    assert set(body) == {"items", "next_cursor", "capped"} and len(body["items"]) == 1
    assert {
        "environment",
        "namespace",
        "name",
        "uuid",
        "kind",
        "owner",
        "health_summary",
        "row_count",
        "size_bytes",
        "last_write_at",
        "last_read_at",
        "readers_7d",
        "created_at",
        "match",
        "state",
        "model_kind",
    } == set(body["items"][0])
    second = client.get(
        "/api/v1/tables",
        params={"limit": 1, "cursor": body["next_cursor"]},
        headers=AUTH_HEADER,
    ).json()
    assert second["items"][0]["name"] == "b" and second["next_cursor"] is None


@pytest.mark.parametrize(
    ("params", "needle"),
    [
        ({"sort": "colour"}, "name, owner, health, rows, size, last_write, last_read, created"),
        ({"limit": 201}, "200"),
        ({"q": " "}, "between 1 and 200 characters"),
        ({"cursor": "not-a-cursor"}, "Start the listing again without `cursor`"),
        ({"environment": "a\x00b"}, "environment"),
        ({"namespace": "a\x00b"}, "namespace"),
        ({"q": "a\x00b"}, "`q`"),
    ],
)
def test_a_bad_parameter_is_422_naming_the_next_step(
    client: TestClient, params: dict[str, object], needle: str
) -> None:
    r = client.get("/api/v1/tables", params=params, headers=AUTH_HEADER)
    assert r.status_code == 422 and needle in r.text


def test_a_cursor_of_another_sort_is_422(client: TestClient) -> None:
    _table(client, "a")
    _table(client, "b")
    c = client.get(
        "/api/v1/tables", params={"sort": "rows", "limit": 1}, headers=AUTH_HEADER
    ).json()["next_cursor"]
    r = client.get("/api/v1/tables", params={"sort": "size", "cursor": c}, headers=AUTH_HEADER)
    assert r.status_code == 422 and "Start the listing again without `cursor`" in r.text


@pytest.mark.parametrize(
    ("sort", "value"), [("health:asc", 2**15), ("rows:desc", 2**63), ("size:desc", -(2**63) - 1)]
)
def test_a_forged_integer_cursor_is_422(client: TestClient, sort: str, value: int) -> None:
    _table(client, "a")
    key, order = sort.split(":")
    cursor = encode_sorted_cursor("tables", sort, (value,), uuid.uuid4())
    r = client.get(
        "/api/v1/tables",
        params={"sort": key, "order": order, "cursor": cursor},
        headers=AUTH_HEADER,
    )
    assert r.status_code == 422 and "Start the listing again without `cursor`" in r.text


def test_reader_names_stay_with_administrators(client: TestClient) -> None:
    _table(client)
    _person(client, "bob", "READ_TABLE")
    rita = _person(client, "rita", "READ_TABLE")
    _add_reads(client, _principal_id(client, "bob"), 0, 2)
    with Session(client.app.state.engine) as session:
        session.execute(update(Table).values(last_read_at=datetime.now(UTC)))
        session.commit()
    for headers in (rita, AUTH_HEADER):
        for url in ("/api/v1/tables", _TABLE):
            r = client.get(url, headers=headers)
            assert r.status_code == 200 and "bob" not in r.text
    assert client.get(_TABLE, headers=rita).json()["readers_7d"] == 1
    assert client.get(_TABLE, headers=rita).json()["last_read_at"] is not None
    assert _health(client, headers=rita).json()["readers"] is None
    assert [x["principal"] for x in _health(client).json()["readers"]] == ["bob"]


def test_the_owner_is_shown_to_anyone_who_may_read_the_table(client: TestClient) -> None:
    _table(client)
    rita = _person(client, "rita", "READ_TABLE")
    for url in ("/api/v1/tables", _TABLE):
        body = client.get(url, headers=rita).json()
        body = body["items"][0] if url == "/api/v1/tables" else body
        assert body["owner"] == {"name": "admin", "kind": "USER"}


def test_the_last_read_and_its_count_are_not_audited_as_activity_viewed(
    client: TestClient,
) -> None:
    _table(client)
    _person(client, "bob", "READ_TABLE")
    _add_reads(client, _principal_id(client, "bob"), 0, 2)
    client.get("/api/v1/tables", headers=AUTH_HEADER)
    client.get(_TABLE, headers=AUTH_HEADER)
    assert _activity_viewed(client) == []
    _health(client)
    assert [e.detail["view"] for e in _activity_viewed(client)] == ["table_readers"]


def test_the_tree_answers_counts_per_node(client: TestClient) -> None:
    _table(client, "a")
    _table(client, "b")
    tree = client.get("/api/v1/tables/tree", headers=AUTH_HEADER).json()
    assert tree == {
        "count": 2,
        "environments": [
            {
                "name": "dev",
                "count": 2,
                "namespaces": [
                    {"path": "bronze", "count": 0},
                    {"path": "gold", "count": 2},
                    {"path": "silver", "count": 0},
                ],
            }
        ],
    }


def test_a_missing_environment_is_404_for_an_administrator(client: TestClient) -> None:
    r = client.get("/api/v1/tables", params={"environment": "nope"}, headers=AUTH_HEADER)
    assert r.status_code == 404 and "lakana env list" in r.text


def test_a_grant_added_between_two_requests_is_seen_by_the_second(client: TestClient) -> None:
    _table(client)
    rita = _person(client, "rita")
    assert client.get("/api/v1/tables", headers=rita).json()["items"] == []
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "rita",
            "action": "READ_TABLE",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert granted.status_code < 300, granted.text
    assert [i["name"] for i in client.get("/api/v1/tables", headers=rita).json()["items"]] == ["t"]
    assert client.get("/api/v1/tables/tree", headers=rita).json()["count"] == 1
