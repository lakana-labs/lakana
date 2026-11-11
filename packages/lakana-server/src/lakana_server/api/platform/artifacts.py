"""The artifact resource of the platform API: adding a version in three requests.

`POST .../uploads` validates the manifest and opens an upload, `PUT .../blobs/{sha256}` streams
one file to storage, `POST .../commit` numbers the version. Every operation is
`lakana_core.artifacts.ArtifactService`'s; this module only moves bytes between the request and
the service, and bounds that.

The request body is untrusted bytes of any length. Nothing here holds a file: a blob goes from
the request's stream to `BlobUpload.write` in pieces of at most `PIECE_BYTES` plus one ASGI chunk,
and the service refuses a body longer than its manifest declares as it arrives. `Content-Length`
is never trusted; the manifest's size and hash decide, and the server's framing (uvicorn) ends
the body at the declared length. What each failure answers:

- a client that disconnects mid-body: 400 (nobody reads it), the upload's file is thrown away;
- no byte for `LAKANA_APPEND__READ_TIMEOUT_SECONDS`: 408, thrown away;
- a body longer than the manifest declares: 422 as soon as the first extra byte arrives;
- a body shorter than declared, or with another hash: 422 at the end, nothing stored;
- all slots of the append limiter taken: 503 with `Retry-After: 1`; the same for a manifest
  request when `MANIFEST_DECODE_SLOTS` requests are being read or decoded, before its body is read;
- a hash that is not in the upload's manifest: 422; a malformed hash or upload id: 422;
- an expired or unknown upload (including one that was already committed): 409.

Reading is the other half. Every read route authorizes before it looks anything up, so a caller
without the privilege gets one answer (403) for an artifact, a version or a file that exists and
for one that does not. A version number arrives as text and is a bounded positive integer or
`current` (`parse_version`). A download streams one file from storage in pieces:

- `Content-Length` is the manifest's size; an object of another length, or a storage failure
  after the first byte, ends the response without completing it: headers were sent, so the
  connection is cut and the client sees an incomplete body, never a short 200 that looks whole;
- no database session or transaction is held while the body is sent (`open_blob` ends its
  transaction first);
- the whole response has a deadline of `DOWNLOAD_BASE_SECONDS` plus the file's size at
  `DOWNLOAD_MIN_BYTES_PER_SECOND`, so a client that reads one byte a minute cannot keep a
  connection for ever. A download takes no slot of the append limiter, so reads are not
  starved by uploads, and has a limiter of its own: at most `MAX_CONCURRENT_DOWNLOADS` files
  are sent at once in a process, a request over that is 503 with `Retry-After: 1` (the SDK
  asks again), so that one caller cannot hold hundreds of stalled downloads, each with a
  storage handle and a buffer. The slot is held from before the storage is opened until the
  response has closed, on every exit;
- the storage iterator is closed on every exit: end, error, client disconnect, the deadline and
  the server shutting down;
- storage that cannot be opened is 503 with `Retry-After`, never 500.

A download is audited by the service, once per principal and version within the upload lifetime
, also when it later fails; this module writes no event.

A person's `POST .../uploads` body is read with a cap of `MAX_MANIFEST_REQUEST_BYTES`, refused on
its `Content-Length` before a byte is read and again on the running total while it arrives.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Sequence
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from lakana_core.artifacts.service import (
    UPLOAD_GONE,
    ArtifactService,
    ArtifactSource,
    ArtifactView,
    BlobUpload,
    DeletedVersionView,
    UploadCursor,
    VersionView,
)
from lakana_core.auth import PrincipalRef
from lakana_core.errors import ConflictError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef, shown
from lakana_server.api.platform.schemas import (
    ArtifactBlobOut,
    ArtifactCommitIn,
    ArtifactFileOut,
    ArtifactKeptOut,
    ArtifactOut,
    ArtifactUploadItemOut,
    ArtifactUploadOut,
    ArtifactUploadPage,
    ArtifactVersionDeletedOut,
    ArtifactVersionOut,
)
from lakana_server.api.responses import errors
from lakana_server.api.routing import AuthenticateFirstRoute
from lakana_server.body_limit import streams_its_own_body
from lakana_server.concurrency import AppendConcurrencyLimiter, hold_one_per_principal
from lakana_server.deps import get_artifact_service, get_principal, get_write_principal
from lakana_server.errors import (
    AppendConcurrencyLimitError,
    AppendReadTimeoutError,
    ClientDisconnectedError,
    DownloadConcurrencyLimitError,
    PayloadTooLargeError,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}/namespaces/{path}/artifacts",
    tags=["artifacts"],
    responses=errors(401),
)
environment_router = APIRouter(
    route_class=AuthenticateFirstRoute,
    prefix="/api/v1/environments/{env}/artifacts",
    tags=["artifacts"],
    responses=errors(401),
)

_MAX_VERSION = 2**31 - 1
_VERSION_NUMBER = re.compile(r"\A[1-9][0-9]{0,9}\Z")

DOWNLOAD_BASE_SECONDS = 60.0
"""Seconds every download may take whatever its size: connect, first byte, a small file."""

DOWNLOAD_MIN_BYTES_PER_SECOND = 64 * 1024
"""The slowest sustained rate a download is given time for (64 KiB/s, about 0.5 Mbit/s).

A file's deadline is `DOWNLOAD_BASE_SECONDS` plus its size over this rate. For the largest file
an artifact may hold, 2 GiB: 2 * 1024**3 / (64 * 1024) = 32768 s, about 9.1 hours, plus the
base. A client slower than that on average, or one that stops reading, is cut off; the SDK's
message says so. The limit is a constant, not a setting, until an operator needs another."""

MAX_CONCURRENT_DOWNLOADS = 32
"""Most files sent at once in one process.

A download takes no upload slot, and a stalled one holds a storage handle and a buffer of
megabytes (a piece of 1 MiB and the backend's read-ahead) until its deadline, so without a cap
one caller could hold hundreds. 32 is far above what a person or a task's inputs need at once
and bounds the memory of stalled downloads to a fraction of a gigabyte."""

MAX_MANIFEST_REQUEST_BYTES = 16 * 1024 * 1024
"""Most bytes of a `POST …/uploads` body: 10,000 files with long paths fit."""

PIECE_BYTES = 4 * 1024 * 1024
"""Bytes gathered from the request before they are handed to storage."""

MANIFEST_DECODE_SLOTS = 2
"""Manifest bodies decoded at once in this process.

Decoding 16 MiB of JSON peaks at 20 to 30 times that in memory (about half a gigabyte), so two
at a time bound the peak to about a gigabyte. A slot is taken before the first byte of the body
is read and held until the decode ends, so the body buffers too are bounded by it; a request
that finds none free is refused with 503 and `Retry-After: 1` without reading anything, and the
SDK asks again. A principal holds at most one of them at a time."""

_NOT_JSON = "The request body must be the manifest as a JSON object."
_BAD_CURSOR = (
    "The cursor is not valid. Start the listing again without `cursor`; pass the `next_cursor` of "
    "a page only to continue that listing."
)

_background: set[asyncio.Task[None]] = set()
"""Aborts in flight; a task is only weakly referenced by the event loop."""


def artifact_ref(env: str, path: str, name: str) -> TableRef:
    """Build the reference a route's path names.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.

    Returns
    -------
    TableRef
        The reference.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422, if the namespace path is malformed.
    """
    return TableRef(environment=env, namespace=NamespacePath.from_parts(path.split(".")), name=name)


def is_task_caller(principal: PrincipalRef) -> bool:
    """Say whether the caller is a task token.

    Parameters
    ----------
    principal
        The authenticated caller.

    Returns
    -------
    bool
        `True` for a task token, bound or not.
    """
    return principal.task_scope is not None


def _text(value: uuid.UUID | None, withhold: bool) -> str | None:
    """Write an identifier for a response, or nothing.

    Parameters
    ----------
    value
        The identifier, if there is one.
    withhold
        Leave it out whatever it is.

    Returns
    -------
    str or None
        The identifier as text; `None` when absent or withheld.
    """
    return None if withhold or value is None else str(value)


def artifact_out(view: ArtifactView) -> ArtifactOut:
    """Map the service's view of an artifact to the response model.

    Parameters
    ----------
    view
        The artifact.

    Returns
    -------
    ArtifactOut
        The response body.
    """
    return ArtifactOut(
        name=view.ref.name,
        namespace=str(view.ref.namespace),
        uuid=str(view.id),
        kind="ARTIFACT",
        current_version=view.current_version,
        versions=view.versions,
        size_bytes=view.size_bytes,
        created_at=view.created_at,
    )


def deleted_out(view: DeletedVersionView) -> ArtifactVersionDeletedOut:
    """Map the service's answer to a delete to the response model.

    Parameters
    ----------
    view
        The deleted version with its freed and kept files.

    Returns
    -------
    ArtifactVersionDeletedOut
        The response body.
    """
    base = version_out(view)
    return ArtifactVersionDeletedOut(
        **base.model_dump(),
        freed_files=view.freed_files,
        freed_bytes=view.freed_bytes,
        kept_files=sum(k.files for k in view.kept),
        kept_bytes=sum(k.bytes for k in view.kept),
        kept=[
            ArtifactKeptOut(reason=k.reason.value, files=k.files, bytes=k.bytes, version=k.version)
            for k in view.kept
        ],
    )


def parse_version(value: str, *, allow_current: bool) -> int | None:
    """Read the `{version}` path segment.

    Parameters
    ----------
    value
        The segment, untrusted text.
    allow_current
        Accept the word ``current`` (and only that spelling).

    Returns
    -------
    int or None
        The number; `None` for ``current``.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422, unless it is a decimal of ASCII digits without sign or leading zero from 1
        to 2147483647, or (when allowed) ``current``. The text is never echoed.
    """
    if allow_current and value == "current":
        return None
    if _VERSION_NUMBER.match(value) is not None and int(value) <= _MAX_VERSION:
        return int(value)
    raise ValidationError(
        "A version is a positive number or 'current'."
        if allow_current
        else "A version is a positive number."
    )


def version_out(
    view: VersionView,
    deleted_versions: Sequence[int] = (),
    *,
    withhold_producer: bool = False,
) -> ArtifactVersionOut:
    """Map the service's view of a version to the response model.

    Parameters
    ----------
    view
        The version.
    deleted_versions
        The numbers a commit pruned; empty for a read.
    withhold_producer
        Leave out the identifiers of the run that produced the version (`task_run_id`, `run_id`,
        `project_version_id`, as `null`). A task needs to know what it consumes (`commit_sha`,
        `created_by` stay), not which run of another task made it.

    Returns
    -------
    ArtifactVersionOut
        The response body.
    """
    return ArtifactVersionOut(
        version=view.version,
        manifest_sha256=view.manifest_sha256,
        size_bytes=view.size_bytes,
        file_count=view.file_count,
        is_directory=view.is_directory,
        task_run_id=_text(view.task_run_id, withhold_producer),
        run_id=_text(view.run_id, withhold_producer),
        project_version_id=_text(view.job_version_id, withhold_producer),
        commit_sha=view.commit_sha,
        created_by=view.created_by,
        created_at=view.created_at,
        deleted_at=view.deleted_at,
        deleted_by=view.deleted_by,
        files=[ArtifactFileOut(path=f.path, size=f.size, sha256=f.sha256) for f in view.files],
        deleted_versions=list(deleted_versions),
    )


async def next_chunk(iterator: AsyncIterator[bytes], idle: float) -> bytes | None:
    """Wait for the next piece of a request body, but not for ever.

    Parameters
    ----------
    iterator
        `request.stream()`'s iterator.
    idle
        Seconds without a byte after which the upload is abandoned.

    Returns
    -------
    bytes or None
        The piece, or `None` at the end of the body.

    Raises
    ------
    AppendReadTimeoutError
        If nothing arrived for `idle` seconds (408).
    ClientDisconnectedError
        If the client closed the connection (400; nobody reads it).
    """
    try:
        return await asyncio.wait_for(anext(iterator, None), idle)
    except TimeoutError as exc:
        raise AppendReadTimeoutError(
            f"No bytes arrived for {idle}s; the upload was abandoned. Check the network "
            "connection, then run the upload again."
        ) from exc
    except ClientDisconnect as exc:
        raise ClientDisconnectedError(
            "The client disconnected before the file finished arriving."
        ) from exc


async def _next_file_chunk(
    blob: BlobUpload, iterator: AsyncIterator[bytes], idle: float
) -> bytes | None:
    """Wait for the next piece of a file, but not beyond the life of its upload.

    A client that sends a byte every few minutes would otherwise hold a slot of the limiter, shared
    with every table append, for as long as the file is long; bytes that arrive after the upload
    expired are wasted anyway, `finish` refuses them.

    Parameters
    ----------
    blob
        The file being received.
    iterator
        `request.stream()`'s iterator.
    idle
        Seconds without a byte after which the upload is abandoned.

    Returns
    -------
    bytes or None
        The piece, or `None` at the end of the body.

    Raises
    ------
    lakana_core.errors.ConflictError
        With `UPLOAD_GONE` once the upload has expired (409, as for an expired upload anywhere).
    AppendReadTimeoutError
        If nothing arrived for `idle` seconds (408).
    ClientDisconnectedError
        If the client closed the connection (400).
    """
    left = blob.seconds_left()
    if left <= 0:
        raise ConflictError(UPLOAD_GONE)
    try:
        return await next_chunk(iterator, min(idle, left))
    except AppendReadTimeoutError:
        if blob.seconds_left() <= 0:
            raise ConflictError(UPLOAD_GONE) from None
        raise


async def _abort(blob: BlobUpload) -> None:
    """Throw an upload's file away, even when this request is being cancelled.

    The abort runs in the threadpool and is shielded: a server that cancels the handler (a client
    that went away) must not leave the temporary file or the multipart upload behind.

    Parameters
    ----------
    blob
        The upload to abort; a no-op when it is finished or already aborted. A failure of the
        abort itself is logged by the task and otherwise ignored.
    """
    task = asyncio.ensure_future(run_in_threadpool(blob.abort))
    _background.add(task)
    task.add_done_callback(_background.discard)
    # A failing abort must not replace the error that made it necessary.
    with contextlib.suppress(Exception):
        await asyncio.shield(task)


def _declared_length(request: Request) -> int | None:
    """Read the `Content-Length` a request declares, if it is a plain number.

    Parameters
    ----------
    request
        The request.

    Returns
    -------
    int or None
        The number, or `None` when absent or not a non-negative integer.
    """
    value = request.headers.get("content-length", "")
    return int(value) if value.isascii() and value.isdigit() and len(value) < 19 else None


@contextlib.asynccontextmanager
async def _manifest_hold(request: Request, principal_id: uuid.UUID) -> AsyncGenerator[None]:
    """Hold a decode slot and the caller's place in line, and give both back together.

    A principal may have one manifest request being read at a time: otherwise two
    requests that send a few bytes and then stall would hold both global slots for the whole
    read timeout and lock every other caller out. The check and the take have no suspension point
    between them, so no other request can slip in. The principal's id leaves the set when this
    block ends, however it ends (a normal end, an exception, a disconnect, cancellation), so the
    set never holds more ids than there are slots.

    The tasks of one run share the run's principal, so two of them beginning an upload at the
    same instant make one of them retry after a second; the SDK does that on its own.

    Parameters
    ----------
    request
        The request.
    principal_id
        The authenticated caller.

    Yields
    ------
    None
        While both holds are taken.

    Raises
    ------
    AppendConcurrencyLimitError
        If the principal already has a manifest request in progress, or every decode slot is
        taken (503, the same answer for both: nothing tells a caller about other principals).
    """
    busy = "Too many concurrent uploads; retry shortly."
    async with hold_one_per_principal(
        request.app.state.manifest_decode_slots,
        request.app.state.manifest_principals,
        principal_id,
        own_busy=busy,
        all_busy=busy,
    ):
        yield


async def _read_manifest_document(
    request: Request, idle: float, principal_id: uuid.UUID
) -> dict[str, object]:
    """Read the manifest request's body with its cap and decode it.

    Parameters
    ----------
    request
        The request.
    idle
        Seconds without a byte after which the request is refused.
    principal_id
        The authenticated caller, who may have one manifest request in progress.

    Returns
    -------
    dict
        The JSON object.

    Raises
    ------
    PayloadTooLargeError
        If the body is, or turns out to be, larger than `MAX_MANIFEST_REQUEST_BYTES` (413).
    AppendConcurrencyLimitError
        If every decode slot is taken, or the caller already has a manifest request in progress
        (503): nothing of the body is read.
    AppendReadTimeoutError
        If the body did not arrive within the read timeout in all (408).
    lakana_core.errors.ValidationError
        If the body is not a JSON object, or has an integer too long to read (422).
    """
    too_large = PayloadTooLargeError(
        f"The manifest request is larger than {MAX_MANIFEST_REQUEST_BYTES} bytes. It lists every "
        "file with its path and hash: store fewer files or give them shorter paths (the limit is "
        "fixed)."
    )
    declared = _declared_length(request)
    if declared is not None and declared > MAX_MANIFEST_REQUEST_BYTES:
        raise too_large
    async with _manifest_hold(request, principal_id):
        iterator = request.stream().__aiter__()
        body = bytearray()
        try:
            async with asyncio.timeout(request.app.state.settings.append.read_timeout_seconds):
                while (chunk := await next_chunk(iterator, idle)) is not None:
                    body.extend(chunk)
                    if len(body) > MAX_MANIFEST_REQUEST_BYTES:
                        raise too_large
        except TimeoutError as exc:
            raise AppendReadTimeoutError(
                "The manifest did not finish arriving within "
                f"{request.app.state.settings.append.read_timeout_seconds}s; the request was "
                "abandoned. Send it again."
            ) from exc
        return await run_in_threadpool(_decode_document, bytes(body))


def _decode_document(body: bytes) -> dict[str, object]:
    """Decode a manifest request's body.

    Parameters
    ----------
    body
        The bytes.

    Returns
    -------
    dict
        The JSON object.

    Raises
    ------
    lakana_core.errors.ValidationError
        If it is not valid JSON, is not an object, nests too deeply, or holds an integer too
        long for Python to read (a `ValueError`, not a 500).
    """
    if not body.lstrip().startswith(b"{"):
        raise ValidationError(_NOT_JSON)
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        raise ValidationError(_NOT_JSON) from None
    if not isinstance(document, dict):  # pragma: no cover -- a text starting '{' is an object
        raise ValidationError(_NOT_JSON)
    return document


@router.post(
    "/{name}/uploads",
    response_model=ArtifactUploadOut,
    status_code=201,
    responses=errors(403, 404, 409, 408, 413, 503),
)
@streams_its_own_body
async def begin_upload(
    request: Request,
    env: str,
    path: str,
    name: str,
    principal: PrincipalRef = Depends(get_write_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactUploadOut:
    """Begin an artifact upload.

    Checks a manifest of files and opens an upload for the next version of an artifact; the
    artifact is created on first use. The answer names the files the store does not have
    yet: send those with the blob route, then commit. Needs `WRITE_TABLE` on the name, and
    `CREATE_TABLE` as well to create the artifact. Answers 404 when the namespace does
    not exist, 409 when the artifact has too many open uploads, 413 for a manifest over the
    server's limit, 408 when the body arrives too slowly, 503 when the server is busy (try
    again after `Retry-After`) and 422 when the body is not a manifest or breaks one of its
    rules.

    Parameters
    ----------
    request
        The current request; its body is the manifest document.
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    service
        The artifact service for this request.

    Returns
    -------
    ArtifactUploadOut
        The upload's id, the missing hashes and when it expires.

    Raises
    ------
    lakana_server.errors.PayloadTooLargeError
        Mapped to 413, if the body is larger than `MAX_MANIFEST_REQUEST_BYTES`.
    lakana_server.errors.AppendConcurrencyLimitError
        Mapped to 503 with `Retry-After: 1`, if every manifest slot is taken or the caller
        already has a manifest request in progress; the body is not read.
    lakana_server.errors.AppendReadTimeoutError
        Mapped to 408, if no byte arrived for the read timeout or the body took longer than it
        in all.
    lakana_core.errors.ValidationError
        Mapped to 422: the body is not a manifest, or the manifest breaks a rule or a limit.
    lakana_core.errors.PermissionDenied
        Mapped to 403.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the namespace does not exist.
    lakana_core.errors.ConflictError
        Mapped to 409, if the artifact has too many open uploads.
    """
    ref = artifact_ref(env, path, name)
    # Before a byte of the body is read: a caller who may not write the name must not make the
    # server read and decode up to 16 MiB.
    await run_in_threadpool(service.check_can_upload, principal, ref)
    idle = request.app.state.settings.append.read_timeout_seconds
    document = await _read_manifest_document(request, idle, principal.id)
    view = await run_in_threadpool(service.begin_upload, principal, ref, document)
    return ArtifactUploadOut(
        upload_id=str(view.upload_id), missing=list(view.missing), expires_at=view.expires_at
    )


@router.put(
    "/{name}/uploads/{upload_id}/blobs/{sha256}",
    response_model=ArtifactBlobOut,
    responses=errors(403, 404, 409, 400, 408, 503),
)
@streams_its_own_body
async def put_blob(
    request: Request,
    env: str,
    path: str,
    name: str,
    upload_id: uuid.UUID,
    sha256: str = Path(pattern=r"^[0-9a-f]{64}$"),
    principal: PrincipalRef = Depends(get_write_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactBlobOut:
    """Upload one file of an artifact.

    Receives one file of an open upload as the request body and checks it against the
    manifest's size and SHA-256. Needs `WRITE_TABLE`. Answers 409 when the upload has
    expired, was committed or does not exist, 422 when the file is longer or shorter than
    declared, its hash differs, or the manifest names no file with this hash, 400 when the
    client closed the connection part-way, 408 when the body arrives too slowly and 503 when
    the server is busy (try again after `Retry-After`).

    Parameters
    ----------
    request
        The current request; its body is the file's bytes.
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.
    upload_id
        The upload's id, as returned when it was begun.
    sha256
        The file's SHA-256, 64 lower-case hexadecimal digits.
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    service
        The artifact service for this request.

    Returns
    -------
    ArtifactBlobOut
        Whether this request stored the file or the artifact already had it.

    Raises
    ------
    lakana_server.errors.AppendConcurrencyLimitError
        Mapped to 503 with `Retry-After: 1`, if every upload slot is taken.
    lakana_server.errors.AppendReadTimeoutError
        Mapped to 408, if no byte arrived for the read timeout.
    lakana_server.errors.ClientDisconnectedError
        Mapped to 400, if the client closed the connection mid-body.
    lakana_core.errors.ValidationError
        Mapped to 422: the file is longer or shorter than declared, or its hash differs, or the
        manifest names no file with this hash.
    lakana_core.errors.ConflictError
        Mapped to 409, if the upload expired, was committed or does not exist.
    lakana_core.errors.PermissionDenied
        Mapped to 403.
    lakana_core.errors.NotFoundError
        Mapped to 404.
    """
    limiter: AppendConcurrencyLimiter = request.app.state.append_concurrency_limiter
    if not limiter.try_acquire():
        raise AppendConcurrencyLimitError("Too many concurrent uploads; retry shortly.")
    try:
        ref = artifact_ref(env, path, name)
        blob = await run_in_threadpool(service.receive_blob, principal, ref, upload_id, sha256)
        finished = False
        try:
            idle = request.app.state.settings.append.read_timeout_seconds
            iterator = request.stream().__aiter__()
            pending = bytearray()
            while (chunk := await _next_file_chunk(blob, iterator, idle)) is not None:
                pending.extend(chunk)
                if len(pending) >= PIECE_BYTES:
                    await run_in_threadpool(blob.write, bytes(pending))
                    pending.clear()
            if pending:
                await run_in_threadpool(blob.write, bytes(pending))
            stored = await run_in_threadpool(blob.finish)
            finished = True
        finally:
            if not finished:
                await _abort(blob)
    finally:
        limiter.release()
    return ArtifactBlobOut(sha256=sha256, stored=stored)


@router.post(
    "/{name}/uploads/{upload_id}/commit",
    response_model=ArtifactVersionOut,
    status_code=201,
    responses=errors(403, 404, 409),
)
def commit_upload(
    env: str,
    path: str,
    name: str,
    upload_id: uuid.UUID,
    body: ArtifactCommitIn,
    principal: PrincipalRef = Depends(get_write_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactVersionOut:
    """Commit an artifact upload.

    Turns a complete upload into the artifact's next version. Needs `WRITE_TABLE`. Answers
    409 when the upload expired, was committed already or does not exist, or files of it are
    not stored yet; 404 when a source the version names does not exist; 422 when a source is
    malformed.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.
    upload_id
        The upload's id, as returned when it was begun.
    body
        What the version was made from, for lineage.
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    service
        The artifact service for this request.

    Returns
    -------
    ArtifactVersionOut
        The version, and the numbers of the versions it pruned.

    Raises
    ------
    lakana_core.errors.ConflictError
        Mapped to 409, if the upload expired, was committed already or does not exist, or files
        of it are not stored yet.
    lakana_core.errors.ValidationError
        Mapped to 422, if a source is malformed.
    lakana_core.errors.PermissionDenied
        Mapped to 403.
    lakana_core.errors.NotFoundError
        Mapped to 404, if a source does not exist.
    """
    result = service.commit_upload(
        principal,
        artifact_ref(env, path, name),
        upload_id,
        sources=[ArtifactSource(s.name, s.version) for s in body.sources],
    )
    return version_out(result.version, result.deleted_versions)


@router.delete("/{name}/uploads/{upload_id}", status_code=204, responses=errors(403, 404, 409))
def abort_upload(
    env: str,
    path: str,
    name: str,
    upload_id: uuid.UUID,
    principal: PrincipalRef = Depends(get_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> None:
    """Withdraw an upload.

    Withdraws an upload that was begun and will not be finished. Only the caller that began
    it (for a task, the same attempt) can, and only while it is open. Files it stored stay
    until the periodic clean-up. Answers 403 with one text whether or not the artifact or
    upload exists, 404 when the caller may write the name and no artifact has it, 409 for an
    upload that does not exist, is another caller's, expired or was committed, and 422 when
    the name belongs to a table or a view.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.
    upload_id
        The upload's id, as returned when it was begun.
    principal
        The authenticated caller.
    service
        The artifact service for this request.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, with one text whether or not the artifact or the upload exists.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the caller may write the name and no artifact has it.
    lakana_core.errors.ValidationError
        Mapped to 422, if the name belongs to a table or a view, or the upload id is malformed.
    lakana_core.errors.ConflictError
        Mapped to 409, with one text for an upload that does not exist, is another caller's,
        expired or was committed.
    """
    service.abort_upload(principal, artifact_ref(env, path, name), upload_id)


@router.delete(
    "/{name}/versions/{version}",
    response_model=ArtifactVersionDeletedOut,
    responses=errors(403, 404, 409),
)
def delete_version(
    env: str,
    path: str,
    name: str,
    version: str,
    principal: PrincipalRef = Depends(get_write_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactVersionDeletedOut:
    """Delete one version of an artifact.

    Deletes the bytes of one version; its record and its lineage stay. Needs `DROP_TABLE` on
    the name; a task token is always refused, and in a protected environment a reason for
    breaking the protection is needed. The version is gone for every reader when this
    answers. Large versions are removed in part by this request and in part by the server's
    periodic clean-up. The answer says how many files and bytes were freed and how many
    stayed, with the reason. Answers 403 with one text whether or not the artifact or
    version exists, 404 when the caller may drop the name and the artifact or version is
    absent, 409 when the version is deleted already and 422 when the version is not a
    positive number or the name is not an artifact.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.
    version
        The version number: a positive number, not ``current``.
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    service
        The artifact service for this request.

    Returns
    -------
    ArtifactVersionDeletedOut
        The version, now with `deleted_at` and `deleted_by` and no files, and the freed and
        kept counts.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422, if the version is not a bounded positive number or the name is not an
        artifact.
    lakana_core.errors.PermissionDenied
        Mapped to 403: no `DROP_TABLE`, a task token, or a protected environment without
        break-glass. One text whether or not the artifact or the version exists.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the caller may drop the name and the artifact or version is absent.
    lakana_core.errors.ConflictError
        Mapped to 409, if the version is already deleted.
    """
    number = parse_version(version, allow_current=False)
    assert number is not None  # nosec B101 -- `current` is not allowed here
    return deleted_out(service.delete_version(principal, artifact_ref(env, path, name), number))


@router.delete("/{name}", status_code=204, responses=errors(403, 404, 409))
def drop_artifact(
    env: str,
    path: str,
    name: str,
    principal: PrincipalRef = Depends(get_write_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> None:
    """Drop an artifact.

    Removes the artifact with all its versions' bytes, its rows and its grants. This cannot
    be undone. Needs `DROP_TABLE` on the name; a task token is always refused, and in a
    protected environment a reason for breaking the protection is needed. An artifact with
    thousands of files takes a while; if the server fails part-way, its periodic clean-up
    finishes the job. Answers 403 with one text whether or not the artifact exists, 404 when
    the caller may drop the name and no artifact has it, 409 when the storage could not be
    written (nothing was deleted) and 422 when the name belongs to a table or a view.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.
    principal
        The authenticated caller, with any `?break_glass_reason=` attached.
    service
        The artifact service for this request.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403: no `DROP_TABLE`, a task token, or a protected environment without
        break-glass. One text whether or not the artifact exists.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the caller may drop the name and no artifact has it.
    lakana_core.errors.ValidationError
        Mapped to 422, if the name belongs to a table or a view.
    lakana_core.errors.ConflictError
        Mapped to 409, if the storage could not be written; nothing was deleted.
    """
    service.drop_artifact(principal, artifact_ref(env, path, name))


def _encode_cursor(cursor: UploadCursor) -> str:
    """Encode a position in the uploads list as an opaque, URL-safe cursor.

    Parameters
    ----------
    cursor
        The position of the last upload of the page being returned.

    Returns
    -------
    str
        Unpadded base64url of `{"c": created_at, "i": id}`.
    """
    raw = json.dumps({"c": cursor.created_at.isoformat(), "i": str(cursor.id)}).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(text: str) -> UploadCursor:
    """Decode a cursor produced by `_encode_cursor`.

    Parameters
    ----------
    text
        The `?cursor=` value a client sent back.

    Returns
    -------
    UploadCursor
        The position to continue after.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422, if `text` is not a cursor this server could have issued: not base64url or
        JSON, not an object with a string time and a string id, a time without a zone or one that
        cannot be converted to UTC, or an id that is not a UUID. It is never echoed.
    """
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        if not (
            isinstance(data, dict)
            and isinstance(data.get("c"), str)
            and isinstance(data.get("i"), str)
        ):
            raise ValidationError(_BAD_CURSOR)
        created_at = datetime.fromisoformat(data["c"])
        if created_at.tzinfo is None:
            raise ValidationError(_BAD_CURSOR)
        # the database binds UTC: a time that cannot be converted would fail there, as a 500
        created_at = created_at.astimezone(UTC)
        upload_id = uuid.UUID(data["i"])
    except (binascii.Error, ValueError, OverflowError) as exc:
        raise ValidationError(_BAD_CURSOR) from exc
    return UploadCursor(created_at, upload_id)


@environment_router.get("/uploads", response_model=ArtifactUploadPage, responses=errors(403, 404))
def list_uploads(
    env: str,
    name: str | None = Query(default=None, max_length=1400),
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalRef = Depends(get_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactUploadPage:
    """List your open uploads.

    Lists the open uploads the caller began in the environment, newest first; for a task,
    those of its own attempt. Other principals' uploads are never listed and nothing says
    they exist. An upload that was begun and never finished holds one of its artifact's
    upload places until it expires; the answer gives the id that the withdraw route needs.
    Needs `READ_ENVIRONMENT`. Answers 404 when the environment does not exist and 422 for a
    `name` that is not `namespace.name` or a `cursor` this server did not issue.

    Parameters
    ----------
    env
        Environment name.
    name
        Only this artifact, as ``namespace.name``.
    limit
        Most uploads in the page (1 to 500).
    cursor
        A previous page's `next_cursor`, to fetch the page after it. The same `name` must be sent
        with every page.
    principal
        The authenticated caller.
    service
        The artifact service for this request.

    Returns
    -------
    ArtifactUploadPage
        Up to `limit` uploads, and the cursor of the next page.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, if the caller may not read the environment.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment does not exist.
    lakana_core.errors.ValidationError
        Mapped to 422, if `name` is not ``namespace.name`` or `cursor` is not one this server
        issued.
    """
    position = None if cursor is None else _decode_cursor(cursor)
    page = service.list_uploads(principal, env, name=name, limit=limit, after=position)
    return ArtifactUploadPage(
        items=[
            ArtifactUploadItemOut(
                upload_id=str(u.upload_id),
                artifact=u.ref.dotted(),
                created_at=u.created_at,
                expires_at=u.expires_at,
            )
            for u in page.items
        ],
        next_cursor=None if page.next is None else _encode_cursor(page.next),
    )


@environment_router.get("", response_model=list[ArtifactOut], responses=errors(403, 404))
def list_artifacts(
    env: str,
    namespace: str | None = Query(default=None, max_length=1024),
    principal: PrincipalRef = Depends(get_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> list[ArtifactOut]:
    """List the artifacts of an environment.

    Lists the artifacts of the environment that the caller may read. A caller who may read
    the environment but no artifact gets an empty list, so that an empty list reveals
    nothing. Needs `READ_ENVIRONMENT`. Answers 404 when the environment does not exist and
    422 when the namespace path is malformed.

    Parameters
    ----------
    env
        Environment name.
    namespace
        Only this dot-joined namespace path.
    principal
        The authenticated caller.
    service
        The artifact service for this request.

    Returns
    -------
    list of ArtifactOut
        Ordered by namespace and name.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, if the caller may not read the environment.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the environment does not exist.
    lakana_core.errors.ValidationError
        Mapped to 422, if the namespace path is malformed.
    """
    parsed = None if namespace is None else NamespacePath.from_parts(namespace.split("."))
    return [artifact_out(view) for view in service.list_artifacts(principal, env, parsed)]


@router.get("/{name}", response_model=ArtifactOut, responses=errors(403, 404))
def get_artifact(
    env: str,
    path: str,
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactOut:
    """Show an artifact.

    Needs `READ_TABLE` on the name. Answers 403 with one text whether or not the artifact
    exists, 404 when the caller may read the name and no artifact has it, and 422 when the
    name belongs to a table or a view.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.
    principal
        The authenticated caller.
    service
        The artifact service for this request.

    Returns
    -------
    ArtifactOut
        The artifact.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, with one text whether or not the artifact exists.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the caller may read the name and no artifact has it.
    lakana_core.errors.ValidationError
        Mapped to 422, if the name belongs to a table or a view.
    """
    return artifact_out(service.get_artifact(principal, artifact_ref(env, path, name)))


@router.get("/{name}/versions", response_model=list[ArtifactVersionOut], responses=errors(403, 404))
def list_versions(
    env: str,
    path: str,
    name: str,
    principal: PrincipalRef = Depends(get_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> list[ArtifactVersionOut]:
    """List the versions of an artifact.

    Lists the versions newest first, deleted ones included, without their files. A task
    token sees no run identifiers. Needs `READ_TABLE` on the name. Answers 403 with one text
    whether or not the artifact exists and 404 when the caller may read the name and no
    artifact has it.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.
    principal
        The authenticated caller.
    service
        The artifact service for this request.

    Returns
    -------
    list of ArtifactVersionOut
        The versions.

    Raises
    ------
    lakana_core.errors.PermissionDenied
        Mapped to 403, with one text whether or not the artifact exists.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the caller may read the name and no artifact has it.
    """
    withhold = is_task_caller(principal)
    return [
        version_out(view, withhold_producer=withhold)
        for view in service.list_versions(principal, artifact_ref(env, path, name))
    ]


@router.get(
    "/{name}/versions/{version}", response_model=ArtifactVersionOut, responses=errors(403, 404)
)
def get_version(
    env: str,
    path: str,
    name: str,
    version: str,
    principal: PrincipalRef = Depends(get_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> ArtifactVersionOut:
    """Show one version of an artifact.

    Shows the version with its files. `version` is a positive number or `current`. Needs
    `READ_TABLE` on the name. Answers 403 with one text whether or not the artifact or
    version exists, 404 when the caller may read the name and the artifact or version is
    absent, and 422 for a version that is not a bounded positive number or `current`.

    Parameters
    ----------
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.
    version
        A positive number, or ``current``.
    principal
        The authenticated caller.
    service
        The artifact service for this request.

    Returns
    -------
    ArtifactVersionOut
        The version; a task token gets no run identifiers.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422, if the version is not a bounded positive number or ``current``.
    lakana_core.errors.PermissionDenied
        Mapped to 403, with one text whether or not the artifact or the version exists.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the caller may read the name and the artifact or version is absent.
    """
    number = parse_version(version, allow_current=True)
    found = service.get_version(principal, artifact_ref(env, path, name), number)
    return version_out(found, withhold_producer=is_task_caller(principal))


class DownloadCut(Exception):
    """A download ended without sending what its manifest promised.

    Raised inside the response body, after the headers went out, so the server cuts the
    connection and the client sees an incomplete body. The text goes to the server's log; it
    names the artifact but never a storage path or storage text.
    """


def download_deadline(size: int) -> float:
    """Say how long a download of `size` bytes may take in all.

    Parameters
    ----------
    size
        The file's size in bytes.

    Returns
    -------
    float
        `DOWNLOAD_BASE_SECONDS` plus `size` at `DOWNLOAD_MIN_BYTES_PER_SECOND`.
    """
    return DOWNLOAD_BASE_SECONDS + size / DOWNLOAD_MIN_BYTES_PER_SECOND


def _close(chunks: Iterator[bytes]) -> None:
    """Close a storage iterator, whatever state it is in.

    Parameters
    ----------
    chunks
        The iterator `read_chunks` returned; one without `close` is left alone.
    """
    close = getattr(chunks, "close", None)
    if close is not None:
        with contextlib.suppress(Exception):
            close()


def _next_piece(chunks: Iterator[bytes]) -> bytes | None:
    """Read the next piece of a stored object, on a worker thread.

    Parameters
    ----------
    chunks
        The object's pieces.

    Returns
    -------
    bytes or None
        The piece, or `None` at the end.
    """
    return next(chunks, None)


def download_label(ref: TableRef, version: int) -> str:
    """Name a download for the log and for the error that cuts it off.

    Parameters
    ----------
    ref
        The artifact; its name came from the request.
    version
        The version number.

    Returns
    -------
    str
        ``name version N`` with the name escaped and bounded (`shown`), so that a line break or
        an escape sequence in a name cannot forge a log record.
    """
    return f"{shown(ref.dotted())} version {version}"


async def download_body(chunks: Iterator[bytes], size: int, *, label: str) -> AsyncIterator[bytes]:
    """Send a stored file in pieces and check it is the length the manifest says.

    Each piece is read in the threadpool; at most one is in memory. Exactly `size` bytes are
    sent: an object that is longer is cut off at `size`, one that ends early ends the response
    unfinished, and a storage failure after the first byte does too. The iterator is closed on
    every exit.

    Parameters
    ----------
    chunks
        The object's pieces.
    size
        The size the version's manifest declares, which `Content-Length` announced.
    label
        What is downloaded, for the log.

    Yields
    ------
    bytes
        The next piece.

    Raises
    ------
    DownloadCut
        If the object is not `size` bytes long or could not be read to the end.
    """
    sent = 0
    try:
        while True:
            try:
                piece = await run_in_threadpool(_next_piece, chunks)
            except Exception as exc:
                logger.warning("Reading %s from storage failed (%s).", label, type(exc).__name__)
                raise DownloadCut(f"{label}: storage failed mid-download") from None
            if piece is None:
                break
            if sent + len(piece) > size:
                if sent < size:
                    yield piece[: size - sent]
                logger.warning("The stored file of %s is longer than its manifest says.", label)
                raise DownloadCut(f"{label}: the stored file is longer than its manifest says")
            sent += len(piece)
            yield piece
        if sent != size:
            logger.warning("The stored file of %s is shorter than its manifest says.", label)
            raise DownloadCut(f"{label}: the stored file is shorter than its manifest says")
    finally:
        _close(chunks)


class DownloadResponse(StreamingResponse):
    """A file's bytes with a deadline and a guaranteed close of the storage iterator.

    Parameters
    ----------
    chunks
        The object's pieces, from `BlobDownload`.
    size
        The size the manifest declares; sent as `Content-Length`.
    label
        What is downloaded, for the log.
    deadline
        Seconds the whole response may take, from the first call. A client that stops reading
        stalls inside `send`; this is what ends it.
    release
        Gives back the download slot the route took; called once, after the iterator is closed,
        on every exit. This is also how a test observes that the response has closed.
    """

    def __init__(
        self,
        chunks: Iterator[bytes],
        *,
        size: int,
        label: str,
        deadline: float,
        release: Callable[[], None],
    ) -> None:
        super().__init__(
            download_body(chunks, size, label=label),
            media_type="application/octet-stream",
            headers={"Content-Length": str(size)},
        )
        self._chunks = chunks
        self._label = label
        self._deadline = deadline
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the response, ending it at the deadline.

        Parameters
        ----------
        scope
            The ASGI scope.
        receive
            The ASGI receive channel.
        send
            The ASGI send channel.

        Raises
        ------
        DownloadCut
            If the deadline passed first; the server cuts the connection.
        """
        try:
            async with asyncio.timeout(self._deadline):
                await super().__call__(scope, receive, send)
        except TimeoutError:
            logger.warning(
                "The download of %s took longer than %.0fs and was cut off.",
                self._label,
                self._deadline,
            )
            raise DownloadCut(f"{self._label}: the download took too long") from None
        finally:
            _close(self._chunks)
            self._release()


@router.get(
    "/{name}/versions/{version}/blobs/{sha256}",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/octet-stream": {}}}, **errors(403, 404, 503)},
)
async def get_blob(
    request: Request,
    env: str,
    path: str,
    name: str,
    version: str,
    sha256: str = Path(pattern=r"^[0-9a-f]{64}$"),
    principal: PrincipalRef = Depends(get_principal),
    service: ArtifactService = Depends(get_artifact_service),
) -> StreamingResponse:
    """Download one file of an artifact version.

    Streams the file as `application/octet-stream` with its size as `Content-Length`; the
    download is audited. Needs `READ_TABLE` on the name. Answers 403 with one text whether
    or not the artifact, version or file exists, 404 when the caller may read the name and
    the version, file or its bytes are absent or the version was deleted, 422 for a
    malformed version or hash, and 503 when too many files are being sent or storage could
    not be opened (try again after `Retry-After`).

    Parameters
    ----------
    request
        The current request; its app holds the download slots.
    env
        Environment name.
    path
        Dot-joined namespace path.
    name
        Artifact name.
    version
        A positive number (not ``current``).
    sha256
        The file's SHA-256, 64 lower-case hexadecimal digits.
    principal
        The authenticated caller.
    service
        The artifact service for this request.

    Returns
    -------
    StreamingResponse
        The file's bytes as `application/octet-stream`.

    Raises
    ------
    lakana_core.errors.ValidationError
        Mapped to 422, if the version or the hash is malformed, or the name is not an artifact.
    lakana_core.errors.PermissionDenied
        Mapped to 403, with one text whether or not the artifact, version or file exists.
    lakana_core.errors.NotFoundError
        Mapped to 404, if the caller may read the name and the version, the file or its bytes
        are absent, or the version was deleted.
    lakana_server.errors.DownloadConcurrencyLimitError
        Mapped to 503 with `Retry-After: 1`, if `MAX_CONCURRENT_DOWNLOADS` files are being sent.
    lakana_core.errors.StorageUnavailableError
        Mapped to 503 with `Retry-After: 5`, if storage could not be opened.
    """
    number = parse_version(version, allow_current=False)
    assert number is not None  # nosec B101 -- `current` is not allowed here
    ref = artifact_ref(env, path, name)
    slots: AppendConcurrencyLimiter = request.app.state.download_slots
    if not slots.try_acquire():
        raise DownloadConcurrencyLimitError(
            "The server is busy with other downloads; try again in a moment."
        )
    try:
        download = await run_in_threadpool(service.open_blob, principal, ref, number, sha256)
        return DownloadResponse(
            download.chunks,
            size=download.size,
            label=download_label(ref, number),
            deadline=download_deadline(download.size),
            release=slots.release,
        )
    except BaseException:
        slots.release()
        raise
