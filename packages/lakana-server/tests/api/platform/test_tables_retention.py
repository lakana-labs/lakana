"""The retention routes: a table's, an environment's, the expiring listing and the warning.

The catalog's own tests (`lakana-core/tests/test_table_retention.py`) cover the clock, the
rights and the sweep; these cover the routes, their shapes and status codes, the audited
refusal, and `LAKANA_TABLES__EXPIRY_WARNING_DAYS`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, AuditEventType
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_table

_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"
_EXPIRING = "/api/v1/environments/dev/expiring-tables"


def _person(client: TestClient, name: str, *actions: str) -> dict[str, str]:
    """Create a user holding only `actions` on `dev` and return its headers.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    *actions
        Actions granted at environment scope.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    for action in actions:
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={"principal": name, "action": action, "kind": "ENVIRONMENT", "environment": "dev"},
        )
        assert granted.status_code < 300, granted.text
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _events(client: TestClient, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one kind.

    Parameters
    ----------
    client
        The test client, whose app holds the engine.
    kind
        The event type.

    Returns
    -------
    list of AuditEvent
        Detached events.
    """
    with Session(client.app.state.engine) as session:
        found = list(session.scalars(select(AuditEvent).where(AuditEvent.event_type == kind)))
        session.expunge_all()
        return found


def test_a_table_has_no_retention_until_one_is_set(client: TestClient) -> None:
    """GET shows none; the table shows no expiry."""
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    shown = client.get(f"{_TABLES}/sales/retention", headers=AUTH_HEADER)
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert (body["source"], body["days"], body["on"], body["never"]) == (None, None, None, False)
    assert (body["expires_at"], body["kept"]) == (None, None)
    assert (body["name"], body["namespace"]) == ("sales", "gold")
    table = client.get(f"{_TABLES}/sales", headers=AUTH_HEADER).json()
    assert (table["expires_at"], table["expires_soon"]) == (None, False)


def test_put_sets_days_a_day_or_never(client: TestClient) -> None:
    """Each rule is stored, shown, and audited."""
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    url = f"{_TABLES}/sales/retention"

    days = client.put(url, headers=AUTH_HEADER, json={"days": 3})
    assert days.status_code == 200, days.text
    assert (days.json()["source"], days.json()["days"], days.json()["set_by"]) == (
        "table",
        3,
        "admin",
    )
    expires = datetime.fromisoformat(days.json()["expires_at"])
    assert abs(expires - (datetime.now(UTC) + timedelta(days=3))) < timedelta(minutes=1)
    table = client.get(f"{_TABLES}/sales", headers=AUTH_HEADER).json()
    assert table["expires_soon"] is True
    assert client.get(_TABLES, headers=AUTH_HEADER).json()[0]["expires_soon"] is True

    day = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
    on = client.put(url, headers=AUTH_HEADER, json={"on": day})
    assert on.status_code == 200, on.text
    assert on.json()["on"] == day
    assert client.get(f"{_TABLES}/sales", headers=AUTH_HEADER).json()["expires_soon"] is False

    never = client.put(url, headers=AUTH_HEADER, json={"never": True})
    assert never.status_code == 200, never.text
    assert (never.json()["never"], never.json()["expires_at"]) == (True, None)
    assert len(_events(client, AuditEventType.TABLE_RETENTION_SET)) == 2
    assert len(_events(client, AuditEventType.TABLE_RETENTION_CLEARED)) == 1


def test_a_bad_rule_is_422_and_names_the_options(client: TestClient) -> None:
    """None or two rules, days out of range, an unknown field."""
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    url = f"{_TABLES}/sales/retention"
    none = client.put(url, headers=AUTH_HEADER, json={})
    assert none.status_code == 422 and "--days" in none.text
    both = client.put(url, headers=AUTH_HEADER, json={"days": 3, "never": True})
    assert both.status_code == 422
    assert client.put(url, headers=AUTH_HEADER, json={"days": 0}).status_code == 422
    assert client.put(url, headers=AUTH_HEADER, json={"weeks": 1}).status_code == 422


def test_a_writer_may_extend_but_not_shorten_and_the_refusal_is_audited(client: TestClient) -> None:
    """`WRITE_TABLE` extends; shortening needs `DROP_TABLE` and is refused with 403."""
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    url = f"{_TABLES}/sales/retention"
    assert client.put(url, headers=AUTH_HEADER, json={"days": 10}).status_code == 200
    writer = _person(client, "writer", "READ_TABLE", "WRITE_TABLE", "READ_ENVIRONMENT")

    refused = client.put(url, headers=writer, json={"days": 2})
    assert refused.status_code == 403, refused.text
    denied = _events(client, AuditEventType.AUTHORIZATION_DENIED)
    assert any(event.detail.get("action") == "DROP_TABLE" for event in denied)

    extended = client.put(url, headers=writer, json={"days": 60})
    assert extended.status_code == 200, extended.text
    assert extended.json()["set_by"] == "writer"


def test_a_missing_table_is_404(client: TestClient) -> None:
    """The admin may write anywhere, so a missing table is not found."""
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    missing = client.get(f"{_TABLES}/nope/retention", headers=AUTH_HEADER)
    assert missing.status_code == 404


def test_put_environment_retention_sets_and_turns_it_off(client: TestClient) -> None:
    """The environment shows it; a table that sets nothing inherits it."""
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    url = "/api/v1/environments/dev/retention"
    on = client.put(url, headers=AUTH_HEADER, json={"days": 30})
    assert on.status_code == 200, on.text
    assert on.json()["retention_days"] == 30
    assert (
        client.get("/api/v1/environments/dev", headers=AUTH_HEADER).json()["retention_days"] == 30
    )
    shown = client.get(f"{_TABLES}/sales/retention", headers=AUTH_HEADER).json()
    assert (shown["source"], shown["days"]) == ("environment", 30)

    off = client.put(url, headers=AUTH_HEADER, json={"days": None})
    assert off.status_code == 200, off.text
    assert off.json()["retention_days"] is None
    assert len(_events(client, AuditEventType.ENVIRONMENT_RETENTION_SET)) == 2
    assert client.put(url, headers=AUTH_HEADER, json={}).status_code == 422


def test_a_protected_environment_refuses_a_retention(client: TestClient) -> None:
    """422 for the environment and for a table in it."""
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    client.patch("/api/v1/environments/dev", headers=AUTH_HEADER, json={"is_protected": True})
    env = client.put("/api/v1/environments/dev/retention", headers=AUTH_HEADER, json={"days": 3})
    assert env.status_code == 422 and "protected" in env.text
    table = client.put(f"{_TABLES}/sales/retention", headers=AUTH_HEADER, json={"days": 3})
    assert table.status_code == 422 and "--never" in table.text


def test_the_expiring_listing(client: TestClient) -> None:
    """Soonest first within the window (default: the warning days), with who set it."""
    create_environment(client)
    for name in ("a", "b", "c"):
        create_table(client, "dev", "gold", name)
    client.put(f"{_TABLES}/a/retention", headers=AUTH_HEADER, json={"days": 5})
    client.put(f"{_TABLES}/b/retention", headers=AUTH_HEADER, json={"days": 2})
    client.put(f"{_TABLES}/c/retention", headers=AUTH_HEADER, json={"days": 20})

    listed = client.get(_EXPIRING, headers=AUTH_HEADER)
    assert listed.status_code == 200, listed.text
    assert [(e["namespace"], e["name"]) for e in listed.json()] == [("gold", "b"), ("gold", "a")]
    assert listed.json()[0]["set_by"] == "admin"
    wide = client.get(f"{_EXPIRING}?within_days=30", headers=AUTH_HEADER).json()
    assert [e["name"] for e in wide] == ["b", "a", "c"]
    assert client.get(f"{_EXPIRING}?within_days=0", headers=AUTH_HEADER).status_code == 422


def test_the_warning_window_is_the_servers_setting(settings: Settings) -> None:
    """`LAKANA_TABLES__EXPIRY_WARNING_DAYS` decides `expires_soon` and the listing's default."""
    settings.tables.expiry_warning_days = 1
    with TestClient(create_app(settings)) as client:
        create_environment(client)
        create_table(client, "dev", "gold", "sales")
        client.put(f"{_TABLES}/sales/retention", headers=AUTH_HEADER, json={"days": 3})
        assert client.get(f"{_TABLES}/sales", headers=AUTH_HEADER).json()["expires_soon"] is False
        assert client.get(_EXPIRING, headers=AUTH_HEADER).json() == []


def test_the_expiring_listing_filters_by_namespace_and_pages(client: TestClient) -> None:
    """`namespace` keeps that namespace and the ones beneath; `limit` pages with a header."""
    create_environment(client)
    for namespace in ("gold", "silver"):
        create_table(client, "dev", namespace, "t")
        url = f"/api/v1/environments/dev/namespaces/{namespace}/tables/t/retention"
        assert client.put(url, headers=AUTH_HEADER, json={"days": 2}).status_code == 200

    only = client.get(f"{_EXPIRING}?namespace=silver&limit=1", headers=AUTH_HEADER)
    assert only.status_code == 200, only.text
    assert [(e["namespace"], e["name"]) for e in only.json()] == [("silver", "t")]
    assert "x-next-cursor" not in only.headers

    both = client.get(f"{_EXPIRING}?limit=1", headers=AUTH_HEADER)
    assert len(both.json()) == 1
    cursor = both.headers["x-next-cursor"]
    rest = client.get(f"{_EXPIRING}?limit=1&cursor={cursor}", headers=AUTH_HEADER)
    assert len(rest.json()) == 1 and "x-next-cursor" not in rest.headers
    assert client.get(f"{_EXPIRING}?cursor=forged", headers=AUTH_HEADER).status_code == 422
    assert client.get(f"{_EXPIRING}?namespace=a%00b", headers=AUTH_HEADER).status_code == 422
