"""`lakana row-filter *` and `lakana column-mask *`."""

from __future__ import annotations

from typing import Annotated, Protocol

import typer

from lakana.cli import _output
from lakana.cli._state import confirm, resolve_environment, split_table_name, state_of
from lakana.cli.grants import exactly_one, subject_of

row_filter_app = typer.Typer(help="Manage row filters.", no_args_is_help=True)
column_mask_app = typer.Typer(help="Manage column masks.", no_args_is_help=True)

_PRINCIPAL = Annotated[str | None, typer.Option("--principal", help="Principal name.")]
_GROUP = Annotated[str | None, typer.Option("--group", help="Group name.")]
_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_FILTER_ENV = Annotated[str | None, typer.Option("--env", help="Only this environment.")]
_EXPRESSION = Annotated[str, typer.Option("--expression", help="Restricted SQL expression.")]
_YES = Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")]
_COLUMNS = ["id", "subject", "environment", "namespace", "table", "expression", "bound"]
_DROPPABLE = ["environment", "id", "namespace", "bound"]
"""What a policy listing leaves out first on a narrow terminal; subject and table stay.

`bound` goes last: it only matters for a policy of an earlier release that was not bound.
"""


class _HasBound(Protocol):
    """What says whether a policy follows its columns."""

    @property
    def bound(self) -> bool | None:
        """Whether the policy is bound; `None` from a server before this release."""


def _yes_no(item: _HasBound) -> str:
    """Say whether a policy follows its columns.

    Parameters
    ----------
    item
        A row filter or column mask.

    Returns
    -------
    str
        `yes` or `no`; a server before this release does not say, shown as `?`.
    """
    return "?" if item.bound is None else "yes" if item.bound else "no"


@row_filter_app.command("create")
def create_row_filter(
    ctx: typer.Context,
    table: str,
    expression: _EXPRESSION,
    principal: _PRINCIPAL = None,
    group: _GROUP = None,
    env: _ENV = None,
) -> None:
    """Restrict which rows of TABLE (namespace.table) a principal or group sees.

    Parameters
    ----------
    ctx
        The Typer context.
    table
        The qualified table name.
    expression
        The filter expression.
    principal
        The principal it applies to.
    group
        The group it applies to.
    env
        The environment.
    """
    state = state_of(ctx)
    exactly_one(principal, group, "--principal or --group")
    namespace, name = split_table_name(table)
    environment = resolve_environment(state, env)
    row_filter = state.client().row_filters.create(
        principal=principal,
        group=group,
        environment=environment,
        namespace=namespace,
        table=name,
        expression=expression,
    )
    _output.done(
        state,
        f"Created row filter {row_filter.id} on "
        f"{_output.inline(environment)}/{_output.inline(table)}",
        row_filter,
    )


@row_filter_app.command("list")
def list_row_filters(
    ctx: typer.Context, principal: _PRINCIPAL = None, group: _GROUP = None, env: _FILTER_ENV = None
) -> None:
    """List active row filters, optionally filtered.

    The table names who each row filter is for in `subject`. When the terminal is too narrow it
    leaves out `environment`, `id`, `namespace` and `bound`, in that order; `--output json`
    always has every field.

    Parameters
    ----------
    ctx
        The Typer context.
    principal
        Only filters for this principal.
    group
        Only filters for this group.
    env
        Only filters in this environment.
    """
    state = state_of(ctx)
    filters = state.client().row_filters.list(
        principal_name=principal, group_name=group, environment=env
    )
    _output.emit(
        state,
        filters,
        columns=_COLUMNS,
        droppable=_DROPPABLE,
        derived={"subject": subject_of, "bound": _yes_no},
        whole=["subject", "bound"],
    )


@row_filter_app.command("revoke")
def revoke_row_filter(ctx: typer.Context, row_filter_id: str, yes: _YES = False) -> None:
    """Revoke row filter ROW_FILTER_ID.

    Take the id from `row-filter list`; on a narrow terminal the table leaves it out, so widen the
    terminal or use `--output json`.

    Parameters
    ----------
    ctx
        The Typer context.
    row_filter_id
        The filter id.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Revoke row filter {row_filter_id}?", yes)
    state.client().row_filters.revoke(row_filter_id)
    _output.done(state, f"Revoked row filter {row_filter_id}")


@column_mask_app.command("create")
def create_column_mask(
    ctx: typer.Context,
    table: str,
    column: str,
    expression: _EXPRESSION,
    principal: _PRINCIPAL = None,
    group: _GROUP = None,
    env: _ENV = None,
) -> None:
    """Mask COLUMN of TABLE (namespace.table) for a principal or group.

    Parameters
    ----------
    ctx
        The Typer context.
    table
        The qualified table name.
    column
        The column to mask.
    expression
        The replacement expression.
    principal
        The principal it applies to.
    group
        The group it applies to.
    env
        The environment.
    """
    state = state_of(ctx)
    exactly_one(principal, group, "--principal or --group")
    namespace, name = split_table_name(table)
    environment = resolve_environment(state, env)
    mask = state.client().column_masks.create(
        principal=principal,
        group=group,
        environment=environment,
        namespace=namespace,
        table=name,
        column=column,
        expression=expression,
    )
    _output.done(
        state,
        f"Created column mask {mask.id} on "
        f"{_output.inline(environment)}/{_output.inline(table)}.{_output.inline(column)}",
        mask,
    )


@column_mask_app.command("list")
def list_column_masks(
    ctx: typer.Context, principal: _PRINCIPAL = None, group: _GROUP = None, env: _FILTER_ENV = None
) -> None:
    """List active column masks, optionally filtered.

    The table names who each column mask is for in `subject`. When the terminal is too narrow it
    leaves out `environment`, `id`, `namespace` and `bound`, in that order; `--output json`
    always has every field.

    Parameters
    ----------
    ctx
        The Typer context.
    principal
        Only masks for this principal.
    group
        Only masks for this group.
    env
        Only masks in this environment.
    """
    state = state_of(ctx)
    masks = state.client().column_masks.list(
        principal_name=principal, group_name=group, environment=env
    )
    _output.emit(
        state,
        masks,
        columns=[*_COLUMNS[:5], "column", "expression", "bound"],
        droppable=[*_DROPPABLE, "expression"],
        derived={"subject": subject_of, "bound": _yes_no},
        whole=["subject", "bound"],
    )


@column_mask_app.command("revoke")
def revoke_column_mask(ctx: typer.Context, column_mask_id: str, yes: _YES = False) -> None:
    """Revoke column mask COLUMN_MASK_ID.

    Take the id from `column-mask list`; on a narrow terminal the table leaves it out, so widen the
    terminal or use `--output json`.

    Parameters
    ----------
    ctx
        The Typer context.
    column_mask_id
        The mask id.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    confirm(state, f"Revoke column mask {column_mask_id}?", yes)
    state.client().column_masks.revoke(column_mask_id)
    _output.done(state, f"Revoked column mask {column_mask_id}")


def register(app: typer.Typer) -> None:
    """Add the `row-filter` and `column-mask` groups to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(row_filter_app, name="row-filter")
    app.add_typer(column_mask_app, name="column-mask")
