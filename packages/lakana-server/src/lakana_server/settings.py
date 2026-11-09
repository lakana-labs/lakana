"""Runtime settings for lakana-server."""

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_core.config import (
    ArtifactsConfig,
    AuditConfig,
    DatabaseConfig,
    DenialAuditConfig,
    HealthConfig,
    LogConfig,
    QueryLimitsConfig,
    RunConfig,
    SecretsConfig,
    TableReadsConfig,
    TableUndoConfig,
    TaskTokensConfig,
    ViewAuditConfig,
    validate_repo_roots,
)
from lakana_core.languages import DEFAULT_MAIN_LANGUAGE, language_problem


class AppendConfig(BaseModel):
    """Operator-level bounds on a governed Arrow append.

    Parameters
    ----------
    max_bytes
        Largest body, in bytes, of one table append (default 256 MiB). A larger request is refused
        with 413 before the rest of the body is read, and nothing is written.
    max_concurrent
        Most appends and artifact uploads one server process receives at once (default 4). A
        request past the limit is refused at once with 503 and `Retry-After: 1`. The SDK asks again
        for an artifact upload (a few tries). A table write (the SDK's `append`, `merge`
        or `history`) is not retried: it raises `ServerError`, and a run of a Python asset ends as
        retryable and is run again.
    max_concurrent_history
        Most history writes (`write="history"` of a Python asset, over REST) one server process runs
        at once (default 1), in addition to the `max_concurrent` slot each takes; a request takes it
        once its authorization is checked, before its body is read. Each holds the table's open
        versions in server memory (measured at about 2 GiB per million narrow rows), bounded by
        `LAKANA_RUN__MAX_HISTORY_OPEN_BYTES` of Arrow data plus one data file. A request past the
        limit is refused at once with 503 and `Retry-After: 1`. The SDK's history write does not
        retry it and raises `ServerError`; a run of a Python asset ends as retryable and runs again
        up to its `retries:` (2 by default, 30 s and then 60 s apart; at most
        `LAKANA_RUN__MAX_RETRIES`), and fails once they are used up. Raise this (with the server's
        memory) or the asset's `retries:` when several Python history assets run at once.
    read_timeout_seconds
        How long the server waits for the body of a request, in seconds (default 300). For a table
        append, the whole body; for an artifact upload, the next piece of a file and the whole
        manifest request. A request past the limit is refused with 408 and nothing is written. It
        stops a slow or stalled client from holding a slot.
    max_arrays
        Most columns, summed over all batches, that the data of one append may hold (default
        100,000). Many tiny batches use much memory and processor time even in a small body. A
        request over the limit is refused with 413; send fewer, larger batches.
    """

    max_bytes: int = Field(default=268_435_456, gt=0)
    max_concurrent: int = Field(default=4, gt=0)
    max_concurrent_history: int = Field(default=1, gt=0)
    read_timeout_seconds: float = Field(default=300, gt=0)
    max_arrays: int = Field(default=100_000, gt=0)


class AuthAuditConfig(BaseModel):
    """Operator-level bound on failed-authentication audit writes.

    Parameters
    ----------
    per_ip_per_minute
        Failed sign-ins that the audit log records per client per minute (default 10). Requests
        past the rate are still refused with 401, but not each recorded, so repeated attempts
        cannot flood the log.
    """

    per_ip_per_minute: int = Field(default=10, gt=0)


class ServerAuditConfig(AuditConfig):
    """The audit settings of the server: those it shares with the workers, and its own.

    Read from ``LAKANA_AUDIT__*``. The shared ones are documented on `AuditConfig`.

    Parameters
    ----------
    sign_in_window_minutes
        Minutes during which a token that signed in successfully writes no second sign-in event
        (default 15; 0 to 1,440). The next event of that token says how many sign-ins were left
        out. Each token counts on its own, and its first use always writes an event, as does
        its first use after it was refused. 0 writes an event for every request. Each server
        process counts on its own. Failed sign-ins are not affected
        (`LAKANA_AUTH_AUDIT__PER_IP_PER_MINUTE`).
    """

    sign_in_window_minutes: int = Field(default=15, ge=0, le=1440)


class RequestBodyConfig(BaseModel):
    """Bound on the body of a JSON request (`LAKANA_REQUEST_BODY__*`).

    Parameters
    ----------
    max_bytes
        Largest body, in bytes, the server accepts on a route whose body is JSON; a larger one
        is answered 413 before anything is parsed, and before the caller is authenticated.
        The default (1 MiB) is far above any ordinary request: a 100,000-character SQL statement
        of ASCII characters is under 0.6 MiB as JSON even with every character escaped. A character
        outside the Basic Multilingual Plane takes up to 12 bytes escaped, so a statement made of
        those can pass 1 MiB (raise this setting if you send such SQL). Routes that stream a body on
        purpose (table data, artifact files and manifests) are not counted: they have their own
        limits.
    """

    max_bytes: int = Field(default=1_048_576, gt=0)


class ReadinessConfig(BaseModel):
    """How the readiness route `/readyz` checks the instance (`LAKANA_READINESS__*`).

    Parameters
    ----------
    timeout_seconds
        How long the database check may take, and then the storage checks together, in seconds
        (default 2; more than 0, at most 30). A check that takes longer fails. A check that has
        not ended is not started again by the next request, so a database or storage that hangs
        holds at most one connection of the server each.
    cache_seconds
        How long one answer of `/readyz` is reused, in seconds (default 5; 1 to 300). However
        often the route is asked, the server checks at most once in this time, so the route,
        which needs no sign-in, cannot be used to load the database or the storage.
    environments
        The environments whose storage `/readyz` checks, as a JSON list, for example
        `["dev", "prod"]`. Empty (the default): every environment, at most the first 5 by name.
        Name the ones that must work when another environment's storage is out of reach on
        purpose; a name no environment has fails the check.
    """

    timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    cache_seconds: float = Field(default=5.0, ge=1, le=300)
    environments: list[str] = []


LOCALHOST = "localhost"
"""The only loopback name the interface's public address may use: passkeys need a name."""

_DEFAULT_PORTS = {"http": 80, "https": 443}

_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
"""One DNS label, lower case, as a browser sends it in `Host` (an IDN in its `xn--` form)."""

_HOST_ENTRY = re.compile(
    r"(?P<host>\[[0-9a-f:.]+\]|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::(?P<port>[0-9]{1,5}))?"
)
"""The shape of a `host[:port]` entry of `LAKANA_UI__ALLOWED_HOSTS` (IP addresses refused after)."""


@dataclass(frozen=True)
class PublicOrigin:
    """The address browsers reach the interface at (`LAKANA_UI__PUBLIC_URL`).

    Parameters
    ----------
    scheme
        `http` or `https`.
    host
        The host name, lower case; never an IP address.
    port
        The port, also when it is the scheme's default.
    origin
        `scheme://host[:port]` exactly as a browser sends it in `Origin`: the default port
        left out.
    loopback
        Whether the host is `localhost`.
    """

    scheme: str
    host: str
    port: int
    origin: str
    loopback: bool

    @property
    def host_header(self) -> str:
        """The `Host` header a browser sends to this address.

        Returns
        -------
        str
            `host`, with `:port` unless the port is the scheme's default.
        """
        return self.origin.split("://", 1)[1]


def _public_url_problem(url: str) -> str | None:
    """Explain why a public address cannot be used, if it cannot.

    Parameters
    ----------
    url
        `LAKANA_UI__PUBLIC_URL` as given.

    Returns
    -------
    str or None
        The message naming the setting and the fix, or None when `url` is a bare origin with a
        host name.
    """
    shape = (
        "LAKANA_UI__PUBLIC_URL must be the bare address browsers open, such as "
        "https://lakana.example.com or http://localhost:8000: a scheme, a host name and an "
        "optional port, with no path, query, fragment or user name."
    )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return shape
    if parts.scheme not in _DEFAULT_PORTS or parts.path not in {"", "/"}:
        return shape
    if parts.query or parts.fragment or "?" in url or "#" in url or parts.username is not None:
        return shape
    host = parts.hostname or ""
    if port == 0 or not host:
        return shape
    if _is_ip_address(host):
        return (
            f"LAKANA_UI__PUBLIC_URL must use a host name, not an IP address: browsers tie "
            f"passkeys to a name. Locally use http://localhost:{port or '<port>'}; on a team "
            "server use the DNS name people open."
        )
    if not host.isascii():
        return (
            "LAKANA_UI__PUBLIC_URL must spell an international host name in its ASCII form "
            "(xn--...), as browsers send it."
        )
    labels = host.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        return (
            shape + " The host name has an empty label (a trailing dot) or a character a host"
            " name cannot hold."
        )
    return None


def _is_ip_address(host: str) -> bool:
    """Tell whether a browser would read a host as an IP address.

    Parameters
    ----------
    host
        The host name of a URL, lower case, without brackets.

    Returns
    -------
    bool
        True for an IPv6 or IPv4 address, and for any name whose last label is a number
        (`127.1`, `0x7f.1`), which browsers read as IPv4 too.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        last = host.rstrip(".").rsplit(".", 1)[-1]
        return last.isdigit() or bool(re.fullmatch(r"0x[0-9a-f]*", last))
    return True


class UiConfig(BaseModel):
    """The web interface (`LAKANA_UI__*`).

    Parameters
    ----------
    enabled
        Whether the server serves the web interface at `/` (default false while the interface
        is being built; turn it on to try it). The interface's files come with a released
        server; in a source checkout, build them first:
        `npm --prefix ui ci && npm --prefix ui run build`. The interface calls only the REST API,
        with the user's own credential. Until sign-in exists it reads no data. Session lengths
        are not environment settings: Settings → Sign-in in the interface, or
        `lakana sign-in settings`.
    public_url
        The address people open the interface at, such as `https://lakana.example`: a
        scheme, a host name and an optional port, nothing more. Unset (the default), it is
        `http://localhost:<LAKANA_PORT>`, for one person on their own machine; there the server
        also answers at `127.0.0.1` and `[::1]` and sends a browser that opens a page there on to
        `localhost`. Team use needs a DNS name and HTTPS: with the interface on, plain `http://` is
        refused except on `localhost`, and an IP address is always refused, because browsers tie
        passkeys to a name. Every link the server makes (sign-in, invite, recovery) starts with
        this address. Browsers that reach the interface under another name are refused (421)
        unless it is in `LAKANA_UI__ALLOWED_HOSTS`. Behind a reverse proxy, the proxy must pass the
        browser's `Host` header through (with nginx, `proxy_set_header Host $http_host;`, which
        keeps a port). Changing the public address invalidates every passkey: issue recovery
        links afterwards.
    allowed_hosts
        Further `host:port` names (`host` alone for port 80 or 443) the interface answers
        browsers at, as a JSON list, for example `["lakana.internal:8443"]`. Empty (the default):
        only the public address. Each entry is matched exactly, without letter case; there is no
        wildcard and no IP address. Signing in and every change still need the public address:
        a browser at an extra name can open pages only.
    local_sign_in
        Whether a person may sign in to the interface with a link they ask for with their own
        token, without a passkey touch (default false). `lakana server start --local` sets it for
        one person's own machine; it needs `LAKANA_HOST` on a loopback address and the public
        address on `localhost`, or the server does not start. On a team server leave it unset.
    passkey_options_per_ip_per_minute
        How often one client address may start a passkey sign-in per minute (default 60; past
        it the server answers 429 "wait a minute"). IPv6 addresses count per /64 network.
        Everyone behind one NAT shares an address, so a large office may need more; behind a
        reverse proxy set `LAKANA_TRUSTED_PROXIES`, or every browser counts as the proxy and one
        client can use up the limit for everyone. A sign-in challenge is signed, not stored, so
        no number of requests can expire a challenge a person is answering.
    passkey_options_per_network_per_minute
        How often one network (IPv4 /24, IPv6 /48) may start a passkey sign-in per minute
        (default 240; past it 429), so a client cannot get around the per-address limit by
        rotating through the addresses of a rented block. An office whose people sign in from
        many addresses of one /24 at once may need more.
    """

    enabled: bool = False
    public_url: str | None = None
    allowed_hosts: list[str] = []
    local_sign_in: bool = False
    passkey_options_per_ip_per_minute: int = Field(default=60, gt=0)
    passkey_options_per_network_per_minute: int = Field(default=240, gt=0)

    @field_validator("public_url")
    @classmethod
    def _public_url_is_an_origin(cls, value: str | None) -> str | None:
        """Refuse a public address browsers could not sign in at.

        Parameters
        ----------
        value
            `LAKANA_UI__PUBLIC_URL`, or None.

        Returns
        -------
        str or None
            `value`, unchanged.

        Raises
        ------
        ValueError
            Naming the setting and what to write instead.
        """
        if value is None:
            return None
        problem = _public_url_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _allowed_hosts_are_host_and_port(cls, value: list[str]) -> list[str]:
        """Refuse an entry that is not an exact `host[:port]`; lower its letters.

        Parameters
        ----------
        value
            `LAKANA_UI__ALLOWED_HOSTS`.

        Returns
        -------
        list of str
            The entries in lower case.

        Raises
        ------
        ValueError
            Naming the setting and the entry's expected shape.
        """
        lowered = [entry.lower() for entry in value]
        for entry in lowered:
            match = _HOST_ENTRY.fullmatch(entry)
            if (
                match is None
                or int(match["port"] or 1) not in range(1, 65536)
                or _is_ip_address(match["host"].strip("[]"))
            ):
                raise ValueError(
                    f"LAKANA_UI__ALLOWED_HOSTS entries are host names with an optional port, "
                    f"such as lakana.internal:8443, matched exactly; {entry!r} is not one "
                    "(no IP address, scheme, path or wildcard: passkeys need a name)."
                )
        return lowered

    @model_validator(mode="after")
    def _team_address_uses_https(self) -> Self:
        """Refuse plain HTTP off `localhost` while the interface is on.

        Returns
        -------
        UiConfig
            This configuration, unchanged.

        Raises
        ------
        ValueError
            Naming `LAKANA_UI__PUBLIC_URL` and the `https://` form.
        """
        if self.enabled and self.public_url is not None:
            parts = urlsplit(self.public_url)
            if parts.scheme == "http" and parts.hostname != LOCALHOST:
                raise ValueError(
                    "LAKANA_UI__PUBLIC_URL must start with https:// on a team server: browsers "
                    "keep passkeys and the sign-in cookie safe only on a secure address. Plain "
                    "http:// works only at http://localhost:<port>."
                )
        return self


class Settings(BaseSettings):
    """Configuration for the control plane.

    Parameters
    ----------
    database
        Metadata database configuration, in the same shape `lakana-core` uses.
    bootstrap_admin
        Name of the administrator created the first time the server starts, if no principal of
        that name exists. Its credential is supplied (`LAKANA_BOOTSTRAP_PAT_KEY_ID`) or written to
        `LAKANA_BOOTSTRAP_TOKEN_FILE`, never logged. Change it only before the first start.
    bootstrap_pat_key_id
        The key id half of a fixed credential for the bootstrap administrator, for tests and
        local development; set it together with `LAKANA_BOOTSTRAP_PAT_SECRET`. Unset (the
        default), a random credential is created at the first start and written to
        `LAKANA_BOOTSTRAP_TOKEN_FILE`. A key id an earlier token already used stops the start-up.
        The Compose bundle sets both in its `.env` file.
    bootstrap_pat_secret
        The secret half of a fixed credential for the bootstrap administrator; set it together
        with `LAKANA_BOOTSTRAP_PAT_KEY_ID`. Unset (the default), a random credential is created
        at the first start and written to `LAKANA_BOOTSTRAP_TOKEN_FILE`. Treat it like a
        password.
    bootstrap_token_file
        Where the server writes the token of a first administrator it had to create without a
        supplied credential: a new file, mode 0600, never overwritten (an existing file stops the
        start-up; remove it after use). The server logs this path, never the token. No default,
        because the server has no data directory of its own; required only in that case, where
        start-up fails naming this setting if it is unset. Not used when
        `LAKANA_BOOTSTRAP_PAT_KEY_ID` is set.
    secrets
        Secrets subsystem configuration, in the same shape `lakana-core` uses.
    task_tokens
        Task-token minting bounds (`LAKANA_TASK_TOKENS__MAX_TTL_SECONDS`).
    artifacts
        Artifact-store bounds (`LAKANA_ARTIFACTS__*`): file size, total size, file
        count and upload lifetime.
    tables
        How many days a dropped table can be restored (`LAKANA_TABLES__UNDO_DAYS`), and how
        many days ahead an expiry is shown (`LAKANA_TABLES__EXPIRY_WARNING_DAYS`).
    table_reads
        How long the reads of a table are kept and how far back its health shows them
        (`LAKANA_TABLE_READS__KEEP_DAYS`, `LAKANA_TABLE_READS__DEFAULT_DAYS`); the worker reads
        the kept period too.
    health
        How long table health signals are kept (`LAKANA_HEALTH__RETENTION_DAYS`).
    append
        Governed Arrow-append bounds: body size (`LAKANA_APPEND__MAX_BYTES`),
        concurrency (`LAKANA_APPEND__MAX_CONCURRENT`), body-read deadline
        (`LAKANA_APPEND__READ_TIMEOUT_SECONDS`), and decoded array count
        (`LAKANA_APPEND__MAX_ARRAYS`).
    request_body
        JSON request body bound (`LAKANA_REQUEST_BODY__MAX_BYTES`).
    auth_audit
        Failed-authentication audit rate limiting
        (`LAKANA_AUTH_AUDIT__PER_IP_PER_MINUTE`).
    denial_audit
        `AUTHORIZATION_DENIED` audit rate limiting per principal
        (`LAKANA_DENIAL_AUDIT__PER_PRINCIPAL_PER_MINUTE`).
    view_audit
        Whether a view of other people's activity is recorded in the audit log, and how often
        (`LAKANA_VIEW_AUDIT__ENABLED`, `LAKANA_VIEW_AUDIT__WINDOW_MINUTES`).
    audit
        How much of a query's SQL text the audit log stores (`LAKANA_AUDIT__QUERY_TEXT`), shared
        with the worker: the server records the queries it cancels.
    query_limits
        Only `timeout_seconds` is used here: the default and cap of a task timeout
        . Set it in the environment shared with the worker.
    run
        ``LAKANA_RUN__*``, shared with the worker.
    log
        ``LAKANA_LOG__LEVEL``, shared with the worker: how much the server logs.
    ci_oidc
        Which CI providers' OIDC ID tokens the server verifies, for which audience, and how it
        reaches their keys (`LAKANA_CI_OIDC__*`). Off by default.
    readiness
        How `/readyz` checks the database, the storages and the workers
        (`LAKANA_READINESS__TIMEOUT_SECONDS`, `LAKANA_READINESS__CACHE_SECONDS`,
        `LAKANA_READINESS__ENVIRONMENTS`).
    ui
        The web interface, `UiConfig`.
    host
        Address the server listens on. `127.0.0.1` (the default) accepts connections from this
        machine only; the Compose bundle publishes the port through `LAKANA_BIND`.
    port
        Port the server listens on (default 8000).
    trusted_proxies
        IP addresses or networks of reverse proxies whose `X-Forwarded-For` and
        `X-Forwarded-Proto` headers the server believes, as a JSON list, for example
        `["10.0.0.2"]` or `["10.1.0.0/16"]`. Empty (the default): no forwarded header is
        believed, also not from `127.0.0.1`, and the client address in audit events and rate
        limits is the TCP peer's. Behind a reverse proxy, list it here, or every request looks
        as if it came from the proxy, and one client can use up a per-address limit, such as
        passkey sign-in's (`LAKANA_UI__PASSKEY_OPTIONS_PER_IP_PER_MINUTE`), for everyone. No
        wildcard and no network of every address (`0.0.0.0/0`,
        `::/0`): a client could then claim any address.
    repo_roots
        Absolute directories that local repositories must lie under, as a JSON list, for example
        `["/repos"]`. Empty (the default) refuses every local repository.
    main_language
        The language catalog texts are in when a project does not declare its own
        (`main_language:` in lakana.yaml), and the language a reader without a choice gets
        (default `en`). A BCP 47 tag such as `de` or `fr-CH`. Nothing stores it: each answer
        reads it. Set it before the first deploy. Changing it later relabels at once the texts
        of every project that declares no `main_language:`, and edits made with `--lang` in the
        new main language stop showing.
    """

    model_config = SettingsConfigDict(
        env_prefix="LAKANA_", env_nested_delimiter="__", hide_input_in_errors=True
    )

    database: DatabaseConfig
    bootstrap_admin: str = "admin"
    bootstrap_pat_key_id: str | None = None
    bootstrap_pat_secret: str | None = None
    bootstrap_token_file: Path | None = None
    secrets: SecretsConfig = SecretsConfig()
    task_tokens: TaskTokensConfig = TaskTokensConfig()
    artifacts: ArtifactsConfig = ArtifactsConfig()
    tables: TableUndoConfig = TableUndoConfig()
    table_reads: TableReadsConfig = TableReadsConfig()
    health: HealthConfig = HealthConfig()
    append: AppendConfig = AppendConfig()
    request_body: RequestBodyConfig = RequestBodyConfig()
    auth_audit: AuthAuditConfig = AuthAuditConfig()
    denial_audit: DenialAuditConfig = DenialAuditConfig()
    view_audit: ViewAuditConfig = ViewAuditConfig()
    audit: ServerAuditConfig = ServerAuditConfig()
    query_limits: QueryLimitsConfig = QueryLimitsConfig()
    run: RunConfig = RunConfig()
    log: LogConfig = LogConfig()
    ci_oidc: CiOidcConfig = CiOidcConfig()
    readiness: ReadinessConfig = ReadinessConfig()
    ui: UiConfig = UiConfig()
    host: str = "127.0.0.1"
    port: int = 8000
    trusted_proxies: list[str] = []
    repo_roots: list[str] = []
    main_language: str = DEFAULT_MAIN_LANGUAGE

    @field_validator("repo_roots")
    @classmethod
    def _repo_roots_are_absolute(cls, value: list[str]) -> list[str]:
        """Validate `LAKANA_REPO_ROOTS`.

        Parameters
        ----------
        value
            The parsed list.

        Returns
        -------
        list of str
            The list, unchanged.
        """
        return validate_repo_roots(value)

    @field_validator("main_language")
    @classmethod
    def _main_language_is_a_tag(cls, value: str) -> str:
        """Refuse a main language that is not a canonical tag.

        Parameters
        ----------
        value
            `LAKANA_MAIN_LANGUAGE`.

        Returns
        -------
        str
            `value`, unchanged.

        Raises
        ------
        ValueError
            With the tag text.
        """
        problem = language_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def _trusted_proxies_are_addresses(cls, value: list[str]) -> list[str]:
        """Refuse a trusted proxy that is not an IP address or network.

        Parameters
        ----------
        value
            `LAKANA_TRUSTED_PROXIES`.

        Returns
        -------
        list of str
            `value`, unchanged.

        Raises
        ------
        ValueError
            Naming the setting and the entry.
        """
        for entry in value:
            try:
                network = ipaddress.ip_network(entry)
            except ValueError:
                network = None
            if network is None or network.prefixlen == 0:
                raise ValueError(
                    f"LAKANA_TRUSTED_PROXIES entries are IP addresses or networks of your "
                    f"reverse proxies, such as 10.0.0.2 or 10.1.0.0/16; {entry!r} is not one "
                    "(no host names, no wildcard, no network of every address such as "
                    "0.0.0.0/0)."
                )
        return value

    @model_validator(mode="after")
    def _local_sign_in_is_local(self) -> Self:
        """Refuse local sign-in anywhere but one person's own machine.

        Returns
        -------
        Settings
            These settings, unchanged.

        Raises
        ------
        ValueError
            Naming `LAKANA_UI__LOCAL_SIGN_IN` and `lakana server start --local`.
        """
        if self.ui.local_sign_in and not (
            _is_loopback_bind(self.host) and self.public_origin().loopback
        ):
            raise ValueError(
                "LAKANA_UI__LOCAL_SIGN_IN is only for one person's own machine: it needs "
                "LAKANA_HOST on a loopback address and the public address on localhost. It is "
                "set by `lakana server start --local`; on a team server leave it unset."
            )
        return self

    def public_origin(self) -> PublicOrigin:
        """Read the address browsers reach the interface at.

        Returns
        -------
        PublicOrigin
            From `LAKANA_UI__PUBLIC_URL`, or `http://localhost:<LAKANA_PORT>` when it is unset.
        """
        if self.ui.public_url is None:
            scheme, host, port = "http", LOCALHOST, self.port
        else:
            parts = urlsplit(self.ui.public_url)
            scheme, host = parts.scheme, parts.hostname or ""
            port = parts.port or _DEFAULT_PORTS[scheme]
        shown = host if port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
        return PublicOrigin(
            scheme=scheme,
            host=host,
            port=port,
            origin=f"{scheme}://{shown}",
            loopback=host == LOCALHOST,
        )

    def allowed_host_headers(self) -> frozenset[str]:
        """List the `Host` headers browser traffic may carry.

        Returns
        -------
        frozenset of str
            The public address as browsers send it; on `localhost` also `127.0.0.1` and
            `[::1]` with the same port, if any (the guard sends their pages on to `localhost`); and
            `LAKANA_UI__ALLOWED_HOSTS`.
        """
        public = self.public_origin()
        headers = {public.host_header, *self.ui.allowed_hosts}
        if public.loopback:
            port = public.host_header.removeprefix(LOCALHOST)
            headers |= {f"127.0.0.1{port}", f"[::1]{port}"}
        return frozenset(headers)


def _is_loopback_bind(host: str) -> bool:
    """Tell whether the server's listening address accepts this machine only.

    Parameters
    ----------
    host
        `LAKANA_HOST`.

    Returns
    -------
    bool
        True for `localhost` and any loopback IP address.
    """
    if host == LOCALHOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
