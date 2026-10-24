"""Model additions: write-run columns, views, lineage edges, the new action and events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import Action
from lakana_core.errors import ValidationError
from lakana_core.grants.authorization import TASK_TOKEN_ACTIONS
from lakana_core.grants.roles import ROLE_ACTIONS, Role
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEventType,
    Environment,
    LineageEdge,
    Namespace,
    QueryRun,
    QueryRunStatus,
    Table,
    TableType,
    WriteMode,
)


def _environment(session: Session) -> Environment:
    """Insert a bare `dev` environment.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    Environment
        The inserted row.
    """
    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/dev",
        storage_options={},
        properties={},
    )
    session.add(env)
    session.commit()
    return env


def test_query_run_round_trips_every_write_field(session: Session) -> None:
    """A write run's columns persist, including a 64-bit snapshot id."""
    env = _environment(session)
    table_id = uuid.uuid4()
    run = QueryRun(
        id=uuid.uuid4(),
        environment_id=env.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.QUEUED,
        write_mode=WriteMode.APPEND,
        target_name="gold.daily",
        target_table_id=table_id,
        incremental_key="ts",
        evolve_schema=False,
        watermark="long:42",
        rows_written=3,
        snapshot_id=2**62 + 7,
        schema_diff={"added": [{"name": "x", "type": "long"}]},
        maintenance={"request": {"keep_days": 7}},
    )
    session.add(run)
    session.commit()
    session.expire_all()

    loaded = session.get(QueryRun, run.id)
    assert loaded is not None
    assert loaded.write_mode is WriteMode.APPEND
    assert loaded.target_name == "gold.daily"
    assert loaded.target_table_id == table_id
    assert loaded.incremental_key == "ts"
    assert loaded.evolve_schema is False
    assert loaded.watermark == "long:42"
    assert loaded.rows_written == 3
    assert loaded.snapshot_id == 2**62 + 7
    assert loaded.schema_diff == {"added": [{"name": "x", "type": "long"}]}
    assert loaded.maintenance == {"request": {"keep_days": 7}}


def test_a_read_run_leaves_every_write_field_null(session: Session) -> None:
    """A plain query run is unchanged: every M5a column defaults to NULL."""
    env = _environment(session)
    run = QueryRun(
        id=uuid.uuid4(), environment_id=env.id, sql_text="SELECT 1", status=QueryRunStatus.QUEUED
    )
    session.add(run)
    session.commit()
    session.expire_all()

    loaded = session.get(QueryRun, run.id)
    assert loaded is not None
    assert loaded.write_mode is None
    assert loaded.target_name is None
    assert loaded.rows_written is None
    assert loaded.snapshot_id is None
    assert loaded.maintenance is None


def test_write_mode_values_are_the_api_spelling() -> None:
    """The REST API speaks lowercase modes; the enum's values are exactly those."""
    assert [mode.value for mode in WriteMode] == [
        "create",
        "replace",
        "append",
        "merge",
        "view",
        "maintain",
    ]


def test_a_view_row_round_trips_its_definition_and_reports_its_kind(session: Session) -> None:
    """A view is a `tables` row with `table_type=VIEW` and a JSON definition."""
    env = _environment(session)
    ns = Namespace(id=uuid.uuid4(), environment_id=env.id, path="gold", properties={})
    session.add(ns)
    definition = {
        "sql": "SELECT 1 AS x",
        "references": [],
        "columns": [{"name": "x", "type": "int", "nullable": True}],
    }
    view = Table(
        id=uuid.uuid4(),
        namespace_id=ns.id,
        name="v",
        table_type=TableType.VIEW,
        view_definition=definition,
    )
    session.add(view)
    session.commit()
    session.expire_all()

    loaded = session.get(Table, view.id)
    assert loaded is not None
    assert loaded.view_definition == definition
    assert loaded.metadata_location is None
    assert loaded.kind == "VIEW"


@pytest.mark.parametrize(
    ("table_type", "kind"),
    [(TableType.ICEBERG, "TABLE"), (TableType.EXTERNAL, "EXTERNAL"), (TableType.VIEW, "VIEW")],
)
def test_kind_maps_every_table_type(table_type: TableType, kind: str) -> None:
    """`Table.kind` is the API's spelling of `table_type`."""
    assert Table(table_type=table_type).kind == kind


def test_a_lineage_edge_outlives_the_tables_it_names(session: Session) -> None:
    """Edges carry table ids without foreign keys, so they survive a drop."""
    env = _environment(session)
    edge = LineageEdge(
        id=uuid.uuid4(),
        environment_id=env.id,
        source_table_id=uuid.uuid4(),
        source_name="bronze.orders",
        source_snapshot_id=11,
        target_table_id=uuid.uuid4(),
        target_name="gold.daily",
        target_snapshot_id=2**62,
        query_run_id=None,
        created_at=datetime.now(UTC),
    )
    session.add(edge)
    session.commit()
    session.expire_all()

    loaded = session.get(LineageEdge, edge.id)
    assert loaded is not None
    assert loaded.source_name == "bronze.orders"
    assert loaded.target_snapshot_id == 2**62


def test_admin_gets_maintain_table_and_no_other_role_does() -> None:
    """The built-in admin role gets `MAINTAIN_TABLE`; nothing else changes."""
    assert Action.MAINTAIN_TABLE in ROLE_ACTIONS[Role.ADMIN]
    for role in (Role.VIEWER, Role.ANALYST, Role.DATA_SCIENTIST, Role.DATA_ENGINEER):
        assert Action.MAINTAIN_TABLE not in ROLE_ACTIONS[role]


def test_task_tokens_may_not_maintain_tables() -> None:
    """`MAINTAIN_TABLE` is deliberately not a task-token action."""
    assert Action.MAINTAIN_TABLE not in TASK_TOKEN_ACTIONS


def test_the_m5a_audit_event_types_exist() -> None:
    """Writes and maintenance each have an event type."""
    assert AuditEventType.TABLE_WRITE.value == "TABLE_WRITE"
    assert AuditEventType.TABLE_MAINTAINED.value == "TABLE_MAINTAINED"


def test_table_ref_from_dotted_splits_off_the_last_component() -> None:
    """The last dotted component is the table; the rest is the namespace."""
    ref = TableRef.from_dotted("dev", "gold.sales.daily")

    assert ref == TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold", "sales"]), name="daily"
    )
    assert ref.dotted() == "gold.sales.daily"


@pytest.mark.parametrize("dotted", ["daily", "gold.", ".daily", "gold..daily"])
def test_table_ref_from_dotted_refuses_a_name_without_a_namespace(dotted: str) -> None:
    """A bare or malformed dotted name is refused, never guessed."""
    with pytest.raises(ValidationError):
        TableRef.from_dotted("dev", dotted)
