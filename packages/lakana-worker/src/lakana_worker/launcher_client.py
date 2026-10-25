"""The worker's side of the launcher's socket.

One request per connection, the launcher's token added from the file next to the socket. Every
failure to reach the launcher, and every Docker failure it reports, becomes
`LauncherUnavailable` with the task's message: "runner unavailable", the launcher service and
`LAKANA_RUN__LAUNCHER_SOCKET`. A refusal is `LauncherRefused`: the request itself was wrong.
Whatever the launcher answers is read as untrusted input: an answer outside the protocol (not
JSON, nested too deeply, a log that is not base64, a line longer than 1 MiB) is the launcher
being unavailable, never another exception.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import fcntl
import json
import os
import re
import secrets
import socket
import stat
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from lakana_worker.launcher.server import TOKEN_FILE

_MAX_LINE = 2 << 20
"""Longest answer line: the launcher sends log pieces of up to 1 MiB, ~1.4 MB as base64 JSON."""
_MAX_TOKEN_BYTES = 4096
_UNSAFE_OWNER = re.compile(r"[^A-Za-z0-9._:-]")
_NOT_PROTOCOL = "the launcher answered something that is not its protocol"
_OWNER_FILE = "launcher-owner"
_OWNER_SLOTS = 16
"""How many workers sharing one cache directory each keep an owner of their own."""
_OWNER_TOKEN = re.compile(r"\A[0-9a-f]{16}\Z")
_HOST_PART = 110
"""Longest host part of an owner: with ``.`` and the token it stays within the launcher's 128."""
_CLAIMS: list[int] = []
"""Descriptors of the owner files this process holds locked, open until it ends."""
CHECK_WAIT_SECONDS = 60.0
"""How long a worker waits for a connection check: its 30 seconds, a relay's 10 and a margin."""
_MAX_CHECK_OUTPUT = 64 * 1024
_TRANSIENT_REFUSALS = ("the launcher is busy:", "no request line arrived in time")
"""Refusals that say the launcher could not take the request now, not that it was wrong."""


class LauncherUnavailable(Exception):
    """The launcher cannot be reached, or Docker failed; the text is the task's message."""


class LauncherRefused(Exception):
    """The launcher refused the request; the text is its reason."""


RELEASE_SKEW = (
    "The worker and the launcher are different releases, so they disagree on the request's "
    "fields: an operator upgrades both to the same release (docker compose pull, then up -d)"
)
"""What a refusal of a request's field set means, whichever of the two is the older."""


def release_skew(reason: str) -> bool:
    """Say whether a refusal is about the request's field set, not its values.

    Parameters
    ----------
    reason
        The launcher's refusal; a launcher of an earlier release words it the same way.

    Returns
    -------
    bool
        Whether it reads ``<op> takes exactly the fields …``.
    """
    return "takes exactly the fields" in reason


class LauncherTimedOut(Exception):
    """No result arrived before the caller's deadline; the connection was closed.

    Closing it is what makes the launcher remove the container: its watcher takes a closed
    connection for a worker that is gone.
    """


class LogSinkFailed(Exception):
    """The caller's log sink raised while output arrived."""


def unavailable_message(detail: str, socket_path: Path) -> str:
    """Write the "runner unavailable" message.

    Parameters
    ----------
    detail
        What went wrong.
    socket_path
        `LAKANA_RUN__LAUNCHER_SOCKET`.

    Returns
    -------
    str
        The cause and the next step.
    """
    return (
        f"runner unavailable: {detail}. Check that the launcher service is running (docker "
        f"compose ps launcher) and that LAKANA_RUN__LAUNCHER_SOCKET ({socket_path}) is the "
        "socket in its volume."
    )


def launcher_owner(host: str) -> str:
    """Make a host name safe for the launcher's owner label: the host part of `claim_owner`.

    Parameters
    ----------
    host
        `socket.gethostname()`.

    Returns
    -------
    str
        Letters, digits, ``.``, ``_``, ``:`` and ``-``, starting with a letter or digit, at most
        128 characters.
    """
    owner = _UNSAFE_OWNER.sub("-", host)[:127]
    if not owner:
        return "worker"
    return owner if owner[0].isalnum() else f"w{owner}"


def _left(deadline: float | None) -> float | None:
    """Turn a deadline into the timeout of the next read.

    Parameters
    ----------
    deadline
        A `time.monotonic` moment, or `None`.

    Returns
    -------
    float or None
        Seconds until then, at least a millisecond; `None` without a deadline.
    """
    return None if deadline is None else max(deadline - time.monotonic(), 0.001)


def _claim_slot(path: Path) -> str | None:
    """Lock one owner file for this process and read its token, writing one if it has none.

    Parameters
    ----------
    path
        ``launcher-owner`` or ``launcher-owner.<n>`` in the worker's cache directory.

    Returns
    -------
    str or None
        The token; `None` when another live worker holds the file, or it is a link or not a
        regular file (never followed or used).

    Raises
    ------
    OSError
        If the file cannot be created, read or written.
    """
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        raise
    claimed = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        token = os.read(descriptor, 64).decode("ascii", errors="replace").strip()
        if _OWNER_TOKEN.match(token) is None:
            token = secrets.token_hex(8)
            os.ftruncate(descriptor, 0)
            os.pwrite(descriptor, token.encode("ascii"), 0)
            os.fsync(descriptor)
        _CLAIMS.append(descriptor)
        claimed = True
        return token
    finally:
        if not claimed:
            os.close(descriptor)


def claim_owner(directory: Path, host: str) -> str:
    """Name this worker for the launcher's labels: unique among live workers, kept on restart.

    The owner is ``<host>.<token>``: the host name made safe (`launcher_owner`) and a random
    token kept in ``launcher-owner`` in the worker's cache directory. The worker holds a lock on
    that file for as long as it runs, so a second live worker sharing the directory takes
    ``launcher-owner.1`` and so on, and two workers that share only a host name have tokens of
    their own: the `cleanup` one asks for at its start never removes the other's tasks. A worker
    restarted on the same directory takes a free file again and, with it, an earlier process's
    leftovers to remove; whatever owner held a free file has no live process. Without a usable
    directory, or with every file held, the token is random: unique, but nothing an earlier
    process left is found by it (the launcher's deadline still removes that).

    Parameters
    ----------
    directory
        The worker's cache directory (`python_support.worker_cache_dir`).
    host
        `socket.gethostname()`.

    Returns
    -------
    str
        Within the launcher's rule for an owner: letters, digits, ``.``, ``_``, ``:`` and ``-``,
        at most 128 characters.
    """
    prefix = launcher_owner(host)[:_HOST_PART]
    with contextlib.suppress(OSError):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for slot in range(_OWNER_SLOTS):
            token = _claim_slot(directory / (f"{_OWNER_FILE}.{slot}" if slot else _OWNER_FILE))
            if token is not None:
                return f"{prefix}.{token}"
    return f"{prefix}.{secrets.token_hex(8)}"


class LauncherClient:
    """Sends requests to the launcher.

    Parameters
    ----------
    socket_path
        `LAKANA_RUN__LAUNCHER_SOCKET`.
    connect_timeout
        Seconds to connect and to send the request; reading the answers has no timeout, since
        a task may be silent for as long as it runs (the launcher ends it at its timeout).
    """

    def __init__(self, socket_path: Path, *, connect_timeout: float = 10.0) -> None:
        self.socket_path = socket_path
        self.connect_timeout = connect_timeout

    def _unavailable(self, detail: str) -> LauncherUnavailable:
        """Build the exception for a failure to reach the launcher.

        Parameters
        ----------
        detail
            What went wrong.

        Returns
        -------
        LauncherUnavailable
            With `unavailable_message`.
        """
        return LauncherUnavailable(unavailable_message(detail, self.socket_path))

    def _token(self) -> str:
        """Read the launcher's token from the file next to its socket.

        Returns
        -------
        str
            The token.

        Raises
        ------
        LauncherUnavailable
            If the file cannot be read or is not a short ASCII text.
        """
        try:
            with (self.socket_path.parent / TOKEN_FILE).open("rb") as handle:
                data = handle.read(_MAX_TOKEN_BYTES + 1)
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise self._unavailable(f"the launcher's token cannot be read ({reason})") from None
        if len(data) > _MAX_TOKEN_BYTES or not data.isascii():
            raise self._unavailable(
                "the launcher's token cannot be read (not the launcher's token)"
            )
        return data.decode("ascii").strip()

    def call(
        self,
        request: dict[str, object],
        on_log: Callable[[bytes], None] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """Send one request and read its answers to the result.

        Parameters
        ----------
        request
            The request without the token.
        on_log
            Receives each log chunk; `None` drops them.
        timeout
            Seconds from the call to the result, however much output arrives meanwhile;
            `None` waits as long as the launcher keeps the connection open.

        Returns
        -------
        dict
            The result line's value.

        Raises
        ------
        LauncherUnavailable
            If the token or the socket cannot be reached, the connection breaks, the answer is
            not the protocol, or Docker failed.
        LauncherRefused
            If the launcher refused the request; a busy launcher or a request line that came
            too late is `LauncherUnavailable` instead, since a retry may succeed.
        LauncherTimedOut
            If `timeout` passed first; the connection is closed.
        LogSinkFailed
            If `on_log` raised.
        ValueError
            Never escapes: an answer outside the protocol becomes `LauncherUnavailable`.
        TimeoutError
            Never escapes: a deadline that passed becomes `LauncherTimedOut`.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        token = self._token()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        reading = False
        try:
            connection.settimeout(self.connect_timeout)
            connection.connect(str(self.socket_path))
            connection.sendall(json.dumps({**request, "token": token}).encode("utf-8") + b"\n")
            reading = True
            connection.settimeout(_left(deadline))
            with connection.makefile("rb") as reader:
                while line := reader.readline(_MAX_LINE):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError  # handled below, as a read that timed out
                    connection.settimeout(_left(deadline))
                    answer = json.loads(line)
                    if not isinstance(answer, dict):
                        raise ValueError(_NOT_PROTOCOL)
                    if "log" in answer:
                        chunk = base64.b64decode(str(answer["log"]), validate=True)
                        if on_log is not None:
                            try:
                                on_log(chunk)
                            except Exception as exc:  # the caller's sink, reported as such
                                raise LogSinkFailed("the log sink failed") from exc
                    elif "result" in answer and isinstance(answer["result"], dict):
                        return answer["result"]
                    elif "refused" in answer:
                        reason = str(answer["refused"])
                        if reason.startswith(_TRANSIENT_REFUSALS):
                            raise LauncherUnavailable(f"runner unavailable: {reason}")
                        raise LauncherRefused(reason)
                    elif "error" in answer:
                        raise self._unavailable(f"Docker failed: {answer['error']}")
                    else:
                        raise ValueError(_NOT_PROTOCOL)
        except OSError as exc:
            if reading and deadline is not None and isinstance(exc, TimeoutError):
                raise LauncherTimedOut(
                    f"the launcher sent no result within {timeout:g}s; the connection is closed"
                ) from None
            reason = exc.strerror or type(exc).__name__
            raise self._unavailable(f"the launcher's socket cannot be reached ({reason})") from None
        except (ValueError, binascii.Error, RecursionError):
            raise self._unavailable(_NOT_PROTOCOL) from None
        finally:
            connection.close()
        raise self._unavailable("the launcher closed the connection without an answer")

    def stop(self, ident: str, owner: str) -> bool:
        """Ask the launcher to stop a task or build this worker started.

        Parameters
        ----------
        ident
            Its id.
        owner
            This worker (`claim_owner`); the launcher stops only runs started under it.

        Returns
        -------
        bool
            Whether it was running; `False` too for an id the launcher has no run of this owner
            under.
        """
        return self.call({"op": "stop", "id": ident, "owner": owner}).get("stopped") is True

    def check(
        self, fields: Mapping[str, object], *, timeout: float = CHECK_WAIT_SECONDS
    ) -> tuple[dict[str, object], bytes]:
        """Run one connection check and collect what it printed.

        Parameters
        ----------
        fields
            The ``check`` request's fields without ``op`` and the token.
        timeout
            Seconds to the result: the check's own 30 seconds, a relay's start and a margin.

        Returns
        -------
        tuple
            The launcher's result (``exit``, ``timed_out`` and so on) and the check's output,
            at most its first 64 KiB.

        Raises
        ------
        LauncherUnavailable
            As `call`.
        LauncherRefused
            As `call`.
        LauncherTimedOut
            As `call`.
        """
        output = bytearray()

        def collect(chunk: bytes) -> None:
            """Keep the first 64 KiB of the check's output.

            Parameters
            ----------
            chunk
                A piece of it.
            """
            output.extend(chunk[: max(_MAX_CHECK_OUTPUT - len(output), 0)])

        result = self.call({"op": "check", **fields}, collect, timeout=timeout)
        return result, bytes(output)

    def status(self, python: str, key: str) -> bool:
        """Ask whether an environment is built.

        Parameters
        ----------
        python
            The Python version.
        key
            The environment key.

        Returns
        -------
        bool
            Whether it is complete.
        """
        answer = self.call({"op": "status", "python": python, "environment": key})
        return answer.get("complete") is True

    def cleanup(self, owner: str) -> int:
        """Remove what this worker left behind before it restarted.

        Parameters
        ----------
        owner
            `launcher_owner`'s answer.

        Returns
        -------
        int
            How many containers were removed.
        """
        removed = self.call({"op": "cleanup", "owner": owner}).get("removed")
        return removed if type(removed) is int else 0
