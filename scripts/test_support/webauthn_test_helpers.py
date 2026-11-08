"""A software WebAuthn authenticator for the passkey tests.

It answers the options the server sends with real attestations and assertions: an ES256 key
made with `cryptography`, a ``clientDataJSON`` with the origin the test chooses, an
``authenticatorData`` with the relying-party id hash the test chooses, the user-present and
user-verified flags switchable, a signature counter, and ``none`` attestation. The server
verifies them with its real WebAuthn library, never a mock.

Shared by the core's and the server's passkey tests, so it lives in `scripts/test_support`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

UP = 0x01
"""The user-present flag of ``authenticatorData``."""

UV = 0x04
"""The user-verified flag."""

BE = 0x08
"""The backup-eligible flag (a synced passkey)."""

AT = 0x40
"""The attested-credential-data flag (set on registration)."""

_SAME = object()
"""Marks a keyword argument the caller did not give."""


def b64url(data: bytes) -> str:
    """Encode bytes as base64url without padding, as WebAuthn's JSON does.

    Parameters
    ----------
    data
        The bytes.

    Returns
    -------
    str
        The text.
    """
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    """Decode base64url with or without padding.

    Parameters
    ----------
    text
        The text.

    Returns
    -------
    bytes
        The bytes.
    """
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftAuthenticator:
    """One passkey on a software authenticator.

    Parameters
    ----------
    origin
        The origin the "browser" puts into ``clientDataJSON`` unless a call overrides it.
    rp_id
        The relying-party id whose hash goes into ``authenticatorData`` unless a call
        overrides it; by default the id the options name.
    counts
        Whether the signature counter goes up with every assertion; `False` is a synced
        passkey, whose counter stays 0.
    backup_eligible
        Whether the backup-eligible flag is set.
    """

    def __init__(
        self,
        *,
        origin: str = "http://localhost:8000",
        rp_id: str | None = None,
        counts: bool = True,
        backup_eligible: bool = False,
    ) -> None:
        self.origin = origin
        self.rp_id = rp_id
        self.counts = counts
        self.backup_eligible = backup_eligible
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0
        self.user_handle: bytes | None = None

    @property
    def credential_id_text(self) -> str:
        """The credential id as the server stores it (base64url).

        Returns
        -------
        str
            The id.
        """
        return b64url(self.credential_id)

    def _cose_key(self) -> bytes:
        """Encode the public key as a COSE EC2 key (ES256, P-256).

        Returns
        -------
        bytes
            The CBOR map.
        """
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _client_data(self, kind: str, options: dict[str, Any], origin: str | None) -> bytes:
        """Build ``clientDataJSON``.

        Parameters
        ----------
        kind
            ``webauthn.create`` or ``webauthn.get``.
        options
            The server's options, for the challenge.
        origin
            The origin to claim; `self.origin` when `None`.

        Returns
        -------
        bytes
            The JSON.
        """
        return json.dumps(
            {
                "type": kind,
                "challenge": options["challenge"],
                "origin": origin or self.origin,
                "crossOrigin": False,
            }
        ).encode("utf-8")

    def _rp_hash(self, options: dict[str, Any], rp_id: str | None) -> bytes:
        """Hash the relying-party id the authenticator claims.

        Parameters
        ----------
        options
            The server's options: registration options name it under ``rp.id``,
            authentication options under ``rpId``.
        rp_id
            An override.

        Returns
        -------
        bytes
            SHA-256 of the id.
        """
        named = options.get("rpId") or options.get("rp", {}).get("id")
        return hashlib.sha256((rp_id or self.rp_id or named).encode("utf-8")).digest()

    def _flags(self, *, up: bool, uv: bool, extra: int = 0) -> int:
        """Combine the flags byte.

        Parameters
        ----------
        up
            User present.
        uv
            User verified.
        extra
            Further bits.

        Returns
        -------
        int
            The byte.
        """
        flags = extra
        if up:
            flags |= UP
        if uv:
            flags |= UV
        if self.backup_eligible:
            flags |= BE
        return flags

    def create(
        self,
        options: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        uv: bool = True,
        up: bool = True,
    ) -> str:
        """Answer registration options, as `navigator.credentials.create` and `toJSON()` would.

        Parameters
        ----------
        options
            The server's ``publicKey`` options (JSON form).
        origin
            The origin to claim instead of `self.origin`.
        rp_id
            The relying-party id to hash instead of the one the options name.
        uv
            Whether the user-verified flag is set.
        up
            Whether the user-present flag is set.

        Returns
        -------
        str
            The registration credential's JSON.
        """
        self.user_handle = unb64url(options["user"]["id"])
        client_data = self._client_data("webauthn.create", options, origin)
        attested = (
            bytes(16)
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self._cose_key()
        )
        auth_data = (
            self._rp_hash(options, rp_id)
            + bytes([self._flags(up=up, uv=uv, extra=AT)])
            + struct.pack(">I", self.sign_count)
            + attested
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return json.dumps(
            {
                "id": self.credential_id_text,
                "rawId": self.credential_id_text,
                "type": "public-key",
                "response": {
                    "clientDataJSON": b64url(client_data),
                    "attestationObject": b64url(attestation),
                    "transports": ["internal"],
                },
                "clientExtensionResults": {},
                "authenticatorAttachment": "platform",
            }
        )

    def get(
        self,
        options: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        uv: bool = True,
        up: bool = True,
        sign_count: int | None = None,
        user_handle: Any = _SAME,  # ruff: ignore[any-type]
        forge: bool = False,
    ) -> str:
        """Answer authentication options, as `navigator.credentials.get` and `toJSON()` would.

        Parameters
        ----------
        options
            The server's ``publicKey`` options (JSON form).
        origin
            The origin to claim instead of `self.origin`.
        rp_id
            The relying-party id to hash instead of the one the options name.
        uv
            Whether the user-verified flag is set.
        up
            Whether the user-present flag is set.
        sign_count
            The counter to report; by default the next one (or 0 for a passkey that does not
            count).
        user_handle
            The user handle to send (bytes, or `None` for none); by default the one the
            registration options named.
        forge
            Sign with a different key, as someone without this passkey would.

        Returns
        -------
        str
            The authentication credential's JSON.
        """
        if sign_count is None:
            if self.counts:
                self.sign_count += 1
            sign_count = self.sign_count
        client_data = self._client_data("webauthn.get", options, origin)
        auth_data = (
            self._rp_hash(options, rp_id)
            + bytes([self._flags(up=up, uv=uv)])
            + struct.pack(">I", sign_count)
        )
        key = ec.generate_private_key(ec.SECP256R1()) if forge else self.key
        signature = key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        handle = self.user_handle if user_handle is _SAME else user_handle
        response: dict[str, Any] = {
            "clientDataJSON": b64url(client_data),
            "authenticatorData": b64url(auth_data),
            "signature": b64url(signature),
        }
        if handle is not None:
            response["userHandle"] = b64url(handle)
        return json.dumps(
            {
                "id": self.credential_id_text,
                "rawId": self.credential_id_text,
                "type": "public-key",
                "response": response,
                "clientExtensionResults": {},
                "authenticatorAttachment": "platform",
            }
        )
