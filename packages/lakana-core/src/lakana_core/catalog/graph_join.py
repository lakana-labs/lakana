"""How two tables join over declared references: the metadata graph's `join-path` (#374, Q3).

A step follows a reference in either direction. From the table that declares it to its target it
is `many_to_one`; back the other way it is `one_to_many` and fans out. A self-reference, and a
reference to a table that does not exist, is never a step. Every path of minimal length, at most
`MAX_JOIN_HOPS` steps, is found, in the order of its references' ids, and at most
`MAX_JOIN_PATHS` are returned, with a flag when more exist.

References come only from `_Context.around`, so a path never runs through a table or a
reference the reader may not see (a reference needs both its table and its target readable, R3),
and the budget counts the walk as it counts lineage. A hidden path is not cut after it is found:
it is never found, so neither the paths, their count, `more_paths` nor the "no path" answer
differs from a graph in which it does not exist.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lakana_core.catalog.graph_load import ReferenceRecord
from lakana_core.catalog.graph_model import (
    MAX_JOIN_HOPS,
    MAX_JOIN_PATHS,
    EdgeKind,
    column_node,
    table_node,
)

if TYPE_CHECKING:
    from lakana_core.catalog.graph_service import _Context


@dataclass(frozen=True)
class JoinStep:
    """One step of a join path.

    Parameters
    ----------
    reference
        The reference it follows (``reference:<uuid>``).
    from_table
        The table it leaves (``table:<uuid>``).
    from_name
        Its dotted name.
    to_table
        The table it reaches.
    to_name
        Its dotted name.
    from_columns
        The columns of `from_table` to join on.
    to_columns
        The columns of `to_table` they equal, in the same order.
    direction
        ``many_to_one`` along the reference, ``one_to_many`` against it.
    fans_out
        Whether a row of `from_table` may meet many rows of `to_table` (``one_to_many``).
    from_model_kind
        `from_table`'s declared model kind.
    from_grain
        `from_table`'s grain, in the reader's language.
    to_model_kind
        `to_table`'s declared model kind.
    to_grain
        `to_table`'s grain, in the reader's language.
    """

    reference: str
    from_table: str
    from_name: str
    to_table: str
    to_name: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    direction: str
    fans_out: bool
    from_model_kind: str | None
    from_grain: str | None
    to_model_kind: str | None
    to_grain: str | None


@dataclass(frozen=True)
class JoinPath:
    """A join path.

    Parameters
    ----------
    steps
        Its steps, from the start table to the goal.
    """

    steps: tuple[JoinStep, ...]


@dataclass(frozen=True)
class JoinPaths:
    """The answer of `GraphService.join_path`.

    Parameters
    ----------
    paths
        The paths of minimal length, at most `MAX_JOIN_PATHS`, in reference-id order.
    reason
        Why there is none (`NO_PATH`); `None` when there are paths.
    more_paths
        Whether more paths of that length exist than were returned.
    """

    paths: tuple[JoinPath, ...]
    reason: str | None
    more_paths: bool


@dataclass(frozen=True)
class Hop:
    """One reference followed from a table.

    Parameters
    ----------
    reference
        The reference.
    to_table
        The table reached.
    forward
        Whether it is followed from its table to its target.
    """

    reference: ReferenceRecord
    to_table: uuid.UUID
    forward: bool


def _record(ctx: _Context, node_id: str) -> ReferenceRecord:
    """Return the record of a reference node.

    Parameters
    ----------
    ctx
        The request's context.
    node_id
        A node a `references` edge of a table or column leads to.

    Returns
    -------
    ReferenceRecord
        Its record.
    """
    record = ctx.graph.records[node_id]
    assert isinstance(record, ReferenceRecord)  # nosec B101 -- a references edge's source
    return record


def _hops(ctx: _Context, table_id: uuid.UUID) -> list[Hop]:
    """List the visible references a step may follow from a table.

    Back along a reference, the table is its target (the `references` edge to the table). Along
    one, a column of the table declares it (the `references` edge to the column), and its target
    table exists.

    Parameters
    ----------
    ctx
        The request's context.
    table_id
        The table; visible to the reader.

    Returns
    -------
    list of Hop
        The hops, in reference-id order.
    """
    table = table_node(table_id)
    hops: list[Hop] = []
    for _, node in ctx.around(table, (EdgeKind.REFERENCES,)):
        record = _record(ctx, node)
        if record.source_table_id != table_id:
            hops.append(Hop(record, record.source_table_id, False))
    for _, column in ctx.around(table, (EdgeKind.HAS_COLUMN,)):
        for _, node in ctx.around(column, (EdgeKind.REFERENCES,)):
            record = _record(ctx, node)
            if column != column_node(record.source_table_id, record.source_column):
                continue  # the column is this reference's target, not its source
            target = ctx.graph.table_ids.get(record.target_name)
            if target is not None and target != table_id:
                hops.append(Hop(record, target, True))
    return sorted(hops, key=lambda h: h.reference.id)


def shortest_paths(
    ctx: _Context, start: uuid.UUID, goal: uuid.UUID
) -> tuple[list[list[Hop]], bool]:
    """Find every shortest path of at most `MAX_JOIN_HOPS` steps between two tables.

    A breadth-first search, level by level, stops at the level that reaches the goal. The tables
    from which the goal is reached at that distance are marked going back, and the paths are
    walked forward through them only, each table's hops in reference-id order, so the paths
    come out in the order of their sequences of reference ids.

    Parameters
    ----------
    ctx
        The request's context.
    start
        The start table; visible to the reader.
    goal
        The goal table; visible, and not `start`.

    Returns
    -------
    tuple
        At most `MAX_JOIN_PATHS` paths, each a list of hops, and whether more exist; no paths
        when the goal is not reached within `MAX_JOIN_HOPS` steps.
    """
    distance = {start: 0}
    levels = [[start]]
    hops: dict[uuid.UUID, list[Hop]] = {}
    while levels[-1] and len(levels) <= MAX_JOIN_HOPS and goal not in distance:
        reached: set[uuid.UUID] = set()
        for table in levels[-1]:
            hops[table] = _hops(ctx, table)
            for hop in hops[table]:
                if hop.to_table not in distance:
                    distance[hop.to_table] = len(levels)
                    reached.add(hop.to_table)
        levels.append(sorted(reached))
    if goal not in distance:
        return [], False

    def onward(table: uuid.UUID) -> list[Hop]:
        """List a table's hops one level further from the start.

        Parameters
        ----------
        table
            A table of a level that was expanded.

        Returns
        -------
        list of Hop
            Its hops to a table one step further, in reference-id order.
        """
        return [h for h in hops.get(table, ()) if distance.get(h.to_table) == distance[table] + 1]

    useful = {goal}
    for level in range(distance[goal] - 1, -1, -1):
        useful |= {t for t in levels[level] if any(h.to_table in useful for h in onward(t))}
    paths: list[list[Hop]] = []
    trail: list[Hop] = []

    def walk(table: uuid.UUID) -> None:
        """Extend the trail along every useful hop, stopping one path past the limit.

        Parameters
        ----------
        table
            The table the trail has reached.
        """
        if len(paths) > MAX_JOIN_PATHS:
            return
        if table == goal:
            paths.append(list(trail))
            return
        for hop in onward(table):
            if hop.to_table in useful:
                trail.append(hop)
                walk(hop.to_table)
                trail.pop()

    walk(start)
    return paths[:MAX_JOIN_PATHS], len(paths) > MAX_JOIN_PATHS
