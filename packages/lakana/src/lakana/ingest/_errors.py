"""The errors of `lakana.ingest`, and whether another attempt of the task could end differently.

The harness reads `IngestError.retryable` for an error the asset's function raised
(`lakana.runner._retryable`): `False` fails the task at once, `True` retries it, `None` leaves
it to the harness's own rule (a function's error is retried).
"""

from __future__ import annotations

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


class SourceError(IngestError):
    """The source answered with an error; the message says what it said."""


class CapExceeded(IngestError):
    """A read went past one of its limits; the same read would again."""

    retryable: ClassVar[bool | None] = False
