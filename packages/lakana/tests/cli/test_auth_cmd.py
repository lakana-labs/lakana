"""Tests for `lakana login`, `logout`, and `whoami`."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from conftest import TOKEN, FakeApi, invoke, make_state

from lakana._config import resolve_config

_WHOAMI = {
    "name": "alice",
    "type": "USER",
    "task_token": None,
    "ci_token": None,
    "language": None,
}
_ENVS: list[dict[str, object]] = [
    {
        "name": "dev",
        "storage_root": "file:///d",
        "storage_secret_ref": None,
        "is_protected": False,
        "properties": {},
    },
    {
        "name": "prod",
        "storage_root": "file:///p",
        "storage_secret_ref": None,
        "is_protected": True,
        "properties": {},
    },
]


def _api() -> FakeApi:
    """Build a fake API that knows `whoami` and `environments`.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    api.on("GET", "/api/v1/whoami", json=_WHOAMI)
    api.on("GET", "/api/v1/environments", json=_ENVS)
    return api


def test_login_with_token_stdin_verifies_then_writes_the_profile(tmp_path: Path) -> None:
    """CI path: `--url --token-stdin --env` writes a profile after whoami succeeds."""
    state = make_state(tmp_path, _api())

    result = invoke(
        state, "login", "--url", "http://p", "--token-stdin", "--env", "dev", input=TOKEN + "\n"
    )

    assert result.exit_code == 0, result.stderr
    assert "Logged in as alice (USER) at http://p" in result.stdout
    data = tomllib.loads(state.profile_path().read_text())
    assert data["default_profile"] == "default"
    assert data["profiles"]["default"] == {
        "url": "http://p",
        "token": TOKEN,
        "default_environment": "dev",
    }


def test_login_never_prints_the_token(tmp_path: Path) -> None:
    """Neither stdout nor stderr contains the token's secret part."""
    state = make_state(tmp_path, _api())

    result = invoke(state, "login", "--url", "http://p", "--token-stdin", input=TOKEN + "\n")

    assert "supersecret" not in result.stdout + result.stderr


def test_login_prompts_for_url_and_hidden_token_and_preselects_dev(tmp_path: Path) -> None:
    """Interactive path: URL and token prompts, then `dev` offered as the default environment."""
    state = make_state(tmp_path, _api(), interactive=True)

    result = invoke(state, "login", input=f"http://p\n{TOKEN}\n\n")

    assert result.exit_code == 0, result.stderr
    assert "supersecret" not in result.stdout
    profile = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]
    assert profile["default_environment"] == "dev"


def test_login_never_preselects_a_protected_environment(tmp_path: Path) -> None:
    """With only `prod` (protected) visible, nothing is preselected."""
    api = _api()
    api.on("GET", "/api/v1/environments", json=[_ENVS[1]])
    state = make_state(tmp_path, api, interactive=True)

    result = invoke(state, "login", input=f"http://p\n{TOKEN}\n\n")

    assert result.exit_code == 0, result.stderr
    profile = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]
    assert "default_environment" not in profile


def _task_token_api(environment: str, environments: list[dict[str, object]]) -> FakeApi:
    """Build a fake API where the credential is a task token for `environment`.

    Parameters
    ----------
    environment
        The task token's environment.
    environments
        The visible environments.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/whoami",
        json={
            "name": "bot",
            "type": "SERVICE_ACCOUNT",
            "task_token": {"environment": environment, "asset_scope": ["gold.*"]},
        },
    )
    api.on("GET", "/api/v1/environments", json=environments)
    return api


def _env(name: str, *, protected: bool) -> dict[str, object]:
    """Build one environment as the API lists it.

    Parameters
    ----------
    name
        The environment name.
    protected
        Its `is_protected` flag.

    Returns
    -------
    dict
        The environment's JSON.
    """
    return {
        "name": name,
        "storage_root": f"file:///{name}",
        "storage_secret_ref": None,
        "is_protected": protected,
        "properties": {},
    }


def test_login_preselects_a_task_tokens_own_environment(tmp_path: Path) -> None:
    """A task token's visible, unprotected environment is the natural default."""
    api = _task_token_api(
        "staging", [_env("dev", protected=False), _env("staging", protected=False)]
    )
    state = make_state(tmp_path, api, interactive=True)

    result = invoke(state, "login", input=f"http://p\n{TOKEN}\n\n")

    assert result.exit_code == 0, result.stderr
    assert "task token for staging" in result.stdout
    profile = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]
    assert profile["default_environment"] == "staging"


def test_login_never_preselects_a_task_tokens_protected_environment(tmp_path: Path) -> None:
    """Final review minor 1: a task token for protected `prod` preselects nothing, not even dev."""
    state = make_state(tmp_path, _task_token_api("prod", _ENVS), interactive=True)

    result = invoke(state, "login", input=f"http://p\n{TOKEN}\n\n")

    assert result.exit_code == 0, result.stderr
    profile = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]
    assert "default_environment" not in profile


def test_login_never_preselects_a_task_tokens_invisible_environment(tmp_path: Path) -> None:
    """Final review minor 1: a task token environment that isn't listed isn't preselected."""
    state = make_state(tmp_path, _task_token_api("hidden", _ENVS), interactive=True)

    result = invoke(state, "login", input=f"http://p\n{TOKEN}\n\n")

    assert result.exit_code == 0, result.stderr
    profile = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]
    assert "default_environment" not in profile


def test_login_honours_lakana_profile_for_the_profile_name(tmp_path: Path) -> None:
    """Final review minor 2: without `--profile`, `LAKANA_PROFILE` names the profile written."""
    state = make_state(tmp_path, _api())

    result = invoke(
        state,
        "login",
        "--url",
        "http://p",
        "--token-stdin",
        input=TOKEN + "\n",
        env={"LAKANA_PROFILE": "ci"},
    )

    assert result.exit_code == 0, result.stderr
    assert "saved profile 'ci'" in result.stdout
    assert set(tomllib.loads(state.profile_path().read_text())["profiles"]) == {"ci"}


def test_login_profile_flag_beats_lakana_profile(tmp_path: Path) -> None:
    """`login --profile` wins over the global `--profile`, which wins over `LAKANA_PROFILE`."""
    state = make_state(tmp_path, _api())

    local = invoke(
        state,
        "--profile",
        "global",
        "login",
        "--profile",
        "local",
        "--url",
        "http://p",
        "--token-stdin",
        input=TOKEN + "\n",
        env={"LAKANA_PROFILE": "ci"},
    )
    global_ = invoke(
        state,
        "--profile",
        "global",
        "login",
        "--url",
        "http://p",
        "--token-stdin",
        input=TOKEN + "\n",
        env={"LAKANA_PROFILE": "ci"},
    )

    assert local.exit_code == 0, local.stderr
    assert global_.exit_code == 0, global_.stderr
    assert set(tomllib.loads(state.profile_path().read_text())["profiles"]) == {"local", "global"}


def test_interactive_json_login_keeps_stdout_one_json_document(tmp_path: Path) -> None:
    """Final review minor 7: prompts and the environment list go to stderr, even in JSON mode."""
    state = make_state(tmp_path, _api(), interactive=True)

    result = invoke(state, "--output", "json", "login", input=f"http://p\n{TOKEN}\n\n")

    assert result.exit_code == 0, result.stderr
    # Click's test runner echoes typed input into stdout (a real terminal echoes
    # it to the tty instead); that echo — the URL, and blanks for the hidden
    # token and the accepted default — is all that may precede the document.
    echoed, brace, document = result.stdout.partition("{")
    assert echoed.split() == ["http://p"]
    assert json.loads(brace + document)["default_environment"] == "dev"
    assert "Environments: dev, prod" in result.stderr
    assert "Lakana URL" in result.stderr
    assert "Default environment" in result.stderr
    assert "Token" in result.stderr


def test_login_with_a_del_byte_in_the_task_tokens_environment_still_writes_a_readable_file(
    tmp_path: Path,
) -> None:
    """A server-supplied environment name containing DEL (0x7F) still round-trips.

    Accepted by pressing Enter at the environment prompt (the task token's own
    environment is preselected), this must not produce a profile file the SDK's
    own `resolve_config` then refuses to read.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    """
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/whoami",
        json={
            "name": "bot",
            "type": "SERVICE_ACCOUNT",
            "task_token": {"environment": "prod\x7f", "asset_scope": []},
        },
    )
    api.on("GET", "/api/v1/environments", json=[_env("prod\x7f", protected=False)])
    state = make_state(tmp_path, api, interactive=True)

    result = invoke(state, "login", input=f"http://p\n{TOKEN}\n\n")

    assert result.exit_code == 0, result.stderr
    config = resolve_config(config_path=state.profile_path(), environ={})
    assert config.url == "http://p"
    profile = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]
    assert profile["default_environment"] == "prod\x7f"


def test_login_rejects_an_unknown_environment_choice(tmp_path: Path) -> None:
    """Typing an environment that isn't visible is a usage error; nothing is written."""
    state = make_state(tmp_path, _api(), interactive=True)

    result = invoke(state, "login", input=f"http://p\n{TOKEN}\nstaging\n")

    assert result.exit_code == 2
    assert not state.profile_path().exists()


def test_a_rejected_token_writes_nothing(tmp_path: Path) -> None:
    """A 401 from whoami exits 4 and leaves no profile behind."""
    api = FakeApi()
    api.on("GET", "/api/v1/whoami", status=401, json={"detail": "Invalid credentials."})
    state = make_state(tmp_path, api)

    result = invoke(state, "login", "--url", "http://p", "--token-stdin", input=TOKEN + "\n")

    assert result.exit_code == 4
    assert not state.profile_path().exists()
    assert "supersecret" not in result.stdout + result.stderr


def test_token_stdin_requires_url(tmp_path: Path) -> None:
    """`--token-stdin` without `--url` is a usage error (stdin can't also answer a prompt)."""
    result = invoke(make_state(tmp_path, _api()), "login", "--token-stdin", input=TOKEN + "\n")

    assert result.exit_code == 2


def test_an_empty_token_is_refused(tmp_path: Path) -> None:
    """A blank line on stdin is not a token."""
    result = invoke(
        make_state(tmp_path, _api()), "login", "--url", "http://p", "--token-stdin", input="\n"
    )

    assert result.exit_code == 2


def test_login_to_a_named_profile_keeps_the_existing_default(tmp_path: Path) -> None:
    """A second login under another name doesn't steal `default_profile`."""
    state = make_state(tmp_path, _api())
    invoke(state, "login", "--url", "http://p", "--token-stdin", input=TOKEN + "\n")

    result = invoke(
        state,
        "login",
        "--profile",
        "work",
        "--url",
        "http://w",
        "--token-stdin",
        input=TOKEN + "\n",
    )

    assert result.exit_code == 0, result.stderr
    data = tomllib.loads(state.profile_path().read_text())
    assert data["default_profile"] == "default"
    assert set(data["profiles"]) == {"default", "work"}


def test_login_json_output_describes_the_session_without_the_token(tmp_path: Path) -> None:
    """`--output json login` prints profile, url, environment and whoami — no token."""
    state = make_state(tmp_path, _api())

    result = invoke(
        state,
        "--output",
        "json",
        "login",
        "--url",
        "http://p",
        "--token-stdin",
        "--env",
        "dev",
        input=TOKEN + "\n",
    )

    assert json.loads(result.stdout) == {
        "profile": "default",
        "url": "http://p",
        "default_environment": "dev",
        "whoami": _WHOAMI,
    }


def test_login_with_no_visible_environments_sets_no_default(tmp_path: Path) -> None:
    """Nothing is preselected, and no `Environments:` line is printed, when none exist."""
    api = FakeApi()
    api.on("GET", "/api/v1/whoami", json=_WHOAMI)
    api.on("GET", "/api/v1/environments", json=[])
    state = make_state(tmp_path, api, interactive=True)

    result = invoke(state, "login", input=f"http://p\n{TOKEN}\n\n")

    assert result.exit_code == 0, result.stderr
    assert "Environments:" not in result.stdout + result.stderr
    profile = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]
    assert "default_environment" not in profile


def test_login_interactive_with_url_given_only_prompts_for_the_token(tmp_path: Path) -> None:
    """Passing `--url` skips the URL prompt; only the token and environment are asked for."""
    state = make_state(tmp_path, _api(), interactive=True)

    result = invoke(state, "login", "--url", "http://p", input=f"{TOKEN}\n\n")

    assert result.exit_code == 0, result.stderr
    data = tomllib.loads(state.profile_path().read_text())
    assert data["profiles"]["default"]["url"] == "http://p"


def test_login_preselects_the_only_unprotected_environment(tmp_path: Path) -> None:
    """A single unprotected, non-`dev` environment is still the natural default."""
    api = FakeApi()
    api.on("GET", "/api/v1/whoami", json=_WHOAMI)
    api.on(
        "GET",
        "/api/v1/environments",
        json=[
            {
                "name": "staging",
                "storage_root": "file:///s",
                "storage_secret_ref": None,
                "is_protected": False,
                "properties": {},
            }
        ],
    )
    state = make_state(tmp_path, api, interactive=True)

    result = invoke(state, "login", input=f"http://p\n{TOKEN}\n\n")

    assert result.exit_code == 0, result.stderr
    profile = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]
    assert profile["default_environment"] == "staging"


def test_logout_removes_the_profile(tmp_path: Path) -> None:
    """`logout` deletes the active profile."""
    state = make_state(tmp_path, _api())
    invoke(state, "login", "--url", "http://p", "--token-stdin", input=TOKEN + "\n")

    result = invoke(state, "logout")

    assert result.exit_code == 0, result.stderr
    assert tomllib.loads(state.profile_path().read_text()) == {}


def test_logout_with_no_active_profile_is_a_usage_error(tmp_path: Path) -> None:
    """`logout` with nothing selected and no `default_profile` is a usage error."""
    state = make_state(tmp_path, _api())

    result = invoke(state, "logout")

    assert result.exit_code == 2


def test_whoami_renders_table_and_json(tmp_path: Path) -> None:
    """`whoami` shows the caller in both output modes."""
    state = make_state(tmp_path, _api())

    table = invoke(state, "whoami")
    as_json = invoke(state, "--output", "json", "whoami")

    assert table.exit_code == 0, table.stderr
    assert "alice" in table.stdout
    assert json.loads(as_json.stdout) == _WHOAMI
