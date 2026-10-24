"""The run-time credential fetch on Postgres over real HTTP, through the real authentication.

A task attempt and its bound token are set up directly in the database
(`python_asset_helpers.bound_attempt`); the fetch of a real Python asset is in
`test_python_assets_integration.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

import lakana
from lakana.errors import AuthenticationError
from lakana_core.db import create_engine
from lakana_core.models import AuditEvent, AuditEventType, TaskRun, TaskStatus
from lakana_server.app import create_app
from tests.integration_helpers import unique_name
from tests.python_asset_helpers import (
    TOKEN,
    BoundAttempt,
    bound_attempt,
    reclaim,
    running_services,
    serving,
)

pytestmark = pytest.mark.integration

_PASSWORD = "pg-fetch-s3cr3t"


@dataclass(frozen=True)
class _Stack:
    """A running server on Postgres, an admin client and an environment with `crm-db`.

    Parameters
    ----------
    url
        The server's base URL.
    admin
        An SDK client with the bootstrap token.
    db
        An engine on the metadata database.
    environment
        The environment's name.
    """

    url: str
    admin: lakana.Client
    db: Engine
    environment: str

    def attempt(self) -> BoundAttempt:
        """Start an attempt of `gold.scores` that declares `crm-db`, and mint its token.

        Returns
        -------
        BoundAttempt
            The attempt.
        """
        return bound_attempt(
            self.db,
            self.admin,
            self.environment,
            asset="gold.scores",
            inputs={},
            connections=["crm-db"],
        )

    def fetch(self, token: str) -> dict[str, object]:
        """Fetch `crm-db` with a token through the SDK.

        Parameters
        ----------
        token
            The bearer token.

        Returns
        -------
        dict
            The answer.
        """
        with lakana.Client(self.url, token, timeout=60.0) as client:
            return client.task_connections.get("crm-db")

    def count(self, event_type: AuditEventType) -> int:
        """Count the audit events of a type.

        Parameters
        ----------
        event_type
            The type.

        Returns
        -------
        int
            How many there are.
        """
        with Session(self.db) as session:
            return len(
                session.execute(
                    select(AuditEvent.id).where(AuditEvent.event_type == event_type)
                ).all()
            )


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Stack]:
    """Start Postgres, SeaweedFS and a real HTTP server; create `crm-db` with its secret.

    Parameters
    ----------
    tmp_path_factory
        For the repo root.

    Yields
    ------
    _Stack
        The stack.
    """
    with running_services(tmp_path_factory) as services:
        db = create_engine(services.settings.database)
        with (
            serving(create_app(services.settings)) as url,
            lakana.Client(url, TOKEN, timeout=60.0) as admin,
        ):
            environment = unique_name()
            services.environment(admin, environment)
            admin.connections.create(
                environment,
                "crm-db",
                kind="postgresql",
                address={"host": "db.example.com", "database": "crm"},
                settings={"user": "reader"},
                secret_values={"password": _PASSWORD},
            )
            yield _Stack(url, admin, db, environment)
        db.dispose()


def test_a_running_task_fetches_its_connection_over_http(stack: _Stack) -> None:
    attempt = stack.attempt()
    before = stack.count(AuditEventType.CONNECTION_CREDENTIAL_FETCHED)

    answer = stack.fetch(attempt.token)

    assert answer["secret"] == {"password": _PASSWORD}
    assert answer["endpoints"] == [{"host": "db.example.com", "port": 5432, "role": "service"}]
    assert stack.count(AuditEventType.CONNECTION_CREDENTIAL_FETCHED) == before + 1
    raw = httpx.get(
        f"{stack.url}/api/v1/tasks/self/connections/crm-db",
        headers={"Authorization": f"Bearer {attempt.token}"},
    )
    assert (raw.headers["cache-control"], raw.headers["pragma"]) == ("no-store", "no-cache")


def test_the_token_of_an_ended_attempt_is_refused_by_authentication(stack: _Stack) -> None:
    """401 from authentication, before the fetch: no audit row of the fetch's own kinds."""
    attempt = stack.attempt()
    with Session(stack.db) as session:
        session.execute(
            update(TaskRun)
            .where(TaskRun.id == attempt.task_run_id)
            .values(status=TaskStatus.SUCCEEDED)
        )
        session.commit()
    fetched = stack.count(AuditEventType.CONNECTION_CREDENTIAL_FETCHED)
    refused = stack.count(AuditEventType.CONNECTION_CREDENTIAL_REFUSED)

    with pytest.raises(AuthenticationError) as ended:
        stack.fetch(attempt.token)

    assert ended.value.status == 401
    assert stack.count(AuditEventType.CONNECTION_CREDENTIAL_FETCHED) == fetched
    assert stack.count(AuditEventType.CONNECTION_CREDENTIAL_REFUSED) == refused


def test_the_token_of_a_reclaimed_attempt_is_refused_and_the_new_one_is_answered(
    stack: _Stack,
) -> None:
    attempt = stack.attempt()
    again = reclaim(stack.db, attempt)

    with pytest.raises(AuthenticationError):
        stack.fetch(attempt.token)
    assert stack.fetch(again.token)["secret"] == {"password": _PASSWORD}
