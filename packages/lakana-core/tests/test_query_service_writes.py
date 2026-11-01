"""Submitting write runs: validation and authorization at submission."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList, python_task_run
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    BoundTask,
    PrincipalRef,
    TaskScope,
)
from lakana_core.catalog.query_service import QueryService, WriteTarget
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import AlreadyExistsError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import Namespace, QueryRunStatus, Table, TableType, WriteMode
from lakana_core.storage.resolver import StorageResolver


@pytest.fixture
def seeded(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` with a managed table `gold.daily`.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        An allow-all catalog.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([1], pa.int64())})
    catalog.write_batches(
        principal,
        TableRef.from_dotted("dev", "gold.daily"),
        mode=WriteMode.CREATE,
        schema=data.schema,
        batches=data.to_batches(),
    )
    return catalog


def test_submit_write_records_the_target_on_a_queued_run(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """A write run is an ordinary queued `QueryRun` carrying its target."""
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal,
        "dev",
        "SELECT 1 AS id",
        WriteTarget(table="gold.daily", mode=WriteMode.APPEND, incremental_key="id"),
    )

    assert run.status == QueryRunStatus.QUEUED
    assert run.write_mode is WriteMode.APPEND
    assert run.target_name == "gold.daily"
    assert run.target_table_id is not None
    assert run.incremental_key == "id"
    assert run.evolve_schema is False


@pytest.mark.parametrize(
    "target",
    [
        WriteTarget(table="gold.daily", mode=WriteMode.REPLACE, incremental_key="id"),
        WriteTarget(table="gold.daily", mode=WriteMode.CREATE, evolve_schema=True),
        WriteTarget(table="gold.v", mode=WriteMode.VIEW, evolve_schema=True),
        WriteTarget(table="gold.daily", mode=WriteMode.MAINTAIN),
    ],
)
def test_option_combinations_the_spec_forbids_are_refused(
    session: Session, principal: PrincipalRef, seeded: CatalogService, target: WriteTarget
) -> None:
    """`incremental_key` is `append` only; `evolve_schema` is `replace`/`append` only."""
    with pytest.raises(ValidationError):
        QueryService(session, AllowAllAuthorization()).submit_write(
            principal, "dev", "SELECT 1 AS id", target
        )


@pytest.mark.parametrize(
    ("target", "needed"),
    [
        (WriteTarget(table="gold.new", mode=WriteMode.CREATE), Action.CREATE_TABLE),
        (WriteTarget(table="gold.daily", mode=WriteMode.REPLACE), Action.WRITE_TABLE),
        (WriteTarget(table="gold.daily", mode=WriteMode.APPEND), Action.WRITE_TABLE),
        (
            WriteTarget(table="gold.daily", mode=WriteMode.APPEND, incremental_key="id"),
            Action.READ_TABLE,
        ),
        (WriteTarget(table="gold.v", mode=WriteMode.VIEW), Action.CREATE_TABLE),
    ],
)
def test_submission_authorizes_every_mode(
    session: Session,
    principal: PrincipalRef,
    seeded: CatalogService,
    target: WriteTarget,
    needed: Action,
) -> None:
    """Security: each mode is refused at submission without its action."""
    everything_else = [action for action in Action if action is not needed]

    with pytest.raises(PermissionDenied, match=needed.value):
        QueryService(session, ActionAllowList(*everything_else)).submit_write(
            principal, "dev", "SELECT 1 AS id", target
        )


def test_create_on_an_existing_table_is_refused_at_submission(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """Fail fast rather than queue a run that must fail."""
    with pytest.raises(AlreadyExistsError):
        QueryService(session, AllowAllAuthorization()).submit_write(
            principal,
            "dev",
            "SELECT 1 AS id",
            WriteTarget(table="gold.daily", mode=WriteMode.CREATE),
        )


def test_replace_of_a_missing_table_is_not_found_at_submission(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """`replace`/`append` need an existing managed table."""
    with pytest.raises(NotFoundError):
        QueryService(session, AllowAllAuthorization()).submit_write(
            principal,
            "dev",
            "SELECT 1 AS id",
            WriteTarget(table="gold.nope", mode=WriteMode.REPLACE),
        )


def test_a_view_can_not_replace_a_table(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """Tables and views share a name space; `mode: view` never overwrites a table."""
    with pytest.raises(ValidationError, match="is a table"):
        QueryService(session, AllowAllAuthorization()).submit_write(
            principal, "dev", "SELECT 1 AS id", WriteTarget(table="gold.daily", mode=WriteMode.VIEW)
        )


def test_submit_write_into_a_nonexistent_environment_is_not_found(
    session: Session, principal: PrincipalRef
) -> None:
    """`_find_environment` refuses an unknown environment before anything else runs."""
    with pytest.raises(NotFoundError):
        QueryService(session, AllowAllAuthorization()).submit_write(
            principal,
            "nope",
            "SELECT 1 AS id",
            WriteTarget(table="gold.daily", mode=WriteMode.CREATE),
        )


def test_view_mode_over_an_existing_view_authorizes_write_table_not_create(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """Replacing an existing view needs WRITE_TABLE, the same as replacing a table."""
    namespace = session.execute(select(Namespace).where(Namespace.path == "gold")).scalar_one()
    session.add(
        Table(id=uuid.uuid4(), namespace_id=namespace.id, name="v", table_type=TableType.VIEW)
    )
    session.commit()

    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal, "dev", "SELECT 1 AS id", WriteTarget(table="gold.v", mode=WriteMode.VIEW)
    )

    assert run.write_mode is WriteMode.VIEW
    assert run.target_table_id is not None


def test_replace_of_a_view_is_not_a_managed_table(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """`replace`/`append` need an `ICEBERG` target, not a view."""
    namespace = session.execute(select(Namespace).where(Namespace.path == "gold")).scalar_one()
    session.add(
        Table(id=uuid.uuid4(), namespace_id=namespace.id, name="v", table_type=TableType.VIEW)
    )
    session.commit()

    with pytest.raises(ValidationError, match="not a managed table"):
        QueryService(session, AllowAllAuthorization()).submit_write(
            principal, "dev", "SELECT 1 AS id", WriteTarget(table="gold.v", mode=WriteMode.REPLACE)
        )


def test_create_into_a_nonexistent_namespace_is_not_found(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """A `create` target's namespace must already exist."""
    with pytest.raises(NotFoundError):
        QueryService(session, AllowAllAuthorization()).submit_write(
            principal,
            "dev",
            "SELECT 1 AS id",
            WriteTarget(table="nope.daily", mode=WriteMode.CREATE),
        )


def test_view_mode_creates_a_new_view_target(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """`mode: view` over a name that doesn't exist yet queues a new-view run."""
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal, "dev", "SELECT 1 AS id", WriteTarget(table="gold.v", mode=WriteMode.VIEW)
    )

    assert run.write_mode is WriteMode.VIEW
    assert run.target_table_id is None


def test_submit_maintenance_queues_a_maintain_run(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """Maintenance is a `QueryRun` with `write_mode = maintain`."""
    run = QueryService(session, AllowAllAuthorization()).submit_maintenance(
        principal, "dev", "gold.daily", keep_days=3, keep_last=2, dry_run=True, older_than_hours=5
    )

    assert run.write_mode is WriteMode.MAINTAIN
    assert run.status == QueryRunStatus.QUEUED
    assert run.target_name == "gold.daily"
    assert run.target_table_id is not None
    assert run.maintenance == {
        "request": {"keep_days": 3, "keep_last": 2, "dry_run": True, "older_than_hours": 5}
    }


@pytest.mark.parametrize("missing", [Action.MAINTAIN_TABLE, Action.SUBMIT_QUERY])
def test_submit_maintenance_needs_maintain_table_and_submit_query(
    session: Session, principal: PrincipalRef, seeded: CatalogService, missing: Action
) -> None:
    """MAINTAIN_TABLE on the table, and SUBMIT_QUERY to poll the run."""
    allowed = [action for action in Action if action is not missing]

    with pytest.raises(PermissionDenied, match=missing.value):
        QueryService(session, ActionAllowList(*allowed)).submit_maintenance(
            principal, "dev", "gold.daily"
        )


def test_submit_maintenance_validates_its_request(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """Out-of-range keep values and unknown tables are refused at submission."""
    service = QueryService(session, AllowAllAuthorization())

    with pytest.raises(ValidationError):
        service.submit_maintenance(principal, "dev", "gold.daily", keep_last=0)
    with pytest.raises(NotFoundError):
        service.submit_maintenance(principal, "dev", "gold.nope")


def test_submit_maintenance_rejects_an_older_than_hours_below_one(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """The orphan-age floor is enforced at submission too."""
    with pytest.raises(ValidationError, match="older_than_hours"):
        QueryService(session, AllowAllAuthorization()).submit_maintenance(
            principal, "dev", "gold.daily", older_than_hours=0
        )


def test_submit_maintenance_rejects_an_older_than_hours_above_the_cap(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    """The orphan-age cap (ten years) is enforced at submission too."""
    with pytest.raises(ValidationError, match="at most 87600"):
        QueryService(session, AllowAllAuthorization()).submit_maintenance(
            principal, "dev", "gold.daily", older_than_hours=87601
        )


def test_write_and_maintenance_runs_are_stamped_with_a_bound_token_task_run(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    task = python_task_run(session, principal.id)
    scope = TaskScope(
        environment="dev",
        asset_scope=[],
        token_id=uuid.uuid4(),
        bound=BoundTask(task.id, 1, "gold.daily", (), ()),
    )
    bound = dataclasses.replace(principal, task_scope=scope)
    service = QueryService(session, AllowAllAuthorization())
    write = service.submit_write(
        bound, "dev", "SELECT 1 AS id", WriteTarget(table="gold.daily", mode=WriteMode.APPEND)
    )
    maintenance = service.submit_maintenance(bound, "dev", "gold.daily")
    assert write.task_run_id == task.id
    assert maintenance.task_run_id == task.id
    plain = service.submit_maintenance(principal, "dev", "gold.daily")
    assert plain.task_run_id is None


@pytest.mark.parametrize(
    "mode",
    [WriteMode.CREATE, WriteMode.REPLACE, WriteMode.APPEND, WriteMode.VIEW],
)
def test_a_sql_write_into_an_artifact_is_refused_in_every_mode(
    session: Session, principal: PrincipalRef, seeded: CatalogService, mode: WriteMode
) -> None:
    row = session.scalars(select(Table).where(Table.name == "daily")).one()
    row.table_type = TableType.ARTIFACT
    row.metadata_location = None
    session.commit()
    with pytest.raises(ValidationError) as caught:
        QueryService(session, AllowAllAuthorization()).submit_write(
            principal, "dev", "SELECT 1 AS id", WriteTarget(table="gold.daily", mode=mode)
        )
    assert str(caught.value) == (
        "gold.daily is an artifact; a SQL write applies to tables. See `lakana artifact --help`."
    )


def test_maintenance_of_an_artifact_is_refused_at_submission(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    row = session.scalars(select(Table).where(Table.name == "daily")).one()
    row.table_type = TableType.ARTIFACT
    row.metadata_location = None
    session.commit()
    with pytest.raises(ValidationError) as caught:
        QueryService(session, AllowAllAuthorization()).submit_maintenance(
            principal, "dev", "gold.daily"
        )
    assert str(caught.value) == (
        "gold.daily is an artifact; maintenance applies to tables. See `lakana artifact --help`."
    )


_MERGE_REFUSAL = (
    "merge is written by assets (`write: merge`) and by `client.tables.merge`; a query writes "
    "with create, replace or append"
)


def test_a_query_cannot_merge_and_is_told_what_does(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    with pytest.raises(ValidationError) as caught:
        QueryService(session, AllowAllAuthorization()).submit_write(
            principal,
            "dev",
            "SELECT 1 AS id",
            WriteTarget(table="gold.daily", mode=WriteMode.MERGE),
        )
    assert str(caught.value) == _MERGE_REFUSAL


def test_a_query_cannot_write_history_and_is_told_what_does(
    session: Session, principal: PrincipalRef, seeded: CatalogService
) -> None:
    with pytest.raises(ValidationError) as caught:
        QueryService(session, AllowAllAuthorization()).submit_write(
            principal,
            "dev",
            "SELECT 1 AS id",
            WriteTarget(table="gold.daily", mode=WriteMode.HISTORY),
        )
    assert str(caught.value) == (
        "history is written by assets (`write: history`); a query writes with create, replace or "
        "append"
    )
