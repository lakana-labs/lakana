"""The HTTPS client of an ``http`` connection, for ``files.read`` (and later ``rest.read``).

One client per connection: the task's proxy as httpx's own proxy (never ``HTTPS_PROXY``:
``trust_env=False`` also ignores ``.netrc`` and ``SSL_CERT_FILE``), the trust bundle plus the
connection's CA through `lakana.ingest._check.trust_context` (TLS 1.2 or newer), the
connection's authentication, and no redirects, so a credential never leaves the connection's
host.
"""

from __future__ import annotations

import base64
import ssl
from typing import TYPE_CHECKING

from lakana import context
from lakana.ingest._check import trust_context
from lakana.ingest._errors import UsageError, missing_secret, task_environment

if TYPE_CHECKING:
    import httpx

    from lakana.ingest.connections import Connection

TIMEOUT_SECONDS = 60.0
"""Seconds per connect, read and write of one request."""


def client_for(conn: Connection) -> httpx.Client:
    """Build the client of an ``http`` connection.

    Parameters
    ----------
    conn
        The connection, with its secret and the task's proxy.

    Returns
    -------
    httpx.Client
        A client whose ``base_url`` is the connection's; the caller closes it.

    Raises
    ------
    UsageError
        For auth ``oauth2`` (not supported by ``files.read``), a missing secret, a CA
        certificate that is no PEM certificate, or a trust bundle this task cannot read.
    """
    import httpx

    auth_mode = str(conn.settings.get("auth", "none"))
    if auth_mode == "oauth2":
        raise UsageError(
            f"`{conn.name}` uses auth oauth2, which files.read does not support yet; use auth "
            "bearer with a token, or fetch the file with connections.get and your own client"
        )
    try:
        verify = trust_context(_ca(conn))
    except ssl.SSLError:
        raise UsageError(
            f"the CA certificate of `{conn.name}` or the workers' trust bundle holds no PEM "
            f"certificate; give the connection your CA: `lakana connection update {conn.name} "
            f"--env {task_environment()} --ca-file ca.pem`, and check LAKANA_RUN__CA_BUNDLE"
        ) from None
    except OSError as exc:
        raise UsageError(
            f"the trust bundle (LAKANA_CA_BUNDLE) cannot be read ({exc.strerror}); check the "
            "worker's LAKANA_RUN__CA_BUNDLE"
        ) from None
    headers: dict[str, str] = {}
    params: dict[str, str] = {}
    auth: httpx.Auth | None = None
    if auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {_secret(conn, 'token')}"
    elif auth_mode == "basic":
        user, password = str(conn.settings["username"]), _secret(conn, "password")
        auth = httpx.BasicAuth(user, password)
        # The header carries a value derived from the secret, which the harness cannot know.
        context.register_secret(base64.b64encode(f"{user}:{password}".encode()).decode())
    elif auth_mode == "api_key":
        name, value = str(conn.settings["api_key_name"]), _secret(conn, "api_key")
        if conn.settings.get("api_key_in") == "query":
            params[name] = value
        else:
            headers[name] = value
    return httpx.Client(
        base_url=str(conn.address["base_url"]),
        proxy=conn.proxy_url,
        trust_env=False,
        verify=verify,
        auth=auth,
        headers=headers,
        params=params,
        follow_redirects=False,
        timeout=TIMEOUT_SECONDS,
    )


def _ca(conn: Connection) -> str | None:
    """Read the connection's own CA certificates.

    Parameters
    ----------
    conn
        The connection.

    Returns
    -------
    str or None
        PEM, or `None`.
    """
    value = conn.settings.get("ca_certificate")
    return value if isinstance(value, str) and value else None


def _secret(conn: Connection, key: str) -> str:
    """Read one value of the connection's secret.

    Parameters
    ----------
    conn
        The connection.
    key
        The key its auth needs.

    Returns
    -------
    str
        The value.

    Raises
    ------
    UsageError
        When the secret is missing, naming the command that binds one.
    """
    if conn.secret is None or key not in conn.secret:
        error = missing_secret(conn.name, key, "--password-stdin")
        raise error
    return conn.secret[key]
