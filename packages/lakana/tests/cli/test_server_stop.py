"""Tests for `lakana server stop`."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conftest import FakeApi, invoke, make_state
from typer.testing import Result

from lakana.cli import server
from lakana.cli.local._datadir import DataDir, prepare_data_dir
from lakana.cli.local._pidfile import write_pidfile

Holder = Callable[..., subprocess.Popen[str]]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

_HOLDER = textwrap.dedent(
    """
    import signal, sys, time
    from pathlib import Path
    from lakana.cli._filelock import try_locked
    from lakana.cli.local._pidfile import write_lock_owner
    import os

    lock = Path(sys.argv[1])
    ignore = sys.argv[2] == "ignore"
    with try_locked(lock) as acquired:
        assert acquired
        write_lock_owner(lock, os.getpid())
        if ignore:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        else:
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        print("held", flush=True)
        time.sleep(60)
    """
)


def _stop(tmp_path: Path, data_dir: Path, *extra: str) -> Result:
    """Run `lakana server stop` in-process.

    Parameters
    ----------
    tmp_path
        The test's directory, for the CLI state.
    data_dir
        The data directory to stop.
    *extra
        Further arguments placed before `server`.

    Returns
    -------
    typer.testing.Result
        The command's result.
    """
    state = make_state(tmp_path, FakeApi())
    return invoke(state, *extra, "server", "stop", "--data-dir", str(data_dir))


@pytest.fixture
def holder(tmp_path: Path) -> Iterator[Holder]:
    """Start a process that holds a data directory's lock, as a running `start` does.

    Parameters
    ----------
    tmp_path
        The test's directory; the data directory is `tmp_path / "data"`.

    Yields
    ------
    callable
        `start(ignore_sigterm)` returns the `Popen`; every started process is killed afterwards.
    """
    procs: list[subprocess.Popen[str]] = []

    def start(ignore_sigterm: bool = False) -> subprocess.Popen[str]:
        """Start the holder and wait until it holds the lock.

        Parameters
        ----------
        ignore_sigterm
            Whether the process ignores SIGTERM.

        Returns
        -------
        subprocess.Popen
            The holder.
        """
        root = prepare_data_dir(tmp_path / "data")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _HOLDER,
                str(root.lock_path),
                "ignore" if ignore_sigterm else "x",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        return proc

    yield start
    for proc in procs:
        proc.kill()
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


def test_stop_with_nothing_running_is_not_an_error(tmp_path: Path) -> None:
    """Stopping what is already stopped exits 0 and says how to start one."""
    result = _stop(tmp_path, tmp_path / "data")

    assert result.exit_code == 0, result.output
    assert "No local instance is running" in result.stdout
    assert "lakana server start --local" in result.stdout


def test_stop_cleans_up_a_stale_pidfile(tmp_path: Path) -> None:
    """Pidfiles of a process that is gone are removed, and the message says so."""
    root = prepare_data_dir(tmp_path / "data")
    write_pidfile(root.server_pid, 2_000_000_000, 8765)
    write_pidfile(root.worker_pid, 2_000_000_001)

    result = _stop(tmp_path, tmp_path / "data")

    assert result.exit_code == 0, result.output
    assert "stale" in result.stdout.lower()
    assert not root.server_pid.exists() and not root.worker_pid.exists()


def test_stop_ends_a_running_instance_and_confirms(
    tmp_path: Path, holder: Holder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM goes to the holder of the lock; stop waits for it to leave and confirms."""
    proc = holder()
    root = DataDir(tmp_path / "data")
    write_pidfile(root.server_pid, proc.pid + 1, 8765)

    result = _stop(tmp_path, tmp_path / "data")

    assert result.exit_code == 0, result.output
    assert "Stopped the local instance at http://127.0.0.1:8765" in result.stdout
    assert proc.wait(timeout=5) == 0


def test_stop_that_times_out_names_the_pid_and_never_kills(
    tmp_path: Path, holder: Holder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An instance that does not leave in time exits non-zero, with the next step."""
    proc = holder(ignore_sigterm=True)
    monkeypatch.setattr(server, "_STOP_TIMEOUT_SECONDS", 0.5)

    result = _stop(tmp_path, tmp_path / "data")

    assert result.exit_code == 1
    assert str(proc.pid) in result.output
    assert "kill" in result.output
    assert proc.poll() is None


def test_stop_without_a_recorded_owner_points_to_ctrl_c(tmp_path: Path, holder: Holder) -> None:
    """A lock held by an older lakana, which did not record its PID, is not guessed at."""
    proc = holder()
    from lakana.cli.local._pidfile import clear_lock_owner

    clear_lock_owner(DataDir(tmp_path / "data").lock_path)

    result = _stop(tmp_path, tmp_path / "data")

    assert result.exit_code == 2
    assert "Ctrl-C" in result.output
    assert proc.poll() is None


def test_stop_refuses_a_remote_target_with_the_compose_hint(tmp_path: Path) -> None:
    """A remote URL is not stopped from here; a Compose stack stops with docker compose down."""
    result = _stop(tmp_path, tmp_path / "data", "--url", "https://lakana.example.org")

    assert result.exit_code == 2
    assert "remote" in result.output
    assert "docker compose down" in result.output


def test_stop_is_refused_where_signals_are_not_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows the stop is refused with the Ctrl-C alternative."""
    monkeypatch.setattr(server, "_can_signal", lambda: False)

    result = _stop(tmp_path, tmp_path / "data")

    assert result.exit_code == 2
    assert "Ctrl-C" in result.output


def test_start_records_its_pid_in_the_lock_file_and_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the supervisor runs the lock file names it; afterwards it names no one."""
    from test_server_cmd import FakeSupervisor, _install_fake_supervisor, _stub_environment

    from lakana.cli.local._pidfile import read_lock_owner

    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    seen: list[int | None] = []
    root = DataDir(tmp_path / "data")

    class Spy(FakeSupervisor):
        """Reads the lock owner while `watch` runs."""

        def watch(self) -> int:
            """Record the owner in the lock file.

            Returns
            -------
            int
                `0`, a clean exit.
            """
            seen.append(read_lock_owner(root.lock_path))
            return 0

    _install_fake_supervisor(monkeypatch, Spy())
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/environments",
        status=201,
        json={
            "name": "dev",
            "storage_root": "file:///d",
            "storage_secret_ref": None,
            "is_protected": False,
            "properties": {},
        },
    )
    result = invoke(
        make_state(tmp_path, api),
        "server",
        "start",
        "--local",
        "--data-dir",
        str(tmp_path / "data"),
        "--port",
        "8765",
    )

    assert result.exit_code == 0, result.stderr
    assert seen == [os.getpid()]
    assert read_lock_owner(root.lock_path) is None


def test_a_stale_owner_in_an_unheld_lock_file_is_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed supervisor leaves its pid behind; with no lock holder nothing is signalled."""
    from lakana.cli.local._pidfile import write_lock_owner

    root = prepare_data_dir(tmp_path / "data")
    root.lock_path.touch()
    write_lock_owner(root.lock_path, os.getpid())

    def fail(pid: int, sig: int) -> None:
        """Fail the test if anything is signalled.

        Parameters
        ----------
        pid
            The target.
        sig
            The signal.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError(f"signalled {pid}")

    monkeypatch.setattr(server.os, "kill", fail)

    result = _stop(tmp_path, tmp_path / "data")

    assert result.exit_code == 0, result.output
    assert "No local instance is running" in result.stdout


def test_a_recorded_pid_that_is_not_lakana_is_not_signalled(
    tmp_path: Path, holder: Holder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock held, but the recorded pid's command line is another program: refuse, name the pid."""
    proc = holder()
    monkeypatch.setattr(server, "_process_cmdline", lambda pid: "/usr/bin/vim notes.txt")

    result = _stop(tmp_path, tmp_path / "data")

    assert result.exit_code == 2
    assert str(proc.pid) in result.output
    assert "Ctrl-C" in result.output
    assert proc.poll() is None


def test_a_failed_owner_write_is_reported_and_leaves_no_stale_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start` says it could not record itself; a stale pid from before is already gone."""
    from test_server_cmd import FakeSupervisor, _install_fake_supervisor, _stub_environment

    from lakana.cli.local._pidfile import read_lock_owner, write_lock_owner

    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    root = prepare_data_dir(tmp_path / "data")
    root.lock_path.touch()
    write_lock_owner(root.lock_path, 99999)
    seen: list[int | None] = []

    def boom(path: Path, pid: int) -> None:
        """Fail like a full disk.

        Parameters
        ----------
        path
            The lock file.
        pid
            The pid to record.

        Raises
        ------
        OSError
            Always.
        """
        raise OSError("disk full")

    class Spy(FakeSupervisor):
        """Reads the lock owner while `watch` runs."""

        def watch(self) -> int:
            """Record the owner in the lock file.

            Returns
            -------
            int
                `0`, a clean exit.
            """
            seen.append(read_lock_owner(root.lock_path))
            return 0

    monkeypatch.setattr(server, "write_lock_owner", boom)
    _install_fake_supervisor(monkeypatch, Spy())
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/environments",
        status=201,
        json={
            "name": "dev",
            "storage_root": "file:///d",
            "storage_secret_ref": None,
            "is_protected": False,
            "properties": {},
        },
    )
    result = invoke(
        make_state(tmp_path, api),
        *("server", "start", "--local", "--data-dir", str(tmp_path / "data"), "--port", "8765"),
    )

    assert result.exit_code == 0, result.stderr
    assert "disk full" in result.stderr and "Ctrl-C" in result.stderr
    assert seen == [None]


def test_a_recorded_owner_that_is_gone_is_not_signalled(
    tmp_path: Path, holder: Holder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock held, but the recorded process is not alive: refuse, name the pid."""
    proc = holder()
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)

    result = _stop(tmp_path, tmp_path / "data")

    assert result.exit_code == 2
    output = " ".join(result.output.split())  # a long data directory path wraps the message
    assert str(proc.pid) in output and "is gone" in output
    assert "Ctrl-C" in output
    assert proc.poll() is None


def test_a_refused_signal_is_reported_with_the_next_step(
    tmp_path: Path, holder: Holder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holder owned by another user cannot be signalled: say so and point to Ctrl-C."""
    proc = holder()

    def deny(pid: int, sig: int) -> None:
        """Refuse like the kernel does for another user's process.

        Parameters
        ----------
        pid
            The target.
        sig
            The signal.

        Raises
        ------
        PermissionError
            Always.
        """
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(server.os, "kill", deny)

    result = _stop(tmp_path, tmp_path / "data")

    assert result.exit_code == 2
    output = " ".join(result.output.split())  # a long data directory path wraps the message
    assert f"Can't signal process {proc.pid}" in output
    assert "Operation not permitted" in output
    assert "Ctrl-C" in output


@pytest.mark.parametrize("padding", range(0, 120, 8))
def test_a_live_instance_without_a_lock_owner_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, padding: int
) -> None:
    """No lock, but the pidfile's instance answers its health check: an older lakana.

    The message names the data directory, and the terminal wraps it at a fixed width, so where
    the line breaks depends on how long the path is. The test runner's temporary directory
    changes in length between runs (`pytest-9` and `pytest-10`, worker names), which broke the
    assertion on some runs only; `padding` walks the line break through the whole message.
    """
    data_dir = tmp_path / ("d" * padding) / "data"
    root = prepare_data_dir(data_dir)
    write_pidfile(root.server_pid, os.getpid(), 8765)
    monkeypatch.setattr(server, "_probe_health", lambda url: True)

    result = _stop(tmp_path, data_dir)

    output = " ".join(result.output.split())
    assert result.exit_code == 2
    assert "http://127.0.0.1:8765" in output
    assert "did not record which process to stop" in output
    assert "Ctrl-C" in output


def test_record_lock_owner_does_nothing_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where there are no POSIX signals the lock file is left without an owner."""
    root = prepare_data_dir(tmp_path / "data")
    written: list[int] = []
    monkeypatch.setattr(server, "write_lock_owner", lambda path, pid: written.append(pid))
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "name", "nt")
        server._record_lock_owner(root)

    assert written == []


def test_process_cmdline_reads_a_live_process_and_none_for_a_missing_one() -> None:
    """A live child's command line comes back with its arguments; a missing pid has none.

    A child, not this process: pytest-xdist rewrites its workers' own command line.
    """
    if not Path("/proc/self/cmdline").exists():
        pytest.skip("no /proc on this platform")

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "marker-arg"])
    try:
        # The kernel fills /proc/<pid>/cmdline once the exec has finished: wait for it, bounded.
        deadline = time.monotonic() + 10
        while not (cmdline := server._process_cmdline(child.pid)) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert cmdline is not None
        assert cmdline.endswith("-c import time; time.sleep(30) marker-arg ")
    finally:
        child.kill()
        child.wait(timeout=10)
    assert server._process_cmdline(2**31 - 2) is None
