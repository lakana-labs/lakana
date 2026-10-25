"""A task's relay: one port per database connection, forwarded to exactly its target.

A database protocol cannot go through the HTTP egress proxy, so a task whose asset declares a
PostgreSQL, MariaDB or tcp connection gets this small process in a container of its own,
attached to the task's network and to the network the egress proxy reaches out through. It
listens only on its address in the task's /29, one port per route, at most 16 connections per
port, and closes a connection from any other address. Each accepted connection resolves its
target once and checks every address as the proxy does (never loopback, link-local, metadata,
multicast, reserved, a network the relay is attached to or the launcher's range of task
networks; no direct connection when the routes cannot be read), then dials through the proxy's
`dial`, so an upstream proxy applies the same way. The relay never looks at the bytes: TLS, if
any, stays end to end. Why a connection was refused goes to stderr, which the launcher copies
into the task's log.

The plan arrives on stdin as one JSON object:
``{"address", "subnet", "internal", "routes": [{"listen", "host", "port"}]}``.
Standard library only, like the launcher.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from lakana_worker.launcher.egress import (
    DialRefused,
    EgressSettings,
    SettingsError,
    dial,
    load_settings,
    relay_bytes,
)
from lakana_worker.launcher.egress_token import valid_host
from lakana_worker.launcher.requests import (
    MAX_RELAY_ROUTES,
    RELAY_MAX_PER_PORT,
    RELAY_PORTS,
    RelayRoute,
)

MAX_PER_PORT = RELAY_MAX_PER_PORT
"""The most connections one route carries at once."""
READY = "ready"
"""The line the relay writes once every port listens."""
_MAX_PLAN = 64 * 1024
_FOREVER = 7 * 86400.0
"""A relay connection ends when a side closes or the container goes, never for silence."""
_INTERNAL_HINT = (
    "give the connection an address outside the Lakana stack (lakana connection update)"
)


@dataclass(frozen=True)
class RelayPlan:
    """What the relay serves.

    Parameters
    ----------
    address
        Its address in the task's network; the only one it listens on.
    subnet
        The task's /29; a client from elsewhere is closed.
    internal
        The launcher's range of task networks, refused as a target.
    routes
        One per database connection.
    """

    address: ipaddress.IPv4Address
    subnet: ipaddress.IPv4Network
    internal: ipaddress.IPv4Network
    routes: tuple[RelayRoute, ...]


def say(text: str) -> None:
    """Write one line for the task's log.

    Parameters
    ----------
    text
        The line, without the prefix.
    """
    sys.stderr.write(f"lakana-relay: {text}\n")
    sys.stderr.flush()


def parse_plan(text: str) -> RelayPlan:
    """Read the plan from stdin's text.

    Parameters
    ----------
    text
        The JSON document.

    Returns
    -------
    RelayPlan
        The plan.

    Raises
    ------
    ValueError
        Naming the first field that breaks its rule.
    """
    if len(text) > _MAX_PLAN:
        raise ValueError("the plan is larger than 64 KiB")
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("the plan is one JSON object") from None
    if not isinstance(data, dict) or set(data) != {"address", "subnet", "internal", "routes"}:
        raise ValueError("the plan has exactly address, subnet, internal and routes")
    try:
        subnet = ipaddress.IPv4Network(str(data["subnet"]))
        internal = ipaddress.IPv4Network(str(data["internal"]))
        address = ipaddress.IPv4Address(str(data["address"]))
    except ValueError:
        raise ValueError("address, subnet and internal are IPv4 in CIDR form") from None
    if subnet.prefixlen != 29 or address not in subnet.hosts():
        raise ValueError("address must be a host address of subnet, a /29")
    raw = data["routes"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_RELAY_ROUTES:
        raise ValueError(f"routes: 1 to {MAX_RELAY_ROUTES}")
    routes: list[RelayRoute] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"listen", "host", "port"}:
            raise ValueError("routes: each has exactly listen, host and port")
        listen, host, port = item["listen"], item["host"], item["port"]
        if type(listen) is not int or listen not in RELAY_PORTS:
            raise ValueError(
                f"routes: listen ports are {RELAY_PORTS.start} to {RELAY_PORTS.stop - 1}"
            )
        if not isinstance(host, str) or not valid_host(host):
            raise ValueError("routes: a lower-case host")
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError("routes: a port from 1 to 65535")
        routes.append(RelayRoute(listen, host, port))
    if len({route.listen for route in routes}) != len(routes):
        raise ValueError("routes: listen ports are distinct")
    return RelayPlan(address, subnet, internal, tuple(routes))


def _handle(
    client: socket.socket,
    route: RelayRoute,
    plan: RelayPlan,
    settings: EgressSettings,
    slots: threading.BoundedSemaphore,
    log: Callable[[str], None],
) -> None:
    """Carry one connection to its route's target, then release its slot.

    Parameters
    ----------
    client
        The accepted connection.
    route
        Its route.
    plan
        For the launcher's range.
    settings
        For `dial`.
    slots
        The route's connection limit.
    log
        Where refusals are written.
    """
    try:
        try:
            upstream = dial(route.host, route.port, settings, refused=frozenset({plan.internal}))
        except DialRefused as exc:
            hint = f"; {_INTERNAL_HINT}" if exc.status == 403 else ""
            log(f"refused {route.host}:{route.port}: {exc.reason}{hint}")
            return
        with upstream:
            relay_bytes(client, upstream, time.monotonic() + _FOREVER, idle=_FOREVER)
    finally:
        client.close()
        slots.release()


def _accept(
    server: socket.socket,
    route: RelayRoute,
    plan: RelayPlan,
    settings: EgressSettings,
    log: Callable[[str], None],
) -> None:
    """Accept connections on one route's port until the socket closes.

    Parameters
    ----------
    server
        The listening socket.
    route
        Its route.
    plan
        For the subnet check.
    settings
        For `dial`.
    log
        Where closures and refusals are written.
    """
    slots = threading.BoundedSemaphore(MAX_PER_PORT)
    while True:
        try:
            client, peer = server.accept()
        except OSError:
            return  # the socket was closed: the relay is ending
        if ipaddress.ip_address(peer[0]) not in plan.subnet:
            log(f"closed a connection from {peer[0]}, which is not on the task's network")
            client.close()
            continue
        if not slots.acquire(blocking=False):
            log(
                f"closed a connection to {route.host}:{route.port}: {MAX_PER_PORT} are open "
                "already, the most one connection of a task may hold"
            )
            client.close()
            continue
        try:
            threading.Thread(
                target=_handle, args=(client, route, plan, settings, slots, log), daemon=True
            ).start()
        except RuntimeError as exc:  # the container's process limit: this port must live on
            log(
                f"closed a connection to {route.host}:{route.port}: the relay cannot start "
                f"another thread ({exc})"
            )
            client.close()
            slots.release()


def serve(
    plan: RelayPlan,
    settings: EgressSettings,
    *,
    ready: Callable[[], None],
    stop: threading.Event | None = None,
    log: Callable[[str], None] = say,
) -> None:
    """Listen on every route's port, then serve until `stop` is set (for ever without one).

    Parameters
    ----------
    plan
        The plan.
    settings
        For `dial`.
    ready
        Called once every port listens.
    stop
        Ends the serving; tests use it, the container's removal ends a real relay.
    log
        Where closures and refusals are written; stderr by default.
    """
    servers: list[socket.socket] = []
    try:
        for route in plan.routes:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            servers.append(server)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((str(plan.address), route.listen))
            server.listen(MAX_PER_PORT)
        for server, route in zip(servers, plan.routes, strict=True):
            threading.Thread(
                target=_accept, args=(server, route, plan, settings, log), daemon=True
            ).start()
        ready()
        (stop or threading.Event()).wait()
    finally:
        for server in servers:
            with contextlib.suppress(OSError):
                server.shutdown(socket.SHUT_RDWR)  # wakes the thread blocked in accept()
            server.close()


def main() -> None:
    """Read the plan and serve until the container is removed.

    Raises
    ------
    SystemExit
        With status 2 and the reason on stderr for a bad plan, a bad setting or a port that
        cannot be bound.
    """
    try:
        plan = parse_plan(sys.stdin.read())
        settings = load_settings(os.environ)
    except (ValueError, SettingsError) as exc:
        say(f"cannot start: {exc}")
        raise SystemExit(2) from None
    try:
        serve(plan, settings, ready=lambda: say(READY))
    except OSError as exc:
        say(f"cannot start: {exc.strerror or exc}")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
