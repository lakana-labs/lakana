"""`--help` shows what a user needs, never the developer docstring."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from conftest import FakeApi, invoke, make_state

from lakana.cli.app import build_app

_NUMPYDOC_HEADING = re.compile(
    r"^\s*(Parameters|Returns|Raises|Yields|Notes|Examples|See Also|Attributes)\s*$"
    r"|^\s*-{4,}\s*$",
    re.MULTILINE,
)


def _paths(command: object, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """List every command path in the tree, groups included.

    Parameters
    ----------
    command
        The click command to walk.
    prefix
        The path to `command`.

    Returns
    -------
    list of tuple of str
        The path of `command` and of everything below it.
    """
    found = [prefix]
    for name, sub in getattr(command, "commands", {}).items():
        found.extend(_paths(sub, (*prefix, name)))
    return found


@pytest.mark.slow
def test_no_help_text_shows_a_numpydoc_section(tmp_path: Path) -> None:
    """Walk the whole command tree: no `--help` carries a docstring section."""
    paths = _paths(typer.main.get_command(build_app()))
    assert len(paths) > 60
    for path in paths:
        result = invoke(make_state(tmp_path, FakeApi()), *path, "--help")
        assert result.exit_code == 0, (path, result.output)
        assert not _NUMPYDOC_HEADING.search(result.output), (path, result.output)


def test_root_help_describes_both_ways_to_begin(tmp_path: Path) -> None:
    """The root help says what Lakana is and that only a remote instance needs a login."""
    result = invoke(make_state(tmp_path, FakeApi()), "--help")
    head = result.output.split("Options")[0]
    assert "login" in head
    assert "lakana server start --local" in head
    assert "login first" not in head.lower()
    assert len([line for line in head.splitlines() if line.strip()]) <= 5


def test_the_check_help_texts(tmp_path: Path) -> None:
    """The group and `check list` say what they are for, in the words of the design."""
    state = make_state(tmp_path, FakeApi())
    group = " ".join(invoke(state, "check", "--help").output.split())
    assert "Data quality checks: the results of the checks a run made." in group
    listing = " ".join(invoke(state, "check", "list", "--help").output.split())
    assert "List check results, newest first. With --run, the results of one run." in listing
    assert "check rows" not in group + listing
