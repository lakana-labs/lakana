"""Look a principal or a group up by its name, for the services that take names."""

from __future__ import annotations

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
