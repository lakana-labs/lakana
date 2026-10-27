"""The connection test's check: reachability, TLS and, where Lakana can, the credentials.

Run as ``python -I -m lakana.ingest._check`` (the local runner: the worker's own Python), or by
path as ``/opt/lakana-check-env/bin/python -I -B /opt/lakana-check/check.py`` in the task image
(the Docker runner). It reads one JSON object from standard input, ``{"connection":
<TaskConnectionOut-shaped>, "routing": {...}}``, and writes one line, ``{"outcome": ...,
"message": ...}``, with exit status 0 whatever it found. The routing is what a task's description
says about the connection (`relay_port`, `root_dir`, `path`).

First reachability: the name resolves, the port answers, the TLS handshake verifies as the
connection is configured, a mounted root and path are there. Then the credentials: a PostgreSQL
login with ``SELECT 1`` executed in a read-only transaction (not read: fetching a row needs
pyarrow, which the check environment leaves out), a MariaDB login over the configured TLS with
``SELECT 1`` in a read-only transaction, a SQLite file opened read-only, a listing of at most one
object for ``s3``, ``azure`` and ``gcs`` (a gcs key signs a token request to the connection's own
token endpoint, never another host). ``http`` credentials arrive with their helper; until then an
``http`` connection with a secret ends its message with "; credentials not checked yet". Every
secret value is replaced by ``***`` in the message, and every control character of a server's
words is escaped.

The module is one file that imports nothing of `lakana`, so the task image can run it alone:
its module-level imports are the standard library's, and each credential step imports its
driver inside the step, from `DRIVERS` only
(`test_the_check_imports_only_the_standard_library_and_its_drivers`); its HTTP requests, the gcs
token request included, go over its own sockets (`_exchange`). The task image installs exactly
`DRIVERS` in ``/opt/lakana-check-env``; where one does not import
(the local runner without ``lakana[assets]``), the test fails saying so, rather than passing
without the check.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import stat
import struct
import sys
import tempfile
import time
from collections.abc import Generator, Mapping
from email.utils import formatdate
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote, unquote, urlsplit

STEP_SECONDS = 10.0
"""The time one step (resolve and connect, or one handshake) may take."""

MAX_MESSAGE = 1000
"""The longest message, as `ConnectionTest.message` holds."""

PROXY_KINDS = frozenset({"http", "s3", "gcs", "azure"})
"""Kinds reached through the egress proxy when `LAKANA_EGRESS_PROXY` is set."""

RELAY_HOST = "lakana-relay"
"""The name a task resolves to its relay."""

DRIVERS = {
    "adbc_driver_postgresql": "adbc-driver-postgresql",
    "adbc_driver_sqlite": "adbc-driver-sqlite",
    "cryptography": "cryptography",
    "pymysql": "PyMySQL",
}
"""Each package a credential step may import (inside the step), to the distribution that brings
it: the root ``check`` dependency group of uv.lock and the task image's import smoke are exactly
these (tests compare all three). The ``http`` credential step adds ``httpx`` with its code."""

NOT_YET = "; credentials not checked yet"
"""What an ``http`` connection with a secret adds until its credential step exists."""

UNAVAILABLE = (
    "the credentials were not checked: this Python cannot import `{module}`; install {package} "
    "next to the worker (local runner), or use the task image of this Lakana version (Docker "
    "runner)"
)
"""The ``failed`` message of a credential step whose driver is missing."""

GCS_URL = "https://storage.googleapis.com/storage/v1/b/{bucket}/o"
"""The objects a ``gcs`` credential step lists; only tests point it at another server."""
GCS_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
"""The scope a ``gcs`` credential step asks a token for."""
_JWT_SECONDS = 600
"""How long the signed token request of a ``gcs`` key is valid."""
GCS_NEXT_STEP = "store the key file Google gave you"
"""The next step of a ``gcs`` secret that is no usable service-account key."""
MARIADB_GREETING_ONLY = (
    "; TLS and credentials are checked only with a password: store one with "
    "`lakana connection update`"
)
"""What a ``mariadb`` test without a secret adds: the greeting alone proves neither."""

TCP_OK = "reachable; Lakana does not check the protocol, TLS or credentials of a tcp connection"

MISSING_ROOT = (
    "`{connection}` names ingestion root `{root}`, which this worker does not have. Set it in "
    "`LAKANA_LAUNCHER__INGESTION_ROOTS` (Docker runner) or `LAKANA_RUN__INGESTION_ROOTS` "
    "(local runner)."
)
"""The same text as the worker's `MISSING_ROOT`; this module cannot import it."""

_OLD_TLS = frozenset(
    {"UNSUPPORTED_PROTOCOL", "TLSV1_ALERT_PROTOCOL_VERSION", "NO_PROTOCOLS_AVAILABLE"}
)
"""How OpenSSL says that the peer offers only a protocol older than the floor."""
_SSL_REQUEST = struct.pack("!ii", 8, 80877103)
_PROXY_HEAD_LIMIT = 16384
AZURE_URL = "https://{account}.blob.core.windows.net/{container}"
"""The container an ``azure`` credential step lists; only tests point it at an emulator."""
_AZURE_VERSION = "2021-08-06"
_ANSWER_LIMIT = 65536
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


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
    if isinstance(exc, ssl.SSLError) and (exc.reason or "") in _OLD_TLS:
        return _Found(
            "tls",
            f"the server at {where} offers only TLS older than 1.2, which Lakana refuses; "
            "upgrade the server's TLS to 1.2 or newer",
        )
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


def trust_context(ca_pem: str | None, *, verify: bool = True) -> ssl.SSLContext:
    """Build a client TLS context: TLS 1.2 or newer, the trust bundle plus the connection's CA.

    The one TLS policy of the check and of the helpers of `lakana.ingest` (``files.read`` over
    HTTPS uses it too), so a connection the test passes is trusted the same way by a task.

    Parameters
    ----------
    ca_pem
        The connection's own CA certificates (PEM), trusted in addition to the bundle.
    verify
        `False` encrypts without checking the chain or the name (libpq's ``require``).

    Returns
    -------
    ssl.SSLContext
        The context; it trusts `LAKANA_CA_BUNDLE` when set, else the system's roots.

    Raises
    ------
    OSError
        When the file `LAKANA_CA_BUNDLE` names cannot be read (`ssl.SSLError`, an `OSError`
        too, when it or `ca_pem` holds no certificate).
    """
    context = ssl.create_default_context(cafile=os.environ.get("LAKANA_CA_BUNDLE"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if ca_pem:
        context.load_verify_locations(cadata=ca_pem)
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _context(ca_pem: str | None, mode: str) -> ssl.SSLContext:
    """Build the TLS context of a check: `trust_context`, its unreadable bundle as an outcome.

    Parameters
    ----------
    ca_pem
        The connection's own CA certificates (PEM), trusted in addition to the bundle.
    mode
        ``verify-full`` or ``require``.

    Returns
    -------
    ssl.SSLContext
        The context.

    Raises
    ------
    _Found
        ``failed`` when the file `LAKANA_CA_BUNDLE` names cannot be read or holds no certificate,
        or the connection's CA holds none.
    """
    try:
        return trust_context(ca_pem, verify=mode != "require")
    except (FileNotFoundError, PermissionError):
        raise _Found(
            "failed",
            f"the trust bundle `{os.environ.get('LAKANA_CA_BUNDLE')}` (LAKANA_CA_BUNDLE) cannot "
            "be read; check that the file exists on this worker",
        ) from None
    except ssl.SSLError:
        pass
    try:
        trust_context(None)
    except ssl.SSLError:
        raise _Found(
            "failed",
            f"the trust bundle `{os.environ.get('LAKANA_CA_BUNDLE')}` (LAKANA_CA_BUNDLE) cannot "
            "be read or holds no certificate; check the file on this worker",
        ) from None
    raise _Found(
        "failed",
        "the connection's ca_certificate holds no usable certificate; set it again with "
        "`lakana connection update NAME --ca-file ca.pem`",
    )


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

    Raises
    ------
    _Found
        ``failed`` when the file `LAKANA_CA_BUNDLE` names cannot be read.
    """
    if mode == "disable":
        return sock
    context = _context(ca_pem, mode)
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
    targets = [(str(e["host"]), int(e["port"])) for e in body["endpoints"]]
    if not targets and kind == "http":
        parts = urlsplit(str(body["address"]["base_url"]))
        targets = [(parts.hostname or "", parts.port or 443)]
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
            f"reachable: `{host}:{port}` greeted like MariaDB or MySQL",
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
    _open(host, port, routing, t).close()
    return "ok", TCP_OK


def _query(params: Mapping[str, str]) -> str:
    """Encode query parameters as SigV4 and Azure expect them: sorted, RFC 3986.

    Parameters
    ----------
    params
        Name to value.

    Returns
    -------
    str
        ``a=1&b=2``.
    """
    return "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(params.items())
    )


def sigv4_headers(
    *,
    host: str,
    path: str,
    params: Mapping[str, str],
    region: str,
    access_key_id: str,
    secret_access_key: str,
    session_token: str | None = None,
    now: time.struct_time | None = None,
) -> dict[str, str]:
    """Sign an S3 ``GET`` with AWS Signature Version 4.

    Parameters
    ----------
    host
        The ``Host`` header as sent (``host`` or ``host:port``).
    path
        The request path, already encoded.
    params
        The query parameters.
    region
        The bucket's region.
    access_key_id
        The key id.
    secret_access_key
        The secret key.
    session_token
        A temporary credential's token, or `None`.
    now
        The signing time (UTC); now when `None`.

    Returns
    -------
    dict
        The headers to send: ``Authorization``, ``x-amz-date``, ``x-amz-content-sha256`` and,
        with a session token, ``x-amz-security-token``.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", now or time.gmtime())
    day = stamp[:8]
    headers = {"host": host, "x-amz-content-sha256": _EMPTY_SHA256, "x-amz-date": stamp}
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed = ";".join(sorted(headers))
    canonical = "\n".join(
        [
            "GET",
            path,
            _query(params),
            "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)),
            signed,
            _EMPTY_SHA256,
        ]
    )
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = f"AWS4{secret_access_key}".encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    del headers["host"]
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, SignedHeaders={signed}, "
        f"Signature={signature}"
    )
    return headers


def azure_shared_key(
    *, account: str, account_key: str, path: str, params: Mapping[str, str], date: str
) -> dict[str, str]:
    """Sign an Azure Blob Storage ``GET`` with the account's shared key.

    Parameters
    ----------
    account
        The storage account.
    account_key
        Its key (base64).
    path
        The request path (``/<container>``; an emulator's ``/<account>/<container>``).
    params
        The query parameters, unencoded.
    date
        The ``x-ms-date`` (RFC 1123, GMT).

    Returns
    -------
    dict
        ``x-ms-date``, ``x-ms-version`` and ``Authorization``.

    Raises
    ------
    _Found
        ``credentials`` when the key is not base64.
    """
    headers = {"x-ms-date": date, "x-ms-version": _AZURE_VERSION}
    resource = f"/{account}{unquote(path)}" + "".join(
        f"\n{k.lower()}:{v}" for k, v in sorted(params.items())
    )
    to_sign = "GET\n" + "\n" * 11 + "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    try:
        key = base64.b64decode(account_key, validate=True)
    except ValueError:
        raise _Found("credentials", "the account_key is not base64") from None
    digest = hmac.new(key, (to_sign + resource).encode(), hashlib.sha256).digest()
    headers["Authorization"] = f"SharedKey {account}:{base64.b64encode(digest).decode()}"
    return headers


def _dechunk(body: bytes) -> bytes:
    """Join the chunks of a body sent with ``Transfer-Encoding: chunked``.

    Parameters
    ----------
    body
        The raw body, possibly cut short at the read limit.

    Returns
    -------
    bytes
        The data of every complete chunk; what follows a malformed size is dropped.
    """
    data = b""
    while body:
        size, _, rest = body.partition(b"\r\n")
        try:
            length = int(size.split(b";", 1)[0], 16)
        except ValueError:
            break
        if length == 0:
            break
        data += rest[:length]
        body = rest[length + 2 :]
    return data


def _exchange(
    method: str,
    url: str,
    headers: Mapping[str, str],
    routing: Mapping[str, Any],
    t: float,
    body: bytes = b"",
) -> tuple[int, str]:
    """Send one request over TLS (through the proxy or directly) and read the answer's start.

    The check's only HTTP client: it follows no redirect (a 3xx is the answer) and reads at most
    64 KiB.

    Parameters
    ----------
    method
        ``GET`` or ``POST``.
    url
        The ``https`` URL, query included.
    headers
        Headers besides ``Host``, ``Connection`` and ``Content-Length``.
    routing
        The routing entry.
    t
        Seconds per step.
    body
        What a ``POST`` sends.

    Returns
    -------
    tuple
        The status and up to 64 KiB of the body as text, chunks joined.

    Raises
    ------
    _Found
        For a network or TLS failure, a refusal of the egress proxy, or an answer that is not
        HTTP.
    """
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    lines = [f"{method} {target} HTTP/1.1", f"Host: {parts.netloc}", "Connection: close"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    if method == "POST":
        lines.append(f"Content-Length: {len(body)}")
    try:
        sock = _open(host, port, routing, t)
        try:
            with _tls(sock, host, None, "verify-full") as tls:
                tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
                data = b""
                while len(data) < _ANSWER_LIMIT and (chunk := tls.recv(16384)):
                    data += chunk
        finally:
            sock.close()
    except OSError as exc:
        _raise_network(exc, host, port, t)
    head, _, answer = data.partition(b"\r\n\r\n")
    status = head.split(b" ", 2)
    if len(status) < 2 or not status[0].startswith(b"HTTP/") or not status[1].isdigit():
        raise _Found("failed", f"`{host}:{port}` did not answer HTTP")
    if re.search(rb"(?im)^transfer-encoding:[ \t]*chunked[ \t]*$", head):
        answer = _dechunk(answer)
    return int(status[1]), answer.decode(errors="replace")


def _get(
    url: str, headers: Mapping[str, str], routing: Mapping[str, Any], t: float
) -> tuple[int, str]:
    """Send one ``GET`` with `_exchange`.

    Parameters
    ----------
    url
        The ``https`` URL, query included.
    headers
        Headers besides ``Host`` and ``Connection``.
    routing
        The routing entry.
    t
        Seconds per step.

    Returns
    -------
    tuple
        The status and up to 64 KiB of the body as text.
    """
    return _exchange("GET", url, headers, routing, t)


def _store_answer(status: int, body: str, what: str) -> None:
    """Read the answer to a credential step's list request.

    Parameters
    ----------
    status
        The HTTP status.
    body
        The start of the body (XML for S3 and Azure).
    what
        The bucket or container, for the message.

    Raises
    ------
    _Found
        ``credentials`` for 401 and 403, ``not_found`` for 404, ``failed`` for another status
        than 200; the store's own code and message are kept.
    """
    if status == 200:
        return
    code = re.search(r"<Code>([^<]{1,200})</Code>", body)
    text = re.search(r"<Message>([^<]{1,500})</Message>", body)
    said = ": ".join(m.group(1).strip() for m in (code, text) if m) or f"HTTP {status}"
    if status in (401, 403):
        raise _Found("credentials", f"the store refused the credentials ({said})")
    if status == 404:
        raise _Found("not_found", f"{what} does not exist ({said})")
    raise _Found("failed", f"the store answered HTTP {status} ({said})")


def _s3_credentials(body: Mapping[str, Any], routing: Mapping[str, Any], t: float) -> None:
    """List at most one object under the prefix with the connection's key (SigV4).

    Parameters
    ----------
    body
        An ``s3`` connection with a secret.
    routing
        The routing entry.
    t
        Seconds per step.
    """
    address, settings, secret = body["address"], body["settings"], body["secret"]
    endpoint = urlsplit(str(address["endpoint"]))
    path = f"{endpoint.path.rstrip('/')}/{quote(str(address['bucket']))}"
    params = {"list-type": "2", "max-keys": "1"}
    if settings.get("prefix"):
        params["prefix"] = str(settings["prefix"])
    headers = sigv4_headers(
        host=endpoint.netloc,
        path=path,
        params=params,
        region=str(address["region"]),
        access_key_id=str(settings["access_key_id"]),
        secret_access_key=str(secret.get("secret_access_key", "")),
        session_token=secret.get("session_token"),
    )
    url = f"https://{endpoint.netloc}{path}?{_query(params)}"
    _store_answer(*_get(url, headers, routing, t), f"bucket `{address['bucket']}`")


def _azure_credentials(body: Mapping[str, Any], routing: Mapping[str, Any], t: float) -> None:
    """List at most one blob under the prefix with the SAS token or the shared key.

    Parameters
    ----------
    body
        An ``azure`` connection with a secret.
    routing
        The routing entry.
    t
        Seconds per step.
    """
    address, settings, secret = body["address"], body["settings"], body["secret"]
    account, container = str(address["account"]), str(address["container"])
    url = urlsplit(AZURE_URL.format(account=account, container=quote(container)))
    params = {"restype": "container", "comp": "list", "maxresults": "1"}
    if settings.get("prefix"):
        params["prefix"] = str(settings["prefix"])
    query = _query(params)
    if secret.get("sas_token"):
        headers = {"x-ms-version": _AZURE_VERSION}
        query += "&" + str(secret["sas_token"]).lstrip("?")
    else:
        headers = azure_shared_key(
            account=account,
            account_key=str(secret.get("account_key", "")),
            path=url.path,
            params=params,
            date=formatdate(usegmt=True),
        )
    full = f"https://{url.netloc}{url.path}?{query}"
    _store_answer(*_get(full, headers, routing, t), f"container `{container}`")


class BundleUnreadable(OSError):
    """The file `LAKANA_CA_BUNDLE` names cannot be read, or is not ASCII."""


def conninfo(parts: Mapping[str, str]) -> str:
    r"""Write a libpq connection string, every value quoted.

    The ADBC driver takes only a connection string. Each value is single-quoted with ``\`` and
    ``'`` escaped, so a database or user name can never add a keyword of its own. `sql.read`
    uses the same function.

    Parameters
    ----------
    parts
        Keyword to value.

    Returns
    -------
    str
        Such as ``host='db' port='5432'``.
    """

    def quoted(value: str) -> str:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    return " ".join(f"{key}={quoted(value)}" for key, value in parts.items())


@contextlib.contextmanager
def root_certificates(ca_pem: str | None) -> Generator[str]:
    """Name the certificates libpq trusts: the trust bundle plus the connection's own CA.

    CA certificates are not secret; a file of both is written only when the connection has a
    CA of its own, and removed afterwards. `sql.read` uses the same function.

    Parameters
    ----------
    ca_pem
        The connection's CA certificates (PEM), or `None`.

    Yields
    ------
    str
        A temporary file, the file `LAKANA_CA_BUNDLE` names, or ``system`` when neither is set.

    Raises
    ------
    BundleUnreadable
        When the file `LAKANA_CA_BUNDLE` names cannot be read, or is not ASCII; only then (a
        failure to write the temporary file is an `OSError` of its own).
    """
    bundle = os.environ.get("LAKANA_CA_BUNDLE")
    try:
        trusted = Path(bundle).read_text(encoding="ascii") if bundle else ""
    except UnicodeDecodeError as exc:
        raise BundleUnreadable(errno.EINVAL, "the trust bundle is not ASCII PEM") from exc
    except OSError as exc:
        raise BundleUnreadable(exc.errno, exc.strerror) from exc
    if not ca_pem:
        yield bundle or "system"
        return
    with tempfile.NamedTemporaryFile("w", suffix=".pem", encoding="ascii") as handle:
        handle.write(f"{trusted}\n{ca_pem}\n")
        handle.flush()
        yield handle.name


def _unavailable(module: str, package: str) -> _Found:
    """Say that a credential step's driver does not import here.

    Parameters
    ----------
    module
        The module that failed to import.
    package
        What to install.

    Returns
    -------
    _Found
        ``failed``: a test that could not check the credentials does not pass.
    """
    return _Found("failed", UNAVAILABLE.format(module=module, package=package))


def _said(text: str) -> str:
    """Keep a server's own words on one line.

    Parameters
    ----------
    text
        A driver's message.

    Returns
    -------
    str
        Its whitespace collapsed; `check` masks and bounds the whole message.
    """
    return " ".join(text.split())


def _bundle_unreadable() -> _Found:
    """Say that the trust bundle cannot be read.

    Returns
    -------
    _Found
        ``failed``, naming the variable.
    """
    return _Found(
        "failed",
        f"the trust bundle `{os.environ.get('LAKANA_CA_BUNDLE')}` (LAKANA_CA_BUNDLE) cannot be "
        "read; check that the file exists on this worker",
    )


def _postgresql_credentials(body: Mapping[str, Any], routing: Mapping[str, Any], t: float) -> None:
    """Log in to PostgreSQL and execute ``SELECT 1`` in a read-only transaction.

    The result is not read: fetching a row needs pyarrow, which the check environment leaves
    out, and a login that runs a statement has proven the credentials.

    Parameters
    ----------
    body
        A ``postgresql`` connection with a secret.
    routing
        The routing entry: a ``relay_port`` sends the login through the relay (libpq's
        ``hostaddr``) while TLS checks the certificate for the real host.
    t
        Seconds per step.

    Raises
    ------
    _Found
        ``credentials`` when PostgreSQL refuses the login, ``tls`` for a certificate it does not
        trust, ``failed`` for anything else or a driver that does not import.
    """
    try:
        import adbc_driver_postgresql.dbapi as pg
    except ImportError:
        missing = _unavailable("adbc_driver_postgresql", "lakana[assets]")
        raise missing from None
    address, settings, secret = body["address"], body["settings"], body["secret"]
    host, mode = str(address["host"]), str(settings.get("tls", "verify-full"))
    parts = {"host": host, "port": str(address["port"])}
    relay_port = routing.get("relay_port")
    if isinstance(relay_port, int):
        try:
            parts.update(hostaddr=socket.gethostbyname(RELAY_HOST), port=str(relay_port))
        except OSError as exc:
            _raise_network(exc, RELAY_HOST, relay_port, t)
    parts.update(
        dbname=str(address["database"]),
        user=str(settings["user"]),
        password=str(secret.get("password", "")),
        sslmode=mode,
        connect_timeout=str(max(2, int(t))),
        options="-c default_transaction_read_only=on",
    )
    for name, value in parts.items():
        if "\x00" in value:
            raise _Found(
                "credentials",
                f"the `{name}` holds a NUL character, which a connection string cannot carry; "
                "set it again",
            )
    try:
        with root_certificates(settings.get("ca_certificate")) as roots:
            if mode == "verify-full":
                parts["sslrootcert"] = roots
            database = pg.connect(conninfo(parts))
    except BundleUnreadable:
        unreadable = _bundle_unreadable()
        raise unreadable from None
    except OSError as exc:
        raise _Found(
            "failed",
            "could not write the connection's CA certificate for libpq "
            f"({exc.strerror or type(exc).__name__})",
        ) from None
    except pg.Error as exc:
        text = str(exc)
        if "password authentication failed" in text or "no pg_hba.conf entry" in text:
            raise _Found("credentials", f"PostgreSQL refused the login ({_said(text)})") from None
        if "certificate verify failed" in text or "does not match host name" in text:
            raise _Found("tls", _tls_text(host)) from None
        raise _Found("failed", f"the PostgreSQL login failed ({_said(text)})") from None
    try:
        database.cursor().execute("SELECT 1")
    except pg.Error as exc:
        raise _Found(
            "failed", f"PostgreSQL accepted the login but not SELECT 1 ({_said(str(exc))})"
        ) from None
    finally:
        database.close()


def _mariadb_credentials(body: Mapping[str, Any], routing: Mapping[str, Any], t: float) -> str:
    """Log in to MariaDB over the configured TLS and run ``SELECT 1`` in a read-only transaction.

    Parameters
    ----------
    body
        A ``mariadb`` connection with a secret.
    routing
        The routing entry: PyMySQL gets a socket opened the way a task's would be (the relay).
    t
        Seconds per step.

    Returns
    -------
    str
        What was found of TLS, for the message.

    Raises
    ------
    _Found
        ``credentials`` for a refused login, ``tls`` for a certificate not trusted or a server
        without TLS, ``failed`` for anything else or a driver that does not import.
    """
    try:
        import pymysql
    except ImportError:
        missing = _unavailable("pymysql", "lakana[assets]")
        raise missing from None
    address, settings, secret = body["address"], body["settings"], body["secret"]
    host, port = str(address["host"]), int(address["port"])
    mode = str(settings.get("tls", "verify-full"))
    context = None if mode == "disable" else _context(settings.get("ca_certificate"), mode)
    database = pymysql.connections.Connection(
        host=host,
        port=port,
        user=str(settings["user"]),
        password=str(secret.get("password", "")),
        database=str(address["database"]),
        ssl=context,
        ssl_disabled=context is None,
        local_infile=False,
        connect_timeout=t,
        read_timeout=t,
        write_timeout=t,
        defer_connect=True,
    )
    sock = _open(host, port, routing, t)
    try:
        database.connect(sock=sock)
        cursor = database.cursor()
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute("SELECT 1")
        cursor.fetchall()
    except pymysql.MySQLError as exc:
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else None
        said = _said(str(exc.args[1] if len(exc.args) > 1 else exc))
        if isinstance(getattr(exc, "original_exception", None), ssl.SSLCertVerificationError):
            raise _Found("tls", _tls_text(host)) from None
        if "SSL is required but the server doesn't support it" in said:
            raise _Found(
                "tls",
                f"the server at `{host}:{port}` does not offer TLS; enable TLS on the server or "
                "set the connection's `tls` to `disable`",
            ) from None
        if code in (1044, 1045, 1698):
            raise _Found("credentials", f"MariaDB refused the login ({said})") from None
        raise _Found("failed", f"the MariaDB login failed ({said})") from None
    finally:
        with contextlib.suppress(Exception):
            database.close()
        sock.close()
    return {
        "disable": "TLS not used (tls: disable)",
        "require": "TLS in use, certificate not checked",
    }.get(mode, "TLS verified")


def _sqlite_opens(routing: Mapping[str, Any]) -> None:
    """Open a ``sqlite-file`` connection's file read-only and run ``SELECT 1``.

    Parameters
    ----------
    routing
        The routing entry, whose ``root_dir`` and ``path`` `_check_mount` has checked.

    Raises
    ------
    _Found
        ``failed`` when the file is no SQLite database or the driver does not import.
    """
    try:
        import adbc_driver_sqlite.dbapi as lite
    except ImportError:
        missing = _unavailable("adbc_driver_sqlite", "lakana[assets]")
        raise missing from None
    path = f"{str(routing['root_dir']).rstrip('/')}/{str(routing['path']).strip('/')}"
    try:
        database = lite.connect(f"file:{quote(path)}?mode=ro")
        try:
            database.cursor().execute("SELECT 1")
        finally:
            database.close()
    except lite.Error as exc:
        raise _Found(
            "failed",
            f"`{routing['path']}` cannot be read as a SQLite database ({_said(str(exc))})",
        ) from None


def _b64url(data: bytes) -> str:
    """Encode as unpadded base64url, as a JWT does.

    Parameters
    ----------
    data
        The bytes.

    Returns
    -------
    str
        The text.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _gcs_key(secret: Mapping[str, Any]) -> dict[str, Any]:
    """Read a ``gcs`` secret's service-account key.

    Parameters
    ----------
    secret
        The connection's secret.

    Returns
    -------
    dict
        The key's JSON.

    Raises
    ------
    _Found
        ``credentials`` for a secret that is no service-account key.
    """
    try:
        key = json.loads(str(secret.get("service_account_json", "")))
    except ValueError:
        key = None
    fields = ("client_email", "private_key", "token_uri")
    if not isinstance(key, dict) or not all(isinstance(key.get(f), str) for f in fields):
        raise _Found(
            "credentials",
            "the secret is not a service-account key (JSON with client_email, private_key and "
            f"token_uri); {GCS_NEXT_STEP}",
        )
    return key


def _gcs_assertion(key: Mapping[str, Any], now: int) -> str:
    """Sign the token request of a service-account key (RS256).

    Parameters
    ----------
    key
        The key's JSON.
    now
        The time, in seconds since the epoch.

    Returns
    -------
    str
        The signed JWT.

    Raises
    ------
    _Found
        ``credentials`` for a private key that is no RSA key; ``failed`` without cryptography.
    """
    try:
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        missing = _unavailable("cryptography", "cryptography")
        raise missing from None
    try:
        private = serialization.load_pem_private_key(key["private_key"].encode(), None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        private = None
    if not isinstance(private, rsa.RSAPrivateKey):
        raise _Found(
            "credentials",
            "the key's private_key is not an RSA private key (PEM); use a service-account JSON "
            "key with an RSA private key: the key file Google gave you",
        )
    header = {"alg": "RS256", "typ": "JWT"}
    if isinstance(key.get("private_key_id"), str):
        header["kid"] = key["private_key_id"]
    claims = {
        "iss": key["client_email"],
        "scope": GCS_SCOPE,
        "aud": key["token_uri"],
        "iat": now,
        "exp": now + _JWT_SECONDS,
    }
    signing = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    signature = private.sign(signing.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing}.{_b64url(signature)}"


def _token_target(token_uri: str, declared: set[tuple[str, int]]) -> None:
    """Accept a key's ``token_uri`` only as an ``https`` URL of a declared token endpoint.

    Parsed once, strictly, before anything is signed: no whitespace or control character, no
    user, a port that parses (and is declared: never 0), a host and port among the connection's
    ``token`` endpoints.

    Parameters
    ----------
    token_uri
        The key's ``token_uri``.
    declared
        The connection's token endpoints, as host and port.

    Raises
    ------
    _Found
        ``credentials``, naming the field, the declared endpoints and at most the URL's host
        (and a port other than 443), never the URL itself; nothing is sent.
    """
    shown = ", ".join(f"`{h}:{p}`" for h, p in sorted(declared)) or "none"
    if any(c.isspace() or not c.isprintable() for c in token_uri):
        malformed = True
    else:
        try:
            parts = urlsplit(token_uri)
            host, explicit = parts.hostname, parts.port
        except ValueError:
            malformed = True
        else:
            malformed = (
                parts.scheme != "https"
                or not host
                or parts.username is not None
                or parts.password is not None
                or explicit == 0
            )
    if malformed:
        raise _Found(
            "credentials",
            "the key's token_uri is not an https URL of a token endpoint of the connection "
            f"({shown}); {GCS_NEXT_STEP}",
        )
    port = 443 if explicit is None else explicit
    if (host, port) not in declared:
        named = str(host)[:100] + ("" if port == 443 else f":{port}")
        raise _Found(
            "credentials",
            f"the key's token_uri names `{named}`, which is not the connection's token endpoint "
            f"({shown}); Lakana sends a signed key nowhere else: {GCS_NEXT_STEP}",
        )


def _gcs_token(token_uri: str, assertion: str, routing: Mapping[str, Any], t: float) -> str:
    """Exchange a signed assertion for an access token, the way the listing is sent.

    The request goes over `_exchange`: through the egress proxy when the task has one, no
    redirect followed.

    Parameters
    ----------
    token_uri
        The key's token endpoint, already checked by `_token_target`.
    assertion
        The signed JWT.
    routing
        The routing entry.
    t
        Seconds per step.

    Returns
    -------
    str
        The access token (never written into a message).

    Raises
    ------
    _Found
        ``credentials`` when Google refuses the key; the network outcomes of `_exchange`;
        ``failed`` for another answer.
    """
    parts = urlsplit(token_uri)
    where = f"`{parts.hostname}:{parts.port or 443}`"
    form = _query(
        {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
    )
    status, text = _exchange(
        "POST",
        token_uri,
        {"Content-Type": "application/x-www-form-urlencoded"},
        routing,
        t,
        form.encode("ascii"),
    )
    try:
        said = json.loads(text)
    except ValueError:
        said = {}
    said = said if isinstance(said, dict) else {}
    if status in (400, 401, 403):
        detail = ": ".join(str(said[k])[:300] for k in ("error", "error_description") if k in said)
        raise _Found("credentials", f"Google refused the key ({detail or f'HTTP {status}'})")
    token = said.get("access_token")
    if status != 200 or not isinstance(token, str):
        raise _Found("failed", f"the token endpoint {where} answered HTTP {status}")
    return token


def _gcs_credentials(body: Mapping[str, Any], routing: Mapping[str, Any], t: float) -> None:
    """Sign a token request with the key, then list at most one object under the prefix.

    The key's ``token_uri`` must be a token endpoint of the connection (Google's): the signed
    assertion is sent nowhere else, under either runner.

    Parameters
    ----------
    body
        A ``gcs`` connection with a secret.
    routing
        The routing entry.
    t
        Seconds per step.

    Raises
    ------
    _Found
        ``credentials`` for a key that is not one, names another token endpoint, or is refused;
        ``not_found`` for a missing bucket.
    """
    key = _gcs_key(body["secret"])
    token_uri = key["token_uri"]
    declared = {
        (str(e["host"]), int(e["port"])) for e in body["endpoints"] if e.get("role") == "token"
    }
    _token_target(token_uri, declared)
    token = _gcs_token(token_uri, _gcs_assertion(key, int(time.time())), routing, t)
    bucket = str(body["address"]["bucket"])
    params = {"maxResults": "1"}
    if body["settings"].get("prefix"):
        params["prefix"] = str(body["settings"]["prefix"])
    url = f"{GCS_URL.format(bucket=quote(bucket, safe=''))}?{_query(params)}"
    status, text = _get(url, {"Authorization": f"Bearer {token}"}, routing, t)
    if status == 200:
        return
    try:
        error = json.loads(text).get("error", {})
        said = str(error.get("message", ""))[:500] if isinstance(error, dict) else ""
    except (ValueError, AttributeError):
        said = ""
    said = said or f"HTTP {status}"
    if status in (401, 403):
        raise _Found("credentials", f"the store refused the credentials ({said})")
    if status == 404:
        raise _Found("not_found", f"bucket `{bucket}` does not exist ({said})")
    raise _Found("failed", f"the store answered HTTP {status} ({said})")


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
    top = root_dir.rstrip("/") or "/"
    try:
        opened = os.open(top, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (FileNotFoundError, NotADirectoryError):
        with contextlib.suppress(OSError):
            if stat.S_ISLNK(os.lstat(top).st_mode):
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


_UNSAFE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)
"""Control and bidirectional control characters: the same set as `lakana`'s `safe_name`."""


def _safe(text: str) -> str:
    r"""Write every control and bidirectional control character as an escape.

    A server's error text ends up in the message; a line feed there could forge a line in a
    log, an escape sequence could rewrite a terminal. The check cannot import `lakana`, so this
    mirrors its `safe_name`.

    Parameters
    ----------
    text
        The message.

    Returns
    -------
    str
        `text` with such characters written as ``\x1b`` or ``\u202e``.
    """

    def escaped(match: re.Match[str]) -> str:
        code = ord(match.group())
        return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"

    return _UNSAFE.sub(escaped, text)


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
        if outcome == "ok" and secret and kind in ("s3", "azure", "gcs"):
            step = {"s3": _s3_credentials, "azure": _azure_credentials, "gcs": _gcs_credentials}
            step[kind](connection, route, timeout)
            message += ", credentials accepted"
        elif outcome == "ok" and secret and kind == "postgresql":
            _postgresql_credentials(connection, route, timeout)
            message += ", credentials accepted"
        elif outcome == "ok" and secret and kind == "mariadb":
            tls = _mariadb_credentials(connection, route, timeout)
            message += f", {tls}, credentials accepted"
        elif outcome == "ok" and kind == "mariadb":
            message += MARIADB_GREETING_ONLY
        elif outcome == "ok" and kind == "sqlite-file":
            _sqlite_opens(route)
            message += "; it opens read-only as a SQLite database"
        elif outcome == "ok" and secret and kind == "http":
            message += NOT_YET
    except _Found as found:
        outcome, message = found.outcome, found.message
    except Exception as exc:
        outcome, message = "failed", f"the check failed unexpectedly ({type(exc).__name__})"
    return outcome, _safe(_mask(message, secret))[:MAX_MESSAGE]


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
