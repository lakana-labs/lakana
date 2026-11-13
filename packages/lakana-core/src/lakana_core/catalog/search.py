"""Search an environment for tables, columns, labels, descriptions, runs, principals and terms.

One query ranks every kind of hit together in the database: an exact match of a name or label
first, then a prefix, then a substring, then a match in a description; within a rank tables come
before columns before runs before principals, then by name. The rights are applied afterwards,
hit by hit, with the checks the listings make, over chunks of the ranked candidates, so no hit,
and no text of one, is read out for a caller who may not read it, and the work is bounded by
`MAX_SCANNED` candidates, not by the size of the environment. Texts in the main language always
match; translated texts match only in the reader's languages, and a table or column found by
several texts is one hit. A glossary term is found by its name, synonyms and definition, and a
table or column a term links is found by that term's name and synonyms, in the languages the term
shows them in.

The table list's search text (`CatalogSearch.table_matches`) walks the same ranked union, of the
table and column arms only, over several environments, restricted in the SQL to the tables its
caller may read, and stops after `MAX_SCANNED` candidates too; an unreadable candidate is never
read or counted.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Callable, Collection, Generator, Sequence
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from itertools import islice
from typing import Any, Protocol
from typing import cast as type_cast

from sqlalchemy import (
    ColumnElement,
    Select,
    String,
    Text,
    and_,
    bindparam,
    case,
    cast,
    exists,
    func,
    literal,
    literal_column,
    null,
    or_,
    select,
    union_all,
)
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.catalog.glossary import MAIN_TEXT_LANGUAGE
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import NAMESPACE_SEPARATOR, NamespacePath
from lakana_core.languages import DEFAULT_MAIN_LANGUAGE, lookup_chain
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    ColumnDescription,
    DeclaredMeasure,
    Environment,
    GlossaryTerm,
    GlossaryTermLink,
    GlossaryTermText,
    Namespace,
    Principal,
    PrincipalType,
    Repository,
    Run,
    RunStatus,
    Table,
    TableColumn,
    TableType,
    TextTranslation,
)
from lakana_core.models.table import table_kind
from lakana_core.pagination import escape_like
from lakana_core.projects.asset_model import require_model_kind

MAX_QUERY_LENGTH = 200
"""The most characters a query may have."""

MAX_LIMIT = 100
"""The most hits one page may hold."""

SNIPPET_LENGTH = 160
"""The most characters of a description a hit shows."""

CHUNK = 200
"""How many ranked candidates are fetched at once, from the one ranked statement a search runs."""

MAX_SCANNED = 5000
"""The most candidates one search examines. A caller who may read few of many matches gets what
the first `MAX_SCANNED` matches hold; a narrower query finds the rest."""

MIN_RUN_ID_CHARACTERS = 4
"""The fewest hex characters of a run id that match it."""

_HEX = re.compile(rf"[0-9a-f]{{{MIN_RUN_ID_CHARACTERS},32}}")


_Expr = ColumnElement[Any] | InstrumentedAttribute[Any]
"""A column or an expression over columns."""

_Candidates = Select[tuple[Any, ...]]
"""A select of candidates in the shape of the union."""


class _Candidate(Protocol):
    """A row of the ranked union, as the checks and `_hit` read it."""

    kind_order: int
    sort_id: str
    """The table's id for a table or column row, the row's own id otherwise."""
    sort_source: int
    """0 for a row of a main arm, more for a translation."""
    matched: str
    f1: str
    f2: str
    f3: str
    f4: str
    f5: str
    f6: str | None
    """The language of a matched text, for a table or column hit not matched by its name."""
    f7: str | None
    """The term's key of a table or column hit found by a term."""


class SearchKind(enum.StrEnum):
    """What a hit is."""

    TABLE = "table"
    COLUMN = "column"
    RUN = "run"
    PRINCIPAL = "principal"
    TERM = "term"


_KIND_ORDER = {
    SearchKind.TABLE: 0,
    SearchKind.COLUMN: 1,
    SearchKind.RUN: 2,
    SearchKind.PRINCIPAL: 3,
    SearchKind.TERM: 4,
}
"""The order of kinds within a rank."""


# jscpd:ignore-start
# The server's `SearchHitOut` is this record's JSON form and lists the same fields; core cannot
# import the server's model, and the server validates this dataclass into it.
@dataclass(frozen=True)
class SearchHit:
    """One hit.

    Parameters
    ----------
    kind
        What was found.
    name
        The display name: ``namespace.table`` for a table, ``namespace.table.column`` for a
        column, the id for a run, the name for a principal, the key for a term.
    matched
        The field that matched: ``name``, ``label``, ``description``, ``grain``, ``measure`` (a
        column's measure label or description), ``id``, ``job``, ``repository``, ``synonym`` (a
        term's) or ``term`` (a table or column found by a term's name or synonym).
    snippet
        For a label, description, grain, measure or term match, and for every term hit, at most
        `SNIPPET_LENGTH` characters of the text around the hit.
    namespace
        For a table or column, its namespace, dotted.
    table
        For a table or column, the table's name.
    table_kind
        For a table, ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``.
    model_kind
        For a table whose asset header declares one, its ``kind``: ``fact``, ``dimension``,
        ``wide`` or ``staging``.
    column
        For a column, its name.
    column_type
        For a column, its type.
    run_id
        For a run, its id.
    job
        For a run, its job, if it has one.
    repository
        For a run or a term, its repository.
    status
        For a run, its status.
    principal
        For a principal, its name.
    principal_type
        For a principal, ``USER`` or ``SERVICE_ACCOUNT``.
    language
        For a table or column hit that a text matched, the language of that text; for a term hit
        that a translated text matched, its language; `None` for a name match, a term match, a
        term's main-language text, and for runs and principals.
    translated
        Whether the text that matched is a translation (in a language other than the table's or
        term's main one); `False` for a header text, a name, a run and a principal.
    term
        For a term hit, its key; for a table or column hit with `matched` ``term``, the key of the
        term whose name or synonym matched.
    """

    kind: SearchKind
    name: str
    matched: str
    snippet: str | None = None
    namespace: str | None = None
    table: str | None = None
    table_kind: str | None = None
    model_kind: str | None = None
    column: str | None = None
    column_type: str | None = None
    run_id: uuid.UUID | None = None
    job: str | None = None
    repository: str | None = None
    status: str | None = None
    principal: str | None = None
    principal_type: str | None = None
    language: str | None = None
    translated: bool = False
    term: str | None = None


# jscpd:ignore-end


@dataclass(frozen=True)
class SearchPage:
    """A page of hits.

    Parameters
    ----------
    results
        The hits, best first.
    limit
        The page size asked for.
    offset
        How many permitted hits came before this page.
    has_more
        Whether a permitted hit follows this page.
    """

    results: list[SearchHit]
    limit: int
    offset: int
    has_more: bool


@dataclass(frozen=True)
class TableMatches:
    """The readable tables a text matches.

    Parameters
    ----------
    hits
        Table id → its best ranked hit, at most `MAX_SCANNED`.
    capped
        Whether a further readable table matched.
    """

    hits: dict[uuid.UUID, SearchHit]
    capped: bool


def _snippet(text: str, needle: str) -> str:
    """Cut at most `SNIPPET_LENGTH` characters of a description around the first hit.

    Parameters
    ----------
    text
        The description.
    needle
        The query.

    Returns
    -------
    str
        The cut.
    """
    index = max(text.lower().find(needle.lower()), 0)
    margin = max(0, SNIPPET_LENGTH - len(needle)) // 2
    start = max(0, index - margin)
    return text[start : start + SNIPPET_LENGTH]


class _Match:
    """The comparisons of one query against text columns, case-insensitive and portable."""

    def __init__(self, needle: str) -> None:
        """Prepare the patterns.

        Parameters
        ----------
        needle
            The query, trimmed.
        """
        escaped = escape_like(needle)
        self._exact = func.lower(literal(needle, String))
        self._prefix = func.lower(literal(escaped + "%", String))
        self._contains = func.lower(literal("%" + escaped + "%", String))

    def exact(self, column: _Expr) -> ColumnElement[bool]:
        """Compare for equality.

        Parameters
        ----------
        column
            A text expression.

        Returns
        -------
        ColumnElement
            True where the column equals the query.
        """
        return func.lower(column) == self._exact

    def prefix(self, column: _Expr) -> ColumnElement[bool]:
        """Compare for starting with.

        Parameters
        ----------
        column
            A text expression.

        Returns
        -------
        ColumnElement
            True where the column starts with the query.
        """
        return func.lower(column).like(self._prefix, escape="\\")

    def contains(self, column: _Expr) -> ColumnElement[bool]:
        """Compare for containing.

        Parameters
        ----------
        column
            A text expression.

        Returns
        -------
        ColumnElement
            True where the column contains the query.
        """
        return func.lower(column).like(self._contains, escape="\\")

    def rank(
        self,
        columns: Sequence[_Expr],
        *,
        otherwise: int,
        extra_exact: Sequence[ColumnElement[bool]] = (),
        extra_prefix: Sequence[ColumnElement[bool]] = (),
        extra_contains: Sequence[ColumnElement[bool]] = (),
    ) -> ColumnElement[int]:
        """Rank the match of any of `columns`: 0 exact, 1 prefix, 2 contains.

        Parameters
        ----------
        columns
            The text expressions that count as the name.
        otherwise
            The rank when none of them matches (a match elsewhere).
        extra_exact
            More conditions that make an exact match.
        extra_prefix
            More conditions that make a prefix match.
        extra_contains
            More conditions that make a substring match.

        Returns
        -------
        ColumnElement
            The rank as an integer expression.
        """
        return case(
            (or_(*(self.exact(c) for c in columns), *extra_exact), literal_column("0")),
            (or_(*(self.prefix(c) for c in columns), *extra_prefix), literal_column("1")),
            (or_(*(self.contains(c) for c in columns), *extra_contains), literal_column("2")),
            else_=literal_column(str(otherwise)),
        )


def _row(
    kind: SearchKind,
    rank: _Expr,
    sort_name: _Expr,
    sort_id: _Expr,
    matched: _Expr,
    *fields: _Expr,
    sort_source: _Expr | None = None,
    by_term: bool = False,
) -> _Candidates:
    """Start a candidate select with the columns every kind shares.

    Parameters
    ----------
    kind
        The kind of hit.
    rank
        Its rank expression.
    sort_name
        What it sorts by after kind.
    sort_id
        The tie-break after the name: the table's id for a table or column, the row's id
        otherwise.
    matched
        The field that matched.
    *fields
        Up to seven payload columns, as text; the rest are null.
    sort_source
        The last tie-break, an integer: which text of one table or column comes first. `None`
        (``0``) for a main-language row, which so comes before every translation.
    by_term
        Whether the row is a table or column found by a term, which a row of the item's own
        text at the same rank, translated or not, comes before.

    Returns
    -------
    Select
        A select with the columns of the union.
    """
    payload = [*fields, *(cast(null(), Text) for _ in range(7 - len(fields)))]
    columns = [
        literal_column(str(_KIND_ORDER[kind])).label("kind_order"),
        rank.label("rank"),
        sort_name.label("sort_name"),
        sort_id.label("sort_id"),
        (literal_column("0") if sort_source is None else sort_source).label("sort_source"),
        literal_column("1" if by_term else "0").label("by_term"),
        matched.label("matched"),
        *(cast(f, Text).label(f"f{i}") for i, f in enumerate(payload, start=1)),
    ]
    return type_cast("_Candidates", select(*columns))


def _text(value: str) -> ColumnElement[str]:
    """Write a constant that every database types as text.

    Parameters
    ----------
    value
        The constant, without a quote.

    Returns
    -------
    ColumnElement
        The constant.
    """
    return literal_column(f"'{value}'")


def _normalized_query(q: str) -> str:
    """Trim a query and check its length.

    Parameters
    ----------
    q
        The query as sent.

    Returns
    -------
    str
        The trimmed query.

    Raises
    ------
    ValidationError
        If it is empty after trimming or longer than `MAX_QUERY_LENGTH`.
    """
    needle = q.strip()
    if not 1 <= len(needle) <= MAX_QUERY_LENGTH:
        raise ValidationError(
            f"The search text must be between 1 and {MAX_QUERY_LENGTH} characters after "
            "trimming; give a name, a part of one or a run id."
        )
    return needle


class CatalogSearch:
    """Search an environment, showing a caller only what they may read.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service the listings use.
    main_language
        The instance's main language, which a table's header texts are in when its project
        declares none.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        main_language: str = DEFAULT_MAIN_LANGUAGE,
    ) -> None:
        """Bind the session, the authorization and the main language.

        Parameters
        ----------
        session
            Database session.
        authz
            The authorization service the listings use.
        main_language
            The instance's main language, which a table's header texts are in when its project
            declares none.
        """
        self._session = session
        self._authz = authz
        self._main_language = main_language

    def search(
        self,
        p: PrincipalRef,
        environment: str,
        q: str,
        *,
        kind: SearchKind | None = None,
        model_kind: str | None = None,
        limit: int = 20,
        offset: int = 0,
        languages: Sequence[str] = (),
    ) -> SearchPage:
        """Find tables, columns, labels, descriptions, runs, principals and terms that match a text.

        Rights are those of the listings. A table or column appears only where `READ_TABLE` is
        permitted for its table, a run needs `READ_ENVIRONMENT`, a principal needs the right to
        list principals. Glossary terms need `READ_ENVIRONMENT`. A table or column found by a
        term's name or synonym (`matched: term`) needs `READ_ENVIRONMENT` and the table's
        `READ_TABLE`. Where the caller lacks a right, those hits are left out, not refused.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        q
            The text, case-insensitive; trimmed, 1 to 200 characters.
        kind
            Only this kind of hit.
        model_kind
            Only tables whose asset header declares this ``kind``: ``fact``, ``dimension``,
            ``wide`` or ``staging``. Columns, runs, principals and terms have none, so they are
            left out.
        limit
            The page size, 1 to 100.
        offset
            How many permitted hits to skip.
        languages
            The reader's canonical languages, most wanted first; translated texts match only in
            these (and their shorter forms), main-language texts always.

        Returns
        -------
        SearchPage
            The page; a table or column found by several texts is one hit, the best ranked.

        Raises
        ------
        ValidationError
            If the text, `limit` or `offset` is out of range, or `model_kind` is not a kind.
        NotFoundError
            If the environment does not exist and `p` may read environments. A caller who may
            not gets an empty page, as for an environment that exists.
        """
        needle = _normalized_query(q)
        if model_kind is not None:
            require_model_kind(model_kind)
        if not 1 <= limit <= MAX_LIMIT:
            raise ValidationError(f"limit must be between 1 and {MAX_LIMIT}; got {limit}.")
        if offset < 0:
            raise ValidationError(f"offset must be 0 or more; got {offset}.")
        environment_id = self._session.scalar(
            select(Environment.id).where(Environment.name == environment, LIVE_ENVIRONMENT)
        )
        may_read_environment = self._authz.permits(
            p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment)
        )
        if environment_id is None:
            if may_read_environment:
                raise NotFoundError(
                    f"Environment {environment!r} does not exist. List them with 'lakana env list'."
                )
            return SearchPage([], limit, offset, False)
        match = _Match(needle)
        wanted = {kind} if kind is not None else set(SearchKind)
        if model_kind is not None:
            wanted &= {SearchKind.TABLE}
        chain = lookup_chain(languages)
        main = self._main_language
        selects: list[_Candidates] = []
        if SearchKind.TABLE in wanted:
            selects.append(_tables(match, [environment_id], model_kind, main))
            if chain:
                selects.append(
                    _table_translations(match, [environment_id], model_kind, chain, main)
                )
        if SearchKind.COLUMN in wanted:
            selects.append(_columns(match, [environment_id], main))
            if chain:
                selects.append(_column_translations(match, [environment_id], chain, main))
        if SearchKind.RUN in wanted and may_read_environment:
            selects.append(_runs(match, needle, environment_id))
        if may_read_environment:
            selects.extend(_glossary_arms(match, environment_id, wanted, model_kind, chain, main))
        if SearchKind.PRINCIPAL in wanted and self._authz.permits(
            p, Action.MANAGE_PRINCIPAL, Securable.instance_scope()
        ):
            selects.append(_principals(match))
        if not selects:
            return SearchPage([], limit, offset, False)
        hits = self._permitted(p, environment, needle, selects, offset + limit + 1, offset)
        return SearchPage(hits[:limit], limit, offset, len(hits) > limit)

    def _permitted(
        self,
        p: PrincipalRef,
        environment: str,
        needle: str,
        selects: list[_Candidates],
        wanted: int,
        offset: int,
    ) -> list[SearchHit]:
        """Walk the ranked candidates and keep the ones `p` may read, past `offset`.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        needle
            The trimmed query.
        selects
            The candidate selects of the kinds asked for.
        wanted
            Stop once this many permitted candidates have been counted.
        offset
            How many permitted candidates to leave out first.

        Returns
        -------
        list of SearchHit
            The permitted candidates after the first `offset`, best first.
        """
        readable: dict[tuple[str, str], bool] = {}
        may_see = partial(self._may_see, p, environment, readable=readable)
        with closing(_walk(self._session, selects, may_see, max_scanned=MAX_SCANNED)) as walk:
            return [_hit(row, needle) for row in islice(walk, offset, wanted)]

    def table_matches(
        self,
        environment_ids: Sequence[uuid.UUID],
        q: str,
        *,
        readable: Collection[uuid.UUID],
        languages: Sequence[str],
    ) -> TableMatches:
        """Find the readable tables a text matches, by their own texts or their columns'.

        The table and column arms are searched, their translations in the reader's languages
        included, never the glossary's. A table is kept once, by its best ranked row. Only
        readable tables are kept and counted, so the answer says nothing of another table.

        Parameters
        ----------
        environment_ids
            The environments to search.
        q
            The text; trimmed, 1 to 200 characters.
        readable
            The ids of the tables the caller may read, other filters applied.
        languages
            The reader's canonical languages, most wanted first.

        Returns
        -------
        TableMatches
            The tables found in the first `MAX_SCANNED` readable candidates (a table and each
            of its matching columns are candidates), each with its best hit, and whether the
            walk stopped there with more left.

        Raises
        ------
        ValidationError
            If the text is empty after trimming or too long.
        """
        needle = _normalized_query(q)
        match = _Match(needle)
        chain = lookup_chain(languages)
        main = self._main_language
        selects = [
            _tables(match, environment_ids, None, main),
            _columns(match, environment_ids, main),
        ]
        if chain:
            selects.append(_table_translations(match, environment_ids, None, chain, main))
            selects.append(_column_translations(match, environment_ids, chain, main))
        # Every arm joins `tables`: keeping only the readable ids there means an unreadable row
        # is never sorted, read or counted against `MAX_SCANNED`, so `capped` speaks of readable
        # tables only. The ids are written into the SQL, which has no limit on bound parameters.
        only = bindparam("readable", list(readable), expanding=True, literal_execute=True)
        selects = [arm.where(Table.id.in_(only)) for arm in selects]
        hits: dict[uuid.UUID, SearchHit] = {}
        cut: list[bool] = []

        def may_see(row: _Candidate) -> bool:
            """Say whether a candidate names a readable table.

            Parameters
            ----------
            row
                A row of the ranked union.

            Returns
            -------
            bool
                Whether its table is in `readable`.
            """
            return uuid.UUID(row.sort_id) in readable

        walk = _walk(
            self._session,
            selects,
            may_see,
            max_scanned=MAX_SCANNED,
            on_cut=lambda: cut.append(True),
        )
        with closing(walk):
            for row in walk:
                table_id = uuid.UUID(row.sort_id)
                if table_id not in hits:
                    hits[table_id] = _hit(row, needle)
        return TableMatches(hits, bool(cut) and self._another_table(selects, hits))

    def _another_table(self, selects: Sequence[_Candidates], listed: Collection[uuid.UUID]) -> bool:
        """Say whether a candidate names a table not yet listed, after a walk was cut.

        The candidates left after the cut may all be further texts of tables already listed
        (another column, a translation); only a further table means some are not listed.

        Parameters
        ----------
        selects
            The candidate selects the walk ran, already restricted to the readable tables.
        listed
            The tables the walk found.

        Returns
        -------
        bool
            Whether any candidate names another table.
        """
        others = bindparam("listed", list(listed), expanding=True, literal_execute=True)
        union = union_all(*(arm.where(Table.id.not_in(others)) for arm in selects)).subquery()
        return (
            self._session.execute(select(literal(1)).select_from(union).limit(1)).first()
            is not None
        )

    def _may_see(
        self,
        p: PrincipalRef,
        environment: str,
        row: _Candidate,
        readable: dict[tuple[str, str], bool],
    ) -> bool:
        """Say whether `p` may read what a candidate row names.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        row
            A row of the ranked union.
        readable
            The answers already found for tables in this search, filled in here.

        Returns
        -------
        bool
            For a table or column, whether `p` holds `READ_TABLE` on its table, asked as a
            listing asks it; for a run, a principal or a term `True`, their right having been
            checked before they were looked for.
        """
        if row.kind_order not in (0, 1):
            return True
        key = (row.f1, row.f2)
        if key not in readable:
            readable[key] = self._authz.permits(
                p,
                Action.READ_TABLE,
                Securable.table_scope(environment, NamespacePath.parse(row.f1), row.f2),
            )
        return readable[key]


def _walk(
    session: Session,
    selects: Sequence[_Candidates],
    may_see: Callable[[_Candidate], bool],
    *,
    max_scanned: int | None,
    on_cut: Callable[[], None] | None = None,
) -> Generator[_Candidate]:
    """Walk the ranked candidates, best first, and yield the ones the caller may see.

    The ranked select runs once and is read `CHUNK` rows at a time, so a walk costs one sort of
    the union however far it goes. Close the generator when done with it: that closes the select.

    Parameters
    ----------
    session
        Database session.
    selects
        The candidate selects, in the shape of the union.
    may_see
        Whether the caller may read what a row names.
    max_scanned
        Stop after examining this many candidates; `None` walks them all.
    on_cut
        Called once when the walk stops at `max_scanned` with candidates left.

    Yields
    ------
    _Candidate
        Each candidate the caller may see. A table, column or term found by a second text is
        yielded once, by its first, best ranked row, and the repeat is skipped before
        `may_see`, as a hidden one would be.
    """
    union = union_all(*selects).subquery("hits")
    ranked = select(union).order_by(
        union.c.rank,
        union.c.kind_order,
        union.c.sort_name,
        union.c.sort_id,
        # An item's own text, translated or not, comes before a term's at the same rank;
        # then its main-language row before a translation; then, for rows still tied, the
        # smallest term key and the matched text (f3 of a term, f4 of a table or column;
        # f3 of those is fixed per item), so `_hit` shows the same row every time.
        union.c.by_term,
        union.c.sort_source,
        union.c.f7,
        union.c.f3,
        union.c.f4,
    )
    seen: set[tuple[object, ...]] = set()
    scanned = 0
    # One statement, read `CHUNK` rows at a time: re-running it per chunk with an offset would
    # sort the whole union again for each chunk.
    result = session.execute(ranked.execution_options(yield_per=CHUNK))
    try:
        for row in result:
            if max_scanned is not None and scanned >= max_scanned:
                if on_cut is not None:
                    on_cut()
                return
            scanned += 1
            if row.kind_order in (0, 1, 4):
                # A table or column by its table's id (two environments may hold the same
                # dotted name) and its column; a term by its key.
                identity = (
                    (row.kind_order, row.f1)
                    if row.kind_order == _KIND_ORDER[SearchKind.TERM]
                    else (row.kind_order, row.sort_id, row.f3)
                )
                if identity in seen:
                    continue
                seen.add(identity)
            if may_see(row):
                yield row
    finally:
        result.close()


def _header_language(main_language: str) -> ColumnElement[str]:
    """Say which language a table's header texts are in.

    Parameters
    ----------
    main_language
        The instance's main language, for a project that declares none.

    Returns
    -------
    ColumnElement
        The table's `text_language`, else `main_language`.
    """
    return func.coalesce(Table.text_language, literal(main_language, String))


def _tables(
    match: _Match,
    environment_ids: Sequence[uuid.UUID],
    model_kind: str | None,
    main_language: str,
) -> _Candidates:
    """Select the tables, views, external tables and artifacts whose main-language texts match.

    Parameters
    ----------
    match
        The comparisons.
    environment_ids
        The environments.
    model_kind
        Keep only tables that declare this kind, or `None` for every table.
    main_language
        The instance's main language, for a table whose project declares none.

    Returns
    -------
    Select
        The candidates. Label, grain and kind are in the row, so they are read out only after the
        caller's `READ_TABLE` on the table has been checked, like a description. A label ranks
        like a name.
    """
    qualified = (
        func.replace(Namespace.path, NAMESPACE_SEPARATOR, ".").concat(".").concat(Table.name)
    )
    shown = func.coalesce(Table.description_edited, Table.description_project)
    label = func.coalesce(Table.label_edited, Table.label_project)
    in_label = match.contains(label)
    in_description = match.contains(shown)
    in_grain = match.contains(func.coalesce(Table.model_grain, ""))
    rank, by_name, by_label = _name_or_label(match, [Table.name, qualified], label)
    matched = case(
        (by_name, _text("name")),
        (by_label, _text("label")),
        (in_description, _text("description")),
        else_=_text("grain"),
    )
    # Ordered like `matched`: a null text makes its comparison null, never true.
    text = case(
        (by_name, shown), (by_label, label), (in_description, shown), else_=Table.model_grain
    )
    query = (
        _row(
            SearchKind.TABLE,
            rank,
            func.lower(Table.name),
            cast(Table.id, String),
            matched,
            Namespace.path,
            Table.name,
            cast(Table.table_type, String),
            text,
            Table.model_kind,
            case((by_name, null()), else_=_header_language(main_language)),
        )
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(
            Namespace.environment_id.in_(environment_ids),
            or_(by_name, in_label, in_description, in_grain),
        )
    )
    return query.where(Table.model_kind == model_kind) if model_kind is not None else query


def _name_or_label(
    match: _Match, names: Sequence[_Expr], label: _Expr
) -> tuple[ColumnElement[int], ColumnElement[bool], ColumnElement[bool]]:
    """Rank a name and a label together, and say which of the two decided the rank.

    Parameters
    ----------
    match
        The comparisons.
    names
        The expressions that count as the name.
    label
        The shown label; null when there is none.

    Returns
    -------
    tuple
        The rank (the better of the two, 3 when neither matches); whether the name decided it (it
        matches and the label does not match better); whether the label decided it.
    """
    name_rank = match.rank(names, otherwise=3)
    label_rank = match.rank([label], otherwise=3)
    label_wins = label_rank < name_rank
    rank = case((label_wins, label_rank), else_=name_rank)
    return rank, and_(name_rank < 3, ~label_wins), label_wins


def _translation_order(languages: Sequence[str]) -> ColumnElement[int]:
    """Order the translations of one table or column: the reader's languages in their order.

    Parameters
    ----------
    languages
        The reader's languages, truncation included, most wanted first.

    Returns
    -------
    ColumnElement
        An integer from 10 up: ten times the language's place in `languages` (from 1), plus the
        text (a label 0, a description 1, a grain 2; a measure's text 3 more). Main-language rows
        sort at 0 (`_row`), so at the same rank a name or header text keeps the hit, then the
        first of the reader's languages, never the spelling of a tag.
    """
    place = case(
        *(
            (TextTranslation.language == language, literal_column(str((index + 1) * 10)))
            for index, language in enumerate(languages)
        ),
        else_=literal_column(str((len(languages) + 1) * 10)),
    )
    text = case(
        (TextTranslation.field == "label", literal_column("0")),
        (TextTranslation.field == "description", literal_column("1")),
        else_=literal_column("2"),
    )
    subject = case(
        (TextTranslation.subject == "measure", literal_column("3")), else_=literal_column("0")
    )
    return place + text + subject


def _not_main_language(main_language: str) -> ColumnElement[bool]:
    """Leave out a translation in its table's own main language, as `table get` does.

    Parameters
    ----------
    main_language
        The instance's main language, for a table whose project declares none.

    Returns
    -------
    ColumnElement
        True for a row in another language. A row in the main language can only be left over from
        before the main language changed; the header text is the main language's.
    """
    return TextTranslation.language != _header_language(main_language)


def _table_translations(
    match: _Match,
    environment_ids: Sequence[uuid.UUID],
    model_kind: str | None,
    languages: Sequence[str],
    main_language: str,
) -> _Candidates:
    """Select the tables whose label, description or grain matches in one of the reader's languages.

    Parameters
    ----------
    match
        The comparisons.
    environment_ids
        The environments.
    model_kind
        Keep only tables that declare this kind, or `None`.
    languages
        The reader's languages, truncation included.
    main_language
        The instance's main language, for a table whose project declares none.

    Returns
    -------
    Select
        Candidates of the table kind, so `_may_see` checks `READ_TABLE` before any text is read.
        A label ranks like a name; a description or grain after names.
    """
    text = func.coalesce(TextTranslation.text_edited, TextTranslation.text_project)
    is_label = TextTranslation.field == "label"
    query = (
        _row(
            SearchKind.TABLE,
            case((is_label, match.rank([text], otherwise=3)), else_=literal_column("3")),
            func.lower(Table.name),
            cast(Table.id, String),
            case(
                (is_label, _text("label")),
                (TextTranslation.field == "grain", _text("grain")),
                else_=_text("description"),
            ),
            Namespace.path,
            Table.name,
            cast(Table.table_type, String),
            text,
            Table.model_kind,
            TextTranslation.language,
            sort_source=_translation_order(languages),
        )
        .select_from(TextTranslation)
        .join(Table, Table.id == TextTranslation.table_id)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(
            Namespace.environment_id.in_(environment_ids),
            TextTranslation.subject == "table",
            TextTranslation.language.in_(languages),
            _not_main_language(main_language),
            match.contains(text),
        )
    )
    return query.where(Table.model_kind == model_kind) if model_kind is not None else query


def _columns(
    match: _Match, environment_ids: Sequence[uuid.UUID], main_language: str
) -> _Candidates:
    """Select the columns whose name, label, description or measure label or description matches.

    Parameters
    ----------
    match
        The comparisons.
    environment_ids
        The environments.
    main_language
        The instance's main language, for a table whose project declares none.

    Returns
    -------
    Select
        The candidates, in the main language. A label and a measure's label and description are
        in the row, so they are read out only after the caller's `READ_TABLE` on the table has
        been checked, like a description. A label ranks like a name.
    """
    shown = func.coalesce(
        ColumnDescription.description_edited, ColumnDescription.description_project
    )
    label = func.coalesce(ColumnDescription.label_edited, ColumnDescription.label_project)
    named = match.contains(TableColumn.name)
    in_column_label = match.contains(label)
    in_description = match.contains(shown)
    rank, by_name, by_label = _name_or_label(match, [TableColumn.name], label)
    in_label = match.contains(func.coalesce(DeclaredMeasure.label, ""))
    in_measure = or_(in_label, match.contains(func.coalesce(DeclaredMeasure.description, "")))
    matched = case(
        (by_name, _text("name")),
        (by_label, _text("label")),
        (in_description, _text("description")),
        else_=_text("measure"),
    )
    # Ordered like `matched`: a null text makes its comparison null, never true.
    text = case(
        (by_name, shown),
        (by_label, label),
        (in_description, shown),
        (in_label, DeclaredMeasure.label),
        else_=DeclaredMeasure.description,
    )
    return (
        _row(
            SearchKind.COLUMN,
            rank,
            func.lower(TableColumn.name),
            cast(TableColumn.table_id, String),
            matched,
            Namespace.path,
            Table.name,
            TableColumn.name,
            text,
            TableColumn.type,
            case((by_name, null()), else_=_header_language(main_language)),
        )
        .select_from(TableColumn)
        .join(Table, Table.id == TableColumn.table_id)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .outerjoin(
            ColumnDescription,
            and_(
                ColumnDescription.table_id == TableColumn.table_id,
                ColumnDescription.column == TableColumn.name,
            ),
        )
        .outerjoin(
            DeclaredMeasure,
            and_(
                DeclaredMeasure.table_id == TableColumn.table_id,
                DeclaredMeasure.column == TableColumn.name,
            ),
        )
        .where(
            Namespace.environment_id.in_(environment_ids),
            or_(named, in_column_label, in_description, in_measure),
        )
    )


def _column_translations(
    match: _Match,
    environment_ids: Sequence[uuid.UUID],
    languages: Sequence[str],
    main_language: str,
) -> _Candidates:
    """Select the columns whose translated label or description, or measure text, matches.

    Parameters
    ----------
    match
        The comparisons.
    environment_ids
        The environments.
    languages
        The reader's languages, truncation included.
    main_language
        The instance's main language, for a table whose project declares none.

    Returns
    -------
    Select
        Candidates of the column kind, so `_may_see` checks `READ_TABLE` before any text is read.
        A column the table no longer has is never found: the rows join its `table_columns` row.
        A column's label ranks like a name; a description or a measure text after names.
    """
    text = func.coalesce(TextTranslation.text_edited, TextTranslation.text_project)
    is_label = and_(TextTranslation.subject == "column", TextTranslation.field == "label")
    return (
        _row(
            SearchKind.COLUMN,
            case((is_label, match.rank([text], otherwise=3)), else_=literal_column("3")),
            func.lower(TableColumn.name),
            cast(TableColumn.table_id, String),
            case(
                (TextTranslation.subject == "measure", _text("measure")),
                (is_label, _text("label")),
                else_=_text("description"),
            ),
            Namespace.path,
            Table.name,
            TableColumn.name,
            text,
            TableColumn.type,
            TextTranslation.language,
            sort_source=_translation_order(languages),
        )
        .select_from(TextTranslation)
        .join(
            TableColumn,
            and_(
                TableColumn.table_id == TextTranslation.table_id,
                TableColumn.name == TextTranslation.name,
            ),
        )
        .join(Table, Table.id == TableColumn.table_id)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(
            Namespace.environment_id.in_(environment_ids),
            TextTranslation.subject.in_(("column", "measure")),
            TextTranslation.language.in_(languages),
            _not_main_language(main_language),
            match.contains(text),
        )
    )


def _glossary_arms(
    match: _Match,
    environment_id: uuid.UUID,
    wanted: set[SearchKind],
    model_kind: str | None,
    languages: Sequence[str],
    main_language: str,
) -> list[_Candidates]:
    """Select the glossary's candidates: terms, and the tables and columns found by a term.

    Called only for a caller who may read the environment.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.
    wanted
        The kinds asked for.
    model_kind
        Keep only tables that declare this kind, or `None`.
    languages
        The reader's languages, truncation included; empty for none.
    main_language
        The instance's main language, for a term whose project declares none.

    Returns
    -------
    list of Select
        The candidate selects of the kinds in `wanted`.
    """
    selects: list[_Candidates] = []
    if SearchKind.TERM in wanted:
        selects.append(_best_per_item(_terms(match, environment_id), "f1"))
        if languages:
            translated = _term_translations(match, environment_id, languages, main_language)
            selects.append(_best_per_item(translated, "f1"))
    if SearchKind.TABLE in wanted:
        tables = _tables_by_term(match, environment_id, model_kind, languages, main_language)
        selects.append(_best_per_item(tables, "f1", "f2", "f3"))
    if SearchKind.COLUMN in wanted:
        columns = _columns_by_term(match, environment_id, languages, main_language)
        selects.append(_best_per_item(columns, "f1", "f2", "f3"))
    return selects


def _best_per_item(arm: _Candidates, *identity: str) -> _Candidates:
    """Keep only the best row of each item that a glossary arm finds.

    A term is one row per matching synonym, and a table or column found by terms one row per
    linking term and matching word, so without this a glossary author could fill the bounded
    scan (`MAX_SCANNED`) with one item and push every result after it out unseen. The row kept
    is the one the union's order would put first, so the hit is the same as before.

    Parameters
    ----------
    arm
        A candidate select of the union's columns.
    *identity
        The payload columns that name one item, as the scan's deduplication names it.

    Returns
    -------
    Select
        The same columns, one row per item.
    """
    best = (
        func.row_number()
        .over(
            partition_by=[arm.selected_columns[name] for name in identity],
            order_by=[
                arm.selected_columns[name] for name in ("rank", "sort_source", "f7", "f3", "f4")
            ],
        )
        .label("best")
    )
    ranked = arm.add_columns(best).subquery()
    columns = [ranked.c[c.key] for c in arm.selected_columns if c.key is not None]
    return type_cast("_Candidates", select(*columns).where(ranked.c.best == 1))


def _language_place(language: _Expr, languages: Sequence[str]) -> ColumnElement[int]:
    """Give a language's place in the reader's languages.

    Parameters
    ----------
    language
        A language expression.
    languages
        The reader's languages, truncation included; not empty.

    Returns
    -------
    ColumnElement
        Its index in `languages`, or ``len(languages)`` for a language not among them.
    """
    return case(
        *((language == tag, literal_column(str(index))) for index, tag in enumerate(languages)),
        else_=literal_column(str(len(languages))),
    )


def _shown_term_language(
    language: _Expr, languages: Sequence[str], main_language: str
) -> ColumnElement[bool]:
    """Say whether a term shows its translated texts in a language, as the term view does.

    A term shows the translations of the reader's languages that come before its main language
    (all of them when the main language is not among them), so search matches over the same set
    and a hit with `matched: synonym` always shows that synonym on the term.

    Parameters
    ----------
    language
        The text's language.
    languages
        The reader's languages, truncation included; not empty.
    main_language
        The instance's main language, for a term whose project declares none.

    Returns
    -------
    ColumnElement
        True for a language the term shows.
    """
    term_main = func.coalesce(GlossaryTerm.main_language, literal(main_language, String))
    return and_(
        language.in_(languages),
        _language_place(language, languages) < _language_place(term_main, languages),
    )


def _in_chosen_language(
    text: type[GlossaryTermText], languages: Sequence[str]
) -> ColumnElement[bool]:
    """Say whether a translated name or definition is in the language the term view shows.

    The view (`glossary_read._shown`) takes its name and definition from one language: the
    first of the reader's languages, before the term's main language, that has a translated name
    or definition. A text of a later language is shown nowhere, so search does not match it. The
    caller has already restricted the text to the languages the term shows.

    Parameters
    ----------
    text
        The text row, or an alias of `GlossaryTermText`.
    languages
        The reader's languages, truncation included; not empty.

    Returns
    -------
    ColumnElement
        True when no text of the same term, in a name or definition field, is in an earlier
        language of the reader's.
    """
    earlier = aliased(GlossaryTermText)
    return ~exists().where(
        earlier.term_id == text.term_id,
        earlier.field.in_(("name", "definition")),
        earlier.language != MAIN_TEXT_LANGUAGE,
        earlier.language.in_(languages),
        _language_place(earlier.language, languages) < _language_place(text.language, languages),
    )


def _terms(match: _Match, environment_id: uuid.UUID) -> _Candidates:
    """Select the terms whose key, name, main-language synonym or definition matches.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.

    Returns
    -------
    Select
        Candidates of the term kind, one row per matching main-language synonym (one row when
        none matches). A synonym ranks like a name, which wins a tie; a definition ranks after
        names (3).
    """
    synonym = aliased(GlossaryTermText)
    name_rank = match.rank([GlossaryTerm.name, GlossaryTerm.term_key], otherwise=3)
    # A missing synonym (the outer join's null) never compares true, so it ranks 3.
    synonym_rank = match.rank([synonym.text], otherwise=3)
    by_synonym = synonym_rank < name_rank
    by_name = and_(name_rank < 3, ~by_synonym)
    return (
        _row(
            SearchKind.TERM,
            case((by_synonym, synonym_rank), else_=name_rank),
            func.lower(GlossaryTerm.term_key),
            cast(GlossaryTerm.id, String),
            case(
                (by_synonym, _text("synonym")), (by_name, _text("name")), else_=_text("definition")
            ),
            GlossaryTerm.term_key,
            Repository.name,
            case(
                (by_synonym, synonym.text),
                (by_name, GlossaryTerm.name),
                else_=GlossaryTerm.definition,
            ),
            null(),
        )
        .select_from(GlossaryTerm)
        .join(Repository, Repository.id == GlossaryTerm.repository_id)
        .outerjoin(
            synonym,
            and_(
                synonym.term_id == GlossaryTerm.id,
                synonym.language == MAIN_TEXT_LANGUAGE,
                synonym.field == "synonym",
                # Only a matching synonym joins, so a term is one row unless several match.
                match.contains(synonym.text),
            ),
        )
        .where(
            GlossaryTerm.environment_id == environment_id,
            or_(
                name_rank < 3,
                match.contains(synonym.text),
                match.contains(GlossaryTerm.definition),
            ),
        )
    )


def _term_translations(
    match: _Match, environment_id: uuid.UUID, languages: Sequence[str], main_language: str
) -> _Candidates:
    """Select the terms whose translated name, synonym or definition matches.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.
    languages
        The reader's languages, truncation included; not empty.
    main_language
        The instance's main language, for a term whose project declares none.

    Returns
    -------
    Select
        Candidates of the term kind, in the languages the term shows (a name or definition only
        in the one language the term view takes them from). A name or synonym ranks
        like a name; a definition after names. Of one term's texts at one rank, the first of the
        reader's languages comes first, then a name, a synonym, a definition.
    """
    text = GlossaryTermText.text
    field = GlossaryTermText.field
    is_word = field.in_(("name", "synonym"))
    order = case(
        (field == "name", literal_column("0")),
        (field == "synonym", literal_column("1")),
        else_=literal_column("2"),
    )
    place = (_language_place(GlossaryTermText.language, languages) + 1) * 10
    return (
        _row(
            SearchKind.TERM,
            case((is_word, match.rank([text], otherwise=3)), else_=literal_column("3")),
            func.lower(GlossaryTerm.term_key),
            cast(GlossaryTerm.id, String),
            field,
            GlossaryTerm.term_key,
            Repository.name,
            text,
            GlossaryTermText.language,
            sort_source=place + order,
        )
        .select_from(GlossaryTermText)
        .join(GlossaryTerm, GlossaryTerm.id == GlossaryTermText.term_id)
        .join(Repository, Repository.id == GlossaryTerm.repository_id)
        .where(
            GlossaryTerm.environment_id == environment_id,
            GlossaryTermText.language != MAIN_TEXT_LANGUAGE,
            _shown_term_language(GlossaryTermText.language, languages, main_language),
            or_(field == "synonym", _in_chosen_language(GlossaryTermText, languages)),
            match.contains(text),
        )
    )


@dataclass(frozen=True)
class _TermWords:
    """A term's name and synonyms, compared, for the tables and columns its links name.

    Parameters
    ----------
    word
        The alias of the term's texts that count as its words.
    joined
        The condition that outer-joins `word`: a matching main-language synonym, or a matching
        translated name or synonym in a language the term shows; only matching words join, so a
        link is one row unless several of its term's words match.
    rank
        The better of the name's and the word's rank, 3 when neither matches.
    text
        The text that decided the rank; the name wins a tie.
    matches
        True where either matches.
    """

    word: type[GlossaryTermText]
    joined: ColumnElement[bool]
    rank: ColumnElement[int]
    text: ColumnElement[str]
    matches: ColumnElement[bool]


def _term_words(match: _Match, languages: Sequence[str], main_language: str) -> _TermWords:
    """Compare a term's key, name and synonyms, in the languages the term shows them in.

    Parameters
    ----------
    match
        The comparisons.
    languages
        The reader's languages, truncation included; empty for none.
    main_language
        The instance's main language, for a term whose project declares none.

    Returns
    -------
    _TermWords
        The comparisons.
    """
    word = aliased(GlossaryTermText)
    shown = and_(word.language == MAIN_TEXT_LANGUAGE, word.field == "synonym")
    if languages:
        translated = and_(
            word.field.in_(("name", "synonym")),
            _shown_term_language(word.language, languages, main_language),
            or_(word.field == "synonym", _in_chosen_language(word, languages)),
        )
        shown = or_(shown, translated)
    name_rank = match.rank([GlossaryTerm.name, GlossaryTerm.term_key], otherwise=3)
    word_rank = match.rank([word.text], otherwise=3)
    by_word = word_rank < name_rank
    return _TermWords(
        word,
        and_(word.term_id == GlossaryTerm.id, shown, match.contains(word.text)),
        case((by_word, word_rank), else_=name_rank),
        case((by_word, word.text), else_=GlossaryTerm.name),
        or_(name_rank < 3, word_rank < 3),
    )


def _linked_by_term(
    query: _Candidates, words: _TermWords, environment_id: uuid.UUID, *, columns: bool
) -> _Candidates:
    """Join the links of the environment's terms to the tables, or columns, they name.

    A link names its table by the dotted name the file gave, so a link to a table or column that
    does not exist joins nothing.

    Parameters
    ----------
    query
        The candidate select to add the joins to; its columns use `GlossaryTerm`, `Table`,
        `Namespace`, `words` and, for columns, `TableColumn`.
    words
        The term's words compared.
    environment_id
        The environment.
    columns
        Whether the links to columns are joined (to `TableColumn`), else those to tables.

    Returns
    -------
    Select
        The joined select, keeping the links whose term's words match.
    """
    qualified = (
        func.replace(Namespace.path, NAMESPACE_SEPARATOR, ".").concat(".").concat(Table.name)
    )
    query = (
        query.select_from(GlossaryTermLink)
        .join(GlossaryTerm, GlossaryTerm.id == GlossaryTermLink.term_id)
        .join(Namespace, Namespace.environment_id == GlossaryTerm.environment_id)
        .join(
            Table,
            and_(Table.namespace_id == Namespace.id, qualified == GlossaryTermLink.table_name),
        )
    )
    if columns:
        query = query.join(
            TableColumn,
            and_(
                TableColumn.table_id == Table.id,
                TableColumn.name == GlossaryTermLink.column_name,
            ),
        )
    linked = GlossaryTermLink.column_name
    return query.outerjoin(words.word, words.joined).where(
        GlossaryTerm.environment_id == environment_id,
        linked.is_not(None) if columns else linked.is_(None),
        words.matches,
    )


def _tables_by_term(
    match: _Match,
    environment_id: uuid.UUID,
    model_kind: str | None,
    languages: Sequence[str],
    main_language: str,
) -> _Candidates:
    """Select the tables a term links whose name or synonyms match.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.
    model_kind
        Keep only tables that declare this kind, or `None` for every table.
    languages
        The reader's languages, truncation included; empty for none.
    main_language
        The instance's main language, for a term whose project declares none.

    Returns
    -------
    Select
        Candidates of the table kind, so `_may_see` checks `READ_TABLE` before anything of the
        table is read out. The word ranks like a name.
    """
    words = _term_words(match, languages, main_language)
    query = _row(
        SearchKind.TABLE,
        words.rank,
        func.lower(Table.name),
        cast(Table.id, String),
        _text("term"),
        Namespace.path,
        Table.name,
        cast(Table.table_type, String),
        words.text,
        Table.model_kind,
        null(),
        GlossaryTerm.term_key,
        by_term=True,
    )
    query = _linked_by_term(query, words, environment_id, columns=False)
    return query.where(Table.model_kind == model_kind) if model_kind is not None else query


def _columns_by_term(
    match: _Match, environment_id: uuid.UUID, languages: Sequence[str], main_language: str
) -> _Candidates:
    """Select the columns a term links whose name or synonyms match.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.
    languages
        The reader's languages, truncation included; empty for none.
    main_language
        The instance's main language, for a term whose project declares none.

    Returns
    -------
    Select
        Candidates of the column kind, so `_may_see` checks `READ_TABLE` before anything of the
        table is read out. A column the table no longer has is never found. The word ranks like
        a name.
    """
    words = _term_words(match, languages, main_language)
    query = _row(
        SearchKind.COLUMN,
        words.rank,
        func.lower(TableColumn.name),
        cast(TableColumn.table_id, String),
        _text("term"),
        Namespace.path,
        Table.name,
        TableColumn.name,
        words.text,
        TableColumn.type,
        null(),
        GlossaryTerm.term_key,
        by_term=True,
    )
    return _linked_by_term(query, words, environment_id, columns=True)


def _runs(match: _Match, needle: str, environment_id: uuid.UUID) -> _Candidates:
    """Select the runs whose id starts with the text, or whose job or repository matches.

    Parameters
    ----------
    match
        The comparisons.
    needle
        The trimmed query.
    environment_id
        The environment.

    Returns
    -------
    Select
        The candidates.
    """
    hex_text = needle.replace("-", "").lower()
    run_id = func.replace(func.lower(cast(Run.id, String)), "-", "")
    by_id = _HEX.fullmatch(hex_text) is not None
    id_exact = [run_id == hex_text] if by_id else []
    id_prefix = [run_id.like(hex_text + "%")] if by_id else []
    matched = case(
        *((cond, _text("id")) for cond in id_prefix),
        (match.contains(Run.job), _text("job")),
        else_=_text("repository"),
    )
    return (
        _row(
            SearchKind.RUN,
            match.rank(
                [Run.job, Repository.name],
                otherwise=2,
                extra_exact=id_exact,
                extra_prefix=id_prefix,
            ),
            func.lower(func.coalesce(Run.job, Repository.name)),
            cast(Run.id, String),
            matched,
            cast(Run.id, String),
            Run.job,
            Repository.name,
            cast(Run.status, String),
        )
        .join(Repository, Repository.id == Run.repository_id)
        .where(
            Run.environment_id == environment_id,
            or_(match.contains(Run.job), match.contains(Repository.name), *id_prefix),
        )
    )


def _principals(match: _Match) -> _Candidates:
    """Select the principals whose name matches.

    Parameters
    ----------
    match
        The comparisons.

    Returns
    -------
    Select
        The candidates.
    """
    return _row(
        SearchKind.PRINCIPAL,
        match.rank([Principal.name], otherwise=2),
        func.lower(Principal.name),
        cast(Principal.id, String),
        _text("name"),
        Principal.name,
        cast(Principal.type, String),
    ).where(match.contains(Principal.name))


def _hit(row: _Candidate, needle: str) -> SearchHit:
    """Turn a row of the union into a hit.

    Parameters
    ----------
    row
        A row the caller may read.
    needle
        The trimmed query, to cut a snippet around.

    Returns
    -------
    SearchHit
        The hit.
    """
    kind = next(k for k, order in _KIND_ORDER.items() if order == row.kind_order)
    if kind is SearchKind.TERM:
        return SearchHit(
            kind=kind,
            name=row.f1,
            matched=row.matched,
            snippet=_snippet(row.f3, needle),
            term=row.f1,
            repository=row.f2,
            language=row.f4,
            translated=row.sort_source > 0,
        )
    snippet = (
        _snippet(row.f4, needle)
        if row.matched in ("label", "description", "grain", "measure", "term")
        and kind in (SearchKind.TABLE, SearchKind.COLUMN)
        else None
    )
    if kind is SearchKind.TABLE:
        namespace = str(NamespacePath.parse(row.f1))
        return SearchHit(
            kind=kind,
            name=f"{namespace}.{row.f2}",
            matched=row.matched,
            snippet=snippet,
            namespace=namespace,
            table=row.f2,
            table_kind=table_kind(TableType[row.f3]),
            model_kind=row.f5,
            language=row.f6,
            translated=row.sort_source > 0,
            term=row.f7,
        )
    if kind is SearchKind.COLUMN:
        namespace = str(NamespacePath.parse(row.f1))
        return SearchHit(
            kind=kind,
            name=f"{namespace}.{row.f2}.{row.f3}",
            matched=row.matched,
            snippet=snippet,
            namespace=namespace,
            table=row.f2,
            column=row.f3,
            column_type=row.f5,
            language=row.f6,
            translated=row.sort_source > 0,
            term=row.f7,
        )
    if kind is SearchKind.RUN:
        run_id = uuid.UUID(row.f1)
        return SearchHit(
            kind=kind,
            name=str(run_id),
            matched=row.matched,
            run_id=run_id,
            job=row.f2,
            repository=row.f3,
            status=RunStatus[row.f4].value,
        )
    return SearchHit(
        kind=kind,
        name=row.f1,
        matched=row.matched,
        principal=row.f1,
        principal_type=PrincipalType[row.f2].value,
    )
