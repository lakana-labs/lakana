"""Reference targets and key columns, against the project and a stand-in environment."""

from __future__ import annotations

from collections.abc import Callable

from test_projects_dag import make_asset

from lakana_core.projects.asset_model import ParsedAsset, ResolvedReference
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.issues import error_for_reader, withheld_lines
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
    built: dict[str, tuple[str, ...] | None] = {"gold.dim_store": ("id",)}
    assert project_reference_target("gold.dim_store", built) == "gold.dim_store"
    assert project_reference_target("gold.dim_store.id", built) == "gold.dim_store"
    assert project_reference_target("gold.dim_store.id", {"gold.dim_store": None}) == (
        "gold.dim_store"
    )
    assert project_reference_target("gold.other", built) is None


def test_project_reference_target_reads_a_name_as_the_deploy_does() -> None:
    """A table part whose known columns lack the last part is not the target, as in `_split`."""
    assert project_reference_target("gold.dim_store.nope", {"gold.dim_store": ("id",)}) is None
    both: dict[str, tuple[str, ...] | None] = {"a.b": ("x",), "a.b.store_id": ("id",)}
    assert project_reference_target("a.b.store_id", both) == "a.b.store_id"
    unknown: dict[str, tuple[str, ...] | None] = {"a.b": None, "a.b.store_id": ("id",)}
    assert project_reference_target("a.b.store_id", unknown) == "a.b.store_id"


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


def test_an_artifact_is_no_reference_target_in_either_form() -> None:
    """An artifact has no rows, so nothing points at it; the deployer may read it: it is named."""
    artifact = TargetInfo(kind=None, key=(), key_none=False, columns=None, artifact=True)
    for raw in ("gold.model", "gold.model.id"):
        resolved, issues = resolve_references(
            [_fact({"store_id": raw})], _lookup({"gold.model": artifact}), environment="dev"
        )
        assert resolved == {}
        assert [i.message for i in issues] == [
            f"Reference `store_id: {raw}`: `gold.model` is an artifact, not a table, so no "
            "column can point at it; point the reference at a table."
        ]


def test_a_reference_too_long_to_store_is_refused_with_what_to_shorten() -> None:
    """The catalog stores each part in 1,024 bytes; a longer one would fail the deploy late."""
    long_table = "gold." + "n" * 1020 + ".dim"
    long_column = "c" * 1025
    targets = {long_table: _DIM, "gold.dim_store": _DIM}
    cases = {
        ("store_id", long_table): f"the target table `{long_table}` has 1029 bytes",
        ("store_id", f"gold.dim_store.{long_column}"): (
            f"the target column `{long_column}` has 1025 bytes"
        ),
        (long_column, "gold.dim_store"): f"the column `{long_column}` has 1025 bytes",
    }
    wide = TargetInfo(kind="dimension", key=("store_id",), key_none=False, columns=None)
    for (column, raw), part in cases.items():
        lookup = _lookup({**targets, "gold.dim_store": wide})
        resolved, issues = resolve_references([_fact({column: raw})], lookup, environment="dev")
        assert resolved == {}
        assert [i.message for i in issues] == [
            f"Reference `{column}: {raw}` is too long to store: {part}, and each of the column, "
            "the target table and the target column may have at most 1024 bytes; shorten that "
            "name."
        ]
    ok, issues = resolve_references(
        [_fact({"c" * 1024: "gold.dim_store"})], _lookup(targets), environment="dev"
    )
    assert issues == [] and ok


def test_an_issue_that_tells_of_a_target_carries_it_and_the_missing_text() -> None:
    """The deploy's stored error reaches every reader of the environment, not only the deployer.

    Each issue that says something of a target the lookup found (that it exists, its kind, its
    key, its columns) names that target in `targets`, and `withheld` is the text of a missing
    target, so a reader who may not read it can be shown what a missing one shows: one line
    per reference, so a second problem of the same reference has an empty `withheld`.
    """
    missing = (
        "Reference target `{raw}` does not exist in environment `dev`, or you may not read it; "
        "build it first, or ask for READ_TABLE on it."
    )
    staging = TargetInfo(kind="staging", key=("a", "b"), key_none=False, columns=("a", "b"))
    artifact = TargetInfo(kind=None, key=(), key_none=False, columns=None, artifact=True)
    cases = [
        ({"store_id": "gold.dim_store"}, {"gold.dim_store": staging}, ("gold.dim_store",)),
        ({"store_id": "gold.dim_store.nope"}, {"gold.dim_store": _DIM}, ("gold.dim_store",)),
        ({"store_id": "gold.model"}, {"gold.model": artifact}, ("gold.model",)),
        (
            {"store_id": "a.b.store_id"},
            {"a.b": _DIM, "a.b.store_id": _DIM},
            ("a.b.store_id", "a.b"),
        ),
        ({"c" * 1025: "gold.dim_store"}, {"gold.dim_store": _DIM}, ("gold.dim_store",)),
    ]
    for references, targets, told in cases:
        _, issues = resolve_references([_fact(references)], _lookup(targets), environment="dev")
        assert issues
        raw = next(iter(references.values()))
        first, *others = issues
        assert (first.targets, first.withheld) == (told, missing.format(raw=raw))
        assert [(i.targets, i.withheld) for i in others] == [(told, "")] * len(others)
    _, plain = resolve_references([_fact({"store_id": "gold.x"})], _lookup({}), environment="dev")
    assert [(i.targets, i.withheld) for i in plain] == [((), None)]


def test_two_references_to_one_hidden_target_read_like_two_missing_targets() -> None:
    """The staging line names no column, so both references make the same line: by position."""
    staging = TargetInfo(kind="staging", key=("store_id",), key_none=False, columns=("store_id",))
    fact = _fact({"a_id": "dim.store", "b_id": "dim.store.store_id"})
    _, issues = resolve_references([fact], _lookup({"dim.store": staging}), environment="dev")
    assert issues[0].message == issues[1].message
    whole = "\n".join(issue.render() for issue in issues)
    _, missing = resolve_references([fact], _lookup({}), environment="dev")
    shown = error_for_reader(whole, withheld_lines(issues), lambda _t: False)
    assert shown.encode() == "\n".join(issue.render() for issue in missing).encode()


def _history(columns: tuple[str, ...] | None, **fields: object) -> ParsedAsset:
    """Build a history dimension with the given SELECT columns.

    Parameters
    ----------
    columns
        Its output columns, or `None` for a star.
    **fields
        More metadata.

    Returns
    -------
    ParsedAsset
        The asset.
    """
    meta = AssetMetadata(
        materialize="table",
        kind="dimension",
        grain="One row per customer and version.",
        key=("customer_id",),
        write="history",
        **fields,  # ty: ignore[invalid-argument-type]
    )
    return make_asset("gold.dim_customer", ("silver.customers",), columns=columns, metadata=meta)


def test_a_history_select_may_not_return_an_added_name_and_must_return_valid_from() -> None:
    issues = project_model_issues(
        [_history(("customer_id", "VALID_TO", "region"), valid_from="changed_at")]
    )
    assert [i.message for i in issues] == [
        "Column `VALID_TO` of `gold.dim_customer`'s SELECT has a name `write: history` adds "
        "itself (valid_from, valid_to, is_current, dim_customer_key); rename it in the SELECT, "
        "for example `AS source_VALID_TO`.",
        "Column `changed_at` in `valid_from:` is not a column of `gold.dim_customer`'s SELECT; "
        "add it to the SELECT, or change `valid_from:`.",
    ]
    assert project_model_issues([_history(None, valid_from="changed_at")]) == []
    assert (
        project_model_issues([_history(("customer_id", "changed_at"), valid_from="changed_at")])
        == []
    )


def test_a_valid_from_column_spelled_in_another_case_is_named_as_the_select_spells_it() -> None:
    """The column check is exact (the run reads the result's own names), so say how to match it."""
    issues = project_model_issues(
        [_history(("customer_id", "Changed_At"), valid_from="changed_at")]
    )
    assert [i.message for i in issues] == [
        "Column `changed_at` in `valid_from:` is not a column of `gold.dim_customer`'s SELECT, "
        "which spells it `Changed_At`; write `valid_from: Changed_At`, or rename the column in "
        "the SELECT to `changed_at`.",
    ]
