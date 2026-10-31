"""`lakana deploy` from a CI job with its OIDC ID token (`LAKANA_OIDC`, `LAKANA_OIDC_TOKEN`)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import httpx
import pytest
from conftest import BASE_URL, TOKEN, FakeApi, invoke, make_state

import lakana
from lakana.cli import _state
from lakana.cli._state import CliState
from lakana.errors import ConfigError

_ID = "00000000-0000-0000-0000-0000000000aa"
_SHA = "a" * 40
_RUNNER = f"{BASE_URL}/runner/idtoken?api-version=2.0"
_SECRET = "cisecretpart"
_GITHUB = {
    "LAKANA_OIDC": "github",
    "LAKANA_URL": BASE_URL,
    "ACTIONS_ID_TOKEN_REQUEST_URL": _RUNNER,
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runner-request-credential",
}


def _issued(n: int) -> dict[str, object]:
    """Build the server's answer to the `n`-th exchange.

    Parameters
    ----------
    n
        The exchange's number.

    Returns
    -------
    dict
        A `CiTokenOut` body.
    """
    return {
        "token": f"lakana_ci_k{n}_{_SECRET}{n}",
        "expires_at": "2026-10-20T13:00:00+00:00",
        "principal": "ci-prod",
        "environment": "prod",
        "repository": "analytics",
        "kinds": ["deploy", "dry_run"],
        "sha": _SHA,
        "rule_id": "00000000-0000-0000-0000-000000000001",
    }


def _api() -> FakeApi:
    """Fake a runner that issues a new ID token per request, and a server that exchanges each.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    tokens = itertools.count(1)
    exchanges = itertools.count(1)
    api.routes["GET", "/runner/idtoken"] = lambda request: httpx.Response(
        200, json={"value": f"id.token.{next(tokens)}"}
    )
    api.routes["POST", "/api/v1/auth/oidc/token"] = lambda request: httpx.Response(
        200, json=_issued(next(exchanges))
    )
    api.on(
        "POST",
        "/api/v1/environments/prod/deploys",
        status=202,
        json={"id": _ID, "status": "queued"},
    )
    api.on(
        "GET",
        f"/api/v1/environments/prod/deploys/{_ID}",
        json={
            "id": _ID,
            "environment": "prod",
            "repository": "analytics",
            "kind": "deploy",
            "status": "succeeded",
            "requested_ref": _SHA,
            "resolved_sha": _SHA,
            "rollback_to_project_version_id": None,
            "error_message": None,
            "diff": None,
            "project_version_id": None,
            "principal_id": None,
            "created_at": "2026-10-01T12:00:00+00:00",
            "started_at": None,
            "finished_at": None,
        },
    )
    return api


def _state_over(tmp_path: Path, api: FakeApi) -> CliState:
    """Build a CLI state whose CI client exchanges through `api`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    api
        The fake.

    Returns
    -------
    CliState
        The state.
    """
    state = make_state(tmp_path, api)

    def ci_client(state: CliState, environment: str, repository: str) -> lakana.Client:
        """Exchange through the fake, as `default_ci_client_factory` does over the network.

        Parameters
        ----------
        state
            The CLI state.
        environment
            The environment.
        repository
            The repository.

        Returns
        -------
        lakana.Client
            The client.
        """
        http = httpx.Client(transport=httpx.MockTransport(api.handle), base_url=BASE_URL)
        return lakana.Client.from_ci_oidc(
            BASE_URL, environment=environment, repository=repository, http_client=http
        )

    state.ci_client_factory = ci_client
    return state


def _paths(api: FakeApi) -> list[str]:
    """List the requests the fake received as ``METHOD path``.

    Parameters
    ----------
    api
        The fake.

    Returns
    -------
    list of str
        The requests.
    """
    return [f"{request.method} {request.url.path}" for request in api.requests]


def test_a_dry_run_then_a_deploy_exchange_a_new_github_id_token_each(tmp_path: Path) -> None:
    api = _api()
    state = _state_over(tmp_path, api)
    for extra in (["--dry-run"], []):
        result = invoke(
            state, "deploy", "analytics", "--env", "prod", "--ref", _SHA, *extra, env=_GITHUB
        )
        state.close()
        assert result.exit_code == 0, result.output
        assert _SECRET not in result.output and "id.token." not in result.output
        assert "acting as ci-prod" in result.stderr
    exchanges = [r for r in api.requests if r.url.path == "/api/v1/auth/oidc/token"]
    assert [json.loads(r.content)["id_token"] for r in exchanges] == ["id.token.1", "id.token.2"]
    deploys = [r for r in api.requests if r.method == "POST" and r.url.path.endswith("/deploys")]
    assert [r.headers["authorization"] for r in deploys] == [
        f"Bearer lakana_ci_k1_{_SECRET}1",
        f"Bearer lakana_ci_k2_{_SECRET}2",
    ]
    assert [json.loads(r.content)["kind"] for r in deploys] == ["dry_run", "deploy"]
    assert not [r for r in api.requests if TOKEN in r.headers.get("authorization", "")]


def test_an_id_token_in_the_environment_is_exchanged_as_it_is(tmp_path: Path) -> None:
    api = _api()
    env = {"LAKANA_URL": BASE_URL, "LAKANA_OIDC_TOKEN": "id.token.given"}
    result = invoke(_state_over(tmp_path, api), "deploy", "analytics", "--env", "prod", env=env)
    assert result.exit_code == 0, result.output
    assert _paths(api)[0] == "POST /api/v1/auth/oidc/token"
    assert json.loads(api.requests[0].content)["id_token"] == "id.token.given"


@pytest.mark.parametrize(
    "oidc", [{"LAKANA_OIDC": "github"}, {"LAKANA_OIDC_TOKEN": "id.token.given"}]
)
def test_an_id_token_and_a_stored_token_together_are_refused(
    tmp_path: Path, oidc: dict[str, str]
) -> None:
    api = _api()
    env = {"LAKANA_URL": BASE_URL, "LAKANA_TOKEN": TOKEN, **oidc}
    result = invoke(_state_over(tmp_path, api), "deploy", "analytics", "--env", "prod", env=env)
    assert result.exit_code == 3, result.output
    text = " ".join(result.stderr.split())
    assert "LAKANA_TOKEN" in text and "LAKANA_OIDC" in text
    assert "does not guess" in text
    assert "supersecret" not in result.output and "id.token.given" not in result.output
    assert api.requests == []


def test_a_refused_exchange_points_at_the_trust_rules_not_at_login(tmp_path: Path) -> None:
    api = _api()
    api.on(
        "POST",
        "/api/v1/auth/oidc/token",
        status=403,
        json={"detail": "No CI trust rule accepts this ID token for prod and analytics."},
    )
    result = invoke(_state_over(tmp_path, api), "deploy", "analytics", "--env", "prod", env=_GITHUB)
    assert result.exit_code == 5, result.output
    assert "lakana login" not in result.output
    assert "ci-trust explain" in " ".join(result.stderr.split())


def test_a_second_gitlab_submission_says_how_to_get_two_id_tokens(tmp_path: Path) -> None:
    api = _api()
    api.on(
        "POST",
        "/api/v1/auth/oidc/token",
        status=401,
        json={
            "detail": "This ID token was exchanged before; each ID token works once. Request a "
            "new one in the CI job for each exchange."
        },
    )
    env = {"LAKANA_OIDC": "gitlab", "LAKANA_URL": BASE_URL, "LAKANA_OIDC_TOKEN": "id.token.used"}
    result = invoke(_state_over(tmp_path, api), "deploy", "analytics", "--env", "prod", env=env)
    assert result.exit_code == 4, result.output
    text = " ".join(result.stderr.split())
    assert "id_tokens:" in text and "LAKANA_OIDC_TOKEN_2" in text
    assert "lakana login" not in text
    assert "id.token.used" not in result.output


def test_other_commands_say_that_only_deploy_exchanges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAKANA_OIDC", "github")
    monkeypatch.setenv("LAKANA_URL", BASE_URL)
    monkeypatch.delenv("LAKANA_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="only `lakana deploy`"):
        _state.default_client_factory(CliState(config_path=tmp_path / "config.toml"))
    monkeypatch.setenv("LAKANA_TOKEN", TOKEN)
    with pytest.raises(ConfigError, match="does not guess"):
        _state.default_client_factory(CliState(config_path=tmp_path / "config.toml"))


def test_the_default_ci_client_exchanges_at_the_url_of_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def record(url: str, **options: object) -> str:
        """Stand in for `Client.from_ci_oidc`.

        Parameters
        ----------
        url
            The server's URL.
        **options
            The rest.

        Returns
        -------
        str
            A stand-in client.
        """
        calls.append((url, options))
        return "client"

    monkeypatch.setattr(lakana.Client, "from_ci_oidc", record)
    monkeypatch.delenv("LAKANA_URL", raising=False)
    state = CliState(config_path=tmp_path / "config.toml", timeout=12)
    with pytest.raises(ConfigError, match="LAKANA_URL"):
        _state.default_ci_client_factory(state, "prod", "analytics")
    monkeypatch.setenv("LAKANA_URL", BASE_URL)
    assert _state.default_ci_client_factory(state, "prod", "analytics") == "client"
    state.url = "https://other.example"
    _state.default_ci_client_factory(state, "dev", "analytics")
    assert calls == [
        (BASE_URL, {"environment": "prod", "repository": "analytics", "timeout": 12}),
        ("https://other.example", {"environment": "dev", "repository": "analytics", "timeout": 12}),
    ]


def test_whoami_shows_what_a_ci_token_may_do(tmp_path: Path) -> None:
    body = {
        "name": "ci-prod",
        "type": "SERVICE_ACCOUNT",
        "task_token": None,
        "ci_token": {
            "rule_id": "00000000-0000-0000-0000-000000000001",
            "environment": "prod",
            "repository": "analytics",
            "kinds": ["deploy"],
            "sha": _SHA,
            "expires_at": "2026-10-20T13:00:00+00:00",
            "used": False,
        },
        "language": None,
    }
    api = FakeApi()
    api.on("GET", "/api/v1/whoami", json=body)
    state = make_state(tmp_path, api)
    table = " ".join(invoke(state, "whoami").stdout.split())
    for shown in ("ci_token", "00000000-0000-0000-0000-000000000001", _SHA, '"used": false'):
        assert shown in table
    assert json.loads(invoke(state, "--output", "json", "whoami").stdout) == body


def test_a_ci_client_without_a_reported_scope_deploys_without_the_scope_line(
    tmp_path: Path,
) -> None:
    api = _api()
    state = make_state(tmp_path, api)
    state.ci_client_factory = lambda state, environment, repository: api.client()
    result = invoke(state, "deploy", "analytics", "--env", "prod", env=_GITHUB)
    assert result.exit_code == 0, result.output
    assert "Exchanged the CI job's ID token" not in result.stderr
    assert _paths(api)[0] == "POST /api/v1/environments/prod/deploys"
