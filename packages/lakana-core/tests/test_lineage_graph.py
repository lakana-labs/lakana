# packages/lakana-core/tests/test_lineage_graph.py
"""Reading persisted lineage back, pruned by grants."""

from __future__ import annotations

import dataclasses
import itertools
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from core_test_helpers import ensure_table
from sqlalchemy import event
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, Securable, SecurableKind, TaskScope
from lakana_core.catalog import lineage_graph
from lakana_core.catalog.lineage_graph import (
    MAX_LINEAGE_NODES,
    LineageDirection,
    LineageGraph,
    LineageService,
)
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import Connection, LineageEdge

_T0 = datetime(2026, 9, 1, tzinfo=UTC)


class _Readable:
    """Permits READ_TABLE on exactly the listed dotted tables and namespaces.

    Parameters
    ----------
    *readable
        Dotted table names the caller may read, by a table-level grant.
    namespaces
        Dotted namespaces the caller may read at namespace level (or above).
    """

    def __init__(self, *readable: str, namespaces: tuple[str, ...] = ()) -> None:
        self.readable = set(readable)
        self.namespaces = set(namespaces)

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer for READ_TABLE on a listed table.

        Parameters
        ----------
        principal
            Ignored.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            Whether the table is listed.
        """
        if action is not Action.READ_TABLE:
            return False
        if securable.kind is SecurableKind.NAMESPACE:
            return str(securable.namespace) in self.namespaces
        return (
            securable.kind is SecurableKind.TABLE
            and f"{securable.namespace}.{securable.table}" in self.readable
        )

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse whatever `permits` refuses.

        Parameters
        ----------
        principal
            Ignored.
        action
            The action.
        securable
            The object.

        Raises
        ------
        PermissionDenied
            If `permits` is false.
        """
        if not self.permits(principal, action, securable):
            raise PermissionDenied("may not READ_TABLE")


def _tables(session: Session, *names: str) -> dict[str, uuid.UUID]:
    """Create `gold.<name>` tables in `dev`.

    Parameters
    ----------
    session
        Database session.
    *names
        Table names.

    Returns
    -------
    dict
        Each name's table id, plus `"env"` for the environment's id.
    """
    ids: dict[str, uuid.UUID] = {}
    for name in names:
        environment_id, table_id = ensure_table(session, "dev", "gold", name)
        ids[name] = table_id
        ids["env"] = environment_id
    return ids


def _edge(
    session: Session,
    environment_id: uuid.UUID,
    source: tuple[uuid.UUID, str],
    target: tuple[uuid.UUID, str],
    *,
    at: datetime = _T0,
    target_snapshot_id: int | None = None,
) -> None:
    """Insert one edge.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    source
        `(id, dotted name)`.
    target
        `(id, dotted name)`.
    at
        Its `created_at`.
    target_snapshot_id
        Its target snapshot.
    """
    session.add(
        LineageEdge(
            id=uuid.uuid4(),
            environment_id=environment_id,
            source_table_id=source[0],
            source_name=source[1],
            source_snapshot_id=None,
            target_table_id=target[0],
            target_name=target[1],
            target_snapshot_id=target_snapshot_id,
            query_run_id=None,
            created_at=at,
        )
    )
    session.commit()


def _chain(session: Session, *names: str) -> dict[str, uuid.UUID]:
    """Create tables and a lineage chain `names[0] → names[1] → …`.

    Parameters
    ----------
    session
        Database session.
    *names
        The chain, upstream first.

    Returns
    -------
    dict
        As `_tables`.
    """
    ids = _tables(session, *names)
    for source, target in itertools.pairwise(names):
        _edge(session, ids["env"], (ids[source], f"gold.{source}"), (ids[target], f"gold.{target}"))
    return ids


def _names(graph: LineageGraph) -> set[str]:
    """Collect a graph's node names.

    Parameters
    ----------
    graph
        The graph.

    Returns
    -------
    set of str
        The names.
    """
    return {node.name for node in graph.nodes}


def _ref(name: str) -> TableRef:
    """Address `gold.<name>` in `dev`.

    Parameters
    ----------
    name
        The table.

    Returns
    -------
    TableRef
        The reference.
    """
    return TableRef.from_dotted("dev", f"gold.{name}")


_EVERYTHING = ("gold.a", "gold.b", "gold.c", "gold.d", "gold.e", "gold.x")


def test_upstream_defaults_to_three_hops(session: Session, principal: PrincipalRef) -> None:
    """Spec: the default is upstream, depth 3."""
    _chain(session, "a", "b", "c", "d", "e")

    graph = LineageService(session, _Readable(*_EVERYTHING)).lineage(principal, _ref("e"))

    assert graph.direction is LineageDirection.UPSTREAM
    assert _names(graph) == {"gold.e", "gold.d", "gold.c", "gold.b"}
    assert len(graph.edges) == 3
    assert graph.hidden_nodes == 0


def test_downstream_follows_edges_the_other_way(session: Session, principal: PrincipalRef) -> None:
    """`direction=downstream` walks from sources to targets."""
    _chain(session, "a", "b", "c", "d")

    graph = LineageService(session, _Readable(*_EVERYTHING)).lineage(
        principal, _ref("a"), direction=LineageDirection.DOWNSTREAM, depth=2
    )

    assert _names(graph) == {"gold.a", "gold.b", "gold.c"}


@pytest.mark.parametrize("depth", [0, 11])
def test_depth_is_bounded_to_ten(session: Session, principal: PrincipalRef, depth: int) -> None:
    """Spec: the maximum depth is 10."""
    _chain(session, "a", "b")

    with pytest.raises(ValidationError, match="between 1 and 10"):
        LineageService(session, _Readable(*_EVERYTHING)).lineage(principal, _ref("b"), depth=depth)


def test_unreadable_nodes_are_hidden_counted_and_never_walked_through(
    session: Session, principal: PrincipalRef
) -> None:
    """Security: lineage never leaks a name, and a hidden node is a dead end."""
    _chain(session, "a", "b", "c")

    graph = LineageService(session, _Readable("gold.a", "gold.c")).lineage(principal, _ref("c"))

    assert _names(graph) == {"gold.c"}
    assert graph.hidden_nodes == 1
    assert graph.edges == []


def test_the_root_itself_needs_read(session: Session, principal: PrincipalRef) -> None:
    """You can only ask for the lineage of a table you can read."""
    _chain(session, "a", "b")

    with pytest.raises(PermissionDenied):
        LineageService(session, _Readable("gold.a")).lineage(principal, _ref("b"))


def test_an_unknown_table_is_not_found(session: Session, principal: PrincipalRef) -> None:
    """A name with no table is a 404, not an empty graph."""
    _tables(session, "a")

    with pytest.raises(NotFoundError):
        LineageService(session, _Readable("gold.nope")).lineage(principal, _ref("nope"))


def test_a_dropped_node_keeps_its_recorded_name_and_is_marked(
    session: Session, principal: PrincipalRef
) -> None:
    """Spec: dropping a table keeps its edges; the API marks dropped nodes."""
    ids = _tables(session, "c")
    gone = uuid.uuid4()
    _edge(session, ids["env"], (gone, "gold.x"), (ids["c"], "gold.c"))

    graph = LineageService(session, _Readable("gold.c", namespaces=("gold",))).lineage(
        principal, _ref("c")
    )
    hidden = LineageService(session, _Readable("gold.c")).lineage(principal, _ref("c"))

    dropped = next(node for node in graph.nodes if node.id == gone)
    assert (dropped.name, dropped.kind, dropped.dropped) == ("gold.x", None, True)
    assert hidden.hidden_nodes == 1


def test_a_table_refreshed_many_times_shows_one_edge(
    session: Session, principal: PrincipalRef
) -> None:
    """One edge per (source, target), the most recent."""
    ids = _tables(session, "a", "b")
    for run in range(5):
        _edge(
            session,
            ids["env"],
            (ids["b"], "gold.b"),
            (ids["a"], "gold.a"),
            at=_T0 + timedelta(days=run),
            target_snapshot_id=run,
        )

    graph = LineageService(session, _Readable(*_EVERYTHING)).lineage(principal, _ref("a"))

    assert len(graph.edges) == 1
    assert graph.edges[0].target_snapshot_id == 4


def test_a_self_edge_appears_once_and_the_walk_ends(
    session: Session, principal: PrincipalRef
) -> None:
    """A refresh that reads its own target records a self-edge."""
    ids = _tables(session, "a")
    _edge(session, ids["env"], (ids["a"], "gold.a"), (ids["a"], "gold.a"))

    graph = LineageService(session, _Readable(*_EVERYTHING)).lineage(principal, _ref("a"), depth=10)

    assert _names(graph) == {"gold.a"}
    assert [(e.source_id, e.target_id) for e in graph.edges] == [(ids["a"], ids["a"])]


def test_edges_from_another_environment_are_ignored(
    session: Session, principal: PrincipalRef
) -> None:
    """Lineage is per environment."""
    ids = _tables(session, "a")
    prod_env, prod_table = ensure_table(session, "prod", "gold", "p")
    _edge(session, prod_env, (prod_table, "gold.p"), (ids["a"], "gold.a"))

    graph = LineageService(session, _Readable(*_EVERYTHING, "gold.p")).lineage(principal, _ref("a"))

    assert _names(graph) == {"gold.a"}


def test_a_hidden_node_reached_by_two_edges_is_counted_once(
    session: Session, principal: PrincipalRef
) -> None:
    """Two edges from one hidden source count one hidden node; equal timestamps keep one edge."""
    ids = _tables(session, "h", "c")
    for _ in range(2):
        _edge(session, ids["env"], (ids["h"], "gold.h"), (ids["c"], "gold.c"))
    readable = _Readable("gold.c", "gold.h")
    visible = LineageService(session, readable).lineage(principal, _ref("c"))
    hidden = LineageService(session, _Readable("gold.c")).lineage(principal, _ref("c"))

    assert len(visible.edges) == 1
    assert hidden.hidden_nodes == 1
    assert hidden.edges == []


def test_a_dropped_node_with_an_unparseable_recorded_name_is_hidden(
    session: Session, principal: PrincipalRef
) -> None:
    """A recorded name that is not `namespace.table` is treated as unreadable."""
    ids = _tables(session, "c")
    _edge(session, ids["env"], (uuid.uuid4(), "nodots"), (ids["c"], "gold.c"))

    graph = LineageService(session, _Readable(*_EVERYTHING)).lineage(principal, _ref("c"))

    assert _names(graph) == {"gold.c"}
    assert graph.hidden_nodes == 1


def test_a_dropped_node_with_no_recorded_name_is_unreadable(session: Session) -> None:
    """A dropped id that no edge names gets no securable, so it is never shown."""
    ids = _tables(session, "c")
    service = LineageService(session, _Readable(*_EVERYTHING))

    node, securable = service._node("dev", ids["env"], uuid.uuid4())

    assert node.dropped is True
    assert node.name == ""
    assert securable is None


@pytest.mark.slow
def test_a_wide_graph_is_capped_and_flagged_truncated(
    session: Session, principal: PrincipalRef
) -> None:
    """One request never returns more than the node cap; it says so."""
    wide = [f"w{i}" for i in range(MAX_LINEAGE_NODES + 100)]
    ids = _tables(session, "root", *wide)
    session.add_all(
        LineageEdge(
            id=uuid.uuid4(),
            environment_id=ids["env"],
            source_table_id=ids[name],
            source_name=f"gold.{name}",
            source_snapshot_id=None,
            target_table_id=ids["root"],
            target_name="gold.root",
            target_snapshot_id=None,
            query_run_id=None,
            created_at=_T0,
        )
        for name in wide
    )
    session.commit()
    everything = _Readable("gold.root", *(f"gold.{name}" for name in wide))

    graph = LineageService(session, everything).lineage(principal, _ref("root"))

    assert graph.truncated is True
    assert len(graph.nodes) == MAX_LINEAGE_NODES
    assert {edge.source_id for edge in graph.edges} <= {n.id for n in graph.nodes}


def test_a_small_graph_is_not_truncated(session: Session, principal: PrincipalRef) -> None:
    """Under the cap, `truncated` is false."""
    _chain(session, "a", "b")

    graph = LineageService(session, _Readable(*_EVERYTHING)).lineage(principal, _ref("b"))

    assert graph.truncated is False


def test_a_hidden_node_beyond_the_cap_does_not_set_truncated(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`truncated` must not reveal that a node the caller can't read lies past the cap.

    With the cap full, a hidden node is counted as hidden like any other; only
    a visible node left out sets `truncated`.
    """
    monkeypatch.setattr(lineage_graph, "MAX_LINEAGE_NODES", 2)
    ids = _tables(session, "root", "a", "h")
    _edge(session, ids["env"], (ids["a"], "gold.a"), (ids["root"], "gold.root"), at=_T0)
    _edge(
        session,
        ids["env"],
        (ids["h"], "gold.h"),
        (ids["root"], "gold.root"),
        at=_T0 + timedelta(seconds=1),
    )

    graph = LineageService(session, _Readable("gold.root", "gold.a")).lineage(
        principal, _ref("root")
    )

    assert graph.truncated is False
    assert graph.hidden_nodes == 1
    assert _names(graph) == {"gold.root", "gold.a"}


def test_a_visible_node_beyond_the_cap_sets_truncated(
    session: Session, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A readable node past the cap is what `truncated` reports."""
    monkeypatch.setattr(lineage_graph, "MAX_LINEAGE_NODES", 2)
    ids = _tables(session, "root", "a", "h", "b")
    for offset, name in enumerate(("a", "h", "b")):
        _edge(
            session,
            ids["env"],
            (ids[name], f"gold.{name}"),
            (ids["root"], "gold.root"),
            at=_T0 + timedelta(seconds=offset),
        )

    graph = LineageService(session, _Readable("gold.root", "gold.a", "gold.b")).lineage(
        principal, _ref("root")
    )

    assert graph.truncated is True
    assert graph.hidden_nodes == 1
    assert _names(graph) == {"gold.root", "gold.a"}


def test_edges_are_fetched_once_per_level_not_per_node(
    session: Session, principal: PrincipalRef
) -> None:
    """A fan-in of many nodes costs one edge query per level."""
    ids = _chain(session, "a", "b", "c", "d", "e")
    extra = _tables(session, "x")
    _edge(session, ids["env"], (extra["x"], "gold.x"), (ids["e"], "gold.e"))
    _edge(session, ids["env"], (extra["x"], "gold.x"), (ids["d"], "gold.d"))
    statements: list[str] = []

    def count(conn: object, cursor: object, statement: str, *rest: object) -> None:
        if "lineage_edges" in statement:
            statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        LineageService(session, _Readable(*_EVERYTHING)).lineage(principal, _ref("e"), depth=10)
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert len(statements) == 5


def test_the_edge_query_returns_one_row_per_pair(session: Session) -> None:
    """Deduping happens in SQL: many refreshes never load their history."""
    ids = _tables(session, "a", "b")
    for run in range(5):
        _edge(
            session,
            ids["env"],
            (ids["b"], "gold.b"),
            (ids["a"], "gold.a"),
            at=_T0 + timedelta(days=run),
            target_snapshot_id=run,
        )

    rows = LineageService(session, _Readable())._edges(
        ids["env"], [ids["a"]], LineageDirection.UPSTREAM
    )

    assert [row.target_snapshot_id for row in rows] == [4]


def test_downstream_hides_unreadable_nodes_and_does_not_walk_through_them(
    session: Session, principal: PrincipalRef
) -> None:
    """Hiding works the same downstream; a readable node beyond a hidden one stays out."""
    _chain(session, "a", "b", "c")

    graph = LineageService(session, _Readable("gold.a", "gold.c")).lineage(
        principal, _ref("a"), direction=LineageDirection.DOWNSTREAM
    )

    assert _names(graph) == {"gold.a"}
    assert graph.hidden_nodes == 1
    assert graph.edges == []


def test_a_dropped_downstream_node_is_checked_at_its_namespace(
    session: Session, principal: PrincipalRef
) -> None:
    """Downstream, a dropped node's recorded target namespace decides, not the root's."""
    ids = _tables(session, "a")
    gone = uuid.uuid4()
    _edge(session, ids["env"], (ids["a"], "gold.a"), (gone, "gold.x"))

    shown = LineageService(session, _Readable("gold.a", namespaces=("gold",))).lineage(
        principal, _ref("a"), direction=LineageDirection.DOWNSTREAM
    )
    hidden = LineageService(session, _Readable("gold.a")).lineage(
        principal, _ref("a"), direction=LineageDirection.DOWNSTREAM
    )

    assert _names(shown) == {"gold.a", "gold.x"}
    assert _names(hidden) == {"gold.a"}
    assert hidden.hidden_nodes == 1


@pytest.mark.parametrize("depth", [1, 10])
def test_a_readable_table_beyond_a_hidden_one_never_appears(
    session: Session, principal: PrincipalRef, depth: int
) -> None:
    """At the depth bounds, a hidden node is still a dead end."""
    _chain(session, "a", "b", "c")

    graph = LineageService(session, _Readable("gold.a", "gold.c")).lineage(
        principal, _ref("c"), depth=depth
    )

    assert _names(graph) == {"gold.c"}
    assert graph.hidden_nodes == 1


def test_a_hidden_node_met_again_at_a_later_level_is_not_re_checked(
    session: Session, principal: PrincipalRef
) -> None:
    """A hidden source feeding two visible nodes counts once and stays out."""
    ids = _chain(session, "b", "c")
    hidden_ids = _tables(session, "h")
    _edge(session, ids["env"], (hidden_ids["h"], "gold.h"), (ids["c"], "gold.c"))
    _edge(session, ids["env"], (hidden_ids["h"], "gold.h"), (ids["b"], "gold.b"))

    graph = LineageService(session, _Readable("gold.b", "gold.c")).lineage(principal, _ref("c"))

    assert _names(graph) == {"gold.b", "gold.c"}
    assert graph.hidden_nodes == 1
    assert len(graph.edges) == 1


def test_a_dropped_node_is_not_shown_through_a_table_grant_on_a_new_table_of_its_name(
    session: Session, principal: PrincipalRef
) -> None:
    """A table recreated under the dropped name is a different table (different id)."""
    ids = _tables(session, "c")
    old_x = uuid.uuid4()
    _edge(session, ids["env"], (old_x, "gold.x"), (ids["c"], "gold.c"), target_snapshot_id=7)
    new_x = _tables(session, "x")["x"]

    graph = LineageService(session, _Readable("gold.c", "gold.x")).lineage(principal, _ref("c"))

    assert new_x != old_x
    assert _names(graph) == {"gold.c"}
    assert [node.id for node in graph.nodes] == [ids["c"]]
    assert graph.edges == []
    assert graph.hidden_nodes == 1


def test_a_dropped_downstream_node_is_not_shown_through_a_table_grant_on_a_new_table(
    session: Session, principal: PrincipalRef
) -> None:
    """Downstream: the same rule in the other direction."""
    ids = _tables(session, "a")
    _edge(session, ids["env"], (ids["a"], "gold.a"), (uuid.uuid4(), "gold.x"))
    _tables(session, "x")

    graph = LineageService(session, _Readable("gold.a", "gold.x")).lineage(
        principal, _ref("a"), direction=LineageDirection.DOWNSTREAM
    )

    assert _names(graph) == {"gold.a"}
    assert graph.edges == []
    assert graph.hidden_nodes == 1


def test_a_dropped_node_is_shown_to_a_namespace_reader_with_its_edges(
    session: Session, principal: PrincipalRef
) -> None:
    """The original owner's namespace-level read still shows the history."""
    ids = _tables(session, "c")
    old_x = uuid.uuid4()
    _edge(session, ids["env"], (old_x, "gold.x"), (ids["c"], "gold.c"), target_snapshot_id=7)

    graph = LineageService(session, _Readable("gold.c", namespaces=("gold",))).lineage(
        principal, _ref("c")
    )

    assert {node.id for node in graph.nodes} == {ids["c"], old_x}
    assert [(e.source_id, e.target_id) for e in graph.edges] == [(old_x, ids["c"])]
    assert graph.hidden_nodes == 0


def test_a_dropped_node_asks_for_the_namespace_never_a_table(
    session: Session, principal: PrincipalRef
) -> None:
    """The securable checked for a dropped node names no table at all."""
    ids = _tables(session, "c")
    _edge(session, ids["env"], (uuid.uuid4(), "gold.x"), (ids["c"], "gold.c"))
    asked: list[Securable] = []

    class _Spy(_Readable):
        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Record the securable and allow everything.

            Parameters
            ----------
            principal
                Ignored.
            action
                Ignored.
            securable
                The object, recorded.

            Returns
            -------
            bool
                Always true.
            """
            asked.append(securable)
            return True

    LineageService(session, _Spy()).lineage(principal, _ref("c"))

    # The root is asked first, as a table; the dropped node after it.
    assert [(s.kind, s.table) for s in asked[1:]] == [(SecurableKind.NAMESPACE, None)]


_READERS_OF_A_MOVED_TABLE = {
    "old namespace only": (("bronze",), False),
    "new namespace only": (("silver",), True),
    "both namespaces": (("bronze", "silver"), True),
}
"""Namespace grants of a reader, and whether the moved, dropped table is theirs to see."""


def _moved_then_dropped(session: Session) -> tuple[dict[str, uuid.UUID], uuid.UUID]:
    """Record lineage of `x`, renamed from `bronze` to `silver` and then dropped.

    `x` fed `bronze.a` as `bronze.x`, then, renamed, fed `silver.b` as `silver.x`; `a` and `b`
    both feed `gold.c`. `x` has no table row any more.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple
        The ids of `a`, `b`, `c` and `"env"`, and `x`'s id.
    """
    environment_id, a = ensure_table(session, "dev", "bronze", "a")
    _, b = ensure_table(session, "dev", "silver", "b")
    _, c = ensure_table(session, "dev", "gold", "c")
    x = uuid.uuid4()
    _edge(session, environment_id, (x, "bronze.x"), (a, "bronze.a"), at=_T0)
    _edge(session, environment_id, (x, "silver.x"), (b, "silver.b"), at=_T0 + timedelta(hours=1))
    _edge(session, environment_id, (a, "bronze.a"), (c, "gold.c"), at=_T0 + timedelta(hours=2))
    _edge(session, environment_id, (b, "silver.b"), (c, "gold.c"), at=_T0 + timedelta(hours=3))
    return {"a": a, "b": b, "c": c, "env": environment_id}, x


@pytest.mark.parametrize("reader", sorted(_READERS_OF_A_MOVED_TABLE))
@pytest.mark.parametrize("root", ["bronze.a", "silver.b", "gold.c"])
def test_a_table_moved_across_namespaces_then_dropped_gets_one_verdict_from_every_root(
    session: Session, principal: PrincipalRef, reader: str, root: str
) -> None:
    """The newest recorded name decides, wherever the walk starts and whichever edge is first.

    Parameters
    ----------
    session
        Database session.
    principal
        The caller.
    reader
        Which namespaces the caller may read (`_READERS_OF_A_MOVED_TABLE`).
    root
        The table the walk starts from.
    """
    _, x = _moved_then_dropped(session)
    namespaces, visible = _READERS_OF_A_MOVED_TABLE[reader]
    authz = _Readable("bronze.a", "silver.b", "gold.c", namespaces=namespaces)

    graph = LineageService(session, authz).lineage(
        principal, TableRef.from_dotted("dev", root), depth=2
    )

    shown = [node for node in graph.nodes if node.id == x]
    assert bool(shown) is visible
    if visible:
        assert [(node.name, node.dropped) for node in shown] == [("silver.x", True)]
        assert x in {edge.source_id for edge in graph.edges}
    else:
        assert all(x not in (edge.source_id, edge.target_id) for edge in graph.edges)
        assert graph.hidden_nodes == 1


def test_a_table_moved_then_dropped_is_shown_under_its_newest_name_downstream(
    session: Session, principal: PrincipalRef
) -> None:
    """Downstream, a dropped target is judged by its newest recorded name, not the edge's."""
    environment_id, a = ensure_table(session, "dev", "gold", "a")
    _, b = ensure_table(session, "dev", "gold", "b")
    y = uuid.uuid4()
    _edge(session, environment_id, (a, "gold.a"), (y, "bronze.y"), at=_T0)
    _edge(session, environment_id, (b, "gold.b"), (y, "silver.y"), at=_T0 + timedelta(hours=1))

    old_reader = LineageService(session, _Readable("gold.a", namespaces=("bronze",)))
    new_reader = LineageService(session, _Readable("gold.a", namespaces=("silver",)))

    hidden = old_reader.lineage(principal, _ref("a"), direction=LineageDirection.DOWNSTREAM)
    shown = new_reader.lineage(principal, _ref("a"), direction=LineageDirection.DOWNSTREAM)

    assert [node.id for node in hidden.nodes] == [a]
    assert hidden.edges == []
    assert [(node.name, node.dropped) for node in shown.nodes if node.id == y] == [
        ("silver.y", True)
    ]


def test_a_dropped_node_with_a_legacy_dotted_name_is_judged_at_the_namespace_before_the_last_dot(
    session: Session, principal: PrincipalRef
) -> None:
    """`bronze.x.y` names the table `y` in the namespace `bronze.x`."""
    ids = _tables(session, "c")
    _edge(session, ids["env"], (uuid.uuid4(), "bronze.x.y"), (ids["c"], "gold.c"))
    asked: list[Securable] = []

    class _Spy(_Readable):
        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Record the securable, then answer as `_Readable` does.

            Parameters
            ----------
            principal
                The caller.
            action
                The action.
            securable
                The object, recorded.

            Returns
            -------
            bool
                As `_Readable.permits`.
            """
            asked.append(securable)
            return super().permits(principal, action, securable)

    graph = LineageService(session, _Spy("gold.c", namespaces=("bronze.x",))).lineage(
        principal, _ref("c")
    )

    assert [(s.kind, str(s.namespace), s.table) for s in asked[1:]] == [
        (SecurableKind.NAMESPACE, "bronze.x", None)
    ]
    assert "bronze.x.y" in _names(graph)


class _ReadsEnvironment(_Readable):
    """`_Readable`, plus READ_ENVIRONMENT on `dev` when `environment` is set.

    Parameters
    ----------
    *readable
        As `_Readable`.
    environment
        Whether READ_ENVIRONMENT on `dev` is permitted.
    """

    def __init__(self, *readable: str, environment: bool = True) -> None:
        super().__init__(*readable)
        self.environment = environment
        self.asked: list[tuple[Action, Securable]] = []

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer READ_ENVIRONMENT from `environment`, the rest as `_Readable`.

        Parameters
        ----------
        principal
            Ignored.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            The verdict.
        """
        self.asked.append((action, securable))
        if action is Action.READ_ENVIRONMENT:
            return (
                self.environment
                and securable.kind is SecurableKind.ENVIRONMENT
                and securable.environment == "dev"
            )
        return super().permits(principal, action, securable)


def _connection_edge(
    session: Session, ids: dict[str, uuid.UUID], connection_id: uuid.UUID, target: str
) -> None:
    """Record that connection `crm-db` fed `gold.<target>`.

    Parameters
    ----------
    session
        Database session.
    ids
        As `_tables`.
    connection_id
        The connection's id.
    target
        The target table.
    """
    session.add(
        LineageEdge(
            id=uuid.uuid4(),
            environment_id=ids["env"],
            source_kind="connection",
            source_table_id=connection_id,
            source_name="crm-db",
            source_snapshot_id=None,
            target_table_id=ids[target],
            target_name=f"gold.{target}",
            target_snapshot_id=7,
            query_run_id=None,
            created_at=_T0,
        )
    )
    session.commit()


def _crm(session: Session, principal: PrincipalRef, environment_id: uuid.UUID) -> uuid.UUID:
    """Create the PostgreSQL connection `crm-db`.

    Parameters
    ----------
    session
        Database session.
    principal
        Its creator.
    environment_id
        Its environment.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    row = Connection(
        id=uuid.uuid4(),
        environment_id=environment_id,
        name="crm-db",
        kind=ConnectionKind.POSTGRESQL,
        address={"host": "db.example.com", "port": 5432, "database": "crm"},
        settings={"user": "reader", "tls": "verify-full"},
        created_by=principal.id,
    )
    session.add(row)
    session.commit()
    return row.id


def test_upstream_lists_a_connection_node(session: Session, principal: PrincipalRef) -> None:
    """A connection the asset declared is a source, kind `CONNECTION`, with its own kind."""
    ids = _chain(session, "a", "customers")
    crm = _crm(session, principal, ids["env"])
    _connection_edge(session, ids, crm, "customers")

    graph = LineageService(session, _ReadsEnvironment(*_EVERYTHING, "gold.customers")).lineage(
        principal, _ref("customers")
    )

    connection = next(node for node in graph.nodes if node.id == crm)
    assert (connection.name, connection.kind, connection.connection_kind, connection.dropped) == (
        "crm-db",
        "CONNECTION",
        "postgresql",
        False,
    )
    assert {node.name for node in graph.nodes} == {"gold.customers", "gold.a", "crm-db"}
    edge = next(e for e in graph.edges if e.source_id == crm)
    assert (edge.target_id, edge.source_snapshot_id, edge.target_snapshot_id) == (
        ids["customers"],
        None,
        7,
    )
    assert graph.hidden_nodes == 0
    tables = [node for node in graph.nodes if node.kind != "CONNECTION"]
    assert all(node.connection_kind is None for node in tables)


def test_walking_up_from_a_connection_stops(session: Session, principal: PrincipalRef) -> None:
    """A connection has no sources: even an edge that names it as a target is never followed."""
    ids = _tables(session, "customers", "x")
    crm = _crm(session, principal, ids["env"])
    _connection_edge(session, ids, crm, "customers")
    _edge(session, ids["env"], (ids["x"], "gold.x"), (crm, "crm-db"))

    graph = LineageService(session, _ReadsEnvironment(*_EVERYTHING, "gold.customers")).lineage(
        principal, _ref("customers"), depth=10
    )

    assert {node.name for node in graph.nodes} == {"gold.customers", "crm-db"}
    assert len(graph.edges) == 1


def test_a_connection_is_hidden_without_read_environment(
    session: Session, principal: PrincipalRef
) -> None:
    """Its name is shown only to a reader of the environment's connections."""
    ids = _tables(session, "customers")
    crm = _crm(session, principal, ids["env"])
    _connection_edge(session, ids, crm, "customers")
    authz = _ReadsEnvironment("gold.customers", environment=False)

    graph = LineageService(session, authz).lineage(principal, _ref("customers"))

    assert [node.name for node in graph.nodes] == ["gold.customers"]
    assert graph.edges == []
    assert graph.hidden_nodes == 1
    assert (Action.READ_ENVIRONMENT, Securable.environment_scope("dev")) in authz.asked


def test_a_connection_is_hidden_from_a_task_token(
    session: Session, principal: PrincipalRef
) -> None:
    """A task may not list connections, so lineage does not show it one either."""
    ids = _tables(session, "customers")
    crm = _crm(session, principal, ids["env"])
    _connection_edge(session, ids, crm, "customers")
    task = dataclasses.replace(
        principal,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
    )

    graph = LineageService(session, _ReadsEnvironment("gold.customers")).lineage(
        task, _ref("customers")
    )

    assert [node.name for node in graph.nodes] == ["gold.customers"]
    assert graph.hidden_nodes == 1


def test_a_deleted_connection_keeps_its_recorded_name(
    session: Session, principal: PrincipalRef
) -> None:
    """A connection deleted since the write stays in the history, marked, without a kind."""
    ids = _tables(session, "customers")
    gone = uuid.uuid4()
    _connection_edge(session, ids, gone, "customers")

    graph = LineageService(session, _ReadsEnvironment("gold.customers")).lineage(
        principal, _ref("customers")
    )

    node = next(node for node in graph.nodes if node.id == gone)
    assert (node.name, node.kind, node.connection_kind, node.dropped) == (
        "crm-db",
        "CONNECTION",
        None,
        True,
    )


def test_a_connection_of_another_environment_is_never_described(
    session: Session, principal: PrincipalRef
) -> None:
    """An edge naming another environment's connection id reads as deleted, never as that row."""
    ids = _tables(session, "customers")
    other_env, _ = ensure_table(session, "prod", "gold", "customers")
    other = _crm(session, principal, other_env)
    _connection_edge(session, ids, other, "customers")

    graph = LineageService(session, _ReadsEnvironment("gold.customers")).lineage(
        principal, _ref("customers")
    )

    node = next(node for node in graph.nodes if node.id == other)
    assert (node.connection_kind, node.dropped) == (None, True)
