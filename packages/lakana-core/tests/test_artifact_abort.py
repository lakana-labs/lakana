"""Withdrawing an upload that was begun and not finished."""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    ActionAllowList,
    artifact_document,
    artifact_service,
    ensure_table,
    python_task_run,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lakana_core.artifacts.service import MAX_OPEN_UPLOADS, UPLOAD_GONE, ArtifactService
from lakana_core.artifacts.store import blob_path, lock_artifact
from lakana_core.auth import BoundTask, PrincipalRef, RunContext, TaskScope
from lakana_core.config import ArtifactsConfig
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.models import (
    ArtifactBlob,
    ArtifactUpload,
    AuditEvent,
    AuditEventType,
    Environment,
    Principal,
    PrincipalType,
    Table,
)
from lakana_core.runs.lockdown import SUPERSEDED_MESSAGE
from lakana_core.storage.resolver import StorageResolver

_DATA = b"abc"
_SHA = hashlib.sha256(_DATA).hexdigest()
_FILES = {"m": _DATA}


def _uploads(session: Session) -> list[uuid.UUID]:
    """List the ids of every upload row.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of uuid.UUID
        The ids.
    """
    session.expire_all()
    return list(session.scalars(select(ArtifactUpload.id)).all())


def _other(session: Session) -> PrincipalRef:
    """Persist another person.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The other caller.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="other", is_active=True)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _attempt(session: Session, principal: PrincipalRef, claim: int = 1) -> PrincipalRef:
    """Make the principal of a running task attempt that produces `models.churn`.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    claim
        The claim the token carries.

    Returns
    -------
    PrincipalRef
        The principal as the attempt's bound token authenticates it.
    """
    ensure_table(session, "dev", "silver", "rows")
    task = python_task_run(
        session,
        principal.id,
        asset="models.churn",
        inputs={"rows": "silver.rows"},
        claim=claim,
        extra={"output": "artifact"},
    )
    bound = BoundTask(
        task.id, claim, "models.churn", ("silver.rows",), ("silver.rows",), "artifact"
    )
    return dataclasses.replace(
        principal,
        task_scope=TaskScope("dev", [], uuid.uuid4(), bound),
        run_context=RunContext(task_run_id=task.id, claim=claim),
    )


def test_an_abort_removes_the_callers_open_upload_and_frees_its_slot(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    mine = [
        service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES)).upload_id
        for _ in range(MAX_OPEN_UPLOADS)
    ]
    with pytest.raises(ConflictError, match="uploads in progress"):
        service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    assert service.abort_upload(principal, ARTIFACT_REF, mine[0]) is None
    assert mine[0] not in _uploads(session)
    assert len(_uploads(session)) == MAX_OPEN_UPLOADS - 1
    service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))


def test_an_abort_deletes_no_bytes_and_the_files_stay_for_the_sweep(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
    assert service.put_blob(principal, ARTIFACT_REF, upload.upload_id, _SHA, [_DATA])
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    storage = StorageResolver(session).for_environment("dev").storage
    service.abort_upload(principal, ARTIFACT_REF, upload.upload_id)
    assert storage.read(blob_path(artifact_id, _SHA)) == _DATA
    assert session.scalars(select(func.count()).select_from(ArtifactBlob)).one() == 1


@pytest.mark.parametrize("how", ["foreign", "unknown", "committed", "expired", "another artifact"])
def test_anything_that_is_not_the_callers_open_upload_is_gone_and_untouched(
    session: Session, tmp_path: Path, principal: PrincipalRef, how: str
) -> None:
    now = [datetime(2026, 10, 4, 12, 0, tzinfo=UTC)]
    service = artifact_service(
        session, tmp_path, config=ArtifactsConfig(upload_ttl_seconds=60), clock=lambda: now[0]
    )
    other = _other(session)
    if how == "foreign":
        target = service.begin_upload(other, ARTIFACT_REF, artifact_document(_FILES)).upload_id
    elif how == "unknown":
        service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
        target = uuid.uuid4()
    elif how == "committed":
        target = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES)).upload_id
        service.put_blob(principal, ARTIFACT_REF, target, _SHA, [_DATA])
        service.commit_upload(principal, ARTIFACT_REF, target)
    elif how == "expired":
        target = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES)).upload_id
        now[0] += timedelta(seconds=60)
    else:
        service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES))
        elsewhere = dataclasses.replace(ARTIFACT_REF, name="elsewhere")
        target = service.begin_upload(principal, elsewhere, artifact_document(_FILES)).upload_id
    before = _uploads(session)
    with pytest.raises(ConflictError) as caught:
        service.abort_upload(principal, ARTIFACT_REF, target)
    assert str(caught.value) == UPLOAD_GONE
    assert _uploads(session) == before
    if how == "committed":
        again = service.commit_upload(principal, ARTIFACT_REF, target)
        assert again.version.version == 1


def test_an_upload_of_a_task_attempt_is_withdrawn_only_by_that_attempt(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES)).upload_id
    assert actor.run_context is not None
    next_claim = dataclasses.replace(
        actor, run_context=RunContext(actor.run_context.task_run_id, claim=2)
    )
    outside = dataclasses.replace(actor, task_scope=None, run_context=None)
    for caller in (next_claim, outside):
        with pytest.raises(ConflictError) as caught:
            service.abort_upload(caller, ARTIFACT_REF, upload)
        assert str(caught.value) == UPLOAD_GONE
    assert _uploads(session) == [upload]
    service.abort_upload(actor, ARTIFACT_REF, upload)
    assert _uploads(session) == []


def test_another_task_run_with_the_same_principal_and_claim_cannot_use_the_upload(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The ownership check compares the task run as well as the claim."""
    service = artifact_service(session, tmp_path)
    actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES)).upload_id
    other_run = _attempt(session, principal)
    assert actor.run_context is not None and other_run.run_context is not None
    assert other_run.run_context.task_run_id != actor.run_context.task_run_id
    assert other_run.run_context.claim == actor.run_context.claim
    uses = {
        "abort": lambda: service.abort_upload(other_run, ARTIFACT_REF, upload),
        "commit": lambda: service.commit_upload(other_run, ARTIFACT_REF, upload),
        "receive": lambda: service.receive_blob(other_run, ARTIFACT_REF, upload, _SHA),
    }
    for name, use in uses.items():
        with pytest.raises(ConflictError) as caught:
            use()
        assert str(caught.value) == UPLOAD_GONE, name
    assert _uploads(session) == [upload]


def test_a_person_cannot_withdraw_an_upload_of_a_task_with_the_same_principal(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES)).upload_id
    person = dataclasses.replace(principal, task_scope=None, run_context=None)
    with pytest.raises(ConflictError):
        service.abort_upload(person, ARTIFACT_REF, upload)
    assert _uploads(session) == [upload]


def test_a_bound_scope_without_a_run_context_is_refused_after_authorization(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    actor = _attempt(session, principal)
    upload = service.begin_upload(actor, ARTIFACT_REF, artifact_document(_FILES)).upload_id
    headless = dataclasses.replace(actor, run_context=None)
    with pytest.raises(PermissionDenied) as caught:
        service.abort_upload(headless, ARTIFACT_REF, upload)
    assert str(caught.value) == SUPERSEDED_MESSAGE
    nobody = ArtifactService(session, StorageResolver(session), ActionAllowList())
    with pytest.raises(PermissionDenied, match="may not WRITE_TABLE") as plain:
        nobody.abort_upload(headless, ARTIFACT_REF, upload)
    assert SUPERSEDED_MESSAGE not in str(plain.value)
    assert _uploads(session) == [upload]


def test_the_name_is_checked_after_authorization(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    ghost = dataclasses.replace(ARTIFACT_REF, name="ghost")
    with pytest.raises(NotFoundError):
        service.abort_upload(principal, ghost, uuid.uuid4())
    ensure_table(session, "dev", "models", "plain")
    with pytest.raises(ValidationError, match="not an artifact"):
        service.abort_upload(
            principal, dataclasses.replace(ARTIFACT_REF, name="plain"), uuid.uuid4()
        )


def test_the_upload_is_read_and_deleted_under_the_artifacts_lock(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES)).upload_id
    steps: list[str] = []
    real_lock = lock_artifact
    real_open = ArtifactService._open_upload

    def lock(s: Session, artifact_id: uuid.UUID) -> bool:
        steps.append("lock")
        return real_lock(s, artifact_id)

    def opened(self: ArtifactService, *args: object, **kwargs: object) -> ArtifactUpload:
        steps.append("read")
        return real_open(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", lock)
    monkeypatch.setattr(ArtifactService, "_open_upload", opened)
    service.abort_upload(principal, ARTIFACT_REF, upload)
    assert steps == ["lock", "read"]


def test_an_artifact_dropped_before_the_lock_is_reported_as_gone(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES)).upload_id
    monkeypatch.setattr("lakana_core.artifacts.service.lock_artifact", lambda s, i: False)
    with pytest.raises(NotFoundError):
        service.abort_upload(principal, ARTIFACT_REF, upload)


def test_a_protected_environment_does_not_stand_in_the_way_of_withdrawing_ones_own_upload(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """No guard: the abort writes no version, no bytes and no guard event, only its own record."""
    service = artifact_service(session, tmp_path)
    glass = dataclasses.replace(principal, break_glass_reason="INC-1 repair by hand")
    upload = service.begin_upload(glass, ARTIFACT_REF, artifact_document(_FILES)).upload_id
    session.execute(update(Environment).where(Environment.name == "dev").values(is_protected=True))
    session.commit()
    before = set(session.scalars(select(AuditEvent.id)).all())
    service.abort_upload(principal, ARTIFACT_REF, upload)
    assert _uploads(session) == []
    written = session.scalars(
        select(AuditEvent.event_type).where(AuditEvent.id.not_in(before))
    ).all()
    assert written == [AuditEventType.ARTIFACT_UPLOAD_ABORTED]


def test_a_file_that_arrives_after_its_upload_was_withdrawn_is_refused(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    service = artifact_service(session, tmp_path)
    upload = service.begin_upload(principal, ARTIFACT_REF, artifact_document(_FILES)).upload_id
    blob = service.receive_blob(principal, ARTIFACT_REF, upload, _SHA)
    blob.write(_DATA)
    service.abort_upload(principal, ARTIFACT_REF, upload)
    with pytest.raises(ConflictError) as caught:
        blob.finish()
    assert str(caught.value) == UPLOAD_GONE
    assert session.scalars(select(func.count()).select_from(ArtifactBlob)).one() == 0
