"""Print the local checks a branch needs before a push, selected by the package graph.

The static checks always run. The tests are the changed packages' own tests and every test file
that imports a changed package (``lakana``, ``lakana_server``, ``lakana_worker``), directly, through
its ``conftest.py`` chain, or through a local helper module it imports (``tests.helpers``,
``from .near import``, a sibling ``helpers.py``). Imports are read with `ast` on every call, nothing
is cached. ``lakana-core``, the shared fixtures (``packages/conftest.py``, everything under
``scripts/``), ``pyproject.toml``, ``uv.lock`` and any file not named here select everything; a
test file that cannot be parsed is always selected. A change to documentation alone selects the
documentation checks. The coverage ratchet and Python 3.11, 3.13 and macOS are CI's, never printed
here.

    uv run python -m scripts.select_tests                 # git diff --name-only origin/main...HEAD
    git diff --name-only | uv run python -m scripts.select_tests -

See docs/agent/testing.md, "Local gates".
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.docs_only import is_docs_only

EVERYTHING: list[str] | None = None
"""The selection that runs the whole unit suite."""

ROOT = Path(__file__).resolve().parents[1]

PACKAGES = {
    "packages/lakana/": ("lakana", "packages/lakana/tests"),
    "packages/lakana-server/": ("lakana_server", "packages/lakana-server/tests"),
    "packages/lakana-worker/": ("lakana_worker", "packages/lakana-worker/tests"),
}
"""A package's directory to its import name and its own tests. ``lakana-core`` is not here: every
package depends on it, so a change to it selects everything."""

IMPORT_NAMES = {"lakana", "lakana_core", "lakana_server", "lakana_worker"}

PACKAGE_WIDE = ("pyproject.toml",)
"""Files inside a package directory that select everything (its dependencies)."""

TEST_TREES = ("packages/*/tests", "scripts/tests")

STATIC = [
    "uv run ruff check . && uv run ruff format --check .",
    "uv run ty check packages && uv run lint-imports",
]
DOCS = [
    "uv run python scripts/docs_blocks.py",
    "uv run python scripts/generate_reference.py --check",
    "uv run --group docs zensical build --strict",
    "uv run pytest scripts/tests -q -p no:cacheprovider",
]
PYTEST = "uv run pytest -n 2 --dist loadfile -q -p no:cacheprovider"


@dataclass(frozen=True)
class Selection:
    """What a change needs locally besides the static checks.

    Attributes
    ----------
    tests
        Test directories to run, or `EVERYTHING` (None) for the whole unit suite.
    docs
        Whether the documentation checks run.
    """

    tests: list[str] | None
    docs: bool


def _package_of(path: str) -> tuple[str, str] | None:
    """Find the package one changed code file belongs to.

    Parameters
    ----------
    path
        A changed file that is not documentation.

    Returns
    -------
    tuple of str or None
        The package's import name and its own tests, or None when the file selects everything.
    """
    for prefix, package in PACKAGES.items():
        if path.startswith(prefix) and not path.endswith(PACKAGE_WIDE):
            return package
    return None


def _module_file(names: Sequence[str], start: Path, root: Path) -> Path | None:
    """Find the local file of a dotted module, looking upwards from a directory.

    Parameters
    ----------
    names
        The module's dotted name, split.
    start
        The directory to look from first; then each directory above it, up to `root`.
    root
        The repository root.

    Returns
    -------
    Path or None
        ``a/b.py`` or ``a/b/__init__.py`` under the nearest directory that has it.
    """
    directory = start
    while True:
        base = directory.joinpath(*names)
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file():
                return candidate
        if directory == root or root not in directory.parents:
            return None
        directory = directory.parent


def _imports(tree: ast.Module) -> Iterator[tuple[int, list[str], list[str]]]:
    """List the imports of a module.

    Parameters
    ----------
    tree
        The parsed module.

    Yields
    ------
    tuple
        The relative level, the module's dotted name split, and the names imported from it.
    """
    # Only statements can import, so expressions (most of a test file) are never visited.
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            pending.extend(getattr(node, field, ()))
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield 0, alias.name.split("."), []
        elif isinstance(node, ast.ImportFrom):
            module = node.module.split(".") if node.module else []
            yield node.level, module, [alias.name for alias in node.names]


def _imported_packages(
    path: Path, root: Path, known: dict[Path, set[str] | None], local_names: set[str]
) -> set[str] | None:
    """Find the Lakana packages a file imports, following local modules it imports.

    Parameters
    ----------
    path
        A Python file.
    root
        The repository root.
    known
        The answers of this selection so far, by file; a file being read counts as importing
        nothing, so a cycle ends. Kept for one call of `select` only.
    local_names
        The names of the repository's files and directories that a module could be found as;
        an import whose first name is not among them is not followed.

    Returns
    -------
    set of str or None
        The import names, or None when a file on the way cannot be parsed.
    """
    if path in known:
        return known[path]
    known[path] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        known[path] = None
        return None
    found: set[str] | None = set()
    for level, module, names in _imports(tree):
        if level == 0 and module and module[0] in IMPORT_NAMES:
            found.add(module[0])
            continue
        start = path.parent
        for _ in range(max(level - 1, 0)):
            start = start.parent
        if level == 0 and (module or names)[0] not in local_names:
            continue  # the standard library or a dependency: no file of ours has that name
        for target in [[*module, name] for name in names] + ([module] if module else []):
            local = _local_module(target, start, root, relative=level > 0)
            more = (
                _imported_packages(local, root, known, local_names) if local is not None else set()
            )
            if more is None:
                found = None
                break
            found |= more
        if found is None:
            break
    known[path] = found
    return found


def _local_module(names: Sequence[str], start: Path, root: Path, *, relative: bool) -> Path | None:
    """Find the file of a module imported by a test file, if it is one of the repository's.

    Parameters
    ----------
    names
        The module's dotted name, split.
    start
        The importing file's directory, or the package a relative import starts from.
    root
        The repository root.
    relative
        Whether the import is relative: then only `start` is looked in.

    Returns
    -------
    Path or None
        The module's file, or None for a module from elsewhere (the standard library, a
        dependency).
    """
    if not relative:
        return _module_file(names, start, root)
    base = start.joinpath(*names)
    return next((c for c in (base.with_suffix(".py"), base / "__init__.py") if c.is_file()), None)


def _conftests(path: Path, root: Path) -> list[Path]:
    """List the ``conftest.py`` files that apply to a test file.

    Parameters
    ----------
    path
        A test file.
    root
        The repository root.

    Returns
    -------
    list of Path
        The existing ``conftest.py`` of its directory and of every directory above, up to `root`.
    """
    return [
        directory / "conftest.py"
        for directory in [path.parent, *path.parent.parents]
        if (directory == root or root in directory.parents)
        and (directory / "conftest.py").is_file()
    ]


def _test_files(root: Path) -> list[Path]:
    """List every test file of the repository.

    Parameters
    ----------
    root
        The repository root.

    Returns
    -------
    list of Path
        The ``test_*.py`` files under the test trees, sorted.
    """
    return sorted(
        path for tree in TEST_TREES for base in root.glob(tree) for path in base.rglob("test_*.py")
    )


def select(paths: Sequence[str], root: Path = ROOT) -> Selection:
    """Select the local checks for a set of changed files.

    Parameters
    ----------
    paths
        The changed files, relative to the repository root.
    root
        The repository root.

    Returns
    -------
    Selection
        The tests and whether the documentation checks run.
    """
    docs = any(is_docs_only([path]) for path in paths)
    changed: set[str] = set()
    chosen: set[str] = set()
    for path in paths:
        if is_docs_only([path]):
            continue
        package = _package_of(path)
        if package is None:
            return Selection(tests=EVERYTHING, docs=docs)
        changed.add(package[0])
        chosen.add(package[1])
    if changed:
        known: dict[Path, set[str] | None] = {}
        test_files = _test_files(root)
        local_names = {entry.stem for entry in root.iterdir()} | {
            entry.stem
            for tree in TEST_TREES
            for base in root.glob(tree)
            for entry in [base, *base.rglob("*")]
        }
        for test_file in test_files:
            imported: set[str] | None = set()
            for source in [test_file, *_conftests(test_file, root)]:
                more = _imported_packages(source, root, known, local_names)
                if more is None:
                    imported = None
                    break
                imported |= more
            if imported is None or imported & changed:
                chosen.add(test_file.relative_to(root).as_posix())
    # A file inside a selected test directory adds nothing.
    kept = [t for t in chosen if not any(t.startswith(o + "/") for o in chosen)]
    return Selection(tests=sorted(kept), docs=docs)


def commands(selection: Selection) -> list[str]:
    """Spell out a selection as the commands to run, the static checks first.

    Parameters
    ----------
    selection
        The selection.

    Returns
    -------
    list of str
        One command per line.
    """
    lines = list(STATIC)
    if selection.tests is EVERYTHING:
        lines.append(PYTEST)
    elif selection.tests:
        lines.append(f"{PYTEST} {' '.join(selection.tests)}")
    if selection.docs:
        lines.extend(DOCS)
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Print the commands for the changes of this branch.

    Parameters
    ----------
    argv
        The arguments; None reads ``sys.argv``.

    Returns
    -------
    int
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", nargs="?", help="'-' reads the changed files from stdin")
    parser.add_argument("--base", default="origin/main", help="compare with BASE...HEAD")
    args = parser.parse_args(argv)
    if args.source == "-":
        text = sys.stdin.read()
    else:
        try:
            text = subprocess.run(
                ["git", "diff", "--name-only", f"{args.base}...HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except subprocess.CalledProcessError as error:
            print(
                f"git diff against {args.base} failed ({(error.stderr or '').strip()}). "
                f"Run `git fetch origin` first, or pass --base, or pipe the files in with `-`.",
                file=sys.stderr,
            )
            return 1
    paths = [line.strip() for line in text.splitlines() if line.strip()]
    for line in commands(select(paths)):
        print(line)
    print(
        "# The coverage ratchet and Python 3.11, 3.13 and macOS run in CI only; a local "
        "pytest is not a ratchet result."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
