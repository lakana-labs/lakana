"""Table operations refuse an artifact and say that it is one."""

from __future__ import annotations

import contextlib
import inspect
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pytest
from core_test_helpers import (
    ActionAllowList,
    FakeColumnCatalog,
    _bootstrap_principal,
    _prepared_catalog,
    python_task_run,
)
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.artifacts.service import ArtifactService
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable
from lakana_core.catalog.identity import artifact_refusal, refuse_view_as_policy_target
from lakana_core.catalog.query_service import QueryService, WriteTarget
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.writes import LineageSource
from lakana_core.errors import (
    AlreadyExistsError,
    CheckResultGoneError,
    ConflictError,
    LakanaError,
    NotFoundError,
    PermissionDenied,
    UnsupportedTableError,
    ValidationError,
)
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    RollbackOutcome,
    Table,
    TableType,
    WriteMode,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.projects.checks import CheckSpec
from lakana_core.runs.checks import record_result
from lakana_core.runs.tasks import Written
from lakana_core.storage.resolver import StorageResolver

_REF = TableRef("dev", NamespacePath.from_parts(["gold"]), "churn")
_ROWS = pa.table({"id": pa.array([1], pa.int64())})


def _catalog(session: Session, tmp_path: Path) -> tuple[CatalogService, PrincipalRef]:
    """Build a catalog in which `gold.churn` is an artifact.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage.

    Returns
    -------
    tuple
        The catalog (allow-all authorization) and a principal.
    """
    catalog = _prepared_catalog(
        session,
        tmp_path,
        AllowAllAuthorization(),
        namespaces=[NamespacePath.from_parts(["gold"])],
        tables=[_REF],
    )
    row = session.query(Table).filter(Table.name == "churn").one()
    row.table_type = TableType.ARTIFACT
    row.metadata_location = None
    row.previous_metadata_location = None
    session.commit()
    return catalog, _bootstrap_principal(session)


def test_the_refusal_text() -> None:
    assert artifact_refusal("gold.churn", "maintenance") == (
        "gold.churn is an artifact; maintenance applies to tables. See `lakana artifact --help`."
    )


def test_describe_shows_an_artifact_without_columns(session: Session, tmp_path: Path) -> None:
    catalog, p = _catalog(session, tmp_path)
    description = catalog.describe_table(p, _REF)
    assert (description.kind, description.metadata_location, description.columns) == (
        "ARTIFACT",
        None,
        [],
    )
    assert catalog.load_table_row(p, _REF).table_type is TableType.ARTIFACT


def test_the_iceberg_surface_never_sees_an_artifact(session: Session, tmp_path: Path) -> None:
    catalog, p = _catalog(session, tmp_path)
    assert catalog.table_exists(p, _REF) is False
    assert catalog.list_tables(p, "dev", _REF.namespace, include_views=False) == []
    assert catalog.list_tables(p, "dev", _REF.namespace) == [_REF]
    not_exposed = (
        "gold.churn is an artifact; artifacts have no Iceberg metadata and are not exposed over "
        "the Iceberg REST catalog."
    )
    with pytest.raises(NotFoundError) as loaded:
        catalog.load_table(p, _REF)
    assert str(loaded.value) == not_exposed
    with pytest.raises(NotFoundError) as committed:
        catalog.commit_table(p, _REF, requirements=[], updates=[])
    assert str(committed.value) == not_exposed
    with pytest.raises(NotFoundError) as dropped:
        catalog.drop_table(p, _REF, include_views=False)
    assert str(dropped.value) == not_exposed
    with pytest.raises(NotFoundError, match="artifacts can't be renamed"):
        catalog.rename_table(p, _REF, TableRef("dev", _REF.namespace, "other"))


def test_a_rename_that_loses_a_race_for_the_name_is_the_already_exists_conflict(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """m4: another writer took the destination after the check: the unique key says so, not a 500.

    The check is made to see a free name, as it would have a moment before the other writer's
    commit; the name is in fact held by an artifact, so the write meets the unique constraint.
    """
    gold = NamespacePath.from_parts(["gold"])
    source = TableRef("dev", gold, "source")
    catalog = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[gold], tables=[source]
    )
    p = _bootstrap_principal(session)
    namespace_id = session.query(Table).filter(Table.name == "source").one().namespace_id
    session.execute(
        insert(Table).values(
            id=uuid.uuid4(), namespace_id=namespace_id, name="taken", table_type=TableType.ARTIFACT
        )
    )
    session.commit()
    real = CatalogService._lookup_table
    monkeypatch.setattr(
        CatalogService,
        "_lookup_table",
        lambda self, namespace, name: None if name == "taken" else real(self, namespace, name),
    )
    with pytest.raises(AlreadyExistsError, match="artifact"):
        catalog.rename_table(p, source, TableRef("dev", gold, "taken"))
    monkeypatch.undo()
    assert sorted(session.scalars(select(Table.name))) == ["source", "taken"]
    moved = catalog.rename_table(p, source, TableRef("dev", gold, "moved"))
    assert moved.ref.name == "moved"


def test_a_rename_that_fails_for_another_integrity_reason_is_a_conflict_with_a_next_step(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gold = NamespacePath.from_parts(["gold"])
    source = TableRef("dev", gold, "source")
    catalog = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[gold], tables=[source]
    )
    p = _bootstrap_principal(session)

    def failing(_session: Session) -> None:
        raise IntegrityError("UPDATE tables", {}, Exception("FOREIGN KEY constraint failed"))

    monkeypatch.setattr(Session, "commit", failing)
    with pytest.raises(ConflictError) as caught:
        catalog.rename_table(p, source, TableRef("dev", gold, "moved"))
    assert str(caught.value) == (
        "Could not rename table 'source' to 'moved' in gold: the write failed for a reason other "
        "than a name collision. Check that the namespace still exists, then try again."
    )
    assert "UPDATE" not in str(caught.value)


@pytest.mark.parametrize(
    ("call", "error", "text"),
    [
        (
            lambda c, p: c.check_can_append(p, _REF),
            ValidationError,
            artifact_refusal("gold.churn", "appending rows"),
        ),
        (
            lambda c, p: c.append(p, _REF, _ROWS),
            ValidationError,
            artifact_refusal("gold.churn", "appending rows"),
        ),
        (
            lambda c, p: c.check_can_replace(p, _REF),
            ValidationError,
            artifact_refusal("gold.churn", "replacing rows"),
        ),
        (
            lambda c, p: c.replace(p, _REF, _ROWS, expected_snapshot_id="none"),
            ValidationError,
            artifact_refusal("gold.churn", "replacing rows"),
        ),
        (
            lambda c, p: c.check_can_merge(p, _REF),
            ValidationError,
            artifact_refusal("gold.churn", "merging rows"),
        ),
        (
            lambda c, p: c.merge(p, _REF, _ROWS, key=["id"], expected_snapshot_id="none"),
            ValidationError,
            artifact_refusal("gold.churn", "merging rows"),
        ),
        (
            lambda c, p: c.read_watermark(p, _REF, "id"),
            ValidationError,
            artifact_refusal("gold.churn", "an incremental key"),
        ),
        (
            lambda c, p: c.maintain(p, _REF),
            ValidationError,
            artifact_refusal("gold.churn", "maintenance"),
        ),
        (
            lambda c, p: c.set_table_description(p, _REF, "text"),
            ValidationError,
            artifact_refusal("gold.churn", "a description"),
        ),
        (
            lambda c, p: c.set_column_description(p, _REF, "id", "text"),
            ValidationError,
            artifact_refusal("gold.churn", "a description"),
        ),
        (
            lambda c, p: c.write_batches(
                p, _REF, mode=WriteMode.REPLACE, schema=_ROWS.schema, batches=_ROWS.to_batches()
            ),
            ValidationError,
            "gold.churn is an artifact; only managed tables can be written. Drop the artifact "
            "(`lakana artifact drop gold.churn`) or write to another name.",
        ),
        (
            lambda c, p: c.write_batches(
                p, _REF, mode=WriteMode.APPEND, schema=_ROWS.schema, batches=_ROWS.to_batches()
            ),
            ValidationError,
            "gold.churn is an artifact; only managed tables can be written. Drop the artifact "
            "(`lakana artifact drop gold.churn`) or write to another name.",
        ),
        (
            lambda c, p: c.drop_table(p, _REF),
            ValidationError,
            "gold.churn is an artifact; drop it with `lakana artifact drop gold.churn`, which "
            "also deletes its versions' bytes.",
        ),
    ],
)
def test_a_table_operation_says_it_is_an_artifact(
    session: Session,
    tmp_path: Path,
    call: Callable[[CatalogService, PrincipalRef], object],
    error: type[LakanaError],
    text: str,
) -> None:
    catalog, p = _catalog(session, tmp_path)
    with pytest.raises(error) as caught:
        call(catalog, p)
    assert str(caught.value) == text
    row = session.query(Table).filter(Table.name == "churn").one()
    assert row.table_type is TableType.ARTIFACT
    assert (row.metadata_location, row.previous_metadata_location) == (None, None)


def test_a_view_or_a_table_cannot_take_an_artifacts_name(session: Session, tmp_path: Path) -> None:
    catalog, p = _catalog(session, tmp_path)
    with pytest.raises(AlreadyExistsError) as view:
        catalog.create_or_replace_view(p, _REF, sql="SELECT 1 AS id", references=[], columns=[])
    assert str(view.value) == (
        "gold.churn is an artifact; a view can't replace it. Drop it first "
        "(`lakana artifact drop gold.churn`), or choose another name."
    )
    taken = (
        "'churn' already exists in gold as an artifact. Choose another name, or drop the "
        "artifact first (`lakana artifact drop gold.churn`)."
    )
    schema = Schema(NestedField(1, "id", LongType(), required=True))
    for call in (
        lambda: catalog.write_batches(
            p, _REF, mode=WriteMode.CREATE, schema=_ROWS.schema, batches=_ROWS.to_batches()
        ),
        lambda: catalog.create_table(p, _REF, schema=schema),
        lambda: catalog.create_external_table(
            p, _REF, location_pattern="raw/churn/*.parquet", format="parquet"
        ),
    ):
        with pytest.raises(AlreadyExistsError) as caught:
            call()
        assert str(caught.value) == taken
    other = TableRef("dev", _REF.namespace, "other")
    catalog.create_table(p, other, schema=schema)
    with pytest.raises(AlreadyExistsError) as renamed:
        catalog.rename_table(p, other, _REF)
    assert str(renamed.value) == taken
    names = {r.name: r.table_type for r in session.query(Table).all()}
    assert names == {"churn": TableType.ARTIFACT, "other": TableType.ICEBERG}


def test_an_undrop_cannot_take_an_artifacts_name_and_never_lists_one(
    session: Session, tmp_path: Path
) -> None:
    catalog, p = _catalog(session, tmp_path)
    other = TableRef("dev", _REF.namespace, "other")
    catalog.create_table(p, other, schema=Schema(NestedField(1, "id", LongType())))
    catalog.drop_table(p, other)
    with pytest.raises(AlreadyExistsError) as caught:
        catalog.undrop_table(p, other, new_name="churn")
    assert "an artifact of that name exists in gold" in str(caught.value)
    with pytest.raises(NotFoundError):
        catalog.undrop_table(p, _REF)
    assert [d.name for d in catalog.list_dropped_tables(p, "dev", _REF.namespace)] == ["other"]


def test_an_artifact_has_no_retention_and_is_never_listed_as_expiring(
    session: Session, tmp_path: Path
) -> None:
    catalog, p = _catalog(session, tmp_path)
    catalog.set_environment_retention(p, "dev", 1)
    for call in (
        lambda: catalog.table_retention(p, _REF),
        lambda: catalog.set_table_retention(p, _REF, days=3),
    ):
        with pytest.raises(ValidationError) as caught:
            call()
        assert "artifacts never expire" in str(caught.value)
    assert catalog.list_expiring_tables(p, "dev", within_days=3650) == []


def test_a_namespace_with_an_artifact_is_not_dropped(session: Session, tmp_path: Path) -> None:
    catalog, p = _catalog(session, tmp_path)
    with pytest.raises(ConflictError) as caught:
        catalog.drop_namespace(p, "dev", _REF.namespace)
    assert str(caught.value) == (
        "Namespace gold still contains artifacts; drop each first (`lakana artifact drop`)."
    )


def test_a_policy_cannot_be_attached_to_an_artifact(session: Session, tmp_path: Path) -> None:
    _catalog(session, tmp_path)
    row = session.query(Table).filter(Table.name == "churn").one()
    with pytest.raises(ValidationError) as caught:
        refuse_view_as_policy_target(session, row.id, "gold", "churn")
    assert str(caught.value) == (
        "'gold.churn' is an artifact; row filters and column masks cannot be attached to an "
        "artifact. Whoever may read an artifact reads all of it."
    )


def test_a_view_cannot_reference_an_artifact(session: Session, tmp_path: Path) -> None:
    catalog, p = _catalog(session, tmp_path)
    references = catalog.resolve_lineage_sources(p, "dev", ["gold.churn"])
    assert [r.name for r in references] == ["gold.churn"]
    view = TableRef("dev", _REF.namespace, "v")
    with pytest.raises(UnsupportedTableError) as caught:
        catalog.create_or_replace_view(
            p, view, sql="SELECT * FROM gold.churn", references=references, columns=[]
        )
    assert str(caught.value) == (
        "gold.churn is an artifact; SQL cannot read it. Only a Python asset can read an "
        "artifact: declare it in the asset's inputs."
    )
    assert session.query(Table).filter(Table.name == "v").count() == 0


@dataclass
class _Context:
    """What one authorization case hands to a call.

    Parameters
    ----------
    catalog
        A catalog bound to the case's authorization.
    queries
        A query service bound to it.
    principal
        The caller.
    authz
        The authorization itself, for services the call builds.
    source
        The lineage source of the table the call names, for a view that references it.
    ref
        The table the call names: the artifact, or for the baseline run an ordinary table.
    """

    catalog: CatalogService
    queries: QueryService
    principal: PrincipalRef
    authz: ActionAllowList
    source: list[LineageSource]
    ref: TableRef


def test_a_rollback_of_an_artifact_reports_not_a_table(session: Session, tmp_path: Path) -> None:
    catalog, p = _catalog(session, tmp_path)
    row_id = session.query(Table).filter(Table.name == "churn").one().id

    result = catalog.roll_back(
        p,
        _REF,
        run_id=uuid.uuid4(),
        task_run_id=uuid.uuid4(),
        written=Written(row_id, None, None, False),
        failed_checks=["gold.churn/c"],
    )

    assert result.outcome is RollbackOutcome.NOT_A_TABLE
    row = session.query(Table).filter(Table.name == "churn").one()
    assert row.table_type is TableType.ARTIFACT
    assert (row.metadata_location, row.unpublished_run_id) == (None, None)


def test_the_rows_of_a_check_are_never_read_from_an_artifact(
    session: Session, tmp_path: Path
) -> None:
    """A check result is never an artifact's, even one that carries the row's id and a snapshot."""
    catalog, p = _catalog(session, tmp_path)
    row_id = session.query(Table).filter(Table.name == "churn").one().id
    task = python_task_run(session, p.id, asset="gold.churn", kind="sql", environment="dev")
    task.has_checks = True
    session.commit()
    assert record_result(
        session,
        task_run_id=task.id,
        worker="w1",
        claim=task.claim_count,
        spec=CheckSpec(
            name="gold.churn/unique_id",
            table="gold.churn",
            check="unique_id",
            path="checks/gold/churn/unique_id.sql",
            on_failure="block",
            description=None,
            sql="SELECT id FROM gold.churn GROUP BY id HAVING count(*) > 1",
        ),
        outcome=CheckOutcome.FAILED,
        failing_rows=1,
        error=None,
        table_id=row_id,
        checked_snapshot_id=7,
        policy_applied=False,
        query_run_id=None,
        started_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )
    result = session.scalars(select(CheckResult).where(CheckResult.task_run_id == task.id)).one()

    with pytest.raises(CheckResultGoneError) as caught:
        catalog.load_table_at_check(p, _REF, check_result_id=result.id)

    # The text of a result that is not the named table's: it names no run and no check.
    assert str(caught.value) == (
        "The rows of this check cannot be read from `gold.churn`: the check result is not one "
        "of this table as it exists now, or was removed with its run."
    )
    row = session.query(Table).filter(Table.name == "churn").one()
    assert (row.table_type, row.metadata_location) == (TableType.ARTIFACT, None)


def _denied_calls() -> dict[str, Callable[[_Context], object]]:
    """List every operation that must authorize before it says anything about an artifact.

    Returns
    -------
    dict
        A label for each call, and the call, which takes a `_Context`.
    """
    other = TableRef("dev", _REF.namespace, "other")
    schema = Schema(NestedField(1, "id", LongType(), required=True))
    calls: dict[str, Callable[[_Context], object]] = {
        "load_table": lambda x: x.catalog.load_table(x.principal, x.ref),
        "load_table_row": lambda x: x.catalog.load_table_row(x.principal, x.ref),
        "load_table_at_check": lambda x: x.catalog.load_table_at_check(
            x.principal, x.ref, check_result_id=uuid.UUID("0b0f4a6e-52a7-4f0c-8f0e-3f6f1c2a9d11")
        ),
        "commit_table": lambda x: x.catalog.commit_table(
            x.principal, x.ref, requirements=[], updates=[]
        ),
        "describe_table": lambda x: x.catalog.describe_table(x.principal, x.ref),
        "table_health": lambda x: x.catalog.table_health(
            x.principal, x.ref, days=30, now=datetime.now(UTC)
        ),
        "table_exists": lambda x: x.catalog.table_exists(x.principal, x.ref),
        "check_can_append": lambda x: x.catalog.check_can_append(x.principal, x.ref),
        "append": lambda x: x.catalog.append(x.principal, x.ref, _ROWS),
        "check_can_replace": lambda x: x.catalog.check_can_replace(x.principal, x.ref),
        "replace": lambda x: x.catalog.replace(
            x.principal, x.ref, _ROWS, expected_snapshot_id="none"
        ),
        "check_can_merge": lambda x: x.catalog.check_can_merge(x.principal, x.ref),
        "merge": lambda x: x.catalog.merge(
            x.principal, x.ref, _ROWS, key=["id"], expected_snapshot_id="none"
        ),
        "read_watermark": lambda x: x.catalog.read_watermark(x.principal, x.ref, "id"),
        "maintain": lambda x: x.catalog.maintain(x.principal, x.ref),
        "drop_table": lambda x: x.catalog.drop_table(x.principal, x.ref),
        "drop_table_iceberg": lambda x: x.catalog.drop_table(
            x.principal, x.ref, include_views=False
        ),
        "rename_from": lambda x: x.catalog.rename_table(x.principal, x.ref, other),
        "rename_onto": lambda x: x.catalog.rename_table(x.principal, other, x.ref),
        "roll_back": lambda x: x.catalog.roll_back(
            x.principal,
            x.ref,
            run_id=uuid.uuid4(),
            task_run_id=uuid.uuid4(),
            written=Written(uuid.uuid4(), None, None, False),
            failed_checks=[],
        ),
        "create_table": lambda x: x.catalog.create_table(x.principal, x.ref, schema=schema),
        "create_external_table": lambda x: x.catalog.create_external_table(
            x.principal, x.ref, location_pattern="raw/churn/*.parquet", format="parquet"
        ),
        "create_view": lambda x: x.catalog.create_or_replace_view(
            x.principal, x.ref, sql="SELECT 1 AS id", references=[], columns=[]
        ),
        "create_view_referencing": lambda x: x.catalog.create_or_replace_view(
            x.principal,
            TableRef("dev", x.ref.namespace, f"v_{x.ref.name}"),
            sql=f"SELECT * FROM {x.ref.dotted()}",
            references=x.source,
            columns=[],
        ),
        "drop_namespace": lambda x: x.catalog.drop_namespace(x.principal, "dev", _REF.namespace),
        "maintenance_run": lambda x: x.queries.submit_maintenance(
            x.principal, "dev", x.ref.dotted()
        ),
        "row_filter": lambda x: RowFilterService(
            x.catalog._session, x.authz, FakeColumnCatalog()
        ).create(
            x.principal,
            principal="catalog-setup",
            environment="dev",
            namespace="gold",
            table=x.ref.name,
            expression="id > 0",
        ),
        # The column source is a stub: this case records what the mask service itself authorizes;
        # the READ_TABLE of reading columns is tested in test_policy_column_masks.
        "column_mask": lambda x: ColumnMaskService(
            x.catalog._session, x.authz, FakeColumnCatalog()
        ).create(
            x.principal,
            principal="catalog-setup",
            environment="dev",
            namespace="gold",
            table=x.ref.name,
            column="id",
            expression="NULL",
        ),
    }
    for mode in (WriteMode.CREATE, WriteMode.REPLACE, WriteMode.APPEND):
        calls[f"write_batches_{mode.value}"] = lambda x, m=mode: x.catalog.write_batches(
            x.principal, x.ref, mode=m, schema=_ROWS.schema, batches=_ROWS.to_batches()
        )
    for mode in (WriteMode.CREATE, WriteMode.REPLACE, WriteMode.APPEND, WriteMode.VIEW):
        calls[f"submit_write_{mode.value}"] = lambda x, m=mode: x.queries.submit_write(
            x.principal, "dev", "SELECT 1 AS id", WriteTarget(table=x.ref.dotted(), mode=m)
        )
    # `incremental_key` is valid for append only (`WriteTarget.validate`), so there is no
    # replace case: it would be refused before any authorization.
    calls["submit_write_append_incremental"] = lambda x: x.queries.submit_write(
        x.principal,
        "dev",
        "SELECT 1 AS id",
        WriteTarget(table=x.ref.dotted(), mode=WriteMode.APPEND, incremental_key="id"),
    )
    return calls


def _context(session: Session, authz: ActionAllowList, setup: _Setup, ref: TableRef) -> _Context:
    """Bind the services of one case to an authorization.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization of this case.
    setup
        The caller and the lineage sources.
    ref
        The table the call names.

    Returns
    -------
    _Context
        The services and what a call needs.
    """
    return _Context(
        catalog=CatalogService(session, StorageResolver(session), authz),
        queries=QueryService(session, authz),
        principal=setup.principal,
        authz=authz,
        source=setup.sources[ref.name],
        ref=ref,
    )


@dataclass
class _Setup:
    """The caller and the artifact's lineage source.

    Parameters
    ----------
    principal
        The caller.
    sources
        The lineage source of each table the cases name, by name.
    """

    principal: PrincipalRef
    sources: dict[str, list[LineageSource]]


_NAME_TAKEN_LABELS = {
    "create_table",
    "create_external_table",
    "rename_onto",
    "write_batches_create",
    "submit_write_create",
}


@pytest.mark.parametrize("label", sorted(_denied_calls()))
def test_a_caller_without_permission_is_never_told_it_is_an_artifact(
    session: Session, tmp_path: Path, label: str
) -> None:
    allowed, p = _catalog(session, tmp_path)
    schema = Schema(NestedField(1, "id", LongType(), required=True))
    twin = TableRef("dev", _REF.namespace, "twin")
    free = TableRef("dev", _REF.namespace, "free")
    for name in ("other", "twin"):
        allowed.create_table(p, TableRef("dev", _REF.namespace, name), schema=schema)
    setup = _Setup(
        p,
        {
            name: allowed.resolve_lineage_sources(p, "dev", [f"gold.{name}"])
            for name in ("churn", "twin")
        }
        | {"free": []},
    )
    call = _denied_calls()[label]

    # What the call must authorize is read off the same call on an ordinary table: an
    # artifact branch that comes too early would hide the check from a recording of the
    # artifact itself.
    # A call that refuses a taken name looks the name up, so its baseline names a free one.
    baseline = free if label in _NAME_TAKEN_LABELS else twin
    recording = ActionAllowList(*Action)
    with contextlib.suppress(LakanaError):
        call(_context(session, recording, setup, baseline))
    consulted = {action for action, _ in recording.seen}
    assert consulted, f"{label} consulted no authorization at all"

    withheld: list[Action | None] = [None, *sorted(consulted)]
    for missing in withheld:
        allowed_actions = [] if missing is None else [a for a in Action if a is not missing]
        authz = ActionAllowList(*allowed_actions)
        with pytest.raises(PermissionDenied) as caught:
            call(_context(session, authz, setup, _REF))
        assert "artifact" not in str(caught.value), f"{label} without {missing}"


def test_a_view_reference_to_an_artifact_needs_read_on_it(session: Session, tmp_path: Path) -> None:
    allowed, p = _catalog(session, tmp_path)
    source = allowed.resolve_lineage_sources(p, "dev", ["gold.churn"])
    authz = ActionAllowList(Action.CREATE_TABLE)
    catalog = CatalogService(session, StorageResolver(session), authz)
    with pytest.raises(PermissionDenied) as caught:
        catalog.create_or_replace_view(
            p,
            TableRef("dev", _REF.namespace, "v"),
            sql="SELECT * FROM gold.churn",
            references=source,
            columns=[],
        )
    assert "artifact" not in str(caught.value)
    # `ActionAllowList` ignores the securable, so the object asked about is pinned here.
    reads = [securable for action, securable in authz.seen if action is Action.READ_TABLE]
    assert reads == [Securable.table_scope("dev", _REF.namespace, "churn")]


def test_deleting_an_environment_keeps_its_artifact_rows_hidden_until_a_restore(
    session: Session, tmp_path: Path
) -> None:
    """An artifact is no tombstone: its row stays with the deleted environment and comes back."""
    catalog, p = _catalog(session, tmp_path)
    artifacts = session.query(Table).filter(Table.table_type == TableType.ARTIFACT)
    assert artifacts.count() == 1
    catalog.delete_environment(p, "dev")
    assert artifacts.count() == 1
    with pytest.raises(NotFoundError):
        catalog.list_tables(p, "dev", _REF.namespace)
    catalog.restore_environment(p, "dev")
    assert artifacts.count() == 1


# What each public operation that takes, lists or removes tables does when the row is an artifact:
#   refuses              says it is an artifact and does nothing (after authorizing)
#   reports-not-a-table  answers `not_a_table` (the rollback's outcome for a row that is no
#                        managed table), changes nothing but its own audit event
#   describes            shows the row or its absence of columns (`can_read_table` only asks
#                        the read check, whatever the kind)
#   hides                answers as if it did not exist (the Iceberg surface; the read at a
#                        check answers as for a name that holds no table, after the grant)
#   lists                lists it, and hides it only when `include_views=False`
#   name-taken           refuses the name, saying an artifact holds it
#   lineage-source       accepts it as a source, with no snapshot
#   refuses-while-present  refuses to remove the container while an artifact is inside
#   cascades             removes the rows with the container (bytes stay)
#   artifact-surface     the artifact service's own operation: it authorizes on the name first,
#                        then answers for an artifact and, for a row of any other kind, says it
#                        is not an artifact (`test_artifact_service_authorization.py` pins the
#                        order for every method)
_ARTIFACT_HANDLING = {
    "CatalogService": {
        "load_table": "refuses",
        "load_table_row": "describes",
        "load_table_at_check": "hides",
        "commit_table": "refuses",
        "describe_table": "describes",
        "table_health": "describes",
        "set_table_description": "refuses",
        "set_column_description": "refuses",
        "can_read_table": "describes",
        "table_exists": "hides",
        "list_tables": "lists",
        "check_can_append": "refuses",
        "append": "refuses",
        "check_can_replace": "refuses",
        "replace": "refuses",
        "check_can_merge": "refuses",
        "check_can_load": "refuses",
        "merge": "refuses",
        "read_watermark": "refuses",
        "maintain": "refuses",
        "drop_table": "refuses",
        "rename_table": "refuses",
        "undrop_table": "name-taken",
        "list_dropped_tables": "hides",
        "table_retention": "refuses",
        "set_table_retention": "refuses",
        "list_expiring_tables": "hides",
        "create_table": "name-taken",
        "create_external_table": "name-taken",
        "create_or_replace_view": "refuses",
        "write_batches": "refuses",
        "roll_back": "reports-not-a-table",
        "find_view": "hides",
        "resolve_lineage_sources": "lineage-source",
        "drop_namespace": "refuses-while-present",
        "delete_environment": "keeps-with-environment",
        "restore_environment": "keeps-with-environment",
    },
    "QueryService": {
        "submit_write": "refuses",
        "submit_maintenance": "refuses",
    },
    "RowFilterService": {
        "create": "refuses",
        "list_for_table": "describes",
    },
    "ColumnMaskService": {
        "create": "refuses",
        "list_for_table": "describes",
    },
    "ArtifactService": {
        "list_artifacts": "artifact-surface",
        "get_artifact": "artifact-surface",
        "list_versions": "artifact-surface",
        "get_version": "artifact-surface",
        "begin_upload": "artifact-surface",
        "check_can_upload": "artifact-surface",
        "receive_blob": "artifact-surface",
        "put_blob": "artifact-surface",
        "commit_upload": "artifact-surface",
        "abort_upload": "artifact-surface",
        "open_blob": "artifact-surface",
        "delete_version": "artifact-surface",
        "drop_artifact": "artifact-surface",
    },
}
_KINDS_OF_HANDLING = {
    "refuses",
    "reports-not-a-table",
    "describes",
    "hides",
    "lists",
    "name-taken",
    "lineage-source",
    "refuses-while-present",
    "keeps-with-environment",
    "artifact-surface",
}
_BY_NAME = {"drop_namespace", "delete_environment", "restore_environment"}


def _table_methods(cls: type) -> set[str]:
    """Find the public methods of a service that take, list or remove tables.

    Parameters
    ----------
    cls
        The service class.

    Returns
    -------
    set of str
        Names of public methods that have a parameter whose annotation mentions `TableRef` or
        `WriteTarget` (which carries a table name), or whose name contains "table" or
        "dotted", or whose own name contains "table" (`list_tables` takes a namespace) or
        "artifact" (`list_artifacts` takes an environment); and
        `drop_namespace`, `delete_environment` and `restore_environment`, which remove or bring
        back tables with their container.
    """
    found = set()
    for name, method in inspect.getmembers(cls, inspect.isfunction):
        if name.startswith("_"):
            continue
        if "table" in name or "artifact" in name or name in _BY_NAME:
            found.add(name)
        for parameter in inspect.signature(method).parameters.values():
            annotation = str(parameter.annotation)
            if (
                "TableRef" in annotation
                or "WriteTarget" in annotation
                or "table" in parameter.name
                or "dotted" in parameter.name
            ):
                found.add(name)
    return found


class _Shapes:
    """Signatures a table can arrive by, for the selector's own test."""

    def by_optional_ref(self, ref: TableRef | None) -> None:
        """Take an optional reference."""

    def by_many_refs(self, refs: Sequence[TableRef]) -> None:
        """Take several references."""

    def by_id(self, table_id: uuid.UUID) -> None:
        """Take an id."""

    def by_dotted(self, environment: str, dotted: str) -> None:
        """Take a dotted name."""

    def describe_table(self, environment: str, name: str) -> None:
        """Take a name; the method's own name says it is about a table."""

    def list_artifacts(self, environment: str) -> None:
        """Take an environment; the method's own name says it is about artifacts."""

    def unrelated(self, environment: str, name: str) -> None:
        """Take what no table is passed by."""

    def delete_environment(self, name: str) -> None:
        """Remove a container of tables."""


def test_the_selector_finds_a_table_by_every_signature_it_arrives_by() -> None:
    assert _table_methods(_Shapes) == {
        "by_optional_ref",
        "by_many_refs",
        "by_id",
        "by_dotted",
        "describe_table",
        "list_artifacts",
        "delete_environment",
    }


@pytest.mark.parametrize(
    "cls",
    [CatalogService, QueryService, RowFilterService, ColumnMaskService, ArtifactService],
    ids=lambda c: c.__name__,
)
def test_every_operation_on_a_table_says_what_it_does_with_an_artifact(cls: type) -> None:
    classified = _ARTIFACT_HANDLING[cls.__name__]
    found = _table_methods(cls)
    for name in sorted(found - set(classified)):
        pytest.fail(
            f"classify `{cls.__name__}.{name}` for artifacts: add it to _ARTIFACT_HANDLING with "
            "what it does when the row is an artifact, and test that in this module."
        )
    for name in sorted(set(classified) - found):
        pytest.fail(
            f"`{cls.__name__}.{name}` is classified for artifacts but no longer takes a table: "
            "remove it from _ARTIFACT_HANDLING, or fix the selection rule in _table_methods."
        )
    assert set(classified.values()) <= _KINDS_OF_HANDLING


def test_a_load_names_the_bronze_layer_and_never_the_artifact(
    session: Session, tmp_path: Path
) -> None:
    """`check_can_load` refuses a target outside bronze by its layer first, artifact or not."""
    catalog, p = _catalog(session, tmp_path)

    with pytest.raises(ValidationError) as caught:
        catalog.check_can_load(p, _REF, write="replace", expected_snapshot_id="none")

    assert "loads into bronze only" in str(caught.value)
    assert "artifact" not in str(caught.value)
