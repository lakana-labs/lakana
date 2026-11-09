"""Sign-in links on a real PostgreSQL: two concurrent uses of one link, exactly one wins."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization
from lakana_core.bootstrap import bootstrap_if_needed
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import LinkUnusableError, ValidationError
from lakana_core.identity.links import LinkService, Redeemed
from lakana_core.identity.pats import PatService
from lakana_core.identity.principals import PrincipalService
from lakana_core.identity.sessions import SessionService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    BrowserSession,
    Principal,
    PrincipalType,
    SignInNotice,
)

pytestmark = pytest.mark.integration

ORIGIN = "http://localhost:8000"


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


def _service(session: Session) -> LinkService:
    """Build a link service that allows every action.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    LinkService
        The service.
    """
    authz = AllowAllAuthorization()
    return LinkService(
        session,
        authz,
        SessionService(session, authz),
        origin=ORIGIN,
        local_mode=True,
        bootstrap_name="admin",
    )


@pytest.mark.parametrize("kind", ["setup", "invite", "recovery", "local"])
def test_two_concurrent_uses_of_one_link_exactly_one_wins(engine: Engine, kind: str) -> None:
    """Two connections use one link at once: one session (one account), one `used` refusal.

    For every kind: setup, invite (one account), recovery (its passkey removal, session end and
    notices happen once) and local.
    """
    with Session(engine) as setup:
        bootstrap_if_needed(setup, "admin", key_id="opkey", secret="opsecret")
        operator = PatService(setup, AllowAllAuthorization()).verify("opkey", "opsecret")
        if kind == "setup":
            link = _service(setup).issue_setup(operator)
        elif kind == "invite":
            link = _service(setup).issue_invite(operator, name="carol", display_name=None)
        else:
            person = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=f"bob-{kind}")
            setup.add(person)
            setup.commit()
            if kind == "recovery":
                link = _service(setup).issue_recovery(operator, principal_name=person.name)
            else:
                issued = PatService(setup, AllowAllAuthorization()).issue(
                    operator, person.name, name="laptop"
                )
                key_id, secret = issued.token.removeprefix("lakana_pat_").split("_", 1)
                own = PatService(setup, AllowAllAuthorization()).verify(key_id, secret)
                link = _service(setup).issue_local(own)
    fragment = dict(item.split("=", 1) for item in link.url.split("#", 1)[1].split("&"))
    barrier = threading.Barrier(2)
    results: list[object] = []

    def use() -> None:
        """Use the link from its own connection."""
        with Session(engine) as own:
            service = _service(own)
            barrier.wait()
            try:
                results.append(
                    service.redeem(
                        fragment["l"],
                        fragment["s"],
                        replacing=None,
                        peer="p",
                        user_agent="u",
                        has_session=False,
                    )
                )
            except LinkUnusableError as exc:
                results.append(exc)

    threads = [threading.Thread(target=use) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)
    assert sorted(type(r).__name__ for r in results) == ["LinkUnusableError", "Redeemed"]
    refused = next(r for r in results if isinstance(r, LinkUnusableError))
    assert refused.state == "used"
    won = next(r for r in results if isinstance(r, Redeemed))
    with Session(engine) as check:
        sessions = check.execute(
            select(func.count())
            .select_from(BrowserSession)
            .where(BrowserSession.link_id == link.id)
        ).scalar_one()
        assert sessions == 1
        used = check.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.SIGN_IN_LINK_USED)
            .where(AuditEvent.detail["link_id"].as_string() == str(link.id))
        ).scalar_one()
        assert used == 1
        if kind == "recovery":
            notices = check.execute(
                select(func.count())
                .select_from(SignInNotice)
                .where(SignInNotice.detail["link_id"].as_string() == str(link.id))
            ).scalar_one()
            assert notices == 2  # the person and the one administrator, once each
        if kind == "invite":
            carols = check.execute(
                select(func.count()).select_from(Principal).where(Principal.name == "carol")
            ).scalar_one()
            assert carols == 1
            assert won.started.principal.name == "carol"


def test_a_name_whose_normalised_form_is_too_long_is_refused(engine: Engine) -> None:
    """On Postgres the refusal comes before the 255-character column, not as a server error."""
    with Session(engine) as session:
        bootstrap_if_needed(session, "admin", key_id="opkey", secret="opsecret")
        operator = PatService(session, AllowAllAuthorization()).verify("opkey", "opsecret")
        with pytest.raises(ValidationError, match="255"):
            _service(session).issue_invite(operator, name="\ufb03" * 100, display_name=None)
        with pytest.raises(ValidationError, match="255"):
            PrincipalService(session, AllowAllAuthorization()).create_principal(
                operator, name="\ufb03" * 100, type=PrincipalType.USER
            )
