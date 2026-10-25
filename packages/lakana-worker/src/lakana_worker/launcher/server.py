"""The launcher's Unix socket: one request per connection, authenticated by a shared token.

The socket and the token file sit in a volume only the worker mounts. The directory belongs to
the worker's group with the set-group-id bit, so both files belong to that group without the
launcher needing a capability to change owners, and with the sticky bit, so the worker can
neither rename nor delete them. Both files get their mode while they are being created (the
umask for the socket, `fchmod` on the open token file): no path is ever changed by name, so a
link planted in the directory cannot redirect it.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import secrets
import signal
import socket
import socketserver
import stat
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from lakana_worker.launcher.engine import EngineError
from lakana_worker.launcher.requests import (
    MAX_REQUEST_BYTES,
    BuildRequest,
    CheckRequest,
    Refused,
    Request,
    RunRequest,
    container_name,
    parse_request,
)
from lakana_worker.launcher.service import Launcher, StartError

logger = logging.getLogger("lakana_worker.launcher")

TOKEN_FILE = "launcher.token"  # nosec B105 -- a file name, not a secret
_MAX_CONNECTIONS = 128
_READ_TIMEOUT = 30.0
"""Seconds a connection has to send its request line; the wait for the result is not bounded."""
_SEND_TIMEOUT = 60.0
"""Seconds a write to the caller may block; a caller that stops reading is treated as gone."""
_ROOT_UID = 0
"""Who must own the socket's directory."""
_TOKEN_TRIES = 8
_INTERNAL_ERROR = "launcher internal error; see docker compose logs launcher"
_SOCKET_UMASK = 0o117
"""Leaves the socket 0660: the owner and the worker's group may connect."""


class _CallerGone(Exception):
    """The caller closed its connection; its run is cleaned up and nothing can be answered."""


def write_token(directory: Path) -> str:
    """Write a fresh token next to the socket, readable by the owner and the group only.

    The file is written under a random temporary name, opened exclusively, and moved into place.

    Parameters
    ----------
    directory
        The socket's directory.

    Returns
    -------
    str
        The token (64 hex digits).

    Raises
    ------
    StartError
        If no free temporary name is found: something keeps creating files in the directory.
    """
    token = secrets.token_hex(32)
    for _ in range(_TOKEN_TRIES):
        temporary = directory / f".{TOKEN_FILE}.{secrets.token_hex(8)}"
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640
            )
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            os.fchmod(handle.fileno(), 0o640)
            handle.write(token)
        os.replace(temporary, directory / TOKEN_FILE)
        return token
    raise StartError(
        f"cannot create the token file in {directory}: names keep being taken; check what else "
        "writes to the launcher's socket volume"
    )


def _watch(connection: socket.socket, done: threading.Event, on_gone: Callable[[], None]) -> None:
    """Call `on_gone` when the caller closes its connection before the request is done.

    A caller sends nothing after its request line, so `recv` returns only at the end.

    Parameters
    ----------
    connection
        The caller's connection.
    done
        Set when the request has been answered; a close after that is not a departure.
    on_gone
        What to do then.
    """
    with contextlib.suppress(OSError):
        while connection.recv(4096):
            pass
    if not done.is_set():
        on_gone()


class _Handler(socketserver.StreamRequestHandler):
    """Serves one connection."""

    @property
    def _owner(self) -> LauncherServer:
        """The server this connection came to (the base class types it as ``BaseServer``).

        Returns
        -------
        LauncherServer
            The launcher's socket server.
        """
        return cast("LauncherServer", self.server)

    def _send(self, answer: dict[str, object]) -> None:
        """Write one answer line.

        Parameters
        ----------
        answer
            The line's object.

        Raises
        ------
        _CallerGone
            If the caller has closed its connection.
        """
        try:
            self.wfile.write(json.dumps(answer).encode("utf-8") + b"\n")
            self.wfile.flush()
        except OSError:
            raise _CallerGone from None

    def _try_send(self, answer: dict[str, object]) -> None:
        """Write one answer line, leaving a caller that has gone unanswered.

        Parameters
        ----------
        answer
            The line's object.
        """
        with contextlib.suppress(_CallerGone):
            self._send(answer)

    def handle(self) -> None:
        """Read the request, run it, answer; never let an error escape."""
        self._serve()

    def _read_line(self) -> bytes | None:
        """Read the request line, bounded in size and by one deadline for the whole line.

        Returns
        -------
        bytes or None
            The line (more than the limit when the caller sent more, which parsing refuses), or
            `None` when it did not arrive in time (the caller was told).
        """
        deadline = time.monotonic() + _READ_TIMEOUT
        data = bytearray()
        late = False
        try:
            while b"\n" not in data and len(data) <= MAX_REQUEST_BYTES:
                left = deadline - time.monotonic()
                if left <= 0:
                    late = True
                    break
                self.connection.settimeout(left)
                chunk = self.connection.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            late = True
        if late:
            self._try_send({"refused": "no request line arrived in time; send the request at once"})
            return None
        self.connection.settimeout(None)
        # A send timeout of its own: the watcher thread blocks in `recv` on this socket, so a
        # socket-wide timeout would end it. A blocked send then fails and the caller is gone.
        seconds = int(_SEND_TIMEOUT)
        micro = int((_SEND_TIMEOUT - seconds) * 1_000_000)
        self.connection.setsockopt(
            socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", seconds, micro)
        )
        return bytes(data.split(b"\n", 1)[0] + b"\n") if b"\n" in data else bytes(data)

    def _serve(self) -> None:
        """Parse, supervise, answer."""
        launcher = self._owner.launcher
        raw = self._read_line()
        if raw is None:
            return
        if not raw:
            # A connection closed without a byte: the bundle's healthcheck, every 5 seconds.
            logger.debug("A connection closed without a request (a healthcheck).")
            return
        try:
            request = parse_request(raw, token=self._owner.token, settings=launcher.settings)
        except Refused as exc:
            logger.warning("Refused a request: %s", exc)
            self._try_send({"refused": str(exc)})
            return
        except Exception:
            logger.exception("The launcher failed while reading a request")
            self._try_send({"error": _INTERNAL_ERROR})
            return
        name: str | None = None
        owner = ""
        done = threading.Event()
        if isinstance(request, (RunRequest, BuildRequest, CheckRequest)):
            owner = request.owner
            if isinstance(request, RunRequest):
                kind, ident = "task", request.task
            elif isinstance(request, CheckRequest):
                kind, ident = "check", request.check
            else:
                kind, ident = "build", request.build
            name = container_name(launcher.settings, kind, ident)
            threading.Thread(
                target=_watch,
                args=(self.connection, done, functools.partial(launcher.abandon, name, owner)),
                daemon=True,
            ).start()
        try:
            self._answer(request)
        finally:
            done.set()
            if name is not None:
                # A departure that landed just before `done` was set is spent here.
                launcher.forget(name, owner)

    def _answer(self, request: Request) -> None:
        """Run a parsed request and send its result, or say what went wrong.

        Parameters
        ----------
        request
            The parsed request.
        """
        try:
            result = self._owner.launcher.handle(request, self._send)
        except _CallerGone:
            return  # the run is cleaned up already
        except Refused as exc:
            self._try_send({"refused": self._owner.launcher.redact(str(exc))})
            return
        except EngineError as exc:
            logger.error("Docker failed: %s", exc)
            self._try_send({"error": self._owner.launcher.redact(str(exc))})
            return
        except Exception:
            logger.exception("The launcher failed while serving a request")
            self._try_send({"error": _INTERNAL_ERROR})
            return
        self._try_send({"result": result})


class LauncherServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """The launcher's socket server.

    Parameters
    ----------
    path
        Where to listen.
    launcher
        Runs the requests.
    token
        What every request must carry.
    """

    daemon_threads = True
    request_queue_size = 64

    def process_request(  # ty: ignore[invalid-method-override]
        self, request: socket.socket, client_address: object
    ) -> None:
        """Hand a connection to a thread, or refuse it at once when every slot is taken.

        The refusal is written here, in the accept loop, so a flood of connections does not
        make a thread each.

        Parameters
        ----------
        request
            The connection.
        client_address
            Its address (a Unix socket has none).
        """
        if self.slots.acquire(blocking=False):
            try:
                super().process_request(request, client_address)
            except BaseException:
                self.slots.release()  # no thread was started to give the slot back
                raise
            return
        with contextlib.suppress(OSError):
            request.settimeout(0.5)
            # Take what the caller already sent: closing with unread bytes would reset the
            # connection and the caller would never see the refusal.
            with contextlib.suppress(BlockingIOError):
                request.recv(MAX_REQUEST_BYTES, socket.MSG_DONTWAIT)
            request.sendall(
                json.dumps(
                    {
                        "refused": "the launcher is busy: too many connections at once; retry, "
                        "or lower the number of workers"
                    }
                ).encode("utf-8")
                + b"\n"
            )
        self.shutdown_request(request)

    def process_request_thread(  # ty: ignore[invalid-method-override]
        self, request: socket.socket, client_address: object
    ) -> None:
        """Serve a connection in its thread, then give its slot back.

        Parameters
        ----------
        request
            The connection.
        client_address
            Its address.
        """
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def __init__(self, path: str, *, launcher: Launcher, token: str) -> None:
        self.launcher = launcher
        self.token = token
        self.slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        # The socket is created with its final mode: the umask leaves it 0660.
        previous = os.umask(_SOCKET_UMASK)
        try:
            super().__init__(path, _Handler)
        finally:
            os.umask(previous)


def _check_directory(directory: Path) -> None:
    """Check that the socket's directory still has the protections the image gives it.

    Parameters
    ----------
    directory
        The socket's directory.

    Raises
    ------
    StartError
        If it is not owned by root, lacks the sticky or set-group-id bit, or is writable by
        everyone: a bind mount or a volume from an older image would silently lose them.
    """
    try:
        info = directory.stat()
    except OSError as exc:
        raise StartError(
            f"cannot read the launcher's socket directory {directory} ({exc.strerror}); mount a "
            "volume there"
        ) from None
    fix = (
        f"The directory {directory} must be root-owned with mode 3770, which the launcher image "
        "prepares. Mount the volume at /run/lakana-launcher in both services (launcher and "
        "worker), then run `docker compose down` and `docker volume rm <project>_launcher-socket` "
        "so the next start creates it from the image."
    )
    if info.st_uid != _ROOT_UID:
        raise StartError(f"The socket directory must belong to root. {fix}")
    if not info.st_mode & stat.S_ISVTX:
        raise StartError(f"The socket directory lacks the sticky bit. {fix}")
    if not info.st_mode & stat.S_ISGID:
        raise StartError(f"The socket directory lacks the set-group-id bit. {fix}")
    if info.st_mode & stat.S_IWOTH:
        raise StartError(f"The socket directory must not be writable by everyone. {fix}")


def remove_stale_socket(path: Path) -> None:
    """Remove the socket an earlier launcher left, before this one starts.

    The socket volume outlives the container, and the bundle's healthcheck connects to the
    socket: a socket nobody listens on refuses the connection, but the start (the engine check,
    the probes, a pull) takes long enough that removing it first keeps the meaning plain: a
    socket at the path is this launcher's.

    Parameters
    ----------
    path
        The socket path.

    Raises
    ------
    StartError
        If the socket's directory is not as the image prepares it, or something that is not a
        socket sits at the path.
    """
    _check_directory(path.parent)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(existing.st_mode):
        raise StartError(
            f"{path} exists and is not a socket; remove it from the launcher's socket volume"
        )
    path.unlink(missing_ok=True)


def serve(launcher: Launcher) -> None:
    """Listen on the configured socket until the process ends.

    Parameters
    ----------
    launcher
        The started launcher.

    Raises
    ------
    StartError
        If the socket's directory is not as the image prepares it, something that is not a
        socket sits at the socket path, or no token file can be written.
    """
    path = launcher.settings.socket
    _check_directory(path.parent)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISSOCK(existing.st_mode):
        raise StartError(
            f"{path} exists and is not a socket; remove it from the launcher's socket volume"
        )
    token = write_token(path.parent)
    if existing is not None:
        path.unlink(missing_ok=True)
    server = LauncherServer(str(path), launcher=launcher, token=token)

    def on_signal(number: int, frame: object) -> None:
        """End the serving loop from a thread.

        `shutdown` waits for the loop, which this handler interrupted, so it cannot run here.

        Parameters
        ----------
        number
            The signal.
        frame
            The interrupted frame.
        """
        logger.info("Signal %d: stopping and removing every run of this stack.", number)
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous = {
        number: signal.signal(number, on_signal) for number in (signal.SIGTERM, signal.SIGINT)
    }
    launcher.start_reaper()
    logger.info("The launcher listens on %s.", path)
    try:
        server.serve_forever()
    finally:
        launcher.shutdown()
        server.server_close()
        for number, handler in previous.items():
            if handler is not None:
                signal.signal(number, handler)
