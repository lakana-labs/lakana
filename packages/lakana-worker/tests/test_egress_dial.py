"""`egress.dial`: the address checks the proxy and the relay share, directly and upstream."""

from __future__ import annotations

import ipaddress
import socket
import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from lakana_worker.launcher import egress
from lakana_worker.launcher.egress import DialRefused, EgressSettings, dial

_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
_ATTACHED = "eth1\t0800CF0A\t00000000\t0001\t0\t0\t0\tF8FFFFFF\n"
"""10.207.0.8/29, a task network the dialling container is attached to."""


class _Upstream(socketserver.BaseRequestHandler):
    """A company proxy: records each CONNECT line and answers 200."""

    seen: list[bytes]

    def handle(self) -> None:
        """Read the head, record it, accept."""
        head = b""
        while b"\r\n\r\n" not in head:
            byte = self.request.recv(1)
            if not byte:
                return
            head += byte
        type(self).seen.append(head.split(b"\r\n", 1)[0])
        self.request.sendall(b"HTTP/1.1 200 OK\r\n\r\n")


@pytest.fixture
def upstream() -> Iterator[tuple[int, list[bytes]]]:
    """Run a company proxy on loopback.

    Yields
    ------
    tuple
        Its port and the CONNECT lines it received.
    """
    seen: list[bytes] = []
    handler = type("Recording", (_Upstream,), {"seen": seen})
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
    yield server.server_address[1], seen
    server.shutdown()
    server.server_close()


def _settings(tmp_path: Path, *, upstream: str | None = None, table: bool = True) -> EgressSettings:
    """Make settings with a routing table of their own.

    Parameters
    ----------
    tmp_path
        Where the table is written.
    upstream
        The company proxy, or `None`.
    table
        Whether the routing table exists (`False`: it cannot be read).

    Returns
    -------
    EgressSettings
        The settings.
    """
    route = tmp_path / "route"
    if table:
        route.write_text(_HEADER + _ATTACHED, encoding="ascii")
    return EgressSettings(
        key=tmp_path / "key", upstream=upstream, route=route, route6=tmp_path / "no-route6"
    )


def _resolving(monkeypatch: pytest.MonkeyPatch, answers: dict[str, str]) -> None:
    """Make chosen names resolve to chosen addresses and any other name fail; numbers resolve.

    Parameters
    ----------
    monkeypatch
        Replaces `socket.getaddrinfo` in the egress module.
    answers
        Name to IPv4 address.
    """
    real = socket.getaddrinfo

    def fake(
        host: str, port: int, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> list[Any]:
        if host[:1].isdigit():
            return real(host, port, family, type, proto, flags)  # the company proxy's own address
        if host not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port))]

    monkeypatch.setattr(egress.socket, "getaddrinfo", fake)


def test_an_attached_network_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A name of the stack (the server, the database) resolves into an attached network: 403."""
    _resolving(monkeypatch, {"postgres": "10.207.0.10"})

    with pytest.raises(DialRefused) as caught:
        dial("postgres", 5432, _settings(tmp_path))

    assert (caught.value.status, caught.value.reason) == (403, egress.INTERNAL)


def test_extra_refused_networks_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The relay refuses the launcher's whole range of task networks, attached or not."""
    _resolving(monkeypatch, {"other-task": "10.207.3.4"})

    with pytest.raises(DialRefused) as caught:
        dial(
            "other-task",
            5432,
            _settings(tmp_path),
            refused=frozenset({ipaddress.ip_network("10.207.0.0/16")}),
        )

    assert caught.value.status == 403


def test_a_numeric_host_no_resolver_reads_is_refused(tmp_path: Path) -> None:
    """Five numbers are neither an address nor a name."""
    with pytest.raises(DialRefused) as caught:
        dial("1.2.3.4.5", 443, _settings(tmp_path))

    assert caught.value.status == 403
    assert "neither a host name nor an IPv4 address" in caught.value.reason


@pytest.mark.parametrize(
    "host", ["127.1", "0x7f.1", "2130706433", "0251.0376.0251.0376", "10.207.0.9"]
)
def test_through_an_upstream_proxy_a_forbidden_literal_never_leaves(
    tmp_path: Path, upstream: tuple[int, list[bytes]], host: str
) -> None:
    """A company proxy on the Docker host would reach its loopback and the stack: refused here.

    Parameters
    ----------
    tmp_path
        For the routing table.
    upstream
        The company proxy and what it saw.
    host
        A forbidden address in some spelling.
    """
    port, seen = upstream

    with pytest.raises(DialRefused) as caught:
        dial(host, 5432, _settings(tmp_path, upstream=f"http://127.0.0.1:{port}"))

    assert caught.value.status == 403
    assert seen == []


def test_through_an_upstream_proxy_a_name_resolving_inside_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: tuple[int, list[bytes]]
) -> None:
    """`postgres` resolves to the stack here; the company proxy is never asked."""
    port, seen = upstream
    _resolving(monkeypatch, {"postgres": "10.207.0.10"})

    with pytest.raises(DialRefused) as caught:
        dial("postgres", 5432, _settings(tmp_path, upstream=f"http://127.0.0.1:{port}"))

    assert caught.value.status == 403
    assert seen == []


def test_through_an_upstream_proxy_an_outside_name_goes_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: tuple[int, list[bytes]]
) -> None:
    """A name that does not resolve here, and a build's names, are the company proxy's to decide."""
    port, seen = upstream
    _resolving(monkeypatch, {"postgres": "10.207.0.10"})
    settings = _settings(tmp_path, upstream=f"http://127.0.0.1:{port}")

    dial("db.example.com", 5432, settings).close()
    dial("postgres", 5432, settings, check_names=False).close()

    assert seen == [b"CONNECT db.example.com:5432 HTTP/1.1", b"CONNECT postgres:5432 HTTP/1.1"]


def test_through_an_upstream_proxy_an_unreadable_table_refuses_a_literal(
    tmp_path: Path, upstream: tuple[int, list[bytes]]
) -> None:
    """Without the attached networks a literal cannot be judged: no connection (503)."""
    port, seen = upstream

    with pytest.raises(DialRefused) as caught:
        dial(
            "203.0.113.7",
            5432,
            _settings(tmp_path, upstream=f"http://127.0.0.1:{port}", table=False),
        )

    assert caught.value.status == 503
    assert seen == []


_HOST_NAMES = [
    "host.docker.internal",
    "gateway.docker.internal",
    "docker.for.mac.host.internal",
    "docker.for.mac.localhost",
    "docker.for.win.localhost",
]


@pytest.mark.parametrize("spelling", ["plain", "upper", "trailing-dot"])
@pytest.mark.parametrize("name", _HOST_NAMES)
@pytest.mark.parametrize("upstream_proxy", [False, True], ids=["direct", "upstream"])
@pytest.mark.parametrize("check_names", [True, False], ids=["task", "build"])
def test_the_docker_hosts_well_known_names_are_refused_whatever_they_resolve_to(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: tuple[int, list[bytes]],
    name: str,
    spelling: str,
    upstream_proxy: bool,
    check_names: bool,
) -> None:
    """Docker Desktop resolves them to the host outside every gateway: refused by name.

    Parameters
    ----------
    tmp_path
        For the routing table.
    monkeypatch
        Makes the name resolve to a public address that would pass every address check.
    upstream
        A company proxy that must not be asked.
    name
        A well-known name.
    spelling
        How it is written.
    upstream_proxy
        Whether the dial goes through the company proxy.
    check_names
        Whether names are resolved before the company proxy (`False` for builds).
    """
    host = {"plain": name, "upper": name.upper(), "trailing-dot": f"{name}."}[spelling]
    _resolving(monkeypatch, {host: "93.184.216.34"})
    port, seen = upstream
    settings = _settings(tmp_path, upstream=f"http://127.0.0.1:{port}" if upstream_proxy else None)

    with pytest.raises(DialRefused) as caught:
        dial(host, 443, settings, check_names=check_names)

    assert (caught.value.status, caught.value.reason) == (403, egress.INTERNAL)
    assert seen == []
