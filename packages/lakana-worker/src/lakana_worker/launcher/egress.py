"""The egress proxy: CONNECT to the host:port pairs a client's token names, and nothing else.

Every build container's network is internal: its only way out is this proxy, which the launcher
attaches to it as `egress`. A client sends ``CONNECT host:port`` with its token as the proxy
password; the proxy tunnels when the token is valid (`egress_token.verify`), the client connects
from the token's network, and the token names exactly that host and port. Everything else is
refused, and the refusal says why. TLS stays end to end: the proxy sees host names, never what
the client sends to the index. Nothing here knows what a build is: the token says what a client
may reach, so another kind of client (an ingestion task's connections) needs only other tokens.

Whatever a token says, a direct connection never goes to loopback, link-local (the cloud
metadata address), multicast, unspecified or reserved addresses, a known metadata address, or a
network the proxy's own container is attached to (the Compose network with the server, the
database and the object store, and every build network). A host is resolved once and the
connection goes to an address that passed the check, so a name that resolves elsewhere a moment
later (DNS rebinding) gains nothing. Private addresses outside those networks stay reachable: a
company's package index is often one. With `LAKANA_EGRESS__UPSTREAM_PROXY` the proxy tunnels
through a company proxy instead, which resolves and decides. When a routing table cannot be read
or has a line the proxy does not understand, it cannot tell internal addresses from others and
makes no direct connection. The launcher writes a new key at every start; the proxy reads the key
file again whenever its inode, time or size changes, so it needs no restart.

Limits: a request head must arrive whole within 10 seconds and 16 KiB; at most
`LAKANA_EGRESS__MAX_CONNECTIONS` connections at once (256), and at most
`LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK` (32) from one client network, so one build cannot
take the connections another build needs (#302); a tunnel ends after five idle minutes and when
its token expires. A client network is the attached network the client's address is in (a
build's /29, read from the routing table), or the address alone when no attached network holds
it; it is known before the request head, so a build that never sends one is held to its cap too.
Standard library only, like the launcher.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ipaddress
import logging
import os
import selectors
import socket
import socketserver
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit

from lakana_worker.launcher.egress_token import Grant, Network, read_key, valid_host, verify

logger = logging.getLogger("lakana_worker.launcher.egress")

PORT = 3128
PREFIX = "LAKANA_EGRESS__"
_NAMES = frozenset(
    {"KEY", "LISTEN", "UPSTREAM_PROXY", "MAX_CONNECTIONS", "MAX_CONNECTIONS_PER_NETWORK"}
)
_MAX_HEAD = 16384
_MAX_HEADERS = 64
_HEAD_SECONDS = 10.0
_CONNECT_SECONDS = 15.0
_IDLE_SECONDS = 300.0
_LINGER_SECONDS = 1.0
_SPARE_THREADS = 64
"""Threads beyond `max_connections` that may answer 503 before connections are just closed."""
_SPARE_PER_NETWORK = 4
"""Threads beyond its cap that one client network may hold to be told 503; past them, its
connections are closed at once, so a flood from one build cannot take the threads of others."""
_ROUTE = Path("/proc/net/route")
_ROUTE6 = Path("/proc/net/ipv6_route")
_PHRASES = {
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    407: "Proxy Authentication Required",
    408: "Request Timeout",
    502: "Bad Gateway",
    503: "Service Unavailable",
}
_HINTS = {
    "build": (
        "a build reaches only the host and port of the package indexes its project uses; "
        "register the index (lakana index add) or set LAKANA_RUN__INDEX_URL, with the port "
        "in its URL"
    ),
    "task": "declare it in @asset(connections=[...]) and create it with lakana connection create",
    "connection-test": "check the connection's address with lakana connection show",
}
"""What a refused client of each purpose can do about a host its token does not name."""
_SUBJECTS = {"connection-test": "connection test"}
"""How a refusal names a client whose purpose is not a plain word."""
_NEVER = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.100.100.200/32"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)
"""Beyond loopback, link-local, multicast, unspecified and reserved: "this network" and the
metadata addresses outside link-local (Alibaba Cloud, AWS over IPv6)."""


class SettingsError(Exception):
    """A setting is malformed; the text names it."""


@dataclass(frozen=True)
class EgressSettings:
    """What the proxy listens on and how it reaches out.

    Parameters
    ----------
    key
        The key file the launcher writes.
    listen
        Address and port.
    upstream
        A proxy to tunnel through, ``http://[user:password@]host:port``; left out of `repr`.
    max_connections
        Most connections at once.
    max_connections_per_network
        Most connections at once from one client network (one build); `max_connections` when
        lower.
    head_seconds
        How long a client may take to send its request head.
    reachable
        Networks a direct connection may reach although the built-in refusals cover them. Not
        a setting: tests reach an index on loopback with it.
    route, route6
        The kernel's routing tables, read for the attached networks. Not settings either.
    """

    key: Path = Path("/run/lakana-egress/egress.key")
    listen: tuple[str, int] = ("0.0.0.0", PORT)  # nosec B104 -- it serves the build networks
    upstream: str | None = field(default=None, repr=False)
    max_connections: int = 256
    max_connections_per_network: int = 32
    head_seconds: float = _HEAD_SECONDS
    reachable: frozenset[Network] = frozenset()
    route: Path = field(default_factory=lambda: _ROUTE)
    route6: Path = field(default_factory=lambda: _ROUTE6)


def _port(text: str) -> int | None:
    """Read a port in its one spelling.

    Parameters
    ----------
    text
        Digits.

    Returns
    -------
    int or None
        The port; `None` unless it is 1 to 65535 without a sign or a leading zero.
    """
    if not text.isascii() or not text.isdigit() or text.startswith("0") or len(text) > 5:
        return None
    number = int(text)
    return number if number < 65536 else None


def load_settings(environ: Mapping[str, str]) -> EgressSettings:
    """Read `LAKANA_EGRESS__*`.

    Parameters
    ----------
    environ
        The environment.

    Returns
    -------
    EgressSettings
        The settings.

    Raises
    ------
    SettingsError
        For a malformed or unknown setting; the message names it.
    """
    unknown = sorted(
        name for name in environ if name.startswith(PREFIX) and name[len(PREFIX) :] not in _NAMES
    )
    if unknown:
        raise SettingsError(
            f"{', '.join(unknown)}: not a setting of the egress proxy; the settings are "
            f"{', '.join(PREFIX + n for n in sorted(_NAMES))}."
        )

    def get(name: str) -> str | None:
        """Read one setting, blank meaning unset.

        Parameters
        ----------
        name
            The name after the prefix.

        Returns
        -------
        str or None
            The value.
        """
        return environ.get(PREFIX + name, "").strip() or None

    defaults = EgressSettings()
    listen = defaults.listen
    if (text := get("LISTEN")) is not None:
        host, _, port_text = text.rpartition(":")
        port = _port(port_text)
        if not host or port is None:
            raise SettingsError(f"{PREFIX}LISTEN must be host:port, such as 0.0.0.0:3128.")
        listen = (host, port)
    upstream = get("UPSTREAM_PROXY")
    if upstream is not None:
        rule = (
            f"{PREFIX}UPSTREAM_PROXY must be http://[user:password@]host:port, the proxy your "
            "network sends https through."
        )
        try:
            parts = urlsplit(upstream)
            _ = parts.port
        except ValueError:
            raise SettingsError(rule) from None
        if (
            parts.scheme != "http"
            or not parts.hostname
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise SettingsError(rule)
    connections = get("MAX_CONNECTIONS") or str(defaults.max_connections)
    if not connections.isdigit() or not 1 <= int(connections) <= 4096:
        raise SettingsError(f"{PREFIX}MAX_CONNECTIONS must be a whole number from 1 to 4096.")
    per_network = get("MAX_CONNECTIONS_PER_NETWORK") or str(
        min(defaults.max_connections_per_network, int(connections))
    )
    if not per_network.isdigit() or not 1 <= int(per_network) <= int(connections):
        raise SettingsError(
            f"{PREFIX}MAX_CONNECTIONS_PER_NETWORK must be a whole number from 1 to "
            f"{PREFIX}MAX_CONNECTIONS ({connections}): the connections one build may hold at once."
        )
    return EgressSettings(
        key=Path(get("KEY") or str(defaults.key)),
        listen=listen,
        upstream=upstream,
        max_connections=int(connections),
        max_connections_per_network=int(per_network),
    )


def _ipv4_route(line: str) -> Network | None:
    """Read one line of ``/proc/net/route``.

    Parameters
    ----------
    line
        The line.

    Returns
    -------
    IPv4Network or None
        The network of a route without a gateway; `None` for a route through a gateway or a
        default route.

    Raises
    ------
    ValueError
        For a line not in the kernel's form.
    """
    fields = line.split()
    if len(fields) < 8:
        raise ValueError(f"a route line has {len(fields)} fields")
    destination = ipaddress.IPv4Address(bytes.fromhex(fields[1])[::-1])
    gateway = int(fields[2], 16)
    mask = ipaddress.IPv4Address(bytes.fromhex(fields[7])[::-1])
    if gateway or not int(mask):
        return None
    return ipaddress.IPv4Network(f"{destination}/{mask}", strict=False)


def _ipv6_route(line: str) -> Network | None:
    """Read one line of ``/proc/net/ipv6_route``.

    Parameters
    ----------
    line
        The line.

    Returns
    -------
    IPv6Network or None
        The network of a route without a next hop; `None` for a route through one or a
        default route.

    Raises
    ------
    ValueError
        For a line not in the kernel's form.
    """
    fields = line.split()
    if len(fields) < 10:
        raise ValueError(f"a route line has {len(fields)} fields")
    address = ipaddress.IPv6Address(bytes.fromhex(fields[0]))
    prefix = int(fields[1], 16)
    next_hop = int(fields[4], 16)
    if next_hop or not prefix:
        return None
    return ipaddress.IPv6Network(f"{address}/{prefix}", strict=False)


def _routes(path: Path, skip: int, parse: Callable[[str], Network | None]) -> set[Network]:
    """Read the attached networks of one routing table.

    Parameters
    ----------
    path
        The table.
    skip
        Header lines.
    parse
        Reads one line.

    Returns
    -------
    set
        The networks.

    Raises
    ------
    OSError
        If the table cannot be read.
    ValueError
        If any line is not in the kernel's form: it might be the network that must be refused.
    """
    lines = path.read_text(encoding="ascii").splitlines()[skip:]
    return {n for n in (parse(line) for line in lines if line.strip()) if n is not None}


def connected_networks(route: Path = _ROUTE, route6: Path = _ROUTE6) -> frozenset[Network] | None:
    """Read the networks this container is attached to from the kernel's routing tables.

    Fail closed: any table that cannot be read or any line not understood gives `None`. Only an
    IPv6 table that does not exist (IPv6 off) counts as no IPv6 networks.

    Parameters
    ----------
    route
        ``/proc/net/route``.
    route6
        ``/proc/net/ipv6_route``.

    Returns
    -------
    frozenset or None
        Every network reached without a gateway; `None` when the caller cannot tell internal
        addresses from others (also when the IPv4 table names no attached network).
    """
    try:
        found = _routes(route, 1, _ipv4_route)
        if not found:
            return None
        with contextlib.suppress(FileNotFoundError):  # IPv6 is off
            found |= _routes(route6, 0, _ipv6_route)
    except (OSError, ValueError):
        return None
    return frozenset(found)


def forbidden(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    connected: frozenset[Network],
    reachable: frozenset[Network],
) -> bool:
    """Say whether a direct connection must not go to an address.

    Parameters
    ----------
    address
        A resolved address.
    connected
        The networks the proxy's container is attached to.
    reachable
        `EgressSettings.reachable`.

    Returns
    -------
    bool
        ``True`` for loopback, link-local, multicast, unspecified, reserved, a metadata
        address, or an address on a connected network.
    """
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if any(address in network for network in reachable if network.version == address.version):
        return False
    return (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or any(address in n for n in (*_NEVER, *connected) if n.version == address.version)
    )


def _read_head(connection: socket.socket, deadline: float) -> bytes | int:
    """Read a head up to its empty line, within a deadline and a size.

    It reads one byte at a time, so nothing past the head leaves the socket before the tunnel
    opens; heads are small.

    Parameters
    ----------
    connection
        The socket.
    deadline
        `time.monotonic` value by which the head must be complete.

    Returns
    -------
    bytes or int
        The head without its final CRLF CRLF; 408 past the deadline, 400 when it is too long
        or the peer closed first.
    """
    head = bytearray()
    while not head.endswith(b"\r\n\r\n"):
        # Past the deadline only bytes already waiting are read (bounded by the head size).
        connection.settimeout(max(deadline - time.monotonic(), 0.001))
        try:
            byte = connection.recv(1)
        except TimeoutError:
            return 408
        if not byte or len(head) >= _MAX_HEAD:
            return 400
        head += byte
    return bytes(head[:-4])


def _relay(left: socket.socket, right: socket.socket, until: float) -> None:
    """Copy bytes both ways until a side closes, both idle too long, or the grant expires.

    Parameters
    ----------
    left
        The client's connection.
    right
        The index's (or the upstream proxy's).
    until
        `time.monotonic` value at which the tunnel ends.
    """
    for side in (left, right):
        side.settimeout(_IDLE_SECONDS)  # a peer that stops reading cannot hold `sendall`
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while (remaining := until - time.monotonic()) > 0:
            ready = selector.select(min(_IDLE_SECONDS, remaining))
            if not ready:
                if remaining > _IDLE_SECONDS:
                    return
                continue
            for key, _ in ready:
                source = cast("socket.socket", key.fileobj)
                data = source.recv(65536)
                if not data:
                    return
                key.data.sendall(data)
    except OSError:
        return
    finally:
        selector.close()


class _Refused(Exception):
    """Ends a request with a status and a reason.

    Parameters
    ----------
    status
        The status code.
    reason
        Why, in words, for the client.
    extra
        More header lines, each ending in CRLF.
    """

    def __init__(self, status: int, reason: str, extra: str = "") -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.extra = extra


class _Handler(socketserver.BaseRequestHandler):
    """Serves one CONNECT."""

    request: socket.socket

    @property
    def proxy(self) -> EgressServer:
        """The server this connection belongs to.

        Returns
        -------
        EgressServer
            The proxy.
        """
        return cast("EgressServer", self.server)

    def _answer(self, status: int, reason: str, extra: str = "") -> None:
        """Answer with a status and a one-line reason, then the connection closes.

        Parameters
        ----------
        status
            The status code.
        reason
            Why, in words.
        extra
            More header lines, each ending in CRLF.
        """
        body = f"{reason}\n".encode()
        head = (
            f"HTTP/1.1 {status} {_PHRASES[status]}\r\n{extra}Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        )
        with contextlib.suppress(OSError):
            self.request.settimeout(_LINGER_SECONDS)
            self.request.sendall(head.encode("ascii") + body)
            self.request.shutdown(socket.SHUT_WR)
            # Read what the client still sends for a moment: closing with unread bytes would
            # reset the connection, and the client could lose the answer that says why.
            until = time.monotonic() + _LINGER_SECONDS
            drained = 0
            while drained < _MAX_HEAD * 4 and (left := until - time.monotonic()) > 0:
                self.request.settimeout(left)
                data = self.request.recv(4096)
                if not data:
                    break
                drained += len(data)

    def handle(self) -> None:
        """Serve the connection within its network's and the total limit; never raise."""
        network = self.proxy.network_of(self.request, self.client_address)
        if not self.proxy.enter(network):
            cap = self.proxy.per_network
            self._answer(
                503,
                f"this client's network already holds {cap} connections to the egress proxy, "
                "the most one build may hold at once (LAKANA_EGRESS__MAX_CONNECTIONS_PER_NETWORK"
                "); try again when one of them has ended",
            )
            return
        try:
            if not self.proxy.slots.acquire(blocking=False):
                self._answer(503, "the egress proxy is busy; try again")
                return
            try:
                self._serve()
            except _Refused as refused:
                self._answer(refused.status, refused.reason, refused.extra)
            except OSError:
                return
            finally:
                self.proxy.slots.release()
        finally:
            self.proxy.leave(network)

    def _serve(self) -> None:
        """Read the head, check the token, the client, the host and the port, then tunnel.

        Raises
        ------
        _Refused
            For anything but a CONNECT the token allows.
        """
        head = _read_head(self.request, time.monotonic() + self.proxy.settings.head_seconds)
        if head == 408:
            raise _Refused(408, "the request head did not arrive in time")
        if not isinstance(head, bytes):
            raise _Refused(400, "the request head is too long or incomplete")
        host, port, credential = _parse(head)
        grant = self._grant(credential)
        client = ipaddress.ip_address(self.client_address[0])
        if client.version != grant.network.version or client not in grant.network:
            logger.warning(
                "Refused %s's token from %s: it belongs to network %s.",
                grant.purpose,
                client,
                grant.network,
            )
            raise _Refused(403, "this token belongs to another client network")
        if (host, port) not in grant.targets:
            logger.warning(
                "Refused a %s connection from %s to %s:%d: not in its token.",
                grant.purpose,
                client,
                host,
                port,
            )
            hint = _HINTS.get(grant.purpose, "ask for it to be allowed")
            raise _Refused(
                403,
                f"{host}:{port} is not a destination this "
                f"{_SUBJECTS.get(grant.purpose, grant.purpose)} may reach; {hint}",
            )
        upstream = self.proxy.open(host, port)
        with upstream:
            self.request.settimeout(_IDLE_SECONDS)
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            until = time.monotonic() + max(0.0, grant.expires - time.time())
            _relay(self.request, upstream, until)

    def _grant(self, header: str | None) -> Grant:
        """Check the token in a `Proxy-Authorization` header.

        Parameters
        ----------
        header
            The header's value, or `None` without one.

        Returns
        -------
        Grant
            The token's grant.

        Raises
        ------
        _Refused
            407 without a valid token.
        """
        refused = _Refused(
            407,
            "this proxy serves the containers Lakana starts, each with its own token",
            'Proxy-Authenticate: Basic realm="lakana-egress"\r\n',
        )
        scheme, _, value = (header or "").partition(" ")
        if scheme.lower() != "basic":
            raise refused
        try:
            decoded = base64.b64decode(value.strip(), validate=True).decode("ascii")
        except (binascii.Error, ValueError):
            raise refused from None
        _, colon, token = decoded.partition(":")
        key = self.proxy.key()
        grant = verify(key, token, time.time()) if colon and key is not None else None
        if grant is None:
            raise refused
        return grant


def _drop(counts: dict[Network, int], network: Network) -> None:
    """Count one fewer for a network, forgetting it at zero.

    Parameters
    ----------
    counts
        Counts by client network; the caller holds their lock.
    network
        The network.
    """
    left = counts.get(network, 0) - 1
    if left > 0:
        counts[network] = left
    else:
        counts.pop(network, None)


def _parse(head: bytes) -> tuple[str, int, str | None]:
    """Read a CONNECT head strictly: anything two parsers could read differently is refused.

    Parameters
    ----------
    head
        The head without its final CRLF CRLF.

    Returns
    -------
    tuple
        The lower-case host, the port, and the `Proxy-Authorization` value (`None` without one).

    Raises
    ------
    _Refused
        405 for another method, 400 for anything malformed.
    """
    lines = head.decode("latin-1").split("\r\n")
    if any("\r" in line or "\n" in line for line in lines):
        raise _Refused(400, "lines must end in CRLF")
    words = lines[0].split(" ")
    if words[0] != "CONNECT":
        raise _Refused(405, "this proxy only tunnels to https destinations (CONNECT)")
    if len(words) != 3 or words[2] not in ("HTTP/1.1", "HTTP/1.0"):
        raise _Refused(400, "CONNECT host:port HTTP/1.1")
    if len(lines) - 1 > _MAX_HEADERS:
        raise _Refused(400, f"more than {_MAX_HEADERS} header lines")
    credential: str | None = None
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if not colon or not name or name != name.strip() or not name.isascii():
            raise _Refused(400, "a header line must be name: value, without folding")
        if name.lower() == "proxy-authorization":
            if credential is not None:
                raise _Refused(400, "one Proxy-Authorization header only")
            credential = value.strip()
    host, _, port_text = words[1].rpartition(":")
    host = host.lower()
    port = _port(port_text)
    if not valid_host(host) or port is None:
        raise _Refused(400, "CONNECT takes host:port: a host name or IPv4 address and a port")
    return host, port, credential


class EgressServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """The proxy.

    Parameters
    ----------
    settings
        Its settings.
    """

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, settings: EgressSettings) -> None:
        self.settings = settings
        self.per_network = min(settings.max_connections_per_network, settings.max_connections)
        self.slots = threading.BoundedSemaphore(settings.max_connections)
        self.threads = threading.BoundedSemaphore(settings.max_connections + _SPARE_THREADS)
        self._key: tuple[tuple[int, int, int], bytes | None] = ((-1, -1, -1), None)
        self._key_lock = threading.Lock()
        self._count_lock = threading.Lock()
        self._threads_by: dict[Network, int] = {}
        self._slots_by: dict[Network, int] = {}
        self._network_of: dict[object, Network] = {}
        super().__init__(settings.listen, _Handler)

    def client_network(self, client_address: object) -> Network:
        """Name the client network a connection counts against.

        Only the peer's host counts; its port does not. Tests replace this method to give
        connections from one loopback address network identities of their own.

        Parameters
        ----------
        client_address
            The peer, ``(host, port)``.

        Returns
        -------
        IPv4Network or IPv6Network
            The most specific attached network that holds the address (a build's /29); the
            address alone when none does or the routing table cannot be read.
        """
        address = ipaddress.ip_address(str(cast("tuple[str, int]", client_address)[0]))
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        connected = connected_networks(self.settings.route, self.settings.route6) or frozenset()
        found = [n for n in connected if n.version == address.version and address in n]
        return max(found, key=lambda n: n.prefixlen) if found else ipaddress.ip_network(address)

    def network_of(self, request: object, client_address: object) -> Network:
        """Return the client network `process_request` counted a connection's thread against.

        Parameters
        ----------
        request
            The connection.
        client_address
            Its peer, for a connection served without `process_request`.

        Returns
        -------
        IPv4Network or IPv6Network
            Its client network.
        """
        with self._count_lock:
            found = self._network_of.get(request)
        if found is not None:
            return found
        return self.client_network(client_address)

    def enter(self, network: Network) -> bool:
        """Take one of a client network's connection slots.

        Parameters
        ----------
        network
            The client network.

        Returns
        -------
        bool
            `False` when it holds `per_network` already.
        """
        with self._count_lock:
            held = self._slots_by.get(network, 0)
            if held >= self.per_network:
                return False
            self._slots_by[network] = held + 1
        return True

    def leave(self, network: Network) -> None:
        """Give back one of a client network's connection slots.

        Parameters
        ----------
        network
            The client network.
        """
        with self._count_lock:
            _drop(self._slots_by, network)

    def slots_held(self) -> int:
        """Count the connections the client networks hold.

        Returns
        -------
        int
            How many connections are being served now.
        """
        with self._count_lock:
            return sum(self._slots_by.values())

    def threads_of(self, network: Network) -> int:
        """Count the threads serving or answering one client network's connections.

        Parameters
        ----------
        network
            The client network.

        Returns
        -------
        int
            At most `per_network` plus `_SPARE_PER_NETWORK`.
        """
        with self._count_lock:
            return self._threads_by.get(network, 0)

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        """Serve a connection on a thread of its own, or close it when too many threads run.

        Past `max_connections`, a few more threads answer 503; beyond those a flood of
        connections is closed at once rather than given a thread each. The same holds for one
        client network past its cap, before any other network's threads are touched.

        Parameters
        ----------
        request
            The connection.
        client_address
            Its peer.
        """
        network = self.client_network(client_address)
        with self._count_lock:
            busy = self._threads_by.get(network, 0)
            if busy >= self.per_network + _SPARE_PER_NETWORK:
                network_full = True
            else:
                network_full = False
                self._threads_by[network] = busy + 1
                self._network_of[request] = network
        if network_full:
            self.shutdown_request(request)
            return
        if not self.threads.acquire(blocking=False):
            self._forget(request)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.threads.release()
            self._forget(request)
            raise

    def _forget(self, request: object) -> None:
        """Give back a connection's thread ticket of its client network.

        Parameters
        ----------
        request
            The connection.
        """
        with self._count_lock:
            network = self._network_of.pop(request, None)
            if network is not None:
                _drop(self._threads_by, network)

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        """Serve one connection, then give its thread back.

        Parameters
        ----------
        request
            The connection.
        client_address
            Its peer.
        """
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.threads.release()
            self._forget(request)

    def handle_error(self, request: object, client_address: object) -> None:
        """Log an unexpected error of one connection briefly; the proxy goes on.

        Parameters
        ----------
        request
            The connection.
        client_address
            Its peer.
        """
        logger.exception("An egress connection from %s failed.", client_address)

    def key(self) -> bytes | None:
        """Read the launcher's key, again whenever the file changed.

        Returns
        -------
        bytes or None
            The key; `None` until the launcher has written it, and for a read that fails. A
            failed read is not remembered: the next connection reads the file again.
        """
        try:
            info = os.stat(self.settings.key)
        except OSError:
            return None
        stamp = (info.st_ino, info.st_mtime_ns, info.st_size)
        with self._key_lock:
            if self._key[0] != stamp:
                key = read_key(self.settings.key)
                if key is None:
                    return None
                self._key = (stamp, key)
            return self._key[1]

    def open(self, host: str, port: int) -> socket.socket:
        """Connect to a host, directly or through the upstream proxy.

        Parameters
        ----------
        host
            The destination's host.
        port
            Its port.

        Returns
        -------
        socket.socket
            The connection, ready to relay.

        Raises
        ------
        _Refused
            403 when the host resolves only to forbidden addresses; 502 when it cannot be
            reached or the upstream proxy refuses.
        """
        if self.settings.upstream is not None:
            return self._through_upstream(host, port)
        try:
            found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise _Refused(502, f"cannot resolve {host} ({exc.strerror or exc})") from None
        connected = connected_networks(self.settings.route, self.settings.route6)
        if connected is None:
            logger.error(
                "Refused %s:%d: cannot read the attached networks from %s, so internal addresses "
                "cannot be told apart; no direct connection is made.",
                host,
                port,
                self.settings.route,
            )
            raise _Refused(
                503,
                "the egress proxy cannot read its routing table, so it makes no direct "
                "connection; see `docker compose logs egress`",
            )
        allowed = [
            entry
            for entry in found
            if not forbidden(ipaddress.ip_address(entry[4][0]), connected, self.settings.reachable)
        ]
        if not allowed:
            logger.warning("Refused %s:%d: it resolves only to forbidden addresses.", host, port)
            raise _Refused(
                403,
                f"{host}:{port} is not reachable through this proxy: it resolves to a loopback, "
                "link-local, metadata or internal address",
            )
        error: OSError | None = None
        for family, kind, proto, _, address in allowed:
            connection = socket.socket(family, kind, proto)
            try:
                connection.settimeout(_CONNECT_SECONDS)
                connection.connect(address)
            except OSError as exc:
                connection.close()
                error = exc
                continue
            return connection
        detail = (error.strerror if error is not None else None) or str(error)
        raise _Refused(502, f"cannot reach {host}:{port} ({detail})")

    def _through_upstream(self, host: str, port: int) -> socket.socket:
        """Open a tunnel through the upstream proxy.

        Parameters
        ----------
        host
            The destination's host, checked by `valid_host`, so it cannot break the request.
        port
            Its port.

        Returns
        -------
        socket.socket
            The tunnel.

        Raises
        ------
        _Refused
            502 when the upstream proxy cannot be reached or does not answer 200.
        """
        parts = urlsplit(self.settings.upstream or "")
        try:
            connection = socket.create_connection(
                (parts.hostname or "", parts.port or PORT), timeout=_CONNECT_SECONDS
            )
        except OSError as exc:
            raise _Refused(
                502, f"cannot reach the upstream proxy ({exc.strerror or type(exc).__name__})"
            ) from None
        auth = ""
        if parts.username is not None:
            pair = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
            auth = f"Proxy-Authorization: Basic {base64.b64encode(pair).decode('ascii')}\r\n"
        try:
            connection.sendall(
                f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode()
            )
            head = _read_head(connection, time.monotonic() + _CONNECT_SECONDS)
        except OSError:
            head = 400
        first = head.split(b"\r\n", 1)[0] if isinstance(head, bytes) else b""
        words = first.split()
        if len(words) < 2 or not words[0].startswith(b"HTTP/1.") or words[1] != b"200":
            connection.close()
            shown = first.decode("latin-1")[:100].strip() or "no answer"
            raise _Refused(502, f"the upstream proxy refused {host}:{port}: {shown}")
        return connection


def main() -> None:
    """Run the proxy until the process ends.

    Raises
    ------
    SystemExit
        With status 2 and the reason in the log for a bad setting.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        settings = load_settings(os.environ)
    except SettingsError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from None
    server = EgressServer(settings)
    logger.info("The egress proxy listens on %s:%d.", *settings.listen)
    if server.key() is None:
        logger.warning(
            "No key at %s yet: every client is refused until the launcher writes it "
            "(docker compose logs launcher).",
            settings.key,
        )
    server.serve_forever()


if __name__ == "__main__":
    main()
