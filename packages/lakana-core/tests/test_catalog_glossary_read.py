"""Reading glossary terms: languages, related terms, paging and which links a reader sees (#375)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from core_test_helpers import _config
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog.glossary import write_glossary
from lakana_core.catalog.glossary_read import GlossaryService, TermRef, table_terms
from lakana_core.catalog.identity import resolve_environment_id
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    DeclaredMeasure,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Table,
)
from lakana_core.pagination import encode_cursor
from lakana_core.storage.resolver import StorageResolver

_GOLD = NamespacePath.from_parts(["gold"])

_GLOSSARY: dict[str, Any] = {
    "version": 13,
    "main_language": "en",
    "glossary": {
        "gross_revenue": {
            "name": "Gross revenue",
            "definition": "Before returns.",
            "owner": "finance",
            "related": ["net_revenue"],
        },
        "net_revenue": {
            "name": "Net revenue",
            "definition": "Revenue after returns.",
            "owner": "finance",
            "synonyms": ["revenue", "sales"],
            "links": {
                "gold.fact_sales": ["net_amount", "date", "gone"],
                "gold.payroll": ["missing_too"],
                "gold.dropped": [],
            },
            "related": ["gross_revenue", "margin"],
            "translations": {"fr": {"name": "CA net", "synonyms": ["ventes"]}},
        },
    },
}
"""The manifest the fixture deploys: every kind of link, a related id that names no term."""


def _schema(*names: str) -> Schema:
    """Build a schema of one long column per name.

    Parameters
    ----------
    *names
        The column names.

    Returns
    -------
    Schema
        The schema.
    """
    return Schema(*(NestedField(i + 1, n, LongType(), required=False) for i, n in enumerate(names)))


def _person(session: Session, name: str) -> PrincipalRef:
    """Persist a user.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    PrincipalRef
        A reference to the row.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=PrincipalType.USER, name=name, is_active=True))
    session.commit()
    return ref


class World:
    """The fixture's environment, readers and glossary service.

    Attributes
    ----------
    session
        Database session.
    env_id
        The id of `dev`.
    bob
        VIEWER on `dev`: reads every table.
    alice
        `READ_ENVIRONMENT` on `dev` and VIEWER on `gold.fact_sales` only.
    carol
        `READ_TABLE` on `gold.fact_sales`, without `READ_ENVIRONMENT`.
    dave
        `READ_ENVIRONMENT` on `dev` and no table.
    service
        The glossary service under the real grants.
    """

    def __init__(self, session: Session, admin: PrincipalRef, env_id: uuid.UUID) -> None:
        """Create the readers and their grants.

        Parameters
        ----------
        session
            Database session.
        admin
            The principal that grants.
        env_id
            The id of `dev`.
        """
        self.session = session
        self.env_id = env_id
        self.bob, self.alice, self.carol, self.dave = (
            _person(session, n) for n in ("bob", "alice", "carol", "dave")
        )
        grants = GrantService(session, AllowAllAuthorization())
        grants.grant(
            admin,
            principal="bob",
            role=Role.VIEWER,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
        )
        for name in ("alice", "dave"):
            grants.grant(
                admin,
                principal=name,
                action=Action.READ_ENVIRONMENT,
                kind=SecurableKind.ENVIRONMENT,
                environment="dev",
            )
        for name, role, action in (
            ("alice", Role.VIEWER, None),
            ("carol", None, Action.READ_TABLE),
        ):
            grants.grant(
                admin,
                principal=name,
                role=role,
                action=action,
                kind=SecurableKind.TABLE,
                environment="dev",
                namespace="gold",
                table="fact_sales",
            )
        self.service = GlossaryService(session, GrantAuthorization(session))


@pytest.fixture
def world(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> World:
    """Provide `dev` with `gold.fact_sales` and `gold.payroll`, the glossary and four readers.

    Parameters
    ----------
    session
        Database session.
    catalog
        An allow-all catalog.
    principal
        The principal that builds the tables.
    tmp_path
        The storage root.

    Returns
    -------
    World
        The fixture.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_table(
        principal, TableRef("dev", _GOLD, "fact_sales"), schema=_schema("date", "net_amount")
    )
    catalog.create_table(principal, TableRef("dev", _GOLD, "payroll"), schema=_schema("salary"))
    env_id = resolve_environment_id(session, "dev")
    assert env_id is not None
    sales = session.scalars(select(Table).where(Table.name == "fact_sales")).one()
    session.add(
        DeclaredMeasure(
            id=uuid.uuid4(),
            environment_id=env_id,
            table_id=sales.id,
            column="net_amount",
            aggregation="sum",
            additivity="additive",
            not_over=[],
        )
    )
    shop = Repository(id=uuid.uuid4(), name="shop", kind=RepositoryKind.HTTPS, url="https://h/s")
    session.add(shop)
    session.flush()
    write_glossary(session, env_id, shop.id, _GLOSSARY)
    session.commit()
    return World(session, principal, env_id)


def _deploy_hr(
    world: World, key: str, links: dict[str, list[str]], translations: dict[str, Any] | None = None
) -> None:
    """Deploy a second repository, `hr`, with one term.

    Parameters
    ----------
    world
        The fixture.
    key
        The term id; its name, definition and owner are fixed.
    links
        The term's links in manifest form.
    translations
        The term's translations in manifest form, if any.
    """
    hr = Repository(id=uuid.uuid4(), name="hr", kind=RepositoryKind.HTTPS, url="https://h/hr")
    world.session.add(hr)
    world.session.flush()
    term = {"name": "Salary cost", "definition": "Pay.", "owner": "hr", "links": links}
    term["translations"] = translations or {}
    write_glossary(world.session, world.env_id, hr.id, {**_GLOSSARY, "glossary": {key: term}})
    world.session.commit()


def test_get_shows_a_term_with_links_related_and_related_by(world: World) -> None:
    term = world.service.get(world.bob, "dev", "net_revenue")
    assert (term.key, term.name, term.definition, term.owner, term.repository, term.language) == (
        "net_revenue",
        "Net revenue",
        "Revenue after returns.",
        "finance",
        "shop",
        "en",
    )
    assert isinstance(term.id, uuid.UUID)
    assert term.synonyms == ("revenue", "sales")
    assert [(lk.table, lk.column, lk.kind, lk.missing) for lk in term.links] == [
        ("gold.dropped", None, "table", True),
        ("gold.fact_sales", "net_amount", "measure", False),
        ("gold.fact_sales", "date", "column", False),
        ("gold.fact_sales", "gone", "column", True),
        ("gold.payroll", "missing_too", "column", True),
    ]
    assert [(r.key, r.name, r.missing) for r in term.related] == [
        ("gross_revenue", "Gross revenue", False),
        ("margin", None, True),
    ]
    assert term.related_by == ("gross_revenue",)


def test_a_link_the_reader_may_not_read_is_left_out_even_when_its_column_is_missing(
    world: World,
) -> None:
    term = world.service.get(world.alice, "dev", "net_revenue")
    assert [lk.table for lk in term.links] == [
        "gold.dropped",
        "gold.fact_sales",
        "gold.fact_sales",
        "gold.fact_sales",
    ]
    assert term.definition == "Revenue after returns."


def test_a_link_to_an_existing_table_needs_read_table_one_to_a_dropped_table_does_not(
    world: World,
) -> None:
    term = world.service.get(world.dave, "dev", "net_revenue")
    assert [(lk.table, lk.column, lk.kind, lk.missing) for lk in term.links] == [
        ("gold.dropped", None, "table", True)
    ]
    _deploy_hr(world, "headcount", {"gold.staff": ["people"]})
    term = world.service.get(world.dave, "dev", "headcount")
    assert [(lk.table, lk.column, lk.kind, lk.missing) for lk in term.links] == [
        ("gold.staff", "people", "column", True)
    ]


def test_a_term_whose_links_are_all_hidden_still_shows_its_definition(world: World) -> None:
    links = {"gold.payroll": [], "gold.fact_sales": ["nope"]}
    _deploy_hr(world, "salary_cost", links)
    term = world.service.get(world.dave, "dev", "salary_cost")
    assert (term.links, term.definition, term.repository) == ((), "Pay.", "hr")
    seen = world.service.get(world.bob, "dev", "salary_cost").links
    assert [(lk.table, lk.column, lk.kind, lk.missing) for lk in seen] == [
        ("gold.fact_sales", "nope", "column", True),
        ("gold.payroll", None, "table", False),
    ]


def test_a_reader_without_read_environment_is_refused(world: World) -> None:
    with pytest.raises(PermissionDenied):
        world.service.get(world.carol, "dev", "net_revenue")
    with pytest.raises(PermissionDenied):
        world.service.list(world.carol, "dev")


def test_the_reader_language_chooses_the_texts_and_falls_back_per_text(world: World) -> None:
    term = world.service.get(world.bob, "dev", "net_revenue", languages=("fr-CH",))
    assert (term.language, term.name, term.definition) == ("fr", "CA net", "Revenue after returns.")
    assert term.synonyms == ("revenue", "sales", "ventes")
    gross = world.service.get(world.bob, "dev", "gross_revenue", languages=("fr",))
    assert gross.language == "en"
    assert [(r.key, r.name) for r in gross.related] == [("net_revenue", "CA net")]
    assert world.service.get(world.bob, "dev", "net_revenue", languages=("en", "fr")).name == (
        "Net revenue"
    )


def test_a_missing_key_names_the_list_command(world: World) -> None:
    with pytest.raises(NotFoundError) as caught:
        world.service.get(world.bob, "dev", "nope")
    assert str(caught.value) == (
        "Term `nope` does not exist in environment `dev`. List the terms with: lakana term list "
        "--env dev"
    )


def test_list_sorts_by_key_filters_and_pages(world: World) -> None:
    first = world.service.list(world.bob, "dev", limit=1)
    assert [t.key for t in first.items] == ["gross_revenue"]
    assert first.next_cursor is not None
    second = world.service.list(world.bob, "dev", limit=1, cursor=first.next_cursor)
    assert [t.key for t in second.items] == ["net_revenue"]
    assert second.next_cursor is None
    assert world.service.list(world.bob, "dev", owner="sales").items == []
    assert [t.key for t in world.service.list(world.bob, "dev", owner="finance").items] == [
        "gross_revenue",
        "net_revenue",
    ]
    assert [t.key for t in world.service.list(world.bob, "dev", repository="shop").items] == [
        "gross_revenue",
        "net_revenue",
    ]
    assert world.service.list(world.bob, "dev", repository="other").items == []
    fr = world.service.list(world.bob, "dev", languages=("fr",)).items
    assert [t.name for t in fr] == ["Gross revenue", "CA net"]
    with pytest.raises(ValidationError):
        world.service.list(
            world.bob, "dev", cursor=encode_cursor("runs", datetime.now(UTC), uuid.uuid4())
        )
    with pytest.raises(ValidationError):
        world.service.list(world.bob, "dev", limit=0)


def test_table_terms_lists_the_table_and_column_terms_in_the_reader_language(
    world: World,
) -> None:
    env_id = resolve_environment_id(world.session, "dev")
    assert env_id is not None
    terms = table_terms(world.session, env_id, "gold.fact_sales", ("fr",), "en")
    assert terms.table == ()
    assert terms.columns == {
        "net_amount": (TermRef("net_revenue", "CA net"),),
        "date": (TermRef("net_revenue", "CA net"),),
        "gone": (TermRef("net_revenue", "CA net"),),
    }
    assert table_terms(world.session, env_id, "gold.dropped", (), "en").table == (
        TermRef("net_revenue", "Net revenue"),
    )
    assert table_terms(world.session, env_id, "gold.nothing", (), "en").columns == {}


def test_describe_table_carries_the_terms_only_with_read_environment(world: World) -> None:
    ref = TableRef("dev", _GOLD, "fact_sales")
    catalog = CatalogService(
        world.session, StorageResolver(world.session), GrantAuthorization(world.session)
    )
    described = catalog.describe_table(world.bob, ref, languages=("fr",))
    assert described.terms == ()
    assert described.column_terms["net_amount"] == (TermRef("net_revenue", "CA net"),)
    assert catalog.describe_table(world.carol, ref).column_terms == {}
    assert catalog.describe_table(world.carol, ref).terms == ()


def test_synonyms_come_from_every_reader_language_also_one_without_a_translated_name(
    world: World,
) -> None:
    _deploy_hr(
        world,
        "salary_cost",
        {},
        {
            "de": {"synonyms": ["Umsatz", "Pay"]},
            "fr": {"name": "Coût salarial", "synonyms": ["masse salariale", "UMSATZ"]},
            "es": {"name": "Coste salarial", "synonyms": ["nómina"]},
        },
    )
    only_synonyms = world.service.get(world.bob, "dev", "salary_cost", languages=("de",))
    assert (only_synonyms.language, only_synonyms.name) == ("en", "Salary cost")
    assert only_synonyms.synonyms == ("Umsatz", "Pay")
    both = world.service.get(world.bob, "dev", "salary_cost", languages=("es", "de", "fr"))
    assert (both.language, both.name) == ("es", "Coste salarial")
    assert both.synonyms == ("nómina", "Umsatz", "Pay", "masse salariale")
    main_first = world.service.get(world.bob, "dev", "salary_cost", languages=("en", "de"))
    assert main_first.synonyms == ()


_NEXT = "; list the terms with: lakana term list --env dev"
_UNSAFE = "contains a control, format or surrogate character (such as NUL), which no term has"


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            lambda s, p: s.get(p, "dev", "k" * 64),
            f"The term key has 64 characters, more than the 63 allowed. Check it{_NEXT}",
        ),
        (
            lambda s, p: s.get(p, "dev", "net\x00revenue"),
            f"The term key {_UNSAFE}. Send it without one{_NEXT}",
        ),
        (
            lambda s, p: s.list(p, "dev", repository="r" * 256),
            f"The repository filter has 256 characters, more than the 255 allowed. Check it{_NEXT}",
        ),
        (
            lambda s, p: s.list(p, "dev", owner="o" * 1001),
            f"The owner filter has 1001 characters, more than the 1000 allowed. Check it{_NEXT}",
        ),
        (
            lambda s, p: s.list(p, "dev", owner="fin\u202e"),
            f"The owner filter {_UNSAFE}. Send it without one{_NEXT}",
        ),
    ],
)
def test_a_key_or_filter_no_term_can_have_is_refused_before_the_database(
    world: World, call: Any, message: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        call(world.service, world.bob)
    assert str(caught.value) == message
