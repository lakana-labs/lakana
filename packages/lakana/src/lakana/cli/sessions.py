"""`lakana session *`: a person's browser sessions in the web interface."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import confirm, state_of

session_app = typer.Typer(help="List and end browser sessions.", no_args_is_help=True)

_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]


@session_app.command("list")
def list_sessions(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="The person (default: you).")] = None,
) -> None:
    """List the open browser sessions of NAME, or your own.

    Another person's sessions need the ADMIN role.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The person's principal name; yours when left out.
    """
    state = state_of(ctx)
    client = state.client()
    who = name if name is not None else client.whoami().name
    _output.emit(
        state,
        client.sessions.list(who),
        columns=("id", "via", "created_at", "last_seen_at", "absolute_expires_at", "browser"),
        droppable=("browser", "absolute_expires_at"),
        flexible=("browser",),
    )


@session_app.command("end")
def end(ctx: typer.Context, name: str, yes: _YES = False) -> None:
    """End every browser session of NAME; they have to sign in again.

    Your own (sign out everywhere), or anyone's with the ADMIN role. Each ended session is
    recorded in the audit log.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The person's principal name.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"End every browser session of {name!r}?", yes)
    state.client().sessions.end_all(name)
    _output.done(state, f"Ended every browser session of {_output.inline(name)}")


def register(app: typer.Typer) -> None:
    """Add the `session` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(session_app, name="session")
