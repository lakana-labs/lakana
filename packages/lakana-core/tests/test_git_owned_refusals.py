"""What a deployed project defines is changed only in git: drops and creates under its names refuse.

A table or view a current deployment builds cannot be dropped, nor renamed away; an artifact it
produces cannot be dropped; and no create (managed, external, a person's create-mode write, an
Iceberg rename onto it) may take a declared name. The file is named only to a caller who may
read the object. A drop is refused only when the declaration and the row are of the same kind.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    ActionAllowList,
    _minimal_schema,
    _prepared_catalog,
    artifact_service,
    deploy_assets,
    ensure_table,
    python_task_run,
    store_version,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.artifacts.store import artifact_prefix, dropped_marker_path
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    BoundTask,
    PrincipalRef,
    RunContext,
    TaskScope,
)
from lakana_core.catalog.git_owned import (
    GIT_OWNED_ARTIFACT_DROP,
    GIT_OWNED_ARTIFACT_HIDDEN,
    GIT_OWNED_CREATE,
    GIT_OWNED_DROP,
    GIT_OWNED_HIDDEN,
    GIT_OWNED_RENAME,
    GIT_OWNED_RENAME_HIDDEN,
    GIT_OWNED_REPLACE,
)
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import ConflictError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Table,
    TableLocation,
    TableLocationState,
    TableType,
    WriteMode,
)
from lakana_core.storage.resolver import StorageResolver

_SILVER = NamespacePath.from_parts(["silver"])
_CLEAN = TableRef("dev", _SILVER, "clean")
_REPO = "analytics"


@pytest.fixture
def catalog(session: Session, tmp_path: Path) -> CatalogService:
    """Create `dev` on `tmp_path` with `silver` and `raw`, and return an allow-all catalog.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[_SILVER, NamespacePath.from_parts(["raw"])],
    )


def _with(session: Session, authz: object) -> CatalogService:
    """Build a catalog on `session` with another authorization.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return CatalogService(session, StorageResolver(session), authz)  # ty: ignore[invalid-argument-type]


def _managed(catalog: CatalogService, p: PrincipalRef, ref: TableRef = _CLEAN) -> uuid.UUID:
    """Create a managed table with its metadata and location record.

    Parameters
    ----------
    catalog
        The catalog.
    p
        The acting principal.
    ref
        The table.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    return catalog.create_table(p, ref, schema=_minimal_schema()).uuid


def _declare(session: Session, *names: str, **fields: object) -> None:
    """Deploy `analytics` to `dev` declaring `names`, each with `fields` over the defaults.

    Parameters
    ----------
    session
        Database session.
    *names
        Dotted asset names.
    **fields
        Entry fields that override the default SQL table entry.
    """
    deploy_assets(session, "dev", _REPO, {name: dict(fields) for name in names})


def _assert_intact(session: Session, table_id: uuid.UUID) -> None:
    """Assert a table was not dropped: its row, no tombstone, no `TABLE_DROPPED`.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    """
    session.rollback()
    assert session.get(Table, table_id) is not None
    tombstones = session.scalars(
        select(TableLocation).where(
            TableLocation.table_id == table_id,
            TableLocation.state == TableLocationState.DROPPED,
        )
    ).all()
    assert tombstones == []
    assert _events(session, AuditEventType.TABLE_DROPPED) == []


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


def _drop_message(name: str = "silver.clean", path: str = "assets/silver/clean.sql") -> str:
    """Fill `GIT_OWNED_DROP` for `dev` and `analytics`.

    Parameters
    ----------
    name
        The dotted table name.
    path
        The declaring file.

    Returns
    -------
    str
        The message.
    """
    return GIT_OWNED_DROP.format(name=name, repository=_REPO, path=path, environment="dev")


def _create_message(name: str = "silver.clean", path: str = "assets/silver/clean.sql") -> str:
    """Fill `GIT_OWNED_CREATE` for `dev` and `analytics`.

    Parameters
    ----------
    name
        The dotted table name.
    path
        The declaring file.

    Returns
    -------
    str
        The message.
    """
    return GIT_OWNED_CREATE.format(name=name, repository=_REPO, path=path, environment="dev")


@pytest.fixture
def built(session: Session, catalog: CatalogService, principal: PrincipalRef) -> uuid.UUID:
    """Create `silver.clean` as a managed table, then deploy the project that builds it.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog.
    principal
        The acting principal.

    Returns
    -------
    uuid.UUID
        The table's id.
    """
    table_id = _managed(catalog, principal)
    _declare(session, "silver.clean")
    return table_id


def test_dropping_a_table_a_deployment_builds_is_refused_naming_the_file(
    session: Session, catalog: CatalogService, principal: PrincipalRef, built: uuid.UUID
) -> None:
    with pytest.raises(ConflictError) as refused:
        catalog.drop_table(principal, _CLEAN)

    assert str(refused.value) == _drop_message()
    _assert_intact(session, built)
    assert catalog.load_table(principal, _CLEAN).uuid == built


def test_a_view_a_deployment_builds_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _, view_id = ensure_table(session, "dev", "silver", "clean")
    session.get_one(Table, view_id).table_type = TableType.VIEW
    session.commit()
    _declare(session, "silver.clean", materialize="view")

    with pytest.raises(ConflictError) as refused:
        catalog.drop_table(principal, _CLEAN)

    assert str(refused.value) == _drop_message()
    _assert_intact(session, view_id)


def test_purge_does_not_get_past_the_refusal(
    session: Session, catalog: CatalogService, principal: PrincipalRef, built: uuid.UUID
) -> None:
    with pytest.raises(ConflictError) as refused:
        catalog.drop_table(principal, _CLEAN, purge=True)

    assert str(refused.value) == _drop_message()
    _assert_intact(session, built)


def test_break_glass_does_not_get_past_the_refusal(
    session: Session, catalog: CatalogService, principal: PrincipalRef, built: uuid.UUID
) -> None:
    catalog.set_environment_protection(principal, "dev", True)
    glass = dataclasses.replace(principal, break_glass_reason="INC-7 remove by hand")

    with pytest.raises(ConflictError) as refused:
        _with(session, ActionAllowList(*Action)).drop_table(glass, _CLEAN)

    assert str(refused.value) == _drop_message()
    _assert_intact(session, built)


def test_a_caller_who_may_not_read_the_table_is_not_told_the_file(
    session: Session, principal: PrincipalRef, built: uuid.UUID
) -> None:
    with pytest.raises(ConflictError) as refused:
        _with(session, ActionAllowList(Action.DROP_TABLE)).drop_table(principal, _CLEAN)

    message = str(refused.value)
    assert message == GIT_OWNED_HIDDEN
    assert "analytics" not in message
    assert "assets/" not in message
    _assert_intact(session, built)


def test_a_table_under_an_artifact_assets_name_can_still_be_dropped(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    table_id = _managed(catalog, principal)
    _declare(session, "silver.clean", kind="python", output="artifact")

    catalog.drop_table(principal, _CLEAN)

    session.rollback()
    assert session.get(Table, table_id) is None


def test_an_external_table_under_a_declared_name_can_be_dropped(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    ref = TableRef("dev", NamespacePath.from_parts(["raw"]), "events")
    row = catalog.create_external_table(
        principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
    )
    _declare(session, "raw.events")

    catalog.drop_table(principal, ref)

    session.rollback()
    assert session.get(Table, row.id) is None


def test_a_table_the_current_version_no_longer_declares_can_be_dropped(
    session: Session, catalog: CatalogService, principal: PrincipalRef, built: uuid.UUID
) -> None:
    _declare(session, "silver.other")

    catalog.drop_table(principal, _CLEAN)

    session.rollback()
    assert session.get(Table, built) is None


def test_a_legacy_dotted_namespace_is_not_taken_for_another_declaration(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    # `["a.b"].c` predates the rule against dots; it shares the dotted name `a.b.c` with
    # `["a", "b"].c`, which is what the deployment declares.
    _, legacy_id = ensure_table(session, "dev", "a.b", "c")
    _declare(session, "a.b.c")
    legacy = TableRef("dev", NamespacePath.from_parts(["a.b"], allow_dots=True), "c")

    catalog.drop_table(principal, legacy)

    session.rollback()
    assert session.get(Table, legacy_id) is None


def test_dropping_an_artifact_a_deployment_produces_is_refused(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    service = artifact_service(session, tmp_path)
    store_version(service, principal, ARTIFACT_REF, {"m": b"weights"})
    artifact_id = session.scalars(select(Table.id).where(Table.name == "churn")).one()
    _declare(session, "models.churn", kind="python", output="artifact", path="assets/churn.py")
    storage = StorageResolver(session).for_environment("dev").storage
    before = sorted(storage.list(artifact_prefix(artifact_id)))

    with pytest.raises(ConflictError) as refused:
        service.drop_artifact(principal, ARTIFACT_REF)

    assert str(refused.value) == GIT_OWNED_ARTIFACT_DROP.format(
        name="models.churn", repository=_REPO, path="assets/churn.py", environment="dev"
    )
    after = sorted(storage.list(artifact_prefix(artifact_id)))
    assert after == before
    assert dropped_marker_path(artifact_id) not in after
    assert _events(session, AuditEventType.ARTIFACT_DROPPED) == []
    assert session.get(Table, artifact_id) is not None


def test_an_external_table_under_a_declared_name_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    _declare(session, "raw.events")
    ref = TableRef("dev", NamespacePath.from_parts(["raw"]), "events")

    with pytest.raises(ConflictError) as refused:
        catalog.create_external_table(
            principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
        )

    assert str(refused.value) == _create_message("raw.events", "assets/raw/events.sql")
    session.rollback()
    assert session.scalars(select(TableLocation)).all() == []
    assert session.scalars(select(Table).where(Table.name == "events")).all() == []


def test_a_managed_table_under_a_declared_name_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _declare(session, "silver.clean")

    with pytest.raises(ConflictError) as refused:
        _managed(catalog, principal)

    assert str(refused.value) == _create_message()
    session.rollback()
    assert session.scalars(select(TableLocation)).all() == []
    assert session.scalars(select(Table).where(Table.name == "clean")).all() == []


def test_a_create_under_a_declared_name_hides_the_file_from_a_caller_who_may_not_read_it(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _declare(session, "silver.clean")
    creator = _with(session, ActionAllowList(Action.CREATE_TABLE))

    with pytest.raises(ConflictError) as refused:
        _managed(creator, principal)

    assert str(refused.value) == GIT_OWNED_HIDDEN


def test_an_undeclared_external_table_is_still_created(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    _declare(session, "raw.other")
    ref = TableRef("dev", NamespacePath.from_parts(["raw"]), "events")

    row = catalog.create_external_table(
        principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
    )

    assert row.table_type is TableType.EXTERNAL


def _rows() -> pa.Table:
    """Build a one-row result.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table({"id": pa.array([1], pa.int64())})


def _create_write(catalog: CatalogService, p: PrincipalRef) -> object:
    """Write `_rows` to `silver.clean` in create mode.

    Parameters
    ----------
    catalog
        The catalog.
    p
        The writer.

    Returns
    -------
    object
        The write's result.
    """
    data = _rows()
    return catalog.write_batches(
        p, _CLEAN, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
    )


def test_a_persons_write_that_would_create_a_declared_table_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _declare(session, "silver.clean")

    with pytest.raises(ConflictError) as refused:
        _create_write(catalog, principal)

    assert str(refused.value) == _create_message()
    session.rollback()
    assert session.scalars(select(TableLocation)).all() == []
    assert session.scalars(select(Table).where(Table.name == "clean")).all() == []


def test_a_run_still_creates_the_table_its_project_declares(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _declare(session, "silver.clean")
    run = python_task_run(session, principal.id, asset="silver.clean", kind="sql")
    task = dataclasses.replace(principal, run_context=RunContext(task_run_id=run.id, claim=1))

    _create_write(catalog, task)

    session.rollback()
    assert session.scalars(select(Table).where(Table.name == "clean")).one() is not None


def test_an_iceberg_rename_onto_a_declared_name_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    source = TableRef("dev", _SILVER, "draft")
    table_id = _managed(catalog, principal, source)
    _declare(session, "silver.clean")

    with pytest.raises(ConflictError) as refused:
        catalog.rename_table(principal, source, _CLEAN)

    assert str(refused.value) == _create_message()
    session.rollback()
    assert session.get_one(Table, table_id).name == "draft"


def test_an_iceberg_rename_of_a_table_a_deployment_builds_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef, built: uuid.UUID
) -> None:
    with pytest.raises(ConflictError) as refused:
        catalog.rename_table(principal, _CLEAN, TableRef("dev", _SILVER, "moved"))

    assert str(refused.value) == GIT_OWNED_RENAME.format(
        name="silver.clean", repository=_REPO, path="assets/silver/clean.sql", environment="dev"
    )
    session.rollback()
    assert session.get_one(Table, built).name == "clean"


def test_an_undeclared_rename_still_moves_the_table(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    source = TableRef("dev", _SILVER, "draft")
    table_id = _managed(catalog, principal, source)
    _declare(session, "silver.other")

    catalog.rename_table(principal, source, TableRef("dev", _SILVER, "final"))

    session.rollback()
    assert session.get_one(Table, table_id).name == "final"


def test_a_rename_away_hides_the_file_from_a_caller_who_may_not_read_the_table(
    session: Session, principal: PrincipalRef, built: uuid.UUID
) -> None:
    mover = _with(session, ActionAllowList(Action.DROP_TABLE, Action.CREATE_TABLE))

    with pytest.raises(ConflictError) as refused:
        mover.rename_table(principal, _CLEAN, TableRef("dev", _SILVER, "moved"))

    assert str(refused.value) == GIT_OWNED_RENAME_HIDDEN
    assert "analytics" not in str(refused.value)


def test_a_persons_upload_of_a_new_artifact_under_a_declared_name_is_refused(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    service = artifact_service(session, tmp_path)
    _declare(session, "models.churn", kind="python", output="artifact", path="assets/churn.py")

    with pytest.raises(ConflictError) as refused:
        store_version(service, principal, ARTIFACT_REF, {"m": b"weights"})

    assert str(refused.value) == _create_message("models.churn", "assets/churn.py")
    session.rollback()
    assert session.scalars(select(Table).where(Table.name == "churn")).all() == []


def test_a_persons_upload_under_a_declared_table_name_is_refused_too(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    service = artifact_service(session, tmp_path)
    _declare(session, "models.churn")

    with pytest.raises(ConflictError) as refused:
        store_version(service, principal, ARTIFACT_REF, {"m": b"weights"})

    assert str(refused.value) == _create_message("models.churn", "assets/models/churn.sql")


def test_an_upload_hides_the_file_from_a_caller_who_may_not_read_the_name(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    service = artifact_service(
        session, tmp_path, ActionAllowList(Action.WRITE_TABLE, Action.CREATE_TABLE)
    )
    _declare(session, "models.churn", kind="python", output="artifact")

    with pytest.raises(ConflictError) as refused:
        store_version(service, principal, ARTIFACT_REF, {"m": b"weights"})

    assert str(refused.value) == GIT_OWNED_ARTIFACT_HIDDEN


def test_a_run_still_uploads_the_artifact_its_project_declares(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    service = artifact_service(session, tmp_path)
    _declare(session, "models.churn", kind="python", output="artifact")
    task = python_task_run(
        session, principal.id, asset="models.churn", extra={"output": "artifact"}
    )
    run = dataclasses.replace(principal, run_context=RunContext(task_run_id=task.id, claim=1))

    store_version(service, run, ARTIFACT_REF, {"m": b"weights"})

    assert session.scalars(select(Table).where(Table.name == "churn")).one() is not None


def _dropped(catalog: CatalogService, p: PrincipalRef) -> None:
    """Create `silver.clean` and drop it while nothing declares it.

    Parameters
    ----------
    catalog
        The catalog.
    p
        The acting principal.
    """
    _managed(catalog, p)
    catalog.drop_table(p, _CLEAN)


def test_an_undrop_onto_a_name_now_declared_as_an_artifact_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _dropped(catalog, principal)
    _declare(session, "silver.clean", kind="python", output="artifact")

    with pytest.raises(ConflictError) as refused:
        catalog.undrop_table(principal, _CLEAN)

    assert str(refused.value) == _create_message("silver.clean", "assets/silver/clean.sql")
    session.rollback()
    assert session.scalars(select(Table).where(Table.name == "clean")).all() == []


def test_an_undrop_onto_a_name_declared_as_a_table_is_allowed(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _dropped(catalog, principal)
    _declare(session, "silver.clean")

    restored = catalog.undrop_table(principal, _CLEAN)

    assert restored.ref == _CLEAN


def test_a_run_of_one_repository_cannot_create_a_table_another_declares(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    # Deploy's ownership claim keeps `silver.b` out of repository `a`'s manifest; what keeps a
    # run of `a` from creating it anyway is its task token, bound to its own asset.
    deploy_assets(session, "dev", "a", {"silver.a": {}})
    deploy_assets(session, "dev", "b", {"silver.b": {}})
    task = python_task_run(session, principal.id, asset="silver.a", kind="sql")
    bound = BoundTask(
        task_run_id=task.id, claim=1, asset="silver.a", inputs=(), readable=(), repository="a"
    )
    run = dataclasses.replace(
        principal,
        run_context=RunContext(task_run_id=task.id, claim=1),
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4(), bound=bound),
    )
    data = _rows()

    with pytest.raises(PermissionDenied):
        _with(session, GrantAuthorization(session)).write_batches(
            run,
            TableRef("dev", _SILVER, "b"),
            mode=WriteMode.CREATE,
            schema=data.schema,
            batches=data.to_batches(),
        )

    session.rollback()
    assert session.scalars(select(Table).where(Table.name == "b")).all() == []


def test_an_undrop_as_a_declared_table_name_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    # A person's own table, restored under a name the project declares, would take that name.
    draft = TableRef("dev", _SILVER, "draft")
    _managed(catalog, principal, draft)
    catalog.drop_table(principal, draft)
    _declare(session, "silver.clean")

    with pytest.raises(ConflictError) as refused:
        catalog.undrop_table(principal, draft, new_name="clean")

    assert str(refused.value) == _create_message()
    session.rollback()
    assert session.scalars(select(Table).where(Table.name.in_(["clean", "draft"]))).all() == []


def _view(catalog: CatalogService, p: PrincipalRef, sql: str = "SELECT 1 AS id") -> Table:
    """Create or replace the view `silver.clean`.

    Parameters
    ----------
    catalog
        The catalog.
    p
        The writer.
    sql
        The view's SELECT.

    Returns
    -------
    Table
        The view's row.
    """
    return catalog.create_or_replace_view(
        p,
        _CLEAN,
        sql=sql,
        references=[],
        columns=[ColumnSpec(name="id", type="int64", nullable=True)],
    )


def test_a_persons_view_under_a_declared_name_is_refused_before_the_first_run(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _declare(session, "silver.clean", materialize="view")

    with pytest.raises(ConflictError) as refused:
        _view(catalog, principal)

    assert str(refused.value) == _create_message()
    session.rollback()
    assert session.scalars(select(Table).where(Table.name == "clean")).all() == []


def test_a_persons_view_under_a_name_declared_as_a_table_is_refused(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _declare(session, "silver.clean")

    with pytest.raises(ConflictError) as refused:
        _view(catalog, principal)

    assert str(refused.value) == _create_message()


def _run_of_clean(session: Session, principal: PrincipalRef) -> PrincipalRef:
    """Make the principal of a running task of `silver.clean`.

    Parameters
    ----------
    session
        Database session.
    principal
        The run's principal.

    Returns
    -------
    PrincipalRef
        It, with the task's run context.
    """
    task = python_task_run(session, principal.id, asset="silver.clean", kind="sql")
    return dataclasses.replace(principal, run_context=RunContext(task_run_id=task.id, claim=1))


def test_a_run_still_creates_the_view_its_project_declares(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _declare(session, "silver.clean", materialize="view")

    row = _view(catalog, _run_of_clean(session, principal))

    assert row.table_type is TableType.VIEW


def test_a_person_may_not_replace_the_sql_of_a_project_view(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _declare(session, "silver.clean", materialize="view")
    view_id = _view(catalog, _run_of_clean(session, principal)).id

    with pytest.raises(ConflictError) as refused:
        _view(catalog, principal, "SELECT 2 AS id")

    assert str(refused.value) == GIT_OWNED_REPLACE.format(
        name="silver.clean", repository=_REPO, path="assets/silver/clean.sql", environment="dev"
    )
    session.rollback()
    assert (session.get_one(Table, view_id).view_definition or {})["sql"] == "SELECT 1 AS id"


def test_a_person_replacing_a_project_view_without_read_is_not_told_the_file(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _declare(session, "silver.clean", materialize="view")
    _view(catalog, _run_of_clean(session, principal))
    writer = _with(session, ActionAllowList(Action.WRITE_TABLE))

    with pytest.raises(ConflictError) as refused:
        _view(writer, principal, "SELECT 2 AS id")

    assert str(refused.value) == GIT_OWNED_HIDDEN


def test_a_person_may_still_replace_a_view_no_project_builds(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    _view(catalog, principal)
    _declare(session, "silver.other")

    row = _view(catalog, principal, "SELECT 2 AS id")

    assert (row.view_definition or {})["sql"] == "SELECT 2 AS id"
