"""Two exchanges of one `lakana login` code at once, on a real PostgreSQL (#32, PR 3).

Each exchange runs in a thread of its own with its own session, released together. The claim is
one conditional ``UPDATE ... WHERE used_at IS NULL``: whichever comes second waits for the row
and then finds it used. Whatever the order, exactly one gets a token.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core import migrate
from lakana_core.auth import AllowAllAuthorization
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import CliGrantRefused
from lakana_core.identity.cli_grants import CliGrantService, s256
from lakana_core.identity.sessions import SessionService
from lakana_core.models import PersonalAccessToken, Principal, PrincipalType

pytestmark = pytest.mark.integration

VERIFIER = "v" * 43


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Start one Postgres, migrated to head.

    Yields
    ------
    Engine
        An engine on it.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url)
        engine = create_engine(DatabaseConfig(url=url))
        yield engine
        engine.dispose()


def test_two_exchanges_of_one_code_at_once_issue_one_token(engine: Engine) -> None:
    allow = AllowAllAuthorization()
    with Session(engine) as session:
        kim = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="kim", is_active=True)
        session.add(kim)
        session.commit()
        sessions = SessionService(session, allow)
        started = sessions.start(
            kim.id, via="passkey", replacing=None, peer="p", user_agent="u", touched=True
        )
        session.commit()
        ref = sessions.verify(started.cookie_value, extend=False)
        code = CliGrantService(session, allow).approve_loopback(
            ref, port=49152, code_challenge=s256(VERIFIER), host="laptop"
        )
        kim_id = kim.id
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def exchange() -> None:
        with Session(engine) as own:
            barrier.wait()
            try:
                CliGrantService(own, allow).exchange_loopback(code, VERIFIER)
                outcomes.append("token")
            except CliGrantRefused:
                outcomes.append("refused")

    threads = [threading.Thread(target=exchange) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(outcomes) == ["refused", "token"]
    with Session(engine) as session:
        tokens = session.execute(
            select(PersonalAccessToken).where(PersonalAccessToken.principal_id == kim_id)
        ).scalars()
        assert [row.name for row in tokens] == ["lakana login on laptop"]
