"""Tests for the task-tokens resource of the platform API."""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.models import AuditEvent, AuditEventType, Base, Grant, Principal, TaskToken
from lakana_server.app import create_app
from lakana_server.settings import Settings, TaskTokensConfig
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import session_headers, signed_in


@contextmanager
def _client_with_max_ttl(tmp_path: Path, max_ttl_seconds: int) -> Generator[TestClient, None, None]:
    """Build a `TestClient` whose configured task-token TTL cap is `max_ttl_seconds`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test temporary directory, for an isolated database.
    max_ttl_seconds
        The value to set `Settings.task_tokens.max_ttl_seconds` to.

    Yields
    ------
    TestClient
        A client for an app built with that setting.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    Base.metadata.create_all(engine)
    engine.dispose()
    settings = Settings(
        database=DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"),
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="testsecret",
        task_tokens=TaskTokensConfig(max_ttl_seconds=max_ttl_seconds),
    )
    with TestClient(create_app(settings)) as client:
        yield client


def _admin_ref(session: Session) -> PrincipalRef:
    """Build a `PrincipalRef` for the real bootstrap admin row, for direct service calls.

    `TaskTokenService.mint`/`revoke` now record an `AuditEvent` in the same
    transaction, and `AuditEvent.principal_id` is a
    foreign key onto `principals.id` — unlike before, this must resolve to
    the bootstrap admin's real row rather than a freshly made-up id, or
    that insert fails its foreign key.

    Parameters
    ----------
    session
        Database session to look the bootstrap admin principal up in.

    Returns
    -------
    PrincipalRef
        A reference to the real, persisted bootstrap admin principal.
    """
    row = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def test_task_tokens_config_rejects_a_non_positive_max_ttl() -> None:
    """`max_ttl_seconds=0` fails at settings load.

    `TaskTokensConfig.max_ttl_seconds` is `Field(default=86_400, gt=0)` — a
    zero or negative operator-configured cap would otherwise reject every
    mint (`ttl_seconds > 0` from the schema always exceeds a `<= 0` cap),
    turning a misconfiguration into total task-token outage instead of a
    clear failure at startup.
    """
    with pytest.raises(PydanticValidationError):
        TaskTokensConfig(max_ttl_seconds=0)


def test_mint_a_task_token_that_authenticates(client: TestClient) -> None:
    """A minted task token authenticates but cannot pass an instance-scoped check."""
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.sales"],
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text
    token = minted.json()["token"]
    assert token.startswith("lakana_task_")

    # A task token can authenticate but, by design, can never pass an
    # instance-scoped authorization check (its scope only ever narrows to
    # one declared environment) — regardless of what its principal could
    # otherwise do. A 403, not a 401, is exactly the proof this token
    # authenticated: PermissionDenied is only ever raised for an
    # already-resolved, real principal.
    response = client.get("/api/v1/principals", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_mint_422s_for_a_non_positive_ttl(client: TestClient) -> None:
    """A zero or negative ttl_seconds is rejected at the schema level, not accepted."""
    for ttl in (0, -1):
        response = client.post(
            "/api/v1/task-tokens",
            headers=AUTH_HEADER,
            json={
                "principal": "admin",
                "environment": "dev",
                "asset_scope": [],
                "ttl_seconds": ttl,
            },
        )
        assert response.status_code == 422, response.text


def _mint(client: TestClient) -> dict[str, str]:
    """Mint a short-lived task token for the bootstrap admin over HTTP.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    dict[str, str]
        The mint response body: `key_id` and `token`.
    """
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={"principal": "admin", "environment": "dev", "asset_scope": [], "ttl_seconds": 300},
    )
    assert minted.status_code == 201, minted.text
    return minted.json()


def test_delete_revokes_a_task_token_over_http(client: TestClient) -> None:
    """`DELETE /api/v1/task-tokens/{key_id}` revokes it; the token stops authenticating."""
    minted = _mint(client)

    response = client.delete(f"/api/v1/task-tokens/{minted['key_id']}", headers=AUTH_HEADER)
    assert response.status_code == 204, response.text

    denied = client.get(
        "/api/v1/principals", headers={"Authorization": f"Bearer {minted['token']}"}
    )
    assert denied.status_code == 401


def test_delete_is_idempotent_and_audited_once(client: TestClient) -> None:
    """A second revoke is a 204 no-op and records no second `TASK_TOKEN_REVOKED` event."""
    key_id = _mint(client)["key_id"]

    for _ in range(2):
        response = client.delete(f"/api/v1/task-tokens/{key_id}", headers=AUTH_HEADER)
        assert response.status_code == 204, response.text

    events = client.get(
        "/api/v1/audit-events", headers=AUTH_HEADER, params={"event_type": "TASK_TOKEN_REVOKED"}
    )
    assert events.status_code == 200, events.text
    revoked = [e for e in events.json()["items"] if e["detail"]["key_id"] == key_id]
    assert len(revoked) == 1


def test_delete_404s_for_an_unknown_key_id(client: TestClient) -> None:
    """Revoking a key_id no task token has is a 404."""
    response = client.delete("/api/v1/task-tokens/nosuchkey", headers=AUTH_HEADER)
    assert response.status_code == 404, response.text


def test_a_task_token_cannot_revoke_task_tokens(client: TestClient) -> None:
    """Revoking needs `MINT_TASK_TOKEN`, an instance-scoped action a task token never passes."""
    victim = _mint(client)["key_id"]
    caller = _mint(client)["token"]

    response = client.delete(
        f"/api/v1/task-tokens/{victim}", headers={"Authorization": f"Bearer {caller}"}
    )
    assert response.status_code == 403, response.text


def test_an_expired_task_token_is_rejected_via_the_api(
    client: TestClient, settings: Settings
) -> None:
    """An already-expired task token is rejected over HTTP, not just at the service layer.

    Minted directly via `TaskTokenService`, bypassing the HTTP schema's
    `ttl_seconds > 0` bound (Fix 7) — that bound exists to stop a caller
    from *requesting* an already-dead token, not to prevent this test from
    constructing one to prove verification actually checks expiry.
    """
    engine = create_engine(settings.database)
    with Session(engine) as session:
        minted = TaskTokenService(session, AllowAllAuthorization()).mint(
            _admin_ref(session), "admin", environment="dev", asset_scope=[], ttl_seconds=-1
        )
    engine.dispose()

    response = client.get("/api/v1/principals", headers={"Authorization": f"Bearer {minted.token}"})
    assert response.status_code == 401


def test_mint_422s_for_a_wildcard_only_asset_pattern(client: TestClient) -> None:
    """Minting with '.*' in asset_scope is rejected, over the real API."""
    response = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": [".*"],
            "ttl_seconds": 300,
        },
    )
    assert response.status_code == 422, response.text


def test_mint_422s_for_a_ttl_over_the_configured_max(tmp_path: Path) -> None:
    """Minting with ttl_seconds above the configured max is rejected, over the real API."""
    with _client_with_max_ttl(tmp_path, 3600) as client:
        response = client.post(
            "/api/v1/task-tokens",
            headers=AUTH_HEADER,
            json={
                "principal": "admin",
                "environment": "dev",
                "asset_scope": ["bronze.*"],
                "ttl_seconds": 3601,
            },
        )
    assert response.status_code == 422, response.text


def test_mint_accepts_a_ttl_above_86400_when_the_configured_max_allows_it(
    tmp_path: Path,
) -> None:
    """`LAKANA_TASK_TOKENS__MAX_TTL_SECONDS` can raise the cap, not just lower it.

    `TaskTokenMintIn.ttl_seconds` used to hard-code `Field(gt=0, le=86400)`,
    so a configured max above one day had no effect at the HTTP boundary —
    the request would 422 before `TaskTokenService.mint` ever saw it, even
    though the service itself would have accepted it.
    `TaskTokenService.mint` is now the only enforcer of the upper bound.
    """
    with _client_with_max_ttl(tmp_path, 100_000) as client:
        response = client.post(
            "/api/v1/task-tokens",
            headers=AUTH_HEADER,
            json={
                "principal": "admin",
                "environment": "dev",
                "asset_scope": ["bronze.*"],
                "ttl_seconds": 90_000,
            },
        )
    assert response.status_code == 201, response.text


def test_mint_404s_for_an_unknown_principal(client: TestClient) -> None:
    """Minting for a name nobody created is a 404."""
    response = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "nobody",
            "environment": "dev",
            "asset_scope": [],
            "ttl_seconds": 300,
        },
    )
    assert response.status_code == 404


def test_task_token_cannot_read_a_run_the_admin_pat_submitted(client: TestClient) -> None:
    """A task token scoped to sandbox.* cannot read a run the admin PAT submitted.

    `QueryService.get_query_run` scopes a task-token caller's reads to runs
    submitted with that same token; a run submitted with the admin's own
    PAT has no task token at all, so it is invisible to the task token even
    though both authenticate as the same principal.
    """
    created = client.post(
        "/api/v1/environments",
        json={"name": "dev", "storage": {"scheme": "file", "root": "file:///tmp/x80admin"}},
        headers=AUTH_HEADER,
    )
    assert created.status_code == 201, created.text

    submitted = client.post(
        "/api/v1/environments/dev/queries",
        json={"sql": "SELECT 1"},
        headers=AUTH_HEADER,
    )
    assert submitted.status_code == 202, submitted.text
    query_id = submitted.json()["query_id"]

    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["sandbox.*"],
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text
    token = minted.json()["token"]

    response = client.get(
        f"/api/v1/environments/dev/queries/{query_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


def test_a_rest_request_cannot_produce_a_bound_token(client: TestClient) -> None:
    """No request schema accepts `task_run_id` or `claim`."""
    response = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["gold.x"],
            "ttl_seconds": 60,
            "task_run_id": str(uuid.uuid4()),
            "claim": 1,
        },
    )
    assert response.status_code == 201, response.text
    with Session(client.app.state.engine) as session:
        row = session.execute(
            select(TaskToken).where(TaskToken.key_id == response.json()["key_id"])
        ).scalar_one()
        assert (row.task_run_id, row.claim) == (None, None)


def test_mint_for_another_principal_needs_the_admin_role(client: TestClient) -> None:
    """Over REST: `MINT_TASK_TOKEN` alone mints only for the caller."""
    for name in ("minter", "worker-1"):
        created = client.post(
            "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"}
        )
        assert created.status_code == 201, created.text
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "minter", "action": "MINT_TASK_TOKEN", "kind": "INSTANCE"},
    )
    assert granted.status_code < 300, granted.text
    issued = client.post(
        "/api/v1/principals/minter/tokens", headers=AUTH_HEADER, json={"name": "minter"}
    )
    minter = {"Authorization": f"Bearer {issued.json()['token']}"}
    body = {"environment": "dev", "asset_scope": ["gold.x"], "ttl_seconds": 60}

    own = client.post("/api/v1/task-tokens", headers=minter, json={**body, "principal": "minter"})
    assert own.status_code == 201, own.text
    for subject in ("worker-1", "admin", "nobody"):
        refused = client.post(
            "/api/v1/task-tokens", headers=minter, json={**body, "principal": subject}
        )
        assert refused.status_code == 403, refused.text
        assert "only for itself" in refused.text
    as_admin = client.post(
        "/api/v1/task-tokens", headers=AUTH_HEADER, json={**body, "principal": "worker-1"}
    )
    assert as_admin.status_code == 201, as_admin.text


def _minter_headers(client: TestClient) -> dict[str, str]:
    """Create a principal holding only `MINT_TASK_TOKEN` and return its PAT header.

    Parameters
    ----------
    client
        Test client.

    Returns
    -------
    dict of str to str
        The `Authorization` header of a PAT for the principal `minter`.
    """
    created = client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "minter", "type": "USER"}
    )
    assert created.status_code == 201, created.text
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "minter", "action": "MINT_TASK_TOKEN", "kind": "INSTANCE"},
    )
    assert granted.status_code < 300, granted.text
    issued = client.post(
        "/api/v1/principals/minter/tokens", headers=AUTH_HEADER, json={"name": "minter"}
    )
    return {"Authorization": f"Bearer {issued.json()['token']}"}


def test_the_mint_refusal_is_audited_and_does_not_reveal_whether_the_target_exists(
    client: TestClient,
) -> None:
    """An existing and a missing target name are refused with identical bodies."""
    minter = _minter_headers(client)
    created = client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "worker-1", "type": "USER"}
    )
    assert created.status_code == 201, created.text
    body = {"environment": "dev", "asset_scope": ["gold.x"], "ttl_seconds": 60}
    existing = client.post(
        "/api/v1/task-tokens", headers=minter, json={**body, "principal": "worker-1"}
    )
    missing = client.post(
        "/api/v1/task-tokens", headers=minter, json={**body, "principal": "nobody"}
    )
    assert (existing.status_code, missing.status_code) == (403, 403)
    assert existing.json() == missing.json()
    with Session(client.app.state.engine) as session:
        events = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED
                )
            )
            .scalars()
            .all()
        )
    details = [e.detail for e in events if (e.detail or {}).get("reason") == "not_self"]
    assert {d["subject"] for d in details} == {"worker-1", "nobody"}
    assert {d["action"] for d in details} == {"MINT_TASK_TOKEN"}


def test_revoke_of_another_principals_token_needs_the_admin_role(client: TestClient) -> None:
    """`MINT_TASK_TOKEN` alone revokes only a token minted for the caller."""
    minter = _minter_headers(client)
    body = {"environment": "dev", "asset_scope": ["gold.x"], "ttl_seconds": 60}
    own = client.post("/api/v1/task-tokens", headers=minter, json={**body, "principal": "minter"})
    assert own.status_code == 201, own.text
    admins = _mint(client)["key_id"]

    own_key = own.json()["key_id"]
    assert client.delete(f"/api/v1/task-tokens/{own_key}", headers=minter).status_code == 204
    refused = client.delete(f"/api/v1/task-tokens/{admins}", headers=minter)
    missing = client.delete("/api/v1/task-tokens/nosuchkey", headers=minter)
    assert (refused.status_code, missing.status_code) == (403, 403)
    assert refused.json() == missing.json()
    assert "only for itself" in refused.text
    assert client.delete(f"/api/v1/task-tokens/{admins}", headers=AUTH_HEADER).status_code == 204
    with Session(client.app.state.engine) as session:
        events = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED
                )
            )
            .scalars()
            .all()
        )
    details = [e.detail for e in events if (e.detail or {}).get("reason") == "not_self"]
    assert len(details) == 2
    assert {d["action"] for d in details} == {"MINT_TASK_TOKEN"}
    assert {d["operation"] for d in details} == {"revoke"}


def test_a_task_token_cannot_mint_at_all(client: TestClient) -> None:
    """A task token is refused `MINT_TASK_TOKEN` even for itself, whoever its principal is."""
    minter = _minter_headers(client)
    body = {"environment": "dev", "asset_scope": ["gold.x"], "ttl_seconds": 60}
    for headers, name in ((minter, "minter"), (AUTH_HEADER, "admin")):
        minted = client.post(
            "/api/v1/task-tokens", headers=headers, json={**body, "principal": name}
        )
        assert minted.status_code == 201, minted.text
        bearer = {"Authorization": f"Bearer {minted.json()['token']}"}
        for subject in ("minter", "admin", "nobody"):
            refused = client.post(
                "/api/v1/task-tokens", headers=bearer, json={**body, "principal": subject}
            )
            assert refused.status_code == 403, refused.text
            assert "Task tokens may not MINT_TASK_TOKEN." in refused.text


def test_an_overlong_principal_name_is_rejected_at_the_schema(client: TestClient) -> None:
    """The refusal's audit row stores the caller-supplied subject, so its length is bounded."""
    minter = _minter_headers(client)
    body = {"environment": "dev", "asset_scope": ["gold.x"], "ttl_seconds": 60}
    too_long = client.post(
        "/api/v1/task-tokens", headers=minter, json={**body, "principal": "x" * 256}
    )
    assert too_long.status_code == 422
    at_limit = client.post(
        "/api/v1/task-tokens", headers=minter, json={**body, "principal": "x" * 255}
    )
    assert at_limit.status_code == 403


def test_get_task_tokens_never_holds_a_secret(client: TestClient) -> None:
    """The list names key ids and scopes; no token value and no `secret_hash` anywhere."""
    minted = _mint(client)

    response = client.get("/api/v1/task-tokens", headers=AUTH_HEADER)

    assert response.status_code == 200, response.text
    assert "lakana_task_" not in response.text
    assert minted["token"] not in response.text
    [row] = response.json()
    assert "secret_hash" not in row
    assert row["key_id"] == minted["key_id"]
    assert row["principal"] == "admin"
    assert (row["run_id"], row["job"], row["asset"]) == (None, None, None)
    assert response.headers.get("X-Next-Cursor") is None


def test_get_task_tokens_pages_and_filters_by_environment(client: TestClient) -> None:
    """`limit` pages with `X-Next-Cursor`; an environment no token names answers `[]`."""
    for environment in ("dev", "dev", "prod"):
        minted = client.post(
            "/api/v1/task-tokens",
            headers=AUTH_HEADER,
            json={
                "principal": "admin",
                "environment": environment,
                "asset_scope": [],
                "ttl_seconds": 300,
            },
        )
        assert minted.status_code == 201, minted.text

    first = client.get("/api/v1/task-tokens", headers=AUTH_HEADER, params={"limit": 2})
    cursor = first.headers["X-Next-Cursor"]
    second = client.get(
        "/api/v1/task-tokens", headers=AUTH_HEADER, params={"limit": 2, "cursor": cursor}
    )
    assert [len(first.json()), len(second.json())] == [2, 1]
    assert "X-Next-Cursor" not in second.headers
    dev = client.get("/api/v1/task-tokens", headers=AUTH_HEADER, params={"environment": "dev"})
    assert [row["environment"] for row in dev.json()] == ["dev", "dev"]
    nowhere = client.get(
        "/api/v1/task-tokens", headers=AUTH_HEADER, params={"environment": "nowhere"}
    )
    assert (nowhere.status_code, nowhere.json()) == (200, [])


def test_get_task_tokens_from_a_browser_session(browser_client: TestClient) -> None:
    """An administrator's session lists the task tokens."""
    _mint(browser_client)
    name, value, csrf = signed_in(browser_client, "admin")

    response = browser_client.get("/api/v1/task-tokens", headers=session_headers(name, value, csrf))

    assert response.status_code == 200, response.text
    assert len(response.json()) == 1


def test_an_administrator_revokes_a_task_token_from_a_browser_session(
    browser_client: TestClient,
) -> None:
    """Cookie and CSRF header: 204, and the token stops authenticating."""
    minted = _mint(browser_client)
    name, value, csrf = signed_in(browser_client, "admin")

    response = browser_client.delete(
        f"/api/v1/task-tokens/{minted['key_id']}", headers=session_headers(name, value, csrf)
    )

    assert response.status_code == 204, response.text
    denied = browser_client.get(
        "/api/v1/principals", headers={"Authorization": f"Bearer {minted['token']}"}
    )
    assert denied.status_code == 401
    assert browser_client.get("/api/v1/task-tokens", headers=AUTH_HEADER).json() == []


def test_minting_from_a_browser_is_still_refused(browser_client: TestClient) -> None:
    """`POST` stays a route for code: a session is refused with `TokenOnlyRoute`."""
    name, value, csrf = signed_in(browser_client, "admin", touched_at=datetime.now(UTC))

    response = browser_client.post(
        "/api/v1/task-tokens",
        headers=session_headers(name, value, csrf),
        json={"principal": "admin", "environment": "dev", "asset_scope": [], "ttl_seconds": 300},
    )

    assert response.status_code == 403, response.text
    assert response.json()["type"] == "TokenOnlyRoute"


def test_a_non_administrators_session_cannot_revoke_anothers_task_token(
    browser_client: TestClient,
) -> None:
    """Letting the cookie through changes no rule: a holder of `MINT_TASK_TOKEN` is refused.

    The same 403 as with a token, for an unknown key id too.
    """
    victim = _mint(browser_client)["key_id"]
    created = browser_client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "bob", "type": "USER"}
    )
    assert created.status_code == 201, created.text
    with Session(browser_client.app.state.engine) as session:
        bob = session.execute(select(Principal).where(Principal.name == "bob")).scalar_one()
        session.add(
            Grant(
                id=uuid.uuid4(),
                principal_id=bob.id,
                action=Action.MINT_TASK_TOKEN,
                kind=SecurableKind.INSTANCE,
                granted_by=bob.id,
                granted_at=datetime.now(UTC),
            )
        )
        session.commit()
    name, value, csrf = signed_in(browser_client, "bob")
    headers = session_headers(name, value, csrf)

    for key_id in (victim, "nosuchkey"):
        response = browser_client.delete(f"/api/v1/task-tokens/{key_id}", headers=headers)
        assert response.status_code == 403, response.text
    assert browser_client.get("/api/v1/task-tokens", headers=headers).status_code == 403
    assert len(browser_client.get("/api/v1/task-tokens", headers=AUTH_HEADER).json()) == 1


def test_revoking_the_personal_token_that_minted_a_task_token_ends_it(
    client: TestClient,
) -> None:
    """Revoking `minter`'s PAT gives its task token 401; the bootstrap PAT's task token still works.

    The cascade is listed in the audit log as a `TASK_TOKEN_REVOKED` with ``reason:
    pat_revoked`` naming the PAT's key id.
    """
    headers = _minter_headers(client)
    pat_key_id = headers["Authorization"].removeprefix("Bearer lakana_pat_").split("_", 1)[0]
    body = {"principal": "minter", "environment": "dev", "asset_scope": [], "ttl_seconds": 300}
    child = client.post("/api/v1/task-tokens", headers=headers, json=body)
    assert child.status_code == 201, child.text
    other = _mint(client)
    child_headers = {"Authorization": f"Bearer {child.json()['token']}"}
    assert client.get("/api/v1/whoami", headers=child_headers).status_code == 200

    revoked = client.delete(f"/api/v1/principals/minter/tokens/{pat_key_id}", headers=AUTH_HEADER)
    assert revoked.status_code == 204, revoked.text

    assert client.get("/api/v1/whoami", headers=child_headers).status_code == 401
    still = client.get("/api/v1/whoami", headers={"Authorization": f"Bearer {other['token']}"})
    assert still.status_code == 200, still.text
    events = client.get(
        "/api/v1/audit-events", headers=AUTH_HEADER, params={"event_type": "TASK_TOKEN_REVOKED"}
    )
    assert [
        (e["detail"]["key_id"], e["detail"]["reason"], e["detail"]["pat_key_id"])
        for e in events.json()["items"]
    ] == [(child.json()["key_id"], "pat_revoked", pat_key_id)]


def test_a_service_accounts_task_token_ends_with_its_personal_token_over_rest(
    client: TestClient,
) -> None:
    """A service account mints with its PAT; revoking that PAT gives the task token 401."""
    created = client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": "etl", "type": "SERVICE_ACCOUNT"}
    )
    assert created.status_code == 201, created.text
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={"principal": "etl", "action": "MINT_TASK_TOKEN", "kind": "INSTANCE"},
    )
    assert granted.status_code < 300, granted.text
    issued = client.post("/api/v1/principals/etl/tokens", headers=AUTH_HEADER, json={"name": "etl"})
    assert issued.status_code == 201, issued.text
    child = client.post(
        "/api/v1/task-tokens",
        headers={"Authorization": f"Bearer {issued.json()['token']}"},
        json={"principal": "etl", "environment": "dev", "asset_scope": [], "ttl_seconds": 300},
    )
    assert child.status_code == 201, child.text
    child_headers = {"Authorization": f"Bearer {child.json()['token']}"}
    assert client.get("/api/v1/whoami", headers=child_headers).status_code == 200

    revoked = client.delete(
        f"/api/v1/principals/etl/tokens/{issued.json()['key_id']}", headers=AUTH_HEADER
    )
    assert revoked.status_code == 204, revoked.text

    assert client.get("/api/v1/whoami", headers=child_headers).status_code == 401
