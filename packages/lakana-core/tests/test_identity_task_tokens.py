"""Tests for TaskTokenService."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import lakana_core.identity.task_tokens as task_tokens_module
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind, TaskScope
from lakana_core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.roles import Role
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.identity.tokens import GeneratedToken, hash_secret, parse_token
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Grant,
    Principal,
    PrincipalType,
    TaskToken,
)


def _admin(session: Session) -> PrincipalRef:
    """Build an acting principal for tests that don't care who it is, persisted.

    `TaskTokenService.mint`/`revoke` now record an `AuditEvent` in the same
    transaction, and `AuditEvent.principal_id` is a
    foreign key onto `principals.id` — the acting principal must exist as a
    real row, not just a bare `PrincipalRef`, or that insert fails its
    foreign key.

    Parameters
    ----------
    session
        Database session to persist the row into.

    Returns
    -------
    PrincipalRef
        A user principal, freshly persisted, independent of any fixture
        instance.
    """
    ref = PrincipalRef(
        id=uuid.uuid4(), name=f"admin-{uuid.uuid4().hex[:8]}", type=PrincipalType.USER
    )
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.flush()
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=ref.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=ref.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    return ref


def _add_principal(session: Session, name: str) -> Principal:
    """Insert a principal directly.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name.

    Returns
    -------
    Principal
        The inserted row.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(principal)
    session.commit()
    return principal


def test_mint_then_verify_resolves_the_full_scope(session: Session) -> None:
    """Verify returns the principal, environment, and asset_scope the token was minted with."""
    principal = _add_principal(session, "worker-1")
    principal.language = "fr"
    session.commit()
    service = TaskTokenService(session, AllowAllAuthorization())

    minted = service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=["gold.sales"], ttl_seconds=300
    )
    parsed = parse_token(minted.token, "lakana_task")
    assert parsed is not None
    _, secret = parsed

    scope = service.verify(minted.key_id, secret)
    assert (scope.principal.id, scope.principal.language) == (principal.id, "fr")
    assert scope.environment == "dev"
    assert scope.asset_scope == ["gold.sales"]
    task_token = session.execute(
        select(TaskToken).where(TaskToken.key_id == minted.key_id)
    ).scalar_one()
    assert scope.principal.task_scope == TaskScope(
        environment="dev", asset_scope=["gold.sales"], token_id=task_token.id
    )


def test_mint_raises_for_an_unknown_principal(session: Session) -> None:
    """Minting for a name nobody created is a NotFoundError."""
    service = TaskTokenService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        service.mint(_admin(session), "nobody", environment="dev", asset_scope=[], ttl_seconds=60)


def test_mint_sets_expires_at_from_ttl(session: Session) -> None:
    """expires_at is issued_at + ttl_seconds, not left open-ended."""
    _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization())
    before = datetime.now(UTC)

    minted = service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=[], ttl_seconds=300
    )

    after = datetime.now(UTC)
    assert before + timedelta(seconds=300) <= minted.expires_at <= after + timedelta(seconds=300)


def test_verify_rejects_an_expired_task_token(session: Session) -> None:
    """A task token past its ttl fails verification even with the right secret."""
    _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization())
    minted = service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=[], ttl_seconds=-1
    )
    parsed = parse_token(minted.token, "lakana_task")
    assert parsed is not None
    _, secret = parsed
    with pytest.raises(AuthenticationError):
        service.verify(minted.key_id, secret)


def test_verify_rejects_the_wrong_secret(session: Session) -> None:
    """A correct key_id with the wrong secret fails verification."""
    _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization())
    minted = service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=[], ttl_seconds=300
    )
    with pytest.raises(AuthenticationError):
        service.verify(minted.key_id, "wrong-secret")


def test_verify_rejects_an_unknown_key_id(session: Session) -> None:
    """A key_id that was never minted fails verification."""
    service = TaskTokenService(session, AllowAllAuthorization())
    with pytest.raises(AuthenticationError):
        service.verify("nonexistent", "whatever")


def test_verify_rejects_a_revoked_task_token(session: Session) -> None:
    """A revoked task token fails verification even before its natural expiry."""
    _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization())
    minted = service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=[], ttl_seconds=300
    )
    parsed = parse_token(minted.token, "lakana_task")
    assert parsed is not None
    _, secret = parsed

    service.revoke(_admin(session), minted.key_id)

    with pytest.raises(AuthenticationError):
        service.verify(minted.key_id, secret)


def test_revoke_raises_for_an_unknown_key_id(session: Session) -> None:
    """Revoking a key_id that was never minted is a NotFoundError."""
    service = TaskTokenService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        service.revoke(_admin(session), "nonexistent")


def test_mint_retries_once_on_a_random_key_id_collision(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A minted key_id colliding with an existing task token is retried once.

    The first `generate_token` call is forced to return a key_id that
    already exists; the retry call returns a fresh one. `mint` must
    succeed, using the *second* (retried) key_id/secret.
    """
    _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization())
    existing = service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=[], ttl_seconds=300
    )

    colliding = GeneratedToken(
        key_id=existing.key_id,
        secret="whatever",
        secret_hash=hash_secret("whatever"),
        full_token=f"lakana_task_{existing.key_id}_whatever",
    )
    fresh = GeneratedToken(
        key_id="fresh-key",
        secret="fresh-secret",
        secret_hash=hash_secret("fresh-secret"),
        full_token="lakana_task_fresh-key_fresh-secret",
    )
    responses = iter([colliding, fresh])
    monkeypatch.setattr(task_tokens_module, "generate_token", lambda prefix: next(responses))

    minted = service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=[], ttl_seconds=300
    )

    assert minted.key_id == "fresh-key"
    assert minted.token == "lakana_task_fresh-key_fresh-secret"


def test_mint_raises_conflict_when_the_retry_also_collides(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the retried random key_id also collides, this is a ConflictError, not a 401."""
    _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization())
    existing = service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=[], ttl_seconds=300
    )

    colliding = GeneratedToken(
        key_id=existing.key_id,
        secret="whatever",
        secret_hash=hash_secret("whatever"),
        full_token=f"lakana_task_{existing.key_id}_whatever",
    )
    monkeypatch.setattr(task_tokens_module, "generate_token", lambda prefix: colliding)

    with pytest.raises(ConflictError):
        service.mint(
            _admin(session), "worker-1", environment="dev", asset_scope=[], ttl_seconds=300
        )


@pytest.mark.parametrize("pattern", ["*", ".*", "", " ", "*.x", "a..b", "bronze.", ".a", "*.*"])
def test_mint_rejects_wildcard_only_patterns(session: Session, pattern: str) -> None:
    """An asset-scope pattern with no non-wildcard namespace component is rejected."""
    _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization())
    with pytest.raises(ValidationError, match=r"Invalid asset-scope pattern"):
        service.mint(
            _admin(session),
            "worker-1",
            environment="dev",
            asset_scope=[pattern],
            ttl_seconds=300,
        )


def test_mint_rejects_ttl_over_max(session: Session) -> None:
    """Ttl_seconds above the configured max_ttl_seconds is rejected."""
    _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization(), max_ttl_seconds=3600)
    with pytest.raises(ValidationError, match=r"ttl_seconds must not exceed 3600"):
        service.mint(
            _admin(session),
            "worker-1",
            environment="dev",
            asset_scope=["bronze.*"],
            ttl_seconds=3601,
        )


def test_mint_accepts_ttl_at_max(session: Session) -> None:
    """ttl_seconds exactly equal to max_ttl_seconds is accepted, not off-by-one rejected."""
    _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization(), max_ttl_seconds=3600)
    service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=["bronze.*"], ttl_seconds=3600
    )


def test_verify_rejects_an_inactive_principal(session: Session) -> None:
    """A task token whose principal has been deactivated fails verification."""
    principal = _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization())
    minted = service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=[], ttl_seconds=300
    )
    parsed = parse_token(minted.token, "lakana_task")
    assert parsed is not None
    _, secret = parsed

    principal.is_active = False
    session.commit()

    with pytest.raises(AuthenticationError):
        service.verify(minted.key_id, secret)


def test_mint_surfaces_a_non_key_id_integrity_error_unchanged(session: Session) -> None:
    """Only a key_id collision is retried; any other IntegrityError propagates as is.

    The acting principal has no row (a different row carries its name, so the
    mint-for-itself rule passes), so the audit event's foreign key fails.
    """
    _add_principal(session, "ghost")
    service = TaskTokenService(session, AllowAllAuthorization())
    ghost = PrincipalRef(id=uuid.uuid4(), name="ghost", type=PrincipalType.USER)

    with pytest.raises(IntegrityError):
        service.mint(ghost, "ghost", environment="dev", asset_scope=[], ttl_seconds=300)


def _holder(session: Session) -> PrincipalRef:
    """Persist a principal with no grant at all (the service is given allow-all authorization).

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The principal.
    """
    ref = PrincipalRef(
        id=uuid.uuid4(), name=f"holder-{uuid.uuid4().hex[:8]}", type=PrincipalType.USER
    )
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    return ref


def test_a_holder_of_mint_task_token_may_mint_for_itself(session: Session) -> None:
    holder = _holder(session)
    service = TaskTokenService(session, AllowAllAuthorization())
    minted = service.mint(
        holder, holder.name, environment="dev", asset_scope=["gold.sales"], ttl_seconds=60
    )
    row = session.execute(select(TaskToken).where(TaskToken.key_id == minted.key_id)).scalar_one()
    assert row.principal_id == holder.id
    assert (row.task_run_id, row.claim) == (None, None)


@pytest.mark.parametrize("subject", ["worker-1", "nobody"])
def test_a_non_admin_cannot_mint_for_another_principal(session: Session, subject: str) -> None:
    """An unknown name gets the same refusal: no probing of principal names."""
    _add_principal(session, "worker-1")
    holder = _holder(session)
    service = TaskTokenService(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied, match="may mint task tokens only for itself"):
        service.mint(holder, subject, environment="dev", asset_scope=["gold.sales"], ttl_seconds=60)
    assert session.execute(select(TaskToken)).scalars().all() == []
    event = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
    ).scalar_one()
    assert event.principal_id == holder.id
    assert event.detail == {"action": "MINT_TASK_TOKEN", "subject": subject, "reason": "not_self"}


def test_an_instance_admin_may_mint_for_another_principal(session: Session) -> None:
    worker = _add_principal(session, "worker-1")
    service = TaskTokenService(session, AllowAllAuthorization())
    minted = service.mint(
        _admin(session), "worker-1", environment="dev", asset_scope=["gold.sales"], ttl_seconds=60
    )
    row = session.execute(select(TaskToken).where(TaskToken.key_id == minted.key_id)).scalar_one()
    assert row.principal_id == worker.id


def test_a_non_admin_revokes_only_its_own_rest_minted_token(session: Session) -> None:
    """A key id that is another's, bound to a task or unknown gets one refusal, audited."""
    _add_principal(session, "worker-1")
    holder = _holder(session)
    service = TaskTokenService(session, AllowAllAuthorization())
    admin = _admin(session)
    own = service.mint(
        holder, holder.name, environment="dev", asset_scope=["gold.x"], ttl_seconds=60
    )
    others = service.mint(
        admin, "worker-1", environment="dev", asset_scope=["gold.x"], ttl_seconds=60
    )

    service.revoke(holder, own.key_id)
    assert session.execute(
        select(TaskToken.revoked_at).where(TaskToken.key_id == own.key_id)
    ).scalar_one()
    for key_id in (others.key_id, "nonexistent"):
        with pytest.raises(PermissionDenied, match="may revoke task tokens only for itself"):
            service.revoke(holder, key_id)
    assert (
        session.execute(
            select(TaskToken.revoked_at).where(TaskToken.key_id == others.key_id)
        ).scalar_one()
        is None
    )
    events = (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
        )
        .scalars()
        .all()
    )
    assert [e.detail["key_id"] for e in events] == [others.key_id, "nonexistent"]
    assert all(e.principal_id == holder.id for e in events)
    with pytest.raises(NotFoundError):
        service.revoke(admin, "nonexistent")
