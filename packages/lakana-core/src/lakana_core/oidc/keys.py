"""Each configured issuer's discovery document and signing keys: cached, refreshed with restraint.

- Only an issuer of the `KeySource` is ever fetched; the verifier checks the token's issuer
  before it asks for a key, so a token cannot make the server reach any other host. Requests
  use port 443, or the port an issuer URL names (`KeySource.ports`).
- The discovery document comes from the issuer's own host and must name the issuer exactly; the
  ``jwks_uri`` it names must be ``https`` on the issuer's host or on a host of the source's
  allowed hosts, and the fetcher (`lakana_core.net.https_json`) checks it again. Only an allowed
  host may resolve to a private address (never loopback, link-local or a metadata address);
  every other host must be public.
- The discovery document and the keys are fetched together and kept together: fresh for
  `FRESH_SECONDS`. A key id that is not in the set refreshes both, but at most once per
  `REFRESH_INTERVAL_SECONDS` per issuer, whatever the reason, so random key ids cannot turn
  the server into a request generator.
- When a refresh fails, the last good document and keys stay usable for `STALE_SECONDS` past
  their freshness; after that every lookup fails closed.
- Only RSA signing keys are kept: an entry with another key type, ``use`` other than ``sig``,
  a private part or a malformed modulus is left out. Two keys with one ``kid`` make the whole set
  unusable. The key size is checked when a key is used (`SigningKey.bits`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from jwt.exceptions import PyJWTError
from pydantic import SecretStr

from lakana_core.net.https_json import FetchFailed, fetch_json
from lakana_core.oidc.errors import IdTokenRefused, RefusalCode

FRESH_SECONDS = 3600.0
"""How long a fetched document and keys are used without asking the issuer again."""

REFRESH_INTERVAL_SECONDS = 60.0
"""The least time between two fetches of one issuer's keys."""

STALE_SECONDS = 86400.0
"""How long past their freshness the last good keys stay usable while refreshes fail."""

MAX_RSA_BITS = 8192
"""The largest RSA key kept; GitHub and GitLab use 2048 bits."""

DISCOVERY_PATH = "/.well-known/openid-configuration"

Fetcher = Callable[[str, Collection[str]], object]
"""Fetch a JSON document: the URL and the host names it may be on, to the parsed value."""

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KeySource:
    """Where an issuer's keys may come from, and how a failure is named.

    Parameters
    ----------
    issuers
        The issuer URLs that may be fetched, exactly as their tokens name them.
    allow_hosts
        The host names that may serve keys besides the issuer's own, and that may resolve to a
        private address.
    proxy
        The operator's proxy, or `None`.
    ca_bundle
        The operator's CA file, or `None`.
    label
        What the keys are for, in the log line of a failed fetch (``CI OIDC``).
    allow_hosts_variable
        The setting that holds `allow_hosts`, named when a ``jwks_uri`` is refused.
    """

    issuers: frozenset[str]
    allow_hosts: frozenset[str]
    proxy: SecretStr | None
    ca_bundle: Path | None
    label: str
    allow_hosts_variable: str

    @property
    def ports(self) -> frozenset[int]:
        """The ports a request to the source may use.

        Returns
        -------
        frozenset of int
            443, and each port an issuer URL names (only the sign-in's issuer may name one).
        """
        named = {urlsplit(issuer).port for issuer in self.issuers}
        return frozenset({443, *(port for port in named if port is not None)})


class KeySetInvalid(Exception):
    """A discovery document or key set cannot be used.

    Parameters
    ----------
    reason
        A fixed description.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SigningKey:
    """One of an issuer's public signing keys.

    Parameters
    ----------
    kid
        Its key id.
    public_key
        The RSA public key.
    alg
        The algorithm the issuer bound it to, or `None` when the JWK names none.
    """

    kid: str
    public_key: rsa.RSAPublicKey
    alg: str | None

    @property
    def bits(self) -> int:
        """The key size.

        Returns
        -------
        int
            The modulus length in bits.
        """
        return self.public_key.key_size


@dataclass(frozen=True)
class _Snapshot:
    """What one successful fetch of an issuer brought.

    Parameters
    ----------
    keys
        The keys by key id.
    document
        The discovery document.
    fetched_at
        The clock's time of the fetch.
    """

    keys: Mapping[str, SigningKey]
    document: Mapping[str, object]
    fetched_at: float


@dataclass
class _Issuer:
    """The cache state of one issuer.

    Parameters
    ----------
    url
        The issuer.
    """

    url: str
    # Replaced as a whole, so a reader needs no lock.
    snapshot: _Snapshot | None = None
    attempted_at: float | None = None
    refreshing: threading.Lock = field(default_factory=threading.Lock)


def _parse_key(entry: object) -> SigningKey | None:
    """Turn one JWK into a signing key, or leave it out.

    Parameters
    ----------
    entry
        One member of the set's ``keys``.

    Returns
    -------
    SigningKey or None
        The key, or `None` for anything but a well-formed public RSA signing key with a key id
        and at most `MAX_RSA_BITS` bits (a larger one only costs time to verify).
    """
    if not isinstance(entry, dict) or entry.get("kty") != "RSA":
        return None
    kid, alg = entry.get("kid"), entry.get("alg")
    if not isinstance(kid, str) or not kid or entry.get("use", "sig") != "sig" or "d" in entry:
        return None
    try:
        public_key = RSAAlgorithm.from_jwk(entry)
    except (PyJWTError, ValueError, TypeError):
        return None
    if not isinstance(public_key, rsa.RSAPublicKey):  # pragma: no cover - "d" is refused above
        return None
    if public_key.key_size > MAX_RSA_BITS:
        return None
    return SigningKey(kid=kid, public_key=public_key, alg=alg if isinstance(alg, str) else None)


def _parse_key_set(document: object) -> dict[str, SigningKey]:
    """Read the usable keys of a key set.

    Parameters
    ----------
    document
        The fetched key set.

    Returns
    -------
    dict[str, SigningKey]
        The keys by key id.

    Raises
    ------
    KeySetInvalid
        If the document has no ``keys`` list, or two keys share a key id.
    """
    if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
        raise KeySetInvalid("the key set has no list of keys")
    keys: dict[str, SigningKey] = {}
    for entry in document["keys"]:
        key = _parse_key(entry)
        if key is None:
            continue
        if key.kid in keys:
            raise KeySetInvalid("two keys of the key set share a key id")
        keys[key.kid] = key
    return keys


def https_url_on(value: object, allowed: Collection[str]) -> bool:
    """Say whether a URL from a discovery document is ``https`` on an allowed host.

    Parameters
    ----------
    value
        The document's member.
    allowed
        The host names it may be on.

    Returns
    -------
    bool
        Whether it is a text, ``https``, without user information, and on one of `allowed`.
    """
    scheme, netloc, host = "", "", ""
    if isinstance(value, str):
        try:
            parts = urlsplit(value)
            scheme, netloc, host = parts.scheme, parts.netloc, parts.hostname or ""
        except ValueError:
            pass
    return scheme == "https" and "@" not in netloc and host in allowed


class JwksCache:
    """The discovery documents and signing keys of a source's issuers; safe to share.

    Parameters
    ----------
    source
        The issuers, the allowed key hosts, the proxy and the CA bundle.
    fetcher
        Fetches one JSON document; the default is `lakana_core.net.https_json.fetch_json` with
        the source's proxy, CA bundle and private hosts.
    clock
        A monotonic clock in seconds.
    """

    _log: logging.Logger = _log

    def __init__(
        self,
        source: KeySource,
        *,
        fetcher: Fetcher | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._fetch = fetcher or self._fetch_with_settings
        self._clock = clock
        self._issuers = {url: _Issuer(url) for url in source.issuers}

    def _json_fetcher(self) -> Callable[..., object]:
        """Name the function the default fetcher calls, looked up when a fetch runs.

        Returns
        -------
        Callable
            `lakana_core.net.https_json.fetch_json` as this module imported it.
        """
        return fetch_json

    def _fetch_options(self) -> dict[str, object]:
        """Name the further keyword arguments of the default fetcher.

        Returns
        -------
        dict
            The ports a request to the source may use (`KeySource.ports`).
        """
        return {"ports": self._source.ports}

    def _refusal(self, code: RefusalCode) -> Exception:
        """Build the refusal of a key lookup.

        Parameters
        ----------
        code
            Why.

        Returns
        -------
        Exception
            An `IdTokenRefused` with the shared message; a binding may name its settings.
        """
        return IdTokenRefused(code)

    def _fetch_with_settings(self, url: str, allowed_hosts: Collection[str]) -> object:
        """Fetch a document with the source's proxy, CA bundle and private hosts.

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
        return self._json_fetcher()(
            url,
            allowed_hosts=allowed_hosts,
            private_hosts=self._source.allow_hosts,
            proxy=self._source.proxy,
            ca_bundle=self._source.ca_bundle,
            **self._fetch_options(),
        )

    def _load(self, issuer: str, now: float) -> _Snapshot:
        """Fetch an issuer's discovery document and then its key set.

        Parameters
        ----------
        issuer
            The issuer URL.
        now
            The clock's time.

        Returns
        -------
        _Snapshot
            The keys and the document.

        Raises
        ------
        KeySetInvalid
            If the discovery document names another issuer or a ``jwks_uri`` that is not
            ``https`` on an allowed host, or the key set is unusable.
        """
        own_host = urlsplit(issuer).hostname or ""
        document = self._fetch(issuer + DISCOVERY_PATH, frozenset({own_host}))
        if not isinstance(document, dict) or document.get("issuer") != issuer:
            raise KeySetInvalid("the discovery document names another issuer")
        jwks_uri = document.get("jwks_uri")
        allowed = frozenset({own_host, *self._source.allow_hosts})
        if not https_url_on(jwks_uri, allowed):
            raise KeySetInvalid(
                "the discovery document's jwks_uri is not https on the issuer's host or a host "
                f"of {self._source.allow_hosts_variable}"
            )
        keys = _parse_key_set(self._fetch(str(jwks_uri), allowed))
        return _Snapshot(keys=keys, document=document, fetched_at=now)

    def _refresh(self, state: _Issuer, now: float) -> None:
        """Fetch the document and keys again, keeping the last good ones when that fails.

        Parameters
        ----------
        state
            The issuer's cache state; its refresh lock is held.
        now
            The clock's time.
        """
        state.attempted_at = now
        reason = ""
        try:
            state.snapshot = self._load(state.url, now)
        except (FetchFailed, KeySetInvalid) as failed:
            reason = failed.reason
        if reason:
            self._log.warning(
                "could not fetch the %s keys of %s: %s", self._source.label, state.url, reason
            )

    @staticmethod
    def _fresh(state: _Issuer, now: float) -> _Snapshot | None:
        """Return the snapshot while it is fresh.

        Parameters
        ----------
        state
            The issuer's cache state.
        now
            The clock's time.

        Returns
        -------
        _Snapshot or None
            The snapshot, or `None` when there is none or it is no longer fresh.
        """
        snapshot = state.snapshot
        if snapshot is None or now - snapshot.fetched_at >= FRESH_SECONDS:
            return None
        return snapshot

    def _snapshot(self, issuer: str, enough: Callable[[_Snapshot], bool]) -> _Snapshot:
        """Return an issuer's usable snapshot, refreshing when it is stale or not `enough`.

        Parameters
        ----------
        issuer
            The issuer URL, exactly as configured.
        enough
            Whether a fresh snapshot answers the question (it has the key id asked for).

        Returns
        -------
        _Snapshot
            The fresh snapshot, or the last good one within `STALE_SECONDS`.

        Raises
        ------
        IdTokenRefused
            `ISSUER_NOT_ALLOWED` for an issuer that is not configured (nothing is fetched),
            `KEYS_UNAVAILABLE` when there is no usable snapshot.
        """
        state = self._issuers.get(issuer)
        if state is None:
            raise self._refusal(RefusalCode.ISSUER_NOT_ALLOWED)
        snapshot = self._fresh(state, self._clock())
        if snapshot is not None and enough(snapshot):
            return snapshot
        with state.refreshing:
            now = self._clock()
            snapshot = self._fresh(state, now)
            if snapshot is not None and enough(snapshot):
                return snapshot
            if state.attempted_at is None or now - state.attempted_at >= REFRESH_INTERVAL_SECONDS:
                self._refresh(state, now)
        snapshot = state.snapshot
        if snapshot is None or now - snapshot.fetched_at >= FRESH_SECONDS + STALE_SECONDS:
            raise self._refusal(RefusalCode.KEYS_UNAVAILABLE)
        return snapshot

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
            this key id.
        """
        key = self._snapshot(issuer, lambda snapshot: kid in snapshot.keys).keys.get(kid)
        if key is None:
            raise self._refusal(RefusalCode.UNKNOWN_KEY)
        return key

    def discovery(self, issuer: str) -> Mapping[str, object]:
        """Return an issuer's discovery document, as it was fetched with its keys.

        Parameters
        ----------
        issuer
            The issuer URL, exactly as configured.

        Returns
        -------
        Mapping[str, object]
            The document; it names the issuer exactly and a usable ``jwks_uri``.

        Raises
        ------
        IdTokenRefused
            `ISSUER_NOT_ALLOWED` for an issuer that is not configured (nothing is fetched),
            `KEYS_UNAVAILABLE` when there is no usable document.
        """
        return self._snapshot(issuer, lambda _: True).document
