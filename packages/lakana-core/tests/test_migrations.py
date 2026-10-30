"""Tests that the Alembic migration matches the ORM models.

`pyproject.toml`'s `[tool.coverage.run]` omit comment for
`lakana_core/migrations` claims migrations "are verified instead by applying
and reversing them against a real SQLite database." Before this test that
described a manual step performed once during development, not anything
repeatable in CI. This test makes that claim true: it applies `0001_initial`
to a fresh SQLite file and asserts that Alembic's own schema-comparison
finds no drift against `Base.metadata`, so a future model change that is
not reflected in a migration (or vice versa) fails here.
"""

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from core_test_helpers import assert_loads_as_pre_m5a, seed_m5a_rows
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from lakana_core.identifiers import NAMESPACE_SEPARATOR
from lakana_core.models import Base

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
_REVISION_0010 = "a3f7c2e9b4d1"


def test_upgraded_schema_matches_the_orm_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Applying every migration yields exactly the schema the models declare.

    Parameters
    ----------
    tmp_path
        Pytest-provided temporary directory, holding the throwaway SQLite
        file the migration is applied to.
    monkeypatch
        Used to point `lakana_core.config.Settings` (read by
        `migrations/env.py`) at that file for the duration of the test.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")

    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert diff == []


def test_env_py_fallback_survives_a_percent_sign_in_the_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`env.py`'s `Settings()` fallback also escapes `%`.

    This path (`alembic.ini` plus `LAKANA_DATABASE__URL`, as tests and `alembic
    upgrade` from a checkout use) sets `sqlalchemy.url` on the same
    `ConfigParser`-backed `Config` that `lakana_core.migrate.alembic_config`
    does, and is exposed to the identical `ValueError: invalid
    interpolation syntax` for a `%` in the URL -- reproduced here with a
    `%` in the data directory path, the local-mode shape.
    """
    weird_dir = tmp_path / "100%-local"
    weird_dir.mkdir()
    db_path = weird_dir / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")

    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "environments" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.slow
def test_0011_binds_grants_and_policies_to_ids_and_revokes_stale_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0011` backfills id bindings and revokes unresolvable rows.

    Reproduces the fixture directly against
    SQLite: upgrading to `0010` (`a3f7c2e9b4d1`), inserting rows with raw
    SQL that predate the id columns, upgrading to head, and asserting the
    backfill's effect — then downgrading back to `0010` and asserting the
    added columns are gone.

    Beyond the brief's fixture, also covers: a `NAMESPACE`-kind grant on
    an existing namespace is kept, one on a missing namespace is revoked;
    a multi-level namespace path resolves; the same table name in two
    different environments binds to each environment's own table id; a
    stale row filter and a stale column mask are revoked; and a grant
    already revoked before the migration keeps its original `revoked_at`
    untouched.

    Parameters
    ----------
    tmp_path
        Pytest-provided temporary directory, holding the throwaway SQLite
        file the migration is applied to.
    monkeypatch
        Used to point `lakana_core.config.Settings` (read by
        `migrations/env.py`) at that file for the duration of the test.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))

    command.upgrade(config, _REVISION_0010)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(UTC)

    principals = sa.table(
        "principals",
        sa.column("id", sa.Uuid()),
        sa.column("type", sa.String()),
        sa.column("name", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    environments = sa.table(
        "environments",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("storage_root", sa.String()),
        sa.column("storage_options", sa.JSON()),
        sa.column("is_protected", sa.Boolean()),
        sa.column("properties", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    namespaces = sa.table(
        "namespaces",
        sa.column("id", sa.Uuid()),
        sa.column("environment_id", sa.Uuid()),
        sa.column("path", sa.String()),
        sa.column("properties", sa.JSON()),
        sa.column("owner_id", sa.Uuid()),
    )
    tables = sa.table(
        "tables",
        sa.column("id", sa.Uuid()),
        sa.column("namespace_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("table_type", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    grants = sa.table(
        "grants",
        sa.column("id", sa.Uuid()),
        sa.column("principal_id", sa.Uuid()),
        sa.column("role", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("environment", sa.String()),
        sa.column("namespace", sa.String()),
        sa.column("table", sa.String()),
        sa.column("granted_by", sa.Uuid()),
        sa.column("granted_at", sa.DateTime(timezone=True)),
        sa.column("revoked_by", sa.Uuid()),
        sa.column("revoked_at", sa.DateTime(timezone=True)),
    )
    row_filters = sa.table(
        "row_filters",
        sa.column("id", sa.Uuid()),
        sa.column("principal_id", sa.Uuid()),
        sa.column("environment", sa.String()),
        sa.column("namespace", sa.String()),
        sa.column("table", sa.String()),
        sa.column("expression", sa.String()),
        sa.column("created_by", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    column_masks = sa.table(
        "column_masks",
        sa.column("id", sa.Uuid()),
        sa.column("principal_id", sa.Uuid()),
        sa.column("environment", sa.String()),
        sa.column("namespace", sa.String()),
        sa.column("table", sa.String()),
        sa.column("column", sa.String()),
        sa.column("expression", sa.String()),
        sa.column("created_by", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    admin_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    gold_id = uuid.uuid4()
    orders_id = uuid.uuid4()
    nested_ns_id = uuid.uuid4()
    deep_table_id = uuid.uuid4()
    prod_id = uuid.uuid4()
    prod_gold_id = uuid.uuid4()
    prod_orders_id = uuid.uuid4()
    table_grant_id = uuid.uuid4()
    env_grant_id = uuid.uuid4()
    instance_grant_id = uuid.uuid4()
    ghost_grant_id = uuid.uuid4()
    already_revoked_grant_id = uuid.uuid4()
    nested_table_grant_id = uuid.uuid4()
    prod_table_grant_id = uuid.uuid4()
    namespace_grant_ok_id = uuid.uuid4()
    namespace_grant_missing_id = uuid.uuid4()
    row_filter_id = uuid.uuid4()
    stale_row_filter_id = uuid.uuid4()
    column_mask_id = uuid.uuid4()
    stale_column_mask_id = uuid.uuid4()
    already_revoked_at = datetime(2020, 1, 1, tzinfo=UTC)
    nested_path = f"gold{NAMESPACE_SEPARATOR}nested"

    with engine.begin() as connection:
        connection.execute(
            principals.insert().values(
                id=admin_id,
                type="USER",
                name="admin",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            environments.insert().values(
                id=dev_id,
                name="dev",
                storage_root="s3://bucket/dev",
                storage_options={},
                is_protected=False,
                properties={},
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            namespaces.insert().values(
                id=gold_id, environment_id=dev_id, path="gold", properties={}, owner_id=None
            )
        )
        connection.execute(
            namespaces.insert().values(
                id=nested_ns_id,
                environment_id=dev_id,
                path=nested_path,
                properties={},
                owner_id=None,
            )
        )
        connection.execute(
            tables.insert().values(
                id=orders_id,
                namespace_id=gold_id,
                name="orders",
                table_type="ICEBERG",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            tables.insert().values(
                id=deep_table_id,
                namespace_id=nested_ns_id,
                name="deep",
                table_type="ICEBERG",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            environments.insert().values(
                id=prod_id,
                name="prod",
                storage_root="s3://bucket/prod",
                storage_options={},
                is_protected=False,
                properties={},
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            namespaces.insert().values(
                id=prod_gold_id, environment_id=prod_id, path="gold", properties={}, owner_id=None
            )
        )
        connection.execute(
            tables.insert().values(
                id=prod_orders_id,
                namespace_id=prod_gold_id,
                name="orders",
                table_type="ICEBERG",
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            grants.insert().values(
                id=table_grant_id,
                principal_id=admin_id,
                role="VIEWER",
                kind="TABLE",
                environment="dev",
                namespace="gold",
                table="orders",
                granted_by=admin_id,
                granted_at=now,
            )
        )
        connection.execute(
            grants.insert().values(
                id=env_grant_id,
                principal_id=admin_id,
                role="VIEWER",
                kind="ENVIRONMENT",
                environment="dev",
                namespace=None,
                table=None,
                granted_by=admin_id,
                granted_at=now,
            )
        )
        connection.execute(
            grants.insert().values(
                id=instance_grant_id,
                principal_id=admin_id,
                role="ADMIN",
                kind="INSTANCE",
                environment=None,
                namespace=None,
                table=None,
                granted_by=admin_id,
                granted_at=now,
            )
        )
        connection.execute(
            grants.insert().values(
                id=ghost_grant_id,
                principal_id=admin_id,
                role="VIEWER",
                kind="TABLE",
                environment="dev",
                namespace="gold",
                table="ghost",
                granted_by=admin_id,
                granted_at=now,
            )
        )
        connection.execute(
            grants.insert().values(
                id=already_revoked_grant_id,
                principal_id=admin_id,
                role="VIEWER",
                kind="TABLE",
                environment="dev",
                namespace="gold",
                table="ghost",
                granted_by=admin_id,
                granted_at=now,
                revoked_by=admin_id,
                revoked_at=already_revoked_at,
            )
        )
        connection.execute(
            grants.insert().values(
                id=nested_table_grant_id,
                principal_id=admin_id,
                role="VIEWER",
                kind="TABLE",
                environment="dev",
                namespace=nested_path,
                table="deep",
                granted_by=admin_id,
                granted_at=now,
            )
        )
        connection.execute(
            grants.insert().values(
                id=prod_table_grant_id,
                principal_id=admin_id,
                role="VIEWER",
                kind="TABLE",
                environment="prod",
                namespace="gold",
                table="orders",
                granted_by=admin_id,
                granted_at=now,
            )
        )
        connection.execute(
            grants.insert().values(
                id=namespace_grant_ok_id,
                principal_id=admin_id,
                role="VIEWER",
                kind="NAMESPACE",
                environment="dev",
                namespace="gold",
                table=None,
                granted_by=admin_id,
                granted_at=now,
            )
        )
        connection.execute(
            grants.insert().values(
                id=namespace_grant_missing_id,
                principal_id=admin_id,
                role="VIEWER",
                kind="NAMESPACE",
                environment="dev",
                namespace="ghost_ns",
                table=None,
                granted_by=admin_id,
                granted_at=now,
            )
        )
        connection.execute(
            row_filters.insert().values(
                id=row_filter_id,
                principal_id=admin_id,
                environment="dev",
                namespace="gold",
                table="orders",
                expression="1=1",
                created_by=admin_id,
                created_at=now,
            )
        )
        connection.execute(
            row_filters.insert().values(
                id=stale_row_filter_id,
                principal_id=admin_id,
                environment="dev",
                namespace="gold",
                table="ghost",
                expression="1=1",
                created_by=admin_id,
                created_at=now,
            )
        )
        connection.execute(
            column_masks.insert().values(
                id=column_mask_id,
                principal_id=admin_id,
                environment="dev",
                namespace="gold",
                table="orders",
                column="ssn",
                expression="'***'",
                created_by=admin_id,
                created_at=now,
            )
        )
        connection.execute(
            column_masks.insert().values(
                id=stale_column_mask_id,
                principal_id=admin_id,
                environment="dev",
                namespace="gold",
                table="ghost",
                column="ssn",
                expression="'***'",
                created_by=admin_id,
                created_at=now,
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    grants_after = sa.table(
        "grants",
        sa.column("id", sa.Uuid()),
        sa.column("environment_id", sa.Uuid()),
        sa.column("table_id", sa.Uuid()),
        sa.column("revoked_at", sa.DateTime(timezone=True)),
        sa.column("revoked_by", sa.Uuid()),
    )
    row_filters_after = sa.table(
        "row_filters",
        sa.column("id", sa.Uuid()),
        sa.column("environment_id", sa.Uuid()),
        sa.column("table_id", sa.Uuid()),
        sa.column("revoked_at", sa.DateTime(timezone=True)),
    )
    column_masks_after = sa.table(
        "column_masks",
        sa.column("id", sa.Uuid()),
        sa.column("environment_id", sa.Uuid()),
        sa.column("table_id", sa.Uuid()),
        sa.column("revoked_at", sa.DateTime(timezone=True)),
    )
    audit_events_after = sa.table(
        "audit_events",
        sa.column("id", sa.Uuid()),
        sa.column("occurred_at", sa.DateTime(timezone=True)),
        sa.column("event_type", sa.String()),
        sa.column("detail", sa.JSON()),
    )

    with engine.begin() as connection:
        table_grant_row = connection.execute(
            sa.select(grants_after.c.environment_id, grants_after.c.table_id).where(
                grants_after.c.id == table_grant_id
            )
        ).one()
        assert table_grant_row.environment_id == dev_id
        assert table_grant_row.table_id == orders_id

        env_grant_row = connection.execute(
            sa.select(grants_after.c.environment_id, grants_after.c.table_id).where(
                grants_after.c.id == env_grant_id
            )
        ).one()
        assert env_grant_row.environment_id == dev_id
        assert env_grant_row.table_id is None

        instance_grant_row = connection.execute(
            sa.select(grants_after.c.environment_id, grants_after.c.table_id).where(
                grants_after.c.id == instance_grant_id
            )
        ).one()
        assert instance_grant_row.environment_id is None
        assert instance_grant_row.table_id is None

        ghost_grant_row = connection.execute(
            sa.select(grants_after.c.revoked_at, grants_after.c.revoked_by).where(
                grants_after.c.id == ghost_grant_id
            )
        ).one()
        assert ghost_grant_row.revoked_at is not None
        assert ghost_grant_row.revoked_by is None

        # A grant already revoked before the migration keeps its original
        # revoked_at/revoked_by untouched, rather than being overwritten.
        already_revoked_row = connection.execute(
            sa.select(grants_after.c.revoked_at, grants_after.c.revoked_by).where(
                grants_after.c.id == already_revoked_grant_id
            )
        ).one()
        # Plain `sa.DateTime(timezone=True)` (not the app's `UTCDateTime`)
        # reads back naive on SQLite, so reattach UTC before comparing.
        assert already_revoked_row.revoked_at.replace(tzinfo=UTC) == already_revoked_at
        assert already_revoked_row.revoked_by == admin_id

        # A multi-level (unit-separator-joined) namespace path resolves.
        nested_table_grant_row = connection.execute(
            sa.select(grants_after.c.environment_id, grants_after.c.table_id).where(
                grants_after.c.id == nested_table_grant_id
            )
        ).one()
        assert nested_table_grant_row.environment_id == dev_id
        assert nested_table_grant_row.table_id == deep_table_id

        # The same table name in two environments binds to each
        # environment's own table id.
        prod_table_grant_row = connection.execute(
            sa.select(grants_after.c.environment_id, grants_after.c.table_id).where(
                grants_after.c.id == prod_table_grant_id
            )
        ).one()
        assert prod_table_grant_row.environment_id == prod_id
        assert prod_table_grant_row.table_id == prod_orders_id
        assert prod_table_grant_row.table_id != orders_id

        # A NAMESPACE-kind grant on an existing namespace is kept.
        namespace_grant_ok_row = connection.execute(
            sa.select(
                grants_after.c.environment_id, grants_after.c.table_id, grants_after.c.revoked_at
            ).where(grants_after.c.id == namespace_grant_ok_id)
        ).one()
        assert namespace_grant_ok_row.environment_id == dev_id
        assert namespace_grant_ok_row.table_id is None
        assert namespace_grant_ok_row.revoked_at is None

        # A NAMESPACE-kind grant on a missing namespace is revoked.
        namespace_grant_missing_row = connection.execute(
            sa.select(grants_after.c.revoked_at, grants_after.c.revoked_by).where(
                grants_after.c.id == namespace_grant_missing_id
            )
        ).one()
        assert namespace_grant_missing_row.revoked_at is not None
        assert namespace_grant_missing_row.revoked_by is None

        row_filter_row = connection.execute(
            sa.select(row_filters_after.c.environment_id, row_filters_after.c.table_id).where(
                row_filters_after.c.id == row_filter_id
            )
        ).one()
        assert row_filter_row.environment_id == dev_id
        assert row_filter_row.table_id == orders_id

        stale_row_filter_row = connection.execute(
            sa.select(
                row_filters_after.c.environment_id,
                row_filters_after.c.table_id,
                row_filters_after.c.revoked_at,
            ).where(row_filters_after.c.id == stale_row_filter_id)
        ).one()
        assert stale_row_filter_row.environment_id is None
        assert stale_row_filter_row.table_id is None
        assert stale_row_filter_row.revoked_at is not None

        column_mask_row = connection.execute(
            sa.select(column_masks_after.c.environment_id, column_masks_after.c.table_id).where(
                column_masks_after.c.id == column_mask_id
            )
        ).one()
        assert column_mask_row.environment_id == dev_id
        assert column_mask_row.table_id == orders_id

        stale_column_mask_row = connection.execute(
            sa.select(
                column_masks_after.c.environment_id,
                column_masks_after.c.table_id,
                column_masks_after.c.revoked_at,
            ).where(column_masks_after.c.id == stale_column_mask_id)
        ).one()
        assert stale_column_mask_row.environment_id is None
        assert stale_column_mask_row.table_id is None
        assert stale_column_mask_row.revoked_at is not None

        # `audit_events.event_type` accepts a 30-character value.
        long_event_type = "ENVIRONMENT_PROTECTION_CHANGED"
        assert len(long_event_type) == 30
        connection.execute(
            audit_events_after.insert().values(
                id=uuid.uuid4(),
                occurred_at=now,
                event_type=long_event_type,
                detail={},
            )
        )
        stored_event_type = connection.execute(
            sa.select(audit_events_after.c.event_type).where(
                audit_events_after.c.event_type == long_event_type
            )
        ).scalar_one()
        assert stored_event_type == long_event_type
    engine.dispose()

    command.downgrade(config, _REVISION_0010)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        grant_columns = {col["name"] for col in inspector.get_columns("grants")}
        row_filter_columns = {col["name"] for col in inspector.get_columns("row_filters")}
        column_mask_columns = {col["name"] for col in inspector.get_columns("column_masks")}
        table_columns = {col["name"] for col in inspector.get_columns("tables")}
        query_run_columns = {col["name"] for col in inspector.get_columns("query_runs")}

        assert "environment_id" not in grant_columns
        assert "table_id" not in grant_columns
        assert "environment_id" not in row_filter_columns
        assert "table_id" not in row_filter_columns
        assert "environment_id" not in column_mask_columns
        assert "table_id" not in column_mask_columns
        assert "external_claim" not in table_columns
        assert "task_token_id" not in query_run_columns

        # `event_type` stays widened on downgrade: audit rows already
        # recorded with a >24-character event type must not become
        # unrepresentable, and older code only validates the enum value
        # in Python, never the column's SQL length.
        event_type_column = next(
            col for col in inspector.get_columns("audit_events") if col["name"] == "event_type"
        )
        event_type = event_type_column["type"]
        assert isinstance(event_type, sa.String)
        assert event_type.length == 64
    finally:
        engine.dispose()


def test_0011_backfills_external_claim_for_parseable_legacy_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0011` backfills `tables.external_claim` for pre-existing `EXTERNAL` rows.

    `external_claim` is a brand-new nullable column, so every `EXTERNAL`
    row that predates it starts `NULL` — a fail-closed state
    (`check_claim_free`, `lakana_worker.pipeline`) that would otherwise
    strand every table created before this fix. This reproduces three
    such rows inserted directly at revision `0010` (before the column
    exists): one whose stored `location_pattern` parses under the new
    `raw/<dir...>/<glob>` rule and should be backfilled, one whose pattern
    does not parse (outside `raw/`) and must be left `NULL` rather than
    guessed at, and an `ICEBERG` row (no `external_config` at all) to prove
    the backfill does not choke on a row that was never external.

    Parameters
    ----------
    tmp_path
        Pytest-provided temporary directory, holding the throwaway SQLite
        file the migration is applied to.
    monkeypatch
        Used to point `lakana_core.config.Settings` (read by
        `migrations/env.py`) at that file for the duration of the test.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))

    command.upgrade(config, _REVISION_0010)

    engine = create_engine(f"sqlite:///{db_path}")
    now = datetime.now(UTC)

    environments = sa.table(
        "environments",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("storage_root", sa.String()),
        sa.column("storage_options", sa.JSON()),
        sa.column("is_protected", sa.Boolean()),
        sa.column("properties", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    namespaces = sa.table(
        "namespaces",
        sa.column("id", sa.Uuid()),
        sa.column("environment_id", sa.Uuid()),
        sa.column("path", sa.String()),
        sa.column("properties", sa.JSON()),
        sa.column("owner_id", sa.Uuid()),
    )
    tables = sa.table(
        "tables",
        sa.column("id", sa.Uuid()),
        sa.column("namespace_id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("table_type", sa.String()),
        sa.column("external_config", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    dev_id = uuid.uuid4()
    raw_ns_id = uuid.uuid4()
    conforming_id = uuid.uuid4()
    nonconforming_id = uuid.uuid4()
    iceberg_id = uuid.uuid4()

    with engine.begin() as connection:
        connection.execute(
            environments.insert().values(
                id=dev_id,
                name="dev",
                storage_root="s3://bucket/dev",
                storage_options={},
                is_protected=False,
                properties={},
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            namespaces.insert().values(
                id=raw_ns_id, environment_id=dev_id, path="raw", properties={}, owner_id=None
            )
        )
        connection.execute(
            tables.insert().values(
                id=conforming_id,
                namespace_id=raw_ns_id,
                name="customers",
                table_type="EXTERNAL",
                external_config={
                    "location_pattern": "raw/customers/*.parquet",
                    "format": "parquet",
                    "schema": {"id": "int64"},
                },
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            tables.insert().values(
                id=nonconforming_id,
                namespace_id=raw_ns_id,
                name="gold_customers",
                table_type="EXTERNAL",
                external_config={
                    "location_pattern": "gold/customers/*.parquet",
                    "format": "parquet",
                    "schema": {"id": "int64"},
                },
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            tables.insert().values(
                id=iceberg_id,
                namespace_id=raw_ns_id,
                name="orders",
                table_type="ICEBERG",
                external_config=None,
                created_at=now,
                updated_at=now,
            )
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    tables_after = sa.table(
        "tables", sa.column("id", sa.Uuid()), sa.column("external_claim", sa.String())
    )
    try:
        with engine.begin() as connection:
            conforming_claim = connection.execute(
                sa.select(tables_after.c.external_claim).where(tables_after.c.id == conforming_id)
            ).scalar_one()
            assert conforming_claim == "raw/customers"

            nonconforming_claim = connection.execute(
                sa.select(tables_after.c.external_claim).where(
                    tables_after.c.id == nonconforming_id
                )
            ).scalar_one()
            assert nonconforming_claim is None

            iceberg_claim = connection.execute(
                sa.select(tables_after.c.external_claim).where(tables_after.c.id == iceberg_id)
            ).scalar_one()
            assert iceberg_claim is None
    finally:
        engine.dispose()


_REVISION_0011 = "75c7a7bb948a"
_REVISION_0012 = "c8d4e2f1a6b3"
_REVISION_0013 = "d1e5f3a7b9c2"


@pytest.mark.slow
def test_0012_adds_the_m5a_schema_and_downgrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0012` adds `lineage_edges`, the write-run columns and `view_definition`.

    Parameters
    ----------
    tmp_path
        Pytest-provided temporary directory, holding the throwaway SQLite file.
    monkeypatch
        Used to point `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert "lineage_edges" in inspector.get_table_names()
        run_columns = {column["name"] for column in inspector.get_columns("query_runs")}
        assert {
            "write_mode",
            "target_name",
            "target_table_id",
            "incremental_key",
            "evolve_schema",
            "watermark",
            "rows_written",
            "snapshot_id",
            "schema_diff",
            "maintenance",
        } <= run_columns
        assert "view_definition" in {c["name"] for c in inspector.get_columns("tables")}
        edge_indexes = {index["name"] for index in inspector.get_indexes("lineage_edges")}
        assert {
            "ix_lineage_edges_source_table_id",
            "ix_lineage_edges_target_table_id",
        } <= edge_indexes
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0011)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert "lineage_edges" not in inspector.get_table_names()
        assert "write_mode" not in {c["name"] for c in inspector.get_columns("query_runs")}
        assert "view_definition" not in {c["name"] for c in inspector.get_columns("tables")}
    finally:
        engine.dispose()


@pytest.mark.slow
def test_0012_downgrade_leaves_nothing_the_previous_release_cannot_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review I2: a `MAINTAIN_TABLE` grant must not survive `downgrade -1`.

    0.4.x loads every active grant of a subject through a non-native enum
    with no `MAINTAIN_TABLE` member, so a leftover one raises `LookupError`
    and breaks every request by that principal.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0012)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            principal_id = seed_m5a_rows(connection)
    finally:
        engine.dispose()

    command.downgrade(config, "-1")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            assert_loads_as_pre_m5a(connection, principal_id)
            assert "lineage_edges" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_0013_adds_the_m5b_schema_and_downgrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0013` adds the four M5b tables and its downgrade removes them and M5b grants.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0013)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = set(inspect(engine).get_table_names())
        assert {"repositories", "deploy_runs", "job_versions", "environment_deployments"} <= names
        with engine.begin() as connection:
            principal_id = seed_m5a_rows(connection)
            for action in ("DEPLOY", "MANAGE_REPOSITORY", "READ_ENVIRONMENT"):
                connection.execute(
                    sa.text(
                        "INSERT INTO grants (id, principal_id, action, kind, granted_by, "
                        "granted_at) VALUES (:id, :p, :a, 'INSTANCE', :p, :t)"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "p": principal_id.hex,
                        "a": action,
                        "t": datetime.now(UTC),
                    },
                )
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0012)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = set(inspect(engine).get_table_names())
        assert (
            not {"repositories", "deploy_runs", "job_versions", "environment_deployments"} & names
        )
        with engine.connect() as connection:
            actions = connection.execute(sa.text("SELECT action FROM grants")).scalars().all()
        assert "DEPLOY" not in actions
        assert "MANAGE_REPOSITORY" not in actions
        assert "READ_ENVIRONMENT" in actions
    finally:
        engine.dispose()


_M5C1_TABLES = {"runs", "task_runs", "task_attempts", "scheduler_lease"}


def test_0014_adds_the_m5c1_schema_and_downgrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0014` adds the run tables and columns; its downgrade removes them and RUN grants.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0014)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) >= _M5C1_TABLES
        assert "run_as_principal_id" in {
            c["name"] for c in inspector.get_columns("environment_deployments")
        }
        assert {"task_run_id", "break_glass_reason"} <= {
            c["name"] for c in inspector.get_columns("query_runs")
        }
        assert "ready_at" in {c["name"] for c in inspector.get_columns("task_runs")}
        assert "ix_runs_one_active" in {i["name"] for i in inspector.get_indexes("runs")}
        with engine.begin() as connection:
            lease = connection.execute(sa.text("SELECT id, holder FROM scheduler_lease")).all()
            assert [tuple(row) for row in lease] == [(1, None)]
            principal_id = seed_m5a_rows(connection)
            connection.execute(
                sa.text(
                    "INSERT INTO grants (id, principal_id, action, kind, granted_by, granted_at) "
                    "VALUES (:id, :p, 'RUN', 'INSTANCE', :p, :t)"
                ),
                {"id": uuid.uuid4().hex, "p": principal_id.hex, "t": datetime.now(UTC)},
            )
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0013)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert not _M5C1_TABLES & set(inspector.get_table_names())
        assert "run_as_principal_id" not in {
            c["name"] for c in inspector.get_columns("environment_deployments")
        }
        assert not {"task_run_id", "break_glass_reason"} & {
            c["name"] for c in inspector.get_columns("query_runs")
        }
        with engine.connect() as connection:
            actions = connection.execute(sa.text("SELECT action FROM grants")).scalars().all()
        assert "RUN" not in actions
    finally:
        engine.dispose()


_REVISION_0014 = "e7f2a9c4b1d3"


@pytest.mark.slow
def test_0015_adds_the_claim_counter_seeded_from_attempt_and_downgrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0015` adds `task_runs.claim_count` (= `attempt` for existing rows).

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0014)
    engine = create_engine(f"sqlite:///{db_path}")
    task_id = uuid.uuid4().hex
    try:
        assert "claim_count" not in {c["name"] for c in inspect(engine).get_columns("task_runs")}
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO task_runs (id, run_id, asset, status, attempt, max_attempts, "
                    "timeout_seconds, retry_delay_seconds, upstream_json, conflict_retried) "
                    "VALUES (:id, :run, 'silver.a', 'RUNNING', 3, 5, 60, 0, '[]', 0)"
                ),
                {"id": task_id, "run": uuid.uuid4().hex},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            rows = connection.execute(sa.text("SELECT attempt, claim_count FROM task_runs")).all()
        assert [tuple(row) for row in rows] == [(3, 3)]
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0014)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "claim_count" not in {c["name"] for c in inspect(engine).get_columns("task_runs")}
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT attempt FROM task_runs")).scalar_one() == 3
    finally:
        engine.dispose()


def test_running_migrations_leaves_existing_loggers_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`env.py` configures logging without disabling loggers that already exist.

    A worker or server that migrates in-process would otherwise lose every
    module logger created before the migration ran.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    existing = logging.getLogger("lakana_test.created_before_the_migration")
    existing.disabled = False
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{tmp_path / 'migrated.db'}")
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    assert existing.disabled is False


_REVISION_0015 = "f4b8d2a6c9e1"


@pytest.mark.slow
def test_0016_adds_the_bound_token_columns_and_its_downgrade_deletes_bound_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0016`: two nullable columns, a CHECK, and a downgrade that drops bound tokens.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0015)
    engine = create_engine(f"sqlite:///{db_path}")

    def columns() -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns("task_tokens")}

    insert = sa.text(
        "INSERT INTO task_tokens (id, principal_id, environment, asset_scope, key_id, "
        "secret_hash, issued_at, expires_at) "
        "VALUES (:id, :p, 'dev', '[]', :key, :hash, :now, :now)"
    )
    now = datetime.now(UTC)
    try:
        assert not {"task_run_id", "claim"} & columns()
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            for key in ("plain", "bound"):
                connection.execute(
                    insert,
                    {
                        "id": uuid.uuid4().hex,
                        "p": uuid.uuid4().hex,
                        "key": key,
                        "hash": "0" * 64,
                        "now": now,
                    },
                )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert {"task_run_id", "claim"} <= columns()
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            rows = connection.execute(sa.text("SELECT task_run_id, claim FROM task_tokens")).all()
            assert [tuple(row) for row in rows] == [(None, None), (None, None)]
            connection.execute(
                sa.text(
                    "UPDATE task_tokens SET task_run_id = :t, claim = 1 WHERE key_id = 'bound'"
                ),
                {"t": uuid.uuid4().hex},
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    sa.text("UPDATE task_tokens SET claim = 1 WHERE key_id = 'plain'")
                )
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0015)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert not {"task_run_id", "claim"} & columns()
        with engine.connect() as connection:
            keys = connection.execute(sa.text("SELECT key_id FROM task_tokens")).scalars().all()
        assert keys == ["plain"]
    finally:
        engine.dispose()


_REVISION_0016 = "a6c3e9d1f7b2"


@pytest.mark.slow
def test_0017_adds_the_nullable_error_cause_and_downgrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0017`: `query_runs.error_cause` is nullable; existing rows keep `NULL`.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0016)
    engine = create_engine(f"sqlite:///{db_path}")
    run_id = uuid.uuid4().hex

    def columns() -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns("query_runs")}

    try:
        assert "error_cause" not in columns()
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO query_runs (id, environment_id, sql_text, status, error_message,"
                    " created_at, updated_at) VALUES (:id, :env, 'SELECT 1', 'FAILED', 'old',"
                    " :now, :now)"
                ),
                {"id": run_id, "env": uuid.uuid4().hex, "now": "2026-10-05 09:00:00"},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "error_cause" in columns()
        nullable = {c["name"]: c["nullable"] for c in inspect(engine).get_columns("query_runs")}[
            "error_cause"
        ]
        assert nullable is True
        with engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT error_message, error_cause FROM query_runs")
            ).one()
        assert tuple(row) == ("old", None)
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0016)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "error_cause" not in columns()
        with engine.connect() as connection:
            stored = connection.execute(
                sa.text("SELECT error_message FROM query_runs")
            ).scalar_one()
        assert stored == "old"
    finally:
        engine.dispose()


_REVISION_0017 = "c8e1a5d7f3b9"


@pytest.mark.slow
def test_0018_adds_the_artifact_tables_and_its_downgrade_removes_artifact_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0018`: three tables; the downgrade deletes `ARTIFACT` rows of `tables`.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    new = {"artifact_versions", "artifact_uploads", "artifact_blobs"}

    def tables() -> set[str]:
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            return set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

    command.upgrade(config, _REVISION_0017)
    assert not new & tables()
    command.upgrade(config, "head")
    assert new <= tables()
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            for name, kind in (("model", "ARTIFACT"), ("events", "ICEBERG")):
                connection.execute(
                    sa.text(
                        "INSERT INTO tables (id, namespace_id, name, table_type, created_at,"
                        " updated_at) VALUES (:id, :ns, :name, :kind, :now, :now)"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "ns": uuid.uuid4().hex,
                        "name": name,
                        "kind": kind,
                        "now": "2026-10-04 09:00:00",
                    },
                )
    finally:
        engine.dispose()
    command.downgrade(config, _REVISION_0017)
    assert not new & tables()
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            left = connection.execute(sa.text("SELECT name FROM tables")).scalars().all()
        assert left == ["events"]
    finally:
        engine.dispose()


_REVISION_0018 = "e2a7c4f1b6d8"


@pytest.mark.slow
def test_0019_adds_the_format_version_and_key_id_and_downgrade_drops_new_format_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0019`: old rows become format 1 under the bare `env` key id.

    The downgrade deletes every row of a newer format first (the previous release cannot
    read one) and leaves the format-1 rows byte for byte.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    old_id, new_id = uuid.uuid4().hex, uuid.uuid4().hex

    def row(row_id: str) -> dict[str, object]:
        return {
            "id": row_id,
            "env": uuid.uuid4().hex,
            "name": row_id[:8],
            "ct": b"ct",
            "n": b"n" * 12,
            "w": b"w",
            "by": uuid.uuid4().hex,
            "now": "2026-10-04 09:00:00",
        }

    def columns() -> set[str]:
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            return {c["name"] for c in inspect(engine).get_columns("secrets")}
        finally:
            engine.dispose()

    command.upgrade(config, _REVISION_0018)
    assert not {"format_version", "key_id"} & columns()
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce,"
                    " wrapped_data_key, created_by, created_at) VALUES"
                    " (:id, :env, :name, :ct, :n, :w, :by, :now)"
                ),
                row(old_id),
            )
        command.upgrade(config, "head")
        assert {"format_version", "key_id"} <= columns()
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce,"
                    " wrapped_data_key, created_by, created_at, format_version, key_id) VALUES"
                    " (:id, :env, :name, :ct, :n, :w, :by, :now, 2, :kid)"
                ),
                {**row(new_id), "kid": "env:0123456789abcdef"},
            )
            rows = connection.execute(
                sa.text("SELECT id, format_version, key_id FROM secrets ORDER BY format_version")
            ).all()
        assert [(r[0], r[1], r[2]) for r in rows] == [
            (old_id, 1, "env"),
            (new_id, 2, "env:0123456789abcdef"),
        ]
        command.downgrade(config, _REVISION_0018)
        assert not {"format_version", "key_id"} & columns()
        with engine.connect() as connection:
            left = connection.execute(sa.text("SELECT id, ciphertext FROM secrets")).all()
        assert [(r[0], bytes(r[1])) for r in left] == [(old_id, b"ct")]
        command.upgrade(config, "head")
        with engine.connect() as connection:
            version = connection.execute(sa.text("SELECT format_version FROM secrets")).scalar()
        assert version == 1
    finally:
        engine.dispose()


_REVISION_0019 = "b5d9f2a8c4e7"


def _utc_text() -> str:
    """Say now as the text SQLite stores a datetime as.

    Returns
    -------
    str
        The current UTC time, which avoids the deprecated default datetime adapter.
    """
    return datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" ")


@pytest.mark.slow
def test_0020_adds_package_indexes_and_widens_the_action_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0020`: the registry of indexes, and room for `MANAGE_PACKAGE_INDEX`."""
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0019)
    engine = create_engine(f"sqlite:///{db_path}")
    principal = uuid.uuid4().hex
    grant = (
        "INSERT INTO grants (id, principal_id, action, kind, granted_by, granted_at) "
        "VALUES (:id, :p, :a, 'INSTANCE', :p, :t)"
    )
    try:
        assert "package_indexes" not in inspect(engine).get_table_names()
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO principals (id, type, name, is_active, created_at, updated_at) "
                    "VALUES (:id, 'USER', 'ada', 1, :t, :t)"
                ),
                {"id": principal, "t": _utc_text()},
            )
            connection.execute(
                sa.text(grant),
                {
                    "id": uuid.uuid4().hex,
                    "p": principal,
                    "a": "READ_AUDIT_LOG",
                    "t": _utc_text(),
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    insert = (
        "INSERT INTO package_indexes (id, name, url, credential_secret, created_by, "
        "created_at, updated_at) VALUES (:id, :n, :u, NULL, :p, :t, :t)"
    )
    try:
        columns = {c["name"]: c for c in inspect(engine).get_columns("package_indexes")}
        assert set(columns) == {
            "id",
            "name",
            "url",
            "credential_secret",
            "created_by",
            "created_at",
            "updated_at",
        }
        assert cast(sa.String, columns["url"]["type"]).length == 1024
        action = next(c for c in inspect(engine).get_columns("grants") if c["name"] == "action")
        assert cast(sa.String, action["type"]).length == 20
        with engine.begin() as connection:
            now = _utc_text()
            connection.execute(
                sa.text(insert),
                {
                    "id": uuid.uuid4().hex,
                    "n": "team",
                    "u": "https://a/simple",
                    "p": principal,
                    "t": now,
                },
            )
            connection.execute(
                sa.text(grant),
                {"id": uuid.uuid4().hex, "p": principal, "a": "MANAGE_PACKAGE_INDEX", "t": now},
            )
        for name, url in (("team", "https://b/simple"), ("other", "https://a/simple")):
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(
                    sa.text(insert),
                    {
                        "id": uuid.uuid4().hex,
                        "n": name,
                        "u": url,
                        "p": principal,
                        "t": _utc_text(),
                    },
                )
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0019)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "package_indexes" not in inspect(engine).get_table_names()
        action = next(c for c in inspect(engine).get_columns("grants") if c["name"] == "action")
        assert cast(sa.String, action["type"]).length == 18
        with engine.connect() as connection:
            actions = connection.execute(sa.text("SELECT action FROM grants")).scalars().all()
        assert actions == ["READ_AUDIT_LOG"]
    finally:
        engine.dispose()


_REVISION_0020 = "c3f8a6d1e9b4"


@pytest.mark.slow
def test_0021_adds_cancel_requested_and_its_downgrade_turns_cancelled_runs_into_failed_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0021`: `cancel_requested` is false for old rows; the downgrade maps `CANCELLED`.

    A release before this one cannot load the status `CANCELLED`, so the downgrade records such
    a run as `FAILED` with the cause `cancelled`, which is how that release recorded a cancel.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0020)
    engine = create_engine(f"sqlite:///{db_path}")
    first, second = uuid.uuid4().hex, uuid.uuid4().hex

    def columns() -> dict[str, dict[str, object]]:
        return {c["name"]: dict(c) for c in inspect(engine).get_columns("query_runs")}

    def insert(connection: sa.Connection, run_id: str, status: str, *, cancel: bool) -> None:
        names = "id, environment_id, sql_text, status, created_at, updated_at"
        values = ":id, :env, 'SELECT 1', :status, :now, :now"
        if cancel:
            names, values = names + ", cancel_requested", values + ", 1"
        connection.execute(
            sa.text(f"INSERT INTO query_runs ({names}) VALUES ({values})"),
            {"id": run_id, "env": uuid.uuid4().hex, "status": status, "now": _utc_text()},
        )

    try:
        assert "cancel_requested" not in columns()
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            insert(connection, first, "SUCCEEDED", cancel=False)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert columns()["cancel_requested"]["nullable"] is False
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            old = connection.execute(
                sa.text("SELECT cancel_requested FROM query_runs WHERE id = :id"), {"id": first}
            ).scalar_one()
            assert not old
            insert(connection, second, "CANCELLED", cancel=True)
            # A server of the previous release inserts without the column during an upgrade.
            insert(connection, uuid.uuid4().hex, "QUEUED", cancel=False)
            assert not connection.execute(
                sa.text("SELECT max(cancel_requested) FROM query_runs WHERE status = 'QUEUED'")
            ).scalar_one()
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0020)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "cancel_requested" not in columns()
        with engine.connect() as connection:
            rows = {
                row[0]: tuple(row[1:])
                for row in connection.execute(
                    sa.text("SELECT id, status, error_message, error_cause FROM query_runs")
                )
            }
        assert rows[second] == ("FAILED", "The query was cancelled.", "cancelled")
        assert rows[first] == ("SUCCEEDED", None, None)
    finally:
        engine.dispose()


_REVISION_0021 = "e4b7d2a9c6f1"


@pytest.mark.slow
def test_0022_adds_workers_the_worker_ids_and_the_status_index_and_downgrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0022`: the `workers` table, nullable `worker_id` columns and the status index.

    Old rows get `NULL`; the downgrade removes all of it and keeps the rows.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0021)
    engine = create_engine(f"sqlite:///{db_path}")
    query_id, deploy_id = uuid.uuid4().hex, uuid.uuid4().hex

    def column_names(table: str) -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns(table)}

    try:
        assert not inspect(engine).has_table("workers")
        assert "worker_id" not in column_names("query_runs")
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO query_runs (id, environment_id, sql_text, status, created_at,"
                    " updated_at) VALUES (:id, :env, 'SELECT 1', 'RUNNING', :now, :now)"
                ),
                {"id": query_id, "env": uuid.uuid4().hex, "now": _utc_text()},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO deploy_runs (id, environment_id, repository_id, kind, status,"
                    " created_at) VALUES (:id, :env, :repo, 'deploy', 'RUNNING', :now)"
                ),
                {
                    "id": deploy_id,
                    "env": uuid.uuid4().hex,
                    "repo": uuid.uuid4().hex,
                    "now": _utc_text(),
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert column_names("workers") == {
            "id",
            "host",
            "started_at",
            "version",
            "heartbeat_at",
            "takes",
            "cache_environments_bytes",
            "cache_packages_bytes",
            "cache_tasks_bytes",
            "cache_complete",
            "cache_measured_at",
        }
        for table in ("query_runs", "deploy_runs"):
            column = {c["name"]: c for c in inspect(engine).get_columns(table)}["worker_id"]
            assert column["nullable"] is True
        indexes = {i["name"]: i for i in inspect(engine).get_indexes("query_runs")}
        assert indexes["ix_query_runs_status_created_at"]["column_names"] == [
            "status",
            "created_at",
        ]
        assert indexes["ix_query_runs_environment_id_finished_at"]["column_names"] == [
            "environment_id",
            "finished_at",
        ]
        with engine.connect() as connection:
            for table, run_id in (("query_runs", query_id), ("deploy_runs", deploy_id)):
                found = connection.execute(
                    sa.text(f"SELECT worker_id FROM {table} WHERE id = :id"), {"id": run_id}
                ).all()
                assert found == [(None,)]
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0021)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert not inspect(engine).has_table("workers")
        assert "worker_id" not in column_names("query_runs")
        assert "worker_id" not in column_names("deploy_runs")
        assert not {
            "ix_query_runs_status_created_at",
            "ix_query_runs_environment_id_finished_at",
        } & {i["name"] for i in inspect(engine).get_indexes("query_runs")}
        with engine.connect() as connection:
            for table, run_id in (("query_runs", query_id), ("deploy_runs", deploy_id)):
                assert (
                    connection.execute(
                        sa.text(f"SELECT count(*) FROM {table} WHERE id = :id"), {"id": run_id}
                    ).scalar_one()
                    == 1
                )
    finally:
        engine.dispose()


_REVISION_0022 = "f6c1a8d3b5e2"


@pytest.mark.slow
def test_0023_adds_takes_and_a_worker_registered_before_it_reads_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0023`: `workers.takes`, string 16, not null, server default `all`.

    A row written before it, and a row written without the column afterwards (a worker of the
    previous release), both read `all`; the downgrade drops the column and keeps the row.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0022)
    engine = create_engine(f"sqlite:///{db_path}")
    insert = sa.text(
        "INSERT INTO workers (id, host, started_at, version, heartbeat_at)"
        " VALUES (:id, 'h', :now, '1', :now)"
    )

    def column_names() -> set[str]:
        return {c["name"] for c in inspect(engine).get_columns("workers")}

    try:
        assert "takes" not in column_names()
        with engine.begin() as connection:
            connection.execute(insert, {"id": "before", "now": _utc_text()})
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        column = {c["name"]: c for c in inspect(engine).get_columns("workers")}["takes"]
        assert (str(column["type"]), column["nullable"]) == ("VARCHAR(16)", False)
        with engine.begin() as connection:
            connection.execute(insert, {"id": "after", "now": _utc_text()})
        with engine.connect() as connection:
            rows = dict(connection.execute(sa.text("SELECT id, takes FROM workers")).all())
        assert rows == {"before": "all", "after": "all"}
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0022)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "takes" not in column_names()
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM workers")).scalar_one() == 2
    finally:
        engine.dispose()


_REVISION_0023 = "a7d3c9e1f4b6"

_COLUMNS_0024_ADDS: dict[str, dict[str, tuple[str, bool]]] = {
    "tables": {"unpublished_run_id": ("CHAR(32)", True)},
    "query_runs": {"snapshot_before": ("BIGINT", True)},
    "task_runs": {
        "has_checks": ("BOOLEAN", False),
        "written_table_id": ("CHAR(32)", True),
        "written_snapshot_before": ("BIGINT", True),
        "written_snapshot_after": ("BIGINT", True),
        "written_publishes": ("BOOLEAN", True),
    },
}


@pytest.mark.slow
def test_0024_adds_what_checks_need_and_older_rows_read_as_no_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0024`: the checks of a deploy, the kind of a task, and `check_results`.

    A `deploy_runs` row written before it has no checks; a `task_runs` row written before it
    reads kind `asset`. The downgrade drops the new columns and the table and keeps the rows.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0023)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "checks_json" not in {c["name"] for c in inspect(engine).get_columns("deploy_runs")}
        assert "kind" not in {c["name"] for c in inspect(engine).get_columns("task_runs")}
        assert "check_results" not in inspect(engine).get_table_names()
        for table, names in _COLUMNS_0024_ADDS.items():
            assert not names.keys() & {c["name"] for c in inspect(engine).get_columns(table)}
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at,"
                    " updated_at) VALUES (:id, :ns, 'orders', 'ICEBERG', :now, :now)"
                ),
                {"id": uuid.uuid4().hex, "ns": uuid.uuid4().hex, "now": _utc_text()},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO query_runs (id, environment_id, sql_text, status, created_at,"
                    " updated_at) VALUES (:id, :env, 'SELECT 1', 'RUNNING', :now, :now)"
                ),
                {"id": uuid.uuid4().hex, "env": uuid.uuid4().hex, "now": _utc_text()},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO deploy_runs (id, environment_id, repository_id, kind, status,"
                    " created_at) VALUES (:id, :env, :repo, 'deploy', 'RUNNING', :now)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "env": uuid.uuid4().hex,
                    "repo": uuid.uuid4().hex,
                    "now": _utc_text(),
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO task_runs (id, run_id, asset, status, attempt, claim_count,"
                    " max_attempts, timeout_seconds, retry_delay_seconds, upstream_json,"
                    " conflict_retried) VALUES (:id, :run, 'silver.a', 'PENDING', 0, 0, 1, 60, 0,"
                    " '[]', 0)"
                ),
                {"id": uuid.uuid4().hex, "run": uuid.uuid4().hex},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        column = {c["name"]: c for c in inspect(engine).get_columns("deploy_runs")}["checks_json"]
        assert column["nullable"] is True
        kind = {c["name"]: c for c in inspect(engine).get_columns("task_runs")}["kind"]
        assert (str(kind["type"]), kind["nullable"]) == ("VARCHAR(16)", False)
        inspector = inspect(engine)
        assert [u["column_names"] for u in inspector.get_unique_constraints("check_results")] == [
            ["task_run_id", "check_name"]
        ]
        indexes = {i["name"]: i["column_names"] for i in inspector.get_indexes("check_results")}
        assert indexes["ix_check_results_environment_id_finished_at"] == [
            "environment_id",
            "finished_at",
            "id",
        ]
        assert "ix_check_results_run_id" in indexes
        assert "ix_check_results_table_id" in indexes
        for table, names in _COLUMNS_0024_ADDS.items():
            found = {c["name"]: c for c in inspector.get_columns(table)}
            for name, (type_text, nullable) in names.items():
                assert (str(found[name]["type"]), found[name]["nullable"]) == (type_text, nullable)
        with engine.connect() as connection:
            row = connection.execute(sa.text("SELECT checks_json FROM deploy_runs")).scalar_one()
            assert row is None
            assert connection.execute(sa.text("SELECT kind FROM task_runs")).scalar_one() == "asset"
            stamp = connection.execute(
                sa.text(
                    "SELECT has_checks, written_table_id, written_snapshot_before,"
                    " written_snapshot_after, written_publishes FROM task_runs"
                )
            ).one()
            assert tuple(stamp) == (False, None, None, None, None)
            assert (
                connection.execute(sa.text("SELECT unpublished_run_id FROM tables")).scalar_one()
                is None
            )
            assert (
                connection.execute(sa.text("SELECT snapshot_before FROM query_runs")).scalar_one()
                is None
            )
            default = connection.execute(
                sa.text(
                    "SELECT dflt_value FROM pragma_table_info('task_runs')"
                    " WHERE name = 'has_checks'"
                )
            ).scalar_one()
            assert str(default).lower() in {"0", "false"}
        tables_fks = {tuple(f["constrained_columns"]) for f in inspector.get_foreign_keys("tables")}
        assert ("unpublished_run_id",) not in tables_fks
        task_fks = {
            tuple(f["constrained_columns"]) for f in inspector.get_foreign_keys("task_runs")
        }
        assert ("written_table_id",) not in task_fks
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0023)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "checks_json" not in {c["name"] for c in inspect(engine).get_columns("deploy_runs")}
        assert "kind" not in {c["name"] for c in inspect(engine).get_columns("task_runs")}
        assert "check_results" not in inspect(engine).get_table_names()
        for table, names in _COLUMNS_0024_ADDS.items():
            assert not names.keys() & {c["name"] for c in inspect(engine).get_columns(table)}
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM deploy_runs")).scalar_one() == 1
            assert connection.execute(sa.text("SELECT count(*) FROM task_runs")).scalar_one() == 1
            assert connection.execute(sa.text("SELECT count(*) FROM tables")).scalar_one() == 1
            assert connection.execute(sa.text("SELECT count(*) FROM query_runs")).scalar_one() == 1
    finally:
        engine.dispose()


_REVISION_0024 = "b8e2c5f7a1d4"
_COLUMNS_0025_ADDS = {
    "kept_run_id": "CHAR(32)",
    "kept_task_run_id": "CHAR(32)",
    "kept_check": "VARCHAR(1024)",
}


def test_0025_adds_the_note_of_a_kept_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0025`: the three columns of the note a table keeps after a failed check.

    The columns are nullable and have no foreign key; a `tables` row written before them reads
    as having no note. The downgrade drops them and keeps the rows.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0024)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert not _COLUMNS_0025_ADDS.keys() & {
            c["name"] for c in inspect(engine).get_columns("tables")
        }
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at,"
                    " updated_at) VALUES (:id, :ns, 'orders', 'ICEBERG', :now, :now)"
                ),
                {"id": uuid.uuid4().hex, "ns": uuid.uuid4().hex, "now": _utc_text()},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        found = {c["name"]: c for c in inspector.get_columns("tables")}
        for name, type_text in _COLUMNS_0025_ADDS.items():
            assert (str(found[name]["type"]), found[name]["nullable"]) == (type_text, True)
        constrained = {
            tuple(f["constrained_columns"]) for f in inspector.get_foreign_keys("tables")
        }
        assert not {(name,) for name in _COLUMNS_0025_ADDS} & constrained
        with engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT kept_run_id, kept_task_run_id, kept_check FROM tables")
            ).one()
            assert tuple(row) == (None, None, None)
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0024)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert not _COLUMNS_0025_ADDS.keys() & {
            c["name"] for c in inspect(engine).get_columns("tables")
        }
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM tables")).scalar_one() == 1
    finally:
        engine.dispose()


_REVISION_0025 = "c9f3d6a8b2e5"


def test_0026_adds_the_binding_of_a_query_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0026`: `query_runs.check_result_id`, nullable and without a foreign key.

    A query run written before it reads as bound to no check result. Without a foreign key a
    result that was removed with its run leaves the id on the query run, so the worker fails
    the query with a message instead of reading the table as it is now. The downgrade drops
    the column and keeps the rows.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0025)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "check_result_id" not in {
            c["name"] for c in inspect(engine).get_columns("query_runs")
        }
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO query_runs (id, environment_id, sql_text, status, created_at,"
                    " updated_at) VALUES (:id, :env, 'SELECT 1', 'QUEUED', :now, :now)"
                ),
                {"id": uuid.uuid4().hex, "env": uuid.uuid4().hex, "now": _utc_text()},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        found = {c["name"]: c for c in inspector.get_columns("query_runs")}
        column = found["check_result_id"]
        assert (str(column["type"]), column["nullable"]) == ("CHAR(32)", True)
        constrained = {
            tuple(f["constrained_columns"]) for f in inspector.get_foreign_keys("query_runs")
        }
        assert ("check_result_id",) not in constrained
        with engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT check_result_id FROM query_runs")).scalar_one()
                is None
            )
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0025)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "check_result_id" not in {
            c["name"] for c in inspect(engine).get_columns("query_runs")
        }
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM query_runs")).scalar_one() == 1
    finally:
        engine.dispose()


_REVISION_0026 = "d1a4e7b9c3f6"
_REVISION_0027 = "e2b5f8c1d4a7"


def test_0027_adds_the_set_back_columns_of_a_task_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0027`: `task_runs.setting_back` and `set_back_claims`, both not null.

    A task run written before it reads as not setting its table back and with no set-back
    claim taken. The downgrade drops both columns and keeps the rows.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0026)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = {c["name"] for c in inspect(engine).get_columns("task_runs")}
        assert not names & {"setting_back", "set_back_claims"}
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO task_runs (id, run_id, asset, status, attempt, claim_count,"
                    " max_attempts, timeout_seconds, retry_delay_seconds, upstream_json,"
                    " conflict_retried) VALUES (:id, :run, 'silver.a', 'PENDING', 0, 0, 1, 60, 0,"
                    " '[]', 0)"
                ),
                {"id": uuid.uuid4().hex, "run": uuid.uuid4().hex},
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        found = {c["name"]: c for c in inspect(engine).get_columns("task_runs")}
        assert found["setting_back"]["nullable"] is False
        assert found["set_back_claims"]["nullable"] is False
        with engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT setting_back, set_back_claims FROM task_runs")
            ).one()
        assert (bool(row[0]), row[1]) == (False, 0)
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0026)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = {c["name"] for c in inspect(engine).get_columns("task_runs")}
        assert not names & {"setting_back", "set_back_claims"}
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM task_runs")).scalar_one() == 1
    finally:
        engine.dispose()


_REVISION_0028 = "f3c6a9d2e5b8"


def test_0028_adds_the_job_tables_and_the_run_columns_and_downgrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0028`: `job_schedules`, `job_fires` and four columns of `runs`.

    A run written before it belongs to no job and no trigger, and missed nothing. The
    downgrade drops the tables and the columns and keeps the runs.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0027)
    engine = create_engine(f"sqlite:///{db_path}")
    new_columns = {"job", "trigger", "scheduled_for", "missed"}
    run_id = uuid.uuid4().hex
    try:
        assert not new_columns & {c["name"] for c in inspect(engine).get_columns("runs")}
        assert "job_schedules" not in inspect(engine).get_table_names()
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO runs (id, environment_id, repository_id, job_version_id, kind,"
                    " selection_json, status, cancel_requested, created_at) VALUES (:id, :a, :b,"
                    " :c, 'FULL', '{}', 'SUCCEEDED', 0, '2026-10-13 00:00:00')"
                ),
                {"id": run_id, "a": uuid.uuid4().hex, "b": uuid.uuid4().hex, "c": uuid.uuid4().hex},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0028)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("runs")}
        assert new_columns <= set(columns)
        assert "ix_runs_job" in {i["name"] for i in inspector.get_indexes("runs")}
        assert [columns[n]["nullable"] for n in ("job", "trigger", "scheduled_for", "missed")] == [
            True,
            True,
            True,
            False,
        ]
        assert {c["name"] for c in inspector.get_columns("job_schedules")} == {
            "id",
            "environment_id",
            "repository_id",
            "job",
            "next_fire_at",
            "paused",
            "paused_by",
            "paused_at",
            "pending_start",
            "last_fired_for",
        }
        assert {c["name"] for c in inspector.get_columns("job_fires")} == {
            "id",
            "job_schedule_id",
            "scheduled_for",
            "run_id",
            "created_at",
        }
        with engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT job, trigger, scheduled_for, missed FROM runs")
            ).one()
        assert tuple(row) == (None, None, None, 0)
        deploy_columns = {c["name"]: c for c in inspector.get_columns("deploy_runs")}
        assert deploy_columns["warnings_json"]["nullable"] is True
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0027)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert not new_columns & {c["name"] for c in inspector.get_columns("runs")}
        assert "ix_runs_job" not in {i["name"] for i in inspector.get_indexes("runs")}
        assert "warnings_json" not in {c["name"] for c in inspector.get_columns("deploy_runs")}
        assert not {"job_schedules", "job_fires"} & set(inspector.get_table_names())
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM runs")).scalar_one() == 1
    finally:
        engine.dispose()


_REVISION_0029 = "a4d7b0e3f6c9"
_DELIVERY_COLUMNS = {
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


@pytest.mark.slow
def test_0029_adds_the_notification_deliveries_and_downgrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0029`: `notification_deliveries`, unique on (run, event), indexed for the loop.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0028)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "notification_deliveries" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0029)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("notification_deliveries")}
        assert set(columns) == _DELIVERY_COLUMNS
        assert columns["run_id"]["nullable"] is True
        assert [columns[n]["nullable"] for n in ("job", "event", "state", "attempts")] == [
            False
        ] * 4
        indexes = {i["name"]: i for i in inspector.get_indexes("notification_deliveries")}
        assert indexes["ix_notification_deliveries_state_next_attempt_at"]["column_names"] == [
            "state",
            "next_attempt_at",
        ]
        uniques = inspector.get_unique_constraints("notification_deliveries")
        assert [u["column_names"] for u in uniques] == [["run_id", "event"]]
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0028)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "notification_deliveries" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


_REVISION_0030 = "b5e8c1f4a7d0"


def test_0030_adds_the_unchecked_mark_of_a_kept_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0030`: `tables.kept_unchecked`, not null; an older note reads as a failed check.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0029)
    engine = create_engine(f"sqlite:///{db_path}")
    table_id = uuid.uuid4().hex
    try:
        assert "kept_unchecked" not in {c["name"] for c in inspect(engine).get_columns("tables")}
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at,"
                    " updated_at, kept_run_id, kept_check) VALUES (:id, :ns, 'x', 'ICEBERG',"
                    " '2026-10-15 00:00:00', '2026-10-15 00:00:00', :run, 'x/old')"
                ),
                {"id": table_id, "ns": uuid.uuid4().hex, "run": uuid.uuid4().hex},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0030)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        found = {c["name"]: c for c in inspect(engine).get_columns("tables")}
        assert found["kept_unchecked"]["nullable"] is False
        with engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT kept_unchecked FROM tables WHERE id = :id"), {"id": table_id}
            ).one()
        assert bool(row[0]) is False  # an older note reads as a failed check
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0029)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "kept_unchecked" not in {c["name"] for c in inspect(engine).get_columns("tables")}
    finally:
        engine.dispose()


_REVISION_0031 = "c6f9d2a5b8e1"


def test_0031_adds_the_stack_of_earlier_marks_of_a_kept_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0031`: `tables.kept_earlier`, not null; an existing row starts with no entries.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0030)
    engine = create_engine(f"sqlite:///{db_path}")
    table_id = uuid.uuid4().hex
    kept_run = uuid.uuid4().hex
    in_flight, idle = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        assert "kept_earlier" not in {c["name"] for c in inspect(engine).get_columns("tables")}
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at,"
                    " updated_at, kept_run_id, kept_check) VALUES (:id, :ns, 'x', 'ICEBERG',"
                    " '2026-10-16 00:00:00', '2026-10-16 00:00:00', :run, 'x/old')"
                ),
                {"id": table_id, "ns": uuid.uuid4().hex, "run": kept_run},
            )
            for name, identifier in (("in_flight", in_flight), ("idle", idle)):
                connection.execute(
                    sa.text(
                        "INSERT INTO tables (id, namespace_id, name, table_type, created_at,"
                        " updated_at) VALUES (:id, :ns, :name, 'ICEBERG',"
                        " '2026-10-15 00:00:00', '2026-10-15 00:00:00')"
                    ),
                    {"id": identifier, "ns": uuid.uuid4().hex, "name": name},
                )
            connection.execute(  # a write that is stamped and not yet judged by its checks
                sa.text(
                    "INSERT INTO task_runs (id, run_id, asset, status, attempt, claim_count,"
                    " max_attempts, timeout_seconds, retry_delay_seconds, upstream_json,"
                    " conflict_retried, written_table_id) VALUES (:id, :run, 'silver.a',"
                    " 'RUNNING', 1, 1, 1, 60, 0, '[]', 0, :written)"
                ),
                {"id": uuid.uuid4().hex, "run": uuid.uuid4().hex, "written": in_flight},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0031)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        found = {c["name"]: c for c in inspect(engine).get_columns("tables")}
        assert found["kept_earlier"]["nullable"] is False
        assert found["kept_snapshot_id"]["nullable"] is True
        with engine.connect() as connection:
            row = connection.execute(
                sa.text(
                    "SELECT kept_run_id, kept_earlier, kept_snapshot_id FROM tables WHERE id = :id"
                ),
                {"id": table_id},
            ).one()
            verdicts = dict(
                connection.execute(
                    sa.text("SELECT id, kept_snapshot_id FROM tables WHERE id IN (:a, :b)"),
                    {"a": in_flight, "b": idle},
                ).all()
            )
        assert row[0] == kept_run
        assert json.loads(row[1]) == []  # an older note has no history to restore
        assert row[2] is None  # and no verdict on any snapshot
        # An unmarked table with a write in flight may have been judged by a clear that ran before
        # the upgrade: its state is unknown. A table with no such write has no history at all.
        assert verdicts == {in_flight: -1, idle: None}
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0030)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "kept_earlier" not in {c["name"] for c in inspect(engine).get_columns("tables")}
    finally:
        engine.dispose()


_REVISION_0032 = "d7a0e3b6c9f2"


def test_0032_adds_the_unknown_mark_of_a_kept_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0032`: `tables.kept_unknown`, not null; an older mark reads as a known one.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0031)
    engine = create_engine(f"sqlite:///{db_path}")
    table_id = uuid.uuid4().hex
    try:
        assert "kept_unknown" not in {c["name"] for c in inspect(engine).get_columns("tables")}
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at,"
                    " updated_at, kept_run_id, kept_check) VALUES (:id, :ns, 'x', 'ICEBERG',"
                    " '2026-10-16 00:00:00', '2026-10-16 00:00:00', :run, 'x/old')"
                ),
                {"id": table_id, "ns": uuid.uuid4().hex, "run": uuid.uuid4().hex},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0032)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        found = {c["name"]: c for c in inspect(engine).get_columns("tables")}
        assert found["kept_unknown"]["nullable"] is False
        with engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT kept_unknown FROM tables WHERE id = :id"), {"id": table_id}
            ).one()
        assert bool(row[0]) is False  # an older mark names a failed check
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0031)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "kept_unknown" not in {c["name"] for c in inspect(engine).get_columns("tables")}
    finally:
        engine.dispose()


_REVISION_0033 = "e8b1f4c7d0a3"


def test_0033_records_the_schema_a_policy_was_written_against(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0033`: a nullable `schema_id` on row filters and column masks; old rows keep none.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0032)
    engine = create_engine(f"sqlite:///{db_path}")
    filter_id, mask_id = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        for table in ("row_filters", "column_masks"):
            assert "schema_id" not in {c["name"] for c in inspect(engine).get_columns(table)}
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            common = (
                ' (id, principal_id, environment, namespace, "table", expression, created_by,'
                " created_at{extra}) VALUES (:id, :who, 'dev', 'gold', 'x', 'TRUE', :who,"
                " '2026-10-17 00:00:00'{values})"
            )
            connection.execute(
                sa.text("INSERT INTO row_filters" + common.format(extra="", values="")),
                {"id": filter_id, "who": uuid.uuid4().hex},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO column_masks" + common.format(extra=', "column"', values=", 'ssn'")
                ),
                {"id": mask_id, "who": uuid.uuid4().hex},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0033)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        for table, row_id in (("row_filters", filter_id), ("column_masks", mask_id)):
            found = {c["name"]: c for c in inspect(engine).get_columns(table)}
            assert found["schema_id"]["nullable"] is True
            with engine.connect() as connection:
                row = connection.execute(
                    sa.text(f"SELECT schema_id FROM {table} WHERE id = :id"), {"id": row_id}
                ).one()
            assert row[0] is None  # a policy of an earlier release names no schema
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0032)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        for table in ("row_filters", "column_masks"):
            assert "schema_id" not in {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


_REVISION_0034 = "f1c4a8e2b6d9"


def test_0034_binds_each_secret_named_by_exactly_one_index_or_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0034`: `secrets.use_kind` and `use_target_id`; only unambiguous names are bound.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0033)
    engine = create_engine(f"sqlite:///{db_path}")
    index_id, repo_id = uuid.uuid4().hex, uuid.uuid4().hex
    secrets = {name: uuid.uuid4().hex for name in ("pypi", "git", "shared", "s3")}
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO package_indexes (id, name, url, credential_secret, created_at,"
                    " updated_at) VALUES (:id, 'corp', 'https://corp/simple', 'pypi',"
                    " '2026-10-17 00:00:00', '2026-10-17 00:00:00')"
                ),
                {"id": index_id},
            )
            for row_id, name, secret in (
                (repo_id, "analytics", "git"),
                (uuid.uuid4().hex, "one", "shared"),
                (uuid.uuid4().hex, "two", "shared"),
            ):
                connection.execute(
                    sa.text(
                        "INSERT INTO repositories (id, name, kind, url, credential_secret,"
                        " created_at, updated_at) VALUES (:id, :name, 'HTTPS', 'https://g/a.git',"
                        " :secret, '2026-10-17 00:00:00', '2026-10-17 00:00:00')"
                    ),
                    {"id": row_id, "name": name, "secret": secret},
                )
            for name, secret_id in secrets.items():
                connection.execute(
                    sa.text(
                        "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce,"
                        " wrapped_data_key, format_version, key_id, created_by, created_at)"
                        " VALUES (:id, :env, :name, x'00', x'00', x'00', 2, 'env', :who,"
                        " '2026-10-17 00:00:00')"
                    ),
                    {
                        "id": secret_id,
                        "env": uuid.uuid4().hex,
                        "name": name,
                        "who": uuid.uuid4().hex,
                    },
                )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0034)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            rows = {
                name: (kind, target)
                for name, kind, target in connection.execute(
                    sa.text("SELECT name, use_kind, use_target_id FROM secrets")
                )
            }
        assert rows["pypi"] == ("PACKAGE_INDEX", index_id)
        assert rows["git"] == ("REPOSITORY", repo_id)
        assert rows["shared"] == (None, None)  # two repositories name it: an administrator decides
        assert rows["s3"] == (None, None)  # named by nothing: stays unbound
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0033)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("secrets")}
        assert not columns & {"use_kind", "use_target_id"}
    finally:
        engine.dispose()


def test_0034_leaves_every_ambiguous_or_environment_used_secret_unbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0034` leaves ambiguous and environment-used secrets unbound.

    Only a secret that exactly one index or repository names, and that its own environment
    does not use, is bound; everything else stays unbound.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "ambiguous.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0033)
    when = "2026-10-17 00:00:00"
    env_one, env_two, env_three, who = (uuid.uuid4().hex for _ in range(4))
    index_ids = {
        name: uuid.uuid4().hex
        for name in ("corp", "dup1", "dup2", "mixed", "own", "case", "hook", "mh", "oh")
    }
    repo_ids = {name: uuid.uuid4().hex for name in ("mixed", "own")}
    # index name -> the secret name its credential_secret holds
    index_secret = {
        "corp": "pypi",  # exactly one index: bound, in both environments
        "dup1": "two-indexes",
        "dup2": "two-indexes",
        "mixed": "index-and-repo",
        "own": "storage-or-hook",  # bound in env_two only
        "hook": "hook-secret",  # bound in env_two only
        "mh": "manifest-hook",  # a deployed webhook that never delivered
        "oh": "old-hook",  # a webhook of an older version only
        "case": "Token",  # a case variant of the secret 'token' does not match
    }
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            for env_id, name, ref in (
                (env_one, "one", "storage-or-hook"),
                (env_two, "two", None),
                (env_three, "three", "repo-own"),
            ):
                connection.execute(
                    sa.text(
                        "INSERT INTO environments (id, name, storage_root, storage_options,"
                        " is_protected, properties, storage_secret_ref, created_at, updated_at)"
                        " VALUES (:id, :name, 'file:///x', '{}', 0, '{}', :ref, :t, :t)"
                    ),
                    {"id": env_id, "name": name, "ref": ref, "t": when},
                )
            for name, index_id in index_ids.items():
                connection.execute(
                    sa.text(
                        "INSERT INTO package_indexes (id, name, url, credential_secret,"
                        " created_at, updated_at) VALUES (:id, :name, :url, :secret, :t, :t)"
                    ),
                    {
                        "id": index_id,
                        "name": name,
                        "url": f"https://{name}/simple",
                        "secret": index_secret[name],
                        "t": when,
                    },
                )
            connection.execute(
                sa.text(
                    "INSERT INTO repositories (id, name, kind, url, credential_secret,"
                    " created_at, updated_at) VALUES (:id, 'mixed', 'HTTPS', 'https://g/a.git',"
                    " 'index-and-repo', :t, :t)"
                ),
                {"id": repo_ids["mixed"], "t": when},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO repositories (id, name, kind, url, credential_secret,"
                    " created_at, updated_at) VALUES (:id, 'own', 'HTTPS', 'https://g/own.git',"
                    " 'repo-own', :t, :t)"
                ),
                {"id": repo_ids["own"], "t": when},
            )
            # a webhook that has delivered in env_one names a second secret there
            connection.execute(
                sa.text(
                    "INSERT INTO notification_deliveries (id, environment_id, repository_id, job,"
                    " event, format, secret_name, state, attempts, created_at)"
                    " VALUES (:id, :env, :repo, 'nightly', 'test', 'json', 'hook-secret',"
                    " 'PENDING', 0, :t)"
                ),
                {"id": uuid.uuid4().hex, "env": env_one, "repo": repo_ids["mixed"], "t": when},
            )
            versions = (
                ("a" * 40, {"jobs": {"nightly": {"notify": {"webhook": "old-hook"}}}}),
                (
                    "b" * 40,
                    {
                        "jobs": {
                            "nightly": {"notify": {"webhook": "manifest-hook", "format": "json"}}
                        }
                    },
                ),
            )
            for sha, manifest in versions:
                connection.execute(
                    sa.text(
                        "INSERT INTO job_versions (id, repository_id, environment_id, commit_sha,"
                        " manifest_json, code_uri, code_sha256, created_at) VALUES (:id, :repo,"
                        " :env, :sha, :manifest, 'file:///c', :sha256, :t)"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "repo": repo_ids["mixed"],
                        "env": env_one,
                        "sha": sha,
                        "manifest": json.dumps(manifest),
                        "sha256": "0" * 64,
                        "t": when,
                    },
                )
            secrets = [
                (env_one, "manifest-hook"),
                (env_two, "manifest-hook"),
                (env_one, "old-hook"),
                (env_two, "old-hook"),
                (env_two, "repo-own"),
                (env_three, "repo-own"),
                (env_one, "pypi"),
                (env_two, "pypi"),
                (env_one, "two-indexes"),
                (env_one, "index-and-repo"),
                (env_one, "storage-or-hook"),
                (env_two, "storage-or-hook"),
                (env_one, "hook-secret"),
                (env_two, "hook-secret"),
                (env_one, "token"),
            ]
            for env_id, name in secrets:
                connection.execute(
                    sa.text(
                        "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce,"
                        " wrapped_data_key, format_version, key_id, created_by, created_at)"
                        " VALUES (:id, :env, :name, x'00', x'00', x'00', 2, 'env', :who, :t)"
                    ),
                    {"id": uuid.uuid4().hex, "env": env_id, "name": name, "who": who, "t": when},
                )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0034)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            found = {
                (env_id, name): (kind, target)
                for env_id, name, kind, target in connection.execute(
                    sa.text("SELECT environment_id, name, use_kind, use_target_id FROM secrets")
                )
            }
    finally:
        engine.dispose()

    unbound = (None, None)
    assert found[env_one, "pypi"] == ("PACKAGE_INDEX", index_ids["corp"])
    assert found[env_two, "pypi"] == ("PACKAGE_INDEX", index_ids["corp"])  # per environment
    assert found[env_one, "two-indexes"] == unbound
    assert found[env_one, "index-and-repo"] == unbound
    assert found[env_one, "storage-or-hook"] == unbound  # env_one's storage uses it
    assert found[env_two, "storage-or-hook"] == ("PACKAGE_INDEX", index_ids["own"])
    assert found[env_one, "hook-secret"] == unbound  # env_one's webhook delivered with it
    assert found[env_two, "hook-secret"] == ("PACKAGE_INDEX", index_ids["hook"])
    assert found[env_one, "manifest-hook"] == unbound  # deployed in env_one, never delivered
    assert found[env_two, "manifest-hook"] == ("PACKAGE_INDEX", index_ids["mh"])
    assert found[env_one, "old-hook"] == unbound  # only an older version names it
    assert found[env_two, "old-hook"] == ("PACKAGE_INDEX", index_ids["oh"])
    assert found[env_two, "repo-own"] == ("REPOSITORY", repo_ids["own"])
    assert found[env_three, "repo-own"] == unbound  # a repository names it, storage uses it
    assert found[env_one, "token"] == unbound  # 'Token' is not 'token'


@pytest.mark.parametrize(
    "odd",
    [
        [],
        "text",
        {"jobs": []},
        {"jobs": {"a": {"notify": None}}},
        {"jobs": {"a": "a string, not a job"}},
        {"jobs": {"a": {"notify": {"format": "json"}}}},
        {"jobs": {"a": {"notify": {"webhook": 5}}}},
    ],
    ids=["list", "string", "jobs-list", "notify-null", "job-string", "no-webhook", "webhook-int"],
)
def test_0034_reads_an_unusual_manifest_without_failing_or_inventing_a_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, odd: object
) -> None:
    """Revision `0034` walks stored manifests defensively and a good one still counts.

    The unusual manifest names no webhook, so the secret an index names stays bound; a normal
    manifest stored beside it still makes its webhook secret unbound.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    odd
        The unusual manifest.
    """
    db_path = tmp_path / "odd.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0033)
    when = "2026-10-17 00:00:00"
    env_id, repo_id, who = (uuid.uuid4().hex for _ in range(3))
    index_ids = {"plain": uuid.uuid4().hex, "hooked": uuid.uuid4().hex}
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO environments (id, name, storage_root, storage_options,"
                    " is_protected, properties, created_at, updated_at)"
                    " VALUES (:id, 'one', 'file:///x', '{}', 0, '{}', :t, :t)"
                ),
                {"id": env_id, "t": when},
            )
            for name, index_id in index_ids.items():
                connection.execute(
                    sa.text(
                        "INSERT INTO package_indexes (id, name, url, credential_secret,"
                        " created_at, updated_at) VALUES (:id, :name, :url, :name, :t, :t)"
                    ),
                    {"id": index_id, "name": name, "url": f"https://{name}/simple", "t": when},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce,"
                        " wrapped_data_key, format_version, key_id, created_by, created_at)"
                        " VALUES (:id, :env, :name, x'00', x'00', x'00', 2, 'env', :who, :t)"
                    ),
                    {"id": uuid.uuid4().hex, "env": env_id, "name": name, "who": who, "t": when},
                )
            good = {"jobs": {"nightly": {"notify": {"webhook": "hooked"}}}}
            for sha, manifest in (("a" * 40, odd), ("b" * 40, good)):
                connection.execute(
                    sa.text(
                        "INSERT INTO job_versions (id, repository_id, environment_id, commit_sha,"
                        " manifest_json, code_uri, code_sha256, created_at) VALUES (:id, :repo,"
                        " :env, :sha, :manifest, 'file:///c', :sha256, :t)"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "repo": repo_id,
                        "env": env_id,
                        "sha": sha,
                        "manifest": json.dumps(manifest),
                        "sha256": "0" * 64,
                        "t": when,
                    },
                )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0034)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            found = dict(connection.execute(sa.text("SELECT name, use_kind FROM secrets")).all())
    finally:
        engine.dispose()
    assert found == {"plain": "PACKAGE_INDEX", "hooked": None}


_REVISION_0035 = "a3d7c9e1f502"


def _insert_secret(
    connection: sa.Connection, kind: str | None, target: str | None, name: str
) -> str:
    """Insert one secret row with the given use, bypassing every service.

    Parameters
    ----------
    connection
        An open connection, inside a transaction the caller commits.
    kind
        The value for `use_kind`, or `None`.
    target
        The value for `use_target_id` (32 hex characters), or `None`.
    name
        The secret's name.

    Returns
    -------
    str
        The new row's id.
    """
    row_id = uuid.uuid4().hex
    connection.execute(
        sa.text(
            "INSERT INTO secrets (id, environment_id, name, ciphertext, nonce, wrapped_data_key,"
            " format_version, key_id, created_by, created_at, use_kind, use_target_id)"
            " VALUES (:id, :env, :name, x'00', x'00', x'00', 2, 'env', :who,"
            " '2026-10-18 00:00:00', :kind, :target)"
        ),
        {
            "id": row_id,
            "env": uuid.uuid4().hex,
            "name": name,
            "who": uuid.uuid4().hex,
            "kind": kind,
            "target": target,
        },
    )
    return row_id


def test_0035_refuses_an_inconsistent_secret_use_and_its_downgrade_drops_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0035`: use_kind and use_target_id are set together; the environment has no target.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0035)
    engine = create_engine(f"sqlite:///{db_path}")
    target = uuid.uuid4().hex
    try:
        with engine.begin() as connection:
            _insert_secret(connection, None, None, "unbound")
            _insert_secret(connection, "PACKAGE_INDEX", target, "index")
            _insert_secret(connection, "REPOSITORY", target, "repo")
            _insert_secret(connection, "ENVIRONMENT", None, "env")
        inconsistent = (
            ("PACKAGE_INDEX", None),
            ("REPOSITORY", None),
            (None, target),
            ("ENVIRONMENT", target),
        )
        for kind, bound in inconsistent:
            with pytest.raises(IntegrityError), engine.begin() as connection:
                _insert_secret(connection, kind, bound, "bad")
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0034)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            _insert_secret(connection, "PACKAGE_INDEX", None, "allowed-again")
    finally:
        engine.dispose()


def test_0035_names_the_secrets_that_break_the_rule_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing inconsistent secret stops the upgrade, with its id and the way out.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0034)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            _insert_secret(connection, None, None, "fine")
            broken = _insert_secret(connection, "PACKAGE_INDEX", None, "broken")
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError) as raised:
        command.upgrade(config, _REVISION_0035)
    message = str(raised.value)
    assert broken in message
    assert "lakana secret bind" in message
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            version = connection.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
            kinds = dict(connection.execute(sa.text("SELECT name, use_kind FROM secrets")).all())
    finally:
        engine.dispose()
    assert version == _REVISION_0034
    assert kinds == {"fine": None, "broken": "PACKAGE_INDEX"}


def test_secret_model_and_migrations_agree_on_the_use_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model declares the same check, so the migrated schema has no drift from it.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        checks = {c["name"] for c in inspect(engine).get_check_constraints("secrets")}
    finally:
        engine.dispose()
    assert "ck_secrets_use_consistent" in checks
    assert "ck_secrets_use_consistent" in {
        c.name for c in Base.metadata.tables["secrets"].constraints
    }


_REVISION_0036 = "b5e2d8f1a7c3"


def test_0036_marks_existing_column_masks_as_not_type_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0036`: `column_masks.type_checked`, false for every mask that already exists.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0035)
    engine = create_engine(f"sqlite:///{db_path}")
    mask_id = uuid.uuid4().hex
    try:
        assert "type_checked" not in {
            c["name"] for c in inspect(engine).get_columns("column_masks")
        }
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO column_masks (id, principal_id, environment, namespace, "
                    '"table", "column", expression, created_by, created_at) VALUES '
                    "(:id, :who, 'dev', 'gold', 'x', 'ssn', '''***''', :who, "
                    "'2026-10-18 00:00:00')"
                ),
                {"id": mask_id, "who": uuid.uuid4().hex},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0036)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        found = {c["name"]: c for c in inspect(engine).get_columns("column_masks")}
        assert found["type_checked"]["nullable"] is False
        with engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT type_checked FROM column_masks WHERE id = :id"), {"id": mask_id}
            ).one()
        assert not row[0]  # a mask of an earlier release was never type-checked
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0035)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "type_checked" not in {
            c["name"] for c in inspect(engine).get_columns("column_masks")
        }
    finally:
        engine.dispose()


_REVISION_0037 = "c6f3a9d2b8e4"


def test_0037_adds_the_description_columns_and_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0037`: descriptions on `tables` and `column_descriptions`, undone on downgrade.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0036)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "column_descriptions" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0037)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        found = {c["name"]: c for c in inspector.get_columns("tables")}
        assert found["description_project"]["nullable"] is True
        assert found["description_edited"]["nullable"] is True
        columns = {c["name"]: c for c in inspector.get_columns("column_descriptions")}
        assert set(columns) == {
            "id",
            "table_id",
            "column",
            "description_project",
            "description_edited",
            "updated_by",
            "updated_at",
        }
        assert [
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("column_descriptions")
        ] == [("tables", "CASCADE"), ("principals", "SET NULL")]
        assert [
            u["column_names"] for u in inspector.get_unique_constraints("column_descriptions")
        ] == [["table_id", "column"]]
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0036)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert "column_descriptions" not in inspector.get_table_names()
        assert {"description_project", "description_edited"}.isdisjoint(
            c["name"] for c in inspector.get_columns("tables")
        )
    finally:
        engine.dispose()


_REVISION_0038 = "d7a4b0e3c9f5"


def test_0038_adds_the_table_reads_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revision `0038`: `table_reads` with its keys and its day index, undone on downgrade.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0037)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "table_reads" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0038)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("table_reads")}
        assert set(columns) == {
            "id",
            "table_id",
            "principal_id",
            "day",
            "read_count",
            "last_read_at",
        }
        assert not any(c["nullable"] for c in columns.values())
        assert [
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("table_reads")
        ] == [("tables", "CASCADE"), ("principals", "CASCADE")]
        assert [u["column_names"] for u in inspector.get_unique_constraints("table_reads")] == [
            ["table_id", "principal_id", "day"]
        ]
        assert [i["column_names"] for i in inspector.get_indexes("table_reads")] == [["day"]]
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0037)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "table_reads" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


_REVISION_0039 = "e8b5c1f4d0a6"


def test_0039_adds_the_table_columns_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revision `0039`: `table_columns` with its keys and its name index, undone on downgrade.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0038)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "table_columns" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0039)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("table_columns")}
        assert set(columns) == {"id", "table_id", "name", "type", "position"}
        assert not any(c["nullable"] for c in columns.values())
        assert [
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("table_columns")
        ] == [("tables", "CASCADE")]
        assert [u["column_names"] for u in inspector.get_unique_constraints("table_columns")] == [
            ["table_id", "name"]
        ]
        assert [i["column_names"] for i in inspector.get_indexes("table_columns")] == [["name"]]
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0038)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "table_columns" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


_REVISION_0040 = "f9c6d2a5e1b7"


def test_0040_adds_the_failure_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revision `0040`: a nullable `failure_code` on `task_runs` and `task_attempts`, undone down.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0039)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        for table in ("task_runs", "task_attempts"):
            assert "failure_code" not in {c["name"] for c in inspector.get_columns(table)}
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0040)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        for table in ("task_runs", "task_attempts"):
            column = {c["name"]: c for c in inspector.get_columns(table)}["failure_code"]
            assert column["nullable"]
            assert getattr(column["type"], "length", None) == 32
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0039)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        for table in ("task_runs", "task_attempts"):
            assert "failure_code" not in {c["name"] for c in inspector.get_columns(table)}
    finally:
        engine.dispose()


_REVISION_0041 = "a0d7e3b6f2c8"


def test_0041_adds_the_time_a_principal_is_passed_by_until(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0041`: a nullable `query_passed_by_until` on `principals`, undone down.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0040)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = {c["name"] for c in inspect(engine).get_columns("principals")}
        assert "query_passed_by_until" not in names
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0041)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        columns = {c["name"]: c for c in inspect(engine).get_columns("principals")}
        assert columns["query_passed_by_until"]["nullable"]
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0040)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = {c["name"] for c in inspect(engine).get_columns("principals")}
        assert "query_passed_by_until" not in names
    finally:
        engine.dispose()


_REVISION_0042 = "b1e8f4c7a3d9"


def _seed_tables_for_0042(db_path: Path) -> dict[str, str]:
    """Insert, before revision `0042`, one table of every kind the backfill must tell apart.

    Parameters
    ----------
    db_path
        The SQLite file.

    Returns
    -------
    dict of str to str
        The id (hex) of each table, by name.
    """
    env_id, ns_id = uuid.uuid4().hex, uuid.uuid4().hex
    root = "file:///lake/dev"
    ids = {name: uuid.uuid4().hex for name in ("orders", "legacy", "twin", "raw", "v", "m", "new")}
    metadata = {
        "orders": f"{root}/managed/gold/orders-{ids['orders']}/metadata/00001-a.metadata.json",
        "legacy": f"{root}/managed/gold/metadata/00000-b.metadata.json",
        "twin": f"{root}/managed/gold/metadata/00003-c.metadata.json",
        "raw": None,
        "v": None,
        "m": None,
        "new": None,
    }
    kinds = {
        "orders": "ICEBERG",
        "legacy": "ICEBERG",
        "twin": "ICEBERG",
        "raw": "EXTERNAL",
        "v": "VIEW",
        "m": "ARTIFACT",
        "new": "ICEBERG",
    }
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO environments (id, name, storage_root, storage_options,"
                    " is_protected, properties, created_at, updated_at)"
                    " VALUES (:id, 'dev', :root, '{}', 0, '{}', :t, :t)"
                ),
                {"id": env_id, "root": root, "t": "2026-10-01 09:00:00"},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO namespaces (id, environment_id, path, properties)"
                    " VALUES (:id, :env, 'gold', '{}')"
                ),
                {"id": ns_id, "env": env_id},
            )
            for name, table_id in ids.items():
                connection.execute(
                    sa.text(
                        "INSERT INTO tables (id, namespace_id, name, table_type,"
                        " metadata_location, created_at, updated_at)"
                        " VALUES (:id, :ns, :name, :kind, :meta, :t, :t)"
                    ),
                    {
                        "id": table_id,
                        "ns": ns_id,
                        "name": name,
                        "kind": kinds[name],
                        "meta": metadata[name],
                        "t": "2026-10-02 09:00:00",
                    },
                )
    finally:
        engine.dispose()
    return {"env": env_id, **ids}


def test_0042_adds_table_locations_with_a_live_record_for_every_managed_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0042`: `table_locations`, backfilled LIVE for managed tables only, undone down.

    An external table, a view, an artifact and a managed table without a metadata file get no
    record; two legacy tables that share a location get one; nothing is deleted.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0041)
    ids = _seed_tables_for_0042(db_path)

    command.upgrade(config, _REVISION_0042)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("table_locations")}
        assert set(columns) == {
            "id",
            "table_id",
            "environment_id",
            "location",
            "state",
            "holder_run_id",
            "claimed_at",
            "dropped_at",
            "purge_after",
            "revoked_policy_ids",
        }
        assert [c for c, info in columns.items() if info["nullable"]] == [
            "table_id",
            "holder_run_id",
            "dropped_at",
            "purge_after",
        ]
        assert [
            (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("table_locations")
        ] == [("environments", "CASCADE")]
        assert [u["column_names"] for u in inspector.get_unique_constraints("table_locations")] == [
            ["location"]
        ]
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text(
                    "SELECT table_id, environment_id, location, state, holder_run_id,"
                    " revoked_policy_ids, purge_after FROM table_locations ORDER BY location"
                )
            ).all()
        root = "file:///lake/dev/managed/gold"
        assert [(r[2], r[3]) for r in rows] == [
            (root, "LIVE"),
            (f"{root}/orders-{ids['orders']}", "LIVE"),
        ]
        assert uuid.UUID(rows[0][0]).hex in {ids["legacy"], ids["twin"]}  # one record, one owner
        assert uuid.UUID(rows[1][0]).hex == ids["orders"]
        assert {uuid.UUID(r[1]).hex for r in rows} == {ids["env"]}
        assert all(r[4] is None and json.loads(r[5]) == [] and r[6] is None for r in rows)
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0041)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert "table_locations" not in inspector.get_table_names()
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM tables")).scalar_one() == 7
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "metadata_location",
    [
        "s3://bucket/dev/managed/gold/orders-0a1b/metadata/00001-a.metadata.json",
        "s3://bucket/dev/managed/gold/x-0a1b/inner-2c3d/metadata/00000-b.metadata.json",
        "file:///lake/dev/managed/metadata/t-0a1b/metadata/00002-c.metadata.json",
        "file:///lake/dev/managed/gold/t-0a1b/metadata/",
        "file:///lake/dev/managed/gold/t-0a1b/metadata",
        "file:///lake/dev/managed/gold/t-0a1b/",
        "file:///lake/dev/managed/gold/t-0a1b/00000-d.metadata.json",
    ],
)
def test_0042_derives_a_table_root_as_the_application_does(metadata_location: str) -> None:
    """The frozen copy in migration `0042` agrees with the function it mirrors.

    Parameters
    ----------
    metadata_location
        A sample: plain, nested, a directory named `metadata`, with and without a trailing
        slash, and without a metadata directory at all.
    """
    import importlib.util

    from lakana_core.storage.resolver import table_root_from_metadata_location

    path = ALEMBIC_INI.parent / "src/lakana_core/migrations/versions/0042_table_locations.py"
    spec = importlib.util.spec_from_file_location("migration_0042", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.table_root_from_metadata_location(
        metadata_location
    ) == table_root_from_metadata_location(metadata_location)


_REVISION_0043 = "c2f9a5d8b4e0"


def test_0043_adds_the_tombstone_columns_and_keeps_every_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0043`: nullable `purge_started_at` and `dropped_table`; nothing else changes.

    A record from before (here a `DROPPED` one, which 0042 allows) keeps its state and
    `purge_after` across the upgrade and the downgrade, so no file's fate changes.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0042)
    ids = _seed_tables_for_0042(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    record_id = uuid.uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO table_locations (id, environment_id, location, state,"
                    " claimed_at, purge_after, revoked_policy_ids)"
                    " VALUES (:id, :env, 'file:///lake/dev/managed/gold/x-0a1b', 'DROPPED',"
                    " :t, :t, '[]')"
                ),
                {"id": record_id, "env": ids["env"], "t": "2026-10-03 09:00:00"},
            )
    finally:
        engine.dispose()

    def read() -> list[tuple[object, ...]]:
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.connect() as connection:
                return [
                    tuple(row)
                    for row in connection.execute(
                        sa.text(
                            "SELECT location, state, purge_after FROM table_locations"
                            " ORDER BY location"
                        )
                    ).all()
                ]
        finally:
            engine.dispose()

    before = read()
    command.upgrade(config, _REVISION_0043)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        columns = {c["name"]: c for c in inspect(engine).get_columns("table_locations")}
        assert columns["purge_started_at"]["nullable"] is True
        assert columns["dropped_table"]["nullable"] is True
        with engine.connect() as connection:
            assert connection.execute(
                sa.text(
                    "SELECT count(*) FROM table_locations"
                    " WHERE purge_started_at IS NULL AND dropped_table IS NULL"
                )
            ).scalar_one() == len(before)
    finally:
        engine.dispose()
    assert read() == before

    command.downgrade(config, _REVISION_0042)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = {c["name"] for c in inspect(engine).get_columns("table_locations")}
        assert "purge_started_at" not in names and "dropped_table" not in names
    finally:
        engine.dispose()
    assert read() == before


_REVISION_0044 = "d3a0b6e9c5f1"


def test_0044_adds_retention_columns_and_sets_no_retention_on_any_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0044`: retention columns on tables and environments, empty on every old row.

    Nothing expires because of an upgrade: no table and no environment comes out of it with a
    retention, and the downgrade removes the columns again without touching a row.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0042)
    ids = _seed_tables_for_0042(db_path)
    command.upgrade(config, _REVISION_0043)
    command.upgrade(config, _REVISION_0044)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = {c["name"]: c for c in inspect(engine).get_columns("tables")}
        for name in ("retention_days", "retention_on", "retention_set_by", "retention_set_at"):
            assert tables[name]["nullable"] is True
        assert tables["retention_never"]["nullable"] is False
        environments = {c["name"]: c for c in inspect(engine).get_columns("environments")}
        for name in ("retention_days", "retention_set_by", "retention_set_at"):
            assert environments[name]["nullable"] is True
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text(
                        "SELECT count(*) FROM tables WHERE retention_days IS NULL"
                        " AND retention_on IS NULL AND retention_never = 0"
                        " AND retention_set_by IS NULL AND retention_set_at IS NULL"
                    )
                ).scalar_one()
                == len(ids) - 1
            )
            assert (
                connection.execute(
                    sa.text("SELECT count(*) FROM environments WHERE retention_days IS NULL")
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0043)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = {c["name"] for c in inspect(engine).get_columns("tables")}
        assert not {n for n in names if n.startswith("retention_")}
        names = {c["name"] for c in inspect(engine).get_columns("environments")}
        assert not {n for n in names if n.startswith("retention_")}
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM tables")).scalar_one() == (
                len(ids) - 1
            )
    finally:
        engine.dispose()


_REVISION_0045 = "ca4f68b6e89a"


def test_0045_adds_the_deletion_columns_and_deletes_no_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0045`: what a deleted environment keeps, empty on every existing row.

    No environment comes out of the upgrade deleted, so nothing is hidden and nothing is purged
    because of it; the downgrade removes the columns again without touching a row.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0042)
    ids = _seed_tables_for_0042(db_path)
    command.upgrade(config, _REVISION_0044)
    command.upgrade(config, _REVISION_0045)
    columns = (
        "deleted_at",
        "deleted_by",
        "purge_after",
        "dropped_location_ids",
        "revoked_policy_ids",
    )
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        environments = {c["name"]: c for c in inspect(engine).get_columns("environments")}
        for name in columns:
            assert environments[name]["nullable"] is True
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text(
                        "SELECT count(*) FROM environments WHERE deleted_at IS NULL"
                        " AND deleted_by IS NULL AND purge_after IS NULL"
                        " AND dropped_location_ids IS NULL AND revoked_policy_ids IS NULL"
                    )
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0044)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = {c["name"] for c in inspect(engine).get_columns("environments")}
        assert not names & set(columns)
        with engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT count(*) FROM environments")).scalar_one() == 1
            )
            assert connection.execute(sa.text("SELECT count(*) FROM tables")).scalar_one() == (
                len(ids) - 1
            )
    finally:
        engine.dispose()


_REVISION_0046 = "00591a77722d"
_VIEW_INDEX = "ix_audit_events_event_type_principal_id_occurred_at"


def test_0046_adds_the_index_of_the_view_audit_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0046`: the composite index the `ACTIVITY_VIEWED` window lookup reads.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0045)

    def indexes() -> dict[str | None, list[str | None]]:
        """Read the indexes of `audit_events`.

        Returns
        -------
        dict of str to list of str
            Each index's columns, by name.
        """
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            return {
                i["name"]: i["column_names"] for i in inspect(engine).get_indexes("audit_events")
            }
        finally:
            engine.dispose()

    assert _VIEW_INDEX not in indexes()
    command.upgrade(config, _REVISION_0046)
    assert indexes()[_VIEW_INDEX] == ["event_type", "principal_id", "occurred_at"]
    command.downgrade(config, _REVISION_0045)
    assert _VIEW_INDEX not in indexes()


_REVISION_0047 = "e0d9e6185736"
_CI_TABLES = {"ci_trust_rules", "ci_tokens", "ci_oidc_jtis"}


def test_0047_adds_the_ci_oidc_tables_and_an_empty_ci_token_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0047`: three empty tables and `deploy_runs.ci_token_id`, empty on every run.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0046)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert not _CI_TABLES & set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0047)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) >= _CI_TABLES
        runs = {c["name"]: c for c in inspector.get_columns("deploy_runs")}
        assert runs["ci_token_id"]["nullable"] is True
        assert not [
            fk
            for fk in inspector.get_foreign_keys("deploy_runs")
            if fk["constrained_columns"] == ["ci_token_id"]
        ]
        tokens = {
            tuple(fk["constrained_columns"]): (fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("ci_tokens")
        }
        assert tokens["rule_id",] == ("ci_trust_rules", "SET NULL")
        assert tokens["principal_id",] == ("principals", "CASCADE")
        unique = {i["name"]: i["unique"] for i in inspector.get_indexes("ci_tokens")}
        assert unique["ix_ci_tokens_key_id"]
        with engine.connect() as connection:
            for table in _CI_TABLES:
                assert connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0046)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert not _CI_TABLES & set(inspector.get_table_names())
        assert "ci_token_id" not in {c["name"] for c in inspector.get_columns("deploy_runs")}
    finally:
        engine.dispose()


_REVISION_0048 = "f1a2b3c4d5e6"
_SIZE_COLUMNS = {
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


def test_0048_adds_empty_size_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revision `0048`: the measured sizes of tables, code archives and worker caches, all empty.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0047)

    def columns() -> dict[str, dict[str, bool]]:
        """Read the nullability of the size columns that exist.

        Returns
        -------
        dict of str to dict of str to bool
            By table, each size column present with whether it is nullable.
        """
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            inspector = inspect(engine)
            return {
                table: {
                    c["name"]: c["nullable"]
                    for c in inspector.get_columns(table)
                    if c["name"] in names
                }
                for table, names in _SIZE_COLUMNS.items()
            }
        finally:
            engine.dispose()

    assert columns() == {table: {} for table in _SIZE_COLUMNS}
    command.upgrade(config, _REVISION_0048)
    assert columns() == {
        table: dict.fromkeys(names, True) for table, names in _SIZE_COLUMNS.items()
    }
    command.downgrade(config, _REVISION_0047)
    assert columns() == {table: {} for table in _SIZE_COLUMNS}


_REVISION_0049 = "a7c3e9f1b2d4"


def test_0049_adds_connections_and_the_connection_secret_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0049`: the connection tables, the secret use `CONNECTION`, `source_kind`.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0048)
    engine = create_engine(f"sqlite:///{db_path}")
    edge = uuid.uuid4().hex
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO lineage_edges (id, environment_id, source_table_id, source_name,"
                    " target_table_id, target_name, created_at) VALUES (:id, :env, :s, 'a.b',"
                    " :t, 'c.d', '2026-10-23 00:00:00')"
                ),
                {"id": edge, "env": uuid.uuid4().hex, "s": uuid.uuid4().hex, "t": uuid.uuid4().hex},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0049)
    engine = create_engine(f"sqlite:///{db_path}")
    target = uuid.uuid4().hex
    try:
        inspector = inspect(engine)
        assert {"connections", "connection_tests"} <= set(inspector.get_table_names())
        assert {
            tuple(c["column_names"]) for c in inspector.get_unique_constraints("connections")
        } == {("environment_id", "name")}
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
        with engine.begin() as connection:
            kind = connection.execute(
                sa.text("SELECT source_kind FROM lineage_edges WHERE id = :id"), {"id": edge}
            ).scalar_one()
            bound = _insert_secret(connection, "CONNECTION", target, "bound")
            connection.execute(
                sa.text(
                    "INSERT INTO lineage_edges (id, environment_id, source_kind, source_table_id,"
                    " source_name, target_table_id, target_name, created_at) VALUES (:id, :env,"
                    " 'connection', :s, 'crm-db', :t, 'c.d', '2026-10-23 00:00:00')"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "env": uuid.uuid4().hex,
                    "s": target,
                    "t": uuid.uuid4().hex,
                },
            )
        assert kind == "table"
        with pytest.raises(IntegrityError), engine.begin() as connection:
            _insert_secret(connection, "CONNECTION", None, "no-target")
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0048)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert not {"connections", "connection_tests"} & set(inspector.get_table_names())
        assert "source_kind" not in {c["name"] for c in inspector.get_columns("lineage_edges")}
        with engine.begin() as connection:
            edges = connection.execute(sa.text("SELECT id FROM lineage_edges")).scalars().all()
            use = connection.execute(
                sa.text("SELECT use_kind, use_target_id FROM secrets WHERE id = :id"),
                {"id": bound},
            ).one()
        assert tuple(use) == (None, None)
        assert edges == [edge]
        with pytest.raises(IntegrityError), engine.begin() as connection:
            _insert_secret(connection, "CONNECTION", target, "refused-again")
    finally:
        engine.dispose()


_REVISION_0050 = "b8d4f0a2c3e5"


def test_0050_adds_the_merge_key_and_maps_merge_runs_down_to_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0050`: `query_runs.merge_key`; a downgrade leaves no `MERGE` the old code reads.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0049)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "merge_key" not in {c["name"] for c in inspect(engine).get_columns("query_runs")}
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0050)
    engine = create_engine(f"sqlite:///{db_path}")
    merged, replaced = uuid.uuid4().hex, uuid.uuid4().hex
    try:
        (column,) = [
            c for c in inspect(engine).get_columns("query_runs") if c["name"] == "merge_key"
        ]
        assert column["nullable"] is True
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            for run_id, mode, key in ((merged, "MERGE", '["id"]'), (replaced, "REPLACE", None)):
                connection.execute(
                    sa.text(
                        "INSERT INTO query_runs (id, environment_id, sql_text, status, write_mode,"
                        " merge_key, created_at, updated_at) VALUES (:id, :env, 'SELECT 1',"
                        " 'SUCCEEDED', :mode, :key, :now, :now)"
                    ),
                    {
                        "id": run_id,
                        "env": uuid.uuid4().hex,
                        "mode": mode,
                        "key": key,
                        "now": _utc_text(),
                    },
                )
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0049)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "merge_key" not in {c["name"] for c in inspect(engine).get_columns("query_runs")}
        with engine.begin() as connection:
            modes = dict(connection.execute(sa.text("SELECT id, write_mode FROM query_runs")).all())
        assert modes == {merged: "APPEND", replaced: "REPLACE"}
    finally:
        engine.dispose()


_REVISION_0051 = "c9e5a1b3d4f6"


def _skip_fsync(dbapi_connection: object, connection_record: object) -> None:
    """Turn off SQLite's flush to disk on commit, as the server tests do (#327).

    Parameters
    ----------
    dbapi_connection
        The new DB-API connection; anything but a `sqlite3.Connection` is left alone.
    connection_record
        SQLAlchemy's record of the connection (unused).
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.execute("PRAGMA synchronous=OFF")


@pytest.fixture
def _without_fsync() -> Iterator[None]:
    """Let the engines of one test commit without waiting for the disk.

    Fifty migrations each commit; on a real disk the waits are most of the test's time. Nothing
    a migration test checks depends on durability across a power cut.

    Yields
    ------
    None
        The listener is registered for the test and removed afterwards.
    """
    event.listen(Engine, "connect", _skip_fsync)
    yield
    event.remove(Engine, "connect", _skip_fsync)


@pytest.mark.usefixtures("_without_fsync")
def test_0051_adds_the_job_starts_and_maps_deploy_runs_down_by_their_starter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0051`: `deploy_runs.job_starts_json`; a downgrade leaves no trigger `DEPLOY`.

    A run started by a deploy of a service account goes down as `API`, one of a person as
    `MANUAL`; every other trigger is kept.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0050)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "job_starts_json" not in {
            c["name"] for c in inspect(engine).get_columns("deploy_runs")
        }
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0051)
    engine = create_engine(f"sqlite:///{db_path}")
    account, person = uuid.uuid4().hex, uuid.uuid4().hex
    by_account, by_person, scheduled = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    try:
        (column,) = [
            c for c in inspect(engine).get_columns("deploy_runs") if c["name"] == "job_starts_json"
        ]
        assert column["nullable"] is True
        (opt_in,) = [
            c
            for c in inspect(engine).get_columns("ci_trust_rules")
            if c["name"] == "start_on_deploy_jobs"
        ]
        assert opt_in["nullable"] is False
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO ci_trust_rules (id, issuer, principal_id, environment_id,"
                    " repository_id, kinds, conditions, enabled, revision, created_at,"
                    " updated_at) VALUES (:id, 'https://i', :p, :e, :r, '[\"deploy\"]', '{}',"
                    " 1, 1, :t, :t)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "p": uuid.uuid4().hex,
                    "e": uuid.uuid4().hex,
                    "r": uuid.uuid4().hex,
                    "t": _utc_text(),
                },
            )
            assert connection.execute(
                sa.text("SELECT start_on_deploy_jobs FROM ci_trust_rules")
            ).scalar_one() in (0, False)
            for principal_id, kind in ((account, "SERVICE_ACCOUNT"), (person, "USER")):
                connection.execute(
                    sa.text(
                        "INSERT INTO principals (id, type, name, is_active, created_at, "
                        "updated_at) VALUES (:id, :type, :id, 1, :t, :t)"
                    ),
                    {"id": principal_id, "type": kind, "t": _utc_text()},
                )
            for run_id, trigger, by in (
                (by_account, "DEPLOY", account),
                (by_person, "DEPLOY", person),
                (scheduled, "SCHEDULE", account),
            ):
                connection.execute(
                    sa.text(
                        "INSERT INTO runs (id, environment_id, repository_id, job_version_id,"
                        " kind, selection_json, status, cancel_requested, created_at,"
                        " triggered_by, job, trigger) VALUES (:id, :a, :b, :c, 'FULL', '{}',"
                        " 'SUCCEEDED', 0, '2026-10-28 00:00:00', :by, 'refresh', :trigger)"
                    ),
                    {
                        "id": run_id,
                        "a": uuid.uuid4().hex,
                        "b": uuid.uuid4().hex,
                        "c": uuid.uuid4().hex,
                        "by": by,
                        "trigger": trigger,
                    },
                )
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0050)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "job_starts_json" not in {
            c["name"] for c in inspect(engine).get_columns("deploy_runs")
        }
        assert "start_on_deploy_jobs" not in {
            c["name"] for c in inspect(engine).get_columns("ci_trust_rules")
        }
        with engine.begin() as connection:
            triggers = dict(connection.execute(sa.text("SELECT id, trigger FROM runs")).all())
        assert triggers == {by_account: "API", by_person: "MANUAL", scheduled: "SCHEDULE"}
    finally:
        engine.dispose()


_REVISION_0052 = "d1f6b2c4e5a7"


@pytest.mark.usefixtures("_without_fsync")
def test_0052_adds_check_results_generated_false_on_old_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0052`: `check_results.generated`, false on a result recorded before it.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0051)
    engine = create_engine(f"sqlite:///{db_path}")
    result_id = uuid.uuid4().hex
    try:
        assert "generated" not in {c["name"] for c in inspect(engine).get_columns("check_results")}
        with engine.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            connection.execute(
                sa.text(
                    "INSERT INTO check_results (id, run_id, task_run_id, environment_id,"
                    " check_name, check_path, table_name, on_failure, outcome, started_at,"
                    " finished_at) VALUES (:id, :run, :task, :env, 'silver.o/c',"
                    " 'checks/silver/o/c.sql', 'silver.o', 'block', 'PASSED', :t, :t)"
                ),
                {
                    "id": result_id,
                    "run": uuid.uuid4().hex,
                    "task": uuid.uuid4().hex,
                    "env": uuid.uuid4().hex,
                    "t": _utc_text(),
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0052)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        (column,) = [
            c for c in inspect(engine).get_columns("check_results") if c["name"] == "generated"
        ]
        assert column["nullable"] is False
        with engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT generated FROM check_results WHERE id = :id"), {"id": result_id}
            ).scalar_one() in (0, False)
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0051)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "generated" not in {c["name"] for c in inspect(engine).get_columns("check_results")}
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT id FROM check_results")).scalars().all() == [
                result_id
            ]
    finally:
        engine.dispose()


_REVISION_0053 = "e3a7c5d9f1b2"
_MODEL_COLUMNS = {"model_kind", "model_grain", "model_key"}


@pytest.mark.usefixtures("_without_fsync")
def test_0053_adds_the_table_model_and_the_references_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0053`: the model columns of `tables`, null on an old row, and `table_references`.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0052)
    engine = create_engine(f"sqlite:///{db_path}")
    table_id = uuid.uuid4().hex
    try:
        assert not _MODEL_COLUMNS & {c["name"] for c in inspect(engine).get_columns("tables")}
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO tables (id, namespace_id, name, table_type, created_at,"
                    " updated_at) VALUES (:id, :ns, 'fact_sales', 'ICEBERG', :t, :t)"
                ),
                {"id": table_id, "ns": uuid.uuid4().hex, "t": _utc_text()},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0053)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        found = {c["name"]: c for c in inspector.get_columns("tables")}
        assert {name: found[name]["nullable"] for name in _MODEL_COLUMNS} == dict.fromkeys(
            _MODEL_COLUMNS, True
        )
        assert "table_references" in inspector.get_table_names()
        assert {c["name"] for c in inspector.get_columns("table_references")} == {
            "id",
            "environment_id",
            "source_table_id",
            "source_column",
            "target_name",
            "target_column",
        }
        assert {
            tuple(c["column_names"]) for c in inspector.get_unique_constraints("table_references")
        } == {("source_table_id", "source_column")}
        assert {
            (i["name"], tuple(i["column_names"])) for i in inspector.get_indexes("table_references")
        } == {("ix_table_references_environment_target", ("environment_id", "target_name"))}
        assert {
            (tuple(fk["constrained_columns"]), fk["referred_table"], fk["options"].get("ondelete"))
            for fk in inspector.get_foreign_keys("table_references")
        } == {
            (("environment_id",), "environments", "CASCADE"),
            (("source_table_id",), "tables", "CASCADE"),
        }
        with engine.begin() as connection:
            model = connection.execute(
                sa.text("SELECT model_kind, model_grain, model_key FROM tables WHERE id = :id"),
                {"id": table_id},
            ).one()
            connection.execute(
                sa.text(
                    "INSERT INTO table_references (id, environment_id, source_table_id,"
                    " source_column, target_name, target_column) VALUES (:id, :env, :table,"
                    " 'store_id', 'gold.dim_store', 'store_id')"
                ),
                {"id": uuid.uuid4().hex, "env": uuid.uuid4().hex, "table": table_id},
            )
        assert tuple(model) == (None, None, None)
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0052)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert "table_references" not in inspector.get_table_names()
        assert not _MODEL_COLUMNS & {c["name"] for c in inspector.get_columns("tables")}
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT id FROM tables")).scalars().all() == [
                table_id
            ]
    finally:
        engine.dispose()


_REVISION_0054 = "f4b8d6e0a2c3"


@pytest.mark.usefixtures("_without_fsync")
def test_0054_adds_the_check_message_state_null_on_old_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0054`: `check_message_state` on task runs and attempts, and back.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0053)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        for table in ("task_runs", "task_attempts"):
            assert "check_message_state" not in {
                c["name"] for c in inspect(engine).get_columns(table)
            }
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION_0054)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        for table in ("task_runs", "task_attempts"):
            found = {c["name"]: c for c in inspect(engine).get_columns(table)}
            assert found["check_message_state"]["nullable"] is True
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0053)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        for table in ("task_runs", "task_attempts"):
            assert "check_message_state" not in {
                c["name"] for c in inspect(engine).get_columns(table)
            }
    finally:
        engine.dispose()


_REVISION_0055 = "a5c9e7f1b3d4"


@pytest.mark.usefixtures("_without_fsync")
def test_0055_adds_the_withheld_lines_of_a_deploy_error_null_on_old_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0055`: `error_withheld_json` on deploy runs, and back.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0055)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        found = {c["name"]: c for c in inspect(engine).get_columns("deploy_runs")}
        assert found["error_withheld_json"]["nullable"] is True
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0054)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = {c["name"] for c in inspect(engine).get_columns("deploy_runs")}
        assert "error_withheld_json" not in names
    finally:
        engine.dispose()


_REVISION_0056 = "b6d0f8a2c4e5"


@pytest.mark.usefixtures("_without_fsync")
def test_0056_adds_the_policy_warnings_of_a_task_null_on_old_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revision `0056`: `policy_warnings_json` on task runs, and back.

    Parameters
    ----------
    tmp_path
        Holds the throwaway SQLite file.
    monkeypatch
        Points `lakana_core.config.Settings` at that file.
    """
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("LAKANA_DATABASE__URL", f"sqlite:///{db_path}")
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, _REVISION_0056)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        found = {c["name"]: c for c in inspect(engine).get_columns("task_runs")}
        assert found["policy_warnings_json"]["nullable"] is True
    finally:
        engine.dispose()

    command.downgrade(config, _REVISION_0055)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        names = {c["name"] for c in inspect(engine).get_columns("task_runs")}
        assert "policy_warnings_json" not in names
    finally:
        engine.dispose()
