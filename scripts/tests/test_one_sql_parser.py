"""Every parse of SQL text in the packages goes through `lakana_core.sql_dialect` (#415).

sqlglot's stock DuckDB reading differs from Lakana's (`~` is a whole-text match, a lambda written
with `LAMBDA` is marked), and the query guard checks what Lakana's reading gives. A second parser
anywhere would let the checked tree and the run tree drift apart, so a source scan refuses one.
"""

from __future__ import annotations

import ast
import unicodedata
from pathlib import Path

import pytest

PACKAGES = Path(__file__).resolve().parents[2] / "packages"
ALLOWED = PACKAGES / "lakana-core" / "src" / "lakana_core" / "sql_dialect.py"
PARSERS = frozenset({"parse", "parse_one", "parse_into", "transpile", "maybe_parse"})
"""sqlglot's functions and methods that read SQL text; a call of one rooted in sqlglot is found."""

DIALECT_PARTS = frozenset({"Dialect", "Dialects", "dialects", "Parser", "parser", "parsers"})
"""Names in sqlglot that reach a dialect's own parser; any use of one outside `sql_dialect.py`
is a finding, so a dialect object kept in a variable cannot parse past the scan."""

LINEAGE_ALLOWED = {
    "lakana-core/src/lakana_core/lineage.py:extract_lineage": (
        "hands sqlglot's lineage the tree sql_dialect.parse_one returned; lineage copies a tree "
        "and parses only text (#415)"
    ),
}
"""Every function that may call sqlglot's `lineage`, with why: any other call is a finding.

`lineage` parses a text argument (and every `sources=` value) with sqlglot's own reading, and
whether an argument holds text cannot be told from the source, so the rule fails closed.
"""


def _sqlglot_names(tree: ast.Module) -> dict[str, str]:
    """Map every local name a file binds to sqlglot or a part of it to that part's dotted name.

    Parameters
    ----------
    tree
        A parsed source file.

    Returns
    -------
    dict of str to str
        `{"sqlglot": "sqlglot", "exp": "sqlglot.expressions", "trace": "sqlglot.lineage.lineage"}`
        and so on, as the file binds them.
    """
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlglot" or alias.name.startswith("sqlglot."):
                    if alias.asname:
                        names[alias.asname] = alias.name
                    else:
                        names[alias.name.split(".")[0]] = "sqlglot"
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlglot"):
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _dotted(node: ast.expr, names: dict[str, str]) -> str | None:
    """Return the sqlglot dotted name an expression reaches, through attributes and calls.

    Parameters
    ----------
    node
        A callee or receiver, such as `sqlglot.Dialect.get_or_raise("duckdb").parse_one`.
    names
        The file's sqlglot names (`_sqlglot_names`).

    Returns
    -------
    str or None
        `sqlglot.Dialect.get_or_raise().parse_one` for that example (a call is written `()`),
        `None` when the expression is not rooted in a sqlglot name.
    """
    parts: list[str] = []
    while True:
        if isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Call):
            parts.append("()")
            node = node.func
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            break
    if not isinstance(node, ast.Name) or node.id not in names:
        return None
    dotted = names[node.id]
    for part in reversed(parts):
        dotted = f"{dotted}{part}" if part == "()" else f"{dotted}.{part}"
    return dotted


def _is_text(node: ast.expr) -> bool:
    """Say whether an argument is written as SQL text: a string literal or an f-string.

    Parameters
    ----------
    node
        The argument.

    Returns
    -------
    bool
        Whether it is literal text.
    """
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


class _Scan(ast.NodeVisitor):
    """Collect the findings of one file, knowing the function each call sits in."""

    def __init__(self, names: dict[str, str], lineage_allowed: set[str]) -> None:
        self.names = names
        self.lineage_allowed = lineage_allowed
        self.functions: list[str] = []
        self.found: list[str] = []

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if (node.module or "").startswith("sqlglot"):
            for alias in node.names:
                if alias.name in PARSERS:
                    self.found.append(f"line {node.lineno}: imports sqlglot's {alias.name}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._reference(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if not self._reference(node):
            self.generic_visit(node)

    def _reference(self, node: ast.Name | ast.Attribute) -> bool:
        """Record a name that reaches a sqlglot parser, called or not; say whether it did.

        Parameters
        ----------
        node
            A name or an attribute, such as `sqlglot.lineage.lineage`.

        Returns
        -------
        bool
            Whether it is a finding (its parts are then not looked at again).
        """
        dotted = _dotted(node, self.names)
        if dotted is None:
            return False
        last = dotted.rsplit(".", 1)[-1]
        if DIALECT_PARTS & set(dotted.replace("()", "").split(".")):
            self.found.append(f"line {node.lineno}: reaches a sqlglot dialect or parser ({dotted})")
        elif last in PARSERS:
            self.found.append(f"line {node.lineno}: uses sqlglot's {last}")
        elif dotted == "sqlglot.lineage.lineage":
            if (self.functions[-1] if self.functions else None) in self.lineage_allowed:
                return False
            self.found.append(f"line {node.lineno}: uses sqlglot's lineage outside the allow-list")
        else:
            return False
        return True

    def visit_Call(self, node: ast.Call) -> None:
        if _dotted(node.func, self.names) == "sqlglot.lineage.lineage":
            self._lineage(node)
        self.generic_visit(node)

    def _lineage(self, call: ast.Call) -> None:
        """Record what an allowed `lineage` call hands sqlglot to parse.

        Parameters
        ----------
        call
            A call of sqlglot's `lineage`; outside the allow-list `_reference` records it.
        """
        if (self.functions[-1] if self.functions else None) not in self.lineage_allowed:
            return
        sql = call.args[1] if len(call.args) > 1 else None
        sql = next((k.value for k in call.keywords if k.arg == "sql"), sql)
        if sql is None or _is_text(sql):
            self.found.append(f"line {call.lineno}: hands lineage SQL text to parse")
        # `sources` is the fourth parameter; `*args` or `**kwargs` may hold it too.
        unpacked = any(isinstance(a, ast.Starred) for a in call.args)
        if unpacked or len(call.args) > 3 or any(k.arg in {"sources", None} for k in call.keywords):
            self.found.append(f"line {call.lineno}: hands lineage sources to parse")


def second_parsers(source: str, lineage_allowed: set[str] | None = None) -> list[str]:
    """List every place in `source` that may parse SQL text without `lakana_core.sql_dialect`.

    Parameters
    ----------
    source
        A Python source file.
    lineage_allowed
        The functions of this file `LINEAGE_ALLOWED` names; none when omitted.

    Returns
    -------
    list of str
        One `line N: what` entry per finding.
    """
    tree = ast.parse(source)
    scan = _Scan(_sqlglot_names(tree), lineage_allowed or set())
    scan.visit(tree)
    return sorted(set(scan.found))


def _sources() -> list[Path]:
    """List the package source files the rule covers.

    Returns
    -------
    list of Path
        Every `.py` under `packages/*/src`, except `sql_dialect.py`.
    """
    return sorted(p for p in PACKAGES.glob("*/src/**/*.py") if p != ALLOWED)


def _mentions_sqlglot(text: str) -> bool:
    """Say whether a source file can bind a sqlglot name, so a file that cannot is not parsed.

    Every way the scan recognises sqlglot is an `import sqlglot...` or `from sqlglot... import`
    statement, and both spell the module name in the file text. Python reads an identifier in
    NFKC form (`import ｓqlglot` binds sqlglot), so the text is normalised the same way first.

    Parameters
    ----------
    text
        A Python source file.

    Returns
    -------
    bool
        Whether `sqlglot` appears in the text after NFKC normalisation.
    """
    return "sqlglot" in unicodedata.normalize("NFKC", text)


def test_the_scan_covers_the_package_sources() -> None:
    """The glob finds the sources, so an empty scan cannot pass for a clean one."""
    paths = _sources()

    assert any(p.name == "query_rewrite.py" for p in paths)
    assert any("lakana-worker" in p.parts for p in paths)


def test_no_package_parses_sql_text_outside_sql_dialect() -> None:
    """Only `lakana_core.sql_dialect` hands SQL text to sqlglot's parser."""
    findings = {}
    for path in _sources():
        relative = path.relative_to(PACKAGES).as_posix()
        allowed = {e.split(":")[1] for e in LINEAGE_ALLOWED if e.split(":")[0] == relative}
        text = path.read_text(encoding="utf-8")
        if _mentions_sqlglot(text) and (hits := second_parsers(text, allowed)):
            findings[relative] = hits

    assert findings == {}, (
        "Parse SQL with lakana_core.sql_dialect.parse_one / parse; hand sqlglot's lineage the "
        f"parsed tree from a function LINEAGE_ALLOWED names, with why: {findings}"
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
        "import sqlglot.lineage\n"
        "def f(run: object) -> None:\n"
        "    sqlglot.lineage.lineage('a', run.sql_text, schema={})\n",
        "import sqlglot.lineage\n"
        "def f(run: object) -> None:\n"
        "    sql = run.sql_text\n"
        "    sqlglot.lineage.lineage('a', sql, schema={})\n",
        "import sqlglot.lineage\n"
        "def f(sql: str | None) -> None:\n"
        "    sqlglot.lineage.lineage('a', sql, schema={})\n",
        "from sqlglot.lineage import lineage as trace\n"
        "def f(sql: str) -> None:\n"
        "    trace('a', sql, schema={})\n",
        "from sqlglot import lineage\n"
        "def f(tree: object) -> None:\n"
        "    lineage.lineage('a', tree, schema={})\n",
        "import sqlglot\nsqlglot.Dialect.get_or_raise('duckdb').parse_one(sql)\n",
        "import sqlglot\nreader = sqlglot.Dialect.get_or_raise('duckdb')\nreader.parse(sql)\n",
        "from sqlglot.dialects.duckdb import DuckDB\nDuckDB().parse(sql)\n",
        "import sqlglot\nsqlglot.parser.Parser().parse(tokens)\n",
        "import sqlglot\nread = sqlglot.parse_one\nread(sql)\n",
        "import sqlglot.lineage\n"
        "trace = sqlglot.lineage.lineage\n"
        "def f(tree: object) -> None:\n"
        "    trace('a', tree, schema={})\n",
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
        "import sqlglot\nfrom lakana_core import sql_dialect\n"
        "sqlglot.tokenize(text, dialect=sql_dialect.reading_dialect('duckdb'))\n",
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


def test_an_allowed_lineage_call_is_left_alone() -> None:
    """A `lineage` call in a function the allow-list names, handed a tree, is not a finding."""
    source = (
        "import sqlglot.lineage\n"
        "def f(parsed: object) -> None:\n"
        "    sqlglot.lineage.lineage('a', parsed, schema={})\n"
    )

    assert second_parsers(source, lineage_allowed={"f"}) == []
    assert second_parsers(source) == ["line 3: uses sqlglot's lineage outside the allow-list"]


@pytest.mark.parametrize(
    "call",
    [
        "sqlglot.lineage.lineage('a', 'SELECT a FROM t', schema={})",
        "sqlglot.lineage.lineage('a', parsed, schema={}, sources={'v': text})",
        "sqlglot.lineage.lineage('a', parsed, {}, {'v': text})",
        "sqlglot.lineage.lineage('a', *[text], schema={})",
    ],
)
def test_an_allowed_lineage_call_still_may_not_take_text(call: str) -> None:
    """The allow-list names a function, not a licence: SQL text or `sources=` stays a finding.

    Parameters
    ----------
    call
        A lineage call that hands sqlglot text to parse with its own reading.
    """
    source = f"import sqlglot.lineage\ndef f(parsed: object, text: str) -> None:\n    {call}\n"

    assert second_parsers(source, lineage_allowed={"f"})


def test_every_allowed_lineage_call_exists() -> None:
    """An allow-list entry whose call is gone is removed, so it cannot cover a new one later."""
    for entry in LINEAGE_ALLOWED:
        path, function = entry.split(":")
        source = (PACKAGES / path).read_text(encoding="utf-8")

        assert second_parsers(source) == second_parsers(source, lineage_allowed=set()) != [], entry
        assert second_parsers(source, lineage_allowed={function}) == [], entry


def test_the_text_check_keeps_every_form_of_the_import() -> None:
    """A file that binds sqlglot under another name, or spells it oddly, is still parsed."""
    assert _mentions_sqlglot("from sqlglot import parse_one as p\n")
    assert _mentions_sqlglot("import sqlglot as s\n")
    assert _mentions_sqlglot("import \uff53qlglot\n")
    assert not _mentions_sqlglot("import argparse\n")
