"""The refusals of an ID token, each with a stable code.

The code is for the caller (an audit row, an answer); the message is for a person. Neither ever
contains the token or a value taken from it. The messages here name no setting: a binding that
does (`lakana_core.ci_oidc.errors`) raises a subclass with its own.
"""

from __future__ import annotations

import enum


class RefusalCode(enum.StrEnum):
    """Why an ID token was refused; the values are stable."""

    DISABLED = "disabled"
    TOKEN_TOO_LARGE = "token_too_large"  # nosec B105 -- a refusal code, not a credential
    MALFORMED = "malformed"
    ALGORITHM_NOT_ALLOWED = "algorithm_not_allowed"
    HEADER_NOT_ALLOWED = "header_not_allowed"
    MISSING_KEY_ID = "missing_key_id"
    ISSUER_NOT_ALLOWED = "issuer_not_allowed"
    UNKNOWN_KEY = "unknown_key"
    KEYS_UNAVAILABLE = "keys_unavailable"
    KEY_MISMATCH = "key_mismatch"
    WEAK_KEY = "weak_key"
    BAD_SIGNATURE = "bad_signature"
    AUDIENCE_MISMATCH = "audience_mismatch"
    CLAIM_MISSING = "claim_missing"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    TOO_OLD = "too_old"
    LIFETIME_TOO_LONG = "lifetime_too_long"
    AUTHORIZED_PARTY_MISMATCH = "authorized_party_mismatch"
    NONCE_MISMATCH = "nonce_mismatch"
    NOT_AN_ID_TOKEN = "not_an_id_token"  # nosec B105 -- a refusal code, not a credential


MESSAGES = {
    RefusalCode.DISABLED: "OIDC is off on this server",
    RefusalCode.TOKEN_TOO_LARGE: "the ID token is larger than 16 KiB",
    RefusalCode.MALFORMED: "the ID token is not a well-formed signed JWT",
    RefusalCode.ALGORITHM_NOT_ALLOWED: (
        "the ID token's signature algorithm is not one this server accepts from the issuer"
    ),
    RefusalCode.HEADER_NOT_ALLOWED: (
        "the ID token's header names a key or a critical extension (jku, x5u, x5c, jwk, crit), "
        "which is never accepted: the keys come from the issuer only"
    ),
    RefusalCode.MISSING_KEY_ID: "the ID token's header has no usable key id (kid)",
    RefusalCode.ISSUER_NOT_ALLOWED: "the ID token's issuer (iss) is not the configured issuer",
    RefusalCode.UNKNOWN_KEY: "the issuer's published keys have no key with the ID token's key id",
    RefusalCode.KEYS_UNAVAILABLE: "the issuer's keys could not be fetched",
    RefusalCode.KEY_MISMATCH: "the issuer's key with this key id is not for this algorithm",
    RefusalCode.WEAK_KEY: "the issuer's key with this key id is an RSA key under 2048 bits",
    RefusalCode.BAD_SIGNATURE: "the ID token's signature does not verify with the issuer's key",
    RefusalCode.AUDIENCE_MISMATCH: "the ID token was issued for another client (aud)",
    RefusalCode.CLAIM_MISSING: "the ID token lacks a required claim or has one of the wrong type",
    RefusalCode.EXPIRED: "the ID token has expired",
    RefusalCode.NOT_YET_VALID: (
        "the ID token is not valid yet; check the clocks of the server and the issuer"
    ),
    RefusalCode.TOO_OLD: "the ID token was issued too long ago",
    RefusalCode.LIFETIME_TOO_LONG: "the ID token claims a lifetime longer than 24 hours",
    RefusalCode.AUTHORIZED_PARTY_MISMATCH: (
        "the ID token was issued to another client (azp), or names several audiences without "
        "naming this client as its authorized party"
    ),
    RefusalCode.NONCE_MISMATCH: "the ID token was issued for another sign-in (nonce)",
    RefusalCode.NOT_AN_ID_TOKEN: "the token is not an ID token (typ)",
}
"""The message of each code, naming no setting."""


class IdTokenRefused(Exception):
    """An ID token was refused.

    Parameters
    ----------
    code
        Why; stable, for audit rows and answers.
    message
        The sentence for a person; `MESSAGES` of the code by default.
    """

    def __init__(self, code: RefusalCode, message: str | None = None) -> None:
        shown = MESSAGES[code] if message is None else message
        super().__init__(shown)
        self.code = code
        self.message = shown
