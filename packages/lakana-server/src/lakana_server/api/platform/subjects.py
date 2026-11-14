"""Name the person or group a grant, row filter or column mask is for.

An answer carries the ids of its subjects. `subject_names` turns the ids of a whole answer into
names with one query for the principals and one for the groups, so a long listing costs the same
number of statements as a short one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.models import Group, Principal, PrincipalType
from lakana_server.api.platform.schemas import SubjectPrincipalOut

_TYPE_WORDS: dict[PrincipalType, Literal["user", "service"]] = {
    PrincipalType.USER: "user",
    PrincipalType.SERVICE_ACCOUNT: "service",
}
"""How the API writes a principal's type."""


class HasSubject(Protocol):
    """A row that is for one principal or one group."""

    @property
    def principal_id(self) -> uuid.UUID | None:
        """The principal's id, or `None` when the row is for a group."""

    @property
    def group_id(self) -> uuid.UUID | None:
        """The group's id, or `None` when the row is for a principal."""


@dataclass(frozen=True)
class SubjectNames:
    """The names behind the subject ids of one answer.

    Parameters
    ----------
    principals
        Each found principal's id to its name and type.
    groups
        Each found group's id to its name.
    """

    principals: dict[uuid.UUID, SubjectPrincipalOut]
    groups: dict[uuid.UUID, str]

    def principal(self, principal_id: uuid.UUID | None) -> SubjectPrincipalOut | None:
        """Say who a principal id is.

        Parameters
        ----------
        principal_id
            The id of a row, or `None`.

        Returns
        -------
        SubjectPrincipalOut or None
            The principal, or `None` for no id or a principal that is gone.
        """
        return self.principals.get(principal_id) if principal_id is not None else None

    def group(self, group_id: uuid.UUID | None) -> str | None:
        """Say what a group id is called.

        Parameters
        ----------
        group_id
            The id of a row, or `None`.

        Returns
        -------
        str or None
            The group's name, or `None` for no id or a group that is gone.
        """
        return self.groups.get(group_id) if group_id is not None else None


def subject_names(session: Session, rows: Iterable[HasSubject]) -> SubjectNames:
    """Look up the names of every principal and group the rows are for.

    Parameters
    ----------
    session
        The request's database session.
    rows
        The grants, row filters or column masks of one answer.

    Returns
    -------
    SubjectNames
        The names found: one query for the principals and one for the groups, and none for a
        kind the rows do not use.
    """
    held = list(rows)
    principal_ids = {row.principal_id for row in held if row.principal_id is not None}
    group_ids = {row.group_id for row in held if row.group_id is not None}
    principals: dict[uuid.UUID, SubjectPrincipalOut] = {}
    groups: dict[uuid.UUID, str] = {}
    if principal_ids:
        found = session.execute(
            select(Principal.id, Principal.name, Principal.type).where(
                Principal.id.in_(principal_ids)
            )
        )
        principals = {
            row_id: SubjectPrincipalOut(id=row_id, name=name, principal_type=_TYPE_WORDS[kind])
            for row_id, name, kind in found
        }
    if group_ids:
        found_groups = session.execute(select(Group.id, Group.name).where(Group.id.in_(group_ids)))
        groups = {row_id: name for row_id, name in found_groups}
    return SubjectNames(principals, groups)
