"""The worker's side of a query child: start it, read its result, stop it.

One `QueryChild` is one process running `lakana_worker.query_child`. The thread that runs the
query does everything in one loop (`QueryChild._step`): it writes the request, reads the result
and standard error, and looks at the clock and at the reasons to stop. No second thread
supervises, so two reasons cannot race; the one exception is the memory watchdog, which may
kill a child from its own thread and takes the child's lock to do so.

**Stopping** is always the same sequence: ask (``SIGTERM`` to the child's process group, which
the child turns into `connection.interrupt()`), wait `kill_grace_seconds`, kill the group. The
first reason is the recorded one; a later reason changes nothing. Memory is the exception to
"ask, wait": the watchdog kills at once.

**A result is complete** only when all five hold: no reason to stop was set, pyarrow read the
stream to its end without an error, the child exited with status 0, the pipe was read to its
real end of file (the marker alone proves nothing: eight bytes of data can look like it), and
the last eight bytes it wrote are the Arrow end-of-stream marker. pyarrow alone is not
enough: its reader treats a stream that simply stops, as after a kill, as a clean end
(measured). `QueryChild.finish` checks this, and the batch iterator of `run` calls it before
it ends, so a caller that sees the iterator end has a complete result, and any other caller
gets `ChildFailed`.

**Back-pressure.** Standard output is read only when the caller asks for bytes, and at most as
many as it asks for. A child that produces faster than the worker consumes blocks on its pipe.

The child leads its own session, so its process group is its process id. The leader is not
reaped until `close`, which keeps that id reserved while the group is signalled.
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import resource
import selectors
import signal
import subprocess  # nosec B404 -- a fixed argv, no shell, an environment built from scratch
import sys
import tempfile
import threading
import uuid
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import pyarrow as pa

from lakana_core.runs.resources import AttemptResources, valid_count
from lakana_worker.processes import remove_private_tree, signal_group
from lakana_worker.query_protocol import (
    END_OF_STREAM,
    EXIT_FAILED,
    INTERNAL,
    MAX_ERROR_LINE_BYTES,
    MAX_USAGE_BYTES,
    USAGE_FILE,
    ErrorDocument,
    Request,
    decode_error,
    encode_request,
)
from lakana_worker.resources import current_collector, from_rusage, peak_memory

logger = logging.getLogger(__name__)

CHILD_MODULE = "lakana_worker.query_child"
_PLATFORM = sys.platform
_CHUNK = 65536
_DISCARD_CHUNK = 1024 * 1024
_MAX_STDERR_BYTES = 2 * MAX_ERROR_LINE_BYTES
"""How much of a child's standard error is kept: its end, where the error document is.

Twice the longest line the protocol allows (about 59 KiB): the document is the last line, and
what a traceback or an extension printed before it cannot push it out.
"""
_DRAIN_SECONDS = 2.0
"""How long a silent pipe is waited on after the child's end (exit or kill), for a process
outside the group that holds it open."""
_STDERR_DRAIN_SECONDS = 0.5
"""How long standard error is read after the child's exit, for a holder of the pipe."""
_CANCEL_POLL_SECONDS = 1.0
"""How often `Stops.cancel_requested` is asked: once a second (a read of memory)."""


class StopReason(enum.StrEnum):
    """Why the worker stopped a query child."""

    TIMEOUT = "timeout"
    CANCEL_EVENT = "cancel_event"
    """The cancel event of the task run this query executes for: a cancel request on the run, a
    lost lease or a release at drain."""
    CANCEL_REQUEST = "cancel_request"
    """`POST .../queries/{id}/cancel` set `query_runs.cancel_requested`."""
    DRAIN = "drain"
    """The worker is shutting down and the drain grace is over."""
    WORKER_STALE = "worker_stale"
    """The worker could not write its heartbeat for a lease."""
    OUT_OF_MEMORY = "out_of_memory"
    """The memory watchdog killed the child."""


@dataclass(frozen=True)
class Stops:
    """What can stop a query besides its time limit.

    Attributes
    ----------
    cancel_event
        The cancel event of the task run the query executes for.
    cancel_requested
        Says whether a cancel request was recorded for the query; asked about once a second,
        outside the supervisor's lock.
    draining
        Says whether the worker is shutting down and its drain grace is over; also asked
        outside the lock.
    worker_stale
        Says whether the worker could not write its heartbeat for a lease; also asked outside
        the lock, and must not block: it answers from memory (the time of the last heartbeat
        that succeeded).

    Notes
    -----
    No callable may be called under the supervisor's lock, which a memory kill takes: a
    read of the database behind any of them must never hold the watchdog up. The supervisor
    calls them outside it. They are still called on the thread that reads the result, so they
    should not block for long.
    """

    cancel_event: threading.Event | None = None
    cancel_requested: Callable[[], bool] | None = None
    draining: Callable[[], bool] | None = None
    worker_stale: Callable[[], bool] | None = None


class ChildFailed(Exception):
    """A query child did not deliver a complete result.

    Parameters
    ----------
    stop
        The first reason the worker stopped the child, if it did.
    killed
        Whether the child had to be killed in the stop sequence (ask, wait, kill).
    own_kill
        Whether the supervisor killed the child on its own while cleaning up, with no reason to
        stop (output it could not read, or a child that did not exit after its output ended).
        A return code of -9 with this set is the supervisor's doing, not a foreign signal.
    error
        The error document the child wrote, if any.
    returncode
        The child's exit status (negative for a signal), if it was reaped.
    detail
        Text for the message: the memory watchdog's reason, or why the child could not start.
    """

    def __init__(
        self,
        *,
        stop: StopReason | None = None,
        killed: bool = False,
        own_kill: bool = False,
        error: ErrorDocument | None = None,
        returncode: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(f"query child failed: stop={stop} error={error} status={returncode}")
        self.stop = stop
        self.killed = killed
        self.own_kill = own_kill
        self.error = error
        self.returncode = returncode
        self.detail = detail


def query_environment(home: Path, tmp: Path) -> dict[str, str]:
    """Build the complete environment of a query child.

    Parameters
    ----------
    home
        The child's private home directory.
    tmp
        The child's private temporary directory.

    Returns
    -------
    dict of str to str
        ``PATH``, ``HOME``, ``TMPDIR`` and, when the worker has
        one, ``TZ``. Nothing else of the worker's environment.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(tmp),
    }
    if "TZ" in os.environ:
        env["TZ"] = os.environ["TZ"]
    return env


def extension_directory() -> str:
    """Name the directory DuckDB installs extensions into for the worker's user.

    Returns
    -------
    str
        ``~/.duckdb/extensions`` of the worker's user.
    """
    return str(Path.home() / ".duckdb" / "extensions")


class QueryChild:
    """One query child, from start to reaped.

    Use it as a context manager; `run` wraps it for the usual case. For pyarrow it is a binary
    file to read from (`read`, `closed`).

    Parameters
    ----------
    make_request
        Builds the request from the path of the child's private temp directory.
    timeout_seconds
        The time limit; it counts until the result is judged complete.
    kill_grace_seconds
        How long the child has to stop after the ask before the group is killed; 0 kills at once.
    stops
        The other reasons to stop.
    argv
        The command to run; the real child unless a test says otherwise.
    directory_prefix
        Prefix of the private directory (a path prefix; it is created in the temp directory
        when relative).
    poll_seconds
        The longest one wait for a descriptor lasts, which bounds how late a reason is noticed.
    """

    def __init__(
        self,
        make_request: Callable[[str], Request],
        *,
        timeout_seconds: float,
        kill_grace_seconds: float,
        stops: Stops | None = None,
        argv: Sequence[str] | None = None,
        directory_prefix: str = "lakana-spill-",
        poll_seconds: float = 0.1,
    ) -> None:
        self._make_request = make_request
        self._timeout = timeout_seconds
        self._grace = kill_grace_seconds
        self._stops = stops or Stops()
        self._argv = (
            list(argv) if argv is not None else [sys.executable, "-I", "-B", "-m", CHILD_MODULE]
        )
        self._prefix = directory_prefix
        self._poll_seconds = poll_seconds
        collector = current_collector()
        self._unit = None if collector is None else collector.unit
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._directory: str | None = None
        self._selector: selectors.BaseSelector | None = None
        self._pending = memoryview(b"")
        self._stderr = bytearray()
        self._tail = b""
        self._stdout_open = False
        self._stderr_open = True
        self._own_kill = False
        self._eof = False
        self._reaped = False
        self._closed = False
        self._deadline = 0.0
        self._kill_at: float | None = None
        self._drain_until: float | None = None
        self._next_cancel_poll = 0.0
        self._cancel_poll_failed = False
        self._resources = AttemptResources()
        self._watched: int | None = None
        self.reason: StopReason | None = None
        self.killed = False
        self.detail: str | None = None

    @property
    def pid(self) -> int | None:
        """The child's process id, or `None` before it started."""
        return None if self._process is None else self._process.pid

    @property
    def unit(self) -> uuid.UUID | None:
        """The query run whose collector was current when the child was made, if any."""
        return self._unit

    @property
    def stderr(self) -> bytes:
        """The kept end of what the child wrote to standard error."""
        return bytes(self._stderr)

    @property
    def resources(self) -> AttemptResources:
        """The figures of the reaped child; all unknown until `close` has reaped it.

        Processor time comes from ``wait4``. Peak memory is what the child reported about itself
        (Linux, its own ``VmHWM``), else the memory watchdog's last reading of a child it
        killed, else unknown; macOS has none yet. Bytes read is what the child counted, if it
        lived to say.
        """
        return self._resources

    def note_resident(self, size: int) -> None:
        """Record the size the memory watchdog measured, for a child it is about to kill.

        Parameters
        ----------
        size
            The resident bytes of the child's process tree.
        """
        self._watched = size

    @property
    def closed(self) -> bool:
        """Whether `close` ran (pyarrow asks)."""
        return self._closed

    def __enter__(self) -> QueryChild:
        """Start the process.

        Returns
        -------
        QueryChild
            This child, started.

        Raises
        ------
        ChildFailed
            If the process could not be started.
        """
        try:
            # Absolute: the request names paths inside it, and the child's working directory is it.
            self._directory = os.path.abspath(tempfile.mkdtemp(prefix=self._prefix))
            home, tmp = Path(self._directory, "home"), Path(self._directory, "tmp")
            home.mkdir(mode=0o700)
            tmp.mkdir(mode=0o700)
            request = encode_request(self._make_request(str(tmp)))
            self._process = subprocess.Popen(  # nosec B603 -- fixed argv, no shell, minimal env
                self._argv,
                cwd=self._directory,
                env=query_environment(home, tmp),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                close_fds=True,
            )
            process = self._process
            assert process.stdin and process.stdout and process.stderr  # nosec B101 -- all PIPE
            self._deadline = time.monotonic() + self._timeout
            self._pending = memoryview(request)
            self._stdout_open = True
            os.set_blocking(process.stdin.fileno(), False)
            self._selector = selectors.DefaultSelector()
            self._selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            self._selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            self._selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            REGISTRY.add(self)
        except (OSError, ValueError) as exc:
            self.close()
            reason = getattr(exc, "strerror", None) or str(exc)
            if getattr(exc, "filename", None):
                reason = f"{reason}: {exc.filename}"  # ty: ignore[unresolved-attribute]
            raise ChildFailed(detail=f"could not start the query process: {reason}") from exc
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Kill what still runs, reap, and remove the private directory.

        Parameters
        ----------
        exc_type
            The exception type, if any.
        exc
            The exception, if any.
        traceback
            Its traceback, if any.
        """
        self.close()

    def _exited(self) -> bool:
        """Say whether the leader has exited, without reaping it.

        Returns
        -------
        bool
            Whether it has exited, or was never started or already reaped.
        """
        process = self._process
        if process is None or self._reaped:
            return True
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None

    def _signal(self, signum: int) -> bool:
        """Send a signal to the child's process group, unless the leader was reaped.

        The caller holds `_lock`: the reap takes it too, so the group id cannot be reused
        between the check and the signal.

        Parameters
        ----------
        signum
            The signal.

        Returns
        -------
        bool
            Whether a group was there to receive it.
        """
        if self._process is None or self._reaped:
            return False
        return signal_group(self._process, signum)

    def _cancel_requested(self, now: float) -> bool:
        """Ask `Stops.cancel_requested` once a second, outside the lock.

        The callable may be slow (a custom one); a memory kill takes the lock and must never
        wait behind it. A read that raises counts as no and is logged once.

        Parameters
        ----------
        now
            The monotonic time.

        Returns
        -------
        bool
            Whether a cancel request was recorded.
        """
        stops = self._stops
        if (
            stops.cancel_requested is None
            or self.reason is not None
            or now < self._next_cancel_poll
        ):
            return False
        if stops.cancel_event is not None and stops.cancel_event.is_set():
            return False
        self._next_cancel_poll = now + _CANCEL_POLL_SECONDS
        try:
            return bool(stops.cancel_requested())
        except Exception:
            if not self._cancel_poll_failed:
                self._cancel_poll_failed = True
                logger.exception("Reading a query's cancel request failed; it is retried.")
        return False

    def _draining(self) -> bool:
        """Ask `Stops.draining` outside the lock, while no reason is recorded yet.

        Returns
        -------
        bool
            Whether the worker is draining.
        """
        draining = self._stops.draining
        return draining is not None and self.reason is None and bool(draining())

    def _worker_stale(self) -> bool:
        """Ask `Stops.worker_stale` outside the lock, while no reason is recorded yet.

        Returns
        -------
        bool
            Whether the worker's heartbeat is a lease old.
        """
        stale = self._stops.worker_stale
        return stale is not None and self.reason is None and bool(stale())

    def _due(
        self, now: float, cancel_requested: bool, draining: bool, worker_stale: bool
    ) -> StopReason | None:
        """Name the reason to stop that applies now, if any.

        Parameters
        ----------
        now
            The monotonic time.
        cancel_requested
            The answer of `_cancel_requested`.
        draining
            The answer of `_draining`.
        worker_stale
            The answer of `_worker_stale`.

        Returns
        -------
        StopReason or None
            The reason, in the order: cancel event, cancel request, drain, stale worker, time
            limit.
        """
        stops = self._stops
        if stops.cancel_event is not None and stops.cancel_event.is_set():
            return StopReason.CANCEL_EVENT
        if cancel_requested:
            return StopReason.CANCEL_REQUEST
        if draining:
            return StopReason.DRAIN
        if worker_stale:
            return StopReason.WORKER_STALE
        if now >= self._deadline:
            return StopReason.TIMEOUT
        return None

    def _tick(self) -> None:
        """Look at the clock and the reasons, and take the next step of "ask, wait, kill".

        A reason counts until the result is judged complete, also after the child has exited.
        """
        now = time.monotonic()
        requested = self._cancel_requested(now)
        draining = self._draining()
        worker_stale = self._worker_stale()
        with self._lock:
            if self.reason is None:
                reason = self._due(now, requested, draining, worker_stale)
                if reason is not None:
                    self.reason = reason
                    self._signal(signal.SIGTERM)
                    self._kill_at = now + self._grace
            elif self._kill_at is not None and now >= self._kill_at and not self._exited():
                self._kill_at = None
                self.killed = self._signal(signal.SIGKILL) or self.killed
            if self._drain_until is None and (self.killed or self._exited()):
                self._signal(signal.SIGKILL)
                self._drain_until = now + _DRAIN_SECONDS

    def stop_reason(self) -> StopReason | None:
        """Evaluate the reasons to stop once, without reading, and return the recorded one.

        For a caller that is busy between reads (a long step of its own with the stream open)
        and wants to know whether the child has been asked to stop. It takes the steps of the
        stop sequence, so a reason found here has already asked the child. It answers nothing
        once the result has been judged complete or the child was closed, and nothing for a child
        that was never started: after the iterator has ended normally it is always `None`, so it
        is no check to make before storing or committing a result.

        Returns
        -------
        StopReason or None
            The first reason recorded, or `None`.
        """
        if self._process is not None and not self._closed:
            self._tick()
        return self.reason

    def kill_for_memory(self, detail: str) -> bool:
        """Kill the child at once because the worker is over its memory limit.

        Called from the memory watchdog's thread. The first reason stays the recorded one: a
        child already asked to stop for its time limit is killed, and still fails as timed out.

        Parameters
        ----------
        detail
            Why the watchdog killed it; it ends up in the failure's `detail`.

        Returns
        -------
        bool
            Whether a live child was killed.
        """
        with self._lock:
            if self._exited() or not self._signal(signal.SIGKILL):
                return False
            if self.reason is None:
                self.reason = StopReason.OUT_OF_MEMORY
                self.detail = detail
            self.killed = True
            self._kill_at = None
            return True

    def _feed(self) -> None:
        """Write the next piece of the request, and close standard input after the last."""
        process, selector = self._process, self._selector
        assert process is not None and selector is not None  # nosec B101 -- started
        assert process.stdin is not None  # nosec B101 -- PIPE
        try:
            sent = os.write(process.stdin.fileno(), self._pending[:_CHUNK])
        except BlockingIOError:
            sent = 0
        except OSError:  # the child closed its standard input or died
            sent = len(self._pending)
        self._pending = self._pending[sent:]
        if not self._pending:
            selector.unregister(process.stdin)
            with contextlib.suppress(OSError):
                process.stdin.close()

    def _read_stderr(self) -> None:
        """Keep the end of what the child wrote to standard error."""
        process, selector = self._process, self._selector
        assert process is not None and selector is not None  # nosec B101 -- started
        assert process.stderr is not None  # nosec B101 -- PIPE
        chunk = os.read(process.stderr.fileno(), _CHUNK)
        if not chunk:
            selector.unregister(process.stderr)
            self._stderr_open = False
            return
        self._stderr += chunk
        del self._stderr[:-_MAX_STDERR_BYTES]

    def _step(self, want: int) -> bytes | None:
        """Do one round: the stop sequence, then whatever a descriptor is ready for.

        Standard input and standard error are served before standard output may end the round,
        so that an error document written just before the end is not left unread. After the
        child's end a silent pipe is given `_DRAIN_SECONDS` and then cut (a process outside the
        group may hold it open); once a reason is set or the child was killed it is cut at the
        end of that time whatever it carries. Bytes that are waiting are never cut off merely
        because the caller took long to come back for them.

        Parameters
        ----------
        want
            The most bytes of standard output to read.

        Returns
        -------
        bytes or None
            What was read (possibly empty), or `None` at the end of standard output.
        """
        selector, process = self._selector, self._process
        assert process is not None and selector is not None  # nosec B101 -- started
        assert process.stdout is not None  # nosec B101 -- PIPE
        self._tick()
        if not self._stdout_open:
            return None
        expired = self._drain_until is not None and time.monotonic() >= self._drain_until
        events = selector.select(0 if expired else self._poll_seconds)
        readable = False
        for key, _events in events:
            if key.data == "stdin":
                self._feed()
            elif key.data == "stderr":
                self._read_stderr()
            else:
                readable = True
        if expired and (self.killed or self.reason is not None or not readable):
            self._stdout_open = False
            selector.unregister(process.stdout)
            return None
        if not readable:
            return b""
        data = os.read(process.stdout.fileno(), want)
        if not data:
            selector.unregister(process.stdout)
            self._stdout_open = False
            self._eof = True
            return None
        self._tail = (self._tail + data)[-len(END_OF_STREAM) :]
        return data

    def read(self, size: int = -1) -> bytes:
        """Read up to `size` bytes of the child's standard output, as a file does.

        Once a reason to stop is set the caller gets no more: what the child still writes is
        read and dropped by `finish` or `failed`, so that a child blocked on a full pipe can
        reach its interrupt.

        Parameters
        ----------
        size
            The most bytes to return; negative or `None` reads one chunk at a time.

        Returns
        -------
        bytes
            Up to `size` bytes; fewer only at the end of the output or after a reason to stop
            was set. Empty at the end.
        """
        want = size if size is not None and size >= 0 else _CHUNK
        out = bytearray()
        while len(out) < want:
            data = self._step(want - len(out))
            if data is None or self.reason is not None:
                break
            out += data
        return bytes(out)

    def _serve(self, seconds: float) -> None:
        """Wait up to `seconds` for standard error and read it.

        Used while the worker waits for the child's exit: standard error has to be emptied, or
        a child writing more than a pipe-full to it could never exit. Only standard error is
        registered by then (`_outcome` stops feeding the request).

        Parameters
        ----------
        seconds
            The longest wait.
        """
        selector = self._selector
        assert selector is not None  # nosec B101 -- started
        if not selector.get_map():
            time.sleep(seconds)
            return
        for _key, _events in selector.select(seconds):
            self._read_stderr()

    def _outcome(self, reader_failed: bool) -> ChildFailed | None:
        """Wait for the child's end and judge the result.

        Output that cannot be read on a pipe that is still open is ended at once. At the real
        end of the pipe the child has announced its own end: it gets the grace period to exit
        with its own status and error document, and is killed only after that. Standard error
        is emptied while waiting and once more after the exit.

        Parameters
        ----------
        reader_failed
            Whether pyarrow raised while reading: the output is then not a result, whatever the
            exit status says.

        Returns
        -------
        ChildFailed or None
            `None` for a complete result, the failure otherwise.
        """
        if reader_failed and self.reason is None and not self._eof:
            # Output that cannot be read, and nobody asked the child to stop: end it now.
            self._clean_up_kill()
        self._stop_feeding()
        trailing = False
        while (data := self._step(_DISCARD_CHUNK)) is not None:
            trailing = trailing or bool(data)
        patience = time.monotonic() + self._grace
        while not self._exited():
            self._tick()
            if self.reason is None and time.monotonic() >= patience:
                self._clean_up_kill()
            self._serve(0.005)
        self._drain_stderr()
        returncode = self.close()
        if self.reason is not None:
            return ChildFailed(
                stop=self.reason,
                killed=self.killed,
                own_kill=self._own_kill and returncode == -signal.SIGKILL,
                returncode=returncode,
                detail=self.detail,
            )
        if (
            not reader_failed
            and not trailing
            and returncode == 0
            and self._eof
            and self._tail == END_OF_STREAM
        ):
            return None
        error = decode_error(bytes(self._stderr)) if returncode == EXIT_FAILED else None
        last_output = bytes(self._stderr[-2000:]).decode("utf-8", "replace")
        if error is None:
            logger.warning(
                "A query process ended without a complete result (exit status %s). Its last "
                "output: %a",
                returncode,
                last_output,
            )
        elif error.kind == INTERNAL:
            # The child's traceback is on standard error. A user's error is never logged here.
            logger.warning(
                "A query process hit an unexpected error. Its last output: %a", last_output
            )
        return ChildFailed(
            error=error,
            own_kill=self._own_kill and returncode == -signal.SIGKILL,
            returncode=returncode,
        )

    def _stop_feeding(self) -> None:
        """Drop the part of the request the child never took: its end has been announced."""
        selector, process = self._selector, self._process
        assert process is not None and selector is not None  # nosec B101 -- started
        assert process.stdin is not None  # nosec B101 -- PIPE
        if self._pending:
            self._pending = memoryview(b"")
            selector.unregister(process.stdin)
            with contextlib.suppress(OSError):
                process.stdin.close()

    def _clean_up_kill(self) -> None:
        """Kill the group on the supervisor's own initiative, and remember that it did."""
        with self._lock:
            if not self._exited():
                self._own_kill = self._signal(signal.SIGKILL) or self._own_kill

    def _drain_stderr(self) -> None:
        """Read what is left on standard error after the exit, without blocking.

        Bounded by a short time: a process outside the group may hold the pipe and keep writing.
        """
        selector = self._selector
        assert selector is not None  # nosec B101 -- started
        deadline = time.monotonic() + _STDERR_DRAIN_SECONDS
        while self._stderr_open and time.monotonic() < deadline:
            if not any(key.data == "stderr" for key, _ in selector.select(0)):
                break
            self._read_stderr()

    def finish(self) -> None:
        """Wait for the child's end and check that the result is complete.

        Raises
        ------
        ChildFailed
            If it is not.
        """
        failed = self._outcome(reader_failed=False)
        if failed is not None:
            raise failed

    def failed(self) -> ChildFailed:
        """End the child after pyarrow could not read its output, and say why it failed.

        Returns
        -------
        ChildFailed
            The failure, for the caller to raise.
        """
        failed = self._outcome(reader_failed=True)
        assert failed is not None  # nosec B101 -- a failed reader is never a complete result
        return failed

    @staticmethod
    def _reap(process: subprocess.Popen[bytes]) -> resource.struct_rusage | None:
        """Wait for the exited leader and take its resource usage; the caller holds `_lock`.

        Parameters
        ----------
        process
            The leader, which has exited.

        Returns
        -------
        resource.struct_rusage or None
            Its usage; `None` if somebody else had reaped it already.
        """
        try:
            _pid, status, usage = os.wait4(process.pid, 0)
        except ChildProcessError:
            process.wait()
            return None
        process.returncode = os.waitstatus_to_exitcode(status)
        return usage

    def _figures(self, usage: resource.struct_rusage | None) -> AttemptResources:
        """Put the figures of the reaped child together.

        Parameters
        ----------
        usage
            The child's resource usage, or `None` if it could not be taken.

        Returns
        -------
        AttemptResources
            Processor time, peak memory and bytes read; any may be unknown.
        """
        if usage is None:
            return AttemptResources()
        measured = from_rusage(usage, _PLATFORM)
        told = self._told()
        peak = peak_memory(
            _PLATFORM,
            reported=valid_count(told.get("peak_memory_bytes")),
            watched=self._watched,
        )
        return AttemptResources(peak, measured.cpu_seconds, valid_count(told.get("bytes_read")))

    def _told(self) -> dict[str, object]:
        """Read what the child wrote about itself, with the file bounded and never followed.

        Returns
        -------
        dict
            The document; empty if the file is missing, too long, a link or not a JSON object.
        """
        assert self._directory is not None  # nosec B101 -- a reaped child was started in it
        try:
            descriptor = os.open(
                Path(self._directory, USAGE_FILE), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
            try:
                raw = os.read(descriptor, MAX_USAGE_BYTES + 1)
            finally:
                os.close(descriptor)
            document = json.loads(raw) if len(raw) <= MAX_USAGE_BYTES else None
        except (OSError, ValueError):
            return {}
        return document if isinstance(document, dict) else {}

    def close(self) -> int | None:
        """Kill the group if anything is left, reap the leader, remove the directory.

        Safe to call twice and before the process started.

        Returns
        -------
        int or None
            The child's exit status, or `None` if it never started.
        """
        process = self._process
        if process is not None and not self._reaped:
            with self._lock:
                if not self._exited():
                    self._own_kill = self._signal(signal.SIGKILL) or self._own_kill
                else:
                    self._signal(signal.SIGKILL)  # what the leader left in the group
            while not self._exited():
                time.sleep(0.005)
            with self._lock:
                usage = self._reap(process)
                self._reaped = True
            self._resources = self._figures(usage)
            REGISTRY.remove(self)
            assert process.stdin and process.stdout and process.stderr  # nosec B101 -- all PIPE
            for pipe in (process.stdin, process.stdout, process.stderr):
                with contextlib.suppress(OSError):
                    pipe.close()
        if self._selector is not None:
            self._selector.close()
            self._selector = None
        self._closed = True
        if self._directory is not None:
            remove_private_tree(Path(self._directory))
            self._directory = None
        return None if process is None else process.returncode


class Registry:
    """The query children that are running, for the memory watchdog."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._children: list[QueryChild] = []

    def add(self, child: QueryChild) -> None:
        """Register a started child.

        Parameters
        ----------
        child
            The child.
        """
        with self._lock:
            self._children.append(child)

    def remove(self, child: QueryChild) -> None:
        """Forget a reaped child.

        Parameters
        ----------
        child
            The child.
        """
        with self._lock, contextlib.suppress(ValueError):
            self._children.remove(child)

    def pids(self) -> list[int]:
        """List the process ids of the running query children.

        Returns
        -------
        list of int
            The ids.
        """
        with self._lock:
            return [child.pid for child in self._children if child.pid is not None]

    def children(self) -> list[QueryChild]:
        """List the query children that are running, for the memory watchdog to choose from.

        Returns
        -------
        list of QueryChild
            A copy.
        """
        with self._lock:
            return list(self._children)


REGISTRY = Registry()
"""The process-wide registry `QueryChild` reports to and the memory watchdog reads."""


class BatchStream(Iterator[pa.RecordBatch]):
    """A child's batches, ending only after the result was checked as complete.

    Parameters
    ----------
    child
        The child the reader reads from.
    reader
        The Arrow reader over the child.
    """

    def __init__(self, child: QueryChild, reader: pa.RecordBatchReader) -> None:
        self._child = child
        self._reader = reader
        self._done = False

    def __next__(self) -> pa.RecordBatch:
        """Return the next batch.

        Returns
        -------
        pyarrow.RecordBatch
            The next batch.

        Raises
        ------
        StopIteration
            After the last batch of a result that was checked as complete, and for ever after
            a stream that has ended or failed.
        ChildFailed
            If the stream cannot be read, or ends without a complete result.
        """
        if self._done:
            raise StopIteration
        try:
            return self._reader.read_next_batch()
        except StopIteration:
            self._done = True
            self._child.finish()
            raise
        except (OSError, pa.ArrowException, MemoryError) as exc:
            self._done = True
            raise self._child.failed() from exc

    def stop_reason(self) -> StopReason | None:
        """Say whether a reason to stop has arrived, without reading a batch.

        Returns
        -------
        StopReason or None
            The first reason recorded, or `None`. See `QueryChild.stop_reason`.
        """
        return self._child.stop_reason()


@contextmanager
def run(
    make_request: Callable[[str], Request],
    *,
    timeout_seconds: float,
    kill_grace_seconds: float,
    stops: Stops | None = None,
    argv: Sequence[str] | None = None,
    directory_prefix: str = "lakana-spill-",
) -> Generator[tuple[pa.Schema, BatchStream], None, None]:
    """Run one request in a child and stream its result.

    Parameters
    ----------
    make_request
        Builds the request from the child's private temp directory.
    timeout_seconds
        The time limit.
    kill_grace_seconds
        The grace between the ask and the kill.
    stops
        The other reasons to stop.
    argv
        The command to run; the real child unless a test says otherwise.
    directory_prefix
        Prefix of the child's private directory.

    Yields
    ------
    tuple of pyarrow.Schema and BatchStream
        The result's schema and its batches. The iterator ends only for a complete result, and
        that normal end is the only proof of one: leaving the block earlier, or reading only the
        schema, proves nothing and raises nothing. `BatchStream.stop_reason` says whether a
        reason to stop has arrived without reading.

    Raises
    ------
    ChildFailed
        If the child fails before the schema, or the iterator meets a result that is not
        complete.

    Notes
    -----
    Whatever way the block ends, the reaped child's figures (`QueryChild.resources`) are added to
    the current `lakana_worker.resources.usage_collector`, if there is one.
    """
    child = QueryChild(
        make_request,
        timeout_seconds=timeout_seconds,
        kill_grace_seconds=kill_grace_seconds,
        stops=stops,
        argv=argv,
        directory_prefix=directory_prefix,
    )
    try:
        with child:
            try:
                reader = pa.ipc.open_stream(child)
            except (OSError, pa.ArrowException, MemoryError) as exc:
                raise child.failed() from exc
            yield reader.schema, BatchStream(child, reader)
    finally:
        collector = current_collector()
        if collector is not None:
            collector.add(child.resources)
