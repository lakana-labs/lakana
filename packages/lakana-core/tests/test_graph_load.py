"""Loading an environment's metadata graph: every node and edge, before any visibility (#374)."""

from __future__ import annotations

import uuid

from graph_test_helpers import (
    StarWorld,
    asset_entry,
    check_entry,
    point,
    sales_manifest,
)
from sqlalchemy import event, select

from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.catalog.glossary import write_glossary
from lakana_core.catalog.glossary_read import term_names
from lakana_core.catalog.graph_load import (
    AssetRecord,
    CheckRecord,
    EnvironmentGraph,
    JobRecord,
    load_graph,
    manifest_part,
)
from lakana_core.catalog.graph_model import (
    EdgeKind,
    NodeKind,
    NodeRef,
    asset_node,
    check_node,
    column_node,
    job_node,
    table_node,
)
from lakana_core.models import (
    GlossaryTerm,
    Namespace,
    Repository,
    RepositoryKind,
    Table,
    TableColumn,
    TableType,
)
from lakana_core.projects.checks import read_manifest_checks
from lakana_core.projects.lookups import current_manifests
from lakana_core.projects.model_checks import KEY_NOT_NULL, KEY_UNIQUE


def _asset(graph: EnvironmentGraph, node_id: str) -> AssetRecord:
    """Read an asset record.

    Parameters
    ----------
    graph
        The loaded graph.
    node_id
        The asset's id.

    Returns
    -------
    AssetRecord
        The record.
    """
    record = graph.records[node_id]
    assert isinstance(record, AssetRecord)
    return record


def _job(graph: EnvironmentGraph, node_id: str) -> JobRecord:
    """Read a job record.

    Parameters
    ----------
    graph
        The loaded graph.
    node_id
        The job's id.

    Returns
    -------
    JobRecord
        The record.
    """
    record = graph.records[node_id]
    assert isinstance(record, JobRecord)
    return record


def _check(graph: EnvironmentGraph, node_id: str) -> CheckRecord:
    """Read a check record.

    Parameters
    ----------
    graph
        The loaded graph.
    node_id
        The check's id.

    Returns
    -------
    CheckRecord
        The record.
    """
    record = graph.records[node_id]
    assert isinstance(record, CheckRecord)
    return record


def _graph(star: StarWorld) -> EnvironmentGraph:
    """Load the star's graph.

    Parameters
    ----------
    star
        The world.

    Returns
    -------
    EnvironmentGraph
        The graph.
    """
    return load_graph(star.session, star.env_id, "dev")


def _edges(graph: EnvironmentGraph, node_id: str) -> set[tuple[EdgeKind, str, str]]:
    """Flatten a node's edges.

    Parameters
    ----------
    graph
        The loaded graph.
    node_id
        The node.

    Returns
    -------
    set
        ``(kind, source, target)`` per edge.
    """
    return {(e.kind, e.source, e.target) for e in graph.edges(node_id)}


def test_the_helpers_build_checks_the_reader_accepts() -> None:
    assert len(read_manifest_checks(sales_manifest()).specs) == 3


def test_the_star_loads_every_kind_of_edge(star: StarWorld) -> None:
    graph = _graph(star)
    dim, fact = star.table_id("gold.dim_store"), star.table_id("gold.fact_sales")
    ref = star.reference("gold.fact_sales", "store_id")
    store_id = column_node(dim, "store_id")
    assert _edges(graph, store_id) == {
        (EdgeKind.HAS_COLUMN, table_node(dim), store_id),
        (EdgeKind.REFERENCES, ref, store_id),
        (EdgeKind.CHECKS, check_node(dim, KEY_UNIQUE), store_id),
        (EdgeKind.CHECKS, check_node(dim, KEY_NOT_NULL), store_id),
        (EdgeKind.LINKS, star.term("store"), store_id),
    }
    assert _edges(graph, ref) == {
        (EdgeKind.REFERENCES, ref, column_node(fact, "store_id")),
        (EdgeKind.REFERENCES, ref, table_node(dim)),
        (EdgeKind.REFERENCES, ref, store_id),
        (EdgeKind.CHECKS, check_node(fact, "ref_store_id"), ref),
    }
    fact_asset = asset_node("sales", "gold.fact_sales")
    assert _edges(graph, fact_asset) == {
        (EdgeKind.BUILDS, fact_asset, table_node(fact)),
        (EdgeKind.READS, fact_asset, table_node(star.table_id("silver.sales"))),
        (EdgeKind.READS, fact_asset, table_node(dim)),
        (EdgeKind.RUNS, job_node("sales", "nightly"), fact_asset),
    }
    assert (
        EdgeKind.MEASURES,
        star.measure("gold.fact_sales", "net_amount"),
        column_node(fact, "net_amount"),
    ) in _edges(graph, column_node(fact, "net_amount"))
    fact_edges = _edges(graph, table_node(fact))
    assert (EdgeKind.FILTERS, star.policy(NodeKind.ROW_FILTER), table_node(fact)) in fact_edges
    assert (EdgeKind.CHECKS, check_node(fact, "positive_amount"), table_node(fact)) in fact_edges
    assert (EdgeKind.HAS_COLUMN, table_node(fact), column_node(fact, "date")) in fact_edges
    mask = star.policy(NodeKind.COLUMN_MASK)
    assert _edges(graph, mask) == {(EdgeKind.MASKS, mask, column_node(dim, "store_name"))}
    assert _job(graph, job_node("sales", "nightly")).assets == (
        "gold.dim_date",
        "gold.fact_sales",
    )
    assert graph.builder_of[dim].repository == "stores"
    assert graph.columns_of(fact) == {"date": "date", "store_id": "long", "net_amount": "double"}


def test_a_named_form_resolves_to_the_id_and_a_missing_one_to_none(star: StarWorld) -> None:
    graph = _graph(star)
    dim = star.table_id("gold.dim_store")
    assert graph.resolve(NodeRef(NodeKind.TABLE, "gold.dim_store")) == table_node(dim)
    assert graph.resolve(NodeRef(NodeKind.TABLE, str(dim))) == table_node(dim)
    assert graph.resolve(NodeRef(NodeKind.COLUMN, "gold.dim_store/store_id")) == column_node(
        dim, "store_id"
    )
    assert graph.resolve(NodeRef(NodeKind.COLUMN, f"{dim}/store_id")) == column_node(
        dim, "store_id"
    )
    fact = star.table_id("gold.fact_sales")
    assert graph.resolve(NodeRef(NodeKind.CHECK, "gold.fact_sales/ref_store_id")) == check_node(
        fact, "ref_store_id"
    )
    assert graph.resolve(NodeRef(NodeKind.TERM, "store")) == star.term("store")
    assert graph.resolve(NodeRef(NodeKind.ASSET, "sales/gold.fact_sales")) == asset_node(
        "sales", "gold.fact_sales"
    )
    for missing in (
        NodeRef(NodeKind.TABLE, "gold.dim_store.store_id"),
        NodeRef(NodeKind.COLUMN, "gold.dim_store/nope"),
        NodeRef(NodeKind.COLUMN, "nope"),
        NodeRef(NodeKind.REFERENCE, "not-a-uuid"),
        NodeRef(NodeKind.MEASURE, str(uuid.uuid4())),
        NodeRef(NodeKind.TERM, "nope"),
        NodeRef(NodeKind.JOB, "sales/nope"),
        NodeRef(NodeKind.TABLE, str(uuid.uuid4())),
    ):
        assert graph.resolve(missing) is None


def test_a_reference_keeps_its_id_across_a_changed_target_and_a_rollback(star: StarWorld) -> None:
    """R1: the deploy updates a reference in place while its source table and column stay."""
    before = star.reference("gold.fact_sales", "store_id")
    changed = sales_manifest()
    changed["assets"][2]["model"]["references"][0]["target"] = "gold.dim_date"
    sync_project_descriptions(star.session, star.env_id, changed)
    star.session.flush()
    assert star.reference("gold.fact_sales", "store_id") == before
    # A rollback writes the older manifest the same way (`DeployService.execute_rollback`).
    sync_project_descriptions(star.session, star.env_id, sales_manifest())
    star.session.flush()
    graph = _graph(star)
    assert graph.resolve(NodeRef(NodeKind.REFERENCE, before.partition(":")[2])) == before


def test_a_damaged_manifest_reads_as_what_it_can(star: StarWorld) -> None:
    broken = Repository(
        id=uuid.uuid4(), name="broken", kind=RepositoryKind.HTTPS, url="https://h/b"
    )
    star.session.add(broken)
    star.session.flush()
    point(
        star.session,
        star.env_id,
        broken,
        {
            "version": 13,
            "assets": [
                {"table": 5},
                "junk",
                {"table": "gold.x", "references": [1, None], "inputs": {"p": 3}, "metadata": 7},
            ],
            "checks": [{"name": "x"}, check_entry("gold.fact_sales", "ok", family="key")],
            "jobs": {"j": {"select": ["nothing.*"]}, "k": {"select": "gold.x"}, 7: {}},
        },
    )
    graph = _graph(star)
    asset = _asset(graph, asset_node("broken", "gold.x"))
    assert (asset.reads, asset.owner) == ((), None)
    assert _job(graph, job_node("broken", "j")).assets == ()
    assert _job(graph, job_node("broken", "k")).assets == ()
    assert manifest_part("r", "not a manifest").assets == ()


def test_two_repositories_claiming_one_table_or_check_read_deterministically(
    star: StarWorld,
) -> None:
    copy = Repository(id=uuid.uuid4(), name="copy", kind=RepositoryKind.HTTPS, url="https://h/c")
    star.session.add(copy)
    star.session.flush()
    point(
        star.session,
        star.env_id,
        copy,
        {
            "version": 13,
            "assets": [asset_entry("gold.fact_sales", ("silver.sales",), "copy-team")],
            "checks": [check_entry("gold.fact_sales", "positive_amount")],
        },
    )
    graph = _graph(star)
    fact = star.table_id("gold.fact_sales")
    builders = {s for k, s, _ in _edges(graph, table_node(fact)) if k is EdgeKind.BUILDS}
    assert builders == {
        asset_node("copy", "gold.fact_sales"),
        asset_node("sales", "gold.fact_sales"),
    }
    assert _check(graph, check_node(fact, "positive_amount")).repository == "copy"
    assert graph.builder_of[fact].repository == "copy"


def test_loading_costs_as_many_statements_for_six_tables_as_for_forty(star: StarWorld) -> None:
    def count() -> int:
        seen: list[object] = []
        engine = star.session.get_bind()

        def listener(*args: object) -> None:
            seen.append(args[2])

        event.listen(engine, "before_cursor_execute", listener)
        try:
            load_graph(star.session, star.env_id, "dev")
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        return len(seen)

    small = count()
    namespace_id = star.session.scalars(
        select(Namespace.id).where(
            Namespace.environment_id == star.env_id, Namespace.path == "bronze"
        )
    ).one()
    for i in range(34):
        table = Table(
            id=uuid.uuid4(),
            namespace_id=namespace_id,
            name=f"extra_{i}",
            table_type=TableType.ICEBERG,
            metadata_location=None,
            previous_metadata_location=None,
        )
        star.session.add(table)
        star.session.add(
            TableColumn(id=uuid.uuid4(), table=table, name="a", type="long", position=0)
        )
    star.session.flush()
    assert count() == small


def test_columns_are_read_once_in_chunks_and_only_when_asked(star: StarWorld) -> None:
    graph = _graph(star)
    dim = star.table_id("gold.dim_store")
    seen: list[object] = []
    engine = star.session.get_bind()

    def listener(*args: object) -> None:
        seen.append(args[2])

    event.listen(engine, "before_cursor_execute", listener)
    try:
        graph.preload_columns(list(graph.tables))
        graph.columns_of(dim)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert len(seen) == 1


def test_term_names_follow_the_reader_language(star: StarWorld) -> None:
    term_id = star.session.scalars(
        select(GlossaryTerm.id).where(GlossaryTerm.term_key == "net_revenue")
    ).one()
    assert term_names(star.session, [term_id], ("fr",), "en") == {term_id: "CA net"}
    assert term_names(star.session, [term_id], ("de",), "en") == {term_id: "Net revenue"}
    assert term_names(star.session, [], ("fr",), "en") == {}


def test_current_manifests_lists_every_repository_or_all_but_one(star: StarWorld) -> None:
    every = current_manifests(star.session, star.env_id)
    assert [name for _, name, _ in every] == ["sales", "stores"]
    others = current_manifests(star.session, star.env_id, exclude=star.repos["sales"].id)
    assert [name for _, name, _ in others] == ["stores"]
    assert isinstance(every[0][0], uuid.UUID)


def test_what_points_at_a_missing_table_adds_no_edge(star: StarWorld) -> None:
    """R6: a reference, a glossary link and an asset read name a table the environment lacks."""
    changed = sales_manifest()
    changed["assets"][2]["model"]["references"][0]["target"] = "gold.nope"
    changed["assets"][2]["references"].append("gold.nope")
    sync_project_descriptions(star.session, star.env_id, changed)
    point(star.session, star.env_id, star.repos["sales"], changed)
    ghost = Repository(id=uuid.uuid4(), name="ghost", kind=RepositoryKind.HTTPS, url="https://h/g")
    star.session.add(ghost)
    star.session.flush()
    manifest = {
        "version": 13,
        "assets": [asset_entry("gold.nope", ("gold.nope",), "ghost-team", columns=("a",))],
        "glossary": {
            "ghost": {
                "name": "Ghost",
                "definition": "Links nowhere.",
                "owner": "ghosts",
                "links": {"gold.nope": ["a"]},
                "related": [],
            }
        },
    }
    point(star.session, star.env_id, ghost, manifest)
    write_glossary(star.session, star.env_id, ghost.id, manifest)
    star.session.flush()
    graph = _graph(star)
    ref = star.reference("gold.fact_sales", "store_id")
    assert all("nope" not in e.target for e in graph.edges(ref))
    assert asset_node("ghost", "gold.nope") in graph.records
    assert _edges(graph, asset_node("ghost", "gold.nope")) == set()
    assert _edges(graph, star.term("ghost")) == set()


def test_exists_says_whether_a_canonical_id_is_there(star: StarWorld) -> None:
    graph = _graph(star)
    dim = star.table_id("gold.dim_store")
    assert graph.exists(table_node(dim))
    assert not graph.exists(table_node(uuid.uuid4()))
    assert graph.exists(column_node(dim, "store_id"))
    assert not graph.exists(column_node(dim, "nope"))
    assert graph.exists(star.term("store"))
