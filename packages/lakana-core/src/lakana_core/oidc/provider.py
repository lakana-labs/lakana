"""The OIDC sign-in's view of its one provider: metadata from discovery, and ID-token checks.

- The discovery document is `lakana_core.oidc.keys`'s, fetched and cached with the keys. Its
  ``issuer`` must be the configured issuer exactly. Its ``authorization_endpoint`` and
  ``token_endpoint`` must each be ``https``, without user information, on the issuer's host or
  a host of `LAKANA_OIDC__ALLOW_HOSTS`: the browser is sent to the one and the client secret to
  the other.
- The accepted signature algorithms are the document's ``id_token_signing_alg_values_supported``
  that Lakana supports (RSA only), or ``RS256`` when the document lists none.
- The client authenticates at the token endpoint with ``client_secret_basic`` unless the
  document lists only ``client_secret_post``.
- An ID token is checked by the shared verifier (`lakana_core.oidc.id_token`) with the rules of a
  sign-in (`OidcProvider.rules`).

Every failure to reach or use the provider is a `ProviderUnavailable` with a fixed reason, which
never quotes a value from the provider.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from lakana_core.oidc.config import OidcSignInConfig
from lakana_core.oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.oidc.id_token import (
    SUPPORTED_ALGORITHMS,
    IdTokenRules,
    VerifiedIdToken,
    verify_id_token,
)
from lakana_core.oidc.keys import JwksCache, KeySource, https_url_on

ID_TOKEN_MAX_AGE_SECONDS = 600
"""The oldest ID token a sign-in accepts, counted from ``iat``: the flow's own lifetime."""

MAX_SUBJECT = 255
"""The longest ``sub`` a sign-in accepts: the column that keeps it."""

UNREACHABLE = (
    "the identity provider's discovery document or keys could not be fetched; check the "
    "server's outbound https access to LAKANA_OIDC__ISSUER (LAKANA_OIDC__PROXY, "
    "LAKANA_OIDC__CA_BUNDLE, LAKANA_OIDC__ALLOW_HOSTS) and the server's log"
)
NO_ALGORITHM = (
    "the identity provider signs ID tokens with no algorithm Lakana supports; configure it to "
    "sign them with RS256"
)
NO_CLIENT_AUTH = (
    "the identity provider's token endpoint accepts neither client_secret_basic nor "
    "client_secret_post; configure the client for one of them"
)


class ProviderUnavailable(Exception):
    """The provider could not be reached or used.

    Parameters
    ----------
    reason
        A fixed description, naming the setting to check; never a value from the provider.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProviderMetadata:
    """What the sign-in needs of the provider's discovery document, checked.

    Parameters
    ----------
    issuer
        The issuer, exactly as configured.
    authorization_endpoint
        Where the browser is sent to sign in.
    token_endpoint
        Where the code is exchanged, with the client secret.
    algorithms
        The signature algorithms an ID token may use.
    token_auth
        How the client authenticates at the token endpoint.
    """

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    algorithms: frozenset[str]
    token_auth: Literal["client_secret_basic", "client_secret_post"]


def key_source(config: OidcSignInConfig) -> KeySource:
    """Describe where the provider's keys may come from.

    Parameters
    ----------
    config
        The sign-in settings.

    Returns
    -------
    KeySource
        The issuer, `LAKANA_OIDC__ALLOW_HOSTS`, the proxy and the CA file.
    """
    return KeySource(
        issuers=frozenset({config.issuer}) if config.issuer else frozenset(),
        allow_hosts=frozenset(config.allow_hosts),
        proxy=config.proxy,
        ca_bundle=config.ca_bundle,
        label="OIDC sign-in",
        allow_hosts_variable="LAKANA_OIDC__ALLOW_HOSTS",
    )


def _endpoint(document: Mapping[str, object], name: str, allowed: Collection[str]) -> str:
    """Read one endpoint of the discovery document.

    Parameters
    ----------
    document
        The discovery document.
    name
        The member, such as ``token_endpoint``.
    allowed
        The host names it may be on.

    Returns
    -------
    str
        The URL.

    Raises
    ------
    ProviderUnavailable
        If it is missing, not ``https``, carries user information or is on another host.
    """
    value = document.get(name)
    if not https_url_on(value, allowed):
        raise ProviderUnavailable(
            f"the identity provider's {name} is not https on the issuer's host or a host of "
            "LAKANA_OIDC__ALLOW_HOSTS"
        )
    return str(value)


def _texts(value: object) -> list[str]:
    """Read a list of texts from the discovery document.

    Parameters
    ----------
    value
        The member.

    Returns
    -------
    list of str
        Its texts; empty when it is not a list.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


class OidcProvider:
    """The configured provider: its checked metadata and the sign-in's ID-token checks.

    Safe to share between threads; the discovery document and keys are cached by `keys`.

    Parameters
    ----------
    config
        The sign-in settings; an issuer must be set.
    keys
        The provider's key cache; by default one built from `config` that fetches over https.

    Raises
    ------
    ValueError
        If `config` has no issuer.
    """

    def __init__(
        self,
        config: OidcSignInConfig,
        *,
        keys: JwksCache | None = None,
    ) -> None:
        if config.issuer is None:
            raise ValueError("OIDC sign-in needs LAKANA_OIDC__ISSUER")
        self._config = config
        self._issuer = config.issuer
        self._keys = keys or JwksCache(key_source(config))

    @property
    def config(self) -> OidcSignInConfig:
        """The sign-in settings.

        Returns
        -------
        OidcSignInConfig
            As given.
        """
        return self._config

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """The hosts a request to the provider may reach.

        Returns
        -------
        frozenset of str
            The issuer's host and `LAKANA_OIDC__ALLOW_HOSTS`.
        """
        own = urlsplit(self._issuer).hostname or ""
        return frozenset({own, *self._config.allow_hosts})

    def metadata(self) -> ProviderMetadata:
        """Read and check the provider's discovery document.

        Returns
        -------
        ProviderMetadata
            The endpoints, the algorithms and the client authentication.

        Raises
        ------
        ProviderUnavailable
            If the document or the keys cannot be fetched, the document names another issuer,
            an endpoint is not ``https`` on an allowed host, no algorithm is supported, or no
            client authentication by secret is.
        """
        try:
            document = self._keys.discovery(self._issuer)
        except IdTokenRefused:
            document = None
        if document is None:
            raise ProviderUnavailable(UNREACHABLE)
        allowed = self.allowed_hosts
        authorization = _endpoint(document, "authorization_endpoint", allowed)
        token = _endpoint(document, "token_endpoint", allowed)
        listed = _texts(document.get("id_token_signing_alg_values_supported")) or ["RS256"]
        algorithms = frozenset(listed) & frozenset(SUPPORTED_ALGORITHMS)
        if not algorithms:
            raise ProviderUnavailable(NO_ALGORITHM)
        methods = _texts(document.get("token_endpoint_auth_methods_supported"))
        # OIDC Discovery: an absent list means client_secret_basic.
        token_auth: Literal["client_secret_basic", "client_secret_post"]
        if not methods or "client_secret_basic" in methods:
            token_auth = "client_secret_basic"
        elif "client_secret_post" in methods:
            token_auth = "client_secret_post"
        else:
            raise ProviderUnavailable(NO_CLIENT_AUTH)
        return ProviderMetadata(
            issuer=self._issuer,
            authorization_endpoint=authorization,
            token_endpoint=token,
            algorithms=algorithms,
            token_auth=token_auth,
        )

    def rules(self, nonce: str) -> IdTokenRules:
        """Name what a sign-in requires of an ID token.

        Parameters
        ----------
        nonce
            The flow's nonce.

        Returns
        -------
        IdTokenRules
            ``aud`` and ``azp`` the client id, the flow's nonce, a ``typ`` of ``ID`` when
            present, at most `ID_TOKEN_MAX_AGE_SECONDS` old, no ``jti`` required, and the
            provider's algorithms.

        Raises
        ------
        ProviderUnavailable
            If the metadata cannot be read (`metadata`).
        """
        client_id = self._config.client_id or ""
        return IdTokenRules(
            audience=client_id,
            algorithms=self.metadata().algorithms,
            max_age_seconds=ID_TOKEN_MAX_AGE_SECONDS,
            require_jti=False,
            authorized_party=client_id,
            nonce=nonce,
            refuse_typ_other_than_id=True,
        )

    def verify(self, id_token: str, *, nonce: str, now: datetime) -> VerifiedIdToken:
        """Verify an ID token the token endpoint answered.

        Parameters
        ----------
        id_token
            The compact JWT.
        nonce
            The flow's nonce.
        now
            The time of the check, with a time zone.

        Returns
        -------
        VerifiedIdToken
            The checked token.

        Raises
        ------
        IdTokenRefused
            With the code of the first check that failed, or `CLAIM_MISSING` for a ``sub``
            longer than `MAX_SUBJECT`.
        ProviderUnavailable
            If the metadata or the keys cannot be fetched.
        """
        rules = self.rules(nonce)
        try:
            verified = verify_id_token(
                id_token, keys=self._keys, issuers=frozenset({self._issuer}), rules=rules, now=now
            )
        except IdTokenRefused as refused:
            if refused.code is not RefusalCode.KEYS_UNAVAILABLE:
                raise
            verified = None
        if verified is None:
            raise ProviderUnavailable(UNREACHABLE)
        if len(verified.subject) > MAX_SUBJECT:
            raise IdTokenRefused(RefusalCode.CLAIM_MISSING)
        return verified
