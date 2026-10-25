"""`lakana.ingest._check`: what a connection test finds, against servers the test runs itself."""

from __future__ import annotations

import ast
import base64
import contextlib
import errno
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest
from tls_fixture import Authority, issue_leaf, make_authority, serving

from lakana.ingest import _check

SECRET = "hunter2-s3cr3t"
FAST = 1.0
"""Seconds one step may take in a test; the real default is 10."""


def connection(
    kind: str,
    address: Mapping[str, object],
    settings: Mapping[str, object] | None = None,
    *,
    secret: dict[str, str] | None = None,
    endpoints: list[tuple[str, int]] | None = None,
) -> dict[str, object]:
    """Build a `TaskConnectionOut`-shaped body.

    Parameters
    ----------
    kind
        The connection's kind.
    address
        Its address.
    settings
        Its settings.
    secret
        Its secret, or `None`.
    endpoints
        The host and port pairs it reaches.

    Returns
    -------
    dict
        The body.
    """
    return {
        "name": "crm",
        "kind": kind,
        "address": address,
        "settings": settings or {},
        "endpoints": [{"host": h, "port": p, "role": "service"} for h, p in endpoints or []],
        "secret": secret,
    }


@contextmanager
def listening(
    handler: Callable[[socket.socket], None] | None = None,
) -> Generator[int, None, None]:
    """Listen on loopback and hand each accepted socket to `handler` on a thread.

    Parameters
    ----------
    handler
        Called with the accepted socket; it closes it. `None` accepts and does nothing.

    Yields
    ------
    int
        The port.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                sock, _ = server.accept()
            except OSError:
                return
            held.append(sock)
            if handler is not None:
                threading.Thread(target=handler, args=(sock,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield server.getsockname()[1]
    finally:
        server.close()
        for sock in held:
            sock.close()


def closed_port() -> int:
    """Find a loopback port nothing listens on.

    Returns
    -------
    int
        The port.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(condition: Callable[[], bool], seconds: float = 3.0) -> None:
    """Wait until a fake server's thread has done what the test asserts on.

    Parameters
    ----------
    condition
        What to wait for.
    seconds
        The longest wait.
    """
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the variables the check reads, so a test sets only what it means to.

    Parameters
    ----------
    monkeypatch
        Pytest's patcher.
    """
    monkeypatch.delenv("LAKANA_EGRESS_PROXY", raising=False)
    monkeypatch.delenv("LAKANA_CA_BUNDLE", raising=False)


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    """Make the authority that signs the test servers.

    Parameters
    ----------
    tmp_path
        Pytest's directory.

    Returns
    -------
    Authority
        The authority.
    """
    return make_authority(tmp_path)


def tls_server(
    authority: Authority, tmp_path: Path, then: Callable[[ssl.SSLSocket], None] | None = None
) -> Callable[[socket.socket], None]:
    """Build a handler that answers PostgreSQL's ``SSLRequest`` with ``S`` and then speaks TLS.

    Parameters
    ----------
    authority
        Signs the server's certificate.
    tmp_path
        Where its files are written.
    then
        Called with the TLS socket after the handshake.

    Returns
    -------
    Callable
        The handler.
    """
    cert, key = issue_leaf(authority, tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    def handle(sock: socket.socket) -> None:
        try:
            assert sock.recv(8) == b"\x00\x00\x00\x08\x04\xd2\x16\x2f"
            sock.sendall(b"S")
            with context.wrap_socket(sock, server_side=True) as tls:
                if then:
                    then(tls)
        except (OSError, ssl.SSLError):
            pass

    return handle


def test_a_host_that_does_not_resolve_is_dns() -> None:
    body = connection("tcp", {"host": "no-such-host.invalid", "port": 5000})
    outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "dns"
    assert "no-such-host.invalid" in message


def test_a_closed_port_is_refused() -> None:
    body = connection("tcp", {"host": "127.0.0.1", "port": closed_port()})
    outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "refused"
    assert "127.0.0.1" in message


def test_a_server_that_never_answers_the_handshake_is_a_timeout(tmp_path: Path) -> None:
    with listening() as port:
        body = connection("http", {"base_url": f"https://localhost:{port}"})
        outcome, message = _check.check(body, {}, timeout=0.5)
    assert outcome == "timeout"
    assert "0.5" in message or "timed out" in message


def test_a_certificate_of_another_authority_is_tls(authority: Authority, tmp_path: Path) -> None:
    other = make_authority(tmp_path, "Other CA")
    served = tmp_path / "served"
    served.mkdir()
    with serving(served, authority, tmp_path) as server:
        port = int(server.base.rsplit(":", 1)[1])
        body = connection(
            "http",
            {"base_url": f"https://localhost:{port}"},
            {"ca_certificate": other.ca_file.read_text()},
        )
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "tls"
    assert message == "the server's certificate is not trusted for `localhost`"


def test_the_right_authority_makes_a_reachable_connection_ok(
    authority: Authority, tmp_path: Path
) -> None:
    served = tmp_path / "served"
    served.mkdir()
    with serving(served, authority, tmp_path) as server:
        port = int(server.base.rsplit(":", 1)[1])
        body = connection(
            "http",
            {"base_url": f"https://localhost:{port}/api"},
            {"ca_certificate": authority.ca_file.read_text()},
        )
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert (outcome, message.startswith("reachable")) == ("ok", True)


def test_the_name_must_match_under_verify_full(authority: Authority, tmp_path: Path) -> None:
    cert, key = issue_leaf(authority, tmp_path, ("other.example.com",))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    def handle(sock: socket.socket) -> None:
        try:
            with context.wrap_socket(sock, server_side=True):
                pass
        except (OSError, ssl.SSLError):
            pass

    with listening(handle) as port:
        body = connection(
            "http",
            {"base_url": f"https://localhost:{port}"},
            {"ca_certificate": authority.ca_file.read_text()},
        )
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert (outcome, message) == ("tls", "the server's certificate is not trusted for `localhost`")


def test_the_operators_bundle_is_trusted_for_s3(authority: Authority, tmp_path: Path) -> None:
    served = tmp_path / "served"
    served.mkdir()
    with serving(served, authority, tmp_path) as server:
        port = int(server.base.rsplit(":", 1)[1])
        body = connection(
            "s3",
            {"bucket": "b", "region": "r", "endpoint": f"https://localhost:{port}"},
            endpoints=[("localhost", port)],
        )
        assert _check.check(body, {}, timeout=FAST)[0] == "tls"
        os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
        assert _check.check(body, {}, timeout=FAST)[0] == "ok"


def test_every_endpoint_of_an_object_store_is_checked(authority: Authority, tmp_path: Path) -> None:
    served = tmp_path / "served"
    served.mkdir()
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
    with serving(served, authority, tmp_path) as server:
        port = int(server.base.rsplit(":", 1)[1])
        body = connection(
            "gcs",
            {"bucket": "b"},
            endpoints=[("localhost", port), ("127.0.0.1", closed_port())],
        )
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "refused"
    assert "127.0.0.1" in message


def test_a_proxy_that_refuses_the_pair_is_not_allowed_with_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bytes] = []
    hint = "api.example.com:443 is not a connection of this asset: declare it"

    def proxy(sock: socket.socket) -> None:
        seen.append(sock.recv(4096))
        body = f"{hint}\n".encode()
        sock.sendall(
            b"HTTP/1.1 403 Forbidden\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
            % (len(body), body)
        )
        sock.close()

    with listening(proxy) as port:
        monkeypatch.setenv("LAKANA_EGRESS_PROXY", f"http://lakana:tok3n@127.0.0.1:{port}")
        body = connection("http", {"base_url": "https://api.example.com"})
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert (outcome, message) == ("not_allowed", hint)
    request = seen[0].decode()
    assert request.startswith("CONNECT api.example.com:443 HTTP/1.1\r\n")
    token = base64.b64encode(b"lakana:tok3n").decode()
    assert f"Proxy-Authorization: Basic {token}\r\n" in request


@pytest.mark.parametrize(
    ("status", "text", "expected"),
    [
        (502, "cannot resolve api.example.com (Name or service not known)", "dns"),
        (502, "cannot reach api.example.com:443 (connection refused)", "refused"),
        (503, "the proxy is busy", "failed"),
    ],
)
def test_the_proxys_other_answers_map_to_outcomes(
    monkeypatch: pytest.MonkeyPatch, status: int, text: str, expected: str
) -> None:
    def proxy(sock: socket.socket) -> None:
        sock.recv(4096)
        body = f"{text}\n".encode()
        sock.sendall(b"HTTP/1.1 %d X\r\nContent-Length: %d\r\n\r\n%s" % (status, len(body), body))
        sock.close()

    with listening(proxy) as port:
        monkeypatch.setenv("LAKANA_EGRESS_PROXY", f"http://lakana:t@127.0.0.1:{port}")
        outcome, message = _check.check(
            connection("http", {"base_url": "https://api.example.com"}), {}, timeout=FAST
        )
    assert (outcome, message) == (expected, text)


def test_a_database_goes_through_the_relay_not_the_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKANA_EGRESS_PROXY", "http://lakana:t@127.0.0.1:1")
    body = connection("tcp", {"host": "db.internal", "port": 5432})
    # `lakana-relay` does not resolve here: the attempt was made at the relay's name.
    outcome, message = _check.check(body, {"relay_port": 15432}, timeout=FAST)
    assert outcome == "dns"
    assert "lakana-relay" in message


def test_a_message_never_holds_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    def proxy(sock: socket.socket) -> None:
        sock.recv(4096)
        body = f"refused for token {SECRET}\n".encode()
        sock.sendall(b"HTTP/1.1 403 X\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
        sock.close()

    with listening(proxy) as port:
        monkeypatch.setenv("LAKANA_EGRESS_PROXY", f"http://lakana:t@127.0.0.1:{port}")
        body = connection(
            "http",
            {"base_url": "https://api.example.com"},
            {"auth": "bearer"},
            secret={"token": SECRET},
        )
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "not_allowed"
    assert SECRET not in message
    assert message == "refused for token ***"


def test_a_kind_with_a_secret_says_credentials_are_not_checked_yet(
    authority: Authority, tmp_path: Path
) -> None:
    served = tmp_path / "served"
    served.mkdir()
    with serving(served, authority, tmp_path) as server:
        port = int(server.base.rsplit(":", 1)[1])
        body = connection(
            "http",
            {"base_url": f"https://localhost:{port}"},
            {"auth": "bearer", "ca_certificate": authority.ca_file.read_text()},
            secret={"token": SECRET},
        )
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "ok"
    assert message.endswith("; credentials not checked yet")
    assert SECRET not in message


def test_a_directory_root_that_is_missing_is_root_unavailable(tmp_path: Path) -> None:
    body = connection("directory", {"root": "shared", "path": "in"})
    routing = {"kind": "directory", "root": "shared", "root_dir": str(tmp_path / "gone")}
    outcome, message = _check.check(body, {**routing, "path": "in"}, timeout=FAST)
    assert outcome == "root_unavailable"
    assert message.startswith(
        "`crm` names ingestion root `shared`, which this worker does not have"
    )
    assert "LAKANA_RUN__INGESTION_ROOTS" in message


def test_a_missing_path_below_a_present_root_is_not_found(tmp_path: Path) -> None:
    body = connection("directory", {"root": "shared", "path": "in"})
    routing = {"root": "shared", "root_dir": str(tmp_path), "path": "in"}
    outcome, message = _check.check(body, routing, timeout=FAST)
    assert (outcome, message) == ("not_found", "the path `in` does not exist under root `shared`")


def test_a_readable_directory_is_ok(tmp_path: Path) -> None:
    (tmp_path / "in" / "deep").mkdir(parents=True)
    (tmp_path / "in" / "deep" / "a.csv").write_text("x")
    body = connection("directory", {"root": "shared", "path": "in/deep"})
    routing = {"root": "shared", "root_dir": str(tmp_path), "path": "in/deep"}
    outcome, message = _check.check(body, routing, timeout=FAST)
    assert outcome == "ok"
    assert "1 entry" in message or "1 entries" in message


def test_a_symbolic_link_component_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "in").symlink_to(tmp_path / "real")
    body = connection("directory", {"root": "shared", "path": "in"})
    routing = {"root": "shared", "root_dir": str(tmp_path), "path": "in"}
    outcome, message = _check.check(body, routing, timeout=FAST)
    assert outcome == "failed"
    assert "symbolic link" in message


def test_a_symbolic_link_as_the_root_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "root").symlink_to(tmp_path / "real")
    body = connection("directory", {"root": "shared", "path": ""})
    routing = {"root": "shared", "root_dir": str(tmp_path / "root"), "path": ""}
    assert _check.check(body, routing, timeout=FAST)[0] == "failed"


def test_a_sqlite_file_must_be_a_regular_file(tmp_path: Path) -> None:
    (tmp_path / "data.db").write_bytes(b"x")
    (tmp_path / "dir.db").mkdir()
    body = connection("sqlite-file", {"root": "shared", "path": "data.db"})
    ok = _check.check(
        body, {"root": "shared", "root_dir": str(tmp_path), "path": "data.db"}, timeout=FAST
    )
    assert ok[0] == "ok"
    as_dir = _check.check(
        body, {"root": "shared", "root_dir": str(tmp_path), "path": "dir.db"}, timeout=FAST
    )
    assert as_dir[0] == "failed"
    assert "not a regular file" in as_dir[1]
    gone = _check.check(
        body, {"root": "shared", "root_dir": str(tmp_path), "path": "no.db"}, timeout=FAST
    )
    assert gone == ("not_found", "the path `no.db` does not exist under root `shared`")


def test_postgresql_asks_for_tls_and_verifies_it(authority: Authority, tmp_path: Path) -> None:
    with listening(tls_server(authority, tmp_path)) as port:
        address = {"host": "localhost", "port": port, "database": "d"}
        body = connection(
            "postgresql",
            address,
            {"user": "u", "tls": "verify-full", "ca_certificate": authority.ca_file.read_text()},
        )
        assert _check.check(body, {}, timeout=FAST)[0] == "ok"
        wrong = make_authority(tmp_path, "Other CA")
        body["settings"] = {"user": "u", "ca_certificate": wrong.ca_file.read_text()}  # type: ignore[assignment]
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "tls"
    assert message == "the server's certificate is not trusted for `localhost`"


def test_postgresql_require_accepts_a_certificate_nobody_trusts(
    authority: Authority, tmp_path: Path
) -> None:
    with listening(tls_server(authority, tmp_path)) as port:
        body = connection(
            "postgresql",
            {"host": "localhost", "port": port, "database": "d"},
            {"user": "u", "tls": "require"},
        )
        assert _check.check(body, {}, timeout=FAST)[0] == "ok"


def test_postgresql_without_tls_on_the_server_is_a_tls_failure_unless_disabled() -> None:
    received: list[bytes] = []

    def refuses_tls(sock: socket.socket) -> None:
        received.append(sock.recv(8))
        sock.sendall(b"N")
        sock.close()

    with listening(refuses_tls) as port:
        address = {"host": "127.0.0.1", "port": port, "database": "d"}
        body = connection("postgresql", address, {"user": "u", "tls": "verify-full"})
        outcome, message = _check.check(body, {}, timeout=FAST)
        assert outcome == "tls"
        assert "does not offer TLS" in message
        body["settings"] = {"user": "u", "tls": "disable"}  # type: ignore[assignment]
        assert _check.check(body, {}, timeout=FAST)[0] == "ok"
        wait_for(lambda: len(received) == 2)
    assert received == [b"\x00\x00\x00\x08\x04\xd2\x16\x2f", b""]


def test_mariadb_reads_the_servers_greeting() -> None:
    def greets(sock: socket.socket) -> None:
        payload = b"\x0a11.4.2-MariaDB\x00" + b"\x00" * 20
        sock.sendall(len(payload).to_bytes(3, "little") + b"\x00" + payload)
        sock.close()

    def not_mysql(sock: socket.socket) -> None:
        sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        sock.close()

    address = {"database": "d", "host": "127.0.0.1"}
    settings = {"user": "u"}
    with listening(greets) as port:
        body = connection("mariadb", {**address, "port": port}, settings)
        assert _check.check(body, {}, timeout=FAST)[0] == "ok"
    with listening(not_mysql) as port:
        body = connection("mariadb", {**address, "port": port}, settings)
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "failed"
    assert "MariaDB or MySQL" in message


def test_tcp_connects_and_sends_nothing() -> None:
    received: list[bytes] = []

    def quiet(sock: socket.socket) -> None:
        sock.settimeout(0.3)
        try:
            received.append(sock.recv(10))
        except TimeoutError:
            received.append(b"")
        sock.close()

    with listening(quiet) as port:
        body = connection("tcp", {"host": "127.0.0.1", "port": port}, secret={"password": SECRET})
        outcome, message = _check.check(body, {}, timeout=FAST)
        wait_for(lambda: len(received) == 1)
    assert outcome == "ok"
    assert message == (
        "reachable; Lakana does not check the protocol, TLS or credentials of a tcp connection"
    )
    assert received == [b""]


def test_the_module_runs_as_a_program_over_stdin_and_stdout() -> None:
    with listening() as port:
        body = connection("tcp", {"host": "127.0.0.1", "port": port}, secret={"password": SECRET})
        done = subprocess.run(
            [sys.executable, "-I", "-m", "lakana.ingest._check"],
            input=json.dumps({"connection": body, "routing": {}}),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    assert done.returncode == 0
    lines = done.stdout.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["outcome"] == "ok"
    assert SECRET not in done.stdout + done.stderr


def test_unreadable_input_is_a_failed_outcome_not_a_crash() -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-m", "lakana.ingest._check"],
        input="not json",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0
    assert json.loads(done.stdout)["outcome"] == "failed"


def test_a_missing_trust_bundle_is_named_not_blamed_on_the_host(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = tmp_path / "served"
    served.mkdir()
    gone = tmp_path / "gone.pem"
    monkeypatch.setenv("LAKANA_CA_BUNDLE", str(gone))
    with serving(served, authority, tmp_path) as server:
        port = int(server.base.rsplit(":", 1)[1])
        body = connection("http", {"base_url": f"https://localhost:{port}"})
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "failed"
    assert message == (
        f"the trust bundle `{gone}` (LAKANA_CA_BUNDLE) cannot be read; check that the file "
        "exists on this worker"
    )


def test_an_oauth2_token_host_that_is_down_makes_the_test_fail_naming_it(
    authority: Authority, tmp_path: Path
) -> None:
    served = tmp_path / "served"
    served.mkdir()
    dead = closed_port()
    with serving(served, authority, tmp_path) as server:
        port = int(server.base.rsplit(":", 1)[1])
        body = connection(
            "http",
            {"base_url": f"https://localhost:{port}"},
            {
                "auth": "oauth2",
                "client_id": "c",
                "token_url": f"https://127.0.0.1:{dead}/token",
                "ca_certificate": authority.ca_file.read_text(),
            },
            endpoints=[("localhost", port), ("127.0.0.1", dead)],
        )
        outcome, message = _check.check(body, {}, timeout=FAST)
        assert (outcome, f"127.0.0.1:{dead}" in message) == ("refused", True)
        body["endpoints"] = [{"host": "localhost", "port": port, "role": "service"}]
        assert _check.check(body, {}, timeout=FAST)[0] == "ok"


def proxy_says(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[socket.socket], None]
) -> contextlib.AbstractContextManager[int]:
    """Point the egress proxy variable at a fake proxy that runs `handler`.

    Parameters
    ----------
    monkeypatch
        Pytest's patcher.
    handler
        What the fake does with the connection.

    Returns
    -------
    contextlib.AbstractContextManager
        The listening fake; yields its port.
    """

    @contextmanager
    def run() -> Generator[int, None, None]:
        with listening(handler) as port:
            monkeypatch.setenv("LAKANA_EGRESS_PROXY", f"http://lakana:t@127.0.0.1:{port}")
            yield port

    return run()


API = connection("http", {"base_url": "https://api.example.com"})


def test_a_proxy_that_hangs_up_or_is_no_proxy_is_a_failed_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hangs_up(sock: socket.socket) -> None:
        sock.recv(4096)
        sock.close()

    def talks_nonsense(sock: socket.socket) -> None:
        sock.recv(4096)
        sock.sendall(b"hello\r\n\r\n")
        sock.close()

    for handler in (hangs_up, talks_nonsense):
        with proxy_says(monkeypatch, handler):
            outcome, message = _check.check(API, {}, timeout=FAST)
        assert (outcome, message) == (
            "failed",
            "the egress proxy did not answer like an HTTP proxy",
        )


def test_a_proxy_that_never_answers_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def silent(sock: socket.socket) -> None:
        sock.recv(4096)

    with proxy_says(monkeypatch, silent):
        outcome, _ = _check.check(API, {}, timeout=0.3)
    assert outcome == "timeout"


def test_a_proxys_reason_that_arrives_in_pieces_is_read_to_its_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(sock: socket.socket) -> None:
        sock.recv(4096)
        sock.sendall(b"HTTP/1.1 403 X\r\nX-Other: 1\r\nContent-Length: 10\r\n\r\nnot ")
        time.sleep(0.1)
        sock.sendall(b"yours\n")
        sock.close()

    def short(sock: socket.socket) -> None:
        sock.recv(4096)
        sock.sendall(b"HTTP/1.1 503 X\r\nContent-Length: 50\r\n\r\n")
        sock.close()

    with proxy_says(monkeypatch, slow):
        assert _check.check(API, {}, timeout=FAST) == ("not_allowed", "not yours")
    with proxy_says(monkeypatch, short):
        assert _check.check(API, {}, timeout=FAST) == ("failed", "HTTP/1.1 503 X")


def test_a_proxy_that_cannot_be_reached_is_named_as_the_host_that_refused() -> None:
    os.environ["LAKANA_EGRESS_PROXY"] = f"http://lakana:t@127.0.0.1:{closed_port()}"
    outcome, message = _check.check(API, {}, timeout=FAST)
    assert (outcome, "127.0.0.1" in message) == ("refused", True)


def test_other_network_errors_are_failures_that_name_the_address() -> None:
    found = _check._network_failure(OSError(errno.ENETUNREACH, "Network is unreachable"), "h", 1, 1)
    assert (found.outcome, found.message) == (
        "failed",
        "could not reach `h:1` (Network is unreachable)",
    )


def test_a_server_that_hangs_up_during_the_handshake_is_a_failed_handshake() -> None:
    def not_tls(sock: socket.socket) -> None:
        sock.recv(1)
        sock.sendall(b"HTTP/1.0 400 Bad Request\r\n\r\n")
        sock.close()

    with listening(not_tls) as port:
        body = connection("http", {"base_url": f"https://localhost:{port}"})
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "failed"
    assert message.startswith(f"the TLS handshake with `localhost:{port}` failed")


def test_tls_disabled_changes_nothing() -> None:
    sock = socket.socket()
    assert _check._tls(sock, "h", None, "disable") is sock
    sock.close()


def test_a_server_that_answers_the_tls_request_with_something_else_is_not_postgresql() -> None:
    def odd(sock: socket.socket) -> None:
        sock.recv(8)
        sock.sendall(b"X")
        sock.close()

    with listening(odd) as port:
        body = connection(
            "postgresql", {"host": "127.0.0.1", "port": port, "database": "d"}, {"user": "u"}
        )
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "failed"
    assert "like a PostgreSQL server" in message


def test_a_mariadb_server_that_hangs_up_or_is_down_is_named() -> None:
    address = {"host": "127.0.0.1", "database": "d"}

    def silent(sock: socket.socket) -> None:
        time.sleep(1)

    with listening(silent) as port:
        body = connection("mariadb", {**address, "port": port}, {"user": "u"})
        assert _check.check(body, {}, timeout=0.2)[0] == "timeout"
    with listening(lambda sock: sock.close()) as port:
        body = connection("mariadb", {**address, "port": port}, {"user": "u"})
        assert _check.check(body, {}, timeout=FAST)[0] == "failed"
    body = connection("mariadb", {**address, "port": closed_port()}, {"user": "u"})
    assert _check.check(body, {}, timeout=FAST)[0] == "refused"


def test_a_tcp_host_that_does_not_resolve_is_dns() -> None:
    body = connection("tcp", {"host": "no-such-host.invalid", "port": 1})
    assert _check.check(body, {}, timeout=FAST)[0] == "dns"


def test_a_path_through_a_file_or_a_directory_nobody_may_read_is_failed(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    body = connection("directory", {"root": "shared", "path": "file/deeper"})
    routing = {"root": "shared", "root_dir": str(tmp_path), "path": "file/deeper"}
    outcome, message = _check.check(body, routing, timeout=FAST)
    assert (outcome, message) == ("failed", "`file` is not a directory")
    if os.geteuid() != 0:
        (tmp_path / "closed").mkdir(mode=0o000)
        try:
            routing = {"root": "shared", "root_dir": str(tmp_path), "path": "closed"}
            outcome, message = _check.check(body, routing, timeout=FAST)
            assert (outcome, message.startswith("this worker may not read `closed`")) == (
                "failed",
                True,
            )
            routing = {"root": "shared", "root_dir": str(tmp_path / "closed"), "path": ""}
            outcome, message = _check.check(body, routing, timeout=FAST)
            assert (outcome, "could not be opened" in message) == ("failed", True)
        finally:
            (tmp_path / "closed").chmod(0o700)


def test_an_unexpected_open_error_is_named() -> None:
    found = _check._component_failure(
        os.open("/", os.O_RDONLY), "x", OSError(errno.EIO, "I/O error"), "shared", "x"
    )
    assert (found.outcome, found.message) == ("failed", "`x` could not be opened (I/O error)")


def test_a_routing_without_a_root_directory_is_root_unavailable() -> None:
    body = connection("directory", {"root": "shared", "path": "in"})
    assert _check.check(body, {}, timeout=FAST)[0] == "root_unavailable"


def test_dot_dot_in_a_path_is_refused(tmp_path: Path) -> None:
    body = connection("directory", {"root": "shared", "path": "a/../b"})
    routing = {"root": "shared", "root_dir": str(tmp_path), "path": "a/../b"}
    (tmp_path / "a").mkdir()
    assert _check.check(body, routing, timeout=FAST) == ("failed", "`..` is not allowed in a path")


def test_a_directory_that_cannot_be_listed_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(fd: int) -> list[str]:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(_check.os, "listdir", broken)
    body = connection("directory", {"root": "shared", "path": ""})
    routing = {"root": "shared", "root_dir": str(tmp_path), "path": ""}
    assert _check.check(body, routing, timeout=FAST) == (
        "failed",
        "`` cannot be listed (I/O error)",
    )


def test_an_empty_path_is_not_a_file(tmp_path: Path) -> None:
    body = connection("sqlite-file", {"root": "shared", "path": ""})
    routing = {"root": "shared", "root_dir": str(tmp_path), "path": ""}
    assert _check.check(body, routing, timeout=FAST)[0] == "failed"


def test_an_unknown_kind_and_a_broken_body_are_failed_tests() -> None:
    unknown = connection("ftp", {})
    assert _check.check(unknown, {}, timeout=FAST) == (
        "failed",
        "this check does not know the kind `ftp`; upgrade lakana",
    )
    outcome, message = _check.check({"kind": "tcp", "name": "x"}, {}, timeout=FAST)
    assert (outcome, message) == ("failed", "the check failed unexpectedly (KeyError)")


def test_main_reads_stdin_and_writes_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with listening() as port:
        body = connection("tcp", {"host": "127.0.0.1", "port": port})
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"connection": body})))
        _check.main()
    out = capsys.readouterr().out
    assert json.loads(out)["outcome"] == "ok"
    assert out.count("\n") == 1


def test_main_survives_unreadable_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{"))
    _check.main()
    assert json.loads(capsys.readouterr().out) == {
        "outcome": "failed",
        "message": "the check could not read its input; upgrade lakana",
    }


def test_a_proxy_that_opens_the_tunnel_carries_the_handshake(
    monkeypatch: pytest.MonkeyPatch, authority: Authority, tmp_path: Path
) -> None:
    cert, key = issue_leaf(authority, tmp_path, ("api.example.com",))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    def tunnel(sock: socket.socket) -> None:
        sock.recv(4096)
        sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        try:
            with context.wrap_socket(sock, server_side=True):
                pass
        except (OSError, ssl.SSLError):
            pass

    body = connection(
        "http",
        {"base_url": "https://api.example.com"},
        {"ca_certificate": authority.ca_file.read_text()},
    )
    with proxy_says(monkeypatch, tunnel):
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert (outcome, message) == (
        "ok",
        "reachable: `api.example.com:443`, TLS verified",
    )


def test_a_server_that_speaks_only_tls_1_1_is_a_tls_failure_naming_the_floor(
    authority: Authority, tmp_path: Path
) -> None:
    cert, key = issue_leaf(authority, tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with pytest.warns(DeprecationWarning):
        context.minimum_version = ssl.TLSVersion.TLSv1
        context.maximum_version = ssl.TLSVersion.TLSv1_1
    context.set_ciphers("ALL:@SECLEVEL=0")
    context.load_cert_chain(cert, key)

    def old(sock: socket.socket) -> None:
        with contextlib.suppress(OSError, ssl.SSLError):
            context.wrap_socket(sock, server_side=True).close()

    with listening(old) as port:
        body = connection(
            "http",
            {"base_url": f"https://localhost:{port}"},
            {"ca_certificate": authority.ca_file.read_text()},
        )
        outcome, message = _check.check(body, {}, timeout=FAST)
    assert outcome == "tls"
    assert message == (
        f"the server at `localhost:{port}` offers only TLS older than 1.2, which Lakana refuses; "
        "upgrade the server's TLS to 1.2 or newer"
    )


@pytest.mark.parametrize("mode", ["verify-full", "require"])
def test_every_context_the_check_makes_refuses_tls_older_than_1_2(mode: str) -> None:
    context = _check._context(None, mode)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2


def test_the_check_imports_only_the_standard_library() -> None:
    """The task image runs `_check.py` on its own, by path, without the `lakana` package.

    So every import must be of the standard library: no `lakana` module, no relative import, no
    third-party package (the image installs none).
    """
    tree = ast.parse(Path(_check.__file__).read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import needs the lakana package"
            assert node.module is not None
            found.append(node.module)

    assert found, "the walk found no import at all"
    roots = {name.split(".")[0] for name in found}
    assert roots - {"__future__"} <= set(sys.stdlib_module_names), roots
    assert "lakana" not in roots
