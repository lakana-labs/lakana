"""`describe_table`: one read path for managed tables, external tables and views."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import _prepared_catalog, python_task_run
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    Securable,
    SecurableKind,
)
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.model_metadata import write_model_entry
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    CheckOutcome,
    CheckResult,
    Environment,
    Principal,
    PrincipalType,
    Table,
    WriteMode,
)
from lakana_core.storage.resolver import StorageResolver


class _DenySecurable:
    """Authorizes everything except one specific securable's `READ_TABLE`.

    Parameters
    ----------
    denied
        The one securable `authorize`/`permits` refuse.
    """

    def __init__(self, denied: Securable) -> None:
        self._denied = denied

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse `READ_TABLE` on the denied securable; permit everything else.

        Parameters
        ----------
        principal
            The acting principal (unused; this stub denies by securable
            alone, not by identity).
        action
            The action requested.
        securable
            The object acted upon.

        Raises
        ------
        PermissionDenied
            If `securable` is the denied one and `action` is `READ_TABLE`.
        """
        if action is Action.READ_TABLE and securable == self._denied:
            raise PermissionDenied("denied")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would, as a boolean.

        Parameters
        ----------
        principal
            The acting principal.
        action
            The action requested.
        securable
            The object acted upon.

        Returns
        -------
        bool
            `False` if `authorize` would raise, else `True`.
        """
        try:
            self.authorize(principal, action, securable)
        except PermissionDenied:
            return False
        return True


def test_describe_table_covers_every_kind(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A managed table has metadata; a view has SQL and references; an external has neither."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([1], pa.int64())})
    orders = TableRef.from_dotted("dev", "bronze.orders")
    catalog.write_batches(
        principal, orders, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
    )
    view = TableRef.from_dotted("dev", "gold.v")
    catalog.create_or_replace_view(
        principal,
        view,
        sql="SELECT id FROM bronze.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    external = TableRef.from_dotted("dev", "raw.events")
    catalog.create_external_table(
        principal, external, location_pattern="raw/events/*.parquet", format="parquet"
    )

    managed = catalog.describe_table(principal, orders)
    described_view = catalog.describe_table(principal, view)
    described_external = catalog.describe_table(principal, external)

    assert (managed.kind, managed.sql, managed.references) == ("TABLE", None, ())
    assert managed.metadata_location is not None
    assert [c.name for c in managed.columns] == ["id"]
    assert described_view.kind == "VIEW"
    assert described_view.metadata_location is None
    assert described_view.sql == "SELECT id FROM bronze.orders"
    assert described_view.references == ((str(managed.id), "bronze.orders"),)
    assert described_view.columns == [ColumnSpec(name="id", type="long", nullable=True)]
    assert described_view.hidden_references == 0
    assert described_external.kind == "EXTERNAL"
    assert described_external.metadata_location is None
    assert [c.name for c in described_external.columns] == ["id"]
    assert (managed.file_format, described_view.file_format) == (None, None)
    assert described_external.file_format == "parquet"


def test_describe_table_hides_a_views_sql_from_a_reader_missing_a_reference(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """READ on the view alone must not disclose a reference the reader can't READ.

    A `gold.v` reader without READ on `bronze.orders` gets no SQL and no
    references — just a count — even though the view's row still names
    `bronze.orders`.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([1], pa.int64())})
    orders = TableRef.from_dotted("dev", "bronze.orders")
    catalog.write_batches(
        principal, orders, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
    )
    view = TableRef.from_dotted("dev", "gold.v")
    catalog.create_or_replace_view(
        principal,
        view,
        sql="SELECT id FROM bronze.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    denied = Securable.table_scope("dev", NamespacePath.from_parts(["bronze"]), "orders")
    reader = CatalogService(session, StorageResolver(session), _DenySecurable(denied))

    described = reader.describe_table(principal, view)

    assert described.kind == "VIEW"
    assert described.sql is None
    assert described.references == ()
    assert described.hidden_references == 1
    assert [c.name for c in described.columns] == ["id"]


def test_describe_table_counts_a_dropped_reference_as_hidden(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A reference dropped since the view was created can't be read, so it is withheld."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([1], pa.int64())})
    orders = TableRef.from_dotted("dev", "bronze.orders")
    catalog.write_batches(
        principal, orders, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
    )
    view = TableRef.from_dotted("dev", "gold.v")
    catalog.create_or_replace_view(
        principal,
        view,
        sql="SELECT id FROM bronze.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )
    catalog.drop_table(principal, orders)

    described = catalog.describe_table(principal, view)

    assert (described.sql, described.references, described.hidden_references) == (None, (), 1)


def _catalog_with_orders(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> CatalogService:
    """Build a catalog on `dev` whose `bronze.orders` has been created empty.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        Directory backing the environment's storage.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    data = pa.table({"id": pa.array([], pa.int64())})
    catalog.write_batches(
        principal,
        TableRef.from_dotted("dev", "bronze.orders"),
        mode=WriteMode.CREATE,
        schema=data.schema,
        batches=data.to_batches(),
    )
    return catalog


def test_a_table_that_is_not_published_is_described_with_its_state(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog_with_orders(session, principal, tmp_path)
    ref = TableRef.from_dotted("dev", "bronze.orders")
    run_id = uuid.uuid4()
    session.execute(update(Table).where(Table.name == "orders").values(unpublished_run_id=run_id))
    session.commit()

    described = catalog.describe_table(principal, ref)

    assert described.state == "not_published"
    assert described.state_message == (
        "`bronze.orders` has not been published: its first build did not finish, "
        f"or failed a check (run {run_id}). It is published by the next run that builds it "
        "and passes its checks."
    )
    assert described.snapshot_id is None
    assert [c.name for c in described.columns] == ["id"]


def test_a_published_table_has_no_message(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog_with_orders(session, principal, tmp_path)

    described = catalog.describe_table(principal, TableRef.from_dotted("dev", "bronze.orders"))

    assert (described.state, described.state_message) == ("published", None)


def test_a_view_has_the_default_state(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog_with_orders(session, principal, tmp_path)
    view = TableRef.from_dotted("dev", "gold.v")
    catalog.create_or_replace_view(
        principal,
        view,
        sql="SELECT id FROM bronze.orders",
        references=catalog.resolve_lineage_sources(principal, "dev", ["bronze.orders"]),
        columns=[ColumnSpec(name="id", type="long", nullable=True)],
    )

    described = catalog.describe_table(principal, view)

    assert (described.state, described.state_message) == ("published", None)


def test_a_kept_table_is_described_with_its_state(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog_with_orders(session, principal, tmp_path)
    ref = TableRef.from_dotted("dev", "bronze.orders")
    run_id = uuid.uuid4()
    session.execute(
        update(Table)
        .where(Table.name == "orders")
        .values(kept_run_id=run_id, kept_task_run_id=uuid.uuid4(), kept_check="bronze.orders/a")
    )
    session.commit()

    described = catalog.describe_table(principal, ref)

    assert described.state == "kept"
    assert described.state_message == (
        f"Kept after a failed check (`bronze.orders/a`, run {run_id}): the table holds "
        "the data that failed. Tasks do not read it until its checks pass."
    )
    assert [c.name for c in described.columns] == ["id"]


def test_a_table_that_is_not_published_stays_not_published_with_a_note(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _catalog_with_orders(session, principal, tmp_path)
    run_id = uuid.uuid4()
    session.execute(
        update(Table)
        .where(Table.name == "orders")
        .values(
            unpublished_run_id=run_id,
            kept_run_id=uuid.uuid4(),
            kept_task_run_id=uuid.uuid4(),
            kept_check="bronze.orders/a",
        )
    )
    session.commit()

    described = catalog.describe_table(principal, TableRef.from_dotted("dev", "bronze.orders"))

    assert described.state == "not_published"
    assert described.state_message == (
        "`bronze.orders` has not been published: its first build did not finish, "
        f"or failed a check (run {run_id}). It is published by the next run that builds it "
        "and passes its checks."
    )


_GOLD = NamespacePath.from_parts(["gold"])
_FACT_GRAIN = "One row per store and day."


def _fact_entry(*references: tuple[str, str]) -> dict[str, object]:
    """Build the manifest entry of a fact keyed by date and store.

    Parameters
    ----------
    *references
        ``(column, target)`` pairs; each target column is ``store_id``.

    Returns
    -------
    dict
        The entry, as a deploy stores it.
    """
    return {
        "write": "replace",
        "key": ["date", "store_id"],
        "model": {
            "kind": "fact",
            "grain": _FACT_GRAIN,
            "references": [
                {"column": column, "target": target, "target_column": "store_id"}
                for column, target in references
            ],
        },
    }


class Star:
    """A small star in `dev`: `gold.dim_store` and `gold.fact_sales`, which references it.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    admin
        The principal that sets it up.
    """

    def __init__(self, session: Session, tmp_path: Path, admin: PrincipalRef) -> None:
        self.session = session
        self.admin = admin
        self.dim_ref = TableRef("dev", _GOLD, "dim_store")
        self.fact_ref = TableRef("dev", _GOLD, "fact_sales")
        _prepared_catalog(
            session,
            tmp_path,
            AllowAllAuthorization(),
            namespaces=[_GOLD],
            tables=[self.dim_ref, self.fact_ref],
        )
        self.write_model(
            "dim_store",
            {
                "write": "replace",
                "key": ["store_id"],
                "model": {"kind": "dimension", "grain": "One row per store."},
            },
        )
        self.write_model("fact_sales", _fact_entry(("store_id", "gold.dim_store")))
        self.record_generated("gold.fact_sales", "key_unique")

    def row(self, name: str) -> Table:
        """Find a table of `gold` by name.

        Parameters
        ----------
        name
            The table's name.

        Returns
        -------
        Table
            Its row.
        """
        return self.session.scalars(select(Table).where(Table.name == name)).one()

    def write_model(self, name: str, entry: dict[str, object]) -> None:
        """Write a manifest entry's model onto a table, as a deploy does, and commit.

        Parameters
        ----------
        name
            The table's name.
        entry
            The manifest entry.
        """
        write_model_entry(self.session, self.row(name), entry)
        self.session.commit()

    def catalog(self, authz: AuthorizationService | None = None) -> CatalogService:
        """Build a catalog over the star.

        Parameters
        ----------
        authz
            The authorization; the real grants when `None`.

        Returns
        -------
        CatalogService
            The catalog.
        """
        return CatalogService(
            self.session, StorageResolver(self.session), authz or GrantAuthorization(self.session)
        )

    def reader_of(self, *tables: str) -> PrincipalRef:
        """Persist a user holding the viewer role on some tables of `gold` only.

        Parameters
        ----------
        *tables
            Dotted names of the tables it may read.

        Returns
        -------
        PrincipalRef
            The user.
        """
        name = f"reader-{uuid.uuid4().hex[:8]}"
        ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
        self.session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
        self.session.commit()
        grants = GrantService(self.session, AllowAllAuthorization())
        for dotted in tables:
            namespace, _, table = dotted.rpartition(".")
            grants.grant(
                self.admin,
                principal=name,
                role=Role.VIEWER,
                kind=SecurableKind.TABLE,
                environment="dev",
                namespace=namespace,
                table=table,
            )
        return ref

    def namespace_reader(self) -> PrincipalRef:
        """Persist a user holding the viewer role on the namespace `gold`.

        Returns
        -------
        PrincipalRef
            The user.
        """
        name = f"ns-reader-{uuid.uuid4().hex[:8]}"
        ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
        self.session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
        self.session.commit()
        GrantService(self.session, AllowAllAuthorization()).grant(
            self.admin,
            principal=name,
            role=Role.VIEWER,
            kind=SecurableKind.NAMESPACE,
            environment="dev",
            namespace="gold",
        )
        return ref

    def record_generated(self, table: str, check: str) -> None:
        """Record one passed result of a generated check of a table.

        Parameters
        ----------
        table
            The table's dotted name.
        check
            The check's short name.
        """
        task = python_task_run(self.session, self.admin.id, asset=table)
        environment = self.session.scalars(select(Environment)).one()
        now = datetime.now(UTC)
        self.session.add(
            CheckResult(
                id=uuid.uuid4(),
                run_id=task.run_id,
                task_run_id=task.id,
                environment_id=environment.id,
                check_name=f"{table}/{check}",
                check_path="models/gold/fact_sales.sql:2",
                table_name=table,
                table_id=self.row(table.rpartition(".")[2]).id,
                on_failure="block",
                outcome=CheckOutcome.PASSED,
                failing_rows=0,
                started_at=now - timedelta(seconds=1),
                finished_at=now,
                generated=True,
            )
        )
        self.session.commit()


@pytest.fixture
def star(session: Session, tmp_path: Path, principal: PrincipalRef) -> Star:
    """Provide the star of `gold.dim_store` and `gold.fact_sales`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The administrator that sets it up.

    Returns
    -------
    Star
        The star.
    """
    return Star(session, tmp_path, principal)


def test_describe_table_carries_the_model_and_its_generated_checks(star: Star) -> None:
    described = star.catalog(AllowAllAuthorization()).describe_table(star.admin, star.fact_ref)
    assert described.model is not None
    assert (described.model.kind, described.model.grain) == ("fact", _FACT_GRAIN)
    assert (described.model.key, described.model.key_none) == (("date", "store_id"), False)
    (ref,) = described.model.references
    assert (ref.id, ref.table, ref.column) == (
        "gold.fact_sales/ref_store_id",
        "gold.fact_sales",
        "store_id",
    )
    assert (ref.target, ref.target_column, ref.target_exists) == (
        "gold.dim_store",
        "store_id",
        True,
    )
    assert described.model.hidden_references == 0
    assert [(c.check, c.generated) for c in described.model.checks] == [("key_unique", True)]


def test_a_table_without_a_model_has_none(star: Star) -> None:
    star.write_model("fact_sales", {"write": "replace"})
    described = star.catalog(AllowAllAuthorization()).describe_table(star.admin, star.fact_ref)
    assert described.model is None


def test_key_none_reads_as_no_columns(star: Star) -> None:
    entry = _fact_entry()
    entry["key"] = []
    cast("dict[str, object]", entry["model"])["key_none"] = True
    star.write_model("fact_sales", entry)
    model = star.catalog(AllowAllAuthorization()).describe_table(star.admin, star.fact_ref).model
    assert model is not None
    assert (model.key, model.key_none, model.references) == ((), True, ())
    assert model.checks == ()


def test_a_generated_check_the_header_no_longer_declares_is_not_shown(star: Star) -> None:
    """A stored `ref_region_id` result stays, but the current model has no such reference."""
    star.record_generated("gold.fact_sales", "ref_region_id")
    star.record_generated("gold.fact_sales", "ref_store_id")
    model = star.catalog(AllowAllAuthorization()).describe_table(star.admin, star.fact_ref).model
    assert model is not None
    assert [c.check for c in model.checks] == ["key_unique", "ref_store_id"]


def test_a_reference_to_a_table_the_reader_cannot_read_is_withheld_and_counted(
    star: Star,
) -> None:
    star.record_generated("gold.fact_sales", "ref_store_id")
    reader = star.reader_of("gold.fact_sales")
    model = star.catalog().describe_table(reader, star.fact_ref).model
    assert model is not None
    assert (model.references, model.hidden_references) == ((), 1)
    # Its generated check would name the reference's column next to an error that may name the
    # target; it goes with the reference.
    assert [c.check for c in model.checks] == ["key_unique"]


def test_a_missing_target_and_an_unreadable_target_look_the_same_to_a_reader(
    star: Star,
) -> None:
    star.write_model(
        "fact_sales",
        _fact_entry(("store_id", "gold.dim_store"), ("region_id", "gold.dim_gone")),
    )
    reader = star.reader_of("gold.fact_sales")
    model = star.catalog().describe_table(reader, star.fact_ref).model
    assert model is not None
    assert (model.references, model.hidden_references) == ((), 2)


def test_a_reader_of_the_namespace_sees_that_a_target_is_missing(star: Star) -> None:
    star.write_model(
        "fact_sales",
        _fact_entry(("store_id", "gold.dim_store"), ("region_id", "gold.dim_gone")),
    )
    model = star.catalog().describe_table(star.namespace_reader(), star.fact_ref).model
    assert model is not None
    assert [(r.target, r.target_exists) for r in model.references] == [
        ("gold.dim_gone", False),
        ("gold.dim_store", True),
    ]
    assert model.hidden_references == 0


def test_table_references_lists_both_directions(star: Star) -> None:
    catalog = star.catalog(AllowAllAuthorization())
    of_dim = catalog.table_references(star.admin, star.dim_ref)
    of_fact = catalog.table_references(star.admin, star.fact_ref)
    assert of_dim.table == "gold.dim_store"
    assert (of_dim.outgoing, of_dim.hidden_outgoing, of_dim.hidden_incoming) == ((), 0, 0)
    (incoming,) = of_dim.incoming
    assert (incoming.id, incoming.table, incoming.column) == (
        "gold.fact_sales/ref_store_id",
        "gold.fact_sales",
        "store_id",
    )
    assert (incoming.target, incoming.target_column, incoming.target_exists) == (
        "gold.dim_store",
        "store_id",
        True,
    )
    assert [r.id for r in of_fact.outgoing] == ["gold.fact_sales/ref_store_id"]
    assert of_fact.incoming == ()


def test_incoming_references_count_hidden_sources(star: Star) -> None:
    reader = star.reader_of("gold.dim_store")
    refs = star.catalog().table_references(reader, star.dim_ref)
    assert refs.incoming == ()
    assert refs.hidden_incoming == 1


def test_outgoing_references_count_hidden_targets(star: Star) -> None:
    reader = star.reader_of("gold.fact_sales")
    refs = star.catalog().table_references(reader, star.fact_ref)
    assert (refs.outgoing, refs.hidden_outgoing) == ((), 1)


def test_table_references_needs_read_table_on_the_table(star: Star) -> None:
    reader = star.reader_of("gold.fact_sales")
    with pytest.raises(PermissionDenied):
        star.catalog().table_references(reader, star.dim_ref)


def test_a_renamed_target_leaves_its_references_with_its_old_name(star: Star) -> None:
    """References are held by the name the header writes, so they stay with that name.

    The header and its generated check name `gold.dim_store`; a table renamed away through the
    API no longer answers to it, and a new table that takes the name does.
    """
    catalog = star.catalog(AllowAllAuthorization())
    shop = TableRef("dev", _GOLD, "dim_shop")
    catalog.rename_table(star.admin, star.dim_ref, shop)

    model = catalog.describe_table(star.admin, star.fact_ref).model
    assert model is not None
    (outgoing,) = model.references
    assert (outgoing.target, outgoing.target_exists) == ("gold.dim_store", False)
    assert catalog.table_references(star.admin, shop).incoming == ()

    catalog.create_table(
        star.admin, star.dim_ref, schema=Schema(NestedField(1, "id", LongType(), required=True))
    )
    assert [r.id for r in catalog.table_references(star.admin, star.dim_ref).incoming] == [
        "gold.fact_sales/ref_store_id"
    ]
    model = catalog.describe_table(star.admin, star.fact_ref).model
    assert model is not None
    (outgoing,) = model.references
    assert outgoing.target_exists is True


def test_a_renamed_source_keeps_its_model_under_its_new_name(star: Star) -> None:
    """Known limit: the model stays on the renamed table until it is dropped or redeployed.

    The rows are keyed by the table's id, so the reference is listed once, under the name the
    table has now; a later table built under the old name gets a model of its own.
    """
    catalog = star.catalog(AllowAllAuthorization())
    renamed = TableRef("dev", _GOLD, "fact_sales_old")
    catalog.rename_table(star.admin, star.fact_ref, renamed)

    model = catalog.describe_table(star.admin, renamed).model
    assert model is not None
    assert [r.id for r in model.references] == ["gold.fact_sales_old/ref_store_id"]
    assert [r.id for r in catalog.table_references(star.admin, star.dim_ref).incoming] == [
        "gold.fact_sales_old/ref_store_id"
    ]

    catalog.create_table(
        star.admin, star.fact_ref, schema=Schema(NestedField(1, "id", LongType(), required=True))
    )
    star.write_model("fact_sales", _fact_entry(("store_id", "gold.dim_store")))
    assert [r.id for r in catalog.table_references(star.admin, star.dim_ref).incoming] == [
        "gold.fact_sales/ref_store_id",
        "gold.fact_sales_old/ref_store_id",
    ]


def test_a_stored_target_that_is_no_table_name_is_counted_as_hidden(star: Star) -> None:
    """A stored manifest is untrusted: a target that names no table is withheld, not an error."""
    star.write_model("fact_sales", _fact_entry(("store_id", "nodot")))
    model = star.catalog(AllowAllAuthorization()).describe_table(star.admin, star.fact_ref).model
    assert model is not None
    assert (model.references, model.hidden_references) == ((), 1)


def test_a_measures_check_result_shows_while_the_header_declares_measures(star: Star) -> None:
    star.record_generated("gold.fact_sales", "measures")
    catalog = star.catalog(AllowAllAuthorization())
    before = catalog.describe_table(star.admin, star.fact_ref).model
    assert before is not None and [c.check for c in before.checks] == ["key_unique"]
    entry = _fact_entry(("store_id", "gold.dim_store"))
    cast("dict[str, object]", entry["model"])["measures"] = [
        {
            "column": "amount",
            "aggregation": "sum",
            "additivity": "additive",
            "not_over": [],
            "unit": None,
            "currency": "EUR",
            "label": None,
            "description": None,
        }
    ]
    star.write_model("fact_sales", entry)
    after = catalog.describe_table(star.admin, star.fact_ref).model
    assert after is not None and [c.check for c in after.checks] == ["key_unique", "measures"]
