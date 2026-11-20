"""The OIDC sign-in settings (`LAKANA_OIDC__*`, `lakana_core.oidc.config`)."""

from __future__ import annotations

import pydantic
import pytest
from pydantic import SecretStr

from lakana_core.oidc.config import OidcSignInConfig, checked_issuer

ISSUER = "https://idp.example.com/realms/lakana"


def config(**fields: object) -> OidcSignInConfig:
    """Build a sign-in configuration with an issuer, a client id and a secret.

    Parameters
    ----------
    **fields
        Fields to set or change.

    Returns
    -------
    OidcSignInConfig
        The configuration.
    """
    base: dict[str, object] = {
        "issuer": ISSUER,
        "client_id": "lakana",
        "client_secret": "s3cr3t-sentinel",
    }
    return OidcSignInConfig.model_validate({**base, **fields})


def test_off_without_an_issuer() -> None:
    off = OidcSignInConfig(issuer=None, display_name=None)
    assert not off.enabled
    assert off.scopes == ["openid", "profile", "email"]
    assert config().enabled


@pytest.mark.parametrize(
    "issuer",
    [
        "http://idp.example.com",
        "https://idp.example.com/",
        "https://idp.example.com?x=1",
        "https://idp.example.com#top",
        "https://user:pw@idp.example.com",
        "https://10.0.0.1",
        "https://[::1]",
        "https://idp.example.com:0",
        "https://idp.example.com:99999",
        "https://idp.example.com:http",
        "idp.example.com",
        "https://" + "a" * 250 + ".example",
        "github",
    ],
)
def test_issuer_is_checked_like_ci(issuer: str) -> None:
    with pytest.raises(pydantic.ValidationError, match="LAKANA_OIDC__ISSUER") as info:
        config(issuer=issuer)
    assert issuer not in str(info.value)


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        (ISSUER, ISSUER),
        ("https://IdP.Example.com/realms/Lakana", "https://idp.example.com/realms/Lakana"),
        ("https://idp.example.com:8443/realms/x", "https://idp.example.com:8443/realms/x"),
    ],
)
def test_issuer_may_name_a_port(given: str, kept: str) -> None:
    assert config(issuer=given).issuer == kept


def test_checked_issuer_refuses_a_port_unless_allowed() -> None:
    with pytest.raises(ValueError, match=r"^no port$"):
        checked_issuer("https://idp.example.com:8443", "no port")
    assert (
        checked_issuer("https://idp.example.com:8443", "x", allow_port=True)
        == "https://idp.example.com:8443"
    )


def test_scopes_must_contain_openid() -> None:
    with pytest.raises(pydantic.ValidationError, match="LAKANA_OIDC__SCOPES"):
        config(scopes="profile email")
    assert config(scopes="openid email").scopes == ["openid", "email"]
    assert config(scopes='["openid", "groups"]').scopes == ["openid", "groups"]
    assert config(scopes="openid,email").scopes == ["openid", "email"]


@pytest.mark.parametrize("scope", ["open id", "a\x00b", 'say"hi', "x" * 129])
def test_scopes_are_plain_words(scope: str) -> None:
    with pytest.raises(pydantic.ValidationError, match="LAKANA_OIDC__SCOPES"):
        config(scopes=["openid", scope])


def test_shown_name_defaults_to_the_issuer_host() -> None:
    assert config().shown_name == "idp.example.com"
    assert config(display_name="Contoso").shown_name == "Contoso"
    assert OidcSignInConfig().shown_name == ""


@pytest.mark.parametrize("name", ["", "  ", "x" * 81, "a\nb"])
def test_display_name_is_one_short_line(name: str) -> None:
    with pytest.raises(pydantic.ValidationError, match="LAKANA_OIDC__DISPLAY_NAME"):
        config(display_name=name)


def test_allow_hosts_are_names() -> None:
    assert config(allow_hosts="IdP.Corp.Example.,keys.example").allow_hosts == [
        "idp.corp.example",
        "keys.example",
    ]
    with pytest.raises(pydantic.ValidationError, match="LAKANA_OIDC__ALLOW_HOSTS"):
        config(allow_hosts="10.0.0.1")


@pytest.mark.parametrize("client_id", ["", "a b", "x" * 256])
def test_client_id_is_one_word(client_id: str) -> None:
    with pytest.raises(pydantic.ValidationError, match="LAKANA_OIDC__CLIENT_ID"):
        config(client_id=client_id)


def test_secrets_are_hidden_in_errors() -> None:
    with pytest.raises(pydantic.ValidationError) as info:
        config(proxy="ftp://user:pw-sentinel@proxy.example", scopes="email")
    assert "pw-sentinel" not in str(info.value)
    settings = config(proxy="http://user:pw-sentinel@proxy.example:3128")
    assert isinstance(settings.client_secret, SecretStr)
    shown = repr(settings) + str(settings)
    assert "s3cr3t-sentinel" not in shown
    assert "pw-sentinel" not in shown
