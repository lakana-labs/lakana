"""The fetch of a declared connection, once per attempt, kept in this process's memory only.

`fetch` asks the server (`client.task_connections.get`) the first time an attempt needs a
connection and keeps the answer in `_CACHE` until the harness ends the attempt (`clear`). Every
value of the secret is registered with the harness (`lakana.context.register_secret`), which
masks it in everything it writes. Nothing is written to a file or an environment variable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from lakana import context
from lakana.errors import PermissionDenied
from lakana.ingest._errors import (
    ConnectionChanged,
    ConnectionNotDeclared,
    ConnectionRefused,
    IngestError,
)


@dataclass(frozen=True)
class Endpoint:
    """One host and port the connection reaches.

    Parameters
    ----------
    host
        The host, lower case.
    port
        The port.
    role
        ``service`` (the data) or ``token`` (where an OAuth2 token is fetched).
    """

    host: str
    port: int
    role: str


@dataclass(frozen=True)
class FetchedConnection:
    """A connection as the server answered it, with this task's routing for it.

    Parameters
    ----------
    name
        Its name.
    kind
        Its kind.
    address
        Its address.
    settings
        Its settings (a ``ca_certificate`` in full).
    endpoints
        Every host and port it reaches.
    secret
        Its secret's values, or `None`. Left out of `repr`.
    routing
        What the task description says about it: its ``kind``, the ``grant`` of host and port
        pairs and, under the Docker runner, its ``relay_port``; for a mounted kind
        ``root``, ``root_dir``, ``path`` and ``root_path``.
    """

    name: str
    kind: str
    address: Mapping[str, object]
    settings: Mapping[str, object]
    endpoints: tuple[Endpoint, ...]
    secret: Mapping[str, str] | None = field(repr=False)
    routing: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_body(
        cls, name: str, body: Mapping[str, object], routing: Mapping[str, object]
    ) -> FetchedConnection:
        """Read a `TaskConnectionOut` body.

        Parameters
        ----------
        name
            The name the asset asked for.
        body
            The answer.
        routing
            The task description's entry for it.

        Returns
        -------
        FetchedConnection
            The connection.

        Raises
        ------
        IngestError
            If the answer is not a connection; the message names the connection only.
        """
        address, settings = body.get("address"), body.get("settings")
        endpoints, secret, kind = body.get("endpoints"), body.get("secret"), body.get("kind")
        well_formed = (
            isinstance(kind, str)
            and isinstance(address, dict)
            and isinstance(settings, dict)
            and isinstance(endpoints, list)
            and all(
                isinstance(e, dict)
                and isinstance(e.get("host"), str)
                and isinstance(e.get("port"), int)
                and isinstance(e.get("role"), str)
                for e in endpoints
            )
            and (
                secret is None
                or (
                    isinstance(secret, dict)
                    and all(isinstance(k, str) and isinstance(v, str) for k, v in secret.items())
                )
            )
        )
        if not well_formed:
            raise IngestError(
                f"the server's answer for connection `{name}` is not a connection; check that "
                "the task's lakana is the server's version"
            )
        assert isinstance(kind, str) and isinstance(endpoints, list)  # nosec B101 -- checked
        assert isinstance(address, dict) and isinstance(settings, dict)  # nosec B101 -- checked
        return cls(
            name=name,
            kind=kind,
            address=dict(address),
            settings=dict(settings),
            endpoints=tuple(Endpoint(e["host"], e["port"], e["role"]) for e in endpoints),
            secret=None if secret is None else dict(secret),
            routing=dict(routing),
        )


_CACHE: dict[str, FetchedConnection] = {}


def fetch(name: str) -> FetchedConnection:
    """Fetch a declared connection once per attempt, keeping it in memory only.

    Parameters
    ----------
    name
        The connection's name.

    Returns
    -------
    FetchedConnection
        Kind, address, settings, endpoints, secret, and this task's routing for it.

    Raises
    ------
    ConnectionNotDeclared
        Before any request, for a name the asset does not declare.
    ConnectionRefused
        When the server refuses (its text, unchanged).
    ConnectionChanged
        When its endpoints, or a mounted connection's root and path, are no longer those the
        worker gave this attempt (retryable; nothing is kept, so the next attempt fetches it
        again).
    """
    state = context.current()
    if name not in state.declared:
        raise ConnectionNotDeclared(
            f"`{name}` is not declared by this asset: add it to @asset(connections=[...]) and "
            "deploy."
        )
    if name not in _CACHE:
        try:
            body = state.client.task_connections.get(name)
        except PermissionDenied as exc:
            raise ConnectionRefused(str(exc)) from None
        secret = body.get("secret")
        if isinstance(secret, dict):
            for value in secret.values():
                if isinstance(value, str):
                    context.register_secret(value)
        fetched = FetchedConnection.from_body(name, body, routing=state.connections.get(name, {}))
        if not _matches_grant(fetched) or not _matches_mount(fetched):
            raise ConnectionChanged(
                f"connection `{name}` changed after this attempt started; the next attempt uses "
                "the new address"
            )
        _CACHE[name] = fetched
    return _CACHE[name]


def _matches_grant(fetched: FetchedConnection) -> bool:
    """Say whether a fetched connection reaches exactly the endpoints the worker granted.

    The worker read the connection before the task started and let it reach those endpoints
    only; a change since (``lakana connection update``) would point the task at a host its
    proxy, relay or token refuses.

    Parameters
    ----------
    fetched
        The connection as fetched, with its routing.

    Returns
    -------
    bool
        Whether the endpoints and the ``grant`` are the same set of host and port pairs (true
        without a ``grant``); a malformed ``grant`` never matches.
    """
    if "grant" not in fetched.routing:
        return True
    grant = fetched.routing.get("grant")
    if not isinstance(grant, list) or not all(
        isinstance(pair, list) and len(pair) == 2 for pair in grant
    ):
        return False
    return {(e.host, e.port) for e in fetched.endpoints} == {tuple(pair) for pair in grant}


def _matches_mount(fetched: FetchedConnection) -> bool:
    """Say whether a mounted connection still names the root and path the worker mounted.

    Parameters
    ----------
    fetched
        The connection as fetched, with its routing.

    Returns
    -------
    bool
        True for a connection without a mount (no ``root_path`` in its routing); otherwise
        whether its address's ``root`` and ``path`` are the routing's.
    """
    if "root_path" not in fetched.routing:
        return True
    return all(fetched.address.get(key) == fetched.routing.get(key) for key in ("root", "path"))


def clear() -> None:
    """Forget every fetched connection (the harness, when the attempt ends)."""
    _CACHE.clear()
