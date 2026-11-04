"""Walking the metadata graph for one reader: lineage, neighbours, cut, paging, budgets (#374)."""

from __future__ import annotations

import itertools
import time
import uuid
from collections.abc import Callable

import pytest
from graph_test_helpers import Grants, StarWorld, asset_entry, point
from sqlalchemy import event

from lakana_core.auth import PrincipalRef
from lakana_core.catalog import graph_service
from lakana_core.catalog.graph_model import (
    MAX_GRAPH_STEPS,
    EdgeKind,
    GraphPage,
    LineageWay,
    NodeKind,
    asset_node,
    table_node,
)
from lakana_core.catalog.graph_service import GraphService
from lakana_core.errors import GraphBusyError, NotFoundError, ValidationError
from lakana_core.models import Repository, RepositoryKind, TextTranslation

_EVERY_BUT_SILVER = {
    "bronze.sales_raw",
    "bronze.stores_raw",
    "gold.dim_store",
    "gold.dim_date",
    "gold.fact_sales",
}
"""Every table of the star except `silver.sales`."""


def _service(
    star: StarWorld,
    grants: Grants | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    max_steps: int = MAX_GRAPH_STEPS,
) -> GraphService:
    """Build the service over the star.

    Parameters
    ----------
    star
        The world.
    grants
        What the reader may do; everything, policies included, when `None`.
    clock
        The service's clock.
    max_steps
        The step budget.

    Returns
    -------
    GraphService
        The service.
    """
    authz = grants or Grants(policies=True)
    return GraphService(star.session, authz, "en", clock=clock, max_steps=max_steps)


def _seen(page: GraphPage) -> set[tuple[NodeKind, str, int]]:
    """Flatten a page's nodes.

    Parameters
    ----------
    page
        The page.

    Returns
    -------
    set
        ``(kind, name, distance)`` per node.
    """
    return {(n.kind, n.name, n.distance) for n in page.nodes}


def _add_repository(star: StarWorld, name: str, assets: list[dict[str, object]]) -> None:
    """Deploy one more repository into `dev` whose manifest holds `assets`.

    Parameters
    ----------
    star
        The world.
    name
        The repository's name.
    assets
        Its manifest's asset entries.
    """
    repo = Repository(
        id=uuid.uuid4(), name=name, kind=RepositoryKind.HTTPS, url=f"https://h/{name}"
    )
    star.session.add(repo)
    star.session.flush()
    point(star.session, star.env_id, repo, {"version": 13, "assets": assets})


_T, _A = NodeKind.TABLE, NodeKind.ASSET


def test_lineage_walks_both_ways_across_two_repositories(
    star: StarWorld, principal: PrincipalRef
) -> None:
    page = _service(star).lineage(principal, "dev", "gold.dim_store", depth=1)
    assert _seen(page) == {
        (_T, "gold.dim_store", 0),
        (_A, "gold.dim_store", 1),
        (_T, "bronze.stores_raw", 1),
        (_A, "gold.fact_sales", 1),
        (_T, "gold.fact_sales", 1),
    }
    dim = star.table_id("gold.dim_store")
    assert {(e.kind, e.source, e.target) for e in page.edges} == {
        (EdgeKind.BUILDS, asset_node("stores", "gold.dim_store"), table_node(dim)),
        (
            EdgeKind.READS,
            asset_node("stores", "gold.dim_store"),
            table_node(star.table_id("bronze.stores_raw")),
        ),
        (EdgeKind.READS, asset_node("sales", "gold.fact_sales"), table_node(dim)),
        (
            EdgeKind.BUILDS,
            asset_node("sales", "gold.fact_sales"),
            table_node(star.table_id("gold.fact_sales")),
        ),
    }
    root = page.nodes[0]
    assert (root.id, root.table_type, root.model_kind, root.key, root.grain) == (
        table_node(dim),
        "managed",
        "dimension",
        ("store_id",),
        "One row per store.",
    )
    assert (root.owner, root.repository, page.next_cursor) == ("stores-team", "stores", None)


def test_up_and_down_and_depth_count_assets(star: StarWorld, principal: PrincipalRef) -> None:
    down = _service(star).lineage(
        principal, "dev", "bronze.sales_raw", direction=LineageWay.DOWN, depth=2
    )
    assert _seen(down) == {
        (_T, "bronze.sales_raw", 0),
        (_A, "silver.sales", 1),
        (_T, "silver.sales", 1),
        (_A, "gold.dim_date", 2),
        (_T, "gold.dim_date", 2),
        (_A, "gold.fact_sales", 2),
        (_T, "gold.fact_sales", 2),
    }
    near = _service(star).lineage(
        principal, "dev", "gold.fact_sales", direction=LineageWay.UP, depth=1
    )
    assert {name for kind, name, _ in _seen(near) if kind is _A} == {"gold.fact_sales"}
    up = _service(star).lineage(principal, "dev", "gold.fact_sales", direction=LineageWay.UP)
    assert {name for kind, name, _ in _seen(up) if kind is _T} == {
        "gold.fact_sales",
        "silver.sales",
        "gold.dim_store",
        "bronze.sales_raw",
        "bronze.stores_raw",
    }


def test_lineage_of_an_asset_starts_at_zero_with_its_tables(
    star: StarWorld, principal: PrincipalRef
) -> None:
    page = _service(star).lineage(
        principal, "dev", "asset:sales/silver.sales", direction=LineageWay.UP, depth=1
    )
    assert _seen(page) == {(_A, "silver.sales", 0), (_T, "bronze.sales_raw", 0)}


def test_a_column_lineage_starts_at_its_table_and_adds_its_references(
    star: StarWorld, principal: PrincipalRef
) -> None:
    page = _service(star).lineage(
        principal, "dev", "column:gold.dim_store/store_id", direction=LineageWay.UP, depth=1
    )
    seen = _seen(page)
    assert (NodeKind.COLUMN, "gold.dim_store.store_id", 0) in seen
    assert (_T, "gold.dim_store", 0) in seen
    assert (_T, "gold.dim_store", 1) not in seen
    assert (
        NodeKind.REFERENCE,
        "gold.fact_sales.store_id → gold.dim_store.store_id",
        1,
    ) in seen
    assert (NodeKind.COLUMN, "gold.fact_sales.store_id", 1) in seen
    assert (_T, "gold.fact_sales", 1) in seen
    assert (_T, "bronze.stores_raw", 1) in seen
    source = _service(star).lineage(
        principal, "dev", "column:gold.fact_sales/store_id", direction=LineageWay.DOWN, depth=1
    )
    assert {(NodeKind.COLUMN, "gold.dim_store.store_id", 1), (_T, "gold.dim_store", 1)} <= _seen(
        source
    )


def test_lineage_starts_only_at_a_table_column_or_asset(
    star: StarWorld, principal: PrincipalRef
) -> None:
    with pytest.raises(ValidationError) as caught:
        _service(star).lineage(principal, "dev", "term:store")
    assert str(caught.value) == (
        "Lineage starts at a table, a column or an asset, and `term:store` is a glossary term. "
        "Ask for its neighbours instead: lakana graph neighbours term:store --env dev"
    )


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (
            lambda s, p: s.lineage(p, "dev", "gold.dim_store", depth=0),
            "depth must be between 1 and 10; got 0. Ask for fewer steps, such as --depth 3.",
        ),
        (
            lambda s, p: s.lineage(p, "dev", "gold.dim_store", depth=11),
            "depth must be between 1 and 10; got 11. Ask for fewer steps, such as --depth 3.",
        ),
        (
            lambda s, p: s.neighbours(p, "dev", "gold.dim_store", limit=501),
            "The page size must be between 1 and 500 (`limit` in the REST API, `page_size` "
            "in the SDK); got 501.",
        ),
        (
            lambda s, p: s.neighbours(p, "dev", "gold.dim_store", kinds=["nope"]),
            "`nope` is not an edge kind. Use one of: builds, checks, filters, has_column, links, "
            "masks, measures, reads, references, runs.",
        ),
    ],
)
def test_parameters_out_of_range_are_refused_with_the_next_step(
    star: StarWorld,
    principal: PrincipalRef,
    call: Callable[[GraphService, PrincipalRef], object],
    message: str,
) -> None:
    with pytest.raises(ValidationError) as caught:
        call(_service(star), principal)
    assert str(caught.value) == message


def test_a_hidden_table_cuts_the_walk_and_leaves_no_trace(
    star: StarWorld, principal: PrincipalRef
) -> None:
    reader = _service(star, Grants(_EVERY_BUT_SILVER))
    cut = reader.lineage(principal, "dev", "bronze.sales_raw", direction=LineageWay.DOWN, depth=10)
    assert cut == GraphPage(cut.nodes[:1], (), None)
    assert cut.nodes[0].name == "bronze.sales_raw"
    up = reader.lineage(principal, "dev", "gold.fact_sales", direction=LineageWay.UP, depth=10)
    names = {n.name for n in up.nodes}
    assert "silver.sales" not in names and "bronze.sales_raw" not in names
    assert {"gold.dim_store", "bronze.stores_raw"} <= names


def test_a_node_beyond_a_hidden_one_appears_when_another_route_reaches_it(
    star: StarWorld, principal: PrincipalRef
) -> None:
    # A second route from bronze.sales_raw to gold.fact_sales, through bronze.stores_raw and
    # gold.dim_store.
    _add_repository(star, "extra", [asset_entry("bronze.stores_raw", ("bronze.sales_raw",), "x")])
    reader = _service(star, Grants(_EVERY_BUT_SILVER))
    page = reader.lineage(principal, "dev", "bronze.sales_raw", direction=LineageWay.DOWN, depth=3)
    names = {n.name for n in page.nodes}
    assert "gold.fact_sales" in names and "silver.sales" not in names
    silver = table_node(star.table_id("silver.sales"))
    assert all(silver not in (e.source, e.target) for e in page.edges)


def test_an_asset_of_a_table_not_in_the_catalog_is_seen_by_its_name(
    star: StarWorld, principal: PrincipalRef
) -> None:
    # Each reads two tables of one walk, so the walk asks about each name twice.
    reads = ("bronze.stores_raw", "gold.dim_store")
    _add_repository(
        star,
        "later",
        [
            asset_entry("gold.not_yet", reads, "o", columns=()),
            asset_entry("x", reads, "o", columns=()),
        ],
    )
    down = LineageWay.DOWN
    page = _service(star).lineage(principal, "dev", "bronze.stores_raw", direction=down)
    assets = {n.id for n in page.nodes if n.kind is _A}
    assert asset_node("later", "gold.not_yet") in assets
    assert asset_node("later", "x") not in assets
    narrow = _service(star, Grants({"bronze.stores_raw", "gold.dim_store", "gold.fact_sales"}))
    seen = narrow.lineage(principal, "dev", "bronze.stores_raw", direction=down)
    assert {n.id for n in seen.nodes if n.kind is _A} == {
        asset_node("stores", "gold.dim_store"),
        asset_node("sales", "gold.fact_sales"),
    }


def test_assets_and_jobs_need_read_environment(star: StarWorld, principal: PrincipalRef) -> None:
    reader = _service(star, Grants(environment=False))
    page = reader.lineage(principal, "dev", "gold.dim_store")
    assert [n.name for n in page.nodes] == ["gold.dim_store"]
    # The owner is the building asset's: without READ_ENVIRONMENT, neither it nor the repository.
    assert (page.nodes[0].owner, page.nodes[0].repository) == (None, None)
    seen = _service(star).lineage(principal, "dev", "gold.dim_store").nodes[0]
    assert (seen.owner, seen.repository) == ("stores-team", "stores")
    with pytest.raises(NotFoundError) as hidden:
        reader.neighbours(principal, "dev", "asset:sales/gold.fact_sales")
    with pytest.raises(NotFoundError) as missing:
        reader.neighbours(principal, "dev", "asset:sales/gold.nope")
    assert str(hidden.value).replace("gold.fact_sales", "X") == str(missing.value).replace(
        "gold.nope", "X"
    )


def test_repositories_need_read_environment(star: StarWorld, principal: PrincipalRef) -> None:
    def repositories(grants: Grants) -> dict[NodeKind, set[str | None]]:
        found: dict[NodeKind, set[str | None]] = {}
        service = _service(star, grants)
        for node in (
            "gold.fact_sales",
            "column:gold.fact_sales/store_id",
            "column:gold.fact_sales/net_amount",
        ):
            for n in service.neighbours(principal, "dev", node).nodes:
                found.setdefault(n.kind, set()).add(n.repository)
        return found

    seen = repositories(Grants())
    assert seen[_T] == {"sales"} and seen[NodeKind.CHECK] == {"sales"}
    assert seen[NodeKind.REFERENCE] == {"sales"} and seen[NodeKind.MEASURE] == {"sales"}
    unseen = repositories(Grants(environment=False))
    for kind in (_T, NodeKind.CHECK, NodeKind.REFERENCE, NodeKind.MEASURE):
        assert unseen[kind] == {None}


def test_a_hidden_or_missing_start_or_environment_reads_as_search_does(
    star: StarWorld, principal: PrincipalRef
) -> None:
    reader = _service(star, Grants({"gold.dim_store"}))
    with pytest.raises(NotFoundError) as hidden:
        reader.lineage(principal, "dev", "gold.fact_sales")
    assert str(hidden.value) == (
        "There is no node `gold.fact_sales` in environment `dev` that you may see. Give a table "
        "as gold.dim_store and a column as column:gold.dim_store/store_id (--column store_id in "
        "the CLI); find a table with: lakana search TEXT --env dev"
    )
    with pytest.raises(NotFoundError) as no_env:
        reader.lineage(principal, "prod", "gold.fact_sales")
    assert (
        str(no_env.value) == "Environment 'prod' does not exist. List them with 'lakana env list'."
    )
    outsider = _service(star, Grants(set(), environment=False))
    with pytest.raises(NotFoundError) as unseen_env:
        outsider.lineage(principal, "prod", "gold.fact_sales")
    assert str(unseen_env.value).startswith(
        "There is no node `gold.fact_sales` in environment `prod` that you may see."
    )


def test_a_column_typed_as_a_dotted_name_gets_the_column_form(
    star: StarWorld, principal: PrincipalRef
) -> None:
    with pytest.raises(NotFoundError) as caught:
        _service(star).neighbours(principal, "dev", "gold.dim_store.store_id")
    assert "a column as column:gold.dim_store/store_id (--column store_id in the CLI)" in str(
        caught.value
    )


def test_neighbours_of_a_table_show_every_kind_a_reader_may_see(
    star: StarWorld, principal: PrincipalRef
) -> None:
    page = _service(star).neighbours(principal, "dev", "gold.fact_sales")
    assert {(n.kind, n.name) for n in page.nodes if n.distance == 1} == {
        (NodeKind.COLUMN, "gold.fact_sales.date"),
        (NodeKind.COLUMN, "gold.fact_sales.store_id"),
        (NodeKind.COLUMN, "gold.fact_sales.net_amount"),
        (_A, "gold.fact_sales"),
        (NodeKind.CHECK, "gold.fact_sales/positive_amount"),
        (NodeKind.CHECK, "gold.fact_sales/ref_date"),
        (NodeKind.CHECK, "gold.fact_sales/ref_store_id"),
        (NodeKind.ROW_FILTER, "row filter on gold.fact_sales"),
    }
    column = next(n for n in page.nodes if n.name == "gold.fact_sales.store_id")
    assert column.column_type == "long"
    plain = _service(star, Grants()).neighbours(principal, "dev", "gold.fact_sales")
    assert NodeKind.ROW_FILTER not in {n.kind for n in plain.nodes}
    checks = _service(star).neighbours(principal, "dev", "gold.fact_sales", kinds=["checks"])
    assert {n.kind for n in checks.nodes} == {_T, NodeKind.CHECK}
    asset = _service(star).neighbours(principal, "dev", "asset:sales/gold.fact_sales")
    job = next(n for n in asset.nodes if n.kind is NodeKind.JOB)
    assert (job.name, job.owner, job.repository) == ("sales/nightly", None, "sales")
    fact = next(n for n in asset.nodes if n.kind is _A)
    assert (fact.name, fact.owner, fact.repository) == ("gold.fact_sales", "sales-team", "sales")


def test_a_reference_whose_target_is_hidden_is_cut_with_its_check(
    star: StarWorld, principal: PrincipalRef
) -> None:
    reader = _service(
        star, Grants({"gold.fact_sales", "gold.dim_store", "silver.sales", "bronze.sales_raw"})
    )
    column = reader.neighbours(principal, "dev", "column:gold.fact_sales/date")
    assert NodeKind.REFERENCE not in {n.kind for n in column.nodes}
    table = reader.neighbours(principal, "dev", "gold.fact_sales", kinds=["checks"])
    assert {n.name for n in table.nodes if n.kind is NodeKind.CHECK} == {
        "gold.fact_sales/positive_amount",
        "gold.fact_sales/ref_store_id",
    }


def test_policies_need_manage_policy_and_the_table(
    star: StarWorld, principal: PrincipalRef
) -> None:
    node = "column:gold.dim_store/store_name"
    allowed = _service(star, Grants({"gold.dim_store"}, policies=True))
    masks = [
        n
        for n in allowed.neighbours(principal, "dev", node).nodes
        if n.kind is NodeKind.COLUMN_MASK
    ]
    assert [n.name for n in masks] == ["column mask on gold.dim_store.store_name"]
    plain = _service(star, Grants({"gold.dim_store"}))
    assert NodeKind.COLUMN_MASK not in {
        n.kind for n in plain.neighbours(principal, "dev", node).nodes
    }


def test_terms_and_labels_show_in_the_reader_language(
    star: StarWorld, principal: PrincipalRef
) -> None:
    page = _service(star).neighbours(
        principal, "dev", "column:gold.fact_sales/net_amount", languages=("fr",)
    )
    term = next(n for n in page.nodes if n.kind is NodeKind.TERM)
    assert (term.name, term.label, term.owner, term.repository) == (
        "net_revenue",
        "CA net",
        "finance",
        "sales",
    )
    measure = next(n for n in page.nodes if n.kind is NodeKind.MEASURE)
    assert (measure.name, measure.repository) == ("gold.fact_sales.net_amount", "sales")


def test_a_table_label_and_grain_show_in_the_reader_language(
    star: StarWorld, principal: PrincipalRef
) -> None:
    dim = star.table_id("gold.dim_store")
    star.session.add_all(
        TextTranslation(
            id=uuid.uuid4(),
            table_id=dim,
            subject="table",
            name="",
            language="fr",
            field=field,
            text_project=text,
        )
        for field, text in (("label", "Magasins"), ("grain", "Une ligne par magasin."))
    )
    star.session.flush()
    service = _service(star)
    french = service.neighbours(principal, "dev", "gold.dim_store", languages=("fr",)).nodes[0]
    assert (french.label, french.grain) == ("Magasins", "Une ligne par magasin.")
    english = service.neighbours(principal, "dev", "gold.dim_store").nodes[0]
    assert (english.label, english.grain) == (None, "One row per store.")


def _statements(star: StarWorld, ask: Callable[[], object]) -> int:
    """Count the SQL statements a question issues.

    Parameters
    ----------
    star
        The world.
    ask
        The question.

    Returns
    -------
    int
        How many statements reached the database.
    """
    seen: list[str] = []

    def count(*args: object) -> None:
        """Note one statement.

        Parameters
        ----------
        *args
            What SQLAlchemy passes.
        """
        seen.append(str(args[2]))

    engine = star.session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        ask()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return len(seen)


def test_a_lineage_walk_reads_no_columns_however_far_it_goes(
    star: StarWorld, principal: PrincipalRef
) -> None:
    service = _service(star)

    def walk(depth: int) -> Callable[[], object]:
        return lambda: service.lineage(
            principal, "dev", "bronze.sales_raw", direction=LineageWay.DOWN, depth=depth, limit=1
        )

    # One page of one table either way; depth 10 visits four tables, depth 1 two.
    assert _statements(star, walk(10)) == _statements(star, walk(1))


def test_pages_carry_every_node_and_each_edge_once(
    star: StarWorld, principal: PrincipalRef
) -> None:
    service = _service(star)
    whole = service.neighbours(principal, "dev", "gold.fact_sales")
    nodes, edges, cursor = [], [], None
    while True:
        page = service.neighbours(principal, "dev", "gold.fact_sales", limit=2, cursor=cursor)
        nodes += page.nodes
        edges += page.edges
        cursor = page.next_cursor
        if cursor is None:
            break
    assert nodes == list(whole.nodes)
    assert sorted(edges) == sorted(whole.edges) and len(edges) == len(set(edges))
    first = service.lineage(principal, "dev", "gold.fact_sales", limit=1)
    with pytest.raises(ValidationError) as caught:
        service.neighbours(principal, "dev", "gold.fact_sales", cursor=first.next_cursor)
    assert "not valid for the `graph-neighbours` list" in str(caught.value)


def test_a_cycle_across_two_repositories_ends_with_each_node_once(
    star: StarWorld, principal: PrincipalRef
) -> None:
    _add_repository(star, "loop", [asset_entry("bronze.sales_raw", ("gold.fact_sales",), "x")])
    page = _service(star).lineage(
        principal, "dev", "bronze.sales_raw", direction=LineageWay.BOTH, depth=10
    )
    ids = [n.id for n in page.nodes]
    assert len(ids) == len(set(ids))
    assert asset_node("loop", "bronze.sales_raw") in ids
    fact = next(n for n in page.nodes if n.name == "gold.fact_sales" and n.kind is _T)
    assert fact.distance == 1


def _pages(call: Callable[..., GraphPage]) -> list[GraphPage]:
    """Read every page of an answer, one node per page.

    Parameters
    ----------
    call
        The question, taking `limit` and `cursor`.

    Returns
    -------
    list of GraphPage
        The pages, in order.
    """
    pages = [call(limit=1, cursor=None)]
    while pages[-1].next_cursor is not None:
        pages.append(call(limit=1, cursor=pages[-1].next_cursor))
    return pages


def _fewest_steps(star: StarWorld, grants: Grants, ask: Callable[[GraphService], object]) -> int:
    """Find the smallest step budget a question fits in.

    Parameters
    ----------
    star
        The world.
    grants
        The reader.
    ask
        The question.

    Returns
    -------
    int
        The fewest steps that answer rather than refuse.

    Raises
    ------
    AssertionError
        If no budget under 200 steps answers.
    """
    for steps in range(200):
        try:
            ask(_service(star, grants, max_steps=steps))
        except ValidationError:
            continue
        return steps
    raise AssertionError("no budget under 200 steps answers")


@pytest.mark.parametrize("question", ["lineage", "neighbours"])
def test_nothing_hidden_changes_a_page_a_cursor_or_the_budget(
    star: StarWorld, principal: PrincipalRef, question: str
) -> None:
    reader = Grants({"gold.dim_store", "bronze.stores_raw", "gold.fact_sales"})

    def ask(service: GraphService, **kwargs: object) -> GraphPage:
        method = getattr(service, question)
        return method(principal, "dev", "gold.dim_store", **kwargs)

    def answer() -> tuple[list[GraphPage], int]:
        pages = _pages(lambda **kw: ask(_service(star, reader), **kw))
        return pages, _fewest_steps(star, reader, ask)

    before = answer()
    # Hidden from the reader: an asset that reads gold.dim_store and builds a table it may not read.
    _add_repository(
        star, "secret", [asset_entry("gold.secret", ("gold.dim_store",), "o", columns=())]
    )
    assert asset_node("secret", "gold.secret") in {
        n.id for n in ask(_service(star), limit=500, cursor=None).nodes
    }
    assert answer() == before


def test_the_step_budget_refuses_and_counts_only_what_the_reader_sees(
    star: StarWorld, principal: PrincipalRef
) -> None:
    with pytest.raises(ValidationError) as caught:
        _service(star, max_steps=3).lineage(principal, "dev", "gold.dim_store", depth=10)
    assert str(caught.value) == (
        "This question reaches more of the graph than one request may walk (3 edges). Ask a "
        "narrower one: a smaller --depth or one --direction for lineage, or --kind for neighbours."
    )
    alone = _service(star, Grants({"gold.dim_store"}, environment=False), max_steps=0)
    assert len(alone.lineage(principal, "dev", "gold.dim_store", depth=10).nodes) == 1


_BUSY = (
    "The graph walk took longer than 10 s and was stopped. Try again in a moment; if it keeps "
    "happening, ask a narrower question (a smaller --depth, one --direction, or --kind)."
)


def test_the_time_budget_refuses_with_busy_after_loading(
    star: StarWorld, principal: PrincipalRef
) -> None:
    clock = itertools.count(0.0, 100.0)
    with pytest.raises(GraphBusyError) as caught:
        _service(star, clock=lambda: next(clock)).neighbours(principal, "dev", "gold.dim_store")
    assert str(caught.value) == _BUSY


def test_the_time_budget_refuses_with_busy_while_walking(
    star: StarWorld, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph_service, "_CHECK_EVERY", 1)
    # Started, loaded, the visibility batch answered in time; then the walk runs out.
    clock = itertools.chain([0.0, 0.0, 0.0], itertools.repeat(100.0))
    with pytest.raises(GraphBusyError) as caught:
        _service(star, clock=lambda: next(clock)).lineage(principal, "dev", "gold.dim_store")
    assert str(caught.value) == _BUSY


@pytest.mark.parametrize("question", ["lineage", "neighbours"])
@pytest.mark.parametrize("cursor", ["%%%", "eyJrIjogMX0", "eyJrIjogInRlcm1zIiwgIm4iOiAieCJ9"])
def test_a_cursor_it_could_not_have_issued_is_refused_before_the_graph_is_read(
    star: StarWorld, principal: PrincipalRef, question: str, cursor: str
) -> None:
    ask = getattr(_service(star), question)
    refusals: list[str] = []

    def refused() -> None:
        """Ask with the cursor and note the refusal."""
        with pytest.raises(ValidationError) as caught:
            ask(principal, "dev", "gold.dim_store", cursor=cursor)
        refusals.append(str(caught.value))

    assert _statements(star, refused) == 0
    assert "cursor" in refusals[0]


def test_the_longest_cursor_fits_a_request_line() -> None:
    longest = graph_service.encode_key_cursor(
        "graph-lineage", f"000010|{chr(0x10FFFF) * graph_service.MAX_NODE_ID}"
    )
    assert len(longest) <= graph_service.MAX_GRAPH_CURSOR < 11_000
