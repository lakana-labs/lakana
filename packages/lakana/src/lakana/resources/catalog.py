"""The `catalog` resource: `Client.catalog`, every table you may read across environments."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import (
    HealthSummary,
    TableListItem,
    TableListPage,
    TableMatch,
    TableTree,
    TreeEnvironment,
    TreeNamespace,
    known_fields,
)
from lakana._routes import ROUTES
from lakana.resources.tables import _when, owner_from_json


def _item_from_json(data: dict[str, Any]) -> TableListItem:
    """Build a `TableListItem` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `TableListItemOut` JSON body.

    Returns
    -------
    TableListItem
        The table.
    """
    match = data["match"]
    return TableListItem(
        environment=data["environment"],
        namespace=data["namespace"],
        name=data["name"],
        uuid=data["uuid"],
        kind=data["kind"],
        model_kind=data["model_kind"],
        state=data["state"],
        owner=owner_from_json(data["owner"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        row_count=data["row_count"],
        size_bytes=data["size_bytes"],
        last_write_at=_when(data["last_write_at"]),
        last_read_at=_when(data["last_read_at"]),
        readers_7d=data["readers_7d"],
        health_summary=HealthSummary(**known_fields(HealthSummary, data["health_summary"])),
        match=None if match is None else TableMatch(**known_fields(TableMatch, match)),
    )


class Catalog:
    """Every table you may read, across environments and namespaces.

    Parameters
    ----------
    transport
        The shared HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(
        self,
        *,
        environment: str | None = None,
        namespace: str | None = None,
        q: str | None = None,
        model_kind: str | None = None,
        sort: str = "name",
        order: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        lang: str | None = None,
    ) -> TableListPage:
        """List one page of the tables you may read.

        What you may not read is left out, not refused. Every table shows its owner, when it was
        last read, how many principals read it in the last 7 days, and its health in one word.

        Parameters
        ----------
        environment
            Only this environment.
        namespace
            Only this namespace and the ones beneath it, dotted.
        q
            Only tables whose name, label, description, grain, or a column's name, label,
            description or measure contains this text; 1 to 200 characters.
        model_kind
            Only tables whose asset header declares this kind: fact, dimension, wide or staging.
        sort
            What the list is sorted by: ``name``, ``owner``, ``health``, ``rows``, ``size``,
            ``last_write``, ``last_read`` or ``created``. A table with no value comes last.
        order
            ``asc`` or ``desc``; by default ``asc`` for name, owner and health, ``desc`` for
            the rest.
        limit
            Tables per page, 1 to 200; the server's default (50) without it.
        cursor
            The ``next_cursor`` of the previous page, sent with the same filters and sort.
        lang
            The language to match translated texts in; your profile's language without it.

        Returns
        -------
        TableListPage
            The page; ``next_cursor`` is `None` on the last, and ``capped`` says a search text
            matched more tables than one search keeps.

        Raises
        ------
        lakana.errors.NotFound
            If ``environment`` does not exist.
        lakana.errors.ValidationError
            If ``sort``, ``order``, ``limit``, ``q``, ``model_kind``, ``lang`` or ``cursor`` is
            not valid; the message names the choices or the range. Start again without
            ``cursor`` after a cursor error.
        """
        given = {
            "environment": environment,
            "namespace": namespace,
            "q": q,
            "model_kind": model_kind,
            "order": order,
            "limit": limit,
            "cursor": cursor,
            "lang": lang,
        }
        params: dict[str, object] = {k: v for k, v in given.items() if v is not None}
        params["sort"] = sort
        body = self._transport.request(ROUTES["catalog.list"], params=params).json()
        return TableListPage(
            items=[_item_from_json(item) for item in body["items"]],
            next_cursor=body["next_cursor"],
            capped=body["capped"],
        )

    def iter(
        self,
        *,
        environment: str | None = None,
        namespace: str | None = None,
        q: str | None = None,
        model_kind: str | None = None,
        sort: str = "name",
        order: str | None = None,
        limit: int | None = None,
        lang: str | None = None,
    ) -> Iterator[TableListItem]:
        """Yield every table you may read, fetching pages as needed.

        Parameters
        ----------
        environment
            Only this environment.
        namespace
            Only this namespace and the ones beneath it, dotted.
        q
            Only tables matching this text; see `list`.
        model_kind
            Only tables whose asset header declares this kind.
        sort
            What the list is sorted by; see `list`.
        order
            ``asc`` or ``desc``.
        limit
            Tables per request, 1 to 200.
        lang
            The language to match translated texts in.

        Yields
        ------
        TableListItem
            Each table, once.
        """
        cursor: str | None = None
        while True:
            page = self.list(
                environment=environment,
                namespace=namespace,
                q=q,
                model_kind=model_kind,
                sort=sort,
                order=order,
                limit=limit,
                cursor=cursor,
                lang=lang,
            )
            yield from page.items
            if page.next_cursor is None:
                return
            cursor = page.next_cursor

    def tree(self) -> TableTree:
        """Count the tables you may read, by environment and namespace.

        Returns
        -------
        TableTree
            The counts; an environment or namespace you may not see is left out.
        """
        body = self._transport.request(ROUTES["catalog.tree"]).json()
        return TableTree(
            count=body["count"],
            environments=[
                TreeEnvironment(
                    name=env["name"],
                    count=env["count"],
                    namespaces=[TreeNamespace(n["path"], n["count"]) for n in env["namespaces"]],
                )
                for env in body["environments"]
            ],
        )
