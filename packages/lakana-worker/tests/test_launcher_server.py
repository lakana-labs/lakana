"""The launcher's Unix socket: the token, one request per connection, and a caller that leaves."""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import socket
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from launcher_fakes import FakeEngine, settings

from lakana_worker.launcher import server as server_module
from lakana_worker.launcher.engine import EngineError
from lakana_worker.launcher.requests import StopRequest, environment_volume
from lakana_worker.launcher.server import (
    TOKEN_FILE,
    LauncherServer,
    remove_stale_socket,
    serve,
    write_token,
)
from lakana_worker.launcher.service import Launcher, StartError

RUN = {
    "op": "run",
    "task": "t1",
    "owner": "w1",
    "python": "3.13",
    "environment": "a" * 64,
    "attempt": "attempt-x1",
    "memory_mb": 256,
    "cpus": 1,
    "pids": 64,
    "timeout_seconds": 30,
    "kill_grace_seconds": 1,
    "stdin": "{}",
    "roots": [],
    "trust": False,
}


@dataclass
class Served:
    """A running launcher server and what a test needs to reach it.

    Parameters
    ----------
    engine
        The fake Docker engine.
    token
        The token every request carries.
    server
        The socket server.
    launcher
        The launcher behind it.
    """

    engine: FakeEngine
    token: str
    server: LauncherServer
    launcher: Launcher


@pytest.fixture
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Served]:
    """Serve a started launcher on a short relative socket path in the test's directory.

    Parameters
    ----------
    tmp_path
        The test's directory, which becomes the working directory (a Unix socket path is
        limited to 108 bytes).
    monkeypatch
        Changes the working directory.

    Yields
    ------
    Served
        The running server.
    """
    monkeypatch.chdir(tmp_path)
    engine = FakeEngine(hold=True)
    launcher = Launcher(settings(tmp_path), engine)
    launcher.start()
    volume = environment_volume(launcher.settings, "3.13", "a" * 64)
    launcher._mark_complete(volume)
    engine.volumes.add(volume)
    token = write_token(Path("."))
    server = LauncherServer("l.sock", launcher=launcher, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Served(engine, token, server, launcher)
    server.shutdown()
    server.server_close()


def _call(request: dict[str, object]) -> list[dict[str, object]]:
    """Send one request and read every answer line.

    Parameters
    ----------
    request
        The request.

    Returns
    -------
    list of dict
        The decoded lines.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
        client.sendall(json.dumps(request).encode() + b"\n")
        data = b""
        while chunk := client.recv(65536):
            data += chunk
    return [json.loads(line) for line in data.splitlines()]


def _wait_for(condition: Callable[[], object], seconds: float = 5.0) -> None:
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


def test_a_request_without_the_token_is_refused_and_the_log_has_no_value(
    served: Served, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal names the rule and the fix; the log line holds no field of the request."""
    caplog.set_level(logging.WARNING)

    answer = _call({**RUN, "token": "not-it", "stdin": "SENTINEL"})

    assert len(answer) == 1 and "LAKANA_RUN__LAUNCHER_SOCKET" in str(answer[0]["refused"])
    assert "SENTINEL" not in caplog.text and "not-it" not in caplog.text


def test_a_run_streams_its_logs_then_its_result(served: Served) -> None:
    """Log lines come first, then exactly one result line."""
    served.engine.hold = False

    answer = _call({**RUN, "token": served.token})

    assert base64.b64decode(str(answer[0]["log"])) == b"hello\n"
    assert answer[-1] == {
        "result": {"exit": 0, "oom_killed": False, "timed_out": False, "stopped": False}
    }
    assert served.launcher._abandoned == set()


def test_stop_status_and_cleanup_answer_one_result_line(served: Served) -> None:
    """The short operations need no stream."""
    token = served.token

    assert _call({"op": "stop", "token": token, "id": "t9", "owner": "w1"}) == [
        {"result": {"stopped": False}}
    ]
    assert _call({"op": "status", "token": token, "python": "3.13", "environment": "a" * 64}) == [
        {"result": {"complete": True}}
    ]
    assert _call({"op": "cleanup", "token": token, "owner": "w1"}) == [{"result": {"removed": 0}}]


def test_the_token_file_is_readable_by_the_group_only(served: Served) -> None:
    """The worker shares the launcher's group through the volume; nobody else reads it."""
    mode = stat.S_IMODE(Path(TOKEN_FILE).stat().st_mode)

    assert mode == 0o640


def test_the_token_file_mode_does_not_depend_on_the_umask(tmp_path: Path) -> None:
    """The mode is set on the open file, so no path can be swapped for a link in between."""
    old = os.umask(0o277)
    try:
        token = write_token(tmp_path)
    finally:
        os.umask(old)

    written = tmp_path / TOKEN_FILE
    assert stat.S_IMODE(written.stat().st_mode) == 0o640
    assert written.read_text(encoding="ascii") == token and len(token) == 64
    assert [p.name for p in tmp_path.iterdir()] == [TOKEN_FILE]


def test_a_token_is_new_each_time_and_the_temporary_name_is_not_guessable(
    tmp_path: Path,
) -> None:
    """A restart invalidates what a worker read; planted entries at a guessable name do nothing."""
    (tmp_path / f".{TOKEN_FILE}.{os.getpid()}").mkdir()
    (tmp_path / f".{TOKEN_FILE}.1").write_text("planted", encoding="ascii")

    first = write_token(tmp_path)
    second = write_token(tmp_path)

    assert first != second and (tmp_path / TOKEN_FILE).read_text(encoding="ascii") == second


def test_a_temporary_name_that_exists_is_replaced_by_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker may create the name between the check and the open; the open is exclusive."""
    (tmp_path / f".{TOKEN_FILE}.aaaa").write_text("planted", encoding="ascii")
    names = iter(["aaaa", "bbbb"])
    real = server_module.secrets.token_hex
    monkeypatch.setattr(
        server_module.secrets, "token_hex", lambda n: next(names) if n == 8 else real(n)
    )

    token = write_token(tmp_path)

    assert (tmp_path / TOKEN_FILE).read_text(encoding="ascii") == token
    assert (tmp_path / f".{TOKEN_FILE}.aaaa").read_text(encoding="ascii") == "planted"


def test_a_directory_that_never_gives_a_free_name_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a few tries the launcher stops, with a message and not a traceback."""
    (tmp_path / f".{TOKEN_FILE}.aaaa").write_text("planted", encoding="ascii")
    real = server_module.secrets.token_hex
    monkeypatch.setattr(server_module.secrets, "token_hex", lambda n: "aaaa" if n == 8 else real(n))

    with pytest.raises(StartError, match="cannot create the token file"):
        write_token(tmp_path)


def test_an_engine_failure_answers_error_with_its_text(served: Served) -> None:
    """The worker turns an `error` line into "runner unavailable"."""
    served.engine.services = {}

    answer = _call({**RUN, "token": served.token})

    assert answer == [
        {
            "error": "the server service of this stack is not running; start it "
            "(docker compose up -d server)"
        }
    ]


def test_an_error_that_is_not_the_callers_is_answered_and_logged(
    served: Served, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk (an OSError) is not a caller that left: the worker is told."""

    def broken(request: object, emit: object) -> dict[str, object]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(served.launcher, "handle", broken)

    answer = _call({**RUN, "token": served.token})

    assert answer == [{"error": "launcher internal error; see docker compose logs launcher"}]
    assert "No space left on device" in caplog.text


def test_a_failure_of_the_launcher_itself_is_answered_as_an_internal_error(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any exception is an answer, never a dropped connection."""

    def broken(request: object, emit: object) -> dict[str, object]:
        raise KeyError("Id")

    monkeypatch.setattr(served.launcher, "handle", broken)

    assert _call({"op": "cleanup", "token": served.token, "owner": "w1"}) == [
        {"error": "launcher internal error; see docker compose logs launcher"}
    ]


def test_a_failure_to_answer_does_not_stop_the_server(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that leaves before its error line arrives costs nothing but a log line."""

    def broken(request: object, emit: object) -> dict[str, object]:
        raise EngineError("down")

    monkeypatch.setattr(served.launcher, "handle", broken)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
        client.sendall(
            json.dumps({"op": "cleanup", "token": served.token, "owner": "w1"}).encode() + b"\n"
        )
    monkeypatch.delattr(served.launcher, "handle")

    assert _call({"op": "stop", "token": served.token, "id": "t9", "owner": "w1"}) == [
        {"result": {"stopped": False}}
    ]


def test_a_client_that_disconnects_mid_run_gets_its_container_removed(served: Served) -> None:
    """Review focus: the watcher sees the closed connection and the launcher kills the task."""
    engine = served.engine
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect("l.sock")
    client.sendall(json.dumps({**RUN, "token": served.token}).encode() + b"\n")
    _wait_for(lambda: sum(path.endswith("/start") for _, path, _ in engine.calls) >= 2)

    client.close()

    _wait_for(lambda: not engine.containers and not engine.networks)


def test_a_client_that_leaves_while_logs_flow_does_not_leave_the_task_running(
    served: Served,
) -> None:
    """Writing to a closed connection ends the run through the caller-gone path."""
    served.engine.hold = False
    served.engine.output = [b"x" * 60000] * 200
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect("l.sock")
    client.sendall(json.dumps({**RUN, "token": served.token}).encode() + b"\n")
    client.recv(10)

    client.close()

    _wait_for(lambda: not served.engine.containers and not served.engine.networks)
    _wait_for(lambda: not served.launcher._live)


def test_a_silent_connection_is_closed_and_frees_its_slot(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a bound 128 idle connections would block every run; the watcher is not bounded."""
    monkeypatch.setattr(server_module, "_READ_TIMEOUT", 0.2)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
        data = client.recv(4096)

    assert json.loads(data) == {
        "refused": "no request line arrived in time; send the request at once"
    }
    _wait_for(lambda: served.server.slots._value == server_module._MAX_CONNECTIONS)


def test_too_many_connections_are_refused_with_the_next_step(served: Served) -> None:
    """The limit answers at once instead of queueing without bound."""
    served.server.slots = threading.BoundedSemaphore(0)

    answer = _call({**RUN, "token": served.token})

    assert answer == [
        {
            "refused": "the launcher is busy: too many connections at once; retry, or "
            "lower the number of workers"
        }
    ]


def test_a_refusal_sent_to_a_caller_that_left_is_dropped(served: Served) -> None:
    """The write fails; nothing escapes and the next caller is served."""
    served.server.slots = threading.BoundedSemaphore(0)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
    time.sleep(0.2)
    served.server.slots = threading.BoundedSemaphore(1)

    assert _call({"op": "stop", "token": served.token, "id": "t9", "owner": "w1"}) == [
        {"result": {"stopped": False}}
    ]


def test_another_owner_that_names_a_live_id_and_leaves_kills_nothing(served: Served) -> None:
    """#305: the duplicate is refused, and its departure is not the first owner's departure."""
    engine = served.engine
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect("l.sock")
    client.sendall(json.dumps({**RUN, "token": served.token}).encode() + b"\n")
    _wait_for(lambda: sum(path.endswith("/start") for _, path, _ in engine.calls) >= 2)

    for _ in range(5):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as intruder:
            intruder.connect("l.sock")
            intruder.sendall(
                json.dumps({**RUN, "owner": "w2", "token": served.token}).encode() + b"\n"
            )
    _call({"op": "stop", "token": served.token, "id": "t1", "owner": "w2"})
    time.sleep(0.3)

    assert not any(path.endswith(("/kill", "/stop")) for _, path, _ in engine.calls)
    assert served.launcher.stop(StopRequest("t1", "w1")) == {"stopped": True}
    answer = client.makefile("rb").readline()
    client.close()
    assert json.loads(answer)["result"]["stopped"] is True


def test_bytes_a_caller_sends_after_its_request_are_ignored(served: Served) -> None:
    """The watcher reads and drops them; only the end of the connection means anything."""
    engine = served.engine
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect("l.sock")
    client.sendall(json.dumps({**RUN, "token": served.token}).encode() + b"\n")
    _wait_for(lambda: sum(path.endswith("/start") for _, path, _ in engine.calls) >= 2)

    client.sendall(b"noise")
    time.sleep(0.1)
    assert engine.containers
    assert served.launcher.stop(StopRequest("t1", "w1")) == {"stopped": True}

    answer = client.makefile("rb").readline()
    client.close()
    assert json.loads(answer)["result"]["stopped"] is True


def test_a_trickling_connection_is_cut_at_one_deadline_for_the_whole_line(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One byte before each timeout would otherwise hold a slot for ever."""
    monkeypatch.setattr(server_module, "_READ_TIMEOUT", 0.4)
    started = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
        answer = b""
        for _ in range(40):
            try:
                client.sendall(b" ")
            except OSError:
                break
            time.sleep(0.1)
            client.settimeout(0.01)
            try:
                answer = client.recv(4096)
            except TimeoutError:
                continue
            break

    assert json.loads(answer) == {
        "refused": "no request line arrived in time; send the request at once"
    }
    assert time.monotonic() - started < 2.0


def test_a_deadline_that_has_passed_refuses_at_once(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No byte is waited for once the time is up."""
    monkeypatch.setattr(server_module, "_READ_TIMEOUT", 0.0)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
        data = client.recv(4096)

    assert json.loads(data)["refused"].startswith("no request line arrived in time")


def test_a_line_cut_short_by_the_end_of_the_connection_is_refused(served: Served) -> None:
    """A caller that sends half a request and hangs up gets the schema refusal."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
        client.sendall(b'{"op": "stop"')
        client.shutdown(socket.SHUT_WR)
        data = client.recv(4096)

    assert json.loads(data) == {"refused": "a request is one JSON object"}


def test_busy_connections_are_refused_without_a_thread_of_their_own(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is written in the accept loop, so a flood cannot make threads without bound."""
    threads: list[object] = []
    real = LauncherServer.process_request_thread
    monkeypatch.setattr(
        LauncherServer,
        "process_request_thread",
        lambda self, request, address: (threads.append(request), real(self, request, address))[1],
    )
    served.server.slots = threading.BoundedSemaphore(0)
    clients = []
    for _ in range(20):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect("l.sock")
        client.sendall(b"x" * 10)
        clients.append(client)
    answers = [client.recv(4096) for client in clients]
    for client in clients:
        client.close()

    assert all(b"too many connections" in answer for answer in answers)
    assert threads == []


def test_an_exception_while_parsing_is_answered_and_logged(
    served: Served, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug in a field parser is not a dropped connection."""

    def broken(raw: bytes, *, token: str, settings: object) -> object:
        raise TypeError("unsupported operand")

    monkeypatch.setattr(server_module, "parse_request", broken)

    answer = _call({"op": "stop", "token": served.token, "id": "t9", "owner": "w1"})

    assert answer == [{"error": "launcher internal error; see docker compose logs launcher"}]
    assert "unsupported operand" in caplog.text


def test_an_engine_error_never_carries_a_host_path_to_the_caller(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every error line passes through the launcher's redaction."""
    served.launcher.settings = settings(Path("."), ingestion_roots={"data": "/srv/data"})

    def broken(request: object, emit: object) -> dict[str, object]:
        raise EngineError("cannot mount /srv/data")

    monkeypatch.setattr(served.launcher, "handle", broken)

    answer = _call({"op": "cleanup", "token": served.token, "owner": "w1"})

    assert answer == [{"error": "cannot mount ingestion root 'data'"}]


@pytest.fixture
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Launcher:
    """Start a launcher whose socket directory looks like the image's: sticky, set-group-id, root.

    Parameters
    ----------
    tmp_path
        The directory (it becomes the working directory).
    monkeypatch
        Makes the current user stand in for root.

    Returns
    -------
    Launcher
        A started launcher with its socket in that directory.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server_module, "_ROOT_UID", os.getuid())
    tmp_path.chmod(0o3770)
    launcher = Launcher(settings(tmp_path), FakeEngine())
    launcher.start()
    return launcher


def test_serve_listens_with_the_group_mode_and_replaces_a_stale_socket(
    shared: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket is created 0660 by the umask, never changed by path afterwards."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind("l.sock")
    seen: dict[str, object] = {}

    def listen(self: LauncherServer, poll_interval: float = 0.5) -> None:
        seen["mode"] = stat.S_IMODE(Path("l.sock").stat().st_mode)
        seen["is_socket"] = stat.S_ISSOCK(Path("l.sock").stat().st_mode)
        seen["token"] = (Path(TOKEN_FILE)).read_text(encoding="ascii")
        seen["server_token"] = self.token

    monkeypatch.setattr(LauncherServer, "serve_forever", listen)
    old = os.umask(0o022)
    try:
        serve(shared)
        assert os.umask(0o022) == 0o022  # the umask of the process is restored
    finally:
        os.umask(old)

    assert seen["mode"] == 0o660 and seen["is_socket"] is True
    assert seen["token"] == seen["server_token"]


def test_serve_creates_the_socket_when_there_is_none_to_replace(
    shared: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first start of a fresh volume."""
    seen: list[bool] = []
    monkeypatch.setattr(
        LauncherServer,
        "serve_forever",
        lambda self, poll_interval=0.5: seen.append(stat.S_ISSOCK(Path("l.sock").stat().st_mode)),
    )

    serve(shared)

    assert seen == [True]


def test_serve_names_a_socket_directory_that_is_missing(shared: Launcher) -> None:
    """The volume was not mounted: the message says so."""
    lost = Launcher(settings(Path("."), socket=Path("nowhere/l.sock")), FakeEngine())

    with pytest.raises(StartError, match="cannot read the launcher's socket directory"):
        serve(lost)


def test_serve_refuses_something_that_is_not_a_socket_at_the_socket_path(
    shared: Launcher,
) -> None:
    """A file or a directory planted there would make `bind` fail with a traceback."""
    Path("l.sock").write_text("planted", encoding="utf-8")
    with pytest.raises(StartError, match="is not a socket"):
        serve(shared)
    Path("l.sock").unlink()
    Path("l.sock").mkdir()
    with pytest.raises(StartError, match="is not a socket"):
        serve(shared)


def test_a_run_naming_an_unusable_root_is_refused_with_the_reason(served: Served) -> None:
    """The caller gets a `refused` line; nothing else is affected."""
    served.launcher.unusable_roots["data"] = "ingestion root 'data' holds a socket"
    served.launcher.settings = settings(Path("."), ingestion_roots={"data": "/srv/data"})

    answer = _call({**RUN, "token": served.token, "roots": ["data"]})

    assert answer == [{"refused": "ingestion root 'data' holds a socket"}]


def test_a_slot_is_given_back_when_its_thread_cannot_start(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed `Thread.start` must not use up one of the 128 slots for ever."""

    def fail(self: object, request: object, client_address: object) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(server_module.socketserver.ThreadingMixIn, "process_request", fail)
    before = served.server.slots._value
    ours, theirs = socket.socketpair()

    with pytest.raises(RuntimeError, match="start new thread"):
        served.server.process_request(ours, None)

    assert served.server.slots._value == before
    ours.close()
    theirs.close()


@pytest.mark.parametrize(
    ("mode", "owner_is_root", "text"),
    [
        (0o3770, False, "must belong to root"),
        (0o2770, True, "sticky bit"),
        (0o1770, True, "set-group-id bit"),
        (0o3777, True, "must not be writable by everyone"),
    ],
)
def test_serve_refuses_a_directory_that_lost_the_image_protections(
    shared: Launcher, monkeypatch: pytest.MonkeyPatch, mode: int, owner_is_root: bool, text: str
) -> None:
    """A bind mount or a volume from an older image would silently lose the sticky protection."""
    Path(".").chmod(mode)
    if not owner_is_root:
        monkeypatch.setattr(server_module, "_ROOT_UID", os.getuid() + 1)

    with pytest.raises(StartError, match=text) as caught:
        serve(shared)

    assert "Mount the volume at /run/lakana-launcher in both services" in str(caught.value)
    assert "docker compose down" in str(caught.value)
    assert "docker volume rm <project>_launcher-socket" in str(caught.value)


def test_sigterm_removes_the_containers_of_live_runs_and_ends_the_server(
    shared: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping the launcher service must not leave tasks running untimed."""
    engine = shared.engine
    assert isinstance(engine, FakeEngine)
    engine.hold = True
    handlers: dict[int, object] = {}
    monkeypatch.setattr(
        server_module.signal,
        "signal",
        lambda number, handler: handlers.__setitem__(number, handler),
    )
    volume = environment_volume(shared.settings, "3.13", "a" * 64)
    shared._mark_complete(volume)
    engine.volumes.add(volume)
    thread = threading.Thread(target=lambda: serve(shared), daemon=True)
    thread.start()
    _wait_for(lambda: Path("l.sock").exists() and signal.SIGTERM in handlers)
    token = Path(TOKEN_FILE).read_text(encoding="ascii")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect("l.sock")
    client.sendall(json.dumps({**RUN, "token": token}).encode() + b"\n")
    _wait_for(lambda: sum(path.endswith("/start") for _, path, _ in engine.calls) >= 2)

    handlers[signal.SIGTERM](signal.SIGTERM, None)  # ty: ignore[call-non-callable]
    thread.join(10)

    assert not thread.is_alive()
    assert engine.containers == {} and engine.networks == {}
    assert signal.SIGINT in handlers
    client.close()


def test_a_caller_that_stops_reading_loses_its_run_after_the_send_timeout(
    served: Served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full socket buffer must not pin a slot, a container and a network for ever."""
    monkeypatch.setattr(server_module, "_SEND_TIMEOUT", 0.3)
    served.engine.hold = False
    served.engine.output = [b"x" * 60000] * 400
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    client.connect("l.sock")
    client.sendall(json.dumps({**RUN, "token": served.token}).encode() + b"\n")

    _wait_for(lambda: not served.engine.containers and not served.engine.networks, seconds=20)
    client.close()


def test_a_connection_that_sends_nothing_gets_no_answer_and_no_log_line(
    served: Served, caplog: pytest.LogCaptureFixture
) -> None:
    """The bundle's healthcheck connects and closes every 5 s: that is no refused request."""
    caplog.set_level(logging.DEBUG, logger="lakana_worker.launcher")

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect("l.sock")
        client.shutdown(socket.SHUT_WR)
        data = client.recv(65536)  # the server closes once its handler has returned

    assert data == b""
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert _call({"op": "stop", "token": served.token, "id": "t9", "owner": "w1"}) == [
        {"result": {"stopped": False}}
    ]


def test_a_stale_socket_is_removed_before_the_launcher_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket left in the volume by an earlier launcher must not answer a healthcheck.

    Parameters
    ----------
    tmp_path
        The socket directory.
    monkeypatch
        Makes the current user stand in for root.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server_module, "_ROOT_UID", os.getuid())
    tmp_path.chmod(0o3770)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind("l.sock")

    remove_stale_socket(tmp_path / "l.sock")
    remove_stale_socket(tmp_path / "l.sock")  # nothing there: nothing to do

    assert not os.path.lexists(tmp_path / "l.sock")


def test_removing_a_stale_socket_refuses_a_planted_file_and_a_wrong_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a socket is removed, and only in a directory as the image prepares it.

    Parameters
    ----------
    tmp_path
        The socket directory.
    monkeypatch
        Makes the current user stand in for root, then not.
    """
    monkeypatch.setattr(server_module, "_ROOT_UID", os.getuid())
    tmp_path.chmod(0o3770)
    (tmp_path / "l.sock").write_text("planted", encoding="ascii")

    with pytest.raises(StartError, match="is not a socket"):
        remove_stale_socket(tmp_path / "l.sock")
    assert (tmp_path / "l.sock").read_text(encoding="ascii") == "planted"

    monkeypatch.setattr(server_module, "_ROOT_UID", os.getuid() + 1)
    with pytest.raises(StartError, match="must belong to root"):
        remove_stale_socket(tmp_path / "l.sock")
