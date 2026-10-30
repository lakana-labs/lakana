"""Tests for the orphaned-locations routes of the environments API."""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.db import create_engine
from lakana_core.models import AuditEvent, AuditEventType
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_server.settings import Settings
from tests.api.platform.test_environments import _create, _grant_environment_admin
from tests.conftest import AUTH_HEADER


def _leftover(root: Path, name: str = "orders") -> str:
    """Write a 40-day-old table directory without a record under `managed/`.

    Parameters
    ----------
    root
        The environment's storage root on disk.
    name
        The table's name.

    Returns
    -------
    str
        Its path relative to the root.
    """
    relative = f"managed/gold/{name}-{uuid.uuid4().hex}"
    path = root / relative / "data" / "a.parquet"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"12345")
    moment = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    os.utime(path, (moment, moment))
    return relative


def _events(settings: Settings, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one kind from the app's database.

    Parameters
    ----------
    settings
        The settings the app was built from.
    kind
        The event type.

    Returns
    -------
    list of AuditEvent
        Every event of that kind.
    """
    engine = create_engine(settings.database)
    with Session(engine) as session:
        found = list(session.scalars(select(AuditEvent).where(AuditEvent.event_type == kind)))
    engine.dispose()
    return found


def test_list_then_delete(client: TestClient, settings: Settings, tmp_path: Path) -> None:
    """The listing is a dry run; the deletion removes the files and is audited."""
    _create(client, tmp_path)
    relative = _leftover(tmp_path / "dev")

    listed = client.get("/api/v1/environments/dev/orphaned-locations", headers=AUTH_HEADER)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert (body["environment"], body["older_than_days"], body["complete"]) == ("dev", 30, True)
    (entry,) = body["locations"]
    assert (entry["location"], entry["files"], entry["bytes"], entry["recent"]) == (
        relative,
        1,
        5,
        False,
    )
    assert entry["newest_file_at"] is not None
    assert _events(settings, AuditEventType.ORPHANED_LOCATIONS_DELETED) == []

    deleted = client.post(
        "/api/v1/environments/dev/orphaned-locations/delete",
        headers=AUTH_HEADER,
        json={"older_than_days": 30},
    )

    assert deleted.status_code == 200, deleted.text
    (gone,) = deleted.json()["locations"]
    assert (gone["deleted"], gone["files_deleted"], gone["bytes_deleted"]) == (True, 1, 5)
    assert not (tmp_path / "dev" / relative / "data" / "a.parquet").exists()
    (event,) = _events(settings, AuditEventType.ORPHANED_LOCATIONS_DELETED)
    assert event.detail["locations"] == [relative]


def test_the_age_is_a_query_parameter_and_a_body_field(client: TestClient, tmp_path: Path) -> None:
    """`older_than_days` defaults to 30 in the body too; out of range is a 422 naming it."""
    _create(client, tmp_path)
    listed = client.get(
        "/api/v1/environments/dev/orphaned-locations?older_than_days=7", headers=AUTH_HEADER
    )
    assert listed.json()["older_than_days"] == 7
    deleted = client.post(
        "/api/v1/environments/dev/orphaned-locations/delete", headers=AUTH_HEADER, json={}
    )
    assert deleted.json()["older_than_days"] == 30
    refused = client.get(
        "/api/v1/environments/dev/orphaned-locations?older_than_days=0", headers=AUTH_HEADER
    )
    assert refused.status_code == 422
    assert "older_than_days" in refused.text


def test_an_environment_admin_is_refused_and_the_denial_audited(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    """The right is the instance's: the walk sees files of every environment on the root."""
    _create(client, tmp_path, "sandbox")
    _leftover(tmp_path / "sandbox")
    bob = _grant_environment_admin(client, "sandbox")
    before = len(_events(settings, AuditEventType.AUTHORIZATION_DENIED))

    listed = client.get("/api/v1/environments/sandbox/orphaned-locations", headers=bob)
    deleted = client.post(
        "/api/v1/environments/sandbox/orphaned-locations/delete", headers=bob, json={}
    )

    assert (listed.status_code, deleted.status_code) == (403, 403)
    assert len(_events(settings, AuditEventType.AUTHORIZATION_DENIED)) == before + 2
    assert _events(settings, AuditEventType.ORPHANED_LOCATIONS_DELETED) == []


def test_a_deleted_environment_is_accepted_and_an_unknown_one_is_404(
    client: TestClient, tmp_path: Path
) -> None:
    """A deleted environment's leftovers are what this is for."""
    _create(client, tmp_path)
    assert client.delete("/api/v1/environments/dev", headers=AUTH_HEADER).status_code == 200
    _leftover(tmp_path / "dev")
    listed = client.get("/api/v1/environments/dev/orphaned-locations", headers=AUTH_HEADER)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["locations"]) == 1
    missing = client.get("/api/v1/environments/nope/orphaned-locations", headers=AUTH_HEADER)
    assert missing.status_code == 404


def test_a_storage_that_refuses_access_is_a_502_with_the_cause_and_the_next_step(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    """A refusal is not a bare 500, and not a 503: asking again does not help (#371)."""
    _create(client, tmp_path)
    _leftover(tmp_path / "dev")
    managed = tmp_path / "dev" / "managed"
    managed.chmod(0)
    try:
        listed = client.get("/api/v1/environments/dev/orphaned-locations", headers=AUTH_HEADER)
        deleted = client.post(
            "/api/v1/environments/dev/orphaned-locations/delete", headers=AUTH_HEADER, json={}
        )
    finally:
        managed.chmod(0o755)

    for answer in (listed, deleted):
        assert answer.status_code == 502, answer.text
        assert "Retry-After" not in answer.headers
        detail = answer.json()["detail"]
        assert "The storage of environment 'dev' refused access" in detail
        assert "check the environment's storage credentials" in detail
        assert str(tmp_path) not in detail
    assert _events(settings, AuditEventType.ORPHANED_LOCATIONS_DELETED) == []


def test_a_storage_that_does_not_answer_is_a_503_to_ask_again(
    client: TestClient, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """s3fs raises botocore's own error for an endpoint that does not answer, not an OSError."""
    _create(client, tmp_path)
    _leftover(tmp_path / "dev")

    def unreachable(self: FsspecStorage, prefix: str) -> Iterator[tuple[str, bool]]:
        raise EndpointConnectionError(endpoint_url="http://seaweedfs:8333/")

    monkeypatch.setattr(FsspecStorage, "list_children", unreachable)
    listed = client.get("/api/v1/environments/dev/orphaned-locations", headers=AUTH_HEADER)
    deleted = client.post(
        "/api/v1/environments/dev/orphaned-locations/delete", headers=AUTH_HEADER, json={}
    )

    for answer in (listed, deleted):
        assert answer.status_code == 503, answer.text
        assert answer.headers["Retry-After"] == "5"
        detail = answer.json()["detail"]
        assert "The storage of environment 'dev' could not be reached" in detail
        assert "seaweedfs" not in detail
    assert _events(settings, AuditEventType.ORPHANED_LOCATIONS_DELETED) == []
