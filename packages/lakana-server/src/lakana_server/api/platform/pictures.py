"""A person's profile picture: upload (re-encoded), read with caching headers, remove.

The upload is read as a stream with a fixed 5 MiB cap, never buffered beyond it, and decoded in
the threadpool by `lakana_core.identity.pictures`: Pillow's decode of up to 40 megapixels must
not run on the event loop. Two uploads per process at most, one per person, both taken before
the body is read and refused with 503 rather than queued. What is served is the stored
re-encoded WebP only, with headers that keep a browser from treating it as anything but an
image.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.concurrency import run_in_threadpool

from lakana_core.auth import PrincipalRef
from lakana_core.identity.pictures import PICTURE_MAX_BYTES, PictureService
from lakana_server.api.platform.artifacts import next_chunk
from lakana_server.api.platform.schemas import PictureOut
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.body_limit import streams_its_own_body
from lakana_server.concurrency import hold_one_per_principal
from lakana_server.deps import get_picture_service, get_principal
from lakana_server.errors import AppendReadTimeoutError, PayloadTooLargeError

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/principals",
    tags=["principals"],
    responses=errors(401, 403),
)

PICTURE_DECODE_SLOTS = 2
"""Picture uploads one server process reads and decodes at once; a further one is refused with
503 and `Retry-After: 1`, and each person may have one in progress."""

PICTURE_READ_TIMEOUT_SECONDS: float = 120
"""The longest a picture's body may take to arrive (5 MB at about 42 KB/s), or the append read
timeout when that is shorter; so two slow uploads hold both decode slots for two minutes at most,
not the append's 300 seconds."""

_NAME = Annotated[str, Path(min_length=1, max_length=255)]

_YOURS_BUSY = (
    "Another picture upload of yours is still in progress. Wait for it to finish, then try again."
)
_ALL_BUSY = "The server is busy with other picture uploads. Try again in a moment."

_TOO_LARGE = "The picture is larger than 5 MB. Choose a smaller image or crop it first."

_HEADERS = {
    "Cache-Control": "private, max-age=86400",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}
"""Sent with every picture and every 304: cached by the reader's browser only, never sniffed as
another type, and inert should it be opened as a document."""


def _media_type(content_type: str | None) -> str:
    """Read a `Content-Type` header's bare media type, lowercased.

    Parameters
    ----------
    content_type
        The header, or `None` when absent.

    Returns
    -------
    str
        The type without parameters; ``""`` without a header.
    """
    return (content_type or "").split(";", 1)[0].strip().lower()


def _matches(if_none_match: str | None, etag: str) -> bool:
    """Say whether an `If-None-Match` header names the picture's current etag.

    Parameters
    ----------
    if_none_match
        The header, or `None`.
    etag
        The picture's etag, unquoted.

    Returns
    -------
    bool
        `True` for ``*`` or a list holding the etag, strong or weak.
    """
    if if_none_match is None:
        return False
    for tag in if_none_match.split(","):
        candidate = tag.strip().removeprefix("W/")
        if candidate in ("*", f'"{etag}"'):
            return True
    return False


async def _read_picture(request: Request) -> bytes:
    """Read the upload's body, refusing it as soon as it passes `PICTURE_MAX_BYTES`.

    Parameters
    ----------
    request
        The request.

    Returns
    -------
    bytes
        The body, at most `PICTURE_MAX_BYTES`.

    Raises
    ------
    PayloadTooLargeError
        If the declared or the arriving body is larger than 5 MiB (413).
    AppendReadTimeoutError
        If the body did not arrive in full within `PICTURE_READ_TIMEOUT_SECONDS`, or the
        shorter append read timeout (408).
    lakana_server.errors.ClientDisconnectedError
        If the client went away mid-body (400).
    """
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > PICTURE_MAX_BYTES:
        raise PayloadTooLargeError(_TOO_LARGE)
    timeout = min(
        request.app.state.settings.append.read_timeout_seconds, PICTURE_READ_TIMEOUT_SECONDS
    )
    iterator = request.stream().__aiter__()
    body = bytearray()
    try:
        async with asyncio.timeout(timeout):
            while (chunk := await next_chunk(iterator, timeout)) is not None:
                body.extend(chunk)
                if len(body) > PICTURE_MAX_BYTES:
                    raise PayloadTooLargeError(_TOO_LARGE)
    except TimeoutError as exc:
        raise AppendReadTimeoutError(
            f"The picture did not finish arriving within {timeout}s. Check the network "
            "connection, then upload it again."
        ) from exc
    return bytes(body)


@router.put(
    "/{name}/picture",
    response_model=PictureOut,
    responses=errors(400, 404, 408, 413, 415, 503),
)
@streams_its_own_body
async def set_picture(
    name: _NAME,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    service: PictureService = Depends(get_picture_service),
) -> PictureOut:
    """Set your profile picture.

    Send the image file as the body, with its type as `Content-Type`: `image/jpeg`, `image/png`
    or `image/webp` (415 for any other, SVG included). At most 5 MB (413) and 40 megapixels
    (422). The server keeps a new 256 x 256 picture cut from the centre and turned upright, with
    none of the file's metadata (location, camera); 422 when the file is not a readable image of
    the type sent.
    Only your own (403 for another name, and for a task or CI token). One upload of yours at a
    time, and a few in all per server: otherwise 503, try again after `Retry-After`. Audited.

    Parameters
    ----------
    name
        Your principal name.
    request
        The request; its body is the image.
    principal
        The authenticated caller.
    service
        The picture service.

    Returns
    -------
    PictureOut
        The new picture's etag.
    """
    media_type = _media_type(request.headers.get("content-type"))
    # Before a byte is read: a caller who may not set this picture must not make the server
    # read and decode 5 MiB.
    service.check_can_set(principal, name, media_type)
    # Taken before the body is read, refused rather than waited for: a request never queues
    # holding a body and a database connection.
    async with hold_one_per_principal(
        request.app.state.picture_decode_slots,
        request.app.state.picture_principals,
        principal.id,
        own_busy=_YOURS_BUSY,
        all_busy=_ALL_BUSY,
    ):
        data = await _read_picture(request)
        etag = await run_in_threadpool(service.set, principal, name, data, media_type)
    return PictureOut(etag=etag)


@router.get(
    "/{name}/picture",
    response_class=Response,
    responses={
        200: {"content": {"image/webp": {}}, "description": "The picture, a 256 x 256 WebP."},
        304: {"description": "The picture is still the one `If-None-Match` names."},
        **errors(404),
    },
)
def get_picture(
    name: _NAME,
    request: Request,
    principal: PrincipalRef = Depends(get_principal),
    service: PictureService = Depends(get_picture_service),
) -> Response:
    """Show a person's profile picture.

    Your own always; anyone's once you have access (any grant, or the `ADMIN` role); 403 for
    a person without access yet, and for a task or CI token. 404 when the person has no
    picture. The answer carries an `ETag` (the `picture` field of the principal and of whoami)
    and is cached privately for a day; send `If-None-Match` for a 304.

    Parameters
    ----------
    name
        The person's principal name.
    request
        The request, for `If-None-Match`.
    principal
        The authenticated caller.
    service
        The picture service.

    Returns
    -------
    Response
        The WebP, or an empty 304.
    """
    image, etag = service.get(principal, name)
    headers = {**_HEADERS, "ETag": f'"{etag}"'}
    if _matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=image, media_type="image/webp", headers=headers)


@router.delete("/{name}/picture", status_code=204, responses=errors(404))
def remove_picture(
    name: _NAME,
    principal: PrincipalRef = Depends(get_principal),
    service: PictureService = Depends(get_picture_service),
) -> None:
    """Remove a profile picture.

    Your own, or anyone's with `MANAGE_PRINCIPAL` on the instance (an administrator removing
    an unsuitable picture); 403 otherwise, and for a task or CI token. 404 when no principal
    has that name; removing a picture that is not there answers 204. Audited.

    Parameters
    ----------
    name
        The person's principal name.
    principal
        The authenticated caller.
    service
        The picture service.
    """
    service.remove(principal, name)
