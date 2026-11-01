"""The protected-environment write lockdown."""

from __future__ import annotations

import contextlib
import dataclasses
import re
import uuid
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import NamedTuple

import pyarrow as pa
import pytest
from core_test_helpers import (
    ActionAllowList,
    _config,
    _minimal_schema,
    _prepared_catalog,
    ensure_table,
    python_task_run,
)
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.snapshots import Operation, Snapshot, Summary
from pyiceberg.table.update import (
    AssertTableUUID,
    SetPropertiesUpdate,
    SetSnapshotRefUpdate,
)
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    RunContext,
    WriteFence,
)
from lakana_core.catalog import service as catalog_service
from lakana_core.catalog.append import build_transaction
from lakana_core.catalog.query_service import QueryService, WriteTarget
from lakana_core.catalog.service import CatalogService, _added_records
from lakana_core.catalog.writes import LineageSource
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    Environment,
    JobVersion,
    LineageEdge,
    Namespace,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    Table,
    TaskRun,
    TaskStatus,
    WriteMode,
)
from lakana_core.runs.lockdown import (
    OTHER_ASSET_MESSAGE,
    PROTECTED_MESSAGE,
    SUPERSEDED_MESSAGE,
    WRITE_FENCE_MESSAGE,
    ProtectedWriteGuard,
    WriteFenceRefused,
    WriteOperation,
    validate_break_glass_reason,
)
from lakana_core.runs.tasks import OwnTip, Written, read_own_tip, read_written
from lakana_core.storage.resolver import StorageResolver

_BRONZE = NamespacePath.from_parts(["bronze"])
_EVENTS = TableRef("dev", _BRONZE, "events")
_SRC = TableRef("dev", _BRONZE, "src")
_REASON = "incident 42: fix a bad row by hand"
_ROWS = pa.table(
    {"id": pa.array([7], pa.int64())},
    schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
)
"""One row matching `_minimal_schema()` (a required long `id`)."""


@pytest.fixture
def catalog(session: Session, tmp_path: Path) -> CatalogService:
    """Build a protected `dev` with `bronze.events`, behind allow-all authorization.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    built = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[_BRONZE], tables=[_EVENTS, _SRC]
    )
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    env.is_protected = True
    session.commit()
    return built


def _task(
    session: Session,
    principal: PrincipalRef,
    asset: str,
    status: TaskStatus = TaskStatus.RUNNING,
    env: str = "dev",
    *,
    run_status: RunStatus = RunStatus.RUNNING,
    cancel_requested: bool = False,
    run_as: uuid.UUID | None = None,
) -> RunContext:
    """Insert a run of a minimal project version with one task run and its query run.

    Parameters
    ----------
    session
        Database session.
    principal
        The principal the run writes as (unless `run_as` says otherwise).
    asset
        The task's asset.
    status
        The task's status.
    env
        The run's environment.
    run_status
        The run's status.
    cancel_requested
        Whether cancellation was requested.
    run_as
        The run's bound principal; defaults to `principal`.

    Returns
    -------
    RunContext
        The context the worker would build for the task's query run.
    """
    environment = session.scalars(select(Environment).where(Environment.name == env)).one()
    repo = Repository(
        id=uuid.uuid4(),
        name=f"r{uuid.uuid4().hex[:6]}",
        kind=RepositoryKind.HTTPS,
        url="https://h/r.git",
    )
    session.add(repo)
    session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=environment.id,
        commit_sha="a" * 40,
        manifest_json={},
        code_uri="file:///x",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    run = Run(
        id=uuid.uuid4(),
        environment_id=environment.id,
        repository_id=repo.id,
        job_version_id=version.id,
        run_as_principal_id=principal.id if run_as is None else run_as,
        kind=RunKind.FULL,
        status=run_status,
        cancel_requested=cancel_requested,
        selection_json={},
    )
    session.add(run)
    session.flush()
    query = QueryRun(
        id=uuid.uuid4(),
        environment_id=environment.id,
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.RUNNING,
    )
    session.add(query)
    session.flush()
    task = TaskRun(
        id=uuid.uuid4(),
        run_id=run.id,
        asset=asset,
        status=status,
        query_run_id=query.id,
        max_attempts=1,
        timeout_seconds=60,
        retry_delay_seconds=0,
        upstream_json=[],
    )
    session.add(task)
    session.flush()
    query.task_run_id = task.id
    session.commit()
    return RunContext(task_run_id=task.id, query_run_id=query.id)


def _in_run(principal: PrincipalRef, context: RunContext) -> PrincipalRef:
    """Return the principal as the worker builds it inside a task run.

    Parameters
    ----------
    principal
        The run principal.
    context
        The run context.

    Returns
    -------
    PrincipalRef
        With a run context.
    """
    return dataclasses.replace(principal, run_context=context)


def _events(session: Session, event_type: AuditEventType) -> list[AuditEvent]:
    """Return the audit events of one type, oldest first.

    Parameters
    ----------
    session
        Database session.
    event_type
        The type to select.

    Returns
    -------
    list[AuditEvent]
        Matching events in time order.
    """
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == event_type)
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )


_WRITES: dict[str, Callable[[CatalogService, PrincipalRef], object]] = {
    "create_namespace": lambda c, p: c.create_namespace(
        p, "dev", NamespacePath.from_parts(["silver"])
    ),
    "drop_namespace": lambda c, p: c.drop_namespace(p, "dev", _BRONZE),
    "update_namespace": lambda c, p: c.update_namespace_properties(
        p, "dev", _BRONZE, {"a": "b"}, []
    ),
    "create_table": lambda c, p: c.create_table(
        p, TableRef("dev", _BRONZE, "t2"), schema=_minimal_schema()
    ),
    "create_external_table": lambda c, p: c.create_external_table(
        p, TableRef("dev", _BRONZE, "ext"), location_pattern="raw/x/*.parquet", format="parquet"
    ),
    "append": lambda c, p: c.append(p, _EVENTS, _ROWS),
    "write_batches": lambda c, p: c.write_batches(
        p, _EVENTS, mode=WriteMode.REPLACE, schema=pa.schema([("id", pa.int64())]), batches=[]
    ),
    "commit_table": lambda c, p: c.commit_table(p, _EVENTS, requirements=[], updates=[]),
    "create_view": lambda c, p: c.create_or_replace_view(
        p, TableRef("dev", _BRONZE, "v"), sql="SELECT 1", references=[], columns=[]
    ),
    "drop_table": lambda c, p: c.drop_table(p, _EVENTS),
    "rename_table": lambda c, p: c.rename_table(p, _EVENTS, TableRef("dev", _BRONZE, "moved")),
    "undrop_table": lambda c, p: c.undrop_table(p, _EVENTS, new_name="back"),
}


@pytest.mark.parametrize("name", sorted(_WRITES))
def test_every_write_path_is_refused_without_a_run_context(
    session: Session, catalog: CatalogService, principal: PrincipalRef, name: str
) -> None:
    """Refused regardless of grants, audited, with the documented message."""
    with pytest.raises(PermissionDenied) as info:
        _WRITES[name](catalog, principal)
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")
    session.rollback()
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PROTECTED_WRITE_DENIED)
    ).one()
    assert event.securable_environment == "dev"
    assert event.detail["in_run"] is False
    assert event.detail["protected"] is True


def test_a_running_task_may_write_its_own_asset(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The run context allows write_batches/append/commit on the task's asset."""
    p = _in_run(principal, _task(session, principal, "bronze.events"))
    result = catalog.append(p, _EVENTS, _ROWS)
    assert result.rows == 1


def test_a_running_task_may_create_its_assets_namespace(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The runner creates a missing namespace inside the run context."""
    p = _in_run(principal, _task(session, principal, "staging.clean"))
    catalog.create_namespace(p, "dev", NamespacePath.from_parts(["staging"]))
    with pytest.raises(PermissionDenied):
        catalog.create_namespace(p, "dev", NamespacePath.from_parts(["other"]))


@pytest.mark.parametrize(
    ("asset", "status", "operation"),
    [
        ("bronze.other", TaskStatus.RUNNING, "append"),
        ("bronze.events", TaskStatus.SUCCEEDED, "append"),
        ("bronze.events", TaskStatus.PENDING, "append"),
        ("bronze.events", TaskStatus.RUNNING, "drop_table"),
        ("bronze.events", TaskStatus.RUNNING, "rename_table"),
        ("bronze.back", TaskStatus.RUNNING, "undrop_table"),
        ("bronze.events", TaskStatus.RUNNING, "drop_namespace"),
        ("bronze.events", TaskStatus.RUNNING, "create_external_table"),
        ("bronze.t2", TaskStatus.RUNNING, "create_table"),
        ("bronze.events", TaskStatus.RUNNING, "update_namespace"),
    ],
)
def test_a_run_context_covers_only_its_own_running_asset(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    asset: str,
    status: TaskStatus,
    operation: str,
) -> None:
    """Another asset, a task not running, and non-materializing operations are refused."""
    p = _in_run(principal, _task(session, principal, asset, status))
    with pytest.raises(PermissionDenied):
        _WRITES[operation](catalog, p)
    session.rollback()
    assert _events(session, AuditEventType.PROTECTED_WRITE_DENIED)[-1].detail["in_run"] is True


def test_a_forged_or_foreign_run_context_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An unknown task run id, or one of another environment, never opens prod."""
    with pytest.raises(PermissionDenied):
        _WRITES["append"](catalog, _in_run(principal, RunContext(uuid.uuid4(), uuid.uuid4())))
    CatalogService(session, StorageResolver(session), AllowAllAuthorization()).create_environment(
        principal,
        name="staging",
        storage=_config(tmp_path / "staging"),
    )
    foreign = _task(session, principal, "bronze.events", env="staging")
    with pytest.raises(PermissionDenied):
        _WRITES["append"](catalog, _in_run(principal, foreign))


def test_break_glass_writes_once_audited_and_needs_manage_environment(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Exactly that write; grants still apply; audited once per target."""
    allowed = ActionAllowList(Action.MANAGE_ENVIRONMENT, Action.WRITE_TABLE, Action.READ_TABLE)
    catalog = _prepared_catalog(session, tmp_path, allowed, namespaces=[_BRONZE], tables=[_EVENTS])
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    env.is_protected = True
    session.commit()
    glass = dataclasses.replace(principal, break_glass_reason=_REASON)
    catalog.append(glass, _EVENTS, _ROWS)
    (event,) = _events(session, AuditEventType.BREAK_GLASS_WRITE)
    assert event.detail == {"reason": _REASON, "operation": "append", "target": "bronze.events"}
    assert event.principal_id == principal.id
    manage_denied = ActionAllowList(Action.WRITE_TABLE)
    no_manage = _prepared_catalog(session, tmp_path, manage_denied)
    with pytest.raises(PermissionDenied, match="may not MANAGE_ENVIRONMENT"):
        no_manage.append(glass, _EVENTS, _ROWS)
    assert [a for a, _ in manage_denied.seen] == [Action.WRITE_TABLE, Action.MANAGE_ENVIRONMENT]
    grant_denied = ActionAllowList(Action.MANAGE_ENVIRONMENT)
    no_grant = _prepared_catalog(session, tmp_path, grant_denied)
    with pytest.raises(PermissionDenied) as info:
        no_grant.drop_table(glass, _EVENTS)
    assert str(info.value) != PROTECTED_MESSAGE.format(environment="dev")
    assert [a for a, _ in grant_denied.seen] == [Action.DROP_TABLE]
    assert len(_events(session, AuditEventType.BREAK_GLASS_WRITE)) == 1


@pytest.mark.parametrize("reason", ["too short", "x" * 501, "          "])
def test_break_glass_reason_length(reason: str) -> None:
    """10-500 characters, not blank."""
    with pytest.raises(ValidationError, match="break-glass reason"):
        validate_break_glass_reason(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "INC-1 fix\nFAKE: approved by CISO",
        "INC-1 fix by hand\x1b[2K",
        "INC-1\tfix by hand",
        "INC-1 fix\x7f by hand",
        "INC-1 fix\u200b by hand",
        "INC-1 fix\u202e by hand",
        "INC-1 fix\u2028FAKE: approved by CISO",
        "INC-1 fix\u2029FAKE: approved by CISO",
        "INC-1 fix\ud800 by hand",
        "INC-1 fix\ue000 by hand",
    ],
)
def test_break_glass_reason_refuses_control_and_format_characters(reason: str) -> None:
    """The reason lands in the audit log: no forged lines, escapes or invisible characters."""
    with pytest.raises(ValidationError, match="control or invisible formatting characters"):
        validate_break_glass_reason(reason)


def test_an_unassigned_code_point_is_not_refused() -> None:
    """`Cn` depends on the build's Unicode tables, so it is not in the refused set.

    U+FFFF is a permanent noncharacter: `Cn` in every Unicode version.
    """
    assert validate_break_glass_reason("INC-1 fix\uffff by hand") == "INC-1 fix\uffff by hand"


def test_surrounding_newlines_are_stripped_before_the_character_check() -> None:
    """Only characters inside the reason count; a trailing newline from a shell is fine."""
    assert validate_break_glass_reason(f"\n{_REASON}\r\n") == _REASON


def test_valid_reason_is_returned_stripped() -> None:
    """Surrounding whitespace is dropped."""
    assert validate_break_glass_reason(f"  {_REASON}  ") == _REASON


def test_maintenance_and_reads_are_not_affected(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """MAINTAIN_TABLE commits and reads pass in a protected environment."""
    catalog.maintain(principal, _EVENTS, dry_run=True)
    catalog.commit_table(
        principal, _EVENTS, requirements=[], updates=[], action=Action.MAINTAIN_TABLE
    )
    assert catalog.load_table(principal, _EVENTS).metadata is not None


def test_unprotected_environment_is_unchanged(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Without `is_protected` the guard never refuses."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_BRONZE])
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["staging"]))
    assert catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["staging"]))
    assert not session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PROTECTED_WRITE_DENIED)
    ).all()


def test_unknown_environment_is_left_to_the_operation(
    session: Session, principal: PrincipalRef
) -> None:
    """The guard does not answer for a missing environment; the operation's own 404 does."""
    ProtectedWriteGuard(session, AllowAllAuthorization()).check(
        principal, "ghost", WriteOperation.CREATE_NAMESPACE, ("x",)
    )
    with pytest.raises(NotFoundError):
        CatalogService(session, StorageResolver(session), AllowAllAuthorization()).create_namespace(
            principal, "ghost", NamespacePath.from_parts(["x"])
        )
    assert not session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PROTECTED_WRITE_DENIED)
    ).all()


def test_sql_write_submission_is_refused_and_break_glass_is_stored(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`submit_write` fails fast; with break-glass the reason travels on the run, unaudited yet."""
    service = QueryService(session, AllowAllAuthorization())
    target = WriteTarget(table="bronze.events", mode=WriteMode.APPEND)
    with pytest.raises(PermissionDenied) as info:
        service.submit_write(principal, "dev", "SELECT 1 AS id", target)
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")
    (denied,) = _events(session, AuditEventType.PROTECTED_WRITE_DENIED)
    assert denied.detail["operation"] == "sql_write"
    glass = dataclasses.replace(principal, break_glass_reason=_REASON)
    run = service.submit_write(glass, "dev", "SELECT 1 AS id", target)
    stored = session.get(QueryRun, run.id)
    assert stored is not None
    assert stored.break_glass_reason == _REASON
    assert not session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.BREAK_GLASS_WRITE)
    ).all()


def test_replace_is_guarded_through_write_batches(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`CatalogService.replace` (the SDK's `tables.replace`) reaches the WRITE choke point."""
    with pytest.raises(PermissionDenied) as info:
        catalog.replace(principal, _EVENTS, _ROWS)
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")
    session.rollback()
    event = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.PROTECTED_WRITE_DENIED)
    ).one()
    assert event.detail["operation"] == "write"
    p = _in_run(principal, _task(session, principal, "bronze.events"))
    assert catalog.replace(p, _EVENTS, _ROWS).rows == 1


def test_a_running_task_may_create_view_commit_and_create_its_new_asset(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """In-run: a view, a commit and a `CREATE` write each work on the task's own asset."""
    view = _in_run(principal, _task(session, principal, "bronze.v"))
    catalog.create_or_replace_view(
        view, TableRef("dev", _BRONZE, "v"), sql="SELECT 1", references=[], columns=[]
    )
    commit = _in_run(principal, _task(session, principal, "bronze.events"))
    catalog.commit_table(commit, _EVENTS, requirements=[], updates=[])
    fresh = _in_run(principal, _task(session, principal, "bronze.fresh"))
    result = catalog.write_batches(
        fresh,
        TableRef("dev", _BRONZE, "fresh"),
        mode=WriteMode.CREATE,
        schema=_ROWS.schema,
        batches=_ROWS.to_batches(),
    )
    assert result.rows == 1
    assert not _events(session, AuditEventType.PROTECTED_WRITE_DENIED)


@pytest.mark.parametrize("case", ["finished-run", "cancel-requested", "other-principal"])
def test_a_run_context_needs_a_live_run_written_as_its_principal(
    session: Session, catalog: CatalogService, principal: PrincipalRef, case: str
) -> None:
    """A cancelled or finished run, or one bound to another principal, grants nothing."""
    kwargs: dict = {}
    if case == "finished-run":
        kwargs["run_status"] = RunStatus.CANCELLED
    elif case == "cancel-requested":
        kwargs["cancel_requested"] = True
    else:
        other = Principal(
            id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name="other", is_active=True
        )
        session.add(other)
        session.commit()
        kwargs["run_as"] = other.id
    p = _in_run(principal, _task(session, principal, "bronze.events", **kwargs))
    with pytest.raises(PermissionDenied) as info:
        catalog.append(p, _EVENTS, _ROWS)
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")


def test_a_zombie_attempt_with_a_stale_query_run_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The task was re-claimed by a different query run: the old attempt may not write."""
    context = _task(session, principal, "bronze.events")
    task = session.get(TaskRun, context.task_run_id)
    assert task is not None
    reclaimed = QueryRun(
        id=uuid.uuid4(),
        environment_id=session.scalars(select(Environment)).one().id,
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.RUNNING,
    )
    session.add(reclaimed)
    session.flush()
    task.query_run_id = reclaimed.id
    session.commit()
    with pytest.raises(PermissionDenied):
        catalog.append(_in_run(principal, context), _EVENTS, _ROWS)
    live = RunContext(task_run_id=context.task_run_id, query_run_id=reclaimed.id)
    assert catalog.append(_in_run(principal, live), _EVENTS, _ROWS).rows == 1


def test_a_refused_break_glass_leaves_no_break_glass_audit_row(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """The grant denial after a passed guard must not commit a `BREAK_GLASS_WRITE` row."""
    seed = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[_BRONZE], tables=[_EVENTS]
    )
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    env.is_protected = True
    session.commit()
    del seed
    audited = AuditingAuthorization(
        ActionAllowList(Action.MANAGE_ENVIRONMENT), session, DenialAuditLimiter(10_000)
    )
    catalog = _prepared_catalog(session, tmp_path, audited)
    glass = dataclasses.replace(principal, break_glass_reason=_REASON)
    with pytest.raises(PermissionDenied):
        catalog.drop_table(glass, _EVENTS)
    assert _events(session, AuditEventType.AUTHORIZATION_DENIED)
    assert not _events(session, AuditEventType.BREAK_GLASS_WRITE)


@pytest.mark.parametrize("path", ["append", "write_batches"])
def test_a_mid_write_denial_persists_none_of_the_writes_pending_state(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """Task lease lost between the first guard and the commit guard: only the denial persists."""
    context = _task(session, principal, "bronze.events")
    p = _in_run(principal, context)
    guard = catalog._guard
    real = guard.check

    def flip(*args: object, **kwargs: object) -> None:
        if args[2] is WriteOperation.COMMIT:
            session.execute(
                update(TaskRun)
                .where(TaskRun.id == context.task_run_id)
                .values(status=TaskStatus.PENDING)
            )
        real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(guard, "check", flip)
    sources = ["bronze.src"]
    with pytest.raises(PermissionDenied):
        if path == "append":
            catalog.append(p, _EVENTS, _ROWS, sources=sources)
        else:
            lineage = catalog.resolve_lineage_sources(p, "dev", sources)
            catalog.write_batches(
                p,
                _EVENTS,
                mode=WriteMode.APPEND,
                schema=_ROWS.schema,
                batches=_ROWS.to_batches(),
                sources=lineage,
            )
    session.rollback()
    assert _events(session, AuditEventType.PROTECTED_WRITE_DENIED)
    assert not _events(session, AuditEventType.TABLE_APPENDED)
    assert not _events(session, AuditEventType.TABLE_WRITE)
    assert not session.scalars(select(LineageEdge)).all()


def test_maintenance_exemption_is_structural(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`action=MAINTAIN_TABLE` carries snapshot expiry only; a data change is refused."""
    loaded = catalog.load_table(principal, _EVENTS)
    update = SetPropertiesUpdate(updates={"a": "b"})
    with pytest.raises(ValidationError, match="maintenance"):
        catalog.commit_table(
            principal,
            _EVENTS,
            requirements=[AssertTableUUID(uuid=loaded.metadata.table_uuid)],
            updates=[update],
            action=Action.MAINTAIN_TABLE,
        )


def test_submit_write_validates_a_break_glass_reason_everywhere(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Even in an unprotected environment the reason is validated and stored stripped."""
    catalog = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[_BRONZE], tables=[_EVENTS]
    )
    del catalog
    service = QueryService(session, AllowAllAuthorization())
    target = WriteTarget(table="bronze.events", mode=WriteMode.APPEND)
    short = dataclasses.replace(principal, break_glass_reason="short")
    with pytest.raises(ValidationError, match="break-glass reason"):
        service.submit_write(short, "dev", "SELECT 1 AS id", target)
    padded = dataclasses.replace(principal, break_glass_reason=f"  {_REASON}  ")
    run = service.submit_write(padded, "dev", "SELECT 1 AS id", target)
    assert run.break_glass_reason == _REASON


def test_a_stale_pending_break_glass_is_not_recorded_for_another_target(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A write that failed after the guard must not leak its row into the next write's commit."""
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    glass = dataclasses.replace(principal, break_glass_reason=_REASON)
    guard.check(glass, "dev", WriteOperation.DROP_TABLE, ("bronze", "a"))
    guard.check(glass, "dev", WriteOperation.DROP_TABLE, ("bronze", "b"))
    guard.record_pending()
    session.commit()
    (event,) = _events(session, AuditEventType.BREAK_GLASS_WRITE)
    assert event.detail["target"] == "bronze.b"
    guard.record_pending()
    session.commit()
    assert len(_events(session, AuditEventType.BREAK_GLASS_WRITE)) == 1


def _reclaim(session: Session, context: RunContext) -> None:
    """Do what the scheduler does to a task whose lease expired: put it back to pending.

    Parameters
    ----------
    session
        Database session.
    context
        The task's run context.
    """
    session.execute(
        update(TaskRun).where(TaskRun.id == context.task_run_id).values(status=TaskStatus.PENDING)
    )


def test_a_run_context_is_verified_again_right_before_the_commit(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A reclaim between the guard's check and the commit refuses the write (audited, in a run)."""
    context = _task(session, principal, "bronze.events")
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    p = _in_run(principal, context)
    guard.check(p, "dev", WriteOperation.APPEND, ("bronze", "events"))
    guard.record_pending()  # still the current attempt: nothing to say
    guard.check(p, "dev", WriteOperation.APPEND, ("bronze", "events"))
    _reclaim(session, context)
    with pytest.raises(PermissionDenied) as info:
        guard.record_pending()
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")
    assert _events(session, AuditEventType.PROTECTED_WRITE_DENIED)[-1].detail["in_run"] is True
    guard.record_pending()  # the verification is spent once it has failed


@pytest.mark.parametrize("cause", ["reclaimed", "cancel_requested", "superseded"])
def test_a_write_whose_attempt_is_superseded_before_the_commit_lands_nothing(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
) -> None:
    """The pointer CAS has run; the attempt loses its standing; the commit never happens."""
    context = _task(session, principal, "bronze.events")
    before = catalog.load_table(principal, _EVENTS).metadata_location
    original = catalog_service.commit_metadata_pointer

    def change_standing(
        pointer_session: Session,
        table_id: uuid.UUID,
        *,
        expected_location: str | None,
        new_location: str,
        publish: bool = False,
    ) -> None:
        if cause == "reclaimed":
            _reclaim(pointer_session, context)
        elif cause == "cancel_requested":
            pointer_session.execute(update(Run).values(cancel_requested=True))
        else:
            pointer_session.execute(
                update(TaskRun).where(TaskRun.id == context.task_run_id).values(query_run_id=None)
            )
        original(
            pointer_session,
            table_id,
            expected_location=expected_location,
            new_location=new_location,
            publish=publish,
        )

    monkeypatch.setattr(catalog_service, "commit_metadata_pointer", change_standing)
    with pytest.raises(PermissionDenied):
        catalog.append(_in_run(principal, context), _EVENTS, _ROWS)
    session.rollback()
    assert catalog.load_table(principal, _EVENTS).metadata_location == before


@pytest.fixture
def open_catalog(session: Session, tmp_path: Path) -> CatalogService:
    """Build an unprotected `dev` with `bronze.events` and `bronze.src`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[_BRONZE], tables=[_EVENTS, _SRC]
    )


@pytest.mark.parametrize(
    "case", ["reclaimed", "superseded", "finished-run", "cancel-requested", "other-principal"]
)
def test_a_stale_run_context_is_refused_in_an_unprotected_environment(
    session: Session, open_catalog: CatalogService, principal: PrincipalRef, case: str
) -> None:
    """I1: a zombie attempt may not write in dev either; neutral message, audited."""
    kwargs: dict = {}
    if case == "reclaimed":
        kwargs["status"] = TaskStatus.PENDING
    elif case == "finished-run":
        kwargs["run_status"] = RunStatus.FAILED
    elif case == "cancel-requested":
        kwargs["cancel_requested"] = True
    elif case == "other-principal":
        other = Principal(
            id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name="other", is_active=True
        )
        session.add(other)
        session.commit()
        kwargs["run_as"] = other.id
    context = _task(session, principal, "bronze.events", **kwargs)
    if case == "superseded":
        context = RunContext(task_run_id=context.task_run_id, query_run_id=uuid.uuid4())
    before = open_catalog.load_table(principal, _EVENTS).metadata_location
    with pytest.raises(PermissionDenied) as info:
        open_catalog.append(_in_run(principal, context), _EVENTS, _ROWS)
    assert str(info.value) == SUPERSEDED_MESSAGE
    session.rollback()
    (event,) = _events(session, AuditEventType.PROTECTED_WRITE_DENIED)
    assert event.detail == {
        "operation": "append",
        "target": "bronze.events",
        "in_run": True,
        "protected": False,
    }
    assert open_catalog.load_table(principal, _EVENTS).metadata_location == before


def test_a_live_run_context_in_an_unprotected_environment_keeps_its_grants_scope(
    session: Session, open_catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Unprotected: only liveness is checked, not the asset; writes without a context pass."""
    p = _in_run(principal, _task(session, principal, "bronze.events"))
    assert open_catalog.append(p, _SRC, _ROWS).rows == 1
    assert open_catalog.append(principal, _EVENTS, _ROWS).rows == 1
    assert not _events(session, AuditEventType.PROTECTED_WRITE_DENIED)


def test_a_reclaim_before_the_commit_lands_nothing_in_an_unprotected_environment(
    session: Session,
    open_catalog: CatalogService,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I1: the pointer CAS has run; the scheduler reclaims; the re-check refuses the commit."""
    context = _task(session, principal, "bronze.events")
    before = open_catalog.load_table(principal, _EVENTS).metadata_location
    original = catalog_service.commit_metadata_pointer

    def reclaim_first(
        pointer_session: Session,
        table_id: uuid.UUID,
        *,
        expected_location: str | None,
        new_location: str,
        publish: bool = False,
    ) -> None:
        _reclaim(pointer_session, context)
        original(
            pointer_session,
            table_id,
            expected_location=expected_location,
            new_location=new_location,
            publish=publish,
        )

    monkeypatch.setattr(catalog_service, "commit_metadata_pointer", reclaim_first)
    with pytest.raises(PermissionDenied) as info:
        open_catalog.append(_in_run(principal, context), _EVENTS, _ROWS)
    assert str(info.value) == SUPERSEDED_MESSAGE
    session.rollback()
    assert open_catalog.load_table(principal, _EVENTS).metadata_location == before
    assert _events(session, AuditEventType.PROTECTED_WRITE_DENIED)[-1].detail["protected"] is False


@pytest.mark.parametrize("name", sorted(_WRITES))
def test_an_unauthorized_caller_gets_the_grant_denial_not_the_protection_status(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, name: str
) -> None:
    """The guard runs after authorization: no grant, no word about the environment's protection."""
    denied = ActionAllowList()
    unauthorized = _prepared_catalog(session, tmp_path, denied)
    with pytest.raises(PermissionDenied) as info:
        _WRITES[name](unauthorized, principal)
    assert str(info.value).startswith("may not ")
    session.rollback()
    assert not _events(session, AuditEventType.PROTECTED_WRITE_DENIED)


def test_an_unauthorized_sql_write_gets_the_grant_denial_not_the_protection_status(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`submit_write` authorizes the target before the guard looks at the environment."""
    service = QueryService(session, ActionAllowList(Action.SUBMIT_QUERY))
    target = WriteTarget(table="bronze.events", mode=WriteMode.APPEND)
    with pytest.raises(PermissionDenied, match="may not WRITE_TABLE"):
        service.submit_write(principal, "dev", "SELECT 1 AS id", target)
    assert not _events(session, AuditEventType.PROTECTED_WRITE_DENIED)


def test_a_legacy_dotted_namespace_never_matches_a_task_asset_by_its_display_name(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Targets compare as parts: `["bronze", "x.y"]`/`z` is not the asset `bronze.x.y.z`."""
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    env.is_protected = False
    legacy = NamespacePath(("bronze", "x.y"))
    session.add(Namespace(id=uuid.uuid4(), environment_id=env.id, path=legacy.joined()))
    session.commit()
    catalog.create_table(principal, TableRef("dev", legacy, "z"), schema=_minimal_schema())
    env.is_protected = True
    session.commit()
    p = _in_run(principal, _task(session, principal, "bronze.x.y.z"))
    with pytest.raises(PermissionDenied):
        catalog.append(p, TableRef("dev", legacy, "z"), _ROWS)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied):
        guard.check(p, "dev", WriteOperation.CREATE_NAMESPACE, ("bronze", "x.y"))
    guard.check(p, "dev", WriteOperation.CREATE_NAMESPACE, ("bronze", "x", "y"))
    guard.check(p, "dev", WriteOperation.APPEND, ("bronze", "x", "y", "z"))


def test_a_maintenance_commit_records_no_break_glass_left_pending_by_an_earlier_write(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """MAINTAIN_TABLE skips the guard, so it must not land what an earlier check left pending."""
    glass = dataclasses.replace(principal, break_glass_reason=_REASON)
    catalog._guard.check(glass, "dev", WriteOperation.APPEND, ("bronze", "events"))
    catalog.commit_table(
        principal, _EVENTS, requirements=[], updates=[], action=Action.MAINTAIN_TABLE
    )
    assert not _events(session, AuditEventType.BREAK_GLASS_WRITE)


@pytest.mark.parametrize("namespace", ["bronze", "nowhere"])
def test_an_ungranted_view_creation_is_denied_before_the_namespace_is_looked_up(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    namespace: str,
) -> None:
    """No existence oracle: an existing and a missing namespace both answer the audited denial."""
    audited = AuditingAuthorization(ActionAllowList(), session, DenialAuditLimiter(10_000))
    unauthorized = _prepared_catalog(session, tmp_path, audited)
    ref = TableRef("dev", NamespacePath.from_parts([namespace]), "v")
    with pytest.raises(PermissionDenied, match="may not CREATE_TABLE"):
        unauthorized.create_or_replace_view(
            principal, ref, sql="SELECT 1", references=[], columns=[]
        )
    session.rollback()
    assert len(_events(session, AuditEventType.AUTHORIZATION_DENIED)) == 1
    assert not _events(session, AuditEventType.PROTECTED_WRITE_DENIED)


def _bound(session: Session, principal: PrincipalRef, asset: str, claim: int = 1) -> PrincipalRef:
    """Return the principal as authentication builds it for a bound task token.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    asset
        The task's asset.
    claim
        The task's claim counter.

    Returns
    -------
    PrincipalRef
        With the claim-form run context of a new running task run.
    """
    task = python_task_run(session, principal.id, asset=asset, claim=claim)
    return _in_run(principal, RunContext(task_run_id=task.id, claim=task.claim_count))


def test_a_bound_task_may_write_its_own_asset_in_a_protected_environment(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    p = _bound(session, principal, "bronze.events", claim=4)
    assert catalog.append(p, _EVENTS, _ROWS).rows == 1
    assert catalog.replace(p, _EVENTS, _ROWS).rows == 1


def test_a_bound_task_may_create_its_new_asset_and_its_namespace(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    p = _bound(session, principal, "bronze.staging.fresh")
    catalog.create_namespace(p, "dev", NamespacePath.from_parts(["bronze", "staging"]))
    created = catalog.replace(
        p,
        TableRef("dev", NamespacePath.from_parts(["bronze", "staging"]), "fresh"),
        _ROWS,
        expected_snapshot_id="none",
    )
    assert created.rows == 1
    with pytest.raises(PermissionDenied):
        catalog.create_namespace(p, "dev", NamespacePath.from_parts(["bronze", "other"]))


@pytest.mark.parametrize("operation", ["append", "drop_table", "rename_table", "create_table"])
def test_a_bound_task_may_not_touch_another_table(
    session: Session, catalog: CatalogService, principal: PrincipalRef, operation: str
) -> None:
    p = _bound(session, principal, "bronze.other")
    with pytest.raises(PermissionDenied) as info:
        _WRITES[operation](catalog, p)
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")


@pytest.mark.parametrize(
    "case",
    [
        "wrong-claim",
        "not-running",
        "finished-run",
        "cancel-requested",
        "other-principal",
        "unknown",
    ],
)
def test_a_stale_or_forged_claim_context_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, case: str
) -> None:
    task = python_task_run(
        session,
        principal.id,
        asset="bronze.events",
        status=TaskStatus.SUCCEEDED if case == "not-running" else TaskStatus.RUNNING,
    )
    context = RunContext(task_run_id=task.id, claim=1)
    run = session.get(Run, task.run_id)
    assert run is not None
    if case == "wrong-claim":
        context = RunContext(task_run_id=task.id, claim=2)
    elif case == "finished-run":
        run.status = RunStatus.FAILED
    elif case == "cancel-requested":
        run.cancel_requested = True
    elif case == "other-principal":
        other = Principal(
            id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name="other", is_active=True
        )
        session.add(other)
        session.flush()
        run.run_as_principal_id = other.id
    elif case == "unknown":
        context = RunContext(task_run_id=uuid.uuid4(), claim=1)
    session.commit()
    with pytest.raises(PermissionDenied):
        catalog.append(_in_run(principal, context), _EVENTS, _ROWS)
    session.rollback()
    assert _events(session, AuditEventType.PROTECTED_WRITE_DENIED)[-1].detail["in_run"] is True


def _setting_back_context(
    session: Session, principal: PrincipalRef, *, setting_back: bool = True
) -> PrincipalRef:
    """Build a claim-form context for a task of a cancel-requested run.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    setting_back
        Whether the task row is marked as setting its table back.

    Returns
    -------
    PrincipalRef
        The principal inside the task's claim.
    """
    task = python_task_run(session, principal.id, asset="bronze.events")
    task.setting_back = setting_back
    run = session.get(Run, task.run_id)
    assert run is not None
    run.cancel_requested = True
    session.commit()
    return _in_run(principal, RunContext(task_run_id=task.id, claim=1))


def test_a_claim_that_sets_the_table_back_may_commit_it_in_a_cancel_requested_run(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The one exception to the cancel request: a commit of the own table, by a set-back claim."""
    p = _setting_back_context(session, principal)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.COMMIT, ("bronze", "events"), rollback=True)
    guard.record_pending()
    assert not _events(session, AuditEventType.PROTECTED_WRITE_DENIED)


def test_a_commit_that_is_not_the_rollback_is_refused_for_a_set_back_claim(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A user-shaped commit of the own table (no rollback fact) keeps today's refusal."""
    p = _setting_back_context(session, principal)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.COMMIT, ("bronze", "events"))
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")


@pytest.mark.parametrize(
    ("operation", "target"),
    [
        (WriteOperation.APPEND, ("bronze", "events")),
        (WriteOperation.MERGE, ("bronze", "events")),
        (WriteOperation.WRITE, ("bronze", "events")),
        (WriteOperation.DROP_TABLE, ("bronze", "events")),
        (WriteOperation.RENAME_TABLE, ("bronze", "events")),
        (WriteOperation.CREATE_VIEW, ("bronze", "events")),
        (WriteOperation.COMMIT, ("bronze", "other")),
        (WriteOperation.COMMIT, ("bronze",)),
        (WriteOperation.CREATE_NAMESPACE, ("bronze",)),
    ],
    ids=lambda v: v.value if isinstance(v, WriteOperation) else ".".join(v),
)
def test_a_claim_that_sets_the_table_back_may_do_nothing_else_under_a_cancel_request(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    operation: WriteOperation,
    target: tuple[str, ...],
) -> None:
    """Any other operation, and a commit of another table, keeps today's refusal."""
    p = _setting_back_context(session, principal)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", operation, target, rollback=True)
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")


@pytest.mark.parametrize("case", ["stale-claim", "run-not-running", "no-flag", "task-not-running"])
def test_the_set_back_exception_needs_the_rows_standing_not_the_contexts(
    session: Session, catalog: CatalogService, principal: PrincipalRef, case: str
) -> None:
    """The row decides: a moved claim counter, an ended run or task, or no flag is refused."""
    p = _setting_back_context(session, principal, setting_back=case != "no-flag")
    assert p.run_context is not None
    task = session.get(TaskRun, p.run_context.task_run_id)
    assert task is not None
    if case == "stale-claim":
        task.claim_count = 2
    elif case == "run-not-running":
        run = session.get(Run, task.run_id)
        assert run is not None
        run.status = RunStatus.CANCELLED
    elif case == "task-not-running":
        task.status = TaskStatus.READY
    session.commit()
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.COMMIT, ("bronze", "events"), rollback=True)
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")


def test_the_set_back_exception_is_checked_again_under_lock(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A claim that loses its standing between the check and the commit is refused there."""
    p = _setting_back_context(session, principal)
    assert p.run_context is not None
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.COMMIT, ("bronze", "events"), rollback=True)
    session.execute(
        update(TaskRun).where(TaskRun.id == p.run_context.task_run_id).values(claim_count=2)
    )
    with pytest.raises(PermissionDenied) as info:
        guard.record_pending()
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")


def test_only_the_internal_rollback_commit_passes_a_cancel_request_through_the_catalog(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A plain `commit_table` (what REST and the SDK reach) is refused; the rollback passes."""
    p = _setting_back_context(session, principal)
    with pytest.raises(PermissionDenied) as info:
        catalog.commit_table(p, _EVENTS, requirements=[], updates=[])
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")
    session.rollback()
    catalog.commit_table(p, _EVENTS, requirements=[], updates=[], internal_rollback=True)


def test_the_rollback_fact_survives_into_the_recheck_under_lock(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """`record_pending` judges the same fact as `check`: without it the recheck would refuse."""
    p = _setting_back_context(session, principal)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.COMMIT, ("bronze", "events"), rollback=True)
    guard.record_pending()
    assert not _events(session, AuditEventType.PROTECTED_WRITE_DENIED)


def test_the_set_back_exception_names_the_own_table_in_an_unprotected_environment(
    session: Session, open_catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Another table is refused with today's text, not passed on to the other-asset rule."""
    p = _setting_back_context(session, principal)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.COMMIT, ("bronze", "other"), rollback=True)
    assert str(info.value) == SUPERSEDED_MESSAGE


def test_the_set_back_exception_is_for_the_claim_form_only(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A query-run-form context of a task row with the flag gets no exception."""
    context = _task(session, principal, "bronze.events", cancel_requested=True)
    task = session.get(TaskRun, context.task_run_id)
    assert task is not None
    task.setting_back = True
    session.commit()
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(
            _in_run(principal, context),
            "dev",
            WriteOperation.COMMIT,
            ("bronze", "events"),
            rollback=True,
        )
    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")


def test_a_context_with_both_forms_covers_nothing(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A claim that matches must not be rescued by a stray `query_run_id`."""
    task = python_task_run(session, principal.id, asset="bronze.events")
    context = RunContext(task.id, query_run_id=uuid.uuid4(), claim=task.claim_count)
    with pytest.raises(PermissionDenied):
        catalog.append(_in_run(principal, context), _EVENTS, _ROWS)


def test_a_context_with_neither_form_covers_nothing(
    session: Session, open_catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A Python task has no `query_run_id`, so `RunContext(task_id)` would match it.

    Without the shape check it would pass as the M5c1 form and, in an
    unprotected environment, write any table.
    """
    task = python_task_run(session, principal.id, asset="bronze.events")
    with pytest.raises(PermissionDenied):
        open_catalog.append(_in_run(principal, RunContext(task.id)), _SRC, _ROWS)


def test_a_claim_context_of_a_run_in_another_environment_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    CatalogService(session, StorageResolver(session), AllowAllAuthorization()).create_environment(
        principal,
        name="staging",
        storage=_config(tmp_path / "staging"),
    )
    task = python_task_run(session, principal.id, asset="bronze.events", environment="staging")
    context = RunContext(task_run_id=task.id, claim=task.claim_count)
    with pytest.raises(PermissionDenied):
        catalog.append(_in_run(principal, context), _EVENTS, _ROWS)


def test_a_reclaim_between_check_and_commit_refuses_a_bound_write(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    p = _bound(session, principal, "bronze.events")
    assert p.run_context is not None
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.WRITE, ("bronze", "events"))
    session.execute(
        update(TaskRun)
        .where(TaskRun.id == p.run_context.task_run_id)
        .values(claim_count=TaskRun.claim_count + 1)
    )
    with pytest.raises(PermissionDenied):
        guard.record_pending()


def test_a_bound_task_is_held_to_its_own_asset_in_an_unprotected_environment(
    session: Session, open_catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Unlike the M5c1 form, a claim-form context is asset-scoped everywhere."""
    p = _bound(session, principal, "bronze.events")
    assert open_catalog.append(p, _EVENTS, _ROWS).rows == 1
    with pytest.raises(PermissionDenied) as info:
        open_catalog.append(p, _SRC, _ROWS)
    assert str(info.value) == OTHER_ASSET_MESSAGE.format(
        asset="bronze.events", operation="append", target="bronze.src"
    )
    assert _audit_detail(session) == {
        "operation": "append",
        "target": "bronze.src",
        "in_run": True,
        "protected": False,
        "reason": "other_asset",
    }


def test_a_stale_attempt_is_still_called_superseded_and_audited_without_a_reason(
    session: Session, open_catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The two refusals are told apart: a reclaimed attempt writing its own asset."""
    p = _bound(session, principal, "bronze.events")
    assert p.run_context is not None
    session.execute(
        update(TaskRun)
        .where(TaskRun.id == p.run_context.task_run_id)
        .values(claim_count=TaskRun.claim_count + 1)
    )
    session.commit()
    with pytest.raises(PermissionDenied) as info:
        open_catalog.append(p, _EVENTS, _ROWS)
    assert str(info.value) == SUPERSEDED_MESSAGE
    assert "reason" not in _audit_detail(session)


def test_the_name_in_the_other_asset_refusal_is_cut_and_escaped(
    session: Session, open_catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A long target or one with an escape never reaches the 403 raw."""
    p = _bound(session, principal, "bronze.events")
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.WRITE, ("bronze", "a\x1b[31m" + "t" * 5000))
    assert "\x1b" not in str(info.value)
    assert len(str(info.value)) < 500


def test_a_bound_principal_cannot_break_glass(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    p = dataclasses.replace(_bound(session, principal, "bronze.other"), break_glass_reason=_REASON)
    with pytest.raises(PermissionDenied):
        catalog.append(p, _EVENTS, _ROWS)
    assert _events(session, AuditEventType.BREAK_GLASS_WRITE) == []


def _set_protected(session: Session, protected: bool) -> None:
    """Create the environment `dev` with a table in it and set whether it is protected.

    Parameters
    ----------
    session
        Database session.
    protected
        Whether `dev` is protected.
    """
    env_id, _ = ensure_table(session, "dev", "silver", "rows")
    session.execute(
        update(Environment).where(Environment.id == env_id).values(is_protected=protected)
    )
    session.commit()


def _artifact_task(
    session: Session,
    principal: PrincipalRef,
    *,
    protected: bool,
    extra: dict[str, object] | None = None,
    kind: str = "python",
    asset: str = "models.churn",
) -> PrincipalRef:
    """Build a principal running as the claim-form task of one asset.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    protected
        Whether the environment `dev` is protected.
    extra
        Keys merged into the asset's manifest entry; ``{"output": "artifact"}`` by default.
    kind
        The manifest entry's kind.
    asset
        The task's asset.

    Returns
    -------
    PrincipalRef
        The principal with the run context of that task.
    """
    _set_protected(session, protected)
    task = python_task_run(
        session,
        principal.id,
        asset=asset,
        inputs={"rows": "silver.rows"},
        kind=kind,
        extra={"output": "artifact"} if extra is None else extra,
    )
    return dataclasses.replace(principal, run_context=RunContext(task_run_id=task.id, claim=1))


def test_a_task_may_add_a_version_only_to_its_own_artifact(
    session: Session, principal: PrincipalRef
) -> None:
    """Adding a version is a materialising operation; deleting and dropping are not."""
    p = _artifact_task(session, principal, protected=False)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    guard.record_pending()
    for operation, target, wording in (
        (WriteOperation.ARTIFACT_VERSION, ("models", "other"), "a task only writes its own asset"),
        (WriteOperation.DELETE_ARTIFACT_VERSION, ("models", "churn"), "to its own asset"),
        (WriteOperation.DROP_ARTIFACT, ("models", "churn"), "to its own asset"),
    ):
        with pytest.raises(PermissionDenied, match=wording):
            guard.check(p, "dev", operation, target)


def test_in_a_protected_environment_a_task_adds_only_to_its_own_artifact(
    session: Session, principal: PrincipalRef
) -> None:
    p = _artifact_task(session, principal, protected=True)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    guard.record_pending()
    for operation, target in (
        (WriteOperation.ARTIFACT_VERSION, ("models", "other")),
        (WriteOperation.ARTIFACT_VERSION, ("models",)),
        (WriteOperation.DELETE_ARTIFACT_VERSION, ("models", "churn")),
        (WriteOperation.DROP_ARTIFACT, ("models", "churn")),
    ):
        with pytest.raises(PermissionDenied, match="is protected"):
            guard.check(p, "dev", operation, target)


def test_only_adding_a_version_is_something_a_task_may_do() -> None:
    """A member left out of `_MATERIALIZING` is refused to a task, never let through."""
    from lakana_core.runs.lockdown import _MATERIALIZING

    assert WriteOperation.ARTIFACT_VERSION in _MATERIALIZING
    assert WriteOperation.DELETE_ARTIFACT_VERSION not in _MATERIALIZING
    assert WriteOperation.DROP_ARTIFACT not in _MATERIALIZING


_ALLOWED = {
    WriteOperation.SQL_WRITE,
    WriteOperation.WRITE,
    WriteOperation.APPEND,
    WriteOperation.MERGE,
    WriteOperation.HISTORY,
    WriteOperation.COMMIT,
    WriteOperation.CREATE_VIEW,
    WriteOperation.ARTIFACT_VERSION,
}
"""A literal, on purpose: what a task may do to its own asset, independent of the code."""


def test_the_operations_a_task_may_do_are_exactly_these() -> None:
    from lakana_core.runs.lockdown import _MATERIALIZING

    assert set(_MATERIALIZING) == _ALLOWED


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
@pytest.mark.parametrize("operation", list(WriteOperation), ids=lambda o: o.value)
def test_a_claim_form_task_may_do_only_what_the_literal_list_says(
    session: Session, principal: PrincipalRef, operation: WriteOperation, protected: bool
) -> None:
    """No `WriteOperation` reaches a task unless it is in `_ALLOWED` (a literal, not the code)."""
    # A table asset for the table operations, an artifact asset for the artifact one.
    artifact = operation is WriteOperation.ARTIFACT_VERSION
    p = _artifact_task(
        session, principal, protected=protected, extra={"output": "artifact"} if artifact else {}
    )
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    if operation in _ALLOWED:
        guard.check(p, "dev", operation, ("models", "churn"))
        return
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", operation, ("models", "churn"))
    assert str(info.value) == (
        PROTECTED_MESSAGE.format(environment="dev")
        if protected
        else _own_asset_text(
            operation.value.replace("_", " "),
            "a namespace with its own asset's name"
            if operation
            in {
                WriteOperation.CREATE_NAMESPACE,
                WriteOperation.DROP_NAMESPACE,
                WriteOperation.UPDATE_NAMESPACE,
            }
            else "its own asset",
        )
    )


def _own_asset_text(operation: str, subject: str) -> str:
    """Spell the own-asset refusal out as a literal, independent of the code.

    Parameters
    ----------
    operation
        The refused operation as the message names it.
    subject
        What the operation was aimed at.

    Returns
    -------
    str
        The whole message for task `models.churn`.
    """
    return (
        f"Task 'models.churn' may not do '{operation}' to {subject}; "
        "a task may only materialize its own asset: for a table, sql write, write, append, merge, "
        "history, commit, create view; for an artifact, artifact version. Do this outside the task."
    )


def test_the_own_asset_refusal_is_true_and_audited_with_its_own_reason(
    session: Session, principal: PrincipalRef
) -> None:
    """A refused operation on the task's own asset is not called 'another asset'."""
    p = _artifact_task(session, principal, protected=False)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.DROP_ARTIFACT, ("models", "churn"))
    assert str(info.value) == _own_asset_text("drop artifact", "its own asset")
    assert _audit_detail(session)["reason"] == "own_asset_operation"


def test_the_other_asset_refusal_keeps_its_wording_and_reason(
    session: Session, principal: PrincipalRef
) -> None:
    """Another asset as the target still gets the original text and `other_asset`."""
    p = _artifact_task(session, principal, protected=False)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.DROP_ARTIFACT, ("models", "other"))
    assert str(info.value) == (
        "Task 'models.churn' may not touch 'models.other' (drop artifact): "
        "a task only writes its own asset. Do this outside the task."
    )
    assert _audit_detail(session)["reason"] == "other_asset"


def test_creating_a_namespace_named_like_the_own_asset_is_refused_as_an_own_asset_one(
    session: Session, principal: PrincipalRef
) -> None:
    """Only a strict ancestor is covered, so the asset's own path is not."""
    p = _artifact_task(session, principal, protected=False)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.CREATE_NAMESPACE, ("models", "churn"))
    assert str(info.value) == _own_asset_text(
        "create namespace", "a namespace with its own asset's name"
    )
    assert _audit_detail(session)["reason"] == "own_asset_operation"


_TABLE_REFUSAL = (
    "Task 'models.churn' produces a table; it cannot add an artifact version. "
    'To store an artifact, declare the asset with output="artifact".'
)
_ARTIFACT_REFUSAL = (
    "Task 'models.churn' produces an artifact; it cannot write a table. "
    "Write the artifact's files into lakana.output_dir() instead."
)
_WORKER_REFUSAL = (
    "Task 'models.churn' adds artifact versions through its task token, not from the worker."
)
_TABLE_OPERATIONS = (
    WriteOperation.SQL_WRITE,
    WriteOperation.WRITE,
    WriteOperation.APPEND,
    WriteOperation.MERGE,
    WriteOperation.COMMIT,
    WriteOperation.CREATE_VIEW,
)


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
@pytest.mark.parametrize(
    "extra", [{}, {"output": "table"}, {"output": "ARTIFACT"}], ids=["none", "table", "casing"]
)
def test_a_table_asset_task_may_not_add_an_artifact_version(
    session: Session, principal: PrincipalRef, protected: bool, extra: dict[str, object]
) -> None:
    p = _artifact_task(session, principal, protected=protected, extra=extra)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    assert str(info.value) == _TABLE_REFUSAL


def _query_run_task(
    session: Session, principal: PrincipalRef, *, protected: bool, extra: dict[str, object]
) -> PrincipalRef:
    """Build a principal as the worker builds it for a task: the query-run form.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.
    protected
        Whether `dev` is protected.
    extra
        Keys merged into the manifest entry of `models.churn`.

    Returns
    -------
    PrincipalRef
        The principal with a run context naming a running query run of the task.
    """
    _set_protected(session, protected)
    task = python_task_run(
        session, principal.id, asset="models.churn", inputs={"rows": "silver.rows"}, extra=extra
    )
    query = QueryRun(
        id=uuid.uuid4(),
        environment_id=session.scalars(select(Environment).where(Environment.name == "dev"))
        .one()
        .id,
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.RUNNING,
        task_run_id=task.id,
    )
    session.add(query)
    session.flush()
    task.query_run_id = query.id
    session.commit()
    return _in_run(principal, RunContext(task_run_id=task.id, query_run_id=query.id))


def _audit_detail(session: Session) -> dict[str, object]:
    """Return the detail of the one `PROTECTED_WRITE_DENIED` row.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    dict
        The detail.
    """
    session.rollback()
    (event,) = _events(session, AuditEventType.PROTECTED_WRITE_DENIED)
    return dict(event.detail)


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
@pytest.mark.parametrize(
    ("extra", "text"),
    [({}, _TABLE_REFUSAL), ({"output": "artifact"}, _WORKER_REFUSAL)],
    ids=["table-entry", "artifact-entry"],
)
def test_a_query_run_context_never_adds_an_artifact_version_on_its_own_asset(
    session: Session, principal: PrincipalRef, protected: bool, extra: dict[str, object], text: str
) -> None:
    p = _query_run_task(session, principal, protected=protected, extra=extra)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    assert str(info.value) == text
    assert _audit_detail(session) == {
        "operation": "artifact_version",
        "target": "models.churn",
        "in_run": True,
        "protected": protected,
        "reason": "output_kind",
    }


def test_a_query_run_context_on_another_name_is_left_to_grants_when_unprotected(
    session: Session, principal: PrincipalRef
) -> None:
    """The remaining case (unchanged): the guard's scope for a worker context is grants alone."""
    p = _query_run_task(session, principal, protected=False, extra={"output": "artifact"})
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "other"))


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
@pytest.mark.parametrize("operation", _TABLE_OPERATIONS, ids=lambda o: o.value)
def test_an_artifact_asset_query_run_context_may_not_write_a_table(
    session: Session, principal: PrincipalRef, protected: bool, operation: WriteOperation
) -> None:
    p = _query_run_task(session, principal, protected=protected, extra={"output": "artifact"})
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", operation, ("models", "churn"))
    assert str(info.value) == _ARTIFACT_REFUSAL


def test_a_table_refusal_is_audited_with_its_reason(
    session: Session, principal: PrincipalRef
) -> None:
    p = _artifact_task(session, principal, protected=True, extra={})
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied):
        guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    assert _audit_detail(session) == {
        "operation": "artifact_version",
        "target": "models.churn",
        "in_run": True,
        "protected": True,
        "reason": "output_kind",
    }


def test_an_artifact_refusal_is_audited_with_its_reason(
    session: Session, principal: PrincipalRef
) -> None:
    p = _artifact_task(session, principal, protected=False)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied):
        guard.check(p, "dev", WriteOperation.APPEND, ("models", "churn"))
    assert _audit_detail(session) == {
        "operation": "append",
        "target": "models.churn",
        "in_run": True,
        "protected": False,
        "reason": "output_kind",
    }


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
def test_a_sql_entry_that_says_artifact_is_a_table_at_the_guard(
    session: Session, principal: PrincipalRef, protected: bool
) -> None:
    """The guard does not look at the entry's kind to build a context, so a claim form is used."""
    p = _artifact_task(session, principal, protected=protected, kind="sql")
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.APPEND, ("models", "churn"))
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    assert str(info.value) == _TABLE_REFUSAL


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
def test_a_later_python_entry_after_a_sql_one_of_the_same_name_is_not_read(
    session: Session, principal: PrincipalRef, protected: bool
) -> None:
    """Claim-form context (the guard never checks the kind to build one), SQL entry first."""
    p = _artifact_task(session, principal, protected=protected)
    assert p.run_context is not None
    task = session.get(TaskRun, p.run_context.task_run_id)
    assert task is not None
    run = session.get(Run, task.run_id)
    assert run is not None
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    version.manifest_json = {
        "assets": [
            {"table": "models.churn", "kind": "sql"},
            {"table": "models.churn", "kind": "python", "output": "artifact"},
        ]
    }
    session.commit()
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.APPEND, ("models", "churn"))
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    assert str(info.value) == _TABLE_REFUSAL


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
@pytest.mark.parametrize("operation", _TABLE_OPERATIONS, ids=lambda o: o.value)
def test_an_artifact_asset_task_may_not_write_a_table(
    session: Session, principal: PrincipalRef, protected: bool, operation: WriteOperation
) -> None:
    p = _artifact_task(session, principal, protected=protected)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", operation, ("models", "churn"))
    assert str(info.value) == _ARTIFACT_REFUSAL


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
def test_the_recheck_under_lock_applies_the_same_rule(
    session: Session, principal: PrincipalRef, protected: bool
) -> None:
    """`record_pending` runs the output-kind rule again under lock, not only `check`.

    The manifest of a project version never changes; the test swaps it between the two calls only to
    show that the second call reads it itself.
    """
    p = _artifact_task(session, principal, protected=protected)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    assert p.run_context is not None
    task = session.get(TaskRun, p.run_context.task_run_id)
    assert task is not None
    run = session.get(Run, task.run_id)
    assert run is not None
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    version.manifest_json = {"assets": [{"table": "models.churn", "kind": "python"}]}
    session.flush()
    session.expire_all()
    with pytest.raises(PermissionDenied) as info:
        guard.record_pending()
    assert str(info.value) == _TABLE_REFUSAL


@pytest.mark.parametrize("protected", [True, False], ids=["protected", "unprotected"])
def test_the_first_entry_of_a_name_decides_the_output_kind(
    session: Session, principal: PrincipalRef, protected: bool
) -> None:
    p = _artifact_task(session, principal, protected=protected)
    assert p.run_context is not None
    task = session.get(TaskRun, p.run_context.task_run_id)
    assert task is not None
    run = session.get(Run, task.run_id)
    assert run is not None
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    first = {"table": "models.churn", "kind": "python", "output": "artifact", "inputs": {}}
    second = {"table": "models.churn", "kind": "python", "inputs": {}}
    version.manifest_json = {"assets": [first, second]}
    session.commit()
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    version.manifest_json = {"assets": [second, first]}
    session.commit()
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    assert str(info.value) == _TABLE_REFUSAL


@pytest.mark.parametrize("manifest", [{}, {"assets": None}, {"assets": "x"}, {"assets": [3, None]}])
def test_a_missing_or_malformed_manifest_counts_as_a_table(
    session: Session, principal: PrincipalRef, manifest: dict[str, object]
) -> None:
    p = _artifact_task(session, principal, protected=True)
    assert p.run_context is not None
    task = session.get(TaskRun, p.run_context.task_run_id)
    assert task is not None
    run = session.get(Run, task.run_id)
    assert run is not None
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    version.manifest_json = manifest
    session.commit()
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.APPEND, ("models", "churn"))
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    assert str(info.value) == _TABLE_REFUSAL


def test_a_run_without_a_job_version_counts_as_a_table(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _artifact_task(session, principal, protected=True)
    real = session.get
    monkeypatch.setattr(
        session,
        "get",
        lambda entity, ident, **kw: None if entity is JobVersion else real(entity, ident, **kw),
    )
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with pytest.raises(PermissionDenied) as info:
        guard.check(p, "dev", WriteOperation.ARTIFACT_VERSION, ("models", "churn"))
    assert str(info.value) == _TABLE_REFUSAL


def test_the_new_operations_have_stable_audit_names() -> None:
    assert WriteOperation.ARTIFACT_VERSION.value == "artifact_version"
    assert WriteOperation.DELETE_ARTIFACT_VERSION.value == "delete_artifact_version"
    assert WriteOperation.DROP_ARTIFACT.value == "drop_artifact"


_NEW_OPERATIONS = (
    WriteOperation.ARTIFACT_VERSION,
    WriteOperation.DELETE_ARTIFACT_VERSION,
    WriteOperation.DROP_ARTIFACT,
)


def test_a_protected_environment_refuses_a_version_outside_a_run(
    session: Session, principal: PrincipalRef
) -> None:
    env_id, _ = ensure_table(session, "dev", "silver", "rows")
    session.execute(update(Environment).where(Environment.id == env_id).values(is_protected=True))
    session.commit()
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    for operation in _NEW_OPERATIONS:
        with pytest.raises(PermissionDenied, match="is protected"):
            guard.check(principal, "dev", operation, ("models", "churn"))


def test_break_glass_covers_the_new_operations_and_needs_manage_environment(
    session: Session, principal: PrincipalRef
) -> None:
    env_id, _ = ensure_table(session, "dev", "silver", "rows")
    session.execute(update(Environment).where(Environment.id == env_id).values(is_protected=True))
    session.commit()
    glass = dataclasses.replace(principal, break_glass_reason=_REASON)
    for operation in _NEW_OPERATIONS:
        guard = ProtectedWriteGuard(session, AllowAllAuthorization())
        guard.check(glass, "dev", operation, ("models", "churn"))
        guard.record_pending()
        (event,) = [
            e
            for e in _events(session, AuditEventType.BREAK_GLASS_WRITE)
            if e.detail["operation"] == operation.value
        ]
        assert event.detail["target"] == "models.churn"
        denied = ProtectedWriteGuard(session, ActionAllowList())
        with pytest.raises(PermissionDenied, match="may not MANAGE_ENVIRONMENT"):
            denied.check(glass, "dev", operation, ("models", "churn"))


def test_a_deletions_break_glass_record_does_not_hide_the_table_write_after_it(
    session: Session, principal: PrincipalRef
) -> None:
    """One guard: the deletion is recorded, then the first table write, then no more."""
    _set_protected(session, True)
    glass = dataclasses.replace(principal, break_glass_reason=_REASON)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    target = ("models", "churn")
    for operation in (
        WriteOperation.DELETE_ARTIFACT_VERSION,
        WriteOperation.APPEND,
        WriteOperation.COMMIT,
        WriteOperation.APPEND,
    ):
        guard.check(glass, "dev", operation, target)
        guard.record_pending()
    assert [e.detail["operation"] for e in _events(session, AuditEventType.BREAK_GLASS_WRITE)] == [
        "delete_artifact_version",
        "append",
    ]


# --- The write fence -------------------------------------------------------


def _unprotected(session: Session) -> None:
    """Make `dev` unprotected, as it is for the ad-hoc writes the fence guards.

    Parameters
    ----------
    session
        Database session.
    """
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    env.is_protected = False
    session.commit()


def _running(session: Session, principal: PrincipalRef, worker: str | None = "w1") -> QueryRun:
    """Insert a RUNNING write run of `principal` in `dev`, claimed by `worker`.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    worker
        The worker id on the run.

    Returns
    -------
    QueryRun
        The committed run.
    """
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    run = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=principal.id,
        sql_text="SELECT 7 AS id",
        status=QueryRunStatus.RUNNING,
        write_mode=WriteMode.APPEND,
        target_name="bronze.events",
        worker_id=worker,
    )
    session.add(run)
    session.commit()
    return run


def _fenced(principal: PrincipalRef, run: QueryRun, worker: str | None = "w1") -> PrincipalRef:
    """Return `principal` carrying the write fence of `run` as `worker` holds it.

    Parameters
    ----------
    principal
        The submitter.
    run
        The run.
    worker
        The worker id the fence names.

    Returns
    -------
    PrincipalRef
        With a write fence.
    """
    return dataclasses.replace(principal, write_fence=WriteFence(run.id, worker))


_ENDINGS: dict[str, dict[str, object]] = {
    "failed by the sweep": {"status": QueryRunStatus.FAILED},
    "cancelled": {"status": QueryRunStatus.CANCELLED},
    "already succeeded": {"status": QueryRunStatus.SUCCEEDED},
    "another worker's": {"worker_id": "w2"},
    "nobody's": {"worker_id": None},
}
"""How a run can stop being this worker's running run, as a set of column values."""


@contextlib.contextmanager
def _statements(session: Session) -> Generator[list[str]]:
    """Record the SQL the session's engine runs while the block executes.

    Parameters
    ----------
    session
        Database session.

    Yields
    ------
    list[str]
        The statements, in order.
    """
    seen: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(bind, "before_cursor_execute", record)


@pytest.mark.parametrize("ending", sorted(_ENDINGS))
def test_a_fenced_write_is_refused_unless_the_run_is_this_workers_running_run(
    session: Session, catalog: CatalogService, principal: PrincipalRef, ending: str
) -> None:
    _unprotected(session)
    run = _running(session, principal)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(_fenced(principal, run), "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.note_result("dev", ("bronze", "events"), rows=1, snapshot_id=5, table_id=uuid.uuid4())
    session.execute(update(QueryRun).where(QueryRun.id == run.id).values(**_ENDINGS[ending]))
    session.commit()  # whoever ended the run committed first
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    env.is_protected = True  # stands for the half-built write: uncommitted work of this transaction
    with pytest.raises(PermissionDenied) as caught:
        guard.record_pending()
    assert str(caught.value) == WRITE_FENCE_MESSAGE
    session.refresh(env)
    assert env.is_protected is False  # the half-built write was rolled back
    session.refresh(run)
    assert run.rows_written is None  # no evidence for a write that did not land
    guard.record_pending()  # the fence is spent once it has refused


def test_a_committed_write_leaves_its_evidence_on_the_run(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _unprotected(session)
    run = _running(session, principal)
    table_id = uuid.uuid4()
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(_fenced(principal, run), "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.note_result("dev", ("bronze", "events"), rows=0, snapshot_id=None, table_id=table_id)
    guard.check(_fenced(principal, run), "dev", WriteOperation.COMMIT, ("bronze", "events"))
    guard.record_pending()
    session.commit()
    session.refresh(run)
    assert (run.rows_written, run.snapshot_id, run.target_table_id) == (0, None, table_id)
    assert run.status is QueryRunStatus.RUNNING  # the worker stamps the status, not the guard


def test_a_fenced_write_of_a_deleted_run_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _unprotected(session)
    run = _running(session, principal)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(_fenced(principal, run), "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.note_result("dev", ("bronze", "events"), rows=1, snapshot_id=5, table_id=uuid.uuid4())
    session.delete(run)
    session.commit()
    with pytest.raises(PermissionDenied) as caught:
        guard.record_pending()
    assert str(caught.value) == WRITE_FENCE_MESSAGE


def test_a_run_claimed_without_a_worker_id_passes_its_own_fence(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _unprotected(session)
    run = _running(session, principal, worker=None)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    p = _fenced(principal, run, worker=None)
    guard.check(p, "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.note_result("dev", ("bronze", "events"), rows=3, snapshot_id=9, table_id=uuid.uuid4())
    guard.record_pending()
    session.commit()
    session.refresh(run)
    assert (run.rows_written, run.snapshot_id) == (3, 9)


def test_a_fence_without_a_noted_result_still_checks_the_run(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _unprotected(session)
    ended = _running(session, principal)
    live = _running(session, principal)
    session.execute(
        update(QueryRun).where(QueryRun.id == ended.id).values(status=QueryRunStatus.FAILED)
    )
    session.commit()
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(_fenced(principal, ended), "dev", WriteOperation.WRITE, ("bronze", "events"))
    with pytest.raises(PermissionDenied) as caught:
        guard.record_pending()
    assert str(caught.value) == WRITE_FENCE_MESSAGE
    guard.check(_fenced(principal, live), "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.record_pending()
    session.commit()
    session.refresh(live)
    assert live.rows_written is None


def test_a_result_noted_for_another_target_is_dropped_by_the_next_check(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _unprotected(session)
    run = _running(session, principal)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    p = _fenced(principal, run)
    guard.check(p, "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.note_result("dev", ("bronze", "events"), rows=1, snapshot_id=5, table_id=uuid.uuid4())
    guard.check(p, "dev", WriteOperation.WRITE, ("bronze", "src"))
    guard.record_pending()
    session.commit()
    session.refresh(run)
    assert run.rows_written is None


def test_a_principal_without_a_fence_is_untouched(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The REST path and a task's own table write carry no fence: no statement on the runs."""
    _unprotected(session)
    _running(session, principal)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    with _statements(session) as statements:
        guard.check(principal, "dev", WriteOperation.WRITE, ("bronze", "events"))
        guard.record_pending()
    assert not [s for s in statements if "query_runs" in s]


def test_a_fence_is_spent_by_one_write_and_not_inherited_by_the_next(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A later write by an unfenced principal on the same guard checks nothing."""
    _unprotected(session)
    run = _running(session, principal)
    session.execute(update(QueryRun).where(QueryRun.id == run.id).values(worker_id="w2"))
    session.commit()
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(_fenced(principal, run), "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.check(principal, "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.record_pending()  # the fence of the first check was replaced by "none"


def _index(statements: list[str], pattern: str) -> int:
    """Return the position of the first statement that matches `pattern`.

    Parameters
    ----------
    statements
        The recorded statements.
    pattern
        A regular expression.

    Returns
    -------
    int
        The index, or `len(statements)` when none matches.
    """
    for position, statement in enumerate(statements):
        if re.search(pattern, statement):
            return position
    return len(statements)


def test_the_run_context_is_verified_before_the_fence(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Lock order of `store._apply_task`: task run, run, then query run."""
    context = _task(session, principal, "bronze.events")
    session.execute(
        update(QueryRun).where(QueryRun.id == context.query_run_id).values(worker_id="w1")
    )
    session.commit()
    p = dataclasses.replace(
        _in_run(principal, context),
        write_fence=WriteFence(context.query_run_id or uuid.uuid4(), "w1"),
    )
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.note_result("dev", ("bronze", "events"), rows=1, snapshot_id=2, table_id=uuid.uuid4())
    with _statements(session) as statements:
        guard.record_pending()
    task_runs = _index(statements, r"FROM task_runs")
    runs = _index(statements, r"FROM runs\b")
    fence = _index(statements, r"UPDATE query_runs")
    assert task_runs < runs < fence < len(statements)


def test_a_reclaimed_task_is_refused_by_its_run_context_before_the_fence_is_asked(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The run context's refusal is the one reported, and the query run is never touched."""
    _unprotected(session)
    context = _task(session, principal, "bronze.events")
    session.execute(
        update(QueryRun).where(QueryRun.id == context.query_run_id).values(worker_id="w1")
    )
    session.commit()
    p = dataclasses.replace(
        _in_run(principal, context),
        write_fence=WriteFence(context.query_run_id or uuid.uuid4(), "w1"),
    )
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.note_result("dev", ("bronze", "events"), rows=1, snapshot_id=2, table_id=uuid.uuid4())
    _reclaim(session, context)
    with _statements(session) as statements, pytest.raises(PermissionDenied) as caught:
        guard.record_pending()
    assert str(caught.value) == SUPERSEDED_MESSAGE
    assert not [s for s in statements if "UPDATE query_runs" in s]


@pytest.mark.parametrize("ended", [False, True], ids=["live", "ended"])
def test_a_tasks_sql_write_must_pass_both_its_claim_and_its_fence(
    session: Session, catalog: CatalogService, principal: PrincipalRef, ended: bool
) -> None:
    """A task's SQL write has a run context and a fence: either alone refuses it."""
    context = _task(session, principal, "bronze.events")
    run_id = context.query_run_id or uuid.uuid4()
    session.execute(
        update(QueryRun)
        .where(QueryRun.id == run_id)
        .values(worker_id="w1", status=QueryRunStatus.FAILED if ended else QueryRunStatus.RUNNING)
    )
    session.commit()
    p = dataclasses.replace(_in_run(principal, context), write_fence=WriteFence(run_id, "w1"))
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.note_result("dev", ("bronze", "events"), rows=1, snapshot_id=2, table_id=uuid.uuid4())
    if ended:
        # The run context's own check does not know the query run's status; the fence does.
        with pytest.raises(PermissionDenied) as caught:
            guard.record_pending()
        assert str(caught.value) == WRITE_FENCE_MESSAGE
    else:
        guard.record_pending()
        session.commit()
        stored = session.get(QueryRun, run_id, populate_existing=True)
        assert stored is not None and stored.rows_written == 1


class _World(NamedTuple):
    """A catalog on a SQLite file, with a second session that sees only what is committed."""

    session: Session
    observer: Session
    catalog: CatalogService
    principal: PrincipalRef


@pytest.fixture
def world(tmp_path: Path) -> Iterator[_World]:
    """Build an unprotected `dev` with `bronze.events` and `bronze.src` on a SQLite file.

    Parameters
    ----------
    tmp_path
        Holds the database file and the environment's storage.

    Yields
    ------
    _World
        The writer's session, a second connection to observe through, the catalog, a principal.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'fence.db'}"))
    Base.metadata.create_all(engine)
    with Session(engine) as writer, Session(engine) as observer:
        ref = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
        writer.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
        writer.commit()
        built = _prepared_catalog(
            writer,
            tmp_path / "storage",
            AllowAllAuthorization(),
            namespaces=[_BRONZE],
            tables=[_EVENTS, _SRC],
        )
        _unprotected(writer)
        yield _World(writer, observer, built, ref)
    engine.dispose()


def _seen(world: _World) -> Session:
    """Return the observer, starting a fresh read.

    Parameters
    ----------
    world
        The world.

    Returns
    -------
    Session
        The second connection, with nothing cached.
    """
    world.observer.rollback()
    world.observer.expire_all()
    return world.observer


def _location(world: _World, ref: TableRef) -> str | None:
    """Read a table's committed metadata location on the second connection.

    Parameters
    ----------
    world
        The world.
    ref
        The table.

    Returns
    -------
    str or None
        The location, or `None` when the table does not exist or has none.
    """
    return _seen(world).scalar(select(Table.metadata_location).where(Table.name == ref.name))


def _run_row(world: _World, run: QueryRun) -> QueryRun:
    """Read a run on the second connection.

    Parameters
    ----------
    world
        The world.
    run
        The run to read.

    Returns
    -------
    QueryRun
        The committed state.
    """
    return _seen(world).scalars(select(QueryRun).where(QueryRun.id == run.id)).one()


_FRESH = TableRef("dev", _BRONZE, "fresh")
_VIEW = TableRef("dev", _BRONZE, "fenced_view")
_MODES = ["create", "replace", "append", "empty append", "view"]


def _write(world: _World, mode: str, p: PrincipalRef, run: QueryRun) -> tuple[TableRef, int]:
    """Perform one ad-hoc write of the given kind as `p`.

    Parameters
    ----------
    world
        The world.
    mode
        One of `_MODES`.
    p
        The (fenced) principal.
    run
        The run the write belongs to.

    Returns
    -------
    tuple of (TableRef, int)
        The target and the rows the call reports.
    """
    if mode == "view":
        world.catalog.create_or_replace_view(
            p, _VIEW, sql="SELECT 1", references=[], columns=[], query_run_id=run.id
        )
        return _VIEW, 0
    write_mode = {
        "create": WriteMode.CREATE,
        "replace": WriteMode.REPLACE,
        "append": WriteMode.APPEND,
        "empty append": WriteMode.APPEND,
    }[mode]
    ref = _FRESH if mode == "create" else _EVENTS
    batches = [] if mode == "empty append" else _ROWS.to_batches()
    result = world.catalog.write_batches(
        p,
        ref,
        mode=write_mode,
        schema=_ROWS.schema,
        batches=batches,
        query_run_id=run.id,
    )
    return ref, result.rows


@pytest.mark.parametrize("mode", _MODES)
def test_every_write_mode_of_a_fenced_run_is_refused_once_the_run_has_ended(
    world: _World, mode: str
) -> None:
    run = _running(world.session, world.principal)
    before = {ref: _location(world, ref) for ref in (_EVENTS, _FRESH, _VIEW)}
    world.session.execute(
        update(QueryRun).where(QueryRun.id == run.id).values(status=QueryRunStatus.FAILED)
    )
    world.session.commit()
    with pytest.raises(PermissionDenied) as caught:
        _write(world, mode, _fenced(world.principal, run), run)
    assert str(caught.value) == WRITE_FENCE_MESSAGE
    assert {ref: _location(world, ref) for ref in (_EVENTS, _FRESH, _VIEW)} == before
    seen = _seen(world)
    assert seen.scalar(select(Table.id).where(Table.name.in_(["fresh", "fenced_view"]))) is None
    assert not seen.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_WRITE)
    ).all()
    assert _run_row(world, run).rows_written is None


@pytest.mark.parametrize("mode", _MODES)
def test_every_write_mode_of_a_running_fenced_run_leaves_its_evidence(
    world: _World, mode: str
) -> None:
    run = _running(world.session, world.principal)
    ref, rows = _write(world, mode, _fenced(world.principal, run), run)
    stored = _run_row(world, run)
    table = _seen(world).scalars(select(Table).where(Table.name == ref.name)).one()
    if mode in ("empty append", "view"):
        expected_snapshot = None
    else:
        expected_snapshot = world.catalog.load_table(
            world.principal, ref
        ).metadata.current_snapshot_id
        assert expected_snapshot is not None
    assert (stored.rows_written, stored.snapshot_id, stored.target_table_id) == (
        rows,
        expected_snapshot,
        table.id,
    )
    assert rows == (0 if mode in ("empty append", "view") else 1)
    assert stored.status is QueryRunStatus.RUNNING


@pytest.mark.parametrize("ending", ["failed", "cancelled", "another worker"])
def test_a_write_whose_run_was_ended_before_the_commit_lands_nothing(
    world: _World, ending: str
) -> None:
    """Ended by someone else while the rows stream: pointer, audit, lineage, evidence untouched."""
    run = _running(world.session, world.principal)
    source = world.session.scalars(select(Table).where(Table.name == "src")).one()
    values = {
        "failed": {"status": QueryRunStatus.FAILED},
        "cancelled": {"status": QueryRunStatus.CANCELLED},
        "another worker": {"worker_id": "w2"},
    }[ending]
    before = _location(world, _EVENTS)

    def stream() -> Iterator[pa.RecordBatch]:
        other = world.observer
        other.execute(update(QueryRun).where(QueryRun.id == run.id).values(**values))
        other.commit()
        yield from _ROWS.to_batches()

    with pytest.raises(PermissionDenied) as caught:
        world.catalog.write_batches(
            _fenced(world.principal, run),
            _EVENTS,
            mode=WriteMode.APPEND,
            schema=_ROWS.schema,
            batches=stream(),
            sources=[LineageSource(table_id=source.id, name="bronze.src", snapshot_id=1)],
            query_run_id=run.id,
        )
    assert str(caught.value) == WRITE_FENCE_MESSAGE
    assert _location(world, _EVENTS) == before
    seen = _seen(world)
    assert not seen.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_WRITE)
    ).all()
    assert not seen.scalars(select(LineageEdge)).all()
    assert _run_row(world, run).rows_written is None


@pytest.mark.parametrize("mode", ["create", "append"])
def test_a_failure_after_the_fence_passed_takes_the_evidence_back_with_the_write(
    world: _World, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """The evidence and the pointer swap share one transaction: neither survives the other."""
    run = _running(world.session, world.principal)
    ref = _FRESH if mode == "create" else _EVENTS
    before = _location(world, ref)
    real = ProtectedWriteGuard.record_pending
    passed: list[int | None] = []

    def fence_passes_then_the_commit_fails(guard: ProtectedWriteGuard) -> None:
        real(guard)
        passed.append(
            world.session.scalar(select(QueryRun.rows_written).where(QueryRun.id == run.id))
        )
        raise ConflictError("a concurrent writer won")

    monkeypatch.setattr(ProtectedWriteGuard, "record_pending", fence_passes_then_the_commit_fails)
    with pytest.raises(ConflictError):
        _write(world, mode, _fenced(world.principal, run), run)
    assert passed == [1]  # the fence had written its evidence, uncommitted, in this transaction
    assert _location(world, ref) == before
    assert _run_row(world, run).rows_written is None
    assert (
        not _seen(world)
        .scalars(select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_WRITE))
        .all()
    )


def test_the_fence_is_taken_after_the_pointer_swap_not_before_the_metadata_upload(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run ends after the real swap: only a fence checked then refuses.

    A fence taken earlier (at `check(COMMIT)`) would hold the run's row lock across the
    metadata file's upload, which the design rejects; here it would also let the write land.
    """
    run = _running(world.session, world.principal)
    before = _location(world, _EVENTS)
    real = catalog_service.commit_metadata_pointer

    def swap_then_end_the_run(
        pointer_session: Session,
        table_id: uuid.UUID,
        *,
        expected_location: str | None,
        new_location: str,
        publish: bool = False,
    ) -> None:
        real(
            pointer_session,
            table_id,
            expected_location=expected_location,
            new_location=new_location,
            publish=publish,
        )
        pointer_session.execute(
            update(QueryRun).where(QueryRun.id == run.id).values(status=QueryRunStatus.FAILED)
        )

    monkeypatch.setattr(catalog_service, "commit_metadata_pointer", swap_then_end_the_run)
    with pytest.raises(PermissionDenied) as caught:
        _write(world, "append", _fenced(world.principal, run), run)
    assert str(caught.value) == WRITE_FENCE_MESSAGE
    assert _location(world, _EVENTS) == before
    assert _run_row(world, run).rows_written is None


def test_a_note_made_under_one_runs_fence_is_not_evidence_for_another_run(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A commit without a note of its own passes on the check alone and writes no evidence."""
    _unprotected(session)
    run_a = _running(session, principal)
    run_b = _running(session, principal)
    guard = ProtectedWriteGuard(session, AllowAllAuthorization())
    guard.check(_fenced(principal, run_a), "dev", WriteOperation.WRITE, ("bronze", "events"))
    guard.note_result("dev", ("bronze", "events"), rows=5, snapshot_id=99, table_id=uuid.uuid4())
    # A's write failed before its record_pending; B commits on the same guard.
    guard.check(_fenced(principal, run_b), "dev", WriteOperation.COMMIT, ("bronze", "events"))
    guard.record_pending()
    session.commit()
    session.refresh(run_a)
    session.refresh(run_b)
    assert (run_a.rows_written, run_b.rows_written, run_b.snapshot_id) == (None, None, None)


_EVENTS_TARGET = ("bronze", "events")


def _checked_task(world: _World, *, has_checks: bool) -> tuple[PrincipalRef, uuid.UUID, uuid.UUID]:
    """Insert a SQL task of `bronze.events` and return its principal as the worker builds it.

    Parameters
    ----------
    world
        The world.
    has_checks
        Whether the asset has checks.

    Returns
    -------
    tuple of (PrincipalRef, uuid.UUID, uuid.UUID)
        The principal with the task's run context and its write fence, the task run's id and a
        table id for the task to note.
    """
    context = _task(world.session, world.principal, "bronze.events")
    assert context.query_run_id is not None
    world.session.execute(
        update(QueryRun).where(QueryRun.id == context.query_run_id).values(worker_id="w1")
    )
    world.session.execute(
        update(TaskRun).where(TaskRun.id == context.task_run_id).values(has_checks=has_checks)
    )
    world.session.commit()
    p = dataclasses.replace(
        _in_run(world.principal, context), write_fence=WriteFence(context.query_run_id, "w1")
    )
    return p, context.task_run_id, uuid.uuid4()


def test_a_fenced_write_records_the_snapshot_before(world: _World) -> None:
    run = _running(world.session, world.principal)
    guard = ProtectedWriteGuard(world.session, AllowAllAuthorization())
    p = _fenced(world.principal, run)
    guard.check(p, "dev", WriteOperation.WRITE, _EVENTS_TARGET)
    guard.note_result(
        "dev", _EVENTS_TARGET, rows=1, snapshot_id=12, table_id=uuid.uuid4(), snapshot_before=11
    )
    guard.record_pending()
    world.session.commit()
    assert _run_row(world, run).snapshot_before == 11


@pytest.mark.parametrize("publishes", [False, True])
def test_the_commit_of_a_task_with_checks_stamps_the_task_run(
    world: _World, publishes: bool
) -> None:
    p, task_run_id, table_id = _checked_task(world, has_checks=True)
    guard = ProtectedWriteGuard(world.session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.WRITE, _EVENTS_TARGET)
    guard.note_result(
        "dev",
        _EVENTS_TARGET,
        rows=1,
        snapshot_id=12,
        table_id=table_id,
        snapshot_before=11,
        publishes=publishes,
    )
    guard.record_pending()
    world.session.commit()
    assert read_written(_seen(world), task_run_id) == Written(table_id, 11, 12, publishes)


def test_a_task_without_checks_is_not_stamped(world: _World) -> None:
    p, task_run_id, table_id = _checked_task(world, has_checks=False)
    guard = ProtectedWriteGuard(world.session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.WRITE, _EVENTS_TARGET)
    guard.note_result(
        "dev", _EVENTS_TARGET, rows=1, snapshot_id=12, table_id=table_id, snapshot_before=11
    )
    guard.record_pending()
    world.session.commit()
    assert read_written(_seen(world), task_run_id) is None


def test_the_stamp_rolls_back_with_the_write(world: _World) -> None:
    p, task_run_id, table_id = _checked_task(world, has_checks=True)
    guard = ProtectedWriteGuard(world.session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.WRITE, _EVENTS_TARGET)
    guard.note_result(
        "dev", _EVENTS_TARGET, rows=1, snapshot_id=12, table_id=table_id, snapshot_before=11
    )
    guard.record_pending()
    world.session.rollback()
    assert read_written(_seen(world), task_run_id) is None
    stored = _seen(world).get(QueryRun, p.write_fence.query_run_id if p.write_fence else None)
    assert stored is not None and stored.rows_written is None


def test_a_refused_fence_stamps_nothing(world: _World) -> None:
    p, task_run_id, table_id = _checked_task(world, has_checks=True)
    assert p.write_fence is not None
    world.session.execute(
        update(QueryRun)
        .where(QueryRun.id == p.write_fence.query_run_id)
        .values(status=QueryRunStatus.FAILED)
    )
    world.session.commit()
    guard = ProtectedWriteGuard(world.session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.WRITE, _EVENTS_TARGET)
    guard.note_result(
        "dev", _EVENTS_TARGET, rows=1, snapshot_id=12, table_id=table_id, snapshot_before=11
    )
    with pytest.raises(WriteFenceRefused):
        guard.record_pending()
    assert read_written(_seen(world), task_run_id) is None


def test_a_claim_form_write_does_not_stamp(world: _World) -> None:
    """A Python task's own REST write must not stamp: the worker does that after the process."""
    task = python_task_run(world.session, world.principal.id, asset="bronze.events", claim=1)
    world.session.execute(update(TaskRun).where(TaskRun.id == task.id).values(has_checks=True))
    world.session.commit()
    fence_run = _running(world.session, world.principal)
    p = dataclasses.replace(
        _in_run(world.principal, RunContext(task_run_id=task.id, claim=1)),
        write_fence=WriteFence(fence_run.id, "w1"),
    )
    guard = ProtectedWriteGuard(world.session, AllowAllAuthorization())
    guard.check(p, "dev", WriteOperation.WRITE, _EVENTS_TARGET)
    guard.note_result(
        "dev", _EVENTS_TARGET, rows=1, snapshot_id=12, table_id=uuid.uuid4(), snapshot_before=11
    )
    guard.record_pending()
    world.session.commit()
    assert read_written(_seen(world), task.id) is None
    assert _run_row(world, fence_run).snapshot_id == 12  # the fence's own evidence is unchanged


def test_a_note_of_another_fence_stamps_nothing(world: _World) -> None:
    run_a = _running(world.session, world.principal)
    p, task_run_id, _ = _checked_task(world, has_checks=True)
    guard = ProtectedWriteGuard(world.session, AllowAllAuthorization())
    guard.check(_fenced(world.principal, run_a), "dev", WriteOperation.WRITE, _EVENTS_TARGET)
    guard.note_result(
        "dev", _EVENTS_TARGET, rows=5, snapshot_id=99, table_id=uuid.uuid4(), snapshot_before=98
    )
    guard.check(p, "dev", WriteOperation.COMMIT, _EVENTS_TARGET)
    guard.record_pending()
    world.session.commit()
    assert read_written(_seen(world), task_run_id) is None


def test_a_rollback_after_a_failed_write_stamps_nothing(world: _World) -> None:
    """Evidence noted by a write that then failed must not be re-stamped by a later rollback."""
    from lakana_core.models import RollbackOutcome
    from lakana_core.runs.tasks import Written as _Written

    first = world.catalog.write_batches(
        world.principal,
        _EVENTS,
        mode=WriteMode.REPLACE,
        schema=_ROWS.schema,
        batches=_ROWS.to_batches(),
    )
    second = world.catalog.write_batches(
        world.principal,
        _EVENTS,
        mode=WriteMode.REPLACE,
        schema=_ROWS.schema,
        batches=_ROWS.to_batches(),
    )
    p, task_run_id, _ = _checked_task(world, has_checks=True)
    table_id = world.session.scalars(select(Table.id).where(Table.name == "events")).one()
    task = world.session.get(TaskRun, task_run_id)
    assert task is not None
    guard = world.catalog._guard
    guard.check(p, "dev", WriteOperation.WRITE, _EVENTS_TARGET)
    guard.note_result(
        "dev", _EVENTS_TARGET, rows=1, snapshot_id=99, table_id=uuid.uuid4(), snapshot_before=98
    )  # a write that noted its result and then failed before record_pending

    result = world.catalog.roll_back(
        p,
        _EVENTS,
        run_id=task.run_id,
        task_run_id=task_run_id,
        written=_Written(table_id, first.snapshot_id, second.snapshot_id, publishes=False),
        failed_checks=[],
    )

    assert result.outcome is RollbackOutcome.ROLLED_BACK
    assert read_written(_seen(world), task_run_id) is None


# --- the tip of a Python task's own writes, for append and the Iceberg commit ---


def _current(world: _World, ref: TableRef = _EVENTS) -> int | None:
    """Read a table's current snapshot as the admin.

    Parameters
    ----------
    world
        The world.
    ref
        The table.

    Returns
    -------
    int or None
        Its current snapshot id.
    """
    return world.catalog.load_table(world.principal, ref).metadata.current_snapshot_id


def _python_task(
    world: _World, *, asset: str = "bronze.events", baseline: tuple[int | None, bool] | None = None
) -> tuple[PrincipalRef, uuid.UUID]:
    """Insert a running Python task with checks and its baseline; return it as its token is.

    Parameters
    ----------
    world
        The world.
    asset
        The task's own asset.
    baseline
        The baseline's snapshot and "publishes"; the table's current snapshot, not publishing,
        when `None`.

    Returns
    -------
    tuple of (PrincipalRef, uuid.UUID)
        The principal with the task's claim-form run context, and the task run's id.
    """
    snapshot, publishes = (_current(world), False) if baseline is None else baseline
    task = python_task_run(world.session, world.principal.id, asset=asset, claim=1)
    world.session.execute(
        update(TaskRun)
        .where(TaskRun.id == task.id)
        .values(has_checks=True, baseline_snapshot_id=snapshot, baseline_publishes=publishes)
    )
    world.session.commit()
    return _in_run(world.principal, RunContext(task_run_id=task.id, claim=1)), task.id


def _tip(world: _World, task_run_id: uuid.UUID) -> OwnTip | None:
    """Read a task run's own tip from what is committed.

    Parameters
    ----------
    world
        The world.
    task_run_id
        The task run.

    Returns
    -------
    OwnTip or None
        The tip.
    """
    return read_own_tip(_seen(world), task_run_id)


def _add_a_snapshot(world: _World, p: PrincipalRef) -> int:
    """Commit one appended row as an Iceberg client would: an added snapshot made the head.

    Parameters
    ----------
    world
        The world.
    p
        The caller.

    Returns
    -------
    int
        The added snapshot.
    """
    loaded = world.catalog.load_table(world.principal, _EVENTS)
    file_io = StorageResolver(world.session).for_environment("dev").file_io
    requirements, updates = build_transaction(
        loaded.metadata, loaded.metadata_location, file_io, _ROWS
    )
    committed = world.catalog.commit_table(
        p, _EVENTS, requirements=list(requirements), updates=list(updates)
    )
    added = committed.metadata.current_snapshot_id
    assert added is not None and added != loaded.metadata.current_snapshot_id
    return added


def test_a_python_tasks_append_moves_its_own_tip(world: _World) -> None:
    world.catalog.append(world.principal, _EVENTS, _ROWS)
    p, task_run_id = _python_task(world)

    first = world.catalog.append(p, _EVENTS, _ROWS).snapshot_id
    assert _tip(world, task_run_id) == OwnTip(first)
    second = world.catalog.append(p, _EVENTS, _ROWS).snapshot_id

    assert _tip(world, task_run_id) == OwnTip(second)


def test_a_python_tasks_iceberg_commit_that_adds_a_snapshot_moves_its_own_tip(
    world: _World,
) -> None:
    world.catalog.append(world.principal, _EVENTS, _ROWS)
    p, task_run_id = _python_task(world)

    added = _add_a_snapshot(world, p)

    assert _tip(world, task_run_id) == OwnTip(added)


def test_pointing_main_at_a_snapshot_that_exists_is_not_the_tasks_own_write(
    world: _World,
) -> None:
    world.catalog.append(world.principal, _EVENTS, _ROWS)
    earlier = _current(world)
    world.catalog.append(world.principal, _EVENTS, _ROWS)
    p, task_run_id = _python_task(world)
    own = world.catalog.append(p, _EVENTS, _ROWS).snapshot_id
    assert earlier is not None and own is not None

    world.catalog.commit_table(
        p,
        _EVENTS,
        requirements=[],
        updates=[
            SetSnapshotRefUpdate(ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=earlier)
        ],
    )

    assert _current(world) == earlier
    assert _tip(world, task_run_id) == OwnTip(own)


def test_a_python_task_that_writes_its_new_table_and_appends_records_the_append(
    world: _World,
) -> None:
    """The write that creates the table is the first link of the chain, the append the next."""
    fresh = TableRef("dev", _BRONZE, "fresh")
    p, task_run_id = _python_task(world, asset="bronze.fresh", baseline=(None, True))

    created = world.catalog.replace(p, fresh, _ROWS, expected_snapshot_id="none")
    assert _tip(world, task_run_id) == OwnTip(_current(world, fresh))
    appended = world.catalog.append(p, fresh, _ROWS).snapshot_id

    assert created.rows == 1 and appended is not None
    assert _tip(world, task_run_id) == OwnTip(appended)


def test_a_fenced_commit_without_a_note_writes_no_evidence(world: _World) -> None:
    """A SQL task's commit notes nothing by itself: `rows_written` means "committed"."""
    p, task_run_id, _ = _checked_task(world, has_checks=True)
    assert p.write_fence is not None

    world.catalog.commit_table(
        p, _EVENTS, requirements=[], updates=[SetPropertiesUpdate(updates={"k": "v"})]
    )
    _add_a_snapshot(world, p)

    stored = _seen(world).get(QueryRun, p.write_fence.query_run_id)
    assert stored is not None
    assert (stored.rows_written, stored.snapshot_id) == (None, None)
    assert read_written(_seen(world), task_run_id) is None


def test_a_commit_of_the_rollback_notes_nothing_even_when_it_adds_a_snapshot(
    world: _World,
) -> None:
    """Only the rollback passes `internal_rollback`; its commit is never the task's own write.

    The rollback's own commit adds no snapshot, so the commit here adds one through the same
    parameter to show that the parameter alone keeps it from being noted. (A maintenance commit
    cannot add a snapshot at all: `_is_maintenance_update` refuses it.)
    """
    world.catalog.append(world.principal, _EVENTS, _ROWS)
    p, task_run_id = _python_task(world)
    own = world.catalog.append(p, _EVENTS, _ROWS).snapshot_id
    loaded = world.catalog.load_table(world.principal, _EVENTS)
    file_io = StorageResolver(world.session).for_environment("dev").file_io
    requirements, updates = build_transaction(
        loaded.metadata, loaded.metadata_location, file_io, _ROWS
    )

    world.catalog.commit_table(
        p, _EVENTS, requirements=list(requirements), updates=list(updates), internal_rollback=True
    )

    assert _current(world) not in (None, own)
    assert _tip(world, task_run_id) == OwnTip(own)


def test_a_python_tasks_own_sql_query_under_a_fence_is_noted_by_its_write_only(
    world: _World,
) -> None:
    """A claim with a write fence: a commit without a note of its own writes no evidence.

    A note under the fence would make `_hold_fence` write `rows_written`, which a reclaim reads
    as "committed", and would move the tip for a write the worker did not make.
    """
    world.catalog.append(world.principal, _EVENTS, _ROWS)
    claimed, task_run_id = _python_task(world)
    run = _running(world.session, world.principal)
    p = dataclasses.replace(claimed, write_fence=WriteFence(run.id, "w1"))

    _add_a_snapshot(world, p)

    assert _run_row(world, run).rows_written is None
    assert _tip(world, task_run_id) is None


def test_a_refused_commit_leaves_no_note_behind(world: _World) -> None:
    """The note is taken only after every refusal of the commit."""
    world.catalog.append(world.principal, _EVENTS, _ROWS)
    p, task_run_id = _python_task(world)
    loaded = world.catalog.load_table(world.principal, _EVENTS)
    file_io = StorageResolver(world.session).for_environment("dev").file_io
    requirements, updates = build_transaction(
        loaded.metadata, loaded.metadata_location, file_io, _ROWS
    )

    with pytest.raises(ValidationError, match=r"write\.data\.path"):
        world.catalog.commit_table(
            p,
            _EVENTS,
            requirements=list(requirements),
            updates=[*updates, SetPropertiesUpdate(updates={"write.data.path": "file:///x"})],
        )

    assert world.catalog._guard._evidence is None
    assert _tip(world, task_run_id) is None


@pytest.mark.parametrize(
    ("summary", "rows"),
    [
        (None, 0),
        ({}, 0),
        ({"added-records": "12"}, 12),
        ({"added-records": "-3"}, 0),
        ({"added-records": "many"}, 0),
    ],
    ids=["no-summary", "no-entry", "a-number", "negative", "not-a-number"],
)
def test_the_rows_of_an_added_snapshot_come_from_its_summary(
    summary: dict[str, str] | None, rows: int
) -> None:
    """The summary is the client's; what cannot be read counts as no rows.

    Parameters
    ----------
    summary
        The summary's entries; `None` for a snapshot without a summary.
    rows
        What is counted.
    """
    snapshot = Snapshot(
        snapshot_id=1,
        timestamp_ms=0,
        manifest_list="file:///m.avro",
        summary=None if summary is None else Summary(Operation.APPEND, **summary),
    )

    assert _added_records(snapshot) == rows
    assert _added_records(None) == 0
