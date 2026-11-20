"""Verify a CI provider's OIDC ID token: the shared checks bound to `LAKANA_CI_OIDC__*`.

The checks and their order are `lakana_core.oidc.id_token`'s, which the OIDC sign-in shares:
the token's shape and header first, then its issuer against `LAKANA_CI_OIDC__ISSUERS`
**before** any network call, then the key and the signature, ``aud`` against
`LAKANA_CI_OIDC__AUDIENCE`, the times with `LAKANA_CI_OIDC__MAX_TOKEN_AGE_SECONDS`, and ``sub``
and ``jti``, which CI requires. CI checks no ``azp``, ``nonce`` or ``typ``, and its refusals
carry CI's own codes and messages (`lakana_core.ci_oidc.errors`, unchanged).

Single use of the ``jti`` is the caller's: it needs the database. `VerifiedIdToken` carries the
``jti`` and the expiry, so the caller can record the one until the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_core.ci_oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.ci_oidc.jwks import JwksCache
from lakana_core.oidc import errors as shared
from lakana_core.oidc import id_token
from lakana_core.oidc.id_token import (
    CLOCK_SKEW_SECONDS,
    MAX_LIFETIME_SECONDS,
    MAX_TOKEN_BYTES,
    MIN_RSA_BITS,
    IdTokenRules,
)

__all__ = [
    "CLOCK_SKEW_SECONDS",
    "MAX_LIFETIME_SECONDS",
    "MAX_TOKEN_BYTES",
    "MIN_RSA_BITS",
    "IdTokenVerifier",
    "VerifiedIdToken",
]


@dataclass(frozen=True)
class VerifiedIdToken(id_token.VerifiedIdToken):
    """A CI ID token whose signature, issuer, audience and times have been checked.

    Parameters
    ----------
    claims
        Every claim of the token, for the caller's trust rules.
    issuer
        The issuer, exactly as configured.
    subject
        The ``sub`` claim.
    jti
        The token's id, always present for CI; the caller must accept it once only.
    expires_at
        When the token expires (``exp``); the caller keeps the ``jti`` until then.
    """

    jti: str


class IdTokenVerifier:
    """Check CI ID tokens against the operator's settings; safe to share between threads.

    Parameters
    ----------
    config
        The operator settings (`LAKANA_CI_OIDC__*`).
    keys
        The issuers' key cache; by default one built from `config` that fetches over https.
    """

    def __init__(self, config: CiOidcConfig, keys: JwksCache | None = None) -> None:
        self._config = config
        self._keys = keys or JwksCache(config)
        self._issuers = frozenset(config.issuers)
        self._algorithms = frozenset(config.algorithms)

    @property
    def issuers(self) -> frozenset[str]:
        """The configured issuers (`LAKANA_CI_OIDC__ISSUERS`, resolved).

        Returns
        -------
        frozenset of str
            The issuer URLs; empty when CI OIDC is off.
        """
        return self._issuers

    def verify_id_token(self, token: str, *, now: datetime) -> VerifiedIdToken:
        """Verify an ID token.

        Parameters
        ----------
        token
            The compact JWT, as the CI provider issued it.
        now
            The time of the check, with a time zone.

        Returns
        -------
        VerifiedIdToken
            The checked token. Its ``jti`` has not been checked for reuse.

        Raises
        ------
        IdTokenRefused
            With the code of the first check that failed, in the order of
            `lakana_core.oidc.id_token`; `DISABLED` when no issuer is configured.
        ValueError
            If `now` has no time zone.
        """
        rules = IdTokenRules(
            audience=self._config.audience or "",
            algorithms=self._algorithms,
            max_age_seconds=self._config.max_token_age_seconds,
            require_jti=True,
            authorized_party=None,
            nonce=None,
            refuse_typ_other_than_id=False,
        )
        try:
            verified = id_token.verify_id_token(
                token, keys=self._keys, issuers=self._issuers, rules=rules, now=now
            )
        except shared.IdTokenRefused as refused:
            # CI's codes are the shared ones it can meet; it never sets the rules that raise
            # the others (azp, nonce, typ).
            code = RefusalCode(refused.code.value)
        else:
            return VerifiedIdToken(
                claims=verified.claims,
                issuer=verified.issuer,
                subject=verified.subject,
                jti=str(verified.jti),
                expires_at=verified.expires_at,
            )
        raise IdTokenRefused(code)
