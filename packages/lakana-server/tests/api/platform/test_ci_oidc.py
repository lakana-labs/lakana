"""The CI token exchange over REST, and what the CI token may do there.

Local RSA keys and a fake key fetcher: no test reaches the network.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection, Iterator
from datetime import UTC, datetime
from functools import cache
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.ci_oidc.config import GITHUB_ISSUER, CiOidcConfig
from lakana_core.ci_oidc.jwks import JwksCache
from lakana_core.ci_oidc.verification import IdTokenVerifier
from lakana_core.models import AuditEvent, AuditEventType, CiToken
from lakana_server.app import create_app
from lakana_server.settings import AuthAuditConfig, Settings
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

AUDIENCE = "https://lakana.example.com"
SHA = "0123456789abcdef0123456789abcdef01234567"
_EXCHANGE = "/api/v1/auth/oidc/token"
_RULES = "/api/v1/ci-trust-rules"


@cache
def _key() -> rsa.RSAPrivateKey:
    """Generate the issuer's signing key once.

    Returns
    -------
    rsa.RSAPrivateKey
        The key.
    """
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _fetch(url: str, allowed_hosts: Collection[str]) -> object:
    """Answer the issuer's discovery document and key set, as a fake fetcher.

    Parameters
    ----------
    url
        The URL asked for.
    allowed_hosts
        Ignored.

    Returns
    -------
    object
        The document.
    """
    del allowed_hosts
    if url.endswith("/openid-configuration"):
        return {"issuer": GITHUB_ISSUER, "jwks_uri": f"{GITHUB_ISSUER}/.well-known/jwks"}
    public = json.loads(RSAAlgorithm.to_jwk(_key().public_key()))
    return {"keys": [{**public, "kid": "k1", "alg": "RS256"}]}


def _claims(**changes: object) -> dict[str, object]:
    """Return the claims of a GitHub Actions ID token, now.

    Parameters
    ----------
    **changes
        Claims to set; `None` removes one.

    Returns
    -------
    dict[str, object]
        The claims.
    """
    now = int(datetime.now(UTC).timestamp())
    base: dict[str, object] = {
        "iss": GITHUB_ISSUER,
        "aud": AUDIENCE,
        "sub": "repo:org/repo:environment:prod",
        "jti": str(uuid.uuid4()),
        "iat": now - 10,
        "exp": now + 290,
        "repository_id": "123456",
        "ref": "refs/heads/main",
        "event_name": "push",
        "sha": SHA,
    }
    base.update(changes)
    return {name: value for name, value in base.items() if value is not None}


def _sign(claims: dict[str, object] | None = None) -> str:
    """Sign an ID token with the issuer's key.

    Parameters
    ----------
    claims
        The claims; `_claims()` by default.

    Returns
    -------
    str
        The token.
    """
    return jwt.encode(claims or _claims(), _key(), algorithm="RS256", headers={"kid": "k1"})


def _app_client(settings: Settings) -> Iterator[TestClient]:
    """Run an app whose verifier reads the fake issuer's keys.

    Parameters
    ----------
    settings
        The settings.

    Yields
    ------
    TestClient
        The client.
    """
    with TestClient(create_app(settings)) as client:
        config = settings.ci_oidc
        client.app.state.ci_oidc_verifier = IdTokenVerifier(
            config, JwksCache(config, fetcher=_fetch)
        )
        yield client


@pytest.fixture
def ci_client(settings: Settings) -> Iterator[TestClient]:
    """Provide a client of a server with CI OIDC on for GitHub.

    Parameters
    ----------
    settings
        The base settings.

    Yields
    ------
    TestClient
        The client.
    """
    configured = settings.model_copy(
        update={
            "ci_oidc": CiOidcConfig(issuers=["github"], audience=AUDIENCE),
            "auth_audit": AuthAuditConfig(per_ip_per_minute=3),
        }
    )
    yield from _app_client(configured)


def _setup(client: TestClient) -> str:
    """Create ``prod``, ``dev``, two repositories, the deployer and a rule; return its id.

    Parameters
    ----------
    client
        The client.

    Returns
    -------
    str
        The rule's id.
    """
    create_environment(client, "prod")
    create_environment(client, "dev")
    for name in ("analytics", "other"):
        response = client.post(
            "/api/v1/repositories",
            headers=AUTH_HEADER,
            json={"name": name, "kind": "https", "url": f"https://github.com/org/{name}.git"},
        )
        assert response.status_code == 201, response.text
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": "deployer", "type": "SERVICE_ACCOUNT"},
    )
    for environment in ("prod", "dev"):
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={
                "principal": "deployer",
                "action": "DEPLOY",
                "kind": "ENVIRONMENT",
                "environment": environment,
            },
        )
        assert granted.status_code == 201, granted.text
    created = client.post(
        _RULES,
        headers=AUTH_HEADER,
        json={
            "issuer": "github",
            "principal": "deployer",
            "environment": "prod",
            "repository": "analytics",
            "kinds": ["deploy", "dry_run"],
            "conditions": {
                "repository_id": "123456",
                "ref": "refs/heads/main",
                "event_name": "push",
            },
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _exchange(client: TestClient, token: str | None = None, **body: Any) -> Any:  # ruff: ignore[any-type]
    """Post an exchange request.

    Parameters
    ----------
    client
        The client.
    token
        The ID token; a fresh signed one by default.
    **body
        Fields to change.

    Returns
    -------
    httpx.Response
        The answer.
    """
    payload = {
        "id_token": token or _sign(),
        "environment": "prod",
        "repository": "analytics",
        **body,
    }
    return client.post(_EXCHANGE, json=payload)


def _bearer(token: str) -> dict[str, str]:
    """Build an Authorization header.

    Parameters
    ----------
    token
        The token.

    Returns
    -------
    dict of str to str
        The header.
    """
    return {"Authorization": f"Bearer {token}"}


def _refusals(session: Session) -> list[dict[str, Any]]:
    """Read the details of the recorded refused exchanges, oldest first.

    Parameters
    ----------
    session
        A session on the app's database.

    Returns
    -------
    list of dict
        The details.
    """
    return [
        dict(row.detail)
        for row in session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.OIDC_EXCHANGE_REFUSED)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
    ]


@pytest.fixture
def db(ci_client: TestClient) -> Iterator[Session]:
    """Open a session on the app's database.

    Parameters
    ----------
    ci_client
        The client.

    Yields
    ------
    Session
        The session.
    """
    with Session(ci_client.app.state.engine) as session:
        yield session


def test_one_deploy_of_the_signed_commit_then_only_its_run(
    ci_client: TestClient, db: Session
) -> None:
    rule_id = _setup(ci_client)
    answer = _exchange(ci_client)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["token"].startswith("lakana_ci_")
    assert (body["principal"], body["environment"], body["repository"], body["sha"]) == (
        "deployer",
        "prod",
        "analytics",
        SHA,
    )
    assert (body["kinds"], body["rule_id"]) == (["deploy", "dry_run"], rule_id)
    header = _bearer(body["token"])
    assert ci_client.get("/api/v1/whoami", headers=header).json()["name"] == "deployer"
    refused = ci_client.post(
        "/api/v1/environments/prod/deploys",
        headers=header,
        json={"repository": "analytics", "ref": "main"},
    )
    assert refused.status_code == 403
    assert SHA in refused.json()["detail"]
    accepted = ci_client.post(
        "/api/v1/environments/prod/deploys", headers=header, json={"repository": "analytics"}
    )
    assert accepted.status_code == 202, accepted.text
    deploy_id = accepted.json()["id"]
    again = ci_client.post(
        "/api/v1/environments/prod/deploys",
        headers=header,
        json={"repository": "analytics", "kind": "dry_run"},
    )
    assert again.status_code == 403
    assert "already submitted" in again.json()["detail"]
    shown = ci_client.get(f"/api/v1/environments/prod/deploys/{deploy_id}", headers=header)
    assert shown.status_code == 200
    assert shown.json()["requested_ref"] == SHA
    for method, path in [
        ("GET", "/api/v1/environments/prod/deploys"),
        ("GET", "/api/v1/environments/prod"),
        ("GET", "/api/v1/environments/prod/deployments"),
        ("GET", "/api/v1/environments/prod/runs"),
        ("GET", "/api/v1/environments/dev/deploys"),
        ("DELETE", "/api/v1/environments/prod/deployments/analytics"),
        ("GET", "/api/v1/ci-trust-rules"),
        ("GET", "/api/v1/audit-events"),
        ("GET", "/api/v1/principals/deployer/permissions?kind=INSTANCE"),
        ("POST", "/api/v1/environments/prod/jobs/nightly/pause"),
    ]:
        payload = {"repository": "analytics"} if path.endswith("/pause") else None
        response = ci_client.request(method, path, headers=header, json=payload)
        assert response.status_code == 403, (method, path, response.text)
    other = ci_client.post(
        "/api/v1/environments/prod/deploys",
        headers=AUTH_HEADER,
        json={"repository": "analytics", "kind": "dry_run"},
    )
    hidden = ci_client.get(
        f"/api/v1/environments/prod/deploys/{other.json()['id']}", headers=header
    )
    assert hidden.status_code == 404
    run = db.execute(select(CiToken)).scalar_one()
    assert run.used_at is not None
    (exchanged,) = db.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.OIDC_TOKEN_EXCHANGED)
    ).scalars()
    assert body["token"] not in json.dumps(exchanged.detail)


def test_another_repository_or_environment_is_refused_after_the_exchange(
    ci_client: TestClient,
) -> None:
    _setup(ci_client)
    header = _bearer(_exchange(ci_client).json()["token"])
    for env, repository in (("prod", "other"), ("dev", "analytics")):
        response = ci_client.post(
            f"/api/v1/environments/{env}/deploys", headers=header, json={"repository": repository}
        )
        assert response.status_code == 403, response.text
    rollback = ci_client.post(
        "/api/v1/environments/prod/deploys",
        headers=header,
        json={"repository": "analytics", "kind": "rollback", "to_previous": True},
    )
    assert rollback.status_code == 403


def test_a_replay_is_refused_and_audited_without_the_token(
    ci_client: TestClient, db: Session
) -> None:
    _setup(ci_client)
    token = _sign()
    assert _exchange(ci_client, token).status_code == 200
    replay = _exchange(ci_client, token)
    assert replay.status_code == 401
    assert "exchanged before" in replay.json()["detail"]
    (row,) = _refusals(db)
    assert row["code"] == "replayed" and row["issuer"] == GITHUB_ISSUER
    assert token not in json.dumps(row)
    assert set(row) == {"code", "issuer", "jti_sha256"}


def test_no_rule_is_one_answer_and_the_reason_is_in_the_audit_log(
    ci_client: TestClient, db: Session
) -> None:
    _setup(ci_client)
    unknown = _exchange(ci_client, repository="nope")
    wrong_ref = _exchange(ci_client, _sign(_claims(ref="refs/heads/feature")))
    assert unknown.status_code == wrong_ref.status_code == 403
    assert unknown.json()["detail"].replace("'nope'", "'analytics'") == wrong_ref.json()["detail"]
    assert "lakana ci-trust explain" in unknown.json()["detail"]
    codes = [(row["code"], row["repository"]) for row in _refusals(db)]
    assert codes == [("no_matching_rule", "nope"), ("no_matching_rule", "analytics")]


def test_a_refused_id_token_names_its_code_and_is_rate_limited(
    ci_client: TestClient, db: Session
) -> None:
    _setup(ci_client)
    bad = _sign(_claims(aud="https://elsewhere.example"))
    for _ in range(5):
        response = _exchange(ci_client, bad)
        assert response.status_code == 401
        assert "LAKANA_CI_OIDC__AUDIENCE" in response.json()["detail"]
    rows = _refusals(db)
    assert len(rows) == 3
    assert {row["code"] for row in rows} == {"audience_mismatch"}
    assert "environment" not in rows[0]
    limiter = ci_client.app.state.oidc_exchange_audit_limiter
    (bucket,) = limiter._buckets.values()
    bucket.tokens = 1.0
    _exchange(ci_client, "garbage")
    assert _refusals(db)[-1] == {"code": "malformed", "suppressed": 2}


def test_the_body_is_limited_and_validated_without_echoing_it(ci_client: TestClient) -> None:
    _setup(ci_client)
    large = ci_client.post(_EXCHANGE, content=b"{" + b" " * (16 * 1024 + 1) + b"}")
    assert large.status_code == 413
    assert "LAKANA_REQUEST_BODY__MAX_BYTES" not in large.text

    def chunks() -> Iterator[bytes]:
        """Send a body in pieces, without a declared length.

        Yields
        ------
        bytes
            Pieces of 4 KiB, 20 KiB in all.
        """
        for _ in range(5):
            yield b" " * 4096

    chunked = ci_client.post(_EXCHANGE, content=chunks())
    assert chunked.status_code == 413
    secret = "eyJsecret.value.here"
    for content in (
        b"not json",
        json.dumps({"id_token": secret}).encode(),
        json.dumps({"id_token": 5, "environment": "prod", "repository": "analytics"}).encode(),
        json.dumps(
            {"id_token": secret, "environment": "prod", "repository": "analytics", "x": 1}
        ).encode(),
    ):
        response = ci_client.post(_EXCHANGE, content=content)
        assert response.status_code == 422, response.text
        assert secret not in response.text


def test_the_exchange_needs_no_credential_and_ignores_one(ci_client: TestClient) -> None:
    _setup(ci_client)
    answer = ci_client.post(
        _EXCHANGE,
        headers={"Authorization": "Bearer lakana_pat_nope_nope"},
        json={"id_token": _sign(), "environment": "prod", "repository": "analytics"},
    )
    assert answer.status_code == 200, answer.text


def test_with_ci_oidc_off_the_exchange_says_how_to_turn_it_on(client: TestClient) -> None:
    response = client.post(
        _EXCHANGE, json={"id_token": _sign(), "environment": "prod", "repository": "analytics"}
    )
    assert response.status_code == 401
    assert "LAKANA_CI_OIDC__ISSUERS" in response.json()["detail"]


def test_a_disabled_rule_ends_the_token_at_the_next_request(
    ci_client: TestClient, db: Session
) -> None:
    rule_id = _setup(ci_client)
    header = _bearer(_exchange(ci_client).json()["token"])
    assert ci_client.get("/api/v1/whoami", headers=header).status_code == 200
    assert ci_client.post(f"{_RULES}/{rule_id}/disable", headers=AUTH_HEADER).status_code == 200
    response = ci_client.get("/api/v1/whoami", headers=header)
    assert response.status_code == 401
    assert "no longer valid" in response.json()["detail"]
    failed = db.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHENTICATION_FAILED)
    ).scalars()
    assert [row.detail["key_id"] for row in failed] == [header["Authorization"].split("_")[2]]


def test_an_expired_token_is_refused(ci_client: TestClient, db: Session) -> None:
    _setup(ci_client)
    header = _bearer(_exchange(ci_client).json()["token"])
    db.execute(update(CiToken).values(expires_at=datetime(2026, 1, 1, tzinfo=UTC)))
    db.commit()
    response = ci_client.get("/api/v1/whoami", headers=header)
    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


def test_a_success_with_a_ci_token_is_recorded_once_per_window(
    ci_client: TestClient, db: Session
) -> None:
    _setup(ci_client)
    header = _bearer(_exchange(ci_client).json()["token"])
    for _ in range(3):
        ci_client.get("/api/v1/whoami", headers=header)
    key_id = header["Authorization"].split("_")[2]
    rows = [
        row
        for row in db.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.AUTHENTICATION_SUCCEEDED
            )
        ).scalars()
        if row.detail.get("key_id") == key_id
    ]
    assert len(rows) == 1


def test_whoami_shows_a_ci_tokens_scope_and_whether_it_was_used(ci_client: TestClient) -> None:
    rule_id = _setup(ci_client)
    issued = _exchange(ci_client).json()
    header = _bearer(issued["token"])
    before = ci_client.get("/api/v1/whoami", headers=header).json()
    assert before["task_token"] is None
    assert before["ci_token"] == {
        "rule_id": rule_id,
        "environment": "prod",
        "repository": "analytics",
        "kinds": ["deploy", "dry_run"],
        "sha": SHA,
        "expires_at": before["ci_token"]["expires_at"],
        "used": False,
    }
    expires = datetime.fromisoformat(before["ci_token"]["expires_at"])
    assert expires == datetime.fromisoformat(issued["expires_at"])
    assert issued["token"] not in json.dumps(before)
    accepted = ci_client.post(
        "/api/v1/environments/prod/deploys", headers=header, json={"repository": "analytics"}
    )
    assert accepted.status_code == 202, accepted.text
    assert ci_client.get("/api/v1/whoami", headers=header).json()["ci_token"]["used"] is True


def test_a_ci_token_is_refused_on_the_access_routes(ci_client: TestClient) -> None:
    """R10: a CI token gets 403 on the access export and plan (#46)."""
    _setup(ci_client)
    token = _exchange(ci_client).json()["token"]
    header = {"Authorization": f"Bearer {token}"}
    assert ci_client.get("/api/v1/access/export", headers=header).status_code == 403
    assert (
        ci_client.post("/api/v1/access/plan", headers=header, json={"text": ""}).status_code == 403
    )
