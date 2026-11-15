"""`lakana ns *`: manage namespaces within an environment."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._break_glass import BREAK_GLASS, REASON, break_glass_reason
from lakana.cli._paging import CURSOR, LIMIT, more_results
from lakana.cli._state import confirm, parse_key_values, resolve_environment, state_of

ns_app = typer.Typer(help="Manage namespaces.", no_args_is_help=True)

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]


@ns_app.command("create")
def create(
    ctx: typer.Context,
    path: str,
    env: _ENV = None,
    prop: Annotated[
        list[str] | None, typer.Option("--property", help="key=value (repeatable).")
    ] = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Create namespace PATH (dotted, e.g. bronze.eu).

    Parameters
    ----------
    ctx
        The Typer context.
    path
        The dotted namespace path.
    env
        The environment.
    prop
        Namespace properties as key=value.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    namespace = state.client().namespaces.create(
        environment,
        path,
        properties=parse_key_values(prop, "--property"),
        break_glass_reason=glass,
    )
    _output.done(
        state, f"Created namespace {_output.inline(environment)}/{_output.inline(path)}", namespace
    )


@ns_app.command("list")
def list_namespaces(
    ctx: typer.Context,
    env: _ENV = None,
    parent: Annotated[
        str | None,
        typer.Option("--parent", help="Only the namespaces below this one, such as gold."),
    ] = None,
    depth: Annotated[
        int | None,
        typer.Option(
            "--depth",
            min=1,
            max=1,
            help="1 lists only the direct children of --parent (the top level without it).",
        ),
    ] = None,
    limit: LIMIT = None,
    cursor: CURSOR = None,
) -> None:
    """List namespaces, one page at a time, in path order.

    The last line names the command for the next page when there is one.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        The environment.
    parent
        `--parent`.
    depth
        `--depth`.
    limit
        `--limit`.
    cursor
        `--cursor`.
    """
    state = state_of(ctx)
    page = state.client().namespaces.list_page(
        resolve_environment(state, env), parent=parent, depth=depth, limit=limit, cursor=cursor
    )
    _output.emit(state, page.items, droppable=["properties"])
    more_results(
        state,
        ["lakana", "ns", "list"],
        {"--env": env, "--parent": parent, "--depth": depth, "--limit": limit},
        page.next_cursor,
    )


@ns_app.command("get")
def get(ctx: typer.Context, path: str, env: _ENV = None) -> None:
    """Show namespace PATH.

    Parameters
    ----------
    ctx
        The Typer context.
    path
        The dotted namespace path.
    env
        The environment.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().namespaces.get(resolve_environment(state, env), path))


@ns_app.command("delete")
def delete(
    ctx: typer.Context,
    path: str,
    env: _ENV = None,
    yes: _YES = False,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Delete namespace PATH.

    Parameters
    ----------
    ctx
        The Typer context.
    path
        The dotted namespace path.
    env
        The environment.
    yes
        Skip the confirmation prompt.
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    confirm(state, f"Delete namespace {environment}/{path}?", yes)
    state.client().namespaces.delete(environment, path, break_glass_reason=glass)
    _output.done(state, f"Deleted namespace {_output.inline(environment)}/{_output.inline(path)}")


def register(app: typer.Typer) -> None:
    """Add the `ns` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(ns_app, name="ns")
