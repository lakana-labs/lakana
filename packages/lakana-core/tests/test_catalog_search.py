"""Search across tables, columns, descriptions, runs and principals, and what a caller may see."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest import mock

import pytest
from core_test_helpers import FakeColumnCatalog, _config
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog import search as search_module
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.search import CatalogSearch, SearchKind, SearchPage
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ColumnDescription,
    DeclaredMeasure,
    Environment,
    JobVersion,
    Namespace,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    Table,
    TableType,
    TextTranslation,
)
from lakana_core.policy.column_masks import ColumnMaskService

_SILVER = NamespacePath.from_parts(["silver"])
_GOLD = NamespacePath.from_parts(["gold"])


def _schema(*names: str) -> Schema:
    """Build a schema of one long column per name.

    Parameters
    ----------
    *names
        The column names.

    Returns
    -------
    Schema
        The schema; the second column is a string.
    """
    return Schema(
        *(
            NestedField(i + 1, n, LongType() if i == 0 else StringType(), required=False)
            for i, n in enumerate(names)
        )
    )


def _person(session: Session, name: str, kind: PrincipalType = PrincipalType.USER) -> PrincipalRef:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.
    kind
        Its type.

    Returns
    -------
    PrincipalRef
        A reference to the row.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=kind)
    session.add(Principal(id=ref.id, type=kind, name=name, is_active=True))
    session.commit()
    return ref


class World:
    """The fixture's catalog, callers and search services.

    Attributes
    ----------
    session
        Database session.
    catalog
        An allow-all catalog that built the tables.
    admin
        The principal that built them.
    alice
        May read `silver.orders` and nothing else.
    bob
        May read everything in `dev`.
    root
        Holds the ADMIN role on the instance.
    """

    def __init__(self, session: Session, catalog: CatalogService, admin: PrincipalRef) -> None:
        """Build the world.

        Parameters
        ----------
        session
            Database session.
        catalog
            An allow-all catalog with `dev`.
        admin
            The principal that builds the tables.
        """
        self.session = session
        self.catalog = catalog
        self.admin = admin
        self.alice = _person(session, "alice")
        self.bob = _person(session, "bob")
        self.root = _person(session, "root")
        grants = GrantService(session, AllowAllAuthorization())
        grants.grant(
            admin,
            principal="alice",
            role=Role.VIEWER,
            kind=SecurableKind.TABLE,
            environment="dev",
            namespace="silver",
            table="orders",
        )
        grants.grant(
            admin,
            principal="bob",
            role=Role.VIEWER,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
        )
        grants.grant(admin, principal="root", role=Role.ADMIN, kind=SecurableKind.INSTANCE)

    def search(
        self,
        who: PrincipalRef,
        q: str,
        *,
        kind: SearchKind | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchPage:
        """Search `dev` as a caller, under the real grants.

        Parameters
        ----------
        who
            The caller.
        q
            The query.
        kind
            Only this kind.
        limit
            The page size.
        offset
            How many to skip.

        Returns
        -------
        SearchPage
            The page.
        """
        return CatalogSearch(self.session, GrantAuthorization(self.session)).search(
            who, "dev", q, kind=kind, limit=limit, offset=offset
        )

    def names(self, who: PrincipalRef, q: str, *, kind: SearchKind | None = None) -> list[str]:
        """Search and list the display names of the hits in order.

        Parameters
        ----------
        who
            The caller.
        q
            The query.
        kind
            Only this kind.

        Returns
        -------
        list of str
            ``kind:name`` of each hit.
        """
        return [f"{h.kind}:{h.name}" for h in self.search(who, q, kind=kind).results]


@pytest.fixture
def world(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> World:
    """Provide `dev` with `silver.orders` and `silver.payroll`, and three callers.

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
        principal, TableRef("dev", _SILVER, "orders"), schema=_schema("order_id", "status")
    )
    catalog.create_table(
        principal, TableRef("dev", _SILVER, "payroll"), schema=_schema("employee", "salary")
    )
    for row in session.scalars(select(Table)):
        row.description_project = {
            "orders": "Purchases by customers",
            "payroll": "Confidential salary data",
        }[row.name]
    session.commit()
    return World(session, catalog, principal)


def test_two_callers_with_different_rights_see_different_tables_and_columns(world: World) -> None:
    assert world.names(world.alice, "o") == [
        "table:silver.orders",
        "column:silver.orders.order_id",
    ]
    assert world.names(world.bob, "o") == [
        "table:silver.orders",
        "column:silver.orders.order_id",
        "table:silver.payroll",
        "column:silver.payroll.employee",
    ]


def test_what_a_caller_may_not_read_never_appears_not_even_in_a_snippet(world: World) -> None:
    for query in ("salary", "payroll", "employee", "confidential"):
        page = world.search(world.alice, query)
        assert page.results == []
        assert not page.has_more
    visible = world.search(world.alice, "customers").results
    assert [h.name for h in visible] == ["silver.orders"]
    assert visible[0].matched == "description"
    assert visible[0].snippet == "Purchases by customers"
    assert "salary" not in repr(world.search(world.alice, "s").results)
    assert [h.name for h in world.search(world.bob, "salary").results] == [
        "silver.payroll.salary",
        "silver.payroll",
    ]


def test_a_name_in_any_case_matches_by_the_name_or_the_qualified_name(world: World) -> None:
    assert world.names(world.bob, "SILVER.ORD") == ["table:silver.orders"]
    assert world.names(world.bob, "PAYROLL", kind=SearchKind.TABLE) == ["table:silver.payroll"]


def test_the_edited_description_is_shown_and_searched_instead_of_the_project_text(
    world: World,
) -> None:
    row = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    row.description_edited = "Edited text about shipping"
    world.session.commit()

    assert world.names(world.bob, "customers") == []
    hit = world.search(world.bob, "shipping").results[0]
    assert (hit.name, hit.matched, hit.snippet) == (
        "silver.orders",
        "description",
        "Edited text about shipping",
    )


def test_a_column_matches_its_description_and_a_dropped_columns_text_does_not(
    world: World,
) -> None:
    orders = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    world.session.add_all(
        [
            ColumnDescription(
                id=uuid.uuid4(),
                table_id=orders.id,
                column="status",
                description_project="Where the parcel is",
                description_edited=None,
            ),
            ColumnDescription(
                id=uuid.uuid4(),
                table_id=orders.id,
                column="gone",
                description_project="Where the parcel was",
            ),
        ]
    )
    world.session.commit()

    page = world.search(world.alice, "parcel")

    assert [(h.kind, h.name, h.matched, h.snippet) for h in page.results] == [
        ("column", "silver.orders.status", "description", "Where the parcel is")
    ]
    assert page.results[0].column_type == "string"


def test_a_long_description_gives_a_snippet_of_at_most_160_characters_around_the_hit(
    world: World,
) -> None:
    row = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    row.description_project = "a" * 300 + " needle " + "b" * 300
    world.session.commit()

    snippet = world.search(world.alice, "needle").results[0].snippet

    assert snippet is not None
    assert len(snippet) <= 160
    assert "needle" in snippet


def test_a_hit_near_the_start_of_a_description_starts_the_snippet_at_the_start(
    world: World,
) -> None:
    row = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    row.description_project = "needle " + "b" * 300
    world.session.commit()

    snippet = world.search(world.alice, "needle").results[0].snippet

    assert snippet is not None and snippet.startswith("needle")


def test_ranking_is_exact_then_prefix_then_substring_then_description_then_kind(
    world: World,
) -> None:
    catalog, admin = world.catalog, world.admin
    for name in ("sales", "salesforce", "annual_sales"):
        catalog.create_table(admin, TableRef("dev", _GOLD, name), schema=_schema("id", "x"))
    catalog.create_table(admin, TableRef("dev", _GOLD, "kpi"), schema=_schema("sales", "x"))
    row = world.session.scalars(select(Table).where(Table.name == "kpi")).one()
    row.description_project = "Mentions sales"
    world.session.commit()

    assert world.names(world.root, "sales", kind=SearchKind.TABLE) == [
        "table:gold.sales",
        "table:gold.salesforce",
        "table:gold.annual_sales",
        "table:gold.kpi",
    ]
    assert world.names(world.root, "sales", kind=SearchKind.COLUMN) == ["column:gold.kpi.sales"]
    assert world.names(world.root, "sales")[:2] == ["table:gold.sales", "column:gold.kpi.sales"]


def test_percent_and_underscore_and_backslash_in_the_query_match_only_themselves(
    world: World,
) -> None:
    catalog, admin = world.catalog, world.admin
    catalog.create_table(admin, TableRef("dev", _GOLD, "a_b"), schema=_schema("id", "x"))
    catalog.create_table(admin, TableRef("dev", _GOLD, "axb"), schema=_schema("id", "x"))
    catalog.create_table(admin, TableRef("dev", _GOLD, "100x"), schema=_schema("id", "x"))
    # New names may not hold `%`; one stored by an older release still must not widen a search.
    world.session.execute(update(Table).where(Table.name == "100x").values(name="100%"))
    world.session.commit()
    catalog.create_table(admin, TableRef("dev", _GOLD, "back\\slash"), schema=_schema("id", "x"))

    assert world.names(world.root, "a_b") == ["table:gold.a_b"]
    assert world.names(world.root, "%") == ["table:gold.100%"]
    underscore = world.names(world.root, "_")
    assert "table:gold.a_b" in underscore
    assert "column:silver.orders.order_id" in underscore
    assert "table:gold.axb" not in underscore
    assert "table:silver.payroll" not in underscore
    assert world.names(world.root, "\\") == ["table:gold.back\\slash"]


def test_the_kind_filter_keeps_one_kind(world: World) -> None:
    assert world.names(world.bob, "order", kind=SearchKind.TABLE) == ["table:silver.orders"]
    assert world.names(world.bob, "order", kind=SearchKind.COLUMN) == [
        "column:silver.orders.order_id"
    ]
    assert world.names(world.bob, "order", kind=SearchKind.RUN) == []
    assert world.names(world.bob, "bob", kind=SearchKind.PRINCIPAL) == []


def test_paging_walks_the_results_with_has_more(world: World) -> None:
    first = world.search(world.bob, "o", limit=2)
    second = world.search(world.bob, "o", limit=2, offset=2)
    third = world.search(world.bob, "o", limit=2, offset=4)

    assert [h.name for h in first.results] == ["silver.orders", "silver.orders.order_id"]
    assert (first.limit, first.offset, first.has_more) == (2, 0, True)
    assert [h.name for h in second.results] == ["silver.payroll", "silver.payroll.employee"]
    assert second.has_more is False
    assert third.results == []
    assert (third.offset, third.has_more) == (4, False)


def test_paging_counts_only_what_the_caller_may_read(world: World) -> None:
    page = world.search(world.alice, "o", limit=1)
    assert [h.name for h in page.results] == ["silver.orders"]
    assert page.has_more is True
    assert world.search(world.alice, "o", limit=1, offset=1).has_more is False


def test_the_query_and_the_page_are_validated_with_a_message_that_says_what_to_do(
    world: World,
) -> None:
    with pytest.raises(ValidationError, match="between 1 and 200 characters"):
        world.search(world.bob, "   ")
    with pytest.raises(ValidationError, match="between 1 and 200 characters"):
        world.search(world.bob, "x" * 201)
    with pytest.raises(ValidationError, match="limit must be between 1 and 100"):
        world.search(world.bob, "o", limit=0)
    with pytest.raises(ValidationError, match="limit must be between 1 and 100"):
        world.search(world.bob, "o", limit=101)
    with pytest.raises(ValidationError, match="offset must be 0 or more"):
        world.search(world.bob, "o", offset=-1)
    assert world.search(world.bob, "  order ").results[0].name == "silver.orders"


def test_an_artifact_is_found_by_name_as_the_table_listing_shows_it(world: World) -> None:
    orders = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    world.session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=orders.namespace_id,
            name="model_weights",
            table_type=TableType.ARTIFACT,
        )
    )
    world.session.commit()

    hits = world.search(world.bob, "weights").results

    assert [(h.kind, h.name, h.table_kind) for h in hits] == [
        ("table", "silver.model_weights", "ARTIFACT")
    ]


def test_a_view_is_found_with_its_columns(world: World) -> None:
    world.catalog.create_or_replace_view(
        world.admin,
        TableRef("dev", _GOLD, "recent"),
        sql="SELECT 1 AS recency",
        references=[],
        columns=[ColumnSpec("recency", "long", True)],
    )

    hits = world.search(world.bob, "recen").results

    assert [(h.kind, h.name, h.table_kind) for h in hits] == [
        ("table", "gold.recent", "VIEW"),
        ("column", "gold.recent.recency", None),
    ]


def _run(world: World, *, job: str | None, repository: str = "analytics") -> Run:
    """Persist a finished run of the repository in `dev`.

    Parameters
    ----------
    world
        The fixture.
    job
        The run's job, or `None` for an ad-hoc run.
    repository
        The repository's name.

    Returns
    -------
    Run
        The run.
    """
    session = world.session
    environment = session.scalars(select(Environment)).one()
    repo = session.scalars(select(Repository).where(Repository.name == repository)).one_or_none()
    if repo is None:
        repo = Repository(
            id=uuid.uuid4(), name=repository, kind=RepositoryKind.HTTPS, url="https://h/a.git"
        )
        session.add(repo)
        session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=environment.id,
        commit_sha="a" * 40,
        manifest_json={"version": 5, "assets": [], "edges": []},
        code_uri="file:///x",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    run = Run(
        id=uuid.uuid4(),
        environment_id=environment.id,
        repository_id=repo.id,
        job_version_id=version.id,
        kind=RunKind.FULL,
        status=RunStatus.SUCCEEDED,
        selection_json={},
        job=job,
    )
    session.add(run)
    session.commit()
    return run


def test_runs_are_found_by_id_prefix_job_and_repository(world: World) -> None:
    nightly = _run(world, job="nightly-load")
    adhoc = _run(world, job=None, repository="reports")

    by_prefix = world.search(world.bob, str(nightly.id)[:8]).results
    assert [(h.kind, h.run_id, h.matched, h.job, h.repository, h.status) for h in by_prefix] == [
        ("run", nightly.id, "id", "nightly-load", "analytics", "succeeded")
    ]
    assert world.search(world.bob, nightly.id.hex[:6]).results[0].run_id == nightly.id
    assert world.search(world.bob, str(nightly.id)).results[0].run_id == nightly.id
    by_job = world.search(world.bob, "nightly").results
    assert [(h.run_id, h.matched) for h in by_job] == [(nightly.id, "job")]
    by_repository = world.search(world.bob, "report", kind=SearchKind.RUN).results
    assert [(h.run_id, h.matched, h.job) for h in by_repository] == [(adhoc.id, "repository", None)]


def test_a_run_id_needs_four_hex_characters_to_match(world: World) -> None:
    run = _run(world, job="x-load")
    prefix = run.id.hex[:3]

    assert world.search(world.bob, prefix, kind=SearchKind.RUN).results == []
    assert world.search(world.bob, run.id.hex[:4], kind=SearchKind.RUN).results[0].run_id == run.id


def test_runs_need_read_environment(world: World) -> None:
    _run(world, job="nightly-load")

    assert world.search(world.alice, "nightly").results == []
    assert world.search(world.alice, "nightly", kind=SearchKind.RUN).results == []
    assert len(world.search(world.bob, "nightly").results) == 1


def test_principals_are_listed_only_to_a_caller_who_may_list_them(world: World) -> None:
    _person(world.session, "etl-bot", PrincipalType.SERVICE_ACCOUNT)

    assert world.names(world.root, "bot") == ["principal:etl-bot"]
    hit = world.search(world.root, "bot").results[0]
    assert (hit.principal, hit.principal_type, hit.matched) == (
        "etl-bot",
        "SERVICE_ACCOUNT",
        "name",
    )
    assert world.names(world.root, "bob", kind=SearchKind.PRINCIPAL) == ["principal:bob"]
    assert world.search(world.bob, "bot").results == []
    assert world.search(world.alice, "bot", kind=SearchKind.PRINCIPAL).results == []


def test_an_environment_that_does_not_exist_is_a_404_for_a_caller_who_may_read_environments(
    session: Session, world: World
) -> None:
    allow = CatalogSearch(session, AllowAllAuthorization())
    with pytest.raises(NotFoundError, match="nope"):
        allow.search(world.admin, "nope", "x")

    grants = CatalogSearch(session, GrantAuthorization(session))
    assert grants.search(world.alice, "nope", "x").results == []


def test_a_search_gives_up_after_looking_at_a_bounded_number_of_matches(world: World) -> None:
    with (
        mock.patch.object(search_module, "MAX_SCANNED", 3),
        mock.patch.object(search_module, "CHUNK", 2),
    ):
        page = world.search(world.bob, "o", limit=5)

    assert [h.name for h in page.results] == [
        "silver.orders",
        "silver.orders.order_id",
        "silver.payroll",
    ]
    assert page.has_more is False


def test_the_authorization_is_asked_once_per_table(world: World) -> None:
    authz = GrantAuthorization(world.session)
    with mock.patch.object(authz, "permits", wraps=authz.permits) as permits:
        CatalogSearch(world.session, authz).search(world.bob, "dev", "o")

    asked = [c.args[1] for c in permits.call_args_list]
    assert asked.count(Action.READ_TABLE) == 2


def _set_model(world: World, name: str, *, kind: str, grain: str) -> None:
    """Write the declared kind and grain on a table's row, as a deploy does.

    Parameters
    ----------
    world
        The fixture.
    name
        The table's name.
    kind
        The declared kind.
    grain
        The declared grain.
    """
    row = world.session.scalars(select(Table).where(Table.name == name)).one()
    row.model_kind = kind
    row.model_grain = grain
    world.session.commit()


def test_a_grain_matches_and_shows_its_snippet(world: World) -> None:
    _set_model(world, "orders", kind="fact", grain="One row per store and calendar day.")
    (hit,) = world.search(world.bob, "calendar day").results
    assert (hit.name, hit.matched, hit.model_kind) == ("silver.orders", "grain", "fact")
    assert "calendar day" in (hit.snippet or "")


def test_a_grain_matches_on_a_table_without_a_description(world: World) -> None:
    _set_model(world, "orders", kind="fact", grain="One row per store and calendar day.")
    row = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    row.description_project = None
    world.session.commit()
    (hit,) = world.search(world.bob, "calendar day").results
    assert (hit.name, hit.matched) == ("silver.orders", "grain")
    assert hit.snippet == "One row per store and calendar day."


def test_a_name_or_description_match_still_shows_the_kind(world: World) -> None:
    _set_model(world, "orders", kind="fact", grain="One row per order.")
    hit = world.search(world.bob, "orders", kind=SearchKind.TABLE).results[0]
    assert (hit.matched, hit.model_kind) == ("name", "fact")
    assert world.search(world.bob, "customers").results[0].matched == "description"


def test_a_table_without_a_model_has_no_kind(world: World) -> None:
    assert world.search(world.bob, "orders").results[0].model_kind is None


def test_model_kind_keeps_only_tables_of_that_kind(world: World) -> None:
    _set_model(world, "orders", kind="dimension", grain="One row per order.")
    _set_model(world, "payroll", kind="fact", grain="One row per order line.")
    page = CatalogSearch(world.session, AllowAllAuthorization()).search(
        world.admin, "dev", "order", model_kind="dimension"
    )
    assert [h.name for h in page.results] == ["silver.orders"]


def test_a_bad_model_kind_is_refused(world: World) -> None:
    with pytest.raises(ValidationError, match="model_kind must be one of fact, dimension"):
        CatalogSearch(world.session, AllowAllAuthorization()).search(
            world.admin, "dev", "x", model_kind="cube"
        )


def test_the_grain_and_kind_of_a_table_the_reader_may_not_read_never_appear(world: World) -> None:
    _set_model(world, "payroll", kind="fact", grain="One row per secret bonus.")
    _set_model(world, "orders", kind="fact", grain="One row per order; see silver.payroll.")
    assert world.search(world.alice, "secret bonus").results == []
    assert world.search(world.alice, "bonus", kind=SearchKind.TABLE).results == []
    assert world.search(world.alice, "payroll").results[0].name == "silver.orders"
    only = CatalogSearch(world.session, GrantAuthorization(world.session)).search(
        world.alice, "dev", "bonus", model_kind="fact"
    )
    assert only.results == []
    assert [h.name for h in world.search(world.bob, "secret bonus").results] == ["silver.payroll"]


def test_a_column_hit_matches_its_measure_label_and_description(world: World) -> None:
    payroll = world.session.scalars(select(Table).where(Table.name == "payroll")).one()
    namespace = world.session.get(Namespace, payroll.namespace_id)
    assert namespace is not None
    world.session.add(
        DeclaredMeasure(
            id=uuid.uuid4(),
            environment_id=namespace.environment_id,
            table_id=payroll.id,
            column="salary",
            aggregation="sum",
            additivity="additive",
            not_over=[],
            currency="EUR",
            label="Gross pay",
            description="Paid before deductions.",
        )
    )
    world.session.commit()
    (by_label,) = world.search(world.bob, "gross pay").results
    assert (by_label.kind, by_label.name, by_label.matched, by_label.snippet) == (
        "column",
        "silver.payroll.salary",
        "measure",
        "Gross pay",
    )
    (by_description,) = world.search(world.bob, "deductions").results
    assert (by_description.matched, by_description.snippet) == (
        "measure",
        "Paid before deductions.",
    )
    assert world.search(world.alice, "gross pay").results == []
    assert world.search(world.alice, "deductions").results == []


def _translate(
    world: World,
    name: str,
    language: str,
    field: str,
    text: str,
    *,
    subject: str = "table",
    column: str = "",
) -> None:
    """Write one project translation row on a table, as a deploy does, and list its language.

    Parameters
    ----------
    world
        The fixture.
    name
        The table's name.
    language
        The tag.
    field
        ``label``, ``description`` or ``grain``.
    text
        The text.
    subject
        ``table``, ``column`` or ``measure``.
    column
        The column, for a column or a measure.
    """
    row = world.session.scalars(select(Table).where(Table.name == name)).one()
    world.session.add(
        TextTranslation(
            id=uuid.uuid4(),
            table_id=row.id,
            subject=subject,
            name=column,
            language=language,
            field=field,
            text_project=text,
        )
    )
    row.text_languages = sorted({*(row.text_languages or []), language})
    world.session.commit()


def _search(world: World, who: PrincipalRef, q: str, languages: tuple[str, ...] = ()) -> SearchPage:
    """Search `dev` under the real grants, in the given reader languages.

    Parameters
    ----------
    world
        The fixture.
    who
        The caller.
    q
        The query.
    languages
        The reader's languages.

    Returns
    -------
    SearchPage
        The page.
    """
    return CatalogSearch(world.session, GrantAuthorization(world.session)).search(
        who, "dev", q, languages=languages
    )


def test_a_label_matches_like_a_name_in_the_main_language(world: World) -> None:
    row = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    row.label_project = "Revenue"
    world.session.commit()
    (hit,) = _search(world, world.bob, "revenue").results
    assert (hit.name, hit.matched, hit.language, hit.snippet) == (
        "silver.orders",
        "label",
        "en",
        "Revenue",
    )


def test_a_label_says_the_tables_own_main_language_and_ranks_like_a_name(world: World) -> None:
    row = world.session.scalars(select(Table).where(Table.name == "payroll")).one()
    row.label_edited = "Lohn"
    row.text_language = "de"
    world.session.commit()
    search = CatalogSearch(world.session, GrantAuthorization(world.session), main_language="fr")
    (hit,) = search.search(world.bob, "dev", "lohn").results
    assert (hit.matched, hit.language) == ("label", "de")
    world.session.scalars(
        select(Table).where(Table.name == "orders")
    ).one().description_project = "Lohn and more"
    world.session.commit()
    assert [h.name for h in search.search(world.bob, "dev", "lohn").results] == [
        "silver.payroll",
        "silver.orders",
    ]


def test_a_description_match_says_the_main_language_and_a_name_match_none(world: World) -> None:
    (hit,) = _search(world, world.bob, "customers").results
    assert (hit.matched, hit.language) == ("description", "en")
    assert _search(world, world.bob, "payroll").results[0].language is None


def test_a_translated_label_matches_only_in_the_readers_language(world: World) -> None:
    _translate(world, "orders", "fr", "label", "Chiffre d'affaires")
    (hit,) = _search(world, world.bob, "chiffre", ("fr-CH",)).results
    assert (hit.name, hit.matched, hit.language) == ("silver.orders", "label", "fr")
    assert _search(world, world.bob, "chiffre", ("de",)).results == []
    assert _search(world, world.bob, "chiffre").results == []


def test_a_translated_description_and_grain_match_with_a_snippet(world: World) -> None:
    _translate(world, "orders", "fr", "description", "Achats des clients")
    _translate(world, "orders", "fr", "grain", "Une ligne par commande")
    (description,) = _search(world, world.bob, "achats", ("fr",)).results
    assert (description.matched, description.language, description.snippet) == (
        "description",
        "fr",
        "Achats des clients",
    )
    (grain,) = _search(world, world.bob, "par commande", ("fr",)).results
    assert (grain.matched, grain.language) == ("grain", "fr")


def test_an_edited_translation_is_searched_instead_of_the_project_text(world: World) -> None:
    _translate(world, "orders", "fr", "label", "Commandes")
    world.session.execute(update(TextTranslation).values(text_edited="Ventes"))
    world.session.commit()
    assert _search(world, world.bob, "commandes", ("fr",)).results == []
    (hit,) = _search(world, world.bob, "ventes", ("fr",)).results
    assert (hit.snippet, hit.language) == ("Ventes", "fr")


def test_a_table_found_twice_is_one_hit(world: World) -> None:
    _translate(world, "orders", "fr", "label", "Orders")
    page = _search(world, world.bob, "orders", ("fr",))
    tables = [h for h in page.results if h.kind is SearchKind.TABLE]
    assert [(h.name, h.matched, h.language) for h in tables] == [("silver.orders", "name", None)]
    assert not page.has_more


def test_a_better_translated_match_wins_over_a_description_match(world: World) -> None:
    _translate(world, "orders", "fr", "label", "Customers")
    (hit,) = _search(world, world.bob, "customers", ("fr",)).results
    assert (hit.name, hit.matched, hit.language) == ("silver.orders", "label", "fr")


def test_a_repeat_is_not_counted_twice_by_paging(world: World) -> None:
    _translate(world, "orders", "fr", "label", "Orders")
    _translate(world, "orders", "de", "label", "Orders")
    first = _search(world, world.bob, "orders", ("fr", "de"))
    assert [h.name for h in first.results] == ["silver.orders"]
    page = CatalogSearch(world.session, GrantAuthorization(world.session)).search(
        world.bob, "dev", "orders", languages=("fr", "de"), limit=1
    )
    assert ([h.name for h in page.results], page.has_more) == (["silver.orders"], False)


def test_a_translated_column_label_and_measure_text_match(world: World) -> None:
    _translate(world, "orders", "fr", "label", "Statut", subject="column", column="status")
    _translate(
        world, "orders", "fr", "label", "Commandes vendues", subject="measure", column="order_id"
    )
    (column,) = _search(world, world.bob, "statut", ("fr",)).results
    assert (column.name, column.matched, column.language) == ("silver.orders.status", "label", "fr")
    (measure,) = _search(world, world.bob, "vendues", ("fr",)).results
    assert (measure.name, measure.matched, measure.language) == (
        "silver.orders.order_id",
        "measure",
        "fr",
    )


def test_a_translated_column_description_matches_and_a_gone_columns_text_does_not(
    world: World,
) -> None:
    _translate(
        world, "orders", "fr", "description", "Où elle en est", subject="column", column="status"
    )
    _translate(world, "orders", "fr", "label", "Disparue", subject="column", column="gone")
    (hit,) = _search(world, world.bob, "elle en", ("fr",)).results
    assert (hit.name, hit.matched, hit.snippet) == (
        "silver.orders.status",
        "description",
        "Où elle en est",
    )
    assert _search(world, world.bob, "disparue", ("fr",)).results == []


def test_a_column_label_in_the_main_language_matches(world: World) -> None:
    world.session.add(
        ColumnDescription(
            id=uuid.uuid4(),
            table_id=world.session.scalars(select(Table.id).where(Table.name == "orders")).one(),
            column="status",
            label_project="State",
        )
    )
    world.session.commit()
    (hit,) = _search(world, world.bob, "state").results
    assert (hit.name, hit.matched, hit.language, hit.snippet) == (
        "silver.orders.status",
        "label",
        "en",
        "State",
    )


def test_a_translated_label_of_a_table_the_reader_cannot_read_is_not_found(world: World) -> None:
    _translate(world, "payroll", "fr", "label", "Salaires secrets")
    page = _search(world, world.alice, "secrets", ("fr",))
    assert page.results == [] and not page.has_more
    assert [h.name for h in _search(world, world.bob, "secrets", ("fr",)).results] == [
        "silver.payroll"
    ]


def test_no_label_or_translation_of_a_hidden_tables_column_appears_not_even_as_a_table(
    world: World,
) -> None:
    payroll = world.session.scalars(select(Table).where(Table.name == "payroll")).one()
    payroll.label_project = "Hidden pay"
    world.session.add(
        ColumnDescription(
            id=uuid.uuid4(), table_id=payroll.id, column="salary", label_project="Hidden wage"
        )
    )
    world.session.commit()
    _translate(world, "payroll", "fr", "label", "Prime cachée", subject="column", column="salary")
    _translate(world, "payroll", "fr", "label", "Bonus caché", subject="measure", column="salary")
    _translate(
        world, "payroll", "fr", "description", "Texte caché", subject="column", column="salary"
    )
    for query in ("hidden", "cachée", "caché", "texte"):
        page = _search(world, world.alice, query, ("fr",))
        assert page.results == [] and not page.has_more, query
    assert "cach" not in repr(_search(world, world.alice, "c", ("fr",)).results)
    assert {h.name for h in _search(world, world.bob, "caché", ("fr",)).results} == {
        "silver.payroll.salary"
    }


def test_a_masked_columns_label_is_found_as_its_name_is(world: World) -> None:
    masks = ColumnMaskService(
        world.session, AllowAllAuthorization(), FakeColumnCatalog(names=["order_id", "status"])
    )
    masks.create(
        world.admin,
        principal="alice",
        environment="dev",
        namespace="silver",
        table="orders",
        column="status",
        expression="NULL",
    )
    _translate(world, "orders", "fr", "label", "Statut", subject="column", column="status")
    assert [h.name for h in _search(world, world.alice, "status").results] == [
        "silver.orders.status"
    ]
    assert [h.name for h in _search(world, world.alice, "statut", ("fr",)).results] == [
        "silver.orders.status"
    ]


def test_translations_cost_no_statement_per_table(world: World) -> None:
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

    engine = world.session.get_bind()
    event.listen(engine, "before_cursor_execute", _count)
    try:
        _search(world, world.bob, "o", ("fr",))
        plain = len(statements)
        _translate(world, "orders", "fr", "label", "Commandes")
        _translate(world, "payroll", "fr", "label", "Paie")
        statements.clear()
        _search(world, world.bob, "o", ("fr",))
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert len(statements) == plain


def test_a_leftover_translation_in_the_tables_main_language_is_never_found(world: World) -> None:
    """A row in the main language is a leftover from before it changed; `table get` ignores it."""
    _translate(world, "orders", "de", "label", "Alte Bestellungen")
    _translate(world, "orders", "de", "label", "Alter Status", subject="column", column="status")
    assert len(_search(world, world.bob, "alte", ("de",)).results) == 2
    row = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    row.text_language = "de"
    row.label_project = "Bestellungen"
    world.session.commit()
    assert _search(world, world.bob, "alte", ("de",)).results == []
    row.text_language = None
    world.session.commit()
    by_instance = CatalogSearch(
        world.session, GrantAuthorization(world.session), main_language="de"
    ).search(world.bob, "dev", "alte", languages=("de",))
    assert by_instance.results == []


def test_of_two_matching_translations_the_readers_first_language_is_shown(world: World) -> None:
    _translate(world, "orders", "fr", "label", "Ventes en France")
    _translate(world, "orders", "de", "label", "Ventes en Allemagne")
    _translate(world, "orders", "fr", "label", "Statut fr", subject="column", column="status")
    _translate(world, "orders", "de", "label", "Statut de", subject="column", column="status")
    for languages, expected in ((("fr", "de"), "fr"), (("de", "fr"), "de")):
        (table,) = _search(world, world.bob, "ventes", languages).results
        assert (table.language, table.snippet) == (
            expected,
            {"fr": "Ventes en France", "de": "Ventes en Allemagne"}[expected],
        )
        (column,) = _search(world, world.bob, "statut", languages).results
        assert column.language == expected


def test_a_regional_tag_and_its_language_are_ordered_by_the_reader_not_by_spelling(
    world: World,
) -> None:
    row = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    row.text_language = "de"
    world.session.commit()
    _translate(world, "orders", "en-US", "label", "Sales US")
    _translate(world, "orders", "en", "label", "Sales EN")
    (regional,) = _search(world, world.bob, "sales", ("en-US",)).results
    assert regional.language == "en-US"
    (plain,) = _search(world, world.bob, "sales", ("en", "en-US")).results
    assert plain.language == "en"


def test_matched_says_label_when_the_label_decided_the_rank(world: World) -> None:
    row = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    row.label_project = "Ord"
    world.session.add(
        ColumnDescription(id=uuid.uuid4(), table_id=row.id, column="status", label_project="Stat")
    )
    world.session.commit()
    table = _search(world, world.bob, "ord").results[0]
    assert (table.name, table.matched, table.language, table.snippet) == (
        "silver.orders",
        "label",
        "en",
        "Ord",
    )
    (column,) = _search(world, world.bob, "stat").results
    assert (column.name, column.matched, column.language) == ("silver.orders.status", "label", "en")
    by_name = _search(world, world.bob, "orders").results[0]
    assert (by_name.matched, by_name.language) == ("name", None)


def test_a_hit_says_whether_a_translation_matched(world: World) -> None:
    row = world.session.scalars(select(Table).where(Table.name == "orders")).one()
    row.label_project = "Revenue"
    world.session.commit()
    _translate(world, "orders", "fr", "description", "Chiffre d'affaires")
    _translate(world, "orders", "fr", "label", "Statut", subject="column", column="status")
    (header,) = _search(world, world.bob, "revenue", ("fr",)).results
    assert (header.language, header.translated) == ("en", False)
    (table,) = _search(world, world.bob, "chiffre", ("fr",)).results
    assert (table.language, table.translated) == ("fr", True)
    (column,) = _search(world, world.bob, "statut", ("fr",)).results
    assert (column.language, column.translated) == ("fr", True)
    assert _search(world, world.bob, "payroll").results[0].translated is False
