"""Binding the row filters and column masks of earlier releases to Iceberg field ids."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import _config
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadata, TableMetadataV2
from pyiceberg.table.sorting import SortOrder
from pyiceberg.table.update import AddSchemaUpdate, SetCurrentSchemaUpdate
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.service import CatalogService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import ColumnMask, RowFilter, Table, TableType
from lakana_core.policy import legacy_binding
from lakana_core.policy.field_binding import BY_NAME, FIELD_IDS, PENDING, UNBOUND, PolicyBinding
from lakana_core.policy.legacy_binding import bind_legacy_policies, legacy_field_ids
from lakana_core.storage.resolver import StorageResolver


def _metadata(current: int, *schemas: Schema) -> TableMetadata:
    """Build the metadata of a table with these schemas.

    Parameters
    ----------
    current
        The id of the current schema.
    *schemas
        The schemas, in the order the metadata lists them.

    Returns
    -------
    TableMetadata
        The metadata.
    """
    return TableMetadataV2(
        location="file:///table",
        last_column_id=20,
        schemas=list(schemas),
        current_schema_id=current,
        partition_specs=[PartitionSpec()],
        sort_orders=[SortOrder()],
    )


def _schema(schema_id: int, *columns: tuple[int, str]) -> Schema:
    """Build a schema of optional string columns after an `id` column.

    Parameters
    ----------
    schema_id
        The schema's id.
    *columns
        Each further column's field id and name.

    Returns
    -------
    Schema
        The schema.
    """
    fields = [NestedField(1, "id", LongType(), required=False)]
    fields += [NestedField(i, n, StringType(), required=False) for i, n in columns]
    return Schema(*fields, schema_id=schema_id)


# Schema 0 had `dept` (field 2); schema 1, the current one, calls it `team`.
_RENAMED = _metadata(1, _schema(0, (2, "dept")), _schema(1, (2, "team")))


def test_a_policy_with_a_recorded_schema_binds_through_that_schema() -> None:
    """Written against schema 0 (`dept` = 2), current schema renamed it `team`: bound to 2."""
    binding = legacy_field_ids(_RENAMED, "dept = 'a'", 0)

    assert binding is not None
    assert binding.binding == FIELD_IDS
    assert binding.field_ids == {"dept": 2}
    assert binding.column_field_id is None


def test_a_recorded_schema_the_metadata_lacks_leaves_it_unbound() -> None:
    """The schema the policy was written against is not in the metadata: never a guess."""
    assert legacy_field_ids(_RENAMED, "dept = 'a'", 99) is None


def test_a_policy_without_a_schema_binds_through_the_current_one_when_nothing_moved() -> None:
    """No rename of `id` in the history: bound through the current schema."""
    binding = legacy_field_ids(_RENAMED, "id > 0 AND team = 'a'", None)
    assert binding is None  # `team` was renamed from `dept`, so the pair is doubtful
    binding = legacy_field_ids(_RENAMED, "id > 0", None)

    assert binding is not None
    assert binding.field_ids == {"id": 1}


def test_a_policy_without_a_schema_after_a_rename_of_its_column_stays_unbound() -> None:
    """Today's rule finds a rename of `dept` in the history: `None`."""
    assert legacy_field_ids(_RENAMED, "dept = 'a'", None) is None


def test_a_legacy_qualified_column_stays_unbound() -> None:
    """`t.dept = 'a'` (accepted by an old release) is never bound, even to a schema it fits."""
    assert legacy_field_ids(_RENAMED, "t.dept = 'a'", 0) is None


def test_a_mask_binds_its_own_column_too() -> None:
    """`column_field_id` is the id of the masked column in the recorded schema."""
    binding = legacy_field_ids(_RENAMED, "'x'", 0, masked_column="dept")

    assert binding is not None
    assert binding.field_ids == {"dept": 2}
    assert binding.column_field_id == 2


def test_an_expression_that_no_longer_parses_stays_unbound() -> None:
    """A stored expression the parser refuses is left alone."""
    assert legacy_field_ids(_RENAMED, "dept = = 'a'", 0) is None


def test_an_ambiguous_name_stays_unbound() -> None:
    """A schema with `Maß` and `MASS`: `None`, never a guess."""
    metadata = _metadata(0, _schema(0, (2, "Maß"), (3, "MASS")))

    assert legacy_field_ids(metadata, "MASS = 'a'", 0) is None


_SILVER = NamespacePath.from_parts(["silver"])
_ORDERS = TableRef("dev", _SILVER, "orders")
_SCHEMA = Schema(
    NestedField(1, "id", LongType(), required=True),
    NestedField(2, "status", StringType(), required=False),
)


@pytest.fixture
def silver(catalog: CatalogService, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` with a `raw` namespace and the `orders` table.

    Parameters
    ----------
    catalog
        The allow-all catalog.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    catalog.create_table(principal, _ORDERS, schema=_SCHEMA)
    return catalog


def _table_id(session: Session, name: str) -> uuid.UUID:
    """Find a table's id.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    return session.scalars(select(Table.id).where(Table.name == name)).one()


def _filter(
    session: Session,
    principal: PrincipalRef,
    table: str,
    expression: str,
    *,
    schema_id: int | None = None,
    binding: str = PENDING,
    revoked: bool = False,
) -> RowFilter:
    """Store a row filter as an earlier release left it.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The subject and creator.
    table
        The table's name.
    expression
        The stored expression.
    schema_id
        The recorded schema.
    binding
        The binding state.
    revoked
        Whether the row is revoked.

    Returns
    -------
    RowFilter
        The stored row.
    """
    row = RowFilter(
        id=uuid.uuid4(),
        principal_id=principal.id,
        environment="dev",
        namespace="silver",
        table=table,
        table_id=_table_id(session, table),
        created_by=principal.id,
        created_at=datetime.now(UTC),
        revoked_at=datetime.now(UTC) if revoked else None,
        revoked_by=principal.id if revoked else None,
        expression=expression,
        schema_id=schema_id,
        binding=binding,
    )
    session.add(row)
    session.commit()
    return row


def test_the_backfill_binds_pending_policies_and_reports_the_count(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    """Two pending rows on an Iceberg table: both `field_ids`, return 2; a second pass 0."""
    row_filter = _filter(session, principal, "orders", "status = 'a'", schema_id=0)
    mask = ColumnMask(
        id=uuid.uuid4(),
        principal_id=principal.id,
        environment="dev",
        namespace="silver",
        table="orders",
        table_id=row_filter.table_id,
        created_by=principal.id,
        created_at=datetime.now(UTC),
        column="status",
        expression="'x'",
        binding=PENDING,
    )
    session.add(mask)
    session.commit()

    assert bind_legacy_policies(session, StorageResolver(session)) == 2

    session.refresh(row_filter)
    session.refresh(mask)
    assert (row_filter.binding, row_filter.field_ids) == (FIELD_IDS, {"status": 2})
    assert (mask.binding, mask.field_ids, mask.column_field_id) == (
        FIELD_IDS,
        {"status": 2},
        2,
    )
    assert bind_legacy_policies(session, StorageResolver(session)) == 0


def test_the_backfill_marks_a_policy_it_cannot_bind_unbound(
    session: Session,
    principal: PrincipalRef,
    silver: CatalogService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pending filter without a schema whose column was renamed: `unbound`, ids stay `None`."""
    current = silver.load_table(principal, _ORDERS).metadata.schema()
    renamed = Schema(
        current.fields[0],
        NestedField(2, "state", StringType(), required=False),
        schema_id=current.schema_id + 1,
    )
    silver.commit_table(
        principal,
        _ORDERS,
        requirements=[],
        updates=[AddSchemaUpdate(schema_=renamed), SetCurrentSchemaUpdate(schema_id=-1)],
    )
    row = _filter(session, principal, "orders", "status = 'a'")

    with caplog.at_level(logging.INFO):
        assert bind_legacy_policies(session, StorageResolver(session)) == 1

    session.refresh(row)
    assert (row.binding, row.field_ids) == (UNBOUND, None)
    assert "Bound 0 row filters and column masks to field ids; 1 left unbound." in caplog.text


def test_the_backfill_binds_a_raw_tables_policies_by_name(
    session: Session, principal: PrincipalRef, silver: CatalogService, tmp_path: Path
) -> None:
    """EXTERNAL: `names`."""
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "p.parquet")
    silver.create_external_table(
        principal,
        TableRef("dev", NamespacePath.from_parts(["raw"]), "events"),
        location_pattern="raw/events/*.parquet",
        format="parquet",
    )
    row = _filter(session, principal, "events", "id > 0")

    assert bind_legacy_policies(session, StorageResolver(session)) == 1

    session.refresh(row)
    assert (row.binding, row.field_ids) == (BY_NAME, None)


def test_the_backfill_binds_a_views_policies_by_name(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    """A view has no field ids and no metadata pointer: `names`, never `pending` forever."""
    silver.create_or_replace_view(
        principal,
        TableRef("dev", _SILVER, "v"),
        sql="SELECT 1 AS a",
        references=[],
        columns=[ColumnSpec("a", "long", True)],
    )
    row = _filter(session, principal, "v", "a > 0")

    assert bind_legacy_policies(session, StorageResolver(session)) == 1

    session.refresh(row)
    assert row.binding == BY_NAME


def test_the_backfill_binds_an_iceberg_table_without_metadata_by_what_it_reads(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    """No metadata pointer: a filter reading no column is exact (`{}`), anything else unbound."""
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=session.scalars(select(Table.namespace_id)).first(),
            name="bare",
            table_type=TableType.ICEBERG,
        )
    )
    session.commit()
    empty = _filter(session, principal, "bare", "has_group('a')")
    reading = _filter(session, principal, "bare", "status = 'a'")

    assert bind_legacy_policies(session, StorageResolver(session)) == 2

    session.refresh(empty)
    session.refresh(reading)
    assert (empty.binding, empty.field_ids) == (FIELD_IDS, {})
    assert reading.binding == UNBOUND


def test_the_backfill_skips_a_table_whose_metadata_cannot_be_read(
    session: Session,
    principal: PrincipalRef,
    silver: CatalogService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The metadata is unreadable: the row stays `pending`, one warning names the class only."""
    row = _filter(session, principal, "orders", "status = 'a'", schema_id=0)

    with mock.patch.object(legacy_binding, "read_metadata", side_effect=OSError("secret path")):
        assert bind_legacy_policies(session, StorageResolver(session)) == 0

    session.refresh(row)
    assert row.binding == PENDING
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "(OSError)" in warnings[0].getMessage()
    assert "secret path" not in caplog.text


def test_the_backfill_never_overwrites_a_row_that_stopped_being_pending(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    """A row set to `field_ids` between the select and the update is left as it is."""
    row = _filter(session, principal, "orders", "status = 'a'", schema_id=0)
    real = legacy_binding.legacy_field_ids

    def meanwhile(
        metadata: TableMetadata,
        expression: str,
        schema_id: int | None,
        *,
        masked_column: str | None = None,
    ) -> PolicyBinding | None:
        """Let another process bind the row before this pass writes.

        Parameters
        ----------
        metadata
            Passed through.
        expression
            Passed through.
        schema_id
            Passed through.
        masked_column
            Passed through.

        Returns
        -------
        PolicyBinding or None
            What the real function returns.
        """
        session.execute(
            update(RowFilter)
            .where(RowFilter.id == row.id)
            .values(binding=FIELD_IDS, field_ids={"status": 9}),
            execution_options={"synchronize_session": False},
        )
        return real(metadata, expression, schema_id, masked_column=masked_column)

    with mock.patch.object(legacy_binding, "legacy_field_ids", meanwhile):
        assert bind_legacy_policies(session, StorageResolver(session)) == 0

    session.refresh(row)
    assert (row.binding, row.field_ids) == (FIELD_IDS, {"status": 9})


def test_a_revoked_pending_policy_is_not_read(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    """Revoked rows are not selected: the pass returns 0 and leaves the row pending."""
    row = _filter(session, principal, "orders", "status = 'a'", schema_id=0, revoked=True)

    assert bind_legacy_policies(session, StorageResolver(session)) == 0

    session.refresh(row)
    assert row.binding == PENDING


def test_a_table_deleted_after_the_select_is_skipped(
    session: Session, principal: PrincipalRef, silver: CatalogService
) -> None:
    """The table is gone by the time its rows are decided: nothing is written, nothing raises."""
    row = _filter(session, principal, "orders", "status = 'a'", schema_id=0)
    gone = {uuid.uuid4(): next(iter(legacy_binding._pending_rows(session, 5).values()))}

    with mock.patch.object(legacy_binding, "_pending_rows", return_value=gone):
        assert bind_legacy_policies(session, StorageResolver(session)) == 0

    session.refresh(row)
    assert row.binding == PENDING
