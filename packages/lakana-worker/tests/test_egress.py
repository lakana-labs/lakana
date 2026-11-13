"""The webhook client refuses internal addresses and never keeps or quotes the URL."""

from __future__ import annotations

import logging
import logging.config
import re
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from scripts.test_support.egress_helpers import Authority, ConnectProxy, Server
from scripts.test_support.ports import closed_port

from lakana_core.config import WebhookConfig
from lakana_worker import egress
from lakana_worker.egress import (
    DeliveryResult,
    EgressRefused,
    check_destination,
    post,
)

NAME = "hooks.example.test"
SECRET_URL = f"https://{NAME}/services/T0SECRETPATH/B0/XSECRETKEY?token=SECRETQUERY"


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

    monkeypatch.setattr(egress, "_resolve", fake)
    return lookups


def refusal(url: str, config: WebhookConfig | None = None) -> str:
    """Return the reason `check_destination` refuses `url` with.

    Parameters
    ----------
    url
        The URL.
    config
        The operator settings.

    Returns
    -------
    str
        The reason.
    """
    with pytest.raises(EgressRefused) as info:
        check_destination(url, config or WebhookConfig())
    return info.value.reason


REFUSED = [
    "10.0.0.1",
    "172.16.5.4",
    "192.168.1.1",
    "127.0.0.1",
    "127.1.2.3",
    "169.254.169.254",
    "100.64.0.1",
    "100.127.255.254",
    "0.0.0.0",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    "192.0.0.8",
    "198.18.0.1",
    "::1",
    "::",
    "fe80::1",
    "fe80::1%eth0",
    "fd00::1",
    "fd00:ec2::254",
    "fc00::1",
    "fec0::1",  # site-local, deprecated: the standard library calls it global
    "feff:ffff::1",
    "ff02::1",
    "ff0e::1",
    "::ffff:10.0.0.1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::ffff:100.64.0.1",
    "::10.0.0.1",
    "2002:0a00:0001::1",
    "2002:7f00:0001::",
    "2002:0808:0808::1",  # 6to4 is deprecated and refused whole, even around a public IPv4
    "2002:a9fe:a9fe::1",
    "64:ff9b::a00:1",
    "64:ff9b::7f00:1",
    "2001:db8::1",
    "100::1",
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    "168.63.129.16",  # Azure's WireServer: a public-range address (#543)
    "::ffff:168.63.129.16",
    "64:ff9b::a83f:8110",
    "192.0.0.192",
    "fd20:ce::254",
]


@pytest.mark.parametrize("address", REFUSED)
def test_every_internal_range_is_refused(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    resolves_to(monkeypatch, address)
    assert refusal(f"https://{NAME}/hook") == egress.PRIVATE


@pytest.mark.parametrize("address", REFUSED)
def test_an_ip_literal_in_the_url_is_checked_like_a_resolved_one(address: str) -> None:
    bare = address.split("%")[0]
    if ":" in bare:
        assert refusal(f"https://[{bare}]/hook") in (egress.PRIVATE, egress.INVALID)
    else:
        assert refusal(f"https://{bare}/hook") == egress.PRIVATE


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f.1", "017700000001", "10.1"])
def test_shorthand_ip_spellings_are_refused_before_any_lookup(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    lookups = resolves_to(monkeypatch, "93.184.216.34")
    assert refusal(f"https://{host}/hook") == egress.INVALID
    assert lookups == []


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8"]
)
def test_public_addresses_are_accepted(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    resolves_to(monkeypatch, address)
    assert check_destination(f"https://{NAME}/hook", WebhookConfig()).host == NAME


def test_a_name_with_a_public_and_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "93.184.216.34", "10.0.0.5")
    assert refusal(f"https://{NAME}/hook") == egress.PRIVATE
    resolves_to(monkeypatch, "10.0.0.5", "93.184.216.34")
    assert refusal(f"https://{NAME}/hook") == egress.PRIVATE


def test_a_name_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch)
    assert refusal(f"https://{NAME}/hook") == egress.UNRESOLVED

    def gaierror(host: str, port: int) -> list[str]:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(egress, "_resolve", gaierror)
    assert refusal(f"https://{NAME}/hook") == egress.UNRESOLVED


def test_http_is_refused_and_other_ports_are_refused_before_a_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups = resolves_to(monkeypatch, "93.184.216.34")
    assert refusal(f"http://{NAME}/hook") == egress.NOT_HTTPS
    assert refusal(f"https://{NAME}:8443/hook") == egress.NOT_443
    assert refusal(f"https://{NAME}:80/hook") == egress.NOT_443
    assert lookups == []
    assert check_destination(f"https://{NAME}:443/hook", WebhookConfig()).port == 443


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "ftp://example.test/x",
        "https://",
        "https:///path",
        "https://user:pw@hooks.example.test/x",
        "https://hooks.example.test:notaport/x",
        "https://hooks.example.test:0/x",
        "https://[::1/x",
        "https://hooks.example.test/a b",
        "https://hooks.example.test/a\r\nHost: evil",
        "https://ho_ok$.example.test/x",
        "https://a..b.example.test/x",
        "https://" + "a" * 300 + ".test/x",
    ],
)
def test_a_malformed_url_is_refused_with_a_fixed_reason(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    resolves_to(monkeypatch, "93.184.216.34")
    assert refusal(url) in {egress.INVALID, egress.NOT_HTTPS}


def test_the_destination_is_pinned_to_the_checked_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "2606:4700::1111", "93.184.216.34")
    destination = check_destination(f"https://{NAME.upper()}./a/b?q=1#frag", WebhookConfig())
    assert destination.host == NAME
    # The system's order is kept (it already ranks by the routes the host has).
    assert destination.addresses == ("2606:4700::1111", "93.184.216.34")
    assert (destination.scheme, destination.port, destination.target) == ("https", 443, "/a/b?q=1")


def test_the_destination_never_shows_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "93.184.216.34")
    destination = check_destination(
        SECRET_URL, WebhookConfig(proxy="http://u:p%40ss@proxy.test:3128")
    )
    text = repr(destination) + str(destination)
    assert "SECRET" not in text and NAME not in text and "p%40ss" not in text


# -- the allow-list ----------------------------------------------------------------------------


def allowed(hosts: str) -> WebhookConfig:
    return WebhookConfig(allow_hosts=hosts)


def test_an_allowed_host_may_use_an_internal_address_http_and_another_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "10.0.0.5")
    destination = check_destination("http://chat.corp.test:8080/hook", allowed("chat.corp.test"))
    assert (destination.scheme, destination.port, destination.addresses[0]) == (
        "http",
        8080,
        "10.0.0.5",
    )


def test_a_wildcard_matches_subdomains_only_without_regard_to_case_or_a_trailing_dot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "10.0.0.5")
    config = allowed("*.Corp.Test, other.test")
    for url in (
        "https://a.corp.test/x",
        "https://A.B.CORP.TEST./x",
        "https://OTHER.test./x",
    ):
        assert check_destination(url, config).addresses[0] == "10.0.0.5"
    for url in ("https://corp.test/x", "https://evilcorp.test/x", "https://corp.test.evil.test/x"):
        assert refusal(url, config) == egress.PRIVATE


def test_an_unlisted_host_gets_no_exemption_from_a_listed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "10.0.0.5")
    assert refusal("http://other.test/x", allowed("chat.corp.test")) == egress.NOT_HTTPS


NEVER_REACHABLE = [
    "127.0.0.1",
    "127.1.2.3",
    "::1",
    "0.0.0.0",
    "::",
    "169.254.169.254",
    "169.254.1.1",
    "100.100.100.200",
    "fd00:ec2::254",
    "fe80::1%eth0",
    "224.0.0.1",
    "240.0.0.1",
    "ff02::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::127.0.0.1",
    "64:ff9b::7f00:1",
    "64:ff9b::a9fe:a9fe",
    "64:ff9b:1::a9fe:a9fe",  # the local-use NAT64 prefix (RFC 8215)
    "64:ff9b:1::7f00:1",
    "64:ff9b:1:ffff::a9fe:a9fe",
    "2002:a9fe:a9fe::1",
    "168.63.129.16",  # Azure's WireServer (#543)
    "::ffff:168.63.129.16",
    "::168.63.129.16",
    "64:ff9b::a83f:8110",
    "2002:a83f:8110::1",
    "192.0.0.192",  # Oracle Compute Classic and Cloud@Customer metadata
    "::ffff:192.0.0.192",
    "fd20:ce::254",  # Google Cloud's IPv6 metadata address
]


@pytest.mark.parametrize("address", NEVER_REACHABLE)
@pytest.mark.parametrize("url", ["https://chat.corp.test/hook", "http://chat.corp.test:8080/x"])
def test_an_allowed_host_never_reaches_loopback_link_local_or_metadata(
    monkeypatch: pytest.MonkeyPatch, address: str, url: str
) -> None:
    resolves_to(monkeypatch, "10.0.0.5", address)
    assert refusal(url, allowed("chat.corp.test")) == egress.NEVER_REACHABLE


@pytest.mark.parametrize(
    "address", ["10.0.0.5", "192.168.1.1", "100.64.0.1", "fd12::1", "fec0::1", "64:ff9b:1::a00:5"]
)
def test_an_allowed_host_keeps_its_private_address_exemption(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    resolves_to(monkeypatch, address, "93.184.216.34")
    destination = check_destination("https://chat.corp.test/hook", allowed("chat.corp.test"))
    assert destination.addresses == (address, "93.184.216.34")


def test_the_test_hook_admits_loopback_but_not_metadata_for_an_allowed_host(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    config = allowed("chat.corp.test")
    assert check_destination("http://chat.corp.test:8080/x", config).addresses == ("127.0.0.1",)
    resolves_to(monkeypatch, "127.0.0.1", "169.254.169.254")
    assert refusal("http://chat.corp.test:8080/x", config) == egress.NEVER_REACHABLE


def test_a_listed_localhost_may_reach_loopback_and_nothing_else_never_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listing ``localhost`` itself asks for loopback; no other listed name gets it."""
    config = allowed("localhost, chat.corp.test")
    resolves_to(monkeypatch, "127.0.0.1", "::1")
    for url in ("http://localhost:8080/hook", "http://LOCALHOST.:8080/hook"):
        assert check_destination(url, config).addresses == ("127.0.0.1", "::1")
    assert refusal("http://chat.corp.test:8080/hook", config) == egress.NEVER_REACHABLE
    assert refusal("http://localhost:8080/hook") == egress.NOT_HTTPS
    resolves_to(monkeypatch, "127.0.0.1", "169.254.169.254")
    assert refusal("http://localhost:8080/hook", config) == egress.NEVER_REACHABLE
    resolves_to(monkeypatch, "0.0.0.0")
    assert refusal("http://localhost:8080/hook", config) == egress.NEVER_REACHABLE


def test_the_never_reachable_reason_names_the_setting() -> None:
    assert "LAKANA_WEBHOOK__ALLOW_HOSTS" in egress.NEVER_REACHABLE
    assert "metadata" in egress.NEVER_REACHABLE


@pytest.mark.parametrize(
    "value", ["", " , ", "a b.test", "*.", "*", "a/b", "http://x.test", "x.test:80"]
)
def test_a_bad_allow_list_is_refused_at_load_without_quoting_it(value: str) -> None:
    with pytest.raises(ValueError) as info:
        WebhookConfig(allow_hosts=value)
    # Short values like `*` also occur in the message's own example.
    assert len(value) < 3 or value not in str(info.value)


def test_the_allow_list_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from lakana_core.config import Settings

    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite://")
    monkeypatch.setenv("LAKANA_WEBHOOK__ALLOW_HOSTS", "a.test,*.b.test")
    monkeypatch.setenv("LAKANA_WEBHOOK__PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("LAKANA_WEBHOOK__CA_BUNDLE", "/certs/ca.pem")
    webhook = Settings().webhook
    assert webhook.allowed_hosts() == ("a.test", "*.b.test")
    assert (
        webhook.proxy is not None and webhook.proxy.get_secret_value() == "http://proxy.test:3128"
    )
    assert webhook.ca_bundle == Path("/certs/ca.pem")


# -- the test hook -----------------------------------------------------------------------------


def test_loopback_is_refused_unless_the_test_fixture_opted_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    assert egress._TEST_ALLOW_LOOPBACK is False
    assert refusal(f"https://{NAME}/x") == egress.PRIVATE
    assert refusal(f"https://{NAME}:9999/x") == egress.NOT_443


def test_the_hook_admits_loopback_only(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    assert check_destination(f"https://{NAME}:9999/x", WebhookConfig()).addresses[0] == "127.0.0.1"
    resolves_to(monkeypatch, "10.0.0.1")
    assert refusal(f"https://{NAME}:9999/x") == egress.PRIVATE
    resolves_to(monkeypatch, "93.184.216.34")
    assert refusal(f"https://{NAME}:9999/x") == egress.NOT_443


def test_nothing_in_the_source_tree_sets_the_test_hook() -> None:
    root = Path(__file__).resolve().parents[3] / "packages"
    mentions: list[tuple[str, str]] = []
    for path in root.glob("*/src/**/*.py"):
        for line in path.read_text().splitlines():
            if "_TEST_ALLOW_LOOPBACK" in line:
                mentions.append((path.name, line.strip()))
    assert mentions, "the guard found no source files: fix its path"
    assert all(name == "egress.py" for name, _ in mentions), mentions
    assignments = [
        line
        for _, line in mentions
        if re.search(r"_TEST_ALLOW_LOOPBACK\b[^=\n]*(?<![=!<>])=(?!=)", line)
    ]
    assert [a.replace(" ", "") for a in assignments] == ["_TEST_ALLOW_LOOPBACK:bool=False"], (
        assignments
    )
    assert not any("global" in line for _, line in mentions)
    assert not any("setattr" in line for _, line in mentions)


# -- sending -----------------------------------------------------------------------------------


@pytest.fixture
def authority(tmp_path: Path) -> Authority:
    return Authority(tmp_path)


@pytest.fixture
def tls(authority: Authority) -> Iterator[Server]:
    with Server(authority.issue(NAME)) as server:
        yield server


def send(
    tls_port: int,
    config: WebhookConfig,
    *,
    path: str = "/hook",
    body: bytes = b'{"a": 1}',
    timeout: float = 5,
) -> DeliveryResult:
    destination = check_destination(f"https://{NAME}:{tls_port}{path}", config)
    return post(
        destination, body, {"Content-Type": "application/json", "X-Test": "1"}, timeout=timeout
    )


def test_a_pinned_request_to_a_name_with_a_valid_certificate_succeeds(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority, tls: Server
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    result = send(tls.port, WebhookConfig(ca_bundle=authority.bundle), path="/p?x=1")
    assert result == DeliveryResult(
        ok=True, permanent=False, reason="the server answered 200", status=200
    )
    path, headers, body = tls.requests[0]
    assert (path, body) == ("/p?x=1", b'{"a": 1}')
    assert headers["Host"] == f"{NAME}:{tls.port}"
    assert headers["X-Test"] == "1"
    assert tls.sni == [NAME]


def test_the_certificate_is_verified_against_the_name_not_the_address(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    config = WebhookConfig(ca_bundle=authority.bundle)
    with Server(authority.issue("other.example.test")) as wrong_name:
        result = send(wrong_name.port, config)
        assert (result.ok, result.permanent, result.reason) == (False, False, egress.TLS_ERROR)
        assert wrong_name.requests == []
    # A certificate for the IP only does not make the name valid either.
    with Server(authority.issue("whatever", ip="127.0.0.1")) as ip_only:
        assert send(ip_only.port, config).reason == egress.TLS_ERROR
        assert ip_only.requests == []


def test_a_certificate_from_an_unknown_ca_fails_and_the_bundle_replaces_trust(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority, tls: Server
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    assert send(tls.port, WebhookConfig()).reason == egress.TLS_ERROR
    (authority.directory / "other").mkdir()
    other = Authority(authority.directory / "other")
    assert send(tls.port, WebhookConfig(ca_bundle=other.bundle)).reason == egress.TLS_ERROR
    assert send(tls.port, WebhookConfig(ca_bundle=authority.bundle)).ok


def test_dns_rebinding_cannot_move_the_connection(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority, tls: Server
) -> None:
    answers = iter([["127.0.0.1"], ["10.9.9.9"], ["10.9.9.9"]])
    monkeypatch.setattr(egress, "_resolve", lambda host, port: next(answers))
    destination = check_destination(
        f"https://{NAME}:{tls.port}/hook", WebhookConfig(ca_bundle=authority.bundle)
    )

    def lookup(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the client resolved a name while sending")

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(egress, "_resolve", lookup)
    assert post(destination, b"{}", {}, timeout=5).ok
    assert len(tls.requests) == 1


@pytest.mark.parametrize(
    ("status", "ok", "permanent"),
    [
        (200, True, False),
        (204, True, False),
        (404, False, True),
        (400, False, True),
        (401, False, True),
        (408, False, False),
        (429, False, False),
        (500, False, False),
        (503, False, False),
        (301, False, True),
        (302, False, True),
        (307, False, True),
        (308, False, True),
    ],
)
def test_the_status_decides_success_permanence_or_retry(
    monkeypatch: pytest.MonkeyPatch,
    allow_loopback: None,
    authority: Authority,
    tls: Server,
    status: int,
    ok: bool,
    permanent: bool,
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    tls.status = status
    result = send(tls.port, WebhookConfig(ca_bundle=authority.bundle))
    assert (result.ok, result.permanent, result.status) == (ok, permanent, status)
    assert str(status) in result.reason


def test_a_redirect_is_not_followed(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority, tls: Server
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    with Server() as target:
        tls.status = 302
        tls.location = f"http://127.0.0.1:{target.port}/stolen"
        result = send(tls.port, WebhookConfig(ca_bundle=authority.bundle))
    assert (result.ok, result.permanent, result.status) == (False, True, 302)
    assert target.requests == []
    assert "stolen" not in result.reason and "127.0.0.1" not in result.reason


def test_an_allowed_host_may_be_plain_http_on_any_port(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    with Server() as plain:
        destination = check_destination(
            f"http://plain.example.test:{plain.port}/hook", allowed("plain.example.test")
        )
        assert post(destination, b"{}", {}, timeout=5).ok
        assert plain.requests[0][1]["Host"] == f"plain.example.test:{plain.port}"


def test_the_proxy_environment_is_ignored(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority, tls: Server
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    with ConnectProxy(tls.port) as proxy:
        for variable in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "HTTP_PROXY"):
            monkeypatch.setenv(variable, f"http://127.0.0.1:{proxy.port}")
        monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
        assert send(tls.port, WebhookConfig(ca_bundle=authority.bundle)).ok
        assert proxy.targets == []


def test_the_explicit_proxy_is_used_with_connect_and_the_destination_is_still_checked(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority, tls: Server
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    with ConnectProxy(tls.port) as proxy:
        config = WebhookConfig(ca_bundle=authority.bundle, proxy=f"http://127.0.0.1:{proxy.port}")
        result = send(tls.port, config)
        assert result.ok
        assert proxy.targets == [f"{NAME}:{tls.port}"]
        assert tls.sni == [NAME]
        resolves_to(monkeypatch, "10.0.0.5")
        assert refusal(f"https://{NAME}:{tls.port}/x", config) == egress.PRIVATE
    assert len(tls.requests) == 1


def test_a_dead_proxy_gives_a_fixed_reason_without_its_address(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    config = WebhookConfig(proxy=f"http://user:s3cr3t@127.0.0.1:{closed_port()}")
    result = send(closed_port(), config)
    assert not result.ok and not result.permanent
    assert "s3cr3t" not in repr(result) and "127.0.0.1" not in repr(result)


# -- reasons never carry the URL ---------------------------------------------------------------


def assert_clean(result: DeliveryResult) -> None:
    text = repr(result)
    for leak in ("SECRET", NAME, "127.0.0.1", "token", "services", "https://", "Errno"):
        assert leak not in text, (leak, text)


def test_a_timeout_reason_is_fixed(monkeypatch: pytest.MonkeyPatch, allow_loopback: None) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    held: list[socket.socket] = []
    threading.Thread(target=lambda: held.append(listener.accept()[0]), daemon=True).start()
    try:
        destination = check_destination(
            SECRET_URL.replace(NAME, f"{NAME}:{listener.getsockname()[1]}"), WebhookConfig()
        )
        result = post(destination, b"{}", {}, timeout=0.5)
    finally:
        listener.close()
        for conn in held:
            conn.close()
    assert (result.ok, result.permanent, result.reason, result.status) == (
        False,
        False,
        egress.TIMEOUT,
        None,
    )
    assert_clean(result)


def test_the_timeout_is_a_total_deadline_not_per_read(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    stop = threading.Event()

    def dribble() -> None:
        conn, _ = listener.accept()
        with conn:
            while not stop.is_set():
                try:
                    conn.sendall(b"x")
                except OSError:
                    return
                stop.wait(0.1)

    threading.Thread(target=dribble, daemon=True).start()
    try:
        destination = check_destination(
            f"https://{NAME}:{listener.getsockname()[1]}/h",
            WebhookConfig(ca_bundle=authority.bundle),
        )
        import time

        started = time.monotonic()
        result = post(destination, b"{}", {}, timeout=0.6)
        assert time.monotonic() - started < 3
    finally:
        stop.set()
        listener.close()
    assert result.reason in {egress.TIMEOUT, egress.TLS_ERROR, egress.CONNECTION_LOST}


def test_connection_refused_reason_is_fixed(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    destination = check_destination(
        SECRET_URL.replace(NAME, f"{NAME}:{closed_port()}"), WebhookConfig()
    )
    result = post(destination, b"{}", {}, timeout=5)
    assert (result.ok, result.permanent, result.reason) == (False, False, egress.REFUSED)
    assert_clean(result)


def test_tls_error_reason_is_fixed(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, tls: Server
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    destination = check_destination(SECRET_URL.replace(NAME, f"{NAME}:{tls.port}"), WebhookConfig())
    result = post(destination, b"{}", {}, timeout=5)
    assert result.reason == egress.TLS_ERROR
    assert_clean(result)


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (httpx.ReadError(f"boom {SECRET_URL}"), egress.CONNECTION_LOST),
        (httpx.RemoteProtocolError(f"boom {SECRET_URL}"), egress.CONNECTION_LOST),
        (httpx.ReadTimeout(f"boom {SECRET_URL}"), egress.TIMEOUT),
        (httpx.ProxyError(f"boom {SECRET_URL}"), egress.PROXY_ERROR),
        (httpx.InvalidURL(f"boom {SECRET_URL}"), egress.FAILED),
        (httpx.UnsupportedProtocol(f"boom {SECRET_URL}"), egress.FAILED),
        (httpx.ConnectError(f"boom {SECRET_URL}"), egress.NO_CONNECTION),
        (RuntimeError(f"boom {SECRET_URL}"), egress.FAILED),
        (UnicodeError(f"boom {SECRET_URL}"), egress.FAILED),
        (OSError(f"boom {SECRET_URL}"), egress.FAILED),
    ],
)
def test_any_exception_text_is_reduced_to_a_fixed_reason(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, reason: str
) -> None:
    resolves_to(monkeypatch, "93.184.216.34")
    destination = check_destination(SECRET_URL, WebhookConfig())

    def explode(self: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(httpx.AsyncClient, "send", explode)
    result = post(destination, b"{}", {}, timeout=5)
    assert (result.ok, result.permanent, result.reason) == (False, False, reason)
    assert_clean(result)


def test_every_refusal_reason_is_fixed_and_without_a_chained_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "10.0.0.1")
    for url in (
        SECRET_URL,
        SECRET_URL.replace("https", "http"),
        SECRET_URL.replace(NAME, f"{NAME}:8443"),
        SECRET_URL.replace(NAME, "user:pw@" + NAME),
        SECRET_URL.replace("/services", "/ser vices"),
        SECRET_URL.replace(NAME, "[" + NAME),
    ):
        with pytest.raises(EgressRefused) as info:
            check_destination(url, WebhookConfig())
        error = info.value
        assert error.__cause__ is None and error.__context__ is None
        shown = repr(error) + str(error) + error.reason + repr(error.args)
        for leak in ("SECRET", NAME, "services", "pw", "10.0.0.1"):
            assert leak not in shown, (leak, shown)


def test_the_resolver_returns_distinct_addresses_in_the_order_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(
        host: str, port: int, *, type: int
    ) -> list[tuple[int, int, int, str, tuple[object, ...]]]:
        # The trailing dot makes the name absolute, so no search domain is tried first.
        assert (host, port, type) == (NAME + ".", 443, socket.SOCK_STREAM)
        return [
            (socket.AF_INET, type, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET6, type, 6, "", ("2606:4700::1111", port, 0, 0)),
            (socket.AF_INET, type, 6, "", ("93.184.216.34", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert egress._resolve(NAME, 443) == ["93.184.216.34", "2606:4700::1111"]


def test_a_number_in_the_last_label_of_an_allow_list_entry_names_the_next_step() -> None:
    with pytest.raises(ValueError, match="host name"):
        WebhookConfig(allow_hosts="chat.corp.test,*.0.0.1")


# -- logs --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 404, None])
def test_the_http_libraries_never_log_the_url(
    monkeypatch: pytest.MonkeyPatch,
    allow_loopback: None,
    authority: Authority,
    tls: Server,
    caplog: pytest.LogCaptureFixture,
    status: int | None,
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    tls.status = status or 200
    # No CA bundle for the TLS failure: the certificate is unknown.
    config = WebhookConfig(ca_bundle=authority.bundle) if status else WebhookConfig()
    result = send(tls.port, config, path="/services/SECRETPATH?token=SECRETQUERY")
    assert result.ok is (status == 200)
    assert (result.reason == egress.TLS_ERROR) is (status is None)
    for leak in ("SECRET", NAME, "services", "127.0.0.1"):
        assert leak not in caplog.text, leak


def test_a_logging_configuration_that_keeps_existing_loggers_does_not_bring_the_urls_back(
    monkeypatch: pytest.MonkeyPatch,
    allow_loopback: None,
    authority: Authority,
    tls: Server,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    caplog.set_level(logging.DEBUG)
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "loggers": {
                "httpx": {"level": "DEBUG", "propagate": True},
                "httpcore": {"level": "DEBUG", "propagate": True},
            },
        }
    )
    logging.getLogger("httpx").disabled = False
    logging.getLogger("httpcore.connection").disabled = False
    result = send(tls.port, WebhookConfig(ca_bundle=authority.bundle), path="/SECRETPATH")
    assert result.ok
    for leak in ("SECRET", NAME, "127.0.0.1"):
        assert leak not in caplog.text, leak


def test_the_name_lookups_have_spare_threads() -> None:
    assert egress._DNS._max_workers == 16


# -- resolving ---------------------------------------------------------------------------------


def blocking_resolver(monkeypatch: pytest.MonkeyPatch, only: str | None = None) -> threading.Event:
    release = threading.Event()

    def block(host: str, port: int) -> list[str]:
        if only is None or host.startswith(only):
            release.wait(10)
        return ["93.184.216.34"]

    monkeypatch.setattr(egress, "_resolve", block)
    monkeypatch.setattr(egress, "RESOLVE_TIMEOUT", 0.2)
    return release


def test_a_lookup_that_does_not_answer_in_time_gives_a_fixed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = blocking_resolver(monkeypatch)
    try:
        assert refusal(f"https://{NAME}/x") == egress.RESOLVE_TIMED_OUT
    finally:
        release.set()


def test_a_proxy_name_that_does_not_answer_in_time_gives_a_fixed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = blocking_resolver(monkeypatch, only="proxy.corp")
    try:
        config = WebhookConfig(proxy="http://proxy.corp.test:3128")
        assert refusal(f"https://{NAME}/x", config) == egress.PROXY_RESOLVE_TIMED_OUT
    finally:
        release.set()


# -- trying the addresses in turn --------------------------------------------------------------


def test_the_next_address_is_tried_when_one_does_not_connect(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority, tls: Server
) -> None:
    # Nothing listens on ::1 at this port, so the first address is refused.
    resolves_to(monkeypatch, "::1", "127.0.0.1")
    result = send(tls.port, WebhookConfig(ca_bundle=authority.bundle))
    assert result.ok
    assert len(tls.requests) == 1


def test_a_failed_certificate_ends_the_attempt_without_trying_the_next_address(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, tls: Server
) -> None:
    resolves_to(monkeypatch, "127.0.0.1", "::1")
    # The CA is unknown; had the second address been tried, the reason would be `refused`.
    assert send(tls.port, WebhookConfig()).reason == egress.TLS_ERROR


def test_when_no_address_connects_the_last_reason_is_given(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None
) -> None:
    resolves_to(monkeypatch, "::1", "127.0.0.1")
    result = send(closed_port(), WebhookConfig())
    assert (result.ok, result.permanent, result.reason) == (False, False, egress.REFUSED)


# -- the proxy ---------------------------------------------------------------------------------


def test_the_ca_bundle_also_covers_the_proxys_own_tls(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority, tls: Server
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    certificate = authority.issue("proxy", ip="127.0.0.1")
    with ConnectProxy(tls.port, certificate) as proxy:
        config = WebhookConfig(ca_bundle=authority.bundle, proxy=f"https://127.0.0.1:{proxy.port}")
        assert send(tls.port, config).ok
        assert proxy.targets == [f"{NAME}:{tls.port}"]


def test_an_upper_case_https_proxy_scheme_still_gets_the_ca_bundle(
    monkeypatch: pytest.MonkeyPatch, allow_loopback: None, authority: Authority, tls: Server
) -> None:
    resolves_to(monkeypatch, "127.0.0.1")
    certificate = authority.issue("proxy", ip="127.0.0.1")
    with ConnectProxy(tls.port, certificate) as proxy:
        config = WebhookConfig(ca_bundle=authority.bundle, proxy=f"HTTPS://127.0.0.1:{proxy.port}")
        assert send(tls.port, config).ok


def test_plain_http_to_an_allowed_host_goes_through_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "10.0.0.5")
    with Server() as plain, ConnectProxy(plain.port) as proxy:
        url = f"http://plain.example.test:{plain.port}/hook"
        destination = check_destination(
            url,
            WebhookConfig(allow_hosts="plain.example.test", proxy=f"http://127.0.0.1:{proxy.port}"),
        )
        assert post(destination, b"{}", {}, timeout=5).ok
        assert proxy.targets == [url]
        assert plain.requests[0][1]["Host"] == f"plain.example.test:{plain.port}"


def test_http_library_logging_outside_a_send_is_untouched(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    logging.getLogger("httpx").info("HTTP Request: GET http://testserver/ok")
    logging.getLogger("httpcore.connection").info("child record")
    assert "http://testserver/ok" in caplog.text
    assert "child record" in caplog.text
