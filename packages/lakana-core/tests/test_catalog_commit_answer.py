"""What the answer to a commit or a create shows of a table: its history only to a reader."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadataUtil, TableMetadataV2, TableMetadataV3
from pyiceberg.table.sorting import SortField, SortOrder
from pyiceberg.table.update import (
    AddPartitionSpecUpdate,
    AddSchemaUpdate,
    AddSnapshotUpdate,
    AddSortOrderUpdate,
    AssertRefSnapshotId,
    AssignUUIDUpdate,
    RemovePartitionSpecsUpdate,
    RemovePropertiesUpdate,
    RemoveSchemasUpdate,
    RemoveSnapshotRefUpdate,
    RemoveSnapshotsUpdate,
    SetCurrentSchemaUpdate,
    SetDefaultSortOrderUpdate,
    SetDefaultSpecUpdate,
    SetPropertiesUpdate,
    SetSnapshotRefUpdate,
)
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import LongType, NestedField
from sqlalchemy import update
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    BoundTask,
    PrincipalRef,
    RunContext,
    TaskScope,
)
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.config import StorageConfig
from lakana_core.errors import ConflictError, PermissionDenied, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import Table, WriteMode
from lakana_core.storage.resolver import StorageResolver

_HISTORY = ("snapshots", "refs", "current-snapshot-id", "snapshot-log", "metadata-log")
_FAILED_TAG = "lakana-check-failed-00000000-0000-0000-0000-0000000000a1"


def _ref(dotted: str = "gold.orders") -> TableRef:
    """Address a table in `dev`.

    Parameters
    ----------
    dotted
        The dotted name.

    Returns
    -------
    TableRef
        The reference.
    """
    return TableRef.from_dotted("dev", dotted)


@pytest.fixture
def admin(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` with `gold.orders`: two writes, a property and a failed check's tag.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

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
    rows = pa.table({"id": pa.array([1], pa.int64())})
    for mode in (WriteMode.CREATE, WriteMode.APPEND):
        catalog.write_batches(
            principal, _ref(), mode=mode, schema=rows.schema, batches=rows.to_batches()
        )
    first = min(
        catalog.load_table(principal, _ref()).metadata.snapshots, key=lambda s: s.timestamp_ms
    )
    catalog.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[SetPropertiesUpdate(updates={"owner": "finance"})],
    )
    # Only the rollback sets such a tag (the one setter a commit over REST is refused).
    catalog.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[
            SetSnapshotRefUpdate(ref_name=_FAILED_TAG, type="tag", snapshot_id=first.snapshot_id)
        ],
        internal_rollback=True,
    )
    return catalog


def _answer(
    session: Session,
    admin: CatalogService,
    p: PrincipalRef,
    authz: AllowAllAuthorization | ActionAllowList,
    dotted: str = "gold.orders",
) -> dict[str, Any]:
    """Commit nothing to a table as `p` and return the answer's metadata.

    Parameters
    ----------
    session
        Database session.
    admin
        The allow-all catalog, which loads the table.
    p
        The caller.
    authz
        The caller's authorization.
    dotted
        The table.

    Returns
    -------
    dict
        The metadata the answer carries.
    """
    catalog = CatalogService(session, StorageResolver(session), authz)
    loaded = catalog.commit_table(p, _ref(dotted), requirements=[], updates=[])
    return catalog.commit_answer(p, loaded)


def _bound(principal: PrincipalRef, asset: str, readable: tuple[str, ...] = ()) -> PrincipalRef:
    """Give a principal a bound task token's scope.

    Parameters
    ----------
    principal
        The run's principal.
    asset
        The task's own asset.
    readable
        What the task may read.

    Returns
    -------
    PrincipalRef
        The principal as a bound task token authenticates it.
    """
    return PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(
            environment="dev",
            asset_scope=[],
            token_id=uuid.uuid4(),
            bound=BoundTask(
                task_run_id=uuid.uuid4(), claim=1, asset=asset, inputs=readable, readable=readable
            ),
        ),
    )


def _reduced(metadata: dict[str, object]) -> None:
    """Assert that metadata shows nothing of the table's history and still parses.

    Parameters
    ----------
    metadata
        An answer's metadata.
    """
    for key in (*_HISTORY, "properties", "statistics", "partition-statistics", "last-updated-ms"):
        assert key not in metadata, key
    assert _FAILED_TAG not in str(metadata)
    parsed = TableMetadataUtil.parse_obj(metadata)
    assert parsed.current_snapshot_id is None
    assert parsed.snapshots == []


def test_a_writer_without_read_gets_no_history(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    metadata = _answer(session, admin, principal, ActionAllowList(Action.WRITE_TABLE))

    _reduced(metadata)
    full = admin.load_table(principal, _ref()).metadata
    assert metadata["table-uuid"] == str(full.table_uuid)
    assert metadata["location"] == full.location
    assert metadata["current-schema-id"] == full.current_schema_id
    assert isinstance(full, TableMetadataV2 | TableMetadataV3)
    assert metadata["last-sequence-number"] == full.last_sequence_number


def test_a_reader_gets_the_whole_metadata(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    metadata = _answer(session, admin, principal, AllowAllAuthorization())

    assert len(metadata["snapshots"]) == 2
    assert _FAILED_TAG in metadata["refs"]
    assert metadata["properties"]["owner"] == "finance"


def test_a_reader_of_a_table_that_is_not_published_gets_no_history(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    """The load would refuse it, so the answer shows no more than the load would."""
    loaded = admin.load_table(principal, _ref())
    session.execute(update(Table).values(unpublished_run_id=uuid.uuid4()))
    session.commit()

    _reduced(admin.commit_answer(principal, loaded))


def test_a_task_that_may_not_read_a_kept_table_gets_no_history(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    session.execute(update(Table).values(kept_run_id=uuid.uuid4()))
    session.commit()
    task = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        run_context=RunContext(task_run_id=uuid.uuid4(), claim=1),
    )
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    loaded = admin.load_table(principal, _ref())

    _reduced(catalog.commit_answer(task, loaded))
    assert "snapshots" in catalog.commit_answer(principal, loaded)


def test_a_bound_task_gets_no_history_of_its_own_asset(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    """It may write its asset and not read it: like any writer without READ."""
    writer = CatalogService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))
    loaded = admin.load_table(principal, _ref())

    _reduced(writer.commit_answer(_bound(principal, "gold.orders"), loaded))


def test_a_bound_task_with_read_gets_the_whole_metadata(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    loaded = admin.load_table(principal, _ref())
    reader = CatalogService(session, StorageResolver(session), AllowAllAuthorization())

    metadata = reader.commit_answer(_bound(principal, "gold.other", ("gold.orders",)), loaded)

    assert len(metadata["snapshots"]) == 2


def test_the_reduced_answer_holds_only_the_current_schema_spec_and_sort_order(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    """A column dropped from the table, for privacy say, is not named to a writer without READ."""
    current = admin.load_table(principal, _ref()).metadata.schema()
    admin.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[
            AddSchemaUpdate(
                schema_=Schema(
                    *current.fields,
                    NestedField(field_id=2, name="salary", field_type=LongType()),
                    schema_id=current.schema_id + 1,
                )
            ),
            SetCurrentSchemaUpdate(schema_id=-1),
        ],
    )
    admin.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[
            AddSchemaUpdate(schema_=Schema(*current.fields, schema_id=current.schema_id + 2)),
            SetCurrentSchemaUpdate(schema_id=-1),
        ],
    )

    metadata = _answer(session, admin, principal, ActionAllowList(Action.WRITE_TABLE))

    _reduced(metadata)
    assert [s["schema-id"] for s in metadata["schemas"]] == [metadata["current-schema-id"]]
    assert "salary" not in str(metadata)
    assert [s["spec-id"] for s in metadata["partition-specs"]] == [metadata["default-spec-id"]]
    assert [o["order-id"] for o in metadata["sort-orders"]] == [metadata["default-sort-order-id"]]
    assert len(admin.load_table(principal, _ref()).metadata.schemas) == 3


def test_the_answer_to_a_create_follows_the_same_rule(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    creator = CatalogService(
        session, StorageResolver(session), ActionAllowList(Action.CREATE_TABLE)
    )
    created: LoadedTable = creator.create_table(
        principal,
        _ref("gold.fresh"),
        schema=admin.load_table(principal, _ref()).metadata.schema(),
        properties={"owner": "finance"},
    )

    _reduced(creator.commit_answer(principal, created))
    assert admin.commit_answer(principal, created)["properties"] == {"owner": "finance"}


@pytest.mark.parametrize("ref", ["main", _FAILED_TAG, "lakana-check-failed-other"])
def test_a_failed_requirement_names_nothing_of_the_table_to_a_writer_without_read(
    session: Session, principal: PrincipalRef, admin: CatalogService, ref: str
) -> None:
    """The text is the same whatever the ref and whether it exists, and has no snapshot id.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    admin
        The allow-all catalog.
    ref
        The ref the requirement names, with a wrong snapshot id.
    """
    head = admin.load_table(principal, _ref()).metadata.current_snapshot_id
    writer = CatalogService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))
    requirement = AssertRefSnapshotId(ref=ref, snapshot_id=1)

    with pytest.raises(ConflictError) as refused:
        writer.commit_table(principal, _ref(), requirements=[requirement], updates=[])
    with pytest.raises(ConflictError) as detailed:
        admin.commit_table(principal, _ref(), requirements=[requirement], updates=[])

    assert str(refused.value) == (
        "The table changed while this write was in progress, so it was not applied. Run the "
        "write again; an Iceberg client has to load the table again first, which needs READ on "
        "the table."
    )
    assert str(detailed.value) != str(refused.value)
    assert ("found" in str(detailed.value)) is (ref != "lakana-check-failed-other")
    if ref == "main":
        assert str(head) in str(detailed.value)


_DOES_NOT_FIT = (
    "The updates do not fit the table's current metadata, so nothing was applied. The details "
    "need READ on the table."
)


def _writers(principal: PrincipalRef) -> list[PrincipalRef]:
    """Return the callers that may write `gold.orders` and not read it.

    Parameters
    ----------
    principal
        The principal behind both.

    Returns
    -------
    list of PrincipalRef
        A plain principal, and the same as a bound task token of `gold.orders` presents it.
    """
    return [principal, _bound(principal, "gold.orders")]


def test_an_update_that_does_not_fit_tells_a_writer_without_read_nothing(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    """A snapshot older than the table's last one: pyiceberg prints the stored sequence number."""
    metadata = admin.load_table(principal, _ref()).metadata
    oldest = min(metadata.snapshots, key=lambda s: s.timestamp_ms)
    update = AddSnapshotUpdate(
        snapshot=oldest.model_copy(
            update={
                "snapshot_id": 424_242,
                "sequence_number": 1,
                "parent_snapshot_id": oldest.snapshot_id,
            }
        )
    )
    writer = CatalogService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))
    assert isinstance(metadata, TableMetadataV2 | TableMetadataV3)
    stored = str(metadata.last_sequence_number)

    for caller in _writers(principal):
        with pytest.raises(ValidationError) as refused:
            writer.commit_table(caller, _ref(), requirements=[], updates=[update])
        assert str(refused.value) == _DOES_NOT_FIT
        assert stored not in str(refused.value)
    with pytest.raises(ValidationError) as detailed:
        admin.commit_table(principal, _ref(), requirements=[], updates=[update])

    assert f"older than last sequence number {stored}" in str(detailed.value)
    assert admin.load_table(principal, _ref()).metadata.snapshots == metadata.snapshots


def test_removing_a_property_tells_a_writer_without_read_nothing_of_whether_it_was_there(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    """Removing a key the table does not have is no change for such a caller, as in Iceberg."""
    writer = CatalogService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))

    for caller in _writers(principal):
        for key in ("owner", "nobody-set-this"):
            loaded = writer.commit_table(
                caller,
                _ref(),
                requirements=[],
                updates=[
                    SetPropertiesUpdate(updates={"touched": key}),
                    RemovePropertiesUpdate(removals=[key]),
                ],
            )
            assert key not in loaded.metadata.properties
            assert loaded.metadata.properties["touched"] == key
    with pytest.raises(ValidationError, match="not present"):
        admin.commit_table(
            principal,
            _ref(),
            requirements=[],
            updates=[RemovePropertiesUpdate(removals=["nobody-set-this"])],
        )


def test_the_refusal_to_publish_by_reference_tells_a_writer_without_read_nothing(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    """Whether a table is published is the load's to tell, and only to a reader."""
    first = min(
        admin.load_table(principal, _ref()).metadata.snapshots, key=lambda s: s.timestamp_ms
    )
    session.execute(update(Table).values(unpublished_run_id=uuid.uuid4()))
    session.commit()
    writer = CatalogService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))
    pointing = [SetSnapshotRefUpdate(ref_name="main", type="branch", snapshot_id=first.snapshot_id)]

    with pytest.raises(ValidationError) as refused:
        writer.commit_table(principal, _ref(), requirements=[], updates=pointing)
    with pytest.raises(ValidationError) as detailed:
        admin.commit_table(principal, _ref(), requirements=[], updates=pointing)

    assert str(refused.value) == _DOES_NOT_FIT
    assert "not published" in str(detailed.value)


def test_a_schema_change_that_reuses_a_schema_id_is_refused(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    """A dropped column must not come back through a schema id the table already has.

    The table evolved back to its first schema, so the current schema is not the highest. A
    client that sees only the current schema, as a writer without READ does, picks the
    current id plus one for its next schema, which is the id of the schema with `salary`.
    """
    first = admin.load_table(principal, _ref()).metadata.schema()
    salary = NestedField(field_id=2, name="salary", field_type=LongType())
    admin.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[
            AddSchemaUpdate(schema_=Schema(*first.fields, salary, schema_id=first.schema_id + 1)),
            SetCurrentSchemaUpdate(schema_id=-1),
        ],
    )
    admin.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[SetCurrentSchemaUpdate(schema_id=first.schema_id)],
    )
    region = NestedField(field_id=3, name="region", field_type=LongType())
    change = [
        AddSchemaUpdate(schema_=Schema(*first.fields, region, schema_id=first.schema_id + 1)),
        SetCurrentSchemaUpdate(schema_id=-1),
    ]
    writer = CatalogService(session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE))

    for catalog in (writer, admin):
        with pytest.raises(ValidationError) as refused:
            catalog.commit_table(principal, _ref(), requirements=[], updates=change)
        assert str(refused.value) == (
            "This schema change adds a schema under an id the table already uses, so nothing "
            "was applied. Load the table again and make the change on what the load gives; "
            "loading needs READ on the table."
        )

    current = admin.load_table(principal, _ref()).metadata.schema()
    assert [f.name for f in current.fields] == [f.name for f in first.fields]


def test_two_schemas_under_one_new_id_in_one_commit_are_refused(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    """The second would be found after the first by its id, and never become current."""
    first = admin.load_table(principal, _ref()).metadata.schema()
    fresh = first.schema_id + 1
    salary = NestedField(field_id=2, name="salary", field_type=LongType())
    region = NestedField(field_id=3, name="region", field_type=LongType())

    with pytest.raises(ValidationError, match="already uses"):
        admin.commit_table(
            principal,
            _ref(),
            requirements=[],
            updates=[
                AddSchemaUpdate(schema_=Schema(*first.fields, salary, schema_id=fresh)),
                AddSchemaUpdate(schema_=Schema(*first.fields, region, schema_id=fresh)),
                SetCurrentSchemaUpdate(schema_id=-1),
            ],
        )

    assert len(admin.load_table(principal, _ref()).metadata.schemas) == 1


_NO_REMOVAL = (
    "An Iceberg commit cannot remove snapshots or the tag of a failed check: running tasks and "
    "kept tags still need them. Expire snapshots with `lakana table maintain`, which keeps "
    "what they need."
)


def _callers(session: Session) -> dict[str, CatalogService]:
    """Build the catalogs of a writer without READ, a writer with READ and an administrator.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    dict
        The catalogs by a name for the test id.
    """
    return {
        "writer": CatalogService(
            session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE)
        ),
        "reader-writer": CatalogService(
            session,
            StorageResolver(session),
            ActionAllowList(Action.WRITE_TABLE, Action.READ_TABLE),
        ),
        "admin": CatalogService(session, StorageResolver(session), AllowAllAuthorization()),
    }


@pytest.mark.parametrize("who", ["writer", "reader-writer", "admin"])
def test_a_commit_over_rest_cannot_remove_snapshots(
    session: Session, principal: PrincipalRef, admin: CatalogService, who: str
) -> None:
    """Expiry is the job of maintenance, which spares what running tasks and kept tags need."""
    before = admin.load_table(principal, _ref()).metadata
    oldest = min(before.snapshots, key=lambda s: s.timestamp_ms)

    with pytest.raises(PermissionDenied) as refused:
        _callers(session)[who].commit_table(
            principal,
            _ref(),
            requirements=[],
            updates=[RemoveSnapshotsUpdate(snapshot_ids=[oldest.snapshot_id])],
        )

    assert str(refused.value) == _NO_REMOVAL
    assert admin.load_table(principal, _ref()).metadata.snapshots == before.snapshots


@pytest.mark.parametrize("who", ["writer", "reader-writer", "admin"])
def test_a_commit_over_rest_cannot_remove_the_tag_of_a_failed_check(
    session: Session, principal: PrincipalRef, admin: CatalogService, who: str
) -> None:
    before = admin.load_table(principal, _ref()).metadata

    with pytest.raises(PermissionDenied) as refused:
        _callers(session)[who].commit_table(
            principal,
            _ref(),
            requirements=[],
            updates=[RemoveSnapshotRefUpdate(ref_name=_FAILED_TAG)],
        )

    assert str(refused.value) == _NO_REMOVAL
    assert _FAILED_TAG in admin.load_table(principal, _ref()).metadata.refs
    assert admin.load_table(principal, _ref()).metadata.refs == before.refs


def test_the_refusal_to_remove_names_nothing_of_the_table(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    """The text is the same for a snapshot that exists, one that does not, and a tag."""
    writer = _callers(session)["writer"]
    texts = set()
    for update_ in (
        RemoveSnapshotsUpdate(snapshot_ids=[1]),
        RemoveSnapshotRefUpdate(ref_name="lakana-check-failed-nothing"),
    ):
        with pytest.raises(PermissionDenied) as refused:
            writer.commit_table(principal, _ref(), requirements=[], updates=[update_])
        texts.add(str(refused.value))

    assert texts == {_NO_REMOVAL}


def test_a_commit_over_rest_may_still_remove_a_ref_of_the_users(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    current = admin.load_table(principal, _ref()).metadata.current_snapshot_id
    assert current is not None
    writer = _callers(session)["reader-writer"]
    writer.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[SetSnapshotRefUpdate(ref_name="mine", type="branch", snapshot_id=current)],
    )
    assert "mine" in admin.load_table(principal, _ref()).metadata.refs

    writer.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[RemoveSnapshotRefUpdate(ref_name="mine")],
    )

    assert "mine" not in admin.load_table(principal, _ref()).metadata.refs


def _two_schemas(admin: CatalogService, principal: PrincipalRef) -> int:
    """Give `gold.orders` a second schema, with a column that a later one drops.

    Parameters
    ----------
    admin
        The allow-all catalog.
    principal
        The caller.

    Returns
    -------
    int
        The id of the schema with the column that is no longer there.
    """
    first = admin.load_table(principal, _ref()).metadata.schema()
    salary = NestedField(field_id=2, name="salary", field_type=LongType())
    admin.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[
            AddSchemaUpdate(schema_=Schema(*first.fields, salary, schema_id=first.schema_id + 1)),
            SetCurrentSchemaUpdate(schema_id=-1),
        ],
    )
    admin.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[
            AddSchemaUpdate(schema_=Schema(*first.fields, schema_id=first.schema_id + 2)),
            SetCurrentSchemaUpdate(schema_id=-1),
        ],
    )
    return first.schema_id + 1


_NEEDS_READ_FOR_SCHEMA = (
    "Making an earlier schema, partition spec or sort order current needs READ on the table. "
    "Add a new one and make that one current, or ask for READ."
)


def test_an_earlier_schema_cannot_be_made_current_without_read(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    """The reduced answer would name the columns that were dropped since."""
    with_salary = _two_schemas(admin, principal)
    before = admin.load_table(principal, _ref()).metadata
    writer = _callers(session)["writer"]

    for schema_id in (with_salary, 12345):
        with pytest.raises(ValidationError) as refused:
            writer.commit_table(
                principal,
                _ref(),
                requirements=[],
                updates=[SetCurrentSchemaUpdate(schema_id=schema_id)],
            )
        assert str(refused.value) == _NEEDS_READ_FOR_SCHEMA

    assert (
        admin.load_table(principal, _ref()).metadata.current_schema_id == before.current_schema_id
    )


@pytest.mark.parametrize("explicit", [-1, "added"])
def test_a_writer_without_read_can_add_a_schema_and_make_it_current(
    session: Session, principal: PrincipalRef, admin: CatalogService, explicit: int | str
) -> None:
    """Both forms name the schema of this commit: the last one added, or its id."""
    first = admin.load_table(principal, _ref()).metadata.schema()
    region = NestedField(field_id=3, name="region", field_type=LongType())
    new_id = first.schema_id + 1

    loaded = _callers(session)["writer"].commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[
            AddSchemaUpdate(schema_=Schema(*first.fields, region, schema_id=new_id)),
            SetCurrentSchemaUpdate(schema_id=new_id if explicit == "added" else -1),
        ],
    )

    assert loaded.metadata.current_schema_id == new_id


def test_a_reader_may_make_an_earlier_schema_current(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    with_salary = _two_schemas(admin, principal)

    loaded = _callers(session)["reader-writer"].commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[SetCurrentSchemaUpdate(schema_id=with_salary)],
    )

    assert loaded.metadata.current_schema_id == with_salary


_NO_FAILED_TAG_CHANGE = (
    "An Iceberg commit cannot set a ref named `lakana-check-failed-...`: Lakana keeps the "
    "snapshot of a failed check under such a name. Use another name for your own ref."
)
_NO_SCHEMA_OR_SPEC_REMOVAL = (
    "Lakana keeps every schema, partition spec and sort order a table has had, because its "
    "snapshots and a set-back may need them. Leave them in place; an unused one costs nothing."
)


@pytest.mark.parametrize("who", ["writer", "admin"])
@pytest.mark.parametrize(
    "ref_update",
    [
        pytest.param("move", id="move-the-tag"),
        pytest.param("new-tag", id="create-a-tag"),
        pytest.param("new-branch", id="create-a-branch"),
        pytest.param("branch-over-tag", id="branch-over-the-tag"),
    ],
)
def test_a_commit_over_rest_cannot_set_a_ref_named_like_the_tag_of_a_failed_check(
    session: Session, principal: PrincipalRef, admin: CatalogService, who: str, ref_update: str
) -> None:
    """Moving the tag lets its snapshot expire; a new one makes `maintain` expire any snapshot."""
    before = admin.load_table(principal, _ref()).metadata
    snapshots = sorted(before.snapshots, key=lambda s: s.timestamp_ms)
    old, head = snapshots[0].snapshot_id, snapshots[-1].snapshot_id
    name = "lakana-check-failed-00000000-0000-0000-0000-0000000000b2"
    update_ = {
        "move": SetSnapshotRefUpdate(ref_name=_FAILED_TAG, type="tag", snapshot_id=head),
        "new-tag": SetSnapshotRefUpdate(ref_name=name, type="tag", snapshot_id=old),
        "new-branch": SetSnapshotRefUpdate(ref_name=name, type="branch", snapshot_id=old),
        "branch-over-tag": SetSnapshotRefUpdate(
            ref_name=_FAILED_TAG, type="branch", snapshot_id=head
        ),
    }[ref_update]

    with pytest.raises(PermissionDenied) as refused:
        _callers(session)[who].commit_table(principal, _ref(), requirements=[], updates=[update_])

    assert str(refused.value) == _NO_FAILED_TAG_CHANGE
    assert admin.load_table(principal, _ref()).metadata.refs == before.refs


def test_a_commit_over_rest_may_set_any_other_ref(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    current = admin.load_table(principal, _ref()).metadata.current_snapshot_id
    assert current is not None

    _callers(session)["writer"].commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[SetSnapshotRefUpdate(ref_name="mine", type="tag", snapshot_id=current)],
    )

    assert "mine" in admin.load_table(principal, _ref()).metadata.refs


@pytest.mark.parametrize("who", ["writer", "admin"])
def test_a_commit_over_rest_cannot_change_the_uuid_of_a_table(
    session: Session, principal: PrincipalRef, admin: CatalogService, who: str
) -> None:
    """Every later write and the set-back require the table's uuid."""
    before = admin.load_table(principal, _ref()).metadata

    with pytest.raises(ValidationError, match="UUID cannot change"):
        _callers(session)[who].commit_table(
            principal,
            _ref(),
            requirements=[],
            updates=[AssignUUIDUpdate(uuid=uuid.uuid4())],
        )

    assert admin.load_table(principal, _ref()).metadata.table_uuid == before.table_uuid


def test_assigning_the_uuid_the_table_has_changes_nothing(
    session: Session, principal: PrincipalRef, admin: CatalogService
) -> None:
    table_uuid = admin.load_table(principal, _ref()).metadata.table_uuid

    loaded = _callers(session)["writer"].commit_table(
        principal, _ref(), requirements=[], updates=[AssignUUIDUpdate(uuid=table_uuid)]
    )

    assert loaded.metadata.table_uuid == table_uuid


@pytest.mark.parametrize("who", ["writer", "admin"])
@pytest.mark.parametrize("removal", ["schemas", "specs"])
def test_a_commit_over_rest_cannot_remove_schemas_or_partition_specs(
    session: Session, principal: PrincipalRef, admin: CatalogService, who: str, removal: str
) -> None:
    with_salary = _two_schemas(admin, principal)
    before = admin.load_table(principal, _ref()).metadata
    update_ = (
        RemoveSchemasUpdate(schema_ids=[with_salary])
        if removal == "schemas"
        else RemovePartitionSpecsUpdate(spec_ids=[0])
    )

    with pytest.raises(PermissionDenied) as refused:
        _callers(session)[who].commit_table(principal, _ref(), requirements=[], updates=[update_])

    assert str(refused.value) == _NO_SCHEMA_OR_SPEC_REMOVAL
    after = admin.load_table(principal, _ref()).metadata
    assert (after.schemas, after.partition_specs) == (before.schemas, before.partition_specs)


def _two_layouts(admin: CatalogService, principal: PrincipalRef) -> None:
    """Give `gold.orders` a second partition spec and sort order, each made the default.

    Parameters
    ----------
    admin
        The allow-all catalog.
    principal
        The caller.
    """
    spec = PartitionSpec(
        PartitionField(source_id=1, field_id=1000, transform=IdentityTransform(), name="id"),
        spec_id=1,
    )
    order = SortOrder(SortField(source_id=1, transform=IdentityTransform()), order_id=1)
    admin.commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[
            AddPartitionSpecUpdate(spec=spec),
            SetDefaultSpecUpdate(spec_id=-1),
            AddSortOrderUpdate(sort_order=order),
            SetDefaultSortOrderUpdate(sort_order_id=-1),
        ],
    )


_SPEC_ID_TAKEN = (
    "This partition spec change adds a partition spec under an id the table already uses, so "
    "nothing was applied. Load the table again and make the change on what the load gives; "
    "loading needs READ on the table."
)
_SORT_ORDER_ID_TAKEN = (
    "This sort order change adds a sort order under an id the table already uses, so nothing "
    "was applied. Load the table again and make the change on what the load gives; loading "
    "needs READ on the table."
)


@pytest.mark.parametrize("who", ["writer", "reader-writer", "admin"])
@pytest.mark.parametrize("same_content", [True, False], ids=["identical", "differing"])
@pytest.mark.parametrize("which", ["spec", "sort-order"])
def test_a_spec_or_sort_order_added_under_an_id_the_table_has_is_refused(
    session: Session,
    principal: PrincipalRef,
    admin: CatalogService,
    who: str,
    same_content: bool,
    which: str,
) -> None:
    """An identical one used to succeed, which told a writer without READ that its guess was right.

    The refusal is the same for every caller and for both contents, so the answer says nothing
    about what the table has under the id.
    """
    _two_layouts(admin, principal)
    before = admin.load_table(principal, _ref()).metadata
    column = 1 if same_content else 2
    name = "id" if same_content else "other"
    if which == "spec":
        added: Any = AddPartitionSpecUpdate(
            spec=PartitionSpec(
                PartitionField(
                    source_id=column, field_id=1000, transform=IdentityTransform(), name=name
                ),
                spec_id=1,
            )
        )
        text = _SPEC_ID_TAKEN
    else:
        added = AddSortOrderUpdate(
            sort_order=SortOrder(
                SortField(source_id=column, transform=IdentityTransform()), order_id=1
            )
        )
        text = _SORT_ORDER_ID_TAKEN

    with pytest.raises(ValidationError) as refused:
        _callers(session)[who].commit_table(principal, _ref(), requirements=[], updates=[added])

    assert str(refused.value) == text
    after = admin.load_table(principal, _ref()).metadata
    assert (after.partition_specs, after.sort_orders) == (
        before.partition_specs,
        before.sort_orders,
    )


@pytest.mark.parametrize("which", ["spec", "sort-order"])
def test_two_specs_or_sort_orders_under_one_new_id_in_one_commit_are_refused(
    session: Session, principal: PrincipalRef, admin: CatalogService, which: str
) -> None:
    """The second would be found after the first by its id, as for schemas."""
    if which == "spec":
        first, second = (
            AddPartitionSpecUpdate(
                spec=PartitionSpec(
                    PartitionField(
                        source_id=1, field_id=1000, transform=IdentityTransform(), name="id"
                    ),
                    spec_id=1,
                )
            ),
            AddPartitionSpecUpdate(
                spec=PartitionSpec(
                    PartitionField(
                        source_id=2, field_id=1000, transform=IdentityTransform(), name="other"
                    ),
                    spec_id=1,
                )
            ),
        )
    else:
        first, second = (
            AddSortOrderUpdate(
                sort_order=SortOrder(
                    SortField(source_id=1, transform=IdentityTransform()), order_id=1
                )
            ),
            AddSortOrderUpdate(
                sort_order=SortOrder(
                    SortField(source_id=2, transform=IdentityTransform()), order_id=1
                )
            ),
        )

    with pytest.raises(ValidationError, match="already uses"):
        admin.commit_table(principal, _ref(), requirements=[], updates=[first, second])


@pytest.mark.parametrize("which", ["spec", "sort-order"])
@pytest.mark.parametrize("target", [0, 12345])
def test_an_earlier_spec_or_sort_order_cannot_be_made_default_without_read(
    session: Session, principal: PrincipalRef, admin: CatalogService, which: str, target: int
) -> None:
    """The reduced answer would carry it, and a partition field is named like its column.

    The text is the same whether or not the id exists.
    """
    _two_layouts(admin, principal)
    before = admin.load_table(principal, _ref()).metadata
    update_ = (
        SetDefaultSpecUpdate(spec_id=target)
        if which == "spec"
        else SetDefaultSortOrderUpdate(sort_order_id=target)
    )

    with pytest.raises(ValidationError) as refused:
        _callers(session)["writer"].commit_table(
            principal, _ref(), requirements=[], updates=[update_]
        )

    assert str(refused.value) == _NEEDS_READ_FOR_SCHEMA
    after = admin.load_table(principal, _ref()).metadata
    assert (after.default_spec_id, after.default_sort_order_id) == (
        before.default_spec_id,
        before.default_sort_order_id,
    )


@pytest.mark.parametrize("which", ["spec", "sort-order"])
def test_a_reader_may_make_an_earlier_spec_or_sort_order_default(
    session: Session, principal: PrincipalRef, admin: CatalogService, which: str
) -> None:
    _two_layouts(admin, principal)
    update_ = (
        SetDefaultSpecUpdate(spec_id=0)
        if which == "spec"
        else SetDefaultSortOrderUpdate(sort_order_id=0)
    )

    loaded = _callers(session)["reader-writer"].commit_table(
        principal, _ref(), requirements=[], updates=[update_]
    )

    assert (loaded.metadata.default_spec_id, loaded.metadata.default_sort_order_id)[
        0 if which == "spec" else 1
    ] == 0


@pytest.mark.parametrize("explicit", [-1, "added"])
def test_a_writer_without_read_can_add_a_spec_and_make_it_default(
    session: Session, principal: PrincipalRef, admin: CatalogService, explicit: int | str
) -> None:
    spec = PartitionSpec(
        PartitionField(source_id=1, field_id=1000, transform=IdentityTransform(), name="id"),
        spec_id=1,
    )
    order = SortOrder(SortField(source_id=1, transform=IdentityTransform()), order_id=1)

    loaded = _callers(session)["writer"].commit_table(
        principal,
        _ref(),
        requirements=[],
        updates=[
            AddPartitionSpecUpdate(spec=spec),
            SetDefaultSpecUpdate(spec_id=1 if explicit == "added" else -1),
            AddSortOrderUpdate(sort_order=order),
            SetDefaultSortOrderUpdate(sort_order_id=1 if explicit == "added" else -1),
        ],
    )

    assert (loaded.metadata.default_spec_id, loaded.metadata.default_sort_order_id) == (1, 1)
