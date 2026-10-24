"""Encrypting and decrypting a secret's key-value bag.

Pure functions: no session, no principal, no authorization. This is
deliberate — see `SecretService`'s own module for why decryption must
never be reachable through a principal-facing method, and
`StorageResolver` for one of the four places `decrypt_secret_row` is
actually called.

Stored format
-------------
Format 1 (every secret written before format 2 existed) authenticates
nothing but the bytes themselves: it passes no associated data to
AES-GCM, so the triple of one secret decrypts just as well on another
secret's row. Format 2, the only one written now, binds both layers (the
values under the data key, and the data key under the master key) to the
row by passing the same helper's output as associated data: see
`associated_data`. A format-1 row is
refused unless the caller says it accepts them (`LAKANA_SECRETS__ACCEPT_FORMAT_1`, read
once into `lakana_core.config.SecretsConfig`; this module never reads the environment). The
format version is a column of the row, so anyone who can write the database can paste an
old-format triple onto any row and set it to 1: only the setting, which lives outside the
database, keeps that read closed. A row becomes format 2 the next time the secret is written.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import uuid
from dataclasses import dataclass
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from lakana_core.errors import LakanaError
from lakana_core.models import Secret
from lakana_core.secrets.keys import KeyProvider, UnwrapError

_logger = logging.getLogger(__name__)

_NONCE_SIZE = 12
"""AES-GCM's recommended nonce size in bytes (96 bits)."""

_DATA_KEY_SIZE_BITS = 256
"""AES-256's key size."""

LEGACY_FORMAT = 1
"""Format 1: no associated data on either layer. Read, never written."""

CURRENT_FORMAT = 2
"""Format 2: both layers bound to the row. The only format written."""

_LENGTH_BYTES = 4
"""Width of each field's length prefix in the associated data."""

Purpose = Literal["values", "wrap"]
"""Which of the two encryption layers an associated-data string is for."""


ACCEPT_FORMAT_1_WARNING = (
    "LAKANA_SECRETS__ACCEPT_FORMAT_1 is on: secrets stored in the old format (1) are read. "
    "That weakens the binding of every secret to its row: any old-format ciphertext written "
    "under this master key is accepted on any row. To finish: `lakana secret list --env ENV` "
    "shows each secret's format_version; rotate each secret whose format_version is 1 with "
    "`lakana secret rotate NAME --env ENV --values-stdin`; then remove the setting from the "
    "server and from every worker."
)
"""The start-up WARNING of a process that reads format 1: the server and the worker log it."""


class SecretFormatError(LakanaError):
    """A stored secret's format version is not one this code reads.

    Parameters
    ----------
    format_version
        The version found on the row.

    Attributes
    ----------
    format_version : int
        The version found on the row.
    """

    def __init__(self, format_version: int) -> None:
        super().__init__(f"Unknown secret format version {format_version}.")
        self.format_version = format_version


class LegacyFormatRefusedError(SecretFormatError):
    """A format-1 secret was found and the caller does not accept that format.

    Raised before anything is unwrapped or decrypted.
    """

    def __init__(self) -> None:
        super().__init__(LEGACY_FORMAT)
        self.args = ("Secrets in the old format (1) are not accepted by this configuration.",)


@dataclass(frozen=True)
class SecretBinding:
    """The columns of a secret's row that decide whose secret it is.

    Parameters
    ----------
    secret_id
        The row's id.
    environment_id
        The id of the environment the secret is scoped to.
    name
        The secret's name, the key an environment's reference resolves by.
        Secrets have no rename operation. One added later must decrypt and
        re-encrypt the secret, which writes it as a new format-2 row value.
    """

    secret_id: uuid.UUID
    environment_id: uuid.UUID
    name: str


@dataclass(frozen=True)
class EncryptedSecret:
    """The pieces persisted for one encrypted secret.

    Parameters
    ----------
    ciphertext
        The JSON-serialized key-value bag, encrypted under the data key.
    nonce
        The nonce used for that encryption.
    wrapped_data_key
        The data key, wrapped under the deployment's master key
        (`KeyProvider.wrap`) — itself self-contained (carries its own
        nonce internally, see `EnvKeyProvider.wrap`).
    format_version
        How the secret was encrypted: `LEGACY_FORMAT` or `CURRENT_FORMAT`.
    key_id
        `KeyProvider.key_id` at the time the data key was wrapped. Part of
        the associated data in format 2.
    """

    ciphertext: bytes
    nonce: bytes
    wrapped_data_key: bytes
    format_version: int
    key_id: str


def _field(data: bytes) -> bytes:
    """Prefix `data` with its length, so field boundaries cannot move.

    Parameters
    ----------
    data
        One field's bytes.

    Returns
    -------
    bytes
        The length as a 4-byte big-endian integer, then `data`.
    """
    return len(data).to_bytes(_LENGTH_BYTES, "big") + data


def associated_data(
    purpose: Purpose, format_version: int, binding: SecretBinding, key_id: str
) -> bytes:
    """Build the associated data that binds one encryption layer to a row.

    The one place the bytes are built, for encryption and decryption alike.
    Five fields, each a 4-byte big-endian length then its bytes: the
    context string ``lakana.secret.<purpose>.v<format_version>``, the
    secret's id (16 bytes), the environment's id (16 bytes), the name
    (UTF-8) and the key id (UTF-8). Every field is length-prefixed, so two
    different inputs never produce the same bytes. The purpose keeps the
    two layers apart too: the wrapped data key cannot stand in for the
    values or the reverse.

    Parameters
    ----------
    purpose
        ``"values"`` for the key-value bag, ``"wrap"`` for the data key.
    format_version
        The format the bytes are for.
    binding
        The row's id, environment and name.
    key_id
        The identifier of the key that wraps the data key.

    Returns
    -------
    bytes
        The associated data.
    """
    return b"".join(
        (
            _field(f"lakana.secret.{purpose}.v{format_version}".encode()),
            _field(binding.secret_id.bytes),
            _field(binding.environment_id.bytes),
            _field(binding.name.encode()),
            _field(key_id.encode()),
        )
    )


def encrypt_secret_values(
    values: dict[str, str], key_provider: KeyProvider, binding: SecretBinding
) -> EncryptedSecret:
    """Encrypt a key-value bag under a freshly generated data key, bound to a row.

    Parameters
    ----------
    values
        The plaintext key-value bag to encrypt.
    key_provider
        Wraps the freshly generated data key under the deployment's
        master key.
    binding
        The row the result will be stored on.

    Returns
    -------
    EncryptedSecret
        The pieces to persist, as `CURRENT_FORMAT`. Calling this twice
        with the same `values` produces different output each time — a
        fresh data key and a fresh nonce every call.

    Raises
    ------
    LakanaError
        If `key_provider` has no usable master key configured.
    """
    key_id = key_provider.key_id
    data_key = AESGCM.generate_key(bit_length=_DATA_KEY_SIZE_BITS)
    nonce = os.urandom(_NONCE_SIZE)
    payload = json.dumps(values).encode()
    values_aad = associated_data("values", CURRENT_FORMAT, binding, key_id)
    ciphertext = AESGCM(data_key).encrypt(nonce, payload, values_aad)
    wrap_aad = associated_data("wrap", CURRENT_FORMAT, binding, key_id)
    wrapped_data_key = key_provider.wrap(data_key, wrap_aad)
    return EncryptedSecret(
        ciphertext=ciphertext,
        nonce=nonce,
        wrapped_data_key=wrapped_data_key,
        format_version=CURRENT_FORMAT,
        key_id=key_id,
    )


def decrypt_secret_values(
    encrypted: EncryptedSecret,
    key_provider: KeyProvider,
    binding: SecretBinding,
    *,
    accept_format_1: bool = False,
) -> dict[str, str]:
    """Decrypt a key-value bag, checking it belongs to the row `binding` names.

    A format-2 secret is decrypted only if both layers authenticate with
    the associated data of `binding` and `encrypted.key_id`. A format-1
    secret is refused, before anything is decrypted, unless `accept_format_1`
    is true; then it is decrypted without associated data (`binding` and
    `encrypted.key_id` are not checked).

    Parameters
    ----------
    encrypted
        The persisted pieces, exactly as `encrypt_secret_values` produced
        them.
    key_provider
        Unwraps the data key under the deployment's master key.
    binding
        The row the pieces were read from.
    accept_format_1
        Whether to read a format-1 secret. Comes from the process's
        configuration, never from the row.

    Returns
    -------
    dict of str to str
        The original plaintext key-value bag.

    Raises
    ------
    LegacyFormatRefusedError
        If the secret is format 1 and `accept_format_1` is false.
    SecretFormatError
        If `encrypted.format_version` is not a version this code reads.
    UnwrapError
        If the data key does not unwrap (wrong master key, altered bytes,
        or another row's data).
    cryptography.exceptions.InvalidTag
        If `encrypted.ciphertext` does not authenticate under the data key,
        the nonce and the row's associated data.
    LakanaError
        If `key_provider` has no usable master key configured.
    """
    values_aad: bytes | None
    wrap_aad: bytes | None
    if encrypted.format_version == CURRENT_FORMAT:
        values_aad = associated_data("values", CURRENT_FORMAT, binding, encrypted.key_id)
        wrap_aad = associated_data("wrap", CURRENT_FORMAT, binding, encrypted.key_id)
    elif encrypted.format_version == LEGACY_FORMAT:
        if not accept_format_1:
            raise LegacyFormatRefusedError
        values_aad = wrap_aad = None
    else:
        raise SecretFormatError(encrypted.format_version)
    data_key = key_provider.unwrap(encrypted.wrapped_data_key, wrap_aad)
    payload = AESGCM(data_key).decrypt(encrypted.nonce, encrypted.ciphertext, values_aad)
    return json.loads(payload)


def decrypt_secret_row(
    secret: Secret,
    key_provider: KeyProvider,
    *,
    environment_name: str,
    accept_format_1: bool = False,
) -> dict[str, str]:
    """Decrypt a stored secret's key-value bag, with errors fit for a caller.

    The row's own id, environment id, name, format version and key id are
    the binding, so a caller cannot pass the wrong ones.

    Parameters
    ----------
    secret
        The row.
    key_provider
        Unwraps the data key under the deployment's master key.
    environment_name
        The environment's name, for the error message only.
    accept_format_1
        Whether to read a format-1 row (`LAKANA_SECRETS__ACCEPT_FORMAT_1`).
        The five product call sites pass the process's setting; the default
        refuses.

    Returns
    -------
    dict of str to str
        The plaintext key-value bag.

    Raises
    ------
    LakanaError
        If no master key is configured; if the row is format 1 and
        `accept_format_1` is false (the message names the secret, the
        command that rewrites it and the setting); if the row was written
        by a newer Lakana (its message says to upgrade); or if the row does
        not authenticate or is malformed (altered, moved to another row, or
        the wrong master key). The last message names the secret and what
        to do, and does not say which check failed. One WARNING is logged
        with the secret's id, `format_version`, `key_id` and the
        configured key's id.
    """
    binding = SecretBinding(secret.id, secret.environment_id, secret.name)
    encrypted = EncryptedSecret(
        ciphertext=secret.ciphertext,
        nonce=secret.nonce,
        wrapped_data_key=secret.wrapped_data_key,
        format_version=secret.format_version,
        key_id=secret.key_id,
    )
    try:
        return decrypt_secret_values(
            encrypted, key_provider, binding, accept_format_1=accept_format_1
        )
    except LegacyFormatRefusedError as exc:
        _logger.warning(
            "Secret %s is stored in the old format (1), which is refused because "
            "LAKANA_SECRETS__ACCEPT_FORMAT_1 is not on; set it again with `lakana secret "
            "rotate`, or see the upgrade page.",
            secret.id,
        )
        raise LakanaError(_legacy_refusal(secret, environment_name)) from exc
    except SecretFormatError as exc:
        _log_refusal(secret, key_provider)
        if secret.format_version > CURRENT_FORMAT:
            raise LakanaError(
                f"Secret {secret.name!r} in environment {environment_name!r} was written by a "
                f"newer Lakana (stored format {secret.format_version}; this version reads up "
                f"to {CURRENT_FORMAT}). Upgrade Lakana to read it."
            ) from exc
        refusal = _undecryptable(secret, environment_name)
        raise refusal from exc
    except (InvalidTag, UnwrapError, ValueError) as exc:
        _log_refusal(secret, key_provider)
        refusal = _undecryptable(secret, environment_name)
        raise refusal from exc


def _log_refusal(secret: Secret, key_provider: KeyProvider) -> None:
    """Log, once, which row could not be decrypted.

    For a format-2 row, a `key_id` that differs from `configured_key_id`
    means the master key is not the one the secret was stored under; equal
    ids mean the row was altered or moved. The message to the caller never
    says which.

    Parameters
    ----------
    secret
        The row. Only its id, format version and key id are logged.
    key_provider
        The configured provider; only its key id is logged.
    """
    try:
        configured = key_provider.key_id
    except LakanaError:
        configured = "unavailable"
    _logger.warning(
        "Secret %s could not be decrypted (format_version=%s, key_id=%s, configured_key_id=%s).",
        secret.id,
        secret.format_version,
        secret.key_id,
        configured,
    )


def _rotate_command(secret: Secret, environment_name: str) -> str:
    """Build the copyable command that sets a secret again.

    Parameters
    ----------
    secret
        The row.
    environment_name
        The environment's name.

    Returns
    -------
    str
        The `lakana secret rotate` command, name and environment shell-quoted.
    """
    return (
        f"lakana secret rotate {shlex.quote(secret.name)} "
        f"--env {shlex.quote(environment_name)} --values-stdin"
    )


def _legacy_refusal(secret: Secret, environment_name: str) -> str:
    """Build the message for a format-1 row that is not read.

    Parameters
    ----------
    secret
        The row.
    environment_name
        The environment's name.

    Returns
    -------
    str
        Names the secret, says why it is refused, and gives the two ways forward.
    """
    return (
        f"Secret {secret.name!r} in environment {environment_name!r} is stored in the old "
        f"format ({LEGACY_FORMAT}), which Lakana does not read by default. Set it again with "
        f"`{_rotate_command(secret, environment_name)}`; that needs no read of the old value. "
        "To keep old secrets working until you have, start the server and the workers with "
        "LAKANA_SECRETS__ACCEPT_FORMAT_1=true, and remove the setting afterwards."
    )


def _undecryptable(secret: Secret, environment_name: str) -> LakanaError:
    """Build the one error a row that fails authentication gets.

    Parameters
    ----------
    secret
        The row.
    environment_name
        The environment's name.

    Returns
    -------
    LakanaError
        Names the secret, says it could not be decrypted and what to do.
        The same text whichever check failed.
    """
    return LakanaError(
        f"Secret {secret.name!r} in environment {environment_name!r} could not be decrypted. "
        "If LAKANA_SECRETS__MASTER_KEY was changed, put back the key the secret was stored "
        "under. Otherwise restore it from a backup, or set it again with "
        f"`{_rotate_command(secret, environment_name)}`."
    )
