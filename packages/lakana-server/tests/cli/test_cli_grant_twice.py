"""A grant made twice through the CLI, against the real app (#673)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from typer.testing import Result

from lakana.cli._state import CliState

_CREATE = (
    "grant",
    "create",
    "--principal",
    "tess",
    "--role",
    "ANALYST",
    "--kind",
    "ENVIRONMENT",
    "--env",
    "dev",
)


def test_the_second_identical_grant_create_is_refused_and_names_the_first(
    cli_state: CliState, invoke_cli: Callable[..., Result], tmp_path: Path
) -> None:
    """The issue's steps: the second create fails with the first grant's id.

    One grant is left, and revoking it leaves none.
    """
    run = invoke_cli
    assert (
        run(
            cli_state, "env", "create", "dev", "--storage-root", f"file://{tmp_path}/lake"
        ).exit_code
        == 0
    )
    assert run(cli_state, "principal", "create", "tess", "--type", "user").exit_code == 0
    first = run(cli_state, "--output", "json", *_CREATE)
    assert first.exit_code == 0, first.stderr
    grant_id = json.loads(first.stdout)["id"]

    second = run(cli_state, *_CREATE)

    assert second.exit_code != 0
    assert (
        f"Principal 'tess' already holds ANALYST on environment 'dev' through grant {grant_id}; "
        "nothing was created." in " ".join(second.stderr.split())
    )
    listed = run(cli_state, "--output", "json", "grant", "list", "--principal", "tess")
    assert [g["id"] for g in json.loads(listed.stdout)] == [grant_id]
    assert run(cli_state, "grant", "revoke", grant_id, "--yes").exit_code == 0
    listed = run(cli_state, "--output", "json", "grant", "list", "--principal", "tess")
    assert json.loads(listed.stdout) == []
