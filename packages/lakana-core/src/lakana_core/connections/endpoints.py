"""The exact host:port pairs a connection lets a task reach.

One place for the worker (the targets it grants a task), for what a binding shows and records
(`endpoint_hosts`) and for what a task's run-time fetch of a connection is told
(`lakana_core.connections.fetch`), so they cannot disagree.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from lakana_core.connections.kinds import ConnectionKind, _kind

PROXY_KINDS = frozenset(
    {
        ConnectionKind.HTTP,
        ConnectionKind.S3,
        ConnectionKind.GCS,
        ConnectionKind.AZURE,
    }
)
"""Kinds a task reaches through the egress proxy (HTTPS)."""
RELAY_KINDS = frozenset({ConnectionKind.POSTGRESQL, ConnectionKind.MARIADB, ConnectionKind.TCP})
"""Kinds a task reaches through its relay (a database or other non-HTTP protocol)."""
MOUNT_KINDS = frozenset({ConnectionKind.DIRECTORY, ConnectionKind.SQLITE_FILE})
"""Kinds a task reads from a read-only mount of an ingestion root, without a network."""


@dataclass(frozen=True)
class Endpoint:
    """One host and port a task may reach for a connection.

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
    role: Literal["service", "token"]


def _of_url(url: str, role: Literal["service", "token"]) -> Endpoint:
    """Read an https URL's host and port.

    Parameters
    ----------
    url
        A URL `kinds._https` accepted.
    role
        Its role.

    Returns
    -------
    Endpoint
        Host and port (443 when the URL names none).
    """
    host, _, port = urlsplit(url).netloc.partition(":")
    return Endpoint(host, int(port) if port else 443, role)


def endpoints(
    kind: ConnectionKind, address: Mapping[str, Any], settings: Mapping[str, Any]
) -> tuple[Endpoint, ...]:
    """List the endpoints of a validated connection, in order, without repeats.

    Parameters
    ----------
    kind
        The kind.
    address
        The validated address.
    settings
        The validated settings.

    Returns
    -------
    tuple of Endpoint
        Empty for the kinds read from a mount.

    Raises
    ------
    ValidationError
        For a kind Lakana does not know (through `kinds._kind`).
    """
    kind = _kind(kind)
    found: list[Endpoint] = []
    if kind in RELAY_KINDS:
        found.append(Endpoint(str(address["host"]), int(address["port"]), "service"))
    elif kind is ConnectionKind.HTTP:
        found.append(_of_url(str(address["base_url"]), "service"))
        if settings.get("token_url"):
            found.append(_of_url(str(settings["token_url"]), "token"))
    elif kind is ConnectionKind.S3:
        found.append(_of_url(str(address["endpoint"]), "service"))
    elif kind is ConnectionKind.GCS:
        found += [
            Endpoint("storage.googleapis.com", 443, "service"),
            Endpoint("oauth2.googleapis.com", 443, "token"),
        ]
    elif kind is ConnectionKind.AZURE:
        found.append(Endpoint(f"{address['account']}.blob.core.windows.net", 443, "service"))
    unique: list[Endpoint] = []
    for endpoint in found:
        if all((e.host, e.port) != (endpoint.host, endpoint.port) for e in unique):
            unique.append(endpoint)
    return tuple(unique)


def endpoint_hosts(
    kind: ConnectionKind, address: Mapping[str, Any], settings: Mapping[str, Any]
) -> str | None:
    """Write every ``host:port`` a connection sends its secret to, for a person to read.

    Shown when a secret is bound to the connection and recorded in the binding's audit event:
    an oauth2 client secret goes to the token URL's host as well as the API's.

    Parameters
    ----------
    kind
        The kind.
    address
        The validated address.
    settings
        The validated settings.

    Returns
    -------
    str or None
        The endpoints joined by ``, ``, in `endpoints` order; `None` for a kind read from a
        mount, which has none.
    """
    found = endpoints(kind, address, settings)
    return ", ".join(f"{e.host}:{e.port}" for e in found) or None
