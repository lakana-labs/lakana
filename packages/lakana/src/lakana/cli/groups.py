"""`lakana group *`: manage groups and their members."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._override import OVERRIDE_MANAGED, OVERRIDE_REASON, override_reason
from lakana.cli._paging import CURSOR, LIMIT, SEARCH, more_results
from lakana.cli._state import confirm, state_of

group_app = typer.Typer(help="Manage groups.", no_args_is_help=True)

_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]


@group_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    override_managed: OVERRIDE_MANAGED = False,
    reason: OVERRIDE_REASON = None,
) -> None:
    """Create group NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The group name.
    override_managed
        `--override-managed`: create the group although an access file manages its scope.
    reason
        `--reason`: why you create the group anyway.
    """
    why = override_reason(override_managed, reason)
    state = state_of(ctx)
    _output.done(
        state,
        f"Created group {_output.inline(name)}",
        state.client().groups.create(name, override_reason=why),
    )


@group_app.command("list")
def list_groups(
    ctx: typer.Context, search: SEARCH = None, limit: LIMIT = None, cursor: CURSOR = None
) -> None:
    """List groups, newest first.

    `--search TEXT` lists those whose name contains TEXT, in any letter case. A long list
    prints a "More results" line with the command that shows the next page.

    Parameters
    ----------
    ctx
        The Typer context.
    search
        Only names that contain this text.
    limit
        Most groups returned (1 to 200); the server's default when omitted.
    cursor
        The cursor of the page to show.
    """
    state = state_of(ctx)
    page = state.client().groups.list_page(search=search, limit=limit, cursor=cursor)
    _output.emit(state, page.items)
    more_results(
        state,
        ["lakana", "group", "list"],
        {"--search": search, "--limit": limit},
        page.next_cursor,
    )


@group_app.command("delete")
def delete(
    ctx: typer.Context,
    name: str,
    yes: _YES = False,
    override_managed: OVERRIDE_MANAGED = False,
    reason: OVERRIDE_REASON = None,
) -> None:
    """Delete group NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The group name.
    yes
        Skip the confirmation prompt.
    override_managed
        `--override-managed`: delete the group although an access file manages its scope.
    reason
        `--reason`: why you delete the group anyway.
    """
    why = override_reason(override_managed, reason)
    state = state_of(ctx)
    confirm(state, f"Delete group {name!r}?", yes)
    state.client().groups.delete(name, override_reason=why)
    _output.done(state, f"Deleted group {_output.inline(name)}")


@group_app.command("members")
def members(ctx: typer.Context, name: str) -> None:
    """List group NAME's members.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The group name.
    """
    state = state_of(ctx)
    _output.emit(state, state.client().groups.list_members(name))


@group_app.command("add-member")
def add_member(
    ctx: typer.Context,
    name: str,
    principal: str,
    override_managed: OVERRIDE_MANAGED = False,
    reason: OVERRIDE_REASON = None,
) -> None:
    """Add PRINCIPAL to group NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The group name.
    principal
        The principal name.
    override_managed
        `--override-managed`: add the member although an access file manages its scope.
    reason
        `--reason`: why you add the member anyway.
    """
    why = override_reason(override_managed, reason)
    state = state_of(ctx)
    state.client().groups.add_member(name, principal, override_reason=why)
    _output.done(state, f"Added {_output.inline(principal)} to group {_output.inline(name)}")


@group_app.command("remove-member")
def remove_member(
    ctx: typer.Context,
    name: str,
    principal: str,
    yes: _YES = False,
    override_managed: OVERRIDE_MANAGED = False,
    reason: OVERRIDE_REASON = None,
) -> None:
    """Remove PRINCIPAL from group NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The group name.
    principal
        The principal name.
    yes
        Skip the confirmation prompt.
    override_managed
        `--override-managed`: remove the member although an access file manages its scope.
    reason
        `--reason`: why you remove the member anyway.
    """
    why = override_reason(override_managed, reason)
    state = state_of(ctx)
    confirm(state, f"Remove {principal!r} from group {name!r}?", yes)
    state.client().groups.remove_member(name, principal, override_reason=why)
    _output.done(state, f"Removed {_output.inline(principal)} from group {_output.inline(name)}")


def register(app: typer.Typer) -> None:
    """Add the `group` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(group_app, name="group")
