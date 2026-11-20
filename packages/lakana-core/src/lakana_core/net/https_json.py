"""Fetch one small JSON document over https, from a public address only, or post a form for one.

For the discovery documents and key sets of OIDC issuers (`lakana_core.oidc.keys`, shared by CI
OIDC and the sign-in) and the sign-in's token endpoint (`post_form_json`). A discovery document
names the URL of the keys and of the token endpoint, so those URLs are hostile input even when
the issuer is trusted: the caller names the hosts a request may reach, and everything else is
refused.

- `check` requires ``https``, port 443 (or a port the caller names: the sign-in's issuer may
  carry one) and an allowed host name (never an IP literal), resolves
  the name **once** under `RESOLVE_TIMEOUT`, and refuses it when **any** of its addresses is
  internal. A host the operator listed (`private_hosts`) may resolve to a private address, so
  that an issuer on an internal network can be reached; loopback, link-local and the known
  metadata addresses stay refused even then (`lakana_core.net.egress.is_never_reachable`).
- `fetch_target` connects to a checked address, not to the name, with the name as the TLS server
  name, so the certificate is verified against the name and DNS cannot answer differently
  between the check and the connection. An operator's proxy resolves the name itself and is
  trusted to refuse internal addresses.
- No redirect is followed, no environment variable is read (``trust_env=False``), the answer
  must be 200, unencoded and at most `MAX_BYTES`, and one deadline covers the whole request.
- Every failure is a `FetchFailed` with a fixed reason, raised outside any ``except`` block. A
  reason never contains the URL, the form or a header, which may carry a client secret.

Call it from a thread without a running event loop.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from lakana_core.net import egress

MAX_BYTES = 64 * 1024
"""The largest answer read; a longer one is refused."""

TIMEOUT_SECONDS = 10.0
"""Seconds for one whole request, connection and answer together, after the lookup."""

RESOLVE_TIMEOUT = 5.0
"""Seconds the name lookup may take."""

INVALID = "the address is not a valid https URL"
NOT_HTTPS = "the address must use https"
NOT_443 = "the address must use port 443"
PORT_NOT_ALLOWED = "the address must use port 443 or the port of the configured issuer"
HOST_NOT_ALLOWED = "the host is not one this request may reach"
UNRESOLVED = "the host name does not resolve"
RESOLVE_TIMED_OUT = "the host name did not resolve in time"
PRIVATE = "the host is a private or internal address"
NEVER_REACHABLE = (
    "the host is a loopback, link-local or metadata address, which is refused even for an "
    "allowed host"
)
REDIRECT = "the server answered with a redirect, which is not followed"
TOO_LARGE = "the answer is larger than 64 KiB"
ENCODED = "the answer is compressed or encoded, which is not accepted"
NOT_JSON = "the answer is not a JSON document"

_resolve = egress.resolve


class FetchFailed(Exception):
    """The document could not be fetched.

    Parameters
    ----------
    reason
        One of this module's or `lakana_core.net.egress`'s fixed reasons; never the URL.
    status
        The HTTP status of an answer other than 200 and a redirect, or `None`.
    """

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Target:
    """A checked request target.

    Parameters
    ----------
    host
        The lower-case host name, without a trailing dot.
    port
        The port: 443, or the explicit port `check` allowed.
    target
        Path and query, starting with ``/``.
    addresses
        The checked addresses, in the system's order; the first that connects is used.
    """

    host: str
    port: int
    target: str
    addresses: tuple[str, ...]


def check(
    url: str,
    allowed_hosts: Collection[str],
    *,
    private_hosts: Collection[str] = (),
    ports: Collection[int] = (443,),
) -> Target:
    """Check a URL and resolve its host once.

    Parameters
    ----------
    url
        The URL.
    allowed_hosts
        The lower-case host names the request may reach.
    private_hosts
        The lower-case host names the operator allowed to resolve to private addresses.
    ports
        The ports the URL may name; 443 alone by default.

    Returns
    -------
    Target
        Where to send the request.

    Raises
    ------
    FetchFailed
        If the URL is malformed, not ``https``, not on an allowed port, not on an allowed host name,
        does not resolve in time, or any address of its host is internal (for a host in
        `private_hosts`: loopback, link-local or a metadata address).
    """
    reason = ""
    try:
        scheme, host, port, target = egress.parse_url(url, INVALID)
    except egress.EgressRefused as refused:
        reason, scheme, host, port, target = refused.reason, "", "", None, ""
    if not reason:
        if scheme != "https":
            reason = NOT_HTTPS
        elif port is not None and port not in ports:
            reason = NOT_443 if set(ports) <= {443} else PORT_NOT_ALLOWED
        elif egress.is_ip_literal(host) or host not in allowed_hosts:
            reason = HOST_NOT_ALLOWED
    addresses: list[str] = []
    if not reason:
        try:
            addresses = egress.lookup(
                host,
                port or 443,
                RESOLVE_TIMED_OUT,
                UNRESOLVED,
                resolver=_resolve,
                timeout=RESOLVE_TIMEOUT,
            )
        except egress.EgressRefused as refused:
            reason = refused.reason
    if not reason:
        if host in private_hosts:
            if egress.any_never_reachable(addresses):
                reason = NEVER_REACHABLE
        elif egress.any_internal(addresses):
            reason = PRIVATE
    if reason:
        raise FetchFailed(reason)
    return Target(host=host, port=port or 443, target=target, addresses=tuple(addresses))


@dataclass(frozen=True)
class _Request:
    """What to send to a checked target.

    Parameters
    ----------
    method
        ``GET`` or ``POST``.
    content
        The body, empty for a GET.
    headers
        Headers of the caller's, such as ``Authorization``; never logged or quoted.
    """

    method: str
    content: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


async def _send(
    target: Target,
    request: _Request,
    address: str | None,
    proxy: SecretStr | None,
    ca_bundle: Path | None,
    timeouts: tuple[float, float],
) -> bytes:
    """Send the request and read the answer, refusing anything but a small, plain 200.

    Parameters
    ----------
    target
        The checked target.
    request
        The method, body and headers.
    address
        The checked address to connect to; `None` when going through the proxy.
    proxy
        The operator's proxy, or `None`.
    ca_bundle
        The operator's CA file, or `None`.
    timeouts
        Seconds for each read and write, and for connecting and the TLS handshake.

    Returns
    -------
    bytes
        The body, at most `MAX_BYTES`.

    Raises
    ------
    FetchFailed
        On a redirect, another status than 200, an encoded body or one over `MAX_BYTES`.
    """
    timeout, connect_timeout = timeouts
    verify, client_proxy = egress.tls_and_proxy(ca_bundle, proxy)
    url, host_header, extensions = egress.pinned_request(
        "https", target.host, target.port, target.target, address
    )
    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        verify=verify,
        proxy=client_proxy,
        timeout=httpx.Timeout(timeout, connect=connect_timeout),
    ) as client:
        headers = {
            **request.headers,
            "Host": host_header,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        content: bytes | None = None
        if request.method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            content = request.content
        built = client.build_request(
            request.method, url, headers=headers, content=content, extensions=extensions
        )
        response = await client.send(built, stream=True)
        try:
            return await _read(response)
        finally:
            await response.aclose()


async def _read(response: httpx.Response) -> bytes:
    """Read a streamed answer within the limits.

    Parameters
    ----------
    response
        The answer, not yet read.

    Returns
    -------
    bytes
        The body.

    Raises
    ------
    FetchFailed
        On a redirect, another status than 200, an encoded body or one over `MAX_BYTES`.
    """
    status = response.status_code
    if 300 <= status < 400:
        raise FetchFailed(REDIRECT)
    if status != 200:
        raise FetchFailed(f"the server answered {status}", status=status)
    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
        raise FetchFailed(ENCODED)
    length = response.headers.get("Content-Length", "")
    if length.isdigit() and int(length) > MAX_BYTES:
        raise FetchFailed(TOO_LARGE)
    body = bytearray()
    async for chunk in response.aiter_raw():
        body.extend(chunk)
        if len(body) > MAX_BYTES:
            raise FetchFailed(TOO_LARGE)
    return bytes(body)


async def _send_any(
    target: Target,
    request: _Request,
    proxy: SecretStr | None,
    ca_bundle: Path | None,
    timeout: float,
) -> bytes:
    """Send through the proxy, or to each checked address in turn until one connects.

    Parameters
    ----------
    target
        The checked target.
    request
        The method, body and headers.
    proxy
        The operator's proxy, or `None`.
    ca_bundle
        The operator's CA file, or `None`.
    timeout
        Seconds for the whole request.

    Returns
    -------
    bytes
        The body.
    """
    if proxy is not None:
        return await _send(target, request, None, proxy, ca_bundle, (timeout, timeout))

    async def attempt(address: str, remaining: float, share: float) -> bytes:
        """Send to one address.

        Parameters
        ----------
        address
            The checked address.
        remaining
            Seconds left for each read and write.
        share
            Seconds for connecting to this address.

        Returns
        -------
        bytes
            The body.
        """
        return await _send(target, request, address, proxy, ca_bundle, (remaining, share))

    return await egress.each_address(target.addresses, timeout, attempt)


def _exchange(
    target: Target,
    request: _Request,
    proxy: SecretStr | None,
    ca_bundle: Path | None,
    timeout: float,
) -> object:
    """Send a request to a checked target and parse its JSON answer.

    Parameters
    ----------
    target
        What `check` returned.
    request
        The method, body and headers.
    proxy
        The operator's proxy, or `None`.
    ca_bundle
        The operator's CA file, which replaces the built-in store, or `None`.
    timeout
        Seconds for the whole request.

    Returns
    -------
    object
        The parsed JSON value.

    Raises
    ------
    FetchFailed
        With a fixed reason, never the text of the underlying exception.
    """
    reason = ""
    status: int | None = None
    body = b""
    with egress.http_libraries_silenced():
        try:
            body = egress.run_with_deadline(
                _send_any(target, request, proxy, ca_bundle, timeout),
                timeout,
                finish_streams=True,
            )
        except FetchFailed as failed:
            reason, status = failed.reason, failed.status
        except Exception as exc:  # any failure must become a fixed reason
            reason = egress.classify(exc)
    document: object = None
    if not reason:
        try:
            document = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            # RecursionError: a deeply nested array or object.
            reason = NOT_JSON
    if reason:
        raise FetchFailed(reason, status=status)
    return document


def fetch_target(
    target: Target,
    *,
    proxy: SecretStr | None = None,
    ca_bundle: Path | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> object:
    """Fetch and parse the document at a checked target.

    Parameters
    ----------
    target
        What `check` returned.
    proxy
        The operator's proxy, or `None`.
    ca_bundle
        The operator's CA file, which replaces the built-in store, or `None`.
    timeout
        Seconds for the whole request.

    Returns
    -------
    object
        The parsed JSON value.
    """
    return _exchange(target, _Request("GET"), proxy, ca_bundle, timeout)


def post_target(
    target: Target,
    form: Mapping[str, str],
    *,
    headers: Mapping[str, str],
    proxy: SecretStr | None = None,
    ca_bundle: Path | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> object:
    """Post a form to a checked target and parse its JSON answer.

    Parameters
    ----------
    target
        What `check` returned.
    form
        The fields, sent ``application/x-www-form-urlencoded``.
    headers
        Further headers, such as ``Authorization``.
    proxy
        The operator's proxy, or `None`.
    ca_bundle
        The operator's CA file, which replaces the built-in store, or `None`.
    timeout
        Seconds for the whole request.

    Returns
    -------
    object
        The parsed JSON value.
    """
    request = _Request("POST", urlencode(dict(form)).encode("ascii"), dict(headers))
    return _exchange(target, request, proxy, ca_bundle, timeout)


def fetch_json(
    url: str,
    *,
    allowed_hosts: Collection[str],
    private_hosts: Collection[str] = (),
    proxy: SecretStr | None = None,
    ca_bundle: Path | None = None,
    ports: Collection[int] = (443,),
) -> object:
    """Check a URL, then fetch and parse the JSON document there.

    Parameters
    ----------
    url
        An ``https`` URL on port 443, or on one of `ports`.
    allowed_hosts
        The lower-case host names the request may reach.
    private_hosts
        The lower-case host names the operator allowed to resolve to private addresses.
    proxy
        The operator's proxy, or `None`.
    ca_bundle
        The operator's CA file, which replaces the built-in store, or `None`.
    ports
        The ports the URL may name; 443 alone by default.

    Returns
    -------
    object
        The parsed JSON value.
    """
    target = check(url, allowed_hosts, private_hosts=private_hosts, ports=ports)
    return fetch_target(target, proxy=proxy, ca_bundle=ca_bundle, timeout=TIMEOUT_SECONDS)


def post_form_json(
    url: str,
    form: Mapping[str, str],
    *,
    headers: Mapping[str, str] | None = None,
    allowed_hosts: Collection[str],
    private_hosts: Collection[str] = (),
    proxy: SecretStr | None = None,
    ca_bundle: Path | None = None,
    ports: Collection[int] = (443,),
) -> object:
    """Check a URL, then post a form there and parse the JSON answer.

    The same checks as `fetch_json`: no redirect, 200 only, at most `MAX_BYTES`, one deadline.
    For an OIDC token endpoint, so the form and the headers carry the client secret: a
    `FetchFailed` never contains either.

    Parameters
    ----------
    url
        An ``https`` URL on port 443, or on one of `ports`.
    form
        The fields.
    headers
        Further headers, such as ``Authorization``.
    allowed_hosts
        The lower-case host names the request may reach.
    private_hosts
        The lower-case host names the operator allowed to resolve to private addresses.
    proxy
        The operator's proxy, or `None`.
    ca_bundle
        The operator's CA file, which replaces the built-in store, or `None`.
    ports
        The ports the URL may name; 443 alone by default.

    Returns
    -------
    object
        The parsed JSON value.
    """
    target = check(url, allowed_hosts, private_hosts=private_hosts, ports=ports)
    return post_target(
        target,
        form,
        headers=dict(headers or {}),
        proxy=proxy,
        ca_bundle=ca_bundle,
        timeout=TIMEOUT_SECONDS,
    )
