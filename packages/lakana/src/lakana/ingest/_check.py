"""The connection test's check of reachability and TLS.

Run as ``python -I -m lakana.ingest._check``. It reads one JSON object from standard input,
``{"connection": <TaskConnectionOut-shaped>, "routing": {...}}``, and writes one line,
``{"outcome": ..., "message": ...}``, with exit status 0 whatever it found. The routing is what
a task's description says about the connection (`relay_port`, `root_dir`, `path`).

This step checks reachability only: the name resolves, the port answers, the TLS handshake
verifies as the connection is configured, a mounted root and path are there. The credential step
of each kind arrives with its helper; until then a kind with a secret ends its message with
"; credentials not checked yet". Every secret value is replaced by ``***`` in the message.

The module uses the standard library only: it runs in an environment that holds nothing else.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import json
import os
import socket
import ssl
import stat
import struct
import sys
from collections.abc import Mapping
from typing import Any, NoReturn
from urllib.parse import urlsplit

STEP_SECONDS = 10.0
"""The time one step (resolve and connect, or one handshake) may take."""

MAX_MESSAGE = 1000
"""The longest message, as `ConnectionTest.message` holds."""

PROXY_KINDS = frozenset({"http", "s3", "gcs", "azure"})
"""Kinds reached through the egress proxy when `LAKANA_EGRESS_PROXY` is set."""

RELAY_HOST = "lakana-relay"
"""The name a task resolves to its relay."""

NOT_YET = "; credentials not checked yet"
"""What a kind with a secret adds until its credential step exists."""

TCP_OK = "reachable; Lakana does not check the protocol, TLS or credentials of a tcp connection"

MISSING_ROOT = (
    "`{connection}` names ingestion root `{root}`, which this worker does not have. Set it in "
    "`LAKANA_LAUNCHER__INGESTION_ROOTS` (Docker runner) or `LAKANA_RUN__INGESTION_ROOTS` "
    "(local runner)."
)
"""The same text as the worker's `MISSING_ROOT`; this module cannot import it."""

_SSL_REQUEST = struct.pack("!ii", 8, 80877103)
_PROXY_HEAD_LIMIT = 16384


class _Found(Exception):
    """A result reached before the end of a check.

    Parameters
    ----------
    outcome
        One of the outcome names.
    message
        What was found.
    """

    def __init__(self, outcome: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.message = message


def _tls_text(host: str) -> str:
    """Write the first sentence of the TLS message.

    Parameters
    ----------
    host
        The name the certificate was checked for.

    Returns
    -------
    str
        The sentence.
    """
    return f"the server's certificate is not trusted for `{host}`"


def _network_failure(exc: OSError, host: str, port: int, timeout: float) -> _Found:
    """Turn a failed connect, read or handshake into an outcome.

    Parameters
    ----------
    exc
        What was raised.
    host
        The host that was dialed.
    port
        Its port.
    timeout
        The seconds the step had.

    Returns
    -------
    _Found
        The outcome and a message that names the host and port.
    """
    where = f"`{host}:{port}`"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return _Found("tls", _tls_text(host))
    if isinstance(exc, socket.gaierror):
        return _Found("dns", f"cannot resolve `{host}` ({exc.strerror or 'lookup failed'})")
    if isinstance(exc, ConnectionRefusedError):
        return _Found("refused", f"{where} refused the connection")
    if isinstance(exc, TimeoutError) or "timed out" in str(exc):
        return _Found("timeout", f"{where} did not answer within {timeout:g} s")
    if isinstance(exc, ssl.SSLError):
        return _Found("failed", f"the TLS handshake with {where} failed ({exc.reason or 'error'})")
    return _Found("failed", f"could not reach {where} ({exc.strerror or type(exc).__name__})")


def _raise_network(exc: OSError, host: str, port: int, timeout: float) -> NoReturn:
    """Raise the outcome of a failed connect, read or handshake.

    Parameters
    ----------
    exc
        What was raised.
    host
        The host that was dialed.
    port
        Its port.
    timeout
        The seconds the step had.

    Raises
    ------
    _Found
        Always: `_network_failure`'s outcome.
    """
    found = _network_failure(exc, host, port, timeout)
    raise found from None


def _proxy_connect(proxy: str, host: str, port: int, timeout: float) -> socket.socket:
    """Open a tunnel to ``host:port`` through the egress proxy.

    Parameters
    ----------
    proxy
        ``LAKANA_EGRESS_PROXY``: ``http://lakana:<token>@host:port``.
    host
        The target host.
    port
        The target port.
    timeout
        Seconds per step.

    Returns
    -------
    socket.socket
        The tunnel, after the proxy's ``200``.

    Raises
    ------
    _Found
        With the proxy's own one-line reason: ``not_allowed`` for 403, ``dns`` or ``refused``
        for 502, ``failed`` otherwise.
    """
    parts = urlsplit(proxy)
    token = parts.password or ""
    sock = socket.create_connection((parts.hostname or "", parts.port or 80), timeout)
    try:
        auth = base64.b64encode(f"lakana:{token}".encode()).decode()
        target = f"{host}:{port}"
        sock.sendall(
            f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}"
            "\r\n\r\n".encode("ascii", "replace")
        )
        buffer = b""
        while b"\r\n\r\n" not in buffer and len(buffer) < _PROXY_HEAD_LIMIT:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
        head, _, body = buffer.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        try:
            status = int(lines[0].split(" ", 2)[1])
        except (IndexError, ValueError):
            raise _Found("failed", "the egress proxy did not answer like an HTTP proxy") from None
        if status == 200:
            return sock
        length = 0
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "content-length" and value.strip().isdigit():
                length = min(int(value.strip()), MAX_MESSAGE)
        while len(body) < length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
        reason = body.decode("utf-8", "replace").strip().splitlines()
        text = reason[0] if reason else lines[0]
    except BaseException:
        sock.close()
        raise
    sock.close()
    if status == 403:
        raise _Found("not_allowed", text)
    if status == 502:
        raise _Found("dns" if "cannot resolve" in text else "refused", text)
    raise _Found("failed", text)


def _open(host: str, port: int, routing: Mapping[str, Any], timeout: float) -> socket.socket:
    """Connect the way a task of this connection would.

    Parameters
    ----------
    host
        The endpoint's host.
    port
        Its port.
    routing
        The task description's entry: a ``relay_port`` sends the connection through
        ``lakana-relay``.
    timeout
        Seconds per step.

    Returns
    -------
    socket.socket
        A connected socket. Through ``CONNECT host:port`` on `LAKANA_EGRESS_PROXY` (with
        ``Proxy-Authorization: Basic base64("lakana:<token>")``) for the kinds that use the
        proxy when the variable is set; directly otherwise.
    """
    relay_port = routing.get("relay_port")
    proxy = os.environ.get("LAKANA_EGRESS_PROXY")
    through_proxy = bool(proxy) and routing.get("_kind") in PROXY_KINDS
    if isinstance(relay_port, int):
        dialed = (RELAY_HOST, relay_port)
    elif through_proxy:
        parts = urlsplit(str(proxy))
        dialed = (parts.hostname or "", parts.port or 80)
    else:
        dialed = (host, port)
    try:
        if through_proxy and not isinstance(relay_port, int):
            return _proxy_connect(str(proxy), host, port, timeout)
        return socket.create_connection(dialed, timeout)
    except OSError as exc:
        _raise_network(exc, dialed[0], dialed[1], timeout)


def _tls(sock: socket.socket, host: str, ca_pem: str | None, mode: str) -> socket.socket:
    """Run the TLS handshake as the connection is configured.

    Parameters
    ----------
    sock
        The connected socket.
    host
        The name to check the certificate for, and to send as the server name.
    ca_pem
        The connection's own CA certificates (PEM), trusted in addition to the bundle.
    mode
        ``verify-full`` checks the chain and the name; ``require`` encrypts without checking
        (libpq's ``require``); ``disable`` does nothing.

    Returns
    -------
    socket.socket
        The TLS socket, or `sock` for ``disable``.
    """
    if mode == "disable":
        return sock
    context = ssl.create_default_context(cafile=os.environ.get("LAKANA_CA_BUNDLE"))
    if ca_pem:
        context.load_verify_locations(cadata=ca_pem)
    if mode == "require":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context.wrap_socket(sock, server_hostname=host)


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    """Read up to `count` bytes, stopping early when the peer closes.

    Parameters
    ----------
    sock
        The socket.
    count
        The bytes wanted.

    Returns
    -------
    bytes
        What arrived.
    """
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _https_endpoint(
    host: str, port: int, ca_pem: str | None, routing: Mapping[str, Any], t: float
) -> None:
    """Connect to one HTTPS endpoint and verify its certificate.

    Parameters
    ----------
    host
        The host.
    port
        The port.
    ca_pem
        The connection's own CA certificates, or `None`.
    routing
        The routing entry.
    t
        Seconds per step.
    """
    try:
        sock = _open(host, port, routing, t)
        try:
            _tls(sock, host, ca_pem, "verify-full").close()
        finally:
            sock.close()
    except OSError as exc:
        _raise_network(exc, host, port, t)


def _check_https(body: Mapping[str, Any], routing: Mapping[str, Any], t: float) -> tuple[str, str]:
    """Check an ``http``, ``s3``, ``gcs`` or ``azure`` connection.

    Parameters
    ----------
    body
        The connection.
    routing
        The routing entry.
    t
        Seconds per step.

    Returns
    -------
    tuple of str
        The outcome and message.
    """
    kind = body["kind"]
    ca_pem = body["settings"].get("ca_certificate")
    if kind == "http":
        parts = urlsplit(str(body["address"]["base_url"]))
        targets = [(parts.hostname or "", parts.port or 443)]
    else:
        targets = [(str(e["host"]), int(e["port"])) for e in body["endpoints"]]
    for host, port in targets:
        _https_endpoint(host, port, ca_pem, routing, t)
    shown = ", ".join(f"`{h}:{p}`" for h, p in targets)
    return "ok", f"reachable: {shown}, TLS verified"


def _check_postgresql(
    body: Mapping[str, Any], routing: Mapping[str, Any], t: float
) -> tuple[str, str]:
    """Check a PostgreSQL connection: connect, ask for TLS, verify it.

    Parameters
    ----------
    body
        The connection.
    routing
        The routing entry.
    t
        Seconds per step.

    Returns
    -------
    tuple of str
        The outcome and message.

    Raises
    ------
    _Found
        When the server does not offer TLS or does not speak PostgreSQL.
    """
    host, port = str(body["address"]["host"]), int(body["address"]["port"])
    settings = body["settings"]
    mode = settings.get("tls", "verify-full")
    try:
        sock = _open(host, port, routing, t)
        try:
            if mode == "disable":
                return "ok", f"reachable: `{host}:{port}`; TLS not used (tls: disable)"
            sock.sendall(_SSL_REQUEST)
            answer = sock.recv(1)
            if answer == b"N":
                raise _Found(
                    "tls",
                    f"the server at `{host}:{port}` does not offer TLS; enable TLS on the server "
                    "or set the connection's `tls` to `disable`",
                )
            if answer != b"S":
                raise _Found(
                    "failed",
                    f"the server at `{host}:{port}` did not answer the TLS request like a "
                    "PostgreSQL server; check the host and port",
                )
            _tls(sock, host, settings.get("ca_certificate"), mode).close()
        finally:
            sock.close()
    except OSError as exc:
        _raise_network(exc, host, port, t)
    if mode == "require":
        return "ok", f"reachable: `{host}:{port}`, TLS in use, certificate not checked"
    return "ok", f"reachable: `{host}:{port}`, TLS verified"


def _check_mariadb(
    body: Mapping[str, Any], routing: Mapping[str, Any], t: float
) -> tuple[str, str]:
    """Check a MariaDB connection by reading the server's greeting.

    Parameters
    ----------
    body
        The connection.
    routing
        The routing entry.
    t
        Seconds per step.

    Returns
    -------
    tuple of str
        The outcome and message.

    Raises
    ------
    _Found
        When what the server sends first is not a MariaDB or MySQL greeting.
    """
    host, port = str(body["address"]["host"]), int(body["address"]["port"])
    try:
        sock = _open(host, port, routing, t)
        try:
            head = _recv_exactly(sock, 5)
        finally:
            sock.close()
    except OSError as exc:
        _raise_network(exc, host, port, t)
    if len(head) == 5 and head[4] == 0x0A:
        return (
            "ok",
            f"reachable: `{host}:{port}` greeted like MariaDB or MySQL (TLS not checked yet)",
        )
    raise _Found(
        "failed",
        f"the server at `{host}:{port}` did not greet like MariaDB or MySQL; check the host and "
        "port",
    )


def _check_tcp(body: Mapping[str, Any], routing: Mapping[str, Any], t: float) -> tuple[str, str]:
    """Check a ``tcp`` connection: connect and send nothing.

    Parameters
    ----------
    body
        The connection.
    routing
        The routing entry.
    t
        Seconds per step.

    Returns
    -------
    tuple of str
        The outcome and `TCP_OK`.
    """
    host, port = str(body["address"]["host"]), int(body["address"]["port"])
    try:
        _open(host, port, routing, t).close()
    except OSError as exc:
        _raise_network(exc, host, port, t)
    return "ok", TCP_OK


def _component_failure(directory: int, name: str, exc: OSError, root: str, path: str) -> _Found:
    """Explain why a path component could not be opened.

    Parameters
    ----------
    directory
        The open directory the component is looked up in.
    name
        The component.
    exc
        What the open raised.
    root
        The root's name.
    path
        The whole path, for the message.

    Returns
    -------
    _Found
        ``not_found`` for a missing entry; ``failed`` otherwise.
    """
    if isinstance(exc, FileNotFoundError):
        return _Found("not_found", f"the path `{path}` does not exist under root `{root}`")
    with contextlib.suppress(OSError):
        if stat.S_ISLNK(os.lstat(name, dir_fd=directory).st_mode):
            return _Found("failed", f"`{name}` is a symbolic link, which Lakana does not follow")
    if isinstance(exc, PermissionError):
        return _Found("failed", f"this worker may not read `{name}` ({exc.strerror})")
    if isinstance(exc, NotADirectoryError) or exc.errno in (errno.ENOTDIR, errno.ELOOP):
        return _Found("failed", f"`{name}` is not a directory")
    return _Found("failed", f"`{name}` could not be opened ({exc.strerror})")


def _check_mount(
    body: Mapping[str, Any], routing: Mapping[str, Any], file: bool
) -> tuple[str, str]:
    """Check a ``directory`` or ``sqlite-file`` connection under a mounted root.

    The root is opened with ``O_DIRECTORY | O_NOFOLLOW`` and each component of the path through
    the directory before it with ``O_NOFOLLOW``, so no symbolic link is followed.

    Parameters
    ----------
    body
        The connection.
    routing
        The routing entry (``root``, ``root_dir`` and ``path``).
    file
        Whether the path must be a regular file (``sqlite-file``) rather than a listable
        directory.

    Returns
    -------
    tuple of str
        The outcome and message.

    Raises
    ------
    _Found
        When the root is missing, a component is missing or a link, or the last one is not
        what the kind needs.
    """
    root = str(routing.get("root", body["address"].get("root", "")))
    path = str(routing.get("path", body["address"].get("path", "")))
    root_dir = routing.get("root_dir")
    unavailable = _Found(
        "root_unavailable", MISSING_ROOT.format(connection=body["name"], root=root)
    )
    if not isinstance(root_dir, str):
        raise unavailable
    try:
        opened = os.open(root_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (FileNotFoundError, NotADirectoryError):
        with contextlib.suppress(OSError):
            if stat.S_ISLNK(os.lstat(root_dir).st_mode):
                raise _Found(
                    "failed", f"the root `{root}` is a symbolic link, which Lakana does not follow"
                ) from None
        raise unavailable from None
    except OSError as exc:
        raise _Found(
            "failed",
            f"the root `{root}` could not be opened ({exc.strerror}); it must be a "
            "directory, not a symbolic link",
        ) from None
    held = [opened]
    try:
        names = [part for part in path.split("/") if part]
        for index, name in enumerate(names):
            last = index == len(names) - 1
            if name in (".", ".."):
                raise _Found("failed", f"`{name}` is not allowed in a path")
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if file and last:
                flags |= os.O_NONBLOCK
            else:
                flags |= os.O_DIRECTORY
            try:
                held.append(os.open(name, flags, dir_fd=held[-1]))
            except OSError as exc:
                failure = _component_failure(held[-1], name, exc, root, path)
                raise failure from None
        if file:
            if not stat.S_ISREG(os.fstat(held[-1]).st_mode) or not names:
                raise _Found("failed", f"`{path}` is not a regular file")
            return "ok", f"the file `{path}` exists under root `{root}` and is readable"
        try:
            count = len(os.listdir(held[-1]))
        except OSError as exc:
            raise _Found("failed", f"`{path}` cannot be listed ({exc.strerror})") from None
        entries = "1 entry" if count == 1 else f"{count} entries"
        return "ok", f"the path `{path}` is readable under root `{root}` ({entries})"
    finally:
        for descriptor in held:
            os.close(descriptor)


def _mask(text: str, secret: Mapping[str, Any] | None) -> str:
    """Replace every secret value with ``***``.

    Parameters
    ----------
    text
        The message.
    secret
        The connection's secret, or `None`.

    Returns
    -------
    str
        The text without any of the values.
    """
    for value in sorted(
        (v for v in (secret or {}).values() if isinstance(v, str) and v), key=len, reverse=True
    ):
        text = text.replace(value, "***")
    return text


def check(
    connection: Mapping[str, Any], routing: Mapping[str, Any], *, timeout: float = STEP_SECONDS
) -> tuple[str, str]:
    """Check one connection and say what was found.

    Parameters
    ----------
    connection
        A ``TaskConnectionOut``-shaped body: ``name``, ``kind``, ``address``, ``settings``,
        ``endpoints`` and ``secret``.
    routing
        What a task's description says about it.
    timeout
        Seconds per step.

    Returns
    -------
    tuple of str
        The outcome (``ok``, ``dns``, ``refused``, ``timeout``, ``tls``, ``not_allowed``,
        ``root_unavailable``, ``not_found`` or ``failed``) and a message of at most 1,000
        characters that holds no secret value.
    """
    secret = connection.get("secret")
    try:
        kind = connection["kind"]
        route = {**routing, "_kind": kind}
        if kind in PROXY_KINDS:
            outcome, message = _check_https(connection, route, timeout)
        elif kind == "postgresql":
            outcome, message = _check_postgresql(connection, route, timeout)
        elif kind == "mariadb":
            outcome, message = _check_mariadb(connection, route, timeout)
        elif kind == "tcp":
            outcome, message = _check_tcp(connection, route, timeout)
        elif kind in ("directory", "sqlite-file"):
            outcome, message = _check_mount(connection, route, kind == "sqlite-file")
        else:
            outcome, message = (
                "failed",
                f"this check does not know the kind `{kind}`; upgrade lakana",
            )
        if outcome == "ok" and secret and kind != "tcp":
            message += NOT_YET
    except _Found as found:
        outcome, message = found.outcome, found.message
    except Exception as exc:
        outcome, message = "failed", f"the check failed unexpectedly ({type(exc).__name__})"
    return outcome, _mask(message, secret)[:MAX_MESSAGE]


def main() -> None:
    """Read the request from standard input and write the result as one JSON line."""
    try:
        request: Any = json.load(sys.stdin)
        outcome, message = check(request["connection"], request.get("routing") or {})
    except Exception:
        outcome, message = "failed", "the check could not read its input; upgrade lakana"
    sys.stdout.write(json.dumps({"outcome": outcome, "message": message}) + "\n")


if __name__ == "__main__":
    main()
