"""`lakana graph *`: ask the metadata graph what depends on what."""

from __future__ import annotations

import enum
import sys
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping
from typing import Annotated

import typer

from lakana._models import Graph, GraphNode
from lakana.cli import _output
from lakana.cli._state import CliError, CliState, resolve_environment, state_of

graph_app = typer.Typer(
    help="Ask the metadata graph: lineage, neighbours, impact and join paths.",
    no_args_is_help=True,
)

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_LANG = Annotated[
    str | None, typer.Option("--lang", help="Show labels in this language, such as de.")
]
_COLUMN = Annotated[str | None, typer.Option("--column", help="A column of the table NODE names.")]
_NODE = Annotated[
    str, typer.Argument(help="A table's dotted name, such as gold.dim_store, or a node id.")
]
_COLUMNS = ["distance", "kind", "name", "label", "repository"]
_LINEAGE_COLUMNS = ["distance", "side", "kind", "name", "label", "repository"]
_SIDE_NOTE = (
    "side: up feeds the start, down is fed by it, both is on a loop through it, join is "
    "joined to a column start by a reference."
)

_IMPACT_COLUMNS = ["distance", "kind", "name", "reason", "may_use", "repository"]
_MAY_USE_NOTE = (
    "may_use: yes means it may use the column or key without naming it (a check, a row filter "
    "or something downstream); SQL is not parsed, so look before you change it."
)
_COLUMN_AND_KEY = "--column and --key ask different questions; give one of them."
_MORE_PATHS = "More paths of this length exist; ask a narrower pair of tables to see them."


class _Direction(enum.StrEnum):
    """Which way `graph lineage` walks."""

    UP = "up"
    DOWN = "down"
    BOTH = "both"


def _node(node: str, column: str | None) -> str:
    """Return NODE, or the column of it that --column names.

    Parameters
    ----------
    node
        What the user typed as NODE.
    column
        The `--column` option.

    Returns
    -------
    str
        `node`, or ``column:<table>/<column>`` when a column is given.
    """
    if column is None:
        return node
    return f"column:{node.removeprefix('table:')}/{column}"


def _reached(starts: Iterable[str], ahead: Mapping[str, list[str]]) -> set[str]:
    """Find every node reachable from the starts, the starts left out.

    Parameters
    ----------
    starts
        Where to begin.
    ahead
        Node → the nodes one step on.

    Returns
    -------
    set of str
        The nodes reached.
    """
    queue = deque(starts)
    seen = set(queue)
    while queue:
        for nxt in ahead.get(queue.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _sides(graph: Graph) -> dict[str, str]:
    """Say on which side of the start each node of a lineage answer is.

    Data flows from a table into the asset that reads it (`reads`) and from an asset into the
    table it builds (`builds`): what reaches the start along that flow is upstream, what the
    start reaches is downstream.

    Parameters
    ----------
    graph
        A lineage answer, every page read.

    Returns
    -------
    dict
        Node id → ``start``, ``up``, ``down``, ``both`` or ``join`` (reached through a
        reference from a column start).
    """
    downstream: defaultdict[str, list[str]] = defaultdict(list)
    upstream: defaultdict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind == "builds":
            downstream[edge.source].append(edge.target)
            upstream[edge.target].append(edge.source)
        elif edge.kind == "reads":
            downstream[edge.target].append(edge.source)
            upstream[edge.source].append(edge.target)
    starts = {n.id for n in graph.nodes if n.distance == 0}
    up, down = _reached(starts, upstream), _reached(starts, downstream)
    sides: dict[str, str] = {}
    for node in graph.nodes:
        if node.id in starts:
            sides[node.id] = "start"
        elif node.id in up and node.id in down:
            sides[node.id] = "both"
        elif node.id in up or node.id in down:
            sides[node.id] = "up" if node.id in up else "down"
        else:
            sides[node.id] = "join"
    return sides


def _show(
    state: CliState,
    graph: Graph,
    columns: list[str],
    derived: Mapping[str, Callable[[GraphNode], str]] | None = None,
    footnotes: Mapping[str, str] | None = None,
) -> None:
    """Print a graph: its nodes as a table, or nodes and edges as JSON.

    Parameters
    ----------
    state
        The CLI state.
    graph
        The answer.
    columns
        The node fields the table shows.
    derived
        Columns that are not node fields, and how to read each from a node.
    footnotes
        A line under the table per column, explaining it.
    """
    if state.output == "json":
        _output.print_json(graph)
        return
    _output.emit(
        state,
        list(graph.nodes),
        columns=columns,
        droppable=["label", "repository"],
        derived=derived,
        footnotes=footnotes,
    )


@graph_app.command("lineage")
def lineage(
    ctx: typer.Context,
    node: _NODE,
    column: _COLUMN = None,
    direction: Annotated[
        _Direction,
        typer.Option("--direction", help="up (what feeds it), down (what it feeds), or both."),
    ] = _Direction.BOTH,
    depth: Annotated[
        int | None,
        typer.Option("--depth", help="Assets to cross, 1 to 10 (default: the server's, 3)."),
    ] = None,
    lang: _LANG = None,
    env: _ENV = None,
) -> None:
    """Show the tables and assets upstream and downstream of NODE, as the projects declare them.

    Parameters
    ----------
    ctx
        The Typer context.
    node
        The start.
    column
        A column of the table `node` names.
    direction
        Which way to walk.
    depth
        Assets to cross.
    lang
        The language of labels.
    env
        The environment.
    """
    state = state_of(ctx)
    graph = state.client().graph.lineage(
        resolve_environment(state, env),
        _node(node, column),
        direction=direction.value,
        depth=depth,
        lang=lang,
    )
    sides = _sides(graph)
    _show(
        state,
        graph,
        _LINEAGE_COLUMNS,
        derived={"side": lambda n: sides[n.id]},
        footnotes={"side": _SIDE_NOTE},
    )


@graph_app.command("neighbours")
def neighbours(
    ctx: typer.Context,
    node: _NODE,
    column: _COLUMN = None,
    kind: Annotated[
        list[str] | None,
        typer.Option("--kind", help="Only edges of this kind; repeat for several."),
    ] = None,
    lang: _LANG = None,
    env: _ENV = None,
) -> None:
    """Show what is one edge away from NODE: columns, assets, references, checks, terms, jobs.

    Parameters
    ----------
    ctx
        The Typer context.
    node
        The node.
    column
        A column of the table `node` names.
    kind
        Only edges of these kinds.
    lang
        The language of labels and names.
    env
        The environment.
    """
    state = state_of(ctx)
    graph = state.client().graph.neighbours(
        resolve_environment(state, env), _node(node, column), kinds=kind or (), lang=lang
    )
    if len(graph.nodes) <= 1 and state.output != "json":
        name = _output.inline(graph.nodes[0].name if graph.nodes else node)
        if kind:
            options = " ".join(f"--kind {_output.inline(k)}" for k in kind)
            _output.emit(
                state,
                f"No neighbours of {name} that you may see with {options}; "
                "drop --kind to see every kind.",
            )
        else:
            _output.emit(state, f"No neighbours of {name} that you may see.")
        return
    _show(state, graph, _COLUMNS)


@graph_app.command("impact")
def impact(
    ctx: typer.Context,
    node: _NODE,
    column: _COLUMN = None,
    key: Annotated[bool, typer.Option("--key", help="Ask about the table's declared key.")] = False,
    lang: _LANG = None,
    env: _ENV = None,
) -> None:
    """Show what the projects declare that depends on NODE, with the reason for each.

    Parameters
    ----------
    ctx
        The Typer context.
    node
        A table.
    column
        A column of the table `node` names.
    key
        Ask about the table's declared key.
    lang
        The language of labels and names.
    env
        The environment.

    Raises
    ------
    CliError
        For --column and --key together (exit 2).
    """
    if column is not None and key:
        raise CliError(_COLUMN_AND_KEY)
    state = state_of(ctx)
    graph = state.client().graph.impact(
        resolve_environment(state, env), _node(node, column), key=key, lang=lang
    )
    if not [n for n in graph.nodes if n.reason] and state.output != "json":
        name = _output.inline(graph.nodes[0].name if graph.nodes else node)
        _output.emit(
            state, f"Nothing the projects declare depends on {name}, as far as you may see."
        )
        return
    _show(
        state,
        graph,
        _IMPACT_COLUMNS,
        derived={"may_use": lambda n: "yes" if n.may_use else ""},
        footnotes={"may_use": _MAY_USE_NOTE},
    )


@graph_app.command("join-path")
def join_path(
    ctx: typer.Context,
    from_table: Annotated[str, typer.Argument(metavar="FROM", help="The table to start from.")],
    to_table: Annotated[str, typer.Argument(metavar="TO", help="The table to reach.")],
    lang: _LANG = None,
    env: _ENV = None,
) -> None:
    """Show how FROM joins TO over declared references: each step's columns, and where rows fan out.

    Parameters
    ----------
    ctx
        The Typer context.
    from_table
        The table to start from.
    to_table
        The table to reach.
    lang
        The language of grains.
    env
        The environment.
    """
    state = state_of(ctx)
    answer = state.client().graph.join_path(
        resolve_environment(state, env), from_table, to_table, lang=lang
    )
    if state.output == "json":
        _output.print_json(answer)
        return
    inline = _output.inline
    if not answer.paths:
        _output.emit(state, inline(answer.reason or "", 500))
        return
    lines: list[str] = []
    for number, path in enumerate(answer.paths, start=1):
        lines.append(f"Path {number}:")
        for s in path.steps:
            pairs = zip(s.from_columns, s.to_columns, strict=True)
            on = " and ".join(f"{inline(a)} = {inline(b)}" for a, b in pairs)
            way = "one to many, fans out" if s.fans_out else "many to one"
            kinds = f"{inline(s.from_model_kind or '?')} → {inline(s.to_model_kind or '?')}"
            lines.append(f"  {inline(s.from_name)} → {inline(s.to_name)} on {on} ({way}: {kinds})")
    if answer.more_paths:
        lines.append(_MORE_PATHS)
    sys.stdout.write("\n".join(lines) + "\n")


def register(app: typer.Typer) -> None:
    """Add the `graph` group to the root app.

    Parameters
    ----------
    app
        The root Typer app.
    """
    app.add_typer(graph_app, name="graph")
