"""The `principals` resource: `Client.principals`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, every_page, query_params
from lakana._models import Permission, Permissions, PermissionVia, Principal, PrincipalPage, Scope
from lakana._routes import ROUTES
from lakana.resources._override import override_params


def _principal_from_json(data: dict[str, Any]) -> Principal:
    """Build a `Principal` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `PrincipalOut` JSON body.

    Returns
    -------
    Principal
        The parsed principal.
    """
    return Principal(name=data["name"], type=data["type"], is_active=data["is_active"])


def _scope_from_json(data: dict[str, Any]) -> Scope:
    """Build a `Scope` from the platform API's JSON.

    Parameters
    ----------
    data
        The decoded `ScopeOut` JSON body.

    Returns
    -------
    Scope
        The parsed object.
    """
    return Scope(
        kind=data["kind"],
        environment=data["environment"],
        namespace=data["namespace"],
        table=data["table"],
    )


def _permissions_from_json(data: dict[str, Any]) -> Permissions:
    """Build `Permissions` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `PermissionsOut` JSON body.

    Returns
    -------
    Permissions
        The parsed answer.
    """
    return Permissions(
        principal=data["principal"],
        active=data["active"],
        scope=_scope_from_json(data["scope"]),
        permissions=[
            Permission(
                action=entry["action"],
                allowed=entry["allowed"],
                via=[
                    PermissionVia(
                        kind=via["kind"],
                        grant_id=via["grant_id"],
                        group=via["group"],
                        role=via["role"],
                        scope=_scope_from_json(via["scope"]),
                    )
                    for via in entry["via"]
                ],
                reason=entry["reason"],
            )
            for entry in data["permissions"]
        ],
        note=data["note"],
    )


_PrincipalList = list[Principal]
"""Alias for `list[Principal]`, resolved at module scope.

`Principals` defines a method named `list`, which shadows the builtin
`list` for any bare `list[...]` annotation written *inside* the class body —
this alias is resolved before that happens, and referenced by name instead.
"""


class Principals:
    """The `principals` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(self, name: str, type: str, *, override_reason: str | None = None) -> Principal:
        """Create a principal.

        Parameters
        ----------
        name
            The principal's unique name.
        type
            `"USER"` or `"SERVICE_ACCOUNT"`.
        override_reason
            Why, 10 to 500 characters on one line, to create a service account although an access
            file manages its scope (an audited override, for an administrator of the instance).

        Returns
        -------
        Principal
            The created principal.

        Raises
        ------
        lakana.errors.Conflict
            If a principal of this name already exists.
        """
        response = self._transport.request(
            ROUTES["principals.create"],
            params=override_params(override_reason),
            json={"name": name, "type": type},
        )
        return _principal_from_json(response.json())

    def get(self, name: str) -> Principal:
        """Fetch a principal by name.

        Parameters
        ----------
        name
            The principal's name.

        Returns
        -------
        Principal
            The principal.

        Raises
        ------
        lakana.errors.NotFound
            If no principal of this name exists.
        """
        response = self._transport.request(ROUTES["principals.get"], path_params={"name": name})
        return _principal_from_json(response.json())

    def list(
        self, *, search: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> _PrincipalList:
        """List principals, newest first.

        The principals of one page; `list_page` also returns the cursor of the next one, and
        `iter` walks every page.

        Parameters
        ----------
        search
            Only principals whose name contains this text, in any letter case.
        limit
            Most principals returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `search` with every page.

        Returns
        -------
        list of Principal
            Newest first.
        """
        return self.list_page(search=search, limit=limit, cursor=cursor).items

    def list_page(
        self, *, search: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> PrincipalPage:
        """List one page of principals, newest first, with the cursor of the next page.

        Parameters
        ----------
        search
            Only principals whose name contains this text, in any letter case.
        limit
            Most principals returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `search` with every page.

        Returns
        -------
        PrincipalPage
            The principals, and the cursor for the next page (`None` on the last).
        """
        response = self._transport.request(
            ROUTES["principals.list"], params=query_params(q=search, limit=limit, cursor=cursor)
        )
        return PrincipalPage(
            items=[_principal_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    def iter(self, *, search: str | None = None, page_size: int = 100) -> Iterator[Principal]:
        """Yield every matching principal, newest first, fetching pages as needed.

        Parameters
        ----------
        search
            Only principals whose name contains this text, in any letter case.
        page_size
            Principals per request (1 to 200).

        Returns
        -------
        Iterator of Principal
            Each principal, once.
        """
        return every_page(
            lambda cursor: self.list_page(search=search, limit=page_size, cursor=cursor)
        )

    def permissions(
        self,
        name: str,
        *,
        environment: str | None = None,
        namespace: str | None = None,
        table: str | None = None,
    ) -> Permissions:
        """Say what a principal may do on an object, action by action, and through which grant.

        The answer comes from the authorizer the server's routes use. Without an object it is
        about the instance. Anyone may ask about themselves; asking about another principal
        needs the `ADMIN` role on the instance.

        Parameters
        ----------
        name
            The principal.
        environment
            The object's environment.
        namespace
            The object's dot-joined namespace (needs `environment`).
        table
            The object's table (needs `namespace` and `environment`).

        Returns
        -------
        Permissions
            One entry per action the server asks for on such an object.

        Raises
        ------
        lakana.errors.PermissionDenied
            About another principal, without the `ADMIN` role on the instance.
        lakana.errors.NotFound
            For an administrator: no such principal, environment, namespace or table.
        """
        response = self._transport.request(
            ROUTES["principals.permissions"],
            path_params={"name": name},
            params=query_params(environment=environment, namespace=namespace, table=table),
        )
        return _permissions_from_json(response.json())

    def deactivate(self, name: str, *, override_reason: str | None = None) -> None:
        """Deactivate a principal.

        Sets `is_active = False` rather than deleting the principal,
        preserving every grant, PAT, and audit entry that references it.

        Parameters
        ----------
        name
            The principal's name.
        override_reason
            To deactivate a service account although an access file manages its scope,
            in an emergency: why, 10 to 500 characters on one line. Needs the `ADMIN`
            role on the instance; audited, and the next apply reverts it unless the file
            lists it.

        Raises
        ------
        lakana.errors.NotFound
            If no principal of this name exists.
        """
        self._transport.request(
            ROUTES["principals.deactivate"],
            params=override_params(override_reason),
            path_params={"name": name},
        )
