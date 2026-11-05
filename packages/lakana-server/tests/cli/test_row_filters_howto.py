"""The commands and messages of the row-filter how-to page, run against the real app."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import Result

from lakana.cli._state import CliState
from tests.cli.conftest import ADMIN_TOKEN, make_cli_state

_ALL_ROWS = "SELECT id, region FROM gold.customers ORDER BY id"


@pytest.mark.slow
def test_the_row_filter_how_to_does_what_the_page_says(
    client: TestClient,
    cli_state: CliState,
    invoke_cli: Callable[..., Result],
    worker: None,
    tmp_path: Path,
) -> None:
    """Table first, then filters; what each reader sees; each refusal as the page prints it."""
    run = invoke_cli

    def ok(state: CliState, *args: str, input: str | None = None) -> str:
        """Run a command that must succeed and return what it printed.

        Parameters
        ----------
        state
            The profile to run as.
        *args
            The command and its arguments.
        input
            Text for standard input.

        Returns
        -------
        str
            Standard output.
        """
        result = run(state, *args, input=input)
        assert result.exit_code == 0, result.stderr
        return result.stdout

    def refused(state: CliState, exit_code: int, *args: str) -> str:
        """Run a command that must fail with `exit_code` and return its message on one line.

        Parameters
        ----------
        state
            The profile to run as.
        exit_code
            The exit code the command must end with.
        *args
            The command and its arguments.

        Returns
        -------
        str
            Standard error, with its line breaks collapsed.
        """
        result = run(state, *args)
        assert result.exit_code == exit_code, result.stderr
        return " ".join(result.stderr.split())

    def reader(name: str) -> CliState:
        """Issue a token for a principal and return a profile that uses it.

        Parameters
        ----------
        name
            The principal's name.

        Returns
        -------
        CliState
            The profile of that principal.
        """
        issued = json.loads(ok(cli_state, "--output", "json", "token", "issue", name, "cli"))
        return make_cli_state(client, tmp_path, token=issued["token"])

    ok(
        cli_state,
        *("login", "--url", "http://testserver", "--token-stdin", "--env", "dev"),
        input=ADMIN_TOKEN + "\n",
    )
    ok(cli_state, "env", "create", "dev", "--storage-root", f"file://{tmp_path}/lake")
    for person in ("alice", "bob"):
        ok(cli_state, "principal", "create", person, "--type", "service-account")
    ok(cli_state, "group", "create", "sales_us")

    # The page's order: the table with its columns, then the filters, then the data, then access.
    ok(
        cli_state,
        *("table", "create", "gold.customers"),
        *("--column", "id:long", "--column", "region:string", "--column", "tier:string"),
    )
    filters = ["row-filter", "create", "gold.customers"]
    created = ok(
        cli_state, *filters, "--principal", "alice", "--expression", "region = 'EU'", "--env", "dev"
    )
    assert created.endswith(" on dev/gold.customers\n")
    ok(
        cli_state,
        *filters,
        *("--group", "sales_us", "--expression", "region = 'US' AND NOT (tier = 'vip')"),
        *("--env", "dev"),
    )
    csv = tmp_path / "customers.csv"
    csv.write_text("id,region,tier\n1,EU,std\n2,US,vip\n3,EU,vip\n")
    ok(cli_state, "table", "append", "gold.customers", str(csv))
    for person in ("alice", "bob"):
        ok(
            cli_state,
            *("grant", "create", "--principal", person, "--role", "analyst"),
            "--kind",
            "environment",
            "--env",
            "dev",
        )
    alice, bob = reader("alice"), reader("bob")

    seen = ok(alice, "--output", "json", "sql", _ALL_ROWS, "--env", "dev")
    assert [row["id"] for row in json.loads(seen)] == [1, 3]
    everything = ok(bob, "--output", "json", "sql", _ALL_ROWS, "--env", "dev")
    assert [row["id"] for row in json.loads(everything)] == [1, 2, 3]

    listed = json.loads(
        ok(cli_state, "--output", "json", "row-filter", "list", "--principal", "alice")
    )
    assert [(f["table"], f["expression"]) for f in listed] == [("customers", "region = 'EU'")]

    where = ("--principal", "alice", "--env", "dev")
    assert refused(cli_state, 8, *filters, "--expression", "regoin = 'EU'", *where) == (
        "Error: Table 'gold.customers' in environment 'dev' has no column 'regoin', which the "
        "row filter reads. Its columns are: id, region, tier. Check the spelling."
    )
    assert refused(cli_state, 8, *filters, "--expression", "customers.region = 'EU'", *where) == (
        "Error: 'customers.region = 'EU'' is not a valid filter expression: the column "
        "'customers.region' is written with a qualifier. Write the bare column name, 'region'; "
        "a row filter always reads the table it is set on."
    )
    assert refused(cli_state, 8, *filters, "--expression", "sha256(id) = 'x'", *where).startswith(
        "Error: 'sha256(id) = 'x'' is not a valid filter expression: "
    )
    assert refused(cli_state, 8, *filters, "--expression", "region = 5", *where) == (
        "Error: 'region = 5' is not a valid filter expression: "
        "`region` is text; compare it with '5', not 5."
    )
    assert refused(cli_state, 8, *filters, "--expression", "region", *where) == (
        "Error: 'region' is not a valid filter expression: `region` is text, not boolean; "
        "a condition is needed here. Compare it with text such as 'x', "
        "for example `region = ...`."
    )
    assert refused(cli_state, 2, *filters, "--expression", "id > 1", "--env", "dev") == (
        "Error: Pass exactly one of --principal or --group"
    )
    assert refused(
        cli_state, 6, *filters, "--expression", "id > 1", "--principal", "nobody", "--env", "dev"
    ) == ("Error: No principal named 'nobody'.")
    assert refused(bob, 5, *filters, "--expression", "id > 1", *where) == (
        "Error: 'bob' may not MANAGE_POLICY this."
    )
    # A name in another letter case is the same column, as the page says.
    ok(cli_state, *filters, "--expression", "REGION = 'EU'", *where)

    ok(cli_state, "row-filter", "revoke", listed[0]["id"], "--yes")
    left = json.loads(
        ok(cli_state, "--output", "json", "row-filter", "list", "--principal", "alice")
    )
    assert [f["expression"] for f in left] == ["REGION = 'EU'"]
