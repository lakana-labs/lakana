"""A new namespace, table, column or artifact name may not start or end with whitespace (#652).

``" gold"`` and ``"orders "`` look like ``gold`` and ``orders`` in every list and are almost
always a typing mistake. Only a name being created is refused, next to the refusal of
invisible characters (#593); a stored one stays readable, renamable and droppable.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from core_test_helpers import ARTIFACT_REF, artifact_document, artifact_service
from pyiceberg.schema import Schema
from pyiceberg.table.update import AddSchemaUpdate
from pyiceberg.types import LongType, NestedField
from sqlalchemy import update
from sqlalchemy.orm import Session
from test_hidden_characters_in_names import _metadata_with_column
from test_table_location_sweep import _schema
from test_table_writes import _catalog, _ref, _write

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.metadata import apply_updates
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef, refuse_outer_whitespace
from lakana_core.models import Namespace, Table, WriteMode


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Create `dev` at `tmp_path` and return a catalog over it.

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
        The catalog.
    """
    return _catalog(session, principal, tmp_path)


@pytest.mark.parametrize(
    ("name", "where", "fixed"),
    [
        (" gold", "starts", "gold"),
        ("orders ", "ends", "orders"),
        ("  orders  ", "starts and ends", "orders"),
        ("orders\xa0", "ends", "orders"),
        ("　orders", "starts", "orders"),
    ],
)
def test_a_new_name_with_outer_whitespace_is_refused_with_the_cause_and_the_next_step(
    name: str, where: str, fixed: str
) -> None:
    """The sentence names the name, where the space is, and the name to use instead."""
    with pytest.raises(ValidationError) as refused:
        refuse_outer_whitespace(name, what="Table name")
    message = str(refused.value)
    assert message.startswith("Table name '")
    assert f"{where} with a space" in message
    assert f"Use '{fixed}'" in message


@pytest.mark.parametrize("name", ["orders", "a b", "daily orders 2026", "注 文"])
def test_a_space_inside_a_name_is_fine(name: str) -> None:
    """Only the ends are judged."""
    refuse_outer_whitespace(name, what="Table name")


def test_every_way_of_creating_a_name_refuses_outer_whitespace(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A table, an external table, a view, a namespace, a rename target and an undrop target."""
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    new = _ref("gold.orders2 ")
    attempts = [
        lambda: catalog.create_table(principal, new, schema=_schema()),
        lambda: catalog.create_external_table(
            principal, _ref("bronze.raw "), location_pattern="raw/x/*.csv", format="csv"
        ),
        lambda: catalog.create_or_replace_view(
            principal, new, sql="SELECT 1 AS id", references=[], columns=[]
        ),
        lambda: catalog.create_namespace(principal, "dev", NamespacePath.from_parts([" silver"])),
        lambda: catalog.create_namespace(
            principal, "dev", NamespacePath.from_parts([" silver", "daily"])
        ),
        lambda: catalog.rename_table(principal, _ref("gold.orders"), new),
        lambda: catalog.undrop_table(principal, _ref("gold.orders"), new_name=" orders"),
    ]
    for attempt in attempts:
        with pytest.raises(ValidationError, match="with a space"):
            attempt()
    assert catalog.load_table(principal, _ref("gold.orders")).ref.name == "orders"


def test_a_stored_name_with_outer_whitespace_stays_readable_renamable_and_droppable(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Names an older release accepted keep working, and a child may be created under one."""
    catalog.create_table(principal, _ref("gold.old"), schema=_schema())
    catalog.create_table(principal, _ref("gold.gone"), schema=_schema())
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["scratch"]))
    session.execute(update(Table).where(Table.name == "old").values(name="old "))
    session.execute(update(Table).where(Table.name == "gone").values(name=" gone"))
    session.execute(update(Namespace).where(Namespace.path == "scratch").values(path=" scratch"))
    session.commit()
    stored = _ref("gold.old ")

    assert catalog.load_table(principal, stored).ref == stored
    assert catalog.rename_table(principal, stored, _ref("gold.old_fixed")).ref.name == "old_fixed"
    catalog.drop_table(principal, _ref("gold. gone"))
    with pytest.raises(NotFoundError):
        catalog.load_table(principal, _ref("gold. gone"))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts([" scratch", "daily"]))
    catalog.drop_namespace(principal, "dev", NamespacePath.from_parts([" scratch", "daily"]))
    catalog.drop_namespace(principal, "dev", NamespacePath.from_parts([" scratch"]))


def test_a_new_column_with_outer_whitespace_is_refused(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A new table's column, a write's new column and a view's new column."""
    schema = Schema(NestedField(1, "id ", LongType(), required=False))
    with pytest.raises(ValidationError, match=r"Column name 'id ' ends with a space"):
        catalog.create_table(principal, _ref("gold.items"), schema=schema)
    with pytest.raises(ValidationError, match="with a space"):
        _write(catalog, principal, "gold.fresh", WriteMode.CREATE, pa.table({" x": [1]}))
    with pytest.raises(ValidationError, match="with a space"):
        catalog.create_or_replace_view(
            principal,
            _ref("gold.v"),
            sql="SELECT 1 AS x",
            references=[],
            columns=[ColumnSpec(name="x ", type="int", nullable=True)],
        )


def test_an_added_schema_refuses_a_new_spaced_column_and_keeps_a_stored_one() -> None:
    """A schema change through the Iceberg REST catalog or a write: only new names are judged."""
    base = _metadata_with_column("old ")
    kept = Schema(
        NestedField(1, "old ", LongType(), required=False),
        NestedField(2, "plain", LongType(), required=False),
        schema_id=1,
    )
    apply_updates(base, [AddSchemaUpdate(schema_=kept)])
    added = Schema(
        NestedField(1, "old ", LongType(), required=False),
        NestedField(2, " new", LongType(), required=False),
        schema_id=1,
    )
    with pytest.raises(ValidationError, match=r"Column name ' new' starts with a space"):
        apply_updates(base, [AddSchemaUpdate(schema_=added)])


def test_an_external_table_refuses_a_spaced_column_of_its_files(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The column names come from the caller's own files: they are new names too."""
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({"id ": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    events = TableRef.from_dotted("dev", "raw.events")

    with pytest.raises(ValidationError, match=r"Column name 'id ' ends with a space"):
        catalog.create_external_table(
            principal, events, location_pattern="raw/events/*.parquet", format="parquet"
        )


def test_a_new_artifact_name_with_outer_whitespace_is_refused(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """An artifact is listed among a namespace's tables, so it meets the same rule."""
    service = artifact_service(session, tmp_path)
    spaced = dataclasses.replace(ARTIFACT_REF, name="churn ")

    with pytest.raises(ValidationError, match=r"Artifact name 'churn ' ends with a space"):
        service.begin_upload(principal, spaced, artifact_document({"m": b"weights"}))
    assert session.query(Table).filter(Table.name == spaced.name).count() == 0
