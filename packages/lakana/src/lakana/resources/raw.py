"""The `raw` resource: `Client.raw`, the folders and files under an environment's `raw/`."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from lakana._http import HttpTransport, every_page, query_params
from lakana._models import RawClaim, RawEntry, RawPage
from lakana._routes import ROUTES
from lakana.errors import InvalidResponse
from lakana.resources.artifacts import _ODD_ANSWER, _object

_WHAT = "the listing of raw/"


def _page_from_json(data: dict[str, Any]) -> RawPage:
    """Build a `RawPage` from a `RawPageOut` body; a field this SDK does not know is ignored.

    Parameters
    ----------
    data
        The decoded object.

    Returns
    -------
    RawPage
        The page.

    Raises
    ------
    InvalidResponse
        If the object is not a page of entries.
    """
    try:
        items = data["items"]
        cursor = data["next_cursor"]
        assert isinstance(items, list) and (cursor is None or isinstance(cursor, str))  # nosec B101 -- an untrusted answer; caught just below
        entries = [
            RawEntry(
                name=item["name"],
                kind=item["kind"],
                size=item["size"],
                claimed_by=None
                if item["claimed_by"] is None
                else RawClaim(table=item["claimed_by"]["table"]),
            )
            for item in items
        ]
        return RawPage(path=data["path"], items=entries, next_cursor=cursor)
    except (AssertionError, KeyError, TypeError):
        raise InvalidResponse(_ODD_ANSWER.format(what=_WHAT)) from None


class Raw:
    """The `raw` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def list(
        self,
        environment: str,
        path: str = "raw",
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> RawPage:
        """List one page of a folder of `raw/`: its folders and files, in key order.

        Needs `CREATE_TABLE` on some namespace of the environment; each folder read is recorded
        in the audit log.

        Parameters
        ----------
        environment
            The environment.
        path
            The folder: ``"raw"`` or a folder below it, such as ``"raw/orders"``.
        limit
            Most entries returned (1 to 200); `None` leaves it to the server (100).
        cursor
            A previous page's `next_cursor`.

        Returns
        -------
        RawPage
            The entries, and the cursor of the next page (`None` on the last).

        Raises
        ------
        lakana.errors.PermissionDenied
            If you may register an external table in no namespace of the environment.
        lakana.errors.NotFound
            If the environment does not exist.
        lakana.errors.ValidationError
            If `path` is not `raw` or a folder below it.
        """
        response = self._transport.request(
            ROUTES["raw.list"],
            path_params={"env": environment},
            params=query_params(path=path, limit=limit, cursor=cursor),
        )
        return _page_from_json(_object(response, _WHAT))

    def iter(
        self, environment: str, path: str = "raw", *, page_size: int = 100
    ) -> Iterator[RawEntry]:
        """Yield every entry of a folder of `raw/`, fetching pages as needed.

        Parameters
        ----------
        environment
            The environment.
        path
            The folder.
        page_size
            Entries per request (1 to 200).

        Returns
        -------
        Iterator of RawEntry
            Each entry, once.
        """
        return every_page(
            lambda cursor: self.list(environment, path, limit=page_size, cursor=cursor)
        )
