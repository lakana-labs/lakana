"""The table list and the search on a real PostgreSQL: one ranked read, integer cursors (M7 3a)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator

import pytest
from home_test_helpers import environment, person
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog import search
from lakana_core.catalog.search import CatalogSearch, SearchPage
from lakana_core.catalog.table_listing import DEFAULT_ORDER, TableListing, TableSort
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NAMESPACE_SEPARATOR
from lakana_core.models import Base, Namespace, Table, TableColumn, TableType
from lakana_core.pagination import encode_sorted_cursor

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Start one Postgres.

    Yields
    ------
    Engine
        An engine on it.
    """
    with PostgresContainer("postgres:16-alpine", driver="pg8000") as pg:
        engine = create_engine(DatabaseConfig(url=pg.get_connection_url()))
        yield engine
        engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """Give each test a fresh schema.

    Parameters
    ----------
    engine
        The engine.

    Yields
    ------
    Session
        A session on the fresh schema.
    """
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _world(session: Session) -> tuple[PrincipalRef, PrincipalRef]:
    """Build `dev` with eight tables in `silver` and two in `gold`, three columns each.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of PrincipalRef
        An administrator, and a reader of the namespace `silver` alone.
    """
    admin, reader = person(session, "admin"), person(session, "reader")
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    dev = environment(session, "dev")
    for path, count in (("silver", 8), ("gold", 2)):
        ns = Namespace(
            id=uuid.uuid4(), environment_id=dev, path=path.replace(".", NAMESPACE_SEPARATOR)
        )
        session.add(ns)
        session.flush()
        for n in range(count):
            table = Table(
                id=uuid.uuid4(),
                namespace_id=ns.id,
                name=f"c{path}{n}",
                table_type=TableType.ICEBERG,
                row_count=n,
            )
            session.add(table)
            session.flush()
            for m in range(3):
                session.add(
                    TableColumn(
                        id=uuid.uuid4(), table_id=table.id, name=f"c{n}_{m}", type="int", position=m
                    )
                )
    session.commit()
    grants.grant(
        admin,
        principal="reader",
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace="silver",
    )
    session.commit()
    return admin, reader


def _ranked(engine: Engine, call: Callable[[], object]) -> int:
    """Count the ranked search statements a call sends.

    Parameters
    ----------
    engine
        The engine.
    call
        What to run, without arguments.

    Returns
    -------
    int
        How many statements were ordered by the search rank.
    """
    statements: list[str] = []

    def _keep(conn: object, cursor: object, statement: str, *args: object) -> None:
        """Keep one statement.

        Parameters
        ----------
        conn
            The connection.
        cursor
            The cursor.
        statement
            The SQL.
        *args
            The rest.
        """
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _keep)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", _keep)
    return sum("ORDER BY hits.rank" in s for s in statements)


def test_a_streamed_search_asks_the_rights_while_it_reads(
    session: Session, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # chunks of 2: the reader's rights are asked between fetches of the one open statement
    monkeypatch.setattr(search, "CHUNK", 2)
    admin, reader = _world(session)
    found = CatalogSearch(session, GrantAuthorization(session))
    pages: list[SearchPage] = []
    assert _ranked(engine, lambda: pages.append(found.search(reader, "dev", "c", limit=30))) == 1
    names = {h.name.split(".")[0] for h in pages[0].results}
    assert names == {"silver"}
    listing = TableListing(session, GrantAuthorization(session))
    listed: list[list[str]] = []
    assert (
        _ranked(engine, lambda: listed.append([i.name for i in listing.list(admin, q="c").items]))
        == 1
    )
    assert len(listed[0]) == 10


@pytest.mark.parametrize(
    ("sort", "value", "ok"),
    [
        (TableSort.HEALTH, 2**15 - 1, True),
        (TableSort.HEALTH, 2**15, False),
        (TableSort.ROWS, 2**63 - 1, True),
        (TableSort.ROWS, 2**63, False),
        (TableSort.SIZE, -(2**63), True),
        (TableSort.SIZE, -(2**63) - 1, False),
    ],
)
def test_an_integer_cursor_is_read_or_refused_never_sent_out_of_range(
    session: Session, sort: TableSort, value: int, ok: bool
) -> None:
    admin, _ = _world(session)
    listing = TableListing(session, GrantAuthorization(session))
    sort_name = f"{sort.value}:{DEFAULT_ORDER[sort].value}"
    cursor = encode_sorted_cursor("tables", sort_name, (value,), uuid.uuid4())
    if ok:
        listing.list(admin, sort=sort, cursor=cursor)
    else:
        with pytest.raises(ValidationError, match="cursor is not valid"):
            listing.list(admin, sort=sort, cursor=cursor)
