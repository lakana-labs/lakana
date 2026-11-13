"""`lakana grant *`: grant, list and revoke privileges."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from typing import Annotated, Protocol

import typer

from lakana._models import Grant, SubjectPrincipal
from lakana.cli import _output
from lakana.cli._override import OVERRIDE_MANAGED, OVERRIDE_REASON, override_reason
from lakana.cli._paging import CURSOR, LIMIT, more_results
from lakana.cli._state import CliError, CliState, confirm, state_of

grant_app = typer.Typer(help="Manage grants.", no_args_is_help=True)

_PRINCIPAL = Annotated[str | None, typer.Option("--principal", help="Principal name.")]
_GROUP = Annotated[str | None, typer.Option("--group", help="Group name.")]
_ENV = Annotated[str | None, typer.Option("--env", help="Environment.")]
_NS = Annotated[str | None, typer.Option("--ns", help="Namespace (dotted).")]
_TABLE = Annotated[str | None, typer.Option("--table", help="Table name.")]


class HasScope(Protocol):
    """What names the object a grant or a permission is on."""

    @property
    def kind(self) -> str:
        """``INSTANCE``, ``ENVIRONMENT``, ``NAMESPACE`` or ``TABLE``."""

    @property
    def environment(self) -> str | None:
        """The environment, below the instance."""

    @property
    def namespace(self) -> str | None:
        """The dot-joined namespace, for a namespace or a table."""

    @property
    def table(self) -> str | None:
        """The table, for a table."""


class _HasSubject(Protocol):
    """What names who a grant or a policy is for."""

    @property
    def principal(self) -> SubjectPrincipal | None:
        """The principal's name and type, or `None` when the subject is a group or unnamed."""

    @property
    def group(self) -> str | None:
        """The group's name, or `None` when the subject is a principal or unnamed."""

    @property
    def principal_id(self) -> str | None:
        """The principal's id, or `None` when the subject is a group."""

    @property
    def group_id(self) -> str | None:
        """The group's id, or `None` when the subject is a principal."""


_NO_EFFECT_LINE = (
    "no effect: the grant names an action that works only with the ADMIN role on the instance. "
    "Revoke it with `lakana grant revoke ID`."
)


_MAX_REVOKE_LINES = 5
"""The most commands written out under the table; `--output json` has every grant."""


def _revoke_lines(grants: Sequence[Grant], subject: Callable[[Grant], str]) -> list[str]:
    """Write the command that revokes each grant without effect.

    The table leaves `id` out on a narrow terminal, so the line under it gives the
    whole id. A note naming the action and who the grant is for comes first, so that
    the command is alone on its line and can be copied as it is.

    Parameters
    ----------
    grants
        The listed grants.
    subject
        Says who a grant is for.

    Returns
    -------
    list of str
        Two lines per grant the server sent a note for, at most `_MAX_REVOKE_LINES`
        grants (the note, then the indented command), then a line that counts the rest;
        what the server sent is shown as text.
    """
    stale = [grant for grant in grants if grant.note]
    lines: list[str] = []
    for grant in stale[:_MAX_REVOKE_LINES]:
        what = _output.inline(grant.action or grant.role or "")
        lines.append(f"{what}, {_output.inline(subject(grant))}:")
        lines.append(f"  lakana grant revoke {shlex.quote(_output.inline(grant.id))}")
    if len(stale) > _MAX_REVOKE_LINES:
        lines.append(f"and {len(stale) - _MAX_REVOKE_LINES} more: use --output json")
    return lines


def _marker(grant: Grant) -> str:
    """Say in a table cell that a grant has no effect.

    Parameters
    ----------
    grant
        A listed grant.

    Returns
    -------
    str
        ``no effect`` when the server sent a note, else an empty cell.
    """
    return "no effect" if grant.note else ""


def securable_of(grant: HasScope) -> str:
    """Say what a grant (or a permission) is on, naming the environment.

    Parameters
    ----------
    grant
        A grant, or the scope of a permission.

    Returns
    -------
    str
        `the instance`, `environment dev`, `namespace gold.sales in environment dev` or
        `table gold.sales.orders in environment dev`.
    """
    if grant.kind == "INSTANCE":
        return "the instance"
    where = f"environment {grant.environment}"
    if grant.kind == "ENVIRONMENT":
        return where
    if grant.kind == "NAMESPACE":
        return f"namespace {grant.namespace} in {where}"
    return f"table {grant.namespace}.{grant.table} in {where}"


def _warn_about_unnamed(state: CliState, grants: Sequence[Grant]) -> None:
    """Say, on stderr, that some grants show an id because the answer carried no name.

    Parameters
    ----------
    state
        The CLI state (for the output mode).
    grants
        The listed grants.
    """
    if any(g.principal is None and g.group is None for g in grants):
        _output.next_step(
            state,
            "Names not found for some subjects, so their ids are shown: the server may be "
            "older than this CLI (upgrade it to see names), or the person or group was deleted "
            "a moment ago. Map the ids with `lakana principal list` and `lakana group list`.",
        )


def subject_of(item: _HasSubject) -> str:
    """Say who a grant or a policy is for.

    Parameters
    ----------
    item
        A grant, row filter or column mask.

    Returns
    -------
    str
        `user alice`, `service ci-bot` or `group analysts`, as the answer names the subject; the
        id-based `principal <id>` or `group <id>` when it names none.
    """
    if item.principal is not None:
        return f"{item.principal.principal_type} {item.principal.name}"
    if item.group is not None:
        return f"group {item.group}"
    if item.principal_id is not None:
        return f"principal {item.principal_id}"
    return f"group {item.group_id}"


def exactly_one(first: str | None, second: str | None, flags: str) -> None:
    """Require exactly one of two mutually exclusive options.

    Parameters
    ----------
    first
        The first option's value.
    second
        The second option's value.
    flags
        Both flags, for the message, e.g. `"--principal or --group"`.

    Raises
    ------
    CliError
        If both or neither are set (exit 2).
    """
    if (first is None) == (second is None):
        raise CliError(f"Pass exactly one of {flags}")


@grant_app.command("create")
def create(
    ctx: typer.Context,
    kind: Annotated[str, typer.Option("--kind", help="INSTANCE, ENVIRONMENT, NAMESPACE or TABLE.")],
    principal: _PRINCIPAL = None,
    group: _GROUP = None,
    role: Annotated[
        str | None,
        typer.Option("--role", help="VIEWER, ANALYST, DATA_SCIENTIST, DATA_ENGINEER or ADMIN."),
    ] = None,
    action: Annotated[
        str | None, typer.Option("--action", help="A single action, e.g. READ_TABLE.")
    ] = None,
    env: _ENV = None,
    ns: _NS = None,
    table: _TABLE = None,
    override_managed: OVERRIDE_MANAGED = False,
    reason: OVERRIDE_REASON = None,
) -> None:
    """Grant a role or action on a securable to a principal or group.

    Parameters
    ----------
    ctx
        The Typer context.
    kind
        The securable's kind.
    principal
        The principal receiving the grant.
    group
        The group receiving the grant.
    role
        A role template.
    action
        A single action.
    env
        The securable's environment.
    ns
        The securable's namespace.
    table
        The securable's table.
    override_managed
        `--override-managed`: make the grant although an access file manages its scope.
    reason
        `--reason`: why you make the grant anyway.
    """
    why = override_reason(override_managed, reason)
    state = state_of(ctx)
    exactly_one(principal, group, "--principal or --group")
    exactly_one(role, action, "--role or --action")
    grant = state.client().grants.grant(
        principal=principal,
        group=group,
        role=role.upper() if role else None,
        action=action.upper() if action else None,
        kind=kind.upper(),
        environment=env,
        namespace=ns,
        table=table,
        override_reason=why,
    )
    recipient = f"group {_output.inline(group)}" if group else _output.inline(principal or "")
    _output.done(
        state,
        f"Created grant {_output.inline(grant.id)} ({grant.role or grant.action} on "
        f"{_output.inline(securable_of(grant))}) to {recipient}",
        grant,
    )


@grant_app.command("list")
def list_grants(
    ctx: typer.Context,
    principal: _PRINCIPAL = None,
    group: _GROUP = None,
    action: Annotated[
        str | None,
        typer.Option("--action", help="Only grants that give this action, e.g. READ_TABLE."),
    ] = None,
    env: _ENV = None,
    ns: _NS = None,
    table: _TABLE = None,
    limit: LIMIT = None,
    cursor: CURSOR = None,
) -> None:
    """List active grants, newest first; without a filter, every grant.

    Filters combine. `--principal` and `--group` list the grants made to them (not those a
    principal has through its groups: `lakana access explain` says that). `--action` lists the
    grants that give an action, by itself or through a role. `--env`, with `--ns` and `--table`,
    lists the grants on exactly that object. A long list prints a "More results" line with the
    command that shows the next page.

    The table names who each grant is for in `subject` (`user alice`, `service ci-bot`,
    `group analysts`); the API answers with the names, so listing costs one request. Only an
    answer without a name shows the id and a notice (a server older than this CLI, or a
    person or group deleted a moment ago). A column with nothing in it is left out. When the
    terminal is too narrow the table leaves out `kind`, `namespace`, `table`, `environment` and
    `action`, in that order, and folds a long `subject` onto more lines, so that the `id` that
    `grant revoke` needs always stays whole, and a line under the table shows the revoke
    command; `--output json` always has every field.

    A grant of `MANAGE_GRANT`, `MANAGE_GROUP` or `MANAGE_TOKEN` as a single action has the
    marker `no effect` in a `note` column, which never drops (a narrow terminal leaves out
    `table` after the others, so that the `id` and the marker stay whole), and a line under
    the table explains it, followed by the command that revokes each such grant with its whole
    id (the first five; `--output json` has all of them and carries the full sentence). Revoke
    such a grant, and give the `ADMIN` role to whoever should keep the ability.

    Parameters
    ----------
    ctx
        The Typer context.
    principal
        Only grants to this principal.
    group
        Only grants to this group.
    action
        Only grants that give this action.
    env
        Only grants on this environment (or, with `ns`, inside it).
    ns
        Only grants on this namespace (or, with `table`, inside it).
    table
        Only grants on this table.
    limit
        Most grants returned (1 to 200); the server's default when omitted.
    cursor
        The cursor of the page to show.
    """
    state = state_of(ctx)
    client = state.client()
    wanted = action.upper() if action else None
    page = client.grants.list_page(
        principal_name=principal,
        group_name=group,
        action=wanted,
        environment=env,
        namespace=ns,
        table=table,
        limit=limit,
        cursor=cursor,
    )
    grants = page.items
    if state.output != "json":
        _warn_about_unnamed(state, grants)
    columns = ["id", "subject", "role", "action", "kind", "environment", "namespace", "table"]
    for name in ("role", "action", "environment", "namespace", "table"):
        if not any(getattr(grant, name) for grant in grants):
            columns.remove(name)
    droppable = ["kind", "namespace", "table", "environment", "action"]
    footnotes: dict[str, str] = {}
    if len(grants) == 1:
        footnotes["id"] = (
            f"Revoke it with: lakana grant revoke {shlex.quote(_output.inline(grants[0].id))}"
        )
    else:
        footnotes["id"] = "Revoke one with: lakana grant revoke ID"
    if any(grant.note for grant in grants):
        columns.append("note")
        footnotes["note"] = "\n".join([_NO_EFFECT_LINE, *_revoke_lines(grants, subject_of)])
    _output.emit(
        state,
        grants,
        columns=columns,
        droppable=droppable,
        derived={"subject": subject_of, "note": _marker},
        whole=["note"],
        footnotes=footnotes,
    )
    more_results(
        state,
        ["lakana", "grant", "list"],
        {
            "--principal": principal,
            "--group": group,
            "--action": wanted,
            "--env": env,
            "--ns": ns,
            "--table": table,
            "--limit": limit,
        },
        page.next_cursor,
    )


@grant_app.command("revoke")
def revoke(
    ctx: typer.Context,
    grant_id: str,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
    override_managed: OVERRIDE_MANAGED = False,
    reason: OVERRIDE_REASON = None,
) -> None:
    """Revoke grant GRANT_ID.

    Take the id from `grant list`, which keeps it in the table even on a narrow terminal.

    Parameters
    ----------
    ctx
        The Typer context.
    grant_id
        The grant id.
    yes
        Skip the confirmation prompt.
    override_managed
        `--override-managed`: revoke the grant although an access file manages its scope.
    reason
        `--reason`: why you revoke the grant anyway.
    """
    why = override_reason(override_managed, reason)
    state = state_of(ctx)
    confirm(state, f"Revoke grant {grant_id}?", yes)
    state.client().grants.revoke(grant_id, override_reason=why)
    _output.done(state, f"Revoked grant {grant_id}")


def register(app: typer.Typer) -> None:
    """Add the `grant` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(grant_app, name="grant")
