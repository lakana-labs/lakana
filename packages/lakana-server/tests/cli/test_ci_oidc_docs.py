"""The commands of the CI OIDC how-tos, run with the CLI against the real app.

`docs/examples/ci-trust-rules.sh` holds what the how-tos "Deploy from GitHub Actions without a
stored secret" and "Deploy from GitLab CI without a stored secret" show. Each command runs here;
`ci-trust explain` then shows that the rules accept the jobs of the CI files `lakana init` writes
and refuse what they must, and `lakana deploy` exchanges an ID token end to end.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from scripts.docs_blocks import read_source
from typer.testing import CliRunner, Result

import lakana
from lakana.cli._state import CliState
from lakana.cli.app import build_app
from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_server.settings import Settings
from tests.api.platform.test_ci_oidc import AUDIENCE, SHA, _app_client, _claims, _sign
from tests.cli.conftest import make_cli_state
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import create_environment

_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = "docs/examples/ci-trust-rules.sh"
_GITHUB = "https://token.actions.githubusercontent.com"
_GITLAB = "https://gitlab.com"


@pytest.fixture
def app(settings: Settings) -> Iterator[TestClient]:
    """Run an app with CI OIDC on for GitHub and GitLab, with `dev`, `prod` and `analytics`.

    Parameters
    ----------
    settings
        The base settings.

    Yields
    ------
    TestClient
        The client.
    """
    configured = settings.model_copy(
        update={"ci_oidc": CiOidcConfig(issuers=["github", "gitlab"], audience=AUDIENCE)}
    )
    for client in _app_client(configured):
        create_environment(client, "dev")
        create_environment(client, "prod")
        added = client.post(
            "/api/v1/repositories",
            headers=AUTH_HEADER,
            json={"name": "analytics", "kind": "https", "url": "https://example.com/a.git"},
        )
        assert added.status_code == 201, added.text
        yield client


def _run(state: CliState, *args: str, env: dict[str, str] | None = None) -> Result:
    """Run one CLI command in-process with a clean environment.

    Parameters
    ----------
    state
        The CLI state.
    *args
        The command line, without the program name.
    env
        More environment variables.

    Returns
    -------
    Result
        The result.
    """
    clean: dict[str, str | None] = {
        name: None
        for name in (
            "LAKANA_OUTPUT",
            "LAKANA_PROFILE",
            "LAKANA_TOKEN",
            "LAKANA_URL",
            "LAKANA_OIDC",
            "LAKANA_OIDC_TOKEN",
        )
    }
    clean.update({"COLUMNS": "200", "NO_COLOR": "1", **(env or {})})
    return CliRunner().invoke(build_app(), list(args), obj=state, env=clean)


def _region_commands(region: str) -> list[list[str]]:
    """Read the commands of a region of the how-tos' script, continuations joined.

    Parameters
    ----------
    region
        The region's name.

    Returns
    -------
    list of list of str
        Each command's words, without the leading ``lakana``.
    """
    text = read_source(_ROOT, f"{_SCRIPT}#{region}").replace("\\\n", " ")
    commands = [shlex.split(line) for line in text.splitlines() if line.strip()]
    assert all(words[0] == "lakana" for words in commands), commands
    return [words[1:] for words in commands]


def _verdict(client: TestClient, environment: str, claims: dict[str, str]) -> str:
    """Ask `ci-trust explain` what an exchange with these claims would do.

    Parameters
    ----------
    client
        The client.
    environment
        The environment asked for.
    claims
        The ID token's claims.

    Returns
    -------
    str
        The verdict.
    """
    response = client.post(
        "/api/v1/ci-trust-rules/explain",
        headers=AUTH_HEADER,
        json={"claims": claims, "environment": environment, "repository": "analytics"},
    )
    assert response.status_code == 200, response.text
    return response.json()["verdict"]


def _create_the_documented_rules(
    app: TestClient, tmp_path: Path, regions: tuple[str, ...] = ("accounts", "github", "gitlab")
) -> CliState:
    """Run the commands of the how-tos' script that create the accounts and rules.

    Parameters
    ----------
    app
        The client.
    tmp_path
        Pytest's per-test directory.
    regions
        The regions to run, in order.

    Returns
    -------
    CliState
        The administrator's CLI state.
    """
    state = make_cli_state(app, tmp_path)
    for region in regions:
        for words in _region_commands(region):
            result = _run(state, *words)
            assert result.exit_code == 0, (words, result.stderr)
    return state


@pytest.mark.slow
def test_the_github_rules_accept_the_workflow_s_jobs_and_nothing_else(
    app: TestClient, tmp_path: Path
) -> None:
    _create_the_documented_rules(app, tmp_path)
    base = {"iss": _GITHUB, "repository_id": "123456", "sha": SHA}
    push = {**base, "ref": "refs/heads/main", "event_name": "push", "environment": "prod"}
    pull = {
        **base,
        "ref": "refs/pull/7/merge",
        "event_name": "pull_request",
        "environment": "lakana-dry-run",
    }
    assert _verdict(app, "prod", push).startswith("accepted")
    assert _verdict(app, "dev", pull).startswith("accepted")
    assert _verdict(app, "prod", pull).startswith("refused")
    target = {**base, "ref": "refs/heads/main", "event_name": "pull_request_target"}
    assert _verdict(app, "prod", target).startswith("refused")
    assert _verdict(app, "prod", {**push, "repository_id": "654321"}).startswith("refused")


@pytest.mark.slow
def test_the_gitlab_rules_accept_the_pipeline_s_jobs_and_nothing_else(
    app: TestClient, tmp_path: Path
) -> None:
    _create_the_documented_rules(app, tmp_path)
    base = {"iss": _GITLAB, "project_id": "123456", "sha": SHA}
    main = {
        **base,
        "ref": "main",
        "ref_type": "branch",
        "ref_protected": "true",
        "pipeline_source": "push",
        "environment": "prod",
    }
    merge = {
        **base,
        "ref": "feature",
        "ref_protected": "false",
        "pipeline_source": "merge_request_event",
        "environment": "lakana-dry-run",
    }
    assert _verdict(app, "prod", main).startswith("accepted")
    assert _verdict(app, "dev", merge).startswith("accepted")
    assert _verdict(app, "prod", merge).startswith("refused")
    assert _verdict(app, "prod", {**main, "ref_protected": "false"}).startswith("refused")
    assert _verdict(app, "prod", {**main, "ref_type": "tag"}).startswith("refused")


@pytest.mark.slow
def test_the_gitlab_job_that_dry_runs_then_deploys_is_accepted(
    app: TestClient, tmp_path: Path
) -> None:
    state = _create_the_documented_rules(app, tmp_path, ("accounts", "gitlab"))
    [job] = yaml.safe_load(
        (_ROOT / "docs/examples/ci-oidc-gitlab-one-job.yml").read_text()
    ).values()
    assert job["rules"] == [{"if": '$CI_COMMIT_BRANCH == "main"'}]
    assert set(job["id_tokens"]) == {"LAKANA_OIDC_TOKEN", "LAKANA_OIDC_TOKEN_2"}
    # The claims GitLab gives this job's ID tokens on a push to the protected main branch.
    claims = {
        "iss": _GITLAB,
        "project_id": "123456",
        "ref": "main",
        "ref_type": "branch",
        "ref_protected": "true",
        "pipeline_source": "push",
        "environment": job["environment"],
        "sha": SHA,
    }
    assert _verdict(app, "prod", claims).startswith("accepted")
    [rule] = [
        r
        for r in app.get("/api/v1/ci-trust-rules", headers=AUTH_HEADER).json()
        if r["environment"] == "prod"
    ]
    [words] = _region_commands("both-kinds")
    result = _run(state, *[rule["id"] if word == "RULE_ID" else word for word in words])
    assert result.exit_code == 0, result.stderr
    shown = app.get(f"/api/v1/ci-trust-rules/{rule['id']}", headers=AUTH_HEADER).json()
    assert sorted(shown["kinds"]) == ["deploy", "dry_run"]
    assert _verdict(app, "prod", claims).startswith("accepted")


@pytest.mark.slow
def test_lakana_deploy_exchanges_the_job_s_id_token_once(app: TestClient, tmp_path: Path) -> None:
    state = _create_the_documented_rules(app, tmp_path)
    state.ci_client_factory = lambda state, environment, repository: lakana.Client.from_ci_oidc(
        "http://testserver", environment=environment, repository=repository, http_client=app
    )
    id_token = _sign(_claims(environment="prod"))
    env = {"LAKANA_URL": "http://testserver", "LAKANA_OIDC_TOKEN": id_token}
    args = ("deploy", "analytics", "--env", "prod", "--no-wait")
    queued = _run(state, *args, env=env)
    assert queued.exit_code == 0, queued.stderr
    assert "acting as ci-prod" in queued.stderr
    assert id_token not in queued.output
    deploy_id = queued.stdout.strip()
    shown = app.get(f"/api/v1/environments/prod/deploys/{deploy_id}", headers=AUTH_HEADER)
    assert shown.json()["requested_ref"] == SHA
    again = _run(state, *args, env=env)
    assert again.exit_code == 4
    assert "exchanged before" in again.stderr
    assert id_token not in again.output


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.slow
def test_explain_accepts_the_documented_claims_of_a_deploy(
    app: TestClient, tmp_path: Path, provider: str
) -> None:
    state = _create_the_documented_rules(app, tmp_path, ("accounts", provider))
    claims = _ROOT / "docs" / "examples" / f"ci-trust-claims-{provider}.json"
    [words] = _region_commands("explain")
    result = _run(state, *[str(claims) if word == "claims.json" else word for word in words])
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith("Verdict: accepted by rule")


@pytest.mark.slow
def test_disabling_a_rule_ends_its_ci_tokens(app: TestClient, tmp_path: Path) -> None:
    state = _create_the_documented_rules(app, tmp_path, ("accounts", "github"))
    id_token = _sign(_claims(environment="prod"))
    issued = app.post(
        "/api/v1/auth/oidc/token",
        json={"id_token": id_token, "environment": "prod", "repository": "analytics"},
    ).json()
    header = {"Authorization": f"Bearer {issued['token']}"}
    assert app.get("/api/v1/whoami", headers=header).status_code == 200
    [words] = _region_commands("disable")
    result = _run(state, *[issued["rule_id"] if word == "RULE_ID" else word for word in words])
    assert result.exit_code == 0, result.stderr
    assert app.get("/api/v1/whoami", headers=header).status_code == 401
