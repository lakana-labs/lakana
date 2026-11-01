"""The `@asset` decorator: the contract a Python asset is written against."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import lakana
import lakana.assets
from lakana.assets import ASSET_ATTRIBUTE, AssetSpec, asset


def test_decorator_returns_the_function_with_metadata() -> None:
    @lakana.asset(
        inputs={"obs": "silver.weather"},
        owner="data-eng",
        tags=["weather"],
        retries=2,
        retry_delay="30s",
        timeout="10m",
        columns={"score": "Anomaly score, 0-1."},
    )
    def build(obs: int) -> int:
        return obs + 1

    assert build(6) == 7
    assert getattr(build, ASSET_ATTRIBUTE) == AssetSpec(
        inputs={"obs": "silver.weather"},
        description=None,
        owner="data-eng",
        tags=("weather",),
        materialize="table",
        columns={"score": "Anomaly score, 0-1."},
        retries=2,
        retry_delay="30s",
        timeout="10m",
    )


def test_defaults_are_empty() -> None:
    @lakana.asset()
    def build() -> None:
        return None

    assert getattr(build, ASSET_ATTRIBUTE) == AssetSpec(
        inputs={},
        description=None,
        owner=None,
        tags=(),
        materialize="table",
        columns=None,
        retries=None,
        retry_delay=None,
        timeout=None,
    )


def test_mappings_are_copied() -> None:
    inputs = {"a": "bronze.a"}
    columns = {"x": "first"}

    @lakana.asset(inputs=inputs, columns=columns)
    def build(a: int) -> int:
        return a

    inputs["b"] = "bronze.b"
    columns["y"] = "second"
    spec = getattr(build, ASSET_ATTRIBUTE)
    assert spec.inputs == {"a": "bronze.a"}
    assert spec.columns == {"x": "first"}


def test_a_bare_decorator_says_to_add_parentheses() -> None:
    """`@asset` without a call."""
    with pytest.raises(TypeError, match=r"write @asset\(\.\.\.\) with parentheses"):

        @lakana.asset
        def build() -> None:
            return None


@pytest.mark.parametrize(
    ("materialize", "fragment"),
    [
        ("view", "only 'table'"),
        ("incremental", 'appends with write="append" and watermark="<column>"'),
        ("snapshot", "only 'table'"),
    ],
)
def test_only_table_materialization(materialize: str, fragment: str) -> None:
    with pytest.raises(ValueError, match=fragment):
        lakana.asset(materialize=materialize)


@pytest.mark.parametrize("removed", ["deps", "incremental_key"])
def test_removed_arguments_are_refused(removed: str) -> None:
    """`deps` and `incremental_key` no longer exist."""
    with pytest.raises(TypeError, match=removed):
        kwargs: dict[str, Any] = {removed: "x"}
        lakana.asset(**kwargs)


@pytest.fixture(autouse=True)
def _no_output_dir() -> Iterator[None]:
    """Leave the process-wide output directory unset before and after each test."""
    lakana.assets._set_output_dir(None)
    yield
    lakana.assets._set_output_dir(None)


def test_an_artifact_asset_stores_output_and_retention() -> None:
    @lakana.asset(inputs={"train": "gold.training_set"}, output="artifact", keep_versions=3)
    def churn_model(train: int) -> int:
        return train

    spec = getattr(churn_model, ASSET_ATTRIBUTE)
    assert (spec.output, spec.keep_versions, spec.materialize, spec.columns) == (
        "artifact",
        3,
        "table",
        None,
    )

    @lakana.asset()
    def plain() -> None:
        return None

    assert (
        getattr(plain, ASSET_ATTRIBUTE).output,
        getattr(plain, ASSET_ATTRIBUTE).keep_versions,
    ) == ("table", None)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"output": "model"}, "output must be 'table' or 'artifact', got 'model'"),
        (
            {"output": 10**5000},
            "output must be 'table' or 'artifact', got a number too large to show",
        ),
        (
            {"output": -(10**5000)},
            "output must be 'table' or 'artifact', got a number too large to show",
        ),
        ({"output": 3}, "output must be 'table' or 'artifact', got 3"),
        (
            {"output": "artifact", "materialize": "table"},
            "materialize does not apply to an artifact asset; remove it",
        ),
        (
            {"output": "artifact", "columns": {"a": "b"}},
            "columns does not apply to an artifact asset; remove it",
        ),
        (
            {"output": "artifact", "keep_versions": 0},
            "keep_versions must be an integer of at least 1, got 0",
        ),
        (
            {"output": "artifact", "keep_versions": True},
            "keep_versions must be an integer of at least 1, got True",
        ),
        (
            {"output": "artifact", "keep_versions": "2"},
            "keep_versions must be an integer of at least 1, got '2'",
        ),
        (
            {"output": "artifact", "keep_versions": 2**31},
            "keep_versions must be at most 2147483647; remove it to keep every version",
        ),
        (
            {"output": "artifact", "keep_versions": 10**5000},
            "keep_versions must be at most 2147483647; remove it to keep every version",
        ),
        (
            {"output": "artifact", "keep_versions": -(10**5000)},
            "keep_versions must be an integer of at least 1, got a negative number",
        ),
        ({"keep_versions": 2}, 'keep_versions applies only to an asset with output="artifact"'),
        (
            {"keep_versions": 10**5000},
            'keep_versions applies only to an asset with output="artifact"',
        ),
    ],
)
def test_artifact_arguments_are_checked(arguments: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError) as caught:
        lakana.asset(**arguments)  # ty: ignore[invalid-argument-type]
    assert str(caught.value) == message


def test_decimals_default_to_float_and_exact_is_stored() -> None:
    @lakana.asset()
    def plain() -> None:
        return None

    @lakana.asset(decimals="exact")
    def exact() -> None:
        return None

    @lakana.asset(decimals="float", output="artifact")
    def model() -> None:
        return None

    assert getattr(plain, ASSET_ATTRIBUTE).decimals == "float"
    assert getattr(exact, ASSET_ATTRIBUTE).decimals == "exact"
    assert getattr(model, ASSET_ATTRIBUTE).decimals == "float"


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        ("fixed", "'fixed'"),
        (None, "None"),
        (1, "1"),
        (True, "True"),
        (10**5000, "a number too large to show"),
    ],
    ids=["text", "none", "number", "bool", "huge"],
)
def test_a_decimals_value_other_than_float_or_exact_is_refused(value: object, shown: str) -> None:
    with pytest.raises(ValueError) as caught:
        lakana.asset(decimals=value)  # ty: ignore[invalid-argument-type]
    assert str(caught.value) == (
        f"decimals must be 'float' (Python floats, the default) or 'exact' (decimal.Decimal "
        f"values), got {shown}"
    )


def test_the_largest_retention_is_accepted() -> None:
    @lakana.asset(output="artifact", keep_versions=lakana.assets.MAX_KEEP_VERSIONS)
    def model() -> None:
        return None

    assert getattr(model, ASSET_ATTRIBUTE).keep_versions == 2**31 - 1


def test_output_dir_exists_only_inside_an_artifact_task(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as caught:
        lakana.output_dir()
    assert str(caught.value) == (
        'lakana.output_dir() is available only while an asset declared with output="artifact" '
        "runs as a task"
    )
    lakana.assets._set_output_dir(tmp_path)
    assert lakana.output_dir() == tmp_path
    assert lakana.assets.output_dir is lakana.output_dir
    assert "output_dir" in lakana.__all__
    lakana.assets._set_output_dir(None)
    with pytest.raises(RuntimeError):
        lakana.output_dir()


def test_connections_default_to_none_and_are_stored_as_a_tuple() -> None:
    @lakana.asset()
    def plain() -> None:
        return None

    @lakana.asset(connections=["crm-db", "files"])
    def declared() -> None:
        return None

    assert getattr(plain, ASSET_ATTRIBUTE).connections == ()
    assert getattr(declared, ASSET_ATTRIBUTE).connections == ("crm-db", "files")
    assert getattr(lakana.asset(connections=("a",))(plain), ASSET_ATTRIBUTE).connections == ("a",)


_RULE = (
    "connection names are lower-case letters, digits and '-', starting with a letter, "
    "at most 63; got "
)


@pytest.mark.parametrize(
    ("connections", "message"),
    [
        ("crm-db", "connections must be a list of strings"),
        ({"crm-db": "x"}, "connections must be a list of strings"),
        ([1], "connections must be a list of strings"),
        (["crm-db", "crm-db"], "connection `crm-db` is listed twice"),
        ([f"c{index}" for index in range(17)], "an asset declares at most 16 connections"),
        (["CRM"], _RULE + "'CRM'"),
        (["crm-db\n"], _RULE + "'crm-db\\n'"),
    ],
)
def test_a_wrong_connections_is_refused(connections: object, message: str) -> None:
    with pytest.raises(ValueError) as info:
        lakana.asset(connections=cast(Any, connections))
    assert str(info.value) == message


def test_the_write_model_defaults_to_replace_and_is_stored() -> None:
    @lakana.asset()
    def plain() -> None:
        return None

    spec = getattr(plain, ASSET_ATTRIBUTE)
    assert (spec.write, spec.key, spec.watermark) == ("replace", (), None)

    @lakana.asset(write="merge", key=["id", "region"], watermark="updated_at")
    def merged() -> None:
        return None

    spec = getattr(merged, ASSET_ATTRIBUTE)
    assert (spec.write, spec.key, spec.watermark) == ("merge", ("id", "region"), "updated_at")


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"write": "upsert"}, "write must be one of replace, append, merge, history; got 'upsert'"),
        (
            {"write": "append"},
            "`write: append` needs `watermark:` — the column whose largest value the next run "
            "starts after",
        ),
        ({"write": "merge"}, "`write: merge` needs `key:` — the columns that identify a row"),
        (
            {"watermark": "ts"},
            "`write: replace` takes no `watermark:`; to add only new rows use `write: append`",
        ),
        ({"write": "merge", "key": ["id", "id"]}, "key column `id` is listed twice"),
        ({"write": "merge", "key": 3}, "key must be a list of strings or one string"),
        (
            {"write": "merge", "key": "none"},
            "`write: merge` needs key columns; `key: none` says that no columns identify a row, "
            "so there is nothing to merge on",
        ),
    ],
)
def test_a_wrong_write_model_is_refused(arguments: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError) as caught:
        lakana.asset(**arguments)
    assert str(caught.value) == message


def test_a_key_is_accepted_with_every_write_mode() -> None:
    assert callable(lakana.asset(key=["id"]))
    assert callable(lakana.asset(write="append", watermark="ts", key=["id"]))
    assert callable(lakana.asset(write="merge", key=[f"c{i}" for i in range(8)]))
    for write in ("replace", "merge"):
        with pytest.raises(ValueError) as caught:
            lakana.asset(write=write, key=[f"c{i}" for i in range(9)])
        assert str(caught.value) == (
            "a key has at most 8 columns, and `key:` lists 9; shorten it to the columns that "
            "identify one row"
        )


_EVOLVE_NEEDS_REPLACE = (
    'evolve_schema=True works with write="replace" (the default) only; remove evolve_schema, '
    "or replace the table instead of appending, merging or keeping its history"
)


def test_evolve_schema_defaults_to_off_and_is_stored() -> None:
    @lakana.asset()
    def plain() -> None:
        return None

    assert getattr(plain, ASSET_ATTRIBUTE).evolve_schema is False

    @lakana.asset(evolve_schema=True)
    def evolving() -> None:
        return None

    assert getattr(evolving, ASSET_ATTRIBUTE).evolve_schema is True

    @lakana.asset(write="append", watermark="ts", evolve_schema=False)
    def appending() -> None:
        return None

    assert getattr(appending, ASSET_ATTRIBUTE).evolve_schema is False


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"write": "append", "watermark": "ts", "evolve_schema": True}, _EVOLVE_NEEDS_REPLACE),
        ({"write": "merge", "key": ["id"], "evolve_schema": True}, _EVOLVE_NEEDS_REPLACE),
        ({"evolve_schema": "yes"}, "evolve_schema must be True or False"),
        ({"evolve_schema": 1}, "evolve_schema must be True or False"),
        (
            {"output": "artifact", "evolve_schema": True},
            "evolve_schema does not apply to an artifact asset; remove it",
        ),
    ],
)
def test_a_wrong_evolve_schema_is_refused(arguments: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError) as caught:
        lakana.asset(**arguments)
    assert str(caught.value) == message


def test_a_key_given_as_one_string_is_a_key_of_one_column() -> None:
    spec = getattr(lakana.asset(write="merge", key="id")(lambda: None), ASSET_ATTRIBUTE)
    assert spec.key == ("id",)


def test_the_model_fields_reach_the_spec() -> None:
    spec = getattr(
        lakana.asset(
            kind="fact",
            grain="One row per order.",
            key="none",
            references={"store_id": "gold.dim_store"},
            checks={"references": "warn"},
        )(lambda: None),
        ASSET_ATTRIBUTE,
    )
    assert (spec.kind, spec.grain, spec.key) == ("fact", "One row per order.", "none")
    assert spec.references == {"store_id": "gold.dim_store"}
    assert spec.checks == {"references": "warn"}


UNSAFE_GRAIN = (
    "grain must not contain a surrogate, a control character other than a newline or tab, "
    "a bidirectional control or a line separator"
)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"kind": 1}, "kind must be a string"),
        ({"kind": "cube"}, "kind must be one of fact, dimension, wide, staging; got 'cube'"),
        ({"grain": 1}, "grain must be a string"),
        ({"grain": "  "}, "grain must not be empty: say in one sentence what one row is"),
        ({"grain": "a\nb"}, "grain must be one line"),
        ({"grain": "a\x07b"}, UNSAFE_GRAIN),
        ({"grain": "a\u202eb"}, UNSAFE_GRAIN),
        ({"grain": "x" * 301}, "grain must be at most 300 characters"),
        (
            {"references": ["x"]},
            'references must be a dict of strings, or of {"to": ..., "at": ...} dicts',
        ),
        (
            {"references": {"a": 1}},
            'references must be a dict of strings, or of {"to": ..., "at": ...} dicts',
        ),
        ({"checks": {"key": 1}}, "checks must be a dict of strings"),
        ({"checks": {"rows": "warn"}}, "checks takes only key, references and measures"),
        ({"checks": {"key": "loud"}}, "checks: key must be block, keep or warn"),
        (
            {"key": "none"},
            "`key: none` is for a fact or a wide table whose rows no columns "
            "identify; declare `kind: fact` or `kind: wide`, or remove `key: none`",
        ),
        ({"kind": "fact", "key": "id"}, "a fact needs grain=: say in one sentence what one row is"),
        (
            {"kind": "wide", "grain": "g"},
            "a wide table needs key=: list the columns that "
            'identify one row, or write key="none" and say in grain why no columns are unique',
        ),
        (
            {"kind": "dimension", "grain": "g", "key": "none"},
            "a dimension needs a real key=; "
            'key="none" is not allowed for a dimension, because nothing could refer to it',
        ),
        (
            {"checks": {"key": "warn"}},
            "checks: key sets how the key checks fail, but the asset declares no key; declare "
            "key=, or remove checks: key",
        ),
        (
            {"checks": {"references": "warn"}},
            "checks: references sets how the reference checks "
            "fail, but the asset declares no references; declare references=, or remove "
            "checks: references",
        ),
        (
            {"references": {"a\u200bb": "gold.t"}},
            "a reference column must not contain a control, "
            "format or surrogate character or a line separator",
        ),
        (
            {"references": {"a.b": "gold.t"}},
            "reference column `a.b` cannot name its check `ref_a.b`: "
            "a check name must not contain '.', '/', a NUL byte or the unit separator",
        ),
        (
            {"references": {"a": "t"}},
            "reference `a: t` must name a table as `ns.table` or `ns.table.column`",
        ),
        (
            {"references": {"a": "gold.a/b"}},
            "reference `a: gold.a/b` must name a table as `ns.table` or `ns.table.column`",
        ),
        ({"key": ["a"] * 2, "kind": "staging"}, "key column `a` is listed twice"),
        ({"key": ""}, "a key column must not be empty; write the name of a column of the asset"),
        (
            {"key": ["id", ""], "write": "merge"},
            "a key column must not be empty; write the name of a column of the asset",
        ),
        ({"references": {"": "gold.t"}}, "a reference column must not be empty"),
        (
            {"references": {"a": "gold.t\nx"}},
            "reference `a` names a target that must not contain a control, format or surrogate "
            "character or a line separator",
        ),
        (
            {"references": {"a": "gold.t\u202ex"}},
            "reference `a` names a target that must not contain a control, format or surrogate "
            "character or a line separator",
        ),
        (
            {"key": [str(n) for n in range(9)]},
            "a key has at most 8 columns, and `key:` lists 9; shorten it to the columns that "
            "identify one row",
        ),
        (
            {"output": "artifact", "kind": "fact"},
            "kind does not apply to an artifact asset; remove it",
        ),
    ],
)
def test_a_wrong_model_is_refused(arguments: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError) as caught:
        lakana.asset(**arguments)
    assert str(caught.value) == message


def test_a_reference_may_be_a_mapping_with_its_time() -> None:
    mapping = {"customer_id": {"to": "gold.dim_customer", "at": "ordered_at"}}
    spec = getattr(lakana.asset(references=mapping)(lambda: None), ASSET_ATTRIBUTE)
    assert spec.references == mapping


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            {"to": "gold.dim_customer"},
            "reference `customer_id` is a mapping without `at:`; write the target as a plain "
            "string, or add `at:`",
        ),
        (
            {"to": "gold.dim_customer", "at": "ordered_at", "as_of": "x"},
            "reference `customer_id` takes `to:` (the target) and `at:` (the row's time "
            "column), such as `{to: gold.dim_customer, at: ordered_at}`",
        ),
        (
            {"to": "gold.dim_customer", "at": ""},
            "`at:` of reference `customer_id` must not be empty; name the column that says when "
            "each row happened",
        ),
        (
            {"to": "gold.dim_customer", "at": "o\u2028d"},
            "`at:` of reference `customer_id` must not contain a control, format or surrogate "
            "character or a line separator",
        ),
    ],
)
def test_a_reference_mapping_is_refused_with_the_deploys_text(
    value: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError) as info:
        lakana.asset(references={"customer_id": value})
    assert str(info.value) == message


@pytest.mark.parametrize(
    ("column", "message"),
    [
        ("", "a reference column must not be empty"),
        (
            "c\u2028d",
            "a reference column must not contain a control, format or surrogate character or a "
            "line separator",
        ),
    ],
)
def test_a_bad_reference_column_is_named_before_its_mapping(column: str, message: str) -> None:
    with pytest.raises(ValueError) as info:
        lakana.asset(references={column: {"to": "gold.dim_customer"}})
    assert str(info.value) == message


def test_a_grain_may_hold_a_tab_and_is_stripped() -> None:
    spec = getattr(lakana.asset(grain="  one\trow per order  ")(lambda: None), ASSET_ATTRIBUTE)
    assert spec.grain == "one\trow per order"


def test_the_decorator_keeps_valid_measures() -> None:
    decorate = asset(
        kind="fact",
        grain="One row per day.",
        key="day",
        measures={
            "stock": {
                "aggregation": "sum",
                "additivity": "semi",
                "not_over": "day",
                "unit": "count",
            }
        },
    )

    def f() -> None:
        """Do nothing."""

    spec = getattr(decorate(f), ASSET_ATTRIBUTE)
    assert spec.measures == {
        "stock": {"aggregation": "sum", "additivity": "semi", "not_over": ["day"], "unit": "count"}
    }


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"kind": "dimension", "grain": "g", "key": "id"},
            'measures belong on a fact or wide table; declare kind="fact" or kind="wide", or '
            "remove measures=",
        ),
        (
            {"kind": "fact", "grain": "g", "key": "m"},
            "measure `m` is a key column; a key identifies a row and is not a value to aggregate. "
            "Remove it from measures=",
        ),
    ],
)
def test_the_decorator_applies_the_rules_between_fields(
    arguments: dict[str, Any], expected: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(expected)):
        asset(**arguments, measures={"m": {"aggregation": "sum", "unit": "count"}})


def test_checks_measures_needs_measures_in_the_decorator() -> None:
    with pytest.raises(ValueError, match="checks: measures sets how the measure checks fail"):
        asset(kind="fact", grain="g", key="none", checks={"measures": "warn"})
    with pytest.raises(ValueError, match="checks takes only key, references and measures"):
        asset(checks={"rows": "warn"})


@pytest.mark.parametrize(
    ("measures", "expected"),
    [
        (["amount"], "measures must be a dict of dicts"),
        ({3: {"aggregation": "sum", "unit": "count"}}, "measures must be a dict of dicts"),
        (
            {"m": "sum"},
            "measures: `m` must be a mapping such as `{aggregation: sum, currency: EUR}`",
        ),
        (
            {"": {"aggregation": "sum", "unit": "count"}},
            "measures: a measure column must not be empty",
        ),
        (
            {"a.b": {"aggregation": "sum", "unit": "count"}},
            "measures: measure column `a.b` cannot name its id `measure_a.b`",
        ),
        (
            {"m": {"aggregation": "sum", "unit": "count", "label": 3}},
            "measures.m.label: must be a text; in YAML, quote a value that reads as a number, "
            "a date, `yes` or `null`",
        ),
        (
            {"m": {"aggregation": "sum", "additivity": "semi", "not_over": [3], "unit": "count"}},
            "measures.m.not_over: must be a column name or a list of column names",
        ),
    ],
)
def test_the_decorator_refuses_a_measures_value_of_the_wrong_shape(
    measures: object, expected: str
) -> None:
    """The shapes only Python can produce: deploy reads them as YAML or literals instead."""
    with pytest.raises(ValueError, match=re.escape(expected)):
        cast("Any", asset)(kind="fact", grain="g", key="none", measures=measures)


def test_the_sdk_currencies_are_the_two_lists_together() -> None:
    assert lakana.assets.CURRENCY_CODES == (
        lakana.assets.CURRENT_CURRENCIES | lakana.assets.WITHDRAWN_CURRENCIES
    )
    assert "EUR" in lakana.assets.CURRENT_CURRENCIES
    assert "DEM" in lakana.assets.WITHDRAWN_CURRENCIES


def test_the_decorator_keeps_a_label_and_a_description_as_given() -> None:
    texts = {"label": "Revenue", "description": "Net of returns."}
    decorate = asset(
        kind="fact",
        grain="g",
        key="none",
        measures={"amount": {"aggregation": "sum", "currency": "EUR", **texts}},
    )

    def f() -> None:
        """Do nothing."""

    spec = getattr(decorate(f), ASSET_ATTRIBUTE)
    assert spec.measures == {"amount": {"aggregation": "sum", "currency": "EUR", **texts}}


def test_the_decorator_records_a_label_and_long_form_columns() -> None:
    @asset(label="Scores", columns={"score": {"label": "Score"}, "id": "The id."})
    def scores() -> None: ...

    spec = getattr(scores, ASSET_ATTRIBUTE)
    assert spec.label == "Scores"
    assert spec.columns == {"score": {"label": "Score"}, "id": "The id."}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"label": "a\nb"}, "label: must be one line"),
        ({"label": "x" * 101}, "label: must be at most 100 characters"),
        ({"label": " "}, "label: must not be empty"),
        ({"label": 3}, "label: must be a text"),
        ({"label": "a\u202eb"}, "label: must not contain a surrogate"),
        ({"columns": ["c"]}, "columns must be a dict of strings or dicts"),
        ({"columns": {"c": {}}}, "columns.c: needs `label:` or `description:`"),
        ({"columns": {"c": {"lable": "x"}}}, "columns.c.lable: Extra inputs are not permitted"),
        ({"columns": {"c": ["x"]}}, "columns: `c` must be a description or a mapping"),
        ({"columns": {"c": {"label": "a\nb"}}}, "columns.c.label: must be one line"),
    ],
)
def test_bad_texts_are_refused_like_the_deploy_refuses_them(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        cast("Any", asset)(**kwargs)


def test_a_label_on_an_artifact_asset_is_refused() -> None:
    with pytest.raises(ValueError, match="label does not apply to an artifact asset"):
        asset(output="artifact", label="Model")


@pytest.mark.parametrize(
    ("description", "message"),
    [
        (" ", "measures.m.description: must not be empty"),
        ("a\u202eb", "measures.m.description: must not contain a surrogate"),
    ],
)
def test_a_bad_measure_description_is_refused(description: str, message: str) -> None:
    measure = {"aggregation": "sum", "unit": "count", "description": description}
    with pytest.raises(ValueError, match=re.escape(message)):
        cast("Any", asset)(kind="fact", grain="g", key="none", measures={"m": measure})


_DIM: dict[str, Any] = {
    "kind": "dimension",
    "grain": "One row per customer and version.",
    "key": "customer_id",
}


def test_the_history_options_reach_the_spec() -> None:
    spec = getattr(
        lakana.asset(write="history", valid_from="changed_at", close_missing=True, **_DIM)(
            lambda: None
        ),
        ASSET_ATTRIBUTE,
    )
    assert (spec.write, spec.valid_from, spec.close_missing) == ("history", "changed_at", True)
    plain = getattr(lakana.asset()(lambda: None), ASSET_ATTRIBUTE)
    assert (plain.valid_from, plain.close_missing) == (None, False)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"write": "history", "key": "id"},
            "`write: history` keeps the versions of a dimension's rows; declare `kind: "
            "dimension`, with `grain:` and `key:`",
        ),
        (
            {"write": "history", "watermark": "ts", **_DIM},
            "`write: history` takes no `watermark:`; its SELECT returns the current state of "
            "every row, and each run compares it with the open versions",
        ),
        ({"valid_from": "changed_at"}, "`valid_from:` works with `write: history` only"),
        ({"close_missing": False}, "`close_missing:` works with `write: history` only"),
        (
            {"write": "history", "valid_from": "customer_id", **_DIM},
            "`valid_from: customer_id` names a key column; name the column that says when the "
            "row's values took effect, or leave `valid_from:` out to use the run's start time",
        ),
        ({"write": "history", "valid_from": 3, **_DIM}, "valid_from must be a string"),
        (
            {"write": "history", "close_missing": "yes", **_DIM},
            "close_missing must be True or False",
        ),
        (
            {"write": "history", "evolve_schema": True, **_DIM},
            'evolve_schema=True works with write="replace" (the default) only; remove '
            "evolve_schema, or replace the table instead of appending, merging or keeping its "
            "history",
        ),
    ],
)
def test_a_wrong_history_model_is_refused_at_import(
    arguments: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError) as info:
        lakana.asset(**arguments)
    assert str(info.value) == message


@pytest.mark.parametrize("column", ["", "changed​_at"])
def test_an_unsafe_valid_from_is_refused_at_import(column: str) -> None:
    with pytest.raises(ValueError) as info:
        lakana.asset(write="history", valid_from=column, **_DIM)
    assert str(info.value) == f"valid_from {lakana.assets._IDENTIFIER_MESSAGE}"
