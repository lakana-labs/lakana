"""Choosing each text for a reader, by the reader's languages and the table's main language.

The edit, else the deployed text, in the first language of the reader's that has one; else the
main language's edit, else its header text.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.catalog.descriptions import ShownDescription, shown
from lakana_core.languages import lookup_chain
from lakana_core.models import ColumnDescription, TextTranslation

_TextKey = tuple[str, str, str]
"""(subject, name, field)."""


@dataclass(frozen=True)
class LanguageCoverage:
    """How many of a table's main-language texts one language has.

    Parameters
    ----------
    language
        The tag.
    translated
        Main-language texts it has a text for (a translation or an edit).
    total
        Main-language texts the table shows now.
    """

    language: str
    translated: int
    total: int


class TextChooser:
    """Pick the text a reader sees, with its language.

    A translation row in the table's own main language is ignored, both when choosing and when
    counting coverage. The main language's texts are the asset header and the main-language
    edits; such a row can only be left over from before the main language changed (the
    project's `main_language:` or `LAKANA_MAIN_LANGUAGE`), so showing it would let a stale text
    win over the header that now speaks for that language.

    Parameters
    ----------
    main_language
        The table's main language: its `text_language`, else the instance's.
    languages
        The reader's languages, most wanted first; expanded by truncation here.
    rows
        The table's translation rows (`translation_rows`).
    """

    def __init__(
        self, main_language: str, languages: Sequence[str], rows: Iterable[TextTranslation]
    ) -> None:
        """Index the rows.

        Parameters
        ----------
        main_language
            The table's main language.
        languages
            The reader's languages.
        rows
            The table's translation rows.
        """
        self.main = main_language
        self.requested = languages[0] if languages else main_language
        self._order = lookup_chain(languages)
        self._texts: dict[tuple[str, str, str, str], tuple[str | None, str | None]] = {
            (r.subject, r.name, r.field, r.language): (r.text_project, r.text_edited) for r in rows
        }

    def choose(
        self, subject: str, name: str, field: str, project: str | None, edited: str | None
    ) -> ShownDescription | None:
        """Choose one text.

        Parameters
        ----------
        subject
            ``table``, ``column`` or ``measure``.
        name
            ``""`` for the table, else the column.
        field
            ``label``, ``description`` or ``grain``.
        project
            The main-language header text.
        edited
            The main-language edit.

        Returns
        -------
        ShownDescription or None
            The text, its source and its language; `None` when no language has one.
        """
        for language in self._order:
            if language == self.main:
                break
            found = self._texts.get((subject, name, field, language))
            chosen = shown(*found) if found is not None else None
            if chosen is not None:
                return replace(chosen, language=language)
        main = shown(project, edited)
        return None if main is None else replace(main, language=self.main)

    def coverage(self, keys: Iterable[_TextKey]) -> tuple[LanguageCoverage, ...]:
        """Count, per language with rows, the main-language texts it covers.

        Parameters
        ----------
        keys
            The main-language texts the table shows now.

        Returns
        -------
        tuple of LanguageCoverage
            By language; empty for a table without rows.
        """
        wanted = list(keys)
        languages = sorted({key[3] for key in self._texts} - {self.main})
        return tuple(
            LanguageCoverage(
                language,
                sum(
                    1
                    for k in wanted
                    if shown(*self._texts.get((*k, language), (None, None))) is not None
                ),
                len(wanted),
            )
            for language in languages
        )


@dataclass(frozen=True)
class ShownColumns:
    """The chosen texts of a table's columns.

    Parameters
    ----------
    descriptions
        Column → its chosen description.
    labels
        Column → its chosen label.
    keys
        The columns' main-language texts, for coverage.
    """

    descriptions: dict[str, ShownDescription]
    labels: dict[str, ShownDescription]
    keys: list[_TextKey]


def column_texts(
    session: Session, table_id: uuid.UUID, columns: Iterable[str], chooser: TextChooser
) -> ShownColumns:
    """Choose the texts of each of a table's present columns.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    columns
        The names of the columns the table has now; the texts of any other column are not shown.
    chooser
        The reader's chooser.

    Returns
    -------
    ShownColumns
        The texts and the main-language keys. A column with only a translation (an edit in a
        language whose main text is missing) shows it too.
    """
    present = list(columns)
    main = {
        r.column: r
        for r in session.scalars(
            select(ColumnDescription).where(ColumnDescription.table_id == table_id)
        )
    }
    descriptions: dict[str, ShownDescription] = {}
    labels: dict[str, ShownDescription] = {}
    keys: list[_TextKey] = []
    for name in present:
        row = main.get(name)
        texts = (
            (None, None, None, None)
            if row is None
            else (
                row.description_project,
                row.description_edited,
                row.label_project,
                row.label_edited,
            )
        )
        for field, target, project, edited in (
            ("description", descriptions, texts[0], texts[1]),
            ("label", labels, texts[2], texts[3]),
        ):
            if shown(project, edited) is not None:
                keys.append(("column", name, field))
            chosen = chooser.choose("column", name, field, project, edited)
            if chosen is not None:
                target[name] = chosen
    return ShownColumns(descriptions, labels, keys)
