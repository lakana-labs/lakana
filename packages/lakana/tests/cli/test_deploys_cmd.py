"""`lakana deploy`, `lakana rollback` and the deploy read commands."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import httpx
import pytest
from conftest import FakeApi, invoke, make_state, write_profile_file

_ID = "00000000-0000-0000-0000-0000000000aa"
_SHA = "a" * 40


def _deploy(status: str, **overrides: object) -> dict[str, object]:
    """Build a `DeployOut` body.

    Parameters
    ----------
    status
        The run's status.
    **overrides
        Fields to change.

    Returns
    -------
    dict
        The body.
    """
    return {
        "id": _ID,
        "environment": "dev",
        "repository": "analytics",
        "kind": "deploy",
        "status": status,
        "requested_ref": "main",
        "resolved_sha": _SHA,
        "rollback_to_project_version_id": None,
        "error_message": None,
        "diff": {
            "added": ["silver.clean"],
            "removed": [],
            "adopted": [],
            "materialization_changed": [],
            "sql_changed": ["gold.daily"],
            "metadata_changed": [],
        },
        "project_version_id": "00000000-0000-0000-0000-0000000000bb",
        "principal_id": None,
        "created_at": "2026-10-01T12:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        **overrides,
    }


def _plain(text: str) -> str:
    """Return CLI output without ANSI codes or Rich box borders, whitespace collapsed.

    Parameters
    ----------
    text
        Raw stdout or stderr.

    Returns
    -------
    str
        The plain, single-spaced text.
    """
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text)
    for border in "│╭╮╰╯─":
        plain = plain.replace(border, " ")
    return " ".join(plain.split())


_CI = {"GITHUB_ACTIONS": "true"}


def _api(status: str = "succeeded", **overrides: object) -> FakeApi:
    """Build a fake API that accepts a deploy and reports it finished.

    Parameters
    ----------
    status
        The final status.
    **overrides
        Fields of the final run.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    api.on(
        "POST", "/api/v1/environments/dev/deploys", status=202, json={"id": _ID, "status": "queued"}
    )
    api.on("GET", f"/api/v1/environments/dev/deploys/{_ID}", json=_deploy(status, **overrides))
    return api


def test_deploy_waits_and_prints_sha_and_diff(tmp_path: Path) -> None:
    """Success prints the resolved SHA and one row per changed table; exit 0."""
    api = _api()
    result = invoke(
        make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", "--ref", "main", env=_CI
    )
    assert result.exit_code == 0, result.output
    out = _plain(result.stdout)
    assert _SHA in out
    assert "added silver.clean" in out
    assert "sql_changed gold.daily" in out
    assert json.loads(api.requests[0].content) == {
        "repository": "analytics",
        "kind": "deploy",
        "ref": "main",
        "to_sha": None,
        "to_previous": False,
    }


def test_dry_run_sends_dry_run(tmp_path: Path) -> None:
    """`--dry-run` queues a dry run."""
    api = _api(kind="dry_run")
    invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", "--dry-run")
    assert json.loads(api.requests[0].content)["kind"] == "dry_run"


def test_failed_deploy_exits_1_with_the_message(tmp_path: Path) -> None:
    """A failed run (including a lost race) exits 1 and shows the error."""
    api = _api("failed", error_message="assets/x.sql:1: table 'bronze.ghost' not found")
    result = invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 1
    assert "table 'bronze.ghost' not found" in _plain(result.stderr)


def test_no_wait_prints_only_the_id(tmp_path: Path) -> None:
    """`--no-wait` prints the deploy id and never polls."""
    api = _api()
    result = invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", "--no-wait")
    assert result.stdout.strip() == _ID
    assert [r.method for r in api.requests] == ["POST"]


def test_repo_comes_from_lakana_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without REPO, `lakana.yaml`'s `repo:` is used, found from a subdirectory."""
    (tmp_path / "proj" / "assets").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path / "proj", check=True)
    (tmp_path / "proj" / "lakana.yaml").write_text("repo: analytics\n")
    monkeypatch.chdir(tmp_path / "proj" / "assets")
    api = _api()
    assert invoke(make_state(tmp_path, api), "deploy", "--env", "dev").exit_code == 0
    posts = [r for r in api.requests if r.method == "POST"]
    assert json.loads(posts[0].content)["repository"] == "analytics"


def test_missing_repo_is_a_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No REPO and no lakana.yaml exits 2."""
    monkeypatch.chdir(tmp_path)
    result = invoke(make_state(tmp_path, FakeApi()), "deploy", "--env", "dev")
    assert result.exit_code == 2


def test_dirty_local_repository_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dirty work tree of a local repository warns that only committed state deploys."""
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "lakana.yaml").write_text("repo: analytics\n")
    monkeypatch.chdir(project)
    api = _api()
    api.on(
        "GET",
        "/api/v1/repositories/analytics",
        json={
            "id": "x",
            "name": "analytics",
            "kind": "local",
            "url": str(project),
            "credential_secret": None,
            "credential_username": None,
            "ssh_known_hosts": None,
            "default_ref": None,
            "created_by": None,
            "created_at": "2026-10-01T12:00:00+00:00",
            "updated_at": "2026-10-01T12:00:00+00:00",
        },
    )
    result = invoke(make_state(tmp_path, api), "deploy", "--env", "dev", env=_CI)
    assert "only the committed state is deployed" in _plain(result.stderr)


def test_rollback_to_sha_and_to_previous(tmp_path: Path) -> None:
    """Exactly one of --to / --to-previous; each sends the matching body."""
    api = _api(kind="rollback")
    state = make_state(tmp_path, api)
    assert invoke(state, "rollback", "analytics", "--env", "dev", "--to", _SHA).exit_code == 0
    assert json.loads(api.requests[0].content)["to_sha"] == _SHA
    assert invoke(state, "rollback", "analytics", "--env", "dev", "--to-previous").exit_code == 0
    assert json.loads(api.requests[2].content)["to_previous"] is True
    assert invoke(state, "rollback", "analytics", "--env", "dev").exit_code == 2
    assert (
        invoke(
            state, "rollback", "analytics", "--env", "dev", "--to", _SHA, "--to-previous"
        ).exit_code
        == 2
    )


def test_read_commands(tmp_path: Path) -> None:
    """Deploys list/show, deployments, project-version show (with the DAG), orphans."""
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/deploys", json=[_deploy("succeeded")])
    api.on("GET", f"/api/v1/environments/dev/deploys/{_ID}", json=_deploy("succeeded"))
    api.on(
        "GET",
        "/api/v1/environments/dev/deployments",
        json=[
            {
                "repository": "analytics",
                "project_version_id": "jv1",
                "commit_sha": _SHA,
                "updated_at": "2026-10-01T12:00:00+00:00",
            }
        ],
    )
    api.on(
        "GET",
        "/api/v1/environments/dev/project-versions/jv1",
        json={
            "id": "jv1",
            "environment": "dev",
            "repository": "analytics",
            "commit_sha": _SHA,
            "code_uri": "s3://b/dev/_artifacts/code/analytics/x.tar.gz",
            "code_sha256": "0" * 64,
            "deployed_by": None,
            "created_at": "2026-10-01T12:00:00+00:00",
            "manifest": {
                "version": 1,
                "assets": [
                    {
                        "table": "bronze.raw",
                        "materialize": "view",
                        "metadata": {"owner": "o"},
                        "hidden_references": 0,
                    },
                    {
                        "table": "silver.clean",
                        "materialize": "table",
                        "metadata": {"owner": None},
                        "hidden_references": 1,
                    },
                ],
                "edges": [["bronze.raw", "silver.clean"]],
                "external_inputs": [],
                "hidden_external_inputs": 1,
            },
        },
    )
    api.on(
        "GET",
        "/api/v1/environments/dev/orphans",
        json={
            "orphans": [{"repository": "analytics", "table": "silver.old"}],
            "hidden": 2,
        },
    )
    state = make_state(tmp_path, api)
    assert _ID in _plain(invoke(state, "deploys", "list", "--env", "dev", env=_CI).stdout)
    assert "succeeded" in _plain(
        invoke(state, "deploys", "show", _ID, "--env", "dev", env=_CI).stdout
    )
    assert _SHA in _plain(invoke(state, "deployments", "--env", "dev", env=_CI).stdout)
    shown = invoke(state, "project-version", "show", "jv1", "--env", "dev", env=_CI).stdout
    assert "bronze.raw" in shown and "└─ silver.clean" in shown
    orphans = _plain(invoke(state, "orphans", "--env", "dev", env=_CI).stdout)
    assert "silver.old" in orphans and "2 hidden" in orphans


def test_json_output(tmp_path: Path) -> None:
    """`--output json` prints the finished run."""
    api = _api()
    result = invoke(
        make_state(tmp_path, api), "--output", "json", "deploy", "analytics", "--env", "dev"
    )
    assert json.loads(result.stdout)["resolved_sha"] == _SHA


def test_timeout_prints_the_deploy_id_and_exits_1(tmp_path: Path) -> None:
    """`--timeout` that elapses is a clear message with the id, no traceback."""
    api = _api("queued")
    result = invoke(
        make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", "--timeout", "0", env=_CI
    )
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    err = _plain(result.stderr)
    assert _ID in err and "still queued" in err and "deploys show" in err


def test_rollback_timeout_exits_1(tmp_path: Path) -> None:
    """A rollback that times out behaves the same."""
    api = _api("running", kind="rollback")
    result = invoke(
        make_state(tmp_path, api),
        "rollback",
        "analytics",
        "--env",
        "dev",
        "--to-previous",
        "--timeout",
        "0",
        env=_CI,
    )
    assert result.exit_code == 1
    assert _ID in _plain(result.stderr)


def test_env_falls_back_to_the_profile_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --env the profile's default environment is used."""
    for name in ("LAKANA_URL", "LAKANA_TOKEN", "LAKANA_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    api = _api()
    state = make_state(tmp_path, api)
    write_profile_file(
        state.profile_path(),
        'default_profile = "p"\n[profiles.p]\nurl = "u"\ntoken = "t"\n'
        'default_environment = "dev"\n',
    )
    result = invoke(state, "deploy", "analytics")
    assert result.exit_code == 0, result.output
    assert api.requests[0].url.path == "/api/v1/environments/dev/deploys"


def test_no_wait_json_prints_the_accepted_run(tmp_path: Path) -> None:
    """`--no-wait --output json` prints the accepted id and status."""
    api = _api()
    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "deploy",
        "analytics",
        "--env",
        "dev",
        "--no-wait",
    )
    assert json.loads(result.stdout) == {"id": _ID, "status": "queued"}


def test_no_changes_is_said(tmp_path: Path) -> None:
    """A deploy with an empty diff prints `No changes.`."""
    api = _api(diff={"added": [], "removed": []})
    result = invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev")
    assert "No changes." in result.stdout
    api = _api(diff=None)
    assert (
        "No changes."
        in invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev").stdout
    )


def _dirty_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a dirty git project registered as `analytics` and chdir into it.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Used to change the working directory.

    Returns
    -------
    Path
        The project directory.
    """
    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "lakana.yaml").write_text("repo: analytics\n")
    monkeypatch.chdir(project)
    return project


def _repo_body(kind: str, url: str) -> dict[str, object]:
    """Build a `RepositoryOut` body.

    Parameters
    ----------
    kind
        The repository kind.
    url
        Its URL.

    Returns
    -------
    dict
        The body.
    """
    return {
        "id": "x",
        "name": "analytics",
        "kind": kind,
        "url": url,
        "credential_secret": None,
        "credential_username": None,
        "ssh_known_hosts": None,
        "default_ref": None,
        "created_by": None,
        "created_at": "2026-10-01T12:00:00+00:00",
        "updated_at": "2026-10-01T12:00:00+00:00",
    }


def test_dirty_https_repository_does_not_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a local repository's work tree matters; https deploys fetch from the host."""
    _dirty_project(tmp_path, monkeypatch)
    api = _api()
    api.on("GET", "/api/v1/repositories/analytics", json=_repo_body("https", "https://x/y.git"))
    result = invoke(make_state(tmp_path, api), "deploy", "--env", "dev", env=_CI)
    assert "uncommitted" not in _plain(result.stderr)


def test_dirty_warning_survives_an_unreadable_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the repository lookup fails the warning is skipped, the deploy still goes on."""
    _dirty_project(tmp_path, monkeypatch)
    api = _api()
    result = invoke(make_state(tmp_path, api), "deploy", "--env", "dev", env=_CI)
    assert result.exit_code == 0, result.output
    assert "uncommitted" not in _plain(result.stderr)


def test_clean_project_does_not_look_up_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean tree never calls GET /repositories/NAME."""
    project = _dirty_project(tmp_path, monkeypatch)
    for args in (
        ("add", "-A"),
        ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"),
    ):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)
    api = _api()
    invoke(make_state(tmp_path, api), "deploy", "--env", "dev")
    assert all(r.url.path != "/api/v1/repositories/analytics" for r in api.requests)


def test_deploy_outside_a_project_does_not_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With REPO given and no project around, there is no dirty check."""
    monkeypatch.chdir(tmp_path)
    api = _api()
    result = invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev")
    assert result.exit_code == 0, result.output


def test_project_version_json_and_dag(tmp_path: Path) -> None:
    """JSON output prints the project version; the tree marks shared nodes and hidden inputs."""
    api = FakeApi()
    manifest = {
        "version": 1,
        "assets": [
            {"table": "bronze.a", "materialize": "view", "metadata": {}, "hidden_references": 0},
            {"table": "bronze.b", "materialize": "view"},
            {"table": "silver.c", "materialize": "table", "metadata": {"owner": "o"}},
            {"table": "gold.d", "materialize": "table", "metadata": {"owner": "o"}},
        ],
        "edges": [
            ["bronze.a", "silver.c"],
            ["bronze.b", "silver.c"],
            ["silver.c", "gold.d"],
        ],
        "external_inputs": [],
        "hidden_external_inputs": 0,
    }
    api.on(
        "GET",
        "/api/v1/environments/dev/project-versions/jv1",
        json={
            "id": "jv1",
            "environment": "dev",
            "repository": "analytics",
            "commit_sha": _SHA,
            "code_uri": "s3://b/x.tar.gz",
            "code_sha256": "0" * 64,
            "deployed_by": None,
            "created_at": "2026-10-01T12:00:00+00:00",
            "manifest": manifest,
        },
    )
    state = make_state(tmp_path, api)
    as_json = invoke(state, "--output", "json", "project-version", "show", "jv1", "--env", "dev")
    assert json.loads(as_json.stdout)["id"] == "jv1"
    shown = invoke(state, "project-version", "show", "jv1", "--env", "dev", env=_CI).stdout
    assert shown.count("silver.c") >= 2 and "silver.c (see above)" in shown
    assert "└─ gold.d" in shown


@pytest.mark.parametrize(
    ("value", "lines"),
    [
        (["alpha", "team"], ["  - alpha", "  - team"]),
        ("missing", None),
        (None, None),
        ([], None),
        ("team", None),
        (["team", 3], None),
    ],
)
def test_project_version_show_names_the_package_indexes(
    tmp_path: Path, value: object, lines: list[str] | None
) -> None:
    api = FakeApi()
    manifest: dict[str, object] = {"version": 3, "assets": [], "edges": []}
    if value != "missing":
        manifest["package_indexes"] = value
    api.on(
        "GET",
        "/api/v1/environments/dev/project-versions/jv1",
        json={
            "id": "jv1",
            "environment": "dev",
            "repository": "analytics",
            "commit_sha": _SHA,
            "code_uri": "s3://b/x.tar.gz",
            "code_sha256": "0" * 64,
            "deployed_by": None,
            "created_at": "2026-10-01T12:00:00+00:00",
            "manifest": manifest,
        },
    )
    shown = invoke(
        make_state(tmp_path, api), "project-version", "show", "jv1", "--env", "dev", env=_CI
    )
    assert shown.exit_code == 0
    assert ("package indexes:" in shown.stdout) == (lines is not None)
    if lines is not None:
        shown_lines = shown.stdout.splitlines()
        start = shown_lines.index("package indexes:")
        assert shown_lines[start + 1 : start + 1 + len(lines)] == lines


def test_project_version_show_prints_index_names_one_per_line_and_escaped(tmp_path: Path) -> None:
    """A name may hold a comma, an escape sequence and a line break."""
    api = FakeApi()
    names = ["a,b", "t\x1b[2J\nrepo @ deadbeef in prod"]
    api.on(
        "GET",
        "/api/v1/environments/dev/project-versions/jv1",
        json={
            "id": "jv1",
            "environment": "dev",
            "repository": "analytics",
            "commit_sha": _SHA,
            "code_uri": "s3://b/x.tar.gz",
            "code_sha256": "0" * 64,
            "deployed_by": None,
            "created_at": "2026-10-01T12:00:00+00:00",
            "manifest": {"version": 3, "assets": [], "edges": [], "package_indexes": names},
        },
    )
    shown = invoke(
        make_state(tmp_path, api), "project-version", "show", "jv1", "--env", "dev", env=_CI
    )
    assert "\x1b" not in shown.stdout
    lines = shown.stdout.splitlines()
    start = lines.index("package indexes:")
    assert lines[start + 1] == "  - a,b"
    assert lines[start + 2].startswith("  - t") and "\\n" in lines[start + 2]
    assert not any(line.startswith("repo @") for line in lines)
    assert [line for line in lines[start + 1 :] if line.startswith("  - ")] == lines[
        start + 1 : start + 3
    ]


def test_project_version_show_lists_at_most_twenty_package_indexes(tmp_path: Path) -> None:
    """A manifest with many indexes cannot flood the screen; JSON has them all."""
    api = FakeApi()
    names = [f"idx{number:02d}" for number in range(25)]
    api.on(
        "GET",
        "/api/v1/environments/dev/project-versions/jv1",
        json={
            "id": "jv1",
            "environment": "dev",
            "repository": "analytics",
            "commit_sha": _SHA,
            "code_uri": "s3://b/x.tar.gz",
            "code_sha256": "0" * 64,
            "deployed_by": None,
            "created_at": "2026-10-01T12:00:00+00:00",
            "manifest": {"version": 3, "assets": [], "edges": [], "package_indexes": names},
        },
    )
    shown = invoke(
        make_state(tmp_path, api), "project-version", "show", "jv1", "--env", "dev", env=_CI
    )
    lines = shown.stdout.splitlines()
    start = lines.index("package indexes:")
    assert lines[start + 1 : start + 21] == [f"  - {name}" for name in names[:20]]
    assert lines[start + 21] == "  and 5 more: use --output json"
    assert "idx20" not in shown.stdout


def test_dag_lines_empty_manifest() -> None:
    """A manifest without assets renders no lines."""
    from lakana.cli.deploys import dag_lines

    assert dag_lines({}) == []


def test_orphans_json_and_empty(tmp_path: Path) -> None:
    """JSON prints the report; no orphans prints no hidden note."""
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/orphans", json={"orphans": [], "hidden": 0})
    state = make_state(tmp_path, api)
    assert json.loads(invoke(state, "--output", "json", "orphans", "--env", "dev").stdout) == {
        "orphans": [],
        "hidden": 0,
    }
    assert "hidden" not in invoke(state, "orphans", "--env", "dev").stdout


def test_the_id_is_printed_before_polling(tmp_path: Path) -> None:
    """A poll that is refused still leaves the id on the terminal (CI with DEPLOY only)."""
    api = FakeApi()
    api.on(
        "POST", "/api/v1/environments/dev/deploys", status=202, json={"id": _ID, "status": "queued"}
    )
    api.on(
        "GET",
        f"/api/v1/environments/dev/deploys/{_ID}",
        status=403,
        json={"detail": "nope", "type": "PermissionDenied"},
    )
    result = invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code != 0
    assert f"Queued deploy {_ID}" in _plain(result.stderr + result.stdout)


def test_rollback_prints_the_id_first(tmp_path: Path) -> None:
    """A rollback announces its run id too."""
    api = _api(kind="rollback")
    result = invoke(
        make_state(tmp_path, api), "rollback", "analytics", "--env", "dev", "--to-previous", env=_CI
    )
    assert f"Queued rollback {_ID}" in _plain(result.stderr + result.stdout)


def test_old_deploy_init_points_to_bundle_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lakana deploy init` (older muscle memory) names the new `lakana bundle init`."""
    monkeypatch.chdir(tmp_path)
    for name in ("LAKANA_URL", "LAKANA_TOKEN", "LAKANA_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/environments/dev/deploys",
        status=404,
        json={"detail": "No repository named 'init'."},
    )
    unknown = invoke(make_state(tmp_path, api), "deploy", "init", "--env", "dev", env=_CI)
    assert unknown.exit_code == 6
    assert "'lakana deploy init' is now 'lakana bundle init'" in _plain(unknown.stderr)
    no_env = invoke(make_state(tmp_path, FakeApi()), "deploy", "init", env=_CI)
    assert no_env.exit_code == 2
    assert "'lakana deploy init' is now 'lakana bundle init'" in _plain(no_env.stderr)
    other = invoke(make_state(tmp_path, api), "deploy", "nothing", "--env", "dev", env=_CI)
    assert other.exit_code == 6
    assert "bundle init" not in _plain(other.stderr)


def test_a_repository_really_named_init_still_deploys(tmp_path: Path) -> None:
    """The hint only appears on failure; a registered `init` repository deploys normally."""
    api = _api(repository="init")
    result = invoke(make_state(tmp_path, api), "deploy", "init", "--env", "dev", env=_CI)
    assert result.exit_code == 0, result.output
    assert "bundle init" not in _plain(result.stderr + result.stdout)


_DEPLOYMENT = {
    "repository": "analytics",
    "project_version_id": "00000000-0000-0000-0000-0000000000bb",
    "commit_sha": _SHA,
    "updated_at": "2026-10-01T12:00:00+00:00",
    "run_as": "etl",
}


def test_deployments_group_lists_with_and_without_the_subcommand(tmp_path: Path) -> None:
    """`lakana deployments --env E` still lists; so does `deployments list`."""
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/deployments", json=[_DEPLOYMENT])
    state = make_state(tmp_path, api)
    for args in (("deployments", "--env", "dev"), ("deployments", "list", "--env", "dev")):
        result = invoke(state, *args, env=_CI)
        assert result.exit_code == 0, result.output
        assert "etl" in _plain(result.stdout)


def test_set_run_as_and_clear(tmp_path: Path) -> None:
    """`set-run-as --principal` PUTs, `--clear` DELETEs; exactly one is required."""
    api = FakeApi()
    path = "/api/v1/environments/prod/deployments/analytics/run-as"
    api.on("PUT", path, json=_DEPLOYMENT)
    api.on("DELETE", path, json={**_DEPLOYMENT, "run_as": None})
    state = make_state(tmp_path, api)
    set_result = invoke(
        state, "deployments", "set-run-as", "analytics", "--env", "prod", "--principal", "etl"
    )
    assert set_result.exit_code == 0, set_result.output
    assert json.loads(api.requests[-1].content) == {"principal": "etl"}
    clear = invoke(state, "deployments", "set-run-as", "analytics", "--env", "prod", "--clear")
    assert clear.exit_code == 0, clear.output
    assert api.requests[-1].method == "DELETE"
    for extra in ((), ("--clear", "--principal", "etl")):
        bad = invoke(state, "deployments", "set-run-as", "analytics", "--env", "prod", *extra)
        assert bad.exit_code == 2


def test_undeploy_confirms_and_reports_conflict(tmp_path: Path) -> None:
    """`undeploy --yes` DELETEs; without a terminal and --yes it refuses; 409 exits 7."""
    api = FakeApi()
    api.on("DELETE", "/api/v1/environments/dev/deployments/analytics", status=204)
    state = make_state(tmp_path, api)
    assert invoke(state, "undeploy", "analytics", "--env", "dev").exit_code == 2
    done = invoke(state, "undeploy", "analytics", "--env", "dev", "--yes", env=_CI)
    assert done.exit_code == 0, done.output
    assert "Undeployed analytics from dev" in _plain(done.stdout)
    busy = FakeApi()
    busy.on(
        "DELETE",
        "/api/v1/environments/dev/deployments/analytics",
        status=409,
        json={"detail": "Run 1 of 'analytics' is active in 'dev'"},
    )
    result = invoke(make_state(tmp_path, busy), "undeploy", "analytics", "--env", "dev", "--yes")
    assert result.exit_code == 7


def test_group_level_env_reaches_the_subcommand(tmp_path: Path) -> None:
    """`deployments --env prod set-run-as` binds in prod; a subcommand `--env` wins."""
    api = FakeApi()
    api.on("PUT", "/api/v1/environments/prod/deployments/analytics/run-as", json=_DEPLOYMENT)
    api.on("PUT", "/api/v1/environments/dev/deployments/analytics/run-as", json=_DEPLOYMENT)
    state = make_state(tmp_path, api)
    grouped = invoke(
        state, "deployments", "--env", "prod", "set-run-as", "analytics", "--principal", "etl"
    )
    assert grouped.exit_code == 0, grouped.output
    assert api.requests[-1].url.path.startswith("/api/v1/environments/prod/")
    explicit = invoke(
        state,
        "deployments", "--env", "prod", "set-run-as", "analytics", "--env", "dev",
        "--principal", "etl",
    )  # fmt: skip
    assert explicit.exit_code == 0, explicit.output
    assert api.requests[-1].url.path.startswith("/api/v1/environments/dev/")


def test_hidden_adopted_tables_are_noted(tmp_path: Path) -> None:
    """The report says how many adopted tables the server withheld, and JSON carries the count."""
    api = _api(hidden_adopted=2)
    result = invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI)
    assert "2 adopted table(s) hidden" in _plain(result.stdout)
    api = _api(hidden_adopted=3)
    as_json = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "deploy",
        "analytics",
        "--env",
        "dev",
        env=_CI,
    )
    assert json.loads(as_json.stdout)["hidden_adopted"] == 3


def test_a_deploy_warning_is_printed_on_stderr_and_carried_in_json(tmp_path: Path) -> None:
    """The warning of a removed asset of a kept table reaches the screen and the JSON output."""
    text = (
        "this deploy removes the asset of `silver.orders`, which is kept after its checks failed "
        "or could not run; add it back and build it, or drop the table"
    )
    api = _api(warnings=[text])
    result = invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 0, result.output
    assert f"warning: {text}" in _plain(result.stderr)
    assert "warning" not in _plain(result.stdout)
    as_json = invoke(
        make_state(tmp_path, _api(warnings=[text])),
        "--output",
        "json",
        "deploy",
        "analytics",
        "--env",
        "dev",
        env=_CI,
    )
    assert json.loads(as_json.stdout)["warnings"] == [text]
    quiet = invoke(make_state(tmp_path, _api()), "deploy", "analytics", "--env", "dev", env=_CI)
    assert "warning" not in _plain(quiet.stderr)


def test_project_version_show_notes_hidden_edges(tmp_path: Path) -> None:
    """Like hidden external inputs, hidden DAG edges are counted on their own line."""
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/environments/dev/project-versions/jv1",
        json={
            "id": "jv1",
            "environment": "dev",
            "repository": "analytics",
            "commit_sha": _SHA,
            "code_uri": "s3://b/x.tar.gz",
            "code_sha256": "0" * 64,
            "deployed_by": None,
            "created_at": "2026-10-01T12:00:00+00:00",
            "manifest": {
                "version": 1,
                "assets": [
                    {
                        "table": "gold.c",
                        "materialize": "table",
                        "metadata": {},
                        "hidden_references": 2,
                    }
                ],
                "edges": [],
                "external_inputs": [],
                "hidden_edges": 2,
            },
        },
    )
    shown = _plain(
        invoke(
            make_state(tmp_path, api), "project-version", "show", "jv1", "--env", "dev", env=_CI
        ).stdout
    )
    assert "2 edge(s) hidden" in shown


def test_no_visible_changes_with_hidden_adopted_does_not_claim_no_changes(
    tmp_path: Path,
) -> None:
    """`No changes.` followed by a hidden-adopted note would contradict itself."""
    api = _api(diff={"added": [], "adopted": []}, hidden_adopted=3)
    out = _plain(
        invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI).stdout
    )
    assert "No changes." not in out
    assert "No visible changes; 3 adopted table(s) hidden" in out


def test_visible_changes_and_hidden_adopted_are_both_shown(tmp_path: Path) -> None:
    """With rows, the hidden-adopted note follows the table."""
    api = _api(hidden_adopted=1)
    out = _plain(
        invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI).stdout
    )
    assert "added silver.clean" in out
    assert "(1 adopted table(s) hidden: you can't read them)" in out


def test_a_successful_deploy_names_the_next_command(tmp_path: Path) -> None:
    """After a finished deploy the closing hint names `lakana run` for that environment."""
    result = invoke(make_state(tmp_path, _api()), "deploy", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 0, result.output
    assert "Hint: run 'lakana run analytics --env dev' to run what you just deployed." in _plain(
        result.stderr
    )
    assert "lakana run" not in _plain(result.stdout)


def test_the_hint_omits_the_repository_when_it_came_from_lakana_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside a project the short form works, so the hint uses it."""
    (tmp_path / "proj").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path / "proj", check=True)
    (tmp_path / "proj" / "lakana.yaml").write_text("repo: analytics\n")
    monkeypatch.chdir(tmp_path / "proj")
    result = invoke(make_state(tmp_path, _api()), "deploy", "--env", "dev")
    assert result.exit_code == 0, result.output
    assert "Hint: run 'lakana run --env dev' to run what you just deployed." in _plain(
        result.stderr
    )


def test_the_next_command_hint_is_only_for_a_finished_deploy(tmp_path: Path) -> None:
    """No hint under JSON, for a failed deploy, a dry run, a rollback or `--no-wait`."""
    hint = "lakana run"
    json_run = invoke(
        make_state(tmp_path, _api()), "--output", "json", "deploy", "analytics", "--env", "dev"
    )
    failed = invoke(
        make_state(tmp_path, _api("failed", error_message="boom")),
        "deploy",
        "analytics",
        "--env",
        "dev",
    )
    dry = invoke(
        make_state(tmp_path, _api(kind="dry_run")),
        "deploy",
        "analytics",
        "--env",
        "dev",
        "--dry-run",
    )
    rollback = invoke(
        make_state(tmp_path, _api(kind="rollback")),
        "rollback",
        "analytics",
        "--env",
        "dev",
        "--to-previous",
    )
    no_wait = invoke(
        make_state(tmp_path, _api()), "deploy", "analytics", "--env", "dev", "--no-wait"
    )
    for result in (json_run, failed, dry, rollback, no_wait):
        assert hint not in result.stdout + result.stderr
    assert failed.exit_code == 1


_CHECKS = {
    "silver.station_temperatures": [
        {"check": "no_missing_station", "on_failure": "block"},
        {"check": "plausible_range", "on_failure": "warn"},
        {"check": "unique_reading", "on_failure": "block"},
    ]
}


def test_a_dry_run_lists_the_checks_per_table(tmp_path: Path) -> None:
    api = _api(kind="dry_run", checks=_CHECKS)
    result = invoke(
        make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", "--dry-run", env=_CI
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.endswith(
        "Checks:\n"
        "  silver.station_temperatures: no_missing_station, plausible_range (warn), "
        "unique_reading\n"
    )
    assert result.stdout.index("sql_changed") < result.stdout.index("Checks:")


def test_a_keep_check_is_marked_in_the_list_of_checks(tmp_path: Path) -> None:
    checks = {
        "silver.orders": [
            {"check": "unique_id", "on_failure": "keep"},
            {"check": "not_null", "on_failure": "block"},
        ]
    }
    result = invoke(
        make_state(tmp_path, _api(checks=checks)), "deploy", "analytics", "--env", "dev", env=_CI
    )
    assert "Checks:\n  silver.orders: unique_id (keep), not_null\n" in result.stdout


def _defaulted(count: int, value: str = "warn") -> dict[str, list[dict[str, str]]]:
    """Build a listing with `count` checks that took the project's default.

    Parameters
    ----------
    count
        How many checks carry ``"from": "lakana.yaml"``.
    value
        Their ``on_failure``.

    Returns
    -------
    dict
        One table, `count` defaulted checks and one that stated its own value.
    """
    taken = [
        {"check": f"c{index}", "on_failure": value, "from": "lakana.yaml"} for index in range(count)
    ]
    return {"silver.t": [*taken, {"check": "own", "on_failure": "block"}]}


@pytest.mark.parametrize(
    ("count", "line"),
    [
        (2, "2 checks take `on_failure: warn` from lakana.yaml.\n"),
        (1, "1 check takes `on_failure: warn` from lakana.yaml.\n"),
    ],
)
def test_the_checks_block_ends_with_the_count_taken_from_the_default(
    tmp_path: Path, count: int, line: str
) -> None:
    result = invoke(
        make_state(tmp_path, _api(checks=_defaulted(count))),
        "deploy",
        "analytics",
        "--env",
        "dev",
        env=_CI,
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.endswith(
        "Checks:\n  silver.t: "
        + ", ".join([*(f"c{i} (warn)" for i in range(count)), "own"])
        + "\n"
        + line
    )


def test_no_default_line_without_a_default(tmp_path: Path) -> None:
    result = invoke(
        make_state(tmp_path, _api(checks=_CHECKS)), "deploy", "analytics", "--env", "dev", env=_CI
    )
    assert "lakana.yaml" not in result.stdout


def test_a_deploy_lists_the_checks_per_table(tmp_path: Path) -> None:
    result = invoke(
        make_state(tmp_path, _api(checks=_CHECKS)), "deploy", "analytics", "--env", "dev", env=_CI
    )
    assert "Checks:\n  silver.station_temperatures: no_missing_station" in result.stdout


def test_a_deploy_without_checks_prints_no_checks_block(tmp_path: Path) -> None:
    for checks in (None, {}):
        result = invoke(
            make_state(tmp_path, _api(checks=checks)),
            "deploy",
            "analytics",
            "--env",
            "dev",
            env=_CI,
        )
        assert result.exit_code == 0, result.output
        assert "Checks" not in result.stdout


def test_a_check_name_cannot_inject_terminal_controls(tmp_path: Path) -> None:
    checks = {"silver.t\x1b[2J": [{"check": "x\x1b[31m\nforged", "on_failure": "block"}]}
    result = invoke(
        make_state(tmp_path, _api(checks=checks)), "deploy", "analytics", "--env", "dev", env=_CI
    )
    assert "\x1b" not in result.stdout
    assert "\nforged" not in result.stdout
    assert "  silver.t\\x1b[2J: x\\x1b[31m\\nforged\n" in result.stdout


def test_a_body_from_an_older_server_reads_checks_as_none() -> None:
    from lakana.resources.deploys import deploy_from_json

    body = _deploy("succeeded")
    assert "checks" not in body
    assert deploy_from_json(body).checks is None
    assert deploy_from_json({**body, "checks": _CHECKS}).checks == _CHECKS


def test_a_dry_run_prints_job_changes_as_their_own_section(tmp_path: Path) -> None:
    """Jobs are not tables: they get a `Jobs:` section, and the table rows stay tables."""
    api = _api(
        diff={
            "added": ["silver.clean"],
            "jobs_added": ["nightly"],
            "jobs_removed": ["old"],
            "jobs_changed": ["hourly (cron, time zone)"],
        }
    )
    out = _plain(
        invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI).stdout
    )
    assert "added silver.clean" in out
    assert "jobs_added" not in out
    assert "Jobs: added nightly removed old changed hourly (cron, time zone)" in out


def test_a_deploy_that_changes_only_jobs_does_not_say_no_changes(tmp_path: Path) -> None:
    api = _api(diff={"added": [], "jobs_added": ["nightly"]})
    out = _plain(
        invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI).stdout
    )
    assert "No changes." not in out
    assert "Jobs: added nightly" in out


def test_a_deploy_without_job_changes_prints_no_jobs_section(tmp_path: Path) -> None:
    api = _api(diff={"added": ["silver.clean"], "jobs_added": []})
    out = _plain(
        invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI).stdout
    )
    assert "Jobs:" not in out


_NO_WORKER = (
    "No worker is running, so this deploy cannot start. Ask the operator to start a worker; "
    "on a local instance, start it again with 'lakana server start --local'."
)
_OF_KIND = (
    "No worker that takes pipeline work is running (1 worker takes interactive work only), so "
    "this deploy cannot start. Ask the operator to start a worker with "
    "LAKANA_WORKER__TAKES=pipeline or all."
)


class _Clock:
    """A clock the test moves by hand.

    Parameters
    ----------
    now
        The time it shows.
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        """Read the clock.

        Returns
        -------
        float
            The time.
        """
        return self.now


@pytest.fixture
def _slow_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Make every sleep of a poll five seconds on the clock of the wait line.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.

    Returns
    -------
    _Clock
        The clock.
    """
    clock = _Clock()
    monkeypatch.setattr("lakana.cli.deploys.clock", clock)
    monkeypatch.setattr(
        "lakana.resources.deploys.time.sleep",
        lambda _seconds: setattr(clock, "now", clock.now + 5.0),
    )
    return clock


def _waiting_api(polls: list[dict[str, object]]) -> FakeApi:
    """Build a fake whose deploy polls answer `polls` in order, the last one repeating.

    Parameters
    ----------
    polls
        The bodies.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = _api()
    answers = iter(polls)
    last = polls[-1]

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal last
        last = next(answers, last)
        return httpx.Response(200, json=last)

    api.routes["GET", f"/api/v1/environments/dev/deploys/{_ID}"] = respond
    return api


def _queued(code: str, message: str, **overrides: object) -> dict[str, object]:
    """Build a queued deploy body with a reason.

    Parameters
    ----------
    code
        The reason's code.
    message
        Its sentence.
    **overrides
        Fields of the run.

    Returns
    -------
    dict
        The body.
    """
    return _deploy("queued", waiting_for={"code": code, "message": message}, **overrides)


@pytest.mark.usefixtures("_slow_clock")
def test_deploy_prints_why_it_waits_once_per_change_of_reason(tmp_path: Path) -> None:
    api = _waiting_api(
        [
            _queued("no_worker", _NO_WORKER),
            _queued("no_worker", _NO_WORKER),
            _queued("no_worker_of_kind", _OF_KIND),
            _queued("no_worker_of_kind", _OF_KIND),
            _deploy("succeeded"),
        ]
    )
    result = invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 0, result.stderr
    assert _SHA in result.stdout and "Waiting" not in result.stdout
    lines = [line for line in result.stderr.splitlines() if line.startswith("Waiting:")]
    assert lines == [f"Waiting: {_NO_WORKER}", f"Waiting: {_OF_KIND}"]


@pytest.mark.usefixtures("_slow_clock")
def test_rollback_prints_why_it_waits(tmp_path: Path) -> None:
    api = _waiting_api(
        [
            _queued("no_worker", _NO_WORKER, kind="rollback"),
            _queued("no_worker", _NO_WORKER, kind="rollback"),
            _deploy("succeeded", kind="rollback"),
        ]
    )
    result = invoke(
        make_state(tmp_path, api),
        "rollback",
        "analytics",
        "--env",
        "dev",
        "--to-previous",
        env=_CI,
    )
    assert result.exit_code == 0, result.stderr
    assert f"Waiting: {_NO_WORKER}" in result.stderr.splitlines()


def test_a_short_wait_prints_no_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lakana.resources.deploys.time.sleep", lambda _seconds: None)
    api = _waiting_api([_queued("starting", "A worker is free."), _deploy("succeeded")])
    result = invoke(make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", env=_CI)
    assert result.exit_code == 0, result.stderr
    assert "Waiting:" not in result.stderr


@pytest.mark.usefixtures("_slow_clock")
def test_no_wait_line_with_json_output(tmp_path: Path) -> None:
    api = _waiting_api(
        [_queued("no_worker", _NO_WORKER), _queued("no_worker", _NO_WORKER), _deploy("succeeded")]
    )
    result = invoke(
        make_state(tmp_path, api), "--output", "json", "deploy", "analytics", "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    assert "Waiting:" not in result.stderr
    assert json.loads(result.stdout)["status"] == "succeeded"


_HINT = (
    "customers: `materialize: incremental` is now written `write: append` with "
    "`watermark: ts`; both work"
)


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_deploy_prints_each_hint_after_the_diff_and_never_as_a_changed_table(
    tmp_path: Path, dry_run: bool
) -> None:
    diff = {"added": ["silver.clean"], "hints": [_HINT]}
    api = _api(kind="dry_run" if dry_run else "deploy", diff=diff)
    flags = ["--dry-run"] if dry_run else []
    result = invoke(
        make_state(tmp_path, api), "deploy", "analytics", "--env", "dev", *flags, env=_CI
    )
    assert result.exit_code == 0, result.output
    assert f"hint: {_HINT}" in _plain(result.stderr)
    out = _plain(result.stdout)
    assert "added silver.clean" in out
    assert "hints" not in out
    alone = invoke(
        make_state(tmp_path, _api(diff={"added": [], "hints": [_HINT]})),
        "deploy",
        "analytics",
        "--env",
        "dev",
        env=_CI,
    )
    assert "No changes." in _plain(alone.stdout)
    assert f"hint: {_HINT}" in _plain(alone.stderr)
