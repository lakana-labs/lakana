"""Tests for the local test selection by package graph."""

from __future__ import annotations

import io
import re
import subprocess
import time
from pathlib import Path

import pytest

from scripts.select_tests import EVERYTHING, Selection, commands, main, select

ROOT = Path(__file__).resolve().parents[2]


def _files_importing(pattern: str, directory: str) -> list[str]:
    """List the test files under a directory whose text matches a pattern.

    Parameters
    ----------
    pattern
        A regular expression over the file's text.
    directory
        A directory relative to the root.

    Returns
    -------
    list of str
        Root-relative paths of the matching ``test_*.py`` files.
    """
    return sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / directory).rglob("test_*.py")
        if re.search(pattern, path.read_text(encoding="utf-8"))
    )


def _selected(selection: Selection, test_file: str) -> bool:
    """Report whether a selection runs a test file.

    Parameters
    ----------
    selection
        The selection.
    test_file
        A root-relative test file.

    Returns
    -------
    bool
        True when the file or a directory above it is selected, or everything is.
    """
    if selection.tests is EVERYTHING:
        return True
    return any(test_file == t or test_file.startswith(t + "/") for t in selection.tests)


def test_an_sdk_change_selects_every_test_that_imports_the_sdk() -> None:
    """The review's cases: the server's CLI and SDK tests and the worker tests of the runner."""
    start = time.monotonic()
    selection = select(["packages/lakana/src/lakana/cli/main.py"])
    # It parses every test file's imports on each call: about 1.1 s, 1.5 s under coverage. The
    # bound leaves room for a busy CI runner and still catches a selection that got slow.
    assert time.monotonic() - start < 4
    assert _selected(selection, "packages/lakana/tests/test_client.py")
    imports = r"(?m)^\s*(from|import) lakana\.{}"
    cli = _files_importing(imports.format("cli"), "packages/lakana-server/tests/cli")
    runner = _files_importing(imports.format("runner"), "packages/lakana-worker/tests")
    assert cli
    assert runner
    for test_file in [*cli, *runner, "packages/lakana-server/tests/sdk/test_sdk_admin.py"]:
        assert _selected(selection, test_file), test_file
    assert not _selected(selection, "packages/lakana-core/tests/test_migrations.py")


def test_a_worker_change_selects_the_server_s_sdk_test_that_imports_the_worker() -> None:
    """``test_sdk_admin.py`` imports ``lakana_worker.pipeline``."""
    selection = select(["packages/lakana-worker/src/lakana_worker/pipeline.py"])
    assert _selected(selection, "packages/lakana-worker/tests/test_tasks.py")
    assert _selected(selection, "packages/lakana-server/tests/sdk/test_sdk_admin.py")
    assert not _selected(selection, "packages/lakana/tests/test_client.py")


def test_a_server_change_selects_the_scripts_tests_that_import_the_server() -> None:
    """The reference generator and the user-text check import ``lakana_server``."""
    selection = select(["packages/lakana-server/src/lakana_server/app.py"])
    for test_file in [
        "packages/lakana-server/tests/test_main_logging.py",
        "scripts/tests/test_generate_reference.py",
        "scripts/tests/test_no_job_version_in_user_text.py",
    ]:
        assert _selected(selection, test_file), test_file
    assert not _selected(selection, "packages/lakana-worker/tests/test_tasks.py")


def _tree(root: Path, files: dict[str, str]) -> Path:
    """Write a small repository.

    Parameters
    ----------
    root
        Where to write it.
    files
        Relative path to content.

    Returns
    -------
    Path
        `root`.
    """
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def test_imports_count_through_conftest_files_helpers_and_relative_imports(
    tmp_path: Path,
) -> None:
    """A test depends on what its conftest chain and its local helpers import, too."""
    root = _tree(
        tmp_path,
        {
            "packages/lakana-worker/tests/conftest.py": "",
            "packages/lakana-worker/tests/helpers.py": "from lakana.runner import run\n",
            "packages/lakana-worker/tests/pkg/__init__.py": "",
            "packages/lakana-worker/tests/pkg/near.py": "import lakana_server.app\n",
            "packages/lakana-worker/tests/test_a.py": "from helpers import x\n",
            "packages/lakana-worker/tests/test_b.py": "import os\n",
            "packages/lakana-worker/tests/pkg/test_c.py": "from .near import y\n",
            "packages/lakana-worker/tests/test_d.py": "from tests.helpers import z\n",
            "packages/lakana-core/tests/sub/conftest.py": "import lakana\n",
            "packages/lakana-core/tests/sub/test_e.py": "",
            "packages/lakana-core/tests/test_f.py": "import lakana_core\n",
            "packages/lakana-core/tests/test_broken.py": "this is not python(\n",
            "packages/lakana-worker/tests/pkg/test_g.py": "from ..helpers import h\n",
            "packages/lakana-worker/tests/broken_helper.py": "def (\n",
            "packages/lakana-worker/tests/test_h.py": "import os\nfrom broken_helper import b\n",
        },
    )
    sdk = select(["packages/lakana/src/lakana/runner.py"], root=root).tests
    # A file that cannot be parsed is a doubt, and a doubt runs it.
    assert sdk == [
        "packages/lakana-core/tests/sub/test_e.py",
        "packages/lakana-core/tests/test_broken.py",
        "packages/lakana-worker/tests/pkg/test_g.py",
        "packages/lakana-worker/tests/test_a.py",
        "packages/lakana-worker/tests/test_d.py",
        "packages/lakana-worker/tests/test_h.py",
        "packages/lakana/tests",
    ]
    server = select(["packages/lakana-server/src/lakana_server/app.py"], root=root).tests
    assert server == [
        "packages/lakana-core/tests/test_broken.py",
        "packages/lakana-server/tests",
        "packages/lakana-worker/tests/pkg/test_c.py",
        "packages/lakana-worker/tests/test_h.py",
    ]


def test_two_changed_packages_select_the_union_without_repeats(tmp_path: Path) -> None:
    """A file inside a selected test directory is not listed again."""
    root = _tree(
        tmp_path,
        {
            "packages/lakana-server/tests/test_s.py": "import lakana\n",
            "packages/lakana-worker/tests/test_w.py": "import lakana_server\n",
        },
    )
    tests = select(
        ["packages/lakana/src/lakana/x.py", "packages/lakana-server/src/lakana_server/y.py"],
        root=root,
    ).tests
    assert tests == [
        "packages/lakana-server/tests",
        "packages/lakana-worker/tests/test_w.py",
        "packages/lakana/tests",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "packages/lakana-core/src/lakana_core/config.py",
        "packages/conftest.py",
        "scripts/pytest_selection.py",
        "scripts/test_support/egress_helpers.py",
        "pyproject.toml",
        "uv.lock",
        "packages/lakana/pyproject.toml",
        ".github/workflows/ci.yml",
        "Dockerfile",
        "docs/examples/getting-started/run.sh",
    ],
)
def test_core_shared_fixtures_the_lock_and_anything_unknown_select_everything(path: str) -> None:
    """Everything depends on core and the shared files; a doubt runs everything."""
    assert select(["packages/lakana/src/lakana/x.py", path]).tests == EVERYTHING


def test_documentation_only_selects_the_documentation_checks() -> None:
    """No package tests, only the checks that read the documentation."""
    assert select(["docs/site/index.md", "mkdocs.yml"]) == Selection(tests=[], docs=True)


def test_documentation_beside_code_adds_the_documentation_checks(tmp_path: Path) -> None:
    """A page changed with the code it describes is checked too."""
    selection = select(
        ["docs/site/index.md", "packages/lakana-worker/src/lakana_worker/x.py"], root=tmp_path
    )
    assert selection == Selection(tests=["packages/lakana-worker/tests"], docs=True)


def test_no_change_selects_only_the_static_checks() -> None:
    """An empty branch still gets the static checks."""
    assert select([]) == Selection(tests=[], docs=False)


def test_the_commands_start_with_the_static_checks_and_never_claim_a_ratchet() -> None:
    """The static checks always run; coverage and its ratchet are CI's."""
    lines = commands(
        Selection(tests=["packages/lakana/tests", "scripts/tests/test_x.py"], docs=False)
    )
    assert lines[0] == "uv run ruff check . && uv run ruff format --check ."
    assert lines[1] == "uv run ty check packages && uv run lint-imports"
    assert lines[2] == (
        "uv run pytest -n 2 --dist loadfile -q -p no:cacheprovider "
        "packages/lakana/tests scripts/tests/test_x.py"
    )
    assert not any("--cov" in line or "coverage_ratchet" in line for line in lines)


def test_everything_runs_the_whole_unit_suite() -> None:
    """No paths: the whole suite, as ``testpaths`` names it."""
    assert commands(Selection(tests=EVERYTHING, docs=False))[2] == (
        "uv run pytest -n 2 --dist loadfile -q -p no:cacheprovider"
    )


def test_the_documentation_checks_are_listed() -> None:
    """The block check, the references, the strict build and the tests that read the pages."""
    lines = commands(Selection(tests=[], docs=True))
    assert "uv run python scripts/docs_blocks.py" in lines
    assert "uv run python scripts/generate_reference.py --check" in lines
    assert "uv run --group docs zensical build --strict" in lines
    assert "uv run pytest scripts/tests -q -p no:cacheprovider" in lines


def _fake_select(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Replace the selection with a quick stand-in, so `main` is tested alone.

    Parameters
    ----------
    monkeypatch
        Replaces `select`.

    Returns
    -------
    list
        The paths of each call, filled as `main` calls it.
    """
    asked: list[list[str]] = []

    def fake(paths: list[str]) -> Selection:
        asked.append(list(paths))
        return Selection(tests=["packages/lakana-worker/tests"], docs=False)

    monkeypatch.setattr("scripts.select_tests.select", fake)
    return asked


def test_main_reads_the_changed_files_from_standard_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``-`` reads one path per line."""
    asked = _fake_select(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO("packages/lakana-worker/src/x.py\n\n"))
    assert main(["-"]) == 0
    assert asked == [["packages/lakana-worker/src/x.py"]]
    out = capsys.readouterr().out
    assert "packages/lakana-worker/tests" in out
    assert "coverage ratchet" in out  # the reminder that CI runs it


def test_main_asks_git_for_the_changes_against_the_base(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``git diff --name-only BASE...HEAD``, base ``origin/main`` unless given."""
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="packages/lakana/src/x.py\n")

    monkeypatch.setattr("scripts.select_tests.subprocess.run", fake_run)
    asked = _fake_select(monkeypatch)
    assert main([]) == 0
    assert calls == [["git", "diff", "--name-only", "origin/main...HEAD"]]
    assert asked == [["packages/lakana/src/x.py"]]
    assert "packages/lakana-worker/tests" in capsys.readouterr().out
    assert main(["--base", "main"]) == 0
    assert calls[-1] == ["git", "diff", "--name-only", "main...HEAD"]


def test_main_names_a_failing_git_and_the_way_around_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing base (no fetch yet) is a message with the next step, not a traceback."""

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(128, args, stderr="unknown revision")

    monkeypatch.setattr("scripts.select_tests.subprocess.run", fake_run)
    assert main([]) == 1
    err = capsys.readouterr().err
    assert "git fetch origin" in err
    assert "unknown revision" in err
