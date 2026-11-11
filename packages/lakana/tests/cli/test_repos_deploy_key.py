"""`lakana repo deploy-key`: the server makes a key per environment, the CLI shows public ones."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import FakeApi, invoke, make_state

_ROUTE = "/api/v1/repositories/analytics/deploy-key"
_DEV = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDev lakana deploy key for analytics"
_PROD = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIProd lakana deploy key for analytics"


def _answer(**extra: Any) -> dict[str, Any]:  # ruff: ignore[any-type]
    """Build a `DeployKeyOut` body: dev made now, prod made earlier, staging skipped.

    Parameters
    ----------
    **extra
        Fields to replace.

    Returns
    -------
    dict
        The body.
    """
    return {
        "secret": "analytics-deploy-key",
        "keys": [
            {"environment": "dev", "public_key": _DEV, "fingerprint": "SHA256:dev", "made": True},
            {
                "environment": "prod",
                "public_key": _PROD,
                "fingerprint": "SHA256:prod",
                "made": False,
            },
        ],
        "skipped": ["staging"],
        "replaced": [],
        "notes": [],
        **extra,
    }


def test_cli_deploy_key_prints_the_public_key_only(tmp_path: Path) -> None:
    """Each public key is printed whole, with its environment; then what to do at the git host."""
    api = FakeApi()
    api.on("POST", _ROUTE, status=201, json=_answer())

    result = invoke(make_state(tmp_path, api), "repo", "deploy-key", "analytics")

    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[0].content) == {}
    lines = result.stdout.splitlines()
    assert lines == ["dev (new):", _DEV, "prod (made earlier):", _PROD]
    assert "PRIVATE" not in result.output
    assert "Add each key as a read-only deploy key at your git host." in result.stderr
    assert "staging" in result.stderr and "lakana repo test analytics --env dev" in result.stderr


def test_cli_deploy_key_sends_the_options(tmp_path: Path) -> None:
    """`--environment` (repeatable, also `--env`), `--replace` and `--secret` go in the body."""
    api = FakeApi()
    note = "The old key for prod stops working: replace it at your git host now."
    api.on("POST", _ROUTE, status=201, json=_answer(replaced=["prod"], notes=[note]))

    result = invoke(
        make_state(tmp_path, api),
        "repo",
        "deploy-key",
        "analytics",
        "--environment",
        "prod",
        "--env",
        "dev",
        "--replace",
        "--secret",
        "k2",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[0].content) == {
        "environments": ["prod", "dev"],
        "replace": True,
        "secret": "k2",
    }
    assert note in result.stderr


def test_cli_deploy_key_json_is_the_answer(tmp_path: Path) -> None:
    """With `--output json` the answer is printed as is, nothing on standard error."""
    api = FakeApi()
    api.on("POST", _ROUTE, status=201, json=_answer())

    result = invoke(
        make_state(tmp_path, api), "--output", "json", "repo", "deploy-key", "analytics"
    )

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["secret"] == "analytics-deploy-key"
    assert [k["environment"] for k in body["keys"]] == ["dev", "prod"]
    assert result.stderr == ""


def test_cli_deploy_key_refusal_names_the_next_step(tmp_path: Path) -> None:
    """A server refusal is printed with its next step and a non-zero exit."""
    api = FakeApi()
    api.on(
        "POST",
        _ROUTE,
        status=409,
        json={
            "type": "Conflict",
            "detail": "Environment 'prod' has a revoked secret named 'analytics-deploy-key'. "
            "Use another secret name: lakana repo deploy-key analytics --secret OTHER-NAME",
        },
    )

    result = invoke(make_state(tmp_path, api), "repo", "deploy-key", "analytics")

    assert result.exit_code != 0
    assert "--secret OTHER-NAME" in result.output


def test_cli_deploy_key_without_keys_names_no_git_host_step(tmp_path: Path) -> None:
    """Nothing made anywhere: the skipped environment is named, no step at the git host."""
    api = FakeApi()
    api.on("POST", _ROUTE, status=201, json=_answer(keys=[]))

    result = invoke(make_state(tmp_path, api), "repo", "deploy-key", "analytics")

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "staging" in result.stderr
    assert "git host" not in result.stderr and "lakana repo test" not in result.stderr


def test_cli_deploy_key_refuses_an_answer_without_its_fields(tmp_path: Path) -> None:
    """An answer that lacks the keys is not read as a deploy key; the error names the URL."""
    api = FakeApi()
    api.on("POST", _ROUTE, status=201, json={"secret": "analytics-deploy-key"})

    result = invoke(make_state(tmp_path, api), "repo", "deploy-key", "analytics")

    assert result.exit_code != 0
    assert "answer to a deploy key was not what a Lakana server sends" in " ".join(
        result.output.split()
    )
