"""Tests for PatService."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import lakana_core.identity.pats as pats_module
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.errors import AuthenticationError, ConflictError, NotFoundError
from lakana_core.identity.pats import PatService
from lakana_core.identity.tokens import GeneratedToken, hash_secret, parse_token
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    PersonalAccessToken,
    Principal,
    PrincipalType,
)


def _admin(session: Session) -> PrincipalRef:
    """Build an acting principal for tests that don't care who it is, persisted.

    `PatService.issue`/`revoke`/`list` now record an `AuditEvent` in the same
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
    session.commit()
    return ref


def _add_principal(session: Session, name: str, *, is_active: bool = True) -> Principal:
    """Insert a principal directly.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name.
    is_active
        Whether the principal starts active.

    Returns
    -------
    Principal
        The inserted row.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name, is_active=is_active)
    session.add(principal)
    session.commit()
    return principal


def test_issue_returns_a_token_verify_accepts(session: Session) -> None:
    """The exact value `issue` returns is what `verify` accepts."""
    principal = _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())

    issued = service.issue(_admin(session), "alice", name="laptop")

    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    _, secret = parsed
    resolved = service.verify(issued.key_id, secret)
    assert resolved.id == principal.id
    assert resolved.name == "alice"


def test_issue_raises_for_an_unknown_principal(session: Session) -> None:
    """Issuing for a name nobody created is a NotFoundError."""
    service = PatService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        service.issue(_admin(session), "nobody", name="laptop")


def test_verify_rejects_an_unknown_key_id(session: Session) -> None:
    """A key_id that was never issued fails verification."""
    service = PatService(session, AllowAllAuthorization())
    with pytest.raises(AuthenticationError):
        service.verify("nonexistent", "whatever")


def test_verify_rejects_the_wrong_secret(session: Session) -> None:
    """A correct key_id with the wrong secret fails verification."""
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    issued = service.issue(_admin(session), "alice", name="laptop")
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    with pytest.raises(AuthenticationError):
        service.verify(issued.key_id, "wrong-secret")


def test_verify_updates_last_used_at(session: Session) -> None:
    """A successful verification records when it happened."""
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    issued = service.issue(_admin(session), "alice", name="laptop")
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    _, secret = parsed

    service.verify(issued.key_id, secret)

    [pat] = service.list(_admin(session), "alice")
    assert pat.last_used_at is not None


def test_verify_rejects_an_expired_pat(session: Session) -> None:
    """A PAT past its expiry fails verification even with the right secret."""
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    issued = service.issue(
        _admin(session), "alice", name="laptop", expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    _, secret = parsed
    with pytest.raises(AuthenticationError):
        service.verify(issued.key_id, secret)


def test_verify_rejects_a_revoked_pat(session: Session) -> None:
    """A revoked PAT fails verification even with the right secret."""
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    issued = service.issue(_admin(session), "alice", name="laptop")
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    _, secret = parsed

    service.revoke(_admin(session), "alice", issued.key_id)

    with pytest.raises(AuthenticationError):
        service.verify(issued.key_id, secret)


def test_verify_rejects_a_pat_for_a_deactivated_principal(session: Session) -> None:
    """A valid, unexpired, unrevoked PAT still fails once its principal is deactivated."""
    principal = _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    issued = service.issue(_admin(session), "alice", name="laptop")
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    _, secret = parsed

    principal.is_active = False
    session.commit()

    with pytest.raises(AuthenticationError):
        service.verify(issued.key_id, secret)


def test_list_never_exposes_a_secret_or_its_hash(session: Session) -> None:
    """Listing surfaces metadata only — the type itself has no secret field to leak."""
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    service.issue(_admin(session), "alice", name="laptop")
    [pat] = service.list(_admin(session), "alice")
    assert not hasattr(pat, "secret")
    assert pat.name == "laptop"


def test_revoke_raises_for_an_unknown_key_id(session: Session) -> None:
    """Revoking a key_id that was never issued for this principal is a NotFoundError."""
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        service.revoke(_admin(session), "alice", "nonexistent")


def test_issue_with_a_fixed_key_id_collision_raises_conflict_without_retrying(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-supplied (key_id, secret) that collides is a ConflictError, not retried.

    The caller asked for this specific credential — silently substituting a
    different, randomly generated one would not honor that request. This is
    also what makes the bootstrap-startup-race scenario (two replicas racing
    to insert the same deterministic bootstrap key_id/secret) fail loudly and
    distinguishably instead of masquerading as `AuthenticationError`.
    """
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    service.issue(_admin(session), "alice", name="laptop", key_id="unique_key_1", secret="secret1")

    def _must_not_be_called(prefix: str) -> GeneratedToken:
        raise AssertionError("a fixed key_id collision must not retry with a random one")

    monkeypatch.setattr(pats_module, "generate_token", _must_not_be_called)

    with pytest.raises(ConflictError):
        service.issue(
            _admin(session), "alice", name="laptop2", key_id="unique_key_1", secret="secret2"
        )


def test_issue_retries_once_on_a_random_key_id_collision(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A randomly generated key_id colliding with an existing row is retried once.

    The first `generate_token` call is forced to return a key_id that
    already exists; the retry call returns a fresh one. `issue` must
    succeed, using the *second* (retried) key_id/secret, not the first.
    """
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    service.issue(_admin(session), "alice", name="existing", key_id="taken", secret="s3cret1")

    colliding = GeneratedToken(
        key_id="taken",
        secret="whatever",
        secret_hash=hash_secret("whatever"),
        full_token="lakana_pat_taken_whatever",
    )
    fresh = GeneratedToken(
        key_id="fresh-key",
        secret="fresh-secret",
        secret_hash=hash_secret("fresh-secret"),
        full_token="lakana_pat_fresh-key_fresh-secret",
    )
    responses = iter([colliding, fresh])
    monkeypatch.setattr(pats_module, "generate_token", lambda prefix: next(responses))

    issued = service.issue(_admin(session), "alice", name="retried")

    assert issued.key_id == "fresh-key"
    assert issued.token == "lakana_pat_fresh-key_fresh-secret"


def test_issue_raises_conflict_when_the_retry_also_collides(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the retried random key_id also collides, this is a ConflictError, not a 401."""
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    service.issue(_admin(session), "alice", name="existing", key_id="taken", secret="s3cret1")

    colliding = GeneratedToken(
        key_id="taken",
        secret="whatever",
        secret_hash=hash_secret("whatever"),
        full_token="lakana_pat_taken_whatever",
    )
    monkeypatch.setattr(pats_module, "generate_token", lambda prefix: colliding)

    with pytest.raises(ConflictError):
        service.issue(_admin(session), "alice", name="retried-twice")


def test_issue_with_commit_false_leaves_nothing_after_a_rollback(session: Session) -> None:
    """`commit=False` only adds the row and audit event; a rollback removes both.

    Proves `issue(..., commit=False)` never commits on
    its own — the row and its `PAT_ISSUED` event are only ever pending,
    exactly as `lakana_core.bootstrap.bootstrap_if_needed` (the only caller
    that passes `commit=False`) relies on to fold them into its own,
    larger single-commit transaction.
    """
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())

    service.issue(_admin(session), "alice", name="laptop", commit=False)
    session.rollback()

    assert session.execute(select(PersonalAccessToken)).scalars().all() == []
    assert (
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PAT_ISSUED)
        )
        .scalars()
        .all()
        == []
    )


def _unpersisted_actor() -> PrincipalRef:
    """Build an acting principal with no row, so the audit insert fails its foreign key.

    Returns
    -------
    PrincipalRef
        A reference to a principal that does not exist.
    """
    return PrincipalRef(id=uuid.uuid4(), name="ghost", type=PrincipalType.USER)


def test_issue_surfaces_a_non_key_id_integrity_error_unchanged(session: Session) -> None:
    """Only a key_id collision is retried; any other IntegrityError propagates as is."""
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())

    with pytest.raises(IntegrityError):
        service.issue(_unpersisted_actor(), "alice", name="laptop")


def test_issue_with_a_fixed_key_id_surfaces_a_non_key_id_integrity_error(
    session: Session,
) -> None:
    """A fixed-credential issue doesn't report an unrelated IntegrityError as a key_id clash."""
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())

    with pytest.raises(IntegrityError):
        service.issue(_unpersisted_actor(), "alice", name="laptop", key_id="k1", secret="s1")


def test_verify_carries_the_principals_language(session: Session) -> None:
    """`verify` reads the principal's profile language into the `PrincipalRef`."""
    principal = _add_principal(session, "alice")
    principal.language = "fr"
    session.commit()
    service = PatService(session, AllowAllAuthorization())
    issued = service.issue(_admin(session), "alice", name="laptop")
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    assert service.verify(issued.key_id, parsed[1]).language == "fr"
