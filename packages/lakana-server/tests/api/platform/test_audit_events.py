"""Tests for the audit-events resource of the platform API."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lakana_core.audit.content import fingerprint, install_query_text, stored_query_text
from lakana_core.audit.service import AuditService
from lakana_core.auth import Securable
from lakana_core.models import AuditEvent, AuditEventType, Principal
from lakana_server.app import create_app
from lakana_server.settings import ServerAuditConfig, Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_namespace


def test_list_audit_events_requires_read_audit_log(client: TestClient, storage_root: str) -> None:
    """A caller with an unrelated grant still gets 403 -- READ_AUDIT_LOG is not in ANALYST's bundle.

    Grants `bob` `Role.ANALYST` on `dev` first and proves that grant genuinely
    works (a `READ_ENVIRONMENT`-gated endpoint returns 200), so the 403 below
    is pinned to a `READ_AUDIT_LOG`-specific denial rather than a blanket
    "you have zero grants" refusal that a no-grants-at-all principal (like
    the one this test used to use) couldn't distinguish from.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "SERVICE_ACCOUNT"}
    )
    issued = client.post("/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "bob"})
    bob_headers = {"Authorization": f"Bearer {issued.json()['token']}"}
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={"name": "dev", "storage": {"scheme": "file", "root": storage_root}},
    )
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "bob", "role": "ANALYST", "kind": "ENVIRONMENT", "environment": "dev"},
    )

    unrelated = client.get("/api/v1/environments/dev", headers=bob_headers)
    assert unrelated.status_code == 200, unrelated.text

    response = client.get("/api/v1/audit-events", headers=bob_headers)

    assert response.status_code == 403
    assert "READ_AUDIT_LOG" in response.json()["detail"]


@pytest.mark.slow
def test_list_audit_events_returns_recorded_events(client: TestClient) -> None:
    """The bootstrap admin (Role.ADMIN, which bundles every Action) can read events back."""
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "alice", "type": "SERVICE_ACCOUNT"}
    )
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "alice", "role": "ANALYST", "kind": "ENVIRONMENT", "environment": "dev"},
    )

    response = client.get("/api/v1/audit-events", headers=AUTH_HEADER)

    assert response.status_code == 200
    body = response.json()["items"]
    assert any(e["event_type"] == "GRANT_CREATED" for e in body)


def test_list_audit_events_filters_by_event_type(client: TestClient) -> None:
    """?event_type=... narrows the result set."""
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "carol", "type": "SERVICE_ACCOUNT"}
    )
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "carol", "role": "VIEWER", "kind": "ENVIRONMENT", "environment": "dev"},
    )

    response = client.get("/api/v1/audit-events?event_type=GRANT_CREATED", headers=AUTH_HEADER)

    assert response.status_code == 200
    body = response.json()["items"]
    assert len(body) > 0
    assert all(e["event_type"] == "GRANT_CREATED" for e in body)


def test_list_audit_events_filters_by_environment(client: TestClient, storage_root: str) -> None:
    """?environment=... narrows the result set."""
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={"name": "dev", "storage": {"scheme": "file", "root": storage_root}},
    )
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"k": "v"}},
    )

    response = client.get("/api/v1/audit-events?environment=dev", headers=AUTH_HEADER)

    assert response.status_code == 200
    body = response.json()["items"]
    assert len(body) > 0
    assert all(e["securable_environment"] == "dev" for e in body)


def test_list_audit_events_reports_namespace_in_dotted_form(client: TestClient) -> None:
    """A namespace-scoped event's securable_namespace crosses the API dotted, not joined."""
    create_environment(client)
    create_namespace(client, "dev", "gold.sales")
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "dave", "type": "USER"})
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "dave",
            "role": "VIEWER",
            "kind": "NAMESPACE",
            "environment": "dev",
            "namespace": "gold.sales",
        },
    )

    response = client.get("/api/v1/audit-events?event_type=GRANT_CREATED", headers=AUTH_HEADER)

    assert response.status_code == 200
    body = response.json()["items"]
    matches = [e for e in body if e["securable_namespace"] == "gold.sales"]
    assert len(matches) > 0


def test_list_audit_events_limit_is_honored(client: TestClient) -> None:
    """?limit=... caps how many events come back."""
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "frank", "type": "SERVICE_ACCOUNT"}
    )
    for i in range(3):
        client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={
                "principal": "frank",
                "action": "READ_TABLE",
                "kind": "TABLE",
                "environment": "dev",
                "namespace": "bronze",
                "table": f"orders{i}",
            },
        )

    response = client.get("/api/v1/audit-events?limit=1", headers=AUTH_HEADER)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


def test_list_audit_events_rejects_out_of_range_limit(client: TestClient) -> None:
    """A limit outside [1, 500] is a clean 422, not silently clamped or ignored."""
    too_low = client.get("/api/v1/audit-events?limit=0", headers=AUTH_HEADER)
    too_high = client.get("/api/v1/audit-events?limit=1000", headers=AUTH_HEADER)

    assert too_low.status_code == 422
    assert too_high.status_code == 422


def test_list_audit_events_filters_by_since(client: TestClient) -> None:
    """?since=... narrows the result set to events at or after that time."""
    before = datetime.now(UTC).isoformat()
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "erin", "type": "SERVICE_ACCOUNT"}
    )
    client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "erin", "role": "VIEWER", "kind": "ENVIRONMENT", "environment": "dev"},
    )
    far_future = datetime(2999, 1, 1, tzinfo=UTC).isoformat()

    inclusive = client.get("/api/v1/audit-events", headers=AUTH_HEADER, params={"since": before})
    assert inclusive.status_code == 200
    assert len(inclusive.json()["items"]) > 0

    exclusive = client.get(
        "/api/v1/audit-events", headers=AUTH_HEADER, params={"since": far_future}
    )
    assert exclusive.status_code == 200
    assert exclusive.json() == {"items": [], "next_cursor": None}


def _create_principals(client: TestClient, n: int) -> None:
    """Record `n` `PRINCIPAL_CREATED` events by creating `n` principals.

    Parameters
    ----------
    client
        The test client.
    n
        How many principals, and so how many events, to create.
    """
    for i in range(n):
        response = client.post(
            "/api/v1/principals",
            headers=AUTH_HEADER,
            json={"name": f"p{i}", "type": "SERVICE_ACCOUNT"},
        )
        assert response.status_code == 201, response.text


def test_list_audit_events_pages_through_every_event_with_a_cursor(client: TestClient) -> None:
    """Following `next_cursor` visits each matching event exactly once, then stops."""
    _create_principals(client, 5)
    params = {"event_type": "PRINCIPAL_CREATED"}
    everything = client.get("/api/v1/audit-events", headers=AUTH_HEADER, params=params).json()
    assert everything["next_cursor"] is None
    expected = [e["id"] for e in everything["items"]]
    assert len(expected) == 6  # the five above, plus the bootstrap admin's own

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        page_params = {**params, "limit": "2"} | ({"cursor": cursor} if cursor else {})
        response = client.get("/api/v1/audit-events", headers=AUTH_HEADER, params=page_params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) <= 2
        seen.extend(e["id"] for e in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == expected


def test_list_audit_events_has_no_next_cursor_when_the_page_is_exactly_full(
    client: TestClient,
) -> None:
    """A page holding exactly the last `limit` events reports no further page."""
    _create_principals(client, 1)  # two PRINCIPAL_CREATED events, with the bootstrap admin's

    response = client.get(
        "/api/v1/audit-events",
        headers=AUTH_HEADER,
        params={"event_type": "PRINCIPAL_CREATED", "limit": "2"},
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 2
    assert response.json()["next_cursor"] is None


@pytest.mark.parametrize(
    "cursor",
    [
        "not-base64!",
        "e30",  # base64url of "{}"
        "eyJvIjogMSwgImkiOiAyfQ",  # {"o": 1, "i": 2}
        # {"o": "2026-01-01T00:00:00", "i": "<uuid>"}: a naive timestamp
        "eyJvIjoiMjAyNi0wMS0wMVQwMDowMDowMCIsImkiOiIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDAifQ",
        # {"o": "2026-01-01T00:00:00+00:00", "i": 1}: a number as the id
        "eyJvIjogIjIwMjYtMDEtMDFUMDA6MDA6MDArMDA6MDAiLCAiaSI6IDF9",
        "W10",  # []: not an object
        # {"o": "0001-01-01T00:00:00+23:59", "i": "<uuid>"}: overflows when converted to UTC
        "eyJvIjogIjAwMDEtMDEtMDFUMDA6MDA6MDArMjM6NTkiLCAiaSI6ICIwMDAwMDAwMC0wMDAwLTAwMDAtMDAwMC0wMDAwMDAwMDAwMDAifQ",
    ],
)
def test_list_audit_events_rejects_a_malformed_cursor(client: TestClient, cursor: str) -> None:
    """A cursor this server didn't issue is a clean 422, never a 500."""
    response = client.get("/api/v1/audit-events", headers=AUTH_HEADER, params={"cursor": cursor})

    assert response.status_code == 422, response.text
    assert "cursor" in response.json()["detail"].lower()


# --- who sees other people's query text, and the record of each read (#88) -------------------

_SQL = "SELECT email FROM customers WHERE name = 'Alice Example'"


def _bobs_query(client: TestClient) -> None:
    """Create `bob` and record a `QUERY_COMPLETED` of his with `_SQL`, as a worker does.

    Parameters
    ----------
    client
        The test client.
    """
    created = client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"}
    )
    assert created.status_code == 201, created.text
    with Session(client.app.state.engine) as session:
        bob = session.scalars(select(Principal.id).where(Principal.name == "bob")).one()
        AuditService(session).record(
            bob,
            AuditEventType.QUERY_COMPLETED,
            securable=Securable.environment_scope("dev"),
            detail={"sql_text": stored_query_text(_SQL), "tables_touched": [], "row_count": 1},
            commit=True,
        )


def _sql_seen(client: TestClient) -> object:
    """Read the `sql_text` of the newest `QUERY_COMPLETED` as the administrator.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    object
        The field as the answer has it.
    """
    response = client.get(
        "/api/v1/audit-events", headers=AUTH_HEADER, params={"event_type": "QUERY_COMPLETED"}
    )
    assert response.status_code == 200, response.text
    return response.json()["items"][0]["detail"]["sql_text"]


def test_an_administrator_sees_other_peoples_sql_only_with_the_right(client: TestClient) -> None:
    _bobs_query(client)
    assert _sql_seen(client) == fingerprint(_SQL)
    explained = client.get("/api/v1/principals/admin/permissions", headers=AUTH_HEADER).json()
    [right] = [p for p in explained["permissions"] if p["action"] == "READ_QUERY_TEXT"]
    assert right["allowed"] is False
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "admin", "action": "READ_QUERY_TEXT", "kind": "INSTANCE"},
    )
    assert granted.status_code == 201, granted.text
    assert _sql_seen(client) == _SQL
    revoked = client.delete(f"/api/v1/grants/{granted.json()['id']}", headers=AUTH_HEADER)
    assert revoked.status_code == 204, revoked.text
    assert _sql_seen(client) == fingerprint(_SQL)


def test_the_right_on_an_environment_is_refused_with_the_command_to_use(
    client: TestClient, storage_root: str
) -> None:
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={"name": "dev", "storage": {"scheme": "file", "root": storage_root}},
    )
    response = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "action": "READ_QUERY_TEXT",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert response.status_code == 422, response.text
    assert "--kind INSTANCE" in response.json()["detail"]


def test_a_read_of_other_peoples_events_is_recorded_and_fails_closed(client: TestClient) -> None:
    _bobs_query(client)
    assert client.get("/api/v1/audit-events", headers=AUTH_HEADER).status_code == 200
    with Session(client.app.state.engine) as session:
        views = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.ACTIVITY_VIEWED)
        ).all()
        assert [v.detail["view"] for v in views] == ["audit_log"]
    with client.app.state.engine.begin() as connection:
        connection.execute(text("DELETE FROM audit_events WHERE event_type = 'ACTIVITY_VIEWED'"))
        connection.execute(
            text(
                "CREATE TRIGGER refuse_views BEFORE INSERT ON audit_events "
                "WHEN NEW.event_type = 'ACTIVITY_VIEWED' "
                "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END"
            )
        )
    response = client.get("/api/v1/audit-events", headers=AUTH_HEADER)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert "could not be recorded in the audit log" in response.json()["detail"]
    assert "sql_text" not in response.text


@pytest.fixture
def hashing_client(settings: Settings) -> Iterator[TestClient]:
    """Provide a client of a server with `LAKANA_AUDIT__QUERY_TEXT=hash`.

    Parameters
    ----------
    settings
        The default test settings.

    Yields
    ------
    TestClient
        A client for the running app; the process's mode is set back to `full` after it.
    """
    hashing = settings.model_copy(update={"audit": ServerAuditConfig(query_text="hash")})
    try:
        with TestClient(create_app(hashing)) as c:
            yield c
    finally:
        install_query_text("full")


def test_the_server_stores_what_the_setting_says(hashing_client: TestClient) -> None:
    assert stored_query_text(_SQL) == fingerprint(_SQL)
    _bobs_query(hashing_client)
    granted = hashing_client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "admin", "action": "READ_QUERY_TEXT", "kind": "INSTANCE"},
    )
    assert granted.status_code == 201, granted.text
    assert _sql_seen(hashing_client) == fingerprint(_SQL)
