"""`lakana graph *`: ask the metadata graph what depends on what."""

from __future__ import annotations

import enum
from typing import Annotated

import typer

from lakana._models import Graph
from lakana.cli import _output
from lakana.cli._state import CliState, resolve_environment, state_of

graph_app = typer.Typer(
    help="Ask the metadata graph: lineage and neighbours.",
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
_COLUMNS = ["distance", "kind", "name", "repository"]


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


def _show(state: CliState, graph: Graph, columns: list[str]) -> None:
    """Print a graph: its nodes as a table, or nodes and edges as JSON.

    Parameters
    ----------
    state
        The CLI state.
    graph
        The answer.
    columns
        The node fields the table shows.
    """
    if state.output == "json":
        _output.print_json(graph)
        return
    _output.emit(state, list(graph.nodes), columns=columns, droppable=["repository"])


@graph_app.command("lineage")
def lineage(
    ctx: typer.Context,
    node: _NODE,
    column: _COLUMN = None,
    direction: Annotated[
        _Direction,
        typer.Option("--direction", help="up (what feeds it), down (what it feeds), or both."),
    ] = _Direction.BOTH,
    depth: Annotated[int, typer.Option("--depth", help="Assets to cross, 1 to 10.")] = 3,
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
    _show(state, graph, _COLUMNS)


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


def register(app: typer.Typer) -> None:
    """Add the `graph` group to the root app.

    Parameters
    ----------
    app
        The root Typer app.
    """
    app.add_typer(graph_app, name="graph")
