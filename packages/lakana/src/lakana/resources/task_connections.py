"""The `task_connections` resource: `Client.task_connections`, for helpers inside a task."""

from __future__ import annotations

from typing import Any

from lakana._http import HttpTransport
from lakana._routes import ROUTES
from lakana.resources.artifacts import _object


class TaskConnections:
    """The run-time fetch of a connection, for the helpers of `lakana.ingest`.

    Use `lakana.ingest.connections.get` in an asset instead: it fetches once per attempt, masks
    the secret in its `repr` and has the harness hide the values in everything it writes.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def get(self, name: str) -> dict[str, Any]:
        """Fetch a connection the running task's asset declares, with its secret's values.

        Answered only for the task's own token while its attempt runs; every call is in the
        audit log.

        Parameters
        ----------
        name
            The connection's name.

        Returns
        -------
        dict
            The answer as the server sent it: ``name``, ``kind``, ``address``, ``settings``,
            ``endpoints`` and ``secret`` (the values, or `None`).

        Raises
        ------
        lakana.errors.PermissionDenied
            For any token but a running task's, a name the asset does not declare, or a
            declared name the environment lacks; the message says which.
        lakana.errors.AuthenticationError
            For a token whose attempt has ended.
        """
        response = self._transport.request(
            ROUTES["task_connections.get"], path_params={"name": name}
        )
        return _object(response, "a connection")
