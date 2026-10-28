"""`lakana.ingest._check`: what a connection test finds, against servers the test runs itself."""

from __future__ import annotations

import ast
import base64
import contextlib
import errno
import hashlib
import hmac
import http.server
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pg_wire_fixture import Login, error_response, pg_login_server
from sdk_tls_fixture import (
    Answer,
    Authority,
    Exchange,
    issue_leaf,
    make_authority,
    scripted,
    serving,
)

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
    context.minimum_version = ssl.TLSVersion.TLSv1_2
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
    context.minimum_version = ssl.TLSVersion.TLSv1_2
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


def test_a_root_with_a_trailing_slash_is_not_followed_when_it_is_a_link(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "root").symlink_to(tmp_path / "real")
    body = connection("directory", {"root": "shared", "path": ""})
    routing = {"root": "shared", "root_dir": f"{tmp_path / 'root'}/", "path": ""}
    assert _check.check(body, routing, timeout=FAST)[0] == "failed"


def test_a_sqlite_file_must_be_a_regular_file(tmp_path: Path) -> None:
    sqlite3.connect(tmp_path / "data.db").close()
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
    context.minimum_version = ssl.TLSVersion.TLSv1_2
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


DRIVERS = set(_check.DRIVERS)
"""What the check environment installs, and the only packages `_check` may import besides the
standard library: inside a credential step, never at module level."""


def test_the_check_imports_only_the_standard_library_and_its_drivers() -> None:
    """The task image runs `_check.py` on its own, by path, without the `lakana` package.

    Its module-level imports are the standard library's; a credential step imports its driver
    inside the step, from `DRIVERS` only (the check environment's packages). Never `lakana`,
    never pyarrow (the check environment has neither), never a relative import.
    """
    tree = ast.parse(Path(_check.__file__).read_text(encoding="utf-8"))
    top: list[str] = []
    inner: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0, "a relative import needs the lakana package"
                assert node.module is not None
                names = [node.module]
            else:
                names = [alias.name for alias in node.names]
            (top if node in tree.body else inner).extend(names)

    assert top and inner, "the walk found no import at all"
    top_roots = {name.split(".")[0] for name in top}
    inner_roots = {name.split(".")[0] for name in inner}
    assert top_roots - {"__future__"} <= set(sys.stdlib_module_names), top_roots
    assert inner_roots <= set(sys.stdlib_module_names) | DRIVERS, inner_roots
    assert "lakana" not in top_roots | inner_roots
    assert "pyarrow" not in top_roots | inner_roots


# --- the credential steps of s3 and azure ----------------------------------------------------


@dataclass
class Answering:
    """A TLS server that records each request and answers with one status and body.

    Attributes
    ----------
    port
        Its port on 127.0.0.1.
    requests
        Each request's line and headers (lower-case names), in order.
    """

    port: int
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)


@contextmanager
def answering(
    authority: Authority, tmp_path: Path, status: int, body: bytes = b""
) -> Generator[Answering, None, None]:
    """Serve one fixed answer over TLS with a certificate for ``localhost``.

    Parameters
    ----------
    authority
        Signs the server's certificate.
    tmp_path
        Where its files are written.
    status
        The status of every answer.
    body
        The body of every answer.

    Yields
    ------
    Answering
        The port and the requests seen.
    """
    cert, key = issue_leaf(authority, tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    seen: list[tuple[str, dict[str, str]]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        """Records and answers."""

        def do_GET(self) -> None:
            """Record the request and send the fixed answer."""
            seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            """Log nothing.

            Parameters
            ----------
            format
                Unused.
            *args
                Unused.
            """

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Answering(server.server_address[1], seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_sigv4_matches_the_aws_example() -> None:
    """The signature of AWS's own "GET Bucket (List Objects)" example."""
    headers = _check.sigv4_headers(
        host="examplebucket.s3.amazonaws.com",
        path="/",
        params={"max-keys": "2", "prefix": "J"},
        region="us-east-1",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        now=time.strptime("20130524T000000Z", "%Y%m%dT%H%M%SZ"),
    )

    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date, "
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )
    assert headers["x-amz-date"] == "20130524T000000Z"


def test_the_azure_shared_key_signs_the_documented_string() -> None:
    """VERB, eleven empty standard headers, the x-ms headers, then the canonical resource."""
    key = base64.b64encode(b"account-key-bytes").decode()
    params = {"restype": "container", "comp": "list", "maxresults": "1", "prefix": "in/"}
    date = "Sun, 25 Oct 2026 12:00:00 GMT"

    headers = _check.azure_shared_key(
        account="acct", account_key=key, path="/landing", params=params, date=date
    )

    expected = (
        "GET\n\n\n\n\n\n\n\n\n\n\n\n"
        f"x-ms-date:{date}\nx-ms-version:2021-08-06\n"
        "/acct/landing\ncomp:list\nmaxresults:1\nprefix:in/\nrestype:container"
    )
    digest = hmac.new(b"account-key-bytes", expected.encode(), hashlib.sha256).digest()
    assert headers["Authorization"] == f"SharedKey acct:{base64.b64encode(digest).decode()}"


def _s3(port: int, secret: dict[str, str] | None) -> dict[str, object]:
    """Build an `s3` connection on a local server.

    Parameters
    ----------
    port
        The server's port.
    secret
        Its secret.

    Returns
    -------
    dict
        The body.
    """
    return connection(
        "s3",
        {"bucket": "landing", "region": "eu-central-1", "endpoint": f"https://localhost:{port}"},
        {"access_key_id": "AKID", "prefix": "in/"},
        secret=secret,
        endpoints=[("localhost", port)],
    )


def test_s3_credentials_list_one_object_signed_as_sent(
    authority: Authority, tmp_path: Path
) -> None:
    """The step lists at most one object under the prefix; the signature is of that request."""
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
    with answering(authority, tmp_path, 200, b"<ListBucketResult/>") as server:
        outcome, message = _check.check(
            _s3(server.port, {"secret_access_key": SECRET}), {}, timeout=FAST
        )

    assert outcome == "ok"
    assert message.endswith(", credentials accepted")
    (path, headers) = server.requests[-1]
    assert path == "/landing?list-type=2&max-keys=1&prefix=in%2F"
    resigned = _check.sigv4_headers(
        host=f"localhost:{server.port}",
        path="/landing",
        params={"list-type": "2", "max-keys": "1", "prefix": "in/"},
        region="eu-central-1",
        access_key_id="AKID",
        secret_access_key=SECRET,
        now=time.strptime(headers["x-amz-date"], "%Y%m%dT%H%M%SZ"),
    )
    assert headers["authorization"] == resigned["Authorization"]
    assert SECRET not in message


def test_a_refused_s3_key_is_credentials_with_the_stores_code(
    authority: Authority, tmp_path: Path
) -> None:
    """403 with S3's error XML: `credentials`, the store's code and message, no secret."""
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
    answer = (
        b"<Error><Code>SignatureDoesNotMatch</Code><Message>The request signature we "
        b"calculated does not match</Message></Error>"
    )
    with answering(authority, tmp_path, 403, answer) as server:
        outcome, message = _check.check(
            _s3(server.port, {"secret_access_key": SECRET}), {}, timeout=FAST
        )

    assert outcome == "credentials"
    assert message == (
        "the store refused the credentials (SignatureDoesNotMatch: The request signature we "
        "calculated does not match)"
    )


def test_a_missing_bucket_is_not_found(authority: Authority, tmp_path: Path) -> None:
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
    answer = b"<Error><Code>NoSuchBucket</Code><Message>gone</Message></Error>"
    with answering(authority, tmp_path, 404, answer) as server:
        outcome, message = _check.check(
            _s3(server.port, {"secret_access_key": SECRET}), {}, timeout=FAST
        )

    assert (outcome, message) == (
        "not_found",
        "bucket `landing` does not exist (NoSuchBucket: gone)",
    )


def test_azure_credentials_with_a_sas_token_or_the_shared_key(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SAS token goes into the query; a shared key signs the request it is sent with."""
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
    key = base64.b64encode(b"account-key-bytes").decode()
    with answering(authority, tmp_path, 200, b"<EnumerationResults/>") as server:
        monkeypatch.setattr(
            _check, "AZURE_URL", f"https://localhost:{server.port}/{{account}}/{{container}}"
        )
        body = connection(
            "azure",
            {"account": "acct", "container": "landing"},
            endpoints=[("localhost", server.port)],
            secret={"sas_token": "?sv=2022&sig=sas-s3cr3t"},
        )
        by_sas = _check.check(body, {}, timeout=FAST)
        body["secret"] = {"account_key": key}
        by_key = _check.check(body, {}, timeout=FAST)

    assert by_sas[0] == by_key[0] == "ok"
    sas_path, _ = server.requests[0]
    assert sas_path.endswith("&sv=2022&sig=sas-s3cr3t")
    assert "sas-s3cr3t" not in by_sas[1]
    key_path, headers = server.requests[1]
    expected = _check.azure_shared_key(
        account="acct",
        account_key=key,
        path="/acct/landing",
        params={"restype": "container", "comp": "list", "maxresults": "1"},
        date=headers["x-ms-date"],
    )
    assert key_path == "/acct/landing?comp=list&maxresults=1&restype=container"
    assert headers["authorization"] == expected["Authorization"]


def test_an_azure_key_that_is_not_base64_is_credentials(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
    with answering(authority, tmp_path, 200) as server:
        monkeypatch.setattr(
            _check, "AZURE_URL", f"https://localhost:{server.port}/{{account}}/{{container}}"
        )
        body = connection(
            "azure",
            {"account": "acct", "container": "landing"},
            endpoints=[("localhost", server.port)],
            secret={"account_key": "not base64!"},
        )
        assert _check.check(body, {}, timeout=FAST) == (
            "credentials",
            "the account_key is not base64",
        )


def test_a_server_that_does_not_speak_http_is_failed(authority: Authority, tmp_path: Path) -> None:
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)

    def garbage(tls: ssl.SSLSocket) -> None:
        tls.recv(65536)
        tls.sendall(b"not http\r\n\r\n")

    cert, key = issue_leaf(authority, tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)

    def handle(sock: socket.socket) -> None:
        with contextlib.suppress(OSError), context.wrap_socket(sock, server_side=True) as tls:
            garbage(tls)

    with listening(handle) as port:
        outcome, message = _check.check(_s3(port, {"secret_access_key": SECRET}), {}, timeout=FAST)

    assert (outcome, message) == ("failed", f"`localhost:{port}` did not answer HTTP")


def test_a_session_token_is_sent_and_signed(authority: Authority, tmp_path: Path) -> None:
    """A temporary credential's token travels in its header and is part of the signature."""
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
    body = _s3(0, {"secret_access_key": SECRET, "session_token": "session-token-0123"})
    with answering(authority, tmp_path, 200) as server:
        body["address"] = {**body["address"], "endpoint": f"https://localhost:{server.port}"}  # ty: ignore[invalid-argument-type]
        body["endpoints"] = [{"host": "localhost", "port": server.port, "role": "service"}]
        body["settings"] = {"access_key_id": "AKID"}
        assert _check.check(body, {}, timeout=FAST)[0] == "ok"

    path, headers = server.requests[-1]
    assert path == "/landing?list-type=2&max-keys=1"
    assert headers["x-amz-security-token"] == "session-token-0123"
    assert "x-amz-security-token" in headers["authorization"]


def test_another_status_is_failed_with_the_status(authority: Authority, tmp_path: Path) -> None:
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
    with answering(authority, tmp_path, 503) as server:
        outcome, message = _check.check(
            _s3(server.port, {"secret_access_key": SECRET}), {}, timeout=FAST
        )

    assert (outcome, message) == ("failed", "the store answered HTTP 503 (HTTP 503)")


def test_the_azure_prefix_narrows_the_listing(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
    with answering(authority, tmp_path, 200) as server:
        monkeypatch.setattr(
            _check, "AZURE_URL", f"https://localhost:{server.port}/{{account}}/{{container}}"
        )
        body = connection(
            "azure",
            {"account": "acct", "container": "landing"},
            {"prefix": "in/"},
            endpoints=[("localhost", server.port)],
            secret={"sas_token": "sv=1&sig=x"},
        )
        assert _check.check(body, {}, timeout=FAST)[0] == "ok"

    assert server.requests[-1][0] == (
        "/acct/landing?comp=list&maxresults=1&prefix=in%2F&restype=container&sv=1&sig=x"
    )


def test_a_network_failure_of_the_credential_step_is_its_outcome(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store reached once and then refusing the list request: `refused`, naming it."""
    os.environ["LAKANA_CA_BUNDLE"] = str(authority.ca_file)
    real_open, calls = _check._open, []

    def second_refused(
        host: str, port: int, routing: Mapping[str, object], t: float
    ) -> socket.socket:
        calls.append(host)
        if len(calls) > 1:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        return real_open(host, port, routing, t)

    monkeypatch.setattr(_check, "_open", second_refused)
    with answering(authority, tmp_path, 200) as server:
        outcome, message = _check.check(
            _s3(server.port, {"secret_access_key": SECRET}), {}, timeout=FAST
        )

    assert outcome == "refused"
    assert f"localhost:{server.port}" in message


# --- the credential steps of gcs, postgresql, mariadb and sqlite-file -------------------------


@dataclass
class Google:
    """A TLS server standing in for Google's token endpoint and the GCS listing.

    Attributes
    ----------
    port
        Its port on 127.0.0.1 (certificate for ``localhost``).
    public_key
        Verifies the assertions it receives.
    seen
        Each request's method, path, headers (lower-case names) and form fields, in order.
    token_status
        The status of the token answer (200 gives ``{"access_token": "tok"}``).
    list_status
        The status of the listing.
    token_raw
        A body that replaces the token answer's, or `None`.
    list_raw
        A body that replaces the listing's, or `None`.
    token_headers
        Extra headers of the token answer (a redirect's ``Location``).
    chunked
        Whether every answer is sent with ``Transfer-Encoding: chunked``, as Google may.
    """

    port: int
    public_key: object
    seen: list[tuple[str, str, dict[str, str], dict[str, str]]] = field(default_factory=list)
    token_status: int = 200
    list_status: int = 200
    token_raw: bytes | None = None
    list_raw: bytes | None = None
    token_headers: dict[str, str] = field(default_factory=dict)
    chunked: bool = False


@contextmanager
def google(authority: Authority, tmp_path: Path) -> Generator[tuple[Google, str], None, None]:
    """Serve a token endpoint and a listing; make a service-account key for them.

    Parameters
    ----------
    authority
        Signs the server's certificate.
    tmp_path
        Where its files are written.

    Yields
    ------
    tuple
        The server and the key's JSON (its ``token_uri`` is the server's ``/token``).
    """
    from urllib.parse import parse_qsl

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    cert, key = issue_leaf(authority, tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    state = Google(0, private.public_key())

    class Handler(http.server.BaseHTTPRequestHandler):
        """Answers as configured and records."""

        def _answer(
            self,
            status: int,
            body: Mapping[str, object],
            raw: bytes | None,
            headers: Mapping[str, str] | None = None,
        ) -> None:
            data = json.dumps(body).encode() if raw is None else raw
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            if state.chunked:
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                half = len(data) // 2
                for part in (data[:half], data[half:], b""):
                    self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                return
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            """Record a token request and answer it."""
            form = dict(parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.seen.append(("POST", self.path, headers, form))
            if state.token_status == 200:
                self._answer(200, {"access_token": "tok", "expires_in": 3599}, state.token_raw)
            else:
                body = {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}
                self._answer(state.token_status, body, state.token_raw, state.token_headers)

        def do_GET(self) -> None:
            """Record a listing and answer it."""
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.seen.append(("GET", self.path, headers, {}))
            if state.list_status == 200:
                self._answer(200, {"kind": "storage#objects"}, state.list_raw)
            else:
                error = {"error": {"message": "No such bucket or denied"}}
                self._answer(state.list_status, error, state.list_raw)

        def log_message(self, format: str, *args: object) -> None:
            """Log nothing.

            Parameters
            ----------
            format
                Unused.
            *args
                Unused.
            """

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    state.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    key_json = json.dumps(
        {
            "type": "service_account",
            "client_email": "reader@project.iam.gserviceaccount.com",
            "private_key_id": "kid-1",
            "private_key": pem,
            "token_uri": f"https://localhost:{state.port}/token",
        }
    )
    try:
        yield state, key_json
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _gcs(port: int, key_json: str) -> dict[str, object]:
    """Build a `gcs` connection whose service and token endpoints are the local server.

    Parameters
    ----------
    port
        The server's port.
    key_json
        The secret.

    Returns
    -------
    dict
        The body.
    """
    body = connection(
        "gcs", {"bucket": "landing"}, {"prefix": "in/"}, secret={"service_account_json": key_json}
    )
    body["endpoints"] = [
        {"host": "localhost", "port": port, "role": "service"},
        {"host": "localhost", "port": port, "role": "token"},
    ]
    return body


@pytest.fixture
def gcs_server(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[Google, str], None, None]:
    """Run `google` and point the check's listing and trust at it.

    Parameters
    ----------
    authority
        Signs the server.
    tmp_path
        Pytest's directory.
    monkeypatch
        Sets `GCS_URL` and `LAKANA_CA_BUNDLE`.

    Yields
    ------
    tuple
        The server and the key.
    """
    with google(authority, tmp_path) as (server, key_json):
        monkeypatch.setattr(
            _check, "GCS_URL", f"https://localhost:{server.port}/storage/v1/b/{{bucket}}/o"
        )
        monkeypatch.setenv("LAKANA_CA_BUNDLE", str(authority.ca_file))
        yield server, key_json


def test_a_gcs_key_signs_a_token_request_and_lists_one_object(
    gcs_server: tuple[Google, str],
) -> None:
    """RS256 over the key's claims, sent to its token endpoint; the token lists the prefix."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    server, key_json = gcs_server

    outcome, message = _check.check(_gcs(server.port, key_json), {}, timeout=FAST)

    assert (outcome, message.endswith(", credentials accepted")) == ("ok", True)
    (_, token_path, _, form), (_, list_path, list_headers, _) = server.seen
    assert token_path == "/token"
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    head, claims, signature = form["assertion"].split(".")

    def decoded(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    server.public_key.verify(  # ty: ignore[unresolved-attribute]
        decoded(signature), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    assert json.loads(decoded(head)) == {"alg": "RS256", "typ": "JWT", "kid": "kid-1"}
    said = json.loads(decoded(claims))
    assert said["aud"] == f"https://localhost:{server.port}/token"
    assert said["iss"] == "reader@project.iam.gserviceaccount.com"
    assert said["scope"] == _check.GCS_SCOPE and said["exp"] - said["iat"] == 600
    assert list_path == "/storage/v1/b/landing/o?maxResults=1&prefix=in%2F"
    assert list_headers["authorization"] == "Bearer tok"
    assert "tok" not in message


def test_a_gcs_key_naming_another_token_endpoint_sends_nothing(
    gcs_server: tuple[Google, str],
) -> None:
    """The signed key goes only to a token endpoint of the connection, under either runner."""
    server, key_json = gcs_server
    key = json.loads(key_json)

    for uri, shown in (
        ("https://evil.example/token?k=v#x", "`evil.example`"),
        ("https://evil.example:8443/path", "`evil.example:8443`"),
    ):
        elsewhere = json.dumps({**key, "token_uri": uri})
        outcome, message = _check.check(_gcs(server.port, elsewhere), {}, timeout=FAST)

        assert (outcome, message) == (
            "credentials",
            f"the key's token_uri names {shown}, which is not the connection's token endpoint "
            f"(`localhost:{server.port}`); Lakana sends a signed key nowhere else: store the key "
            "file Google gave you",
        )
        assert "/token" not in message and "/path" not in message and "k=v" not in message
    assert server.seen == []


@pytest.mark.parametrize(
    ("token", "listing", "outcome", "fragment"),
    [
        (400, 200, "credentials", "Google refused the key (invalid_grant: Invalid JWT Signature.)"),
        (200, 403, "credentials", "the store refused the credentials (No such bucket or denied)"),
        (200, 404, "not_found", "bucket `landing` does not exist"),
        (200, 500, "failed", "HTTP 500"),
    ],
)
def test_a_refused_gcs_key_or_bucket_is_named(
    gcs_server: tuple[Google, str], token: int, listing: int, outcome: str, fragment: str
) -> None:
    """Google's own words are kept; a missing bucket is not a refused key."""
    server, key_json = gcs_server
    server.token_status, server.list_status = token, listing

    found, message = _check.check(_gcs(server.port, key_json), {}, timeout=FAST)

    assert found == outcome
    assert fragment in message


@pytest.mark.parametrize(
    "secret",
    ["not json", json.dumps({"client_email": "a", "token_uri": "b"}), json.dumps([1])],
)
def test_a_gcs_secret_that_is_no_key_is_credentials(
    gcs_server: tuple[Google, str], secret: str
) -> None:
    """Nothing is signed or sent."""
    server, _ = gcs_server

    outcome, message = _check.check(_gcs(server.port, secret), {}, timeout=FAST)

    assert outcome == "credentials"
    assert "not a service-account key" in message
    assert server.seen == []


def test_without_cryptography_a_gcs_test_fails_saying_what_to_install(
    gcs_server: tuple[Google, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict: the local runner without the package cannot pass the test."""
    server, key_json = gcs_server
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives", None)

    outcome, message = _check.check(_gcs(server.port, key_json), {}, timeout=FAST)

    assert outcome == "failed"
    assert message == _check.UNAVAILABLE.format(module="cryptography", package="cryptography")


class _PgError(Exception):
    """The fake PostgreSQL driver's error."""


def _fake_postgres(
    monkeypatch: pytest.MonkeyPatch,
    refuse: str | None = None,
    refuse_select: str | None = None,
    sqlstate: str | None = None,
) -> list[str]:
    """Put a fake ``adbc_driver_postgresql.dbapi`` in place, and a fake second login.

    Parameters
    ----------
    monkeypatch
        Installs it.
    refuse
        The text `connect` raises, or `None` to accept.
    refuse_select
        The text `execute` raises, or `None` to run the statement.
    sqlstate
        What `_check.postgres_refusal` finds after a refused `connect`.

    Returns
    -------
    list of str
        Each connection string, and ``executed: <sql>`` for each statement.
    """
    seen: list[str] = []

    class Cursor:
        def execute(self, statement: str) -> None:
            if refuse_select is not None:
                raise _PgError(refuse_select)
            seen.append(f"executed: {statement}")

    class Database:
        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            seen.append("closed")

    def connect(info: str) -> Database:
        seen.append(info)
        if refuse is not None:
            raise _PgError(refuse)
        return Database()

    import types

    module = types.ModuleType("adbc_driver_postgresql.dbapi")
    module.connect = connect  # ty: ignore[unresolved-attribute]
    module.Error = _PgError  # ty: ignore[unresolved-attribute]
    monkeypatch.setitem(sys.modules, "adbc_driver_postgresql.dbapi", module)
    if "adbc_driver_postgresql" in sys.modules:  # imported for real by an end-to-end test
        monkeypatch.setattr(sys.modules["adbc_driver_postgresql"], "dbapi", module, raising=False)
    monkeypatch.setattr(_check, "postgres_refusal", lambda *args: sqlstate)
    return seen


def _pg_body(port: int, authority: Authority) -> dict[str, object]:
    """Build a `postgresql` connection on a local TLS server, with a password.

    Parameters
    ----------
    port
        The server's port.
    authority
        Its CA, given to the connection.

    Returns
    -------
    dict
        The body.
    """
    return connection(
        "postgresql",
        {"host": "localhost", "port": port, "database": "crm"},
        {"user": "reader", "ca_certificate": authority.ca_file.read_text()},
        secret={"password": SECRET},
    )


def test_postgresql_credentials_log_in_and_execute_select_1(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only login that executes SELECT 1 and reads nothing (no pyarrow in the check)."""
    seen = _fake_postgres(monkeypatch)
    with listening(tls_server(authority, tmp_path)) as port:
        outcome, message = _check.check(_pg_body(port, authority), {}, timeout=FAST)

    assert (outcome, message) == (
        "ok",
        f"reachable: `localhost:{port}`, TLS verified, credentials accepted",
    )
    info, executed, closed = seen
    assert info.startswith(
        f"host='localhost' port='{port}' dbname='crm' user='reader' password='{SECRET}' "
    )
    assert (
        "sslmode='verify-full'" in info and "options='-c default_transaction_read_only=on'" in info
    )
    assert "sslrootcert='/" in info
    assert (executed, closed) == ("executed: SELECT 1", "closed")


@pytest.mark.parametrize(
    ("refusal", "sqlstate", "outcome", "fragment"),
    [
        (
            'FATAL:  password authentication failed for user "reader"',
            "28P01",
            "credentials",
            "PostgreSQL refused the login (FATAL: password",
        ),
        (
            "FATAL:  kein pg_hba.conf-Eintrag für Host »10.0.0.1«",
            "28000",
            "credentials",
            "PostgreSQL refused the login (FATAL: kein pg_hba.conf-Eintrag",
        ),
        ("SSL error: certificate verify failed", None, "tls", "not trusted for `localhost`"),
        (
            'FATAL:  database "crm" does not exist',
            "3D000",
            "failed",
            'the PostgreSQL login failed (FATAL: database "crm"',
        ),
        (
            'FATAL:  password authentication failed for user "reader"',
            None,
            "failed",
            "the PostgreSQL login failed (FATAL: password",
        ),
    ],
    ids=["28P01", "28000", "tls", "3D000", "no-sqlstate"],
)
def test_a_refused_postgresql_login_is_its_outcome(
    authority: Authority,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
    sqlstate: str | None,
    outcome: str,
    fragment: str,
) -> None:
    """Classified by the SQLSTATE, never by words; the server's words are kept, the password not."""
    _fake_postgres(monkeypatch, refuse=f"{refusal} {SECRET}", sqlstate=sqlstate)
    with listening(tls_server(authority, tmp_path)) as port:
        found, message = _check.check(_pg_body(port, authority), {}, timeout=FAST)

    assert found == outcome
    assert fragment in message
    assert SECRET not in message


def test_conninfo_never_lets_libpq_read_the_worker_users_home_directory() -> None:
    """No client certificate, and no root file, CRL or password file from ``~/.postgresql/``."""
    info = _check.conninfo({"host": "db.example.com", "sslmode": "require"})

    assert "sslcertmode='disable'" in info
    for option in ("sslrootcert", "sslcrl", "passfile"):
        assert f"{option}='{_check.NOWHERE}'" in info
    assert not Path(_check.NOWHERE).exists()
    assert not Path(_check.NOWHERE).parent.is_dir()


def test_conninfo_keeps_the_root_file_a_caller_names() -> None:
    info = _check.conninfo({"host": "db.example.com", "sslrootcert": "system"})

    assert "sslrootcert='system'" in info
    assert info.count("sslrootcert=") == 1


def test_postgresql_credentials_through_the_relay_dial_its_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relay is libpq's ``hostaddr``; ``host`` stays the name TLS checks."""
    seen = _fake_postgres(monkeypatch)
    monkeypatch.setattr(_check.socket, "gethostbyname", lambda name: "10.207.0.3")
    body = connection(
        "postgresql",
        {"host": "db.example.com", "port": 5432, "database": "crm"},
        {"user": "reader", "tls": "require"},
        secret={"password": SECRET},
    )

    _check._postgresql_credentials(body, {"relay_port": 15001}, FAST)

    assert seen[0].startswith("host='db.example.com' port='15001' hostaddr='10.207.0.3' ")
    assert f"sslrootcert='{_check.NOWHERE}'" in seen[0]
    assert "sslcertmode='disable'" in seen[0]


def test_without_the_driver_a_postgresql_test_fails_saying_what_to_install(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local runner's Python without `lakana[assets]`: strict, not a silent pass."""
    monkeypatch.setitem(sys.modules, "adbc_driver_postgresql.dbapi", None)
    with listening(tls_server(authority, tmp_path)) as port:
        outcome, message = _check.check(_pg_body(port, authority), {}, timeout=FAST)

    assert outcome == "failed"
    assert message == _check.UNAVAILABLE.format(
        module="adbc_driver_postgresql", package="lakana[assets]"
    )


class _MyError(Exception):
    """PyMySQL's error: ``args`` is ``(code, message)``."""


def _fake_pymysql(monkeypatch: pytest.MonkeyPatch, refuse: Exception | None = None) -> list[object]:
    """Put a fake ``pymysql`` in place.

    Parameters
    ----------
    monkeypatch
        Installs it.
    refuse
        What `connect` raises, or `None` to accept.

    Returns
    -------
    list
        The keyword arguments of the connection, then each statement executed.
    """
    seen: list[object] = []

    class Cursor:
        def execute(self, statement: str) -> None:
            seen.append(statement)

        def fetchall(self) -> list[tuple[int]]:
            return [(1,)]

    class Connection:
        def __init__(self, **kwargs: object) -> None:
            seen.append(kwargs)

        def connect(self, sock: object = None) -> None:
            if refuse is not None:
                raise refuse

        def cursor(self) -> Cursor:
            return Cursor()

        def close(self) -> None:
            seen.append("closed")

    import types

    module = types.ModuleType("pymysql")
    module.connections = types.SimpleNamespace(Connection=Connection)  # ty: ignore[unresolved-attribute]
    module.MySQLError = _MyError  # ty: ignore[unresolved-attribute]
    monkeypatch.setitem(sys.modules, "pymysql", module)
    return seen


def _greeting_server(sock: socket.socket) -> None:
    """Greet like MariaDB and close.

    Parameters
    ----------
    sock
        The accepted socket.
    """
    payload = b"\x0a11.4.2-MariaDB\x00" + b"\x00" * 20
    with contextlib.suppress(OSError):
        sock.sendall(len(payload).to_bytes(3, "little") + b"\x00" + payload)
    sock.close()


def _maria_body(port: int, tls: str = "verify-full") -> dict[str, object]:
    """Build a `mariadb` connection on a local server.

    Parameters
    ----------
    port
        The server's port.
    tls
        Its ``tls``.

    Returns
    -------
    dict
        The body.
    """
    return connection(
        "mariadb",
        {"host": "127.0.0.1", "port": port, "database": "shop"},
        {"user": "reader", "tls": tls},
        secret={"password": SECRET},
    )


def test_mariadb_credentials_log_in_over_tls_and_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The context verifies the host; no LOCAL INFILE; a read-only transaction runs SELECT 1."""
    seen = _fake_pymysql(monkeypatch)
    with listening(_greeting_server) as port:
        outcome, message = _check.check(_maria_body(port), {}, timeout=FAST)

    assert (outcome, message) == (
        "ok",
        f"reachable: `127.0.0.1:{port}` greeted like MariaDB or MySQL, TLS verified, "
        "credentials accepted",
    )
    kwargs = seen[0]
    assert isinstance(kwargs, dict)
    assert kwargs["ssl"].check_hostname is True and kwargs["ssl_disabled"] is False
    assert kwargs["local_infile"] is False and kwargs["defer_connect"] is True
    assert seen[1:] == ["START TRANSACTION READ ONLY", "SELECT 1", "closed"]


def test_mariadb_tls_disable_logs_in_without_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ssl_disabled`: PyMySQL would otherwise try TLS without verifying it."""
    seen = _fake_pymysql(monkeypatch)
    with listening(_greeting_server) as port:
        outcome, message = _check.check(_maria_body(port, "disable"), {}, timeout=FAST)

    assert outcome == "ok" and "TLS not used (tls: disable), credentials accepted" in message
    kwargs = seen[0]
    assert isinstance(kwargs, dict) and kwargs["ssl"] is None and kwargs["ssl_disabled"] is True


def _certificate_refused() -> Exception:
    """Make PyMySQL's wrapped TLS failure.

    Returns
    -------
    Exception
        A 2003 error whose `original_exception` is the certificate error.
    """
    error = _MyError(2003, "Can't connect to MySQL server on '127.0.0.1'")
    error.original_exception = ssl.SSLCertVerificationError("certificate verify failed")  # ty: ignore[unresolved-attribute]
    return error


@pytest.mark.parametrize(
    ("refusal", "outcome", "fragment"),
    [
        (
            _MyError(1045, "Access denied for user 'reader'"),
            "credentials",
            "MariaDB refused the login (Access denied",
        ),
        (_certificate_refused(), "tls", "not trusted for `127.0.0.1`"),
        (
            _MyError(2026, "SSL is required but the server doesn't support it"),
            "tls",
            "does not offer TLS",
        ),
        (
            _MyError(1049, "Unknown database 'shop'"),
            "failed",
            "the MariaDB login failed (Unknown database",
        ),
    ],
)
def test_a_refused_mariadb_login_is_its_outcome(
    monkeypatch: pytest.MonkeyPatch, refusal: Exception, outcome: str, fragment: str
) -> None:
    """MariaDB's TLS is checked here, by the login (the greeting alone cannot)."""
    _fake_pymysql(monkeypatch, refuse=refusal)
    with listening(_greeting_server) as port:
        found, message = _check.check(_maria_body(port), {}, timeout=FAST)

    assert found == outcome
    assert fragment in message


def test_without_pymysql_a_mariadb_test_fails_saying_what_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict, as for PostgreSQL."""
    monkeypatch.setitem(sys.modules, "pymysql", None)
    with listening(_greeting_server) as port:
        outcome, message = _check.check(_maria_body(port), {}, timeout=FAST)

    assert (outcome, message) == (
        "failed",
        _check.UNAVAILABLE.format(module="pymysql", package="lakana[assets]"),
    )


def test_a_sqlite_file_opens_read_only_or_is_named(tmp_path: Path) -> None:
    """A real database opens; a file that is none fails naming the path; nothing is written."""
    with sqlite3.connect(tmp_path / "app.db") as db:
        db.execute("CREATE TABLE t(a)")
    (tmp_path / "notes.db").write_text("not a database at all " * 10)
    body = connection("sqlite-file", {"root": "shared", "path": "app.db"})
    routing = {"root": "shared", "root_dir": str(tmp_path)}

    ok = _check.check(body, {**routing, "path": "app.db"}, timeout=FAST)
    bad = _check.check(body, {**routing, "path": "notes.db"}, timeout=FAST)

    assert ok == (
        "ok",
        "the file `app.db` exists under root `shared` and is readable; it opens read-only as a "
        "SQLite database",
    )
    assert bad[0] == "failed" and "`notes.db` cannot be read as a SQLite database" in bad[1]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["app.db", "notes.db"]


def test_a_server_s_control_characters_are_escaped_in_the_message(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostile server cannot forge a line or recolour a terminal through its error text."""
    _fake_postgres(
        monkeypatch, refuse="FATAL: \x1b[2Jno pg_hba.conf entry ‮evil\x07", sqlstate="28000"
    )
    with listening(tls_server(authority, tmp_path)) as port:
        outcome, message = _check.check(_pg_body(port, authority), {}, timeout=FAST)

    assert outcome == "credentials"
    assert "\\x1b[2Jno pg_hba.conf entry \\u202eevil\\x07" in message
    assert not any(ord(c) < 32 or 0x202A <= ord(c) <= 0x202E for c in message)


def test_a_nul_in_a_postgresql_value_is_refused_before_libpq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NUL would cut the connection string short: libpq reads a C string."""
    seen = _fake_postgres(monkeypatch)
    body = connection(
        "postgresql",
        {"host": "db.example.com", "port": 5432, "database": "crm"},
        {"user": "re\x00ader", "tls": "require"},
        secret={"password": SECRET},
    )

    with pytest.raises(_check._Found) as found:
        _check._postgresql_credentials(body, {}, FAST)

    assert found.value.outcome == "credentials"
    assert "the `user` holds a NUL character" in found.value.message
    assert seen == []


@pytest.mark.parametrize("bundle", [None, "café"])
def test_an_unreadable_trust_bundle_fails_the_postgresql_login_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundle: str | None
) -> None:
    """A missing or non-ASCII `LAKANA_CA_BUNDLE` is named, not a driver error."""
    seen = _fake_postgres(monkeypatch)
    path = tmp_path / "bundle.pem"
    if bundle is not None:
        path.write_text(bundle, encoding="utf-8")
    monkeypatch.setenv("LAKANA_CA_BUNDLE", str(path))
    body = connection(
        "postgresql",
        {"host": "db.example.com", "port": 5432, "database": "crm"},
        {"user": "reader"},
        secret={"password": SECRET},
    )

    with pytest.raises(_check._Found) as found:
        _check._postgresql_credentials(body, {}, FAST)

    assert found.value.outcome == "failed"
    assert f"`{path}` (LAKANA_CA_BUNDLE) cannot be read" in found.value.message
    assert seen == []


def test_an_unresolvable_relay_fails_the_postgresql_login_as_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relay's name is the check's own; a lookup failure is said as such."""
    _fake_postgres(monkeypatch)

    def unresolvable(name: str) -> str:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(_check.socket, "gethostbyname", unresolvable)
    body = connection(
        "postgresql",
        {"host": "db.example.com", "port": 5432, "database": "crm"},
        {"user": "reader", "tls": "require"},
        secret={"password": SECRET},
    )

    with pytest.raises(_check._Found) as found:
        _check._postgresql_credentials(body, {"relay_port": 15001}, FAST)

    assert (found.value.outcome, found.value.message) == (
        "dns",
        f"cannot resolve `{_check.RELAY_HOST}` (Name or service not known)",
    )


def test_a_login_that_cannot_run_select_1_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accepted, but the statement refused: the test does not pass."""
    seen = _fake_postgres(monkeypatch, refuse_select="ERROR:  permission denied")
    body = connection(
        "postgresql",
        {"host": "db.example.com", "port": 5432, "database": "crm"},
        {"user": "reader", "tls": "require"},
        secret={"password": SECRET},
    )

    with pytest.raises(_check._Found) as found:
        _check._postgresql_credentials(body, {}, FAST)

    assert (found.value.outcome, found.value.message) == (
        "failed",
        "PostgreSQL accepted the login but not SELECT 1 (ERROR: permission denied)",
    )
    assert seen[-1] == "closed"


def test_without_the_sqlite_driver_a_sqlite_test_fails_saying_what_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict, as for the network kinds."""
    sqlite3.connect(tmp_path / "app.db").close()
    monkeypatch.setitem(sys.modules, "adbc_driver_sqlite.dbapi", None)
    body = connection("sqlite-file", {"root": "shared", "path": "app.db"})
    routing = {"root": "shared", "root_dir": str(tmp_path), "path": "app.db"}

    assert _check.check(body, routing, timeout=FAST) == (
        "failed",
        _check.UNAVAILABLE.format(module="adbc_driver_sqlite", package="lakana[assets]"),
    )


def _ec_key() -> str:
    """Make a PEM private key that is no RSA key.

    Returns
    -------
    str
        An EC P-256 key.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    return (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


@pytest.mark.parametrize("private_key", ["not a pem", "ec"])
def test_a_gcs_key_without_an_rsa_private_key_is_credentials(
    gcs_server: tuple[Google, str], private_key: str
) -> None:
    """Nothing is signed or sent."""
    server, key_json = gcs_server
    pem = _ec_key() if private_key == "ec" else private_key
    bad = json.dumps({**json.loads(key_json), "private_key": pem})

    outcome, message = _check.check(_gcs(server.port, bad), {}, timeout=FAST)

    assert (outcome, message) == ("credentials", NOT_RSA)
    assert server.seen == []


NOT_RSA = (
    "the key's private_key is not an RSA private key (PEM); use a service-account JSON key with "
    "an RSA private key: the key file Google gave you"
)


def test_a_gcs_key_of_an_algorithm_cryptography_cannot_load_is_credentials(
    gcs_server: tuple[Google, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`UnsupportedAlgorithm` is not a `ValueError`: it gets the same outcome and next step."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization

    def unsupported(data: bytes, password: object) -> object:
        raise UnsupportedAlgorithm("no such curve")

    monkeypatch.setattr(serialization, "load_pem_private_key", unsupported)
    server, key_json = gcs_server

    assert _check.check(_gcs(server.port, key_json), {}, timeout=FAST) == ("credentials", NOT_RSA)
    assert server.seen == []


def test_a_gcs_key_without_an_id_signs_without_kid_and_lists_the_whole_bucket(
    gcs_server: tuple[Google, str],
) -> None:
    """`kid` only when the key has one; no prefix, no `prefix` parameter."""
    server, key_json = gcs_server
    key = json.loads(key_json)
    del key["private_key_id"]
    body = _gcs(server.port, json.dumps(key))
    body["settings"] = {}

    outcome, _ = _check.check(body, {}, timeout=FAST)

    assert outcome == "ok"
    (_, _, _, form), (_, list_path, _, _) = server.seen
    head = form["assertion"].split(".")[0]
    assert json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))) == {
        "alg": "RS256",
        "typ": "JWT",
    }
    assert list_path == "/storage/v1/b/landing/o?maxResults=1"


def test_the_token_request_needs_no_httpx_and_reads_a_chunked_answer(
    gcs_server: tuple[Google, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """It goes out on the check's own socket path, as the listing does; Google may chunk."""
    server, key_json = gcs_server
    server.chunked = True
    monkeypatch.setitem(sys.modules, "httpx", None)

    outcome, message = _check.check(_gcs(server.port, key_json), {}, timeout=FAST)

    assert (outcome, message.endswith(", credentials accepted")) == ("ok", True), message
    (method, path, headers, form), _ = server.seen
    assert (method, path) == ("POST", "/token")
    assert headers["content-type"] == "application/x-www-form-urlencoded"
    assert headers["host"] == f"localhost:{server.port}"
    assert set(form) == {"grant_type", "assertion"}


def test_a_token_endpoint_that_redirects_is_not_followed(
    gcs_server: tuple[Google, str], authority: Authority, tmp_path: Path
) -> None:
    """A 3xx is the answer: the assertion is never sent to the place it names."""
    server, key_json = gcs_server
    (tmp_path / "other").mkdir()
    with answering(authority, tmp_path / "other", 200) as other:
        server.token_status = 302
        server.token_headers = {"Location": f"https://localhost:{other.port}/token"}

        outcome, message = _check.check(_gcs(server.port, key_json), {}, timeout=FAST)

    assert outcome == "failed"
    assert f"the token endpoint `localhost:{server.port}` answered HTTP 302" in message
    assert [method for method, *_ in server.seen] == ["POST"]
    assert other.requests == []


def test_an_egress_proxy_refusal_of_the_token_request_keeps_its_outcome_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token request tunnels through the proxy as every other request of the check does."""
    reason = b"lakana-egress: oauth2.googleapis.com:443 is not a destination of this token\n"

    def refuses(sock: socket.socket) -> None:
        sock.recv(4096)
        sock.sendall(
            b"HTTP/1.1 403 Forbidden\r\nContent-Length: "
            + str(len(reason)).encode()
            + b"\r\n\r\n"
            + reason
        )
        sock.close()

    with proxy_says(monkeypatch, refuses), pytest.raises(_check._Found) as found:
        _check._gcs_token("https://oauth2.googleapis.com/token", "a.b.c", {"_kind": "gcs"}, FAST)

    assert (found.value.outcome, found.value.message) == ("not_allowed", reason.decode().strip())


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (b"<html>not json</html>", "answered HTTP 200"),
        (b'{"expires_in": 3599}', "answered HTTP 200"),
        (b"[1]", "answered HTTP 200"),
    ],
)
def test_a_token_answer_without_a_token_fails(
    gcs_server: tuple[Google, str], raw: bytes, fragment: str
) -> None:
    """Nothing is listed without a token."""
    server, key_json = gcs_server
    server.token_raw = raw

    outcome, message = _check.check(_gcs(server.port, key_json), {}, timeout=FAST)

    assert outcome == "failed"
    assert f"the token endpoint `localhost:{server.port}` {fragment}" in message
    assert [method for method, *_ in server.seen] == ["POST"]


def test_a_listing_error_that_is_not_json_names_the_status(
    gcs_server: tuple[Google, str],
) -> None:
    """Google's words when it gives some, else the status."""
    server, key_json = gcs_server
    server.list_status, server.list_raw = 403, b"<html>denied</html>"

    outcome, message = _check.check(_gcs(server.port, key_json), {}, timeout=FAST)

    assert (outcome, message.endswith("the store refused the credentials (HTTP 403)")) == (
        "credentials",
        True,
    )


def test_a_token_endpoint_with_an_untrusted_certificate_is_tls(
    gcs_server: tuple[Google, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The token request verifies the endpoint as every other request of the check does."""
    server, _ = gcs_server
    monkeypatch.delenv("LAKANA_CA_BUNDLE")

    with pytest.raises(_check._Found) as found:
        _check._gcs_token(f"https://localhost:{server.port}/token", "a.b.c", {}, FAST)

    assert (found.value.outcome, found.value.message) == (
        "tls",
        "the server's certificate is not trusted for `localhost`",
    )
    assert server.seen == []


def test_a_token_endpoint_that_does_not_answer_is_timeout() -> None:
    """A step's seconds bound the token request."""
    with listening() as port, pytest.raises(_check._Found) as found:
        _check._gcs_token(f"https://localhost:{port}/token", "a.b.c", {}, 0.2)

    assert (found.value.outcome, found.value.message) == (
        "timeout",
        f"`localhost:{port}` did not answer within 0.2 s",
    )


def test_a_token_endpoint_that_refuses_the_connection_is_refused() -> None:
    """Named by host and port, as every network failure of the check; never the assertion."""
    port = closed_port()

    with pytest.raises(_check._Found) as found:
        _check._gcs_token(f"https://localhost:{port}/token", "a.b.c", {}, FAST)

    assert (found.value.outcome, found.value.message) == (
        "refused",
        f"`localhost:{port}` refused the connection",
    )


def test_the_check_escapes_exactly_what_safe_name_escapes() -> None:
    """The check cannot import `lakana`; its copy of `safe_name` must not drift from it."""
    from lakana.ingest._errors import safe_name

    text = "".join(chr(code) for code in range(0x2100))

    assert _check._safe(text) == safe_name(text)


@pytest.mark.parametrize(
    "template",
    [
        "https://[localhost]/token",
        "https://localhost:{port}:80/token",
        "https://localhost:99999/token",
        "https://localhost:0/token",
        "https://local\thost:{port}/token",
        "https://localhost:{port}/to\rken",
        "https://localhost:{port}/token\n",
        "https://user:pw@localhost:{port}/token",
        "http://localhost:{port}/token",
        "https:///token",
    ],
)
def test_a_malformed_token_uri_is_named_and_never_sent(
    gcs_server: tuple[Google, str], template: str
) -> None:
    """Parsed once, strictly: no port 0, no second port, no whitespace, no user."""
    server, key_json = gcs_server
    uri = template.format(port=server.port)
    bad = json.dumps({**json.loads(key_json), "token_uri": uri})

    outcome, message = _check.check(_gcs(server.port, bad), {}, timeout=FAST)

    assert (outcome, message) == (
        "credentials",
        "the key's token_uri is not an https URL of a token endpoint of the connection "
        f"(`localhost:{server.port}`); store the key file Google gave you",
    )
    assert server.seen == []


def test_a_mariadb_trust_bundle_that_holds_no_certificate_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that is there but no PEM: the variable is named, not an `SSLError`."""
    _fake_pymysql(monkeypatch)
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("not a pem")
    monkeypatch.setenv("LAKANA_CA_BUNDLE", str(bundle))

    with pytest.raises(_check._Found) as found:
        _check._mariadb_credentials(_maria_body(5), {}, FAST)

    assert (found.value.outcome, found.value.message) == (
        "failed",
        f"the trust bundle `{bundle}` (LAKANA_CA_BUNDLE) cannot be read or holds no "
        "certificate; check the file on this worker",
    )


def test_a_connection_ca_that_holds_no_certificate_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundle is fine; the connection's own CA is not."""
    _fake_pymysql(monkeypatch)
    body = _maria_body(5)
    body["settings"] = {"user": "reader", "ca_certificate": "not a pem"}

    with pytest.raises(_check._Found) as found:
        _check._mariadb_credentials(body, {}, FAST)

    assert (found.value.outcome, found.value.message) == (
        "failed",
        "the connection's ca_certificate holds no usable certificate; set it again with "
        "`lakana connection update NAME --ca-file ca.pem`",
    )


def test_a_ca_file_that_cannot_be_written_is_not_called_an_unreadable_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the bundle's read says the bundle cannot be read; a full /tmp keeps its own words."""
    seen = _fake_postgres(monkeypatch)

    def full(*args: object, **kwargs: object) -> object:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_check.tempfile, "NamedTemporaryFile", full)
    body = connection(
        "postgresql",
        {"host": "db.example.com", "port": 5432, "database": "crm"},
        {"user": "reader", "ca_certificate": "-----BEGIN CERTIFICATE-----"},
        secret={"password": SECRET},
    )

    with pytest.raises(_check._Found) as found:
        _check._postgresql_credentials(body, {}, FAST)

    assert (found.value.outcome, found.value.message) == (
        "failed",
        "could not write the connection's CA certificate for libpq (No space left on device)",
    )
    assert seen == []


def test_a_mariadb_test_without_a_secret_says_what_it_did_not_check() -> None:
    """The greeting only: no TLS, no login, said so."""
    with listening(_greeting_server) as port:
        body = _maria_body(port)
        body["secret"] = None
        outcome, message = _check.check(body, {}, timeout=FAST)

    assert (outcome, message) == (
        "ok",
        f"reachable: `127.0.0.1:{port}` greeted like MariaDB or MySQL; TLS and credentials are "
        "checked only with a password: store one with `lakana connection update`",
    )


def test_the_drivers_are_named_once() -> None:
    """`DRIVERS` maps each import the check may make to the distribution that brings it."""
    assert set(_check.DRIVERS.values()) == {
        "adbc-driver-postgresql",
        "adbc-driver-sqlite",
        "cryptography",
        "PyMySQL",
    }


def test_no_constant_of_the_check_is_named_like_a_credential() -> None:
    """Name messages for what they say, never like a credential.

    CodeQL reads a string named like a credential as one, and its value reaching the output line
    as a secret written out in clear text (alert #66: a message constant named ``..._NO_SECRET``).
    """
    tree = ast.parse(Path(_check.__file__).read_text(encoding="utf-8"))
    names = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]

    assert names, "the walk found no constant"
    flagged = [n for n in names if re.search(r"SECRET|PASS(WORD|WD)?|TOKEN|KEY|CREDENTIAL", n)]
    assert flagged == []


@pytest.mark.parametrize(
    ("raw", "data"),
    [
        (b"5\r\nhello\r\n0\r\n\r\n", b"hello"),
        (b"3;name=x\r\nabc\r\n2\r\nde\r\n", b"abcde"),
        (b"5\r\nhel", b"hel"),
        (b"zz\r\nhello\r\n", b""),
    ],
    ids=["terminated", "extension-and-cut-after-a-chunk", "cut-inside-a-chunk", "not-chunks"],
)
def test_a_chunked_body_is_joined_as_far_as_it_goes(raw: bytes, data: bytes) -> None:
    """At most 64 KiB is read: a body cut short keeps what arrived; nonsense gives nothing."""
    assert _check._dechunk(raw) == data


# --- the credential step of http ---------------------------------------------------------------


def _api(
    base: str,
    authority: Authority,
    settings: Mapping[str, object],
    secret: dict[str, str],
    token_port: int | None = None,
) -> dict[str, object]:
    """Build an ``http`` connection on a scripted server, its token endpoint declared.

    Parameters
    ----------
    base
        The base URL.
    authority
        Whose CA the connection trusts.
    settings
        Its settings besides ``ca_certificate``.
    secret
        Its secret.
    token_port
        The port of a declared token endpoint on 127.0.0.1, or `None`.

    Returns
    -------
    dict
        The body.
    """
    port = int(base.rsplit(":", 1)[1].split("/", 1)[0])
    body = connection(
        "http",
        {"base_url": base},
        {**settings, "ca_certificate": authority.ca_file.read_text()},
        secret=secret,
        endpoints=[("127.0.0.1", port)],
    )
    if token_port is not None:
        endpoints = body["endpoints"]
        assert isinstance(endpoints, list)
        endpoints.append({"host": "127.0.0.1", "port": token_port, "role": "token"})
    return body


def _status(status: int) -> Callable[[Exchange], Answer]:
    """Answer every request with one status.

    Parameters
    ----------
    status
        The status.

    Returns
    -------
    Callable
        The answer.
    """
    return lambda exchange: (status, {}, b"{}")


@pytest.mark.parametrize(
    ("settings", "secret", "header", "value"),
    [
        ({"auth": "bearer"}, {"token": SECRET}, "authorization", f"Bearer {SECRET}"),
        (
            {"auth": "basic", "username": "reader"},
            {"password": SECRET},
            "authorization",
            "Basic " + base64.b64encode(f"reader:{SECRET}".encode()).decode(),
        ),
        (
            {"auth": "api_key", "api_key_name": "X-Key", "api_key_in": "header"},
            {"api_key": SECRET},
            "x-key",
            SECRET,
        ),
    ],
    ids=["bearer", "basic", "api-key-header"],
)
def test_http_credentials_are_sent_with_one_get_of_the_base_url(
    authority: Authority,
    tmp_path: Path,
    settings: dict[str, object],
    secret: dict[str, str],
    header: str,
    value: str,
) -> None:
    with scripted(authority, tmp_path, _status(200)) as server:
        outcome, message = _check.check(
            _api(f"{server.base}/v1", authority, settings, secret), {}, timeout=FAST
        )

    assert (outcome, message.endswith(", credentials accepted")) == ("ok", True)
    (seen,) = server.seen
    assert (seen.method, seen.target, seen.headers[header]) == ("GET", "/v1", value)
    assert SECRET not in message


def test_an_api_key_in_the_query_goes_in_the_query_of_a_base_url_without_a_path(
    authority: Authority, tmp_path: Path
) -> None:
    settings = {"auth": "api_key", "api_key_name": "key", "api_key_in": "query"}
    with scripted(authority, tmp_path, _status(200)) as server:
        outcome, _ = _check.check(
            _api(server.base, authority, settings, {"api_key": SECRET}), {}, timeout=FAST
        )

    assert outcome == "ok"
    assert [s.target for s in server.seen] == [f"/?key={SECRET}"]


@pytest.mark.parametrize(
    ("status", "outcome", "text"),
    [
        (401, "credentials", "the API refused the credentials (HTTP 401)"),
        (403, "credentials", "the API refused the credentials (HTTP 403)"),
        (301, "failed", "answers with a redirect (HTTP 301), which is not followed"),
        (429, "failed", "the API answered HTTP 429; test again later"),
        (503, "failed", "the API answered HTTP 503; test again later"),
        (404, "ok", "credentials not refused (the base URL answered HTTP 404"),
        (405, "ok", "credentials not refused (the base URL answered HTTP 405"),
    ],
)
def test_the_base_urls_status_decides_the_outcome(
    authority: Authority, tmp_path: Path, status: int, outcome: str, text: str
) -> None:
    with scripted(authority, tmp_path, _status(status)) as server:
        found = _check.check(
            _api(server.base, authority, {"auth": "bearer"}, {"token": SECRET}), {}, timeout=FAST
        )

    assert found[0] == outcome
    assert text in found[1]


@pytest.mark.parametrize("bad", ["a\r\nX-Evil: 1", "naïve"])
def test_a_token_no_header_can_carry_is_credentials_and_nothing_is_sent(
    authority: Authority, tmp_path: Path, bad: str
) -> None:
    with scripted(authority, tmp_path, _status(200)) as server:
        outcome, message = _check.check(
            _api(server.base, authority, {"auth": "bearer"}, {"token": bad}), {}, timeout=FAST
        )

    assert outcome == "credentials"
    assert "holds a line break" in message
    assert server.seen == []


_OAUTH = {"auth": "oauth2", "client_id": "lakana-reader", "scope": "read"}
ACCESS = "access-token-of-the-test-0123"


def _provider(token_status: int, token_body: bytes) -> Callable[[Exchange], Answer]:
    """Answer the token request with a status and body, and an API request with its token.

    Parameters
    ----------
    token_status
        The token endpoint's status.
    token_body
        Its body.

    Returns
    -------
    Callable
        The answer.
    """

    def answer(exchange: Exchange) -> Answer:
        if exchange.method == "POST":
            return token_status, {"Content-Type": "application/json"}, token_body
        good = exchange.headers.get("authorization") == f"Bearer {ACCESS}"
        return (200 if good else 401), {}, b"{}"

    return answer


def _oauth_body(base: str, authority: Authority, **settings: object) -> dict[str, object]:
    """Build an OAuth2 connection whose token endpoint is ``/token`` on the scripted server.

    Parameters
    ----------
    base
        The server's base URL.
    authority
        Whose CA the connection trusts.
    **settings
        Settings that replace the defaults.

    Returns
    -------
    dict
        The body.
    """
    port = int(base.rsplit(":", 1)[1])
    merged = {**_OAUTH, "token_url": f"{base}/token", **settings}
    return _api(f"{base}/v1", authority, merged, {"client_secret": SECRET}, token_port=port)


def test_oauth2_asks_the_token_url_then_gets_the_base_url_with_the_token(
    authority: Authority, tmp_path: Path
) -> None:
    token = json.dumps({"access_token": ACCESS, "token_type": "Bearer"}).encode()
    with scripted(authority, tmp_path, _provider(200, token)) as server:
        outcome, message = _check.check(_oauth_body(server.base, authority), {}, timeout=FAST)

    assert (outcome, message.endswith(", credentials accepted")) == ("ok", True)
    post, get = server.seen
    assert (post.method, post.target) == ("POST", "/token")
    assert urllib.parse.parse_qs(post.body.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": ["lakana-reader"],
        "client_secret": [SECRET],
        "scope": ["read"],
    }
    assert (get.target, get.headers["authorization"]) == ("/v1", f"Bearer {ACCESS}")
    assert SECRET not in get.target + json.dumps(get.headers)


@pytest.mark.parametrize(
    ("status", "body", "outcome", "text"),
    [
        (
            401,
            b'{"error": "invalid_client"}',
            "credentials",
            "refused the client credentials (invalid_client)",
        ),
        (400, b"{}", "credentials", "refused the client credentials (HTTP 400)"),
        (302, b"", "failed", "answered with a redirect (HTTP 302)"),
        (500, b"", "failed", "answered HTTP 500"),
        (200, b'{"token": "x"}', "failed", "answered without an access_token"),
        (200, b'{"access_token": "abcd", "token_type": "mac"}', "failed", "of type 'mac'"),
    ],
)
def test_the_token_endpoints_answer_decides_the_outcome(
    authority: Authority, tmp_path: Path, status: int, body: bytes, outcome: str, text: str
) -> None:
    with scripted(authority, tmp_path, _provider(status, body)) as server:
        found = _check.check(_oauth_body(server.base, authority), {}, timeout=FAST)

    assert found[0] == outcome
    assert text in found[1]
    assert [s.method for s in server.seen] == ["POST"]


def test_a_token_endpoints_words_never_show_the_client_secret_or_a_derived_value(
    authority: Authority, tmp_path: Path
) -> None:
    said = json.dumps({"error": "invalid_client", "error_description": f"bad {SECRET}"})
    with scripted(authority, tmp_path, _provider(401, said.encode())) as server:
        outcome, message = _check.check(_oauth_body(server.base, authority), {}, timeout=FAST)

    assert outcome == "credentials"
    assert SECRET not in message
    assert "bad ***" in message


def test_a_token_url_on_no_endpoint_of_the_connection_sends_nothing(
    authority: Authority, tmp_path: Path
) -> None:
    with scripted(authority, tmp_path, _provider(200, b"{}")) as server:
        body = _oauth_body(server.base, authority, token_url="https://127.0.0.1:1/token")
        outcome, message = _check.check(body, {}, timeout=FAST)

    assert outcome == "failed"
    assert "token_url is not on an endpoint of the connection" in message
    assert server.seen == []


def test_a_token_url_on_the_base_urls_host_and_port_is_accepted(
    authority: Authority, tmp_path: Path
) -> None:
    """The common setup: core declares one ``service`` endpoint for both, as the server sends."""
    token = json.dumps({"access_token": ACCESS}).encode()
    with scripted(authority, tmp_path, _provider(200, token)) as server:
        body = _oauth_body(server.base, authority)
        body["endpoints"] = [e for e in body["endpoints"] if e["role"] != "token"]  # ty: ignore[not-iterable]
        outcome, message = _check.check(body, {}, timeout=FAST)

    assert (outcome, message.endswith(", credentials accepted")) == ("ok", True), message
    assert [s.method for s in server.seen] == ["POST", "GET"]


def test_an_access_token_echoed_by_the_api_is_masked(
    authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A derived value is masked like the secret itself, whatever text carries it."""
    monkeypatch.setattr(
        _check, "HTTP_NOT_PROVEN", ", not refused (HTTP {status}) for Bearer " + ACCESS
    )
    token = json.dumps({"access_token": ACCESS}).encode()

    def answer(exchange: Exchange) -> Answer:
        return (200, {}, token) if exchange.method == "POST" else (404, {}, b"")

    with scripted(authority, tmp_path, answer) as server:
        outcome, message = _check.check(_oauth_body(server.base, authority), {}, timeout=FAST)

    assert outcome == "ok"
    assert ACCESS not in message
    assert message.endswith("for Bearer ***")


def test_oauth2_without_a_scope_sends_none(authority: Authority, tmp_path: Path) -> None:
    token = json.dumps({"access_token": ACCESS}).encode()
    with scripted(authority, tmp_path, _provider(200, token)) as server:
        body = _oauth_body(server.base, authority)
        settings = body["settings"]
        assert isinstance(settings, dict)
        del settings["scope"]
        outcome, _ = _check.check(body, {}, timeout=FAST)

    assert outcome == "ok"
    assert "scope" not in urllib.parse.parse_qs(server.seen[0].body.decode())


def test_a_client_secret_echoed_across_the_cut_of_the_error_text_is_masked(
    authority: Authority, tmp_path: Path
) -> None:
    """The endpoint's words are masked before they are cut, so no part of the secret is left."""
    secret = secrets.token_urlsafe(24)
    said = json.dumps({"error_description": "x" * 290 + secret})
    with scripted(authority, tmp_path, _provider(401, said.encode())) as server:
        body = _oauth_body(server.base, authority)
        body["secret"] = {"client_secret": secret}
        outcome, message = _check.check(body, {}, timeout=FAST)

    assert outcome == "credentials"
    assert "x" * 290 + "***" in message
    assert secret[:8] not in message


def test_an_oauth2_secret_without_client_secret_is_credentials_and_sends_nothing(
    authority: Authority, tmp_path: Path
) -> None:
    with scripted(authority, tmp_path, _provider(200, b"{}")) as server:
        body = _oauth_body(server.base, authority)
        body["secret"] = {"password": SECRET}
        outcome, message = _check.check(body, {}, timeout=FAST)

    assert (outcome, server.seen) == ("credentials", [])
    assert "the secret has no `client_secret`, which auth oauth2 needs" in message


def test_a_secret_without_the_key_its_auth_needs_is_credentials(
    authority: Authority, tmp_path: Path
) -> None:
    with scripted(authority, tmp_path, _status(200)) as server:
        outcome, message = _check.check(
            _api(server.base, authority, {"auth": "bearer"}, {"password": SECRET}), {}, timeout=FAST
        )

    assert (outcome, server.seen) == ("credentials", [])
    assert "the secret has no `token`, which auth bearer needs" in message


def test_an_http_connection_whose_auth_sends_nothing_is_not_sent_a_get(
    authority: Authority, tmp_path: Path
) -> None:
    with scripted(authority, tmp_path, _status(200)) as server:
        outcome, message = _check.check(
            _api(server.base, authority, {"auth": "none"}, {"token": SECRET}), {}, timeout=FAST
        )

    assert (outcome, server.seen) == ("ok", [])
    assert message.endswith("TLS verified")


# --- the SQLSTATE of a refused PostgreSQL login ---------------------------------------------------


def _wire_body(port: int, password: str, tls: str = "disable") -> dict[str, object]:
    """Build a `postgresql` connection on a fake wire server.

    Parameters
    ----------
    port
        The server's port.
    password
        The connection's password.
    tls
        Its ``tls`` setting.

    Returns
    -------
    dict
        The body.
    """
    return connection(
        "postgresql",
        {"host": "localhost", "port": port, "database": "crm"},
        {"user": "reader", "tls": tls},
        secret={"password": password},
    )


@pytest.mark.parametrize(
    ("login", "outcome"),
    [
        (Login("cleartext"), "credentials"),
        (Login("md5"), "credentials"),
        (Login("scram"), "credentials"),
        (
            Login("now", sqlstate="28000", message="kein pg_hba.conf-Eintrag für Host"),
            "credentials",
        ),
        (Login("now", sqlstate="53300", message="zu viele Verbindungen"), "failed"),
    ],
    ids=["cleartext", "md5", "scram", "pg-hba", "too-many"],
)
def test_a_login_refused_in_german_is_classified_by_its_sqlstate(
    login: Login, outcome: str
) -> None:
    """A refused login carries no SQLSTATE in libpq; the check's own login reads it."""
    login.password = secrets.token_urlsafe(12)
    with pg_login_server(login) as port:
        found, message = _check.check(_wire_body(port, "not-the-password"), {}, timeout=FAST)

    assert found == outcome
    assert login.message in message
    assert login.seen == ["reader", "reader"]


@pytest.mark.parametrize("auth", ["cleartext", "md5", "scram"])
def test_a_right_password_with_a_missing_database_is_no_credentials_failure(auth: str) -> None:
    """The fake server checks each answer: a wrong SCRAM proof or MD5 hash would read 28P01."""
    password = secrets.token_urlsafe(12)
    login = Login(
        auth, password=password, sqlstate="3D000", message="Datenbank »crm« existiert nicht"
    )
    with pg_login_server(login) as port:
        found, message = _check.check(_wire_body(port, password), {}, timeout=FAST)
        address = {"host": "127.0.0.1", "port": port, "database": "crm"}
        state = _check.postgres_refusal(
            address, {"user": "reader", "tls": "disable"}, password, {}, FAST
        )

    assert found == "failed"
    assert "Datenbank »crm« existiert nicht" in message
    assert state == "3D000"


def test_the_second_login_runs_over_the_connections_tls(
    authority: Authority, tmp_path: Path
) -> None:
    cert, key = issue_leaf(authority, tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    login = Login("scram", tls=context)
    settings = {"user": "reader", "ca_certificate": authority.ca_file.read_text()}
    with pg_login_server(login) as port:
        address = {"host": "localhost", "port": port, "database": "crm"}

        assert _check.postgres_refusal(address, settings, "wrong", {}, FAST) == "28P01"


def test_the_second_login_never_runs_over_tls_it_does_not_trust(
    authority: Authority, tmp_path: Path
) -> None:
    """No CA for the server's certificate: the handshake fails, and no password is sent."""
    cert, key = issue_leaf(authority, tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    login = Login("cleartext", tls=context)
    with pg_login_server(login) as port:
        address = {"host": "localhost", "port": port, "database": "crm"}

        assert _check.postgres_refusal(address, {"user": "reader"}, "wrong", {}, FAST) is None

    assert login.seen == []


def test_the_second_login_finds_nothing_without_a_postgresql_server() -> None:
    """A closed port, a server without TLS, or an accepted login: no SQLSTATE."""
    settings = {"user": "reader"}
    closed = {"host": "127.0.0.1", "port": closed_port(), "database": "crm"}
    assert _check.postgres_refusal(closed, settings, "x", {}, FAST) is None
    with pg_login_server(Login("now")) as port:
        address = {"host": "127.0.0.1", "port": port, "database": "crm"}
        assert _check.postgres_refusal(address, settings, "x", {}, FAST) is None
    with pg_login_server(Login("cleartext", password="x", sqlstate=None)) as port:
        address = {"host": "127.0.0.1", "port": port, "database": "crm"}
        assert (
            _check.postgres_refusal(address, {**settings, "tls": "disable"}, "x", {}, FAST) is None
        )


def _auth(code: int, data: bytes = b"") -> bytes:
    """Frame an ``Authentication*`` message.

    Parameters
    ----------
    code
        Its code.
    data
        What follows.

    Returns
    -------
    bytes
        The message.
    """
    body = struct.pack("!i", code) + data
    return b"R" + struct.pack("!i", len(body) + 4) + body


_FIRST = b"r=nonce,s=" + base64.b64encode(b"salt") + b",i="


@pytest.mark.parametrize(
    "answers",
    [
        b"Z\x00\x00\x00\x05I",
        b"R\x00\x00\x00\x05\x00",
        _auth(7),
        _auth(11, _FIRST + b"4096"),
        _auth(10, b"SCRAM-SHA-256-PLUS\x00\x00"),
        _auth(10, b"SCRAM-SHA-256\x00\x00") + _auth(11, _FIRST + b"1000001"),
        _auth(10, b"SCRAM-SHA-256\x00\x00") + _auth(11, b"r=nonce,i=4096"),
        b"E" + struct.pack("!i", 1 << 20),
        b"E\x00\x00",
        b"E" + struct.pack("!i", 20) + b"C28P0",
        error_response("bad", "words"),
        b"E\x00\x00\x00\x0bMwords\x00\x00",
        _auth(12) * 65,
        _auth(0)
        + b"S\x00\x00\x00\x16server_version\x0018\x00"
        + b"K\x00\x00\x00\x0c\x00\x00\x00\x01\x00\x00\x00\x02"
        + b"Z\x00\x00\x00\x05I",
    ],
    ids=[
        "not-auth",
        "short-auth",
        "gss",
        "continue-unasked",
        "plus-only",
        "too-many-rounds",
        "no-salt",
        "too-long",
        "cut-short",
        "body-cut-short",
        "bad-sqlstate",
        "no-sqlstate",
        "endless",
        "accepted",
    ],
)
def test_an_odd_login_exchange_finds_no_sqlstate(answers: bytes) -> None:
    client, server = socket.socketpair()
    with client, server:
        server.sendall(answers)
        server.shutdown(socket.SHUT_WR)
        client.settimeout(FAST)

        assert _check._login_state(client, "reader", "crm", "pw") is None


def test_a_sasl_final_message_is_followed_by_the_answer() -> None:
    client, server = socket.socketpair()
    with client, server:
        server.sendall(_auth(12, b"v=x") + error_response("28P01", "nein"))
        client.settimeout(FAST)

        assert _check._login_state(client, "reader", "crm", "pw") == "28P01"


@pytest.mark.parametrize(
    ("password", "prepared"),
    [
        ("plain ascii\x07", "plain ascii\x07"),
        ("I\u00adX", "IX"),
        ("\u2168", "IX"),
        ("a\u00a0b", "a b"),
        ("\u00ad", "\u00ad"),
        ("\u00e9\u0007", "\u00e9\u0007"),
        ("\u0627\u0031", "\u0627\u0031"),
        ("\u0627\u0628", "\u0627\u0628"),
    ],
    ids=["ascii", "mapped-to-nothing", "nfkc", "space", "empty", "prohibited", "bidi", "rtl"],
)
def test_saslprep_prepares_a_password_as_postgresql_does(password: str, prepared: str) -> None:
    """RFC 4013; a password it refuses is used as it is, as PostgreSQL stores it then."""
    assert _check._saslprep(password) == prepared
