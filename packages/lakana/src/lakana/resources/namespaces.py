"""The `namespaces` resource: `Client.namespaces`.

Namespace paths are dot-joined strings, such as ``gold.sales``, matching
`lakana_server.api.platform.namespaces`'s own encoding.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, every_page, query_params
from lakana._models import Namespace, NamespacePage
from lakana._routes import ROUTES


def _namespace_from_json(data: dict[str, Any]) -> Namespace:
    """Build a `Namespace` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `NamespaceOut` JSON body.

    Returns
    -------
    Namespace
        The parsed namespace.
    """
    return Namespace(path=data["path"], properties=data["properties"])


_NamespaceList = list[Namespace]
"""Alias for `list[Namespace]`, resolved at module scope.

`Namespaces` defines a method named `list`, which shadows the builtin
`list` for any bare `list[...]` annotation written *inside* the class body —
this alias is resolved before that happens, and referenced by name instead.
"""


class Namespaces:
    """The `namespaces` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(
        self,
        environment: str,
        namespace: str,
        properties: dict[str, str] | None = None,
        *,
        break_glass_reason: str | None = None,
    ) -> Namespace:
        """Create a namespace.

        Parameters
        ----------
        environment
            The environment name.
        namespace
            Dot-joined namespace path, such as `"gold.sales"`.
        properties
            Free-form metadata.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.

        Returns
        -------
        Namespace
            The created namespace.

        Raises
        ------
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.Conflict
            If a namespace of this path already exists.
        """
        body: dict[str, object] = {"path": namespace, "properties": properties or {}}
        if break_glass_reason is not None:
            body["break_glass_reason"] = break_glass_reason
        response = self._transport.request(
            ROUTES["namespaces.create"], path_params={"env": environment}, json=body
        )
        return _namespace_from_json(response.json())

    def get(self, environment: str, namespace: str) -> Namespace:
        """Fetch a namespace by its dotted path.

        Parameters
        ----------
        environment
            The environment name.
        namespace
            Dot-joined namespace path.

        Returns
        -------
        Namespace
            The namespace.

        Raises
        ------
        lakana.errors.NotFound
            If no such namespace exists.
        """
        response = self._transport.request(
            ROUTES["namespaces.get"], path_params={"env": environment, "path": namespace}
        )
        return _namespace_from_json(response.json())

    def list(
        self,
        environment: str,
        *,
        parent: str | None = None,
        depth: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> _NamespaceList:
        """List the namespaces in an environment, in path order.

        The namespaces of one page; `list_page` also returns the cursor of the next one, and
        `iter` walks every page.

        Parameters
        ----------
        environment
            The environment name.
        parent
            Dot-joined path; only the namespaces below it, at every depth.
        depth
            ``1`` keeps only the direct children of `parent` (the top level without one).
        limit
            Most namespaces returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `parent` and `depth`.

        Returns
        -------
        list of Namespace
            The namespaces of this page.

        Raises
        ------
        lakana.errors.NotFound
            If the environment does not exist.
        """
        return self.list_page(
            environment, parent=parent, depth=depth, limit=limit, cursor=cursor
        ).items

    def list_page(
        self,
        environment: str,
        *,
        parent: str | None = None,
        depth: int | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> NamespacePage:
        """List one page of the namespaces, with the cursor of the next page.

        Parameters
        ----------
        environment
            The environment name.
        parent
            Dot-joined path of a namespace: keep only what lies below it, at every depth.
        depth
            ``1`` keeps the direct children of `parent` only (the top level when there is no
            `parent`).
        limit
            Page size, 1 to 200; `None` takes the server's default of 100.
        cursor
            The `next_cursor` of the page before this one, with the same `parent` and `depth`.

        Returns
        -------
        NamespacePage
            The namespaces, and the cursor for the next page (`None` on the last).

        Raises
        ------
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.ValidationError
            If `depth` is not 1, or `cursor` is not one the server issued.
        """
        response = self._transport.request(
            ROUTES["namespaces.list"],
            path_params={"env": environment},
            params=query_params(parent=parent, depth=depth, limit=limit, cursor=cursor),
        )
        return NamespacePage(
            items=[_namespace_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    def iter(
        self,
        environment: str,
        *,
        parent: str | None = None,
        depth: int | None = None,
        page_size: int = 100,
    ) -> Iterator[Namespace]:
        """Yield every matching namespace, in path order, fetching pages as needed.

        Parameters
        ----------
        environment
            The environment name.
        parent
            Dot-joined path; only the namespaces below it, at every depth.
        depth
            ``1`` keeps only the direct children of `parent` (the top level without one).
        page_size
            Namespaces per request (1 to 200).

        Returns
        -------
        Iterator of Namespace
            Each namespace, once.
        """
        return every_page(
            lambda cursor: self.list_page(
                environment, parent=parent, depth=depth, limit=page_size, cursor=cursor
            )
        )

    def delete(
        self, environment: str, namespace: str, *, break_glass_reason: str | None = None
    ) -> None:
        """Drop a namespace.

        Parameters
        ----------
        environment
            The environment name.
        namespace
            Dot-joined namespace path.
        break_glass_reason
            Write to a protected environment outside a run: 10-500 characters, needs
            `MANAGE_ENVIRONMENT`, audited. The next run of the deployed code may overwrite
            the fix.

        Raises
        ------
        lakana.errors.NotFound
            If no such namespace exists.
        lakana.errors.Conflict
            If the namespace still contains tables or child namespaces.
        """
        self._transport.request(
            ROUTES["namespaces.delete"],
            path_params={"env": environment, "path": namespace},
            params={"break_glass_reason": break_glass_reason}
            if break_glass_reason is not None
            else None,
        )
