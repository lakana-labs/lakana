"""The `column_masks` resource: `Client.column_masks`.

`namespace` crosses the wire dotted (`gold.sales`) already —
`lakana_server.api.platform.column_masks` converts to and from its own
internal storage form itself, so the SDK never needs to.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any, Unpack

from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, every_page, query_params
from lakana._models import ColumnMask, ColumnMaskPage
from lakana._routes import ROUTES
from lakana.resources._override import override_params
from lakana.resources._subject import subject_principal
from lakana.resources.row_filters import PolicyFilters


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
        principal=subject_principal(data.get("principal")),
        group=data.get("group"),
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
        override_reason: str | None = None,
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
        override_reason
            Why, 10 to 500 characters on one line, to create the column mask although an access file
            manages its scope (an audited override, for an administrator of the instance).

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
            params=override_params(override_reason),
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

    def list(self, **filters: Unpack[PolicyFilters]) -> _ColumnMaskList:
        """List every active column mask that matches, newest first, reading every page.

        Every filter is optional, and they combine; with none, every column mask is listed.
        `list_page` reads one page, and `iter` yields them as the pages come.

        Parameters
        ----------
        **filters
            The filters of `list_page`: `principal_name` or `group_name`, and `environment`,
            `namespace`, `table`.

        Returns
        -------
        list of ColumnMask
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
    ) -> ColumnMaskPage:
        """List one page of column masks, newest first, with the cursor of the next page.

        Parameters
        ----------
        principal_name
            Only the column masks made to this principal.
        group_name
            Only the column masks made to this group.
        environment
            Only the column masks in this environment.
        namespace
            Only the column masks on tables of this dot-joined namespace; needs `environment`.
        table
            Only the column masks on this table; needs `namespace`.
        limit
            Most column masks returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same filters with every page.

        Returns
        -------
        ColumnMaskPage
            The column masks, and the cursor for the next page (`None` on the last).
        """
        response = self._transport.request(
            ROUTES["column_masks.list"],
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
        return ColumnMaskPage(
            items=[_column_mask_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    def iter(
        self, *, page_size: int = 100, **filters: Unpack[PolicyFilters]
    ) -> Iterator[ColumnMask]:
        """Yield every matching column mask, newest first, fetching pages as needed.

        Parameters
        ----------
        page_size
            Column masks per request (1 to 200).
        **filters
            The filters of `list_page`: `principal_name` or `group_name`, and `environment`,
            `namespace`, `table`.

        Returns
        -------
        Iterator of ColumnMask
            Each column mask, once.
        """
        return every_page(lambda cursor: self.list_page(limit=page_size, cursor=cursor, **filters))

    def revoke(self, column_mask_id: str, *, override_reason: str | None = None) -> None:
        """Revoke a column mask.

        Parameters
        ----------
        column_mask_id
            The column mask's id.
        override_reason
            Why, 10 to 500 characters on one line, to revoke the column mask although an access file
            manages its scope (an audited override, for an administrator of the instance).

        Raises
        ------
        lakana.errors.NotFound
            If no column mask of this id exists.
        """
        self._transport.request(
            ROUTES["column_masks.revoke"],
            params=override_params(override_reason),
            path_params={"column_mask_id": column_mask_id},
        )
