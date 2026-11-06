"""The `row_filters` resource: `Client.row_filters`.

`namespace` crosses the wire dotted (`gold.sales`) already —
`lakana_server.api.platform.row_filters` converts to and from its own
internal storage form itself, so the SDK never needs to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lakana._http import HttpTransport, query_params
from lakana._models import RowFilter
from lakana._routes import ROUTES
from lakana.resources._override import override_params


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
    )


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

    def list(
        self,
        *,
        principal_name: str | None = None,
        group_name: str | None = None,
        environment: str | None = None,
        namespace: str | None = None,
        table: str | None = None,
    ) -> _RowFilterList:
        """List row filters for a principal, a group, or a specific table.

        Exactly one of `principal_name`/`group_name`/(`environment`+
        `namespace`+`table` together) must be given.

        Parameters
        ----------
        principal_name
            List this principal's own direct filters.
        group_name
            List this group's own direct filters.
        environment
            The environment to match, for a table-scoped listing.
        namespace
            The dot-joined namespace to match, for a table-scoped listing.
        table
            The table to match, for a table-scoped listing.

        Returns
        -------
        list of RowFilter
            The matching row filters.

        Raises
        ------
        lakana.errors.ValidationError
            If none of the valid parameter combinations is given.
        """
        response = self._transport.request(
            ROUTES["row_filters.list"],
            params=query_params(
                principal_name=principal_name,
                group_name=group_name,
                environment=environment,
                namespace=namespace,
                table=table,
            ),
        )
        return [_row_filter_from_json(item) for item in response.json()]

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
