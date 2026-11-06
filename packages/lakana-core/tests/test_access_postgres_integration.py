"""The managed-scope guard and the apply on a real PostgreSQL (#46)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy
from access_test_helpers import access_world
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core import migrate
from lakana_core.access.managed import ManagedScopeError, lock_access
from lakana_core.access.service import AccessService
from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.ci_oidc.config import GITHUB_ISSUER
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import AccessManagedScope

pytestmark = pytest.mark.integration

_LOCK_NOT_AVAILABLE = "55P03"
"""The SQLSTATE of a lock wait that ran past `lock_timeout`."""
_BEFORE_0067 = "f8b2c4d6e0a1"
_FILE = (
    "version: 1\nmanages: [instance]\ngrants:\n  - {to: user:admin, role: ADMIN, on: instance}\n"
)


@pytest.fixture(scope="module")
def url() -> Iterator[str]:
    """Start one Postgres, migrated to head.

    Yields
    ------
    str
        Its URL.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url)
        yield url


@pytest.fixture(scope="module")
def engine(url: str) -> Iterator[Engine]:
    """Give an engine on the migrated database.

    Parameters
    ----------
    url
        The database's URL.

    Yields
    ------
    Engine
        The engine.
    """
    engine = create_engine(DatabaseConfig(url=url))
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def admin(engine: Engine) -> PrincipalRef:
    """Seed the access world and apply a file that manages `instance`.

    Parameters
    ----------
    engine
        The engine.

    Returns
    -------
    PrincipalRef
        The administrator who applied it.
    """
    with Session(engine) as session:
        world = access_world(session)
        service = AccessService(
            session, GrantAuthorization(session), world.catalog, issuers=[GITHUB_ISSUER]
        )
        plan = service.plan(world.admin, _FILE)
        assert plan.problems == ()
        service.apply(world.admin, _FILE, plan_fingerprint=plan.fingerprint)
        return world.admin


def test_an_apply_records_its_scope_on_postgres(engine: Engine, admin: PrincipalRef) -> None:
    """The scope row of the apply is there, with its file."""
    with Session(engine) as session:
        row = session.scalars(select(AccessManagedScope)).one()
        assert (row.scope, row.file_name, row.applied_by) == ("instance", "access.yaml", admin.id)


def test_a_guarded_change_waits_for_a_running_apply(engine: Engine, admin: PrincipalRef) -> None:
    """Review focus 2: session A holds the exclusive lock; B's guard times out on the shared one."""
    with Session(engine) as a, Session(engine) as b:
        grants = GrantService(b, AllowAllAuthorization())
        lock_access(a, shared=False)
        b.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError) as caught:
            grants.grant(admin, principal="bob", role=Role.VIEWER, kind=SecurableKind.INSTANCE)
        assert _LOCK_NOT_AVAILABLE in str(caught.value.orig)
        a.rollback()
        b.rollback()
        with pytest.raises(ManagedScopeError):
            grants.grant(admin, principal="bob", role=Role.VIEWER, kind=SecurableKind.INSTANCE)


def test_0067_goes_down_and_up_again(url: str, admin: PrincipalRef) -> None:
    """The migration's round trip on Postgres, after an apply wrote its rows."""
    migrate.downgrade(url, _BEFORE_0067)
    check = sqlalchemy.create_engine(url)
    assert "access_managed_scopes" not in sqlalchemy.inspect(check).get_table_names()
    migrate.upgrade(url)
    tables = set(sqlalchemy.inspect(check).get_table_names())
    check.dispose()
    assert {"access_managed_scopes", "access_overrides"} <= tables
