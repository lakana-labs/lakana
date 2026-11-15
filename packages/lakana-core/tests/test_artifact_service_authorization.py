"""Every `ArtifactService` method authorizes before it looks anything up.

A caller who lacks the privilege a method needs gets the permission error a
table of that name would give, and learns nothing: not that the artifact
exists, not its kind, not whether a version or a file with some SHA-256
exists, not whether an upload id is valid. Each case below runs one method
with one authorization action withheld at a time, against an artifact, a
missing name and a table, with arguments that would each fail a later check.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import inspect
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    ActionAllowList,
    artifact_document,
    artifact_service,
    ensure_table,
    python_task_run,
    store_version,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lakana_core.artifacts.service import (
    DELETE_FROM_RUN_MESSAGE,
    MAX_OPEN_UPLOADS,
    ArtifactService,
    ArtifactSource,
)
from lakana_core.auth import (
    Action,
    BoundTask,
    PrincipalRef,
    RunContext,
    Securable,
    SecurableKind,
    TaskScope,
)
from lakana_core.errors import ConflictError, LakanaError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ArtifactBlob,
    ArtifactUpload,
    ArtifactVersion,
    AuditEvent,
    Environment,
    Grant,
    Principal,
    PrincipalType,
    Table,
)
from lakana_core.storage.resolver import StorageResolver

_FILES = {"model.json": b"{}"}
_SHA = hashlib.sha256(b"{}").hexdigest()
_NAMESPACE = NamespacePath.from_parts(["models"])
_GHOST = TableRef("dev", _NAMESPACE, "ghost")
_PLAIN = TableRef("dev", _NAMESPACE, "plain")
_FREE = TableRef("dev", _NAMESPACE, "free")
_HUGE = 10**5000
_ENVIRONMENT = Securable.environment_scope("dev")


def _table(name: str) -> Securable:
    """Name the securable of a table in `models`.

    Parameters
    ----------
    name
        The table's name.

    Returns
    -------
    Securable
        Its table-scope securable.
    """
    return Securable.table_scope("dev", _NAMESPACE, name)


class _Halt(LakanaError):
    """Raised by `_Peek` to stop a call after its first check."""


class _Peek(ActionAllowList):
    """Records the first authorization and stops the call there, before it can change anything."""

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Record the check and stop.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        _Halt
            Always.
        """
        self.seen.append((action, securable))
        raise _Halt("recorded")


class _Recording(ActionAllowList):
    """An allow list that also records the `permits` questions, which listing asks."""

    def __init__(self, *allowed: Action) -> None:
        super().__init__(*allowed)
        self.permitted: list[tuple[Action, Securable]] = []

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Record the question and answer it.

        Parameters
        ----------
        principal
            The actor the question is about.
        action
            The action in question.
        securable
            The object in question.

        Returns
        -------
        bool
            Whether `action` is allowed.
        """
        self.permitted.append((action, securable))
        return super().permits(principal, action, securable)


@dataclass
class _Ids:
    """What `_setup` leaves in the database.

    Parameters
    ----------
    upload
        An open upload of `models.churn` by the caller.
    foreign
        An open upload of `models.churn` by another principal.
    other
        That other principal.
    """

    upload: uuid.UUID
    foreign: uuid.UUID
    other: PrincipalRef


@dataclass
class _Case:
    """What one call needs.

    Parameters
    ----------
    service
        The service bound to the case's authorization.
    principal
        The caller.
    ref
        The name the call is made on.
    ids
        The uploads that exist.
    """

    service: ArtifactService
    principal: PrincipalRef
    ref: TableRef
    ids: _Ids


def _wrong(value: object) -> Any:  # ruff: ignore[any-type] -- a deliberately mistyped value
    """Pass a value of the wrong type where the service declares another.

    Parameters
    ----------
    value
        The value.

    Returns
    -------
    Any
        The same value, untyped.
    """
    return value


_CALLS: dict[str, tuple[str, Callable[[_Case], object]]] = {
    "get_artifact": ("get_artifact", lambda c: c.service.get_artifact(c.principal, c.ref)),
    "list_versions": ("list_versions", lambda c: c.service.list_versions(c.principal, c.ref)),
    "get_version_current": ("get_version", lambda c: c.service.get_version(c.principal, c.ref)),
    "get_version_one": ("get_version", lambda c: c.service.get_version(c.principal, c.ref, 1)),
    "get_version_missing": ("get_version", lambda c: c.service.get_version(c.principal, c.ref, 5)),
    "get_version_deleted": ("get_version", lambda c: c.service.get_version(c.principal, c.ref, 2)),
    "get_version_huge": (
        "get_version",
        lambda c: c.service.get_version(c.principal, c.ref, _HUGE),
    ),
    "get_version_str": (
        "get_version",
        lambda c: c.service.get_version(c.principal, c.ref, _wrong("1")),
    ),
    "get_version_bool": (
        "get_version",
        lambda c: c.service.get_version(c.principal, c.ref, _wrong(True)),
    ),
    "open_blob": ("open_blob", lambda c: c.service.open_blob(c.principal, c.ref, 1, _SHA)),
    "open_blob_unknown_file": (
        "open_blob",
        lambda c: c.service.open_blob(c.principal, c.ref, 1, "0" * 64),
    ),
    "open_blob_bad_hash": (
        "open_blob",
        lambda c: c.service.open_blob(c.principal, c.ref, 1, "../x"),
    ),
    "open_blob_non_str_hash": (
        "open_blob",
        lambda c: c.service.open_blob(c.principal, c.ref, 1, _wrong(5)),
    ),
    "open_blob_huge_version": (
        "open_blob",
        lambda c: c.service.open_blob(c.principal, c.ref, _HUGE, _SHA),
    ),
    "open_blob_missing_version": (
        "open_blob",
        lambda c: c.service.open_blob(c.principal, c.ref, 5, _SHA),
    ),
    "open_blob_deleted_version": (
        "open_blob",
        lambda c: c.service.open_blob(c.principal, c.ref, 2, _SHA),
    ),
    "open_blob_str_version": (
        "open_blob",
        lambda c: c.service.open_blob(c.principal, c.ref, _wrong("1"), _SHA),
    ),
    "open_blob_float_version": (
        "open_blob",
        lambda c: c.service.open_blob(c.principal, c.ref, _wrong(1.0), _SHA),
    ),
    "begin_upload": (
        "begin_upload",
        lambda c: c.service.begin_upload(c.principal, c.ref, artifact_document(_FILES)),
    ),
    "begin_upload_precheck": (
        "check_can_upload",
        lambda c: c.service.check_can_upload(c.principal, c.ref),
    ),
    "begin_upload_hostile_manifest": (
        "begin_upload",
        lambda c: c.service.begin_upload(c.principal, c.ref, {"files": "x"}),
    ),
    "receive_blob": (
        "receive_blob",
        lambda c: c.service.receive_blob(c.principal, c.ref, c.ids.upload, _SHA),
    ),
    "receive_blob_unknown_upload": (
        "receive_blob",
        lambda c: c.service.receive_blob(c.principal, c.ref, uuid.uuid4(), _SHA),
    ),
    "receive_blob_foreign_upload": (
        "receive_blob",
        lambda c: c.service.receive_blob(c.principal, c.ref, c.ids.foreign, _SHA),
    ),
    "receive_blob_str_upload": (
        "receive_blob",
        lambda c: c.service.receive_blob(c.principal, c.ref, _wrong("x"), _SHA),
    ),
    "receive_blob_bad_hash": (
        "receive_blob",
        lambda c: c.service.receive_blob(c.principal, c.ref, c.ids.upload, "../x"),
    ),
    "receive_blob_non_str_hash": (
        "receive_blob",
        lambda c: c.service.receive_blob(c.principal, c.ref, c.ids.upload, _wrong(5)),
    ),
    "put_blob": (
        "put_blob",
        lambda c: c.service.put_blob(c.principal, c.ref, c.ids.upload, _SHA, [b"{}"]),
    ),
    "put_blob_unknown_upload": (
        "put_blob",
        lambda c: c.service.put_blob(c.principal, c.ref, uuid.uuid4(), _SHA, [b"{}"]),
    ),
    "put_blob_foreign_upload": (
        "put_blob",
        lambda c: c.service.put_blob(c.principal, c.ref, c.ids.foreign, _SHA, [b"{}"]),
    ),
    "put_blob_str_upload": (
        "put_blob",
        lambda c: c.service.put_blob(c.principal, c.ref, _wrong("x"), _SHA, [b"{}"]),
    ),
    "commit_upload": (
        "commit_upload",
        lambda c: c.service.commit_upload(c.principal, c.ref, c.ids.upload),
    ),
    "commit_upload_unknown_upload": (
        "commit_upload",
        lambda c: c.service.commit_upload(c.principal, c.ref, uuid.uuid4()),
    ),
    "commit_upload_foreign_upload": (
        "commit_upload",
        lambda c: c.service.commit_upload(c.principal, c.ref, c.ids.foreign),
    ),
    "commit_upload_str_upload": (
        "commit_upload",
        lambda c: c.service.commit_upload(c.principal, c.ref, _wrong("x")),
    ),
    "commit_upload_hostile_source": (
        "commit_upload",
        lambda c: c.service.commit_upload(
            c.principal, c.ref, c.ids.upload, sources=[ArtifactSource("x" * 5000, _HUGE)]
        ),
    ),
    "abort_upload": (
        "abort_upload",
        lambda c: c.service.abort_upload(c.principal, c.ref, c.ids.upload),
    ),
    "abort_upload_unknown_upload": (
        "abort_upload",
        lambda c: c.service.abort_upload(c.principal, c.ref, uuid.uuid4()),
    ),
    "abort_upload_foreign_upload": (
        "abort_upload",
        lambda c: c.service.abort_upload(c.principal, c.ref, c.ids.foreign),
    ),
    "abort_upload_str_upload": (
        "abort_upload",
        lambda c: c.service.abort_upload(c.principal, c.ref, _wrong("x")),
    ),
    "delete_version_live": (
        "delete_version",
        lambda c: c.service.delete_version(c.principal, c.ref, 1),
    ),
    "delete_version_missing": (
        "delete_version",
        lambda c: c.service.delete_version(c.principal, c.ref, 5),
    ),
    "delete_version_deleted": (
        "delete_version",
        lambda c: c.service.delete_version(c.principal, c.ref, 2),
    ),
    "delete_version_str": (
        "delete_version",
        lambda c: c.service.delete_version(c.principal, c.ref, _wrong("1")),
    ),
    "delete_version_bool": (
        "delete_version",
        lambda c: c.service.delete_version(c.principal, c.ref, _wrong(True)),
    ),
    "delete_version_float": (
        "delete_version",
        lambda c: c.service.delete_version(c.principal, c.ref, _wrong(1.0)),
    ),
    "delete_version_huge": (
        "delete_version",
        lambda c: c.service.delete_version(c.principal, c.ref, _HUGE),
    ),
    "drop_artifact": ("drop_artifact", lambda c: c.service.drop_artifact(c.principal, c.ref)),
}

_DESTRUCTIVE = {"delete_version", "drop_artifact", "abort_upload"}
"""Methods whose recording run must stop at the first check: the artifact has to survive it."""

_EXPECTED: dict[str, list[tuple[Action, Securable]]] = {
    "get_artifact": [(Action.READ_TABLE, _table("churn"))],
    "list_versions": [(Action.READ_TABLE, _table("churn"))],
    "get_version": [(Action.READ_TABLE, _table("churn"))],
    "open_blob": [(Action.READ_TABLE, _table("churn"))],
    "begin_upload": [(Action.WRITE_TABLE, _table("churn"))],
    "check_can_upload": [(Action.WRITE_TABLE, _table("churn"))],
    "receive_blob": [(Action.WRITE_TABLE, _table("churn"))],
    "put_blob": [(Action.WRITE_TABLE, _table("churn"))],
    "commit_upload": [(Action.WRITE_TABLE, _table("churn"))],
    "abort_upload": [(Action.WRITE_TABLE, _table("churn"))],
    "delete_version": [(Action.DROP_TABLE, _table("churn"))],
    "drop_artifact": [(Action.DROP_TABLE, _table("churn"))],
}
"""Written from the spec, not computed from the code: what each method asks, in order.

Reading a version or a file needs `READ_TABLE` on the artifact's name; adding one needs
`WRITE_TABLE` (so does withdrawing one's own unfinished upload); creating the name on first use
also `CREATE_TABLE`; deleting a version or
dropping the artifact needs `DROP_TABLE`.
"""


def _counts(session: Session) -> tuple[int, ...]:
    """Count what a refused call must leave alone.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple of int
        Rows of tables, versions, uploads, blobs and audit events, then the
        number of objects in storage.
    """
    rows = tuple(
        session.scalar(select(func.count()).select_from(model)) or 0
        for model in (Table, ArtifactVersion, ArtifactUpload, ArtifactBlob, AuditEvent)
    )
    storage = StorageResolver(session).for_environment("dev").storage
    return (*rows, len(list(storage.list("artifacts"))))


def _setup(session: Session, tmp_path: Path, principal: PrincipalRef) -> _Ids:
    """Build the world the cases run in.

    `models.churn` has a live version 1 and a deleted version 2, and an open upload each of
    the caller and of another principal; `models.plain` is a table.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage.
    principal
        The caller.

    Returns
    -------
    _Ids
        The open uploads and the other principal.
    """
    allowed = artifact_service(session, tmp_path)
    store_version(allowed, principal, ARTIFACT_REF, _FILES)
    store_version(allowed, principal, ARTIFACT_REF, {"model.json": b'{"v": 1}'})
    session.execute(
        update(ArtifactVersion)
        .where(ArtifactVersion.version == 2)
        .values(deleted_at=datetime.now(UTC))
    )
    ensure_table(session, "dev", "models", "plain")
    other = dataclasses.replace(principal, id=uuid.uuid4(), name="other")
    session.add(Principal(id=other.id, type=other.type, name=other.name, is_active=True))
    session.commit()
    upload = allowed.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"3"}))
    foreign = allowed.begin_upload(other, ARTIFACT_REF, artifact_document({"m": b"4"}))
    return _Ids(upload.upload_id, foreign.upload_id, other)


def _open_uploads(session: Session) -> int:
    """Count the uploads that can still take files; a committed one does not count.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    int
        The number of rows with no committed version.
    """
    return (
        session.scalar(
            select(func.count())
            .select_from(ArtifactUpload)
            .where(ArtifactUpload.committed_version.is_(None))
        )
        or 0
    )


def _fill(session: Session, tmp_path: Path, principal: PrincipalRef) -> None:
    """Open uploads until `models.churn` has the most it allows.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage.
    principal
        The uploader.
    """
    allowed = artifact_service(session, tmp_path)
    for _ in range(MAX_OPEN_UPLOADS):
        if _open_uploads(session) >= MAX_OPEN_UPLOADS:
            break
        allowed.begin_upload(principal, ARTIFACT_REF, artifact_document({"m": b"5"}))
    assert _open_uploads(session) == MAX_OPEN_UPLOADS


def _leave_an_expired_upload(session: Session, principal: PrincipalRef) -> None:
    """Add an upload of `models.churn` that expired, as an abandoned one would be.

    A call that deletes it before it has authorized changes the counts a refusal must keep.

    Parameters
    ----------
    session
        Database session.
    principal
        The uploader.
    """
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    session.add(
        ArtifactUpload(
            id=uuid.uuid4(),
            artifact_id=artifact_id,
            principal_id=principal.id,
            manifest_sha256="0" * 64,
            manifest_json={"files": []},
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
            expires_at=datetime(2020, 1, 2, tzinfo=UTC),
        )
    )
    session.commit()


def _case(
    session: Session,
    authz: ActionAllowList,
    principal: PrincipalRef,
    ref: TableRef,
    ids: _Ids,
) -> _Case:
    """Bind a service to an authorization.

    Parameters
    ----------
    session
        Database session.
    authz
        The case's authorization.
    principal
        The caller.
    ref
        The name the call is made on.
    ids
        The uploads that exist.

    Returns
    -------
    _Case
        The case.
    """
    service = ArtifactService(session, StorageResolver(session), authz)
    return _Case(service, principal, ref, ids)


_MATRIX = [*sorted(_CALLS), "begin_upload_when_full"]


def _call_key(label: str) -> str:
    """Name the key of `_CALLS` a matrix label runs.

    Parameters
    ----------
    label
        A key of `_CALLS`, or ``begin_upload_when_full``.

    Returns
    -------
    str
        The key.
    """
    return "begin_upload" if label == "begin_upload_when_full" else label


def _call_for(label: str) -> Callable[[_Case], object]:
    """Find the call a label names.

    Parameters
    ----------
    label
        A key of `_CALLS`, or ``begin_upload_when_full``.

    Returns
    -------
    Callable
        The call.
    """
    return _CALLS[_call_key(label)][1]


@pytest.mark.parametrize("protected", [False, True], ids=["open", "protected"])
@pytest.mark.parametrize("label", _MATRIX)
def test_a_caller_without_the_privilege_learns_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef, label: str, protected: bool
) -> None:
    ids = _setup(session, tmp_path, principal)
    if label == "begin_upload_when_full":
        _fill(session, tmp_path, principal)
    if protected:
        session.execute(
            update(Environment).where(Environment.name == "dev").values(is_protected=True)
        )
        session.commit()
    call = _call_for(label)
    for ref in (ARTIFACT_REF, _GHOST, _PLAIN):
        # What the call must authorize is read off the same call with everything allowed: a
        # check that comes too early would hide the authorization from a recording of the
        # refusal itself. A name the call would create is free in this recording.
        method = _CALLS[_call_key(label)][0]
        recording = (_Peek if method in _DESTRUCTIVE else ActionAllowList)(*Action)
        baseline = _FREE if label.startswith("begin_upload") and ref is _GHOST else ref
        with contextlib.suppress(LakanaError):
            call(_case(session, recording, principal, baseline, ids))
        consulted = {action for action, _ in recording.seen}
        assert consulted, f"{label} on {ref.name} consulted no authorization at all"

        for missing in [None, *sorted(consulted)]:
            allowed = [] if missing is None else [a for a in Action if a is not missing]
            _leave_an_expired_upload(session, principal)
            before = _counts(session)
            with pytest.raises(PermissionDenied) as caught:
                call(_case(session, ActionAllowList(*allowed), principal, ref, ids))
            text = str(caught.value)
            assert "artifact" not in text.lower(), f"{label} on {ref.name} without {missing}"
            assert ref.name not in text
            # A guard that ran before authorization would audit `PROTECTED_WRITE_DENIED` for
            # a caller who has no grant.
            assert _counts(session) == before, f"{label} on {ref.name} without {missing}"


@pytest.mark.parametrize("label", sorted(_CALLS))
def test_each_method_asks_exactly_what_the_spec_says_in_that_order(
    session: Session, tmp_path: Path, principal: PrincipalRef, label: str
) -> None:
    ids = _setup(session, tmp_path, principal)
    method, call = _CALLS[label]
    recording = ActionAllowList(*Action)
    with contextlib.suppress(LakanaError):
        call(_case(session, recording, principal, ARTIFACT_REF, ids))
    assert recording.seen == _EXPECTED[method], label


def test_creating_a_name_also_needs_create_table_on_that_name(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    ids = _setup(session, tmp_path, principal)
    recording = ActionAllowList(*Action)
    call = _CALLS["begin_upload"][1]
    call(_case(session, recording, principal, _FREE, ids))
    assert recording.seen == [
        (Action.WRITE_TABLE, _table("free")),
        (Action.CREATE_TABLE, _table("free")),
    ]


def test_the_precheck_asks_what_beginning_an_upload_asks_for_a_new_name(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    ids = _setup(session, tmp_path, principal)
    recording = ActionAllowList(*Action)
    _CALLS["begin_upload_precheck"][1](_case(session, recording, principal, _FREE, ids))
    assert recording.seen == [
        (Action.WRITE_TABLE, _table("free")),
        (Action.CREATE_TABLE, _table("free")),
    ]
    assert session.scalar(select(func.count()).select_from(Table).where(Table.name == "free")) == 0


def test_a_listing_asks_for_the_environment_and_reads_per_artifact(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    _setup(session, tmp_path, principal)
    recording = _Recording(*Action)
    service = ArtifactService(session, StorageResolver(session), recording)
    assert [a.ref for a in service.list_artifacts(principal, "dev").items] == [ARTIFACT_REF]
    assert recording.seen == [(Action.READ_ENVIRONMENT, _ENVIRONMENT)]
    assert recording.permitted == [(Action.READ_TABLE, _table("churn"))]


def test_a_listing_needs_the_environment_and_hides_what_the_caller_may_not_read(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    _setup(session, tmp_path, principal)
    service = ArtifactService(
        session,
        StorageResolver(session),
        ActionAllowList(*[a for a in Action if a is not Action.READ_ENVIRONMENT]),
    )
    with pytest.raises(PermissionDenied) as caught:
        service.list_artifacts(principal, "nope")
    assert "artifact" not in str(caught.value).lower()
    blind = ArtifactService(
        session,
        StorageResolver(session),
        ActionAllowList(*[a for a in Action if a is not Action.READ_TABLE]),
    )
    assert blind.list_artifacts(principal, "dev").items == []
    assert blind.list_artifacts(principal, "dev", ARTIFACT_REF.namespace).items == []


def _runner(
    session: Session,
    principal: PrincipalRef,
    asset: str,
    inputs: dict[str, str],
    output: str,
) -> tuple[PrincipalRef, GrantAuthorization]:
    """Make a service account with an admin grant and the token of a running task attempt.

    Only the bound scope narrows what it may do, so every refusal below is the token's. The
    task run is real and the principal carries the run context authentication would give it.

    Parameters
    ----------
    session
        Database session.
    principal
        The principal that grants.
    asset
        The task's own asset.
    inputs
        The asset's declared inputs, parameter to dotted name.
    output
        ``table`` or ``artifact``, in the manifest and on the token.

    Returns
    -------
    tuple
        The principal as the token authenticates it, and the real authorization.
    """
    row = Principal(
        id=uuid.uuid4(),
        type=PrincipalType.SERVICE_ACCOUNT,
        name=f"runner-{uuid.uuid4().hex[:6]}",
        is_active=True,
    )
    session.add(row)
    session.commit()
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=row.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=principal.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()
    task = python_task_run(session, row.id, asset=asset, inputs=inputs, extra={"output": output})
    names = tuple(sorted(inputs.values()))
    bound = BoundTask(task.id, 1, asset, names, names, output)
    scope = TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4(), bound=bound)
    return (
        PrincipalRef(
            id=row.id,
            name=row.name,
            type=row.type,
            task_scope=scope,
            run_context=RunContext(task_run_id=task.id, claim=1),
        ),
        GrantAuthorization(session),
    )


def test_a_task_token_reads_only_declared_inputs_and_writes_only_its_own_asset(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    ids = _setup(session, tmp_path, principal)
    reads = (
        lambda s, p: s.open_blob(p, ARTIFACT_REF, 1, _SHA),
        lambda s, p: s.get_version(p, ARTIFACT_REF),
        lambda s, p: s.list_versions(p, ARTIFACT_REF),
    )
    writes = (
        lambda s, p: s.begin_upload(p, ARTIFACT_REF, artifact_document(_FILES)),
        lambda s, p: s.receive_blob(p, ARTIFACT_REF, ids.upload, _SHA),
        lambda s, p: s.commit_upload(p, ARTIFACT_REF, ids.upload),
    )

    # A task does not read its own earlier versions.
    p, authz = _runner(session, principal, "models.churn", {"rows": "silver.rows"}, "artifact")
    service = ArtifactService(session, StorageResolver(session), authz)
    for read in reads:
        with pytest.raises(PermissionDenied, match="declared inputs"):
            read(service, p)
    # The task that produces it passes the three steps of adding a version.
    upload = service.begin_upload(p, ARTIFACT_REF, artifact_document({"m": b"7"}))
    assert service.put_blob(
        p, ARTIFACT_REF, upload.upload_id, hashlib.sha256(b"7").hexdigest(), [b"7"]
    )
    assert service.commit_upload(p, ARTIFACT_REF, upload.upload_id).version.version == 3
    # A reader of the artifact reads it and cannot add to it.
    p, authz = _runner(session, principal, "gold.scores", {"model": "models.churn"}, "table")
    service = ArtifactService(session, StorageResolver(session), authz)
    assert service.get_version(p, ARTIFACT_REF).version == 3
    assert b"".join(service.open_blob(p, ARTIFACT_REF, 1, _SHA).chunks) == b"{}"
    assert [v.version for v in service.list_versions(p, ARTIFACT_REF)] == [3, 2, 1]
    for write in writes:
        with pytest.raises(PermissionDenied, match="own asset"):
            write(service, p)
    # An unrelated task can do none of it.
    p, authz = _runner(session, principal, "gold.other", {"rows": "silver.rows"}, "table")
    service = ArtifactService(session, StorageResolver(session), authz)
    for read in reads:
        with pytest.raises(PermissionDenied, match="declared inputs"):
            read(service, p)
    for write in writes:
        with pytest.raises(PermissionDenied, match="own asset"):
            write(service, p)


def test_no_task_token_can_delete_a_version_or_drop_an_artifact(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Whatever the token's asset, inputs or admin-grade grants: `DROP_TABLE` is never held."""
    _setup(session, tmp_path, principal)
    tokens = (
        _runner(session, principal, "models.churn", {"rows": "silver.rows"}, "artifact"),
        _runner(session, principal, "gold.scores", {"model": "models.churn"}, "table"),
        _runner(session, principal, "gold.other", {"rows": "silver.rows"}, "table"),
    )
    before = _counts(session)
    for p, authz in tokens:
        service = ArtifactService(session, StorageResolver(session), authz)
        calls: tuple[Callable[[ArtifactService, PrincipalRef], object], ...] = (
            lambda s, who: s.delete_version(who, ARTIFACT_REF, 1),
            lambda s, who: s.delete_version(who, ARTIFACT_REF, 2),
            lambda s, who: s.delete_version(who, _GHOST, 1),
            lambda s, who: s.drop_artifact(who, ARTIFACT_REF),
            lambda s, who: s.drop_artifact(who, _GHOST),
        )
        for call in calls:
            with pytest.raises(PermissionDenied) as caught:
                call(service, p)
            assert str(caught.value) != DELETE_FROM_RUN_MESSAGE
            assert "artifact" not in str(caught.value).lower()
    assert _counts(session)[:4] == before[:4]
    assert _counts(session)[5:] == before[5:]


def test_a_token_not_bound_to_a_run_cannot_delete_even_with_drop_table(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A token minted for a principal holds its `DROP_TABLE`; deleting still refuses."""
    _setup(session, tmp_path, principal)
    runner, authz = _runner(session, principal, "models.churn", {"rows": "silver.rows"}, "artifact")
    unbound = dataclasses.replace(
        runner,
        task_scope=TaskScope(environment="dev", asset_scope=["models.*"], token_id=uuid.uuid4()),
        run_context=None,
    )
    service = ArtifactService(session, StorageResolver(session), authz)
    # The principal itself may drop, so authorization passes and the refusal is the service's.
    for call in (
        lambda: service.delete_version(unbound, ARTIFACT_REF, 1),
        lambda: service.drop_artifact(unbound, ARTIFACT_REF),
    ):
        with pytest.raises(PermissionDenied) as caught:
            call()
        assert str(caught.value) == DELETE_FROM_RUN_MESSAGE
    plain = dataclasses.replace(runner, task_scope=None, run_context=None)
    service.delete_version(plain, ARTIFACT_REF, 1)


def test_every_public_method_has_a_case_here() -> None:
    public = {
        name
        for name, _ in inspect.getmembers(ArtifactService, inspect.isfunction)
        if not name.startswith("_")
    }
    # The two listings authorize an environment, not a name; their own tests are
    # `test_artifact_list_uploads.py` and the `list_artifacts` tests above.
    listings = {"list_artifacts", "list_uploads"}
    covered = {method for method, _ in _CALLS.values()} | listings
    assert public == covered, (
        "add each new public method of ArtifactService to _CALLS in "
        "test_artifact_service_authorization.py, with arguments that would fail a later check"
    )
    assert set(_EXPECTED) == covered - listings


def test_the_full_upload_refusal_is_a_conflict_for_one_who_may_write(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    ids = _setup(session, tmp_path, principal)
    _fill(session, tmp_path, principal)
    with pytest.raises(ConflictError, match="uploads in progress"):
        _call_for("begin_upload_when_full")(
            _case(session, ActionAllowList(*Action), principal, ARTIFACT_REF, ids)
        )
