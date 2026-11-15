"""The artifact service: adding a version, reading versions, downloading files.

An artifact is a row of `tables` with `table_type = ARTIFACT` and versions
that are never edited. `ArtifactService` owns every operation on it. Like
`CatalogService`, every method commits internally.

Every method authorizes on the table-scope securable of the name before it
looks anything up: a caller who may not use the name gets the
authorization error a table of that name would give, and learns nothing about
whether the artifact, a version, a file or an upload exists. Every
`NotFoundError`, `ValidationError` and `ConflictError` below is raised only
after authorization has passed.

A version is added in three steps. `begin_upload` validates the manifest,
creates the artifact on first use and records an open upload; `receive_blob`
streams each missing file to storage while hashing it; `commit_upload` turns
the complete upload into the next version in one transaction. The artifact's
lock (`lock_artifact`) is taken before any read a decision depends on
and held to the end of the transaction. A file becomes visible, and gets its
`artifact_blobs` row, under that lock. The protected-write guard
runs at `begin_upload` and at `commit_upload`; at the commit its
`record_pending` runs inside the locked transaction, so a task attempt that
was reclaimed or cancelled in between is refused with no version written.

Nothing derived from a request reaches a storage path except `blob_path`
(which accepts only 64 lower-case hex digits) and `manifest_path` (which takes
a number the service chose). A request's manifest passes `parse_manifest`
before any use, and no message echoes a manifest path except through `shown`.

An artifact has at most `MAX_OPEN_UPLOADS` open uploads. `begin_upload`, under the
artifact's lock, deletes the expired ones and, for a task attempt, the unfinished ones of
its own task run with its claim or a lower one (a dead attempt's), and refuses
while the limit is reached. A blob request reads one
file's size from the stored manifest with `manifest_size`; only `begin_upload`,
`get_version` and `commit_upload` validate a whole manifest.

`abort_upload` withdraws the caller's own unfinished upload: it authorizes `WRITE_TABLE`, takes
the lock, deletes the row and leaves every file to the sweep; it calls no guard.

`delete_version` and `drop_artifact` authorize `DROP_TABLE` first and refuse any principal
with a task scope or a run context, bound or not. Pruning by `keep_versions` is part of a task's
own commit: it needs neither, and takes its number only from the bound token.

The database changes first, under the artifact's lock: the version is marked, and the
`artifact_blobs` rows of files no live version and no unexpired upload names are removed,
the set built from a stream of manifests with the transaction's own `now`. If any manifest
that could name a file cannot be read, no file is freed (the audit event says why). Bytes are
removed afterwards, one file at a time under the lock again and only while that file still
has no row, so a row never outlives its bytes; bytes that outlive their rows are garbage for
the sweep, and a storage failure is logged and never reaches the caller.

Known limits, for the documentation:

- A delete request that timed out at the client can still reach the object store after the
  artifact's lock is released.
- After a failed storage removal a deleted version's files and its `manifests/<n>.json` (file
  names and hashes) stay until the sweep or a drop removes them.
- The storage delete in flight can also outlive the lock when the server ends the database
  session (a timeout or a failover) during a slow delete.
- A release runs one short transaction and one storage delete per file inside the request, and
  stops after `RELEASE_BUDGET_FILES` files or `RELEASE_BUDGET_SECONDS` seconds. The files left,
  and the manifest object of a version whose release was cut short, stay in storage, with no
  `artifact_blobs` row and no live version, until the next sweep removes them; the deleted
  version is gone for every reader at once, because that is a database change.
- The audit event's `freed_files` counts files made eligible for removal, not objects removed;
  the delete's own answer (`DeletedVersionView`) counts the files this request removed and says
  which it left to the sweep.
- A drop leaves the empty `blobs` and `manifests` directories of a local backend; the storage
  interface has no way to remove a directory.
- A damaged manifest keeps its artifact's files in storage, with their rows, until the damaged
  version is deleted and the sweep runs.
- The audit lookup that keeps `ARTIFACT_DOWNLOADED` to one event per principal and
  version has no index of its own; it uses the existing audit indexes.
- `list_artifacts` returns every artifact the caller may read, without paging. `list_versions`
  returns every version, deleted ones included (a deleted version keeps its row for ever), in a
  fixed number of statements and without their manifests, but still without paging.
- A commit's pruning (`_prune`) loads every live version of the artifact with its manifest, to
  decide which files stay; an artifact with very many large live versions makes a commit slow.
  Bound it with `keep_versions`.
- The artifact sweep does not resume inside an artifact: each run starts an artifact's row pass
  again, so an artifact with roughly 200,000 distinct files never gets past it (see the sweep's
  known limits).
- `delete_environment` locks the environment row after its checks, a deadlock shape
  that table writes share.
- An upload can be withdrawn only by its owner and only by its id (`abort_upload`); a person
  whose process was killed without a trace of the id leaves it open, and 16 such uploads block
  an artifact until they expire. Nothing lists a person's open uploads.
- A blob row whose bytes were lost from storage is not detected until a download
  fails with "missing from storage"; `begin_upload` trusts the row.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from lakana_core.artifacts.manifest import (
    ArtifactManifest,
    ManifestFile,
    ManifestLimits,
    parse_manifest,
)
from lakana_core.artifacts.store import (
    MAX_VERSION,
    UnreadableManifest,
    artifact_prefix,
    blob_path,
    checked_hashes,
    current_version,
    declared_sizes,
    delete_quietly,
    dropped_marker_path,
    holders_of,
    lock_artifact,
    manifest_path,
    manifest_size,
    stored_hashes,
    stored_manifests,
    unreferenced_hashes,
    version_row,
    write_atomically,
)
from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, RunContext, Securable
from lakana_core.catalog.connection_lineage import connection_edges, connection_sources
from lakana_core.catalog.git_owned import refuse_owned_drop
from lakana_core.catalog.identity import resolve_environment_id, resolve_table_id
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.writes import MAX_SOURCE_NAME, MAX_SOURCE_VERSIONS
from lakana_core.config import ArtifactsConfig
from lakana_core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    StorageUnavailableError,
    ValidationError,
)
from lakana_core.grants.revocation import revoke_attached
from lakana_core.identifiers import (
    NamespacePath,
    TableRef,
    refuse_hidden_characters,
    shown,
    validate_path_component,
)
from lakana_core.log_events import LogEvent, log_event
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    ArtifactBlob,
    ArtifactUpload,
    ArtifactVersion,
    AuditEvent,
    AuditEventType,
    Environment,
    JobVersion,
    LineageEdge,
    Namespace,
    Principal,
    Run,
    Table,
    TableType,
    TaskRun,
)
from lakana_core.models.base import utcnow
from lakana_core.runs.lockdown import (
    SUPERSEDED_MESSAGE,
    ProtectedWriteGuard,
    WriteOperation,
    table_target,
)
from lakana_core.storage.base import Storage, StreamWriter
from lakana_core.storage.resolver import StorageResolver

logger = logging.getLogger(__name__)

DELETE_FROM_RUN_MESSAGE = (
    "A task token cannot delete a version or drop an artifact, and neither can a running task "
    "or query. Do it as a person or a service account with DROP_TABLE on the artifact "
    "(`lakana artifact versions delete`, `lakana artifact drop`), or set keep_versions on the "
    "asset to prune old versions when a new one is committed."
)
KEPT_DAMAGED_OWN = (
    "The stored manifest of version {version} is damaged, so none of its files were removed "
    "from storage. The sweep removes files that no version needs."
)
KEPT_DAMAGED_OTHER = (
    "A stored manifest of a live version or open upload of this artifact is damaged, so no "
    "file was removed from storage. Delete the damaged version (`lakana artifact versions` "
    "lists them) or let the damaged upload expire; the sweep then removes files no version "
    "needs."
)
KEPT_UNKNOWN_MANIFESTS = (
    "Storage holds manifest objects of versions the catalog does not know (a commit that "
    "failed part-way, or a database restored from a backup), so no file was removed from "
    "storage in this request. The sweep removes the files that no version, known or not, needs."
)
DROP_NOT_WRITTEN = (
    "{name} was not dropped because its storage could not be written, and nothing was "
    "deleted. Try again when storage is available."
)
COMMIT_NOT_WRITTEN = (
    "This version was not committed because its storage could not be written. The upload is "
    "still open: commit it again when storage is available."
)
VERSION_LIMIT_REACHED = (
    "{name} has reached the highest version number ({limit}); store further versions under a "
    "new artifact name."
)
STORAGE_NOT_WRITTEN = (
    "The storage could not be written, so this file was not stored. Try again in a moment; if "
    "it keeps failing, tell the operator that the environment's storage may be full or "
    "unreachable."
)
STORAGE_NOT_READ = (
    "The storage could not be read, so this file was not sent. Try again in a moment; if it "
    "keeps failing, tell the operator that the environment's storage may be unreachable."
)
UPLOAD_GONE = "This upload has expired or does not exist; start the upload again."
FILES_NOT_STORED = (
    "{count} of this upload's files are not stored yet; upload them and commit again."
)
"""The commit's refusal when files of the upload are missing. The SDK restarts the upload on the
words ``files are not stored yet`` (pinned in `lakana-worker`'s tests); keep them."""
VERSION_DELETED_MESSAGE = (
    "Version {version} of {name} was deleted; its bytes are gone. Read a version that exists: "
    "`lakana artifact versions list {name} --env {environment}` shows which."
)
NO_VERSION_YET = "{name} has no version yet; run the asset that produces it."
_KIND_WORDS = {"TABLE": "a table", "VIEW": "a view", "EXTERNAL": "an external table"}
MAX_OPEN_UPLOADS = 16
"""Open uploads one artifact may have at once."""
RELEASE_BUDGET_FILES = 200
"""Files a request removes from storage after deleting versions; the sweep does the rest."""
RELEASE_BUDGET_SECONDS = 5.0
"""Seconds a request spends removing files from storage, well inside a client's 30 s timeout."""
"""Largest version number: the column is a 32-bit integer."""
"""A namespace path (1024) and a table name (255) joined by a dot."""
_BAD_SOURCE = (
    "A source of this version is not a table or artifact name or a version number; name each "
    "source as 'namespace.name' and a version as a whole number from 1 to 2147483647."
)


class _ListingFailed(Exception):
    """Storage could not list an artifact's manifest objects; carries no storage text."""


def _is_version(value: object) -> bool:
    """Say whether a request's value can be the number of a version.

    Parameters
    ----------
    value
        What a request gave; of any type.

    Returns
    -------
    bool
        Whether it is an `int` (not a `bool`) from 1 to 2**31 - 1.
    """
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_VERSION


@dataclass(frozen=True)
class ArtifactSource:
    """What a new version was made from.

    Parameters
    ----------
    name
        The source's dotted name: a table, a view or another artifact.
    version
        The version read, for an artifact; `None` records the current one.
    """

    name: str
    version: int | None = None


@dataclass(frozen=True)
class ArtifactView:
    """One artifact as a listing shows it.

    Parameters
    ----------
    ref
        Its name.
    id
        Its id.
    current_version
        The highest live version; `None` when it has none.
    versions
        How many versions it has had, deleted ones included.
    size_bytes
        The summed size of its live versions.
    created_at
        When the artifact was created.
    """

    ref: TableRef
    id: uuid.UUID
    current_version: int | None
    versions: int
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True)
class VersionView:
    """One version of an artifact.

    Parameters
    ----------
    version
        The number, from 1, never reused.
    manifest_sha256
        The digest of the canonical manifest.
    size_bytes
        The summed size of its files.
    file_count
        The number of its files.
    is_directory
        Whether it is a directory (otherwise one file).
    task_run_id
        The task run that committed it, if one did.
    run_id
        That task run's run.
    job_version_id
        The project version the run executed.
    commit_sha
        The commit of that project version.
    created_by
        The name of the principal that committed it.
    created_at
        When it was committed.
    deleted_at
        When it was deleted; `None` while it is live.
    deleted_by
        The name of the principal that deleted it.
    files
        Its files; empty in a listing and for a deleted version.
    """

    version: int
    manifest_sha256: str
    size_bytes: int
    file_count: int
    is_directory: bool
    task_run_id: uuid.UUID | None
    run_id: uuid.UUID | None
    job_version_id: uuid.UUID | None
    commit_sha: str | None
    created_by: str | None
    created_at: datetime
    deleted_at: datetime | None
    deleted_by: str | None
    files: tuple[ManifestFile, ...]


class KeptReason(StrEnum):
    """Why a delete left a file in storage; a closed set, the same words in every client."""

    OTHER_VERSION = "other_version"
    """A live version of the artifact also has the file."""
    OPEN_UPLOAD = "open_upload"
    """An unexpired, uncommitted upload of the artifact names the file."""
    LEFT_TO_SWEEP = "left_to_sweep"
    """This request did not remove the file and the periodic clean-up decides: the release
    budget was spent, a storage delete failed, a manifest could not be read, or storage holds
    manifests the catalog does not know."""


@dataclass(frozen=True)
class KeptFiles:
    """Files a delete left in storage, for one reason.

    Parameters
    ----------
    reason
        Why they stayed.
    files
        How many stored files (identical files of one version count once).
    bytes
        Their summed size.
    version
        For `OTHER_VERSION` when the caller may read the artifact: the newest live version that
        has them. `None` otherwise, so a caller who may delete but not read learns no number.
    """

    reason: KeptReason
    files: int
    bytes: int
    version: int | None = None


@dataclass(frozen=True)
class DeletedVersionView(VersionView):
    """A deleted version and what the delete did to its files.

    Parameters
    ----------
    freed_files
        Stored files this request removed from storage.
    freed_bytes
        Their summed size.
    kept
        The files that stayed, grouped by reason; empty when none did. Ordered: other versions
        newest first, then open uploads, then files left to the sweep.
    """

    freed_files: int = 0
    freed_bytes: int = 0
    kept: tuple[KeptFiles, ...] = ()


@dataclass(frozen=True)
class UploadView:
    """An open upload.

    Parameters
    ----------
    upload_id
        The upload's id.
    missing
        The hashes of the files the store does not have yet, sorted.
    expires_at
        When the upload stops being usable.
    """

    upload_id: uuid.UUID
    missing: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True)
class OpenUploadView:
    """An open upload as the person who began it sees it in a list.

    Parameters
    ----------
    upload_id
        The upload's id, for `abort_upload`.
    ref
        The artifact it adds a version to.
    created_at
        When it was begun.
    expires_at
        When it stops being usable and frees its place.
    """

    upload_id: uuid.UUID
    ref: TableRef
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class UploadCursor:
    """A position in `list_uploads`' newest-first order, for keyset paging.

    Parameters
    ----------
    created_at
        The `created_at` of the last upload already seen.
    id
        Its id, breaking ties between uploads begun in the same instant.
    """

    created_at: datetime
    id: uuid.UUID


@dataclass(frozen=True)
class OpenUploadPage:
    """One page of a caller's open uploads.

    Parameters
    ----------
    items
        The uploads, newest first.
    next
        Pass as `after` to get the page that follows; `None` on the last page.
    """

    items: tuple[OpenUploadView, ...]
    next: UploadCursor | None


@dataclass(frozen=True)
class CommitResult:
    """A committed version.

    Parameters
    ----------
    version
        The new version, with its files.
    deleted_versions
        The numbers of the versions the commit pruned (`keep_versions`).
    """

    version: VersionView
    deleted_versions: tuple[int, ...]


@dataclass(frozen=True)
class BlobDownload:
    """A file being downloaded.

    Parameters
    ----------
    size
        Its size as the version's manifest declares it.
    chunks
        Its contents, piece by piece.
    """

    size: int
    chunks: Iterator[bytes]


class BlobUpload:
    """One file being received. `write` pieces, then `finish`; `abort` on any failure.

    The bytes go to storage as they arrive and are hashed on the way. The
    object becomes visible, and gets its `artifact_blobs` row, only in
    `finish`, under the artifact's lock.

    Parameters
    ----------
    session
        The service's session; `finish` runs its own transaction on it.
    writer
        The open stream to the file's storage path.
    artifact_id
        The artifact.
    upload_id
        The upload the file belongs to.
    sha256
        The hash the request named.
    size
        The size the upload's manifest declares for that hash.
    clock
        The service's clock.
    expires_at
        When the upload stops being usable; bytes that arrive later are wasted, `finish` refuses
        them.
    """

    def __init__(
        self,
        session: Session,
        writer: StreamWriter,
        artifact_id: uuid.UUID,
        upload_id: uuid.UUID,
        sha256: str,
        size: int,
        clock: Callable[[], datetime],
        expires_at: datetime,
    ) -> None:
        self._session = session
        self._writer: StreamWriter | None = writer
        self._artifact_id = artifact_id
        self._upload_id = upload_id
        self._sha256 = sha256
        self._size = size
        self._clock = clock
        self._expires_at = expires_at
        self._hash = hashlib.sha256()
        self._received = 0

    def seconds_left(self) -> float:
        """Say how long the upload this file belongs to can still be used.

        Returns
        -------
        float
            Seconds until it expires; zero or negative once it has. A caller that reads the
            file's body from a client stops reading then, instead of holding a connection
            for bytes `finish` would refuse.
        """
        return (self._expires_at - self._clock()).total_seconds()

    def write(self, data: bytes) -> None:
        """Take the next piece.

        Parameters
        ----------
        data
            The bytes.

        Raises
        ------
        ValidationError
            As soon as more bytes arrived than the manifest declares; the
            stream is aborted and nothing is stored.
        StorageUnavailableError
            If storage could not take the bytes (the upload is aborted).
        ValueError
            If the upload was already finished or aborted.
        """
        if self._writer is None:
            raise ValueError("the upload is already closed")
        self._received += len(data)
        if self._received > self._size:
            self.abort()
            raise ValidationError(
                f"The file is longer than the {self._size} bytes its manifest declares; "
                "nothing was stored."
            )
        self._hash.update(data)
        try:
            self._writer.write(data)
        except Exception as exc:
            logger.warning(
                "Writing a file of an artifact upload failed (%s).",
                type(exc).__name__,
                extra=log_event(LogEvent.STORAGE_UNREACHABLE),
            )
            with contextlib.suppress(Exception):
                self.abort()
            raise StorageUnavailableError(STORAGE_NOT_WRITTEN) from None

    def finish(self) -> bool:
        """Verify the file and make it part of the artifact's store.

        Returns
        -------
        bool
            `True` if this call stored the file; `False` if the artifact
            already had it (the written bytes are thrown away).

        Raises
        ------
        ValidationError
            If the size or the hash differs from what was declared.
        ConflictError
            If the upload expired, was committed or the artifact was dropped meanwhile.
        StorageUnavailableError
            If storage could not complete the file.
        ValueError
            If the upload was already finished or aborted.
        """
        writer, self._writer = self._writer, None
        if writer is None:
            raise ValueError("the upload is already closed")
        if self._received != self._size or self._hash.hexdigest() != self._sha256:
            writer.abort()
            raise ValidationError(
                "The file's size or SHA-256 differs from what its manifest declares; nothing "
                "was stored. Send the file's bytes exactly, or start the upload again if the "
                "file changed."
            )
        session = self._session
        try:
            session.rollback()
            if not lock_artifact(session, self._artifact_id):
                raise ConflictError(UPLOAD_GONE)
            upload = session.execute(
                select(ArtifactUpload)
                .where(ArtifactUpload.id == self._upload_id)
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if (
                upload is None
                or upload.artifact_id != self._artifact_id
                or upload.expires_at <= self._clock()
                or upload.committed_version is not None
            ):
                raise ConflictError(UPLOAD_GONE)
            if stored_hashes(session, self._artifact_id, [self._sha256]):
                writer.abort()
                session.rollback()
                return False
            try:
                writer.commit()
            except FileNotFoundError:
                # The temporary file was removed under the upload (a sweep that judged it
                # abandoned, or an operator): the upload cannot be completed.
                raise ConflictError(UPLOAD_GONE) from None
            except Exception as exc:
                logger.warning(
                    "Completing a file of an artifact upload failed (%s).",
                    type(exc).__name__,
                    extra=log_event(LogEvent.STORAGE_UNREACHABLE),
                )
                raise StorageUnavailableError(STORAGE_NOT_WRITTEN) from None
            session.add(
                ArtifactBlob(
                    artifact_id=self._artifact_id,
                    sha256=self._sha256,
                    size_bytes=self._size,
                    created_at=self._clock(),
                )
            )
            session.commit()
        except BaseException:
            writer.abort()
            session.rollback()
            raise
        return True

    def abort(self) -> None:
        """Throw away what was received. A no-op once finished or aborted."""
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.abort()


def _recorded_files(row: ArtifactVersion) -> tuple[int, int]:
    """Count the files of a version whose stored manifest is damaged, best effort.

    Every other group of a delete's report counts distinct files, so a file listed under two
    paths counts once here too. An entry whose hash cannot be read cannot be matched with any
    other and counts as one file of its own (a size it does not declare, or declares below 0,
    counts as zero bytes, so the bytes are never negative).

    Parameters
    ----------
    row
        The version; its `manifest_json` failed `checked_hashes`.

    Returns
    -------
    tuple of int
        The number of files and their bytes; the counts recorded when the version was stored
        (paths, not distinct files) when the document has no list of files at all.
    """
    entries = row.manifest_json.get("files") if isinstance(row.manifest_json, dict) else None
    if not isinstance(entries, list) or not entries:
        return row.file_count, row.size_bytes
    sizes: dict[str, int] = {}
    for position, entry in enumerate(entries):
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        size = entry.get("size") if isinstance(entry, dict) else None
        key = digest if isinstance(digest, str) else f"#{position}"
        sizes[key] = max(size, 0) if isinstance(size, int) and not isinstance(size, bool) else 0
    return len(sizes), sum(sizes.values())


class ArtifactService:
    """Upload, commit, read and download artifact versions.

    Parameters
    ----------
    session
        Database session; every method commits on it.
    storage
        Resolves an environment name to its storage.
    authz
        Authorization.
    config
        Limits and the upload lifetime; the defaults when `None`.
    clock
        The time source; the real one when `None`.
    monotonic
        The clock that times the release of a deletion's files; `time.monotonic` when `None`.
    """

    def __init__(
        self,
        session: Session,
        storage: StorageResolver,
        authz: AuthorizationService,
        config: ArtifactsConfig | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._session = session
        self._storage = storage
        self._authz = authz
        self._config = config or ArtifactsConfig()
        self._limits = ManifestLimits.from_config(self._config)
        self._clock = clock or utcnow
        self._monotonic = monotonic or time.monotonic
        self._guard = ProtectedWriteGuard(session, authz)

    @staticmethod
    def _securable(ref: TableRef) -> Securable:
        """Name the object authorization is asked about.

        Parameters
        ----------
        ref
            The artifact.

        Returns
        -------
        Securable
            The table-scope securable of its name.
        """
        return Securable.table_scope(ref.environment, ref.namespace, ref.name)

    @staticmethod
    def _not_found(ref: TableRef) -> NotFoundError:
        """Word the absence of an artifact.

        Parameters
        ----------
        ref
            The artifact.

        Returns
        -------
        NotFoundError
            The error, to raise.
        """
        return NotFoundError(f"Artifact {ref.dotted()!r} not found in {ref.environment!r}.")

    @staticmethod
    def _no_version(ref: TableRef, version: object) -> NotFoundError:
        """Word the absence of a version, without formatting an unbounded number.

        Parameters
        ----------
        ref
            The artifact.
        version
            What a caller gave as the number; of any type.

        Returns
        -------
        NotFoundError
            The error, to raise.
        """
        if _is_version(version):
            return NotFoundError(f"{ref.dotted()} has no version {version}.")
        return NotFoundError(
            f"{ref.dotted()} has no version with that number; versions count up from 1."
        )

    def _row(self, ref: TableRef) -> Table | None:
        """Find the row of a name, whatever its type.

        Parameters
        ----------
        ref
            The name.

        Returns
        -------
        Table or None
            The row, or `None` when the name is free.
        """
        table_id = resolve_table_id(
            self._session, ref.environment, ref.namespace.joined(), ref.name
        )
        return None if table_id is None else self._session.get(Table, table_id)

    def _artifact(self, ref: TableRef) -> Table:
        """Find an artifact's row. Call only after authorizing on the name.

        Parameters
        ----------
        ref
            The artifact.

        Returns
        -------
        Table
            Its row.

        Raises
        ------
        NotFoundError
            If no row has this name.
        ValidationError
            If the name belongs to a table, a view or an external table.
        """
        row = self._row(ref)
        if row is None:
            raise self._not_found(ref)
        if row.table_type is not TableType.ARTIFACT:
            raise ValidationError(
                f"{ref.dotted()} is {_KIND_WORDS[row.kind]}, not an artifact. "
                f"See `lakana table get {ref.dotted()}`."
            )
        return row

    def _stored_manifest(self, ref: TableRef, row: ArtifactVersion) -> ArtifactManifest:
        """Read a version's stored manifest.

        Parameters
        ----------
        ref
            The artifact.
        row
            The version.

        Returns
        -------
        ArtifactManifest
            The validated manifest.

        Raises
        ------
        ValidationError
            If the stored document does not pass `parse_manifest`.
        """
        try:
            return parse_manifest(row.manifest_json)
        except ValidationError:
            raise ValidationError(
                f"The stored manifest of version {row.version} of {ref.dotted()} is damaged; "
                "delete the version and run the asset again."
            ) from None

    def _principal_name(self, principal_id: uuid.UUID | None) -> str | None:
        """Look a principal's name up.

        Parameters
        ----------
        principal_id
            The principal; `None` once its row was deleted.

        Returns
        -------
        str or None
            The name, or `None`.
        """
        if principal_id is None:
            return None
        principal = self._session.get(Principal, principal_id)
        return None if principal is None else principal.name

    def _version_view(
        self,
        ref: TableRef,
        row: ArtifactVersion,
        *,
        with_files: bool,
        principal_names: Mapping[uuid.UUID, str] | None = None,
        run_ids: Mapping[uuid.UUID, uuid.UUID] | None = None,
    ) -> VersionView:
        """Present a version.

        Parameters
        ----------
        ref
            The artifact.
        row
            The version.
        with_files
            Include its files (never for a deleted version).
        principal_names
            The names of the principals the listing resolved in one statement; `None` looks
            each one up, as for a single version. An id missing from it shows as `None`.
        run_ids
            The run of each task run, resolved the same way.

        Returns
        -------
        VersionView
            The view.
        """
        files: tuple[ManifestFile, ...] = ()
        if with_files and row.deleted_at is None:
            files = self._stored_manifest(ref, row).files
        run_id = None
        if row.task_run_id is not None:
            if run_ids is not None:
                run_id = run_ids.get(row.task_run_id)
            else:
                task = self._session.get(TaskRun, row.task_run_id)
                run_id = None if task is None else task.run_id
        if principal_names is None:
            created_by = self._principal_name(row.created_by)
            deleted_by = self._principal_name(row.deleted_by)
        else:
            created_by = None if row.created_by is None else principal_names.get(row.created_by)
            deleted_by = None if row.deleted_by is None else principal_names.get(row.deleted_by)
        return VersionView(
            version=row.version,
            manifest_sha256=row.manifest_sha256,
            size_bytes=row.size_bytes,
            file_count=row.file_count,
            is_directory=row.is_directory,
            task_run_id=row.task_run_id,
            run_id=run_id,
            job_version_id=row.job_version_id,
            commit_sha=row.commit_sha,
            created_by=created_by,
            created_at=row.created_at,
            deleted_at=row.deleted_at,
            deleted_by=deleted_by,
            files=files,
        )

    def _view(self, ref: TableRef, row: Table) -> ArtifactView:
        """Present an artifact.

        Parameters
        ----------
        ref
            Its name.
        row
            Its row.

        Returns
        -------
        ArtifactView
            The view.
        """
        count, size = self._session.execute(
            select(
                func.count(ArtifactVersion.id),
                func.coalesce(
                    func.sum(
                        case(
                            (ArtifactVersion.deleted_at.is_(None), ArtifactVersion.size_bytes),
                            else_=0,
                        )
                    ),
                    0,
                ),
            ).where(ArtifactVersion.artifact_id == row.id)
        ).one()
        return ArtifactView(
            ref=ref,
            id=row.id,
            current_version=current_version(self._session, row.id),
            versions=int(count),
            size_bytes=int(size),
            created_at=row.created_at,
        )

    def _open_upload(
        self,
        p: PrincipalRef,
        artifact_id: uuid.UUID,
        upload_id: uuid.UUID,
        *,
        committed: bool = False,
    ) -> ArtifactUpload:
        """Find an upload the caller may use.

        Parameters
        ----------
        p
            The caller.
        artifact_id
            The artifact the request names.
        upload_id
            The upload the request names; of any type.
        committed
            Accept an upload that was committed already (a repeated commit returns its
            version); every other caller wants an upload that can still take files.

        Returns
        -------
        ArtifactUpload
            The row, read fresh.

        Raises
        ------
        ConflictError
            With one text for every reason: no such upload, another artifact's,
            another principal's, another task attempt's, an expired one, or a committed one
            when `committed` is false.
        """
        if not isinstance(upload_id, uuid.UUID):
            raise ConflictError(UPLOAD_GONE)
        upload = self._session.execute(
            select(ArtifactUpload)
            .where(ArtifactUpload.id == upload_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        context = p.run_context
        attempt = (None, None) if context is None else (context.task_run_id, context.claim)
        if (
            upload is None
            or upload.artifact_id != artifact_id
            or upload.principal_id != p.id
            or upload.expires_at <= self._clock()
            or (upload.task_run_id, upload.claim) != attempt
            or (upload.committed_version is not None and not committed)
        ):
            raise ConflictError(UPLOAD_GONE)
        return upload

    def list_artifacts(
        self, p: PrincipalRef, environment: str, namespace: NamespacePath | None = None
    ) -> list[ArtifactView]:
        """List the artifacts of an environment that the caller may read.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment.
        namespace
            Only this namespace; every namespace when `None`.

        Returns
        -------
        list of ArtifactView
            Ordered by namespace and name. An artifact the caller may not read
            is left out, as if it did not exist.

        Raises
        ------
        PermissionDenied
            If the caller may not read the environment.
        NotFoundError
            If the environment does not exist.
        """
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment))
        environment_id = resolve_environment_id(self._session, environment)
        if environment_id is None:
            raise NotFoundError(f"No environment named {environment!r}.")
        query = (
            select(Table, Namespace.path)
            .join(Namespace, Table.namespace_id == Namespace.id)
            .where(Namespace.environment_id == environment_id)
            .where(Table.table_type == TableType.ARTIFACT)
            .order_by(Namespace.path, Table.name)
        )
        if namespace is not None:
            query = query.where(Namespace.path == namespace.joined())
        listed: list[ArtifactView] = []
        for row, path in self._session.execute(query).all():
            ref = TableRef(environment, NamespacePath.parse(path), row.name)
            if self._authz.permits(p, Action.READ_TABLE, self._securable(ref)):
                listed.append(self._view(ref, row))
        return listed

    def get_artifact(self, p: PrincipalRef, ref: TableRef) -> ArtifactView:
        """Show one artifact.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The artifact.

        Returns
        -------
        ArtifactView
            The artifact.

        Raises
        ------
        PermissionDenied
            If the caller may not read the name.
        NotFoundError
            If no artifact has this name.
        ValidationError
            If the name belongs to something that is not an artifact.
        """
        self._authz.authorize(p, Action.READ_TABLE, self._securable(ref))
        return self._view(ref, self._artifact(ref))

    def list_versions(self, p: PrincipalRef, ref: TableRef) -> list[VersionView]:
        """List an artifact's versions, newest first, deleted ones included.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The artifact.

        Returns
        -------
        list of VersionView
            The versions, without their files. A fixed number of statements however many
            there are, and no manifest is loaded: deleted versions keep their row for ever,
            so the listing grows with every version ever stored.

        Raises
        ------
        PermissionDenied
            If the caller may not read the name.
        NotFoundError
            If no artifact has this name.
        ValidationError
            If the name belongs to something that is not an artifact.
        """
        self._authz.authorize(p, Action.READ_TABLE, self._securable(ref))
        row = self._artifact(ref)
        mine = ArtifactVersion.artifact_id == row.id
        rows = self._session.scalars(
            select(ArtifactVersion)
            .options(defer(ArtifactVersion.manifest_json))
            .where(mine)
            .order_by(ArtifactVersion.version.desc())
        ).all()
        names = {
            principal_id: name
            for principal_id, name in self._session.execute(
                select(Principal.id, Principal.name).where(
                    Principal.id.in_(select(ArtifactVersion.created_by).where(mine))
                    | Principal.id.in_(select(ArtifactVersion.deleted_by).where(mine))
                )
            )
        }
        run_ids = {
            task_run_id: run_id
            for task_run_id, run_id in self._session.execute(
                select(TaskRun.id, TaskRun.run_id)
                .join(ArtifactVersion, ArtifactVersion.task_run_id == TaskRun.id)
                .where(mine)
            )
        }
        return [
            self._version_view(
                ref, version, with_files=False, principal_names=names, run_ids=run_ids
            )
            for version in rows
        ]

    def get_version(
        self, p: PrincipalRef, ref: TableRef, version: int | None = None
    ) -> VersionView:
        """Show one version with its files.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The artifact.
        version
            The number; the current version when `None`.

        Returns
        -------
        VersionView
            The version. A deleted one has no files.

        Raises
        ------
        PermissionDenied
            If the caller may not read the name.
        NotFoundError
            If the artifact, or the version, does not exist, or the artifact
            has no version yet.
        ValidationError
            If the name belongs to something that is not an artifact, or the
            stored manifest is damaged.
        """
        self._authz.authorize(p, Action.READ_TABLE, self._securable(ref))
        row = self._artifact(ref)
        number = current_version(self._session, row.id) if version is None else version
        if number is None:
            raise NotFoundError(NO_VERSION_YET.format(name=ref.dotted()))
        found = version_row(self._session, row.id, number) if _is_version(number) else None
        if found is None:
            raise self._no_version(ref, number)
        return self._version_view(ref, found, with_files=True)

    @staticmethod
    def _require_attempt(p: PrincipalRef) -> None:
        """Refuse a principal with a task token's scope and no task attempt.

        Authentication never produces one; this guards against a caller that does, so
        that the guard (which only checks a run context it is given) cannot be skipped.

        Parameters
        ----------
        p
            The acting principal.

        Raises
        ------
        PermissionDenied
            With `SUPERSEDED_MESSAGE` if `p` has a bound task scope and no run context.
        """
        if p.task_scope is not None and p.task_scope.bound is not None and p.run_context is None:
            raise PermissionDenied(SUPERSEDED_MESSAGE)

    def _create(self, p: PrincipalRef, ref: TableRef) -> Table:
        """Create the artifact's row, in the caller's transaction.

        The name space's row is locked first (`FOR SHARE` on Postgres), so a
        concurrent `drop_namespace`, which takes the same row `FOR UPDATE`
        before it checks that the name space is empty, either finishes first
        (this call then finds no name space) or waits until this transaction
        commits (and then sees the artifact).

        Parameters
        ----------
        p
            The creator, who becomes the owner.
        ref
            The artifact's name.

        Returns
        -------
        Table
            The new row, flushed.

        Raises
        ------
        ValidationError
            If the name is not a valid artifact name, or holds an invisible or line-breaking
            character (`refuse_hidden_characters`).
        NotFoundError
            If the name space does not exist.
        ConflictError
            If another request created the name at the same moment.
        """
        validate_path_component(ref.name, what="Artifact name")
        # Listed among the namespace's tables: a name that looks like another is refused (#593).
        # A `%` stays allowed: an artifact holds no table directory.
        refuse_hidden_characters(ref.name, what="Artifact name")
        namespace_id = self._session.execute(
            select(Namespace.id)
            .join(Environment, Namespace.environment_id == Environment.id)
            .where(
                Environment.name == ref.environment,
                LIVE_ENVIRONMENT,
                Namespace.path == ref.namespace.joined(),
            )
            .with_for_update(read=True, of=Namespace)
        ).scalar_one_or_none()
        if namespace_id is None:
            raise NotFoundError(f"Namespace {ref.namespace} not found in {ref.environment!r}.")
        row = Table(
            id=uuid.uuid4(),
            namespace_id=namespace_id,
            name=ref.name,
            table_type=TableType.ARTIFACT,
            owner_id=p.id,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            raise ConflictError(
                f"{ref.dotted()} was created by someone else just now; retry."
            ) from None
        AuditService(self._session).record(
            p.id,
            AuditEventType.ARTIFACT_CREATED,
            securable=self._securable(ref),
            detail={"artifact": ref.dotted(), "artifact_id": str(row.id), "principal": p.name},
        )
        return row

    def check_can_upload(self, p: PrincipalRef, ref: TableRef) -> None:
        """Refuse a caller who may not begin an upload, before its manifest is read.

        The route runs this before it reads a request body of up to 16 MiB: authorization first
        (write, and create when the name is new), then the protected-write guard and the kind
        of the name, exactly as `begin_upload` does. Writes nothing and records no break-glass
        event; `begin_upload` does that.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The artifact.

        Raises
        ------
        PermissionDenied
            If the caller may not write (or create) the name, or the guard refuses.
        ValidationError
            If the name belongs to a table, a view or an external table.
        """
        securable = self._securable(ref)
        self._authz.authorize(p, Action.WRITE_TABLE, securable)
        self._require_attempt(p)
        row = self._row(ref)
        if row is None:
            self._authz.authorize(p, Action.CREATE_TABLE, securable)
        self._guard.check(
            p,
            ref.environment,
            WriteOperation.ARTIFACT_VERSION,
            table_target(ref),
            record_break_glass=False,
        )
        if row is not None and row.table_type is not TableType.ARTIFACT:
            raise ValidationError(
                f"{ref.dotted()} is {_KIND_WORDS[row.kind]}, not an artifact; drop it first, "
                "or choose another name."
            )
        self._session.rollback()

    def begin_upload(self, p: PrincipalRef, ref: TableRef, document: object) -> UploadView:
        """Start adding a version: validate the manifest, create the artifact on first use.

        Parameters
        ----------
        p
            The acting principal; a bound task token must belong to an asset
            that produces an artifact.
        ref
            The artifact.
        document
            The manifest as a request sent it; untrusted.

        Returns
        -------
        UploadView
            The upload and the hashes of the files still to send.

        Raises
        ------
        PermissionDenied
            If the caller may not write the name (or create it, when it is
            new), if the token's task produces a table, or if the guard
            refuses the write (protected environment, superseded attempt).
        ValidationError
            If the manifest is invalid or over a limit, the name is not a valid
            artifact name, the name belongs to a table, a view or an external
            table, or a file of the manifest is stored with another size.
        NotFoundError
            If the name space does not exist.
        ConflictError
            If another request created the name at the same moment, or the
            artifact already has `MAX_OPEN_UPLOADS` open uploads.
        """
        securable = self._securable(ref)
        self._authz.authorize(p, Action.WRITE_TABLE, securable)
        self._require_attempt(p)
        row = self._row(ref)
        if row is None:
            self._authz.authorize(p, Action.CREATE_TABLE, securable)
        # Right after authorization, so a token of a task that produces a table is refused
        # (and audited) by the guard, and nothing before it reveals or does anything.
        self._guard.check(p, ref.environment, WriteOperation.ARTIFACT_VERSION, table_target(ref))
        if row is not None and row.table_type is not TableType.ARTIFACT:
            raise ValidationError(
                f"{ref.dotted()} is {_KIND_WORDS[row.kind]}, not an artifact; drop it first, "
                "or choose another name."
            )
        manifest = parse_manifest(document, self._limits)
        try:
            if row is None:
                row = self._create(p, ref)
            if not lock_artifact(self._session, row.id):
                raise self._not_found(ref)
            wanted = manifest.hashes()
            stored = stored_hashes(self._session, row.id, wanted)
            for file in manifest.files:
                have = stored.get(file.sha256)
                if have is not None and have != file.size:
                    raise ValidationError(
                        f"{shown(file.path)}: a file with this SHA-256 is already stored with "
                        f"{have} bytes, not {file.size}; the manifest is wrong."
                    )
            now = self._clock()
            context = p.run_context
            self._make_room(ref, row.id, context, now)
            upload = ArtifactUpload(
                id=uuid.uuid4(),
                artifact_id=row.id,
                principal_id=p.id,
                manifest_sha256=manifest.sha256(),
                manifest_json=manifest.to_json(),
                task_run_id=None if context is None else context.task_run_id,
                claim=None if context is None else context.claim,
                created_at=now,
                expires_at=now + timedelta(seconds=self._config.upload_ttl_seconds),
            )
            self._session.add(upload)
            view = UploadView(
                upload_id=upload.id,
                missing=tuple(sorted(digest for digest in wanted if digest not in stored)),
                expires_at=upload.expires_at,
            )
            self._guard.record_pending()
            self._session.commit()
        except BaseException:
            self._session.rollback()
            raise
        return view

    def _make_room(
        self, ref: TableRef, artifact_id: uuid.UUID, context: RunContext | None, now: datetime
    ) -> None:
        """Bound the artifact's open uploads, under its lock, in the caller's transaction.

        Deletes the artifact's expired uploads and, for a task attempt, the uncommitted
        uploads of the same task run with the same claim (a retry within an attempt replaces
        its own upload) or a lower one (the attempt that began it is dead, its
        claim is superseded, and it can never commit, but it would hold a slot for the
        upload lifetime). Then it refuses while `MAX_OPEN_UPLOADS` are still open. A caller
        that is not a task attempt deletes no task's upload, and no other task run's,
        person's or committed upload is touched. An upload that was committed is not open
        and does not count; it stays until it expires.

        Parameters
        ----------
        ref
            The artifact.
        artifact_id
            Its id.
        context
            The caller's run context, if it has one.
        now
            The time an upload's expiry is compared with.

        Raises
        ------
        ConflictError
            If the artifact has `MAX_OPEN_UPLOADS` open uploads.
        """
        mine = ArtifactUpload.artifact_id == artifact_id
        still_open = ArtifactUpload.committed_version.is_(None)
        self._session.execute(delete(ArtifactUpload).where(mine, ArtifactUpload.expires_at <= now))
        if context is not None and context.claim is not None:
            self._session.execute(
                delete(ArtifactUpload).where(
                    mine,
                    still_open,
                    ArtifactUpload.task_run_id == context.task_run_id,
                    ArtifactUpload.claim <= context.claim,
                )
            )
        open_uploads = self._session.execute(
            select(func.count(ArtifactUpload.id)).where(mine, still_open)
        ).scalar_one()
        if open_uploads >= MAX_OPEN_UPLOADS:
            raise ConflictError(
                f"{ref.dotted()} has {MAX_OPEN_UPLOADS} uploads in progress, the most allowed "
                "at once. Commit one, withdraw one you began (`lakana artifact uploads list "
                f"{ref.dotted()}` shows yours, `lakana artifact uploads abort {ref.dotted()} "
                "UPLOAD_ID` withdraws one), or wait for one to expire (they expire after "
                f"{self._config.upload_ttl_seconds} seconds), then try again."
            )

    def receive_blob(
        self, p: PrincipalRef, ref: TableRef, upload_id: uuid.UUID, sha256: str
    ) -> BlobUpload:
        """Open the stream for one file of an open upload.

        Parameters
        ----------
        p
            The acting principal, who must be the one that began the upload.
        ref
            The artifact.
        upload_id
            The upload.
        sha256
            The file's hash, as a request gave it; untrusted.

        Returns
        -------
        BlobUpload
            The stream to write to. No database transaction stays open.

        Raises
        ------
        PermissionDenied
            If the caller may not write the name, or has a task token's scope and no task
            attempt.
        NotFoundError
            If no artifact has this name.
        ValidationError
            If the name belongs to something that is not an artifact, the hash
            is not 64 lower-case hex digits, or the upload's manifest names no
            file with it.
        ConflictError
            If the upload does not exist, is another caller's, expired or was committed.
        StorageUnavailableError
            If storage could not open the file for writing.
        """
        self._authz.authorize(p, Action.WRITE_TABLE, self._securable(ref))
        self._require_attempt(p)
        artifact_id = self._artifact(ref).id
        path = blob_path(artifact_id, sha256)
        upload = self._open_upload(p, artifact_id, upload_id)
        expires_at = upload.expires_at
        try:
            size = manifest_size(upload.manifest_json, sha256)
        except ValueError:
            raise ConflictError(UPLOAD_GONE) from None
        if size is None:
            raise ValidationError("This upload's manifest names no file with that SHA-256.")
        # Everything the stream needs is read before the rollback: reading an attribute of an
        # expired row afterwards would begin a new transaction that stays open for the body.
        storage = self._storage.for_environment(ref.environment).storage
        try:
            writer = storage.open_write(path)
        except Exception as exc:
            logger.warning(
                "Opening a file of an artifact upload failed (%s).",
                type(exc).__name__,
                extra=log_event(LogEvent.STORAGE_UNREACHABLE),
            )
            self._session.rollback()
            raise StorageUnavailableError(STORAGE_NOT_WRITTEN) from None
        self._session.rollback()
        return BlobUpload(
            self._session, writer, artifact_id, upload_id, sha256, size, self._clock, expires_at
        )

    def put_blob(
        self,
        p: PrincipalRef,
        ref: TableRef,
        upload_id: uuid.UUID,
        sha256: str,
        chunks: Iterable[bytes],
    ) -> bool:
        """Receive one whole file: open, write each piece, finish.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The artifact.
        upload_id
            The upload.
        sha256
            The file's hash.
        chunks
            The file's contents, piece by piece.

        Returns
        -------
        bool
            `True` if the file was stored by this call, `False` if the
            artifact already had it.
        """
        blob = self.receive_blob(p, ref, upload_id, sha256)
        try:
            for chunk in chunks:
                blob.write(chunk)
            return blob.finish()
        except BaseException:
            blob.abort()
            raise

    def list_uploads(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        name: str | None = None,
        limit: int = 100,
        after: UploadCursor | None = None,
    ) -> OpenUploadPage:
        """List the caller's own open uploads of an environment, newest first.

        An upload is the caller's own when the same principal began it and, for a task, the same
        attempt (task run and claim): exactly the uploads `abort_upload` accepts. Other
        principals' uploads are never listed, and nothing says that any exist. An upload in an
        artifact the caller may no longer write is left out, because they could not withdraw it.
        No manifest is read.

        Parameters
        ----------
        p
            The acting principal.
        environment
            The environment.
        name
            Only this artifact, as ``namespace.name``; every artifact when `None`.
        limit
            The most uploads in the page.
        after
            The previous page's `next`, to continue after it.

        Returns
        -------
        OpenUploadPage
            Up to `limit` uploads and the cursor of the next page.

        Raises
        ------
        PermissionDenied
            If the caller may not read the environment, or has a bound task scope and no run
            context.
        NotFoundError
            If the environment does not exist.
        ValidationError
            If `name` is not ``namespace.name``.
        """
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment))
        self._require_attempt(p)
        environment_id = resolve_environment_id(self._session, environment)
        if environment_id is None:
            raise NotFoundError(f"No environment named {environment!r}.")
        context = p.run_context
        run, claim = (None, None) if context is None else (context.task_run_id, context.claim)
        query = (
            select(
                ArtifactUpload.id,
                ArtifactUpload.created_at,
                ArtifactUpload.expires_at,
                Table.name,
                Namespace.path,
            )
            .join(Table, Table.id == ArtifactUpload.artifact_id)
            .join(Namespace, Namespace.id == Table.namespace_id)
            .where(
                Namespace.environment_id == environment_id,
                Table.table_type == TableType.ARTIFACT,
                ArtifactUpload.principal_id == p.id,
                ArtifactUpload.task_run_id.is_(None)
                if run is None
                else ArtifactUpload.task_run_id == run,
                ArtifactUpload.claim.is_(None) if claim is None else ArtifactUpload.claim == claim,
                ArtifactUpload.committed_version.is_(None),
                ArtifactUpload.expires_at > self._clock(),
            )
            .order_by(ArtifactUpload.created_at.desc(), ArtifactUpload.id.desc())
        )
        if name is not None:
            wanted = TableRef.from_dotted(environment, name)
            query = query.where(
                Namespace.path == wanted.namespace.joined(), Table.name == wanted.name
            )
        if after is not None:
            query = query.where(
                (ArtifactUpload.created_at < after.created_at)
                | ((ArtifactUpload.created_at == after.created_at) & (ArtifactUpload.id < after.id))
            )
        writable: dict[TableRef, bool] = {}
        kept: list[OpenUploadView] = []
        rows = self._session.execute(query.execution_options(yield_per=100))
        try:
            for upload_id, created_at, expires_at, table_name, path in rows:
                ref = TableRef(environment, NamespacePath.parse(path), table_name)
                if ref not in writable:
                    writable[ref] = self._authz.permits(p, Action.WRITE_TABLE, self._securable(ref))
                if writable[ref]:
                    kept.append(OpenUploadView(upload_id, ref, created_at, expires_at))
                if len(kept) > limit:
                    break
        finally:
            rows.close()
        page = kept[:limit]
        more = UploadCursor(page[-1].created_at, page[-1].upload_id) if len(kept) > limit else None
        return OpenUploadPage(tuple(page), more)

    def abort_upload(self, p: PrincipalRef, ref: TableRef, upload_id: uuid.UUID) -> None:
        """Withdraw an upload that was begun and will not be finished.

        Deletes the upload's row, under the artifact's lock, if it is open (not committed, not
        expired), belongs to this principal and, for a task, to this task run and claim.
        Anything else is `UPLOAD_GONE`, one text for every reason, so nothing is revealed.
        No bytes are deleted: the files the upload stored stay in storage, with their
        `artifact_blobs` rows, until the sweep finds that nothing names them.

        There is no protected-write guard call. The abort writes no version and no bytes and
        cannot touch anything that is not the caller's own unfinished upload, so it is not a way
        around a protected environment's rules; and a person who began an upload under
        break-glass must not need a second break-glass reason to withdraw it. It does authorize
        `WRITE_TABLE` first, like every upload operation.

        Parameters
        ----------
        p
            The acting principal; must be the one that began the upload.
        ref
            The artifact.
        upload_id
            The upload.

        Raises
        ------
        PermissionDenied
            If the caller may not write the name, or has a bound task scope and no run context.
        NotFoundError
            If no artifact has this name, or it was dropped meanwhile.
        ValidationError
            If the name belongs to something that is not an artifact.
        ConflictError
            With `UPLOAD_GONE` if the upload does not exist, is another caller's or another
            artifact's, expired or was committed.
        """
        self._authz.authorize(p, Action.WRITE_TABLE, self._securable(ref))
        self._require_attempt(p)
        artifact_id = self._artifact(ref).id
        try:
            if not lock_artifact(self._session, artifact_id):
                raise self._not_found(ref)
            self._session.delete(self._open_upload(p, artifact_id, upload_id))
            self._session.commit()
        except BaseException:
            self._session.rollback()
            raise

    @staticmethod
    def _check_sources(sources: Sequence[ArtifactSource]) -> None:
        """Check the names and versions a request gave as sources.

        Parameters
        ----------
        sources
            The sources as a request gave them; untrusted.

        Raises
        ------
        ValidationError
            If a name is empty, too long or has a control, format, separator,
            surrogate, private-use or unassigned character (the rule of
            `parse_source_versions`), or a version is not a whole number from 1
            to 2147483647; if one name has two versions, or there are more than
            `MAX_SOURCE_VERSIONS` sources. A bad value is not echoed.
        """
        if len(sources) > MAX_SOURCE_VERSIONS:
            raise ValidationError(
                f"A version may name at most {MAX_SOURCE_VERSIONS} sources; name only the tables "
                "and artifacts the task read."
            )
        seen: dict[str, int | None] = {}
        for source in sources:
            version = source.version
            if (
                not isinstance(source.name, str)
                or not 0 < len(source.name) <= MAX_SOURCE_NAME
                or any(unicodedata.category(char)[0] in "CZ" for char in source.name)
                or (version is not None and not _is_version(version))
            ):
                raise ValidationError(_BAD_SOURCE)
            if source.name in seen and seen[source.name] != version:
                raise ValidationError(
                    f"{shown(source.name)} is named as a source twice with different versions; "
                    "name each source once."
                )
            seen[source.name] = version

    def _attempt_of(
        self, p: PrincipalRef
    ) -> tuple[uuid.UUID | None, int | None, uuid.UUID | None, str | None]:
        """Name the task attempt a commit belongs to.

        Parameters
        ----------
        p
            The committing principal.

        Returns
        -------
        tuple
            The task run id, the claim, the project version id and its commit, each
            `None` without a run context.
        """
        context = p.run_context
        if context is None:
            return None, None, None, None
        task = self._session.get(TaskRun, context.task_run_id)
        assert task is not None  # nosec B101 -- the guard verified the attempt
        run = self._session.get(Run, task.run_id)
        assert run is not None  # nosec B101 -- the guard verified the attempt
        version = self._session.get(JobVersion, run.job_version_id)
        assert version is not None  # nosec B101 -- a foreign key holds the project version
        return context.task_run_id, context.claim, version.id, version.commit_sha

    def commit_upload(
        self,
        p: PrincipalRef,
        ref: TableRef,
        upload_id: uuid.UUID,
        *,
        sources: Sequence[ArtifactSource] = (),
    ) -> CommitResult:
        """Turn a complete upload into the artifact's next version, in one transaction.

        Parameters
        ----------
        p
            The acting principal, who must be the one that began the upload.
        ref
            The artifact.
        upload_id
            The upload.
        sources
            What the version was made from, for lineage. The caller must be
            able to read each.

        Returns
        -------
        CommitResult
            The version and the numbers of the versions it pruned. A second commit of the same
            upload by the same caller (an answer that was lost) returns the same version, with
            no pruned versions, and writes nothing.

        Raises
        ------
        PermissionDenied
            If the caller may not write the name, may not read a source, or
            the guard refuses (protected environment, superseded attempt).
        NotFoundError
            If the artifact or a source does not exist.
        ValidationError
            If the name belongs to something that is not an artifact, or a
            source's name or version is malformed.
        ConflictError
            If the upload does not exist, is another caller's or expired, or
            files of it are not stored yet.
        """
        securable = self._securable(ref)
        self._authz.authorize(p, Action.WRITE_TABLE, securable)
        self._require_attempt(p)
        artifact = self._artifact(ref)
        artifact_id = artifact.id
        self._check_sources(sources)
        self._guard.check(p, ref.environment, WriteOperation.ARTIFACT_VERSION, table_target(ref))
        environment_id = resolve_environment_id(self._session, ref.environment)
        assert environment_id is not None  # nosec B101 -- the artifact was just found in it
        storage = self._storage.for_environment(ref.environment).storage
        written: str | None = None
        try:
            if not lock_artifact(self._session, artifact_id):
                raise self._not_found(ref)
            upload = self._open_upload(p, artifact_id, upload_id, committed=True)
            if upload.committed_version is not None:
                return self._repeat_commit(p, ref, artifact_id, upload.committed_version)
            # After the repeat check, so a retry whose answer was lost never fails on a source
            # that changed since: a repeat returns what the first commit stored.
            lineage = CatalogService(
                self._session, self._storage, self._authz
            ).resolve_lineage_sources(
                p,
                ref.environment,
                [source.name for source in sources],
                versions={s.name: s.version for s in sources if s.version is not None},
            )
            try:
                manifest = parse_manifest(upload.manifest_json)
            except ValidationError:
                raise ConflictError(UPLOAD_GONE) from None
            wanted = manifest.hashes()
            stored = stored_hashes(self._session, artifact_id, wanted)
            missing = [digest for digest, size in wanted.items() if stored.get(digest) != size]
            if missing:
                raise ConflictError(FILES_NOT_STORED.format(count=len(missing)))
            # A number storage has seen is never reused: an object with no row may be
            # the manifest of a version a restored database forgot, and overwriting it would
            # leave that version's files named by nothing.
            try:
                highest, unknown_manifests = self._manifest_state(storage, artifact_id)
            except _ListingFailed:
                raise ConflictError(COMMIT_NOT_WRITTEN) from None
            if highest >= MAX_VERSION:
                raise ConflictError(
                    VERSION_LIMIT_REACHED.format(name=ref.dotted(), limit=MAX_VERSION)
                )
            number = highest + 1
            now = self._clock()
            task_run_id, claim, job_version_id, commit_sha = self._attempt_of(p)
            row = ArtifactVersion(
                id=uuid.uuid4(),
                artifact_id=artifact_id,
                version=number,
                manifest_sha256=manifest.sha256(),
                manifest_json=manifest.to_json(),
                size_bytes=manifest.size_bytes,
                file_count=manifest.file_count,
                is_directory=manifest.is_directory,
                task_run_id=task_run_id,
                claim=claim,
                job_version_id=job_version_id,
                commit_sha=commit_sha,
                created_by=p.id,
                created_at=now,
            )
            self._session.add(row)
            upload.committed_version = number
            # A repeat of this commit must stay possible for a full lifetime from now, however
            # late in the upload's own lifetime the commit landed; a committed row takes no
            # slot and protects no files. Nothing reads its manifest copy any more, and it
            # can be megabytes.
            upload.expires_at = max(
                upload.expires_at, now + timedelta(seconds=self._config.upload_ttl_seconds)
            )
            upload.manifest_json = {}
            for source in lineage:
                self._session.add(
                    LineageEdge(
                        id=uuid.uuid4(),
                        environment_id=environment_id,
                        source_table_id=source.table_id,
                        source_name=source.name,
                        source_snapshot_id=source.snapshot_id,
                        target_table_id=artifact_id,
                        target_name=ref.dotted(),
                        target_snapshot_id=number,
                        query_run_id=None,
                        created_at=now,
                    )
                )
            self._session.add_all(
                connection_edges(
                    connection_sources(self._session, p, environment_id),
                    environment_id=environment_id,
                    target_id=artifact_id,
                    target_name=ref.dotted(),
                    target_snapshot_id=number,
                    query_run_id=None,
                    created_at=now,
                )
            )
            AuditService(self._session).record(
                p.id,
                AuditEventType.ARTIFACT_VERSION_ADDED,
                securable=securable,
                detail={
                    "artifact": ref.dotted(),
                    "version": number,
                    "manifest_sha256": row.manifest_sha256,
                    "size_bytes": row.size_bytes,
                    "file_count": row.file_count,
                    "task_run_id": None if task_run_id is None else str(task_run_id),
                    "principal": p.name,
                },
            )
            pruned = self._prune(p, ref, artifact_id, now, unknown_manifests=unknown_manifests)
            self._session.flush()
            self._guard.record_pending()
            try:
                write_atomically(storage, manifest_path(artifact_id, number), manifest.canonical())
            except Exception as exc:
                logger.warning(
                    "Writing the manifest of a new version of %s failed (%s).",
                    shown(ref.dotted()),
                    type(exc).__name__,
                )
                raise ConflictError(COMMIT_NOT_WRITTEN) from None
            written = manifest_path(artifact_id, number)
            view = self._version_view(ref, row, with_files=True)
            self._session.commit()
        except BaseException:
            if written is not None:
                # Still holding the artifact's lock, so no other commit can have this number:
                # the object is ours to remove. A crash instead leaves it, and the cost is a gap
                # in the numbers, never an overwrite.
                with contextlib.suppress(Exception):
                    delete_quietly(storage, written)
            self._session.rollback()
            raise
        self._release(ref, artifact_id, pruned)
        return CommitResult(version=view, deleted_versions=tuple(v for v, _ in pruned))

    def _repeat_commit(
        self, p: PrincipalRef, ref: TableRef, artifact_id: uuid.UUID, number: int
    ) -> CommitResult:
        """Answer a repeated commit with the version the upload already produced.

        Called under the artifact's lock, after the same authorization and guard checks as a
        first commit. Writes nothing: no version, no lineage, no audit event, no pruning.

        Parameters
        ----------
        p
            The acting principal, who began the upload.
        ref
            The artifact.
        artifact_id
            Its id.
        number
            The version the upload produced.

        Returns
        -------
        CommitResult
            The same version, with no pruned versions.

        Raises
        ------
        ConflictError
            With its own text, not `UPLOAD_GONE`, if that version was deleted since: a client
            that starts again on `UPLOAD_GONE` would store the version a second time.
        """
        row = version_row(self._session, artifact_id, number)
        if row is None or row.deleted_at is not None:
            raise ConflictError(
                f"This upload was committed as version {number} of {ref.dotted()}, and that "
                "version has since been deleted."
            )
        self._guard.record_pending()
        view = self._version_view(ref, row, with_files=True)
        self._session.commit()
        return CommitResult(version=view, deleted_versions=())

    def _manifest_state(self, storage: Storage, artifact_id: uuid.UUID) -> tuple[int, bool]:
        """Read, under the artifact's lock, which version numbers the catalog and storage have used.

        Parameters
        ----------
        storage
            The environment's storage.
        artifact_id
            The artifact; the caller holds its lock.

        Returns
        -------
        tuple
            The highest number of any version row (deleted ones included) or manifest object,
            and whether storage has a manifest object with no version row at all: a commit
            that failed part-way, or a version a restored database forgot.

        Raises
        ------
        _ListingFailed
            If storage could not list the manifest objects; the callers decide what that means.
        """
        rows = set(
            self._session.scalars(
                select(ArtifactVersion.version).where(ArtifactVersion.artifact_id == artifact_id)
            )
        )
        try:
            objects = {number for number, _ in stored_manifests(storage, artifact_id)}
        except Exception as exc:
            logger.warning(
                "Listing the manifest objects of artifact %s failed (%s).",
                artifact_id.hex,
                type(exc).__name__,
            )
            raise _ListingFailed from None
        return max(rows | objects, default=0), bool(objects - rows)

    def _remove_blob_rows(self, artifact_id: uuid.UUID, hashes: Iterable[str]) -> None:
        """Forget the stored files nothing needs; commits nothing.

        Parameters
        ----------
        artifact_id
            The artifact.
        hashes
            The files to forget.
        """
        ordered = sorted(hashes)
        for start in range(0, len(ordered), 500):
            self._session.execute(
                delete(ArtifactBlob)
                .where(
                    ArtifactBlob.artifact_id == artifact_id,
                    ArtifactBlob.sha256.in_(ordered[start : start + 500]),
                )
                .execution_options(synchronize_session=False)
            )

    def _retire(
        self,
        p: PrincipalRef,
        ref: TableRef,
        rows: Sequence[ArtifactVersion],
        now: datetime,
        *,
        reason: str,
        task_run_id: uuid.UUID | None = None,
        unknown_manifests: bool = False,
    ) -> list[tuple[int, set[str]]]:
        """Mark versions deleted and forget the files nothing else needs; commits nothing.

        The caller holds the artifact's lock and `rows` are fresh. Each row stays with
        `deleted_at` and `deleted_by`; its lineage edges are not touched. The files that
        become unneeded are computed once, after the rows are marked, from every remaining
        live version and every unexpired upload, with the caller's `now`. If a manifest that
        could name a file cannot be read (one of those, or a version's own), no file is
        freed: the rows are still marked, the audit event says why, and the files are left
        for the sweep. `freed_files` in the event counts the files whose `artifact_blobs`
        rows were removed, that is the files made eligible for removal from storage.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The artifact.
        rows
            The live versions to retire, newest first.
        now
            The deletion's time.
        reason
            ``requested`` or ``keep_versions``, for the audit events.
        task_run_id
            The task run whose commit prunes, for the audit events of ``keep_versions``.
        unknown_manifests
            Storage holds manifest objects with no version row (a failed commit, or a version a
            restored database forgot): their files may be shared with the versions retired
            here, and only the sweep, which reads them, may judge that. Nothing is freed in
            the request and the audit event says why.

        Returns
        -------
        list of tuple
            Each version's number and the hashes whose rows were removed, in the order of
            `rows`. A hash appears under the first version that owned it.
        """
        artifact_id = rows[0].artifact_id
        owned: list[set[str] | None] = []
        for row in rows:
            try:
                owned.append(checked_hashes(row.manifest_json))
            except UnreadableManifest:
                owned.append(None)
        for row in rows:
            row.deleted_at = now
            row.deleted_by = p.id
        self._session.flush()
        candidates = set().union(*(own for own in owned if own is not None))
        free: set[str] | None
        try:
            free = (
                set()
                if unknown_manifests
                else unreferenced_hashes(self._session, artifact_id, candidates, now=now)
            )
        except UnreadableManifest:
            free = None
        retired: list[tuple[int, set[str]]] = []
        claimed: set[str] = set()
        for row, own in zip(rows, owned, strict=True):
            kept: str | None = None
            freed: set[str] = set()
            if own is None:
                kept = KEPT_DAMAGED_OWN.format(version=row.version)
            elif unknown_manifests:
                kept = KEPT_UNKNOWN_MANIFESTS
            elif free is None:
                kept = KEPT_DAMAGED_OTHER
            else:
                freed = (own & free) - claimed
            if kept is not None:
                logger.warning(
                    "Deleting version %s of %s: %s", row.version, shown(ref.dotted()), kept
                )
            claimed |= freed
            self._remove_blob_rows(artifact_id, freed)
            detail: dict[str, object] = {
                "artifact": ref.dotted(),
                "version": row.version,
                "reason": reason,
                "size_bytes": row.size_bytes,
                "freed_files": len(freed),
                "principal": p.name,
            }
            if kept is not None:
                detail["kept_reason"] = kept
            if task_run_id is not None:
                detail["task_run_id"] = str(task_run_id)
            AuditService(self._session).record(
                p.id,
                AuditEventType.ARTIFACT_VERSION_DELETED,
                securable=self._securable(ref),
                detail=detail,
            )
            retired.append((row.version, freed))
        return retired

    def _prune(
        self,
        p: PrincipalRef,
        ref: TableRef,
        artifact_id: uuid.UUID,
        now: datetime,
        *,
        unknown_manifests: bool = False,
    ) -> list[tuple[int, set[str]]]:
        """Apply the task's `keep_versions` inside the commit's transaction.

        The number comes only from the bound task token (`BoundTask.keep_versions`, resolved
        from the deployed manifest at authentication); never from a request, a setting or
        a stored manifest. A caller without a bound token, a token without a retention, and
        a value that is not a whole number of at least 1 prune nothing.

        Parameters
        ----------
        p
            The committing principal.
        ref
            The artifact.
        artifact_id
            Its id; the caller holds its lock and has added the new version.
        now
            The commit's time.
        unknown_manifests
            Storage holds manifest objects with no version row; see `_retire`.

        Returns
        -------
        list of tuple
            Each pruned version's number and the hashes it freed, newest first.
        """
        bound = p.task_scope.bound if p.task_scope is not None else None
        keep = None if bound is None else bound.keep_versions
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
            return []
        self._session.flush()
        live = self._session.scalars(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.deleted_at.is_(None))
            .order_by(ArtifactVersion.version.desc())
            .execution_options(populate_existing=True)
        ).all()
        old = live[keep:]
        if not old:
            return []
        context = p.run_context
        task_run_id = None if context is None else context.task_run_id
        return self._retire(
            p,
            ref,
            old,
            now,
            reason="keep_versions",
            task_run_id=task_run_id,
            unknown_manifests=unknown_manifests,
        )

    def _release(
        self, ref: TableRef, artifact_id: uuid.UUID, retired: list[tuple[int, set[str]]]
    ) -> set[str]:
        """Remove the bytes of deleted versions, after their rows are committed.

        A file is removed only while the artifact's lock is held and it still has no
        `artifact_blobs` row: a file stored again in between has a row and is kept, so a row
        never exists without its bytes. The lock is taken, the row checked and the object
        deleted for one file at a time. A failure is logged, without the storage error's text, and
        the rest is left to the sweep; it never reaches the caller.

        The request spends at most `RELEASE_BUDGET_FILES` files and `RELEASE_BUDGET_SECONDS`
        seconds here, whichever comes first. A version whose files were all handled within the
        budget has its manifest object removed; for a version the budget cut short, and for every
        version after it, the files left and the manifest object are the sweep's.

        Parameters
        ----------
        ref
            The artifact.
        artifact_id
            Its id.
        retired
            Each deleted version's number and the hashes whose rows were removed.

        Returns
        -------
        set of str
            The hashes whose bytes this call removed (or found already gone). The rest were
            cut by the budget, kept because the file was stored again, or failed to delete:
            the sweep's.
        """
        removed: set[str] = set()
        if not retired:
            return removed
        try:
            storage = self._storage.for_environment(ref.environment).storage
            started = self._monotonic()
            handled = 0

            def spent() -> bool:
                """Say whether the request's budget is used up.

                Returns
                -------
                bool
                    `True` once `RELEASE_BUDGET_FILES` deletes or `RELEASE_BUDGET_SECONDS` seconds
                    are used.
                """
                return (
                    handled >= RELEASE_BUDGET_FILES
                    or self._monotonic() - started >= RELEASE_BUDGET_SECONDS
                )

            for number, hashes in retired:
                for digest in sorted(hashes):
                    if spent():
                        self._release_cut(ref)
                        return removed
                    handled += 1
                    # One lock, one check, one delete, then the lock goes: a dead connection
                    # raises at the next lock and ends the release, and a slow store holds the
                    # artifact's lock for one delete, never for a batch.
                    if not lock_artifact(self._session, artifact_id):
                        self._session.rollback()
                        return removed
                    if not stored_hashes(self._session, artifact_id, [digest]):
                        delete_quietly(storage, blob_path(artifact_id, digest))
                        removed.add(digest)
                    self._session.rollback()
                # A manifest object is one storage delete like a file: versions that freed
                # nothing must not cost the request an unbudgeted delete each.
                if spent():
                    self._release_cut(ref)
                    return removed
                handled += 1
                delete_quietly(storage, manifest_path(artifact_id, number))
        except Exception as exc:
            logger.warning(
                "Removing the bytes of deleted versions of %s failed (%s); the sweep removes them.",
                shown(ref.dotted()),
                type(exc).__name__,
            )
            with contextlib.suppress(Exception):
                self._session.rollback()
        return removed

    @staticmethod
    def _release_cut(ref: TableRef) -> None:
        """Log that a release stopped at its budget.

        Parameters
        ----------
        ref
            The artifact.
        """
        logger.info(
            "Removing the bytes of deleted versions of %s stopped after its budget; the sweep "
            "removes the rest.",
            shown(ref.dotted()),
        )

    @staticmethod
    def _refuse_run(p: PrincipalRef) -> None:
        """Refuse a principal that acts through a task token or for a task or query.

        Deleting an artifact's bytes cannot be undone, so no form of task token may do it,
        bound to a run or not, whatever its principal may do otherwise.

        Parameters
        ----------
        p
            The acting principal.

        Raises
        ------
        PermissionDenied
            With `DELETE_FROM_RUN_MESSAGE` if `p` has a task scope or a run context.
        """
        if p.run_context is not None or p.task_scope is not None:
            raise PermissionDenied(DELETE_FROM_RUN_MESSAGE)

    def delete_version(self, p: PrincipalRef, ref: TableRef, version: int) -> DeletedVersionView:
        """Delete one version's bytes; its record and its lineage stay.

        The files no live version and no unexpired upload names are forgotten in the same
        transaction that marks the version, and removed from storage right after it. The files
        that stay, and why, are worked out in that transaction too, under the artifact's lock;
        what the removal after the commit could not do is added as left to the sweep.

        Parameters
        ----------
        p
            The acting principal; needs `DROP_TABLE` on the artifact and no run context.
        ref
            The artifact.
        version
            The number.

        Returns
        -------
        DeletedVersionView
            The version, with `deleted_at` and `deleted_by` set and no files, and the files this
            request freed and kept. A version number appears in a kept group only when the
            caller may also read the artifact.

        Raises
        ------
        PermissionDenied
            If the caller may not drop the name, acts for a task or query, or the guard
            refuses (protected environment without break-glass).
        NotFoundError
            If the artifact or the version does not exist, or the artifact was dropped
            meanwhile.
        ValidationError
            If the name belongs to something that is not an artifact.
        ConflictError
            If the version is already deleted.
        """
        self._authz.authorize(p, Action.DROP_TABLE, self._securable(ref))
        self._refuse_run(p)
        self._guard.check(
            p, ref.environment, WriteOperation.DELETE_ARTIFACT_VERSION, table_target(ref)
        )
        artifact_id = self._artifact(ref).id
        if not _is_version(version):
            raise self._no_version(ref, version)
        try:
            if not lock_artifact(self._session, artifact_id):
                raise self._not_found(ref)
            row = version_row(self._session, artifact_id, version)
            if row is None:
                raise self._no_version(ref, version)
            self._session.refresh(row)
            if row.deleted_at is not None:
                raise ConflictError(f"Version {version} of {ref.dotted()} is already deleted.")
            try:
                _, unknown_manifests = self._manifest_state(
                    self._storage.for_environment(ref.environment).storage, artifact_id
                )
            except _ListingFailed:
                unknown_manifests = True  # unknown, so assume the worst: free nothing
            now = self._clock()
            try:
                checked_hashes(row.manifest_json)
                sizes: dict[str, int] | None = declared_sizes(row.manifest_json)
            except UnreadableManifest:
                sizes = None
            retired = self._retire(
                p,
                ref,
                [row],
                now,
                reason="requested",
                unknown_manifests=unknown_manifests,
            )
            freed = retired[0][1]
            kept = self._kept_by_reason(
                p, ref, artifact_id, row, sizes, freed, now, search=not unknown_manifests
            )
            self._guard.record_pending()
            view = self._version_view(ref, row, with_files=False)
            self._session.commit()
        except BaseException:
            self._session.rollback()
            raise
        removed = self._release(ref, artifact_id, retired)
        left = freed - removed
        if left:
            assert sizes is not None  # nosec B101 -- a damaged manifest freed nothing
            kept = [
                *kept,
                KeptFiles(KeptReason.LEFT_TO_SWEEP, len(left), sum(sizes[h] for h in left)),
            ]
        return DeletedVersionView(
            **{f.name: getattr(view, f.name) for f in fields(view)},
            freed_files=len(removed),
            freed_bytes=sum((sizes or {})[h] for h in removed),
            kept=tuple(kept),
        )

    def _kept_by_reason(
        self,
        p: PrincipalRef,
        ref: TableRef,
        artifact_id: uuid.UUID,
        row: ArtifactVersion,
        sizes: dict[str, int] | None,
        freed: set[str],
        now: datetime,
        *,
        search: bool,
    ) -> list[KeptFiles]:
        """Say which files of a deleted version stayed in storage and why; reads, commits nothing.

        Called in the delete's transaction under the artifact's lock, after `_retire` marked
        the row, so the live versions and open uploads it reads are the ones `_retire` judged.

        Parameters
        ----------
        p
            The acting principal; decides whether a version number may be shown.
        ref
            The artifact.
        artifact_id
            Its id.
        row
            The version just deleted.
        sizes
            Hash to size of the version's files; `None` when its manifest is damaged.
        freed
            The hashes `_retire` made removable.
        now
            The delete's time.
        search
            Look for the versions and uploads that hold the files; `False` when storage holds
            manifests the catalog does not know, in which case only the sweep can say.

        Returns
        -------
        list of KeptFiles
            Other versions newest first, then open uploads, then files left to the sweep. The
            files of this request's own release shortfall are added by the caller.
        """
        if sizes is None:
            files, size = _recorded_files(row)
            return [KeptFiles(KeptReason.LEFT_TO_SWEEP, files, size)]
        stayed = set(sizes) - freed
        if not stayed:
            return []
        try:
            holders = holders_of(self._session, artifact_id, stayed, now=now) if search else {}
        except UnreadableManifest:
            holders = {}
        shown_versions = any(holders.get(h) is not None for h in stayed) and self._authz.permits(
            p, Action.READ_TABLE, self._securable(ref)
        )
        groups: dict[tuple[int, int | None], set[str]] = {}
        for digest in stayed:
            if digest not in holders:
                key = (2, None)
            elif holders[digest] is None:
                key = (1, None)
            else:
                key = (0, holders[digest] if shown_versions else None)
            groups.setdefault(key, set()).add(digest)
        reasons = (KeptReason.OTHER_VERSION, KeptReason.OPEN_UPLOAD, KeptReason.LEFT_TO_SWEEP)
        return [
            KeptFiles(reasons[kind], len(group), sum(sizes[h] for h in group), version)
            for (kind, version), group in sorted(
                groups.items(), key=lambda item: (item[0][0], -(item[0][1] or 0))
            )
        ]

    def drop_artifact(self, p: PrincipalRef, ref: TableRef) -> None:
        """Drop an artifact: its rows, its grants and its bytes.

        The marker ``artifacts/<its id>/.dropped`` is written first, under the artifact's lock
        (if that fails the drop fails and nothing is deleted); the catalog row goes (its
        versions, uploads and stored-file rows by cascade) and the grants attached to it are
        revoked, in one transaction; then everything under ``artifacts/<its id>/`` is removed
        from storage, orphan manifest objects included, and the marker last. The sweep
        finishes an interrupted removal, and only a prefix with the marker.
        A new artifact of the same name has another id and starts at version 1.

        Parameters
        ----------
        p
            The acting principal; needs `DROP_TABLE` on the artifact and no run context.
        ref
            The artifact.

        Raises
        ------
        PermissionDenied
            If the caller may not drop the name, acts for a task or query, or the guard
            refuses (protected environment without break-glass).
        NotFoundError
            If the artifact does not exist or was dropped meanwhile.
        ValidationError
            If the name belongs to something that is not an artifact.
        ConflictError
            If a deployed project produces the artifact: only git may remove it
            (`lakana_core.catalog.git_owned.GIT_OWNED_ARTIFACT_DROP`, or the hidden copy to a
            caller who may not read it); checked before the marker, so nothing reaches storage.
            Or if storage could not be written (the drop marker); nothing was deleted.
        """
        securable = self._securable(ref)
        self._authz.authorize(p, Action.DROP_TABLE, securable)
        self._refuse_run(p)
        self._guard.check(p, ref.environment, WriteOperation.DROP_ARTIFACT, table_target(ref))
        artifact = self._artifact(ref)
        artifact_id = artifact.id
        environment_id = resolve_environment_id(self._session, ref.environment)
        if environment_id is not None:
            refuse_owned_drop(self._session, self._authz, p, ref, environment_id, artifact.kind)
        storage = self._storage.for_environment(ref.environment).storage
        marker = dropped_marker_path(artifact_id)
        marked = False
        try:
            if not lock_artifact(self._session, artifact_id):
                raise self._not_found(ref)
            # The marker first, under the lock: the sweep removes the files of an id with no
            # row only while it exists, so a failure to write it fails the drop, nothing deleted.
            try:
                storage.write(
                    marker,
                    json.dumps(
                        {"dropped_at": self._clock().isoformat(), "artifact": ref.dotted()}
                    ).encode(),
                )
            except Exception as exc:
                logger.warning(
                    "Writing the drop marker of %s failed (%s).",
                    shown(ref.dotted()),
                    type(exc).__name__,
                )
                raise ConflictError(DROP_NOT_WRITTEN.format(name=ref.dotted())) from None
            marked = True
            live = self._session.execute(
                select(func.count())
                .select_from(ArtifactVersion)
                .where(
                    ArtifactVersion.artifact_id == artifact_id,
                    ArtifactVersion.deleted_at.is_(None),
                )
            ).scalar_one()
            revoke_attached(self._session, p, table_id=artifact_id)
            AuditService(self._session).record(
                p.id,
                AuditEventType.ARTIFACT_DROPPED,
                securable=securable,
                detail={
                    "artifact": ref.dotted(),
                    "artifact_id": str(artifact_id),
                    "versions": live,
                    "principal": p.name,
                },
            )
            self._session.execute(
                delete(Table)
                .where(Table.id == artifact_id)
                .execution_options(synchronize_session=False)
            )
            self._guard.record_pending()
            self._session.commit()
            self._session.expire_all()
        except BaseException:
            if marked:
                # A marker next to a row that lives would make the prefix deletable if the row
                # were ever lost without a drop; still under the lock, it is ours to remove.
                with contextlib.suppress(Exception):
                    delete_quietly(storage, marker)
            self._session.rollback()
            raise
        try:
            prefix = artifact_prefix(artifact_id)
            for path in storage.list(prefix):
                if path.startswith(prefix + "/") and path != marker:
                    delete_quietly(storage, path)
            # Last, so an interrupted removal is resumed by the sweep.
            delete_quietly(storage, marker)
        except Exception as exc:
            logger.warning(
                "Removing the bytes of the dropped artifact %s failed (%s); the sweep removes "
                "them.",
                shown(ref.dotted()),
                type(exc).__name__,
            )

    def _audit_download(
        self, p: PrincipalRef, ref: TableRef, artifact_id: uuid.UUID, found: ArtifactVersion
    ) -> None:
        """Record `ARTIFACT_DOWNLOADED` once per principal and version.

        An event is written only if the same principal has none for the same
        artifact and version within the last `upload_ttl_seconds`; a bound task
        token is never recorded. Ends the transaction either way.

        Parameters
        ----------
        p
            The downloading principal.
        ref
            The artifact.
        artifact_id
            Its id; a recreated name is another artifact.
        found
            The version whose file is fetched.
        """
        if p.task_scope is not None and p.task_scope.bound is not None:
            self._session.rollback()
            return
        since = self._clock() - timedelta(seconds=self._config.upload_ttl_seconds)
        recent = self._session.scalars(
            select(AuditEvent.detail).where(
                AuditEvent.event_type == AuditEventType.ARTIFACT_DOWNLOADED,
                AuditEvent.principal_id == p.id,
                AuditEvent.securable_environment == ref.environment,
                AuditEvent.securable_namespace == ref.namespace.joined(),
                AuditEvent.securable_table == ref.name,
                AuditEvent.occurred_at >= since,
            )
        ).all()
        key = (str(artifact_id), found.version)
        if any(
            isinstance(detail, dict) and (detail.get("artifact_id"), detail.get("version")) == key
            for detail in recent
        ):
            self._session.rollback()
            return
        AuditService(self._session).record(
            p.id,
            AuditEventType.ARTIFACT_DOWNLOADED,
            securable=self._securable(ref),
            detail={
                "artifact": ref.dotted(),
                "artifact_id": str(artifact_id),
                "version": found.version,
                "file_count": found.file_count,
                "size_bytes": found.size_bytes,
                "principal": p.name,
            },
            commit=True,
        )

    def open_blob(self, p: PrincipalRef, ref: TableRef, version: int, sha256: str) -> BlobDownload:
        """Open one file of a version for download.

        Parameters
        ----------
        p
            The acting principal.
        ref
            The artifact.
        version
            The version number.
        sha256
            The file's hash, as a request gave it; untrusted.

        Returns
        -------
        BlobDownload
            The size and the contents, piece by piece. A principal without a
            bound task token is audited once per version and hour.

        Raises
        ------
        PermissionDenied
            If the caller may not read the name.
        NotFoundError
            If the artifact, the version or the file does not exist, the
            version was deleted, or the bytes are missing from storage.
        ValidationError
            If the name belongs to something that is not an artifact, or the
            stored manifest is damaged.
        StorageUnavailableError
            If storage could not be opened for reading. No transaction is open
            when this method returns or raises it.
        """
        self._authz.authorize(p, Action.READ_TABLE, self._securable(ref))
        row = self._artifact(ref)
        found = version_row(self._session, row.id, version) if _is_version(version) else None
        if found is None:
            raise self._no_version(ref, version)
        if found.deleted_at is not None:
            raise NotFoundError(
                VERSION_DELETED_MESSAGE.format(
                    version=version, name=ref.dotted(), environment=ref.environment
                )
            )
        try:
            size = manifest_size(found.manifest_json, sha256)
        except ValueError:
            # The same text as a full parse gives; only the lookup is cheaper.
            raise ValidationError(
                f"The stored manifest of version {version} of {ref.dotted()} is damaged; "
                "delete the version and run the asset again."
            ) from None
        if size is None:
            raise NotFoundError(
                f"Version {version} of {ref.dotted()} has no file with that SHA-256."
            )
        storage = self._storage.for_environment(ref.environment).storage
        path = blob_path(row.id, sha256)
        try:
            chunks = storage.read_chunks(path)
        except NotFoundError:
            raise NotFoundError(
                f"A file of version {version} of {ref.dotted()} is missing from storage, and the "
                "catalog still lists it as stored, so running the asset again would not store it. "
                "Delete this version and every other version that has the file (`lakana artifact "
                "versions delete`), then run the asset again."
            ) from None
        except Exception as exc:
            logger.warning(
                "Opening a file of artifact %s for download failed (%s).",
                shown(ref.dotted()),
                type(exc).__name__,
                extra=log_event(LogEvent.STORAGE_UNREACHABLE),
            )
            self._session.rollback()
            raise StorageUnavailableError(STORAGE_NOT_READ) from None
        # Everything above is read before the audit: it ends the transaction, and a row
        # attribute read afterwards would begin one that stays open for the whole body.
        try:
            self._audit_download(p, ref, row.id, found)
        except BaseException:
            # Nobody will read the handle that was just opened.
            with contextlib.suppress(Exception):
                getattr(chunks, "close", lambda: None)()
            raise
        return BlobDownload(size, chunks)
