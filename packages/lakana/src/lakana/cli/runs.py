"""`lakana run`, `lakana runs …`, `lakana scheduler status` and `lakana worker list`."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Annotated

import typer

from lakana._models import CheckResult, Notification, Run, RunTask, WorkerInfo
from lakana.cli import _output
from lakana.cli._paging import more_results, parse_since
from lakana.cli._state import CliState, resolve_environment, state_of
from lakana.cli.checks import check_shown, withheld_line
from lakana.cli.deploys import resolve_repository
from lakana.cli.environments import cache_size_cell
from lakana.errors import RunFailed

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_REPO = Annotated[
    str | None, typer.Argument(help="Repository name (default: lakana.yaml's repo:).")
]
_RUN_COLUMNS = [
    "id",
    "repository",
    "job",
    "trigger",
    "kind",
    "status",
    "run_as",
    "created_at",
    "finished_at",
]
_RUN_DROPPABLE = ["finished_at", "run_as", "kind", "trigger", "repository", "created_at", "job"]
"""What `runs list` leaves out first on a narrow terminal: the id and the status always stay."""
_SHOW_COLUMNS = [
    "id",
    "repository",
    "commit_sha",
    "kind",
    "job",
    "trigger",
    "scheduled_for",
    "missed",
    "status",
    "cancel_requested",
    "triggered_by",
    "run_as",
    "resumed_from_run_id",
    "error_message",
    "created_at",
    "started_at",
    "finished_at",
]
_FOLLOW_SECONDS = 2.0
MAX_SUMMARY_ERRORS = 50
"""Most task errors a run's human summary prints; JSON output keeps them all."""
_FINISHED = frozenset({"succeeded", "failed", "cancelled"})

runs_app = typer.Typer(help="Runs of deployed project versions.", no_args_is_help=True)
scheduler_app = typer.Typer(help="The scheduler role.", no_args_is_help=True)
worker_app = typer.Typer(help="The workers of this instance.", no_args_is_help=True)
_WORKER_COLUMNS = [
    "id",
    "host",
    "version",
    "started_at",
    "last_seen",
    "status",
    "takes",
    "doing",
    "caches",
]
_ID_SHOWN = 8
"""How many characters of a run id the `doing` column shows."""
_DOING_NOTE = "doing shows 8 characters of each run id; --output json has them whole."
_CACHES_NOTE = "caches: the disk its caches take (+ a floor, - not measured); see lakana env usage."
_WORKER_DROPPABLE = ["started_at", "version", "host", "caches"]
"""What `worker list` leaves out first on a narrow terminal: the id, state and work stay."""


@dataclass(frozen=True)
class TaskRow:
    """One task in a run summary.

    Parameters
    ----------
    asset
        Dotted table name.
    status
        Task status.
    waiting_for
        Why the task has not started (the server's sentence), or ``None``.
    attempt
        ``n/max``, empty before the first attempt.
    duration
        Seconds from first claim to finish, or empty.
    rows
        Rows written.
    error
        Last failure.
    """

    asset: str
    status: str
    waiting_for: str | None
    attempt: str
    duration: str
    rows: int | None
    error: str | None


def _duration(task: RunTask) -> str:
    """Format a task's duration.

    Parameters
    ----------
    task
        The task.

    Returns
    -------
    str
        ``1.5s``, or empty while unfinished.
    """
    if task.started_at is None or task.finished_at is None:
        return ""
    return f"{(task.finished_at - task.started_at).total_seconds():.1f}s"


_SETTING_BACK = "setting the table back"
"""What the status column says of a running task whose claim only sets its table back."""


def _status(task: RunTask) -> str:
    """Name a task's status for display: a set-back claim is not running the task's code.

    Parameters
    ----------
    task
        The task.

    Returns
    -------
    str
        ``setting the table back`` for a running task that is setting its table back, else the
        status the server sent.
    """
    return _SETTING_BACK if task.setting_back and task.status == "running" else task.status


def _attempts(task: RunTask) -> str:
    """Format how many attempts a task has used, or nothing before the first.

    Parameters
    ----------
    task
        The task.

    Returns
    -------
    str
        ``n/max``, or empty for a task that has not started (``0/3`` would read as a
        failure).
    """
    return f"{task.attempt}/{task.max_attempts}" if task.attempt else ""


_PROGRESS_REASON_LIMIT = 400
"""The most characters of a reason's sentence a progress line keeps, before a ``…``."""

_OWN_LINE_CODES = frozenset(
    {"no_worker", "no_worker_of_kind", "workers_busy", "retry", "setting_back"}
)
"""The reasons the user can act on or wait out knowingly: a change to one prints a line.

A task is never held back by `user_limit` (the limit counts ad-hoc queries only), so that code
is not here.
"""


def _progress_line(task: RunTask) -> str:
    """Build the line printed when a task changes state.

    Parameters
    ----------
    task
        The task.

    Returns
    -------
    str
        The asset and status, then the attempt: none before the first, and
        ``retrying after attempt n/max`` for a task waiting to try again; then the reason the
        task waits (escaped, cut), if the reason is one the user can act on or wait out
        knowingly (`_OWN_LINE_CODES`; `starting` and `upstream` are the normal course of a run
        and would add a sentence to every line). A ``retry`` reason says the same as
        "retrying" and more, so it replaces it.
    """
    line = f"{task.asset}  {_status(task)}"
    reason = task.waiting_for
    if task.attempt:
        if task.status in {"pending", "ready"}:
            if reason is None or reason.code != "retry":
                line += f"  retrying after attempt {_attempts(task)}"
        else:
            line += f"  attempt {_attempts(task)}"
    if reason is not None and reason.code in _OWN_LINE_CODES and reason.message.strip():
        line += f"  {_output.inline(reason.message, _PROGRESS_REASON_LIMIT)}"
    return line


def _rows(run: Run) -> tuple[list[TaskRow], int]:
    """Build the summary rows of a run, with at most `MAX_SUMMARY_ERRORS` error texts.

    Parameters
    ----------
    run
        The run.

    Returns
    -------
    tuple of (list of TaskRow, int)
        One row per task, and how many further tasks have an error that
        was left out of the table.
    """
    rows: list[TaskRow] = []
    errors = 0
    for t in run.tasks:
        error = t.error_message
        if error is not None and t.status == "succeeded":
            # A succeeded task's message is a note (its write was found committed after its
            # worker was lost), not a failure: shown as one, and not counted against the cap.
            error = f"note: {error}"
        elif error is not None:
            errors += 1
            if errors > MAX_SUMMARY_ERRORS:
                error = None
        rows.append(
            TaskRow(
                asset=t.asset,
                status=_status(t),
                waiting_for=None
                if t.waiting_for is None
                else _output.inline(t.waiting_for.message, _PROGRESS_REASON_LIMIT),
                attempt=_attempts(t),
                duration=_duration(t),
                rows=t.rows_written,
                error=error,
            )
        )
    return rows, max(errors - MAX_SUMMARY_ERRORS, 0)


def _emit_tasks(state: CliState, run: Run) -> None:
    """Print a run's task table and, past the cap, how many errors it left out.

    The `waiting_for` column is there only when a task has a reason, so that the summary of a
    finished run keeps its width for the other columns.

    Parameters
    ----------
    state
        The CLI state (table mode).
    run
        The run.
    """
    rows, hidden = _rows(run)
    shown_reason = any(row.waiting_for for row in rows)
    columns = [f.name for f in fields(TaskRow) if shown_reason or f.name != "waiting_for"]
    _output.emit(
        state, rows, columns=columns, droppable=["duration", "rows"], flexible=["waiting_for"]
    )
    if hidden:
        typer.echo(
            f"+{hidden} more failed tasks; "
            f"see `lakana runs show {run.id} --env {run.environment} --output json`."
        )


def _next_step(task: RunTask) -> str:
    """Word the code and the next step of a failed task as one line.

    Parameters
    ----------
    task
        A task with a failure.

    Returns
    -------
    str
        ``failed: timeout. Next: <the next step>``, the server's words escaped and on one line.
    """
    failure = task.failure
    assert failure is not None  # nosec B101 -- callers test it
    return (
        f"failed: {_output.inline(failure.code, 40)}. "
        f"Next: {_output.inline(failure.next_step, _PROGRESS_REASON_LIMIT)}"
    )


def _emit_failures(run: Run) -> None:
    """Print, for each failed task, its code, what happened and what to do; nothing without any.

    A task that never started because an upstream task failed, and one that was cancelled, are
    not listed: the task that failed says it all. At most `MAX_SUMMARY_ERRORS` tasks are listed
    (the task table says how many more failed; ``--output json`` has every failure).

    Parameters
    ----------
    run
        The run.
    """
    failed = [t for t in run.tasks if t.status == "failed" and t.failure is not None]
    for task in failed[:MAX_SUMMARY_ERRORS]:
        assert task.failure is not None  # nosec B101 -- filtered above
        typer.echo("")
        typer.echo(f"{task.asset} failed: {_output.inline(task.failure.code, 40)}")
        typer.echo(f"  {_output.inline(task.failure.cause, _PROGRESS_REASON_LIMIT)}")
        typer.echo(f"  Next: {_output.inline(task.failure.next_step, _PROGRESS_REASON_LIMIT)}")


_NAME_PAD = 2
"""The name column of a check group is its longest name plus this many spaces."""
_STATE_WIDTH = 7
"""The least width of the state column; a group with a longer state (a kept one) widens it."""
# Equal to core's ROLLBACK_PHRASES; test_the_rollback_phrases_of_the_cli_and_of_core_are_the_same.
_ROLLBACK_PHRASES = {
    "rolled_back": "rolled back",
    "unpublished": "not published",
    "nothing_written": "nothing was written",
    "not_a_table": "a view, nothing to set back",
    "changed_since": "left as it is, changed since",
    "failed": "rollback failed",
}
"""What each outcome of a rollback reads as, the words of the server's own texts."""
_SOURCE_NOTE = "not built by this project"
_KEPT_NOTE = "kept after a failed check"
_CHECK_TASK_NOTE = "check task"


def _blocking_failure(result: CheckResult) -> bool:
    """Say whether a check failed or errored and so failed its task.

    Parameters
    ----------
    result
        The check's result.

    Returns
    -------
    bool
        `True` for a `block` or `keep` check that failed or errored.
    """
    return result.on_failure in {"block", "keep"} and result.outcome in {"failed", "errored"}


def _check_state(result: CheckResult) -> str:
    """Name a check's state as the block shows it.

    Parameters
    ----------
    result
        The check's result.

    Returns
    -------
    str
        ``FAILED`` and ``ERRORED`` for a blocking check, ``failed (kept)`` and
        ``ERRORED (kept)`` for a `keep` one, ``errored`` for a warning one, and ``passed`` or
        ``warned`` as they are; any other outcome as the server sent it.
    """
    if result.outcome == "failed":
        return "failed (kept)" if result.on_failure == "keep" else "FAILED"
    if result.outcome == "errored":
        if result.on_failure == "keep":
            return "ERRORED (kept)"
        return "ERRORED" if result.on_failure == "block" else "errored"
    return _output.inline(result.outcome)


def _check_tail(result: CheckResult) -> str:
    """Write what follows a check's state: the count of rows or the error.

    Parameters
    ----------
    result
        The check's result.

    Returns
    -------
    str
        ``N rows`` (``1 row``) for a failed or warned check, the error text on one line for an
        errored one, else empty.
    """
    if result.outcome == "errored":
        return _output.inline(result.error or "", _PROGRESS_REASON_LIMIT)
    if result.outcome in {"failed", "warned"} and result.failing_rows is not None:
        return f"{result.failing_rows} row" + ("" if result.failing_rows == 1 else "s")
    return ""


def _group_state(results: list[CheckResult]) -> str:
    """Name the state of a task's checks as a whole.

    Parameters
    ----------
    results
        The task's check results.

    Returns
    -------
    str
        ``FAILED`` when a blocking or `keep` check failed or errored, else ``warned`` when a check
        warned or a warning check errored (it was not checked), else ``passed``.
    """
    if any(_blocking_failure(r) for r in results):
        return "FAILED"
    if any(r.outcome == "warned" or r.outcome == "errored" for r in results):
        return "warned"
    return "passed"


def _check_group(task: RunTask) -> list[str]:
    """Build the lines of one task's checks.

    Parameters
    ----------
    task
        A task with check results.

    Returns
    -------
    list of str
        The heading (the table, its state, how many checks passed and what the rollback did,
        or that the project does not build the table, or that it was kept after a failed check)
        and a line per check, by name, marked ``(generated)`` when the asset's header generated it,
        then how many checks the server left out, if any.
    """
    results = sorted(task.checks, key=lambda r: r.name)
    name = _output.inline(task.asset)
    names = [_output.inline(check_shown(r)) for r in results]
    width = max([len(name), *map(len, names)]) + _NAME_PAD
    passed = sum(r.outcome in {"passed", "warned"} for r in results)
    summary = f"{passed} of {len(results)} checks passed"
    if task.kind == "source_check":
        note = (
            _CHECK_TASK_NOTE
            if task.kept_check is None
            else _KEPT_NOTE
            if task.kept_check
            else _SOURCE_NOTE
        )
        summary += f", {note}"
    else:
        rollback = next((r.rollback_outcome for r in results if r.rollback_outcome), None)
        if rollback is not None:
            summary += f", {_output.inline(_ROLLBACK_PHRASES.get(rollback, rollback))}"
    group = _group_state(results)
    state_width = max(_STATE_WIDTH, len(group), *(len(_check_state(r)) for r in results))
    lines = [f"{name.ljust(width)} {group.ljust(state_width)}  {summary}"]
    for result, short in zip(results, names, strict=True):
        line = f"  {short.ljust(width - 2)} {_check_state(result).ljust(state_width)}  "
        lines.append((line + _check_tail(result)).rstrip())
        if result.policy_note:
            lines.append(f"    note: {_output.inline(result.policy_note, _PROGRESS_REASON_LIMIT)}")
    if task.hidden_checks:
        lines.append(f"  {withheld_line(task.hidden_checks)}")
    return lines


def _emit_checks(state: CliState, run: Run) -> None:
    """Print the check results of a run under its task table, one group per checked task.

    Prints nothing for a run without check results, so that its output is what it was before
    checks existed.

    Parameters
    ----------
    state
        The CLI state (table mode).
    run
        The run.
    """
    groups = [_check_group(task) for task in run.tasks if task.checks or task.hidden_checks]
    if groups:
        typer.echo("")
        typer.echo("\n".join(line for group in groups for line in group))


def notification_state(notification: Notification) -> str:
    """Write where a notification is, in the words of `lakana runs show`.

    Parameters
    ----------
    notification
        The notification or delivery.

    Returns
    -------
    str
        ``delivered``, ``queued``, ``sending (attempt 1 of 5)`` (an attempt with no result yet),
        ``retrying (attempt 2 of 5); last: ...`` or ``failed: the server answered 404``.
    """
    result = _output.inline(notification.last_result, 300) if notification.last_result else ""
    if notification.state == "delivered":
        return "delivered"
    if notification.state == "failed":
        return f"failed: {result}" if result else "failed"
    if notification.attempts == 0:
        return "queued"
    progress = f"(attempt {notification.attempts} of {notification.max_attempts})"
    return f"retrying {progress}; last: {result}" if result else f"sending {progress}"


def _emit_notifications(state: CliState, run: Run) -> None:
    """Print the notifications of a run: event, delivery id and state per line; nothing without any.

    Parameters
    ----------
    state
        The CLI state (table mode).
    run
        The run.
    """
    if not run.notifications:
        return
    typer.echo("")
    typer.echo("Notifications")
    for notification in run.notifications:
        typer.echo(
            f"  {_output.inline(notification.event):<10}{_output.inline(notification.id)}  "
            f"{notification_state(notification)}"
        )


class _Progress:
    """Prints one plain line per task status change.

    A line for a changed status or attempt carries the task's reason for waiting when it is
    `no_worker`, `workers_busy` or `retry`. When only the reason's code changes to one of these,
    a line of its own is printed; `starting` and `upstream` print nothing, as they are the
    normal course of a run. The numbers in a sentence changing without the code never print
    anything.

    Parameters
    ----------
    state
        The CLI state; nothing is printed in JSON mode.
    """

    def __init__(self, state: CliState) -> None:
        self._state = state
        self._seen: dict[str, tuple[str, int]] = {}
        self._codes: dict[str, str | None] = {}

    def __call__(self, run: Run) -> None:
        """Print the tasks whose status or attempt changed since the last poll.

        Parameters
        ----------
        run
            The polled run.
        """
        if self._state.output == "json":
            return
        for task in run.tasks:
            key = (_status(task), task.attempt)
            reason = task.waiting_for
            code = None if reason is None else reason.code
            changed = self._seen.get(task.asset) != key
            code_changed = self._codes.get(task.asset) != code
            self._seen[task.asset] = key
            self._codes[task.asset] = code
            if changed:
                duration = _duration(task)
                line = _progress_line(task)
                line = f"{line}  {duration}" if duration else line
                if task.status == "failed" and task.failure is not None:
                    line = f"{line}  {_next_step(task)}"
                typer.echo(line)
            elif code_changed and code in _OWN_LINE_CODES and reason is not None:
                message = _output.inline(reason.message, _PROGRESS_REASON_LIMIT)
                if message.strip():
                    typer.echo(f"{task.asset}  {_status(task)}  {message}")


def _summary(state: CliState, run: Run) -> None:
    """Print a finished run: JSON, or its tasks as a table.

    Parameters
    ----------
    state
        The CLI state.
    run
        The run.
    """
    if state.output == "json":
        _output.print_json(run)
        return
    typer.echo(f"Run {run.id} {run.status}")
    _emit_tasks(state, run)
    _emit_failures(run)
    _emit_checks(state, run)


def register(app: typer.Typer) -> None:
    """Add `run`, `runs` and `scheduler`.

    Parameters
    ----------
    app
        The root app.
    """
    app.command("run")(run)
    app.add_typer(runs_app, name="runs")
    app.add_typer(scheduler_app, name="scheduler")
    app.add_typer(worker_app, name="worker")


def run(
    ctx: typer.Context,
    repo: _REPO = None,
    env: _ENV = None,
    select: Annotated[
        list[str] | None,
        typer.Option("--select", help="Asset pattern (shell glob); repeatable."),
    ] = None,
    upstream: Annotated[bool, typer.Option("--upstream", help="Add every ancestor.")] = False,
    downstream: Annotated[bool, typer.Option("--downstream", help="Add every descendant.")] = False,
    no_wait: Annotated[bool, typer.Option("--no-wait", help="Print the run id and exit.")] = False,
    timeout: Annotated[float | None, typer.Option("--timeout", help="Seconds to wait.")] = None,
) -> None:
    """Run REPO's deployed project version; exit 0 on success, 1 otherwise, 7 if one is active.

    Parameters
    ----------
    ctx
        The Typer context.
    repo
        Repository name.
    env
        Environment.
    select
        Asset patterns.
    upstream
        Include ancestors.
    downstream
        Include descendants.
    no_wait
        Return after printing the id.
    timeout
        Seconds to wait.

    Raises
    ------
    typer.Exit
        With code 1 when the run fails, is cancelled or the timeout elapses, and
        130 when interrupted.
    """
    state = state_of(ctx)
    repository = resolve_repository(repo)
    environment = resolve_environment(state, env)
    client = state.client()
    started = client.runs.start(
        environment, repository, select=select or (), upstream=upstream, downstream=downstream
    )
    follow_run(state, environment, started, no_wait=no_wait, timeout=timeout)


def follow_run(
    state: CliState, environment: str, started: Run, *, no_wait: bool, timeout: float | None
) -> None:
    """Print a started run's id and, unless told not to, follow it to its end.

    Shared by `lakana run` and `lakana job run`, so a run looks the same however it started.

    Parameters
    ----------
    state
        The CLI state.
    environment
        Environment name.
    started
        The queued run.
    no_wait
        Print the id (or the run as JSON) and return.
    timeout
        Seconds to wait; `None` waits without a limit.

    Raises
    ------
    typer.Exit
        With code 1 when the run fails, is cancelled or the timeout elapses, and
        130 when interrupted.
    """
    client = state.client()
    if no_wait:
        if state.output == "json":
            _output.print_json(started)
        else:
            typer.echo(started.id)
        return
    if state.output != "json":
        typer.echo(started.id, err=True)
    try:
        finished = client.runs.wait(
            environment, started.id, timeout=timeout, on_update=_Progress(state)
        )
    except RunFailed as exc:
        _summary(state, exc.run)
        raise typer.Exit(1) from exc
    except KeyboardInterrupt as exc:
        typer.echo(
            "Interrupted. The run keeps going; follow it with "
            f"'lakana runs show {started.id} --env {environment}'.",
            err=True,
        )
        raise typer.Exit(130) from exc
    except TimeoutError as exc:
        typer.echo(
            f"error: {exc}. The run keeps going; follow it with "
            f"'lakana runs show {started.id} --env {environment}'.",
            err=True,
        )
        raise typer.Exit(1) from exc
    _summary(state, finished)


@runs_app.command("list")
def list_runs(
    ctx: typer.Context,
    env: _ENV = None,
    repo: Annotated[str | None, typer.Option("--repo", help="Only this repository.")] = None,
    status: Annotated[
        list[str] | None,
        typer.Option(
            "--status",
            help="Only runs in this status: queued, running, succeeded, failed or cancelled. "
            "Repeat the option or separate the values with commas.",
        ),
    ] = None,
    trigger: Annotated[
        list[str] | None,
        typer.Option(
            "--trigger",
            help="Only runs started this way: schedule, api, manual or deploy. Repeat the "
            "option or separate the values with commas.",
        ),
    ] = None,
    job: Annotated[str | None, typer.Option("--job", help="Only runs of this job.")] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Only runs created at or after this date or time, such as 2026-10-18 or "
            "2026-10-18T09:30:00Z. Without a time zone it is read in yours.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, max=200, help="Most runs to list (default 100)."),
    ] = None,
    cursor: Annotated[
        str | None,
        typer.Option("--cursor", help="Continue after the page that printed this cursor."),
    ] = None,
) -> None:
    """List runs, newest first.

    A long list prints a "More results" line with the command that shows the next page.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        Environment.
    repo
        Repository filter.
    status
        Status filter.
    trigger
        Trigger filter.
    job
        Job filter.
    since
        Only runs created at or after this time.
    limit
        Most runs returned (1 to 200); the server's default when omitted.
    cursor
        The cursor of the page to show.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    page = state.client().runs.list_page(
        environment,
        repository=repo,
        status=status,
        trigger=trigger,
        job=job,
        since=parse_since(since),
        limit=limit,
        cursor=cursor,
    )
    _output.emit(state, page.items, columns=_RUN_COLUMNS, droppable=_RUN_DROPPABLE)
    more_results(
        state,
        ["lakana", "runs", "list"],
        {
            "--env": environment,
            "--repo": repo,
            "--status": status,
            "--trigger": trigger,
            "--job": job,
            "--since": since,
            "--limit": limit,
        },
        page.next_cursor,
    )


@runs_app.command("show")
def show(ctx: typer.Context, run_id: str, env: _ENV = None) -> None:
    """Show run RUN_ID, its tasks and its notifications; waiting_for says why a task waits.

    Parameters
    ----------
    ctx
        The Typer context.
    run_id
        The run.
    env
        Environment.
    """
    state = state_of(ctx)
    found = state.client().runs.get(resolve_environment(state, env), run_id)
    if state.output == "json":
        _output.print_json(found)
        return
    _output.emit(state, found, columns=_SHOW_COLUMNS)
    _emit_tasks(state, found)
    _emit_failures(found)
    _emit_checks(state, found)
    _emit_notifications(state, found)


@dataclass
class _Followed:
    """What `lakana runs logs --follow` has printed so far.

    Parameters
    ----------
    offsets
        The byte to ask for next, per task.
    headed
        Tasks whose ``== asset ==`` header was printed.
    complete
        Tasks whose log is finished and read to the end.
    """

    offsets: dict[str, int] = field(default_factory=dict)
    headed: set[str] = field(default_factory=set)
    complete: set[str] = field(default_factory=set)


def _print_log(
    state: CliState,
    environment: str,
    run_id: str,
    asset: str,
    followed: _Followed | None,
    header: bool,
) -> None:
    """Print one task's log: all of it, or only what a follow has not printed yet.

    Parameters
    ----------
    state
        The CLI state.
    environment
        Environment name.
    run_id
        The run.
    asset
        The task.
    followed
        The follow's progress, updated; `None` prints the whole log with one request.
    header
        Print an ``== asset ==`` header before the first output.
    """
    while True:
        offset = None if followed is None else followed.offsets.get(asset, 0)
        log = state.client().runs.log(environment, run_id, asset, offset=offset)
        if log.text:
            if header and (followed is None or asset not in followed.headed):
                typer.echo(f"== {asset} ==")
            typer.echo(log.text, nl=False)
            if followed is not None:
                followed.headed.add(asset)
        if followed is None:
            return
        followed.offsets[asset] = log.next_offset
        if log.complete:
            followed.complete.add(asset)
        if not log.more:
            return


@runs_app.command("logs")
def logs(
    ctx: typer.Context,
    run_id: str,
    env: _ENV = None,
    task: Annotated[str | None, typer.Option("--task", help="Only this task's log.")] = None,
    follow: Annotated[
        bool,
        typer.Option(
            "--follow",
            help="Keep printing new text until the run finishes; each byte is fetched once.",
        ),
    ] = False,
) -> None:
    """Print a run's task logs.

    Parameters
    ----------
    ctx
        The Typer context.
    run_id
        The run.
    env
        Environment.
    task
        One task.
    follow
        Poll until the run, or the one task, finishes.

    Raises
    ------
    typer.Exit
        With code 130 when interrupted.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    followed = _Followed() if follow else None
    try:
        while True:
            current = state.client().runs.get(environment, run_id)
            assets = [task] if task is not None else [t.asset for t in current.tasks]
            for asset in assets:
                if followed is None or asset not in followed.complete:
                    _print_log(state, environment, run_id, asset, followed, header=task is None)
            if followed is None or current.status in _FINISHED:
                return
            if assets and followed.complete.issuperset(assets):
                return
            time.sleep(_FOLLOW_SECONDS)
    except KeyboardInterrupt as exc:
        raise typer.Exit(130) from exc


@runs_app.command("cancel")
def cancel(ctx: typer.Context, run_id: str, env: _ENV = None) -> None:
    """Request cancellation of RUN_ID.

    Parameters
    ----------
    ctx
        The Typer context.
    run_id
        The run.
    env
        Environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    cancelled = state.client().runs.cancel(environment, run_id)
    _output.done(
        state, f"Cancel requested for run {run_id} in {_output.inline(environment)}", cancelled
    )


@runs_app.command("resume")
def resume(ctx: typer.Context, run_id: str, env: _ENV = None) -> None:
    """Resume RUN_ID's failed, upstream_failed and cancelled tasks in a new run.

    Parameters
    ----------
    ctx
        The Typer context.
    run_id
        The finished run.
    env
        Environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    resumed = state.client().runs.resume(environment, run_id)
    if state.output == "json":
        _output.print_json(resumed)
        return
    typer.echo(resumed.id)


@scheduler_app.command("status")
def scheduler_status(ctx: typer.Context) -> None:
    """Show the scheduler holder and queue lengths (admin).

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().scheduler.status())


def _now() -> datetime:
    """Return the current time (a seam for the tests of the `last_seen` column).

    Returns
    -------
    datetime
        The current UTC time.
    """
    return datetime.now(UTC)


def _last_seen(worker: WorkerInfo) -> str:
    """Write how long ago a worker's last heartbeat was, in its largest whole unit.

    Parameters
    ----------
    worker
        The listed worker.

    Returns
    -------
    str
        `5s ago`, `7m ago`, `2h ago` or `1d ago`; `0s ago` for a time ahead of this machine's
        clock. `--output json` has the time itself (`heartbeat_at`).
    """
    seconds = max(0, int((_now() - worker.heartbeat_at).total_seconds()))
    for unit, size in (("d", 86_400), ("h", 3_600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def _doing(worker: WorkerInfo) -> str:
    """Write what a worker is doing from the run ids it carries.

    Parameters
    ----------
    worker
        The listed worker.

    Returns
    -------
    str
        `idle`, or `query ID`, `deploy ID` and `task ID` joined by `, ` (a query can run beside
        a Python task), each id cut to its first `_ID_SHOWN` characters so that the column
        stays readable; `--output json` has the whole id.
    """
    parts = [
        f"{label} {_output.inline(run_id[:_ID_SHOWN])}"
        for label, run_id in (
            ("query", worker.query_run_id),
            ("deploy", worker.deploy_run_id),
            ("task", worker.task_run_id),
        )
        if run_id is not None
    ]
    return ", ".join(parts) if parts else "idle"


@worker_app.command("list")
def list_workers(ctx: typer.Context) -> None:
    """List the registered workers and what each is doing (admin).

    A worker is alive while its heartbeat is recent and lost after that; a lost worker's row
    stays for an hour, and the query or deploy it held is ended by the scheduler. The column
    takes is the work the worker takes: all, interactive (ad-hoc queries) or pipeline (deploys,
    pipeline tasks and table maintenance). The column caches is the disk its caches take, as it
    last measured them (hourly).

    Parameters
    ----------
    ctx
        The Typer context.
    """
    state = state_of(ctx)
    workers = state.client().workers.list()
    if not workers and state.output != "json":
        typer.echo(
            "No worker is registered. Start one; on a local instance, start it again with "
            "'lakana server start --local'."
        )
        return
    _output.emit(
        state,
        workers,
        columns=_WORKER_COLUMNS,
        droppable=_WORKER_DROPPABLE,
        derived={
            "last_seen": _last_seen,
            "doing": _doing,
            "caches": lambda w: cache_size_cell(w, "total_bytes"),
        },
        whole=["takes", "doing", "caches"],
        footnotes={
            **({"doing": _DOING_NOTE} if any(_doing(w) != "idle" for w in workers) else {}),
            "caches": _CACHES_NOTE,
        },
    )
