"""Look principals and groups up by name for the services that take names, and names by id."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, String, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from lakana_core.errors import NotFoundError
from lakana_core.models import Group, Principal
from lakana_core.pagination import SortField, SortKey, name_order, place_keys


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


def subject_name(
    principal_id: InstrumentedAttribute[uuid.UUID | None],
    group_id: InstrumentedAttribute[uuid.UUID | None],
) -> ColumnElement[str]:
    """Name whom a grant or a policy is made to, for a list sorted by it.

    Parameters
    ----------
    principal_id
        The row's principal column; null for a row made to a group.
    group_id
        The row's group column; null for a row made to a principal.

    Returns
    -------
    ColumnElement
        The principal's name, else the group's.
    """
    return func.coalesce(
        select(Principal.name).where(Principal.id == principal_id).scalar_subquery(),
        select(Group.name).where(Group.id == group_id).scalar_subquery(),
        type_=String,
    )


def subject_place_sorts(
    session: Session,
    *,
    newest: str,
    at: InstrumentedAttribute[datetime],
    principal_id: InstrumentedAttribute[uuid.UUID | None],
    group_id: InstrumentedAttribute[uuid.UUID | None],
    place: tuple[InstrumentedAttribute[str] | InstrumentedAttribute[str | None], ...],
) -> dict[str, SortField]:
    """Build the sorts a list of grants or policies shares: by time, by whom and by where.

    Parameters
    ----------
    session
        The session the list is read in, for its database's dialect.
    newest
        The name of the time's field, the list's own order.
    at
        When the row was made.
    principal_id
        The principal it is made to; null for a group.
    group_id
        The group it is made to; null for a principal.
    place
        Its environment, namespace and table columns, the widest first (`place_keys`).

    Returns
    -------
    dict
        The fields `newest`, ``who`` and ``on``; the rows of one subject or place newest first.
    """
    latest = SortKey(at, datetime, descending=True)
    who = name_order(session, subject_name(principal_id, group_id))
    return {
        newest: SortField((latest._replace(descending=None),), descending=True),
        "who": SortField((SortKey(who, str, nullable=True), latest), descending=False),
        "on": SortField((*place_keys(session, *place), latest), descending=False),
    }
