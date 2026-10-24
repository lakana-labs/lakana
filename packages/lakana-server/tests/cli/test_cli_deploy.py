"""`lakana repo add` → `lakana deploy` → `lakana rollback` against the real app and worker."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import Result

from lakana.cli._state import CliState
from lakana_server.settings import Settings
from tests.cli.conftest import make_cli_state

_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    """Return the shared settings with `tmp_path/repos` as the repo root.

    Parameters
    ----------
    settings
        The conftest settings.
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    Settings
        With `repo_roots` set.
    """
    (tmp_path / "repos").mkdir()
    return settings.model_copy(update={"repo_roots": [str(tmp_path / "repos")]})


def _git(cwd: Path, *args: str) -> str:
    """Run git with a fixed identity.

    Parameters
    ----------
    cwd
        Working directory.
    *args
        Arguments after ``git``.

    Returns
    -------
    str
        Stripped stdout.
    """
    return subprocess.run(
        ["git", "-c", "init.defaultBranch=main", *args],
        cwd=cwd,
        env={**os.environ, **_IDENTITY},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _ok(result: Result) -> Result:
    """Assert success, showing output if not.

    Parameters
    ----------
    result
        A CLI result.

    Returns
    -------
    Result
        The same result.
    """
    assert result.exit_code == 0, result.output
    return result


@pytest.mark.slow
def test_deploy_dry_run_redeploy_and_rollback(
    client: TestClient,
    tmp_path: Path,
    worker: None,
    invoke_cli: Callable[..., Result],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit criteria: local repo, deploy, dry-run diff, redeploy reuse, rollback both ways."""
    state: CliState = make_cli_state(client, tmp_path)
    _ok(
        invoke_cli(
            state, "env", "create", "dev", "--storage-root", f"file://{tmp_path / 'storage'}"
        )
    )
    _ok(
        invoke_cli(state, "table", "create", "bronze.events", "--env", "dev", "--column", "id:long")
    )
    project = tmp_path / "repos" / "analytics"
    project.mkdir()
    (project / "lakana.yaml").write_text("repo: analytics\n")
    (project / "assets" / "silver").mkdir(parents=True)
    (project / "assets" / "silver" / "clean.sql").write_text("SELECT id FROM bronze.events\n")
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "one")
    first = _git(project, "rev-parse", "HEAD")
    monkeypatch.chdir(project)
    _ok(invoke_cli(state, "repo", "add", "analytics", "--kind", "local", "--url", "."))
    dry = _ok(invoke_cli(state, "deploy", "--env", "dev", "--dry-run"))
    assert "silver.clean" in dry.stdout
    assert _ok(invoke_cli(state, "deployments", "--env", "dev")).stdout.count(first) == 0
    _ok(invoke_cli(state, "deploy", "--env", "dev"))
    (project / "assets" / "silver" / "more.sql").write_text("SELECT id FROM bronze.events\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "two")
    second = _git(project, "rev-parse", "HEAD")
    _ok(invoke_cli(state, "deploy", "--env", "dev"))
    redeploy = _ok(invoke_cli(state, "deploy", "--env", "dev"))
    assert "No changes." in redeploy.stdout
    _ok(invoke_cli(state, "rollback", "--env", "dev", "--to-previous"))
    assert first in _ok(invoke_cli(state, "deployments", "--env", "dev")).stdout
    _ok(invoke_cli(state, "rollback", "--env", "dev", "--to", second))
    assert second in _ok(invoke_cli(state, "deployments", "--env", "dev")).stdout
    _ok(invoke_cli(state, "rollback", "--env", "dev", "--to", first))
    orphans = _ok(invoke_cli(state, "--output", "json", "orphans", "--env", "dev")).stdout
    assert '"orphans": []' in orphans  # silver.more was never materialized, so it does not exist


@pytest.mark.slow
def test_init_output_deploys_unchanged(
    client: TestClient,
    tmp_path: Path,
    worker: None,
    invoke_cli: Callable[..., Result],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit criterion: `lakana init` produces a project that deploys unchanged."""
    state = make_cli_state(client, tmp_path)
    _ok(
        invoke_cli(
            state, "env", "create", "dev", "--storage-root", f"file://{tmp_path / 'storage'}"
        )
    )
    project = tmp_path / "repos" / "demo"
    _ok(invoke_cli(state, "init", str(project)))
    _git(project, "init", "-q")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    monkeypatch.chdir(project)
    _ok(invoke_cli(state, "repo", "add", "demo", "--kind", "local", "--url", "."))
    result = _ok(invoke_cli(state, "deploy", "--env", "dev"))
    for table in ("bronze.readings", "silver.station_temperatures", "gold.station_ranking"):
        assert table in result.stdout
    assert "silver.station_temperatures: plausible_range" in result.stdout
