"""`lakana access *`: why a principal may, or may not, do something."""

from __future__ import annotations

from typing import Annotated

import typer

from lakana._models import Permission, PermissionVia
from lakana.cli import _output, access_file
from lakana.cli._state import CliError, state_of
from lakana.cli.grants import securable_of

access_app = typer.Typer(
    help="Explain who may do what, and keep access in a file.", no_args_is_help=True
)

_SCOPE_FORMAT = "ENV, ENV/NAMESPACE or ENV/NAMESPACE/TABLE"

_REFUSED = (
    "An action without a `via` is refused: no grant gives it here. Give one with "
    "`lakana grant create`, or look at the grants with `lakana grant list`."
)


def _parse_scope(scope: str | None) -> tuple[str | None, str | None, str | None]:
    """Split `--scope` into environment, namespace and table.

    Parameters
    ----------
    scope
        ``dev``, ``dev/gold.sales`` or ``dev/gold.sales/orders``; `None` for the instance.

    Returns
    -------
    tuple of (str or None, str or None, str or None)
        The environment, the dot-joined namespace and the table; unset parts are `None`.

    Raises
    ------
    CliError
        If a part is empty or there are more than three.
    """
    if scope is None:
        return None, None, None
    parts = scope.split("/")
    if len(parts) > 3 or not all(part.strip() for part in parts):
        raise CliError(
            f"--scope {_output.inline(scope)!r} is not an object. Write it as {_SCOPE_FORMAT}, "
            "for example dev/gold.sales/orders; leave it out for the instance."
        )
    environment, namespace, table = [*parts, None, None][:3]
    return environment, namespace, table


def _via_text(permission: Permission) -> str:
    """Say through which grants a permission is given.

    Parameters
    ----------
    permission
        One action's answer.

    Returns
    -------
    str
        ``role VIEWER on environment dev``, ``group team: READ_TABLE granted on table gold.orders
        in environment dev``, joined with ``; ``; empty when refused.
    """
    return "; ".join(_one_via(permission.action, via) for via in permission.via)


def _one_via(action: str, via: PermissionVia) -> str:
    """Say how one grant gives an action.

    Parameters
    ----------
    action
        The action.
    via
        The grant.

    Returns
    -------
    str
        The role or the action, where it is granted, and the group it comes through.
    """
    what = f"role {via.role}" if via.role else f"{action} granted"
    text = f"{what} on {securable_of(via.scope)}"
    if via.group is not None:
        text = f"group {via.group}: {text}"
    return _output.inline(text)


@access_app.command("explain")
def explain(
    ctx: typer.Context,
    principal: str,
    scope: Annotated[
        str | None,
        typer.Option(
            "--scope", help=f"The object: {_SCOPE_FORMAT}. Leave it out for the instance."
        ),
    ] = None,
) -> None:
    """Show what PRINCIPAL may do on an object, and through which grant, role or group.

    One row per action that applies to the object: `allowed` is the answer of the same
    check the server makes when the principal tries, and `via` names the grants that give it
    (a role, a single action, or either through a group). You may explain yourself; explaining
    someone else needs the `ADMIN` role on the instance.

    Parameters
    ----------
    ctx
        The Typer context.
    principal
        The principal.
    scope
        The object, or the instance when omitted.
    """
    state = state_of(ctx)
    environment, namespace, table = _parse_scope(scope)
    answer = state.client().principals.permissions(
        principal, environment=environment, namespace=namespace, table=table
    )
    if state.output == "json":
        _output.print_json(answer)
        return
    where = securable_of(answer.scope)
    typer.echo(_output.safe_text(f"What {_output.inline(answer.principal)} may do on {where}:"))
    footnotes = {"via": _REFUSED} if not all(p.allowed for p in answer.permissions) else {}
    _output.emit(
        state,
        answer.permissions,
        columns=["action", "allowed", "via"],
        derived={"allowed": lambda p: "yes" if p.allowed else "no", "via": _via_text},
        flexible=["via"],
        footnotes=footnotes,
    )
    if answer.note:
        typer.echo(_output.safe_text(answer.note))


@access_app.command("grants")
def grants(
    ctx: typer.Context,
    principal: Annotated[
        str | None, typer.Argument(help="Whose grants to list; yours when left out.")
    ] = None,
) -> None:
    """Show the grants PRINCIPAL holds, directly and through groups; yourself when left out.

    One row per live grant, newest first. `group` names the group the grant comes through and
    is empty for a grant made to the principal itself. You may list your own grants with a
    personal token; listing someone else's needs the `ADMIN` role on the instance.

    Parameters
    ----------
    ctx
        The Typer context.
    principal
        The principal; yourself when omitted.
    """
    state = state_of(ctx)
    client = state.client()
    who = principal if principal is not None else client.whoami().name
    _output.emit(
        state,
        client.principals.grants(who),
        columns=[
            "group",
            "role",
            "action",
            "kind",
            "environment",
            "namespace",
            "table",
            "granted_at",
        ],
        derived={"group": lambda held: held.group or ""},
    )


def register(app: typer.Typer) -> None:
    """Add the `access` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    access_file.register(access_app)
    app.add_typer(access_app, name="access")
