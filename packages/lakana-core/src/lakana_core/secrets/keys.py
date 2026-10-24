"""The master key that wraps every secret's own per-secret data key."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from abc import ABC, abstractmethod

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from lakana_core.errors import LakanaError

_NONCE_SIZE = 12
"""AES-GCM's recommended nonce size in bytes (96 bits)."""

_KEY_SIZE = 32
"""AES-256's key size in bytes."""

_PROVIDER_NAME = "env"
"""`EnvKeyProvider`'s name, the first part of its `key_id`."""

_FINGERPRINT_LABEL = b"lakana.key-id.v1"
"""Domain separation for the key fingerprint: the HMAC message."""

_FINGERPRINT_BYTES = 8
"""Bytes of the HMAC kept in the fingerprint (16 hex characters)."""


class UnwrapError(LakanaError):
    """A wrapped data key failed authentication.

    Raised by `KeyProvider.unwrap` when the wrapped bytes do not decrypt
    under the configured master key *with the given associated data*: the
    key is wrong, the bytes were altered, or they belong to another row.
    The message does not say which of those it was.
    """


class KeyProvider(ABC):
    """Wraps and unwraps a per-secret data key under a deployment's master key.

    A thin interface so a future cloud-KMS-backed implementation can
    replace `EnvKeyProvider` without touching anything above it —
    `secrets.crypto`, `SecretService`, and `StorageResolver` all depend on
    this interface, never on a concrete provider.
    """

    @property
    def accepts_format_1(self) -> bool:
        """Say whether this process reads secrets stored in the old format (1).

        The process's configuration (`LAKANA_SECRETS__ACCEPT_FORMAT_1`), carried
        by the provider because every place that decrypts already has one.
        The five decrypt sites pass it to `decrypt_secret_row`; it is never read
        from the database.

        Returns
        -------
        bool
            `False` unless a subclass is built with the setting on.
        """
        return False

    @property
    @abstractmethod
    def key_id(self) -> str:
        """Name the provider and the key it wraps data keys under.

        Stored on every secret's row, so a second provider, or a new
        version of a provider's key material, can tell old rows from new
        ones. It must identify the key without revealing it.

        Returns
        -------
        str
            A short, stable identifier: the provider's name, optionally
            followed by ``:`` and a fingerprint of the key material.

        Raises
        ------
        LakanaError
            If no usable master key is configured.
        """

    @abstractmethod
    def wrap(self, data_key: bytes, associated_data: bytes | None = None) -> bytes:
        """Encrypt a data key under the master key.

        Parameters
        ----------
        data_key
            The raw data key to wrap.
        associated_data
            Bytes the wrapped key is bound to: `unwrap` must be given the
            same bytes or it fails. `None` binds it to nothing.

        Returns
        -------
        bytes
            The wrapped (encrypted) data key, self-contained — it carries
            whatever nonce or metadata `unwrap` needs to reverse it.

        Raises
        ------
        LakanaError
            If no usable master key is configured.
        """

    @abstractmethod
    def unwrap(self, wrapped: bytes, associated_data: bytes | None = None) -> bytes:
        """Decrypt a data key that `wrap` produced.

        Parameters
        ----------
        wrapped
            The wrapped data key, exactly as `wrap` returned it.
        associated_data
            The bytes `wrap` was given.

        Returns
        -------
        bytes
            The original raw data key.

        Raises
        ------
        UnwrapError
            If `wrapped` cannot be decrypted under the configured master
            key with this associated data (wrong key, altered bytes, or
            other associated data).
        LakanaError
            If no usable master key is configured.
        """


class EnvKeyProvider(KeyProvider):
    """Wrap and unwrap data keys using a single base64-encoded master key.

    A `KeyProvider` backed by a single master key, given directly (not
    read from the environment). Named for where the key ultimately comes
    from in a real deployment (`LAKANA_SECRETS__MASTER_KEY`, read once into
    `lakana_core.config.Settings` at startup) — this class itself never
    reads `os.environ`, matching this codebase's convention of reading
    environment variables exactly once, into typed settings.

    Parameters
    ----------
    master_key
        Base64-encoded 32-byte master key, or `None` if none is
        configured. `None` is a valid, common state (most local/dev
        deployments never touch secrets) — it only becomes an error at
        the point `wrap`/`unwrap` is actually called.
    accept_format_1
        Whether this process reads secrets stored in the old format
        (`LAKANA_SECRETS__ACCEPT_FORMAT_1`). Off by default.
    """

    def __init__(self, master_key: str | None, *, accept_format_1: bool = False) -> None:
        self._master_key_b64 = master_key
        self._accept_format_1 = accept_format_1

    @property
    def accepts_format_1(self) -> bool:
        """Say whether this process reads secrets stored in the old format (1).

        Returns
        -------
        bool
            The `accept_format_1` this provider was built with.
        """
        return self._accept_format_1

    @property
    def key_id(self) -> str:
        """Name this provider and fingerprint its master key.

        The fingerprint is the first 8 bytes of ``HMAC-SHA256(master key,
        "lakana.key-id.v1")``: one-way, so it does not reveal the key. It
        gives no offline guess a verifier that someone who can read the
        database did not already have: every stored `wrapped_data_key` is
        a GCM ciphertext under the master key, and a wrong guess already
        fails its tag. Someone who may only list secrets never saw a wrapped
        key, so for them the identifier is a verifier: it relies on a
        full-entropy master key, as `lakana bundle init` generates (256
        bits), and would let a guess at a weak one be checked offline.

        Returns
        -------
        str
            ``env:`` followed by 16 hexadecimal characters.

        Raises
        ------
        LakanaError
            If no master key is configured, or it is malformed.
        """
        digest = hmac.new(self._decoded_key(), _FINGERPRINT_LABEL, hashlib.sha256).digest()
        return f"{_PROVIDER_NAME}:{digest[:_FINGERPRINT_BYTES].hex()}"

    def _decoded_key(self) -> bytes:
        """Decode and validate the configured master key.

        Returns
        -------
        bytes
            The raw 32-byte master key.

        Raises
        ------
        LakanaError
            If no master key is configured, it is not valid base64, or it
            does not decode to exactly 32 bytes.
        """
        if self._master_key_b64 is None:
            raise LakanaError("No secrets master key is configured (LAKANA_SECRETS__MASTER_KEY).")
        try:
            decoded = base64.b64decode(self._master_key_b64, validate=True)
        except Exception as exc:
            raise LakanaError("The configured secrets master key is not valid base64.") from exc
        if len(decoded) != _KEY_SIZE:
            raise LakanaError(
                f"The configured secrets master key must decode to {_KEY_SIZE} bytes, "
                f"got {len(decoded)}."
            )
        return decoded

    def wrap(self, data_key: bytes, associated_data: bytes | None = None) -> bytes:
        """Encrypt a data key under the master key.

        Parameters
        ----------
        data_key
            The raw data key to wrap.
        associated_data
            Authenticated with the wrapped key, not stored in it.

        Returns
        -------
        bytes
            The nonce, followed by the AES-256-GCM-encrypted data key —
            self-contained, so `unwrap` needs nothing else.

        Raises
        ------
        LakanaError
            If no usable master key is configured.
        """
        master_key = self._decoded_key()
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext = AESGCM(master_key).encrypt(nonce, data_key, associated_data)
        return nonce + ciphertext

    def unwrap(self, wrapped: bytes, associated_data: bytes | None = None) -> bytes:
        """Decrypt a data key that `wrap` produced.

        Parameters
        ----------
        wrapped
            The wrapped data key, exactly as `wrap` returned it.
        associated_data
            The bytes `wrap` was given.

        Returns
        -------
        bytes
            The original raw data key.

        Raises
        ------
        UnwrapError
            If `wrapped` cannot be decrypted under the configured master
            key with this associated data.
        LakanaError
            If no usable master key is configured.
        """
        master_key = self._decoded_key()
        nonce, ciphertext = wrapped[:_NONCE_SIZE], wrapped[_NONCE_SIZE:]
        try:
            return AESGCM(master_key).decrypt(nonce, ciphertext, associated_data)
        except (InvalidTag, ValueError) as exc:
            raise UnwrapError(
                "Could not decrypt the wrapped data key under the configured master key."
            ) from exc
