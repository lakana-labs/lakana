"""The `groups` resource: `Client.groups`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, every_page, query_params
from lakana._models import Group, GroupPage, Principal
from lakana._routes import ROUTES
from lakana.resources._override import override_params
from lakana.resources.principals import _principal_from_json


def _group_from_json(data: dict[str, Any]) -> Group:
    """Build a `Group` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `GroupOut` JSON body.

    Returns
    -------
    Group
        The parsed group.
    """
    return Group(name=data["name"])


_GroupList = list[Group]
"""Alias for `list[Group]`, resolved at module scope.

`Groups` defines a method named `list`, which shadows the builtin `list`
for any bare `list[...]` annotation written *inside* the class body — this
alias is resolved before that happens, and referenced by name instead.
"""

_PrincipalList = list[Principal]
"""Alias for `list[Principal]`, resolved at module scope for the same reason."""


class Groups:
    """The `groups` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(self, name: str, *, override_reason: str | None = None) -> Group:
        """Create a group.

        Parameters
        ----------
        name
            The group's unique name.
        override_reason
            Why, 10 to 500 characters on one line, to create the group although an access file
            manages its scope (an audited override, for an administrator of the instance).

        Returns
        -------
        Group
            The created group.

        Raises
        ------
        lakana.errors.Conflict
            If a group of this name already exists.
        """
        response = self._transport.request(
            ROUTES["groups.create"], params=override_params(override_reason), json={"name": name}
        )
        return _group_from_json(response.json())

    def list(
        self, *, search: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> _GroupList:
        """List groups, newest first.

        The groups of one page; `list_page` also returns the cursor of the next one, and
        `iter` walks every page.

        Parameters
        ----------
        search
            Only groups whose name contains this text, in any letter case.
        limit
            Most groups returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `search` with every page.

        Returns
        -------
        list of Group
            Newest first.
        """
        return self.list_page(search=search, limit=limit, cursor=cursor).items

    def list_page(
        self, *, search: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> GroupPage:
        """List one page of groups, newest first, with the cursor of the next page.

        Parameters
        ----------
        search
            Only groups whose name contains this text, in any letter case.
        limit
            Most groups returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `search` with every page.

        Returns
        -------
        GroupPage
            The groups, and the cursor for the next page (`None` on the last).
        """
        response = self._transport.request(
            ROUTES["groups.list"], params=query_params(q=search, limit=limit, cursor=cursor)
        )
        return GroupPage(
            items=[_group_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    def iter(self, *, search: str | None = None, page_size: int = 100) -> Iterator[Group]:
        """Yield every matching group, newest first, fetching pages as needed.

        Parameters
        ----------
        search
            Only groups whose name contains this text, in any letter case.
        page_size
            Groups per request (1 to 200).

        Returns
        -------
        Iterator of Group
            Each group, once.
        """
        return every_page(
            lambda cursor: self.list_page(search=search, limit=page_size, cursor=cursor)
        )

    def delete(self, name: str, *, override_reason: str | None = None) -> None:
        """Delete a group.

        Parameters
        ----------
        name
            The group's name.
        override_reason
            Why, 10 to 500 characters on one line, to delete the group although an access file
            manages its scope (an audited override, for an administrator of the instance).

        Raises
        ------
        lakana.errors.NotFound
            If no group of this name exists.
        lakana.errors.Conflict
            If any active grant, row filter, or column mask still
            references the group.
        """
        self._transport.request(
            ROUTES["groups.delete"],
            params=override_params(override_reason),
            path_params={"name": name},
        )

    def list_members(self, name: str) -> _PrincipalList:
        """List a group's members.

        Parameters
        ----------
        name
            The group's name.

        Returns
        -------
        list of Principal
            Every member of the group, ordered by name.

        Raises
        ------
        lakana.errors.NotFound
            If no group of this name exists.
        """
        response = self._transport.request(
            ROUTES["groups.list_members"], path_params={"name": name}
        )
        return [_principal_from_json(item) for item in response.json()]

    def _membership(
        self, route: str, name: str, principal_name: str, override_reason: str | None
    ) -> None:
        """Add a principal to a group or remove it.

        Parameters
        ----------
        route
            The route's key: ``groups.add_member`` or ``groups.remove_member``.
        name
            The group's name.
        principal_name
            The principal.
        override_reason
            The managed-scope override's reason, or `None`.
        """
        self._transport.request(
            ROUTES[route],
            params=override_params(override_reason),
            path_params={"name": name, "principal_name": principal_name},
        )

    def add_member(
        self, name: str, principal_name: str, *, override_reason: str | None = None
    ) -> None:
        """Add a principal to a group. Idempotent.

        Parameters
        ----------
        name
            The group's name.
        principal_name
            The principal to add.
        override_reason
            Why, 10 to 500 characters on one line, to add the member although an access file manages
            its scope (an audited override, for an administrator of the instance).

        Raises
        ------
        lakana.errors.NotFound
            If no group or principal of these names exists.
        """
        self._membership("groups.add_member", name, principal_name, override_reason)

    def remove_member(
        self, name: str, principal_name: str, *, override_reason: str | None = None
    ) -> None:
        """Remove a principal from a group.

        Parameters
        ----------
        name
            The group's name.
        principal_name
            The principal to remove.
        override_reason
            Why, 10 to 500 characters on one line, to remove the member although an access file
            manages its scope (an audited override, for an administrator of the instance).

        Raises
        ------
        lakana.errors.NotFound
            If no group or principal of these names exists.
        """
        self._membership("groups.remove_member", name, principal_name, override_reason)
