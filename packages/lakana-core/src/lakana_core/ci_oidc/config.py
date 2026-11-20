"""The operator settings of the CI OIDC verification (`LAKANA_CI_OIDC__*`).

Every value is checked when the settings are loaded, so a server with a wrong value does not
start, and the message names the variable and the fix. Messages never quote a value: the proxy
may carry a password.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import NoDecode

from lakana_core.config import ProxyAndCaBundleChecks
from lakana_core.net.egress import EgressRefused, is_ip_literal, parse_url
from lakana_core.oidc.id_token import SUPPORTED_ALGORITHMS

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
"""The issuer of GitHub Actions' ID tokens (the preset ``github``)."""

GITLAB_ISSUER = "https://gitlab.com"
"""The issuer of GitLab.com's CI ID tokens (the preset ``gitlab``)."""

PRESETS = {"github": GITHUB_ISSUER, "gitlab": GITLAB_ISSUER}

__all__ = ["GITHUB_ISSUER", "GITLAB_ISSUER", "PRESETS", "SUPPORTED_ALGORITHMS", "CiOidcConfig"]

_MAX_ISSUER = 255
_MAX_AUDIENCE = 255
_HOST_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_NO_SPACE = re.compile(r"^[\x21-\x7e]+$")

_ISSUER_MESSAGE = (
    "LAKANA_CI_OIDC__ISSUERS: each entry must be github, gitlab or the https URL of a "
    "self-hosted GitLab, such as https://gitlab.example.com, exactly as its tokens name it in "
    "iss: a host name, no port, credentials, query or trailing slash"
)
_AUDIENCE_REQUIRED = (
    "LAKANA_CI_OIDC__AUDIENCE is required when LAKANA_CI_OIDC__ISSUERS is set: set it to this "
    "server's public URL, such as https://lakana.example.com, and request the CI ID token for "
    "that audience"
)
_AUDIENCE_MESSAGE = (
    "LAKANA_CI_OIDC__AUDIENCE must be one word of at most 255 printable characters without "
    "spaces, such as https://lakana.example.com"
)
_SUPPORTED = ", ".join(SUPPORTED_ALGORITHMS)
_ALLOW_HOSTS_MESSAGE = (
    "LAKANA_CI_OIDC__ALLOW_HOSTS must be a comma-separated list of host names, such as "
    "keys.example.com; no *.domain pattern and no IP address"
)


def _as_list(value: object, variable: str) -> object:
    """Accept a list setting as a comma-separated text or a JSON list.

    Parameters
    ----------
    value
        What the environment or the caller gave.
    variable
        The variable's name, for the message.

    Returns
    -------
    object
        A list of stripped, non-empty texts for a text; anything else unchanged, for pydantic
        to check.

    Raises
    ------
    ValueError
        If a text that starts with ``[`` is not a JSON list of texts.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text.startswith("["):
        return [part.strip() for part in text.split(",") if part.strip()]
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(
            f"{variable} must be a comma-separated list or a JSON list of texts, "
            'such as github,gitlab or ["github", "gitlab"]'
        )
    return [item.strip() for item in parsed if item.strip()]


def _issuer(value: str) -> str:
    """Resolve a preset and check an issuer URL.

    Parameters
    ----------
    value
        ``github``, ``gitlab`` (in any letter case) or an https URL.

    Returns
    -------
    str
        The issuer URL, with its host in lower case.

    Raises
    ------
    ValueError
        If the entry is neither a preset nor an https URL with a host name, without port,
        credentials, query, fragment or trailing slash.
    """
    preset = PRESETS.get(value.lower())
    if preset is not None:
        return preset
    host = ""
    try:
        _, host, port, _ = parse_url(value, "")
    except EgressRefused:
        port = None
    parts = urlsplit(value)
    if (
        not host
        or parts.scheme != "https"
        or port is not None
        or ":" in parts.netloc
        or is_ip_literal(host)
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
        or value.endswith("/")
        or len(value) > _MAX_ISSUER
    ):
        raise ValueError(_ISSUER_MESSAGE)
    return f"https://{host}{parts.path}"


class CiOidcConfig(ProxyAndCaBundleChecks):
    """CI OIDC verification settings (`LAKANA_CI_OIDC__*`); read by the server.

    With no issuer (the default) the server accepts no CI ID token at all. The server fetches
    each issuer's discovery document and keys over ``https`` on port 443, from public addresses
    unless the host is in `LAKANA_CI_OIDC__ALLOW_HOSTS`, never following a redirect.

    Parameters
    ----------
    issuers
        The CI providers whose ID tokens the server accepts, as a comma-separated list or a JSON
        list: `github` (GitHub Actions), `gitlab` (GitLab.com) or the https URL of a self-hosted
        GitLab, exactly as its tokens name it (no trailing slash). Empty (the default) turns CI
        OIDC off.
    audience
        The audience the CI ID tokens must be issued for, usually this server's public URL, for
        example `https://lakana.example`; request the token for the same value in CI.
        Required when `LAKANA_CI_OIDC__ISSUERS` is set.
    algorithms
        The signature algorithms accepted, as a list; `RS256` (the default) is what GitHub and
        GitLab use. Only `RS256`, `RS384`, `RS512`, `PS256`, `PS384` and `PS512` are supported;
        `none` and the HMAC algorithms (`HS256` and the like) are refused.
    max_token_age_seconds
        The oldest ID token accepted, counted from its issue time (default 900, 15 minutes).
    allow_hosts
        Host names, comma-separated, exact names only. A listed host may serve an issuer's keys
        when its discovery document names another host than the issuer's own, and a listed host
        may resolve to a private address (10.x, 172.16.x to 172.31.x, 192.168.x, fd00::/8);
        every other host must be public. A self-hosted GitLab on an internal network therefore
        needs its URL in `LAKANA_CI_OIDC__ISSUERS` and its host name here. Loopback, link-local
        and cloud metadata addresses are refused even for a listed host. List only names whose
        DNS you control.
    proxy
        URL of the proxy that the discovery and key requests go through, for example
        `http://user:password@host:3128`. The server checks the destination name, but the proxy
        looks it up again, so the proxy must refuse internal addresses itself. A password is
        hidden in logs and error messages.
    ca_bundle
        Absolute path of a PEM file of certificate authorities that the discovery and key
        requests trust; it replaces the built-in store and never turns verification off.
    token_ttl_seconds
        How long the Lakana token a CI job receives for its ID token stays valid, in seconds
        (default 3600, one hour; at most 21600, six hours). The token allows one deploy or dry
        run, so it only has to outlive the job's wait for its deploy to be queued.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    issuers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    audience: str | None = None
    algorithms: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["RS256"])
    max_token_age_seconds: int = Field(default=900, ge=60, le=3600)
    allow_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    proxy: SecretStr | None = None
    ca_bundle: Path | None = None
    token_ttl_seconds: int = Field(default=3600, ge=60, le=21600)

    @property
    def enabled(self) -> bool:
        """Whether any issuer is configured.

        Returns
        -------
        bool
            `True` when CI ID tokens may be accepted at all.
        """
        return bool(self.issuers)

    @field_validator("issuers", mode="before")
    @classmethod
    def _issuers_as_list(cls, value: object) -> object:
        """Accept the issuers as text or a list.

        Parameters
        ----------
        value
            The raw value.

        Returns
        -------
        object
            See `_as_list`.
        """
        return _as_list(value, "LAKANA_CI_OIDC__ISSUERS")

    @field_validator("issuers")
    @classmethod
    def _issuers_are_urls(cls, value: list[str]) -> list[str]:
        """Resolve the presets, check the URLs and drop repeated entries.

        Parameters
        ----------
        value
            The entries.

        Returns
        -------
        list of str
            The issuer URLs, each once, in the given order.
        """
        return list(dict.fromkeys(_issuer(entry.strip()) for entry in value))

    @field_validator("algorithms", mode="before")
    @classmethod
    def _algorithms_as_list(cls, value: object) -> object:
        """Accept the algorithms as text or a list.

        Parameters
        ----------
        value
            The raw value.

        Returns
        -------
        object
            See `_as_list`.
        """
        return _as_list(value, "LAKANA_CI_OIDC__ALGORITHMS")

    @field_validator("algorithms")
    @classmethod
    def _algorithms_are_asymmetric(cls, value: list[str]) -> list[str]:
        """Refuse ``none``, the HMAC algorithms and anything unsupported.

        Parameters
        ----------
        value
            The algorithms.

        Returns
        -------
        list of str
            `value`, unchanged.

        Raises
        ------
        ValueError
            With a message that says why the algorithm is refused and what to use instead.
        """
        if not value:
            raise ValueError(
                "LAKANA_CI_OIDC__ALGORITHMS must name at least one algorithm, such as RS256"
            )
        for name in value:
            if name.lower() == "none":
                raise ValueError(
                    "LAKANA_CI_OIDC__ALGORITHMS must not contain none: an unsigned token proves "
                    "nothing; use RS256"
                )
            if name.upper().startswith("HS"):
                raise ValueError(
                    "LAKANA_CI_OIDC__ALGORITHMS must not contain an HMAC algorithm (HS256 and the "
                    "like): anyone who knows the issuer's public key could sign a token with it; "
                    "use RS256"
                )
            if name not in SUPPORTED_ALGORITHMS:
                raise ValueError(
                    f"LAKANA_CI_OIDC__ALGORITHMS: only {_SUPPORTED} are supported; "
                    "GitHub and GitLab use RS256"
                )
        return value

    @field_validator("allow_hosts", mode="before")
    @classmethod
    def _allow_hosts_as_list(cls, value: object) -> object:
        """Accept the allowed hosts as text or a list.

        Parameters
        ----------
        value
            The raw value.

        Returns
        -------
        object
            See `_as_list`.
        """
        return _as_list(value, "LAKANA_CI_OIDC__ALLOW_HOSTS")

    @field_validator("allow_hosts")
    @classmethod
    def _allow_hosts_are_names(cls, value: list[str]) -> list[str]:
        """Require plain host names and normalise them.

        Parameters
        ----------
        value
            The entries.

        Returns
        -------
        list of str
            The names in lower case, without a trailing dot.

        Raises
        ------
        ValueError
            If an entry is not a host name, or its last label is a number (an IP address).
        """
        names = [entry.strip().rstrip(".").lower() for entry in value]
        if not all(
            _HOST_NAME.match(name) and not name.rsplit(".", 1)[-1].isdigit() for name in names
        ):
            raise ValueError(_ALLOW_HOSTS_MESSAGE)
        return names

    @model_validator(mode="after")
    def _audience_when_enabled(self) -> Self:
        """Require a well-formed audience once an issuer is set.

        Returns
        -------
        CiOidcConfig
            This configuration.

        Raises
        ------
        ValueError
            If an issuer is set and the audience is missing, empty, has a space or a control
            character, or is longer than 255 characters.
        """
        if self.audience is not None and (
            not _NO_SPACE.match(self.audience) or len(self.audience) > _MAX_AUDIENCE
        ):
            raise ValueError(_AUDIENCE_MESSAGE)
        if self.issuers and self.audience is None:
            raise ValueError(_AUDIENCE_REQUIRED)
        return self
