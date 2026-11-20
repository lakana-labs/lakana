"""The shared ID-token checks (`lakana_core.oidc.id_token`) under the sign-in's rules.

CI's binding is covered, unchanged, by `test_ci_oidc_verification.py`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import jwt
import pytest
from ci_oidc_test_helpers import ISSUER, FakeFetcher, jwk, rsa_key

from lakana_core.oidc.errors import IdTokenRefused, RefusalCode
from lakana_core.oidc.id_token import IdTokenRules, VerifiedIdToken, verify_id_token
from lakana_core.oidc.keys import JwksCache, KeySource

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
T = int(NOW.timestamp())
CLIENT = "lakana-web"

RULES = IdTokenRules(
    audience=CLIENT,
    algorithms=frozenset({"RS256"}),
    max_age_seconds=600,
    require_jti=False,
    authorized_party=CLIENT,
    nonce="n-0123456789",
    refuse_typ_other_than_id=True,
)


def claims(**changes: object) -> dict[str, object]:
    """Return the claims of a good sign-in ID token, with changes.

    Parameters
    ----------
    **changes
        Claims to set; `None` removes one.

    Returns
    -------
    dict[str, object]
        The claims.
    """
    base: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT,
        "sub": "248289761001",
        "nonce": "n-0123456789",
        "iat": T - 5,
        "exp": T + 300,
    }
    base.update(changes)
    return {name: value for name, value in base.items() if value is not None}


def keys() -> JwksCache:
    """Serve the main key as ``k1`` at `ISSUER`.

    Returns
    -------
    JwksCache
        A cache over a fake fetcher.
    """
    fetcher = FakeFetcher()
    fetcher.publish(jwk(rsa_key("main"), "k1", alg="RS256"))
    source = KeySource(
        issuers=frozenset({ISSUER}),
        allow_hosts=frozenset(),
        proxy=None,
        ca_bundle=None,
        label="OIDC sign-in",
        allow_hosts_variable="LAKANA_OIDC__ALLOW_HOSTS",
    )
    return JwksCache(source, fetcher=fetcher)


def verify(payload: dict[str, object], rules: IdTokenRules = RULES) -> VerifiedIdToken:
    """Sign and verify a token.

    Parameters
    ----------
    payload
        The claims.
    rules
        The rules.

    Returns
    -------
    VerifiedIdToken
        The verified token.
    """
    token = jwt.encode(payload, rsa_key("main"), algorithm="RS256", headers={"kid": "k1"})
    return verify_id_token(token, keys=keys(), issuers=frozenset({ISSUER}), rules=rules, now=NOW)


def refusal(payload: dict[str, object], rules: IdTokenRules = RULES) -> RefusalCode:
    """Return the code a token is refused with.

    Parameters
    ----------
    payload
        The claims.
    rules
        The rules.

    Returns
    -------
    RefusalCode
        The code.
    """
    with pytest.raises(IdTokenRefused) as info:
        verify(payload, rules)
    assert "LAKANA_CI_OIDC" not in info.value.message
    return info.value.code


def test_a_good_sign_in_token_is_verified() -> None:
    verified = verify(claims(jti="abc"))
    assert verified.subject == "248289761001"
    assert verified.jti == "abc"


def test_a_token_without_jti_passes_when_jti_is_not_required() -> None:
    assert verify(claims()).jti is None
    assert refusal(claims(), replace(RULES, require_jti=True)) is RefusalCode.CLAIM_MISSING


def test_azp_must_be_ours_when_present() -> None:
    assert refusal(claims(azp="other")) is RefusalCode.AUTHORIZED_PARTY_MISMATCH
    assert verify(claims(azp=CLIENT)).subject == "248289761001"


def test_two_audiences_need_azp() -> None:
    both = [CLIENT, "another-client"]
    assert refusal(claims(aud=both)) is RefusalCode.AUTHORIZED_PARTY_MISMATCH
    assert verify(claims(aud=both, azp=CLIENT)).subject == "248289761001"
    assert refusal(claims(aud="another-client")) is RefusalCode.AUDIENCE_MISMATCH


def test_azp_is_not_checked_without_an_authorized_party() -> None:
    loose = replace(RULES, authorized_party=None)
    assert verify(claims(azp="other"), loose).subject == "248289761001"


def test_nonce_must_match() -> None:
    assert refusal(claims(nonce="n-other")) is RefusalCode.NONCE_MISMATCH
    assert refusal(claims(nonce=None)) is RefusalCode.NONCE_MISMATCH
    assert refusal(claims(nonce=7)) is RefusalCode.NONCE_MISMATCH


def test_typ_other_than_id_is_refused() -> None:
    assert refusal(claims(typ="Bearer")) is RefusalCode.NOT_AN_ID_TOKEN
    assert verify(claims(typ="ID")).subject == "248289761001"
    loose = replace(RULES, refuse_typ_other_than_id=False)
    assert verify(claims(typ="Bearer"), loose).subject == "248289761001"


@pytest.mark.parametrize("typ", ["at+jwt", "AT+JWT", "application/at+jwt"])
def test_an_rfc_9068_access_token_is_refused_by_its_header(typ: str) -> None:
    token = jwt.encode(
        claims(), rsa_key("main"), algorithm="RS256", headers={"kid": "k1", "typ": typ}
    )
    with pytest.raises(IdTokenRefused) as info:
        verify_id_token(token, keys=keys(), issuers=frozenset({ISSUER}), rules=RULES, now=NOW)
    assert info.value.code is RefusalCode.NOT_AN_ID_TOKEN
    loose = replace(RULES, refuse_typ_other_than_id=False)
    verified = verify_id_token(
        token, keys=keys(), issuers=frozenset({ISSUER}), rules=loose, now=NOW
    )
    assert verified.subject == "248289761001"
    plain = jwt.encode(
        claims(), rsa_key("main"), algorithm="RS256", headers={"kid": "k1", "typ": "JWT"}
    )
    assert verify_id_token(plain, keys=keys(), issuers=frozenset({ISSUER}), rules=RULES, now=NOW)


def test_iat_older_than_the_rules_allow_is_too_old() -> None:
    assert refusal(claims(iat=T - 700, exp=T + 300)) is RefusalCode.TOO_OLD


def test_no_issuers_means_disabled() -> None:
    token = jwt.encode(claims(), rsa_key("main"), algorithm="RS256", headers={"kid": "k1"})
    with pytest.raises(IdTokenRefused) as info:
        verify_id_token(token, keys=keys(), issuers=frozenset(), rules=RULES, now=NOW)
    assert info.value.code is RefusalCode.DISABLED
