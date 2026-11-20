"""Verify an OIDC ID token, in a fixed order, against its caller's rules.

The order matters: everything that can be decided from the token alone is decided first, and
the token's issuer is checked against the caller's issuers **before** any network call, so an
attacker's token can make the server fetch nothing but a configured issuer's keys (and those at
most once a minute, `lakana_core.oidc.keys`).

1. At most `MAX_TOKEN_BYTES`, three base64url segments, a JSON object as header and payload,
   no member twice (so the claims checked here are the ones that were signed).
2. The header's ``alg`` is one of the rules' algorithms (never ``none`` or HMAC).
3. A header that supplies a key or a critical extension (``jku``, ``x5u``, ``x5c``, ``jwk``,
   ``crit``) is refused: the keys come from the issuer only.
4. A ``kid`` is present.
5. The unverified ``iss`` is exactly one of the issuers (no normalisation: a trailing slash is
   another issuer). The signature, checked next, then binds it.
6. The key with that ``kid`` from the issuer's key set; it must be RSA, of at least 2048 bits and
   not bound to another algorithm.
7. The signature (PyJWT, with this one algorithm and this one key).
8. With `IdTokenRules.refuse_typ_other_than_id`, a ``typ`` claim, when present, is ``ID``
   (Keycloak marks its access tokens ``Bearer``).
9. ``aud`` is, or contains, the rules' audience.
10. With `IdTokenRules.authorized_party`: ``azp``, when present, is that party, and it must be
    present when ``aud`` names more than one audience.
11. ``exp``, ``nbf`` and ``iat`` with `CLOCK_SKEW_SECONDS` of skew, a maximum age since ``iat``
    (`IdTokenRules.max_age_seconds`) and a lifetime of at most a day.
12. With `IdTokenRules.nonce`, the ``nonce`` claim equals it.
13. ``sub`` is present, and ``jti`` too with `IdTokenRules.require_jti`.

Single use of the ``jti`` is the caller's: it needs the database. `VerifiedIdToken` carries the
``jti`` and the expiry, so the caller can record the one until the other.
"""

from __future__ import annotations

import base64
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from jwt.api_jws import PyJWS
from jwt.exceptions import PyJWTError

from lakana_core.oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.oidc.keys import JwksCache

SUPPORTED_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512")
"""The signature algorithms that may ever be accepted: RSA only."""

MAX_TOKEN_BYTES = 16 * 1024
"""The largest ID token looked at; GitHub's and GitLab's are about 1 to 2 KiB."""

CLOCK_SKEW_SECONDS = 60
"""How far the server's clock and the issuer's may disagree."""

MAX_LIFETIME_SECONDS = 86400
"""The longest lifetime (``exp`` minus ``iat``) a token may claim."""

MIN_RSA_BITS = 2048

_MAX_KID = 256
_MAX_JTI = 255
_COMPACT = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")
_KEY_HEADERS = ("jku", "x5u", "x5c", "jwk", "crit")
_JWS = PyJWS()


@dataclass(frozen=True)
class IdTokenRules:
    """What a caller requires of an ID token beyond its issuer and signature.

    Parameters
    ----------
    audience
        The audience ``aud`` must be or contain.
    algorithms
        The accepted signature algorithms; only `SUPPORTED_ALGORITHMS` ever pass.
    max_age_seconds
        The oldest token accepted, counted from ``iat``.
    require_jti
        Whether a ``jti`` must be present (CI records it once; a sign-in has its nonce).
    authorized_party
        The client ``azp`` must name when present, and must be present for several audiences;
        `None` leaves ``azp`` unchecked.
    nonce
        The value the ``nonce`` claim must equal; `None` leaves it unchecked.
    refuse_typ_other_than_id
        Whether a ``typ`` claim other than ``ID`` is refused.
    """

    audience: str
    algorithms: frozenset[str]
    max_age_seconds: int
    require_jti: bool
    authorized_party: str | None
    nonce: str | None
    refuse_typ_other_than_id: bool


@dataclass(frozen=True)
class VerifiedIdToken:
    """An ID token whose signature, issuer, audience and times have been checked.

    Parameters
    ----------
    claims
        Every claim of the token, for the caller's rules.
    issuer
        The issuer, exactly as configured.
    subject
        The ``sub`` claim.
    jti
        The token's id, or `None` when it has none and the rules did not require one; the
        caller must accept it once only.
    expires_at
        When the token expires (``exp``); the caller keeps the ``jti`` until then.
    """

    claims: Mapping[str, object]
    issuer: str
    subject: str
    jti: str | None
    expires_at: datetime


def _refuse_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, refusing a member that appears twice.

    Parameters
    ----------
    pairs
        The members in order.

    Returns
    -------
    dict[str, object]
        The object.

    Raises
    ------
    ValueError
        If a name repeats: parsers disagree on which one wins.
    """
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("a member appears twice")
    return result


def _segment(text: str) -> dict[str, object] | None:
    """Decode one base64url segment as a JSON object.

    Parameters
    ----------
    text
        The segment, without padding.

    Returns
    -------
    dict[str, object] or None
        The object, or `None` when the segment is not a JSON object in UTF-8 without repeated
        members (or is nested too deeply to parse).
    """
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_refuse_duplicates)
    except (ValueError, RecursionError):
        # RecursionError: a deeply nested array or object.
        return None
    return value if isinstance(value, dict) else None


def _header(token: str, algorithms: frozenset[str]) -> tuple[str, str, dict[str, object]]:
    """Check the token's shape and header (steps 1 to 4).

    Parameters
    ----------
    token
        The token.
    algorithms
        The accepted algorithms.

    Returns
    -------
    tuple[str, str, dict[str, object]]
        The algorithm, the key id and the unverified payload.

    Raises
    ------
    IdTokenRefused
        `TOKEN_TOO_LARGE`, `MALFORMED`, `ALGORITHM_NOT_ALLOWED`, `HEADER_NOT_ALLOWED` or
        `MISSING_KEY_ID`.
    """
    if not isinstance(token, str):
        raise IdTokenRefused(RefusalCode.MALFORMED)
    if len(token) > MAX_TOKEN_BYTES:
        raise IdTokenRefused(RefusalCode.TOKEN_TOO_LARGE)
    if not _COMPACT.fullmatch(token):
        raise IdTokenRefused(RefusalCode.MALFORMED)
    head, body, _ = token.split(".")
    header = _segment(head)
    payload = _segment(body)
    if header is None or payload is None:
        raise IdTokenRefused(RefusalCode.MALFORMED)
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in algorithms:
        raise IdTokenRefused(RefusalCode.ALGORITHM_NOT_ALLOWED)
    if any(name in header for name in _KEY_HEADERS):
        raise IdTokenRefused(RefusalCode.HEADER_NOT_ALLOWED)
    kid = header.get("kid")
    if not isinstance(kid, str) or not 0 < len(kid) <= _MAX_KID:
        raise IdTokenRefused(RefusalCode.MISSING_KEY_ID)
    return alg, kid, payload


def _check_signature(token: str, alg: str, keys: JwksCache, issuer: str, kid: str) -> None:
    """Find the key and verify the signature (steps 6 and 7).

    Parameters
    ----------
    token
        The token.
    alg
        Its allowed algorithm.
    keys
        The issuers' key cache.
    issuer
        Its configured issuer.
    kid
        Its key id.

    Raises
    ------
    IdTokenRefused
        `UNKNOWN_KEY`, `KEYS_UNAVAILABLE`, `KEY_MISMATCH`, `WEAK_KEY` or `BAD_SIGNATURE`.
    """
    key = keys.signing_key(issuer, kid)
    # Only RSA algorithms ever reach PyJWT, whatever the rules say: an HMAC "verified" with a
    # public key is the classic forgery.
    if alg not in SUPPORTED_ALGORITHMS or (key.alg is not None and key.alg != alg):
        raise IdTokenRefused(RefusalCode.KEY_MISMATCH)
    if key.bits < MIN_RSA_BITS:
        raise IdTokenRefused(RefusalCode.WEAK_KEY)
    verified = True
    try:
        _JWS.decode_complete(token, key=key.public_key, algorithms=[alg])
    except PyJWTError:
        verified = False
    if not verified:
        raise IdTokenRefused(RefusalCode.BAD_SIGNATURE)


def _check_audience(payload: Mapping[str, object], rules: IdTokenRules) -> None:
    """Require our audience, and our authorized party when the rules name one (9 and 10).

    Parameters
    ----------
    payload
        The claims.
    rules
        The caller's rules.

    Raises
    ------
    IdTokenRefused
        `AUDIENCE_MISMATCH` or `AUTHORIZED_PARTY_MISMATCH`.
    """
    aud = payload.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if not all(isinstance(item, str) for item in audiences) or rules.audience not in audiences:
        raise IdTokenRefused(RefusalCode.AUDIENCE_MISMATCH)
    if rules.authorized_party is None:
        return
    azp = payload.get("azp")
    if (azp is None and len(audiences) > 1) or (azp is not None and azp != rules.authorized_party):
        raise IdTokenRefused(RefusalCode.AUTHORIZED_PARTY_MISMATCH)


def _check_times(payload: Mapping[str, object], now: float, max_age_seconds: int) -> int:
    """Check ``exp``, ``nbf`` and ``iat`` (step 11).

    Parameters
    ----------
    payload
        The claims.
    now
        The time of the check, in seconds since the epoch.
    max_age_seconds
        The oldest token accepted, from ``iat``.

    Returns
    -------
    int
        The expiry.

    Raises
    ------
    IdTokenRefused
        `CLAIM_MISSING`, `EXPIRED`, `NOT_YET_VALID`, `TOO_OLD` or `LIFETIME_TOO_LONG`.
    """
    exp, iat, nbf = payload.get("exp"), payload.get("iat"), payload.get("nbf", 0)
    if not (isinstance(exp, int) and isinstance(iat, int) and isinstance(nbf, int)) or any(
        isinstance(value, bool) for value in (exp, iat, nbf)
    ):
        raise IdTokenRefused(RefusalCode.CLAIM_MISSING)
    if exp <= now - CLOCK_SKEW_SECONDS:
        raise IdTokenRefused(RefusalCode.EXPIRED)
    if nbf > now + CLOCK_SKEW_SECONDS or iat > now + CLOCK_SKEW_SECONDS:
        raise IdTokenRefused(RefusalCode.NOT_YET_VALID)
    if now - iat > max_age_seconds + CLOCK_SKEW_SECONDS:
        raise IdTokenRefused(RefusalCode.TOO_OLD)
    if exp - iat > MAX_LIFETIME_SECONDS:
        raise IdTokenRefused(RefusalCode.LIFETIME_TOO_LONG)
    return exp


def _check_nonce(payload: Mapping[str, object], nonce: str | None) -> None:
    """Require the sign-in's nonce when the rules name one (step 12).

    Parameters
    ----------
    payload
        The claims.
    nonce
        The expected nonce, or `None`.

    Raises
    ------
    IdTokenRefused
        `NONCE_MISMATCH`.
    """
    if nonce is None:
        return
    claimed = payload.get("nonce")
    if not isinstance(claimed, str) or not hmac.compare_digest(
        claimed.encode("utf-8"), nonce.encode("utf-8")
    ):
        raise IdTokenRefused(RefusalCode.NONCE_MISMATCH)


def verify_id_token(
    token: str,
    *,
    keys: JwksCache,
    issuers: frozenset[str],
    rules: IdTokenRules,
    now: datetime,
) -> VerifiedIdToken:
    """Verify an ID token.

    Parameters
    ----------
    token
        The compact JWT, as the issuer issued it.
    keys
        The issuers' key cache.
    issuers
        The issuers accepted, exactly as their tokens name them.
    rules
        The caller's rules.
    now
        The time of the check, with a time zone.

    Returns
    -------
    VerifiedIdToken
        The checked token. Its ``jti`` has not been checked for reuse.

    Raises
    ------
    IdTokenRefused
        With the code of the first check that failed, in the order of this module's
        docstring; `DISABLED` when no issuer is configured.
    ValueError
        If `now` has no time zone.
    """
    if now.tzinfo is None:
        raise ValueError("now must carry a time zone")
    if not issuers:
        raise IdTokenRefused(RefusalCode.DISABLED)
    alg, kid, payload = _header(token, rules.algorithms)
    issuer = payload.get("iss")
    if not isinstance(issuer, str) or issuer not in issuers:
        raise IdTokenRefused(RefusalCode.ISSUER_NOT_ALLOWED)
    _check_signature(token, alg, keys, issuer, kid)
    if rules.refuse_typ_other_than_id and payload.get("typ", "ID") != "ID":
        raise IdTokenRefused(RefusalCode.NOT_AN_ID_TOKEN)
    _check_audience(payload, rules)
    exp = _check_times(payload, now.timestamp(), rules.max_age_seconds)
    _check_nonce(payload, rules.nonce)
    subject, jti = payload.get("sub"), payload.get("jti")
    if not isinstance(subject, str) or not subject:
        raise IdTokenRefused(RefusalCode.CLAIM_MISSING)
    valid_jti = isinstance(jti, str) and 0 < len(jti) <= _MAX_JTI
    if rules.require_jti and not valid_jti:
        raise IdTokenRefused(RefusalCode.CLAIM_MISSING)
    return VerifiedIdToken(
        claims=payload,
        issuer=issuer,
        subject=subject,
        jti=jti if valid_jti and isinstance(jti, str) else None,
        expires_at=datetime.fromtimestamp(exp, UTC),
    )
