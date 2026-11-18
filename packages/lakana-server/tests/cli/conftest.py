"""Fixtures for driving the real app through the CLI in-process."""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import typer
import typer.main
import typer.testing
from fastapi.testclient import TestClient
from scripts.test_support.cli_env import clean_cli_env
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from typer.testing import CliRunner, Result

import lakana
from lakana.cli._state import CliState
from lakana.cli.app import build_app
from lakana_core.grants.authorization import GrantAuthorization
from lakana_worker.deploy import DeploySettings
from lakana_worker.poller import poll_once

ADMIN_TOKEN = "lakana_pat_testkey_testsecret"


def make_cli_state(client: TestClient, tmp_path: Path, token: str = ADMIN_TOKEN) -> CliState:
    """Build a `CliState` whose SDK clients talk to the in-process app.

    Parameters
    ----------
    client
        The running app's `TestClient` (an `httpx.Client`).
    tmp_path
        Where the profile file lives.
    token
        The credential ordinary commands use.

    Returns
    -------
    CliState
        The state.
    """
    return CliState(
        client_factory=lambda state: lakana.Client(
            url="http://testserver", token=token, http_client=client
        ),
        login_client_factory=lambda url, tok: lakana.Client(url=url, token=tok, http_client=client),
        is_interactive=lambda: False,
        config_path=tmp_path / "lakana" / "config.toml",
    )


@pytest.fixture
def cli_state(client: TestClient, tmp_path: Path) -> CliState:
    """Provide an admin `CliState` against the in-process app.

    Parameters
    ----------
    client
        The running app.
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    CliState
        The state.
    """
    return make_cli_state(client, tmp_path)


@functools.cache
def _app() -> typer.Typer:
    """Build the CLI app once per process.

    Typer resolves the type hints of every command each time it turns an app into a click
    command, which took about 0.13 s per invocation under coverage, most of a CLI test's time
    (the CLI unit tests share their app for the same reason, #101). Nothing reads state from
    the app or its command between invocations: the state comes in through `obj`, the
    environment through `env`.

    Returns
    -------
    typer.Typer
        The shared app.
    """
    return build_app()


@functools.cache
def _command() -> Any:  # ruff: ignore[any-type]
    """Turn the shared app into its command, once per process.

    Returns
    -------
    Any
        The root group (Typer's own vendored click type, which it does not export).
    """
    return typer.main.get_command(_app())


# Pay the conversion once while the conftest is imported, a one-off cost of the process like
# the imports, rather than in whichever CLI test happens to run first.
_command()


def run_cli(
    state: CliState, *args: str, input: str | None = None, env: dict[str, str] | None = None
) -> Result:
    """Run one CLI command in-process with a clean environment, on the shared command.

    Parameters
    ----------
    state
        The state object the root callback fills in.
    *args
        The command line, without the program name.
    input
        Text fed to stdin, if any.
    env
        Environment variables to set on top of the clean environment.

    Returns
    -------
    typer.testing.Result
        Exit code, stdout, stderr and any exception.
    """
    clean = clean_cli_env(env)
    # `CliRunner.invoke` turns the app into a command on every call: hand it the cached one.
    with mock.patch.object(typer.testing, "_get_command", lambda _app_: _command()):
        return CliRunner().invoke(_app(), list(args), obj=state, input=input, env=clean)


@pytest.fixture
def invoke_cli() -> Callable[..., Result]:
    """Provide a function that runs one CLI command in-process.

    Returns
    -------
    callable
        `invoke_cli(state, *args, input=None) -> Result` (`run_cli`).
    """
    return run_cli


@pytest.fixture
def worker(client: TestClient) -> Iterator[None]:
    """Run a worker in a background thread for the test's duration.

    `lakana sql` and `lakana deploy` submit and then poll; this thread claims and
    executes queued query and deploy runs, as a real `lakana-worker` process would.

    Parameters
    ----------
    client
        The running app, for its database engine.

    Yields
    ------
    None
        While the worker runs.
    """
    engine = client.app.state.engine
    stop = threading.Event()
    errors: list[BaseException] = []

    def loop() -> None:
        """Repeatedly claim and run one queued query, backing off when idle or locked.

        Any exception that escapes an iteration ends the loop and is
        recorded in `errors`, rather than dying silently on this daemon
        thread — a thread that dies unnoticed would otherwise surface only
        as a confusing CLI query timeout, with no indication a worker
        exception was the actual cause.

        Raises
        ------
        OperationalError
            Re-raised internally when `poll_once` fails for a reason other
            than SQLite lock contention with the test-request thread; caught
            by this function's own outer handler, never propagated to the
            caller.
        """
        try:
            while not stop.is_set():
                try:
                    with Session(engine) as session:
                        claimed = poll_once(
                            session,
                            GrantAuthorization(session),
                            deploy=DeploySettings(
                                repo_roots=tuple(client.app.state.settings.repo_roots)
                            ),
                        )
                except OperationalError as exc:
                    # SQLite serializes writers; the test-request thread and
                    # this worker thread share one file-backed engine, so a
                    # "database is locked" collision is expected under
                    # concurrency, not a real failure. Any other
                    # OperationalError is a genuine bug and should still
                    # fail the test (via the outer `except` below).
                    if "database is locked" not in str(exc):
                        raise
                    claimed = False
                if not claimed:
                    stop.wait(0.05)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    if errors:
        raise errors[0]
