"""Tests for the unauthenticated readiness route `/readyz`."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.models import Environment, Worker
from lakana_server.app import create_app
from lakana_server.settings import ReadinessConfig, Settings


def _add_worker(client: TestClient, age_seconds: int = 0) -> None:
    """Insert a worker whose heartbeat is this old.

    Parameters
    ----------
    client
        The test client; its app's engine is used.
    age_seconds
        The age of the heartbeat.
    """
    with Session(client.app.state.engine) as session:
        beat = db_now(session) - timedelta(seconds=age_seconds)
        session.add(Worker(id="w1", host="h", version="1", started_at=beat, heartbeat_at=beat))
        session.commit()


def _add_environment(client: TestClient, root: str, secret: str | None = None) -> None:
    """Insert the environment `dev` with this storage root.

    Parameters
    ----------
    client
        The test client; its app's engine is used.
    root
        The storage root.
    secret
        The name of a storage secret it needs, if any.
    """
    with Session(client.app.state.engine) as session:
        session.add(
            Environment(
                id=uuid.uuid4(),
                name="dev",
                storage_root=root,
                storage_options={},
                storage_secret_ref=secret,
                is_protected=False,
                properties={},
            )
        )
        session.commit()


async def _nothing_failed() -> tuple[str, ...]:  # ruff: ignore[unused-async] -- stands in for an async check
    """Stand in for the readiness checks when all of them pass.

    Returns
    -------
    tuple[str, ...]
        No failed check.
    """
    return ()


def test_readyz_answers_200_when_no_check_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 200 path, with the checks faked so no database latency can matter."""
    monkeypatch.setattr(client.app.state.readiness, "failed", _nothing_failed)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.quarantine(issue=322)
def test_readyz_answers_200_without_credentials_when_everything_answers(
    client: TestClient, tmp_path: Path
) -> None:
    _add_environment(client, f"file://{tmp_path}/lake")
    _add_worker(client)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.quarantine(issue=322)
def test_readyz_names_the_failed_check_and_nothing_else(client: TestClient) -> None:
    _add_environment(client, "file:///nowhere", secret="s3-keys")
    _add_worker(client, age_seconds=600)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": ["storage", "worker"]}
    assert "dev" not in response.text and "s3-keys" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.slow
def test_readyz_is_not_part_of_the_platform_api(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/readyz" not in paths


def test_the_readiness_settings_have_working_defaults() -> None:
    config = ReadinessConfig()

    assert (config.timeout_seconds, config.cache_seconds, config.environments) == (2.0, 5.0, [])


@pytest.mark.parametrize(
    "values",
    [{"timeout_seconds": 0}, {"timeout_seconds": 31}, {"cache_seconds": 0.5}],
)
def test_the_readiness_settings_refuse_values_that_would_make_the_route_unsafe(
    values: dict[str, float],
) -> None:
    with pytest.raises(ValueError):
        ReadinessConfig.model_validate(values)


def test_only_the_named_environments_count(settings: Settings, tmp_path: Path) -> None:
    named = settings.model_copy(update={"readiness": ReadinessConfig(environments=["prod"])})
    with TestClient(create_app(named)) as client:
        _add_environment(client, f"file://{tmp_path}/lake")
        _add_worker(client)

        response = client.get("/readyz")

    assert response.json() == {"status": "not_ready", "failed": ["storage"]}
