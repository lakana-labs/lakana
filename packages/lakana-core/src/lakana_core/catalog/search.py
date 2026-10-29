"""Search an environment for tables, columns, descriptions, runs and principals.

One query ranks every kind of hit together in the database: an exact match first, then a prefix,
then a substring, then a match in a description; within a rank tables come before columns before
runs before principals, then by name. The rights are applied afterwards, hit by hit, with the
checks the listings make, over chunks of the ranked candidates, so no hit, and no text of one, is
read out for a caller who may not read it, and the work is bounded by `MAX_SCANNED` candidates,
not by the size of the environment.
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
    not_,
    null,
    or_,
    select,
    union_all,
)
from sqlalchemy.orm import InstrumentedAttribute, Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import NAMESPACE_SEPARATOR, NamespacePath
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    ColumnDescription,
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
        The field that matched: ``name``, ``description``, ``grain``, ``id``, ``job`` or
        ``repository``.
    snippet
        For a description or grain match, at most `SNIPPET_LENGTH` characters of the text around
        the hit.
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
        Up to five payload columns, as text; the rest are null.

    Returns
    -------
    Select
        A select with the columns of the union.
    """
    payload = [*fields, *(cast(null(), Text) for _ in range(5 - len(fields)))]
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
    """

    def __init__(self, session: Session, authz: AuthorizationService) -> None:
        """Bind the session and the authorization.

        Parameters
        ----------
        session
            Database session.
        authz
            The authorization service the listings use.
        """
        self._session = session
        self._authz = authz

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
    ) -> SearchPage:
        """Find tables, columns, descriptions, runs and principals that match a text.

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

        Returns
        -------
        SearchPage
            The page.

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
        selects: list[_Candidates] = []
        if SearchKind.TABLE in wanted:
            selects.append(_tables(match, environment_id, model_kind))
        if SearchKind.COLUMN in wanted:
            selects.append(_columns(match, environment_id))
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


def _tables(match: _Match, environment_id: uuid.UUID, model_kind: str | None) -> _Candidates:
    """Select the tables, views, external tables and artifacts that match.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.
    model_kind
        Keep only tables that declare this kind, or `None` for every table.

    Returns
    -------
    Select
        The candidates. Grain and kind are in the row, so they are read out only after the
        caller's `READ_TABLE` on the table has been checked, like a description.
    """
    qualified = (
        func.replace(Namespace.path, NAMESPACE_SEPARATOR, ".").concat(".").concat(Table.name)
    )
    shown = func.coalesce(Table.description_edited, Table.description_project)
    named = or_(match.contains(Table.name), match.contains(qualified))
    in_description = match.contains(shown)
    in_grain = match.contains(func.coalesce(Table.model_grain, ""))
    matched = case(
        (named, _text("name")), (in_description, _text("description")), else_=_text("grain")
    )
    text = case((and_(not_(named), not_(in_description)), Table.model_grain), else_=shown)
    query = (
        _row(
            SearchKind.TABLE,
            match.rank([Table.name, qualified], otherwise=3),
            func.lower(Table.name),
            cast(Table.id, String),
            matched,
            Namespace.path,
            Table.name,
            cast(Table.table_type, String),
            text,
            Table.model_kind,
        )
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(Namespace.environment_id == environment_id, or_(named, in_description, in_grain))
    )
    return query.where(Table.model_kind == model_kind) if model_kind is not None else query


def _columns(match: _Match, environment_id: uuid.UUID) -> _Candidates:
    """Select the columns whose name or description matches.

    Parameters
    ----------
    match
        The comparisons.
    environment_id
        The environment.

    Returns
    -------
    Select
        The candidates.
    """
    shown = func.coalesce(
        ColumnDescription.description_edited, ColumnDescription.description_project
    )
    named = match.contains(TableColumn.name)
    return (
        _row(
            SearchKind.COLUMN,
            match.rank([TableColumn.name], otherwise=3),
            func.lower(TableColumn.name),
            cast(TableColumn.table_id, String),
            case((named, _text("name")), else_=_text("description")),
            Namespace.path,
            Table.name,
            TableColumn.name,
            shown,
            TableColumn.type,
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
        .where(Namespace.environment_id == environment_id, or_(named, match.contains(shown)))
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
        if row.matched in ("description", "grain")
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
