"""Helpers for the CLI unit tests: a fake platform API and a CLI invoker."""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
import typer
import typer.main
import typer.testing
from typer.testing import CliRunner, Result

import lakana
from lakana.cli._state import CliState
from lakana.cli.app import build_app

TOKEN = "lakana_pat_k1_supersecret"
"""The token every fake client authenticates with; `supersecret` must never be printed."""

BASE_URL = "http://lakana.test"


@dataclass
class FakeApi:
    """A table of canned responses keyed by `(method, path)`, recording every request.

    Parameters
    ----------
    routes
        Canned responses; each value builds a fresh `httpx.Response`.
    requests
        Every request received, in order.
    """

    routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = field(
        default_factory=dict
    )
    requests: list[httpx.Request] = field(default_factory=list)

    def on(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        json: Any = None,  # ruff: ignore[any-type]
        content: bytes | None = None,
    ) -> None:
        """Register a canned response.

        Parameters
        ----------
        method
            HTTP method, e.g. `"GET"`.
        path
            Request path, e.g. `"/api/v1/environments"`.
        status
            Response status code.
        json
            A JSON body, if any.
        content
            A raw body, if any (used instead of `json`).
        """

        def respond(request: httpx.Request) -> httpx.Response:
            if content is not None:
                return httpx.Response(status, content=content)
            if json is None:
                return httpx.Response(status)
            return httpx.Response(status, json=json)

        self.routes[method, path] = respond

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer a request from the table, or with a 404 naming what was missing.

        Parameters
        ----------
        request
            The incoming request.

        Returns
        -------
        httpx.Response
            The canned response.
        """
        self.requests.append(request)
        respond = self.routes.get((request.method, request.url.path))
        if respond is None:
            return httpx.Response(
                404,
                json={"detail": f"no fake for {request.method} {request.url.path}"},
            )
        return respond(request)

    def client(self, url: str = BASE_URL, token: str = TOKEN) -> lakana.Client:
        """Build an SDK client that talks to this fake.

        Parameters
        ----------
        url
            The base URL the client is configured with.
        token
            The bearer token the client sends.

        Returns
        -------
        lakana.Client
            A client over `httpx.MockTransport(self.handle)`.
        """
        http = httpx.Client(transport=httpx.MockTransport(self.handle), base_url=url)
        return lakana.Client(url=url, token=token, http_client=http)


def make_state(tmp_path: Path, api: FakeApi, *, interactive: bool = False) -> CliState:
    """Build a `CliState` wired to `api`, with its profile file under `tmp_path`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    api
        The fake platform API.
    interactive
        What `CliState.is_interactive` reports.

    Returns
    -------
    CliState
        The state to pass as `obj=` to the CLI.
    """
    return CliState(
        client_factory=lambda state: api.client(),
        login_client_factory=lambda url, token: api.client(url, token),
        is_interactive=lambda: interactive,
        config_path=tmp_path / "lakana" / "config.toml",
    )


@functools.cache
def _app() -> typer.Typer:
    """Build the CLI app once per process.

    Typer resolves the type hints of every command each time it turns an app into a click
    command, and with ``from __future__ import annotations`` each resolution compiles a
    snippet (about 1.7 million code objects over the unit suite, #101). Nothing reads state
    from the app or its command between invocations: the state comes in through `obj`, the
    environment through `env`. A test that needs an app of its own (a changed registrar, a
    patched command) calls `build_app` itself and runs it with its own runner.

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


def invoke(
    state: CliState, *args: str, input: str | None = None, env: dict[str, str] | None = None
) -> Result:
    """Run the CLI in-process with a clean environment.

    Parameters
    ----------
    state
        The state object the root callback fills in.
    *args
        The command line, without the program name.
    input
        Text fed to stdin (prompts, `--token-stdin`, `-`).
    env
        Environment variables to set on top of the clean environment,
        e.g. `{"LAKANA_PROFILE": "ci"}`.

    Returns
    -------
    typer.testing.Result
        Exit code, stdout, stderr and any exception.
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
            "LAKANA_OIDC_AUDIENCE",
            "ACTIONS_ID_TOKEN_REQUEST_URL",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        )
    }
    clean["COLUMNS"] = "200"
    # Plain output regardless of the caller's terminal settings (CI, FORCE_COLOR). `NO_COLOR`
    # removes colours but not bold; a dumb terminal removes every style, and GITHUB_ACTIONS
    # would otherwise make the help renderer treat a self-hosted runner's TERM as a terminal.
    clean.update(
        {
            "FORCE_COLOR": None,
            "PY_COLORS": None,
            "GITHUB_ACTIONS": None,
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
    )
    clean.update(env or {})
    # `CliRunner.invoke` turns the app into a command on every call: hand it the cached one.
    with mock.patch.object(typer.testing, "_get_command", lambda _app_: _command()):
        return CliRunner().invoke(_app(), list(args), obj=state, input=input, env=clean)


def write_profile_file(path: Path, text: str) -> None:
    """Write a profile file with the permissions the SDK requires.

    Parameters
    ----------
    path
        Where to write it; parent directories are created.
    text
        The TOML content.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text)
    os.chmod(path, 0o600)
