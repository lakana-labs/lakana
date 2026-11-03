"""A repository's glossary rows: written at deploy and rollback, deleted at undeploy (#375).

The rows mirror the manifest's `glossary` (`manifest_glossary`) of the repository's current
project version. They are written in the transaction that makes that version current, so a
reader never sees a glossary of a version that is not current. A stored term that breaks a rule
of the glossary files (a hand-edited manifest row) is left out by `manifest_glossary`, with a
warning, so no count or length ever reaches the database unchecked.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from lakana_core.models import (
    GlossaryTerm,
    GlossaryTermLink,
    GlossaryTermRelated,
    GlossaryTermText,
    Repository,
)
from lakana_core.projects.glossary import GlossaryEntry, manifest_glossary
from lakana_core.projects.manifest import manifest_main_language

MAIN_TEXT_LANGUAGE = ""
"""The `language` of a main-language synonym row: the term's own main language, whatever it is."""


def _children(term_id: uuid.UUID, entry: GlossaryEntry) -> list[object]:
    """Build a term's text, link and related rows.

    Parameters
    ----------
    term_id
        The term's id.
    entry
        The stored term.

    Returns
    -------
    list
        The new rows.
    """
    rows: list[object] = [
        GlossaryTermText(
            term_id=term_id, language=MAIN_TEXT_LANGUAGE, field="synonym", position=n, text=s
        )
        for n, s in enumerate(entry.synonyms)
    ]
    for language, texts in entry.translations.items():
        for field, text in (("name", texts.name), ("definition", texts.definition)):
            if text is not None:
                rows.append(
                    GlossaryTermText(
                        term_id=term_id, language=language, field=field, position=0, text=text
                    )
                )
        rows += [
            GlossaryTermText(
                term_id=term_id, language=language, field="synonym", position=n, text=s
            )
            for n, s in enumerate(texts.synonyms)
        ]
    rows += [
        GlossaryTermLink(term_id=term_id, position=n, table_name=table, column_name=column)
        for n, (table, column) in enumerate(entry.links)
    ]
    rows += [
        GlossaryTermRelated(term_id=term_id, position=n, related_key=key)
        for n, key in enumerate(entry.related)
    ]
    return rows


def _delete_terms(session: Session, term_ids: list[uuid.UUID], *, keep_terms: bool) -> None:
    """Delete the text, link and related rows of terms, and the terms unless kept.

    Parameters
    ----------
    session
        Database session.
    term_ids
        The terms.
    keep_terms
        Whether the term rows themselves stay (they are rewritten in place).
    """
    if not term_ids:
        return
    models = (GlossaryTermText, GlossaryTermLink, GlossaryTermRelated)
    for model in models:
        session.execute(
            delete(model)
            .where(model.term_id.in_(term_ids))
            .execution_options(synchronize_session=False)
        )
    if not keep_terms:
        session.execute(
            delete(GlossaryTerm)
            .where(GlossaryTerm.id.in_(term_ids))
            .execution_options(synchronize_session=False)
        )


def write_glossary(
    session: Session, environment_id: uuid.UUID, repository_id: uuid.UUID, manifest: object
) -> None:
    """Make a repository's glossary rows match the manifest that is becoming current.

    A term whose id stays is updated in place and keeps its UUID; a new one is inserted; one the
    manifest no longer has is deleted, also when the manifest has no glossary at all. Call it
    only after `glossary_clashes` found none under the ownership lock.

    Parameters
    ----------
    session
        Database session, in the transaction that moves the pointer.
    environment_id
        The environment.
    repository_id
        The repository whose version becomes current.
    manifest
        That version's manifest.
    """
    entries = manifest_glossary(manifest)
    main_language = manifest_main_language(manifest)
    existing = {
        row.term_key: row
        for row in session.scalars(
            select(GlossaryTerm).where(
                GlossaryTerm.environment_id == environment_id,
                GlossaryTerm.repository_id == repository_id,
            )
        )
    }
    _delete_terms(session, [row.id for row in existing.values()], keep_terms=True)
    gone = [row for key, row in existing.items() if key not in entries]
    for row in gone:
        session.expunge(row)
    _delete_terms(session, [row.id for row in gone], keep_terms=False)
    children: list[object] = []
    for key, entry in entries.items():
        row = existing.get(key)
        if row is None:
            row = GlossaryTerm(
                id=uuid.uuid4(),
                environment_id=environment_id,
                repository_id=repository_id,
                term_key=key,
            )
            session.add(row)
        row.name, row.definition, row.owner = entry.name, entry.definition, entry.owner
        row.main_language = main_language
        children += _children(row.id, entry)
    session.flush()
    session.add_all(children)
    session.flush()


def delete_glossary(session: Session, environment_id: uuid.UUID, repository_id: uuid.UUID) -> None:
    """Delete a repository's terms in an environment, with their rows (undeploy).

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    repository_id
        The repository.
    """
    ids = list(
        session.scalars(
            select(GlossaryTerm.id).where(
                GlossaryTerm.environment_id == environment_id,
                GlossaryTerm.repository_id == repository_id,
            )
        )
    )
    _delete_terms(session, ids, keep_terms=False)


def terms_elsewhere(
    session: Session, environment_id: uuid.UUID, repository_id: uuid.UUID
) -> dict[str, str]:
    """Map each term another repository owns in an environment to that repository's name.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    repository_id
        The repository to leave out.

    Returns
    -------
    dict
        Term id → repository name.
    """
    rows = session.execute(
        select(GlossaryTerm.term_key, Repository.name)
        .join(Repository, Repository.id == GlossaryTerm.repository_id)
        .where(
            GlossaryTerm.environment_id == environment_id,
            GlossaryTerm.repository_id != repository_id,
        )
    )
    return {key: name for key, name in rows}


def glossary_clashes(
    session: Session, environment_id: uuid.UUID, repository_id: uuid.UUID, keys: Iterable[str]
) -> list[tuple[str, str]]:
    """Name the terms in `keys` that another repository owns.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    repository_id
        The repository that wants the terms.
    keys
        Term ids.

    Returns
    -------
    list of tuple
        ``(id, owner)`` per clash, sorted by id.
    """
    taken = terms_elsewhere(session, environment_id, repository_id)
    return [(key, taken[key]) for key in sorted(set(keys) & taken.keys())]
