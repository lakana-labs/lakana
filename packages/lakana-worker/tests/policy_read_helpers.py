"""Set up a table with grants, row filters and column masks, and read it through the worker.

Shared by the tests of a read bound to a check result (`test_pipeline_check_rows.py`) and of a
preview (`test_pipeline_preview.py`): a real catalog, real files and real grants, and the query
runs in a query child.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.ipc
import pytest
from pyiceberg.schema import Schema
from pyiceberg.table.update import AddSchemaUpdate, SetCurrentSchemaUpdate
from pyiceberg.types import NestedField
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.audit.content import stored_query_text
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog.preview import submit_preview
from lakana_core.catalog.query_service import QueryService, WriteTarget
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    TableRead,
    WriteMode,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.pipeline import run_one

ORDERS = TableRef.from_dotted("dev", "gold.orders")
"""The table the helpers set policies on: `gold.orders` of `dev`."""


@pytest.fixture
def admin(session: Session, tmp_path: Path) -> PrincipalRef:
    """Provide the principal that sets the world up and runs the pipeline, and `dev`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage root.

    Returns
    -------
    PrincipalRef
        The administrator.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    ref = PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)
    catalog(session).create_environment(
        ref, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})
    )
    return ref


def catalog(session: Session) -> CatalogService:
    """Return a catalog that allows everything, for setting the world up.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return CatalogService(session, StorageResolver(session), AllowAllAuthorization())


def orders(ids: list[int], regions: list[str], **more: list[str]) -> pa.Table:
    """Build rows of orders.

    Parameters
    ----------
    ids
        The `id` values.
    regions
        The `region` values.
    **more
        Further string columns by name.

    Returns
    -------
    pa.Table
        The rows, with an `email` column derived from the id.
    """
    return pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "region": pa.array(regions, pa.string()),
            "email": pa.array([f"o{i}@example.org" for i in ids], pa.string()),
        }
        | {name: pa.array(values, pa.string()) for name, values in more.items()}
    )


def write(
    session: Session,
    admin: PrincipalRef,
    rows: pa.Table,
    mode: WriteMode,
    *,
    ref: TableRef = ORDERS,
    evolve_schema: bool = False,
) -> int:
    """Write rows into a table and return the snapshot the write made.

    Parameters
    ----------
    session
        Database session.
    admin
        The writer.
    rows
        The rows.
    mode
        The write mode.
    ref
        The target.
    evolve_schema
        Let the table adopt the rows' schema.

    Returns
    -------
    int
        The id of the new snapshot.
    """
    written = catalog(session).write_batches(
        admin,
        ref,
        mode=mode,
        schema=rows.schema,
        batches=rows.to_batches(),
        evolve_schema=evolve_schema,
    )
    assert written.snapshot_id is not None
    return written.snapshot_id


def reader(session: Session, admin: PrincipalRef, name: str, *tables: str) -> PrincipalRef:
    """Create a person who may read the named tables of `gold` and nothing else.

    Parameters
    ----------
    session
        Database session.
    admin
        The granting principal.
    name
        The person's name.
    *tables
        The tables of `gold` to grant `READ_TABLE` on.

    Returns
    -------
    PrincipalRef
        The person.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name=name))
    session.commit()
    for table in tables:
        grant_read(session, admin, name, table)
    return PrincipalRef(id=principal_id, name=name, type=PrincipalType.USER)


def grant_read(
    session: Session, admin: PrincipalRef, name: str, table: str, *, namespace: str = "gold"
) -> None:
    """Grant a principal `READ_TABLE` on one table.

    Parameters
    ----------
    session
        Database session.
    admin
        The granting principal.
    name
        The grantee's name.
    table
        The table's name.
    namespace
        The table's namespace, in its unit-separator-joined storage form.
    """
    GrantService(session, AllowAllAuthorization()).grant(
        admin,
        principal=name,
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace=namespace,
        table=table,
    )


def run(
    session: Session, tmp_path: Path, query_run_id: uuid.UUID
) -> tuple[QueryRun, list[dict[str, object]]]:
    """Run a queued query under the real grants and return it with its rows.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The storage root, where results land.
    query_run_id
        The queued query run.

    Returns
    -------
    tuple
        The finished run and its rows, ordered by `id` (empty when it failed).
    """
    run_one(session, query_run_id, GrantAuthorization(session))
    finished = query_run(session, query_run_id)
    if finished.result_location is None:
        return finished, []
    with pyarrow.ipc.open_file(tmp_path / finished.result_location) as stream:
        rows = stream.read_all().to_pylist()
    return finished, sorted(rows, key=lambda row: row["id"])


def query_run(session: Session, query_run_id: uuid.UUID) -> QueryRun:
    """Read a query run afresh, after the worker's own transaction.

    Parameters
    ----------
    session
        Database session; rolled back first.
    query_run_id
        The query run.

    Returns
    -------
    QueryRun
        The run as stored.
    """
    session.rollback()
    return session.execute(
        select(QueryRun)
        .where(QueryRun.id == query_run_id)
        .execution_options(populate_existing=True)
    ).scalar_one()


def run_preview(
    session: Session, tmp_path: Path, p: PrincipalRef, dotted: str = "gold.orders"
) -> tuple[QueryRun, list[dict[str, object]]]:
    """Ask for the preview of a table of `dev` as a person, run it, and return its rows.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The storage root, where results land.
    p
        The person; they need `READ_TABLE` on the table and `SUBMIT_QUERY` on `dev`.
    dotted
        The table or view, as a dotted name.

    Returns
    -------
    tuple
        What `run` returns.
    """
    queued = submit_preview(
        session, GrantAuthorization(session), p, TableRef.from_dotted("dev", dotted)
    )
    return run(session, tmp_path, queued.id)


def events(
    session: Session, run_id: uuid.UUID, event_type: AuditEventType
) -> list[dict[str, object]]:
    """Read the details of the audit events of one type that a query run recorded.

    An audit event carries no query run id, so the events are matched by the run's submitter
    and its SQL text, which is unique among the runs of one test.

    Parameters
    ----------
    session
        Database session; rolled back first.
    run_id
        The query run.
    event_type
        The type of the events.

    Returns
    -------
    list of dict
        The detail of each matching event, oldest first.
    """
    finished = query_run(session, run_id)
    found = session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.event_type == event_type,
            AuditEvent.principal_id == finished.principal_id,
        )
        .order_by(AuditEvent.occurred_at, AuditEvent.id)
    )
    text = stored_query_text(finished.sql_text)
    return [event.detail for event in found if event.detail.get("sql_text") == text]


def filter(session: Session, admin: PrincipalRef, name: str, expression: str) -> None:
    """Give a reader a row filter on `gold.orders`.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that sets the filter.
    name
        The reader's name.
    expression
        The predicate.
    """
    RowFilterService(session, AllowAllAuthorization(), catalog(session)).create(
        admin,
        principal=name,
        environment="dev",
        namespace="gold",
        table="orders",
        expression=expression,
    )


def mask_as(session: Session, admin: PrincipalRef, name: str, column: str, expression: str) -> None:
    """Give a reader a column mask on `gold.orders` with an expression of its own.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that sets the mask.
    name
        The reader's name.
    column
        The masked column.
    expression
        What is shown in its place.
    """
    ColumnMaskService(session, AllowAllAuthorization(), catalog(session)).create(
        admin,
        principal=name,
        environment="dev",
        namespace="gold",
        table="orders",
        column=column,
        expression=expression,
    )


def filter_eu(session: Session, admin: PrincipalRef, name: str) -> None:
    """Give a reader the row filter ``region = 'eu'`` on `gold.orders`.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that sets the filter.
    name
        The reader's name.
    """
    filter(session, admin, name, "region = 'eu'")


def mask(session: Session, admin: PrincipalRef, name: str, column: str) -> uuid.UUID:
    """Give a reader a column mask on `gold.orders` that shows ``***``.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that sets the mask.
    name
        The reader's name.
    column
        The masked column; the table must have it now.

    Returns
    -------
    uuid.UUID
        The mask's id.
    """
    return (
        ColumnMaskService(session, AllowAllAuthorization(), catalog(session))
        .create(
            admin,
            principal=name,
            environment="dev",
            namespace="gold",
            table="orders",
            column=column,
            expression="'***'",
        )
        .id
    )


def rename_column(session: Session, admin: PrincipalRef, old: str, new: str) -> None:
    """Rename a column of `gold.orders` through a schema update, keeping its field id.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that commits the update.
    old
        The column's name now.
    new
        Its new name.
    """
    metadata = catalog(session).load_table(admin, ORDERS).metadata
    renamed = Schema(
        *(
            NestedField(
                field.field_id,
                new if field.name == old else field.name,
                field.field_type,
                required=field.required,
            )
            for field in metadata.schema().fields
        ),
        schema_id=metadata.current_schema_id + 1,
    )
    catalog(session).commit_table(
        admin,
        ORDERS,
        requirements=[],
        updates=[AddSchemaUpdate(schema_=renamed), SetCurrentSchemaUpdate(schema_id=-1)],
    )


def create_view(session: Session, admin: PrincipalRef, dotted: str, sql: str) -> None:
    """Create a view of `dev` through a `mode: view` run.

    Parameters
    ----------
    session
        Database session.
    admin
        The creator.
    dotted
        The view's dotted name.
    sql
        Its SELECT.
    """
    queued = QueryService(session, AllowAllAuthorization()).submit_write(
        admin, "dev", sql, WriteTarget(table=dotted, mode=WriteMode.VIEW)
    )
    run_one(session, queued.id, AllowAllAuthorization())
    assert query_run(session, queued.id).status is QueryRunStatus.SUCCEEDED


def drop_and_create(session: Session, admin: PrincipalRef, dotted: str) -> None:
    """Drop a table of `dev` and create another one under the same name, with one order.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that drops and creates it.
    dotted
        The table's dotted name.
    """
    ref = TableRef.from_dotted("dev", dotted)
    catalog(session).drop_table(admin, ref)
    write(session, admin, orders([1], ["eu"]), WriteMode.CREATE, ref=ref)


def drop_and_create_view(session: Session, admin: PrincipalRef, dotted: str, sql: str) -> None:
    """Drop a view of `dev` and create another one under the same name.

    Parameters
    ----------
    session
        Database session.
    admin
        The principal that drops and creates it.
    dotted
        The view's dotted name.
    sql
        The new view's SELECT.
    """
    catalog(session).drop_table(admin, TableRef.from_dotted("dev", dotted))
    create_view(session, admin, dotted, sql)


def reads_of(session: Session, principal_id: uuid.UUID, table_id: uuid.UUID) -> int:
    """Count the reads of a table by a principal that `table_reads` recorded.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The reader.
    table_id
        The table.

    Returns
    -------
    int
        The sum of the read counts of every day.
    """
    counted = session.scalar(
        select(func.sum(TableRead.read_count)).where(
            TableRead.principal_id == principal_id, TableRead.table_id == table_id
        )
    )
    return int(counted or 0)
