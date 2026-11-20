"""`lakana query saved *`: keep SQL under a name, privately or shared with an environment."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from lakana._models import SavedQuery
from lakana.cli import _output
from lakana.cli._paging import more_results
from lakana.cli._state import CliError, confirm, resolve_environment, state_of

saved_app = typer.Typer(
    help="Save queries under a name, keep them private or share them with an environment.",
    no_args_is_help=True,
)

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]
_LIST_MOST = 200
"""The most rows the server returns; the `--limit` bound."""
_LIST_COLUMNS = ["id", "name", "visibility", "owner"]
_ALL_COLUMNS = ["id", "name", "visibility", "owner", "environment"]
"""The columns of `query saved list --all-environments`: the environment comes last."""
_SHOW_COLUMNS = [
    "id",
    "environment",
    "name",
    "visibility",
    "owner",
    "actions",
    "created_at",
    "updated_at",
]
_NO_SQL = "You may not read the SQL of this query."
_HIDDEN_IN_SQL = (
    "This saved query holds control or invisible characters; read it with --output json."
)
"""Why `show --sql` prints nothing: printing the text exactly could drive the terminal, and
escaping it would hand `lakana sql -` another query."""


def _owner(saved: SavedQuery) -> str:
    """Show who owns a saved query.

    Parameters
    ----------
    saved
        The saved query.

    Returns
    -------
    str
        The owner's name, or ``former owner`` once the owner's principal is deleted.
    """
    return "former owner" if saved.former_owner else _output.inline(saved.owner or "")


def _actions(saved: SavedQuery) -> str:
    """Show what the caller may do with a saved query.

    Parameters
    ----------
    saved
        The saved query.

    Returns
    -------
    str
        The actions, comma separated; ``none`` when there are none.
    """
    return ", ".join(saved.actions) or "none"


def _sql_text(query: str) -> str:
    """Read the SQL argument: the text itself, or stdin for ``-``.

    Parameters
    ----------
    query
        The SQL, or ``-``.

    Returns
    -------
    str
        The SQL text.
    """
    return sys.stdin.read() if query == "-" else query


@saved_app.command("create")
def create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The name: one line, unique among your queries.")],
    query: Annotated[str, typer.Argument(help="The SQL, or - to read it from stdin.")],
    env: _ENV = None,
    everyone: Annotated[
        bool,
        typer.Option(
            "--everyone", help="Share it with everyone who may run queries in the environment."
        ),
    ] = False,
) -> None:
    """Save a query under NAME; it is private unless you pass --everyone.

    Saving does not run the query. Whoever runs it later does so with their own rights.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The name.
    query
        The SQL text, or `-` for stdin.
    env
        The environment.
    everyone
        Share the saved query with everyone who may run queries in the environment.
    """
    state = state_of(ctx)
    saved = state.client().saved_queries.create(
        resolve_environment(state, env),
        name,
        _sql_text(query),
        visibility="everyone" if everyone else "private",
    )
    _output.done(state, f"Saved query {_output.inline(saved.name)} ({saved.id})", saved)


@saved_app.command("list")
def list_saved(
    ctx: typer.Context,
    env: _ENV = None,
    all_environments: Annotated[
        bool,
        typer.Option(
            "--all-environments",
            help="The saved queries of every environment you may query, with an environment "
            "column.",
        ),
    ] = False,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=_LIST_MOST, help="Most saved queries to list.")
    ] = 50,
    cursor: Annotated[
        str | None,
        typer.Option("--cursor", help="Continue after the page that printed this cursor."),
    ] = None,
) -> None:
    """List the saved queries you see in an environment, by name.

    You see your own, and those shared with everyone who may run queries in the
    environment. The SQL is not listed: read it with 'lakana query saved show ID'.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        The environment.
    all_environments
        List every environment instead of one; not with `--env`.
    limit
        Most saved queries listed (1 to 200).
    cursor
        The cursor of the page to show.

    Raises
    ------
    CliError
        If `--all-environments` is given with `--env`.
    """
    state = state_of(ctx)
    if all_environments and env is not None:
        raise CliError("--all-environments lists every environment; leave out --env.")
    environment = None if all_environments else resolve_environment(state, env)
    page = state.client().saved_queries.list(environment, limit=limit, cursor=cursor)
    if not page.items and state.output != "json":
        where = "any environment" if environment is None else _output.inline(environment)
        typer.echo(f"No saved queries in {where}.")
        return
    _output.emit(
        state,
        page.items,
        columns=_ALL_COLUMNS if all_environments else _LIST_COLUMNS,
        derived={"owner": _owner},
    )
    more_results(
        state,
        ["lakana", "query", "saved", "list"],
        {
            "--env": environment,
            "--all-environments": all_environments,
            "--limit": limit if limit != 50 else None,
        },
        page.next_cursor,
    )


@saved_app.command("show")
def show(
    ctx: typer.Context,
    saved_query_id: Annotated[str, typer.Argument(metavar="ID", help="The saved query's id.")],
    env: _ENV = None,
    sql: Annotated[
        bool, typer.Option("--sql", help="Print only the SQL, to pipe into 'lakana sql -'.")
    ] = False,
) -> None:
    """Show a saved query: its fields, then its SQL.

    With --sql only the SQL is printed, so 'lakana query saved show ID --sql | lakana sql -'
    runs it. If the SQL holds control or invisible characters (a CRLF line ending is not
    one), --sql prints nothing and exits 1, as printing it escaped would run another query;
    read it with --output json, where --sql prints only {"sql": ...}. An administrator may
    not read the SQL of someone else's private query without READ_QUERY_TEXT; the command
    then says so.

    Parameters
    ----------
    ctx
        The Typer context.
    saved_query_id
        The saved query's id.
    env
        The environment.
    sql
        Print only the SQL.

    Raises
    ------
    CliError
        With `--sql`, if the SQL may not be read, or holds a control or invisible character
        (exit 1).
    """
    state = state_of(ctx)
    saved = state.client().saved_queries.get(resolve_environment(state, env), saved_query_id)
    if sql:
        if saved.sql is None:
            raise CliError(_NO_SQL, exit_code=1, kind="PermissionDenied")
        if state.output == "json":
            # JSON escapes every control character, so the text goes out exactly as stored.
            _output.print_json({"sql": saved.sql})
            return
        plain = saved.sql.replace("\r\n", "\n")  # a CR in a CRLF pair is ordinary text
        if _output.safe_source(plain) != plain:
            raise CliError(_HIDDEN_IN_SQL, exit_code=1, kind="ValidationError")
        sys.stdout.write(saved.sql if saved.sql.endswith("\n") else saved.sql + "\n")
        return
    _output.emit(
        state,
        saved,
        columns=_SHOW_COLUMNS,
        derived={"owner": _owner, "actions": _actions},
    )
    if state.output == "json":
        return
    if saved.sql is None:
        typer.echo(_NO_SQL)
    else:
        sys.stdout.write("\n" + _output.safe_source(saved.sql.rstrip("\n")) + "\n")


@saved_app.command("update")
def update(
    ctx: typer.Context,
    saved_query_id: Annotated[str, typer.Argument(metavar="ID", help="The saved query's id.")],
    env: _ENV = None,
    name: Annotated[str | None, typer.Option("--name", help="The new name.")] = None,
    sql: Annotated[
        str | None, typer.Option("--sql", help="The new SQL, or - to read it from stdin.")
    ] = None,
    private: Annotated[bool, typer.Option("--private", help="Make it private.")] = False,
    everyone: Annotated[
        bool, typer.Option("--everyone", help="Share it with everyone who may run queries.")
    ] = False,
) -> None:
    """Change the name, the SQL or the visibility of a saved query you own.

    The command reads the saved query first and sends every field, so what you leave out
    stays as it is.

    Parameters
    ----------
    ctx
        The Typer context.
    saved_query_id
        The saved query's id.
    env
        The environment.
    name
        The new name.
    sql
        The new SQL text, or `-` for stdin.
    private
        Make the saved query private.
    everyone
        Share the saved query with everyone who may run queries in the environment.

    Raises
    ------
    CliError
        If `--private` and `--everyone` are both given, or nothing is to change.
    """
    if private and everyone:
        raise CliError("--private and --everyone exclude each other.")
    if name is None and sql is None and not private and not everyone:
        raise CliError("Nothing to change: pass --name, --sql, --private or --everyone.")
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    saved_queries = state.client().saved_queries
    current = saved_queries.get(environment, saved_query_id)
    new_sql = current.sql if sql is None else _sql_text(sql)
    if new_sql is None:
        raise CliError(_NO_SQL, exit_code=1, kind="PermissionDenied")
    changed = saved_queries.update(
        environment,
        saved_query_id,
        name=current.name if name is None else name,
        sql=new_sql,
        visibility="everyone" if everyone else "private" if private else current.visibility,
    )
    _output.done(
        state, f"Updated saved query {_output.inline(changed.name)} ({changed.id})", changed
    )


@saved_app.command("delete")
def delete(
    ctx: typer.Context,
    saved_query_id: Annotated[str, typer.Argument(metavar="ID", help="The saved query's id.")],
    env: _ENV = None,
    yes: _YES = False,
) -> None:
    """Delete a saved query: its owner may, and so may an administrator.

    Parameters
    ----------
    ctx
        The Typer context.
    saved_query_id
        The saved query's id.
    env
        The environment.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    confirm(
        state,
        f"Delete saved query {_output.inline(saved_query_id)} in {_output.inline(environment)}?",
        yes,
    )
    state.client().saved_queries.delete(environment, saved_query_id)
    _output.done(state, f"Deleted saved query {_output.inline(saved_query_id)}")
