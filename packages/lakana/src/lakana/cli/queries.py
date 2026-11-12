"""`lakana sql` and `lakana query *`: run SQL against an environment."""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import pyarrow as pa
import typer

from lakana._models import QueryListEntry, QueryStatus, ResultPage, WaitingFor
from lakana.cli import _files, _output, _retention
from lakana.cli._break_glass import BREAK_GLASS, REASON, break_glass_reason
from lakana.cli._paging import more_results
from lakana.cli._state import CliError, CliState, resolve_environment, state_of
from lakana.cli._waiting import WAIT_LINE_LIMIT, WaitLine, clock
from lakana.errors import (
    AuthenticationError,
    Conflict,
    LakanaError,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from lakana.resources.queries import QueryRun

query_app = typer.Typer(
    help="Submit queries and fetch their results step by step.", no_args_is_help=True
)

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
OUT = Annotated[Path | None, typer.Option("--out", help="Write the full result to this file.")]
FORMAT = Annotated[
    str | None,
    typer.Option("--format", help="parquet, csv or arrow (default: from the extension)."),
]
MAX_ROWS = Annotated[int, typer.Option("--max-rows", min=0, help="Most rows to display.")]
TIMEOUT = Annotated[
    float | None, typer.Option("--timeout", help="Seconds to wait before giving up.")
]
_INTO = Annotated[
    str | None,
    typer.Option("--into", help="Write the result into this table or view instead of printing it."),
]
_MODE = Annotated[
    str | None, typer.Option("--mode", help="create, replace, append or view (needs --into).")
]
_INCREMENTAL_KEY = Annotated[
    str | None,
    typer.Option(
        "--incremental-key", help="Append only rows past the target's max of this column."
    ),
]
_EVOLVE_SCHEMA = Annotated[
    bool,
    typer.Option("--evolve-schema", help="Let the target adopt the result's schema."),
]
_WRITE_MODES = ("create", "replace", "append", "view")
_LIST_COLUMNS = ["id", "submitter", "kind", "status", "age", "waiting_for"]
_LIST_DROPPABLE = ["age", "kind", "submitter"]
"""What `query list` leaves out first on a narrow terminal: the id, status and reason stay."""
_LIST_MOST = 200
"""The most rows the server returns; the `--limit` bound."""


def _check_write_flags(
    into: str | None,
    mode: str | None,
    incremental_key: str | None,
    evolve_schema: bool,
    out: Path | None,
) -> None:
    """Refuse write flags that don't go together, before anything is sent.

    Parameters
    ----------
    into
        `--into`.
    mode
        `--mode`.
    incremental_key
        `--incremental-key`.
    evolve_schema
        `--evolve-schema`.
    out
        `--out`.

    Raises
    ------
    CliError
        For every invalid combination (exit 2).
    """
    if (into is None) != (mode is None):
        raise CliError("--into and --mode go together.")
    if into is None:
        if incremental_key is not None or evolve_schema:
            raise CliError("--incremental-key and --evolve-schema need --into.")
        return
    if mode not in _WRITE_MODES:
        raise CliError(f"--mode must be one of {', '.join(_WRITE_MODES)}, not {mode!r}.")
    if incremental_key is not None and mode != "append":
        raise CliError("--incremental-key needs --mode append.")
    if evolve_schema and mode not in ("replace", "append"):
        raise CliError("--evolve-schema needs --mode replace or append.")
    if out is not None:
        raise CliError("--out has nothing to write: a run with --into has no result.")


def _written(environment: str, into: str, mode: str, status: QueryStatus) -> str:
    """Describe a finished write run in one line.

    Parameters
    ----------
    environment
        The environment.
    into
        The target.
    mode
        The mode.
    status
        The finished run's status.

    Returns
    -------
    str
        For example ``"Wrote 3 rows to dev/gold.daily (snapshot 7)"``.
    """
    if mode == "view":
        return f"Created view {_output.inline(environment)}/{_output.inline(into)}"
    message = (
        f"Wrote {status.rows_written} rows to {_output.inline(environment)}/{_output.inline(into)}"
    )
    if status.snapshot_id is not None:
        message += f" (snapshot {status.snapshot_id})"
    if status.watermark is not None:
        message += f"; watermark {_output.inline(str(status.watermark))}"
    return message


@dataclass(frozen=True)
class Submitted:
    """What `query submit` reports.

    Parameters
    ----------
    id
        The query run's id.
    environment
        The environment it runs in.
    """

    id: str
    environment: str


@dataclass(frozen=True)
class Written:
    """What `--out` reports.

    Parameters
    ----------
    rows
        Rows written.
    path
        The file written.
    """

    rows: int
    path: str


def out_format(out: Path | None, fmt: str | None) -> str | None:
    """Resolve `--out`'s format up front, before any query runs.

    Parameters
    ----------
    out
        The destination file, if any.
    fmt
        The `--format` value, if given.

    Returns
    -------
    str or None
        One of `_files.FORMATS`, or `None` without `--out`.
    """
    return None if out is None else _files.infer_format(out, fmt)


def _render(
    state: CliState, table: pa.Table, out: Path | None, fmt: str | None, max_rows: int
) -> None:
    """Print a result, or write it to `out`.

    Parameters
    ----------
    state
        The CLI state.
    table
        The result.
    out
        The destination file, if any.
    fmt
        Its format, already resolved by `out_format`.
    max_rows
        The display cap.
    """
    if out is None or fmt is None:
        _output.emit_arrow(state, table, max_rows)
        return
    _files.write_table(table, out, fmt)
    _output.done(
        state,
        f"Wrote {table.num_rows} rows to {_output.inline(str(out))}",
        Written(rows=table.num_rows, path=str(out)),
    )


_INTERRUPTED = 130
"""The exit status of a Ctrl-C (128 + SIGINT), as a shell reports it."""

_REFUSALS = (AuthenticationError, NotFound, PermissionDenied, ValidationError)
"""Errors that mean the server answered and said no, as against not being reached."""


def _cancel_message(status: QueryStatus, environment: str) -> str:
    """Say what a cancel request did, by the status the server answered with.

    Parameters
    ----------
    status
        The status the server answered the cancel with.
    environment
        The environment of the query.

    Returns
    -------
    str
        One line for `lakana query cancel`.
    """
    query = _output.inline(status.id)
    if status.status == "CANCELLED":
        return f"Cancelled query {query}."
    if status.status in ("QUEUED", "RUNNING"):
        return (
            f"Asked query {query} to stop. A running query ends within the grace period (10 s "
            f"unless the operator changed it); check with 'lakana query status {query} "
            f"--env {_output.inline(environment)}'."
        )
    return f"Query {query} had already ended ({status.status}); nothing was changed."


def _interrupted(run: QueryRun, environment: str) -> str:
    """Cancel the query the user stopped waiting for, and say what happened.

    A second Ctrl-C while the request is on its way ends the attempt at once and says that
    the query may still be running.

    Parameters
    ----------
    run
        The query the user interrupted.
    environment
        The environment of the query.

    Returns
    -------
    str
        The line for standard error.
    """
    query, env = _output.inline(run.id), _output.inline(environment)
    later = f"cancel it with 'lakana query cancel {query} --env {env}'"
    try:
        with contextlib.suppress(KeyboardInterrupt):  # a Ctrl-C in the announcement still cancels
            typer.echo(
                _output.safe_text(
                    f"Interrupted. Cancelling query {query}; press Ctrl-C again to stop waiting "
                    "for it."
                ),
                err=True,
            )
        status = run.cancel()
    except KeyboardInterrupt:
        return f"Interrupted again. Query {query} may still be running; {later}."
    except Conflict:
        # The server's text names the same two commands, but `inline` cuts it before the second.
        return (
            f"Interrupted. Query {query} was not cancelled: it is being started or is finishing. "
            f"Check it with 'lakana query status {query} --env {env}'; if it is still running, "
            f"cancel it with 'lakana query cancel {query} --env {env}'."
        )
    except _REFUSALS as exc:
        return f"Interrupted. Query {query} was not cancelled: {_output.inline(str(exc))}"
    except LakanaError as exc:
        return (
            f"Interrupted. The query could not be cancelled ({_output.inline(str(exc))}) and may "
            f"still be running; {later}."
        )
    if status.status == "CANCELLED":
        return f"Interrupted. Cancelled query {query}."
    if status.status in ("QUEUED", "RUNNING"):
        return (
            f"Interrupted. Asked query {query} to stop; check with 'lakana query status {query} "
            f"--env {env}'."
        )
    return f"Interrupted. Query {query} had already ended ({_output.inline(status.status)})."


_STATUS_COLUMNS = ["id", "status", "waiting_for", "error_message"]
"""What `query status` and `query wait` show; the reason is empty for a query not waiting."""


def _cut(reason: WaitingFor | None) -> str:
    """Make a reason's sentence safe and short for a table cell.

    Parameters
    ----------
    reason
        The reason, or `None`.

    Returns
    -------
    str
        The sentence on one line, escaped and cut at `WAIT_LINE_LIMIT` characters; nothing
        for no reason.
    """
    return "" if reason is None else _output.inline(reason.message, WAIT_LINE_LIMIT)


def _reason(status: QueryStatus) -> str:
    """Show the sentence that says why a queued query waits.

    Parameters
    ----------
    status
        The query's status.

    Returns
    -------
    str
        The reason's message, or nothing (the table cell escapes it).
    """
    return _cut(status.waiting_for)


def _wait(run: QueryRun, environment: str, timeout: float | None, state: CliState) -> QueryStatus:
    """Wait for a query; on Ctrl-C cancel it, say so on standard error, and exit with 130.

    While the query is queued, a line on standard error says why (see `WaitLine`).

    Parameters
    ----------
    run
        The query to wait for.
    environment
        The environment of the query.
    timeout
        Seconds to wait before giving up, or `None`.
    state
        The CLI state.

    Returns
    -------
    QueryStatus
        The query's status, once it has succeeded.

    Raises
    ------
    typer.Exit
        With status 130 after a Ctrl-C.
    """
    try:
        return run.wait(timeout=timeout, on_update=WaitLine(state, monotonic=clock))
    except KeyboardInterrupt as exc:
        typer.echo(_output.safe_text(_interrupted(run, environment)), err=True)
        raise typer.Exit(_INTERRUPTED) from exc


@contextlib.contextmanager
def _fetching(run: QueryRun, environment: str) -> Generator[None]:
    """Turn a Ctrl-C while the result of a finished query is fetched into exit 130.

    Parameters
    ----------
    run
        The query that has succeeded.
    environment
        The environment of the query.

    Yields
    ------
    None
        Control, for the fetch.

    Raises
    ------
    typer.Exit
        With status 130 after a Ctrl-C.
    """
    try:
        yield
    except KeyboardInterrupt as exc:
        query, env = _output.inline(run.id), _output.inline(environment)
        typer.echo(
            f"Interrupted. Query {query} had finished; fetch its result with "
            f"'lakana query result {query} --env {env}'.",
            err=True,
        )
        raise typer.Exit(_INTERRUPTED) from exc


@contextlib.contextmanager
def _unsubmitted(environment: str) -> Generator[None]:
    """Turn a Ctrl-C while a query is being submitted into a clean exit with 130.

    No query id is known then, so there is nothing to cancel and no request is made. The
    message names the command that finds the query if the request did arrive.

    Parameters
    ----------
    environment
        The environment the query was meant for.

    Yields
    ------
    None
        Control, for the submission.

    Raises
    ------
    typer.Exit
        With status 130 after a Ctrl-C.
    """
    env = _output.inline(environment)
    try:
        yield
    except KeyboardInterrupt as exc:
        typer.echo(
            "Interrupted before the server confirmed the query. No query id is known, so "
            "nothing was cancelled. If the request had already reached the server, the query "
            "may still be queued or running: list your queries with "
            f"'lakana query list --env {env}' (add --all for the ones that just ended) and "
            f"cancel it with 'lakana query cancel ID --env {env}'.",
            err=True,
        )
        raise typer.Exit(_INTERRUPTED) from exc


def show_result(
    state: CliState,
    environment: str,
    submit: Callable[[], QueryRun],
    *,
    out: Path | None,
    fmt: str | None,
    max_rows: int,
    timeout: float | None,
) -> None:
    """Submit a read query, wait for it, and print its rows or write them to a file.

    This is what `lakana sql` does for a read, and what `lakana check rows` does after it has
    queued its query: one implementation, so the two behave alike (the wait line, Ctrl-C, a
    failed query's message and exit status, the output options).

    Parameters
    ----------
    state
        The CLI state.
    environment
        The environment the query runs in.
    submit
        Queues the query and returns its handle; called inside the guard that turns a Ctrl-C
        before the server confirmed it into exit 130.
    out
        Write the full result here instead of printing.
    fmt
        The file's format, already resolved by `out_format`.
    max_rows
        The most rows to display.
    timeout
        Seconds to wait for the query.
    """
    with _unsubmitted(environment):
        run = submit()
    _wait(run, environment, timeout, state)
    with _fetching(run, environment):
        table = run.result()
    _render(state, table, out, fmt, max_rows)


def page_cell(value: object) -> str:
    """Write one cell of a JSON result page as the text a table shows.

    Parameters
    ----------
    value
        The cell as the server wrote it: a scalar, a list, an object, or one of the markers
        ``{"cut": true, "text": ...}``, ``{"cut": true}`` and ``{"binary": n}``.

    Returns
    -------
    str
        A cut string is its prefix and an ellipsis, a cut value alone an ellipsis, binary
        ``<n bytes>``, a list or object compact JSON, and `None` nothing.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        if value.get("cut") is True:
            text = value.get("text")
            return f"{text}…" if isinstance(text, str) else "…"
        if set(value) == {"binary"}:
            return f"<{value['binary']} bytes>"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _masked_line(page: ResultPage) -> str | None:
    """Name the columns a preview masked for the reader, never the rule.

    Parameters
    ----------
    page
        The page.

    Returns
    -------
    str or None
        `Masked for you: ...`, a column of the previewed table by its name and one behind a view
        as `table.column`; `None` when no column was masked or it is not known.
    """
    names = [
        m.column if m.table == page.preview_table else f"{m.table}.{m.column}"
        for m in page.masked_columns or ()
    ]
    if page.masked_columns_cut:
        names.append(f"and {page.masked_columns_cut} more")
    return f"Masked for you: {', '.join(names)}" if names else None


def show_page(
    state: CliState, environment: str, submit: Callable[[], QueryRun], *, timeout: float | None
) -> None:
    """Submit a preview, wait for it, and print its first page, with what was masked on stderr.

    Ctrl-C cancels the run as in `lakana sql`; a failed run's message is the error. In JSON
    mode the page is printed as it is.

    Parameters
    ----------
    state
        The CLI state.
    environment
        The environment the run belongs to.
    submit
        Queues the preview and returns its handle; called inside the guard that turns a Ctrl-C
        before the server confirmed it into exit 130.
    timeout
        Seconds to wait for the run.
    """
    with _unsubmitted(environment):
        run = submit()
    _wait(run, environment, timeout, state)
    with _fetching(run, environment):
        page = run.page()
    if state.output == "json":
        _output.print_json(page)
        return
    if page.rows:
        _output.emit_text_table(
            [c.name for c in page.columns],
            [[page_cell(cell) for cell in row] for row in page.rows],
        )
        shown = len(page.rows)
        facts = [
            f"First {shown} rows"
            if page.next_offset is None
            else f"First {shown} of {page.row_count} rows",
            "read as you",
        ]
        if page.duration_ms is not None:
            facts.append(f"{page.duration_ms / 1000:.1f} s")
    else:
        sys.stdout.write("No rows.\n")
        facts = ["Read as you."]
    lines = [" · ".join(facts), _masked_line(page)]
    if page.next_offset is not None:
        table = page.preview_table or "TABLE"
        lines.append(
            f"The page is full; the other rows are left out. Read them with "
            f"'lakana sql \"SELECT * FROM {table} OFFSET {page.next_offset}\"', "
            "or select fewer columns to see more rows at once."
        )
    if page.columns_cut:
        lines.append(f"{page.columns_cut} more columns not shown; use --output json.")
    for line in lines:
        if line:
            typer.echo(_output.safe_text(line), err=True)


def sql(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="The SQL, or - to read it from stdin.")],
    env: _ENV = None,
    out: OUT = None,
    file_format: FORMAT = None,
    max_rows: MAX_ROWS = 50,
    timeout: TIMEOUT = None,
    into: _INTO = None,
    mode: _MODE = None,
    incremental_key: _INCREMENTAL_KEY = None,
    evolve_schema: _EVOLVE_SCHEMA = False,
    expire_after: _retention.EXPIRE_AFTER = None,
    break_glass: BREAK_GLASS = False,
    reason: REASON = None,
) -> None:
    """Run SQL and print (or save) the result, or write it into a table.

    With `--into T --mode create --expire-after N` the new table expires N days after its last
    write or read.

    A query that stays queued for 3 seconds prints a line on standard error that says why
    ('Waiting: ...'), and again when the reason changes; the result still goes to standard
    output only. Nothing is printed about waiting with --output json.

    Parameters
    ----------
    ctx
        The Typer context.
    query
        The SQL text, or `-` for stdin.
    env
        The environment.
    out
        Write the full result here instead of printing.
    file_format
        The output file's format.
    max_rows
        The most rows to display.
    timeout
        Seconds to wait for the query.
    into
        Write the result into this table or view.
    mode
        `create`, `replace`, `append` or `view`.
    incremental_key
        With `--mode append`, the incremental key.
    evolve_schema
        Let the target adopt the result's schema.
    expire_after
        `--expire-after`, with `--mode create`.
    break_glass
        `--break-glass`.
    reason
        `--reason`.

    Raises
    ------
    CliError
        If `--break-glass` is given without `--into`, or `--expire-after` without `--mode
        create` or with `--break-glass` (exit 2).
    """
    state = state_of(ctx)
    if expire_after is not None and mode != "create":
        raise CliError(
            "--expire-after needs --mode create: it sets the retention of the table the write "
            "creates. Set an existing table's with `lakana table retention`."
        )
    _check_write_flags(into, mode, incremental_key, evolve_schema, out)
    if break_glass and into is None:
        raise CliError("--break-glass applies only to writes (--into)")
    if expire_after is not None and break_glass:
        raise CliError(
            "--expire-after cannot go with --break-glass: tables in a protected environment "
            "never expire. Leave out --expire-after."
        )
    fmt = out_format(out, file_format)
    text = sys.stdin.read() if query == "-" else query
    environment = resolve_environment(state, env)
    glass = break_glass_reason(break_glass, reason, environment)
    client = state.client()
    if into is not None and mode is not None:
        with _unsubmitted(environment):
            run = client.queries.submit(
                text,
                environment,
                into=into,
                mode=mode,
                incremental_key=incremental_key,
                evolve_schema=evolve_schema,
                break_glass_reason=glass,
            )
        status = _wait(run, environment, timeout, state)
        message = _written(environment, into, mode, status)
        if expire_after is not None:
            message += _retention.set_expiry(
                client, environment, into, expire_after, created=message
            )
        _output.done(state, message, status)
        return
    show_result(
        state,
        environment,
        lambda: client.queries.submit(text, environment),
        out=out,
        fmt=fmt,
        max_rows=max_rows,
        timeout=timeout,
    )


def _now() -> datetime:
    """Return the current time (a seam for the tests of the `age` column).

    Returns
    -------
    datetime
        The current UTC time.
    """
    return datetime.now(UTC)


def _age(entry: QueryListEntry) -> str:
    """Write the time since a query was submitted in its largest whole unit.

    Parameters
    ----------
    entry
        The listed query.

    Returns
    -------
    str
        `12s`, `3m`, `2h` or `1d`; `0s` for a time ahead of this machine's clock.
    """
    seconds = max(0, int((_now() - entry.created_at).total_seconds()))
    for unit, size in (("d", 86_400), ("h", 3_600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _kind(entry: QueryListEntry) -> str:
    """Write what a listed query does: `read`, `append to T`, `maintenance of T`.

    Parameters
    ----------
    entry
        The listed query.

    Returns
    -------
    str
        The kind in words; a kind this CLI does not know is shown as the server sent it.
    """
    if entry.target is None:
        return entry.kind
    target = _output.inline(entry.target)
    prefix = {"append": "append to", "maintain": "maintenance of"}.get(entry.kind, entry.kind)
    return f"{prefix} {target}"


def _waiting(entry: QueryListEntry) -> str:
    """Show the sentence that says why a queued query waits.

    Parameters
    ----------
    entry
        The listed query.

    Returns
    -------
    str
        The reason's message, or nothing.
    """
    return _cut(entry.waiting_for)


@query_app.command("list")
def list_queries(
    ctx: typer.Context,
    env: _ENV = None,
    all_: Annotated[
        bool,
        typer.Option("--all", help="Also the queries that ended in the last 24 hours."),
    ] = False,
    status: Annotated[
        list[str] | None,
        typer.Option(
            "--status",
            help="Only queries in this status: queued, running, succeeded, failed or cancelled. "
            "Repeat the option or separate the values with commas. An ended status lists the "
            "ended queries of the last 24 hours without --all.",
        ),
    ] = None,
    submitter: Annotated[
        str | None,
        typer.Option("--submitter", help="Only the queries this principal submitted."),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=_LIST_MOST, help="Most queries to list.")
    ] = 100,
    cursor: Annotated[
        str | None,
        typer.Option("--cursor", help="Continue after the page that printed this cursor."),
    ] = None,
) -> None:
    """List the queued and running queries of an environment.

    Running queries come first, then queued ones in the order they will be taken, with the
    reason each waits. You see your own queries; with MANAGE_ENVIRONMENT on the environment,
    everyone's, and --submitter narrows them to one principal's. The SQL and the error text are
    not listed: read them with 'lakana query status ID'. A long list prints a "More results"
    line with the command that shows the next page.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        The environment.
    all_
        Add the queries that ended in the last 24 hours, newest first, after the others.
    status
        Status filter.
    submitter
        Submitter filter.
    limit
        Most queries listed (1 to 200).
    cursor
        The cursor of the page to show.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    page = state.client().queries.list_page(
        environment,
        include_ended=all_,
        status=status,
        submitter=submitter,
        limit=limit,
        cursor=cursor,
    )
    if not page.items and state.output != "json":
        where = _output.inline(environment)
        if status or submitter is not None or cursor is not None:
            typer.echo(f"No queries in {where} match.")
        else:
            typer.echo(
                f"No queries in {where} in the last 24 hours."
                if all_
                else f"No queued or running queries in {where}."
            )
        return
    _output.emit(
        state,
        page.items,
        columns=_LIST_COLUMNS,
        droppable=_LIST_DROPPABLE,
        derived={"kind": _kind, "age": _age, "waiting_for": _waiting},
        flexible=["waiting_for"],
    )
    more_results(
        state,
        ["lakana", "query", "list"],
        {
            "--env": environment,
            "--all": all_,
            "--status": status,
            "--submitter": submitter,
            "--limit": limit if limit != 100 else None,
        },
        page.next_cursor,
    )


@query_app.command("submit")
def submit(ctx: typer.Context, query: str, env: _ENV = None) -> None:
    """Submit SQL without waiting; prints the query id.

    Parameters
    ----------
    ctx
        The Typer context.
    query
        The SQL text, or `-` for stdin.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    text = sys.stdin.read() if query == "-" else query
    run = state.client().queries.submit(text, environment)
    _output.done(state, f"Submitted query {run.id}", Submitted(id=run.id, environment=environment))


@query_app.command("status")
def status(ctx: typer.Context, query_id: str, env: _ENV = None) -> None:
    """Show a query's status; for a queued query, waiting_for says why it has not started.

    Parameters
    ----------
    ctx
        The Typer context.
    query_id
        The query id.
    env
        The environment.
    """
    state = state_of(ctx)
    run = state.client().queries.get(query_id, resolve_environment(state, env))
    _output.emit(state, run.status(), columns=_STATUS_COLUMNS, derived={"waiting_for": _reason})


@query_app.command("wait")
def wait(ctx: typer.Context, query_id: str, env: _ENV = None, timeout: TIMEOUT = None) -> None:
    """Wait for a query to finish.

    After 3 seconds in the queue, a line on standard error says why the query waits ('Waiting:
    ...'), and again when the reason changes. Ctrl-C cancels the query, also one of someone
    else's if you hold MANAGE_ENVIRONMENT.

    Parameters
    ----------
    ctx
        The Typer context.
    query_id
        The query id.
    env
        The environment.
    timeout
        Seconds to wait before giving up (exit 9). Ctrl-C cancels the query and exits 130.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    run = state.client().queries.get(query_id, environment)
    _output.emit(
        state,
        _wait(run, environment, timeout, state),
        columns=_STATUS_COLUMNS,
        derived={"waiting_for": _reason},
    )


@query_app.command("cancel")
def cancel(ctx: typer.Context, query_id: str, env: _ENV = None) -> None:
    """Cancel a queued query, or ask the worker to stop a running one.

    Parameters
    ----------
    ctx
        The Typer context.
    query_id
        The query id.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    status = state.client().queries.cancel(query_id, environment)
    _output.done(state, _cancel_message(status, environment), status)


@query_app.command("result")
def result(
    ctx: typer.Context,
    query_id: str,
    env: _ENV = None,
    out: OUT = None,
    file_format: FORMAT = None,
    max_rows: MAX_ROWS = 50,
) -> None:
    """Fetch a finished query's result.

    Parameters
    ----------
    ctx
        The Typer context.
    query_id
        The query id.
    env
        The environment.
    out
        Write the full result here instead of printing.
    file_format
        The output file's format.
    max_rows
        The most rows to display.
    """
    state = state_of(ctx)
    fmt = out_format(out, file_format)
    run = state.client().queries.get(query_id, resolve_environment(state, env))
    _render(state, run.result(), out, fmt, max_rows)


def register(app: typer.Typer) -> None:
    """Add `sql` and the `query` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.command("sql")(sql)
    app.add_typer(query_app, name="query")
