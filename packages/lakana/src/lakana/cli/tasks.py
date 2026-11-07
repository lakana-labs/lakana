"""`lakana task history`: what the attempts of one task used, run after run."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana._models import TaskHistoryEntry
from lakana.cli import _output
from lakana.cli._state import CliError, resolve_environment, state_of

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_REPO = Annotated[
    str | None,
    typer.Option(
        "--repo",
        help="Repository name; needed only when several repositories ran a job of this name.",
    ),
]
_JOB = Annotated[str, typer.Argument(help="The job's name, as in lakana.yaml.")]
_TASK = Annotated[str, typer.Argument(help="The task's table name, such as silver.orders.")]
_LAST = Annotated[int, typer.Option("--last", help="How many of the newest attempts (1 to 200).")]
_CURSOR = Annotated[
    str | None,
    typer.Option("--cursor", help="Continue after a page: the cursor the previous page printed."),
]
_COLUMNS = ["run", "attempt", "status", "duration", "resources"]

task_app = typer.Typer(
    help="Tasks: the history of one task of a job, with what each attempt used.",
    no_args_is_help=True,
)


def _run(entry: TaskHistoryEntry) -> str:
    """Name the run of an attempt, as `runs show` takes it.

    Parameters
    ----------
    entry
        The attempt.

    Returns
    -------
    str
        The run id.
    """
    return entry.run_id


def _status(entry: TaskHistoryEntry) -> str:
    """Write an attempt's status with the reason when it failed.

    Parameters
    ----------
    entry
        The attempt.

    Returns
    -------
    str
        For example ``succeeded`` or ``failed (memory)``.
    """
    return f"{entry.status} ({entry.failure_code})" if entry.failure_code else entry.status


def _duration(entry: TaskHistoryEntry) -> str:
    """Write how long an attempt took.

    Parameters
    ----------
    entry
        The attempt.

    Returns
    -------
    str
        ``12.0s``, or empty while it runs.
    """
    return "" if entry.duration_seconds is None else f"{entry.duration_seconds:.1f}s"


def _resources(entry: TaskHistoryEntry) -> str:
    """Write what an attempt used.

    Parameters
    ----------
    entry
        The attempt.

    Returns
    -------
    str
        Peak memory, processor time and bytes read, or a dash when none was measured.
    """
    return _output.resources_text(entry.peak_memory_bytes, entry.cpu_seconds, entry.bytes_read)


def register(app: typer.Typer) -> None:
    """Add `task`.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(task_app, name="task")


@task_app.command("history")
def history(
    ctx: typer.Context,
    job: _JOB,
    task: _TASK,
    env: _ENV = None,
    repo: _REPO = None,
    last: _LAST = 20,
    cursor: _CURSOR = None,
) -> None:
    """List the newest attempts of TASK of JOB across runs: status, duration, memory, CPU, bytes.

    Use it to see which task needs the most memory, or whether a task's memory climbs from run
    to run. A dash means the figure was not measured.

    Parameters
    ----------
    ctx
        The Typer context.
    job
        The job.
    task
        The task.
    env
        Environment.
    repo
        Repository name; needed only when several repositories ran a job of this name.
    last
        How many of the newest attempts to show.
    cursor
        The cursor of the previous page, to continue after it.

    Raises
    ------
    CliError
        If `last` is outside 1 to 200.
    """
    if not 1 <= last <= 200:
        raise CliError("--last must be between 1 and 200.")
    state = state_of(ctx)
    page = state.client().runs.task_history(
        resolve_environment(state, env), job, task, repository=repo, limit=last, cursor=cursor
    )
    if state.output == "json":
        _output.emit(state, {"attempts": page.items, "next_cursor": page.next_cursor})
        return
    if not page.items:
        typer.echo(
            "No attempts yet: the task has not run. Start its job with "
            f"`lakana job run {_output.inline(job)}`; the attempts appear here."
        )
        return
    _output.emit(
        state,
        page.items,
        columns=_COLUMNS,
        derived={
            "run": _run,
            "status": _status,
            "duration": _duration,
            "resources": _resources,
        },
        whole=["run", "status", "resources"],
    )
    if page.next_cursor is not None:
        more = (
            f"continue with --cursor {page.next_cursor}"
            if last >= 200
            else "show more with a larger --last (at most 200)"
        )
        typer.echo(f"Older attempts exist; {more}.")
