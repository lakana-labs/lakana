"""Tests for AuditService."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditCursor, AuditService
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
    SecurableKind,
)
from lakana_core.errors import PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath
from lakana_core.models import AuditEventType, Principal, PrincipalType


def _add_principal(session: Session, name: str) -> PrincipalRef:
    """Insert a principal and return a PrincipalRef for it.

    Returns
    -------
    PrincipalRef
        A reference to the newly inserted principal.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def test_record_persists_immediately_when_committing(session: Session) -> None:
    """commit=True writes the row right away, on its own."""
    admin = _add_principal(session, "admin")
    service = AuditService(session)

    event = service.record(
        admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, detail={"key_id": "abc"}, commit=True
    )

    assert session.get(type(event), event.id) is not None


def test_record_does_not_commit_by_default(session: Session) -> None:
    """The default (commit=False) only adds to the session; nothing is persisted yet."""
    admin = _add_principal(session, "admin")
    service = AuditService(session)

    event = service.record(
        admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, detail={"key_id": "abc"}
    )

    session.rollback()
    assert session.get(type(event), event.id) is None


def test_record_builds_securable_columns_from_a_securable(session: Session) -> None:
    """A given Securable populates all four securable_* columns."""
    admin = _add_principal(session, "admin")
    service = AuditService(session)
    securable = Securable.table_scope("dev", NamespacePath.from_parts(["gold"]), "customers")

    event = service.record(
        admin.id, AuditEventType.AUTHORIZATION_DENIED, securable=securable, commit=True
    )

    assert event.securable_kind == SecurableKind.TABLE
    assert event.securable_environment == "dev"
    assert event.securable_namespace == "gold"
    assert event.securable_table == "customers"


def test_record_leaves_securable_columns_none_when_no_securable_given(session: Session) -> None:
    """Authentication events have no securable target."""
    service = AuditService(session)

    event = service.record(None, AuditEventType.AUTHENTICATION_FAILED, commit=True)

    assert event.securable_kind is None
    assert event.securable_environment is None


def test_record_detail_defaults_to_empty_dict(session: Session) -> None:
    """No detail given means {}, not None."""
    service = AuditService(session)

    event = service.record(None, AuditEventType.AUTHORIZATION_DENIED, commit=True)

    assert event.detail == {}


def test_query_raises_validation_error_without_an_authz(session: Session) -> None:
    """Query requires an AuthorizationService; record alone does not need one."""
    admin = _add_principal(session, "admin")
    service = AuditService(session)

    with pytest.raises(ValidationError, match="AuthorizationService"):
        service.query(admin)


def test_query_authorizes_read_audit_log_at_instance_scope(session: Session) -> None:
    """Query checks READ_AUDIT_LOG against Securable.instance_scope() before reading."""
    admin = _add_principal(session, "admin")

    class _RecordAll:
        def __init__(self) -> None:
            self.seen: list[tuple[Action, Securable]] = []

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            self.seen.append((action, securable))

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize` would, as a boolean.

            Parameters
            ----------
            principal
                The actor requesting the action.
            action
                The action requested.
            securable
                The object acted upon.

            Returns
            -------
            bool
                `False` if `authorize` raises `PermissionDenied`, else `True`.
            """
            try:
                self.authorize(principal, action, securable)
            except PermissionDenied:
                return False
            return True

    authz = _RecordAll()
    service = AuditService(session, authz)

    service.query(admin)

    assert authz.seen == [(Action.READ_AUDIT_LOG, Securable.instance_scope())]


def test_query_raises_permission_denied_when_refused(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A denying authz stops query before it reads anything.

    A row already exists in `audit_events`, and `session.execute` is spied
    on, so this proves the ordering itself: if a future refactor hoisted
    the `select(AuditEvent)` above `authorize`, this test would catch it by
    seeing a non-empty `executed` list, not just by seeing `PermissionDenied`
    still raised (which an empty database and a stub denying unconditionally
    cannot distinguish from the correct order).
    """
    admin = _add_principal(session, "admin")
    AuditService(session).record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)

    executed: list[object] = []
    original_execute = session.execute

    def _spy_execute(*args: object, **kwargs: object) -> object:
        executed.append(args[0] if args else None)
        return cast(Any, original_execute)(*args, **kwargs)

    monkeypatch.setattr(session, "execute", _spy_execute)

    class _DenyAll:
        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            raise PermissionDenied("no audit rights")

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize` would, as a boolean.

            Parameters
            ----------
            principal
                The actor requesting the action.
            action
                The action requested.
            securable
                The object acted upon.

            Returns
            -------
            bool
                `False` if `authorize` raises `PermissionDenied`, else `True`.
            """
            try:
                self.authorize(principal, action, securable)
            except PermissionDenied:
                return False
            return True

    service = AuditService(session, _DenyAll())

    with pytest.raises(PermissionDenied):
        service.query(admin)

    assert executed == []


def test_query_filters_by_event_type(session: Session) -> None:
    """Only events matching event_type come back."""
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
    service.record(admin.id, AuditEventType.GRANT_CREATED, commit=True)

    results = service.query(admin, event_type=AuditEventType.GRANT_CREATED).items

    assert [e.event_type for e in results] == [AuditEventType.GRANT_CREATED]


def test_query_filters_by_kind_connection(session: Session) -> None:
    """`kind="connection"` returns every CONNECTION_* event and nothing else, newest first."""
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    service.record(admin.id, AuditEventType.CONNECTION_CREATED, commit=True)
    service.record(admin.id, AuditEventType.SECRET_BOUND, commit=True)
    service.record(admin.id, AuditEventType.CONNECTION_UPDATED, commit=True)
    service.record(admin.id, AuditEventType.CONNECTION_DELETED, commit=True)

    results = service.query(admin, kind="connection").items

    assert [e.event_type for e in results] == [
        AuditEventType.CONNECTION_DELETED,
        AuditEventType.CONNECTION_UPDATED,
        AuditEventType.CONNECTION_CREATED,
    ]
    combined = service.query(
        admin, kind="connection", event_type=AuditEventType.CONNECTION_UPDATED
    ).items
    assert [e.event_type for e in combined] == [AuditEventType.CONNECTION_UPDATED]


def test_query_refuses_an_unknown_kind_or_a_type_outside_the_kind(session: Session) -> None:
    """The one kind today is `connection`; an event type of another kind cannot match it."""
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())

    with pytest.raises(ValidationError, match=r"^kind must be connection$"):
        service.query(admin, kind="secret")
    with pytest.raises(ValidationError) as caught:
        service.query(admin, kind="connection", event_type=AuditEventType.SECRET_BOUND)
    assert str(caught.value) == (
        "event_type SECRET_BOUND is not of kind connection; leave out one of the two filters"
    )


def test_query_filters_by_environment(session: Session) -> None:
    """Only events whose securable_environment matches come back."""
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    service.record(
        admin.id,
        AuditEventType.SECRET_CREATED,
        securable=Securable.environment_scope("dev"),
        commit=True,
    )
    service.record(
        admin.id,
        AuditEventType.SECRET_CREATED,
        securable=Securable.environment_scope("prod"),
        commit=True,
    )

    results = service.query(admin, environment="dev").items

    assert [e.securable_environment for e in results] == ["dev"]


def test_query_filters_by_principal_id(session: Session) -> None:
    """Only events recorded for principal_id come back."""
    admin = _add_principal(session, "admin")
    alice = _add_principal(session, "alice")
    service = AuditService(session, AllowAllAuthorization())
    service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
    service.record(alice.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)

    results = service.query(admin, principal_id=alice.id).items

    assert [e.principal_id for e in results] == [alice.id]


def test_query_filters_by_time_range(session: Session) -> None:
    """since/until narrow to events within the range."""
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    old_event = service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
    old_event.occurred_at = datetime.now(UTC) - timedelta(days=10)
    session.commit()
    recent_event = service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)

    results = service.query(admin, since=datetime.now(UTC) - timedelta(days=1)).items

    assert [e.id for e in results] == [recent_event.id]


def test_query_filters_by_until(session: Session) -> None:
    """Until excludes events that occurred after it."""
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    old_event = service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
    old_event.occurred_at = datetime.now(UTC) - timedelta(days=10)
    session.commit()
    service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)

    results = service.query(admin, until=datetime.now(UTC) - timedelta(days=1)).items

    assert [e.id for e in results] == [old_event.id]


def test_query_limit_caps_the_returned_rows(session: Session) -> None:
    """Limit bounds the result count even when more matching rows exist."""
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    for _ in range(5):
        service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)

    results = service.query(admin, limit=3).items

    assert len(results) == 3


def test_query_default_limit_is_one_hundred(session: Session) -> None:
    """Omitting limit still bounds results, at the documented default of 100."""
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    for _ in range(101):
        service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)

    results = service.query(admin).items

    assert len(results) == 100


def test_query_orders_most_recent_first(session: Session) -> None:
    """Results come back newest-occurred-first, the natural order for a review."""
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    first = service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
    first.occurred_at = datetime.now(UTC) - timedelta(minutes=5)
    session.commit()
    second = service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)

    results = service.query(admin).items

    assert [e.id for e in results] == [second.id, first.id]


def test_query_cursor_resumes_strictly_after_the_given_position(session: Session) -> None:
    """Paging with `AuditCursor` visits every row once, including ties on `occurred_at`.

    Three of the five events share one timestamp, so only the `id` tiebreaker
    tells them apart; a cursor comparing `occurred_at` alone would skip or
    repeat them at a page boundary.
    """
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    tied = datetime.now(UTC) - timedelta(minutes=1)
    events = [
        service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
        for _ in range(5)
    ]
    for event in events[:3]:
        event.occurred_at = tied
    session.commit()
    expected = [e.id for e in service.query(admin).items]

    seen: list[uuid.UUID] = []
    cursor: AuditCursor | None = None
    while True:
        page = service.query(admin, limit=2, cursor=cursor).items
        if not page:
            break
        seen.extend(e.id for e in page)
        cursor = AuditCursor(occurred_at=page[-1].occurred_at, id=page[-1].id)

    assert seen == expected
    assert len(seen) == 5


def test_query_oldest_first_pages_every_event_once(session: Session) -> None:
    """`order="asc"` reverses the order, ties on `occurred_at` included, and pages by its cursor."""
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    tied = datetime.now(UTC) - timedelta(minutes=1)
    events = [
        service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
        for _ in range(5)
    ]
    for event in events[:3]:
        event.occurred_at = tied
    session.commit()
    newest_first = [e.id for e in service.query(admin).items]

    seen: list[uuid.UUID] = []
    cursor: AuditCursor | None = None
    while True:
        page = service.query(admin, limit=2, cursor=cursor, sort="occurred", order="asc")
        seen.extend(e.id for e in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert cursor.sort == "occurred:asc"

    assert seen == newest_first[::-1]


def test_query_refuses_a_cursor_of_the_other_order_and_an_unknown_sort(session: Session) -> None:
    admin = _add_principal(session, "admin")
    service = AuditService(session, AllowAllAuthorization())
    for _ in range(2):
        service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
    newest = service.query(admin, limit=1).next_cursor
    assert newest is not None and newest.sort == "occurred:desc"
    with pytest.raises(
        ValidationError,
        match="sorted by `occurred:asc`: it came from the list sorted by `occurred:desc`",
    ):
        service.query(admin, cursor=newest, order="asc")
    with pytest.raises(ValidationError, match="`sort` must be one of: occurred; got 'principal'"):
        service.query(admin, sort="principal")
