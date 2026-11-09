"""Tests for `lakana server start --local` and `lakana server status`."""

from __future__ import annotations

import json
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeApi, invoke, make_state
from typer.testing import Result

from lakana.cli import _profiles, server
from lakana.cli._filelock import try_locked
from lakana.cli.local._datadir import DataDir, prepare_data_dir
from lakana.cli.local._pidfile import write_pidfile
from lakana.cli.local._supervisor import Supervisor, SupervisorConfig, SupervisorError

_DEV = {
    "name": "dev",
    "storage_root": "file:///data/lake/dev",
    "storage_secret_ref": None,
    "is_protected": False,
    "properties": {},
}


@dataclass
class FakeSupervisor:
    """A test double for `Supervisor`, recording calls and configurable failures.

    Parameters
    ----------
    migrate_error
        Raised from `migrate`, if any.
    wait_ready_error
        Raised from `wait_ready`, if any.
    watch_code
        Returned by `watch`.
    server_pid
        The `pid` of the object `start_server` returns.
    worker_pid
        The `pid` of the object `start_worker` returns.
    """

    migrate_error: BaseException | None = None
    wait_ready_error: BaseException | None = None
    watch_code: int = 0
    server_pid: int = 4242
    worker_pid: int = 4343
    calls: list[str] = field(default_factory=list)
    config: SupervisorConfig | None = None

    def migrate(self) -> None:
        """Record the call and raise `migrate_error`, if set."""
        self.calls.append("migrate")
        if self.migrate_error is not None:
            raise self.migrate_error

    def start_server(self) -> SimpleNamespace:
        """Record the call and return a fake child exposing `pid`.

        Returns
        -------
        SimpleNamespace
            An object with a `pid` attribute.
        """
        self.calls.append("start_server")
        return SimpleNamespace(pid=self.server_pid)

    def wait_ready(self, child: object) -> None:
        """Record the call and raise `wait_ready_error`, if set.

        Parameters
        ----------
        child
            The server child (unused; the fake never inspects it).
        """
        self.calls.append("wait_ready")
        if self.wait_ready_error is not None:
            raise self.wait_ready_error

    def start_worker(self) -> SimpleNamespace:
        """Record the call and return a fake child exposing `pid`.

        Returns
        -------
        SimpleNamespace
            An object with a `pid` attribute.
        """
        self.calls.append("start_worker")
        return SimpleNamespace(pid=self.worker_pid)

    def watch(self) -> int:
        """Record the call and return `watch_code`.

        Returns
        -------
        int
            `watch_code`.
        """
        self.calls.append("watch")
        return self.watch_code

    def stop(self) -> None:
        """Record the call."""
        self.calls.append("stop")


def _install_fake_supervisor(monkeypatch: pytest.MonkeyPatch, fake: FakeSupervisor) -> None:
    """Replace `server.make_supervisor` with a factory returning `fake`.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch fixture.
    fake
        The fake supervisor to return; its `config` is filled in.
    """

    def factory(config: SupervisorConfig) -> FakeSupervisor:
        """Return `fake`, recording the config it was built with.

        Parameters
        ----------
        config
            The supervisor configuration `start` built.

        Returns
        -------
        FakeSupervisor
            `fake`.
        """
        fake.config = config
        return fake

    monkeypatch.setattr(server, "make_supervisor", factory)


def _stub_environment(monkeypatch: pytest.MonkeyPatch, *, port_is_free: bool = True) -> None:
    """Stub the port check and script lookup so `start` never touches the real system.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch fixture.
    port_is_free
        What `server.port_free` reports.
    """
    monkeypatch.setattr(server, "port_free", lambda port: port_is_free)
    monkeypatch.setattr(server, "find_script", lambda name: Path(f"/fake/{name}"))


def test_start_without_local_exits_2(tmp_path: Path) -> None:
    """`start` without `--local` is a usage error pointing at the Compose bundle."""
    result = invoke(make_state(tmp_path, FakeApi()), "server", "start")

    assert result.exit_code == 2
    assert "--local" in result.stderr
    assert "lakana bundle init" in result.stderr
    assert "M4d" not in result.stderr


def test_a_python_without_waitid_is_refused_before_anything_is_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MacOS before Python 3.13 has no `os.waitid`; say so before a worker refuses.

    Nothing is prepared or started: no data directory, no supervisor, no port probe.
    """
    monkeypatch.delattr(server.os, "waitid")

    def fail(*_: object, **__: object) -> None:
        """Fail the test: nothing may be started.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError("nothing may be started on a Python without os.waitid")

    monkeypatch.setattr(server, "make_supervisor", fail)
    monkeypatch.setattr(server, "port_free", fail)
    monkeypatch.setattr(server, "prepare_data_dir", fail)

    result = invoke(make_state(tmp_path, FakeApi()), *_start_args(tmp_path / "data"))

    assert result.exit_code == 2
    err = " ".join(result.stderr.split())
    assert "this Python lacks os.waitid, which the worker needs to supervise query processes" in err
    assert "(macOS has it from Python 3.13)" in err
    assert "on macOS, install lakana with Python 3.13 or newer" in err
    assert "lakana server start --local" in err
    assert not (tmp_path / "data").exists()


def test_missing_extra_exits_3_with_the_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing `lakana[server]` extra exits 3 with the install hint."""
    from lakana.cli._state import CliError

    monkeypatch.setattr(server, "port_free", lambda port: True)

    def boom(name: str) -> Path:
        """Raise the same error `find_script` raises when the extra is missing.

        Parameters
        ----------
        name
            The script name (unused).

        Raises
        ------
        CliError
            Always, mirroring `_scripts.find_script`'s real behaviour.
        """
        raise CliError(
            'Local mode needs the server packages: pip install "lakana[server]"',
            3,
            kind="MissingServerExtra",
        )

    monkeypatch.setattr(server, "find_script", boom)

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "server",
        "start",
        "--local",
        "--data-dir",
        str(tmp_path / "data"),
    )

    assert result.exit_code == 3
    assert 'pip install "lakana[server]"' in result.stderr


def test_port_in_use_exits_2_without_calling_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A port already in use is refused before any child is built."""
    monkeypatch.setattr(server, "port_free", lambda port: False)

    def fail(config: SupervisorConfig) -> FakeSupervisor:
        """Fail the test if the supervisor is ever built.

        Parameters
        ----------
        config
            Unused.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError("make_supervisor must not be called when the port is in use")

    monkeypatch.setattr(server, "make_supervisor", fail)

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "server",
        "start",
        "--local",
        "--data-dir",
        str(tmp_path / "data"),
        "--port",
        "9999",
    )

    assert result.exit_code == 2
    assert "9999" in result.stderr
    err = " ".join(result.stderr.split())
    assert "Error: Port 9999 is in use." in err
    assert "another program, or a Lakana instance started with a different --data-dir" in err
    assert "lakana server status --data-dir DIR" in err
    assert "may already be running" not in err
    assert "--port" in err
    assert "profile 'local'" in err


def _start_args(data_dir_path: Path, port: str = "8765") -> tuple[str, ...]:
    """Build the `server start --local` command line.

    Parameters
    ----------
    data_dir_path
        The data directory.
    port
        The port.

    Returns
    -------
    tuple of str
        The arguments.
    """
    return ("server", "start", "--local", "--data-dir", str(data_dir_path), "--port", port)


def _lock_is_free(root: DataDir) -> bool:
    """Report whether the data directory's lock can be taken right now.

    Parameters
    ----------
    root
        The data directory.

    Returns
    -------
    bool
        `True` if nothing holds the lock.
    """
    with try_locked(root.lock_path) as free:
        return free


def test_a_held_lock_refuses_start_before_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process holding `<data-dir>/lock` makes `start` exit 2 before any child."""
    _stub_environment(monkeypatch)
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)

    with try_locked(root.lock_path) as held:
        assert held
        result = invoke(make_state(tmp_path, FakeApi()), *_start_args(data_dir_path))

    assert result.exit_code == 2
    assert f"Lakana is already running from {root.root}" in result.stderr
    assert " at http://" not in result.stderr
    assert fake.calls == []
    assert fake.config is None


def test_a_held_lock_refuses_start_even_if_healthz_does_not_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow `/healthz` no longer makes a live instance look stale: the lock decides."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: True)
    monkeypatch.setattr(server, "_probe_health", lambda url: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    write_pidfile(root.server_pid, 999, 8765)

    with try_locked(root.lock_path):
        result = invoke(make_state(tmp_path, FakeApi()), *_start_args(data_dir_path))

    assert result.exit_code == 2
    assert "already running" in result.stderr
    assert "at http://localhost:8765" in result.stderr
    assert root.server_pid.exists()


def test_a_free_lock_with_a_live_healthy_pidfile_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older lakana holds no lock: a live PID answering `/healthz` still refuses `start`."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: True)
    monkeypatch.setattr(server, "_probe_health", lambda url: True)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    write_pidfile(root.server_pid, 999, 8765)
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)

    result = invoke(make_state(tmp_path, FakeApi()), *_start_args(data_dir_path))

    assert result.exit_code == 2
    assert "already running" in result.stderr
    assert fake.calls == []
    assert root.server_pid.exists()
    assert _lock_is_free(root)


@pytest.mark.parametrize(
    ("alive", "healthy"), [(False, True), (True, False)], ids=["dead-pid", "no-healthz"]
)
def test_a_free_lock_with_a_stale_pidfile_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alive: bool, healthy: bool
) -> None:
    """A dead PID, or one that doesn't answer `/healthz`, with a free lock is stale."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: alive)
    monkeypatch.setattr(server, "_probe_health", lambda url: healthy)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    write_pidfile(root.server_pid, 999, 8765)
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)

    result = invoke(make_state(tmp_path, api), *_start_args(data_dir_path))

    assert result.exit_code == 0, result.stderr
    assert "migrate" in fake.calls


def test_the_lock_is_held_during_migrate_and_released_after_a_normal_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock covers the window before any pidfile exists, and is gone once `start` returns."""
    _stub_environment(monkeypatch)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    free_at: dict[str, bool] = {}

    class CheckingSupervisor(FakeSupervisor):
        """Records whether the lock is free at `migrate` and at `watch`."""

        def migrate(self) -> None:
            """Record lock state, then behave like `FakeSupervisor`."""
            free_at["migrate"] = _lock_is_free(root)
            super().migrate()

        def watch(self) -> int:
            """Record lock state, then behave like `FakeSupervisor`.

            Returns
            -------
            int
                `watch_code`.
            """
            free_at["watch"] = _lock_is_free(root)
            return super().watch()

    _install_fake_supervisor(monkeypatch, CheckingSupervisor())
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)

    result = invoke(make_state(tmp_path, api), *_start_args(data_dir_path))

    assert result.exit_code == 0, result.stderr
    assert free_at == {"migrate": False, "watch": False}
    assert _lock_is_free(root)


@pytest.mark.parametrize(
    "error", [SupervisorError("migration failed"), KeyboardInterrupt()], ids=["error", "ctrl-c"]
)
def test_the_lock_is_released_after_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    """A failed or interrupted run still lets go of the lock."""
    _stub_environment(monkeypatch)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    _install_fake_supervisor(monkeypatch, FakeSupervisor(migrate_error=error))

    invoke(make_state(tmp_path, FakeApi()), *_start_args(data_dir_path))

    assert _lock_is_free(root)


def test_the_lock_is_released_after_an_unexpected_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even an exception `start` doesn't handle releases the lock."""
    _stub_environment(monkeypatch)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    _install_fake_supervisor(monkeypatch, FakeSupervisor(migrate_error=RuntimeError("boom")))

    invoke(make_state(tmp_path, FakeApi()), *_start_args(data_dir_path))

    assert _lock_is_free(root)


def test_a_stale_pidfile_is_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An alive PID whose recorded port doesn't answer health is stale.

    The stale pidfiles must be gone *before*
    `migrate` runs, not merely by the time `start` finishes.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: True)
    monkeypatch.setattr(server, "_probe_health", lambda url: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    write_pidfile(root.server_pid, 999, 8765)
    write_pidfile(root.worker_pid, 998)
    pidfiles_present_at_migrate: list[bool] = []

    class CheckingSupervisor(FakeSupervisor):
        """Records whether either stale pidfile still exists when `migrate` runs."""

        def migrate(self) -> None:
            """Record the pidfiles' presence, then behave like `FakeSupervisor`."""
            pidfiles_present_at_migrate.append(root.server_pid.exists() or root.worker_pid.exists())
            super().migrate()

    fake = CheckingSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)

    result = invoke(
        make_state(tmp_path, api),
        "server",
        "start",
        "--local",
        "--data-dir",
        str(data_dir_path),
        "--port",
        "8765",
    )

    assert result.exit_code == 0, result.stderr
    assert "migrate" in fake.calls
    assert pidfiles_present_at_migrate == [False]


def test_first_run_bootstraps_the_environment_and_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first run passes the bootstrap credential, creates `dev`, and saves `local`."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)
    state = make_state(tmp_path, api)

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result.exit_code == 0, result.stderr
    assert fake.config is not None
    env = fake.config.env
    assert "LAKANA_BOOTSTRAP_PAT_KEY_ID" in env
    assert "LAKANA_BOOTSTRAP_PAT_SECRET" in env
    assert env["LAKANA_DATABASE__URL"] == f"sqlite:///{data_dir_path}/lakana.db"
    assert len(api.requests) == 1
    body = json.loads(api.requests[0].content)
    assert body["name"] == "dev"
    assert body["storage"]["root"] == f"file://{data_dir_path}/lake/dev"
    profiles = _profiles.load(state.profile_path())
    assert profiles["default_profile"] == "local"
    local = profiles["profiles"]["local"]
    assert local["url"] == "http://127.0.0.1:8765"
    assert local["default_environment"] == "dev"
    # Pin the saved token to the exact credential
    # the child env carried, not just its prefix -- a mismatch here would
    # pass every looser assertion and break every real first run.
    expected_token = (
        f"lakana_pat_{env['LAKANA_BOOTSTRAP_PAT_KEY_ID']}_{env['LAKANA_BOOTSTRAP_PAT_SECRET']}"
    )
    assert local["token"] == expected_token
    assert api.requests[0].headers["authorization"] == f"Bearer {local['token']}"
    assert "Lakana is running at http://localhost:8765" in result.stdout


class _RecordingStdout:
    """A minimal `sys.stdout` stand-in that records `write`/`flush` order.

    Not a full `TextIOBase`: `start` only ever calls `write` and `flush` on
    `sys.stdout`, so this records exactly those two calls, in order.
    """

    def __init__(self) -> None:
        """Start with an empty call log."""
        self.calls: list[str] = []

    def write(self, text: str) -> int:
        """Record the write.

        Parameters
        ----------
        text
            The text written.

        Returns
        -------
        int
            `len(text)`, matching `TextIOBase.write`'s return convention.
        """
        self.calls.append(f"write:{text}")
        return len(text)

    def flush(self) -> None:
        """Record the flush."""
        self.calls.append("flush")


@dataclass
class _FlushOrderSupervisor(FakeSupervisor):
    """A `FakeSupervisor` that snapshots whether stdout was flushed before `watch()` runs.

    Parameters
    ----------
    fake_stdout
        The `_RecordingStdout` `start` is writing the ready line to.
    """

    fake_stdout: _RecordingStdout | None = None
    flushed_before_watch: bool | None = None

    def watch(self) -> int:
        """Snapshot the flush state, then behave like the base class.

        Returns
        -------
        int
            `watch_code`, from `FakeSupervisor.watch`.
        """
        if self.fake_stdout is not None:
            self.flushed_before_watch = "flush" in self.fake_stdout.calls
        return super().watch()


def test_the_ready_line_is_flushed_before_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start` flushes the ready line immediately, not just eventually.

    Whenever stdout isn't a tty -- piped to a file, `tee`, or a subprocess
    reading it, as in a real `lakana server start --local` invocation or this
    suite's own subprocess integration test -- Python block-buffers it by
    default. An unflushed `write` can then sit invisible in that buffer
    until some unrelated output happens to force a flush, or forever if
    none ever does. A caller blocked in `readline()` waiting for exactly
    this line would hang. This pins that the flush happens right after the
    write, before `watch()` -- which is where a real run then blocks for
    the rest of the process's life, i.e. the one place a later, incidental
    flush could never rescue it (the previous,
    unflushed write was reproduced as a 626 s hang in the real end-to-end
    test, not the 283 s the write eventually appearing had suggested).
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    fake_stdout = _RecordingStdout()
    monkeypatch.setattr(server, "sys", SimpleNamespace(stdout=fake_stdout, stderr=sys.stderr))
    fake = _FlushOrderSupervisor(fake_stdout=fake_stdout)
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)
    state = make_state(tmp_path, api)

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result.exit_code == 0, result.stderr
    assert any("Lakana is running at" in call for call in fake_stdout.calls)
    assert fake.flushed_before_watch is True


def test_first_run_does_not_take_over_an_existing_default_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first run never replaces an already-set `default_profile`."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)
    state = make_state(tmp_path, api)
    _profiles.set_profile(
        state.profile_path(),
        "work",
        url="http://work.example",
        token="lakana_pat_k_s",
        default_environment=None,
    )

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result.exit_code == 0, result.stderr
    profiles = _profiles.load(state.profile_path())
    assert profiles["default_profile"] == "work"
    assert "local" in profiles["profiles"]


def test_first_run_warns_before_replacing_a_non_loopback_local_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overwriting a non-loopback `local` profile prints a warning.

    A first run unconditionally overwrote any existing profile named
    `local`, including one pointing at a remote server or another data
    directory's instance -- starting a first run on a second `--data-dir`
    silently replaced the first instance's token. The write must still
    happen (a first run has to save its own credential somewhere), but the
    URL it's replacing is now named in a warning.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)
    state = make_state(tmp_path, api)
    _profiles.set_profile(
        state.profile_path(),
        "local",
        url="https://lakana.example.com",
        token="lakana_pat_remote_token",
        default_environment=None,
    )

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result.exit_code == 0, result.stderr
    assert "https://lakana.example.com" in result.stderr
    profiles = _profiles.load(state.profile_path())
    local = profiles["profiles"]["local"]
    assert local["url"] == "http://127.0.0.1:8765"
    assert local["token"] != "lakana_pat_remote_token"


def test_first_run_against_a_fresh_local_profile_prints_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `local` profile yet is the ordinary case, not a replacement -- no warning."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)
    state = make_state(tmp_path, api)

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result.exit_code == 0, result.stderr
    assert result.stderr == ""


def test_later_run_skips_the_bootstrap_credential_and_environment_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later run (lakana.db already exists) passes no credential and creates nothing."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    root.db_path.touch()
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev", status=200, json=_DEV)
    state = make_state(tmp_path, api)
    _profiles.set_profile(
        state.profile_path(),
        "local",
        url="http://127.0.0.1:8765",
        token="lakana_pat_a_b",
        default_environment="dev",
    )

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result.exit_code == 0, result.stderr
    assert fake.config is not None
    env = fake.config.env
    assert "LAKANA_BOOTSTRAP_PAT_KEY_ID" not in env
    assert "LAKANA_BOOTSTRAP_PAT_SECRET" not in env
    # The only request is the "does dev exist?" check -- since it does,
    # nothing is created.
    assert [(r.method, r.url.path) for r in api.requests] == [("GET", "/api/v1/environments/dev")]


def test_later_run_drops_an_inherited_bootstrap_credential_from_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later run never inherits `LAKANA_BOOTSTRAP_PAT_*` from the shell.

    A stray value left over in the caller's own shell must not let a later
    run silently re-bootstrap.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    root.db_path.touch()
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev", status=200, json=_DEV)
    state = make_state(tmp_path, api)
    _profiles.set_profile(
        state.profile_path(),
        "local",
        url="http://127.0.0.1:8765",
        token="lakana_pat_a_b",
        default_environment="dev",
    )

    result = invoke(
        state,
        "server",
        "start",
        "--local",
        "--data-dir",
        str(data_dir_path),
        "--port",
        "8765",
        env={
            "LAKANA_BOOTSTRAP_PAT_KEY_ID": "leaked-key",
            "LAKANA_BOOTSTRAP_PAT_SECRET": "leaked-secret",
        },
    )

    assert result.exit_code == 0, result.stderr
    assert fake.config is not None
    assert "LAKANA_BOOTSTRAP_PAT_KEY_ID" not in fake.config.env
    assert "LAKANA_BOOTSTRAP_PAT_SECRET" not in fake.config.env


def test_later_run_with_a_different_port_updates_the_profile_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--port` changing on a later run updates the saved URL."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    root.db_path.touch()
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev", status=200, json=_DEV)
    state = make_state(tmp_path, api)
    _profiles.set_profile(
        state.profile_path(),
        "local",
        url="http://127.0.0.1:8765",
        token="lakana_pat_a_b",
        default_environment="dev",
    )

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "9999"
    )

    assert result.exit_code == 0, result.stderr
    assert "Updated profile 'local'" in result.stderr
    profiles = _profiles.load(state.profile_path())
    local = profiles["profiles"]["local"]
    assert local["url"] == "http://127.0.0.1:9999"
    assert local["token"] == "lakana_pat_a_b"
    assert local["default_environment"] == "dev"


def test_later_run_leaves_a_non_loopback_local_profile_untouched_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a loopback `local` profile is ever repointed.

    A `local` profile pointing somewhere else (a remote server, or another
    data directory's instance) must not be silently rewritten to this
    run's `127.0.0.1:<port>` while keeping its original, now-mismatched
    token -- that would leave every subsequent call authenticating with
    the wrong credential against the wrong server.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    root.db_path.touch()
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    state = make_state(tmp_path, FakeApi())
    _profiles.set_profile(
        state.profile_path(),
        "local",
        url="https://lakana.example.com",
        token="lakana_pat_remote_token",
        default_environment="dev",
    )

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result.exit_code == 0, result.stderr
    assert "Updated profile 'local'" not in result.stderr
    assert "points at" in result.stderr
    assert "https://lakana.example.com" in result.stderr
    profiles = _profiles.load(state.profile_path())
    local = profiles["profiles"]["local"]
    assert local["url"] == "https://lakana.example.com"
    assert local["token"] == "lakana_pat_remote_token"


def test_later_run_with_a_tokenless_local_profile_prints_the_recovery_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `local` profile with no `token` doesn't crash the run.

    `validate_profile_document` allows a profile with no `token` (e.g. one
    left over from a setup that relies on `LAKANA_TOKEN`); this must be
    treated like a missing `local` profile -- the recovery hint, and the
    already-running instance kept up -- never a `KeyError` that tears down
    the server and worker mid-run.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    root.db_path.touch()
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    state = make_state(tmp_path, FakeApi())
    _profiles.save(state.profile_path(), {"profiles": {"local": {"url": "http://127.0.0.1:8765"}}})

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "9999"
    )

    assert result.exit_code == 0, result.stderr
    assert "No 'local' profile found" in result.stderr
    assert str(data_dir_path) in result.stderr
    profiles = _profiles.load(state.profile_path())
    assert "token" not in profiles["profiles"]["local"]


def test_after_an_interrupted_first_run_the_next_start_is_a_later_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted first run's `lakana.db` makes the next one later."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    fake1 = FakeSupervisor(wait_ready_error=KeyboardInterrupt())
    _install_fake_supervisor(monkeypatch, fake1)
    api = FakeApi()
    state = make_state(tmp_path, api)

    result1 = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )
    assert result1.exit_code == 0, result1.stderr

    # The real `lakana-server migrate` would have created `lakana.db`; the fake
    # supervisor's `migrate()` never touches disk, so simulate that effect
    # to exercise what the *next* invocation sees.
    root = DataDir(data_dir_path)
    root.db_path.touch()
    fake2 = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake2)

    result2 = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result2.exit_code == 0, result2.stderr
    assert fake2.config is not None
    assert "LAKANA_BOOTSTRAP_PAT_KEY_ID" not in fake2.config.env
    assert "LAKANA_BOOTSTRAP_PAT_SECRET" not in fake2.config.env
    assert "No 'local' profile found" in result2.stderr
    assert api.requests == []


def test_later_run_without_a_local_profile_prints_the_recovery_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later run with no `local` profile keeps running and explains how to reset."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    root.db_path.touch()
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "server",
        "start",
        "--local",
        "--data-dir",
        str(data_dir_path),
        "--port",
        "8765",
    )

    assert result.exit_code == 0, result.stderr
    assert "No 'local' profile found" in result.stderr
    assert str(data_dir_path) in result.stderr


@pytest.mark.parametrize(
    ("get_status", "post_status"),
    [(401, None), (403, None), (500, None), (404, 403), (404, 500)],
)
def test_a_failed_dev_check_warns_and_keeps_the_instance_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, get_status: int, post_status: int | None
) -> None:
    """The later-run `dev` check is best-effort.

    A `local` profile whose token this instance rejects (e.g. overwritten by
    another data directory's first run), or a server error, must not tear
    down an instance that already started: warn and keep running.

    Parameters
    ----------
    tmp_path
        Pytest's per-test temporary directory.
    monkeypatch
        Pytest's monkeypatch fixture.
    get_status
        The status `GET /environments/dev` answers with.
    post_status
        The status `POST /environments` answers with, if it is reached.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    root.db_path.touch()
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev", status=get_status, json={"detail": "nope"})
    if post_status is not None:
        api.on("POST", "/api/v1/environments", status=post_status, json={"detail": "nope"})
    state = make_state(tmp_path, api)
    _profiles.set_profile(
        state.profile_path(),
        "local",
        url="http://127.0.0.1:8765",
        token="lakana_pat_a_b",
        default_environment="dev",
    )

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result.exit_code == 0, result.stderr
    assert "Couldn't check environment 'dev'" in result.stderr
    assert "lakana_pat_a_b" not in result.stderr
    assert "Lakana is running at" in result.stdout


def test_later_run_creates_a_missing_dev_environment_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later run recovers a `dev` missing from an interrupted first run.

    `_first_run_setup` saves the `local` profile before creating `dev`; if
    that create step never ran (a crash or Ctrl-C between the two), a later
    run with the saved, usable profile must create `dev` itself rather than
    only document the manual recovery command.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    root.db_path.touch()
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev", status=404, json={"detail": "not found"})
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)
    state = make_state(tmp_path, api)
    _profiles.set_profile(
        state.profile_path(),
        "local",
        url="http://127.0.0.1:8765",
        token="lakana_pat_a_b",
        default_environment="dev",
    )

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result.exit_code == 0, result.stderr
    assert [(r.method, r.url.path) for r in api.requests] == [
        ("GET", "/api/v1/environments/dev"),
        ("POST", "/api/v1/environments"),
    ]
    body = json.loads(api.requests[1].content)
    assert body["name"] == "dev"
    assert body["storage"]["root"] == f"file://{data_dir_path}/lake/dev"
    assert api.requests[1].headers["authorization"] == "Bearer lakana_pat_a_b"
    assert "Created environment 'dev'" in result.stderr
    assert f"file://{data_dir_path}/lake/dev" in result.stderr


def test_ctrl_c_during_first_run_setup_cleans_up_without_a_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C between readiness and profile setup leaves no trace."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    fake = FakeSupervisor(wait_ready_error=KeyboardInterrupt())
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    state = make_state(tmp_path, api)

    result = invoke(
        state, "server", "start", "--local", "--data-dir", str(data_dir_path), "--port", "8765"
    )

    assert result.exit_code == 0, result.stderr
    assert "stop" in fake.calls
    root = DataDir(data_dir_path)
    assert not root.server_pid.exists()
    assert not root.worker_pid.exists()
    assert api.requests == []
    profiles = _profiles.load(state.profile_path())
    assert "local" not in profiles.get("profiles", {})


def test_migration_failure_exits_1_with_the_output_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `SupervisorError` from `migrate` exits 1, printing the message and output."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    fake = FakeSupervisor(
        migrate_error=SupervisorError("lakana-server migrate exited with code 3", output="boom\n")
    )
    _install_fake_supervisor(monkeypatch, fake)

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "server",
        "start",
        "--local",
        "--data-dir",
        str(tmp_path / "data"),
        "--port",
        "8765",
    )

    assert result.exit_code == 1
    assert "Error: lakana-server migrate exited with code 3" in result.stderr
    assert "boom" in result.stderr
    assert "stop" in fake.calls


def test_migration_failure_with_no_output_still_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `SupervisorError` with no captured output (the default) is handled too."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    fake = FakeSupervisor(migrate_error=SupervisorError("lakana-server migrate exited with code 1"))
    _install_fake_supervisor(monkeypatch, fake)

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "server",
        "start",
        "--local",
        "--data-dir",
        str(tmp_path / "data"),
        "--port",
        "8765",
    )

    assert result.exit_code == 1
    assert "Error: lakana-server migrate exited with code 1" in result.stderr


def test_watch_exit_code_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`watch()` returning a non-zero code becomes `start`'s own exit code."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    fake = FakeSupervisor(watch_code=2)
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)

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

    assert result.exit_code == 2


def test_a_write_pidfile_failure_is_reported_as_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan ruling C: an `OSError` writing a pidfile surfaces as `CliError` (exit 3)."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)

    def boom(path: Path, pid: int, port: int | None = None) -> None:
        """Simulate a disk failure writing the pidfile.

        Parameters
        ----------
        path
            The pidfile path.
        pid
            The process id (unused).
        port
            The port, if any (unused).

        Raises
        ------
        OSError
            Always.
        """
        raise OSError("simulated disk failure")

    monkeypatch.setattr(server, "write_pidfile", boom)

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "server",
        "start",
        "--local",
        "--data-dir",
        str(tmp_path / "data"),
        "--port",
        "8765",
    )

    assert result.exit_code == 3
    assert "simulated disk failure" in result.stderr


def test_a_pidfile_that_is_a_directory_is_a_clean_error_on_start(tmp_path: Path) -> None:
    """`server.pid` as a directory is a clean `CliError`.

    This is deliberately not treated as "no live instance": a directory
    where a pidfile belongs means the data directory is broken, not that
    it's safe to start a second instance against the same database.
    """
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    root.server_pid.mkdir()

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "server",
        "start",
        "--local",
        "--data-dir",
        str(data_dir_path),
        "--port",
        "8765",
    )

    assert result.exit_code == 3
    assert str(root.server_pid) in result.stderr


def test_a_pidfile_that_is_a_directory_is_a_clean_error_on_status(tmp_path: Path) -> None:
    """Same, for `status`."""
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    root.server_pid.mkdir()

    result = invoke(
        make_state(tmp_path, FakeApi()), "server", "status", "--data-dir", str(data_dir_path)
    )

    assert result.exit_code == 3
    assert str(root.server_pid) in result.stderr


def test_pidfile_removal_attempts_both_files_and_still_restores_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both removals run, and SIGTERM is restored regardless.

    `remove_pidfile` fails for every path it's given, so under the old,
    flat `finally` this left the worker pidfile behind and leaked the
    `SIGTERM` handler into the rest of the process (this exact test used
    to demonstrate that leak before the fix).
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    previous_handler = signal.getsignal(signal.SIGTERM)
    fake = FakeSupervisor(wait_ready_error=KeyboardInterrupt())
    _install_fake_supervisor(monkeypatch, fake)
    attempted: list[Path] = []

    def boom(path: Path) -> None:
        """Record the attempt and simulate a failure removing the pidfile.

        Parameters
        ----------
        path
            The pidfile path.

        Raises
        ------
        OSError
            Always.
        """
        attempted.append(path)
        raise OSError("simulated removal failure")

    monkeypatch.setattr(server, "remove_pidfile", boom)
    data_dir_path = tmp_path / "data"

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "server",
        "start",
        "--local",
        "--data-dir",
        str(data_dir_path),
        "--port",
        "8765",
    )

    assert result.exit_code == 3
    assert "simulated removal failure" in result.stderr
    root = DataDir(data_dir_path)
    assert attempted == [root.server_pid, root.worker_pid]
    assert signal.getsignal(signal.SIGTERM) == previous_handler


def test_sigterm_handler_raises_keyboard_interrupt() -> None:
    """The installed `SIGTERM` handler takes the same path as Ctrl-C."""
    with pytest.raises(KeyboardInterrupt):
        server._raise_keyboard_interrupt(15, None)


def test_sigterm_handler_is_installed_during_start_and_restored_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler is live only for the run's duration."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    previous_handler = signal.getsignal(signal.SIGTERM)
    installed_during_run: list[object] = []

    class RecordingSupervisor(FakeSupervisor):
        """Captures the live `SIGTERM` handler while a child is "running"."""

        def migrate(self) -> None:
            """Record the current handler, then behave like `FakeSupervisor`."""
            installed_during_run.append(signal.getsignal(signal.SIGTERM))
            super().migrate()

    fake = RecordingSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)

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
    assert installed_during_run == [server._raise_keyboard_interrupt]
    assert signal.getsignal(signal.SIGTERM) == previous_handler


def test_status_reports_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`status` reports running when the PID is alive and health answers."""
    monkeypatch.setattr(server, "pid_alive", lambda pid: True)
    monkeypatch.setattr(server, "_probe_health", lambda url: True)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    write_pidfile(root.server_pid, 111, 8765)
    write_pidfile(root.worker_pid, 222)

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "--output",
        "json",
        "server",
        "status",
        "--data-dir",
        str(data_dir_path),
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "running": True,
        "url": "http://localhost:8765",
        "data_dir": str(data_dir_path),
        "server_pid": 111,
        "worker_pid": 222,
    }


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX only")
def test_sighup_handler_is_installed_during_start_and_restored_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`SIGHUP` gets the same handler as `SIGTERM`.

    Closing the terminal (or the SSH session) that started a foreground
    `start` must not orphan both children with live pidfiles.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    previous_handler = signal.getsignal(signal.SIGHUP)
    installed_during_run: list[object] = []

    class RecordingSupervisor(FakeSupervisor):
        """Captures the live `SIGHUP` handler while a child is "running"."""

        def migrate(self) -> None:
            """Record the current handler, then behave like `FakeSupervisor`."""
            installed_during_run.append(signal.getsignal(signal.SIGHUP))
            super().migrate()

    fake = RecordingSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)

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
    assert installed_during_run == [server._raise_keyboard_interrupt]
    assert signal.getsignal(signal.SIGHUP) == previous_handler


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX only")
def test_sighup_is_not_installed_over_an_existing_sig_ign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `SIGHUP` already `SIG_IGN` (e.g. `nohup`) is left alone, not overridden.

    Installing our own handler over an existing `SIG_IGN` would defeat
    `nohup`'s entire point.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    previous_handler = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        installed_during_run: list[object] = []

        class RecordingSupervisor(FakeSupervisor):
            """Captures the live `SIGHUP` handler while a child is "running"."""

            def migrate(self) -> None:
                """Record the current handler, then behave like `FakeSupervisor`."""
                installed_during_run.append(signal.getsignal(signal.SIGHUP))
                super().migrate()

        fake = RecordingSupervisor()
        _install_fake_supervisor(monkeypatch, fake)
        api = FakeApi()
        api.on("POST", "/api/v1/environments", status=201, json=_DEV)

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
        assert installed_during_run == [signal.SIG_IGN]
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous_handler)


def test_sigint_gets_the_default_handler_installed_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backgrounded start still stops on `SIGINT`.

    A backgrounded `start` (`&` from a non-interactive shell) inherits
    `SIGINT` as `SIG_IGN` from its shell; `kill -INT` on it would otherwise
    silently do nothing. `signal.default_int_handler` is installed
    explicitly so that always raises `KeyboardInterrupt`, restored after.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        installed_during_run: list[object] = []

        class RecordingSupervisor(FakeSupervisor):
            """Captures the live `SIGINT` handler while a child is "running"."""

            def migrate(self) -> None:
                """Record the current handler, then behave like `FakeSupervisor`."""
                installed_during_run.append(signal.getsignal(signal.SIGINT))
                super().migrate()

        fake = RecordingSupervisor()
        _install_fake_supervisor(monkeypatch, fake)
        api = FakeApi()
        api.on("POST", "/api/v1/environments", status=201, json=_DEV)

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
        assert installed_during_run == [signal.default_int_handler]
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def test_signals_are_ignored_for_the_duration_of_the_final_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second interrupt can't abort `sup.stop()`.

    `SIGTERM` must be `SIG_IGN` while `stop()` itself runs, so a second
    Ctrl-C/SIGTERM arriving mid-cleanup can't raise `KeyboardInterrupt`
    again and abort it partway through, and must be restored once `stop()`
    (and pidfile removal) completes.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    previous_handler = signal.getsignal(signal.SIGTERM)
    handler_during_stop: list[object] = []

    class RecordingSupervisor(FakeSupervisor):
        """Captures the live `SIGTERM` handler while `stop()` runs."""

        def stop(self) -> None:
            """Record the current handler, then behave like `FakeSupervisor`."""
            handler_during_stop.append(signal.getsignal(signal.SIGTERM))
            super().stop()

    fake = RecordingSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)

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
    assert handler_during_stop == [signal.SIG_IGN]
    assert signal.getsignal(signal.SIGTERM) == previous_handler


def test_status_resolves_a_relative_data_dir_to_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status` reports an absolute `data_dir` too."""
    monkeypatch.chdir(tmp_path)
    prepare_data_dir(Path("data"))

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "--output",
        "json",
        "server",
        "status",
        "--data-dir",
        "data",
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["data_dir"] == str(tmp_path / "data")


def test_status_reports_running_while_the_lock_is_held(tmp_path: Path) -> None:
    """A held lock means running, even before any pidfile exists (e.g. during migrate)."""
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)

    with try_locked(root.lock_path):
        result = invoke(
            make_state(tmp_path, FakeApi()),
            "--output",
            "json",
            "server",
            "status",
            "--data-dir",
            str(data_dir_path),
        )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "running": True,
        "url": None,
        "data_dir": str(data_dir_path),
        "server_pid": None,
        "worker_pid": None,
    }
    assert _lock_is_free(root)


def test_status_reports_the_url_when_the_lock_is_held_and_the_pidfile_has_a_port(
    tmp_path: Path,
) -> None:
    """With the lock held and a recorded port, `status` reports that port's URL."""
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    write_pidfile(root.server_pid, 111, 8765)

    with try_locked(root.lock_path):
        result = invoke(
            make_state(tmp_path, FakeApi()),
            "--output",
            "json",
            "server",
            "status",
            "--data-dir",
            str(data_dir_path),
        )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["url"] == "http://localhost:8765"


def test_status_does_not_create_the_data_directory_or_lock(tmp_path: Path) -> None:
    """`status` against a missing data directory reports not running and creates nothing."""
    data_dir_path = tmp_path / "missing"

    result = invoke(
        make_state(tmp_path, FakeApi()), "server", "status", "--data-dir", str(data_dir_path)
    )

    assert result.exit_code == 1
    assert not data_dir_path.exists()


def test_status_reports_not_running_with_no_pidfiles(tmp_path: Path) -> None:
    """`status` against a data directory with no pidfiles at all reports not running."""
    result = invoke(
        make_state(tmp_path, FakeApi()),
        "server",
        "status",
        "--data-dir",
        str(tmp_path / "data"),
    )

    assert result.exit_code == 1


def test_status_reports_not_running_for_a_stale_pidfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`status` treats an alive PID whose port doesn't answer health as not running."""
    monkeypatch.setattr(server, "pid_alive", lambda pid: True)
    monkeypatch.setattr(server, "_probe_health", lambda url: False)
    data_dir_path = tmp_path / "data"
    root = prepare_data_dir(data_dir_path)
    write_pidfile(root.server_pid, 111, 8765)

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "server",
        "status",
        "--data-dir",
        str(data_dir_path),
    )

    assert result.exit_code == 1


def test_make_supervisor_builds_a_real_supervisor() -> None:
    """The real, un-stubbed `make_supervisor` seam builds a genuine `Supervisor`."""
    config = SupervisorConfig(
        server_cmd=["x"], worker_cmd=["y"], migrate_cmd=["z"], env={}, health_url="http://x/healthz"
    )

    assert isinstance(server.make_supervisor(config), Supervisor)


def test_probe_health_against_a_closed_port_is_false() -> None:
    """The real `_probe_health`, against nothing listening."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]

    assert server._probe_health(f"http://127.0.0.1:{closed_port}") is False


def test_probe_health_against_a_real_200_is_true() -> None:
    """The real `_probe_health`, against a real listener answering `200`."""
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

        assert server._probe_health(f"http://127.0.0.1:{port}") is True

        thread.join(timeout=2)


def test_probe_health_ignores_http_proxy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loopback probe bypasses `HTTP_PROXY`.

    A caller with a proxy configured and no `NO_PROXY=127.0.0.1` would
    otherwise route the readiness/status probe through it, turning a dead
    proxy into a spurious 30 s readiness timeout against a perfectly good
    local server.
    """
    with socket.socket() as dead:
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
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

        assert server._probe_health(f"http://127.0.0.1:{port}") is True

        thread.join(timeout=2)


def test_reuse_addr_is_not_set_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SO_REUSEADDR` is never set on Windows.

    On Windows, `SO_REUSEADDR` allows binding a port already in use by
    another socket, which would make `port_free`'s pre-check always report
    "free" there.
    """
    monkeypatch.setattr(server.os, "name", "nt")
    calls: list[tuple[object, ...]] = []

    class FakeSocket:
        """Records `setsockopt` calls without touching a real socket."""

        def setsockopt(self, *args: object) -> None:
            """Record the call.

            Parameters
            ----------
            *args
                The `setsockopt` arguments.
            """
            calls.append(args)

    server._reuse_addr_if_supported(FakeSocket())

    assert calls == []


def test_reuse_addr_is_set_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SO_REUSEADDR` is still set everywhere except Windows."""
    monkeypatch.setattr(server.os, "name", "posix")
    calls: list[tuple[object, ...]] = []

    class FakeSocket:
        """Records `setsockopt` calls without touching a real socket."""

        def setsockopt(self, *args: object) -> None:
            """Record the call.

            Parameters
            ----------
            *args
                The `setsockopt` arguments.
            """
            calls.append(args)

    server._reuse_addr_if_supported(FakeSocket())

    assert calls == [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]


def test_port_free_reflects_a_real_socket(tmp_path: Path) -> None:
    """`port_free` against a real, briefly-bound socket."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        bound_port = sock.getsockname()[1]

        assert server.port_free(bound_port) is False

    assert server.port_free(bound_port) is True


def test_child_env_points_the_token_file_into_the_data_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later run that must mint (a first run whose migrate failed) writes into the data dir.

    The server has no default path of its own (#208); local mode supplies one inside the 0700
    data directory so that recovery needs no setting. An inherited value wins.
    """
    from typing import cast

    data_dir = cast(
        "DataDir",
        SimpleNamespace(database_url="sqlite:///x.db", bootstrap_token_path=Path("/d/token")),
    )
    monkeypatch.delenv("LAKANA_BOOTSTRAP_TOKEN_FILE", raising=False)
    env = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert env["LAKANA_BOOTSTRAP_TOKEN_FILE"] == "/d/token"
    monkeypatch.setenv("LAKANA_BOOTSTRAP_TOKEN_FILE", "/elsewhere")
    given = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert given["LAKANA_BOOTSTRAP_TOKEN_FILE"] == "/elsewhere"


def test_local_mode_sets_repo_roots_to_home_unless_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Local repositories under $HOME are deployable in local mode by default."""
    from typing import cast

    data_dir = cast(
        "DataDir",
        SimpleNamespace(
            database_url="sqlite:///x.db", bootstrap_token_path=Path("/d/bootstrap-token")
        ),
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LAKANA_REPO_ROOTS", raising=False)
    env = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert json.loads(env["LAKANA_REPO_ROOTS"]) == [str(tmp_path.resolve())]
    monkeypatch.setenv("LAKANA_REPO_ROOTS", '["/srv"]')
    given = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert given["LAKANA_REPO_ROOTS"] == '["/srv"]'


def test_child_env_passes_the_log_level_to_the_server_and_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both local children get `LAKANA_LOG__LEVEL` from the environment."""
    from typing import cast

    data_dir = cast(
        "DataDir",
        SimpleNamespace(
            database_url="sqlite:///x.db", bootstrap_token_path=Path("/d/bootstrap-token")
        ),
    )
    monkeypatch.setenv("LAKANA_LOG__LEVEL", "debug")
    env = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert env["LAKANA_LOG__LEVEL"] == "debug"


def test_child_env_tells_the_worker_where_the_server_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local mode can run Python assets; a value the user set wins."""
    from typing import cast

    data_dir = cast(
        "DataDir",
        SimpleNamespace(
            database_url="sqlite:///x.db", bootstrap_token_path=Path("/d/bootstrap-token")
        ),
    )
    monkeypatch.delenv("LAKANA_RUN__SERVER_URL", raising=False)
    env = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert env["LAKANA_RUN__SERVER_URL"] == "http://127.0.0.1:8765"
    monkeypatch.setenv("LAKANA_RUN__SERVER_URL", "http://elsewhere:9000")
    given = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert given["LAKANA_RUN__SERVER_URL"] == "http://elsewhere:9000"


def test_child_env_turns_off_the_request_log_unless_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local mode drops the per-request line only; the log level is left alone."""
    from typing import cast

    data_dir = cast(
        "DataDir",
        SimpleNamespace(
            database_url="sqlite:///x.db", bootstrap_token_path=Path("/d/bootstrap-token")
        ),
    )
    monkeypatch.delenv("LAKANA_LOG__LEVEL", raising=False)
    monkeypatch.delenv("LAKANA_LOG__ACCESS", raising=False)
    quiet = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert quiet["LAKANA_LOG__ACCESS"] == "false"
    assert "LAKANA_LOG__LEVEL" not in quiet
    loud = server._child_env(data_dir, 8765, "k", key_id=None, secret=None, log_requests=True)
    assert "LAKANA_LOG__ACCESS" not in loud
    monkeypatch.setenv("LAKANA_LOG__ACCESS", "true")
    given = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert given["LAKANA_LOG__ACCESS"] == "true"


def test_start_passes_the_request_log_choice_to_the_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--log-requests` leaves the request log on; without it the children are told it is off."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    monkeypatch.delenv("LAKANA_LOG__ACCESS", raising=False)
    values = []
    for extra in ((), ("--log-requests",)):
        fake = FakeSupervisor()
        _install_fake_supervisor(monkeypatch, fake)
        api = FakeApi()
        api.on("POST", "/api/v1/environments", status=201, json=_DEV)
        state = make_state(tmp_path, api)
        result = invoke(state, *_start_args(tmp_path / f"data{len(extra)}"), *extra)
        assert result.exit_code == 0, result.stderr
        assert fake.config is not None
        values.append(fake.config.env.get("LAKANA_LOG__ACCESS"))
    assert values == ["false", None]


def test_the_ready_line_is_the_last_thing_start_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is written to stdout after the ready line, so it stays at the bottom."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    fake_stdout = _RecordingStdout()
    monkeypatch.setattr(server, "sys", SimpleNamespace(stdout=fake_stdout, stderr=sys.stderr))
    _install_fake_supervisor(monkeypatch, FakeSupervisor())
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)
    state = make_state(tmp_path, api)

    result = invoke(state, *_start_args(tmp_path / "data"))

    assert result.exit_code == 0, result.stderr
    writes = [call for call in fake_stdout.calls if call.startswith("write:")]
    assert writes[-1].startswith("write:Lakana is running at")


_LOCAL_LINK = "http://localhost:8765/sign-in/k1#s=topsecret"


@dataclass
class _BrowserRun:
    """What one `server start --local` with the interface on did.

    Parameters
    ----------
    result
        The CLI result.
    api
        The fake API, with every request it received.
    opened
        Every URL handed to `webbrowser.open`.
    files_when_opened
        For each opened URL: the names in the data directory at the moment it was opened.
    data_dir
        The data directory.
    env
        The environment the children got.
    """

    result: Result
    api: FakeApi
    opened: list[str]
    files_when_opened: list[set[str]]
    data_dir: Path
    env: dict[str, str]


def _fake_browser(monkeypatch: pytest.MonkeyPatch, open_url: Callable[[str], object]) -> None:
    """Make `_sign_the_browser_in` hand its link to `open_url`, synchronously.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch fixture.
    open_url
        Receives the URL the browser would open.
    """
    monkeypatch.setattr(server, "_browser", lambda: SimpleNamespace(open=open_url))
    monkeypatch.setattr(server, "_start_opener", lambda browser, url: browser.open(url))


def _start_with_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_run: bool = True,
    terminal: bool = True,
    ui: str | None = "true",
    extra: tuple[str, ...] = (),
) -> _BrowserRun:
    """Run `server start --local` against fakes, recording what the browser was given.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Pytest's monkeypatch fixture.
    first_run
        Whether the data directory is new; otherwise a `local` profile exists.
    terminal
        What `_stdout_is_terminal` reports.
    ui
        `LAKANA_UI__ENABLED` for the run, or None to leave it unset.
    extra
        More command-line arguments.

    Returns
    -------
    _BrowserRun
        The recordings.
    """
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    data_dir_path = tmp_path / "data"
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)
    api.on("GET", "/api/v1/environments/dev", json=_DEV)
    api.on(
        "POST",
        "/api/v1/sign-in/local-link",
        json={"url": _LOCAL_LINK, "expires_at": "2026-11-08T10:10:00Z"},
    )
    state = make_state(tmp_path, api)
    if not first_run:
        prepare_data_dir(data_dir_path).db_path.touch()
        _profiles.set_profile(
            state.profile_path(),
            "local",
            url="http://127.0.0.1:8765",
            token="lakana_pat_a_b",
            default_environment="dev",
        )
    opened: list[str] = []
    files_when_opened: list[set[str]] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        files_when_opened.append({path.name for path in data_dir_path.iterdir()})
        return True

    _fake_browser(monkeypatch, fake_open)
    monkeypatch.setattr(server, "_stdout_is_terminal", lambda: terminal)
    env = {} if ui is None else {"LAKANA_UI__ENABLED": ui}
    if ui is None:
        monkeypatch.delenv("LAKANA_UI__ENABLED", raising=False)
    result = invoke(state, *_start_args(data_dir_path), *extra, env=env)
    assert fake.config is not None
    return _BrowserRun(result, api, opened, files_when_opened, data_dir_path, fake.config.env)


def test_first_run_opens_the_sign_in_link_itself_in_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser gets the web link (so the http handler opens it), and no file is written."""
    run = _start_with_ui(tmp_path, monkeypatch)

    assert run.result.exit_code == 0, run.result.stderr
    assert run.opened == [_LOCAL_LINK]
    assert all("sign-in.html" not in names for names in run.files_when_opened)
    assert not (run.data_dir / "sign-in.html").exists()
    request = next(r for r in run.api.requests if r.url.path == "/api/v1/sign-in/local-link")
    token = _profiles.load(tmp_path / "lakana" / "config.toml")["profiles"]["local"]["token"]
    assert request.headers["authorization"] == f"Bearer {token}"


def test_later_run_signs_the_browser_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An upgraded instance (profile `local` exists) signs in with the profile's own token."""
    run = _start_with_ui(tmp_path, monkeypatch, first_run=False)

    assert run.result.exit_code == 0, run.result.stderr
    request = next(r for r in run.api.requests if r.url.path == "/api/v1/sign-in/local-link")
    assert request.headers["authorization"] == "Bearer lakana_pat_a_b"
    assert run.opened == [_LOCAL_LINK]


def test_local_run_sets_local_sign_in_and_prints_localhost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server gets local sign-in and its public address; the person sees `localhost`."""
    monkeypatch.delenv("LAKANA_UI__PUBLIC_URL", raising=False)
    run = _start_with_ui(tmp_path, monkeypatch, terminal=False)

    assert run.result.exit_code == 0, run.result.stderr
    assert run.env["LAKANA_UI__LOCAL_SIGN_IN"] == "true"
    assert run.env["LAKANA_UI__PUBLIC_URL"] == "http://localhost:8765"
    assert run.env["LAKANA_HOST"] == "127.0.0.1"
    assert "Lakana is running at http://localhost:8765" in run.result.stdout
    assert "127.0.0.1" not in run.result.stdout + run.result.stderr


def test_an_inherited_public_url_of_this_instance_is_kept_quietly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same address the local start would set: no notice."""
    from typing import cast

    data_dir = cast(
        "DataDir",
        SimpleNamespace(database_url="sqlite:///x.db", bootstrap_token_path=Path("/d/t")),
    )
    monkeypatch.setenv("LAKANA_UI__PUBLIC_URL", "http://localhost:8765")
    env = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert env["LAKANA_UI__PUBLIC_URL"] == "http://localhost:8765"
    assert capsys.readouterr().err == ""


def test_no_browser_flag_prints_nothing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-browser`: no link is asked for, nothing is opened or printed."""
    run = _start_with_ui(tmp_path, monkeypatch, extra=("--no-browser",))

    assert run.result.exit_code == 0, run.result.stderr
    assert run.opened == []
    assert "/api/v1/sign-in/local-link" not in [r.url.path for r in run.api.requests]
    assert "topsecret" not in run.result.stdout + run.result.stderr
    assert not (run.data_dir / "sign-in.html").exists()


def test_link_is_printed_only_to_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal gets the fallback line with the link; a pipe or a file never does."""
    piped = _start_with_ui(tmp_path / "piped", monkeypatch, terminal=False)
    shown = _start_with_ui(tmp_path / "shown", monkeypatch, terminal=True)

    assert piped.result.exit_code == 0 and shown.result.exit_code == 0
    assert "topsecret" not in piped.result.stdout + piped.result.stderr
    assert (
        "If the browser did not open signed in, open this link within 1 minute (it works once; "
        f"after that, start Lakana again for a new one): {_LOCAL_LINK}"
        in " ".join(shown.result.stdout.split())
    )


def test_with_the_interface_off_one_line_names_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No link and no browser while the interface is off; one line says how to turn it on."""
    run = _start_with_ui(tmp_path, monkeypatch, ui=None)

    assert run.result.exit_code == 0, run.result.stderr
    assert run.opened == []
    assert "/api/v1/sign-in/local-link" not in [r.url.path for r in run.api.requests]
    lines = [line for line in run.result.stdout.splitlines() if "LAKANA_UI__ENABLED" in line]
    assert len(lines) == 1


def test_a_refused_local_link_keeps_the_instance_running_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal is a warning naming the cause and the next step, never a stop."""
    _stub_environment(monkeypatch)
    monkeypatch.setattr(server, "pid_alive", lambda pid: False)
    fake = FakeSupervisor()
    _install_fake_supervisor(monkeypatch, fake)
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)
    api.on(
        "POST",
        "/api/v1/sign-in/local-link",
        status=403,
        json={"detail": "A local sign-in link is minted with your own personal access token."},
    )
    opened: list[str] = []
    _fake_browser(monkeypatch, opened.append)
    result = invoke(
        make_state(tmp_path, api),
        *_start_args(tmp_path / "data"),
        env={"LAKANA_UI__ENABLED": "true"},
    )

    assert result.exit_code == 0, result.stderr
    assert "watch" in fake.calls
    assert opened == []
    err = " ".join(result.stderr.split())
    assert "personal access token" in err and "http://localhost:8765" in err


def _direct_sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link: str = _LOCAL_LINK,
    token: str | None = "lakana_pat_a_b",
) -> tuple[list[str], FakeApi]:
    """Call `_sign_the_browser_in` itself, with the interface on and the browser faked.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Pytest's monkeypatch fixture.
    link
        The link the fake server answers.
    token
        The token handed in.

    Returns
    -------
    tuple
        The opened URLs and the fake API.
    """
    api = FakeApi()
    api.on("POST", "/api/v1/sign-in/local-link", json={"url": link, "expires_at": "2026-11-08"})
    opened: list[str] = []
    _fake_browser(monkeypatch, opened.append)
    monkeypatch.setattr(server, "_stdout_is_terminal", lambda: False)
    server._sign_the_browser_in(
        make_state(tmp_path, api), 8765, token, {"LAKANA_UI__ENABLED": "TRUE"}
    )
    return opened, api


def test_a_link_that_is_not_a_web_address_is_never_handed_to_the_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only an `http(s)` link is handed to the browser."""
    opened, _ = _direct_sign_in(tmp_path, monkeypatch, link="javascript:x()")

    assert opened == []
    assert "not a web address" in capsys.readouterr().err


def test_without_a_token_of_this_instance_nothing_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later run whose `local` profile is missing or another instance's asks for no link."""
    opened, api = _direct_sign_in(tmp_path, monkeypatch, token=None)

    assert opened == [] and api.requests == []


def test_stdout_is_a_terminal_asks_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seam reports what `sys.stdout.isatty` says."""
    monkeypatch.setattr(server.sys, "stdout", SimpleNamespace(isatty=lambda: True))
    assert server._stdout_is_terminal() is True


def test_a_console_browser_or_no_display_is_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A text browser would take over the terminal; without a display nothing can open."""
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(server.webbrowser, "get", lambda: server.webbrowser.GenericBrowser("w3m"))
    assert server._browser() is None
    graphical = server.webbrowser.BackgroundBrowser("firefox")
    monkeypatch.setattr(server.webbrowser, "get", lambda: graphical)
    assert server._browser() is graphical
    monkeypatch.delenv("DISPLAY")
    assert server._browser() is None
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert server._browser() is graphical
    in_background = server.webbrowser.BackgroundBrowser("/usr/bin/w3m")
    monkeypatch.setattr(server.webbrowser, "get", lambda: in_background)
    assert server._browser() is None

    def no_browser() -> object:
        raise server.webbrowser.Error("could not locate runnable browser")

    monkeypatch.setattr(server.webbrowser, "get", no_browser)
    assert server._browser() is None


def test_a_browser_that_does_not_return_never_holds_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opener runs on its own thread: start-up goes on while it is still busy."""
    release = threading.Event()
    opened: list[str] = []

    def blocking_open(url: str) -> bool:
        opened.append(url)
        release.wait(5)
        return True

    monkeypatch.setattr(server, "_browser", lambda: SimpleNamespace(open=blocking_open))
    api = FakeApi()
    api.on(
        "POST", "/api/v1/sign-in/local-link", json={"url": _LOCAL_LINK, "expires_at": "2026-11-08"}
    )
    monkeypatch.setattr(server, "_stdout_is_terminal", lambda: False)
    began = time.monotonic()
    server._sign_the_browser_in(
        make_state(tmp_path, api), 8765, "lakana_pat_a_b", {"LAKANA_UI__ENABLED": "true"}
    )
    took = time.monotonic() - began
    release.set()
    assert took < 1.0


def test_without_a_browser_a_terminal_still_gets_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No display on Linux: nothing is started or written, and a terminal gets the line."""
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    started: list[object] = []
    monkeypatch.setattr(server, "_start_opener", lambda browser, url: started.append(url))
    api = FakeApi()
    api.on(
        "POST", "/api/v1/sign-in/local-link", json={"url": _LOCAL_LINK, "expires_at": "2026-11-08"}
    )
    monkeypatch.setattr(server, "_stdout_is_terminal", lambda: True)
    root = prepare_data_dir(tmp_path / "data")
    server._sign_the_browser_in(
        make_state(tmp_path, api), 8765, "lakana_pat_a_b", {"LAKANA_UI__ENABLED": "true"}
    )
    assert started == []
    assert not (root.root / "sign-in.html").exists()
    out = " ".join(capsys.readouterr().out.split())
    assert f"start Lakana again for a new one): {_LOCAL_LINK}" in out


def test_an_inherited_public_url_off_localhost_is_replaced_with_a_notice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A shell that sourced a bundle's .env still starts locally; one line names the variable."""
    from typing import cast

    data_dir = cast(
        "DataDir",
        SimpleNamespace(database_url="sqlite:///x.db", bootstrap_token_path=Path("/d/t")),
    )
    monkeypatch.setenv("LAKANA_UI__PUBLIC_URL", "https://lakana.example.com")
    env = server._child_env(data_dir, 8765, "k", key_id=None, secret=None)
    assert env["LAKANA_UI__PUBLIC_URL"] == "http://localhost:8765"
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert "LAKANA_UI__PUBLIC_URL=https://lakana.example.com" in err
    assert "http://localhost:8765" in err


def test_a_sign_in_page_an_older_version_left_is_removed_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older version handed the link over in `sign-in.html`; a start removes a leftover."""
    stale = prepare_data_dir(tmp_path / "data").root / "sign-in.html"
    stale.write_text("old link")
    run = _start_with_ui(tmp_path, monkeypatch, extra=("--no-browser",))
    assert run.result.exit_code == 0, run.result.stderr
    assert not stale.exists()


def test_a_later_run_without_this_instance_s_token_says_why_no_browser_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One line explains that profile 'local' holds no token of this instance."""
    opened, api = _direct_sign_in(tmp_path, monkeypatch, token=None)
    assert opened == [] and api.requests == []
    assert "profile 'local'" in capsys.readouterr().err
