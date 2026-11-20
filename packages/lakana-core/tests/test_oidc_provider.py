"""The OIDC sign-in's provider client (`lakana_core.oidc.provider`), over a fake fetcher."""

from __future__ import annotations

from datetime import UTC, datetime

import jwt
import pytest
from ci_oidc_test_helpers import DISCOVERY, ISSUER, JWKS_URI, FakeFetcher, jwk, rsa_key

from lakana_core.net.https_json import FetchFailed
from lakana_core.oidc import provider as provider_module
from lakana_core.oidc.config import OidcSignInConfig
from lakana_core.oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.oidc.keys import JwksCache
from lakana_core.oidc.provider import (
    ID_TOKEN_MAX_AGE_SECONDS,
    OidcProvider,
    ProviderUnavailable,
    key_source,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
T = int(NOW.timestamp())
CLIENT = "lakana-web"
NONCE = "n-0123456789"


def document(**changes: object) -> dict[str, object]:
    """Build the provider's discovery document.

    Parameters
    ----------
    **changes
        Members to set; `None` removes one.

    Returns
    -------
    dict[str, object]
        The document.
    """
    base: dict[str, object] = {
        "issuer": ISSUER,
        "jwks_uri": JWKS_URI,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
    }
    base.update(changes)
    return {name: value for name, value in base.items() if value is not None}


def provider(fetcher: FakeFetcher | None = None, **settings: object) -> OidcProvider:
    """Build a provider for `ISSUER` over a fake fetcher.

    Parameters
    ----------
    fetcher
        The fetcher; by default one that serves `document()` and the main key.
    **settings
        Further `OidcSignInConfig` fields.

    Returns
    -------
    OidcProvider
        The provider.
    """
    if fetcher is None:
        fetcher = FakeFetcher()
        fetcher.publish(jwk(rsa_key("main"), "k1", alg="RS256"))
        fetcher.documents[DISCOVERY] = document()
    config = OidcSignInConfig.model_validate(
        {"issuer": ISSUER, "client_id": CLIENT, "client_secret": "s3cr3t-sentinel", **settings}
    )
    return OidcProvider(config, keys=JwksCache(key_source(config), fetcher=fetcher))


def serving(**changes: object) -> FakeFetcher:
    """Serve a changed discovery document and the main key.

    Parameters
    ----------
    **changes
        Members of the document to set; `None` removes one.

    Returns
    -------
    FakeFetcher
        The fetcher.
    """
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("main"), "k1", alg="RS256"))
    fetcher.documents[DISCOVERY] = document(**changes)
    return fetcher


def unavailable(fetcher: FakeFetcher, **settings: object) -> str:
    """Return the reason the metadata are refused with.

    Parameters
    ----------
    fetcher
        The fetcher.
    **settings
        Further `OidcSignInConfig` fields.

    Returns
    -------
    str
        The reason.
    """
    with pytest.raises(ProviderUnavailable) as info:
        provider(fetcher, **settings).metadata()
    assert "s3cr3t-sentinel" not in str(info.value)
    return info.value.reason


def test_metadata_from_discovery() -> None:
    meta = provider().metadata()
    assert meta.issuer == ISSUER
    assert meta.authorization_endpoint == f"{ISSUER}/authorize"
    assert meta.token_endpoint == f"{ISSUER}/token"
    assert meta.token_auth == "client_secret_basic"


def test_algorithms_from_discovery_intersected() -> None:
    fetcher = serving(id_token_signing_alg_values_supported=["HS256", "RS256", "ES256"])
    assert provider(fetcher).metadata().algorithms == frozenset({"RS256"})


def test_algorithms_default_rs256() -> None:
    assert provider().metadata().algorithms == frozenset({"RS256"})
    fetcher = serving(id_token_signing_alg_values_supported="RS256")
    assert provider(fetcher).metadata().algorithms == frozenset({"RS256"})


def test_no_supported_algorithm_is_unavailable() -> None:
    fetcher = serving(id_token_signing_alg_values_supported=["ES256", "none"])
    assert "RS256" in unavailable(fetcher)


def test_token_endpoint_on_a_foreign_host_is_refused() -> None:
    fetcher = serving(token_endpoint="https://evil.example/token")
    assert "LAKANA_OIDC__ALLOW_HOSTS" in unavailable(fetcher)
    allowed = serving(token_endpoint="https://login.corp.example/token")
    meta = provider(allowed, allow_hosts="login.corp.example").metadata()
    assert meta.token_endpoint == "https://login.corp.example/token"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://token.actions.githubusercontent.com/authorize",
        "https://user@token.actions.githubusercontent.com/authorize",
        "javascript:alert(1)",
        "https://[::1/x",
        7,
        None,
    ],
)
def test_authorization_endpoint_must_be_https(endpoint: object) -> None:
    fetcher = serving(authorization_endpoint=endpoint)
    assert "authorization_endpoint" in unavailable(fetcher)


def test_token_auth_prefers_basic() -> None:
    both = serving(
        token_endpoint_auth_methods_supported=["client_secret_post", "client_secret_basic"]
    )
    assert provider(both).metadata().token_auth == "client_secret_basic"
    post = serving(token_endpoint_auth_methods_supported=["client_secret_post"])
    assert provider(post).metadata().token_auth == "client_secret_post"
    neither = serving(token_endpoint_auth_methods_supported=["private_key_jwt"])
    assert "client_secret_basic" in unavailable(neither)
    wrong = serving(token_endpoint_auth_methods_supported="client_secret_post")
    assert provider(wrong).metadata().token_auth == "client_secret_basic"


def test_an_unreachable_provider_is_unavailable() -> None:
    fetcher = FakeFetcher({DISCOVERY: FetchFailed("the host does not resolve")})
    assert "could not be fetched" in unavailable(fetcher)


def test_discovery_naming_another_issuer_is_unavailable() -> None:
    fetcher = serving(issuer="https://other.example")
    assert "could not be fetched" in unavailable(fetcher)


def sign(**changes: object) -> str:
    """Sign a sign-in ID token with the main key.

    Parameters
    ----------
    **changes
        Claims to set; `None` removes one.

    Returns
    -------
    str
        The token.
    """
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT,
        "sub": "248289761001",
        "nonce": NONCE,
        "iat": T - 5,
        "exp": T + 300,
    }
    claims.update(changes)
    payload = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(payload, rsa_key("main"), algorithm="RS256", headers={"kid": "k1"})


def refused(token: str) -> RefusalCode:
    """Return the code a token is refused with.

    Parameters
    ----------
    token
        The token.

    Returns
    -------
    RefusalCode
        The code.
    """
    with pytest.raises(IdTokenRefused) as info:
        provider().verify(token, nonce=NONCE, now=NOW)
    return info.value.code


def test_a_sign_in_token_verifies() -> None:
    verified = provider().verify(sign(), nonce=NONCE, now=NOW)
    assert (verified.issuer, verified.subject) == (ISSUER, "248289761001")


def test_sign_in_rules_refuse_an_access_token() -> None:
    assert refused(sign(typ="Bearer", aud="account")) is RefusalCode.NOT_AN_ID_TOKEN
    assert refused(sign(aud="account")) is RefusalCode.AUDIENCE_MISMATCH


def test_sign_in_rules() -> None:
    rules = provider().rules(NONCE)
    assert rules.audience == rules.authorized_party == CLIENT
    assert rules.nonce == NONCE
    assert rules.max_age_seconds == ID_TOKEN_MAX_AGE_SECONDS == 600
    assert not rules.require_jti
    assert rules.refuse_typ_other_than_id


def test_a_long_subject_is_refused() -> None:
    assert refused(sign(sub="s" * 256)) is RefusalCode.CLAIM_MISSING


def test_an_unreachable_provider_refuses_the_token_as_unavailable() -> None:
    fetcher = FakeFetcher({DISCOVERY: FetchFailed("the host does not resolve")})
    with pytest.raises(ProviderUnavailable):
        provider(fetcher).verify(sign(), nonce=NONCE, now=NOW)


def test_keys_lost_after_the_metadata_make_the_provider_unavailable() -> None:
    fetcher = serving()
    # Two reads load the metadata, a third finds them fresh again; then a day and more passes.
    times = iter([0.0, 0.0, 0.0])

    def clock() -> float:
        return next(times, 10.0**7)

    config = OidcSignInConfig.model_validate(
        {"issuer": ISSUER, "client_id": CLIENT, "client_secret": "s3cr3t-sentinel"}
    )
    keys = JwksCache(key_source(config), fetcher=fetcher, clock=clock)
    client = OidcProvider(config, keys=keys)
    assert client.metadata().issuer == ISSUER
    fetcher.documents[DISCOVERY] = FetchFailed("offline")
    with pytest.raises(ProviderUnavailable):
        client.verify(sign(), nonce=NONCE, now=NOW)


def test_the_provider_keeps_its_settings() -> None:
    assert provider().config.client_id == CLIENT
    assert provider().allowed_hosts == frozenset({"token.actions.githubusercontent.com"})


def test_a_provider_needs_an_issuer() -> None:
    with pytest.raises(ValueError, match="LAKANA_OIDC__ISSUER"):
        OidcProvider(OidcSignInConfig())


def test_the_default_poster_uses_the_operator_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, object]] = []

    def fake(url: str, form: object, **kwargs: object) -> object:
        seen.append({"url": url, "form": form, **kwargs})
        return {"id_token": "t"}

    monkeypatch.setattr(provider_module, "post_form_json", fake)
    config = OidcSignInConfig.model_validate(
        {
            "issuer": "https://idp.example:8443/realms/x",
            "client_id": CLIENT,
            "client_secret": "s3cr3t-sentinel",
            "allow_hosts": "idp.corp.example",
            "proxy": "http://proxy.example:3128",
        }
    )
    answer = OidcProvider(config).post(
        "https://idp.example:8443/token", {"code": "c"}, {"Authorization": "Basic x"}
    )
    assert answer == {"id_token": "t"}
    assert seen == [
        {
            "url": "https://idp.example:8443/token",
            "form": {"code": "c"},
            "headers": {"Authorization": "Basic x"},
            "allowed_hosts": frozenset({"idp.example", "idp.corp.example"}),
            "private_hosts": frozenset({"idp.corp.example"}),
            "proxy": config.proxy,
            "ca_bundle": None,
            "ports": frozenset({443, 8443}),
        }
    ]


def test_a_poster_gets_the_allowed_hosts() -> None:
    calls: list[tuple[str, object, object, object]] = []

    def poster(url: str, form: object, headers: object, hosts: object) -> object:
        calls.append((url, form, headers, hosts))
        return {}

    config = OidcSignInConfig.model_validate(
        {"issuer": ISSUER, "client_id": CLIENT, "client_secret": "s3cr3t-sentinel"}
    )
    OidcProvider(config, poster=poster).post(f"{ISSUER}/token", {"code": "c"}, {})
    assert calls == [
        (f"{ISSUER}/token", {"code": "c"}, {}, frozenset({"token.actions.githubusercontent.com"}))
    ]
