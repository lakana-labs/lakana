"""`LAKANA_CI_OIDC__*`: the issuers, audience and algorithms are checked when the server starts."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakana_core.ci_oidc.config import GITHUB_ISSUER, GITLAB_ISSUER, CiOidcConfig

AUDIENCE = "https://lakana.example.com"


class _Settings(BaseSettings):
    """The variables as the server reads them.

    Parameters
    ----------
    ci_oidc
        `LAKANA_CI_OIDC__*`.
    """

    model_config = SettingsConfigDict(env_prefix="LAKANA_", env_nested_delimiter="__")

    ci_oidc: CiOidcConfig = CiOidcConfig()


def refusal(**fields: object) -> str:
    """Return the message a configuration is refused with.

    Parameters
    ----------
    **fields
        The fields of the configuration.

    Returns
    -------
    str
        The text of the validation error.
    """
    with pytest.raises(ValidationError) as info:
        CiOidcConfig.model_validate(fields)
    return str(info.value)


def test_empty_issuers_turn_the_feature_off_and_need_nothing_else() -> None:
    config = CiOidcConfig()
    assert config.issuers == []
    assert not config.enabled
    assert config.audience is None
    assert config.algorithms == ["RS256"]
    assert config.max_token_age_seconds == 900


def test_presets_name_the_public_issuers() -> None:
    config = CiOidcConfig(issuers=["github", "GitLab"], audience=AUDIENCE)
    assert config.issuers == [GITHUB_ISSUER, GITLAB_ISSUER]
    assert GITHUB_ISSUER == "https://token.actions.githubusercontent.com"
    assert GITLAB_ISSUER == "https://gitlab.com"
    assert config.enabled


def test_a_self_hosted_gitlab_is_an_https_url_and_duplicates_collapse() -> None:
    config = CiOidcConfig(
        issuers=[
            "https://gitlab.example.com",
            "https://Git.Example.com/gitlab",
            "gitlab",
            "gitlab",
        ],
        audience=AUDIENCE,
    )
    assert config.issuers == [
        "https://gitlab.example.com",
        "https://git.example.com/gitlab",
        GITLAB_ISSUER,
    ]


def test_the_environment_takes_a_comma_separated_list_or_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKANA_CI_OIDC__ISSUERS", "github, https://gitlab.example.com")
    monkeypatch.setenv("LAKANA_CI_OIDC__AUDIENCE", AUDIENCE)
    monkeypatch.setenv("LAKANA_CI_OIDC__ALGORITHMS", '["RS256", "PS256"]')
    monkeypatch.setenv("LAKANA_CI_OIDC__ALLOW_HOSTS", "keys.example.com")
    monkeypatch.setenv("LAKANA_CI_OIDC__MAX_TOKEN_AGE_SECONDS", "600")
    monkeypatch.setenv("LAKANA_CI_OIDC__PROXY", "http://proxy.corp.test:3128")
    monkeypatch.setenv("LAKANA_CI_OIDC__CA_BUNDLE", "/certs/ca.pem")
    config = _Settings().ci_oidc
    assert config.issuers == [GITHUB_ISSUER, "https://gitlab.example.com"]
    assert config.audience == AUDIENCE
    assert config.algorithms == ["RS256", "PS256"]
    assert config.allow_hosts == ["keys.example.com"]
    assert config.max_token_age_seconds == 600
    assert config.proxy is not None
    assert config.proxy.get_secret_value() == "http://proxy.corp.test:3128"
    assert config.ca_bundle == Path("/certs/ca.pem")


def test_an_empty_variable_is_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKANA_CI_OIDC__ISSUERS", " ")
    assert _Settings().ci_oidc.issuers == []


@pytest.mark.parametrize(
    "value",
    [
        "http://gitlab.example.com",
        "https://gitlab.example.com/",
        "https://gitlab.example.com:8443",
        "https://user:pw@gitlab.example.com",
        "https://gitlab.example.com?x=1",
        "https://gitlab.example.com#x",
        "https://10.0.0.1",
        "https://[::1]",
        "https://127.1",
        "gitlab.example.com",
        "githubb",
        "https://gitlab.example.com/a b",
        "https://",
        "https://gitlab.example.com:x",
        "https://" + "a" * 250 + ".example.com",
    ],
)
def test_an_issuer_that_is_not_a_preset_or_a_plain_https_url_is_refused(value: str) -> None:
    message = refusal(issuers=[value], audience=AUDIENCE)
    assert "LAKANA_CI_OIDC__ISSUERS" in message
    assert "github, gitlab or the https URL" in message


def test_a_refused_issuer_is_not_quoted() -> None:
    message = refusal(issuers=["https://user:SECRETPW@gitlab.example.com"], audience=AUDIENCE)
    assert "SECRETPW" not in message


def test_an_issuer_list_must_hold_text_only() -> None:
    assert "LAKANA_CI_OIDC__ISSUERS" in refusal(issuers='["github", 1]', audience=AUDIENCE)
    assert "LAKANA_CI_OIDC__ISSUERS" in refusal(issuers='{"a": 1}', audience=AUDIENCE)
    assert "LAKANA_CI_OIDC__ISSUERS" in refusal(issuers="[not json", audience=AUDIENCE)


def test_the_audience_is_required_once_an_issuer_is_set() -> None:
    message = refusal(issuers=["github"])
    assert "LAKANA_CI_OIDC__AUDIENCE is required when LAKANA_CI_OIDC__ISSUERS is set" in message
    assert "https://lakana.example.com" in message


@pytest.mark.parametrize("value", ["", "  ", "with space", "a\nb", "x" * 256])
def test_an_audience_that_is_empty_spaced_or_long_is_refused(value: str) -> None:
    assert "LAKANA_CI_OIDC__AUDIENCE" in refusal(issuers=["github"], audience=value)


@pytest.mark.parametrize("value", ["none", "None", "NONE"])
def test_the_none_algorithm_is_refused(value: str) -> None:
    message = refusal(algorithms=[value])
    assert "LAKANA_CI_OIDC__ALGORITHMS must not contain none" in message


@pytest.mark.parametrize("value", ["HS256", "HS384", "hs512"])
def test_an_hmac_algorithm_is_refused(value: str) -> None:
    message = refusal(algorithms=["RS256", value])
    assert "LAKANA_CI_OIDC__ALGORITHMS must not contain an HMAC algorithm" in message


@pytest.mark.parametrize("value", ["ES256", "EdDSA", "rs256", "XYZ"])
def test_an_unsupported_algorithm_names_the_supported_ones(value: str) -> None:
    message = refusal(algorithms=[value])
    assert "RS256, RS384, RS512, PS256, PS384, PS512" in message


def test_at_least_one_algorithm_is_needed() -> None:
    assert "at least one algorithm" in refusal(algorithms=[])


@pytest.mark.parametrize("value", ["*.example.com", "10.0.0.1", "a..b", "x.1", "with space"])
def test_an_allowed_host_is_a_plain_host_name(value: str) -> None:
    assert "LAKANA_CI_OIDC__ALLOW_HOSTS" in refusal(allow_hosts=[value])


def test_allowed_hosts_are_lower_cased_without_a_trailing_dot() -> None:
    assert CiOidcConfig(allow_hosts=["Keys.Example.COM."]).allow_hosts == ["keys.example.com"]


@pytest.mark.parametrize("value", [0, 59, 3601])
def test_the_maximum_token_age_is_bounded(value: int) -> None:
    refusal(max_token_age_seconds=value)


def test_a_bad_proxy_or_ca_bundle_is_refused_without_quoting_it() -> None:
    message = refusal(proxy="ftp://user:SECRETPW@proxy.test")
    assert "SECRETPW" not in message
    assert "proxy" in message
    assert "absolute" in refusal(ca_bundle="relative/ca.pem")


@pytest.mark.parametrize("blank", ["", "  "])
def test_an_empty_setting_counts_as_not_set(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    """Compose passes an empty value for a `${VAR}` that `.env` leaves empty (#853)."""
    for name in (
        "ISSUERS",
        "AUDIENCE",
        "ALGORITHMS",
        "MAX_TOKEN_AGE_SECONDS",
        "ALLOW_HOSTS",
        "PROXY",
        "CA_BUNDLE",
        "TOKEN_TTL_SECONDS",
    ):
        monkeypatch.setenv(f"LAKANA_CI_OIDC__{name}", blank)
    assert _Settings().ci_oidc == CiOidcConfig()
    monkeypatch.setenv("LAKANA_CI_OIDC__ISSUERS", "github")
    with pytest.raises(ValidationError, match="LAKANA_CI_OIDC__AUDIENCE is required"):
        _Settings()
