"""`client.repositories` against the in-process server."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import lakana
from lakana.errors import Conflict, NotFound, PermissionDenied, ValidationError


def test_repository_lifecycle(lakana_client: lakana.Client) -> None:
    """Create, list, get, update (including clearing) and delete."""
    repo = lakana_client.repositories.create(
        "analytics", "https", "https://github.com/acme/analytics.git", default_ref="main"
    )
    assert (repo.name, repo.kind, repo.default_ref) == ("analytics", "https", "main")
    assert [r.name for r in lakana_client.repositories.list()] == ["analytics"]
    assert lakana_client.repositories.get("analytics").url.endswith("analytics.git")
    updated = lakana_client.repositories.update(
        "analytics", default_ref=None, credential_secret="tok"
    )
    assert updated.default_ref is None and updated.credential_secret == "tok"
    with pytest.raises(Conflict):
        lakana_client.repositories.create("analytics", "https", "https://github.com/acme/x.git")
    lakana_client.repositories.delete("analytics")
    with pytest.raises(NotFound):
        lakana_client.repositories.get("analytics")


def test_update_refuses_unknown_fields_client_side(lakana_client: lakana.Client) -> None:
    """Only the five updatable fields are accepted."""
    with pytest.raises(TypeError, match="kind"):
        lakana_client.repositories.update("analytics", kind="ssh")


def test_invalid_url_is_a_validation_error(lakana_client: lakana.Client) -> None:
    """The server's 422 surfaces as `ValidationError`."""
    with pytest.raises(ValidationError):
        lakana_client.repositories.create("x", "https", "http://plain.example/x.git")


def test_a_non_admin_gets_permission_denied_for_repository_changes(
    lakana_client: lakana.Client, client: TestClient
) -> None:
    """The server's 403 surfaces as `PermissionDenied` for create, update and delete."""
    lakana_client.repositories.create("analytics", "https", "https://github.com/acme/a.git")
    lakana_client.principals.create("bob", "USER")
    token = lakana_client.tokens.issue("bob", "laptop").token
    with lakana.Client(url="http://testserver", token=token, http_client=client) as bob:
        with pytest.raises(PermissionDenied):
            bob.repositories.create("x", "https", "https://github.com/acme/x.git")
        with pytest.raises(PermissionDenied):
            bob.repositories.update("analytics", default_ref="main")
        with pytest.raises(PermissionDenied):
            bob.repositories.delete("analytics")
        with pytest.raises(PermissionDenied):
            bob.repositories.list()


_HOSTS = (
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR\n"
)


def test_deploy_key_round_trip(lakana_client: lakana.Client, storage_root: str) -> None:
    """`deploy_key` makes a key per environment; `get` then shows its public half."""
    lakana_client.environments.create("dev", storage={"scheme": "file", "root": storage_root})
    lakana_client.repositories.create(
        "analytics", "ssh", "git@github.com:acme/analytics.git", ssh_known_hosts=_HOSTS
    )

    made = lakana_client.repositories.deploy_key("analytics")

    assert made.secret == "analytics-deploy-key"
    assert [(k.environment, k.made) for k in made.keys] == [("dev", True)]
    assert made.keys[0].public_key.startswith("ssh-ed25519 ")
    assert (made.skipped, made.replaced, made.notes) == ([], [], [])
    shown = lakana_client.repositories.get("analytics")
    assert [(k.environment, k.fingerprint) for k in shown.deploy_keys] == [
        ("dev", made.keys[0].fingerprint)
    ]
    replaced = lakana_client.repositories.deploy_key(
        "analytics", environments=["dev"], replace=True
    )
    assert replaced.replaced == ["dev"] and len(replaced.notes) == 1
    with pytest.raises(ValidationError, match="--environment"):
        lakana_client.repositories.deploy_key("analytics", replace=True)
