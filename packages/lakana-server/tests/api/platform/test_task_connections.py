"""`GET /api/v1/tasks/self/connections/{name}`: the run-time credential fetch over HTTP.

`TestClient`, SQLite and the real authentication stack: the bound token is minted as the worker
mints it, for a running attempt of `gold.scores` that declares `crm-db` and `gone-db`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana
from lakana_core.models import AuditEvent, AuditEventType
from tests.conftest import AUTH_HEADER
from tests.python_asset_helpers import BoundAttempt, bound_attempt
from tests.server_test_helpers import create_environment

_URL = "/api/v1/tasks/self/connections/{name}"
_PASSWORD = "task-fetch-s3cr3t"
_OTHER_PASSWORD = "prod-fetch-s3cr3t"


@dataclass
class Fetching:
    """The test client and the bound attempt.

    Parameters
    ----------
    client
        The test client.
    attempt
        The running attempt and its token.
    """

    client: TestClient
    attempt: BoundAttempt

    def get(self, name: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """Fetch a connection, with the bound token unless other headers are given.

        Parameters
        ----------
        name
            The connection's name.
        headers
            The headers to send instead.

        Returns
        -------
        httpx.Response
            The answer.
        """
        return self.client.get(
            _URL.format(name=name),
            headers=headers or {"Authorization": f"Bearer {self.attempt.token}"},
        )

    def events(self, event_type: AuditEventType) -> list[AuditEvent]:
        """Read the audit events of a type.

        Parameters
        ----------
        event_type
            The type.

        Returns
        -------
        list of AuditEvent
            The events.
        """
        with Session(self.client.app.state.engine) as session:
            return list(
                session.execute(
                    select(AuditEvent).where(AuditEvent.event_type == event_type)
                ).scalars()
            )


def _connection(client: TestClient, environment: str, name: str, password: str) -> None:
    """Create a PostgreSQL connection with its own new secret, as the administrator.

    Parameters
    ----------
    client
        The test client.
    environment
        Its environment.
    name
        Its name.
    password
        Its secret's password.
    """
    created = client.post(
        f"/api/v1/environments/{environment}/connections",
        headers=AUTH_HEADER,
        json={
            "name": name,
            "kind": "postgresql",
            "address": {"host": "db.example.com", "database": "crm"},
            "settings": {"user": "reader"},
            "secret_values": {"password": password},
        },
    )
    assert created.status_code == 201, created.text


@pytest.fixture
def fetching(client: TestClient) -> Iterator[Fetching]:
    """`dev` and `prod` with a `crm-db` each, and a running attempt in `dev` declaring it.

    Parameters
    ----------
    client
        The test client.

    Yields
    ------
    Fetching
        The client and the attempt.
    """
    create_environment(client)
    create_environment(client, name="prod")
    _connection(client, "dev", "crm-db", _PASSWORD)
    _connection(client, "dev", "other-db", "other-fetch-s3cr3t")
    _connection(client, "prod", "crm-db", _OTHER_PASSWORD)
    with lakana.Client(
        url="http://testserver", token=AUTH_HEADER["Authorization"][7:], http_client=client
    ) as admin:
        attempt = bound_attempt(
            client.app.state.engine,
            admin,
            "dev",
            asset="gold.scores",
            inputs={},
            lease_seconds=None,
            connections=["crm-db", "gone-db"],
        )
    yield Fetching(client, attempt)


def test_a_running_task_gets_its_connection_and_the_answer_is_not_cached(
    fetching: Fetching,
) -> None:
    answer = fetching.get("crm-db")

    assert answer.status_code == 200, answer.text
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["pragma"] == "no-cache"
    assert answer.json() == {
        "name": "crm-db",
        "kind": "postgresql",
        "address": {"host": "db.example.com", "port": 5432, "database": "crm"},
        "settings": {"user": "reader", "tls": "verify-full"},
        "endpoints": [{"host": "db.example.com", "port": 5432, "role": "service"}],
        "secret": {"password": _PASSWORD},
    }
    (event,) = fetching.events(AuditEventType.CONNECTION_CREDENTIAL_FETCHED)
    assert event.detail["task_run_id"] == str(fetching.attempt.task_run_id)
    assert _PASSWORD not in str(event.detail)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("other-db", "connection `other-db` is not declared by asset `gold.scores`"),
        ("no-such-db", "connection `no-such-db` is not declared by asset `gold.scores`"),
        ("Not_A_Name", "connection `Not_A_Name` is not declared by asset `gold.scores`"),
        ("gone-db", "connection `gone-db` is not in environment `dev`"),
    ],
)
def test_a_refusal_is_a_403_with_its_fixed_text_never_a_404(
    fetching: Fetching, name: str, message: str
) -> None:
    answer = fetching.get(name)
    assert answer.status_code == 403
    assert answer.json()["detail"] == message
    (event,) = fetching.events(AuditEventType.CONNECTION_CREDENTIAL_REFUSED)
    assert event.detail["connection"] == name


def test_a_personal_token_is_refused(fetching: Fetching) -> None:
    answer = fetching.get("crm-db", AUTH_HEADER)
    assert answer.status_code == 403
    assert answer.json()["detail"] == "only a running task can fetch a connection"


def test_a_rest_minted_task_token_is_refused(fetching: Fetching) -> None:
    minted = fetching.client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": fetching.attempt.principal,
            "environment": "dev",
            "asset_scope": ["gold.*"],
            "ttl_seconds": 60,
        },
    )
    assert minted.status_code == 201, minted.text
    answer = fetching.get("crm-db", {"Authorization": f"Bearer {minted.json()['token']}"})
    assert answer.status_code == 403
    assert answer.json()["detail"] == "only a running task can fetch a connection"


def test_a_name_longer_than_a_connection_name_can_be_is_refused_before_the_service(
    fetching: Fetching,
) -> None:
    answer = fetching.get("a" * 64)
    assert answer.status_code == 422
    assert fetching.events(AuditEventType.CONNECTION_CREDENTIAL_REFUSED) == []


def test_the_secret_is_in_no_log_line_of_the_request(
    fetching: Fetching, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert fetching.get("crm-db").status_code == 200
    captured = capfd.readouterr()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert _PASSWORD not in logged + captured.out + captured.err


@pytest.mark.parametrize("route", ["connections", "secrets"])
def test_a_secret_value_that_is_not_text_is_refused_naming_only_its_key(
    client: TestClient, route: str
) -> None:
    r"""`"\ud800"` is valid JSON but no text: refused with 422, the value never echoed."""
    create_environment(client)
    body: dict[str, object] = (
        {
            "name": "crm-db",
            "kind": "postgresql",
            "address": {"host": "db.example.com", "database": "crm"},
            "settings": {"user": "reader"},
            "secret_values": {"password": "pa\ud800ss-word"},
        }
        if route == "connections"
        else {"name": "crm-pw", "values": {"password": "pa\ud800ss-word"}}
    )
    answer = client.post(
        f"/api/v1/environments/dev/{route}",
        headers={**AUTH_HEADER, "Content-Type": "application/json"},
        content=json.dumps(body),  # ASCII: the surrogate travels as `\ud800`
    )
    assert answer.status_code == 422, answer.text
    assert "`password`" in answer.text
    assert "pa" + "\\ud800" not in answer.text and "ss-word" not in answer.text
