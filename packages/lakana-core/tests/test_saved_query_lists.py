"""The saved-query lists are paged in the database (#692): the same pages, cursors and errors.

`saved_query_test_helpers.old_pages` is the list as it was cut in Python before; each kind of
caller must get exactly its pages, item for item and cursor for cursor, from every list at every
page size. A list reads no SQL text and runs as many statements for many saved queries as for few.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from home_test_helpers import grant, person
from saved_query_test_helpers import LIMITS, LISTS, build_world, new_pages, old_pages
from sqlalchemy import event
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization
from lakana_core.catalog.saved_queries import SavedQueryService
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
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
