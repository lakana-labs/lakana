"""The `@asset` decorator: the contract a Python asset is written against."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import lakana
import lakana.assets
from lakana.assets import ASSET_ATTRIBUTE, AssetSpec


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
        ({"write": "upsert"}, "write must be one of replace, append, merge; got 'upsert'"),
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
        ({"write": "merge", "key": "id"}, "key must be a list of strings"),
    ],
)
def test_a_wrong_write_model_is_refused(arguments: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError) as caught:
        lakana.asset(**arguments)
    assert str(caught.value) == message


def test_a_key_is_accepted_with_every_write_mode() -> None:
    assert callable(lakana.asset(key=["id"]))
    assert callable(lakana.asset(write="append", watermark="ts", key=["id"]))
    with pytest.raises(ValueError) as caught:
        lakana.asset(write="merge", key=[f"c{i}" for i in range(9)])
    assert str(caught.value) == "a key has at most 8 columns; `key:` lists 9"


_EVOLVE_NEEDS_REPLACE = (
    'evolve_schema=True works with write="replace" (the default) only; remove evolve_schema, '
    "or replace the table instead of appending or merging"
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
