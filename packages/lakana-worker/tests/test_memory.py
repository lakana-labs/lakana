"""Tests for the worker's memory limit and its watchdog."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_query_process import _child, _rows
from test_runners import Harness, _alive

from lakana_core.config import QueryLimitsConfig, parse_memory_limit
from lakana_core.runs.failures import FailureCode
from lakana_worker import memory
from lakana_worker.activity import Activity, TaskClaim
from lakana_worker.executor import OutOfMemoryQuery, execute_query
from lakana_worker.memory import (
    EXIT_CODE,
    Limit,
    MemoryWatchdog,
    ProcessTree,
    descendants,
    read_cgroup_limit,
    record_memory_failure,
    resident_bytes,
    resolve_limit,
    start_watchdog,
    stop_worker,
    tools,
)
from lakana_worker.query_process import REGISTRY, ChildFailed, StopReason
from lakana_worker.runners import RunOutcome

MB = 1024 * 1024
GB = 1024**3


def test_the_watchdog_checks_five_times_a_second_and_gives_recording_two_seconds() -> None:
    """One thread can make gigabytes resident in a second; the numbers are the review's."""
    assert pytest.approx(0.2) == memory.INTERVAL_SECONDS
    assert pytest.approx(2.0) == memory.RECORD_TIMEOUT_SECONDS


def test_the_session_scan_survives_the_proc_directory_disappearing(tmp_path: Path) -> None:
    """A vanished ``/proc`` during the scan leaves nothing but the walked descendants."""
    tree = ProcessTree(100, tmp_path / "missing", 1)
    tree._sessions.add(5)
    assert tree.members() == []


def test_the_exit_code_is_pinned() -> None:
    """The local-mode supervisor repeats this number; `test_local_supervisor` pins its copy."""
    assert EXIT_CODE == 75


def _process(
    proc: Path,
    pid: int,
    pages: int,
    parent: int | None = None,
    *,
    children_file: bool = True,
    session: int = 1,
) -> None:
    """Add a fake process to a fake ``/proc``.

    Parameters
    ----------
    proc
        The fake directory.
    pid
        The process id.
    pages
        Resident pages it reports.
    parent
        Its parent, if any.
    children_file
        Whether the kernel offers ``task/<pid>/children``.
    session
        Its process group and session id.
    """
    directory = proc / str(pid)
    (directory / "task" / str(pid)).mkdir(parents=True)
    (directory / "statm").write_text(f"100 {pages} 10 1 0 5 0\n")
    (directory / "stat").write_text(f"{pid} (a b) S {parent or 1} {session} {session}\n")
    if children_file:
        (directory / "task" / str(pid) / "children").write_text("")
        file = proc / str(parent) / "task" / str(parent) / "children"
        if parent is not None and file.exists():
            file.write_text(file.read_text() + f"{pid} ")


def _tree(proc: Path, *, children_file: bool = True) -> None:
    """Build 100 -> (101 -> 103, 102); 200 is somebody else's.

    Parameters
    ----------
    proc
        The fake ``/proc``.
    children_file
        Whether the kernel offers ``children`` files.
    """
    _process(proc, 100, 10, children_file=children_file)
    _process(proc, 101, 20, 100, children_file=children_file)
    _process(proc, 102, 30, 100, children_file=children_file)
    _process(proc, 103, 40, 101, children_file=children_file)
    _process(proc, 200, 1000, children_file=children_file)


@pytest.mark.parametrize("children_file", [True, False])
def test_resident_memory_sums_the_process_and_its_descendants(
    tmp_path: Path, children_file: bool
) -> None:
    """The worker's number counts its tasks and their children, and nobody else.

    Without ``children`` files the scan of ``stat`` finds the same tree.
    """
    _tree(tmp_path, children_file=children_file)
    assert sorted(descendants(100, tmp_path)) == [101, 102, 103]
    assert resident_bytes(100, tmp_path, page_size=4096) == (10 + 20 + 30 + 40) * 4096


def test_a_loop_in_the_process_tree_is_not_followed_for_ever(tmp_path: Path) -> None:
    """A child that lists the root (or an earlier process) as its own child ends the walk."""
    _tree(tmp_path)
    (tmp_path / "103" / "task" / "103" / "children").write_text("100 101 ")
    assert sorted(descendants(100, tmp_path)) == [101, 102, 103]


def test_a_process_that_vanished_counts_for_nothing(tmp_path: Path) -> None:
    """A missing or garbled ``statm`` and a missing process are 0, never an error."""
    _tree(tmp_path)
    (tmp_path / "102" / "statm").write_text("garbage")
    (tmp_path / "103" / "statm").unlink()
    assert resident_bytes(100, tmp_path, page_size=1) == 10 + 20
    assert descendants(999, tmp_path) == []
    assert resident_bytes(999, tmp_path, page_size=1) == 0


def test_the_scan_skips_what_it_cannot_read(tmp_path: Path) -> None:
    """Non-numeric entries and unreadable or garbled ``stat`` files are ignored."""
    _tree(tmp_path, children_file=False)
    (tmp_path / "self").mkdir()
    (tmp_path / "300").mkdir()
    (tmp_path / "301").mkdir()
    (tmp_path / "301" / "stat").write_text("301 (x) S")
    assert sorted(descendants(100, tmp_path)) == [101, 102, 103]
    assert memory._children_by_scan(1, tmp_path / "missing") == []


def test_resident_memory_of_this_process_is_measured() -> None:
    """The real ``/proc`` works, on Linux."""
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux only")
    assert resident_bytes() > 10 * MB
    assert ProcessTree().resident() > 10 * MB


def test_kill_sends_sigkill_to_every_descendant_and_ignores_the_gone(tmp_path: Path) -> None:
    """SIGKILL goes to every descendant, never to the worker; a vanished one is fine."""
    _tree(tmp_path)
    killed: list[int] = []

    def kill(pid: int, sig: int) -> None:
        killed.append(pid)
        if pid == 102:
            raise ProcessLookupError

    ProcessTree(100, tmp_path, 1, scan_interval=0.0, killpg=lambda g, s: None, kill=kill).kill()
    assert sorted(killed) == [101, 102, 103]


def test_an_orphaned_grandchild_is_counted_and_killed_through_its_session(tmp_path: Path) -> None:
    """A task's session is remembered: its member whose parent exited is still the worker's.

    101 leads session 101 and is seen at the first measurement; later it exits, and 150 (its
    child, now a child of init) is found only through the session. The worker's own session
    is never touched.
    """
    _process(tmp_path, 100, 10, session=7)
    _process(tmp_path, 101, 20, 100, session=101)
    _process(tmp_path, 300, 5000, session=7)
    groups: list[int] = []
    killed: list[int] = []
    tree = ProcessTree(
        100,
        tmp_path,
        1,
        scan_interval=0.0,
        killpg=lambda g, s: groups.append(g),
        kill=lambda p, s: killed.append(p),
    )
    assert tree.resident() == 10 + 20
    # 101 exits; its child 150 stays in session 101 under init.
    import shutil

    shutil.rmtree(tmp_path / "101")
    (tmp_path / "100" / "task" / "100" / "children").write_text("")
    _process(tmp_path, 150, 40, 1, session=101)
    assert tree.members() == [150]
    assert tree.resident() == 10 + 40
    tree.kill()
    assert groups == [101] and killed == [150]
    # Once no member is left, the session is forgotten.
    shutil.rmtree(tmp_path / "150")
    assert tree.members() == []
    groups.clear()
    tree.kill()
    assert groups == []


def test_a_group_that_is_already_gone_is_ignored_when_killing(tmp_path: Path) -> None:
    """`killpg` failing for a vanished group does not stop the rest."""
    _process(tmp_path, 100, 10, session=7)
    _process(tmp_path, 101, 20, 100, session=101)
    killed: list[int] = []

    def killpg(group: int, sig: int) -> None:
        raise ProcessLookupError

    ProcessTree(
        100, tmp_path, 1, scan_interval=0.0, killpg=killpg, kill=lambda p, s: killed.append(p)
    ).kill()
    assert killed == [101]


def test_the_session_scan_ignores_unreadable_entries(tmp_path: Path) -> None:
    """A non-numeric entry or a process that vanished mid-scan is skipped."""
    _process(tmp_path, 100, 10, session=7)
    _process(tmp_path, 101, 20, 100, session=101)
    (tmp_path / "self").mkdir()
    (tmp_path / "999").mkdir()
    tree = ProcessTree(100, tmp_path, 1, scan_interval=0.0)
    assert tree.members() == [101]


def test_tools_by_platform() -> None:
    """Linux measures the tree and can kill it; Windows measures nothing."""
    linux = tools("linux")
    assert linux is not None and callable(linux[0]) and callable(linux[1])
    assert tools("win32") is None


def _cgroup(tmp_path: Path, own: str, files: dict[str, str]) -> tuple[Path, Path]:
    """Build a fake cgroup mount and ``/proc/self/cgroup``.

    Parameters
    ----------
    tmp_path
        Scratch directory.
    own
        The contents of ``/proc/self/cgroup``.
    files
        Limit files, by path below the mount.

    Returns
    -------
    tuple of Path
        The mount and the ``/proc/self/cgroup`` file.
    """
    root = tmp_path / "cgroup"
    root.mkdir()
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    self_cgroup = tmp_path / "self_cgroup"
    self_cgroup.write_text(own)
    return root, self_cgroup


@pytest.mark.parametrize(
    ("own", "files", "expected"),
    [
        # the limit at the leaf
        ("0::/a/b\n", {"a/b/memory.max": "2147483648\n"}, 2 * GB),
        # at a parent only
        ("0::/a/b\n", {"a/memory.max": "1073741824\n", "a/b/memory.max": "max\n"}, GB),
        # at both: the smallest wins, whichever level it is on
        ("0::/a/b\n", {"a/memory.max": "1073741824\n", "a/b/memory.max": "4294967296\n"}, GB),
        ("0::/a/b\n", {"a/memory.max": "4294967296\n", "a/b/memory.max": "1073741824\n"}, GB),
        # at the mount root (a private cgroup namespace shows `/`)
        ("0::/\n", {"memory.max": "4294967296\n"}, 4 * GB),
        # none
        ("0::/a/b\n", {"a/b/memory.max": "max\n", "memory.max": "max\n"}, None),
        ("0::/a/b\n", {}, None),
        ("0::/a/b\n", {"a/b/memory.max": "lots"}, None),
        ("0::/a/b\n", {"a/b/memory.max": "0"}, None),
        ("0::/a/b\n", {"a/b/memory.max": "\uff11\uff12"}, None),
        # cgroup v1, the memory controller's line; its huge number means none
        (
            "12:cpu:/x\n4:memory:/m/n\n",
            {"memory/m/n/memory.limit_in_bytes": "2147483648\n"},
            2 * GB,
        ),
        (
            "4:memory:/m\n",
            {"memory/m/memory.limit_in_bytes": "9223372036854771712\n"},
            None,
        ),
        # hybrid: the smaller of v2 and v1
        (
            "4:memory:/m\n0::/a\n",
            {"memory/m/memory.limit_in_bytes": "1073741824\n", "a/memory.max": "4294967296\n"},
            GB,
        ),
    ],
)
def test_the_cgroup_limit(
    tmp_path: Path, own: str, files: dict[str, str], expected: int | None
) -> None:
    """Find the process's own cgroup; the smallest limit up to the mount root wins.

    `max`, v1's huge number, a missing file and garbage mean none.
    """
    root, self_cgroup = _cgroup(tmp_path, own, files)
    assert read_cgroup_limit(root, self_cgroup) == expected


def test_an_unreadable_proc_self_cgroup_falls_back_to_the_mount_root(tmp_path: Path) -> None:
    """A container with its own cgroup namespace shows its limit at the root."""
    root, _ = _cgroup(tmp_path, "", {"memory.max": "2147483648\n"})
    assert read_cgroup_limit(root, tmp_path / "missing") == 2 * GB


def test_a_cgroup_path_leading_outside_the_mount_is_not_followed(tmp_path: Path) -> None:
    """`..` in the path is refused, so a limit file outside the mount is never read."""
    root, self_cgroup = _cgroup(tmp_path, "0::/../outside\n", {"memory.max": "1073741824\n"})
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "memory.max").write_text("1073741824\n")
    assert read_cgroup_limit(root, self_cgroup) is None


def test_the_limit_is_the_setting_else_ninety_percent_of_the_container_else_four_gigabytes() -> (
    None
):
    """Which limit applies, and what the start-up line says about it."""
    explicit = resolve_limit("2g", lambda: 4 * GB)
    assert (explicit.bytes, explicit.describe()) == (2 * GB, "2g (LAKANA_WORKER__MEMORY_LIMIT)")
    off = resolve_limit("off", lambda: 4 * GB)
    assert (off.bytes, off.text) == (None, "off")
    derived = resolve_limit(None, lambda: 4 * GB)
    assert derived.bytes == int(4 * GB * 0.9)
    assert derived.describe() == "3686 MB, 90% of the container's 4096 MB"
    default = resolve_limit(None, lambda: None)
    assert (default.bytes, default.describe()) == (4 * GB, "4GB (the default)")


def test_the_default_cgroup_reader_is_used_when_no_seam_is_given() -> None:
    """`resolve_limit` runs unpatched; whatever the host says, the answer is a limit."""
    assert resolve_limit(None).bytes is not None


# -- the watchdog ------------------------------------------------------------------------------


def test_the_watchdog_acts_only_over_the_limit() -> None:
    """At the limit nothing happens; one byte over, the handler gets the measurement."""
    seen: list[int] = []
    sizes = iter([100, 101])
    dog = MemoryWatchdog(100, lambda: next(sizes), seen.append)
    assert dog.check_once() is False
    assert dog.check_once() is True
    assert seen == [101]


def test_the_loop_survives_an_exception_and_logs_it_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing measurement is logged the first time and the loop goes on measuring."""
    calls = [0]

    def measure() -> int:
        calls[0] += 1
        raise OSError("boom")

    waits = iter([False, False, False, True])
    dog = MemoryWatchdog(1, measure, lambda total: None, wait=lambda seconds: next(waits))
    with caplog.at_level(logging.ERROR):
        dog.run()
    assert calls[0] == 3
    assert [r.message for r in caplog.records].count(
        "Memory watchdog failed; it keeps running."
    ) == 1


def test_the_watchdog_thread_starts_and_stops() -> None:
    """A real thread: daemon, named, ends when told."""
    dog = MemoryWatchdog(10**12, lambda: 0, lambda total: None, interval=0.01)
    thread = dog.start()
    assert thread.daemon and thread.name == "memory-watchdog"
    dog.stop()
    thread.join(5)
    assert not thread.is_alive()


LIMIT = Limit(4 * GB, "4GB", "LAKANA_WORKER__MEMORY_LIMIT")


def test_stopping_logs_records_kills_children_then_exits(caplog: pytest.LogCaptureFixture) -> None:
    """The order is the contract: the line, the record, the children, the exit."""
    order: list[str] = []
    with caplog.at_level(logging.ERROR):
        stop_worker(
            5 * GB + 3 * MB,
            LIMIT,
            record=lambda: order.append("record"),
            kill_children=lambda: order.append("kill"),
            exit_process=lambda code: order.append(f"exit {code}"),
            mark_stopping=lambda: order.append("stopping"),
        )
    assert order == ["stopping", "kill", "record", f"exit {EXIT_CODE}"]
    assert caplog.records[0].event == "memory_stop"  # ty: ignore[unresolved-attribute]
    assert caplog.messages[0] == (
        "Memory limit exceeded: the worker and its tasks use 5123 MB, the limit is 4GB "
        "(LAKANA_WORKER__MEMORY_LIMIT). The worker stops now. What it was running is marked "
        "failed; a task is retried if it has attempts left."
    )


def test_the_exceeded_line_points_a_bundle_operator_at_the_setting_they_changed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Derived from the container: the line says to raise the container's limit."""
    derived = Limit(int(4 * GB * 0.9), "3686 MB", "90% of the container's 4096 MB", derived=True)
    with caplog.at_level(logging.ERROR):
        stop_worker(
            5 * GB,
            derived,
            record=lambda: None,
            kill_children=lambda: None,
            exit_process=lambda code: None,
        )
    assert (
        "the limit is 3686 MB, 90% of the container's 4096 MB; raise the container's memory "
        "limit (in the Compose bundle: LAKANA_WORKER_MEMORY in .env). The worker stops now."
        in caplog.messages[0]
    )


def test_a_failing_record_or_kill_does_not_prevent_the_exit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each step may fail; the process still ends."""
    exits: list[int] = []

    def boom() -> None:
        raise RuntimeError("database down")

    with caplog.at_level(logging.ERROR):
        stop_worker(GB, LIMIT, record=boom, kill_children=boom, exit_process=exits.append)
    assert exits == [EXIT_CODE]
    assert "recording the failure failed" in caplog.text
    assert "killing the worker's children failed" in caplog.text


def test_a_recording_thread_that_cannot_start_does_not_prevent_the_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Under memory pressure a new thread may not start; the worker must exit all the same.

    Otherwise the watchdog would keep looping with `stopping` set, and every later outcome
    would be dropped.

    Parameters
    ----------
    monkeypatch
        Replaces the thread class `stop_worker` uses.
    caplog
        Captures the log.
    """

    class Unstartable(threading.Thread):
        """A thread that cannot start, as when the system has no memory for one."""

        def start(self) -> None:
            """Fail to start.

            Raises
            ------
            RuntimeError
                Always.
            """
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(memory, "threading", SimpleNamespace(Thread=Unstartable))
    exits: list[int] = []
    with caplog.at_level(logging.ERROR):
        stop_worker(
            GB,
            LIMIT,
            record=lambda: None,
            kill_children=lambda: None,
            exit_process=exits.append,
            mark_stopping=lambda: None,
        )
    assert exits == [EXIT_CODE]
    assert "Recording the memory failure could not start" in caplog.text


def test_a_recording_that_hangs_is_abandoned_after_the_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A database that never answers cannot keep a worker over its limit alive."""
    release = threading.Event()
    order: list[str] = []

    def hang() -> None:
        release.wait(30)

    started = time.monotonic()
    with caplog.at_level(logging.ERROR):
        stop_worker(
            GB,
            LIMIT,
            record=hang,
            kill_children=lambda: order.append("kill"),
            exit_process=lambda code: order.append("exit"),
            record_timeout=0.05,
        )
    elapsed = time.monotonic() - started
    release.set()
    assert order == ["kill", "exit"]
    assert "took more than" in caplog.text
    assert elapsed < 2.0


# -- recording ---------------------------------------------------------------------------------


def test_the_failure_messages_name_the_limit_and_the_next_step() -> None:
    """What a user reads: the limit, and which setting to raise."""
    query = memory.query_message(LIMIT)
    task = memory.task_message(LIMIT)
    for text in (query, task):
        assert "4GB" in text
        assert "LAKANA_WORKER__MEMORY_LIMIT" in text
        assert "LAKANA_WORKER_MEMORY in the Compose bundle's .env" in text
    assert query.startswith("Query failed: the worker ran out of memory and was stopped")
    assert "LAKANA_QUERY_LIMITS__MEMORY_LIMIT" not in query


class _FakeSessions:
    """A session factory whose sessions do nothing."""

    def __call__(self) -> _FakeSessions:
        """Open a session.

        Returns
        -------
        _FakeSessions
            Itself.
        """
        return self

    def __enter__(self) -> _FakeSessions:
        """Enter the session.

        Returns
        -------
        _FakeSessions
            Itself.
        """
        return self

    def __exit__(self, *exc: object) -> None:
        """Close the session.

        Parameters
        ----------
        *exc
            The exception, if any.
        """


FAKE_SESSIONS: Any = _FakeSessions()


def test_nothing_in_flight_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle worker over its limit touches no row."""
    monkeypatch.setattr(memory, "record_attempt", lambda *a, **k: pytest.fail("no task"))
    record_memory_failure(FAKE_SESSIONS, LIMIT, lambda: 0.5, "w1", Activity())


def test_the_activity_registry_marks_and_clears() -> None:
    """Entries exist inside the block and are gone after it, even after an error.

    Raises
    ------
    RuntimeError
        Deliberately, inside the block.
    """
    activity = Activity()
    run = uuid.uuid4()
    claim = TaskClaim(uuid.uuid4(), "w", 1)
    with pytest.raises(RuntimeError), activity.query(run), activity.task(claim):
        assert activity.snapshot() == ((run,), (claim,))
        raise RuntimeError
    assert activity.snapshot() == ((), ())


def test_what_leaves_the_registry_once_the_worker_stops_stays_in_its_snapshot() -> None:
    """#154: a task and a query that end after the stop began are still recorded by the stop.

    The watchdog marks the worker stopping before it kills the children; a thread whose child
    dies then leaves its block without recording anything, and the recording that follows
    must still find it. A new process starts empty: clearing `stopping` drops them.
    """
    activity = Activity()
    run = uuid.uuid4()
    claim = TaskClaim(uuid.uuid4(), "w", 1)
    with activity.query(run), activity.task(claim):
        activity.stopping = True
    assert activity.snapshot() == ((run,), (claim,))
    activity.stopping = False
    assert activity.snapshot() == ((), ())


def test_the_stop_records_a_task_whose_thread_left_it_before_the_recording_began() -> None:
    """#154: the worker's thread leaves its task between the kill and the recording.

    The thread holds a task until its child is killed, then returns without recording
    (`Activity.stopping`), as `run_claimed` does. The kill waits for it to leave, so the
    recording always starts after that: it must still see the task.
    """
    activity = Activity()
    claim = TaskClaim(uuid.uuid4(), "w", 1)
    entered, killed = threading.Event(), threading.Event()

    def worker() -> None:
        """Hold the task until the child is killed."""
        with activity.task(claim):
            entered.set()
            killed.wait(10)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert entered.wait(10)
    recorded: list[tuple[TaskClaim, ...]] = []

    def kill_children() -> None:
        """Kill the child, and let the worker's thread leave its task."""
        killed.set()
        thread.join(10)
        assert not thread.is_alive()

    def mark_stopping() -> None:
        """Mark the worker stopping, as the registry's default does."""
        activity.stopping = True

    stop_worker(
        GB,
        LIMIT,
        record=lambda: recorded.append(activity.snapshot()[1]),
        kill_children=kill_children,
        exit_process=lambda code: None,
        mark_stopping=mark_stopping,
    )
    assert recorded == [(claim,)]


# -- starting it -------------------------------------------------------------------------------


def test_start_watchdog_logs_the_limit_and_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The start-up line says what applies and where it came from; a thread runs."""
    started: list[int] = []
    monkeypatch.setattr(MemoryWatchdog, "start", lambda self: started.append(1))
    monkeypatch.setattr(memory, "read_cgroup_limit", lambda: None)
    monkeypatch.setattr(memory, "tools", lambda *a: (lambda: 0, lambda: None))
    with caplog.at_level(logging.INFO):
        dog = start_watchdog("4GB", FAKE_SESSIONS, lambda: 0.5, "w1", platform="linux")
    assert dog is not None and started == [1]
    assert "Memory limit: 4GB (LAKANA_WORKER__MEMORY_LIMIT)." in caplog.messages


def test_on_macos_the_start_up_line_says_the_limit_covers_the_worker_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The measurement there is the worker's own peak, and the line says so."""
    monkeypatch.setattr(MemoryWatchdog, "start", lambda self: None)
    monkeypatch.setattr(memory, "read_cgroup_limit", lambda: None)
    monkeypatch.setattr(memory, "tools", lambda *a: (lambda: 0, lambda: None))
    with caplog.at_level(logging.INFO):
        assert (
            start_watchdog("4GB", FAKE_SESSIONS, lambda: 0.5, "w1", platform="darwin") is not None
        )
    assert (
        "Memory limit: 4GB (LAKANA_WORKER__MEMORY_LIMIT); on macOS it covers the worker "
        "process only." in caplog.messages
    )


def test_a_derived_limit_below_the_floor_warns_with_the_minimum(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A 256m container gives 230 MB: the worker starts and says what the container needs."""
    monkeypatch.setattr(MemoryWatchdog, "start", lambda self: None)
    monkeypatch.setattr(memory, "read_cgroup_limit", lambda: 256 * MB)
    monkeypatch.setattr(memory, "tools", lambda *a: (lambda: 0, lambda: None))
    with caplog.at_level(logging.INFO):
        assert start_watchdog(None, FAKE_SESSIONS, lambda: 0.5, "w1", platform="linux") is not None
    assert "Memory limit: 230 MB, 90% of the container's 256 MB." in caplog.messages
    assert any(
        "at least 512 MB" in m and "LAKANA_WORKER_MEMORY to at least 1g" in m
        for m in caplog.messages
    )


def test_start_watchdog_with_the_limit_off_or_nothing_to_measure_with(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`off` and Windows start nothing and say so in one line."""
    with caplog.at_level(logging.INFO):
        assert start_watchdog("off", FAKE_SESSIONS, lambda: 0.5, "w1") is None
        monkeypatch.setattr(memory, "tools", lambda *a: None)
        assert start_watchdog("4GB", FAKE_SESSIONS, lambda: 0.5, "w1") is None
    assert "Memory limit: off (LAKANA_WORKER__MEMORY_LIMIT)." in caplog.messages
    assert any("not enforced on this platform" in m for m in caplog.messages)


def test_the_handler_start_watchdog_installs_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over the limit, the installed handler calls `stop_worker` with the record and the kill."""
    handlers: list[object] = []
    monkeypatch.setattr(MemoryWatchdog, "start", lambda self: handlers.append(self._on_exceeded))
    monkeypatch.setattr(memory, "tools", lambda *a: (lambda: 0, lambda: None))
    stopped: list[tuple[int, Limit]] = []

    def fake_stop(total: int, limit: Limit, **steps: object) -> None:
        stopped.append((total, limit))
        record = steps["record"]
        assert callable(record)
        record()

    monkeypatch.setattr(memory, "stop_worker", fake_stop)
    recorded: list[Limit] = []
    monkeypatch.setattr(
        memory,
        "record_memory_failure",
        lambda sf, limit, rand, worker_id, peak=False, earlier=(), earlier_tasks=(): (
            recorded.append(limit)
        ),
    )
    start_watchdog("1GB", FAKE_SESSIONS, lambda: 0.5, "w1")
    handler = handlers[0]
    assert callable(handler)
    handler(2 * GB)
    assert stopped[0][0] == 2 * GB and recorded == [stopped[0][1]]


# -- validation of the setting -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4GB", 4 * GB),
        ("4g", 4 * GB),
        ("4 GiB", 4 * GB),
        ("512MB", 512 * MB),
        ("512MiB", 512 * MB),
        ("3600m", 3600 * MB),
        ("1.5GB", int(1.5 * GB)),
        ("1T", 1024**4),
        ("1024T", 1024**5),
        ("0", None),
        ("0b", None),
        ("off", None),
        (" OFF ", None),
        ("Off", None),
    ],
)
def test_valid_limits(text: str, expected: int | None) -> None:
    """Sizes are binary; `0`, `0b` and `off` in any case switch the limit off."""
    assert parse_memory_limit(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "lots",
        "4",
        "-1GB",
        "4XB",
        "GB",
        "4GB extra",
        "0GB",
        "0.0001KB",
        "00",
        "\uff14GB",
        "9" * 400 + "GB",
        "9" * 40,
        "1025T",
        "1e3MB",
    ],
)
def test_garbage_is_refused_with_the_setting_named_and_never_overflows(text: str) -> None:
    """Every refusal is the worded error: the setting, an example, `off`."""
    with pytest.raises(ValueError, match="LAKANA_WORKER__MEMORY_LIMIT") as caught:
        parse_memory_limit(text)
    assert "4GB" in str(caught.value) and "off" in str(caught.value)


@pytest.mark.parametrize("text", ["511MB", "256MB", "100B", "0.4GB"])
def test_a_limit_below_the_floor_is_refused_with_the_reason(text: str) -> None:
    """A worker needs about 512 MB to start."""
    with pytest.raises(ValueError, match=r"at least 512MB.*needs about that much to start"):
        parse_memory_limit(text)


# -- the real thing ----------------------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="measures through /proc")
def test_a_real_watchdog_stops_a_process_that_holds_too_much_resident_memory() -> None:
    """A child with a 300 MB limit touches 400 MB and exits with the watchdog's code."""
    script = textwrap.dedent(
        """
        import time
        from lakana_worker import memory

        limit = memory.Limit(300 * 1024 * 1024, "300 MB", "test")
        dog = memory.MemoryWatchdog(
            limit.bytes,
            memory.resident_bytes,
            lambda total: memory.stop_worker(
                total, limit, record=lambda: None, kill_children=lambda: None,
                exit_process=__import__("os")._exit,
            ),
            interval=0.1,
        )
        dog.start()
        block = bytearray(400 * 1024 * 1024)
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        time.sleep(20)
        print("survived")
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == EXIT_CODE, (done.stdout, done.stderr)
    assert "survived" not in done.stdout


# -- what the tree may signal ------------------------------------------------------------------


def test_a_descendant_in_the_workers_own_session_is_never_remembered(tmp_path: Path) -> None:
    """Its session is the worker's: killing "its group" would kill the worker."""
    _process(tmp_path, 100, 10, session=7)
    _process(tmp_path, 101, 20, 100, session=7)
    _process(tmp_path, 300, 5000, session=7)
    killed_groups: list[int] = []
    tree = ProcessTree(
        100, tmp_path, 1, scan_interval=0.0, killpg=lambda g, s: killed_groups.append(g)
    )
    assert tree.members() == [101]
    tree.kill()
    assert killed_groups == [] and tree._sessions == set()


@pytest.mark.parametrize("session", [0, 1])
def test_a_descendant_reporting_session_zero_or_one_is_never_remembered(
    tmp_path: Path, session: int
) -> None:
    """Session 0 and 1 are the kernel's and init's: never a group to signal."""
    _process(tmp_path, 100, 10, session=7)
    _process(tmp_path, 101, 20, 100, session=session)
    _process(tmp_path, 300, 5000, session=session)
    groups: list[int] = []
    tree = ProcessTree(100, tmp_path, 1, scan_interval=0.0, killpg=lambda g, s: groups.append(g))
    assert tree.members() == [101]
    tree.kill()
    assert groups == [] and tree._sessions == set()


def test_a_command_name_with_a_closing_parenthesis_is_parsed(tmp_path: Path) -> None:
    """The fields are split after the last `)`, so `comm` may hold anything."""
    _process(tmp_path, 100, 10, session=7)
    _process(tmp_path, 101, 20, 100, session=101)
    (tmp_path / "101" / "stat").write_text("101 (a) R 1 1 1 (b) S 100 101 101\n")
    assert memory._ids(101, tmp_path) == (100, 101, 101)
    tree = ProcessTree(100, tmp_path, 1, scan_interval=0.0)
    tree.members()
    assert tree._sessions == {101}


def test_the_session_scan_runs_once_a_second_but_the_walk_every_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cadence, with an injected clock: a scan at most per interval, at once for a new session."""
    _process(tmp_path, 100, 10, session=7)
    _process(tmp_path, 101, 20, 100, session=101)
    now = [0.0]
    tree = ProcessTree(100, tmp_path, 1, clock=lambda: now[0], scan_interval=1.0)
    scans: list[int] = []
    real_scan = tree._scan
    monkeypatch.setattr(tree, "_scan", lambda: (scans.append(1), real_scan())[1])
    tree.members()
    assert len(scans) == 1  # a new session
    now[0] = 0.5
    tree.members()
    assert len(scans) == 1  # cached
    _process(tmp_path, 160, 30, 1, session=101)  # an orphan appears in the task's session
    assert tree.members() == [101]  # only a scan finds it, and the last one is cached
    now[0] = 1.0
    assert tree.members() == [101, 160]
    assert len(scans) == 2
    tree.kill()  # killing always looks afresh
    assert len(scans) == 3
    _process(tmp_path, 102, 5, 100, session=102)  # a new task session
    tree.members()
    assert len(scans) == 4


def test_without_a_session_nothing_is_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No remembered session, no scan of `/proc`."""
    _process(tmp_path, 100, 10, session=7)
    _process(tmp_path, 101, 20, 100, session=7)
    tree = ProcessTree(100, tmp_path, 1, scan_interval=0.0)
    monkeypatch.setattr(tree, "_scan", lambda: pytest.fail("no scan"))
    assert tree.members() == [101]


def test_the_stopping_flag_is_set_before_the_children_are_killed() -> None:
    """The default marker is the registry's flag; the kill sees it already set."""
    from lakana_worker.activity import ACTIVITY

    ACTIVITY.stopping = False
    seen: list[bool] = []
    try:
        stop_worker(
            GB,
            LIMIT,
            record=lambda: None,
            kill_children=lambda: seen.append(ACTIVITY.stopping),
            exit_process=lambda code: None,
        )
        assert seen == [True]
    finally:
        ACTIVITY.stopping = False


def test_on_macos_the_measurement_is_a_peak_so_the_blame_is_never_certain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform is injected: the recorder is told the measurement is a peak."""
    handlers: list[object] = []
    monkeypatch.setattr(MemoryWatchdog, "start", lambda self: handlers.append(self._on_exceeded))
    monkeypatch.setattr(memory, "tools", lambda platform: (lambda: 0, lambda: None))
    peaks: list[bool] = []
    monkeypatch.setattr(
        memory,
        "record_memory_failure",
        lambda sf, limit, rand, worker_id, peak=False, earlier=(), earlier_tasks=(): peaks.append(
            peak
        ),
    )
    monkeypatch.setattr(memory, "stop_worker", lambda total, limit, **steps: steps["record"]())
    for platform in ("linux", "darwin"):
        start_watchdog("1GB", FAKE_SESSIONS, lambda: 0.5, "w1", platform)
        handler = handlers[-1]
        assert callable(handler)
        handler(2 * GB)
    assert peaks == [False, True]


# -- the largest query first ---------------------------------------------------------------------


def _watchdog(
    readings: list[int], relieve: Callable[[int], bool] | None
) -> tuple[MemoryWatchdog, list[int]]:
    """Build a watchdog with a limit of 100 that measures `readings` in turn.

    Parameters
    ----------
    readings
        What each measurement returns.
    relieve
        The relief, or `None` for none.

    Returns
    -------
    tuple
        The watchdog and the list of totals the worker was stopped with.
    """
    stopped: list[int] = []
    measured = iter(readings)
    dog = MemoryWatchdog(100, lambda: next(measured), stopped.append, relieve=relieve)
    return dog, stopped


def test_over_the_limit_a_query_is_stopped_and_the_worker_goes_on() -> None:
    asked: list[int] = []
    dog, stopped = _watchdog([500, 80], lambda total: asked.append(total) or True)
    assert dog.check_once() is True
    assert (asked, stopped) == ([500], [])


def test_the_worker_stops_when_stopping_the_query_was_not_enough() -> None:
    dog, stopped = _watchdog([500, 450], lambda total: True)
    assert dog.check_once() is True
    assert stopped == [450]


def test_the_worker_stops_as_before_when_no_query_is_running() -> None:
    dog, stopped = _watchdog([500], lambda total: False)
    assert dog.check_once() is True
    assert stopped == [500]


def test_without_a_relief_the_worker_stops_as_before() -> None:
    dog, stopped = _watchdog([500], None)
    assert dog.check_once() is True
    assert stopped == [500]


def test_a_relief_that_raises_never_keeps_the_worker_from_stopping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def relieve(total: int) -> bool:
        raise OSError("boom")

    dog, stopped = _watchdog([500], relieve)
    with caplog.at_level("ERROR", logger="lakana_worker.memory"):
        assert dog.check_once() is True
    assert stopped == [500]
    assert "ending a query or task failed; the worker stops instead" in caplog.text


def test_under_the_limit_nothing_is_asked() -> None:
    dog, stopped = _watchdog([100], lambda total: pytest.fail("not over the limit"))
    assert dog.check_once() is False
    assert stopped == []


class _Consumer:
    """Stands in for a query child or a running task: a scripted tree that can have ended."""

    def __init__(self, pid: int | None, *, alive: bool = True) -> None:
        self.pid = pid
        self.alive = alive
        self.asset = "sales.daily"
        self.task_run_id = uuid.uuid4()
        self.details: list[str] = []

    def kill_for_memory(self, detail: str) -> bool:
        """End the scripted process, unless it has ended already.

        Parameters
        ----------
        detail
            The message its user reads.

        Returns
        -------
        bool
            Whether it was alive.
        """
        if not self.alive:
            return False
        self.alive = False
        self.details.append(detail)
        return True


class _Registry:
    """Stands in for the registry of query children or of task processes."""

    def __init__(self, *consumers: _Consumer) -> None:
        self.consumers = list(consumers)

    def children(self) -> list[_Consumer]:
        """List the query children.

        Returns
        -------
        list of _Consumer
            The scripted ones.
        """
        return list(self.consumers)

    def running(self) -> list[_Consumer]:
        """List the task processes.

        Returns
        -------
        list of _Consumer
            The scripted ones.
        """
        return list(self.consumers)


def _stop(
    queries: list[_Consumer],
    tasks: list[_Consumer],
    sizes: dict[int, int],
) -> memory.Ended | None:
    """Run `stop_largest_consumer` over scripted trees whose memory is gone as soon as it is read.

    Parameters
    ----------
    queries
        The query children.
    tasks
        The task processes.
    sizes
        Resident bytes of each process id before it is ended; 0 once it has been.

    Returns
    -------
    Ended or None
        What it returns.
    """
    everyone = [*queries, *tasks]

    def resident(pid: int) -> int:
        """Measure a scripted tree.

        Parameters
        ----------
        pid
            Its root.

        Returns
        -------
        int
            Its size; 0 for one that has been ended.
        """
        gone = any(c.pid == pid and not c.alive for c in everyone)
        return 0 if gone else sizes.get(pid, 0)

    return memory.stop_largest_consumer(
        5000 * MB,
        LIMIT,
        queries=_Registry(*queries),  # ty: ignore[invalid-argument-type]
        tasks=_Registry(*tasks),  # ty: ignore[invalid-argument-type]
        resident=resident,
        wait=lambda seconds: None,
    )


def test_a_query_larger_than_the_task_is_the_one_ended() -> None:
    query, task = _Consumer(10), _Consumer(20)
    ended = _stop([query], [task], {10: 900, 20: 100})
    assert ended == memory.Ended(10, None)
    assert (query.alive, task.alive) == (False, True)
    assert query.details == [memory.query_child_message(LIMIT)]


def test_a_task_larger_than_the_query_is_the_one_ended_with_its_own_message() -> None:
    query, task = _Consumer(10), _Consumer(20)
    ended = _stop([query], [task], {10: 100, 20: 900})
    assert ended == memory.Ended(20, task.task_run_id)
    assert (query.alive, task.alive) == (True, False)
    assert task.details == [memory.task_ended_message(LIMIT)]


def test_of_equal_sizes_the_query_comes_first_and_only_one_is_ended() -> None:
    query, task = _Consumer(10), _Consumer(20)
    ended = _stop([query], [task], {10: 500, 20: 500})
    assert ended == memory.Ended(10, None)
    assert (query.alive, task.alive) == (False, True)


def test_a_consumer_that_ended_since_the_measurement_is_passed_over_for_the_next() -> None:
    query, task = _Consumer(10, alive=False), _Consumer(20)
    ended = _stop([query], [task], {10: 900, 20: 100})
    assert ended == memory.Ended(20, task.task_run_id)
    assert query.details == [] and task.alive is False


def test_with_more_than_two_consumers_the_largest_live_one_is_ended() -> None:
    first, second = _Consumer(1), _Consumer(2)
    third, fourth = _Consumer(3, alive=False), _Consumer(4)
    ended = _stop([first, second], [third, fourth], {1: 10, 2: 40, 3: 90, 4: 30})
    assert ended == memory.Ended(2, None)
    assert [c.alive for c in (first, second, third, fourth)] == [True, False, False, True]


def test_when_nothing_is_running_or_everything_has_ended_nothing_is_ended_or_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("ERROR", logger="lakana_worker.memory"):
        assert _stop([], [], {}) is None
        assert _stop([_Consumer(1, alive=False)], [_Consumer(None)], {1: 5}) is None
    assert caplog.messages == []


def test_the_log_names_the_task_its_run_the_total_the_limit_and_that_the_worker_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    task = _Consumer(20)
    task.asset = "bad\nname"
    with caplog.at_level("ERROR", logger="lakana_worker.memory"):
        _stop([], [task], {20: 900})
    assert caplog.messages == [
        f"Memory limit exceeded: the worker and its tasks use 5000 MB, the limit is "
        f"{LIMIT.origin()}. The largest consumer, a Python task (asset bad\\nname, task run "
        f"{task.task_run_id}, process 20), was ended; the worker keeps running if that frees "
        "enough memory."
    ]


def test_the_log_names_a_query_by_its_process(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("ERROR", logger="lakana_worker.memory"):
        _stop([_Consumer(4711)], [], {4711: 300})
    assert caplog.records[0].event == "memory_task_ended"  # ty: ignore[unresolved-attribute]
    assert caplog.messages == [
        f"Memory limit exceeded: the worker and its tasks use 5000 MB, the limit is "
        f"{LIMIT.origin()}. The largest consumer, a query (process 4711), was ended; the "
        "worker keeps running if that frees enough memory."
    ]


def test_ending_the_largest_consumer_waits_until_its_memory_is_gone() -> None:
    sizes = iter([300, 300, 300, 0])
    waits: list[float] = []
    ended = memory.stop_largest_consumer(
        1,
        LIMIT,
        queries=_Registry(_Consumer(4711)),  # ty: ignore[invalid-argument-type]
        tasks=_Registry(),  # ty: ignore[invalid-argument-type]
        resident=lambda pid: next(sizes),
        wait=waits.append,
    )
    assert ended == memory.Ended(4711, None)
    assert waits == [0.01, 0.01]


def test_ending_the_largest_consumer_gives_up_waiting_after_two_seconds() -> None:
    now = [0.0]

    def wait(seconds: float) -> None:
        now[0] += 0.5

    ended = memory.stop_largest_consumer(
        1,
        LIMIT,
        queries=_Registry(_Consumer(4711)),  # ty: ignore[invalid-argument-type]
        tasks=_Registry(),  # ty: ignore[invalid-argument-type]
        resident=lambda pid: 300,
        wait=wait,
        clock=lambda: now[0],
    )
    assert ended == memory.Ended(4711, None)
    assert now[0] == memory.RELEASE_SECONDS


def test_the_task_message_names_the_cause_the_limit_and_the_next_step() -> None:
    text = memory.task_ended_message(LIMIT)
    assert "largest consumer" in text and "went on" not in text
    assert "(4GB)" in text and "LAKANA_WORKER__MEMORY_LIMIT" in text
    assert "LAKANA_WORKER_MEMORY in the Compose bundle's .env" in text
    assert "narrow its inputs with an upstream SQL asset" in text
    assert "stopped" not in text  # the worker was not stopped


def test_start_watchdog_relieves_on_linux_and_not_on_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux gets the relief, and it asks for the query to go; macOS has none (a peak)."""
    monkeypatch.setattr(MemoryWatchdog, "start", lambda self: None)
    monkeypatch.setattr(memory, "tools", lambda platform: (lambda: 0, lambda: None))
    asked: list[tuple[int, Limit]] = []
    monkeypatch.setattr(
        memory,
        "stop_largest_consumer",
        lambda total, limit: asked.append((total, limit)) or memory.Ended(1),
    )
    linux = start_watchdog("1GB", FAKE_SESSIONS, lambda: 0.5, "w1", "linux")
    darwin = start_watchdog("1GB", FAKE_SESSIONS, lambda: 0.5, "w1", "darwin")
    assert linux is not None and darwin is not None
    assert darwin._relieve is None
    assert linux._relieve is not None
    assert linux._relieve(2 * GB) is True
    assert len(asked) == 1 and asked[0][0] == 2 * GB and asked[0][1].bytes == GB
    monkeypatch.setattr(memory, "stop_largest_consumer", lambda total, limit: None)
    assert linux._relieve(2 * GB) is False  # nothing endable: the worker must stop


def _linux_only() -> pytest.MarkDecorator:
    """Mark a test that measures through ``/proc``.

    Returns
    -------
    pytest.MarkDecorator
        The mark.
    """
    return pytest.mark.skipif(not sys.platform.startswith("linux"), reason="measures /proc")


def _query_children_resident() -> int:
    """Measure the query children only, so that the test process is never over the limit.

    Returns
    -------
    int
        Resident bytes of every registered query child and its descendants.
    """
    return sum(resident_bytes(pid) for pid in REGISTRY.pids())


@_linux_only()
@pytest.mark.usefixtures("child_leak_check")
def test_a_growing_child_is_killed_alone_while_the_worker_and_a_task_live_on() -> None:
    """A scripted child grows by 10 MB steps (300 MB at most, 20 s at most) past 150 MB.

    The watchdog measures the query children only. The child is killed at once, by the
    supervisor's object, and fails as out of memory; the worker is not stopped and a process
    beside it (a Python task) runs on.
    """
    worker_stops: list[int] = []
    bystander = subprocess.Popen(  # stands for a Python task of the same worker
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    dog = MemoryWatchdog(
        150 * MB,
        _query_children_resident,
        worker_stops.append,
        interval=0.02,
        relieve=lambda total: memory.stop_largest_consumer(total, LIMIT) is not None,
    )
    thread = dog.start()
    try:
        with pytest.raises(ChildFailed) as caught, _child("grows", timeout_seconds=30.0) as child:
            _rows(child)
        failure = caught.value
        assert failure.stop is StopReason.OUT_OF_MEMORY
        assert failure.killed is True
        assert failure.detail == memory.query_child_message(LIMIT)
        assert worker_stops == []
        assert bystander.poll() is None
    finally:
        dog.stop()
        thread.join(5)
        bystander.kill()
        bystander.wait()


@_linux_only()
@pytest.mark.usefixtures("child_leak_check")
def test_a_growing_task_is_ended_alone_while_the_worker_and_a_query_live_on(tmp_path: Path) -> None:
    """Against real processes: the largest consumer is a Python task, and only it ends.

    The scripted task allocates in 10 MB steps and stops by itself at 300 MB or after 20 s. The
    watchdog measures the task processes and the query children only (so the test process is
    never the one over the limit) with a limit of 250 MB: a query child beside it is about
    90 MB, so the task is the larger by far when the total crosses the limit. The task is
    ended by the registry's object, its attempt reads the "ended" message, the worker is not
    stopped and the query child runs on.
    """
    harness = Harness(tmp_path)
    task_pids: list[int] = []
    add = harness.registry.add
    harness.registry.add = lambda task: (task_pids.append(task.pid), add(task))[1]  # ty: ignore[invalid-assignment]
    worker_stops: list[int] = []
    dog = MemoryWatchdog(
        250 * MB,
        lambda: sum(
            resident_bytes(pid)
            for pid in [*REGISTRY.pids(), *(t.pid for t in harness.registry.running())]
        ),
        worker_stops.append,
        interval=0.02,
        relieve=lambda total: (
            memory.stop_largest_consumer(total, LIMIT, tasks=harness.registry) is not None
        ),
    )
    with _child("ignores_sigterm", timeout_seconds=60.0) as query:
        thread = dog.start()
        try:
            outcome = harness.run(
                "chunks = []\n"
                "end = time.monotonic() + 20\n"
                "while len(chunks) < 30 and time.monotonic() < end:\n"
                "    chunks.append(bytearray(b'x') * (10 * 1024 * 1024))\n"
                "    time.sleep(0.02)\n"
                "print('reached the end', flush=True)\n"
                "write({'status': 'succeeded', 'snapshot_id': 1, 'rows': 1})\n"
            )
            assert outcome == RunOutcome(
                succeeded=False,
                error_message=memory.task_ended_message(LIMIT),
                killed=True,
                failure_code=FailureCode.MEMORY,
            )
            assert "reached the end" not in harness.output()
            assert worker_stops == []
            assert query.reason is None and not query._exited()
            assert len(task_pids) == 1 and not _alive(task_pids[0])
            assert harness.registry.running() == []
        finally:
            dog.stop()
            thread.join(5)


def _cross_join_of_literals(rows: int = 3000) -> str:
    """Build a query that grows to hundreds of megabytes within a fraction of a second.

    Parameters
    ----------
    rows
        Literal rows on each side of the join.

    Returns
    -------
    str
        The statement.
    """
    values = ",".join(f"({i},'value-{i:06d}')" for i in range(rows))
    return f"WITH a(x, s) AS (VALUES {values}) SELECT list(a.s || b.s) AS l FROM a, a b"


@_linux_only()
@pytest.mark.usefixtures("child_leak_check")
def test_a_query_over_the_limit_is_stopped_while_the_worker_and_a_task_live_on() -> None:
    """Spec 8, against a real child: the watchdog kills the query, and nothing else.

    The watchdog here measures the query children only, with a limit of 200 MB (a child is
    about 90 MB before its statement runs), so the test process is never the one over the
    limit. DuckDB's own limit of 300 MB bounds the child should the watchdog fail: the query
    then fails with DuckDB's message, and the test fails on the text.
    """
    worker_stops: list[int] = []
    bystander = subprocess.Popen(  # stands for a Python task of the same worker
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    dog = MemoryWatchdog(
        200 * MB,
        _query_children_resident,
        worker_stops.append,
        interval=0.02,
        relieve=lambda total: memory.stop_largest_consumer(total, LIMIT) is not None,
    )
    thread = dog.start()
    try:
        with pytest.raises(OutOfMemoryQuery) as caught:
            execute_query(
                _cross_join_of_literals(),
                spill=True,
                limits=QueryLimitsConfig(memory_limit="300MB", threads=2),
            )
        assert str(caught.value) == memory.query_child_message(LIMIT)
        assert worker_stops == []
        assert bystander.poll() is None
        assert execute_query("SELECT 42 AS a").to_pydict() == {"a": [42]}
    finally:
        dog.stop()
        thread.join(5)
        bystander.kill()
        bystander.wait()


def _wait_until_exited(child: object) -> None:
    """Wait, for at most ten seconds, until a started child's process has exited.

    Parameters
    ----------
    child
        A started `QueryChild`.
    """
    deadline = time.monotonic() + 10
    while not child._exited() and time.monotonic() < deadline:  # ty: ignore[unresolved-attribute]
        time.sleep(0.01)


@_linux_only()
@pytest.mark.usefixtures("child_leak_check")
@pytest.mark.slow
def test_a_child_that_has_exited_is_never_signalled_and_the_next_largest_is_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry hands over a child that exited and was not reaped: it gets no signal."""
    signalled: list[int] = []
    real_killpg = os.killpg

    def killpg(pgid: int, signum: int) -> None:
        """Record the group and pass the signal on.

        Parameters
        ----------
        pgid
            The group.
        signum
            The signal.
        """
        signalled.append(pgid)
        real_killpg(pgid, signum)

    monkeypatch.setattr("lakana_worker.query_process.os.killpg", killpg)
    with _child("exits_without_reading") as dead, _child("ignores_sigterm") as running:
        _wait_until_exited(dead)
        sizes = {dead.pid: 900, running.pid: 100}
        stopped = memory.stop_largest_consumer(
            1, LIMIT, resident=lambda pid: sizes.get(pid, 0), wait=lambda seconds: None
        )
        assert stopped is not None
        assert signalled == [running.pid]
        assert dead.reason is None and running.reason is StopReason.OUT_OF_MEMORY


@_linux_only()
@pytest.mark.usefixtures("child_leak_check")
def test_when_every_child_has_exited_nothing_is_signalled_and_the_worker_must_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to kill: `stop_largest_consumer` says so and the watchdog stops the worker."""
    signalled: list[int] = []
    monkeypatch.setattr("lakana_worker.query_process.os.killpg", lambda g, s: signalled.append(g))
    with _child("exits_without_reading") as dead:
        _wait_until_exited(dead)
        assert memory.stop_largest_consumer(1, LIMIT, wait=lambda seconds: None) is None
        stops: list[int] = []
        dog = MemoryWatchdog(
            100,
            lambda: 500,
            stops.append,
            relieve=lambda total: memory.stop_largest_consumer(total, LIMIT) is not None,
        )
        assert dog.check_once() is True
        assert stops == [500] and signalled == []
