"""The `tokens` resource (PATs): `Client.tokens`."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lakana._http import HttpTransport
from lakana._models import IssuedToken, Token
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
    last_used_at = data["last_used_at"]
    return Token(
        key_id=data["key_id"],
        name=data["name"],
        last_used_at=None if last_used_at is None else datetime.fromisoformat(last_used_at),
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
