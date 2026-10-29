"""The checks a header generates: names, SQL, failure modes, collisions and limits."""

from __future__ import annotations

import dataclasses

from test_projects_dag import make_asset

from lakana_core.projects.asset_model import ParsedAsset, ResolvedReference
from lakana_core.projects.checks import GeneratedFrom, ParsedCheck, check_issues
from lakana_core.projects.dag import build_graph
from lakana_core.projects.frontmatter import AssetMetadata
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.model_checks import (
    KEY_NOT_NULL,
    KEY_UNIQUE,
    REF_PREFIX,
    generated_checks,
    generated_name_issues,
    key_not_null_sql,
    key_unique_sql,
    reference_sql,
)
from lakana_core.query_rewrite import check_blacklist, extract_table_refs

_REF = ResolvedReference("store_id", "gold.dim_store", "store_id")


def _fact(**fields: object) -> ParsedAsset:
    """Build a fact with a key and one reference.

    Parameters
    ----------
    **fields
        Overrides of the metadata.

    Returns
    -------
    ParsedAsset
        The asset.
    """
    values: dict[str, object] = {
        "materialize": "table",
        "kind": "fact",
        "grain": "g",
        "key": ("date", "store_id"),
        "references": {"store_id": "gold.dim_store"},
        **fields,
    }
    meta = AssetMetadata(**values)  # ty: ignore[invalid-argument-type]
    return make_asset(
        "gold.fact_sales", ("gold.dim_store",), columns=("date", "store_id"), metadata=meta
    )


def _hand(check: str, table: str = "gold.fact_sales") -> ParsedCheck:
    """Build a hand-written check of a table.

    Parameters
    ----------
    check
        Its name.
    table
        The checked table.

    Returns
    -------
    ParsedCheck
        The check, at the path its table and name decide.
    """
    return ParsedCheck(
        name=f"{table}/{check}",
        table=table,
        check=check,
        path=f"checks/{table.replace('.', '/')}/{check}.sql",
        on_failure="block",
        description=None,
        sql=f"SELECT 1 FROM {table}",
        references=(table,),
        body_line=1,
    )


def test_the_names_are_the_spec_names() -> None:
    assert (KEY_UNIQUE, KEY_NOT_NULL, REF_PREFIX) == ("key_unique", "key_not_null", "ref_")


def test_a_header_generates_three_checks_with_their_names_and_sources() -> None:
    checks = generated_checks([_fact()], {"gold.fact_sales": (_REF,)}, default_on_failure="block")
    assert [c.name for c in checks] == [
        "gold.fact_sales/key_not_null",
        "gold.fact_sales/key_unique",
        "gold.fact_sales/ref_store_id",
    ]
    assert {c.path for c in checks} == {"assets/gold/fact_sales.sql:1"}
    assert checks[0].generated == GeneratedFrom("key", ("date", "store_id"))
    assert checks[0].references == ("gold.fact_sales",)
    assert checks[0].description == "generated from `key:`"
    assert checks[2].generated == GeneratedFrom(
        "references", ("store_id",), "gold.dim_store", "store_id"
    )
    assert checks[2].references == ("gold.dim_store", "gold.fact_sales")
    assert checks[2].description == "generated from `references: store_id`"


def test_a_generated_check_points_at_the_header_block() -> None:
    asset = dataclasses.replace(_fact(), block_line=3, body_line=9)
    checks = generated_checks([asset], {}, default_on_failure="block")
    assert {(c.path, c.block_line, c.body_line) for c in checks} == {
        ("assets/gold/fact_sales.sql:3", 3, 3)
    }


def test_the_failure_mode_comes_from_the_header_then_the_project() -> None:
    checks = generated_checks(
        [_fact(checks={"references": "warn"})],
        {"gold.fact_sales": (_REF,)},
        default_on_failure="block",
    )
    assert {c.check: c.on_failure for c in checks} == {
        "key_not_null": "block",
        "key_unique": "block",
        "ref_store_id": "warn",
    }
    assert not any(c.from_default for c in checks)
    warned = generated_checks([_fact()], {"gold.fact_sales": (_REF,)}, default_on_failure="warn")
    assert all(c.on_failure == "warn" and c.from_default for c in warned)
    own = generated_checks(
        [_fact(checks={"key": "block"})], {"gold.fact_sales": (_REF,)}, default_on_failure="warn"
    )
    assert {c.check: c.from_default for c in own} == {
        "key_not_null": False,
        "key_unique": False,
        "ref_store_id": True,
    }


def test_key_none_generates_no_key_checks() -> None:
    meta = AssetMetadata(materialize="table", kind="fact", grain="g", key="none")
    assert (
        generated_checks([make_asset("gold.r", metadata=meta)], {}, default_on_failure="block")
        == []
    )


def test_a_merge_key_generates_the_key_checks() -> None:
    """Every merge declares a key, so every merge asset gains the two key checks (breaking)."""
    meta = AssetMetadata(materialize="table", write="merge", key=("id",))
    checks = generated_checks(
        [make_asset("silver.m", metadata=meta)], {}, default_on_failure="block"
    )
    assert [c.name for c in checks] == ["silver.m/key_not_null", "silver.m/key_unique"]


def test_the_sql_is_one_select_the_guard_passes_and_reads_the_right_tables() -> None:
    for sql, tables in (
        (key_unique_sql("gold.fact_sales", ["date", "store_id"]), {"gold.fact_sales"}),
        (key_not_null_sql("gold.fact_sales", ["date"]), {"gold.fact_sales"}),
        (
            reference_sql("gold.fact_sales", "store_id", "gold.dim_store", "store_id"),
            {"gold.fact_sales", "gold.dim_store"},
        ),
    ):
        check_blacklist(sql, dialect="duckdb")
        assert set(extract_table_refs(sql, dialect="duckdb")) == tables


def test_a_hand_written_check_may_not_take_a_generated_name() -> None:
    generated = generated_checks(
        [_fact()], {"gold.fact_sales": (_REF,)}, default_on_failure="block"
    )
    hand = dataclasses.replace(_hand("key_unique"), path="checks/gold/fact_sales/key_unique.sql")
    issues = generated_name_issues([hand], generated)
    assert [(i.path, i.line, i.message) for i in issues] == [
        (
            "checks/gold/fact_sales/key_unique.sql",
            None,
            "A check named `key_unique` is generated for `gold.fact_sales`; rename "
            "`checks/gold/fact_sales/key_unique.sql`.",
        )
    ]


def test_a_hand_written_check_with_a_generated_name_elsewhere_is_accepted() -> None:
    """The F26 ruling: only the name the check's own table's header generates is refused."""
    generated = generated_checks(
        [_fact()], {"gold.fact_sales": (_REF,)}, default_on_failure="block"
    )
    hands = [
        _hand("ref_region"),
        _hand("key_unique", "gold.dim_store"),
        _hand("ref_store_id", "gold.dim_store"),
    ]
    assert generated_name_issues(hands, generated) == []


def test_generated_checks_count_toward_the_limit_and_keep_is_refused_on_a_merge() -> None:
    merge = _fact(write="merge", checks={"key": "keep"})
    generated = generated_checks([merge], {"gold.fact_sales": (_REF,)}, default_on_failure="block")
    issues = check_issues(
        generated, [merge], build_graph([merge]), limits=DeployLimits(max_checks_per_table=2)
    )
    messages = [i.message for i in issues]
    assert any(
        m.startswith("`checks: key: keep` cannot be used on `gold.fact_sales`") for m in messages
    )
    assert any(
        "more than 2 checks of table 'gold.fact_sales'" in m and "3 of them generated" in m
        for m in messages
    )


def test_the_keep_refusal_of_a_generated_check_names_the_header_setting() -> None:
    """Keep on a view or an append is refused for a generated check too; the fix is the header's."""
    view = _fact(materialize="view", checks={"references": "keep"})
    generated = generated_checks([view], {"gold.fact_sales": (_REF,)}, default_on_failure="block")
    (issue,) = check_issues(generated, [view], build_graph([view]))
    assert (issue.path, issue.line) == ("assets/gold/fact_sales.sql", 1)
    assert issue.message == (
        "`checks: references: keep` cannot be used on `gold.fact_sales`: it is a view and holds "
        "no data to keep. Use `checks: references: block`: for a view it stops what reads it in "
        "that run."
    )
    append = _fact(write="append", watermark="date", checks={"key": "keep"})
    generated = generated_checks([append], {}, default_on_failure="block")
    (issue,) = check_issues(generated, [append], build_graph([append]))
    assert issue.message.startswith(
        "`checks: key: keep` cannot be used on `gold.fact_sales`: it is an"
    )
    assert "Use `checks: key: block`" in issue.message


def test_keep_on_a_table_asset_is_accepted_for_a_generated_check() -> None:
    table = _fact(checks={"key": "keep"})
    generated = generated_checks([table], {}, default_on_failure="block")
    assert check_issues(generated, [table], build_graph([table])) == []


def test_the_limit_directory_comes_from_a_hand_written_check_when_there_is_one() -> None:
    asset = _fact()
    generated = generated_checks([asset], {}, default_on_failure="block")
    limits = DeployLimits(max_checks_per_table=2)
    graph = build_graph([asset])
    (alone,) = check_issues([*generated, _hand("a")], [asset], graph, limits=limits)
    assert (alone.path, alone.line) == ("checks/gold/fact_sales", None)
    assert alone.message == (
        "more than 2 checks of table 'gold.fact_sales'; combine checks or split the table "
        "(2 of them generated from the asset's header)"
    )
    only_generated = generated_checks(
        [asset], {"gold.fact_sales": (_REF,)}, default_on_failure="block"
    )
    (issue,) = check_issues(only_generated, [asset], graph, limits=limits)
    assert issue.path == "assets/gold/fact_sales.sql"


def test_a_hand_written_check_whose_name_differs_only_in_case_is_refused() -> None:
    """Discovery treats `Key_Unique.sql` and `key_unique.sql` as one check; so does this rule."""
    generated = generated_checks(
        [_fact()], {"gold.fact_sales": (_REF,)}, default_on_failure="block"
    )
    hands = [_hand("Key_Unique"), _hand("REF_STORE_ID")]
    assert [i.message for i in generated_name_issues(hands, generated)] == [
        "A check named `Key_Unique` is generated for `gold.fact_sales`; rename "
        "`checks/gold/fact_sales/Key_Unique.sql`.",
        "A check named `REF_STORE_ID` is generated for `gold.fact_sales`; rename "
        "`checks/gold/fact_sales/REF_STORE_ID.sql`.",
    ]


def test_keep_on_a_family_is_refused_once_however_many_checks_it_generates() -> None:
    merge = _fact(
        write="merge",
        checks={"key": "keep", "references": "keep"},
        references={"store_id": "gold.dim_store", "date": "gold.dim_date"},
    )
    refs = (_REF, ResolvedReference("date", "gold.dim_date", "date"))
    generated = generated_checks([merge], {"gold.fact_sales": refs}, default_on_failure="block")
    messages = [i.message for i in check_issues(generated, [merge], build_graph([merge]))]
    assert [m.split(" cannot")[0] for m in messages] == [
        "`checks: key: keep`",
        "`checks: references: keep`",
    ]
