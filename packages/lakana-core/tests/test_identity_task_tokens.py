"""Tests for TaskTokenService."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog, python_task_run
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import lakana_core.identity.task_tokens as task_tokens_module
from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    CiScope,
    PrincipalRef,
    SecurableKind,
    TaskScope,
)
from lakana_core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath
from lakana_core.identity.pats import PatService
from lakana_core.identity.task_tokens import ListedTaskToken, TaskTokenService
from lakana_core.identity.tokens import GeneratedToken, hash_secret, parse_token
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Grant,
    PersonalAccessToken,
    Principal,
    PrincipalType,
    Run,
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


def _listing_service(session: Session) -> TaskTokenService:
    """Build the service as production does: grants, wrapped to audit denials.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    TaskTokenService
        The service whose authorization is the grants' own.
    """
    authz = AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(10_000))
    return TaskTokenService(session, authz)


def _administrator(session: Session) -> PrincipalRef:
    """Persist a user holding the `ADMIN` role on the instance.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The administrator, authenticated with a personal token.
    """
    ref = _admin(session)
    return replace(ref, credential="pat")


def _stamped(
    session: Session, key_id: str, *, minutes_ago: int, **changes: datetime | uuid.UUID | int
) -> None:
    """Move a token's issue time into the past and set other columns.

    Parameters
    ----------
    session
        Database session.
    key_id
        The token's key id.
    minutes_ago
        How long ago it was issued.
    **changes
        Other columns to set.
    """
    row = session.execute(select(TaskToken).where(TaskToken.key_id == key_id)).scalar_one()
    row.issued_at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    for column, value in changes.items():
        setattr(row, column, value)
    session.commit()


def test_list_live_pages_unrevoked_unexpired_tokens_newest_first(
    session: Session, tmp_path: Path
) -> None:
    """A REST token and a bound one are live; a revoked and an expired one are not."""
    admin = _administrator(session)
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[NamespacePath.from_parts(["bronze"])],
        tables=[],
    )
    plain = _add_principal(session, "worker-1")
    minting = TaskTokenService(session, AllowAllAuthorization())
    rest = minting.mint(
        admin, "worker-1", environment="dev", asset_scope=["gold.a"], ttl_seconds=3600
    )
    revoked = minting.mint(
        admin, "worker-1", environment="dev", asset_scope=["gold.b"], ttl_seconds=3600
    )
    expired = minting.mint(
        admin, "worker-1", environment="dev", asset_scope=["gold.c"], ttl_seconds=3600
    )
    task = python_task_run(session, admin.id, asset="gold.scores", claim=2)
    run = session.get(Run, task.run_id)
    assert run is not None
    run.job = "nightly"
    session.commit()
    bound = minting.mint_for_task(task.id, 2, ttl_seconds=3600)
    _stamped(session, rest.key_id, minutes_ago=30)
    _stamped(session, revoked.key_id, minutes_ago=20, revoked_at=datetime.now(UTC))
    _stamped(
        session,
        expired.key_id,
        minutes_ago=10,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    _stamped(session, bound.key_id, minutes_ago=5)
    service = _listing_service(session)

    first = service.list_live(admin, limit=1)
    assert first.next_cursor is not None
    second = service.list_live(admin, limit=1, cursor=first.next_cursor)

    listed = [*first.items, *second.items]
    assert second.next_cursor is None
    assert all(isinstance(item, ListedTaskToken) for item in listed)
    assert [item.token.key_id for item in listed] == [bound.key_id, rest.key_id]
    newer, older = listed
    assert (newer.principal, newer.run_id, newer.job, newer.asset) == (
        admin.name,
        run.id,
        "nightly",
        "gold.scores",
    )
    assert newer.token.asset_scope == ["bronze.stations", "gold.scores", "silver.weather"]
    assert older.token.asset_scope == ["gold.a"]
    assert (older.principal, older.run_id, older.job, older.asset) == (plain.name, None, None, None)


def test_list_live_filters_by_environment_and_an_unknown_one_is_empty(session: Session) -> None:
    """`environment` keeps one environment's tokens; a name no token has is an empty page."""
    admin = _administrator(session)
    _add_principal(session, "worker-1")
    minting = TaskTokenService(session, AllowAllAuthorization())
    dev = minting.mint(admin, "worker-1", environment="dev", asset_scope=[], ttl_seconds=60)
    minting.mint(admin, "worker-1", environment="prod", asset_scope=[], ttl_seconds=60)
    service = _listing_service(session)

    assert [i.token.key_id for i in service.list_live(admin, environment="dev").items] == [
        dev.key_id
    ]
    empty = service.list_live(admin, environment="nowhere")
    assert (empty.items, empty.next_cursor) == ([], None)


def test_list_live_is_for_an_administrator_only(session: Session) -> None:
    """`MINT_TASK_TOKEN` without `ADMIN`, and an administrator's task or CI token, are refused."""
    admin = _administrator(session)
    holder = _holder(session)
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=holder.id,
            action=Action.MINT_TASK_TOKEN,
            kind=SecurableKind.INSTANCE,
            granted_by=admin.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    task = replace(
        admin,
        credential="task",
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4()),
    )
    ci = replace(
        admin,
        credential="ci",
        ci_scope=CiScope(
            token_id=uuid.uuid4(),
            rule_id=uuid.uuid4(),
            environment="dev",
            environment_id=uuid.uuid4(),
            repository="sales",
            repository_id=uuid.uuid4(),
            kinds=frozenset(),
            sha=None,
        ),
    )
    service = _listing_service(session)
    for ref in (holder, task, ci):
        with pytest.raises(PermissionDenied):
            service.list_live(ref)


def _signed_in_with_pat(
    session: Session, principal: PrincipalRef, *, expires_at: datetime | None = None
) -> tuple[PrincipalRef, PersonalAccessToken]:
    """Issue a personal token for a principal and authenticate with it.

    Parameters
    ----------
    session
        Database session.
    principal
        The token's owner.
    expires_at
        When the token stops working; `None` for never.

    Returns
    -------
    tuple of PrincipalRef and PersonalAccessToken
        The principal as `PatService.verify` returns it (``credential="pat"`` and the token's
        id), and the token's row.
    """
    secret = uuid.uuid4().hex
    pat = PersonalAccessToken(
        id=uuid.uuid4(),
        principal_id=principal.id,
        key_id=uuid.uuid4().hex[:16],
        secret_hash=hash_secret(secret),
        name="laptop",
        expires_at=expires_at,
    )
    session.add(pat)
    session.commit()
    return PatService(session, AllowAllAuthorization()).verify(pat.key_id, secret), pat


def _verify(service: TaskTokenService, token: str) -> None:
    """Verify a full task token's value.

    Parameters
    ----------
    service
        The task-token service.
    token
        The full ``lakana_task_...`` value.

    Raises
    ------
    AuthenticationError
        If the token no longer authenticates.
    """
    parsed = parse_token(token, "lakana_task")
    assert parsed is not None
    service.verify(*parsed)


def test_mint_records_the_personal_token_that_minted_it(session: Session) -> None:
    """A token minted with a PAT names that PAT; one minted without a PAT names none."""
    admin = _admin(session)
    signed_in, pat = _signed_in_with_pat(session, admin)
    service = TaskTokenService(session, AllowAllAuthorization())

    with_pat = service.mint(
        signed_in, admin.name, environment="dev", asset_scope=[], ttl_seconds=60
    )
    without = service.mint(admin, admin.name, environment="dev", asset_scope=[], ttl_seconds=60)

    rows = {row.key_id: row.source_pat_id for row in session.execute(select(TaskToken)).scalars()}
    assert rows == {with_pat.key_id: pat.id, without.key_id: None}


def test_revoking_the_personal_token_ends_its_task_tokens_only(session: Session) -> None:
    """Revoking a PAT revokes its task tokens, audited; another PAT's task token still works."""
    admin = _admin(session)
    first, first_pat = _signed_in_with_pat(session, admin)
    second, _ = _signed_in_with_pat(session, admin)
    service = TaskTokenService(session, AllowAllAuthorization())
    child = service.mint(first, admin.name, environment="dev", asset_scope=[], ttl_seconds=60)
    other = service.mint(second, admin.name, environment="dev", asset_scope=[], ttl_seconds=60)

    PatService(session, AllowAllAuthorization()).revoke(admin, admin.name, first_pat.key_id)

    with pytest.raises(AuthenticationError):
        _verify(service, child.token)
    _verify(service, other.token)
    revoked = session.execute(
        select(TaskToken).where(TaskToken.key_id == child.key_id)
    ).scalar_one()
    assert revoked.revoked_at is not None
    events = [
        e.detail
        for e in session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TASK_TOKEN_REVOKED)
        ).scalars()
    ]
    assert events == [
        {
            "actor": admin.name,
            "subject": admin.name,
            "key_id": child.key_id,
            "environment": "dev",
            "asset_scope": [],
            "reason": "pat_revoked",
            "pat_key_id": first_pat.key_id,
        }
    ]


def test_a_task_token_stops_when_its_personal_token_expires(session: Session) -> None:
    """An expired PAT's task token fails at once, and is no longer listed as live."""
    admin = _administrator(session)
    signed_in, pat = _signed_in_with_pat(
        session, admin, expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    service = _listing_service(session)
    child = service.mint(signed_in, admin.name, environment="dev", asset_scope=[], ttl_seconds=60)
    _verify(service, child.token)

    pat.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    with pytest.raises(AuthenticationError):
        _verify(service, child.token)
    assert child.key_id not in {i.token.key_id for i in service.list_live(admin).items}


def test_a_task_token_stops_when_its_personal_tokens_owner_is_deactivated(
    session: Session,
) -> None:
    """An administrator's PAT mints for someone else; deactivating the administrator ends it."""
    admin = _admin(session)
    _add_principal(session, "worker-1")
    signed_in, _ = _signed_in_with_pat(session, admin)
    service = TaskTokenService(session, AllowAllAuthorization())
    child = service.mint(signed_in, "worker-1", environment="dev", asset_scope=[], ttl_seconds=60)
    _verify(service, child.token)

    session.get_one(Principal, admin.id).is_active = False
    session.commit()

    with pytest.raises(AuthenticationError):
        _verify(service, child.token)


def test_deleting_the_personal_token_deletes_its_task_tokens(session: Session) -> None:
    """The foreign key cascades: a task token never outlives the row of its PAT."""
    admin = _admin(session)
    signed_in, pat = _signed_in_with_pat(session, admin)
    service = TaskTokenService(session, AllowAllAuthorization())
    child = service.mint(signed_in, admin.name, environment="dev", asset_scope=[], ttl_seconds=60)

    session.delete(pat)
    session.commit()

    with pytest.raises(AuthenticationError):
        _verify(service, child.token)
    assert (
        session.execute(select(TaskToken).where(TaskToken.key_id == child.key_id)).first() is None
    )


def test_a_task_token_without_a_personal_token_works_until_its_own_expiry(
    session: Session,
) -> None:
    """A row minted before the link existed (`source_pat_id` NULL) is judged by itself alone."""
    admin = _admin(session)
    service = TaskTokenService(session, AllowAllAuthorization())
    child = service.mint(admin, admin.name, environment="dev", asset_scope=[], ttl_seconds=60)
    _verify(service, child.token)

    _stamped(
        session,
        child.key_id,
        minutes_ago=5,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    with pytest.raises(AuthenticationError):
        _verify(service, child.token)


def _service_account(session: Session, granted_by: PrincipalRef) -> PrincipalRef:
    """Persist a service account holding `MINT_TASK_TOKEN` on the instance, and nothing else.

    Parameters
    ----------
    session
        Database session.
    granted_by
        The administrator recorded as granting it.

    Returns
    -------
    PrincipalRef
        The service account.
    """
    ref = PrincipalRef(
        id=uuid.uuid4(), name=f"svc-{uuid.uuid4().hex[:8]}", type=PrincipalType.SERVICE_ACCOUNT
    )
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.flush()
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=ref.id,
            action=Action.MINT_TASK_TOKEN,
            kind=SecurableKind.INSTANCE,
            granted_by=granted_by.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    return ref


def test_a_service_accounts_task_token_ends_with_its_personal_token(session: Session) -> None:
    """A service account's PAT is recorded at mint, and revoking it gives the task token 401."""
    admin = _admin(session)
    account = _service_account(session, admin)
    signed_in, pat = _signed_in_with_pat(session, account)
    assert signed_in.type == PrincipalType.SERVICE_ACCOUNT
    service = _listing_service(session)
    child = service.mint(signed_in, account.name, environment="dev", asset_scope=[], ttl_seconds=60)
    row = session.execute(select(TaskToken).where(TaskToken.key_id == child.key_id)).scalar_one()
    assert row.source_pat_id == pat.id
    _verify(service, child.token)

    PatService(session, AllowAllAuthorization()).revoke(admin, account.name, pat.key_id)

    with pytest.raises(AuthenticationError):
        _verify(service, child.token)


def test_a_service_accounts_task_token_stops_when_it_is_deactivated(session: Session) -> None:
    """Deactivating the service account fails the task token its own PAT minted."""
    admin = _admin(session)
    account = _service_account(session, admin)
    signed_in, _ = _signed_in_with_pat(session, account)
    service = _listing_service(session)
    child = service.mint(signed_in, account.name, environment="dev", asset_scope=[], ttl_seconds=60)
    _verify(service, child.token)

    session.get_one(Principal, account.id).is_active = False
    session.commit()

    with pytest.raises(AuthenticationError):
        _verify(service, child.token)


def test_an_administrator_minting_for_a_service_account_ties_the_token_to_its_own_pat(
    session: Session,
) -> None:
    """The token signs in as the service account but lives and dies with the administrator's PAT."""
    admin = _admin(session)
    account = _service_account(session, admin)
    _, account_pat = _signed_in_with_pat(session, account)
    signed_in, admin_pat = _signed_in_with_pat(session, admin)
    service = _listing_service(session)
    child = service.mint(signed_in, account.name, environment="dev", asset_scope=[], ttl_seconds=60)
    row = session.execute(select(TaskToken).where(TaskToken.key_id == child.key_id)).scalar_one()
    assert (row.principal_id, row.source_pat_id) == (account.id, admin_pat.id)

    pats = PatService(session, AllowAllAuthorization())
    pats.revoke(admin, account.name, account_pat.key_id)
    _verify(service, child.token)
    pats.revoke(admin, admin.name, admin_pat.key_id)
    with pytest.raises(AuthenticationError):
        _verify(service, child.token)


def test_a_service_accounts_ci_or_task_token_cannot_mint(session: Session) -> None:
    """Only a PAT mints over REST: the account's CI or task token is refused before any row."""
    admin = _admin(session)
    account = _service_account(session, admin)
    task = replace(
        account,
        credential="task",
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4()),
    )
    ci = replace(
        account,
        credential="ci",
        ci_scope=CiScope(
            token_id=uuid.uuid4(),
            rule_id=uuid.uuid4(),
            environment="dev",
            environment_id=uuid.uuid4(),
            repository="sales",
            repository_id=uuid.uuid4(),
            kinds=frozenset(),
            sha=None,
        ),
    )
    service = _listing_service(session)
    for ref in (task, ci):
        with pytest.raises(PermissionDenied):
            service.mint(ref, account.name, environment="dev", asset_scope=[], ttl_seconds=60)
    assert session.execute(select(TaskToken)).first() is None
