"""`lakana job list|show|run|pause|resume|notify-test|notification`: the jobs of a project."""

from __future__ import annotations

import time
from datetime import UTC
from typing import Annotated

import typer

from lakana._models import Job, Trigger
from lakana.cli import _output
from lakana.cli._state import resolve_environment, state_of
from lakana.cli.deploys import resolve_repository
from lakana.cli.runs import follow_run, notification_state

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_REPO = Annotated[
    str | None, typer.Option("--repo", help="Repository name (default: lakana.yaml's repo:).")
]
_NAME = Annotated[str, typer.Argument(help="The job's name, as in lakana.yaml.")]
_COLUMNS = ["name", "repository", "state", "triggers", "next_run", "last_run"]
_DROPPABLE = ["repository", "last_run", "triggers"]
"""What `job list` leaves out first on a narrow terminal: the name, state and next run stay."""

_NOTIFY_POLLS = 30
"""How many times `notify-test` reads the delivery, one second apart, before it gives up."""
_DELIVERY_ID = Annotated[str, typer.Argument(help="The delivery id `job notify-test` printed.")]

job_app = typer.Typer(
    help=(
        "Jobs: named selections of a project, started by hand, by the API, by a schedule or "
        "after each deploy."
    ),
    no_args_is_help=True,
)


def _trigger(trigger: Trigger) -> str:
    """Write one trigger.

    Parameters
    ----------
    trigger
        The trigger.

    Returns
    -------
    str
        ``schedule 0 6 * * * Europe/Berlin`` with `` (paused)`` when paused, ``after each
        deploy`` or ``on demand``.
    """
    if trigger.kind == "on_demand":
        return "on demand"
    if trigger.kind == "deploy":
        return "after each deploy"
    paused = " (paused)" if trigger.paused else ""
    return f"schedule {trigger.cron} {trigger.timezone}{paused}"


def _triggers(job: Job) -> str:
    """Write a job's triggers for the list.

    Parameters
    ----------
    job
        The job.

    Returns
    -------
    str
        One trigger per line, schedule first.
    """
    return "\n".join(_trigger(trigger) for trigger in job.triggers)


def _triggers_detail(job: Job) -> str:
    """Write a job's triggers for `job show`, one per line, with the next run.

    Parameters
    ----------
    job
        The job.

    Returns
    -------
    str
        A line per trigger: a schedule with its next run (or how to resume it), and how to start by
        hand.
    """
    lines = []
    for trigger in job.triggers:
        if trigger.kind == "on_demand":
            lines.append(f"on demand: lakana job run {_output.inline(job.name)}")
        elif trigger.kind == "deploy":
            lines.append("after each deploy: starts as the deployer")
        elif trigger.paused:
            resume = f"lakana job resume {_output.inline(job.name)}"
            lines.append(f"{_trigger(trigger)}; resume it with '{resume}'")
        else:
            lines.append(f"{_trigger(trigger)}; next run {_next_run(job)}".rstrip())
    return "\n".join(lines)


def _next_run(job: Job) -> str:
    """Write a job's next scheduled time, in its zone and in UTC.

    Parameters
    ----------
    job
        The job.

    Returns
    -------
    str
        ``2026-07-01 03:00 Europe/Berlin (01:00 UTC)``; the zone alone for a job in UTC; empty
        when nothing is scheduled (a paused job keeps no time that would fire).
    """
    if job.next_run_at is None or job.next_run_local is None:
        return ""
    local = job.next_run_local[:16].replace("T", " ")
    at = job.next_run_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    if job.timezone in (None, "UTC"):
        return f"{at} UTC"
    return f"{local} {job.timezone} ({at[11:]} UTC)"


def _last_run(job: Job) -> str:
    """Write a job's latest run for the list.

    Parameters
    ----------
    job
        The job.

    Returns
    -------
    str
        ``succeeded (schedule)``, or empty for a job that never ran.
    """
    if job.last_run_status is None:
        return ""
    return (
        f"{job.last_run_status} ({job.last_run_trigger})"
        if job.last_run_trigger
        else (job.last_run_status)
    )


def register(app: typer.Typer) -> None:
    """Add `job`.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(job_app, name="job")


@job_app.command("list")
def list_jobs(ctx: typer.Context, env: _ENV = None, repo: _REPO = None) -> None:
    """List the jobs of the deployed projects: state, triggers, next run and last run.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        Environment.
    repo
        Only this repository's jobs (default: all of them).
    """
    state = state_of(ctx)
    jobs = state.client().jobs.list(resolve_environment(state, env), repository=repo)
    _output.emit(
        state,
        jobs,
        columns=_COLUMNS,
        droppable=_DROPPABLE,
        derived={
            "triggers": _triggers,
            "next_run": _next_run,
            "last_run": _last_run,
        },
        whole=["triggers"],
    )
    if state.output == "json":
        return
    for job in jobs:
        if job.waiting_for is not None:
            typer.echo(
                f"{_output.inline(job.name)}: {_output.inline(job.waiting_for.message, 300)}"
            )


@job_app.command("show")
def show_job(ctx: typer.Context, name: _NAME, env: _ENV = None, repo: _REPO = None) -> None:
    """Show job NAME: its state, its triggers with the next run, and its last run.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The job.
    env
        Environment.
    repo
        Repository name; needed only when several repositories declare a job of this name.
    """
    state = state_of(ctx)
    job = state.client().jobs.get(resolve_environment(state, env), name, repository=repo)
    _output.emit(
        state,
        job,
        columns=["name", "repository", "state", "triggers", "select", "last_run"],
        derived={"triggers": _triggers_detail, "last_run": _last_run},
        flexible=["triggers"],
    )
    if state.output != "json" and job.waiting_for is not None:
        typer.echo(_output.inline(job.waiting_for.message, 300))


@job_app.command("run")
def run_job(
    ctx: typer.Context,
    name: _NAME,
    env: _ENV = None,
    repo: _REPO = None,
    no_wait: Annotated[bool, typer.Option("--no-wait", help="Print the run id and exit.")] = False,
    timeout: Annotated[float | None, typer.Option("--timeout", help="Seconds to wait.")] = None,
) -> None:
    """Start job NAME now; if its project is busy, the start waits and begins when it is free.

    Exit 0 when the run succeeds, 1 when it fails, is cancelled or the timeout elapses. When the
    start waits, the command prints what it waits for and exits 0: that means accepted, not run;
    a script that needs the result follows the run it names with `lakana runs show`.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The job.
    env
        Environment.
    repo
        Repository name.
    no_wait
        Return after printing the id.
    timeout
        Seconds to wait.
    """
    state = state_of(ctx)
    repository = resolve_repository(repo)
    environment = resolve_environment(state, env)
    started = state.client().jobs.start(environment, repository, name)
    if started.run is not None:
        follow_run(state, environment, started.run, no_wait=no_wait, timeout=timeout)
        return
    if state.output == "json":
        _output.print_json(started)
    elif started.waiting is not None:
        _output.done(state, _output.inline(started.waiting.message, 300), started)


@job_app.command("pause")
def pause(ctx: typer.Context, name: _NAME, env: _ENV = None, repo: _REPO = None) -> None:
    """Pause job NAME's schedule; a waiting start still happens and a start by hand still works.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The job.
    env
        Environment.
    repo
        Repository name.
    """
    state = state_of(ctx)
    repository = resolve_repository(repo)
    environment = resolve_environment(state, env)
    paused = state.client().jobs.pause(environment, repository, name)
    _output.done(
        state,
        f"Paused {_output.inline(name)} in {_output.inline(environment)}; "
        f"resume it with 'lakana job resume {_output.inline(name)} --env "
        f"{_output.inline(environment)}'.",
        paused,
    )


@job_app.command("resume")
def resume(ctx: typer.Context, name: _NAME, env: _ENV = None, repo: _REPO = None) -> None:
    """Resume job NAME's schedule from now; the times missed while paused do not fire.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The job.
    env
        Environment.
    repo
        Repository name.
    """
    state = state_of(ctx)
    repository = resolve_repository(repo)
    environment = resolve_environment(state, env)
    resumed = state.client().jobs.resume(environment, repository, name)
    next_run = _next_run(resumed)
    _output.done(
        state,
        f"Resumed {_output.inline(name)} in {_output.inline(environment)}"
        + (f"; next run {next_run}." if next_run else "."),
        resumed,
    )


@job_app.command("notify-test")
def notify_test(ctx: typer.Context, name: _NAME, env: _ENV = None, repo: _REPO = None) -> None:
    """Send a test message to job NAME's webhook and wait up to 30 seconds for the result.

    The message carries clearly fake run data. Exit 0 when it was delivered; 1 when the webhook
    refused it or was not reached (the reason is printed), and also 1 when it is still being
    tried after the wait (look later with `lakana job notification`). Needs the deploy right.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The job.
    env
        Environment.
    repo
        Repository name.

    Raises
    ------
    typer.Exit
        With 1 unless the message was delivered.
    """
    state = state_of(ctx)
    repository = resolve_repository(repo)
    environment = resolve_environment(state, env)
    jobs = state.client().jobs
    delivery_id = jobs.notify_test(environment, repository, name)
    delivery = jobs.delivery(environment, delivery_id)
    for _ in range(_NOTIFY_POLLS - 1):
        if delivery.state != "pending":
            break
        time.sleep(1)
        delivery = jobs.delivery(environment, delivery_id)
    if state.output == "json":
        _output.print_json(delivery)
    elif delivery.state == "delivered":
        typer.echo("delivered")
    if delivery.state == "delivered":
        return
    if state.output != "json":
        again = f"lakana job notify-test {_output.inline(name)} --env {_output.inline(environment)}"
        if delivery.state == "failed":
            typer.echo(notification_state(delivery), err=True)
            typer.echo(
                f"Check the webhook secret and the address it names, then run '{again}' again.",
                err=True,
            )
        else:
            later = (
                f"lakana job notification {_output.inline(delivery_id)} "
                f"--env {_output.inline(environment)}"
            )
            tried = (
                f"still trying (attempt {delivery.attempts} of {delivery.max_attempts})"
                if delivery.attempts
                else "still queued (no attempt yet; is a worker running?)"
            )
            typer.echo(f"{tried}; see '{later}'.", err=True)
    raise typer.Exit(1)


@job_app.command("notification")
def notification(ctx: typer.Context, delivery_id: _DELIVERY_ID, env: _ENV = None) -> None:
    """Show one notification delivery, such as the one `job notify-test` queued.

    Parameters
    ----------
    ctx
        The Typer context.
    delivery_id
        The delivery.
    env
        Environment.
    """
    state = state_of(ctx)
    found = state.client().jobs.delivery(resolve_environment(state, env), delivery_id)
    _output.emit(
        state,
        found,
        columns=["id", "job", "event", "result"],
        derived={"result": notification_state},
        flexible=["result"],
    )
