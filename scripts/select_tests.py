"""Print the local checks a branch needs before a push, selected by the package graph.

The static checks always run. The tests are those of the packages the change touches and of the
packages that depend on them: the SDK (``packages/lakana``) selects its own tests and the server's
SDK tests, the server and the worker their own tests; ``lakana-core``, the shared fixtures
(``packages/conftest.py``, everything under ``scripts/``), ``pyproject.toml``, ``uv.lock`` and any
file not named here select everything. A change to documentation alone selects the documentation
checks. The coverage ratchet and Python 3.11, 3.13 and macOS are CI's, never printed here.

    uv run python -m scripts.select_tests                 # git diff --name-only origin/main...HEAD
    git diff --name-only | uv run python -m scripts.select_tests -

See docs/agent/testing.md, "Local gates".
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from scripts.docs_only import is_docs_only

EVERYTHING: list[str] | None = None
"""The selection that runs the whole unit suite."""

PACKAGE_TESTS = {
    "packages/lakana/": ["packages/lakana/tests", "packages/lakana-server/tests/sdk"],
    "packages/lakana-server/": ["packages/lakana-server/tests"],
    "packages/lakana-worker/": ["packages/lakana-worker/tests"],
}
"""A package's directory to the tests it selects: its own and its dependents'."""

PACKAGE_WIDE = ("pyproject.toml",)
"""Files inside a package directory that select everything (its dependencies)."""

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


def _tests_of(path: str) -> list[str] | None:
    """Find the tests one changed code file selects.

    Parameters
    ----------
    path
        A changed file that is not documentation.

    Returns
    -------
    list of str or None
        The package's test directories, or None (everything) for anything else.
    """
    for prefix, tests in PACKAGE_TESTS.items():
        if path.startswith(prefix) and not path.endswith(PACKAGE_WIDE):
            return tests
    return EVERYTHING


def select(paths: Sequence[str]) -> Selection:
    """Select the local checks for a set of changed files.

    Parameters
    ----------
    paths
        The changed files, relative to the repository root.

    Returns
    -------
    Selection
        The tests and whether the documentation checks run.
    """
    docs = any(is_docs_only([path]) for path in paths)
    chosen: list[str] = []
    for path in paths:
        if is_docs_only([path]):
            continue
        tests = _tests_of(path)
        if tests is EVERYTHING:
            return Selection(tests=EVERYTHING, docs=docs)
        chosen.extend(t for t in tests if t not in chosen)
    # A directory inside another selected one adds nothing.
    kept = [t for t in chosen if not any(t != o and t.startswith(o + "/") for o in chosen)]
    return Selection(tests=kept, docs=docs)


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
