"""The rows a deploy writes for labels and translations, and their guard."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from core_test_helpers import _prepared_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.catalog.translations import edit_translation, translation_rows
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import ColumnDescription, Environment, Table, TextTranslation

_GOLD = NamespacePath.from_parts(["gold"])
_SALES = TableRef("dev", _GOLD, "sales")


def _manifest(translations: dict | None = None, *, label: str | None = "Sales") -> dict:
    """Build a manifest of `gold.sales` with a label, a column label and translations.

    Parameters
    ----------
    translations
        The entry's `metadata.translations`, or `None` for none.
    label
        The table's label.

    Returns
    -------
    dict
        The manifest, with `main_language: en`.
    """
    metadata: dict = {
        "description": "One row per sale.",
        "owner": None,
        "tags": [],
        "columns": {"id": "The id."},
        "column_labels": {"id": "Id"},
    }
    if label is not None:
        metadata["label"] = label
    if translations is not None:
        metadata["translations"] = translations
    return {
        "version": 11,
        "main_language": "en",
        "assets": [{"table": "gold.sales", "metadata": metadata}],
    }


@pytest.fixture
def sales(session: Session, tmp_path: Path, principal: PrincipalRef) -> Table:
    """Create `dev` with `gold.sales (id)`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The creator.

    Returns
    -------
    Table
        The table's row.
    """
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_GOLD])
    catalog.create_table(
        principal, _SALES, schema=Schema(NestedField(1, "id", LongType(), required=True))
    )
    return session.scalars(select(Table).where(Table.name == "sales")).one()


def _env(session: Session) -> uuid.UUID:
    """Return the id of `dev`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The id.
    """
    return session.scalars(select(Environment.id).where(Environment.name == "dev")).one()


def _statements(session: Session, action: Callable[[], object]) -> list[str]:
    """Record the SQL `action` runs.

    Parameters
    ----------
    session
        Database session.
    action
        What to run.

    Returns
    -------
    list of str
        The statements.
    """
    seen: list[str] = []

    def record(conn: object, cursor: object, statement: str, *rest: object) -> None:
        """Keep one statement.

        Parameters
        ----------
        conn
            The connection.
        cursor
            The DBAPI cursor.
        statement
            The SQL.
        *rest
            The parameters, context and executemany flag.
        """
        seen.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return seen


def test_a_deploy_writes_labels_the_main_language_and_translations(
    session: Session, sales: Table
) -> None:
    """The table and column labels, the main language and one row per translated text.

    Parameters
    ----------
    session
        Database session.
    sales
        The table.
    """
    translations = {
        "fr": {"label": "Ventes", "columns": {"id": {"label": "Identifiant"}}},
        "de": {"description": "Eine Zeile je Verkauf."},
    }
    sync_project_descriptions(session, _env(session), _manifest(translations))
    session.commit()
    assert (sales.label_project, sales.text_language, sales.text_languages) == (
        "Sales",
        "en",
        ["de", "fr"],
    )
    column = session.scalars(select(ColumnDescription)).one()
    assert (column.label_project, column.description_project) == ("Id", "The id.")
    assert sorted(
        (r.language, r.subject, r.name, r.field, r.text_project)
        for r in translation_rows(session, sales)
    ) == [
        ("de", "table", "", "description", "Eine Zeile je Verkauf."),
        ("fr", "column", "id", "label", "Identifiant"),
        ("fr", "table", "", "label", "Ventes"),
    ]


def test_a_removed_translation_loses_its_project_text_and_keeps_an_edit(
    session: Session, sales: Table
) -> None:
    """A text the files no longer have goes, unless it was edited; then only the edit stays.

    Parameters
    ----------
    session
        Database session.
    sales
        The table.
    """
    sync_project_descriptions(
        session, _env(session), _manifest({"fr": {"label": "Ventes", "description": "D"}})
    )
    session.commit()
    edited = session.scalars(select(TextTranslation).where(TextTranslation.field == "label")).one()
    edited.text_edited = "Ventes (édité)"
    session.commit()
    sync_project_descriptions(session, _env(session), _manifest(None))
    session.commit()
    (left,) = translation_rows(session, sales)
    assert (left.field, left.text_project, left.text_edited) == ("label", None, "Ventes (édité)")
    assert sales.text_languages == ["fr"]


def test_a_table_without_translations_is_never_read_for_them(
    session: Session, sales: Table
) -> None:
    """Neither a deploy nor a read touches `text_translations` for a table without any.

    Parameters
    ----------
    session
        Database session.
    sales
        The table.
    """
    sync_project_descriptions(session, _env(session), _manifest(None))
    session.commit()
    seen = _statements(
        session, lambda: sync_project_descriptions(session, _env(session), _manifest(None))
    )
    assert not [s for s in seen if "text_translations" in s]
    assert translation_rows(session, sales) == []
    read = _statements(session, lambda: translation_rows(session, sales))
    assert not [s for s in read if "text_translations" in s]


def test_a_manifest_without_main_language_leaves_the_instance_language(
    session: Session, sales: Table
) -> None:
    """Without `main_language`, the table's texts are in the instance's main language.

    Parameters
    ----------
    session
        Database session.
    sales
        The table.
    """
    manifest = _manifest(None)
    del manifest["main_language"]
    sync_project_descriptions(session, _env(session), manifest)
    session.commit()
    assert sales.text_language is None


def test_an_edit_adds_a_row_and_its_reset_removes_it_again(
    session: Session, sales: Table, principal: PrincipalRef
) -> None:
    """An edit of a text without a project text makes a row; resetting it leaves none.

    Parameters
    ----------
    session
        Database session.
    sales
        The table.
    principal
        Who edits.
    """
    key = {"subject": "column", "name": "id", "language": "fr", "field": "label"}
    assert edit_translation(session, sales, **key, text="Identifiant", by=principal.id) == (0, 11)
    session.commit()
    (row,) = translation_rows(session, sales)
    assert (row.text_project, row.text_edited, row.updated_by) == (
        None,
        "Identifiant",
        principal.id,
    )
    assert sales.text_languages == ["fr"]
    assert edit_translation(session, sales, **key, text=None, by=principal.id) == (11, 0)
    session.commit()
    assert sales.text_languages is None
    assert session.scalars(select(TextTranslation)).all() == []
    assert edit_translation(session, sales, **key, text=None, by=principal.id) == (0, 0)


def test_resetting_an_edit_keeps_the_project_text(
    session: Session, sales: Table, principal: PrincipalRef
) -> None:
    """A reset of an edited deployed text keeps the row with the file's text.

    Parameters
    ----------
    session
        Database session.
    sales
        The table.
    principal
        Who edits.
    """
    sync_project_descriptions(session, _env(session), _manifest({"fr": {"label": "Ventes"}}))
    session.commit()
    key = {"subject": "table", "name": "", "language": "fr", "field": "label"}
    assert edit_translation(session, sales, **key, text="Les ventes", by=principal.id) == (0, 10)
    assert edit_translation(session, sales, **key, text=None, by=principal.id) == (10, 0)
    session.commit()
    (row,) = translation_rows(session, sales)
    assert (row.text_project, row.text_edited) == ("Ventes", None)
    assert sales.text_languages == ["fr"]


def test_a_column_label_alone_keeps_a_row_and_an_edited_label_outlives_the_header(
    session: Session, sales: Table
) -> None:
    """A label without a description makes a row; a label edit keeps it once the header drops it.

    Parameters
    ----------
    session
        Database session.
    sales
        The table.
    """
    manifest = _manifest(None)
    del manifest["assets"][0]["metadata"]["columns"]
    sync_project_descriptions(session, _env(session), manifest)
    session.commit()
    column = session.scalars(select(ColumnDescription)).one()
    assert (column.label_project, column.description_project) == ("Id", None)
    column.label_edited = "Identifier"
    session.commit()
    del manifest["assets"][0]["metadata"]["column_labels"]
    sync_project_descriptions(session, _env(session), manifest)
    session.commit()
    column = session.scalars(select(ColumnDescription)).one()
    assert (column.label_project, column.label_edited) == (None, "Identifier")


def test_a_changed_translation_updates_its_row_in_place(session: Session, sales: Table) -> None:
    """A redeploy with a new text for the same key keeps the row and changes its project text.

    Parameters
    ----------
    session
        Database session.
    sales
        The table.
    """
    sync_project_descriptions(session, _env(session), _manifest({"fr": {"label": "Ventes"}}))
    session.commit()
    (before,) = translation_rows(session, sales)
    sync_project_descriptions(session, _env(session), _manifest({"fr": {"label": "Les ventes"}}))
    session.commit()
    (after,) = translation_rows(session, sales)
    assert (after.id, after.text_project) == (before.id, "Les ventes")
