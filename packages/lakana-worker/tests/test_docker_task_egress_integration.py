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

import hashlib
import io
import ipaddress
import json
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, cast

import pytest
from docker_stack import APP, Stack, build_task_image, docker_client, stack, start_egress
from tls_fixture import issue_leaf, make_authority

from lakana_worker.launcher import egress
from lakana_worker.launcher.requests import RelayRoute, StopRequest, container_name
from lakana_worker.trust import build_trust_bundle

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.slow]
"""Each case starts containers and takes seconds. The slow-test ratchet reads only the unit
shards' reports, which never hold an integration test, so the marker does not count there."""

_HOST = "target.lakana.test"
_HINT = "give the connection an address outside the Lakana stack (lakana connection update)"
_SSL_REQUEST = b"\x00\x00\x00\x08\x04\xd2\x16\x2f"
"""The first message of a PostgreSQL client that asks for TLS; a server without it says ``N``."""

_SOURCE = f"""import socket, ssl, threading
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
def serve(port, handler):
    server = socket.create_server(("0.0.0.0", port))
    while True:
        connection, _ = server.accept()
        threading.Thread(target=handler, args=(connection,), daemon=True).start()
threading.Thread(target=serve, args=(5432, postgres), daemon=True).start()
threading.Thread(target=serve, args=(9000, echo), daemon=True).start()
serve(8443, https)
"""
"""The source: TLS on 8443, the first PostgreSQL answer on 5432, an echo on 9000."""

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
    """

    stack: Stack
    network: str
    subnet: str
    bundle: bytes
    bridge_gateway: str
    neighbour: str
    egress: Any


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
    certificate, key = issue_leaf(authority, material, names=(_HOST,))
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
                    network.name: client.api.create_endpoint_config(aliases=[_HOST])
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
