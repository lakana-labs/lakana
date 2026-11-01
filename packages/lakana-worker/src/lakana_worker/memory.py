"""The worker's memory limit and the watchdog that enforces it.

DuckDB's `memory_limit` is a target for its own buffers; pyarrow, thread stacks and some
operators allocate beyond it, and one query once took 24 GB of a 30 GB machine until the
kernel's out-of-memory killer ended the worker.

`setrlimit` was tried and rejected: `RLIMIT_DATA` and `RLIMIT_AS` count what a process has
*mapped*, not what it uses, and the count does not shrink after a query, so an ordinary
worker ran out of threads after a few queries and a real hit left it crashing at random.

Instead a daemon thread measures the **resident** memory of the worker and of every process
it started (a Python task, `uv`, a query child) five times a second. Over the limit, the
watchdog first measures each query child's process tree and each running Python task's process
tree and kills the largest of them, at once (no ask, no grace: memory cannot wait). A
query so ended fails with the cause `out_of_memory`; a task so ended fails its attempt as
transient and is retried within its attempts (`task_ended_message`). The worker, and whatever
runs beside, go on. The watchdog then measures again. If the total is still over the limit, or
nothing endable was running, the worker kills its children, records why against what it was
running, and exits with `EXIT_CODE`; Docker (`restart: unless-stopped`) or the local-mode
supervisor starts it again. Only Linux does this: macOS measures the worker's own peak, which
never falls.

Known limits:

- The worker's own memory (maintenance, a result being stored) cannot be ended separately. The
  watchdog ends the largest endable consumer whatever its share, so a worker whose own memory sits
  just under the limit ends a small task or query beside it, the total drops, and the worker goes
  on; each retry and each new query is ended the same way (the task ends FAILED, "the largest
  consumer"). Give the worker more memory; restart it if its own memory keeps growing.
- The total counts every process of the worker's sessions; a consumer's size is its parent-child
  tree. A process a task detached by a double fork counts in the total, not in the task's size.
- A task whose virtual environment is still being built is not endable (the build cannot be
  interrupted, #48) and is skipped when the watchdog chooses; if the build is what grew and
  nothing else can be ended, the worker stops.

- A burst between two checks is not seen: one thread can make gigabytes resident in a second,
  so a very fast query can reach the container's own limit (or, in local mode, exhaust the
  machine) first.
- macOS measures the worker's own peak only; Windows is not enforced.
- `os._exit` skips the scheduler role's resignation, so scheduling pauses until that lease
  expires, as after a crash.
- A write that committed just as the watchdog marked its run failed is retried, as after a
  lost lease.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import and_, false, or_, select, update
from sqlalchemy.orm import Session

from lakana_core.config import DEFAULT_WORKER_MEMORY, MIN_WORKER_MEMORY, parse_memory_limit
from lakana_core.db import db_now
from lakana_core.identifiers import shown
from lakana_core.log_events import LogEvent, log_event
from lakana_core.models import QueryRun, QueryRunStatus
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.scheduler import COMMITTED_BEFORE_STOPPED_MESSAGE
from lakana_core.runs.store import committed_write
from lakana_core.runs.tasks import FailureKind, record_attempt
from lakana_worker.activity import ACTIVITY, Activity, TaskClaim
from lakana_worker.query_process import REGISTRY, Registry
from lakana_worker.runners import TASKS, TaskRegistry

resource: Any
try:
    import resource
except ImportError:  # pragma: no cover — Windows only
    resource = None

logger = logging.getLogger(__name__)

SETTING = "LAKANA_WORKER__MEMORY_LIMIT"
EXIT_CODE = 75
"""How the worker exits when it goes over its limit (`EX_TEMPFAIL`). Docker and shells treat
no code below 126 specially. `lakana.cli.local._supervisor.WORKER_MEMORY_EXIT_CODE` repeats the
number (the CLI does not import the worker), and a test on each side pins it."""
INTERVAL_SECONDS = 0.2
"""How often the watchdog measures."""
SESSION_SCAN_SECONDS = 1.0
"""How often the watchdog scans ``/proc`` for the members of its tasks' sessions (the parent
walk, which is cheap, runs on every check)."""
RECORD_TIMEOUT_SECONDS = 2.0
"""Longest the watchdog waits for the failure to be recorded before it exits anyway."""
RELEASE_SECONDS = 2.0
"""Longest the watchdog waits for an ended query's or task's memory to be gone before it measures
again."""
CGROUP_FRACTION = 0.9
"""Share of the container's limit the worker allows itself, so the watchdog fires first."""
_MB = 1024 * 1024
_CGROUP_V1_NO_LIMIT = 1 << 60
"""Cgroup v1 reports "no limit" as a number near 2**63; anything above this is none."""


def _limit_in(directory: Path, name: str, none_above: int | None) -> int | None:
    """Read one cgroup limit file.

    Parameters
    ----------
    directory
        The cgroup directory.
    name
        ``memory.max`` (v2) or ``memory.limit_in_bytes`` (v1).
    none_above
        Values at or above this mean "no limit" (v1); `None` for v2, where the word `max` does.

    Returns
    -------
    int or None
        The limit in bytes; `None` for no limit, a missing file, zero or garbage.
    """
    try:
        text = (directory / name).read_text().strip()
    except OSError:
        return None
    if not (text.isascii() and text.isdigit()):
        return None
    value = int(text)
    if value == 0 or (none_above is not None and value >= none_above):
        return None
    return value


def _smallest_up_to(root: Path, parts: list[str], name: str, none_above: int | None) -> int | None:
    """Take the smallest limit from a cgroup directory up to the mount root.

    Parameters
    ----------
    root
        The directory the cgroup hierarchy is mounted at.
    parts
        The cgroup's path components below `root`.
    name
        The limit file's name.
    none_above
        See `_limit_in`.

    Returns
    -------
    int or None
        The smallest limit found on the way; `None` when no level has one.
    """
    found = []
    for depth in range(len(parts) + 1):
        value = _limit_in(root.joinpath(*parts[:depth]), name, none_above)
        if value is not None:
            found.append(value)
    return min(found) if found else None


def _cgroup_paths(text: str) -> tuple[list[str] | None, list[str] | None]:
    """Find this process's cgroup in the v2 and v1 (memory controller) lines.

    Parameters
    ----------
    text
        The contents of ``/proc/self/cgroup``.

    Returns
    -------
    tuple
        The path components for v2 and for the v1 memory controller; `None` for a hierarchy
        that is absent or whose path leads outside the mount (a ``..`` component).
    """
    v2 = v1 = None
    for line in text.splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        parts = [part for part in path.split("/") if part]
        if ".." in parts or "." in parts:
            continue
        if hierarchy == "0" and controllers == "":
            v2 = parts
        elif "memory" in controllers.split(","):
            v1 = parts
    return v2, v1


def read_cgroup_limit(
    root: Path = Path("/sys/fs/cgroup"), self_cgroup: Path = Path("/proc/self/cgroup")
) -> int | None:
    """Read the memory limit that applies to this process's container or scope.

    The cgroup is found through ``/proc/self/cgroup``, so a worker without a private cgroup
    namespace is found too. The smallest limit from its cgroup up to the mount root wins.
    If ``/proc/self/cgroup`` cannot be read, only the mount root is looked at (what a
    container with its own cgroup namespace shows there).

    Parameters
    ----------
    root
        The cgroup mount.
    self_cgroup
        This process's cgroup file.

    Returns
    -------
    int or None
        The limit in bytes, or `None` for no limit, a missing file or garbage.
    """
    try:
        v2, v1 = _cgroup_paths(self_cgroup.read_text())
    except OSError:
        v2, v1 = [], []
    found = []
    if v2 is not None:
        value = _smallest_up_to(root, v2, "memory.max", None)
        if value is not None:
            found.append(value)
    if v1 is not None:
        value = _smallest_up_to(root / "memory", v1, "memory.limit_in_bytes", _CGROUP_V1_NO_LIMIT)
        if value is not None:
            found.append(value)
    return min(found) if found else None


@dataclass(frozen=True)
class Limit:
    """The worker's resolved limit.

    Parameters
    ----------
    bytes
        The limit, or `None` for none.
    text
        What the log and the messages call it, such as ``"4096 MB"``.
    source
        Where it came from: the setting's name, ``"the default"`` or, when derived, a
        description of the container's limit.
    derived
        Whether it was derived from the container's limit.
    """

    bytes: int | None
    text: str
    source: str
    derived: bool = False

    def describe(self) -> str:
        """Say what applies and why, for the start-up line.

        Returns
        -------
        str
            ``"4GB (LAKANA_WORKER__MEMORY_LIMIT)"``, ``"4GB (the default)"`` or
            ``"3686 MB, 90% of the container's 4096 MB"``.
        """
        return f"{self.text}, {self.source}" if self.derived else f"{self.text} ({self.source})"

    def origin(self) -> str:
        """Say where the limit came from, for the line logged when it is exceeded.

        Returns
        -------
        str
            As `describe`; for a derived limit, also where an operator changes it.
        """
        if self.derived:
            return (
                f"{self.describe()}; raise the container's memory limit "
                "(in the Compose bundle: LAKANA_WORKER_MEMORY in .env)"
            )
        return self.describe()


def resolve_limit(configured: str | None, cgroup: Callable[[], int | None] | None = None) -> Limit:
    """Decide which limit applies: the setting, else 90 percent of the container's, else 4GB.

    Parameters
    ----------
    configured
        `LAKANA_WORKER__MEMORY_LIMIT`, or `None` when it is not set.
    cgroup
        Reads the container's limit (a seam for tests).

    Returns
    -------
    Limit
        The limit and its source.
    """
    if configured is not None:
        limit = parse_memory_limit(configured)
        return Limit(limit, "off" if limit is None else configured, SETTING)
    container = (cgroup if cgroup is not None else read_cgroup_limit)()
    if container is not None:
        derived = int(container * CGROUP_FRACTION)
        return Limit(
            derived,
            f"{derived // _MB} MB",
            f"90% of the container's {container // _MB} MB",
            derived=True,
        )
    return Limit(parse_memory_limit(DEFAULT_WORKER_MEMORY), DEFAULT_WORKER_MEMORY, "the default")


def _children(pid: int, proc: Path) -> list[int] | None:
    """List the direct children of a process from ``/proc/<pid>/task/*/children``.

    Parameters
    ----------
    pid
        The parent.
    proc
        The ``/proc`` directory.

    Returns
    -------
    list of int or None
        The children, or `None` when the kernel does not offer the files.
    """
    found: list[int] = []
    seen = False
    try:
        tasks = list((proc / str(pid) / "task").iterdir())
    except OSError:
        return []
    for task in tasks:
        try:
            found.extend(int(part) for part in (task / "children").read_text().split())
            seen = True
        except (OSError, ValueError):
            continue
    return found if seen or not tasks else None


def _children_by_scan(pid: int, proc: Path) -> list[int]:
    """List the direct children of a process by reading every ``/proc/<n>/stat``.

    Parameters
    ----------
    pid
        The parent.
    proc
        The ``/proc`` directory.

    Returns
    -------
    list of int
        The children.
    """
    found = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rpartition(")")[2].split()
            if int(fields[1]) == pid:
                found.append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    return found


def descendants(pid: int, proc: Path = Path("/proc")) -> list[int]:
    """List every live descendant of a process.

    Parameters
    ----------
    pid
        The root process.
    proc
        The ``/proc`` directory.

    Returns
    -------
    list of int
        Children, grandchildren and so on, without `pid` itself.
    """
    found: list[int] = []
    pending = [pid]
    while pending:
        current = pending.pop()
        children = _children(current, proc)
        if children is None:
            children = _children_by_scan(current, proc)
        for child in children:
            if child not in found and child != pid:
                found.append(child)
                pending.append(child)
    return found


def _resident(pid: int, proc: Path, page_size: int) -> int:
    """Read one process's resident memory from ``/proc/<pid>/statm``.

    Parameters
    ----------
    pid
        The process.
    proc
        The ``/proc`` directory.
    page_size
        Bytes per page.

    Returns
    -------
    int
        Resident bytes; 0 for a process that has just exited.
    """
    try:
        return int((proc / str(pid) / "statm").read_text().split()[1]) * page_size
    except (OSError, ValueError, IndexError):
        return 0


def resident_bytes(
    pid: int | None = None, proc: Path = Path("/proc"), page_size: int | None = None
) -> int:
    """Measure the resident memory of a process and all its live descendants (Linux).

    Parameters
    ----------
    pid
        The root process; this one by default.
    proc
        The ``/proc`` directory.
    page_size
        Bytes per page; the system's by default.

    Returns
    -------
    int
        Resident bytes of the tree.
    """
    root = os.getpid() if pid is None else pid
    size = page_size if page_size is not None else os.sysconf("SC_PAGE_SIZE")
    return sum(_resident(p, proc, size) for p in [root, *descendants(root, proc)])


def _peak_self() -> int:  # pragma: no cover — macOS only
    """Measure this process's peak resident memory (macOS, where `ru_maxrss` is bytes).

    Returns
    -------
    int
        Peak resident bytes of this process; its children are not counted.
    """
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _ids(pid: int, proc: Path) -> tuple[int, int, int] | None:
    """Read a process's parent, process group and session from ``/proc/<pid>/stat``.

    Parameters
    ----------
    pid
        The process.
    proc
        The ``/proc`` directory.

    Returns
    -------
    tuple of int or None
        ``(parent, group, session)``, or `None` for a process that is gone or unreadable.
    """
    try:
        fields = (proc / str(pid) / "stat").read_text().rpartition(")")[2].split()
        return int(fields[1]), int(fields[2]), int(fields[3])
    except (OSError, ValueError, IndexError):
        return None


class ProcessTree:
    """The worker's descendants, including grandchildren whose parent has exited (Linux).

    Every subprocess the worker starts (a Python task, `uv`, `git`) leads its own session.
    A grandchild whose parent exited is no longer found by walking the tree, but it stays in
    that session, so the sessions seen are remembered and their members counted and killed
    too, for as long as any member lives.

    Parameters
    ----------
    pid
        The root process; this one by default.
    proc
        The ``/proc`` directory.
    page_size
        Bytes per page; the system's by default.
    killpg
        Sends a signal to a process group (a seam for tests).
    kill
        Sends a signal to a process (a seam for tests).
    clock
        A monotonic clock (a seam for tests).
    scan_interval
        Seconds between scans of ``/proc`` for session members.
    """

    def __init__(
        self,
        pid: int | None = None,
        proc: Path = Path("/proc"),
        page_size: int | None = None,
        killpg: Callable[[int, int], None] | None = None,
        kill: Callable[[int, int], None] | None = None,
        clock: Callable[[], float] | None = None,
        scan_interval: float = SESSION_SCAN_SECONDS,
    ) -> None:
        self._pid = os.getpid() if pid is None else pid
        self._proc = proc
        self._page_size = page_size
        self._killpg = killpg if killpg is not None else os.killpg
        self._kill = kill if kill is not None else os.kill
        self._sessions: set[int] = set()
        self._session_members: set[int] = set()
        self._scanned_at: float | None = None
        self._clock = clock if clock is not None else time.monotonic
        self._scan_interval = scan_interval

    def _scan(self) -> tuple[set[int], set[int]]:
        """Find every process in a remembered session by reading ``/proc/*/stat``.

        Returns
        -------
        tuple of set
            The processes found, and the sessions among the remembered ones that have members.
        """
        found: set[int] = set()
        alive: set[int] = set()
        try:
            entries = list(self._proc.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            ids = _ids(int(entry.name), self._proc) if entry.name.isdigit() else None
            if ids is not None and ids[2] in self._sessions:
                found.add(int(entry.name))
                alive.add(ids[2])
        return found, alive

    def members(self, fresh: bool = False) -> list[int]:
        """List the live processes the worker started, however they are related now.

        The parent walk runs on every call. The scan of ``/proc`` for the members of remembered
        sessions costs a read per process on the host, so it runs at most once per
        `scan_interval` (at once when a new session appears, or when `fresh`).

        The walk finds what the worker started because orphans are adopted by init, not by the
        worker: the image runs the worker under tini. As PID 1 without it, the worker would
        adopt the orphans of ``docker exec`` sessions and a stop would kill them too.

        Parameters
        ----------
        fresh
            Scan now, whatever the interval.

        Returns
        -------
        list of int
            Descendants by parent, plus every process in a session that a descendant led.
        """
        walked = descendants(self._pid, self._proc)
        own = _ids(self._pid, self._proc)
        own_session = own[2] if own is not None else -1
        new = False
        for pid in walked:
            ids = _ids(pid, self._proc)
            if ids is not None and ids[2] != own_session and ids[2] > 1:
                new = new or ids[2] not in self._sessions
                self._sessions.add(ids[2])
        found = set(walked)
        if not self._sessions:
            self._session_members = set()
            self._scanned_at = None
        else:
            now = self._clock()
            if (
                fresh
                or new
                or self._scanned_at is None
                or now - self._scanned_at >= self._scan_interval
            ):
                self._session_members, alive = self._scan()
                self._sessions &= alive
                self._scanned_at = now
            found |= self._session_members
        found.discard(self._pid)
        return sorted(found)

    def resident(self) -> int:
        """Measure the resident bytes of the worker and everything it started.

        Returns
        -------
        int
            Resident bytes.
        """
        size = self._page_size if self._page_size is not None else os.sysconf("SC_PAGE_SIZE")
        return sum(_resident(pid, self._proc, size) for pid in [self._pid, *self.members()])

    def kill(self) -> None:
        """Send SIGKILL to every process the worker started: whole sessions, then the rest."""
        members = self.members(fresh=True)
        for session in sorted(self._sessions):
            try:
                self._killpg(session, signal.SIGKILL)
            except OSError:
                continue
        for pid in members:
            try:
                self._kill(pid, signal.SIGKILL)
            except OSError:
                continue


def tools(platform: str = sys.platform) -> tuple[Callable[[], int], Callable[[], None]] | None:
    """Pick how this platform measures the worker and ends what it started.

    Parameters
    ----------
    platform
        `sys.platform`.

    Returns
    -------
    tuple or None
        Linux: the resident memory of the worker and everything it started, and a function
        that kills what it started. macOS: the worker's own peak, and no killing. Elsewhere
        `None`: nothing is measured.
    """
    if platform.startswith("linux"):
        tree = ProcessTree()
        return tree.resident, tree.kill
    if platform == "darwin":  # pragma: no cover — macOS only
        return _peak_self, lambda: None
    return None


def stop_worker(
    total: int,
    limit: Limit,
    *,
    record: Callable[[], None],
    kill_children: Callable[[], None],
    exit_process: Callable[[int], None],
    record_timeout: float = RECORD_TIMEOUT_SECONDS,
    mark_stopping: Callable[[], None] | None = None,
) -> None:
    """Stop the worker: log, mark it stopping, kill the children, record, exit.

    The children go first: a task's subprocess is usually what grew, and the growth must stop
    before the (slower) recording. The worker is marked stopping before that, so that the main
    thread, which wakes when a task's process dies, does not record an outcome of its own
    (`Activity.stopping`). The exit runs whatever fails before it, a recording thread that
    cannot start included.

    Parameters
    ----------
    total
        Measured resident bytes.
    limit
        The limit that was exceeded.
    record
        Records the failure against what the worker was running. Run in a thread
        and abandoned after `record_timeout`.
    kill_children
        Kills what the worker started.
    exit_process
        Ends the process (`os._exit`: the main thread may be inside DuckDB).
    record_timeout
        Seconds to wait for `record`.
    mark_stopping
        Sets `Activity.stopping`; the registry's by default.
    """
    logger.error(
        "Memory limit exceeded: the worker and its tasks use %d MB, the limit is %s. "
        "The worker stops now. What it was running is marked failed; a task is retried if it "
        "has attempts left.",
        total // _MB,
        limit.origin(),
        extra=log_event(LogEvent.MEMORY_STOP),
    )
    try:
        if mark_stopping is not None:
            mark_stopping()
        else:
            ACTIVITY.stopping = True
        _guarded(kill_children, "killing the worker's children")()
        thread = threading.Thread(target=_guarded(record, "recording the failure"), daemon=True)
        try:
            thread.start()
        except Exception:
            # Under memory pressure a new thread may not start. A worker that stays alive with
            # `stopping` set would drop every later outcome, so it exits all the same.
            logger.exception("Recording the memory failure could not start; skipped.")
            return
        thread.join(record_timeout)
        if thread.is_alive():
            logger.error(
                "Recording the memory failure took more than %.0f s; skipped.", record_timeout
            )
    finally:
        exit_process(EXIT_CODE)


def _guarded(step: Callable[[], None], what: str) -> Callable[[], None]:
    """Wrap a step so that its failure is logged, not raised.

    Parameters
    ----------
    step
        The step.
    what
        What it does, for the log.

    Returns
    -------
    callable
        The wrapped step.
    """

    def run() -> None:
        """Run the step."""
        try:
            step()
        except BaseException:
            logger.exception("Memory watchdog: %s failed.", what)

    return run


class MemoryWatchdog:
    """Measure the worker once an interval and stop it when it is over the limit.

    Parameters
    ----------
    limit_bytes
        The limit.
    measure
        Returns the current resident bytes.
    on_exceeded
        Called with the measured bytes when over the limit; it is expected not to return
        (a test's may).
    interval
        Seconds between measurements.
    wait
        Waits `interval` seconds and returns `True` to stop (a seam for tests).
    relieve
        Called with the measured bytes when over the limit, before `on_exceeded`; returns
        whether it stopped something. The watchdog then measures again and leaves the worker
        alone if that was enough.
    """

    def __init__(
        self,
        limit_bytes: int,
        measure: Callable[[], int],
        on_exceeded: Callable[[int], None],
        interval: float = INTERVAL_SECONDS,
        wait: Callable[[float], bool] | None = None,
        relieve: Callable[[int], bool] | None = None,
    ) -> None:
        self._limit = limit_bytes
        self._relieve = relieve
        self._measure = measure
        self._on_exceeded = on_exceeded
        self._interval = interval
        self._stop = threading.Event()
        self._wait = wait if wait is not None else self._stop.wait
        self._failed_once = False

    def check_once(self) -> bool:
        """Measure and act once: a query first, the worker only if that was not enough.

        Returns
        -------
        bool
            Whether the limit was exceeded.
        """
        total = self._measure()
        if total <= self._limit:
            return False
        if self._relieve is not None and self._relieved(total):
            total = self._measure()
            if total <= self._limit:
                return True
        self._on_exceeded(total)
        return True

    def _relieved(self, total: int) -> bool:
        """Ask the relief to stop something; a relief that fails counts as "nothing stopped".

        A failing relief must never keep the worker from stopping itself.

        Parameters
        ----------
        total
            The measured bytes.

        Returns
        -------
        bool
            Whether the relief stopped something.
        """
        assert self._relieve is not None  # nosec B101 -- the caller checked
        try:
            return self._relieve(total)
        except Exception:
            logger.exception(
                "Memory watchdog: ending a query or task failed; the worker stops instead."
            )
            return False

    def run(self) -> None:
        """Loop until stopped; an exception is logged once and the loop goes on."""
        while not self._wait(self._interval):
            try:
                self.check_once()
            except Exception:
                if not self._failed_once:
                    self._failed_once = True
                    logger.exception("Memory watchdog failed; it keeps running.")

    def start(self) -> threading.Thread:
        """Start the loop in a daemon thread.

        Returns
        -------
        threading.Thread
            The thread.
        """
        thread = threading.Thread(target=self.run, name="memory-watchdog", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Ask the loop to end."""
        self._stop.set()

    def start_in_background(self) -> MemoryWatchdog:
        """Start the loop in a daemon thread.

        Returns
        -------
        MemoryWatchdog
            This watchdog.
        """
        self.start()
        return self


def query_message(limit: Limit) -> str:
    """Say what a query run's user reads when the worker was stopped for memory.

    Parameters
    ----------
    limit
        The limit that was exceeded.

    Returns
    -------
    str
        The failure text.
    """
    return (
        f"Query failed: the worker ran out of memory and was stopped (its limit is {limit.text}). "
        f"{WORKER_MEMORY_ADVICE}"
    )


def query_child_message(limit: Limit) -> str:
    """Say what a query's user reads when the watchdog stopped that query.

    Parameters
    ----------
    limit
        The limit that was exceeded.

    Returns
    -------
    str
        The failure text.
    """
    return (
        f"Query failed: this query made the worker exceed its memory limit ({limit.text}) and "
        f"was stopped. {WORKER_MEMORY_ADVICE}"
    )


class Consumer(Protocol):
    """A query child or a task process, as the watchdog chooses among them."""

    @property
    def pid(self) -> int | None:
        """Give the process id of the tree's root; `None` for one that has not started."""
        ...

    def kill_for_memory(self, detail: str) -> bool:
        """Kill the process at once; `False` (and no signal) when it has already ended.

        Parameters
        ----------
        detail
            Why, as the failure's message reads.
        """
        ...


def kill_largest(
    candidates: Sequence[tuple[Consumer, str]], resident: Callable[[int], int]
) -> Consumer | None:
    """End the consumer whose process tree is the largest, passing over any that has ended.

    The kill goes through the consumer's own object, which signals the process group it
    started and knows whether it has reaped it, so one that has exited since the measurement is
    passed over and the next largest is tried; no unrelated process is signalled. Of equal
    sizes the earlier candidate is tried first. A task whose leader has exited but whose group a
    grandchild still holds is passed over too (its `kill_for_memory` does nothing; the runner ends
    the group within its poll interval), so in that window the choice moves on to the next
    largest. One is ended per call.

    Parameters
    ----------
    candidates
        Each consumer with the message its user reads if it is ended.
    resident
        Gives the resident memory of a process and its descendants for a process id.

    Returns
    -------
    Consumer or None
        The consumer that was ended, or `None` when none was left to end.
    """
    sized = sorted(
        (
            (resident(consumer.pid), index, consumer, detail)
            for index, (consumer, detail) in enumerate(candidates)
            if consumer.pid is not None
        ),
        key=lambda entry: (-entry[0], entry[1]),
    )
    for _size, _index, consumer, detail in sized:
        if consumer.kill_for_memory(detail):
            return consumer
    return None


@dataclass(frozen=True)
class Ended:
    """What the watchdog ended to relieve the worker.

    Parameters
    ----------
    pid
        The root process of the ended tree.
    task_run_id
        The task run, when it was a Python task; `None` for a query.
    """

    pid: int
    task_run_id: uuid.UUID | None = None


def stop_largest_consumer(
    total: int,
    limit: Limit,
    *,
    queries: Registry = REGISTRY,
    tasks: TaskRegistry = TASKS,
    resident: Callable[[int], int] = resident_bytes,
    wait: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Ended | None:
    """End the query child or Python task that uses the most memory, and wait for it to go.

    No ask and no grace: memory is the one reason to stop that cannot wait. See `kill_largest`
    for the choice and the passing over of what has ended. The query's or the task's own
    thread sees its process end and records the outcome (a query: ``out_of_memory``; a task: a
    transient failure that is retried within its attempts). The kernel detaches a killed
    process's address space at once, so its resident size reads 0 within a fraction of a
    millisecond (before the process is even waitable); the wait polls every 10 ms without
    holding a lock and only matters when the kill does not take. It ends after
    `RELEASE_SECONDS` whatever the size reads, and the watchdog measures again.

    Parameters
    ----------
    total
        Measured resident bytes of the worker and everything it started, for the log.
    limit
        The limit that was exceeded.
    queries
        The running query children.
    tasks
        The running task processes.
    resident
        Measures a process and its descendants.
    wait
        Sleeps this many seconds (a seam for tests).
    clock
        A monotonic clock (a seam for tests).

    Returns
    -------
    Ended or None
        What was ended; `None` when nothing endable was running.
    """
    candidates: list[tuple[Consumer, str]] = []
    described: dict[int, tuple[str, uuid.UUID | None, int | None]] = {}
    for child in queries.children():
        candidates.append((child, query_child_message(limit)))
        described[id(child)] = (f"a query (process {child.pid})", None, child.pid)
    for task in tasks.running():
        candidates.append((task, task_ended_message(limit)))
        described[id(task)] = (
            f"a Python task (asset {shown(task.asset)}, task run {task.task_run_id}, "
            f"process {task.pid})",
            task.task_run_id,
            task.pid,
        )
    chosen = kill_largest(candidates, resident)
    if chosen is None:
        return None
    what, task_run_id, pid = described[id(chosen)]
    assert pid is not None  # nosec B101 -- `kill_largest` passes over a consumer without a pid
    logger.error(
        "Memory limit exceeded: the worker and its tasks use %d MB, the limit is %s. The "
        "largest consumer, %s, was ended; the worker keeps running if that frees enough memory.",
        total // _MB,
        limit.origin(),
        what,
        extra=log_event(LogEvent.MEMORY_TASK_ENDED),
    )
    deadline = clock() + RELEASE_SECONDS
    while resident(pid) > 0 and clock() < deadline:
        wait(0.01)
    return Ended(pid, task_run_id)


def task_message(limit: Limit) -> str:
    """Say what a task's user reads when the worker was stopped for memory.

    Parameters
    ----------
    limit
        The limit that was exceeded.

    Returns
    -------
    str
        The failure text.
    """
    return (
        f"the worker running this task was stopped for using more than its memory limit "
        f"({limit.text}). Use less data in the task (narrow its inputs with an upstream SQL "
        f"asset), or ask the operator to raise the limit ({WORKER_LIMIT_HINT})."
    )


def task_ended_message(limit: Limit) -> str:
    """Say what a task's user reads when the watchdog ended that task, not the worker.

    Parameters
    ----------
    limit
        The limit that was exceeded.

    Returns
    -------
    str
        The failure text.
    """
    return (
        f"this task was ended because the worker exceeded its memory limit ({limit.text}) and "
        f"the task was the largest consumer. Use less data in the task "
        f"(narrow its inputs with an upstream SQL asset), or ask the operator to raise the "
        f"limit ({WORKER_LIMIT_HINT})."
    )


WORKER_LIMIT_HINT = f"{SETTING}, or LAKANA_WORKER_MEMORY in the Compose bundle's .env"
"""Where an operator raises the worker's limit."""
WORKER_MEMORY_ADVICE = (
    "Read or compute less (filter or aggregate earlier, or select fewer columns), or ask the "
    f"operator to raise the worker's memory limit ({WORKER_LIMIT_HINT})."
)
"""What to do when the watchdog stopped the whole worker."""
MACHINE_MEMORY_ADVICE = (
    "Read or compute less (filter or aggregate earlier, or select fewer columns), or run it on a "
    "machine or in a container with more memory."
)
"""What to do about a `MemoryError`: the machine or the container itself had no memory left."""


def record_memory_failure(
    session_factory: Callable[[], Session],
    limit: Limit,
    rand: Callable[[], float],
    worker_id: str,
    activity: Activity = ACTIVITY,
    peak: bool = False,
    earlier: tuple[uuid.UUID, ...] = (),
    earlier_tasks: tuple[TaskClaim, ...] = (),
) -> None:
    """Fail what the worker is running, so the user sees why.

    Every query run in flight, and every `RUNNING` query run of a task in flight (a SQL task's
    write run is created before `run_one` registers it), becomes `FAILED` with cause
    `out_of_memory`, final; except a run whose write had already committed (it has `rows_written`
    and is not maintenance; a SQL task's own write included), which becomes `SUCCEEDED`: "failed,
    run it again" would append twice. No audit event is written for either.
    A task run's attempt ends as a failure that counts against
    `max_attempts`, except an attempt whose own write run had already committed: that
    attempt ends `succeeded` with a message that says why, and is not retried. For a task
    whose asset has checks neither holds once its write is in the table (stamped, or
    its write run shows the evidence; a rolled-back task has only the evidence): the attempt ends
    as a transient failure whatever the blame (`record_attempt(stopped=True)` decides it on the
    locked row), the task goes back to pending (it cannot run out of attempts) and the next
    claim runs its checks. A Python task with checks that may have written (it recorded a baseline
    and has no stamp) is not ended by a stop that ends it for good (a certain blame, or the last
    attempt): `record_attempt` hands it back, ready with `setting_back` and the stop's message, so
    the next worker sets the table back and ends it. The blame is
    certain, and the attempt `PERMANENT`, when the worker was running exactly one task and
    nothing but that task's own query runs: the same task would need the same memory again.
    When an unrelated query was in flight too, the attempt is `TRANSIENT` and the task is retried
    while attempts are left.

    Parameters
    ----------
    session_factory
        Opens a short-lived database session.
    limit
        The limit that was exceeded.
    rand
        Jitter source for the retry backoff.
    worker_id
        This worker's id. Only query runs it holds (`QueryRun.worker_id`) are ended: an entry
        kept after its thread left can name a run that another worker has since claimed, which
        the stop window of about 2 s must not fail (#171).
    activity
        What is in flight.
    peak
        Whether the measurement is a peak that never falls (macOS): a spike from a query that
        already finished can then be blamed on the task that follows, so the blame is never
        certain and the attempt is always `TRANSIENT`.
    earlier
        Query runs that were in flight before the watchdog ended the largest query. The query
        it ended may have left `activity` by the time the worker stops, and its run would stay
        `RUNNING` for ever; the update touches `RUNNING` rows only, so a run that was already
        recorded is left alone.
    earlier_tasks
        Claims of the task the watchdog ended before it came to stop the worker. That
        task's own thread records its transient failure through the normal outcome path, unless
        the stop began first (`Activity.stopping`: the thread then records nothing), and by
        then it has usually left `activity`. It is recorded here, once: the update is
        conditional on the task still running under that claim, so a task whose own thread
        recorded it already is left alone. It always gets the transient failure with
        `task_ended_message`, never the certain blame: the worker's own memory may be what
        stays over the limit.
    """
    queries, tasks = activity.snapshot()
    queries = tuple(dict.fromkeys((*queries, *earlier)))
    ended = {claim.task_run_id for claim in earlier_tasks}
    tasks = tuple(dict.fromkeys((*tasks, *earlier_tasks)))
    task_ids = [task.task_run_id for task in tasks]
    with session_factory() as session:
        foreign = 0
        if queries:
            foreign = len(
                session.execute(
                    select(QueryRun.id).where(
                        QueryRun.id.in_(queries),
                        or_(QueryRun.task_run_id.is_(None), QueryRun.task_run_id.notin_(task_ids)),
                    )
                ).all()
            )
        mine = QueryRun.id.in_(queries) if queries else false()
        if task_ids:
            mine = or_(mine, QueryRun.task_run_id.in_(task_ids))
        mine = and_(mine, QueryRun.worker_id == worker_id)
        if queries or task_ids:
            now = db_now(session)
            # A write whose commit left its evidence on the run (`rows_written`) landed, so it
            # ends SUCCEEDED, exactly as the sweep ends a lost run; the predicate is the
            # sweep's. Each statement carries the evidence in its own WHERE: a commit that
            # lands between the two leaves the run RUNNING for the sweep, never FAILED.
            session.execute(
                update(QueryRun)
                .where(mine, QueryRun.status == QueryRunStatus.RUNNING, committed_write())
                .values(status=QueryRunStatus.SUCCEEDED, finished_at=now)
                .execution_options(synchronize_session=False)
            )
            session.execute(
                update(QueryRun)
                .where(mine, QueryRun.status == QueryRunStatus.RUNNING, ~committed_write())
                .values(
                    status=QueryRunStatus.FAILED,
                    error_message=query_message(limit),
                    error_cause="out_of_memory",
                    finished_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            session.commit()
        certain = len(tasks) == 1 and foreign == 0 and not peak
        for task in tasks:
            was_ended = task.task_run_id in ended
            record_attempt(
                session,
                task.task_run_id,
                task.worker,
                task.claim,
                kind=FailureKind.PERMANENT if certain and not was_ended else FailureKind.TRANSIENT,
                rand=rand,
                error_message=task_ended_message(limit) if was_ended else task_message(limit),
                failure_code=FailureCode.MEMORY,
                on_committed=COMMITTED_BEFORE_STOPPED_MESSAGE,
                stopped=True,
            )


def start_watchdog(
    configured: str | None,
    session_factory: Callable[[], Session],
    rand: Callable[[], float],
    worker_id: str,
    platform: str = sys.platform,
) -> MemoryWatchdog | None:
    """Resolve the limit, say which applies and start the watchdog.

    Parameters
    ----------
    configured
        `LAKANA_WORKER__MEMORY_LIMIT`, or `None` when it is not set.
    session_factory
        Opens a database session for recording the failure.
    rand
        Jitter source for the retry backoff.
    worker_id
        This worker's id, handed to `record_memory_failure`.
    platform
        `sys.platform`; on macOS the measurement is a peak.

    Returns
    -------
    MemoryWatchdog or None
        The running watchdog, or `None` when there is no limit or no way to measure.
    """
    limit = resolve_limit(configured)
    if limit.bytes is None:
        logger.info("Memory limit: off (%s).", SETTING)
        return None
    platform_tools = tools(platform)
    if platform_tools is None:
        logger.warning(
            "Memory limit: %s is not enforced on this platform: the worker cannot measure its "
            "memory here.",
            limit.describe(),
        )
        return None
    measure, kill_children = platform_tools
    note = "; on macOS it covers the worker process only" if platform == "darwin" else ""
    logger.info("Memory limit: %s%s.", limit.describe(), note)
    if limit.derived and limit.bytes < MIN_WORKER_MEMORY:
        logger.warning(
            "The container's memory limit is too small for a worker: the worker's limit is %s, "
            "and a worker needs at least 512 MB to start. Set LAKANA_WORKER_MEMORY to at least 1g.",
            limit.text,
        )

    before_kill: list[tuple[uuid.UUID, ...]] = [()]
    ended_tasks: list[tuple[TaskClaim, ...]] = [()]

    def relieve(total: int) -> bool:
        """End the largest query or Python task, after noting what is in flight.

        Parameters
        ----------
        total
            Measured resident bytes.

        Returns
        -------
        bool
            Whether a query or a task was ended.
        """
        queries, claims = ACTIVITY.snapshot()
        before_kill[0] = queries
        ended_tasks[0] = ()
        ended = stop_largest_consumer(total, limit)
        if ended is None:
            return False
        if ended.task_run_id is not None:
            ended_tasks[0] = tuple(c for c in claims if c.task_run_id == ended.task_run_id)
        return True

    def on_exceeded(total: int) -> None:
        """Stop the worker.

        Parameters
        ----------
        total
            Measured resident bytes.
        """
        stop_worker(
            total,
            limit,
            record=lambda: record_memory_failure(
                session_factory,
                limit,
                rand,
                worker_id,
                peak=platform == "darwin",
                earlier=before_kill[0],
                earlier_tasks=ended_tasks[0],
            ),
            kill_children=kill_children,
            exit_process=os._exit,
        )

    return MemoryWatchdog(
        limit.bytes,
        measure,
        on_exceeded,
        relieve=relieve if platform.startswith("linux") else None,
    ).start_in_background()
