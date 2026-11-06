"""`--override-managed --reason` for the commands that change access (#46)."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli._state import CliError

OVERRIDE_MANAGED = Annotated[
    bool,
    typer.Option(
        "--override-managed",
        help=(
            "Change access in a scope an access file manages anyway, in an emergency (needs "
            "ADMIN on the instance; audited; the next apply reverts it)."
        ),
    ),
]
OVERRIDE_REASON = Annotated[
    str | None,
    typer.Option("--reason", help="Why, 10-500 characters on one line (with --override-managed)."),
]


def override_reason(flag: bool, reason: str | None) -> str | None:
    """Validate the flag pair and warn before an override.

    Parameters
    ----------
    flag
        `--override-managed`.
    reason
        `--reason`.

    Returns
    -------
    str or None
        The reason to send, or `None` without `--override-managed`.

    Raises
    ------
    CliError
        If only one of the two flags is given (exit 2).
    """
    if not flag:
        if reason is not None:
            raise CliError("--reason goes with --override-managed")
        return None
    if reason is None:
        raise CliError('--override-managed needs --reason "<why, 10-500 characters>"')
    typer.echo(
        "Overriding a scope an access file manages; the next apply reverts this unless the file "
        "lists it.",
        err=True,
    )
    return reason
