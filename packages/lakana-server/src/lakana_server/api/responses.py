"""The error answers every platform route can document, in one place.

The server answers a refused or failed call with a JSON body of ``detail`` and ``type``
(`lakana_server.errors`). A route lists the statuses it can return with `errors`, so that the
OpenAPI document describes them without each route repeating the same dictionaries.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorOut(BaseModel):
    """The body of an error answer of the platform API.

    Parameters
    ----------
    detail
        What went wrong and, where there is one, what to do next.
    type
        The name of the kind of error, for a client that wants to tell them apart.
    """

    detail: str = Field(description="What went wrong and, where there is one, what to do next.")
    type: str = Field(description="The name of the kind of error, such as `NotFoundError`.")


ERROR_MEANING: dict[int, str] = {
    400: "The client closed the connection before the request body arrived in full.",
    401: "Not signed in: the token is missing, wrong, expired or revoked.",
    403: "Signed in, but not allowed to do this. The refusal is recorded in the audit log.",
    404: "What the request names does not exist, or the caller may not know that it does.",
    408: "No part of the request body arrived within the read timeout.",
    409: "The request conflicts with the current state; the message says what to change.",
    413: "The request body is larger than the server accepts.",
    502: (
        "A service behind the server, such as the environment's storage, refused it; the message "
        "says what to fix. Asking again does not help until then."
    ),
    503: "The server cannot do this right now; ask again after the time in `Retry-After`.",
}
"""The meaning of each status the platform API documents besides 422, by status code."""


def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """Describe error answers for a route's ``responses``.

    Parameters
    ----------
    *codes
        The statuses the route can return, each one of `ERROR_MEANING`.

    Returns
    -------
    dict
        The ``responses`` entries, each with its meaning and the `ErrorOut` body.

    Raises
    ------
    KeyError
        If a code has no meaning in `ERROR_MEANING`; add it there first.
    """
    return {code: {"model": ErrorOut, "description": ERROR_MEANING[code]} for code in codes}
