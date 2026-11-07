"""Local mode end to end with real processes."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import socket
import subprocess
import sysconfig
import threading
import time
from pathlib import Path
from typing import IO

import pytest

from lakana.cli._filelock import try_locked

pytestmark = pytest.mark.integration

_LAKANA = str(Path(sysconfig.get_path("scripts")) / "lakana")


def _free_port() -> int:
    """Pick a free localhost port.

    Returns
    -------
    int
        The port.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _cli(
    env: dict[str, str], *args: str, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the installed `lakana` CLI.

    Parameters
    ----------
    env
        The environment (isolated XDG dirs).
    *args
        The command line.
    input
        Stdin text.

    Returns
    -------
    subprocess.CompletedProcess
        The finished process.
    """
    return subprocess.run(
        [_LAKANA, *args],
        env=env,
        input=input,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _read_lines(stream: IO[str], sink: queue.Queue[str | None]) -> None:
    """Forward each line from `stream` to `sink`, then `None` at EOF.

    Runs on a daemon thread for the supervised process's entire lifetime, so
    its stdout pipe can never fill up and block it while the test drives the
    CLI in separate processes. Crucially, this also means nothing in the
    main test thread ever calls the blocking `readline()` itself: `queue.get
    (timeout=...)` on the consuming side is what is actually bounded. A loop that instead
    re-checked a deadline only
    *between* `readline()` calls was not a real timeout, since a single
    `readline()` call that never returns -- exactly what an unflushed write
    on the other end produces -- blocks regardless of how soon the deadline
    already passed.

    Parameters
    ----------
    stream
        The child's merged stdout/stderr stream.
    sink
        The queue each line is put onto; `None` is put once at EOF so a
        consumer can tell the stream ended.
    """
    try:
        for line in stream:
            sink.put(line)
    finally:
        sink.put(None)


def _assert_process_gone(pid: int, timeout: float = 5.0) -> None:
    """Assert a process id is no longer alive, polling briefly for it.

    A short, bounded poll rather than a single check: `Supervisor.stop()`
    already waits for each child by the time the supervisor process itself
    exits, but this stays a poll (not a hard requirement of instant
    reaping) so the test can't hang if that assumption is ever wrong.

    Parameters
    ----------
    pid
        The process id to check.
    timeout
        Seconds to poll before giving up.

    Raises
    ------
    AssertionError
        If the process still answers `os.kill(pid, 0)` after `timeout`.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"process {pid} is still running {timeout} s after shutdown")
        time.sleep(0.1)


def _drain_queue(source: queue.Queue[str | None], lines: list[str]) -> None:
    """Move every remaining item from `source` into `lines`, until `None`.

    Runs on a daemon thread once the ready line has been seen, taking over
    from the bounded wait in the test body so the queue itself can never
    grow without bound for the rest of the run.

    Parameters
    ----------
    source
        The queue `_read_lines` is feeding.
    lines
        The list each item is appended to.
    """
    while (item := source.get()) is not None:
        lines.append(item)


def test_local_mode_start_query_and_stop(tmp_path: Path) -> None:
    """`start --local` -> table create -> append CSV -> sql -> Ctrl-C stops everything."""
    port = _free_port()
    data_dir = tmp_path / "data"
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "share"),
        "NO_COLOR": "1",
    }
    for key in ("LAKANA_URL", "LAKANA_TOKEN", "LAKANA_PROFILE", "LAKANA_OUTPUT"):
        env.pop(key, None)

    proc = subprocess.Popen(
        [_LAKANA, "server", "start", "--local", "--data-dir", str(data_dir), "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    reader: threading.Thread | None = None
    try:
        assert proc.stdout is not None
        output: queue.Queue[str | None] = queue.Queue()
        lines: list[str] = []
        reader = threading.Thread(target=_read_lines, args=(proc.stdout, output), daemon=True)
        reader.start()

        deadline = time.monotonic() + 90
        ready = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = output.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                break
            lines.append(line)
            if "Lakana is running at" in line:
                ready = True
                break
        assert ready, "".join(lines)

        threading.Thread(target=_drain_queue, args=(output, lines), daemon=True).start()

        # A second real `start` on the same data directory is refused
        # while the first runs, even with a different port.
        second = _cli(
            env,
            "server",
            "start",
            "--local",
            "--data-dir",
            str(data_dir),
            "--port",
            str(_free_port()),
        )
        assert second.returncode == 2, second.stderr
        assert f"Lakana is already running from {data_dir}" in "".join(second.stderr.split("\n"))

        assert (
            _cli(
                env,
                "table",
                "create",
                "bronze.sales",
                "--column",
                "id:long",
                "--column",
                "region:string?",
            ).returncode
            == 0
        )
        csv = tmp_path / "sales.csv"
        csv.write_text("id,region\n1,eu\n2,\n")
        appended = _cli(env, "table", "append", "bronze.sales", str(csv))
        assert appended.returncode == 0, appended.stderr
        result = _cli(
            env, "--output", "json", "sql", "SELECT id, region FROM bronze.sales ORDER BY id"
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [{"id": 1, "region": "eu"}, {"id": 2, "region": None}]

        status = _cli(env, "--output", "json", "server", "status", "--data-dir", str(data_dir))
        status_payload = json.loads(status.stdout)
        assert status_payload["running"] is True
        server_pid = status_payload["server_pid"]
        worker_pid = status_payload["worker_pid"]
        assert server_pid is not None
        assert worker_pid is not None

        # Only the lock can refuse now: with both pidfiles gone, a second
        # `start` is still refused, and with no recorded port the message
        # carries no URL.
        (data_dir / "server.pid").unlink()
        (data_dir / "worker.pid").unlink()
        third = _cli(env, "server", "start", "--local", "--data-dir", str(data_dir))
        assert third.returncode == 2, third.stderr
        flat = "".join(third.stderr.split("\n"))
        assert f"Lakana is already running from {data_dir}" in flat
        assert "at http://" not in flat
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # A hung shutdown must not hang the whole test suite: force-kill the
            # supervisor and any children it failed to reap, using the pidfiles
            # (the children run detached, in their own session, so killing the
            # supervisor alone would not reach them).
            proc.kill()
            code = proc.wait(timeout=10)
            for pid_file in (data_dir / "server.pid", data_dir / "worker.pid"):
                try:
                    pid = int(pid_file.read_text().split()[0])
                except (FileNotFoundError, ValueError, IndexError):
                    continue
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)
        # The child has ended, so the reader has seen the end of the stream: close the pipe
        # rather than leave it to the garbage collector (#468).
        if reader is not None:
            reader.join(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()

    assert code == 0
    assert not (data_dir / "server.pid").exists()
    assert not (data_dir / "worker.pid").exists()
    assert _cli(env, "server", "status", "--data-dir", str(data_dir)).returncode == 1
    # The lock goes with the supervisor: a fresh `start` is not refused.
    with try_locked(data_dir / "lock") as free:
        assert free
    # The spec's exit criterion: not just the pidfiles gone (removed in
    # `finally` regardless of whether `stop()` actually reaped anything),
    # but the child processes themselves.
    _assert_process_gone(server_pid)
    _assert_process_gone(worker_pid)
