"""What the sweep keeps after a restore, and how it shares its time.

A database restored from an older backup, a failover that lost commits, or a fresh database
pointed at an existing bucket leaves storage that the catalog has forgotten. A file with no
record goes only after seven days or when a deleted version's manifest names it, and never
while a manifest object the catalog does not know, a live version or an unexpired upload names
it; a manifest object of an unknown version and a directory without a row and marker are never
deleted.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import ARTIFACT_REF, artifact_document, artifact_service, store_version
from sqlalchemy import delete, event, select, update
from sqlalchemy.orm import Session
from test_artifacts_sweep import (
    _NOW,
    _TTL,
    _age,
    _before_lock,
    _canonical,
    _everything,
    _id,
    _mark,
    _names,
    _put_old,
    _sha,
    _storage,
    _sweep,
)

from lakana_core.artifacts import sweep as sweep_module
from lakana_core.artifacts.store import (
    artifact_prefix,
    blob_path,
    blobs_prefix,
    dropped_marker_path,
    manifest_path,
)
from lakana_core.artifacts.sweep import SweepReport
from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import ConflictError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ArtifactBlob,
    ArtifactUpload,
    ArtifactVersion,
    Namespace,
    Table,
    TableType,
)
from lakana_core.storage.base import StoredObject
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import ResolvedStorage, StorageResolver

_DAY = 24 * 3600


def _lose_versions(session: Session, *versions: int, keep_blob_rows: bool = False) -> None:
    """Make the database forget versions, as a restore from an older backup does.

    Parameters
    ----------
    session
        Database session.
    *versions
        The version numbers whose rows are removed; their manifest objects and bytes stay.
    keep_blob_rows
        Keep the `artifact_blobs` rows too.
    """
    rows = session.scalars(select(ArtifactVersion).where(ArtifactVersion.version.in_(versions)))
    hashes: set[str] = set()
    for row in rows:
        hashes |= {entry["sha256"] for entry in row.manifest_json["files"]}
    session.execute(delete(ArtifactVersion).where(ArtifactVersion.version.in_(versions)))
    if not keep_blob_rows:
        session.execute(delete(ArtifactBlob).where(ArtifactBlob.sha256.in_(hashes)))
    session.commit()


@pytest.mark.parametrize("keep_blob_rows", [False, True])
def test_a_restored_database_does_not_cost_the_versions_it_forgot(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
    keep_blob_rows: bool,
) -> None:
    """Versions 2 and 3 exist in storage; the database is back at version 1."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b"})
    store_version(service, principal, ARTIFACT_REF, {"c": b"c"})
    artifact_id = _id(session)
    for data in (b"a", b"b", b"c"):
        _age(tmp_path / blob_path(artifact_id, _sha(data)), 30 * _DAY)
    _lose_versions(session, 2, 3, keep_blob_rows=keep_blob_rows)
    before = _everything(session, artifact_id)

    with caplog.at_level(logging.WARNING):
        report = _sweep(session)

    assert report == SweepReport()
    assert _everything(session, artifact_id) == before
    assert (
        "found manifest objects for versions 2..3 of artifact 'models.churn' that the catalog "
        "does not know; the files they name are kept"
    ) in caplog.text
    assert _sweep(session) == SweepReport()
    assert _everything(session, artifact_id) == before


def test_unknown_manifest_objects_that_cannot_be_read_leave_the_artifact_whole(
    session: Session, tmp_path: Path, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    clock = [_NOW]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    expired = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"n": b"n"}))
    service.put_blob(principal, ARTIFACT_REF, expired.upload_id, _sha(b"n"), [b"n"])
    artifact_id = _id(session)
    _put_old(session, blob_path(artifact_id, "7" * 64))
    _storage(session).write(manifest_path(artifact_id, 2), b"{ half a manifest")
    before = _everything(session, artifact_id)
    uploads = session.scalars(select(ArtifactUpload.id)).all()
    with caplog.at_level(logging.WARNING):
        report = _sweep(session, _NOW + timedelta(seconds=_TTL))
    assert report == SweepReport(kept_artifacts=1)
    assert _everything(session, artifact_id) == before
    assert session.scalars(select(ArtifactUpload.id)).all() == uploads
    assert caplog.text.count("left 'models.churn' whole for this run") == 1


def test_more_unknown_manifest_objects_than_the_limit_leave_the_artifact_whole(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _put_old(session, blob_path(artifact_id, "7" * 64))
    for number in range(2, 2 + 20):  # exactly the limit: read and honoured
        _storage(session).write(manifest_path(artifact_id, number), _canonical({"a": b"a"}))
    assert _sweep(session).kept_artifacts == 0
    assert (
        _storage(session).exists(blob_path(artifact_id, "7" * 64)) is False
    )  # unknown, unprotected
    _put_old(session, blob_path(artifact_id, "8" * 64))
    _storage(session).write(manifest_path(artifact_id, 22), _canonical({"a": b"a"}))
    report = _sweep(session)
    assert report == SweepReport(kept_artifacts=1)
    assert _storage(session).exists(blob_path(artifact_id, "8" * 64))


def test_an_unknown_manifest_object_over_the_size_limit_leaves_the_artifact_whole(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _put_old(session, blob_path(artifact_id, "7" * 64))
    _storage(session).write(manifest_path(artifact_id, 2), _canonical({"a": b"a"}))
    real = FsspecStorage.list_objects

    def huge(self: FsspecStorage, where: str) -> Iterator[StoredObject]:
        for found in real(self, where):
            if found.path.endswith("/2.json"):
                yield StoredObject(found.path, 16 * 1024 * 1024 + 1, found.modified_at)
            else:
                yield found

    monkeypatch.setattr(FsspecStorage, "list_objects", huge)
    assert _sweep(session) == SweepReport(kept_artifacts=1)


def test_a_manifest_object_is_deleted_only_for_a_deleted_version(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Deleted: goes. Live: stays. No row at or below the highest: left alone and counted."""
    service = artifact_service(session, tmp_path)
    for name in (b"a", b"b", b"c"):
        store_version(service, principal, ARTIFACT_REF, {"f": name})
    artifact_id = _id(session)
    session.execute(
        update(ArtifactVersion).where(ArtifactVersion.version == 1).values(deleted_at=_NOW)
    )
    session.execute(delete(ArtifactVersion).where(ArtifactVersion.version == 2))
    session.commit()
    report = _sweep(session)
    assert report.manifests_deleted == 1 and report.left_alone == 0
    left = {n for n in _everything(session, artifact_id) if n.startswith("manifests/")}
    assert left == {"manifests/2.json", "manifests/3.json"}
    # The object of the forgotten version 2 protects its file, which no known version names.
    for data in (b"b", b"c"):
        _age(tmp_path / blob_path(artifact_id, _sha(data)), 30 * _DAY)
    _sweep(session, _NOW + timedelta(days=1))
    assert _sha(b"b") in _names(session, artifact_id)


def test_a_manifest_name_that_is_not_a_plain_version_number_is_left_alone_and_counted(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    for name in ("007.json", "2147483648.json"):
        _storage(session).write(f"{artifact_prefix(artifact_id)}/manifests/{name}", b"{}")
    assert _sweep(session) == SweepReport(left_alone=2)
    assert {"manifests/007.json", "manifests/2147483648.json"} <= _everything(session, artifact_id)


def test_a_rowless_file_needs_a_reason_to_go(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "gone": b"gone"})
    artifact_id = _id(session)
    storage = _storage(session)
    paths = {name: blob_path(artifact_id, name * 64) for name in "123456"}
    for path in paths.values():
        storage.write(path, b"x")
    day = 24 * 3600
    _age(tmp_path / paths["1"], 7 * day + 1)  # old: goes
    _age(tmp_path / paths["2"], 7 * day)  # exactly the grace: goes
    _age(tmp_path / paths["3"], 7 * day - 1)  # younger: stays
    _age(tmp_path / paths["4"], -3600)  # from the future: stays
    # 5 and 6 have the time of writing, which is after `_NOW`: also young. 6 is the only one
    # a deleted version names.
    deleted = session.scalars(select(ArtifactVersion)).one()
    manifest = dict(deleted.manifest_json)
    manifest["files"] = [*manifest["files"], {"path": "z", "sha256": "6" * 64, "size": 1}]
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "keep": b"keep"})
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.version == 1)
        .values(deleted_at=_NOW, manifest_json=manifest)
    )
    session.commit()
    report = _sweep(session)
    # 1, 2 and 6 (named by a deleted version) and `gone`, whose row the deletion released.
    assert report.blobs_deleted == 4
    present = {name: storage.exists(path) for name, path in paths.items()}
    assert present == {"1": False, "2": False, "3": True, "4": True, "5": True, "6": False}


def test_a_file_with_no_modification_time_is_left_alone_unless_a_deleted_version_names_it(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    storage = _storage(session)
    storage.write(blob_path(artifact_id, "1" * 64), b"x")
    real = FsspecStorage.list_objects

    def no_time(self: FsspecStorage, where: str) -> Iterator[StoredObject]:
        for found in real(self, where):
            yield StoredObject(found.path, found.size, None)

    monkeypatch.setattr(FsspecStorage, "list_objects", no_time)
    assert _sweep(session) == SweepReport(left_alone=1)
    assert storage.exists(blob_path(artifact_id, "1" * 64))


def test_a_rowless_file_that_an_unknown_manifest_names_stays_however_old_it_is(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _put_old(session, blob_path(artifact_id, _sha(b"lost")), b"lost")
    _put_old(session, blob_path(artifact_id, "5" * 64))
    _storage(session).write(manifest_path(artifact_id, 2), _canonical({"lost": b"lost"}))
    assert _sweep(session) == SweepReport(blobs_deleted=1)
    assert _names(session, artifact_id) == {_sha(b"a"), _sha(b"lost")}


def test_a_blob_row_that_an_unknown_manifest_names_is_not_released(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b"})
    artifact_id = _id(session)
    _lose_versions(session, 2, keep_blob_rows=True)
    assert _sweep(session) == SweepReport()
    assert _sha(b"b") in set(session.scalars(select(ArtifactBlob.sha256)))
    assert _sha(b"b") in _names(session, artifact_id)


# ---- the marker of a drop -------------------------------------------------------------------


def test_a_rowless_prefix_without_a_marker_is_left_alone_counted_and_reported_once(
    session: Session, tmp_path: Path, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    artifact_service(session, tmp_path)
    storage = _storage(session)
    forgotten = [uuid.uuid4() for _ in range(3)]
    for artifact_id in forgotten:
        storage.write(blob_path(artifact_id, "1" * 64), b"x")
        storage.write(manifest_path(artifact_id, 1), b"{}")
    with caplog.at_level(logging.WARNING):
        report = _sweep(session)
    assert report == SweepReport(unmarked_prefixes=3)
    assert all(len(list(storage.list(artifact_prefix(i)))) == 2 for i in forgotten)
    lines = [r.getMessage() for r in caplog.records if "no drop marker" in r.getMessage()]
    assert lines == [
        "3 artifact directories in environment dev have no catalog entry and no drop marker; "
        "they are left alone. If the database was restored from a backup or lost recent "
        'changes, the files are intact. See "After a restore" in the documentation before '
        "removing anything."
    ]
    assert _sweep(session) == SweepReport(unmarked_prefixes=3)  # nothing ever collects them


def test_an_artifact_created_after_the_ids_were_read_is_not_reported_as_forgotten(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m1: a prefix without a marker counts as forgotten only if the database has no row for it."""
    service = artifact_service(session, tmp_path)
    real = FsspecStorage.list_children

    def created_meanwhile(self: FsspecStorage, prefix: str) -> Iterator[tuple[str, bool]]:
        if not session.scalars(select(ArtifactBlob.sha256)).all():
            store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
        return real(self, prefix)

    monkeypatch.setattr(FsspecStorage, "list_children", created_meanwhile)
    with caplog.at_level(logging.WARNING):
        report = _sweep(session)
    assert report == SweepReport()
    assert not [r for r in caplog.records if "no drop marker" in r.getMessage()]


def test_a_marked_rowless_prefix_goes_and_its_marker_goes_last(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_service(session, tmp_path)
    storage = _storage(session)
    gone = uuid.uuid4()
    _mark(session, gone)
    storage.write(blob_path(gone, "1" * 64), b"x")
    storage.write(blob_path(gone, "2" * 64), b"x")
    storage.write(manifest_path(gone, 1), b"{}")
    deleted: list[str] = []
    real = FsspecStorage.delete

    def delete(self: FsspecStorage, path: str) -> None:
        deleted.append(path)
        real(self, path)

    monkeypatch.setattr(FsspecStorage, "delete", delete)
    assert _sweep(session) == SweepReport(orphan_objects_deleted=4)
    assert deleted[-1] == dropped_marker_path(gone)
    assert len(deleted) == 4
    assert list(storage.list(artifact_prefix(gone))) == []


def test_an_interrupted_removal_of_a_marked_prefix_is_resumed(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    artifact_service(session, tmp_path)
    storage = _storage(session)
    gone = uuid.uuid4()
    _mark(session, gone)
    storage.write(blob_path(gone, "1" * 64), b"x")
    first = _sweep(session, should_stop=lambda: len(list(storage.list(artifact_prefix(gone)))) < 2)
    assert first.stopped_early is True
    assert storage.exists(dropped_marker_path(gone))  # not deleted: the removal is not finished
    assert _sweep(session).orphan_objects_deleted >= 1
    assert list(storage.list(artifact_prefix(gone))) == []


def test_a_marker_next_to_an_existing_row_is_removed_and_nothing_else_is(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A drop whose commit failed, or a restore to before a drop: the artifact lives."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _mark(session, artifact_id)
    before = _everything(session, artifact_id) - {".dropped"}
    assert _sweep(session) == SweepReport(stale_markers_removed=1)
    assert _everything(session, artifact_id) == before
    assert b"".join(service.open_blob(principal, ARTIFACT_REF, 1, _sha(b"a")).chunks) == b"a"
    # The row is lost later without a drop: with no marker the prefix is not deletable.
    session.execute(delete(Table).where(Table.id == artifact_id))
    session.commit()
    assert _sweep(session) == SweepReport(unmarked_prefixes=1)
    assert _everything(session, artifact_id) == before


def test_a_drop_writes_its_marker_first_and_deletes_it_last(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    events: list[str] = []
    real_write, real_delete = FsspecStorage.write, FsspecStorage.delete

    def write(self: FsspecStorage, path: str, data: bytes) -> None:
        events.append(f"write {path.rsplit('/', 1)[1]}")
        real_write(self, path, data)
        if path == dropped_marker_path(artifact_id):
            assert session.scalars(select(Table.id).where(Table.id == artifact_id)).all()

    def delete(self: FsspecStorage, path: str) -> None:
        events.append(f"delete {path.rsplit('/', 1)[1]}")
        real_delete(self, path)

    monkeypatch.setattr(FsspecStorage, "write", write)
    monkeypatch.setattr(FsspecStorage, "delete", delete)
    service.drop_artifact(principal, ARTIFACT_REF)
    monkeypatch.undo()
    assert events[0] == "write .dropped"
    assert events[-1] == "delete .dropped"
    assert events.count("delete .dropped") == 1
    assert list(_storage(session).list(artifact_prefix(artifact_id))) == []


def test_the_marker_names_the_artifact_and_the_time(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path, clock=lambda: _NOW)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    seen: list[bytes] = []
    real = FsspecStorage.delete

    def delete(self: FsspecStorage, path: str) -> None:
        if path == dropped_marker_path(artifact_id):
            seen.append(self.read(path))
        real(self, path)

    monkeypatch.setattr(FsspecStorage, "delete", delete)
    service.drop_artifact(principal, ARTIFACT_REF)
    assert json.loads(seen[0]) == {"dropped_at": _NOW.isoformat(), "artifact": "models.churn"}


def test_a_drop_whose_marker_cannot_be_written_deletes_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    before = _everything(session, artifact_id)
    real = FsspecStorage.write

    def write(self: FsspecStorage, path: str, data: bytes) -> None:
        if path.endswith("/.dropped"):
            raise OSError("/secret/storage: read-only")
        real(self, path, data)

    monkeypatch.setattr(FsspecStorage, "write", write)
    with pytest.raises(ConflictError) as refused:
        service.drop_artifact(principal, ARTIFACT_REF)
    monkeypatch.undo()
    assert str(refused.value) == (
        "models.churn was not dropped because its storage could not be written, and nothing was "
        "deleted. Try again when storage is available."
    )
    assert _id(session) == artifact_id
    assert _everything(session, artifact_id) == before
    assert b"".join(service.open_blob(principal, ARTIFACT_REF, 1, _sha(b"a")).chunks) == b"a"


def test_a_drop_that_fails_after_its_marker_removes_the_marker_before_the_rollback(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    events: list[str] = []
    real_delete, real_rollback = FsspecStorage.delete, session.rollback

    def delete(self: FsspecStorage, path: str) -> None:
        events.append("delete marker" if path.endswith("/.dropped") else "delete")
        real_delete(self, path)

    def rollback() -> None:
        events.append("rollback")
        real_rollback()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr("lakana_core.artifacts.service.revoke_attached", boom)
    monkeypatch.setattr(FsspecStorage, "delete", delete)
    monkeypatch.setattr(session, "rollback", rollback)
    with pytest.raises(RuntimeError, match="database went away"):
        service.drop_artifact(principal, ARTIFACT_REF)
    assert events.index("delete marker") < events.index("rollback")
    assert not _storage(session).exists(dropped_marker_path(artifact_id))
    assert _id(session) == artifact_id


def test_a_drop_that_is_interrupted_after_the_commit_is_finished_by_the_sweep(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "b": b"b"})
    artifact_id = _id(session)
    real = FsspecStorage.delete

    def delete(self: FsspecStorage, path: str) -> None:
        raise OSError("storage went away")

    monkeypatch.setattr(FsspecStorage, "delete", delete)
    service.drop_artifact(principal, ARTIFACT_REF)  # the failure is logged, not raised
    monkeypatch.setattr(FsspecStorage, "delete", real)
    assert _storage(session).exists(dropped_marker_path(artifact_id))
    assert _sweep(session).orphan_objects_deleted == 4  # two blobs, the manifest, the marker
    assert list(_storage(session).list(artifact_prefix(artifact_id))) == []


# ---- the damage gate ------------------------------------------------------------------------


@pytest.mark.parametrize("plant", ["rowless file", "stale part", "manifest of a deleted version"])
def test_the_damage_gate_guards_each_kind_of_deletion_on_its_own(
    session: Session, tmp_path: Path, principal: PrincipalRef, plant: str
) -> None:
    """A damaged live version that is read last, rows all named by a healthy one.

    Nothing but the gate before the deletion itself stops the plant from going.
    """
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "b": b"b"})
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.version == 2)
        .values(manifest_json={"files": "damaged"})
    )
    if plant == "manifest of a deleted version":
        # Only this plant has a deleted version; elsewhere its manifest object would be the
        # first thing to meet the gate and hide whether the others have one.
        session.execute(
            update(ArtifactVersion).where(ArtifactVersion.version == 3).values(deleted_at=_NOW)
        )
    session.commit()
    storage = _storage(session)
    if plant == "rowless file":
        _put_old(session, blob_path(artifact_id, "9" * 64))
        planted = blob_path(artifact_id, "9" * 64)
    elif plant == "stale part":
        planted = f"{blobs_prefix(artifact_id)}/.x.aaaaaaaaaaaaaaaa.part"
        storage.write(planted, b"p")
        _age(tmp_path / planted, 2 * _TTL)
    else:
        planted = manifest_path(artifact_id, 3)
    before = _everything(session, artifact_id)
    assert _sweep(session) == SweepReport(damaged_artifacts=1)
    assert storage.exists(planted)
    assert _everything(session, artifact_id) == before


# ---- failures and bounds --------------------------------------------------------------------


def test_the_environment_is_given_up_after_exactly_five_artifacts_fail_in_a_row(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    for index in range(8):
        store_version(
            service, principal, TableRef("dev", ARTIFACT_REF.namespace, f"m{index}"), {"a": b"a"}
        )

    def broken(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        raise OSError("storage is down")

    monkeypatch.setattr(FsspecStorage, "list_objects", broken)
    assert _sweep(session) == SweepReport(failed_artifacts=5, failed_environments=1)


def test_a_success_resets_the_run_of_failures(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    for index in range(10):
        store_version(
            service, principal, TableRef("dev", ARTIFACT_REF.namespace, f"m{index}"), {"a": b"a"}
        )
    real = FsspecStorage.list_objects
    calls = [0]

    def every_fifth_works(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        if prefix.endswith("/manifests"):
            calls[0] += 1
            if calls[0] % 5 != 0:
                raise OSError("storage hiccup")
        return real(self, prefix)

    monkeypatch.setattr(FsspecStorage, "list_objects", every_fifth_works)
    report = _sweep(session)
    assert report == SweepReport(failed_artifacts=8)  # four in a row, twice: never five


def test_an_interrupt_is_never_swallowed_and_leaves_no_transaction_open(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})

    def interrupted(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        raise KeyboardInterrupt

    monkeypatch.setattr(FsspecStorage, "list_objects", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _sweep(session)
    assert session.in_transaction() is False


def test_the_deadline_is_reached_at_exactly_the_deadline() -> None:
    def run(deadline: float) -> sweep_module._Run:
        return sweep_module._Run(
            None,  # ty: ignore[invalid-argument-type]
            None,  # ty: ignore[invalid-argument-type]
            "dev",
            _NOW,
            _TTL,
            lambda: 5.0,
            deadline,
            lambda: False,
        )

    with pytest.raises(sweep_module._Spent):
        run(5.0)._tick()
    run(5.000001)._tick()


def test_shutdown_ends_the_sweep_between_two_objects(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    for digest in ("1" * 64, "2" * 64, "3" * 64):
        _put_old(session, blob_path(artifact_id, digest))
    stop = [False]
    real = FsspecStorage.delete

    def delete_then_stop(self: FsspecStorage, path: str) -> None:
        real(self, path)
        stop[0] = True

    FsspecStorage.delete = delete_then_stop  # type: ignore[method-assign]
    try:
        report = _sweep(session, should_stop=lambda: stop[0])
    finally:
        FsspecStorage.delete = real  # type: ignore[method-assign]
    assert report.blobs_deleted == 1 and report.stopped_early is True
    assert (
        len([n for n in _names(session, artifact_id) if n in {"1" * 64, "2" * 64, "3" * 64}]) == 2
    )


def test_the_byte_deletes_after_a_row_batch_each_check_the_deadline(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    clock = [_NOW]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    upload = service.begin_upload(
        principal, ARTIFACT_REF, artifact_document({f"f{i}": f"n{i}".encode() for i in range(3)})
    )
    for i in range(3):
        service.put_blob(
            principal, ARTIFACT_REF, upload.upload_id, _sha(f"n{i}".encode()), [f"n{i}".encode()]
        )
    artifact_id = _id(session)
    stop = [False]
    real = FsspecStorage.delete

    def delete_then_stop(self: FsspecStorage, path: str) -> None:
        real(self, path)
        stop[0] = True

    FsspecStorage.delete = delete_then_stop  # type: ignore[method-assign]
    try:
        report = _sweep(session, _NOW + timedelta(seconds=_TTL), should_stop=lambda: stop[0])
    finally:
        FsspecStorage.delete = real  # type: ignore[method-assign]
    assert (report.blob_rows_removed, report.blobs_deleted) == (3, 1)
    assert len(_names(session, artifact_id)) == 3  # a, and two files left for the next run


def test_expired_uploads_are_deleted_only_for_the_artifact_that_is_locked(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    clock = [_NOW]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    other = TableRef("dev", ARTIFACT_REF.namespace, "other")
    for ref in (ARTIFACT_REF, other):
        store_version(service, principal, ref, {"a": b"a"})
        service.begin_upload(principal, ref, artifact_document({"n": b"n"}))
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.artifact_id == _id(session, "other"))
        .values(manifest_json={"files": "damaged"})
    )
    session.commit()
    report = _sweep(session, _NOW + timedelta(seconds=_TTL))
    # The healthy artifact's two rows (open and committed) go; the damaged one's stay.
    assert (report.uploads_expired, report.damaged_artifacts) == (2, 1)
    remaining = session.scalars(select(ArtifactUpload.artifact_id)).all()
    assert set(remaining) == {_id(session, "other")}
    assert len(remaining) == 2


def test_a_stale_part_is_never_judged_before_an_hour(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    blobs = tmp_path / blobs_prefix(_id(session))
    young, old = blobs / ".y.aaaaaaaaaaaaaaaa.part", blobs / ".o.bbbbbbbbbbbbbbbb.part"
    for part in (young, old):
        part.write_bytes(b"p")
    _age(young, 3599)  # older than the 60 s lifetime, younger than the hour
    _age(old, 3600)
    assert _sweep(session, upload_ttl_seconds=60) == SweepReport(parts_deleted=1)
    assert young.exists() and not old.exists()


def test_a_temporary_file_that_vanished_makes_the_upload_conflict(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"a": b"abc"}))
    stream = service.receive_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"abc"))
    stream.write(b"abc")
    for part in (tmp_path / blobs_prefix(_id(session))).glob(".*.part"):
        part.unlink()
    with pytest.raises(ConflictError, match="expired or does not exist"):
        stream.finish()
    assert session.in_transaction() is False


def test_no_lock_is_taken_while_a_transaction_is_open(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every unlocked read lets go of its transaction before the lock is asked for."""
    clock = [_NOW]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b"})
    artifact_id = _id(session)
    session.execute(
        update(ArtifactVersion).where(ArtifactVersion.version == 1).values(deleted_at=_NOW)
    )
    session.commit()
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"n": b"n"}))
    _put_old(session, blob_path(artifact_id, "9" * 64))
    gone = uuid.uuid4()
    _mark(session, gone)
    seen: list[bool] = []
    real = sweep_module.lock_artifact

    def checked(inner: Session, target: uuid.UUID) -> bool:
        seen.append(inner.in_transaction())
        return real(inner, target)

    monkeypatch.setattr(sweep_module, "lock_artifact", checked)
    report = _sweep(session, _NOW + timedelta(seconds=_TTL))
    assert report.manifests_deleted == 1 and report.uploads_expired == 3
    assert len(seen) >= 5
    assert seen == [False] * len(seen)


def test_a_clean_environment_that_runs_out_of_time_has_nothing_left(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    artifact_service(session, tmp_path)
    ticks = iter(range(1000))
    report = _sweep(session, monotonic=lambda: float(next(ticks)), max_seconds=10.0)
    assert report.stopped_early is False


# ---- sharing the time -----------------------------------------------------------------------


def _staging(session: Session, tmp_path: Path, principal: PrincipalRef) -> TableRef:
    """Create a second environment, `stg`, with its own storage and a `models` namespace.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Directory under which its storage root is made.
    principal
        The acting principal.

    Returns
    -------
    TableRef
        A reference for an artifact in it.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="stg",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path / 'stg'}", options={}),
    )
    catalog.create_namespace(principal, "stg", NamespacePath.from_parts(["models"]))
    return TableRef("stg", NamespacePath.from_parts(["models"]), "staged")


def test_the_second_environment_is_swept_in_the_same_run_as_a_backlogged_first(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep_module, "_FILE_BATCH", 5)
    service = artifact_service(session, tmp_path)
    ref = _staging(session, tmp_path, principal)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ref, {"a": b"a"})
    dev_id = _id(session)
    stg_id = _id(session, "staged")
    for index in range(40):
        _put_old(session, blob_path(dev_id, f"{index:064x}"))
    stg_storage = StorageResolver(session).for_environment("stg").storage
    stg_storage.write(blob_path(stg_id, "9" * 64), b"x")
    _age(tmp_path / "stg" / blob_path(stg_id, "9" * 64), 8 * _DAY)
    ticks = iter(range(10_000))
    report = _sweep(session, monotonic=lambda: float(next(ticks)), max_seconds=60.0)
    assert report.stopped_early is True
    assert stg_storage.exists(blob_path(stg_id, "9" * 64)) is False  # reached
    assert 0 < len([n for n in _names(session, dev_id) if n != _sha(b"a")]) < 40  # dev: a share


def test_the_order_of_the_environments_rotates_with_the_run(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_service(session, tmp_path)
    _staging(session, tmp_path, principal)
    order: list[str] = []
    real = StorageResolver.for_environment

    def spy(self: StorageResolver, name: str) -> ResolvedStorage:
        order.append(name)
        return real(self, name)

    monkeypatch.setattr(StorageResolver, "for_environment", spy)
    _sweep(session, rotation=0)
    _sweep(session, rotation=1)
    _sweep(session, rotation=2)
    assert order == ["dev", "stg", "stg", "dev", "dev", "stg"]


def test_an_environment_is_not_started_once_the_deadline_has_passed(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_service(session, tmp_path)
    _staging(session, tmp_path, principal)
    started: list[str] = []
    real = StorageResolver.for_environment

    def spy(self: StorageResolver, name: str) -> ResolvedStorage:
        started.append(name)
        return real(self, name)

    monkeypatch.setattr(StorageResolver, "for_environment", spy)
    report = _sweep(session, monotonic=lambda: 7.0, max_seconds=0.0)
    assert started == [] and report.stopped_early is True


def test_each_environment_lists_only_its_own_artifacts(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    ref = _staging(session, tmp_path, principal)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ref, {"b": b"b"})
    dev_id = _id(session)
    stg_id = _id(session, "staged")
    asked: list[tuple[str, str]] = []
    real = FsspecStorage.list_objects

    def spy(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        asked.append(("stg" if "/stg" in self._base else "dev", prefix))
        return real(self, prefix)

    monkeypatch.setattr(FsspecStorage, "list_objects", spy)
    _sweep(session)
    assert {p for env, p in asked if env == "dev"} == {
        artifact_prefix(dev_id),
        artifact_prefix(dev_id) + "/manifests",
    }
    assert {p for env, p in asked if env == "stg"} == {
        artifact_prefix(stg_id),
        artifact_prefix(stg_id) + "/manifests",
    }


def test_the_files_of_an_artifact_are_checked_against_the_database_in_batches(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(
        service, principal, ARTIFACT_REF, {f"f{i}": f"file {i}".encode() for i in range(9)}
    )
    statements: list[str] = []
    engine = session.get_bind()

    def record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "FROM artifact_blobs" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert _sweep(session, datetime.now(UTC)) == SweepReport()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    # One page of rows for the release step, one query for all nine files: not one per file.
    assert len(statements) <= 3


# ---- the release budget ---------------------------------------------------------------------


@pytest.mark.slow
def test_pruning_versions_that_free_no_file_still_costs_a_request_at_most_its_budget(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    for _ in range(206):
        store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    session.execute(
        update(ArtifactVersion).where(ArtifactVersion.version <= 205).values(deleted_at=_NOW)
    )
    session.commit()
    retired: list[tuple[int, set[str]]] = [(n, set()) for n in range(205, 0, -1)]
    deletes: list[str] = []
    real = FsspecStorage.delete

    def delete(self: FsspecStorage, path: str) -> None:
        deletes.append(path)
        real(self, path)

    monkeypatch.setattr(FsspecStorage, "delete", delete)
    service._release(ARTIFACT_REF, artifact_id, retired)
    monkeypatch.undo()
    assert len(deletes) == 200
    left = [n for n in _everything(session, artifact_id) if n.startswith("manifests/")]
    assert len(left) == 206 - 200
    report = _sweep(session)
    assert report.manifests_deleted == 5
    assert _everything(session, artifact_id) >= {"manifests/206.json"}
    assert len([n for n in _everything(session, artifact_id) if n.startswith("manifests/")]) == 1


def test_an_unknown_manifest_object_that_vanished_before_it_was_read_is_skipped(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _storage(session).write(manifest_path(artifact_id, 2), _canonical({"x": b"x"}))
    real = FsspecStorage.read

    def vanished(self: FsspecStorage, path: str) -> bytes:
        if path == manifest_path(artifact_id, 2):
            self.delete(path)
        return real(self, path)

    monkeypatch.setattr(FsspecStorage, "read", vanished)
    assert _sweep(session) == SweepReport()


def test_a_deleted_versions_damaged_manifest_makes_nothing_eligible(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b"})
    artifact_id = _id(session)
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.version == 1)
        .values(deleted_at=_NOW, manifest_json={"files": "damaged"})
    )
    session.commit()
    young = blob_path(artifact_id, "6" * 64)
    _storage(session).write(young, b"x")
    assert _sweep(session).blobs_deleted == 1  # file a, through its released row
    assert _storage(session).exists(young)


def test_a_manifest_object_whose_version_came_back_is_kept(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision is taken again under the lock, on the row as it is then."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b"})
    artifact_id = _id(session)
    session.execute(
        update(ArtifactVersion).where(ArtifactVersion.version == 1).values(deleted_at=_NOW)
    )
    session.commit()

    def restore() -> None:
        session.execute(
            update(ArtifactVersion).where(ArtifactVersion.version == 1).values(deleted_at=None)
        )
        session.commit()

    # Locks: the rows' batch, the byte delete of file a, then the manifest object's.
    _before_lock(monkeypatch, restore, skip=2)
    assert _sweep(session).manifests_deleted == 0
    assert _storage(session).exists(manifest_path(artifact_id, 1))


def test_a_manifest_object_that_is_already_gone_costs_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b"})
    artifact_id = _id(session)
    session.execute(
        update(ArtifactVersion).where(ArtifactVersion.version == 1).values(deleted_at=_NOW)
    )
    session.commit()
    _before_lock(
        monkeypatch, lambda: _storage(session).delete(manifest_path(artifact_id, 1)), skip=2
    )
    assert _sweep(session).manifests_deleted == 0


def test_a_marked_prefix_whose_artifact_appears_before_the_marker_is_removed_keeps_it(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_service(session, tmp_path)
    gone = uuid.uuid4()
    _mark(session, gone)
    namespace_id = session.scalars(select(Namespace.id).where(Namespace.path == "models")).one()

    def create_it() -> None:
        session.add(
            Table(id=gone, namespace_id=namespace_id, name="born", table_type=TableType.ARTIFACT)
        )
        session.commit()

    _before_lock(monkeypatch, create_it)  # the one lock of the final step: nothing else to delete
    assert _sweep(session) == SweepReport()
    assert _storage(session).exists(dropped_marker_path(gone))


def test_a_sweep_of_a_database_without_environments_does_nothing(session: Session) -> None:
    assert _sweep(session) == SweepReport()


# ---- numbers storage has seen are never reused ------------------------------------


def _before_commit_lock(monkeypatch: pytest.MonkeyPatch, action: Callable[[], object]) -> None:
    """Run `action` once, right before the service takes the artifact lock.

    Parameters
    ----------
    monkeypatch
        The fixture.
    action
        What another request does at that moment.
    """
    from lakana_core.artifacts import service as service_module

    real = service_module.lock_artifact
    fired = [False]

    def hooked(inner: Session, artifact_id: uuid.UUID) -> bool:
        if not fired[0]:
            fired[0] = True
            action()
        return real(inner, artifact_id)

    monkeypatch.setattr(service_module, "lock_artifact", hooked)


def test_the_next_commit_after_a_restore_does_not_overwrite_a_forgotten_versions_manifest(
    session: Session, tmp_path: Path, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    """Probe a: the restore lost exactly the newest version."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "s": b"s"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b", "s": b"s"})
    store_version(service, principal, ARTIFACT_REF, {"c": b"c", "s": b"s"})
    artifact_id = _id(session)
    forgotten = _storage(session).read(manifest_path(artifact_id, 3))
    _forget_rows(session, 3)

    result = store_version(service, principal, ARTIFACT_REF, {"d": b"d", "s": b"s"})

    assert result.version.version == 4
    assert _storage(session).read(manifest_path(artifact_id, 3)) == forgotten
    for data in (b"a", b"b", b"c", b"d", b"s"):
        _age(tmp_path / blob_path(artifact_id, _sha(data)), 60 * _DAY)
    with caplog.at_level(logging.WARNING):
        assert _sweep(session, _NOW + timedelta(days=1)) == SweepReport()
    assert _sha(b"c") in _names(session, artifact_id)  # whatever its age
    assert "manifest objects for version 3 of artifact 'models.churn'" in caplog.text


def test_a_cut_release_and_a_forgotten_version_both_keep_their_numbers(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Probe f: the catalog is back at 5; storage has 6 (deleted, files gone) and 7."""
    service = artifact_service(session, tmp_path)
    for i in range(1, 8):
        store_version(service, principal, ARTIFACT_REF, {"f": f"f{i}".encode(), "s": b"s"})
    artifact_id = _id(session)
    service.delete_version(principal, ARTIFACT_REF, 6)
    seven = _storage(session).read(manifest_path(artifact_id, 7))
    _forget_rows(session, 6, 7)

    assert (
        store_version(service, principal, ARTIFACT_REF, {"f": b"new", "s": b"s"}).version.version
        == 8
    )
    assert _storage(session).read(manifest_path(artifact_id, 7)) == seven
    _age(tmp_path / blob_path(artifact_id, _sha(b"f7")), 60 * _DAY)
    assert _sweep(session, _NOW + timedelta(days=1)).blobs_deleted == 0
    assert _sha(b"f7") in _names(session, artifact_id)


def test_two_commits_that_meet_at_the_lock_get_distinct_numbers(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    first = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"b": b"b"}))
    second = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"c": b"c"}))
    service.put_blob(principal, ARTIFACT_REF, first.upload_id, _sha(b"b"), [b"b"])
    service.put_blob(principal, ARTIFACT_REF, second.upload_id, _sha(b"c"), [b"c"])
    committed: list[int] = []
    _before_commit_lock(
        monkeypatch,
        lambda: committed.append(
            service.commit_upload(principal, ARTIFACT_REF, first.upload_id).version.version
        ),
    )
    # The hook commits the first upload just before the second commit takes its lock.
    last = service.commit_upload(principal, ARTIFACT_REF, second.upload_id).version.version
    assert (committed, last) == ([2], 3)
    assert _storage(session).read(manifest_path(artifact_id, 2)) == _canonical({"b": b"b"})
    assert _storage(session).read(manifest_path(artifact_id, 3)) == _canonical({"c": b"c"})


def test_the_manifest_listing_of_a_commit_is_taken_under_the_artifacts_lock(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_core.artifacts import service as service_module

    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"a": b"a"}))
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"a"), [b"a"])
    events: list[str] = []
    real_lock, real_list = service_module.lock_artifact, FsspecStorage.list_objects

    def lock(inner: Session, artifact_id: uuid.UUID) -> bool:
        events.append("lock")
        return real_lock(inner, artifact_id)

    def listing(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        if prefix.endswith("/manifests"):
            events.append("list manifests")
        return real_list(self, prefix)

    monkeypatch.setattr(service_module, "lock_artifact", lock)
    monkeypatch.setattr(FsspecStorage, "list_objects", listing)
    service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert events.index("lock") < events.index("list manifests")


def _forget_rows(session: Session, *versions: int) -> None:
    """Make the database forget versions and their file rows (a restore); objects stay.

    Parameters
    ----------
    session
        Database session.
    *versions
        The numbers.
    """
    _lose_versions(session, *versions)


# ---- atomic manifest writes and strays ------------------------------------------------------


def test_a_manifest_object_is_written_through_the_streaming_writer_and_never_half(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lakana_core.storage import fsspec_storage

    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"a": b"a"}))
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(b"a"), [b"a"])
    artifact_id = _id(session)
    manifests = tmp_path / artifact_prefix(artifact_id) / "manifests"

    def crash(self: object, data: bytes) -> None:
        raise OSError("crashed mid-write")

    with monkeypatch.context() as patch:
        patch.setattr(fsspec_storage._LocalStreamWriter, "write", crash)
        with pytest.raises(ConflictError, match="could not be written"):
            service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert list(manifests.iterdir()) == []  # neither an object nor a temporary file
    assert service.commit_upload(principal, ARTIFACT_REF, upload.upload_id).version.version == 1
    assert [p.name for p in manifests.iterdir()] == ["1.json"]


def test_an_unfinished_manifest_write_is_collected_by_age(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    manifests = tmp_path / artifact_prefix(_id(session)) / "manifests"
    old, young = (
        manifests / ".2.json.aaaaaaaaaaaaaaaa.part",
        manifests / ".3.json.bbbbbbbbbbbbbbbb.part",
    )
    for part in (old, young):
        part.write_bytes(b"{")
    _age(old, 2 * _TTL)
    _age(young, 0)
    assert _sweep(session) == SweepReport(parts_deleted=1)
    assert young.exists() and not old.exists()


def test_manifest_names_are_read_by_one_strict_parser() -> None:
    from lakana_core.artifacts.store import manifest_number

    assert manifest_number("manifests/12.json") == 12
    assert manifest_number("manifests/2147483647.json") == 2147483647
    for bad in (
        "manifests/0.json",
        "manifests/012.json",
        "manifests/+1.json",
        "manifests/1.json.bak",
        "manifests/1.JSON",
        "manifests/a/1.json",
        "blobs/1.json",
        "manifests/2147483648.json",
        "manifests/99999999999.json",
    ):
        assert manifest_number(bad) is None


def test_the_version_numbers_of_a_warning_are_ranges_then_a_count() -> None:
    assert sweep_module._ranges([3]) == "3"
    assert sweep_module._ranges([2, 3, 4, 7, 9, 10]) == "2..4, 7, 9..10"
    assert sweep_module._ranges([1, 3, 5, 7, 9, 11, 12, 14]) == "1, 3, 5, 7, 9 and 3 more"


def test_a_file_only_a_live_version_names_is_not_taken_for_one_a_deleted_version_names(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A rowless file named by a live version needs the age rule like any other."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "x": b"x"})
    artifact_id = _id(session)
    session.execute(delete(ArtifactBlob).where(ArtifactBlob.sha256 == _sha(b"x")))
    session.commit()
    assert _sweep(session, datetime.now(UTC)) == SweepReport()
    assert _sha(b"x") in _names(session, artifact_id)


@pytest.mark.parametrize("namer", ["live version", "open upload"])
def test_a_file_that_a_version_or_an_upload_names_is_kept_whatever_its_record_says(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
    namer: str,
) -> None:
    """Second line of defence: the state a consistent catalog cannot reach.

    The record of a file is gone by hand and the file is older than the grace; a live version
    (or an unexpired upload) still names it.
    """
    service = artifact_service(session, tmp_path, clock=lambda: _NOW)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    document = artifact_document({"x": b"x", "y": b"y"})
    if namer == "live version":
        store_version(service, principal, ARTIFACT_REF, {"x": b"x", "y": b"y"})
    else:
        upload = service.begin_upload(principal, ARTIFACT_REF, document)
        for data in (b"x", b"y"):
            service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _sha(data), [data])
    artifact_id = _id(session)
    for data in (b"x", b"y"):
        _age(tmp_path / blob_path(artifact_id, _sha(data)), 30 * _DAY)
    session.execute(delete(ArtifactBlob).where(ArtifactBlob.sha256.in_([_sha(b"x"), _sha(b"y")])))
    session.commit()
    before = _everything(session, artifact_id)

    with caplog.at_level(logging.WARNING):
        report = _sweep(session)

    assert _everything(session, artifact_id) == before
    assert report == SweepReport(named_files_kept=2)
    assert caplog.text.count("2 files of artifact 'models.churn' are named by a version") == 1
    assert "have no record; kept" in caplog.text


def test_a_file_stored_between_the_check_and_the_delete_is_kept(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing named the file when the batch was checked; its row is there by the delete's lock."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    path = blob_path(artifact_id, "1" * 64)
    _put_old(session, path)

    def stored_meanwhile() -> None:
        session.add(
            ArtifactBlob(artifact_id=artifact_id, sha256="1" * 64, size_bytes=1, created_at=_NOW)
        )
        session.commit()

    _before_lock(monkeypatch, stored_meanwhile, skip=2)
    assert _sweep(session) == SweepReport()
    assert _storage(session).exists(path)


def test_a_file_that_nothing_names_is_still_deleted_and_the_check_runs_once_per_batch(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    paths = [blob_path(artifact_id, name * 64) for name in "123"]
    for path in paths:
        _put_old(session, path)
    asked: list[int] = []
    real = sweep_module.unreferenced_hashes

    def counting(*args: object, **kwargs: object) -> set[str]:
        candidates = set(args[2])  # ty: ignore[invalid-argument-type]
        if "1" * 64 in candidates:  # the planted files; the row release asks about row names
            asked.append(len(candidates))
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(sweep_module, "unreferenced_hashes", counting)
    assert _sweep(session) == SweepReport(blobs_deleted=3)
    assert not any(_storage(session).exists(path) for path in paths)
    assert asked == [3]


def test_a_damaged_live_manifest_still_leaves_everything_of_the_artifact_alone(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a", "x": b"x"})
    artifact_id = _id(session)
    _age(tmp_path / blob_path(artifact_id, _sha(b"x")), 30 * _DAY)
    _put_old(session, blob_path(artifact_id, "9" * 64))
    session.execute(delete(ArtifactBlob).where(ArtifactBlob.sha256 == _sha(b"x")))
    session.execute(update(ArtifactVersion).values(manifest_json={"files": "damaged"}))
    session.commit()
    before = _everything(session, artifact_id)
    assert _sweep(session) == SweepReport(damaged_artifacts=1)
    assert _everything(session, artifact_id) == before


def test_a_sweep_summary_counts_stale_markers(
    session: Session, tmp_path: Path, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    _mark(session, _id(session))
    with caplog.at_level(logging.INFO):
        _sweep(session)
    assert "1 stale drop markers removed" in caplog.text


def test_a_stale_marker_that_is_gone_by_the_time_of_the_lock_costs_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _mark(session, artifact_id)
    # Locks: the rows' batch, then the marker's.
    _before_lock(
        monkeypatch, lambda: _storage(session).delete(dropped_marker_path(artifact_id)), skip=1
    )
    assert _sweep(session) == SweepReport()


def test_a_stale_marker_is_kept_when_the_artifacts_row_is_gone_by_the_time_of_the_lock(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-2: the row check is what stands between a marker and a drop that is in flight."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _mark(session, artifact_id)

    def drop_row() -> None:
        session.execute(delete(Table).where(Table.id == artifact_id))
        session.commit()

    _before_lock(monkeypatch, drop_row, skip=1)  # the rows' batch, then the marker's
    assert _sweep(session) == SweepReport()
    assert ".dropped" in _everything(session, artifact_id)


def test_a_manifest_object_whose_commit_lands_during_the_sweep_is_not_reported(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-3: the object was listed before the row was visible; the lock settles which it is."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    store_version(service, principal, ARTIFACT_REF, {"b": b"b"})
    artifact_id = _id(session)
    row = session.execute(select(ArtifactVersion).where(ArtifactVersion.version == 2)).scalar_one()
    values = {
        column.name: getattr(row, column.name) for column in ArtifactVersion.__table__.columns
    }
    _lose_versions(session, 2, keep_blob_rows=True)

    def commit_lands() -> None:
        session.add(ArtifactVersion(**values))
        session.commit()

    _before_lock(monkeypatch, commit_lands)
    with caplog.at_level(logging.WARNING):
        _sweep(session)
    assert "that the catalog does not know" not in caplog.text
    assert manifest_path(artifact_id, 2).rsplit("/", 1)[1] in {
        name.rsplit("/", 1)[-1] for name in _everything(session, artifact_id)
    }


def test_the_unknown_versions_warning_says_version_for_one_and_versions_for_more(
    session: Session, tmp_path: Path, principal: PrincipalRef, caplog: pytest.LogCaptureFixture
) -> None:
    """R-7."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _storage(session).write(manifest_path(artifact_id, 5), _canonical({"b": b"b"}))
    with caplog.at_level(logging.WARNING):
        _sweep(session)
    assert "manifest objects for version 5 of artifact 'models.churn'" in caplog.text
    caplog.clear()
    _storage(session).write(manifest_path(artifact_id, 6), _canonical({"c": b"c"}))
    with caplog.at_level(logging.WARNING):
        _sweep(session)
    assert "manifest objects for versions 5..6 of artifact 'models.churn'" in caplog.text


def test_an_artifact_dropped_while_its_unknown_manifests_are_judged_is_left_whole(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-3: when the lock finds no row, nothing is warned about and nothing is deleted."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"a": b"a"})
    artifact_id = _id(session)
    _storage(session).write(manifest_path(artifact_id, 5), _canonical({"b": b"b"}))
    before = _everything(session, artifact_id)

    def drop_row() -> None:
        session.execute(delete(Table).where(Table.id == artifact_id))
        session.commit()

    _before_lock(monkeypatch, drop_row)
    assert _sweep(session) == SweepReport(kept_artifacts=1)
    assert _everything(session, artifact_id) == before


def _lose_records_of_named_files(
    session: Session, tmp_path: Path, principal: PrincipalRef, contents: dict[str, bytes]
) -> uuid.UUID:
    """Store a version that names `contents`, age its files and remove their records.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The storage root.
    principal
        The caller.
    contents
        Path to bytes of the one version.

    Returns
    -------
    uuid.UUID
        The artifact's id.
    """
    service = artifact_service(session, tmp_path, clock=lambda: _NOW)
    store_version(service, principal, ARTIFACT_REF, contents)
    artifact_id = _id(session)
    for data in contents.values():
        _age(tmp_path / blob_path(artifact_id, _sha(data)), 30 * _DAY)
    session.execute(
        delete(ArtifactBlob).where(ArtifactBlob.sha256.in_([_sha(d) for d in contents.values()]))
    )
    session.commit()
    return artifact_id


def test_the_warning_about_named_files_survives_a_run_that_stops_inside_the_artifact(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m-1: a deadline or a shutdown between two batches must not swallow what was kept."""
    contents = {f"f{n}": f"x{n}".encode() for n in range(4)}
    _lose_records_of_named_files(session, tmp_path, principal, contents)
    monkeypatch.setattr(sweep_module, "_FILE_BATCH", 2)
    stop = [False]
    named = {_sha(value) for value in contents.values()}
    real = sweep_module.unreferenced_hashes

    def stopping_after_the_first_batch(*args: object, **kwargs: object) -> set[str]:
        found = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        # The first batch of the named files, whichever two the listing gives first, ends the
        # run at the next listed object; looking for one file made the stop depend on the order.
        if named & set(args[2]):  # ty: ignore[invalid-argument-type]
            stop[0] = True
        return found

    monkeypatch.setattr(sweep_module, "unreferenced_hashes", stopping_after_the_first_batch)
    with caplog.at_level(logging.WARNING):
        report = _sweep(session, should_stop=lambda: stop[0])
    assert report.stopped_early is True
    assert report.named_files_kept == 2
    assert caplog.text.count("2 files of artifact 'models.churn' are named by a version") == 1


def test_a_manifest_damaged_after_the_readability_check_is_a_damaged_artifact_like_any_other(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m-2: the error of the batch check is not a failure that counts toward giving up."""
    from lakana_core.artifacts.store import UnreadableManifest

    artifact_id = _lose_records_of_named_files(session, tmp_path, principal, {"f": b"x"})
    _put_old(session, blob_path(artifact_id, "1" * 64))
    real = sweep_module.unreferenced_hashes

    def damaged_since(*args: object, **kwargs: object) -> set[str]:
        if _sha(b"x") in set(args[2]):  # ty: ignore[invalid-argument-type]
            raise UnreadableManifest("damaged since the gate was cached")
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(sweep_module, "unreferenced_hashes", damaged_since)
    before = _everything(session, artifact_id)
    with caplog.at_level(logging.WARNING):
        report = _sweep(session)
    assert report == SweepReport(damaged_artifacts=1)
    assert "left 'models.churn' whole: a stored manifest" in caplog.text
    assert _everything(session, artifact_id) == before


def test_several_batches_keep_the_named_files_warn_once_and_drop_no_file(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m-3: 8 files in batches of 2, 4 named by a version and 4 nothing names."""
    contents = {f"f{n}": f"x{n}".encode() for n in range(4)}
    artifact_id = _lose_records_of_named_files(session, tmp_path, principal, contents)
    garbage = [blob_path(artifact_id, str(n) * 64) for n in range(1, 5)]
    for path in garbage:
        _put_old(session, path)
    monkeypatch.setattr(sweep_module, "_FILE_BATCH", 2)
    with caplog.at_level(logging.WARNING):
        report = _sweep(session)
    assert report == SweepReport(named_files_kept=4, blobs_deleted=4)
    assert caplog.text.count("are named by a version or an upload but have no record") == 1
    assert "4 files of artifact 'models.churn'" in caplog.text
    storage = _storage(session)
    assert all(storage.exists(blob_path(artifact_id, _sha(d))) for d in contents.values())
    assert not any(storage.exists(path) for path in garbage)
