"""`LauncherClient`: the token, the answer lines, and what each failure becomes."""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess  # nosec B404 -- runs this interpreter as a second worker process
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from lakana_worker import launcher_client
from lakana_worker.launcher_client import (
    LauncherClient,
    LauncherRefused,
    LauncherTimedOut,
    LauncherUnavailable,
    LogSinkFailed,
    claim_owner,
    launcher_owner,
    unavailable_message,
)


@dataclass
class Answering:
    """A one-connection-at-a-time launcher stand-in.

    Parameters
    ----------
    answers
        The bytes to send on the next connection, in order.
    received
        The request line of each connection.
    """

    answers: list[bytes] = field(default_factory=list)
    received: list[bytes] = field(default_factory=list)


@pytest.fixture
def answering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Answering]:
    """Serve one scripted answer per connection on a short relative socket path.

    Parameters
    ----------
    tmp_path
        The working directory.
    monkeypatch
        Changes it.

    Yields
    ------
    Answering
        Append answers; read the request lines.
    """
    monkeypatch.chdir(tmp_path)
    Path("launcher.token").write_text("t" * 64)
    state = Answering()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind("l.sock")
    server.listen(4)

    def serve() -> None:
        """Answer connections until the socket closes."""
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection, connection.makefile("rb") as reader:
                state.received.append(reader.readline())
                connection.sendall(state.answers.pop(0))

    threading.Thread(target=serve, daemon=True).start()
    yield state
    server.close()


def test_the_client_sends_the_token_streams_logs_and_returns_the_result(
    answering: Answering,
) -> None:
    """Logs go to the sink as bytes; the result line ends the call."""
    answering.answers.append(
        json.dumps({"log": base64.b64encode(b"hi\n").decode()}).encode()
        + b"\n"
        + json.dumps({"result": {"complete": True}}).encode()
        + b"\n"
    )
    logs: list[bytes] = []

    result = LauncherClient(Path("l.sock")).call({"op": "status"}, logs.append)

    assert result == {"complete": True} and logs == [b"hi\n"]
    sent = json.loads(answering.received[0])
    assert sent == {"op": "status", "token": "t" * 64}


def test_logs_without_a_sink_are_dropped(answering: Answering) -> None:
    """`on_log=None` reads past the logs to the result."""
    answering.answers.append(
        json.dumps({"log": base64.b64encode(b"x").decode()}).encode()
        + b'\n{"result": {"removed": 2}}\n'
    )

    assert LauncherClient(Path("l.sock")).cleanup("worker-1") == 2
    assert json.loads(answering.received[0]) == {
        "op": "cleanup",
        "owner": "worker-1",
        "token": "t" * 64,
    }


def test_a_refusal_and_an_error_become_their_exceptions(answering: Answering) -> None:
    """`refused` is the launcher's verdict; `error` is Docker failing: runner unavailable."""
    answering.answers.append(
        b'{"refused": "op must be one of build, cleanup, run, status, stop"}\n'
    )
    answering.answers.append(b'{"error": "the server service of this stack is not running"}\n')
    client = LauncherClient(Path("l.sock"))

    with pytest.raises(LauncherRefused, match="op must be one of"):
        client.call({"op": "x"})
    with pytest.raises(LauncherUnavailable) as caught:
        client.call({"op": "run"})
    assert str(caught.value) == unavailable_message(
        "Docker failed: the server service of this stack is not running", Path("l.sock")
    )


@pytest.mark.parametrize(
    "answer",
    [
        b"not json\n",
        b"[1]\n",
        b'{"log": "%%%"}\n',
        b'{"result": 3}\n',
        b'{"other": 1}\n',
        b"[" * 100_000 + b"\n",
    ],
    ids=["text", "list", "bad-base64", "result-not-object", "unknown", "deep-nesting"],
)
def test_an_answer_outside_the_protocol_is_runner_unavailable(
    answering: Answering, answer: bytes
) -> None:
    """Nothing the launcher sends escapes as another exception, deep nesting included.

    Parameters
    ----------
    answering
        The stand-in.
    answer
        What it sends.
    """
    answering.answers.append(answer)

    with pytest.raises(LauncherUnavailable, match="not its protocol"):
        LauncherClient(Path("l.sock")).call({"op": "status"})


def test_a_connection_closed_without_a_result_is_runner_unavailable(
    answering: Answering,
) -> None:
    """The launcher went away in the middle of the call."""
    answering.answers.append(b"")

    with pytest.raises(LauncherUnavailable, match="closed the connection without an answer"):
        LauncherClient(Path("l.sock")).call({"op": "status"})


def test_a_missing_socket_is_runner_unavailable_naming_the_service_and_the_setting(
    tmp_path: Path,
) -> None:
    """The spec's message: the launcher service and LAKANA_RUN__LAUNCHER_SOCKET."""
    path = tmp_path / "nowhere" / "launcher.sock"

    with pytest.raises(LauncherUnavailable) as caught:
        LauncherClient(path).call({"op": "status"})

    text = str(caught.value)
    assert text.startswith("runner unavailable: the launcher's token cannot be read")
    assert "docker compose ps launcher" in text and f"LAKANA_RUN__LAUNCHER_SOCKET ({path})" in text


def test_a_token_without_a_listening_socket_is_runner_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token is there, the launcher is not."""
    monkeypatch.chdir(tmp_path)
    Path("launcher.token").write_text("t" * 64)

    with pytest.raises(LauncherUnavailable, match="socket cannot be reached"):
        LauncherClient(Path("l.sock")).call({"op": "status"})


@pytest.mark.parametrize("content", [b"t" * 5000, b"\xff" * 64], ids=["huge", "not-ascii"])
def test_a_token_file_that_is_not_a_token_is_runner_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    """The file is read bounded, and only as ASCII.

    Parameters
    ----------
    tmp_path
        The working directory.
    monkeypatch
        Changes it.
    content
        What the token file holds.
    """
    monkeypatch.chdir(tmp_path)
    Path("launcher.token").write_bytes(content)

    with pytest.raises(LauncherUnavailable, match=r"token cannot be read \(not the launcher's"):
        LauncherClient(Path("l.sock")).call({"op": "status"})


def test_a_sink_that_raises_is_reported_as_the_sink_failing(answering: Answering) -> None:
    """Not as the launcher being unavailable: the runner must stop the container."""
    answering.answers.append(json.dumps({"log": base64.b64encode(b"x").decode()}).encode() + b"\n")

    def broken(chunk: bytes) -> None:
        """Fail like a full disk.

        Parameters
        ----------
        chunk
            The output.

        Raises
        ------
        OSError
            Always.
        """
        raise OSError("disk full")

    with pytest.raises(LogSinkFailed):
        LauncherClient(Path("l.sock")).call({"op": "run"}, broken)


def test_stop_and_status_read_their_answers(answering: Answering) -> None:
    """Anything but `true` is `False`."""
    answering.answers += [
        b'{"result": {"stopped": true}}\n',
        b'{"result": {"stopped": 1}}\n',
        b'{"result": {"complete": true}}\n',
        b'{"result": {"removed": "2"}}\n',
    ]
    client = LauncherClient(Path("l.sock"))

    assert client.stop("abc", "worker-1") is True
    assert client.stop("abc", "worker-1") is False
    assert client.status("3.13", "e" * 64) is True
    assert client.cleanup("worker-1") == 0
    assert json.loads(answering.received[0]) == {
        "op": "stop",
        "id": "abc",
        "owner": "worker-1",
        "token": "t" * 64,
    }
    assert json.loads(answering.received[2]) == {
        "op": "status",
        "python": "3.13",
        "environment": "e" * 64,
        "token": "t" * 64,
    }


@pytest.mark.parametrize(
    ("host", "owner"),
    [
        ("worker-1", "worker-1"),
        ("Box.local", "Box.local"),
        ("a b/c", "a-b-c"),
        ("-x", "w-x"),
        ("", "worker"),
        ("x" * 300, "x" * 127),
    ],
)
def test_the_owner_is_the_host_name_made_safe(host: str, owner: str) -> None:
    """The host part of the owner passes the launcher's rule.

    Parameters
    ----------
    host
        The host name.
    owner
        The owner the launcher gets.
    """
    assert launcher_owner(host) == owner


@pytest.mark.parametrize(
    "reason",
    [
        "the launcher is busy: too many connections at once; retry, or lower the number of workers",
        "no request line arrived in time; send the request at once",
    ],
)
def test_a_refusal_that_a_retry_can_cure_is_runner_unavailable(
    answering: Answering, reason: str
) -> None:
    """A busy launcher or a late request line is transient, not the request being wrong.

    Parameters
    ----------
    answering
        The stand-in.
    reason
        The launcher's refusal.
    """
    answering.answers.append(json.dumps({"refused": reason}).encode() + b"\n")

    with pytest.raises(LauncherUnavailable) as caught:
        LauncherClient(Path("l.sock")).call({"op": "status"})
    assert str(caught.value) == f"runner unavailable: {reason}"
    assert not isinstance(caught.value, LauncherRefused)


def test_a_log_line_over_one_mebibyte_is_read(answering: Answering) -> None:
    """The launcher sends pieces of up to 1 MiB raw, about 1.4 MB as base64 JSON."""
    piece = b"x" * (1 << 20)
    answering.answers.append(
        json.dumps({"log": base64.b64encode(piece).decode()}).encode()
        + b'\n{"result": {"complete": true}}\n'
    )
    logs: list[bytes] = []

    assert LauncherClient(Path("l.sock")).call({"op": "run"}, logs.append) == {"complete": True}
    assert logs == [piece]


def _silent_launcher(path: str, *, trickle: bool) -> tuple[threading.Event, socket.socket]:
    """Accept one connection and never answer it with a result.

    Parameters
    ----------
    path
        The socket path.
    trickle
        Send log lines without pause meanwhile, as a task that prints forever would.

    Returns
    -------
    tuple
        Set once the caller closed its connection; the listening socket, to close.
    """
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    closed = threading.Event()
    line = json.dumps({"log": base64.b64encode(b".").decode()}).encode() + b"\n"

    def serve() -> None:
        """Hold the connection open until the caller closes it."""
        connection, _ = server.accept()
        with connection:
            connection.settimeout(0.001 if trickle else 0.05)
            while True:
                try:
                    if not connection.recv(4096):
                        break
                except TimeoutError:
                    pass
                except OSError:
                    break
                if trickle:
                    try:
                        connection.sendall(line)
                    except OSError:
                        break
        closed.set()

    threading.Thread(target=serve, daemon=True).start()
    return closed, server


@pytest.mark.parametrize("trickle", [False, True], ids=["silent", "logging"])
def test_a_call_past_its_deadline_closes_the_connection_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trickle: bool
) -> None:
    """The worker stops waiting; its closed connection makes the launcher remove the container.

    Parameters
    ----------
    tmp_path
        The working directory.
    monkeypatch
        Changes it.
    trickle
        Whether output keeps arriving, which must not extend the deadline.
    """
    monkeypatch.chdir(tmp_path)
    Path("launcher.token").write_text("t" * 64)
    closed, server = _silent_launcher("q.sock", trickle=trickle)
    began = time.monotonic()

    try:
        with pytest.raises(LauncherTimedOut, match=r"no result within 0\.3s"):
            LauncherClient(Path("q.sock")).call({"op": "run"}, lambda chunk: None, timeout=0.3)
        assert time.monotonic() - began < 5
        assert closed.wait(5), "the client left the connection open"
    finally:
        server.close()


def test_a_line_that_arrives_after_the_deadline_ends_the_call(
    answering: Answering, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line read after the deadline is not handled, even if the read itself did not time out.

    The test above reaches this branch only when the timing happens to fall that way; here
    the clock jumps inside the log sink, so the second line is always read past the deadline.

    Parameters
    ----------
    answering
        The launcher stand-in.
    monkeypatch
        Replaces the client's clock.
    """
    now = [0.0]
    monkeypatch.setattr(launcher_client, "time", SimpleNamespace(monotonic=lambda: now[0]))
    log = json.dumps({"log": base64.b64encode(b".").decode()}).encode() + b"\n"
    answering.answers.append(log + log + b'{"result": {"complete": true}}\n')
    logs: list[bytes] = []

    def sink(chunk: bytes) -> None:
        """Keep the chunk and move the clock past the deadline.

        Parameters
        ----------
        chunk
            The log bytes.
        """
        logs.append(chunk)
        now[0] = 1000.0

    with pytest.raises(LauncherTimedOut, match=r"no result within 60s"):
        LauncherClient(Path("l.sock")).call({"op": "run"}, sink, timeout=60)
    assert logs == [b"."]


def test_a_call_without_a_deadline_waits_as_long_as_the_task_runs(answering: Answering) -> None:
    """`timeout=None` is the default: a build or a status call has no worker deadline."""
    answering.answers.append(b'{"result": {"complete": true}}\n')

    assert LauncherClient(Path("l.sock")).call({"op": "status"}, timeout=None) == {"complete": True}


_OWNER_RULE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
"""The launcher's rule for an owner (`lakana_worker.launcher.requests._OWNER`)."""


def _claim_in_another_process(directory: Path, host: str) -> str:
    """Claim an owner the way a second worker process would.

    Parameters
    ----------
    directory
        The worker's cache directory.
    host
        Its host name.

    Returns
    -------
    str
        The owner; the process has ended, so its claim is released.
    """
    script = (
        "import sys; from pathlib import Path; "
        "from lakana_worker.launcher_client import claim_owner; "
        "print(claim_owner(Path(sys.argv[1]), sys.argv[2]))"
    )
    done = subprocess.run(  # nosec B603 -- this interpreter, fixed argv
        [sys.executable, "-c", script, str(directory), host],
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    )
    return done.stdout.strip()


def test_a_restarted_worker_gets_its_owner_back_so_its_cleanup_finds_its_leftovers(
    tmp_path: Path,
) -> None:
    """The owner is kept in the worker's cache directory; a restart reads it again."""
    first = _claim_in_another_process(tmp_path, "box-1")
    again = _claim_in_another_process(tmp_path, "box-1")

    assert first == again
    assert re.fullmatch(r"box-1\.[0-9a-f]{16}", first)
    assert (tmp_path / "launcher-owner").stat().st_mode & 0o777 == 0o600


def test_two_workers_on_one_host_name_never_share_an_owner(tmp_path: Path) -> None:
    """A cleanup at one's start must not remove the other's running tasks (Task 12 review).

    Two hosts named alike (`network_mode: host`, two installs) have their own directories;
    two live workers sharing one directory take a slot each.
    """
    own = claim_owner(tmp_path / "a", "box-1")
    other_directory = claim_owner(tmp_path / "b", "box-1")
    same_process = claim_owner(tmp_path / "a", "box-1")
    shared = _claim_in_another_process(tmp_path / "a", "box-1")

    assert len({own, other_directory, shared, same_process}) == 4
    assert _claim_in_another_process(tmp_path / "a", "box-1") == shared
    assert all(name.startswith("box-1.") for name in (own, other_directory, shared))


def test_an_owner_file_that_is_a_link_or_holds_no_token_is_not_trusted(tmp_path: Path) -> None:
    """A link is never followed (that slot is skipped); a damaged file gets a new token."""
    target = tmp_path / "elsewhere"
    target.write_text("0123456789abcdef")
    (tmp_path / "launcher-owner").symlink_to(target)
    (tmp_path / "launcher-owner.1").write_text("not a token\n")

    owner = claim_owner(tmp_path, "box-1")

    assert owner != "box-1.0123456789abcdef"
    assert (tmp_path / "launcher-owner.1").read_text() == owner.removeprefix("box-1.")
    assert target.read_text() == "0123456789abcdef"


def test_without_a_writable_directory_the_owner_is_unique_but_not_kept(tmp_path: Path) -> None:
    """A worker that cannot keep its owner still runs; only its restart cleanup finds nothing."""
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        first = claim_owner(locked, "box-1")
        second = claim_owner(locked, "box-1")
    finally:
        locked.chmod(0o700)

    assert first != second
    assert all(re.fullmatch(r"box-1\.[0-9a-f]{16}", name) for name in (first, second))
    assert list(locked.iterdir()) == []


def test_an_owner_always_passes_the_launchers_rule(tmp_path: Path) -> None:
    """A long or odd host name is cut and made safe; the token is kept whole."""
    owner = claim_owner(tmp_path, "-" + "x" * 300)

    assert _OWNER_RULE.match(owner)
    assert re.search(r"\.[0-9a-f]{16}\Z", owner)


def test_a_pipe_in_place_of_the_owner_file_is_skipped(tmp_path: Path) -> None:
    """Only a regular file is read or written."""
    os.mkfifo(tmp_path / "launcher-owner")

    owner = claim_owner(tmp_path, "box-1")

    assert (tmp_path / "launcher-owner.1").read_text() == owner.removeprefix("box-1.")


def test_with_every_owner_file_held_the_owner_is_unique_but_not_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More live workers on one directory than files: the last one gets a random owner."""
    monkeypatch.setattr("lakana_worker.launcher_client._OWNER_SLOTS", 1)
    held = claim_owner(tmp_path, "box-1")

    extra = claim_owner(tmp_path, "box-1")

    assert extra != held and re.fullmatch(r"box-1\.[0-9a-f]{16}", extra)
    assert [p.name for p in tmp_path.iterdir()] == ["launcher-owner"]


def test_an_owner_kept_from_an_earlier_process_is_read_back(tmp_path: Path) -> None:
    """The file's token is the owner; nothing rewrites it."""
    (tmp_path / "launcher-owner").write_text("0123456789abcdef\n")

    assert claim_owner(tmp_path, "box-1") == "box-1.0123456789abcdef"
    assert (tmp_path / "launcher-owner").read_text() == "0123456789abcdef\n"


def test_check_sends_the_op_and_collects_the_checks_output(answering: Answering) -> None:
    """The output comes back whole (up to 64 KiB) beside the launcher's result."""
    line = b'{"outcome": "dns", "message": "cannot resolve db.example.com"}\n'
    answering.answers.append(
        json.dumps({"log": base64.b64encode(line).decode()}).encode()
        + b"\n"
        + json.dumps({"log": base64.b64encode(b"x" * 70_000).decode()}).encode()
        + b"\n"
        + json.dumps({"result": {"exit": 0}}).encode()
        + b"\n"
    )

    result, output = LauncherClient(Path("l.sock")).check({"check": "k1", "owner": "w"})

    assert result == {"exit": 0}
    assert output.startswith(line) and len(output) == 64 * 1024
    assert json.loads(answering.received[0]) == {
        "op": "check",
        "check": "k1",
        "owner": "w",
        "token": "t" * 64,
    }
