"""The message for a server the CLI could not reach: which one, why, and what to do next.

The SDK says only "Could not reach <url>: ConnectError". A person with two instances (one on
their own machine under the profile `local`, a stopped Compose bundle under `team`) learns
from that neither which profile was used nor how to choose the other, so the CLI names the
profile, the operating system's reason, and the commands that choose another profile.
"""

from __future__ import annotations

import contextlib
import os
import socket
from typing import Any

from lakana.cli import _profiles
from lakana.cli._state import CliState
from lakana.errors import ConfigError, LakanaConnectionError

_CHECK = "Check the URL and that the server is running."


def _reason(exc: LakanaConnectionError) -> str:
    """Say in words why the connection failed, from the chain of causes.

    Parameters
    ----------
    exc
        The SDK's error; its cause is httpx's error, whose cause is the operating system's.

    Returns
    -------
    str
        "connection refused" or "host name not found", else the name of httpx's error.
    """
    cause: BaseException | None = exc.__cause__
    while cause is not None:
        if isinstance(cause, ConnectionRefusedError):
            return "connection refused"
        if isinstance(cause, socket.gaierror):
            return "host name not found"
        cause = cause.__cause__ or cause.__context__
    return type(exc.__cause__ or exc).__name__


def unreachable_message(state: CliState, exc: LakanaConnectionError) -> str:
    """Write the message for a server that could not be reached, with the next step in it.

    The next step is part of the message, not a hint line, so `--output json` carries it too.

    Parameters
    ----------
    state
        The CLI state, for `--url`, `--profile` and the profile file.
    exc
        The SDK's error.

    Returns
    -------
    str
        The server named by where its URL came from (`--url`, `LAKANA_URL`, or the profile
        in effect, with the other profiles to choose instead), the reason, and the next step.
    """
    reason = _reason(exc)
    for url, source in ((state.url, "--url"), (os.environ.get("LAKANA_URL"), "LAKANA_URL")):
        if url:
            return f"Could not reach {url} (from {source}) ({reason}). {_CHECK}"
    data: dict[str, Any] = {}
    with contextlib.suppress(ConfigError):
        data = _profiles.load(state.profile_path())
    profiles: dict[str, Any] = data.get("profiles", {})
    name = _profiles.active_profile_name(data, state.profile)
    url = profiles.get(name, {}).get("url") if name is not None else None
    if url is None:
        return f"{exc} ({reason}). {_CHECK}"
    others = sorted(other for other in profiles if other != name)
    found = f"Could not reach profile {name!r} at {url} ({reason})."
    if not others:
        return (
            f"{found} Start that instance, or add a profile for another one with "
            "lakana login --profile NAME --url URL."
        )
    other = others[0]
    return (
        f"{found} Start that instance, or use another profile: lakana --profile {other} "
        f"COMMAND, or make it the default with lakana profile use {other} "
        "(all profiles: lakana profile list)."
    )
