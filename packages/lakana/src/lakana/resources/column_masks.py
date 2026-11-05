"""The `column_masks` resource: `Client.column_masks`.

`namespace` crosses the wire dotted (`gold.sales`) already —
`lakana_server.api.platform.column_masks` converts to and from its own
internal storage form itself, so the SDK never needs to.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lakana._http import HttpTransport, query_params
from lakana._models import ColumnMask
from lakana._routes import ROUTES


def _column_mask_from_json(data: dict[str, Any]) -> ColumnMask:
    """Build a `ColumnMask` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `ColumnMaskOut` JSON body.

    Returns
    -------
    ColumnMask
        The parsed column mask.
    """
    return ColumnMask(
        id=data["id"],
        principal_id=data["principal_id"],
        group_id=data["group_id"],
        environment=data["environment"],
        namespace=data["namespace"],
        table=data["table"],
        column=data["column"],
        expression=data["expression"],
        created_by=data["created_by"],
        created_at=datetime.fromisoformat(data["created_at"]),
        bound=data.get("bound"),
    )


_ColumnMaskList = list[ColumnMask]
"""Alias for `list[ColumnMask]`, resolved at module scope.

`ColumnMasks` defines a method named `list`, which shadows the builtin
`list` for any bare `list[...]` annotation written *inside* the class body —
this alias is resolved before that happens, and referenced by name instead.
"""


class ColumnMasks:
    """The `column-masks` resource of the platform API.

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
        column: str,
        expression: str,
    ) -> ColumnMask:
        """Create a column mask.

        Exactly one of `principal`/`group` must be given; the server
        raises `ValidationError` otherwise, and validates `expression`
        against the mask grammar (the restricted subset plus
        `substr`/`substring`/`||`/`md5`).

        Parameters
        ----------
        principal
            Name of the principal this mask applies to, or `None` if
            applying to a group.
        group
            Name of the group this mask applies to, or `None` if applying
            to a principal.
        environment
            The environment the masked table lives in.
        namespace
            The dot-joined namespace path, such as `"gold.sales"`.
        table
            The table name.
        column
            The masked column's name.
        expression
            The replacement value.

        Returns
        -------
        ColumnMask
            The created column mask.

        Raises
        ------
        lakana.errors.ValidationError
            If neither or both of `principal`/`group` is given, or
            `expression` fails validation.
        lakana.errors.NotFound
            If the named principal, group, or table does not exist.
        """
        response = self._transport.request(
            ROUTES["column_masks.create"],
            json={
                "principal": principal,
                "group": group,
                "environment": environment,
                "namespace": namespace,
                "table": table,
                "column": column,
                "expression": expression,
            },
        )
        return _column_mask_from_json(response.json())

    def list(
        self,
        *,
        principal_name: str | None = None,
        group_name: str | None = None,
        environment: str | None = None,
        namespace: str | None = None,
        table: str | None = None,
    ) -> _ColumnMaskList:
        """List column masks for a principal, a group, or a specific table.

        Exactly one of `principal_name`/`group_name`/(`environment`+
        `namespace`+`table` together) must be given.

        Parameters
        ----------
        principal_name
            List this principal's own direct masks.
        group_name
            List this group's own direct masks.
        environment
            The environment to match, for a table-scoped listing.
        namespace
            The dot-joined namespace to match, for a table-scoped listing.
        table
            The table to match, for a table-scoped listing.

        Returns
        -------
        list of ColumnMask
            The matching column masks.

        Raises
        ------
        lakana.errors.ValidationError
            If none of the valid parameter combinations is given.
        """
        response = self._transport.request(
            ROUTES["column_masks.list"],
            params=query_params(
                principal_name=principal_name,
                group_name=group_name,
                environment=environment,
                namespace=namespace,
                table=table,
            ),
        )
        return [_column_mask_from_json(item) for item in response.json()]

    def revoke(self, column_mask_id: str) -> None:
        """Revoke a column mask.

        Parameters
        ----------
        column_mask_id
            The column mask's id.

        Raises
        ------
        lakana.errors.NotFound
            If no column mask of this id exists.
        """
        self._transport.request(
            ROUTES["column_masks.revoke"], path_params={"column_mask_id": column_mask_id}
        )
