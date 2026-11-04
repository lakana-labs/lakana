"""The `graph` resource: `Client.graph`, the metadata graph of an environment."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from lakana._http import HttpTransport, _next_cursor, query_params
from lakana._models import Graph, GraphEdge, GraphNode, JoinPath, JoinPaths, JoinStep, known_fields
from lakana._routes import ROUTES
from lakana.errors import InvalidResponse, NotFound
from lakana.resources.artifacts import _ODD_ANSWER, _object
from lakana.resources.terms import _no_routes


def _join_paths_from_json(data: dict[str, Any]) -> JoinPaths:
    """Read a join path answer; a field this SDK does not know is ignored.

    Parameters
    ----------
    data
        The decoded object of a `JoinPathsOut`.

    Returns
    -------
    JoinPaths
        The answer.

    Raises
    ------
    InvalidResponse
        If the answer is not shaped like a `JoinPathsOut`.
    """
    try:
        paths = []
        for path in data["paths"]:
            steps = []
            for step in path["steps"]:
                fields = known_fields(JoinStep, step)
                fields["from_columns"] = tuple(step["from_columns"])
                fields["to_columns"] = tuple(step["to_columns"])
                steps.append(JoinStep(**fields))
            paths.append(JoinPath(tuple(steps)))
        return JoinPaths(tuple(paths), data["reason"], bool(data["more_paths"]))
    except (AttributeError, KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="the join paths")) from None


def _page_from_json(data: dict[str, Any]) -> tuple[list[GraphNode], list[GraphEdge], str | None]:
    """Read one page of an answer; a field this SDK does not know is ignored.

    Parameters
    ----------
    data
        The decoded object of a `GraphOut`.

    Returns
    -------
    tuple
        The page's nodes, its edges, and the cursor of the next page (`None` on the last).

    Raises
    ------
    InvalidResponse
        If the page is not shaped like a `GraphOut`.
    """
    odd = InvalidResponse(_ODD_ANSWER.format(what="the graph"))
    try:
        nodes = []
        for item in data["nodes"]:
            fields = known_fields(GraphNode, item)
            if fields.get("key") is not None:
                fields["key"] = tuple(fields["key"])
            nodes.append(GraphNode(**fields))
        edges = [GraphEdge(**known_fields(GraphEdge, item)) for item in data["edges"]]
        cursor = data["next_cursor"]
        if cursor is not None and not isinstance(cursor, str):
            raise odd
        return nodes, edges, cursor
    except (AttributeError, KeyError, TypeError, ValueError):
        raise odd from None


class MetadataGraph:
    """The `graph` resource of the platform API: lineage, neighbours, impact and join paths.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def _every_page(self, route: str, environment: str, params: dict[str, object]) -> Graph:
        """Ask one question and read every page of its answer into one `Graph`.

        Parameters
        ----------
        route
            The key of the route in `ROUTES`.
        environment
            The environment.
        params
            The question's query parameters.

        Returns
        -------
        Graph
            The nodes and the edges of all pages.

        Raises
        ------
        lakana.errors.NotFound
            If the environment or the node does not exist or is hidden, or the server has no
            graph routes.
        lakana.errors.InvalidResponse
            If a page is not shaped like a `GraphOut`, or the server repeats a cursor.
        """
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            try:
                response = self._transport.request(
                    ROUTES[route],
                    path_params={"env": environment},
                    params={**params, **({"cursor": cursor} if cursor else {})},
                )
            except NotFound as error:
                translated = _no_routes(error, "metadata graph", 374)
                raise translated from None
            page_nodes, page_edges, cursor = _page_from_json(_object(response, "the graph"))
            nodes += page_nodes
            edges += page_edges
            if cursor is None:
                return Graph(tuple(nodes), tuple(edges))
            _next_cursor(cursor, seen)

    def lineage(
        self,
        environment: str,
        node: str,
        *,
        direction: str = "both",
        depth: int | None = None,
        lang: str | None = None,
        page_size: int | None = None,
    ) -> Graph:
        """Walk the tables and assets upstream and/or downstream of a table, column or asset.

        Parameters
        ----------
        environment
            The environment.
        node
            A node id, or a table's dotted name; a column as ``column:gold.dim_store/store_id``.
        direction
            ``up``, ``down`` or ``both``.
        depth
            Assets to cross, 1 to 10; `None` leaves it to the server (3).
        lang
            The language of labels and grains.
        page_size
            Nodes per request (1 to 500); `None` leaves it to the server (500).

        Returns
        -------
        Graph
            The nodes and the edges, every page read.

        Raises
        ------
        lakana.errors.NotFound
            If the environment or the node does not exist or you may not see it, or the server
            has no graph routes.
        """
        return self._every_page(
            "graph.lineage",
            environment,
            query_params(node=node, direction=direction, depth=depth, lang=lang, limit=page_size),
        )

    def neighbours(
        self,
        environment: str,
        node: str,
        *,
        kinds: Sequence[str] = (),
        lang: str | None = None,
        page_size: int | None = None,
    ) -> Graph:
        """List the nodes one edge away from a node.

        Parameters
        ----------
        environment
            The environment.
        node
            A node id, or a table's dotted name; a column as ``column:gold.dim_store/store_id``.
        kinds
            Only edges of these kinds (``reads``, ``builds``, ``references``, ``measures``,
            ``checks``, ``filters``, ``masks``, ``links``, ``runs``, ``has_column``).
        lang
            The language of labels and names.
        page_size
            Nodes per request (1 to 500); `None` leaves it to the server (500).

        Returns
        -------
        Graph
            The node first, then its neighbours, every page read.

        Raises
        ------
        lakana.errors.NotFound
            If the environment or the node does not exist or you may not see it, or the server
            has no graph routes.
        """
        params = query_params(node=node, lang=lang, limit=page_size)
        if kinds:
            params["kinds"] = list(kinds)
        return self._every_page("graph.neighbours", environment, params)

    def impact(
        self,
        environment: str,
        node: str,
        *,
        key: bool = False,
        lang: str | None = None,
        page_size: int | None = None,
    ) -> Graph:
        """List everything declared that depends on a table, a column or (``key=True``) its key.

        Each node but the start carries `reason`, such as ``references gold.dim_store.store_id``;
        for a column or a key, `may_use` marks what only may use it: the table's hand-written
        checks, its row filters and everything downstream, because SQL is not parsed.

        Parameters
        ----------
        environment
            The environment.
        node
            A table's dotted name or id, or a column as ``column:gold.dim_store/store_id``.
        key
            Ask about the table's declared key instead of the whole table.
        lang
            The language of labels and names.
        page_size
            Nodes per request (1 to 500); `None` leaves it to the server (500).

        Returns
        -------
        Graph
            The start first, then what depends on it, every page read.

        Raises
        ------
        lakana.errors.NotFound
            If the environment or the node does not exist or you may not see it, or the server
            has no graph routes.
        lakana.errors.ValidationError
            If the node is not a table or a column, or `key` is set for a column or a table that
            declares no key.
        """
        return self._every_page(
            "graph.impact",
            environment,
            query_params(node=node, key="true" if key else None, lang=lang, limit=page_size),
        )

    def join_path(
        self, environment: str, from_table: str, to_table: str, *, lang: str | None = None
    ) -> JoinPaths:
        """Show how two tables join: every declared path of minimal length over references.

        A path has at most 4 steps and at most 10 paths are returned; `more_paths` says when
        more exist. A step with `fans_out` goes from one row to possibly many (``one_to_many``).

        Parameters
        ----------
        environment
            The environment.
        from_table
            The table to start from: its dotted name or id.
        to_table
            The table to reach.
        lang
            The language of grains.

        Returns
        -------
        JoinPaths
            The paths, or none with the reason.

        Raises
        ------
        lakana.errors.NotFound
            If a table or the environment does not exist or you may not see it, or the server
            has no graph routes.
        lakana.errors.ValidationError
            If either is not a table, or both are the same table.
        lakana.errors.InvalidResponse
            If the answer is not shaped like a `JoinPathsOut`.
        """
        try:
            response = self._transport.request(
                ROUTES["graph.join_path"],
                path_params={"env": environment},
                params=query_params(**{"from": from_table, "to": to_table, "lang": lang}),
            )
        except NotFound as error:
            translated = _no_routes(error, "metadata graph", 374)
            raise translated from None
        return _join_paths_from_json(_object(response, "the join paths"))
