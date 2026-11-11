"""Repository checks over REST: queue a host-key fetch or an access test, read its answer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    ProbeOutcome,
    Repository,
    RepositoryKind,
    Worker,
)
from lakana_core.projects.probes import HostKey, RepositoryProbeQueue
from tests.api.platform.test_whoami import _new_person
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_PROBES = "/api/v1/repository-probes"
_URL = "git@git.example.com:team/analytics.git"
_KEY = HostKey(
    type="ssh-ed25519",
    fingerprint="SHA256:BbeSiQQjnjpybq7nV8S8tEVnQiZNHfceXjKvrLVxecI",
    line="git.example.com ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR",
)


def _worker(client: TestClient, takes: str = "all") -> None:
    """Register a live worker.

    Parameters
    ----------
    client
        The test client.
    takes
        What the worker takes.
    """
    with Session(client.app.state.engine) as session:
        now = db_now(session)
        session.add(
            Worker(id="w1", host="h", version="1", started_at=now, heartbeat_at=now, takes=takes)
        )
        session.commit()


def _repository(client: TestClient) -> None:
    """Register the SSH repository `analytics`.

    Parameters
    ----------
    client
        The test client.
    """
    with Session(client.app.state.engine) as session:
        session.add(
            Repository(
                id=uuid.uuid4(),
                name="analytics",
                kind=RepositoryKind.SSH,
                url=_URL,
                credential_secret="analytics-deploy-key",
            )
        )
        session.commit()


def test_post_then_get_round_trip(client: TestClient) -> None:
    """A host-key probe is queued (202), read back, and once a worker finished it, shows keys."""
    _worker(client)
    queued = client.post(_PROBES, headers=AUTH_HEADER, json={"kind": "host_keys", "url": _URL})
    assert queued.status_code == 202, queued.text
    body = queued.json()
    assert body["kind"] == "host_keys"
    assert body["state"] == "queued"
    assert (body["outcome"], body["message"], body["host_keys"], body["finished_at"]) == (
        None,
        None,
        None,
        None,
    )
    probe_id = body["id"]
    seen = client.get(f"{_PROBES}/{probe_id}", headers=AUTH_HEADER)
    assert seen.status_code == 200 and seen.json()["id"] == probe_id
    with Session(client.app.state.engine) as session:
        queue = RepositoryProbeQueue(session)
        assert queue.claim("w1", lease_seconds=90, now=db_now(session)) is not None
        assert queue.finish(
            uuid.UUID(probe_id), "w1", outcome=ProbeOutcome.OK, message="found", host_keys=[_KEY]
        )
    done = client.get(f"{_PROBES}/{probe_id}", headers=AUTH_HEADER).json()
    assert done["state"] == "done" and done["outcome"] == "ok"
    assert done["host_keys"] == [
        {"type": _KEY.type, "fingerprint": _KEY.fingerprint, "line": _KEY.line}
    ]
    assert datetime.fromisoformat(done["finished_at"]).tzinfo is not None
    with Session(client.app.state.engine) as session:
        (event,) = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.REPOSITORY_PROBE_REQUESTED
            )
        )
        assert event.detail["host"] == "git.example.com"


def test_an_access_probe_names_its_repository_and_environment(client: TestClient) -> None:
    """An access probe needs a repository and an environment."""
    _worker(client)
    _repository(client)
    create_environment(client, "dev")
    queued = client.post(
        _PROBES,
        headers=AUTH_HEADER,
        json={"kind": "access", "repository": "analytics", "environment": "dev"},
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["kind"] == "access"
    missing = client.post(
        _PROBES,
        headers=AUTH_HEADER,
        json={"kind": "access", "repository": "nope", "environment": "dev"},
    )
    assert missing.status_code == 404
    assert "nope" in missing.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "host_keys"},
        {"kind": "host_keys", "url": _URL, "repository": "analytics"},
        {"kind": "host_keys", "url": _URL, "environment": "dev"},
        {"kind": "access", "repository": "analytics"},
        {"kind": "access", "environment": "dev"},
        {"kind": "access", "repository": "analytics", "environment": "dev", "url": _URL},
        {"kind": "deploy_key", "url": _URL},
        {"kind": "host_keys", "url": _URL, "extra": 1},
    ],
)
def test_body_must_carry_exactly_its_kinds_fields(
    client: TestClient, body: dict[str, object]
) -> None:
    """A field of the other kind, a missing field or an unknown one is a 422.

    Parameters
    ----------
    client
        The test client.
    body
        A malformed body.
    """
    _worker(client)
    answer = client.post(_PROBES, headers=AUTH_HEADER, json=body)
    assert answer.status_code == 422, answer.text


def test_an_unsafe_url_is_a_422(client: TestClient) -> None:
    """A URL that could carry an option or a pattern is refused before anything is queued."""
    _worker(client)
    answer = client.post(
        _PROBES, headers=AUTH_HEADER, json={"kind": "host_keys", "url": "ssh://a,b/r"}
    )
    assert answer.status_code == 422
    assert "plain host name" in answer.json()["detail"]


def test_no_worker_is_503_naming_the_next_step(client: TestClient) -> None:
    """Without a worker that takes pipeline work the request says how to start one."""
    _worker(client, takes="interactive")
    answer = client.post(_PROBES, headers=AUTH_HEADER, json={"kind": "host_keys", "url": _URL})
    assert answer.status_code == 503
    assert "docker compose up -d worker" in answer.json()["detail"]


def test_another_persons_probe_is_404_and_a_person_without_the_right_is_403(
    client: TestClient,
) -> None:
    """Only the requester reads a probe; only `MANAGE_REPOSITORY` starts one."""
    _worker(client)
    probe_id = client.post(
        _PROBES, headers=AUTH_HEADER, json={"kind": "host_keys", "url": _URL}
    ).json()["id"]
    kim = _new_person(client, "kim")
    assert client.get(f"{_PROBES}/{probe_id}", headers=kim).status_code == 404
    assert client.get(f"{_PROBES}/{uuid.uuid4()}", headers=AUTH_HEADER).status_code == 404
    refused = client.post(_PROBES, headers=kim, json={"kind": "host_keys", "url": _URL})
    assert refused.status_code == 403


def test_probes_need_a_credential(client: TestClient) -> None:
    """Both routes are 401 without one."""
    assert client.post(_PROBES, json={"kind": "host_keys", "url": _URL}).status_code == 401
    assert client.get(f"{_PROBES}/{uuid.uuid4()}").status_code == 401


def test_created_at_is_the_databases_time(client: TestClient) -> None:
    """The probe's time is an aware UTC time."""
    _worker(client)
    body = client.post(_PROBES, headers=AUTH_HEADER, json={"kind": "host_keys", "url": _URL}).json()
    created = datetime.fromisoformat(body["created_at"])
    assert created.tzinfo is not None
    assert abs((datetime.now(UTC) - created).total_seconds()) < 120
