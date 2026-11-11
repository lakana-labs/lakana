"""The repositories resource of the platform API."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import Environment, EnvironmentDeployment, JobVersion, Repository
from lakana_server.app import create_app
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment


def _token_for(client: TestClient, name: str) -> dict[str, str]:
    """Create a service account with a PAT and return its auth header.

    Parameters
    ----------
    client
        The test client.
    name
        Principal name.

    Returns
    -------
    dict of str to str
        An Authorization header.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "SERVICE_ACCOUNT"}
    )
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


_HTTPS = {
    "name": "analytics",
    "kind": "https",
    "url": "https://github.com/acme/analytics.git",
    "credential_secret": "github-token",
    "default_ref": "main",
}


def test_register_list_show_update_delete(client: TestClient) -> None:
    """The full lifecycle as an admin."""
    created = client.post("/api/v1/repositories", headers=AUTH_HEADER, json=_HTTPS)
    assert created.status_code == 201, created.text
    assert created.json()["kind"] == "https"
    assert created.json()["credential_username"] is None
    listed = client.get("/api/v1/repositories", headers=AUTH_HEADER)
    assert [r["name"] for r in listed.json()] == ["analytics"]
    shown = client.get("/api/v1/repositories/analytics", headers=AUTH_HEADER)
    assert shown.json()["default_ref"] == "main"
    patched = client.patch(
        "/api/v1/repositories/analytics",
        headers=AUTH_HEADER,
        json={"default_ref": None, "credential_username": "gitlab+deploy"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["default_ref"] is None
    assert patched.json()["credential_username"] == "gitlab+deploy"
    assert client.delete("/api/v1/repositories/analytics", headers=AUTH_HEADER).status_code == 204
    assert client.get("/api/v1/repositories/analytics", headers=AUTH_HEADER).status_code == 404


def test_name_and_kind_cannot_be_patched(client: TestClient) -> None:
    """Unknown or fixed fields are a 422."""
    client.post("/api/v1/repositories", headers=AUTH_HEADER, json=_HTTPS)
    response = client.patch(
        "/api/v1/repositories/analytics", headers=AUTH_HEADER, json={"kind": "ssh"}
    )
    assert response.status_code == 422


def test_invalid_url_and_duplicates(client: TestClient) -> None:
    """Credentials in the URL are a 422; a second registration of a name is a 409."""
    bad = client.post(
        "/api/v1/repositories",
        headers=AUTH_HEADER,
        json={**_HTTPS, "url": "https://u:token@github.com/a/b"},
    )
    assert bad.status_code == 422
    assert "token" not in bad.text
    client.post("/api/v1/repositories", headers=AUTH_HEADER, json=_HTTPS)
    assert client.post("/api/v1/repositories", headers=AUTH_HEADER, json=_HTTPS).status_code == 409


def test_non_admin_cannot_register_or_list(client: TestClient) -> None:
    """MANAGE_REPOSITORY to register; READ_ENVIRONMENT somewhere to list."""
    header = _token_for(client, "ci")
    assert client.post("/api/v1/repositories", headers=header, json=_HTTPS).status_code == 403
    assert client.get("/api/v1/repositories", headers=header).status_code == 403


def test_local_repository_honours_repo_roots(settings: Settings, tmp_path: Path) -> None:
    """The server checks LAKANA_REPO_ROOTS at registration."""
    (tmp_path / "repos" / "analytics").mkdir(parents=True)
    settings = settings.model_copy(update={"repo_roots": [str(tmp_path / "repos")]})
    with TestClient(create_app(settings)) as client:
        ok = client.post(
            "/api/v1/repositories",
            headers=AUTH_HEADER,
            json={"name": "local", "kind": "local", "url": str(tmp_path / "repos" / "analytics")},
        )
        assert ok.status_code == 201, ok.text
        outside = client.post(
            "/api/v1/repositories",
            headers=AUTH_HEADER,
            json={"name": "escape", "kind": "local", "url": str(tmp_path)},
        )
        assert outside.status_code == 422


def test_responses_carry_the_secret_name_never_a_value(client: TestClient) -> None:
    """Only the bound secret's name is returned; known_hosts and public deploy keys may be."""
    created = client.post(
        "/api/v1/repositories",
        headers=AUTH_HEADER,
        json={
            "name": "infra",
            "kind": "ssh",
            "url": "git@gitlab.internal:data/infra.git",
            "credential_secret": "deploy-key",
            "ssh_known_hosts": "gitlab.internal ssh-ed25519 AAAA\n",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["credential_secret"] == "deploy-key"
    assert created.json()["ssh_known_hosts"] == "gitlab.internal ssh-ed25519 AAAA\n"
    assert set(created.json()) == {
        "id",
        "name",
        "kind",
        "url",
        "credential_secret",
        "credential_username",
        "ssh_known_hosts",
        "default_ref",
        "created_by",
        "created_at",
        "updated_at",
        "deploy_keys",
    }
    assert created.json()["deploy_keys"] == []


def test_unknown_repository_is_not_revealed_to_a_caller_without_read_rights(
    client: TestClient,
) -> None:
    """A caller who cannot read gets the same 403 for a real and a missing name."""
    client.post("/api/v1/repositories", headers=AUTH_HEADER, json=_HTTPS)
    header = _token_for(client, "ci")
    real = client.get("/api/v1/repositories/analytics", headers=header)
    missing = client.get("/api/v1/repositories/nothing-here", headers=header)
    assert real.status_code == missing.status_code == 403
    assert client.get("/api/v1/repositories/nothing-here", headers=AUTH_HEADER).status_code == 404
    for response in (
        client.patch("/api/v1/repositories/analytics", headers=header, json={"default_ref": "x"}),
        client.patch(
            "/api/v1/repositories/nothing-here", headers=header, json={"default_ref": "x"}
        ),
        client.delete("/api/v1/repositories/analytics", headers=header),
        client.delete("/api/v1/repositories/nothing-here", headers=header),
    ):
        assert response.status_code == 403


def test_unknown_repository_is_404_for_an_admin(client: TestClient) -> None:
    """An admin gets 404 on PATCH and DELETE of an unknown name."""
    assert client.delete("/api/v1/repositories/nothing", headers=AUTH_HEADER).status_code == 404
    patched = client.patch(
        "/api/v1/repositories/nothing", headers=AUTH_HEADER, json={"default_ref": "x"}
    )
    assert patched.status_code == 404


def test_delete_is_a_409_while_the_repository_is_deployed(client: TestClient) -> None:
    """A repository an environment currently deploys from cannot be deleted; undeploy first."""
    create_environment(client, "dev")
    assert client.post("/api/v1/repositories", headers=AUTH_HEADER, json=_HTTPS).status_code == 201
    with Session(client.app.state.engine) as session:
        env = session.scalars(select(Environment)).one()
        repo = session.scalars(select(Repository)).one()
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json={"version": 1, "assets": [], "edges": [], "external_inputs": []},
            code_uri="s3://b/x.tar.gz",
            code_sha256="0" * 64,
        )
        session.add(version)
        session.flush()
        session.add(
            EnvironmentDeployment(
                environment_id=env.id, repository_id=repo.id, current_job_version_id=version.id
            )
        )
        session.commit()
    refused = client.delete("/api/v1/repositories/analytics", headers=AUTH_HEADER)
    assert refused.status_code == 409, refused.text
    assert "lakana undeploy analytics --env dev" in refused.json()["detail"]
    assert client.get("/api/v1/repositories/analytics", headers=AUTH_HEADER).status_code == 200


def test_a_read_environment_reader_can_list_and_show_but_not_register(
    client: TestClient,
) -> None:
    """READ_ENVIRONMENT on any environment is enough to read repositories, not to change them."""
    create_environment(client, "dev")
    assert client.post("/api/v1/repositories", headers=AUTH_HEADER, json=_HTTPS).status_code == 201
    header = _token_for(client, "reader")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "reader",
            "action": "READ_ENVIRONMENT",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert granted.status_code in (200, 201), granted.text
    listed = client.get("/api/v1/repositories", headers=header)
    assert listed.status_code == 200, listed.text
    assert [r["name"] for r in listed.json()] == ["analytics"]
    assert client.get("/api/v1/repositories/analytics", headers=header).status_code == 200
    assert (
        client.post(
            "/api/v1/repositories", headers=header, json={**_HTTPS, "name": "x"}
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/v1/repositories/analytics", headers=header, json={"default_ref": "x"}
        ).status_code
        == 403
    )
    assert client.delete("/api/v1/repositories/analytics", headers=header).status_code == 403


def test_an_empty_patch_is_a_422(client: TestClient) -> None:
    """A PATCH that names nothing is refused instead of auditing a no-op."""
    assert client.post("/api/v1/repositories", headers=AUTH_HEADER, json=_HTTPS).status_code == 201
    response = client.patch("/api/v1/repositories/analytics", headers=AUTH_HEADER, json={})
    assert response.status_code == 422, response.text
