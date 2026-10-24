"""Repo hygiene: no user-facing text says "job version" any more.

A deployed snapshot of a project is a *project version*. The word "job" names an entry of
`lakana.yaml`, so "job version" would read as the version of such an entry. The internal
`JobVersion` model, the `job_versions` table and the Python identifiers built on them keep their
names; this test looks at text a person reads, not at code.

What is scanned:

- every Markdown page under `docs/site/` (the user documentation) and the READMEs;
- `docs/agent/*.md`, where only the code names of `AGENT_DOC_CODE_NAMES` may stay;
- the OpenAPI schema of the server (paths and every property name) and the field names of the
  SDK dataclasses, which a string-literal scan cannot see;
- every string literal under `packages/*/src` (messages, help texts, docstrings, which become
  help and API documentation), except the migrations, which are history;
- the `--help` of every command of the command line.

`CHANGELOG.md` is history and is not scanned.

The exemptions are narrow, and each is checked by a test below:

- A compound Python identifier that exists in the source and contains the old name
  (`JobVersionView`, `job_version_id`) may be named in a docstring, because a docstring has to
  name the attribute it documents. The bare words `JobVersion` and `job_version` are never
  exempt that way: they are the prose this test hunts.
- A string that is one bare word (`"JobVersion"` in an `__all__`, a relationship target, a
  dictionary key): it names code, it is not shown as prose. The table name `job_versions` and
  its columns (`"job_versions.id"`) are exempt as a string of their own, too.
- The one paragraph of the `JobVersion` model docstring that explains the name.
- In the docstrings of `lakana-core` and `lakana-worker`, the type line of a ``Parameters`` or
  ``Returns`` entry: it names a real Python type (``JobVersion or None``). Server and SDK
  docstrings become API documentation and stay prose.
- In `docs/site/explanation/upgrades.md` only, the lines between `<!-- old-name -->` and
  `<!-- /old-name -->`: the upgrade note has to name the old command and route to tell a reader
  what to change.
"""

import ast
import base64
import re
from pathlib import Path

import click
import pytest
from typer.main import get_command
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[2]
OLD_NAME = re.compile(r"(?i)job[ _-]?versions?")
DOC_DIRS = ("docs/site",)
DOC_FILES = ("README.md", "packages/lakana/README.md", "packages/lakana-core/README.md")
UPGRADE_NOTES = "docs/site/explanation/upgrades.md"
OPEN_MARK = "<!-- old-name -->"
CLOSE_MARK = "<!-- /old-name -->"
AGENT_DOCS = "docs/agent"
AGENT_DOC_CODE_NAMES = {
    "docs/agent/architecture.md": ("`JobVersion`", "`job_version_id`"),
    "docs/agent/security.md": ("`Run.job_version_id`", "`DeployService.job_version`"),
}
"""The code names that stay in the developer docs: the model, a column and a method."""
EXPLANATION_START = "Users call it a project version."
TYPE_LINE_PACKAGES = ("packages/lakana-core/", "packages/lakana-worker/")
MODEL_FILE = "packages/lakana-core/src/lakana_core/models/deploy.py"
BARE_NAMES = frozenset({"JobVersion", "JobVersions", "job_version", "job_versions"})
CODE_NAME = re.compile(r"^(\w+|job_versions\.\w+)$")


def _source_files() -> list[Path]:
    return sorted(
        p
        for p in ROOT.glob("packages/*/src/**/*.py")
        if "/migrations/" not in p.as_posix() and ".venv" not in p.parts
    )


def _doc_files() -> list[Path]:
    files = [p for d in DOC_DIRS for p in (ROOT / d).rglob("*.md")]
    files += [ROOT / f for f in DOC_FILES if (ROOT / f).is_file()]
    return sorted(files)


def doc_hits(path: str, text: str) -> list[str]:
    """Find the lines of one Markdown page that say "job version".

    Parameters
    ----------
    path
        Path of the page relative to the repository root.
    text
        Its content.

    Returns
    -------
    list[str]
        One ``path:line: text`` entry per offending line.
    """
    hits = []
    exempt = False
    for number, line in enumerate(text.splitlines(), start=1):
        if path == UPGRADE_NOTES and OPEN_MARK in line:
            exempt = True
        elif path == UPGRADE_NOTES and CLOSE_MARK in line:
            exempt = False
        elif not exempt and OLD_NAME.search(line):
            hits.append(f"{path}:{number}: {line.strip()[:120]}")
    return hits


def agent_doc_hits(path: str, text: str) -> list[str]:
    """Find the lines of a developer page that say "job version" outside the allowed code names.

    Parameters
    ----------
    path
        Path of the page relative to the repository root.
    text
        Its content.

    Returns
    -------
    list[str]
        One ``path:line: text`` entry per offending line.
    """
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        scanned = line
        for name in AGENT_DOC_CODE_NAMES.get(path, ()):
            scanned = scanned.replace(name, "")
        if OLD_NAME.search(scanned):
            hits.append(f"{path}:{number}: {line.strip()[:120]}")
    return hits


def without_type_lines(text: str) -> str:
    """Drop the type lines of the Parameters and Returns sections of a numpydoc docstring.

    Parameters
    ----------
    text
        The docstring.

    Returns
    -------
    str
        The docstring without those lines.
    """
    lines = text.splitlines()
    kept = []
    section_indent: int | None = None
    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        nxt = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if line.strip() in {"Parameters", "Returns", "Yields"} and set(nxt) == {"-"}:
            section_indent = indent
        elif (set(line.strip()) == {"-"} and line.strip()) or not line.strip():
            pass
        elif section_indent is not None and indent < section_indent:
            section_indent = None
        elif section_indent is not None and indent == section_indent:
            continue
        kept.append(line)
    return "\n".join(kept)


def without_explanation(text: str) -> str:
    """Drop the paragraph that explains the model's name.

    Parameters
    ----------
    text
        The docstring of the model.

    Returns
    -------
    str
        The docstring without that paragraph.
    """
    paragraphs = re.split(r"\n\s*\n", text)
    return "\n\n".join(p for p in paragraphs if not p.strip().startswith(EXPLANATION_START))


def _compound_identifiers(trees: list[ast.AST]) -> set[str]:
    """Collect the Python identifiers that contain the old name, bare ones left out.

    Parameters
    ----------
    trees
        The parsed source files.

    Returns
    -------
    set[str]
        The identifiers.
    """
    names: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            found: list[str] = []
            if isinstance(node, ast.Name):
                found.append(node.id)
            elif isinstance(node, ast.Attribute):
                found.append(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found.append(node.name)
            elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg):
                found.append(node.arg)
            elif isinstance(node, ast.alias):
                found.append(node.asname or node.name)
            names.update(n for n in found if OLD_NAME.search(n) and n not in BARE_NAMES)
    return names


def string_hits(text: str, identifiers: set[str]) -> bool:
    """Tell whether a string literal says "job version" in words a person reads.

    Parameters
    ----------
    text
        The string literal.
    identifiers
        Compound Python identifiers that exist in the source; naming one is not a hit.

    Returns
    -------
    bool
        Whether the literal has a hit.
    """
    if CODE_NAME.match(text):
        return False
    if identifiers:
        pattern = "|".join(re.escape(n) for n in sorted(identifiers, key=len, reverse=True))
        text = re.sub(rf"\b(?:{pattern})\b", "", text)
    return bool(OLD_NAME.search(text))


def _model_docstring_node(tree: ast.Module) -> ast.Constant | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "JobVersion":
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                return first.value
    return None


def _docstring_nodes(tree: ast.Module) -> list[ast.Constant]:
    nodes = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                nodes.append(first.value)
    return nodes


def _source_hits() -> list[str]:
    parsed = [
        (p.relative_to(ROOT).as_posix(), ast.parse(p.read_text(encoding="utf-8")))
        for p in _source_files()
    ]
    identifiers = _compound_identifiers([tree for _, tree in parsed])
    hits = []
    for rel, tree in parsed:
        model_doc = _model_docstring_node(tree) if rel == MODEL_FILE else None
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            text = node.value
            if node is model_doc:
                text = without_explanation(text)
            elif node in docstrings and rel.startswith(TYPE_LINE_PACKAGES):
                text = without_type_lines(text)
            if string_hits(text, identifiers):
                hits.append(f"{rel}:{node.lineno}: {node.value.strip()[:100]!r}")
    return hits


def _help_texts() -> list[tuple[str, str]]:
    from lakana.cli.app import build_app

    runner = CliRunner()
    app = build_app()
    root = get_command(app)
    out: list[tuple[str, str]] = []

    def visit(command: click.Command, path: list[str]) -> None:
        result = runner.invoke(app, [*path, "--help"], terminal_width=200)
        out.append((" ".join(path) or "lakana", result.output))
        for name, sub in getattr(command, "commands", {}).items():
            visit(sub, [*path, name])

    visit(root, [])
    return out


def test_no_page_says_job_version() -> None:
    hits = [
        h
        for p in _doc_files()
        for h in doc_hits(p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8"))
    ]
    assert not hits, f"{len(hits)} lines say 'job version':\n" + "\n".join(hits[:60])


def test_no_developer_page_says_job_version_outside_the_code_names() -> None:
    hits = [
        h
        for p in sorted((ROOT / AGENT_DOCS).glob("*.md"))
        for h in agent_doc_hits(p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8"))
    ]
    assert not hits, f"{len(hits)} lines say 'job version':\n" + "\n".join(hits[:60])


@pytest.mark.slow
def test_the_api_schema_and_the_sdk_fields_do_not_carry_the_old_name(tmp_path: Path) -> None:
    import dataclasses
    import inspect
    import json

    from lakana import _models
    from lakana_core.config import DatabaseConfig, SecretsConfig
    from lakana_server.app import create_app
    from lakana_server.settings import Settings

    settings = Settings(
        database=DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"),
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="testsecret",
        secrets=SecretsConfig(master_key=base64.b64encode(b"0" * 32).decode()),
    )
    spec = create_app(settings).openapi()
    names = set(spec["paths"])
    for schema in spec.get("components", {}).get("schemas", {}).values():
        names.update(schema.get("properties", {}))
        names.add(schema.get("title", ""))
    assert any("project_version" in n or "project-version" in n for n in names)
    assert "project-versions" in json.dumps(spec["paths"])
    for _, cls in inspect.getmembers(_models, dataclasses.is_dataclass):
        if isinstance(cls, type):
            names.update(f.name for f in dataclasses.fields(cls))
            names.add(cls.__name__)
    hits = sorted(n for n in names if OLD_NAME.search(n))
    assert not hits, f"the API schema or the SDK still carries the old name: {hits}"


@pytest.mark.slow
def test_no_message_or_docstring_says_job_version() -> None:
    hits = _source_hits()
    assert not hits, f"{len(hits)} string literals say 'job version':\n" + "\n".join(hits[:80])


@pytest.mark.slow
def test_no_command_help_says_job_version() -> None:
    texts = _help_texts()
    assert len(texts) > 30  # the walk really reached the sub-commands
    hits = [name for name, text in texts if OLD_NAME.search(text)]
    assert not hits, f"the help of these commands says 'job version': {hits}"


def test_only_the_explaining_paragraph_of_the_model_docstring_is_exempt() -> None:
    doc = f"Summary.\n\n    {EXPLANATION_START} The table is job_versions."
    assert not string_hits(without_explanation(doc), set())
    assert string_hits(without_explanation("Summary.\n\n    job version again."), set())
    assert string_hits(without_explanation(doc + "\n\n    More job version prose."), set())


def test_type_lines_are_exempt_but_descriptions_are_not() -> None:
    doc = (
        "Do it.\n\n    Returns\n    -------\n    JobVersion or None\n"
        "        The job version, if any.\n"
    )
    assert "JobVersion" not in without_type_lines(doc)
    assert string_hits(without_type_lines(doc), set())
    assert string_hits(without_type_lines("JobVersion is here.\n"), set())


def test_the_developer_pages_keep_only_their_code_names() -> None:
    page = "docs/agent/security.md"
    assert agent_doc_hits(page, "see `Run.job_version_id`") == []
    assert agent_doc_hits(page, "a job version") != []
    assert agent_doc_hits(page, "the `job_version_id` field") != []
    assert agent_doc_hits("docs/agent/testing.md", "`Run.job_version_id`") != []


def test_the_model_docstring_explains_the_name() -> None:
    tree = ast.parse((ROOT / MODEL_FILE).read_text(encoding="utf-8"))
    node = _model_docstring_node(tree)
    assert node is not None
    assert "project version" in str(node.value)


@pytest.mark.parametrize(
    "text",
    [
        "only runs of the deployed JobVersion may write",
        "Run REPO's deployed job version",
        "lakana job-version show",
        "the job_version of a run",
        "/job-versions/{id}",
        "the JobVersions of a repository",
    ],
)
def test_prose_is_a_hit(text: str) -> None:
    assert string_hits(text, {"job_version_id", "JobVersionView"})


@pytest.mark.parametrize(
    "text",
    [
        "job_versions",
        "job_versions.id",
        "JobVersion",
        "job_version_id",
        "`job_version_id` of the view",
        "see JobVersionView",
    ],
)
def test_identifiers_and_the_table_name_are_not_hits(text: str) -> None:
    assert not string_hits(text, {"job_version_id", "JobVersionView"})


def test_the_old_name_is_exempt_only_between_the_markers_of_the_upgrade_notes() -> None:
    text = f"job version\n{OPEN_MARK}\njob-version show\n{CLOSE_MARK}\nJobVersion\n"
    assert doc_hits(UPGRADE_NOTES, text) == [
        f"{UPGRADE_NOTES}:1: job version",
        f"{UPGRADE_NOTES}:5: JobVersion",
    ]
    assert len(doc_hits("docs/site/how-to/projects.md", text)) == 3
    assert doc_hits("docs/site/how-to/projects.md", f"{OPEN_MARK}\njob version\n") != []
