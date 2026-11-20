"""The shared key cache (`lakana_core.oidc.keys`) as the OIDC sign-in binds it.

CI's binding is covered, unchanged, by `test_ci_oidc_jwks.py`.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

import pytest
from ci_oidc_test_helpers import (
    DISCOVERY,
    ISSUER,
    ISSUER_HOST,
    JWKS_URI,
    FakeFetcher,
    discovery,
    jwk,
    rsa_key,
)
from pydantic import SecretStr

from lakana_core.net.https_json import FetchFailed
from lakana_core.oidc import keys
from lakana_core.oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.oidc.keys import FRESH_SECONDS, STALE_SECONDS, JwksCache, KeySource


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        """Tell the time.

        Returns
        -------
        float
            The current time.
        """
        return self.now


def source(
    allow_hosts: frozenset[str] = frozenset(),
    proxy: SecretStr | None = None,
    ca_bundle: Path | None = None,
) -> KeySource:
    """Build the sign-in's key source for `ISSUER`.

    Parameters
    ----------
    allow_hosts
        The hosts that may serve keys or resolve privately.
    proxy
        The proxy.
    ca_bundle
        The CA file.

    Returns
    -------
    KeySource
        The source.
    """
    return KeySource(
        issuers=frozenset({ISSUER}),
        allow_hosts=allow_hosts,
        proxy=proxy,
        ca_bundle=ca_bundle,
        label="OIDC sign-in",
        allow_hosts_variable="LAKANA_OIDC__ALLOW_HOSTS",
    )


def published() -> FakeFetcher:
    """Serve a discovery document with a token endpoint, and one key.

    Returns
    -------
    FakeFetcher
        The fetcher.
    """
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("main"), "k1"))
    fetcher.documents[DISCOVERY] = {**discovery(), "token_endpoint": f"{ISSUER}/token"}
    return fetcher


def test_discovery_is_cached_with_the_keys() -> None:
    fetcher, clock = published(), Clock()
    cache = JwksCache(source(), fetcher=fetcher, clock=clock)
    first = cache.discovery(ISSUER)
    assert cache.signing_key(ISSUER, "k1").kid == "k1"
    clock.now += FRESH_SECONDS - 1
    assert cache.discovery(ISSUER) == first
    assert first["token_endpoint"] == f"{ISSUER}/token"
    assert [url for url, _ in fetcher.calls] == [DISCOVERY, JWKS_URI]


def test_stale_discovery_serves_while_refreshes_fail() -> None:
    fetcher, clock = published(), Clock()
    cache = JwksCache(source(), fetcher=fetcher, clock=clock)
    first = cache.discovery(ISSUER)
    fetcher.documents[DISCOVERY] = FetchFailed("offline")
    clock.now += FRESH_SECONDS
    assert cache.discovery(ISSUER) == first
    clock.now += STALE_SECONDS
    with pytest.raises(IdTokenRefused) as info:
        cache.discovery(ISSUER)
    assert info.value.code is RefusalCode.KEYS_UNAVAILABLE


def test_discovery_of_another_issuer_fetches_nothing() -> None:
    fetcher = published()
    cache = JwksCache(source(), fetcher=fetcher)
    with pytest.raises(IdTokenRefused) as info:
        cache.discovery("https://evil.example")
    assert info.value.code is RefusalCode.ISSUER_NOT_ALLOWED
    assert fetcher.calls == []


def test_the_label_names_the_source_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    fetcher = FakeFetcher({DISCOVERY: FetchFailed("the host does not resolve")})
    cache = JwksCache(source(), fetcher=fetcher)
    with pytest.raises(IdTokenRefused):
        cache.signing_key(ISSUER, "k1")
    assert f"could not fetch the OIDC sign-in keys of {ISSUER}" in caplog.text
    assert "CI OIDC" not in caplog.text


def test_jwks_uri_refusal_names_the_sources_variable(caplog: pytest.LogCaptureFixture) -> None:
    fetcher = FakeFetcher({DISCOVERY: discovery(jwks_uri="https://keys.elsewhere.example/jwks")})
    cache = JwksCache(source(), fetcher=fetcher)
    with pytest.raises(IdTokenRefused):
        cache.signing_key(ISSUER, "k1")
    assert "a host of LAKANA_OIDC__ALLOW_HOSTS" in caplog.text
    assert "LAKANA_CI_OIDC" not in caplog.text


def test_the_refusal_names_no_setting() -> None:
    cache = JwksCache(source(), fetcher=FakeFetcher())
    with pytest.raises(IdTokenRefused) as info:
        cache.signing_key(ISSUER, "k1")
    assert "LAKANA_" not in info.value.message


def test_the_default_fetcher_uses_the_sources_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Collection[str], Collection[str], object, object, Collection[int]]] = []

    def fake(
        url: str,
        *,
        allowed_hosts: Collection[str],
        private_hosts: Collection[str],
        proxy: object,
        ca_bundle: object,
        ports: Collection[int],
    ) -> object:
        seen.append((url, allowed_hosts, private_hosts, proxy, ca_bundle, ports))
        raise FetchFailed("offline")

    monkeypatch.setattr(keys, "fetch_json", fake)
    proxy = SecretStr("http://proxy.corp.test:3128")
    cache = JwksCache(
        source(
            allow_hosts=frozenset({"idp.corp.example"}),
            proxy=proxy,
            ca_bundle=Path("/certs/ca.pem"),
        )
    )
    with pytest.raises(IdTokenRefused):
        cache.discovery(ISSUER)
    url, allowed, private, used_proxy, bundle, ports = seen[0]
    assert (url, set(allowed), set(private)) == (DISCOVERY, {ISSUER_HOST}, {"idp.corp.example"})
    assert used_proxy is proxy and bundle == Path("/certs/ca.pem")
    assert set(ports) == {443}


def test_an_issuers_port_is_allowed_for_its_requests() -> None:
    with_port = KeySource(
        issuers=frozenset({"https://idp.example:8443/realms/x"}),
        allow_hosts=frozenset(),
        proxy=None,
        ca_bundle=None,
        label="OIDC sign-in",
        allow_hosts_variable="LAKANA_OIDC__ALLOW_HOSTS",
    )
    assert with_port.ports == frozenset({443, 8443})
    assert source().ports == frozenset({443})
