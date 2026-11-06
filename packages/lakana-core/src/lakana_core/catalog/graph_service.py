"""Walking the metadata graph for one reader: `lineage`, `neighbours`, `impact`, `join_path` (#374).

Every node goes through `_Visibility` before a walk reaches it (spec §4):
- a table, its columns, measures and checks need `READ_TABLE` on the table;
- a reference also needs its target readable, by name when the target does not exist (R3);
- a generated `ref_` check also needs its target readable;
- a row filter or column mask needs `MANAGE_POLICY` and the table (R2);
- an asset needs `READ_ENVIRONMENT` and its output table readable;
- a job or glossary term needs `READ_ENVIRONMENT`.

Which repository builds a table, or declares a check, and who owns the asset that builds a
table, are facts about the environment's deployments: a table, reference, measure or check names
its repository, and a table its builder's owner, only for a reader who holds `READ_ENVIRONMENT`.

A hidden node is never added, never walked through and never counted: the step budget counts
only edges to visible nodes, so neither an answer nor a refusal tells that one is there. The one
exception is time (R8), documented in `docs/agent/security.md`. A start the caller may not see
reads as one that does not exist.
"""

from __future__ import annotations

import bisect
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pyiceberg.types import NestedField
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each
from lakana_core.catalog.glossary_read import term_names
from lakana_core.catalog.graph_impact import NAMING_KINDS, impact_of
from lakana_core.catalog.graph_join import JoinPath, JoinPaths, JoinStep, shortest_paths
from lakana_core.catalog.graph_load import (
    AssetRecord,
    CheckRecord,
    EnvironmentGraph,
    JobRecord,
    ManifestPart,
    MeasureRecord,
    PolicyNode,
    ReferenceRecord,
    TableRecord,
    TermRecord,
    load_graph,
)
from lakana_core.catalog.graph_model import (
    BAD_DEPTH,
    BAD_EDGE_KIND,
    BAD_LIMIT,
    BUSY,
    DEFAULT_GRAPH_DEPTH,
    GRAPH_TIME_BUDGET_SECONDS,
    IMPACT_KINDS,
    JOIN_KINDS,
    KEY_NEEDS_TABLE,
    KIND_WORDS,
    LINEAGE_KINDS,
    MAX_GRAPH_DEPTH,
    MAX_GRAPH_PAGE,
    MAX_GRAPH_STEPS,
    MAX_NODE_ID,
    NO_ENVIRONMENT,
    NO_KEY,
    NO_NODE,
    NO_PATH,
    SAME_TABLE,
    TOO_LARGE,
    EdgeKind,
    GraphEdge,
    GraphNode,
    GraphPage,
    LineageWay,
    NodeKind,
    NodeRef,
    node_kind,
    parse_node,
    table_node,
    table_part,
)
from lakana_core.catalog.texts import TextChooser
from lakana_core.errors import GraphBusyError, NotFoundError, ValidationError
from lakana_core.identifiers import TableRef, shown
from lakana_core.languages import DEFAULT_MAIN_LANGUAGE
from lakana_core.models import Environment, TextTranslation
from lakana_core.models.environment import LIVE_ENVIRONMENT
from lakana_core.pagination import decode_key_cursor, encode_key_cursor

_CHECK_EVERY = 256
"""How many edges examined, visible or hidden, pass between two looks at the clock."""

_LINEAGE_STARTS = frozenset({NodeKind.TABLE, NodeKind.COLUMN, NodeKind.ASSET})

_STEPS: dict[LineageWay, dict[NodeKind, EdgeKind]] = {
    LineageWay.UP: {NodeKind.TABLE: EdgeKind.BUILDS, NodeKind.ASSET: EdgeKind.READS},
    LineageWay.DOWN: {NodeKind.TABLE: EdgeKind.READS, NodeKind.ASSET: EdgeKind.BUILDS},
}
"""Per direction and kind of node: the edge a lineage step follows.

An asset is always the source of its `reads` and `builds` edges and a table always the target,
so the kind alone says which way an edge leads.
"""


@dataclass(frozen=True)
class _Found:
    """A node an answer holds.

    Parameters
    ----------
    distance
        Steps from the start.
    reason
        Why `impact` lists it.
    may_use
        Whether `impact` lists it as possibly using a column.
    """

    distance: int
    reason: str | None = None
    may_use: bool = False


def _pointed_at(graph: EnvironmentGraph) -> Iterator[str]:
    """List the table names the graph's records point at by name.

    Parameters
    ----------
    graph
        The environment's graph.

    Yields
    ------
    str
        An asset's output, a reference's target, a ``references`` check's target; each may
        name a table that does not exist.
    """
    for record in graph.records.values():
        if isinstance(record, AssetRecord):
            yield record.name
        elif isinstance(record, ReferenceRecord):
            yield record.target_name
        elif isinstance(record, CheckRecord) and record.target is not None:
            yield record.target


class _Visibility:
    """Whether one reader may see a node, every right decided in one batch.

    Built once per request: every table of the graph, every table name its records point at,
    `READ_ENVIRONMENT` and `MANAGE_POLICY` are asked in one `permits_each`, so what deciding
    visibility costs does not grow with the tables a walk reaches or hides (R8).

    Parameters
    ----------
    authz
        The authorization service.
    p
        The reader.
    graph
        The environment's graph.
    """

    def __init__(
        self, authz: AuthorizationService, p: PrincipalRef, graph: EnvironmentGraph
    ) -> None:
        self._graph = graph
        environment = graph.environment
        tables = list(graph.tables.values())
        names: dict[str, TableRef] = {}
        for dotted in _pointed_at(graph):
            if dotted not in graph.table_ids and dotted not in names:
                try:
                    names[dotted] = TableRef.from_dotted(environment, dotted)
                except ValidationError:
                    continue
        questions = [
            (Action.READ_ENVIRONMENT, Securable.environment_scope(environment)),
            (Action.MANAGE_POLICY, Securable.instance_scope()),
            *(
                (
                    Action.READ_TABLE,
                    Securable.table_scope(ref.environment, ref.namespace, ref.name),
                )
                for ref in (*(t.ref for t in tables), *names.values())
            ),
        ]
        answers = permits_each(authz, p, questions)
        self._environment, self._policies = answers[0], answers[1]
        self._tables = {t.id: a for t, a in zip(tables, answers[2 : 2 + len(tables)], strict=True)}
        self._names = dict(zip(names, answers[2 + len(tables) :], strict=True))

    def table(self, table_id: uuid.UUID) -> bool:
        """Say whether the reader may read a table of the graph.

        Parameters
        ----------
        table_id
            The table.

        Returns
        -------
        bool
            `READ_TABLE` on its scope; `False` for a table the graph did not load.
        """
        return self._tables.get(table_id, False)

    def name(self, dotted: str) -> bool:
        """Say whether the reader may read a table by its name, whether or not it exists.

        Parameters
        ----------
        dotted
            A dotted name from a stored manifest or reference; untrusted.

        Returns
        -------
        bool
            `READ_TABLE` on its scope; `False` for a name that is not a table name, or one no
            record of the graph points at.
        """
        table_id = self._graph.table_ids.get(dotted)
        if table_id is not None:
            return self.table(table_id)
        return self._names.get(dotted, False)

    def environment(self) -> bool:
        """Say whether the reader holds `READ_ENVIRONMENT`.

        Returns
        -------
        bool
            The answer.
        """
        return self._environment

    def policies(self) -> bool:
        """Say whether the reader may see policies at all (`MANAGE_POLICY`, R2).

        Returns
        -------
        bool
            The answer.
        """
        return self._policies

    def node(self, node_id: str) -> bool:
        """Say whether the reader may see an existing node.

        Parameters
        ----------
        node_id
            A canonical id that `EnvironmentGraph.exists` accepts.

        Returns
        -------
        bool
            The answer, by the rules of the module docstring.
        """
        if node_kind(node_id) in (NodeKind.TABLE, NodeKind.COLUMN):
            return self.table(table_part(node_id))
        record = self._graph.records[node_id]
        if isinstance(record, AssetRecord):
            return self.environment() and self.name(record.name)
        if isinstance(record, JobRecord | TermRecord):
            return self.environment()
        if isinstance(record, ReferenceRecord):
            return self.table(record.source_table_id) and self.name(record.target_name)
        if isinstance(record, MeasureRecord):
            return self.table(record.table_id)
        if isinstance(record, CheckRecord):
            return self.table(record.table_id) and (
                record.target is None or self.name(record.target)
            )
        return self.policies() and self.table(record.table_id)


class _Budget:
    """The step and time budget of one request (R8).

    Parameters
    ----------
    clock
        Seconds, monotonic.
    seconds
        The time budget.
    max_steps
        The step budget.
    """

    def __init__(self, clock: Callable[[], float], seconds: float, max_steps: int) -> None:
        self._clock = clock
        self._seconds = seconds
        self._deadline = clock() + seconds
        self._max_steps = max_steps
        self.steps = 0
        self._looks = 0

    def look(self) -> None:
        """Count one edge examined, to a visible node or a hidden one, and now and then the time.

        Every edge counts here, so a walk that sees little and hides much is timed as any
        other; only `step` counts against the step budget.

        Raises
        ------
        GraphBusyError
            Past the time budget (`BUSY`).
        """
        self._looks += 1
        if self._looks % _CHECK_EVERY == 0:
            self.check()

    def step(self) -> None:
        """Count one edge to a visible node.

        Raises
        ------
        ValidationError
            Past the step budget (`TOO_LARGE`).
        """
        self.steps += 1
        if self.steps > self._max_steps:
            raise ValidationError(TOO_LARGE.format(steps=self._max_steps))

    def check(self) -> None:
        """Look at the clock.

        Raises
        ------
        GraphBusyError
            Past the time budget (`BUSY`).
        """
        if self._clock() > self._deadline:
            raise GraphBusyError(BUSY.format(seconds=self._seconds))


@dataclass
class _Context:
    """One request's graph, reader and budget.

    Parameters
    ----------
    graph
        The environment's graph.
    visible
        The reader's visibility.
    budget
        The request's budget.
    """

    graph: EnvironmentGraph
    visible: _Visibility
    budget: _Budget

    def around(self, node_id: str, kinds: Collection[EdgeKind] = ()) -> list[tuple[GraphEdge, str]]:
        """List a node's edges to existing, visible nodes.

        Parameters
        ----------
        node_id
            The node.
        kinds
            Only these edge kinds; every kind when empty.

        Returns
        -------
        list of tuple
            ``(edge, other end)`` per edge, in the graph's order.
        """
        found: list[tuple[GraphEdge, str]] = []
        for edge in self.graph.edges(node_id, kinds):
            self.budget.look()
            if kinds and edge.kind not in kinds:
                continue
            other = edge.target if edge.source == node_id else edge.source
            if not self.graph.exists(other) or not self.visible.node(other):
                continue
            self.budget.step()
            found.append((edge, other))
        return found


def _check_limit(limit: int) -> None:
    """Refuse a page size out of range.

    Parameters
    ----------
    limit
        The page size asked.

    Raises
    ------
    ValidationError
        Outside 1 to `MAX_GRAPH_PAGE` (`BAD_LIMIT`).
    """
    if not 1 <= limit <= MAX_GRAPH_PAGE:
        raise ValidationError(BAD_LIMIT.format(limit=limit))


def _edge_kinds(kinds: Iterable[str]) -> frozenset[EdgeKind]:
    """Read the edge kinds a caller asked for.

    Parameters
    ----------
    kinds
        Their names.

    Returns
    -------
    frozenset of EdgeKind
        The kinds; empty for every kind.

    Raises
    ------
    ValidationError
        For a name that is not an edge kind (`BAD_EDGE_KIND`).
    """
    wanted: set[EdgeKind] = set()
    for kind in kinds:
        try:
            wanted.add(EdgeKind(kind))
        except ValueError:
            raise ValidationError(BAD_EDGE_KIND.format(kind=shown(kind))) from None
    return frozenset(wanted)


def _key(distance: int, node_id: str) -> str:
    """Build the sort key a cursor holds.

    Parameters
    ----------
    distance
        The node's distance.
    node_id
        Its id.

    Returns
    -------
    str
        A key whose text order is the answer's order.
    """
    return f"{distance:06d}|{node_id}"


_LINEAGE = "graph-lineage"
_NEIGHBOURS = "graph-neighbours"
_IMPACT = "graph-impact"


def _after(list_kind: str, cursor: str | None) -> str | None:
    """Read a cursor before any work is done, so that a bad one costs nothing.

    Parameters
    ----------
    list_kind
        The list asked: `_LINEAGE`, `_NEIGHBOURS` or `_IMPACT`.
    cursor
        The previous page's `next_cursor`, or `None`.

    Returns
    -------
    str or None
        The key to resume after; `None` for the first page.

    Raises
    ------
    ValidationError
        For a cursor this list did not issue.
    """
    return None if cursor is None else decode_key_cursor(list_kind, cursor)


MAX_GRAPH_CURSOR = len(encode_key_cursor(_NEIGHBOURS, _key(10**6 - 1, "\U0010ffff" * MAX_NODE_ID)))
"""The longest `next_cursor` an answer can carry: a node id of `MAX_NODE_ID` characters of
four bytes each, past which no node can be named at all."""


def _translation_rows(
    session: Session, table_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[TextTranslation]]:
    """Read the translation rows of several tables in one statement.

    Parameters
    ----------
    session
        Database session.
    table_ids
        The tables; at most one page's.

    Returns
    -------
    dict
        Table id → its rows; a table without rows is absent.
    """
    rows: defaultdict[uuid.UUID, list[TextTranslation]] = defaultdict(list)
    if table_ids:
        for row in session.scalars(
            select(TextTranslation).where(TextTranslation.table_id.in_(table_ids))
        ):
            rows[row.table_id].append(row)
    return dict(rows)


def _chosen(
    table: TableRecord,
    rows: Mapping[uuid.UUID, list[TextTranslation]],
    languages: Sequence[str],
    main_language: str,
    field: str,
) -> str | None:
    """Choose a table's label or grain in the reader's language.

    Parameters
    ----------
    table
        The table.
    rows
        Translation rows by table (`_translation_rows`).
    languages
        The reader's languages, most wanted first.
    main_language
        The instance's main language, for a table that declares none.
    field
        ``label`` or ``grain``.

    Returns
    -------
    str or None
        The text; `None` when no language has one.
    """
    chooser = TextChooser(table.text_language or main_language, languages, rows.get(table.id, []))
    project, edited = (
        (table.label_project, table.label_edited) if field == "label" else (table.grain, None)
    )
    text = chooser.choose("table", "", field, project, edited)
    return None if text is None else text.text


def _common(node_id: str, found: _Found) -> dict[str, Any]:
    """Give the attributes every node carries.

    Parameters
    ----------
    node_id
        The node.
    found
        What the walk found.

    Returns
    -------
    dict
        `GraphNode`'s ``id``, ``kind``, ``distance``, ``reason`` and ``may_use``.
    """
    return {
        "id": node_id,
        "kind": node_kind(node_id),
        "distance": found.distance,
        "reason": found.reason,
        "may_use": found.may_use,
    }


class GraphService:
    """Answer questions about an environment's metadata graph for one reader at a time.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service; only `permits` is asked.
    main_language
        The instance's main language.
    clock
        Seconds, monotonic; a test passes its own.
    budget_seconds
        The time budget.
    max_steps
        The step budget.
    part
        Reads one repository's part (`load_graph`'s `part`); `None` for no cache.
    fields_of
        Reads a table's current top-level fields (`load_graph`'s `fields_of`), so a column mask
        is linked to its column's current name; `None` links it by the name it was made with.
    """

    def __init__(
        self,
        session: Session,
        authz: AuthorizationService,
        main_language: str = DEFAULT_MAIN_LANGUAGE,
        *,
        clock: Callable[[], float] = time.monotonic,
        budget_seconds: float = GRAPH_TIME_BUDGET_SECONDS,
        max_steps: int = MAX_GRAPH_STEPS,
        part: Callable[[uuid.UUID, str, object], ManifestPart] | None = None,
        fields_of: Callable[[uuid.UUID], Sequence[NestedField] | None] | None = None,
    ) -> None:
        self._session = session
        self._authz = authz
        self._main_language = main_language
        self._clock = clock
        self._budget_seconds = budget_seconds
        self._max_steps = max_steps
        self._part = part
        self._fields_of = fields_of

    def _open(self, p: PrincipalRef, environment: str, node: str) -> _Context:
        """Find the environment, load its graph, and start the budget.

        Parameters
        ----------
        p
            The reader.
        environment
            The environment's name.
        node
            The node the caller sent, for the message of an environment it cannot see.

        Returns
        -------
        _Context
            The request's context.

        Raises
        ------
        NotFoundError
            If the environment does not exist: `NO_ENVIRONMENT` for a caller who may read
            environments, the start's `NO_NODE` for anyone else (as search answers).
        GraphBusyError
            If loading the graph or deciding what the reader may see ran past the time budget.
        """
        budget = _Budget(self._clock, self._budget_seconds, self._max_steps)
        environment_id = self._session.scalar(
            select(Environment.id).where(Environment.name == environment, LIVE_ENVIRONMENT)
        )
        if environment_id is None:
            if self._authz.permits(
                p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment)
            ):
                raise NotFoundError(NO_ENVIRONMENT.format(env=environment))
            raise NotFoundError(NO_NODE.format(node=shown(node), env=shown(environment)))
        # The schema is read only for a reader who can see mask nodes (`_Visibility.policies`).
        sees_masks = self._authz.permits(p, Action.MANAGE_POLICY, Securable.instance_scope())
        graph = load_graph(
            self._session,
            environment_id,
            environment,
            part=self._part,
            fields_of=self._fields_of if sees_masks else None,
        )
        budget.check()
        visible = _Visibility(self._authz, p, graph)
        budget.check()
        return _Context(graph, visible, budget)

    def _root(self, ctx: _Context, node: str, ref: NodeRef) -> str:
        """Find the start node, as the reader may see it.

        Parameters
        ----------
        ctx
            The request's context.
        node
            What the caller sent.
        ref
            What `parse_node` read from it.

        Returns
        -------
        str
            Its canonical id.

        Raises
        ------
        NotFoundError
            If it does not exist or the reader may not see it; one text for both (`NO_NODE`).
        """
        found = ctx.graph.resolve(ref)
        if found is None or not ctx.visible.node(found):
            raise NotFoundError(NO_NODE.format(node=shown(node), env=shown(ctx.graph.environment)))
        return found

    def lineage(
        self,
        p: PrincipalRef,
        environment: str,
        node: str,
        *,
        direction: LineageWay = LineageWay.BOTH,
        depth: int = DEFAULT_GRAPH_DEPTH,
        languages: Sequence[str] = (),
        limit: int = MAX_GRAPH_PAGE,
        cursor: str | None = None,
    ) -> GraphPage:
        """Walk the tables and assets upstream and/or downstream of a table, column or asset.

        `depth` counts the assets crossed (R4). A column starts at its table, plus the references
        that name it and what they join it to, at distance 1.

        Parameters
        ----------
        p
            The reader.
        environment
            The environment's name.
        node
            The start: a node id or a named form.
        direction
            Up, down or both.
        depth
            Steps, 1 to `MAX_GRAPH_DEPTH`.
        languages
            The reader's languages, most wanted first.
        limit
            Nodes per page, 1 to `MAX_GRAPH_PAGE`.
        cursor
            The `next_cursor` of the previous page.

        Returns
        -------
        GraphPage
            One page of the answer.

        Raises
        ------
        ValidationError
            For a depth, limit, node, start kind or cursor out of bounds, or past the step budget.
        NotFoundError
            If the environment or the start does not exist or is hidden.
        GraphBusyError
            Past the time budget.
        """
        if not 1 <= depth <= MAX_GRAPH_DEPTH:
            raise ValidationError(BAD_DEPTH.format(depth=depth))
        _check_limit(limit)
        ref = parse_node(node)
        if ref.kind not in _LINEAGE_STARTS:
            raise ValidationError(
                LINEAGE_KINDS.format(
                    node=shown(node), word=KIND_WORDS[ref.kind], env=shown(environment)
                )
            )
        after = _after(_LINEAGE, cursor)
        ctx = self._open(p, environment, node)
        root = self._root(ctx, node, ref)
        found: dict[str, _Found] = {root: _Found(0)}
        edges: set[GraphEdge] = set()
        starts = {root: 0}
        if node_kind(root) is NodeKind.COLUMN:
            starts = self._column_starts(ctx, root, found, edges)
        ways = (LineageWay.UP, LineageWay.DOWN) if direction is LineageWay.BOTH else (direction,)
        for way in ways:
            self._walk_lineage(ctx, starts, way, depth, found, edges)
        return self._page(ctx, _LINEAGE, found, edges, languages, limit, after)

    @staticmethod
    def _column_starts(
        ctx: _Context, root: str, found: dict[str, _Found], edges: set[GraphEdge]
    ) -> dict[str, int]:
        """Add a column's table, and the references naming it with what they join it to.

        Parameters
        ----------
        ctx
            The request's context.
        root
            The column.
        found
            The answer's nodes; filled.
        edges
            The answer's edges; filled.

        Returns
        -------
        dict
            The column's table at distance 0, where the lineage walk starts.
        """
        table = table_node(table_part(root))
        found[table] = _Found(0)
        for edge, other in ctx.around(root, (EdgeKind.HAS_COLUMN, EdgeKind.REFERENCES)):
            edges.add(edge)
            if edge.kind is EdgeKind.HAS_COLUMN:
                continue
            found.setdefault(other, _Found(1))
            for joined, end in ctx.around(other, (EdgeKind.REFERENCES,)):
                if end == root:
                    continue
                edges.add(joined)
                found.setdefault(end, _Found(1))
                if node_kind(end) is NodeKind.COLUMN:
                    owner = table_node(table_part(end))
                    edges.add(GraphEdge(EdgeKind.HAS_COLUMN, owner, end))
                    found.setdefault(owner, _Found(1))
        return {table: 0}

    @staticmethod
    def _walk_lineage(
        ctx: _Context,
        starts: Mapping[str, int],
        way: LineageWay,
        depth: int,
        found: dict[str, _Found],
        edges: set[GraphEdge],
    ) -> None:
        """Walk one direction, each asset crossed one step (0-1 breadth first).

        A table is reached only from an asset at no cost and an asset only from a table at a
        cost of one, so the first time a node is reached is its shortest, and no node is queued
        twice.

        Parameters
        ----------
        ctx
            The request's context.
        starts
            The start nodes and their distances.
        way
            `LineageWay.UP` or `LineageWay.DOWN`.
        depth
            The most steps.
        found
            The answer's nodes; filled, each at its shortest distance.
        edges
            The answer's edges; filled.
        """
        best = dict(starts)
        queue = deque(starts.items())
        steps = _STEPS[way]
        while queue:
            current, distance = queue.popleft()
            for edge, other in ctx.around(current, (steps[node_kind(current)],)):
                cost = 1 if node_kind(other) is NodeKind.ASSET else 0
                reached = distance + cost
                if reached > depth:
                    continue
                edges.add(edge)
                if other not in best:
                    best[other] = reached
                    shortest = min(reached, found[other].distance) if other in found else reached
                    found[other] = _Found(shortest)
                    (queue.appendleft if cost == 0 else queue.append)((other, reached))

    def neighbours(
        self,
        p: PrincipalRef,
        environment: str,
        node: str,
        *,
        kinds: Sequence[str] = (),
        languages: Sequence[str] = (),
        limit: int = MAX_GRAPH_PAGE,
        cursor: str | None = None,
    ) -> GraphPage:
        """List the nodes one edge away from a node, in either direction.

        Parameters
        ----------
        p
            The reader.
        environment
            The environment's name.
        node
            The node: an id or a named form.
        kinds
            Only edges of these kinds; every kind when empty.
        languages
            The reader's languages, most wanted first.
        limit
            Nodes per page, 1 to `MAX_GRAPH_PAGE`.
        cursor
            The `next_cursor` of the previous page.

        Returns
        -------
        GraphPage
            The node at distance 0, its neighbours at 1.

        Raises
        ------
        ValidationError
            For a kind, limit, node or cursor out of bounds, or past the step budget.
        NotFoundError
            If the environment or the node does not exist or is hidden.
        GraphBusyError
            Past the time budget.
        """
        wanted = _edge_kinds(kinds)
        _check_limit(limit)
        ref = parse_node(node)
        after = _after(_NEIGHBOURS, cursor)
        ctx = self._open(p, environment, node)
        root = self._root(ctx, node, ref)
        found: dict[str, _Found] = {root: _Found(0)}
        edges: set[GraphEdge] = set()
        for edge, other in ctx.around(root, wanted):
            edges.add(edge)
            found.setdefault(other, _Found(1))
        return self._page(ctx, _NEIGHBOURS, found, edges, languages, limit, after)

    def impact(
        self,
        p: PrincipalRef,
        environment: str,
        node: str,
        *,
        key: bool = False,
        languages: Sequence[str] = (),
        limit: int = MAX_GRAPH_PAGE,
        cursor: str | None = None,
    ) -> GraphPage:
        """List everything declared that depends on a table, a column or a table's key.

        Each node but the start carries its `reason`; what only may use a column or key carries
        `may_use` (`graph_impact`). The walk is cut at every node the reader may not see.

        Parameters
        ----------
        p
            The reader.
        environment
            The environment's name.
        node
            The start, a table or a column: a node id or a named form.
        key
            Ask about the table's declared key instead of the whole table.
        languages
            The reader's languages, most wanted first.
        limit
            Nodes per page, 1 to `MAX_GRAPH_PAGE`.
        cursor
            The `next_cursor` of the previous page.

        Returns
        -------
        GraphPage
            One page of the answer: the start at distance 0 (with its table, for a column), then
            the rest by distance in edges.

        Raises
        ------
        ValidationError
            For a start that is not a table or column (`IMPACT_KINDS`), `key` with a column
            (`KEY_NEEDS_TABLE`), `key` on a table without a declared key (`NO_KEY`), a node,
            limit or cursor out of bounds, or past the step budget.
        NotFoundError
            If the environment or the start does not exist or is hidden.
        GraphBusyError
            Past the time budget.
        """
        _check_limit(limit)
        ref = parse_node(node)
        if ref.kind not in (NodeKind.TABLE, NodeKind.COLUMN):
            raise ValidationError(
                IMPACT_KINDS.format(
                    node=shown(node), word=KIND_WORDS[ref.kind], env=shown(environment)
                )
            )
        if key and ref.kind is NodeKind.COLUMN:
            raise ValidationError(KEY_NEEDS_TABLE.format(node=shown(node)))
        after = _after(_IMPACT, cursor)
        ctx = self._open(p, environment, node)
        root = self._root(ctx, node, ref)
        table = ctx.graph.tables[table_part(root)]
        if key and not table.key:
            raise ValidationError(NO_KEY.format(table=table.ref.dotted(), env=shown(environment)))
        hits, edges = impact_of(ctx, root, key=key)
        found = {n: _Found(h.distance, h.reason, h.may_use) for n, h in hits.items()}
        return self._page(ctx, _IMPACT, found, edges, languages, limit, after)

    def join_path(
        self,
        p: PrincipalRef,
        environment: str,
        start: str,
        goal: str,
        *,
        languages: Sequence[str] = (),
    ) -> JoinPaths:
        """Find every declared path of minimal length joining two tables.

        A path has at most `MAX_JOIN_HOPS` steps, and at most `MAX_JOIN_PATHS` paths are
        returned (`graph_join`). No path, and none the reader may see, give the same `NO_PATH`.

        Parameters
        ----------
        p
            The reader.
        environment
            The environment's name.
        start
            The table to start from: a node id or a dotted name.
        goal
            The table to reach.
        languages
            The reader's languages, most wanted first, for the grains.

        Returns
        -------
        JoinPaths
            The paths, or none with the reason.

        Raises
        ------
        ValidationError
            For a start or goal that is not a table (`JOIN_KINDS`), the same table twice
            (`SAME_TABLE`), a node out of bounds, or past the step budget.
        NotFoundError
            If the environment, `start` or `goal` does not exist or is hidden.
        GraphBusyError
            Past the time budget.
        """
        refs = [parse_node(text) for text in (start, goal)]
        for text, ref in zip((start, goal), refs, strict=True):
            if ref.kind is not NodeKind.TABLE:
                raise ValidationError(
                    JOIN_KINDS.format(node=shown(text), word=KIND_WORDS[ref.kind])
                )
        ctx = self._open(p, environment, start)
        first, last = self._root(ctx, start, refs[0]), self._root(ctx, goal, refs[1])
        tables = ctx.graph.tables
        a, b = tables[table_part(first)], tables[table_part(last)]
        if a.id == b.id:
            raise ValidationError(SAME_TABLE.format(table=a.ref.dotted(), env=shown(environment)))
        found, more = shortest_paths(ctx, a.id, b.id)
        if not found:
            reason = NO_PATH.format(
                start=a.ref.dotted(), goal=b.ref.dotted(), env=shown(environment)
            )
            return JoinPaths((), reason, False)
        reached = {a.id} | {hop.to_table for path in found for hop in path}
        rows = _translation_rows(self._session, reached)
        grains = {
            t: _chosen(tables[t], rows, languages, self._main_language, "grain") for t in reached
        }
        paths: list[JoinPath] = []
        for path in found:
            steps: list[JoinStep] = []
            here = a.id
            for hop in path:
                there, record = hop.to_table, hop.reference
                own, target = (record.source_column,), (record.target_column,)
                steps.append(
                    JoinStep(
                        reference=record.id,
                        from_table=table_node(here),
                        from_name=tables[here].ref.dotted(),
                        to_table=table_node(there),
                        to_name=tables[there].ref.dotted(),
                        from_columns=own if hop.forward else target,
                        to_columns=target if hop.forward else own,
                        direction="many_to_one" if hop.forward else "one_to_many",
                        fans_out=not hop.forward,
                        from_model_kind=tables[here].model_kind,
                        from_grain=grains[here],
                        to_model_kind=tables[there].model_kind,
                        to_grain=grains[there],
                    )
                )
                here = there
            paths.append(JoinPath(tuple(steps)))
        return JoinPaths(tuple(paths), None, more)

    def breakage(
        self, p: PrincipalRef, environment: str, nodes: Sequence[str]
    ) -> dict[str, tuple[tuple[GraphNode, tuple[str, ...]], ...]]:
        """List the declarations that name each of several tables and columns, for a deploy.

        Only the references, measures, checks and glossary terms that name a node itself are
        listed (`impact_of` with ``named_only``), never what only may use it: nothing
        downstream is walked, and only those declarations are built, in one build for every
        node. So the cost grows with the declarations on the nodes, not with what lies
        downstream of them, and one budget covers it all: the clock is read after each walk
        and after the build. A node that does not exist, or that `p` may not see, gets
        nothing.

        Parameters
        ----------
        p
            The deployer, whose visibility applies.
        environment
            The environment's name.
        nodes
            Named forms: a dotted table, or ``column:<table>/<column>``.

        Returns
        -------
        dict
            Node → each declaration naming it, with the dotted tables it names
            (`_named_tables`), in node id order.

        Raises
        ------
        ValidationError
            For a node out of bounds, or past the step budget.
        GraphBusyError
            Past the time budget.
        NotFoundError
            If the environment does not exist.
        """
        ctx = self._open(p, environment, nodes[0] if nodes else "")
        walked: list[tuple[str, list[tuple[str, _Found]]]] = []
        for node in nodes:
            found = ctx.graph.resolve(parse_node(node))
            if found is None or not ctx.visible.node(found):
                continue
            hits, _ = impact_of(ctx, found, key=False, named_only=True)
            walked.append(
                (
                    node,
                    [
                        (n, _Found(h.distance, h.reason))
                        for n, h in sorted(hits.items())
                        if not h.may_use and node_kind(n) in NAMING_KINDS
                    ],
                )
            )
            ctx.budget.check()
        # One build for every answer, so its statements do not grow with the number of nodes.
        built = self._nodes(ctx, [e for _, entries in walked for e in entries], ())
        ctx.budget.check()
        answers: dict[str, tuple[tuple[GraphNode, tuple[str, ...]], ...]] = {}
        start = 0
        for node, entries in walked:
            end = start + len(entries)
            answers[node] = tuple((g, _named_tables(ctx.graph, g.id)) for g in built[start:end])
            start = end
        return answers

    def _page(
        self,
        ctx: _Context,
        list_kind: str,
        found: Mapping[str, _Found],
        edges: Iterable[GraphEdge],
        languages: Sequence[str],
        limit: int,
        after: str | None,
    ) -> GraphPage:
        """Cut one page out of an answer (R7).

        Parameters
        ----------
        ctx
            The request's context.
        list_kind
            The cursor's list: ``graph-lineage``, ``graph-neighbours``, ``graph-impact``.
        found
            Every node of the answer.
        edges
            Every edge of the answer; both ends of each are in `found`.
        languages
            The reader's languages.
        limit
            Nodes per page.
        after
            The key the previous page's cursor holds (`_after`); `None` for the first page.

        Returns
        -------
        GraphPage
            The page.
        """
        order = sorted(found, key=lambda n: (found[n].distance, n))
        keys = [_key(found[n].distance, n) for n in order]
        start = 0 if after is None else bisect.bisect_right(keys, after)
        end = min(start + limit, len(order))
        position = {n: i for i, n in enumerate(order)}
        shown_edges = sorted(
            e for e in edges if start <= max(position[e.source], position[e.target]) < end
        )
        nodes = self._nodes(ctx, [(n, found[n]) for n in order[start:end]], languages)
        next_cursor = encode_key_cursor(list_kind, keys[end - 1]) if end < len(order) else None
        return GraphPage(tuple(nodes), tuple(shown_edges), next_cursor)

    def _nodes(
        self, ctx: _Context, entries: Sequence[tuple[str, _Found]], languages: Sequence[str]
    ) -> list[GraphNode]:
        """Build a page's nodes, with their texts in the reader's language.

        One statement reads the page's tables' translations, and one (`term_names`) the
        page's terms' texts. The page's columns are read with `preload_columns`.

        Parameters
        ----------
        ctx
            The request's context.
        entries
            The page's node ids and what the walk found.
        languages
            The reader's languages.

        Returns
        -------
        list of GraphNode
            The nodes, in the order given.
        """
        graph = ctx.graph
        tables = [table_part(n) for n, _ in entries if node_kind(n) is NodeKind.TABLE]
        graph.preload_columns(
            {table_part(n) for n, _ in entries if node_kind(n) is NodeKind.COLUMN}
        )
        rows = _translation_rows(self._session, tables)
        terms = [r.row_id for n, _ in entries if isinstance(r := graph.records.get(n), TermRecord)]
        names = term_names(self._session, terms, languages, self._main_language)
        return [self._node(ctx, n, f, rows, languages, names) for n, f in entries]

    def _node(
        self,
        ctx: _Context,
        node_id: str,
        found: _Found,
        rows: Mapping[uuid.UUID, list[TextTranslation]],
        languages: Sequence[str],
        names: Mapping[uuid.UUID, str],
    ) -> GraphNode:
        """Build one node with the attributes its reader may see.

        Parameters
        ----------
        ctx
            The request's context.
        node_id
            The node; visible to the reader.
        found
            What the walk found.
        rows
            The page's tables' translation rows.
        languages
            The reader's languages.
        names
            The page's terms' shown names.

        Returns
        -------
        GraphNode
            The node.
        """
        graph = ctx.graph
        base = _common(node_id, found)
        deployed = ctx.visible.environment()
        if base["kind"] is NodeKind.TABLE:
            table = graph.tables[table_part(node_id)]
            builder = graph.builder_of.get(table.id)
            return GraphNode(
                name=table.ref.dotted(),
                table_type=table.table_type,
                model_kind=table.model_kind,
                grain=_chosen(table, rows, languages, self._main_language, "grain"),
                key=table.key or None,
                owner=builder.owner if builder and deployed else None,
                repository=_repository(graph, table.id) if deployed else None,
                label=_chosen(table, rows, languages, self._main_language, "label"),
                **base,
            )
        if base["kind"] is NodeKind.COLUMN:
            table_id, column = table_part(node_id), node_id.partition("/")[2]
            return GraphNode(
                name=f"{graph.tables[table_id].ref.dotted()}.{column}",
                column_type=graph.columns_of(table_id).get(column),
                **base,
            )
        record = graph.records[node_id]
        if isinstance(record, AssetRecord):
            return GraphNode(
                name=record.name, owner=record.owner, repository=record.repository, **base
            )
        if isinstance(record, JobRecord):
            name = f"{record.repository}/{record.name}"
            return GraphNode(name=name, repository=record.repository, **base)
        if isinstance(record, TermRecord):
            return GraphNode(
                name=record.key,
                owner=record.owner,
                repository=record.repository,
                label=names.get(record.row_id),
                **base,
            )
        if isinstance(record, ReferenceRecord):
            source = f"{graph.tables[record.source_table_id].ref.dotted()}.{record.source_column}"
            return GraphNode(
                name=f"{source} → {record.target_name}.{record.target_column}",
                repository=_repository(graph, record.source_table_id) if deployed else None,
                **base,
            )
        if isinstance(record, MeasureRecord):
            table_name = graph.tables[record.table_id].ref.dotted()
            return GraphNode(
                name=f"{table_name}.{record.column}",
                repository=_repository(graph, record.table_id) if deployed else None,
                **base,
            )
        if isinstance(record, CheckRecord):
            table_name = graph.tables[record.table_id].ref.dotted()
            return GraphNode(
                name=f"{table_name}/{record.check}",
                repository=record.repository if deployed else None,
                **base,
            )
        assert isinstance(record, PolicyNode)  # nosec B101 -- the last kind of record
        table_name = graph.tables[record.table_id].ref.dotted()
        name = (
            f"row filter on {table_name}"
            if record.column is None
            else f"column mask on {table_name}.{record.column}"
        )
        return GraphNode(name=name, **base)


def _repository(graph: EnvironmentGraph, table_id: uuid.UUID) -> str | None:
    """Name the repository that builds a table.

    Parameters
    ----------
    graph
        The environment's graph.
    table_id
        The table.

    Returns
    -------
    str or None
        The first repository by name whose asset builds it; `None` when none does.
    """
    builder = graph.builder_of.get(table_id)
    return builder.repository if builder else None


def _named_tables(graph: EnvironmentGraph, node_id: str) -> tuple[str, ...]:
    """Name the tables a warning about a node names, so a reader sees it only if it may read them.

    Parameters
    ----------
    graph
        The environment's graph.
    node_id
        The node.

    Returns
    -------
    tuple of str
        Dotted tables: a reference its own and its target; a measure its table; a check its
        table, and a generated ``ref_`` check its target too (R3); any other node none.
    """
    record = graph.records.get(node_id)
    if isinstance(record, ReferenceRecord):
        return (graph.tables[record.source_table_id].ref.dotted(), record.target_name)
    if isinstance(record, CheckRecord):
        table = graph.tables[record.table_id].ref.dotted()
        return (table, record.target) if record.target else (table,)
    if isinstance(record, MeasureRecord):
        return (graph.tables[record.table_id].ref.dotted(),)
    return ()
