"""Configuration models for lakana-core."""

import logging
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from lakana_core.identifiers import shown
from lakana_core.log_setup import LOG_LEVELS


def _redact_database_url(url: str) -> str:
    """Render a database URL with any password hidden.

    Parameters
    ----------
    url
        A SQLAlchemy database URL, as stored in `DatabaseConfig.url`.

    Returns
    -------
    str
        `url` rendered through `sqlalchemy.engine.make_url` and
        `render_as_string(hide_password=True)`, or ``"***"`` if `url`
        cannot be parsed as a SQLAlchemy URL at all — an unparseable value
        might still carry a credential of its own, so it is redacted
        wholesale rather than echoed on a best-effort basis.
    """
    try:
        return make_url(url).render_as_string(hide_password=True)
    except ArgumentError:
        return "***"


class StorageConfig(BaseModel):
    """Where an environment's data lives, and how to reach it.

    Parameters
    ----------
    scheme
        Storage backend to use.
    root
        Root URI for the environment, such as ``file:///var/lakana/dev``.
    options
        Backend-specific options passed through to fsspec and pyiceberg,
        such as an endpoint or a region. `StorageResolver.for_environment`
        can merge a decrypted secret's values into this mapping, so its
        values never appear in this model's `repr`/`str` — see
        `__repr_args__`.
    """

    scheme: Literal["file", "s3", "abfs"]
    root: str
    options: dict[str, str] = {}

    def __repr_args__(self) -> Iterable[tuple[str | None, Any]]:
        """Yield repr fields with every `options` value redacted.

        `options` can hold a decrypted credential merged in by
        `StorageResolver.for_environment`
        (`lakana_core.storage.resolver._resolve_secret_values`). Every value
        is replaced with ``"***"`` unconditionally, keys included, rather
        than only values matching a secret-looking key name: `options` is
        backend configuration nothing needs to read back out of a `repr`,
        an endpoint or a region gains nothing from being printed either,
        and unconditional masking has no key-name heuristic (`secret`,
        `key`, `token`, ...) for a future backend's option name to fall
        outside of. `scheme` and `root` are structural, not secret, and
        stay visible.

        Yields
        ------
        tuple of (str or None, Any)
            Each field's name and value, as
            `pydantic.BaseModel.__repr_args__` defines them, with every
            `options` value replaced by ``"***"``.
        """
        for name, value in super().__repr_args__():
            if name == "options":
                yield name, dict.fromkeys(value, "***")
            else:
                yield name, value


class DatabaseConfig(BaseModel):
    """How to reach the metadata database.

    Parameters
    ----------
    url
        Address of the metadata database, for example
        `postgresql+psycopg://lakana:PASSWORD@postgres/lakana`. A password in it is hidden in logs
        and error messages. Required.
    echo
        Whether every SQL statement sent to the metadata database is written to the log. Off by
        default; turn it on only while you look for a problem, because it is very verbose.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    url: str
    echo: bool = False

    def __repr_args__(self) -> Iterable[tuple[str | None, Any]]:
        """Yield repr fields with `url`'s password redacted.

        Yields
        ------
        tuple of (str or None, Any)
            Each field's name and value, with `url` replaced by
            `_redact_database_url(url)`.
        """
        for name, value in super().__repr_args__():
            if name == "url":
                yield name, _redact_database_url(value)
            else:
                yield name, value


class SecretsConfig(BaseModel):
    """Configuration for the secrets subsystem.

    Parameters
    ----------
    master_key
        The key that protects every stored secret: 32 random bytes, base64-encoded. Unset (the
        default), the instance starts but no secret can be created or read. The Compose bundle
        generates one. Keep it safe: a lost key makes every stored secret unrecoverable. Set the
        same value on the server and on every worker.
    accept_format_1
        Whether to read secrets stored in the old format, written by a release before a secret was
        bound to its row. `false` (the default) refuses such a secret with a message that says how
        to set it again. `true` reads them, and the server and the worker log a warning at start
        while it is on. Turn it on, on the server and on every worker, only while you set old
        secrets again, and remove it afterwards.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    master_key: str | None = None
    accept_format_1: bool = False

    @field_validator("accept_format_1", mode="before")
    @classmethod
    def _accept_format_1_is_true_or_false(cls, value: object) -> bool:
        """Accept only ``true`` or ``false`` (any case), ``1`` or ``0``.

        Parameters
        ----------
        value
            The configured value: a bool, or the text of the environment variable.

        Returns
        -------
        bool
            The setting.

        Raises
        ------
        ValueError
            If it is anything else. The message never contains the value.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("true", "1"):
                return True
            if text in ("false", "0"):
                return False
        raise ValueError(
            "must be true or false, e.g. LAKANA_SECRETS__ACCEPT_FORMAT_1=true. It is false "
            "unless you set old-format secrets to be read (see the upgrade page)."
        )

    def __repr_args__(self) -> Iterable[tuple[str | None, Any]]:
        """Yield the `master_key` field redacted, then `accept_format_1`.

        Adding a field to this model means revisiting this method to yield
        it too.

        Yields
        ------
        tuple of (str or None, Any)
            ``("master_key", "***")`` when set, or ``("master_key", None)``
            when unset; then ``("accept_format_1", <bool>)``.
        """
        yield "master_key", ("***" if self.master_key is not None else None)
        yield "accept_format_1", self.accept_format_1


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s")
_USERINFO_PART = re.compile(r"(?:[A-Za-z0-9._~!$&'()*+,;=-]|%[0-9A-Fa-f]{2})+")
_URL_MESSAGE = "must be an http:// or https:// URL with a host, e.g. {example}"
_EMPTY_PORT_MESSAGE = (
    "the port after the colon is missing: write a port number, e.g. {example}, or drop the colon"
)


_TOKEN_LIKE_SEGMENT = re.compile(r"(?=[^/]*[0-9A-Z])[A-Za-z0-9._~%+-]{16,}")
"""A URL path segment that looks like a token: 16 or more URL-safe characters with a digit or a
capital letter in them. A long word of lowercase letters (a repository's name) does not match.
Real project and repository names match too (a cloud project id with digits), so a match is a
warning at start-up (`warn_about_tokens_in_url_paths`), never a refusal."""


def warn_about_tokens_in_url_paths(
    logger: logging.Logger, urls: Mapping[str, SecretStr | None]
) -> None:
    """Warn, once per setting, about a URL whose path has a segment that looks like a token.

    The log redactor removes a URL's userinfo, not its path, so a credential in the path would
    reach the logs. A project or repository name can look the same, so this warns and never
    refuses. The message names the setting and quotes neither the URL nor the segment. Called
    when the server and the worker start.

    Parameters
    ----------
    logger
        The process's logger.
    urls
        Each URL setting's environment variable name, and its value or `None`.
    """
    for name, value in urls.items():
        if value is None:
            continue
        path = urlsplit(value.get_secret_value()).path
        if any(_TOKEN_LIKE_SEGMENT.fullmatch(segment) for segment in path.split("/")):
            logger.warning(
                "%s has a path segment that looks like a token. A credential in a URL's path is "
                "not redacted from logs. If it is one, put it in the URL's userinfo instead "
                "(https://user:token@host/path), which is redacted. If it is not one (a project "
                "or repository name), nothing needs doing.",
                name,
            )


def _empty_port(netloc: str) -> bool:
    """Say whether a URL's host is followed by a colon and no port.

    Parameters
    ----------
    netloc
        The URL's network location, which `urlsplit` reads as having no port in this case.

    Returns
    -------
    bool
        `True` for ``host:``, ``user:pw@host:`` and ``[::1]:``.
    """
    return netloc.rpartition("@")[2].endswith(":")


def validate_proxy_url(
    value: str, noun: str = "proxy", example: str = "http://proxy.corp:3128"
) -> None:
    """Check a proxy (or package index) URL's shape without ever quoting it.

    Parameters
    ----------
    value
        The URL.
    noun
        What the URL is, for the error message ("proxy", "index").
    example
        A sample URL for the error message.

    Raises
    ------
    ValueError
        If it is not an `http(s)://host[:port]` URL without whitespace, a query string or a
        fragment; if the host is followed by a colon and no port (with a message of its own); or
        if it has userinfo that is not ``user:password`` with both
        parts non-empty and percent-encoded (a username alone would make git ask
        for the password, and an unencoded ``@`` splits the userinfo ambiguously).
        The message never contains the value.
    """
    message = _URL_MESSAGE.format(example=example)
    if _CONTROL.search(value) or _WHITESPACE.search(value):
        raise ValueError(message)
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        raise ValueError(message) from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(message)
    if _empty_port(parts.netloc):
        raise ValueError(_EMPTY_PORT_MESSAGE.format(example=example))
    if parts.fragment:
        raise ValueError(message)
    if "?" in value:
        raise ValueError(
            f"{noun} URL must not have a query string: put a credential in the URL's "
            "userinfo instead (https://user:token@host/path), which is redacted from logs"
        )
    if "@" in parts.netloc:
        userinfo = parts.netloc.rpartition("@")[0]
        username, _, password = userinfo.partition(":")
        if not (
            _USERINFO_PART.fullmatch(username)
            and _USERINFO_PART.fullmatch(password)
            and "@" not in userinfo
        ):
            raise ValueError(
                f"{noun} URL must include a password with its username, both percent-encoded "
                "(user:password@host)"
            )


def _check_no_proxy(value: str) -> None:
    """Refuse a proxy bypass list that names no host, or has control characters.

    Parameters
    ----------
    value
        The comma-separated list.

    Raises
    ------
    ValueError
        If it has no entry besides spaces and commas, or contains a control character.
    """
    if not any(entry.strip() for entry in value.split(",")) or _CONTROL.search(value):
        raise ValueError("must be a non-empty, single-line, comma-separated host list")


def _check_absolute_path(value: Path) -> None:
    """Require an absolute, single-line path.

    Parameters
    ----------
    value
        The path.

    Raises
    ------
    ValueError
        If the path is relative or contains a control character.
    """
    if not value.is_absolute() or _CONTROL.search(str(value)):
        raise ValueError("must be an absolute, single-line file path")


_SERVER_URL_MESSAGE = (
    "LAKANA_RUN__SERVER_URL must be an http:// or https:// URL of the Lakana server, "
    "without credentials, query or fragment, e.g. http://server:8000"
)
_SERVER_URL_EMPTY_PORT_MESSAGE = (
    "LAKANA_RUN__SERVER_URL has a colon after the host and no port: write the port, "
    "e.g. http://server:8000, or drop the colon"
)


def _checked_server_url(value: str) -> str:
    """Validate the server URL a Python task's harness talks to.

    Parameters
    ----------
    value
        The URL.

    Returns
    -------
    str
        The URL without a trailing slash.

    Raises
    ------
    ValueError
        If it is not an `http(s)://host[:port][/path]` URL, or carries
        userinfo, a query or a fragment, or if the host is followed by a colon and no port
        (with a message of its own). The message never contains the value.
    """
    if _CONTROL.search(value) or _WHITESPACE.search(value):
        raise ValueError(_SERVER_URL_MESSAGE)
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a non-numeric or out-of-range port
    except ValueError:
        raise ValueError(_SERVER_URL_MESSAGE) from None
    if parts.scheme in ("http", "https") and parts.hostname and _empty_port(parts.netloc):
        raise ValueError(_SERVER_URL_EMPTY_PORT_MESSAGE)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
    ):
        raise ValueError(_SERVER_URL_MESSAGE)
    return value.rstrip("/")


DEFAULT_MAINTENANCE_TIMEOUT = 1800.0
"""Seconds a maintenance run may take before it stops at a safe point (30 minutes)."""

DEFAULT_MAX_HISTORY_OPEN_VERSIONS = 1_000_000
"""Most open versions a history write takes on by default (`max_history_open_versions`)."""


class RunConfig(BaseModel):
    """Operator settings of the run engine (`LAKANA_RUN__*`; Python assets too).

    Validation errors never echo a value: the proxy and index URLs may carry a password.

    Parameters
    ----------
    max_retries
        Most retries a project may configure for one asset; deploy
        validation refuses more.
    scheduler_interval_seconds
        How often, in seconds, the worker that holds the scheduler role checks for work that is
        ready, retries that are due, lost workers and cancellations.
    lease_seconds
        How long, in seconds, a claimed task stays with its worker without the worker renewing the
        claim; after that another worker may take it.
    renew_seconds
        How often, in seconds, a worker renews its claim on a task. It must be below
        `LAKANA_RUN__LEASE_SECONDS`.
    drain_grace_seconds
        When a worker is asked to stop, how many seconds a running task may take to finish before
        the task is handed to another worker.
    server_url
        Base URL of the Lakana server as a Python task's subprocess reaches it.
        Unset, this worker cannot run Python assets.
    venv_cache
        Absolute path of the directory that keeps the Python environments of projects. Unset, the
        worker uses its own cache directory.
    venv_keep
        How many venvs to keep, most recently used first.
    venv_build_timeout
        Longest, in seconds, that building the Python environment of a project may take.
    venv_max_bytes
        Largest on-disk size of one venv while it is built.
    kill_grace
        Seconds between asking a Python task to stop and ending its process by force.
    sweep_interval
        Seconds between the worker's sweeps of attempt directories a dead worker left
        behind (also at worker start), and between the sweeps that the worker holding the
        scheduler role runs of the artifact store (expired uploads, files and manifest objects
        no version names) and of table locations (the files of creates that never finished);
        0 turns them off, and then nothing collects those leftovers.
    maintenance_timeout
        Seconds one `lakana table maintain` run may take. The worker checks the time between
        steps and between files, stops at the first safe point, and fails the run with the
        timeout cause; running maintenance again continues where it stopped.
    max_history_open_versions
        Most open versions a table built with `write: history` may have for a run to write it,
        its first run included. A history write holds every open version in memory, roughly 2 GiB
        per million at 8 columns and more for wider rows: the worker's for a SQL asset, the
        server's for a Python asset, which writes over the REST API. A run is refused before
        it reads them when the table's open versions or its result's rows exceed it (a first run
        before it creates the table), and before it writes when the open versions it would leave
        exceed it. Raise it together with `LAKANA_WORKER__MEMORY_LIMIT` (SQL asset) or the
        memory the server has (Python asset).
    index_url
        An extra package index for building the environment of a Python project, as a URL. It may
        carry a password, which is hidden in logs and error messages.
    https_proxy
        Proxy that the environment build uses, as a URL. It may carry a password, which is hidden
        in logs and error messages. Local runner only: refused with `LAKANA_RUN__RUNNER=docker`.
    no_proxy
        Comma-separated hosts that bypass the proxy.
    ca_bundle
        Absolute path of a PEM file of certificate authorities to trust in addition to the public
        ones: for a package index or proxy signed by a private certificate authority, and for the
        sources of ingestion tasks behind one. Builds and tasks trust it under both runners,
        added to Mozilla's roots. Only its certificate blocks are used; a file over 1 MiB, without
        a certificate or with a private key stops the worker at start.
    runner
        How Python tasks run: `local` (the default: as processes of the worker, not isolated
        from it) or `docker` (each task and each environment build in a container of its own,
        started by the launcher service).
    launcher_socket
        Absolute path of the launcher's Unix socket, in the volume the worker shares with the
        launcher service. Read with `LAKANA_RUN__RUNNER=docker` only.
    task_memory
        Memory limit of each task container and environment build container, with a unit such
        as 2GB or 512MB (binary: 1GB is 1024MB; at least 64MB). It is outside the worker's own
        memory limit. Read with `LAKANA_RUN__RUNNER=docker` only.
    task_cpus
        CPUs of each task container and environment build container. Read with
        `LAKANA_RUN__RUNNER=docker` only.
    task_pids
        Most processes in each task container and environment build container. Read with
        `LAKANA_RUN__RUNNER=docker` only.
    ingestion_roots
        Name to absolute host directory that a `directory` or `sqlite-file` connection may read
        under the local runner (JSON object; empty by default). The Docker runner ignores it: its
        launcher has `LAKANA_LAUNCHER__INGESTION_ROOTS`. The worker refuses at start the same
        directories the launcher refuses (system directories, the Docker socket's, its own
        socket's).
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    max_retries: int = Field(default=5, ge=0)
    scheduler_interval_seconds: float = Field(default=1.0, gt=0)
    lease_seconds: int = Field(default=60, gt=0)
    renew_seconds: int = Field(default=20, gt=0)
    drain_grace_seconds: float = Field(default=20.0, ge=0)
    server_url: str | None = None
    venv_cache: Path | None = None
    venv_keep: int = Field(default=10, ge=1)
    venv_build_timeout: float = Field(default=600.0, gt=0)
    venv_max_bytes: int = Field(default=5 * 1024**3, gt=0)
    kill_grace: float = Field(default=10.0, ge=0)
    sweep_interval: float = Field(default=3600.0, ge=0)
    maintenance_timeout: float = Field(default=DEFAULT_MAINTENANCE_TIMEOUT, gt=0)
    max_history_open_versions: int = Field(default=DEFAULT_MAX_HISTORY_OPEN_VERSIONS, gt=0)
    index_url: SecretStr | None = None
    https_proxy: SecretStr | None = None
    no_proxy: str | None = None
    ca_bundle: Path | None = None
    runner: Literal["local", "docker"] = "local"
    launcher_socket: Path = Path("/run/lakana-launcher/launcher.sock")
    task_memory: str = "2GB"
    task_cpus: float = Field(default=1.0, gt=0, le=1024, allow_inf_nan=False)
    task_pids: int = Field(default=256, ge=16, le=4_194_304)
    ingestion_roots: dict[str, str] = Field(default_factory=dict)

    @field_validator("runner", mode="before")
    @classmethod
    def _runner_is_local_or_docker(cls, value: object) -> object:
        """Accept the runner in any letter case, and name the two values otherwise.

        Parameters
        ----------
        value
            The configured text.

        Returns
        -------
        object
            The lower-case value.

        Raises
        ------
        ValueError
            If it is neither.
        """
        cleaned = value.strip().lower() if isinstance(value, str) else value
        if cleaned not in ("local", "docker"):
            raise ValueError("LAKANA_RUN__RUNNER must be local or docker.")
        return cleaned

    @field_validator("task_memory")
    @classmethod
    def _task_memory_parses(cls, value: str) -> str:
        """Refuse a task memory `parse_task_memory` cannot read.

        Parameters
        ----------
        value
            The configured text.

        Returns
        -------
        str
            `value`, unchanged.
        """
        parse_task_memory(value)
        return value

    @field_validator("server_url")
    @classmethod
    def _server_url_is_a_plain_http_url(cls, value: str | None) -> str | None:
        """Validate and normalise the server URL.

        Parameters
        ----------
        value
            The URL, or `None`.

        Returns
        -------
        str or None
            The URL without a trailing slash.
        """
        return None if value is None else _checked_server_url(value)

    @field_validator("index_url", "https_proxy")
    @classmethod
    def _urls_are_http_urls(cls, value: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        """Validate the index and proxy URLs with `validate_proxy_url`.

        Parameters
        ----------
        value
            The URL, or `None`.
        info
            Validation context; names the field for the error message.

        Returns
        -------
        SecretStr or None
            `value`, unchanged.
        """
        if value is not None:
            if info.field_name == "index_url":
                validate_proxy_url(
                    value.get_secret_value(), noun="index", example="https://pypi.corp/simple"
                )
            else:
                validate_proxy_url(value.get_secret_value())
        return value

    @field_validator("no_proxy")
    @classmethod
    def _no_proxy_is_one_line(cls, value: str | None) -> str | None:
        """Refuse an empty list or control characters.

        Parameters
        ----------
        value
            The bypass list, or `None`.

        Returns
        -------
        str or None
            `value`, unchanged.
        """
        if value is not None:
            _check_no_proxy(value)
        return value

    @field_validator("venv_cache", "ca_bundle", "launcher_socket")
    @classmethod
    def _paths_are_absolute(cls, value: Path | None) -> Path | None:
        """Require absolute, single-line paths.

        Parameters
        ----------
        value
            The path, or `None`.

        Returns
        -------
        Path or None
            `value`, unchanged.
        """
        if value is not None:
            _check_absolute_path(value)
        return value

    @model_validator(mode="after")
    def _renewal_beats_expiry(self) -> Self:
        """Require renewals to come before the lease lapses.

        Returns
        -------
        Self
            `self`.

        Raises
        ------
        ValueError
            If `renew_seconds` is not below `lease_seconds`.
        """
        if self.renew_seconds >= self.lease_seconds:
            raise ValueError("LAKANA_RUN__RENEW_SECONDS must be below LAKANA_RUN__LEASE_SECONDS")
        return self

    @model_validator(mode="after")
    def _docker_builds_go_through_the_egress_proxy(self) -> Self:
        """Refuse the local runner's build network settings with the docker runner.

        A build container reaches its package indexes only through the egress service, so the
        local runner's proxy would never reach it and is refused at start rather than ignored.
        (`ca_bundle` serves both runners: the worker mounts it into build and task containers.)

        Returns
        -------
        Self
            `self`.

        Raises
        ------
        ValueError
            If `runner` is ``docker`` and `https_proxy` is set, or `index_url` is
            a plain ``http://`` URL (the egress proxy tunnels https only).
        """
        if self.runner == "docker" and self.https_proxy is not None:
            raise ValueError(
                "LAKANA_RUN__HTTPS_PROXY applies to builds of the local runner; with "
                "LAKANA_RUN__RUNNER=docker builds reach the package indexes through the egress "
                "service: set LAKANA_EGRESS__UPSTREAM_PROXY there, and unset this one."
            )
        index = self.index_url.get_secret_value() if self.index_url is not None else ""
        if self.runner == "docker" and urlsplit(index).scheme.lower() == "http":
            raise ValueError(
                "LAKANA_RUN__INDEX_URL is a plain http:// URL; with LAKANA_RUN__RUNNER=docker "
                "builds reach package indexes only over https, through the egress proxy. Serve "
                "the index over https and write https:// in LAKANA_RUN__INDEX_URL, or keep "
                "LAKANA_RUN__RUNNER=local for this index."
            )
        return self


class GitConfig(BaseModel):
    """Operator network settings for the worker's git fetches (`LAKANA_GIT__*`).

    The worker runs git with a minimal environment, so `HTTPS_PROXY`, `NO_PROXY`
    and a CA store variable of the worker are never seen by it; these settings
    are passed to git explicitly instead, for HTTPS repositories only. SSH is
    unaffected (`ProxyCommand` is not supported). Only the worker uses them: the
    server and `migrate` load but ignore them, so the CA file need not exist
    there (the worker checks it at startup). Validation errors never echo the
    offending value, which may carry a proxy password.

    Parameters
    ----------
    https_proxy
        URL of the proxy that git uses for HTTPS repositories, for example
        `http://user:password@host:3128`; a username needs a password, and both are percent-
        encoded. A password is hidden in logs and error messages.
    no_proxy
        Comma-separated hosts, domains or CIDRs that bypass the proxy.
    ca_bundle
        Absolute path of a PEM file of certificate authorities that git trusts for HTTPS; it
        replaces the system store. The file must exist and be readable on the worker.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    https_proxy: SecretStr | None = None
    no_proxy: str | None = None
    ca_bundle: Path | None = None

    @field_validator("https_proxy")
    @classmethod
    def _proxy_is_an_http_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Validate the proxy URL with `validate_proxy_url`.

        Parameters
        ----------
        value
            The proxy URL, or `None`.

        Returns
        -------
        SecretStr or None
            `value`, unchanged.
        """
        if value is not None:
            validate_proxy_url(value.get_secret_value())
        return value

    @field_validator("no_proxy")
    @classmethod
    def _no_proxy_is_one_line(cls, value: str | None) -> str | None:
        """Refuse an empty list or control characters.

        Parameters
        ----------
        value
            The bypass list, or `None`.

        Returns
        -------
        str or None
            `value`, unchanged.

        Raises
        ------
        ValueError
            If it is empty or contains a control character.
        """
        if value is not None:
            _check_no_proxy(value)
        return value

    @field_validator("ca_bundle")
    @classmethod
    def _ca_bundle_is_an_absolute_path(cls, value: Path | None) -> Path | None:
        """Require an absolute, single-line path (the file itself is checked by the worker).

        Parameters
        ----------
        value
            The path, or `None`.

        Returns
        -------
        Path or None
            `value`, unchanged.

        Raises
        ------
        ValueError
            If the path is relative or contains a control character.
        """
        if value is not None:
            _check_absolute_path(value)
        return value


_ALLOW_HOST = re.compile(
    r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)


class ProxyAndCaBundleChecks(BaseModel):
    """The checks of an operator's ``PROXY`` and ``CA_BUNDLE`` settings, shared by every client.

    A subclass declares the fields `proxy` (`SecretStr` or `None`) and `ca_bundle` (`Path` or
    `None`) itself, so that they keep their place among its own fields.
    """

    @field_validator("proxy", check_fields=False)
    @classmethod
    def _proxy_is_an_http_url(cls, value: SecretStr | None) -> SecretStr | None:
        """Validate the proxy URL with `validate_proxy_url`.

        Parameters
        ----------
        value
            The proxy URL, or `None`.

        Returns
        -------
        SecretStr or None
            `value`, unchanged.
        """
        if value is not None:
            validate_proxy_url(value.get_secret_value())
        return value

    @field_validator("ca_bundle", check_fields=False)
    @classmethod
    def _ca_bundle_is_an_absolute_path(cls, value: Path | None) -> Path | None:
        """Require an absolute, single-line path (the file itself is checked where it is used).

        Parameters
        ----------
        value
            The path, or `None`.

        Returns
        -------
        Path or None
            `value`, unchanged.
        """
        if value is not None:
            _check_absolute_path(value)
        return value


class WebhookConfig(ProxyAndCaBundleChecks):
    """Operator network settings for the worker's webhooks (`LAKANA_WEBHOOK__*`).

    The worker sends a webhook only to a public ``https`` address on port 443. These settings
    are the operator's way to widen or route that, and nothing else widens it: the worker's own
    ``HTTPS_PROXY`` and CA variables are never read. Only the worker uses them; the server and
    `migrate` load but ignore them, so the CA file need not exist there. Validation errors never
    echo the offending value, which may carry a proxy password.

    Parameters
    ----------
    allow_hosts
        Comma-separated host names, or ``*.domain`` for every subdomain of a domain (not the
        domain itself), that may resolve to private addresses and use ``http`` and any port.
        Loopback, link-local and cloud metadata addresses stay refused, except loopback for the
        entry ``localhost`` itself. Matching ignores case and a trailing dot. For an internal
        chat server.
    proxy
        URL of the proxy that webhooks go through, for example `http://user:password@host:3128`.
        The worker checks the destination name, but the proxy looks it up again, so the proxy must
        refuse internal addresses itself. A password is hidden in logs and error messages.
    ca_bundle
        Absolute path of a PEM file of certificate authorities that webhooks trust; it replaces
        the built-in store and never turns verification off. The file must exist and be readable
        on the worker.
    """

    model_config = ConfigDict(hide_input_in_errors=True)

    allow_hosts: str | None = None
    proxy: SecretStr | None = None
    ca_bundle: Path | None = None

    @field_validator("allow_hosts")
    @classmethod
    def _allow_hosts_are_names(cls, value: str | None) -> str | None:
        """Require a comma-separated list of host names or ``*.domain`` patterns.

        Parameters
        ----------
        value
            The list, or `None`.

        Returns
        -------
        str or None
            `value`, unchanged.

        Raises
        ------
        ValueError
            If an entry is empty or is not a host name; the message does not quote the value.
        """
        if value is not None:
            entries = [entry.strip().rstrip(".") for entry in value.split(",")]
            # A number in the last label would let `*.0.0.1` match an IP address.
            if not all(
                _ALLOW_HOST.match(entry) and not entry.rsplit(".", 1)[-1].isdigit()
                for entry in entries
            ):
                raise ValueError(
                    "LAKANA_WEBHOOK__ALLOW_HOSTS must be a comma-separated list of host names "
                    "or *.domain patterns, such as chat.corp.example,*.hooks.example; "
                    "an IP address cannot be allowed, use its host name"
                )
        return value

    def allowed_hosts(self) -> tuple[str, ...]:
        """Return the allow-list, lower-cased and without trailing dots.

        Returns
        -------
        tuple[str, ...]
            The host names and ``*.domain`` patterns; empty when none are set.
        """
        if self.allow_hosts is None:
            return ()
        return tuple(entry.strip().rstrip(".").lower() for entry in self.allow_hosts.split(","))


class DenialAuditConfig(BaseModel):
    """Operator-level bound on `AUTHORIZATION_DENIED` audit rows.

    Parameters
    ----------
    per_principal_per_minute
        Most refusals the audit log records for one principal per minute, in each server and each
        worker. A refusal past the limit is still refused, but not recorded; the next row recorded
        for that principal says how many were left out, and each one is written to the process
        log. The first refusal of each action by a principal is always recorded. Raise it if you
        need every refusal in the table.
    """

    per_principal_per_minute: int = Field(default=30, gt=0)


class ViewAuditConfig(BaseModel):
    """Whether, and how often, a view of other people's activity is written to the audit log.

    Read from ``LAKANA_VIEW_AUDIT__ENABLED`` and ``LAKANA_VIEW_AUDIT__WINDOW_MINUTES``.

    Parameters
    ----------
    enabled
        Record an `ACTIVITY_VIEWED` event when someone sees other people's activity: a manager's
        listing of other people's queries (`lakana query list`), the readers of a table
        (`lakana table health`) and a page of the audit log with other people's events
        (`lakana audit list`) (default true). If the event cannot be written, the read is
        refused with 503. Turn it off only if you do not need to know who looked at whose
        activity.
    window_minutes
        Minutes during which the same view by the same person writes no second event (default
        60; 1 to 1,440). The next page, another status filter and a screen that refreshes itself
        are the same view; another environment, another table, another `--submitter` or another
        `--principal-id` is another view.
    """

    enabled: bool = True
    window_minutes: int = Field(default=60, ge=1, le=1440)


QUERY_TEXT_MODES: tuple[str, ...] = ("full", "hash", "off")
"""The values of `LAKANA_AUDIT__QUERY_TEXT`."""


class AuditConfig(BaseModel):
    """How much of a query's SQL text the audit log stores.

    Read from ``LAKANA_AUDIT__QUERY_TEXT``.

    Parameters
    ----------
    query_text
        What a new `QUERY_COMPLETED` event stores of the query's SQL text: ``full`` (the
        default) the text; ``hash`` only its SHA-256 and length, so that the same query can still
        be recognised; ``off`` neither. In any letter case. Set the same value on the server and
        on every worker, since both record queries. Events already written are not changed. The
        query's own record keeps its text either way, for its submitter.
    """

    query_text: str = "full"

    @field_validator("query_text")
    @classmethod
    def _query_text_is_one_of_three(cls, value: str) -> str:
        """Refuse a `query_text` the audit log does not know, and say what to write.

        Parameters
        ----------
        value
            The configured text.

        Returns
        -------
        str
            `value` in lower case, without surrounding blanks.

        Raises
        ------
        ValueError
            If it is not one of `QUERY_TEXT_MODES`.
        """
        cleaned = value.strip().lower()
        if cleaned not in QUERY_TEXT_MODES:
            raise ValueError(
                f"LAKANA_AUDIT__QUERY_TEXT must be full, hash or off (got '{shown(value)}')."
            )
        return cleaned


class TableReadsConfig(BaseModel):
    """How long the reads of a table are kept, and how far back its health shows them.

    Read from ``LAKANA_TABLE_READS__KEEP_DAYS`` and ``LAKANA_TABLE_READS__DEFAULT_DAYS``.

    Parameters
    ----------
    keep_days
        Days of read counts kept for each table (default 90). A worker deletes older counts, and
        the health of a table can look back at most this many days.
    default_days
        Days of reads that the health of a table shows when the request names none (default 30);
        at most `LAKANA_TABLE_READS__KEEP_DAYS`.
    """

    keep_days: int = Field(default=90, ge=1)
    default_days: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _window_fits_the_kept_period(self) -> Self:
        """Require the default window to lie within the kept period.

        Returns
        -------
        Self
            `self`.

        Raises
        ------
        ValueError
            If `default_days` is above `keep_days`.
        """
        if self.default_days > self.keep_days:
            raise ValueError(
                "LAKANA_TABLE_READS__DEFAULT_DAYS must not be above LAKANA_TABLE_READS__KEEP_DAYS: "
                "lower the window or raise the kept period."
            )
        return self


class TablesConfig(BaseModel):
    """How the worker cleans up after creates that never finished, and expires tables.

    Read from ``LAKANA_TABLES__*``.

    Parameters
    ----------
    abandoned_grace_hours
        Hours a create that did not finish, and was not made by a pipeline run, keeps its
        storage location before the worker's sweep removes the files it left (default 24; 1 to
        8,760). A create made by a run keeps it until that run has ended. The sweep runs every
        `LAKANA_RUN__SWEEP_INTERVAL` seconds and never removes a table that exists. Raise it if
        a create can take longer than a day.
    undo_days
        The worker reads it too, for the tables its sweep drops because their retention has
        passed; such a table can always be restored for at least one day, even with 0. Set the
        same value for the server and every worker.
    max_expiries_per_sweep
        The most tables one sweep drops because their retention has passed (default 100; 1 to
        10,000), the most overdue first; the rest follow at the next sweep. It bounds what one
        mistaken retention can remove at once, so lower it if that worries you.
    """

    abandoned_grace_hours: int = Field(default=24, ge=1, le=8760)
    undo_days: int = Field(default=7, ge=0, le=365)
    max_expiries_per_sweep: int = Field(default=100, ge=1, le=10_000)


class TableUndoConfig(BaseModel):
    """How long the server keeps a dropped table restorable, and warns of an expiry.

    Read from ``LAKANA_TABLES__UNDO_DAYS`` by the server, which stamps the period on each drop,
    and ``LAKANA_TABLES__EXPIRY_WARNING_DAYS``.

    Parameters
    ----------
    undo_days
        Days a dropped managed table keeps its files and can be restored with `lakana table
        undrop` (default 7; 0 to 365). After that the worker's sweep deletes its files, at the
        latest one sweep (`LAKANA_RUN__SWEEP_INTERVAL`) later. 0 deletes them at the next sweep
        and allows no undrop. A drop with `--purge` ignores it. Each drop records its own end
        of the period, so a change applies to later drops only. The workers read it too, for the
        tables they drop because their retention has passed, which keep at least one day; set
        the same value for the server and every worker.
    expiry_warning_days
        Days ahead `lakana table list` and `lakana table get` warn that a table expires because
        of its retention, and the default window of `lakana table expiring` (default 7; 0 to
        3,650). 0 turns the warning off.
    """

    undo_days: int = Field(default=7, ge=0, le=365)
    expiry_warning_days: int = Field(default=7, ge=0, le=3650)


DEFAULT_MAX_STATEMENT_CHARS = 100_000
"""The default of `QueryLimitsConfig.max_statement_chars` and of a deploy's SQL limit."""


_CAP_DIGITS = 9
"""A cap written with more digits is read as 999999999, which is no limit in practice."""


class QueryLimitsConfig(BaseModel):
    """Operator-level bounds on one query's execution and result.

    Parameters
    ----------
    max_result_rows
        Most rows a query may return. Exceeding it fails the query; the
        result is never truncated.
    max_result_bytes
        Most bytes a query result may take in memory. A result over the limit fails the query. It
        protects the worker from running out of memory; the check happens between batches of rows,
        so a result can pass the limit by a little before it is stopped.
    memory_limit
        Memory one query may use, written as DuckDB writes sizes, for example `1GB` or `512MiB`.
        Some operations go beyond it. Keep it well below the memory limit of the worker.
    max_write_rows
        Most rows one write run may write. A run that passes it fails and removes every file it
        wrote.
    timeout_seconds
        Longest one query may run, in seconds. For a write it includes the time to store the
        result, so a slow object store can fail a write with the time-limit message. At the limit
        the worker asks the query to stop and, after the grace period of
        `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS`, ends it.
    kill_grace_seconds
        Seconds between asking a query to stop (its time limit, a cancel, the worker's
        drain) and killing its process. 0 kills at once; it must be a finite number. A query that
        stops when asked ends with DuckDB's own clean error; one that had to be killed after its
        time limit or a cancel request says so in its message (a task's cancel and the worker's
        drain only log it).
    threads
        Processor threads one query may use, so that one query cannot take every core of the
        worker.
    max_temp_directory_size
        Disk a query may use for temporary files when it needs more than its memory limit, written
        as DuckDB writes sizes, for example `10GB`. Only local file storage allows it; with every
        other storage a query over its memory limit fails instead of using disk.
    max_statement_chars
        Longest SQL text, in characters, that is accepted: a submitted query or write, a row
        filter or column mask expression, and a SQL asset at deploy. A longer text is refused with
        a message that names this setting. The default is far above any statement written by hand.
    running_per_principal
        The most ad-hoc queries one user or service account runs at once, over every
        environment: `auto` or a whole number of 1 or more. A further query waits
        and is not refused. With `auto`, one principal may run ad-hoc queries on every worker
        that takes interactive work while there are one or two such workers, and on all of
        them but one from three on. Pipeline work is never counted. The server reads it too,
        for the reason a query waits.

    Notes
    -----
    The time limit is hard. `memory_limit` is DuckDB's own target inside the query's process and
    can be overshot; on Linux the worker's memory limit (`WorkerConfig.memory_limit`) then stops
    the largest query first, and the worker only if that was not enough. On macOS
    the worker stops without ending a query first.
    """

    max_result_rows: int = Field(default=1_000_000, gt=0)
    max_result_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    memory_limit: str = "1GB"
    max_write_rows: int = Field(default=100_000_000, gt=0)
    timeout_seconds: float = Field(default=900, gt=0)
    kill_grace_seconds: float = Field(default=10, ge=0, allow_inf_nan=False)
    threads: int = Field(default=4, gt=0)
    max_temp_directory_size: str = "10GB"
    max_statement_chars: int = Field(default=DEFAULT_MAX_STATEMENT_CHARS, gt=0)
    running_per_principal: int | Literal["auto"] = "auto"

    @field_validator("running_per_principal", mode="before")
    @classmethod
    def _auto_or_a_whole_number(cls, value: object) -> int | Literal["auto"]:
        """Accept `auto` in any letter case, or a whole number of 1 or more.

        Parameters
        ----------
        value
            The configured value: an int, or the text of the environment variable.

        Returns
        -------
        int or Literal["auto"]
            The setting.

        Raises
        ------
        ValueError
            If it is anything else (a bool, 0, a negative or fractional number, other text).
        """
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
        if isinstance(value, str):
            text = value.strip().lower()
            if text == "auto":
                return "auto"
            if text.isascii() and text.isdigit():
                number = int(text) if len(text) <= _CAP_DIGITS else 10**_CAP_DIGITS - 1
                if number >= 1:
                    return number
        raise ValueError(
            "LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL must be auto or a whole number of 1 or "
            f"more (got '{shown(str(value))}'). auto reserves one worker for other users once "
            "three or more workers take interactive work."
        )


def validate_repo_roots(roots: list[str]) -> list[str]:
    """Require every `LAKANA_REPO_ROOTS` entry to be an absolute path.

    Shared by the worker's and the server's settings.

    Parameters
    ----------
    roots
        The configured roots.

    Returns
    -------
    list of str
        `roots`, unchanged.

    Raises
    ------
    ValueError
        If an entry is not absolute (pydantic reports it as a settings error).
    """
    for root in roots:
        if not os.path.isabs(root):
            raise ValueError(f"LAKANA_REPO_ROOTS entries must be absolute paths, got {root!r}.")
    return roots


_MEMORY_UNITS = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}
_MEMORY_SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([a-z]*)", re.ASCII)
_MEMORY_TEXT_MAX = 32
_MEMORY_MAX = 1024**5
MIN_WORKER_MEMORY = 512 * 1024**2
"""Least a worker may be limited to: an idle worker is 170 to 190 MB resident, and the first
queries add to that, so 512 MB leaves about a factor of 2.5."""
DEFAULT_WORKER_MEMORY = "4GB"
"""The worker's limit when nothing else says."""


def parse_memory_limit(text: str) -> int | None:
    """Parse `LAKANA_WORKER__MEMORY_LIMIT`.

    Parameters
    ----------
    text
        A size such as ``"4GB"``, ``"4g"``, ``"512MB"`` or ``"3600m"``: ASCII digits and a
        unit (``B``, ``K``, ``M``, ``G``, ``T`` with an optional ``B`` or ``iB``, any case),
        or ``"0"``, ``"0b"`` or ``"off"`` for no limit. Units are binary here, as in Docker:
        ``GB`` is 1024 MB, and ``GiB`` and ``MiB`` mean the same as ``GB`` and ``MB``.
        (`LAKANA_QUERY_LIMITS__MEMORY_LIMIT` is read by DuckDB, where ``GB`` is decimal
        and ``GiB`` binary: ``1GB`` is 953.6 MiB.)

    Returns
    -------
    int or None
        The limit in bytes, or `None` for no limit.

    Raises
    ------
    ValueError
        If `text` is none of those, is more than 1 PiB, or is less than 512 MB. The
        message names the setting.
    """
    cleaned = text.strip().lower()
    if cleaned in {"0", "0b", "off"}:
        return None
    syntax = ValueError(
        "LAKANA_WORKER__MEMORY_LIMIT must be a size with a unit, such as 4GB, 512MB or 4g "
        "(binary: 1GB is 1024MB), or off for no limit."
    )
    matched = _MEMORY_SIZE.fullmatch(cleaned) if len(cleaned) <= _MEMORY_TEXT_MAX else None
    if matched is None or matched.group(2) not in _MEMORY_UNITS:
        raise syntax
    size = int(float(matched.group(1)) * _MEMORY_UNITS[matched.group(2)])
    if size == 0 or size > _MEMORY_MAX:
        raise syntax
    if size < MIN_WORKER_MEMORY:
        raise ValueError(
            "LAKANA_WORKER__MEMORY_LIMIT must be at least 512MB: a worker needs about that much "
            "to start. Use off to run without a limit."
        )
    return size


MIN_TASK_MEMORY = 64 * 1024**2
"""The least memory a task container may get."""


def parse_task_memory(text: str) -> int:
    """Parse `LAKANA_RUN__TASK_MEMORY`, a task container's memory limit.

    Parameters
    ----------
    text
        A size such as ``"2GB"`` or ``"512MB"``, binary units as in Docker.

    Returns
    -------
    int
        Bytes.

    Raises
    ------
    ValueError
        If it is not a size, is below 64 MB or above 1 PiB; the message names the setting.
    """
    cleaned = text.strip().lower()
    message = (
        "LAKANA_RUN__TASK_MEMORY must be a size with a unit, such as 2GB or 512MB (binary: "
        "1GB is 1024MB), at least 64MB."
    )
    matched = _MEMORY_SIZE.fullmatch(cleaned) if len(cleaned) <= _MEMORY_TEXT_MAX else None
    if matched is None or matched.group(2) not in _MEMORY_UNITS:
        raise ValueError(message)
    size = int(float(matched.group(1)) * _MEMORY_UNITS[matched.group(2)])
    if not MIN_TASK_MEMORY <= size <= _MEMORY_MAX:
        raise ValueError(message)
    return size


WORKER_TAKES: tuple[str, ...] = ("all", "interactive", "pipeline")
"""What `LAKANA_WORKER__TAKES` may be: all work, ad-hoc queries only, or pipeline work only."""


class WorkerConfig(BaseModel):
    """Settings of the worker process itself.

    Parameters
    ----------
    memory_limit
        The most memory the worker may use in all, with the Python tasks and `uv` it started, for
        example `4GB` (at least `512MB`; `off` or `0` for none). Units are binary: `GB` and `g`
        mean 1024 MB, unlike `LAKANA_QUERY_LIMITS__MEMORY_LIMIT`, where `GB` is decimal. A worker
        over the limit ends its tasks, records why and stops; Docker or the local-mode supervisor
        restarts it. Unset, the worker uses 90 percent of its container's memory limit, or 4GB
        without one. Linux measures the worker and what it started; macOS only the worker; Windows
        nothing.
    notifications
        Whether this worker delivers job notifications (`LAKANA_WORKER__NOTIFICATIONS`, on by
        default). Set to ``false`` on a worker that must not reach the webhook hosts (a worker
        in a network without outbound access); another worker delivers them. With no worker
        delivering, queued notifications wait.
    takes
        The work this worker takes (`LAKANA_WORKER__TAKES`): ``"all"`` (the default),
        ``"interactive"`` (ad-hoc queries) or ``"pipeline"`` (deploys, pipeline tasks and
        table maintenance), in any letter case. The worker says it when it registers.
    package_cache_max_bytes
        The size, in bytes, above which the worker trims uv's package download cache in the
        ``uv`` directory of its cache (10 GiB by default; 0 never trims). The trim runs in the
        worker's housekeeping, every `LAKANA_RUN__SWEEP_INTERVAL`, never while this worker builds
        an environment, and waits for no other: when another worker that shares the cache is
        building, it tries again at the next sweep. It removes first what no package refers to
        any more (``uv cache prune``), then, while the cache is still too large, the packages
        downloaded longest ago. Python environments that exist keep working; the next build that
        needs a removed package downloads it again.
    package_cache_min_age_hours
        A package downloaded less than this many hours ago is never removed by the trim (24 by
        default), so that the builds of the day do not download the same packages again.
    """

    memory_limit: str | None = None
    notifications: bool = True
    takes: str = "all"
    package_cache_max_bytes: int = Field(default=10 * 1024**3, ge=0)
    package_cache_min_age_hours: float = Field(default=24.0, ge=0)

    @field_validator("memory_limit")
    @classmethod
    def _memory_limit_parses(cls, value: str | None) -> str | None:
        """Refuse a value `parse_memory_limit` cannot read.

        Parameters
        ----------
        value
            The configured text, if any.

        Returns
        -------
        str or None
            `value`, unchanged.
        """
        if value is not None:
            parse_memory_limit(value)
        return value

    @field_validator("takes")
    @classmethod
    def _takes_is_one_of_three(cls, value: str) -> str:
        """Refuse a `takes` the worker does not know, and say what to write.

        Parameters
        ----------
        value
            The configured text.

        Returns
        -------
        str
            `value` in lower case, without surrounding blanks.

        Raises
        ------
        ValueError
            If it is not one of `WORKER_TAKES`.
        """
        cleaned = value.strip().lower()
        if cleaned not in WORKER_TAKES:
            raise ValueError(
                f"LAKANA_WORKER__TAKES must be all, interactive or pipeline (got '{shown(value)}')."
            )
        return cleaned


class LogConfig(BaseModel):
    """How much the server and the worker log (`LAKANA_LOG__LEVEL`).

    Parameters
    ----------
    level
        `DEBUG`, `INFO`, `WARNING` or `ERROR`, in any letter case. Lakana's own log lines and the
        server's are printed from this level up; other libraries only from `WARNING`. Above
        `INFO`, refusals past the audit rate leave no line in the log, only the summary row in the
        audit log. The request log, one line per request, is printed at `INFO` and `DEBUG` only.
    access
        Whether the server logs every request, one line each. On by default; `lakana server start
        --local` turns it off unless you pass `--log-requests`. Nothing else in the log changes.
    """

    level: str = "INFO"
    access: bool = True

    @field_validator("level")
    @classmethod
    def _level_is_one_of_four(cls, value: str) -> str:
        """Refuse a level the log set-up does not know, and say what to write.

        Parameters
        ----------
        value
            The configured text.

        Returns
        -------
        str
            `value` in upper case.

        Raises
        ------
        ValueError
            If it is not one of `lakana_core.log_setup.LOG_LEVELS`.
        """
        if value.upper() not in LOG_LEVELS:
            raise ValueError(
                f"LAKANA_LOG__LEVEL is '{shown(value)}'; set it to one of "
                f"{', '.join(LOG_LEVELS[:-1])} or {LOG_LEVELS[-1]} (any letter case). "
                "The default is INFO."
            )
        return value.upper()


class TaskTokensConfig(BaseModel):
    """Operator-level bounds on task-token minting.

    Parameters
    ----------
    max_ttl_seconds
        Longest lifetime, in seconds, that can be requested for a task token. A task token is the
        temporary credential a Python task uses to call Lakana. The default is one day. Lower it
        to shorten the time a leaked token would work. The server and the worker both read it; set
        the same value on both.
    """

    max_ttl_seconds: int = Field(default=86_400, gt=0)


class ArtifactsConfig(BaseModel):
    """Operator-level bounds on the artifact store.

    Read from ``LAKANA_ARTIFACTS__MAX_FILE_BYTES``, ``LAKANA_ARTIFACTS__MAX_BYTES``,
    ``LAKANA_ARTIFACTS__MAX_FILES`` and ``LAKANA_ARTIFACTS__UPLOAD_TTL_SECONDS``.

    Parameters
    ----------
    max_file_bytes
        Largest single file of an artifact version (default 2 GiB).
    max_bytes
        Largest total size of one artifact version (default 10 GiB).
    max_files
        Most files in one artifact version (default 10,000).
    upload_ttl_seconds
        How long an open upload may stay uncommitted, and how long a commit of it can be repeated
        (default one hour).
    """

    max_file_bytes: int = Field(default=2 * 1024**3, gt=0)
    max_bytes: int = Field(default=10 * 1024**3, gt=0)
    max_files: int = Field(default=10_000, gt=0)
    upload_ttl_seconds: int = Field(default=3600, gt=0)


class Settings(BaseSettings):
    """Runtime settings for lakana-core.

    Parameters
    ----------
    database
        Metadata database configuration.
    bootstrap_admin
        Name of the administrator created the first time the server starts, if no principal of
        that name exists. Its credential is shown once at the first start. Change it only before
        the first start.
    secrets
        Secrets subsystem configuration.
    query_limits
        Operator-level bounds on query execution and results
        (`LAKANA_QUERY_LIMITS__*`), enforced by `lakana_worker.executor`.
    run
        Run engine settings (`LAKANA_RUN__*`).
    git
        Proxy and CA settings of the worker's git fetches (`LAKANA_GIT__*`).
    webhook
        Allow-list, proxy and CA settings of the worker's webhooks (`LAKANA_WEBHOOK__*`).
    worker
        The worker process's own settings (`LAKANA_WORKER__*`), such as its hard
        memory limit.
    log
        How much the server and the worker log (`LAKANA_LOG__LEVEL`, default INFO).
    task_tokens
        Bounds on task-token minting (`LAKANA_TASK_TOKENS__*`); the worker reads
        the TTL cap for the tokens it mints for Python tasks.
    denial_audit
        Bound on `AUTHORIZATION_DENIED` rows per principal (`LAKANA_DENIAL_AUDIT__*`).
    audit
        How much of a query's SQL text the audit log stores (`LAKANA_AUDIT__QUERY_TEXT`).
    artifacts
        Artifact-store bounds (`LAKANA_ARTIFACTS__*`): file size, total size, file
        count and upload lifetime.
    table_reads
        How long the reads of a table are kept and how far back its health shows them
        (`LAKANA_TABLE_READS__*`).
    tables
        How long the storage location of a create that did not finish is kept
        (`LAKANA_TABLES__*`).
    repo_roots
        Absolute directories that local repositories must lie under, as a JSON list, for example
        `["/repos"]`. Empty (the default) refuses every local repository.
    """

    model_config = SettingsConfigDict(
        env_prefix="LAKANA_", env_nested_delimiter="__", hide_input_in_errors=True
    )

    database: DatabaseConfig
    bootstrap_admin: str = "admin"
    secrets: SecretsConfig = SecretsConfig()
    query_limits: QueryLimitsConfig = QueryLimitsConfig()
    run: RunConfig = RunConfig()
    git: GitConfig = GitConfig()
    webhook: WebhookConfig = WebhookConfig()
    worker: WorkerConfig = WorkerConfig()
    log: LogConfig = LogConfig()
    task_tokens: TaskTokensConfig = TaskTokensConfig()
    denial_audit: DenialAuditConfig = DenialAuditConfig()
    audit: AuditConfig = AuditConfig()
    artifacts: ArtifactsConfig = ArtifactsConfig()
    table_reads: TableReadsConfig = TableReadsConfig()
    tables: TablesConfig = TablesConfig()
    repo_roots: list[str] = []

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
