"""Tests that no package's own tests import what the package must not depend on.

import-linter reads the packages' ``src`` trees only, so a test that imported ``duckdb`` or the
worker into lakana-core's tests passed ``lint-imports`` while it inverted the layering and made a
per-package test run fail to collect. This is the gate for the tests: it reads every ``.py`` file
under ``packages/<package>/tests`` and fails on an import of anything in that package's list.
"""

from __future__ import annotations

import ast
import configparser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = {
    # The core sits below the server and the worker, and only the worker may import DuckDB.
    "lakana-core": ("lakana_worker", "lakana_server", "duckdb"),
    # The SDK depends on none of the other packages (the `sdk-is-independent` contract).
    "lakana": ("lakana_core", "lakana_server", "lakana_worker"),
}


def forbidden_imports(source: str, forbidden: tuple[str, ...]) -> list[tuple[int, str]]:
    """Find the imports of forbidden top-level modules in a Python source.

    Imports anywhere count (inside a function, under ``TYPE_CHECKING``), and so do calls of
    ``importlib.import_module("x")`` and ``__import__("x")`` with a literal name.

    Parameters
    ----------
    source
        Python source text.
    forbidden
        Top-level module names; ``duckdb`` forbids ``duckdb`` and ``duckdb.anything``.

    Returns
    -------
    list of tuple
        ``(line, module)`` for each import found.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        elif (
            isinstance(node, ast.Call)
            and _called_name(node) in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names = [node.args[0].value]
        found.extend((node.lineno, name) for name in names if name.split(".")[0] in forbidden)
    return sorted(found)


def _called_name(call: ast.Call) -> str:
    """Name the function a call invokes, for ``f(...)`` and ``m.f(...)``.

    Parameters
    ----------
    call
        A call node.

    Returns
    -------
    str
        The attribute or the plain name; empty for anything else.
    """
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import duckdb", [(1, "duckdb")]),
        ("import pyarrow, duckdb as d", [(1, "duckdb")]),
        ("import duckdb.typing", [(1, "duckdb.typing")]),
        ("from duckdb import connect", [(1, "duckdb")]),
        ("from lakana_worker.executor import execute_query", [(1, "lakana_worker.executor")]),
        ("def f():\n    import lakana_server\n", [(2, "lakana_server")]),
        ("if TYPE_CHECKING:\n    from lakana_worker import x\n", [(2, "lakana_worker")]),
        (
            "import importlib\nimportlib.import_module('lakana_worker.tasks')",
            [(2, "lakana_worker.tasks")],
        ),
        ("__import__('duckdb')", [(1, "duckdb")]),
        ("from . import duckdb", []),
        ("import lakana_workers", []),
        ("import my_duckdb", []),
        ("from lakana_core import x", []),
        ('"""Uses duckdb and lakana_worker."""\n# import duckdb\n', []),
        ("importlib.import_module(name)", []),
        ("importlib.import_module(1)", []),
        ("importlib.import_module()", []),
        ("registry['x'](\"duckdb\")", []),
        ("(lambda: 1)()", []),
    ],
)
def test_the_checker_finds_every_form_of_import_and_nothing_else(
    source: str, expected: list[tuple[int, str]]
) -> None:
    """An import statement or a literal dynamic import of a forbidden module is a violation."""
    assert forbidden_imports(source, ("duckdb", "lakana_worker", "lakana_server")) == expected


def _test_files(package: str) -> list[Path]:
    """List the Python files of a package's tests.

    Parameters
    ----------
    package
        Directory name under ``packages``.

    Returns
    -------
    list of Path
        Every ``.py`` file under the package's ``tests`` directory.
    """
    return sorted((ROOT / "packages" / package / "tests").rglob("*.py"))


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
@pytest.mark.slow
def test_a_package_s_tests_do_not_import_what_the_package_must_not_depend_on(
    package: str,
) -> None:
    """Move the test to the package that may import the module, or change the design."""
    files = _test_files(package)
    assert files, f"no tests found for {package}: this gate would pass without checking anything"
    violations = [
        f"{path.relative_to(ROOT)}:{line} imports {module}"
        for path in files
        for line, module in forbidden_imports(path.read_text(), FORBIDDEN[package])
    ]
    assert violations == [], (
        f"{package}'s tests import what the package must not depend on; move the test into "
        "the package that may (for DuckDB: packages/lakana-worker/tests):\n" + "\n".join(violations)
    )


@pytest.mark.slow
def test_only_the_query_child_imports_duckdb_in_the_packages_source() -> None:
    """The query process is the one place DuckDB runs; every other module goes through it."""
    allowed = ROOT / "packages" / "lakana-worker" / "src" / "lakana_worker" / "query_child.py"
    files = sorted((ROOT / "packages").glob("*/src/**/*.py"))
    assert allowed in files
    violations = [
        f"{path.relative_to(ROOT)}:{line} imports {module}"
        for path in files
        if path != allowed
        for line, module in forbidden_imports(path.read_text(), ("duckdb",))
    ]
    assert violations == []


def test_import_linter_holds_the_duckdb_rule_as_a_contract() -> None:
    """`lint-imports` enforces the rule, so a new import fails CI and not only this test."""
    config = configparser.ConfigParser()
    config.read(ROOT / ".importlinter")
    contract = config["importlinter:contract:only-the-query-child-imports-duckdb"]
    assert contract["type"] == "forbidden"
    assert contract["source_modules"].split() == [
        "lakana",
        "lakana_core",
        "lakana_server",
        "lakana_worker",
    ]
    assert contract["forbidden_modules"].split() == ["duckdb"]
    assert contract["ignore_imports"].strip() == "lakana_worker.query_child -> duckdb"
