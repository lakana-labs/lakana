"""The relay in-process: one port per route, its own subnet only, the proxy's address checks."""

from __future__ import annotations

import ipaddress
import itertools
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lakana_worker.launcher import egress, relay
from lakana_worker.launcher.egress import EgressSettings
from lakana_worker.launcher.relay import MAX_PER_PORT, RelayPlan, parse_plan, serve
from lakana_worker.launcher.requests import RelayRoute

LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
_ATTACHED = "eth1\t0000090A\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
"""10.9.0.0/24: the network the relay reaches out through, which holds the stack."""
_SUBNETS = itertools.count(1)
_BOUND: dict[tuple[str, int], int] = {}
"""The relay's address and listen port to the loopback port it really listens on."""
_PRETENDED: dict[int, str] = {}
"""A client's loopback port to the address the relay reads as its peer."""


class _LoopbackSocket(socket.socket):
    """A relay's socket that listens on 127.0.0.1, the one loopback address every platform has.

    macOS has no 127.N.M.x beyond 127.0.0.1, so a relay's plan keeps its own /29 while its
    sockets bind 127.0.0.1 on a free port (recorded in `_BOUND`), and an accepted connection's
    peer is read as the address the client recorded in `_PRETENDED`. The subnet check, the
    ports and the limits still run on the plan's addresses.
    """

    def bind(self, address: tuple[str, int], /) -> None:  # ty: ignore[invalid-method-override]
        """Bind 127.0.0.1 on a free port instead, and remember which.

        Parameters
        ----------
        address
            The plan's address and listen port.
        """
        super().bind(("127.0.0.1", 0))
        _BOUND[str(address[0]), int(address[1])] = self.getsockname()[1]

    def accept(self) -> tuple[socket.socket, tuple[str, int]]:
        """Accept, reading the peer as the address its client pretended.

        Returns
        -------
        tuple
            The connection and its pretended peer.
        """
        client, peer = super().accept()
        return client, (_PRETENDED.get(peer[1], peer[0]), peer[1])


class _RelaySockets:
    """The `socket` module as the relay sees it in these tests: `_LoopbackSocket` for sockets."""

    socket = _LoopbackSocket

    def __getattr__(self, name: str) -> object:
        """Hand out everything else of the real module.

        Parameters
        ----------
        name
            The attribute.

        Returns
        -------
        object
            The real module's attribute.
        """
        return getattr(socket, name)


def _closed_port() -> int:
    """Find a loopback port nothing listens on.

    Returns
    -------
    int
        The port.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _Echo(socketserver.BaseRequestHandler):
    """Echoes what it receives, standing in for a database."""

    def handle(self) -> None:
        """Echo until the peer closes."""
        while data := self.request.recv(4096):
            self.request.sendall(data)


class _EchoServer(socketserver.ThreadingTCPServer):
    """The echo target, with a listen backlog above a relay port's 16 connections.

    `socketserver`'s default backlog of 5 drops the SYNs of a burst of 16 dials, which the
    kernel retries after 1 s, then 3 s more: under load that crossed the clients' 5 s timeout.
    """

    request_queue_size = 4 * MAX_PER_PORT


@pytest.fixture
def target() -> Iterator[int]:
    """Run an echo "database" on loopback.

    Yields
    ------
    int
        Its port.
    """
    server = _EchoServer(("127.0.0.1", 0), _Echo)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@dataclass
class RelayHarness:
    """A relay serving on a loopback /29 of its own, its log, and a way to stop it.

    Parameters
    ----------
    plan
        What it serves.
    logs
        The lines it wrote.
    stop
        Ends it.
    """

    plan: RelayPlan
    logs: list[str] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)

    def connect(self, listen: int, *, source: str | None = None) -> socket.socket:
        """Connect to one of the relay's ports from an address of the task's subnet.

        Parameters
        ----------
        listen
            The port.
        source
            Another source address (outside the subnet), or `None` for the subnet's first host.

        Returns
        -------
        socket.socket
            The connection.

        Raises
        ------
        OSError
            When the relay does not accept it (a port it does not listen on is refused).
        """
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(5)
        client.bind(("127.0.0.1", 0))
        _PRETENDED[client.getsockname()[1]] = source or str(next(self.plan.subnet.hosts()))
        port = _BOUND.get((str(self.plan.address), listen)) or _closed_port()
        try:
            client.connect(("127.0.0.1", port))
        except OSError:
            client.close()
            raise
        return client

    def wait_for_log(self, fragment: str) -> str:
        """Wait up to five seconds for a log line holding a fragment.

        Parameters
        ----------
        fragment
            Text the line holds.

        Returns
        -------
        str
            The line.

        Raises
        ------
        AssertionError
            When no such line arrives.
        """
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for line in list(self.logs):
                if fragment in line:
                    return line
            time.sleep(0.01)
        raise AssertionError(f"no log line with {fragment!r}: {self.logs}")


@pytest.fixture
def relay_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., RelayHarness]]:
    """Start relays on loopback /29s; each test's relays stop at its end.

    Parameters
    ----------
    tmp_path
        For the routing table.
    monkeypatch
        Gives the relay `_LoopbackSocket` sockets.

    Yields
    ------
    callable
        ``(routes, reachable=LOOPBACK, table=True, refused=(range,), plan_reachable=(),
        verdicts=False) -> RelayHarness``; ``plan_reachable`` is the plan's own ``reachable``.
    """
    monkeypatch.setattr(relay, "socket", _RelaySockets())
    started: list[RelayHarness] = []
    route = tmp_path / "route"
    route.write_text(_HEADER + _ATTACHED, encoding="ascii")

    def start(
        routes: list[tuple[int, str, int]],
        *,
        reachable: frozenset[object] = frozenset({LOOPBACK}),
        table: bool = True,
        refused: tuple[str, ...] = ("10.207.0.0/16",),
        plan_reachable: tuple[str, ...] = (),
        verdicts: bool = False,
    ) -> RelayHarness:
        subnet = ipaddress.IPv4Network(f"127.{next(_SUBNETS)}.{len(started)}.0/29")
        plan = RelayPlan(
            address=list(subnet.hosts())[-1],
            subnet=subnet,
            refused=frozenset(ipaddress.ip_network(r) for r in refused),
            routes=tuple(RelayRoute(*r) for r in routes),
            reachable=frozenset(ipaddress.IPv4Network(r) for r in plan_reachable),
            verdicts=verdicts,
        )
        settings = EgressSettings(
            key=tmp_path / "unused.key",
            reachable=reachable,  # ty: ignore[invalid-argument-type]
            route=route if table else tmp_path / "missing",
            route6=tmp_path / "no-route6",
        )
        harness = RelayHarness(plan)
        ready = threading.Event()
        threading.Thread(
            target=serve,
            args=(plan, settings),
            kwargs={"ready": ready.set, "stop": harness.stop, "log": harness.logs.append},
            daemon=True,
        ).start()
        assert ready.wait(5)
        started.append(harness)
        return harness

    yield start
    for harness in started:
        harness.stop.set()


def _resolving(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[str]]) -> list[str]:
    """Make names resolve to a sequence of answers, one per lookup, and record each lookup.

    Parameters
    ----------
    monkeypatch
        Replaces `socket.getaddrinfo` as the egress module sees it.
    answers
        Name to the address each successive lookup returns.

    Returns
    -------
    list of str
        The names looked up, in order.
    """
    asked: list[str] = []
    real = socket.getaddrinfo

    def fake(
        host: str, port: int, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> list[Any]:
        if host not in answers:
            return real(host, port, family, type, proto, flags)
        asked.append(host)
        address = answers[host][min(len(asked), len(answers[host])) - 1]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(egress.socket, "getaddrinfo", fake)
    return asked


def _echoes(client: socket.socket, data: bytes) -> bytes:
    """Send bytes and read as many back.

    Parameters
    ----------
    client
        The connection.
    data
        What to send.

    Returns
    -------
    bytes
        What came back.
    """
    client.sendall(data)
    received = b""
    while len(received) < len(data):
        chunk = client.recv(4096)
        if not chunk:
            break
        received += chunk
    return received


def test_a_route_forwards_bytes_both_ways(
    relay_on: Callable[..., RelayHarness], target: int
) -> None:
    """A client on the task subnet reaches the target through its listen port."""
    harness = relay_on([(15001, "localhost", target)])

    with harness.connect(15001) as client:
        assert _echoes(client, b"SELECT 1") == b"SELECT 1"


def test_a_tcp_connection_is_forwarded_like_a_database(
    relay_on: Callable[..., RelayHarness], target: int
) -> None:
    """Any protocol's bytes, binary ones too, go through unchanged: the relay never reads them."""
    harness = relay_on([(15001, "localhost", target), (15002, "localhost", target)])
    payload = bytes(range(256)) * 64

    with harness.connect(15002) as client:
        assert _echoes(client, payload) == payload


def test_only_listed_ports_listen(relay_on: Callable[..., RelayHarness], target: int) -> None:
    """Another port of the relay's address is refused by the kernel."""
    harness = relay_on([(15001, "localhost", target)])

    with pytest.raises(ConnectionRefusedError):
        harness.connect(15002)


def test_a_client_outside_the_task_subnet_is_closed(
    relay_on: Callable[..., RelayHarness], target: int
) -> None:
    """A peer outside the /29 is closed at once, and the log says from where."""
    harness = relay_on([(15001, "localhost", target)])
    outside = str(harness.plan.subnet.broadcast_address + 2)

    with harness.connect(15001, source=outside) as client:
        assert client.recv(1) == b""

    assert harness.wait_for_log(f"closed a connection from {outside}")


def test_the_seventeenth_connection_on_a_port_is_closed(
    relay_on: Callable[..., RelayHarness], target: int
) -> None:
    """At most 16 at once per port; the 17th is closed with a line naming the limit."""
    harness = relay_on([(15001, "localhost", target)])
    held = [harness.connect(15001) for _ in range(MAX_PER_PORT)]
    try:
        for client in held:
            assert _echoes(client, b"x") == b"x"  # each one is being carried
        with harness.connect(15001) as extra:
            assert extra.recv(1) == b""
    finally:
        for client in held:
            client.close()

    assert harness.wait_for_log(f"{MAX_PER_PORT} are open already")


@pytest.mark.parametrize(
    ("host", "answer"),
    [
        ("db.internal", "10.9.0.5"),  # the stack's network, which the relay is attached to
        ("postgres", "10.9.0.7"),
        ("other-task.internal", "10.207.4.3"),  # the launcher's range of task networks
        ("metadata.internal", "169.254.169.254"),
    ],
)
def test_the_relay_refuses_an_attached_network_and_says_why(
    relay_on: Callable[..., RelayHarness],
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    answer: str,
) -> None:
    """Review Focus 3: an internal target is never dialled; the log line says why and what to do.

    Parameters
    ----------
    relay_on
        Starts the relay.
    monkeypatch
        For the resolver.
    host
        The connection's host.
    answer
        Where it resolves to.
    """
    _resolving(monkeypatch, {host: [answer]})
    harness = relay_on([(15001, host, 5432)], reachable=frozenset())

    with harness.connect(15001) as client:
        assert client.recv(1) == b""

    assert harness.wait_for_log("refused") == (
        f"refused {host}:5432: {egress.INTERNAL}; give the connection an address outside the "
        "Lakana stack (lakana connection update)"
    )


@pytest.mark.parametrize("host", ["127.1", "0x7f.1", "2130706433", "localhost"])
def test_every_spelling_of_loopback_is_refused(
    relay_on: Callable[..., RelayHarness], host: str
) -> None:
    """Short and hex numbers are read as the resolver reads them, then refused.

    Parameters
    ----------
    relay_on
        Starts the relay.
    host
        A spelling of 127.0.0.1.
    """
    harness = relay_on([(15001, host, 5432)], reachable=frozenset())

    with harness.connect(15001) as client:
        assert client.recv(1) == b""

    assert egress.INTERNAL in harness.wait_for_log(f"refused {host}:5432")


def test_the_relay_resolves_once_and_dials_the_checked_address(
    relay_on: Callable[..., RelayHarness], monkeypatch: pytest.MonkeyPatch, target: int
) -> None:
    """One lookup per connection, and the connection goes to the address that was checked.

    The name answers an allowed address, then a forbidden one: DNS rebinding gains nothing.
    """
    asked = _resolving(monkeypatch, {"db.example.com": ["127.0.0.1", "10.9.0.5"]})
    harness = relay_on([(15001, "db.example.com", target)])

    with harness.connect(15001) as client:
        assert _echoes(client, b"ping") == b"ping"

    assert asked == ["db.example.com"]


def test_unreadable_routes_fail_closed(relay_on: Callable[..., RelayHarness], target: int) -> None:
    """Without a routing table no address can be judged: nothing is dialled, the log says so."""
    harness = relay_on([(15001, "localhost", target)], table=False)

    with harness.connect(15001) as client:
        assert client.recv(1) == b""

    assert "cannot read its routing table" in harness.wait_for_log("refused localhost")


def test_a_silent_database_keeps_its_connection(
    relay_on: Callable[..., RelayHarness], monkeypatch: pytest.MonkeyPatch, target: int
) -> None:
    """The proxy's five idle minutes do not apply: a long query sends nothing for a while."""
    monkeypatch.setattr(egress, "_IDLE_SECONDS", 0.1)
    harness = relay_on([(15001, "localhost", target)])

    with harness.connect(15001) as client:
        assert _echoes(client, b"a") == b"a"
        time.sleep(0.5)
        assert _echoes(client, b"b") == b"b"


_GOOD_ROUTE = '{"listen": 15001, "host": "db.example.com", "port": 5432}'


@pytest.mark.parametrize(
    ("plan", "message"),
    [
        ("{", "the plan is one JSON object"),
        ("[]", "exactly address, subnet, refused, reachable, routes and verdicts"),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}], "extra": 1}}',
            "exactly address, subnet, refused, reachable, routes and verdicts",
        ),
        (
            '{"address": "10.0.0.9", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}',
            "address must be a host address of subnet",
        ),
        (
            '{"address": "10.0.0.7", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}',
            "address must be a host address of subnet",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/28", "refused": ["10.0.0.0/16"], '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}',
            "a /29",
        ),
        (
            '{"address": "x", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            '"verdicts": false, "reachable": [], "routes": []}',
            "address and subnet are IPv4",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["x"], '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}',
            "refused: each entry is an IPv4 or IPv6 network in CIDR form",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.5/16"], '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}',
            "refused: each entry is an IPv4 or IPv6 network in CIDR form",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": [7], '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}',
            "refused: each entry is an IPv4 or IPv6 network in CIDR form",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": [], '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}',
            "refused: 1 to 256",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": "10.0.0.0/16", '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}',
            "refused: 1 to 256",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            f'"verdicts": false, "reachable": "172.30.0.0/16", "routes": [{_GOOD_ROUTE}]}}',
            "reachable: at most 4 IPv4 networks in CIDR form",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            '"verdicts": false, "reachable": ["172.30.0.0/16", "172.31.0.0/16", "172.29.0.0/16", '
            '"172.28.0.0/16", '
            f'"172.27.0.0/16"], "routes": [{_GOOD_ROUTE}]}}',
            "reachable: at most 4 IPv4 networks in CIDR form",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            f'"verdicts": false, "reachable": ["fd00::/8"], "routes": [{_GOOD_ROUTE}]}}',
            "reachable: at most 4 IPv4 networks in CIDR form",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            f'"verdicts": false, "reachable": ["172.30.0.5/16"], "routes": [{_GOOD_ROUTE}]}}',
            "reachable: at most 4 IPv4 networks in CIDR form",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            f'"verdicts": false, "reachable": [7], "routes": [{_GOOD_ROUTE}]}}',
            "reachable: at most 4 IPv4 networks in CIDR form",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            f'"routes": [{_GOOD_ROUTE}]}}',
            "exactly address, subnet, refused, reachable, routes and verdicts",
        ),
        pytest.param(" " * (64 * 1024 + 1), "the plan is larger than 64 KiB", id="too-large"),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            '"verdicts": false, "reachable": [], "routes": [[15001, "db", 5432]]}',
            "routes: each has exactly listen, host and port",
        ),
        pytest.param(
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ['
            + ", ".join(f'"10.{n // 256}.{n % 256}.1/32"' for n in range(257))
            + f'], "verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}',
            "refused: 1 to 256",
            id="257-refused",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "internal": "10.0.0.0/16", '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}',
            "exactly address, subnet, refused, reachable, routes and verdicts",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            '"verdicts": false, "reachable": [], "routes": []}',
            "routes: 1 to 16",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            '"verdicts": false, "reachable": [], '
            '"routes": [{"listen": 99, "host": "db", "port": 5432}]}',
            "listen ports are 15001 to 15064",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            '"verdicts": false, "reachable": [], '
            '"routes": [{"listen": 15001, "host": "DB", "port": 5432}]}',
            "a lower-case host",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            '"verdicts": false, "reachable": [], '
            '"routes": [{"listen": 15001, "host": "db", "port": 0}]}',
            "a port from 1 to 65535",
        ),
        (
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}, {_GOOD_ROUTE}]}}',
            "listen ports are distinct",
        ),
    ],
)
def test_a_malformed_plan_is_refused(plan: str, message: str) -> None:
    """`parse_plan` names the first field that breaks its rule.

    Parameters
    ----------
    plan
        The stdin text.
    message
        Part of the refusal.
    """
    with pytest.raises(ValueError, match=message):
        parse_plan(plan)


def test_a_plan_is_read() -> None:
    """The fields the launcher writes come back typed."""
    plan = parse_plan(
        '{"address": "10.207.0.6", "subnet": "10.207.0.0/29", '
        '"refused": ["10.207.0.0/16", "172.17.0.1/32", "fd12:3456::1/128"], '
        f'"verdicts": false, "reachable": ["172.30.0.0/16"], "routes": [{_GOOD_ROUTE}]}}'
    )

    assert plan == RelayPlan(
        address=ipaddress.IPv4Address("10.207.0.6"),
        subnet=ipaddress.IPv4Network("10.207.0.0/29"),
        refused=frozenset(
            {
                ipaddress.ip_network("10.207.0.0/16"),
                ipaddress.ip_network("172.17.0.1/32"),
                ipaddress.ip_network("fd12:3456::1/128"),
            }
        ),
        routes=(RelayRoute(15001, "db.example.com", 5432),),
        reachable=frozenset({ipaddress.IPv4Network("172.30.0.0/16")}),
        verdicts=False,
    )


def test_main_refuses_a_bad_plan_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit status 2 and one line that the launcher shows as the reason the relay did not start."""
    monkeypatch.setattr(relay.sys, "stdin", type("In", (), {"read": staticmethod(lambda: "{")})())

    with pytest.raises(SystemExit) as exited:
        relay.main()

    assert exited.value.code == 2
    assert capsys.readouterr().err == "lakana-relay: cannot start: the plan is one JSON object\n"


@pytest.mark.parametrize(
    ("host", "answer", "refused"),
    [
        ("db.example.com", "172.17.0.1", ("10.207.0.0/16", "172.17.0.1/32")),  # docker0
        ("crm.lan", "192.0.2.44", ("10.207.0.0/16", "192.0.2.0/24")),  # the operator's range
    ],
)
def test_a_target_in_the_refused_ranges_is_not_dialled(
    relay_on: Callable[..., RelayHarness],
    monkeypatch: pytest.MonkeyPatch,
    host: str,
    answer: str,
    refused: tuple[str, ...],
) -> None:
    """A Docker gateway or an operator's range from the plan is refused like the stack.

    Parameters
    ----------
    relay_on
        Starts the relay.
    monkeypatch
        For the resolver.
    host
        The connection's host.
    answer
        Where it resolves to.
    refused
        The plan's ranges.
    """
    _resolving(monkeypatch, {host: [answer]})
    harness = relay_on([(15001, host, 5432)], refused=refused)

    with harness.connect(15001) as client:
        assert client.recv(1) == b""

    assert harness.wait_for_log("refused") == (
        f"refused {host}:5432: {egress.INTERNAL}; give the connection an address outside the "
        "Lakana stack (lakana connection update)"
    )


def test_a_target_outside_the_refused_ranges_is_forwarded(
    relay_on: Callable[..., RelayHarness], target: int
) -> None:
    """Ranges that do not hold the target change nothing."""
    harness = relay_on(
        [(15001, "localhost", target)],
        refused=("10.207.0.0/16", "172.17.0.1/32", "192.0.2.0/24", "fd12:3456::1/128"),
    )

    with harness.connect(15001) as client:
        assert _echoes(client, b"ping") == b"ping"


def test_a_target_on_a_network_the_plan_makes_reachable_is_dialled(
    relay_on: Callable[..., RelayHarness], target: int
) -> None:
    """The plan's ``reachable`` (a test hook, empty in production) lets the relay dial there."""
    harness = relay_on(
        [(15001, "localhost", target)], reachable=frozenset(), plan_reachable=("127.0.0.0/8",)
    )

    with harness.connect(15001) as client:
        assert _echoes(client, b"ping") == b"ping"


def test_a_refused_range_wins_over_the_plans_reachable_networks(
    relay_on: Callable[..., RelayHarness], target: int
) -> None:
    """A range the launcher refuses stays refused, whatever ``reachable`` lets in."""
    harness = relay_on(
        [(15001, "localhost", target)],
        reachable=frozenset(),
        plan_reachable=("127.0.0.0/8",),
        refused=("10.207.0.0/16", "127.0.0.0/8"),
    )

    with harness.connect(15001) as client:
        assert client.recv(1) == b""

    assert egress.INTERNAL in harness.wait_for_log(f"refused localhost:{target}")


def test_a_handler_thread_that_cannot_start_gives_its_slot_back(
    relay_on: Callable[..., RelayHarness], monkeypatch: pytest.MonkeyPatch, target: int
) -> None:
    """Past the process limit `Thread.start` raises: the client is closed, the port lives on.

    More failures than the port has slots, then a connection that works: a leaked slot would
    close it as the seventeenth.
    """
    harness = relay_on([(15001, "localhost", target)])
    real = threading.Thread.start
    failures = [MAX_PER_PORT + 1]

    def start(self: threading.Thread) -> None:
        if getattr(self, "_target", None) is relay._handle and failures[0] > 0:
            failures[0] -= 1
            raise RuntimeError("can't start new thread")
        real(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    for _ in range(MAX_PER_PORT + 1):
        with harness.connect(15001) as client:
            assert client.recv(1) == b""

    with harness.connect(15001) as client:
        assert _echoes(client, b"ping") == b"ping"
    assert harness.wait_for_log("cannot start another thread") == (
        f"closed a connection to localhost:{target}: the relay cannot start another thread "
        "(can't start new thread)"
    )
    assert not [line for line in harness.logs if "are open already" in line]


def test_the_docker_hosts_name_is_refused_by_the_relay(
    relay_on: Callable[..., RelayHarness], monkeypatch: pytest.MonkeyPatch, target: int
) -> None:
    """Even when it resolves to an address the relay would otherwise forward to."""
    _resolving(monkeypatch, {"host.docker.internal": ["127.0.0.1"]})
    harness = relay_on([(15001, "host.docker.internal", target)])

    with harness.connect(15001) as client:
        assert client.recv(1) == b""

    assert harness.wait_for_log("refused") == (
        f"refused host.docker.internal:{target}: {egress.INTERNAL}; give the connection an "
        "address outside the Lakana stack (lakana connection update)"
    )


def test_main_stops_with_status_2_when_it_cannot_listen(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A plan whose address is not this container's: the port cannot be bound, the line says so."""
    plan = (
        '{"address": "10.207.0.6", "subnet": "10.207.0.0/29", "refused": ["10.207.0.0/16"], '
        f'"verdicts": false, "reachable": [], "routes": [{_GOOD_ROUTE}]}}'
    )
    monkeypatch.setattr(relay.sys, "stdin", type("In", (), {"read": staticmethod(lambda: plan)})())
    for name in [n for n in relay.os.environ if n.startswith("LAKANA_EGRESS__")]:
        monkeypatch.delenv(name)

    with pytest.raises(SystemExit) as exited:
        relay.main()

    assert exited.value.code == 2
    said = capsys.readouterr().err
    assert said.startswith("lakana-relay: cannot start: ") and said.endswith("\n")
    assert "ready" not in said


def test_a_checks_relay_says_connected_once_it_has_dialled_the_target(
    relay_on: Callable[..., RelayHarness], target: int
) -> None:
    """The positive verdict a connection check waits for: the source answered the dial."""
    harness = relay_on([(15001, "localhost", target)], verdicts=True)

    with harness.connect(15001) as client:
        assert _echoes(client, b"x") == b"x"

    assert harness.wait_for_log("connected") == f"connected localhost:{target}"


def test_a_tasks_relay_never_says_connected(
    relay_on: Callable[..., RelayHarness], target: int
) -> None:
    """A task's log stays free of a line per database connection."""
    harness = relay_on([(15001, "localhost", target)])

    with harness.connect(15001) as client:
        assert _echoes(client, b"x") == b"x"

    assert not any("connected" in line for line in harness.logs)


def test_a_checks_relay_that_cannot_dial_says_refused_and_never_connected(
    relay_on: Callable[..., RelayHarness],
) -> None:
    harness = relay_on([(15001, "localhost", _closed_port())], verdicts=True)

    with harness.connect(15001) as client:
        assert client.recv(1) == b""

    assert harness.wait_for_log("refused").startswith("refused localhost:")
    assert not any(line.startswith("connected") for line in harness.logs)


def test_a_plan_whose_verdicts_is_not_a_boolean_is_refused() -> None:
    with pytest.raises(ValueError, match="verdicts: true or false"):
        parse_plan(
            '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
            f'"verdicts": 1, "reachable": [], "routes": [{_GOOD_ROUTE}]}}'
        )
    plan = parse_plan(
        '{"address": "10.0.0.6", "subnet": "10.0.0.0/29", "refused": ["10.0.0.0/16"], '
        f'"verdicts": true, "reachable": [], "routes": [{_GOOD_ROUTE}]}}'
    )
    assert plan.verdicts is True
