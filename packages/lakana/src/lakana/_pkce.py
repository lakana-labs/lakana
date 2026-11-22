"""PKCE (RFC 7636) for `lakana login`: the S256 challenge of a verifier."""

from __future__ import annotations

import base64
import hashlib


def s256(verifier: str) -> str:
    """Compute the S256 challenge of a PKCE verifier.

    Parameters
    ----------
    verifier
        The verifier: 43 to 128 unreserved characters.

    Returns
    -------
    str
        The unpadded base64url of its SHA-256.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
