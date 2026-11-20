"""A world of saved queries, and the saved-query lists as they were cut in Python before #692.

The world mixes owners with and without `SUBMIT_QUERY`, private and shared queries, a former
owner, an instance administrator with and without `READ_QUERY_TEXT`, a CI token of one, a service
account, several environments and a deleted one. Names differ only in letter case, fold to the
same text (``Straße`` and ``STRASSE``), and environment names order differently by code point
than under a glibc collation (``dev-eu`` and ``deva``).

`old_pages` is the list as `SavedQueryService.list_page` built it before #692: every row of a live
environment read, kept by the visibility rule in Python, sorted and cut by `page_in_order`. The
tests compare the database's pages with it, caller by caller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from home_test_helpers import grant, person
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, CiScope, PrincipalRef
from lakana_core.catalog.saved_queries import SavedQueryService, SavedQueryView
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import NotFoundError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    Environment,
    Grant,
    Principal,
    PrincipalType,
    SavedQuery,
    SavedQueryVisibility,
)
from lakana_core.pagination import page_in_order
from lakana_core.storage.resolver import StorageResolver

ENVIRONMENTS = ("dev", "dev-eu", "deva", "gone")
"""The environments; ``gone`` is deleted once its queries are saved."""

LISTS: tuple[str | None, ...] = (None, *ENVIRONMENTS, "nowhere")
"""Every list a caller can ask for: across environments, each environment, a missing one."""

LIMITS = (1, 3, 200)
"""The page sizes each list is read with."""

_NAMES = ("Revenue", "revenue", "REVENUE", "Straße", "STRASSE", "alpha", "Ärger", "zeta", "Ω")


@dataclass(frozen=True)
class Caller:
    """A kind of caller, and what the rule gives them, written out by hand.

    Parameters
    ----------
    label
        The case's name.
    ref
        The caller.
    admin
        Whether they act as an instance administrator.
    queryable
        The environments where they hold `SUBMIT_QUERY`.
    """

    label: str
    ref: PrincipalRef
    admin: bool
    queryable: frozenset[str]


Item = tuple[uuid.UUID, str, str, str | None, bool, tuple[str, ...]]
"""What a list shows of one saved query: id, environment, name, owner, former owner, actions."""

Pages = list[tuple[list[Item], str | None]]
"""Each page's items and its `next_cursor`; or the error a list raised, by its class name."""


def _owner_saves(
    service: SavedQueryService, who: PrincipalRef, environment: str, step: int
) -> None:
    """Save a few queries for one owner in one environment, private and shared.

    Parameters
    ----------
    service
        A service that allows every save.
    who
        The owner.
    environment
        The environment.
    step
        Where in `_NAMES` this owner starts, so owners share some names and not others.
    """
    for n in range(4):
        name = _NAMES[(step + 2 * n) % len(_NAMES)]
        visibility = "everyone" if (step + n) % 2 else "private"
        service.create(
            who, environment, name=name, sql=f"SELECT {step}, {n}", visibility=visibility
        )


def build_world(session: Session, tmp_path: Path) -> list[Caller]:
    """Save the world's queries and give every kind of caller.

    Parameters
    ----------
    session
        Database session, with the schema created.
    tmp_path
        Holds the environments' storage roots.

    Returns
    -------
    list of Caller
        One per kind of caller.
    """
    maker = person(session, "maker")
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    for name in ENVIRONMENTS:
        catalog.create_environment(
            maker,
            name=name,
            storage=StorageConfig(scheme="file", root=f"file://{tmp_path / name}", options={}),
        )
    bob = person(session, "bob")
    carol = person(session, "carol")
    dave = person(session, "dave")
    erin = person(session, "erin")
    svc = person(session, "svc", PrincipalType.SERVICE_ACCOUNT)
    frank = person(session, "frank")
    root = person(session, "root")
    reader = person(session, "reader")
    nobody = person(session, "nobody")
    for who, environments in (
        (bob, ("dev", "deva")),
        (carol, ("dev",)),
        (dave, ("deva",)),
        (svc, ("dev-eu",)),
    ):
        for environment in environments:
            grant(session, who, action=Action.SUBMIT_QUERY, environment_name=environment)
    for admin in (root, reader):
        grant(session, admin, role=Role.ADMIN)
    grant(session, reader, action=Action.READ_QUERY_TEXT)

    saves = SavedQueryService(session, AllowAllAuthorization())
    for step, (who, environments) in enumerate(
        (
            (bob, ("dev", "dev-eu", "deva", "gone")),
            (carol, ("dev", "deva")),
            (dave, ("dev", "deva", "gone")),
            (erin, ("dev", "dev-eu", "gone")),
            (svc, ("dev-eu", "dev")),
            (frank, ("dev", "deva")),
            (root, ("dev", "gone")),
        )
    ):
        for environment in environments:
            _owner_saves(saves, who, environment, step)
    # Renames keep `name_key` in step: one to a name that folds away from where it sorted.
    for who in (bob, svc):
        mine = next(v for v in saves.list_page(who, environment="dev").items if v.owner == who.name)
        saves.update(who, "dev", mine.id, name="ÉCOLE", sql="SELECT 2", visibility=mine.visibility)
    # Frank's principal goes: his private queries with it, his shared ones stay as former owner's.
    session.execute(delete(Grant).where(Grant.principal_id == frank.id))
    session.execute(delete(Principal).where(Principal.id == frank.id))
    session.commit()
    catalog.delete_environment(maker, "gone")
    session.expire_all()

    ci = CiScope(
        token_id=uuid.uuid4(),
        rule_id=uuid.uuid4(),
        environment="dev",
        environment_id=uuid.uuid4(),
        repository="repo",
        repository_id=uuid.uuid4(),
        kinds=frozenset({"deploy"}),
        sha=None,
    )
    none: frozenset[str] = frozenset()
    every = frozenset(ENVIRONMENTS)  # the ADMIN role holds every right, `SUBMIT_QUERY` too
    return [
        Caller("querier of two environments, owner", bob, False, frozenset({"dev", "deva"})),
        Caller("querier of one environment, owner", carol, False, frozenset({"dev"})),
        Caller("owner without the right where they own", dave, False, frozenset({"deva"})),
        Caller("owner with no right anywhere", erin, False, none),
        Caller("service account", svc, False, frozenset({"dev-eu"})),
        Caller("administrator, owner", root, True, every),
        Caller("administrator with READ_QUERY_TEXT", reader, True, every),
        Caller(
            "administrator's CI token",
            PrincipalRef(root.id, root.name, root.type, ci_scope=ci),
            False,
            none,
        ),
        Caller("nobody", nobody, False, none),
    ]


def _old_rows(session: Session, caller: Caller, environment: str | None) -> list[Item]:
    """Read every saved query of the live environments and keep the caller's, in Python.

    Parameters
    ----------
    session
        Database session.
    caller
        The caller and their rights.
    environment
        The list's environment, or `None` for every one.

    Returns
    -------
    list of Item
        What the old list showed, in no order.

    Raises
    ------
    NotFoundError
        If the named environment does not exist or is deleted.
    PermissionDenied
        If the caller may not list the named environment.
    """
    live = dict(
        session.execute(select(Environment.name, Environment.id).where(LIVE_ENVIRONMENT)).all()
    )
    me = caller.ref.id
    if environment is not None:
        if environment not in live:
            raise NotFoundError(environment)
        owns_one = session.execute(
            select(SavedQuery.id).where(
                SavedQuery.environment_id == live[environment], SavedQuery.owner_id == me
            )
        ).first()
        if environment not in caller.queryable and not caller.admin and owns_one is None:
            raise PermissionDenied(environment)
    queryable = {live[name] for name in caller.queryable if name in live}
    rows = session.execute(
        select(SavedQuery, Environment.name, Principal.name)
        .join(Environment, Environment.id == SavedQuery.environment_id)
        .outerjoin(Principal, Principal.id == SavedQuery.owner_id)
        .where(LIVE_ENVIRONMENT)
    ).all()
    items: list[Item] = []
    for row, env_name, owner in rows:
        if environment is not None and env_name != environment:
            continue
        own = row.owner_id == me
        sees = (
            caller.admin
            or own
            or (row.visibility == SavedQueryVisibility.EVERYONE and row.environment_id in queryable)
        )
        if not sees:
            continue
        if own:
            actions = ("update", "delete") if row.environment_id in queryable else ("delete",)
        else:
            actions = ("delete",) if caller.admin else ()
        items.append((row.id, env_name, row.name, owner, row.owner_id is None, actions))
    return items


def old_pages(session: Session, caller: Caller, environment: str | None, limit: int) -> Pages | str:
    """Page a list as the service did before #692.

    Parameters
    ----------
    session
        Database session.
    caller
        The caller and their rights.
    environment
        The list's environment, or `None` for every one.
    limit
        The page size.

    Returns
    -------
    Pages or str
        Every page with its cursor, or the name of the error the list raised.
    """
    try:
        items = _old_rows(session, caller, environment)
    except (NotFoundError, PermissionDenied) as exc:
        return type(exc).__name__
    kind = "all-saved-queries" if environment is None else "saved-queries"
    pages: Pages = []
    cursor: str | None = None
    while True:
        page = page_in_order(
            items,
            kind=kind,
            key_of=lambda i: (i[2].casefold(), i[1], str(i[0])),
            limit=limit,
            cursor=cursor,
        )
        pages.append((list(page.items), page.next_cursor))
        cursor = page.next_cursor
        if cursor is None:
            return pages


def _item(view: SavedQueryView) -> Item:
    return (view.id, view.environment, view.name, view.owner, view.former_owner, view.actions)


def new_pages(session: Session, caller: Caller, environment: str | None, limit: int) -> Pages | str:
    """Page a list with `SavedQueryService.list_page`, checking that no item carries SQL.

    Parameters
    ----------
    session
        Database session.
    caller
        The caller.
    environment
        The list's environment, or `None` for every one.
    limit
        The page size.

    Returns
    -------
    Pages or str
        Every page with its cursor, or the name of the error the list raised.
    """
    service = SavedQueryService(session, GrantAuthorization(session))
    pages: Pages = []
    cursor: str | None = None
    while True:
        try:
            page = service.list_page(
                caller.ref, environment=environment, limit=limit, cursor=cursor
            )
        except (NotFoundError, PermissionDenied) as exc:
            return type(exc).__name__
        assert all(v.sql is None for v in page.items)
        pages.append(([_item(v) for v in page.items], page.next_cursor))
        cursor = page.next_cursor
        if cursor is None:
            return pages
