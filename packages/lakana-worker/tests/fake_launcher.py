"""A scripted stand-in for `LauncherClient`, for the DockerRunner, environment and check tests."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lakana_worker.launcher_client import LauncherRefused, LauncherUnavailable

Script = Callable[[dict[str, Any], Callable[[bytes], None]], dict[str, object]]
"""What `call` does for one request: emit logs, write files, return the result."""


@dataclass
class FakeLauncher:
    """Answers `call` from a script per operation and records every request.

    Parameters
    ----------
    scripts
        Operation to its script.
    requests
        Every request, in order.
    stopped
        Ids `stop` was called with.
    released
        Set by `stop`; a script may wait on it.
    socket_path
        The socket the real client would use.
    timeouts
        The `timeout` of every `call`, in order.
    owners
        The owner of every `stop`, in order.
    """

    scripts: dict[str, Script] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    released: threading.Event = field(default_factory=threading.Event)
    socket_path: Path = Path("/run/lakana-launcher/launcher.sock")
    timeouts: list[float | None] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)

    def call(
        self,
        request: dict[str, Any],
        on_log: Callable[[bytes], None] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """Run the operation's script.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.
        timeout
            The caller's deadline, recorded; the script decides what happens.

        Returns
        -------
        dict
            The script's result.

        Raises
        ------
        LauncherUnavailable
            When no script is set for the operation.
        """
        self.requests.append(request)
        self.timeouts.append(timeout)
        script = self.scripts.get(str(request["op"]))
        if script is None:
            raise LauncherUnavailable(f"runner unavailable: no script for {request['op']}")
        return script(request, on_log or (lambda chunk: None))

    def status(self, python: str, key: str) -> bool:
        """Ask the status script, as `LauncherClient.status` does.

        Parameters
        ----------
        python
            The Python version.
        key
            The environment key.

        Returns
        -------
        bool
            Whether the script says complete.
        """
        answer = self.call({"op": "status", "python": python, "environment": key})
        return answer.get("complete") is True

    def check(
        self, fields: Mapping[str, object], *, timeout: float = 60.0
    ) -> tuple[dict[str, object], bytes]:
        """Run the check script, collecting its output as `LauncherClient.check` does.

        Parameters
        ----------
        fields
            The request's fields without ``op``.
        timeout
            The caller's deadline, recorded.

        Returns
        -------
        tuple
            The script's result and everything it emitted.
        """
        output = bytearray()
        result = self.call({"op": "check", **fields}, output.extend, timeout=timeout)
        return result, bytes(output)

    def stop(self, ident: str, owner: str) -> bool:
        """Record a stop and release a waiting script.

        Parameters
        ----------
        ident
            The task's id.
        owner
            The worker that asks, recorded in `owners`.

        Returns
        -------
        bool
            `True`.
        """
        self.stopped.append(ident)
        self.owners.append(owner)
        self.released.set()
        return True


def refusing(reason: str) -> Script:
    """Build a script that refuses.

    Parameters
    ----------
    reason
        The refusal.

    Returns
    -------
    Script
        It raises `LauncherRefused`.
    """

    def script(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        """Refuse the request.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.

        Raises
        ------
        LauncherRefused
            Always.
        """
        raise LauncherRefused(reason)

    return script
