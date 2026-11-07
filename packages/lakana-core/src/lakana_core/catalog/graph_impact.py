"""What depends on a table, a column or a table's key: the metadata graph's `impact` (#374, Q4).

Everything declared that depends on the start is listed, each with a reason:

- references to it, their generated `ref_` checks, and the columns and tables that declare them;
- measures, masks and glossary terms on its columns;
- its checks and row filters, and glossary terms on the table;
- the asset that builds it;
- the assets downstream of it (transitively) and the tables they build;
- the jobs that run any asset listed.

For a column or a key, only what names those columns is certain. The table's hand-written checks,
its row filters and everything downstream may use them, and carry `may_use` (SQL is not parsed).
References to the table and terms on the table name the table, not the column, and are left out.

The walk goes only through `_Context.around`, so it is cut at every node the reader may not see,
and the budget counts it as it counts lineage. A reason names only the start, a table the walk
reached, or the other end of a visible reference or asset; never a hidden node.

Columns are read for the start table (`has_column`) and, in one batch, for the tables that declare
the references found (to tell their columns exist); every other edge leads to a record, an asset
or a table, none of which reads columns. So a question costs the same statements however many
tables it reaches.

Distances are settled last, breadth first over the answer's own edges, so a node met again by a
shorter way passes the shorter distance on to what lies beyond it.
"""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from lakana_core.catalog.graph_load import AssetRecord, CheckRecord, ReferenceRecord
from lakana_core.catalog.graph_model import (
    EdgeKind,
    GraphEdge,
    NodeKind,
    column_node,
    node_kind,
    table_node,
    table_part,
)

if TYPE_CHECKING:
    from lakana_core.catalog.graph_service import _Context

_ON_COLUMN = (
    EdgeKind.REFERENCES,
    EdgeKind.MEASURES,
    EdgeKind.CHECKS,
    EdgeKind.MASKS,
    EdgeKind.LINKS,
)
"""The edges by which something names a column."""

_ON_TABLE = (
    EdgeKind.REFERENCES,
    EdgeKind.CHECKS,
    EdgeKind.FILTERS,
    EdgeKind.LINKS,
    EdgeKind.BUILDS,
)
"""The edges by which something names a table."""

NAMING_KINDS = frozenset({NodeKind.REFERENCE, NodeKind.MEASURE, NodeKind.CHECK, NodeKind.TERM})
"""The kinds of declaration that name a table or column."""

WARNING_KINDS = NAMING_KINDS | {NodeKind.ASSET}
"""What a warning about a removed or retyped table or column lists: those declarations, and the
assets that read it (`impact_of` with ``named_only``)."""

_COLUMN_VERBS = {
    EdgeKind.MEASURES: "a measure on",
    EdgeKind.MASKS: "masks",
    EdgeKind.LINKS: "links",
}


@dataclass(frozen=True)
class Hit:
    """A node impact lists.

    Parameters
    ----------
    distance
        Its distance from the start, in edges, the shortest found.
    reason
        Why it is listed; `None` for the start (and a column start's table).
    may_use
        Whether it only may use the start, rather than naming it.
    via
        The dotted tables the walk passed through to reach it, beyond the start's and its own:
        the table an asset that reads the start builds, for what names a column it takes from
        the start (`_readers`). A warning that names it names them too, so that a reader who may
        not read them is not shown it.
    """

    distance: int
    reason: str | None
    may_use: bool
    via: tuple[str, ...] = ()


class _Answer:
    """The nodes and edges found so far; an edge is added only with both its ends in the answer.

    Parameters
    ----------
    root
        The start, at distance 0.
    """

    def __init__(self, root: str) -> None:
        self.hits: dict[str, Hit] = {root: Hit(0, None, False)}
        self.edges: set[GraphEdge] = set()

    def add(self, node: str, distance: int, reason: str, may_use: bool, edge: GraphEdge) -> bool:
        """Add a node reached through `edge`, whose other end is in the answer.

        A node found again keeps its shortest distance. Its reason changes only when it was
        listed as `may_use` and is now found as certain.

        Parameters
        ----------
        node
            The node reached.
        distance
            Its distance by this way.
        reason
            Why this way lists it.
        may_use
            Whether this way only may use the start.
        edge
            The edge it was reached by.

        Returns
        -------
        bool
            Whether the node is new, or became certain.
        """
        self.edges.add(edge)
        old = self.hits.get(node)
        if old is None or (old.may_use and not may_use):
            shortest = distance if old is None else min(distance, old.distance)
            self.hits[node] = Hit(shortest, reason, may_use, old.via if old else ())
            return True
        if distance < old.distance:
            self.hits[node] = Hit(distance, old.reason, old.may_use, old.via)
        return False


def impact_of(
    ctx: _Context, root: str, *, key: bool, named_only: bool = False
) -> tuple[dict[str, Hit], set[GraphEdge]]:
    """List what depends on a visible table, column or key.

    Parameters
    ----------
    ctx
        The request's context.
    root
        A table or column id the reader may see.
    key
        Whether the question is about the table's declared key (the caller checked it has one).
    named_only
        List only what names the start itself, for a warning (a deploy's, a retyping run's):
        no tables declaring the references found, nothing further downstream, no jobs and not
        the asset that builds the start. Instead the assets that read the start are listed
        (`_readers`): for a column only those whose lineage traces one of their columns to it,
        with what names those columns. Its cost then grows with the declarations on the start
        and its direct readers, not with what lies downstream of them.

    Returns
    -------
    tuple
        The nodes, with their hits, and the edges between them.
    """
    table_id = table_part(root)
    table = table_node(table_id)
    dotted = ctx.graph.tables[table_id].ref.dotted()
    answer = _Answer(root)
    if node_kind(root) is NodeKind.COLUMN:
        answer.hits[table] = Hit(0, None, False)
        answer.edges.add(GraphEdge(EdgeKind.HAS_COLUMN, table, root))
        columns, base, subject = [root], 0, root.partition("/")[2]
    else:
        wanted = set(ctx.graph.tables[table_id].key) if key else None
        columns = [
            column
            for _, column in ctx.around(table, (EdgeKind.HAS_COLUMN,))
            if wanted is None or column.partition("/")[2] in wanted
        ]
        base, subject = 1, ("its key" if key else None)
        for column in columns if key else ():
            edge = GraphEdge(EdgeKind.HAS_COLUMN, table, column)
            answer.add(column, 1, f"a key column of {dotted}", False, edge)
    for column in columns:
        _on_column(ctx, answer, column, dotted, base)
    _on_table(ctx, answer, table, dotted, subject, builders=not named_only)
    if named_only and not key:
        read = root.partition("/")[2] if node_kind(root) is NodeKind.COLUMN else None
        _readers(ctx, answer, table, dotted, read)
    if not named_only:
        _referencing_tables(ctx, answer, table_id)
        _downstream(ctx, answer, table, may_use=subject is not None)
        _jobs(ctx, answer)
    _settle(answer, [table, root])
    return answer.hits, answer.edges


def _referencing_tables(ctx: _Context, answer: _Answer, table_id: uuid.UUID) -> None:
    """Add the column and the table that declare each reference listed, other than the start's.

    The columns of those tables are read in one batch first, so that this costs one statement
    (per 500 tables) however many references there are. A reference is visible only with its
    source table readable, and its source column and table only through `_Context.around`.

    Parameters
    ----------
    ctx
        The request's context.
    answer
        The answer; filled.
    table_id
        The start table.
    """
    references = [
        (node, record)
        for node in sorted(answer.hits)
        if isinstance(record := ctx.graph.records.get(node), ReferenceRecord)
        and record.source_table_id != table_id
    ]
    ctx.graph.preload_columns({record.source_table_id for _, record in references})
    for node, record in references:
        hit = answer.hits[node]
        reason = f"references {record.target_name}.{record.target_column}"
        source = column_node(record.source_table_id, record.source_column)
        for edge, column in ctx.around(node, (EdgeKind.REFERENCES,)):
            if column != source:
                continue
            answer.add(column, hit.distance + 1, reason, False, edge)
            for has, owner in ctx.around(column, (EdgeKind.HAS_COLUMN,)):
                answer.add(owner, hit.distance + 2, reason, False, has)


def _settle(answer: _Answer, starts: list[str]) -> None:
    """Give every node its shortest distance over the answer's edges.

    Parameters
    ----------
    answer
        The answer; its distances are rewritten.
    starts
        The nodes at distance 0: the start, and a column start's table.
    """
    near: defaultdict[str, list[str]] = defaultdict(list)
    for edge in answer.edges:
        near[edge.source].append(edge.target)
        near[edge.target].append(edge.source)
    distance = dict.fromkeys(starts, 0)
    queue = deque(distance)
    while queue:
        node = queue.popleft()
        for other in near[node]:
            if other not in distance:
                distance[other] = distance[node] + 1
                queue.append(other)
    for node, hit in answer.hits.items():
        answer.hits[node] = Hit(distance[node], hit.reason, hit.may_use, hit.via)


def _on_column(ctx: _Context, answer: _Answer, column: str, dotted: str, base: int) -> None:
    """Add what names one column: references, measures, key checks, masks and terms.

    Parameters
    ----------
    ctx
        The request's context.
    answer
        The answer; filled.
    column
        The column, of the start table.
    dotted
        The start table's name.
    base
        The column's distance.
    """
    table_id, name = table_part(column), column.partition("/")[2]
    shown = f"{dotted}.{name}"
    for edge, other in ctx.around(column, _ON_COLUMN):
        record = ctx.graph.records[other]
        if isinstance(record, ReferenceRecord):
            own = record.source_table_id == table_id and record.source_column == name
            reason = f"declared on {shown}" if own else f"references {shown}"
        elif edge.kind is EdgeKind.CHECKS:
            reason = f"checks the key of {dotted}"
        else:
            reason = f"{_COLUMN_VERBS[edge.kind]} {shown}"
        if column not in answer.hits:
            has = GraphEdge(EdgeKind.HAS_COLUMN, table_node(table_id), column)
            answer.add(column, base, f"a column of {dotted}", False, has)
        if answer.add(other, base + 1, reason, False, edge) and isinstance(record, ReferenceRecord):
            _reference_checks(ctx, answer, record, base + 2)


def _reference_checks(
    ctx: _Context, answer: _Answer, reference: ReferenceRecord, distance: int
) -> None:
    """Add the generated `ref_` check of a reference.

    Parameters
    ----------
    ctx
        The request's context.
    answer
        The answer, which holds the reference; filled.
    reference
        The reference.
    distance
        The check's distance.
    """
    source = ctx.graph.tables[reference.source_table_id].ref.dotted()
    reason = f"checks the reference {source}.{reference.source_column}"
    for edge, check in ctx.around(reference.id, (EdgeKind.CHECKS,)):
        answer.add(check, distance, reason, False, edge)


def _on_table(
    ctx: _Context,
    answer: _Answer,
    table: str,
    dotted: str,
    subject: str | None,
    *,
    builders: bool = True,
) -> None:
    """Add what hangs on the start table itself.

    Parameters
    ----------
    ctx
        The request's context.
    answer
        The answer; filled.
    table
        The start table.
    dotted
        Its name.
    subject
        `None` for the whole table. For a column or key, what checks and row filters may use;
        then references to the table, terms on it and its generated checks are left out.
    builders
        Whether the asset that builds the table is listed.
    """
    may = "" if subject is None else f"; may use {subject}"
    for edge, other in ctx.around(table, _ON_TABLE):
        record = ctx.graph.records[other]
        if edge.kind is EdgeKind.BUILDS:
            if builders:
                answer.add(other, 1, f"builds {dotted}", False, edge)
        elif edge.kind is EdgeKind.FILTERS:
            answer.add(other, 1, f"filters {dotted}{may}", subject is not None, edge)
        elif isinstance(record, CheckRecord):
            if subject is None or record.family is None:
                answer.add(other, 1, f"checks {dotted}{may}", subject is not None, edge)
        elif subject is not None:
            continue
        elif isinstance(record, ReferenceRecord):
            reason = f"references {record.target_name}.{record.target_column}"
            if answer.add(other, 1, reason, False, edge):
                _reference_checks(ctx, answer, record, 2)
        else:
            answer.add(other, 1, f"links {dotted}", False, edge)


def _derived(ctx: _Context, asset: str, dotted: str, wanted: str) -> set[str]:
    """Name the output columns of an asset whose lineage traces them to a column.

    Parameters
    ----------
    ctx
        The request's context.
    asset
        The asset's node id; a `reads` edge always starts at an asset
        (`EnvironmentGraph.add_part`).
    dotted
        The source table's name.
    wanted
        The source column, casefolded.

    Returns
    -------
    set of str
        The casefolded output columns derived from it.
    """
    record = cast(AssetRecord, ctx.graph.records[asset])
    return {
        out.casefold()
        for out, source, name in record.lineage
        if source == dotted and name.casefold() == wanted
    }


def _readers(ctx: _Context, answer: _Answer, table: str, dotted: str, column: str | None) -> None:
    """Add the assets that read the start, for a warning (#451).

    For a table, every asset that reads it. For a column, only an asset whose lineage traces one
    of its columns to it (which a manifest records only for an asset that lists its columns),
    and then the table it builds, those columns, and what names them (`_on_column`): a measure
    or a glossary term on a column derived from the start breaks with it.

    Parameters
    ----------
    ctx
        The request's context.
    answer
        The answer; filled.
    table
        The start table.
    dotted
        Its name.
    column
        The start column; `None` for the whole table.
    """
    wanted = None if column is None else column.casefold()
    if wanted is not None and not ctx.graph.column_readers.get((dotted, wanted)):
        return  # no lineage reads it: the table's readers are not walked for nothing
    readers = list(ctx.around(table, (EdgeKind.READS,)))
    if wanted is not None:
        # One query for the columns of every table a deriving reader builds, not one per reader.
        ctx.graph.preload_columns(
            [
                table_part(built)
                for _, asset in readers
                if _derived(ctx, asset, dotted, wanted)
                for _, built in ctx.around(asset, (EdgeKind.BUILDS,))
            ]
        )
    for read, asset in readers:
        if wanted is None:
            answer.add(asset, 1, f"reads {dotted}", False, read)
            continue
        derived = _derived(ctx, asset, dotted, wanted)
        if not derived:
            continue
        answer.add(asset, 1, f"reads {dotted}.{column}", False, read)
        for built_edge, built in ctx.around(asset, (EdgeKind.BUILDS,)):
            built_id = table_part(built)
            built_dotted = ctx.graph.tables[built_id].ref.dotted()
            answer.add(built, 2, f"built from {dotted}.{column}", False, built_edge)
            for name in ctx.graph.columns_of(built_id):
                node = column_node(built_id, name)
                if name.casefold() in derived and ctx.visible.node(node):
                    before = set(answer.hits)
                    _on_column(ctx, answer, node, built_dotted, 3)
                    for found in set(answer.hits) - before:
                        hit = answer.hits[found]
                        answer.hits[found] = Hit(
                            hit.distance, hit.reason, hit.may_use, (*hit.via, built_dotted)
                        )


def _downstream(ctx: _Context, answer: _Answer, table: str, *, may_use: bool) -> None:
    """Add the assets downstream of the table, transitively, and the tables they build.

    An asset is always the source of its `reads` and `builds` edges and a table their target, so
    the kinds alone say the direction.

    Parameters
    ----------
    ctx
        The request's context.
    answer
        The answer; filled.
    table
        The start table.
    may_use
        Whether what is downstream only may use the start (a column or key).
    """
    queue, seen = deque([table]), {table}
    while queue:
        current = queue.popleft()
        reason = f"downstream of {ctx.graph.tables[table_part(current)].ref.dotted()}"
        distance = answer.hits[current].distance + 1
        for read, asset in ctx.around(current, (EdgeKind.READS,)):
            answer.add(asset, distance, reason, may_use, read)
            for built_edge, built in ctx.around(asset, (EdgeKind.BUILDS,)):
                answer.add(built, distance, reason, may_use, built_edge)
                if built not in seen:
                    seen.add(built)
                    queue.append(built)


def _jobs(ctx: _Context, answer: _Answer) -> None:
    """Add the jobs that run any listed asset; a job is `may_use` only through `may_use` assets.

    Parameters
    ----------
    ctx
        The request's context.
    answer
        The answer; filled.
    """
    for node, hit in sorted(answer.hits.items()):
        record = ctx.graph.records.get(node)
        if not isinstance(record, AssetRecord):
            continue
        for edge, job in ctx.around(node, (EdgeKind.RUNS,)):
            answer.add(job, hit.distance + 1, f"runs {record.name}", hit.may_use, edge)
