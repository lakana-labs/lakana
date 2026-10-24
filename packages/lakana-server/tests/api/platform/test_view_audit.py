"""`ACTIVITY_VIEWED` over REST: the query list of a manager and the readers of a table."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lakana_core.config import ViewAuditConfig
from lakana_core.models import AuditEvent, AuditEventType, Principal, Table, TableRead
from lakana_server.api.platform.listing import NEXT_CURSOR_HEADER
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.api.platform.test_queries import _environment, _person, _submit
from tests.conftest import AUTH_HEADER

_QUERIES = "/api/v1/environments/dev/queries"
_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"
_REFUSED = "could not be recorded in the audit log"


def _views(client: TestClient) -> list[AuditEvent]:
    """Read the `ACTIVITY_VIEWED` rows, oldest first.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    list of AuditEvent
        The rows, detached.
    """
    with Session(client.app.state.engine, expire_on_commit=False) as session:
        return list(
            session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == AuditEventType.ACTIVITY_VIEWED)
                .order_by(AuditEvent.occurred_at)
            ).all()
        )


def _refuse_view_rows(client: TestClient) -> None:
    """Make the database refuse every new `ACTIVITY_VIEWED` row, as a full disk would.

    Parameters
    ----------
    client
        The test client.
    """
    with client.app.state.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TRIGGER refuse_views BEFORE INSERT ON audit_events "
                "WHEN NEW.event_type = 'ACTIVITY_VIEWED' "
                "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END"
            )
        )


def _list(client: TestClient, headers: dict[str, str] = AUTH_HEADER, **params: object) -> list:
    """List the queries of `dev`.

    Parameters
    ----------
    client
        The test client.
    headers
        The caller.
    **params
        Query parameters.

    Returns
    -------
    list
        The rows.
    """
    response = client.get(_QUERIES, headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_managers_list_of_someone_elses_query_is_recorded_once(client: TestClient) -> None:
    _environment(client)
    bob = _person(client, "bob", "ANALYST")
    _submit(client)
    _submit(client, bob)
    _submit(client, bob)
    first = client.get(_QUERIES, headers=AUTH_HEADER, params={"limit": 1})
    cursor = first.headers[NEXT_CURSOR_HEADER]
    _list(client, limit=1, cursor=cursor)
    _list(client, status="queued")
    _list(client, include_ended=True)
    # bob lists his own queries and filters on the admin: no row of his
    _list(client, bob)
    _list(client, bob, submitter="admin")
    (row,) = _views(client)
    assert row.securable_environment == "dev"
    assert row.detail["view"] == "queries"
    assert row.detail["targeting_key"] is None
    _list(client, submitter="bob")
    assert [v.detail["targeting_key"] for v in _views(client)] == [None, {"submitter": "bob"}]
    listed = client.get(
        "/api/v1/audit-events", headers=AUTH_HEADER, params={"event_type": "ACTIVITY_VIEWED"}
    )
    assert listed.status_code == 200, listed.text
    assert [e["detail"]["view"] for e in listed.json()["items"]] == ["queries", "queries"]


@pytest.mark.slow
def test_a_list_that_cannot_be_audited_is_a_503_without_rows(client: TestClient) -> None:
    _environment(client)
    query_id = _submit(client, _person(client, "bob", "ANALYST"))
    _refuse_view_rows(client)
    response = client.get(_QUERIES, headers=AUTH_HEADER)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    body = response.json()
    assert body["type"] == "AuditUnavailableError"
    assert _REFUSED in body["detail"] and "Try again in a moment" in body["detail"]
    assert query_id not in response.text and "bob" not in response.text


@pytest.fixture
def quiet_client(settings: Settings) -> Iterator[TestClient]:
    """Provide a client of a server with the view audit turned off.

    Parameters
    ----------
    settings
        The default test settings.

    Yields
    ------
    TestClient
        A client for the running app.
    """
    off = settings.model_copy(update={"view_audit": ViewAuditConfig(enabled=False)})
    with TestClient(create_app(off)) as c:
        yield c


def test_the_setting_turns_the_view_audit_off(quiet_client: TestClient) -> None:
    _environment(quiet_client)
    _submit(quiet_client, _person(quiet_client, "bob", "ANALYST"))
    assert len(_list(quiet_client)) == 1
    _table_with_a_reader(quiet_client)
    assert quiet_client.get(f"{_TABLES}/t/health", headers=AUTH_HEADER).status_code == 200
    assert _views(quiet_client) == []


def _table_with_a_reader(client: TestClient) -> None:
    """Create `gold.t` in `dev` and store one read of it by `bob` (who must exist).

    Parameters
    ----------
    client
        The test client.
    """
    created = client.post(
        _TABLES,
        headers=AUTH_HEADER,
        json={"name": "t", "schema": [{"name": "id", "type": "long", "nullable": False}]},
    )
    assert created.status_code == 201, created.text
    now = datetime.now(UTC)
    with Session(client.app.state.engine) as session:
        session.add(
            TableRead(
                id=uuid.uuid4(),
                table_id=session.scalars(select(Table.id).where(Table.name == "t")).one(),
                principal_id=session.scalars(
                    select(Principal.id).where(Principal.name == "bob")
                ).one(),
                day=now.date(),
                read_count=3,
                last_read_at=now,
            )
        )
        session.commit()


def test_the_readers_of_a_table_are_recorded_once(client: TestClient) -> None:
    _environment(client)
    _person(client, "bob", "ANALYST")
    _table_with_a_reader(client)
    for days in (30, 30, 7):
        response = client.get(f"{_TABLES}/t/health", headers=AUTH_HEADER, params={"days": days})
        assert response.status_code == 200, response.text
        assert [r["principal"] for r in response.json()["readers"]] == ["bob"]
    (row,) = _views(client)
    assert (row.securable_namespace, row.securable_table) == ("gold", "t")
    assert row.detail["view"] == "table_readers"
    assert (row.detail["rows_shown"], row.detail["others_shown"]) == (1, 1)


def test_readers_that_cannot_be_audited_are_a_503(client: TestClient) -> None:
    _environment(client)
    _person(client, "bob", "ANALYST")
    _table_with_a_reader(client)
    _refuse_view_rows(client)
    response = client.get(f"{_TABLES}/t/health", headers=AUTH_HEADER)
    assert response.status_code == 503
    assert _REFUSED in response.json()["detail"]
    assert "bob" not in response.text
