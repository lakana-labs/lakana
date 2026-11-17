"""`lakana configuration`: the server's settings, read-only."""

from __future__ import annotations

import json

import typer

from lakana.cli import _output
from lakana.cli._state import state_of


def show_configuration(ctx: typer.Context) -> None:
    """Show the running server's configuration (admin only).

    One row per setting with its environment variable, its value and whether the server's
    environment set it (`set`) or it is the default. A secret shows only `(secret, set)` or
    `(secret, not set)`, never its value. Needs the ADMIN role.

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    settings = state.client().configuration.list()
    if state.output == "json":
        _output.print_json(settings)
    else:
        _output.emit_text_table(
            ["name", "value", "source"],
            [
                [
                    item.name,
                    ("(secret, set)" if item.has_value else "(secret, not set)")
                    if item.secret
                    else item.value
                    if isinstance(item.value, str)
                    else json.dumps(item.value),
                    item.source,
                ]
                for item in settings
            ],
        )
    _output.next_step(
        state,
        "Values change in the server's .env file (or its environment) and take effect after a "
        "restart.",
    )


def register(app: typer.Typer) -> None:
    """Add `lakana configuration` to the app.

    Parameters
    ----------
    app
        The root app.
    """
    app.command("configuration")(show_configuration)
