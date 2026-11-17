"""The `task_tokens` resource: `Client.task_tokens`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, every_page, query_params
from lakana._models import MintedTaskToken, TaskToken, TaskTokenPage
from lakana._routes import ROUTES

_TaskTokenList = list[TaskToken]
_Scope = list[str]


def _task_token_from_json(data: dict[str, Any]) -> TaskToken:
    """Read one listed task token from a response body.

    Parameters
    ----------
    data
        One element of the list body.

    Returns
    -------
    TaskToken
        The parsed token, never carrying a secret.
    """
    run_id = data.get("run_id")
    return TaskToken(
        key_id=data["key_id"],
        principal=data["principal"],
        environment=data["environment"],
        asset_scope=list(data["asset_scope"]),
        issued_at=datetime.fromisoformat(data["issued_at"]),
        expires_at=datetime.fromisoformat(data["expires_at"]),
        run_id=None if run_id is None else str(run_id),
        job=data.get("job"),
        asset=data.get("asset"),
    )


class TaskTokens:
    """The `task-tokens` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(
        self, *, environment: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> _TaskTokenList:
        """List the live task tokens, newest first. Needs the ADMIN role.

        The tokens of one page; `list_page` also returns the cursor of the next one, and `iter`
        walks every page. Revoked and expired tokens are left out.

        Parameters
        ----------
        environment
            Only this environment's tokens; every environment when `None`.
        limit
            Most tokens returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `environment` with every page.

        Returns
        -------
        list of TaskToken
            Newest first, each with its principal and, for a token bound to a task, its run.

        Raises
        ------
        lakana.errors.PermissionDenied
            If you do not hold the ADMIN role on the instance.
        """
        return self.list_page(environment=environment, limit=limit, cursor=cursor).items

    def list_page(
        self, *, environment: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> TaskTokenPage:
        """List one page of the live task tokens, with the cursor of the next page.

        Parameters
        ----------
        environment
            Only this environment's tokens; every environment when `None`.
        limit
            Most tokens returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `environment` with every page.

        Returns
        -------
        TaskTokenPage
            The tokens, and the cursor for the next page (`None` on the last).

        Raises
        ------
        lakana.errors.PermissionDenied
            If you do not hold the ADMIN role on the instance.
        """
        response = self._transport.request(
            ROUTES["task_tokens.list"],
            params=query_params(environment=environment, limit=limit, cursor=cursor),
        )
        return TaskTokenPage(
            items=[_task_token_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    def iter(self, *, environment: str | None = None, page_size: int = 100) -> Iterator[TaskToken]:
        """Yield every live task token, newest first, fetching pages as needed.

        Parameters
        ----------
        environment
            Only this environment's tokens; every environment when `None`.
        page_size
            Tokens per request (1 to 200).

        Returns
        -------
        Iterator of TaskToken
            Each token, once.

        Raises
        ------
        lakana.errors.PermissionDenied
            If you do not hold the ADMIN role on the instance.
        """
        return every_page(
            lambda cursor: self.list_page(environment=environment, limit=page_size, cursor=cursor)
        )

    def mint(
        self, principal: str, environment: str, asset_scope: _Scope, ttl_seconds: int
    ) -> MintedTaskToken:
        """Mint a task token.

        Parameters
        ----------
        principal
            The identity the token authenticates as: your own name, unless you
            hold the `ADMIN` role on the instance.
        environment
            The one environment this task may touch.
        asset_scope
            The declared asset name/pattern strings, e.g. `["bronze.*"]`.
        ttl_seconds
            Seconds until the token expires, from now. Capped server-side by
            the deployment's configured maximum.

        Returns
        -------
        MintedTaskToken
            The full token value — shown here exactly once.

        Raises
        ------
        lakana.errors.NotFound
            If no principal of this name exists.
        lakana.errors.PermissionDenied
            If minting for another principal without the `ADMIN` role.
        lakana.errors.ValidationError
            If `asset_scope` contains a malformed pattern, or `ttl_seconds`
            exceeds the deployment's configured maximum.
        """
        response = self._transport.request(
            ROUTES["task_tokens.mint"],
            json={
                "principal": principal,
                "environment": environment,
                "asset_scope": asset_scope,
                "ttl_seconds": ttl_seconds,
            },
        )
        body = response.json()
        return MintedTaskToken(key_id=body["key_id"], token=body["token"])

    def revoke(self, key_id: str) -> None:
        """Revoke a task token before it expires; revoking one already revoked is a no-op.

        Parameters
        ----------
        key_id
            The task token's key_id.

        Raises
        ------
        lakana.errors.NotFound
            If no task token has this key_id.
        lakana.errors.PermissionDenied
            If the caller may not mint task tokens.
        """
        self._transport.request(ROUTES["task_tokens.revoke"], path_params={"key_id": key_id})
