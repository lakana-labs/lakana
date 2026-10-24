"""Tests for the query pipeline, against a real table (SQLite + local storage)."""

import base64
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

import pyarrow as pa
import pyarrow.ipc
import pyarrow.parquet as pq
import pytest
from pyiceberg.manifest import (
    DataFile,
    DataFileContent,
    FileFormat,
    ManifestContent,
    ManifestEntry,
    ManifestEntryStatus,
    ManifestFile,
    ManifestWriterV2,
    write_manifest,
    write_manifest_list,
)
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.snapshots import Operation, Snapshot, Summary
from pyiceberg.table.update import AddSnapshotUpdate, SetSnapshotRefUpdate
from pyiceberg.typedef import Record
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from worker_test_helpers import ActionAllowList

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.metadata import apply_updates, build_table_metadata, write_metadata
from lakana_core.catalog.query_service import QueryService, WriteTarget
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.config import QueryLimitsConfig, StorageConfig
from lakana_core.errors import (
    NotFoundError,
    PermissionDenied,
    UnsupportedTableError,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.identity.task_tokens import MintedTaskToken, TaskTokenService
from lakana_core.identity.tokens import parse_token
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    Table,
    TableType,
    TaskToken,
    WriteMode,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.query_rewrite import PIVOT_VALUES_MESSAGE
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import ResolvedStorage, StorageResolver
from lakana_worker import pipeline as pipeline_module
from lakana_worker.pipeline import (
    _DELETED_PRINCIPAL_MESSAGE,
    _INACTIVE_PRINCIPAL_MESSAGE,
    _INVALID_TASK_TOKEN_MESSAGE,
    _REDACTED,
    ReadOutcome,
    _resolve_sources,
    run_one,
)


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Provide the acting principal, persisted so `QueryRun.principal_id`'s FK resolves.

    `QueryRun.principal_id` is a real foreign key to `principals.id`,
    enforced even under SQLite (`lakana_core.db` turns the pragma on
    deliberately) — a bare `PrincipalRef` is only the auth-layer identity,
    not a row in the table, so the row is persisted here the same way
    `lakana-core`'s `test_query_service.py` already does.

    Parameters
    ----------
    session
        Database session to persist the backing `Principal` row into.

    Returns
    -------
    PrincipalRef
        The acting principal, whose id matches a persisted `Principal` row.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name="admin"))
    session.commit()
    return PrincipalRef(id=principal_id, name="admin", type=PrincipalType.USER)


def _write_one_data_file_and_commit(
    catalog: CatalogService,
    principal: PrincipalRef,
    ref: TableRef,
    created: LoadedTable,
    data: pa.Table,
) -> LoadedTable:
    """Write one real Parquet data file and commit it as the table's current snapshot.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    ref
        The table to write to.
    created
        The table as `create_table` returned it (a `LoadedTable`).
    data
        The rows to write. Its Arrow schema's nullability must match the
        Iceberg table's schema exactly — a required Iceberg column needs a
        non-nullable Arrow field, or pyiceberg refuses the append with a
        schema-mismatch error.

    Returns
    -------
    LoadedTable
        The table at its new metadata location, with the write committed
        through `CatalogService.commit_table`'s normal CAS path — never
        through `StaticTable`'s own commit, which always raises
        `NotImplementedError` (it has no real catalog bound to it).
    """
    static = StaticTable.from_metadata(created.metadata_location)
    tx = static.transaction()
    tx.append(data)
    return catalog.commit_table(principal, ref, requirements=tx._requirements, updates=tx._updates)


def test_run_one_executes_a_single_table_query_end_to_end(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A query against one real Iceberg table runs, and the row is marked succeeded."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "region", StringType(), required=False),
        ),
    )
    # Write one data file directly and commit it as this table's snapshot,
    # bypassing a full write path (M2 has none yet) to give the pipeline
    # something real to read.
    arrow_schema = pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )
    data = pa.table({"id": [1, 2], "region": ["eu", "us"]}, schema=arrow_schema)
    _write_one_data_file_and_commit(catalog, principal, ref, created, data)

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id, region FROM gold.sales")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED
    assert run.error_message is None
    assert run.lineage_json is not None
    assert run.lineage_json["id"] == [{"table": "gold.sales", "column": "id"}]
    assert run.result_location is not None


def test_a_query_opens_only_the_locations_of_the_tables_it_reads(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The query process is given each table's location and nothing else of the store.

    An Iceberg table's own id-suffixed directory and an external table's ``raw/`` claim; a
    table the query does not read (``gold.other``) is not among them.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal, ref, schema=Schema(NestedField(1, "id", LongType(), required=True))
    )
    data = pa.table({"id": [1, 2]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]))
    _write_one_data_file_and_commit(catalog, principal, ref, created, data)
    catalog.create_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="other"),
        schema=Schema(NestedField(1, "id", LongType(), required=True)),
    )
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [3]}), tmp_path / "raw" / "events" / "part-0.parquet")
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events"),
        location_pattern="raw/events/*.parquet",
        format="parquet",
    )
    real = pipeline_module.execute_query
    seen: list[tuple[str, ...]] = []

    def spy(*args: object, **kwargs: object) -> pa.Table:
        seen.append(tuple(cast("Sequence[str]", kwargs["read_prefixes"])))
        return real(*args, **kwargs)  # ty: ignore[invalid-argument-type]

    run = QueryService(session, AllowAllAuthorization()).submit_query(
        principal,
        "dev",
        "SELECT id FROM gold.sales UNION ALL SELECT id FROM raw.events ORDER BY id",
    )
    with mock.patch("lakana_worker.pipeline.execute_query", spy):
        run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    location = created.metadata.location.removeprefix("file://")
    assert seen == [tuple(sorted([f"{location}/", f"{tmp_path}/raw/events/"]))]
    assert _read_result(tmp_path, run).column("id").to_pylist() == [1, 2, 3]


def _sales(session: Session, principal: PrincipalRef, tmp_path: Path) -> None:
    """Create ``gold.sales`` (id, region) with rows (1, eu), (2, us), (3, eu).

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "region", StringType(), required=False),
        ),
    )
    schema = pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )
    data = pa.table({"id": [1, 2, 3], "region": ["eu", "us", "eu"]}, schema=schema)
    _write_one_data_file_and_commit(catalog, principal, ref, created, data)


def test_a_pivot_without_its_values_fails_with_what_to_write(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The guard words it; the query process, which runs one SELECT, is never asked."""
    _sales(session, principal, tmp_path)
    run = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", "SELECT * FROM (PIVOT gold.sales ON region USING sum(id))"
    )
    with mock.patch(
        "lakana_worker.pipeline.execute_query", side_effect=AssertionError("nothing may run")
    ):
        run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == PIVOT_VALUES_MESSAGE
    assert run.error_cause == "invalid_query"


def test_a_pivot_with_its_values_listed_returns_rows(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """DuckDB's statement form with an ``IN`` list runs as one SELECT on 1.5.5 and 2.0.0."""
    sql = "SELECT * FROM (PIVOT gold.sales ON region IN ('eu', 'us') USING sum(id))"
    _sales(session, principal, tmp_path)
    run = QueryService(session, AllowAllAuthorization()).submit_query(principal, "dev", sql)
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _read_result(tmp_path, run).to_pylist() == [{"eu": 4, "us": 2}]


@pytest.mark.parametrize("operator", ["//", "%"])
@pytest.mark.slow
def test_an_integer_division_by_zero_fails_and_says_how_to_get_null(
    session: Session, principal: PrincipalRef, tmp_path: Path, operator: str
) -> None:
    """DuckDB 2.0 raises on `//` and `%` by zero (1.5 returned NULL).

    DuckDB's text also suggests `SET error_on_division_by_zero`, which a query cannot run; the
    message keeps only the advice that works in a query.
    """
    _sales(session, principal, tmp_path)
    run = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", f"SELECT id {operator} (id - id) AS q FROM gold.sales"
    )
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_cause == "invalid_query"
    assert run.error_message == (
        f"Query failed: Invalid Input Error: Division by zero in expression "
        f"(id {operator} (id - id)). Use TRY(...) to return NULL for this expression."
    )


def test_try_turns_an_integer_division_by_zero_into_null(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The way out that the message names."""
    _sales(session, principal, tmp_path)
    run = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", "SELECT TRY(id // (id - id)) AS q, TRY(id % 0) AS r FROM gold.sales"
    )
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _read_result(tmp_path, run).to_pylist() == [{"q": None, "r": None}] * 3


def test_a_generate_series_alias_names_the_value_itself(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Characterisation: on DuckDB 2.0 `i` in `generate_series(1, 2) i` is the number.

    DuckDB 1.5 made `i` a struct with one field; `i.generate_series` still reads the number.
    """
    _local_catalog(session, principal, tmp_path)
    run = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", "SELECT i, i.generate_series AS g FROM generate_series(1, 2) i"
    )
    run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    result = _read_result(tmp_path, run)
    assert result.schema.field("i").type == pa.int64()
    assert result.to_pylist() == [{"i": 1, "g": 1}, {"i": 2, "g": 2}]


def test_a_table_location_outside_the_storage_root_is_never_turned_into_a_prefix(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A location that does not start with the root fails the read before anything runs."""
    catalog = _local_catalog(session, principal, tmp_path / "root")
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal, ref, schema=Schema(NestedField(1, "id", LongType(), required=True))
    )
    elsewhere = f"file://{tmp_path}/elsewhere/{created.metadata.location.rsplit('/', 1)[1]}"
    moved = created.metadata.model_copy(update={"location": elsewhere})
    with (
        mock.patch(
            "lakana_core.catalog.service.CatalogService.load_table",
            return_value=mock.Mock(metadata=moved),
        ),
        mock.patch(
            "lakana_worker.pipeline.execute_query", side_effect=AssertionError("nothing may run")
        ),
    ):
        run = QueryService(session, AllowAllAuthorization()).submit_query(
            principal, "dev", "SELECT id FROM gold.sales"
        )
        run_one(session, run.id, AllowAllAuthorization())
    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "Resolving the tables this query reads failed (ValidationError)."


def test_run_one_fails_when_the_result_exceeds_the_row_cap(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A query whose result exceeds `limits.max_result_rows` fails, with no result written."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "region", StringType(), required=False),
        ),
    )
    arrow_schema = pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )
    data = pa.table({"id": [1, 2], "region": ["eu", "us"]}, schema=arrow_schema)
    _write_one_data_file_and_commit(catalog, principal, ref, created, data)

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id, region FROM gold.sales")

    run_one(session, run.id, AllowAllAuthorization(), limits=QueryLimitsConfig(max_result_rows=1))

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert "max_result_rows" in run.error_message
    assert run.result_location is None
    assert not (tmp_path / "_artifacts" / "queries" / str(run.id) / "result.arrow").exists()


def test_run_one_fails_when_the_result_exceeds_the_byte_cap(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A query whose result exceeds `limits.max_result_bytes` fails, with no result written."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "region", StringType(), required=False),
        ),
    )
    arrow_schema = pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )
    data = pa.table({"id": [1, 2], "region": ["eu", "us"]}, schema=arrow_schema)
    _write_one_data_file_and_commit(catalog, principal, ref, created, data)

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id, region FROM gold.sales")

    run_one(session, run.id, AllowAllAuthorization(), limits=QueryLimitsConfig(max_result_bytes=1))

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert "max_result_bytes" in run.error_message
    assert run.result_location is None
    assert not (tmp_path / "_artifacts" / "queries" / str(run.id) / "result.arrow").exists()


def test_run_one_fails_a_query_against_an_unauthorized_table(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A referenced table the principal cannot read fails the run, not the process."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.nonexistent")

    class _DenyRead:
        """Authorization stub that refuses every check, to force a per-table failure."""

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Deny unconditionally.

            Parameters
            ----------
            principal
                The acting principal.
            action
                The action requested.
            securable
                The object acted upon.

            Raises
            ------
            PermissionDenied
                Always.
            """
            raise PermissionDenied("denied")

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize` would, as a boolean.

            Parameters
            ----------
            principal
                The actor requesting the action.
            action
                The action requested.
            securable
                The object acted upon.

            Returns
            -------
            bool
                `False` if `authorize` raises `PermissionDenied`, else `True`.
            """
            try:
                self.authorize(principal, action, securable)
            except PermissionDenied:
                return False
            return True

    run_one(session, run.id, _DenyRead())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "Resolving the tables this query reads failed (PermissionDenied)."


def test_run_one_executes_an_external_table_query_end_to_end(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A query against a raw-zone EXTERNAL table runs the same way an ICEBERG one does."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1, 2]}), tmp_path / "raw" / "events" / "part-0.parquet")
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events"),
        location_pattern="raw/events/*.parquet",
        format="parquet",
    )

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id FROM raw.events")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED
    assert run.error_message is None
    assert run.result_location is not None


def test_run_one_fails_when_an_external_row_has_no_external_config(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A corrupted EXTERNAL row (no `external_config`) fails the run, not the process.

    `create_external_table` always sets `external_config`; this state is
    only reachable by inserting the row directly, the same technique
    `lakana-core`'s `test_tables.py` uses for states unreachable through the
    service itself.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="broken",
            table_type=TableType.EXTERNAL,
            external_config=None,
        )
    )
    session.commit()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM raw.broken")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None


def test_stored_nonconforming_external_pattern_fails_the_query(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A older row whose stored pattern no longer parses fails the query, not the process.

    `check_claim_free`/`parse_external_location` only run inside
    `create_external_table`, so a row created before this
    change — or inserted directly, as here — can hold a pattern outside
    `raw/<dir…>/<glob>`. The worker must refuse to read it rather than
    silently confining nothing, and must leave no result behind. Real data
    is seeded at the stored (non-conforming) location to prove it is never
    read, not just that the pattern would have matched nothing anyway. The
    stored `ValidationError`'s own message is not asserted verbatim through
    `run_one`: this module's docstring documents that every
    resolution-stage failure is reported only by its exception class name
    (`_resolve_sources_opaquely`), since resolution is what failed and
    there is no known-safe location list to redact against yet. The
    underlying reason is instead pinned directly against `_resolve_sources`,
    which is not opaque.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["gold"]))
    (tmp_path / "managed" / "gold" / "x").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "managed" / "gold" / "x" / "a.parquet")
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="x",
            table_type=TableType.EXTERNAL,
            external_config={
                "location_pattern": "managed/gold/x/*.parquet",
                "format": "parquet",
                "schema": {"id": "int64"},
            },
        )
    )
    session.commit()

    with pytest.raises(ValidationError, match="raw/"):
        _resolve_sources(session, "dev", principal, ["gold.x"], AllowAllAuthorization())

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.x")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert "ValidationError" in run.error_message
    assert run.result_location is None
    assert not (tmp_path / "_artifacts").exists()


def test_external_listing_is_not_recursive(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A file nested under the claim is not read: listing the claim is non-recursive."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "c" / "sub").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1, 2]}), tmp_path / "raw" / "c" / "a.parquet")
    pq.write_table(pa.table({"id": [3, 4, 5]}), tmp_path / "raw" / "c" / "sub" / "b.parquet")
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="c"),
        location_pattern="raw/c/*.parquet",
        format="parquet",
    )

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id FROM raw.c")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.result_location is not None
    with (tmp_path / run.result_location).open("rb") as f:
        result = pyarrow.ipc.open_file(f).read_all()
    assert result.num_rows == 2


def test_legacy_null_claim_rows_each_fail_query_via_claim_mismatch_not_overlap(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Two directly-inserted `EXTERNAL` rows sharing a claim both refuse to run.

    The two rows do not fail *because* they overlap each other: the
    worker's `row.external_claim == loc.claim` check refuses each one
    independently, on its own row — a `NULL` claim can never equal a
    freshly parsed one, regardless of what any other row claims. Neither
    row has `external_claim` set here (as a row created before the id-binding
    migration, or one the migration's backfill couldn't parse, would be); they
    share the same `location_pattern` only for setup convenience, not
    because the overlap is what this test exercises.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ns = catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "customers").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "customers" / "a.parquet")
    for name in ("a", "b"):
        session.add(
            Table(
                id=uuid.uuid4(),
                namespace_id=ns.id,
                name=name,
                table_type=TableType.EXTERNAL,
                external_config={
                    "location_pattern": "raw/customers/*.parquet",
                    "format": "parquet",
                    "schema": {"id": "int64"},
                },
                external_claim=None,
            )
        )
    session.commit()

    query_service = QueryService(session, AllowAllAuthorization())
    for name in ("a", "b"):
        run = query_service.submit_query(principal, "dev", f"SELECT * FROM raw.{name}")
        run_one(session, run.id, AllowAllAuthorization())
        session.refresh(run)
        assert run.status == QueryRunStatus.FAILED, name
        assert run.result_location is None, name


def test_worker_refuses_a_row_whose_claim_does_not_match_its_own_pattern(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A tampered `external_claim` that no longer matches the row's own pattern fails closed.

    `create_external_table` always sets `external_claim` from the same
    parse as `location_pattern`, so the two can only disagree if a row was
    altered directly (or is otherwise corrupted) after creation. The
    worker re-derives the claim from the pattern and refuses to trust a
    stored claim that disagrees with it.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "customers").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "customers" / "a.parquet")
    table = catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="customers"),
        location_pattern="raw/customers/*.parquet",
        format="parquet",
    )
    table.external_claim = "raw/tampered"
    session.commit()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM raw.customers")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.result_location is None


def test_worker_check_claim_free_catches_a_tampered_overlap_at_query_time(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Defense in depth: the worker also re-checks exclusivity, not only its own claim match.

    Two tables are legitimately created with disjoint claims, then one is
    tampered directly to claim the other's directory (bypassing
    `check_claim_free`, which only runs inside `create_external_table`).
    Its own `external_claim` still matches its own (also tampered)
    pattern, so the simpler match check alone would not catch this — the
    worker's own `check_claim_free(..., exclude_table_id=row.id)` call
    does.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "customers").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "customers" / "a.parquet")
    (tmp_path / "raw" / "orders").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "orders" / "a.parquet")
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="customers"),
        location_pattern="raw/customers/*.parquet",
        format="parquet",
    )
    orders = catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="orders"),
        location_pattern="raw/orders/*.parquet",
        format="parquet",
    )
    # Simulate a mutation that bypassed `check_claim_free` entirely (a
    # direct DB edit, or a future bug) rather than something reachable
    # through `create_external_table` today.
    orders.external_config = {
        **(orders.external_config or {}),
        "location_pattern": "raw/customers/*.parquet",
    }
    orders.external_claim = "raw/customers"
    session.commit()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM raw.orders")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.result_location is None


def _committed_entries(resolved: ResolvedStorage, loaded: LoadedTable) -> list[ManifestEntry]:
    """Fetch the live manifest entries of a committed table's current snapshot.

    Parameters
    ----------
    resolved
        Storage the table's files are written under.
    loaded
        The table, at a metadata location with a current snapshot.

    Returns
    -------
    list of ManifestEntry
        Every live entry (deleted ones excluded) across all of the current
        snapshot's manifests.
    """
    snapshot = loaded.metadata.current_snapshot()
    assert snapshot is not None
    entries: list[ManifestEntry] = []
    for manifest in snapshot.manifests(resolved.file_io):
        entries.extend(manifest.fetch_manifest_entry(resolved.file_io, discard_deleted=True))
    return entries


def _forge_snapshot_under_own_location(
    resolved: ResolvedStorage,
    loaded: LoadedTable,
    entries: list[ManifestEntry],
    snapshot_id: int,
) -> Snapshot:
    """Write a manifest and manifest list under `loaded`'s own location, listing `entries`.

    Simulates a commit forging Iceberg metadata directly (bypassing any real
    write path) rather than a legitimate append, to reproduce the
    location-confinement exploit: a manifest is Iceberg's own metadata, so nothing about
    `commit_table`'s id-scoped location assignment inspects which files a
    manifest actually names.

    Parameters
    ----------
    resolved
        Storage `loaded`'s files are written under.
    loaded
        The table the forged snapshot will be committed to. Only its
        current metadata (format version, schema, partition spec, location)
        is used to shape the manifest; `entries` decide what it claims to
        contain.
    entries
        The manifest entries to write into the forged manifest, verbatim.
    snapshot_id
        The forged snapshot's id.

    Returns
    -------
    Snapshot
        A snapshot object pointing at the freshly written manifest list,
        ready to be committed with `AddSnapshotUpdate` and
        `SetSnapshotRefUpdate`.
    """
    manifest_path = f"{loaded.metadata.location}/metadata/{snapshot_id}-forged-manifest.avro"
    with write_manifest(
        format_version=loaded.metadata.format_version,
        spec=loaded.metadata.spec(),
        schema=loaded.metadata.schema(),
        output_file=resolved.file_io.new_output(manifest_path),
        snapshot_id=snapshot_id,
        avro_compression="null",
    ) as writer:
        for entry in entries:
            writer.add(entry)
    manifest_file = writer.to_manifest_file()

    manifest_list_path = f"{loaded.metadata.location}/metadata/{snapshot_id}-forged-snap.avro"
    with write_manifest_list(
        format_version=loaded.metadata.format_version,
        output_file=resolved.file_io.new_output(manifest_list_path),
        snapshot_id=snapshot_id,
        parent_snapshot_id=None,
        sequence_number=1,
        avro_compression="null",
    ) as list_writer:
        list_writer.add_manifests([manifest_file])

    return Snapshot(
        snapshot_id=snapshot_id,
        parent_snapshot_id=None,
        sequence_number=1,
        timestamp_ms=int(time.time() * 1000),
        manifest_list=manifest_list_path,
        summary=Summary(operation=Operation.APPEND),
        schema_id=loaded.metadata.current_schema_id,
    )


def _commit_forged_snapshot(
    catalog: CatalogService, principal: PrincipalRef, ref: TableRef, snapshot: Snapshot
) -> LoadedTable:
    """Commit a forged `Snapshot` as a table's new current snapshot on `main`.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    ref
        The table to commit to.
    snapshot
        The forged snapshot, already written to storage.

    Returns
    -------
    LoadedTable
        The table at its new metadata location.
    """
    return catalog.commit_table(
        principal,
        ref,
        requirements=[],
        updates=[
            AddSnapshotUpdate(snapshot=snapshot),
            SetSnapshotRefUpdate(
                ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=snapshot.snapshot_id
            ),
        ],
    )


class _DeleteManifestWriter(ManifestWriterV2):
    """A V2 manifest writer that produces a real `DELETES`-content manifest (tests).

    pyiceberg 0.12's own `write_manifest` only ever dispatches to
    `ManifestWriterV2`, which hardcodes `content() -> ManifestContent.DATA`
    -- there is no public way to ask it for a delete manifest, even though
    the format supports one (that is exactly what an external engine
    writes for a merge-on-read `UPDATE`/`DELETE`). Overriding
    `content` is enough to change what `to_manifest_file()` records in the
    `ManifestFile` the manifest list stores; `_meta`'s own `"content"` Avro
    metadata key (set by `ManifestWriterV2._meta`, hardcoded to `"data"`) is
    overridden too, so a manifest written this way is indistinguishable
    from a real one on both axes the format defines it by.
    """

    def content(self) -> ManifestContent:
        """Report this manifest's own content kind.

        Returns
        -------
        ManifestContent
            Always `DELETES`, unlike the base class's hardcoded `DATA`.
        """
        return ManifestContent.DELETES

    @property
    def _meta(self) -> dict[str, str]:
        """The Avro file metadata written alongside the manifest's entries.

        Returns
        -------
        dict
            The base class's metadata with `"content"` corrected to
            `"deletes"`.
        """
        return {**super()._meta, "content": "deletes"}


def _position_delete_data_file(
    resolved: ResolvedStorage, table_location: str, name: str
) -> DataFile:
    """Write a real, small position-delete Parquet file and describe it as a `DataFile`.

    The file itself (`file_path`, `pos` columns, per the Iceberg spec's
    position-delete format) is never read by the code path under test --
    `list_data_files` refuses the table before any file's bytes are
    touched -- but it is written for real, under the table's own location,
    rather than merely referenced by a made-up path, so the scenario is a
    delete file in every respect except which engine wrote it.

    Parameters
    ----------
    resolved
        Storage to write the Parquet file through.
    table_location
        The owning table's own location; the file is written under its
        `data/` directory, alongside where a real data file would live.
    name
        A file name unique within this test, so parallel tests sharing a
        `tmp_path` root never collide.

    Returns
    -------
    DataFile
        `content=DataFileContent.POSITION_DELETES`, pointing at the file
        just written, with an empty (unpartitioned) partition record and
        no column statistics -- none of which the code under test reads.
    """
    delete_table = pa.table({"file_path": ["unused"], "pos": [0]})
    file_path = f"{table_location}/data/{name}.parquet"
    local_path = Path(file_path.removeprefix("file://"))
    local_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(delete_table, local_path)
    file_size = local_path.stat().st_size
    return DataFile.from_args(
        content=DataFileContent.POSITION_DELETES,
        file_path=file_path,
        file_format=FileFormat.PARQUET,
        partition=Record(),
        record_count=1,
        file_size_in_bytes=file_size,
        column_sizes={},
        value_counts={},
        null_value_counts={},
        nan_value_counts={},
        lower_bounds={},
        upper_bounds={},
        key_metadata=None,
        split_offsets=None,
        equality_ids=None,
        sort_order_id=None,
    )


def _write_delete_manifest(
    resolved: ResolvedStorage,
    committed: LoadedTable,
    delete_file: DataFile,
    new_snapshot_id: int,
    *,
    live: bool,
) -> ManifestFile:
    """Write a real `DELETES`-content manifest naming one position-delete file.

    Parameters
    ----------
    resolved
        Storage to write the manifest through.
    committed
        The table the manifest belongs to; only its current metadata
        (format version, schema, partition spec, location) shapes the
        manifest.
    delete_file
        The delete `DataFile` the manifest's single entry names.
    new_snapshot_id
        The snapshot this manifest is written for.
    live
        `True` writes the entry with status `ADDED` (a live delete, the
        case that must block a query). `False` writes it with status
        `DELETED` (the delete file has itself since been superseded/
        compacted away) -- `fetch_manifest_entry`'s own `discard_deleted`
        excludes it, so this manifest must not block a query even though
        its `ManifestFile.content` is still `DELETES`.

    Returns
    -------
    ManifestFile
        Ready to pass to `write_manifest_list.add_manifests`, alongside the
        table's other, real manifests.
    """
    entry_template = ManifestEntry.from_args(
        status=ManifestEntryStatus.ADDED,
        snapshot_id=new_snapshot_id,
        sequence_number=1,
        file_sequence_number=1,
        data_file=delete_file,
    )
    manifest_path = f"{committed.metadata.location}/metadata/{new_snapshot_id}-delete-manifest.avro"
    with _DeleteManifestWriter(
        spec=committed.metadata.spec(),
        schema=committed.metadata.schema(),
        output_file=resolved.file_io.new_output(manifest_path),
        snapshot_id=new_snapshot_id,
        avro_compression="null",
    ) as writer:
        if live:
            writer.add(entry_template)
        else:
            writer.delete(entry_template)
    return writer.to_manifest_file()


def _commit_snapshot_with_delete_manifest(
    catalog: CatalogService,
    resolved: ResolvedStorage,
    principal: PrincipalRef,
    ref: TableRef,
    committed: LoadedTable,
    delete_manifest: ManifestFile,
    new_snapshot_id: int,
) -> LoadedTable:
    """Commit a new snapshot whose manifest list is the table's real manifest plus a delete one.

    Mirrors what an external engine's merge-on-read `UPDATE`/`DELETE`
    commits through the Iceberg REST catalog: a new manifest list carrying
    forward the previous snapshot's data manifest(s) unchanged, plus one
    new delete manifest -- committed through the real `commit_table`
    compare-and-swap path, the same path an external writer actually uses
    (preferred over hand-writing `metadata.json`).

    Parameters
    ----------
    catalog
        The catalog under test.
    resolved
        Storage to write the manifest list through.
    principal
        The acting principal.
    ref
        The table to commit to.
    committed
        The table at its previous, real snapshot.
    delete_manifest
        The delete manifest to add alongside the previous snapshot's own.
    new_snapshot_id
        The new snapshot's id.

    Returns
    -------
    LoadedTable
        The table at its new metadata location, with `new_snapshot_id`
        current on `main`.
    """
    previous_snapshot = committed.metadata.current_snapshot()
    assert previous_snapshot is not None
    assert previous_snapshot.sequence_number is not None
    real_manifests = list(previous_snapshot.manifests(resolved.file_io))
    manifest_list_path = (
        f"{committed.metadata.location}/metadata/{new_snapshot_id}-manifest-list.avro"
    )
    new_sequence_number = previous_snapshot.sequence_number + 1
    with write_manifest_list(
        format_version=committed.metadata.format_version,
        output_file=resolved.file_io.new_output(manifest_list_path),
        snapshot_id=new_snapshot_id,
        parent_snapshot_id=committed.metadata.current_snapshot_id,
        sequence_number=new_sequence_number,
        avro_compression="null",
    ) as list_writer:
        list_writer.add_manifests([*real_manifests, delete_manifest])
    forged_snapshot = Snapshot(
        snapshot_id=new_snapshot_id,
        parent_snapshot_id=committed.metadata.current_snapshot_id,
        sequence_number=new_sequence_number,
        timestamp_ms=int(time.time() * 1000),
        manifest_list=manifest_list_path,
        summary=Summary(operation=Operation.OVERWRITE),
        schema_id=committed.metadata.current_schema_id,
    )
    return _commit_forged_snapshot(catalog, principal, ref, forged_snapshot)


def test_resolve_sources_refuses_a_table_with_a_live_delete_file(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`_resolve_sources` refuses a table whose current snapshot has a live delete file.

    (.) `list_data_files` walks every manifest of the current snapshot,
    including a `DELETES` one -- if nothing refused this, its entries'
    `file_path`s would have been returned and read as Parquet data rows by
    the worker, silently returning rows an external engine (a
    merge-on-read `UPDATE`/`DELETE` through Lakana's Iceberg REST catalog)
    meant deleted. `_resolve_sources` must instead raise
    `UnsupportedTableError` naming the table.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="t")
    created = catalog.create_table(principal, ref, schema=schema)
    committed = _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table(
            {"id": [1, 2], "region": ["eu", "us"]},
            schema=pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
            ),
        ),
    )
    resolved = StorageResolver(session).for_environment("dev")
    delete_file = _position_delete_data_file(resolved, committed.metadata.location, "delete-0")
    delete_manifest = _write_delete_manifest(
        resolved, committed, delete_file, new_snapshot_id=900_000_101, live=True
    )
    committed_with_delete = _commit_snapshot_with_delete_manifest(
        catalog, resolved, principal, ref, committed, delete_manifest, new_snapshot_id=900_000_101
    )
    assert committed_with_delete.metadata.current_snapshot_id == 900_000_101

    with pytest.raises(UnsupportedTableError, match=r"gold\.t.*row-level deletes"):
        _resolve_sources(session, "dev", principal, ["gold.t"], AllowAllAuthorization())


def test_a_query_that_reads_an_artifact_says_only_a_python_asset_can(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`_resolve_sources` refuses an artifact and names the way to read one.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The environment's storage root.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(NestedField(1, "id", LongType(), required=True))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="t")
    catalog.create_table(principal, ref, schema=schema)
    row = session.scalars(select(Table).where(Table.name == "t")).one()
    row.table_type = TableType.ARTIFACT
    row.metadata_location = None
    session.commit()
    with pytest.raises(UnsupportedTableError) as caught:
        _resolve_sources(session, "dev", principal, ["gold.t"], AllowAllAuthorization())
    assert str(caught.value) == (
        "gold.t is an artifact; SQL cannot read it. Only a Python asset can read an artifact: "
        "declare it in the asset's inputs."
    )


def test_a_caller_who_may_not_read_an_artifact_is_not_told_it_is_one(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Authorization comes first: `_resolve_sources` answers a denied caller as for a table.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The environment's storage root.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(NestedField(1, "id", LongType(), required=True))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="t")
    catalog.create_table(principal, ref, schema=schema)
    row = session.scalars(select(Table).where(Table.name == "t")).one()
    row.table_type = TableType.ARTIFACT
    row.metadata_location = None
    session.commit()
    with pytest.raises(PermissionDenied) as caught:
        _resolve_sources(session, "dev", principal, ["gold.t"], ActionAllowList())
    assert "artifact" not in str(caught.value)


def test_a_query_against_a_table_with_delete_files_fails_without_leaking_a_path(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A submitted query against such a table ends `FAILED`, naming the reason, not a path.

    (.) `_resolve_sources_opaquely` makes every resolution failure's
    message opaque except this one -- `UnsupportedTableError`'s own message
    is built entirely from the dotted table name the submitter's query
    already named, so it is safe to surface verbatim, unlike a path on the
    worker's own disk or a presigned URL.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="t")
    created = catalog.create_table(principal, ref, schema=schema)
    committed = _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table(
            {"id": [1, 2], "region": ["eu", "us"]},
            schema=pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
            ),
        ),
    )
    resolved = StorageResolver(session).for_environment("dev")
    delete_file = _position_delete_data_file(resolved, committed.metadata.location, "delete-0")
    delete_manifest = _write_delete_manifest(
        resolved, committed, delete_file, new_snapshot_id=900_000_102, live=True
    )
    _commit_snapshot_with_delete_manifest(
        catalog, resolved, principal, ref, committed, delete_manifest, new_snapshot_id=900_000_102
    )

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.t")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.result_location is None
    assert run.error_message is not None
    assert "row-level deletes" in run.error_message
    assert "gold.t" in run.error_message
    assert str(tmp_path) not in run.error_message
    assert "file://" not in run.error_message


def test_a_delete_manifest_with_only_deleted_entries_does_not_block_the_query(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A `DELETES` manifest whose every entry is superseded (`DELETED` status) is not live.

    (.) `fetch_manifest_entry(discard_deleted=True)` already excludes a
    `DELETED`-status entry, so a delete file that was itself compacted away
    leaves behind a manifest with `content=DELETES` but zero *live*
    entries -- that must not refuse the table; the data rows must still
    come back.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="t")
    created = catalog.create_table(principal, ref, schema=schema)
    committed = _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table(
            {"id": [1, 2], "region": ["eu", "us"]},
            schema=pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
            ),
        ),
    )
    resolved = StorageResolver(session).for_environment("dev")
    delete_file = _position_delete_data_file(resolved, committed.metadata.location, "delete-0")
    delete_manifest = _write_delete_manifest(
        resolved, committed, delete_file, new_snapshot_id=900_000_103, live=False
    )
    _commit_snapshot_with_delete_manifest(
        catalog, resolved, principal, ref, committed, delete_manifest, new_snapshot_id=900_000_103
    )

    sources = _resolve_sources(session, "dev", principal, ["gold.t"], AllowAllAuthorization())
    assert len(sources["gold.t"].urls) == 1

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id, region FROM gold.t ORDER BY id")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED
    assert run.error_message is None
    result = _read_result(tmp_path, run)
    assert result.column("id").to_pylist() == [1, 2]
    assert result.column("region").to_pylist() == ["eu", "us"]


def test_manifest_listing_another_tables_files_fails(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A manifest committed under one table's own location cannot vouch for another's files.

    Reproduces the location-confinement exploit: `gold.customers` has real, legitimately
    committed data. `gold.attacker` is a second table, created normally (so
    it gets its own id-suffixed location), whose current snapshot is then
    replaced by a forged one — written into `gold.attacker`'s own metadata
    directory, through the real `commit_table` CAS path — that lists
    `gold.customers`' actual data file. Grants and policies bind to
    `gold.attacker`'s id, but id-binding alone says nothing about which
    files a manifest names, so the worker's own containment check is what
    must catch this.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )
    customers_ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
    )
    created_customers = catalog.create_table(principal, customers_ref, schema=schema)
    data = pa.table(
        {"id": [1, 2], "region": ["eu", "us"]},
        schema=pa.schema(
            [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
        ),
    )
    committed_customers = _write_one_data_file_and_commit(
        catalog, principal, customers_ref, created_customers, data
    )

    attacker_ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="attacker"
    )
    created_attacker = catalog.create_table(principal, attacker_ref, schema=schema)

    resolved = StorageResolver(session).for_environment("dev")
    foreign_entries = _committed_entries(resolved, committed_customers)
    forged = _forge_snapshot_under_own_location(
        resolved, created_attacker, foreign_entries, snapshot_id=900_000_001
    )
    committed_attacker = _commit_forged_snapshot(catalog, principal, attacker_ref, forged)
    assert committed_attacker.metadata.current_snapshot_id == forged.snapshot_id

    with pytest.raises(ValidationError, match="outside its location"):
        _resolve_sources(session, "dev", principal, ["gold.attacker"], AllowAllAuthorization())

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.attacker")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.result_location is None


def test_data_file_in_sibling_directory_with_shared_prefix_fails(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A sibling directory that merely shares a table's location as a string prefix still fails.

    `gold.orders` is a real table at
    ``.../orders-<hex>``. Its own legitimately committed entry is mutated to
    point at ``.../orders-<hex>2/...`` — a distinct, sibling directory that
    is not nested under the real one — and forged back in as the table's
    current snapshot. `is_within_location` must not treat this as contained
    just because the string `.../orders-<hex>` is a prefix of
    `.../orders-<hex>2`.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )
    orders_ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="orders"
    )
    created_orders = catalog.create_table(principal, orders_ref, schema=schema)
    data = pa.table(
        {"id": [1], "region": ["eu"]},
        schema=pa.schema(
            [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
        ),
    )
    committed_orders = _write_one_data_file_and_commit(
        catalog, principal, orders_ref, created_orders, data
    )

    resolved = StorageResolver(session).for_environment("dev")
    entries = _committed_entries(resolved, committed_orders)
    assert len(entries) == 1
    real_path = entries[0].data_file.file_path
    sibling_location = committed_orders.metadata.location.rstrip("/") + "2"
    entries[0].data_file[1] = real_path.replace(
        committed_orders.metadata.location, sibling_location
    )

    forged = _forge_snapshot_under_own_location(
        resolved, committed_orders, entries, snapshot_id=900_000_002
    )
    _commit_forged_snapshot(catalog, principal, orders_ref, forged)

    with pytest.raises(ValidationError, match="outside its location"):
        _resolve_sources(session, "dev", principal, ["gold.orders"], AllowAllAuthorization())

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.orders")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.result_location is None


def test_manifest_entry_with_a_traversal_escape_fails(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A `..`-laden data file path that textually starts with the table's own location fails.

    `gold.attacker`'s own, legitimately
    committed entry is mutated so its `file_path` reads
    ``<attacker's own location>/../<gold.customers's directory>/data/<file>``
    -- a string that literally begins with attacker's own location (so a
    naive `str.startswith` containment check would have accepted it) but
    resolves, once a storage backend normalises it, to a file under
    `gold.customers`' own directory instead. `gold.customers` has real data
    at that resolved path, proving this is not merely a string exercise:
    the exploit would have let `gold.attacker` read `gold.customers`' rows
    under its own grants and policies.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )
    arrow_schema = pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )
    customers_ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
    )
    created_customers = catalog.create_table(principal, customers_ref, schema=schema)
    customers_data = pa.table({"id": [1], "region": ["eu"]}, schema=arrow_schema)
    committed_customers = _write_one_data_file_and_commit(
        catalog, principal, customers_ref, created_customers, customers_data
    )
    resolved = StorageResolver(session).for_environment("dev")
    customers_entry = _committed_entries(resolved, committed_customers)[0]
    customers_dir_name = committed_customers.metadata.location.rsplit("/", 1)[-1]
    customers_filename = customers_entry.data_file.file_path.rsplit("/", 1)[-1]

    attacker_ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="attacker"
    )
    created_attacker = catalog.create_table(principal, attacker_ref, schema=schema)
    attacker_data = pa.table({"id": [2], "region": ["us"]}, schema=arrow_schema)
    committed_attacker = _write_one_data_file_and_commit(
        catalog, principal, attacker_ref, created_attacker, attacker_data
    )
    attacker_entries = _committed_entries(resolved, committed_attacker)
    assert len(attacker_entries) == 1
    traversal_path = (
        f"{committed_attacker.metadata.location}/../{customers_dir_name}/data/{customers_filename}"
    )
    attacker_entries[0].data_file[1] = traversal_path

    forged = _forge_snapshot_under_own_location(
        resolved, committed_attacker, attacker_entries, snapshot_id=900_000_003
    )
    _commit_forged_snapshot(catalog, principal, attacker_ref, forged)

    with pytest.raises(ValidationError, match="outside its location"):
        _resolve_sources(session, "dev", principal, ["gold.attacker"], AllowAllAuthorization())

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.attacker")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.result_location is None


def test_manifest_entry_with_a_glob_pattern_fails(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A data file path containing a glob metacharacter fails, even inside the table's own dir.

    `gold.attacker`
    is a normal, id-suffixed table (unaffected by the
    location-scheme gate), so this specifically exercises the glob check
    inside `is_within_location` rather than that gate or the
    canonical-path check. Reproduces the finding's exploit shape (a wildcard
    standing in for a path component DuckDB's `read_parquet` would expand
    against the local filesystem) confined to the attacker's own directory:
    even a glob that never leaves the attacker's own location must still be
    refused, since the point is that `read_parquet` -- not this check --
    decides what a glob matches, and this check must never hand it one.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )
    arrow_schema = pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )
    attacker_ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="attacker"
    )
    created_attacker = catalog.create_table(principal, attacker_ref, schema=schema)
    attacker_data = pa.table({"id": [1], "region": ["eu"]}, schema=arrow_schema)
    committed_attacker = _write_one_data_file_and_commit(
        catalog, principal, attacker_ref, created_attacker, attacker_data
    )
    resolved = StorageResolver(session).for_environment("dev")
    attacker_entries = _committed_entries(resolved, committed_attacker)
    assert len(attacker_entries) == 1
    glob_path = f"{committed_attacker.metadata.location}/data/*.parquet"
    attacker_entries[0].data_file[1] = glob_path

    forged = _forge_snapshot_under_own_location(
        resolved, committed_attacker, attacker_entries, snapshot_id=900_000_007
    )
    _commit_forged_snapshot(catalog, principal, attacker_ref, forged)

    with pytest.raises(ValidationError, match="outside its location"):
        _resolve_sources(session, "dev", principal, ["gold.attacker"], AllowAllAuthorization())

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.attacker")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.result_location is None


def _insert_legacy_table(
    session: Session,
    resolved: ResolvedStorage,
    namespace_id: uuid.UUID,
    schema: Schema,
    name: str,
    location: str,
) -> uuid.UUID:
    """Insert an `ICEBERG` table row directly at `location`, bypassing `create_table`.

    Simulates a table created before locations carried the table's id (or its `confine_to_root`
    predecessor), whose location is not the id-suffixed leaf form
    `ResolvedStorage.table_location` now assigns. No snapshot is written:
    the worker's location-scheme check runs
    unconditionally, before a table's data is ever inspected.

    Parameters
    ----------
    session
        Database session.
    resolved
        Storage the metadata file is written under.
    namespace_id
        The namespace to create the row in.
    schema
        The table's schema.
    name
        The table's name.
    location
        The table's (legacy-shaped) root location.

    Returns
    -------
    uuid.UUID
        The inserted row's id.
    """
    metadata = build_table_metadata(
        schema=schema, location=location, partition_spec=None, sort_order=None, properties={}
    )
    target = f"{location}/metadata/00000-legacy.metadata.json"
    write_metadata(resolved.file_io, metadata, target)
    table_id = uuid.uuid4()
    session.add(
        Table(
            id=table_id,
            namespace_id=namespace_id,
            name=name,
            table_type=TableType.ICEBERG,
            metadata_location=target,
        )
    )
    session.commit()
    return table_id


def test_legacy_table_without_an_id_suffix_fails_the_query(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A table at a plain leaf directory, not id-suffixed, fails the query outright.

    A location without the id fails closed.
    `gold.legacy` sits at `managed/gold/legacy` -- a shape the earlier
    `confine_to_root` check allowed, since it only required the value to lie
    inside the storage root, not to end in `-<table id hex>`. The worker
    refuses it outright, before listing any file, rather than trusting the
    "most specific owner" rule to be complete enough to catch every way a
    legacy table's location could be abused: that rule's "other locations"
    set can never include an `EXTERNAL` table's `raw/` claim or a dropped
    table's orphaned files, since only currently-existing `ICEBERG` rows
    contribute to it.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(NestedField(1, "id", LongType(), required=True))
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["gold"]))
    resolved = StorageResolver(session).for_environment("dev")
    _insert_legacy_table(
        session, resolved, ns.id, schema, "legacy", f"file://{tmp_path}/managed/gold/legacy"
    )

    with pytest.raises(ValidationError, match="was created by an older Lakana"):
        _resolve_sources(session, "dev", principal, ["gold.legacy"], AllowAllAuthorization())

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.legacy")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.result_location is None


def test_legacy_root_location_table_fails_the_query(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A table at the environment root itself -- the broadest legacy shape -- fails the query.

    A location without the id fails closed.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(NestedField(1, "id", LongType(), required=True))
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["gold"]))
    resolved = StorageResolver(session).for_environment("dev")
    _insert_legacy_table(session, resolved, ns.id, schema, "legacy", f"file://{tmp_path}")

    with pytest.raises(ValidationError, match="was created by an older Lakana"):
        _resolve_sources(session, "dev", principal, ["gold.legacy"], AllowAllAuthorization())

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.legacy")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.result_location is None


def test_suffixed_table_still_reads_its_files_beside_a_legacy_table(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A normal, id-suffixed table still reads its own files, even with a legacy table nearby.

    Proves the location-scheme gate does not
    reject every table, only ones whose own location fails it --
    `gold.customers` is a real, normally-created table in the same
    environment and namespace as `gold.legacy`, and its query succeeds
    normally.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )
    ns = catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["gold"]))
    resolved = StorageResolver(session).for_environment("dev")
    _insert_legacy_table(
        session, resolved, ns.id, schema, "legacy", f"file://{tmp_path}/managed/gold"
    )

    customers_ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
    )
    created_customers = catalog.create_table(principal, customers_ref, schema=schema)
    _write_one_data_file_and_commit(
        catalog,
        principal,
        customers_ref,
        created_customers,
        pa.table(
            {"id": [1], "region": ["eu"]},
            schema=pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
            ),
        ),
    )

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.customers")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert run.result_location is not None


def test_worker_refuses_a_snapshot_whose_manifest_list_lies_outside_the_location(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The worker refuses to read a snapshot whose manifest list lies outside the table's location.

    `commit_table`
    itself now refuses an `AddSnapshotUpdate` with a foreign
    `manifest_list` (`test_commit_refuses_a_foreign_manifest_list`,
    `test_tables.py`), so this scenario is set up by writing the metadata
    directly and patching the catalog row's `metadata_location` in the
    session, bypassing `commit_table` entirely -- simulating a row altered
    outside it (a direct mutation, a bug, or a pre-fix-round row) rather
    than exercising the commit-time guard again. The worker's own check
    inside `list_data_files` must still catch it: nothing about a query
    against this row goes through `commit_table` at all.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="t")
    created = catalog.create_table(principal, ref, schema=schema)
    committed = _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table(
            {"id": [1], "region": ["eu"]},
            schema=pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
            ),
        ),
    )
    resolved = StorageResolver(session).for_environment("dev")

    foreign_manifest_list = f"file://{tmp_path}/OUTSIDE/fake-manifest-list.avro"
    forged_snapshot = Snapshot(
        snapshot_id=900_000_005,
        parent_snapshot_id=committed.metadata.current_snapshot_id,
        sequence_number=2,
        timestamp_ms=int(time.time() * 1000),
        manifest_list=foreign_manifest_list,
        summary=Summary(operation=Operation.APPEND),
        schema_id=committed.metadata.current_schema_id,
    )
    updated_metadata = apply_updates(
        committed.metadata,
        [
            AddSnapshotUpdate(snapshot=forged_snapshot),
            SetSnapshotRefUpdate(
                ref_name="main",
                type=SnapshotRefType.BRANCH,
                snapshot_id=forged_snapshot.snapshot_id,
            ),
        ],
    )
    new_target = f"{committed.metadata.location}/metadata/00001-bypassed.metadata.json"
    write_metadata(resolved.file_io, updated_metadata, new_target)
    # Bypasses `commit_table`'s CAS and its `AddSnapshotUpdate` guard
    # entirely: the row is patched directly, simulating a row altered
    # outside the normal write path.
    table_row = session.get(Table, created.uuid)
    assert table_row is not None
    table_row.metadata_location = new_target
    session.commit()

    with pytest.raises(ValidationError, match="manifest list"):
        _resolve_sources(session, "dev", principal, ["gold.t"], AllowAllAuthorization())

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.t")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.result_location is None


def test_worker_refuses_a_manifest_that_lies_outside_the_location(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The worker refuses a manifest whose own path lies outside the table's location.

    Unlike the
    manifest-list case, `commit_table`'s own `AddSnapshotUpdate` guard does
    not catch this: it only inspects `snapshot.manifest_list`, never the
    manifests that list in turn points at, so a manifest list that is
    itself legitimately inside the table's location -- and therefore passes
    `commit_table`'s check -- can still lead to a manifest filed somewhere
    else entirely. This is committed through the real `commit_table` CAS
    path, unlike the sibling manifest-list test.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    schema = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "region", StringType(), required=False),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="t")
    created = catalog.create_table(principal, ref, schema=schema)
    committed = _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table(
            {"id": [1], "region": ["eu"]},
            schema=pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
            ),
        ),
    )
    resolved = StorageResolver(session).for_environment("dev")
    entries = _committed_entries(resolved, committed)

    snapshot_id = 900_000_006
    # The manifest itself is written *outside* `t`'s own location, unlike
    # `_forge_snapshot_under_own_location`'s helper, which always writes
    # both files under the table's own location.
    foreign_manifest_path = (
        f"file://{tmp_path}/OUTSIDE_MANIFESTS/{snapshot_id}-forged-manifest.avro"
    )
    with write_manifest(
        format_version=committed.metadata.format_version,
        spec=committed.metadata.spec(),
        schema=committed.metadata.schema(),
        output_file=resolved.file_io.new_output(foreign_manifest_path),
        snapshot_id=snapshot_id,
        avro_compression="null",
    ) as writer:
        for entry in entries:
            writer.add(entry)
    manifest_file = writer.to_manifest_file()

    # The manifest *list*, unlike the manifest it names, is written under
    # `t`'s own location, so `commit_table`'s `AddSnapshotUpdate` guard
    # (which checks only the manifest list) passes it through.
    manifest_list_path = f"{committed.metadata.location}/metadata/{snapshot_id}-forged-snap.avro"
    with write_manifest_list(
        format_version=committed.metadata.format_version,
        output_file=resolved.file_io.new_output(manifest_list_path),
        snapshot_id=snapshot_id,
        parent_snapshot_id=committed.metadata.current_snapshot_id,
        sequence_number=2,
        avro_compression="null",
    ) as list_writer:
        list_writer.add_manifests([manifest_file])

    forged_snapshot = Snapshot(
        snapshot_id=snapshot_id,
        parent_snapshot_id=committed.metadata.current_snapshot_id,
        sequence_number=2,
        timestamp_ms=int(time.time() * 1000),
        manifest_list=manifest_list_path,
        summary=Summary(operation=Operation.APPEND),
        schema_id=committed.metadata.current_schema_id,
    )
    committed_again = _commit_forged_snapshot(catalog, principal, ref, forged_snapshot)
    assert committed_again.metadata.current_snapshot_id == snapshot_id

    with pytest.raises(ValidationError, match="manifest"):
        _resolve_sources(session, "dev", principal, ["gold.t"], AllowAllAuthorization())

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.t")
    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.result_location is None


def test_run_one_raises_not_found_for_an_unknown_query_run(session: Session) -> None:
    """A caller passing an id absent from `query_runs` gets a clear error, not an AttributeError."""
    with pytest.raises(NotFoundError):
        run_one(session, uuid.uuid4(), AllowAllAuthorization())


def test_run_one_records_a_sqlglot_parse_error_as_a_syntax_error(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A sqlglot `ParseError` (not a `LakanaError`) is recorded as a syntax error.

    `check_blacklist` and friends call `sqlglot.parse_one` with no wrapping
    of its own `ParseError`, since `QueryService.submit_query` does not
    validate SQL before enqueueing it — a genuinely malformed query only
    fails once `run_one` tries to parse it.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT FROM (((")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith("SQL syntax error")
    assert run.error_cause == "invalid_query"


def _local_catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Create a `dev` environment rooted at `tmp_path` and return its catalog.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        Directory to use as the environment's storage root.

    Returns
    -------
    CatalogService
        A catalog whose `dev` environment already exists.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    return catalog


def _read_result(tmp_path: Path, run: QueryRun) -> pa.Table:
    """Read back the Arrow file a successful run wrote.

    Parameters
    ----------
    tmp_path
        The environment's storage root.
    run
        The finished, successful run.

    Returns
    -------
    pyarrow.Table
        The query's result as it was persisted.
    """
    assert run.result_location is not None
    with pyarrow.ipc.open_file(tmp_path / run.result_location) as reader:
        return reader.read_all()


def test_run_one_refuses_a_run_whose_principal_no_longer_exists(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A deleted submitter means no identity to authorize as, so the run is refused.

    Synthesising a `PrincipalRef` here would have made every per-table
    authorization decision in the run on an identity that never existed.
    """
    _local_catalog(session, principal, tmp_path)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT 1 AS x")
    run.principal_id = None
    session.commit()

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == _DELETED_PRINCIPAL_MESSAGE
    assert run.result_location is None


def test_run_one_refuses_a_pat_submitted_run_whose_principal_is_now_inactive(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A PAT-submitted run whose principal was deactivated after submission fails closed.

    Mirrors the task-token path (`_INVALID_TASK_TOKEN_MESSAGE`): a
    principal deactivated between submission and execution must not have
    its queued query run on its behalf, whether it was submitted with a
    task token or a PAT. The
    failure is recorded in the same transaction as the status change, and
    before any parsing.
    """
    _local_catalog(session, principal, tmp_path)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT 1 AS x")
    principal_row = session.get(Principal, principal.id)
    assert principal_row is not None
    principal_row.is_active = False
    session.commit()

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == _INACTIVE_PRINCIPAL_MESSAGE
    assert run.result_location is None
    events = (
        session.execute(
            select(AuditEvent).where(
                AuditEvent.event_type == AuditEventType.QUERY_COMPLETED,
                AuditEvent.principal_id == principal.id,
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


def _verify_minted_token(task_tokens: TaskTokenService, minted: MintedTaskToken) -> PrincipalRef:
    """Verify a freshly minted task token and return its resolved `PrincipalRef`.

    Mirrors `lakana_server.deps.get_principal`'s own real path from a minted
    token to an authenticated `PrincipalRef` carrying `task_scope`, rather
    than constructing a `TaskScope` by hand — the exploit these tests guard
    against is specifically about what the real verification path attaches.

    Parameters
    ----------
    task_tokens
        The service to verify against.
    minted
        The `MintedTaskToken` `TaskTokenService.mint` returned.

    Returns
    -------
    PrincipalRef
        The verified principal, with `task_scope` set.
    """
    parsed = parse_token(minted.token, "lakana_task")
    assert parsed is not None
    key_id, secret = parsed
    return task_tokens.verify(key_id, secret).principal


def test_task_token_scope_applies_in_the_worker(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A task token's declared asset scope narrows worker reads (exploit regression).

    `principal` holds a real `READ_TABLE` grant on the whole `raw` namespace,
    but the task token it queries with only declares `sandbox.*`. Before
    this fix, `run_one` never carried `task_scope` into the `PrincipalRef`
    it authorizes each table against, so `GrantAuthorization` never saw the
    token's narrower declared scope and the query would have run against
    `raw.customers` on the strength of the principal's own wide grant
    alone. `GrantAuthorization` — not `AllowAllAuthorization` — is used as
    `run_one`'s authz here, since the fix is specifically about what reaches
    a real check.

    Positive control: the same principal, same grant, same table — but
    submitted via a PAT (no task scope at all) — succeeds. This is what
    proves the task-scoped run's denial above is attributable to the task
    token's own declared scope and not to some other fluke of the setup
    (a missing table, a missing grant, an unrelated storage error).
    """
    catalog = _local_catalog(session, principal, tmp_path)
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "customers").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1, 2]}), tmp_path / "raw" / "customers" / "part-0.parquet")
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="customers"),
        location_pattern="raw/customers/*.parquet",
        format="parquet",
    )
    GrantService(session, AllowAllAuthorization()).grant(
        principal,
        principal=principal.name,
        action=Action.READ_TABLE,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace="raw",
    )
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        principal, principal.name, environment="dev", asset_scope=["sandbox.*"], ttl_seconds=300
    )
    task_principal = _verify_minted_token(task_tokens, minted)

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(task_principal, "dev", "SELECT * FROM raw.customers")

    run_one(session, run.id, GrantAuthorization(session))

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "Resolving the tables this query reads failed (PermissionDenied)."
    assert run.result_location is None

    pat_run = query_service.submit_query(principal, "dev", "SELECT * FROM raw.customers")

    run_one(session, pat_run.id, GrantAuthorization(session))

    session.refresh(pat_run)
    assert pat_run.status == QueryRunStatus.SUCCEEDED, pat_run.error_message


def test_queued_run_fails_when_token_deleted(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A run submitted with a task token whose row has since been deleted fails closed.

    `task_token_id` deliberately has no foreign key (migration
    `0011`), specifically so a *deleted* row — not just a revoked one — is
    still detectable rather than silently read as "no task scope at all."
    """
    _local_catalog(session, principal, tmp_path)
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        principal, principal.name, environment="dev", asset_scope=["gold.*"], ttl_seconds=300
    )
    task_principal = _verify_minted_token(task_tokens, minted)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(task_principal, "dev", "SELECT 1")
    task_token_row = session.execute(
        select(TaskToken).where(TaskToken.key_id == minted.key_id)
    ).scalar_one()
    session.delete(task_token_row)
    session.commit()

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == _INVALID_TASK_TOKEN_MESSAGE
    assert run.result_location is None


def test_queued_run_fails_when_token_revoked(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A run submitted with a task token revoked before the worker picks it up fails closed.

    `task_token_id` has no foreign key (migration `0011`) so it
    stays set even once the token itself is gone or revoked; the worker
    must re-check the token's own validity, not just trust the id was once
    real.
    """
    _local_catalog(session, principal, tmp_path)
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        principal, principal.name, environment="dev", asset_scope=["gold.*"], ttl_seconds=300
    )
    task_principal = _verify_minted_token(task_tokens, minted)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(task_principal, "dev", "SELECT 1")
    task_tokens.revoke(principal, minted.key_id)

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == _INVALID_TASK_TOKEN_MESSAGE
    assert run.result_location is None


def test_queued_run_fails_when_token_principal_deactivated(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A run submitted with a task token whose principal was since deactivated fails closed."""
    _local_catalog(session, principal, tmp_path)
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        principal, principal.name, environment="dev", asset_scope=["gold.*"], ttl_seconds=300
    )
    task_principal = _verify_minted_token(task_tokens, minted)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(task_principal, "dev", "SELECT 1")
    principal_row = session.get(Principal, principal.id)
    assert principal_row is not None
    principal_row.is_active = False
    session.commit()

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == _INVALID_TASK_TOKEN_MESSAGE
    assert run.result_location is None


def test_queued_run_fails_when_token_expired(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A run submitted with a task token that has since expired fails closed."""
    _local_catalog(session, principal, tmp_path)
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        principal, principal.name, environment="dev", asset_scope=["gold.*"], ttl_seconds=300
    )
    task_principal = _verify_minted_token(task_tokens, minted)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(task_principal, "dev", "SELECT 1")
    task_token_row = session.execute(
        select(TaskToken).where(TaskToken.key_id == minted.key_id)
    ).scalar_one()
    task_token_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == _INVALID_TASK_TOKEN_MESSAGE
    assert run.result_location is None


def test_queued_run_fails_when_tokens_principal_no_longer_matches_the_runs(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A task token whose stored principal no longer matches the run's own fails closed.

    `QueryRun.task_token_id` has no foreign key (migration `0011`),
    so nothing in the schema stops a `TaskToken` row's `principal_id` from
    later pointing at a different principal than the one the run was
    actually submitted under (a direct mutation, or a future bug); the
    worker must re-check this itself rather than trust the stored id still
    means what it meant at submission time.
    """
    _local_catalog(session, principal, tmp_path)
    other = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="other")
    session.add(other)
    session.commit()
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        principal, principal.name, environment="dev", asset_scope=["gold.*"], ttl_seconds=300
    )
    task_principal = _verify_minted_token(task_tokens, minted)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(task_principal, "dev", "SELECT 1")
    task_token_row = session.execute(
        select(TaskToken).where(TaskToken.key_id == minted.key_id)
    ).scalar_one()
    task_token_row.principal_id = other.id
    session.commit()

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == _INVALID_TASK_TOKEN_MESSAGE
    assert run.result_location is None


def test_invalid_task_token_records_exactly_one_query_completed_event(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The invalid-task-token branch writes exactly one QUERY_COMPLETED row, atomically.

    Regression for a fix-round-1 finding: the branch used to `session.commit()`
    the run's `FAILED` status and then record `QUERY_COMPLETED` in a second,
    separate transaction (`AuditService.record(..., commit=True)`), which
    contradicted the documented "audit written in the same transaction as
    the action it records" invariant. It now records first and commits once.
    """
    _local_catalog(session, principal, tmp_path)
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        principal, principal.name, environment="dev", asset_scope=["gold.*"], ttl_seconds=300
    )
    task_principal = _verify_minted_token(task_tokens, minted)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(task_principal, "dev", "SELECT 1")
    task_tokens.revoke(principal, minted.key_id)

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.QUERY_COMPLETED).all()
    assert len(events) == 1
    assert events[0].detail["status"] == "FAILED"
    assert events[0].detail["row_count"] is None


def test_invalid_task_token_is_atomic_when_the_audit_write_fails(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """If recording the audit event raises, nothing the operation did is committed.

    The test above this one cannot actually fail against the pre-fix code:
    `record` runs uninstrumented there, so a version of the invalid-task-token
    branch that `session.commit()`ed the run's `FAILED` status and *then*
    recorded `QUERY_COMPLETED` in a second, separate transaction (the
    fix-round-1 bug this module's docstring describes) still leaves exactly
    one `QUERY_COMPLETED` row and a `FAILED` run either way — both
    transactions simply succeed. Forcing `AuditService.record` to raise is
    what actually distinguishes the two: this branch mutates
    `run.status`/`error_message`/`finished_at` on the session and calls
    `record` *before* its one `session.commit()`, so if `record` never
    returns, that commit is never reached and `session.rollback()` leaves
    the run exactly as `submit_query` queued it. The old, two-commit code
    would instead have already committed the `FAILED` status by the time
    `record` raised, so this test would fail against it.
    """
    _local_catalog(session, principal, tmp_path)
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        principal, principal.name, environment="dev", asset_scope=["gold.*"], ttl_seconds=300
    )
    task_principal = _verify_minted_token(task_tokens, minted)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(task_principal, "dev", "SELECT 1")
    task_tokens.revoke(principal, minted.key_id)

    with (
        mock.patch.object(AuditService, "record", side_effect=RuntimeError("audit down")),
        pytest.raises(RuntimeError, match="audit down"),
    ):
        run_one(session, run.id, AllowAllAuthorization())

    session.rollback()
    session.refresh(run)
    assert run.status == QueryRunStatus.QUEUED
    assert run.error_message is None
    assert run.finished_at is None
    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.QUERY_COMPLETED).all()
    assert events == []


def test_run_one_reads_a_table_with_no_snapshot_as_zero_rows(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A freshly created table has no data files, and `read_parquet([])` is an error.

    Zero rows is the right answer, so the CTE becomes a typed empty
    relation shaped like the table's declared schema instead.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    catalog.create_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales"),
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "region", StringType(), required=False),
        ),
    )

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id, region FROM gold.sales")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    result = _read_result(tmp_path, run)
    assert result.num_rows == 0
    assert result.column_names == ["id", "region"]
    assert result.schema.field("id").type == pa.int64()


def test_run_one_executes_a_join_of_two_aliased_tables_end_to_end(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The milestone's own exit-criterion query shape: two tables, both aliased.

    Replacing a table node used to drop its alias, so `o.id` no longer
    resolved and the whole query failed.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    orders_ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="orders"
    )
    orders = catalog.create_table(
        principal,
        orders_ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "customer_id", LongType(), required=True),
        ),
    )
    _write_one_data_file_and_commit(
        catalog,
        principal,
        orders_ref,
        orders,
        pa.table(
            {"id": [1, 2], "customer_id": [10, 11]},
            schema=pa.schema(
                [
                    pa.field("id", pa.int64(), nullable=False),
                    pa.field("customer_id", pa.int64(), nullable=False),
                ]
            ),
        ),
    )
    customers_ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
    )
    customers = catalog.create_table(
        principal,
        customers_ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "name", StringType(), required=False),
        ),
    )
    _write_one_data_file_and_commit(
        catalog,
        principal,
        customers_ref,
        customers,
        pa.table(
            {"id": [10, 11], "name": ["ada", "grace"]},
            schema=pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("name", pa.string())]
            ),
        ),
    )

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal,
        "dev",
        "SELECT o.id, c.name FROM gold.orders AS o "
        "JOIN gold.customers AS c ON o.customer_id = c.id",
    )

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    result = _read_result(tmp_path, run)
    assert sorted(
        zip(result.column("id").to_pylist(), result.column("name").to_pylist(), strict=True)
    ) == [
        (1, "ada"),
        (2, "grace"),
    ]


def test_run_one_applies_a_column_mask_to_the_query_result(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A column mask on a referenced table replaces that column's real value end to end."""
    catalog = _local_catalog(session, principal, tmp_path)
    ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
    )
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "ssn", StringType(), required=False),
        ),
    )
    _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table(
            {"id": [1, 2], "ssn": ["111-11-1111", "222-22-2222"]},
            schema=pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("ssn", pa.string())]
            ),
        ),
    )
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="customers",
        column="ssn",
        expression="NULL",
    )

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id, ssn FROM gold.customers")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    result = _read_result(tmp_path, run)
    assert result.column("ssn").to_pylist() == [None, None]
    assert result.column("id").to_pylist() == [1, 2]


def test_run_one_applies_a_row_filter_to_the_query_result(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A row filter on a referenced table excludes non-matching rows end to end."""
    catalog = _local_catalog(session, principal, tmp_path)
    ref = TableRef(
        environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="customers"
    )
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "region", StringType(), required=False),
        ),
    )
    _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table(
            {"id": [1, 2, 3], "region": ["EU", "US", "EU"]},
            schema=pa.schema(
                [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
            ),
        ),
    )
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="customers",
        expression="region = 'EU'",
    )

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id, region FROM gold.customers")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    result = _read_result(tmp_path, run)
    assert sorted(result.column("id").to_pylist()) == [1, 3]
    assert set(result.column("region").to_pylist()) == {"EU"}


def test_run_one_reads_a_secret_backed_environment_and_never_leaks_it_on_write_failure(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A decrypted secret value must never leak into `error_message` if the write fails.

    Uses a real local (`file`) environment with a real secret attached via
    `storage_secret_ref`, so `resolved_storage.config.options` in `run_one`
    genuinely contains the decrypted value (real crypto, real
    `EnvKeyProvider`, real `StorageResolver.for_environment` call) -- only
    the final `Storage.write` call is patched to fail, standing in for a
    real backend (S3, credential rejected, endpoint unreachable) echoing a
    credential back in its exception text. `update_environment_storage`
    only ever changes `storage_secret_ref`/`storage_options`, never
    `storage_root` (there is no API to promote a `file` environment to
    `s3` after creation), so the read side and the write side always
    resolve the *same* `file`-scheme storage in this test: read genuinely
    succeeds against local disk, and `fsspec`'s local filesystem silently
    ignores unrecognised S3-shaped kwargs rather than erroring on them, so
    the write cannot be made to fail for real without a live S3-compatible
    backend. Patching `Storage.write` is what forces a failure at exactly
    the point this task's fix instruments, deterministically and without
    Docker, while every value up to that point -- including the secret
    ending up in `resolved_storage.config.options` -- is produced by the
    real code path.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal, ref, schema=Schema(NestedField(1, "id", LongType(), required=True))
    )
    _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table({"id": [1]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)])),
    )
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    SecretService(session, AllowAllAuthorization(), provider).create(
        principal,
        environment="dev",
        name="bogus-creds",
        values={"secret_access_key": "MARKER_VALUE_MUST_NOT_LEAK"},
    )
    catalog.update_environment_storage(principal, "dev", secret_ref="bogus-creds")

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id FROM gold.sales")

    def _failing_write(self: FsspecStorage, path: str, data: bytes) -> None:
        raise RuntimeError("Write failed: secret_access_key=MARKER_VALUE_MUST_NOT_LEAK")

    with mock.patch.object(FsspecStorage, "write", _failing_write):
        run_one(session, run.id, AllowAllAuthorization(), provider)

    session.refresh(run)
    assert run.error_message is not None
    assert "Write failed:" in run.error_message  # the patched write was actually reached
    assert _REDACTED in run.error_message  # and redaction is what removed the value
    assert "MARKER_VALUE_MUST_NOT_LEAK" not in run.error_message


def test_run_one_reads_a_secret_backed_environment_and_never_leaks_it_on_query_execution_failure(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A decrypted secret value must never leak into `error_message` if execution fails.

    Same secret-backed environment as the write-failure test above, but here the
    failure is forced during `execute_query` itself -- the step that, for a real
    S3-backed environment, is what actually generates the presigned URLs DuckDB
    binds against (URLs that embed `access_key_id` verbatim). Before this task's
    fix, the decrypted secret was only added to the scrub-list after
    `rewrite_query`/`execute_query` had already run (as part of the write-side
    `StorageResolver.for_environment` call), so a failure here -- before the
    write step is ever reached -- had nothing in `locations` to redact against.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal, ref, schema=Schema(NestedField(1, "id", LongType(), required=True))
    )
    _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table({"id": [1]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)])),
    )
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    SecretService(session, AllowAllAuthorization(), provider).create(
        principal,
        environment="dev",
        name="bogus-creds",
        values={"secret_access_key": "MARKER_VALUE_MUST_NOT_LEAK"},
    )
    catalog.update_environment_storage(principal, "dev", secret_ref="bogus-creds")

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id FROM gold.sales")

    def _failing_execute(*args: object, **kwargs: object) -> pa.Table:
        raise RuntimeError("DuckDB error: secret_access_key=MARKER_VALUE_MUST_NOT_LEAK")

    with mock.patch("lakana_worker.pipeline.execute_query", _failing_execute):
        run_one(session, run.id, AllowAllAuthorization(), provider)

    session.refresh(run)
    assert run.error_message is not None
    assert "DuckDB error:" in run.error_message  # the patched execute_query was actually reached
    assert _REDACTED in run.error_message  # and redaction is what removed the value
    assert "MARKER_VALUE_MUST_NOT_LEAK" not in run.error_message


def test_run_one_does_not_scrub_short_option_values_from_the_error_message(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A short option value (an empty/short `session_token`, say) must not enter the scrub-list.

    `_redact` does a blind `str.replace` for every entry in the scrub-list built
    from `resolved_storage.config.options.values()`; an unfiltered short value
    would match constantly through an unrelated error message and destroy it.
    Only sufficiently long values -- real access keys and secrets always are --
    get scrubbed.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal, ref, schema=Schema(NestedField(1, "id", LongType(), required=True))
    )
    _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table({"id": [1]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)])),
    )
    provider = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
    SecretService(session, AllowAllAuthorization(), provider).create(
        principal, environment="dev", name="bogus-creds", values={"session_token": "ab"}
    )
    catalog.update_environment_storage(principal, "dev", secret_ref="bogus-creds")

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id FROM gold.sales")

    def _failing_execute(*args: object, **kwargs: object) -> pa.Table:
        raise RuntimeError("Query failed: table gold.sales has an ab-normal schema")

    with mock.patch("lakana_worker.pipeline.execute_query", _failing_execute):
        run_one(session, run.id, AllowAllAuthorization(), provider)

    session.refresh(run)
    assert run.error_message is not None
    # If the short "ab" session_token had been added to the scrub-list, this
    # ordinary occurrence of "ab" inside "ab-normal" would have been destroyed too.
    assert "ab-normal" in run.error_message


def _register_raw_table(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    *,
    name: str,
    file_name: str,
) -> None:
    """Write one raw-zone parquet file and register an EXTERNAL table over it.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    tmp_path
        The environment's storage root.
    name
        The table name inside the `raw` namespace, also its directory.
    file_name
        The object key to write the file under.
    """
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / name).mkdir(parents=True)
    pq.write_table(pa.table({"id": [1, 2]}), tmp_path / "raw" / name / file_name)
    catalog.create_external_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name=name),
        location_pattern=f"raw/{name}/*.parquet",
        format="parquet",
    )


def test_run_one_executes_a_query_that_defines_its_own_cte(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A `WITH` name is not a table reference; treating it as one failed the query outright."""
    catalog = _local_catalog(session, principal, tmp_path)
    _register_raw_table(catalog, principal, tmp_path, name="events", file_name="part-0.parquet")

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal, "dev", "WITH recent AS (SELECT id FROM raw.events) SELECT id FROM recent"
    )

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _read_result(tmp_path, run).column("id").to_pylist() == [1, 2]


class _RecordingAuthorization:
    """Allow-all authorization that records every check, to prove one was made.

    A skipped table reference is invisible in the run's *outcome* when the
    policy allows everything, which is exactly how the scope-blind exclusion
    hid an unauthorized read. Recording the calls asserts the check happened
    rather than that it passed.

    Parameters
    ----------
    calls
        Every `(action, securable)` pair checked so far, in order.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Action, Securable]] = []

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Record the check and permit it.

        Parameters
        ----------
        principal
            The acting principal.
        action
            The action requested.
        securable
            The object acted upon.
        """
        self.calls.append((action, securable))

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, as a boolean.

        Parameters
        ----------
        principal
            The actor requesting the action.
        action
            The action requested.
        securable
            The object acted upon.

        Returns
        -------
        bool
            `False` if `authorize` raises `PermissionDenied`, else `True`.
        """
        try:
            self.authorize(principal, action, securable)
        except PermissionDenied:
            return False
        return True


def test_run_one_authorizes_a_real_table_shadowed_by_an_inner_cte(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An inner `WITH` name must not hide the outer read of the real table of that name.

    Collecting every CTE name anywhere in the statement dropped the outer
    reference too, so no `READ_TABLE` check was ever made for it while
    DuckDB — which resolves scopes correctly — still read it. The check is
    that authorization is consulted, not merely that the run succeeds.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    _register_raw_table(catalog, principal, tmp_path, name="events", file_name="part-0.parquet")
    authz = _RecordingAuthorization()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal,
        "dev",
        "SELECT id FROM raw.events "
        'WHERE id IN (WITH "raw.events" AS (SELECT 1 AS id) SELECT id FROM "raw.events")',
    )

    run_one(session, run.id, authz)

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _read_result(tmp_path, run).column("id").to_pylist() == [1]
    assert (
        Action.READ_TABLE,
        Securable.table_scope("dev", NamespacePath.from_parts(["raw"]), "events"),
    ) in authz.calls


def test_run_one_refuses_a_url_read_shadowed_by_a_same_named_cte(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The exploit: a quoted URL DuckDB fetches over HTTP, hidden behind a same-named CTE.

    DuckDB reads `FROM "http://…/x.csv"` as a real HTTP request, so dropping
    the reference was worker-side SSRF with no authorization call at all and
    no credential needed. It must now be reported as a table reference and
    resolved like any other, which refuses it: a URL is not a catalog name,
    and `NamespacePath` rejects the `/` in it before any read is attempted.
    """
    _local_catalog(session, principal, tmp_path)
    authz = _RecordingAuthorization()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal,
        "dev",
        'SELECT * FROM "http://evil.invalid/x.csv" WHERE 1 IN ('
        'WITH "http://evil.invalid/x.csv" AS (SELECT 1 AS z) '
        'SELECT z FROM "http://evil.invalid/x.csv")',
    )

    run_one(session, run.id, authz)

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "Resolving the tables this query reads failed (ValidationError)."
    assert run.result_location is None
    assert authz.calls == []


def test_run_one_authorizes_a_table_shadowed_by_a_derived_table_alias(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A derived table's alias must not hide a real read of that name beside it.

    `scope.sources` maps `(SELECT ...) AS x` to a `Scope` exactly as it maps a
    `WITH` binding, so excluding every scope-valued source dropped the second,
    bare reference here and made no `READ_TABLE` check for it while DuckDB
    still read the table. The check is that authorization is consulted.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    _register_raw_table(catalog, principal, tmp_path, name="events", file_name="part-0.parquet")
    authz = _RecordingAuthorization()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal,
        "dev",
        'SELECT id FROM (SELECT 1 AS z) AS "raw.events", "raw.events"',
    )

    run_one(session, run.id, authz)

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _read_result(tmp_path, run).column("id").to_pylist() == [1, 2]
    assert (
        Action.READ_TABLE,
        Securable.table_scope("dev", NamespacePath.from_parts(["raw"]), "events"),
    ) in authz.calls


def test_run_one_refuses_a_url_read_shadowed_by_a_derived_table_alias(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The proven exploit: a URL DuckDB fetches, hidden behind a same-named derived table.

    The bare second `FROM` entry is a real HTTP read, but a derived table in
    the same `FROM` clause aliased to that URL made `scope.sources` map the
    name to a `Scope`, so the reference was dropped: the run executed and
    DuckDB issued the request with no authorization call at all. It is now
    reported as a table reference and refused — `NamespacePath` rejects the
    `/` in it — before any read or any check is attempted.
    """
    _local_catalog(session, principal, tmp_path)
    authz = _RecordingAuthorization()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal,
        "dev",
        'SELECT * FROM (SELECT 1 AS z) AS "http://evil.invalid/x.csv", "http://evil.invalid/x.csv"',
    )

    run_one(session, run.id, authz)

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "Resolving the tables this query reads failed (ValidationError)."
    assert run.result_location is None
    assert authz.calls == []


def test_run_one_refuses_a_file_read_aliased_to_a_cte_name(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The proven exploit: a real file read *aliased* to an unrelated `WITH` name.

    Classification asked `scope.sources` for the reference's `alias_or_name`,
    so aliasing a genuine read to a CTE's name returned that CTE's `Scope` and
    dropped the read from the reported references: DuckDB still read the file
    and returned its rows, with no authorization call made at all. The file
    here is a real one on disk, so a run that succeeds is a real unauthorized
    read, not a hypothetical one.
    """
    _local_catalog(session, principal, tmp_path)
    secret = tmp_path / "secret.csv"
    secret.write_text("val\n4711\n")
    authz = _RecordingAuthorization()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal,
        "dev",
        f'WITH c AS (SELECT 1 AS id) SELECT * FROM (SELECT val FROM c, "{secret}" AS c) AS w',
    )

    run_one(session, run.id, authz)

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.result_location is None
    assert authz.calls == []


def test_run_one_authorizes_a_table_aliased_to_a_cte_name(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A borrowed alias is not an ambiguity, and the real read behind it is authorized.

    The alias shadows the `WITH` binding inside the derived table, which is
    how DuckDB resolves it too, so this query is classified rather than
    refused — and the aliased reference is a catalog table whose
    `READ_TABLE` check must be made.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    _register_raw_table(catalog, principal, tmp_path, name="events", file_name="part-0.parquet")
    authz = _RecordingAuthorization()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal,
        "dev",
        "WITH c AS (SELECT 9 AS id) SELECT w.id FROM (SELECT id FROM raw.events AS c) AS w",
    )

    run_one(session, run.id, authz)

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _read_result(tmp_path, run).column("id").to_pylist() == [1, 2]
    assert (
        Action.READ_TABLE,
        Securable.table_scope("dev", NamespacePath.from_parts(["raw"]), "events"),
    ) in authz.calls


def test_run_one_refuses_a_recursive_ctes_base_term_file_read(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The proven exploit: a real file read hidden in a `WITH RECURSIVE` base term.

    sqlglot pre-seeds the recursive CTE's own name into `Scope.cte_sources`
    before walking its body, and a `UNION ALL` body inherits that binding
    into *both* branches. DuckDB binds it only in the recursive term; in the
    base term the quoted path is still a real file it reads by replacement
    scan. So `extract_table_refs` reported nothing, no authorization call was
    made, and the file's rows came back. The file here is a real one on disk,
    so a run that succeeds is an actual unauthorized read.

    `WITH RECURSIVE` is now rejected structurally by `check_blacklist`, which
    `run_one` calls before `extract_table_refs`, `rewrite_query`, or the
    executor — so the query dies at the gate rather than being classified.
    """
    _local_catalog(session, principal, tmp_path)
    secret = tmp_path / "secret.csv"
    secret.write_text("val\n4711\n")
    authz = _RecordingAuthorization()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal,
        "dev",
        f'WITH RECURSIVE "{secret}" AS ('
        f'SELECT val FROM "{secret}" '
        f'UNION ALL SELECT val FROM "{secret}" WHERE FALSE'
        f') SELECT * FROM "{secret}"',
    )

    run_one(session, run.id, authz)

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert "Recursive CTEs" in run.error_message
    assert "4711" not in run.error_message
    assert run.result_location is None
    assert authz.calls == []


def test_run_one_refuses_an_innocent_recursive_cte(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A recursive CTE touching nothing external is refused too, and says why.

    The rejection is of the feature, not of a malicious use of it, so the
    error a legitimate user gets has to name the unsupported construct rather
    than leave them guessing.
    """
    _local_catalog(session, principal, tmp_path)

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal,
        "dev",
        "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
        "SELECT n FROM r",
    )

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == (
        "Recursive CTEs (WITH RECURSIVE) are not supported in submitted queries."
    )
    assert run.result_location is None


def test_run_one_refuses_a_connect_by_query_and_says_what_to_write(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A hierarchical query is refused before it runs, with the cause and the next step.

    sqlglot would hand DuckDB a `CONNECT BY` as a `WITH RECURSIVE` query, which the guard
    refuses when written directly, so it refuses this spelling too.
    """
    _local_catalog(session, principal, tmp_path)

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal,
        "dev",
        "SELECT a FROM range(3) AS t(a) START WITH a = 0 CONNECT BY PRIOR a = a - 1",
    )

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == (
        "CONNECT BY is not supported in submitted queries: it runs as a recursive query. "
        "Write the query without hierarchical recursion, for example with one join per level."
    )
    assert run.result_location is None


def test_run_one_executes_an_aggregate_whose_column_has_no_lineage(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`SELECT COUNT(*)` used to fail the whole run when lineage could not trace it."""
    catalog = _local_catalog(session, principal, tmp_path)
    _register_raw_table(catalog, principal, tmp_path, name="events", file_name="part-0.parquet")

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT COUNT(*) FROM raw.events")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _read_result(tmp_path, run).column(0).to_pylist() == [2]
    assert run.lineage_json == {"*": []}


def test_run_one_executes_a_computed_projection_beside_a_traced_one(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A computed column records empty lineage without costing its sibling its own."""
    catalog = _local_catalog(session, principal, tmp_path)
    _register_raw_table(catalog, principal, tmp_path, name="events", file_name="part-0.parquet")

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id, id * 2 FROM raw.events")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _read_result(tmp_path, run).column(1).to_pylist() == [2, 4]
    assert run.lineage_json == {"id": [{"table": "raw.events", "column": "id"}], "": []}


def test_run_one_fails_a_query_that_reads_files_directly(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The blacklist, not a lineage crash, is what stops a direct file read.

    Tolerant lineage extraction removed the accidental protection an
    exception used to give these, so the pipeline must reject them on the
    blacklist's own terms.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    _register_raw_table(catalog, principal, tmp_path, name="events", file_name="part-0.parquet")
    secret = tmp_path / "secret.parquet"
    pq.write_table(pa.table({"id": [99]}), secret)

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", f"SELECT * FROM parquet_scan('{secret}')")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert "parquet_scan" in run.error_message
    assert run.result_location is None


def test_run_one_fails_a_multi_statement_payload(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A `;`-separated second statement is refused, not silently executed."""
    catalog = _local_catalog(session, principal, tmp_path)
    _register_raw_table(catalog, principal, tmp_path, name="events", file_name="part-0.parquet")

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(
        principal, "dev", "SELECT id FROM raw.events; SET s3_access_key_id = 'AKIA'"
    )

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert "single query" in run.error_message


def test_run_one_reads_a_file_whose_object_key_contains_a_quote(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A quote in a real object key is data, not syntax — it used to break the SQL outright."""
    catalog = _local_catalog(session, principal, tmp_path)
    _register_raw_table(
        catalog,
        principal,
        tmp_path,
        name="quoted",
        file_name="part-0') UNION ALL SELECT 42 -- .parquet",
    )

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id FROM raw.quoted")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message
    assert _read_result(tmp_path, run).column("id").to_pylist() == [1, 2]


def test_run_one_keeps_resolved_locations_out_of_the_error_message(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """DuckDB echoes the bound file list, which in team mode is a signed URL.

    `error_message` is read back by whoever submitted the query, so a
    presigned URL there is a credential handed out in a status field.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal, ref, schema=Schema(NestedField(1, "id", LongType(), required=True))
    )
    _write_one_data_file_and_commit(
        catalog,
        principal,
        ref,
        created,
        pa.table({"id": [1]}, schema=pa.schema([pa.field("id", pa.int64(), nullable=False)])),
    )
    for data_file in (tmp_path / "managed").rglob("*.parquet"):
        data_file.unlink()

    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id FROM gold.sales")

    run_one(session, run.id, AllowAllAuthorization())

    session.refresh(run)
    assert run.status == QueryRunStatus.FAILED
    assert run.error_message is not None
    assert str(tmp_path) not in run.error_message
    # The read is mapped by field id, so the child, not the redaction here, takes the
    # location out of the message.
    assert "<file>" in run.error_message


def test_run_one_allows_no_spill_and_no_location_when_storage_is_not_local(
    session: Session, principal: PrincipalRef
) -> None:
    """Cloud storage: no spill to the worker's disk, and a query of no table opens nothing."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="s3", root="s3://bucket/data", options={}),
    )
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT 1 AS x")

    with (
        mock.patch(
            "lakana_worker.pipeline.execute_query", return_value=pa.table({"x": [1]})
        ) as execute_mock,
        mock.patch.object(FsspecStorage, "write"),
    ):
        run_one(session, run.id, AllowAllAuthorization())

    assert execute_mock.call_args.kwargs["spill"] is False
    assert execute_mock.call_args.kwargs["read_prefixes"] == ()
    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message


def test_run_one_allows_spill_when_storage_is_local(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Local mode spills to the worker's disk, as it always has."""
    _local_catalog(session, principal, tmp_path)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT 1 AS x")

    with mock.patch(
        "lakana_worker.pipeline.execute_query", return_value=pa.table({"x": [1]})
    ) as execute_mock:
        run_one(session, run.id, AllowAllAuthorization())

    assert execute_mock.call_args.kwargs["spill"] is True
    session.refresh(run)
    assert run.status == QueryRunStatus.SUCCEEDED, run.error_message


def test_run_one_records_a_query_completed_event_on_success(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A successful run records QUERY_COMPLETED with sql_text, tables touched, and row_count.

    Setup copied from this file's own `test_run_one_executes_a_single_table_
    query_end_to_end` (a real environment, table, and one data file), not
    reinvented, since that test already proves the exact SUCCEEDED shape
    this one adds an audit-event assertion to.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="sales")
    created = catalog.create_table(
        principal,
        ref,
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "region", StringType(), required=False),
        ),
    )
    arrow_schema = pa.schema(
        [pa.field("id", pa.int64(), nullable=False), pa.field("region", pa.string())]
    )
    data = pa.table({"id": [1, 2], "region": ["eu", "us"]}, schema=arrow_schema)
    _write_one_data_file_and_commit(catalog, principal, ref, created, data)
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT id, region FROM gold.sales")

    run_one(session, run.id, AllowAllAuthorization())

    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.QUERY_COMPLETED).all()
    assert len(events) == 1
    assert events[0].principal_id == principal.id
    assert events[0].securable_environment == "dev"
    assert events[0].detail["status"] == "SUCCEEDED"
    assert events[0].detail["row_count"] == 2
    assert events[0].detail["tables_touched"] == ["gold.sales"]


def test_run_one_records_a_query_completed_event_on_failure_with_no_row_count(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A failed run still records QUERY_COMPLETED, with row_count None.

    Setup copied from this file's own `test_run_one_fails_a_query_against_
    an_unauthorized_table` (a `_DenyRead` authorization stub forces a
    per-table failure without needing a table that doesn't exist).
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    query_service = QueryService(session, AllowAllAuthorization())
    run = query_service.submit_query(principal, "dev", "SELECT * FROM gold.nonexistent")

    class _DenyRead:
        """Authorization stub that refuses every check, to force a per-table failure."""

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Deny unconditionally.

            Parameters
            ----------
            principal
                The acting principal.
            action
                The action requested.
            securable
                The object acted upon.

            Raises
            ------
            PermissionDenied
                Always.
            """
            raise PermissionDenied("denied")

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize` would, as a boolean.

            Parameters
            ----------
            principal
                The actor requesting the action.
            action
                The action requested.
            securable
                The object acted upon.

            Returns
            -------
            bool
                `False` if `authorize` raises `PermissionDenied`, else `True`.
            """
            try:
                self.authorize(principal, action, securable)
            except PermissionDenied:
                return False
            return True

    run_one(session, run.id, _DenyRead())

    events = session.query(AuditEvent).filter_by(event_type=AuditEventType.QUERY_COMPLETED).all()
    assert len(events) == 1
    assert events[0].detail["status"] == "FAILED"
    assert events[0].detail["row_count"] is None
    assert events[0].detail["tables_touched"] == ["gold.nonexistent"]


def _unpublished_orders(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> tuple[CatalogService, uuid.UUID]:
    """Create `gold.orders` with two rows and mark it as not published.

    The table keeps its snapshot and its files: a refusal must not depend on the snapshot
    being gone.

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
    tuple
        The allow-all catalog and the run id the table is marked with.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    data = pa.table({"id": pa.array([1, 2], pa.int64())})
    catalog.write_batches(
        principal,
        TableRef.from_dotted("dev", "gold.orders"),
        mode=WriteMode.CREATE,
        schema=data.schema,
        batches=data.to_batches(),
    )
    run_id = uuid.uuid4()
    session.execute(update(Table).where(Table.name == "orders").values(unpublished_run_id=run_id))
    session.commit()
    return catalog, run_id


def _not_published_text(run_id: uuid.UUID) -> str:
    """Build the text of `gold.orders` when no result row of its run is left.

    Parameters
    ----------
    run_id
        The run.

    Returns
    -------
    str
        The text.
    """
    return (
        "`gold.orders` has not been published: its first build did not finish, "
        f"or failed a check (run {run_id}). It is published by the next run that builds it "
        "and passes its checks."
    )


def _grant_read_on(session: Session, admin: PrincipalRef, name: str, table: str) -> None:
    """Grant `name` READ_TABLE on one table or view of `gold`.

    Parameters
    ----------
    session
        Database session.
    admin
        The granting principal.
    name
        The grantee's name.
    table
        The table or view, in `gold`.
    """
    GrantService(session, AllowAllAuthorization()).grant(
        admin,
        principal=name,
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace="gold",
        table=table,
    )


def _new_principal(session: Session, name: str) -> PrincipalRef:
    """Persist a principal.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.

    Returns
    -------
    PrincipalRef
        A reference to it.
    """
    principal_id = uuid.uuid4()
    session.add(Principal(id=principal_id, type=PrincipalType.USER, name=name))
    session.commit()
    return PrincipalRef(id=principal_id, name=name, type=PrincipalType.USER)


def _run_as(
    session: Session, principal: PrincipalRef, sql: str, monkeypatch: pytest.MonkeyPatch
) -> QueryRun:
    """Run a query under real grants with an executor that must never be asked for a statement.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    sql
        The query.
    monkeypatch
        Replaces the executor.

    Returns
    -------
    QueryRun
        The finished run.
    """
    run = QueryService(session, AllowAllAuthorization()).submit_query(principal, "dev", sql)
    monkeypatch.setattr(
        "lakana_worker.pipeline.execute_query",
        lambda *args, **kwargs: pytest.fail("a statement reached the executor"),
    )
    run_one(session, run.id, GrantAuthorization(session))
    session.refresh(run)
    return run


def test_every_reader_of_load_table_refuses_a_table_that_is_not_published(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The query fails with the reason, and reads no row."""
    _, run_id = _unpublished_orders(session, principal, tmp_path)
    reader = _new_principal(session, "reader")
    _grant_read_on(session, principal, "reader", "orders")

    run = _run_as(session, reader, "SELECT count(*) FROM gold.orders", monkeypatch)

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == _not_published_text(run_id)
    assert run.error_cause == "not_found"
    assert run.result_location is None
    assert not (tmp_path / "_artifacts" / "queries").exists()


def test_a_view_over_a_table_that_is_not_published_is_refused_with_the_same_text(
    session: Session, principal: PrincipalRef, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, run_id = _unpublished_orders(session, principal, tmp_path)
    catalog.create_or_replace_view(
        principal,
        TableRef.from_dotted("dev", "gold.v"),
        sql="SELECT id FROM gold.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["gold.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    reader = _new_principal(session, "reader")
    _grant_read_on(session, principal, "reader", "orders")
    _grant_read_on(session, principal, "reader", "v")

    run = _run_as(session, reader, "SELECT count(*) FROM gold.v", monkeypatch)

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == _not_published_text(run_id)
    assert run.result_location is None


@pytest.mark.parametrize("sql", ["SELECT count(*) FROM gold.orders", "SELECT count(*) FROM gold.v"])
def test_a_principal_without_the_read_grant_learns_nothing_about_publication(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
) -> None:
    catalog, _ = _unpublished_orders(session, principal, tmp_path)
    catalog.create_or_replace_view(
        principal,
        TableRef.from_dotted("dev", "gold.v"),
        sql="SELECT id FROM gold.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["gold.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    stranger = _new_principal(session, "stranger")
    _grant_read_on(session, principal, "stranger", "v")

    run = _run_as(session, stranger, sql, monkeypatch)

    assert run.status == QueryRunStatus.FAILED
    assert run.error_message == "Resolving the tables this query reads failed (PermissionDenied)."
    assert run.error_cause == "permission_denied"


def _orders(session: Session, principal: PrincipalRef, tmp_path: Path) -> int:
    """Create `gold.orders` with two rows in a new `dev` environment.

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
    int
        The table's current snapshot.
    """
    catalog = _local_catalog(session, principal, tmp_path)
    data = pa.table({"id": pa.array([1, 2], pa.int64())})
    written = catalog.write_batches(
        principal,
        TableRef.from_dotted("dev", "gold.orders"),
        mode=WriteMode.CREATE,
        schema=data.schema,
        batches=data.to_batches(),
    )
    assert written.snapshot_id is not None
    return written.snapshot_id


def _stored_results(tmp_path: Path) -> list[Path]:
    """List the result files under the environment's storage root.

    Parameters
    ----------
    tmp_path
        The environment's storage root.

    Returns
    -------
    list of Path
        Every file under ``_artifacts/queries``.
    """
    return [path for path in (tmp_path / "_artifacts" / "queries").rglob("*") if path.is_file()]


def test_a_read_with_a_callback_hands_over_its_result_and_stores_none(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A count for a check comes back to the worker; no result file is written."""
    snapshot_id = _orders(session, principal, tmp_path)
    run = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", "SELECT count(*) AS n FROM gold.orders"
    )
    seen: list[ReadOutcome] = []

    failure = run_one(session, run.id, AllowAllAuthorization(), on_result=seen.append)

    assert failure is None
    (outcome,) = seen
    assert outcome.table.to_pydict() == {"n": [2]}
    assert outcome.snapshots == {"gold.orders": snapshot_id}
    assert outcome.restricted == frozenset()
    session.refresh(run)
    assert (run.status, run.result_location) == (QueryRunStatus.SUCCEEDED, None)
    assert _stored_results(tmp_path) == []
    (event,) = session.query(AuditEvent).filter_by(event_type=AuditEventType.QUERY_COMPLETED).all()
    assert (event.detail["row_count"], event.detail["status"]) == (1, "SUCCEEDED")


def test_a_read_without_a_callback_stores_its_result_as_before(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _orders(session, principal, tmp_path)
    run = QueryService(session, AllowAllAuthorization()).submit_query(
        principal, "dev", "SELECT count(*) AS n FROM gold.orders"
    )

    assert run_one(session, run.id, AllowAllAuthorization()) is None

    session.refresh(run)
    assert run.result_location == f"_artifacts/queries/{run.id}/result.arrow"
    assert _stored_results(tmp_path) == [tmp_path / run.result_location]
    assert _read_result(tmp_path, run).to_pydict() == {"n": [2]}


def test_a_read_with_a_callback_names_the_tables_a_policy_applied_to(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The base tables with a row filter or a column mask of the reader, and no others."""
    _orders(session, principal, tmp_path)
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    for name in ("filtered", "masked", "plain"):
        data = pa.table({"id": pa.array([1], pa.int64()), "note": pa.array(["x"], pa.string())})
        catalog.write_batches(
            principal,
            TableRef.from_dotted("dev", f"gold.{name}"),
            mode=WriteMode.CREATE,
            schema=data.schema,
            batches=data.to_batches(),
        )
    RowFilterService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="filtered",
        expression="id > 0",
    )
    ColumnMaskService(session, AllowAllAuthorization(), catalog).create(
        principal,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="masked",
        column="note",
        expression="NULL",
    )
    run = QueryService(session, AllowAllAuthorization()).submit_query(
        principal,
        "dev",
        "SELECT count(*) AS n FROM gold.filtered f, gold.masked m, gold.plain p, gold.orders o",
    )
    seen: list[ReadOutcome] = []

    assert run_one(session, run.id, AllowAllAuthorization(), on_result=seen.append) is None

    (outcome,) = seen
    assert outcome.restricted == frozenset({"gold.filtered", "gold.masked"})
    assert set(outcome.snapshots) == {"gold.filtered", "gold.masked", "gold.plain", "gold.orders"}


def test_a_write_ignores_the_callback(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    _orders(session, principal, tmp_path)
    run = QueryService(session, AllowAllAuthorization()).submit_write(
        principal,
        "dev",
        "SELECT id FROM gold.orders",
        WriteTarget(table="gold.copy", mode=WriteMode.CREATE),
    )

    failure = run_one(
        session,
        run.id,
        AllowAllAuthorization(),
        on_result=lambda outcome: pytest.fail("a write handed its result to the callback"),
    )

    assert failure is None
    session.refresh(run)
    assert (run.status, run.rows_written) == (QueryRunStatus.SUCCEEDED, 2)
