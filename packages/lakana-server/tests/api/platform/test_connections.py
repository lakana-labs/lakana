"""The connections resource of the platform API: `/api/v1/environments/{env}/connections`."""

from __future__ import annotations

import datetime
import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    Repository,
    RepositoryKind,
)
from tests.api.platform.test_artifacts_delete import _task_headers
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_BASE = "/api/v1/environments/prod/connections"
_VALUE = "s3cr3t-value"
_PG = {
    "name": "crm-db",
    "kind": "postgresql",
    "address": {"host": "db.example.com", "database": "crm"},
    "settings": {"user": "reader"},
}
_ROLES = {
    "data_engineer": "DATA_ENGINEER",
    "analyst": "ANALYST",
    "viewer": "VIEWER",
}


class _As:
    """Send requests with one principal's token.

    Parameters
    ----------
    client
        The test client.
    headers
        The principal's `Authorization` header.
    """

    def __init__(self, client: TestClient, headers: Mapping[str, str]) -> None:
        self._client = client
        self._headers = dict(headers)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:  # ruff: ignore[any-type]
        """Send a GET.

        Parameters
        ----------
        url
            The path.
        **kwargs
            Passed to the client.

        Returns
        -------
        httpx.Response
            The answer.
        """
        return self._client.get(url, headers=self._headers, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:  # ruff: ignore[any-type]
        """Send a POST.

        Parameters
        ----------
        url
            The path.
        **kwargs
            Passed to the client.

        Returns
        -------
        httpx.Response
            The answer.
        """
        return self._client.post(url, headers=self._headers, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:  # ruff: ignore[any-type]
        """Send a PATCH.

        Parameters
        ----------
        url
            The path.
        **kwargs
            Passed to the client.

        Returns
        -------
        httpx.Response
            The answer.
        """
        return self._client.patch(url, headers=self._headers, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:  # ruff: ignore[any-type]
        """Send a PUT.

        Parameters
        ----------
        url
            The path.
        **kwargs
            Passed to the client.

        Returns
        -------
        httpx.Response
            The answer.
        """
        return self._client.put(url, headers=self._headers, **kwargs)

    def delete(self, url: str) -> httpx.Response:
        """Send a DELETE.

        Parameters
        ----------
        url
            The path.

        Returns
        -------
        httpx.Response
            The answer.
        """
        return self._client.delete(url, headers=self._headers)


ClientAs = Callable[[str], _As]


def _token(client: TestClient, name: str, role: str, environment: str) -> dict[str, str]:
    """Create a user with a role on one environment and return its header.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    role
        The role granted on `environment`.
    environment
        The environment.

    Returns
    -------
    dict of str to str
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": name, "role": role, "kind": "ENVIRONMENT", "environment": environment},
    )
    assert granted.status_code == 201, granted.text
    token = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client_as(client: TestClient) -> ClientAs:
    """Give a request sender per role: `admin`, a role in `prod`, or `other` (a role in `dev`).

    `data_engineer`, `analyst` and `viewer` hold that role in `prod`; `other` is a data
    engineer of `dev` only.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    ClientAs
        The role's name to its sender.
    """
    create_environment(client, "prod")
    create_environment(client, "dev")
    senders = {"admin": _As(client, AUTH_HEADER)}
    for name, role in _ROLES.items():
        senders[name] = _As(client, _token(client, name, role, "prod"))
    senders["other"] = _As(client, _token(client, "other", "DATA_ENGINEER", "dev"))
    return senders.__getitem__


@pytest.fixture
def ca_pem() -> str:
    """Provide a self-signed CA certificate in PEM, valid for one day.

    Returns
    -------
    str
        The certificate.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lakana test CA")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


@pytest.fixture
def created(client_as: ClientAs, ca_pem: str) -> dict[str, Any]:
    """Create `crm-db` in `prod` as the data engineer, with a CA and its own new secret.

    Parameters
    ----------
    client_as
        The senders.
    ca_pem
        The CA certificate.

    Returns
    -------
    dict
        The create answer's body.
    """
    response = client_as("data_engineer").post(
        _BASE,
        json={
            **_PG,
            "settings": {"user": "reader", "ca_certificate": ca_pem},
            "secret_values": {"password": _VALUE},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _deploy(
    client: TestClient, environment: str, repository: str, assets: Mapping[str, Sequence[str]]
) -> None:
    """Make a project version whose Python assets declare connections the deployed one.

    Parameters
    ----------
    client
        The test client, for its database.
    environment
        The environment's name.
    repository
        The repository's name; registered on first use.
    assets
        Asset name to the connections it declares.
    """
    with Session(client.app.state.engine) as session:
        env_id = session.execute(
            select(Environment.id).where(Environment.name == environment)
        ).scalar_one()
        repo = session.execute(
            select(Repository).where(Repository.name == repository)
        ).scalar_one_or_none()
        if repo is None:
            repo = Repository(
                id=uuid.uuid4(),
                name=repository,
                kind=RepositoryKind.HTTPS,
                url=f"https://git.example.org/{repository}.git",
            )
            session.add(repo)
            session.flush()
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=env_id,
            commit_sha=uuid.uuid4().hex + "00000000",
            manifest_json={
                "assets": [
                    {"table": table, "kind": "python", "connections": list(names)}
                    for table, names in assets.items()
                ]
            },
            code_uri="file:///code.tar.gz",
            code_sha256="0" * 64,
        )
        session.add(version)
        session.flush()
        pointer = session.get(EnvironmentDeployment, (env_id, repo.id))
        if pointer is None:
            session.add(
                EnvironmentDeployment(
                    environment_id=env_id,
                    repository_id=repo.id,
                    current_job_version_id=version.id,
                )
            )
        else:
            pointer.current_job_version_id = version.id
        session.commit()


def _event_types(client: TestClient) -> list[str]:
    """List the audit event types in the order they were recorded.

    Parameters
    ----------
    client
        The test client, for its database.

    Returns
    -------
    list of str
        The event types, oldest first.
    """
    with Session(client.app.state.engine) as session:
        rows = session.execute(select(AuditEvent).order_by(AuditEvent.occurred_at)).scalars()
        return [row.event_type.value for row in rows]


def _details(client: TestClient, event_type: AuditEventType) -> list[dict[str, Any]]:
    """Read the details of every event of one type, oldest first.

    Parameters
    ----------
    client
        The test client, for its database.
    event_type
        The type.

    Returns
    -------
    list of dict
        The details.
    """
    with Session(client.app.state.engine) as session:
        rows = session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == event_type)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
        return [dict(row.detail or {}) for row in rows]


def test_create_without_manage_connection_is_403(client_as: ClientAs) -> None:
    """An analyst gets 403 before the body's kind or the environment is judged."""
    analyst = client_as("analyst")
    good = analyst.post(_BASE, json=_PG)
    bad_kind = analyst.post(_BASE, json={**_PG, "kind": "nope"})
    no_env = analyst.post("/api/v1/environments/nowhere/connections", json=_PG)

    assert [r.status_code for r in (good, bad_kind, no_env)] == [403, 403, 403]
    assert client_as("admin").get(_BASE).json()["items"] == []


def test_a_data_engineer_creates_lists_shows_updates_and_deletes(
    client: TestClient, client_as: ClientAs
) -> None:
    """The whole life of a connection, each answer's shape and each audit event."""
    engineer = client_as("data_engineer")

    created = engineer.post(_BASE, json={**_PG, "secret_values": {"password": _VALUE}})

    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {
        "name",
        "kind",
        "address",
        "settings",
        "secret",
        "declared_by",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert body["address"] == {"host": "db.example.com", "port": 5432, "database": "crm"}
    assert body["settings"] == {"user": "reader", "tls": "verify-full"}
    assert (body["secret"], body["declared_by"], body["created_by"]) == (
        "crm-db-connection",
        [],
        "data_engineer",
    )

    listed = engineer.get(_BASE)
    assert listed.status_code == 200
    assert set(listed.json()) == {"items", "next_cursor"}
    assert [item["name"] for item in listed.json()["items"]] == ["crm-db"]
    assert listed.json()["next_cursor"] is None

    updated = engineer.patch(f"{_BASE}/crm-db", json={"settings": {"tls": "require"}})
    assert updated.status_code == 403, updated.text  # weaker TLS moves the secret (S1)
    updated = engineer.patch(f"{_BASE}/crm-db", json={"settings": {"user": "loader"}})
    assert updated.status_code == 200, updated.text
    assert updated.json()["settings"]["user"] == "loader"

    cleared = engineer.patch(f"{_BASE}/crm-db", json={"clear": ["secret"]})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["secret"] is None

    assert engineer.delete(f"{_BASE}/crm-db").status_code == 204
    assert engineer.get(f"{_BASE}/crm-db").status_code == 404
    connection_events = [t for t in _event_types(client) if t.startswith("CONNECTION_")]
    assert connection_events == [
        "CONNECTION_CREATED",
        "CONNECTION_UPDATED",
        "CONNECTION_UPDATED",
        "CONNECTION_DELETED",
    ]
    assert _details(client, AuditEventType.CONNECTION_CREATED) == [
        {
            "name": "crm-db",
            "kind": "postgresql",
            "address": {"host": "db.example.com", "port": 5432, "database": "crm"},
            "secret": "crm-db-connection",
        }
    ]


def test_list_pages_with_limit_and_cursor(client_as: ClientAs) -> None:
    """`limit` and `cursor` page the list newest first; a foreign cursor is a 422."""
    engineer = client_as("data_engineer")
    for name in ("one", "two", "three"):
        response = engineer.post(
            _BASE, json={"name": name, "kind": "directory", "address": {"root": "r", "path": "p"}}
        )
        assert response.status_code == 201, response.text

    first = engineer.get(_BASE, params={"limit": 2}).json()
    second = engineer.get(_BASE, params={"limit": 2, "cursor": first["next_cursor"]}).json()

    names = [item["name"] for item in first["items"] + second["items"]]
    assert sorted(names) == ["one", "three", "two"]
    assert second["next_cursor"] is None
    assert engineer.get(_BASE, params={"cursor": "not-a-cursor"}).status_code == 422
    assert engineer.get(_BASE, params={"limit": 0}).status_code == 422


def test_read_with_read_environment(client_as: ClientAs, created: dict[str, Any]) -> None:
    """A viewer lists and shows; the CA is a fingerprint and no secret value appears."""
    viewer = client_as("viewer")

    shown = viewer.get(f"{_BASE}/crm-db")
    listed = viewer.get(_BASE)

    assert shown.status_code == 200
    body = shown.json()
    assert body["settings"]["ca_certificate"].startswith("sha256:")
    assert "BEGIN CERTIFICATE" not in shown.text + listed.text
    assert body["secret"] == "crm-db-connection"
    assert _VALUE not in json.dumps(body)
    assert [item["name"] for item in listed.json()["items"]] == ["crm-db"]
    assert _VALUE not in listed.text
    assert viewer.patch(f"{_BASE}/crm-db", json={"settings": {"user": "x"}}).status_code == 403
    assert viewer.delete(f"{_BASE}/crm-db").status_code == 403
    assert viewer.post(_BASE, json={**_PG, "name": "other"}).status_code == 403


def test_a_role_in_another_environment_reads_and_changes_nothing(
    client_as: ClientAs, created: dict[str, Any]
) -> None:
    """A data engineer of `dev` gets 403 on every route of `prod`, a missing name too."""
    other = client_as("other")

    calls = {
        "create": other.post(_BASE, json={**_PG, "name": "mine"}),
        "list": other.get(_BASE),
        "show": other.get(f"{_BASE}/crm-db"),
        "show missing": other.get(f"{_BASE}/missing"),
        "update": other.patch(f"{_BASE}/crm-db", json={"settings": {"user": "x"}}),
        "delete": other.delete(f"{_BASE}/crm-db"),
    }

    for route, response in calls.items():
        assert response.status_code == 403, (route, response.text)
    assert "crm-db" not in calls["show missing"].text
    created_in_dev = other.post(
        "/api/v1/environments/dev/connections", json={**_PG, "name": "mine"}
    )
    assert created_in_dev.status_code == 201, created_in_dev.text


def test_every_route_needs_a_credential(client: TestClient) -> None:
    """Without a token every route is a 401."""
    calls = (
        client.post(_BASE, json=_PG),
        client.get(_BASE),
        client.get(f"{_BASE}/crm-db"),
        client.patch(f"{_BASE}/crm-db", json={"settings": {"user": "x"}}),
        client.delete(f"{_BASE}/crm-db"),
        client.post(f"{_BASE}/crm-db/tests"),
        client.get(f"{_BASE}/crm-db/tests/{uuid.uuid4()}"),
    )
    assert [call.status_code for call in calls] == [401] * 7


def test_secret_values_are_never_echoed_or_logged(
    client: TestClient,
    client_as: ClientAs,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The create response, the access log and stderr never carry the value."""
    engineer = client_as("data_engineer")
    rotated = "r0tated-value"
    with caplog.at_level(logging.DEBUG):
        created = engineer.post(_BASE, json={**_PG, "secret_values": {"password": _VALUE}})
        updated = engineer.patch(f"{_BASE}/crm-db", json={"secret_values": {"password": rotated}})
        wrong_key = engineer.patch(f"{_BASE}/crm-db", json={"secret_values": {"token": _VALUE}})
        not_text = engineer.post(
            _BASE, json={**_PG, "name": "x", "secret_values": {"password": [_VALUE]}}
        )
    out, err = capfd.readouterr()

    assert (created.status_code, updated.status_code) == (201, 200), updated.text
    assert (wrong_key.status_code, not_text.status_code) == (422, 422)
    assert "token is not a secret key of kind postgresql" in wrong_key.text
    for text in (created.text, updated.text, wrong_key.text, not_text.text, caplog.text, out, err):
        assert _VALUE not in text
        assert rotated not in text
    assert all(
        _VALUE not in json.dumps(detail) and rotated not in json.dumps(detail)
        for event_type in AuditEventType
        for detail in _details(client, event_type)
    )


def test_per_kind_validation_is_422_naming_the_field(client_as: ClientAs) -> None:
    """`address.base_url` for an http:// URL (S3 rule), at create and at update."""
    engineer = client_as("data_engineer")
    api = {"name": "api", "kind": "http", "address": {"base_url": "https://api.example.com"}}

    refused = engineer.post(_BASE, json={**api, "address": {"base_url": "http://api.example.com"}})
    assert refused.status_code == 422
    assert "address.base_url" in refused.json()["detail"]

    assert engineer.post(_BASE, json=api).status_code == 201
    changed = engineer.patch(
        f"{_BASE}/api", json={"address": {"base_url": "http://api.example.com"}}
    )
    assert changed.status_code == 422
    assert "address.base_url" in changed.json()["detail"]
    unknown = engineer.post(_BASE, json={**_PG, "name": "y", "address": {"hots": "x"}})
    assert unknown.status_code == 422
    assert "address.hots" in unknown.json()["detail"]
    nothing = engineer.patch(f"{_BASE}/api", json={})
    assert nothing.status_code == 422
    assert nothing.json()["detail"] == "Nothing to change; give at least one change."


def test_unknown_name_is_404_listing_nothing(client_as: ClientAs, created: dict[str, Any]) -> None:
    """The 404 says the connection does not exist and names `lakana connection list`."""
    viewer, engineer = client_as("viewer"), client_as("data_engineer")
    for response in (
        viewer.get(f"{_BASE}/missing"),
        engineer.get(f"{_BASE}/missing"),
        engineer.patch(f"{_BASE}/missing", json={"settings": {"user": "x"}}),
        engineer.delete(f"{_BASE}/missing"),
    ):
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == (
            "environment `prod` has no connection `missing`; "
            "`lakana connection list --env prod` lists them"
        )
        assert "crm-db" not in response.text
    missing_env = client_as("admin").get("/api/v1/environments/nowhere/connections/crm-db")
    assert missing_env.status_code == 404
    assert missing_env.json()["detail"] == "No environment named 'nowhere'."


def test_a_taken_name_is_409_and_a_taken_secret_name_takes_the_next(
    client_as: ClientAs, created: dict[str, Any]
) -> None:
    """A taken connection name is a 409; a taken `<name>-connection` gives `-2`."""
    engineer = client_as("data_engineer")

    again = engineer.post(_BASE, json=_PG)
    assert again.status_code == 409
    assert again.json()["detail"] == (
        "environment `prod` already has a connection `crm-db`; change it with "
        "`lakana connection update crm-db --env prod`"
    )

    assert engineer.delete(f"{_BASE}/crm-db").status_code == 204
    recreated = engineer.post(_BASE, json={**_PG, "secret_values": {"password": _VALUE}})
    assert recreated.status_code == 201, recreated.text
    assert recreated.json()["secret"] == "crm-db-connection-2"
    assert engineer.get(f"{_BASE}/crm-db").json()["secret"] == "crm-db-connection-2"


def test_delete_while_declared_is_409(client: TestClient, client_as: ClientAs) -> None:
    """The fixed delete text; after a deploy without the connection it goes."""
    engineer = client_as("data_engineer")
    assert engineer.post(_BASE, json=_PG).status_code == 201
    _deploy(client, "prod", "analytics", {"customers": ["crm-db"], "orders": []})

    shown = engineer.get(f"{_BASE}/crm-db").json()
    refused = engineer.delete(f"{_BASE}/crm-db")

    assert shown["declared_by"] == [{"repository": "analytics", "asset": "customers"}]
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "connection `crm-db` is used by `analytics: customers` in `prod`; deploy a version "
        "without it first."
    )
    _deploy(client, "prod", "analytics", {"customers": []})
    assert engineer.delete(f"{_BASE}/crm-db").status_code == 204


def test_naming_a_secret_follows_s1(client: TestClient, client_as: ClientAs) -> None:
    """A data engineer may not name an unbound secret; the administrator's bind does it.

    The refusal says that the bind also makes it the connection's secret, and the binding
    route takes the use `connection`.
    """
    admin, engineer = client_as("admin"), client_as("data_engineer")
    admin.post(
        "/api/v1/environments/prod/secrets",
        json={"name": "crm-pw", "values": {"password": "pw"}},
    )
    assert engineer.post(_BASE, json=_PG).status_code == 201

    refused = engineer.patch(f"{_BASE}/crm-db", json={"secret": "crm-pw"})

    assert refused.status_code == 403
    assert refused.json()["detail"] == (
        "Secret 'crm-pw' of environment 'prod' is not bound to connection 'crm-db'. Ask "
        "someone who may manage secrets in 'prod' to bind it to this connection: lakana secret "
        "bind crm-pw --env prod --connection crm-db (the bind also makes it the secret the "
        "connection uses)"
    )
    bound = admin.put(
        "/api/v1/environments/prod/secrets/crm-pw/use",
        json={"kind": "connection", "target": "crm-db"},
    )
    assert bound.status_code == 200, bound.text
    assert (bound.json()["use"], bound.json()["use_target"]) == ("connection", "crm-db")
    assert engineer.get(f"{_BASE}/crm-db").json()["secret"] == "crm-pw"
    assert _details(client, AuditEventType.SECRET_BOUND)[-1]["use"] == "connection"


def test_an_address_change_of_a_connection_with_a_secret_needs_manage_secret(
    client_as: ClientAs, created: dict[str, Any]
) -> None:
    """S1: a new host needs `MANAGE_SECRET`, unless the same change gives new values."""
    engineer = client_as("data_engineer")

    refused = engineer.patch(f"{_BASE}/crm-db", json={"address": {"host": "evil.example.com"}})
    allowed = engineer.patch(
        f"{_BASE}/crm-db",
        json={"address": {"host": "db2.example.com"}, "secret_values": {"password": "new-pw"}},
    )

    assert refused.status_code == 403
    assert refused.json()["detail"].startswith(
        "This change of connection 'crm-db' (its address, TLS, CA certificate or how it sends "
        "its secret) could send secret 'crm-db-connection' of environment 'prod' somewhere new."
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["address"]["host"] == "db2.example.com"
    assert allowed.json()["secret"] == "crm-db-connection"


def test_audit_kind_connection(client: TestClient, client_as: ClientAs) -> None:
    """`?kind=connection` returns exactly the CONNECTION_* events, newest first."""
    engineer, admin = client_as("data_engineer"), client_as("admin")
    engineer.post(_BASE, json={**_PG, "secret_values": {"password": _VALUE}})
    engineer.patch(f"{_BASE}/crm-db", json={"settings": {"user": "loader"}})
    engineer.delete(f"{_BASE}/crm-db")

    page = admin.get("/api/v1/audit-events", params={"kind": "connection"})
    combined = admin.get(
        "/api/v1/audit-events",
        params={"kind": "connection", "event_type": "CONNECTION_UPDATED", "environment": "prod"},
    )
    disagree = admin.get(
        "/api/v1/audit-events", params={"kind": "connection", "event_type": "SECRET_BOUND"}
    )
    unknown = admin.get("/api/v1/audit-events", params={"kind": "secret"})

    assert page.status_code == 200, page.text
    assert [item["event_type"] for item in page.json()["items"]] == [
        "CONNECTION_DELETED",
        "CONNECTION_UPDATED",
        "CONNECTION_CREATED",
    ]
    assert "SECRET_CREATED" in _event_types(client)
    assert [item["event_type"] for item in combined.json()["items"]] == ["CONNECTION_UPDATED"]
    assert disagree.status_code == 422
    assert disagree.json()["detail"] == (
        "event_type SECRET_BOUND is not of kind connection; leave out one of the two filters"
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"] == "kind must be connection"


@pytest.mark.slow
def test_a_task_token_may_not_list_or_show_connections(
    client: TestClient, client_as: ClientAs
) -> None:
    """Bound or not, a task token gets 403 naming no connection; a person still reads.

    A task learns its own connections only through the fetch at run time.
    """
    admin = client_as("admin")
    created = admin.post(
        "/api/v1/environments/dev/connections",
        json={**_PG, "secret_values": {"password": _VALUE}},
    )
    assert created.status_code == 201, created.text
    reader = _As(client, _token(client, "dev-viewer", "VIEWER", "dev"))

    for kind, header in _task_headers(client).items():
        task = _As(client, header)
        for response in (
            task.get("/api/v1/environments/dev/connections"),
            task.get("/api/v1/environments/dev/connections/crm-db"),
            task.get("/api/v1/environments/dev/connections/missing"),
        ):
            assert response.status_code == 403, (kind, response.text)
            assert "crm-db" not in response.text
            assert "db.example.com" not in response.text
            assert response.json()["detail"] == (
                "a task token cannot list or show connections; read them with your own token"
            )
    listed = reader.get("/api/v1/environments/dev/connections")
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()["items"]] == ["crm-db"]


def test_a_badly_shaped_secret_values_names_a_connection_key(client_as: ClientAs) -> None:
    """The shape error of `secret_values` shows a neutral example, not an s3 setting."""
    response = client_as("data_engineer").post(
        _BASE, json={**_PG, "secret_values": {"password": 1}}
    )
    assert response.status_code == 422
    text = response.text
    assert '{\\"password\\": \\"...\\"}' in text
    assert "access_key_id" not in text


def test_binding_shows_every_host_the_secret_goes_to(
    client: TestClient, client_as: ClientAs
) -> None:
    """An oauth2 connection's client secret also goes to the token URL's host: the bind says so."""
    admin = client_as("admin")
    created = admin.post(
        _BASE,
        json={
            "name": "api",
            "kind": "http",
            "address": {"base_url": "https://api.example.com/v1"},
            "settings": {
                "auth": "oauth2",
                "client_id": "lakana",
                "token_url": "https://login.example.org/oauth/token",
            },
        },
    )
    assert created.status_code == 201, created.text
    admin.post(
        "/api/v1/environments/prod/secrets",
        json={"name": "api-client", "values": {"client_secret": "c"}},
    )

    bound = admin.put(
        "/api/v1/environments/prod/secrets/api-client/use",
        json={"kind": "connection", "target": "api"},
    )

    hosts = "api.example.com:443, login.example.org:443"
    assert bound.status_code == 200, bound.text
    assert bound.json()["use_target_url"] == hosts
    assert _details(client, AuditEventType.SECRET_BOUND)[-1]["url"] == hosts


def test_a_test_is_queued_shown_and_refused_while_open(client_as: ClientAs) -> None:
    """POST answers 202 with the queued test; a second is 409; GET shows it."""
    engineer = client_as("data_engineer")
    engineer.post(_BASE, json={**_PG, "secret_values": {"password": _VALUE}})

    queued = engineer.post(f"{_BASE}/crm-db/tests")
    again = engineer.post(f"{_BASE}/crm-db/tests")
    shown = engineer.get(f"{_BASE}/crm-db/tests/{queued.json()['id']}")

    assert queued.status_code == 202, queued.text
    body = queued.json()
    assert set(body) == {"id", "state", "outcome", "message", "created_at", "finished_at"}
    assert (body["state"], body["outcome"], body["message"], body["finished_at"]) == (
        "queued",
        None,
        None,
        None,
    )
    assert again.status_code == 409
    assert again.json()["detail"] == (
        "a test of `crm-db` is already queued; "
        f"see it with lakana connection test crm-db --env prod --id {body['id']}"
    )
    assert shown.status_code == 200 and shown.json() == body
    assert _VALUE not in queued.text + shown.text


def test_a_test_needs_manage_connection(client_as: ClientAs, created: dict[str, Any]) -> None:
    """An analyst, a viewer and a role of another environment get 403 on both routes."""
    queued = client_as("data_engineer").post(f"{_BASE}/crm-db/tests").json()

    for who in ("analyst", "viewer", "other"):
        sender = client_as(who)
        assert sender.post(f"{_BASE}/crm-db/tests").status_code == 403, who
        assert sender.get(f"{_BASE}/crm-db/tests/{queued['id']}").status_code == 403, who


def test_a_test_of_another_connection_or_a_missing_connection_is_404(
    client_as: ClientAs, created: dict[str, Any]
) -> None:
    """GET answers 404 for a test id of another connection, an odd id, or an unknown name."""
    engineer = client_as("data_engineer")
    engineer.post(_BASE, json={**_PG, "name": "other-db"})
    queued = engineer.post(f"{_BASE}/crm-db/tests").json()

    foreign = engineer.get(f"{_BASE}/other-db/tests/{queued['id']}")
    odd = engineer.get(f"{_BASE}/crm-db/tests/not-an-id")
    missing = engineer.post(f"{_BASE}/missing/tests")

    assert [r.status_code for r in (foreign, odd, missing)] == [404, 404, 404]
    assert foreign.json()["detail"] == (
        f"connection `other-db` has no test `{queued['id']}`; "
        "`lakana connection test other-db --env prod` starts one"
    )
    assert missing.json()["detail"] == (
        "environment `prod` has no connection `missing`; `lakana connection list --env prod` "
        "lists them"
    )
