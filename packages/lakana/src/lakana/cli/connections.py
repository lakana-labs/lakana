"""`lakana connection *`: the sources an environment's assets read from.

A connection has a kind, an address and settings, and at most one secret. Each kind takes its
own options (`KIND_OPTIONS`); an option the kind does not take is refused before anything is
sent. `--password-stdin` stores standard input under the kind's single secret key, which this
module knows itself (`single_key`), so it asks the server nothing to learn it.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from lakana._models import Connection, ConnectionTest
from lakana.cli import _output
from lakana.cli._state import CliError, confirm, resolve_environment, state_of
from lakana.cli.secrets import read_values

connection_app = typer.Typer(
    help="Manage connections: the databases, file stores and APIs assets read from.",
    no_args_is_help=True,
)

SECRET_OPTIONS = ("--secret", "--password-stdin", "--values-stdin")
"""The three ways of giving a connection its secret."""

OPTION_FIELDS: dict[str, tuple[str, str]] = {
    "--host": ("address", "host"),
    "--port": ("address", "port"),
    "--database": ("address", "database"),
    "--url": ("address", "base_url"),
    "--bucket": ("address", "bucket"),
    "--region": ("address", "region"),
    "--endpoint": ("address", "endpoint"),
    "--account": ("address", "account"),
    "--container": ("address", "container"),
    "--root": ("address", "root"),
    "--path": ("address", "path"),
    "--user": ("settings", "user"),
    "--tls": ("settings", "tls"),
    "--ca-file": ("settings", "ca_certificate"),
    "--auth": ("settings", "auth"),
    "--username": ("settings", "username"),
    "--api-key-name": ("settings", "api_key_name"),
    "--api-key-in": ("settings", "api_key_in"),
    "--client-id": ("settings", "client_id"),
    "--token-url": ("settings", "token_url"),
    "--scope": ("settings", "scope"),
    "--access-key-id": ("settings", "access_key_id"),
    "--prefix": ("settings", "prefix"),
}
"""Each address or settings option, and the part and key it sets."""

_DATABASE = ("--host", "--port", "--database", "--user", "--tls", "--ca-file", *SECRET_OPTIONS)
KIND_OPTIONS: dict[str, tuple[str, ...]] = {
    "postgresql": _DATABASE,
    "mariadb": _DATABASE,
    "sqlite-file": ("--root", "--path"),
    "http": (
        "--url",
        "--auth",
        "--username",
        "--api-key-name",
        "--api-key-in",
        "--client-id",
        "--token-url",
        "--scope",
        "--ca-file",
        *SECRET_OPTIONS,
    ),
    "s3": ("--bucket", "--region", "--endpoint", "--access-key-id", "--prefix", *SECRET_OPTIONS),
    "gcs": ("--bucket", "--prefix", *SECRET_OPTIONS),
    "azure": ("--account", "--container", "--prefix", *SECRET_OPTIONS),
    "directory": ("--root", "--path"),
    "tcp": ("--host", "--port", *SECRET_OPTIONS),
}
"""The options each kind takes, in the order a refusal names them."""

_SINGLE_KEY = {
    "postgresql": "password",
    "mariadb": "password",
    "s3": "secret_access_key",
    "gcs": "service_account_json",
    "azure": "account_key",
    "tcp": "password",
}
"""The key `--password-stdin` stores stdin under, per kind; `http` depends on its auth."""

_HTTP_SINGLE_KEY = {
    "bearer": "token",
    "basic": "password",
    "api_key": "api_key",
    "oauth2": "client_secret",
}
"""The key of an `http` connection's secret, per `auth`; `none` takes no secret."""


class Kind(StrEnum):
    """The kinds of connection, as `--kind` takes them."""

    postgresql = "postgresql"
    mariadb = "mariadb"
    sqlite_file = "sqlite-file"
    http = "http"
    s3 = "s3"
    gcs = "gcs"
    azure = "azure"
    directory = "directory"
    tcp = "tcp"


def single_key(kind: str, auth: str) -> str | None:
    """Name the key `--password-stdin` stores standard input under.

    A copy of `lakana_core.connections.kinds.single_secret_key`'s table; a test in
    `lakana-core` keeps the two equal.

    Parameters
    ----------
    kind
        The connection's kind.
    auth
        Its `auth` setting (only `http` reads it).

    Returns
    -------
    str or None
        The key; `None` for a kind or auth that takes no secret.
    """
    if kind == "http":
        return _HTTP_SINGLE_KEY.get(auth)
    return _SINGLE_KEY.get(kind)


_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_HOST = Annotated[str | None, typer.Option("--host", help="postgresql, mariadb, tcp: host name.")]
_PORT = Annotated[
    int | None,
    typer.Option("--port", help="postgresql (5432), mariadb (3306), tcp (required): port."),
]
_DATABASE_NAME = Annotated[
    str | None, typer.Option("--database", help="postgresql, mariadb: database name.")
]
_URL = Annotated[
    str | None, typer.Option("--url", help="http: base URL, such as https://api.example.com/v1.")
]
_BUCKET = Annotated[str | None, typer.Option("--bucket", help="s3, gcs: bucket.")]
_REGION = Annotated[str | None, typer.Option("--region", help="s3: region.")]
_ENDPOINT = Annotated[
    str | None,
    typer.Option("--endpoint", help="s3: https URL (default: https://s3.<region>.amazonaws.com)."),
]
_ACCOUNT = Annotated[str | None, typer.Option("--account", help="azure: storage account.")]
_CONTAINER = Annotated[str | None, typer.Option("--container", help="azure: container.")]
_ROOT = Annotated[
    str | None,
    typer.Option("--root", help="sqlite-file, directory: the worker's ingestion root name."),
]
_PATH = Annotated[
    str | None, typer.Option("--path", help="sqlite-file, directory: relative path in the root.")
]
_USER = Annotated[str | None, typer.Option("--user", help="postgresql, mariadb: user name.")]
_TLS = Annotated[
    str | None,
    typer.Option("--tls", help="postgresql, mariadb: verify-full (default), require or disable."),
]
_CA_FILE = Annotated[
    Path | None,
    typer.Option("--ca-file", help="postgresql, mariadb, http: PEM file of your CA certificates."),
]
_AUTH = Annotated[
    str | None,
    typer.Option("--auth", help="http: none (default), bearer, basic, api_key or oauth2."),
]
_USERNAME = Annotated[str | None, typer.Option("--username", help="http (basic): user name.")]
_API_KEY_NAME = Annotated[
    str | None, typer.Option("--api-key-name", help="http (api_key): header or parameter name.")
]
_API_KEY_IN = Annotated[
    str | None, typer.Option("--api-key-in", help="http (api_key): header (default) or query.")
]
_CLIENT_ID = Annotated[str | None, typer.Option("--client-id", help="http (oauth2): client id.")]
_TOKEN_URL = Annotated[
    str | None, typer.Option("--token-url", help="http (oauth2): https URL of the token endpoint.")
]
_SCOPE = Annotated[str | None, typer.Option("--scope", help="http (oauth2): scope.")]
_ACCESS_KEY_ID = Annotated[str | None, typer.Option("--access-key-id", help="s3: access key id.")]
_PREFIX = Annotated[
    str | None, typer.Option("--prefix", help="s3, gcs, azure: only objects under this prefix.")
]
_SECRET = Annotated[
    str | None,
    typer.Option("--secret", help="Use this existing secret of the environment."),
]
_PASSWORD_STDIN = Annotated[
    bool,
    typer.Option(
        "--password-stdin",
        help="Read the secret from stdin: the password, token or key the kind takes.",
    ),
]
_VALUES_STDIN = Annotated[
    bool,
    typer.Option("--values-stdin", help="Read the secret's keys as a JSON object from stdin."),
]


def _refuse_foreign(kind: str, given: Mapping[str, object], name: str) -> None:
    """Refuse an option the kind does not take, naming the kind's options.

    Parameters
    ----------
    kind
        The connection's kind.
    given
        The options given, by name, in the order they are declared.
    name
        The connection's name, for the message about a kind this release does not know.

    Raises
    ------
    CliError
        For a kind a newer server has and this command line does not know, or for the first
        option the kind does not take (exit 2).
    """
    allowed = KIND_OPTIONS.get(kind)
    if allowed is None:
        raise CliError(
            f"connection {_output.inline(name)} is of kind {_output.inline(kind)}, which this "
            "lakana command line does not know; upgrade lakana to change it (the server is newer)"
        )
    for option in given:
        if option not in allowed:
            raise CliError(
                f"{option} does not apply to kind {kind}; its options are {', '.join(allowed)}"
            )


def _read_ca(path: Path) -> str:
    """Read a CA certificate file.

    Parameters
    ----------
    path
        The `--ca-file` path.

    Returns
    -------
    str
        Its text.

    Raises
    ------
    CliError
        If it cannot be read (exit 2).
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        raise CliError(f"cannot read --ca-file {path}: {reason}") from exc


def _parts(given: Mapping[str, object]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the address and settings from the options given.

    Parameters
    ----------
    given
        The address and settings options given, by name.

    Returns
    -------
    tuple of (dict, dict)
        The address and the settings; `--ca-file` becomes the certificate's text.
    """
    parts: dict[str, dict[str, Any]] = {"address": {}, "settings": {}}
    for option, value in given.items():
        if option not in OPTION_FIELDS:
            continue
        part, key = OPTION_FIELDS[option]
        parts[part][key] = _read_ca(value) if isinstance(value, Path) else value
    return parts["address"], parts["settings"]


def _secret_values(
    kind: str, auth: str, password_stdin: bool, values_stdin: bool
) -> dict[str, str] | None:
    """Read new secret values from standard input, if asked to.

    Parameters
    ----------
    kind
        The connection's kind.
    auth
        Its `auth` setting after the change.
    password_stdin
        `--password-stdin`: stdin, less one trailing newline, under the kind's single key.
    values_stdin
        `--values-stdin`: a JSON object of text keys and values.

    Returns
    -------
    dict of str to str or None
        The values, or `None` when neither option was given.

    Raises
    ------
    CliError
        If the kind or auth takes no secret, or the JSON is not an object of text (exit 2).
    """
    if values_stdin:
        return read_values(None, True)
    if not password_stdin:
        return None
    key = single_key(kind, auth)
    if key is None:
        raise CliError(
            f"--password-stdin: kind {kind} with auth {auth} takes no secret; give --auth, or "
            "leave out --password-stdin"
        )
    value = sys.stdin.read()
    return {key: value.removesuffix("\n")}


_OPTION_TYPES: dict[str, Any] = {
    "--host": _HOST,
    "--port": _PORT,
    "--database": _DATABASE_NAME,
    "--url": _URL,
    "--bucket": _BUCKET,
    "--region": _REGION,
    "--endpoint": _ENDPOINT,
    "--account": _ACCOUNT,
    "--container": _CONTAINER,
    "--root": _ROOT,
    "--path": _PATH,
    "--user": _USER,
    "--tls": _TLS,
    "--ca-file": _CA_FILE,
    "--auth": _AUTH,
    "--username": _USERNAME,
    "--api-key-name": _API_KEY_NAME,
    "--api-key-in": _API_KEY_IN,
    "--client-id": _CLIENT_ID,
    "--token-url": _TOKEN_URL,
    "--scope": _SCOPE,
    "--access-key-id": _ACCESS_KEY_ID,
    "--prefix": _PREFIX,
    "--secret": _SECRET,
    "--password-stdin": _PASSWORD_STDIN,
    "--values-stdin": _VALUES_STDIN,
}
"""Each address, settings and secret option, and its type with its help, in help order."""

_FLAGS = frozenset({"--password-stdin", "--values-stdin"})


def _with_kind_options(command: Callable[..., None]) -> Callable[..., None]:
    """Give a command every option of `_OPTION_TYPES` in place of its `**options`.

    `create` and `update` take the same two dozen options; declaring them once keeps the two
    commands from drifting apart. Typer reads a command's parameters from its signature, so
    the options are added there as keyword-only parameters and reach the command as
    `**options`, by parameter name (``ca_file`` for ``--ca-file``).

    Parameters
    ----------
    command
        A command whose last parameter is ``**options``.

    Returns
    -------
    Callable
        The same command, with the options in its signature.
    """
    signature = inspect.signature(command, eval_str=True)
    kept = [p for p in signature.parameters.values() if p.kind is not p.VAR_KEYWORD]
    added = [
        inspect.Parameter(
            option.removeprefix("--").replace("-", "_"),
            inspect.Parameter.KEYWORD_ONLY,
            default=False if option in _FLAGS else None,
            annotation=annotation,
        )
        for option, annotation in _OPTION_TYPES.items()
    ]
    # Typer, like `inspect.signature`, reads `__signature__` before the function's own.
    command.__dict__["__signature__"] = signature.replace(parameters=[*kept, *added])
    return command


def _given(options: Mapping[str, object]) -> dict[str, object]:
    """Keep the options that were given, by option name.

    Parameters
    ----------
    options
        The `**options` of a command, by parameter name, `None` or `False` when not given.

    Returns
    -------
    dict of str to object
        The given ones by option name (``--ca-file``), in `_OPTION_TYPES` order.
    """
    return {
        option: options[name]
        for option in _OPTION_TYPES
        if (name := option.removeprefix("--").replace("-", "_")) in options
        and options[name] is not None
        and options[name] is not False
    }


def _request_parts(
    kind: str, given: Mapping[str, object], name: str, auth: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str] | None]:
    """Check the options against the kind and build what the request sends.

    Parameters
    ----------
    kind
        The connection's kind.
    given
        The options given, by option name.
    name
        The connection's name, for a message.
    auth
        The connection's `auth` after the change (only `http` reads it).

    Returns
    -------
    tuple of (dict, dict, dict or None)
        The address, the settings and the new secret values (`None` when none were given).

    Raises
    ------
    CliError
        For an option the kind does not take, or a CA file or stdin that cannot be read
        (exit 2).
    """
    _refuse_foreign(kind, given, name)
    address, settings = _parts(given)
    values = _secret_values(kind, auth, "--password-stdin" in given, "--values-stdin" in given)
    return address, settings, values


def _one_secret_way(given: Mapping[str, object]) -> None:
    """Refuse two ways of giving the secret.

    Parameters
    ----------
    given
        The options given, by option name.

    Raises
    ------
    CliError
        If more than one of `SECRET_OPTIONS` was given (exit 2).
    """
    if sum(option in given for option in SECRET_OPTIONS) > 1:
        raise CliError("Pass only one of --secret, --password-stdin or --values-stdin")


def _pairs(values: Mapping[str, Any]) -> str:
    """Write an address or settings as ``key=value`` pairs on one line.

    Parameters
    ----------
    values
        The part.

    Returns
    -------
    str
        The pairs, separated by spaces; ``(none)`` for an empty part.
    """
    if not values:
        return "(none)"
    return " ".join(f"{_output.inline(str(k))}={_output.inline(str(v))}" for k, v in values.items())


def _declared(connection: Connection) -> str:
    """Write the assets that declare a connection.

    Parameters
    ----------
    connection
        The connection.

    Returns
    -------
    str
        ``repository: asset`` pairs separated by commas; ``(none)`` when none does.
    """
    if not connection.declared_by:
        return "(none)"
    return ", ".join(
        f"{_output.inline(item.repository)}: {_output.inline(item.asset)}"
        for item in connection.declared_by
    )


@connection_app.command("create")
@_with_kind_options
def create(
    ctx: typer.Context,
    name: str,
    kind: Annotated[Kind, typer.Option("--kind", help="The kind of source.")],
    env: _ENV = None,
    **options: Any,  # ruff: ignore[any-type]
) -> None:
    """Create connection NAME of kind --kind; needs the right to manage connections.

    Give the kind's address and settings options, and its secret one of three ways:
    `--password-stdin` (stdin becomes the password, token or key the kind takes),
    `--values-stdin` (a JSON object of several keys), both of which create a secret
    NAME-connection (NAME-connection-2, -3, ... when a secret holds that name) that only this
    connection uses; or `--secret` with an existing secret.
    The values are never shown again. A connection that needs no secret takes none.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The connection's name: lower-case letters, digits and '-', starting with a letter.
    kind
        Its kind.
    env
        The environment.
    **options
        The address, settings and secret options (`_OPTION_TYPES`), by parameter name.
    """
    given = _given(options)
    _one_secret_way(given)
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    address, settings, values = _request_parts(
        kind.value, given, name, str(given.get("--auth", "none"))
    )
    connection = state.client().connections.create(
        environment,
        name,
        kind=kind.value,
        address=address,
        settings=settings,
        secret=options.get("secret"),
        secret_values=values,
    )
    made = f"Created connection {_output.inline(name)} in {_output.inline(environment)}"
    if connection.secret is not None:
        how = "with its secret" if values is not None else "using secret"
        made += f" {how} {_output.inline(connection.secret)}"
    _output.done(state, made, connection)


@connection_app.command("list")
def list_connections(ctx: typer.Context, env: _ENV = None) -> None:
    """List the connections of an environment, newest first.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        The environment.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    connections = list(state.client().connections.iter(environment))
    _output.emit(
        state,
        connections,
        columns=["name", "kind", "address", "secret", "declared_by"],
        derived={"address": lambda row: _pairs(row.address), "declared_by": _declared},
        droppable=["declared_by", "secret"],
    )


@connection_app.command("show")
def show(ctx: typer.Context, name: str, env: _ENV = None) -> None:
    """Show connection NAME: its address, settings, secret's name and the assets that declare it.

    A CA certificate shows as its SHA-256 fingerprint; a secret's values never show.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The connection's name.
    env
        The environment.
    """
    state = state_of(ctx)
    connection = state.client().connections.get(resolve_environment(state, env), name)
    if state.output == "json":
        _output.print_json(connection)
        return
    lines = [
        f"Connection:  {_output.inline(connection.name)} ({_output.inline(connection.kind)})",
        f"Address:     {_pairs(connection.address)}",
        f"Settings:    {_pairs(connection.settings)}",
        f"Secret:      {_output.inline(connection.secret or '(none)')}",
        f"Declared by: {_declared(connection)}",
        f"Created:     {connection.created_at.isoformat()} by "
        f"{_output.inline(connection.created_by or '(unknown)')}",
        f"Updated:     {connection.updated_at.isoformat()}",
    ]
    sys.stdout.write(_output.safe_text("\n".join(lines)) + "\n")


@connection_app.command("update")
@_with_kind_options
def update(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    clear: Annotated[
        list[str] | None,
        typer.Option(
            "--clear", help="Remove 'secret' (unbinds it) or a setting, such as ca_certificate."
        ),
    ] = None,
    **options: Any,  # ruff: ignore[any-type]
) -> None:
    """Change connection NAME; only the options given change. Its name and kind never change.

    `--password-stdin` or `--values-stdin` sets the connection's own secret again (or creates
    NAME-connection, or the next free NAME-connection-N, when it has none). A change that could
    send its secret somewhere new (the address, a weaker --tls, another --ca-file, another --auth
    or --api-key-in) also needs the right to manage secrets, unless it gives new values in the
    same command.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The connection's name.
    env
        The environment.
    clear
        ``secret`` or settings keys to remove.
    **options
        The address, settings and secret options (`_OPTION_TYPES`), by parameter name.

    Raises
    ------
    CliError
        If nothing would change (exit 2).
    """
    given = _given(options)
    _one_secret_way(given)
    if not given and not clear:
        raise CliError("Nothing to change; pass at least one option.")
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    client = state.client()
    current = client.connections.get(environment, name)
    auth = given.get("--auth", current.settings.get("auth", "none"))
    address, settings, values = _request_parts(current.kind, given, name, str(auth))
    connection = client.connections.update(
        environment,
        name,
        address=address or None,
        settings=settings or None,
        secret=options.get("secret"),
        secret_values=values,
        clear=clear or (),
    )
    _output.done(
        state,
        f"Updated connection {_output.inline(name)} in {_output.inline(environment)}",
        connection,
    )


@connection_app.command("delete")
def delete(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete connection NAME; refused while a deployed project version declares it.

    Its secret stays in the environment, unbound.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The connection's name.
    env
        The environment.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    confirm(
        state,
        f"Delete connection {_output.inline(name)} in {_output.inline(environment)}? Its secret "
        "stays in the environment, unbound.",
        yes,
    )
    state.client().connections.delete(environment, name)
    _output.done(
        state, f"Deleted connection {_output.inline(name)} in {_output.inline(environment)}"
    )


_NEXT_STEPS: dict[str, str] = {
    "dns": "Check the host name.",
    "refused": "Check the port and the source's firewall.",
    "timeout": "Check the host, the port and the network path between the worker and the source.",
    "tls": (
        "Check the CA (`lakana connection update NAME --ca-file ca.pem`), or, when the message "
        "names the TLS version, that the server offers TLS 1.2 or newer."
    ),
    "not_allowed": "The address must be reachable from outside the Lakana stack.",
    "root_unavailable": "Check the root setting.",
    "not_found": "Check the path under the root (`lakana connection show NAME`).",
    "credentials": "Check the secret.",
    "failed": "See the worker's log.",
}
"""What to do about each outcome of a connection test that is not `ok`."""


def next_step(outcome: str | None) -> str | None:
    """Say what to do about the outcome of a connection test.

    Parameters
    ----------
    outcome
        The outcome.

    Returns
    -------
    str or None
        The advice; `None` for ``ok`` and for an outcome this version does not know.
    """
    return _NEXT_STEPS.get(outcome or "")


def _test_line(test: ConnectionTest) -> str:
    """Write what a test found, or where it is.

    Parameters
    ----------
    test
        The test.

    Returns
    -------
    str
        ``<outcome>: <message>`` once done; the state and id before.
    """
    if test.state != "done":
        return f"Test {_output.inline(test.id)} is {_output.inline(test.state)}."
    return f"{_output.inline(test.outcome or 'failed')}: {_output.inline(test.message or '')}"


@connection_app.command("test")
def test(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    no_wait: Annotated[
        bool, typer.Option("--no-wait", help="Queue the test and print its id; don't wait.")
    ] = False,
    test_id: Annotated[
        str | None,
        typer.Option("--id", help="Show an earlier test of NAME instead of starting one."),
    ] = None,
) -> None:
    """Test connection NAME: a worker checks that its host can be reached.

    Prints ``ok: <message>`` or ``<outcome>: <message>`` with what to do next; exits 1 unless the
    outcome is ``ok``. Waits up to 60 seconds for a worker.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The connection's name.
    env
        The environment.
    no_wait
        Print the test's id without waiting for the outcome.
    test_id
        Show this earlier test instead of starting one.

    Raises
    ------
    CliError
        If a worker did not finish the test in time (exit 1).
    typer.Exit
        With code 1 when the outcome is not ``ok``.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    try:
        result = state.client().connections.test(
            environment, name, wait=not no_wait, test_id=test_id
        )
    except TimeoutError as exc:
        raise CliError(str(exc), 1, kind="ConnectionTestTimeout") from exc
    again = (
        f"lakana connection test {_output.inline(name)} --env {_output.inline(environment)} "
        f"--id {_output.inline(result.id)}"
    )
    if state.output == "json":
        _output.print_json(result)
    elif result.state != "done":
        sys.stdout.write(_output.safe_text(f"{_test_line(result)} Look again with {again}") + "\n")
    else:
        sys.stdout.write(_output.safe_text(_test_line(result)) + "\n")
    if result.state != "done" or result.outcome == "ok":
        return
    advice = next_step(result.outcome)
    if advice:
        _output.next_step(state, advice)
    raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    """Add the `connection` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(connection_app, name="connection")
