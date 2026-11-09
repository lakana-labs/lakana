"""Browser sessions on a real PostgreSQL: two concurrent ends of one session end it once."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import SessionEndedError
from lakana_core.identity.sessions import SessionService
from lakana_core.models import AuditEvent, AuditEventType, Base, Principal, PrincipalType

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Start one Postgres with the schema of the models.

    Yields
    ------
    Engine
        An engine on it.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()


def test_two_concurrent_ends_of_one_session_end_it_once(engine: Engine) -> None:
    """The second end waits for the first, then finds nothing open: one end, one event."""
    principal_id = uuid.uuid4()
    with Session(engine) as setup:
        setup.add(Principal(id=principal_id, type=PrincipalType.USER, name="alice"))
        setup.commit()
        started = SessionService(setup, AllowAllAuthorization()).start(
            principal_id,
            via="passkey",
            replacing=None,
            peer="127.0.0.1",
            user_agent="x",
            touched=True,
        )
        setup.commit()
    counts: list[int] = []
    with Session(engine) as first:
        counts.append(
            SessionService(first, AllowAllAuthorization()).end_for_principal(
                principal_id, "deactivated"
            )
        )

        def second() -> None:
            """End the same sessions from another connection; waits for `first`'s lock."""
            with Session(engine) as other:
                counts.append(
                    SessionService(other, AllowAllAuthorization()).end_for_principal(
                        principal_id, "ended_by_admin", commit=True
                    )
                )

        waiting = threading.Thread(target=second)
        waiting.start()
        waiting.join(timeout=0.5)
        first.commit()
    waiting.join(timeout=30)
    assert not waiting.is_alive()
    assert sorted(counts) == [0, 1]
    with Session(engine) as check:
        ended = check.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.SESSION_ENDED)
        ).scalar_one()
        assert ended == 1
        with pytest.raises(SessionEndedError) as e:
            SessionService(check, AllowAllAuthorization(), clock=lambda: datetime.now(UTC)).verify(
                started.cookie_value, extend=True
            )
        assert e.value.reason == "deactivated"
