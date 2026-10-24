"""Tests for the local test selection by package graph."""

from __future__ import annotations

import io
import subprocess

import pytest

from scripts.select_tests import EVERYTHING, Selection, commands, main, select

SDK = ["packages/lakana/tests", "packages/lakana-server/tests/sdk"]


@pytest.mark.parametrize(
    ("paths", "tests"),
    [
        (["packages/lakana/src/lakana/client.py"], SDK),
        (["packages/lakana/tests/test_client.py"], SDK),
        (["packages/lakana-server/src/lakana_server/app.py"], ["packages/lakana-server/tests"]),
        (["packages/lakana-worker/src/lakana_worker/tasks.py"], ["packages/lakana-worker/tests"]),
        (
            ["packages/lakana/src/lakana/x.py", "packages/lakana-worker/src/lakana_worker/y.py"],
            [*SDK, "packages/lakana-worker/tests"],
        ),
        (
            ["packages/lakana/src/lakana/x.py", "packages/lakana-server/src/lakana_server/y.py"],
            ["packages/lakana/tests", "packages/lakana-server/tests"],
        ),
    ],
)
def test_a_package_change_selects_its_tests_and_its_dependents(
    paths: list[str], tests: list[str]
) -> None:
    """SDK → its tests and the server's SDK tests; server or worker → their own tests."""
    assert select(paths) == Selection(tests=tests, docs=False)


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


def test_documentation_beside_code_adds_the_documentation_checks() -> None:
    """A page changed with the code it describes is checked too."""
    selection = select(["docs/site/index.md", "packages/lakana-worker/src/lakana_worker/x.py"])
    assert selection == Selection(tests=["packages/lakana-worker/tests"], docs=True)


def test_no_change_selects_only_the_static_checks() -> None:
    """An empty branch still gets the static checks."""
    assert select([]) == Selection(tests=[], docs=False)


def test_the_commands_start_with_the_static_checks_and_never_claim_a_ratchet() -> None:
    """The static checks always run; coverage and its ratchet are CI's."""
    lines = commands(Selection(tests=SDK, docs=False))
    assert lines[0] == "uv run ruff check . && uv run ruff format --check ."
    assert lines[1] == "uv run ty check packages && uv run lint-imports"
    assert lines[2] == (
        "uv run pytest -n 2 --dist loadfile -q -p no:cacheprovider "
        "packages/lakana/tests packages/lakana-server/tests/sdk"
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


def test_main_reads_the_changed_files_from_standard_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``-`` reads one path per line."""
    monkeypatch.setattr("sys.stdin", io.StringIO("packages/lakana-worker/src/x.py\n\n"))
    assert main(["-"]) == 0
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
    assert main([]) == 0
    assert calls == [["git", "diff", "--name-only", "origin/main...HEAD"]]
    assert "packages/lakana-server/tests/sdk" in capsys.readouterr().out
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
