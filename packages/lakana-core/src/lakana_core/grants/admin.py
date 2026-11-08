"""Whether a principal is an instance administrator, and the actions only one may use.

The few rules that ask for the `ADMIN` role itself, not for an action it
bundles. `MINT_TASK_TOKEN` for another principal asks `holds_instance_admin`.
`MANAGE_GRANT`, `MANAGE_GROUP` and `MANAGE_TOKEN` ask it for every
use: each of them makes its holder an administrator in effect, so a
grant of the action alone gives nothing, and `authorize_admin_only` is the
one way their call sites authorize.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    Securable,
    SecurableKind,
)
from lakana_core.errors import PermissionDenied
from lakana_core.grants.roles import Role
from lakana_core.models import Grant, Group, GroupMembership, Principal


def holds_instance_admin(session: Session, principal_id: uuid.UUID) -> bool:
    """Say whether a principal holds the `ADMIN` role at instance scope.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The principal.

    Returns
    -------
    bool
        `True` for an active principal with an unrevoked `ADMIN` role grant of
        kind `INSTANCE`, held directly or through a group. A single action,
        another role, `ADMIN` on an environment or namespace, a revoked grant,
        a deactivated principal, or a deleted group does not count.

    Notes
    -----
    `lakana_core.grants.service.active_instance_admin_ids` answers the same
    question for every principal at once; a test asserts the two agree, so
    change them together.
    """
    groups = select(GroupMembership.group_id).where(GroupMembership.principal_id == principal_id)
    found = session.execute(
        select(Grant.id)
        .where(
            exists().where(Principal.id == principal_id, Principal.is_active.is_(True)),
            Grant.revoked_at.is_(None),
            Grant.role == Role.ADMIN,
            Grant.kind == SecurableKind.INSTANCE,
            or_(Grant.principal_id == principal_id, Grant.group_id.in_(groups)),
        )
        .limit(1)
    ).first()
    return found is not None


ADMIN_ONLY_ACTIONS: frozenset[Action] = frozenset(
    {Action.MANAGE_GRANT, Action.MANAGE_GROUP, Action.MANAGE_TOKEN}
)
"""The actions that work only for a holder of the `ADMIN` role on the instance.

Granting access, changing who is in a group and issuing a token for a
principal each let their holder become an administrator, so the role is
asked for directly and the action cannot be granted on its own.
"""


def needs_admin_role(action: Action) -> bool:
    """Say whether an action works only for a holder of the `ADMIN` role on the instance.

    For a reader that explains access (`lakana_core.grants.explain`): the call sites of these
    actions authorize through `authorize_admin_only`, never through this.

    Parameters
    ----------
    action
        Any action.

    Returns
    -------
    bool
        `True` for one of `ADMIN_ONLY_ACTIONS`.
    """
    return action in ADMIN_ONLY_ACTIONS


def admin_only_message(action: Action) -> str:
    """Say that an action needs the `ADMIN` role, and how to get on.

    Parameters
    ----------
    action
        One of `ADMIN_ONLY_ACTIONS`.

    Returns
    -------
    str
        The text of the refusal.
    """
    return (
        f"{action.value} needs the ADMIN role on the instance. Ask an administrator to do "
        "this, or to give you the role: lakana grant create --kind INSTANCE --principal NAME "
        "--role ADMIN"
    )


def authorize_admin_only(
    session: Session, authz: AuthorizationService, p: PrincipalRef, action: Action
) -> None:
    """Authorize one of the three actions: the action, then the `ADMIN` role.

    The only way a call site of `MANAGE_GRANT`, `MANAGE_GROUP` or
    `MANAGE_TOKEN` authorizes, so that none can do one check without the
    other (a test enumerates the call sites).

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service of the calling service. An
        `AllowAllAuthorization` asks for no role either: it is the server's
        bootstrap, which grants the first administrator before one exists.
    p
        The acting principal.
    action
        One of `ADMIN_ONLY_ACTIONS`.

    Raises
    ------
    ValueError
        If `action` is not one of the three: a programming error.
    PermissionDenied
        If `authz` refuses the action at instance scope (its own message and
        audit), or `p` does not hold the `ADMIN` role at instance scope
        (`admin_only_message`, audited through `authz`'s limiter as
        `AUTHORIZATION_DENIED` with ``reason: not_instance_admin``).
    """
    if action not in ADMIN_ONLY_ACTIONS:
        names = ", ".join(sorted(a.value for a in ADMIN_ONLY_ACTIONS))
        raise ValueError(f"{action.value} is not one of {names}")
    scope = Securable.instance_scope()
    authz.authorize(p, action, scope)
    if isinstance(authz, AllowAllAuthorization) or holds_instance_admin(session, p.id):
        return
    if isinstance(authz, AuditingAuthorization):
        authz.record_denial(p, action, scope, reason="not_instance_admin")
    raise PermissionDenied(admin_only_message(action))


def acts_as_instance_admin(session: Session, authz: AuthorizationService, p: PrincipalRef) -> bool:
    """Say whether `p` counts as an instance administrator for the authorization in use.

    For a rule that asks for the `ADMIN` role together with another action, which the caller
    authorizes itself (`lakana_core.identity.sessions`: ending another person's sessions,
    changing the session limits).

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service of the calling service.
    p
        The acting principal.

    Returns
    -------
    bool
        `True` if `p` holds the `ADMIN` role at instance scope, or `authz` is
        `AllowAllAuthorization` (the server's bootstrap, which asks for no role).
    """
    return isinstance(authz, AllowAllAuthorization) or holds_instance_admin(session, p.id)


def may_see_grants(session: Session, authz: AuthorizationService, p: PrincipalRef) -> bool:
    """Say whether `p` may see who holds access: the same rule as the grants listing.

    That is `authorize_admin_only` for `MANAGE_GRANT`, asked as a yes or no: nothing is
    recorded for a `False`, since a caller who may not see the grants is not attempting
    anything.

    Parameters
    ----------
    session
        Database session.
    authz
        The authorization service of the calling service.
    p
        The acting principal.

    Returns
    -------
    bool
        `True` if `authz` permits `MANAGE_GRANT` at instance scope and `p` holds the `ADMIN`
        role there (or `authz` is `AllowAllAuthorization`, which asks for no role).
    """
    if not authz.permits(p, Action.MANAGE_GRANT, Securable.instance_scope()):
        return False
    return isinstance(authz, AllowAllAuthorization) or holds_instance_admin(session, p.id)


_WARNED = 20
"""Most grants the start-up warning names (and the most rows it loads)."""

_SHOWN_CHARS = 200
"""Most characters of a name the start-up warning prints."""


@dataclass(frozen=True)
class IneffectiveGrant:
    """A single-action grant of one of the three actions: it exists and does nothing.

    Parameters
    ----------
    id
        The grant's id, as `lakana grant revoke` takes it.
    action
        ``MANAGE_GRANT``, ``MANAGE_GROUP`` or ``MANAGE_TOKEN``.
    subject
        ``principal '<name>'`` or ``group '<name>'``, the name escaped.
    """

    id: uuid.UUID
    action: str
    subject: str


def ineffective_note(action: Action | None) -> str | None:
    """Say, for a listing, that a grant of this action has no effect.

    Parameters
    ----------
    action
        A grant's single action, or `None` for a role grant.

    Returns
    -------
    str or None
        ``no effect: <ACTION> needs the ADMIN role`` for one of
        `ADMIN_ONLY_ACTIONS`, else `None`. It names no release, so it cannot go stale.
    """
    if action is not None and action in ADMIN_ONLY_ACTIONS:
        return f"no effect: {action.value} needs the ADMIN role"
    return None


def _quoted(name: str) -> str:
    """Write a name that came from a request so that it cannot end its own quotes.

    Parameters
    ----------
    name
        A principal's or group's name.

    Returns
    -------
    str
        `ascii(name)` with its delimiting quotes, so that a quote inside the name is
        escaped or makes the delimiter the other quote; the escaped text is cut at the
        length `shown` allows, and ends in ``…`` when it was.
    """
    whole = ascii(name)
    inner = whole[1:-1]
    cut = len(inner) > _SHOWN_CHARS
    return f"{whole[0]}{inner[:_SHOWN_CHARS]}{'…' if cut else ''}{whole[-1]}"


def ineffective_grants_warning(session: Session) -> str | None:
    """Write the server's start-up warning about single-action grants of the three actions.

    They were created before the actions were bound to the `ADMIN` role and
    are kept: nothing is deleted on upgrade. The query counts them and loads
    at most `_WARNED` rows, oldest first, so that memory stays bounded.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    str or None
        `None` when no unrevoked grant of the three exists. Otherwise one line: how many,
        the first `_WARNED` with id, action and subject, the two commands (for the kinds of
        subject that occur) and the sentence on who must act now.
    """
    matching = (Grant.revoked_at.is_(None), Grant.action.in_(ADMIN_ONLY_ACTIONS))
    total, principals = session.execute(
        select(func.count(), func.count(Grant.principal_id)).where(*matching)
    ).one()
    if total == 0:
        return None
    rows = session.execute(
        select(Grant.id, Grant.action, Principal.name, Group.name)
        .outerjoin(Principal, Principal.id == Grant.principal_id)
        .outerjoin(Group, Group.id == Grant.group_id)
        .where(*matching)
        .order_by(Grant.granted_at, Grant.id)
        .limit(_WARNED)
    ).all()
    found: list[IneffectiveGrant] = []
    for grant_id, action, principal, group in rows:
        assert action is not None  # nosec B101 -- the query selected on the action
        subject = (
            f"principal {_quoted(principal)}"
            if principal is not None
            else f"group {_quoted(group or '')}"
        )
        found.append(IneffectiveGrant(grant_id, action.value, subject))
    listed = "; ".join(f"{g.id} ({g.action}, {g.subject})" for g in found)
    if total > len(found):
        listed += f"; and {total - len(found)} more (`lakana grant list` marks each with a note)"
    role = "`lakana grant create --kind INSTANCE --{} NAME --role ADMIN`"
    if principals == total:
        command = role.format("principal")
    elif principals == 0:
        command = role.format("group")
    else:
        command = f"{role.format('principal')} for a principal, {role.format('group')} for a group"
    return (
        f"{total} grant(s) of MANAGE_GRANT, MANAGE_GROUP or MANAGE_TOKEN as a single action "
        "have no effect: these actions work only with the ADMIN role on the instance. "
        f"{listed}. Revoke each with `lakana grant revoke ID`. To let someone keep the "
        f"ability, give them the role: {command}. Their holders can no longer grant access, "
        "manage groups or issue tokens: an administrator (a principal with the ADMIN role on "
        "the instance) must do that now. If no administrator can sign in any more, read "
        '"Upgrade policy" '
        "(https://lakana-labs.github.io/lakana/explanation/upgrades/) in the documentation."
    )
