"""The `grants` resource: `Client.grants`.

`namespace` crosses the wire dotted (`gold.sales`) already —
`lakana_server.api.platform.grants` converts to and from its own internal
storage form itself, so the SDK never needs to.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any, TypedDict, Unpack

from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, every_page, query_params
from lakana._models import Grant, GrantPage
from lakana._routes import ROUTES
from lakana.resources._override import override_params


def _grant_from_json(data: dict[str, Any]) -> Grant:
    """Build a `Grant` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `GrantOut` JSON body.

    Returns
    -------
    Grant
        The parsed grant.
    """
    return Grant(
        id=data["id"],
        principal_id=data["principal_id"],
        group_id=data["group_id"],
        role=data["role"],
        action=data["action"],
        kind=data["kind"],
        environment=data["environment"],
        namespace=data["namespace"],
        table=data["table"],
        granted_by=data["granted_by"],
        granted_at=datetime.fromisoformat(data["granted_at"]),
        note=data.get("note"),
    )


class GrantFilters(TypedDict, total=False):
    """The filters of a grants listing; each is optional, as in `Grants.list_page`."""

    principal_name: str | None
    group_name: str | None
    action: str | None
    environment: str | None
    namespace: str | None
    table: str | None


_GrantList = list[Grant]
"""Alias for `list[Grant]`, resolved at module scope.

`Grants` defines a method named `list`, which shadows the builtin `list`
for any bare `list[...]` annotation written *inside* the class body — this
alias is resolved before that happens, and referenced by name instead.
"""


class Grants:
    """The `grants` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def grant(
        self,
        *,
        principal: str | None = None,
        group: str | None = None,
        role: str | None = None,
        action: str | None = None,
        kind: str,
        environment: str | None = None,
        namespace: str | None = None,
        table: str | None = None,
        override_reason: str | None = None,
    ) -> Grant:
        """Create a grant.

        Exactly one of `principal`/`group` and exactly one of `role`/
        `action` must be given; the server raises `ValidationError`
        otherwise. `environment`/`namespace`/`table` must match what `kind`
        expects.

        Parameters
        ----------
        principal
            Name of the principal to grant to, or `None` if granting to a
            group.
        group
            Name of the group to grant to, or `None` if granting to a
            principal.
        role
            A role template to grant, or `None` if granting a raw action.
        action
            A raw action to grant, or `None` if granting a role.
        kind
            Which tier of the securable hierarchy this grant applies to:
            `"INSTANCE"`, `"ENVIRONMENT"`, `"NAMESPACE"`, or `"TABLE"`.
        environment
            The environment, for an ENVIRONMENT/NAMESPACE/TABLE-scoped
            grant.
        namespace
            The dot-joined namespace path, such as `"gold.sales"`, for a
            NAMESPACE/TABLE-scoped grant.
        table
            The table name, for a TABLE-scoped grant.
        override_reason
            Why, 10 to 500 characters on one line, to make the grant although an access file manages
            its scope (an audited override, for an administrator of the instance).

        Returns
        -------
        Grant
            The created grant.

        Raises
        ------
        lakana.errors.ValidationError
            If the given fields don't form a valid combination.
        lakana.errors.NotFound
            If the named principal, group, or securable does not exist.
        """
        response = self._transport.request(
            ROUTES["grants.create"],
            params=override_params(override_reason),
            json={
                "principal": principal,
                "group": group,
                "role": role,
                "action": action,
                "kind": kind,
                "environment": environment,
                "namespace": namespace,
                "table": table,
            },
        )
        return _grant_from_json(response.json())

    def list(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        **filters: Unpack[GrantFilters],
    ) -> _GrantList:
        """List active grants, newest first; every filter is optional and they combine.

        The grants of one page; `list_page` also returns the cursor of the next one, and
        `iter` walks every page.

        Parameters
        ----------
        limit
            Most grants returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same filters with every page.
        **filters
            The filters of `list_page`: `principal_name`, `group_name`, `action`,
            `environment`, `namespace`, `table`.

        Returns
        -------
        list of Grant
            Newest first.

        Raises
        ------
        lakana.errors.ValidationError
            If both `principal_name` and `group_name` are given, an object filter has a gap,
            `action` is unknown, or `cursor` is not one the server issued for the grants list.
        lakana.errors.PermissionDenied
            Without the `ADMIN` role on the instance.
        """
        return self.list_page(limit=limit, cursor=cursor, **filters).items

    def list_page(
        self,
        *,
        principal_name: str | None = None,
        group_name: str | None = None,
        action: str | None = None,
        environment: str | None = None,
        namespace: str | None = None,
        table: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> GrantPage:
        """List one page of grants, newest first, with the cursor of the next page.

        Parameters
        ----------
        principal_name
            Only grants made to this principal.
        group_name
            Only grants made to this group.
        action
            Only grants that give this action.
        environment
            The environment of an object filter.
        namespace
            The dot-joined namespace of an object filter.
        table
            The table of an object filter.
        limit
            Most grants returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same filters with every page.

        Returns
        -------
        GrantPage
            The grants, and the cursor for the next page (`None` on the last).
        """
        response = self._transport.request(
            ROUTES["grants.list"],
            params=query_params(
                principal_name=principal_name,
                group_name=group_name,
                action=action,
                environment=environment,
                namespace=namespace,
                table=table,
                limit=limit,
                cursor=cursor,
            ),
        )
        return GrantPage(
            items=[_grant_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    def iter(self, *, page_size: int = 100, **filters: Unpack[GrantFilters]) -> Iterator[Grant]:
        """Yield every matching grant, newest first, fetching pages as needed.

        Parameters
        ----------
        page_size
            Grants per request (1 to 200).
        **filters
            The filters of `list_page`: `principal_name`, `group_name`, `action`,
            `environment`, `namespace`, `table`.

        Returns
        -------
        Iterator of Grant
            Each grant, once.
        """
        return every_page(lambda cursor: self.list_page(limit=page_size, cursor=cursor, **filters))

    def revoke(self, grant_id: str, *, override_reason: str | None = None) -> None:
        """Revoke a grant.

        Parameters
        ----------
        grant_id
            The grant's id.
        override_reason
            Why, 10 to 500 characters on one line, to revoke the grant although an access file
            manages its scope (an audited override, for an administrator of the instance).

        Raises
        ------
        lakana.errors.NotFound
            If no grant of this id exists.
        lakana.errors.Conflict
            If revoking this grant would leave zero active instance admins.
        """
        self._transport.request(
            ROUTES["grants.revoke"],
            params=override_params(override_reason),
            path_params={"grant_id": grant_id},
        )
