"""Choosing a text for a reader: edit > translation > main language, by the reader's languages."""

from __future__ import annotations

import uuid

from lakana_core.catalog.descriptions import ShownDescription
from lakana_core.catalog.texts import LanguageCoverage, TextChooser
from lakana_core.models import TextTranslation


def _row(
    language: str,
    field: str,
    project: str | None,
    edited: str | None = None,
    *,
    subject: str = "table",
    name: str = "",
) -> TextTranslation:
    """Build an unsaved translation row.

    Parameters
    ----------
    language
        Its tag.
    field
        Its field.
    project
        The file's text.
    edited
        The edit.
    subject
        Its subject.
    name
        Its name.

    Returns
    -------
    TextTranslation
        The row.
    """
    return TextTranslation(
        id=uuid.uuid4(),
        table_id=uuid.uuid4(),
        subject=subject,
        name=name,
        language=language,
        field=field,
        text_project=project,
        text_edited=edited,
    )


def _chose(
    chooser: TextChooser, field: str, project: str | None, edited: str | None
) -> ShownDescription:
    """Choose a table text that must exist.

    Parameters
    ----------
    chooser
        The chooser.
    field
        The field.
    project
        The header text.
    edited
        The edit.

    Returns
    -------
    ShownDescription
        The chosen text.
    """
    chosen = chooser.choose("table", "", field, project, edited)
    assert chosen is not None
    return chosen


def test_each_text_comes_in_the_first_language_that_has_it() -> None:
    chooser = TextChooser("de", ["fr-CH", "de"], [_row("fr", "label", "Ventes")])
    label = _chose(chooser, "label", "Verkäufe", None)
    description = _chose(chooser, "description", "Je Verkauf.", None)
    assert (label.text, label.language, label.source) == ("Ventes", "fr", "project")
    assert (description.text, description.language) == ("Je Verkauf.", "de")
    assert chooser.requested == "fr-CH"


def test_an_edit_beats_the_translation_and_the_main_edit_beats_the_header() -> None:
    chooser = TextChooser("en", ["fr"], [_row("fr", "label", "Ventes", "Ventes (édité)")])
    assert _chose(chooser, "label", "Sales", None).text == "Ventes (édité)"
    main = _chose(chooser, "description", "Header", "Edited")
    assert (main.text, main.source, main.language) == ("Edited", "edited", "en")


def test_the_main_language_in_the_list_stops_the_search() -> None:
    chooser = TextChooser("en", ["it", "en", "fr"], [_row("fr", "label", "Ventes")])
    assert _chose(chooser, "label", "Sales", None).language == "en"


def test_no_text_anywhere_is_none_and_no_languages_means_the_main_one() -> None:
    chooser = TextChooser("en", [], [])
    assert chooser.choose("table", "", "label", None, None) is None
    assert chooser.requested == "en"


def test_coverage_counts_the_main_texts_each_language_has() -> None:
    rows = [
        _row("fr", "label", "V"),
        _row("fr", "label", None, "C", subject="column", name="id"),
        _row("it", "description", "D"),
    ]
    chooser = TextChooser("en", ["fr"], rows)
    keys = [("table", "", "label"), ("table", "", "description"), ("column", "id", "label")]
    assert chooser.coverage(keys) == (LanguageCoverage("fr", 2, 3), LanguageCoverage("it", 1, 3))


def test_a_row_in_the_main_language_is_ignored_and_never_counted() -> None:
    rows = [_row("en", "label", "Stale"), _row("fr", "label", "Ventes")]
    chooser = TextChooser("en", ["en"], rows)
    assert _chose(chooser, "label", "Sales", None).text == "Sales"
    assert chooser.coverage([("table", "", "label")]) == (LanguageCoverage("fr", 1, 1),)
