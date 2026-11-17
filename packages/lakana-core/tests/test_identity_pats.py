"""Tests for PatService."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import lakana_core.identity.pats as pats_module
from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    CiScope,
    PrincipalRef,
    SecurableKind,
    SessionContext,
    TaskScope,
)
from lakana_core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    StepUpRequired,
)
from lakana_core.grants.admin import admin_only_message, may_manage_tokens
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.identity.pats import OWN_TOKEN_FROM_BROWSER, PatService
from lakana_core.identity.tokens import GeneratedToken, hash_secret, parse_token
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Grant,
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


def test_verify_carries_the_token_id_and_credential(session: Session) -> None:
    """A verified PAT names itself on the principal it resolves to."""
    _add_principal(session, "alice")
    service = PatService(session, AllowAllAuthorization())
    issued = service.issue(_admin(session), "alice", name="laptop")
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    row = session.execute(
        select(PersonalAccessToken).where(PersonalAccessToken.key_id == issued.key_id)
    ).scalar_one()
    p = service.verify(*parsed)
    assert p.token_id == row.id
    assert p.credential == "pat"
    assert p.session is None


def test_only_bootstrap_if_needed_marks_a_pat_as_bootstrap(session: Session) -> None:
    """`issue` marks nothing by default, not even a PAT named like the bootstrap one."""
    _add_principal(session, "admin")
    service = PatService(session, AllowAllAuthorization())
    actor = _admin(session)
    plain = service.issue(actor, "admin", name="laptop")
    named = service.issue(actor, "admin", name="bootstrap")
    marked = service.issue(actor, "admin", name="x", bootstrap=True)
    flags = dict(
        session.execute(select(PersonalAccessToken.key_id, PersonalAccessToken.bootstrap)).all()
    )
    assert flags == {plain.key_id: False, named.key_id: False, marked.key_id: True}


def _auditing(session: Session) -> AuditingAuthorization:
    """Build the production pairing: grants, wrapped to audit denials.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    AuditingAuthorization
        The wrapper around `GrantAuthorization`.
    """
    return AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(10_000))


def _person(session: Session, name: str, *, admin: bool = False) -> PrincipalRef:
    """Persist an active user, with the `ADMIN` role on the instance when asked.

    Parameters
    ----------
    session
        Database session.
    name
        The user's name.
    admin
        Whether the user holds `ADMIN` at instance scope.

    Returns
    -------
    PrincipalRef
        A personal-token reference to the user.
    """
    row = _add_principal(session, name)
    ref = PrincipalRef(id=row.id, name=name, type=row.type, credential="pat")
    if admin:
        session.add(
            Grant(
                id=uuid.uuid4(),
                principal_id=row.id,
                role=Role.ADMIN,
                kind=SecurableKind.INSTANCE,
                granted_by=row.id,
                granted_at=datetime.now(UTC),
            )
        )
        session.commit()
    return ref


def _in_browser(ref: PrincipalRef, *, touched_minutes_ago: int | None) -> PrincipalRef:
    """Give a reference a browser session whose last passkey touch is some minutes old.

    Parameters
    ----------
    ref
        The person.
    touched_minutes_ago
        Age of the last touch at `verified_at`; `None` for no touch.

    Returns
    -------
    PrincipalRef
        The reference with a `SessionContext`.
    """
    now = datetime.now(UTC)
    touched = None if touched_minutes_ago is None else now - timedelta(minutes=touched_minutes_ago)
    context = SessionContext(
        session_id=uuid.uuid4(),
        key_id="abc",
        via="passkey",
        created_at=now,
        last_touch_at=touched,
        source_pat_id=None,
        verified_at=now,
    )
    return replace(ref, credential="session", session=context)


def _task_ref(ref: PrincipalRef) -> PrincipalRef:
    """Make a task-token reference for a person.

    Parameters
    ----------
    ref
        The person.

    Returns
    -------
    PrincipalRef
        The same person, authenticated with a task token.
    """
    scope = TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4())
    return replace(ref, credential="task", task_scope=scope)


def _ci_ref(ref: PrincipalRef) -> PrincipalRef:
    """Make a CI-token reference for a person.

    Parameters
    ----------
    ref
        The person.

    Returns
    -------
    PrincipalRef
        The same person, authenticated with a CI token.
    """
    scope = CiScope(
        token_id=uuid.uuid4(),
        rule_id=uuid.uuid4(),
        environment="dev",
        environment_id=uuid.uuid4(),
        repository="sales",
        repository_id=uuid.uuid4(),
        kinds=frozenset(),
        sha=None,
    )
    return replace(ref, credential="ci", ci_scope=scope)


def _denials(session: Session) -> list[dict[str, object]]:
    """Read the details of the recorded denials, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of dict
        One detail dictionary per `AUTHORIZATION_DENIED` row.
    """
    rows = session.execute(
        select(AuditEvent)
        .where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
        .order_by(AuditEvent.occurred_at)
    ).scalars()
    return [row.detail for row in rows]


def _tokens_of(session: Session, ref: PrincipalRef) -> list[PersonalAccessToken]:
    """Read every token row of a person.

    Parameters
    ----------
    session
        Database session.
    ref
        The person.

    Returns
    -------
    list of PersonalAccessToken
        Their rows.
    """
    return list(
        session.execute(
            select(PersonalAccessToken).where(PersonalAccessToken.principal_id == ref.id)
        ).scalars()
    )


def test_a_token_cannot_create_its_owners_token(session: Session) -> None:
    """A stolen token of a non-administrator cannot mint a replacement."""
    bob = _person(session, "bob")
    service = PatService(session, _auditing(session))
    with pytest.raises(PermissionDenied) as refused:
        service.issue(bob, "bob", name="mine")
    assert str(refused.value) == OWN_TOKEN_FROM_BROWSER
    assert _denials(session) == [{"operation": "issue_token", "reason": "not_from_a_browser"}]
    assert _tokens_of(session, bob) == []
    with pytest.raises(PermissionDenied):
        service.issue(_task_ref(bob), "bob", name="mine")
    with pytest.raises(PermissionDenied):
        service.issue(_ci_ref(bob), "bob", name="mine")
    assert _tokens_of(session, bob) == []


def test_a_person_creates_their_own_token_from_a_fresh_session_only(session: Session) -> None:
    """A browser session with a stale touch is asked to step up; a fresh one creates the token."""
    bob = _person(session, "bob")
    service = PatService(session, _auditing(session))
    with pytest.raises(StepUpRequired):
        service.issue(_in_browser(bob, touched_minutes_ago=16), "bob", name="laptop")
    assert _tokens_of(session, bob) == []
    issued = service.issue(_in_browser(bob, touched_minutes_ago=1), "bob", name="laptop")
    [row] = _tokens_of(session, bob)
    assert row.principal_id == bob.id
    assert row.key_id == issued.key_id
    [event] = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PAT_ISSUED)
    ).scalars()
    assert event.detail["issuer"] == "bob"
    assert event.detail["subject"] == "bob"


def test_a_person_lists_and_revokes_their_own_tokens_with_a_pat_or_a_session(
    session: Session,
) -> None:
    """Listing and revoking one's own tokens need no step-up and work with either credential."""
    bob = _person(session, "bob")
    service = PatService(session, _auditing(session))
    first = service.issue(_in_browser(bob, touched_minutes_ago=0), "bob", name="one")
    second = service.issue(_in_browser(bob, touched_minutes_ago=0), "bob", name="two")
    stale = _in_browser(bob, touched_minutes_ago=60)
    for ref in (bob, stale):
        assert {t.key_id for t in service.list(ref, "bob")} == {first.key_id, second.key_id}
    service.revoke(bob, "bob", first.key_id)
    service.revoke(stale, "bob", second.key_id)
    assert all(t.revoked_at is not None for t in _tokens_of(session, bob))


def test_task_and_ci_tokens_may_not_list_or_revoke_their_owners_tokens(session: Session) -> None:
    """A task or CI token is not a personal credential."""
    bob = _person(session, "bob")
    service = PatService(session, _auditing(session))
    issued = service.issue(_in_browser(bob, touched_minutes_ago=0), "bob", name="one")
    for ref in (_task_ref(bob), _ci_ref(bob)):
        with pytest.raises(PermissionDenied):
            service.list(ref, "bob")
        with pytest.raises(PermissionDenied):
            service.revoke(ref, "bob", issued.key_id)


@pytest.mark.parametrize("holds_the_action", [False, True])
def test_someone_elses_tokens_are_403_whether_or_not_they_exist(
    session: Session, holds_the_action: bool
) -> None:
    """The refusal comes before any lookup, so it never says whether a principal exists.

    With a single-action grant of `MANAGE_TOKEN` the refusal is `admin_only_message`; without
    any grant it is the authorization's own. Either way it is one text for every method and
    for a person who exists and one who does not.
    """
    bob = _person(session, "bob")
    ada = _person(session, "ada", admin=True)
    if holds_the_action:
        session.add(
            Grant(
                id=uuid.uuid4(),
                principal_id=bob.id,
                action=Action.MANAGE_TOKEN,
                kind=SecurableKind.INSTANCE,
                granted_by=ada.id,
                granted_at=datetime.now(UTC),
            )
        )
        session.commit()
    service = PatService(session, _auditing(session))
    messages: set[str] = set()
    for subject in ("ada", "nobody"):
        for call in (
            lambda s=subject: service.list(bob, s),
            lambda s=subject: service.revoke(bob, s, "key"),
            lambda s=subject: service.issue(bob, s, name="x"),
        ):
            with pytest.raises(PermissionDenied) as refused:
                call()
            messages.add(str(refused.value))
    assert len(messages) == 1
    assert not any(name in text for text in messages for name in ("ada", "nobody"))
    if holds_the_action:
        assert messages == {admin_only_message(Action.MANAGE_TOKEN)}


def test_an_administrator_still_issues_with_a_token(session: Session) -> None:
    """An administrator keeps today's rule, which the CLI and the bootstrap rely on."""
    ada = _person(session, "ada", admin=True)
    _person(session, "bob")
    service = PatService(session, _auditing(session))
    service.issue(ada, "bob", name="for-bob")
    service.issue(ada, "ada", name="for-me")
    assert len(_tokens_of(session, ada)) == 1


def test_may_manage_tokens(session: Session) -> None:
    """Only an administrator's personal credential, or the bootstrap, may manage tokens."""
    ada = _person(session, "ada", admin=True)
    bob = _person(session, "bob")
    authz = _auditing(session)
    assert may_manage_tokens(session, authz, ada)
    assert may_manage_tokens(session, AllowAllAuthorization(), bob)
    assert not may_manage_tokens(session, authz, bob)
    assert not may_manage_tokens(session, authz, _task_ref(ada))
    assert _denials(session) == []


def test_a_self_made_token_stops_when_its_owner_is_deactivated(session: Session) -> None:
    """A token a person made for themselves ends with their account."""
    bob = _person(session, "bob")
    service = PatService(session, _auditing(session))
    issued = service.issue(_in_browser(bob, touched_minutes_ago=1), "bob", name="laptop")
    parsed = parse_token(issued.token, "lakana_pat")
    assert parsed is not None
    session.execute(select(Principal).where(Principal.name == "bob")).scalar_one().is_active = False
    session.commit()
    with pytest.raises(AuthenticationError):
        service.verify(*parsed)
