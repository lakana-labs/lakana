"""`lakana login`, `lakana logout`, and `lakana whoami`."""

from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlencode

import typer

import lakana
from lakana._models import Whoami
from lakana._pkce import s256
from lakana.cli import _output, _profiles
from lakana.cli._loopback import LoopbackServer
from lakana.cli._state import CliError, CliState, state_of
from lakana.errors import LakanaError


@dataclass(frozen=True)
class LoginResult:
    """What `lakana login` reports; deliberately has no token field.

    Parameters
    ----------
    profile
        The profile written.
    url
        Its platform URL.
    default_environment
        The default environment chosen, if any.
    whoami
        Who the credential authenticates as.
    """

    profile: str
    url: str
    default_environment: str | None
    whoami: Whoami


def _choose_environment(state: CliState, client: lakana.Client, who: Whoami) -> str | None:
    """Offer the visible environments and return the chosen default, if any.

    Preselects a task token's own environment, else `dev`, else the only
    visible environment — but only one that is visible and unprotected, so
    a protected environment is never preselected. A task token whose own
    environment doesn't qualify preselects nothing: it can't act anywhere
    else. The list and prompt go to stderr, keeping stdout for the result.
    Without a TTY nothing is chosen.

    Parameters
    ----------
    state
        The CLI state.
    client
        A client with the verified credential.
    who
        The verified identity.

    Returns
    -------
    str or None
        The environment name, or `None` for no default.

    Raises
    ------
    CliError
        If the user types an environment that isn't visible (exit 2).
    """
    if not state.is_interactive():
        return None
    environments = client.environments.list()
    names = [env.name for env in environments]
    unprotected = [env.name for env in environments if not env.is_protected]
    if who.task_token is not None:
        own = who.task_token.environment
        preselected: str | None = own if own in unprotected else None
    elif "dev" in unprotected:
        preselected = "dev"
    elif len(environments) == 1 and unprotected:
        preselected = unprotected[0]
    else:
        preselected = None
    if names:
        sys.stderr.write("Environments: " + ", ".join(names) + "\n")
    choice = typer.prompt(
        "Default environment (blank for none)",
        default=preselected or "",
        show_default=True,
        err=True,
    ).strip()
    if not choice:
        return None
    if choice not in names and (who.task_token is None or choice != who.task_token.environment):
        raise CliError(f"Unknown environment {choice!r}; visible: {', '.join(names) or 'none'}")
    return choice


LOGIN_TIMEOUT_HINT = (
    "No answer came from the browser within 10 minutes. Run lakana login again; on a computer "
    "without a browser use lakana login --device, or paste a token with lakana login --paste."
)
"""The error of a loopback sign-in that timed out."""

NO_BROWSER = (
    "No browser could be opened here. Open the address above in a browser on this computer, or "
    "press Ctrl+C and run lakana login --device to confirm on another one."
)
"""What `lakana login` says when the browser did not start."""


def _uses_oidc(state: CliState, base_url: str) -> bool:
    """Say whether the server signs people in through an identity provider.

    Any answer but a clear yes keeps today's token prompt: an older server (404), the interface
    off (409), an address the server does not answer browsers at (421), a proxy's refusal, a
    server error or no answer at all.

    Parameters
    ----------
    state
        The CLI state.
    base_url
        The server.

    Returns
    -------
    bool
        `True` only when the server says so.
    """
    with state.login_client_factory(base_url, "") as client:
        try:
            return client.sign_in.oidc_info().enabled
        except LakanaError:
            return False


def _browser_login(state: CliState, base_url: str) -> str:
    """Sign in through the browser and a loopback redirect, and exchange the code.

    Parameters
    ----------
    state
        The CLI state.
    base_url
        The server.

    Returns
    -------
    str
        The new token.

    Raises
    ------
    CliError
        When the sign-in was cancelled in the browser, or no answer came in time (exit 1).
    """
    verifier = secrets.token_urlsafe(64)
    expected = secrets.token_urlsafe(32)
    with LoopbackServer(expected) as server:
        query = urlencode(
            {
                "port": server.port,
                "state": expected,
                "challenge": s256(verifier),
                "host": state.hostname(),
            }
        )
        page = f"{base_url.rstrip('/')}/sign-in/cli?{query}"
        sys.stderr.write(
            f"Opening your browser to sign in to {_output.inline(base_url)}…\n{page}\n"
        )
        # `webbrowser` raises its own `Error`, an `OSError` or whatever a launcher raises: in
        # every case the address is on the screen already, and the person is told what to do.
        try:
            opened = state.browser_opener(page)
        except Exception:
            opened = False
        if not opened:
            sys.stderr.write(NO_BROWSER + "\n")
        answer = server.wait(state.login_timeout)
    if answer is None:
        raise CliError(LOGIN_TIMEOUT_HINT, exit_code=1, kind="LoginTimeout")
    if answer.code is None:
        raise CliError(
            "The sign-in was cancelled in the browser, so no token was created.",
            exit_code=1,
            kind="LoginCancelled",
        )
    with state.login_client_factory(base_url, "") as client:
        return client.sign_in.exchange_code(answer.code, verifier).token


def _device_login(state: CliState, base_url: str) -> str:
    """Sign in by a code confirmed in a browser elsewhere, and wait for the token.

    Parameters
    ----------
    state
        The CLI state.
    base_url
        The server.

    Returns
    -------
    str
        The new token.

    Raises
    ------
    CliError
        When the sign-in was refused in the browser or the code expired (exit 1).
    """
    with state.login_client_factory(base_url, "") as client:
        started = client.sign_in.start_device(state.hostname())
        sys.stderr.write(
            f"To sign in, open {_output.inline(started.verification_url)} in a browser and enter "
            f"the code {_output.inline(started.user_code)}\n"
            "Waiting for you to confirm… (Ctrl+C to stop)\n"
        )
        interval = float(started.interval)
        while True:
            state.sleep(interval)
            polled = client.sign_in.poll_device(started.device_code)
            if polled.state == "slow_down":
                interval = max(interval + 5.0, polled.retry_after or 0.0)
            elif polled.state == "approved" and polled.token is not None:
                return polled.token.token
            elif polled.state == "denied":
                raise CliError(
                    "The sign-in was refused in the browser, so no token was created.",
                    exit_code=1,
                    kind="LoginCancelled",
                )
            elif polled.state != "pending":
                raise CliError(
                    "The code expired before it was confirmed. Run lakana login --device again.",
                    exit_code=1,
                    kind="LoginTimeout",
                )


def login(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile name to write.")] = None,
    url: Annotated[str | None, typer.Option("--url", help="Platform URL.")] = None,
    token_stdin: Annotated[
        bool, typer.Option("--token-stdin", help="Read the token from stdin (for CI).")
    ] = False,
    env: Annotated[str | None, typer.Option("--env", help="Default environment.")] = None,
    device: Annotated[
        bool,
        typer.Option(
            "--device", help="Sign in by a code confirmed in a browser on another computer."
        ),
    ] = False,
    browser: Annotated[
        bool,
        typer.Option(
            "--browser", help="Sign in through this computer's browser, with or without OIDC."
        ),
    ] = False,
    paste: Annotated[
        bool, typer.Option("--paste", help="Paste a token at a hidden prompt.")
    ] = False,
) -> None:
    """Sign in and save the credential as a profile.

    Against a server whose people sign in through an identity provider, opens the browser: you
    confirm there, and a new personal token named `lakana login on HOST`, HOST being this
    computer's name, is created for 90 days and saved. Anywhere else, and whenever stdin is not
    a terminal, asks for a token as before (`--paste`). On a computer without a browser, use
    `--device`: it shows an address and a short code to confirm in a browser elsewhere. The
    token is never printed.

    Parameters
    ----------
    ctx
        The Typer context.
    profile
        The profile to write; defaults to the global `--profile`, then
        `LAKANA_PROFILE`, then `default`.
    url
        The platform URL; prompted for when omitted.
    token_stdin
        Read the token from stdin instead of a hidden prompt.
    env
        The profile's default environment; chosen interactively when omitted.
    device
        Sign in by a code to confirm in a browser elsewhere, for a computer without one.
    browser
        Sign in through this computer's browser, also on a server without an identity
        provider.
    paste
        Ask for a token at a hidden prompt, also on a server with an identity provider.

    Raises
    ------
    CliError
        For more than one of `--token-stdin`, `--paste`, `--device` and `--browser`,
        `--token-stdin` without `--url`, or an empty token (exit 2); a sign-in cancelled,
        refused or timed out in the browser (exit 1).
    """
    state = state_of(ctx)
    if sum((token_stdin, paste, device, browser)) > 1:
        raise CliError("Choose one of --token-stdin, --paste, --device and --browser.")
    name = profile or state.profile or os.environ.get("LAKANA_PROFILE") or "default"
    base_url = url or state.url
    if token_stdin:
        if not base_url:
            raise CliError("--token-stdin needs --url (stdin can't also answer a prompt)")
        token = sys.stdin.readline().strip()
    else:
        if not base_url:
            base_url = typer.prompt("Lakana URL", err=True).strip()
        if device:
            token = _device_login(state, base_url)
        elif browser or (not paste and state.is_interactive() and _uses_oidc(state, base_url)):
            token = _browser_login(state, base_url)
        else:
            token = typer.prompt("Token", hide_input=True, err=True).strip()
    if not token:
        raise CliError("No token given.")
    with state.login_client_factory(base_url, token) as client:
        who = client.whoami()
        environment = env or _choose_environment(state, client, who)
    _profiles.set_profile(
        state.profile_path(), name, url=base_url, token=token, default_environment=environment
    )
    scope = (
        ""
        if who.task_token is None
        else f", task token for {_output.inline(who.task_token.environment)}"
    )
    _output.done(
        state,
        f"Logged in as {_output.inline(who.name)} ({who.type}{scope}) at "
        f"{_output.inline(base_url)}; saved profile {name!r}",
        LoginResult(profile=name, url=base_url, default_environment=environment, whoami=who),
    )


def logout(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile to remove.")] = None,
) -> None:
    """Remove a saved profile (and its token) from the profile file.

    Parameters
    ----------
    ctx
        The Typer context.
    profile
        The profile; defaults to the active one.

    Raises
    ------
    CliError
        If no profile is active or it doesn't exist (exit 2).
    """
    state = state_of(ctx)
    path = state.profile_path()
    name = _profiles.active_profile_name(_profiles.load(path), profile or state.profile)
    if name is None:
        raise CliError("No profile selected; pass --profile NAME")
    _profiles.remove_profile(path, name)
    _output.done(state, f"Removed profile {name!r}")


def whoami(ctx: typer.Context) -> None:
    """Show who the configured credential authenticates as.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().whoami())


def register(app: typer.Typer) -> None:
    """Add `login`, `logout` and `whoami` to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.command("login")(login)
    app.command("logout")(logout)
    app.command("whoami")(whoami)
