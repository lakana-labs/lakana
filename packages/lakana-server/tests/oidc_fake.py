"""A fake OIDC provider for the sign-in tests: discovery, keys and a token endpoint.

No test reaches the network. The provider serves its discovery document and key set through a
fetcher, "signs a person in" by handing out a code for an authorization URL the server built,
and answers the token endpoint with an ID token it signs, carrying the nonce of that URL, once
the PKCE verifier matches.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from lakana_core.net.https_json import FetchFailed
from lakana_core.oidc.config import OidcSignInConfig
from lakana_core.oidc.keys import JwksCache
from lakana_core.oidc.provider import OidcProvider, key_source

ISSUER = "https://idp.example.com/realms/lakana"
CLIENT_ID = "lakana-web"
CLIENT_SECRET = "s3cr3t-sentinel"  # nosec B105 -- a test value, looked for in logs and answers
DISCOVERY = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER}/protocol/openid-connect/certs"
AUTHORIZE = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN = f"{ISSUER}/protocol/openid-connect/token"


@cache
def signing_key() -> rsa.RSAPrivateKey:
    """Generate the provider's key once.

    Returns
    -------
    rsa.RSAPrivateKey
        The key.
    """
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def config(**changes: object) -> OidcSignInConfig:
    """Build the sign-in settings for the fake provider.

    Parameters
    ----------
    **changes
        Fields to change.

    Returns
    -------
    OidcSignInConfig
        The settings.
    """
    return OidcSignInConfig.model_validate(
        {"issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **changes}
    )


@dataclass
class Grant:
    """What the provider remembers of one code.

    Parameters
    ----------
    nonce
        The authorization request's nonce.
    challenge
        Its PKCE challenge.
    redirect_uri
        Its redirect URI.
    claims
        The claims of the ID token to answer.
    """

    nonce: str
    challenge: str
    redirect_uri: str
    claims: dict[str, object]


@dataclass
class FakeProvider:
    """The provider: its documents, the codes it handed out and every token request.

    Parameters
    ----------
    documents
        URL to document for the fetcher; a `FetchFailed` is raised instead of returned.
    """

    documents: dict[str, object] = field(default_factory=dict)
    grants: dict[str, Grant] = field(default_factory=dict)
    token_requests: list[tuple[str, dict[str, str], dict[str, str]]] = field(default_factory=list)
    token_answer: object = None
    """A fixed answer of the token endpoint (or `FetchFailed` to raise); `None` answers a code."""

    def __post_init__(self) -> None:
        public = json.loads(RSAAlgorithm.to_jwk(signing_key().public_key()))
        self.documents.setdefault(
            DISCOVERY,
            {
                "issuer": ISSUER,
                "jwks_uri": JWKS_URI,
                "authorization_endpoint": AUTHORIZE,
                "token_endpoint": TOKEN,
                "id_token_signing_alg_values_supported": ["RS256"],
            },
        )
        self.documents.setdefault(JWKS_URI, {"keys": [{**public, "kid": "k1", "alg": "RS256"}]})

    def fetch(self, url: str, allowed_hosts: Collection[str]) -> object:
        """Answer a fetch of discovery or keys.

        Parameters
        ----------
        url
            The URL.
        allowed_hosts
            Unused.

        Returns
        -------
        object
            The document.

        Raises
        ------
        FetchFailed
            When the document is a `FetchFailed`, or unknown.
        """
        del allowed_hosts
        document = self.documents.get(url, FetchFailed("not found"))
        if isinstance(document, FetchFailed):
            raise document
        return document

    def post(
        self,
        url: str,
        form: Mapping[str, str],
        headers: Mapping[str, str],
        allowed_hosts: Collection[str],
    ) -> object:
        """Answer the token endpoint.

        Parameters
        ----------
        url
            The URL.
        form
            The form.
        headers
            The headers.
        allowed_hosts
            Unused.

        Returns
        -------
        object
            ``{"id_token", "access_token", "token_type"}`` for a known code whose verifier and
            redirect URI match.

        Raises
        ------
        FetchFailed
            With status 400 for an unknown code or a wrong verifier, or as `token_answer` says.
        """
        del allowed_hosts
        self.token_requests.append((url, dict(form), dict(headers)))
        if isinstance(self.token_answer, FetchFailed):
            raise self.token_answer
        if self.token_answer is not None:
            return self.token_answer
        grant = self.grants.pop(form.get("code", ""), None)
        verifier = form.get("code_verifier", "")
        if (
            grant is None
            or s256(verifier) != grant.challenge
            or form.get("redirect_uri") != grant.redirect_uri
        ):
            raise FetchFailed("the server answered 400", status=400)
        return {
            "id_token": sign({"nonce": grant.nonce, **grant.claims}),
            "access_token": sign({"typ": "Bearer", "aud": "account"}),
            "token_type": "Bearer",
        }

    def authorize(self, authorization_url: str, **claims: object) -> tuple[str, str]:
        """Sign a person in at the provider: hand out a code for an authorization URL.

        Parameters
        ----------
        authorization_url
            What the server's `start` answered.
        **claims
            Claims of the ID token to change; `None` removes one.

        Returns
        -------
        tuple of (str, str)
            The code and the state, as the provider sends them back to the callback.
        """
        parsed = parse_qs(urlsplit(authorization_url).query)
        query = {name: values[0] for name, values in parsed.items()}
        code = f"code-{len(self.grants)}-{query['state'][:8]}"
        self.grants[code] = Grant(
            nonce=query["nonce"],
            challenge=query["code_challenge"],
            redirect_uri=query["redirect_uri"],
            claims=dict(claims),
        )
        return code, query["state"]

    def provider(
        self,
        settings: OidcSignInConfig | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> OidcProvider:
        """Build the server's provider client over this fake.

        Parameters
        ----------
        settings
            The sign-in settings; `config()` by default.
        clock
            The key cache's monotonic clock; the real one by default.

        Returns
        -------
        OidcProvider
            The client.
        """
        settings = settings or config()
        keys = JwksCache(key_source(settings), fetcher=self.fetch, clock=clock or time.monotonic)
        return OidcProvider(settings, keys=keys, poster=self.post)


def s256(verifier: str) -> str:
    """Compute a PKCE S256 challenge, independently of the server's code.

    Parameters
    ----------
    verifier
        The verifier.

    Returns
    -------
    str
        The challenge.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sign(changes: Mapping[str, object]) -> str:
    """Sign an ID token for `CLIENT_ID`, issued now.

    Parameters
    ----------
    changes
        Claims to set; `None` removes one.

    Returns
    -------
    str
        The token.
    """
    now = int(datetime.now(UTC).timestamp())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": "248289761001",
        "email": "alice@example.com",
        "name": "Alice Example",
        "iat": now - 5,
        "exp": now + 300,
        "typ": "ID",
    }
    claims.update(changes)
    payload = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(payload, signing_key(), algorithm="RS256", headers={"kid": "k1"})
