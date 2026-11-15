"""Adding and reading artifact versions."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import inspect
import json
import re
import uuid
from collections.abc import Callable, Generator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    ActionAllowList,
    _prepared_catalog,
    artifact_document,
    artifact_service,
    deploy_assets,
    ensure_table,
    python_task_run,
    store_version,
)
from sqlalchemy import Executable, Select, delete, event, insert, select, update
from sqlalchemy.orm import ORMExecuteState, Session

from lakana_core.artifacts.service import (
    COMMIT_NOT_WRITTEN,
    MAX_OPEN_UPLOADS,
    STORAGE_NOT_WRITTEN,
    UPLOAD_GONE,
    ArtifactService,
    ArtifactSource,
    BlobUpload,
    VersionView,
)
from lakana_core.artifacts.store import (
    blob_path,
    blobs_prefix,
    lock_artifact,
    manifest_path,
    stored_hashes,
)
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    BoundTask,
    PrincipalRef,
    RunContext,
    Securable,
    TaskScope,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.config import ArtifactsConfig
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    StorageUnavailableError,
    ValidationError,
)
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ArtifactBlob,
    ArtifactUpload,
    ArtifactVersion,
    AuditEvent,
    AuditEventType,
    Connection,
    Environment,
    LineageEdge,
    Namespace,
    Principal,
    PrincipalType,
    Run,
    Table,
    TableType,
    TaskRun,
)
from lakana_core.pagination import encode_key_cursor
from lakana_core.projects.definition import DefiningFile
from lakana_core.runs.lockdown import SUPERSEDED_MESSAGE, TABLE_TASK_MESSAGE
from lakana_core.storage.base import StoredObject, StreamWriter
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver

_MODEL = b'{"factor": 2}\n'
_NOTES = b"notes"
_MODEL_SHA = hashlib.sha256(_MODEL).hexdigest()
_NOTES_SHA = hashlib.sha256(_NOTES).hexdigest()
_FILES = {"model.json": _MODEL, "docs/notes.txt": _NOTES}
_OTHER = TableRef("dev", NamespacePath.from_parts(["models"]), "ghost")


def _events(session: Session) -> list[str]:
    """List the audit event types recorded so far, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of str
        The event types.
    """
    rows = session.scalars(select(AuditEvent).order_by(AuditEvent.occurred_at, AuditEvent.id))
    return [row.event_type.value for row in rows]


def _objects(session: Session, prefix: str) -> list[str]:
    """List the objects under a prefix of the `dev` environment's storage.

    Parameters
    ----------
    session
        Database session.
    prefix
        Root-relative prefix.

    Returns
    -------
    list of str
        Root-relative paths, sorted.
    """
    return sorted(StorageResolver(session).for_environment("dev").storage.list(prefix))


def _artifact_id(session: Session) -> uuid.UUID:
    """Return the id of `models.churn`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The artifact's id.
    """
    return session.scalars(select(Table.id).where(Table.name == "churn")).one()


def test_three_steps_store_version_one(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    assert upload.missing == tuple(sorted([_MODEL_SHA, _NOTES_SHA]))
    row = session.scalars(select(Table).where(Table.name == "churn")).one()
    assert (row.table_type, row.owner_id) == (TableType.ARTIFACT, principal.id)
    assert service.put_blob(
        principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, [_MODEL[:5], _MODEL[5:]]
    )
    assert service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _NOTES_SHA, [_NOTES])
    assert (
        service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _NOTES_SHA, [_NOTES]) is False
    )
    result = service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)

    version = result.version
    assert (version.version, version.size_bytes, version.file_count, version.is_directory) == (
        1,
        len(_MODEL) + len(_NOTES),
        2,
        True,
    )
    assert (version.created_by, version.deleted_at, version.task_run_id) == ("admin", None, None)
    assert [file.path for file in version.files] == ["docs/notes.txt", "model.json"]
    assert result.deleted_versions == ()
    assert _objects(session, blobs_prefix(row.id)) == sorted(
        [blob_path(row.id, _MODEL_SHA), blob_path(row.id, _NOTES_SHA)]
    )
    stored = StorageResolver(session).for_environment("dev").storage.read(manifest_path(row.id, 1))
    assert hashlib.sha256(stored).hexdigest() == version.manifest_sha256
    assert json.loads(stored)["files"][1]["path"] == "model.json"
    (kept,) = session.scalars(select(ArtifactUpload)).all()
    assert kept.committed_version == 1  # kept until it expires, so a repeated commit can answer
    assert _events(session)[-2:] == ["ARTIFACT_CREATED", "ARTIFACT_VERSION_ADDED"]

    view = service.get_artifact(principal, ARTIFACT_REF)
    assert (view.current_version, view.versions, view.size_bytes) == (1, 1, version.size_bytes)
    assert [a.ref for a in service.list_artifacts(principal, "dev").items] == [ARTIFACT_REF]
    assert service.list_artifacts(principal, "dev", NamespacePath.from_parts(["gold"])).items == []
    assert service.get_version(principal, ARTIFACT_REF) == version
    assert service.get_version(principal, ARTIFACT_REF, 1) == version
    listed = service.list_versions(principal, ARTIFACT_REF)
    assert [(v.version, v.files) for v in listed] == [(1, ())]


def test_artifacts_carry_their_producer_and_keep_versions(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A declared artifact names its file and its retention; an uploaded one has neither."""
    service = artifact_service(session, tmp_path)
    other = TableRef("dev", NamespacePath.from_parts(["models"]), "uploaded")
    store_version(service, principal, ARTIFACT_REF, _FILES)
    store_version(service, principal, other, _FILES)
    zeta = TableRef("dev", NamespacePath.from_parts(["models"]), "zeta")
    store_version(service, principal, zeta, _FILES)
    assert [
        (a.defined_by, a.keep_versions) for a in service.list_artifacts(principal, "dev").items
    ] == [(None, None)] * 3
    deploy_assets(
        session,
        "dev",
        "analytics",
        {
            "models.churn": {
                "kind": "python",
                "output": "artifact",
                "keep_versions": 3,
                "path": "assets/models/m.py",
            },
            "models.zeta": {
                "kind": "python",
                "output": "artifact",
                "keep_versions": 5,
                "path": "assets/models/z.py",
            },
        },
    )
    by_name = {a.ref.name: a for a in service.list_artifacts(principal, "dev").items}
    assert by_name["churn"].defined_by == DefiningFile(
        "analytics", "assets/models/m.py", "artifact"
    )
    assert by_name["churn"].keep_versions == 3
    assert by_name["zeta"].defined_by == DefiningFile("analytics", "assets/models/z.py", "artifact")
    assert by_name["zeta"].keep_versions == 5
    assert (by_name["uploaded"].defined_by, by_name["uploaded"].keep_versions) == (None, None)


def test_artifacts_page_by_namespace_then_name(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """One page at a time, in (namespace, name) order, with no skip and no repeat."""
    service = artifact_service(session, tmp_path)
    refs = [
        TableRef("dev", NamespacePath.from_parts(parts), name)
        for parts, name in [(["models"], "b"), (["models"], "a"), (["gold"], "z"), (["gold"], "a")]
    ]
    _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[NamespacePath.from_parts(["gold"])],
    )
    for ref in refs:
        store_version(service, principal, ref, _FILES)
    seen: list[str] = []
    cursor = None
    while True:
        page = service.list_artifacts(principal, "dev", limit=1, cursor=cursor)
        assert len(page.items) == 1
        seen.append(f"{page.items[0].ref.namespace}.{page.items[0].ref.name}")
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == ["gold.a", "gold.z", "models.a", "models.b"]


def test_a_forged_artifact_cursor_reveals_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The listing is filtered by what the caller may read before the cursor applies."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    blind = ArtifactService(
        session, StorageResolver(session), ActionAllowList(Action.READ_ENVIRONMENT)
    )
    forged = encode_key_cursor("artifacts", json.dumps(['["a"]', "a"]))
    page = blind.list_artifacts(principal, "dev", limit=1, cursor=forged)
    assert page.items == [] and page.next_cursor is None


def test_an_unreadable_artifact_sorting_first_does_not_cost_the_reader_a_page(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The read filter runs before paging: page 1 at limit=1 holds the first readable one."""
    service = artifact_service(session, tmp_path)
    other = TableRef("dev", ARTIFACT_REF.namespace, "other")
    store_version(service, principal, ARTIFACT_REF, _FILES)
    store_version(service, principal, other, {"w": b"w"}, is_directory=False)

    class OnlyOther(ActionAllowList):
        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            return securable.table == "other" or action is Action.READ_ENVIRONMENT

    narrow = ArtifactService(session, StorageResolver(session), OnlyOther(Action.READ_ENVIRONMENT))
    page = narrow.list_artifacts(principal, "dev", limit=1)
    assert [a.ref.name for a in page.items] == ["other"]
    assert page.next_cursor is None


def test_a_second_version_uploads_only_what_the_store_lacks(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    changed = {"model.json": b'{"factor": 3}\n', "docs/notes.txt": _NOTES}
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(changed))
    assert upload.missing == (hashlib.sha256(changed["model.json"]).hexdigest(),)
    service.put_blob(
        principal, ARTIFACT_REF, upload.upload_id, upload.missing[0], [changed["model.json"]]
    )
    second = service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert second.version.version == 2
    assert [v.version for v in service.list_versions(principal, ARTIFACT_REF)] == [2, 1]
    assert service.get_artifact(principal, ARTIFACT_REF).current_version == 2
    assert _events(session).count("ARTIFACT_CREATED") == 1


def test_a_single_file_artifact(session: Session, tmp_path: Path, principal: PrincipalRef) -> None:
    service = artifact_service(session, tmp_path)
    result = store_version(
        service, principal, ARTIFACT_REF, {"model.json": _MODEL}, is_directory=False
    )
    assert (result.version.is_directory, result.version.file_count) == (False, 1)
    assert result.version.manifest_sha256 == (
        "b97616016d0d6bd401c61c158cbf7405bc346d3d6ee6c22f8ed4099a3fe87c81"
    )


def test_a_body_longer_than_declared_is_cut_off_and_nothing_is_stored(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Checked as bytes arrive; a refused file leaves no object and no row."""
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(
        principal, ARTIFACT_REF, artifact_document({"model.json": _MODEL})
    )
    seen: list[int] = []

    def endless() -> Iterator[bytes]:
        for index in range(1000):
            seen.append(index)
            yield b"x" * 10

    with pytest.raises(ValidationError) as longer:
        service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, endless())
    assert str(longer.value) == (
        f"The file is longer than the {len(_MODEL)} bytes its manifest declares; "
        "nothing was stored."
    )
    assert len(seen) == 2
    for chunks in ([_MODEL[:-1]], [b"y" * len(_MODEL)], []):
        with pytest.raises(ValidationError, match=re.escape("size or SHA-256 differs")):
            service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, chunks)
    assert _objects(session, blobs_prefix(_artifact_id(session))) == []
    assert session.scalars(select(ArtifactBlob)).all() == []
    with pytest.raises(ValidationError, match=re.escape("names no file with that SHA-256")):
        service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _NOTES_SHA, [_NOTES])
    with pytest.raises(ValidationError, match="64 hex digits"):
        service.put_blob(principal, ARTIFACT_REF, upload.upload_id, "../escape", [b""])


def test_a_manifest_over_a_limit_creates_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(
        session, tmp_path, config=ArtifactsConfig(max_file_bytes=4, max_bytes=8, max_files=2)
    )
    with pytest.raises(ValidationError, match="LAKANA_ARTIFACTS__MAX_FILE_BYTES"):
        service.begin_upload(principal, ARTIFACT_REF, artifact_document({"model.json": _MODEL}))
    with pytest.raises(ValidationError, match="exactly the keys"):
        service.begin_upload(principal, ARTIFACT_REF, {"files": []})
    assert session.scalars(select(Table).where(Table.name == "churn")).all() == []
    assert "ARTIFACT_CREATED" not in _events(session)


def test_commit_refuses_while_files_are_missing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, [_MODEL])
    with pytest.raises(ConflictError) as caught:
        service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert str(caught.value) == (
        "1 of this upload's files are not stored yet; upload them and commit again."
    )
    assert session.scalars(select(ArtifactVersion)).all() == []
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _NOTES_SHA, [_NOTES])
    assert service.commit_upload(principal, ARTIFACT_REF, upload.upload_id).version.version == 1


def test_an_expired_or_foreign_upload_is_refused_alike(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    now = [datetime(2026, 10, 4, 12, 0, tzinfo=UTC)]
    service = artifact_service(
        session, tmp_path, config=ArtifactsConfig(upload_ttl_seconds=60), clock=lambda: now[0]
    )
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    assert upload.expires_at == now[0] + timedelta(seconds=60)
    other = dataclasses.replace(principal, id=uuid.uuid4(), name="other")
    for actor, upload_id in ((other, upload.upload_id), (principal, uuid.uuid4())):
        with pytest.raises(ConflictError) as put:
            service.put_blob(actor, ARTIFACT_REF, upload_id, _MODEL_SHA, [_MODEL])
        assert str(put.value) == UPLOAD_GONE
        with pytest.raises(ConflictError) as commit:
            service.commit_upload(actor, ARTIFACT_REF, upload_id)
        assert str(commit.value) == UPLOAD_GONE
    blob = service.receive_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA)
    blob.write(_MODEL)
    now[0] += timedelta(seconds=60)
    with pytest.raises(ConflictError) as late:
        blob.finish()
    assert str(late.value) == UPLOAD_GONE
    with pytest.raises(ConflictError):
        service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert _objects(session, blobs_prefix(_artifact_id(session))) == []


def _attempt(session: Session, principal: PrincipalRef) -> tuple[TaskRun, PrincipalRef]:
    """Put a Python task that produces `models.churn` into `running`.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.

    Returns
    -------
    tuple
        The task run and the principal as its bound token authenticates it.
    """
    ensure_table(session, "dev", "silver", "rows")
    task = python_task_run(
        session,
        principal.id,
        asset="models.churn",
        inputs={"rows": "silver.rows"},
        extra={"output": "artifact"},
    )
    bound = BoundTask(
        task_run_id=task.id,
        claim=1,
        asset="models.churn",
        inputs=("silver.rows",),
        readable=("silver.rows",),
        output="artifact",
    )
    actor = dataclasses.replace(
        principal,
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4(), bound=bound),
        run_context=RunContext(task_run_id=task.id, claim=1),
    )
    return task, actor


def test_a_version_records_the_attempt_that_made_it(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    task, actor = _attempt(session, principal)
    result = store_version(service, actor, ARTIFACT_REF, _FILES)
    run = session.get(Run, task.run_id)
    assert run is not None
    assert (result.version.task_run_id, result.version.run_id, result.version.job_version_id) == (
        task.id,
        run.id,
        run.job_version_id,
    )
    assert result.version.commit_sha == "a" * 40
    row = session.scalars(select(ArtifactVersion)).one()
    assert (row.task_run_id, row.claim) == (task.id, 1)


def _statements_of(session: Session, call: Callable[[], object]) -> list[str]:
    """Run `call` and return the SQL statements it sent to the database.

    Parameters
    ----------
    session
        The session whose engine is watched.
    call
        What to run.

    Returns
    -------
    list of str
        The statements, in order.
    """
    seen: list[str] = []
    engine = session.get_bind()

    def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return seen


def test_listing_versions_sends_a_fixed_number_of_statements_and_no_manifest(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A-I2: 2 versions and 6 cost the same, and no statement selects `manifest_json`."""
    service = artifact_service(session, tmp_path)
    task, actor = _attempt(session, principal)
    for number in range(2):
        store_version(service, actor, ARTIFACT_REF, {"m": bytes([number])}, is_directory=False)
    few = _statements_of(session, lambda: service.list_versions(principal, ARTIFACT_REF))
    for number in range(2, 6):
        store_version(service, actor, ARTIFACT_REF, {"m": bytes([number])}, is_directory=False)
    service.delete_version(principal, ARTIFACT_REF, 1)
    many: list[VersionView] = []
    statements = _statements_of(
        session, lambda: many.extend(service.list_versions(principal, ARTIFACT_REF))
    )
    assert len(statements) == len(few)
    assert not [text for text in statements if "manifest_json" in text]
    assert [v.version for v in many] == [6, 5, 4, 3, 2, 1]
    run = session.get(Run, task.run_id)
    assert run is not None
    assert {v.run_id for v in many} == {run.id}
    assert {v.created_by for v in many} == {principal.name}
    assert [(v.version, v.deleted_by) for v in many if v.deleted_at] == [(1, principal.name)]
    assert all(v.deleted_by is None for v in many if v.deleted_at is None)


@pytest.mark.parametrize("how", ["reclaimed", "cancelled"])
def test_a_superseded_attempt_cannot_commit_a_version(
    session: Session, tmp_path: Path, principal: PrincipalRef, how: str
) -> None:
    """The commit guard refuses an attempt that is no longer the current one."""
    service = artifact_service(session, tmp_path)
    task, actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES))
    for digest, data in ((_MODEL_SHA, _MODEL), (_NOTES_SHA, _NOTES)):
        service.put_blob(actor, ARTIFACT_REF, upload.upload_id, digest, [data])
    if how == "reclaimed":
        session.execute(update(TaskRun).where(TaskRun.id == task.id).values(claim_count=2))
    else:
        session.execute(update(Run).where(Run.id == task.run_id).values(cancel_requested=True))
    session.commit()
    with pytest.raises(PermissionDenied) as caught:
        service.commit_upload(actor, ARTIFACT_REF, upload.upload_id)
    assert str(caught.value) == SUPERSEDED_MESSAGE
    assert session.scalars(select(ArtifactVersion)).all() == []
    assert _objects(session, f"artifacts/{_artifact_id(session).hex}/manifests") == []
    assert _events(session)[-1] == "PROTECTED_WRITE_DENIED"
    with pytest.raises(PermissionDenied):
        service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES))


def test_an_upload_belongs_to_the_attempt_that_began_it(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    _, actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES))
    outside = dataclasses.replace(actor, task_scope=None, run_context=None)
    with pytest.raises(ConflictError) as caught:
        service.commit_upload(outside, ARTIFACT_REF, upload.upload_id)
    assert str(caught.value) == UPLOAD_GONE


def test_a_protected_environment_takes_versions_only_from_a_running_task(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    _, actor = _attempt(session, principal)
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=True))
    session.commit()
    with pytest.raises(PermissionDenied, match="is protected"):
        service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    assert session.scalars(select(Table).where(Table.name == "churn")).all() == []
    assert store_version(service, actor, ARTIFACT_REF, _FILES).version.version == 1


def test_a_table_assets_token_cannot_add_an_artifact_version(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The guard refuses it right after authorization, and audits the refusal."""
    service = artifact_service(session, tmp_path)
    ensure_table(session, "dev", "silver", "rows")
    task = python_task_run(
        session, principal.id, asset="models.churn", inputs={"rows": "silver.rows"}
    )
    bound = BoundTask(task.id, 1, "models.churn", ("silver.rows",), ("silver.rows",), "table")
    table_task = dataclasses.replace(
        principal,
        task_scope=TaskScope("dev", [], uuid.uuid4(), bound),
        run_context=RunContext(task_run_id=task.id, claim=1),
    )
    with pytest.raises(PermissionDenied) as caught:
        service.begin_upload(table_task, ARTIFACT_REF, artifact_document(_FILES))
    assert str(caught.value) == TABLE_TASK_MESSAGE.format(asset="models.churn")
    assert session.scalars(select(Table).where(Table.name == "churn")).all() == []
    denied = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PROTECTED_WRITE_DENIED)
    ).one()
    assert denied.detail["reason"] == "output_kind"
    assert denied.principal_id == principal.id


def test_creating_needs_create_table_and_a_free_name(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    writer_only = ActionAllowList(Action.WRITE_TABLE)
    service = artifact_service(session, tmp_path, writer_only)
    with pytest.raises(PermissionDenied):
        service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    assert (Action.CREATE_TABLE in [a for a, _ in writer_only.seen]) is True
    assert session.scalars(select(Table).where(Table.name == "churn")).all() == []

    allowed = artifact_service(session, tmp_path)
    ensure_table(session, "dev", "models", "taken")
    taken = TableRef("dev", ARTIFACT_REF.namespace, "taken")
    with pytest.raises(ValidationError) as caught:
        allowed.begin_upload(principal, taken, artifact_document(_FILES))
    assert str(caught.value) == (
        "models.taken is a table, not an artifact; drop it first, or choose another name."
    )
    missing_namespace = TableRef("dev", NamespacePath.from_parts(["nowhere"]), "m")
    with pytest.raises(NotFoundError, match=re.escape("Namespace nowhere not found in 'dev'.")):
        allowed.begin_upload(principal, missing_namespace, artifact_document(_FILES))
    with pytest.raises(ValidationError, match="Artifact name"):
        allowed.begin_upload(
            principal, TableRef("dev", ARTIFACT_REF.namespace, "a.b"), artifact_document(_FILES)
        )


def test_an_unreadable_artifact_and_a_missing_one_are_refused_alike(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Without READ_TABLE the answer does not depend on whether it exists."""
    store_version(artifact_service(session, tmp_path), principal, ARTIFACT_REF, _FILES)
    blind = ArtifactService(
        session, StorageResolver(session), ActionAllowList(Action.READ_ENVIRONMENT)
    )
    assert blind.list_artifacts(principal, "dev").items == []
    calls = (
        lambda ref: blind.get_artifact(principal, ref),
        lambda ref: blind.list_versions(principal, ref),
        lambda ref: blind.get_version(principal, ref),
        lambda ref: blind.get_version(principal, ref, 1),
        lambda ref: blind.open_blob(principal, ref, 1, _MODEL_SHA),
        lambda ref: blind.begin_upload(principal, ref, artifact_document(_FILES)),
        lambda ref: blind.commit_upload(principal, ref, uuid.uuid4()),
        lambda ref: blind.receive_blob(principal, ref, uuid.uuid4(), _MODEL_SHA),
    )
    for call in calls:
        texts = []
        for ref in (ARTIFACT_REF, _OTHER):
            with pytest.raises(PermissionDenied) as caught:
                call(ref)
            texts.append(str(caught.value))
        assert texts[0] == texts[1]
        assert "churn" not in texts[0]


def test_reading_says_what_is_missing_to_one_who_may_read(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    with pytest.raises(NotFoundError) as absent:
        service.get_artifact(principal, _OTHER)
    assert str(absent.value) == "Artifact 'models.ghost' not found in 'dev'."
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    with pytest.raises(NotFoundError) as empty:
        service.get_version(principal, ARTIFACT_REF)
    assert str(empty.value) == "models.churn has no version yet; run the asset that produces it."
    assert service.get_artifact(principal, ARTIFACT_REF).current_version is None
    for digest, data in ((_MODEL_SHA, _MODEL), (_NOTES_SHA, _NOTES)):
        service.put_blob(principal, ARTIFACT_REF, upload.upload_id, digest, [data])
    service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    with pytest.raises(NotFoundError, match=re.escape("models.churn has no version 2.")):
        service.get_version(principal, ARTIFACT_REF, 2)
    ensure_table(session, "dev", "models", "taken")
    with pytest.raises(ValidationError) as table:
        service.get_artifact(principal, TableRef("dev", ARTIFACT_REF.namespace, "taken"))
    assert str(table.value) == (
        "models.taken is a table, not an artifact. See `lakana table get models.taken`."
    )
    with pytest.raises(NotFoundError, match=re.escape("No environment named 'nope'.")):
        service.list_artifacts(principal, "nope")


def _downloads(session: Session) -> list[AuditEvent]:
    """List the `ARTIFACT_DOWNLOADED` events recorded so far.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The events, oldest first.
    """
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.ARTIFACT_DOWNLOADED)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )


def _bound(principal: PrincipalRef) -> PrincipalRef:
    """Give a principal a bound task token that reads `models.churn`.

    Parameters
    ----------
    principal
        The principal.

    Returns
    -------
    PrincipalRef
        The principal as that token authenticates it.
    """
    return dataclasses.replace(
        principal,
        task_scope=TaskScope(
            environment="dev",
            asset_scope=[],
            token_id=uuid.uuid4(),
            bound=BoundTask(uuid.uuid4(), 1, "gold.scores", ("models.churn",), ("models.churn",)),
        ),
    )


def test_download_streams_the_stored_bytes(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    download = service.open_blob(principal, ARTIFACT_REF, 1, _MODEL_SHA)
    assert (download.size, b"".join(download.chunks)) == (len(_MODEL), _MODEL)
    assert b"".join(service.open_blob(_bound(principal), ARTIFACT_REF, 1, _NOTES_SHA).chunks) == (
        _NOTES
    )
    with pytest.raises(
        NotFoundError, match=re.escape("Version 1 of models.churn has no file with that SHA-256.")
    ):
        service.open_blob(principal, ARTIFACT_REF, 1, "0" * 64)
    with pytest.raises(NotFoundError, match=re.escape("models.churn has no version 5.")):
        service.open_blob(principal, ARTIFACT_REF, 5, _MODEL_SHA)
    with pytest.raises(NotFoundError, match=re.escape("has no file with that SHA-256")):
        service.open_blob(principal, ARTIFACT_REF, 1, "../" + "a" * 61)
    storage = StorageResolver(session).for_environment("dev").storage
    storage.delete(blob_path(_artifact_id(session), _MODEL_SHA))
    with pytest.raises(NotFoundError) as lost:
        service.open_blob(principal, ARTIFACT_REF, 1, _MODEL_SHA)
    assert str(lost.value) == (
        "A file of version 1 of models.churn is missing from storage, and the catalog still "
        "lists it as stored, so running the asset again would not store it. Delete this version "
        "and every other version that has the file (`lakana artifact versions delete`), then run "
        "the asset again."
    )
    assert "artifacts/" not in str(lost.value)


def test_a_download_is_audited_once_per_principal_and_version(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Two files of one version by one principal are one event."""
    now = [datetime.now(UTC)]
    service = artifact_service(session, tmp_path, clock=lambda: now[0])
    store_version(service, principal, ARTIFACT_REF, _FILES)
    store_version(service, principal, ARTIFACT_REF, {"model.json": _MODEL})
    other = dataclasses.replace(principal, id=uuid.uuid4(), name="other")
    session.add(Principal(id=other.id, type=other.type, name=other.name, is_active=True))
    session.commit()

    # A bound token writes no event, even for a version nobody fetched before; it shares the
    # id of the principal below, so a recording would also hide that principal's own event.
    b"".join(service.open_blob(_bound(principal), ARTIFACT_REF, 2, _MODEL_SHA).chunks)
    assert _downloads(session) == []

    for digest in (_MODEL_SHA, _NOTES_SHA):
        b"".join(service.open_blob(principal, ARTIFACT_REF, 1, digest).chunks)
    assert len(_downloads(session)) == 1
    event = _downloads(session)[0]
    assert event.principal_id == principal.id
    assert event.detail == {
        "artifact": "models.churn",
        "artifact_id": str(_artifact_id(session)),
        "version": 1,
        "file_count": 2,
        "size_bytes": len(_MODEL) + len(_NOTES),
        "principal": "admin",
    }
    assert "model.json" not in json.dumps(event.detail)

    service.open_blob(other, ARTIFACT_REF, 1, _MODEL_SHA)
    service.open_blob(principal, ARTIFACT_REF, 2, _MODEL_SHA)
    assert len(_downloads(session)) == 3
    now[0] += timedelta(hours=2)
    service.open_blob(principal, ARTIFACT_REF, 1, _MODEL_SHA)
    assert len(_downloads(session)) == 4
    assert [e.detail["version"] for e in _downloads(session)] == [1, 1, 2, 1]
    assert _downloads(session)[3].principal_id == principal.id


def test_a_recreated_artifact_is_not_taken_for_the_old_one(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    service.open_blob(principal, ARTIFACT_REF, 1, _MODEL_SHA)
    session.delete(session.get(Table, _artifact_id(session)))
    session.commit()
    store_version(service, principal, ARTIFACT_REF, _FILES)
    service.open_blob(principal, ARTIFACT_REF, 1, _MODEL_SHA)
    assert len(_downloads(session)) == 2


def test_a_damaged_stored_manifest_is_reported_not_raised(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    session.execute(update(ArtifactVersion).values(manifest_json={"files": "x"}))
    session.commit()
    for call in (
        lambda: service.get_version(principal, ARTIFACT_REF, 1),
        lambda: service.open_blob(principal, ARTIFACT_REF, 1, _MODEL_SHA),
    ):
        with pytest.raises(ValidationError) as caught:
            call()
        assert str(caught.value) == (
            "The stored manifest of version 1 of models.churn is damaged; delete the version and "
            "run the asset again."
        )
    assert [v.version for v in service.list_versions(principal, ARTIFACT_REF)] == [1]


def test_a_commit_records_an_edge_from_every_source(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    ensure_table(session, "dev", "silver", "rows")
    base = TableRef("dev", ARTIFACT_REF.namespace, "base")
    store_version(service, principal, base, {"w": b"w"}, is_directory=False)
    store_version(service, principal, base, {"w": b"v"}, is_directory=False)
    result = store_version(
        service,
        principal,
        ARTIFACT_REF,
        _FILES,
        sources=[ArtifactSource("silver.rows"), ArtifactSource("models.base", 1)],
    )
    edges = {
        (edge.source_name, edge.source_snapshot_id, edge.target_name, edge.target_snapshot_id)
        for edge in session.scalars(
            select(LineageEdge).where(LineageEdge.target_table_id == _artifact_id(session))
        )
    }
    assert edges == {
        ("silver.rows", None, "models.churn", result.version.version),
        ("models.base", 1, "models.churn", result.version.version),
    }
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    with pytest.raises(NotFoundError):
        service.commit_upload(
            principal, ARTIFACT_REF, upload.upload_id, sources=[ArtifactSource("silver.nope")]
        )
    assert service.get_artifact(principal, ARTIFACT_REF).current_version == 1


def test_a_stored_file_with_another_size_makes_the_manifest_wrong(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"model.json": _MODEL}, is_directory=False)
    lying = artifact_document({"model.json": _MODEL}, is_directory=False)
    lying["files"][0]["size"] = 3  # ty: ignore[not-subscriptable]
    with pytest.raises(ValidationError) as caught:
        service.begin_upload(principal, ARTIFACT_REF, lying)
    assert str(caught.value) == (
        f"model.json: a file with this SHA-256 is already stored with {len(_MODEL)} bytes, "
        "not 3; the manifest is wrong."
    )


def test_a_commit_that_cannot_write_the_manifest_changes_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    for digest, data in ((_MODEL_SHA, _MODEL), (_NOTES_SHA, _NOTES)):
        service.put_blob(principal, ARTIFACT_REF, upload.upload_id, digest, [data])
    real = FsspecStorage.open_write

    def failing(self: FsspecStorage, path: str) -> StreamWriter:
        if "/manifests/" in path:
            raise OSError("/secret/path: disk full")
        return real(self, path)

    monkeypatch.setattr(FsspecStorage, "open_write", failing)
    with pytest.raises(ConflictError) as refused:
        service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert str(refused.value) == (
        "This version was not committed because its storage could not be written. The upload is "
        "still open: commit it again when storage is available."
    )
    assert session.scalars(select(ArtifactUpload.id)).all() == [upload.upload_id]
    assert session.scalars(select(ArtifactVersion)).all() == []
    assert "ARTIFACT_VERSION_ADDED" not in _events(session)
    monkeypatch.setattr(FsspecStorage, "open_write", real)
    assert service.commit_upload(principal, ARTIFACT_REF, upload.upload_id).version.version == 1


def test_an_artifact_dropped_under_an_operation_is_reported_as_gone(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    blob = service.receive_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA)
    blob.write(_MODEL)
    artifact_id = _artifact_id(session)
    session.delete(session.get(Table, artifact_id))
    session.commit()
    with pytest.raises(ConflictError) as finished:
        blob.finish()
    assert str(finished.value) == UPLOAD_GONE
    assert _objects(session, blobs_prefix(artifact_id)) == []

    again = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", lambda _s, _a: False)
    for call in (
        lambda: service.commit_upload(principal, ARTIFACT_REF, again.upload_id),
        lambda: service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES)),
    ):
        with pytest.raises(
            NotFoundError, match=re.escape("Artifact 'models.churn' not found in 'dev'.")
        ):
            call()


def test_two_creators_of_one_name_leave_one_artifact(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    monkeypatch.setattr("lakana_core.artifacts.service.resolve_table_id", lambda *_a: None)
    with pytest.raises(ConflictError) as caught:
        service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    assert str(caught.value) == "models.churn was created by someone else just now; retry."
    assert len(session.scalars(select(Table).where(Table.name == "churn")).all()) == 1


def test_a_closed_upload_refuses_further_use(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    blob = service.receive_blob(principal, ARTIFACT_REF, upload.upload_id, _NOTES_SHA)
    blob.write(_NOTES)
    assert blob.finish() is True
    blob.abort()
    for call in (lambda: blob.write(b"x"), blob.finish):
        with pytest.raises(ValueError, match="already closed"):
            call()
    assert session.scalars(select(ArtifactBlob.sha256)).all() == [_NOTES_SHA]


@contextlib.contextmanager
def _right_before_the_namespace_lock(session: Session, statement: Executable) -> Generator[None]:
    """Run `statement` as if another transaction committed it just before the name-space lock.

    Parameters
    ----------
    session
        The session the service under test uses.
    statement
        What the other transaction does.

    Yields
    ------
    None
        While the hook is installed.
    """

    def interleave(state: ORMExecuteState) -> None:
        query = state.statement
        if (
            isinstance(query, Select)
            and query._for_update_arg is not None
            and query.column_descriptions[0]["entity"] is Namespace
        ):
            state.session.connection().execute(statement)

    event.listen(session, "do_orm_execute", interleave)
    try:
        yield
    finally:
        event.remove(session, "do_orm_execute", interleave)


def test_creating_an_artifact_against_a_namespace_drop_leaves_nothing_behind(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point D: the name space vanishes before the creator's lock; no row, no upload."""
    service = artifact_service(session, tmp_path)
    with (
        _right_before_the_namespace_lock(session, delete(Namespace)),
        pytest.raises(NotFoundError, match=re.escape("Namespace models not found in 'dev'.")),
    ):
        service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    assert session.scalars(select(Table)).all() == []
    assert session.scalars(select(ArtifactUpload)).all() == []
    assert "ARTIFACT_CREATED" not in _events(session)


def test_a_namespace_drop_takes_the_lock_before_it_looks_for_artifacts(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point D: an artifact created right before the drop's lock is seen, not cascaded away."""
    artifact_service(session, tmp_path)
    ns_id = session.scalars(select(Namespace.id).where(Namespace.path == "models")).one()
    late = insert(Table).values(
        id=uuid.uuid4(), namespace_id=ns_id, name="late", table_type=TableType.ARTIFACT
    )
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    with (
        _right_before_the_namespace_lock(session, late),
        pytest.raises(ConflictError, match="still holds 1 artifact;"),
    ):
        catalog.drop_namespace(principal, "dev", ARTIFACT_REF.namespace)
    assert session.scalars(select(Table.name)).all() == ["late"]
    gone = delete(Namespace).where(Namespace.id == ns_id)
    with (
        _right_before_the_namespace_lock(session, gone),
        pytest.raises(NotFoundError, match=re.escape("Namespace models not found in 'dev'.")),
    ):
        catalog.drop_namespace(principal, "dev", ARTIFACT_REF.namespace)


def test_a_claim_reclaimed_between_the_check_and_the_lock_is_refused_under_lock(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point B: the guard's `record_pending` re-verifies the attempt inside the commit."""
    service = artifact_service(session, tmp_path)
    task, actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES))
    for digest, data in ((_MODEL_SHA, _MODEL), (_NOTES_SHA, _NOTES)):
        service.put_blob(actor, ARTIFACT_REF, upload.upload_id, digest, [data])
    real = lock_artifact

    def reclaimed_just_before(s: Session, artifact_id: uuid.UUID) -> bool:
        s.execute(update(TaskRun).where(TaskRun.id == task.id).values(claim_count=2))
        return real(s, artifact_id)

    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", reclaimed_just_before)
    with pytest.raises(PermissionDenied) as caught:
        service.commit_upload(actor, ARTIFACT_REF, upload.upload_id)
    assert str(caught.value) == SUPERSEDED_MESSAGE
    assert session.scalars(select(ArtifactVersion)).all() == []
    assert _objects(session, f"artifacts/{_artifact_id(session).hex}/manifests") == []
    assert _events(session)[-1] == "PROTECTED_WRITE_DENIED"
    assert "ARTIFACT_VERSION_ADDED" not in _events(session)


def test_no_request_parameter_sets_keep_versions() -> None:
    """Point C: only the bound token's `keep_versions` can prune, so no method takes one."""
    for name in ("begin_upload", "commit_upload", "receive_blob", "put_blob"):
        parameters = inspect.signature(getattr(ArtifactService, name)).parameters
        assert not [p for p in parameters if "keep" in p or "prune" in p]
    assert not [f for f in ArtifactsConfig.model_fields if "keep" in f]


def test_a_listing_shows_only_artifacts_the_caller_may_read(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Filtered per object, not per name space."""
    service = artifact_service(session, tmp_path)
    other = TableRef("dev", ARTIFACT_REF.namespace, "other")
    store_version(service, principal, ARTIFACT_REF, _FILES)
    store_version(service, principal, other, {"w": b"w"}, is_directory=False)

    class OnlyOther(ActionAllowList):
        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            return securable.table == "other" or action is Action.READ_ENVIRONMENT

    narrow = ArtifactService(session, StorageResolver(session), OnlyOther(Action.READ_ENVIRONMENT))
    assert [a.ref.name for a in narrow.list_artifacts(principal, "dev").items] == ["other"]
    assert [a.ref.name for a in service.list_artifacts(principal, "dev").items] == [
        "churn",
        "other",
    ]


def test_a_version_number_nobody_could_have_is_refused_with_a_bounded_message(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point G: a number from a request is never formatted unbounded, nor sent to the database."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    text = "models.churn has no version with that number; versions count up from 1."
    for number in (0, -1, 2**31, 10**5000):
        for call in (
            lambda n=number: service.get_version(principal, ARTIFACT_REF, n),
            lambda n=number: service.open_blob(principal, ARTIFACT_REF, n, _MODEL_SHA),
        ):
            with pytest.raises(NotFoundError) as caught:
                call()
            assert str(caught.value) == text


def test_hostile_sources_are_refused_without_being_echoed(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point G: a source's name and version come from a request."""
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    for source in (
        ArtifactSource("silver.rows\nforged"),
        ArtifactSource("x" * 5000),
        ArtifactSource(""),
        ArtifactSource("silver.rows", 10**5000),
        ArtifactSource("silver.rows", 0),
        ArtifactSource("silver.rows", True),
    ):
        with pytest.raises(ValidationError) as caught:
            service.commit_upload(principal, ARTIFACT_REF, upload.upload_id, sources=[source])
        assert "whole number from 1 to 2147483647" in str(caught.value)
        assert "forged" not in str(caught.value)
        assert len(str(caught.value)) < 300


def test_a_hostile_manifest_path_is_never_echoed_raw(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Point G: a path in a refusal passes through `shown`."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"model.json": _MODEL}, is_directory=False)
    lying = artifact_document({"model.json": _MODEL}, is_directory=False)
    lying["files"][0]["path"] = "evil\u202e\n" + "x" * 5000  # ty: ignore[not-subscriptable]
    lying["files"][0]["size"] = 3  # ty: ignore[not-subscriptable]
    with pytest.raises(ValidationError) as caught:
        service.begin_upload(principal, ARTIFACT_REF, lying)
    message = str(caught.value)
    assert "\n" not in message and "\u202e" not in message
    assert len(message) < 1500
    assert [u.committed_version for u in session.scalars(select(ArtifactUpload))] == [1]


def test_an_upload_of_one_artifact_is_not_usable_through_another(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, _OTHER, {"w": b"w"}, is_directory=False)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    for call in (
        lambda: service.put_blob(principal, _OTHER, upload.upload_id, _MODEL_SHA, [_MODEL]),
        lambda: service.commit_upload(principal, _OTHER, upload.upload_id),
    ):
        with pytest.raises(ConflictError) as caught:
            call()
        assert str(caught.value) == UPLOAD_GONE
    # A hand-made writer bound to the other artifact is refused at `finish` too.
    other_id = session.scalars(select(Table.id).where(Table.name == "ghost")).one()
    writer = (
        StorageResolver(session)
        .for_environment("dev")
        .storage.open_write(blob_path(other_id, _MODEL_SHA))
    )
    foreign = BlobUpload(
        session,
        writer,
        other_id,
        upload.upload_id,
        _MODEL_SHA,
        len(_MODEL),
        lambda: datetime.now(UTC),
        datetime.now(UTC) + timedelta(hours=1),
    )
    foreign.write(_MODEL)
    with pytest.raises(ConflictError):
        foreign.finish()
    assert _objects(session, blobs_prefix(other_id)) == [
        blob_path(other_id, hashlib.sha256(b"w").hexdigest())
    ]


def test_a_deleted_version_cannot_be_downloaded_and_shows_no_files(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    session.execute(
        update(ArtifactVersion).values(deleted_at=datetime.now(UTC), deleted_by=principal.id)
    )
    session.commit()
    with pytest.raises(NotFoundError) as gone:
        service.open_blob(principal, ARTIFACT_REF, 1, _MODEL_SHA)
    assert str(gone.value) == (
        "Version 1 of models.churn was deleted; its bytes are gone. "
        "Read a version that exists: `lakana artifact versions list models.churn --env dev` "
        "shows which."
    )
    deleted = service.get_version(principal, ARTIFACT_REF, 1)
    assert (deleted.files, deleted.deleted_by) == ((), "admin")
    assert service.get_artifact(principal, ARTIFACT_REF).current_version is None
    assert service.get_artifact(principal, ARTIFACT_REF).size_bytes == 0
    assert _downloads(session) == []


def _more_principals(session: Session, count: int) -> list[PrincipalRef]:
    """Persist some more principals.

    Parameters
    ----------
    session
        Database session.
    count
        How many.

    Returns
    -------
    list of PrincipalRef
        The principals.
    """
    made = [
        PrincipalRef(id=uuid.uuid4(), name=f"writer{i}", type=PrincipalType.USER)
        for i in range(count)
    ]
    session.add_all(Principal(id=m.id, type=m.type, name=m.name, is_active=True) for m in made)
    session.commit()
    return made


def test_an_expired_upload_row_is_gone_after_the_next_begin(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    now = [datetime(2026, 10, 4, 12, 0, tzinfo=UTC)]
    service = artifact_service(
        session, tmp_path, config=ArtifactsConfig(upload_ttl_seconds=60), clock=lambda: now[0]
    )
    old = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"a": b"a"}))
    now[0] += timedelta(seconds=60)
    new = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"b": b"b"}))
    assert session.scalars(select(ArtifactUpload.id)).all() == [new.upload_id]
    assert old.upload_id != new.upload_id


def test_a_tasks_second_begin_in_one_attempt_replaces_its_own_upload(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    _, actor = _attempt(session, principal)
    first = service.begin_upload(actor, ARTIFACT_REF, artifact_document({"a": b"a"}))
    other = _more_principals(session, 1)[0]
    kept = service.begin_upload(other, ARTIFACT_REF, artifact_document({"c": b"c"}))
    second = service.begin_upload(actor, ARTIFACT_REF, artifact_document({"b": b"b"}))
    ids = set(session.scalars(select(ArtifactUpload.id)).all())
    assert ids == {kept.upload_id, second.upload_id}
    assert first.upload_id not in ids
    with pytest.raises(ConflictError):
        service.commit_upload(actor, ARTIFACT_REF, first.upload_id)


def test_the_replacement_touches_only_this_attempts_uploads_of_this_artifact(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """N1: another attempt's upload, and this attempt's upload of another artifact, survive."""
    service = artifact_service(session, tmp_path)
    task, actor = _attempt(session, principal)
    _, rival = _attempt(session, principal)
    elsewhere = TableRef("dev", ARTIFACT_REF.namespace, "elsewhere")
    store_version(service, principal, elsewhere, {"w": b"w"}, is_directory=False)
    elsewhere_id = session.scalars(select(Table.id).where(Table.name == "elsewhere")).one()
    now = datetime.now(UTC)
    session.add(
        ArtifactUpload(
            id=uuid.uuid4(),
            artifact_id=elsewhere_id,
            principal_id=principal.id,
            manifest_sha256="0" * 64,
            manifest_json={"files": []},
            task_run_id=task.id,
            claim=1,
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
    )
    session.commit()
    first = service.begin_upload(actor, ARTIFACT_REF, artifact_document({"a": b"a"}))
    rivals = service.begin_upload(rival, ARTIFACT_REF, artifact_document({"r": b"r"}))
    second = service.begin_upload(actor, ARTIFACT_REF, artifact_document({"b": b"b"}))
    on_churn = set(
        session.scalars(
            select(ArtifactUpload.id).where(ArtifactUpload.artifact_id != elsewhere_id)
        ).all()
    )
    assert on_churn == {rivals.upload_id, second.upload_id}
    assert first.upload_id not in on_churn
    assert session.scalars(
        select(ArtifactUpload.id).where(ArtifactUpload.artifact_id == elsewhere_id)
    ).all()


def test_a_free_name_in_a_protected_environment_does_not_reveal_that_it_is_protected(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """N2: without `CREATE_TABLE` the caller gets the plain denial, not the guard's."""
    artifact_service(session, tmp_path)
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=True))
    session.commit()
    writer = ArtifactService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))
    before = len(_events(session))
    with pytest.raises(PermissionDenied) as caught:
        writer.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    assert "protected" not in str(caught.value)
    assert len(_events(session)) == before


def test_make_room_runs_under_the_artifacts_lock(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count of open uploads is read after `lock_artifact`, never before."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    steps: list[str] = []
    real_lock = lock_artifact
    real_room = ArtifactService._make_room

    def lock(s: Session, artifact_id: uuid.UUID) -> bool:
        steps.append("lock")
        return real_lock(s, artifact_id)

    def room(self: ArtifactService, *args: object) -> None:
        steps.append("room")
        real_room(self, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", lock)
    monkeypatch.setattr(ArtifactService, "_make_room", room)
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"b": b"b"}))
    assert steps == ["lock", "room"]


def test_the_guard_refuses_before_the_kind_of_an_existing_table_is_named(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A table task whose asset already exists as a table gets the audited guard refusal."""
    service = artifact_service(session, tmp_path)
    ensure_table(session, "dev", "models", "churn")
    task = python_task_run(session, principal.id, asset="models.churn", inputs={})
    bound = BoundTask(task.id, 1, "models.churn", (), (), "table")
    table_task = dataclasses.replace(
        principal,
        task_scope=TaskScope("dev", [], uuid.uuid4(), bound),
        run_context=RunContext(task_run_id=task.id, claim=1),
    )
    with pytest.raises(PermissionDenied) as caught:
        service.begin_upload(table_task, ARTIFACT_REF, artifact_document(_FILES))
    assert str(caught.value) == TABLE_TASK_MESSAGE.format(asset="models.churn")
    denied = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PROTECTED_WRITE_DENIED)
    ).one()
    assert denied.detail["reason"] == "output_kind"


def test_a_bound_scope_without_a_run_context_is_refused_before_the_guard(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Authentication never produces one; a future caller that does gets no write."""
    service = artifact_service(session, tmp_path)
    _, actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES))
    headless = dataclasses.replace(actor, run_context=None)
    before = len(_events(session))
    for call in (
        lambda: service.begin_upload(headless, ARTIFACT_REF, artifact_document(_FILES)),
        lambda: service.receive_blob(headless, ARTIFACT_REF, upload.upload_id, _MODEL_SHA),
        lambda: service.commit_upload(headless, ARTIFACT_REF, upload.upload_id),
    ):
        with pytest.raises(PermissionDenied) as caught:
            call()
        assert str(caught.value) == SUPERSEDED_MESSAGE
    assert len(_events(session)) == before
    assert session.scalars(select(ArtifactVersion)).all() == []


def test_the_attempt_check_comes_after_authorization(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A caller with no privilege learns only the plain permission text, never the attempt's."""
    service = artifact_service(session, tmp_path)
    _, actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES))
    headless = dataclasses.replace(actor, run_context=None)
    nobody = ArtifactService(session, StorageResolver(session), ActionAllowList())
    for call in (
        lambda: nobody.begin_upload(headless, ARTIFACT_REF, artifact_document(_FILES)),
        lambda: nobody.receive_blob(headless, ARTIFACT_REF, upload.upload_id, _MODEL_SHA),
        lambda: nobody.commit_upload(headless, ARTIFACT_REF, upload.upload_id),
    ):
        with pytest.raises(PermissionDenied, match="may not WRITE_TABLE") as caught:
            call()
        assert SUPERSEDED_MESSAGE not in str(caught.value)


def _next_claim(session: Session, task: TaskRun, actor: PrincipalRef, claim: int) -> PrincipalRef:
    """Move a task run on to a new claim, as a drain release or a steal and re-claim does.

    Parameters
    ----------
    session
        Database session.
    task
        The task run.
    actor
        The attempt's principal.
    claim
        The new claim counter.

    Returns
    -------
    PrincipalRef
        The principal of the new attempt.
    """
    session.execute(update(TaskRun).where(TaskRun.id == task.id).values(claim_count=claim))
    session.commit()
    return dataclasses.replace(actor, run_context=RunContext(task_run_id=task.id, claim=claim))


def _upload_ids(session: Session) -> set[uuid.UUID]:
    """List the id of every upload row.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    set
        The ids.
    """
    return set(session.scalars(select(ArtifactUpload.id)).all())


def test_a_begin_under_the_next_claim_clears_the_open_upload_of_the_earlier_claim(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The earlier claim's upload can never commit, so it must not hold a slot."""
    service = artifact_service(session, tmp_path)
    task, first_claim = _attempt(session, principal)
    old = service.begin_upload(first_claim, ARTIFACT_REF, artifact_document({"a": b"a"}))
    second_claim = _next_claim(session, task, first_claim, 2)
    new = service.begin_upload(second_claim, ARTIFACT_REF, artifact_document({"b": b"b"}))
    assert _upload_ids(session) == {new.upload_id}
    with pytest.raises(ConflictError) as caught:
        service.put_blob(
            first_claim, ARTIFACT_REF, old.upload_id, hashlib.sha256(b"a").hexdigest(), [b"a"]
        )
    assert str(caught.value) == UPLOAD_GONE
    with pytest.raises(PermissionDenied, match="superseded"):
        service.commit_upload(first_claim, ARTIFACT_REF, old.upload_id)
    assert session.scalars(select(ArtifactVersion)).all() == []


def test_killed_attempts_never_lock_their_task_out_of_its_artifact(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """More killed attempts than the cap allows still leave the next one a slot."""
    service = artifact_service(session, tmp_path)
    task, actor = _attempt(session, principal)
    for claim in range(1, MAX_OPEN_UPLOADS + 4):
        actor = _next_claim(session, task, actor, claim)
        service.begin_upload(actor, ARTIFACT_REF, artifact_document({"a": b"a"}))
        assert len(_upload_ids(session)) == 1


def test_a_begin_under_a_later_claim_leaves_what_is_not_its_tasks_own_open_upload(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Another task run's, a person's and a committed upload of the task all stay."""
    service = artifact_service(session, tmp_path)
    task, first_claim = _attempt(session, principal)
    _, other_task = _attempt(session, principal)
    (person,) = _more_principals(session, 1)
    committed = service.begin_upload(first_claim, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    service.put_blob(first_claim, ARTIFACT_REF, committed.upload_id, _MODEL_SHA, [_MODEL])
    service.commit_upload(first_claim, ARTIFACT_REF, committed.upload_id)
    dead = service.begin_upload(first_claim, ARTIFACT_REF, artifact_document({"a": b"a"}))
    others = {
        service.begin_upload(other_task, ARTIFACT_REF, artifact_document({"o": b"o"})).upload_id,
        service.begin_upload(person, ARTIFACT_REF, artifact_document({"p": b"p"})).upload_id,
    }
    second_claim = _next_claim(session, task, first_claim, 2)
    new = service.begin_upload(second_claim, ARTIFACT_REF, artifact_document({"b": b"b"}))
    assert _upload_ids(session) == {committed.upload_id, new.upload_id} | others
    assert dead.upload_id not in _upload_ids(session)


def test_a_caller_that_is_not_a_task_attempt_clears_no_task_upload(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A person's begin never deletes an attempt's open upload."""
    service = artifact_service(session, tmp_path)
    _, actor = _attempt(session, principal)
    held = service.begin_upload(actor, ARTIFACT_REF, artifact_document({"a": b"a"}))
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"b": b"b"}))
    assert held.upload_id in _upload_ids(session)
    assert len(_upload_ids(session)) == 2


def test_only_a_lower_claim_of_the_same_task_run_is_cleared(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """An upload of a higher claim is never touched by a lower one's room-making."""
    service = artifact_service(session, tmp_path)
    task, first_claim = _attempt(session, principal)
    second_claim = _next_claim(session, task, first_claim, 2)
    newer = service.begin_upload(second_claim, ARTIFACT_REF, artifact_document({"b": b"b"}))
    row = session.get(ArtifactUpload, newer.upload_id)
    assert row is not None
    service._make_room(ARTIFACT_REF, row.artifact_id, first_claim.run_context, datetime.now(UTC))
    assert _upload_ids(session) == {newer.upload_id}


def test_an_upload_of_another_artifact_is_never_cleared_by_this_ones_room_making(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The lock covers one artifact, and so does what is deleted under it.

    A task attempt never has an upload in a second artifact (it may write only its own
    asset), so the row is made by hand.
    """
    service = artifact_service(session, tmp_path)
    task, first_claim = _attempt(session, principal)
    other = service.begin_upload(
        principal, TableRef.from_dotted("dev", "models.other"), artifact_document({"o": b"o"})
    )
    row = session.get(ArtifactUpload, other.upload_id)
    assert row is not None
    row.task_run_id, row.claim = task.id, 1
    session.commit()
    second_claim = _next_claim(session, task, first_claim, 2)
    service.begin_upload(second_claim, ARTIFACT_REF, artifact_document({"b": b"b"}))
    assert other.upload_id in _upload_ids(session)


def test_commit_of_a_damaged_stored_upload_reads_as_gone(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    session.execute(update(ArtifactUpload).values(manifest_json={"files": "x"}))
    session.commit()
    with pytest.raises(ConflictError) as caught:
        service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert str(caught.value) == UPLOAD_GONE


def test_the_seventeenth_open_upload_is_refused(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    writers = _more_principals(session, MAX_OPEN_UPLOADS + 1)
    for writer in writers[:MAX_OPEN_UPLOADS]:
        service.begin_upload(writer, ARTIFACT_REF, artifact_document({"a": b"a"}))
    with pytest.raises(ConflictError) as caught:
        service.begin_upload(writers[-1], ARTIFACT_REF, artifact_document({"a": b"a"}))
    assert str(caught.value) == (
        "models.churn has 16 uploads in progress, the most allowed at once. Commit one, "
        "withdraw one you began (`lakana artifact uploads list models.churn` shows yours, "
        "`lakana artifact uploads abort models.churn UPLOAD_ID` withdraws one), or wait for one "
        "to expire (they expire after 3600 seconds), then try again."
    )
    assert len(session.scalars(select(ArtifactUpload.id)).all()) == MAX_OPEN_UPLOADS
    assert MAX_OPEN_UPLOADS == 16


def test_a_commit_frees_a_slot(session: Session, tmp_path: Path, principal: PrincipalRef) -> None:
    service = artifact_service(session, tmp_path)
    writers = _more_principals(session, MAX_OPEN_UPLOADS)
    for writer in writers:
        service.begin_upload(writer, ARTIFACT_REF, artifact_document({"a": b"a"}))
    first = session.scalars(select(ArtifactUpload)).first()
    assert first is not None
    owner = next(w for w in writers if w.id == first.principal_id)
    service.put_blob(owner, ARTIFACT_REF, first.id, hashlib.sha256(b"a").hexdigest(), [b"a"])
    service.commit_upload(owner, ARTIFACT_REF, first.id)
    service.begin_upload(principal, ARTIFACT_REF, artifact_document({"a": b"a"}))


def test_begin_upload_by_a_task_with_a_stale_claim_is_refused_under_lock(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`record_pending` in `begin_upload` re-verifies the attempt after the lock."""
    service = artifact_service(session, tmp_path)
    task, actor = _attempt(session, principal)
    service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES))
    real = lock_artifact

    def reclaimed_just_before(s: Session, artifact_id: uuid.UUID) -> bool:
        s.execute(update(TaskRun).where(TaskRun.id == task.id).values(claim_count=2))
        return real(s, artifact_id)

    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", reclaimed_just_before)
    with pytest.raises(PermissionDenied) as caught:
        service.begin_upload(actor, ARTIFACT_REF, artifact_document({"b": b"b"}))
    assert str(caught.value) == SUPERSEDED_MESSAGE
    assert len(session.scalars(select(ArtifactUpload.id)).all()) == 1


def test_a_break_glass_upload_into_a_protected_environment_is_audited(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=True))
    session.commit()
    glass = dataclasses.replace(principal, break_glass_reason="INC-1 repair by hand")
    service.begin_upload(glass, ARTIFACT_REF, artifact_document(_FILES))
    assert _events(session).count("BREAK_GLASS_WRITE") == 1
    weak = ArtifactService(
        session,
        StorageResolver(session),
        ActionAllowList(Action.WRITE_TABLE, Action.CREATE_TABLE),
    )
    other = TableRef("dev", ARTIFACT_REF.namespace, "other")
    with pytest.raises(PermissionDenied):
        weak.begin_upload(glass, other, artifact_document(_FILES))
    assert _events(session).count("BREAK_GLASS_WRITE") == 1
    assert session.scalars(select(Table).where(Table.name == "other")).all() == []


def test_the_lock_is_taken_before_a_file_is_checked_and_made_visible(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order is `lock_artifact`, then `stored_hashes`, then `StreamWriter.commit`."""
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    steps: list[str] = []
    real_lock = lock_artifact
    real_stored = stored_hashes
    real_open = FsspecStorage.open_write

    def lock(s: Session, artifact_id: uuid.UUID) -> bool:
        steps.append("lock")
        return real_lock(s, artifact_id)

    def stored(s: Session, artifact_id: uuid.UUID, hashes: list[str]) -> dict[str, int]:
        steps.append("stored")
        return real_stored(s, artifact_id, hashes)

    def open_write(self: FsspecStorage, path: str) -> StreamWriter:
        writer = real_open(self, path)
        real_commit = writer.commit

        def commit() -> None:
            steps.append("commit")
            real_commit()

        writer.commit = commit  # ty: ignore[invalid-assignment]
        return writer

    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", lock)
    monkeypatch.setattr("lakana_core.artifacts.service.stored_hashes", stored)
    monkeypatch.setattr(FsspecStorage, "open_write", open_write)
    assert service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, [_MODEL])
    assert steps == ["lock", "stored", "commit"]


def test_a_failed_commit_removes_its_own_manifest_object_before_the_lock_is_gone(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the lock the object is ours to remove; a crash would leave a gap."""
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, [_MODEL])
    artifact_id = _artifact_id(session)
    real_commit, real_rollback = session.commit, session.rollback
    failing = [True]
    events: list[str] = []

    def commit() -> None:
        if failing[0] and _objects(session, f"artifacts/{artifact_id.hex}/manifests"):
            failing[0] = False
            raise OSError("connection lost")
        real_commit()

    def rollback() -> None:
        events.append("rollback")
        real_rollback()

    real_delete = FsspecStorage.delete

    def delete(self: FsspecStorage, path: str) -> None:
        events.append("delete manifest" if "/manifests/" in path else "delete")
        real_delete(self, path)

    monkeypatch.setattr(session, "commit", commit)
    monkeypatch.setattr(session, "rollback", rollback)
    monkeypatch.setattr(FsspecStorage, "delete", delete)
    with pytest.raises(OSError, match="connection lost"):
        service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert _objects(session, f"artifacts/{artifact_id.hex}/manifests") == []
    assert events.index("delete manifest") < events.index("rollback")
    assert session.scalars(select(ArtifactVersion)).all() == []
    done = service.commit_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert done.version.version == 1
    stored = (
        StorageResolver(session).for_environment("dev").storage.read(manifest_path(artifact_id, 1))
    )
    assert hashlib.sha256(stored).hexdigest() == done.version.manifest_sha256


def test_a_version_number_is_never_reused(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"m": b"1"})
    store_version(service, principal, ARTIFACT_REF, {"m": b"2"})
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.version == 2)
        .values(deleted_at=datetime.now(UTC))
    )
    session.commit()
    assert store_version(service, principal, ARTIFACT_REF, {"m": b"3"}).version.version == 3


def test_blob_requests_do_not_parse_the_whole_manifest(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I5: a file's size is looked up in the stored document, not by validating all of it."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"new"}))

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the manifest was parsed in full")

    monkeypatch.setattr("lakana_core.artifacts.service.parse_manifest", boom)
    digest = hashlib.sha256(b"new").hexdigest()
    assert service.put_blob(principal, ARTIFACT_REF, upload.upload_id, digest, [b"new"])
    assert b"".join(service.open_blob(principal, ARTIFACT_REF, 1, _MODEL_SHA).chunks) == _MODEL
    with pytest.raises(ValidationError, match="names no file with that SHA-256"):
        service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, [_MODEL])
    with pytest.raises(NotFoundError, match="has no file with that SHA-256"):
        service.open_blob(principal, ARTIFACT_REF, 1, "0" * 64)


@pytest.mark.parametrize(
    "size", ["x", -1, True, 10**20, None, 1.5], ids=["str", "neg", "bool", "huge", "none", "float"]
)
def test_a_damaged_size_in_a_stored_manifest_is_reported(
    session: Session, tmp_path: Path, principal: PrincipalRef, size: object
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"m": _MODEL}, is_directory=False)
    entry = {"path": "m", "sha256": _MODEL_SHA, "size": size}
    session.execute(update(ArtifactVersion).values(manifest_json={"files": [entry]}))
    session.commit()
    with pytest.raises(ValidationError, match="is damaged"):
        service.open_blob(principal, ARTIFACT_REF, 1, _MODEL_SHA)
    with pytest.raises(NotFoundError, match="has no file with that SHA-256"):
        service.open_blob(principal, ARTIFACT_REF, 1, "0" * 64)


def test_a_damaged_open_upload_reads_as_gone(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    session.execute(update(ArtifactUpload).values(manifest_json={"files": "x"}))
    session.commit()
    with pytest.raises(ConflictError) as caught:
        service.receive_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA)
    assert str(caught.value) == UPLOAD_GONE


def test_wrongly_typed_arguments_end_as_the_ordinary_refusals(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A `str` id, a `bool` or `float` version and a non-`str` hash raise no raw error."""
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, _FILES)
    bad: object = "not-a-uuid"
    for call in (
        lambda: service.receive_blob(principal, ARTIFACT_REF, bad, _MODEL_SHA),  # ty: ignore[invalid-argument-type]
        lambda: service.put_blob(principal, ARTIFACT_REF, bad, _MODEL_SHA, []),  # ty: ignore[invalid-argument-type]
        lambda: service.commit_upload(principal, ARTIFACT_REF, bad),  # ty: ignore[invalid-argument-type]
    ):
        with pytest.raises(ConflictError) as gone:
            call()
        assert str(gone.value) == UPLOAD_GONE
    text = "models.churn has no version with that number; versions count up from 1."
    for number in ("1", True, 1.0, None.__class__):
        for call in (
            lambda n=number: service.get_version(principal, ARTIFACT_REF, n),  # ty: ignore[invalid-argument-type]
            lambda n=number: service.open_blob(principal, ARTIFACT_REF, n, _MODEL_SHA),  # ty: ignore[invalid-argument-type]
        ):
            with pytest.raises(NotFoundError) as caught:
                call()
            assert str(caught.value) == text
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"n"}))
    with pytest.raises(ValidationError, match="64 hex digits"):
        service.receive_blob(principal, ARTIFACT_REF, upload.upload_id, 5)  # ty: ignore[invalid-argument-type]
    with pytest.raises(NotFoundError, match="has no file with that SHA-256"):
        service.open_blob(principal, ARTIFACT_REF, 1, 5)  # ty: ignore[invalid-argument-type]


def test_sources_are_bounded_and_unambiguous(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """One name with two versions is refused, 64 sources are the most, Cn is refused."""
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    with pytest.raises(ValidationError) as twice:
        service.commit_upload(
            principal,
            ARTIFACT_REF,
            upload.upload_id,
            sources=[ArtifactSource("models.base", 1), ArtifactSource("models.base", 2)],
        )
    assert str(twice.value) == (
        "models.base is named as a source twice with different versions; name each source once."
    )
    names = [ArtifactSource(f"silver.t{i}") for i in range(65)]
    with pytest.raises(ValidationError, match="at most 64 sources"):
        service.commit_upload(principal, ARTIFACT_REF, upload.upload_id, sources=names)
    for name in ("silver.rows\u0378", "silver.rows\u00a0", "silver.rows\ue000"):
        with pytest.raises(ValidationError, match="whole number from 1 to 2147483647"):
            service.commit_upload(
                principal, ARTIFACT_REF, upload.upload_id, sources=[ArtifactSource(name)]
            )


def _stored_upload(
    service: ArtifactService, principal: PrincipalRef
) -> tuple[uuid.UUID, uuid.UUID]:
    """Begin an upload of one file, store the file and return the upload's and artifact's ids.

    Parameters
    ----------
    service
        The service under test.
    principal
        The acting principal.

    Returns
    -------
    tuple
        The upload id and the artifact id.
    """
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, [_MODEL])
    return upload.upload_id, _artifact_id(service._session)


def test_a_storage_outage_while_listing_manifests_is_a_clean_commit_error(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-1: the listing under the lock raised a raw OSError out of the commit."""
    service = artifact_service(session, tmp_path)
    upload_id, _ = _stored_upload(service, principal)
    real = FsspecStorage.list_objects

    def down(self: FsspecStorage, prefix: str) -> Iterator[StoredObject]:
        raise OSError("/secret/bucket/path: connection refused")

    monkeypatch.setattr(FsspecStorage, "list_objects", down)
    with pytest.raises(ConflictError) as refused:
        service.commit_upload(principal, ARTIFACT_REF, upload_id)
    assert str(refused.value) == COMMIT_NOT_WRITTEN
    assert "secret" not in str(refused.value)
    assert session.scalars(select(ArtifactVersion)).all() == []
    assert session.scalars(select(ArtifactUpload.id)).all() == [upload_id]
    monkeypatch.setattr(FsspecStorage, "list_objects", real)
    assert service.commit_upload(principal, ARTIFACT_REF, upload_id).version.version == 1


def test_when_deleting_the_manifest_object_of_a_failed_commit_fails_the_first_error_is_raised(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-4: the clean-up of the failure path never replaces the error that caused it."""
    service = artifact_service(session, tmp_path)
    upload_id, artifact_id = _stored_upload(service, principal)
    real_commit = session.commit
    real_delete = FsspecStorage.delete

    def commit() -> None:
        if _objects(session, f"artifacts/{artifact_id.hex}/manifests"):
            raise OSError("connection lost")
        real_commit()

    def delete(self: FsspecStorage, path: str) -> None:
        if "/manifests/" in path:
            raise OSError("/secret/path: storage went away")
        real_delete(self, path)

    monkeypatch.setattr(session, "commit", commit)
    monkeypatch.setattr(FsspecStorage, "delete", delete)
    with pytest.raises(OSError, match="connection lost"):
        service.commit_upload(principal, ARTIFACT_REF, upload_id)
    assert session.scalars(select(ArtifactVersion)).all() == []


def test_a_manifest_object_at_the_highest_number_ends_the_numbering(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """R-5: the next number would overflow the 32-bit column on Postgres."""
    service = artifact_service(session, tmp_path)
    upload_id, artifact_id = _stored_upload(service, principal)
    StorageResolver(session).for_environment("dev").storage.write(
        manifest_path(artifact_id, 2**31 - 1), b"{}"
    )
    with pytest.raises(ConflictError) as refused:
        service.commit_upload(principal, ARTIFACT_REF, upload_id)
    assert str(refused.value) == (
        "models.churn has reached the highest version number (2147483647); store further "
        "versions under a new artifact name."
    )
    assert session.scalars(select(ArtifactVersion)).all() == []
    assert session.scalars(select(ArtifactUpload.id)).all() == [upload_id]


# --- a commit is idempotent per upload ----------------------------------------


def _committed_upload(
    service: ArtifactService, principal: PrincipalRef
) -> tuple[uuid.UUID, uuid.UUID]:
    """Store version 1 and return the upload that produced it, and the artifact's id.

    Parameters
    ----------
    service
        The service under test.
    principal
        The uploader.

    Returns
    -------
    tuple
        The upload id and the artifact id.
    """
    upload_id, artifact_id = _stored_upload(service, principal)
    service.commit_upload(principal, ARTIFACT_REF, upload_id)
    return upload_id, artifact_id


def test_a_repeated_commit_returns_the_same_version_and_writes_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload_id, artifact_id = _stored_upload(service, principal)
    first = service.commit_upload(principal, ARTIFACT_REF, upload_id)
    assert not session.in_transaction()
    before = (
        session.scalars(select(ArtifactVersion.version)).all(),
        len(_events(session)),
        _objects(session, f"artifacts/{artifact_id.hex}/manifests"),
    )
    again = service.commit_upload(principal, ARTIFACT_REF, upload_id)
    assert again.version == first.version
    assert again.deleted_versions == ()
    after = (
        session.scalars(select(ArtifactVersion.version)).all(),
        len(_events(session)),
        _objects(session, f"artifacts/{artifact_id.hex}/manifests"),
    )
    assert after == before == ([1], before[1], [manifest_path(artifact_id, 1)])


def test_a_repeated_commit_is_recognised_before_its_sources_are_resolved(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A-I1 twin: a source that is gone when the answer was lost does not undo the commit."""
    service = artifact_service(session, tmp_path)
    ensure_table(session, "dev", "silver", "rows")
    upload_id, _ = _stored_upload(service, principal)
    sources = [ArtifactSource("silver.rows")]
    first = service.commit_upload(principal, ARTIFACT_REF, upload_id, sources=sources)
    session.execute(delete(LineageEdge))
    session.execute(delete(Table).where(Table.name == "rows"))
    session.commit()
    again = service.commit_upload(principal, ARTIFACT_REF, upload_id, sources=sources)
    assert again.version == first.version
    other = _more_principals(session, 1)[0]
    with pytest.raises(ConflictError) as foreign:
        service.commit_upload(other, ARTIFACT_REF, upload_id, sources=sources)
    assert str(foreign.value) == UPLOAD_GONE


def test_a_repeated_commit_by_another_principal_or_after_expiry_is_gone(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    clock = [datetime.now(UTC)]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    upload_id, _ = _committed_upload(service, principal)
    (other,) = _more_principals(session, 1)
    with pytest.raises(ConflictError) as foreign:
        service.commit_upload(other, ARTIFACT_REF, upload_id)
    assert str(foreign.value) == UPLOAD_GONE
    clock[0] += timedelta(seconds=ArtifactsConfig().upload_ttl_seconds)
    with pytest.raises(ConflictError) as late:
        service.commit_upload(principal, ARTIFACT_REF, upload_id)
    assert str(late.value) == UPLOAD_GONE


def test_a_repeated_commit_of_a_version_deleted_since_says_so(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload_id, _ = _committed_upload(service, principal)
    service.delete_version(principal, ARTIFACT_REF, 1)
    with pytest.raises(ConflictError) as gone:
        service.commit_upload(principal, ARTIFACT_REF, upload_id)
    assert str(gone.value) == (
        "This upload was committed as version 1 of models.churn, and that version has since "
        "been deleted."
    )
    assert str(gone.value) != UPLOAD_GONE


def test_a_commit_just_before_expiry_can_still_be_repeated(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """N-1: the repeat window is a full lifetime from the commit, not from the begin."""
    clock = [datetime(2026, 10, 6, 12, 0, tzinfo=UTC)]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    upload_id, _ = _stored_upload(service, principal)
    ttl = ArtifactsConfig().upload_ttl_seconds
    original_expiry = clock[0] + timedelta(seconds=ttl)
    clock[0] = original_expiry - timedelta(seconds=3)
    first = service.commit_upload(principal, ARTIFACT_REF, upload_id)
    clock[0] = original_expiry + timedelta(seconds=7)
    again = service.commit_upload(principal, ARTIFACT_REF, upload_id)
    assert again.version == first.version
    assert session.scalars(select(ArtifactVersion.version)).all() == [1]
    row = session.get(ArtifactUpload, upload_id)
    assert row is not None
    assert row.expires_at.replace(tzinfo=UTC) == (
        original_expiry - timedelta(seconds=3) + timedelta(seconds=ttl)
    )


def test_a_commit_never_shortens_an_upload_that_has_longer_to_live(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """N-1: the later of the two, so a commit right after the begin keeps the begin's expiry."""
    clock = [datetime(2026, 10, 6, 12, 0, tzinfo=UTC)]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    upload_id, _ = _stored_upload(service, principal)
    session.execute(
        update(ArtifactUpload)
        .where(ArtifactUpload.id == upload_id)
        .values(expires_at=clock[0] + timedelta(days=2))
    )
    session.commit()
    service.commit_upload(principal, ARTIFACT_REF, upload_id)
    row = session.get(ArtifactUpload, upload_id)
    assert row is not None
    assert row.expires_at.replace(tzinfo=UTC) == clock[0] + timedelta(days=2)


def test_a_committed_upload_keeps_no_copy_of_its_manifest(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The copy can be megabytes and nothing reads it once the upload is committed."""
    service = artifact_service(session, tmp_path)
    upload_id, _ = _stored_upload(service, principal)
    open_row = session.get(ArtifactUpload, upload_id)
    assert open_row is not None
    assert open_row.manifest_json != {}
    first = service.commit_upload(principal, ARTIFACT_REF, upload_id)
    session.expire_all()
    row = session.get(ArtifactUpload, upload_id)
    assert row is not None
    assert row.manifest_json == {}
    assert service.commit_upload(principal, ARTIFACT_REF, upload_id).version == first.version


def test_a_new_upload_of_the_same_attempt_keeps_its_committed_one(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """N-2: a retry replaces its own open upload, never one that was committed."""
    service = artifact_service(session, tmp_path)
    _, actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    service.put_blob(actor, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, [_MODEL])
    first = service.commit_upload(actor, ARTIFACT_REF, upload.upload_id)
    other = service.begin_upload(actor, ARTIFACT_REF, artifact_document({"n": _NOTES}))
    assert other.upload_id != upload.upload_id
    assert {row.id for row in session.scalars(select(ArtifactUpload))} == {
        upload.upload_id,
        other.upload_id,
    }
    again = service.commit_upload(actor, ARTIFACT_REF, upload.upload_id)
    assert again.version == first.version


def test_the_precheck_records_no_break_glass_event(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """N-5: nothing is written yet, so nothing is audited."""
    service = artifact_service(session, tmp_path)
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=True))
    session.commit()
    glass = dataclasses.replace(principal, break_glass_reason="INC-1 repair by hand")
    service.check_can_upload(glass, ARTIFACT_REF)
    assert "BREAK_GLASS_WRITE" not in _events(session)
    # Nothing is left pending either: whatever lands a write later in the request writes the
    # row of its own operation, never one the precheck prepared.
    service._guard.record_pending()
    session.commit()
    assert "BREAK_GLASS_WRITE" not in _events(session)
    service.begin_upload(glass, ARTIFACT_REF, artifact_document(_FILES))
    assert _events(session).count("BREAK_GLASS_WRITE") == 1


def test_a_repeated_commit_after_the_claim_was_superseded_is_refused(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-5: the repeat runs `record_pending`, which re-verifies the attempt under the lock."""
    service = artifact_service(session, tmp_path)
    task, actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    service.put_blob(actor, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, [_MODEL])
    service.commit_upload(actor, ARTIFACT_REF, upload.upload_id)
    real = lock_artifact

    def reclaimed_just_before(s: Session, artifact_id: uuid.UUID) -> bool:
        s.execute(update(TaskRun).where(TaskRun.id == task.id).values(claim_count=2))
        return real(s, artifact_id)

    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", reclaimed_just_before)
    with pytest.raises(PermissionDenied) as caught:
        service.commit_upload(actor, ARTIFACT_REF, upload.upload_id)
    assert str(caught.value) == SUPERSEDED_MESSAGE


def test_two_commits_of_one_upload_at_the_same_moment_make_one_version(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-5: a whole commit lands while the first waits for the lock; the first then repeats it."""
    service = artifact_service(session, tmp_path)
    _, actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    service.put_blob(actor, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, [_MODEL])
    sources = [ArtifactSource(name="silver.rows")]
    real = lock_artifact
    seen: list[object] = []

    def other_commit_first(s: Session, artifact_id: uuid.UUID) -> bool:
        if not seen:
            seen.append(None)
            service.commit_upload(actor, ARTIFACT_REF, upload.upload_id, sources=sources)
        return real(s, artifact_id)

    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", other_commit_first)
    result = service.commit_upload(actor, ARTIFACT_REF, upload.upload_id, sources=sources)
    assert len(seen) == 1
    assert result.version.version == 1
    assert session.scalars(select(ArtifactVersion.version)).all() == [1]
    assert _events(session).count("ARTIFACT_VERSION_ADDED") == 1
    assert len(session.scalars(select(LineageEdge.id)).all()) == 1


def test_a_committed_upload_takes_no_more_files(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload_id, _ = _committed_upload(service, principal)
    with pytest.raises(ConflictError) as refused:
        service.receive_blob(principal, ARTIFACT_REF, upload_id, _MODEL_SHA)
    assert str(refused.value) == UPLOAD_GONE


def test_a_file_that_finishes_after_its_upload_was_committed_is_refused(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA, [_MODEL])
    session.execute(
        update(ArtifactUpload)
        .where(ArtifactUpload.id == upload.upload_id)
        .values(committed_version=1)
    )
    session.commit()
    artifact_id = _artifact_id(session)
    writer = (
        StorageResolver(session)
        .for_environment("dev")
        .storage.open_write(blob_path(artifact_id, "f" * 64))
    )
    late = BlobUpload(
        session,
        writer,
        artifact_id,
        upload.upload_id,
        hashlib.sha256(b"x").hexdigest(),
        1,
        lambda: datetime.now(UTC),
        datetime.now(UTC) + timedelta(hours=1),
    )
    late.write(b"x")
    with pytest.raises(ConflictError) as refused:
        late.finish()
    assert str(refused.value) == UPLOAD_GONE


def test_committed_uploads_do_not_count_toward_the_cap(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    for n in range(MAX_OPEN_UPLOADS + 2):
        store_version(service, principal, ARTIFACT_REF, {"m": str(n).encode()})
    assert len(session.scalars(select(ArtifactUpload.id)).all()) == MAX_OPEN_UPLOADS + 2
    # None of them is open: sixteen more can begin.
    for _ in range(MAX_OPEN_UPLOADS):
        service.begin_upload(principal, ARTIFACT_REF, artifact_document({"a": b"a"}))
    with pytest.raises(ConflictError, match="uploads in progress"):
        service.begin_upload(principal, ARTIFACT_REF, artifact_document({"a": b"a"}))


def test_a_committed_uploads_manifest_protects_nothing_by_itself(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload_id, artifact_id = _committed_upload(service, principal)
    service.delete_version(principal, ARTIFACT_REF, 1)
    assert session.scalars(select(ArtifactUpload.committed_version)).all() == [1]
    assert _objects(session, blobs_prefix(artifact_id)) == []
    assert upload_id is not None


# --- no transaction while a file arrives, a bounded upload, storage that fails -------------


def test_no_transaction_is_open_while_a_file_arrives(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    blob = service.receive_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA)
    assert not session.in_transaction()
    blob.write(_MODEL)
    assert not session.in_transaction()
    assert blob.finish() is True


def test_a_download_leaves_no_transaction_open_while_the_bytes_stream(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"m": _MODEL})
    download = service.open_blob(principal, ARTIFACT_REF, 1, _MODEL_SHA)
    assert not session.in_transaction()
    assert b"".join(download.chunks) == _MODEL


def test_a_file_knows_how_long_its_upload_can_still_take_it(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    clock = [datetime.now(UTC)]
    service = artifact_service(session, tmp_path, clock=lambda: clock[0])
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    blob = service.receive_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA)
    ttl = ArtifactsConfig().upload_ttl_seconds
    assert blob.seconds_left() == ttl
    clock[0] += timedelta(seconds=ttl - 1)
    assert blob.seconds_left() == 1
    clock[0] += timedelta(seconds=2)
    assert blob.seconds_left() == -1
    blob.abort()


class _FailingWriter:
    """A stream whose storage is full."""

    def write(self, data: bytes) -> None:
        raise OSError(28, "/secret/bucket/path: No space left on device")

    def commit(self) -> None:
        raise OSError(28, "/secret/bucket/path: No space left on device")

    def abort(self) -> None:
        return None


@pytest.mark.parametrize("where", ["open", "write", "finish"])
def test_storage_that_cannot_be_written_is_a_domain_error_without_storage_text(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": _MODEL}))
    real = FsspecStorage.open_write

    def failing(self: FsspecStorage, path: str) -> StreamWriter:
        if where == "open":
            raise OSError("/secret/bucket/path: access denied")
        if where == "write":
            return _FailingWriter()
        writer = real(self, path)
        writer.commit = _FailingWriter().commit  # ty: ignore[invalid-assignment]
        return writer

    monkeypatch.setattr(FsspecStorage, "open_write", failing)
    with pytest.raises(StorageUnavailableError) as caught:
        blob = service.receive_blob(principal, ARTIFACT_REF, upload.upload_id, _MODEL_SHA)
        blob.write(_MODEL)
        blob.finish()
    assert str(caught.value) == STORAGE_NOT_WRITTEN
    assert "secret" not in str(caught.value)
    assert "Try again" in str(caught.value)
    assert "storage_unreachable" in {getattr(r, "event", None) for r in caplog.records}
    assert not session.in_transaction()


def _crm_connection(session: Session, principal: PrincipalRef) -> uuid.UUID:
    """Create the connection `crm-db` in `dev`.

    Parameters
    ----------
    session
        Database session.
    principal
        Its creator.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    environment = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    row = Connection(
        id=uuid.uuid4(),
        environment_id=environment.id,
        name="crm-db",
        kind=ConnectionKind.POSTGRESQL,
        address={"host": "db.example.com", "port": 5432, "database": "crm"},
        settings={"user": "reader", "tls": "verify-full"},
        created_by=principal.id,
    )
    session.add(row)
    session.commit()
    return row.id


def _connection_edges(session: Session) -> list[tuple[uuid.UUID, str, int | None, int | None]]:
    """Read the connection edges.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of tuple
        Each edge's source id, source name, source snapshot and target version.
    """
    return [
        (e.source_table_id, e.source_name, e.source_snapshot_id, e.target_snapshot_id)
        for e in session.scalars(
            select(LineageEdge)
            .where(LineageEdge.source_kind == "connection")
            .order_by(LineageEdge.source_name)
        )
    ]


def test_a_task_version_records_its_assets_declared_connections(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """An artifact written by a bound task names its connections, as a table write does."""
    service = artifact_service(session, tmp_path)
    crm = _crm_connection(session, principal)
    _, actor = _attempt(session, principal)
    assert actor.task_scope is not None
    assert actor.task_scope.bound is not None
    bound = dataclasses.replace(actor.task_scope.bound, connections=("crm-db", "gone"))
    actor = dataclasses.replace(
        actor, task_scope=dataclasses.replace(actor.task_scope, bound=bound)
    )

    result = store_version(service, actor, ARTIFACT_REF, _FILES)

    assert _connection_edges(session) == [(crm, "crm-db", None, result.version.version)]
    edge = session.scalars(select(LineageEdge)).one()
    assert edge.target_name == "models.churn"


def test_a_pat_version_records_no_connection_edge(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    _crm_connection(session, principal)

    store_version(service, principal, ARTIFACT_REF, _FILES)

    assert _connection_edges(session) == []
