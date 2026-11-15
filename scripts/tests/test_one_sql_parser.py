"""Every parse of SQL text in the packages goes through `lakana_core.sql_dialect` (#415).

sqlglot's stock DuckDB reading differs from Lakana's (`~` is a whole-text match, a lambda written
with `LAMBDA` is marked), and the query guard checks what Lakana's reading gives. A second parser
anywhere would let the checked tree and the run tree drift apart, so a source scan refuses one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parents[2] / "packages"
ALLOWED = PACKAGES / "lakana-core" / "src" / "lakana_core" / "sql_dialect.py"
PARSERS = frozenset({"parse", "parse_one", "transpile", "maybe_parse"})


def _sqlglot_names(tree: ast.Module) -> set[str]:
    """Name every local alias of sqlglot or one of its modules.

    Parameters
    ----------
    tree
        A parsed source file.

    Returns
    -------
    set of str
        `sqlglot`, `exp`, and so on, as the file binds them.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlglot" or alias.name.startswith("sqlglot."):
                    names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlglot"):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def _root(node: ast.expr) -> str | None:
    """Return the leftmost name of a dotted expression.

    Parameters
    ----------
    node
        The receiver of an attribute call.

    Returns
    -------
    str or None
        `sqlglot` for `sqlglot.expressions.maybe_parse`, `None` for anything not a dotted name.
    """
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _str_parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Name the parameters of `function` annotated `str`.

    Parameters
    ----------
    function
        A function definition.

    Returns
    -------
    set of str
        Those parameter names.
    """
    arguments = function.args
    every = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
    return {
        argument.arg
        for argument in every
        if isinstance(argument.annotation, ast.Name) and argument.annotation.id == "str"
    }


def _is_text(node: ast.expr, text_names: set[str]) -> bool:
    """Say whether an argument is SQL text rather than a parsed tree.

    Parameters
    ----------
    node
        The argument.
    text_names
        Names known to hold text: the enclosing function's `str` parameters.

    Returns
    -------
    bool
        Whether it is a string literal, an f-string or one of `text_names`.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.JoinedStr):
        return True
    return isinstance(node, ast.Name) and node.id in text_names


def second_parsers(source: str) -> list[str]:
    """List every place in `source` that parses SQL text without `lakana_core.sql_dialect`.

    Parameters
    ----------
    source
        A Python source file.

    Returns
    -------
    list of str
        One `line N: what` entry per finding.
    """
    tree = ast.parse(source)
    aliases = _sqlglot_names(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlglot"):
            for alias in node.names:
                if alias.name in PARSERS:
                    found.append(f"line {node.lineno}: imports sqlglot's {alias.name}")
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            continue
        text_names = _str_parameters(node) if not isinstance(node, ast.Module) else set()
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if isinstance(func, ast.Attribute) and name in PARSERS and _root(func) in aliases:
                found.append(f"line {call.lineno}: calls sqlglot's {name}")
            sqlglots = func.id in aliases if isinstance(func, ast.Name) else _root(func) in aliases
            if name == "lineage" and sqlglots:
                sql = call.args[1] if len(call.args) > 1 else None
                sql = next((k.value for k in call.keywords if k.arg == "sql"), sql)
                if sql is not None and _is_text(sql, text_names):
                    found.append(f"line {call.lineno}: hands lineage SQL text to parse")
    return sorted(set(found))


def _sources() -> list[Path]:
    """List the package source files the rule covers.

    Returns
    -------
    list of Path
        Every `.py` under `packages/*/src`, except `sql_dialect.py`.
    """
    return sorted(p for p in PACKAGES.glob("*/src/**/*.py") if p != ALLOWED)


def test_the_scan_covers_the_package_sources() -> None:
    """The glob finds the sources, so an empty scan cannot pass for a clean one."""
    paths = _sources()

    assert any(p.name == "query_rewrite.py" for p in paths)
    assert any("lakana-worker" in p.parts for p in paths)


def test_no_package_parses_sql_text_outside_sql_dialect() -> None:
    """Only `lakana_core.sql_dialect` hands SQL text to sqlglot's parser."""
    findings = {
        str(path.relative_to(PACKAGES)): hits
        for path in _sources()
        if (hits := second_parsers(path.read_text(encoding="utf-8")))
    }

    assert findings == {}, (
        "Parse SQL with lakana_core.sql_dialect.parse_one / parse, and hand sqlglot's lineage "
        f"the parsed tree: {findings}"
    )


@pytest.mark.parametrize(
    "source",
    [
        "import sqlglot\nsqlglot.parse_one('SELECT 1')\n",
        "import sqlglot\nsqlglot.parse('SELECT 1')\n",
        "import sqlglot\nsqlglot.transpile('SELECT 1')\n",
        "import sqlglot.expressions as exp\nexp.maybe_parse('a')\n",
        "from sqlglot import parse_one\n",
        "from sqlglot.expressions import maybe_parse\n",
        "import sqlglot.lineage\n"
        "def f(sql: str) -> None:\n"
        "    sqlglot.lineage.lineage('a', sql, schema={})\n",
        "from sqlglot.lineage import lineage\nlineage('a', sql='SELECT a FROM t', schema={})\n",
    ],
)
def test_the_scan_finds_a_second_parser(source: str) -> None:
    """Each way of parsing SQL text past `sql_dialect` is a finding.

    Parameters
    ----------
    source
        A file that parses SQL text with sqlglot directly.
    """
    assert second_parsers(source)


@pytest.mark.parametrize(
    "source",
    [
        "from lakana_core import sql_dialect\nsql_dialect.parse_one('SELECT 1')\n",
        "import sqlglot.lineage\n"
        "def f(parsed: object) -> None:\n"
        "    sqlglot.lineage.lineage('a', parsed, schema={})\n",
        "import argparse\nargparse.ArgumentParser().parse_args()\n",
        "def f(client: object, name: str) -> None:\n    client.tables.lineage('dev', name)\n",
        "def parse(text: str) -> str:\n    return text\nparse('x')\n",
    ],
)
def test_the_scan_leaves_lakanas_own_parsing_alone(source: str) -> None:
    """Calls through `sql_dialect`, a lineage of a tree, and other `parse`s are not findings.

    Parameters
    ----------
    source
        A file that parses no SQL text with sqlglot directly.
    """
    assert second_parsers(source) == []
