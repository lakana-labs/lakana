"""Tests for the QueryRun model."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from lakana_core.models import PrincipalType, QueryOrigin, QueryRun, QueryRunStatus


def test_a_query_run_round_trips_through_the_database(session: Session) -> None:
    """A row can be written with only its required fields and read back."""
    from lakana_core.models import Environment, Principal

    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana-test",
        storage_options={},
        is_protected=False,
        properties={},
    )
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    session.add_all([env, principal])
    session.commit()

    run = QueryRun(
        origin=QueryOrigin.WRITTEN,
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.QUEUED,
    )
    session.add(run)
    session.commit()

    fetched = session.get(QueryRun, run.id)
    assert fetched is not None
    assert fetched.status == QueryRunStatus.QUEUED
    assert fetched.sql_text == "SELECT 1"
    assert fetched.error_message is None
    assert fetched.lineage_json is None
    assert fetched.result_location is None
    assert fetched.started_at is None
    assert fetched.finished_at is None


def test_a_schema_from_the_models_records_a_run_without_an_origin_as_written(
    session: Session,
) -> None:
    """A row that names no origin is `written`, as in a database migrated by 0081.

    A server or worker of the previous release inserts such rows during an upgrade; the schema
    the tests build from the models must take them as the migrated one does.
    """
    from lakana_core.models import Environment

    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana-test",
        storage_options={},
        is_protected=False,
        properties={},
    )
    session.add(env)
    session.commit()
    run_id = uuid.uuid4()
    session.execute(
        text(
            "INSERT INTO query_runs (id, environment_id, sql_text, status, created_at,"
            " updated_at) VALUES (:id, :env, 'SELECT 1', 'QUEUED', :now, :now)"
        ),
        {"id": run_id.hex, "env": env.id.hex, "now": datetime.now(UTC).isoformat()},
    )
    session.commit()

    fetched = session.get(QueryRun, run_id)
    assert fetched is not None
    assert fetched.origin is QueryOrigin.WRITTEN


def test_a_query_run_survives_its_principal_being_deleted(session: Session) -> None:
    """`principal_id` is set NULL, not cascaded, so query history outlives its author."""
    from lakana_core.models import Environment, Principal

    env = Environment(
        id=uuid.uuid4(),
        name="dev",
        storage_root="file:///tmp/lakana-test",
        storage_options={},
        is_protected=False,
        properties={},
    )
    principal = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin")
    session.add_all([env, principal])
    session.commit()
    run = QueryRun(
        origin=QueryOrigin.WRITTEN,
        id=uuid.uuid4(),
        environment_id=env.id,
        principal_id=principal.id,
        sql_text="SELECT 1",
        status=QueryRunStatus.SUCCEEDED,
        lineage_json={"a": []},
        result_location="_artifacts/queries/x/result.arrow",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()

    session.delete(principal)
    session.commit()

    fetched = session.get(QueryRun, run.id)
    assert fetched is not None
    assert fetched.principal_id is None
