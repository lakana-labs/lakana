"""The `saved_queries` resource: `Client.saved_queries`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from lakana._http import HttpTransport, every_page, query_params
from lakana._models import SavedQuery, SavedQueryPage
from lakana._routes import ROUTES


def _saved_query_from_json(data: dict[str, Any]) -> SavedQuery:
    """Build a `SavedQuery` from a `SavedQueryOut` body.

    Parameters
    ----------
    data
        The decoded saved query. It has no `sql` key when the caller may not read the SQL.

    Returns
    -------
    SavedQuery
        The saved query, with `sql` `None` when the key is missing.
    """
    return SavedQuery(
        id=data["id"],
        environment=data["environment"],
        name=data["name"],
        sql=data.get("sql"),
        visibility=data["visibility"],
        owner=data["owner"],
        former_owner=data["former_owner"],
        actions=tuple(data["actions"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        updated_at=datetime.fromisoformat(data["updated_at"]),
    )


class SavedQueries:
    """Saved queries: SQL kept under a name, private or shared with the environment.

    A saved query is text and an environment. Running it is an ordinary query
    (`client.queries.submit`) by whoever runs it, with their own rights.

    Parameters
    ----------
    transport
        The shared HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(
        self,
        environment: str | None = None,
        *,
        limit: int = 50,
        cursor: str | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> SavedQueryPage:
        """List one page of the saved queries you see, by name.

        You see your own, the shared ones (``everyone``) of an environment where you may submit
        queries, and, as an instance administrator, every one. The items carry no SQL (their
        `sql` is `None`); `get` reads it. Without `environment`, the list spans every environment
        you may query.

        Parameters
        ----------
        environment
            Only this environment; `None` for every environment.
        limit
            Most items returned (1 to 200).
        cursor
            A previous page's `next_cursor` of the same list and sort.
        sort
            What the list is sorted by: ``name`` (the default, in any letter case), ``updated``
            or ``created``; every sort lists the same saved queries.
        order
            ``asc`` or ``desc``; by default ``asc`` for ``name``, ``desc`` for the times.

        Returns
        -------
        SavedQueryPage
            The saved queries, and the cursor of the next page (`None` on the last).

        Raises
        ------
        lakana.errors.PermissionDenied
            With `environment`, if you may not query it and own no saved query there.
        lakana.errors.NotFound
            With `environment`, if it does not exist.
        lakana.errors.ValidationError
            If `limit` is outside 1 to 200, `sort` or `order` is unknown, or `cursor` is not
            one the server issued for this list in this sort.
        """
        if environment is None:
            response = self._transport.request(
                ROUTES["saved_queries.list_all"],
                params=query_params(limit=limit, cursor=cursor, sort=sort, order=order),
            )
        else:
            response = self._transport.request(
                ROUTES["saved_queries.list"],
                path_params={"env": environment},
                params=query_params(limit=limit, cursor=cursor, sort=sort, order=order),
            )
        body = response.json()
        return SavedQueryPage(
            items=[_saved_query_from_json(item) for item in body["items"]],
            next_cursor=body["next_cursor"],
        )

    def iter(
        self,
        environment: str | None = None,
        *,
        sort: str | None = None,
        order: str | None = None,
    ) -> Iterator[SavedQuery]:
        """Yield every saved query you see, fetching pages as needed.

        Parameters
        ----------
        environment
            Only this environment; `None` for every environment.
        sort
            The sort, as `list` takes it.
        order
            ``asc`` or ``desc``, as `list` takes it.

        Yields
        ------
        SavedQuery
            Each saved query, once, in the order of `list`.

        Raises
        ------
        lakana.errors.PermissionDenied
            As `list`.
        lakana.errors.NotFound
            As `list`.
        """
        yield from every_page(
            lambda cursor: self.list(environment, cursor=cursor, sort=sort, order=order)
        )

    def create(
        self, environment: str, name: str, sql: str, *, visibility: str = "private"
    ) -> SavedQuery:
        """Save a query under your name in an environment.

        Parameters
        ----------
        environment
            The environment; you need `SUBMIT_QUERY` there.
        name
            The name: one line of 1 to 200 visible characters, unique among your saved queries
            in the environment.
        sql
            The SQL, stored as sent; not empty, and no longer than the server's statement limit.
        visibility
            ``private`` (only you) or ``everyone`` (whoever may query the environment).

        Returns
        -------
        SavedQuery
            The saved query.

        Raises
        ------
        lakana.errors.PermissionDenied
            If you may not submit queries in the environment, or use a task token.
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.Conflict
            If you already have a saved query of this name there; the sentence names its id.
        lakana.errors.ValidationError
            If the name or the SQL is not allowed, or `visibility` is unknown.
        """
        response = self._transport.request(
            ROUTES["saved_queries.create"],
            path_params={"env": environment},
            json={"name": name, "sql": sql, "visibility": visibility},
        )
        return _saved_query_from_json(response.json())

    def get(self, environment: str, saved_query_id: str) -> SavedQuery:
        """Read one saved query.

        Parameters
        ----------
        environment
            The environment.
        saved_query_id
            The saved query's id.

        Returns
        -------
        SavedQuery
            The saved query; its `sql` is `None` when you may not read it.

        Raises
        ------
        lakana.errors.NotFound
            If there is no such saved query that you see.
        lakana.errors.PermissionDenied
            For a task token.
        """
        response = self._transport.request(
            ROUTES["saved_queries.get"],
            path_params={"env": environment, "saved_query_id": saved_query_id},
        )
        return _saved_query_from_json(response.json())

    def update(
        self,
        environment: str,
        saved_query_id: str,
        *,
        name: str,
        sql: str,
        visibility: str,
    ) -> SavedQuery:
        """Replace a saved query's name, SQL and visibility; only its owner may.

        The call replaces every field: read the saved query first to keep one as it is.

        Parameters
        ----------
        environment
            The environment.
        saved_query_id
            The saved query's id.
        name
            The new name, as in `create`.
        sql
            The new SQL, as in `create`.
        visibility
            ``private`` or ``everyone``.

        Returns
        -------
        SavedQuery
            The saved query as changed.

        Raises
        ------
        lakana.errors.NotFound
            If there is no such saved query that you see.
        lakana.errors.PermissionDenied
            If you are not the owner, or no longer hold `SUBMIT_QUERY` in the environment.
        lakana.errors.Conflict
            If you already have another saved query of this name there.
        lakana.errors.ValidationError
            If the name or the SQL is not allowed.
        """
        response = self._transport.request(
            ROUTES["saved_queries.update"],
            path_params={"env": environment, "saved_query_id": saved_query_id},
            json={"name": name, "sql": sql, "visibility": visibility},
        )
        return _saved_query_from_json(response.json())

    def delete(self, environment: str, saved_query_id: str) -> None:
        """Delete a saved query; its owner or an administrator may.

        Parameters
        ----------
        environment
            The environment.
        saved_query_id
            The saved query's id.

        Raises
        ------
        lakana.errors.NotFound
            If there is no such saved query that you see.
        lakana.errors.PermissionDenied
            If you are neither its owner nor an administrator.
        """
        self._transport.request(
            ROUTES["saved_queries.delete"],
            path_params={"env": environment, "saved_query_id": saved_query_id},
        )
