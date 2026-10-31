"""The translated texts of a table: written by a deploy, edited through the API, read guarded.

Every read is guarded by `Table.text_languages`: a table that has no rows is never read for them,
so a project without translations costs no statement here.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from lakana_core.models import Table, TextTranslation
from lakana_core.projects.manifest import EntryText

_Key = tuple[str, str, str, str]
"""(language, subject, name, field)."""


def _key(row: TextTranslation) -> _Key:
    """Index a row.

    Parameters
    ----------
    row
        The row.

    Returns
    -------
    tuple
        Its language, subject, name and field.
    """
    return (row.language, row.subject, row.name, row.field)


def translation_rows(session: Session, row: Table) -> list[TextTranslation]:
    """Read a table's translation rows, only when it has any.

    Parameters
    ----------
    session
        Database session.
    row
        The table.

    Returns
    -------
    list of TextTranslation
        Its rows; empty without a statement when `text_languages` is empty or the row is new.
    """
    state = inspect(row)
    if not row.text_languages or state.pending or state.transient:
        return []
    return list(session.scalars(select(TextTranslation).where(TextTranslation.table_id == row.id)))


def _relist(row: Table, rows: Sequence[TextTranslation]) -> None:
    """Set a table's `text_languages` from the rows it keeps.

    Parameters
    ----------
    row
        The table.
    rows
        Its remaining rows.
    """
    languages = sorted({r.language for r in rows})
    row.text_languages = languages or None


def write_translations(
    session: Session, row: Table, texts: Mapping[str, Sequence[EntryText]], *, stored: bool
) -> None:
    """Make a table's project translations what the manifest says; never touch an edit.

    Parameters
    ----------
    session
        Database session, not flushing (the table may be pending).
    row
        The table.
    texts
        Language → its texts, from `entry_translations`.
    stored
        Whether the table is in the database already; a new one has no rows to read.
    """
    existing = {_key(r): r for r in (translation_rows(session, row) if stored else [])}
    wanted = {
        (language, t.subject, t.name, t.field): t.text
        for language, items in texts.items()
        for t in items
    }
    kept: list[TextTranslation] = []
    for key, found in existing.items():
        found.text_project = wanted.get(key)
        if found.text_project is None and found.text_edited is None:
            session.delete(found)
        else:
            kept.append(found)
    for key, text in wanted.items():
        if key not in existing:
            language, subject, name, field = key
            added = TextTranslation(
                id=uuid.uuid4(),
                table=row,
                subject=subject,
                name=name,
                language=language,
                field=field,
                text_project=text,
            )
            session.add(added)
            kept.append(added)
    _relist(row, kept)


def edit_translation(
    session: Session,
    row: Table,
    *,
    subject: str,
    name: str,
    language: str,
    field: str,
    text: str | None,
    by: uuid.UUID,
) -> tuple[int, int]:
    """Set or reset the edit of one translated text.

    Parameters
    ----------
    session
        Database session.
    row
        The table.
    subject
        ``table`` or ``column``.
    name
        ``""`` for the table, else the column.
    language
        The canonical tag; never the table's main language.
    field
        ``label`` or ``description``.
    text
        The new text, or `None` to reset.
    by
        The principal who edits.

    Returns
    -------
    tuple of (int, int)
        The length of the edit before and after (`0` for none).
    """
    rows = translation_rows(session, row)
    found = next((r for r in rows if _key(r) == (language, subject, name, field)), None)
    old = len((found.text_edited if found is not None else None) or "")
    if found is None:
        if text is None:
            return 0, 0
        found = TextTranslation(
            id=uuid.uuid4(),
            table_id=row.id,
            subject=subject,
            name=name,
            language=language,
            field=field,
        )
        session.add(found)
        rows.append(found)
    found.text_edited = text
    found.updated_by = by
    found.updated_at = datetime.now(UTC)
    if text is None and found.text_project is None:
        session.delete(found)
        rows.remove(found)
    _relist(row, rows)
    return old, len(text or "")
