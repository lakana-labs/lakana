"""Tests for the tokens resource of the platform API."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import lakana_server.ui
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.db import create_engine
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import Grant, PersonalAccessToken, Principal
from lakana_server.app import create_app
from lakana_server.settings import Settings, UiConfig
from tests.conftest import AUTH_HEADER, PUBLIC, write_ui_dist
from tests.server_test_helpers import session_headers, signed_in


def _admin_ref(session: Session) -> PrincipalRef:
    """Build a `PrincipalRef` for the bootstrap admin, for a direct service call.

    Parameters
    ----------
    session
        Database session to look up the real admin row on.

    Returns
    -------
    PrincipalRef
        A reference naming the bootstrap admin, backed by its real row's
        id. `Grant.granted_by` is a non-nullable foreign key onto
        `principals.id` (unlike the identity checks `AllowAllAuthorization`
        skips), so the id must match a real, already-persisted principal —
        the bootstrap admin, created at app startup, is exactly that row.
    """
    admin_row = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    return PrincipalRef(id=admin_row.id, name=admin_row.name, type=admin_row.type)


def _grant_admin(settings: Settings, principal_name: str) -> None:
    """Grant `principal_name` `admin` at instance scope, via a direct service call.

    Bypasses the grants HTTP API (not built until a later task) and goes
    straight to `GrantService` against the same database the running
    `client` fixture's app uses, mirroring the direct-service-call pattern
    `test_task_tokens.py` already uses for out-of-router operations.

    Parameters
    ----------
    settings
        The same settings the `client` fixture's app was built from.
    principal_name
        The principal to grant `admin` to.
    """
    engine = create_engine(settings.database)
    with Session(engine) as session:
        principal = session.execute(
            select(Principal).where(Principal.name == principal_name)
        ).scalar_one()
        GrantService(session, AllowAllAuthorization()).grant(
            _admin_ref(session),
            principal=principal.name,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
        )
    engine.dispose()


def test_issue_list_and_revoke_a_token(client: TestClient) -> None:
    """A token can be issued, appears in the list without its secret, then revoked."""
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
    )
    issued = client.post(
        "/api/v1/principals/ci-bot/tokens", headers=AUTH_HEADER, json={"name": "ci"}
    )
    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert body["token"].startswith("lakana_pat_")
    key_id = body["key_id"]

    listed = client.get("/api/v1/principals/ci-bot/tokens", headers=AUTH_HEADER)
    assert listed.status_code == 200
    [row] = listed.json()
    assert {k: row[k] for k in ("key_id", "name", "owner", "last_used_at")} == {
        "key_id": key_id,
        "name": "ci",
        "owner": "ci-bot",
        "last_used_at": None,
    }

    revoked = client.delete(f"/api/v1/principals/ci-bot/tokens/{key_id}", headers=AUTH_HEADER)
    assert revoked.status_code == 204


def test_issued_token_authenticates(client: TestClient, settings: Settings) -> None:
    """The token issue returns is a real, verifiable credential for its principal."""
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
    )
    _grant_admin(settings, "ci-bot")
    issued = client.post(
        "/api/v1/principals/ci-bot/tokens", headers=AUTH_HEADER, json={"name": "ci"}
    )
    token = issued.json()["token"]

    response = client.get("/api/v1/principals", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_listing_a_used_token_succeeds_with_a_populated_last_used_at(
    client: TestClient, settings: Settings
) -> None:
    """Listing a PAT that has actually authenticated a request must not 500.

    `TokenOut.last_used_at` was typed `str | None`, but the underlying ORM
    column is a real `datetime` — Pydantic v2 does not coerce `datetime` to
    `str` under `from_attributes=True`, so `model_validate` raised for any
    row with a non-`None` `last_used_at`. This is every PAT that has ever
    successfully authenticated, including the bootstrap admin's own,
    exercised on every other request in this suite via `AUTH_HEADER`.
    """
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
    )
    _grant_admin(settings, "ci-bot")
    issued = client.post(
        "/api/v1/principals/ci-bot/tokens", headers=AUTH_HEADER, json={"name": "ci"}
    )
    token = issued.json()["token"]

    used = client.get("/api/v1/principals", headers={"Authorization": f"Bearer {token}"})
    assert used.status_code == 200

    listed = client.get("/api/v1/principals/ci-bot/tokens", headers=AUTH_HEADER)
    assert listed.status_code == 200, listed.text
    [pat] = listed.json()
    assert pat["last_used_at"] is not None


def test_a_deactivated_principals_pat_is_rejected(client: TestClient, settings: Settings) -> None:
    """Deactivating a principal immediately revokes its PAT's ability to authenticate.

    This behaviour already has unit coverage at the service level
    (`PatService.verify` against a deactivated principal); this is the
    missing HTTP-level proof of the same headline security property,
    through the real `DELETE /principals/{name}` endpoint.
    """
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
    )
    _grant_admin(settings, "ci-bot")
    issued = client.post(
        "/api/v1/principals/ci-bot/tokens", headers=AUTH_HEADER, json={"name": "ci"}
    )
    token = issued.json()["token"]

    authenticated = client.get("/api/v1/principals", headers={"Authorization": f"Bearer {token}"})
    assert authenticated.status_code == 200

    deactivated = client.delete("/api/v1/principals/ci-bot", headers=AUTH_HEADER)
    assert deactivated.status_code == 204

    rejected = client.get("/api/v1/principals", headers={"Authorization": f"Bearer {token}"})
    assert rejected.status_code == 401


def test_revoke_404s_for_an_unknown_key_id(client: TestClient) -> None:
    """Revoking a key_id that was never issued is a 404."""
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "ci-bot", "type": "SERVICE_ACCOUNT"},
    )
    response = client.delete("/api/v1/principals/ci-bot/tokens/nonexistent", headers=AUTH_HEADER)
    assert response.status_code == 404


def _holder_of(client: TestClient, action: Action) -> dict[str, str]:
    """Create the principal `bob` with a token and one single-action grant at instance scope.

    Parameters
    ----------
    client
        The test client of the app; its engine receives the grant row.
    action
        The single action to grant at instance scope.

    Returns
    -------
    dict[str, str]
        The `Authorization` header carrying bob's token.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "SERVICE_ACCOUNT"}
    )
    issued = client.post("/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "t"})
    with Session(client.app.state.engine) as session:
        bob = session.execute(select(Principal).where(Principal.name == "bob")).scalar_one()
        session.add(
            Grant(
                id=uuid.uuid4(),
                principal_id=bob.id,
                action=action,
                kind=SecurableKind.INSTANCE,
                granted_by=bob.id,
                granted_at=datetime.now(UTC),
            )
        )
        session.commit()
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def test_manage_token_as_a_single_action_is_a_403_on_every_route(client: TestClient) -> None:
    header = _holder_of(client, Action.MANAGE_TOKEN)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "carol", "type": "USER"})
    answers = [
        client.post("/api/v1/principals/carol/tokens", headers=header, json={"name": "mine"}),
        client.get("/api/v1/principals/carol/tokens", headers=header),
        client.delete("/api/v1/principals/carol/tokens/nokey", headers=header),
    ]
    for answer in answers:
        assert answer.status_code == 403, (answer.request.method, str(answer.request.url))
        assert "MANAGE_TOKEN needs the ADMIN role on the instance" in answer.text
    assert client.get("/api/v1/principals/carol/tokens", headers=AUTH_HEADER).status_code == 200


@pytest.fixture
def browser_client(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Provide a client of an app with the web interface on, at its public address.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.
    tmp_path
        Pytest's per-test directory, for the stand-in build.
    monkeypatch
        Points `lakana_server.ui.UI_DIST` at the stand-in.

    Yields
    ------
    TestClient
        A started client whose browser sessions the app reads.
    """
    monkeypatch.setattr(lakana_server.ui, "UI_DIST", write_ui_dist(tmp_path))
    with TestClient(
        create_app(settings.model_copy(update={"ui": UiConfig(enabled=True)})),
        base_url=PUBLIC,
        headers={"Origin": PUBLIC},
    ) as c:
        yield c


def _bob(client: TestClient) -> dict[str, str]:
    """Create Bob, a user with no grant, and give him a personal token through an administrator.

    Parameters
    ----------
    client
        Test client for the API.

    Returns
    -------
    dict of str to str
        Bob's `Authorization` header.
    """
    created = client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"}
    )
    assert created.status_code == 201, created.text
    token = client.post(
        "/api/v1/principals/bob/tokens", headers=AUTH_HEADER, json={"name": "first"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_post_own_token_with_a_pat_is_403_and_from_a_fresh_session_is_201(
    browser_client: TestClient,
) -> None:
    """A person's token cannot create a token; their fresh browser session can."""
    client = browser_client
    bob = _bob(client)

    refused = client.post("/api/v1/principals/bob/tokens", headers=bob, json={"name": "second"})
    assert refused.status_code == 403, refused.text
    assert "A token cannot create a token" in refused.json()["detail"]

    name, value, csrf = signed_in(client, "bob", touched_at=datetime.now(UTC))
    created = client.post(
        "/api/v1/principals/bob/tokens",
        headers=session_headers(name, value, csrf),
        json={"name": "second"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["token"].startswith("lakana_pat_")

    stale_name, stale_value, stale_csrf = signed_in(
        client, "bob", touched_at=datetime.now(UTC) - timedelta(minutes=16)
    )
    stale = client.post(
        "/api/v1/principals/bob/tokens",
        headers=session_headers(stale_name, stale_value, stale_csrf),
        json={"name": "third"},
    )
    assert stale.status_code == 403
    assert stale.json()["type"] == "StepUpRequired"


def test_get_and_delete_own_tokens_with_a_pat(client: TestClient) -> None:
    """A person lists and revokes their own tokens with a personal token; not another's."""
    bob = _bob(client)

    for other in ("admin", "nobody"):
        assert client.get(f"/api/v1/principals/{other}/tokens", headers=bob).status_code == 403
    listed = client.get("/api/v1/principals/bob/tokens", headers=bob)
    assert listed.status_code == 200, listed.text
    [token] = listed.json()
    revoked = client.delete(f"/api/v1/principals/bob/tokens/{token['key_id']}", headers=bob)
    assert revoked.status_code == 204, revoked.text


def _listed(client: TestClient, headers: dict[str, str], path: str) -> list[dict[str, object]]:
    """Read a token list and return its rows.

    Parameters
    ----------
    client
        The test client.
    headers
        The caller's credentials.
    path
        The list's path and query.

    Returns
    -------
    list of dict
        The rows.
    """
    answer = client.get(path, headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_a_listed_token_says_owner_created_expires_and_revoked(client: TestClient) -> None:
    """A row names its owner and its dates; a revoke sets `revoked_at`."""
    bob = _bob(client)
    [before] = _listed(client, bob, "/api/v1/principals/bob/tokens")
    with Session(client.app.state.engine) as session:
        row = session.execute(select(PersonalAccessToken)).scalars().all()[-1]
        created = row.created_at
    assert before["owner"] == "bob"
    assert datetime.fromisoformat(str(before["created_at"])).replace(tzinfo=UTC) == created.replace(
        tzinfo=UTC
    )
    assert before["expires_at"] is None
    assert before["revoked_at"] is None

    client.delete(f"/api/v1/principals/bob/tokens/{before['key_id']}", headers=bob)
    [after] = _listed(client, AUTH_HEADER, "/api/v1/principals/bob/tokens")
    assert after["revoked_at"] is not None


def test_get_all_tokens_pages_with_the_cursor_header(client: TestClient) -> None:
    """Every owner's live tokens, newest first, a page at a time."""
    _bob(client)
    first = client.get("/api/v1/tokens?limit=1", headers=AUTH_HEADER)
    assert first.status_code == 200, first.text
    assert [row["owner"] for row in first.json()] == ["bob"]
    cursor = first.headers["X-Next-Cursor"]
    second = client.get(f"/api/v1/tokens?limit=1&cursor={cursor}", headers=AUTH_HEADER)
    assert [row["owner"] for row in second.json()] == ["admin"]
    assert "X-Next-Cursor" not in second.headers
    only_bob = client.get("/api/v1/tokens?owner=bob", headers=AUTH_HEADER)
    assert [row["owner"] for row in only_bob.json()] == ["bob"]
    assert client.get("/api/v1/tokens?owner=nobody", headers=AUTH_HEADER).status_code == 404
    assert client.get("/api/v1/tokens?limit=0", headers=AUTH_HEADER).status_code == 422


def test_get_all_tokens_is_403_for_a_person_and_a_task_token(client: TestClient) -> None:
    """Only an administrator's personal credential lists everyone's tokens."""
    bob = _bob(client)
    assert client.get("/api/v1/tokens", headers=bob).status_code == 403
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.sales"],
            "ttl_seconds": 60,
        },
    )
    assert minted.status_code == 201, minted.text
    task = {"Authorization": f"Bearer {minted.json()['token']}"}
    assert client.get("/api/v1/tokens", headers=task).status_code == 403


def test_get_all_tokens_never_holds_a_secret(client: TestClient) -> None:
    """No row of the list carries a token value."""
    _bob(client)
    answer = client.get("/api/v1/tokens", headers=AUTH_HEADER)
    assert answer.status_code == 200
    assert "lakana_pat_" not in answer.text
