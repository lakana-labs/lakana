"""Finding a live grant identical to another one, and what the database enforces of it (#673).

Two grants are twins when both are live (not revoked) and they agree on the subject (the
principal or the group), on the role or the action, on the kind, and on what binds the target:
the environment's id, the namespace's path within it, or the table's id. The display names of
the environment and table play no part, as they play none in a decision
(`GrantAuthorization._applies_to`). A grant whose target row is gone holds no id and has no
twin; such a grant never matches anything, and `revoke_attached` revokes it with its target.

`GrantService.grant` refuses a twin with `AlreadyExistsError`; the unique indexes of `Grant`
(`ux_grants_live_<kind>`) hold the same rule in the database, one per kind, so two requests that
both looked before either wrote still leave one grant.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from lakana_core.auth import SecurableKind
from lakana_core.identifiers import NamespacePath
from lakana_core.models import Grant
from lakana_core.models.grant import LIVE_GRANT_TARGETS


def live_twin(session: Session, grant: Grant) -> Grant | None:
    """Find the oldest live grant identical to `grant`, other than `grant` itself.

    Parameters
    ----------
    session
        Database session.
    grant
        A grant, live or not, saved or not.

    Returns
    -------
    Grant or None
        The twin, or `None` when there is none or `grant`'s target is gone.
    """
    conditions: list[ColumnElement[bool]] = [
        Grant.revoked_at.is_(None),
        Grant.id != grant.id,
        Grant.kind == grant.kind,
        Grant.principal_id.is_not_distinct_from(grant.principal_id),
        Grant.group_id.is_not_distinct_from(grant.group_id),
        Grant.role.is_not_distinct_from(grant.role),
        Grant.action.is_not_distinct_from(grant.action),
    ]
    for name in LIVE_GRANT_TARGETS[grant.kind]:
        value = getattr(grant, name)
        if value is None:
            return None
        conditions.append(getattr(Grant, name) == value)
    return session.scalars(
        select(Grant).where(*conditions).order_by(Grant.granted_at, Grant.id).limit(1)
    ).first()


def twin_message(subject: str, twin: Grant) -> str:
    """Say that a grant already exists, naming it.

    Parameters
    ----------
    subject
        Who the refused grant was for, as the caller named them: ``Principal 'tess'`` or
        ``Group 'eng'``.
    twin
        The live grant it would have duplicated.

    Returns
    -------
    str
        One sentence with the twin's id.
    """
    return (
        f"{subject} already holds {twin.role or twin.action} on {_target(twin)} "
        f"through grant {twin.id}; nothing was created."
    )


def _target(grant: Grant) -> str:
    """Name a grant's target the way the grant refusals name it.

    Parameters
    ----------
    grant
        The grant.

    Returns
    -------
    str
        ``the instance``, ``environment 'dev'``, ``namespace gold in 'dev'`` or
        ``table 'orders' in 'dev'/gold``.
    """
    if grant.kind is SecurableKind.INSTANCE:
        return "the instance"
    if grant.kind is SecurableKind.ENVIRONMENT:
        return f"environment {grant.environment!r}"
    namespace = NamespacePath.parse(grant.namespace or "")
    if grant.kind is SecurableKind.NAMESPACE:
        return f"namespace {namespace} in {grant.environment!r}"
    return f"table {grant.table!r} in {grant.environment!r}/{namespace}"
