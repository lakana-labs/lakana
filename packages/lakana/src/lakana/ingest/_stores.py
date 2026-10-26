"""The object stores of `lakana.ingest.files`: S3, GCS and Azure Blob Storage, through obstore.

Each store gets the connection's address and credential explicitly and nothing from the task's
environment: obstore's constructors read every ``AWS_*``, ``GOOGLE_*`` and ``AZURE_*`` variable
(a token there would replace or join the connection's credential), so a store is built while
those variables are out of the environment, and put back after. The proxy goes into the store's
own client options (`Connection.proxy_url`, which holds the task's egress token), never into
``HTTPS_PROXY``; the trust bundle (`LAKANA_CA_BUNDLE`) is added to the store's roots. A GCS
service-account key is passed on with the three fields that sign its requests only, so a key
cannot point the store at another host.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Callable, Generator
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlsplit

from lakana import context
from lakana.ingest._errors import UsageError, missing_secret, task_environment

if TYPE_CHECKING:
    from obstore.store import AzureStore, ClientConfig, GCSStore, S3Store

    from lakana.ingest.connections import Connection

GCS_BASE_URL = "https://storage.googleapis.com"
"""Where a GCS store sends its requests; only tests point it at an emulator."""
AZURE_ENDPOINT = "https://{account}.blob.core.windows.net"
"""An Azure store's account URL; only tests point it at an emulator."""
_NO_PROXY = "http://127.0.0.1:9"
"""The proxy of a task that has none: no request uses it (see `client_options`)."""
_GCS_KEY_FIELDS = ("private_key", "private_key_id", "client_email")
_CLOUD_PREFIXES = ("AWS_", "GOOGLE_", "AZURE_")
"""The variables obstore's S3, GCS and Azure constructors read, by prefix."""
_CLOUD_NAMES = frozenset(
    {"IDENTITY_ENDPOINT", "IDENTITY_HEADER", "MSI_ENDPOINT", "IMDS_ENDPOINT", "SERVICE_ACCOUNT"}
)
"""The ones without a prefix: a managed-identity endpoint and a key file's path."""
_ENVIRONMENT = threading.Lock()
_Store = TypeVar("_Store")
_NEEDS_ASSETS = (
    "files.read needs the assets extra on the worker: add lakana[assets] to the project's "
    "dependencies"
)


def client_options(conn: Connection, url: str) -> ClientConfig:
    """Build a store's client options: the task's proxy and the trust bundle, nothing else.

    obstore's client falls back to ``HTTPS_PROXY`` when it is given no proxy. A task that runs
    without the egress proxy therefore gets a proxy that no request uses: the store's own host
    is excluded from it, so the store connects directly, and a request to any other host fails
    instead of leaving through a proxy from the environment.

    Parameters
    ----------
    conn
        The connection.
    url
        The URL the store sends its requests to, whose host is excluded from the unused proxy.

    Returns
    -------
    ClientConfig
        ``proxy_url`` (the task's, or the unused one with ``proxy_excludes``),
        ``root_certificate`` when a trust bundle is set, and timeouts.

    Raises
    ------
    UsageError
        When `LAKANA_CA_BUNDLE` names a file this task cannot read.
    """
    options: ClientConfig = {"connect_timeout": "10s", "timeout": "300s"}
    if conn.proxy_url:
        options["proxy_url"] = conn.proxy_url
    else:
        options["proxy_url"] = _NO_PROXY
        options["proxy_excludes"] = urlsplit(url).hostname or ""
    bundle = os.environ.get("LAKANA_CA_BUNDLE")
    if bundle:
        try:
            with open(bundle, "rb") as handle:
                # Bytes: the bundle's comments may hold names that are not ASCII.
                options["root_certificate"] = handle.read()
        except OSError as exc:
            raise UsageError(
                f"the trust bundle `{bundle}` (LAKANA_CA_BUNDLE) cannot be read "
                f"({exc.strerror}); check the worker's LAKANA_RUN__CA_BUNDLE"
            ) from None
        if b"-----BEGIN CERTIFICATE-----" not in options["root_certificate"]:
            raise UsageError(
                f"the trust bundle `{bundle}` (LAKANA_CA_BUNDLE) holds no certificate; check the "
                "worker's LAKANA_RUN__CA_BUNDLE"
            )
    return options


@contextlib.contextmanager
def _without_cloud_environment() -> Generator[None]:
    """Take obstore's variables out of the environment while a store is built.

    One store is built at a time (a lock), and every variable is put back afterwards, so the
    task's own code sees its environment unchanged.

    Yields
    ------
    None
        While the variables are out.
    """
    with _ENVIRONMENT:
        taken = {
            name: os.environ.pop(name)
            for name in list(os.environ)
            if name.startswith(_CLOUD_PREFIXES) or name in _CLOUD_NAMES
        }
        try:
            yield
        finally:
            os.environ.update(taken)


def _build(conn: Connection, make: Callable[[], _Store], fix: str) -> _Store:
    """Build a store without the environment, turning obstore's refusal into a next step.

    Parameters
    ----------
    conn
        The connection, for the message.
    make
        Calls the store's constructor.
    fix
        What to set right, such as ``its secret``.

    Returns
    -------
    _Store
        The store.

    Raises
    ------
    UsageError
        When obstore refuses the values (a key that is no RSA key, an account key that is not
        base64); the message never shows them.
    """
    import obstore

    with _without_cloud_environment():
        try:
            return make()
        except (obstore.exceptions.BaseError, ValueError, TypeError):
            raise UsageError(
                f"obstore cannot use the secret of `{conn.name}` ({fix}); store the right one "
                f"with `lakana connection update {conn.name} --env {task_environment()} "
                "--values-stdin`"
            ) from None


def _secret(conn: Connection, key: str) -> str:
    """Read one value of the connection's secret.

    Parameters
    ----------
    conn
        The connection.
    key
        The key, such as ``secret_access_key``.

    Returns
    -------
    str
        The value.

    Raises
    ------
    UsageError
        When the connection has no secret or the secret lacks the key, naming the command that
        binds one.
    """
    if conn.secret is None or key not in conn.secret:
        error = missing_secret(conn.name, key, "--values-stdin")
        raise error
    return conn.secret[key]


def _gcs_key(conn: Connection) -> str:
    """Rewrite a GCS service-account key with only the fields that sign requests.

    Parameters
    ----------
    conn
        A ``gcs`` connection.

    Returns
    -------
    str
        The key as JSON: ``private_key``, ``private_key_id`` and ``client_email``.

    Raises
    ------
    UsageError
        When the secret is not a service-account key; the message never shows the key.
    """
    try:
        key = json.loads(_secret(conn, "service_account_json"))
    except json.JSONDecodeError:
        key = None
    if (
        not isinstance(key, dict)
        or key.get("type") != "service_account"
        or not all(isinstance(key.get(f), str) and key.get(f) for f in _GCS_KEY_FIELDS)
    ):
        raise UsageError(
            f"the secret of `{conn.name}` is not a service-account key: it must be the JSON "
            "Google gives you, with type service_account, private_key, private_key_id and "
            "client_email; store it with `lakana connection update "
            f"{conn.name} --env {task_environment()} --values-stdin`"
        )
    return json.dumps({field: key[field] for field in _GCS_KEY_FIELDS})


def _azure_credential(conn: Connection) -> dict[str, Any]:
    """Choose an Azure store's credential: the SAS token when there is one, else the account key.

    Parameters
    ----------
    conn
        An ``azure`` connection.

    Returns
    -------
    dict
        ``sas_key`` (without its leading ``?``) or ``account_key``, as obstore takes them.

    Raises
    ------
    UsageError
        When the secret has neither.
    """
    if conn.secret is not None and "sas_token" in conn.secret:
        sas = conn.secret["sas_token"].lstrip("?")
        # The store's requests and errors carry the token without its `?`: mask that form too.
        context.register_secret(sas)
        return {"sas_key": sas}
    return {"account_key": _secret(conn, "account_key")}


def store_for(conn: Connection) -> S3Store | GCSStore | AzureStore:
    """Build the obstore store of an ``s3``, ``gcs`` or ``azure`` connection.

    Parameters
    ----------
    conn
        The connection, with its secret and the task's proxy.

    Returns
    -------
    S3Store, GCSStore or AzureStore
        A store whose paths are relative to the connection's ``prefix``.

    Raises
    ------
    UsageError
        Without the assets extra, for a missing secret, or for a kind without a store.
    """
    try:
        from obstore.store import AzureStore, GCSStore, S3Store
    except ImportError:
        raise UsageError(_NEEDS_ASSETS) from None
    prefix = conn.settings.get("prefix")
    prefix = prefix if isinstance(prefix, str) and prefix else None
    if conn.kind == "s3":
        session = conn.secret.get("session_token") if conn.secret else None
        endpoint = str(conn.address["endpoint"])
        s3: dict[str, Any] = {
            "prefix": prefix,
            "region": str(conn.address["region"]),
            "endpoint": endpoint,
            "access_key_id": str(conn.settings["access_key_id"]),
            "secret_access_key": _secret(conn, "secret_access_key"),
            **({"session_token": session} if session else {}),
            "virtual_hosted_style_request": False,
            "skip_signature": False,
            "client_options": client_options(conn, endpoint),
        }
        return _build(conn, lambda: S3Store(str(conn.address["bucket"]), **s3), "its secret key")
    if conn.kind == "gcs":
        gcs: dict[str, Any] = {
            "prefix": prefix,
            "service_account_key": _gcs_key(conn),
            "base_url": GCS_BASE_URL,
            "client_options": client_options(conn, GCS_BASE_URL),
        }
        return _build(
            conn,
            lambda: GCSStore(str(conn.address["bucket"]), **gcs),
            "the private key of its service-account key",
        )
    if conn.kind == "azure":
        account = str(conn.address["account"])
        endpoint = AZURE_ENDPOINT.format(account=account)
        azure: dict[str, Any] = {
            "container_name": str(conn.address["container"]),
            "prefix": prefix,
            "account_name": account,
            "endpoint": endpoint,
            "client_options": client_options(conn, endpoint),
            **_azure_credential(conn),
        }
        return _build(conn, lambda: AzureStore(**azure), "its account key or SAS token")
    raise UsageError(f"`{conn.name}` is {conn.kind}, which has no object store")
