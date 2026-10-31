"""`Launcher`: start-up checks, the five operations, and what it removes, against a fake engine."""
# ruff: file-ignore[any-type]  (Docker's JSON bodies and the tests' hooks are untyped by nature)

from __future__ import annotations

import base64
import errno
import io
import ipaddress
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from launcher_fakes import FakeEngine, settings

from lakana_worker.launcher import egress as egress_module
from lakana_worker.launcher import service as service_module
from lakana_worker.launcher.egress_token import read_key, verify
from lakana_worker.launcher.engine import EngineError
from lakana_worker.launcher.requests import (
    EGRESS_TOKEN_TOO_LONG,
    INGEST_PROBE_SCRIPT,
    LABEL_DEADLINE,
    RELAY_COMMAND,
    RELAY_PIDS,
    BuildRequest,
    CheckRequest,
    CleanupRequest,
    Limits,
    Refused,
    RelayRoute,
    RunRequest,
    StatusRequest,
    StopRequest,
    cache_volume,
    container_name,
    environment_volume,
    relay_address,
)
from lakana_worker.launcher.service import Launcher, StartError, read_frames

KEY = "a" * 64
LIMITS = Limits(memory_mb=256, cpus=1.0, pids=64, timeout_seconds=30)


def _run(task: str = "t1", owner: str = "w1", timeout: int = 30) -> RunRequest:
    """Build a run request.

    Parameters
    ----------
    task
        The task id.
    owner
        The worker.
    timeout
        Its timeout.

    Returns
    -------
    RunRequest
        The request.
    """
    return RunRequest(
        task=task,
        owner=owner,
        python="3.13",
        environment=KEY,
        attempt="attempt-x1",
        limits=Limits(256, 1.0, 64, timeout),
        kill_grace_seconds=3,
        stdin=b'{"v": 2}',
    )


def _build(build: str = "b1", key: str = KEY) -> BuildRequest:
    """Build a build request.

    Parameters
    ----------
    build
        The build id.
    key
        The environment key.

    Returns
    -------
    BuildRequest
        The request.
    """
    return BuildRequest(
        build=build,
        owner="w1",
        python="3.13",
        environment=key,
        attempt="attempt-x1",
        indexes=(),
        operator_index=None,
        targets=(("pypi.org", 443),),
        no_build=False,
        limits=LIMITS,
        max_bytes=1 << 30,
    )


def _started(engine: FakeEngine, tmp_path: Path, *, built: bool = True, **changes: Any) -> Launcher:
    """Build and start a launcher.

    Parameters
    ----------
    engine
        The fake engine.
    tmp_path
        The state directory.
    built
        Whether the environment of `KEY` counts as built already (a run needs it).
    **changes
        Settings to replace.

    Returns
    -------
    Launcher
        The started launcher.
    """
    launcher = Launcher(settings(tmp_path, **changes), engine)
    launcher.start()
    if built:
        _built(launcher, engine)
    return launcher


def _built(launcher: Launcher, engine: FakeEngine, key: str = KEY) -> None:
    """Make an environment count as built: its marker exists and its volume does.

    Parameters
    ----------
    launcher
        The started launcher.
    engine
        Its fake engine.
    key
        The environment key.
    """
    volume = environment_volume(launcher.settings, "3.13", key)
    launcher._mark_complete(volume)
    engine.volumes.add(volume)


def _logs(emitted: list[dict[str, object]]) -> bytes:
    """Join the log lines a run emitted.

    Parameters
    ----------
    emitted
        What the launcher emitted.

    Returns
    -------
    bytes
        The decoded output.
    """
    return b"".join(base64.b64decode(str(e["log"])) for e in emitted if "log" in e)


def test_start_refuses_an_engine_older_than_28(tmp_path: Path) -> None:
    """Without the isolated gateway mode a task network would reach the host."""
    with pytest.raises(StartError, match="Docker Engine 28 or later is needed") as caught:
        _started(FakeEngine(version="27.5.1"), tmp_path)
    assert "27.5.1" in str(caught.value)


def test_start_removes_what_an_earlier_launcher_left(tmp_path: Path) -> None:
    """Owners run their tasks again; the launcher keeps nothing from before its start."""
    engine = FakeEngine()
    engine.containers["old"] = {
        "Name": "lakana-task-old",
        "exit": None,
        "spec": {"HostConfig": {"Mounts": []}},
        "Labels": {"lakana.launcher.stack": "lakana"},
    }
    engine._released["old"] = threading.Event()
    engine.networks["lakana-task-old"] = {
        "Labels": {"lakana.launcher.stack": "lakana"},
        "Containers": {},
    }

    _started(engine, tmp_path)

    assert "old" not in engine.containers and "lakana-task-old" not in engine.networks


def test_no_new_privileges_auto_stays_off_where_docker_refuses_it(tmp_path: Path) -> None:
    """Snap Docker refuses every exec with the option; auto turns it off there."""
    engine = FakeEngine(no_new_privileges_works=False)
    launcher = _started(engine, tmp_path)

    launcher.run(_run(), lambda line: None)

    assert launcher.no_new_privileges is False
    assert engine.created("task")[0]["HostConfig"]["SecurityOpt"] == []
    on = _started(FakeEngine(), tmp_path)
    assert on.no_new_privileges is True


def test_no_new_privileges_on_refuses_to_start_where_it_breaks(tmp_path: Path) -> None:
    """`on` is a promise: the launcher does not start without the option."""
    with pytest.raises(StartError, match="NO_NEW_PRIVILEGES is on"):
        _started(FakeEngine(no_new_privileges_works=False), tmp_path, no_new_privileges="on")


def test_a_run_streams_output_and_removes_its_container_and_network(tmp_path: Path) -> None:
    """The task gets its description on stdin, the server joins its network as `server`."""
    engine = FakeEngine(output=[b"hel", b"lo\n"])
    launcher = _started(engine, tmp_path)
    emitted: list[dict[str, object]] = []
    network = container_name(launcher.settings, "task", "t1")
    seen: dict[str, Any] = {}
    original = engine._network

    def spy(method: str, name: str, action: str | None, body: Any) -> tuple[int, Any]:
        if action == "connect":
            seen[name] = body
        return original(method, name, action, body)

    engine._network = spy  # ty: ignore[invalid-assignment]

    result = launcher.run(_run(), emitted.append)

    assert result == {"exit": 0, "oom_killed": False, "timed_out": False, "stopped": False}
    assert _logs(emitted) == b"hello\n"
    assert engine.stdin == [b'{"v": 2}']
    assert seen[network] == {"Container": "c-server", "EndpointConfig": {"Aliases": ["server"]}}
    assert [
        c for c in engine.containers.values() if c["Labels"]["lakana.launcher.kind"] == "task"
    ] == []
    assert network not in engine.networks


def test_a_run_reports_an_out_of_memory_kill_and_the_exit_code(tmp_path: Path) -> None:
    """`OOMKilled` reaches the worker, which maps it to the memory stop reason."""
    engine = FakeEngine(exit_code=137, oom=True)

    result = _started(engine, tmp_path).run(_run(), lambda line: None)

    assert (result["exit"], result["oom_killed"]) == (137, True)


@pytest.mark.parametrize(("code", "oom"), [(137, True), (143, False), (1, False)])
def test_a_sigkill_nobody_asked_for_is_an_out_of_memory_kill(
    tmp_path: Path, code: int, oom: bool
) -> None:
    """Docker's `OOMKilled` can miss a kill by the memory limit; the SIGKILL exit cannot.

    Nothing but the kernel sends SIGKILL to a task the launcher neither stopped nor timed out,
    and the containerd event that sets the flag is lost when the container goes first.

    Parameters
    ----------
    tmp_path
        The launcher's directory.
    code
        The container's exit status, 128 + the signal for a signal.
    oom
        Whether it reads as an out-of-memory kill.
    """
    engine = FakeEngine(exit_code=code, oom=False)

    result = _started(engine, tmp_path).run(_run(), lambda line: None)

    assert (result["exit"], result["oom_killed"]) == (code, oom)


def test_the_timeout_stops_the_container_with_its_grace(tmp_path: Path) -> None:
    """At the timeout the launcher asks Docker to stop the container, SIGKILL after the grace."""
    engine = FakeEngine(hold=True)

    result = _started(engine, tmp_path).run(_run(timeout=1), lambda line: None)

    assert result["timed_out"] is True and result["exit"] == 137
    assert result["oom_killed"] is False, "the launcher's own SIGKILL is not a memory kill"
    stops = [(p, q) for m, p, q in engine.calls if p.endswith("/stop")]
    assert stops and stops[0][1] == {"t": "3"}


def test_stop_stops_a_running_task(tmp_path: Path) -> None:
    """A `stop` from another connection ends a `run` that is waiting on its container."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.run(_run(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))

    assert launcher.stop(StopRequest("t1", "w1")) == {"stopped": True}
    worker.join(10)

    assert results[0]["stopped"] is True and results[0]["oom_killed"] is False
    assert launcher.stop(StopRequest("t1", "w1")) == {"stopped": False}


@pytest.mark.parametrize("kind", ["task", "build"])
def test_a_second_owner_cannot_stop_the_first_owners_run(tmp_path: Path, kind: str) -> None:
    """#305: `stop` acts only on its owner's runs, and a refusal looks like an unknown id."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path, built=kind == "task")
    results: list[dict[str, object]] = []
    ident = "t1" if kind == "task" else "b1"

    def work() -> None:
        if kind == "task":
            results.append(launcher.run(_run(), lambda line: None))
        else:
            results.append(launcher.build(_build(), lambda line: None))

    worker = threading.Thread(target=work)
    worker.start()
    _until(lambda: _task_started(engine))

    foreign = launcher.stop(StopRequest(ident, "w2"))
    unknown = launcher.stop(StopRequest("nobody", "w2"))

    assert foreign == unknown == {"stopped": False}
    assert not any(p.endswith(("/stop", "/kill")) for _, p, _ in engine.calls)
    assert worker.is_alive()
    assert launcher.stop(StopRequest(ident, "w1")) == {"stopped": True}
    worker.join(10)
    assert results[0]["stopped"] is True


def test_a_second_owner_cannot_abandon_the_first_owners_run(tmp_path: Path) -> None:
    """#305: a connection of another owner that names a live id and closes kills nothing."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.run(_run(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))

    launcher.abandon(_named(launcher), "w2")
    launcher.forget(_named(launcher), "w2")

    assert not any(p.endswith(("/stop", "/kill")) for _, p, _ in engine.calls)
    assert worker.is_alive()
    launcher.abandon(_named(launcher), "w1")
    worker.join(10)
    assert results[0]["stopped"] is True


def test_another_owners_departure_before_a_run_registers_does_not_stop_it(
    tmp_path: Path,
) -> None:
    """#305: an abandonment is remembered for its own owner only."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    launcher.abandon(_named(launcher), "w2")

    result = launcher.run(_run(), lambda line: None)

    assert result["stopped"] is False and result["exit"] == 0


@pytest.mark.parametrize(
    "failure",
    [(500, {"message": "cannot stop container: permission denied"}), EngineError("timed out")],
    ids=["docker-refuses", "docker-unreachable"],
)
def test_a_stop_docker_did_not_carry_out_answers_false_and_a_retry_stops_the_task(
    tmp_path: Path, failure: Any
) -> None:
    """The caller asks again on False; a True it cannot trust would end its retries early."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.run(_run(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))
    real = engine.request
    _intercept(engine, lambda method, path, body: path.endswith("/stop"), failure)

    assert launcher.stop(StopRequest("t1", "w1")) == {"stopped": False}
    assert worker.is_alive()

    engine.request = real  # ty: ignore[invalid-assignment]
    assert launcher.stop(StopRequest("t1", "w1")) == {"stopped": True}
    worker.join(10)
    assert results[0]["stopped"] is True


def test_a_stop_of_a_container_that_already_ended_or_is_gone_answers_true(tmp_path: Path) -> None:
    """Docker's 304 (not running) and 404 (removed) mean nothing of the task runs any more."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    worker = threading.Thread(target=lambda: launcher.run(_run(), lambda line: None))
    worker.start()
    _until(lambda: _task_started(engine))
    real = engine.request

    for status in (304, 404):
        _intercept(engine, lambda method, path, body: path.endswith("/stop"), (status, b""))
        assert launcher.stop(StopRequest("t1", "w1")) == {"stopped": True}
        engine.request = real  # ty: ignore[invalid-assignment]
    launcher.stop(StopRequest("t1", "w1"))
    worker.join(10)


def test_a_closed_connection_removes_the_container_and_its_network(tmp_path: Path) -> None:
    """Review focus: a worker that dies sends nothing; the launcher still cleans up."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)

    def gone(line: dict[str, object]) -> None:
        raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        launcher.run(_run(), gone)
    assert engine.containers == {} and engine.networks == {}

    quiet = FakeEngine(hold=True)
    launcher = _started(quiet, tmp_path)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.run(_run(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(quiet))
    launcher.abandon(container_name(launcher.settings, "task", "t1"), "w1")
    worker.join(10)
    assert results[0]["stopped"] is True
    assert quiet.containers == {} and quiet.networks == {}
    assert any(p.endswith("/kill") for _, p, _ in quiet.calls)


def test_cleanup_spares_a_container_whose_connection_is_alive(tmp_path: Path) -> None:
    """Review focus: another replica's running task is not an orphan."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    worker = threading.Thread(target=lambda: launcher.run(_run(owner="w1"), lambda line: None))
    worker.start()
    _until(lambda: _task_started(engine))
    engine.containers["orphan"] = {
        "Name": "lakana-task-orphan",
        "exit": None,
        "spec": {"HostConfig": {"Mounts": []}},
        "Labels": {"lakana.launcher.stack": "lakana", "lakana.launcher.owner": "w1"},
    }
    engine._released["orphan"] = threading.Event()

    assert launcher.cleanup(CleanupRequest("w1")) == {"removed": 1}

    assert "orphan" not in engine.containers
    assert any(c["Name"] == "lakana-task-t1" for c in engine.containers.values())
    launcher.stop(StopRequest("t1", "w1"))
    worker.join(10)


def test_two_builds_of_one_environment_run_once(tmp_path: Path) -> None:
    """Review focus: the second request waits for the first and finds the environment built."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    results: list[dict[str, object]] = []
    threads = [
        threading.Thread(
            target=lambda b=b: results.append(launcher.build(_build(b), lambda line: None))
        )
        for b in ("b1", "b2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert [r["complete"] for r in results] == [True, True]
    assert len(engine.created("build")) == 1
    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": True}


def test_a_build_stopped_while_it_waits_for_another_build_ends_at_once(tmp_path: Path) -> None:
    """#291: a stop reaches a build queued behind a build of the same environment."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path, built=False)
    first = threading.Thread(target=lambda: launcher.build(_build("b1"), lambda line: None))
    first.start()
    _until(lambda: _task_started(engine))
    results: list[dict[str, object]] = []
    second = threading.Thread(
        target=lambda: results.append(launcher.build(_build("b2"), lambda line: None))
    )
    second.start()
    _until(lambda: _named(launcher, "build", "b2") in launcher._live)
    time.sleep(0.5)  # it keeps waiting while nobody stops it
    assert second.is_alive()

    assert launcher.stop(StopRequest("b2", "w1")) == {"stopped": True}
    second.join(5)

    assert not second.is_alive(), "the queued build waited for the other build to end"
    assert results[0]["stopped"] is True and results[0]["complete"] is False
    assert first.is_alive()
    launcher.stop(StopRequest("b1", "w1"))
    first.join(10)


def test_a_stopped_build_leaves_no_environment_and_no_cache(tmp_path: Path) -> None:
    """#291: what a stopped build wrote is removed; the next build starts clean."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path, built=False)
    volume = environment_volume(launcher.settings, "3.13", KEY)
    cache = cache_volume(launcher.settings, _build(), key=launcher._cache_key)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.build(_build(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))
    assert volume in engine.volumes and cache in engine.volumes

    assert launcher.stop(StopRequest("b1", "w1")) == {"stopped": True}
    worker.join(10)

    assert results[0]["complete"] is False
    assert volume not in engine.volumes and cache not in engine.volumes
    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": False}
    assert not launcher._marker(volume).exists()


def test_a_failed_build_leaves_no_marker_and_no_volume(tmp_path: Path) -> None:
    """Review focus: the next build of the same environment starts clean."""
    engine = FakeEngine(exit_code=1)
    launcher = _started(engine, tmp_path, built=False)
    volume = environment_volume(launcher.settings, "3.13", KEY)

    result = launcher.build(_build(), lambda line: None)

    assert result["complete"] is False and result["exit"] == 1
    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": False}
    deletes = [p for m, p, _ in engine.calls if m == "DELETE" and p == f"/volumes/{volume}"]
    assert len(deletes) == 2  # once before the build, once after it failed
    engine.exit_code = 0
    assert launcher.build(_build("b2"), lambda line: None)["complete"] is True


def test_old_environments_are_removed_beyond_the_ones_kept(tmp_path: Path) -> None:
    """With one kept, building a second removes the first's volume and marker."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, keep_environments=1, built=False)
    other = "b" * 64

    launcher.build(_build("b1", KEY), lambda line: None)
    time.sleep(0.01)
    launcher.build(_build("b2", other), lambda line: None)

    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": False}
    assert launcher.status(StatusRequest("3.13", other)) == {"complete": True}


def test_a_run_without_a_running_server_names_the_service(tmp_path: Path) -> None:
    """The worker reports this as "runner unavailable", with the text."""
    engine = FakeEngine(services={})
    launcher = _started(engine, tmp_path)

    with pytest.raises(EngineError, match="the server service of this stack is not running"):
        launcher.run(_run(), lambda line: None)
    assert engine.networks == {}


def test_a_missing_image_is_pulled_once(tmp_path: Path) -> None:
    """Only an allowed reference is ever pulled."""
    engine = FakeEngine(images=set())

    _started(engine, tmp_path).run(_run(), lambda line: None)

    pulls = [q for m, p, q in engine.calls if p == "/images/create"]
    assert pulls == [{"fromImage": "ghcr.io/lakana-labs/lakana-task:0.14.0-py3.13"}]


def _intercept(engine: FakeEngine, match: Any, reply: Any) -> None:
    """Answer the calls `match` selects with `reply` (raised when it is an exception).

    Parameters
    ----------
    engine
        The fake engine.
    match
        Takes method, path and body; says whether to intercept.
    reply
        The answer, or the exception to raise.
    """
    real = engine.request

    def request(
        method: str, path: str, body: Any = None, *, query: Any = None, timeout: Any = 60.0
    ) -> tuple[int, Any]:
        if match(method, path, body):
            if isinstance(reply, Exception):
                raise reply
            return reply
        return real(method, path, body, query=query, timeout=timeout)

    engine.request = request  # ty: ignore[invalid-assignment]


def _starts(engine: FakeEngine) -> int:
    """Count the containers started so far.

    Parameters
    ----------
    engine
        The fake engine.

    Returns
    -------
    int
        How many `/start` calls were made.
    """
    return sum(path.endswith("/start") for _, path, _ in engine.calls)


def test_start_refuses_an_engine_that_cannot_make_an_isolated_network(tmp_path: Path) -> None:
    """Without it a task network reaches the host's services through its gateway."""
    with pytest.raises(StartError, match="isolated network") as caught:
        _started(FakeEngine(isolated="refused"), tmp_path)
    assert "29.8.0" in str(caught.value) and "invalid option gateway_mode_ipv4" in str(caught.value)


def test_start_refuses_an_engine_that_drops_the_isolated_option(tmp_path: Path) -> None:
    """An engine that accepts the network but forgets the option is as bad as one that refuses."""
    engine = FakeEngine(isolated="ignored")

    with pytest.raises(StartError, match="did not keep gateway_mode_ipv4=isolated") as caught:
        _started(engine, tmp_path)

    assert "29.8.0" in str(caught.value)
    assert engine.networks == {}


def test_start_leaves_no_check_network_behind(tmp_path: Path) -> None:
    """The network made to prove the engine can isolate is removed again."""
    engine = FakeEngine()

    _started(engine, tmp_path)

    assert engine.networks == {}
    assert any(p == "/networks/create" for _, p, _ in engine.calls)


def _roots(tmp_path: Path, engine: FakeEngine) -> Launcher:
    """Start a launcher with one ingestion root, named `data`.

    Parameters
    ----------
    tmp_path
        The state directory.
    engine
        The fake engine.

    Returns
    -------
    Launcher
        The started launcher.
    """
    return _started(engine, tmp_path, ingestion_roots={"data": "/srv/data"})


def test_an_ingestion_root_is_probed_by_a_container_that_is_then_removed(tmp_path: Path) -> None:
    """The probe sees the root as a task would: read-only, non-recursive, no network."""
    engine = FakeEngine()

    _roots(tmp_path, engine)

    probe = engine.created("probe")[-1]
    assert probe["Labels"]["lakana.launcher.id"] == "ingest-data"
    assert probe["HostConfig"]["Mounts"] == [
        {
            "Type": "bind",
            "Source": "/srv/data",
            "Target": "/ingest/data",
            "ReadOnly": True,
            "BindOptions": {"Propagation": "rprivate", "NonRecursive": True},
        }
    ]
    assert probe["NetworkDisabled"] is True
    assert probe["Cmd"] == ["/ingest/data", "/proc/self/mountinfo", "100000"]
    assert engine.containers == {}


def _reason(tmp_path: Path, engine: FakeEngine) -> str:
    """Start with the root `data` and return why it was marked unusable.

    Parameters
    ----------
    tmp_path
        The state directory.
    engine
        The fake engine.

    Returns
    -------
    str
        The reason the launcher recorded for `data`.
    """
    return _roots(tmp_path, engine).unusable_roots["data"]


def test_a_root_with_a_socket_is_unusable_and_its_reason_never_names_its_path(
    tmp_path: Path,
) -> None:
    """A read-only mount does not stop a task from connecting to a socket under it."""
    reason = _reason(tmp_path, FakeEngine(probe_exits={"data": 4}))

    assert "ingestion root 'data' holds a socket, pipe or device node" in reason
    assert "/srv/data" not in reason and "INGESTION_ROOTS" in reason


def test_a_root_that_is_a_system_mount_is_unusable(tmp_path: Path) -> None:
    """A root behind a symlink to /proc, /sys or /dev would hand the host to every task."""
    reason = _reason(tmp_path, FakeEngine(probe_exits={"data": 3}))

    assert "proc, sysfs or devtmpfs" in reason and "ingestion root 'data'" in reason


def test_a_root_past_the_entry_cap_is_unusable_and_the_reason_names_the_setting(
    tmp_path: Path,
) -> None:
    """The cap is a setting, and the message says which."""
    reason = _reason(tmp_path, FakeEngine(probe_exits={"data": 5}))

    assert "ingestion root 'data' has more than 100000 entries" in reason
    assert "LAKANA_LAUNCHER__PROBE_MAX_ENTRIES" in reason


def test_the_probe_gets_the_entry_cap_from_the_settings(tmp_path: Path) -> None:
    """The container's script reads the cap as its third argument."""
    engine = FakeEngine()

    _started(engine, tmp_path, ingestion_roots={"data": "/srv/data"}, probe_max_entries=7)

    assert engine.created("probe")[-1]["Cmd"] == ["/ingest/data", "/proc/self/mountinfo", "7"]


def test_a_root_with_something_unreadable_is_unusable_and_the_reason_says_what_to_do(
    tmp_path: Path,
) -> None:
    """`lost+found` at the top of a filesystem is the usual cause."""
    reason = _reason(tmp_path, FakeEngine(probe_exits={"data": 6}))

    assert "ingestion root 'data' holds something the task user" in reason
    assert "lost+found" in reason and "subdirectory" in reason and "10001" in reason
    assert "/srv/data" not in reason


def test_a_root_that_could_not_be_checked_is_unusable(tmp_path: Path) -> None:
    """Any other probe result marks the root: an unchecked root is not a safe one."""
    reason = _reason(tmp_path, FakeEngine(probe_exits={"data": 1}))

    assert "ingestion root 'data' could not be checked" in reason


def test_the_launcher_starts_with_an_unusable_root_and_refuses_only_the_requests_naming_it(
    tmp_path: Path,
) -> None:
    """Every task that names no such root keeps working; the refusal carries the reason."""
    engine = FakeEngine(probe_exits={"data": 6})
    launcher = _roots(tmp_path, engine)

    with pytest.raises(Refused, match="ingestion root 'data' holds something the task user"):
        launcher.run(replace(_run(), roots=("data",)), lambda line: None)

    assert engine.created("task") == [] and launcher._live == {}
    assert launcher.run(_run(), lambda line: None)["exit"] == 0


def test_a_usable_root_still_works_beside_an_unusable_one(tmp_path: Path) -> None:
    """The marking is per root."""
    engine = FakeEngine(probe_exits={"bad": 4})
    launcher = _started(engine, tmp_path, ingestion_roots={"good": "/srv/good", "bad": "/srv/bad"})

    assert set(launcher.unusable_roots) == {"bad"}
    assert launcher.run(replace(_run(), roots=("good",)), lambda line: None)["exit"] == 0


def test_a_probe_docker_refuses_marks_the_root_unusable_with_dockers_reason(
    tmp_path: Path,
) -> None:
    """A root that does not exist on the host fails the probe's create; the reason says so."""
    engine = FakeEngine()
    _intercept(
        engine,
        lambda m, p, b: (
            p == "/containers/create" and b["Labels"]["lakana.launcher.id"] == "ingest-data"
        ),
        (400, {"message": "bind source path does not exist"}),
    )

    reason = _reason(tmp_path, engine)

    assert "cannot check ingestion root 'data'" in reason
    assert "bind source path does not exist" in reason


def test_a_probe_that_does_not_start_marks_the_root_unusable_and_is_removed(
    tmp_path: Path,
) -> None:
    """Docker may accept the create and refuse the start (a missing source, a bad mount)."""
    engine = FakeEngine()
    _intercept(
        engine,
        lambda m, p, b: p.endswith("/start") and "c2" in p,
        (400, {"message": "mount denied"}),
    )

    reason = _reason(tmp_path, engine)

    assert "cannot check ingestion root 'data'" in reason and "mount denied" in reason
    assert engine.containers == {}


def _probe(root: Path, mountinfo: Path, limit: int = 100000) -> int:
    """Run the ingestion probe's script on the host, as the container would run it.

    Parameters
    ----------
    root
        The directory to check.
    mountinfo
        The mountinfo file to read.
    limit
        The entry cap, as the launcher passes it.

    Returns
    -------
    int
        Its exit status.
    """
    return subprocess.run(
        [sys.executable, "-I", "-c", INGEST_PROBE_SCRIPT, str(root), str(mountinfo), str(limit)],
        check=False,
        capture_output=True,
    ).returncode


def test_the_probe_script_accepts_a_plain_directory_and_a_link_loop(tmp_path: Path) -> None:
    """Plain files, a symlink back to the root and a mount elsewhere are fine."""
    root = tmp_path / "data"
    (root / "a").mkdir(parents=True)
    (root / "a" / "link").symlink_to(root)
    (root / "x.sock").write_text("not a socket", encoding="utf-8")
    info = tmp_path / "info"
    info.write_text(
        "30 22 0:25 / /ingest/data rw - ext4 /dev/sda1 rw\n"
        "31 30 0:5 / /somewhere/else rw - proc proc rw\n"
        "garbage\n",
        encoding="utf-8",
    )

    assert _probe(root, info) == 0


def test_the_probe_script_refuses_a_system_mount_at_or_under_the_root(tmp_path: Path) -> None:
    """A proc, sysfs or devtmpfs mount exposes the host whatever the directory looks like."""
    root = tmp_path / "data"
    root.mkdir()
    for fstype in (
        "proc",
        "sysfs",
        "devtmpfs",
        "debugfs",
        "tracefs",
        "cgroup",
        "cgroup2",
        "bpf",
        "securityfs",
        "pstore",
        "configfs",
    ):
        info = tmp_path / "info"
        info.write_text(f"31 30 0:5 / {root}/x rw - {fstype} s rw\n", encoding="utf-8")
        assert _probe(root, info) == 3, fstype


def test_the_probe_script_finds_a_socket_below_the_root_or_as_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only mount does not stop `connect()`, so any socket refuses the root."""
    root = tmp_path / "data"
    (root / "a" / "b").mkdir(parents=True)
    info = tmp_path / "info"
    info.write_text("", encoding="utf-8")
    monkeypatch.chdir(root / "a" / "b")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("s.sock")
        assert _probe(root, info) == 4
        assert _probe(root / "a" / "b" / "s.sock", info) == 4


def test_the_probe_script_refuses_a_pipe_or_a_device_node(
    tmp_path: Path,
) -> None:
    """A world-writable FIFO is a way into a host process, whatever the mount says."""
    root = tmp_path / "data"
    (root / "a").mkdir(parents=True)
    info = tmp_path / "info"
    info.write_text("", encoding="utf-8")
    os.mkfifo(root / "a" / "pipe")

    assert _probe(root, info) == 4
    assert _probe(root / "a" / "pipe", info) == 4
    assert _probe(Path("/dev/null"), info) == 4


def test_the_probe_script_stops_walking_after_a_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree past the bound is refused, not passed: the part not seen could hold a socket."""
    root = tmp_path / "data"
    (root / "z").mkdir(parents=True)
    for number in range(6):
        (root / f"f{number}").write_text("", encoding="utf-8")
    info = tmp_path / "info"
    info.write_text("", encoding="utf-8")
    monkeypatch.chdir(root / "z")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind("s.sock")
        assert _probe(root, info) == 4
        assert _probe(root, info, limit=5) == 5


def test_the_probe_script_refuses_what_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that cannot be listed can still be walked through by name: it is refused."""
    root = tmp_path / "data"
    (root / "closed").mkdir(parents=True)
    info = tmp_path / "info"
    info.write_text("", encoding="utf-8")
    (root / "closed").chmod(0o000)
    try:
        if os.access(root / "closed", os.R_OK):  # running as root: nothing is unreadable
            pytest.skip("the user can read every directory")
        assert _probe(root, info) == 6
    finally:
        (root / "closed").chmod(0o700)
    assert _probe(tmp_path / "missing", info) == 6
    plain_file = tmp_path / "file"
    plain_file.write_text("x", encoding="utf-8")
    assert _probe(plain_file, info) == 0


def _named(launcher: Launcher, kind: str = "task", ident: str = "t1") -> str:
    """Name a container of the launcher.

    Parameters
    ----------
    launcher
        The launcher.
    kind
        ``task`` or ``build``.
    ident
        The id.

    Returns
    -------
    str
        The name.
    """
    return container_name(launcher.settings, kind, ident)


def _started_count(engine: FakeEngine) -> int:
    """Count the task or build containers started, leaving out the start-up probes.

    Parameters
    ----------
    engine
        The fake engine.

    Returns
    -------
    int
        How many.
    """
    return _starts(engine) - 1


def test_a_caller_that_left_before_the_run_registered_never_starts_it(tmp_path: Path) -> None:
    """The watcher can fire before the request reaches `run`; the name is remembered."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    launcher.abandon(_named(launcher), "w1")

    result = launcher.run(_run(), lambda line: None)

    assert result["stopped"] is True and result["exit"] is None
    assert engine.created("task") == [] and _started_count(engine) == 0
    assert launcher.stop(StopRequest("t1", "w1")) == {"stopped": False}
    assert launcher.run(_run(), lambda line: None)["stopped"] is False  # the name was spent


def test_a_caller_that_left_during_the_image_pull_never_starts_the_task(tmp_path: Path) -> None:
    """A pull takes minutes; a disconnect in it used to leave the task running to its timeout."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    engine.images.clear()
    engine.hooks["pull"] = lambda: launcher.abandon(_named(launcher), "w1")

    result = launcher.run(_run(), lambda line: None)

    assert result["stopped"] is True
    assert engine.created("task") == [] and _started_count(engine) == 0
    assert engine.networks == {}


@pytest.mark.parametrize(
    ("moment", "leave"),
    [
        ("created", "stop"),
        ("created", "abandon"),
        ("attach", "stop"),
        ("attach", "abandon"),
    ],
)
def test_a_stop_between_create_and_start_leaves_nothing_running(
    tmp_path: Path, moment: str, leave: str
) -> None:
    """The container exists but has not started: `stop` and `kill` on it do nothing in Docker."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)

    def leave_now() -> None:
        if leave == "stop":
            launcher.stop(StopRequest("t1", "w1"))
        else:
            launcher.abandon(_named(launcher), "w1")

    engine.hooks[moment] = leave_now

    result = launcher.run(_run(), lambda line: None)

    assert result["stopped"] is True and result["exit"] is None
    assert _started_count(engine) == 0
    assert engine.containers == {} and engine.networks == {}


def test_a_caller_that_left_while_docker_starts_the_container_kills_it(tmp_path: Path) -> None:
    """Leaving after the start call began: the kill that found nothing running is repeated."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    engine.hooks["start"] = lambda: launcher.abandon(_named(launcher), "w1")

    result = launcher.run(_run(), lambda line: None)

    assert result["stopped"] is True
    assert sum(p.endswith("/kill") for _, p, _ in engine.calls) >= 2
    assert engine.containers == {} and engine.networks == {}


def test_a_build_whose_caller_left_while_it_waited_does_not_build(tmp_path: Path) -> None:
    """A build queued behind one of the same environment checks again on its turn."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    launcher.abandon(_named(launcher, "build", "b1"), "w1")

    result = launcher.build(_build(), lambda line: None)

    assert result["stopped"] is True and result["complete"] is False
    assert engine.created("build") == []


def test_stop_reaches_a_build_by_its_id(tmp_path: Path) -> None:
    """`stop` takes a task's or a build's id."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path, built=False)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.build(_build(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))

    assert launcher.stop(StopRequest("b1", "w1")) == {"stopped": True}
    worker.join(10)

    assert results[0]["stopped"] is True and results[0]["complete"] is False


def test_a_run_needs_a_built_environment(tmp_path: Path) -> None:
    """Docker would create a missing volume empty, filled from the image's /env."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)

    with pytest.raises(EngineError, match=r"environment .* is not built"):
        launcher.run(_run(), lambda line: None)

    assert engine.created("task") == [] and engine.networks == {}


def test_a_run_needs_the_volume_of_a_built_environment(tmp_path: Path) -> None:
    """A marker outlives `docker volume prune`: the volume must exist too."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    engine.volumes.clear()

    with pytest.raises(EngineError, match="is not built"):
        launcher.run(_run(), lambda line: None)
    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": False}


def test_a_build_replaces_an_environment_whose_volume_is_gone(tmp_path: Path) -> None:
    """A stale marker is removed and the environment is built again."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    launcher._mark_complete(environment_volume(launcher.settings, "3.13", KEY))

    result = launcher.build(_build(), lambda line: None)

    assert result["complete"] is True and len(engine.created("build")) == 1


def test_a_build_fails_when_the_old_volume_cannot_be_removed(tmp_path: Path) -> None:
    """Building into an environment a container still uses would mix two builds."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    volume = environment_volume(launcher.settings, "3.13", KEY)
    engine.containers["user"] = {
        "Name": "other",
        "Labels": {},
        "exit": None,
        "spec": {"HostConfig": {"Mounts": [{"Source": volume, "Type": "volume"}]}},
    }

    with pytest.raises(EngineError, match="still in use"):
        launcher.build(_build(), lambda line: None)

    assert engine.created("build") == [] and engine.networks == {}


def test_a_build_fails_when_docker_cannot_be_asked_to_remove_the_volume(tmp_path: Path) -> None:
    """An unreachable engine is the same refusal, not a silent build into old contents."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    _intercept(
        engine, lambda m, p, b: m == "DELETE" and p.startswith("/volumes/"), EngineError("gone")
    )

    with pytest.raises(EngineError, match="still in use"):
        launcher.build(_build(), lambda line: None)


def test_eviction_keeps_a_marker_whose_volume_cannot_be_removed(tmp_path: Path) -> None:
    """A volume in use stays, with its marker, until a later build can remove it."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False, keep_environments=1)
    old = environment_volume(launcher.settings, "3.13", KEY)
    launcher.build(_build("b1", KEY), lambda line: None)
    time.sleep(0.01)
    engine.containers["user"] = {
        "Name": "other",
        "Labels": {},
        "exit": None,
        "spec": {"HostConfig": {"Mounts": [{"Source": old, "Type": "volume"}]}},
    }

    launcher.build(_build("b2", "b" * 64), lambda line: None)

    assert (tmp_path / "environments" / f"{old}.complete").exists()
    assert old in engine.volumes


def test_handle_dispatches_each_request_kind(tmp_path: Path) -> None:
    """The socket server hands every parsed request to `handle`."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)

    assert launcher.handle(_build(), lambda line: None)["complete"] is True
    assert launcher.handle(_run(), lambda line: None)["exit"] == 0
    assert launcher.handle(StopRequest("t1", "w1"), lambda line: None) == {"stopped": False}
    assert launcher.handle(StatusRequest("3.13", KEY), lambda line: None) == {"complete": True}
    assert launcher.handle(CleanupRequest("w1"), lambda line: None) == {"removed": 0}


def test_a_failed_pull_names_the_image_and_the_next_step(tmp_path: Path) -> None:
    """A host without registry access says what to do."""
    engine = FakeEngine(images=set(), pull_works=False)

    with pytest.raises(EngineError, match=r"pull the task image ghcr\.io/lakana-labs/lakana-task"):
        _started(engine, tmp_path)


@pytest.mark.parametrize(
    ("match", "reply", "text"),
    [
        (
            lambda m, p, b: (
                p == "/containers/create" and b["Labels"]["lakana.launcher.kind"] == "task"
            ),
            (409, {"message": "Conflict. The container name is in use"}),
            "Docker refused the container: Conflict",
        ),
        (
            lambda m, p, b: (
                p == "/networks/create" and b["Labels"]["lakana.launcher.kind"] == "task"
            ),
            (403, {"message": "network not allowed"}),
            "Docker refused the task network: network not allowed",
        ),
        (
            lambda m, p, b: p.endswith("/connect"),
            (500, {"message": "no such container"}),
            "Docker could not attach server: no such container",
        ),
        (
            lambda m, p, b: p.endswith("/start") and "c2" in p,
            (500, {"message": "oci runtime error"}),
            "Docker could not start the container: oci runtime error",
        ),
    ],
)
def test_a_refused_step_is_an_engine_error_that_leaves_nothing_behind(
    tmp_path: Path, match: Any, reply: Any, text: str
) -> None:
    """Docker's own message is passed on; the network and container are removed."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, match, reply)

    with pytest.raises(EngineError, match=text):
        launcher.run(_run(), lambda line: None)

    assert engine.containers == {} and engine.networks == {}


def test_a_body_without_a_message_still_gives_a_reason(tmp_path: Path) -> None:
    """Docker's answer is not always JSON."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: p == "/networks/create", (500, b"boom"))

    with pytest.raises(EngineError, match="no message"):
        launcher.run(_run(), lambda line: None)


def test_failures_while_removing_are_logged_and_never_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The result of a run does not depend on whether its leftovers could be removed."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: m == "DELETE", EngineError("socket closed"))

    result = launcher.run(_run(), lambda line: None)

    assert result["exit"] == 0
    assert "Could not remove container" in caplog.text and "Could not remove network" in caplog.text


def test_a_stop_and_a_kill_that_docker_cannot_carry_out_are_not_errors(tmp_path: Path) -> None:
    """`stop` still says it found the task; the timeout still ends the run."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    stops: list[str] = []
    real = engine.request

    def request(
        method: str, path: str, body: Any = None, *, query: Any = None, timeout: Any = 60.0
    ) -> tuple[int, Any]:
        if path.endswith("/stop"):
            stops.append(path)
            # The stop fails, so the held task is not ended by it; it ends a moment later by
            # itself, which a real container does, instead of the fake holding it for 10 s.
            engine.release(path.split("/")[-2])
            raise EngineError("gone")
        return real(method, path, body, query=query, timeout=timeout)

    engine.request = request  # ty: ignore[invalid-assignment]

    result = launcher.run(_run(timeout=1), lambda line: None)

    assert stops, "the timeout never asked Docker to stop the container"
    assert result["timed_out"] is True


def test_a_kill_that_docker_cannot_carry_out_does_not_fail_the_run(tmp_path: Path) -> None:
    """The container is removed by force at the end, whatever the kill answered."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: p.endswith("/kill"), EngineError("gone"))
    engine.hooks["attach"] = lambda: launcher.abandon(_named(launcher), "w1")

    assert launcher.run(_run(), lambda line: None)["stopped"] is True


def test_a_run_ends_even_when_the_task_exits_before_reading_its_input(tmp_path: Path) -> None:
    """A task that dies at once leaves a closed pipe; that is its exit status, not an error."""
    engine = FakeEngine(exit_code=1)
    launcher = _started(engine, tmp_path)

    def closed(container_id: str, *, stdin: bool) -> socket.socket:
        ours, theirs = socket.socketpair()
        theirs.close()
        return ours

    engine.attach = closed  # ty: ignore[invalid-assignment]

    assert launcher.run(_run(), lambda line: None)["exit"] == 1


class _Chunks:
    """A stream that hands out bytes the way a socket does.

    Parameters
    ----------
    data
        What to hand out.
    """

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    def recv(self, size: int) -> bytes:
        """Return up to `size` bytes.

        Parameters
        ----------
        size
            The most to return.

        Returns
        -------
        bytes
            The next bytes; empty at the end.
        """
        return self._data.read(size)


def test_read_frames_splits_a_huge_frame_and_stops_at_a_cut_stream() -> None:
    """The size in a frame header is Docker's, but no read waits for more than a megabyte."""
    big = b"x" * (3 * 1024 * 1024 + 5)
    header = struct.pack(">BxxxI", 1, len(big))

    frames = list(read_frames(_Chunks(header + big + struct.pack(">BxxxI", 1, 10) + b"cut")))

    assert [len(f) for f in frames] == [1 << 20, 1 << 20, 1 << 20, 5]
    assert b"".join(frames) == big
    assert list(read_frames(_Chunks(b"short"))) == []


def test_an_old_container_of_this_stack_is_removed_but_a_foreign_one_is_not(tmp_path: Path) -> None:
    """Only the stack label selects."""
    engine = FakeEngine()
    engine.containers["other"] = {
        "Name": "other-task-x",
        "exit": None,
        "spec": {"HostConfig": {"Mounts": []}},
        "Labels": {"lakana.launcher.stack": "other"},
    }
    engine._released["other"] = threading.Event()

    _started(engine, tmp_path)

    assert "other" in engine.containers


def test_a_network_that_is_already_gone_is_not_an_error(tmp_path: Path) -> None:
    """A removal that finds nothing still asks Docker to delete, and moves on."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)

    launcher.close_network("lakana-task-nothing")

    assert ("DELETE", "/networks/lakana-task-nothing", None) in engine.calls


def test_a_volume_that_cannot_be_asked_about_is_not_built(tmp_path: Path) -> None:
    """An unreachable engine does not count as a built environment."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: p.startswith("/volumes/"), EngineError("gone"))

    assert launcher.status(StatusRequest("3.13", KEY)) == {"complete": False}


def test_a_run_checks_the_environment_again_after_the_pull(tmp_path: Path) -> None:
    """A prune or an eviction during a pull must not let Docker create the volume empty."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    engine.images.clear()
    engine.hooks["pull"] = engine.volumes.clear

    with pytest.raises(EngineError, match="is not built"):
        launcher.run(_run(), lambda line: None)

    assert engine.created("task") == [] and engine.networks == {}


def test_a_second_run_with_a_live_id_is_refused_and_leaves_the_first_alone(tmp_path: Path) -> None:
    """The registry is keyed by name; a duplicate must not unregister the run that owns it."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.run(_run(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))

    with pytest.raises(EngineError, match="a run with the id t1 is already live"):
        launcher.run(_run(), lambda line: None)

    assert launcher.stop(StopRequest("t1", "w1")) == {"stopped": True}
    worker.join(10)
    assert results[0]["stopped"] is True


def test_cleanup_lists_the_containers_before_it_looks_at_the_live_runs(tmp_path: Path) -> None:
    """A run that registers after the listing is spared; one that registered before is too."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    engine.containers["late"] = {
        "Name": "lakana-task-late",
        "exit": None,
        "spec": {"HostConfig": {"Mounts": []}},
        "Labels": {"lakana.launcher.stack": "lakana", "lakana.launcher.owner": "w1"},
    }
    engine._released["late"] = threading.Event()
    real = engine.request

    def listing_then_register(
        method: str, path: str, body: Any = None, *, query: Any = None, timeout: Any = 60.0
    ) -> tuple[int, Any]:
        answer = real(method, path, body, query=query, timeout=timeout)
        if path == "/containers/json":
            launcher._register("lakana-task-late", "w1", 3, 0)
        return answer

    engine.request = listing_then_register  # ty: ignore[invalid-assignment]

    assert launcher.cleanup(CleanupRequest("w1")) == {"removed": 0}
    assert "late" in engine.containers


def test_a_host_path_in_dockers_message_is_replaced_by_the_root_name(tmp_path: Path) -> None:
    """The worker and its task log never learn where an ingestion root lives on the host."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, ingestion_roots={"data": "/srv/data"})
    _intercept(
        engine,
        lambda m, p, b: p == "/containers/create" and b["Labels"]["lakana.launcher.kind"] == "task",
        (400, {"message": "invalid mount config: bind source path does not exist: /srv/data"}),
    )

    with pytest.raises(EngineError) as caught:
        launcher.run(replace(_run(), roots=("data",)), lambda line: None)

    assert "/srv/data" not in str(caught.value) and "ingestion root 'data'" in str(caught.value)
    assert "/srv/data" not in launcher.redact("failed at /srv/data/sub")
    assert "ingestion root 'data'/sub" in launcher.redact("failed at /srv/data/sub")


def test_a_probe_failure_never_names_a_host_path(tmp_path: Path) -> None:
    """Docker's text reaches the start-up error, so it is redacted there too."""
    engine = FakeEngine()
    _intercept(
        engine,
        lambda m, p, b: (
            p == "/containers/create" and b["Labels"]["lakana.launcher.id"] == "ingest-data"
        ),
        (400, {"message": "bind source path does not exist: /srv/data"}),
    )

    assert "/srv/data" not in _reason(tmp_path, engine)


def test_any_other_answer_to_the_version_request_is_not_called_an_api_problem(
    tmp_path: Path,
) -> None:
    """A 500, or something that is not Docker, has another cause and another next step."""
    engine = FakeEngine()
    _intercept(engine, lambda m, p, b: p == "/version", (500, {"message": "daemon is starting"}))

    with pytest.raises(StartError, match="answered status 500") as caught:
        _started(engine, tmp_path)

    assert "daemon is starting" in str(caught.value)
    assert "API version" not in str(caught.value)


def test_redaction_replaces_whole_path_components_only(tmp_path: Path) -> None:
    """`/srv` is a root; `/srv2` and `/x/srv` are other places."""
    launcher = _started(
        FakeEngine(), tmp_path, ingestion_roots={"data": "/srv", "other": "/srv/other"}
    )

    redacted = launcher.redact("at /srv2/a, /x/srv/b, /srv/other/c, /srv: and /srv.")

    assert redacted == (
        "at /srv2/a, /x/srv/b, ingestion root 'other'/c, ingestion root 'data': and "
        "ingestion root 'data'."
    )


def test_an_engine_that_rejects_the_api_version_says_so_with_both_versions(tmp_path: Path) -> None:
    """A future engine may refuse /v1.47; the next step is to upgrade Lakana, not Docker."""
    engine = FakeEngine()
    _intercept(
        engine,
        lambda m, p, b: p == "/version",
        (400, {"message": "client version 1.47 is too old. Minimum supported API version is 1.52"}),
    )

    with pytest.raises(StartError, match=r"API version 1\.47 is not supported") as caught:
        _started(engine, tmp_path)

    assert "Minimum supported API version is 1.52" in str(caught.value)
    assert "upgrade Lakana" in str(caught.value)


def test_a_marker_that_disappears_during_eviction_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent eviction of another key may unlink a marker between the listing and its stat."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, keep_environments=1)
    ghost = tmp_path / "environments" / "lakana-env-ghost.complete"
    real = Path.glob
    monkeypatch.setattr(Path, "glob", lambda self, pattern: iter([ghost, *real(self, pattern)]))

    launcher._evict(keep="lakana-env-other")

    assert not ghost.exists()


def test_a_task_that_writes_before_it_reads_its_input_does_not_block_the_launcher(
    tmp_path: Path,
) -> None:
    """Input is sent while output is read, so a full pipe in one direction cannot stop the other."""
    engine = FakeEngine(write_first=True, output=[b"x" * 60000] * 100)
    launcher = _started(engine, tmp_path)
    emitted: list[dict[str, object]] = []
    big = replace(_run(), stdin=b"y" * (1 << 20))
    results: list[dict[str, object]] = []
    worker = threading.Thread(target=lambda: results.append(launcher.run(big, emitted.append)))

    worker.start()
    worker.join(15)

    assert not worker.is_alive(), "the launcher is stuck writing input while the task writes output"
    assert len(_logs(emitted)) == 60000 * 100
    assert engine.stdin == [b"y" * (1 << 20)]


def _volume_creations(engine: FakeEngine) -> dict[str, dict[str, str]]:
    """Collect the labels of every volume created explicitly.

    Parameters
    ----------
    engine
        The fake engine.

    Returns
    -------
    dict
        Volume name to labels.
    """
    return {b["Name"]: b["Labels"] for m, p, b in engine.calls if p == "/volumes/create"}


def test_a_build_creates_its_volumes_with_labels_and_a_source_build_gets_a_private_cache(
    tmp_path: Path,
) -> None:
    """Environment and cache volumes are found by label; the private cache goes with the build."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    environment = environment_volume(launcher.settings, "3.13", KEY)

    launcher.build(_build(), lambda line: None)

    created = _volume_creations(engine)
    cache = engine.created("build")[0]["HostConfig"]["Mounts"][2]["Source"]
    assert "uvcache-private-" in cache
    assert created[environment]["lakana.launcher.kind"] == "env"
    assert {k: v for k, v in created[cache].items() if k != "lakana.launcher.deadline"} == {
        "lakana.launcher.stack": "lakana",
        "lakana.launcher.kind": "cache",
        "lakana.launcher.shared": "0",
    }
    assert "lakana.launcher.deadline" in created[cache]
    assert cache not in engine.volumes and environment in engine.volumes


def test_a_failed_network_still_removes_the_private_cache_the_build_created(
    tmp_path: Path,
) -> None:
    """The volumes and the network are made inside the cleanup's reach."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    _intercept(
        engine,
        lambda m, p, b: p == "/networks/create" and "build" in b["Name"],
        (500, {"message": "no address"}),
    )

    with pytest.raises(EngineError, match="no address"):
        launcher.build(_build(), lambda line: None)

    assert not [v for v in engine.volumes if "uvcache-private" in v]
    assert launcher._subnets == {}


def test_a_private_cache_left_by_a_dead_launcher_is_removed_before_the_build_makes_its_own(
    tmp_path: Path,
) -> None:
    """The same environment's leftover must not seed the next build."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    request = _build()
    cache = cache_volume(launcher.settings, request, key=launcher._cache_key)
    engine.volumes.add(cache)

    launcher.build(request, lambda line: None)

    events = [
        "delete" if m == "DELETE" and p == f"/volumes/{cache}" else "create"
        for m, p, b in engine.calls
        if (m == "DELETE" and p == f"/volumes/{cache}")
        or (p == "/volumes/create" and b["Name"] == cache)
    ]
    assert events[:2] == ["delete", "create"]


@pytest.mark.parametrize("reply", [(409, {"message": "in use"}), EngineError("engine down")])
def test_a_leftover_private_cache_that_cannot_be_removed_refuses_the_build(
    tmp_path: Path, reply: Any
) -> None:
    """A private cache is never reused: if the old one stays, nothing is created or mounted."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    request = _build()
    cache = cache_volume(launcher.settings, request, key=launcher._cache_key)
    engine.volumes.add(cache)
    _intercept(engine, lambda m, p, b: m == "DELETE" and p == f"/volumes/{cache}", reply)

    with pytest.raises(EngineError, match=r"private cache.*docker compose logs launcher"):
        launcher.build(request, lambda line: None)

    assert engine.created("build") == []
    assert not [c for c in engine.calls if c[1] == "/volumes/create"]
    assert launcher._subnets == {}


def test_the_cache_key_is_unusable_before_start(tmp_path: Path) -> None:
    """An unstarted launcher must fail loudly, never HMAC with an empty key."""
    launcher = Launcher(settings(tmp_path), FakeEngine())

    with pytest.raises(RuntimeError, match=r"start\(\)"):
        _ = launcher._cache_key


def test_a_failed_write_of_the_cache_key_leaves_no_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key file appears whole or not at all; a crash cannot leave an empty one."""

    def fail(descriptor: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail)

    with pytest.raises(OSError, match="disk full"):
        _started(FakeEngine(), tmp_path)

    assert not (tmp_path / "cache.key").exists()
    assert not list(tmp_path.glob("cache.key*"))


def test_two_launchers_starting_together_agree_on_the_key_that_won(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loser of the race to create `cache.key` reads the winner's key, never replaces it."""
    winner = b"w" * 32

    def lose(source: Any, target: Any) -> None:
        Path(target).write_bytes(winner)
        Path(target).chmod(0o600)
        raise FileExistsError(target)

    monkeypatch.setattr(os, "link", lose)

    launcher = _started(FakeEngine(), tmp_path)

    assert launcher._cache_key == winner
    assert not list(tmp_path.glob("cache.key.*"))


def test_a_symlinked_cache_key_is_refused(tmp_path: Path) -> None:
    """A link could point the key at a file someone else controls."""
    target = tmp_path / "elsewhere"
    target.write_bytes(b"k" * 32)
    target.chmod(0o600)
    (tmp_path / "cache.key").symlink_to(target)

    with pytest.raises(StartError, match=r"cache\.key.*symbolic link.*delete it and restart"):
        _started(FakeEngine(), tmp_path)


def test_a_cache_key_that_is_a_directory_is_refused(tmp_path: Path) -> None:
    """Only a regular file can hold the key."""
    (tmp_path / "cache.key").mkdir()

    with pytest.raises(StartError, match=r"cache\.key.*regular file"):
        _started(FakeEngine(), tmp_path)


def test_a_cache_key_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planted key is known to whoever planted it."""
    _started(FakeEngine(), tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(tmp_path / "cache.key").st_uid + 1)

    with pytest.raises(StartError, match=r"cache\.key.*owned by"):
        _started(FakeEngine(), tmp_path)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660])
def test_a_cache_key_others_can_read_or_write_is_refused(tmp_path: Path, mode: int) -> None:
    """Group or world access means the key may be known."""
    _started(FakeEngine(), tmp_path)
    (tmp_path / "cache.key").chmod(mode)

    with pytest.raises(StartError, match=r"cache\.key.*accessible"):
        _started(FakeEngine(), tmp_path)


def test_an_empty_cache_key_file_is_refused_with_the_fix(tmp_path: Path) -> None:
    """Fail closed: an empty file is never used, and the message names the way out."""
    key_file = tmp_path / "cache.key"
    key_file.write_bytes(b"")
    key_file.chmod(0o600)

    with pytest.raises(StartError, match=r"cache\.key.*delete it and restart"):
        _started(FakeEngine(), tmp_path)


def test_a_fifo_at_the_cache_key_is_refused_without_hanging_the_start(tmp_path: Path) -> None:
    """A FIFO planted as `cache.key` must not block the open until a writer appears."""
    os.mkfifo(tmp_path / "cache.key", 0o600)
    errors: list[BaseException] = []

    def start() -> None:
        """Start a launcher, keeping what it raised."""
        try:
            _started(FakeEngine(), tmp_path)
        except BaseException as exc:  # handed to the test thread below
            errors.append(exc)

    thread = threading.Thread(target=start, daemon=True)
    thread.start()
    thread.join(10)
    hung = thread.is_alive()
    if hung:  # release the blocked open so the thread ends
        with open(tmp_path / "cache.key", "wb"):
            pass
        thread.join(10)

    assert not hung
    assert len(errors) == 1 and isinstance(errors[0], StartError)
    assert "regular file" in str(errors[0])


def test_the_state_directory_is_synced_after_the_key_is_linked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link is durable only once its directory is: a crash must not lose the key's name."""
    synced: list[bool] = []
    real = os.fsync

    def record(descriptor: int) -> None:
        """Note whether a directory or a file is synced, then sync it.

        Parameters
        ----------
        descriptor
            The descriptor synced.
        """
        synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        real(descriptor)

    monkeypatch.setattr(os, "fsync", record)

    _started(FakeEngine(), tmp_path)

    assert synced[:2] == [
        False,
        True,
    ]  # the cache key's file, then its directory; the egress key follows


def test_stale_temporary_key_files_are_swept_at_start(tmp_path: Path) -> None:
    """A launcher killed before linking leaves a temporary file; a later start removes it."""
    stale = tmp_path / "cache.key.0123456789abcdef.tmp"
    fresh = tmp_path / "cache.key.fedcba9876543210.tmp"
    other = tmp_path / "cache.key.notours.tmp"
    for path in (stale, fresh, other):
        path.write_bytes(b"k" * 32)
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    os.utime(other, (time.time() - 3600, time.time() - 3600))

    _started(FakeEngine(), tmp_path)

    assert not stale.exists()
    assert fresh.exists(), "a recent one may belong to a launcher starting next to this one"
    assert other.exists(), "only the launcher's own names are swept"


@pytest.mark.parametrize(
    ("code", "text"),
    [
        (errno.ELOOP, "is a symbolic link"),
        (errno.EACCES, "cannot be read by the launcher's user (permission denied)"),
        (errno.EIO, "cannot be opened (Input/output error)"),
    ],
)
def test_an_unopenable_cache_key_names_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, text: str
) -> None:
    """The message says what is wrong with the file, not always "a symbolic link".

    Parameters
    ----------
    tmp_path
        The state directory.
    monkeypatch
        Makes the open fail.
    code
        The errno of the failure.
    text
        What the message says.
    """
    real = os.open

    def fail(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        """Fail the open of `cache.key` only.

        Parameters
        ----------
        path
            The path opened.
        flags
            Its flags.
        *args
            The mode, if any.
        **kwargs
            Keyword arguments of `os.open`.

        Returns
        -------
        int
            The descriptor of any other file.

        Raises
        ------
        OSError
            For `cache.key`.
        """
        if Path(path).name == "cache.key":
            raise OSError(code, os.strerror(code))
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail)

    with pytest.raises(StartError) as caught:
        _started(FakeEngine(), tmp_path)

    assert text in str(caught.value) and "delete it and restart" in str(caught.value)


def test_the_reaper_removes_private_caches_past_their_deadline(tmp_path: Path) -> None:
    """A launcher that died mid-build leaves a private cache; its label says when it is stale."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    for name, deadline in (
        ("lakana-uvcache-private-old", 10),
        ("lakana-uvcache-private-new", 10**12),
    ):
        engine.volumes.add(name)
        engine.volume_labels[name] = {
            "lakana.launcher.stack": "lakana",
            "lakana.launcher.kind": "cache",
            "lakana.launcher.shared": "0",
            "lakana.launcher.deadline": str(deadline),
        }

    launcher.reap()

    assert "lakana-uvcache-private-old" not in engine.volumes
    assert "lakana-uvcache-private-new" in engine.volumes


def test_a_private_cache_carries_a_deadline_and_a_shared_one_does_not(tmp_path: Path) -> None:
    """Only a cache that dies with its build has a deadline."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    launcher.build(_build(), lambda line: None)
    launcher.build(replace(_build("b2", "b" * 64), no_build=True), lambda line: None)

    labels = [b["Labels"] for m, p, b in engine.calls if p == "/volumes/create"]
    caches = [item for item in labels if item["lakana.launcher.kind"] == "cache"]
    assert "lakana.launcher.deadline" in caches[0]
    assert "lakana.launcher.deadline" not in caches[1]


def test_the_cache_key_is_made_once_kept_private_and_reused(tmp_path: Path) -> None:
    """Different tokens must never share a cache; the key never leaves the state volume."""
    first = _started(FakeEngine(), tmp_path)
    second = _started(FakeEngine(), tmp_path)

    key_file = tmp_path / "cache.key"
    assert len(first._cache_key) == 32 and first._cache_key == second._cache_key
    assert key_file.read_bytes() == first._cache_key
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_a_cache_key_file_of_the_wrong_size_is_refused(tmp_path: Path) -> None:
    """A truncated or planted file would weaken every cache name."""
    (tmp_path / "cache.key").write_bytes(b"short")
    (tmp_path / "cache.key").chmod(0o600)

    with pytest.raises(StartError, match=r"cache\.key"):
        _started(FakeEngine(), tmp_path)


def test_a_wheels_only_build_keeps_the_shared_cache_it_mounted(tmp_path: Path) -> None:
    """The shared cache is labelled shared and outlives the build."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)

    launcher.build(replace(_build(), no_build=True), lambda line: None)

    cache = engine.created("build")[0]["HostConfig"]["Mounts"][2]["Source"]
    assert "private" not in cache
    assert _volume_creations(engine)[cache]["lakana.launcher.shared"] == "1"
    assert cache in engine.volumes


def test_a_failed_source_build_removes_its_private_cache_too(tmp_path: Path) -> None:
    """Nothing of a failed build outlives it."""
    engine = FakeEngine(exit_code=1)
    launcher = _started(engine, tmp_path, built=False)

    launcher.build(_build(), lambda line: None)

    assert not [v for v in engine.volumes if "uvcache-private" in v]


def test_every_container_is_removed_with_its_anonymous_volumes(tmp_path: Path) -> None:
    """An image `VOLUME` makes an anonymous volume that survives a removal without `v=1`."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, ingestion_roots={"data": "/srv/data"})

    launcher.run(_run(), lambda line: None)

    removals = [q for m, p, q in engine.calls if m == "DELETE" and p.startswith("/containers/")]
    assert removals and all(q == {"force": "1", "v": "1"} for q in removals)


def test_every_container_and_network_gets_a_deadline_after_its_timeout(tmp_path: Path) -> None:
    """The reaper needs a clock that does not depend on the launcher's memory."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    before = time.time()

    launcher.run(_run(timeout=1), lambda line: None)

    label = "lakana.launcher.deadline"
    deadline = int(engine.created("task")[0]["Labels"][label])
    assert before + 1 + 3 <= deadline <= time.time() + 1 + 3 + 121
    network = next(b for m, p, b in engine.calls if p == "/networks/create" and "task" in b["Name"])
    assert network["Labels"][label] == str(deadline)


def _plant(
    engine: FakeEngine, name: str, deadline: float | None, *, cid: str | None = None
) -> None:
    """Add a labelled container and network, as an earlier launcher run would have left.

    Parameters
    ----------
    engine
        The fake engine.
    name
        Their name.
    deadline
        The deadline label, or `None` for none.
    cid
        The container id (the name by default).
    """
    labels = {"lakana.launcher.stack": "lakana"}
    if deadline is not None:
        labels["lakana.launcher.deadline"] = str(int(deadline))
    ident = cid or name
    engine.containers[ident] = {
        "Name": name,
        "exit": None,
        "spec": {"HostConfig": {"Mounts": []}},
        "Labels": labels,
    }
    engine._released[ident] = threading.Event()
    engine.networks[name] = {"Labels": labels, "Containers": {}}


def test_the_reaper_removes_what_is_past_its_deadline_and_spares_the_rest(tmp_path: Path) -> None:
    """Including a container nobody supervises, as after a launcher restart."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    now = time.time()
    _plant(engine, "lakana-task-old", now - 10)
    _plant(engine, "lakana-task-new", now + 600)
    _plant(engine, "lakana-task-none", None)

    removed = launcher.reap()

    assert removed == 1
    assert set(engine.containers) == {"lakana-task-new", "lakana-task-none"}
    assert set(engine.networks) == {"lakana-task-new", "lakana-task-none"}


def test_the_reaper_stops_a_live_run_that_outlived_its_deadline(tmp_path: Path) -> None:
    """A stuck run is marked stopped, so its connection ends with the container."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.run(_run(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))

    launcher.reap(now=time.time() + 100000)
    worker.join(10)

    assert results[0]["stopped"] is True and engine.containers == {}


def test_the_reaper_survives_an_engine_that_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad listing is logged; the next pass tries again."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: p == "/containers/json", (500, {"message": "busy"}))

    assert launcher.reap() == 0
    assert "Could not list" in caplog.text


def test_the_reaper_thread_removes_an_expired_container_and_stops_on_request(
    tmp_path: Path,
) -> None:
    """The loop that runs beside the server."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _plant(engine, "lakana-task-old", time.time() - 10)

    thread = launcher.start_reaper(interval=0.05)
    _until(lambda: "lakana-task-old" not in engine.containers)
    launcher.stop_reaper()
    thread.join(5)

    assert not thread.is_alive()


def test_an_error_inside_a_reaper_pass_does_not_end_the_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unexpected exception is logged and the loop goes on."""
    launcher = _started(FakeEngine(), tmp_path)
    calls: list[int] = []

    def reap(now: float | None = None) -> int:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(launcher, "reap", reap)

    thread = launcher.start_reaper(interval=0.02)
    _until(lambda: len(calls) >= 2)
    launcher.stop_reaper()
    thread.join(5)

    assert "The reaper failed" in caplog.text


def test_the_reaper_removes_old_shared_caches_and_keeps_recent_and_used_ones(
    tmp_path: Path,
) -> None:
    """A simple age limit; a cache in use answers 409 and stays."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    cache = {
        "lakana.launcher.stack": "lakana",
        "lakana.launcher.kind": "cache",
        "lakana.launcher.shared": "1",
    }
    for name, created in (
        ("lakana-uvcache-old", "2020-01-01T00:00:00Z"),
        ("lakana-uvcache-new", "2999-01-01T00:00:00+00:00"),
        ("lakana-uvcache-busy", "2020-01-01T00:00:00Z"),
        ("lakana-uvcache-odd", "yesterday"),
    ):
        engine.volumes.add(name)
        engine.volume_labels[name] = cache
        engine.volume_created[name] = created
    engine.containers["user"] = {
        "Name": "other",
        "Labels": {},
        "exit": None,
        "spec": {"HostConfig": {"Mounts": [{"Source": "lakana-uvcache-busy", "Type": "volume"}]}},
    }

    launcher.reap()

    caches = {name for name in engine.volumes if name.startswith("lakana-uvcache")}
    assert caches == {"lakana-uvcache-new", "lakana-uvcache-busy", "lakana-uvcache-odd"}


def test_a_deadline_label_that_is_not_a_number_is_ignored(tmp_path: Path) -> None:
    """Someone else's label on a stack-labelled object must not crash the reaper."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _plant(engine, "lakana-task-odd", None)
    engine.containers["lakana-task-odd"]["Labels"]["lakana.launcher.deadline"] = "soon"

    assert launcher.reap() == 0
    assert "lakana-task-odd" in engine.containers


def test_a_reaper_pass_goes_on_when_the_network_or_cache_listing_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Each listing is tried on its own."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: p == "/networks", (500, {"message": "busy"}))
    _intercept(engine, lambda m, p, b: p == "/volumes", EngineError("gone"))

    launcher.reap()

    assert "Could not list networks" in caplog.text and "Could not list caches" in caplog.text


def test_start_refuses_a_docker_socket_anybody_can_write_to(tmp_path: Path) -> None:
    """A world-writable socket is root on the host for every user of the machine."""
    docker = tmp_path / "docker.sock"
    docker.write_text("", encoding="utf-8")
    docker.chmod(0o666)

    with pytest.raises(StartError, match="writable by everyone") as caught:
        _started(FakeEngine(), tmp_path, docker_socket=docker)

    assert "chmod" in str(caught.value) or "group" in str(caught.value)


def test_a_missing_docker_socket_file_is_left_to_the_engine_check(tmp_path: Path) -> None:
    """The group check needs a socket to look at; without one the engine check speaks."""
    _started(FakeEngine(), tmp_path, docker_socket=tmp_path / "missing.sock")


def test_a_recovered_network_with_an_unreadable_subnet_is_skipped(tmp_path: Path) -> None:
    """Only addresses inside the pool are taken as in use."""
    engine = FakeEngine()
    _plant(engine, "lakana-task-odd", None)
    engine.networks["lakana-task-odd"]["spec"] = {"IPAM": {"Config": [{"Subnet": "not-a-subnet"}]}}
    _intercept(
        engine,
        lambda m, p, b: m == "DELETE" and p.startswith("/networks/"),
        (500, {"message": "x"}),
    )

    launcher = _started(engine, tmp_path)

    assert launcher._subnets == {}


def test_a_build_fails_when_docker_refuses_its_volume(tmp_path: Path) -> None:
    """The volumes are created with labels first; a refusal stops the build."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    _intercept(engine, lambda m, p, b: p == "/volumes/create", (500, {"message": "no space"}))

    with pytest.raises(EngineError, match=r"Docker refused the volume .*: no space"):
        launcher.build(_build(), lambda line: None)

    assert engine.created("build") == []


def test_a_shutdown_removes_the_containers_and_networks_of_live_runs(tmp_path: Path) -> None:
    """SIGTERM must not leave a task running with nobody to time it out."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.run(_run(), lambda line: None))
    )
    worker.start()
    _until(lambda: _task_started(engine))

    launcher.shutdown()
    worker.join(10)

    assert results[0]["stopped"] is True
    assert engine.containers == {} and engine.networks == {}


def test_a_shutdown_survives_an_engine_that_cannot_be_asked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The process still ends; the reaper of the next launcher does the rest."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(engine, lambda m, p, b: p == "/containers/json", EngineError("gone"))

    launcher.shutdown()

    assert "Could not remove everything" in caplog.text


def test_start_refuses_when_it_cannot_list_what_an_earlier_launcher_left(tmp_path: Path) -> None:
    """A failed listing used to remove nothing, silently, and leave tasks running."""
    engine = FakeEngine()
    _intercept(engine, lambda m, p, b: p == "/containers/json", (500, {"message": "daemon busy"}))

    with pytest.raises(StartError, match="cannot list the containers") as caught:
        _started(engine, tmp_path)

    assert "daemon busy" in str(caught.value)


def test_a_task_network_gets_a_slash_29_from_the_range_and_gives_it_back(tmp_path: Path) -> None:
    """The host's address pools are not touched, and the same subnet is used again."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)

    launcher.run(_run(), lambda line: None)
    launcher.run(_run("t2"), lambda line: None)

    subnets = [
        b["IPAM"]["Config"][0]["Subnet"]
        for m, p, b in engine.calls
        if p == "/networks/create" and "IPAM" in b
    ]
    assert subnets == ["10.207.0.0/29", "10.207.0.0/29"]
    assert launcher._subnets == {} and launcher._used_subnets == set()


def test_concurrent_networks_get_different_subnets(tmp_path: Path) -> None:
    """Allocation happens under the lock."""
    launcher = _started(FakeEngine(), tmp_path)

    first = launcher._open_network("task", "t1", "w1", [], 0)
    second = launcher._open_network("build", "b1", "w1", [], 0)

    assert launcher._subnets[first] == "10.207.0.0/29"
    assert launcher._subnets[second] == "10.207.0.8/29"


def test_subnets_of_networks_that_survive_the_start_are_recovered(tmp_path: Path) -> None:
    """A network that could not be removed keeps its subnet; no new network may take it."""
    engine = FakeEngine()
    _plant(engine, "lakana-task-stuck", None)
    engine.networks["lakana-task-stuck"]["spec"] = {
        "IPAM": {"Config": [{"Subnet": "10.207.0.0/29"}, {"Subnet": "192.168.1.0/29"}]}
    }
    _intercept(
        engine,
        lambda m, p, b: m == "DELETE" and p.startswith("/networks/"),
        (500, {"message": "x"}),
    )

    launcher = _started(engine, tmp_path)

    assert launcher._subnets == {"lakana-task-stuck": "10.207.0.0/29"}
    assert launcher._open_network("task", "t1", "w1", [], 0) and (
        launcher._subnets["lakana-task-t1"] == "10.207.0.8/29"
    )


def test_a_subnet_docker_says_overlaps_is_skipped(tmp_path: Path) -> None:
    """Something else on the host may hold an address inside the range."""
    engine = FakeEngine(overlaps={"10.207.0.0/29"})
    launcher = _started(engine, tmp_path)

    name = launcher._open_network("task", "t1", "w1", [], 0)

    assert launcher._subnets[name] == "10.207.0.8/29"
    assert "10.207.0.0/29" in launcher._used_subnets


def test_a_range_full_of_overlaps_gives_up_with_the_setting_named(tmp_path: Path) -> None:
    """After a few tries the message says what to change."""
    engine = FakeEngine(overlaps={f"10.207.0.{8 * n}/29" for n in range(8)})
    launcher = _started(engine, tmp_path)

    with pytest.raises(EngineError, match="LAKANA_LAUNCHER__NETWORK_RANGE"):
        launcher._open_network("task", "t1", "w1", [], 0)

    assert launcher._subnets == {}


def test_runs_beyond_the_concurrency_bound_are_refused_naming_the_setting(tmp_path: Path) -> None:
    """A /24 holds 32 networks; the bound is below that, so a burst cannot use them all."""
    launcher = _started(FakeEngine(), tmp_path, network_range="10.207.0.0/24")
    for number in range(24):
        launcher._open_network("task", f"t{number}", "w1", [], 0)

    with pytest.raises(EngineError, match="LAKANA_LAUNCHER__NETWORK_RANGE") as caught:
        launcher._open_network("task", "t99", "w1", [], 0)

    assert "24 of 24" in str(caught.value)


def test_an_exhausted_pool_is_an_error_naming_the_setting(tmp_path: Path) -> None:
    """Subnets that overlaps took out of use can exhaust the pool below the bound."""
    launcher = _started(FakeEngine(), tmp_path, network_range="10.207.0.0/24")
    launcher._used_subnets.update(f"10.207.0.{8 * n}/29" for n in range(32))

    with pytest.raises(EngineError, match="no free address range"):
        launcher._open_network("task", "t1", "w1", [], 0)


def test_a_network_that_cannot_be_deleted_keeps_its_subnet(tmp_path: Path) -> None:
    """Only a removed network gives its addresses back."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    name = launcher._open_network("task", "t1", "w1", [], 0)
    _intercept(engine, lambda m, p, b: m == "DELETE", (500, {"message": "busy"}))

    launcher.close_network(name)

    assert launcher._subnets == {name: "10.207.0.0/29"}


def test_no_new_privileges_auto_turns_off_only_when_a_control_container_starts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A probe that fails for another reason must not silently drop the option."""
    engine = FakeEngine(probe_exits={"no-new-privileges": 1})

    launcher = _started(engine, tmp_path)

    assert launcher.no_new_privileges is False
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_start_refuses_when_not_even_the_control_container_runs(tmp_path: Path) -> None:
    """The image or the host is broken, not the option: do not start with a guess."""
    engine = FakeEngine(probe_exits={"no-new-privileges": 1, "no-new-privileges-control": 1})

    with pytest.raises(StartError, match="cannot start a container at all"):
        _started(engine, tmp_path)


def test_start_refuses_a_docker_socket_whose_group_is_a_task_group(tmp_path: Path) -> None:
    """The docker group must not be a group that tasks run in."""
    docker = tmp_path / "docker.sock"
    docker.write_text("", encoding="utf-8")

    with pytest.raises(StartError, match="group of the Docker socket") as caught:
        _started(FakeEngine(), tmp_path, docker_socket=docker, task_user=f"10001:{os.getgid()}")

    assert str(os.getgid()) in str(caught.value)
    _started(FakeEngine(), tmp_path, docker_socket=docker)  # another gid: fine


def test_a_container_that_never_stops_is_removed_when_the_wait_gives_up(tmp_path: Path) -> None:
    """The wait after the stream is bounded by the timeout, the grace and a margin."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    seen: list[float | None] = []
    real = engine.request

    def request(
        method: str, path: str, body: Any = None, *, query: Any = None, timeout: Any = 60.0
    ) -> tuple[int, Any]:
        if path.endswith("/wait") and "c2" in path:
            seen.append(timeout)
            raise EngineError("Docker did not answer within 123 s")
        return real(method, path, body, query=query, timeout=timeout)

    engine.request = request  # ty: ignore[invalid-assignment]

    with pytest.raises(EngineError, match="did not answer"):
        launcher.run(_run(timeout=30), lambda line: None)

    assert seen[0] is not None and 3 + 60 <= seen[0] <= 30 + 3 + 60
    assert engine.containers == {} and engine.networks == {}


def test_the_memory_of_live_runs_is_bounded_in_total(tmp_path: Path) -> None:
    """One run asks for 256 MiB; a total of 300 MiB admits one at a time."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path, max_total_memory_mb=300)
    worker = threading.Thread(target=lambda: launcher.run(_run(), lambda line: None))
    worker.start()
    _until(lambda: _task_started(engine))

    with pytest.raises(EngineError, match="LAKANA_LAUNCHER__MAX_TOTAL_MEMORY_MB"):
        launcher.run(_run("t2"), lambda line: None)

    launcher.stop(StopRequest("t1", "w1"))
    worker.join(10)
    engine.hold = False
    assert launcher._reserved_mb == 0
    assert launcher.run(_run("t3"), lambda line: None)["exit"] == 0


def _task_started(engine: FakeEngine) -> bool:
    """Say whether the task container was started: it is supervised from then on.

    Parameters
    ----------
    engine
        The fake engine.

    Returns
    -------
    bool
        Whether a second container (the first is the start-up probe) was started.
    """
    return sum(path.endswith("/start") for _, path, _ in engine.calls) >= 2


_STALLED_RUNNER_SECONDS = 60.0
"""How long a test that waits for a relay allows: a shared macOS runner has stalled for more than
10 s (#365), longer than the launcher's own wait for ``ready``. A healthy run takes milliseconds,
so only a failure waits this long."""


@pytest.fixture(autouse=True)
def _relay_ready_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the launcher's wait for a relay's ``ready`` to the stalled-runner bound (#365).

    The wait still ends at the real condition, the ``ready`` line; a test of a relay that never
    starts sets its own short bound.

    Parameters
    ----------
    monkeypatch
        pytest's patcher.
    """
    monkeypatch.setattr(service_module, "_RELAY_READY_SECONDS", _STALLED_RUNNER_SECONDS)


def _until(condition: Any, seconds: float = _STALLED_RUNNER_SECONDS) -> None:
    """Wait for a condition, failing after a bound.

    Parameters
    ----------
    condition
        A callable returning something truthy when done.
    seconds
        The bound.
    """
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "the condition was never met"
        time.sleep(0.01)


def test_a_build_reaches_its_hosts_through_the_egress_proxy_only(tmp_path: Path) -> None:
    """The build network gets the `egress` service; the proxy URL carries the build's grant."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    connected: dict[str, Any] = {}
    original = engine._network

    def spy(method: str, name: str, action: str | None, body: Any) -> tuple[int, Any]:
        """Record what is attached to which network.

        Parameters
        ----------
        method
            The HTTP method.
        name
            The network.
        action
            ``connect`` and the like.
        body
            The request body.

        Returns
        -------
        tuple
            The fake engine's answer.
        """
        if action == "connect":
            connected[name] = body
        return original(method, name, action, body)

    engine._network = spy  # ty: ignore[invalid-assignment]
    subnets: list[str] = []
    allocate = launcher._allocate_subnet

    def remember(name: str) -> str:
        """Record the subnet the build network gets.

        Parameters
        ----------
        name
            The network.

        Returns
        -------
        str
            The subnet.
        """
        subnets.append(allocate(name))
        return subnets[-1]

    launcher._allocate_subnet = remember  # ty: ignore[invalid-assignment]

    launcher.build(_build(), lambda line: None)

    (spec,) = engine.created("build")
    proxy = next(v for v in spec["Env"] if v.startswith("HTTPS_PROXY="))
    token = proxy.removeprefix("HTTPS_PROXY=http://lakana:").removesuffix("@egress:3128")
    key = read_key(tmp_path / "egress.key")
    assert key is not None
    grant = verify(key, token, time.time())
    assert grant is not None
    assert (grant.purpose, grant.targets) == ("build", frozenset({("pypi.org", 443)}))
    assert grant.network == ipaddress.ip_network(subnets[-1])
    assert time.time() + 30 < grant.expires <= time.time() + 30 + 61
    assert connected["lakana-build-b1"] == {
        "Container": "c-egress",
        "EndpointConfig": {"Aliases": ["egress"]},
    }


def test_a_build_without_the_egress_service_says_so_and_has_no_way_out(tmp_path: Path) -> None:
    """An offline build (wheels in the repository) still works; the log says why others fail."""
    engine = FakeEngine(services={"server": "c-server"})
    launcher = _started(engine, tmp_path, built=False)
    emitted: list[dict[str, object]] = []

    launcher.build(_build(), emitted.append)

    (spec,) = engine.created("build")
    assert not any(v.startswith("HTTPS_PROXY=") for v in spec["Env"])
    assert b"the egress service is not running" in _logs(emitted)
    assert b"docker compose up -d egress" in _logs(emitted)


def test_the_launcher_writes_a_new_egress_key_at_every_start(tmp_path: Path) -> None:
    """No token from before a restart stays valid: the resting /29s were forgotten with it."""
    launcher = _started(FakeEngine(), tmp_path)
    key = read_key(tmp_path / "egress.key")
    _, proxy = launcher.open_build_network("b1", "w1", [("pypi.org", 443)], 60)
    assert key is not None and proxy is not None
    token = proxy.split(":", 2)[2].split("@")[0]
    assert verify(key, token, time.time()) is not None

    Launcher(launcher.settings, FakeEngine()).start()

    fresh = read_key(tmp_path / "egress.key")
    assert fresh is not None and fresh != key
    assert verify(fresh, token, time.time()) is None
    assert stat.S_IMODE((tmp_path / "egress.key").stat().st_mode) == 0o640


def test_a_network_is_closed_by_its_public_name(tmp_path: Path) -> None:
    """`close_network` is what a caller of `open_build_network` uses afterwards."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    network, proxy = launcher.open_build_network("b9", "w1", [("pypi.org", 443)], 60)

    assert proxy is not None
    launcher.close_network(network)
    assert network not in engine.networks


def test_a_launcher_that_cannot_write_the_egress_key_says_where(tmp_path: Path) -> None:
    """The message names the path and the volume to mount."""
    blocked = tmp_path / "file"
    blocked.write_text("")

    with pytest.raises(StartError, match="egress-key volume"):
        _started(FakeEngine(), tmp_path, egress_key=blocked / "egress.key")


def test_a_build_network_needs_a_started_launcher(tmp_path: Path) -> None:
    """Without the key there is no token to give."""
    launcher = Launcher(settings(tmp_path), FakeEngine())

    with pytest.raises(RuntimeError, match="start"):
        launcher.open_build_network("b1", "w1", [("pypi.org", 443)], 60)


def test_a_build_subnet_rests_until_its_token_has_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token stays valid after its build ends: its /29 goes to no other network meanwhile.

    Parameters
    ----------
    tmp_path
        The state directory.
    monkeypatch
        Moves the launcher's clock past the token's expiry.
    """
    from lakana_worker.launcher import service

    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    first, proxy = launcher.open_build_network("b1", "w1", [("pypi.org", 443)], 60)
    key = read_key(tmp_path / "egress.key")
    assert proxy is not None and key is not None
    grant = verify(key, proxy.split(":", 2)[2].split("@")[0], time.time())
    assert grant is not None
    launcher.close_network(first)

    second, _ = launcher.open_build_network("b2", "w1", [("pypi.org", 443)], 60)
    assert launcher._subnets[second] != str(grant.network)
    launcher.close_network(second)
    task = launcher._open_network("task", "t1", "w1", [], 0)
    assert launcher._subnets[task] != str(grant.network)
    launcher.close_network(task)

    later = grant.expires + 61
    monkeypatch.setattr(
        service, "time", types.SimpleNamespace(time=lambda: later, monotonic=time.monotonic)
    )
    third, _ = launcher.open_build_network("b3", "w1", [("pypi.org", 443)], 60)
    assert launcher._subnets[third] == str(grant.network)


def test_a_build_whose_token_would_be_too_long_is_refused_and_leaves_no_network(
    tmp_path: Path,
) -> None:
    """32 long host names fit the request but not a token: a refusal, nothing left behind."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    hosts = [(f"{'a' * 60}.{'b' * 60}.{'c' * 60}.h{n}.example.org", 443) for n in range(32)]

    with pytest.raises(Refused, match="too long"):
        launcher.open_build_network("b1", "w1", hosts, 60)
    assert not [n for n in engine.networks if n.startswith("lakana-build")]


def test_resting_subnets_count_against_the_network_cap(tmp_path: Path) -> None:
    """The cap trips, naming the resting subnets, before the address range runs out."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, network_range="10.207.0.0/26")
    assert launcher._max_networks == 6
    for n in range(6):
        network, _ = launcher.open_build_network(f"b{n}", "w1", [("pypi.org", 443)], 60)
        launcher.close_network(network)

    with pytest.raises(EngineError, match="6 resting"):
        launcher.open_build_network("b9", "w1", [("pypi.org", 443)], 60)


_DB = RelayRoute(15001, "db.example.com", 5432)


def _egress_run(**fields: Any) -> RunRequest:
    """Build a run request with egress pairs or relay routes.

    Parameters
    ----------
    **fields
        `egress`, `relay` and other fields to replace.

    Returns
    -------
    RunRequest
        The request.
    """
    return replace(_run(), **fields)


def _subnet_of(launcher: Launcher, engine: FakeEngine) -> list[str]:
    """Record the subnet each network of a launcher gets.

    Parameters
    ----------
    launcher
        The launcher.
    engine
        Its fake engine.

    Returns
    -------
    list of str
        Filled as networks are created.
    """
    subnets: list[str] = []
    allocate = launcher._allocate_subnet

    def remember(name: str) -> str:
        """Record the subnet.

        Parameters
        ----------
        name
            The network.

        Returns
        -------
        str
            The subnet.
        """
        subnets.append(allocate(name))
        return subnets[-1]

    launcher._allocate_subnet = remember  # ty: ignore[invalid-assignment]
    return subnets


def _task_running(engine: FakeEngine) -> bool:
    """Say whether a task container is running (a relay may have started before it).

    Parameters
    ----------
    engine
        The fake engine.

    Returns
    -------
    bool
        Whether a container of kind ``task`` was started and not yet removed.
    """
    return any(
        c["running"] and c["Labels"]["lakana.launcher.kind"] == "task"
        for c in list(engine.containers.values())
    )


def _paths(engine: FakeEngine, method: str) -> list[str]:
    """List the paths of every call of one method, in order.

    Parameters
    ----------
    engine
        The fake engine.
    method
        ``POST``, ``DELETE`` and so on.

    Returns
    -------
    list of str
        The paths.
    """
    return [path for m, path, _ in engine.calls if m == method]


def test_a_run_with_egress_gets_the_proxy_and_a_task_token_for_exactly_its_pairs(
    tmp_path: Path,
) -> None:
    """Server and proxy join the task network; the token is purpose `task`, bound to its /29."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    subnets = _subnet_of(launcher, engine)
    pairs = (("api.example.com", 443), ("login.example.com", 443))

    launcher.run(_egress_run(egress=pairs), lambda line: None)

    connects = [b for m, p, b in engine.calls if p == "/networks/lakana-task-t1/connect"]
    assert connects == [
        {"Container": "c-server", "EndpointConfig": {"Aliases": ["server"]}},
        {"Container": "c-egress", "EndpointConfig": {"Aliases": ["egress"]}},
    ]
    (spec,) = engine.created("task")
    env = dict(item.split("=", 1) for item in spec["Env"])
    assert not {"HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"} & set(env)
    token = env["LAKANA_EGRESS_PROXY"].removeprefix("http://lakana:").removesuffix("@egress:3128")
    key = read_key(tmp_path / "egress.key")
    assert key is not None
    grant = verify(key, token, time.time())
    assert grant is not None
    assert (grant.purpose, grant.targets) == ("task", frozenset(pairs))
    assert grant.network == ipaddress.ip_network(subnets[0])
    assert time.time() + 30 + 3 < grant.expires <= time.time() + 30 + 3 + 61
    assert "lakana-task-t1" not in engine.networks
    assert subnets[0] in launcher._resting  # no other network gets it while the token lives


def test_a_task_token_is_measured_from_after_the_image_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pull takes minutes; a token minted before it would expire before the task's timeout."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    engine.images.clear()
    skew = [0.0]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + skew[0])

    def slow_pull() -> None:
        """Let a ten-minute pull pass on the clock."""
        skew[0] += 600.0

    engine.hooks["pull"] = slow_pull

    launcher.run(_egress_run(egress=(("api.example.com", 443),)), lambda line: None)

    (spec,) = engine.created("task")
    env = dict(item.split("=", 1) for item in spec["Env"])
    token = env["LAKANA_EGRESS_PROXY"].removeprefix("http://lakana:").removesuffix("@egress:3128")
    key = read_key(tmp_path / "egress.key")
    assert key is not None
    grant = verify(key, token, time.time())
    assert grant is not None
    assert time.time() + 30 < grant.expires <= time.time() + 30 + 3 + 61


@pytest.mark.parametrize(
    "fields", [{"egress": (("api.example.com", 443),)}, {"relay": (_DB,)}], ids=["egress", "relay"]
)
def test_a_run_with_connections_without_the_egress_service_is_refused_first(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    """The fixed refusal, before any network or container exists.

    Parameters
    ----------
    tmp_path
        The state directory.
    fields
        Egress pairs or relay routes.
    """
    engine = FakeEngine(services={"server": "c-server"})
    launcher = _started(engine, tmp_path)
    before = len(engine.calls)

    with pytest.raises(Refused) as caught:
        launcher.run(_egress_run(**fields), lambda line: None)

    assert str(caught.value) == (
        "tasks with connections need the `egress` service; start it (Compose bundle) or remove "
        "the connection from the asset."
    )
    assert not [p for m, p, _ in engine.calls[before:] if m in ("POST", "DELETE")]


def test_a_run_without_connections_keeps_the_server_only_posture(tmp_path: Path) -> None:
    """One attachment, no token, no proxy variable, no relay, no resting subnet."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    subnets = _subnet_of(launcher, engine)

    launcher.run(_run(), lambda line: None)

    assert [b["Container"] for m, p, b in engine.calls if p.endswith("/connect")] == ["c-server"]
    (spec,) = engine.created("task")
    assert not any(item.startswith("LAKANA_EGRESS_PROXY=") for item in spec["Env"])
    assert engine.created("relay") == []
    assert subnets[0] not in launcher._resting


def test_a_run_with_relay_routes_starts_its_relay_before_the_task(tmp_path: Path) -> None:
    """The relay comes from the egress image, fixed fields, its address, then the task."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    subnets = _subnet_of(launcher, engine)

    launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    (relay,) = engine.created("relay")
    address = relay_address(subnets[0])
    assert address == str(ipaddress.ip_network(subnets[0]).network_address + 6)
    assert relay["Image"] == "sha256:" + "e" * 64
    assert relay["Entrypoint"] == list(RELAY_COMMAND)
    assert relay["User"] == "65534:65534"
    assert relay["Env"] == ["PATH=/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE=1"]
    host = relay["HostConfig"]
    assert host["Memory"] == host["MemorySwap"] == 64 * 1024 * 1024
    assert (host["NanoCpus"], host["PidsLimit"]) == (250_000_000, RELAY_PIDS)
    assert RELAY_PIDS >= 16 * (16 + 1) + 2  # a handler per connection, an acceptor per route
    assert (host["CapDrop"], host["ReadonlyRootfs"], host["Mounts"]) == (["ALL"], True, [])
    assert host["NetworkMode"] == "lakana-task-t1"
    assert relay["NetworkingConfig"] == {
        "EndpointsConfig": {
            "lakana-task-t1": {"Aliases": ["lakana-relay"], "IPAMConfig": {"IPv4Address": address}}
        }
    }
    assert relay["Labels"]["lakana.launcher.kind"] == "relay"
    assert relay["Labels"]["lakana.launcher.deadline"]
    plan = json.loads(engine.relay_stdin[0])
    assert plan == {
        "address": address,
        "subnet": subnets[0],
        "refused": _refused(tmp_path),
        "reachable": [],
        "routes": [{"listen": 15001, "host": "db.example.com", "port": 5432}],
        "verdicts": False,
    }
    kinds = [
        b["Labels"]["lakana.launcher.kind"] for m, p, b in engine.calls if p == "/containers/create"
    ]
    assert kinds[-2:] == ["relay", "task"]
    relay_id = f"c{kinds.index('relay') + 1}"  # the fake numbers containers as they are made
    posts = _paths(engine, "POST")
    assert posts.index("/networks/lakana_default/connect") < posts.index(
        f"/containers/{relay_id}/start"
    )
    deletes = _paths(engine, "DELETE")
    assert deletes.index(f"/containers/{relay_id}") < deletes.index("/networks/lakana-task-t1")
    assert engine.containers == {} and "lakana-task-t1" not in engine.networks


def test_the_relay_gets_the_upstream_proxy_of_the_egress_service(tmp_path: Path) -> None:
    """`LAKANA_EGRESS__UPSTREAM_PROXY` applies to the relay as to the proxy."""
    engine = FakeEngine()
    engine.service_info["c-egress"]["Config"]["Env"] = [
        "LAKANA_EGRESS__UPSTREAM_PROXY=http://corp:3128"
    ]
    launcher = _started(engine, tmp_path)

    launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    (relay,) = engine.created("relay")
    assert "LAKANA_EGRESS__UPSTREAM_PROXY=http://corp:3128" in relay["Env"]


def test_the_relays_lines_reach_the_tasks_log(
    tmp_path: Path,
) -> None:
    """Review Focus 3: the relay's refusal is in the run's log, prefixed as the relay wrote it.

    Both waits are on the real condition, with a bound a stalled runner stays inside (#365): the
    launcher's wait for the relay's ``ready`` (`_relay_ready_wait`) and the test's wait for the
    line in the log.

    Parameters
    ----------
    tmp_path
        The state directory.
    """
    line = b"lakana-relay: refused postgres:5432: it resolves to an internal address\n"
    engine = FakeEngine(hold=True, relay_output=[b"lakana-relay: ready\n", line])
    launcher = _started(engine, tmp_path)
    emitted: list[dict[str, object]] = []
    failed: list[BaseException] = []

    def run() -> None:
        try:
            launcher.run(_egress_run(relay=(_DB,)), emitted.append)
        except BaseException as exc:  # reported by the waits below, not as a stray thread error
            failed.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    _until(lambda: failed or line in _logs(emitted), _STALLED_RUNNER_SECONDS)
    assert not failed, failed
    _until(lambda: _task_running(engine), _STALLED_RUNNER_SECONDS)
    engine.release(
        next(
            c for c, v in engine.containers.items() if v["Labels"]["lakana.launcher.kind"] == "task"
        )
    )
    worker.join(_STALLED_RUNNER_SECONDS)

    assert not worker.is_alive()
    assert not failed, failed
    assert line in _logs(emitted)
    assert b"lakana-relay: ready" not in _logs(emitted)


def test_a_relay_that_does_not_start_fails_the_run_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run fails with the relay's last line; relay and network are gone, no task started.

    Parameters
    ----------
    tmp_path
        The state directory.
    monkeypatch
        Shortens the wait for `ready`.
    """
    from lakana_worker.launcher import service

    monkeypatch.setattr(service, "_RELAY_READY_SECONDS", 0.3)
    engine = FakeEngine(relay_output=[b"lakana-relay: cannot start: Address in use\n"])
    launcher = _started(engine, tmp_path)

    with pytest.raises(EngineError) as caught:
        launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    assert str(caught.value) == "the task's relay did not start: cannot start: Address in use"
    assert engine.created("task") == []
    assert engine.containers == {} and "lakana-task-t1" not in engine.networks


def test_a_relay_needs_exactly_one_outward_network(tmp_path: Path) -> None:
    """An egress service on two Compose networks is ambiguous: the run fails naming them."""
    engine = FakeEngine()
    engine.compose_networks["lakana_other"] = {"Labels": {}, "Containers": {}}
    engine.service_info["c-egress"]["NetworkSettings"]["Networks"]["lakana_other"] = {}
    launcher = _started(engine, tmp_path)

    with pytest.raises(EngineError, match="lakana_default, lakana_other"):
        launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    assert engine.containers == {} and "lakana-task-t1" not in engine.networks


def test_cleanup_spares_the_relay_of_a_live_run_and_removes_an_orphan(tmp_path: Path) -> None:
    """A relay belongs to its task: `cleanup(owner)` keeps it while the run lives."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path)
    worker = threading.Thread(
        target=lambda: launcher.run(_egress_run(relay=(_DB,)), lambda line: None)
    )
    worker.start()
    _until(lambda: _task_running(engine))

    assert launcher.cleanup(CleanupRequest("w1")) == {"removed": 0}
    assert engine.created("relay") and any(
        v["Labels"]["lakana.launcher.kind"] == "relay" for v in engine.containers.values()
    )
    launcher.stop(StopRequest("t1", "w1"))
    worker.join(_STALLED_RUNNER_SECONDS)
    assert not worker.is_alive()
    engine.containers["c-orphan"] = {
        "Name": "lakana-relay-t9",
        "Labels": {
            "lakana.launcher.stack": "lakana",
            "lakana.launcher.kind": "relay",
            "lakana.launcher.id": "t9",
            "lakana.launcher.owner": "w1",
        },
        "spec": {"HostConfig": {"Mounts": []}},
        "exit": None,
        "running": True,
    }
    engine._released["c-orphan"] = threading.Event()

    assert launcher.cleanup(CleanupRequest("w1")) == {"removed": 1}
    assert engine.containers == {}


_GATEWAYS = ["172.17.0.1/32", "172.18.0.1/32"]
"""The fake's default bridge and Compose network gateways (IPv4)."""


def _refused(tmp_path: Path) -> list[str]:
    """Read the refused ranges the launcher wrote for the proxy.

    Parameters
    ----------
    tmp_path
        The state directory, which also holds the egress key.

    Returns
    -------
    list of str
        The ranges.
    """
    return json.loads((tmp_path / "refused.json").read_text(encoding="utf-8"))["refused"]


def _networks_listing_answers(engine: FakeEngine, reply: Any) -> None:
    """Answer the unfiltered ``GET /networks`` (the gateway listing) with `reply`.

    Parameters
    ----------
    engine
        The fake engine.
    reply
        The status and body.
    """
    real = engine.request

    def request(
        method: str, path: str, body: Any = None, *, query: Any = None, timeout: Any = 60.0
    ) -> tuple[int, Any]:
        if method == "GET" and path == "/networks" and not query:
            return reply
        return real(method, path, body, query=query, timeout=timeout)

    engine.request = request  # ty: ignore[invalid-assignment]


def test_start_writes_every_docker_gateway_and_the_operator_ranges_for_the_proxy(
    tmp_path: Path,
) -> None:
    """Default bridge, Compose networks (IPv4 and IPv6), the setting and the launcher's range."""
    engine = FakeEngine()

    _started(engine, tmp_path, egress_refused=("192.0.2.0/24", "fd00:1::/32"))

    assert _refused(tmp_path) == [
        "10.207.0.0/16",
        *_GATEWAYS,
        "192.0.2.0/24",
        "fd00:1::/32",
        "fd12:3456::1/128",
    ]
    info = (tmp_path / "refused.json").stat()
    assert stat.S_IMODE(info.st_mode) == 0o640
    assert info.st_gid == (tmp_path / "egress.key").stat().st_gid
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_a_network_made_after_start_is_refused_before_the_next_run_attaches_anything(
    tmp_path: Path,
) -> None:
    """Refreshed at every network open, after the network exists, before a service joins it."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    engine.host_networks["other-project_default"] = {
        "Labels": {"com.docker.compose.project": "other-project"},
        "IPAM": {"Config": [{"Subnet": "192.168.77.0/24", "Gateway": "192.168.77.1"}]},
    }
    seen: list[list[str]] = []
    real = engine.request

    def request(
        method: str, path: str, body: Any = None, *, query: Any = None, timeout: Any = 60.0
    ) -> tuple[int, Any]:
        if path.endswith("/connect") and not seen:
            seen.append(_refused(tmp_path))
        return real(method, path, body, query=query, timeout=timeout)

    engine.request = request  # ty: ignore[invalid-assignment]

    launcher.run(_run(), lambda line: None)

    assert seen == [["10.207.0.0/16", *_GATEWAYS, "192.168.77.1/32", "fd12:3456::1/128"]]


def test_the_refused_ranges_are_written_only_when_they_change(tmp_path: Path) -> None:
    """A run that finds the same networks leaves the file alone: the proxy reads nothing new."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    before = (tmp_path / "refused.json").stat()

    launcher.run(_run(), lambda line: None)

    after = (tmp_path / "refused.json").stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_start_stops_when_docker_cannot_list_its_networks(tmp_path: Path) -> None:
    """Without the gateways the proxy would let a task reach the Docker host: no start."""
    engine = FakeEngine()
    _networks_listing_answers(engine, (500, {"message": "daemon busy"}))

    with pytest.raises(StartError) as caught:
        _started(engine, tmp_path)

    assert str(caught.value).startswith("cannot list Docker's networks to find their gateways")
    assert "daemon busy" in str(caught.value)
    assert not (tmp_path / "refused.json").exists()


def test_a_failed_listing_at_a_network_open_keeps_the_last_ranges_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A busy daemon does not fail the run; the ranges written at start stay in force."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    written = _refused(tmp_path)
    _networks_listing_answers(engine, (500, {"message": "daemon busy"}))

    result = launcher.run(_run(), lambda line: None)

    assert result["exit"] == 0
    assert _refused(tmp_path) == written
    assert "cannot list Docker's networks to find their gateways" in caplog.text


def test_the_relays_plan_carries_the_test_hooks_reachable_networks(tmp_path: Path) -> None:
    """`LauncherSettings.relay_reachable` reaches the plan as ``reachable`` (empty otherwise)."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, relay_reachable=("172.30.0.0/16",))

    launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    assert json.loads(engine.relay_stdin[0])["reachable"] == ["172.30.0.0/16"]


def test_the_relays_plan_refuses_what_the_proxy_refuses(tmp_path: Path) -> None:
    """The relay gets the same list as `refused.json`, current at its start."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, egress_refused=("192.0.2.0/24",))
    engine.host_networks["late"] = {
        "Labels": {},
        "IPAM": {"Config": [{"Subnet": "192.168.88.0/24", "Gateway": "192.168.88.1"}]},
    }

    launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    plan = json.loads(engine.relay_stdin[0])
    assert plan["refused"] == _refused(tmp_path)
    assert "192.168.88.1/32" in plan["refused"] and "192.0.2.0/24" in plan["refused"]


def test_a_relay_is_refused_when_docker_has_more_gateways_than_its_plan_holds(
    tmp_path: Path,
) -> None:
    """The plan holds at most 256 ranges; past them the run fails naming the next step."""
    engine = FakeEngine()
    for n in range(300):
        engine.host_networks[f"n{n}"] = {
            "Labels": {},
            "IPAM": {"Config": [{"Subnet": "x", "Gateway": f"10.{n // 200}.{n % 200}.1"}]},
        }
    launcher = _started(engine, tmp_path)

    with pytest.raises(EngineError, match="at most 256") as caught:
        launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    assert "docker network prune" in str(caught.value)
    assert engine.created("relay") == [] and engine.created("task") == []
    assert "lakana-task-t1" not in engine.networks


def test_a_gateway_that_is_not_an_address_is_skipped(tmp_path: Path) -> None:
    """Docker leaves `Gateway` out or empty for some drivers; nothing else is read from it."""
    engine = FakeEngine()
    engine.host_networks["odd"] = {
        "Labels": {},
        "IPAM": {"Config": [{"Subnet": "10.1.0.0/24", "Gateway": ""}, {"Gateway": "nope"}, "x"]},
    }
    engine.host_networks["none"] = {"Labels": {}, "IPAM": None}

    _started(engine, tmp_path)

    assert _refused(tmp_path) == ["10.207.0.0/16", *_GATEWAYS, "fd12:3456::1/128"]


def test_an_older_gateway_listing_never_replaces_a_newer_one(tmp_path: Path) -> None:
    """Two network opens at once: a listing taken before a network existed must not win.

    Open A lists before network G exists and is held there; G appears; open B lists and writes.
    When A goes on, G stays refused, in the file and in what the relays get.
    """
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    held = threading.Event()
    listed = threading.Event()
    real = engine.request

    def request(
        method: str, path: str, body: Any = None, *, query: Any = None, timeout: Any = 60.0
    ) -> tuple[int, Any]:
        answer = real(method, path, body, query=query, timeout=timeout)
        if (
            method == "GET"
            and path == "/networks"
            and not query
            and threading.current_thread().name == "open-a"
        ):
            listed.set()
            held.wait(5)
        return answer

    engine.request = request  # ty: ignore[invalid-assignment]
    first = threading.Thread(target=launcher._write_refused, name="open-a")
    first.start()
    assert listed.wait(5)
    engine.host_networks["late"] = {
        "Labels": {},
        "IPAM": {"Config": [{"Subnet": "192.168.77.0/24", "Gateway": "192.168.77.1"}]},
    }
    second = threading.Thread(target=launcher._write_refused, name="open-b")
    second.start()
    second.join(0.3)  # B either finished (no lock) or waits for A (lock)
    held.set()
    first.join(5)
    second.join(5)

    assert "192.168.77.1/32" in _refused(tmp_path)
    assert "192.168.77.1/32" in launcher._refused


def test_a_gateway_docker_spells_in_another_form_is_logged_naming_its_network(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A format change in Docker's answer must show, not leave a gateway silently unrefused."""
    engine = FakeEngine()
    engine.host_networks["odd"] = {
        "Labels": {},
        "IPAM": {"Config": [{"Gateway": "172.30.0.1/16"}, {"Gateway": ""}, {}]},
    }

    _started(engine, tmp_path)

    warnings = [r.getMessage() for r in caplog.records if "odd" in r.getMessage()]
    assert len(warnings) == 1
    assert "172.30.0.1/16" in warnings[0] and "not an address" in warnings[0]


def _unwritable(launcher: Launcher) -> None:
    """Make every write of the refused ranges fail as on a read-only volume.

    Parameters
    ----------
    launcher
        The launcher.
    """

    def fail(path: Path, text: str) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    launcher._replace_refused = fail  # ty: ignore[invalid-assignment]


def test_start_stops_when_the_refused_ranges_cannot_be_written(tmp_path: Path) -> None:
    """Without the file the proxy refuses everything: the operator hears why, at start."""
    engine = FakeEngine()
    launcher = Launcher(settings(tmp_path), engine)
    _unwritable(launcher)

    with pytest.raises(StartError) as caught:
        launcher.start()

    assert str(caught.value) == (
        f"cannot write the refused ranges {tmp_path / 'refused.json'} (Read-only file system); "
        "mount the egress-key volume into the launcher, writable (see compose.yaml)."
    )


def test_a_failed_write_after_start_keeps_the_last_file_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A run goes on; the file the proxy reads stays the last one written, and is tried again."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    written = _refused(tmp_path)
    engine.host_networks["late"] = {
        "Labels": {},
        "IPAM": {"Config": [{"Subnet": "192.168.77.0/24", "Gateway": "192.168.77.1"}]},
    }
    _unwritable(launcher)

    result = launcher.run(_run(), lambda line: None)

    assert result["exit"] == 0
    assert _refused(tmp_path) == written
    assert "Cannot write the refused ranges" in caplog.text
    assert "Read-only file system" in caplog.text
    del launcher._replace_refused  # the class's own writer again
    launcher._write_refused()
    assert "192.168.77.1/32" in _refused(tmp_path)


def test_the_refused_ranges_file_gets_the_keys_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the directory does not hand down the key's group, the launcher sets it itself."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    key = tmp_path / "egress.key"
    other = os.stat(key).st_gid + 1
    real_stat = os.stat
    chowned: list[int] = []

    def stat_with_other_group(path: Any, *args: Any, **kwargs: Any) -> Any:
        info = real_stat(path, *args, **kwargs)
        if Path(path) == key:
            fields = list(info)
            fields[stat.ST_GID] = other
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(os, "stat", stat_with_other_group)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: chowned.append(gid))
    engine.host_networks["late"] = {
        "Labels": {},
        "IPAM": {"Config": [{"Subnet": "192.168.77.0/24", "Gateway": "192.168.77.1"}]},
    }

    launcher._write_refused()

    assert chowned == [other]
    assert "192.168.77.1/32" in _refused(tmp_path)


def test_a_run_with_egress_needs_a_started_launcher(tmp_path: Path) -> None:
    """Without `start()` there is no egress key to sign a token with."""
    engine = FakeEngine()
    launcher = Launcher(settings(tmp_path), engine)

    with pytest.raises(RuntimeError, match="only available after start"):
        launcher.run(_egress_run(egress=(("api.example.com", 443),)), lambda line: None)

    assert not [p for m, p, _ in engine.calls if m in ("POST", "DELETE")]


def test_egress_pairs_that_do_not_fit_in_a_token_are_refused_and_leave_nothing(
    tmp_path: Path,
) -> None:
    """The token has a size limit; the run is refused naming egress, and its network goes."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    pairs = tuple((f"{'h' * 60}{n}.example.com", 443) for n in range(400))

    with pytest.raises(Refused, match=r"^egress: "):
        launcher.run(_egress_run(egress=pairs), lambda line: None)

    assert engine.created("task") == []
    assert "lakana-task-t1" not in engine.networks


def test_pairs_too_long_for_a_token_are_refused_with_the_fixed_beginning(tmp_path: Path) -> None:
    """32 pairs of long host names fit the count but not the token: the worker reads the text."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    label = "h" * 62
    pairs = tuple((f"{label}.{label}.{label}.n{n:02d}.example.com", 443) for n in range(32))

    with pytest.raises(Refused) as caught:
        launcher.run(_egress_run(egress=pairs), lambda line: None)

    assert str(caught.value).startswith(EGRESS_TOKEN_TOO_LONG)
    assert engine.created("task") == []


def test_a_relay_fails_the_run_when_docker_cannot_describe_an_egress_network(
    tmp_path: Path,
) -> None:
    """The relay's way out must be known; an unanswered network description fails the run."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    _intercept(
        engine,
        lambda m, p, b: m == "GET" and p == "/networks/lakana_default",
        (500, {"message": "daemon busy"}),
    )

    with pytest.raises(EngineError, match="could not describe network lakana_default: daemon busy"):
        launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    assert engine.created("relay") == [] and "lakana-task-t1" not in engine.networks


def test_the_relays_way_out_skips_the_networks_the_launcher_made(tmp_path: Path) -> None:
    """The egress service also sits on build networks; only the Compose network is its way out."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)  # a start removes the launcher's own networks
    engine.service_info["c-egress"]["NetworkSettings"]["Networks"]["lakana-build-b9"] = {}
    engine.networks["lakana-build-b9"] = {
        "Labels": {"lakana.launcher.stack": "lakana"},
        "Containers": {},
    }

    launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    assert "/networks/lakana_default/connect" in _paths(engine, "POST")
    assert "/networks/lakana-build-b9/connect" not in _paths(engine, "POST")


def test_a_relay_fails_the_run_when_docker_cannot_describe_the_egress_service(
    tmp_path: Path,
) -> None:
    """The relay runs the egress service's image: without it there is no relay and no task."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    engine.service_info["c-egress"] = {"message": "no such container"}

    with pytest.raises(EngineError, match="could not describe the egress service"):
        launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    assert engine.created("relay") == [] and engine.created("task") == []


@pytest.mark.parametrize("step", ["attach", "start"])
def test_a_relay_docker_refuses_to_attach_or_start_is_removed(tmp_path: Path, step: str) -> None:
    """Either refusal fails the run with Docker's reason; the relay container goes with it.

    Parameters
    ----------
    tmp_path
        The state directory.
    step
        Which call Docker refuses.
    """
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    if step == "attach":
        _intercept(
            engine,
            lambda m, p, b: m == "POST" and p == "/networks/lakana_default/connect",
            (500, {"message": "no"}),
        )
    else:
        _intercept(
            engine, lambda m, p, b: m == "POST" and p.endswith("/start"), (500, {"message": "no"})
        )

    with pytest.raises(EngineError, match=f"Docker could not {step} the relay: no"):
        launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    assert engine.containers == {} and "lakana-task-t1" not in engine.networks


def test_the_relay_reader_ends_quietly_when_its_stream_breaks() -> None:
    """A removed relay's stream fails; the reader ends without a line and without `ready`."""

    class Broken:
        def recv(self, size: int, /) -> bytes:
            raise ConnectionResetError(104, "Connection reset by peer")

    ready = threading.Event()
    last: list[str] = []
    emitted: list[dict[str, object]] = []

    Launcher._read_relay(Broken(), ready, last, emitted.append, threading.Event())  # ty: ignore[invalid-argument-type]

    assert not ready.is_set() and last == [] and emitted == []


def test_a_run_stopped_while_its_relay_starts_never_creates_the_task(tmp_path: Path) -> None:
    """The stop arrives between the relay and the task: the task container is never made."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)

    def stop() -> None:
        """Stop the run while its relay is being attached."""
        launcher.stop(StopRequest("t1", "w1"))

    engine.hooks["attach"] = stop

    result = launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    assert result == {"exit": None, "oom_killed": False, "timed_out": False, "stopped": True}
    assert engine.created("task") == []
    assert engine.containers == {} and "lakana-task-t1" not in engine.networks


def _check(**fields: Any) -> CheckRequest:
    """Build a check request.

    Parameters
    ----------
    **fields
        Fields to replace.

    Returns
    -------
    CheckRequest
        The request.
    """
    return replace(
        CheckRequest(
            check="k1", owner="w1", python="3.13", attempt="attempt-check_k1", stdin=b'{"c": 1}'
        ),
        **fields,
    )


def test_a_check_has_no_server_and_a_connection_test_token(tmp_path: Path) -> None:
    """Only the proxy joins a check's network; its token has purpose `connection-test`."""
    engine = FakeEngine(output=[b'{"outcome": "ok", "message": "reachable"}\n'])
    launcher = _started(engine, tmp_path, built=False)
    emitted: list[dict[str, object]] = []

    result = launcher.check(_check(egress=(("api.example.com", 443),)), emitted.append)

    assert result["exit"] == 0
    assert _logs(emitted) == b'{"outcome": "ok", "message": "reachable"}\n'
    connects = [b for m, p, b in engine.calls if p == "/networks/lakana-check-k1/connect"]
    assert connects == [{"Container": "c-egress", "EndpointConfig": {"Aliases": ["egress"]}}]
    (spec,) = engine.created("check")
    proxy = next(v for v in spec["Env"] if v.startswith("LAKANA_EGRESS_PROXY="))
    token = proxy.split("http://lakana:", 1)[1].removesuffix("@egress:3128")
    key = read_key(tmp_path / "egress.key")
    assert key is not None
    grant = verify(key, token, time.time())
    assert grant is not None and grant.purpose == "connection-test"
    assert engine.stdin[-1] == b'{"c": 1}'
    assert engine.containers == {} and "lakana-check-k1" not in engine.networks


def test_a_check_with_relay_routes_gets_its_relay(tmp_path: Path) -> None:
    """The relay joins the check's network, as a run's joins the task's, and reports verdicts."""
    engine = FakeEngine(relay_output=[b"lakana-relay: ready\n", _CONNECTED])
    launcher = _started(engine, tmp_path, built=False)

    launcher.check(_check(relay=(_DB,)), lambda line: None)

    (relay,) = engine.created("relay")
    assert relay["HostConfig"]["NetworkMode"] == "lakana-check-k1"
    assert relay["Labels"]["lakana.launcher.id"] == "k1"
    assert json.loads(engine.relay_stdin[0])["verdicts"] is True
    assert engine.containers == {}


def test_a_check_with_connections_without_the_egress_service_is_refused(tmp_path: Path) -> None:
    """The same fixed refusal as a run's."""
    engine = FakeEngine(services={"server": "c-server"})
    launcher = _started(engine, tmp_path, built=False)

    with pytest.raises(Refused, match="tasks with connections need the `egress` service"):
        launcher.check(_check(relay=(_DB,)), lambda line: None)


def test_a_check_can_be_stopped_by_its_owner(tmp_path: Path) -> None:
    """`stop` reaches a check as it reaches a run."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path, built=False)
    results: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: results.append(launcher.check(_check(), lambda line: None))
    )
    worker.start()
    _until(
        lambda: any(
            c["running"] and c["Labels"]["lakana.launcher.kind"] == "check"
            for c in list(engine.containers.values())
        )
    )

    assert launcher.stop(StopRequest("k1", "w1")) == {"stopped": True}
    worker.join(10)
    assert results[0]["stopped"] is True


def test_a_check_token_is_measured_from_after_the_image_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As for a run: a token minted before a ten-minute pull would have expired at the start."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    engine.images.clear()
    skew = [0.0]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + skew[0])

    def slow_pull() -> None:
        """Let a ten-minute pull pass on the clock."""
        skew[0] += 600.0

    engine.hooks["pull"] = slow_pull

    launcher.check(_check(egress=(("api.example.com", 443),)), lambda line: None)

    (spec,) = engine.created("check")
    env = dict(item.split("=", 1) for item in spec["Env"])
    token = env["LAKANA_EGRESS_PROXY"].removeprefix("http://lakana:").removesuffix("@egress:3128")
    key = read_key(tmp_path / "egress.key")
    assert key is not None
    grant = verify(key, token, time.time())
    assert grant is not None
    assert time.time() + 30 < grant.expires <= time.time() + 30 + 1 + 61
    assert int(spec["Labels"][LABEL_DEADLINE]) > time.time() + 30


def test_a_worker_that_left_during_a_checks_pull_never_starts_it(tmp_path: Path) -> None:
    """The check's caller hung up while the image was pulled: no network, no container."""
    engine = FakeEngine(hold=True)
    launcher = _started(engine, tmp_path, built=False)
    engine.images.clear()
    engine.hooks["pull"] = lambda: launcher.abandon(
        container_name(launcher.settings, "check", "k1"), "w1"
    )

    result = launcher.check(_check(egress=(("api.example.com", 443),)), lambda line: None)

    assert result["stopped"] is True
    assert engine.created("check") == [] and _started_count(engine) == 0
    assert engine.networks == {}


def test_a_worker_that_left_before_the_check_registered_never_starts_it(tmp_path: Path) -> None:
    """The watcher can fire before the request reaches `check`; nothing is pulled or created."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    launcher.abandon(container_name(launcher.settings, "check", "k1"), "w1")
    before = len(engine.calls)

    result = launcher.check(_check(), lambda line: None)

    assert result["stopped"] is True and result["exit"] is None
    assert [p for m, p, _ in engine.calls[before:] if m == "POST"] == []


def test_a_check_naming_an_unusable_root_is_refused_with_its_reason(tmp_path: Path) -> None:
    """The launcher's probe found the root unsafe: a check is refused as a run is."""
    engine = FakeEngine(probe_exits={"data": 4})
    launcher = _roots(tmp_path, engine)

    with pytest.raises(Refused, match="ingestion root 'data' holds a socket"):
        launcher.check(_check(roots=("data",)), lambda line: None)

    assert engine.created("check") == []


def test_a_check_with_egress_needs_a_started_launcher(tmp_path: Path) -> None:
    """Without `start()` there is no egress key to sign the check's token with."""
    engine = FakeEngine()
    launcher = Launcher(settings(tmp_path), engine)

    with pytest.raises(RuntimeError, match="only available after start"):
        launcher.check(_check(egress=(("api.example.com", 443),)), lambda line: None)

    assert not [p for m, p, _ in engine.calls if m in ("POST", "DELETE")]


def test_a_check_mounts_a_usable_root_read_only_and_nothing_of_a_task(tmp_path: Path) -> None:
    """The root the probe accepted, at ``/ingest/<root>``; no code, environment or work mount."""
    engine = FakeEngine()
    launcher = _roots(tmp_path, engine)

    result = launcher.check(_check(roots=("data",)), lambda line: None)

    assert result["exit"] == 0
    (spec,) = engine.created("check")
    mounts = spec["HostConfig"]["Mounts"]
    assert [(m["Target"], m["ReadOnly"]) for m in mounts] == [("/ingest/data", True)]


_CONNECTED = b"lakana-relay: connected db.example.com:5432\n"


def test_a_check_waits_for_its_relays_verdict_that_comes_after_the_check_exits(
    tmp_path: Path,
) -> None:
    """The check said `ok` at once; the relay was still dialling. Its line is not lost."""
    engine = FakeEngine(relay_output=[b"lakana-relay: ready\n", 0.5, _CONNECTED])
    launcher = _started(engine, tmp_path, built=False)
    emitted: list[dict[str, object]] = []

    result = launcher.check(_check(relay=(_DB,)), emitted.append)

    assert result["exit"] == 0
    assert _CONNECTED in _logs(emitted)
    assert engine.containers == {}


def test_a_check_waits_for_a_late_refusal_too(tmp_path: Path) -> None:
    line = (
        b"lakana-relay: refused db.example.com:5432: cannot reach db.example.com:5432 (timed out)\n"
    )
    engine = FakeEngine(relay_output=[b"lakana-relay: ready\n", 0.5, line])
    launcher = _started(engine, tmp_path, built=False)
    emitted: list[dict[str, object]] = []

    launcher.check(_check(relay=(_DB,)), emitted.append)

    assert line in _logs(emitted)


def test_a_verdict_that_never_comes_ends_the_wait_at_its_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No line within the bound (the dial timeout and a margin): the relay goes, the check ends."""
    monkeypatch.setattr(service_module, "_RELAY_VERDICT_SECONDS", 0.3)
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    started = time.monotonic()

    result = launcher.check(_check(relay=(_DB,)), lambda line: None)

    assert result["exit"] == 0
    assert 0.3 <= time.monotonic() - started < 5
    assert engine.containers == {}


def test_a_task_never_waits_for_a_relay_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a check's relay reports verdicts; a run ends as soon as its task does."""
    monkeypatch.setattr(service_module, "_RELAY_VERDICT_SECONDS", 30.0)
    engine = FakeEngine()
    launcher = _started(engine, tmp_path)
    started = time.monotonic()

    launcher.run(_egress_run(relay=(_DB,)), lambda line: None)

    assert time.monotonic() - started < 10


def test_the_relay_verdict_bound_covers_the_dial_timeout() -> None:
    assert service_module._RELAY_VERDICT_SECONDS > egress_module._CONNECT_SECONDS


def test_a_relay_line_that_is_no_verdict_does_not_end_the_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ``connected`` or ``refused`` is a verdict; another line is passed on and waited past."""
    monkeypatch.setattr(service_module, "_RELAY_VERDICT_SECONDS", 0.5)
    other = b"lakana-relay: closed a connection on 15001: 16 are open already\n"
    engine = FakeEngine(relay_output=[b"lakana-relay: ready\n", other])
    launcher = _started(engine, tmp_path, built=False)
    emitted: list[dict[str, object]] = []
    started = time.monotonic()

    launcher.check(_check(relay=(_DB,)), emitted.append)

    assert other in _logs(emitted)
    assert time.monotonic() - started >= 0.5


def test_a_check_is_supervised_for_its_fixed_30_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout comes from `CHECK_LIMITS`, never from the request or the settings."""
    engine = FakeEngine()
    launcher = _started(engine, tmp_path, built=False)
    seen: list[int] = []
    real = launcher._supervise

    def recording(
        name: str, spec: dict[str, Any], live: Any, timeout: int, *args: Any, **kw: Any
    ) -> dict[str, object]:
        seen.append(timeout)
        return real(name, spec, live, timeout, *args, **kw)

    monkeypatch.setattr(launcher, "_supervise", recording)

    launcher.check(_check(), lambda line: None)

    assert seen == [30]


def test_a_relays_output_buffered_at_its_removal_still_reaches_the_caller(tmp_path: Path) -> None:
    """The stream is closed only after the reader drained it: the last lines are not lost."""
    first = b"lakana-relay: closed a connection on 15001: 16 are open already\n"
    second = b"lakana-relay: closed a connection on 15001: 16 are open already (again)\n"
    engine = FakeEngine(
        relay_output=[b"lakana-relay: ready\n", _CONNECTED],
        relay_last_words=[first, 0.3, second],
    )
    launcher = _started(engine, tmp_path, built=False)
    emitted: list[dict[str, object]] = []

    launcher.check(_check(relay=(_DB,)), emitted.append)

    assert first in _logs(emitted) and second in _logs(emitted)
