"""The commands and messages of the column-mask how-to page, run against the real app."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import Result

from lakana.cli._state import CliState
from tests.cli.conftest import ADMIN_TOKEN, make_cli_state

_ALL_ROWS = "SELECT id, name, ssn FROM gold.customers ORDER BY id"


@pytest.mark.slow
def test_the_column_mask_how_to_does_what_the_page_says(
    client: TestClient,
    cli_state: CliState,
    invoke_cli: Callable[..., Result],
    worker: None,
    tmp_path: Path,
) -> None:
    """Add, list and remove a mask; what each reader sees; each refusal as the page prints it."""
    run = invoke_cli

    def ok(state: CliState, *args: str, input: str | None = None) -> str:
        result = run(state, *args, input=input)
        assert result.exit_code == 0, result.stderr
        return result.stdout

    def refused(state: CliState, exit_code: int, *args: str) -> str:
        result = run(state, *args)
        assert result.exit_code == exit_code, result.stderr
        return result.stderr.strip()

    def reader(name: str) -> CliState:
        issued = json.loads(ok(cli_state, "--output", "json", "token", "issue", name, "cli"))
        return make_cli_state(client, tmp_path, token=issued["token"])

    ok(
        cli_state,
        *("login", "--url", "http://testserver", "--token-stdin", "--env", "dev"),
        input=ADMIN_TOKEN + "\n",
    )
    ok(cli_state, "env", "create", "dev", "--storage-root", f"file://{tmp_path}/lake")
    ok(
        cli_state,
        *("table", "create", "gold.customers"),
        *("--column", "id:long", "--column", "name:string", "--column", "ssn:string"),
    )
    csv = tmp_path / "customers.csv"
    csv.write_text("id,name,ssn\n1,Ada,111-11-1111\n2,Grace,222-22-2222\n")
    ok(cli_state, "table", "append", "gold.customers", str(csv))
    for person in ("alice", "bob"):
        ok(cli_state, "principal", "create", person, "--type", "service-account")
        ok(
            cli_state,
            *("grant", "create", "--principal", person, "--role", "analyst"),
            "--kind",
            "environment",
            "--env",
            "dev",
        )
    alice, bob = reader("alice"), reader("bob")

    mask = ["column-mask", "create", "gold.customers"]
    created = ok(
        cli_state, *mask, "ssn", "--principal", "alice", "--expression", "'***'", "--env", "dev"
    )
    assert created.endswith(" on dev/gold.customers.ssn\n")

    masked = ok(alice, "--output", "json", "sql", _ALL_ROWS, "--env", "dev")
    assert [row["ssn"] for row in json.loads(masked)] == ["***", "***"]
    unmasked = ok(bob, "--output", "json", "sql", _ALL_ROWS, "--env", "dev")
    assert [row["ssn"] for row in json.loads(unmasked)] == ["111-11-1111", "222-22-2222"]
    probe = ok(
        alice,
        "--output",
        "json",
        "sql",
        "SELECT id FROM gold.customers WHERE ssn = '111-11-1111'",
        "--env",
        "dev",
    )
    assert json.loads(probe) == []

    listed = json.loads(
        ok(cli_state, "--output", "json", "column-mask", "list", "--principal", "alice")
    )
    assert [(m["table"], m["column"], m["expression"]) for m in listed] == [
        ("customers", "ssn", "'***'")
    ]

    where = ("--principal", "alice", "--expression", "NULL", "--env", "dev")
    assert refused(cli_state, 8, *mask, "sn", *where) == (
        "Error: Table 'gold.customers' in environment 'dev' has no column 'sn'. "
        "Its columns are: id, name, ssn. Check the spelling; column names are case-sensitive."
    )
    assert refused(cli_state, 8, *mask, "SSN", *where).endswith(" Did you mean 'ssn'?")
    assert refused(
        cli_state,
        8,
        *mask,
        "ssn",
        "--principal",
        "alice",
        "--expression",
        "sha256(ssn)",
        "--env",
        "dev",
    ) == (
        "Error: 'sha256(ssn)' is not a valid mask expression: "
        "SHA2 is outside the restricted subset."
    )
    assert refused(
        cli_state, 8, *mask, "ssn", "--principal", "alice", "--expression", "0", "--env", "dev"
    ) == (
        "Error: '0' is not a valid mask expression: `ssn` is text, but the mask is a number. "
        "Use a value of its own type: '***', NULL, or a text function such as substr(ssn, 1, 2)."
    )
    assert refused(cli_state, 2, *mask, "ssn", "--expression", "NULL", "--env", "dev") == (
        "Error: Pass exactly one of --principal or --group"
    )
    assert refused(
        cli_state, 6, *mask, "ssn", "--principal", "nobody", "--expression", "NULL", "--env", "dev"
    ) == ("Error: No principal named 'nobody'.")
    assert refused(bob, 5, *mask, "ssn", *where) == "Error: 'bob' may not MANAGE_POLICY this."
    ok(
        cli_state,
        "sql",
        "SELECT id, name FROM gold.customers",
        "--into",
        "gold.names",
        "--mode",
        "view",
        "--env",
        "dev",
    )
    assert refused(cli_state, 8, "column-mask", "create", "gold.names", "name", *where) == (
        "Error: 'gold.names' is a view; row filters and column masks apply to tables. "
        "Attach them to the view's base tables."
    )

    ok(cli_state, "column-mask", "revoke", listed[0]["id"], "--yes")
    assert ok(cli_state, "column-mask", "list", "--principal", "alice").strip() == "(none)"
    again = ok(alice, "--output", "json", "sql", _ALL_ROWS, "--env", "dev")
    assert [row["ssn"] for row in json.loads(again)] == ["111-11-1111", "222-22-2222"]
