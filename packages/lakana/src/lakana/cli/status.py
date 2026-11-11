"""`lakana status`: whether the instance is healthy, degraded or down, in one line."""

from __future__ import annotations

import typer

from lakana._models import InstanceStatus
from lakana.cli import _output
from lakana.cli._state import CliState, state_of
from lakana.cli.artifacts import human_size
from lakana.cli.runs import emit_workers

_STATES = {"healthy": "Healthy", "degraded": "Degraded", "down": "Down"}

DOWN_EXIT_CODE = 12
"""How `lakana status` exits when the instance is down, so a script can tell; 0 otherwise.

It is none of the error codes (1 to 11): the command itself worked."""

_SENTENCES = {
    "database": (
        "The catalog database does not answer, so nothing works until it is back. Check the "
        "database service and its logs."
    ),
    "storage": (
        "The storage of an environment does not answer, so its tables cannot be read or "
        "written. Check the storage service and the environment's credentials."
    ),
    "no_worker": (
        "No worker is running, so queries and runs wait. Start a worker; on a local instance, "
        "start it again with 'lakana server start --local'."
    ),
    "memory_high": (
        "A worker uses 90 % or more of its memory limit, so a large query or task may be ended. "
        "Raise LAKANA_WORKER__MEMORY_LIMIT or start another worker."
    ),
}
"""The sentence of each cause but `worker_lost`, which counts the workers."""

_WITHOUT_CAUSE = {
    "healthy": "Lakana is working normally.",
    "degraded": "Lakana works, but slower than usual; ask an administrator if it lasts.",
    "down": "Lakana cannot do work right now; ask an administrator.",
}
"""The sentence when no cause is named: none applies, the reader may not see it, or a newer
server sent a cause this release does not know."""


def _sentence(status: InstanceStatus) -> str:
    """Say in one sentence why the instance is in its state.

    Parameters
    ----------
    status
        The status.

    Returns
    -------
    str
        The sentence of the first cause this release knows, else one by the state.
    """
    for cause in status.causes:
        if cause == "worker_lost":
            lost = status.workers_total - status.workers_alive
            verb = "is" if lost == 1 else "are"
            return (
                f"Queries and runs may be slower: {lost} of {status.workers_total} workers "
                f"{verb} down."
            )
        if cause in _SENTENCES:
            return _SENTENCES[cause]
    return _WITHOUT_CAUSE.get(status.state, _WITHOUT_CAUSE["degraded"])


def status(ctx: typer.Context) -> None:
    """Show whether the instance is healthy, degraded or down, and why.

    Prints one line: Healthy, Degraded or Down, and a sentence that says why. An administrator
    (who may manage environments on the instance) also sees the busy task slots, the workers'
    memory against their limits, and the workers as lakana worker list shows them. Degraded
    means work gets done, slower or at risk; Down means nothing gets done.

    Exit code: 0 for Healthy and for Degraded, 12 for Down (also with --output json), so a
    script can branch on it. While the catalog database is down the command cannot check who
    you are and fails with the server's error (exit code 11); the address /readyz still
    answers then.

    Parameters
    ----------
    ctx
        The Typer context.

    Raises
    ------
    typer.Exit
        With `DOWN_EXIT_CODE` when the instance is down.
    """
    state = state_of(ctx)
    found = state.client().status()
    if state.output == "json":
        _output.emit(state, found)
    else:
        _print(state, found)
    if found.state == "down":
        raise typer.Exit(DOWN_EXIT_CODE)


def _print(state: CliState, found: InstanceStatus) -> None:
    """Print the status for people.

    Parameters
    ----------
    state
        The CLI state.
    found
        The status.
    """
    typer.echo(f"{_STATES.get(found.state, found.state.title())} — {_sentence(found)}")
    detail = found.detail
    if detail is None or not detail.workers:
        return
    if found.workers_alive:
        typer.echo(f"Compute: {detail.slots_busy} of {detail.slots_total} slots busy")
        used = human_size(detail.memory_used_bytes)
        limit = detail.memory_limit_bytes
        typer.echo(
            f"Memory: {used} in use; limit unknown: a worker has not reported its memory yet "
            "(it just started, or runs an earlier release) or runs without a limit"
            if limit is None
            else f"Memory: {used} of {human_size(limit)}"
        )
    emit_workers(state, detail.workers)


def register(app: typer.Typer) -> None:
    """Add `status`.

    Parameters
    ----------
    app
        The root app.
    """
    app.command("status")(status)
