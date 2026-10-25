"""The egress proxy: CONNECT with a valid token to one of its host:port pairs, and nothing else."""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import logging
import select
import socket
import socketserver
import struct
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fake_launcher import FakeLauncher
from launcher_fakes import settings

from lakana_core.projects.package_sources import RegisteredIndex
from lakana_worker.docker_environments import DockerEnvironments
from lakana_worker.index_credentials import IndexAccess
from lakana_worker.launcher import egress
from lakana_worker.launcher.egress import (
    EgressServer,
    EgressSettings,
    SettingsError,
    connected_networks,
    load_settings,
)
from lakana_worker.launcher.egress_token import ensure_key, mint, rotate_key
from lakana_worker.launcher.requests import BuildRequest, build_targets, parse_request

LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
_GOOD = "eth1\t0800CF0A\t00000000\t0001\t0\t0\t0\tF8FFFFFF\n"
_GOOD6 = (
    "fd000000000000000000000000000000 40 00000000000000000000000000000000 00 "
    "00000000000000000000000000000000 00000100 00000001 00000000 00000001 eth0\n"
)


@pytest.fixture(autouse=True)
def _no_host_routing_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every proxy of this module behave as on a host without ``/proc`` (macOS).

    A test that needs attached networks writes its own tables; none reads the host's.

    Parameters
    ----------
    tmp_path
        Where no table is.
    monkeypatch
        Points the default tables there.
    """
    monkeypatch.setattr(egress, "_ROUTE", tmp_path / "no-route")
    monkeypatch.setattr(egress, "_ROUTE6", tmp_path / "no-ipv6-route")


class _Echo(socketserver.BaseRequestHandler):
    """Echoes what it receives, standing in for an index."""

    def handle(self) -> None:
        """Echo until the peer closes."""
        while data := self.request.recv(4096):
            self.request.sendall(data)


@dataclass
class Running:
    """A proxy and an echo "index" on loopback.

    Parameters
    ----------
    port
        The proxy's port.
    index
        The index's port.
    key
        The signing key.
    """

    port: int
    index: int
    key: bytes

    def token(
        self,
        targets: list[tuple[str, int]] | None = None,
        *,
        expires: int = 2**40,
        network: str = "127.0.0.0/8",
        key: bytes | None = None,
    ) -> str:
        """Mint a token, by default for the index on localhost.

        Parameters
        ----------
        targets
            Host and port pairs; the index on ``localhost`` when left out.
        expires
            Epoch seconds.
        network
            The client network it is bound to.
        key
            Another signing key, for a forged token.

        Returns
        -------
        str
            The token.
        """
        return mint(
            key or self.key,
            targets if targets is not None else [("localhost", self.index)],
            expires,
            purpose="build",
            network=ipaddress.ip_network(network),
        )


def _serve(server: socketserver.BaseServer) -> None:
    """Serve on a daemon thread.

    Parameters
    ----------
    server
        The server.
    """
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()


@pytest.fixture
def index() -> Iterator[int]:
    """Run an echo "index" on loopback.

    Yields
    ------
    int
        Its port.
    """
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Echo)
    server.daemon_threads = True
    _serve(server)
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _write_refused(tmp_path: Path, ranges: object) -> None:
    """Write the refused ranges as the launcher does, next to the key.

    Parameters
    ----------
    tmp_path
        The key's directory.
    ranges
        The ranges; `None` removes the file.
    """
    path = tmp_path / "refused.json"
    if ranges is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(json.dumps({"refused": ranges}), encoding="utf-8")


def _proxy(tmp_path: Path, *, ranges: object = (), **changes: object) -> EgressServer:
    """Start a proxy on loopback with a key and routing tables of its own under `tmp_path`.

    Parameters
    ----------
    tmp_path
        For the key.
    ranges
        The refused ranges the launcher wrote; none by default, `None` for no file.
    **changes
        Settings to replace.

    Returns
    -------
    EgressServer
        The running proxy.
    """
    ensure_key(tmp_path / "egress.key")
    _write_refused(tmp_path, None if ranges is None else list(cast("tuple[str, ...]", ranges)))
    route = tmp_path / "proxy-route"
    route.write_text(_HEADER + _GOOD, encoding="ascii")  # attached: 10.207.0.8/29 only
    values: dict[str, object] = {
        "key": tmp_path / "egress.key",
        "listen": ("127.0.0.1", 0),
        "reachable": frozenset({LOOPBACK}),
        "route": route,
        "route6": tmp_path / "proxy-no-ipv6-route",
    }
    values.update(changes)
    server = EgressServer(EgressSettings(**values))  # ty: ignore[invalid-argument-type]
    _PRETENDED.clear()  # a port recorded by an earlier test may be any connection's now
    _pretending(server)
    _serve(server)
    return server


@pytest.fixture
def proxy(tmp_path: Path, index: int) -> Iterator[Running]:
    """Run the proxy, which may reach loopback (as a test needs), and an index.

    Parameters
    ----------
    tmp_path
        For the key.
    index
        The index's port.

    Yields
    ------
    Running
        The proxy, the index and the key.
    """
    server = _proxy(tmp_path)
    yield Running(server.server_address[1], index, ensure_key(tmp_path / "egress.key"))
    server.shutdown()
    server.server_close()


def _head(connection: socket.socket) -> bytes:
    """Read an answer's head.

    Parameters
    ----------
    connection
        The connection.

    Returns
    -------
    bytes
        Everything up to the empty line; what arrived before the connection closed otherwise.
    """
    head = b""
    while b"\r\n\r\n" not in head:
        byte = connection.recv(1)
        if not byte:
            break
        head += byte
    return head


def _connect(
    port: int, target: str, token: str | None, extra: str = ""
) -> tuple[bytes, socket.socket]:
    """Send a CONNECT and read the proxy's answer head.

    Parameters
    ----------
    port
        The proxy.
    target
        ``host:port``.
    token
        The token, or `None` for no credential.
    extra
        More header lines, each ending in CRLF.

    Returns
    -------
    tuple
        The status line and the open connection.
    """
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    auth = ""
    if token is not None:
        credential = base64.b64encode(f"lakana:{token}".encode()).decode()
        auth = f"Proxy-Authorization: Basic {credential}\r\n"
    connection.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}{extra}\r\n".encode())
    return _head(connection).split(b"\r\n", 1)[0], connection


def _status(port: int, target: str, token: str | None, extra: str = "") -> bytes:
    """Send a CONNECT and return only the status code.

    Parameters
    ----------
    port
        The proxy.
    target
        ``host:port``.
    token
        The token, or `None`.
    extra
        More header lines.

    Returns
    -------
    bytes
        The code, such as ``b"403"``.
    """
    answer, connection = _connect(port, target, token, extra)
    connection.close()
    return answer.split()[1]


def test_a_token_for_the_host_and_port_opens_a_tunnel(proxy: Running) -> None:
    """A client with ``localhost:<index>`` in its token reaches it, bytes both ways."""
    status, connection = _connect(proxy.port, f"localhost:{proxy.index}", proxy.token())

    with connection:
        assert status == b"HTTP/1.1 200 Connection established"
        connection.sendall(b"ping")
        assert connection.recv(4) == b"ping"


def test_bytes_sent_right_after_the_head_reach_the_index(proxy: Running) -> None:
    """A client that does not wait for the 200 loses nothing."""
    connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    credential = base64.b64encode(f"lakana:{proxy.token()}".encode()).decode()
    connection.sendall(
        f"CONNECT localhost:{proxy.index} HTTP/1.1\r\nProxy-Authorization: Basic {credential}"
        "\r\n\r\nearly".encode()
    )
    with connection:
        assert _head(connection).startswith(b"HTTP/1.1 200 ")
        assert connection.recv(5) == b"early"


@pytest.mark.parametrize(
    ("target", "targets", "network", "status"),
    [
        ("example.org:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"403"),
        ("localhost:1", [("localhost", "{port}")], "127.0.0.0/8", b"403"),
        ("localhost:{port}", [("localhost", 443)], "127.0.0.0/8", b"403"),
        ("localhost:{port}", [], "127.0.0.0/8", b"403"),
        ("localhost:{port}", [("localhost", "{port}")], "10.207.0.8/29", b"403"),
        ("LOCALHOST:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"200"),
        ("localhost.:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("localhost:0{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("localhost:+{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("localhost", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("[::1]:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("local\x1bhost:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
        ("user@localhost:{port}", [("localhost", "{port}")], "127.0.0.0/8", b"400"),
    ],
    ids=[
        "host-not-in-token",
        "port-not-in-token",
        "host-with-another-port",
        "no-targets",
        "token-of-another-network",
        "case-insensitive-host",
        "trailing-dot",
        "leading-zero-port",
        "signed-port",
        "no-port",
        "ip-v6-literal",
        "control-character",
        "userinfo",
    ],
)
def test_everything_outside_the_token_is_refused(
    proxy: Running, target: str, targets: list[tuple[str, object]], network: str, status: bytes
) -> None:
    """Review focus: exactly the token's host:port pairs, from the token's own network.

    Parameters
    ----------
    proxy
        The running proxy.
    target
        The CONNECT target.
    targets
        The token's pairs; ``{port}`` is the index's port.
    network
        The client network the token is bound to.
    status
        The expected status code.
    """
    pairs = [(h, int(str(p).format(port=proxy.index))) for h, p in targets]
    token = proxy.token(pairs, network=network)

    assert _status(proxy.port, target.format(port=proxy.index), token) == status


@pytest.mark.parametrize(
    "credential",
    [
        None,
        "forged",
        "expired",
        "Bearer {token}",
        "Basic !!!",
        "Basic {b64}",
    ],
)
def test_a_missing_or_bad_credential_is_asked_for(proxy: Running, credential: str | None) -> None:
    """407 with a challenge, whatever is wrong with the credential.

    Parameters
    ----------
    proxy
        The running proxy.
    credential
        What goes in the header.
    """
    target = f"localhost:{proxy.index}"
    if credential == "forged":
        status = _status(proxy.port, target, proxy.token(key=b"z" * 32))
    elif credential == "expired":
        status = _status(proxy.port, target, proxy.token(expires=int(time.time()) - 1))
    elif credential is None:
        status = _status(proxy.port, target, None)
    else:
        value = credential.format(
            token=proxy.token(), b64=base64.b64encode(proxy.token().encode()).decode()
        )
        status = _status(proxy.port, target, None, f"Proxy-Authorization: {value}\r\n")
    assert status == b"407"


def test_anything_but_connect_is_refused(proxy: Running) -> None:
    """A plain GET through the proxy is not a tunnel to an https index."""
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as plain:
        plain.sendall(b"GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert _head(plain).split()[1] == b"405"


@pytest.mark.parametrize(
    "head",
    [
        "CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {c}\r\n"
        "Proxy-Authorization: Basic {c}\r\n\r\n",
        "CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {c}\r\n folded\r\n\r\n",
        "CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {c}\nX: y\r\n\r\n",
        "CONNECT {target} HTTP/1.1\r\nno colon\r\nProxy-Authorization: Basic {c}\r\n\r\n",
        "CONNECT {target} HTTP/1.1\r\n"
        + "X-A: b\r\n" * 70
        + "Proxy-Authorization: Basic {c}\r\n\r\n",
        "CONNECT {target} HTTP/1.1\r\nX-A: " + "b" * 20000 + "\r\n\r\n",
        "CONNECT {target} HTTP/1.1 extra\r\nProxy-Authorization: Basic {c}\r\n\r\n",
        "CONNECT {target} SPDY/3\r\nProxy-Authorization: Basic {c}\r\n\r\n",
    ],
    ids=[
        "duplicate-credential",
        "folded-line",
        "bare-line-feed",
        "line-without-colon",
        "too-many-headers",
        "too-long",
        "four-words",
        "not-http",
    ],
)
def test_an_ambiguous_head_is_refused(proxy: Running, head: str) -> None:
    """Nothing the proxy and a client could read in two ways gets a tunnel.

    Parameters
    ----------
    proxy
        The running proxy.
    head
        The request head.
    """
    credential = base64.b64encode(f"lakana:{proxy.token()}".encode()).decode()
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as connection:
        connection.sendall(head.format(target=f"localhost:{proxy.index}", c=credential).encode())
        assert _head(connection).split()[1] == b"400"


def test_a_slow_head_is_cut_off(tmp_path: Path) -> None:
    """A client that drips its head byte by byte gets 408 at the deadline, not per byte."""
    server = _proxy(tmp_path, head_seconds=1.0)
    try:
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as slow:
            started = time.monotonic()
            for byte in b"CONNECT localhost:1 HTTP/1.1\r\nX-Drip: " + b"a" * 100:
                if select.select([slow], [], [], 0.2)[0] or time.monotonic() - started > 4:
                    break
                slow.sendall(bytes([byte]))
            answer = _head(slow)
            elapsed = time.monotonic() - started
        assert answer.split()[1] == b"408"
        assert elapsed < 3
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.slow
def test_a_tunnel_ends_when_its_token_expires(proxy: Running) -> None:
    """A token's time bounds the tunnels it opened, not only their start."""
    status, connection = _connect(
        proxy.port, f"localhost:{proxy.index}", proxy.token(expires=int(time.time()) + 2)
    )
    with connection:
        assert status.split()[1] == b"200"
        connection.settimeout(6)
        assert connection.recv(1) == b""


def test_a_full_proxy_says_it_is_busy(tmp_path: Path, index: int) -> None:
    """Past `max_connections`, a client gets 503 at once instead of a hanging connection."""
    server = _proxy(tmp_path, max_connections=1)
    key = ensure_key(tmp_path / "egress.key")
    running = Running(server.server_address[1], index, key)
    try:
        status, first = _connect(running.port, f"localhost:{index}", running.token())
        with first:
            assert status.split()[1] == b"200"
            assert _status(running.port, f"localhost:{index}", running.token()) == b"503"
    finally:
        server.shutdown()
        server.server_close()


_PRETENDED: dict[int, str] = {}
"""Client port to the address a connection pretends to come from (see `_from`)."""


def _from(address: str, port: int) -> socket.socket:
    """Open a connection to the proxy that the proxy counts as coming from `address`.

    The connection comes from 127.0.0.1, the one loopback address every platform has (macOS
    has no other); its port is recorded with `address`, and `_proxy` makes the proxy read the
    peer as `address`, so the client network is still worked out from it, routing table and
    all.

    Parameters
    ----------
    address
        The address to count the connection against.
    port
        The proxy's port.

    Returns
    -------
    socket.socket
        The connection; nothing is sent on it.
    """
    connection = socket.socket()
    connection.settimeout(5)
    connection.bind(("127.0.0.1", 0))
    _PRETENDED[connection.getsockname()[1]] = address
    connection.connect(("127.0.0.1", port))
    return connection


def _pretending(server: EgressServer) -> None:
    """Make a proxy read a connection's peer as the address `_from` recorded for its port.

    Parameters
    ----------
    server
        The proxy.
    """
    real = server.client_network

    def client_network(client_address: object) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
        """Swap the peer's host for the pretended one, then work out the network as usual.

        Parameters
        ----------
        client_address
            The peer.

        Returns
        -------
        IPv4Network or IPv6Network
            The client network of the pretended peer.
        """
        host, port = cast("tuple[str, int]", client_address)
        return real((_PRETENDED.get(port, host), port))

    server.client_network = client_network  # ty: ignore[invalid-assignment]


def _connect_from(address: str, port: int, target: str, token: str) -> tuple[bytes, bytes]:
    """Send a CONNECT from a loopback address and read the answer.

    Parameters
    ----------
    address
        The client's address.
    port
        The proxy's port.
    target
        ``host:port``.
    token
        The token.

    Returns
    -------
    tuple
        The status code and the first bytes of the body (empty for a tunnel).
    """
    credential = base64.b64encode(f"lakana:{token}".encode()).decode()
    with _from(address, port) as connection:
        connection.sendall(
            f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {credential}\r\n\r\n".encode()
        )
        head = _head(connection)
        status = head.split(b"\r\n", 1)[0].split()[1]
        body = b"" if status == b"200" else connection.recv(4096)
    return status, body


def _held(server: EgressServer, count: int, address: str) -> list[socket.socket]:
    """Open `count` connections from one address that send nothing, and wait until each is held.

    Parameters
    ----------
    server
        The proxy.
    count
        How many.
    address
        The client's address.

    Returns
    -------
    list
        The open connections.
    """
    before = server.slots_held()
    connections = [_from(address, server.server_address[1]) for _ in range(count)]
    deadline = time.monotonic() + 5
    while server.slots_held() < before + count:
        assert time.monotonic() < deadline, "the proxy never took the connections"
        time.sleep(0.01)
    return connections


def test_one_build_holding_its_cap_cannot_starve_another(tmp_path: Path, index: int) -> None:
    """#302: a network at its cap gets 503 naming the cause; another network still gets through."""
    server = _proxy(tmp_path, max_connections=8, max_connections_per_network=3, head_seconds=30.0)
    token = Running(server.server_address[1], index, ensure_key(tmp_path / "egress.key")).token()
    port = server.server_address[1]
    try:
        idle = _held(server, 3, "127.0.0.2")
        try:
            status, body = _connect_from("127.0.0.2", port, f"localhost:{index}", token)
            other, _ = _connect_from("127.0.0.3", port, f"localhost:{index}", token)
        finally:
            for connection in idle:
                connection.close()
    finally:
        server.shutdown()
        server.server_close()

    assert status == b"503"
    assert b"already holds 3 connections" in body
    assert b"one build or task may hold" in body
    assert b"LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK" in body
    assert other == b"200"


def test_the_cap_counts_every_client_of_one_attached_network_together(
    tmp_path: Path, index: int
) -> None:
    """#302: the cap belongs to the build's network, not to one address in it."""
    route = tmp_path / "loopback-route"
    route.write_text(_HEADER + "lo\t0000007F\t00000000\t0001\t0\t0\t0\tFCFFFFFF\n")  # /30
    server = _proxy(tmp_path, max_connections_per_network=2, head_seconds=30.0, route=route)
    token = Running(server.server_address[1], index, ensure_key(tmp_path / "egress.key")).token()
    port = server.server_address[1]
    try:
        idle = _held(server, 2, "127.0.0.2")
        try:
            same, _ = _connect_from("127.0.0.3", port, f"localhost:{index}", token)
            outside, _ = _connect_from("127.0.0.5", port, f"localhost:{index}", token)
        finally:
            for connection in idle:
                connection.close()
    finally:
        server.shutdown()
        server.server_close()

    assert (same, outside) == (b"503", b"200")


def test_a_full_proxy_is_busy_for_a_network_below_its_cap(tmp_path: Path, index: int) -> None:
    """The total still applies when the client's network has room."""
    server = _proxy(tmp_path, max_connections=1, max_connections_per_network=1, head_seconds=30.0)
    token = Running(server.server_address[1], index, ensure_key(tmp_path / "egress.key")).token()
    try:
        idle = _held(server, 1, "127.0.0.2")
        try:
            status, body = _connect_from(
                "127.0.0.3", server.server_address[1], f"localhost:{index}", token
            )
        finally:
            for connection in idle:
                connection.close()
    finally:
        server.shutdown()
        server.server_close()

    assert status == b"503" and b"busy" in body


def test_the_client_network_of_a_mapped_or_unknown_connection(tmp_path: Path) -> None:
    """An IPv4-mapped address counts as its IPv4 address; an unseen connection is looked up."""
    server = _proxy(tmp_path)
    try:
        mapped = server.client_network(("::ffff:127.0.0.2", 1234))
        looked_up = server.network_of(object(), ("127.0.0.9", 1234))
        server._forget(object())  # a connection without a ticket gives nothing back
    finally:
        server.shutdown()
        server.server_close()

    assert mapped == ipaddress.ip_network("127.0.0.2/32")
    assert looked_up == ipaddress.ip_network("127.0.0.9/32")
    assert server.threads_of(looked_up) == 0


def test_a_flood_from_one_network_takes_only_its_own_threads(tmp_path: Path, index: int) -> None:
    """#302: past its cap and a few answers, a network's connections are closed without a thread."""
    server = _proxy(tmp_path, max_connections=8, max_connections_per_network=2, head_seconds=30.0)
    token = Running(server.server_address[1], index, ensure_key(tmp_path / "egress.key")).token()
    port = server.server_address[1]
    try:
        flood = [_from("127.0.0.2", port) for _ in range(40)]
        try:
            time.sleep(0.3)
            assert server.threads_of(ipaddress.ip_network("127.0.0.2/32")) <= 2 + 4
            other, _ = _connect_from("127.0.0.3", port, f"localhost:{index}", token)
        finally:
            for connection in flood:
                connection.close()
    finally:
        server.shutdown()
        server.server_close()

    assert other == b"200"


def test_a_flood_beyond_the_spare_threads_is_closed_without_a_thread(tmp_path: Path) -> None:
    """When every thread is taken, a new connection is closed at once."""
    server = _proxy(tmp_path, max_connections=1)
    taken = 0
    try:
        while server.threads.acquire(blocking=False):
            taken += 1
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as late:
            assert late.recv(1) == b""
    finally:
        for _ in range(taken):
            server.threads.release()
        server.shutdown()
        server.server_close()

    assert taken == 65


@pytest.mark.parametrize(
    ("host", "reachable"),
    [
        ("localhost", frozenset()),
        ("127.0.0.1", frozenset()),
        ("169.254.169.254", frozenset({LOOPBACK})),
        ("0.0.0.0", frozenset({LOOPBACK})),
        ("100.100.100.200", frozenset({LOOPBACK})),
        ("224.0.0.1", frozenset({LOOPBACK})),
    ],
    ids=[
        "loopback-name",
        "loopback-literal",
        "metadata",
        "unspecified",
        "alibaba-metadata",
        "multicast",
    ],
)
def test_a_signed_host_on_a_forbidden_address_is_refused(
    tmp_path: Path, index: int, host: str, reachable: frozenset[object]
) -> None:
    """Loopback, link-local (cloud metadata), multicast: never, whatever the token says.

    Parameters
    ----------
    tmp_path
        For the key.
    index
        The index's port.
    host
        The signed host.
    reachable
        Networks the test lets the proxy reach.
    """
    server = _proxy(tmp_path, reachable=reachable)
    running = Running(server.server_address[1], index, ensure_key(tmp_path / "egress.key"))
    try:
        answer, connection = _connect(
            running.port, f"{host}:{index}", running.token([(host, index)])
        )
        body = connection.recv(4096)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()

    assert answer.split()[1] == b"403"
    assert b"not reachable" in body


def test_a_connected_network_is_read_from_the_routing_table(tmp_path: Path) -> None:
    """The networks the proxy's container is attached to: its Compose network and every build's."""
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "eth1\t0800CF0A\t00000000\t0001\t0\t0\t0\tF8FFFFFF\t0\t0\t0\n",
        encoding="ascii",
    )
    route6 = tmp_path / "ipv6_route"
    route6.write_text(
        "fd000000000000000000000000000000 40 00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00000100 00000001 00000000 00000001 eth0\n"
        "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
        "fd000000000000000000000000000001 00000400 00000001 00000000 00000003 eth0\n",
        encoding="ascii",
    )

    found = connected_networks(route, route6)

    assert found == frozenset(
        {
            ipaddress.ip_network("172.18.0.0/16"),
            ipaddress.ip_network("10.207.0.8/29"),
            ipaddress.ip_network("fd00::/64"),
        }
    )
    assert connected_networks(tmp_path / "missing", route6) is None
    assert connected_networks(route, tmp_path / "missing6") == frozenset(
        {ipaddress.ip_network("172.18.0.0/16"), ipaddress.ip_network("10.207.0.8/29")}
    )


@pytest.mark.parametrize(
    ("route", "route6"),
    [
        (_HEADER + "eth0\tZZZZ\t00000000\t0001\t0\t0\t0\t0000FFFF\n" + _GOOD, _GOOD6),
        (_HEADER + "short\n" + _GOOD, _GOOD6),
        (_HEADER + _GOOD, "zz 40 x x 00 x x x x eth0\n"),
        (_HEADER + _GOOD, "fd00 40\n"),
    ],
    ids=["bad-ipv4-field", "short-ipv4-line", "bad-ipv6-field", "short-ipv6-line"],
)
def test_one_route_line_not_understood_fails_closed(
    tmp_path: Path, route: str, route6: str
) -> None:
    """A line the parser cannot read might be the attached network it would have refused.

    Parameters
    ----------
    tmp_path
        For the tables.
    route
        The IPv4 table.
    route6
        The IPv6 table.
    """
    (tmp_path / "route").write_text(route, encoding="ascii")
    (tmp_path / "ipv6_route").write_text(route6, encoding="ascii")

    assert connected_networks(tmp_path / "route", tmp_path / "ipv6_route") is None
    (tmp_path / "ipv6_route").write_text(_GOOD6 + "\n", encoding="ascii")
    (tmp_path / "route").write_text(_HEADER + _GOOD + "\n", encoding="ascii")
    assert len(connected_networks(tmp_path / "route", tmp_path / "ipv6_route") or ()) == 2


def test_an_unreadable_ipv6_table_fails_closed_and_a_missing_one_means_ipv6_is_off(
    tmp_path: Path,
) -> None:
    """Only a table that does not exist is IPv6 switched off; any other error refuses."""
    (tmp_path / "route").write_text(_HEADER + _GOOD, encoding="ascii")
    (tmp_path / "ipv6_route").mkdir()

    assert connected_networks(tmp_path / "route", tmp_path / "ipv6_route") is None
    assert connected_networks(tmp_path / "route", tmp_path / "missing") == frozenset(
        {ipaddress.ip_network("10.207.0.8/29")}
    )


def test_without_a_routing_table_no_direct_connection_is_made(
    proxy: Running, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed: without its attached networks the proxy cannot tell internal from outside.

    Parameters
    ----------
    proxy
        The running proxy (its index would otherwise be reachable).
    tmp_path
        Where no table is.
    caplog
        The log.
    """
    server = _proxy(tmp_path, route=tmp_path / "missing")
    try:
        answer, connection = _connect(
            server.server_address[1], f"localhost:{proxy.index}", proxy.token()
        )
        body = connection.recv(4096)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()

    assert answer.split()[1] == b"503"
    assert b"routing table" in body
    assert "missing" in caplog.text


def test_an_upstream_proxy_is_asked_to_connect(tmp_path: Path) -> None:
    """`LAKANA_EGRESS__UPSTREAM_PROXY`: the company proxy gets the CONNECT, with its credential."""
    seen: list[bytes] = []

    class Upstream(socketserver.BaseRequestHandler):
        """Records the CONNECT and accepts it."""

        def handle(self) -> None:
            """Read the head, answer 200, echo."""
            seen.append(_head(self.request))
            self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
            while data := self.request.recv(4096):
                self.request.sendall(data)

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    _serve(upstream)
    server = _proxy(
        tmp_path,
        reachable=frozenset(),
        upstream=f"http://corp:p%40ss@127.0.0.1:{upstream.server_address[1]}",
    )
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("pypi.org", 443)], 2**40, purpose="build", network=LOOPBACK)
        status, connection = _connect(server.server_address[1], "pypi.org:443", token)
        with connection:
            connection.sendall(b"ping")
            echoed = connection.recv(4)
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()

    assert status == b"HTTP/1.1 200 Connection established"
    assert echoed == b"ping"
    assert seen[0].startswith(b"CONNECT pypi.org:443 HTTP/1.1\r\n")
    assert b"Proxy-Authorization: Basic " + base64.b64encode(b"corp:p@ss") in seen[0]
    assert b"lakana" not in seen[0]


def test_an_upstream_refusal_is_a_bad_gateway(tmp_path: Path) -> None:
    """The upstream's answer is named; its credential is not."""

    class Refusing(socketserver.BaseRequestHandler):
        """Refuses every CONNECT."""

        def handle(self) -> None:
            """Read the head, answer 403."""
            _head(self.request)
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Refusing)
    upstream.daemon_threads = True
    _serve(upstream)
    server = _proxy(tmp_path, upstream=f"http://corp:secret@127.0.0.1:{upstream.server_address[1]}")
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("pypi.org", 443)], 2**40, purpose="build", network=LOOPBACK)
        answer, connection = _connect(server.server_address[1], "pypi.org:443", token)
        body = connection.recv(4096)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()

    assert answer.split()[1] == b"502"
    assert b"403 Forbidden" in body
    assert b"secret" not in body
    assert "secret" not in repr(server.settings)


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({"LAKANA_EGRESS__UPSTREAM_PROXY": "socks5://proxy:1080"}, "LAKANA_EGRESS__UPSTREAM_PROXY"),
        ({"LAKANA_EGRESS__UPSTREAM_PROXY": "http://proxy:99999"}, "LAKANA_EGRESS__UPSTREAM_PROXY"),
        (
            {"LAKANA_EGRESS__UPSTREAM_PROXY": "http://proxy:3128/path?q"},
            "LAKANA_EGRESS__UPSTREAM_PROXY",
        ),
        ({"LAKANA_EGRESS__LISTEN": "3128"}, "LAKANA_EGRESS__LISTEN"),
        ({"LAKANA_EGRESS__LISTEN": "0.0.0.0:70000"}, "LAKANA_EGRESS__LISTEN"),
        ({"LAKANA_EGRESS__MAX_CONNECTIONS": "0"}, "LAKANA_EGRESS__MAX_CONNECTIONS"),
        (
            {"LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK": "0"},
            "LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK",
        ),
        (
            {"LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK": "x"},
            "LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK",
        ),
        (
            {
                "LAKANA_EGRESS__MAX_CONNECTIONS": "16",
                "LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK": "17",
            },
            "LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK",
        ),
        ({"LAKANA_EGRESS__PORTS": "443"}, "LAKANA_EGRESS__PORTS"),
    ],
)
def test_a_bad_setting_names_itself(environ: dict[str, str], message: str) -> None:
    """Each message names the setting; an unknown one is refused, not ignored.

    Parameters
    ----------
    environ
        The proxy's environment.
    message
        Part of the error.
    """
    with pytest.raises(SettingsError, match=message):
        load_settings(environ)


def test_the_defaults_work_and_the_upstream_credential_stays_out_of_repr() -> None:
    """Without settings: port 3128 on every address, the key in the shared volume."""
    settings = load_settings({"LAKANA_EGRESS__UPSTREAM_PROXY": "http://u:pw@proxy:8080"})

    assert settings.listen == ("0.0.0.0", 3128)  # nosec B104
    assert settings.key == Path("/run/lakana-egress/egress.key")
    assert settings.upstream == "http://u:pw@proxy:8080"
    assert "pw" not in repr(settings)
    assert settings.reachable == frozenset()
    assert (settings.max_connections, settings.max_connections_per_network) == (256, 32)
    assert (
        load_settings(
            {"LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK": "64"}
        ).max_connections_per_network
        == 64
    )


def test_an_unreachable_destination_is_a_bad_gateway(proxy: Running) -> None:
    """A signed host that refuses the connection, or does not resolve, gets 502 with the cause."""
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()

    assert _status(proxy.port, f"localhost:{port}", proxy.token([("localhost", port)])) == b"502"
    missing = "no-such-host.invalid"
    assert _status(proxy.port, f"{missing}:443", proxy.token([(missing, 443)])) == b"502"


def test_an_unreachable_upstream_is_a_bad_gateway(tmp_path: Path) -> None:
    """The message says the upstream proxy cannot be reached."""
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    server = _proxy(tmp_path, upstream=f"http://127.0.0.1:{port}")
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("pypi.org", 443)], 2**40, purpose="build", network=LOOPBACK)
        answer, connection = _connect(server.server_address[1], "pypi.org:443", token)
        body = connection.recv(4096)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()

    assert answer.split()[1] == b"502"
    assert b"cannot reach the upstream proxy" in body


def test_a_new_key_is_picked_up_without_a_restart(proxy: Running, tmp_path: Path) -> None:
    """A launcher start replaces the key; at its next connection the proxy reads the new one."""
    old = proxy.token()
    assert _status(proxy.port, f"localhost:{proxy.index}", old) == b"200"
    fresh = rotate_key(tmp_path / "egress.key")  # what a launcher start does

    assert _status(proxy.port, f"localhost:{proxy.index}", old) == b"407"
    new = mint(fresh, [("localhost", proxy.index)], 2**40, purpose="build", network=LOOPBACK)
    assert _status(proxy.port, f"localhost:{proxy.index}", new) == b"200"


def test_without_a_key_every_client_is_asked_for_a_token(proxy: Running, tmp_path: Path) -> None:
    """Until the launcher writes the key, nothing passes."""
    token = proxy.token()
    (tmp_path / "egress.key").unlink()

    assert _status(proxy.port, f"localhost:{proxy.index}", token) == b"407"


def test_a_failed_key_read_is_tried_again_at_the_next_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient read error must not deny every client until the next launcher start.

    Parameters
    ----------
    tmp_path
        For the key.
    monkeypatch
        Makes the first read fail.
    """
    key_file = tmp_path / "egress.key"
    key = ensure_key(key_file)
    settings = EgressSettings(key=key_file, listen=("127.0.0.1", 0))
    server = EgressServer(settings)
    try:
        real = egress.read_key
        monkeypatch.setattr(egress, "read_key", lambda path: None)
        assert server.key() is None
        monkeypatch.setattr(egress, "read_key", real)

        assert server.key() == key  # the file did not change, but nothing was cached
    finally:
        server.server_close()


def test_an_ipv4_mapped_address_is_judged_as_its_ipv4_address() -> None:
    """``::ffff:127.0.0.1`` is loopback."""
    mapped = ipaddress.ip_address("::ffff:127.0.0.1")

    assert egress.forbidden(mapped, frozenset(), frozenset())
    assert not egress.forbidden(mapped, frozenset(), frozenset({LOOPBACK}))
    assert not egress.forbidden(ipaddress.ip_address("10.1.2.3"), frozenset(), frozenset())


def test_a_listen_address_is_read(tmp_path: Path) -> None:
    """`LAKANA_EGRESS__LISTEN` and `LAKANA_EGRESS__KEY` take effect."""
    settings = load_settings(
        {"LAKANA_EGRESS__LISTEN": "127.0.0.1:3129", "LAKANA_EGRESS__KEY": str(tmp_path / "k")}
    )

    assert (settings.listen, settings.key) == (("127.0.0.1", 3129), tmp_path / "k")


def test_main_stops_with_the_setting_named(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad setting ends the process with status 2 and the reason in the log.

    Parameters
    ----------
    monkeypatch
        Sets the environment.
    caplog
        The log.
    """
    monkeypatch.setenv("LAKANA_EGRESS__MAX_CONNECTIONS", "lots")

    with pytest.raises(SystemExit) as stopped:
        egress.main()

    assert stopped.value.code == 2
    assert "LAKANA_EGRESS__MAX_CONNECTIONS" in caplog.text


def test_main_serves_and_warns_without_a_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """At start the log says when the launcher has not written the key yet.

    Parameters
    ----------
    monkeypatch
        Sets the environment and stops the serving loop.
    caplog
        The log.
    tmp_path
        Where no key is.
    """
    monkeypatch.setenv("LAKANA_EGRESS__KEY", str(tmp_path / "none"))
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    monkeypatch.setenv("LAKANA_EGRESS__LISTEN", f"127.0.0.1:{free.getsockname()[1]}")
    free.close()
    served: list[EgressServer] = []

    def once(self: EgressServer, poll_interval: float = 0.5) -> None:
        """Record the server instead of serving.

        Parameters
        ----------
        self
            The server.
        poll_interval
            Unused.
        """
        served.append(self)

    monkeypatch.setattr(EgressServer, "serve_forever", once)

    egress.main()
    served[0].server_close()

    assert "No key at" in caplog.text


def test_a_routing_table_without_an_attached_network_fails_closed(tmp_path: Path) -> None:
    """Only routes through a gateway: the attached networks are unknown, so nothing direct."""
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\n",
        encoding="ascii",
    )

    assert connected_networks(route, tmp_path / "missing6") is None


def test_a_silent_client_gets_408(tmp_path: Path) -> None:
    """A connection that sends nothing is answered and closed at the head deadline."""
    server = _proxy(tmp_path, head_seconds=0.5)
    try:
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as idle:
            assert _head(idle).split()[1] == b"408"
    finally:
        server.shutdown()
        server.server_close()


def test_an_idle_tunnel_is_closed(proxy: Running, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tunnel nobody uses ends after the idle time, long before its token expires.

    Parameters
    ----------
    proxy
        The running proxy.
    monkeypatch
        Shortens the idle time.
    """
    monkeypatch.setattr(egress, "_IDLE_SECONDS", 0.5)
    status, connection = _connect(proxy.port, f"localhost:{proxy.index}", proxy.token())
    with connection:
        assert status.split()[1] == b"200"
        started = time.monotonic()
        assert connection.recv(1) == b""
        assert time.monotonic() - started < 4


def _reset(connection: socket.socket) -> None:
    """Close a connection with a reset instead of an orderly end.

    Parameters
    ----------
    connection
        The connection.
    """
    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    connection.close()


def test_a_client_that_resets_a_tunnel_ends_it(proxy: Running) -> None:
    """A reset from the client ends the relay; the proxy keeps serving."""
    status, connection = _connect(proxy.port, f"localhost:{proxy.index}", proxy.token())
    assert status.split()[1] == b"200"
    connection.sendall(b"ping")
    time.sleep(0.2)
    _reset(connection)
    time.sleep(0.2)

    assert _status(proxy.port, f"localhost:{proxy.index}", proxy.token()) == b"200"


def test_a_client_that_resets_during_its_head_is_dropped(proxy: Running) -> None:
    """A reset in the middle of the head ends that connection quietly; others are served."""
    connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
    connection.sendall(b"CONNECT localhost:1 HTTP/1.1\r\n")
    time.sleep(0.2)
    _reset(connection)
    time.sleep(0.2)

    assert _status(proxy.port, f"localhost:{proxy.index}", proxy.token()) == b"200"


def test_a_client_that_keeps_sending_after_a_refusal_is_drained_then_closed(
    proxy: Running,
) -> None:
    """The proxy reads at most a bounded amount after its answer, then closes."""
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as connection:
        connection.sendall(b"CONNECT localhost:1 HTTP/1.1\r\nX-A: " + b"b" * 17000)
        assert _head(connection).split()[1] == b"400"
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            for _ in range(20):
                connection.sendall(b"c" * 8192)
            time.sleep(0.3)


def test_a_thread_that_cannot_start_gives_its_slot_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Out of threads: the connection is dropped and logged, and its permit returns.

    Parameters
    ----------
    tmp_path
        For the key.
    monkeypatch
        Makes starting a thread fail, as an exhausted host does.
    caplog
        The log.
    """
    server = _proxy(tmp_path, max_connections=1)

    def no_thread(self: object, request: object, client_address: object) -> None:
        """Fail like a host out of threads.

        Parameters
        ----------
        self
            The server.
        request
            The connection.
        client_address
            Its peer.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(socketserver.ThreadingMixIn, "process_request", no_thread)
    try:
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as late:
            assert late.recv(1) == b""
        taken = 0
        while server.threads.acquire(blocking=False):
            taken += 1
        for _ in range(taken):
            server.threads.release()
    finally:
        server.shutdown()
        server.server_close()

    assert taken == 65
    assert "An egress connection from" in caplog.text


def _upstream(handler: type[socketserver.BaseRequestHandler]) -> socketserver.ThreadingTCPServer:
    """Run an upstream proxy stand-in.

    Parameters
    ----------
    handler
        Its handler.

    Returns
    -------
    socketserver.ThreadingTCPServer
        The running server.
    """
    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    upstream.daemon_threads = True
    _serve(upstream)
    return upstream


def test_an_upstream_without_a_credential_gets_none(tmp_path: Path) -> None:
    """No userinfo in `LAKANA_EGRESS__UPSTREAM_PROXY`: no `Proxy-Authorization` goes upstream."""
    seen: list[bytes] = []

    class Open(socketserver.BaseRequestHandler):
        """Records the CONNECT and accepts it."""

        def handle(self) -> None:
            """Read the head, answer 200."""
            seen.append(_head(self.request))
            self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")

    upstream = _upstream(Open)
    server = _proxy(tmp_path, upstream=f"http://127.0.0.1:{upstream.server_address[1]}")
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("pypi.org", 443)], 2**40, purpose="build", network=LOOPBACK)
        status = _status(server.server_address[1], "pypi.org:443", token)
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()

    assert status == b"200"
    assert b"Proxy-Authorization" not in seen[0]


def test_an_upstream_that_resets_is_a_bad_gateway(tmp_path: Path) -> None:
    """An upstream that drops the connection with a reset: 502, saying it gave no answer."""

    class Resetting(socketserver.BaseRequestHandler):
        """Reads the CONNECT and resets the connection."""

        def handle(self) -> None:
            """Read the head, reset."""
            _head(self.request)
            _reset(self.request)

    upstream = _upstream(Resetting)
    server = _proxy(tmp_path, upstream=f"http://127.0.0.1:{upstream.server_address[1]}")
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("pypi.org", 443)], 2**40, purpose="build", network=LOOPBACK)
        answer, connection = _connect(server.server_address[1], "pypi.org:443", token)
        body = connection.recv(4096)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()

    assert answer.split()[1] == b"502"
    assert b"no answer" in body


def test_main_serves_quietly_with_a_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """With the launcher's key in place, the start-up log has no warning.

    Parameters
    ----------
    monkeypatch
        Sets the environment and stops the serving loop.
    caplog
        The log.
    tmp_path
        For the key.
    """
    caplog.set_level(logging.INFO, logger="lakana_worker.launcher.egress")
    ensure_key(tmp_path / "egress.key")
    monkeypatch.setenv("LAKANA_EGRESS__KEY", str(tmp_path / "egress.key"))
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    monkeypatch.setenv("LAKANA_EGRESS__LISTEN", f"127.0.0.1:{free.getsockname()[1]}")
    free.close()
    served: list[EgressServer] = []
    monkeypatch.setattr(EgressServer, "serve_forever", lambda self, *a: served.append(self))

    egress.main()
    served[0].server_close()

    assert "listens on" in caplog.text
    assert "No key at" not in caplog.text


def test_a_refused_build_is_told_what_to_do(proxy: Running) -> None:
    """The 403 says how to make an index reachable; no port is special any more (#301)."""
    answer, connection = _connect(proxy.port, "pkgs.example.org:8443", proxy.token())
    body = connection.recv(4096).decode()
    connection.close()

    assert answer.split()[1] == b"403"
    assert "pkgs.example.org:8443 is not a destination this build may reach" in body
    assert "lakana index add" in body
    assert "LAKANA_RUN__INDEX_URL" in body
    assert "#301" not in body and "credential" not in body


def test_an_index_on_another_port_without_a_credential_is_reachable(
    tmp_path: Path, proxy: Running
) -> None:
    """#301 end to end: worker request, launcher parse, token, proxy; other ports stay closed."""
    launcher = FakeLauncher(
        scripts={
            "status": lambda request, on_log: {"complete": False},
            "build": lambda request, on_log: {"complete": True},
        }
    )
    code = tmp_path / "attempt-abc_12" / "code"
    code.mkdir(parents=True)
    (code / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    index = RegisteredIndex("odd", f"https://localhost:{proxy.index}/simple")
    environments = DockerEnvironments(
        client=launcher,  # ty: ignore[invalid-argument-type]
        owner="worker-1",
        memory_mb=2048,
        cpus=1.0,
        pids=256,
        build_timeout=600.0,
        max_bytes=1 << 30,
    )
    environments.acquire(
        "a" * 64,
        code,
        lambda chunk: None,
        lambda: [IndexAccess("odd", index.url, "dev")],
        python="3.13",
        attempt="attempt-abc_12",
        used=(index,),
        project_id=uuid.UUID(int=1),
    )
    sent = json.dumps({**launcher.requests[1], "token": "t" * 64}).encode()
    build = parse_request(sent, token="t" * 64, settings=settings(tmp_path))
    assert isinstance(build, BuildRequest) and build.indexes == ()
    token = proxy.token(build_targets(build))
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        undeclared = spare.getsockname()[1]

    answer, connection = _connect(proxy.port, f"localhost:{proxy.index}", token)
    connection.sendall(b"ping")
    echoed = connection.recv(4)
    connection.close()

    assert answer.split()[1] == b"200" and echoed == b"ping"
    assert _status(proxy.port, f"localhost:{undeclared}", token) == b"403"
    assert _status(proxy.port, "localhost:443", token) == b"403"


def _refusal(server: EgressServer, target: str, token: str) -> tuple[bytes, str]:
    """Send a CONNECT that the proxy refuses and read its status and reason.

    Parameters
    ----------
    server
        The proxy.
    target
        ``host:port``.
    token
        The token.

    Returns
    -------
    tuple
        The status code and the one-line reason.
    """
    answer, connection = _connect(server.server_address[1], target, token)
    with connection:
        body = connection.recv(4096)
    return answer.split()[1], body.decode().strip()


@pytest.mark.parametrize(
    ("purpose", "reason"),
    [
        (
            "task",
            "api.other.com:443 is not a destination this task may reach; declare it in "
            "@asset(connections=[...]) and create it with lakana connection create",
        ),
        (
            "connection-test",
            "api.other.com:443 is not a destination this connection test may reach; check the "
            "connection's address with lakana connection show",
        ),
    ],
)
def test_an_unlisted_host_is_refused_with_the_purposes_next_step(
    tmp_path: Path, purpose: str, reason: str
) -> None:
    """A task or a connection test hears what to do about a host its token does not name.

    Parameters
    ----------
    tmp_path
        For the key.
    purpose
        The token's purpose.
    reason
        The whole refusal.
    """
    server = _proxy(tmp_path)
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("api.example.com", 443)], 2**40, purpose=purpose, network=LOOPBACK)
        status, said = _refusal(server, "api.other.com:443", token)
    finally:
        server.shutdown()
        server.server_close()

    assert (status, said) == (b"403", reason)


@pytest.mark.parametrize(
    "host", ["127.1", "0x7f.1", "2130706433", "0177.0.0.1", "0251.0376.0251.0376", "localhost"]
)
def test_every_spelling_of_loopback_and_metadata_is_refused_for_a_task(
    tmp_path: Path, host: str
) -> None:
    """A resolver reads short, octal and hex numbers as addresses; the proxy checks what it read.

    Parameters
    ----------
    tmp_path
        For the key.
    host
        A spelling of 127.0.0.1 or 169.254.169.254 that `valid_host` accepts.
    """
    server = _proxy(tmp_path, reachable=frozenset())
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [(host, 5432)], 2**40, purpose="task", network=LOOPBACK)
        status, said = _refusal(server, f"{host}:5432", token)
    finally:
        server.shutdown()
        server.server_close()

    assert status == b"403"
    assert said == (
        f"{host}:5432 is not reachable through this proxy: {egress.INTERNAL}; give the "
        "connection an address outside the Lakana stack (lakana connection update)"
    )


def test_a_build_refused_for_an_internal_address_gets_no_connection_hint(tmp_path: Path) -> None:
    """The next step about connections is a task's; a build keeps the plain reason."""
    server = _proxy(tmp_path, reachable=frozenset())
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("127.1", 443)], 2**40, purpose="build", network=LOOPBACK)
        status, said = _refusal(server, "127.1:443", token)
    finally:
        server.shutdown()
        server.server_close()

    assert (status, said) == (
        b"403",
        f"127.1:443 is not reachable through this proxy: {egress.INTERNAL}",
    )


_TASK_NEXT_STEP = (
    "give the connection an address outside the Lakana stack (lakana connection update)"
)


@pytest.mark.parametrize(
    ("purpose", "target", "ranges"),
    [
        ("task", "10.99.0.1:5432", ["10.99.0.1/32"]),  # a Docker network's gateway
        ("build", "10.99.0.1:443", ["10.99.0.1/32"]),
        ("connection-test", "10.99.0.1:5432", ["10.99.0.1/32"]),
        ("task", "gateway.example.com:5432", ["10.99.0.1/32"]),  # a name that resolves there
        ("task", "192.0.2.10:443", ["192.0.2.0/24"]),  # the operator's range
        ("build", "192.0.2.10:443", ["192.0.2.0/24"]),
    ],
)
def test_an_address_in_the_refused_ranges_is_refused_for_every_purpose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, purpose: str, target: str, ranges: list[str]
) -> None:
    """Gateways and the operator's ranges are refused like the stack; tasks hear the next step.

    Parameters
    ----------
    tmp_path
        For the key and the ranges.
    monkeypatch
        For the resolver.
    purpose
        The token's purpose.
    target
        ``host:port``.
    ranges
        What the launcher wrote.
    """
    real = socket.getaddrinfo

    def resolve(
        host: str, port: int, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> list[Any]:
        if host == "gateway.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.99.0.1", port))]
        return real(host, port, family, type, proto, flags)

    monkeypatch.setattr(egress.socket, "getaddrinfo", resolve)
    server = _proxy(tmp_path, ranges=ranges)
    key = ensure_key(tmp_path / "egress.key")
    host, _, port = target.rpartition(":")
    try:
        token = mint(key, [(host, int(port))], 2**40, purpose=purpose, network=LOOPBACK)
        status, said = _refusal(server, target, token)
    finally:
        server.shutdown()
        server.server_close()

    reason = f"{target} is not reachable through this proxy: {egress.INTERNAL}"
    assert status == b"403"
    assert said == (reason if purpose == "build" else f"{reason}; {_TASK_NEXT_STEP}")


def test_the_refused_ranges_apply_before_an_upstream_proxy_is_asked(tmp_path: Path) -> None:
    """Through a company proxy too: a literal in the ranges never leaves."""
    asked: list[bytes] = []

    class Upstream(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            asked.append(self.request.recv(4096))

    upstream = _upstream(Upstream)
    server = _proxy(
        tmp_path,
        ranges=["10.99.0.0/24"],
        upstream=f"http://127.0.0.1:{upstream.server_address[1]}",
    )
    key = ensure_key(tmp_path / "egress.key")
    try:
        for purpose in ("task", "build"):
            token = mint(key, [("10.99.0.1", 5432)], 2**40, purpose=purpose, network=LOOPBACK)
            status, _ = _refusal(server, "10.99.0.1:5432", token)
            assert status == b"403"
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()

    assert asked == []


def test_a_target_outside_the_refused_ranges_still_tunnels(tmp_path: Path, index: int) -> None:
    """Ranges that do not hold the index change nothing."""
    server = _proxy(tmp_path, ranges=["10.99.0.1/32", "192.0.2.0/24", "fd12:3456::1/128"])
    running = Running(server.server_address[1], index, ensure_key(tmp_path / "egress.key"))
    try:
        answer, connection = _connect(running.port, f"localhost:{index}", running.token())
        with connection:
            connection.sendall(b"ping")
            echoed = connection.recv(4)
    finally:
        server.shutdown()
        server.server_close()

    assert answer.split()[1] == b"200" and echoed == b"ping"


_UNREADABLE_RANGES = (
    "the egress proxy cannot read the refused ranges the launcher writes; see "
    "`docker compose logs launcher`"
)


@pytest.mark.parametrize(
    "content",
    [
        None,
        "{",
        "[]",
        '{"refused": "10.0.0.0/8"}',
        '{"refused": ["lan"]}',
        '{"refused": ["10.0.0.5/8"]}',
        '{"refused": [], "extra": 1}',
    ],
    ids=["missing", "not-json", "not-an-object", "not-a-list", "not-a-range", "host-bits", "extra"],
)
def test_unreadable_refused_ranges_fail_closed(
    tmp_path: Path, index: int, content: str | None
) -> None:
    """Without the launcher's ranges the proxy cannot tell the Docker host apart: 503.

    Parameters
    ----------
    tmp_path
        For the key and the ranges.
    index
        A target that would otherwise tunnel.
    content
        The file's text; `None` for no file.
    """
    server = _proxy(tmp_path, ranges=None)
    if content is not None:
        (tmp_path / "refused.json").write_text(content, encoding="utf-8")
    key = ensure_key(tmp_path / "egress.key")
    try:
        token = mint(key, [("localhost", index)], 2**40, purpose="task", network=LOOPBACK)
        status, said = _refusal(server, f"localhost:{index}", token)
    finally:
        server.shutdown()
        server.server_close()

    assert (status, said) == (b"503", _UNREADABLE_RANGES)


def test_rewritten_refused_ranges_are_picked_up_without_a_restart(
    tmp_path: Path, index: int
) -> None:
    """Missing, then written, then broken: each connection sees the file as it is now."""
    server = _proxy(tmp_path, ranges=None)
    running = Running(server.server_address[1], index, ensure_key(tmp_path / "egress.key"))
    try:
        first = _status(running.port, f"localhost:{index}", running.token())
        _write_refused(tmp_path, ["10.99.0.1/32"])
        second = _status(running.port, f"localhost:{index}", running.token())
        (tmp_path / "refused.json").write_text("{", encoding="utf-8")
        third = _status(running.port, f"localhost:{index}", running.token())
    finally:
        server.shutdown()
        server.server_close()

    assert (first, second, third) == (b"503", b"200", b"503")


def test_the_refused_ranges_file_sits_next_to_the_key_unless_set(tmp_path: Path) -> None:
    """Like `reachable`, the file's place is not a setting: the key's directory."""
    settings = EgressSettings(key=tmp_path / "k" / "egress.key")

    assert settings.refused_file == tmp_path / "k" / "refused.json"
    assert EgressSettings(refused_file=tmp_path / "r.json").refused_file == tmp_path / "r.json"
    assert load_settings({}).refused_file == Path("/run/lakana-egress/refused.json")
