"""Check ``ui/parity.yaml`` against the live CLI command tree and REST route table.

Every CLI command and every ``/api/v1`` route has one entry saying where the web interface
covers it (class ``ui`` or ``read`` with a part 1-8), or why it does not (class ``git`` or
``exception`` with a reason). The check walks the real command tree. The routes come from the
committed ``docs/site/reference/openapi.json`` (importing the whole server takes seconds); CI's
``generate_reference.py --check`` step (``.github/workflows/ci.yml``, not a pytest test) keeps
that file equal to the live application, so a new route cannot land without a decision.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from typer._click import Command
from typer.core import TyperGroup

ROOT = Path(__file__).resolve().parent.parent
PARITY_FILE = ROOT / "ui" / "parity.yaml"
OPENAPI_FILE = ROOT / "docs" / "site" / "reference" / "openapi.json"

CLASSES = ("ui", "read", "git", "exception")
PARTS = range(1, 9)
EXCEPTIONS = frozenset({"task-token mint", "POST /api/v1/task-tokens"})
"""The only keys allowed class ``exception``; a new one is a visible diff here."""


def load_entries(path: Path) -> dict[str, dict[str, Any]]:
    """Read the inventory file.

    Parameters
    ----------
    path
        The YAML file.

    Returns
    -------
    dict of str to dict
        The entries by key.
    """
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _leaves(command: Command, path: str) -> set[str]:
    """Collect the leaf command paths below a command, hidden ones included.

    Parameters
    ----------
    command
        A command or group.
    path
        The words that reach it, empty for the root.

    Returns
    -------
    set of str
        The paths of its leaves.
    """
    if not isinstance(command, TyperGroup):
        return {path}
    found: set[str] = set()
    for name, child in command.commands.items():
        found |= _leaves(child, f"{path} {name}".strip())
    return found


def live_keys() -> set[str]:
    """List every CLI command path and ``/api/v1`` operation the code defines.

    Returns
    -------
    set of str
        CLI paths such as ``table undrop`` and routes such as ``GET /api/v1/jobs``.
    """
    import typer.main

    from lakana.cli.app import build_app

    keys = _leaves(typer.main.get_command(build_app()), "")
    paths = json.loads(OPENAPI_FILE.read_text(encoding="utf-8"))["paths"]
    for path, methods in paths.items():
        if path.startswith("/api/v1/"):
            keys |= {f"{method.upper()} {path}" for method in methods}
    return keys


def _invalid(key: str, entry: Mapping[str, Any]) -> str | None:
    """Say what is wrong with one entry.

    Parameters
    ----------
    key
        The entry's key.
    entry
        Its value.

    Returns
    -------
    str or None
        The problem with the next step, or None when the entry is valid.
    """
    cls = entry.get("class")
    if cls not in CLASSES:
        return f"{key!r} has class {cls!r}; use one of {', '.join(CLASSES)}."
    if cls in ("ui", "read"):
        if entry.get("part") not in PARTS:
            return (
                f"{key!r} is class {cls} and needs `part:` 1-8 (the interface part that shows it)."
            )
        if "reason" in entry:
            return f"{key!r} is class {cls}; drop `reason:` (it belongs to git and exception)."
        return None
    if not entry.get("reason"):
        return f"{key!r} is class {cls} and needs a `reason:` (why the UI does not cover it)."
    if "part" in entry:
        return f"{key!r} is class {cls}; drop `part:` (give a part only to ui and read)."
    if cls == "exception" and key not in EXCEPTIONS:
        return (
            f"{key!r} is class exception, which is reserved for {sorted(EXCEPTIONS)}; "
            "use class git, or add it to EXCEPTIONS in scripts/check_ui_parity.py "
            "with the maintainer's approval."
        )
    return None


def problems(entries: Mapping[str, Mapping[str, Any]], live: set[str]) -> list[str]:
    """Compare the inventory with the live commands and routes.

    Parameters
    ----------
    entries
        The inventory by key.
    live
        The keys the code defines.

    Returns
    -------
    list of str
        One message per missing, stale or invalid entry; empty when all is well.
    """
    found = [
        f"{key!r} has no entry: add it to ui/parity.yaml with class ui or read and a part 1-8, "
        "or class git and a reason (Git is for what a Git repository defines or local tooling)."
        for key in sorted(live - entries.keys())
    ]
    found += [
        f"{key!r} is in ui/parity.yaml but no such command or route exists: remove it from "
        "ui/parity.yaml (or fix the spelling)."
        for key in sorted(entries.keys() - live)
    ]
    for key, entry in entries.items():
        if (message := _invalid(key, entry)) is not None:
            found.append(message)
    return found
