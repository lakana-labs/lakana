"""Merge: update the rows whose key matches, insert the rest, in one governed commit."""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList, _config
from pyiceberg.catalog.noop import NoopCatalog
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.table.update import AddSnapshotUpdate
from pyiceberg.types import LongType, NestedField, StringType, TimestampType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    BoundTask,
    PrincipalRef,
    SecurableKind,
    TaskScope,
)
from lakana_core.catalog import merge as merge_module
from lakana_core.catalog import service as service_module
from lakana_core.catalog.merge import (
    COMPOSITE_KEY_MAX_ROWS,
    SQL_ADVICE,
    MergeResult,
    check_merge_batch,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.writes import SnapshotPin
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEvent, AuditEventType, Grant, LineageEdge
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService
from lakana_core.runs.lockdown import PROTECTED_MESSAGE
from lakana_core.storage.resolver import StorageResolver

T0 = datetime(2026, 10, 1, 8, 0)
T1 = datetime(2026, 10, 2, 9, 30)

_ARROW = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False),
        pa.field("name", pa.string()),
        pa.field("updated_at", pa.timestamp("us")),
    ]
)

Row = tuple[int | None, str | None, datetime | None]


@dataclasses.dataclass
class Cat:
    """A file-backed environment `dev` with `bronze.customers`, and helpers to read it.

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

    def ref(self, dotted: str) -> TableRef:
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
    def arrow(rows: Sequence[Row], *, nullable_id: bool = False) -> pa.Table:
        """Build rows of `bronze.customers`.

        Parameters
        ----------
        rows
            ``(id, name, updated_at)`` tuples.
        nullable_id
            Declare `id` nullable, to carry a null key.

        Returns
        -------
        pa.Table
            The rows.
        """
        schema = _ARROW.set(0, pa.field("id", pa.int64(), nullable=True)) if nullable_id else _ARROW
        columns = list(zip(*rows, strict=True)) if rows else [[], [], []]
        return pa.table(
            [
                pa.array(list(values), field.type)
                for values, field in zip(columns, schema, strict=True)
            ],
            schema=schema,
        )

    def append(self, dotted: str, rows: Sequence[Row]) -> int:
        """Append rows, as an earlier writer would have.

        Parameters
        ----------
        dotted
            The table.
        rows
            The rows.

        Returns
        -------
        int
            The new snapshot id.
        """
        snapshot_id = self.service.append(
            self.admin, self.ref(dotted), self.arrow(rows)
        ).snapshot_id
        assert snapshot_id is not None
        return snapshot_id

    def metadata(self, dotted: str) -> TableMetadata:
        """Read the table's current metadata.

        Parameters
        ----------
        dotted
            The table.

        Returns
        -------
        TableMetadata
            The metadata.
        """
        return self.service.load_table(self.admin, self.ref(dotted)).metadata

    def rows(self, dotted: str) -> list[Row]:
        """Read the table's current rows, ordered by id.

        Parameters
        ----------
        dotted
            The table.

        Returns
        -------
        list of tuple
            The rows.
        """
        loaded = self.service.load_table(self.admin, self.ref(dotted))
        file_io = StorageResolver(self.session).for_environment("dev").file_io
        static = StaticTable(
            identifier=("dev", loaded.metadata_location),
            metadata=loaded.metadata,
            metadata_location=loaded.metadata_location,
            io=file_io,
            catalog=NoopCatalog("dev"),
        )
        table = static.scan().to_arrow().sort_by("id")
        return [(r["id"], r["name"], r["updated_at"]) for r in table.to_pylist()]

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

    def metadata_location(self, dotted: str) -> str:
        """Read the table's metadata pointer.

        Parameters
        ----------
        dotted
            The table.

        Returns
        -------
        str
            The location.
        """
        return self.service.load_table(self.admin, self.ref(dotted)).metadata_location

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


@pytest.fixture
def cat(catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path) -> Cat:
    """Create `dev` with `bronze.customers(id long not null, name string, updated_at timestamp)`.

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
    Cat
        The helpers.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    helper = Cat(service=catalog, admin=principal, session=session, root=tmp_path)
    catalog.create_table(
        principal,
        helper.ref("bronze.customers"),
        schema=Schema(
            NestedField(1, "id", LongType(), required=True),
            NestedField(2, "name", StringType(), required=False),
            NestedField(3, "updated_at", TimestampType(), required=False),
        ),
    )
    return helper


def test_merge_updates_matching_keys_and_inserts_the_rest_in_one_commit(cat: Cat) -> None:
    """Existing ids are replaced, new ids inserted, nothing deleted; one commit."""
    cat.append("bronze.customers", [(1, "a", T0), (2, "b", T0)])

    with mock.patch.object(cat.service, "commit_table", wraps=cat.service.commit_table) as commit:
        result = cat.service.merge(
            cat.admin,
            cat.ref("bronze.customers"),
            cat.arrow([(2, "B", T1), (3, "c", T1)]),
            key=["id"],
        )

    assert (result.rows_inserted, result.rows_updated, result.created) == (1, 1, False)
    assert cat.rows("bronze.customers") == [(1, "a", T0), (2, "B", T1), (3, "c", T1)]
    assert commit.call_count == 1
    assert cat.metadata("bronze.customers").current_snapshot_id == result.snapshot_id


def test_a_reader_sees_the_old_or_the_new_snapshot_only(cat: Cat) -> None:
    """The merge adds one snapshot, whose parent is the old one; nothing in between exists.

    pyiceberg stages an update as three snapshots (delete the matched rows, append the
    updated ones, append the new ones); the first is a table without the updated rows. None of
    them may be reachable: not as `main`, not in the snapshot log, not as an ancestor.
    """
    before_id = cat.append("bronze.customers", [(1, "a", T0), (2, "b", T0)])
    before = cat.metadata("bronze.customers")

    result = cat.service.merge(
        cat.admin,
        cat.ref("bronze.customers"),
        cat.arrow([(2, "B", T1), (3, "c", T1)]),
        key=["id"],
    )

    after = cat.metadata("bronze.customers")
    old_ids = {s.snapshot_id for s in before.snapshots}
    assert {s.snapshot_id for s in after.snapshots} == old_ids | {result.snapshot_id}
    assert [entry.snapshot_id for entry in after.snapshot_log] == [
        *(entry.snapshot_id for entry in before.snapshot_log),
        result.snapshot_id,
    ]
    new = after.snapshot_by_id(result.snapshot_id)  # ty: ignore[invalid-argument-type]
    assert new is not None
    assert new.parent_snapshot_id == before_id
    assert after.refs["main"].snapshot_id == result.snapshot_id


def test_a_merge_leaves_no_file_that_no_snapshot_names(cat: Cat) -> None:
    """The files of pyiceberg's intermediate snapshots are deleted once the merge commits."""
    cat.append("bronze.customers", [(1, "a", T0), (2, "b", T0)])

    cat.service.merge(
        cat.admin,
        cat.ref("bronze.customers"),
        cat.arrow([(2, "B", T1), (3, "c", T1)]),
        key=["id"],
    )

    metadata = cat.metadata("bronze.customers")
    file_io = StorageResolver(cat.session).for_environment("dev").file_io
    named_lists = {s.manifest_list for s in metadata.snapshots}
    named_manifests: set[str] = set()
    named_data: set[str] = set()
    for snapshot in metadata.snapshots:
        for manifest in snapshot.manifests(file_io):
            named_manifests.add(manifest.manifest_path)
            for entry in manifest.fetch_manifest_entry(file_io, discard_deleted=False):
                named_data.add(entry.data_file.file_path)
    assert cat.files(".avro") == named_lists | named_manifests
    assert cat.files(".parquet") == named_data


def test_an_insert_only_merge_commits_one_snapshot(cat: Cat) -> None:
    """New keys only: one snapshot, the old rows kept."""
    before_id = cat.append("bronze.customers", [(1, "a", T0)])

    result = cat.service.merge(
        cat.admin, cat.ref("bronze.customers"), cat.arrow([(2, "b", T1)]), key=["id"]
    )

    assert (result.rows_inserted, result.rows_updated) == (1, 0)
    assert cat.rows("bronze.customers") == [(1, "a", T0), (2, "b", T1)]
    metadata = cat.metadata("bronze.customers")
    new = metadata.snapshot_by_id(result.snapshot_id)  # ty: ignore[invalid-argument-type]
    assert new is not None
    assert new.parent_snapshot_id == before_id
    assert len(metadata.snapshots) == 2


def test_a_merge_into_an_empty_table_inserts_every_row(cat: Cat) -> None:
    """A table without a snapshot gets its first one."""
    result = cat.service.merge(
        cat.admin, cat.ref("bronze.customers"), cat.arrow([(1, "a", T0)]), key=["id"]
    )

    assert (result.rows_inserted, result.rows_updated, result.created) == (1, 0, False)
    assert cat.rows("bronze.customers") == [(1, "a", T0)]


def test_unchanged_rows_commit_nothing(cat: Cat) -> None:
    """Rows equal to what the table holds change nothing: no snapshot, no audit event."""
    cat.append("bronze.customers", [(1, "a", T0)])
    location = cat.metadata_location("bronze.customers")
    writes = len(cat.events(AuditEventType.TABLE_WRITE))

    result = cat.service.merge(
        cat.admin, cat.ref("bronze.customers"), cat.arrow([(1, "a", T0)]), key=["id"]
    )

    table_id = cat.service.load_table(cat.admin, cat.ref("bronze.customers")).uuid
    assert result == MergeResult(
        snapshot_id=None, rows_inserted=0, rows_updated=0, table_id=table_id
    )
    assert cat.metadata_location("bronze.customers") == location
    assert len(cat.events(AuditEventType.TABLE_WRITE)) == writes


def test_zero_rows_commit_nothing(cat: Cat) -> None:
    """An empty batch is a no-op, after the checks."""
    location = cat.metadata_location("bronze.customers")

    result = cat.service.merge(cat.admin, cat.ref("bronze.customers"), cat.arrow([]), key=["id"])

    table_id = cat.service.load_table(cat.admin, cat.ref("bronze.customers")).uuid
    assert result == MergeResult(
        snapshot_id=None, rows_inserted=0, rows_updated=0, table_id=table_id
    )
    assert cat.metadata_location("bronze.customers") == location


def test_a_composite_key_matches_on_every_column(cat: Cat) -> None:
    """`(id, name)` is the key: a row with the same id and another name is a new row."""
    cat.append("bronze.customers", [(1, "a", T0)])

    result = cat.service.merge(
        cat.admin,
        cat.ref("bronze.customers"),
        cat.arrow([(1, "a", T1), (1, "z", T1)]),
        key=["id", "name"],
    )

    assert (result.rows_inserted, result.rows_updated) == (1, 1)
    assert sorted(cat.rows("bronze.customers")) == [(1, "a", T1), (1, "z", T1)]


def _nothing_written(cat: Cat, location: str, parquet: set[str]) -> None:
    """Assert the table's pointer and its data files are as they were.

    Parameters
    ----------
    cat
        The helpers.
    location
        The metadata pointer before.
    parquet
        The data files before.
    """
    assert cat.metadata_location("bronze.customers") == location
    assert cat.files(".parquet") == parquet


def test_a_null_key_is_refused_with_a_count(cat: Cat) -> None:
    """3 null ids: "key column `id` is null in 3 rows"; nothing is written."""
    cat.append("bronze.customers", [(1, "a", T0)])
    location, parquet = cat.metadata_location("bronze.customers"), cat.files(".parquet")
    data = cat.arrow(
        [(None, "x", T1), (2, "b", T1), (None, "y", T1), (None, "z", T1)], nullable_id=True
    )

    with pytest.raises(ValidationError) as info:
        cat.service.merge(cat.admin, cat.ref("bronze.customers"), data, key=["id"])

    assert str(info.value) == "key column `id` is null in 3 rows"
    _nothing_written(cat, location, parquet)


def test_a_repeated_key_is_refused_with_a_count_never_values(cat: Cat) -> None:
    """Ids 7 three times and 8 twice: 5 rows; neither 7 nor 8 in the text."""
    location, parquet = cat.metadata_location("bronze.customers"), cat.files(".parquet")
    data = cat.arrow([(7, "a", T0), (7, "b", T0), (8, "c", T0), (7, "d", T0), (8, "e", T0)])

    with pytest.raises(ValidationError) as info:
        cat.service.merge(cat.admin, cat.ref("bronze.customers"), data, key=["id"])

    assert str(info.value) == (
        "key `id` repeats in 5 rows of this batch; make it unique "
        "(for example with `unique(subset=['id'], keep='last')`)"
    )
    assert "7" not in str(info.value)
    assert "8" not in str(info.value)
    _nothing_written(cat, location, parquet)


def test_a_repeated_composite_key_names_every_column(cat: Cat) -> None:
    """A composite key is named in full, in the text and in the `unique` hint."""
    data = cat.arrow([(1, "a", T0), (1, "a", T1), (1, "b", T0)])

    with pytest.raises(ValidationError) as info:
        cat.service.merge(cat.admin, cat.ref("bronze.customers"), data, key=["id", "name"])

    assert str(info.value) == (
        "key `id`, `name` repeats in 2 rows of this batch; make it unique "
        "(for example with `unique(subset=['id', 'name'], keep='last')`)"
    )


def test_a_key_repeated_in_the_table_is_refused_with_a_count(cat: Cat) -> None:
    """Two rows of the table share the id the batch updates: refused, never collapsed."""
    cat.append("bronze.customers", [(5, "a", T0), (5, "b", T0), (6, "c", T0)])
    location, parquet = cat.metadata_location("bronze.customers"), cat.files(".parquet")

    with pytest.raises(ValidationError) as info:
        cat.service.merge(
            cat.admin, cat.ref("bronze.customers"), cat.arrow([(5, "z", T1)]), key=["id"]
        )

    message = str(info.value)
    assert message.startswith(
        "key `id` repeats in 2 rows of bronze.customers that this batch matches; "
    )
    assert "GROUP BY id HAVING count(*) > 1" in message
    assert "5" not in message.replace("bronze.customers", "")
    _nothing_written(cat, location, parquet)


def test_a_key_repeated_in_the_table_but_not_matched_does_not_stop_a_merge(cat: Cat) -> None:
    """Only the keys the batch matches must be unique in the table."""
    cat.append("bronze.customers", [(5, "a", T0), (5, "b", T0)])

    result = cat.service.merge(
        cat.admin, cat.ref("bronze.customers"), cat.arrow([(6, "c", T1)]), key=["id"]
    )

    assert (result.rows_inserted, result.rows_updated) == (1, 0)


def test_a_composite_key_repeated_in_the_table_counts_exact_matches_only(cat: Cat) -> None:
    """(1, b) twice is refused only when the batch has (1, b), not for (1, a) and (2, b)."""
    cat.append("bronze.customers", [(1, "b", T0), (1, "b", T0), (2, "a", T0)])
    ref = cat.ref("bronze.customers")

    result = cat.service.merge(
        cat.admin, ref, cat.arrow([(1, "a", T1), (2, "b", T1)]), key=["id", "name"]
    )
    with pytest.raises(ValidationError) as info:
        cat.service.merge(cat.admin, ref, cat.arrow([(1, "b", T1)]), key=["id", "name"])

    assert (result.rows_inserted, result.rows_updated) == (2, 0)
    assert str(info.value).startswith(
        "key `id`, `name` repeats in 2 rows of bronze.customers that this batch matches; "
    )


@pytest.mark.parametrize(
    ("key", "message"),
    [
        (["customer_id"], "key column `customer_id` is not a column of bronze.customers"),
        ([], "A merge needs at least one key column."),
        (["id", "id"], "key column `id` is named twice"),
    ],
)
def test_a_bad_key_is_refused(cat: Cat, key: list[str], message: str) -> None:
    """A key the table does not have, no key, or a column named twice."""
    with pytest.raises(ValidationError) as info:
        cat.service.merge(
            cat.admin, cat.ref("bronze.customers"), cat.arrow([(1, "a", T0)]), key=key
        )

    assert str(info.value) == message


def test_a_key_column_missing_from_the_data_is_refused() -> None:
    """The data must carry every key column the table has."""
    data = pa.table({"name": ["a"]})

    with pytest.raises(ValidationError) as info:
        check_merge_batch(data, ["id"], table_columns=["id", "name"], table_name="bronze.customers")

    assert str(info.value) == "key column `id` is not a column of the rows sent"


def test_a_nested_key_column_is_refused() -> None:
    """A list or struct cannot be a key."""
    data = pa.table({"tags": pa.array([["a"]], pa.list_(pa.string()))})

    with pytest.raises(ValidationError) as info:
        check_merge_batch(data, ["tags"], table_columns=["tags"], table_name="bronze.customers")

    assert str(info.value) == (
        "key column `tags` has type list<item: string>; a key column must hold single values "
        "(a number, a string, a date or a timestamp)"
    )


def test_a_schema_mismatch_is_refused_by_column(cat: Cat) -> None:
    """The batch must fit the table, as for an append."""
    data = pa.table({"id": pa.array([1], pa.int64()), "name": ["a"]}).cast(
        pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("name", pa.string())])
    )

    with pytest.raises(ValidationError, match="updated_at"):
        cat.service.merge(cat.admin, cat.ref("bronze.customers"), data, key=["id"])


def test_the_first_merge_creates_the_table(cat: Cat) -> None:
    """A missing table with expected_snapshot_id="none" is created with the rows."""
    ref = cat.ref("bronze.accounts")

    result = cat.service.merge(
        cat.admin,
        ref,
        cat.arrow([(1, "a", T0), (2, "b", T0)]),
        key=["id"],
        expected_snapshot_id="none",
    )

    assert (result.rows_inserted, result.rows_updated, result.created) == (2, 0, True)
    assert cat.rows("bronze.accounts") == [(1, "a", T0), (2, "b", T0)]
    assert result.snapshot_id == cat.metadata("bronze.accounts").current_snapshot_id
    (write,) = [
        e for e in cat.events(AuditEventType.TABLE_WRITE) if e.detail["target"] == ref.dotted()
    ]
    assert write.detail["mode"] == "create"


def test_the_first_merge_checks_the_batch_before_creating(cat: Cat) -> None:
    """A repeated key refuses the create too; the table is not created."""
    ref = cat.ref("bronze.accounts")

    with pytest.raises(ValidationError, match="repeats in 2 rows"):
        cat.service.merge(
            cat.admin,
            ref,
            cat.arrow([(1, "a", T0), (1, "b", T0)]),
            key=["id"],
            expected_snapshot_id="none",
        )

    with pytest.raises(NotFoundError):
        cat.service.load_table(cat.admin, ref)


def test_creating_by_merge_needs_create_table(cat: Cat) -> None:
    """`WRITE_TABLE` alone may merge into a table, not create one."""
    allow = ActionAllowList(Action.WRITE_TABLE, Action.READ_TABLE)
    limited = CatalogService(cat.session, StorageResolver(cat.session), allow)

    with pytest.raises(PermissionDenied):
        limited.merge(
            cat.admin,
            cat.ref("bronze.accounts"),
            cat.arrow([(1, "a", T0)]),
            key=["id"],
            expected_snapshot_id="none",
        )

    assert [action for action, _ in allow.seen] == [
        Action.WRITE_TABLE,
        Action.READ_TABLE,
        Action.CREATE_TABLE,
    ]


def test_a_missing_table_without_none_is_not_found(cat: Cat) -> None:
    """Without `none` a merge never creates."""
    with pytest.raises(NotFoundError):
        cat.service.merge(
            cat.admin, cat.ref("bronze.accounts"), cat.arrow([(1, "a", T0)]), key=["id"]
        )


def test_merge_needs_write_table(cat: Cat) -> None:
    """Without `WRITE_TABLE` nothing is read or written."""
    allow = ActionAllowList(Action.READ_TABLE)
    limited = CatalogService(cat.session, StorageResolver(cat.session), allow)

    with pytest.raises(PermissionDenied):
        limited.merge(cat.admin, cat.ref("bronze.customers"), cat.arrow([(1, "a", T0)]), key=["id"])

    assert [action for action, _ in allow.seen] == [Action.WRITE_TABLE]


def test_a_stale_pin_is_a_conflict(cat: Cat) -> None:
    """expected_snapshot_id of an older snapshot: ConflictError; nothing written."""
    first = cat.append("bronze.customers", [(1, "a", T0)])
    cat.append("bronze.customers", [(2, "b", T0)])
    location, parquet = cat.metadata_location("bronze.customers"), cat.files(".parquet")

    with pytest.raises(ConflictError, match="changed"):
        cat.service.merge(
            cat.admin,
            cat.ref("bronze.customers"),
            cat.arrow([(1, "z", T1)]),
            key=["id"],
            expected_snapshot_id=first,
        )

    _nothing_written(cat, location, parquet)


def test_the_current_pin_commits(cat: Cat) -> None:
    """expected_snapshot_id of the current snapshot commits."""
    current = cat.append("bronze.customers", [(1, "a", T0)])

    result = cat.service.merge(
        cat.admin,
        cat.ref("bronze.customers"),
        cat.arrow([(1, "z", T1)]),
        key=["id"],
        expected_snapshot_id=current,
    )

    assert result.rows_updated == 1


def test_a_commit_that_fails_deletes_every_file_the_merge_wrote(cat: Cat) -> None:
    """Another writer wins the swap: a conflict, and no file of the merge is left."""
    cat.append("bronze.customers", [(1, "a", T0), (2, "b", T0)])
    location, parquet = cat.metadata_location("bronze.customers"), cat.files(".parquet")
    avro = cat.files(".avro")

    with (
        mock.patch.object(cat.service, "commit_table", side_effect=ConflictError("lost")),
        pytest.raises(ConflictError, match="changed"),
    ):
        cat.service.merge(
            cat.admin,
            cat.ref("bronze.customers"),
            cat.arrow([(2, "B", T1), (3, "c", T1)]),
            key=["id"],
        )

    _nothing_written(cat, location, parquet)
    assert cat.files(".avro") == avro
    assert cat.events(AuditEventType.TABLE_WRITE) == []


def test_merge_records_table_write_with_counts(cat: Cat) -> None:
    """TABLE_WRITE says merge, the target, the snapshot, both counts and the key."""
    cat.append("bronze.customers", [(1, "a", T0)])

    result = cat.service.merge(
        cat.admin,
        cat.ref("bronze.customers"),
        cat.arrow([(1, "A", T1), (2, "b", T1), (3, "c", T1)]),
        key=["id"],
    )

    event = cat.events(AuditEventType.TABLE_WRITE)[-1]
    assert {k: event.detail[k] for k in ("mode", "target", "snapshot_id", "key")} == {
        "mode": "merge",
        "target": "bronze.customers",
        "snapshot_id": result.snapshot_id,
        "key": ["id"],
    }
    assert (event.detail["rows_inserted"], event.detail["rows_updated"]) == (2, 1)
    assert event.detail["rows"] == 3
    assert event.principal_id == cat.admin.id


def test_merge_records_a_lineage_edge_per_source(cat: Cat) -> None:
    """A declared source gets an edge to the merge's snapshot."""
    cat.service.create_table(
        cat.admin,
        cat.ref("bronze.raw"),
        schema=Schema(NestedField(1, "id", LongType(), required=True)),
    )

    result = cat.service.merge(
        cat.admin,
        cat.ref("bronze.customers"),
        cat.arrow([(1, "a", T0)]),
        key=["id"],
        sources=["bronze.raw"],
    )

    edges = cat.session.scalars(select(LineageEdge)).all()
    assert [(e.source_name, e.target_name, e.target_snapshot_id) for e in edges] == [
        ("bronze.raw", "bronze.customers", result.snapshot_id)
    ]


def test_merge_into_a_protected_environment_needs_break_glass(cat: Cat) -> None:
    """Outside a run, a protected environment refuses a merge as it refuses an append."""
    cat.append("bronze.customers", [(1, "a", T0)])
    cat.service.set_environment_protection(cat.admin, "dev", True)

    with pytest.raises(PermissionDenied) as info:
        cat.service.merge(
            cat.admin, cat.ref("bronze.customers"), cat.arrow([(1, "z", T1)]), key=["id"]
        )

    assert str(info.value) == PROTECTED_MESSAGE.format(environment="dev")
    (denied,) = cat.events(AuditEventType.PROTECTED_WRITE_DENIED)
    assert denied.detail["operation"] == "merge"
    glass = dataclasses.replace(cat.admin, break_glass_reason="incident 9: fix a customer row")
    result = cat.service.merge(
        glass, cat.ref("bronze.customers"), cat.arrow([(1, "z", T1)]), key=["id"]
    )
    assert result.rows_updated == 1


def test_merge_needs_read_table(cat: Cat) -> None:
    """A merge compares the batch with the table's rows, so it needs READ_TABLE as well."""
    allow = ActionAllowList(Action.WRITE_TABLE)
    limited = CatalogService(cat.session, StorageResolver(cat.session), allow)
    cat.append("bronze.customers", [(1, "a", T0)])
    location = cat.metadata_location("bronze.customers")

    with pytest.raises(PermissionDenied):
        limited.merge(cat.admin, cat.ref("bronze.customers"), cat.arrow([(1, "z", T1)]), key=["id"])

    assert [action for action, _ in allow.seen] == [Action.WRITE_TABLE, Action.READ_TABLE]
    assert cat.metadata_location("bronze.customers") == location


def test_a_reader_and_writer_without_a_policy_may_merge(cat: Cat) -> None:
    """WRITE_TABLE and READ_TABLE, no row filter or column mask: the merge commits."""
    allow = ActionAllowList(Action.WRITE_TABLE, Action.READ_TABLE)
    limited = CatalogService(cat.session, StorageResolver(cat.session), allow)
    cat.append("bronze.customers", [(1, "a", T0)])

    result = limited.merge(
        cat.admin, cat.ref("bronze.customers"), cat.arrow([(1, "z", T1), (2, "b", T1)]), key=["id"]
    )

    assert (result.rows_inserted, result.rows_updated) == (1, 1)


_HIDDEN = (
    "Merges into bronze.customers are refused while a row filter or column mask applies to you "
    "on it: a merge compares your rows with rows you can't see. Ask someone the table is not "
    "filtered or masked for to run it, or ask an owner to lift the filter or mask for you."
)


def test_a_writer_with_a_row_filter_may_not_merge(cat: Cat) -> None:
    """The counts and the refusals would tell rows the filter hides; nothing is written."""
    cat.append("bronze.customers", [(1, "a", T0), (5, "e", T0)])
    location = cat.metadata_location("bronze.customers")
    RowFilterService(cat.session, AllowAllAuthorization(), cat.service).create(
        cat.admin,
        principal="admin",
        environment="dev",
        namespace="bronze",
        table="customers",
        expression="id < 3",
    )

    for call in (
        lambda: cat.service.check_can_merge(cat.admin, cat.ref("bronze.customers")),
        lambda: cat.service.merge(
            cat.admin, cat.ref("bronze.customers"), cat.arrow([(5, "z", T1)]), key=["id"]
        ),
    ):
        with pytest.raises(ValidationError) as info:
            call()
        assert str(info.value) == _HIDDEN
    assert cat.metadata_location("bronze.customers") == location


def test_a_writer_with_a_column_mask_may_not_merge(cat: Cat) -> None:
    """A masked column would be compared with values the writer can't see."""
    cat.append("bronze.customers", [(1, "a", T0)])
    ColumnMaskService(cat.session, AllowAllAuthorization(), cat.service).create(
        cat.admin,
        principal="admin",
        environment="dev",
        namespace="bronze",
        table="customers",
        column="name",
        expression="'***'",
    )

    with pytest.raises(ValidationError) as info:
        cat.service.merge(
            cat.admin, cat.ref("bronze.customers"), cat.arrow([(1, "a", T1)]), key=["id"]
        )

    assert str(info.value) == _HIDDEN


def _many(count: int) -> list[Row]:
    """Build `count` distinct rows.

    Parameters
    ----------
    count
        How many.

    Returns
    -------
    list of tuple
        Rows with ids from 0.
    """
    return [(i, "n", T0) for i in range(count)]


def test_a_composite_key_takes_at_most_the_cap(cat: Cat) -> None:
    """Exactly the cap merges on a key of two columns."""
    result = cat.service.merge(
        cat.admin,
        cat.ref("bronze.customers"),
        cat.arrow(_many(COMPOSITE_KEY_MAX_ROWS)),
        key=["id", "name"],
    )

    assert result.rows_inserted == COMPOSITE_KEY_MAX_ROWS


def test_a_composite_key_over_the_cap_is_refused_before_staging(cat: Cat) -> None:
    """One row over the cap: refused with the cap, the batch size and the advice."""
    location = cat.metadata_location("bronze.customers")

    with (
        mock.patch.object(service_module, "stage_upsert") as staged,
        pytest.raises(ValidationError) as info,
    ):
        cat.service.merge(
            cat.admin,
            cat.ref("bronze.customers"),
            cat.arrow(_many(COMPOSITE_KEY_MAX_ROWS + 1)),
            key=["id", "name"],
        )

    assert str(info.value) == (
        "A merge on a key of several columns takes at most 5,000 rows per batch, and this "
        "batch has 5,001. Merge on one key column (a column that holds the whole key), or send "
        "batches of at most 5,000 rows."
    )
    staged.assert_not_called()
    assert cat.metadata_location("bronze.customers") == location


def test_a_single_column_key_has_no_cap(cat: Cat) -> None:
    """One key column: far over the composite cap is fine."""
    result = cat.service.merge(
        cat.admin,
        cat.ref("bronze.customers"),
        cat.arrow(_many(COMPOSITE_KEY_MAX_ROWS * 3)),
        key=["id"],
    )

    assert result.rows_inserted == COMPOSITE_KEY_MAX_ROWS * 3


def _named_files(cat: Cat) -> tuple[set[str], set[str]]:
    """List the files the table's snapshots name.

    Parameters
    ----------
    cat
        The helpers.

    Returns
    -------
    tuple of set
        The manifest lists and manifests, and the data files.
    """
    metadata = cat.metadata("bronze.customers")
    file_io = StorageResolver(cat.session).for_environment("dev").file_io
    avro = {s.manifest_list for s in metadata.snapshots}
    parquet: set[str] = set()
    for snapshot in metadata.snapshots:
        for manifest in snapshot.manifests(file_io):
            avro.add(manifest.manifest_path)
            for entry in manifest.fetch_manifest_entry(file_io, discard_deleted=False):
                parquet.add(entry.data_file.file_path)
    return avro, parquet


def test_a_storage_error_while_folding_deletes_the_staged_files(cat: Cat) -> None:
    """The fold fails after pyiceberg wrote its snapshots: none of their files is left."""
    cat.append("bronze.customers", [(1, "a", T0), (2, "b", T0)])
    real = merge_module._live_files
    calls = {"n": 0}

    def failing(metadata: TableMetadata, file_io: object) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("storage went away")
        return real(metadata, file_io)  # ty: ignore[invalid-argument-type]

    with (
        mock.patch.object(merge_module, "_live_files", side_effect=failing),
        pytest.raises(OSError, match="storage went away"),
    ):
        cat.service.merge(
            cat.admin,
            cat.ref("bronze.customers"),
            cat.arrow([(2, "B", T1), (3, "c", T1)]),
            key=["id"],
        )

    avro, parquet = _named_files(cat)
    assert calls["n"] == 2
    assert cat.files(".avro") == avro
    assert cat.files(".parquet") == parquet


def test_a_concurrent_writer_wins_and_the_merge_conflicts(cat: Cat) -> None:
    """Another writer appends while the merge stages: a conflict, its rows intact, no orphan."""
    cat.append("bronze.customers", [(1, "a", T0), (2, "b", T0)])
    other = CatalogService(cat.session, StorageResolver(cat.session), AllowAllAuthorization())
    real = service_module.stage_upsert

    def racing(*args: object, **kwargs: object) -> object:
        staged = real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        other.append(cat.admin, cat.ref("bronze.customers"), cat.arrow([(9, "i", T0)]))
        return staged

    with (
        mock.patch.object(service_module, "stage_upsert", side_effect=racing),
        pytest.raises(ConflictError, match="changed during the write"),
    ):
        cat.service.merge(
            cat.admin,
            cat.ref("bronze.customers"),
            cat.arrow([(2, "B", T1), (3, "c", T1)]),
            key=["id"],
        )

    assert cat.rows("bronze.customers") == [(1, "a", T0), (2, "b", T0), (9, "i", T0)]
    avro, parquet = _named_files(cat)
    assert cat.files(".avro") == avro
    assert cat.files(".parquet") == parquet


def test_cleaning_up_a_failed_staging_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    """A snapshot that cannot be listed loses its manifest list; a failed delete is logged."""
    snapshot = mock.Mock(manifest_list="file:///nowhere/snap-1.avro")
    tx = mock.Mock(_updates=[mock.Mock(spec=[]), mock.Mock(spec=AddSnapshotUpdate)])
    tx._updates[1].snapshot = snapshot
    file_io = mock.Mock()
    file_io.delete.side_effect = PermissionError("no")

    with mock.patch.object(merge_module, "new_data_file_paths", side_effect=OSError("gone")):
        merge_module._delete_staged(tx, file_io)

    file_io.delete.assert_called_once_with("file:///nowhere/snap-1.avro")
    assert "Could not list the files of a failed merge's staged snapshot: OSError" in caplog.text
    assert "cleaning up a failed merge: PermissionError" in caplog.text
    assert "nowhere" not in caplog.text


def test_a_key_column_sent_twice_is_refused() -> None:
    """Two columns of the same name in the rows: refused by name."""
    data = pa.table([pa.array([1]), pa.array([2])], names=["id", "id"])

    with pytest.raises(ValidationError) as info:
        check_merge_batch(data, ["id"], table_columns=["id"], table_name="bronze.customers")

    assert str(info.value) == "Column 'id' appears more than once in the data."


def test_a_table_without_a_snapshot_has_no_live_files(cat: Cat) -> None:
    """The fold's file listing of an empty table is empty."""
    file_io = StorageResolver(cat.session).for_environment("dev").file_io

    assert merge_module._live_files(cat.metadata("bronze.customers"), file_io) == {}


def test_a_table_created_by_someone_else_meanwhile_is_a_conflict(cat: Cat) -> None:
    """The first merge loses the create to another writer: a conflict, as for a replace."""
    with (
        mock.patch.object(cat.service, "write_batches", side_effect=AlreadyExistsError("taken")),
        pytest.raises(ConflictError, match="changed during the write"),
    ):
        cat.service.merge(
            cat.admin,
            cat.ref("bronze.accounts"),
            cat.arrow([(1, "a", T0)]),
            key=["id"],
            expected_snapshot_id="none",
        )


def test_a_refused_commit_is_raised_as_it_is_and_its_files_deleted(cat: Cat) -> None:
    """A refusal other than a conflict reaches the caller unchanged; nothing is left."""
    cat.append("bronze.customers", [(1, "a", T0), (2, "b", T0)])
    parquet, avro = cat.files(".parquet"), cat.files(".avro")

    with (
        mock.patch.object(cat.service, "commit_table", side_effect=ValidationError("refused")),
        pytest.raises(ValidationError, match=r"^refused$"),
    ):
        cat.service.merge(
            cat.admin, cat.ref("bronze.customers"), cat.arrow([(2, "B", T1)]), key=["id"]
        )

    assert (cat.files(".parquet"), cat.files(".avro")) == (parquet, avro)


def test_a_commit_that_landed_although_it_raised_is_reported_as_committed(cat: Cat) -> None:
    """The answer to the commit was lost: the table shows the merge, so it is kept."""
    cat.append("bronze.customers", [(1, "a", T0)])
    real = cat.service.commit_table

    def lost_answer(*args: object, **kwargs: object) -> object:
        real(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        raise ConnectionError("the answer was lost")

    with mock.patch.object(cat.service, "commit_table", side_effect=lost_answer):
        result = cat.service.merge(
            cat.admin, cat.ref("bronze.customers"), cat.arrow([(1, "z", T1)]), key=["id"]
        )

    assert result.rows_updated == 1
    assert cat.metadata("bronze.customers").current_snapshot_id == result.snapshot_id
    assert cat.rows("bronze.customers") == [(1, "z", T1)]


def _bound_task(
    cat: Cat, actions: tuple[Action, ...] | None = None
) -> tuple[CatalogService, PrincipalRef]:
    """Act as the bound task token of asset `bronze.customers`, which reads only `bronze.raw`.

    The run principal holds `DATA_ENGINEER` on the instance, or only `actions`; the token
    narrows that to the task.

    Parameters
    ----------
    cat
        The helpers.
    actions
        Grant the run principal only these actions, instead of the role.

    Returns
    -------
    tuple
        A catalog with real grant checks, and the principal as the token authenticates it.
    """
    cat.session.add_all(
        Grant(
            id=uuid.uuid4(),
            principal_id=cat.admin.id,
            role=role,
            action=action,
            kind=SecurableKind.INSTANCE,
            granted_by=cat.admin.id,
            granted_at=datetime.now(UTC),
        )
        for role, action in (
            [(Role.DATA_ENGINEER, None)] if actions is None else [(None, a) for a in actions]
        )
    )
    cat.session.commit()
    bound = BoundTask(
        task_run_id=uuid.uuid4(),
        claim=1,
        asset="bronze.customers",
        inputs=("bronze.raw",),
        readable=("bronze.raw",),
    )
    task = dataclasses.replace(
        cat.admin,
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4(), bound=bound),
    )
    governed = CatalogService(
        cat.session, StorageResolver(cat.session), GrantAuthorization(cat.session)
    )
    return governed, task


def test_a_task_merges_into_its_own_asset_without_reading_it(cat: Cat) -> None:
    """A bound token may not READ its own asset, yet merges into it: the one exception."""
    cat.append("bronze.customers", [(1, "a", T0)])
    governed, task = _bound_task(cat)

    result = governed.merge(
        task, cat.ref("bronze.customers"), cat.arrow([(1, "z", T1), (2, "b", T1)]), key=["id"]
    )

    assert (result.rows_inserted, result.rows_updated) == (1, 1)
    with pytest.raises(PermissionDenied, match="may read only its declared inputs"):
        governed.load_table(task, cat.ref("bronze.customers"))


def test_a_task_may_not_merge_into_another_table(cat: Cat) -> None:
    """The exemption is for the own asset only: another table stays refused."""
    cat.service.create_table(
        cat.admin,
        cat.ref("bronze.other"),
        schema=cat.metadata("bronze.customers").schema(),
    )
    governed, task = _bound_task(cat)

    with pytest.raises(PermissionDenied, match="may write only its own asset"):
        governed.merge(task, cat.ref("bronze.other"), cat.arrow([(1, "a", T0)]), key=["id"])


def test_a_task_of_a_filtered_principal_may_not_merge_into_its_own_asset(cat: Cat) -> None:
    """The policy refusal still runs, against the principal the task acts for."""
    cat.append("bronze.customers", [(1, "a", T0)])
    RowFilterService(cat.session, AllowAllAuthorization(), cat.service).create(
        cat.admin,
        principal="admin",
        environment="dev",
        namespace="bronze",
        table="customers",
        expression="id < 3",
    )
    governed, task = _bound_task(cat)

    with pytest.raises(ValidationError) as info:
        governed.merge(task, cat.ref("bronze.customers"), cat.arrow([(1, "z", T1)]), key=["id"])

    assert str(info.value) == _HIDDEN


def test_a_task_of_a_write_only_principal_may_not_merge_as_its_token_may_not(cat: Cat) -> None:
    """The token's input narrowing is waived on its own asset; the principal's READ is not."""
    cat.append("bronze.customers", [(1, "a", T0)])
    governed, task = _bound_task(cat, (Action.WRITE_TABLE,))
    location = cat.metadata_location("bronze.customers")
    as_itself = dataclasses.replace(task, task_scope=None)

    with pytest.raises(PermissionDenied) as by_task:
        governed.merge(task, cat.ref("bronze.customers"), cat.arrow([(1, "z", T1)]), key=["id"])
    with pytest.raises(PermissionDenied) as by_pat:
        governed.merge(
            as_itself, cat.ref("bronze.customers"), cat.arrow([(1, "z", T1)]), key=["id"]
        )

    assert str(by_task.value) == str(by_pat.value)
    assert "READ_TABLE" in str(by_task.value)
    assert cat.metadata_location("bronze.customers") == location


def test_a_task_of_a_reading_and_writing_principal_merges(cat: Cat) -> None:
    """READ_TABLE and WRITE_TABLE held by the principal: its task merges into its own asset."""
    cat.append("bronze.customers", [(1, "a", T0)])
    governed, task = _bound_task(cat, (Action.WRITE_TABLE, Action.READ_TABLE))

    result = governed.merge(
        task, cat.ref("bronze.customers"), cat.arrow([(1, "z", T1)]), key=["id"]
    )

    assert result.rows_updated == 1


def test_a_pin_of_another_table_or_snapshot_is_a_conflict_and_the_current_one_commits(
    cat: Cat,
) -> None:
    """A worker's pin names the table too: a namesake created again does not match."""
    current = cat.append("bronze.customers", [(1, "a", T0)])
    table_id = cat.service.load_table(cat.admin, cat.ref("bronze.customers")).uuid
    location, parquet = cat.metadata_location("bronze.customers"), cat.files(".parquet")
    for pin in (SnapshotPin(uuid.uuid4(), current), SnapshotPin(table_id, None)):
        with pytest.raises(ConflictError, match="changed"):
            cat.service.merge(
                cat.admin,
                cat.ref("bronze.customers"),
                cat.arrow([(1, "z", T1)]),
                key=["id"],
                pin=pin,
            )
        _nothing_written(cat, location, parquet)

    run_id = uuid.uuid4()
    result = cat.service.merge(
        cat.admin,
        cat.ref("bronze.customers"),
        cat.arrow([(1, "z", T1)]),
        key=["id"],
        pin=SnapshotPin(table_id, current),
        query_run_id=run_id,
        watermark="long:1",
    )

    assert (result.rows_updated, result.table_id) == (1, table_id)
    event = cat.events(AuditEventType.TABLE_WRITE)[-1]
    assert (event.detail["run_id"], event.detail["watermark"]) == (str(run_id), "long:1")


def test_a_sql_assets_repeated_key_is_refused_with_advice_for_its_select() -> None:
    """A SQL asset cannot call `unique`: the advice is a QUALIFY for its SELECT."""
    data = pa.table({"id": [1, 1, 2], "day": [1, 1, 1]})

    with pytest.raises(ValidationError) as info:
        check_merge_batch(
            data,
            ["id", "day"],
            table_columns=["id", "day"],
            table_name="silver.orders",
            advice=SQL_ADVICE,
        )

    assert str(info.value) == (
        "key `id`, `day` repeats in 2 rows of the asset's result; keep one row per key in its "
        "SELECT, for example with `QUALIFY ROW_NUMBER() OVER (PARTITION BY id, day ORDER BY "
        "<the column that says which row is newest> DESC) = 1`"
    )


def test_a_sql_assets_composite_key_over_the_cap_is_refused_with_advice_for_its_runs() -> None:
    """A SQL asset sends no batches: the advice is one key column, or a watermark."""
    rows = COMPOSITE_KEY_MAX_ROWS + 1
    data = pa.table({"id": list(range(rows)), "day": [1] * rows})

    with pytest.raises(ValidationError) as info:
        check_merge_batch(
            data,
            ["id", "day"],
            table_columns=["id", "day"],
            table_name="silver.orders",
            advice=SQL_ADVICE,
        )

    assert str(info.value) == (
        "A merge on a key of several columns takes at most 5,000 rows per run, and the asset's "
        "result has 5,001. Merge on one key column (a column that holds the whole key), or give "
        "the asset a `watermark:` so that each run merges only the rows changed since the last "
        "one; a first run reads every row, so load the history in parts of at most 5,000 rows."
    )
