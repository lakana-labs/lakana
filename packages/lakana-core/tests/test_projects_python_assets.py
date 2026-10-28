"""`ast` discovery of a Python asset: every accepted and refused shape."""

from __future__ import annotations

import ast
import hashlib
import textwrap
import warnings
from typing import Any

import pytest

import lakana_core.projects.python_assets as python_assets_module
from lakana_core.projects.asset_model import ParsedAsset
from lakana_core.projects.discovery import DiscoveredAsset
from lakana_core.projects.frontmatter import parse_front_matter
from lakana_core.projects.issues import ProjectIssue, ProjectValidationError
from lakana_core.projects.parse import ParsedAsset as ReExported
from lakana_core.projects.project_config import AssetDefaults
from lakana_core.projects.python_assets import PYTHON_SOURCE_PREFIX, parse_python_asset
from lakana_core.projects.run_policy import RunCaps, run_policy_issues

_PATH = "assets/gold/station_scores.py"

_GOOD = '''\
import polars as pl
from lakana import asset


@asset(
    inputs={"obs": "silver.weather", "stations": "bronze.stations"},
    owner="data-eng",
    tags=["weather"],
    retries=2,
    retry_delay="30s",
    timeout="10m",
    columns={"score": "Anomaly score, 0-1."},
)
def station_scores(obs: pl.DataFrame, stations: pl.DataFrame) -> pl.DataFrame:
    """Anomaly score per station
    from the last week.

    Details that are not part of the description.
    """
    return obs
'''


def _asset(text: str) -> DiscoveredAsset:
    """Build a discovered Python asset.

    Parameters
    ----------
    text
        The file content, dedented.

    Returns
    -------
    DiscoveredAsset
        `gold.station_scores`, kind ``python``.
    """
    return DiscoveredAsset(
        path=_PATH, table="gold.station_scores", text=textwrap.dedent(text), kind="python"
    )


def _parsed(text: str, defaults: AssetDefaults | None = None) -> ParsedAsset:
    """Parse and expect success.

    Parameters
    ----------
    text
        The file content.
    defaults
        `lakana.yaml` defaults.

    Returns
    -------
    ParsedAsset
        The parsed asset.
    """
    result = parse_python_asset(_asset(text), defaults or AssetDefaults())
    assert isinstance(result, ParsedAsset), result
    return result


def _issues(text: str) -> list[ProjectIssue]:
    """Parse and expect issues.

    Parameters
    ----------
    text
        The file content.

    Returns
    -------
    list of ProjectIssue
        The issues.
    """
    result = parse_python_asset(_asset(text), AssetDefaults())
    assert isinstance(result, list), result
    assert result
    assert all(issue.path == _PATH for issue in result)
    return result


def test_parsed_asset_is_one_class() -> None:
    assert ReExported is ParsedAsset


def test_the_spec_example_parses() -> None:
    parsed = _parsed(_GOOD)
    assert parsed.kind == "python"
    assert parsed.table == "gold.station_scores"
    assert parsed.inputs == (("obs", "silver.weather"), ("stations", "bronze.stations"))
    assert parsed.references == ("bronze.stations", "silver.weather")
    assert parsed.sql == ""
    assert (
        parsed.normalized_sql == PYTHON_SOURCE_PREFIX + hashlib.sha256(_GOOD.encode()).hexdigest()
    )
    assert parsed.columns is None
    assert parsed.lineage == {}
    assert parsed.block_line == 5
    assert parsed.body_line == 14
    metadata = parsed.metadata
    assert metadata.description == "Anomaly score per station from the last week."
    assert metadata.owner == "data-eng"
    assert metadata.tags == ("weather",)
    assert metadata.materialize == "table"
    assert (metadata.retries, metadata.retry_delay, metadata.timeout) == (2, 30, 600)
    assert metadata.columns == {"score": "Anomaly score, 0-1."}


def test_the_module_attribute_form_and_no_inputs() -> None:
    parsed = _parsed(
        """\
        import lakana

        @lakana.asset()
        def load():
            return None
        """
    )
    assert parsed.inputs == ()
    assert parsed.references == ()
    assert parsed.metadata.description is None


def test_a_comprehension_variable_does_not_rebind_the_decorator() -> None:
    parsed = _parsed(
        """\
        from lakana import asset

        names = [asset for asset in range(3)]

        @asset()
        def load():
            return None
        """
    )
    assert parsed.inputs == ()


def test_a_submodule_import_and_a_later_rebinding_do_not_unbind_the_decorator() -> None:
    parsed = _parsed(
        """\
        import lakana
        import lakana.sub
        from lakana import asset

        @lakana.asset()
        def load():
            return None

        lakana = None
        asset = None
        """
    )
    assert parsed.inputs == ()


def test_an_explicit_description_beats_the_docstring() -> None:
    parsed = _parsed(
        '''\
        from lakana import asset

        @asset(description="Explicit.", owner=None)
        def load():
            """Docstring."""
        '''
    )
    assert parsed.metadata.description == "Explicit."
    assert parsed.metadata.owner is None


def test_yaml_defaults_apply_except_materialize() -> None:
    """A project default of `materialize: view` must not turn a Python asset into a view."""
    parsed = _parsed(
        """\
        from lakana import asset

        @asset()
        def load():
            return None
        """,
        AssetDefaults(owner="platform", materialize="view", retries=4, description="Default."),
    )
    assert parsed.metadata.materialize == "table"
    assert parsed.metadata.owner == "platform"
    assert parsed.metadata.retries == 4
    assert parsed.metadata.description == "Default."


def test_duplicate_input_tables_are_one_reference() -> None:
    parsed = _parsed(
        """\
        from lakana import asset

        @asset(inputs={"a": "bronze.x", "b": "bronze.x"})
        def load(a, *, b):
            return a
        """
    )
    assert parsed.inputs == (("a", "bronze.x"), ("b", "bronze.x"))
    assert parsed.references == ("bronze.x",)


def test_nothing_in_the_file_is_executed() -> None:
    """Module-level code that would raise if run is only parsed."""
    parsed = _parsed(
        """\
        from lakana import asset
        raise SystemExit("deploy must not run this")

        @asset()
        def load():
            return None
        """
    )
    assert parsed.kind == "python"


_HEAD = "from lakana import asset\n"


@pytest.mark.parametrize(
    ("text", "line", "fragment"),
    [
        ("x = 1\n", None, "no function decorated with @asset"),
        (
            "from lakana import asset as a\n@a()\ndef f():\n    pass\n",
            None,
            "aliases and re-exports are not resolved",
        ),
        (
            "import lakana as lk\n@lk.asset()\ndef f():\n    pass\n",
            2,
            "aliases and re-exports are not resolved",
        ),
        ("@asset()\ndef f():\n    pass\n", 1, "aliases and re-exports are not resolved"),
        (
            _HEAD + "asset = print\n@asset()\ndef f():\n    pass\n",
            3,
            "aliases and re-exports are not resolved",
        ),
        (
            _HEAD + "from os import path as asset\n@asset()\ndef f():\n    pass\n",
            3,
            "aliases and re-exports are not resolved",
        ),
        (
            _HEAD + "if True:\n    asset = print\n@asset()\ndef f():\n    pass\n",
            4,
            "aliases and re-exports are not resolved",
        ),
        (
            _HEAD + "match 1:\n    case asset:\n        pass\n@asset()\ndef f():\n    pass\n",
            5,
            "aliases and re-exports are not resolved",
        ),
        (
            _HEAD + "names = [(asset := print) for x in range(2)]\n@asset()\ndef f():\n    pass\n",
            3,
            "aliases and re-exports are not resolved",
        ),
        (
            _HEAD + "names = [x for x in (asset := print, 1)]\n@asset()\ndef f():\n    pass\n",
            3,
            "aliases and re-exports are not resolved",
        ),
        (
            "import lakana\nlakana = object()\n@lakana.asset()\ndef f():\n    pass\n",
            3,
            "aliases and re-exports are not resolved",
        ),
        (
            _HEAD + "@asset()\ndef f():\n    pass\n@asset()\ndef g():\n    pass\n",
            6,
            "more than one @asset function",
        ),
        (_HEAD + "@asset()\n@asset()\ndef f():\n    pass\n", 4, "more than one @asset decorator"),
        (
            _HEAD + "import functools\n@functools.cache\n@asset()\ndef f():\n    pass\n",
            3,
            "the asset function must have @asset(...) as its only decorator",
        ),
        (
            _HEAD + "import functools\n@asset()\n@functools.cache\ndef f():\n    pass\n",
            4,
            "the asset function must have @asset(...) as its only decorator",
        ),
        (_HEAD + "@asset()\nasync def f():\n    pass\n", 3, "must not be async"),
        (_HEAD + "@asset(inputs=build())\ndef f():\n    pass\n", 2, "'inputs' must be a literal"),
        (
            _HEAD + "T = 'a.b'\n@asset(inputs={'x': T})\ndef f(x):\n    pass\n",
            3,
            "must be a literal",
        ),
        (_HEAD + "@asset(owner=f'{1}')\ndef f():\n    pass\n", 2, "'owner' must be a literal"),
        (
            _HEAD + "@asset(**options)\ndef f():\n    pass\n",
            2,
            "keyword arguments must be written out",
        ),
        (_HEAD + "@asset(inputs={**base})\ndef f():\n    pass\n", 2, "'inputs' must be a literal"),
        (_HEAD + "@asset({'x': 'a.b'})\ndef f(x):\n    pass\n", 2, "keyword arguments only"),
        (_HEAD + "@asset(deps=['a.b'])\ndef f():\n    pass\n", 2, "unknown argument 'deps'"),
        (
            _HEAD + "@asset(inputs=['a.b'])\ndef f():\n    pass\n",
            2,
            "'inputs' must be a dict of strings",
        ),
        (_HEAD + "@asset(tags='x')\ndef f():\n    pass\n", 2, "'tags' must be a list of strings"),
        (_HEAD + "@asset(owner=3)\ndef f():\n    pass\n", 2, "'owner' must be a string"),
        (_HEAD + "@asset(retries='two')\ndef f():\n    pass\n", 2, "retries"),
        (_HEAD + "@asset(retries=-1)\ndef f():\n    pass\n", 2, "retries"),
        (_HEAD + "@asset(timeout='soon')\ndef f():\n    pass\n", 2, "timeout"),
        (_HEAD + "@asset(timeout=1.5)\ndef f():\n    pass\n", 2, "timeout"),
        (_HEAD + "@asset(materialize='view')\ndef f():\n    pass\n", 2, "only materialize 'table'"),
        (
            _HEAD + "@asset(materialize='incremental')\ndef f():\n    pass\n",
            2,
            'a Python asset appends with write="append" and watermark="<column>", '
            "not materialize='incremental'",
        ),
        (_HEAD + "@asset()\ndef f(x):\n    pass\n", 3, "parameter 'x' has no entry in inputs"),
        (
            _HEAD + "@asset(inputs={'y': 'a.b'})\ndef f():\n    pass\n",
            2,
            "inputs names 'y', which is not a parameter",
        ),
        (_HEAD + "@asset(inputs={'x': 'a.b'})\ndef f(x, /):\n    pass\n", 3, "positional-only"),
        (_HEAD + "@asset()\ndef f(*args):\n    pass\n", 3, "*args"),
        (_HEAD + "@asset()\ndef f(**kwargs):\n    pass\n", 3, "**kwargs"),
        (
            _HEAD + "@asset(inputs={'x': 'a.b'})\ndef f(x=None):\n    pass\n",
            3,
            "must not have a default",
        ),
        (
            _HEAD + "@asset(inputs={'x': 'a.b'})\ndef f(*, x=None):\n    pass\n",
            3,
            "must not have a default",
        ),
        (_HEAD + "@asset(inputs={'x': 'weather'})\ndef f(x):\n    pass\n", 2, "input 'x'"),
        (_HEAD + "@asset(inputs={'x': 'a..b'})\ndef f(x):\n    pass\n", 2, "input 'x'"),
    ],
)
def test_refused_shapes(text: str, line: int | None, fragment: str) -> None:
    issues = _issues(text)
    assert any(fragment in issue.message and issue.line == line for issue in issues), issues


def test_a_decorator_above_and_one_below_are_both_issues() -> None:
    issues = _issues(
        _HEAD
        + "import functools\n@functools.cache\n@asset()\n@functools.lru_cache\ndef f():\n    pass\n"
    )
    only = "the asset function must have @asset(...) as its only decorator"
    assert [(i.line, i.message) for i in issues if only in i.message] == [(3, only), (5, only)]


@pytest.mark.parametrize(
    "text", [_HEAD + "@asset(\ndef f():\n", "def f(:\n    pass\n", "x = '\x00'\n"]
)
def test_a_file_that_does_not_parse(text: str) -> None:
    """The line is whatever the interpreter reports; the message is ours."""
    (issue,) = _issues(text)
    assert issue.message.startswith("the file does not parse")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (RecursionError(), "the file is nested too deeply"),
        (MemoryError(), "the file is nested too deeply"),
        (ValueError("null"), "the file does not parse: it contains a null byte"),
    ],
)
def test_parser_failures_other_than_syntax_errors(
    monkeypatch: pytest.MonkeyPatch, error: Exception, message: str
) -> None:
    """3.11 raises `ValueError` for a null byte, 3.12+ a `SyntaxError`; both branches are pinned."""

    def explode(_text: str) -> None:
        raise error

    monkeypatch.setattr(python_assets_module.ast, "parse", explode)
    (issue,) = _issues("x = 1\n")
    assert issue == ProjectIssue(_PATH, None, message)


def test_a_bare_decorator_is_a_deploy_issue() -> None:
    """`@asset` without parentheses."""
    for text in (
        _HEAD + "@asset\ndef f():\n    pass\n",
        "import lakana\n@lakana.asset\ndef f():\n    pass\n",
    ):
        (issue,) = _issues(text)
        assert issue == ProjectIssue(_PATH, 2, "write @asset(...) with parentheses")


def test_a_nested_function_is_not_an_asset() -> None:
    issues = _issues(_HEAD + "def outer():\n    @asset()\n    def inner():\n        pass\n")
    assert "no function decorated with @asset" in issues[0].message


def test_every_issue_of_a_file_is_reported_together() -> None:
    issues = _issues(
        _HEAD + "@asset(owner=3, inputs={'y': 'a.b'}, nope=1)\ndef f(x, *rest):\n    pass\n"
    )
    assert len(issues) >= 4


def test_deep_nesting_is_an_issue_not_a_crash() -> None:
    issues = _issues("x = " + "(" * 5000 + ")" * 5000 + "\n")
    assert "does not parse" in issues[0].message or "nested too deeply" in issues[0].message


_BIG = "0x" + "f" * 20000


@pytest.mark.parametrize("argument", ["retries", "retry_delay", "timeout"])
def test_an_unprintable_integer_is_a_deploy_issue(argument: str) -> None:
    """An integer past Python's 4300-digit limit must not reach `run_policy_issues`."""
    issues = _issues(_HEAD + f"@asset({argument}={_BIG})\ndef f():\n    pass\n")
    assert any(argument in issue.message and issue.line == 2 for issue in issues), issues


def test_the_front_matter_refuses_an_unprintable_integer() -> None:
    with pytest.raises(ProjectValidationError):
        parse_front_matter(f"/* lakana\nretries: {_BIG}\n*/\nSELECT 1\n", path="a.sql")


def test_the_largest_accepted_values_never_break_run_policy() -> None:
    parsed = _parsed(
        _HEAD
        + "@asset(retries=10000, retry_delay=31536000, timeout=31536000)\ndef f():\n    pass\n"
    )
    assert run_policy_issues([parsed], RunCaps())
    assert isinstance(repr(parsed.metadata), str)


@pytest.mark.parametrize(
    ("text", "line", "fragment"),
    [
        (
            _HEAD + "@asset(owner='a', owner='b')\ndef f():\n    pass\n",
            2,
            "repeats argument 'owner'",
        ),
        (
            _HEAD + "@asset(inputs={'x': 'a.b'})\ndef f(x, x):\n    pass\n",
            3,
            "parameter 'x' is declared twice",
        ),
        (
            _HEAD + "@asset(inputs={'x': 'a.b', 'x': 'c.d'})\ndef f(x):\n    pass\n",
            2,
            "'inputs' repeats a key",
        ),
        (
            _HEAD + "@asset(columns={'c': 'one', 'c': 'two'})\ndef f():\n    pass\n",
            2,
            "'columns' repeats a key",
        ),
    ],
)
def test_duplicates_are_refused(text: str, line: int, fragment: str) -> None:
    issues = _issues(text)
    assert any(fragment in issue.message and issue.line == line for issue in issues), issues


_IDENT = "must not contain a control, format or surrogate character or a line separator"
_FREE = "must not contain a surrogate, a control character other than a newline or tab"
_DEF = "\ndef f():\n    pass\n"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (_HEAD + "@asset(owner='\\ud800')" + _DEF, _IDENT),
        (_HEAD + "@asset(owner='a\\x00')" + _DEF, _IDENT),
        (_HEAD + "@asset(owner='a\\u2028b')" + _DEF, _IDENT),
        (_HEAD + "@asset(owner='a\\u200bb')" + _DEF, _IDENT),
        (_HEAD + "@asset(owner='a\\u200db')" + _DEF, _IDENT),
        (_HEAD + "@asset(owner='a\\nb')" + _DEF, _IDENT),
        (_HEAD + "@asset(tags=['a\\x00'])" + _DEF, _IDENT),
        (_HEAD + "@asset(columns={'a\\x00': 'd'})" + _DEF, _IDENT),
        (_HEAD + "@asset(columns={'a\\u200d': 'd'})" + _DEF, _IDENT),
        (_HEAD + "@asset(description='a\\ud800')" + _DEF, _FREE),
        (_HEAD + "@asset(description='a\\x00b')" + _DEF, _FREE),
        (_HEAD + "@asset(description='a\\u202eb')" + _DEF, _FREE),
        (_HEAD + "@asset(description='a\\u2066b')" + _DEF, _FREE),
        (_HEAD + "@asset(description='a\\u200fb')" + _DEF, _FREE),
        (_HEAD + "@asset(description='a\\u061cb')" + _DEF, _FREE),
        (_HEAD + "@asset(description='a\\u2028b')" + _DEF, _FREE),
        (_HEAD + "@asset(description='a\\u2029b')" + _DEF, _FREE),
        (_HEAD + "@asset(description='a\\rb')" + _DEF, _FREE),
        (_HEAD + "@asset(columns={'a': 'd\\x00'})" + _DEF, _FREE),
        (_HEAD + "@asset()\ndef f():\n    '''Doc\\x00string.'''\n", _FREE),
        (_HEAD + "@asset(inputs={'x': 'bronze.\\ud800'})\ndef f(x):\n    pass\n", _IDENT),
        (_HEAD + "@asset(inputs={'x': 'bronze.x\\n'})\ndef f(x):\n    pass\n", _IDENT),
        (_HEAD + "@asset(inputs={'x\\x00': 'bronze.x'})\ndef f(x):\n    pass\n", _IDENT),
        (
            _HEAD + "@asset(inputs={'x': ' bronze.x '})\ndef f(x):\n    pass\n",
            "surrounding whitespace",
        ),
        (
            _HEAD + "@asset(inputs={'x': 'bronze. x'})\ndef f(x):\n    pass\n",
            "surrounding whitespace",
        ),
        (_HEAD + "@asset(inputs={'x': 'bronze.'})\ndef f(x):\n    pass\n", "empty"),
        (_HEAD + "@asset(inputs={'x': '.x'})\ndef f(x):\n    pass\n", "empty"),
    ],
)
def test_unsafe_strings_are_refused(text: str, fragment: str) -> None:
    issues = _issues(text)
    assert any(fragment in issue.message for issue in issues), issues
    assert all(issue.line is not None for issue in issues), issues


_FREE_TEXT = (
    "Wetter \U0001f468\u200d\U0001f469\u200d\U0001f467 "
    "\u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645 Zoe\u0308 \u00fc\n\tsecond line"
)


def test_free_text_keeps_emoji_zwnj_newlines_and_tabs() -> None:
    """Descriptions are free text: only surrogates, NUL and bidi tricks are refused."""
    literal = repr(_FREE_TEXT)
    parsed = _parsed(_HEAD + f"@asset(description={literal}, columns={{'c': {literal}}})" + _DEF)
    assert parsed.metadata.description == _FREE_TEXT
    assert parsed.metadata.columns == {"c": _FREE_TEXT}


def test_a_multi_line_description_is_kept() -> None:
    parsed = _parsed(_HEAD + '@asset(description="""One.\n\nTwo.""")' + _DEF)
    assert parsed.metadata.description == "One.\n\nTwo."


@pytest.mark.parametrize(
    "text", ["a\x00b", "a\ud800b", "a\u202eb", "a\u2028b", "a\u2066b", "a\u200eb", "a\r"]
)
def test_the_front_matter_refuses_unsafe_free_text(text: str) -> None:
    block = f'/* lakana\ndescription: "{text.encode("unicode_escape").decode()}"\n*/\nSELECT 1\n'
    with pytest.raises(ProjectValidationError):
        parse_front_matter(block, path="a.sql")


@pytest.mark.parametrize("field", ["owner", "tags"])
def test_the_front_matter_refuses_an_unsafe_identifier(field: str) -> None:
    value = '"\\uD800"' if field == "owner" else '["a\\u2028b"]'
    with pytest.raises(ProjectValidationError):
        parse_front_matter(f"/* lakana\n{field}: {value}\n*/\nSELECT 1\n", path="a.sql")


def test_the_front_matter_refuses_nul_and_a_surrogate_in_free_text() -> None:
    for value in ('"a\\0b"', '"\\uD800"'):
        with pytest.raises(ProjectValidationError):
            parse_front_matter(f"/* lakana\ndescription: {value}\n*/\nSELECT 1\n", path="a.sql")


def test_the_front_matter_keeps_free_text() -> None:
    block = (
        "/* lakana\n"
        "description: |\n"
        "  Wetter \U0001f468\u200d\U0001f469 \u0645\u06cc\u200c\u062e\u0648\u0627\u0647\u0645\n"
        "  \tsecond line\n"
        "columns:\n"
        '  c: "one\\ntwo"\n'
        "*/\nSELECT 1\n"
    )
    metadata = parse_front_matter(block, path="a.sql").metadata
    assert "\u200d" in (metadata.description or "")
    assert "\u200c" in (metadata.description or "")
    assert metadata.columns == {"c": "one\ntwo"}


_MODEL = '''\
from pathlib import Path

import polars as pl
import lakana
from lakana import asset


@asset(inputs={"train": "gold.training_set"}, output="artifact", keep_versions=2)
def station_scores(train: pl.DataFrame) -> Path:
    """The churn model, retrained on every run."""
    return lakana.output_dir() / "model.json"
'''


def test_an_artifact_asset_parses() -> None:
    parsed = _parsed(_MODEL)
    assert (parsed.output, parsed.keep_versions, parsed.kind) == ("artifact", 2, "python")
    assert parsed.inputs == (("train", "gold.training_set"),)
    assert parsed.metadata.description == "The churn model, retrained on every run."
    assert (_parsed(_GOOD).output, _parsed(_GOOD).keep_versions) == ("table", None)
    assert _parsed(_MODEL.replace(", keep_versions=2", "")).keep_versions is None
    assert _parsed(_MODEL.replace("keep_versions=2", "keep_versions=2147483647")).keep_versions == (
        2**31 - 1
    )


def test_decimals_is_read_from_the_decorator_and_defaults_to_float() -> None:
    assert _parsed(_GOOD).decimals == "float"
    assert _parsed(_MODEL).decimals == "float"
    assert _parsed(_GOOD.replace("@asset(", '@asset(decimals="exact", ', 1)).decimals == "exact"
    assert _parsed(_GOOD.replace("@asset(", '@asset(decimals="float", ', 1)).decimals == "float"
    both = _MODEL.replace("keep_versions=2", 'keep_versions=2, decimals="exact"')
    assert _parsed(both).decimals == "exact"


_DECIMALS = "argument 'decimals' must be 'float' or 'exact'"


@pytest.mark.parametrize(
    ("argument", "message"),
    [
        ('decimals="fixed"', _DECIMALS + ", got 'fixed'"),
        ("decimals=3", "argument 'decimals' must be a string"),
        ("decimals=None", "argument 'decimals' must be a string"),
        ("decimals=KIND", "argument 'decimals' must be a literal"),
        ('decimals=f"{KIND}"', "argument 'decimals' must be a literal"),
        ("decimals=" + "9" * 4000, "argument 'decimals' must be a string"),
    ],
)
def test_a_wrong_decimals_is_a_deploy_issue(argument: str, message: str) -> None:
    source = _GOOD.replace("@asset(", f"@asset({argument}, ", 1)
    messages = [issue.message for issue in _issues(source)]
    assert messages == [message]
    assert not any("9999" in text for text in messages)


_KEEP = "argument 'keep_versions' must be an integer of at least 1"
_TOO_MANY = "argument 'keep_versions' must be at most 2147483647; remove it to keep every version"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            'output="artifact"',
            'output="model"',
            "argument 'output' must be 'table' or 'artifact', got 'model'",
        ),
        ('output="artifact"', "output=KIND", "argument 'output' must be a literal"),
        ('output="artifact"', 'output=f"{KIND}"', "argument 'output' must be a literal"),
        ('output="artifact"', "output=3", "argument 'output' must be a string"),
        ('output="artifact"', "output=None", "argument 'output' must be a string"),
        ("keep_versions=2", "keep_versions=0", _KEEP),
        ("keep_versions=2", "keep_versions=-1", _KEEP),
        ("keep_versions=2", "keep_versions=True", _KEEP),
        ("keep_versions=2", "keep_versions=2.0", _KEEP),
        ("keep_versions=2", 'keep_versions="2"', _KEEP),
        ("keep_versions=2", "keep_versions=2147483648", _TOO_MANY),
        ("keep_versions=2", "keep_versions=" + "9" * 4000, _TOO_MANY),
        ("keep_versions=2", "keep_versions=-" + "9" * 4000, _KEEP),
        (
            'output="artifact", ',
            "",
            "argument 'keep_versions' applies only to an asset with output=\"artifact\"; "
            'remove it or set output="artifact"',
        ),
        (
            "keep_versions=2",
            'materialize="table"',
            "argument 'materialize' does not apply to an artifact asset; remove it",
        ),
        (
            "keep_versions=2",
            'columns={"a": "b"}',
            "argument 'columns' does not apply to an artifact asset; remove it",
        ),
        (
            '"gold.training_set"',
            '"gold.station_scores"',
            "an artifact asset cannot read its own artifact (gold.station_scores); "
            "remove it from inputs",
        ),
        ("keep_versions=2", 'incremental_key="id"', "unknown argument 'incremental_key'"),
    ],
)
def test_an_artifact_argument_that_is_wrong_is_a_deploy_issue(
    old: str, new: str, message: str
) -> None:
    """Each issue names the field and what to do."""
    assert old in _MODEL
    assert message in [issue.message for issue in _issues(_MODEL.replace(old, new))]


@pytest.mark.parametrize("digits", [4300, 5000])
def test_a_huge_integer_is_an_issue_not_an_exception(digits: int) -> None:
    """Python refuses to turn a number of over 4300 digits into text: nothing may try."""
    for argument in ("keep_versions", "output"):
        source = _MODEL.replace("keep_versions=2", f"{argument}={'9' * digits}")
        if argument == "output":
            source = source.replace('output="artifact", ', "")
        messages = [issue.message for issue in _issues(source)]
        assert messages
        assert not any("9999" in message for message in messages)


def test_a_table_asset_may_still_read_its_own_table() -> None:
    source = _GOOD.replace('"silver.weather"', '"gold.station_scores"')
    assert _parsed(source).output == "table"


@pytest.mark.parametrize("output", ['"model"', "3", "None"])
def test_a_badly_shaped_output_does_not_also_blame_keep_versions(output: str) -> None:
    """`keep_versions` is fine with `output="artifact"`; only `output` is reported."""
    source = _MODEL.replace('output="artifact"', f"output={output}")
    messages = [issue.message for issue in _issues(source)]
    assert len(messages) == 1
    assert messages[0].startswith("argument 'output' must be ")


def test_a_badly_shaped_output_still_reports_a_bad_keep_versions() -> None:
    """The two are independent problems; each is reported."""
    source = _MODEL.replace('output="artifact"', 'output="model"').replace(
        "keep_versions=2", "keep_versions=0"
    )
    assert [issue.message for issue in _issues(source)] == [
        "argument 'output' must be 'table' or 'artifact', got 'model'",
        _KEEP,
    ]


@pytest.mark.parametrize(
    ("argument", "dropped"),
    [
        ('output="model"', set()),
        ("output=KIND", {"output"}),
        ("output=3", {"output"}),
        ("output=None", {"output"}),
        ("unknown=1", set()),
        ("keep_versions=KIND", {"keep_versions"}),
        ("tags=3, owner='x'", {"tags"}),
    ],
)
def test_arguments_report_which_keys_they_dropped(argument: str, dropped: set[str]) -> None:
    """No logic depends on the wording of an issue: the dropped keys are returned."""
    import ast

    call = ast.parse(f"asset({argument})", mode="eval").body
    assert isinstance(call, ast.Call)
    issues: list[ProjectIssue] = []
    _, left_out = python_assets_module._arguments(call, "a.py", issues)
    assert left_out == dropped


def test_a_non_literal_output_does_not_also_blame_keep_versions() -> None:
    """`output=KIND` is dropped and reported once; `keep_versions=2` is not blamed for it."""
    source = _MODEL.replace('output="artifact"', "output=KIND")
    assert [issue.message for issue in _issues(source)] == ["argument 'output' must be a literal"]


def test_a_syntax_warning_does_not_leave_the_parser(recwarn: pytest.WarningsRecorder) -> None:
    r"""`ast.parse` warns about `'\d'`; the deploy neither prints it nor fails on it."""
    parsed = _parsed(_HEAD + "PATTERN = '\\d+'\n@asset()\ndef f():\n    pass\n")

    assert parsed.kind == "python"
    assert not [w for w in recwarn if issubclass(w.category, SyntaxWarning)]


def test_a_syntax_warning_is_not_raised_as_an_error() -> None:
    """With warnings turned into errors, the file still parses."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        parsed = _parsed(_HEAD + "PATTERN = '\\d+'\n@asset()\ndef f():\n    pass\n")

    assert parsed.kind == "python"


def test_two_threads_never_parse_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The filter swap around a parse is process-wide, so parses take turns."""
    import threading
    import time

    real_parse = python_assets_module.ast.parse
    lock = threading.Lock()
    inside = 0
    most = 0

    def slow_parse(*args: Any, **kwargs: Any) -> ast.Module:  # ruff: ignore[any-type]
        nonlocal inside, most
        with lock:
            inside += 1
            most = max(most, inside)
        time.sleep(0.01)
        try:
            return real_parse(*args, **kwargs)
        finally:
            with lock:
                inside -= 1

    monkeypatch.setattr(python_assets_module.ast, "parse", slow_parse)
    source = _HEAD + "@asset()\ndef f():\n    pass\n"
    kinds: list[str] = []

    def parse_some() -> None:
        kinds.extend(_parsed(source).kind for _ in range(5))

    threads = [threading.Thread(target=parse_some) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert kinds == ["python"] * 20
    assert most == 1


def test_a_value_error_other_than_a_null_byte_is_not_called_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a null byte is reported as one; any other `ValueError` gets its own text."""

    def explode(_text: str) -> None:
        raise ValueError("something else")

    monkeypatch.setattr(python_assets_module.ast, "parse", explode)
    (issue,) = _issues("x = 1\n")
    assert issue == ProjectIssue(
        _PATH, None, "the file does not parse: Python could not read it as source text"
    )


@pytest.mark.parametrize(
    ("decorator", "echoed"),
    [
        ("@asset(output='" + "o" * 5000 + "')", "'output'"),
        ("@asset(materialize='" + "m" * 5000 + "')", "materialize"),
        ("@asset(" + "k" * 5000 + "=1)", "unknown argument"),
    ],
)
def test_a_message_that_echoes_a_value_bounds_it(decorator: str, echoed: str) -> None:
    """A long value in the file never makes a long message."""
    issues = _issues(_HEAD + decorator + "\ndef f():\n    pass\n")
    assert any(echoed in issue.message for issue in issues)
    assert all(len(issue.message) < 400 for issue in issues)


def test_a_long_parameter_name_is_bounded_in_its_messages() -> None:
    """The parameter name in "declared twice" and "no entry" is cut."""
    name = "p" * 5000
    issues = _issues(_HEAD + f"@asset()\ndef f({name}, {name}):\n    pass\n")
    assert any("declared twice" in issue.message for issue in issues)
    assert any("has no entry in inputs" in issue.message for issue in issues)
    assert all(len(issue.message) < 400 for issue in issues)


def test_an_escape_in_a_value_is_written_out() -> None:
    """A terminal escape in an echoed value reaches the message as text."""
    issues = _issues(_HEAD + "@asset(output='\\x1b[31mred')\ndef f():\n    pass\n")
    assert all("\x1b" not in issue.message for issue in issues)
    assert any("\\x1b[31mred" in issue.message for issue in issues)


@pytest.mark.parametrize(
    "text",
    [
        _HEAD + "class K:\n    @asset()\n    def f(self):\n        pass\n",
        _HEAD + "if True:\n    @asset()\n    def f():\n        pass\n",
        _HEAD + "try:\n    @asset()\n    def f():\n        pass\nexcept ImportError:\n    pass\n",
    ],
    ids=["class method", "conditional", "try block"],
)
def test_a_definition_that_is_not_at_the_top_level_is_not_an_asset(text: str) -> None:
    """Only a function in the module body is found."""
    (issue,) = _issues(text)
    assert "no function decorated with @asset" in issue.message


@pytest.mark.parametrize(
    "argument",
    ["owner=b'x'", "tags={'a'}", "inputs={'x': b'a.b'}", "tags=[b'x']", "description=b''"],
)
def test_bytes_and_sets_are_not_literals_an_asset_accepts(argument: str) -> None:
    """A bytes or a set literal is refused as a non-literal."""
    issues = _issues(_HEAD + f"@asset({argument})\ndef f(x):\n    pass\n")
    assert any("must be a literal" in issue.message for issue in issues), issues


def test_a_file_with_a_byte_order_mark_that_reaches_the_parser_is_an_issue() -> None:
    """Discovery drops a BOM; one that survives (inside the text) is a syntax error, not a crash."""
    (issue,) = _issues("x = 1\n\ufeffy = 2\n")
    assert issue.message.startswith("the file does not parse")


def test_connections_are_read_from_the_decorator_and_default_to_none() -> None:
    assert _parsed(_GOOD).connections == ()
    declared = _GOOD.replace("@asset(", '@asset(connections=["crm-db", "files"], ', 1)
    assert _parsed(declared).connections == ("crm-db", "files")
    assert _parsed(_GOOD.replace("@asset(", "@asset(connections=[], ", 1)).connections == ()
    assert _parsed(_GOOD.replace("@asset(", "@asset(connections=None, ", 1)).connections == ()


def test_the_most_connections_an_asset_may_declare_is_accepted() -> None:
    names = ", ".join(f'"c{index}"' for index in range(16))
    parsed = _parsed(_GOOD.replace("@asset(", f"@asset(connections=[{names}], ", 1))
    assert parsed.connections == tuple(f"c{index}" for index in range(16))


_SEVENTEEN = ", ".join(f'"c{index}"' for index in range(17))


@pytest.mark.parametrize(
    ("argument", "message"),
    [
        ('connections="crm-db"', "argument 'connections' must be a list of strings"),
        ("connections={'a': 'b'}", "argument 'connections' must be a list of strings"),
        ("connections=[1]", "argument 'connections' must be a literal"),
        ("connections=NAMES", "argument 'connections' must be a literal"),
        ('connections=["crm-db", "crm-db"]', "connection `crm-db` is listed twice"),
        (f"connections=[{_SEVENTEEN}]", "an asset declares at most 16 connections"),
        (
            'connections=["CRM"]',
            "connection names are lower-case letters, digits and '-', starting with a letter, "
            "at most 63; got 'CRM'",
        ),
        (
            'connections=["1db"]',
            "connection names are lower-case letters, digits and '-', starting with a letter, "
            "at most 63; got '1db'",
        ),
        (
            'connections=["' + "a" * 64 + '"]',
            "connection names are lower-case letters, digits and '-', starting with a letter, "
            "at most 63; got '" + "a" * 64 + "'",
        ),
        (
            'connections=["crm-db\\n"]',
            "connection names are lower-case letters, digits and '-', starting with a letter, "
            "at most 63; got 'crm-db\\n'",
        ),
    ],
)
def test_a_wrong_connections_is_a_deploy_issue_at_the_decorator(
    argument: str, message: str
) -> None:
    source = _GOOD.replace("@asset(", f"@asset({argument}, ", 1)
    issues = _issues(source)
    assert [issue.message for issue in issues] == [message]
    assert issues[0].line == 5  # the decorator's line in `_GOOD`


def test_a_bad_and_a_repeated_connection_are_both_reported() -> None:
    source = _GOOD.replace("@asset(", '@asset(connections=["Bad", "ok", "ok"], ', 1)
    assert [issue.message for issue in _issues(source)] == [
        "connection names are lower-case letters, digits and '-', starting with a letter, "
        "at most 63; got 'Bad'",
        "connection `ok` is listed twice",
    ]


_APPEND_NEEDS_WATERMARK = (
    "`write: append` needs `watermark:` — the column whose largest value the next run starts after"
)


def test_the_write_model_is_read_from_the_decorator() -> None:
    source = _GOOD.replace(
        "@asset(", '@asset(write="merge", key=["id", "region"], watermark="updated_at", ', 1
    )
    metadata = _parsed(source).metadata
    assert (metadata.write, metadata.key, metadata.watermark) == (
        "merge",
        ("id", "region"),
        "updated_at",
    )
    plain = _parsed(_GOOD).metadata
    assert (plain.write, plain.key, plain.watermark) == (None, (), None)
    appended = _parsed(_GOOD.replace("@asset(", '@asset(write="append", watermark="ts", ', 1))
    assert (appended.metadata.write, appended.metadata.watermark) == ("append", "ts")


@pytest.mark.parametrize(
    ("argument", "message"),
    [
        ('write="upsert"', "write must be one of replace, append, merge; got 'upsert'"),
        ('write="append"', _APPEND_NEEDS_WATERMARK),
        ('write="merge"', "`write: merge` needs `key:` — the columns that identify a row"),
        ('write="merge", key=[]', "`write: merge` needs `key:` — the columns that identify a row"),
        ('write="replace", key=["id"]', "`write: replace` takes neither `key:` nor `watermark:`"),
        ('watermark="ts"', "`write: replace` takes neither `key:` nor `watermark:`"),
        ('write="merge", key=["id", "id"]', "key column `id` is listed twice"),
        (
            'write="append", watermark="ts", key=["id"]',
            "`write: append` takes no `key:`; to merge rows on a key use `write: merge`",
        ),
        (
            'write="merge", key=["i\\u200bd"]',
            "a key column must not contain a control, format or surrogate character or a line "
            "separator",
        ),
        (
            'write="append", watermark="t\\u2028s"',
            "watermark must not contain a control, format or surrogate character or a line "
            "separator",
        ),
        ("write=3", "argument 'write' must be a string"),
        ('key="id"', "argument 'key' must be a list of strings"),
        ("watermark=['ts']", "argument 'watermark' must be a string"),
    ],
)
def test_a_wrong_write_model_is_a_deploy_issue_at_the_decorator(
    argument: str, message: str
) -> None:
    source = _GOOD.replace("@asset(", f"@asset({argument}, ", 1)
    issues = _issues(source)
    assert [issue.message for issue in issues] == [message]
    assert issues[0].line == 5  # the decorator's line in `_GOOD`


@pytest.mark.parametrize("argument", ['write="append", watermark="ts"', 'key=["id"]'])
def test_an_artifact_asset_takes_no_write_model(argument: str) -> None:
    name = argument.split("=", 1)[0]
    messages = [issue.message for issue in _issues(_MODEL.replace("keep_versions=2", argument))]
    assert f"argument {name!r} does not apply to an artifact asset; remove it" in messages


def test_ctx_is_the_context_and_not_an_input() -> None:
    source = _GOOD.replace(
        "def station_scores(obs: pl.DataFrame, stations: pl.DataFrame)",
        "def station_scores(ctx, obs: pl.DataFrame, stations: pl.DataFrame)",
    )
    parsed = _parsed(source)
    assert parsed.inputs == (("obs", "silver.weather"), ("stations", "bronze.stations"))
    alone = _parsed(_HEAD + "@asset()\ndef customers(ctx):\n    pass\n")
    assert alone.inputs == ()
    assert alone.references == ()


def test_an_input_named_ctx_is_refused_with_one_issue() -> None:
    issues = _issues(_HEAD + "@asset(inputs={'ctx': 'silver.a'})\ndef f(ctx):\n    pass\n")
    assert [(issue.line, issue.message) for issue in issues] == [
        (2, "`ctx` is the asset's context; give the input another parameter name")
    ]
