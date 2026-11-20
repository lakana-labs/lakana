"""Each configured CI issuer's signing keys: the shared key cache bound to `LAKANA_CI_OIDC__*`.

The cache itself, its freshness and refresh rules and the checks on the discovery document and
the ``jwks_uri`` are `lakana_core.oidc.keys`, which the OIDC sign-in shares. This binding names
CI in its log line and `LAKANA_CI_OIDC__ALLOW_HOSTS` in its refusal, raises CI's refusals
(`lakana_core.ci_oidc.errors`, CI's own codes and messages) and fetches with `fetch_json` from
this module, looked up when a fetch runs:

- Only an issuer in `LAKANA_CI_OIDC__ISSUERS` is ever fetched.
- Only a host on `LAKANA_CI_OIDC__ALLOW_HOSTS` may resolve to a private address (never loopback,
  link-local or a metadata address); every other host must be public.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection

from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_core.ci_oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.net.https_json import fetch_json
from lakana_core.oidc import errors as shared
from lakana_core.oidc import keys
from lakana_core.oidc.keys import (
    DISCOVERY_PATH,
    FRESH_SECONDS,
    MAX_RSA_BITS,
    REFRESH_INTERVAL_SECONDS,
    STALE_SECONDS,
    Fetcher,
    KeySource,
    SigningKey,
)

__all__ = [
    "DISCOVERY_PATH",
    "FRESH_SECONDS",
    "MAX_RSA_BITS",
    "REFRESH_INTERVAL_SECONDS",
    "STALE_SECONDS",
    "Fetcher",
    "JwksCache",
    "SigningKey",
    "fetch_json",
]

_log = logging.getLogger(__name__)


class JwksCache(keys.JwksCache):
    """The signing keys of the configured CI issuers; safe to share between threads.

    Parameters
    ----------
    config
        The operator settings: the issuers, the allowed key hosts, the proxy and the CA bundle.
    fetcher
        Fetches one JSON document; the default is `lakana_core.net.https_json.fetch_json` with
        the operator's proxy and CA bundle.
    clock
        A monotonic clock in seconds.
    """

    _log = _log

    def __init__(
        self,
        config: CiOidcConfig,
        *,
        fetcher: Fetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        source = KeySource(
            issuers=frozenset(config.issuers),
            allow_hosts=frozenset(config.allow_hosts),
            proxy=config.proxy,
            ca_bundle=config.ca_bundle,
            label="CI OIDC",
            allow_hosts_variable="LAKANA_CI_OIDC__ALLOW_HOSTS",
        )
        super().__init__(source, fetcher=fetcher, clock=clock)

    def _fetch_with_settings(self, url: str, allowed_hosts: Collection[str]) -> object:
        """Fetch a document with the operator's proxy, CA bundle and private hosts.

        Parameters
        ----------
        url
            The URL.
        allowed_hosts
            The host names it may be on.

        Returns
        -------
        object
            The parsed document.
        """
        # Only the operator's own list may resolve to private addresses: an issuer's host
        # (or a jwks_uri on it) needs to be listed there too.
        return fetch_json(
            url,
            allowed_hosts=allowed_hosts,
            private_hosts=self._source.allow_hosts,
            proxy=self._source.proxy,
            ca_bundle=self._source.ca_bundle,
        )

    def signing_key(self, issuer: str, kid: str) -> SigningKey:
        """Return an issuer's key by its key id.

        Parameters
        ----------
        issuer
            The issuer URL, exactly as configured.
        kid
            The key id from the token's header.

        Returns
        -------
        SigningKey
            The key.

        Raises
        ------
        IdTokenRefused
            `ISSUER_NOT_ALLOWED` for an issuer that is not configured (nothing is fetched),
            `KEYS_UNAVAILABLE` when there are no usable keys, `UNKNOWN_KEY` when the keys lack
            this key id; with CI's messages.
        """
        try:
            return super().signing_key(issuer, kid)
        except shared.IdTokenRefused as refused:
            code = RefusalCode(refused.code.value)
        raise IdTokenRefused(code)
