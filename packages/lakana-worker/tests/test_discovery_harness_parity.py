"""Deploy discovery and the task harness agree on which function is the asset.

Deploy finds a file's asset function with `ast`, without running the file
(`lakana_core.projects.python_assets`). The harness finds it again inside the
task process, after importing the file (`lakana.runner`). The two are separate
code in separate packages, and they must not drift: a file that deploys must
be runnable, and the harness must never run a function deploy did not pick.

Every source below goes through both. The rule this module pins:

- a source discovery **accepts** is selected by the harness, at the same
  line, and its body runs;
- a source discovery **refuses** for the shape of its decorator or the place
  of its function is never run by the harness;
- a source that only *running* it can tell apart (the function's name is rebound
  after it is defined) deploys, and the harness refuses it: it fails
  closed, it never runs the other function.
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

import pytest

from lakana import assets as sdk_assets
from lakana import runner
from lakana.assets import ASSET_ATTRIBUTE
from lakana_core.connections.kinds import NAME
from lakana_core.projects.asset_model import (
    EVOLVE_NEEDS_REPLACE,
    MAX_CONNECTIONS,
    MAX_KEEP_VERSIONS,
    MODEL_KINDS,
    WRITE_CHOICES,
    ParsedAsset,
)
from lakana_core.projects.deploys import ARTIFACT_PARAMETER, _artifact_parameter_issues
from lakana_core.projects.discovery import DiscoveredAsset
from lakana_core.projects.project_config import AssetDefaults, normalize_source
from lakana_core.projects.python_assets import parse_python_asset

_PATH = "assets/gold/scores.py"
_RAN = "the asset function ran"

_PLAIN = f"""\
from lakana import asset


@asset()
def scores():
    return {_RAN!r}
"""

_ARTIFACT = _PLAIN.replace("@asset()", '@asset(output="artifact", keep_versions=2)')

_MODULE_FORM = f"""\
import lakana


@lakana.asset(inputs={{"clean": "silver.clean"}})
def scores(clean):
    return {_RAN!r}
"""

_DECORATOR_OVER_SEVERAL_LINES = f"""\
import functools

from lakana import asset


@functools.cache
def helper():
    return 1


@asset(
    inputs={{"clean": "silver.clean", "other": "bronze.staging.other"}},
    description="Scores.",
)
def scores(clean, other):
    return {_RAN!r}
"""

_NESTED_SECOND_IN_IF = f"""\
from lakana import asset


@asset()
def scores():
    return {_RAN!r}


if True:

    @asset()
    def other():
        return "the wrong function ran"
"""

_NESTED_SECOND_IN_CLASS_AND_FUNCTION = f"""\
from lakana import asset


class Holder:
    @asset()
    def method():
        return "the wrong function ran"


def factory():
    @asset()
    def inner():
        return "the wrong function ran"

    return inner


made = factory()


@asset()
def scores():
    return {_RAN!r}
"""

_ACCEPTED = {
    "plain": _PLAIN,
    "artifact-asset": _ARTIFACT,
    "with-connections": _PLAIN.replace("@asset()", '@asset(connections=["crm-db", "files"])'),
    "module-form": _MODULE_FORM,
    "byte-order-mark": "﻿" + _PLAIN,
    "crlf": _PLAIN.replace("\n", "\r\n"),
    "decorator-over-several-lines": _DECORATOR_OVER_SEVERAL_LINES,
    "second-decorated-function-in-an-if": _NESTED_SECOND_IN_IF,
    "second-decorated-functions-in-a-class-and-a-function": _NESTED_SECOND_IN_CLASS_AND_FUNCTION,
}

_REFUSED = {
    "no-asset": "def scores():\n    return 1\n",
    "bare-decorator": f"from lakana import asset\n\n\n@asset\ndef scores():\n    return {_RAN!r}\n",
    "two-assets": _PLAIN + f"\n\n@asset()\ndef more():\n    return {_RAN!r}\n",
    "aliased-name": (
        f"from lakana import asset as make\n\n\n@make()\ndef scores():\n    return {_RAN!r}\n"
    ),
    "aliased-module": (
        f"import lakana as lk\n\n\n@lk.asset()\ndef scores():\n    return {_RAN!r}\n"
    ),
    "re-exported-decorator": (
        f"import lakana\n\nasset = lakana.asset\n\n\n@asset()\ndef scores():\n    return {_RAN!r}\n"
    ),
    "stacked-decorator-above": (
        "import functools\n\nfrom lakana import asset\n\n\n"
        f"@functools.cache\n@asset()\ndef scores():\n    return {_RAN!r}\n"
    ),
    "stacked-decorator-below": (
        "from lakana import asset\n\n\ndef noisy(function):\n    return function\n\n\n"
        f"@asset()\n@noisy\ndef scores():\n    return {_RAN!r}\n"
    ),
    "two-asset-decorators": (
        f"from lakana import asset\n\n\n@asset()\n@asset()\ndef scores():\n    return {_RAN!r}\n"
    ),
    "only-in-an-if": (
        "from lakana import asset\n\n\nif True:\n\n"
        f"    @asset()\n    def scores():\n        return {_RAN!r}\n"
    ),
    "only-in-a-class": (
        "from lakana import asset\n\n\nclass Holder:\n"
        f"    @asset()\n    def scores():\n        return {_RAN!r}\n"
    ),
    "only-in-a-function": (
        "from lakana import asset\n\n\ndef factory():\n"
        f"    @asset()\n    def scores():\n        return {_RAN!r}\n\n"
        "    return scores\n\n\nscores = factory()\n"
    ),
    "async": f"from lakana import asset\n\n\n@asset()\nasync def scores():\n    return {_RAN!r}\n",
    "does-not-parse": "from lakana import asset\n\n\n@asset(\ndef scores():\n    return 1\n",
    # The name is rebound before the decorator is used (#51): discovery follows the rebinding.
    "decorator-replaced-before-use": (
        "from lakana import asset\n\n\ndef asset(**arguments):\n"
        "    return lambda function: function\n\n\n"
        f"@asset()\ndef scores():\n    return {_RAN!r}\n"
    ),
    "decorator-assigned-before-use": (
        "from lakana import asset\n\nasset = print\n\n\n"
        f"@asset()\ndef scores():\n    return {_RAN!r}\n"
    ),
}

_ONLY_RUNNING_TELLS = {
    "name-rebound-to-another-function": _PLAIN
    + '\n\ndef other():\n    return "the wrong function ran"\n\n\nscores = other\n',
    "function-defined-again": _PLAIN + '\n\ndef scores():\n    return "the wrong function ran"\n',
}


def _discovered(source: str) -> ParsedAsset | None:
    """Run deploy discovery on a source.

    Parameters
    ----------
    source
        The asset file's text.

    Returns
    -------
    ParsedAsset or None
        The parsed asset, or `None` when discovery refuses the file.
    """
    text = normalize_source(source)  # what `discover` hands over: no BOM, LF line ends
    parsed = parse_python_asset(
        DiscoveredAsset(path=_PATH, table="gold.scores", text=text, kind="python"),
        AssetDefaults(),
    )
    return parsed if isinstance(parsed, ParsedAsset) else None


def _harness(source: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int | None, object]:
    """Let the harness select the file's asset function, and call what it selects.

    Parameters
    ----------
    source
        The asset file's text.
    root
        An empty directory standing in for the unpacked code.
    monkeypatch
        Restores `sys.modules` after the file was imported.

    Returns
    -------
    tuple
        The line of the selected function's decorator and what the function
        returned; ``(None, <the exception>)`` when the harness selected
        nothing or the call failed.
    """
    file = root / _PATH
    file.parent.mkdir(parents=True)
    file.write_bytes(source.encode("utf-8"))
    monkeypatch.setitem(sys.modules, runner._MODULE_NAME, None)
    try:
        function = runner._load(root, _PATH)
        assert isinstance(function, types.FunctionType)
        spec = getattr(function, ASSET_ATTRIBUTE)
        result = function(**dict.fromkeys(spec.inputs, 0))
    except Exception as exc:
        return None, exc
    if inspect.iscoroutine(result):
        result.close()  # selected and called: that counts as run, though the body did not start
    return function.__code__.co_firstlineno, result


_HUGE = "9" * 4000

_ARGUMENT_CASES: dict[str, str] = {
    "output-table": 'output="table"',
    "output-artifact": 'output="artifact"',
    "output-other-string": 'output="model"',
    "output-number": "output=3",
    "output-variable": "output=KIND",
    "output-f-string": 'output=f"{KIND}"',
    "output-none": "output=None",
    "output-huge": f"output={_HUGE}",
    "output-huge-negative": f"output=-{_HUGE}",
    "keep-versions-without-artifact": "keep_versions=2",
    "keep-versions-on-table": 'output="table", keep_versions=2',
    "keep-versions-none": 'output="artifact", keep_versions=None',
    "keep-versions-one": 'output="artifact", keep_versions=1',
    "keep-versions-zero": 'output="artifact", keep_versions=0',
    "keep-versions-negative": 'output="artifact", keep_versions=-1',
    "keep-versions-bool": 'output="artifact", keep_versions=True',
    "keep-versions-float": 'output="artifact", keep_versions=2.0',
    "keep-versions-string": 'output="artifact", keep_versions="2"',
    "keep-versions-the-largest": 'output="artifact", keep_versions=2147483647',
    "keep-versions-one-too-many": 'output="artifact", keep_versions=2147483648',
    "keep-versions-huge": f'output="artifact", keep_versions={_HUGE}',
    "keep-versions-huge-negative": f'output="artifact", keep_versions=-{_HUGE}',
    "keep-versions-huge-on-table": f"keep_versions={_HUGE}",
    "artifact-with-materialize": 'output="artifact", materialize="table"',
    "artifact-with-columns": 'output="artifact", columns={"a": "b"}',
    "table-with-materialize": 'materialize="table"',
    "decimals-float": 'decimals="float"',
    "decimals-exact": 'decimals="exact"',
    "decimals-exact-on-artifact": 'output="artifact", decimals="exact"',
    "decimals-other-string": 'decimals="fixed"',
    "decimals-wrong-case": 'decimals="Exact"',
    "decimals-number": "decimals=3",
    "decimals-bool": "decimals=True",
    "decimals-none": "decimals=None",
    "decimals-huge": f"decimals={_HUGE}",
    "decimals-variable": "decimals=KIND",
    "connections-two": 'connections=["crm-db", "files"]',
    "connections-empty": "connections=[]",
    "connections-tuple": 'connections=("crm-db",)',
    "connections-none": "connections=None",
    "connections-sixteen": "connections=[" + ", ".join(f'"c{i}"' for i in range(16)) + "]",
    "connections-seventeen": "connections=[" + ", ".join(f'"c{i}"' for i in range(17)) + "]",
    "connections-a-string": 'connections="crm-db"',
    "connections-a-dict": 'connections={"crm-db": "x"}',
    "connections-repeated": 'connections=["crm-db", "crm-db"]',
    "connections-upper-case": 'connections=["CRM"]',
    "connections-leading-digit": 'connections=["1db"]',
    "connections-too-long": 'connections=["' + "a" * 64 + '"]',
    "connections-longest": 'connections=["' + "a" * 63 + '"]',
    "connections-trailing-newline": 'connections=["crm-db\\n"]',
    "connections-underscore": 'connections=["crm_db"]',
    "write-replace": 'write="replace"',
    "write-append": 'write="append", watermark="ts"',
    "write-merge": 'write="merge", key=["id"]',
    "write-merge-composite-with-watermark": 'write="merge", key=["id", "region"], watermark="u"',
    "write-other-string": 'write="upsert"',
    "write-number": "write=3",
    "write-none": "write=None",
    "write-append-without-watermark": 'write="append"',
    "write-append-with-key": 'write="append", watermark="ts", key=["id"]',
    "write-merge-without-key": 'write="merge"',
    "write-merge-empty-key": 'write="merge", key=[]',
    "write-replace-with-key": 'write="replace", key=["id"]',
    "watermark-alone": 'watermark="ts"',
    "key-alone": 'key=["id"]',
    "key-repeated": 'write="merge", key=["id", "id"]',
    "key-a-string": 'write="merge", key="id"',
    "key-tuple": 'write="merge", key=("id",)',
    "key-unsafe": 'write="merge", key=["i\\u200bd"]',
    "watermark-unsafe": 'write="append", watermark="t\\u2028s"',
    "evolve-schema-true": "evolve_schema=True",
    "evolve-schema-false": "evolve_schema=False",
    "evolve-schema-none": "evolve_schema=None",
    "evolve-schema-string": 'evolve_schema="yes"',
    "evolve-schema-number": "evolve_schema=1",
    "evolve-schema-on-replace": 'write="replace", evolve_schema=True',
    "evolve-schema-on-append": 'write="append", watermark="ts", evolve_schema=True',
    "evolve-schema-off-on-append": 'write="append", watermark="ts", evolve_schema=False',
    "evolve-schema-on-merge": 'write="merge", key=["id"], evolve_schema=True',
    "evolve-schema-on-artifact": 'output="artifact", evolve_schema=True',
    "evolve-schema-off-on-artifact": 'output="artifact", evolve_schema=False',
    "watermark-a-list": "write=\"append\", watermark=['ts']",
    "kind-fact-complete": 'kind="fact", grain="One row per order.", key="id"',
    "kind-fact-no-grain": 'kind="fact", key="id"',
    "kind-unknown": 'kind="cube"',
    "kind-dimension-key-none": 'kind="dimension", grain="g", key="none"',
    "key-none-without-kind": 'key="none"',
    "key-nine-columns": 'key=["a","b","c","d","e","f","g","h","i"]',
    "grain-two-lines": 'grain="a\\nb"',
    "grain-bell": 'grain="a\\x07b"',
    "grain-tab": 'grain="a\\tb"',
    "grain-bidi-override": 'grain="a\\u202eb"',
    "references-short": 'references={"store_id": "gold.dim_store"}',
    "references-no-namespace": 'references={"store_id": "dim_store"}',
    "references-bad-column": 'references={"a/b": "gold.dim_store"}',
    "references-dotted-column": 'references={"a.b": "gold.dim_store"}',
    "references-slash-in-target": 'references={"x": "gold.a/b"}',
    "checks-unknown-family": 'checks={"rows": "warn"}',
    "checks-bad-mode": 'checks={"key": "loud"}',
    "checks-without-key": 'checks={"key": "warn"}',
    "write-on-artifact": 'output="artifact", write="append", watermark="ts"',
    "key-on-artifact": 'output="artifact", key=["id"]',
    "replace-and-empty-key-on-artifact": 'output="artifact", write="replace", key=[]',
    "materialize-incremental": 'materialize="incremental"',
}


def _call(source: str) -> str | None:
    """Run the decorator of a source at run time, as the harness's import does.

    Parameters
    ----------
    source
        The asset file's text.

    Returns
    -------
    str or None
        The `ValueError` text, or `None` when the decorator accepts its arguments.
    """
    try:
        exec(compile(source, _PATH, "exec"), {"KIND": "artifact", "__name__": "parity"})  # nosec B102 -- the test's own literal
    except ValueError as exc:
        return str(exc)
    return None


def test_the_connection_rules_are_the_same_in_core_and_the_sdk() -> None:
    assert sdk_assets.CONNECTION_NAME.pattern == NAME.pattern
    assert sdk_assets.MAX_CONNECTIONS == MAX_CONNECTIONS == 16
    plain = _discovered(_PLAIN)
    assert plain is not None
    assert plain.connections == sdk_assets.AssetSpec.__dataclass_fields__["connections"].default
    declared = _discovered(_PLAIN.replace("@asset()", '@asset(connections=["crm-db"])'))
    assert declared is not None
    assert declared.connections == ("crm-db",)


def test_the_write_choices_and_defaults_are_the_same_in_core_and_the_sdk() -> None:
    assert sdk_assets.WRITE_CHOICES == WRITE_CHOICES == ("replace", "append", "merge")
    fields = sdk_assets.AssetSpec.__dataclass_fields__
    assert (fields["write"].default, fields["key"].default, fields["watermark"].default) == (
        "replace",
        (),
        None,
    )
    merged = _discovered(
        _PLAIN.replace("@asset()", '@asset(write="merge", key=["id"], watermark="u")')
    )
    assert merged is not None
    assert (merged.metadata.write, merged.metadata.key, merged.metadata.watermark) == (
        "merge",
        ("id",),
        "u",
    )


def test_the_decorator_records_the_write_model_on_its_spec() -> None:
    namespace: dict[str, object] = {"__name__": "parity"}
    source = _PLAIN.replace("@asset()", '@asset(write="merge", key=["id", "region"])')
    exec(compile(source, _PATH, "exec"), namespace)  # nosec B102 -- the test's own literal
    spec = getattr(namespace["scores"], ASSET_ATTRIBUTE)
    assert (spec.write, spec.key, spec.watermark) == ("merge", ("id", "region"), None)


def test_the_evolve_schema_rule_reads_the_same_in_core_and_the_sdk() -> None:
    assert sdk_assets.EVOLVE_NEEDS_REPLACE == EVOLVE_NEEDS_REPLACE
    assert sdk_assets.AssetSpec.__dataclass_fields__["evolve_schema"].default is False
    refusal = _call(
        _PLAIN.replace("@asset()", '@asset(write="merge", key=["id"], evolve_schema=True)')
    )
    assert refusal == EVOLVE_NEEDS_REPLACE


def test_the_retention_bound_is_the_same_in_core_and_the_sdk() -> None:
    assert MAX_KEEP_VERSIONS == sdk_assets.MAX_KEEP_VERSIONS == 2**31 - 1


def test_the_decimals_modes_and_default_are_the_same_in_core_and_the_sdk() -> None:
    assert sdk_assets.DECIMALS_MODES == ("float", "exact")
    for mode in sdk_assets.DECIMALS_MODES:
        parsed = _discovered(_PLAIN.replace("@asset()", f'@asset(decimals="{mode}")'))
        assert parsed is not None
        assert parsed.decimals == mode
    plain = _discovered(_PLAIN)
    assert plain is not None
    assert plain.decimals == sdk_assets.AssetSpec.__dataclass_fields__["decimals"].default


@pytest.mark.parametrize("name", sorted(_ARGUMENT_CASES))
def test_deploy_and_the_decorator_accept_and_refuse_the_same_arguments(name: str) -> None:
    """The rules for the decorator's arguments exist twice (core, SDK); they must agree."""
    source = _PLAIN.replace("@asset()", f"@asset({_ARGUMENT_CASES[name]})")
    deploy_accepts = _discovered(source) is not None
    refusal = _call(source)
    # Values deploy reads as non-literals (variables, f-strings) can only be refused by
    # deploy; at run time the name resolves, so only literals are compared.
    if name in ("output-variable", "output-f-string", "decimals-variable"):
        assert not deploy_accepts
        return
    assert deploy_accepts == (refusal is None), refusal
    if refusal is not None:
        assert "9999" not in refusal


@pytest.mark.parametrize("name", sorted(_ACCEPTED))
def test_a_source_discovery_accepts_is_the_function_the_harness_runs(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _ACCEPTED[name]
    parsed = _discovered(source)
    assert parsed is not None, "discovery refuses a source this test lists as accepted"
    line, result = _harness(source, tmp_path, monkeypatch)
    assert result == _RAN
    assert line == parsed.block_line


@pytest.mark.parametrize("name", sorted(_REFUSED))
def test_a_source_discovery_refuses_is_never_run_by_the_harness(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _REFUSED[name]
    assert _discovered(source) is None, "discovery accepts a source this test lists as refused"
    line, result = _harness(source, tmp_path, monkeypatch)
    assert line is None, f"the harness ran a function deploy refuses: {result!r}"
    if name == "async":
        assert isinstance(result, runner.AssetLoadError)
        assert str(result) == f"{_PATH}: an asset function must not be async"


@pytest.mark.parametrize("name", sorted(_ONLY_RUNNING_TELLS))
def test_a_rebound_name_deploys_and_the_harness_refuses_it_without_running_anything(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _ONLY_RUNNING_TELLS[name]
    assert _discovered(source) is not None
    line, result = _harness(source, tmp_path, monkeypatch)
    assert line is None
    assert isinstance(result, runner.AssetLoadError), result
    assert "replaced or wrapped" in str(result)


@pytest.mark.parametrize(
    "name",
    [
        "model",
        "_m",
        "M1",
        "m" * 128,
        "m" * 129,
        "modèle",
        "mo del",
        "1m",
        "",
        "m\n",
        "\uff4dodel",
        "m-1",
    ],
)
def test_deploy_and_harness_agree_on_the_name_of_a_parameter_that_reads_an_artifact(
    name: str,
) -> None:
    """Deploy refuses exactly the names with which the harness exits with status 2."""
    assert (ARTIFACT_PARAMETER.fullmatch(name) is None) == (
        runner._PARAMETER.fullmatch(name) is None
    )
    assert ARTIFACT_PARAMETER.pattern == runner._PARAMETER.pattern


@pytest.mark.parametrize(
    ("first", "second", "clash"),
    [
        ("Model", "model", True),
        ("MODEL", "model", True),
        ("model_a", "Model_A", True),
        ("model", "models", False),
        ("a", "b", False),
        ("model", "model_", False),
    ],
)
def test_deploy_and_harness_agree_that_two_artifact_parameters_may_not_differ_only_in_case(
    first: str, second: str, clash: bool
) -> None:
    """The harness refuses the document exactly when deploy reports the asset."""
    source = (
        "from lakana import asset\n\n\n"
        f'@asset(inputs={{"{first}": "models.a", "{second}": "models.b"}})\n'
        f"def scores({first}, {second}):\n    return 1\n"
    )
    parsed = _discovered(source)
    assert parsed is not None
    deploy_refuses = bool(_artifact_parameter_issues(parsed, {"models.a", "models.b"}))
    inputs = {first: "models.a", second: "models.b"}
    document: dict[str, object] = {
        "input_kinds": dict.fromkeys(inputs, "artifact"),
        "input_versions": dict.fromkeys(inputs, 1),
    }
    try:
        runner._input_fields(document, inputs)
    except ValueError as exc:
        assert "differ only in case" in str(exc)
        harness_refuses = True
    else:
        harness_refuses = False
    assert deploy_refuses is harness_refuses is clash


def test_the_sdk_routes_the_kinds_the_worker_routes() -> None:
    """The SDK gives the proxy to the kinds the worker sends through it; the relay's alias."""
    from lakana.ingest import connections as sdk_connections
    from lakana_core.connections.endpoints import PROXY_KINDS
    from lakana_worker.launcher.requests import RELAY_ALIAS

    assert {kind.value for kind in PROXY_KINDS} == sdk_connections._PROXY_KINDS
    assert sdk_connections._RELAY_HOST == RELAY_ALIAS


def test_the_model_kinds_are_the_same_in_core_and_the_sdk() -> None:
    assert sdk_assets.MODEL_KINDS == MODEL_KINDS == ("fact", "dimension", "wide", "staging")
