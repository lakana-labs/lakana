"""The artifact sweep: remove what a crash or an abandoned upload left behind.

Run in a thread of its own by the worker that holds the scheduler role. It deletes bytes
unattended, so the one rule is: **bytes that a live version or an upload in progress still names
are never deleted, and the database never says a file is stored when its bytes are gone.** A file
that has no `artifact_blobs` row is a different matter, because a database restored from an
older backup, a failover that lost recent commits, or a fresh database pointed at an existing
bucket leaves storage that the catalog has forgotten. Such a file is deleted only after
`ORPHAN_BLOB_GRACE` or when a deleted version's manifest names it, and never while a manifest
object the catalog does not know, a live version's manifest or an unexpired upload's manifest
names it; a manifest object of an unknown version and a directory without a row and marker are
never deleted. When in doubt, the sweep leaves a thing alone.

What it removes, for an artifact that has a catalog row, each decision under the artifact's lock
(`lock_artifact`) and with the one ``now`` the run was given:

1. upload rows that have expired;
2. `artifact_blobs` rows whose hash no live version, no unexpired upload and no manifest object
   the database does not know names, and then, one file at a time, their bytes (rows first,
   bytes second, as `ArtifactService._retire` and `_release` do);
3. objects under ``blobs/`` that have no row, **and have a reason to go**: the hash is named by
   the manifest of a *deleted* version of this artifact (a release that was cut short), or the
   object is older than `ORPHAN_BLOB_GRACE` (a file stored by an upload that never committed and
   expired), and no live version and no unexpired upload names it, whatever its record says (one
   check per batch of such files; the files kept are counted in `named_files_kept` and logged
   once per artifact). An object with no modification time is left alone. `BlobUpload.finish`
   makes an object visible and inserts its row under the lock, so an object without a row that is
   seen *under the lock* is not a file being stored; seen outside it, it may be. The sweep lists
   without a lock, checks rows for a batch of names in one query, and only then takes the lock
   for each candidate, reads the row again, deletes that one object and lets the lock go;
4. temporary ``.part`` objects older than the upload lifetime (never less than an hour): an
   upload in progress writes them, so only age makes one garbage (a file's modification time is
   its last write, which is never before its upload began, so an old one belongs to an expired
   upload). Only the local backend has them: on S3 the writer uploads in parts to the final key,
   and an incomplete multipart upload is invisible to a listing, so the bucket needs a lifecycle
   rule that aborts incomplete multipart uploads;
5. ``manifests/<n>.json`` objects of a version whose row has ``deleted_at`` set (a release that
   failed or was cut short by its budget). The database row decides, never the object. An object
   whose number is *greater than the highest version the database knows* is never deleted: it
   is a commit in flight (a commit writes it inside its transaction, under the artifact's lock,
   before the row is committed, and the sweep decides under that same lock), a failed commit's
   leftover (a number storage has seen is never reused, so the next commit takes a
   higher one and the object stays), or a version the database lost. Its
   files are protected too: before the sweep removes a row or an object of an artifact it reads
   those manifest objects (at most `MAX_UNKNOWN_MANIFESTS`, each at most 16 MiB) and treats every
   hash they name as referenced; more of them, or one that cannot be read, leaves the artifact
   alone for this run. An object at or below the highest known number with no version row at all
   is left alone too: a crashed commit followed by a successful one leaves exactly that.

Anything else under an artifact's directory is left alone and counted.

A prefix ``artifacts/<id>/`` whose id has no catalog row is removed only if it carries the marker
`ArtifactService.drop_artifact` writes first (``.dropped``, under the artifact's lock, before the
row is deleted) and deletes last, so an interrupted removal is resumed. The id is a random UUID
that is never reused, and a new artifact's row is created before its first object is written, so a
marked prefix without a row is a dropped artifact's; each object is still deleted only after a
lock on the id found no row. A rowless prefix without a marker is left alone, counted
(`unmarked_prefixes`) and reported once per environment and run: it may be an artifact a restored
database forgot. A marker next to an existing row (a drop whose commit failed) is left in place
and ignored; the next drop of that artifact overwrites it. Ids are read strictly (32 lower-case
hex digits, a directory); anything under ``artifacts/`` that does not parse is left alone and
counted, and the sweep never deletes outside ``artifacts/<id>/``. The storage layer refuses to
create a table inside the root's ``artifacts/`` directory, so no table's data can be taken for
an artifact's.

A manifest of a live version or open upload that cannot be read may name any file. For that
artifact the sweep deletes nothing (no row, no object, no manifest object), counts it, and
logs one line with the artifact's name.

Bounds. The sweep runs off the scheduler's thread: a hung object-store call or a wait for an
artifact's lock stalls nothing but itself. One run has a deadline (`max_seconds`) shared between
the environments (each gets the time that is left divided by the environments not yet visited,
and the starting environment and artifact rotate from run to run), and ends at the next decision
once it is past it or once `should_stop` says so (shutdown). The next run starts again, from the
next artifact in the rotation; since what was deleted does not come back, a run that did some
work leaves less for the next one. It does not resume inside an artifact: see the last known
limit below. Prefixes without a row get half of an
environment's time. A storage or database error for one artifact is logged and counted and the
others go on; after `MAX_CONSECUTIVE_FAILURES` in a row the environment is given up for this
run. Listings are iterated, never collected: `Storage.list_objects` for one artifact at a time,
`Storage.list_children` one level of ``artifacts/``. Both ask the backend for current state
(`FsspecStorage` drops fsspec's directory cache for the listed path first, and a listing that
is stale anyway can only name objects already gone or miss new ones, because every delete is
decided again against the database under the lock).

Known limits:

- A storage delete in flight can outlive the artifact lock when the server ends the database
  session (a timeout or a failover) during a slow delete.
- Each run starts an artifact's row pass from its beginning, and the run's time share per
  artifact is fixed, so an artifact whose row pass does not fit it (roughly 200,000 distinct
  files in one artifact) never reaches its later rows or its object pass, however often the
  sweep runs. Rotating the starting artifact makes the others fair, not this one finish.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from lakana_core.artifacts.manifest import SHA256_PATTERN
from lakana_core.artifacts.store import (
    DROPPED_MARKER,
    IN_CHUNK,
    UnreadableManifest,
    artifact_prefix,
    blob_path,
    checked_hashes,
    delete_quietly,
    dropped_marker_path,
    lock_artifact,
    manifest_number,
    stored_hashes,
    stored_manifests,
    unreferenced_hashes,
)
from lakana_core.db import rowcount
from lakana_core.errors import NotFoundError
from lakana_core.identifiers import NAMESPACE_SEPARATOR, shown
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    ArtifactBlob,
    ArtifactUpload,
    ArtifactVersion,
    Environment,
    Namespace,
    Table,
    TableType,
)
from lakana_core.storage.base import PART_SUFFIX, Storage, StoredObject
from lakana_core.storage.resolver import ARTIFACTS_PREFIX, StorageResolver

logger = logging.getLogger(__name__)

DEFAULT_MAX_SECONDS = 300.0
"""Default length of one run; shutdown ends it earlier."""
MAX_CONSECUTIVE_FAILURES = 5
"""Artifacts that fail one after the other before an environment is given up for the run."""
ORPHAN_BLOB_GRACE = timedelta(days=7)
"""How old an object without a row must be, by its modification time, to need no other reason."""
MIN_PART_AGE_SECONDS = 3600
"""A temporary object is never judged abandoned before it is this old."""
MAX_UNKNOWN_MANIFESTS = 20
"""Manifest objects the database does not know that are read per artifact and run."""

_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_ARTIFACT_ID = re.compile(r"\A[0-9a-f]{32}\Z")
_ROW_BATCH = 500
"""`artifact_blobs` rows looked at per locked step."""
FILE_BATCH = IN_CHUNK
"""Listed file names whose rows are checked in one query: one `IN` list of the store."""
_PROBE = "a hash no manifest names"
"""Not a SHA-256, so no manifest names it and a search for it reads every manifest."""

UNMARKED_MESSAGE = (
    "%d artifact directories in environment %s have no catalog entry and no drop marker; they are "
    "left alone. If the database was restored from a backup or lost recent changes, the files "
    'are intact. See "After a restore" in the documentation before removing anything.'
)
UNKNOWN_VERSIONS_MESSAGE = (
    "The artifact sweep found manifest objects for %s of artifact %s that the catalog "
    "does not know; the files they name are kept. Either a commit failed part-way (then this is "
    "harmless and the number stays unused), or the database was restored from a backup or lost "
    "recent changes (then those versions are missing from the catalog but their files are "
    'intact). See "After a restore" in the documentation.'
)
KEPT_MESSAGE = (
    "The artifact sweep left %s whole for this run: manifest objects the catalog does not know "
    "could not all be read, or there are more than %d of them; nothing of this artifact is "
    'removed until that is resolved. See "After a restore" in the documentation.'
)
NAMED_WITHOUT_RECORD_MESSAGE = (
    "%d files of artifact %s are named by a version or an upload but have no record; kept. The "
    'catalog should have a record for each of them: see "After a restore" in the documentation.'
)
_RANGES_SHOWN = 5


def _versions(numbers: list[int]) -> str:
    """Name version numbers for a message: ``version 3`` or ``versions 3..5, 8``.

    Parameters
    ----------
    numbers
        The version numbers, ascending.

    Returns
    -------
    str
        The noun in the right number, then `_ranges`.
    """
    return f"{'version' if len(numbers) == 1 else 'versions'} {_ranges(numbers)}"


def _ranges(numbers: list[int]) -> str:
    """Write version numbers as ranges, a handful of them, then a count.

    Parameters
    ----------
    numbers
        The version numbers, ascending.

    Returns
    -------
    str
        For example ``2..4, 7`` or ``1, 3, 5, 7, 9 and 12 more``.
    """
    runs: list[list[int]] = []
    for number in numbers:
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    text = ", ".join(str(a) if a == b else f"{a}..{b}" for a, b in runs[:_RANGES_SHOWN])
    hidden = sum(b - a + 1 for a, b in runs[_RANGES_SHOWN:])
    return f"{text} and {hidden} more" if hidden else text


@dataclass(frozen=True)
class SweepReport:
    """What one sweep did.

    Parameters
    ----------
    uploads_expired
        Upload rows removed because they expired.
    blobs_deleted
        Files removed from ``blobs/`` that no `artifact_blobs` row names.
    parts_deleted
        Stale temporary files removed.
    left_alone
        Objects (and entries of ``artifacts/``) that are none of these; never touched.
    failed_environments
        Environments skipped because their storage or database failed, or because
        `MAX_CONSECUTIVE_FAILURES` artifacts failed in a row.
    blob_rows_removed
        `artifact_blobs` rows removed because nothing names their hash.
    manifests_deleted
        ``manifests/<n>.json`` objects removed because their version is deleted.
    orphan_objects_deleted
        Objects removed from marked prefixes whose artifact has no catalog row (the marker
        included).
    damaged_artifacts
        Artifacts left whole because a manifest of a live version or open upload is unreadable.
    failed_artifacts
        Artifacts skipped because a storage or database error stopped their sweep.
    stopped_early
        Whether the run ended at its deadline or at shutdown with artifacts or objects not yet
        examined.
    unmarked_prefixes
        Directories under ``artifacts/`` with no catalog row and no drop marker; left alone.
    kept_artifacts
        Artifacts left whole for this run because manifest objects the database does not know
        could not all be read, or there were more than `MAX_UNKNOWN_MANIFESTS`.
    stale_markers_removed
        Drop markers removed from artifacts that still have their row.
    named_files_kept
        Files with no record, and a reason to go, that a live version or an open upload names;
        kept.
    """

    uploads_expired: int = 0
    blobs_deleted: int = 0
    parts_deleted: int = 0
    left_alone: int = 0
    failed_environments: int = 0
    blob_rows_removed: int = 0
    manifests_deleted: int = 0
    orphan_objects_deleted: int = 0
    damaged_artifacts: int = 0
    failed_artifacts: int = 0
    stopped_early: bool = False
    unmarked_prefixes: int = 0
    kept_artifacts: int = 0
    stale_markers_removed: int = 0
    named_files_kept: int = 0


_COUNTED = [field.name for field in fields(SweepReport) if field.type == "int"]


class _Spent(Exception):
    """The run's deadline has passed, or shutdown was asked for."""


class _GiveUp(Exception):
    """Too many artifacts of one environment failed in a row."""


class _Damaged(Exception):
    """A manifest that decides what may be deleted cannot be read."""


class _Kept(Exception):
    """Manifest objects the database does not know could not all be read."""


class _Run:
    """The state of one sweep over one environment's storage.

    Parameters
    ----------
    session
        Database session; every step commits or rolls back, nothing stays open.
    storage
        The environment's storage.
    environment
        The environment's name, for log lines.
    now
        The run's time; every expiry and age is compared with it.
    ttl
        The upload lifetime in seconds.
    monotonic
        The clock the deadline is read from.
    deadline
        The monotonic time after which no further decision is taken.
    should_stop
        Whether shutdown was asked for.
    """

    def __init__(
        self,
        session: Session,
        storage: Storage,
        environment: str,
        now: datetime,
        ttl: int,
        monotonic: Callable[[], float],
        deadline: float,
        should_stop: Callable[[], bool],
    ) -> None:
        self.counts: dict[str, int] = dict.fromkeys(_COUNTED, 0)
        self.stopped_early = False
        self.deadline = deadline
        self._session = session
        self._storage = storage
        self._environment = environment
        self._now = now
        self._part_age = timedelta(seconds=max(ttl, MIN_PART_AGE_SECONDS))
        self._monotonic = monotonic
        self._should_stop = should_stop
        self._failures = 0
        self._checked: uuid.UUID | None = None
        self._protected: set[str] = set()
        self._named_kept = 0

    def _tick(self) -> None:
        """Stop the run if its deadline has passed or shutdown was asked for.

        Raises
        ------
        _Spent
            When it has.
        """
        if self._should_stop() or self._monotonic() >= self.deadline:
            self.stopped_early = True
            raise _Spent

    def _label(self, artifact_id: uuid.UUID) -> str:
        """Name an artifact for a log line.

        Parameters
        ----------
        artifact_id
            The artifact.

        Returns
        -------
        str
            ``namespace.name`` as quoted text, or the id when the row is gone.
        """
        found = self._session.execute(
            select(Namespace.path, Table.name)
            .join(Namespace, Table.namespace_id == Namespace.id)
            .where(Table.id == artifact_id)
        ).one_or_none()
        self._session.rollback()
        if found is None:
            return artifact_id.hex
        return repr(f"{found[0].replace(NAMESPACE_SEPARATOR, '.')}.{found[1]}")

    def _ensure_readable(self, artifact_id: uuid.UUID) -> None:
        """Refuse to delete anything of an artifact whose manifests cannot all be read.

        Reads every manifest of the artifact's live versions and unexpired uploads once per
        run (the result is kept for the artifact), only when something is about to be deleted.

        Parameters
        ----------
        artifact_id
            The artifact.

        Raises
        ------
        _Damaged
            If a manifest is damaged.
        """
        if self._checked == artifact_id:
            return
        try:
            unreferenced_hashes(self._session, artifact_id, {_PROBE}, now=self._now)
        except UnreadableManifest:
            raise _Damaged from None
        finally:
            self._session.rollback()
        self._checked = artifact_id

    def artifact(self, artifact_id: uuid.UUID) -> None:
        """Sweep one artifact that has a catalog row; failures are counted, not raised.

        Parameters
        ----------
        artifact_id
            The artifact.

        Raises
        ------
        _Spent
            When the deadline passed.
        _GiveUp
            After too many failures in a row.
        """
        self._guarded(artifact_id, self._live)

    def prefix(self, artifact_id: uuid.UUID) -> None:
        """Sweep a prefix whose id had no catalog row when it was listed.

        Parameters
        ----------
        artifact_id
            The id found under ``artifacts/``.

        Raises
        ------
        _Spent
            When the deadline passed.
        _GiveUp
            After too many failures in a row.
        """
        self._guarded(artifact_id, self._orphan)

    def _guarded(self, artifact_id: uuid.UUID, work: Callable[[uuid.UUID], None]) -> None:
        """Run one artifact's work, turning its failures into counts.

        Parameters
        ----------
        artifact_id
            The artifact.
        work
            What to do with it.

        Raises
        ------
        _Spent
            When the deadline passed.
        _GiveUp
            After `MAX_CONSECUTIVE_FAILURES` failures in a row.
        """
        self._checked = None
        self._protected = set()
        self._named_kept = 0
        try:
            work(artifact_id)
            self._failures = 0
        except _Spent:
            raise
        except _Damaged:
            self._failures = 0
            self.counts["damaged_artifacts"] += 1
            logger.warning(
                "The artifact sweep left %s whole: a stored manifest of a live version or open "
                "upload is damaged. Delete the damaged version, or let the upload expire; "
                "the sweep then removes what nothing names.",
                self._label(artifact_id),
            )
        except _Kept:
            self._failures = 0
            self.counts["kept_artifacts"] += 1
            logger.warning(
                KEPT_MESSAGE,
                self._label(artifact_id),
                MAX_UNKNOWN_MANIFESTS,
            )
        except Exception as exc:
            self._session.rollback()
            self.counts["failed_artifacts"] += 1
            self._failures += 1
            logger.warning(
                "The artifact sweep skipped artifact %s (%s); the next sweep tries again.",
                self._label(artifact_id),
                type(exc).__name__,
            )
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                raise _GiveUp from None
        finally:
            self._session.rollback()

    def _live(self, artifact_id: uuid.UUID) -> None:
        """Sweep an artifact that has a row: unknown manifests, uploads, rows, then objects.

        Parameters
        ----------
        artifact_id
            The artifact.
        """
        self._load_unknown_manifests(artifact_id)
        self._expire_uploads(artifact_id)
        self._release_unneeded_rows(artifact_id)
        self._sweep_objects(artifact_id)

    def _load_unknown_manifests(self, artifact_id: uuid.UUID) -> None:
        """Read the manifest objects of unknown versions and protect the files they name.

        Parameters
        ----------
        artifact_id
            The artifact.

        Raises
        ------
        _Kept
            If there are more than `MAX_UNKNOWN_MANIFESTS`, or one cannot be read or parsed.
        """
        known = set(
            self._session.scalars(
                select(ArtifactVersion.version).where(ArtifactVersion.artifact_id == artifact_id)
            )
        )
        self._session.rollback()
        unknown = [
            (number, found)
            for number, found in stored_manifests(self._storage, artifact_id)
            if number not in known
        ]
        if unknown:
            # Listed after the rows were read, with no lock: a commit that landed in between
            # shows an object without a known row. Under the lock the rows decide.
            self._tick()
            if not lock_artifact(self._session, artifact_id):
                self._session.rollback()
                raise _Kept  # dropped meanwhile: leave whatever is there for a later run
            known = set(
                self._session.scalars(
                    select(ArtifactVersion.version).where(
                        ArtifactVersion.artifact_id == artifact_id
                    )
                )
            )
            self._session.rollback()
            unknown = [(number, found) for number, found in unknown if number not in known]
        if len(unknown) > MAX_UNKNOWN_MANIFESTS:
            raise _Kept
        unknown.sort(key=lambda item: item[0])
        for _, found in unknown:
            if found.size > _MAX_MANIFEST_BYTES:
                raise _Kept
            try:
                self._protected |= checked_hashes(json.loads(self._storage.read(found.path)))
            except NotFoundError:
                continue  # gone since it was listed: a commit that removed its own
            except (ValueError, UnreadableManifest):
                raise _Kept from None
        if unknown:
            logger.warning(
                UNKNOWN_VERSIONS_MESSAGE,
                _versions([number for number, _ in unknown]),
                self._label(artifact_id),
            )

    def _expire_uploads(self, artifact_id: uuid.UUID) -> None:
        """Remove the artifact's expired upload rows, under its lock.

        Parameters
        ----------
        artifact_id
            The artifact.
        """
        expired = self._session.execute(
            select(func.count())
            .select_from(ArtifactUpload)
            .where(
                ArtifactUpload.artifact_id == artifact_id, ArtifactUpload.expires_at <= self._now
            )
        ).scalar_one()
        self._session.rollback()
        if not expired:
            return
        self._tick()
        self._ensure_readable(artifact_id)
        if lock_artifact(self._session, artifact_id):
            removed = rowcount(
                self._session.execute(
                    delete(ArtifactUpload)
                    .where(
                        ArtifactUpload.artifact_id == artifact_id,
                        ArtifactUpload.expires_at <= self._now,
                    )
                    .execution_options(synchronize_session=False)
                )
            )
            self._session.commit()
            self.counts["uploads_expired"] += removed
        self._session.rollback()

    def _release_unneeded_rows(self, artifact_id: uuid.UUID) -> None:
        """Remove `artifact_blobs` rows nothing names, a batch at a time, then their bytes.

        Parameters
        ----------
        artifact_id
            The artifact.

        Raises
        ------
        _Damaged
            If a manifest that had to be read is damaged.
        """
        cursor = ""
        while True:
            self._tick()
            if not lock_artifact(self._session, artifact_id):
                self._session.rollback()
                return
            batch = list(
                self._session.scalars(
                    select(ArtifactBlob.sha256)
                    .where(ArtifactBlob.artifact_id == artifact_id, ArtifactBlob.sha256 > cursor)
                    .order_by(ArtifactBlob.sha256)
                    .limit(_ROW_BATCH)
                )
            )
            if not batch:
                self._session.rollback()
                return
            cursor = batch[-1]
            try:
                free = unreferenced_hashes(self._session, artifact_id, batch, now=self._now)
            except UnreadableManifest:
                raise _Damaged from None
            free -= self._protected
            if free:
                self._session.execute(
                    delete(ArtifactBlob)
                    .where(ArtifactBlob.artifact_id == artifact_id, ArtifactBlob.sha256.in_(free))
                    .execution_options(synchronize_session=False)
                )
                self._session.commit()
                self.counts["blob_rows_removed"] += len(free)
            self._session.rollback()
            for digest in sorted(free):
                self._tick()
                if self._delete_blob(artifact_id, digest):
                    self.counts["blobs_deleted"] += 1
            if len(batch) < _ROW_BATCH:
                return

    def _delete_blob(self, artifact_id: uuid.UUID, digest: str) -> bool:
        """Delete one file's object if, under the lock, it still has no row.

        Parameters
        ----------
        artifact_id
            The artifact.
        digest
            The file's hash.

        Returns
        -------
        bool
            Whether an object was deleted.
        """
        try:
            if lock_artifact(self._session, artifact_id) and stored_hashes(
                self._session, artifact_id, [digest]
            ):
                return False
            return delete_quietly(self._storage, blob_path(artifact_id, digest))
        finally:
            self._session.rollback()

    @staticmethod
    def _inside(prefix: str, found: StoredObject) -> str | None:
        """Return an object's path relative to an artifact's directory, if it is safely inside.

        Parameters
        ----------
        prefix
            ``artifacts/<id>``.
        found
            An object the backend listed.

        Returns
        -------
        str or None
            The path below ``prefix/``, or `None` when the path is outside it or has an empty,
            ``.`` or ``..`` segment.
        """
        path = found.path
        if not path.startswith(prefix + "/"):
            return None
        rest = path[len(prefix) + 1 :]
        if any(part in {"", ".", ".."} for part in rest.split("/")):
            return None
        return rest

    def _sweep_objects(self, artifact_id: uuid.UUID) -> None:
        """Walk the artifact's objects and decide each one.

        Parameters
        ----------
        artifact_id
            The artifact.
        """
        prefix = artifact_prefix(artifact_id)
        pending: list[StoredObject] = []
        try:
            for found in self._storage.list_objects(prefix):
                self._tick()
                rest = self._inside(prefix, found)
                manifest = None if rest is None else manifest_number(rest)
                if rest is None:
                    self.counts["left_alone"] += 1
                elif rest.startswith("blobs/") and "/" not in rest[6:]:
                    name = rest[6:]
                    if SHA256_PATTERN.match(name):
                        pending.append(found)
                        if len(pending) >= FILE_BATCH:
                            self._files(artifact_id, pending)
                            pending = []
                    elif name.startswith(".") and name.endswith(PART_SUFFIX):
                        self._stale_part(artifact_id, found)
                    else:
                        self.counts["left_alone"] += 1
                elif manifest is not None:
                    self._manifest_object(artifact_id, found, manifest)
                elif rest == DROPPED_MARKER:
                    self._stale_marker(artifact_id, found)
                elif (
                    rest.startswith("manifests/")
                    and "/" not in rest[10:]
                    and rest[10:].startswith(".")
                    and rest.endswith(PART_SUFFIX)
                ):
                    self._stale_part(artifact_id, found)  # a manifest write that never finished
                else:
                    self.counts["left_alone"] += 1
            if pending:
                self._files(artifact_id, pending)
        finally:
            # Also when the run stops at its deadline or at shutdown inside the artifact.
            if self._named_kept:
                logger.warning(
                    NAMED_WITHOUT_RECORD_MESSAGE, self._named_kept, self._label(artifact_id)
                )

    def _named_by_deleted(self, artifact_id: uuid.UUID, hashes: set[str]) -> set[str]:
        """Say which of some files the manifest of a deleted version of the artifact names.

        Parameters
        ----------
        artifact_id
            The artifact.
        hashes
            The candidates.

        Returns
        -------
        set of str
            Those a deleted version's stored manifest names. A damaged manifest names nothing
            here, so its files are never made eligible by it.
        """
        remaining = set(hashes)
        found: set[str] = set()
        rows = self._session.execute(
            select(ArtifactVersion.manifest_json)
            .where(
                ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.deleted_at.is_not(None)
            )
            .execution_options(yield_per=4)
        ).scalars()
        try:
            for document in rows:
                try:
                    named = checked_hashes(document) & remaining
                except UnreadableManifest:
                    continue
                found |= named
                remaining -= named
                if not remaining:
                    break
        finally:
            rows.close()
            self._session.rollback()
        return found

    def _files(self, artifact_id: uuid.UUID, listed: list[StoredObject]) -> None:
        """Decide a batch of hash-named objects: those with no row need a reason to go.

        Parameters
        ----------
        artifact_id
            The artifact.
        listed
            Objects of ``blobs/`` named by a SHA-256.
        """
        names = {found.path.rsplit("/", 1)[1]: found for found in listed}
        have = stored_hashes(self._session, artifact_id, names)
        self._session.rollback()
        candidates = {
            name: found
            for name, found in names.items()
            if name not in have and name not in self._protected
        }
        if not candidates:
            return
        cutoff = self._now - ORPHAN_BLOB_GRACE
        young = {
            name
            for name, found in candidates.items()
            if found.modified_at is None or found.modified_at > cutoff
        }
        named = self._named_by_deleted(artifact_id, young) if young else set()
        doomed: dict[str, StoredObject] = {}
        for name, found in candidates.items():
            self._tick()
            if name in young and name not in named:
                if found.modified_at is None:
                    self.counts["left_alone"] += 1
                continue
            doomed[name] = found
        for name, found in self._not_named(artifact_id, doomed).items():
            self._tick()
            self._garbage_blob(artifact_id, found.path, name)

    def _not_named(
        self, artifact_id: uuid.UUID, doomed: dict[str, StoredObject]
    ) -> dict[str, StoredObject]:
        """Drop from a batch of files about to be deleted those a version or upload names.

        A second line of defence: from a consistent catalog a live version's file has a row, but
        a record removed by hand or lost must not cost the file. One pass over the live
        manifests and open uploads for the whole batch, never one per file.

        Parameters
        ----------
        artifact_id
            The artifact.
        doomed
            Object name (a SHA-256) to listed object, each judged deletable by age or by a
            deleted version.

        Returns
        -------
        dict
            The part of `doomed` that nothing names; the rest is counted and logged.

        Raises
        ------
        _Damaged
            If a manifest is damaged.
        """
        if not doomed:
            return doomed
        self._ensure_readable(artifact_id)
        try:
            lock_artifact(self._session, artifact_id)
            try:
                free = unreferenced_hashes(self._session, artifact_id, set(doomed), now=self._now)
            except UnreadableManifest:  # damaged since the readability check was cached
                raise _Damaged from None
            named = set(doomed) - free
            # A file that has its row by now (a blob arrived since the listing) is no lost record.
            lost = named - set(stored_hashes(self._session, artifact_id, named))
        finally:
            self._session.rollback()
        self.counts["named_files_kept"] += len(lost)
        self._named_kept += len(lost)
        return {name: found for name, found in doomed.items() if name in free}

    def _garbage_blob(self, artifact_id: uuid.UUID, path: str, digest: str) -> None:
        """Delete a hash-named object that has no row and a reason to go, deciding under the lock.

        Parameters
        ----------
        artifact_id
            The artifact.
        path
            The object's path, as listed.
        digest
            Its name, a SHA-256.
        """
        self._ensure_readable(artifact_id)
        try:
            if lock_artifact(self._session, artifact_id) and stored_hashes(
                self._session, artifact_id, [digest]
            ):
                return
            if delete_quietly(self._storage, path):
                self.counts["blobs_deleted"] += 1
        finally:
            self._session.rollback()

    def _stale_part(self, artifact_id: uuid.UUID, found: StoredObject) -> None:
        """Delete a temporary file nobody has written to for the upload lifetime.

        Parameters
        ----------
        artifact_id
            The artifact.
        found
            The listed object.
        """
        written = found.modified_at
        if written is None:
            self.counts["left_alone"] += 1
        elif written <= self._now - self._part_age:
            self._ensure_readable(artifact_id)
            if delete_quietly(self._storage, found.path):
                self.counts["parts_deleted"] += 1

    def _manifest_object(self, artifact_id: uuid.UUID, found: StoredObject, number: int) -> None:
        """Decide a manifest object: only a deleted version's goes, deciding under the lock.

        Parameters
        ----------
        artifact_id
            The artifact.
        found
            The listed object.
        number
            The version number in its name.
        """
        state = self._version_state(artifact_id, number)
        if not state:
            # A live version's, or none at all: a commit in flight, a failed commit's leftover
            # or a version the catalog lost. Never deleted; its files are protected and the
            # unknown ones were reported when the artifact was loaded.
            return
        self._ensure_readable(artifact_id)
        try:
            lock_artifact(self._session, artifact_id)
            if not self._version_state(artifact_id, number):
                return
            if delete_quietly(self._storage, found.path):
                self.counts["manifests_deleted"] += 1
        finally:
            self._session.rollback()

    def _stale_marker(self, artifact_id: uuid.UUID, found: StoredObject) -> None:
        """Remove a drop marker next to an artifact that still has its row.

        The marker is written, and the row deleted, in one locked transaction of a drop. Seen
        under the lock with the row present, no drop is in flight: the drop failed or a restore
        brought the row back. Left in place, the marker would make the directory deletable if the
        row were ever lost without a drop.

        Parameters
        ----------
        artifact_id
            The artifact.
        found
            The listed marker.
        """
        try:
            if lock_artifact(self._session, artifact_id) and delete_quietly(
                self._storage, found.path
            ):
                self.counts["stale_markers_removed"] += 1
        finally:
            self._session.rollback()

    def _version_state(self, artifact_id: uuid.UUID, number: int) -> bool | None:
        """Say whether a version row exists and is deleted.

        Parameters
        ----------
        artifact_id
            The artifact.
        number
            The version number.

        Returns
        -------
        bool or None
            `None` with no row at all, `True` for a deleted version, `False` for a live one.
            The transaction is rolled back.
        """
        found = self._session.execute(
            select(ArtifactVersion.deleted_at).where(
                ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.version == number
            )
        ).first()
        self._session.rollback()
        return None if found is None else found[0] is not None

    def _orphan(self, artifact_id: uuid.UUID) -> None:
        """Remove everything under a marked prefix whose artifact has no row, marker last.

        Parameters
        ----------
        artifact_id
            The id found under ``artifacts/``.
        """
        marker = dropped_marker_path(artifact_id)
        if not self._storage.exists(marker):
            # The ids were read before the listing: an artifact created since has a row now and
            # is not a forgotten one.
            try:
                created_since = lock_artifact(self._session, artifact_id)
            finally:
                self._session.rollback()
            if not created_since:
                self.counts["unmarked_prefixes"] += 1
            return
        prefix = artifact_prefix(artifact_id)
        for found in self._storage.list_objects(prefix):
            self._tick()
            if found.path == marker:
                continue
            if self._inside(prefix, found) is None:
                self.counts["left_alone"] += 1
                continue
            try:
                if lock_artifact(self._session, artifact_id):
                    return  # the row exists now: not a dropped artifact's
                if delete_quietly(self._storage, found.path):
                    self.counts["orphan_objects_deleted"] += 1
            finally:
                self._session.rollback()
        self._tick()
        try:
            if not lock_artifact(self._session, artifact_id) and delete_quietly(
                self._storage, marker
            ):
                self.counts["orphan_objects_deleted"] += 1
        finally:
            self._session.rollback()

    def environment(self, environment_id: uuid.UUID, rotate: int) -> None:
        """Sweep one environment: prefixes without a row first, then the artifacts.

        Parameters
        ----------
        environment_id
            The environment.
        rotate
            Where in the artifacts' order to start.

        Raises
        ------
        _Spent
            When the deadline passed.
        _GiveUp
            After too many failures in a row.
        """
        ids = sorted(
            self._session.scalars(
                select(Table.id)
                .join(Namespace, Table.namespace_id == Namespace.id)
                .where(
                    Namespace.environment_id == environment_id,
                    Table.table_type == TableType.ARTIFACT,
                )
            )
        )
        self._session.rollback()
        known = set(ids)
        final = self.deadline
        started = self._monotonic()
        self.deadline = started + (final - started) / 2
        unmarked = self.counts["unmarked_prefixes"]
        try:
            for name, is_directory in self._storage.list_children(ARTIFACTS_PREFIX):
                if not is_directory or _ARTIFACT_ID.match(name) is None:
                    self.counts["left_alone"] += 1
                elif (found := uuid.UUID(name)) not in known:
                    self.prefix(found)
        except _Spent:
            pass
        finally:
            self.deadline = final
        if self.counts["unmarked_prefixes"] > unmarked:
            logger.warning(
                UNMARKED_MESSAGE,
                self.counts["unmarked_prefixes"] - unmarked,
                shown(self._environment),
            )
        if ids:
            start = rotate % len(ids)
            for artifact_id in ids[start:] + ids[:start]:
                self._tick()
                self.artifact(artifact_id)


def sweep_artifacts(
    session: Session,
    storage: StorageResolver,
    *,
    now: datetime,
    upload_ttl_seconds: int,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    monotonic: Callable[[], float] | None = None,
    rotation: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> SweepReport:
    """Sweep every environment's artifact store once.

    Parameters
    ----------
    session
        Database session; the sweep commits its own transactions on it and leaves it without
        one open.
    storage
        Resolves each environment's storage.
    now
        The sweep's time (the database's clock in production), used for the whole run.
    upload_ttl_seconds
        `LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS`: how long a temporary file may sit unwritten.
    max_seconds
        How long the run may take, shared between the environments, before it stops at the
        next decision.
    monotonic
        The clock the deadline is read from; `time.monotonic` when `None`.
    rotation
        A number that grows by one with each run of the same process; it moves the starting
        environment and artifact so that no one's backlog always comes first.
    should_stop
        Called before every decision; the run ends when it returns `True` (shutdown).

    Returns
    -------
    SweepReport
        What was removed. An artifact that failed, or whose manifest is damaged, is counted and
        skipped; an environment whose storage cannot be reached is counted and skipped; the
        others are still swept.
    """
    clock = monotonic or time.monotonic
    stop = should_stop or (lambda: False)
    final = clock() + max_seconds
    totals = dict.fromkeys(_COUNTED, 0)
    stopped = False
    environments = list(
        session.execute(
            select(Environment.id, Environment.name)
            .where(LIVE_ENVIRONMENT)
            .order_by(Environment.name)
        ).all()
    )
    session.rollback()
    if environments:
        start = rotation % len(environments)
        environments = environments[start:] + environments[:start]
    for position, (environment_id, name) in enumerate(environments):
        moment = clock()
        if stop() or moment >= final:
            stopped = True
            break
        share = moment + (final - moment) / (len(environments) - position)
        try:
            backend = storage.for_environment(name).storage
            run = _Run(session, backend, name, now, upload_ttl_seconds, clock, share, stop)
            try:
                run.environment(environment_id, rotation)
            finally:
                for key, value in run.counts.items():
                    totals[key] += value
                stopped = stopped or run.stopped_early
        except _Spent:
            continue
        except Exception as exc:
            totals["failed_environments"] += 1
            session.rollback()
            logger.warning(
                "The artifact sweep skipped environment %s (%s).",
                shown(name),
                type(exc).__name__ if not isinstance(exc, _GiveUp) else "repeated failures",
            )
    report = SweepReport(**totals, stopped_early=stopped)
    if any(
        getattr(report, key) for key in totals if key not in {"left_alone", "unmarked_prefixes"}
    ):
        logger.info(
            "Artifact sweep: %d expired uploads, %d unneeded file records, %d files, %d "
            "partial files, %d manifest objects, %d objects of dropped artifacts and %d stale drop "
            "markers removed; "
            "%d objects left alone, %d artifacts damaged, %d artifacts kept whole, %d artifacts "
            "and %d environments failed%s.",
            report.uploads_expired,
            report.blob_rows_removed,
            report.blobs_deleted,
            report.parts_deleted,
            report.manifests_deleted,
            report.orphan_objects_deleted,
            report.stale_markers_removed,
            report.left_alone,
            report.damaged_artifacts,
            report.kept_artifacts,
            report.failed_artifacts,
            report.failed_environments,
            "; stopped at its time limit, and an artifact too large for one run's time share "
            "is not finished by later runs either (see the artifact sweep's known limits)"
            if stopped
            else "",
        )
    return report
