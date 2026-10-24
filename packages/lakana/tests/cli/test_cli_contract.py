"""Contract between the CLI's registered commands, its command map, and the SDK."""

from __future__ import annotations

import ast
from pathlib import Path

import typer

import lakana
from lakana.cli._commands import COMMANDS, LOCAL_COMMANDS
from lakana.cli.app import build_app
from lakana.resources.queries import QueryRun


def registered_commands(app: typer.Typer) -> set[str]:
    """Walk the app's command tree into space-separated command paths.

    Parameters
    ----------
    app
        The CLI app.

    Returns
    -------
    set of str
        Every leaf command, e.g. `{"sql", "env list", "table append"}`.
    """
    paths: set[str] = set()

    def walk(command: object, prefix: str) -> None:
        subcommands = getattr(command, "commands", None)
        if not subcommands:
            # The bare root (no commands registered at all, before
            # any command group is wired up) is not itself a
            # command, so it must not be recorded as one.
            if prefix:
                paths.add(prefix)
            return
        for name, sub in subcommands.items():
            walk(sub, f"{prefix} {name}".strip())

    walk(typer.main.get_command(app), "")
    return paths


def resolve_sdk_method(key: str) -> object:
    """Find the SDK callable a command-map key names.

    Parameters
    ----------
    key
        `"client.sql"`, `"query_run.status"`, or `"<resource>.<method>"`.

    Returns
    -------
    object
        The bound or unbound callable.
    """
    owner, method = key.split(".")
    client = lakana.Client(url="http://x", token="lakana_pat_k_s")
    try:
        if owner == "client":
            return getattr(client, method)
        if owner == "query_run":
            return getattr(QueryRun, method)
        return getattr(getattr(client, owner), method)
    finally:
        client.close()


def test_every_registered_command_is_mapped_or_local() -> None:
    """No command exists outside the map (or the local-only set)."""
    unmapped = registered_commands(build_app()) - set(COMMANDS) - LOCAL_COMMANDS
    assert not unmapped, f"Commands missing from _commands.COMMANDS: {sorted(unmapped)}"


def test_every_mapped_command_is_registered() -> None:
    """The map names no command the app doesn't have."""
    missing = set(COMMANDS) - registered_commands(build_app())
    assert not missing, f"Mapped but not registered: {sorted(missing)}"


def test_every_mapped_sdk_method_exists() -> None:
    """Each map value names real SDK callables."""
    for command, keys in COMMANDS.items():
        assert keys, f"{command} maps to no SDK method"
        for key in keys:
            assert callable(resolve_sdk_method(key)), f"{command} -> {key}"


def _public_sdk_methods() -> set[str]:
    """Enumerate every public SDK method a CLI command must cover.

    Returns
    -------
    set of str
        Keys in the command map's format.
    """
    client = lakana.Client(url="http://x", token="lakana_pat_k_s")
    try:
        keys = {"client.sql", "client.whoami"}
        keys |= {f"query_run.{m}" for m in ("status", "wait", "result", "cancel")}
        for attr, resource in vars(client).items():
            if attr.startswith("_"):
                continue
            for method in dir(type(resource)):
                if not method.startswith("_") and callable(getattr(resource, method)):
                    keys.add(f"{attr}.{method}")
        return keys
    finally:
        client.close()


_TASK_ONLY = frozenset({"task_connections.get"})
"""SDK methods only a running task's own token is answered for: a command would only ever print
the refusal. `lakana.ingest` calls them inside the task; a person reads connections with
`lakana connection show`."""


def test_every_public_sdk_method_has_a_command() -> None:
    """Completeness: no SDK capability is missing from the CLI (plan ruling 2)."""
    covered = {key for keys in COMMANDS.values() for key in keys}
    missing = _public_sdk_methods() - covered - _TASK_ONLY
    assert not missing, f"SDK methods with no CLI command: {sorted(missing)}"


def test_every_local_command_is_registered() -> None:
    """`login`, `logout` and the `profile` commands exist."""
    assert registered_commands(build_app()) >= LOCAL_COMMANDS


def test_the_commands_the_core_message_tests_may_name_exist() -> None:
    """The core test cannot import the CLI, so its hand-written list is checked from here."""
    source = Path(__file__).parents[3] / "lakana-core" / "tests" / "test_runs_checks.py"
    listed: set[str] = set()
    for node in ast.parse(source.read_text()).body:
        if isinstance(node, ast.Assign) and [
            t.id for t in node.targets if isinstance(t, ast.Name)
        ] == ["_COMMANDS"]:
            listed = set(ast.literal_eval(node.value))
    assert listed, "the list of commands that the message tests may name was not found"
    assert listed <= registered_commands(build_app())
