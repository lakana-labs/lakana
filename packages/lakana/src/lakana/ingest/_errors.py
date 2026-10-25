"""The errors of `lakana.ingest`, and whether another attempt of the task could end differently.

The harness reads `IngestError.retryable` for an error the asset's function raised
(`lakana.runner._retryable`): `False` fails the task at once, `True` retries it, `None` leaves
it to the harness's own rule (a function's error is retried).
"""

from __future__ import annotations

import re
from typing import ClassVar


class IngestError(Exception):
    """An error of a `lakana.ingest` helper; the message names the connection, never a secret."""

    retryable: ClassVar[bool | None] = None
    """Whether another attempt could end differently; `None` leaves it to the harness."""


class ConnectionNotDeclared(IngestError):
    """The asset asked for a connection it does not declare; every attempt would."""

    retryable: ClassVar[bool | None] = False


class ConnectionRefused(IngestError):
    """The server refused the fetch of a connection; the message is the server's."""

    retryable: ClassVar[bool | None] = False


class ConnectionChanged(IngestError):
    """The connection changed while the task ran; the next attempt fetches it again."""

    retryable: ClassVar[bool | None] = True


class UsageError(IngestError):
    """The call's own arguments, or the connection's kind, are wrong; every attempt would fail."""

    retryable: ClassVar[bool | None] = False


class SourceError(IngestError):
    """The source answered with an error; the message says what it said."""


class FileShapeError(SourceError):
    """The files themselves are wrong (types, columns or content); the same files fail again."""

    retryable: ClassVar[bool | None] = False


class CapExceeded(IngestError):
    """A read went past one of its limits; the same read would again."""

    retryable: ClassVar[bool | None] = False


_UNSAFE = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


def _escaped(match: re.Match[str]) -> str:
    r"""Spell one unsafe character the way Python writes it.

    Parameters
    ----------
    match
        The character.

    Returns
    -------
    str
        `\x0a` for a code point below 256, `\u202e` above.
    """
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def safe_name(text: str) -> str:
    r"""Make a file name safe to put in a message: control characters become escapes.

    A name with a line feed would otherwise start a new line in a task's log or a failure
    message, which could forge a line.

    Parameters
    ----------
    text
        A file name or a path, as the source reported it.

    Returns
    -------
    str
        `text` with every control and bidirectional control character written as an escape
        such as `\x0a`.
    """
    return _UNSAFE.sub(_escaped, text)
