"""Rows, paths and the lock of the artifact store.

Everything here takes a session and commits nothing.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lakana_core.artifacts.manifest import SHA256_PATTERN
from lakana_core.bounds import MAX_INT32
from lakana_core.db import rowcount
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    ArtifactBlob,
    ArtifactUpload,
    ArtifactVersion,
    Table,
    TableType,
    TaskRun,
)
from lakana_core.storage.base import Storage, StoredObject
from lakana_core.storage.resolver import ARTIFACTS_PREFIX

_MANIFEST_RELATIVE = re.compile(r"\Amanifests/([1-9][0-9]{0,9})\.json\Z")
MAX_VERSION = MAX_INT32
"""Largest version number: the column is a 32-bit integer."""
_STREAM_ROWS = 4
"""Manifests fetched from the database at a time while the referenced hashes are built."""
IN_CHUNK = 500
"""Hashes per `IN` list, well below every backend's parameter limit."""


class UnreadableManifest(ValueError):
    """A stored manifest that is needed to decide what may be deleted cannot be read.

    Raised instead of treating the manifest as one that names no files: a decision to delete
    bytes made without a manifest that may name them is a decision to lose them.
    """


def current_version(session: Session, artifact_id: uuid.UUID) -> int | None:
    """Return an artifact's current version: the highest whose bytes exist.

    Parameters
    ----------
    session
        Database session.
    artifact_id
        The artifact.

    Returns
    -------
    int or None
        The version number, or `None` when no live version exists.
    """
    return session.execute(
        select(func.max(ArtifactVersion.version)).where(
            ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.deleted_at.is_(None)
        )
    ).scalar_one()


def version_row(session: Session, artifact_id: uuid.UUID, version: int) -> ArtifactVersion | None:
    """Return one version's row, deleted or not.

    Parameters
    ----------
    session
        Database session.
    artifact_id
        The artifact.
    version
        The version number.

    Returns
    -------
    ArtifactVersion or None
        The row, or `None` when the artifact has no such version.
    """
    return session.execute(
        select(ArtifactVersion).where(
            ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.version == version
        )
    ).scalar_one_or_none()


def artifact_prefix(artifact_id: uuid.UUID) -> str:
    """Return the root-relative directory of one artifact's bytes.

    Parameters
    ----------
    artifact_id
        The artifact's id. The id, never the name: a recreated name never
        reaches old bytes.

    Returns
    -------
    str
        ``artifacts/<32 hex digits>``.
    """
    return f"{ARTIFACTS_PREFIX}/{artifact_id.hex}"


def blobs_prefix(artifact_id: uuid.UUID) -> str:
    """Return the directory of one artifact's files.

    Parameters
    ----------
    artifact_id
        The artifact's id.

    Returns
    -------
    str
        ``artifacts/<32 hex digits>/blobs``.
    """
    return f"{artifact_prefix(artifact_id)}/blobs"


def blob_path(artifact_id: uuid.UUID, sha256: str) -> str:
    """Return where one file of an artifact is stored.

    Parameters
    ----------
    artifact_id
        The artifact's id.
    sha256
        The file's hash, as a caller gave it; untrusted, of any type.

    Returns
    -------
    str
        ``artifacts/<32 hex digits>/blobs/<sha256>``.

    Raises
    ------
    ValidationError
        Unless `sha256` is a string of exactly 64 lower-case hex digits. This is the one
        place a caller's string becomes part of a storage path.
    """
    if not isinstance(sha256, str) or SHA256_PATTERN.match(sha256) is None:
        raise ValidationError("A file is named by its lower-case SHA-256 (64 hex digits).")
    return f"{blobs_prefix(artifact_id)}/{sha256}"


def manifest_path(artifact_id: uuid.UUID, version: int) -> str:
    """Return where one version's manifest object is stored.

    Parameters
    ----------
    artifact_id
        The artifact's id.
    version
        The version number.

    Returns
    -------
    str
        ``artifacts/<32 hex digits>/manifests/<version>.json``.
    """
    return f"{artifact_prefix(artifact_id)}/manifests/{int(version)}.json"


DROPPED_MARKER = ".dropped"
"""Name of the object a drop writes first, directly under the artifact's directory."""


def dropped_marker_path(artifact_id: uuid.UUID) -> str:
    """Return where the marker of a dropped artifact is stored.

    A drop writes it under the artifact's lock before the catalog row is deleted, and deletes it
    last. The sweep removes the files of an id that has no catalog row only while it exists:
    without it a rowless directory may be a database that forgot an artifact, not a drop.

    Parameters
    ----------
    artifact_id
        The artifact's id.

    Returns
    -------
    str
        ``artifacts/<32 hex digits>/.dropped``.
    """
    return f"{artifact_prefix(artifact_id)}/{DROPPED_MARKER}"


def manifest_number(relative: str) -> int | None:
    """Read the version number out of a manifest object's name, strictly.

    The one parser of ``manifests/<n>.json`` names, shared by the commit that picks a number
    and the sweep that decides what to delete.

    Parameters
    ----------
    relative
        A path relative to the artifact's directory, as ``manifests/3.json``.

    Returns
    -------
    int or None
        The number, or `None` for any other name (leading zeros, a sign, another suffix, a
        subdirectory) and for a number above `MAX_VERSION`.
    """
    matched = _MANIFEST_RELATIVE.match(relative)
    if matched is None:
        return None
    number = int(matched.group(1))
    return number if number <= MAX_VERSION else None


def stored_manifests(storage: Storage, artifact_id: uuid.UUID) -> list[tuple[int, StoredObject]]:
    """List the manifest objects an artifact has in storage, with the version each names.

    One listing of ``manifests/``. Objects whose names are not a plain version number are
    left out.

    Parameters
    ----------
    storage
        The environment's storage.
    artifact_id
        The artifact.

    Returns
    -------
    list of tuple
        Each object's version number and the object, in no particular order.
    """
    prefix = artifact_prefix(artifact_id) + "/"
    found: list[tuple[int, StoredObject]] = []
    for stored in storage.list_objects(f"{prefix}manifests"):
        number = (
            manifest_number(stored.path[len(prefix) :]) if stored.path.startswith(prefix) else None
        )
        if number is not None:
            found.append((number, stored))
    return found


def write_atomically(storage: Storage, path: str, data: bytes) -> None:
    """Write a small object so that it is never visible half written.

    Uses the streaming writer: the local backend writes a temporary name and renames, an object
    store completes a multipart upload.

    Parameters
    ----------
    storage
        The environment's storage.
    path
        Root-relative path.
    data
        The contents.
    """
    writer = storage.open_write(path)
    try:
        writer.write(data)
        writer.commit()
    except BaseException:
        writer.abort()
        raise


def delete_quietly(storage: Storage, path: str) -> bool:
    """Delete an object that may already be gone.

    Parameters
    ----------
    storage
        The environment's storage.
    path
        Root-relative path.

    Returns
    -------
    bool
        `False` if there was no object.
    """
    try:
        storage.delete(path)
    except NotFoundError:
        return False
    return True


def lock_artifact(session: Session, artifact_id: uuid.UUID) -> bool:
    """Take the artifact's lock for the rest of the transaction.

    A no-op `UPDATE` of the artifact's own row: a row lock on Postgres, the
    database's write lock on SQLite. Take it before any read the decision
    depends on.

    Parameters
    ----------
    session
        Database session.
    artifact_id
        The artifact.

    Returns
    -------
    bool
        `False` if no artifact has this id (it was dropped, or the id is a
        table's).
    """
    result = session.execute(
        update(Table)
        .where(Table.id == artifact_id, Table.table_type == TableType.ARTIFACT)
        .values(name=Table.name)
        .execution_options(synchronize_session=False)
    )
    return rowcount(result) == 1


def stored_hashes(
    session: Session, artifact_id: uuid.UUID, hashes: Iterable[str]
) -> dict[str, int]:
    """Say which of some files are stored for an artifact, and with what size.

    Parameters
    ----------
    session
        Database session.
    artifact_id
        The artifact.
    hashes
        The hashes asked about.

    Returns
    -------
    dict
        Hash to stored size, for those that have an `artifact_blobs` row.
    """
    wanted = sorted(set(hashes))
    found: dict[str, int] = {}
    for start in range(0, len(wanted), IN_CHUNK):
        rows = session.execute(
            select(ArtifactBlob.sha256, ArtifactBlob.size_bytes).where(
                ArtifactBlob.artifact_id == artifact_id,
                ArtifactBlob.sha256.in_(wanted[start : start + IN_CHUNK]),
            )
        )
        found.update({digest: size for digest, size in rows})
    return found


def manifest_size(document: object, sha256: str) -> int | None:
    """Look one file's declared size up in a stored manifest, without validating the rest.

    A blob request needs the size of one file; parsing a manifest of ten thousand
    files for that costs a tenth of a second of CPU on every request. This scans
    for the first entry with the hash and reads only that entry's size.

    Parameters
    ----------
    document
        `manifest_json` of a version or an upload.
    sha256
        The hash asked about, as a caller gave it; untrusted, of any type.

    Returns
    -------
    int or None
        The size of the first entry with this hash, or `None` if there is none.

    Raises
    ------
    ValueError
        If the document is not a mapping with a list of files, or the entry found has a
        size that is not an integer from 0 to 10**20 - 1.
    """
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(files, list):
        raise ValueError("the manifest has no list of files")
    for entry in files:
        if isinstance(entry, dict) and entry.get("sha256") == sha256 and isinstance(sha256, str):
            size = entry.get("size")
            if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size < 10**20:
                raise ValueError("the manifest has a file with no valid size")
            return size
    return None


def checked_hashes(document: object) -> set[str]:
    """Read the hashes a stored manifest names, refusing a document it cannot trust.

    A document that is not a mapping with a non-empty list of
    files, or that has an entry without a lower-case SHA-256 of 64 hex digits, is refused.
    A version's manifest passed `parse_manifest` when it was stored, so one that fails here
    was damaged afterwards; "names nothing" would be the wrong reading of it.

    Parameters
    ----------
    document
        `manifest_json` of a version or an upload.

    Returns
    -------
    set of str
        Every hash the manifest names; each is safe to put in a storage path.

    Raises
    ------
    UnreadableManifest
        If the document is damaged.
    """
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(files, list) or not files:
        raise UnreadableManifest("a stored manifest has no list of files")
    found: set[str] = set()
    for entry in files:
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(digest, str) or SHA256_PATTERN.match(digest) is None:
            raise UnreadableManifest("a stored manifest names a file by something but a SHA-256")
        found.add(digest)
    return found


def unreferenced_hashes(
    session: Session, artifact_id: uuid.UUID, candidates: Iterable[str], *, now: datetime
) -> set[str]:
    """Say which of some files no live version and no open upload of an artifact names.

    The manifests are read from the database a few at a time, and the search stops as soon
    as every candidate has been found named, so the memory used is bounded by the candidates
    and a few manifests, not by every file of every version. The sweep asks the same question
    of the files it finds in storage.

    Parameters
    ----------
    session
        Database session; the caller holds the artifact's lock.
    artifact_id
        The artifact.
    candidates
        The hashes asked about; never changed.
    now
        The time an upload's expiry is compared with.

    Returns
    -------
    set of str
        The candidates nothing names, that is those that may be deleted.

    Raises
    ------
    UnreadableManifest
        If a live version's or unexpired upload's manifest that has to be read before the
        search ends is damaged: it may name any file, so the caller must delete nothing.
    """
    remaining = set(candidates)
    live = select(ArtifactVersion.manifest_json).where(
        ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.deleted_at.is_(None)
    )
    open_uploads = select(ArtifactUpload.manifest_json).where(
        ArtifactUpload.artifact_id == artifact_id,
        ArtifactUpload.expires_at > now,
        ArtifactUpload.committed_version.is_(None),
    )
    for query in (live, open_uploads):
        if not remaining:
            break
        rows = session.execute(query.execution_options(yield_per=_STREAM_ROWS)).scalars()
        try:
            for document in rows:
                remaining -= checked_hashes(document)
                if not remaining:
                    break
        finally:
            rows.close()
    return remaining


def declared_sizes(document: dict) -> dict[str, int]:
    """Read the size each file of a stored manifest declares, for a report.

    Lenient on purpose: a report needs a number for each file `checked_hashes` accepted, and
    a size that is not a whole number counts as zero rather than making a delete fail.

    Parameters
    ----------
    document
        `manifest_json` of a version whose hashes `checked_hashes` accepted.

    Returns
    -------
    dict
        Hash to size in bytes; a file listed under several paths appears once.
    """
    sizes: dict[str, int] = {}
    for entry in document["files"]:
        size = entry.get("size")
        sizes[entry["sha256"]] = size if isinstance(size, int) and not isinstance(size, bool) else 0
    return sizes


def holders_of(
    session: Session, artifact_id: uuid.UUID, hashes: Iterable[str], *, now: datetime
) -> dict[str, int | None]:
    """Say which live version or open upload names each of some files.

    The same streamed search as `unreferenced_hashes`, and it stops as soon as every file has
    been found. Live versions are read newest first, so a file several versions name is
    reported for the newest one; uploads are read only for what no version names.

    Parameters
    ----------
    session
        Database session; the caller holds the artifact's lock.
    artifact_id
        The artifact.
    hashes
        The hashes asked about; never changed.
    now
        The time an upload's expiry is compared with.

    Returns
    -------
    dict
        For each hash that something names: the number of the newest live version that does,
        or `None` when only an open upload does. A hash nothing names is absent.

    Raises
    ------
    UnreadableManifest
        If a manifest that has to be read before the search ends is damaged.
    """
    remaining = set(hashes)
    found: dict[str, int | None] = {}
    live = (
        select(ArtifactVersion.version, ArtifactVersion.manifest_json)
        .where(ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.deleted_at.is_(None))
        .order_by(ArtifactVersion.version.desc())
    )
    open_uploads = select(ArtifactUpload.manifest_json).where(
        ArtifactUpload.artifact_id == artifact_id,
        ArtifactUpload.expires_at > now,
        ArtifactUpload.committed_version.is_(None),
    )
    versions = session.execute(live.execution_options(yield_per=_STREAM_ROWS))
    try:
        for number, document in versions:
            named = checked_hashes(document) & remaining
            found.update(dict.fromkeys(named, number))
            remaining -= named
            if not remaining:
                break
    finally:
        versions.close()
    if remaining:
        uploads = session.execute(open_uploads.execution_options(yield_per=_STREAM_ROWS)).scalars()
        try:
            for document in uploads:
                named = checked_hashes(document) & remaining
                found.update(dict.fromkeys(named, None))
                remaining -= named
                if not remaining:
                    break
        finally:
            uploads.close()
    return found


def _row_of(session: Session, environment: str, dotted: str) -> Table | None:
    """Find a catalog row by its dotted name, without raising.

    Parameters
    ----------
    session
        Database session.
    environment
        Environment name.
    dotted
        A dotted name; untrusted.

    Returns
    -------
    Table or None
        The row, or `None` for a missing or malformed name.
    """
    # Imported here because `lakana_core.catalog` imports this module at load time: a module-level
    # import made `import lakana_core.artifacts.store` fail as the first import of a process
    # (the worker's console script does exactly that).
    from lakana_core.catalog.identity import resolve_table_id

    try:
        ref = TableRef.from_dotted(environment, dotted)
    except ValidationError:
        return None
    table_id = resolve_table_id(session, environment, ref.namespace.joined(), ref.name)
    return None if table_id is None else session.get(Table, table_id)


def kind_of(session: Session, environment: str, dotted: str) -> str | None:
    """Say what a name is in the catalog right now.

    Authorizes nothing and is for the worker only: it answers for any name, readable
    by the caller or not. A caller must have asked the authorization first and must show
    nothing of the answer to a principal that may not read the name.

    Parameters
    ----------
    session
        Database session.
    environment
        Environment name.
    dotted
        A dotted name.

    Returns
    -------
    str or None
        ``TABLE``, ``VIEW``, ``EXTERNAL`` or ``ARTIFACT``; `None` if it does not exist.
    """
    row = _row_of(session, environment, dotted)
    return None if row is None else row.kind


def run_input_version(
    session: Session, environment: str, dotted: str, run_id: uuid.UUID
) -> int | None:
    """Resolve which version of an artifact a task of a run reads.

    Authorizes nothing and is for the worker only: it answers for any name, readable
    by the caller or not. A caller must have asked the authorization first and must show
    nothing of the answer to a principal that may not read the name.

    Parameters
    ----------
    session
        Database session.
    environment
        Environment name.
    dotted
        The artifact's dotted name.
    run_id
        The consumer's run.

    Returns
    -------
    int or None
        The highest live version a task run of `run_id` committed, else the
        current version; `None` if there is no live version or the name is
        not an artifact.
    """
    row = _row_of(session, environment, dotted)
    if row is None or row.table_type is not TableType.ARTIFACT:
        return None
    own = session.execute(
        select(func.max(ArtifactVersion.version)).where(
            ArtifactVersion.artifact_id == row.id,
            ArtifactVersion.deleted_at.is_(None),
            ArtifactVersion.task_run_id.in_(select(TaskRun.id).where(TaskRun.run_id == run_id)),
        )
    ).scalar_one()
    return own if own is not None else current_version(session, row.id)


def attempt_version(
    session: Session, environment: str, dotted: str, task_run_id: uuid.UUID, claim: int
) -> int | None:
    """Find the version one task attempt committed: the worker's evidence of success.

    A version row of exactly this task run and claim counts whether or not it has been deleted
    since: an overlapping run's `keep_versions` may prune it between its commit and
    the worker's check, and the attempt did commit it. Only a row that never existed, or one of
    another task run or claim, is no evidence. `run_input_version`, which tells a consumer what to
    read, still skips deleted versions.

    Authorizes nothing and is for the worker only: it answers for any name, readable
    by the caller or not. A caller must have asked the authorization first and must show
    nothing of the answer to a principal that may not read the name.

    Parameters
    ----------
    session
        Database session.
    environment
        Environment name.
    dotted
        The artifact's dotted name.
    task_run_id
        The task run.
    claim
        The attempt's claim counter.

    Returns
    -------
    int or None
        The highest version, deleted or not, with exactly this task run and claim, or `None`.
    """
    row = _row_of(session, environment, dotted)
    if row is None or row.table_type is not TableType.ARTIFACT:
        return None
    return session.execute(
        select(func.max(ArtifactVersion.version)).where(
            ArtifactVersion.artifact_id == row.id,
            ArtifactVersion.task_run_id == task_run_id,
            ArtifactVersion.claim == claim,
        )
    ).scalar_one()
