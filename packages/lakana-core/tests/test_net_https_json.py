"""The JSON fetcher reaches only public https addresses on port 443 and reads at most 64 KiB."""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from scripts.test_support.egress_helpers import Authority, ConnectProxy, Server
from scripts.test_support.ports import closed_port

from lakana_core.net import egress, https_json
from lakana_core.net.https_json import FetchFailed, Target, check, fetch_json

NAME = "keys.example.test"
PUBLIC = "93.184.216.34"


def resolves_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> list[tuple[str, int]]:
    """Make every name resolve to `addresses`, and record the lookups.

    Parameters
    ----------
    monkeypatch
        The pytest fixture.
    *addresses
        What the lookup answers.

    Returns
    -------
    list[tuple[str, int]]
        The lookups made so far, as (name, port).
    """
    lookups: list[tuple[str, int]] = []

    def fake(host: str, port: int) -> list[str]:
        lookups.append((host, port))
        return list(addresses)

    monkeypatch.setattr(https_json, "_resolve", fake)
    return lookups


def refusal(url: str, allowed: tuple[str, ...] = (NAME,)) -> str:
    """Return the reason `check` refuses `url` with.

    Parameters
    ----------
    url
        The URL.
    allowed
        The hosts the request may reach.

    Returns
    -------
    str
        The reason.
    """
    with pytest.raises(FetchFailed) as info:
        check(url, allowed)
    return info.value.reason


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    """Provide a throwaway certificate authority.

    Parameters
    ----------
    tmp_path
        The pytest fixture.

    Returns
    -------
    Authority
        The authority, its bundle in `tmp_path`.
    """
    return Authority(tmp_path)


@pytest.fixture
def tls(authority: Authority) -> Iterator[Server]:
    """Serve HTTPS on loopback with a certificate for `NAME`.

    Parameters
    ----------
    authority
        The CA that signs the certificate.

    Yields
    ------
    Server
        The running server.
    """
    with Server(authority.issue(NAME)) as server:
        yield server


def fetch_from(
    server: Server, ca: Path | None, *, timeout: float = 5, proxy: SecretStr | None = None
) -> object:
    """Fetch `/doc` from a loopback server, pinned as `check` would pin `NAME`.

    Parameters
    ----------
    server
        The server.
    ca
        The CA bundle, or `None` for the built-in store.
    timeout
        Seconds for the whole request.
    proxy
        The proxy, or `None`.

    Returns
    -------
    object
        The parsed document.
    """
    target = Target(host=NAME, port=server.port, target="/doc", addresses=("127.0.0.1",))
    return https_json.fetch_target(target, proxy=proxy, ca_bundle=ca, timeout=timeout)


def failure(server: Server, ca: Path | None, *, timeout: float = 5) -> str:
    """Return the reason a fetch from a loopback server fails with.

    Parameters
    ----------
    server
        The server.
    ca
        The CA bundle, or `None`.
    timeout
        Seconds for the whole request.

    Returns
    -------
    str
        The reason.
    """
    with pytest.raises(FetchFailed) as info:
        fetch_from(server, ca, timeout=timeout)
    error = info.value
    assert error.__cause__ is None and error.__context__ is None
    return error.reason


# -- the check -------------------------------------------------------------------------------


def test_a_public_https_name_is_pinned_to_its_checked_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups = resolves_to(monkeypatch, PUBLIC, "2606:4700::1111")
    target = check(f"https://{NAME.upper()}./.well-known/jwks?x=1", (NAME,))
    assert target == Target(
        host=NAME, port=443, target="/.well-known/jwks?x=1", addresses=(PUBLIC, "2606:4700::1111")
    )
    assert lookups == [(NAME, 443)]


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (f"http://{NAME}/x", https_json.NOT_HTTPS),
        (f"https://{NAME}:8443/x", https_json.NOT_443),
        (f"https://{NAME}:80/x", https_json.NOT_443),
        ("https://other.example.test/x", https_json.HOST_NOT_ALLOWED),
        (f"https://{PUBLIC}/x", https_json.HOST_NOT_ALLOWED),
        ("https://[2606:4700::1111]/x", https_json.HOST_NOT_ALLOWED),
        (f"https://user@{NAME}/x", https_json.INVALID),
        (f"https://{NAME}/a b", https_json.INVALID),
        ("ftp://x/", https_json.INVALID),
        ("https://127.1/x", https_json.INVALID),
    ],
)
def test_anything_but_an_allowed_https_name_on_443_is_refused_before_a_lookup(
    monkeypatch: pytest.MonkeyPatch, url: str, reason: str
) -> None:
    lookups = resolves_to(monkeypatch, PUBLIC)
    assert refusal(url) == reason
    assert lookups == []


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1%eth0",
        "fd00::1",
        "fec0::1",
        "feff:ffff::1",
        "::ffff:127.0.0.1",
        "64:ff9b::a00:1",
    ],
)
def test_a_name_that_resolves_to_an_internal_address_is_refused(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    resolves_to(monkeypatch, PUBLIC, address)
    assert refusal(f"https://{NAME}/x") == https_json.PRIVATE


@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",
        "172.16.0.1",
        "192.168.0.1",
        "100.64.0.1",
        "fd12:3456::1",
        "fec0::1",
        "64:ff9b:1::a01:203",
    ],
)
def test_an_operator_listed_host_may_resolve_to_a_private_address(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    resolves_to(monkeypatch, address, PUBLIC)
    target = check(f"https://{NAME}/x", (NAME,), private_hosts=(NAME,))
    assert target.addresses == (address, PUBLIC)
    assert refusal(f"https://{NAME}/x") == https_json.PRIVATE


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "169.254.1.1",
        "fe80::1%eth0",
        "fd00:ec2::254",
        "100.100.100.200",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::a9fe:a9fe",
        "64:ff9b:1::a9fe:a9fe",
        "64:ff9b:1:ffff::7f00:1",
    ],
)
def test_loopback_link_local_and_metadata_stay_refused_for_a_listed_host(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    resolves_to(monkeypatch, "10.1.2.3", address)
    with pytest.raises(FetchFailed) as info:
        check(f"https://{NAME}/x", (NAME,), private_hosts=(NAME,))
    assert info.value.reason == https_json.NEVER_REACHABLE


PROVIDER_SERVICE_ADDRESSES = [
    pytest.param("168.63.129.16", id="azure-wireserver"),
    pytest.param("::ffff:168.63.129.16", id="azure-wireserver-ipv4-mapped"),
    pytest.param("::168.63.129.16", id="azure-wireserver-ipv4-compatible"),
    pytest.param("64:ff9b::a83f:8110", id="azure-wireserver-nat64"),
    pytest.param("64:ff9b:1::a83f:8110", id="azure-wireserver-local-nat64"),
    pytest.param("2002:a83f:8110::1", id="azure-wireserver-6to4"),
    pytest.param("192.0.0.192", id="oracle-legacy-metadata"),
    pytest.param("::ffff:192.0.0.192", id="oracle-legacy-metadata-ipv4-mapped"),
    pytest.param("fd20:ce::254", id="gcp-ipv6-metadata"),
]
"""Cloud service addresses outside link-local that hand out a machine's settings (#543)."""


@pytest.mark.parametrize("address", PROVIDER_SERVICE_ADDRESSES)
def test_a_cloud_service_address_outside_link_local_is_never_reachable(address: str) -> None:
    parsed = ipaddress.ip_address(address)
    assert egress.is_never_reachable(parsed)
    assert egress.is_internal(parsed)


@pytest.mark.parametrize("address", PROVIDER_SERVICE_ADDRESSES)
def test_a_cloud_service_address_stays_refused_for_a_listed_host(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    resolves_to(monkeypatch, "10.1.2.3", address)
    with pytest.raises(FetchFailed) as info:
        check(f"https://{NAME}/x", (NAME,), private_hosts=(NAME,))
    assert info.value.reason == https_json.NEVER_REACHABLE


@pytest.mark.parametrize("address", PROVIDER_SERVICE_ADDRESSES)
def test_a_cloud_service_address_is_refused_for_an_unlisted_host(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    resolves_to(monkeypatch, PUBLIC, address)
    with pytest.raises(FetchFailed) as info:
        check(f"https://{NAME}/x", (NAME,))
    assert info.value.reason == https_json.PRIVATE


def test_the_private_exemption_is_for_the_listed_host_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, PUBLIC, "10.1.2.3")
    with pytest.raises(FetchFailed) as info:
        check(f"https://{NAME}/x", (NAME,), private_hosts=("other.example.test",))
    assert info.value.reason == https_json.PRIVATE


def test_a_listed_host_still_needs_https_on_443_and_the_allow_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "10.1.2.3")
    for url, reason in (
        (f"http://{NAME}/x", https_json.NOT_HTTPS),
        (f"https://{NAME}:8443/x", https_json.NOT_443),
    ):
        with pytest.raises(FetchFailed) as info:
            check(url, (NAME,), private_hosts=(NAME,))
        assert info.value.reason == reason
    with pytest.raises(FetchFailed) as info:
        check(f"https://{NAME}/x", (), private_hosts=(NAME,))
    assert info.value.reason == https_json.HOST_NOT_ALLOWED


def test_a_name_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch)
    assert refusal(f"https://{NAME}/x") == https_json.UNRESOLVED

    def gaierror(host: str, port: int) -> list[str]:
        raise socket.gaierror("no such name")

    monkeypatch.setattr(https_json, "_resolve", gaierror)
    assert refusal(f"https://{NAME}/x") == https_json.UNRESOLVED


def test_a_lookup_that_does_not_answer_in_time_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def block(host: str, port: int) -> list[str]:
        release.wait(10)
        return [PUBLIC]

    monkeypatch.setattr(https_json, "_resolve", block)
    monkeypatch.setattr(https_json, "RESOLVE_TIMEOUT", 0.2)
    try:
        assert refusal(f"https://{NAME}/x") == https_json.RESOLVE_TIMED_OUT
    finally:
        release.set()


def test_the_resolver_is_the_shared_one() -> None:
    assert https_json._resolve is egress.resolve


# -- the request -----------------------------------------------------------------------------


def test_a_pinned_get_returns_the_parsed_document(authority: Authority, tls: Server) -> None:
    tls.body = b'{"keys": []}'
    assert fetch_from(tls, authority.bundle) == {"keys": []}
    path, headers, _ = tls.requests[0]
    assert path == "/doc"
    assert headers["Host"] == f"{NAME}:{tls.port}"
    assert headers["Accept-Encoding"] == "identity"
    assert tls.sni == [NAME]


def test_the_certificate_must_match_the_name_and_the_built_in_store_is_replaced(
    authority: Authority, tmp_path: Path
) -> None:
    (tmp_path / "other").mkdir()
    other = Authority(tmp_path / "other")
    with Server(authority.issue("someone-else.test")) as wrong_name:
        assert failure(wrong_name, authority.bundle) == egress.TLS_ERROR
    with Server(authority.issue(NAME)) as server:
        assert failure(server, None) == egress.TLS_ERROR
        assert failure(server, other.bundle) == egress.TLS_ERROR


def test_a_redirect_is_not_followed(authority: Authority, tls: Server) -> None:
    tls.status = 302
    tls.location = "https://169.254.169.254/latest/meta-data/"
    assert failure(tls, authority.bundle) == https_json.REDIRECT
    assert len(tls.requests) == 1


def test_an_answer_other_than_200_is_refused(authority: Authority, tls: Server) -> None:
    tls.status = 404
    assert failure(tls, authority.bundle) == "the server answered 404"


def test_a_body_over_64_kib_is_refused_by_its_length(authority: Authority, tls: Server) -> None:
    tls.body = b'"' + b"a" * (64 * 1024) + b'"'
    assert failure(tls, authority.bundle) == https_json.TOO_LARGE


def test_a_body_over_64_kib_without_a_length_is_cut_off(authority: Authority, tls: Server) -> None:
    tls.body = b'"' + b"a" * (200 * 1024) + b'"'
    tls.send_length = False
    assert failure(tls, authority.bundle) == https_json.TOO_LARGE


def test_a_body_of_exactly_64_kib_is_read(authority: Authority, tls: Server) -> None:
    tls.body = b'"' + b"a" * (64 * 1024 - 2) + b'"'
    tls.send_length = False
    assert fetch_from(tls, authority.bundle) == "a" * (64 * 1024 - 2)


def test_an_encoded_body_is_refused(authority: Authority, tls: Server) -> None:
    tls.body = b"\x1f\x8b"
    tls.headers = {"Content-Encoding": "gzip"}
    assert failure(tls, authority.bundle) == https_json.ENCODED


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe", b""])
def test_a_body_that_is_not_json_is_refused(authority: Authority, tls: Server, body: bytes) -> None:
    tls.body = body
    assert failure(tls, authority.bundle) == https_json.NOT_JSON


def test_deep_nesting_parses_or_is_refused_but_never_raises_anything_else(
    authority: Authority, tls: Server
) -> None:
    # Python 3.11 raises RecursionError at a depth of about a thousand; 3.12 on parse deeper.
    tls.body = b"[" * 32000 + b"]" * 32000
    try:
        assert isinstance(fetch_from(tls, authority.bundle), list)
    except FetchFailed as failed:
        assert failed.reason == https_json.NOT_JSON


def test_a_server_that_does_not_answer_times_out() -> None:
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        target = Target(
            host=NAME, port=silent.getsockname()[1], target="/doc", addresses=("127.0.0.1",)
        )
        with pytest.raises(FetchFailed) as info:
            https_json.fetch_target(target, ca_bundle=None, timeout=0.3)
    assert info.value.reason == egress.TIMEOUT


def test_a_closed_port_gives_a_fixed_reason() -> None:
    target = Target(host=NAME, port=closed_port(), target="/doc", addresses=("127.0.0.1",))
    with pytest.raises(FetchFailed) as info:
        https_json.fetch_target(target, ca_bundle=None, timeout=5)
    assert info.value.reason == egress.REFUSED


def test_the_explicit_proxy_carries_the_request_by_name(authority: Authority, tls: Server) -> None:
    tls.body = b"[1]"
    with ConnectProxy(tls.port) as proxy:
        address = SecretStr(f"http://127.0.0.1:{proxy.port}")
        assert fetch_from(tls, authority.bundle, proxy=address) == [1]
    assert proxy.targets == [f"{NAME}:{tls.port}"]


def test_the_proxy_environment_is_ignored(
    monkeypatch: pytest.MonkeyPatch, authority: Authority, tls: Server
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{closed_port()}")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
    tls.body = b"{}"
    assert fetch_from(tls, authority.bundle) == {}


def test_the_http_libraries_stay_silent(
    authority: Authority, tls: Server, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    tls.body = b"{}"
    fetch_from(tls, authority.bundle)
    assert NAME not in caplog.text


# -- the whole call --------------------------------------------------------------------------


def test_fetch_json_checks_then_fetches_the_pinned_target(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, PUBLIC)
    seen: list[tuple[Target, object, object, float]] = []

    def fake(target: Target, *, proxy: object, ca_bundle: object, timeout: float) -> object:
        seen.append((target, proxy, ca_bundle, timeout))
        return {"ok": True}

    monkeypatch.setattr(https_json, "fetch_target", fake)
    bundle = Path("/certs/ca.pem")
    assert fetch_json(f"https://{NAME}/doc", allowed_hosts=(NAME,), ca_bundle=bundle) == {
        "ok": True
    }
    assert seen == [
        (Target(NAME, 443, "/doc", (PUBLIC,)), None, bundle, https_json.TIMEOUT_SECONDS)
    ]
    resolves_to(monkeypatch, "10.1.2.3")
    assert fetch_json(f"https://{NAME}/doc", allowed_hosts=(NAME,), private_hosts=(NAME,))
    assert seen[-1][0].addresses == ("10.1.2.3",)


def test_fetch_json_never_fetches_a_refused_url(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "10.0.0.1")
    monkeypatch.setattr(https_json, "fetch_target", pytest.fail)
    with pytest.raises(FetchFailed) as info:
        fetch_json(f"https://{NAME}/doc", allowed_hosts=(NAME,))
    assert info.value.reason == https_json.PRIVATE
