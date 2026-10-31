"""`lakana me set`: your own preferences on the instance."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import CliError, state_of

me_app = typer.Typer(help="Your own preferences on the instance.", no_args_is_help=True)


@me_app.command("set")
def set_preferences(
    ctx: typer.Context,
    language: Annotated[
        str | None,
        typer.Option(
            "--language", help="Show catalog texts in this language, such as de or fr-CH."
        ),
    ] = None,
    reset: Annotated[bool, typer.Option("--reset", help="Remove your language.")] = False,
) -> None:
    """Set the language you see catalog texts in, or remove it.

    Labels and descriptions of tables and columns are then shown in it where the project or an
    edit has them; `--lang` on a command still asks for another. A task or CI token cannot set
    one.

    Parameters
    ----------
    ctx
        The Typer context.
    language
        The language tag.
    reset
        Remove the preference.

    Raises
    ------
    CliError
        If neither or both of `--language` and `--reset` are given.
    """
    if (language is None) == (not reset):
        raise CliError("Give exactly one of --language TAG and --reset.")
    state = state_of(ctx)
    who = state.client().set_language(None if reset else language)
    if who.language is None:
        _output.done(
            state,
            "Your language is reset; you see the instance's main language unless a command asks "
            "for another",
            who,
        )
    else:
        _output.done(state, f"Your language is now {_output.inline(who.language)}", who)


def register(app: typer.Typer) -> None:
    """Add `lakana me` to the app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(me_app, name="me")
