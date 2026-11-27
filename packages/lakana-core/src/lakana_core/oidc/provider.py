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

import logging
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from lakana_core.net.https_json import post_form_json
from lakana_core.oidc.config import OidcSignInConfig
from lakana_core.oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.oidc.id_token import (
    SUPPORTED_ALGORITHMS,
    IdTokenRules,
    VerifiedIdToken,
    verify_id_token,
)
from lakana_core.oidc.keys import REFRESH_INTERVAL_SECONDS, JwksCache, KeySource, https_url_on

_log = logging.getLogger(__name__)


class CauseThrottle:
    """Let one log line per cause through in each `REFRESH_INTERVAL_SECONDS`.

    The sign-in's start and callback take no credential, so a caller can repeat a failure as
    often as the start limiter allows; the keys line is bounded the same way, by the refresh
    interval, so the lines of this module are too. A cause is any text; a line is allowed the
    first time and again once the interval has passed.

    Parameters
    ----------
    clock
        A monotonic clock in seconds.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: dict[str, float] = {}

    def allow(self, cause: str) -> bool:
        """Say whether a line for this cause may be written now.

        Parameters
        ----------
        cause
            What the line is about, with the issuer.

        Returns
        -------
        bool
            `True` for the first line of a cause and for one after the interval; the time is
            then taken.
        """
        now = self._clock()
        last = self._last.get(cause)
        if last is not None and now - last < REFRESH_INTERVAL_SECONDS:
            return False
        self._last[cause] = now
        return True

    def reset(self) -> None:
        """Forget every cause (for tests)."""
        self._last.clear()


LOG_THROTTLE = CauseThrottle()
"""The process's throttle of the sign-in's failure lines (this module's and the flow's)."""

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
ENDPOINT_NOT_ALLOWED = (
    "is not https on the issuer's host or a host of LAKANA_OIDC__ALLOW_HOSTS; add its host to "
    "LAKANA_OIDC__ALLOW_HOSTS or correct the provider's configuration"
)

Poster = Callable[[str, Mapping[str, str], Mapping[str, str], Collection[str]], object]
"""Send a form to the token endpoint: the URL, the form, the headers and the hosts it may be on,
to the parsed JSON answer. Raises `lakana_core.net.https_json.FetchFailed`."""


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


def _logged(issuer: str, reason: str) -> str:
    """Log why the provider cannot be used, and hand the reason back.

    `metadata` runs on every start and every callback, so the line is throttled
    (`LOG_THROTTLE`): the first one, then at most one per interval for each reason.

    The reason is a fixed sentence naming the setting to check, never a value from the
    provider; the issuer is the configured one. The discovery and key fetches log their own
    line (`lakana_core.oidc.keys`), so they do not come here.

    Parameters
    ----------
    issuer
        The configured issuer.
    reason
        The fixed reason.

    Returns
    -------
    str
        The reason, for `ProviderUnavailable`.
    """
    if LOG_THROTTLE.allow(f"metadata {issuer} {reason}"):
        _log.warning("could not use the OIDC sign-in provider %s: %s", issuer, reason)
    return reason


def _endpoint(
    document: Mapping[str, object], name: str, allowed: Collection[str], issuer: str
) -> str:
    """Read one endpoint of the discovery document.

    Parameters
    ----------
    document
        The discovery document.
    name
        The member, such as ``token_endpoint``.
    allowed
        The host names it may be on.
    issuer
        The configured issuer, for the log line.

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
            _logged(issuer, f"the identity provider names a {name} that {ENDPOINT_NOT_ALLOWED}")
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
    poster
        Sends a form to the token endpoint; the default is
        `lakana_core.net.https_json.post_form_json` with the operator's proxy, CA file and
        private hosts, and the issuer's port.

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
        poster: Poster | None = None,
    ) -> None:
        if config.issuer is None:
            raise ValueError("OIDC sign-in needs LAKANA_OIDC__ISSUER")
        self._config = config
        self._issuer = config.issuer
        self._source = key_source(config)
        self._keys = keys or JwksCache(self._source)
        self._post = poster or self._post_with_settings

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

    def _post_with_settings(
        self,
        url: str,
        form: Mapping[str, str],
        headers: Mapping[str, str],
        allowed_hosts: Collection[str],
    ) -> object:
        """Post a form with the operator's proxy, CA file and private hosts.

        Parameters
        ----------
        url
            The URL.
        form
            The fields.
        headers
            Further headers.
        allowed_hosts
            The host names it may be on.

        Returns
        -------
        object
            The parsed answer.
        """
        return post_form_json(
            url,
            form,
            headers=headers,
            allowed_hosts=allowed_hosts,
            private_hosts=self._source.allow_hosts,
            proxy=self._source.proxy,
            ca_bundle=self._source.ca_bundle,
            ports=self._source.ports,
        )

    def post(self, url: str, form: Mapping[str, str], headers: Mapping[str, str]) -> object:
        """Post a form to the provider, on the issuer's host or an allowed one only.

        Parameters
        ----------
        url
            The token endpoint, as `metadata` checked it.
        form
            The fields; they carry the code and may carry the client secret.
        headers
            Further headers; they may carry the client secret.

        Returns
        -------
        object
            The parsed JSON answer.

        Raises
        ------
        lakana_core.net.https_json.FetchFailed
            With a fixed reason, and the status of an answer other than 200.
        """
        return self._post(url, form, headers, self.allowed_hosts)

    def metadata(self, *, current: bool = False) -> ProviderMetadata:
        """Read and check the provider's discovery document.

        Parameters
        ----------
        current
            Whether the provider must have answered its latest fetch
            (`JwksCache.current_discovery`): asked before the browser is sent there. Otherwise
            the cached document serves, also while refreshes fail.

        Returns
        -------
        ProviderMetadata
            The endpoints, the algorithms and the client authentication.

        Raises
        ------
        ProviderUnavailable
            If the document or the keys cannot be fetched (with `current`, if the latest fetch
            failed), the document names another issuer,
            an endpoint is not ``https`` on an allowed host, no algorithm is supported, or no
            client authentication by secret is.
        """
        try:
            if current:
                document = self._keys.current_discovery(self._issuer)
            else:
                document = self._keys.discovery(self._issuer)
        except IdTokenRefused:
            document = None
        if document is None:
            raise ProviderUnavailable(UNREACHABLE)
        allowed = self.allowed_hosts
        authorization = _endpoint(document, "authorization_endpoint", allowed, self._issuer)
        token = _endpoint(document, "token_endpoint", allowed, self._issuer)
        listed = _texts(document.get("id_token_signing_alg_values_supported")) or ["RS256"]
        algorithms = frozenset(listed) & frozenset(SUPPORTED_ALGORITHMS)
        if not algorithms:
            raise ProviderUnavailable(_logged(self._issuer, NO_ALGORITHM))
        methods = _texts(document.get("token_endpoint_auth_methods_supported"))
        # OIDC Discovery: an absent list means client_secret_basic.
        token_auth: Literal["client_secret_basic", "client_secret_post"]
        if not methods or "client_secret_basic" in methods:
            token_auth = "client_secret_basic"  # nosec B105 -- a method name, not a secret
        elif "client_secret_post" in methods:
            token_auth = "client_secret_post"  # nosec B105 -- a method name, not a secret
        else:
            raise ProviderUnavailable(_logged(self._issuer, NO_CLIENT_AUTH))
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
