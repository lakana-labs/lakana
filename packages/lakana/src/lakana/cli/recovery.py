"""`lakana recovery *`: let a person who lost their passkeys back in by a single-use link."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import state_of
from lakana.cli.sign_in import print_link, send_advice

recovery_app = typer.Typer(
    help="Recovery links for people who lost their passkeys (ADMIN).", no_args_is_help=True
)


@recovery_app.command("create")
def create(ctx: typer.Context, name: str) -> None:
    """Issue a recovery link for NAME, who lost their passkeys; prints the link to send them.

    The link works once, for 1 hour. Using it removes NAME's passkeys, ends their sessions and
    tells them and every administrator. Recovering an administrator is for the server operator
    only: run this where the server runs, with the profile that holds the bootstrap token (the
    one 'lakana bundle init' or 'lakana server start --local' saved). Only the link goes to
    stdout.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The person's principal name.
    """
    state = state_of(ctx)
    link = state.client().recovery_links.create(name)
    print_link(state, link.url, link)
    send_advice(state, name, "1 hour")


@recovery_app.command("list")
def list_links(ctx: typer.Context) -> None:
    """List the open recovery links (never the links themselves).

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().recovery_links.list())


@recovery_app.command("withdraw")
def withdraw(ctx: typer.Context, link_id: Annotated[str, typer.Argument(metavar="ID")]) -> None:
    """Withdraw the open recovery link ID; it stops working at once.

    Parameters
    ----------
    ctx
        The Typer context.
    link_id
        The link's id, from 'lakana recovery list'.
    """
    state = state_of(ctx)
    state.client().recovery_links.withdraw(link_id)
    _output.done(state, f"Withdrew recovery link {_output.inline(link_id)}")


def register(app: typer.Typer) -> None:
    """Add the `recovery` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(recovery_app, name="recovery")
