"""`POST /api/v1/repositories/{name}/deploy-key`: a deploy key per environment, never its secret."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import AuditEvent, Secret
from lakana_core.projects import deploy_keys as deploy_keys_module
from lakana_core.projects.deploy_keys import DeployKeyPair
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_REPOS = "/api/v1/repositories"
_HOSTS = (
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR\n"
)


def _ssh_repository(client: TestClient, name: str = "analytics") -> None:
    """Register an ssh repository with confirmed host keys.

    Parameters
    ----------
    client
        The test client.
    name
        Its name.
    """
    created = client.post(
        _REPOS,
        headers=AUTH_HEADER,
        json={
            "name": name,
            "kind": "ssh",
            "url": "git@github.com:acme/analytics.git",
            "ssh_known_hosts": _HOSTS,
        },
    )
    assert created.status_code == 201, created.text


@pytest.fixture
def made(monkeypatch: pytest.MonkeyPatch) -> list[DeployKeyPair]:
    """Record every key pair the server makes.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.

    Returns
    -------
    list of DeployKeyPair
        Filled as keys are made.
    """
    pairs: list[DeployKeyPair] = []
    real = deploy_keys_module.generate_deploy_key

    def _recording(comment: str) -> DeployKeyPair:
        pair = real(comment)
        pairs.append(pair)
        return pair

    monkeypatch.setattr(deploy_keys_module, "generate_deploy_key", _recording)
    return pairs


def _secret_parts(pair: DeployKeyPair) -> list[str]:
    """List what would give a private key away: each PEM body line and the raw seed's base64.

    Parameters
    ----------
    pair
        A key pair.

    Returns
    -------
    list of str
        The texts that must never leave the server.
    """
    body = [line for line in pair.private_key.splitlines() if line and "-----" not in line]
    key = serialization.load_ssh_private_key(pair.private_key.encode(), password=None)
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return [*body, base64.b64encode(seed).decode(), "PRIVATE KEY"]


def _leaks(text: str, pairs: list[DeployKeyPair]) -> list[str]:
    """Find the private parts of `pairs` in a text.

    Parameters
    ----------
    text
        The text.
    pairs
        The pairs made.

    Returns
    -------
    list of str
        The parts found; empty when nothing leaked.
    """
    return [part for pair in pairs for part in _secret_parts(pair) if part in text]


def _audit_text(client: TestClient) -> str:
    """Join every audit event's detail.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    str
        The details as JSON.
    """
    with Session(cast(FastAPI, client.app).state.engine) as session:
        return json.dumps([e.detail for e in session.execute(select(AuditEvent)).scalars()])


def test_post_makes_a_key_per_environment(client: TestClient) -> None:
    """201 with one public key per environment; the repository then lists them."""
    create_environment(client, "dev")
    create_environment(client, "prod")
    _ssh_repository(client)

    answer = client.post(f"{_REPOS}/analytics/deploy-key", headers=AUTH_HEADER)

    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["secret"] == "analytics-deploy-key"
    assert (body["skipped"], body["replaced"], body["notes"]) == ([], [], [])
    assert [(k["environment"], k["made"]) for k in body["keys"]] == [
        ("dev", True),
        ("prod", True),
    ]
    assert all(k["public_key"].startswith("ssh-ed25519 ") for k in body["keys"])
    assert all(k["fingerprint"].startswith("SHA256:") for k in body["keys"])
    shown = client.get(f"{_REPOS}/analytics", headers=AUTH_HEADER).json()
    assert shown["credential_secret"] == "analytics-deploy-key"
    assert shown["deploy_keys"] == [
        {k: key[k] for k in ("environment", "public_key", "fingerprint")} for key in body["keys"]
    ]
    listed = client.get(_REPOS, headers=AUTH_HEADER).json()
    assert listed[0]["deploy_keys"] == shown["deploy_keys"]


def test_a_named_secret_and_environment_with_replace(client: TestClient) -> None:
    """The body names the secret and the environment; a replaced key says what to do."""
    create_environment(client, "dev")
    create_environment(client, "prod")
    _ssh_repository(client)
    url = f"{_REPOS}/analytics/deploy-key"
    first = client.post(url, headers=AUTH_HEADER, json={"secret": "git-key"}).json()

    again = client.post(
        url,
        headers=AUTH_HEADER,
        json={"secret": "git-key", "environments": ["prod"], "replace": True},
    )

    assert again.status_code == 201, again.text
    body = again.json()
    assert [k["environment"] for k in body["keys"]] == ["prod"]
    assert body["replaced"] == ["prod"]
    assert body["notes"] == ["The old key for prod stops working: replace it at your git host now."]
    assert body["keys"][0]["public_key"] != first["keys"][1]["public_key"]


def test_replace_without_an_environment_is_422(client: TestClient) -> None:
    """`replace` alone is refused with the option to add."""
    create_environment(client, "dev")
    _ssh_repository(client)

    answer = client.post(
        f"{_REPOS}/analytics/deploy-key", headers=AUTH_HEADER, json={"replace": True}
    )

    assert answer.status_code == 422
    assert "--environment" in answer.text


def test_a_taken_name_is_409_and_an_unknown_repository_404(client: TestClient) -> None:
    """A secret of the name that is not a key made for the repository stops the call."""
    create_environment(client, "dev")
    _ssh_repository(client)
    created = client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "analytics-deploy-key", "values": {"private_key": "x"}},
    )
    assert created.status_code == 201, created.text

    assert client.post(f"{_REPOS}/analytics/deploy-key", headers=AUTH_HEADER).status_code == 409
    assert client.post(f"{_REPOS}/nothing/deploy-key", headers=AUTH_HEADER).status_code == 404


def test_private_key_never_in_answer_audit_or_logs(
    client: TestClient, made: list[DeployKeyPair], caplog: pytest.LogCaptureFixture
) -> None:
    """No line of a private key, nor its seed, is in the answer, the audit log, a log or the API."""
    for name in ("dev", "prod", "staging"):
        create_environment(client, name)
    _ssh_repository(client)

    with caplog.at_level(logging.DEBUG):
        answer = client.post(f"{_REPOS}/analytics/deploy-key", headers=AUTH_HEADER)
        shown = client.get(f"{_REPOS}/analytics", headers=AUTH_HEADER)

    assert answer.status_code == 201 and len(made) == 3
    openapi = json.dumps(cast(FastAPI, client.app).openapi())
    for text in (answer.text, shown.text, _audit_text(client), caplog.text, openapi):
        assert _leaks(text, made) == []


@pytest.fixture
def lenient_client(settings: Settings) -> Iterator[TestClient]:
    """Provide a client that answers a server error with 500 instead of raising it.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.

    Yields
    ------
    TestClient
        The client.
    """
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        yield c


def test_a_failure_in_the_third_environment_leaks_nothing_and_writes_nothing(
    lenient_client: TestClient,
    made: list[DeployKeyPair],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The third write fails: a 500 whose answer and log carry no key, and no secret is kept."""
    client = lenient_client
    for name in ("dev", "prod", "staging"):
        create_environment(client, name)
    _ssh_repository(client)
    real = deploy_keys_module.insert_secret
    calls: list[int] = []

    def _third_fails(*args: Any, **kwargs: Any) -> Secret:  # ruff: ignore[any-type]
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("the storage of the third secret failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(deploy_keys_module, "insert_secret", _third_fails)

    with caplog.at_level(logging.DEBUG):
        answer = client.post(f"{_REPOS}/analytics/deploy-key", headers=AUTH_HEADER)

    assert answer.status_code == 500
    assert len(made) == 3
    for text in (answer.text, caplog.text, _audit_text(client)):
        assert _leaks(text, made) == []
    with Session(cast(FastAPI, client.app).state.engine) as session:
        assert session.execute(select(Secret)).all() == []
    assert client.get(f"{_REPOS}/analytics", headers=AUTH_HEADER).json()["deploy_keys"] == []
