"""`lakana raw ls`: the folders and files under an environment's `raw/`."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana._models import RawEntry
from lakana.cli import _output
from lakana.cli._paging import CURSOR, LIMIT, more_results
from lakana.cli._state import resolve_environment, state_of

raw_app = typer.Typer(
    help="Look under raw/, where the files of external tables lie.", no_args_is_help=True
)

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]


def _entry(entry: RawEntry) -> str:
    """Name an entry, a folder with a slash after it.

    Parameters
    ----------
    entry
        The entry.

    Returns
    -------
    str
        The name.
    """
    return f"{entry.name}/" if entry.kind == "folder" else entry.name


def _in_use_by(entry: RawEntry) -> str:
    """Say which external table holds an entry.

    Parameters
    ----------
    entry
        The entry.

    Returns
    -------
    str
        The table, a phrase for one you may not read, or nothing for a free entry.
    """
    if entry.claimed_by is None:
        return ""
    return entry.claimed_by.table or "(a table you may not read)"


@raw_app.command("ls")
def ls(
    ctx: typer.Context,
    path: Annotated[
        str, typer.Argument(help="The folder: raw, or a folder below it, such as raw/orders.")
    ] = "raw",
    env: _ENV = None,
    limit: LIMIT = None,
    cursor: CURSOR = None,
) -> None:
    """List folder PATH of raw/, one page at a time: its folders and files with their sizes.

    A folder or file inside an external table's folder names the table (when you may read it):
    it cannot be used for another table. Needs CREATE_TABLE on a namespace of the environment.
    Register a table over a folder with `lakana table create-external`.

    Parameters
    ----------
    ctx
        The Typer context.
    path
        The folder.
    env
        The environment; the profile's by default.
    limit
        `--limit`.
    cursor
        `--cursor`.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    page = state.client().raw.list(environment, path, limit=limit, cursor=cursor)
    if not page.items and state.output != "json":
        _output.emit(state, f"Nothing in {_output.inline(page.path)}.")
    else:
        _output.emit(
            state,
            page.items,
            columns=["entry", "kind", "size", "in_use_by"],
            derived={"entry": _entry, "in_use_by": _in_use_by},
        )
    more_results(
        state,
        ["lakana", "raw", "ls", path],
        {"--env": env, "--limit": limit},
        page.next_cursor,
    )


def register(app: typer.Typer) -> None:
    """Add the `raw` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(raw_app, name="raw")
