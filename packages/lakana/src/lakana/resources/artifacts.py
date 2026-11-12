"""The `artifacts` resource: `Client.artifacts`.

An artifact is a versioned file or directory, such as a trained model, stored in the catalog next
to tables and addressed like one: a dot-joined ``"namespace.name"``. `upload` scans a local file
or directory, asks the server which of its files it does not have yet, streams each of those from
disk and commits the version. Nothing here holds a whole file in memory, and nothing reaches the
server except through its REST routes.

What an upload does when something goes wrong:

- a tree the server would refuse (a link, a device, a Windows-unsafe name, too many files) is
  refused before the first request, naming the path;
- a file that changes between the scan and its upload is detected as it is sent (the hash sent
  must be the hash scanned): the request is cut off, nothing is stored from it, and the error
  says to run the upload again;
- a file the server answers 503 is sent again a few times (the route is addressed by hash, so a
  second copy is answered "already had it"); opening an upload is asked again only after the
  server's own busy answer (`AppendConcurrencyLimitError`), as a proxy's 503 may follow a
  request the server processed and a second upload would be opened;
- an upload that expired, or whose files were swept before the commit, starts again from the
  manifest, at most `_UPLOAD_ROUNDS` times;
- a commit whose answer is lost (a timeout, a broken connection or a 5xx) is sent again, the same
  commit for the same upload, a few times with a growing wait: the server answers a repeated
  commit with the version the first one stored and adds nothing. If every attempt
  fails the error names the upload and says what is known;
- a 409 saying the upload is gone starts again from the manifest: the upload truly expired;
- an upload that gives up before it sent the commit (a file that changed, a server that stayed
  busy, a Ctrl-C) withdraws its own open upload, best effort: an error while withdrawing never
  hides the first error, and nothing is withdrawn once the commit request was sent, because
  the version may exist;
- the local scan keeps to the limits the server's operator set (read once from the info route;
  an older server that sends none means the defaults), unless the caller passes its own.

`download` is the other direction. It validates the manifest the server sent with this package's
own path rules before it writes anything, writes into a staging directory beside the target
without following a link, verifies every file's size and SHA-256 and moves the finished tree to
the target in one step (`lakana._artifact_tree`). A failed download leaves nothing at the target.
"""

from __future__ import annotations

import builtins
import contextlib
import dataclasses
import hashlib
import os
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx

from lakana import _http
from lakana._artifact_manifest import (
    CHANGED,
    READ_BYTES,
    LocalFile,
    ManifestError,
    ScanLimits,
    StoredManifestDamaged,
    StoredManifestInvalid,
    manifest_digest,
    open_regular,
    scan,
    shown,
)
from lakana._artifact_tree import TreeWriter, check_manifest
from lakana._http import (
    BUSY_ATTEMPTS,
    BUSY_BACKOFF_SECONDS,
    HttpTransport,
    query_params,
    retry_wait,
)
from lakana._models import (
    Artifact,
    ArtifactDownload,
    ArtifactFile,
    ArtifactKept,
    ArtifactUpload,
    ArtifactUploadPage,
    ArtifactVersion,
    DeletedArtifactVersion,
)
from lakana._routes import ROUTES
from lakana.errors import (
    Conflict,
    InvalidResponse,
    LakanaConnectionError,
    LakanaError,
    NotFound,
    ServerError,
    ValidationError,
)
from lakana.resources.tables import _split_namespace_table

_BLOB_TIMEOUT_SECONDS = 300.0
"""Per-operation timeout for one file's upload.

httpx applies a timeout to each connect, write and read, not to the whole request, so this is how
long the connection may stall, not how long a file may take: a multi-gigabyte file that keeps
moving never hits it. The read part also covers the server's verification after the last byte
(hashing is done on the way; the finish takes the artifact's lock and renames the file)."""

_COMMIT_TIMEOUT_SECONDS = 120.0
"""Timeout for the commit request, longer than the client's 30 s default.

A commit that applies `keep_versions` deletes up to 200 files, or for 5 seconds, inside the
request, on top of its own transaction; the default timeout would turn a slow success into an
ambiguous failure."""

_DELETE_TIMEOUT_SECONDS = 120.0
"""Timeout for deleting a version, longer than the client's 30 s default.

The request marks the version and removes up to 200 files, or for 5 seconds, from storage before
it answers (what is left is the server's periodic clean-up), so the commit's timeout fits."""

_DROP_TIMEOUT_SECONDS = 600.0
"""Timeout for dropping an artifact: ten minutes.

A drop removes every file of every version from storage inside the request, with no budget, and
an artifact may hold tens of thousands of objects (10,000 files in each of many versions); on an
object store a delete takes tens of milliseconds. A drop that outlasts this has not failed: the
server goes on, and what it leaves the periodic clean-up removes."""

_WITHDRAW_TIMEOUT_SECONDS = 5.0
"""Timeout for withdrawing an upload the client gives up: a best-effort clean-up that must not keep
a failed upload (or a person's Ctrl-C) waiting for the client's 30 s default."""

_DOWNLOAD_TIMEOUT_SECONDS = 120.0
"""Per-operation timeout for one file's download: how long the connection may stall (httpx
applies it to each connect and read, not to the whole file)."""

_UPLOAD_ROUNDS = 3
"""Times an upload is started from the manifest when the server lost it."""

_DOWNLOAD_ATTEMPTS = 6
"""Times one file is asked for when the server answers 503 (every download slot is taken, or
storage could not be opened). A download can hold a slot for minutes, so the waits grow: 1, 2, 4,
8 and 16 seconds, about 30 in all."""

_DOWNLOAD_BACKOFF_SECONDS = 1.0
"""Seconds waited after the first 503 of a file; each further wait doubles."""

_BUSY_TYPE = "DownloadConcurrencyLimitError"
"""The `type` of the server's 503 for a full set of download slots (storage that could not be
opened has another)."""

_UPLOAD_BUSY_TYPE = "AppendConcurrencyLimitError"
"""The `type` of the server's 503 when every upload slot is taken: it answers a manifest request
before reading it, so nothing was created. Only that answer lets `begin` ask again; a proxy's 503
may follow a request the server processed, and asking again could open a second upload. A file
is different: its route is addressed by hash, a second copy is answered "already had it" and
thrown away, so every 503 on a file is sent again."""

_UPLOAD_GONE = "This upload has expired or does not exist; start the upload again."
_NOT_STORED = "files are not stored yet"

_COMMIT_ATTEMPTS = 4
"""Times the same commit is sent when its answer is lost or a 5xx; a repeat is safe."""

_COMMIT_BACKOFF_SECONDS = 2.0
"""Seconds before the second attempt; each further wait doubles (2, 4, 8)."""

_LOST_COMMIT = (
    "The server did not confirm the commit of {name} (upload {upload}) after {attempts} tries "
    "({cause}), so the version may or may not have been stored. If the server is reachable, run "
    "`lakana --output json artifact versions list {name} --env {environment}` to see whether it "
    "was (the table may leave the digest out on a narrow terminal; the JSON never does): look for "
    "a new version with a manifest_sha256 starting {digest}. Run the upload again only if it was "
    "not: a second upload of files the server already has sends nothing, but stores the same "
    "content as one more version and, if the asset limits its versions, may remove one more old "
    "version."
)
_COMMIT_NOT_SENT = (
    "The connection to the server failed before the commit of {name} (upload {upload}) was sent, "
    "after {attempts} tries ({cause}), so the version was not stored. Check the network and the "
    "profile's URL, then run the upload again: the files the server already has are not sent "
    "twice."
)
_GATEWAY_STATUSES = (502, 503, 504)
"""The statuses of a proxy or gateway that may have passed a request on and lost the answer."""

_DELETE_LOST = (
    "The server did not answer the request to delete version {version} of {environment}/{name} "
    "({cause}), so the version may or may not have been deleted. Run `lakana artifact versions "
    "list {name} --env {environment}`: if it is not shown as deleted, run the delete again."
)
_DROP_LOST = (
    "The server did not answer the request to drop {environment}/{name} ({cause}), so it may "
    "or may not be dropped; if the server is still working, it finishes by itself. Run "
    "`lakana artifact list --env {environment}`: if {name} is still listed, run the drop again."
)
_DELETE_NOT_SENT = (
    "The connection to the server failed before the request to delete version {version} of "
    "{environment}/{name} was sent ({cause}), so it was not deleted. Check the network and the "
    "profile's URL, then run the delete again."
)
_DROP_NOT_SENT = (
    "The connection to the server failed before the request to drop {environment}/{name} was "
    "sent ({cause}), so it was not dropped. Check the network and the profile's URL, then run "
    "the drop again."
)
_NOT_UPLOAD_ID = (
    "{given!r} is not an upload id. An upload id is a UUID such as "
    "3f2b9c1e-5a4d-4c8e-9b1a-0d6e7f8a9b0c; `lakana artifact upload` names the upload in its "
    "error message when it cannot tell whether a commit went through."
)
_NOT_REACHED = (
    "The connection to the server failed while downloading {path} ({cause}); nothing was kept. "
    "Check the network and the profile's URL, then download it again."
)
_CUT = (
    "The connection to the server ended before {path} was complete; nothing was kept. Check the "
    "network, then download it again. If it fails again, the server may be ending downloads "
    "slower than 64 KiB/s (about 0.5 Mbit/s): use a faster connection, or an artifact with "
    "smaller files."
)
_BUSY_DOWNLOAD = (
    "The server was busy with other downloads while downloading {path} and did not recover "
    "after {attempts} tries over about 30 seconds (the server said: {cause}). Nothing was kept; "
    "try again in a minute. If it keeps happening, tell the operator that downloads are held up."
)
_NOT_SERVED = (
    "The server could not serve {path} and did not recover after {attempts} tries over about "
    "30 seconds (the server said: {cause}). Nothing was kept; try again in a minute. If it keeps "
    "happening, tell the operator what the server said."
)
_GIVE_UP = (
    "The upload of {name} was lost {rounds} times in a row ({cause}). Run the upload again in a "
    "minute; if it keeps happening, tell the operator that uploads of this artifact expire "
    "before they finish."
)
_NOT_IN_UPLOAD = (
    "The server asked for a file this upload does not contain, which a Lakana server does not "
    "do. The upload was withdrawn. Check the URL and that the server and this SDK are the same "
    "version, then run the upload again."
)
_VERSION_DELETED = (
    "Version {version} of {name} was deleted; its bytes are gone. Read a version that exists: "
    "`lakana artifact versions list {name} --env {environment}` shows which."
)
_ODD_ANSWER = "The server's answer to {what} was not what a Lakana server sends. Check the URL."


def _wait(seconds: float) -> None:
    """Wait before asking again.

    Parameters
    ----------
    seconds
        How long.
    """
    _http.wait(seconds)


def _object(response: Any, what: str) -> dict[str, Any]:  # ruff: ignore[any-type]
    """Read a response's JSON object, or say that the server did not answer like a server.

    Parameters
    ----------
    response
        The `httpx.Response`.
    what
        What was asked, for the message.

    Returns
    -------
    dict
        The decoded object.

    Raises
    ------
    InvalidResponse
        If the body is not a JSON object.
    """
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise InvalidResponse(_ODD_ANSWER.format(what=what))
    return data


def _array(response: Any, what: str) -> list[Any]:  # ruff: ignore[any-type]
    """Read a response's JSON array, or say that the server did not answer like a server.

    Parameters
    ----------
    response
        The `httpx.Response`.
    what
        What was asked, for the message.

    Returns
    -------
    list
        The decoded array.

    Raises
    ------
    InvalidResponse
        If the body is not a JSON array.
    """
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, list):
        raise InvalidResponse(_ODD_ANSWER.format(what=what))
    return data


def artifact_from_json(data: object) -> Artifact:
    """Build an `Artifact` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `ArtifactOut` JSON body.

    Returns
    -------
    Artifact
        The parsed artifact.

    Raises
    ------
    InvalidResponse
        If a field an artifact always has is missing or has the wrong type.
    """
    try:
        assert isinstance(data, dict)  # nosec B101 -- a non-object takes the error path below
        return Artifact(
            name=str(data["name"]),
            namespace=str(data["namespace"]),
            uuid=str(data["uuid"]),
            kind=str(data["kind"]),
            current_version=data["current_version"],
            versions=int(data["versions"]),
            size_bytes=int(data["size_bytes"]),
            created_at=str(data["created_at"]),
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="an artifact")) from None


def _file_chunks(file: LocalFile) -> Iterator[bytes]:
    """Read a local file in pieces, without following a link, checking it is the file scanned.

    The hash of what is sent is computed on the way. A file that grew, shrank or has other
    content than when it was scanned ends the stream with an error instead of a clean end: the
    request is cut off, the server throws the bytes away, and nothing wrong is ever stored.

    Parameters
    ----------
    file
        The file, as scanned.

    Yields
    ------
    bytes
        At most `READ_BYTES` at a time.

    Raises
    ------
    ManifestError
        If the file cannot be read or is not the one that was scanned.
    """
    descriptor = open_regular(file.source, file.path, file.root)
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(descriptor, "rb") as handle:
        try:
            while chunk := handle.read(READ_BYTES):
                size += len(chunk)
                if size > file.size:
                    raise ManifestError(CHANGED.format(path=shown(file.path)))
                digest.update(chunk)
                yield chunk
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise ManifestError(
                f"{shown(file.path)} could not be read: {reason}. "
                "The upload was stopped and can be tried again once the file is readable."
            ) from exc
    if size != file.size or digest.hexdigest() != file.sha256:
        raise ManifestError(CHANGED.format(path=shown(file.path)))


def version_from_json(data: dict[str, Any]) -> ArtifactVersion:
    """Build an `ArtifactVersion` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `ArtifactVersionOut` JSON body.

    Returns
    -------
    ArtifactVersion
        The parsed version.

    Raises
    ------
    InvalidResponse
        If a field a version always has is missing or has the wrong type.
    """
    try:
        return _version(data)
    except (KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="a version")) from None


def _version(data: dict[str, Any]) -> ArtifactVersion:
    """Build the version, letting a malformed body raise what it raises.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    ArtifactVersion
        The version.

    Raises
    ------
    TypeError
        If a number is not an integer (a boolean is not one) or `is_directory` is not a boolean.
    """
    for key in ("version", "size_bytes", "file_count"):
        if isinstance(data[key], bool) or not isinstance(data[key], int):
            raise TypeError(key)
    if not isinstance(data["is_directory"], bool):
        raise TypeError("is_directory")
    return ArtifactVersion(
        version=data["version"],
        manifest_sha256=data["manifest_sha256"],
        size_bytes=data["size_bytes"],
        file_count=data["file_count"],
        is_directory=data["is_directory"],
        task_run_id=data.get("task_run_id"),
        run_id=data.get("run_id"),
        project_version_id=data.get("project_version_id"),
        commit_sha=data.get("commit_sha"),
        created_by=data.get("created_by"),
        created_at=data["created_at"],
        deleted_at=data.get("deleted_at"),
        deleted_by=data.get("deleted_by"),
        files=[ArtifactFile(f["path"], f["size"], f["sha256"]) for f in data.get("files", [])],
        deleted_versions=list(data.get("deleted_versions", [])),
    )


def _word(value: object) -> str:
    """Check that a reason in an answer is text.

    Parameters
    ----------
    value
        The decoded JSON value.

    Returns
    -------
    str
        The text.

    Raises
    ------
    TypeError
        If it is not a string.
    """
    if not isinstance(value, str):
        raise TypeError("a word")
    return value


def _whole(value: object) -> int:
    """Check that a count in an answer is a whole number.

    Parameters
    ----------
    value
        The decoded JSON value.

    Returns
    -------
    int
        The count.

    Raises
    ------
    TypeError
        If it is not an integer (a boolean is not one).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("a count")
    return value


def deleted_version_from_json(data: dict[str, Any]) -> DeletedArtifactVersion:
    """Build a `DeletedArtifactVersion` from the answer to a delete.

    Parameters
    ----------
    data
        The decoded `ArtifactVersionDeletedOut` JSON body; the counts may be missing, as from a
        server older than the release that reports them.

    Returns
    -------
    DeletedArtifactVersion
        The version, with the counts it reports.

    Raises
    ------
    InvalidResponse
        If a field a version always has, or a count that is present, is missing or has the
        wrong type.
    """
    try:
        version = _version(data)
        kept = data.get("kept", [])
        if not isinstance(kept, list):
            raise InvalidResponse(_ODD_ANSWER.format(what="a version"))
        groups = [
            ArtifactKept(
                reason=_word(group["reason"]),
                files=_whole(group["files"]),
                bytes=_whole(group["bytes"]),
                version=None if group.get("version") is None else _whole(group["version"]),
            )
            for group in kept
        ]
        return DeletedArtifactVersion(
            **{f.name: getattr(version, f.name) for f in dataclasses.fields(version)},
            freed_files=None if "freed_files" not in data else _whole(data["freed_files"]),
            freed_bytes=None if "freed_bytes" not in data else _whole(data["freed_bytes"]),
            kept_files=None if "kept_files" not in data else _whole(data["kept_files"]),
            kept_bytes=None if "kept_bytes" not in data else _whole(data["kept_bytes"]),
            kept=groups,
        )
    except (KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="a version")) from None


def uploads_page_from_json(data: object) -> ArtifactUploadPage:
    """Build an `ArtifactUploadPage` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `ArtifactUploadPage` JSON body.

    Returns
    -------
    ArtifactUploadPage
        The parsed page.

    Raises
    ------
    InvalidResponse
        If the body is not an object with a list of uploads and a cursor that is text or
        absent, or an upload lacks a field or has one that is not text.
    """
    try:
        assert isinstance(data, dict)  # nosec B101 -- a non-object takes the error path below
        items = data["items"]
        assert isinstance(items, list)  # nosec B101 -- as above
        cursor = data["next_cursor"]
        assert cursor is None or isinstance(cursor, str)  # nosec B101 -- as above
        return ArtifactUploadPage(
            items=[
                ArtifactUpload(
                    upload_id=_word(item["upload_id"]),
                    artifact=_word(item["artifact"]),
                    created_at=_word(item["created_at"]),
                    expires_at=_word(item["expires_at"]),
                )
                for item in items
            ],
            next_cursor=cursor,
        )
    except (AssertionError, KeyError, TypeError):
        raise InvalidResponse(_ODD_ANSWER.format(what="a list of uploads")) from None


def _require_version(version: object) -> int:
    """Check a version number before a request is sent.

    Parameters
    ----------
    version
        What the caller gave.

    Returns
    -------
    int
        The number.

    Raises
    ------
    lakana.errors.ValidationError
        If it is not an integer of at least 1 (a `bool` is not one).
    """
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValidationError("A version is a positive number.")
    return version


def _restartable(exc: Conflict) -> bool:
    """Say whether a conflict means the upload was lost and a new one may succeed.

    Parameters
    ----------
    exc
        The 409 the server answered.

    Returns
    -------
    bool
        `True` for an expired or unknown upload and for a commit that found files not stored;
        `False` for every other conflict (too many open uploads, a name taken, a full artifact),
        which a new upload would meet again.
    """
    detail = str(exc)
    return detail == _UPLOAD_GONE or _NOT_STORED in detail


def _never_sent(exc: LakanaError) -> bool:
    """Say whether a failed request certainly did not reach the server.

    Parameters
    ----------
    exc
        What the transport raised.

    Returns
    -------
    bool
        `True` for a connection that was refused or timed out while opening; `False` for
        every other error, after which the server may have processed the request.
    """
    return isinstance(exc, LakanaConnectionError) and isinstance(
        exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout)
    )


def _lost_or_unsent(
    exc: LakanaError, lost: str, not_sent: str, **fields: object
) -> LakanaError | None:
    """Say what is known about a delete or drop that failed without a verdict.

    Parameters
    ----------
    exc
        What the transport raised.
    lost
        The message for an answer that may have been lost, with `{cause}` and the `fields`.
    not_sent
        The message for a connection that failed before anything was sent, likewise.
    **fields
        The values the messages name.

    Returns
    -------
    LakanaError or None
        A connection that was refused or timed out while opening: a `LakanaConnectionError` with
        `not_sent`. A timeout or broken connection after the request went out: a
        `LakanaConnectionError` with `lost`. A 502, 503 or 504 (a gateway that may have passed
        the request on and lost the answer): a `ServerError` with `lost` and the same status.
        `None` for every other error, which the caller raises as it is.
    """
    if isinstance(exc, LakanaConnectionError):
        if _never_sent(exc):
            return LakanaConnectionError(not_sent.format(cause=exc, **fields))
        return LakanaConnectionError(lost.format(cause=exc, **fields))
    if isinstance(exc, ServerError) and exc.status in _GATEWAY_STATUSES:
        return ServerError(
            lost.format(cause=exc, **fields), status=exc.status, error_type=exc.error_type
        )
    return None


class Artifacts:
    """Versioned files and directories, stored in the catalog next to tables.

    Parameters
    ----------
    transport
        The shared HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport
        self._limits: ScanLimits | None = None

    def _scan_limits(self) -> ScanLimits:
        """Return what the server's operator allows an artifact to hold, asking once.

        Returns
        -------
        ScanLimits
            The limits the info route sends; the defaults when the server sends none (an older
            server), sends something else, or cannot answer the question (it is not asked
            again). A server that cannot be reached is not hidden behind the defaults.

        Raises
        ------
        lakana.errors.LakanaConnectionError
            If the server could not be reached.
        """
        if self._limits is None:
            self._limits = self._read_limits()
        return self._limits

    def _read_limits(self) -> ScanLimits:
        """Ask the info route for the artifact limits.

        Returns
        -------
        ScanLimits
            The limits, or the defaults when the answer has none that make sense.

        Raises
        ------
        lakana.errors.LakanaConnectionError
            If the server could not be reached.
        """
        try:
            response = self._transport.request(ROUTES["info"], retry=False)
        except LakanaConnectionError:
            raise
        except LakanaError:
            return ScanLimits()
        try:
            data = response.json()
        except ValueError:
            return ScanLimits()
        sent = data.get("artifact_limits") if isinstance(data, dict) else None
        if not isinstance(sent, dict):
            return ScanLimits()
        values = [sent.get(key) for key in ("max_files", "max_file_bytes", "max_bytes")]
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in values):
            return ScanLimits()
        return ScanLimits(*values)

    def upload(
        self,
        environment: str,
        qualified_name: str,
        path: str | os.PathLike[str],
        *,
        sources: Mapping[str, int | None] | None = None,
        break_glass_reason: str | None = None,
        limits: ScanLimits | None = None,
    ) -> ArtifactVersion:
        """Store a local file or directory as the next version of an artifact.

        The artifact is created on first use. Only files the store does not have yet are sent,
        so a second version that changes one file sends one file. Version numbers are never
        reused and can have gaps: do not assume the result is the previous number plus one.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined ``"namespace.name"``.
        path
            A regular file (a single-file artifact) or a directory. Symbolic links, devices,
            sockets and pipes are refused. Everything in a directory is stored, including hidden
            files such as ``.env``: point it at a directory that holds only the artifact.
        sources
            What the version was made from, for lineage: a table, view or artifact name mapped
            to the artifact version read, or `None` for the current one.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited.
        limits
            What the local scan may look at before it gives up. `None` (the default) uses the
            limits the server's operator set, read once per client from the info route (the
            defaults when the server sends none).

        Returns
        -------
        ArtifactVersion
            The new version, with `files_sent` and `files_reused` set.

        Raises
        ------
        lakana.errors.ValidationError
            If the name is not dotted, the tree cannot be stored (`ManifestError`, naming the
            path), a file changed while it was uploaded, or the server refuses the manifest.
        lakana.errors.PermissionDenied
            If the caller may not write the name.
        lakana.errors.NotFound
            If the namespace does not exist.
        lakana.errors.Conflict
            If the upload kept getting lost, or the artifact has too many open uploads. An
            upload the server refused to commit is withdrawn before the error is raised.
        lakana.errors.LakanaConnectionError
            If the server could not be reached, or the answer to the commit stayed lost after
            every attempt (the version may exist; the message says how to find out).
        lakana.errors.InvalidResponse
            If the server's answer is not what a Lakana server sends.
        """
        namespace, name = _split_namespace_table(qualified_name)
        local = scan(Path(path), limits if limits is not None else self._scan_limits())
        by_hash: dict[str, LocalFile] = {}
        for file in local.files:
            by_hash.setdefault(file.sha256, file)
        params = {"break_glass_reason": break_glass_reason} if break_glass_reason else None
        path_params = {"env": environment, "path": namespace, "name": name}
        commit_body = {
            "sources": [{"name": n, "version": v} for n, v in sorted((sources or {}).items())]
        }

        def once() -> ArtifactVersion:
            return self._upload_once(
                local.document(),
                by_hash,
                (environment, qualified_name, local.digest()),
                path_params,
                params,
                commit_body,
                len(local.files),
            )

        for _ in range(_UPLOAD_ROUNDS - 1):
            try:
                return once()
            except Conflict as exc:
                if not _restartable(exc):
                    raise
        try:
            return once()
        except Conflict as exc:
            if not _restartable(exc):
                raise
            raise Conflict(
                _GIVE_UP.format(name=qualified_name, rounds=_UPLOAD_ROUNDS, cause=exc),
                status=exc.status,
                error_type=exc.error_type,
            ) from exc

    def _upload_once(
        self,
        document: dict[str, object],
        by_hash: Mapping[str, LocalFile],
        identity: tuple[str, str, str],
        path_params: Mapping[str, str],
        params: Mapping[str, object] | None,
        commit_body: Mapping[str, object],
        file_count: int,
    ) -> ArtifactVersion:
        """Begin an upload, send what is missing, commit.

        Parameters
        ----------
        document
            The manifest.
        by_hash
            One scanned file for each distinct hash.
        identity
            The environment, the artifact's dotted name and the manifest's digest, for messages.
        path_params
            The route's path parameters.
        params
            Query parameters sent with every request.
        commit_body
            The commit request's body.
        file_count
            How many files the manifest has.

        Returns
        -------
        ArtifactVersion
            The committed version.

        Raises
        ------
        lakana.errors.InvalidResponse
            If the server asks for a file this upload does not contain.
        lakana.errors.Conflict
            If the upload was lost on the server.
        lakana.errors.LakanaConnectionError
            If the commit's answer stayed lost after every attempt.
        lakana.errors.ServerError
            If the commit kept answering with a 5xx.
        """
        begun = _object(self._begin(path_params, params, document), "the start of the upload")
        upload_id, missing = begun.get("upload_id"), begun.get("missing")
        if (
            not isinstance(upload_id, str)
            or not isinstance(missing, list)
            or not all(isinstance(wanted, str) for wanted in missing)
        ):
            raise InvalidResponse(_ODD_ANSWER.format(what="the start of the upload"))
        upload_params = {**path_params, "upload_id": upload_id}
        try:
            for wanted in missing:
                file = by_hash.get(wanted)
                if file is None:
                    raise InvalidResponse(_NOT_IN_UPLOAD)
                self._send(upload_params, params, file)
        except BaseException as exc:
            # Only before the commit request exists: once it was sent the version may be stored.
            if not (isinstance(exc, Conflict) and str(exc) == _UPLOAD_GONE):
                self._withdraw(upload_params)
            raise
        response = self._commit(identity, upload_id, upload_params, params, commit_body)
        return dataclasses.replace(
            version_from_json(_object(response, "the commit")),
            files_sent=len(missing),
            files_reused=file_count - len(missing),
        )

    def _begin(
        self,
        path_params: Mapping[str, str],
        params: Mapping[str, object] | None,
        document: Mapping[str, object],
    ) -> Any:  # ruff: ignore[any-type]
        """Ask the server to open an upload, again a few times while it is busy.

        The server's own busy answer (a 503 of type `AppendConcurrencyLimitError`) comes before
        it read the manifest, so nothing was created and asking again is safe. Any other 503 is
        raised: a proxy's may follow a request the server processed.

        Parameters
        ----------
        path_params
            The route's path parameters.
        params
            Query parameters.
        document
            The manifest.

        Returns
        -------
        httpx.Response
            The server's answer.

        Raises
        ------
        lakana.errors.ServerError
            If the server stayed busy (its own 503) for every attempt, or answered any other error.
        """

        def begin() -> Any:  # ruff: ignore[any-type]
            return self._transport.request(
                ROUTES["artifacts.begin_upload"],
                path_params=path_params,
                params=params,
                json=document,
            )

        for _ in range(BUSY_ATTEMPTS - 1):
            try:
                return begin()
            except ServerError as exc:
                if exc.status != 503 or exc.error_type != _UPLOAD_BUSY_TYPE:
                    raise
                _wait(BUSY_BACKOFF_SECONDS)
        return begin()

    def _withdraw(self, upload_params: Mapping[str, str]) -> None:
        """Withdraw an upload this client gives up, without letting that fail the caller.

        Parameters
        ----------
        upload_params
            The route's path parameters, including the upload.
        """
        # The error that made the upload give up is the one the caller needs to see.
        with contextlib.suppress(Exception):
            self._transport.request(
                ROUTES["artifacts.abort_upload"],
                path_params=upload_params,
                timeout=_WITHDRAW_TIMEOUT_SECONDS,
                retry=False,
            )

    def _commit(
        self,
        identity: tuple[str, str, str],
        upload_id: str,
        upload_params: Mapping[str, str],
        params: Mapping[str, object] | None,
        commit_body: Mapping[str, object],
    ) -> Any:  # ruff: ignore[any-type]
        """Send the commit, and the same commit again while its answer is lost.

        A timeout, a broken connection or a 5xx says nothing about whether the server stored
        the version. The server answers a repeated commit of one upload with the version the
        first one stored and adds none, so asking again cannot duplicate it.

        Parameters
        ----------
        identity
            The environment, the artifact's dotted name and the manifest's digest, for messages.
        upload_id
            The upload.
        upload_params
            The route's path parameters, including the upload.
        params
            Query parameters.
        commit_body
            The request's body.

        Returns
        -------
        httpx.Response
            The server's answer.

        Raises
        ------
        lakana.errors.LakanaConnectionError
            If every attempt got no answer.
        lakana.errors.ServerError
            If every attempt got a 5xx.
        lakana.errors.LakanaError
            If the first attempt was answered 409 "upload gone" (a `Conflict`: start again), or
            with any other refusal; a 4xx after an attempt whose answer was lost is raised as
            it is. "Gone" after a re-send raises the error of the lost attempt instead, because
            the first one may have stored the version. A 4xx other than "gone" on a first
            attempt that was answered withdraws the upload (best effort) before it is raised,
            since it proves that no version was stored; after a lost answer nothing is
            withdrawn.
        """

        def send() -> Any:  # ruff: ignore[any-type]
            return self._transport.request(
                ROUTES["artifacts.commit_upload"],
                path_params=upload_params,
                params=params,
                json=commit_body,
                timeout=_COMMIT_TIMEOUT_SECONDS,
            )

        lost: LakanaConnectionError | ServerError | None = None
        reached = False
        for attempt in range(_COMMIT_ATTEMPTS):
            try:
                return send()
            except (LakanaConnectionError, ServerError) as exc:
                lost = exc
                reached = reached or not _never_sent(exc)
                if attempt < _COMMIT_ATTEMPTS - 1:
                    _wait(_COMMIT_BACKOFF_SECONDS * 2**attempt)
            except LakanaError as exc:
                gone = isinstance(exc, Conflict) and str(exc) == _UPLOAD_GONE
                if gone and lost is not None:
                    # The first attempt may have stored the version and the upload since
                    # expired: starting again would store it twice.
                    raise self._unconfirmed(
                        identity, upload_id, attempt + 1, lost, reached=reached
                    ) from exc
                if lost is None and not gone and 400 <= (exc.status or 0) < 500:
                    # A definite refusal proves no version exists, and nothing else will close
                    # this upload before it expires.
                    self._withdraw(upload_params)
                raise
        assert lost is not None  # nosec B101 -- the loop ends only after a lost attempt
        raise self._unconfirmed(
            identity, upload_id, _COMMIT_ATTEMPTS, lost, reached=reached
        ) from lost

    @staticmethod
    def _unconfirmed(
        identity: tuple[str, str, str],
        upload_id: str,
        attempts: int,
        lost: LakanaConnectionError | ServerError,
        *,
        reached: bool,
    ) -> LakanaConnectionError | ServerError:
        """Build the error for a commit whose outcome the server never confirmed.

        Parameters
        ----------
        identity
            The environment, the artifact's dotted name and the manifest's digest.
        upload_id
            The upload.
        attempts
            How many times the commit was sent.
        lost
            The last attempt that got no answer, or a 5xx.
        reached
            Whether any attempt may have reached the server. When none did (every connection
            failed while opening), the version was certainly not stored and the message says so.

        Returns
        -------
        LakanaConnectionError or ServerError
            The same kind of error as `lost`, naming the upload.
        """
        environment, qualified_name, digest = identity
        message = (_LOST_COMMIT if reached else _COMMIT_NOT_SENT).format(
            name=qualified_name,
            environment=environment,
            digest=digest[:12],
            upload=upload_id,
            attempts=attempts,
            cause=lost,
        )
        if isinstance(lost, ServerError):
            return ServerError(message, status=lost.status, error_type=lost.error_type)
        return LakanaConnectionError(message)

    def _send(
        self, path_params: Mapping[str, str], params: Mapping[str, object] | None, file: LocalFile
    ) -> None:
        """Stream one file to the server, again a few times while the server answers 503.

        Between two tries it waits the answer's `Retry-After` when that is whole seconds (at most
        10), else one second.

        Parameters
        ----------
        path_params
            The route's path parameters, including the upload.
        params
            Query parameters.
        file
            The file to send, as scanned.

        Raises
        ------
        lakana.errors.ServerError
            If the server (or a proxy before it) answered 503 for every attempt, or answered any
            other error at once. A resend is harmless: the route is addressed by hash.
        """

        def put() -> None:
            self._transport.request(
                ROUTES["artifacts.put_blob"],
                path_params={**path_params, "sha256": file.sha256},
                params=params,
                content=_file_chunks(file),
                headers={"Content-Type": "application/octet-stream"},
                timeout=_BLOB_TIMEOUT_SECONDS,
            )

        for _ in range(BUSY_ATTEMPTS - 1):
            try:
                return put()
            except ServerError as exc:
                if exc.status != 503:
                    raise
                _wait(retry_wait(exc))
        put()

    def abort_upload(self, environment: str, qualified_name: str, upload_id: str) -> None:
        """Withdraw an upload that was begun and will not be finished.

        `upload` does this by itself when it gives up after a failure, or when the server
        refused its commit; call it for an upload you started some other way, or one a killed
        process left behind (`list_uploads` shows its id). Only the
        principal that began the upload can withdraw it, and only while it is open. A task
        attempt can withdraw only the uploads begun under its own claim, and a superseded
        attempt still can: withdrawing writes nothing, so it is not guarded, by design. Files
        it already stored stay on the server until its periodic clean-up finds that nothing
        names them.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined ``"namespace.name"``.
        upload_id
            The upload's id, as the server gave it when the upload began.

        Raises
        ------
        lakana.errors.ValidationError
            If the name is not dotted, or the id is not an upload id.
        lakana.errors.PermissionDenied
            If you may not write the name.
        lakana.errors.NotFound
            If no artifact has this name.
        lakana.errors.Conflict
            If the upload does not exist, is not yours, has expired or was committed.
        """
        params = self._path_params(environment, qualified_name)
        try:
            parsed = uuid.UUID(upload_id)
        except ValueError as exc:
            raise ValidationError(_NOT_UPLOAD_ID.format(given=upload_id)) from exc
        self._transport.request(
            ROUTES["artifacts.abort_upload"], path_params={**params, "upload_id": str(parsed)}
        )

    def list_uploads(
        self,
        environment: str,
        qualified_name: str | None = None,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ArtifactUploadPage:
        """List your own open uploads, newest first, one page at a time.

        An upload that was begun and never finished, for example after a killed process, holds
        one of its artifact's 16 upload places until it expires (one hour by default). Withdraw
        one with `abort_upload`. Only uploads you began are listed (a task attempt sees its own),
        never another principal's, and an upload in an artifact you may no longer write is left
        out. The list does not say how many files an upload has sent.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Only this artifact, dot-joined ``"namespace.name"``; every artifact when `None`.
        limit
            Uploads per page, 1 to 500; the server refuses a value outside that with a 422.
        cursor
            A previous page's `next_cursor`, to fetch the page after it. Pass the same
            `qualified_name` with every page; `iter_uploads` does this for you.

        Returns
        -------
        ArtifactUploadPage
            Up to `limit` uploads, each with `upload_id`, `artifact`, `created_at` and
            `expires_at`, and the cursor of the next page.

        Raises
        ------
        lakana.errors.ValidationError
            If `qualified_name` is not ``namespace.name`` or `cursor` is not one the server
            issued.
        lakana.errors.PermissionDenied
            If you may not read the environment.
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.InvalidResponse
            If the server's answer is not what a Lakana server sends.
        """
        response = self._transport.request(
            ROUTES["artifacts.list_uploads"],
            path_params={"env": environment},
            params=query_params(name=qualified_name, limit=limit, cursor=cursor),
        )
        try:
            body = response.json()
        except ValueError:
            body = None
        return uploads_page_from_json(body)

    def iter_uploads(
        self, environment: str, qualified_name: str | None = None, *, page_size: int = 100
    ) -> Iterator[ArtifactUpload]:
        """Yield every one of your open uploads, newest first, fetching pages as needed.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Only this artifact, dot-joined ``"namespace.name"``; every artifact when `None`.
        page_size
            Uploads per request, 1 to 500.

        Yields
        ------
        ArtifactUpload
            Each open upload, once.

        Raises
        ------
        lakana.errors.PermissionDenied
            If you may not read the environment.
        lakana.errors.NotFound
            If the environment does not exist.
        """
        cursor: str | None = None
        while True:
            page = self.list_uploads(environment, qualified_name, limit=page_size, cursor=cursor)
            yield from page.items
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    def delete_version(
        self,
        environment: str,
        qualified_name: str,
        version: int,
        *,
        break_glass_reason: str | None = None,
    ) -> DeletedArtifactVersion:
        """Delete one version's files. This cannot be undone.

        The version stays in the list, marked deleted, with who deleted it and when, and no
        files; the other versions are untouched, and so is the lineage that names it. A file
        another version still has is kept. The server removes the bytes before it answers,
        unless the version is large: it removes up to a budget and its periodic clean-up
        finishes. Version numbers are never reused.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined ``"namespace.name"``.
        version
            The number of the version.
        break_glass_reason
            Delete in a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited.

        Returns
        -------
        DeletedArtifactVersion
            The version, now with `deleted_at` and `deleted_by` and no files (it is an
            `ArtifactVersion`), and what the delete did to its files: `freed_files` and
            `freed_bytes` it removed from storage, `kept_files` and `kept_bytes` that stayed,
            and `kept`, the files that stayed by reason (`other_version` with the version's
            number if you may read it, `open_upload`, `left_to_sweep`). The counts are `None`
            from a server that is older than the release that reports them.

        Raises
        ------
        lakana.errors.ValidationError
            If `version` is not a positive integer, or the name is not dotted.
        lakana.errors.PermissionDenied
            If you may not drop the name (a task token never may), or the environment is
            protected and no break-glass reason was given.
        lakana.errors.NotFound
            If the artifact or the version does not exist.
        lakana.errors.Conflict
            If the version is already deleted.
        lakana.errors.LakanaConnectionError
            If the server did not answer in time; the version may have been deleted (the
            message says how to find out). If the connection could not be opened at all, the
            message says nothing was sent.
        lakana.errors.ServerError
            If a gateway answered 502, 503 or 504, which says nothing about whether the version
            was deleted (the message says how to find out), or the server failed otherwise.
        lakana.errors.InvalidResponse
            If the server's answer is not what a Lakana server sends.
        """
        number = _require_version(version)
        params = self._path_params(environment, qualified_name)
        try:
            response = self._transport.request(
                ROUTES["artifacts.delete_version"],
                path_params={**params, "version": str(number)},
                params={"break_glass_reason": break_glass_reason}
                if break_glass_reason is not None
                else None,
                timeout=_DELETE_TIMEOUT_SECONDS,
            )
        except (LakanaConnectionError, ServerError) as exc:
            told = _lost_or_unsent(
                exc,
                _DELETE_LOST,
                _DELETE_NOT_SENT,
                version=number,
                environment=environment,
                name=qualified_name,
            )
            if told is None:
                raise
            raise told from exc
        return deleted_version_from_json(_object(response, "the deletion"))

    def drop(
        self, environment: str, qualified_name: str, *, break_glass_reason: str | None = None
    ) -> None:
        """Drop an artifact: every version, its files and its grants. This cannot be undone.

        The name becomes free and a new artifact of that name starts at version 1. The server
        removes the files inside this request, so for an artifact with thousands of files it
        can take minutes (the call waits up to ten); if the server fails part-way, its periodic
        clean-up finishes.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined ``"namespace.name"``.
        break_glass_reason
            Drop in a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited.

        Raises
        ------
        lakana.errors.ValidationError
            If the name is not dotted, or belongs to a table or a view.
        lakana.errors.PermissionDenied
            If you may not drop the name (a task token never may), or the environment is
            protected and no break-glass reason was given.
        lakana.errors.NotFound
            If no artifact has this name.
        lakana.errors.LakanaConnectionError
            If the server did not answer in time; the artifact may be dropped (the message
            says how to find out). If the connection could not be opened at all, the message
            says nothing was sent.
        lakana.errors.ServerError
            If a gateway answered 502, 503 or 504, which says nothing about whether the artifact
            was dropped (the message says how to find out), or the server failed otherwise.
        """
        try:
            self._transport.request(
                ROUTES["artifacts.drop"],
                path_params=self._path_params(environment, qualified_name),
                params={"break_glass_reason": break_glass_reason}
                if break_glass_reason is not None
                else None,
                timeout=_DROP_TIMEOUT_SECONDS,
            )
        except (LakanaConnectionError, ServerError) as exc:
            told = _lost_or_unsent(
                exc, _DROP_LOST, _DROP_NOT_SENT, environment=environment, name=qualified_name
            )
            if told is None:
                raise
            raise told from exc

    def get(self, environment: str, qualified_name: str) -> Artifact:
        """Show one artifact.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined ``"namespace.name"``.

        Returns
        -------
        Artifact
            The artifact.

        Raises
        ------
        lakana.errors.ValidationError
            If the name is not dotted, or belongs to a table or a view.
        lakana.errors.PermissionDenied
            If you may not read the name.
        lakana.errors.NotFound
            If no artifact has this name.
        lakana.errors.InvalidResponse
            If the server's answer is not what a Lakana server sends.
        """
        response = self._transport.request(
            ROUTES["artifacts.get"], path_params=self._path_params(environment, qualified_name)
        )
        return artifact_from_json(_object(response, "an artifact"))

    def versions(self, environment: str, qualified_name: str) -> builtins.list[ArtifactVersion]:
        """List an artifact's versions, newest first, deleted ones included, without files.

        Version numbers can have gaps (a number is never reused): do not assume they are
        consecutive.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined ``"namespace.name"``.

        Returns
        -------
        list of ArtifactVersion
            The versions. A task token gets no run identifiers (`task_run_id`, `run_id`,
            `project_version_id` are `None`).

        Raises
        ------
        lakana.errors.PermissionDenied
            If you may not read the name.
        lakana.errors.NotFound
            If no artifact has this name.
        lakana.errors.InvalidResponse
            If the server's answer is not what a Lakana server sends.
        """
        response = self._transport.request(
            ROUTES["artifacts.versions"],
            path_params=self._path_params(environment, qualified_name),
        )
        return [version_from_json(item) for item in _array(response, "the list of versions")]

    def version(
        self, environment: str, qualified_name: str, version: int | None = None
    ) -> ArtifactVersion:
        """Show one version with its files.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined ``"namespace.name"``.
        version
            The number; the current version when `None`.

        Returns
        -------
        ArtifactVersion
            The version. A deleted version has no files.

        Raises
        ------
        lakana.errors.ValidationError
            If `version` is not a positive integer.
        lakana.errors.PermissionDenied
            If you may not read the name.
        lakana.errors.NotFound
            If the artifact, or the version, does not exist, or the artifact has no version yet.
        lakana.errors.InvalidResponse
            If the server's answer is not what a Lakana server sends.
        """
        if version is not None:
            _require_version(version)
        response = self._transport.request(
            ROUTES["artifacts.version"],
            path_params={
                **self._path_params(environment, qualified_name),
                "version": "current" if version is None else str(version),
            },
        )
        return version_from_json(_object(response, "a version"))

    def download(
        self,
        environment: str,
        qualified_name: str,
        directory: str | os.PathLike[str],
        *,
        version: int | None = None,
        read_only: bool = False,
    ) -> ArtifactDownload:
        """Write a version's files into a new or an empty directory on this machine.

        The tree appears in `directory` only when every file has arrived and matches its size
        and SHA-256 in the manifest; a download that fails (a bad manifest, a file that does not
        match, a connection that breaks, a full disk) leaves nothing at `directory`: a directory
        that did not exist is not there, one that existed is empty again. An existing empty
        directory is never replaced: it keeps its place, its mode and whoever stands in it, and
        the files are moved into it at the end (not as one atomic step: a process killed at that
        moment leaves some files in it and the rest in a hidden ``.<name>.lakana-partial-*``
        directory inside it; empty `directory`, including that hidden one, and download again).
        Nothing is written through a link.

        Permissions: the copy is private to you, as the artifact's contents are access-controlled
        on the server and a world-readable copy on a shared machine would leak them. Files are
        0600 and directories 0700 (less your umask) and nothing is executable. To open a copy to
        others run ``chmod -R a+rX DIR``. With `read_only` files are 0400 and directories 0500
        instead, so that nothing can change them by accident; remove such a copy with
        ``chmod -R u+w DIR`` first, then ``rm -r DIR``. A `directory` that did not exist is
        sealed as a whole before it is moved into place, itself included. For an existing empty
        one, the top-level directories are sealed right after they are moved in, and `directory`
        keeps its own mode; to have the root sealed too, pass one that does not exist yet.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined ``"namespace.name"``.
        directory
            Where to write: a new directory (its parents are created), or an empty one.
        version
            The number; the current version when `None`.
        read_only
            Make the copy read-only: files 0400 and directories 0500 (the directory itself
            only if this call created it). The default is private and deletable (0600/0700).

        Returns
        -------
        ArtifactDownload
            `path` is `directory` (resolved) for a directory artifact, and
            ``<directory>/<file name>`` for a single file.

        Raises
        ------
        lakana.errors.NotFound
            If the artifact or version does not exist, or the version was deleted.
        lakana.errors.PermissionDenied
            If you may not read the name.
        lakana.errors.ValidationError
            If the server's manifest has a path this client refuses, or `directory` is a link,
            is in use, is too small for the artifact, cannot be created or written to, or the
            file system refused a write (`ManifestError`: the message names the path and the
            system's reason).
        lakana.errors.ArtifactCorrupt
            If a file does not match the manifest (it may be a transient fault).
        StoredManifestDamaged
            An `ArtifactCorrupt`: the manifest does not match its digest, the record is damaged
            on the server and downloading again cannot help.
        StoredManifestInvalid
            A `ValidationError`: the server's manifest breaks a rule this client enforces (a path
            it refuses, a size out of range); the same on every download.
        lakana.errors.LakanaConnectionError
            If the server could not be reached, or the connection ended before a file was
            complete (the message then says that a server also ends a download slower than
            64 KiB/s).
        lakana.errors.ServerError
            If the server answered 503 for every one of six tries at a file, over about 30
            seconds (busy with other downloads, or unable to serve the file: the message says
            which, with the server's own text).
        lakana.errors.InvalidResponse
            If the server's answer is not what a Lakana server sends.
        """
        found = self.version(environment, qualified_name, version)
        if found.deleted_at is not None:
            raise NotFound(
                _VERSION_DELETED.format(
                    version=found.version, name=qualified_name, environment=environment
                )
            )
        try:
            check_manifest(found.files, found.is_directory)
        except ManifestError as exc:
            raise StoredManifestInvalid(str(exc)) from exc
        if (
            manifest_digest(found.is_directory, [(f.path, f.size, f.sha256) for f in found.files])
            != found.manifest_sha256
        ):
            raise StoredManifestDamaged(
                f"The stored manifest of version {found.version} does not match its digest, so "
                "this version is damaged on the server and downloading it again will not help. "
                "Tell the operator, and store the artifact again (run the asset that produces "
                "it, or use `lakana artifact upload`)."
            )
        writer = TreeWriter.begin(
            Path(directory), sum(f.size for f in found.files), read_only=read_only
        )
        path_params = self._path_params(environment, qualified_name)
        try:
            for file in found.files:
                self._fetch(
                    writer,
                    file,
                    {**path_params, "version": str(found.version), "sha256": file.sha256},
                )
            root = writer.commit()
        except BaseException:
            writer.discard()
            raise
        return ArtifactDownload(
            path=str(root if found.is_directory else root / found.files[0].path),
            version=found.version,
            manifest_sha256=found.manifest_sha256,
            size_bytes=sum(f.size for f in found.files),
            file_count=len(found.files),
        )

    def _fetch(
        self, writer: TreeWriter, file: ArtifactFile, path_params: Mapping[str, str]
    ) -> None:
        """Download one file into the writer, again a few times while the server is busy.

        A 503 comes before the first byte of the body (the server's cap on concurrent downloads,
        or storage that could not be opened), so asking again starts the file from the
        beginning. The waits are 1, 2, 4, 8 and 16 seconds (six tries).

        Parameters
        ----------
        writer
            Where the file goes.
        file
            The manifest entry.
        path_params
            The blob route's path parameters.

        Raises
        ------
        lakana.errors.LakanaConnectionError
            If the server could not be reached, or the connection ended before the file was
            complete.
        lakana.errors.ServerError
            If the server answered 503 for every attempt (the message says whether it was busy
            with other downloads or could not serve the file, with the server's own text), or
            failed in another way.
        """
        attempt = 0
        while True:
            attempt += 1
            started = False
            try:
                with self._transport.stream(
                    ROUTES["artifacts.blob"],
                    path_params=path_params,
                    timeout=_DOWNLOAD_TIMEOUT_SECONDS,
                ) as chunks:
                    started = True
                    writer.write_file(file, chunks)
                return
            except LakanaConnectionError as exc:
                # Before the body began the transport's own text says what could not be reached;
                # after it, only that the body ended early is true and useful.
                message = (
                    _CUT.format(path=shown(file.path))
                    if started
                    else _NOT_REACHED.format(path=shown(file.path), cause=exc)
                )
                raise LakanaConnectionError(message) from exc
            except ServerError as exc:
                if exc.status != 503:
                    raise
                if attempt == _DOWNLOAD_ATTEMPTS:
                    text = _BUSY_DOWNLOAD if exc.error_type == _BUSY_TYPE else _NOT_SERVED
                    raise ServerError(
                        text.format(path=shown(file.path), attempts=attempt, cause=exc),
                        status=exc.status,
                        error_type=exc.error_type,
                    ) from exc
                _wait(_DOWNLOAD_BACKOFF_SECONDS * 2 ** (attempt - 1))

    @staticmethod
    def _path_params(environment: str, qualified_name: str) -> dict[str, str]:
        """Build the path parameters that name an artifact.

        Parameters
        ----------
        environment
            The environment name.
        qualified_name
            Dot-joined ``"namespace.name"``.

        Returns
        -------
        dict
            `env`, `path` and `name`.

        Raises
        ------
        lakana.errors.ValidationError
            If the name is not dotted.
        """
        namespace, name = _split_namespace_table(qualified_name)
        return {"env": environment, "path": namespace, "name": name}

    def list(self, environment: str, namespace: str | None = None) -> builtins.list[Artifact]:
        """List the artifacts of an environment that you may read.

        Parameters
        ----------
        environment
            The environment name.
        namespace
            Only this dot-joined namespace path; every namespace when `None`.

        Returns
        -------
        list of Artifact
            Ordered by namespace and name. An artifact you may not read is not listed.

        Raises
        ------
        lakana.errors.PermissionDenied
            If you may not read the environment.
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.InvalidResponse
            If the server's answer is not what a Lakana server sends.
        """
        params = {"namespace": namespace} if namespace is not None else None
        response = self._transport.request(
            ROUTES["artifacts.list"], path_params={"env": environment}, params=params
        )
        return [artifact_from_json(item) for item in _array(response, "the list of artifacts")]
