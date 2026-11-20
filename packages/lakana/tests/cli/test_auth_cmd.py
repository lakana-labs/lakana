"""Tests for `lakana login`, `logout`, and `whoami`."""

from __future__ import annotations

import http.client
import json
import threading
import tomllib
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from conftest import TOKEN, FakeApi, invoke, make_state

from lakana._config import resolve_config
from lakana._pkce import s256
from lakana.cli._state import CliState

_WHOAMI = {
    "name": "alice",
    "type": "USER",
    "task_token": None,
    "ci_token": None,
    "language": None,
    "token_window_ends_at": None,
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


def test_cli_whoami_shows_the_window(tmp_path: Path) -> None:
    """A linked person's `whoami` says when their tokens' window ends."""
    api = FakeApi()
    api.on(
        "GET", "/api/v1/whoami", json={**_WHOAMI, "token_window_ends_at": "2026-12-20T09:00:00Z"}
    )
    result = invoke(make_state(tmp_path, api), "whoami")
    assert result.exit_code == 0, result.stderr
    assert "token_window_ends_at" in result.stdout and "2026-12-20" in result.stdout


# -- through the browser (#32, PR 3) ------------------------------------------------------------

_ISSUED = {"key_id": "k2", "name": "lakana login on laptop-7", "token": TOKEN}
_TODAY = "Logged in as alice (USER) at http://p; saved profile 'default'\n"
"""`lakana login --token-stdin`'s output before the browser sign-in, copied from `origin/main`."""
_TODAY_PROMPTED = (" \n" + _TODAY, "Token:")
"""Its output and error output after the hidden prompt, copied from `origin/main`."""


def _oidc_api(*, enabled: bool = True) -> FakeApi:
    """Build a fake API with the OIDC info route and the exchange.

    Parameters
    ----------
    enabled
        What the info route says.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = _api()
    info = {"enabled": enabled, "display_name": "Keycloak" if enabled else None}
    api.on("GET", "/api/v1/sign-in/oidc", json=info)
    api.on("POST", "/api/v1/auth/cli/token", json=_ISSUED)
    return api


def _send(port: int, query: dict[str, str]) -> None:
    """Send the browser's request to the listener.

    Parameters
    ----------
    port
        The listener's port.
    query
        The query parameters.
    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", "/?" + urlencode(query))
    connection.getresponse().read()
    connection.close()


class _Browser:
    """A browser that answers the loopback listener from a thread.

    Parameters
    ----------
    answer
        The query to send back besides ``state``; `None` sends nothing.
    state
        The ``state`` to send; the one in the URL by default.
    """

    def __init__(self, answer: dict[str, str] | None, *, state: str | None = None) -> None:
        self.answer = answer
        self.state = state
        self.opened: list[str] = []
        self.threads: list[threading.Thread] = []

    def __call__(self, url: str) -> bool:
        """Open a URL.

        Parameters
        ----------
        url
            The page.

        Returns
        -------
        bool
            `True`.
        """
        self.opened.append(url)
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        if self.answer is not None:
            sent = {**self.answer, "state": self.state or query["state"]}
            thread = threading.Thread(target=_send, args=(int(query["port"]), sent))
            thread.start()
            self.threads.append(thread)
        return True

    def query(self) -> dict[str, str]:
        """Read the query of the page opened.

        Returns
        -------
        dict of str to str
            Its parameters.
        """
        for thread in self.threads:
            thread.join()
        return {k: v[0] for k, v in parse_qs(urlsplit(self.opened[0]).query).items()}


def _browser_state(tmp_path: Path, api: FakeApi, browser: _Browser) -> CliState:
    """Build a state whose browser is `browser` and whose computer is `laptop-7`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    api
        The fake API.
    browser
        The fake browser.

    Returns
    -------
    CliState
        The state.
    """
    state = make_state(tmp_path, api)
    state.browser_opener = browser
    state.hostname = lambda: "laptop-7"
    state.login_timeout = 5.0
    return state


def test_with_oidc_login_opens_the_browser_and_saves_the_profile(tmp_path: Path) -> None:
    api = _oidc_api()
    browser = _Browser({"code": "c0de"})
    state = _browser_state(tmp_path, api, browser)

    result = invoke(state, "login", "--url", "http://p")

    assert result.exit_code == 0, result.stderr
    assert result.stdout == _TODAY
    query = browser.query()
    assert browser.opened[0].startswith("http://p/sign-in/cli?")
    assert query["host"] == "laptop-7"
    assert f"Opening your browser to sign in to http://p…\n{browser.opened[0]}\n" in result.stderr
    exchange = next(r for r in api.requests if r.url.path == "/api/v1/auth/cli/token")
    sent = json.loads(exchange.content)
    assert sent["code"] == "c0de"
    assert s256(sent["code_verifier"]) == query["challenge"]
    assert "authorization" not in exchange.headers
    profile = tomllib.loads(state.profile_path().read_text())["profiles"]["default"]
    assert profile == {"url": "http://p", "token": TOKEN}


def test_browser_flag_opens_the_browser_without_oidc(tmp_path: Path) -> None:
    api = _oidc_api(enabled=False)
    browser = _Browser({"code": "c0de"})

    result = invoke(
        _browser_state(tmp_path, api, browser), "login", "--url", "http://p", "--browser"
    )

    assert result.exit_code == 0, result.stderr
    assert len(browser.opened) == 1


def test_without_oidc_login_prompts_as_today(tmp_path: Path) -> None:
    browser = _Browser(None)
    state = _browser_state(tmp_path, _oidc_api(enabled=False), browser)

    result = invoke(state, "login", "--url", "http://p", input=f"{TOKEN}\n")

    assert result.exit_code == 0, result.stderr
    assert (result.stdout, result.stderr) == _TODAY_PROMPTED
    assert browser.opened == []


@pytest.mark.parametrize("status", [404, 409, 421])
def test_older_server_without_the_info_route_prompts(tmp_path: Path, status: int) -> None:
    api = _api()
    api.on("GET", "/api/v1/sign-in/oidc", status=status, json={"detail": "no"})
    browser = _Browser(None)

    result = invoke(
        _browser_state(tmp_path, api, browser), "login", "--url", "http://p", input=f"{TOKEN}\n"
    )

    assert result.exit_code == 0, result.stderr
    assert (result.stdout, result.stderr) == _TODAY_PROMPTED
    assert browser.opened == []


def test_another_refusal_of_the_info_route_is_an_error(tmp_path: Path) -> None:
    api = _api()
    api.on("GET", "/api/v1/sign-in/oidc", status=500, json={"detail": "broken"})

    result = invoke(make_state(tmp_path, api), "login", "--url", "http://p", input=f"{TOKEN}\n")

    assert result.exit_code != 0
    assert "broken" in result.stderr


def test_token_stdin_is_unchanged(tmp_path: Path) -> None:
    api = _oidc_api()
    browser = _Browser(None)

    result = invoke(
        _browser_state(tmp_path, api, browser),
        "login",
        "--url",
        "http://p",
        "--token-stdin",
        input=TOKEN + "\n",
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout == _TODAY
    assert browser.opened == []
    assert all(r.url.path != "/api/v1/sign-in/oidc" for r in api.requests)


def test_paste_prompts_on_an_oidc_instance(tmp_path: Path) -> None:
    browser = _Browser(None)
    state = _browser_state(tmp_path, _oidc_api(), browser)

    result = invoke(state, "login", "--url", "http://p", "--paste", input=f"{TOKEN}\n")

    assert result.exit_code == 0, result.stderr
    assert (result.stdout, result.stderr) == _TODAY_PROMPTED
    assert browser.opened == []


def test_the_sign_in_flags_exclude_each_other(tmp_path: Path) -> None:
    result = invoke(
        make_state(tmp_path, _api()), "login", "--url", "http://p", "--paste", "--device"
    )

    assert result.exit_code == 2
    assert "Choose one of --token-stdin, --paste, --device and --browser" in result.stderr


def test_cancel_in_the_browser_exits_1_with_a_sentence(tmp_path: Path) -> None:
    state = _browser_state(tmp_path, _oidc_api(), _Browser({"error": "access_denied"}))

    result = invoke(state, "login", "--url", "http://p")

    assert result.exit_code == 1
    assert "The sign-in was cancelled in the browser, so no token was created." in result.stderr
    assert not state.profile_path().exists()


def test_wrong_state_is_refused(tmp_path: Path) -> None:
    browser = _Browser({"code": "evil"}, state="not-this-one")
    state = _browser_state(tmp_path, _oidc_api(), browser)
    state.login_timeout = 0.3

    result = invoke(state, "login", "--url", "http://p")

    browser.query()
    assert result.exit_code == 1
    assert not state.profile_path().exists()


def test_loopback_timeout_names_device_and_paste(tmp_path: Path) -> None:
    state = _browser_state(tmp_path, _oidc_api(), _Browser(None))
    state.login_timeout = 0.05

    result = invoke(state, "login", "--url", "http://p")

    assert result.exit_code == 1
    assert "lakana login --device" in result.stderr
    assert "lakana login --paste" in result.stderr


def _device_api(*polls: dict[str, object] | int) -> FakeApi:
    """Build a fake API for `login --device` whose polls answer in turn.

    Parameters
    ----------
    *polls
        Each poll's body, or 429 for ``slow_down``.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = _api()
    api.on(
        "POST",
        "/api/v1/auth/cli/device",
        json={
            "device_code": "dev1ce",
            "user_code": "BCDF-GHJK",
            "verification_url": "http://p/sign-in/device",
            "expires_in": 600,
            "interval": 5,
        },
    )
    answers = iter(polls)

    def poll(request: httpx.Request) -> httpx.Response:
        answer = next(answers)
        if answer == 429:
            return httpx.Response(
                429, json={"detail": "slow", "type": "CliPollTooFast", "error": "slow_down"}
            )
        return httpx.Response(200, json=answer)

    api.routes["POST", "/api/v1/auth/cli/device/token"] = poll
    return api


def test_device_prints_url_and_code_and_saves_the_profile(tmp_path: Path) -> None:
    api = _device_api(
        {"state": "pending", "token": None}, 429, {"state": "approved", "token": _ISSUED}
    )
    state = _browser_state(tmp_path, api, _Browser(None))
    waits: list[float] = []
    state.sleep = waits.append

    result = invoke(state, "login", "--url", "http://p", "--device")

    assert result.exit_code == 0, result.stderr
    assert result.stdout == _TODAY
    assert "open http://p/sign-in/device in a browser and enter the code BCDF-GHJK" in result.stderr
    assert "Waiting for you to confirm… (Ctrl+C to stop)" in result.stderr
    assert waits == [5.0, 5.0, 10.0]
    start = next(r for r in api.requests if r.url.path == "/api/v1/auth/cli/device")
    assert json.loads(start.content) == {"host": "laptop-7"}
    assert "dev1ce" not in result.stderr + result.stdout


@pytest.mark.parametrize(
    ("state_name", "sentence"),
    [
        ("denied", "The sign-in was refused in the browser"),
        ("expired", "The code expired before it was confirmed"),
    ],
)
def test_device_ends_on_a_refusal_or_expiry(tmp_path: Path, state_name: str, sentence: str) -> None:
    state = _browser_state(
        tmp_path, _device_api({"state": state_name, "token": None}), _Browser(None)
    )
    state.sleep = lambda seconds: None

    result = invoke(state, "login", "--url", "http://p", "--device")

    assert result.exit_code == 1
    assert sentence in result.stderr


def test_the_token_is_never_printed(tmp_path: Path) -> None:
    for output in ("table", "json"):
        state = _browser_state(tmp_path / output, _oidc_api(), _Browser({"code": "c0de"}))
        result = invoke(state, "--output", output, "login", "--url", "http://p")
        assert result.exit_code == 0, result.stderr
        assert "supersecret" not in result.stdout + result.stderr
        assert "c0de" not in result.stdout + result.stderr
    device = _browser_state(
        tmp_path / "device", _device_api({"state": "approved", "token": _ISSUED}), _Browser(None)
    )
    device.sleep = lambda seconds: None
    result = invoke(device, "--output", "json", "login", "--url", "http://p", "--device")
    assert result.exit_code == 0, result.stderr
    assert "supersecret" not in result.stdout + result.stderr


def test_the_default_browser_opener_is_the_webbrowser_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    assert CliState().browser_opener("http://p/sign-in/cli") is True
    assert opened == ["http://p/sign-in/cli"]
