"""Reading an environment's glossary: terms in the reader's language, and what a reader sees (#375).

A term shows to anyone who may read its environment. Its links are resolved by name when read:
a link to a table that exists shows only to a reader who may read that table, and is left out
otherwise (never refused), also when its column is gone; a link to a table that does not exist
shows as missing to everyone who sees the term. The name and definition are shown in the first
of the reader's languages that has a translated name or definition, each text without a
translation in the term's main language. Synonyms are additional words, not a translation: the
main-language synonyms show always, followed by those of every reader language before the main
language, a word that folds like one already shown left out.
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
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.languages import DEFAULT_MAIN_LANGUAGE, lookup_chain
from lakana_core.models import (
    DeclaredMeasure,
    Environment,
    GlossaryTerm,
    GlossaryTermLink,
    GlossaryTermRelated,
    GlossaryTermText,
    Namespace,
    Repository,
    Table,
    TableColumn,
)
from lakana_core.pagination import Page, by_key
from lakana_core.projects.glossary import folded_text
from lakana_core.projects.lookups import environment_by_name
from lakana_core.projects.project_config import unsafe_identifier

_TERM_LIST = "terms"

_LOOKUP_CHUNK = 500
"""The most names or ids one statement of `_Links.load` looks up, well under any driver's limit."""
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
        The main-language synonyms, then those of each reader language before the main language
        (also one without a translated name or definition), in the reader's order; a word that
        folds (NFC, then case) like an earlier one is left out.
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
    has a translated name or definition; each text it lacks is shown in the main language. The
    synonyms are the main language's, then those of every language of the chain before the main
    language, whatever the shown language; a word folding like an earlier one shows once.

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
    chain = lookup_chain(languages)
    before_main = chain[: chain.index(main)] if main in chain else chain
    chosen = next(
        (lang for lang in before_main if (lang, "name") in by or (lang, "definition") in by), main
    )
    synonyms: dict[str, str] = {}
    for language in (MAIN_TEXT_LANGUAGE, *before_main):
        for word in by.get((language, "synonym"), []):
            synonyms.setdefault(folded_text(word), word)
    return _Shown(
        chosen,
        by.get((chosen, "name"), [term.name])[0],
        by.get((chosen, "definition"), [term.definition])[0],
        tuple(synonyms.values()),
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

    def load(self, dotted_names: Iterable[str]) -> None:
        """Look up the linked tables not looked up yet, a few statements for all of them.

        One statement finds the tables, a chunk of names at a time, and one each reads the
        columns and the measures of those the reader may read; only the authorization is asked
        per table, as every listing asks it.

        Parameters
        ----------
        dotted_names
            The table names the links give; untrusted, so one that is not a table name is
            simply not found.
        """
        wanted: dict[tuple[str, str], list[str]] = {}
        for dotted in set(dotted_names) - self._tables.keys():
            self._tables[dotted] = None
            try:
                ref = TableRef.from_dotted(self._environment, dotted)
            except ValidationError:
                continue
            wanted.setdefault((ref.namespace.joined(), ref.name), []).append(dotted)
        names = sorted(wanted)
        readable: dict[uuid.UUID, list[str]] = {}
        for at in range(0, len(names), _LOOKUP_CHUNK):
            chunk = names[at : at + _LOOKUP_CHUNK]
            rows = self._session.execute(
                select(Table.id, Namespace.path, Table.name)
                .join(Namespace, Namespace.id == Table.namespace_id)
                .where(
                    Namespace.environment_id == self._environment_id,
                    Namespace.path.in_({path for path, _ in chunk}),
                    Table.name.in_({name for _, name in chunk}),
                )
            ).all()
            for table_id, path, name in rows:
                spellings = wanted.get((path, name), [])
                if not spellings:
                    continue
                for dotted in spellings:
                    self._tables[dotted] = _Table(False)
                ref = TableRef.from_dotted(self._environment, spellings[0])
                if self._authz.permits(
                    self._p,
                    Action.READ_TABLE,
                    Securable.table_scope(self._environment, ref.namespace, ref.name),
                ):
                    readable[table_id] = spellings
        columns: dict[uuid.UUID, set[str]] = {table_id: set() for table_id in readable}
        measures: dict[uuid.UUID, set[str]] = {table_id: set() for table_id in readable}
        ids = sorted(readable)
        for at in range(0, len(ids), _LOOKUP_CHUNK):
            chunk = ids[at : at + _LOOKUP_CHUNK]
            for table_id, name in self._session.execute(
                select(TableColumn.table_id, TableColumn.name).where(
                    TableColumn.table_id.in_(chunk)
                )
            ):
                columns[table_id].add(name)
            for table_id, name in self._session.execute(
                select(DeclaredMeasure.table_id, DeclaredMeasure.column).where(
                    DeclaredMeasure.table_id.in_(chunk)
                )
            ):
                measures[table_id].add(name)
        for table_id, spellings in readable.items():
            seen = _Table(True, frozenset(columns[table_id]), frozenset(measures[table_id]))
            for dotted in spellings:
                self._tables[dotted] = seen

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

        Raises
        ------
        KeyError
            If `load` was not given the link's table first.
        """
        table, column = link.table_name, link.column_name
        found = self._tables[table]
        if found is not None and not found.readable:
            return None
        if column is None:
            return TermLinkView(table, None, "table", found is None)
        if found is None:
            return TermLinkView(table, column, "column", True)
        kind: LinkKind = "measure" if column in found.measures else "column"
        return TermLinkView(table, column, kind, column not in found.columns)


_MAX_TERM_KEY = 63
"""The longest term key, the size of its column."""

_MAX_REPOSITORY = 255
"""The longest repository name, the size of its column."""

_MAX_OWNER_FILTER = 1000
"""The longest owner filter: an owner is stored as text, so this only bounds the request."""


def _check_lookup(what: str, value: str | None, limit: int, environment: str) -> None:
    """Refuse a key or filter that no stored term can match, before it reaches the database.

    A NUL would otherwise be a driver error on Postgres, and a lone surrogate cannot be encoded.

    Parameters
    ----------
    what
        What the value is, for the message (``term key``, ``owner filter``).
    value
        The value as sent; `None` when not given.
    limit
        The most characters it may have.
    environment
        The environment's name, for the next step.

    Raises
    ------
    ValidationError
        If `value` is longer than `limit` or has a control, format or surrogate character.
    """
    if value is None:
        return
    after = f"; list the terms with: lakana term list --env {environment}"
    if len(value) > limit:
        raise ValidationError(
            f"The {what} has {len(value)} characters, more than the {limit} allowed. "
            f"Check it{after}"
        )
    if unsafe_identifier(value):
        raise ValidationError(
            f"The {what} contains a control, format or surrogate character (such as NUL), "
            f"which no term has. Send it without one{after}"
        )


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
            If `limit` is below 1, `cursor` is not one of this list, or a filter is longer than
            any stored value or has a control character.
        """
        _check_lookup("owner filter", owner, _MAX_OWNER_FILTER, environment)
        _check_lookup("repository filter", repository, _MAX_REPOSITORY, environment)
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
        ValidationError
            If `key` is longer than a term key or has a control character.
        """
        _check_lookup("term key", key, _MAX_TERM_KEY, environment)
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
        resolver.load(r.table_name for rows in links.values() for r in rows)
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
