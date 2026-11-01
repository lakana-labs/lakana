"""The rules of `kind`, `key` and the write model, checked on an asset by itself."""

from __future__ import annotations

import pytest

from lakana_core.errors import ValidationError
from lakana_core.projects.asset_model import (
    key_columns,
    key_issues,
    model_issues,
    require_model_kind,
    write_rule_issues,
)
from lakana_core.projects.frontmatter import AssetMetadata, consistency_issues

_T = "gold.fact_orders"


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            AssetMetadata(kind="fact", key=("id",)),
            [
                "Asset `gold.fact_orders` is a fact and needs `grain:`: say in one sentence "
                "what one row is."
            ],
        ),
        (
            AssetMetadata(kind="fact", grain="One row per order."),
            [
                "Asset `gold.fact_orders` is a fact and needs `key:`: list the columns that "
                "identify one row (usually the grain's columns), or write `key: none` and say in "
                "`grain` why no columns are unique."
            ],
        ),
        (
            AssetMetadata(kind="dimension", grain="One row per customer.", key="none"),
            [
                "Asset `gold.fact_orders` is a dimension and needs `key:`; `key: none` is not "
                "allowed for a dimension, because nothing could refer to it."
            ],
        ),
        (AssetMetadata(kind="wide", grain="One row per order.", key="none"), []),
        (AssetMetadata(kind="staging"), []),
        (AssetMetadata(key=("id",)), []),
        (
            AssetMetadata(key="none"),
            [
                "`key: none` is for a fact or a wide table whose rows no columns identify; "
                "declare `kind: fact` or `kind: wide`, or remove `key: none`"
            ],
        ),
        (
            AssetMetadata(checks={"references": "warn"}),
            [
                "`checks: references` sets how the reference checks fail, but the asset "
                "declares no `references:`; declare `references:`, or remove `checks: references`"
            ],
        ),
        (
            AssetMetadata(checks={"key": "warn"}),
            [
                "`checks: key` sets how the key checks fail, but the asset declares no `key:`; "
                "declare `key:`, or remove `checks: key`"
            ],
        ),
    ],
)
def test_the_kind_rules(metadata: AssetMetadata, expected: list[str]) -> None:
    """The kind rules of the spec, with its fixed texts."""
    assert model_issues(metadata, table=_T) == expected


def test_a_key_has_at_most_eight_columns_a_merge_key_too() -> None:
    """`key_issues` alone holds the cap; since generated key checks, a merge key has it too."""
    nine = tuple(f"c{i}" for i in range(9))
    too_many = (
        "a key has at most 8 columns, and `key:` lists 9; shorten it to the columns that "
        "identify one row"
    )
    assert key_issues(nine) == [too_many]
    assert key_issues(nine[:8]) == []
    assert key_issues("none") == []
    replaced = AssetMetadata(materialize="table", key=nine)
    messages = [i.message for i in consistency_issues(replaced, path="assets/gold/f.sql", line=1)]
    assert messages.count(too_many) == 1
    merged = AssetMetadata(materialize="table", write="merge", key=nine)
    messages = [i.message for i in consistency_issues(merged, path="assets/gold/f.sql", line=1)]
    assert messages.count(too_many) == 1


def test_a_key_column_must_not_be_empty() -> None:
    empty = "a key column must not be empty; write the name of a column of the asset"
    assert key_issues(("",)) == [empty]
    assert key_issues(("id", "")) == [empty]


@pytest.mark.parametrize(
    ("write", "key", "watermark", "materialize", "expected"),
    [
        ("replace", ("id",), None, "table", []),
        (None, ("id",), None, "table", []),
        ("append", ("id",), "ts", "table", []),
        (None, ("id",), None, "view", []),
        (None, ("id",), None, "incremental", []),
        (
            "replace",
            (),
            "ts",
            "table",
            ["`write: replace` takes no `watermark:`; to add only new rows use `write: append`"],
        ),
        (
            "merge",
            (),
            None,
            "table",
            ["`write: merge` needs `key:` — the columns that identify a row"],
        ),
        (
            "merge",
            "none",
            None,
            "table",
            [
                "`write: merge` needs key columns; `key: none` says that no columns identify a "
                "row, so there is nothing to merge on"
            ],
        ),
    ],
)
def test_the_write_rules_take_the_one_key(
    write: str | None,
    key: tuple[str, ...] | str,
    watermark: str | None,
    materialize: str,
    expected: list[str],
) -> None:
    """A key identifies a row for every write mode; only a merge needs one."""
    assert (
        write_rule_issues(write, key, watermark, materialize=materialize, evolve_schema=None)
        == expected
    )


def test_key_columns_reads_none_as_no_columns() -> None:
    """`none` has no columns; a tuple is its columns."""
    assert key_columns("none") == ()
    assert key_columns(("a", "b")) == ("a", "b")


@pytest.mark.parametrize("kind", ["fact", "dimension", "wide", "staging"])
def test_require_model_kind_accepts_each_kind(kind: str) -> None:
    assert require_model_kind(kind) is None


def test_require_model_kind_names_the_kinds_and_the_value() -> None:
    with pytest.raises(ValidationError) as raised:
        require_model_kind("cube")
    assert (
        str(raised.value) == "model_kind must be one of fact, dimension, wide, staging; got 'cube'"
    )


def test_require_model_kind_escapes_what_it_repeats() -> None:
    with pytest.raises(ValidationError, match=r"got '\\n'"):
        require_model_kind("\n")


_DIM = {"kind": "dimension", "grain": "One row per customer and version.", "key": ("customer_id",)}


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({**_DIM, "write": "history"}, []),
        ({**_DIM, "write": "history", "valid_from": "changed_at"}, []),
        (
            {"write": "history", "key": ("customer_id",)},
            [
                "`write: history` keeps the versions of a dimension's rows; declare "
                "`kind: dimension`, with `grain:` and `key:`"
            ],
        ),
        (
            {**_DIM, "write": "history", "kind": "fact"},
            [
                "`write: history` keeps the versions of a dimension's rows; declare "
                "`kind: dimension`, with `grain:` and `key:`"
            ],
        ),
        (
            {**_DIM, "write": "history", "columns": {"Is_Current": "Mine."}},
            [
                "`columns: Is_Current`: `write: history` adds this column and describes it "
                "itself; remove it from `columns:`"
            ],
        ),
        (
            {
                **_DIM,
                "write": "history",
                "valid_from": "changed_at",
                "columns": {"changed_at": "When the CRM changed the row."},
                "references": {"changed_at": "gold.dim_calendar"},
            },
            [
                "`columns: changed_at`: `changed_at` is the `valid_from:` column, which "
                "`write: history` does not store (its value becomes each version's `valid_from`, "
                "which Lakana describes itself); remove it from `columns:`",
                "`references: changed_at`: `changed_at` is the `valid_from:` column, which "
                "`write: history` does not store (its value becomes each version's "
                "`valid_from`), so no check can read it; remove it from `references:`",
            ],
        ),
    ],
)
def test_history_is_for_dimensions_and_describes_its_own_columns(
    fields: dict[str, object], expected: list[str]
) -> None:
    metadata = AssetMetadata(**fields)  # ty: ignore[invalid-argument-type]
    assert model_issues(metadata, table="gold.dim_customer") == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"write": "history", "valid_from": "changed_at", "close_missing": True}, []),
        (
            {"write": "history", "watermark": "changed_at"},
            [
                "`write: history` takes no `watermark:`; its SELECT returns the current state of "
                "every row, and each run compares it with the open versions"
            ],
        ),
        (
            {"write": "merge", "valid_from": "changed_at"},
            ["`valid_from:` works with `write: history` only"],
        ),
        ({"close_missing": False}, ["`close_missing:` works with `write: history` only"]),
        (
            {"write": "history", "valid_from": "customer_id"},
            [
                "`valid_from: customer_id` names a key column; name the column that says when "
                "the row's values took effect, or leave `valid_from:` out to use the run's start "
                "time"
            ],
        ),
        (
            {"write": "history", "evolve_schema": True},
            ["`evolve_schema` works with `write: append` only"],
        ),
        (
            {"write": "history", "materialize": "view"},
            ["`materialize: view` cannot take `write:`"],
        ),
        (
            {"write": "history", "materialize": "incremental", "incremental_key": "t"},
            ["use one: `write: append` with `watermark:` replaces `materialize: incremental`"],
        ),
    ],
)
def test_the_write_rules_of_history(fields: dict[str, object], expected: list[str]) -> None:
    metadata = AssetMetadata(key=("customer_id",), **fields)  # ty: ignore[invalid-argument-type]
    assert (
        write_rule_issues(
            metadata.write,
            metadata.key,
            metadata.watermark,
            materialize=metadata.materialize,
            evolve_schema=metadata.evolve_schema,
            valid_from=metadata.valid_from,
            close_missing=metadata.close_missing,
        )
        == expected
    )


def test_a_block_passes_valid_from_and_close_missing_to_the_rules() -> None:
    metadata = AssetMetadata(materialize="table", valid_from="changed_at", close_missing=True)
    messages = [i.message for i in consistency_issues(metadata, path="a.sql", line=1)]
    assert messages == [
        "`valid_from:` works with `write: history` only",
        "`close_missing:` works with `write: history` only",
    ]
