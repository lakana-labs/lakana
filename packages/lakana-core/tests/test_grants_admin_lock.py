"""The administrator-count lock: every change that can lower the count takes it first (#724)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy.orm import Session

from lakana_core.access.managed import ACCESS_LOCK_KEY
from lakana_core.grants import service as grants_service
from lakana_core.grants.admin_lock import ADMIN_COUNT_LOCK_KEY, lock_admin_count
from lakana_core.grants.service import assert_admin_headroom


def test_lock_admin_count_does_nothing_off_postgres(session: Session) -> None:
    """On SQLite nothing is locked; the key is a signed 64-bit integer of its own."""
    lock_admin_count(session)
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
