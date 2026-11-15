"""`--break-glass --reason` for the write commands."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli._state import CliError

BREAK_GLASS = Annotated[
    bool,
    typer.Option(
        "--break-glass",
        help="Write to a protected environment outside a run (needs MANAGE_ENVIRONMENT; audited).",
    ),
]
REASON = Annotated[
    str | None,
    typer.Option("--reason", help="Why, 10-500 characters on one line (with --break-glass)."),
]


def break_glass_reason(break_glass: bool, reason: str | None, environment: str) -> str | None:
    """Validate the flag pair and warn before a break-glass write.

    Parameters
    ----------
    break_glass
        `--break-glass`.
    reason
        `--reason`.
    environment
        The target environment, for the warning.

    Returns
    -------
    str or None
        The reason to send, or `None` without `--break-glass`.

    Raises
    ------
    CliError
        If only one of the two flags is given (exit 2).
    """
    if not break_glass:
        if reason is not None:
            raise CliError("--reason is only used with --break-glass")
        return None
    if reason is None:
        raise CliError('--break-glass needs --reason "<why, 10-500 characters>"')
    warn_break_glass(environment)
    return reason


def warn_break_glass(environment: str) -> None:
    """Print the warning before a break-glass write.

    Parameters
    ----------
    environment
        The target environment, for the warning.
    """
    typer.echo(
        f"warning: break-glass write to {environment}: it is audited, and the next run of the "
        "deployed code may overwrite it; real fixes go through a deploy.",
        err=True,
    )
