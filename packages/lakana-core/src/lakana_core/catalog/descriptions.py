"""Descriptions of tables and columns: the project's text, an edit, and which one is shown.

A table or column has up to two texts. The *project* text comes from the asset header
(`description:` and `columns:`) and is written by a deploy, a rollback, or the creation of the
table by a run. An *edit* is set through the API and is shown instead. A deploy never writes an
edit; resetting an edit makes the project text show again. Labels (`label:` and
`column_labels`) are kept the same way, and the entry's translations are written through
`lakana_core.catalog.translations`.

The same writes carry the asset's model (`kind:`, `grain:`, `key:`, `references:`) onto the table,
through `lakana_core.catalog.model_metadata`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from lakana_core.catalog.model_metadata import find_project_table, write_model_entry
from lakana_core.catalog.translations import write_translations
from lakana_core.errors import ValidationError
from lakana_core.models import (
    ColumnDescription,
    EnvironmentDeployment,
    JobVersion,
    Table,
    TableType,
)
from lakana_core.projects.manifest import entry_translations, manifest_main_language
from lakana_core.projects.project_config import MAX_DESCRIPTION_CHARS

MAX_DESCRIPTION_LENGTH = MAX_DESCRIPTION_CHARS
"""The most characters a description may have, for a table and for a column."""

DescriptionSource = Literal["project", "edited"]


@dataclass(frozen=True)
class ShownDescription:
    """The description a reader sees, and where it came from.

    Parameters
    ----------
    text
        The description.
    source
        ``"edited"`` for a text set through the API, ``"project"`` for the asset header's.
    """

    text: str
    source: DescriptionSource


def shown(project: str | None, edited: str | None) -> ShownDescription | None:
    """Choose the text a reader sees: the edit when there is one, else the project's.

    Parameters
    ----------
    project
        The project's text, if any.
    edited
        The edit, if any.

    Returns
    -------
    ShownDescription or None
        The text and its source, or `None` when there is neither.
    """
    if edited is not None:
        return ShownDescription(edited, "edited")
    if project is not None:
        return ShownDescription(project, "project")
    return None


def table_description(row: Table) -> ShownDescription | None:
    """Choose the description a table shows.

    Parameters
    ----------
    row
        The table.

    Returns
    -------
    ShownDescription or None
        What is shown, or `None` for none.
    """
    return shown(row.description_project, row.description_edited)


def column_descriptions(
    session: Session, table_id: uuid.UUID, columns: Iterable[str]
) -> dict[str, ShownDescription]:
    """Choose the description each of a table's columns shows.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    columns
        The names of the columns the table has now. The text of any other column is not shown.

    Returns
    -------
    dict
        Column name → description, for the columns that have one.
    """
    present = set(columns)
    result: dict[str, ShownDescription] = {}
    for row in session.scalars(
        select(ColumnDescription).where(ColumnDescription.table_id == table_id)
    ):
        chosen = shown(row.description_project, row.description_edited)
        if row.column in present and chosen is not None:
            result[row.column] = chosen
    return result


def check_text(text: str | None, what: str) -> str | None:
    """Refuse a description that is blank or too long.

    Parameters
    ----------
    text
        The new description, or `None` to reset the edit.
    what
        What is described, for the message (``"table silver.orders"``).

    Returns
    -------
    str or None
        `text`, unchanged.

    Raises
    ------
    ValidationError
        If `text` is blank, or longer than `MAX_DESCRIPTION_LENGTH` characters.
    """
    if text is None:
        return None
    if not text.strip():
        raise ValidationError(
            f"The description of {what} is blank. Write the text, or reset it to go back to the "
            "project's text (`lakana table describe --reset`, or send null)."
        )
    if len(text) > MAX_DESCRIPTION_LENGTH:
        raise ValidationError(
            f"The description of {what} has {len(text):,} characters; the limit is "
            f"{MAX_DESCRIPTION_LENGTH:,}. Write a shorter one."
        )
    return text


def _text(value: object) -> str | None:
    """Read a description out of a stored manifest, which is untrusted.

    Parameters
    ----------
    value
        What the manifest holds.

    Returns
    -------
    str or None
        The text when it is a non-empty string, else `None`.
    """
    return value if isinstance(value, str) and value.strip() else None


def _apply_entry(
    session: Session, row: Table, entry: Mapping[str, Any], main_language: str | None
) -> None:
    """Write the project's texts and model of one manifest entry onto a table and its columns.

    Never touches an edit, and leaves an artifact alone. A column whose texts the header no longer
    has loses its project texts; a row left with no text at all is removed. The same holds for
    the entry's translations (`lakana_core.catalog.translations`).

    Parameters
    ----------
    session
        Database session.
    row
        The table.
    entry
        The manifest entry of the asset that produces it.
    main_language
        The manifest's `main_language`, or `None` when it declares none.
    """
    if row.table_type is TableType.ARTIFACT:
        return
    with session.no_autoflush:
        _write_entry(session, row, entry, main_language)


def _texts(declared: object) -> dict[str, str]:
    """Read a name → text map out of a stored manifest, which is untrusted.

    Parameters
    ----------
    declared
        What the manifest holds.

    Returns
    -------
    dict
        Name → text, for the string names whose text is a non-empty string.
    """
    return {
        name: text
        for name, raw in (declared.items() if isinstance(declared, Mapping) else ())
        if isinstance(name, str) and (text := _text(raw)) is not None
    }


def _write_entry(
    session: Session, row: Table, entry: Mapping[str, Any], main_language: str | None
) -> None:
    """Write the project's texts and model of one manifest entry; `_apply_entry` without its guard.

    Parameters
    ----------
    session
        Database session, which must not flush while this reads (the table may be pending).
    row
        The table.
    entry
        The manifest entry.
    main_language
        The language of the header texts, or `None` for the instance's main language.
    """
    metadata = entry.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    row.description_project = _text(metadata.get("description"))
    row.label_project = _text(metadata.get("label"))
    row.text_language = main_language
    wanted = _texts(metadata.get("columns"))
    wanted_labels = _texts(metadata.get("column_labels"))
    existing = {
        r.column: r
        for r in session.scalars(
            select(ColumnDescription).where(ColumnDescription.table_id == row.id)
        )
    }
    for name in wanted.keys() | wanted_labels.keys():
        if name not in existing:
            session.add(
                ColumnDescription(
                    id=uuid.uuid4(),
                    table_id=row.id,
                    column=name,
                    description_project=wanted.get(name),
                    label_project=wanted_labels.get(name),
                )
            )
    for name, found in existing.items():
        found.description_project = wanted.get(name)
        found.label_project = wanted_labels.get(name)
        if all(
            text is None
            for text in (
                found.description_project,
                found.description_edited,
                found.label_project,
                found.label_edited,
            )
        ):
            session.delete(found)
    state = inspect(row)
    write_translations(
        session, row, entry_translations(entry), stored=not (state.pending or state.transient)
    )
    write_model_entry(session, row, entry)


def _entries(manifest: object) -> dict[str, Mapping[str, Any]]:
    """Index a stored manifest's assets by table name, skipping what is malformed.

    Parameters
    ----------
    manifest
        A manifest.

    Returns
    -------
    dict
        Dotted table name → asset entry.
    """
    assets = manifest.get("assets") if isinstance(manifest, Mapping) else None
    return {
        entry["table"]: entry
        for entry in (assets if isinstance(assets, list) else [])
        if isinstance(entry, Mapping) and isinstance(entry.get("table"), str)
    }


def sync_project_descriptions(
    session: Session, environment_id: uuid.UUID, manifest: object
) -> None:
    """Write a project's descriptions and models onto the tables of an environment that exist.

    Called by a deploy and a rollback, in their transaction, with the manifest that becomes
    current. A table the project produces but no run has created yet is skipped; it gets its
    text when it is created (`sync_created_table`). Nothing is committed here.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    manifest
        The manifest of the project version that becomes current.
    """
    main_language = manifest_main_language(manifest)
    for dotted, entry in _entries(manifest).items():
        row = find_project_table(session, environment_id, dotted)
        if row is not None:
            _apply_entry(session, row, entry, main_language)


def sync_created_table(
    session: Session, environment_id: uuid.UUID, row: Table, dotted: str
) -> None:
    """Give a table that was just created the project text and model of its deployment.

    Nothing is committed here; the caller's commit carries it.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    row
        The new table, already added to the session.
    dotted
        Its dotted name.
    """
    with session.no_autoflush:
        manifests = list(
            session.scalars(
                select(JobVersion.manifest_json)
                .join(
                    EnvironmentDeployment,
                    EnvironmentDeployment.current_job_version_id == JobVersion.id,
                )
                .where(EnvironmentDeployment.environment_id == environment_id)
            )
        )
    for manifest in manifests:
        entry = _entries(manifest).get(dotted)
        if entry is not None:
            _apply_entry(session, row, entry, manifest_main_language(manifest))
            return


def edit_table(row: Table, text: str | None) -> tuple[int, int]:
    """Set or reset the edit of a table's description.

    Parameters
    ----------
    row
        The table.
    text
        The new text, or `None` to reset.

    Returns
    -------
    tuple of (int, int)
        The length of the edit before and after (`0` for none).
    """
    old = len(row.description_edited or "")
    row.description_edited = text
    return old, len(text or "")


def edit_column(
    session: Session, table_id: uuid.UUID, column: str, text: str | None, by: uuid.UUID
) -> tuple[int, int]:
    """Set or reset the edit of a column's description.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    column
        The column's name.
    text
        The new text, or `None` to reset.
    by
        The principal who edits.

    Returns
    -------
    tuple of (int, int)
        The length of the edit before and after (`0` for none).
    """
    found = session.execute(
        select(ColumnDescription).where(
            ColumnDescription.table_id == table_id, ColumnDescription.column == column
        )
    ).scalar_one_or_none()
    old = len((found.description_edited if found is not None else None) or "")
    if found is None:
        if text is None:
            return 0, 0
        found = ColumnDescription(id=uuid.uuid4(), table_id=table_id, column=column)
        session.add(found)
    found.description_edited = text
    found.updated_by = by
    found.updated_at = datetime.now(UTC)
    if text is None and found.description_project is None:
        session.delete(found)
    return old, len(text or "")
