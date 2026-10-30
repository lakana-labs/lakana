"""Row filters and column masks that a write's schema change leaves out of date (#376)."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pytest
from core_test_helpers import python_task_run
from pyiceberg.schema import Schema
from pyiceberg.types import IcebergType, IntegerType, LongType, NestedField, StringType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, RunContext
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    ColumnMask,
    Environment,
    QueryRun,
    QueryRunStatus,
    RowFilter,
    Table,
    TaskRun,
    WriteMode,
)
from lakana_core.policy.schema_change import (
    COLUMNS_CHANGED_NOTE,
    BrokenPolicy,
    broken_policies,
    changed_columns,
    changed_tables,
    schema_change_for_reader,
)
from lakana_core.storage.resolver import StorageResolver

_TABLE = "gold.orders"


def _schema(*fields: tuple[int, str, IcebergType]) -> Schema:
    """Build a schema from (field id, name, type) triples.

    Parameters
    ----------
    *fields
        The fields.

    Returns
    -------
    Schema
        The schema.
    """
    return Schema(*(NestedField(i, n, t, required=False) for i, n, t in fields))


def test_a_dropped_and_a_replaced_column_are_named_and_a_widened_one_is_not() -> None:
    before = _schema((1, "id", IntegerType()), (2, "Region", StringType()), (3, "n", LongType()))
    after = _schema((1, "id", LongType()), (4, "region", LongType()))

    assert changed_columns(before, after) == {"region": "replaced", "n": "removed"}


def _catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Create `dev` at `tmp_path` with `gold.orders (id long, region string)`.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The environment's storage root.

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
    env = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    env.is_protected = False
    session.commit()
    _write(catalog, principal, WriteMode.CREATE, _rows(region=pa.array(["eu"])))
    return catalog


def _rows(**columns: pa.Array) -> pa.Table:
    """Build a result with an `id` column and the given others.

    Parameters
    ----------
    **columns
        More columns.

    Returns
    -------
    pa.Table
        One row.
    """
    return pa.table({"id": pa.array([1], pa.int64()), **columns})


def _write(
    catalog: CatalogService,
    principal: PrincipalRef,
    mode: WriteMode,
    rows: pa.Table,
    *,
    evolve_schema: bool = False,
) -> None:
    """Write `rows` into `gold.orders`.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The writer.
    mode
        The write mode.
    rows
        The rows.
    evolve_schema
        Passed through.
    """
    catalog.write_batches(
        principal,
        TableRef.from_dotted("dev", _TABLE),
        mode=mode,
        schema=rows.schema,
        batches=rows.to_batches(),
        evolve_schema=evolve_schema,
    )


def _policy(
    session: Session,
    principal: PrincipalRef,
    model: type[RowFilter] | type[ColumnMask],
    expression: str,
    *,
    column: str | None = None,
    revoked: bool = False,
) -> uuid.UUID:
    """Store an active (or revoked) row filter or column mask on `gold.orders`.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The subject and creator.
    model
        `RowFilter` or `ColumnMask`.
    expression
        The expression.
    column
        A mask's column.
    revoked
        Whether it is revoked.

    Returns
    -------
    uuid.UUID
        The policy's id.
    """
    table = session.scalars(select(Table).where(Table.name == "orders")).one()
    now = datetime.now(UTC)
    extra = {} if column is None else {"column": column}
    row = model(
        id=uuid.uuid4(),
        principal_id=principal.id,
        environment="dev",
        namespace="gold",
        table="orders",
        environment_id=session.scalars(select(Environment.id)).one(),
        table_id=table.id,
        expression=expression,
        created_by=principal.id,
        created_at=now,
        revoked_at=now if revoked else None,
        revoked_by=principal.id if revoked else None,
        **extra,
    )
    session.add(row)
    session.commit()
    return row.id


def _python_task(session: Session, principal: PrincipalRef) -> tuple[PrincipalRef, uuid.UUID]:
    """Insert a running Python task of `gold.orders` and bind the principal to it.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.

    Returns
    -------
    tuple of (PrincipalRef, uuid.UUID)
        The principal as a bound task token authenticates, and the task run's id.
    """
    task = python_task_run(session, principal.id, asset=_TABLE)
    bound = RunContext(task_run_id=task.id, claim=1)
    return dataclasses.replace(principal, run_context=bound), task.id


def _sql_task(session: Session, principal: PrincipalRef) -> tuple[PrincipalRef, uuid.UUID]:
    """Insert a running SQL task of `gold.orders` with its write run, as the worker does.

    Parameters
    ----------
    session
        Database session.
    principal
        The run principal.

    Returns
    -------
    tuple of (PrincipalRef, uuid.UUID)
        The principal with the query-run form of the run context, and the task run's id.
    """
    task = python_task_run(session, principal.id, asset=_TABLE, kind="sql")
    query = QueryRun(
        id=uuid.uuid4(),
        environment_id=session.scalars(select(Environment.id)).one(),
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.RUNNING,
        task_run_id=task.id,
    )
    session.add(query)
    session.flush()
    task.query_run_id = query.id
    session.commit()
    context = RunContext(task_run_id=task.id, query_run_id=query.id)
    return dataclasses.replace(principal, run_context=context), task.id


def _stored(session: Session, task_run_id: uuid.UUID) -> dict[str, object] | None:
    """Read a task run's stored schema change.

    Parameters
    ----------
    session
        Database session.
    task_run_id
        The task run.

    Returns
    -------
    dict or None
        What the column holds.
    """
    session.expire_all()
    return session.scalar(select(TaskRun.schema_change_json).where(TaskRun.id == task_run_id))


def _last_write_event(session: Session) -> dict[str, object]:
    """Read the detail of the newest `TABLE_WRITE` event.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    dict
        Its detail.
    """
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.TABLE_WRITE)
    ).all()
    return dict(events[-1].detail or {})


@pytest.mark.parametrize("form", ["python", "sql"])
def test_a_task_that_drops_a_policys_column_records_a_warning_and_the_write_lands(
    session: Session, principal: PrincipalRef, tmp_path: Path, form: str
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    row_filter = _policy(session, principal, RowFilter, "region = 'eu'")
    mask = _policy(session, principal, ColumnMask, "'***'", column="region")
    _policy(session, principal, ColumnMask, "NULL", column="id")
    _policy(session, principal, RowFilter, "region <> 'us'", revoked=True)
    p, task_run_id = (_python_task if form == "python" else _sql_task)(session, principal)

    _write(catalog, p, WriteMode.REPLACE, _rows(), evolve_schema=True)

    table = catalog.load_table(principal, TableRef.from_dotted("dev", _TABLE)).metadata
    assert [f.name for f in table.schema().fields] == ["id"]
    expected = [
        {
            "table": _TABLE,
            "kind": "column_mask",
            "policy_id": str(mask),
            "column": "region",
            "change": "removed",
        },
        {
            "table": _TABLE,
            "kind": "row_filter",
            "policy_id": str(row_filter),
            "column": "region",
            "change": "removed",
        },
    ]
    assert _stored(session, task_run_id) == {"tables": [_TABLE], "policies": expected}
    assert _last_write_event(session)["broken_policies"] == expected


def test_a_retyped_column_is_replaced_and_a_second_write_adds_only_what_is_new(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    row_filter = _policy(session, principal, RowFilter, "\"REGION\" = 'eu' OR id > 3")
    p, task_run_id = _python_task(session, principal)

    _write(catalog, p, WriteMode.REPLACE, _rows(region=pa.array([7])), evolve_schema=True)
    _write(catalog, p, WriteMode.REPLACE, _rows(region=pa.array(["x"])), evolve_schema=True)

    assert _stored(session, task_run_id) == {
        "tables": [_TABLE],
        "policies": [
            {
                "table": _TABLE,
                "kind": "row_filter",
                "policy_id": str(row_filter),
                "column": "REGION",
                "change": "replaced",
            }
        ],
    }


def test_a_task_that_drops_a_column_no_policy_uses_records_the_change_alone(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    _policy(session, principal, ColumnMask, "NULL", column="id")
    p, task_run_id = _python_task(session, principal)

    _write(catalog, p, WriteMode.REPLACE, _rows(), evolve_schema=True)

    assert _stored(session, task_run_id) == {"tables": [_TABLE], "policies": []}
    assert "broken_policies" not in _last_write_event(session)


def test_an_added_or_unchanged_column_and_a_write_outside_a_run_record_no_task_warning(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog(session, principal, tmp_path)
    row_filter = _policy(session, principal, RowFilter, "region = 'eu'")
    p, task_run_id = _python_task(session, principal)

    _write(
        catalog,
        p,
        WriteMode.REPLACE,
        _rows(region=pa.array(["eu"]), extra=pa.array([1])),
        evolve_schema=True,
    )
    assert _stored(session, task_run_id) is None
    assert "broken_policies" not in _last_write_event(session)

    _write(catalog, principal, WriteMode.REPLACE, _rows(), evolve_schema=True)
    assert _stored(session, task_run_id) is None
    assert _last_write_event(session)["broken_policies"] == [
        {
            "table": _TABLE,
            "kind": "row_filter",
            "policy_id": str(row_filter),
            "column": "region",
            "change": "removed",
        }
    ]


def test_a_mask_whose_stored_expression_does_not_parse_still_counts_its_own_column(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _catalog(session, principal, tmp_path)
    table = session.scalars(select(Table)).one()
    mask = _policy(session, principal, ColumnMask, "((", column="region")
    _policy(session, principal, RowFilter, "((")
    found = broken_policies(session, table.id, _TABLE, {"region": "removed"})

    assert found == (BrokenPolicy(_TABLE, "column_mask", mask, "region", "removed"),)


_MASK = BrokenPolicy(_TABLE, "column_mask", uuid.UUID(int=7), "ssn", "replaced")
_FILTER = BrokenPolicy("gold.other", "row_filter", uuid.UUID(int=8), "region", "removed")


def test_a_reader_who_manages_no_policies_sees_one_neutral_line_whatever_the_policies() -> None:
    with_policies = {"tables": [_TABLE], "policies": [_MASK.to_json(), _FILTER.to_json()]}
    without = {"tables": [_TABLE], "policies": []}

    shown = schema_change_for_reader(with_policies, manages_policies=False)

    assert shown == (COLUMNS_CHANGED_NOTE, ())
    assert shown == schema_change_for_reader(without, manages_policies=False)
    assert COLUMNS_CHANGED_NOTE == (
        "This run changed the table's columns; an administrator has been told what to check."
    )
    assert schema_change_for_reader(None, manages_policies=False) == (None, ())
    assert schema_change_for_reader({"tables": []}, manages_policies=True) == (None, ())


def test_a_policy_manager_sees_every_warning_with_the_id_and_the_revoke_command() -> None:
    note, shown = schema_change_for_reader(
        {
            "tables": [_TABLE],
            "policies": [
                _MASK.to_json(),
                _FILTER.to_json(),
                {"kind": "unknown"},
                "junk",
                {**_MASK.to_json(), "kind": "grant"},
                {**_MASK.to_json(), "policy_id": "not an id"},
            ],
        },
        manages_policies=True,
    )

    assert note == COLUMNS_CHANGED_NOTE
    assert [(w.kind, w.column, w.change, w.policy_id) for w in shown] == [
        ("column_mask", "ssn", "replaced", uuid.UUID(int=7)),
        ("row_filter", "region", "removed", uuid.UUID(int=8)),
    ]
    assert shown[1].message == (
        f"Row filter {uuid.UUID(int=8)} on `gold.other` uses column `region`, which this run "
        "removed from the table. Reads of the table by the people the filter applies to are "
        "refused until it is created again. Next: create the filter again for the column it is "
        f"meant for, then revoke this one (`lakana row-filter revoke {uuid.UUID(int=8)}`), in "
        "that order: revoking it first shows those people every row until the new one exists."
    )
    assert "real values" in shown[0].message


def test_the_changed_tables_are_read_from_what_is_stored_and_junk_is_ignored() -> None:
    assert changed_tables({"tables": ["gold.a", 7, "gold.b"]}) == ("gold.a", "gold.b")
    assert changed_tables(["gold.a"]) == ()
    assert changed_tables({"tables": "gold.a"}) == ()
    assert changed_tables(None) == ()
