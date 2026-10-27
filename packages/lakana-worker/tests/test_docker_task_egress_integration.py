"""A task reaches exactly the connections its asset declares, on a real Docker Engine.

A source container on a labelled network of its own serves TLS on 8443 (a leaf of a private
authority for ``target.lakana.test``), a stand-in PostgreSQL on 5432 that answers the first
message of a client, and a TCP echo on 9000. The egress service is created on that network,
which is its outward network: a task's relay joins it too. Docker keeps bridges apart, so both
are attached to the source's network, which they otherwise refuse; the proxy is told it may
reach it (`start_egress`'s ``reachable``) and the relay likewise (the test hook
`LauncherSettings.relay_reachable`). A refused range still wins over both, which is how a range
of `LAKANA_LAUNCHER__EGRESS_REFUSED` is proven on the very target that is reached without it.

`start_egress` copies the refused ranges into the proxy each time the launcher writes them, so
the gateways of networks made after the launcher started are refused as in production. Each
probe is the task; the launcher runs it as the Docker runner would, with ``egress`` and
``relay`` as the worker sends them for declared connections.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import docker_stack
import pytest
from docker_stack import (
    APP,
    PYTHON,
    Stack,
    build_task_image,
    docker_client,
    serve,
    stack,
    start_egress,
)
from tls_fixture import Authority, issue_leaf, make_authority

from lakana_core.connections.endpoints import endpoints
from lakana_core.connections.kinds import ConnectionKind
from lakana_worker import connection_tests
from lakana_worker.launcher import egress
from lakana_worker.launcher.requests import (
    RelayRoute,
    StopRequest,
    container_name,
    environment_volume,
)
from lakana_worker.launcher_client import LauncherClient
from lakana_worker.python_tasks import connection_plan
from lakana_worker.runners import DockerRunner, PythonTask, StopSignal
from lakana_worker.trust import build_trust_bundle

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.slow]
"""Each case starts containers and takes seconds. The slow-test ratchet reads only the unit
shards' reports, which never hold an integration test, so the marker does not count there."""

_HOST = "target.lakana.test"
_GOOGLE = ("oauth2.googleapis.com", "storage.googleapis.com")
"""The source answers for Google's two hosts too (port 443), under their real names, which the
egress proxy resolves on the source's network: a gcs connection's endpoints, unchanged."""
_HINT = "give the connection an address outside the Lakana stack (lakana connection update)"
_SSL_REQUEST = b"\x00\x00\x00\x08\x04\xd2\x16\x2f"
"""The first message of a PostgreSQL client that asks for TLS; a server without it says ``N``."""

_SOURCE = f"""import base64, json, socket, ssl, threading, urllib.parse
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain({APP!r} + "/leaf.pem", {APP!r} + "/leaf.key")
def https(connection):
    try:
        with context.wrap_socket(connection, server_side=True) as tls:
            tls.recv(4096)
            tls.sendall(b"HTTP/1.0 200 OK\\r\\nContent-Length: 13\\r\\n\\r\\nhello, source")
    except (OSError, ssl.SSLError):
        pass
def postgres(connection):
    with connection:
        if connection.recv(8) == {_SSL_REQUEST!r}:
            connection.sendall(b"N")
def echo(connection):
    with connection:
        while data := connection.recv(65536):
            connection.sendall(data)
def google(connection):
    # Google's token endpoint and the GCS listing, by their real names: the token is given
    # to an assertion whose key id is "right"; the listing wants that token.
    try:
        with context.wrap_socket(connection, server_side=True) as tls:
            data = b""
            while b"\\r\\n\\r\\n" not in data:
                chunk = tls.recv(4096)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b"\\r\\n\\r\\n")
            lines = head.decode("latin-1").split("\\r\\n")
            headers = {{k.strip().lower(): v.strip() for k, _, v in
                       (line.partition(":") for line in lines[1:])}}
            while len(body) < int(headers.get("content-length", "0")):
                body += tls.recv(4096)
            method, target = lines[0].split(" ")[:2]
            if method == "POST" and target == "/token":
                form = dict(urllib.parse.parse_qsl(body.decode()))
                part = form.get("assertion", "").split(".")[0]
                header = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
                if header.get("kid") == "right":
                    status, answer = 200, {{"access_token": "tok-right", "expires_in": 3599}}
                else:
                    status, answer = 400, {{"error": "invalid_grant",
                                           "error_description": "Invalid JWT Signature."}}
            elif method == "GET" and target.startswith("/storage/v1/b/landing/o?"):
                good = headers.get("authorization") == "Bearer tok-right"
                status, answer = (200, {{"items": []}}) if good else (401, {{}})
            elif method == "GET" and target == "/api":
                good = headers.get("authorization") == "Bearer api-right"
                status, answer = (200, {{}}) if good else (401, {{}})
            elif method == "POST" and target == "/api-token":
                form = dict(urllib.parse.parse_qsl(body.decode()))
                if (form.get("grant_type") == "client_credentials"
                        and form.get("client_id") == "lakana"):
                    status, answer = 200, {{"access_token": "api-right"}}
                else:
                    status, answer = 401, {{}}
            else:
                status, answer = 404, {{}}
            out = json.dumps(answer).encode()
            tls.sendall(b"HTTP/1.1 %d X\\r\\nContent-Length: %d\\r\\nConnection: close\\r\\n\\r\\n"
                        % (status, len(out)) + out)
    except (OSError, ssl.SSLError, ValueError):
        pass
def serve(port, handler):
    server = socket.create_server(("0.0.0.0", port))
    while True:
        connection, _ = server.accept()
        threading.Thread(target=handler, args=(connection,), daemon=True).start()
held = socket.create_server(("0.0.0.0", 7000), backlog=0)
fillers = []
for _ in range(2):
    filler = socket.socket()
    filler.setblocking(False)
    filler.connect_ex(("127.0.0.1", 7000))
    fillers.append(filler)
threading.Thread(target=serve, args=(5432, postgres), daemon=True).start()
threading.Thread(target=serve, args=(9000, echo), daemon=True).start()
threading.Thread(target=serve, args=(443, google), daemon=True).start()
serve(8443, https)
"""
"""The source: TLS on 8443, the first PostgreSQL answer on 5432, an echo on 9000, and on 7000 a
listener whose one-place queue its own connections fill and that never accepts, so the kernel
drops every later SYN: a blackholed port."""

_NEIGHBOUR_HOST = "postgres.lakana.test"
_NEIGHBOUR = """import socket
server = socket.create_server(("0.0.0.0", 5432))
while True:
    connection, _ = server.accept()
    connection.sendall(b"N")
    connection.close()
"""
"""A stand-in for a service of the stack (Postgres) beside the source: it answers anyone."""

_HELPERS = r"""
import base64, hashlib, json, os, socket, ssl, sys, time
from urllib.parse import urlsplit
def connect(host, port, proxy=None):
    url = urlsplit(proxy or os.environ["LAKANA_EGRESS_PROXY"])
    secret = base64.b64encode(f"{url.username}:{url.password}".encode()).decode()
    sock = socket.create_connection((url.hostname, url.port), timeout=20)
    sock.sendall(
        f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Proxy-Authorization: Basic {secret}\r\n\r\n".encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        byte = sock.recv(1)
        if not byte:
            break
        head += byte
    lines = head.decode().split("\r\n")
    if lines[0].split()[1:2] == ["200"]:
        return lines[0], None, sock
    length = 0
    for line in lines[1:]:
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1])
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            break
        body += chunk
    sock.close()
    return lines[0], body.decode().rstrip("\n"), None
def fetch(host, port, proxy=None):
    status, body, sock = connect(host, port, proxy)
    if sock is None:
        return {"status": status, "body": body}
    with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
        tls.sendall(b"GET / HTTP/1.0\r\n\r\n")
        data = b""
        while chunk := tls.recv(4096):
            data += chunk
    return {"status": status, "body": data.decode().split("\r\n\r\n", 1)[-1]}
def relay(port, payload=b"", size=None):
    try:
        with socket.create_connection(("lakana-relay", port), timeout=20) as sock:
            sock.sendall(payload)
            received = b""
            while len(received) < (len(payload) if size is None else size):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                received += chunk
        return received
    except OSError as exc:
        return type(exc).__name__
    finally:
        time.sleep(1)  # the relay's line reaches the run's log before the task ends
def report(**seen):
    print(json.dumps(seen, default=lambda raw: hashlib.sha256(raw).hexdigest()
                     if len(raw) > 64 else raw.decode("latin-1")))
"""
"""What every probe uses: a CONNECT through the task's proxy, TLS on top, and the relay."""


@dataclass
class Sources:
    """The module's stack with its source and egress service.

    Parameters
    ----------
    stack
        The stack, its egress service on the source's network.
    network
        The source's network, the egress service's outward network.
    subnet
        That network's IPv4 range.
    bundle
        The trust bundle with the private authority of the source's certificate.
    bridge_gateway
        The gateway of Docker's default bridge, where the Docker host answers.
    neighbour
        The address of a second container on the source's network, which the test hooks do not
        make reachable: a service of the stack on the egress service's network, as Postgres is
        on the Compose bundle's.
    egress
        The egress container, whose copy of the refused ranges a test reads.
    authority
        The private authority of the source's certificate, for a second server's.
    material
        A directory for that server's certificate files.
    """

    stack: Stack
    network: str
    subnet: str
    bundle: bytes
    bridge_gateway: str
    neighbour: str
    egress: Any
    authority: Authority
    material: Path


def _files(entries: dict[str, bytes]) -> bytes:
    """Pack files for the source container, readable by its user only.

    Parameters
    ----------
    entries
        Name to content.

    Returns
    -------
    bytes
        The archive.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.uid, info.gid = len(data), 0o400, 10001, 10001
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _address(container: Any, network: str) -> str:  # ruff: ignore[any-type] (Docker SDK)
    """Read a container's address on a network.

    Parameters
    ----------
    container
        The container.
    network
        The network's name.

    Returns
    -------
    str
        Its IPv4 address there.
    """
    container.reload()
    return str(container.attrs["NetworkSettings"]["Networks"][network]["IPAddress"])


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Sources]:
    """Start a stack, a source on a network of its own, and the egress service on it.

    Parameters
    ----------
    tmp_path_factory
        For the launcher's state and the authority.

    Yields
    ------
    Sources
        The stack and what the tests need to know about the source.
    """
    material = tmp_path_factory.mktemp("authority")
    authority = make_authority(material)
    certificate, key = issue_leaf(authority, material, names=(_HOST, *_GOOGLE))
    bundle = build_trust_bundle(authority.ca_file)
    assert bundle is not None
    client = docker_client()
    image = build_task_image(client)
    try:
        with stack(client, image, tmp_path_factory.mktemp("stack")) as current:
            network: Any = client.networks.create(
                f"{current.name}-sources", driver="bridge", labels=current.labels
            )
            network.reload()
            subnet = str(network.attrs["IPAM"]["Config"][0]["Subnet"])
            source = client.containers.create(
                image,
                ["python", "-I", "-c", _SOURCE],
                user="10001:10001",
                labels=current.labels,
                network=network.name,
                networking_config={
                    network.name: client.api.create_endpoint_config(aliases=[_HOST, *_GOOGLE])
                },
            )
            source.put_archive(
                APP,
                _files({"leaf.pem": certificate.read_bytes(), "leaf.key": key.read_bytes()}),
            )
            source.start()
            neighbour = client.containers.run(
                image,
                ["python", "-I", "-c", _NEIGHBOUR],
                detach=True,
                user="10001:10001",
                labels=current.labels,
                network=network.name,
                networking_config={
                    network.name: client.api.create_endpoint_config(aliases=[_NEIGHBOUR_HOST])
                },
            )
            # Only the source itself is made reachable: the rest of its network stays attached,
            # and so refused, as the Compose network is for the real egress service and relays.
            reachable = f"{_address(source, network.name)}/32"
            egress = start_egress(current, network=network.name, reachable=reachable)
            bridge = client.networks.get("bridge").attrs["IPAM"]["Config"][0]["Gateway"]
            with current.configured(relay_reachable=(reachable,)):
                yield Sources(
                    current,
                    network.name,
                    subnet,
                    bundle,
                    str(bridge),
                    _address(neighbour, network.name),
                    egress,
                    authority,
                    material,
                )
    finally:
        client.close()


def _run(
    sources: Sources,
    probe: str,
    *,
    egress_pairs: tuple[tuple[str, int], ...] = (),
    relay: tuple[RelayRoute, ...] = (),
    task: str | None = None,
    timeout: int = 60,
) -> tuple[dict[str, Any], str]:
    """Run a probe as a task with routes, and read what it reported.

    Parameters
    ----------
    sources
        The module's stack.
    probe
        The probe's own lines, after `_HELPERS`; its last call is `report`.
    egress_pairs
        The host:port pairs of its declared HTTPS connections.
    relay
        The routes of its declared database and tcp connections.
    task
        Its id; a random one by default.
    timeout
        Its timeout.

    Returns
    -------
    tuple
        The last JSON line the probe printed (``{}`` when there is none) and the whole log.
    """
    attempt = sources.stack.attempt(_HELPERS + probe, trust=sources.bundle)
    result, output = sources.stack.run(
        attempt, egress=egress_pairs, relay=relay, task=task, timeout=timeout, trust=True
    )
    text = output.decode(errors="replace")
    assert result["exit"] == 0, (result, text)
    reports = [line for line in text.splitlines() if line.startswith("{")]
    return (json.loads(reports[-1]) if reports else {}), text


def _internal(host: str, port: int) -> str:
    """Write the proxy's refusal of an internal address, for a task.

    Parameters
    ----------
    host
        The host as the task asked for it.
    port
        Its port.

    Returns
    -------
    str
        The body of the 403.
    """
    return f"{host}:{port} is not reachable through this proxy: {egress.INTERNAL}; {_HINT}"


def _relay_refused(host: str, port: int) -> str:
    """Write the relay's line in the task's log for an internal address.

    Parameters
    ----------
    host
        The route's host.
    port
        Its port.

    Returns
    -------
    str
        The line.
    """
    return f"lakana-relay: refused {host}:{port}: {egress.INTERNAL}; {_HINT}"


def _in_background(work: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    """Run a call on a thread, keeping what it returned or raised.

    Parameters
    ----------
    work
        The call.

    Returns
    -------
    tuple
        The started thread and a list that receives the result or the exception.
    """
    outcome: list[object] = []

    def target() -> None:
        """Run the call and keep its outcome."""
        try:
            outcome.append(work())
        except Exception as exc:  # handed to the test, which names it
            outcome.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcome


# --- what a task reaches ----------------------------------------------------------------------


def test_a_declared_https_target_is_reachable_through_the_proxy(sources: Sources) -> None:
    """CONNECT with the task's `LAKANA_EGRESS_PROXY`, then TLS trusting the private authority."""
    seen, _ = _run(
        sources,
        f"report(https=fetch({_HOST!r}, 8443), variables=sorted(k for k in os.environ "
        "if 'PROXY' in k.upper()))\n",
        egress_pairs=((_HOST, 8443),),
    )

    assert seen["https"]["status"].split()[1] == "200"
    assert seen["https"]["body"] == "hello, source"
    assert seen["variables"] == ["LAKANA_EGRESS_PROXY"]


def test_a_declared_database_target_is_reachable_through_the_relay(sources: Sources) -> None:
    """The first PostgreSQL message to ``lakana-relay:15001`` gets the source's answer."""
    seen, _ = _run(
        sources,
        f"report(answer=relay(15001, {_SSL_REQUEST!r}, size=1))\n",
        relay=(RelayRoute(15001, _HOST, 5432),),
    )

    assert seen["answer"] == "N"


def test_a_tcp_route_carries_any_bytes(sources: Sources) -> None:
    """Every byte value, a mebibyte of them, comes back unchanged through a ``tcp`` route."""
    payload = bytes(range(256)) * 4096
    seen, _ = _run(
        sources,
        "report(echo=relay(15002, bytes(range(256)) * 4096))\n",
        relay=(RelayRoute(15001, _HOST, 5432), RelayRoute(15002, _HOST, 9000)),
    )

    assert seen["echo"] == hashlib.sha256(payload).hexdigest()


# --- what it does not -------------------------------------------------------------------------


def test_an_undeclared_host_is_refused_with_the_task_hint(sources: Sources) -> None:
    """A host:port its token does not name: 403 naming what to declare and create."""
    seen, _ = _run(
        sources,
        f"report(other=fetch('example.lakana.test', 443), port=fetch({_HOST!r}, 9000))\n",
        egress_pairs=((_HOST, 8443),),
    )

    hint = "declare it in @asset(connections=[...]) and create it with lakana connection create"
    assert seen["other"] == {
        "status": "HTTP/1.1 403 Forbidden",
        "body": f"example.lakana.test:443 is not a destination this task may reach; {hint}",
    }
    assert seen["port"]["body"] == f"{_HOST}:9000 is not a destination this task may reach; {hint}"


def test_the_metadata_address_is_refused(sources: Sources) -> None:
    """Declared or not, the cloud metadata address is refused by the proxy and the relay."""
    seen, log = _run(
        sources,
        "report(proxy=fetch('169.254.169.254', 80), relay=relay(15001, size=1))\n",
        egress_pairs=(("169.254.169.254", 80),),
        relay=(RelayRoute(15001, "169.254.169.254", 80),),
    )

    assert seen["proxy"] == {
        "status": "HTTP/1.1 403 Forbidden",
        "body": _internal("169.254.169.254", 80),
    }
    assert seen["relay"] == ""
    assert _relay_refused("169.254.169.254", 80) in log.splitlines()


def test_the_tasks_own_network_is_refused(sources: Sources) -> None:
    """The stack's server, on the task's network, is refused through proxy and relay.

    Its address is in the launcher's task range, which is refused as well as attached; the next
    test proves the attached rule on its own.
    """
    seen, log = _run(
        sources,
        "report(proxy=fetch('server', 8000), relay=relay(15001, size=1))\n",
        egress_pairs=(("server", 8000),),
        relay=(RelayRoute(15001, "server", 8000),),
    )

    assert seen["proxy"] == {"status": "HTTP/1.1 403 Forbidden", "body": _internal("server", 8000)}
    assert seen["relay"] == ""
    assert _relay_refused("server", 8000) in log.splitlines()


def test_an_attached_network_outside_every_refused_range_is_refused(sources: Sources) -> None:
    """A service beside the source, on the egress service's network, in no refused range.

    Only the rule that the proxy and the relay never connect into a network they are attached
    to (read from their kernel's routes) refuses it: this is how they keep away from Postgres and
    SeaweedFS on the Compose bundle's network.
    """
    assert sources.stack.launcher is not None
    address = ipaddress.ip_address(sources.neighbour)
    refused = [ipaddress.ip_network(r) for r in sources.stack.launcher._refused]
    assert not [r for r in refused if address in r], refused
    host = _NEIGHBOUR_HOST
    seen, log = _run(
        sources,
        f"report(proxy=fetch({host!r}, 5432), relay=relay(15001, size=1))\n",
        egress_pairs=((host, 5432),),
        relay=(RelayRoute(15001, host, 5432),),
    )

    assert seen["proxy"] == {"status": "HTTP/1.1 403 Forbidden", "body": _internal(host, 5432)}
    assert seen["relay"] == ""
    assert _relay_refused(host, 5432) in log.splitlines()


def test_the_default_bridge_gateway_is_refused(sources: Sources) -> None:
    """Docker's ``bridge`` gateway, where the Docker host answers, read from the engine."""
    gateway = sources.bridge_gateway
    seen, log = _run(
        sources,
        f"report(proxy=fetch({gateway!r}, 80), relay=relay(15001, size=1))\n",
        egress_pairs=((gateway, 80),),
        relay=(RelayRoute(15001, gateway, 80),),
    )

    assert seen["proxy"] == {"status": "HTTP/1.1 403 Forbidden", "body": _internal(gateway, 80)}
    assert seen["relay"] == ""
    assert _relay_refused(gateway, 80) in log.splitlines()


def test_a_configured_range_is_refused(sources: Sources) -> None:
    """`LAKANA_LAUNCHER__EGRESS_REFUSED` naming the source's network: refused; without it: reached.

    The proxy and the relay may reach that network only through the tests' ``reachable``; a
    refused range wins over it, as it would over any address.
    """
    # Nothing is sent to a refused route: the relay closes it unread, which could reset it.
    refusing = f"report(https=fetch({_HOST!r}, 8443), answer=relay(15001, size=1))\n"
    reaching = f"report(https=fetch({_HOST!r}, 8443), answer=relay(15001, {_SSL_REQUEST!r}))\n"
    pairs, routes = ((_HOST, 8443),), (RelayRoute(15001, _HOST, 5432),)

    with sources.stack.configured(egress_refused=(sources.subnet,)):
        refused, log = _run(sources, refusing, egress_pairs=pairs, relay=routes)
    reached, _ = _run(sources, reaching, egress_pairs=pairs, relay=routes)

    assert refused["https"] == {"status": "HTTP/1.1 403 Forbidden", "body": _internal(_HOST, 8443)}
    assert refused["answer"] == ""
    assert _relay_refused(_HOST, 5432) in log.splitlines()
    assert reached["https"]["body"] == "hello, source"
    assert reached["answer"] == "N"


def test_another_tasks_token_is_refused(sources: Sources) -> None:
    """Task B with task A's `LAKANA_EGRESS_PROXY`: the token is bound to A's network."""
    first = uuid.uuid4().hex[:20]
    attempt = sources.stack.attempt("import time\ntime.sleep(60)\n")
    thread, _ = _in_background(
        lambda: sources.stack.run(attempt, egress=((_HOST, 8443),), task=first, timeout=90)
    )
    try:
        env = sources.stack.container("task", first)["Config"]["Env"]
        proxy = next(
            item.split("=", 1)[1] for item in env if item.startswith("LAKANA_EGRESS_PROXY=")
        )
        seen, _ = _run(
            sources,
            f"report(borrowed=fetch({_HOST!r}, 8443, proxy={proxy!r}))\n",
            egress_pairs=((_HOST, 8443),),
        )
    finally:
        assert sources.stack.launcher is not None
        sources.stack.launcher.stop(StopRequest(first, "test"))
        thread.join(60)

    assert seen["borrowed"] == {
        "status": "HTTP/1.1 403 Forbidden",
        "body": "this token belongs to another client network",
    }


_OUTSIDER = r"""
import fcntl, json, socket, struct, sys
subnet, mask, via, address = sys.argv[1:5]
def sockaddr(ip):
    return struct.pack("=H2s4s8x", socket.AF_INET, b"\0\0", socket.inet_aton(ip))
# struct rtentry (LP64): add a route to the task's /29 through the relay's outward address.
entry = struct.pack(
    "=Q16s16s16sHh4xQQh6xQQQH6x",
    0, sockaddr(subnet), sockaddr(via), sockaddr(mask), 0x0003, 0, 0, 0, 0, 0, 0, 0, 0,
)
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
    fcntl.ioctl(control.fileno(), 0x890B, entry)  # SIOCADDRT
seen = {}
for name, target in (("listening", address), ("outward", via)):
    try:
        with socket.create_connection((target, 15001), timeout=5) as sock:
            seen[name] = "answered " + repr(sock.recv(1))
            seen["source"] = sock.getsockname()[0]
    except OSError as exc:
        seen[name] = type(exc).__name__
print(json.dumps(seen))
"""
"""A container on the outward network, as root with ``NET_ADMIN``, routes the task's /29
through the relay and calls the relay's listening address, then its outward address."""


def test_the_relay_refuses_a_client_outside_the_task_network(sources: Sources) -> None:
    """A client on the egress service's network gets no connection through the relay.

    The relay listens only on its address in the task's /29, so its outward address answers
    nothing. Its listening address, routed to explicitly, is not served either: on Docker
    Engine 28 the engine refuses such a packet before the relay sees it; should one get
    through, the relay closes it and says so in the run's log (its own check, which the unit
    tests prove).
    """
    assert sources.stack.launcher is not None
    launcher = sources.stack.launcher
    task = uuid.uuid4().hex[:20]
    attempt = sources.stack.attempt("import time\ntime.sleep(60)\n")
    thread, outcome = _in_background(
        lambda: sources.stack.run(
            attempt, relay=(RelayRoute(15001, _HOST, 9000),), task=task, timeout=90
        )
    )
    try:
        networks = sources.stack.container("relay", task)["NetworkSettings"]["Networks"]
        own = networks[container_name(launcher.settings, "task", task)]["IPAddress"]
        subnet = ipaddress.ip_network(f"{own}/29", strict=False)
        output = sources.stack.client.containers.run(
            sources.stack.image,
            [
                "python",
                "-I",
                "-c",
                _OUTSIDER,
                str(subnet.network_address),
                str(subnet.netmask),
                networks[sources.network]["IPAddress"],
                own,
            ],
            user="0:0",
            cap_add=["NET_ADMIN"],
            network=sources.network,
            labels=sources.stack.labels,
            remove=True,
        )
        seen = json.loads(output.decode().strip().splitlines()[-1])
        time.sleep(1)
    finally:
        launcher.stop(StopRequest(task, "test"))
        thread.join(60)

    assert outcome and not isinstance(outcome[0], Exception), outcome
    _, run_output = cast("tuple[dict[str, object], bytes]", outcome[0])
    log = run_output.decode()
    assert not seen["outward"].startswith("answered"), seen
    if seen["listening"].startswith("answered"):
        assert seen["listening"] == "answered b''", seen
        assert (
            f"lakana-relay: closed a connection from {seen['source']}, which is not on the "
            "task's network"
        ) in log.splitlines()
    else:
        assert seen["listening"] in {"ConnectionRefusedError", "TimeoutError"}, seen


@pytest.mark.parametrize("end", ["exit", "stop", "timeout"])
def test_teardown_leaves_no_relay_network_or_token(sources: Sources, end: str) -> None:
    """After an exit, a stop or a timeout: nothing labelled, no token expiry, the subnet resting.

    Parameters
    ----------
    sources
        The module's stack.
    end
        How the task ends.
    """
    assert sources.stack.launcher is not None
    launcher = sources.stack.launcher
    task = uuid.uuid4().hex[:20]
    seconds = 3 if end == "exit" else 60
    attempt = sources.stack.attempt(f"import time\ntime.sleep({seconds})\n")
    thread, outcome = _in_background(
        lambda: sources.stack.run(
            attempt,
            egress=((_HOST, 8443),),
            relay=(RelayRoute(15001, _HOST, 5432),),
            task=task,
            timeout=4 if end == "timeout" else 90,
        )
    )
    sources.stack.container("relay", task)
    sources.stack.container("task", task)
    network = container_name(launcher.settings, "task", task)
    with launcher._lock:
        subnet = launcher._subnets[network]
        assert network in launcher._token_expiry
    if end == "stop":
        assert launcher.stop(StopRequest(task, "test")) == {"stopped": True}
    thread.join(60)

    assert outcome and not isinstance(outcome[0], Exception), outcome
    result, _ = cast("tuple[dict[str, object], bytes]", outcome[0])
    assert (result["exit"] == 0) is (end == "exit")
    assert bool(result["timed_out"]) is (end == "timeout")
    assert bool(result["stopped"]) is (end == "stop")
    assert not [name for name in sources.stack.labelled() if task in name]
    with launcher._lock:
        assert network not in launcher._token_expiry
        assert network not in launcher._subnets
        assert subnet in launcher._resting


def test_a_relay_refusal_reaches_the_task_log(sources: Sources) -> None:
    """Review Focus 3 end to end: why the relay closed a connection is in the run's log."""
    seen, log = _run(
        sources,
        "report(relay=relay(15001, size=1))\n",
        relay=(RelayRoute(15001, "169.254.169.254", 80),),
    )

    assert seen["relay"] == ""
    assert _relay_refused("169.254.169.254", 80) in log.splitlines()
    assert log.index("lakana-relay: refused") < log.index('{"relay"')


def test_the_source_network_gateway_is_refused_once_the_launcher_lists_it(
    sources: Sources,
) -> None:
    """The source's network was made after the launcher started; its gateway is refused now.

    The proxy's own copy of the refused ranges holds it: the launcher wrote it again at a later
    network open, and the proxy read the new file.
    """
    network: Any = sources.stack.client.networks.get(sources.network)
    gateway = str(network.attrs["IPAM"]["Config"][0]["Gateway"])
    seen, _ = _run(
        sources, f"report(proxy=fetch({gateway!r}, 8443))\n", egress_pairs=((gateway, 8443),)
    )
    stream, _ = sources.egress.get_archive(f"{APP}/refused.json")
    with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
        member = archive.extractfile("refused.json")
        assert member is not None
        copied = json.loads(member.read())["refused"]

    assert seen["proxy"] == {"status": "HTTP/1.1 403 Forbidden", "body": _internal(gateway, 8443)}
    assert any(ipaddress.ip_address(gateway) in ipaddress.ip_network(r) for r in copied), copied


# --- the connection test (the launcher's check op) --------------------------------------------


@dataclass
class _Mirrored:
    """The worker's launcher client, with the worker's attempt directories mirrored into the volume.

    The worker's attempts directory is the launcher's work volume in production. Here the volume
    is Docker's and the attempts directory a host directory, so before a run or a check its
    attempt directory (code, task directories, trust bundle) is copied into the volume, then
    the request goes to the real launcher over its socket.

    Parameters
    ----------
    stack
        The stack whose work volume receives the copies.
    real
        The `LauncherClient` on the served launcher.
    root
        The host attempts directory.
    checks
        The id of every check sent, in order.
    """

    stack: Stack
    real: LauncherClient
    root: Path
    checks: list[str] = field(default_factory=list)

    def _mirror(self, attempt: str) -> None:
        """Copy one attempt directory into the work volume, owned by the task user.

        Parameters
        ----------
        attempt
            Its name.
        """
        top = self.root / attempt
        entries: list[Any] = [(attempt, "dir", b"", top.stat().st_mode & 0o777, 10001)]
        for path in sorted(top.rglob("*")):
            name = f"{attempt}/{path.relative_to(top)}"
            mode = path.stat().st_mode & 0o777
            if path.is_dir():
                entries.append((name, "dir", b"", mode, 10001))
            else:
                entries.append((name, "file", path.read_bytes(), mode, 10001))
        self.stack.put(self.stack.work_volume, entries)

    def call(
        self,
        request: dict[str, Any],
        on_log: Callable[[bytes], None] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, object]:
        """Mirror a run's attempt, then send the request.

        Parameters
        ----------
        request
            The request.
        on_log
            The log sink.
        timeout
            The deadline.

        Returns
        -------
        dict
            The launcher's result.
        """
        if request.get("op") == "run":
            self._mirror(str(request["attempt"]))
        return self.real.call(request, on_log, timeout=timeout)

    def stop(self, ident: str, owner: str) -> bool:
        """Stop a run.

        Parameters
        ----------
        ident
            Its id.
        owner
            The worker.

        Returns
        -------
        bool
            The launcher's answer.
        """
        return self.real.stop(ident, owner)

    def check(
        self, fields: Mapping[str, object], *, timeout: float = 90.0
    ) -> tuple[dict[str, object], bytes]:
        """Mirror a check's attempt, then send the check.

        Parameters
        ----------
        fields
            The request's fields.
        timeout
            The deadline.

        Returns
        -------
        tuple
            The launcher's result and the check's output.
        """
        self._mirror(str(fields["attempt"]))
        self.checks.append(str(fields["check"]))
        return self.real.check(fields, timeout=timeout)


@pytest.fixture
def worker_side(
    sources: Sources, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Mirrored]:
    """Serve the module's launcher on a socket and give the worker a client and attempts dir.

    Parameters
    ----------
    sources
        The module's stack.
    tmp_path
        The working directory (for the socket) and the attempts directory.
    monkeypatch
        Changes the working directory.

    Yields
    ------
    _Mirrored
        The worker's launcher client.
    """
    assert sources.stack.launcher is not None
    monkeypatch.chdir(tmp_path)
    server, _ = serve(sources.stack.launcher)
    attempts = tmp_path / "attempts"
    attempts.mkdir(mode=0o700)
    try:
        yield _Mirrored(sources.stack, LauncherClient(Path("l.sock")), attempts)
    finally:
        server.shutdown()
        server.server_close()


def _check(
    sources: Sources,
    worker: _Mirrored,
    kind: str,
    address: dict[str, object],
    settings: dict[str, object],
    *,
    trust: bool,
    egress_pairs: tuple[tuple[str, int], ...] | None = None,
    secret: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """Run the connection check of one connection through the worker's own Docker path.

    `ConnectionTestLoop._run_check` builds the attempt, the request and the outcome exactly as
    in production (the plan of `connection_plan`, the relay's verdict, masking); only the
    database row is skipped.

    Parameters
    ----------
    sources
        The module's stack.
    worker
        The worker's launcher client.
    kind
        The connection's kind.
    address
        Its address.
    settings
        Its settings.
    trust
        Whether the worker has the trust bundle with the source's authority.
    egress_pairs
        Replaces the planned proxy pairs, to send a token that does not name the host.
    secret
        The connection's secret, for the credential step.

    Returns
    -------
    tuple of str
        The outcome, the message and the check's id.
    """
    loop = connection_tests.ConnectionTestLoop(
        cast("Any", None),
        owner="w1",
        key_provider=cast("Any", None),
        runner_kind="docker",
        ingestion_roots={},
        docker=connection_tests.DockerCheck(
            client=worker,
            owner="test",
            work_dir=worker.root,
            trust=sources.bundle if trust else None,
        ),
    )
    connection_kind = ConnectionKind(kind)
    plan = connection_plan(
        [("src", connection_kind, address, settings)], isolated=True, root_dir=loop._root_dir
    )
    if egress_pairs is not None:
        plan = dataclasses.replace(plan, egress=egress_pairs)
    body = {
        "name": "src",
        "kind": kind,
        "address": address,
        "settings": settings,
        "endpoints": [
            {"host": e.host, "port": e.port, "role": e.role}
            for e in endpoints(connection_kind, address, settings)
        ],
        "secret": secret,
    }
    outcome, message = loop._run_check(body, plan)
    return outcome, message, (worker.checks[-1] if worker.checks else "")


_SOURCE_HTTPS = {"base_url": f"https://{_HOST}:8443"}
_SOURCE_DB = {"host": _HOST, "port": 5432, "database": "crm"}


@pytest.mark.parametrize(
    ("kind", "address", "settings", "trust", "pairs", "outcome", "words"),
    [
        ("http", _SOURCE_HTTPS, {}, True, None, "ok", "reachable"),
        ("http", _SOURCE_HTTPS, {}, False, None, "tls", f"not trusted for `{_HOST}`"),
        ("http", {"base_url": "https://nowhere.lakana.test:8443"}, {}, True, None, "dns",
         "cannot resolve nowhere.lakana.test"),
        ("http", {"base_url": f"https://{_HOST}:9999"}, {}, True, None, "refused",
         f"cannot reach {_HOST}:9999"),
        ("http", _SOURCE_HTTPS, {}, True, ((_HOST, 9000),), "not_allowed",
         "is not a destination this connection test may reach"),
        ("postgresql", _SOURCE_DB, {"user": "r", "tls": "disable"}, False, None, "ok",
         "TLS not used"),
        ("postgresql", _SOURCE_DB, {"user": "r"}, False, None, "tls", "does not offer TLS"),
        ("postgresql", {**_SOURCE_DB, "port": 5433}, {"user": "r", "tls": "disable"}, False, None,
         "refused", f"the relay refused {_HOST}:5433: cannot reach"),
        ("tcp", {"host": _HOST, "port": 9000}, {}, False, None, "ok", "reachable"),
        ("tcp", {"host": _HOST, "port": 7000}, {}, False, None, "timeout", f"{_HOST}:7000"),
        ("directory", {"root": "shared", "path": "in"}, {}, False, None, "root_unavailable",
         "`src` names ingestion root `shared`, which this worker does not have"),
    ],
    ids=[
        "ok-proxy", "tls-untrusted", "dns", "refused", "not-allowed", "ok-relay",
        "tls-not-offered", "refused-relay", "ok-tcp", "blackholed-relay", "root-unavailable",
    ],
)  # fmt: skip
def test_connection_test_outcomes(
    sources: Sources,
    worker_side: _Mirrored,
    kind: str,
    address: dict[str, object],
    settings: dict[str, object],
    trust: bool,
    pairs: tuple[tuple[str, int], ...] | None,
    outcome: str,
    words: str,
) -> None:
    """Each outcome on a real engine, the check in the task image under a task's rules.

    ``blackholed-relay``: port 7000 of the source accepts one connection it never takes, so the
    kernel drops every later SYN; the relay's dial times out after the check said ``ok`` to the
    relay, and the outcome is ``timeout``, never ``ok``. ``root-unavailable`` goes through the
    launcher's refusal of a root it does not have, as in production.

    Parameters
    ----------
    sources
        The module's stack.
    worker_side
        The worker's launcher client.
    kind
        The connection's kind.
    address
        Its address.
    settings
        Its settings.
    trust
        Whether the check trusts the source's authority.
    pairs
        Proxy pairs replacing the planned ones (a token that does not name the host).
    outcome
        The expected outcome.
    words
        Part of the expected message.
    """
    found, message, ident = _check(
        sources, worker_side, kind, address, settings, trust=trust, egress_pairs=pairs
    )

    assert (found, words in message) == (outcome, True), message
    if ident:
        assert not [name for name in sources.stack.labelled() if ident in name]


# --- an ingestion asset, end to end -------------------------------------------------------------

_E2E_KEY = "e" * 64
"""The key of the environment with the real `lakana` from the wheelhouse."""

_ASSET = f"""import json, socket, ssl

import httpx

from lakana import asset
from lakana.ingest import connections


@asset(connections=["api", "db"])
def items():
    api = connections.get("api")
    tls = ssl.create_default_context()
    with httpx.Client(proxy=api.proxy_url, verify=tls, timeout=20) as client:
        declared = client.get("https://{_HOST}:8443/").text
        try:
            client.get("https://example.lakana.test/")
            undeclared = "reached"
        except httpx.ProxyError as exc:
            undeclared = str(exc)
    db = connections.get("db")
    host, port = db.relay_address.split(":")
    with socket.create_connection((host, int(port)), timeout=20) as sock:
        sock.sendall({_SSL_REQUEST!r})
        answer = sock.recv(1).decode()
    print("E2E " + json.dumps(
        {{"declared": declared, "undeclared": undeclared, "database": answer,
          "relay": db.relay_address, "proxied": api.proxy_url is not None}}
    ), flush=True)
    raise RuntimeError("the probe is done")
"""
"""An ingestion asset as a team writes it: the SDK's `connections.get`, httpx through the proxy,
a database socket through the relay; it prints what it reached and fails on purpose."""


_FILES_ASSET = """import json

from lakana import asset
from lakana.ingest import _directory, files


@asset(connections=["exports"])
def items():
    ids = files.read("exports", "**/*.csv", since=None).column("id").to_pylist()
    try:
        _directory.read("/ingest/shared", "in", "link.csv", max_bytes=10_000)
        link_refused = False
    except _directory.DirectoryError:
        link_refused = True
    try:
        open("/ingest/shared/in/new.csv", "w").close()
        write_refused = False
    except OSError:
        write_refused = True
    print("E2E " + json.dumps(
        {"ids": ids, "link_refused": link_refused, "write_refused": write_refused}
    ), flush=True)
    raise RuntimeError("the probe is done")
"""
"""An ingestion asset that reads a mounted directory root with `files.read`; it prints what it
saw and fails on purpose."""


def _wheelhouse() -> Path:
    """Find the wheel directory the real `lakana` is installed from, or skip.

    Returns
    -------
    Path
        `LAKANA_TEST_WHEELHOUSE`.
    """
    value = os.environ.get("LAKANA_TEST_WHEELHOUSE")
    if not value:
        message = "set LAKANA_TEST_WHEELHOUSE (scripts/build_wheelhouse.sh DIR) for this case"
        if os.environ.get("LAKANA_TEST_DOCKER") == "1":
            pytest.fail(message)
        pytest.skip(message)
    return Path(value)


def _real_environment(stack: Stack, wheels: Path) -> None:
    """Build the environment `_E2E_KEY` with the wheelhouse's `lakana`, as a build would.

    Parameters
    ----------
    stack
        The stack.
    wheels
        The wheel directory.
    """
    assert stack.launcher is not None
    volume = environment_volume(stack.launcher.settings, PYTHON, _E2E_KEY)
    if (stack.root / "state" / "environments" / f"{volume}.complete").exists():
        return
    store = f"{stack.name}-wheels"
    stack.client.volumes.create(store, labels=stack.labels)
    stack.client.volumes.create(volume, labels=stack.labels)
    stack.put(
        store,
        [(path.name, "file", path.read_bytes(), 0o644, 10002) for path in wheels.glob("*.whl")],
    )
    stack.client.containers.run(
        stack.image,
        [
            "sh",
            "-c",
            "python -m venv /env && /env/bin/python -m pip install -q --no-index "
            "--find-links /wheels 'lakana[assets]'",
        ],
        user="10002:10002",
        environment={"HOME": "/tmp"},
        network_mode="none",
        volumes={volume: {"bind": "/env", "mode": "rw"}, store: {"bind": "/wheels", "mode": "ro"}},
        labels=stack.labels,
        remove=True,
    )
    (stack.root / "state" / "environments" / f"{volume}.complete").write_text(volume)


def _serve_connections(stack: Stack, bodies: dict[str, dict[str, object]]) -> None:
    """Put each connection's answer where the stand-in server serves the task route.

    The stack's server is ``python -m http.server`` in ``/``; the SDK's `connections.get` asks
    ``GET /api/v1/tasks/self/connections/<name>``.

    Parameters
    ----------
    stack
        The stack.
    bodies
        Name to the server's answer.
    """
    (server,) = stack.client.containers.list(
        filters={
            "label": [
                f"com.docker.compose.project={stack.name}",
                "com.docker.compose.service=server",
            ]
        }
    )
    route = "api/v1/tasks/self/connections"
    entries: list[Any] = []
    for part in ("api", "api/v1", "api/v1/tasks", "api/v1/tasks/self", route):
        entries.append((part, "dir", b"", 0o755, 0))
    for name, body in bodies.items():
        entries.append((f"{route}/{name}", "file", json.dumps(body), 0o644, 0))
    server.put_archive("/", docker_stack._tar(entries))


def test_an_ingestion_asset_reaches_its_declared_connections_and_nothing_else(
    sources: Sources, worker_side: _Mirrored
) -> None:
    """#308: a real asset, run by the worker's Docker runner with the worker's plan.

    The asset uses the SDK's `connections.get`: its HTTPS connection through the egress proxy
    (with the trust bundle), its database through the relay; an undeclared host is refused by
    the proxy. `DockerRunner` sends what `connection_plan` gives; the server is a stand-in that
    answers only the connection route, so the asset prints what it reached and fails on purpose.
    """
    _real_environment(sources.stack, _wheelhouse())
    declared = [
        ("api", ConnectionKind.HTTP, {"base_url": f"https://{_HOST}:8443"}, {}),
        ("db", ConnectionKind.POSTGRESQL, dict(_SOURCE_DB), {"user": "r", "tls": "disable"}),
    ]
    plan = connection_plan(declared, isolated=True, root_dir=lambda root, name: f"/ingest/{root}")
    _serve_connections(
        sources.stack,
        {
            name: {
                "name": name,
                "kind": kind.value,
                "address": address,
                "settings": settings,
                "endpoints": [
                    {"host": e.host, "port": e.port, "role": e.role}
                    for e in endpoints(kind, address, settings)
                ],
                "secret": None,
            }
            for name, kind, address, settings in declared
        },
    )
    attempt = worker_side.root / f"attempt-{uuid.uuid4().hex[:8]}"
    attempt.mkdir(mode=0o700)
    (attempt / "code").mkdir(mode=0o755)
    (attempt / "code" / "items.py").write_text(_ASSET)
    launcher = sources.stack.launcher
    assert launcher is not None
    runner = DockerRunner(
        cast("LauncherClient", worker_side),
        owner="test",
        memory_mb=512,
        cpus=1.0,
        pids=128,
        kill_grace=2.0,
        trust=sources.bundle,
    )
    task = PythonTask(
        code_dir=attempt / "code",
        asset_path="items.py",
        inputs={},
        table="raw.items",
        environment="dev",
        server_url=f"http://{launcher.settings.server_alias}:8000",
        token="t" * 48,
        timeout_seconds=120,
        venv=Path("/env"),
        expected_snapshot_id="none",
        attempt_dir=attempt,
        environment_key=_E2E_KEY,
        python=PYTHON,
        connections=plan.routing,
        egress=plan.egress,
        relay=plan.relay,
    )
    output = bytearray()

    outcome = runner.run(task, output.extend, StopSignal())

    text = output.decode(errors="replace")
    reports = [line for line in text.splitlines() if line.startswith("E2E ")]
    assert reports, text
    seen = json.loads(reports[-1].removeprefix("E2E "))
    assert seen["declared"] == "hello, source"
    assert seen["proxied"] is True
    assert "403" in seen["undeclared"], seen
    assert seen["database"] == "N"
    assert seen["relay"] == "lakana-relay:15001"
    assert outcome.succeeded is False
    assert not [name for name in sources.stack.labelled() if "task" in name]


def test_files_read_reads_a_mounted_root_and_never_follows_a_link(
    sources: Sources, worker_side: _Mirrored
) -> None:
    """A directory connection under the Docker runner: read-only mount, links skipped.

    The root is a host directory the launcher mounts at ``/ingest/shared``; below it a link to
    ``/etc/passwd`` and a link to the parent. `files.read` returns the two regular files, lists
    both links as skipped on stderr, the walker refuses to read the link, and the mount takes no
    write.
    """
    _real_environment(sources.stack, _wheelhouse())
    host_dir = worker_side.root / f"ingest-{uuid.uuid4().hex[:8]}"
    (host_dir / "in" / "sub").mkdir(parents=True, mode=0o755)
    for directory in (host_dir, host_dir / "in", host_dir / "in" / "sub"):
        directory.chmod(0o755)
    for relative, text in (("in/a.csv", "id\n1\n"), ("in/sub/b.csv", "id\n2\n")):
        (host_dir / relative).write_text(text)
        (host_dir / relative).chmod(0o644)
    (host_dir / "in" / "link.csv").symlink_to("/etc/passwd")
    (host_dir / "in" / "up").symlink_to("..")
    declared = [("exports", ConnectionKind.DIRECTORY, {"root": "shared", "path": "in"}, {})]
    plan = connection_plan(declared, isolated=True, root_dir=lambda root, name: f"/ingest/{root}")
    _serve_connections(
        sources.stack,
        {
            "exports": {
                "name": "exports",
                "kind": "directory",
                "address": {"root": "shared", "path": "in"},
                "settings": {},
                "endpoints": [],
                "secret": None,
            }
        },
    )
    attempt = worker_side.root / f"attempt-{uuid.uuid4().hex[:8]}"
    attempt.mkdir(mode=0o700)
    (attempt / "code").mkdir(mode=0o755)
    (attempt / "code" / "items.py").write_text(_FILES_ASSET)
    launcher = sources.stack.launcher
    assert launcher is not None
    runner = DockerRunner(
        cast("LauncherClient", worker_side),
        owner="test",
        memory_mb=512,
        cpus=1.0,
        pids=128,
        kill_grace=2.0,
        trust=sources.bundle,
    )
    task = PythonTask(
        code_dir=attempt / "code",
        asset_path="items.py",
        inputs={},
        table="raw.items",
        environment="dev",
        server_url=f"http://{launcher.settings.server_alias}:8000",
        token="t" * 48,
        timeout_seconds=120,
        venv=Path("/env"),
        expected_snapshot_id="none",
        attempt_dir=attempt,
        environment_key=_E2E_KEY,
        python=PYTHON,
        connections=plan.routing,
        egress=plan.egress,
        relay=plan.relay,
        roots=tuple(plan.root_users),
        root_users=plan.root_users,
    )
    output = bytearray()

    with sources.stack.configured(ingestion_roots={"shared": str(host_dir)}):
        outcome = runner.run(task, output.extend, StopSignal())

    text = output.decode(errors="replace")
    reports = [line for line in text.splitlines() if line.startswith("E2E ")]
    assert reports, text
    seen = json.loads(reports[-1].removeprefix("E2E "))
    assert sorted(seen["ids"]) == [1, 2]
    assert seen["link_refused"] is True
    assert seen["write_refused"] is True
    assert "skipped 'link.csv'" in text
    assert "skipped 'up'" in text
    assert "root:x:" not in text
    assert outcome.succeeded is False
    assert not [name for name in sources.stack.labelled() if "task" in name]


_DB_HOST = "db.lakana.test"
_POSTGRES = "postgres:18.6@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6"
_READER = secrets.token_urlsafe(16)
_POSTGRES_START = (
    "install -d -o 999 -g 999 -m 0700 /etc/pg-ssl && "
    "install -o 999 -g 999 -m 0600 /certs/server.crt /certs/server.key /etc/pg-ssl/ && "
    "exec docker-entrypoint.sh postgres -c ssl=on "
    "-c ssl_cert_file=/etc/pg-ssl/server.crt -c ssl_key_file=/etc/pg-ssl/server.key"
)
"""The key must belong to the server's user (uid 999) with mode 0600: copied before the start."""

_SQL_ASSET = """import json

from lakana import asset
from lakana.ingest import sql


@asset(connections=["crm"])
def items():
    try:
        seen = sql.read(
            "crm",
            "SELECT s.ssl, current_setting('transaction_read_only') AS read_only, :n AS n "
            "FROM pg_stat_ssl s WHERE s.pid = pg_backend_pid()",
            params={"n": 7},
        ).to_pylist()
    except Exception as exc:
        seen = {"error": f"{type(exc).__name__}: {exc}"}
    print("E2E " + json.dumps(seen), flush=True)
    raise RuntimeError("the probe is done")
"""
"""An ingestion asset that reads PostgreSQL with `sql.read`; it prints what it read and fails on
purpose (the stand-in server takes no table)."""


def _postgres_ready(container: Any) -> None:  # ruff: ignore[any-type] (Docker SDK)
    """Wait up to 60 seconds for PostgreSQL to accept TCP connections inside its container.

    Parameters
    ----------
    container
        The running container.

    Raises
    ------
    AssertionError
        When it does not.
    """
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        code, _ = container.exec_run(["pg_isready", "-h", "127.0.0.1", "-U", "postgres"])
        if code == 0:
            return
        time.sleep(0.5)
    raise AssertionError(container.logs().decode(errors="replace")[-2000:])


def test_sql_read_reaches_postgresql_through_the_relay_with_verify_full(
    sources: Sources, worker_side: _Mirrored
) -> None:
    """A real `sql.read` under the Docker runner: libpq dials the relay, TLS checks the name.

    PostgreSQL 18 on the sources' network serves a certificate for ``db.lakana.test`` from the
    module's authority, which the task trusts through the trust bundle only. The asset reads one
    row with a bound value; PostgreSQL reports the session's TLS and its read-only transaction.
    """
    _real_environment(sources.stack, _wheelhouse())
    client = sources.stack.client
    directory = sources.material / f"db-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    certificate, key = issue_leaf(sources.authority, directory, names=(_DB_HOST,))
    # `create` never pulls: a fresh runner has no copy of the image (by digest) yet.
    client.images.pull(_POSTGRES)
    database = client.containers.create(
        _POSTGRES,
        ["bash", "-c", _POSTGRES_START],
        entrypoint=[],
        environment={"POSTGRES_PASSWORD": secrets.token_urlsafe(16)},
        labels=sources.stack.labels,
        network=sources.network,
        networking_config={sources.network: client.api.create_endpoint_config(aliases=[_DB_HOST])},
    )
    try:
        database.put_archive(
            "/",
            docker_stack._tar(
                [
                    ("certs", "dir", b"", 0o755, 0),
                    ("certs/server.crt", "file", certificate.read_bytes(), 0o644, 0),
                    ("certs/server.key", "file", key.read_bytes(), 0o600, 0),
                ]
            ),
        )
        database.start()
        _postgres_ready(database)
        code, out = database.exec_run(
            [
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-c",
                f"CREATE ROLE reader LOGIN PASSWORD '{_READER}'",
            ]
        )
        assert code == 0, out
        address = {"host": _DB_HOST, "port": 5432, "database": "postgres"}
        settings = {"user": "reader", "tls": "verify-full"}
        declared = [("crm", ConnectionKind.POSTGRESQL, address, settings)]
        plan = connection_plan(
            declared, isolated=True, root_dir=lambda root, name: f"/ingest/{root}"
        )
        _serve_connections(
            sources.stack,
            {
                "crm": {
                    "name": "crm",
                    "kind": "postgresql",
                    "address": address,
                    "settings": settings,
                    "endpoints": [
                        {"host": e.host, "port": e.port, "role": e.role}
                        for e in endpoints(ConnectionKind.POSTGRESQL, address, settings)
                    ],
                    "secret": {"password": _READER},
                }
            },
        )
        attempt = worker_side.root / f"attempt-{uuid.uuid4().hex[:8]}"
        attempt.mkdir(mode=0o700)
        (attempt / "code").mkdir(mode=0o755)
        (attempt / "code" / "items.py").write_text(_SQL_ASSET)
        launcher = sources.stack.launcher
        assert launcher is not None
        runner = DockerRunner(
            cast("LauncherClient", worker_side),
            owner="test",
            memory_mb=512,
            cpus=1.0,
            pids=128,
            kill_grace=2.0,
            trust=sources.bundle,
        )
        task = PythonTask(
            code_dir=attempt / "code",
            asset_path="items.py",
            inputs={},
            table="raw.items",
            environment="dev",
            server_url=f"http://{launcher.settings.server_alias}:8000",
            token="t" * 48,
            timeout_seconds=120,
            venv=Path("/env"),
            expected_snapshot_id="none",
            attempt_dir=attempt,
            environment_key=_E2E_KEY,
            python=PYTHON,
            connections=plan.routing,
            egress=plan.egress,
            relay=plan.relay,
        )
        output = bytearray()
        # The source stays reachable for the other cases; the database is added for this one.
        reachable = (
            *launcher.settings.relay_reachable,
            f"{_address(database, sources.network)}/32",
        )

        with sources.stack.configured(relay_reachable=reachable):
            outcome = runner.run(task, output.extend, StopSignal())

        text = output.decode(errors="replace")
        reports = [line for line in text.splitlines() if line.startswith("E2E ")]
        assert reports, text
        seen = json.loads(reports[-1].removeprefix("E2E "))
        assert seen == [{"ssl": True, "read_only": "on", "n": 7}], text
        assert _READER not in text
        assert outcome.succeeded is False
        assert not [name for name in sources.stack.labelled() if "task" in name]
    finally:
        database.remove(force=True)


def test_connection_test_checks_credentials_on_the_check_environment(
    sources: Sources, worker_side: _Mirrored
) -> None:
    """The task image's check environment logs in to PostgreSQL and opens a SQLite file.

    PostgreSQL 18 serves TLS for ``db.lakana.test`` from the module's authority, through the
    relay with ``verify-full``: the right password is ``ok`` with "credentials accepted", a wrong
    one ``credentials``, and neither password reaches the message. A ``sqlite-file`` under a root
    the launcher mounts read-only opens as a SQLite database.
    """
    client = sources.stack.client
    directory = sources.material / f"db-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    certificate, key = issue_leaf(sources.authority, directory, names=(_DB_HOST,))
    client.images.pull(_POSTGRES)
    database = client.containers.create(
        _POSTGRES,
        ["bash", "-c", _POSTGRES_START],
        entrypoint=[],
        environment={"POSTGRES_PASSWORD": secrets.token_urlsafe(16)},
        labels=sources.stack.labels,
        network=sources.network,
        networking_config={sources.network: client.api.create_endpoint_config(aliases=[_DB_HOST])},
    )
    wrong = secrets.token_urlsafe(16)
    try:
        database.put_archive(
            "/",
            docker_stack._tar(
                [
                    ("certs", "dir", b"", 0o755, 0),
                    ("certs/server.crt", "file", certificate.read_bytes(), 0o644, 0),
                    ("certs/server.key", "file", key.read_bytes(), 0o600, 0),
                ]
            ),
        )
        database.start()
        _postgres_ready(database)
        code, out = database.exec_run(
            [
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "postgres",
                "-c",
                f"CREATE ROLE reader LOGIN PASSWORD '{_READER}'",
            ]
        )
        assert code == 0, out
        launcher = sources.stack.launcher
        assert launcher is not None
        reachable = (
            *launcher.settings.relay_reachable,
            f"{_address(database, sources.network)}/32",
        )
        address: dict[str, object] = {"host": _DB_HOST, "port": 5432, "database": "postgres"}
        settings: dict[str, object] = {"user": "reader", "tls": "verify-full"}

        with sources.stack.configured(relay_reachable=reachable):
            accepted = _check(
                sources,
                worker_side,
                "postgresql",
                address,
                settings,
                trust=True,
                secret={"password": _READER},
            )
            refused = _check(
                sources,
                worker_side,
                "postgresql",
                address,
                settings,
                trust=True,
                secret={"password": wrong},
            )
    finally:
        database.remove(force=True)

    assert accepted[:2] == (
        "ok",
        f"reachable: `{_DB_HOST}:5432`, TLS verified, credentials accepted",
    ), accepted
    assert refused[0] == "credentials", refused
    assert "PostgreSQL refused the login" in refused[1]
    assert _READER not in accepted[1] + refused[1] and wrong not in refused[1]

    host_dir = worker_side.root / f"ingest-{uuid.uuid4().hex[:8]}"
    host_dir.mkdir(mode=0o755)
    host_dir.chmod(0o755)
    with sqlite3.connect(host_dir / "app.db") as db:
        db.execute("CREATE TABLE t(a)")
    (host_dir / "app.db").chmod(0o644)

    with sources.stack.configured(ingestion_roots={"shared": str(host_dir)}):
        opened = _check(
            sources,
            worker_side,
            "sqlite-file",
            {"root": "shared", "path": "app.db"},
            {},
            trust=False,
        )

    assert opened[:2] == (
        "ok",
        "the file `app.db` exists under root `shared` and is readable; it opens read-only as a "
        "SQLite database",
    ), opened
    assert sorted(p.name for p in host_dir.iterdir()) == ["app.db"]
    assert not [name for name in sources.stack.labelled() if "check" in name]


def _python_base(version: str) -> str:
    """Read `Dockerfile.task`'s base image of one Python, by digest.

    Parameters
    ----------
    version
        Such as ``3.14``.

    Returns
    -------
    str
        ``python:<version>-slim@sha256:...``.
    """
    text = (docker_stack.REPO / "Dockerfile.task").read_text()
    found = re.search(
        rf"^FROM (python:{re.escape(version)}-slim@sha256:[0-9a-f]{{64}}) ", text, re.M
    )
    assert found is not None
    return found.group(1)


def test_a_task_image_without_the_check_environment_is_named(
    sources: Sources, worker_side: _Mirrored
) -> None:
    """An image of an earlier release: Docker cannot start the check's Python.

    The task image's own base, which has `/usr/local/bin/python` but no `/opt/lakana-check-env`,
    stands in for it. The launcher reports Docker's start failure; the worker says the image is
    older than the worker, not that the launcher is down.
    """
    client = sources.stack.client
    base = _python_base(PYTHON)
    client.images.pull(base)
    launcher = sources.stack.launcher
    assert launcher is not None

    with sources.stack.configured(images={**launcher.settings.images, PYTHON: base}):
        found = _check(sources, worker_side, "tcp", {"host": _HOST, "port": 9000}, {}, trust=False)

    assert found[:2] == ("failed", connection_tests.NO_CHECK_IN_IMAGE), found
    assert not [name for name in sources.stack.labelled() if found[2] in name]


def _gcs_key(kid: str, token_uri: str = "https://oauth2.googleapis.com/token") -> str:
    """Make a service-account key the source's token endpoint accepts by its key id.

    Parameters
    ----------
    kid
        ``right`` gets a token; anything else is refused as Google refuses a bad signature.
    token_uri
        The key's token endpoint.

    Returns
    -------
    str
        The key's JSON.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    return json.dumps(
        {
            "type": "service_account",
            "client_email": "reader@project.iam.gserviceaccount.com",
            "private_key_id": kid,
            "private_key": pem,
            "token_uri": token_uri,
        }
    )


@pytest.mark.parametrize(
    ("key", "pairs", "outcome", "words"),
    [
        ("right", None, "ok", "credentials accepted"),
        ("wrong", None, "credentials", "Google refused the key (invalid_grant: Invalid JWT"),
        ("elsewhere", None, "credentials", "is not the connection's token endpoint"),
        (
            "right",
            (("storage.googleapis.com", 443),),
            "not_allowed",
            "oauth2.googleapis.com:443",
        ),
    ],
    ids=["ok", "refused-key", "undeclared-token-uri", "proxy-refuses-token-host"],
)
def test_a_gcs_key_is_checked_through_the_egress_proxy(
    sources: Sources,
    worker_side: _Mirrored,
    key: str,
    pairs: tuple[tuple[str, int], ...] | None,
    outcome: str,
    words: str,
) -> None:
    """The signed token request and the listing tunnel through the real egress service.

    The source answers for ``oauth2.googleapis.com`` and ``storage.googleapis.com`` on the
    source network, so the connection's endpoints are a real gcs connection's. A key whose
    ``token_uri`` names another host is refused by the check before anything is sent; a proxy
    token that does not name the token host is refused by the proxy (``not_allowed``).

    Parameters
    ----------
    sources
        The module's stack.
    worker_side
        The worker's launcher client.
    key
        ``right``, ``wrong`` (the token endpoint refuses it) or ``elsewhere`` (its
        ``token_uri`` is the source's own host).
    pairs
        Proxy pairs replacing the planned ones.
    outcome
        The expected outcome.
    words
        Part of the expected message.
    """
    if key == "elsewhere":
        secret = _gcs_key("right", f"https://{_HOST}:8443/token")
    else:
        secret = _gcs_key(key)

    found, message, ident = _check(
        sources,
        worker_side,
        "gcs",
        {"bucket": "landing"},
        {"prefix": "in/"},
        trust=True,
        egress_pairs=pairs,
        secret={"service_account_json": secret},
    )

    assert (found, words in message) == (outcome, True), message
    assert "tok-right" not in message and "PRIVATE KEY" not in message
    assert not [name for name in sources.stack.labelled() if ident in name]


_API: dict[str, object] = {"base_url": f"https://{_HOST}/api"}
_API_OAUTH2 = {
    "auth": "oauth2",
    "client_id": "lakana",
    "token_url": "https://oauth2.googleapis.com/api-token",
}
"""The token host differs from the base URL's: `endpoints` merges a token URL on the base URL's
own host and port into the ``service`` endpoint, which the check does not accept as a token
endpoint."""


@pytest.mark.parametrize(
    ("settings", "secret", "pairs", "outcome", "words"),
    [
        ({"auth": "bearer"}, "api-right", None, "ok", "credentials accepted"),
        ({"auth": "bearer"}, "api-wrong", None, "credentials", "the API refused the credentials"),
        (_API_OAUTH2, "any-secret", None, "ok", "credentials accepted"),
        (_API_OAUTH2, "any-secret", ((_HOST, 443),), "not_allowed", "oauth2.googleapis.com:443"),
    ],
    ids=["bearer-right", "bearer-wrong", "oauth2-ok", "proxy-refuses-token-host"],
)
def test_an_http_secret_is_checked_through_the_egress_proxy(
    sources: Sources,
    worker_side: _Mirrored,
    settings: dict[str, object],
    secret: str,
    pairs: tuple[tuple[str, int], ...] | None,
    outcome: str,
    words: str,
) -> None:
    """The GET of the base URL, after OAuth2's token request, tunnels through the egress service.

    The source's 443 serves ``/api`` (200 for ``Bearer api-right``, else 401) and
    ``/api-token`` (``client_credentials`` of ``client_id=lakana``), both under the names
    its leaf carries (the base URL's host and Google's token host). A proxy token that does not
    name the token host is refused by the proxy (``not_allowed``).

    Parameters
    ----------
    sources
        The module's stack.
    worker_side
        The worker's launcher client.
    settings
        The connection's settings (its auth).
    secret
        The bearer token, or the OAuth2 client secret.
    pairs
        Proxy pairs replacing the planned ones.
    outcome
        The expected outcome.
    words
        Part of the expected message.
    """
    key = "client_secret" if settings["auth"] == "oauth2" else "token"
    found, message, ident = _check(
        sources,
        worker_side,
        "http",
        _API,
        settings,
        trust=True,
        egress_pairs=pairs,
        secret={key: secret},
    )

    assert (found, words in message) == (outcome, True), message
    assert "api-right" not in message and secret not in message
    assert not [name for name in sources.stack.labelled() if ident in name]
