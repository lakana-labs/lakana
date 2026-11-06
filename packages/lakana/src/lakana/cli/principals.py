"""`lakana principal *`: manage users and service accounts."""

from __future__ import annotations

import shlex
from enum import StrEnum
from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._override import OVERRIDE_MANAGED, OVERRIDE_REASON, override_reason
from lakana.cli._paging import CURSOR, LIMIT, SEARCH, more_results
from lakana.cli._state import confirm, state_of

principal_app = typer.Typer(help="Manage principals.", no_args_is_help=True)


class PrincipalType(StrEnum):
    """The values `--type` accepts."""

    user = "user"
    service_account = "service-account"


@principal_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    principal_type: Annotated[
        PrincipalType, typer.Option("--type", help="user or service-account")
    ],
    override_managed: OVERRIDE_MANAGED = False,
    reason: OVERRIDE_REASON = None,
) -> None:
    """Create principal NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The principal name.
    principal_type
        User or service account.
    override_managed
        `--override-managed`: create a service account although an access file manages its scope.
    reason
        `--reason`: why you create a service account anyway.
    """
    why = override_reason(override_managed, reason)
    state = state_of(ctx)
    api_type = principal_type.value.upper().replace("-", "_")
    principal = state.client().principals.create(name, api_type, override_reason=why)
    who = shlex.quote(_output.inline(name))
    _output.done(
        state,
        f"Created {principal_type.value} {_output.inline(name)}\n"
        "It can do nothing yet. Next:\n"
        f"  lakana grant create --principal {who} --role ANALYST --kind ENVIRONMENT --env ENV\n"
        f"  lakana token issue {who} NAME",
        principal,
    )


@principal_app.command("list")
def list_principals(
    ctx: typer.Context, search: SEARCH = None, limit: LIMIT = None, cursor: CURSOR = None
) -> None:
    """List principals, newest first.

    `--search TEXT` lists those whose name contains TEXT, in any letter case. A long list
    prints a "More results" line with the command that shows the next page.

    Parameters
    ----------
    ctx
        The Typer context.
    search
        Only names that contain this text.
    limit
        Most principals returned (1 to 200); the server's default when omitted.
    cursor
        The cursor of the page to show.
    """
    state = state_of(ctx)
    page = state.client().principals.list_page(search=search, limit=limit, cursor=cursor)
    _output.emit(state, page.items)
    more_results(
        state,
        ["lakana", "principal", "list"],
        {"--search": search, "--limit": limit},
        page.next_cursor,
    )


@principal_app.command("get")
def get(ctx: typer.Context, name: str) -> None:
    """Show principal NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The principal name.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().principals.get(name))


@principal_app.command("deactivate")
def deactivate(
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
    override_managed: OVERRIDE_MANAGED = False,
    reason: OVERRIDE_REASON = None,
) -> None:
    """Deactivate principal NAME; its credentials stop working.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The principal name.
    yes
        Skip the confirmation prompt.
    override_managed
        `--override-managed`: deactivate a service account although an access file manages it.
    reason
        `--reason`: why you deactivate a service account anyway.
    """
    why = override_reason(override_managed, reason)
    state = state_of(ctx)
    confirm(state, f"Deactivate principal {name!r}? Its tokens stop working.", yes)
    state.client().principals.deactivate(name, override_reason=why)
    _output.done(state, f"Deactivated principal {_output.inline(name)}")


def register(app: typer.Typer) -> None:
    """Add the `principal` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(principal_app, name="principal")
