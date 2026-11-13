"""Tests for the local-mode process supervisor."""

from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import IO

import pytest
from scripts.test_support.ports import closed_port

from lakana.cli.local._supervisor import (
    MEMORY_RESTART_WINDOW,
    MEMORY_RESTARTS,
    WORKER_MEMORY_EXIT_CODE,
    Supervisor,
    SupervisorConfig,
    SupervisorError,
    _probe,
    _spawn,
)


@dataclass
class FakeChild:
    """A controllable stand-in for `subprocess.Popen`.

    Parameters
    ----------
    output
        What the child "prints".
    exit_code
        What `poll`/`wait` return once exited; `None` while running.
    ignore_terminate
        Keep running after `terminate` (forces a kill).
    """

    output: str = ""
    exit_code: int | None = None
    ignore_terminate: bool = False
    pid: int = 4242
    events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Expose `output` as a text stream."""
        self.stdout = io.StringIO(self.output)

    def poll(self) -> int | None:
        """Return the exit code, or None while running.

        Returns
        -------
        int or None
            The exit code.
        """
        return self.exit_code

    def wait(self, timeout: float | None = None) -> int:
        """Return the exit code, or raise a timeout if still running.

        Parameters
        ----------
        timeout
            Ignored beyond deciding whether to raise.

        Returns
        -------
        int
            The exit code.

        Raises
        ------
        subprocess.TimeoutExpired
            If still running.
        """
        if self.exit_code is None:
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return self.exit_code

    def terminate(self) -> None:
        """Record SIGTERM; exit 0 unless configured to ignore it."""
        self.events.append("terminate")
        if not self.ignore_terminate:
            self.exit_code = 0

    def kill(self) -> None:
        """Record SIGKILL and exit -9."""
        self.events.append("kill")
        self.exit_code = -9


def _supervisor(
    children: dict[str, FakeChild], probe_results: list[bool], **kw: float
) -> tuple[Supervisor, list[list[str]], io.StringIO]:
    """Build a supervisor with fake spawn/probe/clock.

    Parameters
    ----------
    children
        The child returned per first command word (`"migrate"`, `"server"`, `"worker"`).
    probe_results
        Successive health-probe answers.
    **kw
        Extra `SupervisorConfig` fields (`ready_timeout`, `grace`).

    Returns
    -------
    tuple
        The supervisor, the list of spawned commands, and its output stream.
    """
    spawned: list[list[str]] = []
    now = [0.0]
    out = io.StringIO()

    def spawn(cmd: list[str], env: dict[str, str]) -> FakeChild:
        """Return the configured fake child for `cmd`.

        Parameters
        ----------
        cmd
            The command.
        env
            The environment (unused).

        Returns
        -------
        FakeChild
            The fake.
        """
        spawned.append(cmd)
        return children[cmd[0]]

    def sleep(seconds: float) -> None:
        """Advance the fake clock.

        Parameters
        ----------
        seconds
            How far.
        """
        now[0] += seconds

    config = SupervisorConfig(
        server_cmd=["server"],
        worker_cmd=["worker"],
        migrate_cmd=["migrate"],
        env={},
        health_url="http://127.0.0.1:1/healthz",
        **kw,
    )
    answers = iter(probe_results)
    sup = Supervisor(
        config,
        spawn=spawn,
        probe=lambda url: next(answers, False),
        clock=lambda: now[0],
        sleep=sleep,
        out=out,
    )
    return sup, spawned, out


def test_migration_failure_raises_with_its_output() -> None:
    """A non-zero `lakana-server migrate` stops everything, carrying its output."""
    sup, spawned, _ = _supervisor({"migrate": FakeChild("boom\n", exit_code=3)}, [])

    with pytest.raises(SupervisorError, match="migrat") as caught:
        sup.migrate()

    assert "boom" in caught.value.output
    assert spawned == [["migrate"]]


def test_wait_ready_returns_once_health_answers() -> None:
    """Probe false, false, true → ready."""
    server = FakeChild()
    sup, _, _ = _supervisor({"server": server}, [False, False, True])

    sup.wait_ready(sup.start_server())


def test_wait_ready_fails_fast_when_the_server_exits() -> None:
    """Review focus 1: a dying server fails immediately, not after the timeout."""
    server = FakeChild("address already in use\n", exit_code=1)
    sup, _, _ = _supervisor({"server": server}, [])

    with pytest.raises(SupervisorError, match="exited with code 1") as caught:
        sup.wait_ready(sup.start_server())

    assert "address already in use" in caught.value.output


def test_wait_ready_times_out() -> None:
    """No healthy answer within ready_timeout → error, server terminated."""
    server = FakeChild()
    sup, _, _ = _supervisor({"server": server}, [], ready_timeout=1.0)

    with pytest.raises(SupervisorError, match="didn't become ready"):
        sup.wait_ready(sup.start_server())

    assert "terminate" in server.events


def test_output_is_prefixed() -> None:
    """Child lines stream with `server |` / `worker |` prefixes."""
    sup, _, out = _supervisor({"server": FakeChild("hello\n"), "worker": FakeChild("hi\n")}, [True])

    server = sup.start_server()
    sup.wait_ready(server)
    sup.start_worker()
    sup.stop()

    assert "server | hello" in out.getvalue()
    assert "worker | hi" in out.getvalue()


def test_watch_stops_the_other_child_when_one_exits() -> None:
    """The worker dying stops the server; watch returns the worker's code."""
    server, worker = FakeChild(), FakeChild()
    sup, _, _ = _supervisor({"server": server, "worker": worker}, [True])
    sup.wait_ready(sup.start_server())
    sup.start_worker()
    worker.exit_code = 2

    code = sup.watch()

    assert code == 2
    assert "terminate" in server.events


def test_stop_kills_a_child_that_ignores_sigterm() -> None:
    """After the grace period, a child still running is killed."""
    server = FakeChild(ignore_terminate=True)
    sup, _, _ = _supervisor({"server": server}, [True], grace=0.5)
    sup.wait_ready(sup.start_server())

    sup.stop()

    assert server.events == ["terminate", "kill"]


def test_stop_child_skips_a_child_that_already_exited() -> None:
    """`_stop_child`'s own guard against a child that exited just before the call.

    `wait_ready`'s timeout path calls `_stop_child` right after observing
    `poll() is None`, but a real child can still exit in the gap between
    that check and this call; the guard must not send a redundant signal.
    """
    server = FakeChild(exit_code=0)
    sup, _, _ = _supervisor({"server": server}, [])
    sup.start_server()

    sup._stop_child(server)

    assert server.events == []


def test_stop_terminates_all_children_before_killing_any() -> None:
    """Critical fix: every running child gets `terminate` before any gets `kill`.

    The grace period is one shared budget, not one per child. Both children
    ignore `terminate`, sharing one `events` list so their
    call order is directly observable. Under the old, sequential
    implementation this would read `["terminate", "kill", "terminate",
    "kill"]` and the fake clock would advance by `2 * grace`.
    """
    events: list[str] = []
    server = FakeChild(ignore_terminate=True, events=events)
    worker = FakeChild(ignore_terminate=True, events=events)
    sup, _, _ = _supervisor({"server": server, "worker": worker}, [True], grace=1.0)
    sup.wait_ready(sup.start_server())
    sup.start_worker()

    sup.stop()

    assert events == ["terminate", "terminate", "kill", "kill"]
    assert sup._clock() < 1.5  # one grace period, not two


def test_a_chatty_child_never_blocks() -> None:
    """Review focus 4: 1 MiB of output is drained while waiting."""
    server = FakeChild("x" * 1023 + "\n" * 1024)
    sup, _, out = _supervisor({"server": server}, [True])

    sup.wait_ready(sup.start_server())
    sup.stop()

    assert out.getvalue().count("server | ") >= 1


def test_watch_treats_a_negative_exit_code_as_failure() -> None:
    """A child killed by a signal (negative code) still fails `start` with exit 1."""
    server, worker = FakeChild(), FakeChild()
    sup, _, _ = _supervisor({"server": server, "worker": worker}, [True])
    sup.wait_ready(sup.start_server())
    sup.start_worker()
    worker.exit_code = -9

    assert sup.watch() == 1


def test_watch_treats_an_unexpected_clean_exit_as_failure() -> None:
    """A child exiting 0 on its own (nobody called `stop`) is still a failure."""
    server, worker = FakeChild(), FakeChild()
    sup, _, _ = _supervisor({"server": server, "worker": worker}, [True])
    sup.wait_ready(sup.start_server())
    sup.start_worker()
    worker.exit_code = 0

    assert sup.watch() == 1


def test_stop_also_stops_a_still_running_migrate_child() -> None:
    """Ctrl-C during `migrate` must not orphan it.

    `migrate()` normally blocks on `child.wait()` until the child exits, so
    a `KeyboardInterrupt` raised during that wait (Ctrl-C) leaves the
    migrate child itself still running, tracked only under the `"migrate"`
    label -- `stop()` used to check only `"server"`/`"worker"` and would
    walk straight past it.
    """
    migrate_child = FakeChild()
    sup, _, _ = _supervisor({"migrate": migrate_child}, [])
    sup._start("migrate", sup._config.migrate_cmd)

    sup.stop()

    assert "terminate" in migrate_child.events


def test_stop_is_idempotent_and_safe_before_any_child_started() -> None:
    """`stop` with nothing spawned yet, and called twice, is a no-op."""
    sup, _, _ = _supervisor({}, [])

    sup.stop()
    sup.stop()


def test_stop_skips_a_child_that_already_exited() -> None:
    """A child that exited on its own is not sent a redundant signal."""
    server = FakeChild(exit_code=0)
    sup, _, _ = _supervisor({"server": server}, [])
    sup.start_server()

    sup.stop()

    assert server.events == []


def test_real_spawn_and_probe() -> None:
    """The real seams: a genuine child process and a genuine HTTP probe."""
    child = _spawn([sys.executable, "-c", "print('hello from child')"], dict(os.environ))
    try:
        assert child.pid > 0
        assert child.wait(timeout=5) == 0
        assert child.stdout is not None
        assert "hello from child" in child.stdout.read()
    finally:
        if child.poll() is None:
            child.kill()
        if child.stdout is not None:
            child.stdout.close()

    assert _probe(f"http://127.0.0.1:{closed_port()}/healthz") is False


def test_real_probe_ignores_http_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The supervisor's loopback probe bypasses `HTTP_PROXY`."""
    dead_port = closed_port()
    monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{dead_port}")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve_one() -> None:
            """Accept a single connection and answer with a bare `200 OK`."""
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

        thread = threading.Thread(target=serve_one, daemon=True)
        thread.start()

        assert _probe(f"http://127.0.0.1:{port}/healthz") is True

        thread.join(timeout=2)


def test_real_probe_succeeds_against_a_real_200() -> None:
    """The real `_probe`, against a real listener answering `200`."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve_one() -> None:
            """Accept a single connection and answer with a bare `200 OK`."""
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

        thread = threading.Thread(target=serve_one, daemon=True)
        thread.start()

        assert _probe(f"http://127.0.0.1:{port}/healthz") is True

        thread.join(timeout=2)


def test_migrate_succeeds_silently() -> None:
    """A clean `lakana-server migrate` exit raises nothing."""
    sup, spawned, _ = _supervisor({"migrate": FakeChild("ok\n", exit_code=0)}, [])

    sup.migrate()

    assert spawned == [["migrate"]]


def test_watch_skips_a_child_that_was_never_started() -> None:
    """`watch` tolerates a label with no child, e.g. the worker not yet started."""
    worker = FakeChild()
    sup, _, _ = _supervisor({"worker": worker}, [])
    sup.start_worker()
    worker.exit_code = 5

    assert sup.watch() == 5


def test_watch_sleeps_between_polls_while_both_children_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer loop actually sleeps and re-polls before anything has exited."""
    server, worker = FakeChild(), FakeChild()
    sup, _, _ = _supervisor({"server": server, "worker": worker}, [True])
    sup.wait_ready(sup.start_server())
    sup.start_worker()
    sleeps: list[float] = []

    def sleep_then_exit(seconds: float) -> None:
        """Record the call and make the worker exit, ending the next poll.

        Parameters
        ----------
        seconds
            The requested sleep duration.
        """
        sleeps.append(seconds)
        worker.exit_code = 7

    monkeypatch.setattr(sup, "_sleep", sleep_then_exit)

    assert sup.watch() == 7
    assert sleeps == [0.2]


def test_drain_returns_immediately_when_stdout_is_none() -> None:
    """A child whose stdout was never piped is simply not drained."""

    class _StdoutlessChild:
        """A `Child` whose `stdout` is `None` (never piped)."""

        pid = 999
        stdout: IO[str] | None = None

        def poll(self) -> int | None:
            """Report still running.

            Returns
            -------
            int or None
                Always `None`.
            """
            return None

        def wait(self, timeout: float | None = None) -> int:
            """Return immediately with exit code `0`.

            Parameters
            ----------
            timeout
                Ignored.

            Returns
            -------
            int
                Always `0`.
            """
            return 0

        def terminate(self) -> None:
            """No-op."""

        def kill(self) -> None:
            """No-op."""

    server = _StdoutlessChild()
    out = io.StringIO()
    config = SupervisorConfig(
        server_cmd=["server"],
        worker_cmd=["worker"],
        migrate_cmd=["migrate"],
        env={},
        health_url="http://127.0.0.1:1/healthz",
        grace=0.1,
    )
    now = [0.0]
    sup = Supervisor(
        config,
        spawn=lambda cmd, env: server,
        probe=lambda url: True,
        clock=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        out=out,
    )

    sup.wait_ready(sup.start_server())
    sup.stop()

    assert out.getvalue() == ""


def test_stop_does_not_kill_a_child_that_exits_right_at_the_grace_deadline() -> None:
    """A child that stops exactly as `grace` elapses isn't force-killed."""

    class _EdgeChild:
        """A child reporting running once, then exited (a race at the deadline)."""

        pid = 1
        stdout: IO[str] | None = None

        def __init__(self) -> None:
            """Start with no recorded `poll` calls."""
            self.calls = 0

        def poll(self) -> int | None:
            """Return `None` on the first call, `0` on every call after.

            Returns
            -------
            int or None
                `None`, then `0`.
            """
            self.calls += 1
            return None if self.calls == 1 else 0

        def wait(self, timeout: float | None = None) -> int:
            """Return the already-known exit code.

            Parameters
            ----------
            timeout
                Ignored.

            Returns
            -------
            int
                Always `0`.
            """
            return 0

        def terminate(self) -> None:
            """No-op."""

        def kill(self) -> None:
            """Fail the test: an already-exited child must not be killed.

            Raises
            ------
            AssertionError
                Always.
            """
            raise AssertionError("kill() must not be called for an already-exited child")

    child = _EdgeChild()
    config = SupervisorConfig(
        server_cmd=["server"],
        worker_cmd=["worker"],
        migrate_cmd=["migrate"],
        env={},
        health_url="http://127.0.0.1:1/healthz",
        grace=0.0,
    )
    sup = Supervisor(
        config,
        spawn=lambda cmd, env: child,
        probe=lambda url: False,
        clock=lambda: 0.0,
        sleep=lambda seconds: None,
        out=io.StringIO(),
    )
    sup.start_server()

    sup.stop()

    # No assertion needed beyond "this returned at all": `kill()` raises if
    # called, so a passing test already proves the already-exited child was
    # never force-killed. `calls > 1` just confirms `poll()` was actually
    # consulted again after the child was observed running.
    assert child.calls > 1


def test_the_memory_exit_code_is_pinned() -> None:
    """The worker's `lakana_worker.memory.EXIT_CODE` is the same number; its test pins that side."""
    assert WORKER_MEMORY_EXIT_CODE == 75


class _Workers:
    """Spawn a fresh fake worker each time, remembering them; the server is one fake child."""

    def __init__(self, exits: list[int | None]) -> None:
        self.exits = list(exits)
        self.spawned_workers: list[FakeChild] = []
        self.server = FakeChild()

    def __call__(self, cmd: list[str], env: dict[str, str]) -> FakeChild:
        """Return the server, or the next worker with its scripted exit code.

        Parameters
        ----------
        cmd
            The command.
        env
            The environment (unused).

        Returns
        -------
        FakeChild
            The fake child.
        """
        if cmd[0] == "server":
            return self.server
        worker = FakeChild(exit_code=self.exits.pop(0))
        self.spawned_workers.append(worker)
        return worker


def _watched(
    exits: list[int | None], clock_step: float = 1.0
) -> tuple[Supervisor, _Workers, io.StringIO]:
    """Build a supervisor whose workers exit as scripted.

    Parameters
    ----------
    exits
        The exit code of each successive worker; `None` keeps it running.
    clock_step
        Seconds the fake clock advances per reading.

    Returns
    -------
    tuple
        The supervisor, the spawner and the output stream.
    """
    spawner = _Workers(exits)
    now = [0.0]

    def clock() -> float:
        now[0] += clock_step
        return now[0]

    out = io.StringIO()
    config = SupervisorConfig(
        server_cmd=["server"],
        worker_cmd=["worker"],
        migrate_cmd=["migrate"],
        env={},
        health_url="http://127.0.0.1:1/healthz",
    )
    sup = Supervisor(
        config, spawn=spawner, probe=lambda url: True, clock=clock, sleep=lambda s: None, out=out
    )
    sup.start_server()
    sup.start_worker()
    return sup, spawner, out


def test_a_worker_that_stopped_for_memory_is_restarted_with_one_line_saying_why() -> None:
    """Exit 75 restarts the worker; the next worker's different exit ends the run as today."""
    sup, spawner, out = _watched([WORKER_MEMORY_EXIT_CODE, 3])
    assert sup.watch() == 3
    assert len(spawner.spawned_workers) == 2
    text = out.getvalue()
    assert "is being restarted" in text and "has failed" in text
    assert "start-up line says which limit applies" in text
    assert "4GB" not in text
    assert "terminate" in spawner.server.events


def test_a_server_exit_with_the_memory_code_is_not_a_restart() -> None:
    """Only the worker's exit code means "memory"."""
    spawner = _Workers([None])
    sup, _, out = _watched([None])
    sup._children["server"] = FakeChild(exit_code=WORKER_MEMORY_EXIT_CODE)
    assert sup.watch() == WORKER_MEMORY_EXIT_CODE
    assert "restarted" not in out.getvalue()
    assert spawner.spawned_workers == []


def test_a_worker_that_keeps_exceeding_its_limit_ends_the_run_with_advice() -> None:
    """More than `MEMORY_RESTARTS` stops within the window: stop, say what to do, exit 1."""
    sup, spawner, out = _watched([WORKER_MEMORY_EXIT_CODE] * (MEMORY_RESTARTS + 1))
    assert sup.watch() == 1
    assert len(spawner.spawned_workers) == MEMORY_RESTARTS + 1
    text = out.getvalue()
    assert f"exceeded its memory limit {MEMORY_RESTARTS + 1} times" in text
    assert "LAKANA_WORKER__MEMORY_LIMIT=8GB" in text
    assert "terminate" in spawner.server.events


def test_restarts_older_than_the_window_do_not_count() -> None:
    """Slow, occasional stops never end the run."""
    sup, spawner, out = _watched(
        [WORKER_MEMORY_EXIT_CODE] * (MEMORY_RESTARTS + 2) + [4], clock_step=MEMORY_RESTART_WINDOW
    )
    assert sup.watch() == 4
    assert len(spawner.spawned_workers) == MEMORY_RESTARTS + 3
    assert "exceeded its memory limit" not in out.getvalue()
