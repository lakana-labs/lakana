"""Mapping from lakana-core's error vocabulary to HTTP responses.

The Iceberg REST router does not use this module — it has its own error
shaping (see `api/iceberg/route.py`), because the specification mandates a
different response body for the same exception types. This module serves
the platform API only.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from lakana_core.errors import (
    AlreadyExistsError,
    AuditUnavailableError,
    AuthenticationError,
    ConflictError,
    KeptAfterCheckError,
    LakanaError,
    NoCheckedSnapshotError,
    NotFoundError,
    NotPublishedError,
    PermissionDenied,
    StorageRefusedError,
    StorageUnavailableError,
    ValidationError,
)


class PayloadTooLargeError(LakanaError):
    """A request body exceeded a configured size bound.

    Server-only: this is an HTTP-layer concern (a streamed request body too
    large to buffer), not a domain error `lakana-core` has any reason to
    raise, so it lives here rather than in `lakana_core.errors`.
    """


class AppendConcurrencyLimitError(LakanaError):
    """Too many governed appends are already in flight in this process.

    Server-only, the same reasoning as `PayloadTooLargeError`: this is an
    HTTP-layer resource-exhaustion concern, not
    a domain error `lakana-core` has any reason to raise. Mapped to 503 with
    a `Retry-After: 1` header, so a well-behaved client backs off briefly
    instead of piling on more concurrent uploads.
    """


class DownloadConcurrencyLimitError(LakanaError):
    """Too many artifact files are already being downloaded from this process.

    Server-only, like `AppendConcurrencyLimitError`: an HTTP-layer cap on connections that
    each hold a storage handle and buffers. Mapped to 503 with a `Retry-After: 1` header; the
    SDK asks again a few times.
    """


class AppendReadTimeoutError(LakanaError):
    """A governed append's request body took too long to arrive.

    Server-only, the same reasoning as `PayloadTooLargeError`: this is an
    HTTP-layer concern, not a domain error
    `lakana-core` has any reason to raise. Mapped to 408; nothing is written.
    """


class ClientDisconnectedError(LakanaError):
    """A client closed its connection before a governed append's body finished arriving.

    Server-only, the same reasoning as `PayloadTooLargeError`: this is an
    HTTP-layer concern, not a domain error
    `lakana-core` has any reason to raise. Mapped to 400. The client is
    already gone by the time this is raised, so no one ever reads this
    response — the point of raising a handled `LakanaError` here, rather than
    letting Starlette's own `ClientDisconnect` propagate unhandled, is that
    an *unhandled* exception is what Starlette logs at `ERROR` with a full
    traceback ("Exception in ASGI application"); a handled one is not.
    """


_STATUS_CODES: dict[type[LakanaError], int] = {
    AuthenticationError: 401,
    NotFoundError: 404,
    AlreadyExistsError: 409,
    ConflictError: 409,
    NotPublishedError: 409,
    KeptAfterCheckError: 409,
    NoCheckedSnapshotError: 409,
    ValidationError: 422,
    PermissionDenied: 403,
    PayloadTooLargeError: 413,
    AppendReadTimeoutError: 408,
    AppendConcurrencyLimitError: 503,
    DownloadConcurrencyLimitError: 503,
    ClientDisconnectedError: 400,
    StorageUnavailableError: 503,
    StorageRefusedError: 502,
    AuditUnavailableError: 503,
}

_RETRY_AFTER_SECONDS: dict[type[LakanaError], str] = {
    AppendConcurrencyLimitError: "1",
    DownloadConcurrencyLimitError: "1",
    StorageUnavailableError: "5",
    AuditUnavailableError: "5",
}
"""Extra `Retry-After` header value, by exception type, beyond the plain
status/body mapping `_STATUS_CODES` covers. Kept as its own small table
rather than a general per-exception-headers mechanism, since this is the
only error that needs one."""


class _AsciiJSONResponse(JSONResponse):
    """A JSON response written with ASCII escapes, so that any string can be encoded."""

    def render(self, content: object) -> bytes:
        """Serialise `content` with every non-ASCII character escaped.

        Parameters
        ----------
        content
            The JSON-compatible body.

        Returns
        -------
        bytes
            The body; a lone surrogate is written as an escape instead of failing.
        """
        return json.dumps(
            content, ensure_ascii=True, allow_nan=False, indent=None, separators=(",", ":")
        ).encode("ascii")


def install_exception_handlers(app: FastAPI) -> None:
    """Register handlers mapping LakanaError subclasses to JSON responses.

    Parameters
    ----------
    app
        The FastAPI application to install handlers on.
    """

    def handle_lakana_error(request: Request, exc: Exception) -> JSONResponse:
        """Map a LakanaError to a status code and a Lakana-shaped body.

        Parameters
        ----------
        request
            The request that raised the exception. Unused; required by
            FastAPI's exception handler signature.
        exc
            The exception raised somewhere in request handling. Typed as
            the base `Exception` to match Starlette's `ExceptionHandler`
            protocol exactly; in practice this is always a `LakanaError`,
            since it is registered only for that class and its subclasses.

        Returns
        -------
        JSONResponse
            A body of the shape `{"detail": str, "type": str}`, with a
            `Retry-After` header attached for the one error type that needs
            one (`_RETRY_AFTER_SECONDS`).

        Notes
        -----
        A subclass (`SchemaMismatchError`, a `ValidationError`) maps
        through its nearest mapped base class; a `LakanaError` with no mapped
        base is still a 500.
        """
        status_code = next(
            (
                _STATUS_CODES[cls]
                for cls in type(exc).__mro__
                if issubclass(cls, LakanaError) and cls in _STATUS_CODES
            ),
            500,
        )
        retry_after = _RETRY_AFTER_SECONDS.get(type(exc))
        headers = {"Retry-After": retry_after} if retry_after is not None else None
        return JSONResponse(
            status_code=status_code,
            content={"detail": str(exc), "type": type(exc).__name__},
            headers=headers,
        )

    def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
        """Answer a request that does not fit its schema, without echoing the input it was sent.

        FastAPI's own handler puts each problem's `input` into the body; this one leaves
        that key out. Field locations (a key the client chose) and some messages still carry
        text the client sent, JSON-escaped, so a caller must not treat the answer as free of
        user text. FastAPI's handler also writes the body as UTF-8: a string with a lone
        surrogate (valid JSON, as an escaped surrogate) made that write fail with a 500. The shape
        is the same, a list of `type`, `loc` and `msg` (and `ctx` when pydantic has one) under
        `detail`, which the SDK and the CLI print, and the text is written with ASCII escapes,
        so no character can fail it.

        Parameters
        ----------
        request
            The request. Unused; required by FastAPI's exception handler signature.
        exc
            The `RequestValidationError`; typed as `Exception` to match Starlette's
            `ExceptionHandler` protocol, and registered only for that class.

        Returns
        -------
        JSONResponse
            Status 422 with `{"detail": [problem, ...]}`.
        """
        assert isinstance(exc, RequestValidationError)  # nosec B101 -- registered for it only
        problems = [
            {key: value for key, value in problem.items() if key != "input"}
            for problem in exc.errors()
        ]
        return _AsciiJSONResponse(status_code=422, content={"detail": jsonable_encoder(problems)})

    app.add_exception_handler(LakanaError, handle_lakana_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
