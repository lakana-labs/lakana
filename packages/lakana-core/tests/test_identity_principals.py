"""Tests for PrincipalService."""

import uuid
from dataclasses import replace
from typing import Any, cast

import pytest
from core_test_helpers import _RecordThenDeny
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import Principal, PrincipalType


def _admin(session: Session) -> PrincipalRef:
    """Build an acting principal for tests that don't care who it is, persisted.

    `PrincipalService.create_principal`/`deactivate_principal` now record an
    `AuditEvent` in the same transaction, and
    `AuditEvent.principal_id` is a foreign key onto `principals.id` — the
    acting principal must exist as a real row, not just a bare
    `PrincipalRef`, or that insert fails its foreign key. `Grant.granted_by`
    has the same requirement for `GrantService` calls.

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


def test_create_principal_persists_and_returns_it(session: Session) -> None:
    """A created principal is active by default and reloadable by name."""
    service = PrincipalService(session, AllowAllAuthorization())
    created = service.create_principal(
        _admin(session), name="ci-bot", type=PrincipalType.SERVICE_ACCOUNT
    )
    assert created.is_active is True

    fetched = service.get_principal_by_name(_admin(session), "ci-bot")
    assert fetched.id == created.id


def test_create_principal_rejects_a_duplicate_name(session: Session) -> None:
    """The unique name constraint is enforced at the service layer, not just the DB."""
    service = PrincipalService(session, AllowAllAuthorization())
    service.create_principal(_admin(session), name="ci-bot", type=PrincipalType.SERVICE_ACCOUNT)
    with pytest.raises(AlreadyExistsError):
        service.create_principal(_admin(session), name="ci-bot", type=PrincipalType.SERVICE_ACCOUNT)


def test_get_principal_by_name_raises_when_missing(session: Session) -> None:
    """Looking up a name nobody created is a NotFoundError, not None."""
    service = PrincipalService(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError):
        service.get_principal_by_name(_admin(session), "nobody")


def test_deactivate_principal_sets_is_active_false(session: Session) -> None:
    """Deactivation flips the flag without deleting the row."""
    service = PrincipalService(session, AllowAllAuthorization())
    service.create_principal(_admin(session), name="ci-bot", type=PrincipalType.SERVICE_ACCOUNT)

    service.deactivate_principal(_admin(session), "ci-bot")

    fetched = service.get_principal_by_name(_admin(session), "ci-bot")
    assert fetched.is_active is False


def test_list_principals_returns_every_principal(session: Session) -> None:
    """Unfiltered at M3a, matching Constraint 9's carry-forward for list endpoints."""
    admin = _admin(session)
    service = PrincipalService(session, AllowAllAuthorization())
    service.create_principal(admin, name="ci-bot", type=PrincipalType.SERVICE_ACCOUNT)
    service.create_principal(admin, name="alice", type=PrincipalType.USER)
    names = {p.name for p in service.list_principals(admin).items}
    assert names == {admin.name, "ci-bot", "alice"}


def test_create_principal_authorizes_manage_principal_on_the_instance(session: Session) -> None:
    """The call site exists and checks the right action, even though nothing enforces it yet."""
    denier = _RecordThenDeny()
    service = PrincipalService(session, denier)
    with pytest.raises(Exception):  # ruff: ignore[assert-raises-exception], PermissionDenied re-raised by the stub
        service.create_principal(_admin(session), name="ci-bot", type=PrincipalType.SERVICE_ACCOUNT)
    assert denier.seen == (Action.MANAGE_PRINCIPAL, Securable.instance_scope())


def test_deactivating_the_last_admin_is_refused(session: Session) -> None:
    """Deactivating the sole active instance admin is a ConflictError."""
    admin = _admin(session)
    principals = PrincipalService(session, AllowAllAuthorization())
    grants = GrantService(session, AllowAllAuthorization())
    principals.create_principal(admin, name="root", type=PrincipalType.SERVICE_ACCOUNT)
    grants.grant(admin, principal="root", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    with pytest.raises(ConflictError):
        principals.deactivate_principal(admin, "root")


def test_deactivating_a_non_admin_is_unaffected_by_the_lockout_guard(session: Session) -> None:
    """The lockout guard only cares about admins; deactivating anyone else is unaffected."""
    admin = _admin(session)
    principals = PrincipalService(session, AllowAllAuthorization())
    principals.create_principal(admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT)

    principals.deactivate_principal(admin, "alice")  # does not raise

    assert principals.get_principal_by_name(admin, "alice").is_active is False


def test_a_person_sets_and_resets_their_own_language(session: Session) -> None:
    """The tag is stored canonically, `None` removes it and a non-tag is refused."""
    me = _admin(session)
    service = PrincipalService(session, AllowAllAuthorization())
    assert service.set_own_language(me, "FR-ch") == "fr-CH"
    assert session.get_one(Principal, me.id).language == "fr-CH"
    assert service.set_own_language(me, None) is None
    assert session.get_one(Principal, me.id).language is None
    with pytest.raises(
        ValidationError, match="language: `French` is not a language tag Lakana takes"
    ):
        service.set_own_language(me, "French")


def test_a_task_or_ci_token_may_not_set_a_language(session: Session) -> None:
    """A task-scoped caller is refused with the token to use instead."""
    me = _admin(session)
    service = PrincipalService(session, AllowAllAuthorization())
    for scoped in (
        replace(me, task_scope=cast("Any", object())),
        replace(me, ci_scope=cast("Any", object())),
    ):
        with pytest.raises(
            PermissionDenied, match="a task or CI token cannot change its principal's profile"
        ):
            service.set_own_language(scoped, "fr")
    assert session.get_one(Principal, me.id).language is None


def test_a_principal_that_is_gone_cannot_set_a_language(session: Session) -> None:
    """A caller whose row no longer exists is told so."""
    ghost = PrincipalRef(id=uuid.uuid4(), name="ghost", type=PrincipalType.USER)
    with pytest.raises(NotFoundError, match="Your principal no longer exists"):
        PrincipalService(session, AllowAllAuthorization()).set_own_language(ghost, "fr")
