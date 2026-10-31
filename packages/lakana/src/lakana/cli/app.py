"""The `lakana` command-line interface: root app, global options, and exit codes."""

from __future__ import annotations

import inspect
import re
import sys
import threading
import traceback
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Any

import typer
from typer.core import TyperGroup
from typer.models import CommandInfo, TyperInfo

import lakana
from lakana.cli import (
    _output,
    access,
    artifacts,
    audit,
    auth,
    checks,
    ci_trust,
    connections,
    deploy,
    deploys,
    environments,
    grants,
    groups,
    indexes,
    jobs,
    load,
    me,
    namespaces,
    policies,
    principals,
    profile,
    project_init,
    queries,
    repos,
    runs,
    search,
    secrets,
    server,
    tables,
    tokens,
)
from lakana.cli._state import CliError, CliState, ci_oidc_in_use, state_of
from lakana.errors import (
    AuthenticationError,
    ConfigError,
    Conflict,
    InvalidResponse,
    LakanaConnectionError,
    LakanaError,
    NotFound,
    PayloadTooLarge,
    PermissionDenied,
    QueryFailed,
    ServerError,
    ValidationError,
)

EXIT_CODES: tuple[tuple[type[LakanaError], int], ...] = (
    (ConfigError, 3),
    (AuthenticationError, 4),
    (PermissionDenied, 5),
    (NotFound, 6),
    (Conflict, 7),
    (ValidationError, 8),
    (QueryFailed, 9),
    (PayloadTooLarge, 10),
    (ServerError, 11),
    (LakanaConnectionError, 11),
)
"""SDK error type → process exit code. Documented in `packages/lakana/README.md`."""


class OutputFormat(StrEnum):
    """The values `--output` accepts."""

    table = "table"
    json = "json"


def exit_code_for(exc: LakanaError) -> int:
    """Map an SDK error to its exit code.

    Parameters
    ----------
    exc
        The error.

    Returns
    -------
    int
        The documented code, or 1 for an unlisted `LakanaError`.
    """
    for exc_type, code in EXIT_CODES:
        if isinstance(exc, exc_type):
            return code
    return 1


def _hint(exc: LakanaError) -> str | None:
    """Suggest a next step for an SDK error, where one exists.

    Parameters
    ----------
    exc
        The error.

    Returns
    -------
    str or None
        The hint.
    """
    if isinstance(exc, (AuthenticationError, PermissionDenied)) and ci_oidc_in_use():
        return (
            "an administrator can run 'lakana ci-trust explain' with the job's claims to see "
            "which rule matches; see https://lakana-labs.github.io/lakana/reference/ci-trust-rules/"
        )
    if isinstance(exc, AuthenticationError):
        return "run 'lakana login'"
    if isinstance(exc, ConfigError) and str(exc).startswith("No credentials"):
        return "run 'lakana login'"
    return None


class _LakanaGroup(TyperGroup):
    """The root command group: turns exceptions into messages and exit codes."""

    # `typer.Context` is a subclass of typer's own vendored `_click.Context`
    # (typer 0.27 vendors click internally), narrower than the `_click.Context`
    # parameter `TyperGroup.invoke` declares — a real Liskov violation only in
    # the type checker's eyes, since typer always passes a `typer.Context`
    # here in practice; `typer.Context` is also the only public spelling
    # allowed for this parameter.
    def invoke(  # ty: ignore[invalid-method-override]
        self, ctx: typer.Context
    ) -> Any:  # ruff: ignore[any-type]
        """Run the selected command, reporting failures.

        Parameters
        ----------
        ctx
            The root context.

        Returns
        -------
        Any
            Whatever the command returns.

        Raises
        ------
        typer.Exit
            With the mapped exit code, after printing the error.
        typer.Abort
            Propagated unchanged.
        typer.TyperException
            Propagated unchanged, except that a usage error under
            `--output json` is reported as a JSON envelope and becomes
            `typer.Exit(2)`.
        """
        try:
            return super().invoke(ctx)
        except (typer.Exit, typer.Abort):
            raise
        except typer.TyperException as exc:
            state = state_of(ctx)
            # An unknown command is resolved before the root callback stores the
            # output mode on the state, but the root's parsed `--output` (or
            # LAKANA_OUTPUT) is already in ctx.params.
            requested = ctx.params.get("output")
            if getattr(requested, "value", requested) == "json":
                state.output = "json"
            # Typer's usage errors (unknown command, missing argument, bad option)
            # carry exit code 2 and a formatted message; typer vendors click, so
            # match them by those attributes rather than by a private class.
            format_message = getattr(exc, "format_message", None)
            if state.output != "json" or getattr(exc, "exit_code", None) != 2 or not format_message:
                raise
            _output.error(state, "UsageError", format_message(), None)
            raise typer.Exit(2) from exc
        except CliError as exc:
            _output.error(state_of(ctx), exc.kind, str(exc), exc.hint)
            raise typer.Exit(exc.exit_code) from exc
        except LakanaError as exc:
            _output.error(state_of(ctx), type(exc).__name__, str(exc), _hint(exc))
            raise typer.Exit(exit_code_for(exc)) from exc
        except TimeoutError as exc:
            query_hint = "check later with 'lakana query status <id>'"
            _output.error(state_of(ctx), "QueryTimeout", str(exc), query_hint)
            raise typer.Exit(9) from exc
        except Exception as exc:
            state = state_of(ctx)
            if state.verbose:
                traceback.print_exc(file=sys.stderr)
            _output.error(
                state,
                "UnexpectedError",
                f"{type(exc).__name__}: {exc}",
                None if state.verbose else "re-run with --verbose for a traceback",
            )
            raise typer.Exit(1) from exc


def _ask_instance(state: CliState, result: dict[str, str | None]) -> None:
    """Fetch the instance's version into `result`, whatever happens.

    Runs in a daemon thread (see `_instance_version`), so it must catch everything itself.

    Parameters
    ----------
    state
        The CLI state.
    result
        Filled with ``url``, ``version`` and ``error``.
    """
    url: str | None = None
    version: str | None = None
    try:
        client = state.client()
        url = client.url
        result["url"] = url
        version = client.info().version
        error = None
    except ConfigError as exc:
        if str(exc).startswith("No credentials"):
            error = "no profile is configured; run 'lakana login'"
        else:
            error = str(exc)
    except AuthenticationError:
        error = "the server rejected the credential; run 'lakana login'"
    except LakanaConnectionError as exc:
        error = f"not reachable ({exc}); check the URL and that the server is running"
    except NotFound:
        error = "the server does not report its version; it predates 'lakana --version'"
    except InvalidResponse:
        error = "the URL did not answer like a Lakana server; check the URL"
    except LakanaError as exc:
        error = f"could not be asked ({exc})"
    except Exception:  # a version banner must never fail the command, whatever came back
        error = "the URL did not answer like a Lakana server; check the URL"
    result.update({"url": url, "version": version, "error": error})


def _instance_version(state: CliState) -> dict[str, str | None]:
    """Ask the configured instance for its version within one overall deadline, never failing.

    The lookup runs in a daemon thread that is abandoned at `state.version_deadline`, so a
    server that answers one byte at a time cannot hold the command; the request itself has
    that timeout too and is not retried.

    Parameters
    ----------
    state
        The CLI state; its request timeout is set to the deadline for this call.

    Returns
    -------
    dict
        ``url``, ``version`` and ``error``: the error is a sentence that names the cause and
        the next step, set when the version could not be read.
    """
    state.timeout = state.version_deadline
    result: dict[str, str | None] = {}
    thread = threading.Thread(target=_ask_instance, args=(state, result), daemon=True)
    thread.start()
    thread.join(state.version_deadline)
    if thread.is_alive():
        error = (
            f"did not answer within {state.version_deadline:g} s; "
            "check the URL and that the server is running"
        )
        return {"url": result.get("url"), "version": None, "error": error}
    return dict(result)


def _show_version(state: CliState) -> None:
    """Print the CLI's version, then the instance's.

    Parameters
    ----------
    state
        The CLI state.
    """
    if state.output != "json":
        typer.echo(f"lakana {lakana.__version__}")
    instance = _instance_version(state)
    if state.output == "json":
        _output.print_json({"cli": lakana.__version__, "instance": instance})
    else:
        label = "instance" if instance["url"] is None else f"instance {instance['url']}"
        typer.echo(
            f"{label}: {instance['version'] if instance['error'] is None else instance['error']}"
        )


_ROOT_HELP = (
    "Lakana's command line: a lightweight, self-hosted lakehouse.\n\n"
    "On this machine: 'lakana server start --local', then 'lakana init'. "
    "On a team's instance, start with 'lakana login'."
)
"""The root command's `--help` description (the docstring is for developers)."""

_NUMPYDOC_SECTION = re.compile(r"^[A-Z][A-Za-z ]*\n-{3,}[ \t]*$", re.MULTILINE)


def _user_help(doc: str) -> str:
    """Cut a numpydoc docstring down to the part a user reads in `--help`.

    Parameters
    ----------
    doc
        The docstring.

    Returns
    -------
    str
        The text before the first section heading (`Parameters`, `Returns`, `Raises`, ...).
    """
    text = inspect.cleandoc(doc)
    found = _NUMPYDOC_SECTION.search(text)
    return (text[: found.start()] if found else text).strip()


def _trim_help(app: typer.Typer) -> None:
    """Replace each command's docstring help with its user-facing part, recursively.

    Typer shows a command's whole docstring as its help unless `help=` is given; the
    project's numpydoc sections are for developers and must not reach `--help`.

    Parameters
    ----------
    app
        The app (or sub-app) to fix in place.
    """
    infos: list[CommandInfo | TyperInfo] = [*app.registered_commands]
    if app.registered_callback is not None:
        infos.append(app.registered_callback)
    for info in infos:
        if info.help is None and info.callback is not None and info.callback.__doc__:
            info.help = _user_help(info.callback.__doc__)
    for group in app.registered_groups:
        if group.typer_instance is not None:  # pragma: no branch -- `add_typer` always sets it
            _trim_help(group.typer_instance)


def _root(
    ctx: typer.Context,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Profile to use from the profile file.")
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="Override the profile's platform URL.")
    ] = None,
    output: Annotated[
        OutputFormat,
        typer.Option("--output", envvar="LAKANA_OUTPUT", help="Output format."),
    ] = OutputFormat.table,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="Show tracebacks for unexpected errors.")
    ] = False,
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the CLI's version and the instance's, then exit."),
    ] = False,
) -> None:
    """Lakana's command line: a lightweight, self-hosted lakehouse.

    Parameters
    ----------
    ctx
        The root context.
    profile
        `--profile`.
    url
        `--url`.
    output
        `--output`.
    verbose
        `--verbose`.
    version
        `--version`: print the versions and exit without running a command. The
        instance is asked for its version only when a profile or `LAKANA_URL`
        and `LAKANA_TOKEN` are configured, for at most a few seconds, and the
        command succeeds whatever the answer.

    Raises
    ------
    typer.Exit
        With code 0 after `--version` has printed. With neither a command nor
        `--version` the usage error "Missing command." is raised instead.
    """
    state = ctx.obj if isinstance(ctx.obj, CliState) else CliState()
    state.profile = profile
    state.url = url
    state.output = output.value
    state.verbose = verbose
    ctx.obj = state
    ctx.call_on_close(state.close)
    if version:
        _show_version(state)
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        ctx.fail("Missing command.")


_REGISTRARS: tuple[Callable[[typer.Typer], None], ...] = (
    auth.register,
    me.register,
    profile.register,
    environments.register,
    namespaces.register,
    tables.register,
    load.register,
    artifacts.register,
    queries.register,
    principals.register,
    groups.register,
    tokens.register,
    grants.register,
    access.register,
    policies.register,
    secrets.register,
    connections.register,
    repos.register,
    indexes.register,
    ci_trust.register,
    deploys.register,
    runs.register,
    search.register,
    jobs.register,
    checks.register,
    project_init.register,
    audit.register,
    server.register,
    deploy.register,
)
"""Each command module's `register`, in help order. Later tasks extend this."""


def build_app() -> typer.Typer:
    """Build the CLI app with every command group registered.

    Returns
    -------
    typer.Typer
        A fresh app (tests build their own).
    """
    app = typer.Typer(
        cls=_LakanaGroup,
        no_args_is_help=True,
        add_completion=False,
        pretty_exceptions_enable=False,
    )
    app.callback(invoke_without_command=True, help=_ROOT_HELP)(_root)
    for register in _REGISTRARS:
        register(app)
    _trim_help(app)
    return app


app = build_app()


def main() -> None:
    """Run the CLI (the `lakana` console script)."""
    app(prog_name="lakana")
