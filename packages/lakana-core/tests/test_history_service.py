"""`CatalogService.history`: authorize, create or diff, stage one snapshot, commit as merge does."""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pyarrow as pa
import pyiceberg.io.pyarrow as pyiceberg_arrow
import pytest
from core_test_helpers import ActionAllowList, _config, python_task_run
from pyiceberg.catalog.noop import NoopCatalog
from pyiceberg.expressions import EqualTo
from pyiceberg.manifest import DataFileContent
from pyiceberg.schema import Schema
from pyiceberg.table import DataScan, StaticTable
from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_merge import _bound_task

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, RunContext
from lakana_core.catalog import history as history_module
from lakana_core.catalog import service as service_module
from lakana_core.catalog.history import (
    EARLIEST_START,
    HISTORY_KEY_PROPERTY,
    HistoryResult,
    check_history_table,
    count_open_versions,
    read_last_ends,
    read_open_versions,
)
from lakana_core.catalog.merge import SDK_ADVICE, SQL_ADVICE, MergeAdvice
from lakana_core.catalog.service import CatalogService
from lakana_core.config import RunConfig
from lakana_core.errors import (
    ConflictError,
    PermissionDenied,
    UnsupportedTableError,
    ValidationError,
)
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEvent, AuditEventType, LineageEdge, Run, TaskRun
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.runs.lockdown import PROTECTED_MESSAGE
from lakana_core.storage.resolver import StorageResolver

T1 = datetime(2026, 2, 1, tzinfo=UTC)
T2 = datetime(2026, 3, 1, tzinfo=UTC)
T3 = datetime(2026, 4, 1, tzinfo=UTC)
DIM = "gold.dim_customer"

Row = tuple[str, str | None]


@dataclasses.dataclass
class Hist:
    """A file-backed environment `dev` (with bronze, silver and gold), and history table helpers.

    Parameters
    ----------
    service
        The catalog, allow-all.
    admin
        The acting principal.
    session
        The database session.
    root
        The environment's storage root on disk.
    """

    service: CatalogService
    admin: PrincipalRef
    session: Session
    root: Path

    def ref(self, dotted: str = DIM) -> TableRef:
        """Address a table of `dev`.

        Parameters
        ----------
        dotted
            ``namespace.table``.

        Returns
        -------
        TableRef
            The reference.
        """
        namespace, name = dotted.rsplit(".", 1)
        return TableRef(
            environment="dev", namespace=NamespacePath.from_parts(namespace.split(".")), name=name
        )

    @staticmethod
    def batch(rows: Sequence[Row]) -> pa.Table:
        """Build an asset's result: `customer_id`, `region`.

        Parameters
        ----------
        rows
            ``(customer_id, region)`` tuples.

        Returns
        -------
        pa.Table
            The rows.
        """
        return pa.table(
            {
                "customer_id": pa.array([r[0] for r in rows], pa.string()),
                "region": pa.array([r[1] for r in rows], pa.string()),
            }
        )

    def run(
        self, rows: Sequence[Row], clock: datetime, *, first: bool = False, **options: object
    ) -> HistoryResult:
        """Apply one history write at a fixed run clock.

        Parameters
        ----------
        rows
            The batch.
        clock
            The run clock.
        first
            Create a missing table (``expected_snapshot_id="none"``).
        **options
            More keywords for `CatalogService.history`.

        Returns
        -------
        HistoryResult
            What it committed.
        """
        with mock.patch.object(service_module, "run_clock", return_value=clock):
            return self.service.history(
                self.admin,
                self.ref(),
                self.batch(rows),
                key=["customer_id"],
                expected_snapshot_id="none" if first else None,
                **options,  # ty: ignore[invalid-argument-type]
            )

    def static(self, dotted: str = DIM) -> StaticTable:
        """Open the table's current metadata for reading.

        Parameters
        ----------
        dotted
            The table.

        Returns
        -------
        StaticTable
            The table.
        """
        loaded = self.service.load_table(self.admin, self.ref(dotted))
        return StaticTable(
            identifier=("dev", loaded.metadata_location),
            metadata=loaded.metadata,
            metadata_location=loaded.metadata_location,
            io=StorageResolver(self.session).for_environment("dev").file_io,
            catalog=NoopCatalog("dev"),
        )

    def versions(self) -> list[tuple[str, str | None, datetime, datetime | None, bool]]:
        """Read every version, ordered by key and start.

        Returns
        -------
        list of tuple
            ``(customer_id, region, valid_from, valid_to, is_current)``.
        """
        rows = self.static().scan().to_arrow().to_pylist()
        return sorted(
            (r["customer_id"], r["region"], r["valid_from"], r["valid_to"], r["is_current"])
            for r in rows
        )

    def events(self, kind: AuditEventType) -> list[AuditEvent]:
        """Read the audit events of one kind, oldest first.

        Parameters
        ----------
        kind
            The event type.

        Returns
        -------
        list of AuditEvent
            The events.
        """
        return list(
            self.session.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == kind)
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        )

    def files(self, suffix: str) -> set[str]:
        """List the files under the storage root with a suffix.

        Parameters
        ----------
        suffix
            ``.parquet`` or ``.avro``.

        Returns
        -------
        set of str
            Their ``file://`` URIs.
        """
        return {f"file://{path}" for path in self.root.rglob(f"*{suffix}")}


@pytest.fixture
def hist(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> Hist:
    """Create `dev` (which comes with the namespaces bronze, silver and gold) and no table.

    Parameters
    ----------
    catalog
        The catalog, allow-all.
    principal
        The acting principal.
    session
        The database session.
    tmp_path
        The storage root.

    Returns
    -------
    Hist
        The helpers.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    return Hist(service=catalog, admin=principal, session=session, root=tmp_path)


def test_the_first_run_creates_the_table_with_every_row_open(hist: Hist) -> None:
    result = hist.run([("A", "north"), ("B", "north")], T1, first=True)

    assert (result.created, result.versions_opened, result.versions_closed) == (True, 2, 0)
    metadata = hist.static().metadata
    assert [f.name for f in metadata.schema().fields] == [
        "customer_id",
        "region",
        "dim_customer_key",
        "valid_from",
        "valid_to",
        "is_current",
    ]
    assert [f.required for f in metadata.schema().fields][2:] == [True, True, False, True]
    assert json.loads(metadata.properties[HISTORY_KEY_PROPERTY]) == ["customer_id"]
    assert hist.versions() == [
        ("A", "north", EARLIEST_START, None, True),
        ("B", "north", EARLIEST_START, None, True),
    ]
    assert [e.detail["mode"] for e in hist.events(AuditEventType.TABLE_WRITE)] == ["create"]


def test_a_later_run_opens_closes_and_leaves_alone_in_one_snapshot(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north"), ("C", "north")], T1, first=True)
    before = hist.static().metadata

    with mock.patch.object(hist.service, "commit_table", wraps=hist.service.commit_table) as commit:
        result = hist.run([("A", "south"), ("B", "north"), ("D", "west")], T2, close_missing=True)

    assert (result.versions_opened, result.versions_closed, result.closed_missing) == (2, 2, 1)
    assert commit.call_count == 1
    assert commit.call_args.kwargs["own_data_files"] is True
    assert hist.versions() == [
        ("A", "north", EARLIEST_START, T2, False),
        ("A", "south", T2, None, True),
        ("B", "north", EARLIEST_START, None, True),
        ("C", "north", EARLIEST_START, T2, False),
        ("D", "west", EARLIEST_START, None, True),
    ]
    after = hist.static().metadata
    assert {s.snapshot_id for s in after.snapshots} == {
        *(s.snapshot_id for s in before.snapshots),
        result.snapshot_id,
    }
    new = after.snapshot_by_id(result.snapshot_id)  # ty: ignore[invalid-argument-type]
    assert new is not None and new.parent_snapshot_id == before.current_snapshot_id


def test_a_run_that_changes_nothing_commits_nothing(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    location = hist.service.load_table(hist.admin, hist.ref()).metadata_location

    result = hist.run([("A", "north")], T2)

    assert (result.snapshot_id, result.versions_opened, result.versions_closed) == (None, 0, 0)
    assert hist.service.load_table(hist.admin, hist.ref()).metadata_location == location
    assert len(hist.events(AuditEventType.TABLE_WRITE)) == 1


def test_the_same_result_twice_opens_nothing_the_second_time(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    hist.run([("A", "south"), ("B", "west")], T2)
    once = hist.versions()

    again = hist.run([("A", "south"), ("B", "west")], T2)

    assert (again.snapshot_id, again.versions_opened, again.versions_closed) == (None, 0, 0)
    assert hist.versions() == once


def test_a_change_seen_twice_in_one_run_is_refused(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    hist.run([("A", "south")], T2)

    with pytest.raises(ValidationError) as info:
        hist.run([("A", "east")], T2)

    assert str(info.value) == (
        "1 open version of gold.dim_customer starts at or after this run's start time "
        "(2026-03-01T00:00:00Z); a run that started earlier cannot replace or close it. Start a "
        "new run."
    )


def test_an_empty_result_with_close_missing_is_refused_before_anything_is_read(
    hist: Hist,
) -> None:
    hist.run([("A", "north")], T1, first=True)
    location = hist.service.load_table(hist.admin, hist.ref()).metadata_location

    with (
        mock.patch.object(service_module, "read_open_versions", side_effect=AssertionError),
        pytest.raises(ValidationError, match="The asset's result is empty"),
    ):
        hist.run([], T2, close_missing=True)

    assert hist.service.load_table(hist.admin, hist.ref()).metadata_location == location


def test_a_key_that_comes_back_opens_a_new_version(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    hist.run([("B", "north")], T2, close_missing=True)

    result = hist.run([("A", "north"), ("B", "north")], T3, close_missing=True)

    assert (result.versions_opened, result.versions_closed) == (1, 0)
    assert hist.versions()[:2] == [
        ("A", "north", EARLIEST_START, T2, False),
        ("A", "north", T3, None, True),
    ]


def test_a_declared_valid_from_starts_a_later_version_and_is_not_stored(hist: Hist) -> None:
    changed_at = datetime(2026, 2, 15, tzinfo=UTC)
    with mock.patch.object(service_module, "run_clock", return_value=T1):
        hist.service.history(
            hist.admin,
            hist.ref(),
            _with_start([("A", "north")], datetime(2026, 1, 15, tzinfo=UTC)),
            key=["customer_id"],
            valid_from="changed_at",
            expected_snapshot_id="none",
        )
    with mock.patch.object(service_module, "run_clock", return_value=T2):
        hist.service.history(
            hist.admin,
            hist.ref(),
            _with_start([("A", "south")], changed_at),
            key=["customer_id"],
            valid_from="changed_at",
        )

    assert "changed_at" not in hist.static().metadata.schema().column_names
    assert hist.versions() == [
        ("A", "north", EARLIEST_START, changed_at, False),
        ("A", "south", changed_at, None, True),
    ]


def test_a_returning_key_closed_later_than_its_new_start_is_refused(hist: Hist) -> None:
    def run(rows: Sequence[Row], start: datetime, clock: datetime) -> None:
        data = hist.batch(rows).append_column(
            "changed_at", pa.array([start] * len(rows), pa.timestamp("us", tz="UTC"))
        )
        with mock.patch.object(service_module, "run_clock", return_value=clock):
            hist.service.history(
                hist.admin,
                hist.ref(),
                data,
                key=["customer_id"],
                valid_from="changed_at",
                close_missing=True,
                expected_snapshot_id="none" if start == T1 else None,
            )

    run([("A", "north"), ("B", "north")], T1, T1)
    run([("B", "north")], T2, T3)  # A is closed at the clock, T3
    with pytest.raises(ValidationError, match="before the end of the key's last version in 1 row"):
        run([("A", "north"), ("B", "north")], T2, T3)


def test_the_audit_event_names_the_counts_the_key_and_the_clock(hist: Hist) -> None:
    hist.run([("A", "north"), ("C", "north")], T1, first=True)

    result = hist.run([("A", "south")], T2, close_missing=True)

    event = hist.events(AuditEventType.TABLE_WRITE)[-1]
    assert event.detail["mode"] == "history"
    assert event.detail["rows"] == 3
    assert event.detail["snapshot_id"] == result.snapshot_id
    assert {
        k: event.detail[k]
        for k in (
            "versions_opened",
            "versions_closed",
            "closed_missing",
            "key",
            "valid_from",
            "run_started_at",
        )
    } == {
        "versions_opened": 1,
        "versions_closed": 2,
        "closed_missing": 1,
        "key": ["customer_id"],
        "valid_from": "run",
        "run_started_at": "2026-03-01T00:00:00+00:00",
    }


def test_a_history_write_records_a_lineage_edge_per_source(hist: Hist) -> None:
    hist.service.create_table(
        hist.admin,
        hist.ref("silver.customers"),
        schema=Schema(NestedField(1, "customer_id", StringType(), required=False)),
    )
    hist.run([("A", "north")], T1, first=True, sources=["silver.customers"])

    result = hist.run([("A", "south")], T2, sources=["silver.customers"])

    edges = hist.session.scalars(
        select(LineageEdge).where(LineageEdge.target_snapshot_id == result.snapshot_id)
    ).all()
    assert [edge.source_name for edge in edges] == ["silver.customers"]


def test_a_lost_race_conflicts_and_the_retry_diffs_against_the_new_state(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    other = CatalogService(hist.session, StorageResolver(hist.session), AllowAllAuthorization())
    real = service_module.stage_history
    calls = {"n": 0}

    def racing(*args: object, **kwargs: object) -> object:
        # The other writer's own staging comes back here too; only the first call races.
        calls["n"] += 1
        staged = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        if calls["n"] == 1:
            other.history(hist.admin, hist.ref(), hist.batch([("A", "east")]), key=["customer_id"])
        return staged

    with (
        mock.patch.object(service_module, "stage_history", side_effect=racing),
        pytest.raises(ConflictError, match="changed during the write"),
    ):
        hist.run([("A", "south"), ("B", "north")], T2)

    retried = hist.run([("A", "south"), ("B", "north")], T3)
    assert (retried.versions_opened, retried.versions_closed) == (1, 1)
    assert sorted((v[2], v[1], v[3], v[4]) for v in hist.versions() if v[0] == "A") == [
        (EARLIEST_START, "north", T2, False),
        (T2, "east", T3, False),
        (T3, "south", None, True),
    ]
    named = {s.manifest_list for s in hist.static().metadata.snapshots}
    assert {f for f in hist.files(".avro") if "snap-" in f} <= named


def test_a_failed_commit_deletes_every_file_the_write_staged(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    parquet, avro = hist.files(".parquet"), hist.files(".avro")

    with (
        mock.patch.object(hist.service, "commit_table", side_effect=OSError("storage went away")),
        pytest.raises(OSError, match="storage went away"),
    ):
        hist.run([("A", "south")], T2, close_missing=True)

    assert hist.files(".parquet") == parquet
    assert hist.files(".avro") == avro


def test_open_versions_are_read_by_is_current_so_closed_files_are_skipped(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    hist.run([("A", "south"), ("B", "south")], T2)  # every first version closed, in one file

    metadata = hist.static().metadata
    file_io = StorageResolver(hist.session).for_environment("dev").file_io
    every = DataScan(table_metadata=metadata, io=file_io).plan_files()
    current = DataScan(
        table_metadata=metadata, io=file_io, row_filter=EqualTo(term="is_current", value=True)
    ).plan_files()
    assert len(list(current)) == len(list(every)) - 1


@pytest.mark.parametrize("make", ["replace", "merge"])
def test_a_table_built_another_way_is_refused(hist: Hist, make: str) -> None:
    if make == "replace":
        hist.service.create_table(
            hist.admin,
            hist.ref(),
            schema=Schema(
                NestedField(1, "customer_id", StringType(), required=False),
                NestedField(2, "region", StringType(), required=False),
            ),
        )
    else:
        hist.service.merge(
            hist.admin,
            hist.ref(),
            hist.batch([("A", "north")]),
            key=["customer_id"],
            expected_snapshot_id="none",
        )

    with pytest.raises(ValidationError) as info:
        hist.run([("A", "north")], T2)

    assert str(info.value) == (
        "gold.dim_customer exists without the columns `write: history` adds; a history table is "
        "created by its first history run. Drop it (`lakana table delete gold.dim_customer --env "
        "dev`) so that the next run creates it with every row as an open version, or build the "
        "history into a new asset."
    )


def test_a_changed_key_is_refused(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)

    with (
        mock.patch.object(service_module, "run_clock", return_value=T2),
        pytest.raises(ValidationError) as info,
    ):
        hist.service.history(
            hist.admin, hist.ref(), hist.batch([("A", "north")]), key=["customer_id", "region"]
        )

    assert str(info.value) == (
        "gold.dim_customer keeps its history by `customer_id`, and the asset now declares `key: "
        "[customer_id, region]`; a history table keeps its key. Declare the old key again, or "
        "build the new key into a new asset (its history starts with its first run)."
    )


def test_a_result_with_another_column_is_refused(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    data = hist.batch([("A", "north")]).append_column("segment", pa.array(["x"]))

    with (
        mock.patch.object(service_module, "run_clock", return_value=T2),
        pytest.raises(ValidationError) as info,
    ):
        hist.service.history(hist.admin, hist.ref(), data, key=["customer_id"])

    assert str(info.value) == (
        "The result does not match gold.dim_customer's columns (added segment (string)); a "
        "history table keeps its columns. Make the SELECT (or the Python asset's result) return "
        "the columns and types gold.dim_customer has; to add a column, build a new history asset "
        "(its history starts with its first run)."
    )


def test_two_open_versions_of_a_key_are_refused(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    duplicate = hist.static().scan().to_arrow()
    hist.service.append(hist.admin, hist.ref(), duplicate)

    with pytest.raises(ValidationError) as info:
        hist.run([("A", "south")], T2)

    assert str(info.value) == (
        "1 key of gold.dim_customer has more than one open version; there must be one per key, "
        "so nothing was written. Drop the table (`lakana table delete gold.dim_customer --env "
        "dev`) and let the next run create it again (its history starts again with that run)."
    )


def test_open_versions_sharing_a_surrogate_key_are_refused_after_staging(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    twin = hist.static().scan().to_arrow()
    twin = twin.set_column(0, "customer_id", pa.array(["B"], twin.schema.field(0).type))
    hist.service.append(hist.admin, hist.ref(), twin)  # B's open version has A's surrogate key
    parquet, avro = hist.files(".parquet"), hist.files(".avro")
    location = hist.service.load_table(hist.admin, hist.ref()).metadata_location

    with pytest.raises(ValidationError) as info:
        hist.run([("A", "south"), ("B", "north")], T2)

    assert str(info.value) == (
        "This write would change the number of rows in gold.dim_customer by 0, but it opens 1 "
        "version: an open version shares its surrogate key (`dim_customer_key`) with another, "
        "so closing one would remove both. Nothing was written. Drop the table (`lakana table "
        "delete gold.dim_customer --env dev`) and let the next run create it again (its history "
        "starts again with that run)."
    )
    assert hist.service.load_table(hist.admin, hist.ref()).metadata_location == location
    assert (hist.files(".parquet"), hist.files(".avro")) == (parquet, avro)


def test_the_arrow_pool_returns_its_unused_memory_after_a_write_that_fails(hist: Hist) -> None:
    pool = mock.Mock()
    with (
        mock.patch.object(service_module.pa, "default_memory_pool", return_value=pool),
        pytest.raises(ValidationError),
    ):
        hist.run([], T1, first=True, close_missing=True)

    pool.release_unused.assert_called_once_with()


def test_more_open_versions_than_max_rows_are_refused(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north"), ("C", "north")], T1, first=True)

    with pytest.raises(ValidationError, match=r"exceeds max_write_rows \(2\)"):
        hist.run([("A", "north")], T2, max_rows=2)


def test_a_composite_key_over_merges_cap_writes(hist: Hist) -> None:
    rows = pa.table({"a": list(range(6000)), "b": ["x"] * 6000, "v": ["n"] * 6000})
    with mock.patch.object(service_module, "run_clock", return_value=T1):
        result = hist.service.history(
            hist.admin,
            hist.ref(),
            rows,
            key=["a", "b"],
            expected_snapshot_id="none",
            advice=SQL_ADVICE,
        )
    assert result.versions_opened == 6000


def test_history_needs_write_then_read_table(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    allow = ActionAllowList(Action.WRITE_TABLE)
    limited = CatalogService(hist.session, StorageResolver(hist.session), allow)

    with pytest.raises(PermissionDenied):
        limited.history(hist.admin, hist.ref(), hist.batch([("A", "x")]), key=["customer_id"])

    assert [action for action, _ in allow.seen] == [Action.WRITE_TABLE, Action.READ_TABLE]


_HIDDEN = (
    "History writes into gold.dim_customer are refused while a row filter or column mask applies "
    "to you on it: a history write compares your rows with rows you can't see. Ask someone the "
    "table is not filtered or masked for to run it, or ask an owner to lift the filter or mask "
    "for you."
)


def test_a_writer_with_a_row_filter_may_not_write_history(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    RowFilterService(hist.session, AllowAllAuthorization(), hist.service).create(
        hist.admin,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="dim_customer",
        expression="customer_id = 'A'",
    )
    with pytest.raises(ValidationError) as info:
        hist.run([("A", "south")], T2)
    assert str(info.value) == _HIDDEN


def test_a_writer_with_a_column_mask_may_not_write_history(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    ColumnMaskService(hist.session, AllowAllAuthorization(), hist.service).create(
        hist.admin,
        principal="admin",
        environment="dev",
        namespace="gold",
        table="dim_customer",
        column="region",
        expression="'***'",
    )
    with pytest.raises(ValidationError) as info:
        hist.service.check_can_history(hist.admin, hist.ref())
    assert str(info.value) == _HIDDEN


def test_a_task_may_not_write_history_into_another_table(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    governed, task = _bound_task(hist)  # ty: ignore[invalid-argument-type]

    with pytest.raises(PermissionDenied, match="may write only its own asset"):
        governed.history(task, hist.ref(), hist.batch([("A", "x")]), key=["customer_id"])


def test_a_batch_over_max_rows_is_refused_before_anything_is_written(hist: Hist) -> None:
    with pytest.raises(ValidationError, match=r"exceeds max_write_rows \(2\)"):
        hist.run([("A", "north"), ("B", "north"), ("C", "north")], T1, first=True, max_rows=2)

    assert hist.service._existing_table(hist.ref()) is None


def test_a_table_created_by_someone_else_meanwhile_conflicts(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)

    with (
        mock.patch.object(hist.service, "_existing_table", return_value=None),
        pytest.raises(ConflictError, match="changed during the write"),
    ):
        hist.run([("A", "south")], T2, first=True)


def test_an_empty_first_run_creates_the_table_and_the_next_opens_its_rows(hist: Hist) -> None:
    created = hist.run([], T1, first=True)

    result = hist.run([("A", "north")], T2)

    assert (created.created, created.snapshot_id, created.versions_opened) == (True, None, 0)
    assert (result.versions_opened, result.versions_closed) == (1, 0)
    assert hist.versions() == [("A", "north", EARLIEST_START, None, True)]


@pytest.mark.parametrize("stored", ["", "not json", '{"customer_id": 1}', "[]", "[1]"])
def test_a_missing_or_unreadable_key_property_is_not_a_history_table(stored: str) -> None:
    schema = Schema(
        NestedField(1, "customer_id", StringType(), required=False),
        NestedField(2, "dim_customer_key", StringType(), required=True),
        NestedField(3, "valid_from", TimestamptzType(), required=True),
        NestedField(4, "valid_to", TimestamptzType(), required=False),
        NestedField(5, "is_current", BooleanType(), required=True),
    )
    properties = {HISTORY_KEY_PROPERTY: stored} if stored else {}

    with pytest.raises(ValidationError, match="exists without the columns `write: history` adds"):
        check_history_table(schema, properties, key=["customer_id"], table=DIM, environment="dev")


def test_a_failure_while_staging_deletes_what_was_staged(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    parquet, avro = hist.files(".parquet"), hist.files(".avro")

    with (
        mock.patch.object(history_module, "fold_snapshots", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        hist.run([("A", "south")], T2, close_missing=True)

    assert hist.files(".parquet") == parquet
    assert hist.files(".avro") == avro


def test_a_history_write_into_a_protected_environment_needs_break_glass(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    hist.service.set_environment_protection(hist.admin, "dev", True)

    with (
        mock.patch.object(service_module, "read_open_versions", side_effect=AssertionError),
        pytest.raises(PermissionDenied) as info,
    ):
        hist.run([("A", "south")], T2)

    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")
    (denied,) = hist.events(AuditEventType.PROTECTED_WRITE_DENIED)
    assert denied.detail["operation"] == "history"
    glass = dataclasses.replace(hist.admin, break_glass_reason="incident 9: fix a customer row")
    with mock.patch.object(service_module, "run_clock", return_value=T2):
        result = hist.service.history(
            glass, hist.ref(), hist.batch([("A", "south")]), key=["customer_id"]
        )
    assert (result.versions_opened, result.versions_closed) == (1, 1)
    (glass_event,) = hist.events(AuditEventType.BREAK_GLASS_WRITE)
    assert glass_event.detail["operation"] == "history"


def test_a_uuid_key_is_refused_with_the_cast_to_use(hist: Hist) -> None:
    ids = pa.array([uuid.UUID(int=1).bytes], pa.binary(16))
    data = pa.table(
        {"id": pa.ExtensionArray.from_storage(pa.uuid(), ids), "region": pa.array(["north"])}
    )

    with pytest.raises(ValidationError) as info:
        hist.service.history(hist.admin, hist.ref(), data, key=["id"], expected_snapshot_id="none")

    assert str(info.value) == (
        "key column `id` is a uuid, which a history key cannot be; cast it in the SELECT, for "
        "example `CAST(id AS VARCHAR)`."
    )


def test_a_task_writes_history_into_its_own_asset_without_reading_it(hist: Hist) -> None:
    own = hist.ref("bronze.customers")
    with mock.patch.object(service_module, "run_clock", return_value=T1):
        hist.service.history(
            hist.admin,
            own,
            hist.batch([("A", "north")]),
            key=["customer_id"],
            expected_snapshot_id="none",
        )
    governed, task = _bound_task(hist)  # ty: ignore[invalid-argument-type]

    with mock.patch.object(service_module, "run_clock", return_value=T2):
        result = governed.history(
            task, own, hist.batch([("A", "south"), ("B", "west")]), key=["customer_id"]
        )

    assert (result.versions_opened, result.versions_closed) == (2, 1)
    with pytest.raises(PermissionDenied, match="may read only its declared inputs"):
        governed.load_table(task, own)


def _task(hist: Hist) -> PrincipalRef:
    """Act as the running claim-form task of `gold.dim_customer` (a Python task's table asset).

    Parameters
    ----------
    hist
        The helpers.

    Returns
    -------
    PrincipalRef
        The admin with that task run's run context; its writes carry the run's stamp.
    """
    task = python_task_run(
        hist.session, hist.admin.id, asset=DIM, inputs={"rows": "silver.customers"}, extra={}
    )
    return dataclasses.replace(hist.admin, run_context=RunContext(task_run_id=task.id, claim=1))


def _with_start(rows: Sequence[Row], start: datetime) -> pa.Table:
    """Add a `changed_at` column, one start for every row.

    Parameters
    ----------
    rows
        The batch.
    start
        The start.

    Returns
    -------
    pa.Table
        The batch with `changed_at`.
    """
    return Hist.batch(rows).append_column(
        "changed_at", pa.array([start] * len(rows), pa.timestamp("us", tz="UTC"))
    )


def test_a_run_that_already_wrote_the_history_may_not_write_it_again(hist: Hist) -> None:
    with mock.patch.object(service_module, "run_clock", return_value=T1):
        hist.service.history(
            _task(hist),
            hist.ref(),
            _with_start([("A", "north")], T1),
            key=["customer_id"],
            valid_from="changed_at",
            expected_snapshot_id="none",
        )
    task = _task(hist)
    with mock.patch.object(service_module, "run_clock", return_value=T3):
        hist.service.history(
            task,
            hist.ref(),
            _with_start([("A", "south")], T2),
            key=["customer_id"],
            valid_from="changed_at",
        )
        before = hist.versions()

        unchanged = hist.service.history(
            task,
            hist.ref(),
            _with_start([("A", "south")], T2),
            key=["customer_id"],
            valid_from="changed_at",
        )
        with pytest.raises(ValidationError) as info:
            hist.service.history(
                task,
                hist.ref(),
                _with_start([("A", "east")], T3),
                key=["customer_id"],
                valid_from="changed_at",
            )

    assert unchanged.snapshot_id is None
    assert str(info.value) == (
        "This run already wrote this table's history (the current snapshot of gold.dim_customer "
        "is its write), and a run opens at most one version per key. Start a new run to record "
        "the newer values."
    )
    assert hist.versions() == before


def test_another_run_writes_after_a_run_that_wrote_the_history(hist: Hist) -> None:
    with mock.patch.object(service_module, "run_clock", return_value=T2):
        hist.service.history(
            _task(hist),
            hist.ref(),
            _with_start([("A", "north")], T1),
            key=["customer_id"],
            valid_from="changed_at",
            expected_snapshot_id="none",
        )
    with mock.patch.object(service_module, "run_clock", return_value=T3):
        result = hist.service.history(
            _task(hist),
            hist.ref(),
            _with_start([("A", "south")], T2),
            key=["customer_id"],
            valid_from="changed_at",
        )

    assert (result.versions_opened, result.versions_closed) == (1, 1)


def test_the_closed_versions_a_returning_key_reads_are_capped(hist: Hist) -> None:
    t4 = datetime(2026, 5, 1, tzinfo=UTC)
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    hist.run([("B", "north")], T2, close_missing=True)
    hist.run([("A", "north"), ("B", "north")], T3, close_missing=True)
    hist.run([("B", "north")], t4, close_missing=True)
    metadata = hist.static().metadata
    file_io = StorageResolver(hist.session).for_environment("dev").file_io
    wanted = pa.table({"customer_id": ["A"]})

    ends = read_last_ends(metadata, file_io, wanted, T1, key=["customer_id"], table=DIM, max_rows=2)
    with pytest.raises(ValidationError) as one:
        read_last_ends(metadata, file_io, wanted, T1, key=["customer_id"], table=DIM, max_rows=1)
    with pytest.raises(ValidationError) as none:
        read_last_ends(metadata, file_io, wanted, T1, key=["customer_id"], table=DIM, max_rows=0)

    assert ends.to_pylist() == [{"customer_id": "A", "valid_to": t4}]  # A's latest end
    assert str(one.value) == (
        "Together, the keys this run opens again have more than 1 closed version in "
        "gold.dim_customer to read (their first versions, and the versions that end after "
        "2026-02-01T00:00:00Z). That is more rows than one "
        "write may read (the max_write_rows setting, a total for all keys), so nothing was "
        "committed. Start the returning versions later (a later `valid_from`), or ask an "
        "operator to raise max_write_rows."
    )
    assert str(none.value).startswith(
        "Together, the keys this run opens again have more than 0 closed versions in "
        "gold.dim_customer to read (their first versions, and the versions that end after "
        "2026-02-01T00:00:00Z). That is more rows"
    )


def _dated(
    hist: Hist, keys: Sequence[str], region: str, start: datetime, **options: object
) -> HistoryResult:
    """Write `keys` with a declared `valid_from` (`changed_at`) at a late run clock.

    Parameters
    ----------
    hist
        The helpers.
    keys
        The batch's keys.
    region
        Every row's region.
    start
        Every row's `changed_at`.
    **options
        More keywords for `CatalogService.history`.

    Returns
    -------
    HistoryResult
        What it committed.
    """
    data = pa.table(
        {
            "customer_id": pa.array(keys, pa.string()),
            "region": pa.array([region] * len(keys), pa.string()),
            "changed_at": pa.array([start] * len(keys), pa.timestamp("us", tz="UTC")),
        }
    )
    clock = datetime(2030, 1, 1, tzinfo=UTC)
    with mock.patch.object(service_module, "run_clock", return_value=clock):
        return hist.service.history(
            hist.admin,
            hist.ref(),
            data,
            key=["customer_id"],
            valid_from="changed_at",
            **options,  # ty: ignore[invalid-argument-type]
        )


def test_the_ends_of_returning_keys_are_held_one_per_key_whatever_the_history(
    hist: Hist, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old `valid_from` reads every closed version of the keys; only each key's end is kept."""
    keys = [f"{i:02d}" + "k" * 4096 for i in range(8)]  # 8 wide keys, about 32 KiB a batch
    _dated(hist, keys, "r0", datetime(2026, 1, 1, tzinfo=UTC), expected_snapshot_id="none")
    for n in range(1, 5):
        _dated(hist, keys, f"r{n}", datetime(2026, 1, 1 + n, tzinfo=UTC))
    _dated(hist, ["Z"], "z", datetime(2027, 1, 1, tzinfo=UTC), close_missing=True)
    rows: list[int] = []
    original = pyiceberg_arrow._task_to_record_batches

    def counting(*args: object, **kwargs: object) -> object:
        for batch in original(*args, **kwargs):  # ty: ignore[invalid-argument-type]
            rows.append(batch.num_rows)
            yield batch

    ends: list[pa.Table] = []
    read = service_module.read_last_ends

    def spy(*args: object, **kwargs: object) -> pa.Table:
        ends.append(read(*args, **kwargs))  # ty: ignore[invalid-argument-type]
        return ends[-1]

    monkeypatch.setattr(pyiceberg_arrow, "_task_to_record_batches", counting)
    monkeypatch.setattr(service_module, "read_last_ends", spy)
    cap = 3 * sum(len(k) for k in keys)  # below the 5 closed versions of every key

    with pytest.raises(ValidationError, match="before the end of the key's last version"):
        _dated(hist, keys, "back", datetime(2000, 1, 1, tzinfo=UTC), max_open_bytes=cap)

    assert sum(rows) >= 5 * len(keys)  # every closed version was read ...
    assert ends[0].num_rows == len(keys)  # ... and one end per key held
    assert ends[0].nbytes <= cap


def test_the_ends_of_returning_keys_have_the_byte_cap_as_a_backstop(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    hist.run([("C", "north")], T2, close_missing=True)  # A and B closed at T2
    metadata = hist.static().metadata
    file_io = StorageResolver(hist.session).for_environment("dev").file_io
    wanted = pa.table({"customer_id": ["A", "B"]})

    with pytest.raises(ValidationError) as info:
        read_last_ends(
            metadata,
            file_io,
            wanted,
            T1,
            key=["customer_id"],
            table=DIM,
            max_rows=None,
            max_bytes=1,
            holder="server",
        )

    assert str(info.value).startswith(
        "Reading the closed versions of the keys that return to gold.dim_customer passed "
    )
    assert "LAKANA_RUN__MAX_HISTORY_OPEN_BYTES (1 bytes, 0.0 MiB) allows" in str(info.value)


def test_no_key_filter_is_built_when_the_table_has_no_closed_version(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    metadata = hist.static().metadata
    file_io = StorageResolver(hist.session).for_environment("dev").file_io
    wanted = pa.table({"customer_id": ["A", "B"]})

    with mock.patch.object(history_module, "In", wraps=history_module.In) as built:
        ends = read_last_ends(
            metadata, file_io, wanted, T2, key=["customer_id"], table=DIM, max_rows=None
        )

    assert ends.num_rows == 0
    built.assert_not_called()


@pytest.mark.parametrize("in_limit", [10_000, 0], ids=["with-in", "without-in"])
def test_only_the_exact_returning_keys_count_against_the_cap(hist: Hist, in_limit: int) -> None:
    def run(rows: list[tuple[int, str]], clock: datetime, **options: object) -> None:
        data = pa.table({"id": [r[0] for r in rows], "country": [r[1] for r in rows]})
        data = data.append_column("v", pa.array(["x"] * len(rows)))
        with mock.patch.object(service_module, "run_clock", return_value=clock):
            hist.service.history(
                hist.admin,
                hist.ref(),
                data,
                key=["id", "country"],
                close_missing=True,
                **options,  # ty: ignore[invalid-argument-type]
            )

    run([(1, "DE"), (1, "FR"), (2, "FR")], T1, expected_snapshot_id="none")
    run([(2, "DE")], T2)  # every version closed at T2; (2, DE) opened
    metadata = hist.static().metadata
    file_io = StorageResolver(hist.session).for_environment("dev").file_io
    wanted = pa.table({"id": [1, 2], "country": ["DE", "FR"]})

    with mock.patch.object(history_module, "IN_LIMIT", in_limit):
        ends = read_last_ends(
            metadata, file_io, wanted, T1, key=["id", "country"], table=DIM, max_rows=2
        )

    assert sorted((r["id"], r["country"]) for r in ends.to_pylist()) == [(1, "DE"), (2, "FR")]


def test_the_open_versions_cap_defaults_to_the_settings_default() -> None:
    default = inspect.signature(CatalogService.history).parameters["max_open_versions"].default
    assert default == RunConfig().max_history_open_versions == 1_000_000


def test_a_table_over_the_open_versions_cap_is_refused_before_anything_is_read(
    hist: Hist,
) -> None:
    hist.run([("A", "north"), ("B", "north"), ("C", "north")], T1, first=True)

    with (
        mock.patch.object(service_module, "read_open_versions", side_effect=AssertionError),
        pytest.raises(ValidationError) as info,
    ):
        hist.run([("A", "south")], T2, max_open_versions=2, advice=SQL_ADVICE)

    assert str(info.value) == (
        "gold.dim_customer has 3 open versions, more than LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS "
        "(2) allows. A history write holds every open version in the worker's memory, roughly "
        "2 GiB per million open versions of 8 columns and more for wider rows, so nothing was "
        "read or written. Ask an operator to raise LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS "
        "together with the worker's memory limit (LAKANA_WORKER__MEMORY_LIMIT)."
    )


def test_the_cap_names_the_server_when_the_server_holds_the_versions(hist: Hist) -> None:
    """A Python asset writes over REST: the server, not the worker, holds the versions."""
    hist.run([("A", "north"), ("B", "north"), ("C", "north")], T1, first=True)

    with pytest.raises(ValidationError) as info:
        hist.run([("A", "south")], T2, max_open_versions=2)

    assert str(info.value) == (
        "gold.dim_customer has 3 open versions, more than LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS "
        "(2) allows. A history write holds every open version in the server's memory, roughly "
        "2 GiB per million open versions of 8 columns and more for wider rows, so nothing was "
        "read or written. Ask an operator to raise LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS "
        "together with the memory the server has."
    )


def test_a_first_run_over_the_open_versions_cap_creates_nothing(hist: Hist) -> None:
    with (
        mock.patch.object(service_module, "first_versions", side_effect=AssertionError),
        pytest.raises(ValidationError) as info,
    ):
        hist.run(
            [("A", "north"), ("B", "north"), ("C", "north")],
            T1,
            first=True,
            max_open_versions=2,
            advice=SQL_ADVICE,
        )

    assert str(info.value) == (
        "gold.dim_customer would start with 3 open versions, more than "
        "LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS (2) allows. A history write holds every open "
        "version in the worker's memory, roughly 2 GiB per million open versions of 8 columns "
        "and more for wider rows, so nothing was read or written. Ask an operator to raise "
        "LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS together with the worker's memory limit "
        "(LAKANA_WORKER__MEMORY_LIMIT)."
    )
    assert not hist.service.table_exists(hist.admin, hist.ref())


@pytest.mark.parametrize("cap", [3, 4])
def test_a_table_at_or_under_a_raised_open_versions_cap_is_written(hist: Hist, cap: int) -> None:
    hist.run([("A", "north"), ("B", "north"), ("C", "north")], T1, first=True)

    result = hist.run([("A", "south")], T2, max_open_versions=cap)

    assert (result.versions_opened, result.versions_closed) == (1, 1)


def test_the_open_bytes_cap_defaults_to_the_settings_default() -> None:
    default = inspect.signature(CatalogService.history).parameters["max_open_bytes"].default
    assert default == RunConfig().max_history_open_bytes == 536_870_912


@pytest.mark.parametrize(
    ("advice", "memory"),
    [
        (SQL_ADVICE, "the worker's memory limit (LAKANA_WORKER__MEMORY_LIMIT) allows it"),
        (SDK_ADVICE, "the memory the server has allows it"),
    ],
)
def test_open_versions_past_the_byte_cap_are_refused_while_reading_and_nothing_is_written(
    hist: Hist, advice: MergeAdvice, memory: str
) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    before = hist.versions()

    with pytest.raises(ValidationError) as info:
        hist.run([("A", "south")], T2, max_open_bytes=1, advice=advice)

    found = re.fullmatch(
        r"Reading the open versions of gold\.dim_customer passed (\d+) bytes \(\d+\.\d MiB\) of "
        r"Arrow data, more than LAKANA_RUN__MAX_HISTORY_OPEN_BYTES \(1 bytes, 0\.0 MiB\) allows, "
        r"so the read stopped and nothing "
        rf"was written\. A history write holds every open version in the {advice.holder}'s "
        r"memory\. Ask an operator to raise LAKANA_RUN__MAX_HISTORY_OPEN_BYTES if "
        rf"{re.escape(memory)}, or narrow the dimension's columns: keep wide text such as "
        r"descriptions in a table of its own and join it by the key\.",
        str(info.value),
    )
    assert found is not None, str(info.value)
    assert int(found.group(1)) > 1
    assert hist.versions() == before


def test_open_versions_under_the_byte_cap_are_written(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)

    result = hist.run([("A", "south")], T2, max_open_bytes=1024 * 1024)

    assert (result.versions_opened, result.versions_closed) == (1, 1)


def test_the_byte_cap_stops_the_read_before_the_rest_of_the_table_is_read(
    hist: Hist, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most the cap plus one batch is read: the files are read one after another, lazily."""
    wide = "x" * 4096
    hist.run([("K0", wide)], T1, first=True)
    for day in range(1, 8):  # one more data file of open versions per write
        hist.run([(f"K{day}", wide)], datetime(2026, 2, day + 1, tzinfo=UTC))
    metadata = hist.static().metadata
    file_io = StorageResolver(hist.session).for_environment("dev").file_io
    read: list[int] = []
    original = pyiceberg_arrow._task_to_record_batches

    def counting(*args: object, **kwargs: object) -> object:
        for batch in original(*args, **kwargs):  # ty: ignore[invalid-argument-type]
            read.append(batch.nbytes)
            yield batch

    monkeypatch.setattr(pyiceberg_arrow, "_task_to_record_batches", counting)
    whole = read_open_versions(
        metadata, file_io, max_rows=None, max_bytes=None, table=DIM, holder="server"
    )
    assert whole.num_rows == 8
    assert len(read) == 8
    cap = sum(read) // 3
    biggest = max(read)
    read.clear()

    with pytest.raises(ValidationError, match="LAKANA_RUN__MAX_HISTORY_OPEN_BYTES"):
        read_open_versions(
            metadata, file_io, max_rows=None, max_bytes=cap, table=DIM, holder="server"
        )

    assert sum(read) <= cap + biggest
    assert len(read) < 8


def test_open_versions_are_counted_from_file_bounds_and_mixed_files_are_scanned(
    hist: Hist,
) -> None:
    hist.run([("A", "north"), ("B", "north"), ("C", "north")], T1, first=True)
    hist.run([("A", "south"), ("B", "north"), ("C", "north")], T2)  # a closed file and two open
    file_io = StorageResolver(hist.session).for_environment("dev").file_io
    with mock.patch.object(history_module, "DataScan", wraps=history_module.DataScan) as none:
        pure = count_open_versions(hist.static().metadata, file_io, table=DIM, environment="dev")
    assert none.call_count == 0  # the manifests alone
    rows = hist.static().scan().to_arrow()
    mixed = rows.filter(
        pa.array([c == "A" for c in rows["customer_id"].to_pylist()])
    )  # A's closed and open rows
    mixed = mixed.set_column(0, "customer_id", pa.array(["X", "Y"], rows.schema.field(0).type))
    hist.service.append(hist.admin, hist.ref(), mixed)

    with mock.patch.object(history_module, "DataScan", wraps=history_module.DataScan) as scans:
        counted = count_open_versions(hist.static().metadata, file_io, table=DIM, environment="dev")

    assert (pure, counted) == (3, 4)
    assert scans.call_count == 1


def test_a_table_with_row_level_delete_files_is_refused_with_its_cause(hist: Hist) -> None:
    hist.run([("A", "north")], T1, first=True)
    real = history_module._live_entries

    def with_a_delete_file(snapshot: object, file_io: object) -> list[object]:
        deletes = SimpleNamespace(
            data_file=SimpleNamespace(content=DataFileContent.POSITION_DELETES)
        )
        return [*real(snapshot, file_io), deletes]  # ty: ignore[invalid-argument-type]

    with (
        mock.patch.object(history_module, "_live_entries", side_effect=with_a_delete_file),
        mock.patch.object(service_module, "read_open_versions", side_effect=AssertionError),
        pytest.raises(UnsupportedTableError) as info,
    ):
        hist.run([("A", "south")], T2)

    assert str(info.value) == (
        "gold.dim_customer has row-level delete files, which an Iceberg writer outside Lakana "
        "added; a history write cannot keep its versions right with them, so nothing was read or "
        "written. Compact the table with that writer so that no delete files remain, or drop it "
        "(`lakana table delete gold.dim_customer --env dev`) and let the next run create it "
        "again (its history starts again with that run)."
    )


def test_a_returning_keys_first_version_is_read_whatever_its_end(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    hist.run([("B", "north")], T2, close_missing=True)  # A's first version ends at T2
    metadata = hist.static().metadata
    file_io = StorageResolver(hist.session).for_environment("dev").file_io
    wanted = pa.table({"customer_id": ["A", "C"]})

    ends = read_last_ends(metadata, file_io, wanted, T3, key=["customer_id"], table=DIM, max_rows=5)

    assert ends.to_pylist() == [{"customer_id": "A", "valid_to": T2}]


def test_a_new_key_starts_at_the_earliest_time_and_a_returning_one_at_the_clock(
    hist: Hist,
) -> None:
    hist.run([("A", "north")], T1, first=True)
    hist.run([], T2, close_missing=False)
    hist.run([("B", "north")], T2, close_missing=True)  # A closed at T2, B new

    hist.run([("A", "north"), ("B", "south")], T3)

    assert hist.versions() == [
        ("A", "north", EARLIEST_START, T2, False),
        ("A", "north", T3, None, True),
        ("B", "north", EARLIEST_START, T3, False),
        ("B", "south", T3, None, True),
    ]


def _started(hist: Hist, started: datetime) -> PrincipalRef:
    """Act as a running task whose run started at a given time (the run clock it reads).

    Parameters
    ----------
    hist
        The helpers.
    started
        The run's `started_at`.

    Returns
    -------
    PrincipalRef
        The admin with that task run's run context.
    """
    principal = _task(hist)
    assert principal.run_context is not None
    task = hist.session.get_one(TaskRun, principal.run_context.task_run_id)
    hist.session.get_one(Run, task.run_id).started_at = started
    hist.session.commit()
    return principal


def test_an_older_run_may_not_replace_a_first_version_after_a_newer_run_wrote(
    hist: Hist,
) -> None:
    newer, older = _started(hist, T2), _started(hist, T1)
    hist.service.history(
        newer,
        hist.ref(),
        hist.batch([("A", "north")]),
        key=["customer_id"],
        expected_snapshot_id="none",
    )
    before = hist.versions()

    with pytest.raises(ValidationError) as changed:
        hist.service.history(older, hist.ref(), hist.batch([("A", "south")]), key=["customer_id"])
    with pytest.raises(ValidationError) as missing:
        hist.service.history(
            older,
            hist.ref(),
            hist.batch([("B", "north")]),
            key=["customer_id"],
            close_missing=True,
        )

    assert str(changed.value) == (
        "A run that started after this one (2026-03-01T00:00:00Z) already wrote "
        "gold.dim_customer, and this run (2026-02-01T00:00:00Z) would replace or close 1 first "
        "version that it may have opened with newer values, so nothing was written. Start a new "
        "run."
    )
    assert str(missing.value).startswith("A run that started after this one (2026-03-01")
    assert hist.versions() == before
    added = hist.service.history(
        older, hist.ref(), hist.batch([("B", "north")]), key=["customer_id"]
    )
    assert added.versions_opened == 1


def test_a_result_over_the_open_versions_cap_is_refused_before_anything_is_read(
    hist: Hist,
) -> None:
    hist.run([("A", "north")], T1, first=True)
    four_new = [("A", "north"), ("B", "north"), ("C", "north"), ("D", "north"), ("E", "north")]

    with (
        mock.patch.object(service_module, "read_open_versions", side_effect=AssertionError),
        pytest.raises(ValidationError) as info,
    ):
        hist.run(four_new, T2, max_open_versions=2, advice=SQL_ADVICE)

    assert str(info.value) == (
        "The asset's result for gold.dim_customer has 5 rows, and each can open a version: "
        "more than LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS (2) allows. A history write holds every "
        "open version in the worker's memory, roughly 2 GiB per million open versions of 8 "
        "columns and more for wider rows, so nothing was read or written. Ask an operator to "
        "raise LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS together with the worker's memory limit "
        "(LAKANA_WORKER__MEMORY_LIMIT)."
    )
    assert hist.run([("A", "south")], T3, max_open_versions=2).versions_opened == 1


def test_a_write_that_would_leave_too_many_open_versions_stages_nothing(hist: Hist) -> None:
    hist.run([("A", "north"), ("B", "north")], T1, first=True)
    parquet = hist.files(".parquet")

    with (
        mock.patch.object(service_module, "stage_history", side_effect=AssertionError),
        pytest.raises(ValidationError) as info,
    ):
        hist.run([("C", "north"), ("D", "north")], T2, max_open_versions=3, advice=SQL_ADVICE)

    assert str(info.value) == (
        "gold.dim_customer would have 4 open versions after this write, more than "
        "LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS (3) allows. A history write holds every open "
        "version in the worker's memory, roughly 2 GiB per million open versions of 8 columns "
        "and more for wider rows, so nothing was written. Ask an operator to raise "
        "LAKANA_RUN__MAX_HISTORY_OPEN_VERSIONS together with the worker's memory limit "
        "(LAKANA_WORKER__MEMORY_LIMIT)."
    )
    assert hist.files(".parquet") == parquet
    replaced = hist.run([("C", "north")], T2, max_open_versions=2, close_missing=True)
    assert (replaced.versions_opened, replaced.versions_closed) == (1, 2)
