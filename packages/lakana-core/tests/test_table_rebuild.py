"""Rebuild: drop a table a deployed project builds and queue a run of its asset, in one go.

A rebuild is an operational action on a table that git owns. It drops the table, which keeps
its undo period (`TABLE_DROPPED` with reason ``rebuild``), records `TABLE_REBUILT`, and queues a
run of exactly the asset that builds it. Nothing is written when the run cannot be queued, so a
person is never left with a dropped table and no run.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from unittest import mock

import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    ActionAllowList,
    _minimal_schema,
    _prepared_catalog,
    artifact_service,
    deploy_assets,
    store_version,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, AuthorizationService, PrincipalRef
from lakana_core.catalog.rebuild import (
    NOT_BUILT_BY_A_PROJECT,
    REBUILD_ARTIFACT,
    REBUILD_RUN_ACTIVE,
    REBUILD_RUN_ACTIVE_UNREAD,
    TableRebuild,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import ConflictError, PermissionDenied, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Run,
    Table,
    TableLocation,
    TableLocationState,
    TaskRun,
)
from lakana_core.runs.selection import Selection
from lakana_core.runs.service import NO_RUN_PRINCIPAL_MESSAGE, ActiveRunError, RunService

_SILVER = NamespacePath.from_parts(["silver"])
_CLEAN = TableRef("dev", _SILVER, "clean")
_REASON = "the source was corrected upstream"


def _catalog(session: Session, tmp_path: Path) -> CatalogService:
    """Build a catalog with the `dev` environment and the `silver` namespace.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage root.

    Returns
    -------
    CatalogService
        An allow-all catalog.
    """
    return _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])


def _managed(catalog: CatalogService, principal: PrincipalRef) -> uuid.UUID:
    """Create `silver.clean` as a managed table with a location record.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        Who creates it.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    return catalog.create_table(principal, _CLEAN, schema=_minimal_schema()).uuid


def _rebuild(
    session: Session, catalog: CatalogService, authz: AuthorizationService | None = None
) -> TableRebuild:
    """Build the rebuild service over `catalog`.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog.
    authz
        The authorization to check; allow-all by default.

    Returns
    -------
    TableRebuild
        The service, with a 7-day undo period.
    """
    authz = authz or AllowAllAuthorization()
    return TableRebuild(session, catalog, RunService(session, authz), authz, undo_days=7)


def _events(session: Session, kind: AuditEventType) -> list[AuditEvent]:
    """Read the audit events of one kind.

    Parameters
    ----------
    session
        Database session.
    kind
        The event type.

    Returns
    -------
    list of AuditEvent
        The events.
    """
    return list(session.scalars(select(AuditEvent).where(AuditEvent.event_type == kind)))


def _audit_rows(session: Session) -> int:
    """Count the audit rows.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    int
        How many there are.
    """
    return session.execute(select(func.count()).select_from(AuditEvent)).scalar_one()


def _assert_intact(session: Session, table_id: uuid.UUID) -> None:
    """Assert that the table was not dropped: its row, no tombstone, no drop event.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table's id.
    """
    session.rollback()
    assert session.get(Table, table_id) is not None
    dropped = session.scalars(
        select(TableLocation).where(TableLocation.state == TableLocationState.DROPPED)
    ).all()
    assert dropped == []
    assert _events(session, AuditEventType.TABLE_DROPPED) == []


def test_rebuild_drops_the_table_and_queues_a_run_of_its_asset(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _catalog(session, tmp_path)
    table_id = _managed(catalog, principal)
    deploy_assets(session, "dev", "analytics", {"silver.clean": {}, "silver.other": {}})

    result = _rebuild(session, catalog).rebuild(principal, _CLEAN, reason=f"  {_REASON} ")

    assert result.ref == _CLEAN
    assert result.repository == "analytics"
    assert result.path == "assets/silver/clean.sql"
    run = session.get(Run, result.run_id)
    assert run is not None
    assert run.selection_json == {
        "patterns": ["silver.clean"],
        "upstream": False,
        "downstream": False,
    }
    tasks = session.scalars(select(TaskRun.asset).where(TaskRun.run_id == run.id)).all()
    assert list(tasks) == ["silver.clean"]
    assert session.get(Table, table_id) is None
    (dropped,) = catalog.list_dropped_tables(principal, "dev", _SILVER)
    assert (dropped.name, dropped.restorable) == ("clean", True)
    (drop,) = _events(session, AuditEventType.TABLE_DROPPED)
    assert drop.detail["reason"] == "rebuild"
    assert drop.detail["rebuild_reason"] == _REASON
    (rebuilt,) = _events(session, AuditEventType.TABLE_REBUILT)
    assert rebuilt.detail == {"table_id": str(table_id), "reason": _REASON, "run_id": str(run.id)}
    assert rebuilt.securable_table == "clean"


def test_rebuild_of_a_table_no_project_builds_points_to_drop(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _catalog(session, tmp_path)
    table_id = _managed(catalog, principal)
    deploy_assets(session, "dev", "analytics", {"silver.other": {}})

    with pytest.raises(ConflictError) as refused:
        _rebuild(session, catalog).rebuild(principal, _CLEAN, reason=_REASON)

    assert str(refused.value) == NOT_BUILT_BY_A_PROJECT.format(
        name="silver.clean", environment="dev"
    )
    _assert_intact(session, table_id)


def test_rebuild_of_an_artifact_is_refused(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[ARTIFACT_REF.namespace]
    )
    store_version(artifact_service(session, tmp_path), principal, ARTIFACT_REF, {"m": b"one"})
    artifact_id = session.execute(
        select(Table.id).where(Table.name == ARTIFACT_REF.name)
    ).scalar_one()
    deploy_assets(
        session,
        "dev",
        "analytics",
        {ARTIFACT_REF.dotted(): {"kind": "python", "output": "artifact"}},
    )

    with pytest.raises(ConflictError) as refused:
        _rebuild(session, catalog).rebuild(principal, ARTIFACT_REF, reason=_REASON)

    assert str(refused.value) == REBUILD_ARTIFACT.format(name=ARTIFACT_REF.dotted())
    session.rollback()
    assert session.get(Table, artifact_id) is not None
    assert _events(session, AuditEventType.TABLE_REBUILT) == []
    assert session.scalars(select(Run)).all() == []


def test_rebuild_of_a_table_under_an_artifact_assets_name_queues_that_asset(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _catalog(session, tmp_path)
    table_id = _managed(catalog, principal)
    deploy_assets(
        session,
        "dev",
        "analytics",
        {"silver.clean": {"kind": "python", "output": "artifact", "path": "assets/clean.py"}},
    )

    result = _rebuild(session, catalog).rebuild(principal, _CLEAN, reason=_REASON)

    assert result.path == "assets/clean.py"
    tasks = session.scalars(select(TaskRun.asset).where(TaskRun.run_id == result.run_id)).all()
    assert list(tasks) == ["silver.clean"]
    assert session.get(Table, table_id) is None


@pytest.mark.parametrize("allowed", [Action.DROP_TABLE, Action.RUN])
def test_rebuild_needs_drop_table_and_run(
    session: Session, tmp_path: Path, principal: PrincipalRef, allowed: Action
) -> None:
    catalog = _catalog(session, tmp_path)
    table_id = _managed(catalog, principal)
    deploy_assets(session, "dev", "analytics", {"silver.clean": {}})

    with pytest.raises(PermissionDenied):
        _rebuild(session, catalog, ActionAllowList(allowed)).rebuild(
            principal, _CLEAN, reason=_REASON
        )

    _assert_intact(session, table_id)


def test_rebuild_names_the_file_only_to_a_caller_who_may_read_the_table(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Ruling 2: without `READ_TABLE` the answer does not name the file."""
    catalog = _catalog(session, tmp_path)
    _managed(catalog, principal)
    deploy_assets(session, "dev", "analytics", {"silver.clean": {}})
    authz = ActionAllowList(Action.DROP_TABLE, Action.RUN)

    result = _rebuild(session, catalog, authz).rebuild(principal, _CLEAN, reason=_REASON)

    assert (result.repository, result.path) == ("analytics", None)


def test_rebuild_while_a_run_is_active_drops_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _catalog(session, tmp_path)
    table_id = _managed(catalog, principal)
    deploy_assets(session, "dev", "analytics", {"silver.clean": {}})
    active = RunService(session, AllowAllAuthorization()).start(
        principal, "dev", "analytics", Selection()
    )
    before = _audit_rows(session)

    with pytest.raises(ConflictError) as refused:
        _rebuild(session, catalog).rebuild(principal, _CLEAN, reason=_REASON)

    assert str(refused.value) == REBUILD_RUN_ACTIVE.format(
        run_id=active.id, repository="analytics", environment="dev", name="silver.clean"
    )
    _assert_intact(session, table_id)
    assert _audit_rows(session) == before
    assert len(session.scalars(select(Run)).all()) == 1


def test_an_active_run_that_ended_before_it_was_read_is_named_without_an_id(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """F13: `ActiveRunError.run_id` is `None`; the sentence has no ``Run None``."""
    catalog = _catalog(session, tmp_path)
    table_id = _managed(catalog, principal)
    deploy_assets(session, "dev", "analytics", {"silver.clean": {}})
    rebuild = _rebuild(session, catalog)

    with (
        mock.patch.object(RunService, "insert_run", side_effect=ActiveRunError("gone", None)),
        pytest.raises(ConflictError) as refused,
    ):
        rebuild.rebuild(principal, _CLEAN, reason=_REASON)

    assert str(refused.value) == REBUILD_RUN_ACTIVE_UNREAD.format(
        repository="analytics", environment="dev", name="silver.clean"
    )
    _assert_intact(session, table_id)


def test_rebuild_in_a_protected_environment_without_a_run_principal_drops_nothing(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _catalog(session, tmp_path)
    table_id = _managed(catalog, principal)
    deploy_assets(session, "dev", "analytics", {"silver.clean": {}})
    catalog.set_environment_protection(principal, "dev", True)
    glass = dataclasses.replace(principal, break_glass_reason="incident 9: rebuild by hand")
    before = _audit_rows(session)

    with pytest.raises(ConflictError) as refused:
        _rebuild(session, catalog).rebuild(glass, _CLEAN, reason=_REASON)

    assert str(refused.value) == NO_RUN_PRINCIPAL_MESSAGE.format(
        repository="analytics", environment="dev"
    )

    _assert_intact(session, table_id)
    assert _audit_rows(session) == before
    assert session.scalars(select(Run)).all() == []


def test_rebuild_in_a_protected_environment_needs_break_glass(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _catalog(session, tmp_path)
    table_id = _managed(catalog, principal)
    deploy_assets(session, "dev", "analytics", {"silver.clean": {}})
    catalog.set_environment_protection(principal, "dev", True)

    with pytest.raises(PermissionDenied):
        _rebuild(session, catalog).rebuild(principal, _CLEAN, reason=_REASON)

    _assert_intact(session, table_id)
    assert session.scalars(select(Run)).all() == []


@pytest.mark.parametrize("reason", ["too short", "two\nlines", "one line, then\nanother one"])
def test_the_rebuild_reason_is_checked(
    session: Session, tmp_path: Path, principal: PrincipalRef, reason: str
) -> None:
    catalog = _catalog(session, tmp_path)
    table_id = _managed(catalog, principal)
    deploy_assets(session, "dev", "analytics", {"silver.clean": {}})

    with pytest.raises(ValidationError, match="The rebuild reason"):
        _rebuild(session, catalog).rebuild(principal, _CLEAN, reason=reason)

    _assert_intact(session, table_id)
