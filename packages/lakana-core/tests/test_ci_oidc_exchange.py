"""Exchanging a CI ID token for a Lakana token, and authenticating with that token."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from ci_oidc_test_helpers import ISSUER
from ci_trust_test_helpers import (
    NOW,
    SHA,
    CiWorld,
    add_rule,
    authenticate,
    ci_world,
    claims,
    config,
    exchange,
    principal,
    rules,
    scope_of,
    sign,
    verifier,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.catalog.service import CatalogService
from lakana_core.ci_oidc.errors import RefusalCode
from lakana_core.ci_oidc.exchange import (
    CiExchangeRefused,
    CiTokenService,
    ExchangeRefusal,
    jti_digest,
)
from lakana_core.errors import AuthenticationError, ValidationError
from lakana_core.identity.tokens import parse_token
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    CiOidcJti,
    CiToken,
    CiTrustRule,
    Principal,
)
from lakana_core.storage.resolver import StorageResolver


@pytest.fixture
def world(session: Session, tmp_path: Path) -> CiWorld:
    """Build the CI world.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    CiWorld
        The world.
    """
    return ci_world(session, tmp_path)


def _refusal(session: Session, **kwargs: object) -> CiExchangeRefused:
    """Exchange and return the refusal.

    Parameters
    ----------
    session
        Database session.
    **kwargs
        Passed to `exchange`.

    Returns
    -------
    CiExchangeRefused
        The refusal.
    """
    with pytest.raises(CiExchangeRefused) as caught:
        exchange(session, **kwargs)  # ty: ignore[invalid-argument-type]
    return caught.value


def test_a_matching_rule_issues_a_bound_one_hour_token_and_audits_it(
    session: Session, world: CiWorld
) -> None:
    rule_id = add_rule(session, world)
    payload = claims()
    issued = exchange(session, payload=payload)
    assert issued.token.startswith("lakana_ci_")
    assert (issued.principal, issued.environment, issued.repository) == (
        "deployer",
        "prod",
        "analytics",
    )
    assert issued.kinds == ["deploy", "dry_run"]
    assert issued.sha == SHA
    assert issued.rule_id == rule_id
    assert issued.expires_at == NOW + timedelta(hours=1)
    row = session.execute(select(CiToken)).scalar_one()
    assert issued.token not in row.secret_hash
    assert row.rule_id == rule_id and row.rule_revision == 1 and row.used_at is None
    digest = jti_digest(ISSUER, str(payload["jti"]))
    assert row.jti_sha256 == digest
    (event,) = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.OIDC_TOKEN_EXCHANGED)
    ).scalars()
    assert event.principal_id == world.deployer.id
    assert event.detail == {
        "rule_id": str(rule_id),
        "principal": "deployer",
        "environment": "prod",
        "repository": "analytics",
        "sha": SHA,
        "kinds": ["deploy", "dry_run"],
        "jti_sha256": digest,
        "key_id": row.key_id,
        "expires_at": issued.expires_at.isoformat(),
    }
    assert issued.token not in str(event.detail)


def test_the_ttl_comes_from_the_settings_and_is_capped_at_six_hours(
    session: Session, world: CiWorld
) -> None:
    add_rule(session, world)
    issued = CiTokenService(session, ttl_seconds=600).exchange(
        verifier(), sign(), environment="prod", repository="analytics", now=NOW
    )
    assert issued.expires_at == NOW + timedelta(minutes=10)
    with pytest.raises(ValueError, match="21600"):
        config(token_ttl_seconds=21601)


def test_the_token_authenticates_as_the_service_account_with_its_scope(
    session: Session, world: CiWorld
) -> None:
    rule_id = add_rule(session, world)
    issued = exchange(session)
    session.execute(
        update(Principal).where(Principal.id == world.deployer.id).values(language="de")
    )
    session.commit()
    p = authenticate(session, issued)
    assert (p.id, p.name, p.language) == (world.deployer.id, "deployer", "de")
    scope = scope_of(p)
    assert (scope.environment, scope.repository, scope.sha, scope.rule_id) == (
        "prod",
        "analytics",
        SHA,
        rule_id,
    )
    assert scope.kinds == frozenset({"deploy", "dry_run"})
    assert not scope.used
    assert p.task_scope is None


def test_a_wrong_secret_or_unknown_key_is_refused(session: Session, world: CiWorld) -> None:
    add_rule(session, world)
    issued = exchange(session)
    parsed = parse_token(issued.token, "lakana_ci")
    assert parsed is not None
    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        CiTokenService(session).verify(parsed[0], parsed[1] + "x", now=NOW)
    with pytest.raises(AuthenticationError, match="Invalid credentials"):
        CiTokenService(session).verify("0000000000000000", parsed[1], now=NOW)


def test_the_token_expires(session: Session, world: CiWorld) -> None:
    add_rule(session, world)
    issued = exchange(session)
    authenticate(session, issued, NOW + timedelta(minutes=59))
    with pytest.raises(AuthenticationError, match="expired"):
        authenticate(session, issued, NOW + timedelta(hours=1))


def test_the_token_dies_when_its_rule_is_disabled_changed_or_deleted(
    session: Session, world: CiWorld
) -> None:
    rule_id = add_rule(session, world)
    service = rules(session)
    issued = exchange(session)
    service.disable(world.admin, rule_id)
    with pytest.raises(AuthenticationError, match="no longer valid"):
        authenticate(session, issued)
    service.enable(world.admin, rule_id)
    # Re-enabling does not bring back a token issued before: the revision moved on.
    with pytest.raises(AuthenticationError, match="no longer valid"):
        authenticate(session, issued)
    second = exchange(session)
    authenticate(session, second)
    service.update(world.admin, rule_id, kinds=["deploy"])
    with pytest.raises(AuthenticationError, match="no longer valid"):
        authenticate(session, second)
    third = exchange(session)
    service.delete(world.admin, rule_id)
    with pytest.raises(AuthenticationError, match="no longer valid"):
        authenticate(session, third)


def test_the_token_dies_when_its_principal_is_deactivated(session: Session, world: CiWorld) -> None:
    add_rule(session, world)
    issued = exchange(session)
    row = session.get(Principal, world.deployer.id)
    assert row is not None
    row.is_active = False
    session.commit()
    with pytest.raises(AuthenticationError, match="no longer valid"):
        authenticate(session, issued)


def test_the_token_dies_with_its_environment(
    session: Session, world: CiWorld, tmp_path: Path
) -> None:
    add_rule(session, world)
    issued = exchange(session)
    from lakana_core.auth import AllowAllAuthorization

    CatalogService(session, StorageResolver(session), AllowAllAuthorization()).delete_environment(
        world.admin, "prod"
    )
    with pytest.raises(AuthenticationError, match="no longer valid"):
        authenticate(session, issued)


def test_a_used_token_still_authenticates_and_says_so(session: Session, world: CiWorld) -> None:
    add_rule(session, world)
    issued = exchange(session)
    session.execute(update(CiToken).values(used_at=NOW))
    session.commit()
    assert scope_of(authenticate(session, issued)).used


def test_a_replayed_id_token_is_refused_once_exchanged(session: Session, world: CiWorld) -> None:
    add_rule(session, world)
    payload = claims()
    exchange(session, payload=payload)
    refused = _refusal(session, payload=payload)
    assert refused.code == ExchangeRefusal.REPLAYED
    assert refused.authenticated is False
    assert "exchanged before" in refused.message
    assert refused.issuer == ISSUER
    assert session.execute(select(CiToken)).scalars().all().__len__() == 1


def test_a_refused_exchange_still_uses_up_the_id_token(session: Session, world: CiWorld) -> None:
    add_rule(session, world)
    payload = claims()
    assert _refusal(session, payload=payload, repository="other").code == ExchangeRefusal.NO_RULE
    assert _refusal(session, payload=payload).code == ExchangeRefusal.REPLAYED


def test_used_ids_are_kept_until_the_id_token_expires_then_swept(
    session: Session, world: CiWorld
) -> None:
    add_rule(session, world)
    stale = CiOidcJti(jti_sha256="0" * 64, expires_at=NOW - timedelta(minutes=2))
    fresh = CiOidcJti(jti_sha256="1" * 64, expires_at=NOW - timedelta(seconds=30))
    session.add_all([stale, fresh])
    session.commit()
    exchange(session)
    kept = set(session.execute(select(CiOidcJti.jti_sha256)).scalars())
    assert "0" * 64 not in kept
    assert "1" * 64 in kept
    assert len(kept) == 2


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"repository": "other"}, ExchangeRefusal.NO_RULE),
        ({"environment": "dev"}, ExchangeRefusal.NO_RULE),
        ({"environment": "nope"}, ExchangeRefusal.NO_RULE),
        ({"repository": "nope"}, ExchangeRefusal.NO_RULE),
        ({"payload": claims(ref="refs/heads/feature")}, ExchangeRefusal.NO_RULE),
        ({"payload": claims(repository_id="654321")}, ExchangeRefusal.NO_RULE),
        ({"payload": claims(repository_id=123456)}, ExchangeRefusal.NO_RULE),
        ({"payload": claims(ref=None)}, ExchangeRefusal.NO_RULE),
        ({"payload": claims(sha=None)}, ExchangeRefusal.COMMIT_MISSING),
        ({"payload": claims(sha="ABC")}, ExchangeRefusal.COMMIT_MISSING),
        ({"payload": claims(sha=SHA.upper())}, ExchangeRefusal.COMMIT_MISSING),
        ({"payload": claims(sha=12)}, ExchangeRefusal.COMMIT_MISSING),
    ],
)
def test_no_rule_wrong_target_or_claims_are_refused_with_one_answer(
    session: Session, world: CiWorld, kwargs: dict[str, object], code: ExchangeRefusal
) -> None:
    add_rule(session, world)
    refused = _refusal(session, **kwargs)
    assert refused.code == code
    assert refused.authenticated is True
    assert refused.message == _refusal_text(kwargs)
    assert session.execute(select(CiToken)).first() is None


def _refusal_text(kwargs: dict[str, object]) -> str:
    """Return the one text every refusal after verification carries.

    Parameters
    ----------
    kwargs
        The exchange's arguments.

    Returns
    -------
    str
        The message.
    """
    environment = kwargs.get("environment", "prod")
    repository = kwargs.get("repository", "analytics")
    return (
        f"No enabled CI trust rule accepts this ID token for environment {environment!r} and "
        f"repository {repository!r}. An administrator finds the reason in the audit log "
        "(lakana audit list --event-type OIDC_EXCHANGE_REFUSED) and can test the token's claims "
        "with lakana ci-trust explain."
    )


def test_a_dry_run_rule_needs_no_commit(session: Session, world: CiWorld) -> None:
    add_rule(session, world, kinds=("dry_run",))
    issued = exchange(session, payload=claims(sha=None))
    assert (issued.kinds, issued.sha) == (["dry_run"], None)


def test_two_matching_rules_refuse_as_ambiguous(session: Session, world: CiWorld) -> None:
    first = add_rule(session, world)
    second = add_rule(session, world, conditions={"repository_id": "123456", "environment": "prod"})
    refused = _refusal(session)
    assert refused.code == ExchangeRefusal.AMBIGUOUS
    assert refused.message == _refusal_text({})
    assert refused.detail["rule_ids"] == sorted([str(first), str(second)])
    rules(session).disable(world.admin, second)
    exchange(session)


def test_a_disabled_rule_does_not_match(session: Session, world: CiWorld) -> None:
    rule_id = add_rule(session, world)
    rules(session).disable(world.admin, rule_id)
    assert _refusal(session).code == ExchangeRefusal.NO_RULE


def test_an_inactive_principal_is_refused(session: Session, world: CiWorld) -> None:
    add_rule(session, world)
    row = session.get(Principal, world.deployer.id)
    assert row is not None
    row.is_active = False
    session.commit()
    refused = _refusal(session)
    assert refused.code == ExchangeRefusal.PRINCIPAL_INACTIVE
    assert refused.message == _refusal_text({})


def test_a_rule_whose_principal_stopped_being_a_service_account_is_refused(
    session: Session, world: CiWorld
) -> None:
    rule_id = add_rule(session, world)
    user = principal(session, "someone")
    session.execute(update(CiTrustRule).values(principal_id=user.id))
    row = session.get(Principal, user.id)
    assert row is not None
    from lakana_core.models import PrincipalType

    row.type = PrincipalType.USER
    session.commit()
    assert rule_id
    assert _refusal(session).code == ExchangeRefusal.PRINCIPAL_INACTIVE


@pytest.mark.parametrize(
    ("token", "code", "issuer"),
    [
        ("not-a-jwt", RefusalCode.MALFORMED, None),
        ("a.b.c", RefusalCode.MALFORMED, None),
        (sign(claims(aud="https://elsewhere.example")), RefusalCode.AUDIENCE_MISMATCH, ISSUER),
        (sign(claims(iss="https://evil.example")), RefusalCode.ISSUER_NOT_ALLOWED, None),
        (sign(claims(iss=None)), RefusalCode.ISSUER_NOT_ALLOWED, None),
    ],
    ids=["not-a-jwt", "three-segments", "audience", "issuer", "no-issuer"],
)
def test_verifier_refusals_keep_their_code_and_are_unauthenticated(
    session: Session, world: CiWorld, token: str, code: RefusalCode, issuer: str | None
) -> None:
    add_rule(session, world)
    with pytest.raises(CiExchangeRefused) as caught:
        CiTokenService(session).exchange(
            verifier(), token, environment="prod", repository="analytics", now=NOW
        )
    assert caught.value.code == code
    assert caught.value.authenticated is False
    assert caught.value.issuer == issuer
    assert token not in caught.value.message
    assert session.execute(select(CiOidcJti)).first() is None


def test_a_bad_signature_names_the_configured_issuer(session: Session, world: CiWorld) -> None:
    token = sign()
    head, body, _ = token.split(".")
    with pytest.raises(CiExchangeRefused) as caught:
        CiTokenService(session).exchange(
            verifier(), f"{head}.{body}.AAAA", environment="prod", repository="analytics", now=NOW
        )
    assert caught.value.code == RefusalCode.BAD_SIGNATURE
    assert caught.value.issuer == ISSUER


def test_an_exchange_with_ci_oidc_off_is_refused(session: Session, world: CiWorld) -> None:
    from lakana_core.ci_oidc.config import CiOidcConfig
    from lakana_core.ci_oidc.verification import IdTokenVerifier

    with pytest.raises(CiExchangeRefused) as caught:
        CiTokenService(session).exchange(
            IdTokenVerifier(CiOidcConfig()), sign(), environment="prod", repository="analytics"
        )
    assert caught.value.code == RefusalCode.DISABLED


def test_environment_and_repository_must_be_short_texts(session: Session, world: CiWorld) -> None:
    for kwargs in ({"environment": "e" * 256}, {"repository": ""}):
        with pytest.raises(ValidationError):
            CiTokenService(session).exchange(
                verifier(),
                sign(),
                **{"environment": "prod", "repository": "analytics", **kwargs},
                now=NOW,
            )


def test_jti_digest_separates_issuer_and_jti() -> None:
    assert jti_digest("a", "bc") != jti_digest("ab", "c")
    assert jti_digest("a", "b") == hashlib.sha256(b"a\nb").hexdigest()


def test_an_expired_token_has_no_scope(session: Session, world: CiWorld) -> None:
    from lakana_core.ci_oidc.exchange import resolve_ci_scope

    add_rule(session, world)
    exchange(session)
    token = session.execute(select(CiToken)).scalar_one()
    later = NOW + timedelta(hours=2)
    assert resolve_ci_scope(session, token, now=later, require_unexpired=True) is None
    assert resolve_ci_scope(session, token, now=later, require_unexpired=False) is not None


def test_a_key_id_collision_is_retried_once(
    session: Session, world: CiWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_core.ci_oidc import exchange as module
    from lakana_core.errors import ConflictError
    from lakana_core.identity.tokens import generate_token

    add_rule(session, world)
    first = exchange(session)
    fixed = generate_token("lakana_ci")
    taken = type(fixed)(
        key_id=first.key_id,
        secret=fixed.secret,
        secret_hash=fixed.secret_hash,
        full_token=f"lakana_ci_{first.key_id}_{fixed.secret}",
    )
    answers = iter([taken, fixed])
    monkeypatch.setattr(module, "generate_token", lambda prefix: next(answers))
    second = exchange(session)
    assert second.key_id == fixed.key_id
    monkeypatch.setattr(module, "generate_token", lambda prefix: taken)
    with pytest.raises(ConflictError, match="duplicate key id"):
        exchange(session)


def test_a_rule_without_a_trigger_condition_never_matches(session: Session, world: CiWorld) -> None:
    add_rule(session, world)
    session.execute(
        update(CiTrustRule).values(conditions={"repository_id": "123456", "ref": "refs/heads/main"})
    )
    session.commit()
    assert _refusal(session).code == ExchangeRefusal.NO_RULE
