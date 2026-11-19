"""The `row_filters` resource: `Client.row_filters`.

`namespace` crosses the wire dotted (`gold.sales`) already —
`lakana_server.api.platform.row_filters` converts to and from its own
internal storage form itself, so the SDK never needs to.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any, TypedDict, Unpack

from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, every_page, query_params
from lakana._models import RowFilter, RowFilterPage
from lakana._routes import ROUTES
from lakana.resources._override import override_params
from lakana.resources._subject import subject_principal


def _row_filter_from_json(data: dict[str, Any]) -> RowFilter:
    """Build a `RowFilter` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `RowFilterOut` JSON body.

    Returns
    -------
    RowFilter
        The parsed row filter.
    """
    return RowFilter(
        id=data["id"],
        principal_id=data["principal_id"],
        group_id=data["group_id"],
        environment=data["environment"],
        namespace=data["namespace"],
        table=data["table"],
        expression=data["expression"],
        created_by=data["created_by"],
        created_at=datetime.fromisoformat(data["created_at"]),
        bound=data.get("bound"),
        principal=subject_principal(data.get("principal")),
        group=data.get("group"),
    )


class PolicyFilters(TypedDict, total=False):
    """The filters of a row filter or column mask listing; each is optional, and they combine."""

    principal_name: str | None
    group_name: str | None
    environment: str | None
    namespace: str | None
    table: str | None


_RowFilterList = list[RowFilter]
"""Alias for `list[RowFilter]`, resolved at module scope.

`RowFilters` defines a method named `list`, which shadows the builtin
`list` for any bare `list[...]` annotation written *inside* the class body —
this alias is resolved before that happens, and referenced by name instead.
"""


class RowFilters:
    """The `row-filters` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(
        self,
        *,
        principal: str | None = None,
        group: str | None = None,
        environment: str,
        namespace: str,
        table: str,
        expression: str,
        override_reason: str | None = None,
    ) -> RowFilter:
        """Create a row filter.

        Exactly one of `principal`/`group` must be given; the server
        raises `ValidationError` otherwise, and validates `expression`
        against the restricted subset.

        Parameters
        ----------
        principal
            Name of the principal this filter applies to, or `None` if
            applying to a group.
        group
            Name of the group this filter applies to, or `None` if
            applying to a principal.
        environment
            The environment the filtered table lives in.
        namespace
            The dot-joined namespace path, such as `"gold.sales"`.
        table
            The table name.
        expression
            The restricted-subset predicate.
        override_reason
            Why, 10 to 500 characters on one line, to create the row filter although an access file
            manages its scope (an audited override, for an administrator of the instance).

        Returns
        -------
        RowFilter
            The created row filter.

        Raises
        ------
        lakana.errors.ValidationError
            If neither or both of `principal`/`group` is given, or
            `expression` fails validation.
        lakana.errors.NotFound
            If the named principal, group, or table does not exist.
        """
        response = self._transport.request(
            ROUTES["row_filters.create"],
            params=override_params(override_reason),
            json={
                "principal": principal,
                "group": group,
                "environment": environment,
                "namespace": namespace,
                "table": table,
                "expression": expression,
            },
        )
        return _row_filter_from_json(response.json())

    def list(self, **filters: Unpack[PolicyFilters]) -> _RowFilterList:
        """List every active row filter that matches, newest first, reading every page.

        Every filter is optional, and they combine; with none, every row filter is listed.
        `list_page` reads one page, and `iter` yields them as the pages come.

        Parameters
        ----------
        **filters
            The filters of `list_page`: `principal_name` or `group_name`, and `environment`,
            `namespace`, `table`.

        Returns
        -------
        list of RowFilter
            Newest first.

        Raises
        ------
        lakana.errors.ValidationError
            If both `principal_name` and `group_name` are given, or `table` without `namespace`
            or `namespace` without `environment`.
        lakana.errors.NotFound
            If no principal or group has the name given.
        lakana.errors.PermissionDenied
            Without `MANAGE_POLICY` on the instance.
        """
        return list(self.iter(**filters))

    def list_page(
        self,
        *,
        principal_name: str | None = None,
        group_name: str | None = None,
        environment: str | None = None,
        namespace: str | None = None,
        table: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> RowFilterPage:
        """List one page of row filters, newest first, with the cursor of the next page.

        Parameters
        ----------
        principal_name
            Only the row filters made to this principal.
        group_name
            Only the row filters made to this group.
        environment
            Only the row filters in this environment.
        namespace
            Only the row filters on tables of this dot-joined namespace; needs `environment`.
        table
            Only the row filters on this table; needs `namespace`.
        limit
            Most row filters returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same filters with every page.

        Returns
        -------
        RowFilterPage
            The row filters, and the cursor for the next page (`None` on the last).
        """
        response = self._transport.request(
            ROUTES["row_filters.list"],
            params=query_params(
                principal_name=principal_name,
                group_name=group_name,
                environment=environment,
                namespace=namespace,
                table=table,
                limit=limit,
                cursor=cursor,
            ),
        )
        return RowFilterPage(
            items=[_row_filter_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    def iter(
        self, *, page_size: int = 100, **filters: Unpack[PolicyFilters]
    ) -> Iterator[RowFilter]:
        """Yield every matching row filter, newest first, fetching pages as needed.

        Parameters
        ----------
        page_size
            Row filters per request (1 to 200).
        **filters
            The filters of `list_page`: `principal_name` or `group_name`, and `environment`,
            `namespace`, `table`.

        Returns
        -------
        Iterator of RowFilter
            Each row filter, once.
        """
        return every_page(lambda cursor: self.list_page(limit=page_size, cursor=cursor, **filters))

    def revoke(self, row_filter_id: str, *, override_reason: str | None = None) -> None:
        """Revoke a row filter.

        Parameters
        ----------
        row_filter_id
            The row filter's id.
        override_reason
            Why, 10 to 500 characters on one line, to revoke the row filter although an access file
            manages its scope (an audited override, for an administrator of the instance).

        Raises
        ------
        lakana.errors.NotFound
            If no row filter of this id exists.
        """
        self._transport.request(
            ROUTES["row_filters.revoke"],
            params=override_params(override_reason),
            path_params={"row_filter_id": row_filter_id},
        )
