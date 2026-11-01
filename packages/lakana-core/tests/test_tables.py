"""Tests for table operations through the catalog service."""

import ast
import time
import uuid
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import (
    FakeColumnCatalog,
    _catalog_with,
    _config,
    _prepared_catalog,
    _RecordAll,
    _RecordThenDeny,
)
from pyiceberg.io.pyarrow import PyArrowFileIO
from pyiceberg.schema import Schema
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.snapshots import Operation, Snapshot, Summary
from pyiceberg.table.update import (
    AddSnapshotUpdate,
    AssertTableUUID,
    SetLocationUpdate,
    SetPropertiesUpdate,
    SetSnapshotRefUpdate,
)
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.catalog import service as service_module
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    NotPublishedError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import (
    Principal,
    PrincipalType,
    Table,
    TableLocation,
    TableLocationState,
    TableType,
)
from lakana_core.policy.column_masks import ColumnMaskService
from lakana_core.policy.row_filters import RowFilterService


def _schema() -> Schema:
    """Build a two-column schema.

    Returns
    -------
    Schema
        A schema with a required id and an optional name.
    """
    return Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "name", StringType(), required=False),
    )


def _prepared_ref(catalog: CatalogService, principal: PrincipalRef, tmp_path: Path) -> TableRef:
    """Create `dev` (which scaffolds `gold` among its namespaces) and return a ref to a table in it.

    Parameters
    ----------
    catalog
        The catalog under test.
    principal
        The acting principal.
    tmp_path
        Directory backing the environment's storage.

    Returns
    -------
    TableRef
        A reference to `dev` / `gold` / `sales`, which does not exist yet.
    """
    ns = NamespacePath.from_parts(["gold"])
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    return TableRef(environment="dev", namespace=ns, name="sales")


def _metadata_dir(loaded: LoadedTable) -> Path:
    """Return the local directory holding a table's metadata files.

    Parameters
    ----------
    loaded
        A loaded table whose location is a ``file://`` URI.

    Returns
    -------
    Path
        The directory containing the metadata files.
    """
    return Path(loaded.metadata_location.removeprefix("file://")).parent


def test_create_then_load_round_trips(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A created table loads back with the same uuid and schema."""
    ref = _prepared_ref(catalog, principal, tmp_path)

    created = catalog.create_table(principal, ref, schema=_schema())
    loaded = catalog.load_table(principal, ref)

    assert loaded.uuid == created.uuid
    assert loaded.metadata_location == created.metadata_location
    assert loaded.metadata.schemas[0] == created.metadata.schemas[0]


def test_the_row_id_is_the_iceberg_table_uuid(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """Lakana's identity and the identity engines observe are one value."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())

    row = session.execute(select(Table).where(Table.id == created.uuid)).scalar_one()

    assert row.id == created.metadata.table_uuid
    assert row.table_type == TableType.ICEBERG
    assert row.previous_metadata_location is None


def test_the_metadata_file_exists_in_storage(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The pointer is not the only artefact: the file is really written."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())

    assert Path(created.metadata_location.removeprefix("file://")).is_file()
    assert created.metadata_location.split("/")[-1].startswith("00000-")


def test_creating_the_same_table_twice_raises_already_exists(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The second create is refused."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    catalog.create_table(principal, ref, schema=_schema())

    with pytest.raises(AlreadyExistsError):
        catalog.create_table(principal, ref, schema=_schema())


def test_create_table_race_on_the_unique_constraint_raises_already_exists(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A concurrent writer that wins the insert race is still refused.

    The pre-existence check only produces a good error message in the
    common case; the ``(namespace_id, name)`` unique constraint is what
    actually arbitrates a genuine race, per architecture Constraint 3. A
    real concurrent writer cannot be exercised against a single in-process
    session, so the race is simulated by patching the pre-check to miss the
    row that is, in fact, already there, forcing the insert itself to hit
    the constraint.

    The count of metadata files written pins the ordering itself, not just
    the outcome: a losing writer writes its metadata file before it loses
    the insert race. A build that reversed Constraint 3 — inserting the row
    before writing the file — would never reach the write on the losing
    attempt. Each `create_table` call generates its own id, so the losing
    writer's file lands in its own sibling ``sales-<hex>`` directory, and
    since a create claims its location the loser, known not to have
    committed, deletes that file and its claim again: only the winner's
    file is left.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())

    with (
        mock.patch.object(CatalogService, "_lookup_table", return_value=None),
        mock.patch.object(
            service_module, "write_metadata", wraps=service_module.write_metadata
        ) as written,
        pytest.raises(AlreadyExistsError),
    ):
        catalog.create_table(principal, ref, schema=_schema())

    assert written.call_count == 1  # the loser wrote its file before it lost the insert
    sales_root = Path(created.metadata_location.removeprefix("file://")).parent.parent.parent
    assert len(list(sales_root.glob("sales-*/metadata/*.metadata.json"))) == 1


def test_create_table_reports_conflict_when_the_integrity_error_is_not_a_name_collision(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An `IntegrityError` that is not a name collision must not be misreported as one.

    Dropping the namespace from another session between the pre-check and
    the insert is reachable in production — `drop_namespace` permits
    dropping a namespace that is empty at that instant — and manifests as a
    foreign-key violation, not a unique-constraint violation. A handler
    that assumes every `IntegrityError` here means "this name is taken"
    would tell a caller (or a REST layer mapping this to 409) something
    false. This does not attempt to reproduce the exact foreign-key race;
    it proves the classifying logic itself, by making `commit` fail for an
    unrelated reason while no conflicting row actually exists.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)

    real = Session.commit

    def _fail(self: Session) -> None:
        if any(isinstance(item, TableLocation) for item in self.new):
            real(self)  # the create's claim of its location, which comes first
            return
        raise IntegrityError(
            "INSERT INTO tables (id, name) VALUES (?, ?)",
            {"id": "secret-uuid", "name": "sales"},
            Exception("FOREIGN KEY constraint failed"),
        )

    with (
        mock.patch.object(Session, "commit", _fail),
        pytest.raises(ConflictError) as excinfo,
    ):
        catalog.create_table(principal, ref, schema=_schema())

    assert catalog.table_exists(principal, ref) is False
    # The statement and its bound parameters must not leak into the
    # message a caller (eventually a REST client, once Phase C maps
    # `ConflictError` onto a response) sees.
    assert "INSERT INTO" not in str(excinfo.value)
    assert "secret-uuid" not in str(excinfo.value)


def test_create_table_rejects_an_invalid_table_name(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An invalid table name is refused even with no `location` to bypass validation with.

    Formerly this was only exercised together with a caller-supplied
    `location`, since `location=` bypassed `table_location`'s own
    validation of the table name entirely. Now that `location=` is always
    refused (see `test_caller_supplied_location_is_refused`), the only
    remaining path to a computed location is `table_location` itself, which
    still validates the name unconditionally.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ns = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=ns, name="../../pwn")

    with pytest.raises(ValidationError):
        catalog.create_table(principal, ref, schema=_schema())


def test_create_table_assigns_an_id_suffixed_location(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A created table's location is suffixed with its own id's hex digits.

    The suffix is what lets a new table safely reuse a name a
    previous table was renamed away from: `rename_table` never moves files,
    so without a unique suffix the new table would inherit the old table's
    directory, and its data files would then pass the worker's containment
    check as if they belonged to it.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)

    created = catalog.create_table(principal, ref, schema=_schema())

    assert created.metadata.location.endswith(f"/managed/gold/sales-{created.uuid.hex}")


def test_caller_supplied_location_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`create_table` refuses any caller-supplied `location`, inside the root or not.

    Lakana now assigns every managed table's location itself,
    id-suffixed; a caller-supplied `location` -- including the Iceberg REST
    `CreateTableRequest.location` a client can send -- is refused outright
    rather than confined, since honouring one at all would let a table's
    location collide with another table's, defeating the confinement the id
    suffix exists to guarantee.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    custom = f"file://{tmp_path}/custom/sales"

    with pytest.raises(ValidationError, match="do not supply one"):
        catalog.create_table(principal, ref, schema=_schema(), location=custom)

    assert catalog.table_exists(principal, ref) is False


@pytest.mark.parametrize(
    "key", ["write.metadata.path", "write.data.path", "write.object-storage.path"]
)
def test_create_table_refuses_a_location_valued_property(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, key: str
) -> None:
    """A location-valued table property is refused outright at create time.

    Previously only
    `write.metadata.path` and `write.data.path` were confined to the
    storage root; now all three (`write.object-storage.path` included, even
    though the installed pyiceberg does not currently read it) are refused
    whatever their value, the same as a caller-supplied `location=` is —
    confining them was not enough to stop one redirecting a table's
    metadata or data to a location colliding with another table's own.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    outside = tmp_path.parent / "OUTSIDE"

    with pytest.raises(ValidationError, match="Lakana assigns table locations"):
        catalog.create_table(
            principal,
            ref,
            schema=_schema(),
            properties={key: f"file://{outside}/via_prop"},
        )

    assert not outside.exists()
    assert catalog.table_exists(principal, ref) is False


def test_create_table_rejects_an_arbitrary_location_provider_class(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`write.py-location-provider.impl` must never be honoured from caller input.

    pyiceberg's location-provider loader `importlib.import_module`s and
    instantiates an arbitrary dotted path taken directly from this
    property. Confining the resulting file path is not enough to close
    this: the import and instantiation already happened by the time there
    is a path to confine, so this property must be rejected outright
    before `next_metadata_location` (and therefore pyiceberg's loader) is
    ever called.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)

    with pytest.raises(ValidationError):
        catalog.create_table(
            principal,
            ref,
            schema=_schema(),
            properties={"write.py-location-provider.impl": "os.system"},
        )


def test_load_table_with_no_metadata_pointer_raises_not_found(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """A table row without a metadata pointer cannot be loaded.

    `create_table` never leaves a row in this state; it is reachable here
    only by inserting the row directly, which is what this test does.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    ns = catalog.load_namespace(principal, "dev", ref.namespace)
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name=ref.name,
            table_type=TableType.ICEBERG,
            metadata_location=None,
            previous_metadata_location=None,
        )
    )
    session.commit()

    with pytest.raises(NotFoundError):
        catalog.load_table(principal, ref)


def test_commit_table_with_no_metadata_pointer_raises_not_found(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """A table row without a metadata pointer cannot be committed to.

    `create_table` never leaves a row in this state; it is reachable here
    only by inserting the row directly, which is what this test does.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    ns = catalog.load_namespace(principal, "dev", ref.namespace)
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name=ref.name,
            table_type=TableType.ICEBERG,
            metadata_location=None,
            previous_metadata_location=None,
        )
    )
    session.commit()

    with pytest.raises(NotFoundError):
        catalog.commit_table(principal, ref, requirements=[], updates=[])


def test_create_in_an_unknown_namespace_raises_not_found(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The namespace must exist first."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["ghost"]), name="sales")
    with pytest.raises(NotFoundError):
        catalog.create_table(principal, ref, schema=_schema())


def test_load_unknown_raises_not_found(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A missing table is an error."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    with pytest.raises(NotFoundError):
        catalog.load_table(principal, ref)


def test_load_table_row_of_unknown_table_raises_not_found(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A missing table is an error, the same as `load_table`."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    with pytest.raises(NotFoundError):
        catalog.load_table_row(principal, ref)


def test_load_table_row_of_an_iceberg_table_returns_the_raw_row(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The raw row for an ICEBERG table comes back, without reading its metadata."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())

    row = catalog.load_table_row(principal, ref)

    assert row.id == created.uuid
    assert row.table_type == TableType.ICEBERG
    assert row.metadata_location == created.metadata_location


def test_load_table_row_of_an_external_table_returns_the_raw_row(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The raw row for an EXTERNAL table comes back — `load_table` cannot do this at all."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(
        pa.table({"id": [1, 2]}),
        tmp_path / "raw" / "events" / "part-0.parquet",
    )
    ref = TableRef(environment="dev", namespace=NamespacePath.from_parts(["raw"]), name="events")
    created = catalog.create_external_table(
        principal, ref, location_pattern="raw/events/*.parquet", format="parquet"
    )

    row = catalog.load_table_row(principal, ref)

    assert row.id == created.id
    assert row.table_type == TableType.EXTERNAL
    assert row.external_config == created.external_config


def test_table_exists_is_true_only_after_creation(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`table_exists` answers without raising, in both directions."""
    ref = _prepared_ref(catalog, principal, tmp_path)

    assert catalog.table_exists(principal, ref) is False
    catalog.create_table(principal, ref, schema=_schema())
    assert catalog.table_exists(principal, ref) is True


def test_list_tables_returns_the_namespace_contents_in_order(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Listing is scoped to one namespace and stably ordered.

    Three tables, inserted out of alphabetical order, so the assertion
    cannot be satisfied by insertion order alone.
    """
    ns = NamespacePath.from_parts(["gold"])
    other = NamespacePath.from_parts(["bronze"])
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    for name in ("c_table", "a_table", "b_table"):
        catalog.create_table(
            principal, TableRef(environment="dev", namespace=ns, name=name), schema=_schema()
        )
    catalog.create_table(
        principal,
        TableRef(environment="dev", namespace=other, name="hidden"),
        schema=_schema(),
    )

    listed = catalog.list_tables(principal, "dev", ns)

    assert [r.name for r in listed] == ["a_table", "b_table", "c_table"]


def test_each_table_operation_authorizes_its_own_action(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Every operation consults authorization with the exact expected pair.

    `commit_table` and `drop_table` are here rather than resting on the
    table-scope checks above: they are the two mutating table operations,
    and Constraint 9 says M3 replaces the authorization *implementation*,
    never the call sites. An unpinned call site is a silent future
    privilege bug. The table is created up front so that each row is about
    the action and securable alone, not about whether the lookup happens to
    fail first.
    """
    ns = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=ns, name="sales")
    expected = [
        (Action.CREATE_TABLE, Securable.table_scope("dev", ns, "sales")),
        (Action.READ_TABLE, Securable.table_scope("dev", ns, "sales")),
        (Action.READ_TABLE, Securable.table_scope("dev", ns, "sales")),
        (Action.READ_TABLE, Securable.namespace_scope("dev", ns)),
        (Action.WRITE_TABLE, Securable.table_scope("dev", ns, "sales")),
        (Action.DROP_TABLE, Securable.table_scope("dev", ns, "sales")),
    ]
    calls = [
        lambda c, p: c.create_table(p, ref, schema=_schema()),
        lambda c, p: c.load_table(p, ref),
        lambda c, p: c.table_exists(p, ref),
        lambda c, p: c.list_tables(p, "dev", ns),
        lambda c, p: c.commit_table(p, ref, requirements=[], updates=[]),
        lambda c, p: c.drop_table(p, ref),
    ]
    for call, want in zip(calls, expected, strict=True):
        recorder = _RecordThenDeny()
        catalog = _prepared_catalog(session, tmp_path, recorder, namespaces=[ns], tables=[ref])
        with pytest.raises(PermissionDenied):
            call(catalog, principal)
        assert recorder.seen == want


def test_create_table_refuses_a_data_path_even_inside_the_root(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`write.data.path` is refused even when it really is inside the root.

    It used to be
    merely confined to the storage root (and so accepted when it was truly
    inside it, as this test used to assert); it is now refused whatever its
    value, the same as every other location-valued property.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    inside = f"file://{tmp_path}/custom-data"

    with pytest.raises(ValidationError, match="Lakana assigns table locations"):
        catalog.create_table(
            principal, ref, schema=_schema(), properties={"write.data.path": inside}
        )


def test_drop_removes_the_catalog_entry(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A dropped table can no longer be loaded."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    catalog.create_table(principal, ref, schema=_schema())

    catalog.drop_table(principal, ref)

    assert catalog.table_exists(principal, ref) is False


def test_drop_without_purge_leaves_the_metadata_file(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The default drop is catalog-only, so the data survives."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())
    path = Path(created.metadata_location.removeprefix("file://"))

    catalog.drop_table(principal, ref)

    assert path.is_file()


def test_drop_with_purge_deletes_nothing_itself_and_leaves_the_files_to_the_sweep(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """`purge=True` makes the files due at once; only the location sweep deletes table files.

    A drop that touched storage could fail after its commit and leave a row-less half; this one
    never calls the storage's delete at all.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())
    path = Path(created.metadata_location.removeprefix("file://"))

    with mock.patch.object(PyArrowFileIO, "delete", side_effect=OSError("boom")) as delete:
        catalog.drop_table(principal, ref, purge=True)

    delete.assert_not_called()
    assert path.exists()
    assert session.execute(select(Table)).first() is None
    record = session.scalars(select(TableLocation)).one()
    assert record.state is TableLocationState.DROPPED
    assert record.purge_after == record.dropped_at


def test_drop_unknown_raises_not_found(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Dropping a missing table is an error, not a silent no-op."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    with pytest.raises(NotFoundError):
        catalog.drop_table(principal, ref)


def test_rename_moves_the_table_between_namespaces(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The table is gone from the source and present at the destination."""
    source = _prepared_ref(catalog, principal, tmp_path)
    target_ns = NamespacePath.from_parts(["silver"])
    destination = TableRef(environment="dev", namespace=target_ns, name="renamed")
    created = catalog.create_table(principal, source, schema=_schema())

    moved = catalog.rename_table(principal, source, destination)

    assert catalog.table_exists(principal, source) is False
    assert catalog.table_exists(principal, destination) is True
    assert moved.uuid == created.uuid


def test_rename_keeps_the_metadata_location(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A rename is a catalog operation; it does not move data."""
    source = _prepared_ref(catalog, principal, tmp_path)
    destination = TableRef(environment="dev", namespace=source.namespace, name="renamed")
    created = catalog.create_table(principal, source, schema=_schema())

    moved = catalog.rename_table(principal, source, destination)

    assert moved.metadata_location == created.metadata_location


def test_rename_refreshes_display_columns_for_bound_grants_and_policies(
    catalog: CatalogService, session: Session, tmp_path: Path
) -> None:
    """A rename updates the display `namespace`/`table` of every id-bound grant/filter/mask.

    `rename_table` keeps a grant's or policy's `table_id` binding stable
    (see `test_grants_authorization.py::test_table_grant_follows_rename`
    for the authorization-level guarantee), but a caller that lists by
    name — `GrantService.list_for_securable`, `RowFilterService`/
    `ColumnMaskService.list_for_table` — must find it under the table's
    *new* name too. That needs the display columns themselves rewritten,
    not merely the id binding continuing to work.
    """
    admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=admin.id, type=admin.type, name=admin.name, is_active=True))
    session.commit()
    ns = NamespacePath.from_parts(["gold"])
    catalog.create_environment(admin, name="dev", storage=_config(tmp_path))
    source = TableRef(environment="dev", namespace=ns, name="sales")
    catalog.create_table(admin, source, schema=_schema())

    PrincipalService(session, AllowAllAuthorization()).create_principal(
        admin, name="alice", type=PrincipalType.SERVICE_ACCOUNT
    )
    grants = GrantService(session, AllowAllAuthorization())
    grant = grants.grant(
        admin,
        principal="alice",
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace=ns.joined(),
        table="sales",
    )
    row_filters = RowFilterService(session, AllowAllAuthorization(), FakeColumnCatalog())
    row_filter = row_filters.create(
        admin,
        principal="alice",
        environment="dev",
        namespace=ns.joined(),
        table="sales",
        expression="TRUE",
    )
    column_masks = ColumnMaskService(session, AllowAllAuthorization(), FakeColumnCatalog())
    mask = column_masks.create(
        admin,
        principal="alice",
        environment="dev",
        namespace=ns.joined(),
        table="sales",
        column="name",
        expression="NULL",
    )

    destination = TableRef(environment="dev", namespace=ns, name="sales2")
    catalog.rename_table(admin, source, destination)

    at_new_name = grants.list_grants(admin, scope=Securable.table_scope("dev", ns, "sales2"))
    assert [g.id for g in at_new_name.items] == [grant.id]
    at_old_name = grants.list_grants(admin, scope=Securable.table_scope("dev", ns, "sales"))
    assert at_old_name.items == []

    filters_at_new_name = row_filters.list_for_table(
        admin, environment="dev", namespace=ns.joined(), table="sales2"
    )
    assert [f.id for f in filters_at_new_name] == [row_filter.id]
    assert (
        row_filters.list_for_table(admin, environment="dev", namespace=ns.joined(), table="sales")
        == []
    )

    masks_at_new_name = column_masks.list_for_table(
        admin, environment="dev", namespace=ns.joined(), table="sales2"
    )
    assert [m.id for m in masks_at_new_name] == [mask.id]
    assert (
        column_masks.list_for_table(admin, environment="dev", namespace=ns.joined(), table="sales")
        == []
    )


def test_rename_onto_an_existing_table_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A rename must not silently replace the destination."""
    source = _prepared_ref(catalog, principal, tmp_path)
    destination = TableRef(environment="dev", namespace=source.namespace, name="occupied")
    catalog.create_table(principal, source, schema=_schema())
    catalog.create_table(principal, destination, schema=_schema())

    with pytest.raises(AlreadyExistsError):
        catalog.rename_table(principal, source, destination)


def test_rename_logs_the_exception_type_when_the_integrity_error_is_not_a_name_clash(
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 409 for an unexplained integrity error leaves one server log line, with no SQL."""
    source = _prepared_ref(catalog, principal, tmp_path)
    destination = TableRef(environment="dev", namespace=source.namespace, name="renamed")
    catalog.create_table(principal, source, schema=_schema())

    def _fail(self: Session) -> None:
        raise IntegrityError(
            "UPDATE tables SET name=?",
            {"name": "secret-name"},
            Exception("FOREIGN KEY constraint failed"),
        )

    with (
        caplog.at_level("WARNING", logger="lakana_core.catalog.service"),
        mock.patch.object(Session, "commit", _fail),
        pytest.raises(ConflictError),
    ):
        catalog.rename_table(principal, source, destination)

    lines = [r.getMessage() for r in caplog.records if r.name == "lakana_core.catalog.service"]
    assert len(lines) == 1
    assert "IntegrityError" in lines[0]
    assert "UPDATE" not in lines[0] and "secret-name" not in lines[0]
    assert "FOREIGN KEY" not in lines[0]


def test_rename_across_environments_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Environments are isolation boundaries, not folders.

    Allowing a rename to cross them would be exactly the dev-to-prod
    promotion accident the environment-as-connection-context design exists to
    make impossible.
    """
    source = _prepared_ref(catalog, principal, tmp_path)
    catalog.create_environment(principal, name="prd", storage=_config(tmp_path / "prd"))
    catalog.create_table(principal, source, schema=_schema())
    destination = TableRef(environment="prd", namespace=source.namespace, name="sales")

    with pytest.raises(ValidationError):
        catalog.rename_table(principal, source, destination)


def test_rename_authorizes_both_source_and_destination(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Amendment A2: two securables, two calls, both before any mutation.

    A single authorization would leave a hole in one direction or the
    other: source-only lets a principal move a table into a namespace they
    cannot write to, destination-only lets them move it out of one they
    cannot drop from.
    """
    ns = NamespacePath.from_parts(["gold"])
    target_ns = NamespacePath.from_parts(["silver"])
    source = TableRef(environment="dev", namespace=ns, name="sales")
    destination = TableRef(environment="dev", namespace=target_ns, name="sales")
    recorder = _RecordAll()

    catalog = _prepared_catalog(
        session, tmp_path, recorder, namespaces=[ns, target_ns], tables=[source]
    )
    catalog.rename_table(principal, source, destination)

    assert recorder.seen == [
        (Action.DROP_TABLE, Securable.table_scope("dev", ns, "sales")),
        (Action.CREATE_TABLE, Securable.table_scope("dev", target_ns, "sales")),
    ]


def test_rename_is_refused_when_only_the_source_is_permitted(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Permission to drop from the source is not permission to create."""
    ns = NamespacePath.from_parts(["gold"])
    target_ns = NamespacePath.from_parts(["silver"])
    source = TableRef(environment="dev", namespace=ns, name="sales")
    destination = TableRef(environment="dev", namespace=target_ns, name="sales")

    class _DenyCreate:
        """Permits everything except CREATE_TABLE."""

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Deny only table creation.

            Parameters
            ----------
            principal
                The acting principal.
            action
                The attempted action.
            securable
                The target.

            Raises
            ------
            PermissionDenied
                When `action` is `CREATE_TABLE`.
            """
            if action is Action.CREATE_TABLE:
                raise PermissionDenied("nope")

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

    catalog = _prepared_catalog(
        session, tmp_path, _DenyCreate(), namespaces=[ns, target_ns], tables=[source]
    )

    with pytest.raises(PermissionDenied):
        catalog.rename_table(principal, source, destination)

    checker = _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    assert checker.table_exists(principal, source) is True


def test_rename_is_refused_when_only_the_destination_is_permitted(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Permission to create at the destination is not permission to drop the source.

    The mirror of `test_rename_is_refused_when_only_the_source_is_permitted`:
    without this half, a principal who cannot drop from the source
    namespace could still move a table out of it, which is exactly the
    hole amendment A2 exists to close.
    """
    ns = NamespacePath.from_parts(["gold"])
    target_ns = NamespacePath.from_parts(["silver"])
    source = TableRef(environment="dev", namespace=ns, name="sales")
    destination = TableRef(environment="dev", namespace=target_ns, name="sales")

    class _DenyDrop:
        """Permits everything except DROP_TABLE."""

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Deny only table drop.

            Parameters
            ----------
            principal
                The acting principal.
            action
                The attempted action.
            securable
                The target.

            Raises
            ------
            PermissionDenied
                When `action` is `DROP_TABLE`.
            """
            if action is Action.DROP_TABLE:
                raise PermissionDenied("nope")

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

    catalog = _prepared_catalog(
        session, tmp_path, _DenyDrop(), namespaces=[ns, target_ns], tables=[source]
    )

    with pytest.raises(PermissionDenied):
        catalog.rename_table(principal, source, destination)

    checker = _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    assert checker.table_exists(principal, source) is True


def test_commit_applies_an_update_and_advances_the_pointer(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A committed update is visible on reload, at a new metadata location."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())

    committed = catalog.commit_table(
        principal,
        ref,
        requirements=[AssertTableUUID(uuid=created.uuid)],
        updates=[SetPropertiesUpdate(updates={"owner": "chris"})],
    )

    assert committed.metadata_location != created.metadata_location
    assert committed.metadata_location.split("/")[-1].startswith("00001-")
    assert catalog.load_table(principal, ref).metadata.properties["owner"] == "chris"


def test_commit_records_the_previous_location(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """The replaced pointer is kept, which is what the CAS matches on next time."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())

    catalog.commit_table(
        principal, ref, requirements=[], updates=[SetPropertiesUpdate(updates={"a": "1"})]
    )

    session.expire_all()
    row = session.execute(select(Table).where(Table.id == created.uuid)).scalar_one()
    assert row.previous_metadata_location == created.metadata_location


def test_a_failed_requirement_writes_nothing(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Requirements are checked before anything is written or swapped."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())
    before = sorted(p.name for p in _metadata_dir(created).iterdir())

    with pytest.raises(ConflictError):
        catalog.commit_table(
            principal,
            ref,
            requirements=[AssertTableUUID(uuid=uuid.uuid4())],
            updates=[SetPropertiesUpdate(updates={"owner": "chris"})],
        )

    assert sorted(p.name for p in _metadata_dir(created).iterdir()) == before
    assert catalog.load_table(principal, ref).metadata_location == created.metadata_location


def test_a_failed_write_leaves_the_pointer_and_metadata_dir_untouched(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Constraint 3: a failed metadata write must not have moved anything.

    This pins the write-then-CAS *ordering*, not merely its outcome. An
    implementation that swapped the compare-and-swap ahead of the write
    would still propagate this same `OSError`, but only after already
    publishing a pointer to a file the write was about to fail to create
    -- exactly the dangling-pointer corruption Constraint 3 exists to
    prevent. Asserting that both the pointer and the metadata directory
    are exactly as `create_table` left them is what distinguishes the
    correct order from the reversed one; the reversed order fails this
    test's pointer assertion even though it still raises.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())
    before = sorted(p.name for p in _metadata_dir(created).iterdir())

    with (
        mock.patch("lakana_core.catalog.service.write_metadata", side_effect=OSError("boom")),
        pytest.raises(OSError, match="boom"),
    ):
        catalog.commit_table(
            principal,
            ref,
            requirements=[],
            updates=[SetPropertiesUpdate(updates={"owner": "chris"})],
        )

    assert sorted(p.name for p in _metadata_dir(created).iterdir()) == before
    assert catalog.load_table(principal, ref).metadata_location == created.metadata_location


def test_a_lost_race_raises_conflict_and_leaves_the_pointer_at_the_rival(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """A commit that reads a stale pointer loses the compare-and-swap.

    Contrary to what an earlier draft of this suite claimed, a stale CAS
    *is* reachable through the single-threaded public API. `_find_table`'s
    `SELECT` returns whatever the session's identity map already holds
    without refreshing unexpired attributes, so a session that loaded the
    row before a rival moved the pointer still sees the pre-race
    `metadata_location` when `commit_table` runs -- exactly the position a
    losing writer occupies against a real concurrent session. The rival's
    move is applied with `synchronize_session=False` specifically so it
    does *not* touch the identity map, which is what makes the row go
    stale within a single session at all.

    This is also the test that actually pins `commit_table` to calling
    `commit_metadata_pointer` rather than moving the pointer some other
    way: a version that replaced that call with an unconditional ORM
    assignment (`row.metadata_location = written`) passes every other test
    in this module, because none of the others force a mismatch between
    the expected and actual pointer. This one does, and only a real
    compare-and-swap can fail it the way it is asserted to fail here.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())

    row = session.execute(select(Table).where(Table.id == created.uuid)).scalar_one()
    rival = f"{created.metadata_location.rsplit('/', 1)[0]}/00001-{uuid.uuid4()}.metadata.json"
    session.execute(
        update(Table).where(Table.id == created.uuid).values(metadata_location=rival),
        execution_options={"synchronize_session": False},
    )
    assert row.metadata_location == created.metadata_location  # the stale read

    with pytest.raises(ConflictError):
        catalog.commit_table(
            principal, ref, requirements=[], updates=[SetPropertiesUpdate(updates={"l": "1"})]
        )

    session.expire_all()
    assert (
        session.execute(select(Table).where(Table.id == created.uuid))
        .scalar_one()
        .metadata_location
        == rival
    )


def test_sequential_commits_chain_through_previous_location(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """Two commits in sequence advance the version and chain the pointer history.

    This covers the happy-path sequencing only: that two commits in a row
    each advance the version by one, and that each one's
    `previous_metadata_location` is exactly the location the prior commit
    just published. A lost race is covered separately, at the
    `commit_table` level, by
    `test_a_lost_race_raises_conflict_and_leaves_the_pointer_at_the_rival`,
    and again directly against `commit_metadata_pointer` in
    `test_commit.py`.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())

    first = catalog.commit_table(
        principal, ref, requirements=[], updates=[SetPropertiesUpdate(updates={"a": "1"})]
    )
    second = catalog.commit_table(
        principal, ref, requirements=[], updates=[SetPropertiesUpdate(updates={"b": "2"})]
    )

    assert first.metadata_location.split("/")[-1].startswith("00001-")
    assert second.metadata_location.split("/")[-1].startswith("00002-")
    assert len({created.metadata_location, first.metadata_location, second.metadata_location}) == 3

    session.expire_all()
    row = session.execute(select(Table).where(Table.id == created.uuid)).scalar_one()
    assert row.metadata_location == second.metadata_location
    assert row.previous_metadata_location == first.metadata_location


def test_commit_requires_write_not_merely_read(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Reading a table must not be enough to change it."""
    ns = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=ns, name="sales")

    class _DenyWrite:
        """Permits everything except WRITE_TABLE."""

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Deny only table writes.

            Parameters
            ----------
            principal
                The acting principal.
            action
                The attempted action.
            securable
                The target.

            Raises
            ------
            PermissionDenied
                When `action` is `WRITE_TABLE`.
            """
            if action is Action.WRITE_TABLE:
                raise PermissionDenied("nope")

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

    catalog = _catalog_with(session, tmp_path, AllowAllAuthorization())
    catalog.create_table(principal, ref, schema=_schema())
    catalog = _catalog_with(session, tmp_path, _DenyWrite())

    with pytest.raises(PermissionDenied):
        catalog.commit_table(principal, ref, requirements=[], updates=[])


@pytest.mark.parametrize(
    "key", ["write.metadata.path", "write.data.path", "write.object-storage.path"]
)
def test_commit_refuses_a_location_valued_property(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, key: str
) -> None:
    """A location-valued table property set through a commit update is refused outright.

    All three are refused
    whatever their value now, not merely confined to the root, exactly as
    `create_table`'s own `properties=` argument refuses them.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())
    outside = tmp_path.parent / "LOCATION_PROPERTY_ESCAPE_COMMIT"

    with pytest.raises(ValidationError, match="Lakana assigns table locations"):
        catalog.commit_table(
            principal,
            ref,
            requirements=[],
            updates=[SetPropertiesUpdate(updates={key: f"file://{outside}/via_prop"})],
        )

    assert not outside.exists()
    assert catalog.load_table(principal, ref).metadata_location == created.metadata_location


def test_commit_refuses_set_location(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A commit can never move a table's location, even to one inside the root.

    A table's location is assigned once, at `create_table`, and is
    immutable for the table's lifetime: the worker's file
    confinement check trusts that every data file under a table's location
    belongs to it, and a `SetLocationUpdate` -- to anywhere, not just
    outside the storage root -- would invalidate that trust for files
    already committed under the old location. This supersedes the narrower
    older guard that only caught a `SetLocationUpdate` whose *target*
    escaped the root; an in-root target is refused just as outright.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())
    in_root = f"file://{tmp_path}/managed/gold/still-inside"

    with pytest.raises(ValidationError, match="location cannot change"):
        catalog.commit_table(
            principal,
            ref,
            requirements=[],
            updates=[SetLocationUpdate(location=in_root)],
        )

    reloaded = catalog.load_table(principal, ref)
    assert reloaded.metadata_location == created.metadata_location
    assert reloaded.metadata.location == created.metadata.location


def test_commit_refuses_a_foreign_manifest_list(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An `AddSnapshotUpdate` whose `manifest_list` lies outside the table's location is refused.

    A committed
    snapshot's `manifest_list` is Iceberg's own metadata pointing at further
    metadata, so it is exactly as capable of naming a location outside the
    table's own as `write.metadata.path` or a `SetLocationUpdate` are. This
    is a shallow check on the pointer itself, not a walk of the manifests it
    would lead to -- `list_data_files`/the worker (`lakana_worker.pipeline`)
    is where every manifest and data file the manifest list leads to is
    checked, as defence in depth.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())
    foreign_manifest_list = f"file://{tmp_path}/OUTSIDE/fake-manifest-list.avro"
    foreign_snapshot = Snapshot(
        snapshot_id=1,
        parent_snapshot_id=None,
        sequence_number=1,
        timestamp_ms=int(time.time() * 1000),
        manifest_list=foreign_manifest_list,
        summary=Summary(operation=Operation.APPEND),
        schema_id=created.metadata.current_schema_id,
    )

    with pytest.raises(ValidationError, match="manifest list"):
        catalog.commit_table(
            principal,
            ref,
            requirements=[],
            updates=[
                AddSnapshotUpdate(snapshot=foreign_snapshot),
                SetSnapshotRefUpdate(ref_name="main", type=SnapshotRefType.BRANCH, snapshot_id=1),
            ],
        )

    reloaded = catalog.load_table(principal, ref)
    assert reloaded.metadata_location == created.metadata_location
    assert reloaded.metadata.current_snapshot_id is None


def test_commit_rejects_an_arbitrary_location_provider_class(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`write.py-location-provider.impl` must never be honoured from a commit update either.

    pyiceberg's location-provider loader imports and instantiates an
    arbitrary dotted path taken directly from this property value, so it
    must be rejected before `next_metadata_location` -- and therefore that
    loader -- is ever reached, exactly as `create_table` already refuses it
    at creation time.
    """
    ref = _prepared_ref(catalog, principal, tmp_path)
    created = catalog.create_table(principal, ref, schema=_schema())

    with pytest.raises(ValidationError):
        catalog.commit_table(
            principal,
            ref,
            requirements=[],
            updates=[SetPropertiesUpdate(updates={"write.py-location-provider.impl": "os.system"})],
        )

    assert catalog.load_table(principal, ref).metadata_location == created.metadata_location


def test_create_refuses_a_namespace_outside_the_medallion_convention(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An ICEBERG table cannot be created under a namespace that isn't bronze/silver/gold.

    `create_namespace` itself stays unrestricted (the rule tracks a table's zone, not
    the namespace's name) — `junk` is created successfully here, and it is only the
    following `create_table` call that is refused.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    junk = NamespacePath.from_parts(["junk"])
    catalog.create_namespace(principal, "dev", junk)

    with pytest.raises(ValidationError, match="bronze, silver, or gold"):
        catalog.create_table(
            principal, TableRef(environment="dev", namespace=junk, name="sales"), schema=_schema()
        )


def test_create_table_rejects_a_dotted_name(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A table name with a dot is refused up front, naming the name."""
    ref = _prepared_ref(catalog, principal, tmp_path)
    dotted = TableRef(environment="dev", namespace=ref.namespace, name="a.b")

    with pytest.raises(ValidationError, match=r"'a\.b'"):
        catalog.create_table(principal, dotted, schema=_schema())


def test_rename_to_a_dotted_name_is_refused_but_away_from_one_is_allowed(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """Renaming *to* a dotted name fails; an old dotted table can be renamed to a clean name."""
    source = _prepared_ref(catalog, principal, tmp_path)
    catalog.create_table(principal, source, schema=_schema())
    dotted = TableRef(environment="dev", namespace=source.namespace, name="a.b")

    with pytest.raises(ValidationError, match=r"'a\.b'"):
        catalog.rename_table(principal, source, dotted)

    session.execute(update(Table).where(Table.name == source.name).values(name="a.b"))
    session.commit()
    assert catalog.table_exists(principal, dotted)
    clean = TableRef(environment="dev", namespace=source.namespace, name="clean")
    catalog.rename_table(principal, dotted, clean)
    assert catalog.table_exists(principal, clean)


def test_a_dotted_table_stays_loadable_and_droppable(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """Loading and dropping a pre-existing dotted table never re-validates the name."""
    source = _prepared_ref(catalog, principal, tmp_path)
    catalog.create_table(principal, source, schema=_schema())
    session.execute(update(Table).where(Table.name == source.name).values(name="a.b"))
    session.commit()
    dotted = TableRef(environment="dev", namespace=source.namespace, name="a.b")

    catalog.load_table(principal, dotted)
    catalog.drop_table(principal, dotted)
    assert not catalog.table_exists(principal, dotted)


def _unpublish(session: Session, ref: TableRef, run_id: uuid.UUID | None) -> None:
    """Mark a table as not published, as the rollback of a failed first build does.

    Parameters
    ----------
    session
        Database session; commits.
    ref
        The table.
    run_id
        The run whose checks failed; `None` publishes the table again.
    """
    session.execute(update(Table).where(Table.name == ref.name).values(unpublished_run_id=run_id))
    session.commit()


def _not_published_text(ref: TableRef, run_id: uuid.UUID) -> str:
    """Build the text of a table that is not published, with no result row left.

    Parameters
    ----------
    ref
        The table.
    run_id
        The run.

    Returns
    -------
    str
        The text.
    """
    return (
        f"`{ref.dotted()}` has not been published: its first build did not finish, "
        f"or failed a check (run {run_id}). It is published by the next run that builds it "
        "and passes its checks."
    )


def test_loading_a_table_that_is_not_published_is_refused(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    ns = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=ns, name="sales")
    allowed = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[ns], tables=[ref]
    )
    run_id = uuid.uuid4()
    _unpublish(session, ref, run_id)

    with pytest.raises(NotPublishedError) as refused:
        allowed.load_table(principal, ref)

    assert str(refused.value) == _not_published_text(ref, run_id)
    denied = _prepared_catalog(session, tmp_path, _RecordThenDeny())
    with pytest.raises(PermissionDenied) as denial:
        denied.load_table(principal, ref)
    assert "published" not in str(denial.value)
    assert not isinstance(denial.value, NotPublishedError)


def test_a_table_is_loadable_again_once_it_is_published(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    ns = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=ns, name="sales")
    catalog = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[ns], tables=[ref]
    )
    _unpublish(session, ref, uuid.uuid4())
    with pytest.raises(NotPublishedError):
        catalog.load_table(principal, ref)
    _unpublish(session, ref, None)

    assert catalog.load_table(principal, ref).ref == ref


def test_no_new_catalog_method_builds_a_loaded_table_without_a_decision() -> None:
    """A new `CatalogService` method that calls `_to_loaded_table` must choose a side.

    Guards only that call: a reader's path calls `_require_published` (`load_table`), a
    writer's does not. `load_table_at_check` is a reader's path with a rule of its own in place
    of that call; `test_no_other_caller_reads_a_table_for_a_task` (`test_catalog_kept.py`)
    holds its reason. `commit_table` builds its own `LoadedTable`, and lineage and the
    worker's `python_tasks` read metadata directly; they are not seen here.
    """
    tree = ast.parse(Path(service_module.__file__).read_text(encoding="utf-8"))
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CatalogService"
    )
    callers = sorted(
        method.name
        for method in service.body
        if isinstance(method, ast.FunctionDef)
        and method.name != "_to_loaded_table"
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "_to_loaded_table"
            for call in ast.walk(method)
        )
    )
    assert callers == [
        # `describe_table` reads through it, then adds the descriptions.
        "_describe_row",
        # A writer's: it reads only the snapshot id, to refuse a create pin on a table that has one.
        "_refuse_create_pin_on_snapshot",
        "append",
        # A writer's: it compares the pin and the schema; returns whether it creates the table.
        "check_can_load",
        # A writer's: a create whose commit raised but landed returns the table it created.
        "create_table",
        # A writer's: it reads the open versions to compare them and returns counts only.
        "history",
        "load_table",
        # A reader that names one snapshot a check result recorded, never the current state.
        "load_table_at_check",
        "maintain",
        # A writer's: it reads the rows its keys match to compare them and returns counts only.
        "merge",
        "read_watermark",
        "rename_table",
        # The table it restored, after the same two rights as a rename.
        "undrop_table",
        "write_batches",
    ]


def test_a_table_that_is_not_published_still_exists_and_is_still_listed(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    ns = NamespacePath.from_parts(["gold"])
    ref = TableRef(environment="dev", namespace=ns, name="sales")
    catalog = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[ns], tables=[ref]
    )
    _unpublish(session, ref, uuid.uuid4())

    assert catalog.table_exists(principal, ref) is True
    assert catalog.list_tables(principal, "dev", ns) == [ref]
