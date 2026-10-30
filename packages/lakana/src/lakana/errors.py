"""Typed exceptions raised by the Lakana SDK.

Every exception here is safe to print or log: none of them ever include a
credential. An HTTP-mapped error's `.detail`/`.error_type` come only from the
server's own response body, and a transport failure's message names only the
URL and the underlying `httpx` exception's class name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from lakana._models import Deploy, Run


class LakanaError(Exception):
    """Base class for every error raised by the Lakana SDK.

    Parameters
    ----------
    message
        A human-readable description of the failure.
    status
        The HTTP status code that produced this error, if any.
    error_type
        The server's own `type` field from the error body, if any.
    retry_after
        The response's `Retry-After` header as it was sent (text, not parsed), if any.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_type: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.detail = message
        self.error_type = error_type
        self.retry_after = retry_after


class ConfigError(LakanaError):
    """The SDK's configuration could not be resolved."""


class AuthenticationError(LakanaError):
    """The server rejected the request's credentials (HTTP 401)."""


class PermissionDenied(LakanaError):
    """The credentials are valid but lack the right to do this (HTTP 403)."""


class NotFound(LakanaError):
    """The requested resource does not exist (HTTP 404)."""


class Conflict(LakanaError):
    """The request conflicts with the resource's current state (HTTP 409)."""


class PayloadTooLarge(LakanaError):
    """The request body exceeded the server's size limit (HTTP 413)."""


class ValidationError(LakanaError):
    """The request was structurally invalid (HTTP 422)."""


class ArtifactCorrupt(LakanaError):
    """A downloaded artifact did not match its manifest; nothing of it was kept."""


class ServerError(LakanaError):
    """The server failed unexpectedly (HTTP 5xx)."""


class InvalidResponse(LakanaError):
    """The server answered with a body that is not the JSON the SDK expects (a wrong URL?)."""


class LakanaConnectionError(LakanaError):
    """The server could not be reached at all."""


class QueryFailed(LakanaError):
    """A submitted query run ended in the `FAILED` or `CANCELLED` state.

    Parameters
    ----------
    message
        The run's `error_message`.
    cause
        The server's machine-readable cause (`not_found`, `permission_denied`,
        `result_too_large`, `invalid_query`, `out_of_memory`, `timeout`, `cancelled`,
        `worker_lost` or `internal`; a value this SDK does not know is possible), or `None` when
        the server did not send one: an older server, or a run that failed without one.
    """

    def __init__(self, message: str, *, cause: str | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class DeployFailed(LakanaError):
    """A deploy, dry run or rollback ended in the `failed` state.

    Parameters
    ----------
    message
        The run's `error_message`.
    deploy
        The failed run.
    """

    def __init__(self, message: str, deploy: Deploy) -> None:
        super().__init__(message)
        self.deploy = deploy


class JobStartsNotRecorded(TimeoutError):
    """A deploy succeeded, but the jobs it starts were not all recorded before the timeout.

    A `TimeoutError`, so code that catches the timeout of `Deploys.wait` still catches it. The
    deploy itself is done; the worker records each start of an ``on_deploy`` job right after it,
    so reading the deploy again later shows what came of each.

    Parameters
    ----------
    message
        What happened.
    deploy
        The succeeded deploy, with each start not yet recorded as ``pending``.
    """

    def __init__(self, message: str, deploy: Deploy) -> None:
        super().__init__(message)
        self.deploy = deploy


class RunFailed(LakanaError):
    """A run ended `failed` or `cancelled`.

    Parameters
    ----------
    message
        What happened.
    run
        The finished run.
    """

    def __init__(self, message: str, run: Run) -> None:
        super().__init__(message)
        self.run = run


_STATUS_TO_ERROR: dict[int, type[LakanaError]] = {
    401: AuthenticationError,
    403: PermissionDenied,
    404: NotFound,
    409: Conflict,
    413: PayloadTooLarge,
    422: ValidationError,
}


def _parse_error_body(response: httpx.Response) -> tuple[str, str | None]:
    """Extract a detail message and error type from an error response body.

    Parameters
    ----------
    response
        The HTTP response to parse.

    Returns
    -------
    tuple[str, str | None]
        The detail message and the server's `type` field, if present. The
        detail falls back to the raw response text when the body is not a
        JSON object with a `detail` key, and to the response's HTTP reason
        phrase when `detail` is present but explicitly `null` — never to
        the literal string `"None"`.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text, None
    if not isinstance(body, dict):
        return response.text, None
    error_type = body.get("type")
    if "detail" not in body:
        return response.text, error_type
    detail = body["detail"]
    if detail is None:
        return response.reason_phrase, error_type
    return str(detail), error_type


def raise_for_response(response: httpx.Response) -> None:
    """Raise the SDK exception matching a non-2xx HTTP response.

    Parameters
    ----------
    response
        The HTTP response to check.

    Raises
    ------
    LakanaError
        Mapped from the response's status code: a known 4xx status raises
        its specific subclass (`AuthenticationError`, `PermissionDenied`,
        `NotFound`, `Conflict`, `PayloadTooLarge`, `ValidationError`), 5xx
        raises `ServerError`, a redirect (3xx) raises the base `LakanaError` with a message that
        says to check the profile's URL, and any other non-2xx status raises the base
        `LakanaError` itself.
    """
    if response.is_success:
        return
    if 300 <= response.status_code < 400:
        # httpx does not follow redirects here, and a 3xx has no body worth showing.
        raise LakanaError(
            f"The server answered with a redirect (HTTP {response.status_code}); check the "
            "profile's URL.",
            status=response.status_code,
        )
    detail, error_type = _parse_error_body(response)
    error_cls = _STATUS_TO_ERROR.get(response.status_code)
    if error_cls is None:
        error_cls = ServerError if response.status_code >= 500 else LakanaError
    raise error_cls(
        detail,
        status=response.status_code,
        error_type=error_type,
        retry_after=response.headers.get("Retry-After"),
    )
