"""Postgres migration round trip: upgrade, downgrade, upgrade.

Every other integration test builds its schema with `Base.metadata.create_all`, so no
CI test had ever applied the Alembic migrations themselves to a real database. This test's
own history is specifically about revision `0011`'s (`75c7a7bb948a`) upgrade, backfill
and downgrade against Postgres 16 — its id-binding and `external_claim` backfills were
checked by hand rather than in CI. A round trip seeded only in `principals`, a table
`0011` never touches, would run `_backfill_ids`/`_backfill_external_claims` over zero
rows and prove nothing about the migration this issue exists to catch.

This instead seeds data at revision `0010` (`a3f7c2e9b4d1`, the schema immediately
before `0011`'s new columns exist) that mirrors the fixture in
`test_migrations.py::test_0011_binds_grants_and_policies_to_ids_and_revokes_stale_rows`
and `test_0011_backfills_external_claim_for_parseable_legacy_rows`: an environment, a
namespace, an `ICEBERG` table, a `TABLE`-kind grant on it (so `_backfill_ids` has
something to bind `environment_id`/`table_id` from), and an `EXTERNAL` table with a
parseable `location_pattern` (so `_backfill_external_claims` has something to set).
Upgrading to head, downgrading one step, and upgrading again must reproduce the same
backfilled values from the still-present source rows on real Postgres.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy
from core_test_helpers import assert_loads_as_pre_m5a, seed_m5a_rows
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

from lakana_core import migrate
from lakana_core.db import db_now
from lakana_core.runs.store import end_lost_runs

pytestmark = pytest.mark.integration

_REVISION_0010 = "a3f7c2e9b4d1"
_REVISION_0011 = "75c7a7bb948a"
_REVISION_0012 = "c8d4e2f1a6b3"
_REVISION_0013 = "d1e5f3a7b9c2"


def test_0011_backfill_round_trips_on_postgres() -> None:
    """0011's id bindings and external-claim backfill survive upgrade/downgrade/upgrade.

    Seeds, at revision 0010 (before the backfilled columns exist), a principal, an
    environment, a namespace, an `ICEBERG` table, an `EXTERNAL` table with a parseable
    `location_pattern`, and a `TABLE`-kind grant on the `ICEBERG` table. Upgrading to
    head must bind the grant's `environment_id`/`table_id` and set the external
    table's `external_claim`; downgrading one step must drop those columns without
    error; upgrading again must recompute the same values from the source rows, which
    the downgrade leaves untouched.
    """
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, _REVISION_0010)
        engine = sqlalchemy.create_engine(url)
        now = datetime.now(UTC)

        admin_id = uuid.uuid4()
        dev_id = uuid.uuid4()
        gold_id = uuid.uuid4()
        orders_id = uuid.uuid4()
        customers_id = uuid.uuid4()
        grant_id = uuid.uuid4()

        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO principals (id, type, name, is_active, created_at, updated_at) "
                    "VALUES (:id, 'USER', 'admin', true, :now, :now)"
                ),
                {"id": admin_id, "now": now},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO environments (id, name, storage_root, storage_options, "
                    "is_protected, properties, created_at, updated_at) "
                    "VALUES (:id, 'dev', 's3://bucket/dev', :options, false, :props, :now, :now)"
                ),
                {"id": dev_id, "options": json.dumps({}), "props": json.dumps({}), "now": now},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO namespaces (id, environment_id, path, properties, owner_id) "
                    "VALUES (:id, :environment_id, 'gold', :props, NULL)"
                ),
                {"id": gold_id, "environment_id": dev_id, "props": json.dumps({})},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at, "
                    "updated_at) VALUES (:id, :namespace_id, 'orders', 'ICEBERG', :now, :now)"
                ),
                {"id": orders_id, "namespace_id": gold_id, "now": now},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, external_config, "
                    "created_at, updated_at) "
                    "VALUES (:id, :namespace_id, 'customers', 'EXTERNAL', :config, :now, :now)"
                ),
                {
                    "id": customers_id,
                    "namespace_id": gold_id,
                    "config": json.dumps(
                        {
                            "location_pattern": "raw/customers/*.parquet",
                            "format": "parquet",
                            "schema": {"id": "int64"},
                        }
                    ),
                    "now": now,
                },
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO grants (id, principal_id, role, kind, environment, namespace, "
                    '"table", granted_by, granted_at) '
                    "VALUES (:id, :principal_id, 'VIEWER', 'TABLE', 'dev', 'gold', 'orders', "
                    ":principal_id, :now)"
                ),
                {"id": grant_id, "principal_id": admin_id, "now": now},
            )

        migrate.upgrade(url, _REVISION_0011)
        _assert_backfilled(engine, grant_id, dev_id, orders_id, customers_id)
        version_after_first_upgrade = _alembic_version(engine)

        migrate.downgrade(url, _REVISION_0010)
        migrate.upgrade(url, _REVISION_0011)
        _assert_backfilled(engine, grant_id, dev_id, orders_id, customers_id)
        version_after_second_upgrade = _alembic_version(engine)

        engine.dispose()

    assert version_after_first_upgrade == _REVISION_0011
    assert version_after_second_upgrade == _REVISION_0011


def _assert_backfilled(
    engine: sqlalchemy.Engine,
    grant_id: uuid.UUID,
    dev_id: uuid.UUID,
    orders_id: uuid.UUID,
    customers_id: uuid.UUID,
) -> None:
    """Assert revision 0011's backfilled columns hold the expected values.

    Parameters
    ----------
    engine
        Engine connected to the migrated database.
    grant_id
        Id of the seeded `TABLE`-kind grant.
    dev_id
        Id of the seeded `dev` environment; expected as the grant's `environment_id`.
    orders_id
        Id of the seeded `ICEBERG` table the grant targets; expected as the grant's
        `table_id`.
    customers_id
        Id of the seeded `EXTERNAL` table with a parseable `location_pattern`.

    Raises
    ------
    AssertionError
        If any backfilled column does not hold the expected value.
    """
    with engine.connect() as conn:
        environment_id, table_id = conn.execute(
            sqlalchemy.text("SELECT environment_id, table_id FROM grants WHERE id = :id"),
            {"id": grant_id},
        ).one()
        external_claim = conn.execute(
            sqlalchemy.text("SELECT external_claim FROM tables WHERE id = :id"),
            {"id": customers_id},
        ).scalar_one()
    assert environment_id == dev_id
    assert table_id == orders_id
    assert external_claim == "raw/customers"


def _alembic_version(engine: sqlalchemy.Engine) -> str:
    """Return the database's current `alembic_version.version_num`.

    Parameters
    ----------
    engine
        Engine connected to the migrated database.

    Returns
    -------
    str
        The current revision id.
    """
    with engine.connect() as conn:
        return conn.execute(sqlalchemy.text("SELECT version_num FROM alembic_version")).scalar_one()


def test_0012_downgrade_leaves_nothing_the_previous_release_cannot_load_on_postgres() -> None:
    """On Postgres: seeded rows of write runs, then `downgrade -1` from 0012.

    A `MAINTAIN_TABLE` grant, a view row, a lineage edge and a write run are
    seeded at head; afterwards the principal's grants and the tables load
    through older enums, and every remaining action is a older one.
    """
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, _REVISION_0012)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as connection:
            principal_id = seed_m5a_rows(connection)

        migrate.downgrade(url, "-1")

        with engine.connect() as connection:
            assert_loads_as_pre_m5a(connection, principal_id)
            assert not sqlalchemy.inspect(engine).has_table("lineage_edges")
        version = _alembic_version(engine)
        engine.dispose()

    assert version == _REVISION_0011


def test_0013_round_trips_on_postgres() -> None:
    """0013 upgrades, downgrades to 0012 and upgrades again on Postgres."""
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, _REVISION_0013)
        migrate.downgrade(url, _REVISION_0012)
        engine = sqlalchemy.create_engine(url)
        assert not sqlalchemy.inspect(engine).has_table("deploy_runs")
        assert _alembic_version(engine) == _REVISION_0012
        migrate.upgrade(url, _REVISION_0013)
        assert sqlalchemy.inspect(engine).has_table("deploy_runs")
        engine.dispose()


def test_0014_round_trips_on_postgres() -> None:
    """0014 round-trips with a seeded RUN grant; its one-active-run index is partial."""
    revision_0014 = "e7f2a9c4b1d3"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0014)
        engine = sqlalchemy.create_engine(url)
        indexes = {i["name"]: i for i in sqlalchemy.inspect(engine).get_indexes("runs")}
        one_active = indexes["ix_runs_one_active"]
        assert one_active["unique"]
        assert "status" in one_active["dialect_options"]["postgresql_where"]
        principal_id = uuid.uuid4()
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO principals (id, type, name, is_active, created_at, updated_at) "
                    "VALUES (:id, 'USER', 'admin', true, :now, :now)"
                ),
                {"id": principal_id, "now": now},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO grants (id, principal_id, action, kind, granted_by, granted_at) "
                    "VALUES (:id, :p, 'RUN', 'INSTANCE', :p, :now)"
                ),
                {"id": uuid.uuid4(), "p": principal_id, "now": now},
            )
        migrate.downgrade(url, _REVISION_0013)
        assert not sqlalchemy.inspect(engine).has_table("runs")
        assert _alembic_version(engine) == _REVISION_0013
        with engine.connect() as conn:
            actions = conn.execute(sqlalchemy.text("SELECT action FROM grants")).scalars().all()
        assert "RUN" not in actions
        migrate.upgrade(url, revision_0014)
        assert sqlalchemy.inspect(engine).has_table("task_attempts")
        engine.dispose()


def test_0015_round_trips_on_postgres() -> None:
    """0015 adds `task_runs.claim_count` seeded from `attempt`, downgrades and upgrades again."""
    revision_0015 = "f4b8d2a6c9e1"
    revision_0014 = "e7f2a9c4b1d3"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0014)
        engine = sqlalchemy.create_engine(url)

        def columns() -> set[str]:
            return {c["name"] for c in sqlalchemy.inspect(engine).get_columns("task_runs")}

        assert "claim_count" not in columns()
        with engine.begin() as conn:
            # The seeding is about this one row; its run is not needed, so the
            # foreign keys are switched off for the insert only.
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO task_runs (id, run_id, asset, status, attempt, max_attempts, "
                    "timeout_seconds, retry_delay_seconds, upstream_json, conflict_retried) "
                    "VALUES (:id, :run, 'silver.a', 'RUNNING', 3, 5, 60, 0, '[]', false)"
                ),
                {"id": uuid.uuid4(), "run": uuid.uuid4()},
            )
        migrate.upgrade(url, revision_0015)
        assert "claim_count" in columns()
        with engine.connect() as conn:
            rows = conn.execute(sqlalchemy.text("SELECT attempt, claim_count FROM task_runs")).all()
        assert [tuple(row) for row in rows] == [(3, 3)]
        migrate.downgrade(url, revision_0014)
        assert _alembic_version(engine) == revision_0014
        assert "claim_count" not in columns()
        migrate.upgrade(url, revision_0015)
        with engine.connect() as conn:
            rows = conn.execute(sqlalchemy.text("SELECT attempt, claim_count FROM task_runs")).all()
        assert [tuple(row) for row in rows] == [(3, 3)]
        engine.dispose()


def test_0016_round_trips_on_postgres() -> None:
    """0016 adds the bound-token columns; the downgrade deletes bound tokens; it upgrades again."""
    revision_0016 = "a6c3e9d1f7b2"
    revision_0015 = "f4b8d2a6c9e1"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0015)
        engine = sqlalchemy.create_engine(url)

        def columns() -> set[str]:
            return {c["name"] for c in sqlalchemy.inspect(engine).get_columns("task_tokens")}

        assert not {"task_run_id", "claim"} & columns()
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            for key in ("plain", "bound"):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO task_tokens (id, principal_id, environment, asset_scope, "
                        "key_id, secret_hash, issued_at, expires_at) "
                        "VALUES (:id, :p, 'dev', '[]', :key, :hash, :now, :now)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "p": uuid.uuid4(),
                        "key": key,
                        "hash": "0" * 64,
                        "now": now,
                    },
                )
        migrate.upgrade(url, revision_0016)
        assert {"task_run_id", "claim"} <= columns()
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "UPDATE task_tokens SET task_run_id = :t, claim = 1 WHERE key_id = 'bound'"
                ),
                {"t": uuid.uuid4()},
            )
        with (
            pytest.raises(DBAPIError, match="ck_task_tokens_bound_pair"),
            engine.begin() as conn,
        ):
            conn.execute(sqlalchemy.text("UPDATE task_tokens SET claim = 1 WHERE key_id = 'plain'"))
        migrate.downgrade(url, revision_0015)
        assert _alembic_version(engine) == revision_0015
        assert not {"task_run_id", "claim"} & columns()
        with engine.connect() as conn:
            keys = conn.execute(sqlalchemy.text("SELECT key_id FROM task_tokens")).scalars().all()
        assert keys == ["plain"]
        migrate.upgrade(url, revision_0016)
        assert {"task_run_id", "claim"} <= columns()
        engine.dispose()


def test_0018_round_trips_on_postgres() -> None:
    """0018 adds the three artifact tables; the downgrade deletes `ARTIFACT` rows of `tables`."""
    revision_0018 = "e2a7c4f1b6d8"
    revision_0017 = "c8e1a5d7f3b9"
    new = {"artifact_versions", "artifact_uploads", "artifact_blobs"}
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0017)
        engine = sqlalchemy.create_engine(url)
        assert not new & set(sqlalchemy.inspect(engine).get_table_names())
        migrate.upgrade(url, revision_0018)
        assert new <= set(sqlalchemy.inspect(engine).get_table_names())
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            for name, kind in (("model", "ARTIFACT"), ("events", "ICEBERG")):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO tables (id, namespace_id, name, table_type, created_at, "
                        "updated_at) VALUES (:id, :ns, :name, :kind, :now, :now)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "ns": uuid.uuid4(),
                        "name": name,
                        "kind": kind,
                        "now": now,
                    },
                )
        migrate.downgrade(url, revision_0017)
        assert _alembic_version(engine) == revision_0017
        assert not new & set(sqlalchemy.inspect(engine).get_table_names())
        with engine.connect() as conn:
            left = conn.execute(sqlalchemy.text("SELECT name FROM tables")).scalars().all()
        assert left == ["events"]
        migrate.upgrade(url, revision_0018)
        assert new <= set(sqlalchemy.inspect(engine).get_table_names())
        engine.dispose()


def test_0019_round_trips_on_postgres() -> None:
    """0019 backfills format 1 and the bare `env` key id; the downgrade deletes newer rows."""
    revision_0019 = "b5d9f2a8c4e7"
    revision_0018 = "e2a7c4f1b6d8"
    old_id, new_id = uuid.uuid4(), uuid.uuid4()
    insert_old = sqlalchemy.text(
        "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce, wrapped_data_key,"
        " created_by, created_at) VALUES (:id, :env, :name, :ct, :n, :w, :by, :now)"
    )
    insert_new = sqlalchemy.text(
        "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce, wrapped_data_key,"
        " created_by, created_at, format_version, key_id) VALUES"
        " (:id, :env, :name, :ct, :n, :w, :by, :now, 2, 'env:0123456789abcdef')"
    )

    def params(row_id: uuid.UUID) -> dict[str, object]:
        return {
            "id": row_id,
            "env": uuid.uuid4(),
            "name": str(row_id)[:8],
            "ct": b"ct",
            "n": b"n" * 12,
            "w": b"w",
            "by": uuid.uuid4(),
            "now": datetime.now(UTC),
        }

    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0018)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(insert_old, params(old_id))
        migrate.upgrade(url, revision_0019)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(insert_new, params(new_id))
            rows = conn.execute(
                sqlalchemy.text(
                    "SELECT id, format_version, key_id FROM secrets ORDER BY format_version"
                )
            ).all()
        assert [(r[0], r[1], r[2]) for r in rows] == [
            (old_id, 1, "env"),
            (new_id, 2, "env:0123456789abcdef"),
        ]
        migrate.downgrade(url, revision_0018)
        assert _alembic_version(engine) == revision_0018
        columns = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("secrets")}
        assert not {"format_version", "key_id"} & columns
        with engine.connect() as conn:
            left = conn.execute(sqlalchemy.text("SELECT id FROM secrets")).scalars().all()
        assert left == [old_id]
        migrate.upgrade(url, revision_0019)
        engine.dispose()


def test_0020_downgrade_drops_the_long_action_and_keeps_the_audit_rows_on_postgres() -> None:
    """Postgres enforces `VARCHAR(18)`: a 20-character action must be gone before it narrows."""
    revision_0019 = "b5d9f2a8c4e7"
    revision_0020 = "c3f8a6d1e9b4"
    grant = sqlalchemy.text(
        "INSERT INTO grants (id, principal_id, action, kind, granted_by, granted_at)"
        " VALUES (:id, :p, :a, 'INSTANCE', :p, :now)"
    )
    audit = sqlalchemy.text(
        "INSERT INTO audit_events (id, occurred_at, event_type, detail)"
        " VALUES (:id, :now, 'PACKAGE_INDEX_CREATED', '{}')"
    )
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0020)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            for action in ("MANAGE_PACKAGE_INDEX", "READ_AUDIT_LOG"):
                conn.execute(
                    grant,
                    {"id": uuid.uuid4(), "p": uuid.uuid4(), "a": action, "now": datetime.now(UTC)},
                )
            conn.execute(audit, {"id": uuid.uuid4(), "now": datetime.now(UTC)})
        migrate.downgrade(url, revision_0019)
        assert _alembic_version(engine) == revision_0019
        assert not sqlalchemy.inspect(engine).has_table("package_indexes")
        with engine.connect() as conn:
            actions = conn.execute(sqlalchemy.text("SELECT action FROM grants")).scalars().all()
            events = conn.execute(sqlalchemy.text("SELECT event_type FROM audit_events")).all()
        assert actions == ["READ_AUDIT_LOG"]
        assert [row[0] for row in events] == ["PACKAGE_INDEX_CREATED"]
        migrate.upgrade(url, revision_0020)
        engine.dispose()


def test_0021_round_trips_on_postgres() -> None:
    """0021 keeps the server default; its downgrade turns `CANCELLED` runs into `FAILED` ones."""
    revision_0020 = "c3f8a6d1e9b4"
    revision_0021 = "e4b7d2a9c6f1"
    old_id, queued_id, cancelled_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    without_column = sqlalchemy.text(
        "INSERT INTO query_runs (id, environment_id, sql_text, status, created_at, updated_at)"
        " VALUES (:id, :env, 'SELECT 1', :status, :now, :now)"
    )
    with_column = sqlalchemy.text(
        "INSERT INTO query_runs (id, environment_id, sql_text, status, cancel_requested,"
        " created_at, updated_at) VALUES (:id, :env, 'SELECT 1', :status, true, :now, :now)"
    )

    def params(run_id: uuid.UUID, status: str) -> dict[str, object]:
        return {"id": run_id, "env": uuid.uuid4(), "status": status, "now": datetime.now(UTC)}

    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0020)
        engine = sqlalchemy.create_engine(url)
        columns = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("query_runs")}
        assert "cancel_requested" not in columns
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(without_column, params(old_id, "SUCCEEDED"))
        migrate.upgrade(url, revision_0021)
        assert _alembic_version(engine) == revision_0021
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            # A server or worker of the release before this one inserts without the column
            # while an upgrade is under way: the server default fills it in.
            conn.execute(without_column, params(queued_id, "QUEUED"))
            conn.execute(with_column, params(cancelled_id, "CANCELLED"))
            flags = dict(
                conn.execute(sqlalchemy.text("SELECT id, cancel_requested FROM query_runs")).all()
            )
        assert flags == {old_id: False, queued_id: False, cancelled_id: True}
        migrate.downgrade(url, revision_0020)
        assert _alembic_version(engine) == revision_0020
        columns = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("query_runs")}
        assert "cancel_requested" not in columns
        with engine.connect() as conn:
            rows = {
                row[0]: tuple(row[1:])
                for row in conn.execute(
                    sqlalchemy.text("SELECT id, status, error_cause, error_message FROM query_runs")
                )
            }
        assert rows[cancelled_id] == ("FAILED", "cancelled", "The query was cancelled.")
        assert rows[old_id][0] == "SUCCEEDED"
        assert rows[queued_id][0] == "QUEUED"
        migrate.upgrade(url, revision_0021)
        engine.dispose()


def test_0022_round_trips_on_postgres() -> None:
    """0022 adds the table, the columns and both indexes, and the downgrade removes them."""
    revision_0022 = "f6c1a8d3b5e2"
    revision_0021 = "e4b7d2a9c6f1"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0021)
        engine = sqlalchemy.create_engine(url)
        migrate.upgrade(url, revision_0022)
        inspector = sqlalchemy.inspect(engine)
        assert {c["name"] for c in inspector.get_columns("workers")} == {
            "id",
            "host",
            "started_at",
            "version",
            "heartbeat_at",
        }
        for table in ("query_runs", "deploy_runs"):
            column = {c["name"]: c for c in inspector.get_columns(table)}["worker_id"]
            assert column["nullable"] is True
        indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("query_runs")}
        assert indexes["ix_query_runs_status_created_at"] == ["status", "created_at"]
        assert indexes["ix_query_runs_environment_id_finished_at"] == [
            "environment_id",
            "finished_at",
        ]
        migrate.downgrade(url, revision_0021)
        assert _alembic_version(engine) == revision_0021
        inspector = sqlalchemy.inspect(engine)
        assert not inspector.has_table("workers")
        for table in ("query_runs", "deploy_runs"):
            assert "worker_id" not in {c["name"] for c in inspector.get_columns(table)}
        assert not {
            "ix_query_runs_status_created_at",
            "ix_query_runs_environment_id_finished_at",
        } & {i["name"] for i in inspector.get_indexes("query_runs")}
        migrate.upgrade(url, revision_0022)
        assert _alembic_version(engine) == revision_0022
        engine.dispose()


def test_a_run_found_running_by_the_upgrade_is_ended_later_on_postgres() -> None:
    """A run left `RUNNING` before 0022 has no worker id: the lost-run rule ends it by its age.

    The rule is application code, which reads every column of the current schema, so this
    upgrades to the current head on purpose and asserts nothing about a revision.
    """
    revision_0021 = "e4b7d2a9c6f1"
    old_query, young_query, old_deploy = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    query = sqlalchemy.text(
        "INSERT INTO query_runs (id, environment_id, sql_text, status, started_at, created_at,"
        " updated_at) VALUES (:id, :env, 'SELECT 1', 'RUNNING', :started, :started, :started)"
    )
    deploy = sqlalchemy.text(
        "INSERT INTO deploy_runs (id, environment_id, repository_id, kind, status, created_at,"
        " started_at) VALUES (:id, :env, :repo, 'DEPLOY', 'RUNNING', :started, :started)"
    )
    now = datetime.now(UTC)
    old = now - timedelta(seconds=120)
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0021)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(query, {"id": old_query, "env": uuid.uuid4(), "started": old})
            conn.execute(query, {"id": young_query, "env": uuid.uuid4(), "started": now})
            conn.execute(
                deploy,
                {"id": old_deploy, "env": uuid.uuid4(), "repo": uuid.uuid4(), "started": old},
            )
        migrate.upgrade(url, "head")
        with Session(engine) as session:
            # The upgrade rule: a run without a worker id counts as held until it started a lease
            # ago, so the old ones are ended and the young one is not.
            assert end_lost_runs(session, db_now(session), 60) == 2
        with engine.connect() as conn:
            statuses = {
                str(row[0]): row[1:]
                for row in conn.execute(
                    sqlalchemy.text(
                        "SELECT id, status, error_cause FROM query_runs "
                        "UNION ALL SELECT id, status, NULL FROM deploy_runs"
                    )
                )
            }
        assert statuses[str(old_query)] == ("FAILED", "worker_lost")
        assert statuses[str(young_query)] == ("RUNNING", None)
        assert statuses[str(old_deploy)][0] == "FAILED"
        engine.dispose()


def test_0024_on_postgres() -> None:
    """0024 adds the checks' table and columns; rows written before it read as "no checks".

    A task run, a table, a query run and a deploy run written by the release before are
    there when the upgrade runs: the task run reads as an asset without checks and without a
    stamp, a baseline or a tip, the table as published. The downgrade drops all of it and
    keeps the rows.
    """
    revision_0023 = "a7d3c9e1f4b6"
    revision_0024 = "b8e2c5f7a1d4"
    task_id, table_id, query_id, deploy_id = (uuid.uuid4() for _ in range(4))
    now = datetime.now(UTC)
    added = {
        "deploy_runs": {"checks_json"},
        "query_runs": {"snapshot_before"},
        "tables": {"unpublished_run_id"},
        "task_runs": {
            "kind",
            "has_checks",
            "written_table_id",
            "written_snapshot_before",
            "written_snapshot_after",
            "written_publishes",
            "baseline_snapshot_id",
            "baseline_publishes",
            "own_snapshot_id",
        },
    }
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0023)
        engine = sqlalchemy.create_engine(url)
        inspector = sqlalchemy.inspect(engine)
        assert not inspector.has_table("check_results")
        for table, columns in added.items():
            assert not columns & {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO task_runs (id, run_id, asset, status, attempt, max_attempts, "
                    "timeout_seconds, retry_delay_seconds, upstream_json, conflict_retried) "
                    "VALUES (:id, :run, 'silver.x', 'SUCCEEDED', 1, 3, 60, 0, '[]', false)"
                ),
                {"id": task_id, "run": uuid.uuid4()},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at, "
                    "updated_at) VALUES (:id, :ns, 'x', 'ICEBERG', :now, :now)"
                ),
                {"id": table_id, "ns": uuid.uuid4(), "now": now},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO query_runs (id, environment_id, sql_text, status, created_at, "
                    "updated_at) VALUES (:id, :env, 'SELECT 1', 'SUCCEEDED', :now, :now)"
                ),
                {"id": query_id, "env": uuid.uuid4(), "now": now},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO deploy_runs (id, environment_id, repository_id, kind, status, "
                    "created_at) VALUES (:id, :env, :repo, 'DEPLOY', 'SUCCEEDED', :now)"
                ),
                {"id": deploy_id, "env": uuid.uuid4(), "repo": uuid.uuid4(), "now": now},
            )

        migrate.upgrade(url, revision_0024)
        assert _alembic_version(engine) == revision_0024
        inspector = sqlalchemy.inspect(engine)
        described = {
            (table, column["name"]): (
                type(column["type"]).__name__,
                column["nullable"],
                column["default"],
            )
            for table in added
            for column in inspector.get_columns(table)
            if column["name"] in added[table]
        }
        assert described == {
            ("deploy_runs", "checks_json"): ("JSON", True, None),
            ("query_runs", "snapshot_before"): ("BIGINT", True, None),
            ("tables", "unpublished_run_id"): ("UUID", True, None),
            ("task_runs", "kind"): ("VARCHAR", False, "'asset'::character varying"),
            ("task_runs", "has_checks"): ("BOOLEAN", False, "false"),
            ("task_runs", "written_table_id"): ("UUID", True, None),
            ("task_runs", "written_snapshot_before"): ("BIGINT", True, None),
            ("task_runs", "written_snapshot_after"): ("BIGINT", True, None),
            ("task_runs", "written_publishes"): ("BOOLEAN", True, None),
            ("task_runs", "baseline_snapshot_id"): ("BIGINT", True, None),
            ("task_runs", "baseline_publishes"): ("BOOLEAN", True, None),
            ("task_runs", "own_snapshot_id"): ("BIGINT", True, None),
        }
        results = {c["name"]: c for c in inspector.get_columns("check_results")}
        assert {name: (type(c["type"]).__name__, c["nullable"]) for name, c in results.items()} == {
            "id": ("UUID", False),
            "run_id": ("UUID", False),
            "task_run_id": ("UUID", False),
            "environment_id": ("UUID", False),
            "check_name": ("VARCHAR", False),
            "check_path": ("VARCHAR", False),
            "table_name": ("VARCHAR", False),
            "table_id": ("UUID", True),
            "on_failure": ("VARCHAR", False),
            "outcome": ("VARCHAR", False),
            "failing_rows": ("BIGINT", True),
            "error": ("TEXT", True),
            "checked_snapshot_id": ("BIGINT", True),
            "rollback_outcome": ("VARCHAR", True),
            "policy_applied": ("BOOLEAN", False),
            "query_run_id": ("UUID", True),
            "started_at": ("TIMESTAMP", False),
            "finished_at": ("TIMESTAMP", False),
        }
        assert results["policy_applied"]["default"] == "false"
        assert getattr(results["started_at"]["type"], "timezone", None) is True
        assert [
            (u["name"], u["column_names"])
            for u in inspector.get_unique_constraints("check_results")
        ] == [("uq_check_results_task_run_id_check_name", ["task_run_id", "check_name"])]
        indexes = {
            i["name"]: i["column_names"]
            for i in inspector.get_indexes("check_results")
            if not i.get("duplicates_constraint")
        }
        assert indexes == {
            "ix_check_results_run_id": ["run_id"],
            "ix_check_results_table_id": ["table_id"],
            "ix_check_results_environment_id_finished_at": ["environment_id", "finished_at", "id"],
        }
        foreign = {
            f["constrained_columns"][0]: (f["referred_table"], f["options"].get("ondelete"))
            for f in inspector.get_foreign_keys("check_results")
        }
        assert foreign == {
            "run_id": ("runs", "CASCADE"),
            "task_run_id": ("task_runs", "CASCADE"),
            "environment_id": ("environments", "CASCADE"),
            "query_run_id": ("query_runs", "SET NULL"),
        }  # none on `table_id`: a dropped table keeps its results
        with engine.connect() as conn:
            task = conn.execute(
                sqlalchemy.text(
                    "SELECT kind, has_checks, written_table_id, written_snapshot_before, "
                    "written_snapshot_after, written_publishes, baseline_snapshot_id, "
                    "baseline_publishes, own_snapshot_id FROM task_runs WHERE id = :id"
                ),
                {"id": task_id},
            ).one()
            others = conn.execute(
                sqlalchemy.text(
                    "SELECT (SELECT unpublished_run_id FROM tables WHERE id = :t), "
                    "(SELECT snapshot_before FROM query_runs WHERE id = :q), "
                    "(SELECT checks_json FROM deploy_runs WHERE id = :d)"
                ),
                {"t": table_id, "q": query_id, "d": deploy_id},
            ).one()
        assert tuple(task) == ("asset", False, None, None, None, None, None, None, None)
        assert tuple(others) == (None, None, None)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            # A worker of the release before inserts a task run without the new columns
            # while the upgrade is under way: the server defaults fill them in.
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO task_runs (id, run_id, asset, status, attempt, max_attempts, "
                    "timeout_seconds, retry_delay_seconds, upstream_json, conflict_retried) "
                    "VALUES (:id, :run, 'silver.y', 'PENDING', 0, 3, 60, 0, '[]', false)"
                ),
                {"id": uuid.uuid4(), "run": uuid.uuid4()},
            )
            kinds = conn.execute(
                sqlalchemy.text("SELECT asset, kind, has_checks FROM task_runs ORDER BY asset")
            ).all()
        assert [tuple(k) for k in kinds] == [
            ("silver.x", "asset", False),
            ("silver.y", "asset", False),
        ]

        migrate.downgrade(url, revision_0023)
        assert _alembic_version(engine) == revision_0023
        inspector = sqlalchemy.inspect(engine)
        assert not inspector.has_table("check_results")
        for table, columns in added.items():
            assert not columns & {c["name"] for c in inspector.get_columns(table)}
        with engine.connect() as conn:
            kept = conn.execute(
                sqlalchemy.text(
                    "SELECT (SELECT count(*) FROM task_runs), (SELECT count(*) FROM tables), "
                    "(SELECT count(*) FROM query_runs), (SELECT count(*) FROM deploy_runs)"
                )
            ).one()
        assert tuple(kept) == (2, 1, 1, 1)
        migrate.upgrade(url, revision_0024)
        assert _alembic_version(engine) == revision_0024
        engine.dispose()


def test_0025_on_postgres() -> None:
    """0025 adds the note of a kept table; a table written before it reads as having none.

    A table row written by the release before is there when the upgrade runs: the three
    columns are nullable, without a default and without a foreign key, and the row reads as
    having no note. The downgrade drops them and keeps the row.
    """
    revision_0024 = "b8e2c5f7a1d4"
    revision_0025 = "c9f3d6a8b2e5"
    table_id = uuid.uuid4()
    now = datetime.now(UTC)
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0024)
        engine = sqlalchemy.create_engine(url)
        names = {"kept_run_id", "kept_task_run_id", "kept_check"}
        assert not names & {c["name"] for c in sqlalchemy.inspect(engine).get_columns("tables")}
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at, "
                    "updated_at) VALUES (:id, :ns, 'x', 'ICEBERG', :now, :now)"
                ),
                {"id": table_id, "ns": uuid.uuid4(), "now": now},
            )

        migrate.upgrade(url, revision_0025)
        assert _alembic_version(engine) == revision_0025
        inspector = sqlalchemy.inspect(engine)
        described = {
            column["name"]: (
                type(column["type"]).__name__,
                getattr(column["type"], "length", None),
                column["nullable"],
                column["default"],
            )
            for column in inspector.get_columns("tables")
            if column["name"] in names
        }
        assert described == {
            "kept_run_id": ("UUID", None, True, None),
            "kept_task_run_id": ("UUID", None, True, None),
            "kept_check": ("VARCHAR", 1024, True, None),
        }
        assert {
            f["constrained_columns"][0] for f in inspector.get_foreign_keys("tables")
        }.isdisjoint(names)
        with engine.connect() as conn:
            note = conn.execute(
                sqlalchemy.text(
                    "SELECT kept_run_id, kept_task_run_id, kept_check FROM tables WHERE id = :id"
                ),
                {"id": table_id},
            ).one()
        assert tuple(note) == (None, None, None)

        migrate.downgrade(url, revision_0024)
        assert _alembic_version(engine) == revision_0024
        assert not names & {c["name"] for c in sqlalchemy.inspect(engine).get_columns("tables")}
        with engine.connect() as conn:
            assert conn.execute(sqlalchemy.text("SELECT count(*) FROM tables")).scalar() == 1
        migrate.upgrade(url, revision_0025)
        assert _alembic_version(engine) == revision_0025
        engine.dispose()


def test_0026_on_postgres() -> None:
    """0026 adds the check result a query run is bound to; an older query run reads as unbound.

    A query run written before the upgrade is there when it runs: the column is nullable,
    without a default and without a foreign key (the binding must survive the removal of the
    result's run, so that the worker can fail the query with a message), and the row reads as
    bound to no result. The downgrade drops the column and keeps the row.
    """
    revision_0025 = "c9f3d6a8b2e5"
    revision_0026 = "d1a4e7b9c3f6"
    query_id = uuid.uuid4()
    now = datetime.now(UTC)
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0025)
        engine = sqlalchemy.create_engine(url)
        assert "check_result_id" not in {
            c["name"] for c in sqlalchemy.inspect(engine).get_columns("query_runs")
        }
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO query_runs (id, environment_id, sql_text, status, "
                    "cancel_requested, created_at, updated_at) VALUES (:id, :env, 'SELECT 1', "
                    "'SUCCEEDED', false, :now, :now)"
                ),
                {"id": query_id, "env": uuid.uuid4(), "now": now},
            )

        migrate.upgrade(url, revision_0026)
        assert _alembic_version(engine) == revision_0026
        inspector = sqlalchemy.inspect(engine)
        (column,) = [
            c for c in inspector.get_columns("query_runs") if c["name"] == "check_result_id"
        ]
        assert (type(column["type"]).__name__, column["nullable"], column["default"]) == (
            "UUID",
            True,
            None,
        )
        assert "check_result_id" not in {
            f["constrained_columns"][0] for f in inspector.get_foreign_keys("query_runs")
        }
        with engine.connect() as conn:
            bound = conn.execute(
                sqlalchemy.text("SELECT check_result_id FROM query_runs WHERE id = :id"),
                {"id": query_id},
            ).scalar()
        assert bound is None

        migrate.downgrade(url, revision_0025)
        assert _alembic_version(engine) == revision_0025
        assert "check_result_id" not in {
            c["name"] for c in sqlalchemy.inspect(engine).get_columns("query_runs")
        }
        with engine.connect() as conn:
            assert conn.execute(sqlalchemy.text("SELECT count(*) FROM query_runs")).scalar() == 1
        migrate.upgrade(url, revision_0026)
        assert _alembic_version(engine) == revision_0026
        engine.dispose()


def test_0027_on_postgres() -> None:
    """0027 adds the set-back columns of a task run; an older task run reads as not setting back.

    A task run written before the upgrade is there when it runs: both columns are not null
    with a server default, so the row reads as not setting its table back and with no set-back
    claim taken. The downgrade drops both columns and keeps the row.
    """
    revision_0026 = "d1a4e7b9c3f6"
    revision_0027 = "e2b5f8c1d4a7"
    task_id = uuid.uuid4()
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0026)
        engine = sqlalchemy.create_engine(url)
        new = {"setting_back", "set_back_claims"}
        assert not new & {c["name"] for c in sqlalchemy.inspect(engine).get_columns("task_runs")}
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO task_runs (id, run_id, asset, status, attempt, claim_count, "
                    "max_attempts, timeout_seconds, retry_delay_seconds, upstream_json, "
                    "conflict_retried) VALUES (:id, :run, 'silver.a', 'PENDING', 0, 0, 1, 60, 0, "
                    "'[]', false)"
                ),
                {"id": task_id, "run": uuid.uuid4()},
            )

        migrate.upgrade(url, revision_0027)
        assert _alembic_version(engine) == revision_0027
        columns = {c["name"]: c for c in sqlalchemy.inspect(engine).get_columns("task_runs")}
        assert (columns["setting_back"]["nullable"], columns["set_back_claims"]["nullable"]) == (
            False,
            False,
        )
        with engine.connect() as conn:
            row = conn.execute(
                sqlalchemy.text("SELECT setting_back, set_back_claims FROM task_runs")
            ).one()
        assert (row[0], row[1]) == (False, 0)

        migrate.downgrade(url, revision_0026)
        assert _alembic_version(engine) == revision_0026
        assert not new & {c["name"] for c in sqlalchemy.inspect(engine).get_columns("task_runs")}
        with engine.connect() as conn:
            assert conn.execute(sqlalchemy.text("SELECT count(*) FROM task_runs")).scalar() == 1
        migrate.upgrade(url, revision_0027)
        assert _alembic_version(engine) == revision_0027
        engine.dispose()


def test_0028_on_postgres() -> None:
    """0028 adds the job tables and the run columns; the unique keys hold on Postgres.

    A run written before the upgrade reads as belonging to no job, with no trigger and
    nothing missed. A scheduled time can fire only once. The downgrade drops the tables and the
    columns and keeps the run.
    """
    revision_0027 = "e2b5f8c1d4a7"
    revision_0028 = "f3c6a9d2e5b8"
    run_id = uuid.uuid4()
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0027)
        engine = sqlalchemy.create_engine(url)
        new = {"job", "trigger", "scheduled_for", "missed"}
        assert not new & {c["name"] for c in sqlalchemy.inspect(engine).get_columns("runs")}
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO runs (id, environment_id, repository_id, job_version_id, kind, "
                    "selection_json, status, cancel_requested, created_at) VALUES (:id, :a, :b, "
                    ":c, 'FULL', '{}', 'SUCCEEDED', false, now())"
                ),
                {"id": run_id, "a": uuid.uuid4(), "b": uuid.uuid4(), "c": uuid.uuid4()},
            )

        migrate.upgrade(url, revision_0028)
        assert _alembic_version(engine) == revision_0028
        columns = {c["name"]: c for c in sqlalchemy.inspect(engine).get_columns("runs")}
        assert columns["missed"]["nullable"] is False
        assert "ix_runs_job" in {i["name"] for i in sqlalchemy.inspect(engine).get_indexes("runs")}
        with engine.connect() as conn:
            row = conn.execute(
                sqlalchemy.text("SELECT job, trigger, scheduled_for, missed FROM runs")
            ).one()
        assert tuple(row) == (None, None, None, 0)
        deploy_columns = {
            c["name"]: c for c in sqlalchemy.inspect(engine).get_columns("deploy_runs")
        }
        assert deploy_columns["warnings_json"]["nullable"] is True

        schedule_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO job_schedules (id, environment_id, repository_id, job) "
                    "VALUES (:id, :a, :b, 'daily')"
                ),
                {"id": schedule_id, "a": uuid.uuid4(), "b": uuid.uuid4()},
            )
            insert_fire = sqlalchemy.text(
                "INSERT INTO job_fires (id, job_schedule_id, scheduled_for, created_at) "
                "VALUES (:id, :s, '2026-10-13 06:00:00+00', now())"
            )
            conn.execute(insert_fire, {"id": uuid.uuid4(), "s": schedule_id})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(insert_fire, {"id": uuid.uuid4(), "s": schedule_id})

        # with the foreign keys enforced: deleting the environment takes its schedules and their
        # fires along
        env_id, repo_id, real_schedule = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO environments (id, name, storage_root, storage_options, "
                    "is_protected, properties, created_at, updated_at) "
                    "VALUES (:id, 'prod', 'file:///tmp/prod', '{}', false, '{}', now(), now())"
                ),
                {"id": env_id},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO repositories (id, name, kind, url, created_at, updated_at) "
                    "VALUES (:id, 'analytics', 'HTTPS', 'https://h/a.git', now(), now())"
                ),
                {"id": repo_id},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO job_schedules (id, environment_id, repository_id, job) "
                    "VALUES (:id, :e, :r, 'nightly')"
                ),
                {"id": real_schedule, "e": env_id, "r": repo_id},
            )
            conn.execute(insert_fire, {"id": uuid.uuid4(), "s": real_schedule})
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("DELETE FROM environments WHERE id = :id"), {"id": env_id})
        with engine.connect() as conn:
            left = conn.execute(
                sqlalchemy.text("SELECT count(*) FROM job_schedules WHERE job = 'nightly'")
            ).scalar()
            fires = conn.execute(
                sqlalchemy.text("SELECT count(*) FROM job_fires WHERE job_schedule_id = :s"),
                {"s": real_schedule},
            ).scalar()
        assert (left, fires) == (0, 0)

        migrate.downgrade(url, revision_0027)
        assert _alembic_version(engine) == revision_0027
        tables = set(sqlalchemy.inspect(engine).get_table_names())
        assert not {"job_schedules", "job_fires"} & tables
        assert not new & {c["name"] for c in sqlalchemy.inspect(engine).get_columns("runs")}
        assert "ix_runs_job" not in {
            i["name"] for i in sqlalchemy.inspect(engine).get_indexes("runs")
        }
        assert "warnings_json" not in {
            c["name"] for c in sqlalchemy.inspect(engine).get_columns("deploy_runs")
        }
        with engine.connect() as conn:
            assert conn.execute(sqlalchemy.text("SELECT count(*) FROM runs")).scalar() == 1
        migrate.upgrade(url, revision_0028)
        assert _alembic_version(engine) == revision_0028
        engine.dispose()


def test_0029_on_postgres() -> None:
    """0029 adds `notification_deliveries`; one row per (run, event), many test rows.

    A second row for the same run and event is refused; rows without a run (the event `test`)
    do not collide. Deleting the run takes its rows along. The downgrade drops the table.
    """
    revision_0028 = "f3c6a9d2e5b8"
    revision_0029 = "a4d7b0e3f6c9"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0028)
        engine = sqlalchemy.create_engine(url)
        assert "notification_deliveries" not in sqlalchemy.inspect(engine).get_table_names()

        migrate.upgrade(url, revision_0029)
        assert _alembic_version(engine) == revision_0029
        inspector = sqlalchemy.inspect(engine)
        assert {c["name"] for c in inspector.get_columns("notification_deliveries")} == {
            "id",
            "run_id",
            "environment_id",
            "repository_id",
            "job",
            "event",
            "format",
            "secret_name",
            "state",
            "attempts",
            "next_attempt_at",
            "lease_owner",
            "lease_expires_at",
            "last_result",
            "created_at",
            "delivered_at",
        }
        assert "ix_notification_deliveries_state_next_attempt_at" in {
            i["name"] for i in inspector.get_indexes("notification_deliveries")
        }

        insert = sqlalchemy.text(
            "INSERT INTO notification_deliveries (id, run_id, environment_id, repository_id, job, "
            "event, format, secret_name, state, attempts, next_attempt_at, created_at) VALUES "
            "(:id, :run, :e, :r, 'daily', :event, 'json', 'alerts', 'PENDING', 0, now(), now())"
        )
        env_id, repo_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO environments (id, name, storage_root, storage_options, "
                    "is_protected, properties, created_at, updated_at) "
                    "VALUES (:id, 'prod', 'file:///tmp/prod', '{}', false, '{}', now(), now())"
                ),
                {"id": env_id},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO repositories (id, name, kind, url, created_at, updated_at) "
                    "VALUES (:id, 'analytics', 'HTTPS', 'https://h/a.git', now(), now())"
                ),
                {"id": repo_id},
            )
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO runs (id, environment_id, repository_id, job_version_id, kind, "
                    "selection_json, status, cancel_requested, created_at) VALUES (:id, :e, :r, "
                    ":c, 'FULL', '{}', 'FAILED', false, now())"
                ),
                {"id": run_id, "e": env_id, "r": repo_id, "c": uuid.uuid4()},
            )
        with engine.begin() as conn:
            conn.execute(
                insert,
                {"id": uuid.uuid4(), "run": run_id, "e": env_id, "r": repo_id, "event": "failed"},
            )
            conn.execute(
                insert,
                {"id": uuid.uuid4(), "run": None, "e": env_id, "r": repo_id, "event": "test"},
            )
            conn.execute(
                insert,
                {"id": uuid.uuid4(), "run": None, "e": env_id, "r": repo_id, "event": "test"},
            )
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                insert,
                {"id": uuid.uuid4(), "run": run_id, "e": env_id, "r": repo_id, "event": "failed"},
            )
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("DELETE FROM runs WHERE id = :id"), {"id": run_id})
            left = conn.execute(
                sqlalchemy.text("SELECT count(*) FROM notification_deliveries")
            ).scalar()
        assert left == 2

        migrate.downgrade(url, revision_0028)
        assert _alembic_version(engine) == revision_0028
        assert "notification_deliveries" not in sqlalchemy.inspect(engine).get_table_names()
        migrate.upgrade(url, revision_0029)
        assert _alembic_version(engine) == revision_0029
        engine.dispose()


def test_0030_on_postgres() -> None:
    """0030 adds `tables.kept_unchecked`; a note written before it reads as a failed check.

    A table row with a note is there when the upgrade runs: the column is a boolean, not null,
    with a default of false, and the row reads `False`. The downgrade drops the column, keeps the
    row and its note, and the upgrade runs again.
    """
    revision_0029 = "a4d7b0e3f6c9"
    revision_0030 = "b5e8c1f4a7d0"
    table_id = uuid.uuid4()
    kept_run = uuid.uuid4()
    now = datetime.now(UTC)
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0029)
        engine = sqlalchemy.create_engine(url)
        assert "kept_unchecked" not in {
            c["name"] for c in sqlalchemy.inspect(engine).get_columns("tables")
        }
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at, "
                    "updated_at, kept_run_id, kept_task_run_id, kept_check) VALUES "
                    "(:id, :ns, 'x', 'ICEBERG', :now, :now, :run, :task, 'x/old')"
                ),
                {
                    "id": table_id,
                    "ns": uuid.uuid4(),
                    "now": now,
                    "run": kept_run,
                    "task": uuid.uuid4(),
                },
            )

        migrate.upgrade(url, revision_0030)
        assert _alembic_version(engine) == revision_0030
        column = next(
            c
            for c in sqlalchemy.inspect(engine).get_columns("tables")
            if c["name"] == "kept_unchecked"
        )
        assert type(column["type"]).__name__ == "BOOLEAN"
        assert column["nullable"] is False
        assert "false" in str(column["default"]).lower()
        with engine.connect() as conn:
            row = conn.execute(
                sqlalchemy.text("SELECT kept_run_id, kept_unchecked FROM tables WHERE id = :id"),
                {"id": table_id},
            ).one()
        assert (row[0], row[1]) == (kept_run, False)

        migrate.downgrade(url, revision_0029)
        assert _alembic_version(engine) == revision_0029
        assert "kept_unchecked" not in {
            c["name"] for c in sqlalchemy.inspect(engine).get_columns("tables")
        }
        with engine.connect() as conn:
            kept = conn.execute(
                sqlalchemy.text("SELECT kept_run_id FROM tables WHERE id = :id"), {"id": table_id}
            ).scalar()
        assert kept == kept_run
        migrate.upgrade(url, revision_0030)
        assert _alembic_version(engine) == revision_0030
        engine.dispose()


def test_0031_on_postgres() -> None:
    """0031 adds `tables.kept_earlier`; a note written before it has no entries to restore.

    A table row with a note is there when the upgrade runs: the column is JSON, not null, with a
    default of an empty list, the row reads ``[]``, and a new row without the column reads the
    same. The downgrade drops the column, keeps the row and its note, and the upgrade runs again.
    """
    revision_0030 = "b5e8c1f4a7d0"
    revision_0031 = "c6f9d2a5b8e1"
    table_id = uuid.uuid4()
    kept_run = uuid.uuid4()
    in_flight, idle = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    insert = sqlalchemy.text(
        "INSERT INTO tables (id, namespace_id, name, table_type, created_at, "
        "updated_at, kept_run_id, kept_check) VALUES "
        "(:id, :ns, :name, 'ICEBERG', :now, :now, :run, 'x/old')"
    )
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0030)
        engine = sqlalchemy.create_engine(url)
        assert "kept_earlier" not in {
            c["name"] for c in sqlalchemy.inspect(engine).get_columns("tables")
        }
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                insert,
                {"id": table_id, "ns": uuid.uuid4(), "name": "x", "now": now, "run": kept_run},
            )
            for name, identifier in (("in_flight", in_flight), ("idle", idle)):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO tables (id, namespace_id, name, table_type, created_at, "
                        "updated_at) VALUES (:id, :ns, :name, 'ICEBERG', :now, :now)"
                    ),
                    {"id": identifier, "ns": uuid.uuid4(), "name": name, "now": now},
                )
            conn.execute(  # a write that is stamped and not yet judged by its checks
                sqlalchemy.text(
                    "INSERT INTO task_runs (id, run_id, asset, status, attempt, max_attempts, "
                    "timeout_seconds, retry_delay_seconds, upstream_json, conflict_retried, "
                    "written_table_id) VALUES (:id, :run, 'silver.x', 'RUNNING', 1, 3, 60, 0, "
                    "'[]', false, :written)"
                ),
                {"id": uuid.uuid4(), "run": uuid.uuid4(), "written": in_flight},
            )

        migrate.upgrade(url, revision_0031)
        assert _alembic_version(engine) == revision_0031
        column = next(
            c
            for c in sqlalchemy.inspect(engine).get_columns("tables")
            if c["name"] == "kept_earlier"
        )
        assert type(column["type"]).__name__ == "JSON"
        assert column["nullable"] is False
        snapshot = next(
            c
            for c in sqlalchemy.inspect(engine).get_columns("tables")
            if c["name"] == "kept_snapshot_id"
        )
        assert type(snapshot["type"]).__name__ == "BIGINT"
        assert snapshot["nullable"] is True
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                insert,
                {"id": uuid.uuid4(), "ns": uuid.uuid4(), "name": "y", "now": now, "run": kept_run},
            )
        with engine.connect() as conn:
            rows = conn.execute(
                sqlalchemy.text(
                    "SELECT kept_run_id, kept_earlier FROM tables "
                    "WHERE kept_run_id IS NOT NULL ORDER BY name"
                )
            ).all()
        assert [(row[0], row[1]) for row in rows] == [(kept_run, []), (kept_run, [])]
        with engine.connect() as conn:
            verdicts = dict(
                conn.execute(
                    sqlalchemy.text("SELECT id, kept_snapshot_id FROM tables WHERE id IN (:a, :b)"),
                    {"a": in_flight, "b": idle},
                ).all()
            )
        # A write in flight may have been judged by a clear before the upgrade: unknown (-1).
        assert verdicts == {in_flight: -1, idle: None}

        migrate.downgrade(url, revision_0030)
        assert _alembic_version(engine) == revision_0030
        assert not {"kept_earlier", "kept_snapshot_id"} & {
            c["name"] for c in sqlalchemy.inspect(engine).get_columns("tables")
        }
        with engine.connect() as conn:
            kept = conn.execute(
                sqlalchemy.text("SELECT kept_run_id FROM tables WHERE id = :id"), {"id": table_id}
            ).scalar()
        assert kept == kept_run
        migrate.upgrade(url, revision_0031)
        assert _alembic_version(engine) == revision_0031
        engine.dispose()


def test_0032_on_postgres() -> None:
    """0032 adds `tables.kept_unknown`; a mark written before it reads as a known one.

    A table row with a mark is there when the upgrade runs: the column is boolean, not null,
    false for that row. The downgrade drops the column, keeps the row and its mark, and the
    upgrade runs again.
    """
    revision_0031 = "c6f9d2a5b8e1"
    revision_0032 = "d7a0e3b6c9f2"
    table_id = uuid.uuid4()
    kept_run = uuid.uuid4()
    now = datetime.now(UTC)
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0031)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at, "
                    "updated_at, kept_run_id, kept_check) VALUES "
                    "(:id, :ns, 'x', 'ICEBERG', :now, :now, :run, 'x/old')"
                ),
                {"id": table_id, "ns": uuid.uuid4(), "now": now, "run": kept_run},
            )

        migrate.upgrade(url, revision_0032)
        assert _alembic_version(engine) == revision_0032
        column = next(
            c
            for c in sqlalchemy.inspect(engine).get_columns("tables")
            if c["name"] == "kept_unknown"
        )
        assert type(column["type"]).__name__ == "BOOLEAN"
        assert column["nullable"] is False
        with engine.connect() as conn:
            unknown = conn.execute(
                sqlalchemy.text("SELECT kept_unknown FROM tables WHERE id = :id"), {"id": table_id}
            ).scalar()
        assert unknown is False

        migrate.downgrade(url, revision_0031)
        assert _alembic_version(engine) == revision_0031
        assert "kept_unknown" not in {
            c["name"] for c in sqlalchemy.inspect(engine).get_columns("tables")
        }
        with engine.connect() as conn:
            kept = conn.execute(
                sqlalchemy.text("SELECT kept_run_id FROM tables WHERE id = :id"), {"id": table_id}
            ).scalar()
        assert kept == kept_run
        migrate.upgrade(url, revision_0032)
        assert _alembic_version(engine) == revision_0032
        engine.dispose()


def test_0033_on_postgres() -> None:
    """0033 adds a nullable `schema_id` to row filters and column masks; old rows keep none.

    A filter row is there when the upgrade runs: the column is an integer, null for that row.
    The downgrade drops the columns and keeps the row, and the upgrade runs again.
    """
    revision_0032 = "d7a0e3b6c9f2"
    revision_0033 = "e8b1f4c7d0a3"
    filter_id = uuid.uuid4()
    now = datetime.now(UTC)
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0032)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO row_filters (id, principal_id, environment, namespace, "
                    '"table", expression, created_by, created_at) VALUES '
                    "(:id, :who, 'dev', 'gold', 'x', 'TRUE', :who, :now)"
                ),
                {"id": filter_id, "who": uuid.uuid4(), "now": now},
            )

        migrate.upgrade(url, revision_0033)
        assert _alembic_version(engine) == revision_0033
        for table in ("row_filters", "column_masks"):
            column = next(
                c for c in sqlalchemy.inspect(engine).get_columns(table) if c["name"] == "schema_id"
            )
            assert type(column["type"]).__name__ == "INTEGER"
            assert column["nullable"] is True
        with engine.connect() as conn:
            recorded = conn.execute(
                sqlalchemy.text("SELECT schema_id FROM row_filters WHERE id = :id"),
                {"id": filter_id},
            ).scalar()
        assert recorded is None

        migrate.downgrade(url, revision_0032)
        assert _alembic_version(engine) == revision_0032
        for table in ("row_filters", "column_masks"):
            assert "schema_id" not in {
                c["name"] for c in sqlalchemy.inspect(engine).get_columns(table)
            }
        with engine.connect() as conn:
            kept = conn.execute(
                sqlalchemy.text("SELECT count(*) FROM row_filters WHERE id = :id"),
                {"id": filter_id},
            ).scalar()
        assert kept == 1
        migrate.upgrade(url, revision_0033)
        assert _alembic_version(engine) == revision_0033
        engine.dispose()


def test_0034_on_postgres() -> None:
    """0034 binds a secret one index names, leaves the rest unbound, and downgrades cleanly."""
    revision_0033 = "e8b1f4c7d0a3"
    revision_0034 = "f1c4a8e2b6d9"
    index_id, bound_id, free_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0033)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO package_indexes (id, name, url, credential_secret, created_at, "
                    "updated_at) VALUES (:id, 'corp', 'https://corp/simple', 'pypi', :now, :now)"
                ),
                {"id": index_id, "now": now},
            )
            for secret_id, name in ((bound_id, "pypi"), (free_id, "s3")):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce, "
                        "wrapped_data_key, format_version, key_id, created_by, created_at) "
                        "VALUES (:id, :env, :name, '\\x00', '\\x00', '\\x00', 2, 'env', :who, :now)"
                    ),
                    {
                        "id": secret_id,
                        "env": uuid.uuid4(),
                        "name": name,
                        "who": uuid.uuid4(),
                        "now": now,
                    },
                )

        migrate.upgrade(url, revision_0034)
        assert _alembic_version(engine) == revision_0034
        with engine.connect() as conn:
            found = dict(
                conn.execute(sqlalchemy.text("SELECT id, use_target_id FROM secrets")).all()
            )
        assert found == {bound_id: index_id, free_id: None}
        with engine.connect() as conn:
            kinds = dict(conn.execute(sqlalchemy.text("SELECT id, use_kind FROM secrets")).all())
        assert kinds == {bound_id: "PACKAGE_INDEX", free_id: None}

        migrate.downgrade(url, revision_0033)
        assert _alembic_version(engine) == revision_0033
        assert "use_kind" not in {
            c["name"] for c in sqlalchemy.inspect(engine).get_columns("secrets")
        }
        migrate.upgrade(url, revision_0034)
        assert _alembic_version(engine) == revision_0034
        engine.dispose()


def test_0034_ambiguous_and_environment_used_secrets_on_postgres() -> None:
    """0034 leaves ambiguous and environment-used secrets unbound, per environment."""
    revision_0033 = "e8b1f4c7d0a3"
    revision_0034 = "f1c4a8e2b6d9"
    env_one, env_two, who, repo_id = (uuid.uuid4() for _ in range(4))
    index_ids = {
        n: uuid.uuid4() for n in ("corp", "dup1", "dup2", "mixed", "own", "case", "mh", "oh")
    }
    index_secret = {
        "corp": "pypi",
        "dup1": "two-indexes",
        "dup2": "two-indexes",
        "mixed": "index-and-repo",
        "own": "storage-or-hook",
        "mh": "manifest-hook",
        "oh": "old-hook",
        "case": "Token",
    }
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0033)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            # the migration rewrites a secret row twice in one transaction, and PostgreSQL then
            # checks its foreign keys again, so the creator must exist
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO principals (id, type, name, is_active, created_at, updated_at) "
                    "VALUES (:id, 'USER', 'admin', true, now(), now())"
                ),
                {"id": who},
            )
            for env_id, name, ref in ((env_one, "one", "storage-or-hook"), (env_two, "two", None)):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO environments (id, name, storage_root, storage_options, "
                        "is_protected, properties, storage_secret_ref, created_at, updated_at) "
                        "VALUES (:id, :name, 'file:///x', '{}', false, '{}', :ref, now(), now())"
                    ),
                    {"id": env_id, "name": name, "ref": ref},
                )
            for name, index_id in index_ids.items():
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO package_indexes (id, name, url, credential_secret, "
                        "created_at, updated_at) VALUES (:id, :name, :url, :s, now(), now())"
                    ),
                    {
                        "id": index_id,
                        "name": name,
                        "url": f"https://{name}/simple",
                        "s": index_secret[name],
                    },
                )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO repositories (id, name, kind, url, credential_secret, "
                    "created_at, updated_at) VALUES (:id, 'mixed', 'HTTPS', 'https://g/a.git', "
                    "'index-and-repo', now(), now())"
                ),
                {"id": repo_id},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO package_indexes (id, name, url, credential_secret, created_at, "
                    "updated_at) VALUES (:id, 'hook', 'https://hook/simple', 'hook-secret', "
                    "now(), now())"
                ),
                {"id": index_ids.setdefault("hook", uuid.uuid4())},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO notification_deliveries (id, environment_id, repository_id, "
                    "job, event, format, secret_name, state, attempts, created_at) VALUES "
                    "(:id, :env, :repo, 'nightly', 'test', 'json', 'hook-secret', 'PENDING', 0, "
                    "now())"
                ),
                {"id": uuid.uuid4(), "env": env_one, "repo": repo_id},
            )
            for sha, manifest in (
                ("a" * 40, {"jobs": {"nightly": {"notify": {"webhook": "old-hook"}}}}),
                (
                    "b" * 40,
                    {
                        "jobs": {
                            "nightly": {"notify": {"webhook": "manifest-hook", "format": "json"}}
                        }
                    },
                ),
            ):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO job_versions (id, repository_id, environment_id, commit_sha, "
                        "manifest_json, code_uri, code_sha256, created_at) VALUES (:id, :repo, "
                        ":env, :sha, CAST(:manifest AS json), 'file:///c', :sha256, now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "repo": repo_id,
                        "env": env_one,
                        "sha": sha,
                        "manifest": json.dumps(manifest),
                        "sha256": "0" * 64,
                    },
                )
            for env_id, name in (
                (env_one, "manifest-hook"),
                (env_two, "manifest-hook"),
                (env_one, "old-hook"),
                (env_two, "old-hook"),
                (env_one, "pypi"),
                (env_two, "pypi"),
                (env_one, "two-indexes"),
                (env_one, "index-and-repo"),
                (env_one, "storage-or-hook"),
                (env_two, "storage-or-hook"),
                (env_one, "hook-secret"),
                (env_two, "hook-secret"),
                (env_one, "token"),
            ):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce, "
                        "wrapped_data_key, format_version, key_id, created_by, created_at) "
                        "VALUES (:id, :env, :name, '\\x00', '\\x00', '\\x00', 2, 'env', :who, "
                        "now())"
                    ),
                    {"id": uuid.uuid4(), "env": env_id, "name": name, "who": who},
                )

        migrate.upgrade(url, revision_0034)
        with engine.connect() as conn:
            found = {
                (e, n): (k, t)
                for e, n, k, t in conn.execute(
                    sqlalchemy.text(
                        "SELECT environment_id, name, use_kind, use_target_id FROM secrets"
                    )
                ).all()
            }
        engine.dispose()

    unbound = (None, None)
    assert found[env_one, "pypi"] == ("PACKAGE_INDEX", index_ids["corp"])
    assert found[env_two, "pypi"] == ("PACKAGE_INDEX", index_ids["corp"])
    assert found[env_one, "two-indexes"] == unbound
    assert found[env_one, "index-and-repo"] == unbound
    assert found[env_one, "storage-or-hook"] == unbound
    assert found[env_two, "storage-or-hook"] == ("PACKAGE_INDEX", index_ids["own"])
    assert found[env_one, "hook-secret"] == unbound
    assert found[env_two, "hook-secret"] == ("PACKAGE_INDEX", index_ids["hook"])
    assert found[env_one, "manifest-hook"] == unbound
    assert found[env_two, "manifest-hook"] == ("PACKAGE_INDEX", index_ids["mh"])
    assert found[env_one, "old-hook"] == unbound
    assert found[env_two, "old-hook"] == ("PACKAGE_INDEX", index_ids["oh"])
    assert found[env_one, "token"] == unbound


def test_0035_secret_use_check_on_postgres() -> None:
    """0035 refuses an inconsistent secret use, names existing ones, and downgrades cleanly."""
    revision_0034 = "f1c4a8e2b6d9"
    revision_0035 = "a3d7c9e1f502"
    now = datetime.now(UTC)

    def insert(
        conn: sqlalchemy.Connection, kind: str | None, target: uuid.UUID | None
    ) -> uuid.UUID:
        """Insert one secret with the given use.

        Parameters
        ----------
        conn
            An open connection in a transaction.
        kind
            The `use_kind`, or `None`.
        target
            The `use_target_id`, or `None`.

        Returns
        -------
        uuid.UUID
            The new secret's id.
        """
        row_id = uuid.uuid4()
        conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce, "
                "wrapped_data_key, format_version, key_id, created_by, created_at, use_kind, "
                "use_target_id) VALUES (:id, :env, :name, '\\x00', '\\x00', '\\x00', 2, 'env', "
                ":who, :now, :kind, :target)"
            ),
            {
                "id": row_id,
                "env": uuid.uuid4(),
                "name": str(row_id),
                "who": uuid.uuid4(),
                "now": now,
                "kind": kind,
                "target": target,
            },
        )
        return row_id

    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0034)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            broken = insert(conn, "PACKAGE_INDEX", None)
        with pytest.raises(Exception, match=str(broken)):
            migrate.upgrade(url, revision_0035)
        # The refused upgrade must have left the database at 0034: moving to 0034 again changes
        # nothing, so a version left at 0035 would show here.
        migrate.upgrade(url, revision_0034)
        assert _alembic_version(engine) == revision_0034
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("DELETE FROM secrets"))

        migrate.upgrade(url, revision_0035)
        assert _alembic_version(engine) == revision_0035
        target = uuid.uuid4()
        with engine.begin() as conn:
            insert(conn, None, None)
            insert(conn, "PACKAGE_INDEX", target)
            insert(conn, "REPOSITORY", target)
            insert(conn, "ENVIRONMENT", None)
        for kind, bound in (
            ("PACKAGE_INDEX", None),
            ("REPOSITORY", None),
            (None, target),
            ("ENVIRONMENT", target),
        ):
            # The driver reports a violated CHECK as a `ProgrammingError` (code 23514), not an
            # `IntegrityError`; the constraint's name tells which rule refused the row.
            with (
                pytest.raises(DBAPIError, match="ck_secrets_use_consistent"),
                engine.begin() as conn,
            ):
                insert(conn, kind, bound)

        migrate.downgrade(url, revision_0034)
        with engine.begin() as conn:
            insert(conn, "PACKAGE_INDEX", None)
        engine.dispose()


def test_0036_on_postgres() -> None:
    """0036 adds `column_masks.type_checked`, false for existing masks; the downgrade drops it."""
    revision_0035 = "a3d7c9e1f502"
    revision_0036 = "b5e2d8f1a7c3"
    mask_id = uuid.uuid4()
    now = datetime.now(UTC)
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0035)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO column_masks (id, principal_id, environment, namespace, "
                    '"table", "column", expression, created_by, created_at) VALUES '
                    "(:id, :who, 'dev', 'gold', 'x', 'ssn', 'NULL', :who, :now)"
                ),
                {"id": mask_id, "who": uuid.uuid4(), "now": now},
            )

        migrate.upgrade(url, revision_0036)
        assert _alembic_version(engine) == revision_0036
        column = next(
            c
            for c in sqlalchemy.inspect(engine).get_columns("column_masks")
            if c["name"] == "type_checked"
        )
        assert type(column["type"]).__name__ == "BOOLEAN"
        assert column["nullable"] is False
        with engine.connect() as conn:
            checked = conn.execute(
                sqlalchemy.text("SELECT type_checked FROM column_masks WHERE id = :id"),
                {"id": mask_id},
            ).scalar()
        assert checked is False

        migrate.downgrade(url, revision_0035)
        assert _alembic_version(engine) == revision_0035
        assert "type_checked" not in {
            c["name"] for c in sqlalchemy.inspect(engine).get_columns("column_masks")
        }
        with engine.connect() as conn:
            kept = conn.execute(
                sqlalchemy.text("SELECT count(*) FROM column_masks WHERE id = :id"), {"id": mask_id}
            ).scalar()
        assert kept == 1
        migrate.upgrade(url, revision_0036)
        assert _alembic_version(engine) == revision_0036
        engine.dispose()


def test_0037_on_postgres() -> None:
    """0037 adds the two description texts and `column_descriptions`; the downgrade drops them."""
    revision_0036 = "b5e2d8f1a7c3"
    revision_0037 = "c6f3a9d2b8e4"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0036)
        engine = sqlalchemy.create_engine(url)
        assert "column_descriptions" not in sqlalchemy.inspect(engine).get_table_names()

        migrate.upgrade(url, revision_0037)
        assert _alembic_version(engine) == revision_0037
        inspector = sqlalchemy.inspect(engine)
        found = {c["name"]: c for c in inspector.get_columns("tables")}
        for name in ("description_project", "description_edited"):
            assert type(found[name]["type"]).__name__ == "TEXT"
            assert found[name]["nullable"] is True
        columns = {c["name"] for c in inspector.get_columns("column_descriptions")}
        assert {"table_id", "column", "description_project", "description_edited"} <= columns
        assert [
            u["column_names"] for u in inspector.get_unique_constraints("column_descriptions")
        ] == [["table_id", "column"]]

        migrate.downgrade(url, revision_0036)
        assert _alembic_version(engine) == revision_0036
        inspector = sqlalchemy.inspect(engine)
        assert "column_descriptions" not in inspector.get_table_names()
        assert "description_edited" not in {c["name"] for c in inspector.get_columns("tables")}
        migrate.upgrade(url, revision_0037)
        assert _alembic_version(engine) == revision_0037
        engine.dispose()


def test_0038_on_postgres() -> None:
    """0038 creates `table_reads` with its unique key; the downgrade drops it."""
    revision_0037 = "c6f3a9d2b8e4"
    revision_0038 = "d7a4b0e3c9f5"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0037)
        engine = sqlalchemy.create_engine(url)
        assert "table_reads" not in sqlalchemy.inspect(engine).get_table_names()

        migrate.upgrade(url, revision_0038)
        assert _alembic_version(engine) == revision_0038
        inspector = sqlalchemy.inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("table_reads")}
        assert type(columns["day"]["type"]).__name__ == "DATE"
        assert type(columns["read_count"]["type"]).__name__ == "BIGINT"
        assert [u["column_names"] for u in inspector.get_unique_constraints("table_reads")] == [
            ["table_id", "principal_id", "day"]
        ]

        migrate.downgrade(url, revision_0037)
        assert _alembic_version(engine) == revision_0037
        assert "table_reads" not in sqlalchemy.inspect(engine).get_table_names()
        migrate.upgrade(url, revision_0038)
        assert _alembic_version(engine) == revision_0038
        engine.dispose()


def test_0039_on_postgres() -> None:
    """0039 creates `table_columns` with its unique key; the downgrade drops it."""
    revision_0038 = "d7a4b0e3c9f5"
    revision_0039 = "e8b5c1f4d0a6"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0038)
        engine = sqlalchemy.create_engine(url)
        assert "table_columns" not in sqlalchemy.inspect(engine).get_table_names()

        migrate.upgrade(url, revision_0039)
        assert _alembic_version(engine) == revision_0039
        inspector = sqlalchemy.inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("table_columns")}
        assert type(columns["type"]["type"]).__name__ == "TEXT"
        assert [u["column_names"] for u in inspector.get_unique_constraints("table_columns")] == [
            ["table_id", "name"]
        ]

        migrate.downgrade(url, revision_0038)
        assert _alembic_version(engine) == revision_0038
        assert "table_columns" not in sqlalchemy.inspect(engine).get_table_names()
        migrate.upgrade(url, revision_0039)
        assert _alembic_version(engine) == revision_0039
        engine.dispose()


def test_0040_on_postgres() -> None:
    """0040 adds a nullable `failure_code` to task runs and attempts; the downgrade drops them."""
    revision_0039 = "e8b5c1f4d0a6"
    revision_0040 = "f9c6d2a5e1b7"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0039)
        engine = sqlalchemy.create_engine(url)
        for table in ("task_runs", "task_attempts"):
            names = {c["name"] for c in sqlalchemy.inspect(engine).get_columns(table)}
            assert "failure_code" not in names

        migrate.upgrade(url, revision_0040)
        assert _alembic_version(engine) == revision_0040
        for table in ("task_runs", "task_attempts"):
            columns = {c["name"]: c for c in sqlalchemy.inspect(engine).get_columns(table)}
            assert columns["failure_code"]["nullable"]
            assert getattr(columns["failure_code"]["type"], "length", None) == 32

        migrate.downgrade(url, revision_0039)
        assert _alembic_version(engine) == revision_0039
        for table in ("task_runs", "task_attempts"):
            names = {c["name"] for c in sqlalchemy.inspect(engine).get_columns(table)}
            assert "failure_code" not in names
        migrate.upgrade(url, revision_0040)
        assert _alembic_version(engine) == revision_0040
        engine.dispose()


def test_0041_on_postgres() -> None:
    """0041 adds a nullable `query_passed_by_until` to principals; the downgrade drops it."""
    revision_0040 = "f9c6d2a5e1b7"
    revision_0041 = "a0d7e3b6f2c8"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0040)
        engine = sqlalchemy.create_engine(url)
        names = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("principals")}
        assert "query_passed_by_until" not in names

        migrate.upgrade(url, revision_0041)
        assert _alembic_version(engine) == revision_0041
        columns = {c["name"]: c for c in sqlalchemy.inspect(engine).get_columns("principals")}
        assert columns["query_passed_by_until"]["nullable"]

        migrate.downgrade(url, revision_0040)
        assert _alembic_version(engine) == revision_0040
        names = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("principals")}
        assert "query_passed_by_until" not in names
        migrate.upgrade(url, revision_0041)
        assert _alembic_version(engine) == revision_0041
        engine.dispose()


def test_0042_on_postgres() -> None:
    """0042 creates `table_locations` and backfills a LIVE record per managed table; down drops it.

    A managed table with a metadata file gets one record at its root location; an external
    table and a managed table without a metadata file get none. The downgrade keeps the tables,
    and a second upgrade backfills the same location again.
    """
    revision_0041 = "a0d7e3b6f2c8"
    revision_0042 = "b1e8f4c7a3d9"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0041)
        engine = sqlalchemy.create_engine(url)
        now = datetime.now(UTC)
        env_id, ns_id = uuid.uuid4(), uuid.uuid4()
        orders_id, bare_id, raw_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        location = f"s3://bucket/dev/managed/gold/orders-{orders_id.hex}"
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO environments (id, name, storage_root, storage_options, "
                    "is_protected, properties, created_at, updated_at) "
                    "VALUES (:id, 'dev', 's3://bucket/dev', '{}', false, '{}', :now, :now)"
                ),
                {"id": env_id, "now": now},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO namespaces (id, environment_id, path, properties) "
                    "VALUES (:id, :env, 'gold', '{}')"
                ),
                {"id": ns_id, "env": env_id},
            )
            for table_id, name, kind, meta in (
                (orders_id, "orders", "ICEBERG", f"{location}/metadata/00001-a.metadata.json"),
                (bare_id, "bare", "ICEBERG", None),
                (raw_id, "raw", "EXTERNAL", None),
            ):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO tables (id, namespace_id, name, table_type, "
                        "metadata_location, created_at, updated_at) "
                        "VALUES (:id, :ns, :name, :kind, :meta, :now, :now)"
                    ),
                    {
                        "id": table_id,
                        "ns": ns_id,
                        "name": name,
                        "kind": kind,
                        "meta": meta,
                        "now": now,
                    },
                )

        def records() -> list[tuple[object, ...]]:
            with engine.connect() as conn:
                return [
                    tuple(row)
                    for row in conn.execute(
                        sqlalchemy.text(
                            "SELECT table_id, environment_id, location, state, holder_run_id "
                            "FROM table_locations"
                        )
                    ).all()
                ]

        migrate.upgrade(url, revision_0042)
        assert _alembic_version(engine) == revision_0042
        assert records() == [(orders_id, env_id, location, "LIVE", None)]
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO table_locations (id, environment_id, location, state, "
                    "claimed_at, revoked_policy_ids) "
                    "VALUES (:id, :env, :loc, 'CLAIMED', :now, '[]')"
                ),
                {"id": uuid.uuid4(), "env": env_id, "loc": location, "now": now},
            )

        migrate.downgrade(url, revision_0041)
        assert _alembic_version(engine) == revision_0041
        assert "table_locations" not in sqlalchemy.inspect(engine).get_table_names()
        migrate.upgrade(url, revision_0042)
        assert _alembic_version(engine) == revision_0042
        assert records() == [(orders_id, env_id, location, "LIVE", None)]
        engine.dispose()


def test_0043_on_postgres() -> None:
    """0043 adds the two nullable tombstone columns, keeps the records, and drops them down."""
    revision_0042 = "b1e8f4c7a3d9"
    revision_0043 = "c2f9a5d8b4e0"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0042)
        engine = sqlalchemy.create_engine(url)
        now = datetime.now(UTC)
        env_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO environments (id, name, storage_root, storage_options, "
                    "is_protected, properties, created_at, updated_at) "
                    "VALUES (:id, 'dev', 's3://bucket/dev', '{}', false, '{}', :now, :now)"
                ),
                {"id": env_id, "now": now},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO table_locations (id, environment_id, location, state, "
                    "claimed_at, purge_after, revoked_policy_ids) "
                    "VALUES (:id, :env, 's3://bucket/dev/managed/gold/x-0a1b', 'DROPPED', "
                    ":now, :now, '[]')"
                ),
                {"id": uuid.uuid4(), "env": env_id, "now": now},
            )

        def records() -> list[tuple[object, ...]]:
            with engine.connect() as conn:
                return [
                    tuple(row)
                    for row in conn.execute(
                        sqlalchemy.text("SELECT location, state, purge_after FROM table_locations")
                    ).all()
                ]

        before = records()
        migrate.upgrade(url, revision_0043)
        assert _alembic_version(engine) == revision_0043
        columns = {c["name"]: c for c in sqlalchemy.inspect(engine).get_columns("table_locations")}
        assert columns["purge_started_at"]["nullable"] is True
        assert columns["dropped_table"]["nullable"] is True
        assert records() == before
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "UPDATE table_locations SET purge_started_at = :now, "
                    "dropped_table = CAST(:snapshot AS JSON)"
                ),
                {"now": now, "snapshot": json.dumps({"namespace": ["gold"], "row": {}})},
            )

        migrate.downgrade(url, revision_0042)
        assert _alembic_version(engine) == revision_0042
        names = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("table_locations")}
        assert "purge_started_at" not in names and "dropped_table" not in names
        assert records() == before
        engine.dispose()


def test_0044_on_postgres() -> None:
    """0044 adds the retention columns, empty on every row, and drops them down."""
    revision_0043 = "c2f9a5d8b4e0"
    revision_0044 = "d3a0b6e9c5f1"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0043)
        engine = sqlalchemy.create_engine(url)
        now = datetime.now(UTC)
        env_id, ns_id, table_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO environments (id, name, storage_root, storage_options, "
                    "is_protected, properties, created_at, updated_at) "
                    "VALUES (:id, 'dev', 's3://bucket/dev', '{}', false, '{}', :now, :now)"
                ),
                {"id": env_id, "now": now},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO namespaces (id, environment_id, path, properties) "
                    "VALUES (:id, :env, 'gold', '{}')"
                ),
                {"id": ns_id, "env": env_id},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at, "
                    "updated_at) VALUES (:id, :ns, 'orders', 'ICEBERG', :now, :now)"
                ),
                {"id": table_id, "ns": ns_id, "now": now},
            )

        migrate.upgrade(url, revision_0044)
        assert _alembic_version(engine) == revision_0044
        with engine.connect() as conn:
            assert conn.execute(
                sqlalchemy.text(
                    "SELECT retention_days, retention_on, retention_never, retention_set_by, "
                    "retention_set_at FROM tables"
                )
            ).all() == [(None, None, False, None, None)]
            assert conn.execute(
                sqlalchemy.text(
                    "SELECT retention_days, retention_set_by, retention_set_at FROM environments"
                )
            ).all() == [(None, None, None)]

        migrate.downgrade(url, revision_0043)
        assert _alembic_version(engine) == revision_0043
        for table in ("tables", "environments"):
            names = {c["name"] for c in sqlalchemy.inspect(engine).get_columns(table)}
            assert not {n for n in names if n.startswith("retention_")}
        engine.dispose()


def test_0045_on_postgres() -> None:
    """0045 adds the deletion columns, empty on every row, and drops them down."""
    revision_0044 = "d3a0b6e9c5f1"
    revision_0045 = "ca4f68b6e89a"
    columns = (
        "deleted_at",
        "deleted_by",
        "purge_after",
        "dropped_location_ids",
        "revoked_policy_ids",
    )
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0044)
        engine = sqlalchemy.create_engine(url)
        now = datetime.now(UTC)
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO environments (id, name, storage_root, storage_options, "
                    "is_protected, properties, created_at, updated_at) "
                    "VALUES (:id, 'dev', 's3://bucket/dev', '{}', false, '{}', :now, :now)"
                ),
                {"id": uuid.uuid4(), "now": now},
            )

        migrate.upgrade(url, revision_0045)
        assert _alembic_version(engine) == revision_0045
        with engine.connect() as conn:
            assert conn.execute(
                sqlalchemy.text(f"SELECT {', '.join(columns)} FROM environments")
            ).all() == [(None, None, None, None, None)]
        foreign_keys = sqlalchemy.inspect(engine).get_foreign_keys("environments")
        assert {
            (tuple(fk["constrained_columns"]), fk["referred_table"], fk["options"].get("ondelete"))
            for fk in foreign_keys
            if fk["constrained_columns"] == ["deleted_by"]
        } == {(("deleted_by",), "principals", "SET NULL")}

        migrate.downgrade(url, revision_0044)
        assert _alembic_version(engine) == revision_0044
        names = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("environments")}
        assert not names & set(columns)
        with engine.connect() as conn:
            assert (
                conn.execute(sqlalchemy.text("SELECT count(*) FROM environments")).scalar_one() == 1
            )
        engine.dispose()


def test_0046_on_postgres() -> None:
    """0046 adds the composite index of the view audit's lookup and drops it down."""
    revision_0045 = "ca4f68b6e89a"
    revision_0046 = "00591a77722d"
    name = "ix_audit_events_event_type_principal_id_occurred_at"
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0045)
        engine = sqlalchemy.create_engine(url)

        migrate.upgrade(url, revision_0046)
        assert _alembic_version(engine) == revision_0046
        indexes = {
            i["name"]: i["column_names"]
            for i in sqlalchemy.inspect(engine).get_indexes("audit_events")
        }
        assert indexes[name] == ["event_type", "principal_id", "occurred_at"]

        migrate.downgrade(url, revision_0045)
        assert _alembic_version(engine) == revision_0045
        names = {i["name"] for i in sqlalchemy.inspect(engine).get_indexes("audit_events")}
        assert name not in names
        engine.dispose()


def test_0047_on_postgres() -> None:
    """0047 creates the CI OIDC tables and `deploy_runs.ci_token_id`, and drops them down."""
    revision_0046 = "00591a77722d"
    revision_0047 = "e0d9e6185736"
    tables = {"ci_trust_rules", "ci_tokens", "ci_oidc_jtis"}
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0046)
        engine = sqlalchemy.create_engine(url)

        migrate.upgrade(url, revision_0047)
        assert _alembic_version(engine) == revision_0047
        inspector = sqlalchemy.inspect(engine)
        assert tables <= set(inspector.get_table_names())
        assert "ci_token_id" in {c["name"] for c in inspector.get_columns("deploy_runs")}
        assert {
            (tuple(fk["constrained_columns"]), fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("ci_tokens")
            if fk["constrained_columns"] == ["rule_id"]
        } == {(("rule_id",), "ci_trust_rules", "SET NULL")}

        migrate.downgrade(url, revision_0046)
        assert _alembic_version(engine) == revision_0046
        inspector = sqlalchemy.inspect(engine)
        assert not tables & set(inspector.get_table_names())
        assert "ci_token_id" not in {c["name"] for c in inspector.get_columns("deploy_runs")}
        engine.dispose()


def test_0048_on_postgres() -> None:
    """0048 adds the size columns, all empty and nullable, and drops them down."""
    revision_0047 = "e0d9e6185736"
    revision_0048 = "f1a2b3c4d5e6"
    wanted = {
        "tables": {"size_bytes", "size_metadata_location"},
        "job_versions": {"code_bytes"},
        "workers": {
            "cache_environments_bytes",
            "cache_packages_bytes",
            "cache_tasks_bytes",
            "cache_complete",
            "cache_measured_at",
        },
    }
    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0047)
        engine = sqlalchemy.create_engine(url)

        migrate.upgrade(url, revision_0048)
        assert _alembic_version(engine) == revision_0048
        inspector = sqlalchemy.inspect(engine)
        for table, names in wanted.items():
            found = {c["name"]: c["nullable"] for c in inspector.get_columns(table)}
            assert {name: found.get(name) for name in names} == dict.fromkeys(names, True)

        migrate.downgrade(url, revision_0047)
        assert _alembic_version(engine) == revision_0047
        inspector = sqlalchemy.inspect(engine)
        for table, names in wanted.items():
            assert not names & {c["name"] for c in inspector.get_columns(table)}
        engine.dispose()


def test_0049_on_postgres() -> None:
    """0049 adds the connection tables, the secret use and `source_kind`, and drops them down."""
    revision_0048 = "f1a2b3c4d5e6"
    revision_0049 = "a7c3e9f1b2d4"
    now = datetime.now(UTC)

    def secret(
        conn: sqlalchemy.Connection, kind: str | None, target: uuid.UUID | None
    ) -> uuid.UUID:
        """Insert one secret with the given use, foreign keys off.

        Parameters
        ----------
        conn
            An open connection in a transaction.
        kind
            The `use_kind`, or `None`.
        target
            The `use_target_id`, or `None`.

        Returns
        -------
        uuid.UUID
            The new secret's id.
        """
        row_id = uuid.uuid4()
        conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
        conn.execute(
            sqlalchemy.text(
                "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce, "
                "wrapped_data_key, format_version, key_id, created_by, created_at, use_kind, "
                "use_target_id) VALUES (:id, :env, :name, '\\x00', '\\x00', '\\x00', 2, 'env', "
                ":who, :now, :kind, :target)"
            ),
            {
                "id": row_id,
                "env": uuid.uuid4(),
                "name": str(row_id),
                "who": uuid.uuid4(),
                "now": now,
                "kind": kind,
                "target": target,
            },
        )
        return row_id

    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0048)
        engine = sqlalchemy.create_engine(url)
        edge = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO lineage_edges (id, environment_id, source_table_id, source_name,"
                    " target_table_id, target_name, created_at) VALUES (:id, :env, :s, 'a.b',"
                    " :t, 'c.d', :now)"
                ),
                {"id": edge, "env": uuid.uuid4(), "s": uuid.uuid4(), "t": uuid.uuid4(), "now": now},
            )

        migrate.upgrade(url, revision_0049)
        assert _alembic_version(engine) == revision_0049
        inspector = sqlalchemy.inspect(engine)
        assert {"connections", "connection_tests"} <= set(inspector.get_table_names())
        assert {
            c["name"]: tuple(c["column_names"])
            for c in inspector.get_unique_constraints("connections")
        } == {"uq_connections_environment_name": ("environment_id", "name")}
        assert {
            (tuple(fk["constrained_columns"]), fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("connections")
        } == {
            (("environment_id",), "environments", "CASCADE"),
            (("secret_id",), "secrets", "SET NULL"),
            (("created_by",), "principals", None),
        }
        assert {
            (tuple(fk["constrained_columns"]), fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("connection_tests")
        } == {
            (("connection_id",), "connections", "CASCADE"),
            (("environment_id",), "environments", "CASCADE"),
            (("requested_by",), "principals", None),
        }
        target = uuid.uuid4()
        with engine.begin() as conn:
            kind = conn.execute(
                sqlalchemy.text("SELECT source_kind FROM lineage_edges WHERE id = :id"),
                {"id": edge},
            ).scalar_one()
            bound = secret(conn, "CONNECTION", target)
        assert kind == "table"
        with (
            pytest.raises(DBAPIError, match="ck_secrets_use_consistent"),
            engine.begin() as conn,
        ):
            secret(conn, "CONNECTION", None)

        migrate.downgrade(url, revision_0048)
        assert _alembic_version(engine) == revision_0048
        inspector = sqlalchemy.inspect(engine)
        assert not {"connections", "connection_tests"} & set(inspector.get_table_names())
        assert "source_kind" not in {c["name"] for c in inspector.get_columns("lineage_edges")}
        with engine.begin() as conn:
            use = conn.execute(
                sqlalchemy.text("SELECT use_kind, use_target_id FROM secrets WHERE id = :id"),
                {"id": bound},
            ).one()
        assert tuple(use) == (None, None)
        with (
            pytest.raises(DBAPIError, match="ck_secrets_use_consistent"),
            engine.begin() as conn,
        ):
            secret(conn, "CONNECTION", target)
        engine.dispose()


def test_0050_on_postgres() -> None:
    """0050 adds `query_runs.merge_key`; a downgrade records a merge run as an append."""
    revision_0049 = "a7c3e9f1b2d4"
    revision_0050 = "b8d4f0a2c3e5"
    now = datetime.now(UTC)

    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0049)
        migrate.upgrade(url, revision_0050)
        engine = sqlalchemy.create_engine(url)
        assert _alembic_version(engine) == revision_0050
        merged, replaced = uuid.uuid4(), uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            for run_id, mode, key in ((merged, "MERGE", '["id"]'), (replaced, "REPLACE", None)):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO query_runs (id, environment_id, sql_text, status, "
                        "cancel_requested, write_mode, merge_key, created_at, updated_at) VALUES "
                        "(:id, :env, 'SELECT 1', 'SUCCEEDED', false, :mode, CAST(:key AS json), "
                        ":now, :now)"
                    ),
                    {"id": run_id, "env": uuid.uuid4(), "mode": mode, "key": key, "now": now},
                )
        with engine.connect() as conn:
            stored = conn.execute(
                sqlalchemy.text("SELECT merge_key FROM query_runs WHERE id = :id"), {"id": merged}
            ).scalar_one()
        assert stored == ["id"]

        migrate.downgrade(url, revision_0049)
        assert _alembic_version(engine) == revision_0049
        columns = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("query_runs")}
        assert "merge_key" not in columns
        with engine.connect() as conn:
            modes = dict(
                conn.execute(sqlalchemy.text("SELECT id, write_mode FROM query_runs")).all()
            )
        assert modes == {merged: "APPEND", replaced: "REPLACE"}
        engine.dispose()


def test_0051_on_postgres() -> None:
    """0051 adds `deploy_runs.job_starts_json`; a downgrade names a deploy's run by its starter."""
    revision_0050 = "b8d4f0a2c3e5"
    revision_0051 = "c9e5a1b3d4f6"
    now = datetime.now(UTC)

    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0050)
        migrate.upgrade(url, revision_0051)
        engine = sqlalchemy.create_engine(url)
        assert _alembic_version(engine) == revision_0051
        columns = {c["name"]: c for c in sqlalchemy.inspect(engine).get_columns("deploy_runs")}
        assert columns["job_starts_json"]["nullable"] is True
        rules = {c["name"]: c for c in sqlalchemy.inspect(engine).get_columns("ci_trust_rules")}
        assert rules["start_on_deploy_jobs"]["nullable"] is False
        account, person = uuid.uuid4(), uuid.uuid4()
        by_account, by_person, scheduled = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            for principal_id, kind in ((account, "SERVICE_ACCOUNT"), (person, "USER")):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO principals (id, type, name, is_active, created_at, "
                        "updated_at) VALUES (:id, :type, :name, true, :now, :now)"
                    ),
                    {"id": principal_id, "type": kind, "name": str(principal_id), "now": now},
                )
            for run_id, trigger, by in (
                (by_account, "DEPLOY", account),
                (by_person, "DEPLOY", person),
                (scheduled, "SCHEDULE", account),
            ):
                conn.execute(
                    sqlalchemy.text(
                        "INSERT INTO runs (id, environment_id, repository_id, job_version_id, "
                        "kind, selection_json, status, cancel_requested, created_at, "
                        "triggered_by, job, trigger) VALUES (:id, :a, :b, :c, 'FULL', '{}', "
                        "'SUCCEEDED', false, now(), :by, 'refresh', :trigger)"
                    ),
                    {
                        "id": run_id,
                        "a": uuid.uuid4(),
                        "b": uuid.uuid4(),
                        "c": uuid.uuid4(),
                        "by": by,
                        "trigger": trigger,
                    },
                )

        migrate.downgrade(url, revision_0050)
        assert _alembic_version(engine) == revision_0050
        columns = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("deploy_runs")}
        assert "job_starts_json" not in columns
        rules = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("ci_trust_rules")}
        assert "start_on_deploy_jobs" not in rules
        with engine.connect() as conn:
            triggers = dict(conn.execute(sqlalchemy.text("SELECT id, trigger FROM runs")).all())
        assert triggers == {by_account: "API", by_person: "MANUAL", scheduled: "SCHEDULE"}
        engine.dispose()


def test_0052_on_postgres() -> None:
    """0052 adds `check_results.generated`, false on an older result, and drops it down."""
    revision_0051 = "c9e5a1b3d4f6"
    revision_0052 = "d1f6b2c4e5a7"
    now = datetime.now(UTC)
    result_id = uuid.uuid4()

    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0051)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("SET LOCAL session_replication_role = replica"))
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO check_results (id, run_id, task_run_id, environment_id, "
                    "check_name, check_path, table_name, on_failure, outcome, started_at, "
                    "finished_at) VALUES (:id, :run, :task, :env, 'silver.o/c', "
                    "'checks/silver/o/c.sql', 'silver.o', 'block', 'PASSED', :now, :now)"
                ),
                {
                    "id": result_id,
                    "run": uuid.uuid4(),
                    "task": uuid.uuid4(),
                    "env": uuid.uuid4(),
                    "now": now,
                },
            )

        migrate.upgrade(url, revision_0052)
        assert _alembic_version(engine) == revision_0052
        columns = {c["name"]: c for c in sqlalchemy.inspect(engine).get_columns("check_results")}
        assert columns["generated"]["nullable"] is False
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sqlalchemy.text("SELECT generated FROM check_results WHERE id = :id"),
                    {"id": result_id},
                ).scalar_one()
                is False
            )

        migrate.downgrade(url, revision_0051)
        assert _alembic_version(engine) == revision_0051
        names = {c["name"] for c in sqlalchemy.inspect(engine).get_columns("check_results")}
        assert "generated" not in names
        with engine.connect() as conn:
            assert (
                conn.execute(sqlalchemy.text("SELECT count(*) FROM check_results")).scalar_one()
                == 1
            )
        engine.dispose()


def test_0053_on_postgres() -> None:
    """0053 adds the model columns, null on an old table, and `table_references`, and drops them."""
    revision_0052 = "d1f6b2c4e5a7"
    revision_0053 = "e3a7c5d9f1b2"
    now = datetime.now(UTC)
    env_id, ns_id, table_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    insert_reference = sqlalchemy.text(
        "INSERT INTO table_references (id, environment_id, source_table_id, source_column, "
        "target_name, target_column) VALUES (:id, :env, :table, 'store_id', 'gold.dim_store', "
        "'store_id')"
    )

    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0052)
        engine = sqlalchemy.create_engine(url)
        with engine.begin() as conn:
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO environments (id, name, storage_root, storage_options, "
                    "is_protected, properties, created_at, updated_at) "
                    "VALUES (:id, 'dev', 's3://bucket/dev', '{}', false, '{}', :now, :now)"
                ),
                {"id": env_id, "now": now},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO namespaces (id, environment_id, path, properties) "
                    "VALUES (:id, :env, 'gold', '{}')"
                ),
                {"id": ns_id, "env": env_id},
            )
            conn.execute(
                sqlalchemy.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at, "
                    "updated_at) VALUES (:id, :ns, 'fact_sales', 'ICEBERG', :now, :now)"
                ),
                {"id": table_id, "ns": ns_id, "now": now},
            )

        migrate.upgrade(url, revision_0053)
        assert _alembic_version(engine) == revision_0053
        inspector = sqlalchemy.inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("tables")}
        assert {name: columns[name]["nullable"] for name in ("model_kind", "model_grain")} == {
            "model_kind": True,
            "model_grain": True,
        }
        assert getattr(columns["model_kind"]["type"], "length", None) == 16
        assert columns["model_key"]["nullable"] is True
        assert {
            tuple(c["column_names"]) for c in inspector.get_unique_constraints("table_references")
        } == {("source_table_id", "source_column")}
        assert (
            "ix_table_references_environment_target",
            ("environment_id", "target_name"),
        ) in {
            (i["name"], tuple(i["column_names"])) for i in inspector.get_indexes("table_references")
        }
        with engine.begin() as conn:
            assert conn.execute(
                sqlalchemy.text("SELECT model_kind, model_grain, model_key FROM tables")
            ).all() == [(None, None, None)]
            conn.execute(insert_reference, {"id": uuid.uuid4(), "env": env_id, "table": table_id})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert_reference, {"id": uuid.uuid4(), "env": env_id, "table": table_id})
        with engine.begin() as conn:
            conn.execute(sqlalchemy.text("DELETE FROM tables"))
            assert (
                conn.execute(sqlalchemy.text("SELECT count(*) FROM table_references")).scalar_one()
                == 0
            )

        migrate.downgrade(url, revision_0052)
        assert _alembic_version(engine) == revision_0052
        inspector = sqlalchemy.inspect(engine)
        assert "table_references" not in inspector.get_table_names()
        names = {c["name"] for c in inspector.get_columns("tables")}
        assert not {"model_kind", "model_grain", "model_key"} & names

        migrate.upgrade(url, revision_0053)
        assert _alembic_version(engine) == revision_0053
        assert "table_references" in sqlalchemy.inspect(engine).get_table_names()
        engine.dispose()


def test_0054_on_postgres() -> None:
    """0054 adds `check_message_state` to task runs and attempts, drops it, and adds it again."""
    revision_0053 = "e3a7c5d9f1b2"
    revision_0054 = "f4b8d6e0a2c3"
    tables = ("task_runs", "task_attempts")

    with PostgresContainer("postgres:16", driver="pg8000") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url, revision_0054)
        engine = sqlalchemy.create_engine(url)
        assert _alembic_version(engine) == revision_0054
        inspector = sqlalchemy.inspect(engine)
        for table in tables:
            columns = {c["name"]: c for c in inspector.get_columns(table)}
            assert columns["check_message_state"]["nullable"] is True

        migrate.downgrade(url, revision_0053)
        assert _alembic_version(engine) == revision_0053
        inspector = sqlalchemy.inspect(engine)
        for table in tables:
            assert "check_message_state" not in {c["name"] for c in inspector.get_columns(table)}

        migrate.upgrade(url, revision_0054)
        assert _alembic_version(engine) == revision_0054
        engine.dispose()
