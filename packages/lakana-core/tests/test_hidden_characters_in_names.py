"""An invisible or line-breaking character is refused in a new namespace, table or column name.

A format character (Unicode ``Cf``, such as U+202E RIGHT-TO-LEFT OVERRIDE), a control
character (``Cc``), a surrogate (``Cs``) and the line and paragraph separators (U+2028,
U+2029) can make a name look like another one wherever it is shown. Only a name being created
is refused; a stored one keeps working.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC
from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadata, new_table_metadata
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER
from pyiceberg.table.update import AddSchemaUpdate, SetCurrentSchemaUpdate
from pyiceberg.types import ListType, LongType, NestedField, StringType, StructType
from sqlalchemy import update
from sqlalchemy.orm import Session
from test_table_location_sweep import _schema
from test_table_writes import _catalog, _ref, _write

from lakana_core.auth import PrincipalRef
from lakana_core.catalog.json_schema import ColumnSpec
from lakana_core.catalog.metadata import apply_updates, build_table_metadata
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef, refuse_hidden_characters
from lakana_core.models import Table, WriteMode
from lakana_core.projects.project_config import unsafe_identifier

_RLO = "\u202e"


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
    ("character", "code"),
    [
        ("\u202e", "U+202E"),
        ("\u200b", "U+200B"),
        ("\xad", "U+00AD"),
        ("\u2028", "U+2028"),
        ("\u2029", "U+2029"),
        ("\x1b", "U+001B"),
        ("\n", "U+000A"),
        ("\ud800", "U+D800"),
    ],
)
def test_a_new_name_with_a_hidden_character_is_refused_and_names_it(
    character: str, code: str
) -> None:
    """The refusal names the character as U+XXXX, its position and what to do."""
    with pytest.raises(ValidationError) as refused:
        refuse_hidden_characters(f"ab{character}c", what="Table name")
    message = str(refused.value)
    assert code in message
    assert "character 3" in message
    assert "Table name" in message
    assert "Choose a name" in message
    assert character not in message


@pytest.mark.parametrize(
    "name", ["orders", "Bestellungen_2026", "commandes_\xe9t\xe9", "\u6ce8\u6587", "a b"]
)
def test_visible_names_are_accepted(name: str) -> None:
    """Letters of any script, digits, ``_`` and a space are fine."""
    refuse_hidden_characters(name, what="Table name")


def test_the_project_rule_uses_the_same_characters() -> None:
    """A repository file's identifiers and a new catalog name refuse the same characters."""
    for character in ("\u202e", "\u2028", "\u2029", "\x1b", "\ud800"):
        assert unsafe_identifier(f"a{character}")
        with pytest.raises(ValidationError):
            refuse_hidden_characters(f"a{character}", what="Column name")


def test_every_way_of_creating_a_name_refuses_a_hidden_character(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A table, an external table, a view, a namespace, a rename target and an undrop target."""
    catalog.create_table(principal, _ref("gold.orders"), schema=_schema())
    new = _ref(f"gold.n{_RLO}")
    attempts = [
        lambda: catalog.create_table(principal, new, schema=_schema()),
        lambda: catalog.create_external_table(
            principal, _ref(f"bronze.n{_RLO}"), location_pattern="raw/x/*.csv", format="csv"
        ),
        lambda: catalog.create_or_replace_view(
            principal, new, sql="SELECT 1 AS id", references=[], columns=[]
        ),
        lambda: catalog.create_namespace(
            principal, "dev", NamespacePath.from_parts(["gold", f"n{_RLO}"])
        ),
        lambda: catalog.rename_table(principal, _ref("gold.orders"), new),
        lambda: catalog.undrop_table(principal, _ref("gold.orders"), new_name=f"n{_RLO}"),
    ]
    for attempt in attempts:
        with pytest.raises(ValidationError, match="U\\+202E"):
            attempt()
    assert catalog.load_table(principal, _ref("gold.orders")).ref.name == "orders"


def test_a_stored_table_name_with_a_hidden_character_keeps_working(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The upgrade note's promise: an existing name is loaded and can be renamed away."""
    catalog.create_table(principal, _ref("gold.old"), schema=_schema())
    session.execute(update(Table).where(Table.name == "old").values(name=f"old{_RLO}"))
    session.commit()
    stored = _ref(f"gold.old{_RLO}")

    assert catalog.load_table(principal, stored).ref == stored
    assert catalog.rename_table(principal, stored, _ref("gold.old_fixed")).ref.name == "old_fixed"


def test_a_new_table_with_a_hidden_character_in_a_column_is_refused(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Nested fields are checked too; nothing is created."""
    nested = Schema(
        NestedField(1, "id", LongType(), required=False),
        NestedField(
            2,
            "items",
            ListType(
                3,
                StructType(NestedField(4, f"sku{_RLO}", StringType(), required=False)),
                element_required=False,
            ),
            required=False,
        ),
    )
    with pytest.raises(ValidationError, match=r"Column name 'sku\\u202e' .*U\+202E"):
        catalog.create_table(principal, _ref("gold.items"), schema=nested)
    with pytest.raises(ValidationError, match="U\\+2028"):
        build_table_metadata(
            schema=Schema(NestedField(1, "a\u2028b", LongType(), required=False)),
            location="file:///x",
            partition_spec=None,
            sort_order=None,
            properties={},
        )


def _metadata_with_column(name: str) -> TableMetadata:
    """Build metadata whose one column was stored by an older release under `name`.

    Parameters
    ----------
    name
        The stored column name.

    Returns
    -------
    TableMetadata
        The metadata, built without Lakana's check.
    """
    return new_table_metadata(
        schema=Schema(NestedField(1, name, LongType(), required=False)),
        partition_spec=UNPARTITIONED_PARTITION_SPEC,
        sort_order=UNSORTED_SORT_ORDER,
        location="file:///x",
        properties={},
    )


def test_an_added_schema_refuses_a_new_hidden_column_and_keeps_a_stored_one() -> None:
    """A schema change through the Iceberg REST catalog or a write: only new names are judged."""
    base = _metadata_with_column(f"old{_RLO}")
    kept = Schema(
        NestedField(1, f"old{_RLO}", LongType(), required=False),
        NestedField(2, "plain", LongType(), required=False),
        schema_id=1,
    )
    updated = apply_updates(
        base, [AddSchemaUpdate(schema_=kept), SetCurrentSchemaUpdate(schema_id=-1)]
    )
    assert [f.name for f in updated.schema().fields] == [f"old{_RLO}", "plain"]

    added = Schema(
        NestedField(1, f"old{_RLO}", LongType(), required=False),
        NestedField(2, "new\u2029", LongType(), required=False),
        schema_id=1,
    )
    with pytest.raises(ValidationError, match=r"Column name 'new\\u2029' .*U\+2029"):
        apply_updates(base, [AddSchemaUpdate(schema_=added)])


def test_a_write_that_evolves_the_schema_refuses_a_hidden_column(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A deploy's or load's new column goes through the same check."""
    _write(catalog, principal, "gold.daily", WriteMode.CREATE, pa.table({"id": [1]}))
    with pytest.raises(ValidationError, match="U\\+202E"):
        _write(
            catalog,
            principal,
            "gold.daily",
            WriteMode.REPLACE,
            pa.table({"id": [5], f"amount{_RLO}": [2.5]}),
            evolve_schema=True,
        )
    with pytest.raises(ValidationError, match="U\\+202E"):
        _write(catalog, principal, "gold.fresh", WriteMode.CREATE, pa.table({f"x{_RLO}": [1]}))


def test_a_view_refuses_a_new_hidden_column_and_keeps_a_stored_one(
    session: Session, catalog: CatalogService, principal: PrincipalRef
) -> None:
    """A replaced view may keep the column names it already had."""
    ref: TableRef = _ref("gold.v")
    with pytest.raises(ValidationError, match="U\\+202E"):
        catalog.create_or_replace_view(
            principal,
            ref,
            sql="SELECT 1 AS x",
            references=[],
            columns=[ColumnSpec(name=f"x{_RLO}", type="int", nullable=True)],
        )
    catalog.create_or_replace_view(
        principal,
        ref,
        sql="SELECT 1 AS x",
        references=[],
        columns=[ColumnSpec(name="x", type="int", nullable=True)],
    )
    row = session.query(Table).filter(Table.name == "v").one()
    assert row.view_definition is not None
    row.view_definition = {**row.view_definition, "columns": [{"name": f"x{_RLO}"}]}
    session.commit()

    catalog.create_or_replace_view(
        principal,
        ref,
        sql="SELECT 1 AS x",
        references=[],
        columns=[ColumnSpec(name=f"x{_RLO}", type="int", nullable=True)],
    )


def test_an_external_table_refuses_a_hidden_character_in_a_column_of_its_files(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The column names come from the caller's own files: they are new names too."""
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["raw"]))
    (tmp_path / "raw" / "events").mkdir(parents=True)
    pq.write_table(pa.table({f"id{_RLO}": [1]}), tmp_path / "raw" / "events" / "part-0.parquet")
    events = TableRef.from_dotted("dev", "raw.events")

    with pytest.raises(ValidationError, match=r"Column name 'id\\u202e' .*U\+202E"):
        catalog.create_external_table(
            principal, events, location_pattern="raw/events/*.parquet", format="parquet"
        )
    with pytest.raises(NotFoundError):
        catalog.load_table(principal, events)
