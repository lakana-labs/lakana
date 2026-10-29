"""Reference targets and key columns, against the project and a stand-in environment."""

from __future__ import annotations

from collections.abc import Callable

from test_projects_dag import make_asset

from lakana_core.projects.asset_model import ParsedAsset, ResolvedReference
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.model import (
    TargetInfo,
    project_model_issues,
    project_reference_target,
    resolve_references,
)

_DIM = TargetInfo(kind="dimension", key=("store_id",), key_none=False, columns=("store_id", "n"))


def _fact(
    references: dict[str, str], columns: tuple[str, ...] | None = ("store_id", "id")
) -> ParsedAsset:
    """Build a fact asset with the given references.

    Parameters
    ----------
    references
        Column to target.
    columns
        Its output columns, or `None` for a star.

    Returns
    -------
    ParsedAsset
        The asset.
    """
    meta = AssetMetadata(
        materialize="table",
        kind="fact",
        grain="One row per order.",
        key=("id",),
        references=references,
    )
    return make_asset("gold.fact_orders", ("gold.dim_store",), columns=columns, metadata=meta)


def _lookup(table: dict[str, TargetInfo]) -> Callable[[str], TargetInfo | None]:
    """Look up targets in a dict.

    Parameters
    ----------
    table
        Dotted name to info.

    Returns
    -------
    callable
        The lookup.
    """
    return table.get


def test_the_short_form_points_at_the_single_key_column() -> None:
    resolved, issues = resolve_references(
        [_fact({"store_id": "gold.dim_store"})],
        _lookup({"gold.dim_store": _DIM}),
        environment="dev",
    )
    assert issues == []
    assert resolved == {
        "gold.fact_orders": (ResolvedReference("store_id", "gold.dim_store", "store_id"),)
    }


def test_a_target_without_a_key_needs_the_long_form() -> None:
    keyless = TargetInfo(kind=None, key=(), key_none=False, columns=("id",))
    _, issues = resolve_references(
        [_fact({"store_id": "gold.dim_store"})],
        _lookup({"gold.dim_store": keyless}),
        environment="dev",
    )
    assert [i.message for i in issues] == [
        "Reference `store_id: gold.dim_store`: `gold.dim_store` declares no key. Declare `key:` "
        "in its asset, or name the column: `store_id: gold.dim_store.<column>`."
    ]


def test_a_composite_key_needs_the_long_form() -> None:
    wide = TargetInfo(kind="dimension", key=("a", "b"), key_none=False, columns=("a", "b"))
    _, issues = resolve_references(
        [_fact({"store_id": "gold.dim_store"})],
        _lookup({"gold.dim_store": wide}),
        environment="dev",
    )
    assert [i.message for i in issues] == [
        "Reference `store_id: gold.dim_store`: `gold.dim_store` has a key of 2 columns; name "
        "the column: `store_id: gold.dim_store.<column>`."
    ]


def test_the_long_form_names_a_column_the_target_has() -> None:
    _, issues = resolve_references(
        [_fact({"store_id": "gold.dim_store.nope"})],
        _lookup({"gold.dim_store": _DIM}),
        environment="dev",
    )
    assert [i.message for i in issues] == [
        "Reference `store_id: gold.dim_store.nope`: `gold.dim_store` has no column `nope`; name "
        "one of its columns, or use the short form to point at its key."
    ]


def test_a_missing_or_unreadable_target_reads_the_same() -> None:
    _, issues = resolve_references(
        [_fact({"store_id": "gold.dim_store"})], _lookup({}), environment="prod"
    )
    assert [i.message for i in issues] == [
        "Reference target `gold.dim_store` does not exist in environment `prod`, or you may not "
        "read it; build it first, or ask for READ_TABLE on it."
    ]


def test_a_modelled_table_may_not_reference_a_staging_table() -> None:
    staging = TargetInfo(kind="staging", key=("store_id",), key_none=False, columns=("store_id",))
    _, issues = resolve_references(
        [_fact({"store_id": "gold.dim_store"})],
        _lookup({"gold.dim_store": staging}),
        environment="dev",
    )
    assert [i.message for i in issues] == [
        "`gold.fact_orders` (a fact) references `gold.dim_store`, a staging table; point it at a "
        "modelled table."
    ]


def test_a_target_that_reads_two_ways_is_refused() -> None:
    both = {"a.b": _DIM, "a.b.store_id": _DIM}
    _, issues = resolve_references(
        [_fact({"store_id": "a.b.store_id"})], _lookup(both), environment="dev"
    )
    assert [i.message for i in issues] == [
        "Reference `store_id: a.b.store_id`: `a.b.store_id` is a table, and so is `a.b`, which "
        "has a column `store_id`; rename one of them so that the reference names one thing."
    ]


def test_a_shorter_table_without_that_column_does_not_make_it_ambiguous() -> None:
    """`a.b` is known not to have `x`, so `a.b.x` can only be the table."""
    tables = {"a.b": _DIM, "a.b.x": _DIM}
    resolved, issues = resolve_references(
        [_fact({"store_id": "a.b.x"})], _lookup(tables), environment="dev"
    )
    assert issues == []
    assert resolved["gold.fact_orders"] == (ResolvedReference("store_id", "a.b.x", "store_id"),)


def test_a_shorter_table_with_unknown_columns_reads_two_ways() -> None:
    unknown = TargetInfo(kind="dimension", key=("store_id",), key_none=False, columns=None)
    tables = {"a.b": unknown, "a.b.store_id": _DIM}
    _, issues = resolve_references(
        [_fact({"store_id": "a.b.store_id"})], _lookup(tables), environment="dev"
    )
    assert [i.message for i in issues] == [
        "Reference `store_id: a.b.store_id`: `a.b.store_id` is a table, and so is `a.b`, which "
        "would read `store_id` as its column; rename one of them so that the reference names one "
        "thing."
    ]


def test_project_reference_target_prefers_the_whole_name() -> None:
    assert project_reference_target("gold.dim_store", {"gold.dim_store"}) == "gold.dim_store"
    assert project_reference_target("gold.dim_store.id", {"gold.dim_store"}) == "gold.dim_store"
    assert project_reference_target("gold.other", {"gold.dim_store"}) is None


def test_key_and_reference_columns_must_be_in_an_explicit_select() -> None:
    asset = _fact({"region_id": "gold.dim_region"}, columns=("store_id",))
    assert [i.message for i in project_model_issues([asset])] == [
        "Key column `id` is not a column of `gold.fact_orders`'s SELECT; add it to the SELECT, "
        "or change `key:`.",
        "Reference column `region_id` is not a column of `gold.fact_orders`'s SELECT; add it to "
        "the SELECT, or remove the reference.",
    ]


def test_a_star_select_defers_the_columns_to_the_run() -> None:
    assert project_model_issues([_fact({"x": "gold.dim_store"}, columns=None)]) == []


def test_a_name_the_lookup_cannot_describe_reads_as_missing() -> None:
    """An `is_table` that passes a name the lookup does not know never leaks a half answer."""
    _, issues = resolve_references(
        [_fact({"store_id": "gold.dim_store"})],
        _lookup({}),
        environment="dev",
        is_table=lambda name: True,
    )
    assert [i.message for i in issues] == [
        "Reference target `gold.dim_store` does not exist in environment `dev`, or you may not "
        "read it; build it first, or ask for READ_TABLE on it."
    ]


def test_the_long_form_resolves_to_the_named_column() -> None:
    """A known column, and any column of a target whose columns are not known yet."""
    unknown = TargetInfo(kind=None, key=(), key_none=False, columns=None)
    resolved, issues = resolve_references(
        [_fact({"store_id": "gold.dim_store.n", "id": "gold.other.any"})],
        _lookup({"gold.dim_store": _DIM, "gold.other": unknown}),
        environment="dev",
    )
    assert issues == []
    assert resolved["gold.fact_orders"] == (
        ResolvedReference("id", "gold.other", "any"),
        ResolvedReference("store_id", "gold.dim_store", "n"),
    )


def test_key_and_reference_columns_the_select_returns_pass() -> None:
    assert project_model_issues([_fact({"store_id": "gold.dim_store"})]) == []
