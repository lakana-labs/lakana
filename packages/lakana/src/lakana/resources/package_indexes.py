"""The `package_indexes` resource: `Client.package_indexes`."""

from __future__ import annotations

from datetime import datetime

from lakana._http import HttpTransport
from lakana._models import PackageIndex
from lakana._routes import ROUTES
from lakana.errors import InvalidResponse
from lakana.resources.artifacts import _ODD_ANSWER, _array, _object

UPDATABLE = frozenset({"url", "credential_secret"})
"""Fields `update` may change; mirrors the server."""


def _package_index_from_json(data: object) -> PackageIndex:
    """Build a `PackageIndex` from a `PackageIndexOut` body.

    A field the server may leave out (`credential_secret`, `created_by`) reads as `None`, and
    a field this SDK does not know is ignored.

    Parameters
    ----------
    data
        The decoded body.

    Returns
    -------
    PackageIndex
        The index.

    Raises
    ------
    InvalidResponse
        If `data` is not an object with an id, a name, a URL and both times.
    """
    try:
        assert isinstance(data, dict)  # nosec B101 -- an untrusted answer; caught just below
        return PackageIndex(
            id=data["id"],
            name=data["name"],
            url=data["url"],
            credential_secret=data.get("credential_secret"),
            created_by=data.get("created_by"),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        raise InvalidResponse(_ODD_ANSWER.format(what="a package index")) from None


_PackageIndexList = list[PackageIndex]
"""`list[PackageIndex]` resolved before `PackageIndexes.list` shadows the builtin."""


class PackageIndexes:
    """The `package_indexes` resource of the platform API.

    Parameters
    ----------
    transport
        The HTTP transport.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def create(self, name: str, url: str, *, credential_secret: str | None = None) -> PackageIndex:
        """Register a package index (needs `MANAGE_PACKAGE_INDEX`).

        Parameters
        ----------
        name
            Unique name; no dots, no control or format character, no line
            separator and no space at either end.
        url
            ``https://host/path`` of the index, without a user name or password.
        credential_secret
            Name of the secret that holds ``username`` and ``password``, per environment.

        Returns
        -------
        PackageIndex
            The registered index.

        Raises
        ------
        lakana.errors.Conflict
            If the name or the URL is taken.
        lakana.errors.ValidationError
            If a field is invalid.
        """
        response = self._transport.request(
            ROUTES["package_indexes.create"],
            json={"name": name, "url": url, "credential_secret": credential_secret},
        )
        return _package_index_from_json(_object(response, "a package index"))

    def list(self) -> _PackageIndexList:
        """List package indexes.

        Returns
        -------
        list of PackageIndex
            Sorted by name.
        """
        response = self._transport.request(ROUTES["package_indexes.list"])
        return [_package_index_from_json(item) for item in _array(response, "the package indexes")]

    def get(self, name: str) -> PackageIndex:
        """Show a package index.

        Parameters
        ----------
        name
            Its name.

        Returns
        -------
        PackageIndex
            The index.

        Raises
        ------
        lakana.errors.NotFound
            If none has that name.
        """
        response = self._transport.request(
            ROUTES["package_indexes.get"], path_params={"name": name}
        )
        return _package_index_from_json(_object(response, "a package index"))

    def update(self, name: str, **changes: str | None) -> PackageIndex:
        """Change a package index; `None` clears the credential secret.

        Parameters
        ----------
        name
            Its name.
        **changes
            Any of `url`, `credential_secret`.

        Returns
        -------
        PackageIndex
            The updated index.

        Raises
        ------
        TypeError
            For any other keyword.
        """
        unknown = set(changes) - UPDATABLE
        if unknown:
            raise TypeError(f"update() got unexpected field(s): {', '.join(sorted(unknown))}")
        response = self._transport.request(
            ROUTES["package_indexes.update"], path_params={"name": name}, json=changes
        )
        return _package_index_from_json(_object(response, "a package index"))

    def delete(self, name: str) -> None:
        """Delete a package index.

        Parameters
        ----------
        name
            Its name.
        """
        self._transport.request(ROUTES["package_indexes.delete"], path_params={"name": name})
