"""The saved-query lists are paged in the database (#692): the same pages, cursors and errors.

`saved_query_test_helpers.old_pages` is the list as it was cut in Python before; each kind of
caller must get exactly its pages, item for item and cursor for cursor, from every list at every
page size. A list reads no SQL text and runs as many statements for many saved queries as for few.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from home_test_helpers import grant, person
from saved_query_test_helpers import LIMITS, LISTS, build_world, new_pages, old_pages
from sqlalchemy import event
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.saved_queries import SavedQueryService, by_code_point
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.models import Environment, SavedQuery
from lakana_core.pagination import BAD_CURSOR, encode_tuple_cursor
from lakana_core.storage.resolver import StorageResolver

_CALLERS = 9


@pytest.mark.parametrize("case", range(_CALLERS))
def test_every_list_gives_each_kind_of_caller_the_pages_of_the_python_filter(
    session: Session, tmp_path: Path, case: int
) -> None:
    callers = build_world(session, tmp_path)
    assert len(callers) == _CALLERS
    caller = callers[case]

    for environment in LISTS:
        for limit in LIMITS:
            expected = old_pages(session, caller, environment, limit)
            got = new_pages(session, caller, environment, limit)
            assert got == expected, (caller.label, environment, limit)


def test_the_world_holds_every_case_the_comparison_needs(session: Session, tmp_path: Path) -> None:
    """The comparison is not vacuous: each kind of caller sees something different."""
    callers = build_world(session, tmp_path)
    seen = {}
    for caller in callers:
        pages = old_pages(session, caller, None, 200)
        assert not isinstance(pages, str)
        seen[caller.label] = frozenset(item[0] for item in pages[0][0])
    # Only the two administrators see the same: every one. A CI token of one sees only their own.
    assert len(set(seen.values())) == len(callers) - 1
    assert seen["nobody"] == frozenset()
    assert frozenset() < seen["administrator's CI token"] < seen["administrator, owner"]
    every = seen["administrator, owner"]
    assert seen["administrator with READ_QUERY_TEXT"] == every
    assert all(ids < every for label, ids in seen.items() if "administrator" not in label)
    errors = set()
    for caller in callers:
        for environment in ("gone", "nowhere", "dev-eu"):
            pages = old_pages(session, caller, environment, 200)
            if isinstance(pages, str):
                errors.add(pages)
    assert {"NotFoundError", "PermissionDenied"} <= errors


@contextmanager
def _statements(session: Session) -> Generator[list[str]]:
    """Collect the SQL statements the session's engine runs.

    Parameters
    ----------
    session
        Database session.

    Yields
    ------
    list of str
        The statements, filled as they run.
    """
    engine = session.get_bind()
    ran: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        ran.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield ran
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_a_list_reads_no_sql_text_and_the_same_statements_for_many_queries(
    session: Session, tmp_path: Path
) -> None:
    maker = person(session, "maker")
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    for name in ("dev", "prod"):
        catalog.create_environment(
            maker,
            name=name,
            storage=StorageConfig(scheme="file", root=f"file://{tmp_path / name}", options={}),
        )
    bob = person(session, "bob")
    carol = person(session, "carol")
    root = person(session, "root")
    for who in (bob, carol):
        grant(session, who, action=Action.SUBMIT_QUERY, environment_name="dev")
    grant(session, root, role=Role.ADMIN)
    grant(session, root, action=Action.READ_QUERY_TEXT)
    saves = SavedQueryService(session, AllowAllAuthorization())
    service = SavedQueryService(session, GrantAuthorization(session))

    def save(first: int, last: int) -> None:
        for n in range(first, last):
            for environment in ("dev", "prod"):
                visibility = "everyone" if n % 2 else "private"
                saves.create(
                    bob, environment, name=f"q{n:03}", sql="SELECT 1", visibility=visibility
                )

    def counts() -> list[int]:
        found = []
        for who, environment in ((bob, None), (bob, "dev"), (carol, None), (root, None)):
            with _statements(session) as ran:
                page = service.list_page(who, environment=environment, limit=1)
            assert len(page.items) == 1 and page.next_cursor is not None
            assert not [s for s in ran if "sql_text" in s], ran
            listing = [s for s in ran if "FROM saved_queries" in s]
            assert listing and all("LIMIT" in s for s in listing), listing
            found.append(len(ran))
        return found

    save(0, 4)
    # The first round also writes the administrator's view, once per window, and warms up.
    counts()
    few = counts()
    save(4, 40)
    assert counts() == few


@pytest.mark.parametrize(
    "key",
    [("revenue", "dev"), ("revenue", "dev", "not-an-id"), ("revenue", "dev", "a", "b")],
    ids=["two-parts", "bad-id", "four-parts"],
)
def test_a_cursor_whose_key_is_no_saved_querys_is_refused(
    session: Session, tmp_path: Path, key: tuple[str, ...]
) -> None:
    callers = build_world(session, tmp_path)
    service = SavedQueryService(session, GrantAuthorization(session))

    with pytest.raises(ValidationError) as refused:
        service.list_page(callers[0].ref, cursor=encode_tuple_cursor("all-saved-queries", key))

    assert str(refused.value) == BAD_CURSOR


def _queries(
    session: Session, tmp_path: Path, *names: str
) -> tuple[SavedQueryService, PrincipalRef]:
    """Save private queries of one querier in `dev`, by name.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Holds the environment's storage root.
    names
        The names, saved in this order.

    Returns
    -------
    tuple of (SavedQueryService, PrincipalRef)
        A service with the grants' authorization, and the querier.
    """
    maker = person(session, "maker")
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        maker,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path / 'dev'}", options={}),
    )
    bob = person(session, "bob")
    grant(session, bob, action=Action.SUBMIT_QUERY, environment_name="dev")
    service = SavedQueryService(session, GrantAuthorization(session))
    for name in names:
        service.create(bob, "dev", name=name, sql="SELECT 1")
    return service, bob


def test_a_cursor_written_as_main_issued_it_resumes_after_its_item(
    session: Session, tmp_path: Path
) -> None:
    """The cursor format is pinned by hand, as `page_in_order` wrote it before #692."""
    service, bob = _queries(session, tmp_path, "gamma", "Beta", "alpha")
    first = service.list_page(bob, limit=2)
    beta = first.items[1]
    assert [v.name for v in first.items] == ["alpha", "Beta"]
    # base64url, unpadded, of the UTF-8 JSON {"k": kind, "n": JSON array of the key}.
    body = '{"k": "all-saved-queries", "n": "[\\"beta\\", \\"dev\\", \\"' + str(beta.id) + '\\"]"}'
    written = base64.urlsafe_b64encode(body.encode()).rstrip(b"=").decode()

    assert first.next_cursor == written
    after = service.list_page(bob, limit=2, cursor=written)
    assert [v.name for v in after.items] == ["gamma"]
    assert after.next_cursor is None
    # A cursor of an item since deleted still resumes after where it was.
    gone = body.replace(str(beta.id), str(uuid.UUID(int=0)))
    stale = base64.urlsafe_b64encode(gone.encode()).rstrip(b"=").decode()
    assert [v.name for v in service.list_page(bob, cursor=stale).items] == ["Beta", "gamma"]


def test_a_rename_moves_the_query_in_the_list_order(session: Session, tmp_path: Path) -> None:
    service, bob = _queries(session, tmp_path, "zeta", "Beta")
    zeta = service.list_page(bob).items[1]

    service.update(bob, "dev", zeta.id, name="STRAẞE", sql="SELECT 1", visibility="private")
    service.update(bob, "dev", zeta.id, name="Alpha", sql="SELECT 1", visibility="private")

    row = session.get(SavedQuery, zeta.id)
    assert row is not None and row.name_key == "alpha"
    assert [v.name for v in service.list_page(bob).items] == ["Alpha", "Beta"]
    one = service.list_page(bob, environment="dev", limit=1)
    assert one.next_cursor == encode_tuple_cursor("saved-queries", ("alpha", "dev", str(zeta.id)))


@pytest.mark.parametrize(
    ("url", "collated"),
    [("sqlite://", False), ("postgresql+pg8000://lakana@localhost/lakana", True)],
    ids=["sqlite", "postgres"],
)
def test_environment_names_compare_by_code_point_on_each_database(url: str, collated: bool) -> None:
    """Postgres is told ``"C"``; SQLite compares UTF-8 bytes, code-point order, already.

    No connection is opened: the dialect alone decides, and the expression is compiled for it.
    """
    engine = sa.create_engine(url)
    try:
        column = by_code_point(engine.dialect.name, Environment.name.expression)
        compiled = str(column.compile(dialect=engine.dialect))
    finally:
        engine.dispose()
    assert compiled.endswith('COLLATE "C"') is collated
    assert compiled.startswith("environments.name")
