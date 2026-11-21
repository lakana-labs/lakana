"""Tests for the AuditEvent model's structural constraints."""

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from lakana_core.auth import SecurableKind
from lakana_core.models import AuditEvent, AuditEventType, Principal, PrincipalType


def _add_principal(session: Session, name: str) -> Principal:
    """Insert and return a bare principal for foreign-key targets.

    Parameters
    ----------
    session : sqlalchemy.orm.Session
        The database session.
    name : str
        The principal's name.

    Returns
    -------
    Principal
        The inserted principal.
    """
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(principal)
    session.commit()
    return principal


def test_a_well_formed_audit_event_persists(session: Session) -> None:
    """An event with every field set commits cleanly."""
    admin = _add_principal(session, "admin")
    event = AuditEvent(
        id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        principal_id=admin.id,
        event_type=AuditEventType.GRANT_CREATED,
        securable_kind=SecurableKind.ENVIRONMENT,
        securable_environment="dev",
        securable_namespace=None,
        securable_table=None,
        detail={"role": "analyst"},
    )
    session.add(event)
    session.commit()
    assert session.get(AuditEvent, event.id) is not None


def test_principal_id_is_nullable_for_a_failed_authentication(session: Session) -> None:
    """An event with no verified principal (a failed authentication) still persists."""
    event = AuditEvent(
        id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        principal_id=None,
        event_type=AuditEventType.AUTHENTICATION_FAILED,
        securable_kind=None,
        securable_environment=None,
        securable_namespace=None,
        securable_table=None,
        detail={"key_id": "doesnotexist"},
    )
    session.add(event)
    session.commit()
    row = session.get(AuditEvent, event.id)
    assert row is not None
    assert row.principal_id is None


def test_deleting_a_principal_sets_audit_events_principal_id_to_null(session: Session) -> None:
    """An audit trail outlives its author, matching QueryRun's precedent."""
    admin = _add_principal(session, "admin")
    event = AuditEvent(
        id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        principal_id=admin.id,
        event_type=AuditEventType.AUTHENTICATION_SUCCEEDED,
        securable_kind=None,
        securable_environment=None,
        securable_namespace=None,
        securable_table=None,
        detail={"key_id": "abc123"},
    )
    session.add(event)
    session.commit()

    session.delete(admin)
    session.commit()
    session.refresh(event)
    assert event.principal_id is None


def test_detail_defaults_to_an_empty_dict(session: Session) -> None:
    """A row that omits detail entirely gets {}, not None -- the column's own default."""
    event = AuditEvent(
        id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        principal_id=None,
        event_type=AuditEventType.AUTHORIZATION_DENIED,
        securable_kind=SecurableKind.TABLE,
        securable_environment="dev",
        securable_namespace="gold",
        securable_table="customers",
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    assert event.detail == {}


def test_oidc_link_events_are_known(session: Session) -> None:
    """Linking and unlinking a provider identity are audit event types of their own (#32)."""
    principal = _add_principal(session, "alice")
    for kind in ("PRINCIPAL_OIDC_LINKED", "PRINCIPAL_OIDC_UNLINKED"):
        session.add(
            AuditEvent(
                id=uuid.uuid4(),
                occurred_at=datetime.now(UTC),
                principal_id=principal.id,
                event_type=AuditEventType(kind),
                detail={"iss": "https://idp.example.com", "sub": "1"},
            )
        )
    session.commit()
