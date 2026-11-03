"""Reading an environment's glossary: terms in the reader's language, and what a reader sees (#375).

A term shows to anyone who may read its environment. Its links are resolved by name when read:
a link to a table that exists shows only to a reader who may read that table, and is left out
otherwise (never refused), also when its column is gone; a link to a table that does not exist
shows as missing to everyone who sees the term. Texts are shown in the first of the reader's
languages that has a translated name or definition, each text without a translation in the
term's main language.
"""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.catalog.glossary import MAIN_TEXT_LANGUAGE
from lakana_core.catalog.model_metadata import find_project_table
from lakana_core.errors import NotFoundError
from lakana_core.identifiers import TableRef
from lakana_core.languages import DEFAULT_MAIN_LANGUAGE, lookup_chain
from lakana_core.models import (
    DeclaredMeasure,
    Environment,
    GlossaryTerm,
    GlossaryTermLink,
    GlossaryTermRelated,
    GlossaryTermText,
    Repository,
    TableColumn,
)
from lakana_core.pagination import Page, by_key
from lakana_core.projects.lookups import environment_by_name

_TERM_LIST = "terms"
"""The kind the terms' cursor names."""

LinkKind = Literal["table", "column", "measure"]
"""What a link points at: the table itself, a column, or a column declared as a measure."""


@dataclass(frozen=True)
class TermLinkView:
    """A link of a term, as one reader sees it.

    Parameters
    ----------
    table
        The dotted table name the file gave.
    column
        The column, or `None` for a link to the table itself.
    kind
        ``table``, ``column`` or ``measure``.
    missing
        Whether the table, or the column, does not exist.
    """

    table: str
    column: str | None
    kind: LinkKind
    missing: bool


@dataclass(frozen=True)
class RelatedTermView:
    """A related term id a term names.

    Parameters
    ----------
    key
        The id.
    name
        The related term's name in the reader's language, or `None` when no term has the id.
    missing
        Whether no term of the environment has the id.
    """

    key: str
    name: str | None
    missing: bool


@dataclass(frozen=True)
class TermView:
    """A term as one reader sees it.

    Parameters
    ----------
    key
        The term id.
    id
        The term's UUID, kept while the id stays.
    name
        The name in `language`.
    definition
        The definition, in `language` when translated, else in the main language.
    synonyms
        The main-language synonyms, then those of `language`.
    owner
        Who answers for the term.
    repository
        The repository that deployed it.
    language
        The language the texts are shown in.
    links
        The links the reader may see, by table name, each table's in the order of the file.
    related
        The related ids the term names, in the order of the file.
    related_by
        The ids of the terms that name this one as related, sorted.
    """

    key: str
    id: uuid.UUID
    name: str
    definition: str
    synonyms: tuple[str, ...]
    owner: str
    repository: str
    language: str
    links: tuple[TermLinkView, ...]
    related: tuple[RelatedTermView, ...]
    related_by: tuple[str, ...]


@dataclass(frozen=True)
class TermRef:
    """A term named on a table or a column: its id and its shown name.

    Parameters
    ----------
    key
        The term id.
    name
        Its name in the reader's language.
    """

    key: str
    name: str


@dataclass(frozen=True)
class TableTerms:
    """The terms linked to a table and to each of its columns.

    Parameters
    ----------
    table
        The terms linked to the table itself, sorted by id.
    columns
        Column name → the terms linked to it, sorted by id.
    """

    table: tuple[TermRef, ...] = ()
    columns: Mapping[str, tuple[TermRef, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Shown:
    """A term's texts as one reader sees them.

    Parameters
    ----------
    language
        The language they are shown in.
    name
        The name.
    definition
        The definition.
    synonyms
        The synonyms.
    """

    language: str
    name: str
    definition: str
    synonyms: tuple[str, ...]


def _shown(
    term: GlossaryTerm,
    texts: Sequence[GlossaryTermText],
    languages: Sequence[str],
    main_language: str,
) -> _Shown:
    """Choose a term's texts for a reader.

    The shown language is the first of the reader's lookup chain, before the main language, that
    has a translated name or definition; each text it lacks is shown in the main language.

    Parameters
    ----------
    term
        The term.
    texts
        Its text rows.
    languages
        The reader's canonical languages, most wanted first.
    main_language
        The instance's main language, for a term whose project declares none.

    Returns
    -------
    _Shown
        The texts.
    """
    main = term.main_language or main_language
    by: dict[tuple[str, str], list[str]] = {}
    for row in sorted(texts, key=lambda r: r.position):
        by.setdefault((row.language, row.field), []).append(row.text)
    chosen = main
    for language in lookup_chain(languages):
        if language == main:
            break
        if (language, "name") in by or (language, "definition") in by:
            chosen = language
            break
    synonyms = by.get((MAIN_TEXT_LANGUAGE, "synonym"), [])
    if chosen != main:
        synonyms = synonyms + by.get((chosen, "synonym"), [])
    return _Shown(
        chosen,
        by.get((chosen, "name"), [term.name])[0],
        by.get((chosen, "definition"), [term.definition])[0],
        tuple(synonyms),
    )


def _texts_by_term(
    session: Session, term_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[GlossaryTermText]]:
    """Load the text rows of terms.

    Parameters
    ----------
    session
        Database session.
    term_ids
        The terms.

    Returns
    -------
    dict
        Term id → its rows; a term without rows is absent.
    """
    found: dict[uuid.UUID, list[GlossaryTermText]] = {}
    rows = session.scalars(select(GlossaryTermText).where(GlossaryTermText.term_id.in_(term_ids)))
    for row in rows:
        found.setdefault(row.term_id, []).append(row)
    return found


@dataclass(frozen=True)
class _Table:
    """An existing table a link names, as one reader may see it.

    Parameters
    ----------
    readable
        Whether the reader may read it.
    columns
        Its columns; empty when not readable.
    measures
        Its columns declared as measures; empty when not readable.
    """

    readable: bool
    columns: frozenset[str] = frozenset()
    measures: frozenset[str] = frozenset()


class _Links:
    """Resolve links for one reader, each table looked up and authorized once.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service.
    p
        The reader.
    environment
        The environment's name.
    environment_id
        Its id.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        p: PrincipalRef,
        environment: str,
        environment_id: uuid.UUID,
    ) -> None:
        """Start with no table looked up.

        Parameters
        ----------
        session
            Database session.
        authz
            The authorization service.
        p
            The reader.
        environment
            The environment's name.
        environment_id
            Its id.
        """
        self._session = session
        self._authz = authz
        self._p = p
        self._environment = environment
        self._environment_id = environment_id
        self._tables: dict[str, _Table | None] = {}

    def _table(self, dotted: str) -> _Table | None:
        """Look a linked table up, with what the reader may see of it.

        Parameters
        ----------
        dotted
            The table name the link gives.

        Returns
        -------
        _Table or None
            The table, or `None` when it does not exist.
        """
        if dotted in self._tables:
            return self._tables[dotted]
        row = find_project_table(self._session, self._environment_id, dotted)
        found: _Table | None = None
        if row is not None:
            ref = TableRef.from_dotted(self._environment, dotted)
            found = _Table(False)
            if self._authz.permits(
                self._p,
                Action.READ_TABLE,
                Securable.table_scope(self._environment, ref.namespace, ref.name),
            ):
                columns = select(TableColumn.name).where(TableColumn.table_id == row.id)
                measures = select(DeclaredMeasure.column).where(DeclaredMeasure.table_id == row.id)
                found = _Table(
                    True,
                    frozenset(self._session.scalars(columns)),
                    frozenset(self._session.scalars(measures)),
                )
        self._tables[dotted] = found
        return found

    def view(self, link: GlossaryTermLink) -> TermLinkView | None:
        """Show a link to the reader, or leave it out.

        Parameters
        ----------
        link
            The stored link.

        Returns
        -------
        TermLinkView or None
            The link; `None` when its table exists and the reader may not read it.
        """
        table, column = link.table_name, link.column_name
        found = self._table(table)
        if found is not None and not found.readable:
            return None
        if column is None:
            return TermLinkView(table, None, "table", found is None)
        if found is None:
            return TermLinkView(table, column, "column", True)
        kind: LinkKind = "measure" if column in found.measures else "column"
        return TermLinkView(table, column, kind, column not in found.columns)


class GlossaryService:
    """Read an environment's glossary terms, for one reader at a time.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service.
    main_language
        The instance's main language, for a term whose project declares none.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        main_language: str = DEFAULT_MAIN_LANGUAGE,
    ) -> None:
        """Wire the service.

        Parameters
        ----------
        session
            Database session.
        authz
            The authorization service.
        main_language
            The instance's main language.
        """
        self._session = session
        self._authz = authz
        self._main_language = main_language

    def _read_environment(self, p: PrincipalRef, environment: str) -> Environment:
        """Authorize reading an environment and look it up.

        Parameters
        ----------
        p
            The reader.
        environment
            The environment's name.

        Returns
        -------
        Environment
            The row.

        Raises
        ------
        PermissionDenied
            If `p` may not read the environment.
        NotFoundError
            If it does not exist.
        """
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment))
        return environment_by_name(self._session, environment)

    def list(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        owner: str | None = None,
        repository: str | None = None,
        languages: Sequence[str] = (),
        limit: int = 100,
        cursor: str | None = None,
    ) -> Page[TermView]:
        """List an environment's terms, sorted by id.

        Parameters
        ----------
        p
            The reader.
        environment
            The environment's name.
        owner
            Only the terms of this owner.
        repository
            Only the terms this repository deployed.
        languages
            The reader's canonical languages, most wanted first.
        limit
            Most terms returned.
        cursor
            The `next_cursor` of the previous page, or `None` for the first.

        Returns
        -------
        Page
            The terms, and the cursor of the next page (`None` on the last).

        Raises
        ------
        PermissionDenied
            If `p` may not read the environment.
        NotFoundError
            If it does not exist.
        ValidationError
            If `limit` is below 1 or `cursor` is not one of this list.
        """
        env = self._read_environment(p, environment)
        query = select(GlossaryTerm).where(GlossaryTerm.environment_id == env.id)
        if owner is not None:
            query = query.where(GlossaryTerm.owner == owner)
        if repository is not None:
            query = query.join(Repository, Repository.id == GlossaryTerm.repository_id).where(
                Repository.name == repository
            )
        page = by_key(
            self._session,
            query,
            kind=_TERM_LIST,
            key=GlossaryTerm.term_key,
            key_of=lambda row: row.term_key,
            limit=limit,
            cursor=cursor,
        )
        return Page(self._views(p, environment, env.id, page.items, languages), page.next_cursor)

    def get(
        self, p: PrincipalRef, environment: str, key: str, *, languages: Sequence[str] = ()
    ) -> TermView:
        """Read one term.

        Parameters
        ----------
        p
            The reader.
        environment
            The environment's name.
        key
            The term id.
        languages
            The reader's canonical languages, most wanted first.

        Returns
        -------
        TermView
            The term.

        Raises
        ------
        PermissionDenied
            If `p` may not read the environment.
        NotFoundError
            If the environment or the term does not exist.
        """
        env = self._read_environment(p, environment)
        term = self._session.scalars(
            select(GlossaryTerm).where(
                GlossaryTerm.environment_id == env.id, GlossaryTerm.term_key == key
            )
        ).one_or_none()
        if term is None:
            raise NotFoundError(
                f"Term `{key}` does not exist in environment `{environment}`. List the terms "
                f"with: lakana term list --env {environment}"
            )
        return self._views(p, environment, env.id, [term], languages)[0]

    def _views(
        self,
        p: PrincipalRef,
        environment: str,
        environment_id: uuid.UUID,
        terms: Iterable[GlossaryTerm],
        languages: Sequence[str],
    ) -> builtins.list[TermView]:
        """Show terms to a reader, loading their rows a few queries for all of them.

        Parameters
        ----------
        p
            The reader.
        environment
            The environment's name.
        environment_id
            Its id.
        terms
            The terms, in the order to return them.
        languages
            The reader's canonical languages, most wanted first.

        Returns
        -------
        list of TermView
            One view per term.
        """
        terms = list(terms)
        ids = [t.id for t in terms]
        texts = _texts_by_term(self._session, ids)
        repositories = dict(
            self._session.execute(
                select(Repository.id, Repository.name).where(
                    Repository.id.in_({t.repository_id for t in terms})
                )
            ).all()
        )
        links: dict[uuid.UUID, list[GlossaryTermLink]] = {}
        for link in self._session.scalars(
            select(GlossaryTermLink).where(GlossaryTermLink.term_id.in_(ids))
        ):
            links.setdefault(link.term_id, []).append(link)
        related: dict[uuid.UUID, list[GlossaryTermRelated]] = {}
        for rel in self._session.scalars(
            select(GlossaryTermRelated).where(GlossaryTermRelated.term_id.in_(ids))
        ):
            related.setdefault(rel.term_id, []).append(rel)
        named = {r.related_key for rows in related.values() for r in rows}
        targets = {
            t.term_key: t
            for t in self._session.scalars(
                select(GlossaryTerm).where(
                    GlossaryTerm.environment_id == environment_id, GlossaryTerm.term_key.in_(named)
                )
            )
        }
        target_texts = _texts_by_term(self._session, [t.id for t in targets.values()])
        naming: dict[str, set[str]] = {}
        for related_key, by in self._session.execute(
            select(GlossaryTermRelated.related_key, GlossaryTerm.term_key)
            .join(GlossaryTerm, GlossaryTerm.id == GlossaryTermRelated.term_id)
            .where(
                GlossaryTerm.environment_id == environment_id,
                GlossaryTermRelated.related_key.in_([t.term_key for t in terms]),
            )
        ).all():
            naming.setdefault(related_key, set()).add(by)
        resolver = _Links(self._session, self._authz, p, environment, environment_id)
        main = self._main_language
        views: list[TermView] = []
        for term in terms:
            shown = _shown(term, texts.get(term.id, []), languages, main)
            ordered = sorted(links.get(term.id, []), key=lambda r: (r.table_name, r.position))
            seen = [v for v in (resolver.view(r) for r in ordered) if v is not None]
            names: list[RelatedTermView] = []
            for rel in sorted(related.get(term.id, []), key=lambda r: r.position):
                target = targets.get(rel.related_key)
                name = None
                if target is not None:
                    name = _shown(target, target_texts.get(target.id, []), languages, main).name
                names.append(RelatedTermView(rel.related_key, name, target is None))
            views.append(
                TermView(
                    key=term.term_key,
                    id=term.id,
                    name=shown.name,
                    definition=shown.definition,
                    synonyms=shown.synonyms,
                    owner=term.owner,
                    repository=repositories[term.repository_id],
                    language=shown.language,
                    links=tuple(seen),
                    related=tuple(names),
                    related_by=tuple(sorted(naming.get(term.term_key, ()))),
                )
            )
        return views


def table_terms(
    session: Session,
    environment_id: uuid.UUID,
    dotted: str,
    languages: Sequence[str],
    main_language: str,
) -> TableTerms:
    """List the terms linked to a table and to each of its columns.

    Authorization is the caller's: `table get` shows these to a reader of the table and of the
    environment.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    dotted
        The table's dotted name.
    languages
        The reader's canonical languages, most wanted first.
    main_language
        The instance's main language, for a term whose project declares none.

    Returns
    -------
    TableTerms
        The terms; a term linked several times to one column shows once.
    """
    pairs = session.execute(
        select(GlossaryTermLink.column_name, GlossaryTerm)
        .join(GlossaryTerm, GlossaryTerm.id == GlossaryTermLink.term_id)
        .where(GlossaryTerm.environment_id == environment_id, GlossaryTermLink.table_name == dotted)
        .order_by(GlossaryTerm.term_key, GlossaryTermLink.position)
    ).all()
    texts = _texts_by_term(session, {term.id for _, term in pairs})
    table: set[TermRef] = set()
    columns: dict[str, set[TermRef]] = {}
    for column, term in pairs:
        ref = TermRef(
            term.term_key, _shown(term, texts.get(term.id, []), languages, main_language).name
        )
        (table if column is None else columns.setdefault(column, set())).add(ref)
    return TableTerms(
        tuple(sorted(table, key=lambda r: r.key)),
        {c: tuple(sorted(refs, key=lambda r: r.key)) for c, refs in columns.items()},
    )
