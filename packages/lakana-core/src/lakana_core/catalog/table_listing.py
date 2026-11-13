"""Every table a caller may read, across environments and namespaces: filtered, searched, sorted.

Readability is decided once per request, with one `permits_each` batch over the live tables the
filters leave, never with `authorize`: reading a list is no attempt, so nothing is audited. The
rows are then scanned in the chosen order, a chunk at a time, from a keyset cursor, and kept when
readable. A search text narrows the readable set through the table and column arms of
`catalog.search`, so a table is never found by the text of one its caller may not read.

The sort keys are the ones stored on the ``tables`` row (`catalog.health.stored_stats`,
`catalog.health_summary`), so a page reads no metadata file and no signal. A null key sorts last
in both orders. The facts a page shows beside them (owner, readers, the reader's own health word)
are read for the page's rows in a fixed number of statements (`table_facts`).
"""

from __future__ import annotations

import enum
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import ColumnElement, Row, Select, distinct, func, or_, select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each
from lakana_core.catalog.health import CheckHealth
from lakana_core.catalog.health_summary import (
    HealthSummary,
    reference_checks,
    shown_health,
)
from lakana_core.catalog.search import CatalogSearch, SearchHit, _normalized_query
from lakana_core.db import db_now
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identifiers import NAMESPACE_SEPARATOR, NamespacePath
from lakana_core.languages import DEFAULT_MAIN_LANGUAGE
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    Environment,
    Namespace,
    Principal,
    Table,
    TableRead,
    TableType,
)
from lakana_core.models.table import table_kind
from lakana_core.pagination import (
    BIGINT_KEY,
    SMALLINT_KEY,
    CursorValue,
    KeyType,
    SortedPosition,
    after_sorted,
    decode_sorted_cursor,
    encode_sorted_cursor,
    sorted_order,
)
from lakana_core.projects.asset_model import require_model_kind
from lakana_core.runs.hidden_checks import ReferenceCheckFilter

__all__ = [
    "CURSOR_KIND",
    "DEFAULT_LIMIT",
    "DEFAULT_ORDER",
    "MAX_LIMIT",
    "READERS_DAYS",
    "SCAN_CHUNK",
    "SortOrder",
    "TableFacts",
    "TableListItem",
    "TableListPage",
    "TableListing",
    "TableMatch",
    "TableOwner",
    "TableSort",
    "TableTree",
    "TreeEnvironment",
    "TreeNamespace",
    "table_facts",
]


class TableSort(enum.StrEnum):
    """What the list is sorted by."""

    NAME = "name"
    OWNER = "owner"
    HEALTH = "health"
    ROWS = "rows"
    SIZE = "size"
    LAST_WRITE = "last_write"
    LAST_READ = "last_read"
    CREATED = "created"


class SortOrder(enum.StrEnum):
    """Which way the list runs."""

    ASC = "asc"
    DESC = "desc"


DEFAULT_ORDER: Mapping[TableSort, SortOrder] = {
    TableSort.NAME: SortOrder.ASC,
    TableSort.OWNER: SortOrder.ASC,
    TableSort.HEALTH: SortOrder.ASC,
    TableSort.ROWS: SortOrder.DESC,
    TableSort.SIZE: SortOrder.DESC,
    TableSort.LAST_WRITE: SortOrder.DESC,
    TableSort.LAST_READ: SortOrder.DESC,
    TableSort.CREATED: SortOrder.DESC,
}
"""The order of each sort when none is asked: names and the worst health first, the most of the
rest first."""

DEFAULT_LIMIT = 50
"""The page size when none is asked."""

MAX_LIMIT = 200
"""The largest page."""

SCAN_CHUNK = 500
"""How many rows the scan reads at a time before it keeps the readable ones."""

READERS_DAYS = 7
"""The window of `readers_7d`, in UTC days, today included."""

CURSOR_KIND = "tables"
"""The list's name in its cursor."""

_KEY_TYPES: Mapping[TableSort, tuple[KeyType, ...]] = {
    TableSort.NAME: (str, str, str),
    TableSort.OWNER: (str,),
    TableSort.HEALTH: (SMALLINT_KEY,),
    TableSort.ROWS: (BIGINT_KEY,),
    TableSort.SIZE: (BIGINT_KEY,),
    TableSort.LAST_WRITE: (datetime,),
    TableSort.LAST_READ: (datetime,),
    TableSort.CREATED: (datetime,),
}
"""The type of each key of a sort, as its cursor holds them; an integer as its column's range
(`Table.health_rank` is a ``SMALLINT``, `row_count` and `size_bytes` ``BIGINT``)."""


# jscpd:ignore-start
# The server's response models and the SDK's records mirror these field by field; core cannot
# import either, and the server validates these dataclasses into its models.
@dataclass(frozen=True)
class TableOwner:
    """Who owns a table.

    Parameters
    ----------
    name
        The principal's name.
    kind
        ``USER`` or ``SERVICE_ACCOUNT``.
    """

    name: str
    kind: str


@dataclass(frozen=True)
class TableMatch:
    """What part of a table a search text matched.

    Parameters
    ----------
    kind
        ``table`` for the table's own text, ``column`` for one of its columns'.
    matched
        The field: ``name``, ``label``, ``description``, ``grain`` or ``measure``.
    column
        For a column match, the column's name.
    column_type
        For a column match, its type.
    snippet
        For a label, description, grain or measure match, the text around the match.
    language
        The language of the matched text; `None` for a name.
    translated
        Whether the matched text is a translation.
    """

    kind: str
    matched: str
    column: str | None
    column_type: str | None
    snippet: str | None
    language: str | None
    translated: bool


@dataclass(frozen=True)
class TableListItem:
    """One table of the list.

    Parameters
    ----------
    environment
        Its environment.
    namespace
        Its namespace, dotted.
    name
        Its name.
    id
        Its id.
    kind
        ``TABLE``, ``EXTERNAL``, ``VIEW`` or ``ARTIFACT``.
    model_kind
        The ``kind`` its asset header declares, if any.
    state
        ``not_published`` or ``kept`` for a managed table so marked; ``published`` otherwise.
    owner
        Its owner; `None` when the owner was deleted.
    created_at
        When it was created.
    row_count
        Its rows, as stored; `None` when not known.
    size_bytes
        The size of its data files; `None` when not known, and whenever `last_write_at` is.
    last_write_at
        When its current snapshot was committed; `None` without one.
    last_read_at
        When a succeeded query last read it; `None` when none did.
    readers_7d
        How many principals read it in the last 7 UTC days, today included.
    health_summary
        Its health in one word, as the caller may see it.
    match
        With a search text, what of the table matched; `None` without one.
    """

    environment: str
    namespace: str
    name: str
    id: uuid.UUID
    kind: str
    model_kind: str | None
    state: Literal["published", "not_published", "kept"]
    owner: TableOwner | None
    created_at: datetime
    row_count: int | None
    size_bytes: int | None
    last_write_at: datetime | None
    last_read_at: datetime | None
    readers_7d: int
    health_summary: HealthSummary
    match: TableMatch | None


@dataclass(frozen=True)
class TableListPage:
    """A page of the list.

    Parameters
    ----------
    items
        The tables, in the list's order.
    next_cursor
        The cursor of the next page; `None` on the last.
    capped
        Whether a search text stopped early while a further readable table matched.
    """

    items: list[TableListItem]
    next_cursor: str | None
    capped: bool


@dataclass(frozen=True)
class TreeNamespace:
    """A namespace of the tree.

    Parameters
    ----------
    path
        Its path, dotted.
    count
        The readable tables in it and beneath it.
    """

    path: str
    count: int


@dataclass(frozen=True)
class TreeEnvironment:
    """An environment of the tree.

    Parameters
    ----------
    name
        Its name.
    count
        The readable tables in it.
    namespaces
        Its namespaces the caller is shown, by path.
    """

    name: str
    count: int
    namespaces: list[TreeNamespace]


@dataclass(frozen=True)
class TableTree:
    """The counts of readable tables, by environment and namespace.

    Parameters
    ----------
    count
        The readable tables in every environment.
    environments
        The environments the caller is shown, by name.
    """

    count: int
    environments: list[TreeEnvironment]


# jscpd:ignore-end


@dataclass(frozen=True)
class TableFacts:
    """What a table shows beside its stored keys, for one reader.

    Parameters
    ----------
    owner
        Its owner; `None` when there is none.
    last_read_at
        When it was last read, as stored.
    readers_7d
        The distinct principals that read it in `READERS_DAYS` UTC days, today included.
    health_summary
        Its stored word, merged with the reference checks the reader may see (`shown_health`).
    """

    owner: TableOwner | None
    last_read_at: datetime | None
    readers_7d: int
    health_summary: HealthSummary


def table_facts(
    session: Session,
    authz: AuthorizationService,
    p: PrincipalRef,
    rows: Sequence[tuple[str, Table]],
    *,
    now: datetime,
) -> dict[uuid.UUID, TableFacts]:
    """Read the owner, the readers and the reader's health word of some tables.

    Three statements for every table at once (owners, readers, reference checks), then one
    `ReferenceCheckFilter` per environment that holds such a check; their work grows with the
    environments and the runs of those checks, never with the rows. The reference checks are
    the failing ones, and the passed ones of a managed table whose stored word is
    ``not_judged``: a reader who may see one is shown ``healthy``.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service, for the reference checks.
    p
        The reader.
    rows
        ``(environment name, row)`` of each table.
    now
        The present time, the end of the readers' window.

    Returns
    -------
    dict
        Table id → its facts.
    """
    if not rows:
        return {}
    ids = [row.id for _, row in rows]
    owners = {
        table_id: TableOwner(name, kind.value)
        for table_id, name, kind in session.execute(
            select(Table.id, Principal.name, Principal.type)
            .join(Principal, Principal.id == Table.owner_id)
            .where(Table.id.in_(ids))
        )
    }
    first_day = now.astimezone(UTC).date() - timedelta(days=READERS_DAYS - 1)
    readers: dict[uuid.UUID, int] = {
        table_id: count
        for table_id, count in session.execute(
            select(TableRead.table_id, func.count(distinct(TableRead.principal_id)))
            .where(TableRead.table_id.in_(ids), TableRead.day >= first_day)
            .group_by(TableRead.table_id)
        )
    }
    environment_of = {row.id: environment for environment, row in rows}
    # Only a managed table is judged, so only its passed checks can be the evidence it lacks.
    unjudged = [
        row.id
        for _, row in rows
        if row.health_item == "not_judged" and table_kind(row.table_type) == "TABLE"
    ]
    filters: dict[str, ReferenceCheckFilter] = {}
    visible: dict[uuid.UUID, list[CheckHealth]] = {}
    for table_id, checks in reference_checks(session, ids, passed_for=unjudged).items():
        environment = environment_of[table_id]
        if environment not in filters:
            filters[environment] = ReferenceCheckFilter(session, authz, p, environment)
        hides = filters[environment].hides
        visible[table_id] = [c for c in checks if not hides(c.run_id, c.name, c.generated)]
    return {
        row.id: TableFacts(
            owner=owners.get(row.id),
            last_read_at=row.last_read_at,
            readers_7d=readers.get(row.id, 0),
            health_summary=shown_health(
                row.health_item, row.health_others, visible.get(row.id, [])
            ),
        )
        for _, row in rows
    }


def _under(namespace: str) -> ColumnElement[bool]:
    """Keep the tables of a namespace and of the namespaces beneath it, never a sibling's.

    The filter is compared with the namespace's path as it is shown, dotted: equal, or followed
    by a dot. So a namespace created before dots were refused, whose component holds a dot, is
    listed by the path it is shown with, whichever of its components holds the dot.

    Parameters
    ----------
    namespace
        The filter, dotted.

    Returns
    -------
    ColumnElement
        The condition on `Namespace.path`. It compares characters, never a pattern, so no
        character of the filter is special and case counts on SQLite as on PostgreSQL (SQLite's
        ``LIKE`` ignores the case of ASCII letters).
    """
    dotted = func.replace(Namespace.path, NAMESPACE_SEPARATOR, ".")
    prefix = namespace + "."
    return or_(dotted == namespace, func.substr(dotted, 1, len(prefix)) == prefix)


def _is_under(path: str, namespace: str) -> bool:
    """Say what `_under` says of one stored path.

    Parameters
    ----------
    path
        A stored namespace path.
    namespace
        The filter, dotted.

    Returns
    -------
    bool
        Whether a table of `path` is listed under `namespace`.
    """
    dotted = path.replace(NAMESPACE_SEPARATOR, ".")
    return dotted == namespace or dotted.startswith(namespace + ".")


def _sort_keys(sort: TableSort) -> list[ColumnElement[Any]]:
    """Give the keys a sort orders by, most significant first; the id comes after them.

    Parameters
    ----------
    sort
        The sort.

    Returns
    -------
    list of ColumnElement
        Name: the table's name, its dotted namespace, its environment's name. Owner: the owner's
        name. Every other sort: its stored key.
    """
    if sort is TableSort.NAME:
        dotted = func.replace(Namespace.path, NAMESPACE_SEPARATOR, ".")
        return [Table.name.expression, dotted, Environment.name.expression]
    column = {
        TableSort.OWNER: Principal.name,
        TableSort.HEALTH: Table.health_rank,
        TableSort.ROWS: Table.row_count,
        TableSort.SIZE: Table.size_bytes,
        TableSort.LAST_WRITE: Table.last_write_at,
        TableSort.LAST_READ: Table.last_read_at,
        TableSort.CREATED: Table.created_at,
    }[sort]
    return [column.expression]


def _state(row: Table) -> Literal["published", "not_published", "kept"]:
    """Decide a table's state as `CatalogService.describe_table` does.

    Parameters
    ----------
    row
        The table.

    Returns
    -------
    str
        ``not_published`` or ``kept`` for a managed table so marked; ``published`` otherwise.
    """
    if row.table_type is not TableType.ICEBERG:
        return "published"
    if row.unpublished_run_id is not None:
        return "not_published"
    return "kept" if row.kept_run_id is not None else "published"


def _match(hit: SearchHit | None) -> TableMatch | None:
    """Keep what of a search hit says which part of the table matched.

    Parameters
    ----------
    hit
        The table's best hit, or `None` without a search.

    Returns
    -------
    TableMatch or None
        The match.
    """
    if hit is None:
        return None
    return TableMatch(
        kind=hit.kind.value,
        matched=hit.matched,
        column=hit.column,
        column_type=hit.column_type,
        snippet=hit.snippet,
        language=hit.language,
        translated=hit.translated,
    )


class TableListing:
    """List and count the tables a caller may read.

    Parameters
    ----------
    session
        Database session; read only.
    authz
        The authorization service; every right is asked with `permits`, never `authorize`.
    main_language
        The instance's main language, for a search text.
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
            Database session; read only.
        authz
            The authorization service.
        main_language
            The instance's main language, for a search text.
        """
        self._session = session
        self._authz = authz
        self._main_language = main_language

    def list(
        self,
        p: PrincipalRef,
        *,
        environment: str | None = None,
        namespace: str | None = None,
        q: str | None = None,
        model_kind: str | None = None,
        sort: TableSort = TableSort.NAME,
        order: SortOrder | None = None,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
        languages: Sequence[str] = (),
    ) -> TableListPage:
        """List one page of the tables `p` may read.

        Parameters
        ----------
        p
            The caller.
        environment
            Only this environment; `None` for every one.
        namespace
            Only this namespace and the ones beneath it, dotted.
        q
            Only tables whose name, label, description, grain, or a column's name, label,
            description or measure, contains this text; trimmed, 1 to 200 characters.
        model_kind
            Only tables whose asset header declares this ``kind``.
        sort
            What the list is sorted by.
        order
            Which way; `None` for the sort's default (`DEFAULT_ORDER`).
        limit
            The page size, 1 to `MAX_LIMIT`.
        cursor
            The `next_cursor` of the previous page, sent with the same filters and sort.
        languages
            The reader's canonical languages, most wanted first, for translated texts.

        Returns
        -------
        TableListPage
            The page. Ties sort by the later keys, and a null key last, in either order.

        Raises
        ------
        ValidationError
            If `limit`, `q`, `model_kind` or `cursor` is not valid.
        NotFoundError
            If `environment` does not exist and `p` may read environments; anyone else gets an
            empty page, as for an environment that exists.
        """
        if not 1 <= limit <= MAX_LIMIT:
            raise ValidationError(f"limit must be between 1 and {MAX_LIMIT}; got {limit}.")
        if q is not None:
            _normalized_query(q)
        if model_kind is not None:
            require_model_kind(model_kind)
        order = order or DEFAULT_ORDER[sort]
        sort_name = f"{sort.value}:{order.value}"
        at = None
        if cursor is not None:
            at = decode_sorted_cursor(CURSOR_KIND, sort_name, cursor, _KEY_TYPES[sort])
        environments = self._environments(p, environment)
        filters: list[ColumnElement[bool]] = [Namespace.environment_id.in_(environments)]
        if namespace is not None:
            filters.append(_under(namespace))
        if model_kind is not None:
            filters.append(Table.model_kind == model_kind)
        allowed = self._readable(p, filters)
        hits: dict[uuid.UUID, SearchHit] = {}
        capped = False
        if q is not None and allowed:
            search = CatalogSearch(self._session, self._authz, self._main_language)
            found = search.table_matches(
                list(environments), q, readable=allowed, languages=languages
            )
            hits, capped = found.hits, found.capped
            allowed = {table_id for table_id in hits if table_id in allowed}
        if not allowed:
            return TableListPage([], None, capped)
        keys = _sort_keys(sort)
        kept = self._scan(filters, keys, order is SortOrder.DESC, at, allowed, limit)
        page = kept[:limit]
        next_cursor = None
        if len(kept) > limit:
            last = page[-1]
            next_cursor = encode_sorted_cursor(
                CURSOR_KIND, sort_name, _values(last, len(keys)), last.Table.id
            )
        facts = table_facts(
            self._session,
            self._authz,
            p,
            [(r.environment, r.Table) for r in page],
            now=db_now(self._session),
        )
        items = [_item(r, facts[r.Table.id], _match(hits.get(r.Table.id))) for r in page]
        return TableListPage(items, next_cursor, capped)

    def tree(self, p: PrincipalRef) -> TableTree:
        """Count the tables `p` may read, by environment and namespace.

        A node's count is what `list` returns for it without a search text. An environment is
        shown with ``READ_ENVIRONMENT`` or a readable table in it; a namespace with
        ``READ_NAMESPACE`` on its environment or a readable table in it or beneath it.

        Parameters
        ----------
        p
            The caller.

        Returns
        -------
        TableTree
            The counts; environments by name, namespaces by dotted path.
        """
        environments = self._session.execute(
            select(Environment.id, Environment.name).where(LIVE_ENVIRONMENT)
        ).all()
        ids = [env_id for env_id, _ in environments]
        tables = self._session.execute(
            select(Namespace.environment_id, Namespace.path, Table.name)
            .join(Namespace, Namespace.id == Table.namespace_id)
            .where(Namespace.environment_id.in_(ids))
        ).all()
        namespaces = self._session.execute(
            select(Namespace.environment_id, Namespace.path).where(
                Namespace.environment_id.in_(ids)
            )
        ).all()
        name_of = dict(environments)
        questions = [
            (Action.READ_TABLE, _table_scope(name_of[env_id], path, name))
            for env_id, path, name in tables
        ]
        for _, name in environments:
            questions.append((Action.READ_ENVIRONMENT, Securable.environment_scope(name)))
            questions.append((Action.READ_NAMESPACE, Securable.environment_scope(name)))
        answers = permits_each(self._authz, p, questions)
        readable: dict[uuid.UUID, Counter[str]] = {env_id: Counter() for env_id in ids}
        for (env_id, path, _), may_read in zip(tables, answers[: len(tables)], strict=True):
            if may_read:
                readable[env_id][path] += 1
        rights = answers[len(tables) :]
        shown: list[TreeEnvironment] = []
        for index, (env_id, name) in enumerate(environments):
            may_read_environment, may_read_namespaces = rights[2 * index : 2 * index + 2]
            counts = readable[env_id]
            if not (may_read_environment or counts):
                continue
            nodes = [
                TreeNamespace(dotted, _count_under(counts, dotted))
                for dotted in sorted(
                    path.replace(NAMESPACE_SEPARATOR, ".")
                    for owner, path in namespaces
                    if owner == env_id
                )
            ]
            nodes = [n for n in nodes if may_read_namespaces or n.count]
            shown.append(TreeEnvironment(name, counts.total(), nodes))
        shown.sort(key=lambda e: e.name)
        return TableTree(sum(e.count for e in shown), shown)

    def _environments(self, p: PrincipalRef, environment: str | None) -> Sequence[uuid.UUID]:
        """Find the live environments a list covers.

        Parameters
        ----------
        p
            The caller.
        environment
            The one asked for, or `None` for every one.

        Returns
        -------
        sequence of uuid.UUID
            Their ids; empty for a missing environment the caller may not read.

        Raises
        ------
        NotFoundError
            If `environment` does not exist and `p` may read environments.
        """
        query = select(Environment.id).where(LIVE_ENVIRONMENT)
        if environment is not None:
            query = query.where(Environment.name == environment)
        found = list(self._session.scalars(query))
        if environment is not None and not found:
            scope = Securable.environment_scope(environment)
            if self._authz.permits(p, Action.READ_ENVIRONMENT, scope):
                raise NotFoundError(
                    f"Environment {environment!r} does not exist. List them with 'lakana env list'."
                )
        return found

    def _readable(self, p: PrincipalRef, filters: Sequence[ColumnElement[bool]]) -> set[uuid.UUID]:
        """Decide which of the filtered tables `p` may read, in one batch.

        Parameters
        ----------
        p
            The caller.
        filters
            The conditions on the table, its namespace and its environment.

        Returns
        -------
        set of uuid.UUID
            The ids of the tables `p` holds ``READ_TABLE`` on.
        """
        rows = self._session.execute(
            select(Table.id, Environment.name, Namespace.path, Table.name)
            .join(Namespace, Namespace.id == Table.namespace_id)
            .join(Environment, Environment.id == Namespace.environment_id)
            .where(*filters)
        ).all()
        answers = permits_each(
            self._authz,
            p,
            [(Action.READ_TABLE, _table_scope(env, path, name)) for _, env, path, name in rows],
        )
        return {row[0] for row, may_read in zip(rows, answers, strict=True) if may_read}

    def _scan(
        self,
        filters: Sequence[ColumnElement[bool]],
        keys: Sequence[ColumnElement[Any]],
        descending: bool,
        at: SortedPosition | None,
        allowed: set[uuid.UUID],
        limit: int,
    ) -> Sequence[Row[*tuple[Any, ...]]]:
        """Read the rows in sort order after a position, keeping the allowed ones.

        Parameters
        ----------
        filters
            The conditions on the table, its namespace and its environment.
        keys
            The sort keys.
        descending
            Whether the list runs from the largest key down.
        at
            Where the previous page ended; `None` for the first page.
        allowed
            The tables the caller may see.
        limit
            The page size; one row more is read, to tell whether a page follows.

        Returns
        -------
        sequence of Row
            At most ``limit + 1`` rows of ``(Table, environment, path, k0…)``, in order.
        """
        query: Select[*tuple[Any, ...]] = (
            select(
                Table,
                Environment.name.label("environment"),
                Namespace.path.label("path"),
                *(key.label(f"k{i}") for i, key in enumerate(keys)),
            )
            .join(Namespace, Namespace.id == Table.namespace_id)
            .join(Environment, Environment.id == Namespace.environment_id)
            .outerjoin(Principal, Principal.id == Table.owner_id)
            .where(*filters)
            .order_by(*sorted_order(keys, Table.id.expression, descending))
            .limit(SCAN_CHUNK)
        )
        kept: list[Row[*tuple[Any, ...]]] = []
        while True:
            chunk = self._session.execute(
                query.where(after_sorted(keys, Table.id.expression, descending, at))
            ).all()
            for row in chunk:
                if row.Table.id in allowed:
                    kept.append(row)
                    if len(kept) > limit:
                        return kept
            if len(chunk) < SCAN_CHUNK:
                return kept
            at = SortedPosition(_values(chunk[-1], len(keys)), chunk[-1].Table.id)


def _values(row: Row[*tuple[Any, ...]], count: int) -> tuple[CursorValue, ...]:
    """Read the sort keys of a scanned row.

    Parameters
    ----------
    row
        A row `TableListing._scan` read.
    count
        How many keys the sort has.

    Returns
    -------
    tuple
        The keys, in order.
    """
    return tuple(row._mapping[f"k{i}"] for i in range(count))


def _table_scope(environment: str, path: str, name: str) -> Securable:
    """Name a table as a securable.

    Parameters
    ----------
    environment
        Its environment.
    path
        Its stored namespace path.
    name
        Its name.

    Returns
    -------
    Securable
        The table's scope.
    """
    return Securable.table_scope(environment, NamespacePath.parse(path), name)


def _count_under(counts: Counter[str], namespace: str) -> int:
    """Count the readable tables `list` shows under a namespace.

    Parameters
    ----------
    counts
        Stored namespace path → its readable tables.
    namespace
        The namespace, dotted.

    Returns
    -------
    int
        The tables in it and beneath it.
    """
    return sum(n for path, n in counts.items() if _is_under(path, namespace))


def _item(row: Row[*tuple[Any, ...]], facts: TableFacts, match: TableMatch | None) -> TableListItem:
    """Build the list's item of a scanned row.

    Parameters
    ----------
    row
        The row.
    facts
        Its facts for the reader.
    match
        What of it a search text matched.

    Returns
    -------
    TableListItem
        The item; its size is null whenever its last write is.
    """
    table: Table = row.Table
    return TableListItem(
        environment=row.environment,
        namespace=row.path.replace(NAMESPACE_SEPARATOR, "."),
        name=table.name,
        id=table.id,
        kind=table_kind(table.table_type),
        model_kind=table.model_kind,
        state=_state(table),
        owner=facts.owner,
        created_at=table.created_at,
        row_count=table.row_count,
        size_bytes=None if table.last_write_at is None else table.size_bytes,
        last_write_at=table.last_write_at,
        last_read_at=facts.last_read_at,
        readers_7d=facts.readers_7d,
        health_summary=facts.health_summary,
        match=match,
    )
