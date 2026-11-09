"""Tests for the CLI's root app: global options, exit codes, and shared helpers."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import conftest
import pytest
import typer
import typer.testing
from conftest import FakeApi, invoke, make_state, write_profile_file
from typer.testing import CliRunner

from lakana.cli import _profiles, _state
from lakana.cli import app as _app_module
from lakana.cli._state import (
    CliError,
    CliState,
    confirm,
    parse_key_values,
    resolve_environment,
    split_table_name,
)
from lakana.cli.app import EXIT_CODES, build_app
from lakana.errors import (
    ArtifactCorrupt,
    AuthenticationError,
    ConfigError,
    InvalidResponse,
    LakanaError,
    NotFound,
)


def _app_raising(exc: Exception) -> typer.Typer:
    """Build the real app plus one test command that raises `exc`.

    Parameters
    ----------
    exc
        The exception the `boom` command raises.

    Returns
    -------
    typer.Typer
        The app.
    """
    app = build_app()

    @app.command("boom")
    def boom() -> None:
        """Raise the configured exception."""
        raise exc

    return app


def _run(app: typer.Typer, *args: str, state: CliState | None = None) -> typer.testing.Result:
    """Invoke `app` with a clean environment.

    Parameters
    ----------
    app
        The app to run.
    *args
        The command line.
    state
        The state object, if any.

    Returns
    -------
    typer.testing.Result
        The result.
    """
    return CliRunner().invoke(
        app,
        list(args),
        obj=state or CliState(),
        env={
            "LAKANA_OUTPUT": None,
            "COLUMNS": "200",
            "FORCE_COLOR": None,
            "PY_COLORS": None,
            "NO_COLOR": "1",
        },
    )


@pytest.mark.parametrize(("exc_type", "code"), EXIT_CODES)
def test_each_sdk_error_maps_to_its_exit_code(
    exc_type: type[LakanaError], code: int, tmp_path: Path
) -> None:
    """Every SDK error type exits with its documented code and an `Error:` line."""
    # An empty profile file: a connection error names the profile in effect, never the
    # developer's own.
    state = CliState(config_path=tmp_path / "config.toml")
    result = _run(_app_raising(exc_type("boom")), "boom", state=state)

    assert result.exit_code == code
    assert "Error: boom" in result.stderr
    assert result.stdout == ""


def test_json_mode_reports_errors_as_json_on_stderr() -> None:
    """`--output json` turns the error into `{"error": {type, message}}` on stderr."""
    result = _run(_app_raising(NotFound("no such table")), "--output", "json", "boom")

    assert result.exit_code == 6
    assert json.loads(result.stderr) == {"error": {"type": "NotFound", "message": "no such table"}}


def test_authentication_error_hints_at_login() -> None:
    """A 401 suggests `lakana login`."""
    result = _run(_app_raising(AuthenticationError("bad token")), "boom")

    assert result.exit_code == 4
    assert "lakana login" in result.stderr


def test_cli_error_uses_its_own_exit_code() -> None:
    """A `CliError` exits with the code it carries."""
    result = _run(_app_raising(CliError("bad file", 8, kind="FileError")), "boom")

    assert result.exit_code == 8
    assert "Error: bad file" in result.stderr


def test_a_query_timeout_exits_9_with_a_hint() -> None:
    """The SDK's builtin `TimeoutError` from `QueryRun.wait` is exit 9 (plan ruling 3)."""
    result = _run(_app_raising(TimeoutError("Query q1 still RUNNING after 5s")), "boom")

    assert result.exit_code == 9
    assert "lakana query status" in result.stderr


def test_an_unexpected_error_exits_1_without_a_traceback() -> None:
    """A bug is exit 1; the traceback appears only with `--verbose`."""
    quiet = _run(_app_raising(RuntimeError("kaboom")), "boom")
    loud = _run(_app_raising(RuntimeError("kaboom")), "--verbose", "boom")

    assert quiet.exit_code == 1
    assert "Traceback" not in quiet.stderr
    assert "--verbose" in quiet.stderr
    assert loud.exit_code == 1
    assert "Traceback" in loud.stderr


def test_usage_errors_keep_exit_code_2() -> None:
    """An unknown command and a bad `--output` value are usage errors, not bugs."""
    assert _run(build_app(), "nosuchcommand").exit_code == 2
    assert _run(build_app(), "--output", "yaml", "whoami").exit_code == 2


def test_global_options_are_stored_on_the_state() -> None:
    """The root callback copies global options onto the injected state."""
    app = build_app()
    seen: list[CliState] = []

    @app.command("peek")
    def peek(ctx: typer.Context) -> None:
        """Record the state.

        Parameters
        ----------
        ctx
            The Typer context.
        """
        seen.append(_state.state_of(ctx))

    result = _run(
        app, "--profile", "work", "--url", "http://u", "--output", "json", "--verbose", "peek"
    )

    assert result.exit_code == 0, result.stderr
    assert (seen[0].profile, seen[0].url, seen[0].output, seen[0].verbose) == (
        "work",
        "http://u",
        "json",
        True,
    )


def test_output_mode_can_come_from_the_environment() -> None:
    """`LAKANA_OUTPUT=json` selects JSON mode."""
    result = CliRunner().invoke(
        _app_raising(NotFound("x")), ["boom"], obj=CliState(), env={"LAKANA_OUTPUT": "json"}
    )

    assert json.loads(result.stderr)["error"]["type"] == "NotFound"


def test_state_builds_the_client_once_and_closes_it(tmp_path: Path) -> None:
    """`CliState.client()` caches the factory's client; `close()` closes it."""
    api = FakeApi()
    built: list[object] = []
    state = make_state(tmp_path, api)
    state.client_factory = lambda s: built.append(1) or api.client()

    first = state.client()
    assert state.client() is first
    state.close()

    assert built == [1]


def _work_profile(tmp_path: Path, url: str = "https://lakana.example.com/api") -> Path:
    """Write a profile file whose default profile `work` points at `url`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    url
        The profile's platform URL.

    Returns
    -------
    Path
        The profile file.
    """
    path = tmp_path / "lakana" / "config.toml"
    write_profile_file(
        path,
        f'default_profile = "work"\n[profiles.work]\nurl = "{url}"\ntoken = "lakana_pat_k_s"\n',
    )
    return path


@pytest.fixture
def no_env_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear `LAKANA_URL`, `LAKANA_TOKEN` and `LAKANA_PROFILE` for the test.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch fixture.
    """
    for name in ("LAKANA_URL", "LAKANA_TOKEN", "LAKANA_PROFILE"):
        monkeypatch.delenv(name, raising=False)


def test_default_client_factory_uses_the_profile_without_url_override(
    tmp_path: Path, no_env_credentials: None
) -> None:
    """The production factory resolves credentials from the profile like the SDK."""
    state = CliState(config_path=_work_profile(tmp_path))

    client = _state.default_client_factory(state)

    assert client._config.url == "https://lakana.example.com/api"
    assert client._config.token == "lakana_pat_k_s"
    client.close()


@pytest.mark.parametrize(
    "override",
    [
        "https://evil.example.com/api",
        "http://lakana.example.com/api",
        "https://lakana.example.com:8443/api",
        "https://lakana.example.com:notaport/api",
        "https://lakana.example.com:0/api",
        "https://lakana.example.com@evil.example.com/api",
    ],
    ids=["host", "scheme", "port", "bad-port", "port-zero", "userinfo"],
)
def test_default_client_factory_refuses_a_url_off_the_profile_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_env_credentials: None,
    override: str,
) -> None:
    """Final review I1: `--url` may not carry the profile's token to another origin."""
    built: list[object] = []
    monkeypatch.setattr(_state.lakana, "Client", lambda **kwargs: built.append(kwargs))
    state = CliState(config_path=_work_profile(tmp_path), url=override)

    with pytest.raises(ConfigError) as caught:
        _state.default_client_factory(state)

    assert str(caught.value) == (
        f"--url {override} doesn't match profile 'work' (https://lakana.example.com/api); "
        "refusing to send its token elsewhere. Use LAKANA_URL/LAKANA_TOKEN or "
        f"`lakana login --profile NAME --url {override}`."
    )
    assert built == []


def test_the_cli_refuses_a_foreign_url_with_exit_3_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env_credentials: None
) -> None:
    """Final review I1, end to end: exit 3, the token never leaves, nothing is requested."""
    built: list[object] = []
    monkeypatch.setattr(_state.lakana, "Client", lambda **kwargs: built.append(kwargs))
    state = CliState(config_path=_work_profile(tmp_path))

    result = CliRunner().invoke(
        build_app(), ["--url", "https://evil.example.com", "whoami"], obj=state
    )

    assert result.exit_code == 3
    assert "refusing to send its token elsewhere" in result.stderr
    assert "lakana_pat_k_s" not in result.stderr + result.stdout
    assert built == []


@pytest.mark.parametrize(
    ("profile_url", "override"),
    [
        ("https://lakana.example.com/api", "https://LAKANA.example.com/other/path"),
        ("https://lakana.example.com/api", "https://lakana.example.com:443/api"),
        ("http://lakana.example.com:80", "http://lakana.example.com/api"),
    ],
    ids=["path", "explicit-default-port", "implicit-default-port"],
)
def test_default_client_factory_allows_a_url_on_the_profile_origin(
    tmp_path: Path, no_env_credentials: None, profile_url: str, override: str
) -> None:
    """Same scheme, host and effective port: only the path differs, so `--url` is used."""
    state = CliState(config_path=_work_profile(tmp_path, profile_url), url=override)

    client = _state.default_client_factory(state)

    assert client._config.url == override
    assert client._config.token == "lakana_pat_k_s"
    client.close()


def test_default_client_factory_lets_url_override_env_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `LAKANA_URL`/`LAKANA_TOKEN` supplying the token, `--url` may point anywhere."""
    monkeypatch.setenv("LAKANA_URL", "https://a.example.com")
    monkeypatch.setenv("LAKANA_TOKEN", "lakana_pat_env_s")
    state = CliState(config_path=_work_profile(tmp_path), url="https://b.example.com")

    client = _state.default_client_factory(state)

    assert client._config.url == "https://b.example.com"
    assert client._config.token == "lakana_pat_env_s"
    client.close()


def test_resolve_environment_prefers_the_flag(tmp_path: Path) -> None:
    """An explicit `--env` always wins."""
    assert resolve_environment(make_state(tmp_path, FakeApi()), "prod") == "prod"


def test_resolve_environment_falls_back_to_the_profile_default(
    tmp_path: Path, no_env_credentials: None
) -> None:
    """Without `--env`, the active profile's `default_environment` is used."""
    state = make_state(tmp_path, FakeApi())
    write_profile_file(
        state.profile_path(),
        'default_profile = "p"\n[profiles.p]\nurl = "u"\ntoken = "t"\n'
        'default_environment = "dev"\n',
    )

    assert resolve_environment(state, None) == "dev"


def test_resolve_environment_without_any_default_is_a_usage_error(tmp_path: Path) -> None:
    """No flag and no profile default: exit 2 with the documented message."""
    with pytest.raises(CliError) as caught:
        resolve_environment(make_state(tmp_path, FakeApi()), None)

    assert caught.value.exit_code == 2
    assert str(caught.value) == (
        "No environment: pass --env or run 'lakana profile use PROFILE --env ENV'"
    )


def test_resolve_environment_ignores_the_profile_default_with_env_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review minor 3: with LAKANA_URL/LAKANA_TOKEN credentials the profile isn't in play."""
    state = make_state(tmp_path, FakeApi())
    write_profile_file(
        state.profile_path(),
        'default_profile = "p"\n[profiles.p]\nurl = "u"\ntoken = "t"\n'
        'default_environment = "prod"\n',
    )
    monkeypatch.setenv("LAKANA_URL", "https://other.example.com")
    monkeypatch.setenv("LAKANA_TOKEN", "lakana_pat_env_s")

    with pytest.raises(CliError) as caught:
        resolve_environment(state, None)

    assert caught.value.exit_code == 2
    assert str(caught.value).startswith("No environment: pass --env")


def test_resolve_environment_ignores_the_profile_default_with_only_lakana_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Either variable alone means the environment, not the profile, supplies credentials."""
    state = make_state(tmp_path, FakeApi())
    write_profile_file(
        state.profile_path(),
        'default_profile = "p"\n[profiles.p]\nurl = "u"\ntoken = "t"\n'
        'default_environment = "prod"\n',
    )
    monkeypatch.delenv("LAKANA_URL", raising=False)
    monkeypatch.setenv("LAKANA_TOKEN", "lakana_pat_env_s")

    with pytest.raises(CliError):
        resolve_environment(state, None)


def test_confirm_passes_with_yes_and_refuses_without_a_tty(tmp_path: Path) -> None:
    """`--yes` skips the prompt; no TTY and no `--yes` refuses with exit 2."""
    confirm(make_state(tmp_path, FakeApi()), "Delete it?", yes=True)

    with pytest.raises(CliError) as caught:
        confirm(make_state(tmp_path, FakeApi()), "Delete it?", yes=False)

    assert caught.value.exit_code == 2
    assert "--yes" in str(caught.value)


def test_parse_key_values_splits_on_the_first_equals_sign() -> None:
    """`k=v=w` keeps `v=w` as the value; a missing `=` is a usage error."""
    assert parse_key_values(["a=1", "b=x=y"], "--option") == {"a": "1", "b": "x=y"}
    assert parse_key_values(None, "--option") == {}
    with pytest.raises(CliError):
        parse_key_values(["novalue"], "--option")
    with pytest.raises(CliError):
        parse_key_values(["=v"], "--option")


def test_parse_key_values_sensitive_never_echoes_the_offending_item() -> None:
    """`sensitive=True` names the item's position instead of its text."""
    assert parse_key_values(["a=1"], "--value", sensitive=True) == {"a": "1"}

    with pytest.raises(CliError) as missing_equals:
        parse_key_values(["AKIAHIDDEN"], "--value", sensitive=True)
    assert "AKIAHIDDEN" not in str(missing_equals.value)
    assert "--value #1" in str(missing_equals.value)

    with pytest.raises(CliError) as empty_key:
        parse_key_values(["=AKIAHIDDEN"], "--value", sensitive=True)
    assert "AKIAHIDDEN" not in str(empty_key.value)
    assert "--value #1" in str(empty_key.value)


def test_split_table_name_splits_at_the_last_dot() -> None:
    """A qualified name splits into `(namespace, table)` at the final dot."""
    assert split_table_name("bronze.sales") == ("bronze", "sales")
    assert split_table_name("bronze.eu.sales") == ("bronze.eu", "sales")


def test_split_table_name_without_a_dot_is_a_usage_error() -> None:
    """A name with no dot (or an empty namespace/table) is exit 2."""
    with pytest.raises(CliError) as caught:
        split_table_name("sales")

    assert caught.value.exit_code == 2


def test_confirm_accepts_an_interactive_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a TTY and no `--yes`, an accepted prompt does not raise."""
    monkeypatch.setattr(_state.typer, "confirm", lambda *args, **kwargs: True)

    confirm(make_state(tmp_path, FakeApi(), interactive=True), "Delete it?", yes=False)


def test_confirm_raises_on_an_interactive_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a TTY and no `--yes`, a declined prompt is a usage error."""
    monkeypatch.setattr(_state.typer, "confirm", lambda *args, **kwargs: False)

    with pytest.raises(CliError) as caught:
        confirm(make_state(tmp_path, FakeApi(), interactive=True), "Delete it?", yes=False)

    assert caught.value.exit_code == 2
    assert "Aborted" in str(caught.value)


def test_login_client_factory_builds_a_client_from_explicit_credentials() -> None:
    """`_login_client` (the default `login_client_factory`) builds a bare client."""
    client = _state._login_client("http://a", "lakana_pat_k_s")

    assert client._config.url == "http://a"
    client.close()


def test_stdin_is_tty_default_reflects_the_real_stdin() -> None:
    """`_stdin_is_tty` (the default `is_interactive`) delegates to `sys.stdin.isatty`."""
    assert _state._stdin_is_tty() == sys.stdin.isatty()


def test_profiles_load_rejects_invalid_toml(tmp_path: Path) -> None:
    """A profile file that isn't valid TOML is a `ConfigError`."""
    path = tmp_path / "lakana" / "config.toml"
    write_profile_file(path, "not valid toml [[[")

    with pytest.raises(ConfigError):
        _profiles.load(path)


def test_exit_code_for_an_unlisted_lakana_error_is_1() -> None:
    """A bare `LakanaError`, matching no `EXIT_CODES` entry, exits 1."""
    assert _app_module.exit_code_for(LakanaError("boom")) == 1


def test_the_errors_that_exit_1_are_named_in_the_readme_table() -> None:
    """A corrupt download and an unreadable answer exit 1, and the table says so."""
    readme = Path(__file__).resolve().parents[2] / "README.md"
    (row,) = [
        line
        for line in readme.read_text().splitlines()
        if line.startswith("| 1 | Unexpected error")
    ]
    for error in (ArtifactCorrupt, InvalidResponse):
        assert _app_module.exit_code_for(error("x")) == 1
        assert error.__name__ in row


def test_hint_suggests_login_for_a_missing_credentials_config_error() -> None:
    """A `ConfigError` about missing credentials hints at `lakana login` too."""
    assert _app_module._hint(ConfigError("No credentials found.")) == "run 'lakana login'"


def test_build_app_calls_every_registrar(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_app` passes the fresh app to each registered command-group hook."""
    seen: list[typer.Typer] = []
    monkeypatch.setattr(_app_module, "_REGISTRARS", (seen.append,))

    built = build_app()

    assert seen == [built]


def test_main_runs_the_built_app_under_the_lakana_program_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main()` (the console script) invokes the module-level app and exits cleanly."""
    monkeypatch.setattr(sys, "argv", ["lakana", "--help"])

    with pytest.raises(SystemExit) as caught:
        _app_module.main()

    assert caught.value.code == 0


def test_importing_the_sdk_does_not_load_the_cli_stack() -> None:
    """`import lakana` stays light: neither `typer` nor `lakana.cli` is loaded.

    `rich` is deliberately not asserted here: httpx's own optional CLI
    (`httpx._main`) imports `rich` unconditionally whenever it's installed,
    regardless of anything this SDK does, so `rich` ending up in
    `sys.modules` reflects httpx's own behaviour, not a CLI-stack leak from
    `lakana` itself.
    """
    code = (
        "import sys, lakana\n"
        "cli_loaded = any(\n"
        "    m == 'lakana.cli' or m.startswith('lakana.cli.') for m in sys.modules\n"
        ")\n"
        "print('typer' in sys.modules or cli_loaded)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout

    assert out.strip() == "False"


def test_the_lakana_console_script_points_at_main() -> None:
    """`[project.scripts] lakana` resolves to `lakana.cli.app:main`."""
    [script] = [ep for ep in entry_points(group="console_scripts") if ep.name == "lakana"]

    assert script.value == "lakana.cli.app:main"


@pytest.mark.parametrize(
    "args",
    [("nosuchcommand",), ("env", "get"), ("env", "create", "dev", "--bogus")],
)
def test_usage_errors_use_the_json_envelope_in_json_mode(args: tuple[str, ...]) -> None:
    """Under `--output json`, Typer's own usage errors print `{"error": …}` on stderr."""
    result = _run(build_app(), "--output", "json", *args)

    assert result.exit_code == 2
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "UsageError"
    assert error["message"]


def test_usage_errors_stay_plain_text_in_table_mode() -> None:
    """Table mode keeps Typer's normal usage message."""
    result = _run(build_app(), "env", "get")

    assert result.exit_code == 2
    assert "Usage" in result.stderr


def test_confirm_shows_the_prompt_escaped(tmp_path: Path) -> None:
    """The names in a question are what the user typed; no escape may reach the screen."""
    with pytest.raises(CliError) as caught:
        confirm(make_state(tmp_path, FakeApi()), "Delete a\x1b[2Jb?", yes=False)
    assert "\x1b" not in str(caught.value) and "a\\x1b[2Jb" in str(caught.value)


def test_the_shared_invoker_builds_the_app_once_for_any_number_of_invocations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#101: Typer resolves every command's type hints each time it builds the click command.

    `conftest.invoke` therefore builds one command per process; a test that needs a fresh app
    (a changed registrar, say) builds its own with `build_app` and runs it itself.
    """
    built: list[int] = []
    real = conftest.build_app

    def counting() -> typer.Typer:
        built.append(1)
        return real()

    monkeypatch.setattr(conftest, "build_app", counting)
    first = invoke(make_state(tmp_path, FakeApi()), "--help")
    second = invoke(make_state(tmp_path, FakeApi()), "--help")
    third = invoke(make_state(tmp_path, FakeApi()), "whoami", "--help")

    assert (first.exit_code, second.exit_code, third.exit_code) == (0, 0, 0)
    assert len(built) <= 1
