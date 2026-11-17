"""`lakana token *` (personal access tokens) and `lakana task-token *`."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._paging import CURSOR, LIMIT, more_results
from lakana.cli._state import CliError, confirm, resolve_environment, state_of

token_app = typer.Typer(help="Manage personal access tokens.", no_args_is_help=True)
task_token_app = typer.Typer(help="Mint and revoke task tokens.", no_args_is_help=True)

_DROPPABLE = ("revoked_at", "expires_at", "created_at")
"""The columns of a token list left out first, when the terminal is too narrow for all of them."""

_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]


@token_app.command("issue")
def issue(ctx: typer.Context, principal: str, name: str) -> None:
    """Issue a personal access token named NAME for PRINCIPAL; the token is shown once.

    Parameters
    ----------
    ctx
        The Typer context.
    principal
        The principal the token authenticates as.
    name
        A label for the token.
    """
    state = state_of(ctx)
    issued = state.client().tokens.issue(principal, name)
    if state.output == "json":
        _output.print_json(issued)
    else:
        _output.done(
            state,
            f"Issued token {issued.key_id} ({_output.inline(name)}) "
            f"for {_output.inline(principal)}",
        )
    _output.print_secret(state, "Token", issued.token)
    _output.next_step(
        state,
        f"Give this token to {_output.inline(principal)} over a trusted channel; "
        "they run `lakana login`.",
    )


@token_app.command("list")
def list_tokens(
    ctx: typer.Context,
    principal: Annotated[
        str | None, typer.Argument(help="Whose tokens to list; yours when left out.")
    ] = None,
    all_owners: Annotated[
        bool,
        typer.Option(
            "--all", help="List every owner's live tokens, with an owner column (admin only)."
        ),
    ] = False,
    limit: LIMIT = None,
    cursor: CURSOR = None,
) -> None:
    """List your tokens, or PRINCIPAL's (never their secrets).

    Without a name, your own. `--all` lists the live tokens of every owner, newest first, with
    an owner column; it needs the ADMIN role and prints a "More results" line with the command
    that shows the next page.

    Parameters
    ----------
    ctx
        The Typer context.
    principal
        The principal; yourself when left out.
    all_owners
        `--all`: every owner's live tokens.
    limit
        Most tokens returned (1 to 200) with `--all`; the server's default when omitted.
    cursor
        The cursor of the page to show with `--all`.

    Raises
    ------
    CliError
        With exit code 2, if `--all` is given with a name, or `--limit` or `--cursor` without
        `--all`.
    """
    if all_owners and principal is not None:
        raise CliError("--all lists every owner's tokens; leave out PRINCIPAL.")
    if not all_owners and (limit is not None or cursor is not None):
        raise CliError("--limit and --cursor page the --all list.")
    state = state_of(ctx)
    client = state.client()
    if all_owners:
        page = client.tokens.list_all_page(limit=limit, cursor=cursor)
        _output.emit(state, page.items, droppable=_DROPPABLE)
        more_results(
            state, ["lakana", "token", "list", "--all"], {"--limit": limit}, page.next_cursor
        )
        return
    who = principal if principal is not None else client.whoami().name
    _output.emit(state, client.tokens.list(who), droppable=_DROPPABLE)


@token_app.command("revoke")
def revoke(ctx: typer.Context, principal: str, key_id: str, yes: _YES = False) -> None:
    """Revoke PRINCIPAL's token KEY_ID.

    Parameters
    ----------
    ctx
        The Typer context.
    principal
        The principal.
    key_id
        The token's key id.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Revoke token {key_id} of {principal!r}?", yes)
    state.client().tokens.revoke(principal, key_id)
    _output.done(state, f"Revoked token {key_id}")


@task_token_app.command("mint")
def mint(
    ctx: typer.Context,
    principal: str,
    scope: Annotated[
        list[str], typer.Option("--scope", help="Asset pattern, e.g. bronze.* (repeatable).")
    ],
    ttl: Annotated[int, typer.Option("--ttl", min=1, help="Lifetime in seconds.")],
    env: Annotated[
        str | None, typer.Option("--env", help="Environment (default: the profile's).")
    ] = None,
) -> None:
    """Mint a task token for PRINCIPAL; the token is shown once.

    PRINCIPAL is your own name, unless you are an instance admin.

    Parameters
    ----------
    ctx
        The Typer context.
    principal
        The principal the token authenticates as. Your own name, unless you
        are an instance admin.
    scope
        The asset patterns it may touch.
    ttl
        Its lifetime in seconds.
    env
        The one environment it may touch.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    minted = state.client().task_tokens.mint(principal, environment, scope, ttl)
    if state.output == "json":
        _output.print_json(minted)
    else:
        _output.done(
            state,
            f"Minted task token {minted.key_id} for {_output.inline(principal)} "
            f"in {_output.inline(environment)}",
        )
    _output.print_secret(state, "Token", minted.token)


@task_token_app.command("revoke")
def revoke_task_token(ctx: typer.Context, key_id: str, yes: _YES = False) -> None:
    """Revoke task token KEY_ID.

    Parameters
    ----------
    ctx
        The Typer context.
    key_id
        The task token's key id.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Revoke task token {key_id}?", yes)
    state.client().task_tokens.revoke(key_id)
    _output.done(state, f"Revoked task token {key_id}")


def register(app: typer.Typer) -> None:
    """Add the `token` and `task-token` groups to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(token_app, name="token")
    app.add_typer(task_token_app, name="task-token")
