"""The platform API's drop, `purge`, the dropped-table listing and `POST .../undrop`.

The catalog's own tests (`lakana-core/tests/test_table_undrop.py`) cover what a tombstone holds
and what an undrop restores; these cover the routes: their shapes, their status codes and the
undo period the server reads from `LAKANA_TABLES__UNDO_DAYS`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, AuditEventType, TableLocation, TableLocationState
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment, create_table

_TABLES = "/api/v1/environments/dev/namespaces/gold/tables"
_DROPPED = "/api/v1/environments/dev/namespaces/gold/dropped-tables"


def _records(settings: Settings) -> list[TableLocation]:
    """Read every location record.

    Parameters
    ----------
    settings
        The app's settings, which name its database.

    Returns
    -------
    list of TableLocation
        Detached records.
    """
    engine = create_engine(settings.database.url)
    try:
        with Session(engine) as session:
            found = list(session.scalars(select(TableLocation)))
            session.expunge_all()
            return found
    finally:
        engine.dispose()


def _dropped_events(settings: Settings) -> list[AuditEvent]:
    """Read the `TABLE_DROPPED` events.

    Parameters
    ----------
    settings
        The app's settings.

    Returns
    -------
    list of AuditEvent
        Detached events.
    """
    engine = create_engine(settings.database.url)
    try:
        with Session(engine) as session:
            found = list(
                session.scalars(
                    select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_DROPPED)
                )
            )
            session.expunge_all()
            return found
    finally:
        engine.dispose()


def test_a_drop_keeps_the_table_for_the_undo_period_and_undrop_restores_it(
    client: TestClient, settings: Settings
) -> None:
    """DELETE leaves a tombstone of seven days; the listing shows it; POST undrop restores it."""
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    table = client.get(f"{_TABLES}/sales", headers=AUTH_HEADER).json()

    assert client.delete(f"{_TABLES}/sales", headers=AUTH_HEADER).status_code == 204

    (record,) = _records(settings)
    assert record.state is TableLocationState.DROPPED
    assert record.purge_after is not None and record.dropped_at is not None
    assert record.purge_after - record.dropped_at == timedelta(days=7)
    listed = client.get(_DROPPED, headers=AUTH_HEADER)
    assert listed.status_code == 200, listed.text
    (entry,) = listed.json()
    assert entry["name"] == "sales"
    assert entry["namespace"] == "gold"
    assert entry["uuid"] == table["uuid"]
    assert entry["kind"] == "TABLE"
    assert entry["restorable"] is True
    assert datetime.fromisoformat(entry["purge_after"]) == record.purge_after
    assert datetime.fromisoformat(entry["dropped_at"]) == record.dropped_at

    restored = client.post(f"{_TABLES}/sales/undrop", headers=AUTH_HEADER, json={})

    assert restored.status_code == 200, restored.text
    assert restored.json()["uuid"] == table["uuid"]
    assert restored.json()["metadata_location"] == table["metadata_location"]
    assert client.get(f"{_TABLES}/sales", headers=AUTH_HEADER).status_code == 200
    assert client.get(_DROPPED, headers=AUTH_HEADER).json() == []


def test_purge_makes_the_drop_due_at_once_and_unrestorable(
    client: TestClient, settings: Settings
) -> None:
    """`?purge=true`: `purge_after` is the drop's time; undrop answers 409 with the reason."""
    create_environment(client)
    create_table(client, "dev", "gold", "sales")

    assert client.delete(f"{_TABLES}/sales?purge=true", headers=AUTH_HEADER).status_code == 204

    (record,) = _records(settings)
    assert record.purge_after == record.dropped_at
    (event,) = _dropped_events(settings)
    assert event.detail["reason"] == "purge"
    assert client.get(_DROPPED, headers=AUTH_HEADER).json()[0]["restorable"] is False
    refused = client.post(f"{_TABLES}/sales/undrop", headers=AUTH_HEADER, json={})
    assert refused.status_code == 409
    assert "undo period ended" in refused.json()["detail"]


def test_undrop_under_a_new_name_and_its_refusals(client: TestClient) -> None:
    """`new_name` restores beside a new table of the old name; a taken name and none are 409/404."""
    create_environment(client)
    create_table(client, "dev", "gold", "sales")
    original = client.get(f"{_TABLES}/sales", headers=AUTH_HEADER).json()["uuid"]
    client.delete(f"{_TABLES}/sales", headers=AUTH_HEADER)
    create_table(client, "dev", "gold", "sales")

    taken = client.post(f"{_TABLES}/sales/undrop", headers=AUTH_HEADER, json={})
    assert taken.status_code == 409
    assert "--as NEW_NAME" in taken.json()["detail"]

    moved = client.post(
        f"{_TABLES}/sales/undrop", headers=AUTH_HEADER, json={"new_name": "sales_before"}
    )
    assert moved.status_code == 200, moved.text
    assert (moved.json()["name"], moved.json()["uuid"]) == ("sales_before", original)

    none = client.post(f"{_TABLES}/nothing/undrop", headers=AUTH_HEADER, json={})
    assert none.status_code == 404
    assert "--dropped" in none.json()["detail"]
    bad = client.post(f"{_TABLES}/nothing/undrop", headers=AUTH_HEADER, json={"other": 1})
    assert bad.status_code == 422


def test_the_server_reads_the_undo_period_from_its_settings(settings: Settings) -> None:
    """`LAKANA_TABLES__UNDO_DAYS=0`: every drop is due at once."""
    settings.tables.undo_days = 0
    with TestClient(create_app(settings)) as client:
        create_environment(client)
        create_table(client, "dev", "gold", "sales")
        client.delete(f"{_TABLES}/sales", headers=AUTH_HEADER)

    (record,) = _records(settings)
    assert record.purge_after == record.dropped_at


def test_the_dropped_listing_of_an_unknown_namespace_is_404(client: TestClient) -> None:
    """The listing names the namespace it could not find."""
    create_environment(client)
    response = client.get(
        "/api/v1/environments/dev/namespaces/nope/dropped-tables", headers=AUTH_HEADER
    )
    assert response.status_code == 404


def test_dropped_tables_across_the_environment_are_paged(client: TestClient) -> None:
    """One route lists every namespace's tombstones, newest first, as items and a cursor."""
    create_environment(client)
    for namespace in ("gold", "silver"):
        create_table(client, "dev", namespace, "sales")
        url = f"/api/v1/environments/dev/namespaces/{namespace}/tables/sales"
        assert client.delete(url, headers=AUTH_HEADER).status_code == 204
    url = "/api/v1/environments/dev/dropped-tables"

    first = client.get(f"{url}?limit=1", headers=AUTH_HEADER)

    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["items"]) == 1 and isinstance(body["next_cursor"], str)
    second = client.get(f"{url}?limit=1&cursor={body['next_cursor']}", headers=AUTH_HEADER).json()
    assert second["next_cursor"] is None
    seen = {(e["namespace"], e["name"]) for e in body["items"] + second["items"]}
    assert seen == {("gold", "sales"), ("silver", "sales")}
    assert client.get(f"{url}?cursor=forged", headers=AUTH_HEADER).status_code == 422
    assert client.get(f"{url}?limit=201", headers=AUTH_HEADER).status_code == 422
    assert (
        client.get("/api/v1/environments/nope/dropped-tables", headers=AUTH_HEADER).status_code
        == 404
    )
