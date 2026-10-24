"""Tests for the docs-only check and for the workflows that depend on it."""

import io
from pathlib import Path

import pytest
import yaml

from scripts.docs_only import MAX_FILES, is_docs_only, main

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

SKIPPED_JOBS = {
    "ci.yml": (
        "lint",
        "unit",
        "integration",
        "integration-services",
        "integration-docker",
        "integration-assets",
        "five-minute",
        "compose-smoke",
        "docs-examples",
        "unit-macos",
        "test-macos",
        "quarantine",
    ),
    "security.yml": ("static",),
}
ALWAYS_RUN = {
    "ci.yml": ("changes", "test", "docs", "budgets"),
    "security.yml": ("changes", "secrets"),
}
HEAVY_ONLY = "!cancelled() && needs.changes.outputs.docs_only != 'true'"


@pytest.mark.parametrize(
    "paths",
    [
        ["docs/scope.md"],
        ["docs/roadmap.md", "README.md", "CHANGELOG.md"],
        ["docs/assets/logo.svg"],
        ["docs/site/tutorials/getting-started.md"],
        [".github/CONTRIBUTING.md", ".github/pull_request_template.md"],
        ["packages/lakana/README.md"],
        ["mkdocs.yml"],
        ["mkdocs.yml", "docs/site/how-to/new-page.md"],
        ["docs/agent/architecture.md"],
    ],
)
def test_documentation_changes_are_docs_only(paths: list[str]) -> None:
    """Files under ``docs/``, Markdown outside package sources and the root ``mkdocs.yml`` count."""
    assert is_docs_only(paths) is True


@pytest.mark.parametrize(
    "paths",
    [
        ["packages/lakana-core/src/lakana_core/config.py"],
        ["docs/scope.md", "pyproject.toml"],
        ["docs/scope.md", "uv.lock"],
        [".github/workflows/ci.yml"],
        ["scripts/docs_only.py"],
        ["scripts/tests/test_docs_only.py"],
        ["Dockerfile"],
        ["LICENSE"],
        ["NOTICE"],
        ["packages/lakana/src/lakana/cli/templates/README.md"],
        ["packages/lakana/src/lakana/cli/deploy/compose.yaml"],
        ["docs/../pyproject.toml"],
        ["/docs/scope.md"],
        ["docs"],
        ["docsx/logo.svg"],
        ["README.md.py"],
        [""],
        ["docs/examples/getting-started/run.sh"],
        ["docs/examples/getting-started/project/assets/bronze/readings.sql"],
        ["docs/examples/README.md"],
        ["docs/site/index.md", "docs/examples/getting-started/README.md"],
        ["packages/lakana/mkdocs.yml"],
        ["site/mkdocs.yml"],
        ["mkdocs.yaml"],
        ["mkdocs.yml.bak"],
        ["./mkdocs.yml"],
        ["mkdocs.yml", "pyproject.toml"],
        ["MKDOCS.YML"],
        ["sub/mkdocs.yml"],
        ["mkdocs.yml", "docs/examples/getting-started/run.sh"],
        ["mkdocs.yml", "docs/examples/README.md"],
    ],
)
def test_anything_else_runs_the_whole_pipeline(paths: list[str]) -> None:
    """One file that is not documentation is enough to run everything."""
    assert is_docs_only(paths) is False


def test_no_changed_files_runs_the_whole_pipeline() -> None:
    """An empty list is not evidence of a documentation change."""
    assert is_docs_only([]) is False


def test_a_listing_at_the_api_limit_runs_the_whole_pipeline() -> None:
    """The pull-request files API stops at a limit; a full listing may be cut short."""
    assert is_docs_only(["docs/a.md"] * (MAX_FILES - 1)) is True
    assert is_docs_only(["docs/a.md"] * MAX_FILES) is False


def test_main_prints_the_verdict_for_the_paths_on_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workflow pipes one path per line and reads ``true`` or ``false``."""
    monkeypatch.setattr("sys.stdin", io.StringIO("docs/scope.md\nREADME.md\n"))
    assert main() == 0
    assert capsys.readouterr().out == "true\n"

    monkeypatch.setattr("sys.stdin", io.StringIO("docs/scope.md\nuv.lock\n"))
    assert main() == 0
    assert capsys.readouterr().out == "false\n"


def _jobs(workflow: str) -> dict:
    """Load the jobs of a workflow file.

    Parameters
    ----------
    workflow
        File name under ``.github/workflows``.

    Returns
    -------
    dict
        The workflow's ``jobs`` mapping.
    """
    return yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"]


@pytest.mark.parametrize("workflow", sorted(SKIPPED_JOBS))
def test_the_changes_job_cannot_skip_the_pipeline_by_failing(workflow: str) -> None:
    """Every error path of the detection step says "not docs-only"."""
    job = _jobs(workflow)["changes"]
    assert job["outputs"]["docs_only"] == "${{ steps.scope.outputs.docs_only }}"
    step = next(s for s in job["steps"] if s.get("id") == "scope")
    script = step["run"]
    assert 'verdict="false"' in script.splitlines()[0]
    assert 'echo "docs_only=${verdict}" >> "$GITHUB_OUTPUT"' in script
    assert "set -e" not in script
    assert step["shell"] == "bash --noprofile --norc {0}"
    # The classifier is fetched from the base commit, so a pull request cannot change the
    # rule it is judged by.
    assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert "contents/scripts/docs_only.py?ref=${BASE_SHA}" in script
    assert 'python3 "$RUNNER_TEMP/docs_only.py"' in script
    # No checkout: a sparse checkout would stay configured in the workspace a
    # self-hosted runner reuses, and the next job would find one file there.
    assert all("uses" not in s for s in job["steps"])


@pytest.mark.parametrize(
    ("workflow", "name"),
    [(w, n) for w, names in SKIPPED_JOBS.items() for n in names],
)
def test_heavy_jobs_are_skipped_only_for_a_docs_only_change(workflow: str, name: str) -> None:
    """A heavy job runs unless the detection said "docs only", also when detection failed."""
    job = _jobs(workflow)[name]
    # A job that collects others (`integration`, `test (macos)`) waits for `changes` first.
    needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
    assert needs[0] == "changes"
    if workflow == "ci.yml":
        # ci.yml's jobs add conditions of their own (a draft, a commit tested already); each is
        # a further `&&` term, and none replaces the docs-only one. The terms are pinned in
        # test_workflows.py.
        terms = job["if"].removeprefix("${{ ").removesuffix(" }}").split(" && ")
        assert terms[:2] == HEAVY_ONLY.split(" && ")
    else:
        # Elsewhere a required check (`static`) is among these jobs: a skipped required check
        # counts as passing, so the condition is pinned exactly.
        assert job["if"] == f"${{{{ {HEAVY_ONLY} }}}}"


def test_the_test_job_always_reports_and_runs_the_docs_tests() -> None:
    """``test (3.11)`` and ``test (3.14)`` are required: the job never skips for a pull request."""
    job = _jobs("ci.yml")["test"]
    assert job["needs"] == ["changes", "unit"]
    # The only condition is the one that skips a push to main already tested (change in
    # test_workflows.py); never one that depends on the pull request's content.
    assert "docs_only" not in job.get("if", "")
    steps = {s["name"]: s for s in job["steps"] if "name" in s}
    docs_only = "needs.changes.outputs.docs_only == 'true'"
    assert steps["Documentation tests"]["if"] == docs_only
    assert steps["Documentation tests"]["run"] == "uv run pytest scripts/tests"
    assert steps["Unit test shards"]["if"] == "needs.changes.outputs.docs_only != 'true'"
    assert steps["Coverage ratchet"]["if"] == (
        "matrix.python-version == '3.14' && needs.changes.outputs.docs_only != 'true'"
    )


@pytest.mark.parametrize(
    ("workflow", "name"),
    [(w, n) for w, names in ALWAYS_RUN.items() for n in names],
)
def test_jobs_that_guard_every_change_have_no_condition(workflow: str, name: str) -> None:
    """The detection, the tests and the secret scan run on every pull request."""
    assert "docs_only" not in _jobs(workflow)[name].get("if", "")
    if name == "budgets":
        # It waits for the other jobs and must still run after one of them failed.
        assert _jobs(workflow)[name]["if"] == "${{ !cancelled() }}"
    elif name != "test":
        assert "if" not in _jobs(workflow)[name]


def test_the_docs_job_checks_the_blocks_and_builds_the_site_strictly() -> None:
    """The docs job guards a documentation-only change, so no step may be able to pass quietly."""
    job = _jobs("ci.yml")["docs"]
    runs = [step.get("run") for step in job["steps"]]
    assert "uv run python scripts/docs_blocks.py" in runs
    assert "uv run --group docs zensical build --strict" in runs
    assert "uv run python scripts/check_built_site.py site-build" in runs
    assert runs.index("uv run --group docs zensical build --strict") < runs.index(
        "uv run python scripts/check_built_site.py site-build"
    )
    uploads = [s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert uploads
    assert uploads[0]["with"]["path"] == "site-build"
    assert uploads[0]["with"]["retention-days"] == 7
    assert "if" not in job
    assert "needs" not in job
    assert all("if" not in step for step in job["steps"])


@pytest.mark.parametrize("name", ["docs", "docs-examples"])
def test_no_step_of_a_documentation_job_can_fail_quietly(name: str) -> None:
    """``continue-on-error`` and ``|| true`` turn a failing check into a green one."""
    job = _jobs("ci.yml")[name]
    assert "continue-on-error" not in job
    for step in job["steps"]:
        assert "continue-on-error" not in step, step
        run = step.get("run", "")
        assert "|| true" not in run and "||true" not in run, run
        assert "set +e" not in run, run


def test_the_examples_job_runs_the_runner() -> None:
    """The example projects run end to end unless the change is documentation only."""
    job = _jobs("ci.yml")["docs-examples"]
    assert any("scripts/run_docs_examples.sh" in step.get("run", "") for step in job["steps"])
    assert job["timeout-minutes"] <= 25


def test_the_examples_job_builds_the_lakana_wheel_before_it_runs_the_examples() -> None:
    """The Python-asset example carries the lakana wheel in its project; without it, it fails."""
    steps = _jobs("ci.yml")["docs-examples"]["steps"]
    build = next(
        i for i, s in enumerate(steps) if "uv build --package lakana --wheel" in s.get("run", "")
    )
    runner = next(
        i for i, s in enumerate(steps) if "scripts/run_docs_examples.sh" in s.get("run", "")
    )
    assert build < runner
    env = steps[runner].get("env", {})
    assert env.get("LAKANA_TEST_WHEELHOUSE"), "the example needs LAKANA_TEST_WHEELHOUSE"
    assert "$RUNNER_TEMP/wheelhouse" in steps[build]["run"]
    assert "runner.temp" in env["LAKANA_TEST_WHEELHOUSE"]
    assert int(env["LAKANA_DOCS_EXAMPLE_BUDGET"]) >= 240


@pytest.mark.parametrize("name", ["docs", "docs-examples"])
def test_the_documentation_jobs_sync_from_the_lock_file(name: str) -> None:
    """A job that syncs without ``--locked`` may test other versions than the lock file says."""
    syncs = [
        step["run"] for step in _jobs("ci.yml")[name]["steps"] if "uv sync" in step.get("run", "")
    ]
    assert syncs
    assert all("--locked" in run for run in syncs)


@pytest.mark.parametrize("workflow", sorted(SKIPPED_JOBS))
def test_every_job_is_accounted_for(workflow: str) -> None:
    """A job added later must be put on one of the two lists on purpose."""
    assert set(_jobs(workflow)) == set(SKIPPED_JOBS[workflow]) | set(ALWAYS_RUN[workflow])
