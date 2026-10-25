"""Read a connection the asset declares: ``connections.get("crm-db")``, inside a Lakana task.

Example::

    from lakana import asset
    from lakana.ingest import connections


    @asset(connections=["crm-db"])
    def customers():
        crm = connections.get("crm-db")
        crm.address["host"], crm.settings["user"], crm.secret["password"]
        ...

The secret is fetched from the server with the task's own token the first time the attempt asks
for it, kept in memory for the attempt, and hidden (``***``) in the task's log, its failure
message and its trace. Its `repr` shows ``***`` for each value.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from lakana import context
from lakana.ingest._connections import Endpoint, fetch

_PROXY_KINDS = frozenset({"http", "s3", "gcs", "azure"})
"""Kinds an isolated task reaches through the egress proxy (`LAKANA_EGRESS_PROXY`)."""
_RELAY_HOST = "lakana-relay"
"""The name an isolated task resolves to its relay."""


class MaskedMapping(Mapping[str, str]):
    """A secret's values, readable by key, shown as ``***`` by `repr` and `str`.

    Parameters
    ----------
    values
        The values.
    """

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def __getitem__(self, key: str) -> str:
        """Read one value.

        Parameters
        ----------
        key
            Its key, such as ``password``.

        Returns
        -------
        str
            The value.
        """
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        """Iterate over the keys.

        Returns
        -------
        Iterator of str
            The keys.
        """
        return iter(self._values)

    def __len__(self) -> int:
        """Count the values.

        Returns
        -------
        int
            How many there are.
        """
        return len(self._values)

    def __repr__(self) -> str:
        """Show the keys, each value as ``***``.

        Returns
        -------
        str
            Such as ``{'password': '***'}``.
        """
        return repr(dict.fromkeys(self._values, "***"))

    __str__ = __repr__


@dataclass(frozen=True)
class Connection:
    """A connection the asset declares, as the running task may use it.

    Parameters
    ----------
    name
        Its name.
    kind
        ``postgresql``, ``mariadb``, ``sqlite-file``, ``http``, ``s3``, ``gcs``, ``azure``,
        ``directory`` or ``tcp``.
    address
        Where the source is (``host``, ``port``, ``database``; ``base_url``; ...).
    settings
        How to reach it (``user``, ``tls``, ``ca_certificate``; ``auth``; ...).
    endpoints
        Every host and port the connection reaches.
    secret
        Its secret's values (``password``, ``token``, ...), or `None` when it has none.
    proxy_url
        The proxy an HTTPS client of the task goes through (``http``, ``s3``, ``gcs`` and
        ``azure``). It holds the task's egress token: left out of `repr` and hidden in the
        task's log. `None` when the task does not run isolated; connect directly then.
    relay_address
        ``lakana-relay:<port>``, which a database client of the task connects to instead of
        the host (``postgresql``, ``mariadb`` and ``tcp``). `None` when the task does not run
        isolated; connect directly to `address` then.
    root_path
        The directory or file a ``directory`` or ``sqlite-file`` connection reads, under either
        runner; `None` for the other kinds.
    """

    name: str
    kind: str
    address: Mapping[str, object]
    settings: Mapping[str, object]
    endpoints: tuple[Endpoint, ...]
    secret: MaskedMapping | None = field(repr=False)
    proxy_url: str | None = field(default=None, repr=False)
    relay_address: str | None = None
    root_path: str | None = None


def get(name: str) -> Connection:
    """Return a connection the asset declares, fetching it on the attempt's first call.

    Parameters
    ----------
    name
        The connection's name, as in ``@asset(connections=[...])``.

    Returns
    -------
    Connection
        The connection, with its secret.

    Raises
    ------
    lakana.ingest.ConnectionNotDeclared
        For a name the asset does not declare: add it to ``@asset(connections=[...])`` and
        deploy.
    lakana.ingest.ConnectionRefused
        When the server refuses; the message says why.
    lakana.ingest.ConnectionChanged
        When the connection changed after the attempt started; the next attempt uses the new
        address.
    RuntimeError
        Outside a running task.
    """
    found = fetch(name)
    proxy_url = os.environ.get("LAKANA_EGRESS_PROXY") if found.kind in _PROXY_KINDS else None
    if proxy_url:
        token = urlsplit(proxy_url).password
        if token:
            context.register_secret(token)
    relay_port = found.routing.get("relay_port")
    root_path = found.routing.get("root_path")
    return Connection(
        name=found.name,
        kind=found.kind,
        address=found.address,
        settings=found.settings,
        endpoints=found.endpoints,
        secret=None if found.secret is None else MaskedMapping(found.secret),
        proxy_url=proxy_url,
        relay_address=f"{_RELAY_HOST}:{relay_port}" if isinstance(relay_port, int) else None,
        root_path=root_path if isinstance(root_path, str) else None,
    )
