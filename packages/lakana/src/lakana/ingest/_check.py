"""The connection test's check of reachability and TLS.

Run as ``python -I -m lakana.ingest._check`` (the local runner), or by path as
``python -I -B /opt/lakana-check/check.py`` in the task image (the Docker runner). It reads one
JSON object from standard input, ``{"connection": <TaskConnectionOut-shaped>, "routing":
{...}}``, and writes one line, ``{"outcome": ..., "message": ...}``, with exit status 0 whatever
it found. The routing is what a task's description says about the connection (`relay_port`,
`root_dir`, `path`).

This step checks reachability only: the name resolves, the port answers, the TLS handshake
verifies as the connection is configured, a mounted root and path are there. The credential step
of each kind arrives with its helper; until then a kind with a secret ends its message with
"; credentials not checked yet". Every secret value is replaced by ``***`` in the message.

The module uses the standard library only, and imports nothing of `lakana`: the task image runs
it as a lone file on its bare Python (`test_the_check_imports_only_the_standard_library`).
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
import time
from collections.abc import Mapping
from email.utils import formatdate
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

NOT_YET = "; credentials not checked yet"
"""What a kind with a secret adds until its credential step exists."""

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
GCS_NOT_CHECKED = (
    "; the credentials of a gcs connection are not checked (the check cannot sign its key): a "
    "refused key shows in the first files.read"
)
"""What an ``ok`` of a ``gcs`` connection with a secret ends with."""
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
        ``failed`` when the file `LAKANA_CA_BUNDLE` names cannot be read.
    """
    try:
        return trust_context(ca_pem, verify=mode != "require")
    except (FileNotFoundError, PermissionError):
        raise _Found(
            "failed",
            f"the trust bundle `{os.environ.get('LAKANA_CA_BUNDLE')}` (LAKANA_CA_BUNDLE) cannot "
            "be read; check that the file exists on this worker",
        ) from None


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


def _get(
    url: str, headers: Mapping[str, str], routing: Mapping[str, Any], t: float
) -> tuple[int, str]:
    """Send one ``GET`` over TLS (through the proxy or directly) and read the answer's start.

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

    Raises
    ------
    _Found
        For a network or TLS failure, or an answer that is not HTTP.
    """
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    lines = [f"GET {target} HTTP/1.1", f"Host: {parts.netloc}", "Connection: close"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    try:
        sock = _open(host, port, routing, t)
        try:
            with _tls(sock, host, None, "verify-full") as tls:
                tls.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
                data = b""
                while len(data) < _ANSWER_LIMIT and (chunk := tls.recv(16384)):
                    data += chunk
        finally:
            sock.close()
    except OSError as exc:
        _raise_network(exc, host, port, t)
    head, _, body = data.partition(b"\r\n\r\n")
    status = head.split(b" ", 2)
    if len(status) < 2 or not status[0].startswith(b"HTTP/") or not status[1].isdigit():
        raise _Found("failed", f"`{host}:{port}` did not answer HTTP")
    return int(status[1]), body.decode(errors="replace")


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
        if outcome == "ok" and secret and kind in ("s3", "azure"):
            (_s3_credentials if kind == "s3" else _azure_credentials)(connection, route, timeout)
            message += ", credentials accepted"
        elif outcome == "ok" and secret and kind == "gcs":
            message += GCS_NOT_CHECKED
        elif outcome == "ok" and secret and kind != "tcp":
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
