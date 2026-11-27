"""The operator settings of the OIDC sign-in (`LAKANA_OIDC__*`), and checks CI OIDC shares.

Every value is checked when the settings are loaded, so a server with a wrong value does not
start, and the message names the variable and the fix. Messages never quote a value: the proxy
may carry a password, and the client secret is one.
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

DEFAULT_SCOPES = ("openid", "profile", "email")
"""The scopes asked for when `LAKANA_OIDC__SCOPES` is unset."""

_MAX_ISSUER = 255
_MAX_CLIENT_ID = 255
_MAX_SCOPE = 128
_MAX_DISPLAY_NAME = 80
_HOST_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
_NO_SPACE = re.compile(r"^[\x21-\x7e]+$")
# RFC 6749, 3.3: a scope token is printable ASCII without space, '"' and '\'.
_SCOPE = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]+$")

_ISSUER_MESSAGE = (
    "LAKANA_OIDC__ISSUER must be the https URL of the identity provider exactly as its ID tokens "
    "name it in iss, such as https://login.example.com/realms/lakana: a host name and an "
    "optional port, no credentials, query or trailing slash, at most 255 characters"
)
_CLIENT_ID_MESSAGE = (
    "LAKANA_OIDC__CLIENT_ID must be the client id the identity provider gave Lakana: one word "
    "of at most 255 printable characters without spaces"
)
_SCOPES_MESSAGE = (
    "LAKANA_OIDC__SCOPES must be a list of scopes that contains openid, such as "
    "openid profile email; each scope one word without quotes or backslashes"
)
_DISPLAY_NAME_MESSAGE = (
    "LAKANA_OIDC__DISPLAY_NAME must be one line of 1 to 80 characters, such as Contoso, shown "
    "on the sign-in button"
)
_ALLOW_HOSTS_MESSAGE = (
    "LAKANA_OIDC__ALLOW_HOSTS must be a comma-separated list of host names, such as "
    "login.example.com; no *.domain pattern and no IP address"
)


def without_empty_values(data: object) -> object:
    """Leave out every setting whose value is empty, so that it counts as not set.

    Compose passes a variable that ``.env`` leaves empty as an empty value, so an empty
    ``LAKANA_OIDC__`` or ``LAKANA_CI_OIDC__`` setting means the same as a missing one (#853).

    Parameters
    ----------
    data
        The raw settings, as the environment or the caller gave them.

    Returns
    -------
    object
        For a mapping, the same mapping without the entries whose value is a text, or a secret
        text, that is empty or only white space; anything else unchanged, for pydantic to check.
    """
    if not isinstance(data, dict):
        return data
    return {key: value for key, value in data.items() if not _is_empty(value)}


def _is_empty(value: object) -> bool:
    """Tell whether a raw setting is empty.

    Parameters
    ----------
    value
        The raw value.

    Returns
    -------
    bool
        `True` for a text or a secret text that is empty or only white space.
    """
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return isinstance(value, str) and not value.strip()


def as_list(value: object, message: str, *, separators: str = ",") -> object:
    """Accept a list setting as a separated text or a JSON list.

    Parameters
    ----------
    value
        What the environment or the caller gave.
    message
        The message of a refusal, naming the variable.
    separators
        The characters that separate entries of a plain text.

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
        parts = re.split(f"[{re.escape(separators)}]", text)
        return [part.strip() for part in parts if part.strip()]
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(message)
    return [item.strip() for item in parsed if item.strip()]


def checked_issuer(value: str, message: str, *, allow_port: bool = False) -> str:
    """Check an issuer URL.

    Parameters
    ----------
    value
        An https URL.
    message
        The message of a refusal, naming the variable.
    allow_port
        Whether the URL may name a port.

    Returns
    -------
    str
        The issuer URL, with its host in lower case.

    Raises
    ------
    ValueError
        With `message`, if the entry is not an https URL with a host name, or has a port (unless
        `allow_port`), credentials, a query, a fragment or a trailing slash, or is longer than
        255 characters.
    """
    host = ""
    try:
        _, host, port, _ = parse_url(value, "")
    except EgressRefused:
        port = None
    parts = urlsplit(value)
    shown_port = f":{port}" if port is not None else ""
    if allow_port:
        port_ok = parts.netloc.lower() == f"{host}{shown_port}"
    else:
        port_ok = port is None and ":" not in parts.netloc
    if (
        not host
        or parts.scheme != "https"
        or not port_ok
        or is_ip_literal(host)
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
        or value.endswith("/")
        or len(value) > _MAX_ISSUER
    ):
        raise ValueError(message)
    return f"https://{host}{shown_port}{parts.path}"


def checked_host_names(value: list[str], message: str) -> list[str]:
    """Require plain host names and normalise them.

    Parameters
    ----------
    value
        The entries.
    message
        The message of a refusal, naming the variable.

    Returns
    -------
    list of str
        The names in lower case, without a trailing dot.

    Raises
    ------
    ValueError
        With `message`, if an entry is not a host name, or its last label is a number (an IP
        address).
    """
    names = [entry.strip().rstrip(".").lower() for entry in value]
    if not all(_HOST_NAME.match(name) and not name.rsplit(".", 1)[-1].isdigit() for name in names):
        raise ValueError(message)
    return names


class OidcSignInConfig(ProxyAndCaBundleChecks):
    """OIDC sign-in settings (`LAKANA_OIDC__*`); read by the server.

    With no issuer (the default) OIDC sign-in is off and the interface does not mention it. With
    one, the server also needs `LAKANA_OIDC__CLIENT_ID`, `LAKANA_OIDC__CLIENT_SECRET`,
    `LAKANA_UI__ENABLED=true` and `LAKANA_UI__PUBLIC_URL`, or it does not start. Register
    `<LAKANA_UI__PUBLIC_URL>/api/v1/sign-in/oidc/callback` as the redirect URI with the provider.
    The server reads the provider's discovery document, keys and token endpoint over ``https``,
    on port 443 or the port in the issuer, from public addresses unless the host is in
    `LAKANA_OIDC__ALLOW_HOSTS`, never following a redirect.

    Parameters
    ----------
    issuer
        The identity provider's issuer URL, exactly as its ID tokens name it in iss, such as
        `https://idp.example/realms/lakana` for a Keycloak realm. A port is allowed; no trailing
        slash. Not set (the default) turns OIDC sign-in off. An empty value of any
        `LAKANA_OIDC__` setting counts as not set, as Compose passes one for a `${VAR}` that
        `.env` leaves empty. People are linked to this issuer:
        after a change, or with it removed, everyone linked to the old one counts as unlinked
        until they link again (their passkeys sign them in meanwhile, and their API tokens stop
        at the end of the token window); setting the old issuer back counts their links again.
        Deactivate in Lakana anyone who left at the old provider: the start-up log names the
        people concerned.
    client_id
        The client id the provider gave Lakana. Required with an issuer.
    client_secret
        The client secret the provider gave Lakana; sent only to the provider's token endpoint,
        never shown or logged. Required with an issuer.
    scopes
        The scopes Lakana asks for, as a space- or comma-separated list or a JSON list (default
        `openid profile email`). It must contain `openid`. Lakana reads only the ID token's
        `sub`, and `email` and `name` to show who is linked.
    display_name
        The provider's name on the sign-in button, such as `Contoso` (default: the issuer's host
        name). At most 80 characters.
    allow_hosts
        Host names, comma-separated, exact names only. A listed host may serve the provider's
        keys or token endpoint when its discovery document names another host than the
        issuer's own, and a listed host may resolve to a private address (10.x, 172.16.x to
        172.31.x, 192.168.x, fd00::/8); every other host must be public. A provider on an
        internal network therefore needs its host name here. Loopback, link-local and cloud
        metadata addresses are refused even for a listed host. List only names whose DNS you
        control.
    proxy
        URL of the proxy that the requests to the provider go through, for example
        `http://user:password@host:3128`. The server checks the destination name, but the proxy
        looks it up again, so the proxy must refuse internal addresses itself. A password is
        hidden in logs and error messages.
    ca_bundle
        Absolute path of a PEM file of certificate authorities that the requests to the provider
        trust; it replaces the built-in store and never turns verification off.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    issuer: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    scopes: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(DEFAULT_SCOPES))
    display_name: str | None = None
    allow_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    proxy: SecretStr | None = None
    ca_bundle: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def _empty_is_not_set(cls, data: object) -> object:
        """Treat an empty setting as a missing one.

        Parameters
        ----------
        data
            The raw settings.

        Returns
        -------
        object
            See `without_empty_values`.
        """
        return without_empty_values(data)

    @property
    def enabled(self) -> bool:
        """Whether an issuer is configured.

        Returns
        -------
        bool
            `True` when people may sign in through the provider.
        """
        return self.issuer is not None

    @property
    def shown_name(self) -> str:
        """The provider's name as the interface shows it.

        Returns
        -------
        str
            `display_name`, else the issuer's host name; empty while OIDC is off.
        """
        if self.display_name is not None:
            return self.display_name
        return urlsplit(self.issuer or "").hostname or ""

    @field_validator("issuer")
    @classmethod
    def _issuer_is_a_url(cls, value: str | None) -> str | None:
        """Check the issuer URL; a port is allowed.

        Parameters
        ----------
        value
            The issuer, or `None`.

        Returns
        -------
        str or None
            The issuer with its host in lower case, or `None`.
        """
        if value is None:
            return None
        return checked_issuer(value.strip(), _ISSUER_MESSAGE, allow_port=True)

    @field_validator("client_id")
    @classmethod
    def _client_id_is_one_word(cls, value: str | None) -> str | None:
        """Require one printable word.

        Parameters
        ----------
        value
            The client id, or `None`.

        Returns
        -------
        str or None
            `value`, unchanged.

        Raises
        ------
        ValueError
            If it is empty, has a space or a control character, or is longer than 255.
        """
        if value is not None and (not _NO_SPACE.match(value) or len(value) > _MAX_CLIENT_ID):
            raise ValueError(_CLIENT_ID_MESSAGE)
        return value

    @field_validator("scopes", mode="before")
    @classmethod
    def _scopes_as_list(cls, value: object) -> object:
        """Accept the scopes as a space- or comma-separated text, or a JSON list.

        Parameters
        ----------
        value
            The raw value.

        Returns
        -------
        object
            See `as_list`.
        """
        return as_list(value, _SCOPES_MESSAGE, separators=", ")

    @field_validator("scopes")
    @classmethod
    def _scopes_include_openid(cls, value: list[str]) -> list[str]:
        """Require ``openid`` and well-formed scope tokens.

        Parameters
        ----------
        value
            The scopes.

        Returns
        -------
        list of str
            The scopes, each once, in the given order.

        Raises
        ------
        ValueError
            If ``openid`` is missing or a scope is not an RFC 6749 scope token of at most 128
            characters.
        """
        if "openid" not in value or not all(
            _SCOPE.match(scope) and len(scope) <= _MAX_SCOPE for scope in value
        ):
            raise ValueError(_SCOPES_MESSAGE)
        return list(dict.fromkeys(value))

    @field_validator("display_name")
    @classmethod
    def _display_name_is_one_line(cls, value: str | None) -> str | None:
        """Require one short line.

        Parameters
        ----------
        value
            The name, or `None`.

        Returns
        -------
        str or None
            The name without surrounding space, or `None`.

        Raises
        ------
        ValueError
            If it is longer than 80 characters or has a control character (an empty name
            never gets here: it counts as not set).
        """
        if value is None:
            return None
        name = value.strip()
        if len(name) > _MAX_DISPLAY_NAME or any(
            ord(char) < 0x20 or ord(char) == 0x7F for char in name
        ):
            raise ValueError(_DISPLAY_NAME_MESSAGE)
        return name

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
            See `as_list`.
        """
        return as_list(value, _ALLOW_HOSTS_MESSAGE)

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
            See `checked_host_names`.
        """
        return checked_host_names(value, _ALLOW_HOSTS_MESSAGE)

    @model_validator(mode="after")
    def _client_when_enabled(self) -> Self:
        """Require the client id and secret once an issuer is set.

        Returns
        -------
        OidcSignInConfig
            This configuration.

        Raises
        ------
        ValueError
            Naming the missing variable.
        """
        if self.issuer is None:
            return self
        for variable, value in (
            ("LAKANA_OIDC__CLIENT_ID", self.client_id),
            ("LAKANA_OIDC__CLIENT_SECRET", self.client_secret),
        ):
            if value is None or (isinstance(value, SecretStr) and not value.get_secret_value()):
                raise ValueError(
                    f"{variable} is required when LAKANA_OIDC__ISSUER is set: copy it from the "
                    "client you registered for Lakana at the identity provider"
                )
        return self
