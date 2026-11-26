"""Look principals and groups up by name for the services that take names, and names by id."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.errors import NotFoundError
from lakana_core.models import Group, Principal


def principal_named(session: Session, name: str) -> Principal:
    """Look up a principal by name.

    Parameters
    ----------
    session
        Database session.
    name
        Principal name.

    Returns
    -------
    Principal
        The matching row.

    Raises
    ------
    NotFoundError
        If no principal has that name.
    """
    principal = session.execute(
        select(Principal).where(Principal.name == name)
    ).scalar_one_or_none()
    if principal is None:
        raise NotFoundError(f"No principal named {name!r}.")
    return principal


def principal_name(session: Session, principal_id: uuid.UUID | None) -> str | None:
    """Return the name of a principal a row refers to, as a reader is shown it.

    Parameters
    ----------
    session
        Database session.
    principal_id
        The principal, or `None` when the row names none or its principal was deleted.

    Returns
    -------
    str or None
        Its name, or `None` if unset or deleted.
    """
    if principal_id is None:
        return None
    principal = session.get(Principal, principal_id)
    return principal.name if principal is not None else None


def group_named(session: Session, name: str) -> Group:
    """Look up a group by name.

    Parameters
    ----------
    session
        Database session.
    name
        Group name.

    Returns
    -------
    Group
        The matching row.

    Raises
    ------
    NotFoundError
        If no group has that name.
    """
    group = session.execute(select(Group).where(Group.name == name)).scalar_one_or_none()
    if group is None:
        raise NotFoundError(f"No group named {name!r}.")
    return group
