"""The list of the tables a caller may read: rights, filters, search, sorting, paging, the tree."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from home_test_helpers import check_result, deployed_job, environment, person, run_of, table_read
from sqlalchemy import Engine, delete, event, func, select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind, TaskScope
from lakana_core.catalog import search, table_listing
from lakana_core.catalog.health import readers
from lakana_core.catalog.health_summary import HEALTH_ITEMS, HealthSummary
from lakana_core.catalog.table_listing import (
    DEFAULT_ORDER,
    MAX_LIMIT,
    SortOrder,
    TableListing,
    TableListItem,
    TableListPage,
    TableMatch,
    TableOwner,
    TableSort,
)
from lakana_core.db import db_now
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NAMESPACE_SEPARATOR
from lakana_core.models import (
    AuditEvent,
    CheckOutcome,
    ColumnDescription,
    Namespace,
    PrincipalType,
    Table,
    TableColumn,
    TableType,
    TextTranslation,
)
from lakana_core.pagination import encode_sorted_cursor

_T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _namespace(session: Session, env_id: uuid.UUID, dotted: str) -> uuid.UUID:
    """Persist a namespace.

    Parameters
    ----------
    session
        Database session.
    env_id
        Its environment.
    dotted
        Its path, every dot a separator.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    row = Namespace(
        id=uuid.uuid4(), environment_id=env_id, path=dotted.replace(".", NAMESPACE_SEPARATOR)
    )
    session.add(row)
    session.flush()
    return row.id


def _table(session: Session, namespace_id: uuid.UUID, name: str, **keys: object) -> Table:
    """Persist a table row with its stored keys.

    Parameters
    ----------
    session
        Database session.
    namespace_id
        Its namespace.
    name
        Its name.
    **keys
        More columns of the row (`table_type`, the stored keys, `owner_id`, `created_at`).

    Returns
    -------
    Table
        The row, flushed.
    """
    row = Table(
        id=uuid.uuid4(),
        namespace_id=namespace_id,
        name=name,
        table_type=keys.pop("table_type", TableType.ICEBERG),
        **keys,
    )
    session.add(row)
    session.flush()
    return row


def _health(item: str | None) -> dict[str, object]:
    """Give the stored health key of a word.

    Parameters
    ----------
    item
        The word; `None` for one not yet computed.

    Returns
    -------
    dict
        `health_item` and `health_rank`.
    """
    return {"health_item": item, "health_rank": 16 if item is None else HEALTH_ITEMS.index(item)}


class ListingWorld:
    """Two environments of tables, three callers and the listing.

    Attributes
    ----------
    session
        Database session.
    listing
        The listing, under the real grants.
    admin
        Holds the ADMIN role on the instance.
    alice
        May read `dev.silver.orders` and the namespace `prod.gold`.
    tim
        May read `dev.gold.orders` alone, and not the environment.
    last_read
        The stored last read of `dev.silver.orders`.
    admin_count
        How many tables there are.
    """

    def __init__(self, session: Session) -> None:
        """Build the world.

        Parameters
        ----------
        session
            Database session.
        """
        self.session = session
        self.admin = person(session, "admin")
        self.alice = person(session, "alice")
        self.tim = person(session, "tim")
        loader = person(session, "loader", PrincipalType.SERVICE_ACCOUNT)
        bob, carl, dave = (person(session, n) for n in ("bob", "carl", "dave"))
        dev, prod = environment(session, "dev"), environment(session, "prod")
        ns = {
            path: _namespace(session, dev, path)
            for path in ("silver", "gold", "gold.sales", "golden", "gold_x", "raw", "empty")
        }
        self.last_read = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
        managed = {"table_type": TableType.ICEBERG}
        orders = _table(
            session,
            ns["silver"],
            "orders",
            model_kind="fact",
            row_count=3,
            size_bytes=300,
            last_write_at=_T0 + timedelta(days=3),
            last_read_at=self.last_read,
            owner_id=self.alice.id,
            created_at=_T0,
            **_health("healthy"),
        )
        gold_orders = _table(
            session,
            ns["gold"],
            "orders",
            row_count=10,
            size_bytes=1000,
            last_write_at=_T0 + timedelta(days=3),
            last_read_at=_T0,
            owner_id=loader.id,
            created_at=_T0,
            unpublished_run_id=uuid.uuid4(),
            **_health("blocked_late"),
        )
        _table(
            session,
            ns["silver"],
            "secret",
            row_count=10,
            size_bytes=50,
            last_write_at=_T0 + timedelta(days=1),
            last_read_at=_T0 + timedelta(days=1),
            created_at=_T0 + timedelta(days=1),
            **_health("check_failed"),
        )
        _table(
            session,
            ns["gold"],
            "a",
            size_bytes=0,
            owner_id=self.admin.id,
            created_at=_T0 + timedelta(days=2),
            **_health(None),
        )
        _table(
            session,
            ns["gold.sales"],
            "b",
            row_count=5,
            size_bytes=500,
            last_write_at=_T0 + timedelta(days=2),
            last_read_at=_T0 + timedelta(days=2),
            owner_id=self.admin.id,
            created_at=_T0 + timedelta(days=3),
            **_health("late"),
        )
        _table(
            session,
            ns["golden"],
            "c",
            row_count=7,
            size_bytes=700,
            last_write_at=_T0 + timedelta(days=4),
            last_read_at=_T0 + timedelta(days=4),
            owner_id=self.admin.id,
            created_at=_T0 + timedelta(days=4),
            kept_run_id=uuid.uuid4(),
            **_health("blocked_rows_dropped"),
        )
        _table(
            session,
            ns["gold_x"],
            "d",
            row_count=1,
            size_bytes=100,
            last_write_at=_T0 + timedelta(days=5),
            owner_id=self.admin.id,
            created_at=_T0 + timedelta(days=5),
            **managed,
            **_health(None),
        )
        _table(session, ns["silver"], "v", table_type=TableType.VIEW, **_health("not_judged"))
        _table(session, ns["raw"], "e", table_type=TableType.EXTERNAL, **_health("not_judged"))
        prod_gold, prod_silver = (
            _namespace(session, prod, "gold"),
            _namespace(session, prod, "silver"),
        )
        _table(
            session,
            prod_gold,
            "sales",
            row_count=20,
            size_bytes=2000,
            last_write_at=_T0 + timedelta(days=6),
            last_read_at=_T0 + timedelta(days=6),
            owner_id=self.admin.id,
            created_at=_T0 + timedelta(days=6),
            **_health("check_failed"),
        )
        _table(
            session,
            prod_silver,
            "orders",
            row_count=3,
            size_bytes=300,
            last_write_at=_T0 + timedelta(days=6),
            last_read_at=_T0 + timedelta(days=6),
            owner_id=self.admin.id,
            created_at=_T0 + timedelta(days=6),
            **_health(None),
        )
        session.add(
            TableColumn(
                id=uuid.uuid4(), table_id=orders.id, name="store_id", type="integer", position=0
            )
        )
        session.add(
            ColumnDescription(
                id=uuid.uuid4(),
                table_id=orders.id,
                column="store_id",
                description_project="joins dim_store",
            )
        )
        session.commit()
        self.admin_count = 11
        self.orders_id = orders.id
        now = datetime.now(UTC)
        table_read(session, bob, orders.id, now)
        table_read(session, carl, orders.id, now - timedelta(days=6))
        table_read(session, dave, orders.id, now - timedelta(days=10))
        repo, version = deployed_job(session, "dev", "shop", "daily")
        run = run_of(session, repo, version)
        check_result(
            session,
            run,
            orders.id,
            name="silver.orders/ref_store_id",
            outcome=CheckOutcome.FAILED,
            finished_at=now,
            generated=True,
        )
        check_result(
            session,
            run,
            gold_orders.id,
            name="gold.orders/ref_store_id",
            outcome=CheckOutcome.FAILED,
            finished_at=now,
            generated=True,
        )
        grants = GrantService(session, AllowAllAuthorization())
        grants.grant(self.admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
        grants.grant(
            self.admin,
            principal="alice",
            role=Role.VIEWER,
            kind=SecurableKind.TABLE,
            environment="dev",
            namespace="silver",
            table="orders",
        )
        grants.grant(
            self.admin,
            principal="alice",
            role=Role.VIEWER,
            kind=SecurableKind.NAMESPACE,
            environment="prod",
            namespace="gold",
        )
        grants.grant(
            self.admin,
            principal="tim",
            role=Role.VIEWER,
            kind=SecurableKind.TABLE,
            environment="dev",
            namespace="gold",
            table="orders",
        )
        self.listing = TableListing(session, GrantAuthorization(session))


@pytest.fixture
def world(session: Session) -> ListingWorld:
    """Provide the listing's world.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    ListingWorld
        The world.
    """
    return ListingWorld(session)


def _count_statements(engine: Engine, call: Callable[[], object]) -> int:
    """Count the statements a call sends.

    Parameters
    ----------
    engine
        The engine the call uses.
    call
        What to run.

    Returns
    -------
    int
        The statements it sent.
    """
    return len(_statements(engine, call))


def _statements(engine: Engine, call: Callable[[], object]) -> list[str]:
    """Record the statements a call sends.

    Parameters
    ----------
    engine
        The engine the call uses.
    call
        What to run.

    Returns
    -------
    list of str
        The statements it sent, in order.
    """
    statements: list[str] = []

    def _count(conn: object, cursor: object, statement: str, *args: object) -> None:
        """Record one statement.

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

    event.listen(engine, "before_cursor_execute", _count)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    return statements


def _page_through(listing: TableListing, p: PrincipalRef, **filters: object) -> list[uuid.UUID]:
    """Read a list one item a page, following each page's cursor.

    Parameters
    ----------
    listing
        The listing.
    p
        The caller.
    **filters
        The filters and sort, sent with every page.

    Returns
    -------
    list of uuid.UUID
        The ids in the order the pages gave them.
    """
    seen: list[uuid.UUID] = []
    cursor: str | None = None
    while True:
        page = listing.list(p, limit=1, cursor=cursor, **cast("dict[str, Any]", filters))
        seen.extend(i.id for i in page.items)
        if page.next_cursor is None:
            return seen
        cursor = page.next_cursor


def _names(page: TableListPage) -> list[str]:
    """Name the items of a page in order.

    Parameters
    ----------
    page
        The page.

    Returns
    -------
    list of str
        ``env:namespace.name`` of each item.
    """
    return [f"{i.environment}:{i.namespace}.{i.name}" for i in page.items]


def _item(page: TableListPage, name: str) -> TableListItem:
    """Find the item `_names` calls `name`.

    Parameters
    ----------
    page
        The page.
    name
        ``env:namespace.name``.

    Returns
    -------
    TableListItem
        The item.
    """
    return page.items[_names(page).index(name)]


def test_a_reader_sees_only_what_they_may_read_across_environments(world: ListingWorld) -> None:
    page = world.listing.list(world.alice)
    assert _names(page) == ["dev:silver.orders", "prod:gold.sales"]  # name ascending
    assert page.next_cursor is None and not page.capped


def test_equal_names_order_by_namespace_then_environment(world: ListingWorld) -> None:
    names = _names(world.listing.list(world.admin, limit=200))
    assert [n for n in names if n.endswith(".orders")] == [
        "dev:gold.orders",
        "dev:silver.orders",
        "prod:silver.orders",
    ]


def test_the_namespace_filter_covers_beneath_and_not_siblings(world: ListingWorld) -> None:
    page = world.listing.list(world.admin, environment="dev", namespace="gold")
    # by table name; never golden or gold_x
    assert _names(page) == ["dev:gold.a", "dev:gold.sales.b", "dev:gold.orders"]


@pytest.mark.parametrize("sort", list(TableSort))
@pytest.mark.parametrize("order", list(SortOrder))
def test_paging_one_by_one_neither_repeats_nor_skips_across_nulls(
    world: ListingWorld, sort: TableSort, order: SortOrder
) -> None:
    seen = _page_through(world.listing, world.admin, sort=sort, order=order)
    whole = [i.id for i in world.listing.list(world.admin, sort=sort, order=order, limit=200).items]
    assert seen == whole
    assert len(set(seen)) == len(whole) == world.admin_count


def test_rows_sort_descending_by_default_and_nulls_last(world: ListingWorld) -> None:
    rows = [
        i.row_count for i in world.listing.list(world.admin, sort=TableSort.ROWS, limit=200).items
    ]
    known = [r for r in rows if r is not None]
    assert known == sorted(known, reverse=True)
    assert rows[len(known) :] == [None] * (len(rows) - len(known)) == [None] * 3


def test_every_sort_has_its_default_order() -> None:
    ascending = {TableSort.NAME, TableSort.OWNER, TableSort.HEALTH}
    assert {s for s, o in DEFAULT_ORDER.items() if o is SortOrder.ASC} == ascending
    assert set(DEFAULT_ORDER) == set(TableSort)


def test_owner_sorts_by_name_with_no_owner_last(world: ListingWorld) -> None:
    items = world.listing.list(world.admin, sort=TableSort.OWNER, limit=200).items
    owners = [i.owner.name if i.owner else None for i in items]
    assert owners == ["admin"] * 6 + ["alice", "loader", None, None, None]
    assert _item(TableListPage(items, None, False), "dev:gold.orders").owner == TableOwner(
        "loader", "SERVICE_ACCOUNT"
    )


def test_health_sorts_worst_first_by_the_stored_word(world: ListingWorld) -> None:
    items = world.listing.list(world.admin, sort=TableSort.HEALTH, limit=200).items
    stored = {r[0]: r[1] for r in world.session.execute(select(Table.id, Table.health_rank))}
    ranks = [stored[i.id] for i in items]
    assert ranks == sorted(ranks)
    # dev:silver.orders is stored healthy but shown "check_failed" to admin; it sorts by its
    # stored word, after gold.sales.b's "late", so the shown words are out of order here.
    shown = [HEALTH_ITEMS.index(i.health_summary.item) for i in items]
    assert shown != sorted(shown)
    names = _names(TableListPage(items, None, False))
    assert names.index("dev:gold.sales.b") < names.index("dev:silver.orders")


def test_a_table_without_a_snapshot_shows_no_size(world: ListingWorld) -> None:
    item = _item(world.listing.list(world.admin, limit=200), "dev:gold.a")  # no snapshot, size 0
    assert (item.size_bytes, item.last_write_at, item.row_count) == (None, None, None)
    sizes = [
        i.size_bytes for i in world.listing.list(world.admin, sort=TableSort.SIZE, limit=200).items
    ]
    # Sorted by the stored 0, so ahead of the views' null, and shown as null.
    assert sizes[-3:] == [None, None, None]


def test_an_item_carries_its_kind_state_and_stored_keys(world: ListingWorld) -> None:
    page = world.listing.list(world.admin, limit=200)
    orders = _item(page, "dev:silver.orders")
    assert (orders.kind, orders.model_kind, orders.state) == ("TABLE", "fact", "published")
    assert (orders.row_count, orders.size_bytes) == (3, 300)
    assert orders.last_write_at == _T0 + timedelta(days=3)
    assert orders.created_at == _T0
    assert orders.match is None
    assert _item(page, "dev:gold.orders").state == "not_published"
    assert _item(page, "dev:golden.c").state == "kept"
    view = _item(page, "dev:silver.v")
    assert (view.kind, view.state, view.health_summary.item) == ("VIEW", "published", "not_judged")
    assert _item(page, "dev:raw.e").kind == "EXTERNAL"


def test_q_matches_a_column_description_and_carries_the_matching_part(world: ListingWorld) -> None:
    item = _item(world.listing.list(world.admin, q="dim_store"), "dev:silver.orders")
    assert item.match == TableMatch(
        "column", "description", "store_id", "integer", "joins dim_store", "en", False
    )


def test_q_matches_a_table_name_and_keeps_the_sort(world: ListingWorld) -> None:
    page = world.listing.list(world.admin, q="orders", sort=TableSort.LAST_READ)
    assert _names(page) == ["dev:silver.orders", "prod:silver.orders", "dev:gold.orders"]
    assert page.items[0].match == TableMatch("table", "name", None, None, None, None, False)


def test_q_finds_a_translated_text_only_in_the_readers_language(world: ListingWorld) -> None:
    world.session.add(
        TextTranslation(
            id=uuid.uuid4(),
            table_id=world.orders_id,
            subject="table",
            name="",
            language="de",
            field="label",
            text_project="Bestellungen",
        )
    )
    world.session.commit()
    assert world.listing.list(world.admin, q="bestell").items == []
    item = world.listing.list(world.admin, q="bestell", languages=("de",)).items[0]
    assert item.match == TableMatch("table", "label", None, None, "Bestellungen", "de", True)


def test_q_with_model_kind_still_finds_a_table_by_its_column(world: ListingWorld) -> None:
    # silver.orders declares model kind "fact"
    page = world.listing.list(world.admin, q="store_id", model_kind="fact")
    assert _names(page) == ["dev:silver.orders"]


def test_model_kind_keeps_only_tables_of_that_kind(world: ListingWorld) -> None:
    assert _names(world.listing.list(world.admin, model_kind="fact")) == ["dev:silver.orders"]


def test_q_never_finds_a_table_by_the_text_of_one_the_caller_may_not_read(
    world: ListingWorld,
) -> None:
    assert world.listing.list(world.alice, q="secret").items == []


def test_q_takes_percent_and_underscore_literally(session: Session) -> None:
    admin = person(session, "admin")
    GrantService(session, AllowAllAuthorization()).grant(
        admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    silver = _namespace(session, environment(session, "dev"), "silver")
    _table(session, silver, "orders")
    _table(session, silver, "order_lines")
    session.commit()
    listing = TableListing(session, GrantAuthorization(session))
    assert _names(listing.list(admin, q="_")) == ["dev:silver.order_lines"]
    assert listing.list(admin, q="%").items == []


def _admin_listing(
    session: Session, namespaces: dict[str, str]
) -> tuple[PrincipalRef, TableListing]:
    """Build `dev` with one table in each namespace, and an administrator.

    Parameters
    ----------
    session
        Database session.
    namespaces
        Dotted namespace → the name of its one table.

    Returns
    -------
    tuple
        The administrator and the listing.
    """
    admin = person(session, "admin")
    GrantService(session, AllowAllAuthorization()).grant(
        admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    dev = environment(session, "dev")
    for dotted, name in namespaces.items():
        _table(session, _namespace(session, dev, dotted), name)
    session.commit()
    return admin, TableListing(session, GrantAuthorization(session))


def test_the_namespace_filter_is_case_sensitive_as_on_postgres(session: Session) -> None:
    admin, listing = _admin_listing(
        session, {"gold": "a", "gold.sales": "b", "Gold": "c", "Gold.sales": "d", "GOLD.x": "e"}
    )
    assert _names(listing.list(admin, namespace="gold")) == ["dev:gold.a", "dev:gold.sales.b"]
    assert _names(listing.list(admin, namespace="Gold")) == ["dev:Gold.c", "dev:Gold.sales.d"]
    assert listing.list(admin, namespace="gOLD").items == []


@pytest.mark.parametrize(
    ("namespace", "found"),
    [("a*", "star"), ("a?", "mark"), ("[a]", "bracket"), ("a", "plain"), ("a[", "open")],
)
def test_the_namespace_filter_takes_pattern_characters_literally(
    session: Session, namespace: str, found: str
) -> None:
    admin, listing = _admin_listing(
        session,
        {
            "a*.n": "star",
            "a?.n": "mark",
            "[a].n": "bracket",
            "a.n": "plain",
            "a[.n": "open",
            "ab.n": "other",
            "a%.n": "percent",
        },
    )
    assert [item.name for item in listing.list(admin, namespace=namespace).items] == [found]


def test_a_legacy_dotted_namespace_stays_listable(session: Session) -> None:
    admin = person(session, "admin")
    GrantService(session, AllowAllAuthorization()).grant(
        admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    dev = environment(session, "dev")
    legacy = Namespace(id=uuid.uuid4(), environment_id=dev, path="le.gacy")
    # The dot sits in the first of two components.
    beneath = Namespace(
        id=uuid.uuid4(), environment_id=dev, path=f"le.gacy{NAMESPACE_SEPARATOR}sub"
    )
    session.add_all([legacy, beneath])
    session.flush()
    _table(session, legacy.id, "t")
    _table(session, beneath.id, "u")
    session.commit()
    listing = TableListing(session, GrantAuthorization(session))
    both = ["dev:le.gacy.t", "dev:le.gacy.sub.u"]
    assert _names(listing.list(admin, namespace="le.gacy")) == both
    assert _names(listing.list(admin, namespace="le.gacy.sub")) == ["dev:le.gacy.sub.u"]
    # A filter matches the dotted path as it is shown, at a dot.
    assert _names(listing.list(admin, namespace="le")) == both
    assert listing.list(admin, namespace="le.g").items == []
    tree = listing.tree(admin)
    assert [(n.path, n.count) for n in tree.environments[0].namespaces] == [
        ("le.gacy", 2),
        ("le.gacy.sub", 1),
    ]


def test_capped_counts_only_readable_matches(
    world: ListingWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search, "MAX_SCANNED", 1)
    page = world.listing.list(world.admin, q="o")
    assert page.capped is True
    assert len(page.items) == 1
    assert world.listing.list(world.alice, q="secret").capped is False  # unreadable never count
    # `orders` also matches dev:gold.orders and prod:silver.orders, which alice may not read:
    # counting them would cap her one readable match.
    page = world.listing.list(world.alice, q="orders")
    assert (_names(page), page.capped) == (["dev:silver.orders"], False)


def _wide_world(session: Session) -> tuple[PrincipalRef, PrincipalRef]:
    """Build `dev.silver` with `wide`, five columns starting with ``c``, and `zc`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of PrincipalRef
        An administrator, and a reader of `zc` alone.
    """
    admin, reader = person(session, "admin"), person(session, "reader")
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    silver = _namespace(session, environment(session, "dev"), "silver")
    wide = _table(session, silver, "wide")
    _table(session, silver, "zc")
    for m in range(5):
        session.add(
            TableColumn(id=uuid.uuid4(), table_id=wide.id, name=f"c{m}", type="int", position=m)
        )
    session.commit()
    grants.grant(
        admin,
        principal="reader",
        role=Role.VIEWER,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace="silver",
        table="zc",
    )
    return admin, reader


def test_a_search_stops_after_max_scanned_candidates_and_says_so(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # two tables, but six candidates: `wide`'s five columns (prefix) before `zc`'s name
    # (substring); the walk stops after three, so `zc` is never reached
    admin, _ = _wide_world(session)
    monkeypatch.setattr(search, "MAX_SCANNED", 3)
    page = TableListing(session, GrantAuthorization(session)).list(admin, q="c")
    assert (_names(page), page.capped) == (["dev:silver.wide"], True)


@pytest.mark.parametrize("scanned", [1, 4])
def test_a_cut_that_leaves_only_texts_of_listed_tables_is_not_capped(
    session: Session, monkeypatch: pytest.MonkeyPatch, scanned: int
) -> None:
    # `wide` alone matches, by its five columns; with 4 scanned the fifth candidate, the first
    # one left, is `wide`'s last column: the walk is cut, yet no further table matched
    admin, _ = _wide_world(session)
    session.execute(delete(Table).where(Table.name == "zc"))
    session.commit()
    monkeypatch.setattr(search, "MAX_SCANNED", scanned)
    page = TableListing(session, GrantAuthorization(session)).list(admin, q="c")
    assert (_names(page), page.capped) == (["dev:silver.wide"], False)


def test_a_search_cut_with_a_further_table_beyond_texts_of_listed_ones_is_capped(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the candidate after the cut is `wide`'s last column; `zc` comes after it
    admin, _ = _wide_world(session)
    monkeypatch.setattr(search, "MAX_SCANNED", 4)
    page = TableListing(session, GrantAuthorization(session)).list(admin, q="c")
    assert (_names(page), page.capped) == (["dev:silver.wide"], True)


def test_unreadable_candidates_never_use_up_the_scan(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `wide`'s five matching columns come before `zc` in the ranked union; the reader may
    # not read `wide`, so they must neither fill the scan nor set `capped`
    _, reader = _wide_world(session)
    monkeypatch.setattr(search, "MAX_SCANNED", 1)
    page = TableListing(session, GrantAuthorization(session)).list(reader, q="c")
    assert (_names(page), page.capped) == (["dev:silver.zc"], False)


def test_a_missing_environment_is_404_only_for_a_reader_of_environments(
    world: ListingWorld,
) -> None:
    with pytest.raises(NotFoundError, match="lakana env list"):
        world.listing.list(world.admin, environment="nope")
    assert world.listing.list(world.tim, environment="nope").items == []


def test_an_unknown_namespace_gives_an_empty_page(world: ListingWorld) -> None:
    assert world.listing.list(world.admin, namespace="nope").items == []


def test_the_scan_reads_on_past_a_chunk_of_unreadable_rows(
    world: ListingWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(table_listing, "SCAN_CHUNK", 2)
    assert _names(world.listing.list(world.alice)) == ["dev:silver.orders", "prod:gold.sales"]
    assert len(_page_through(world.listing, world.admin, sort=TableSort.ROWS)) == 11


def test_a_cursor_past_the_last_readable_table_gives_an_empty_page(world: ListingWorld) -> None:
    cursor = world.listing.list(world.alice, limit=1).next_cursor
    world.session.execute(delete(Table).where(Table.name == "sales"))
    world.session.commit()
    page = world.listing.list(world.alice, limit=1, cursor=cursor)
    assert (page.items, page.next_cursor) == ([], None)


def test_a_table_found_by_its_name_and_its_column_is_one_item(world: ListingWorld) -> None:
    # "o" is in `orders` and in its column `store_id`
    names = _names(world.listing.list(world.admin, q="o", limit=200))
    assert names.count("dev:silver.orders") == 1
    assert len(names) == len(set(names))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"limit": 0}, "limit must be between 1 and 200"),
        ({"limit": MAX_LIMIT + 1}, "limit must be between 1 and 200"),
        ({"q": "  "}, "between 1 and 200 characters"),
        ({"model_kind": "cube"}, "cube"),
        ({"cursor": "!"}, "cursor is not valid"),
    ],
)
def test_the_parameters_are_validated(
    world: ListingWorld, kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        world.listing.list(world.admin, **cast("dict[str, Any]", kwargs))


def test_a_cursor_of_another_sort_is_refused(world: ListingWorld) -> None:
    cursor = world.listing.list(world.admin, limit=1).next_cursor
    with pytest.raises(ValidationError, match=r"sorted by `rows:desc`.*sorted by `name:asc`"):
        world.listing.list(world.admin, sort=TableSort.ROWS, cursor=cursor)


def test_a_task_token_lists_only_its_declared_assets(world: ListingWorld) -> None:
    token = replace(
        world.admin,
        task_scope=TaskScope(environment="dev", asset_scope=["silver.*"], token_id=uuid.uuid4()),
    )
    names = _names(world.listing.list(token, limit=200))
    assert {n.split(":")[1].split(".")[0] for n in names} == {"silver"}


def test_readers_7d_counts_principals_never_names_and_equals_the_readers_window(
    world: ListingWorld,
) -> None:
    item = _item(world.listing.list(world.alice), "dev:silver.orders")
    now = db_now(world.session)
    assert item.readers_7d == len(readers(world.session, item.id, days=7, now=now)) == 2
    assert item.last_read_at == world.last_read


def test_a_hidden_reference_check_is_not_in_a_readers_word(world: ListingWorld) -> None:
    # stored key "healthy"; a failed ref_ check of silver.orders whose target alice may not read
    alice_sees = _item(world.listing.list(world.alice), "dev:silver.orders").health_summary
    assert alice_sees.item == "healthy"
    admin_sees = _item(world.listing.list(world.admin, limit=200), "dev:silver.orders")
    assert admin_sees.health_summary.item == "check_failed"
    # gold.orders keeps its worse stored word; a visible failing check is counted beside it
    admin_gold = _item(world.listing.list(world.admin, limit=200), "dev:gold.orders").health_summary
    assert (admin_gold.item, admin_gold.others) == ("blocked_late", 1)
    tim_gold = _item(world.listing.list(world.tim), "dev:gold.orders").health_summary
    assert (tim_gold.item, tim_gold.others) == ("blocked_late", 0)


def test_a_passed_reference_check_makes_a_not_judged_table_healthy_for_who_may_see_it(
    session: Session,
) -> None:
    admin, reader = person(session, "admin"), person(session, "reader")
    silver = _namespace(session, environment(session, "dev"), "silver")
    only_ref = _table(session, silver, "facts", **_health("not_judged"))
    view = _table(session, silver, "v", table_type=TableType.VIEW, **_health("not_judged"))
    unset = _table(session, silver, "unset", **_health(None))
    repo, version = deployed_job(session, "dev", "shop", "daily")
    run = run_of(session, repo, version)
    for row in (only_ref, view, unset):
        check_result(
            session,
            run,
            row.id,
            name=f"silver.{row.name}/ref_store_id",
            outcome=CheckOutcome.PASSED,
            finished_at=datetime.now(UTC),
            generated=True,
        )
    session.commit()
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    grants.grant(
        admin,
        principal="reader",
        role=Role.VIEWER,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace="silver",
        table="facts",
    )
    listing = TableListing(session, GrantAuthorization(session))
    words = {item.name: item.health_summary for item in listing.list(admin).items}
    assert words["facts"] == HealthSummary("healthy", "healthy", "Healthy", 0)
    # a view is never judged; a word not computed yet may hide a finding
    assert (words["v"].item, words["unset"].item) == ("not_judged", "not_judged")
    # the check's target cannot be told, so a reader of the table alone may not see it
    [item] = listing.list(reader).items
    assert item.health_summary.item == "not_judged"


def test_listing_writes_no_audit_event(world: ListingWorld) -> None:
    before = world.session.scalar(select(func.count(AuditEvent.id)))
    world.listing.list(world.alice)
    world.listing.tree(world.alice)
    assert world.session.scalar(select(func.count(AuditEvent.id))) == before


def test_the_tree_counts_equal_the_list_for_each_node(world: ListingWorld) -> None:
    for who in (world.alice, world.admin, world.tim):
        tree = world.listing.tree(who)
        assert tree.count == len(world.listing.list(who, limit=200).items)
        for env in tree.environments:
            assert env.count == len(world.listing.list(who, environment=env.name, limit=200).items)
            for ns in env.namespaces:
                listed = world.listing.list(who, environment=env.name, namespace=ns.path, limit=200)
                assert ns.count == len(listed.items)


def test_the_tree_orders_environments_and_namespaces_by_name(world: ListingWorld) -> None:
    tree = world.listing.tree(world.admin)
    assert tree.count == world.admin_count
    assert [e.name for e in tree.environments] == ["dev", "prod"]
    assert [(n.path, n.count) for n in tree.environments[0].namespaces] == [
        ("empty", 0),
        ("gold", 3),
        ("gold.sales", 1),
        ("gold_x", 1),
        ("golden", 1),
        ("raw", 1),
        ("silver", 3),
    ]


def test_the_tree_shows_empty_namespaces_only_to_a_reader_of_namespaces(
    world: ListingWorld,
) -> None:
    # dev namespace `empty` holds no table
    admin_tree = world.listing.tree(world.admin)
    assert "empty" in [
        n.path for e in admin_tree.environments if e.name == "dev" for n in e.namespaces
    ]
    alice_tree = world.listing.tree(world.alice)
    assert "empty" not in [n.path for e in alice_tree.environments for n in e.namespaces]
    assert [e.name for e in world.listing.tree(world.tim).environments] == ["dev"]


def test_the_statements_do_not_grow_with_the_page(session: Session) -> None:
    # its own world: 12 readable tables, 6 in each of dev and prod, each with a failing
    # reference check of one run per environment, so each page needs one check filter per
    # environment and one manifest per run, however many of its rows hold such a check
    admin = person(session, "admin")
    GrantService(session, AllowAllAuthorization()).grant(
        admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    now = datetime.now(UTC)
    for env in ("dev", "prod"):
        silver = _namespace(session, environment(session, env), "silver")
        tables = [_table(session, silver, f"t{n}", owner_id=admin.id) for n in range(6)]
        session.commit()
        repo, version = deployed_job(session, env, f"shop-{env}", "daily")
        run = run_of(session, repo, version)
        for table in tables:
            check_result(
                session,
                run,
                table.id,
                name=f"silver.{table.name}/ref_store_id",
                outcome=CheckOutcome.FAILED,
                finished_at=now,
                generated=True,
            )
    session.commit()

    def fresh() -> TableListing:
        """Build a listing whose authorization has cached nothing yet.

        Returns
        -------
        TableListing
            The listing.
        """
        return TableListing(session, GrantAuthorization(session))

    engine = session.get_bind()
    assert isinstance(engine, Engine)
    small = _count_statements(engine, lambda: fresh().list(admin, limit=2))
    large = _count_statements(engine, lambda: fresh().list(admin, limit=12))
    assert large == small
    words = {i.health_summary.item for i in fresh().list(admin, limit=12).items}
    assert words == {"check_failed"}


def test_a_search_reads_the_ranked_matches_once_however_many_there_are(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # chunks of 2 rows: 3 tables of 3 matching columns are 12 ranked rows, 12 tables are 48
    monkeypatch.setattr(search, "CHUNK", 2)
    admin = person(session, "admin")
    GrantService(session, AllowAllAuthorization()).grant(
        admin, principal="admin", role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    silver = _namespace(session, environment(session, "dev"), "silver")

    def add_tables(first: int, count: int) -> None:
        """Add tables named ``cN``, each with three columns ``cN_M``.

        Parameters
        ----------
        first
            The first N.
        count
            How many.
        """
        for n in range(first, first + count):
            table = _table(session, silver, f"c{n}")
            for m in range(3):
                session.add(
                    TableColumn(
                        id=uuid.uuid4(), table_id=table.id, name=f"c{n}_{m}", type="int", position=m
                    )
                )
        session.commit()

    def listed() -> list[str]:
        """List the tables matching ``c``, as a fresh request would.

        Returns
        -------
        list of str
            Their names.
        """
        return _names(TableListing(session, GrantAuthorization(session)).list(admin, q="c"))

    engine = session.get_bind()
    assert isinstance(engine, Engine)
    add_tables(0, 3)
    small = _statements(engine, listed)
    add_tables(3, 9)
    large = _statements(engine, listed)
    assert len(large) == len(small)
    assert sum("ORDER BY hits.rank" in s for s in large) == 1
    assert len(listed()) == 12


@pytest.mark.parametrize(
    ("sort", "value"),
    [
        (TableSort.HEALTH, 2**15),
        (TableSort.HEALTH, -(2**15) - 1),
        (TableSort.ROWS, 2**63),
        (TableSort.ROWS, -(2**63) - 1),
        (TableSort.SIZE, 2**63),
        (TableSort.SIZE, -(2**63) - 1),
    ],
)
def test_a_forged_integer_cursor_outside_its_column_is_refused(
    world: ListingWorld, sort: TableSort, value: int
) -> None:
    sort_name = f"{sort.value}:{DEFAULT_ORDER[sort].value}"
    cursor = encode_sorted_cursor("tables", sort_name, (value,), uuid.uuid4())
    with pytest.raises(ValidationError, match="cursor is not valid"):
        world.listing.list(world.admin, sort=sort, cursor=cursor)


@pytest.mark.parametrize(
    ("sort", "value"),
    [
        (TableSort.HEALTH, 2**15 - 1),
        (TableSort.HEALTH, -(2**15)),
        (TableSort.ROWS, 2**63 - 1),
        (TableSort.SIZE, -(2**63)),
    ],
)
def test_an_integer_cursor_at_the_edge_of_its_column_is_read(
    world: ListingWorld, sort: TableSort, value: int
) -> None:
    sort_name = f"{sort.value}:{DEFAULT_ORDER[sort].value}"
    cursor = encode_sorted_cursor("tables", sort_name, (value,), uuid.uuid4())
    assert world.listing.list(world.admin, sort=sort, cursor=cursor).next_cursor is None
