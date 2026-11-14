"""The sweep removes leftovers and nothing a live version or an open upload needs.

Each kind of thing the sweep deletes has a test for the nearest thing it must not delete.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import ARTIFACT_REF, artifact_document, artifact_service, store_version
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.artifacts import sweep as sweep_module
from lakana_core.artifacts.manifest import parse_manifest
from lakana_core.artifacts.service import ArtifactService
from lakana_core.artifacts.store import (
    artifact_prefix,
    blob_path,
    blobs_prefix,
    dropped_marker_path,
    manifest_path,
)
from lakana_core.artifacts.sweep import SweepReport, sweep_artifacts
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.config import ArtifactsConfig
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    ArtifactBlob,
    ArtifactUpload,
    ArtifactVersion,
    Namespace,
    Table,
    TableType,
)
from lakana_core.storage.base import Storage, StoredObject
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import ResolvedStorage, StorageResolver

_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
_TTL = 3600
_OTHER = TableRef("dev", ARTIFACT_REF.namespace, "other")


def _sha(data: bytes) -> str:
    """Hash bytes.

    Parameters
    ----------
    data
        The bytes.

    Returns
    -------
    str
        The SHA-256, hex.
    """
    return hashlib.sha256(data).hexdigest()


def _id(session: Session, name: str = "churn") -> uuid.UUID:
    """Return an artifact's id.

    Parameters
    ----------
    session
        Database session.
    name
        The artifact's name in `models`.

    Returns
    -------
    uuid.UUID
        The id.
    """
    return session.scalars(select(Table.id).where(Table.name == name)).one()


def _storage(session: Session) -> Storage:
    """Return the storage of `dev`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    Storage
        The environment's storage.
    """
    return StorageResolver(session).for_environment("dev").storage


def _names(session: Session, artifact_id: uuid.UUID) -> set[str]:
    """Return the file names in an artifact's blob directory.

    Parameters
    ----------
    session
        Database session.
    artifact_id
        The artifact.

    Returns
    -------
    set of str
        The names.
    """
    return {path.rsplit("/", 1)[1] for path in _storage(session).list(blobs_prefix(artifact_id))}


def _everything(session: Session, artifact_id: uuid.UUID) -> set[str]:
    """Return every object under an artifact's directory, relative to it.

    Parameters
    ----------
    session
        Database session.
    artifact_id
        The artifact.

    Returns
    -------
    set of str
        ``blobs/<name>`` and ``manifests/<n>.json`` and anything else.
    """
    prefix = artifact_prefix(artifact_id) + "/"
    return {path[len(prefix) :] for path in _storage(session).list(artifact_prefix(artifact_id))}


def _sweep(session: Session, now: datetime = _NOW, **options: object) -> SweepReport:
    """Run the sweep.

    Parameters
    ----------
    session
        Database session.
    now
        The sweep's time.
    **options
        Further keyword arguments of `sweep_artifacts`.

    Returns
    -------
    SweepReport
        What it did.
    """
    options.setdefault("upload_ttl_seconds", _TTL)
    return sweep_artifacts(
        session,
        StorageResolver(session),
        now=now,
        **options,  # ty: ignore[invalid-argument-type]
    )


def _before_lock(
    monkeypatch: pytest.MonkeyPatch, action: Callable[[], object], *, skip: int = 0
) -> None:
    """Run `action` once, right before the sweep takes the `skip + 1`-th artifact lock.

    This is what another request does at that moment, on the same session.

    Parameters
    ----------
    monkeypatch
        The fixture.
    action
        What the other request does.
    skip
        How many locks to let through first.
    """
    real = sweep_module.lock_artifact
    state = {"seen": 0, "fired": False}

    def hooked(session: Session, artifact_id: uuid.UUID) -> bool:
        if not state["fired"]:
            if state["seen"] == skip:
                state["fired"] = True
                action()
            else:
                state["seen"] += 1
        return real(session, artifact_id)

    monkeypatch.setattr(sweep_module, "lock_artifact", hooked)


def _put_old(session: Session, path: str, data: bytes = b"x") -> None:
    """Write an object that is older than `ORPHAN_BLOB_GRACE` at `_NOW`.

    Parameters
    ----------
    session
        Database session.
    path
        Root-relative path.
    data
        The contents.
    """
    storage = StorageResolver(session).for_environment("dev")
    storage.storage.write(path, data)
    _age(Path(storage.config.root.removeprefix("file://")) / path, 8 * 24 * 3600)


def _mark(session: Session, artifact_id: uuid.UUID) -> None:
    """Write the marker a drop leaves, as a drop that was interrupted would have.

    Parameters
    ----------
    session
        Database session.
    artifact_id
        The dropped artifact's id.
    """
    _storage(session).write(dropped_marker_path(artifact_id), b"{}")


def _age(path: Path, seconds: float) -> None:
    """Make a file's modification time `seconds` before `_NOW`.

    Parameters
    ----------
    path
        The file.
    seconds
        Its age.
    """
    moment = (_NOW - timedelta(seconds=seconds)).timestamp()
    os.utime(path, (moment, moment))


def test_the_sweep_never_removes_a_file_a_live_version_or_open_upload_needs(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Live and in-flight files survive; the report is all zeros.

    Real time throughout: the in-flight temporary file carries the file
    system's own modification time.
    """
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "s": b"shared"})
    upload = service.begin_upload(
        principal, ARTIFACT_REF, artifact_document({"n": b"new", "s": b"shared"})
    )
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"new"), [b"new"])
    second = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"f": b"flying"}))
    in_flight = service.receive_blob(principal, ARTIFACT_REF, second.upload_id, _sha(b"flying"))
    in_flight.write(b"fly")
    artifact_id = _id(session)
    before = _everything(session, artifact_id)
    assert len([n for n in before if n.startswith("blobs/")]) == 4
    assert "manifests/1.json" in before

    assert _sweep(session, datetime.now(UTC)) == SweepReport()
    assert _everything(session, artifact_id) == before
    in_flight.write(b"ing")
    assert in_flight.finish() is True
    assert service.commit_upload(principal, ARTIFACT_REF, upload.upload_id).version.version == 2
    assert b"".join(service.open_blob(principal, ARTIFACT_REF, 2, _sha(b"new")).chunks) == b"new"


def test_an_expired_uploads_files_are_removed_and_its_row_too(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    clock = [_NOW]
    service = artifact_service(
        session, tmp_path, config=ArtifactsConfig(upload_ttl_seconds=_TTL), clock=lambda: clock[0]
    )
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "s": b"shared"})
    upload = service.begin_upload(
        principal, ARTIFACT_REF, artifact_document({"n": b"new", "s": b"shared"})
    )
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"new"), [b"new"])
    artifact_id = _id(session)

    report = _sweep(session, _NOW + timedelta(seconds=_TTL))

    # Two rows: the open upload and the committed one of version 1, which also expired.
    assert report == SweepReport(uploads_expired=2, blob_rows_removed=1, blobs_deleted=1)
    assert _names(session, artifact_id) == {_sha(b"a"), _sha(b"shared")}
    assert session.scalars(select(ArtifactUpload)).all() == []
    assert set(session.scalars(select(ArtifactBlob.sha256))) == {_sha(b"a"), _sha(b"shared")}
    assert (
        b"".join(service.open_blob(principal, ARTIFACT_REF, 1, _sha(b"shared")).chunks) == b"shared"
    )
    assert _sweep(session, _NOW + timedelta(seconds=_TTL)) == SweepReport()


def test_leftovers_of_a_crash_are_removed_and_strangers_are_left_alone(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A hash without a row, a stale `.part` file and an orphan manifest are deleted."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    storage = _storage(session)
    orphan = "0" * 64
    _put_old(session, blob_path(artifact_id, orphan), b"left by a crash")
    blobs = tmp_path / blobs_prefix(artifact_id)
    stale, fresh = blobs / ".x.aaaaaaaaaaaaaaaa.part", blobs / ".y.bbbbbbbbbbbbbbbb.part"
    for part in (stale, fresh):
        part.write_bytes(b"partial")
    _age(stale, _TTL + 1)
    _age(fresh, 0)
    (blobs / "README").write_bytes(b"not ours")
    (blobs / ("A" * 64)).write_bytes(b"upper case is not a hash name")
    storage.write("managed/gold/t/data.parquet", b"table data")
    storage.write("artifacts/not-an-id/blobs/" + orphan, b"x")

    report = _sweep(session)

    assert report == SweepReport(blobs_deleted=1, parts_deleted=1, left_alone=3)
    assert _names(session, artifact_id) == {_sha(b"a"), fresh.name, "README", "A" * 64}
    assert storage.exists(manifest_path(artifact_id, 1))
    assert storage.exists("managed/gold/t/data.parquet")
    assert storage.exists("artifacts/not-an-id/blobs/" + orphan)


def test_what_is_under_artifacts_is_read_strictly_and_the_rest_of_storage_is_never_touched(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    artifact_service(session, tmp_path)
    storage = _storage(session)
    odd = [
        "artifacts/" + "A" * 32 + "/blobs/" + "1" * 64,
        "artifacts/" + "a" * 31 + "/blobs/" + "1" * 64,
        "artifacts/" + "a" * 33 + "/blobs/" + "1" * 64,
        "artifacts/" + "a" * 32 + ".txt",
        "artifacts/stray-file",
        "artifacts/" + "c" * 32,  # a file with an artifact's name, not a directory
        "artifacts-other/" + "a" * 32 + "/blobs/" + "1" * 64,
        "managed/" + "a" * 32 + "/blobs/" + "1" * 64,
    ]
    for path in odd:
        storage.write(path, b"x")
    report = _sweep(session)
    assert report == SweepReport(left_alone=6)
    assert all(storage.exists(path) for path in odd)


def test_blobs_and_manifests_of_a_dropped_artifact_are_removed(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A drop that crashed leaves a prefix no row names; all of it goes."""
    artifact_service(session, tmp_path)
    storage = _storage(session)
    gone = uuid.uuid4()
    _mark(session, gone)
    storage.write(blob_path(gone, "1" * 64), b"orphan of a drop that crashed")
    storage.write(manifest_path(gone, 1), b"{}")
    storage.write(blobs_prefix(gone) + "/.x.0123456789abcdef.part", b"p")
    report = _sweep(session)
    assert report == SweepReport(orphan_objects_deleted=4)  # three objects and the marker
    assert list(storage.list(artifact_prefix(gone))) == []


def test_a_prefix_whose_artifact_exists_is_never_taken_for_a_dropped_one(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id is a random UUID, never reused: no row means dropped, unless the row appears now.

    Objects are written before the row exists (a new artifact's row appears in the transaction
    of its first upload, before its first object, so this only models the sweep's view); the
    row is created between the sweep's listing of `artifacts/` and its first delete.
    """
    service = artifact_service(session, tmp_path)
    storage = _storage(session)
    new_id = uuid.uuid4()
    _mark(session, new_id)
    storage.write(blob_path(new_id, "2" * 64), b"just written")
    storage.write(manifest_path(new_id, 1), b"{}")
    namespace_id = session.scalars(select(Namespace.id).where(Namespace.path == "models")).one()

    def create_it() -> None:
        session.add(
            Table(id=new_id, namespace_id=namespace_id, name="born", table_type=TableType.ARTIFACT)
        )
        session.commit()

    _before_lock(monkeypatch, create_it)
    report = _sweep(session)
    assert report.orphan_objects_deleted == 0
    assert _everything(session, new_id) == {
        f"blobs/{'2' * 64}",
        "manifests/1.json",
        ".dropped",
    }
    del service


def test_an_artifact_created_while_the_sweep_runs_is_untouched(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    upload_of_new: list[uuid.UUID] = []

    def another_request() -> None:
        store_version(service, principal, _OTHER, {"m": b"model"})
        upload_of_new.append(_id(session, "other"))

    _before_lock(monkeypatch, another_request)
    assert _sweep(session, datetime.now(UTC)) == SweepReport()
    (new_id,) = upload_of_new
    assert _names(session, new_id) == {_sha(b"model")}
    assert b"".join(service.open_blob(principal, _OTHER, 1, _sha(b"model")).chunks) == b"model"
    assert _sweep(session, datetime.now(UTC)) == SweepReport()
    assert _names(session, new_id) == {_sha(b"model")}


def test_another_artifacts_objects_are_never_touched(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Files are stored per artifact: the same hash in two artifacts is two files."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, _OTHER, {"x": b"same"})
    mine, theirs = _id(session), _id(session, "other")
    _put_old(session, blob_path(mine, _sha(b"same")), b"same")  # a leftover of this one
    before = _everything(session, theirs)

    assert _sweep(session) == SweepReport(blobs_deleted=1)
    assert _names(session, mine) == {_sha(b"a")}
    assert _everything(session, theirs) == before
    assert b"".join(service.open_blob(principal, _OTHER, 1, _sha(b"same")).chunks) == b"same"


def test_a_file_being_stored_right_now_is_kept(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep's listing shows an object with no row; the row is there by the time of its lock.

    `BlobUpload.finish` makes an object visible and inserts its row under the artifact lock,
    so an object without a row seen under the lock is garbage and one seen outside it may be
    a file being stored.
    """
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    upload = service.begin_upload(
        principal, ARTIFACT_REF, artifact_document({"a": b"a", "n": b"new"})
    )
    artifact_id = _id(session)
    _put_old(session, blob_path(artifact_id, _sha(b"new")), b"new")

    def finish_storing() -> None:
        service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"new"), [b"new"])

    _before_lock(monkeypatch, finish_storing, skip=1)
    assert _sweep(session, datetime.now(UTC)) == SweepReport()
    assert _sha(b"new") in _names(session, artifact_id)
    assert service.commit_upload(principal, ARTIFACT_REF, upload.upload_id).version.version == 2
    assert b"".join(service.open_blob(principal, ARTIFACT_REF, 2, _sha(b"new")).chunks) == b"new"


def test_a_file_stored_between_the_two_steps_is_kept(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blob re-uploaded after its row was removed has a row again when bytes are deleted."""
    clock = [_NOW]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    stale = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"n": b"new"}))
    service.put_blob(principal, ARTIFACT_REF, stale.upload_id, _sha(b"new"), [b"new"])
    artifact_id = _id(session)

    def reupload() -> None:
        clock[0] = _NOW + timedelta(seconds=_TTL)
        again = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"n": b"new"}))
        assert again.missing == (_sha(b"new"),)
        service.put_blob(principal, ARTIFACT_REF, again.upload_id, _sha(b"new"), [b"new"])

    # Lock 1 expires the upload, lock 2 removes the row; the bytes' lock is the third.
    _before_lock(monkeypatch, reupload, skip=2)
    report = _sweep(session, _NOW + timedelta(seconds=_TTL))
    assert (report.uploads_expired, report.blob_rows_removed, report.blobs_deleted) == (2, 1, 0)
    assert _sha(b"new") in _names(session, artifact_id)
    assert session.scalars(select(ArtifactBlob.sha256).where(ArtifactBlob.sha256 == _sha(b"new")))


def test_a_file_only_an_unexpired_upload_names_keeps_its_row_and_its_bytes(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The row of a hash no live version names stays while an upload that is open names it."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"n": b"new"}))
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"new"), [b"new"])
    artifact_id = _id(session)
    assert _sweep(session, datetime.now(UTC)) == SweepReport()
    assert _names(session, artifact_id) == {_sha(b"a"), _sha(b"new")}
    assert _sha(b"new") in set(session.scalars(select(ArtifactBlob.sha256)))


def test_a_row_nothing_names_any_more_goes_with_its_bytes_rows_first(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a deletion that met a damaged manifest left, once the manifest is gone."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "s": b"shared"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b", "s": b"shared"})
    artifact_id = _id(session)
    session.execute(
        update(ArtifactVersion).where(ArtifactVersion.version == 1).values(deleted_at=_NOW)
    )
    session.commit()
    seen: list[str] = []
    real = FsspecStorage.delete

    def delete(self: FsspecStorage, path: str) -> None:
        rows = set(session.scalars(select(ArtifactBlob.sha256)))
        seen.append("row still there" if path.rsplit("/", 1)[1] in rows else "row gone")
        real(self, path)

    monkeypatch.setattr(FsspecStorage, "delete", delete)
    report = _sweep(session)
    monkeypatch.undo()
    assert report == SweepReport(blob_rows_removed=1, blobs_deleted=1, manifests_deleted=1)
    assert seen == ["row gone", "row gone"]  # the file, then version 1's manifest object
    assert _names(session, artifact_id) == {_sha(b"b"), _sha(b"shared")}
    assert set(session.scalars(select(ArtifactBlob.sha256))) == {_sha(b"b"), _sha(b"shared")}
    assert _sweep(session) == SweepReport()


def test_a_temporary_file_younger_than_the_upload_lifetime_is_left_alone(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    blobs = tmp_path / blobs_prefix(_id(session))
    edge = blobs / ".edge.cccccccccccccccc.part"
    old = blobs / ".old.dddddddddddddddd.part"
    future = blobs / ".future.eeeeeeeeeeeeeeee.part"
    for part in (edge, old, future):
        part.write_bytes(b"p")
    _age(edge, _TTL - 1)
    _age(old, _TTL)
    _age(future, -60)
    report = _sweep(session)
    assert report == SweepReport(parts_deleted=1)
    assert edge.exists() and future.exists() and not old.exists()


def test_a_part_file_with_no_known_age_is_left_alone(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    storage = _storage(session)
    part = f"{blobs_prefix(artifact_id)}/.x.aaaaaaaaaaaaaaaa.part"
    storage.write(part, b"p")
    real = FsspecStorage.list_objects

    def unknown_age(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        for found in real(self, prefix):
            yield found if found.path != part else StoredObject(part, found.size, None)

    monkeypatch.setattr(FsspecStorage, "list_objects", unknown_age)
    assert _sweep(session) == SweepReport(left_alone=1)
    assert storage.exists(part)


def test_the_manifest_object_of_a_live_version_is_kept_and_a_deleted_ones_is_not(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b"})
    artifact_id = _id(session)
    session.execute(
        update(ArtifactVersion).where(ArtifactVersion.version == 1).values(deleted_at=_NOW)
    )
    session.commit()
    _sweep(session)
    assert [n for n in _everything(session, artifact_id) if n.startswith("manifests/")] == [
        "manifests/2.json"
    ]


def _canonical(files: dict[str, bytes]) -> bytes:
    """Return the bytes of the manifest object a commit of these files writes.

    Parameters
    ----------
    files
        Path to contents.

    Returns
    -------
    bytes
        The canonical manifest.
    """
    return parse_manifest(artifact_document(files)).canonical()


def test_a_commit_that_completes_while_the_sweep_runs_loses_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit writes `manifests/<n>.json` under the artifact's lock before its row commits.

    The commit completes between two steps of the sweep; neither its manifest object nor the
    files it names are touched.
    """
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"b": b"b"}))
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"b"), [b"b"])

    _before_lock(
        monkeypatch, lambda: service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    )
    assert _sweep(session, datetime.now(UTC)) == SweepReport()
    assert _storage(session).exists(manifest_path(artifact_id, 2))
    assert [v.version for v in service.list_versions(principal, ARTIFACT_REF)] == [2, 1]
    assert b"".join(service.open_blob(principal, ARTIFACT_REF, 2, _sha(b"b")).chunks) == b"b"


def test_a_manifest_object_the_catalog_does_not_know_is_kept_reported_and_never_reused(
    session: Session, tmp_path: Path, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed commit's leftover, or a version a restore forgot: the next number is above it."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _storage(session).write(manifest_path(artifact_id, 2), _canonical({"x": b"x"}))
    with caplog.at_level(logging.WARNING):
        assert _sweep(session) == SweepReport()
    assert "manifest objects for version 2 of artifact 'models.churn'" in caplog.text
    assert _storage(session).read(manifest_path(artifact_id, 2)) == _canonical({"x": b"x"})
    assert store_version(service, principal, ARTIFACT_REF, {"b": b"b"}).version.version == 3
    assert _storage(session).read(manifest_path(artifact_id, 2)) == _canonical({"x": b"x"})
    assert _storage(session).read(manifest_path(artifact_id, 3)) == _canonical({"b": b"b"})


def test_a_damaged_manifest_stops_the_sweep_of_that_artifact_and_no_other(
    session: Session, tmp_path: Path, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed: an unreadable manifest may name any file, so nothing of its artifact goes."""
    clock = [_NOW]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, _OTHER, {"x": b"x"})
    expired = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"n": b"new"}))
    service.put_blob(principal, ARTIFACT_REF, expired.upload_id, _sha(b"new"), [b"new"])
    mine = _id(session)
    _put_old(session, blob_path(mine, "0" * 64), b"no row")
    blobs = tmp_path / blobs_prefix(mine)
    stale = blobs / ".x.aaaaaaaaaaaaaaaa.part"
    stale.write_bytes(b"p")
    _age(stale, _TTL + 1)
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == mine)
        .values(manifest_json={"files": "not a list"})
    )
    session.commit()
    before = _everything(session, mine)
    rows = set(session.scalars(select(ArtifactBlob.sha256)))
    uploads = session.scalars(
        select(ArtifactUpload.id).where(ArtifactUpload.artifact_id == mine)
    ).all()

    with caplog.at_level(logging.WARNING):
        report = _sweep(session, _NOW + timedelta(seconds=_TTL))

    # The other artifact's committed upload row expired; the damaged artifact's rows stay.
    assert report == SweepReport(damaged_artifacts=1, uploads_expired=1)
    assert _everything(session, mine) == before
    assert set(session.scalars(select(ArtifactBlob.sha256))) - {_sha(b"x")} <= rows
    assert (
        session.scalars(select(ArtifactUpload.id).where(ArtifactUpload.artifact_id == mine)).all()
        == uploads
    )
    line = [r.getMessage() for r in caplog.records if "damaged" in r.getMessage()]
    assert len(line) == 1
    assert "models.churn" in line[0]
    assert str(tmp_path) not in line[0] and "blobs/" not in line[0]


def test_a_damaged_manifest_found_while_rows_are_checked_stops_the_artifact_too(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    session.add(
        ArtifactBlob(artifact_id=artifact_id, sha256="3" * 64, size_bytes=1, created_at=_NOW)
    )
    session.execute(update(ArtifactVersion).values(manifest_json={"files": "damaged"}))
    session.commit()
    before = _everything(session, artifact_id)
    assert _sweep(session) == SweepReport(damaged_artifacts=1)
    assert _everything(session, artifact_id) == before
    assert "3" * 64 in set(session.scalars(select(ArtifactBlob.sha256)))


def test_an_environment_whose_storage_fails_does_not_stop_the_others(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    real = StorageResolver.for_environment

    def flaky(self: StorageResolver, name: str) -> ResolvedStorage:
        if name == "dev":
            raise OSError("no route to storage")
        return real(self, name)

    monkeypatch.setattr(StorageResolver, "for_environment", flaky)
    assert _sweep(session) == SweepReport(failed_environments=1)


def test_an_artifact_that_fails_does_not_stop_the_next_one(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, _OTHER, {"x": b"x"})
    broken, fine = _id(session), _id(session, "other")
    _put_old(session, blob_path(fine, "4" * 64), b"leftover")
    real = FsspecStorage.list_objects

    def list_objects(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        if prefix == artifact_prefix(broken):
            raise OSError("/secret/storage/path is unreachable")
        return real(self, prefix)

    monkeypatch.setattr(FsspecStorage, "list_objects", list_objects)
    with caplog.at_level(logging.WARNING):
        report = _sweep(session)
    assert report == SweepReport(blobs_deleted=1, failed_artifacts=1)
    assert "OSError" in caplog.text and "secret" not in caplog.text
    assert _names(session, fine) == {_sha(b"x")}


def test_an_environment_whose_artifacts_all_fail_is_given_up_after_a_few(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = artifact_service(session, tmp_path)
    for index in range(sweep_module.MAX_CONSECUTIVE_FAILURES + 3):
        store_version(
            service, principal, TableRef("dev", ARTIFACT_REF.namespace, f"m{index}"), {"a": b"a"}
        )

    def broken(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        raise OSError("storage is down")

    monkeypatch.setattr(FsspecStorage, "list_objects", broken)
    report = _sweep(session)
    assert report == SweepReport(
        failed_artifacts=sweep_module.MAX_CONSECUTIVE_FAILURES, failed_environments=1
    )


def test_the_sweep_lists_lazily_and_deletes_as_it_goes(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No listing of a whole artifact, let alone an environment, is held in memory."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    leftovers = ["5" * 64, "6" * 64, "7" * 64]
    for digest in leftovers:
        _put_old(session, blob_path(artifact_id, digest))
    events: list[str] = []
    real_list, real_delete = FsspecStorage.list_objects, FsspecStorage.delete

    def list_objects(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        if prefix != artifact_prefix(artifact_id):
            yield from real_list(self, prefix)
            return
        for found in real_list(self, prefix):
            events.append("listed")
            yield found
        events.append("listing ended")

    def delete(self: FsspecStorage, path: str) -> None:
        events.append("deleted")
        real_delete(self, path)

    def refuse_a_full_listing(self: FsspecStorage, prefix: str) -> Iterator[str]:
        raise AssertionError("the sweep must not use the whole-listing method")

    monkeypatch.setattr(FsspecStorage, "list_objects", list_objects)
    monkeypatch.setattr(FsspecStorage, "delete", delete)
    monkeypatch.setattr(FsspecStorage, "list", refuse_a_full_listing)
    monkeypatch.setattr(sweep_module, "FILE_BATCH", 2)  # the files' rows are checked 2 at a time
    _sweep(session)
    assert events.index("deleted") < events.index("listing ended")


def test_a_run_stops_at_its_deadline_and_the_next_run_does_the_rest(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    ids = []
    for index in range(4):
        ref = TableRef("dev", ARTIFACT_REF.namespace, f"m{index}")
        store_version(service, principal, ref, {"a": b"a"})
        ids.append(_id(session, f"m{index}"))
        _put_old(session, blob_path(ids[-1], "8" * 64), b"leftover")
    ticks = iter(range(10_000))
    first = _sweep(session, monotonic=lambda: float(next(ticks)), max_seconds=14.0)
    assert first.stopped_early is True
    left = [i for i in ids if "8" * 64 in _names(session, i)]
    assert 0 < len(left) < 4
    for run in range(1, 8):
        report = _sweep(
            session,
            _NOW + timedelta(seconds=run),
            monotonic=lambda: float(next(ticks)),
            max_seconds=14.0,
            rotation=run,
        )
        if not report.stopped_early:
            break
    assert all("8" * 64 not in _names(session, i) for i in ids)


def test_the_summary_of_a_run_stopped_at_its_deadline_does_not_promise_the_rest(
    session: Session, tmp_path: Path, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    """A-I6: the sweep does not resume inside an artifact, so the log must not say it does."""
    service = artifact_service(session, tmp_path)
    for index in range(4):
        ref = TableRef("dev", ARTIFACT_REF.namespace, f"m{index}")
        store_version(service, principal, ref, {"a": b"a"})
        _put_old(session, blob_path(_id(session, f"m{index}"), "8" * 64), b"leftover")
    ticks = iter(range(10_000))
    with caplog.at_level(logging.INFO, logger=sweep_module.logger.name):
        report = _sweep(session, monotonic=lambda: float(next(ticks)), max_seconds=14.0)
    assert report.stopped_early is True
    (line,) = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("Artifact sweep")
    ]
    assert "continues" not in line
    assert line.endswith(
        "; stopped at its time limit, and an artifact too large for one run's time share is not "
        "finished by later runs either (see the artifact sweep's known limits)."
    )


def test_the_sweep_lists_the_storage_root_the_storage_layer_reserves(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_core.storage.resolver import ARTIFACTS_PREFIX

    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    listed: list[str] = []
    real = FsspecStorage.list_children

    def spy(self: FsspecStorage, prefix: str) -> Iterator[tuple[str, bool]]:
        listed.append(prefix)
        return real(self, prefix)

    monkeypatch.setattr(FsspecStorage, "list_children", spy)
    _sweep(session)
    assert listed == [ARTIFACTS_PREFIX]


def test_a_run_with_no_time_does_nothing_and_says_so(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    _put_old(session, blob_path(_id(session), "9" * 64), b"leftover")
    assert _sweep(session, max_seconds=0.0) == SweepReport(stopped_early=True)


def test_every_object_is_decided_under_the_artifacts_lock(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One lock per decision, never one for a listing: take it, delete one object, let it go."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    for digest in ("a" * 64, "b" * 64):
        _put_old(session, blob_path(artifact_id, digest))
    rollbacks = [0]
    real_rollback, real_delete = session.rollback, FsspecStorage.delete
    held: list[int] = []

    monkeypatch.setattr(
        session,
        "rollback",
        lambda: (rollbacks.__setitem__(0, rollbacks[0] + 1), real_rollback())[1],
    )

    def delete(self: FsspecStorage, path: str) -> None:
        held.append(rollbacks[0])
        real_delete(self, path)

    monkeypatch.setattr(FsspecStorage, "delete", delete)
    _sweep(session)
    monkeypatch.undo()
    assert len(held) == 2 and held[1] > held[0]  # the first lock was let go before the second


def test_a_release_over_the_file_budget_leaves_the_rest_to_the_sweep(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A request deletes 200 files; one sweep removes the rest and the manifest."""
    service = artifact_service(session, tmp_path)
    files = {f"f{i:03}": f"file {i}".encode() for i in range(205)}  # the budget is 200
    store_version(service, principal, ARTIFACT_REF, files)
    artifact_id = _id(session)
    service.delete_version(principal, ARTIFACT_REF, 1)

    left = _everything(session, artifact_id)
    assert len([n for n in left if n.startswith("blobs/")]) == 5
    assert "manifests/1.json" in left
    assert session.scalars(select(ArtifactBlob)).all() == []
    assert service.get_version(principal, ARTIFACT_REF, 1).files == ()
    with pytest.raises(Exception, match="was deleted"):
        service.open_blob(principal, ARTIFACT_REF, 1, _sha(b"file 0"))

    assert _sweep(session) == SweepReport(blobs_deleted=5, manifests_deleted=1)
    assert _everything(session, artifact_id) == set()


def test_the_sweep_leaves_a_live_artifact_alone_whatever_a_release_did(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A version deleted past the budget, with another version live: only the garbage goes."""
    service = artifact_service(session, tmp_path)
    shared = {f"s{i}": f"shared {i}".encode() for i in range(3)}
    store_version(service, principal, ARTIFACT_REF, {**shared, "old": b"old"})
    store_version(service, principal, ARTIFACT_REF, {**shared, "new": b"new"})
    artifact_id = _id(session)
    service.delete_version(principal, ARTIFACT_REF, 1)
    assert _sweep(session) == SweepReport()
    assert len(_names(session, artifact_id)) == 4
    for data in (*shared.values(), b"new"):
        assert b"".join(service.open_blob(principal, ARTIFACT_REF, 2, _sha(data)).chunks) == data


def test_a_sweep_object_whose_path_is_not_under_its_prefix_is_left_alone(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend that lists more than was asked for (or odd segments) never costs a delete."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    storage = _storage(session)
    storage.write("managed/keep", b"x")
    prefix = artifact_prefix(artifact_id)
    real = FsspecStorage.list_objects

    def wide(self: FsspecStorage, where: str) -> Iterator[StoredObject]:
        yield from real(self, where)
        yield StoredObject("managed/keep", 1, None)
        yield StoredObject(f"{prefix}/blobs/../../../managed/keep", 1, None)
        yield StoredObject(f"{prefix}/blobs//{'1' * 64}", 1, None)

    monkeypatch.setattr(FsspecStorage, "list_objects", wide)
    report = _sweep(session)
    assert report == SweepReport(left_alone=3)
    assert storage.exists("managed/keep")


def test_a_run_dropped_artifact_that_vanishes_mid_decision_is_garbage(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifact is dropped after the listing: its objects are garbage, whatever they are."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _put_old(session, blob_path(artifact_id, "1" * 64), b"leftover")
    drop = ArtifactService(session, StorageResolver(session), AllowAllAuthorization())
    _before_lock(monkeypatch, lambda: drop.drop_artifact(principal, ARTIFACT_REF), skip=1)
    report = _sweep(session)
    assert report.failed_artifacts == 0
    assert _everything(session, artifact_id) == set()


def test_the_report_is_frozen_and_its_first_five_fields_keep_their_order() -> None:
    names = [f.name for f in dataclasses.fields(SweepReport)][:5]
    assert names == [
        "uploads_expired",
        "blobs_deleted",
        "parts_deleted",
        "left_alone",
        "failed_environments",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        SweepReport().__setattr__("uploads_expired", 1)


def test_a_listing_that_names_objects_already_gone_costs_nothing_and_fails_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale listing can only name what is gone: deleting it is a no-op, and it is not counted."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    gone = uuid.uuid4()
    prefix = artifact_prefix(artifact_id)
    real = FsspecStorage.list_objects

    def ghosts(self: FsspecStorage, where: str) -> Iterator[StoredObject]:
        yield from real(self, where)
        if where == prefix:
            yield StoredObject(f"{prefix}/blobs/{'1' * 64}", 1, _NOW - timedelta(days=9))
            yield StoredObject(
                f"{prefix}/blobs/.g.aaaaaaaaaaaaaaaa.part", 1, _NOW - timedelta(days=9)
            )
            yield StoredObject(f"{prefix}/manifests/7.json", 1, None)
        elif where == artifact_prefix(gone):
            yield StoredObject(f"{artifact_prefix(gone)}/blobs/{'2' * 64}", 1, None)

    monkeypatch.setattr(FsspecStorage, "list_objects", ghosts)
    _mark(session, gone)
    _storage(session).write(manifest_path(gone, 1), b"{}")
    report = _sweep(session)
    assert report == SweepReport(orphan_objects_deleted=2)  # the objects that exist


def test_an_object_outside_an_orphan_prefix_is_left_alone_whatever_the_listing_says(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review I-5: a listing that names a path outside the prefix, or climbs out with `..`."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, _OTHER, {"x": b"x"})
    neighbour = _id(session, "other")
    storage = _storage(session)
    gone = uuid.uuid4()
    _mark(session, gone)
    storage.write(manifest_path(gone, 1), b"{}")
    storage.write("managed/keep", b"x")
    prefix = artifact_prefix(gone)
    targets = [
        "managed/keep",
        f"{prefix}/blobs/../../../managed/keep",
        f"{prefix}/../{neighbour.hex}/blobs/{_sha(b'x')}",
    ]
    real = FsspecStorage.list_objects

    def wide(self: FsspecStorage, where: str) -> Iterator[StoredObject]:
        yield from real(self, where)
        if where == prefix:
            for path in targets:
                yield StoredObject(path, 1, _NOW - timedelta(days=30))

    monkeypatch.setattr(FsspecStorage, "list_objects", wide)
    assert _sweep(session) == SweepReport(orphan_objects_deleted=2, left_alone=3)
    assert storage.exists("managed/keep")
    assert storage.exists(blob_path(neighbour, _sha(b"x")))
    assert b"".join(service.open_blob(principal, _OTHER, 1, _sha(b"x")).chunks) == b"x"


def test_something_unknown_in_an_artifacts_directory_is_left_alone(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    for path in ("README", "manifests/x.json", "manifests/1.json.bak", "blobs/sub/" + "1" * 64):
        _storage(session).write(f"{artifact_prefix(artifact_id)}/{path}", b"x")
    assert _sweep(session) == SweepReport(left_alone=4)


def test_an_artifact_dropped_before_its_uploads_are_expired_is_left_to_the_next_listing(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [_NOW]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"n": b"new"}))
    drop = ArtifactService(session, StorageResolver(session), AllowAllAuthorization())
    _before_lock(monkeypatch, lambda: drop.drop_artifact(principal, ARTIFACT_REF))
    report = _sweep(session, _NOW + timedelta(seconds=_TTL))
    assert report == SweepReport()
    assert session.scalars(select(ArtifactUpload)).all() == []


def test_a_failure_is_logged_with_the_name_and_an_id_when_the_row_is_gone(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact_service(session, tmp_path)
    gone = uuid.uuid4()
    _mark(session, gone)
    _storage(session).write(manifest_path(gone, 1), b"{}")

    def broken(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        raise OSError("storage failed")

    monkeypatch.setattr(FsspecStorage, "list_objects", broken)
    with caplog.at_level(logging.WARNING):
        assert _sweep(session) == SweepReport(failed_artifacts=1)
    assert gone.hex in caplog.text


def test_the_prefixes_without_a_row_get_half_the_time_and_the_rest_goes_to_the_artifacts(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _put_old(session, blob_path(artifact_id, "1" * 64), b"leftover")
    for _ in range(12):
        orphan = uuid.uuid4()
        _mark(session, orphan)
        _storage(session).write(manifest_path(orphan, 1), b"{}")
    ticks = iter(range(10_000))
    report = _sweep(session, monotonic=lambda: float(next(ticks)), max_seconds=20.0)
    assert 0 < report.orphan_objects_deleted < 12
    assert report.blobs_deleted == 1
    assert report.stopped_early is True


def test_the_rows_are_checked_a_batch_at_a_time_and_every_batch_is_reached(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep_module, "_ROW_BATCH", 2)
    service = artifact_service(session, tmp_path)
    files = {f"f{i}": f"file {i}".encode() for i in range(5)}
    store_version(service, principal, ARTIFACT_REF, files)
    artifact_id = _id(session)
    storage = _storage(session)
    for digest in ("1" * 64, "2" * 64, "e" * 64, "f" * 64):
        storage.write(blob_path(artifact_id, digest), b"x")
        session.add(
            ArtifactBlob(artifact_id=artifact_id, sha256=digest, size_bytes=1, created_at=_NOW)
        )
    session.commit()
    assert _sweep(session) == SweepReport(blob_rows_removed=4, blobs_deleted=4)
    assert _names(session, artifact_id) == {_sha(d) for d in files.values()}
    assert len(set(session.scalars(select(ArtifactBlob.sha256)))) == 5
