"""The `tokens` resource (PATs): `Client.tokens`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from lakana._http import NEXT_CURSOR_HEADER, HttpTransport, every_page, query_params
from lakana._models import IssuedToken, Token, TokenPage
from lakana._routes import ROUTES


def _token_from_json(data: dict[str, Any]) -> Token:
    """Build a `Token` from the platform API's JSON response body.

    Parameters
    ----------
    data
        The decoded `TokenOut` JSON body.

    Returns
    -------
    Token
        The parsed token, never carrying a secret.
    """

    def moment(key: str) -> datetime | None:
        """Read a time from the body; a missing or null key is `None`.

        Parameters
        ----------
        key
            The body's key.

        Returns
        -------
        datetime or None
            The time, if the server sent one.
        """
        value = data.get(key)
        return None if value is None else datetime.fromisoformat(value)

    return Token(
        key_id=data["key_id"],
        name=data["name"],
        last_used_at=moment("last_used_at"),
        owner=data.get("owner"),
        created_at=moment("created_at"),
        expires_at=moment("expires_at"),
        revoked_at=moment("revoked_at"),
    )


_TokenList = list[Token]
"""Alias for `list[Token]`, resolved at module scope.

`Tokens` defines a method named `list`, which shadows the builtin `list`
for any bare `list[...]` annotation written *inside* the class body — this
alias is resolved before that happens, and referenced by name instead.
"""


class Tokens:
    """The `tokens` (PAT) resource of the platform API, nested under a principal.

    Parameters
    ----------
    transport
        The HTTP transport to send requests through.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def issue(self, principal: str, name: str) -> IssuedToken:
        """Issue a PAT for a principal.

        Parameters
        ----------
        principal
            The principal the PAT authenticates as.
        name
            A human label for this PAT (e.g. `"laptop"`, `"CI"`); no control or format
            character, no line separator and no space at either end.

        Returns
        -------
        IssuedToken
            The full token value — shown here exactly once.

        Raises
        ------
        lakana.errors.NotFound
            If no principal of this name exists.
        """
        response = self._transport.request(
            ROUTES["tokens.issue"], path_params={"name": principal}, json={"name": name}
        )
        body = response.json()
        return IssuedToken(key_id=body["key_id"], name=body["name"], token=body["token"])

    def list(self, principal: str) -> _TokenList:
        """List every PAT issued to a principal. Never includes a secret.

        Parameters
        ----------
        principal
            The principal whose PATs to list.

        Returns
        -------
        list of Token
            Every PAT for that principal.

        Raises
        ------
        lakana.errors.NotFound
            If no principal of this name exists.
        """
        response = self._transport.request(ROUTES["tokens.list"], path_params={"name": principal})
        return [_token_from_json(item) for item in response.json()]

    def list_all(
        self, *, owner: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> _TokenList:
        """List every owner's live PATs, newest first. Needs the ADMIN role.

        The tokens of one page; `list_all_page` also returns the cursor of the next one, and
        `iter_all` walks every page. Revoked and expired tokens are left out.

        Parameters
        ----------
        owner
            Only this principal's tokens.
        limit
            Most tokens returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `owner` with every page.

        Returns
        -------
        list of Token
            Newest first, each with its owner.

        Raises
        ------
        lakana.errors.NotFound
            If `owner` names no principal.
        lakana.errors.PermissionDenied
            If you do not hold the ADMIN role on the instance.
        """
        return self.list_all_page(owner=owner, limit=limit, cursor=cursor).items

    def list_all_page(
        self, *, owner: str | None = None, limit: int | None = None, cursor: str | None = None
    ) -> TokenPage:
        """List one page of every owner's live PATs, with the cursor of the next page.

        Parameters
        ----------
        owner
            Only this principal's tokens.
        limit
            Most tokens returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`. Send the same `owner` with every page.

        Returns
        -------
        TokenPage
            The tokens, and the cursor for the next page (`None` on the last).

        Raises
        ------
        lakana.errors.NotFound
            If `owner` names no principal.
        lakana.errors.PermissionDenied
            If you do not hold the ADMIN role on the instance.
        """
        response = self._transport.request(
            ROUTES["tokens.list_all"], params=query_params(owner=owner, limit=limit, cursor=cursor)
        )
        return TokenPage(
            items=[_token_from_json(item) for item in response.json()],
            next_cursor=response.headers.get(NEXT_CURSOR_HEADER),
        )

    def iter_all(self, *, owner: str | None = None, page_size: int = 100) -> Iterator[Token]:
        """Yield every owner's live PATs, newest first, fetching pages as needed.

        Parameters
        ----------
        owner
            Only this principal's tokens.
        page_size
            Tokens per request (1 to 200).

        Returns
        -------
        Iterator of Token
            Each token, once.
        """
        return every_page(
            lambda cursor: self.list_all_page(owner=owner, limit=page_size, cursor=cursor)
        )

    def revoke(self, principal: str, key_id: str) -> None:
        """Revoke a PAT.

        Parameters
        ----------
        principal
            The principal the PAT belongs to.
        key_id
            The PAT's key_id.

        Raises
        ------
        lakana.errors.NotFound
            If no principal or PAT of these identifiers exists.
        """
        self._transport.request(
            ROUTES["tokens.revoke"], path_params={"name": principal, "key_id": key_id}
        )
