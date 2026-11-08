"""`lakana invite *`: invite new people to the web interface by a single-use link."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import state_of
from lakana.cli.sign_in import print_link, send_advice

invite_app = typer.Typer(
    help="Invite people by a link that creates their account (ADMIN).", no_args_is_help=True
)


@invite_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    display_name: Annotated[
        str | None,
        typer.Option("--display-name", help="The person's name as the interface shows it."),
    ] = None,
) -> None:
    """Invite a new person as NAME; prints the link to send them.

    The link works once, for 7 days, and creates the account NAME with no rights. Confirm with
    the person that they signed up before you grant roles. Only the link goes to stdout.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The new principal name; refused if any principal or open invite has it in any case.
    display_name
        `--display-name`: the person's name as the interface shows it.
    """
    state = state_of(ctx)
    invite = state.client().invites.create(name, display_name=display_name)
    print_link(state, invite.url, invite)
    send_advice(state, name, "7 days")


@invite_app.command("list")
def list_invites(ctx: typer.Context) -> None:
    """List the open invites (never their links).

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().invites.list())


@invite_app.command("withdraw")
def withdraw(ctx: typer.Context, invite_id: Annotated[str, typer.Argument(metavar="ID")]) -> None:
    """Withdraw the open invite ID; its link stops working and its name is free again.

    Parameters
    ----------
    ctx
        The Typer context.
    invite_id
        The invite's id, from 'lakana invite list'.
    """
    state = state_of(ctx)
    state.client().invites.withdraw(invite_id)
    _output.done(state, f"Withdrew invite {_output.inline(invite_id)}")


def register(app: typer.Typer) -> None:
    """Add the `invite` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(invite_app, name="invite")
