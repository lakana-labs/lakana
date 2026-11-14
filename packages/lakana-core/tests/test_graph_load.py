"""Loading an environment's metadata graph: every node and edge, before any visibility (#374)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from graph_test_helpers import (
    TABLES,
    Grants,
    StarWorld,
    asset_entry,
    check_entry,
    point,
    python_entry,
    sales_manifest,
)
from pyiceberg.types import NestedField, StringType
from sqlalchemy import event, select

from lakana_core.auth import PrincipalRef
from lakana_core.catalog import graph_service
from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.catalog.glossary import write_glossary
from lakana_core.catalog.glossary_read import term_names
from lakana_core.catalog.graph_load import (
    AssetRecord,
    CheckRecord,
    EnvironmentGraph,
    JobRecord,
    ManifestPart,
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
from lakana_core.catalog.graph_service import GraphService
from lakana_core.errors import GraphBusyError
from lakana_core.models import (
    ColumnMask,
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


def _bind_the_mask(star: StarWorld, *, binding: str = "field_ids") -> str:
    """Bind the star's mask on ``dim_store.store_name`` (field id 2) the way #120 does.

    Parameters
    ----------
    star
        The star.
    binding
        The binding state to store.

    Returns
    -------
    str
        The mask's node id.
    """
    mask = star.session.scalars(select(ColumnMask)).one()
    mask.binding = binding
    mask.field_ids = {"store_name": 2} if binding == "field_ids" else None
    mask.column_field_id = 2 if binding == "field_ids" else None
    star.session.flush()
    return star.policy(NodeKind.COLUMN_MASK)


def _fields(*names: str) -> Callable[[uuid.UUID], tuple[NestedField, ...]]:
    """Give a table's current fields: the given names with the ids 1, 2, ...

    Parameters
    ----------
    *names
        The columns' current names.

    Returns
    -------
    Callable
        What `load_graph` takes as ``fields_of``.
    """
    fields = tuple(NestedField(i + 1, n, StringType(), required=False) for i, n in enumerate(names))
    return lambda _table_id: fields


def test_a_mask_follows_its_column_through_a_rename(star: StarWorld) -> None:
    """#460: the MASKS edge points at the column's current name, resolved by field id."""
    mask = _bind_the_mask(star)
    dim = star.table_id("gold.dim_store")
    graph = load_graph(star.session, star.env_id, "dev", fields_of=_fields("store_id", "shop_name"))
    assert _edges(graph, mask) == {(EdgeKind.MASKS, mask, column_node(dim, "shop_name"))}


def test_a_mask_not_renamed_keeps_its_column(star: StarWorld) -> None:
    """A bound mask whose column kept its name links the same node."""
    mask = _bind_the_mask(star)
    dim = star.table_id("gold.dim_store")
    graph = load_graph(
        star.session, star.env_id, "dev", fields_of=_fields("store_id", "store_name")
    )
    assert _edges(graph, mask) == {(EdgeKind.MASKS, mask, column_node(dim, "store_name"))}


@pytest.mark.parametrize("case", ["unbound", "no_reader", "unreadable", "gone"])
def test_a_mask_that_cannot_be_resolved_keeps_the_name_it_was_made_with(
    star: StarWorld, case: str
) -> None:
    """An unbound policy, no schema reader, a failing one, or a gone field: today's link."""
    mask = _bind_the_mask(star, binding="unbound" if case == "unbound" else "field_ids")
    dim = star.table_id("gold.dim_store")
    fields_of = {
        "unbound": _fields("store_id", "shop_name"),
        "no_reader": None,
        "unreadable": lambda _table_id: None,
        "gone": _fields("store_id"),
    }[case]
    graph = load_graph(star.session, star.env_id, "dev", fields_of=fields_of)
    assert _edges(graph, mask) == {(EdgeKind.MASKS, mask, column_node(dim, "store_name"))}


@pytest.mark.parametrize("policies", [True, False])
def test_current_fields_are_read_only_for_a_reader_who_sees_masks(
    star: StarWorld, principal: PrincipalRef, policies: bool
) -> None:
    """The schema is read for bound masks only when the reader holds MANAGE_POLICY."""
    _bind_the_mask(star)
    asked: list[uuid.UUID] = []

    def spy(table_id: uuid.UUID, seconds: float) -> tuple[NestedField, ...]:
        """Record the read and give a renamed schema.

        Parameters
        ----------
        table_id
            The table read.
        seconds
            How long the read may take.

        Returns
        -------
        tuple of NestedField
            Its fields.
        """
        asked.append(table_id)
        return _fields("store_id", "shop_name")(table_id)

    service = GraphService(star.session, Grants(policies=policies), "en", fields_of=spy)
    service.neighbours(principal, "dev", "gold.dim_store")
    assert bool(asked) is policies


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


def _bound_mask(star: StarWorld, principal: PrincipalRef, dotted: str, column: str) -> None:
    """Add a column mask bound to field id 1 (#120) on a table of the star.

    Parameters
    ----------
    star
        The star.
    principal
        The mask's subject and creator.
    dotted
        The table.
    column
        The masked column.
    """
    namespace, _, name = dotted.partition(".")
    star.session.add(
        ColumnMask(
            id=uuid.uuid4(),
            principal_id=principal.id,
            environment="dev",
            namespace=namespace,
            table=name,
            column=column,
            environment_id=star.env_id,
            table_id=star.table_id(dotted),
            created_by=principal.id,
            created_at=datetime.now(UTC),
            expression="NULL",
            binding="field_ids",
            field_ids={column: 1},
            column_field_id=1,
        )
    )
    star.session.flush()


def test_mask_fields_are_read_once_per_table_and_stop_while_time_is_left(
    star: StarWorld, principal: PrincipalRef
) -> None:
    """PR #465's review: slow storage falls back to the stored names, never to GraphBusyError."""
    _bind_the_mask(star)
    _bound_mask(star, principal, "gold.dim_store", "store_id")  # a second mask, same table
    for dotted in ("gold.fact_sales", "gold.dim_date", "silver.sales"):
        _bound_mask(star, principal, dotted, TABLES[dotted][0][0])
    now = [0.0]
    asked: list[tuple[uuid.UUID, float]] = []

    def slow(table_id: uuid.UUID, seconds: float) -> tuple[NestedField, ...]:
        """Take three seconds per read, as storage that hangs before it answers.

        Parameters
        ----------
        table_id
            The table read.
        seconds
            How long the read may take.

        Returns
        -------
        tuple of NestedField
            Its fields.
        """
        asked.append((table_id, seconds))
        now[0] += 3.0
        return _fields("store_id", "shop_name")(table_id)

    service = GraphService(
        star.session, Grants(policies=True), "en", clock=lambda: now[0], fields_of=slow
    )
    page = service.neighbours(principal, "dev", "gold.dim_store")
    assert page.nodes  # answered, not GraphBusyError
    # Each table once, and no read starts once half the 10 s budget is spent.
    assert len({table_id for table_id, _ in asked}) == len(asked) == 2
    assert [seconds for _, seconds in asked] == [5.0, 2.0]


def test_a_failing_mask_field_reader_keeps_the_stored_names(
    star: StarWorld, principal: PrincipalRef
) -> None:
    mask = _bind_the_mask(star)
    dim = star.table_id("gold.dim_store")
    service = GraphService(
        star.session, Grants(policies=True), "en", fields_of=lambda _table_id, _seconds: None
    )
    page = service.neighbours(principal, "dev", mask)
    assert column_node(dim, "store_name") in {n.id for n in page.nodes}


def test_the_graph_load_stops_at_the_budget_between_repositories(
    star: StarWorld, principal: PrincipalRef
) -> None:
    """Batch B review M1: the budget is read during the load, after each repository's part."""
    now = [0.0]
    read: list[str] = []

    def slow(version: uuid.UUID, repository: str, manifest: object) -> ManifestPart:
        """Take three seconds per repository, past a 2 s budget after the first.

        Parameters
        ----------
        version
            The project version.
        repository
            The repository.
        manifest
            Its manifest.

        Returns
        -------
        ManifestPart
            Its part.
        """
        read.append(repository)
        now[0] += 3.0
        return manifest_part(repository, manifest)

    service = GraphService(
        star.session, Grants(), "en", clock=lambda: now[0], budget_seconds=2.0, part=slow
    )
    with pytest.raises(GraphBusyError):
        service.breakage(principal, "dev", ["gold.dim_store"])
    assert len(read) == 1  # stopped after the first part, not after loading both


def test_lineage_is_read_only_from_an_entry_that_lists_its_columns_and_skips_junk() -> None:
    entry = asset_entry("gold.x", ("gold.a",), "o", columns=("c",))
    entry["lineage"] = {
        "c": [{"table": "gold.a", "column": "k"}, "junk", {"table": 1, "column": "k"}],
        "d": "junk",
        7: [],
    }
    (asset,) = manifest_part("r", {"assets": [entry]}).assets
    assert asset.lineage == (("c", "gold.a", "k"),)


def test_mask_fields_are_cached_per_table() -> None:
    """Each table read once per request, whoever asks again (`shown_policies` asks once too)."""
    read: list[uuid.UUID] = []
    fields = graph_service._MaskFields(
        lambda table_id, _seconds: read.append(table_id) or (),
        graph_service._Budget(lambda: 0.0, 10.0, 10),
    )
    table = uuid.uuid4()
    assert fields(table) == fields(table) == ()
    assert read == [table]


def test_an_asset_reads_its_connections_as_a_python_asset_declares_them() -> None:
    entries = [
        python_entry("bronze.a", ["pg-sales", "crm-db"]),
        python_entry("bronze.b", "crm-db"),
        python_entry("bronze.c", ["pg-sales", "../x"]),
        {**asset_entry("gold.dim_store", ("bronze.stores_raw",), "o"), "connections": ["pg-sales"]},
    ]
    assets = manifest_part("r", {"assets": entries}).assets
    assert [(a.name, a.connections) for a in assets] == [
        ("bronze.a", ("pg-sales", "crm-db")),
        ("bronze.b", ()),
        ("bronze.c", ()),
        ("gold.dim_store", ()),
    ]


def test_a_graph_read_stays_silent_about_a_damaged_connections_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a deploy warns about a damaged entry; every graph request would repeat it."""
    with caplog.at_level("WARNING"):
        assets = manifest_part(
            "r", {"assets": [python_entry("bronze.c", ["pg-sales", "../x"])]}
        ).assets
    assert [a.connections for a in assets] == [()]
    assert caplog.records == []
