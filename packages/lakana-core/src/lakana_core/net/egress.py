"""The egress checks that every Lakana client of a URL someone else named shares.

A URL that comes from a user, a secret or a document fetched from the internet is hostile input:
an attacker who can name it could otherwise point Lakana at the cloud metadata service or at an
internal admin interface. These functions are the parts of that defence that do not depend on
what is sent:

- `parse_url` splits a URL and refuses userinfo, control characters and shorthand IP spellings.
- `is_internal` refuses every address that is not plainly public, including IPv6 forms that
  carry an internal IPv4 address.
- `lookup` resolves a name **once**, on a thread pool, under a deadline; the caller checks every
  address and then connects to one of them (`pinned_request`), so DNS cannot answer differently
  between the check and the connection (rebinding).
- `tls_and_proxy` builds the TLS and proxy settings from the operator's settings only: clients
  run with ``trust_env=False``, so ``HTTPS_PROXY``, ``SSL_CERT_FILE`` and the like change nothing.
- `classify` reduces any exception to a fixed reason. The text of an `httpx` exception embeds the
  URL, so it is never kept, logged or chained.
- `http_libraries_silenced` drops every log record of `httpx` and `httpcore` while a request
  runs, because they log URLs and host names.

- `is_never_reachable` is the stricter rule for a host an operator listed: it may resolve to a
  private address, never to loopback, link-local, unspecified, multicast, reserved or a known cloud
  service address (metadata services, Azure's WireServer), nor to an IPv6 form that carries one.

The worker's webhook client (`lakana_worker.egress`) and the JSON fetcher of the CI OIDC
verification (`lakana_core.net.https_json`) are built on these.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import ipaddress
import logging
import re
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Coroutine, Generator, Sequence
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

T = TypeVar("T")

TIMEOUT = "timeout"
REFUSED = "connection refused"
TLS_ERROR = "TLS error"
NO_CONNECTION = "could not connect"
CONNECTION_LOST = "connection lost"
PROXY_ERROR = "proxy error"
FAILED = "the request failed"

# A lookup that times out keeps its thread until the system resolver gives up, so spare threads
# keep a few slow names from blocking the next request.
DNS = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="egress-dns")

_HTTPCORE_PARTS = ("connection", "http11", "http2", "proxy", "socks")

_SENDING: contextvars.ContextVar[bool] = contextvars.ContextVar("egress_sending", default=False)


class _SilenceWhileSending(logging.Filter):
    """Drop every record of the http libraries while a request is being sent."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Decide whether a record may pass.

        Parameters
        ----------
        record
            The record being logged.

        Returns
        -------
        bool
            `False` while a request is sent, so nothing of the URL reaches any handler.
        """
        return not _SENDING.get()


_SILENCER = _SilenceWhileSending()


def _silence_http_libraries() -> None:
    """Attach the silencing filter to every logger of `httpx` and `httpcore`.

    `httpx` logs every request's full URL at INFO and `httpcore` traces host names at DEBUG;
    the path and query of a webhook URL are its secret. A filter on a parent logger does not see
    the records of a child such as `httpcore.connection`, so the filter sits on every logger of
    both libraries. It only acts inside `http_libraries_silenced`; other code's `httpx` logging
    is untouched.
    """
    # httpcore creates its child loggers when it imports its connection modules, which happens
    # on the first request; naming them here attaches the filter to the very objects it will
    # fetch later.
    known = ("httpx", "httpcore", *(f"httpcore.{part}" for part in _HTTPCORE_PARTS))
    names = [n for n in logging.root.manager.loggerDict if n.split(".")[0] in ("httpx", "httpcore")]
    for name in {*known, *names}:
        logging.getLogger(name).addFilter(_SILENCER)


@contextlib.contextmanager
def http_libraries_silenced() -> Generator[None]:
    """Keep `httpx` and `httpcore` from logging anything inside the block.

    The filter is attached again on every entry: loggers created since the last one (httpcore's
    children) must carry it too.

    Yields
    ------
    None
        Nothing; the block runs with the libraries silenced.
    """
    _silence_http_libraries()
    token = _SENDING.set(True)
    try:
        yield
    finally:
        _SENDING.reset(token)


_HOST = re.compile(r"^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*$")
_MAX_HOST = 253

_REFUSED_NETWORKS = tuple(
    ipaddress.ip_network(text)
    for text in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "240.0.0.0/4",
        "::/128",
        "100::/64",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        # Site-local, deprecated (RFC 3879): the standard library calls it global.
        "fec0::/10",
        "ff00::/8",
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# The local-use NAT64 prefix (RFC 8215), from which operators pick their own prefixes; its
# addresses are judged by their last 32 bits, as the well-known prefix's are. A prefix an
# operator chose elsewhere cannot be known.
_LOCAL_NAT64 = ipaddress.ip_network("64:ff9b:1::/48")


class EgressRefused(Exception):
    """The address may not be used, or the request to it failed.

    Parameters
    ----------
    reason
        One of the callers' fixed reason strings; it never contains the URL.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _embedded_ipv4(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> list[ipaddress.IPv4Address]:
    """Return the IPv4 addresses an IPv6 address carries.

    The egress proxy of the launcher, which imports only the standard library, keeps a copy
    (`lakana_worker.launcher.egress._embedded_ipv4`); a test keeps the two in agreement.

    Parameters
    ----------
    address
        Any address.

    Returns
    -------
    list[ipaddress.IPv4Address]
        The IPv4-mapped, IPv4-compatible, 6to4, Teredo and NAT64 embedded addresses (the
        well-known ``64:ff9b::/96`` and the local-use ``64:ff9b:1::/48``, by their last 32
        bits); empty for IPv4 and for IPv6 addresses that carry none.
    """
    if not isinstance(address, ipaddress.IPv6Address):
        return []
    found: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address.teredo is not None:
        found.extend(address.teredo)
    if address in _NAT64 or address in _LOCAL_NAT64 or int(address) >> 32 == 0:
        found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return found


def is_internal(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Say whether an address must not be reached.

    Parameters
    ----------
    address
        Any address.

    Returns
    -------
    bool
        `True` for private, loopback, link-local, multicast, reserved, unspecified, CGNAT,
        documentation and benchmarking addresses, unique-local and site-local IPv6, anything the
        standard library does not call global, every address that `is_never_reachable` (Azure's
        WireServer, 168.63.129.16, is in a public range), and any IPv6 address that carries such
        an IPv4 address.
    """
    if is_never_reachable(address):
        return True
    embedded = _embedded_ipv4(address)
    if any(is_internal(inner) for inner in embedded):
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
        or any(
            address in network
            for network in _REFUSED_NETWORKS
            if network.version == address.version
        )
    )


def any_internal(addresses: Sequence[str]) -> bool:
    """Say whether any of a name's addresses must not be reached.

    Parameters
    ----------
    addresses
        Addresses as `resolve` returns them; an IPv6 zone (``%eth0``) is ignored.

    Returns
    -------
    bool
        `True` when at least one of them `is_internal`.
    """
    return any(is_internal(ipaddress.ip_address(address.split("%")[0])) for address in addresses)


_NEVER_NETWORKS = tuple(
    ipaddress.ip_network(text)
    for text in (
        "0.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "240.0.0.0/4",
        # Alibaba Cloud's metadata service, inside the CGNAT range.
        "100.100.100.200/32",
        # Azure's WireServer (VM extension settings, sometimes protected ones), in a public
        # range, so nothing else here or in `is_internal` would catch it (#543).
        "168.63.129.16/32",
        # Oracle Cloud's older metadata address, inside the IETF protocol range 192.0.0.0/24.
        "192.0.0.192/32",
        "::/128",
        "::1/128",
        "fe80::/10",
        "ff00::/8",
        # AWS's IPv6 metadata service, inside the unique-local range.
        "fd00:ec2::254/128",
        # Google Cloud's IPv6 metadata service, inside the unique-local range.
        "fd20:ce::254/128",
    )
)
"""Never reached, even for a host an operator listed. The egress proxy of the launcher, which
imports only the standard library, keeps a copy (`lakana_worker.launcher.egress._NEVER`); a test
keeps the two in sync."""


def is_never_reachable(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Say whether an address stays refused even for a host an operator allowed.

    Parameters
    ----------
    address
        Any address.

    Returns
    -------
    bool
        `True` for loopback, link-local (the cloud metadata address 169.254.169.254 among them),
        unspecified, multicast and reserved IPv4 addresses, the known cloud service addresses
        outside link-local (metadata services, Azure's WireServer 168.63.129.16), and any IPv6
        address that carries such an IPv4 address. Private ranges themselves, site-local IPv6
        (``fec0::/10``) among them, are not: an operator may list a host on an internal network.
    """
    if any(is_never_reachable(inner) for inner in _embedded_ipv4(address)):
        return True
    return any(
        address in network for network in _NEVER_NETWORKS if network.version == address.version
    )


def any_never_reachable(addresses: Sequence[str]) -> bool:
    """Say whether any of a name's addresses `is_never_reachable`.

    Parameters
    ----------
    addresses
        Addresses as `resolve` returns them; an IPv6 zone (``%eth0``) is ignored.

    Returns
    -------
    bool
        `True` when at least one of them is never reachable.
    """
    return any(
        is_never_reachable(ipaddress.ip_address(address.split("%")[0])) for address in addresses
    )


def resolve(host: str, port: int) -> list[str]:
    """Resolve a name to its addresses.

    Parameters
    ----------
    host
        The name.
    port
        The port, only for the lookup.

    Returns
    -------
    list[str]
        The distinct addresses, in the order the system gave them.
    """
    # The trailing dot makes the name absolute: no search domain is tried first.
    infos = socket.getaddrinfo(host + ".", port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def lookup(
    host: str,
    port: int,
    timed_out: str,
    unresolved: str,
    *,
    resolver: Callable[[str, int], list[str]],
    timeout: float,
) -> list[str]:
    """Resolve a name on a helper thread, giving up after `timeout` seconds.

    Parameters
    ----------
    host
        The name.
    port
        The port, only for the lookup.
    timed_out
        The fixed reason when the lookup does not answer in time.
    unresolved
        The fixed reason when the name does not resolve.
    resolver
        The function that resolves, normally `resolve`.
    timeout
        Seconds the lookup may take.

    Returns
    -------
    list[str]
        The addresses.

    Raises
    ------
    EgressRefused
        If the lookup times out or finds nothing.
    """
    future = DNS.submit(resolver, host, port)
    failure = ""
    addresses: list[str] = []
    try:
        addresses = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        failure = timed_out
    except OSError:
        failure = unresolved
    if failure:
        future.cancel()
        raise EgressRefused(failure)
    if not addresses:
        raise EgressRefused(unresolved)
    return addresses


def host_matches(host: str, allowed: tuple[str, ...]) -> bool:
    """Say whether a host is on an operator's allow-list.

    Parameters
    ----------
    host
        The normalised host name.
    allowed
        The normalised allow-list.

    Returns
    -------
    bool
        `True` for an exact entry, or for a subdomain of a ``*.domain`` entry.
    """
    for entry in allowed:
        if entry.startswith("*."):
            if host.endswith(entry[1:]) and len(host) > len(entry) - 1:
                return True
        elif host == entry:
            return True
    return False


def is_ip_literal(host: str) -> bool:
    """Say whether a host is an IP address rather than a name.

    Parameters
    ----------
    host
        A host as `parse_url` returns it.

    Returns
    -------
    bool
        `True` for an IPv4 or IPv6 literal.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def parse_url(url: str, invalid: str) -> tuple[str, str, int | None, str]:
    """Split a URL into scheme, host, port and target.

    Parameters
    ----------
    url
        The URL.
    invalid
        The fixed reason of every refusal.

    Returns
    -------
    tuple[str, str, int | None, str]
        The scheme, the host (lower case, no trailing dot, IDNA-encoded), the explicit port or
        `None`, and the path with query.

    Raises
    ------
    EgressRefused
        If the URL is malformed, is not ``http`` or ``https``, has a user name or password, or
        names a host that is neither a DNS name nor an IP literal in its full spelling.
    """
    if any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise EgressRefused(invalid)
    # Every refusal is raised outside the `except` blocks, so that no exception is chained to it.
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        parts, port, host = None, None, None
    if parts is None or host is None:
        raise EgressRefused(invalid)
    if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc or port == 0:
        raise EgressRefused(invalid)
    host = host.rstrip(".")
    if not is_ip_literal(host):
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            host = ""
        # A last label of digits only is a shorthand IPv4 spelling (`127.1`, `2130706433`) that
        # some resolvers expand; no real top-level domain looks like that.
        if not _HOST.match(host) or len(host) > _MAX_HOST or host.rsplit(".", 1)[-1].isdigit():
            raise EgressRefused(invalid)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return parts.scheme, host, port, target


def netloc(host: str, port: int) -> str:
    """Format a host and port for a URL or a Host header.

    Parameters
    ----------
    host
        A name or an IP literal.
    port
        The port.

    Returns
    -------
    str
        ``host:port``, with an IPv6 literal in brackets.
    """
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _chain(exc: BaseException) -> list[BaseException]:
    """List an exception with its causes and contexts.

    Parameters
    ----------
    exc
        The exception.

    Returns
    -------
    list[BaseException]
        The exception and everything it was raised from or during.
    """
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def classify(exc: BaseException) -> str:
    """Reduce any exception of a request to one of the fixed reasons.

    Parameters
    ----------
    exc
        What the request raised. Its text is never read.

    Returns
    -------
    str
        A fixed reason.
    """
    chain = _chain(exc)
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return TIMEOUT
    if isinstance(exc, httpx.ProxyError):
        return PROXY_ERROR
    if any(isinstance(inner, ssl.SSLError) for inner in chain):
        return TLS_ERROR
    if any(isinstance(inner, ConnectionRefusedError) for inner in chain):
        return REFUSED
    if isinstance(exc, httpx.ConnectError):
        return NO_CONNECTION
    if isinstance(exc, (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)):
        return CONNECTION_LOST
    return FAILED


def tls_and_proxy(
    ca_bundle: Path | None, proxy: SecretStr | None
) -> tuple[ssl.SSLContext | bool, httpx.Proxy | None]:
    """Build the TLS verification and the proxy of a client from the operator's settings.

    Parameters
    ----------
    ca_bundle
        The operator's CA file, which replaces the built-in store, or `None`.
    proxy
        The operator's proxy URL, or `None`.

    Returns
    -------
    tuple[ssl.SSLContext | bool, httpx.Proxy | None]
        `True` (the built-in store) or a context with the bundle, and the proxy or `None`.
        Verification is never turned off.
    """
    verify: ssl.SSLContext | bool = True
    if ca_bundle is not None:
        verify = ssl.create_default_context(cafile=str(ca_bundle))
    if proxy is None:
        return verify, None
    # The bundle also covers an https proxy's own certificate.
    proxy_url = proxy.get_secret_value()
    # httpcore refuses a TLS context for a plain-http proxy.
    proxy_tls = (
        verify
        if isinstance(verify, ssl.SSLContext) and urlsplit(proxy_url).scheme.lower() == "https"
        else None
    )
    return verify, httpx.Proxy(proxy_url, ssl_context=proxy_tls)


def pinned_request(
    scheme: str, host: str, port: int, target: str, address: str | None
) -> tuple[str, str, dict[str, object]]:
    """Name the URL, the Host header and the extensions of a request to a checked address.

    Parameters
    ----------
    scheme
        ``https``, or ``http`` for a host an operator allowed.
    host
        The checked host name or IP literal.
    port
        The port.
    target
        Path and query, starting with ``/``.
    address
        The checked address to connect to; `None` when going through a proxy, which resolves
        the name itself and cannot be pinned (it is trusted to do so).

    Returns
    -------
    tuple[str, str, dict[str, object]]
        The URL to request, the ``Host`` header, and the `httpx` request extensions: with an
        address and ``https``, the server name for TLS, so the certificate is verified against
        the name, never against the address.
    """
    host_header = host if ":" not in host else f"[{host}]"
    if port != (443 if scheme == "https" else 80):
        host_header = f"{host_header}:{port}"
    extensions: dict[str, object] = {}
    if address is not None:
        location = netloc(address, port)
        if scheme == "https":
            extensions["sni_hostname"] = host
    else:
        location = netloc(host, port)
    return f"{scheme}://{location}{target}", host_header, extensions


async def each_address(
    addresses: Sequence[str],
    timeout: float,
    attempt: Callable[[str, float, float], Awaitable[T]],
) -> T:
    """Try each checked address in turn until one connects, within one time budget.

    Parameters
    ----------
    addresses
        The checked addresses, in the system's order; at least one.
    timeout
        Seconds for the whole attempt.
    attempt
        Called with an address, the seconds left in all, and this address's share of them for
        connecting.

    Returns
    -------
    T
        What the first attempt that connected returned.

    Raises
    ------
    Exception
        The last attempt's failure, when no address connects, or the failure of an attempt that
        did connect.
    AssertionError
        Never in practice: there is at least one address.
    """
    deadline = time.monotonic() + timeout
    last = len(addresses) - 1
    for index, address in enumerate(addresses):
        remaining = max(deadline - time.monotonic(), 0.001)
        # Each address gets its share of what is left, so a silent one cannot use it all.
        share = remaining / (last - index + 1)
        try:
            return await attempt(address, remaining, share)
        except Exception as exc:
            # Only a connection that never came up moves on; a certificate that fails to verify
            # or an answer that breaks off ends the attempt.
            if index == last or not (
                isinstance(exc, httpx.ConnectTimeout) or classify(exc) in (REFUSED, NO_CONNECTION)
            ):
                raise
    raise AssertionError("unreachable: there is at least one address")  # pragma: no cover


def run_with_deadline(
    work: Coroutine[Any, Any, T], timeout: float, *, finish_streams: bool = False
) -> T:
    """Run a request's coroutine on a fresh event loop and give up at one deadline.

    Not `asyncio.run`: it waits for the loop's default executor, where a proxy name's lookup may
    still be running, and the request must end at its deadline. Call it from a thread without a
    running event loop.

    Parameters
    ----------
    work
        The request.
    timeout
        Seconds for the whole request, connection and answer together.
    finish_streams
        Whether to close, within one more second, the stream iterators that a body read
        abandoned midway (`httpx` nests several), before the loop closes.

    Returns
    -------
    T
        What the request returned.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(work, timeout))
    finally:
        if finish_streams:
            with contextlib.suppress(Exception):
                loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), 1.0))
        loop.close()
