"""`lakana passkey *`: list and remove passkeys (adding one happens in the web interface)."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import confirm, state_of

passkey_app = typer.Typer(
    help="List and remove passkeys; add one in the web interface.", no_args_is_help=True
)

_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]


@passkey_app.command("list")
def list_passkeys(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument(help="The person (default: you).")] = None,
) -> None:
    """List the passkeys of NAME, or your own.

    Another person's passkeys need the ADMIN role. A passkey that is not usable here was
    registered for an earlier public address (LAKANA_UI__PUBLIC_URL).

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
    _output.emit(state, client.passkeys.list(who), droppable=("backup_eligible",))


@passkey_app.command("remove")
def remove(
    ctx: typer.Context,
    name: str,
    passkey_id: Annotated[str, typer.Argument(metavar="ID")],
    yes: _YES = False,
) -> None:
    """Remove passkey ID of NAME.

    Your own, or anyone's with the ADMIN role; removing another person's passkey also ends
    their browser sessions. Your own last working passkey cannot be removed: add another one
    first.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The person's principal name.
    passkey_id
        The passkey's id, from 'lakana passkey list'.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Remove passkey {passkey_id} of {name!r}?", yes)
    state.client().passkeys.remove(name, passkey_id)
    _output.done(state, f"Removed passkey {_output.inline(passkey_id)} of {_output.inline(name)}")


def register(app: typer.Typer) -> None:
    """Add the `passkey` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(passkey_app, name="passkey")
