"""The DAG and project-level validation (pure part)."""

from __future__ import annotations

from lakana_core.projects.dag import build_graph, find_cycle, validate_project
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import Requirements, ResolvedProjectConfig


def make_asset(
    table: str,
    references: tuple[str, ...] = (),
    *,
    columns: tuple[str, ...] | None = ("a",),
    metadata: AssetMetadata | None = None,
) -> ParsedAsset:
    """Build a parsed asset without going through the parser.

    Parameters
    ----------
    table
        Dotted table name.
    references
        Tables it reads.
    columns
        Its output columns, or `None` for a star projection.
    metadata
        Merged metadata; `materialize: table` by default.

    Returns
    -------
    ParsedAsset
        The asset, body on line 1 with no block.
    """
    sql = "SELECT a FROM " + (references[0] if references else "bronze.src")
    return ParsedAsset(
        table=table,
        path=f"assets/{table.replace('.', '/')}.sql",
        block_line=None,
        body_line=1,
        metadata=metadata or AssetMetadata(materialize="table"),
        sql=sql,
        normalized_sql=sql,
        references=references,
        columns=columns,
        lineage={},
    )


def test_edges_are_internal_and_external_inputs_are_the_rest() -> None:
    """A reference produced in the repo is an edge; anything else is an external input."""
    assets = [
        make_asset("bronze.raw", ("landing.events",)),
        make_asset("silver.clean", ("bronze.raw", "ref.countries")),
    ]
    graph = build_graph(assets)
    assert graph.edges == (("bronze.raw", "silver.clean"),)
    assert graph.external_inputs == ("landing.events", "ref.countries")


def test_self_reference_is_neither_an_edge_nor_an_input() -> None:
    """A refresh reading its own target is not a cycle."""
    graph = build_graph([make_asset("gold.t", ("gold.t",))])
    assert graph.edges == ()
    assert graph.external_inputs == ()
    assert find_cycle([make_asset("gold.t", ("gold.t",))]) is None


def test_cycle_is_found_with_its_path() -> None:
    """The error names the whole path, ending where it started."""
    assets = [
        make_asset("a.x", ("a.z",)),
        make_asset("a.y", ("a.x",)),
        make_asset("a.z", ("a.y",)),
        make_asset("a.free"),
    ]
    assert find_cycle(assets) == ["a.x", "a.z", "a.y", "a.x"]
    issues = validate_project(assets, ResolvedProjectConfig())
    assert [issue.message for issue in issues] == ["cycle: a.x -> a.z -> a.y -> a.x"]
    assert issues[0].path == "assets/a/x.sql"


def test_required_fields_come_from_front_matter_or_defaults() -> None:
    """`requirements.fields` is checked on merged metadata, one issue per missing field."""
    config = ResolvedProjectConfig(
        requirements=Requirements(fields=("description", "owner", "tags"))
    )
    good = make_asset(
        "s.good",
        metadata=AssetMetadata(materialize="table", description="d", owner="o", tags=("t",)),
    )
    bad = make_asset("s.bad", metadata=AssetMetadata(materialize="table", owner="o"))
    issues = validate_project([good, bad], config)
    assert [(i.path, i.message) for i in issues] == [
        ("assets/s/bad.sql", "missing required field 'description'"),
        ("assets/s/bad.sql", "missing required field 'tags'"),
    ]


def test_documented_columns_must_exist_when_projection_is_explicit() -> None:
    """A documented column the query doesn't produce is an error; skipped for SELECT *."""
    metadata = AssetMetadata(materialize="table", columns={"a": "ok", "ghost": "gone"})
    issues = validate_project(
        [make_asset("s.t", columns=("a",), metadata=metadata)], ResolvedProjectConfig()
    )
    assert [i.message for i in issues] == [
        "documents column 'ghost', which the query does not produce"
    ]
    assert (
        validate_project(
            [make_asset("s.t", columns=None, metadata=metadata)], ResolvedProjectConfig()
        )
        == []
    )


def test_column_docs_requirement() -> None:
    """`column_docs: true` needs every explicit column documented and every expression aliased."""
    config = ResolvedProjectConfig(requirements=Requirements(column_docs=True))
    metadata = AssetMetadata(materialize="table", columns={"a": "doc"})
    issues = validate_project(
        [make_asset("s.t", columns=("a", "b", ""), metadata=metadata)], config
    )
    assert [i.message for i in issues] == [
        "column 'b' is not documented",
        "column 3 has no name; alias it so it can be documented",
    ]
    assert validate_project([make_asset("s.star", columns=None, metadata=metadata)], config) == []


def test_all_issues_are_collected_across_assets() -> None:
    """A cycle and a missing field are both reported."""
    config = ResolvedProjectConfig(requirements=Requirements(fields=("owner",)))
    assets = [make_asset("a.x", ("a.y",)), make_asset("a.y", ("a.x",))]
    messages = [i.message for i in validate_project(assets, config)]
    assert "cycle: a.x -> a.y -> a.x" in messages
    assert messages.count("missing required field 'owner'") == 2


def test_diamond_and_external_references_are_not_cycles() -> None:
    """Revisiting a finished node, or reading an unproduced table, is no cycle."""
    assets = [
        make_asset("a.top", ("a.left", "a.right")),
        make_asset("a.left", ("a.base", "landing.x")),
        make_asset("a.right", ("a.base",)),
        make_asset("a.base"),
    ]
    assert find_cycle(assets) is None
    assert validate_project(assets, ResolvedProjectConfig()) == []


def _chain(length: int, *, closed: bool) -> list:
    """Build a linear dependency chain `t0 -> t1 -> ... `.

    Parameters
    ----------
    length
        Number of assets.
    closed
        Whether the last asset reads `t0`, closing a cycle.

    Returns
    -------
    list
        The assets.
    """
    assets = [make_asset(f"t.t{i}", (f"t.t{i + 1}",)) for i in range(length - 1)]
    assets.append(make_asset(f"t.t{length - 1}", ("t.t0",) if closed else ()))
    return assets


def test_deep_chain_does_not_overflow_the_stack() -> None:
    """A 1500-deep chain is acyclic and checked without recursion."""
    assets = _chain(1500, closed=False)
    assert find_cycle(assets) is None
    assert validate_project(assets, ResolvedProjectConfig()) == []
    assert len(build_graph(assets).edges) == 1499


def test_deep_cycle_is_reported_with_its_full_path() -> None:
    """A 1500-chain closing on itself reports every member."""
    cycle = find_cycle(_chain(1500, closed=True))
    assert cycle is not None
    assert cycle == [*(f"t.t{i}" for i in range(1500)), "t.t0"]


def _python_asset(table: str, inputs: tuple[str, ...]) -> ParsedAsset:
    """Build a parsed Python asset that reads `inputs`.

    Parameters
    ----------
    table
        Dotted table name.
    inputs
        The tables it reads.

    Returns
    -------
    ParsedAsset
        A Python asset with those inputs, one parameter per input.
    """
    return ParsedAsset(
        table=table,
        path=f"assets/{table.replace('.', '/')}.py",
        block_line=1,
        body_line=1,
        metadata=AssetMetadata(materialize="table"),
        sql="",
        normalized_sql="python:sha256:0",
        references=inputs,
        columns=None,
        lineage={},
        kind="python",
        inputs=tuple((f"p{i}", name) for i, name in enumerate(inputs)),
    )


def test_a_cycle_through_a_python_asset_is_found_and_named() -> None:
    """A Python asset takes part in the graph like a SQL one."""
    assets = [
        make_asset("a.x", ("a.z",)),
        _python_asset("a.y", ("a.x",)),
        make_asset("a.z", ("a.y",)),
    ]
    assert find_cycle(assets) == ["a.x", "a.z", "a.y", "a.x"]
    messages = [issue.message for issue in validate_project(assets, ResolvedProjectConfig())]
    assert "cycle: a.x -> a.z -> a.y -> a.x" in messages


def test_a_reference_orders_the_target_first() -> None:
    dim = make_asset("gold.dim_store")
    fact = make_asset(
        "gold.fact",
        columns=("store_id",),
        metadata=AssetMetadata(materialize="table", references={"store_id": "gold.dim_store"}),
    )
    assert ("gold.dim_store", "gold.fact") in build_graph([dim, fact]).edges


def test_a_long_form_reference_orders_its_table_first_and_an_outside_target_is_no_input() -> None:
    """The long form's table part is the edge; a target no asset builds adds nothing."""
    dim = make_asset("gold.dim_store")
    fact = make_asset(
        "gold.fact",
        metadata=AssetMetadata(
            materialize="table",
            references={"store_id": "gold.dim_store.id", "region": "ref.regions.code"},
        ),
    )
    graph = build_graph([dim, fact])
    assert graph.edges == (("gold.dim_store", "gold.fact"),)
    assert graph.external_inputs == ()


def test_mutual_references_are_a_cycle_and_a_self_reference_is_not() -> None:
    a = make_asset(
        "gold.a", metadata=AssetMetadata(materialize="table", references={"b_id": "gold.b"})
    )
    b = make_asset(
        "gold.b", metadata=AssetMetadata(materialize="table", references={"a_id": "gold.a"})
    )
    issues = validate_project([a, b], ResolvedProjectConfig())
    assert [i.message for i in issues] == [
        "cycle: gold.a -> gold.b -> gold.a (a reference in `references:` counts as a dependency: "
        "the target is built first)"
    ]
    assert find_cycle([a, b], with_references=False) is None
    me = make_asset(
        "gold.e", metadata=AssetMetadata(materialize="table", references={"m": "gold.e"})
    )
    assert validate_project([me], ResolvedProjectConfig()) == []


def _referencing(references: dict[str, str]) -> AssetMetadata:
    """Build the metadata of a table asset that declares references.

    Parameters
    ----------
    references
        Column to target.

    Returns
    -------
    AssetMetadata
        The metadata.
    """
    return AssetMetadata(materialize="table", references=references)


def test_the_reference_hint_names_a_reference_cycle_beside_a_sql_cycle() -> None:
    """The hint depends on the cycle reported, not on whether a SQL cycle exists elsewhere."""
    a = make_asset("a.x", metadata=_referencing({"y_id": "a.y"}))
    b = make_asset("a.y", ("a.x",))
    sql_one = make_asset("z.p", ("z.q",))
    sql_two = make_asset("z.q", ("z.p",))
    issues = validate_project([a, b, sql_one, sql_two], ResolvedProjectConfig())
    assert [i.message for i in issues] == [
        "cycle: a.x -> a.y -> a.x (a reference in `references:` counts as a dependency: "
        "the target is built first)"
    ]
    assert [i.message for i in validate_project([sql_one, sql_two], ResolvedProjectConfig())] == [
        "cycle: z.p -> z.q -> z.p"
    ]
