"""Runtime settings for lakana-server."""

from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakana_core.ci_oidc.config import CiOidcConfig
from lakana_core.config import (
    ArtifactsConfig,
    AuditConfig,
    DatabaseConfig,
    DenialAuditConfig,
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
        request past the limit is refused at once with 503 and a hint to retry; the SDK asks
        again.
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
    host
        Address the server listens on. `127.0.0.1` (the default) accepts connections from this
        machine only; the Compose bundle publishes the port through `LAKANA_BIND`.
    port
        Port the server listens on (default 8000).
    repo_roots
        Absolute directories that local repositories must lie under, as a JSON list, for example
        `["/repos"]`. Empty (the default) refuses every local repository.
    main_language
        The language catalog texts are in when a project does not declare its own
        (`main_language:` in lakana.yaml), and the language a reader without a choice gets
        (default `en`). A BCP 47 tag such as `de` or `fr-CH`. Set it before the first deploy:
        existing texts are marked with it.
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
    host: str = "127.0.0.1"
    port: int = 8000
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
