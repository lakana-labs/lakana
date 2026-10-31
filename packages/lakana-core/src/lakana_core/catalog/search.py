"""Search an environment for tables, columns, labels, descriptions, runs and principals.

One query ranks every kind of hit together in the database: an exact match of a name or label
first, then a prefix, then a substring, then a match in a description; within a rank tables come
before columns before runs before principals, then by name. The rights are applied afterwards,
hit by hit, with the checks the listings make, over chunks of the ranked candidates, so no hit,
and no text of one, is read out for a caller who may not read it, and the work is bounded by
`MAX_SCANNED` candidates, not by the size of the environment. Texts in the main language always
match; translated texts match only in the reader's languages, and a table or column found by
several texts is one hit.
"""

from __future__ import annotations

import enum
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from typing import cast as type_cast

from sqlalchemy import (
    ColumnElement,
    Select,
    String,
    Text,
    and_,
    case,
    cast,
    func,
    literal,
    literal_column,
    null,
    or_,
    select,
    union_all,
)
from sqlalchemy.orm import InstrumentedAttribute, Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import NAMESPACE_SEPARATOR, NamespacePath
from lakana_core.languages import DEFAULT_MAIN_LANGUAGE, lookup_chain
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    ColumnDescription,
    DeclaredMeasure,
    Environment,
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
"""How many ranked candidates are fetched, and then checked against the caller's rights, at once."""

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
    matched: str
    f1: str
    f2: str
    f3: str
    f4: str
    f5: str
    f6: str | None
    """The language of a matched text, for a table or column hit not matched by its name."""


class SearchKind(enum.StrEnum):
    """What a hit is."""

    TABLE = "table"
    COLUMN = "column"
    RUN = "run"
    PRINCIPAL = "principal"


_KIND_ORDER = {
    SearchKind.TABLE: 0,
    SearchKind.COLUMN: 1,
    SearchKind.RUN: 2,
    SearchKind.PRINCIPAL: 3,
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
        column, the id for a run, the name for a principal.
    matched
        The field that matched: ``name``, ``label``, ``description``, ``grain``, ``measure`` (a
        column's measure label or description), ``id``, ``job`` or ``repository``.
    snippet
        For a label, description, grain or measure match, at most `SNIPPET_LENGTH` characters of
        the text around the hit.
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
        For a run, its repository.
    status
        For a run, its status.
    principal
        For a principal, its name.
    principal_type
        For a principal, ``USER`` or ``SERVICE_ACCOUNT``.
    language
        For a table or column hit that a text matched, the language of that text; `None` for a
        name match and for runs and principals.
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
        The final tie-break, unique per kind.
    matched
        The field that matched.
    *fields
        Up to six payload columns, as text; the rest are null.

    Returns
    -------
    Select
        A select with the columns of the union.
    """
    payload = [*fields, *(cast(null(), Text) for _ in range(6 - len(fields)))]
    columns = [
        literal_column(str(_KIND_ORDER[kind])).label("kind_order"),
        rank.label("rank"),
        sort_name.label("sort_name"),
        sort_id.label("sort_id"),
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
        """Find tables, columns, labels, descriptions, runs and principals that match a text.

        Rights are those of the listings. A table or column appears only where `READ_TABLE` is
        permitted for its table, a run needs `READ_ENVIRONMENT`, a principal needs the right to
        list principals. Where the caller lacks a right, those hits are left out, not refused.

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
            ``wide`` or ``staging``. Columns, runs and principals have none, so they are left out.
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
        selects: list[_Candidates] = []
        if SearchKind.TABLE in wanted:
            selects.append(_tables(match, environment_id, model_kind, self._main_language))
            if chain:
                selects.append(_table_translations(match, environment_id, model_kind, chain))
        if SearchKind.COLUMN in wanted:
            selects.append(_columns(match, environment_id, self._main_language))
            if chain:
                selects.append(_column_translations(match, environment_id, chain))
        if SearchKind.RUN in wanted and may_read_environment:
            selects.append(_runs(match, needle, environment_id))
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
        union = union_all(*selects).subquery("hits")
        ranked = select(union).order_by(
            union.c.rank, union.c.kind_order, union.c.sort_name, union.c.sort_id
        )
        readable: dict[tuple[str, str], bool] = {}
        seen: set[tuple[object, ...]] = set()
        counted = scanned = position = 0
        hits: list[SearchHit] = []
        done = False
        while not done:
            rows = self._session.execute(ranked.limit(CHUNK).offset(position)).all()
            if not rows:
                break
            position += len(rows)
            for row in rows:
                if scanned >= MAX_SCANNED:
                    done = True
                    break
                scanned += 1
                if row.kind_order in (0, 1):
                    # A table or column found by a second text: the first, better ranked row
                    # stands for it. Skipped before `_may_see`, as a hidden one would be.
                    identity = (row.kind_order, row.f1, row.f2, row.f3)
                    if identity in seen:
                        continue
                    seen.add(identity)
                if not self._may_see(p, environment, row, readable):
                    continue
                counted += 1
                if counted > offset:
                    hits.append(_hit(row, needle))
                if counted >= wanted:
                    done = True
                    break
        return hits

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
            listing asks it; for a run or a principal `True`, their right having been checked
            before they were looked for.
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
    match: _Match, environment_id: uuid.UUID, model_kind: str | None, main_language: str
) -> _Candidates:
    """Select the tables, views, external tables and artifacts whose main-language texts match.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.
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
    named = or_(match.contains(Table.name), match.contains(qualified))
    in_label = match.contains(label)
    in_description = match.contains(shown)
    in_grain = match.contains(func.coalesce(Table.model_grain, ""))
    matched = case(
        (named, _text("name")),
        (in_label, _text("label")),
        (in_description, _text("description")),
        else_=_text("grain"),
    )
    # Ordered like `matched`: a null text makes its comparison null, never true.
    text = case((named, shown), (in_label, label), (in_description, shown), else_=Table.model_grain)
    query = (
        _row(
            SearchKind.TABLE,
            match.rank([Table.name, qualified, label], otherwise=3),
            func.lower(Table.name),
            cast(Table.id, String),
            matched,
            Namespace.path,
            Table.name,
            cast(Table.table_type, String),
            text,
            Table.model_kind,
            case((named, null()), else_=_header_language(main_language)),
        )
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(
            Namespace.environment_id == environment_id,
            or_(named, in_label, in_description, in_grain),
        )
    )
    return query.where(Table.model_kind == model_kind) if model_kind is not None else query


def _translation_sort_id(owner: _Expr) -> ColumnElement[str]:
    """Write the tie-break of a translation row, which sorts after its owner's main row.

    Parameters
    ----------
    owner
        The id the main arm sorts the same table or column by.

    Returns
    -------
    ColumnElement
        The owner's id followed by ``/``, the language and the field: the main arm's sort id is a
        prefix of it, so at the same rank a table or column matched by its name and by a
        translation keeps the name hit.
    """
    return (
        cast(owner, String)
        .concat("/")
        .concat(TextTranslation.language)
        .concat("/")
        .concat(TextTranslation.field)
    )


def _table_translations(
    match: _Match, environment_id: uuid.UUID, model_kind: str | None, languages: Sequence[str]
) -> _Candidates:
    """Select the tables whose label, description or grain matches in one of the reader's languages.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.
    model_kind
        Keep only tables that declare this kind, or `None`.
    languages
        The reader's languages, truncation included.

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
            _translation_sort_id(Table.id),
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
        )
        .select_from(TextTranslation)
        .join(Table, Table.id == TextTranslation.table_id)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(
            Namespace.environment_id == environment_id,
            TextTranslation.subject == "table",
            TextTranslation.language.in_(languages),
            match.contains(text),
        )
    )
    return query.where(Table.model_kind == model_kind) if model_kind is not None else query


def _columns(match: _Match, environment_id: uuid.UUID, main_language: str) -> _Candidates:
    """Select the columns whose name, label, description or measure label or description matches.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.
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
    in_label = match.contains(func.coalesce(DeclaredMeasure.label, ""))
    in_measure = or_(in_label, match.contains(func.coalesce(DeclaredMeasure.description, "")))
    matched = case(
        (named, _text("name")),
        (in_column_label, _text("label")),
        (in_description, _text("description")),
        else_=_text("measure"),
    )
    # Ordered like `matched`: a null text makes its comparison null, never true.
    text = case(
        (named, shown),
        (in_column_label, label),
        (in_description, shown),
        (in_label, DeclaredMeasure.label),
        else_=DeclaredMeasure.description,
    )
    return (
        _row(
            SearchKind.COLUMN,
            match.rank([TableColumn.name, label], otherwise=3),
            func.lower(TableColumn.name),
            cast(TableColumn.table_id, String),
            matched,
            Namespace.path,
            Table.name,
            TableColumn.name,
            text,
            TableColumn.type,
            case((named, null()), else_=_header_language(main_language)),
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
            Namespace.environment_id == environment_id,
            or_(named, in_column_label, in_description, in_measure),
        )
    )


def _column_translations(
    match: _Match, environment_id: uuid.UUID, languages: Sequence[str]
) -> _Candidates:
    """Select the columns whose translated label or description, or measure text, matches.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.
    languages
        The reader's languages, truncation included.

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
            _translation_sort_id(TableColumn.table_id),
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
            Namespace.environment_id == environment_id,
            TextTranslation.subject.in_(("column", "measure")),
            TextTranslation.language.in_(languages),
            match.contains(text),
        )
    )


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
    snippet = (
        _snippet(row.f4, needle)
        if row.matched in ("label", "description", "grain", "measure")
        and kind
        in (
            SearchKind.TABLE,
            SearchKind.COLUMN,
        )
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
