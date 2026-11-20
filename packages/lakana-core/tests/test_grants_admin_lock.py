"""The administrator-count lock: every change that can lower the count takes it first (#724)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from lakana_core.access.managed import ACCESS_LOCK_KEY
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.grants import service as grants_service
from lakana_core.grants.admin_lock import ADMIN_COUNT_LOCK_KEY, lock_admin_count
from lakana_core.grants.service import assert_admin_headroom
from lakana_core.identity import groups as groups_module
from lakana_core.identity.groups import GroupService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import Principal, PrincipalType


def test_lock_admin_count_does_nothing_off_postgres(session: Session) -> None:
    """On SQLite no statement runs; the key is a signed 64-bit integer of its own."""
    statements: list[str] = []

    def record(*args: object) -> None:
        """Record the statement of a cursor execution.

        Parameters
        ----------
        *args
            The listener's arguments; the third is the statement.
        """
        statements.append(str(args[2]))

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        lock_admin_count(session)
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert statements == []
    assert -(2**63) <= ADMIN_COUNT_LOCK_KEY < 2**63
    assert ADMIN_COUNT_LOCK_KEY != ACCESS_LOCK_KEY


class _PostgresSession:
    """A session double whose bind says Postgres and that records what it executes."""

    def __init__(self) -> None:
        """Start with nothing executed."""
        self.executed: list[tuple[str, dict[str, int]]] = []

    def get_bind(self) -> SimpleNamespace:
        """Return a bind whose dialect is Postgres.

        Returns
        -------
        SimpleNamespace
            An object with ``dialect.name == "postgresql"``.
        """
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement: object, params: dict[str, int]) -> None:
        """Record a statement and its parameters.

        Parameters
        ----------
        statement
            The statement.
        params
            Its parameters.
        """
        self.executed.append((str(statement), params))


def test_lock_admin_count_takes_the_exclusive_transaction_lock_on_postgres() -> None:
    """On Postgres two counting changes never run beside each other: the lock is exclusive."""
    fake = _PostgresSession()
    lock_admin_count(cast(Session, fake))
    assert fake.executed == [("SELECT pg_advisory_xact_lock(:key)", {"key": ADMIN_COUNT_LOCK_KEY})]


def test_the_headroom_check_locks_before_it_counts(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revoke, member removal and deactivation share this check, so all three take the lock."""
    calls: list[str] = []
    monkeypatch.setattr(grants_service, "lock_admin_count", lambda _session: calls.append("lock"))
    monkeypatch.setattr(
        grants_service,
        "active_instance_admin_ids",
        lambda _session, **_kwargs: calls.append("count") or set(),
    )
    assert_admin_headroom(session, excluding_principal_id=uuid.uuid4())
    assert calls == ["lock", "count"]


def test_a_member_removal_locks_before_it_asks_whether_the_group_holds_admin(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #760: the lock comes first, also for a group without ADMIN.

    A concurrent grant of ADMIN to the group may commit after the check, so a removal that
    decided without the lock could slip past a count that relies on the member.
    """
    allow = AllowAllAuthorization()
    actor = PrincipalRef(id=uuid.uuid4(), name="actor", type=PrincipalType.USER)
    session.add(Principal(id=actor.id, type=actor.type, name=actor.name, is_active=True))
    session.commit()
    PrincipalService(session, allow).create_principal(actor, name="x", type=PrincipalType.USER)
    groups = GroupService(session, allow)
    groups.create_group(actor, name="g")
    groups.add_member(actor, "g", "x")
    calls: list[str] = []
    asked = GroupService._grants_instance_admin

    def ask(service: GroupService, group_id: uuid.UUID) -> bool:
        """Record the question and answer it.

        Parameters
        ----------
        service
            The group service.
        group_id
            The group.

        Returns
        -------
        bool
            Whether the group holds instance ADMIN.
        """
        calls.append("ask")
        return asked(service, group_id)

    monkeypatch.setattr(groups_module, "lock_admin_count", lambda _session: calls.append("lock"))
    monkeypatch.setattr(GroupService, "_grants_instance_admin", ask)
    groups.remove_member(actor, "g", "x")
    assert calls == ["lock", "ask"]
