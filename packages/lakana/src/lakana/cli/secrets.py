"""`lakana secret *`: write-only secrets."""

from __future__ import annotations

import json
import sys
from typing import Annotated

import typer

from lakana.cli import _output
from lakana.cli._state import CliError, confirm, parse_key_values, resolve_environment, state_of

secret_app = typer.Typer(help="Manage write-only secrets.", no_args_is_help=True)

_ENV = Annotated[str | None, typer.Option("--env", help="Environment (default: the profile's).")]
_VALUE = Annotated[
    list[str] | None,
    typer.Option(
        "--value", help="key=value (repeatable). Lands in shell history; prefer --values-stdin."
    ),
]
_STDIN = Annotated[
    bool,
    typer.Option(
        "--values-stdin",
        help="Read a JSON object of values from stdin (a leading byte order mark is dropped).",
    ),
]


_WHOLE = 100_000
"""No cut: a URL that is printed is the whole URL the secret goes to."""
_INDEX = Annotated[str | None, typer.Option("--index", help="Bind it to this package index.")]
_REPOSITORY = Annotated[
    str | None, typer.Option("--repository", help="Bind it to this repository.")
]
_CONNECTION = Annotated[
    str | None,
    typer.Option("--connection", help="Bind it to this connection; it becomes the secret it uses."),
]
_OWN_ENV = Annotated[
    bool, typer.Option("--own-env", help="Bind it to its environment: storage and webhooks.")
]


def chosen_use(
    index: str | None, repository: str | None, own_env: bool, connection: str | None = None
) -> tuple[str, str | None] | None:
    """Read the use the options name.

    Parameters
    ----------
    index
        A package index's name.
    repository
        A repository's name.
    own_env
        Whether the environment was named.
    connection
        A connection's name.

    Returns
    -------
    tuple of str and str or None
        The kind and the target's name (`None` for the environment), or `None` when no use
        was named.

    Raises
    ------
    CliError
        If more than one use was named (exit 2).
    """
    chosen = [
        (kind, target)
        for kind, target, given in (
            ("package_index", index, index is not None),
            ("repository", repository, repository is not None),
            ("connection", connection, connection is not None),
            ("environment", None, own_env),
        )
        if given
    ]
    if len(chosen) > 1:
        raise CliError("Pass only one of --index, --repository, --connection or --own-env")
    return chosen[0] if chosen else None


def read_stdin() -> str:
    """Read standard input as text, less one leading byte order mark.

    A Windows editor may start a file with a UTF-8 byte order mark, which is not part of a
    secret and which JSON does not allow.

    Returns
    -------
    str
        The text.
    """
    return sys.stdin.read().removeprefix("\ufeff")


def read_values(value: list[str] | None, values_stdin: bool) -> dict[str, str]:
    """Collect secret values from `--value` or `--values-stdin`.

    Parameters
    ----------
    value
        `--value` items.
    values_stdin
        Whether to read a JSON object from stdin.

    Returns
    -------
    dict of str to str
        The values.

    Raises
    ------
    CliError
        Unless exactly one source is used and it yields a string-to-string
        object (exit 2).
    """
    if bool(value) == values_stdin:
        raise CliError("Pass exactly one of --value key=value or --values-stdin")
    if not values_stdin:
        return parse_key_values(value, "--value", sensitive=True)
    try:
        data = json.loads(read_stdin())
    except json.JSONDecodeError as exc:
        raise CliError(f"--values-stdin expects a JSON object: {exc}") from exc
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise CliError("--values-stdin expects a JSON object of string keys and values")
    return data


def _use_text(kind: str, target: str | None, url: str | None) -> str:
    """Say what a secret was bound to, with the URL the secret now goes to.

    Parameters
    ----------
    kind
        ``package_index``, ``repository``, ``connection`` or ``environment``.
    target
        The index's, repository's or connection's name.
    url
        Its URL as the server holds it (for a connection, every ``host:port`` it sends the
        secret to); the person who binds sees where the secret goes.

    Returns
    -------
    str
        For example ``package index corp (https://pkgs.example.org/simple)``.
    """
    noun = {
        "package_index": "package index",
        "repository": "repository",
        "connection": "connection",
    }.get(kind)
    if noun is None:
        return "its environment"
    where = f" ({_output.inline(url, limit=_WHOLE)})" if url else ""
    return f"{noun} {_output.inline(target or '')}{where}"


@secret_app.command("create")
def create(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    value: _VALUE = None,
    values_stdin: _STDIN = False,
    index: _INDEX = None,
    repository: _REPOSITORY = None,
    connection: _CONNECTION = None,
    own_env: _OWN_ENV = False,
) -> None:
    """Create secret NAME; its values can never be read back.

    Name its use with `--index`, `--repository`, `--connection` or `--own-env` to bind it as it
    is created; that needs the right to manage secrets, as `secret bind` does. A connection's
    own secret is usually created with the connection (`lakana connection create ...
    --password-stdin`).

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The secret name.
    env
        The environment.
    value
        Values as key=value.
    values_stdin
        Read values as JSON from stdin.
    index
        A package index's name.
    repository
        A repository's name.
    connection
        A connection's name.
    own_env
        Bind it to its environment.
    """
    state = state_of(ctx)
    use = chosen_use(index, repository, own_env, connection)
    values = read_values(value, values_stdin)
    environment = resolve_environment(state, env)
    kind, target = use if use is not None else (None, None)
    secret = state.client().secrets.create(environment, name, values, kind=kind, target=target)
    made = f"Created secret {_output.inline(name)} in {_output.inline(environment)}"
    if kind is not None:
        made += f", bound to {_use_text(kind, target, secret.use_target_url)}"
    _output.done(state, made, secret)


@secret_app.command("list")
def list_secrets(ctx: typer.Context, env: _ENV = None) -> None:
    """List secrets (names and metadata only).

    `format_version` 1 marks a secret stored in the original format; set it
    again with `lakana secret rotate` to store it in the current one.

    `use` says what a secret may be used for: a package index or a repository (`use_target`
    names it), or its environment's storage and webhooks. A secret with no use serves its
    environment only; `lakana secret bind` gives it one.

    Parameters
    ----------
    ctx
        The Typer context.
    env
        The environment.
    """
    state = state_of(ctx)
    secrets = state.client().secrets.list(resolve_environment(state, env))
    _output.emit(
        state,
        secrets,
        columns=[
            "name",
            "environment",
            "use",
            "use_target",
            "format_version",
            "key_id",
            "created_by",
            "created_at",
        ],
        droppable=["created_by", "environment", "key_id", "format_version", "use_target"],
    )


@secret_app.command("rotate")
def rotate(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    value: _VALUE = None,
    values_stdin: _STDIN = False,
) -> None:
    """Replace secret NAME's values.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The secret name.
    env
        The environment.
    value
        Values as key=value.
    values_stdin
        Read values as JSON from stdin.
    """
    state = state_of(ctx)
    values = read_values(value, values_stdin)
    environment = resolve_environment(state, env)
    secret = state.client().secrets.rotate(environment, name, values)
    _output.done(
        state, f"Rotated secret {_output.inline(name)} in {_output.inline(environment)}", secret
    )


@secret_app.command("bind")
def bind(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    index: _INDEX = None,
    repository: _REPOSITORY = None,
    connection: _CONNECTION = None,
    own_env: _OWN_ENV = False,
) -> None:
    """Bind secret NAME to the one use it may serve; needs the right to manage secrets.

    Bound to a connection with `--connection`, the secret becomes the one the connection uses,
    in the same step; any other secret of that connection is unbound.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The secret name.
    env
        The environment.
    index
        A package index's name.
    repository
        A repository's name.
    connection
        A connection's name.
    own_env
        Bind it to its environment instead.

    Raises
    ------
    CliError
        Unless exactly one of the four options is given (exit 2).
    """
    chosen = chosen_use(index, repository, own_env, connection)
    if chosen is None:
        raise CliError("Pass exactly one of --index, --repository, --connection or --own-env")
    kind, target = chosen
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    secret = state.client().secrets.bind(environment, name, kind, target)
    what = _use_text(kind, target, secret.use_target_url)
    if kind == "connection":
        what += "; it is now the secret the connection uses"
    _output.done(
        state,
        f"Bound secret {_output.inline(name)} in {_output.inline(environment)} to {what}",
        secret,
    )


@secret_app.command("revoke")
def revoke(
    ctx: typer.Context,
    name: str,
    env: _ENV = None,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Revoke secret NAME.

    Parameters
    ----------
    ctx
        The Typer context.
    name
        The secret name.
    env
        The environment.
    yes
        Skip the confirmation prompt.
    """
    state = state_of(ctx)
    environment = resolve_environment(state, env)
    confirm(state, f"Revoke secret {environment}/{name}?", yes)
    state.client().secrets.revoke(environment, name)
    _output.done(state, f"Revoked secret {_output.inline(name)} in {_output.inline(environment)}")


def register(app: typer.Typer) -> None:
    """Add the `secret` group to the root app.

    Parameters
    ----------
    app
        The root app.
    """
    app.add_typer(secret_app, name="secret")
