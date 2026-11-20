"""State shared by every CLI command, and small helpers commands have in common."""

from __future__ import annotations

import os
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import typer

import lakana
from lakana._config import default_config_path, resolve_config
from lakana.cli import _output, _profiles
from lakana.errors import ConfigError

_DEFAULT_PORTS = {"http": 80, "https": 443}


class CliError(Exception):
    """A failure the CLI reports itself, with its own exit code.

    Parameters
    ----------
    message
        What went wrong, shown after `Error:`.
    exit_code
        The process exit code; 2 (usage) by default.
    hint
        An optional follow-up suggestion.
    kind
        The `type` reported in JSON error output.
    """

    def __init__(
        self, message: str, exit_code: int = 2, *, hint: str | None = None, kind: str = "UsageError"
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.hint = hint
        self.kind = kind


def env_supplies_credentials() -> bool:
    """Report whether `LAKANA_URL`/`LAKANA_TOKEN` supply this invocation's credentials.

    The SDK resolves them ahead of the profile file, so when either is set
    the profile's token (and its default environment) is not in play.

    Returns
    -------
    bool
        `True` if either variable is set.
    """
    return os.environ.get("LAKANA_URL") is not None or os.environ.get("LAKANA_TOKEN") is not None


_BOTH_CREDENTIALS = (
    "Both LAKANA_TOKEN and LAKANA_OIDC (or LAKANA_OIDC_TOKEN) are set, and Lakana does not guess "
    "which credential you meant. Unset LAKANA_TOKEN to deploy with the CI job's ID token, or "
    "unset LAKANA_OIDC and LAKANA_OIDC_TOKEN to use the stored token."
)
_ONLY_DEPLOY = (
    "LAKANA_OIDC (or LAKANA_OIDC_TOKEN) is set, and only `lakana deploy` (with or without "
    "--dry-run) exchanges the CI job's ID token; a CI token may do nothing else. Run this "
    "command with LAKANA_TOKEN or a profile instead, without LAKANA_OIDC."
)
_NO_URL = (
    "LAKANA_OIDC needs the Lakana server's URL to exchange the CI job's ID token at: set "
    "LAKANA_URL in the job (or pass --url)."
)


def _oidc_variables_set() -> bool:
    """Report whether `LAKANA_OIDC` or `LAKANA_OIDC_TOKEN` is set.

    Returns
    -------
    bool
        `True` if either is set and not empty.
    """
    return bool(os.environ.get("LAKANA_OIDC") or os.environ.get("LAKANA_OIDC_TOKEN"))


def ci_oidc_in_use() -> bool:
    """Report whether this invocation's credential is a CI job's ID token, without raising.

    Returns
    -------
    bool
        `True` when `LAKANA_OIDC` or `LAKANA_OIDC_TOKEN` is set and `LAKANA_TOKEN` is not.
    """
    return _oidc_variables_set() and not os.environ.get("LAKANA_TOKEN")


def ci_oidc_requested() -> bool:
    """Report whether this invocation should exchange the CI job's ID token for its credential.

    Returns
    -------
    bool
        `True` when `LAKANA_OIDC` or `LAKANA_OIDC_TOKEN` is set.

    Raises
    ------
    ConfigError
        If `LAKANA_TOKEN` is set as well: which one wins is not guessed (exit 3).
    """
    if not _oidc_variables_set():
        return False
    if os.environ.get("LAKANA_TOKEN"):
        raise ConfigError(_BOTH_CREDENTIALS)
    return True


def default_ci_client_factory(state: CliState, environment: str, repository: str) -> lakana.Client:
    """Build a client for a CI job by exchanging its ID token (`Client.from_ci_oidc`).

    Parameters
    ----------
    state
        The CLI state with `--url` and the request timeout.
    environment
        The environment the job deploys to.
    repository
        The repository it deploys.

    Returns
    -------
    lakana.Client
        A client holding the CI token.

    Raises
    ------
    ConfigError
        If neither `--url` nor `LAKANA_URL` names the server (exit 3).
    """
    url = state.url or os.environ.get("LAKANA_URL")
    if not url:
        raise ConfigError(_NO_URL)
    return lakana.Client.from_ci_oidc(
        url, environment=environment, repository=repository, timeout=state.timeout
    )


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """Return a URL's origin: scheme, host and effective port.

    Parameters
    ----------
    url
        The URL.

    Returns
    -------
    tuple or None
        `(scheme, host, port)`, lower-cased, with the scheme's default port
        filled in; `None` if the port isn't a valid number.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        return None
    effective = port if port is not None else _DEFAULT_PORTS.get(scheme)
    return scheme, (parts.hostname or "").lower(), effective


def default_client_factory(state: CliState) -> lakana.Client:
    """Build the SDK client the way the SDK itself resolves credentials.

    `--url` replaces only the URL; the token still comes from `LAKANA_TOKEN`
    or the profile, never from the command line. With a profile's token,
    `--url` must stay on the profile's own origin (scheme, host and
    effective port), so a typo or a malicious suggestion can't send the
    stored token to another host.

    Parameters
    ----------
    state
        The CLI state carrying `--profile`, `--url`, and the profile path.

    Returns
    -------
    lakana.Client
        A configured client.

    Raises
    ------
    ConfigError
        If `--url` leaves the origin of the profile whose token would be
        sent, or if `LAKANA_OIDC` asks for a CI exchange, which only `lakana deploy`
        makes (exit 3).
    """
    if ci_oidc_requested():
        raise ConfigError(_ONLY_DEPLOY)
    config = resolve_config(profile=state.profile, config_path=state.config_path)
    if state.url is None or env_supplies_credentials():
        return lakana.Client(url=state.url or config.url, token=config.token, timeout=state.timeout)
    target = _origin(state.url)
    if target is None or target != _origin(config.url):
        name = _profiles.active_profile_name(_profiles.load(state.profile_path()), state.profile)
        raise ConfigError(
            f"--url {state.url} doesn't match profile {name!r} ({config.url}); "
            "refusing to send its token elsewhere. Use LAKANA_URL/LAKANA_TOKEN or "
            f"`lakana login --profile NAME --url {state.url}`."
        )
    return lakana.Client(url=state.url, token=config.token, timeout=state.timeout)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def selected_url(state: CliState, *, include_default: bool = False) -> str | None:
    """Return the platform URL the user has aimed this invocation at, without contacting it.

    In order: `--url`, `LAKANA_URL`, the URL of the profile chosen with `--profile` or
    `LAKANA_PROFILE`, and, if `include_default`, the URL of the file's default profile.

    Parameters
    ----------
    state
        The CLI state with `--url`, `--profile` and the profile file's path.
    include_default
        Whether the default profile counts when nothing chose one.

    Returns
    -------
    str or None
        The URL, or `None` when nothing names one.

    Raises
    ------
    ConfigError
        If the profile file can't be read (symlink, permissions, invalid TOML).
    """
    url = state.url or os.environ.get("LAKANA_URL")
    if url:
        return url
    name = state.profile or os.environ.get("LAKANA_PROFILE")
    if name is None and not include_default:
        return None
    data = _profiles.load(state.profile_path())
    if name is None:
        name = data.get("default_profile")
    if name is None:
        return None
    return data.get("profiles", {}).get(name, {}).get("url")


def is_loopback_url(url: str) -> bool:
    """Report whether a URL names this machine.

    Parameters
    ----------
    url
        The URL.

    Returns
    -------
    bool
        `True` for `127.0.0.1`, `localhost` and `::1`.
    """
    return urlsplit(url).hostname in _LOOPBACK_HOSTS


def _login_client(url: str, token: str) -> lakana.Client:
    """Build a client from explicit credentials, for `lakana login`.

    Parameters
    ----------
    url
        The platform API base URL.
    token
        The credential being verified.

    Returns
    -------
    lakana.Client
        A configured client.
    """
    return lakana.Client(url=url, token=token)


def _open_browser(url: str) -> bool:
    """Open a URL in the person's browser, importing `webbrowser` only when it is needed.

    Parameters
    ----------
    url
        The page.

    Returns
    -------
    bool
        Whether a browser was started.
    """
    import webbrowser

    return webbrowser.open(url)


def _stdin_is_tty() -> bool:
    """Report whether stdin is an interactive terminal.

    Returns
    -------
    bool
        `sys.stdin.isatty()`.
    """
    return sys.stdin.isatty()


@dataclass
class CliState:
    """Global options and injectable dependencies for one CLI invocation.

    Parameters
    ----------
    profile
        `--profile`.
    url
        `--url`.
    output
        `"table"` or `"json"`.
    verbose
        `--verbose`.
    client_factory
        Builds the SDK client for ordinary commands.
    ci_client_factory
        Builds the SDK client of `lakana deploy` in a CI job, from its ID token.
    login_client_factory
        Builds a client from explicit credentials, for `login`.
    is_interactive
        Whether prompts may be shown.
    browser_opener
        Opens a URL in the person's browser, for `login` (`webbrowser.open`).
    sleep
        Waits between two polls of `login --device`; `time.sleep` by default.
    hostname
        Names this computer, for the token `login` creates; `socket.gethostname` by default.
    login_timeout
        Seconds `login` waits for the browser's answer: 10 minutes.
    config_path
        The profile file; `None` means the SDK's default location.
    timeout
        Request timeout in seconds of the client `default_client_factory` builds.
    version_deadline
        Seconds `lakana --version` waits in all for the instance's answer.
    """

    profile: str | None = None
    url: str | None = None
    output: str = "table"
    verbose: bool = False
    client_factory: Callable[[CliState], lakana.Client] = default_client_factory
    ci_client_factory: Callable[[CliState, str, str], lakana.Client] = default_ci_client_factory
    login_client_factory: Callable[[str, str], lakana.Client] = _login_client
    is_interactive: Callable[[], bool] = _stdin_is_tty
    browser_opener: Callable[[str], bool] = _open_browser
    sleep: Callable[[float], None] = time.sleep
    hostname: Callable[[], str] = socket.gethostname
    login_timeout: float = 600.0
    config_path: Path | None = None
    timeout: float = 30.0
    version_deadline: float = 3.0
    _client: lakana.Client | None = field(default=None, repr=False)

    def client(self) -> lakana.Client:
        """Return the SDK client, building it on first use.

        Returns
        -------
        lakana.Client
            The cached client.
        """
        if self._client is None:
            self._client = self.client_factory(self)
        return self._client

    def use_ci_oidc(self, environment: str, repository: str) -> None:
        """Exchange the CI job's ID token for this invocation's client, when one is asked for.

        Each call exchanges anew: a CI token queues one submission, so every `lakana deploy`
        gets its own. A line on standard error says what the token may do, never the token.

        Parameters
        ----------
        environment
            The environment the job deploys to.
        repository
            The repository it deploys.

        Raises
        ------
        ConfigError
            If `LAKANA_TOKEN` is set as well (exit 3).
        """
        if not ci_oidc_requested():
            return
        self.close()
        self._client = self.ci_client_factory(self, environment, repository)
        issued = self._client.ci_token
        if issued is not None:
            kinds = " or ".join(kind.replace("_", " ") for kind in issued.kinds)
            commit = f" at {issued.sha}" if issued.sha else ""
            typer.echo(
                f"Exchanged the CI job's ID token: acting as {_output.inline(issued.principal)} "
                f"for one {kinds} of {_output.inline(issued.repository)}{commit} to "
                f"{_output.inline(issued.environment)}, until {issued.expires_at.isoformat()}.",
                err=True,
            )

    def close(self) -> None:
        """Close the SDK client if one was built."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def profile_path(self) -> Path:
        """Return the profile file path in effect.

        Returns
        -------
        Path
            `config_path`, or the SDK's default location.
        """
        return self.config_path or default_config_path()


def state_of(ctx: typer.Context) -> CliState:
    """Return the invocation's `CliState` from the root context.

    Parameters
    ----------
    ctx
        Any context in the invocation.

    Returns
    -------
    CliState
        The state the root callback stored, or a fresh default.
    """
    obj = ctx.find_root().obj
    return obj if isinstance(obj, CliState) else CliState()


def resolve_environment(state: CliState, env: str | None) -> str:
    """Pick the environment for an environment-scoped command.

    Parameters
    ----------
    state
        The CLI state.
    env
        The `--env` value, if given.

    Returns
    -------
    str
        `env`, or the active profile's `default_environment` — the latter
        only when the profile also supplies the credentials, i.e. neither
        `LAKANA_URL` nor `LAKANA_TOKEN` is set.

    Raises
    ------
    CliError
        If neither applies (exit 2).
    """
    if env:
        return env
    if not env_supplies_credentials():
        default = _profiles.default_environment(state.profile_path(), state.profile)
        if default:
            return default
    raise CliError("No environment: pass --env or run 'lakana profile use PROFILE --env ENV'")


def confirm(state: CliState, prompt: str, yes: bool) -> None:
    """Ask before a destructive action; refuse outright without a TTY.

    Parameters
    ----------
    state
        The CLI state.
    prompt
        The question, e.g. `"Delete table dev/bronze.sales?"`.
    yes
        `--yes`: skip the question.

    Raises
    ------
    CliError
        If there is no TTY and no `--yes`, or the user declines (exit 2).
    """
    if yes:
        return
    prompt = _output.safe_text(prompt)
    if not state.is_interactive():
        raise CliError(f"{prompt} Refusing without a terminal; pass --yes to confirm.")
    if not typer.confirm(prompt, default=False):
        raise CliError("Aborted.")


def parse_key_values(
    items: list[str] | None, flag: str, *, sensitive: bool = False
) -> dict[str, str]:
    """Parse repeated `k=v` options into a dict.

    Parameters
    ----------
    items
        The raw option values.
    flag
        The option's name, for error messages.
    sensitive
        If `True`, a malformed item is never echoed in the error message —
        only its position — because the item may hold a secret value.

    Returns
    -------
    dict of str to str
        Parsed pairs; the value keeps any further `=`.

    Raises
    ------
    CliError
        If an item has no `=` or an empty key (exit 2).
    """
    parsed: dict[str, str] = {}
    for index, item in enumerate(items or [], start=1):
        key, sep, value = item.partition("=")
        if not sep or not key:
            if sensitive:
                raise CliError(f"{flag} #{index} expects key=value")
            raise CliError(f"{flag} expects key=value, got {item!r}")
        parsed[key] = value
    return parsed


def split_table_name(qualified: str) -> tuple[str, str]:
    """Split `namespace.table` at its last dot.

    Parameters
    ----------
    qualified
        E.g. `"bronze.sales"` or `"bronze.eu.sales"`.

    Returns
    -------
    tuple of str
        `(namespace, table)`.

    Raises
    ------
    CliError
        If there is no dot (exit 2).
    """
    namespace, sep, table = qualified.rpartition(".")
    if not sep or not namespace or not table:
        raise CliError(f"Expected namespace.table, got {qualified!r}")
    return namespace, table
