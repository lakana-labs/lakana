"""Tests for the environments platform API."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.db import create_engine
from lakana_core.models import Environment
from lakana_server.settings import Settings
from tests.conftest import AUTH_HEADER


def _create(client: TestClient, tmp_path: Path, name: str = "dev") -> dict:
    """Create an environment via the API and return its JSON body.

    Parameters
    ----------
    client
        The test client.
    tmp_path
        Pytest's per-test directory. The environment's storage root is a
        `name`-suffixed subdirectory of it, so two environments created in
        the same test (e.g. `"dev"` and `"prod"`) never share a root.
    name
        Environment name.

    Returns
    -------
    dict
        The created environment's response body.
    """
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": name,
            "storage": {"scheme": "file", "root": f"file://{tmp_path}/{name}", "options": {}},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_then_get_round_trips(client: TestClient, tmp_path: Path) -> None:
    """A created environment can be fetched back by name."""
    created = _create(client, tmp_path)
    response = client.get("/api/v1/environments/dev", headers=AUTH_HEADER)
    assert response.status_code == 200
    assert response.json() == created


def test_create_is_rejected_when_the_name_exists(client: TestClient, tmp_path: Path) -> None:
    """The second create with the same name is a 409."""
    _create(client, tmp_path)
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": f"file://{tmp_path}/other", "options": {}},
        },
    )
    assert response.status_code == 409


def test_get_unknown_is_404(client: TestClient) -> None:
    """A missing environment is a 404, not a 500 or an empty 200."""
    response = client.get("/api/v1/environments/nope", headers=AUTH_HEADER)
    assert response.status_code == 404


def test_list_returns_every_environment(client: TestClient, tmp_path: Path) -> None:
    """An admin's instance-scoped grant sees every environment, unfiltered."""
    _create(client, tmp_path, "dev")
    _create(client, tmp_path, "prd")
    response = client.get("/api/v1/environments", headers=AUTH_HEADER)
    assert response.status_code == 200
    names = {env["name"] for env in response.json()}
    assert names == {"dev", "prd"}


def test_delete_hides_the_environment_and_names_the_restore(
    client: TestClient, tmp_path: Path
) -> None:
    """The deleted environment comes back with its `purge_after`; a get is 404 with the restore."""
    _create(client, tmp_path)
    response = client.delete("/api/v1/environments/dev", headers=AUTH_HEADER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "dev"
    assert body["deleted_at"] is not None and body["purge_after"] is not None
    missing = client.get("/api/v1/environments/dev", headers=AUTH_HEADER)
    assert missing.status_code == 404
    assert "lakana env restore dev" in missing.json()["detail"]
    assert client.get("/api/v1/environments", headers=AUTH_HEADER).json() == []
    assert client.get("/v1/dev/namespaces", headers=AUTH_HEADER).status_code == 404
    assert client.get("/v1/dev/namespaces/gold", headers=AUTH_HEADER).status_code == 404


def test_deleted_environments_are_listed_and_restored(client: TestClient, tmp_path: Path) -> None:
    """`?deleted=true` lists them; `POST .../restore` brings one back as it was."""
    created = _create(client, tmp_path)
    client.delete("/api/v1/environments/dev", headers=AUTH_HEADER)
    listed = client.get("/api/v1/environments?deleted=true", headers=AUTH_HEADER)
    assert listed.status_code == 200
    assert [(e["name"], e["purge_after"] is not None) for e in listed.json()] == [("dev", True)]

    restored = client.post("/api/v1/environments/dev/restore", headers=AUTH_HEADER)

    assert restored.status_code == 200, restored.text
    assert restored.json() == created
    assert client.get("/api/v1/environments/dev", headers=AUTH_HEADER).json() == created
    again = client.post("/api/v1/environments/dev/restore", headers=AUTH_HEADER)
    assert again.status_code == 409
    assert "not deleted" in again.json()["detail"]
    assert client.post("/api/v1/environments/nope/restore", headers=AUTH_HEADER).status_code == 404


def test_the_name_of_a_deleted_environment_is_refused_with_both_ways_out(
    client: TestClient, tmp_path: Path
) -> None:
    """409, naming the restore and the early purge."""
    _create(client, tmp_path)
    client.delete("/api/v1/environments/dev", headers=AUTH_HEADER)
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": f"file://{tmp_path}/dev", "options": {}},
        },
    )
    assert response.status_code == 409
    assert "lakana env restore dev" in response.json()["detail"]
    assert "lakana env delete dev --purge" in response.json()["detail"]


def test_purge_makes_a_deletion_due_at_once(client: TestClient, tmp_path: Path) -> None:
    """`?purge=true`: `purge_after` is the deletion's time; a restore is refused."""
    _create(client, tmp_path)
    response = client.delete("/api/v1/environments/dev?purge=true", headers=AUTH_HEADER)
    assert response.status_code == 200, response.text
    assert response.json()["purge_after"] == response.json()["deleted_at"]
    again = client.delete("/api/v1/environments/dev?purge=true", headers=AUTH_HEADER)
    assert again.status_code == 200, again.text
    refused = client.post("/api/v1/environments/dev/restore", headers=AUTH_HEADER)
    assert refused.status_code == 409
    assert "undo period ended" in refused.json()["detail"]


def test_an_environment_admin_deletes_but_an_instance_admin_restores(
    client: TestClient, tmp_path: Path
) -> None:
    """The deletion revokes the environment's own grants, the deleter's among them."""
    _create(client, tmp_path, "sandbox")
    bob = _grant_environment_admin(client, "sandbox")
    assert client.delete("/api/v1/environments/sandbox", headers=bob).status_code == 200
    assert client.post("/api/v1/environments/sandbox/restore", headers=bob).status_code == 403
    restored = client.post("/api/v1/environments/sandbox/restore", headers=AUTH_HEADER)
    assert restored.status_code == 200, restored.text
    assert client.get("/api/v1/environments/sandbox", headers=bob).status_code == 200


def test_delete_unknown_is_404(client: TestClient) -> None:
    """Deleting a missing environment is an error, not a silent success."""
    response = client.delete("/api/v1/environments/nope", headers=AUTH_HEADER)
    assert response.status_code == 404


def _storage_options(settings: Settings, name: str) -> dict[str, str]:
    """Read an environment's `storage_options` straight from the database.

    `EnvironmentOut` does not expose `storage_options`, so a PATCH
    response alone cannot prove the update persisted — this reads the row
    back directly, against the same database the `client` fixture's app
    uses, mirroring `test_tokens.py`'s own direct-database-access pattern.

    Parameters
    ----------
    settings
        The same settings the `client` fixture's app was built from.
    name
        Environment name.

    Returns
    -------
    dict of str to str
        The persisted `storage_options`.
    """
    engine = create_engine(settings.database)
    with Session(engine) as session:
        env = session.execute(select(Environment).where(Environment.name == name)).scalar_one()
        options = dict(env.storage_options)
    engine.dispose()
    return options


def test_update_environment_storage(client: TestClient, settings: Settings, tmp_path: Path) -> None:
    """PATCH .../storage updates an environment's storage options, and it persists."""
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": f"file://{tmp_path}"},
        },
    )

    updated = client.patch(
        "/api/v1/environments/dev/storage", headers=AUTH_HEADER, json={"options": {"a": "b"}}
    )
    assert updated.status_code == 200, updated.text
    assert _storage_options(settings, "dev") == {"a": "b"}


def test_update_environment_storage_raises_not_found(client: TestClient) -> None:
    """PATCH .../storage for an environment that doesn't exist is a 404."""
    response = client.patch(
        "/api/v1/environments/nope/storage", headers=AUTH_HEADER, json={"options": {"a": "b"}}
    )
    assert response.status_code == 404


def test_update_environment_storage_sets_the_secret_ref(client: TestClient, tmp_path: Path) -> None:
    """PATCH .../storage with `secret_ref` attaches a secret to the environment's storage."""
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": f"file://{tmp_path}"},
        },
    )
    client.post(
        "/api/v1/environments/dev/secrets",
        headers=AUTH_HEADER,
        json={"name": "s3-creds", "values": {"access_key_id": "AKIA123"}},
    )

    updated = client.patch(
        "/api/v1/environments/dev/storage", headers=AUTH_HEADER, json={"secret_ref": "s3-creds"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["storage_secret_ref"] == "s3-creds"

    refetched = client.get("/api/v1/environments/dev", headers=AUTH_HEADER)
    assert refetched.json()["storage_secret_ref"] == "s3-creds"


def test_update_environment_storage_secret_ref_requires_manage_secret(
    client: TestClient, tmp_path: Path
) -> None:
    """Setting `secret_ref` needs MANAGE_SECRET, not just instance-scoped MANAGE_ENVIRONMENT.

    A confused-deputy check: a caller who can administer environments'
    storage settings in general must not thereby be able to point one at an
    arbitrary secret — `CatalogService.update_environment_storage`
    authorizes `MANAGE_SECRET` separately, only when `secret_ref` is given.
    Bob's grant here is instance-scoped (`update_environment_storage` requires instance-scoped
    `MANAGE_ENVIRONMENT`, not environment-scoped), so the `options`-only
    call still succeeds; only `MANAGE_SECRET` is missing.
    """
    client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": f"file://{tmp_path}"},
        },
    )
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "SERVICE_ACCOUNT"}
    )
    issued = client.post("/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "bob"})
    bob_headers = {"Authorization": f"Bearer {issued.json()['token']}"}
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "bob", "action": "MANAGE_ENVIRONMENT", "kind": "INSTANCE"},
    )
    assert granted.status_code == 201, granted.text

    options_only = client.patch(
        "/api/v1/environments/dev/storage", headers=bob_headers, json={"options": {"a": "b"}}
    )
    assert options_only.status_code == 200, options_only.text

    with_secret_ref = client.patch(
        "/api/v1/environments/dev/storage", headers=bob_headers, json={"secret_ref": "s3-creds"}
    )
    assert with_secret_ref.status_code == 403


def _token_for(client: TestClient, name: str) -> dict[str, str]:
    """Create a service-account principal with a PAT and return its auth header.

    Parameters
    ----------
    client
        The test client.
    name
        Principal name.

    Returns
    -------
    dict of str to str
        An Authorization header for the new principal.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "SERVICE_ACCOUNT"}
    )
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def _grant_environment_admin(client: TestClient, environment: str) -> dict[str, str]:
    """Create bob, grant him the ADMIN role scoped to one environment, and return his header.

    Parameters
    ----------
    client
        The test client.
    environment
        The environment bob's grant is scoped to. Must already exist.

    Returns
    -------
    dict of str to str
        Bob's Authorization header.
    """
    bob = _token_for(client, "bob")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "bob",
            "role": "ADMIN",
            "kind": "ENVIRONMENT",
            "environment": environment,
        },
    )
    assert granted.status_code == 201, granted.text
    return bob


def test_environment_scoped_admin_cannot_create_an_environment(
    client: TestClient, tmp_path: Path
) -> None:
    """An environment-scoped ADMIN must not be able to create a new environment.

    `create_environment` now requires instance-scoped `MANAGE_ENVIRONMENT`
    — an ENVIRONMENT-kind grant, however broad the role, only ever applies
    to the one environment it names, not to environments that don't exist
    yet.
    """
    _create(client, tmp_path, "sandbox")
    bob = _grant_environment_admin(client, "sandbox")

    response = client.post(
        "/api/v1/environments",
        headers=bob,
        json={
            "name": "sandbox2",
            "storage": {"scheme": "file", "root": f"file://{tmp_path}/sandbox2", "options": {}},
        },
    )
    assert response.status_code == 403


def test_environment_scoped_admin_cannot_repoint_storage(
    client: TestClient, tmp_path: Path
) -> None:
    """An environment-scoped ADMIN must not be able to repoint its own environment's storage.

    `update_environment_storage` now requires instance-scoped
    `MANAGE_ENVIRONMENT` too, closing the re-rooting hole this grant used
    to be sufficient for.
    """
    _create(client, tmp_path, "sandbox")
    bob = _grant_environment_admin(client, "sandbox")

    response = client.patch(
        "/api/v1/environments/sandbox/storage", headers=bob, json={"options": {"a": "b"}}
    )
    assert response.status_code == 403


def test_protected_environment_cannot_be_deleted(client: TestClient, tmp_path: Path) -> None:
    """A protected environment refuses DELETE, whoever asks.

    `delete_environment` still accepts environment-scoped
    `MANAGE_ENVIRONMENT` — bob's grant authorizes the call — but
    `is_protected` refuses it outright (422) regardless of who is asking.
    The refusal is total, not partial: the environment still exists
    afterwards, and bob's grant — which `revoke_attached` would otherwise
    have soft-revoked as part of the delete — is still active.
    """
    _create(client, tmp_path, "sandbox")
    bob = _grant_environment_admin(client, "sandbox")
    protect = client.patch(
        "/api/v1/environments/sandbox", headers=AUTH_HEADER, json={"is_protected": True}
    )
    assert protect.status_code == 200, protect.text

    assert client.delete("/api/v1/environments/sandbox", headers=bob).status_code == 422
    assert client.delete("/api/v1/environments/sandbox", headers=AUTH_HEADER).status_code == 422

    assert client.get("/api/v1/environments/sandbox", headers=AUTH_HEADER).status_code == 200
    active_grants = client.get(
        "/api/v1/grants", headers=AUTH_HEADER, params={"environment": "sandbox"}
    )
    assert active_grants.status_code == 200
    assert len(active_grants.json()) == 1


def test_protected_environment_storage_is_frozen(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    """A protected environment's storage cannot be repointed, even by the instance admin.

    The refusal leaves the stored options untouched, not merely raising
    after partially applying the change.
    """
    _create(client, tmp_path, "sandbox")
    protect = client.patch(
        "/api/v1/environments/sandbox", headers=AUTH_HEADER, json={"is_protected": True}
    )
    assert protect.status_code == 200, protect.text
    options_before = _storage_options(settings, "sandbox")

    response = client.patch(
        "/api/v1/environments/sandbox/storage", headers=AUTH_HEADER, json={"options": {"a": "b"}}
    )
    assert response.status_code == 422

    assert client.get("/api/v1/environments/sandbox", headers=AUTH_HEADER).status_code == 200
    assert _storage_options(settings, "sandbox") == options_before


def test_only_instance_admin_can_change_protection(client: TestClient, tmp_path: Path) -> None:
    """Only an instance-scoped MANAGE_ENVIRONMENT grant may toggle is_protected."""
    _create(client, tmp_path, "sandbox")
    bob = _grant_environment_admin(client, "sandbox")

    denied = client.patch("/api/v1/environments/sandbox", headers=bob, json={"is_protected": True})
    assert denied.status_code == 403

    allowed = client.patch(
        "/api/v1/environments/sandbox", headers=AUTH_HEADER, json={"is_protected": True}
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["is_protected"] is True


def test_protection_patch_rejects_a_non_boolean_is_protected(
    client: TestClient, tmp_path: Path
) -> None:
    """`is_protected` is `StrictBool`: a string like `"true"` is refused, not coerced."""
    _create(client, tmp_path, "sandbox")

    response = client.patch(
        "/api/v1/environments/sandbox", headers=AUTH_HEADER, json={"is_protected": "true"}
    )
    assert response.status_code == 422


def test_protection_patch_rejects_an_unknown_field(client: TestClient, tmp_path: Path) -> None:
    """An unrecognised field in the PATCH body is refused, not silently ignored."""
    _create(client, tmp_path, "sandbox")

    response = client.patch(
        "/api/v1/environments/sandbox",
        headers=AUTH_HEADER,
        json={"is_protected": True, "storage_root": "file:///tmp/elsewhere"},
    )
    assert response.status_code == 422


def test_list_is_filtered_by_grants(client: TestClient, settings: Settings, tmp_path: Path) -> None:
    """Admin sees all, env-scoped alice sees hers, bob with no grants gets [] not 403.

    Also proves the filter writes no audit rows: hiding prod from alice and
    both from bob is a visibility filter, not a denial.
    """
    from lakana_core.models import AuditEvent, AuditEventType

    _create(client, tmp_path, "dev")
    _create(client, tmp_path, "prod")
    alice = _token_for(client, "alice")
    bob = _token_for(client, "bob")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "alice", "role": "VIEWER", "kind": "ENVIRONMENT", "environment": "dev"},
    )
    assert granted.status_code == 201, granted.text
    engine = create_engine(settings.database)
    with Session(engine) as session:
        denials_before = len(
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED
                )
            )
            .scalars()
            .all()
        )

    admin_list = client.get("/api/v1/environments", headers=AUTH_HEADER)
    alice_list = client.get("/api/v1/environments", headers=alice)
    bob_list = client.get("/api/v1/environments", headers=bob)

    assert {e["name"] for e in admin_list.json()} == {"dev", "prod"}
    assert [e["name"] for e in alice_list.json()] == ["dev"]
    assert bob_list.status_code == 200
    assert bob_list.json() == []
    with Session(engine) as session:
        denials_after = len(
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED
                )
            )
            .scalars()
            .all()
        )
    engine.dispose()
    assert denials_after == denials_before


@pytest.mark.slow
def test_task_token_lists_only_its_own_environment(client: TestClient, tmp_path: Path) -> None:
    """A task token for the admin, scoped to dev, lists dev only, not everything the admin could."""
    _create(client, tmp_path, "dev")
    _create(client, tmp_path, "prod")
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.*"],
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text
    task = {"Authorization": f"Bearer {minted.json()['token']}"}

    listed = client.get("/api/v1/environments", headers=task)

    assert listed.status_code == 200
    assert [e["name"] for e in listed.json()] == ["dev"]


def test_list_still_requires_authentication(client: TestClient) -> None:
    """Filtering replaced the authorization check, not authentication: no token is 401."""
    assert client.get("/api/v1/environments").status_code == 401


def test_changing_options_of_an_environment_with_a_storage_secret_is_403_and_audited(
    client: TestClient, settings: Settings, tmp_path: Path
) -> None:
    """MANAGE_ENVIRONMENT alone gets 403 with the message; the denial row persists (#134)."""
    from lakana_core.models import AuditEvent, AuditEventType

    _create(client, tmp_path)
    attached = client.patch(
        "/api/v1/environments/dev/storage", headers=AUTH_HEADER, json={"secret_ref": "s3-creds"}
    )
    assert attached.status_code == 200, attached.text
    bob = _token_for(client, "bob")
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "bob", "action": "MANAGE_ENVIRONMENT", "kind": "INSTANCE"},
    )
    assert granted.status_code == 201, granted.text

    response = client.patch(
        "/api/v1/environments/dev/storage",
        headers=bob,
        json={"options": {"s3.endpoint": "https://evil.example.org"}},
    )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "would send its storage secret 's3-creds'" in detail
    assert "the right to manage secrets in 'dev'" in detail
    assert _storage_options(settings, "dev") == {}
    engine = create_engine(settings.database)
    with Session(engine) as session:
        reasons = [
            row.detail.get("reason")
            for row in session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED
                )
            ).scalars()
        ]
    engine.dispose()
    assert "storage_options_move_secret" in reasons


def test_a_storage_root_written_another_way_is_422_with_the_spelling_to_use(
    client: TestClient, tmp_path: Path
) -> None:
    """``file:////...`` is refused, and the answer says how to write it."""
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={
            "name": "dev",
            "storage": {"scheme": "file", "root": f"file:///{tmp_path}/dev", "options": {}},
        },
    )
    assert response.status_code == 422, response.text
    assert f"file://{tmp_path}/dev" in response.json()["detail"]
    assert client.get("/api/v1/environments/dev", headers=AUTH_HEADER).status_code == 404


def test_a_storage_root_inside_another_environments_managed_files_is_422_naming_it(
    client: TestClient, tmp_path: Path
) -> None:
    """The answer names the other environment and a root of its own."""
    for name, root in (("prod", f"file://{tmp_path}"), ("dev", f"file://{tmp_path}/managed/dev")):
        response = client.post(
            "/api/v1/environments",
            headers=AUTH_HEADER,
            json={"name": name, "storage": {"scheme": "file", "root": root, "options": {}}},
        )
    assert response.status_code == 422, response.text
    assert "'prod'" in response.json()["detail"]
    assert f"'file://{tmp_path}/dev'" in response.json()["detail"]
    assert client.get("/api/v1/environments/dev", headers=AUTH_HEADER).status_code == 404


def test_a_storage_root_in_another_scheme_is_422_naming_both_schemes(client: TestClient) -> None:
    """``local://`` on a ``file`` environment is each process's working directory: refused."""
    response = client.post(
        "/api/v1/environments",
        headers=AUTH_HEADER,
        json={"name": "dev", "storage": {"scheme": "file", "root": "local:///data", "options": {}}},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "'local'" in detail
    assert "storage scheme is 'file'" in detail
    assert "'file:///data/lakana'" in detail
    assert client.get("/api/v1/environments/dev", headers=AUTH_HEADER).status_code == 404
