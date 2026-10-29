"""`lakana check list`: the results of the data quality checks a run made."""

from __future__ import annotations

from itertools import islice
from typing import Annotated

import typer

from lakana._models import CheckHealth, CheckResult
from lakana.cli import _output
from lakana.cli._state import resolve_environment, state_of
from lakana.cli.queries import FORMAT, MAX_ROWS, OUT, TIMEOUT, out_format, show_result

check_app = typer.Typer(
    help="Data quality checks: the results of the checks a run made.", no_args_is_help=True
)

_COLUMNS = ["run", "table", "check", "outcome", "rows", "on_failure", "finished_at"]
_DROPPABLE = ["on_failure", "run"]
"""What `check list` leaves out first on a narrow terminal: the check, its outcome and when stay."""
_ID_SHOWN = 8
"""How many characters of a run id the `run` column shows."""
_MAX_PAGE = 200
"""The most results the server sends in one page."""
_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]


def _run_shown(result: CheckResult) -> str:
    """Cut a result's run id to the length the `run` column shows.

    Parameters
    ----------
    result
        The listed result.

    Returns
    -------
    str
        The first characters of the run id; `--output json` has it whole.
    """
    return result.run_id[:_ID_SHOWN]


def check_shown(result: CheckResult | CheckHealth) -> str:
    """Name a check as the CLI shows it.

    Parameters
    ----------
    result
        A listed result, or a check of a table's health.

    Returns
    -------
    str
        Its name, with ``(generated)`` after a check from the asset's header.
    """
    return f"{result.check} (generated)" if result.generated else result.check


@check_app.command("list")
def list_checks(
    ctx: typer.Context,
    env: _ENV = None,
    run: Annotated[str | None, typer.Option("--run", help="Only the results of this run.")] = None,
    table: Annotated[
        str | None, typer.Option("--table", help="Only this table's (namespace.table).")
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=1000, help="Most results to list.")
    ] = 50,
) -> None:
    """List check results, newest first. With --run, the results of one run.

    The run column shows 8 characters of the run id; --output json has the whole id.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        Environment.
    run
        Only the results of this run.
    table
        Only the results for this table.
    limit
        Most results listed.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    checks = state.client().checks
    if run is not None:
        found = [r for r in checks.for_run(environment, run) if table is None or r.table == table]
        results = found[:limit]
    else:
        results = list(
            islice(checks.iter(environment, table=table, page_size=min(limit, _MAX_PAGE)), limit)
        )
    if not results and state.output != "json":
        if run is not None:
            typer.echo(f"Run {_output.inline(run)} has no check results.")
        else:
            typer.echo(f"No check results in {_output.inline(environment)}.")
        return
    _output.emit(
        state,
        results,
        columns=_COLUMNS,
        droppable=_DROPPABLE,
        derived={"run": _run_shown, "check": check_shown, "rows": lambda r: r.failing_rows},
        whole=["rows"],
    )


@check_app.command("rows")
def rows(
    ctx: typer.Context,
    run: Annotated[
        str, typer.Argument(metavar="RUN", help="The whole run id, as `lakana runs list` shows it.")
    ],
    check: Annotated[
        str,
        typer.Argument(
            metavar="CHECK", help="The check, as `lakana check list` shows it: table/check."
        ),
    ],
    env: _ENV = None,
    out: OUT = None,
    file_format: FORMAT = None,
    max_rows: MAX_ROWS = 50,
    timeout: TIMEOUT = None,
) -> None:
    """Show the rows a check found in a run.

    These are the rows this check found in that run, as they were then, also after the table
    was set back.

    It runs as a query of yours, under your own rights, row filters and column masks: you see
    the rows the check found that you may read, and every other table the check's SQL reads is
    read as it is now. It needs the right to read the run and to submit queries, and the right
    to read every table the check's SQL reads: without it, the command refuses and shows neither
    rows nor SQL.

    The rows stay readable for seven days after a failed blocking check; then `lakana table
    maintain` removes the snapshot, and the number of rows remains in `lakana check list`. A
    check that read no snapshot (a view, or SQL that does not read its table) has no rows to
    show; the message says where to run its SQL yourself.

    Prints a table like `lakana sql`; --out writes every row to a file instead.

    Parameters
    ----------
    ctx
        The Typer context.
    run
        The run.
    check
        The check, ``<namespace>.<table>/<check>``.
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
    """
    state = state_of(ctx)
    fmt = out_format(out, file_format)
    environment = resolve_environment(state, env)
    checks = state.client().checks
    show_result(
        state,
        environment,
        lambda: checks.rows(run, check, environment=environment),
        out=out,
        fmt=fmt,
        max_rows=max_rows,
        timeout=timeout,
    )


def register(app: typer.Typer) -> None:
    """Add the `check` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(check_app, name="check")
