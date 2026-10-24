"""The CLI driven end to end against the real app."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from typer.testing import Result

from lakana.cli._state import CliState
from tests.cli.conftest import ADMIN_TOKEN, make_cli_state


def _ok(result: Result) -> Result:
    """Assert a command succeeded, showing stderr if not.

    Parameters
    ----------
    result
        The command's result.

    Returns
    -------
    Result
        The same result.
    """
    assert result.exit_code == 0, result.stderr
    return result


@pytest.mark.slow
def test_login_create_append_and_query(
    cli_state: CliState, invoke_cli: Callable[..., Result], worker: None, tmp_path: Path
) -> None:
    """Login, create env/table, append CSV, then sql: result equal to the input."""
    run = invoke_cli
    _ok(
        run(
            cli_state,
            "login",
            "--url",
            "http://testserver",
            "--token-stdin",
            "--env",
            "dev",
            input=ADMIN_TOKEN + "\n",
        )
    )
    profile = tomllib.loads(cli_state.profile_path().read_text())["profiles"]["default"]
    assert profile["default_environment"] == "dev"

    assert json.loads(_ok(run(cli_state, "--output", "json", "whoami")).stdout)["name"] == "admin"
    _ok(run(cli_state, "env", "create", "dev", "--storage-root", f"file://{tmp_path}/lake"))
    _ok(
        run(
            cli_state,
            "table",
            "create",
            "bronze.sales",
            "--column",
            "id:long",
            "--column",
            "region:string?",
        )
    )

    csv = tmp_path / "sales.csv"
    csv.write_text("id,region\n1,eu\n2,us\n3,\n")
    assert (
        "Appended 3 rows to dev/bronze.sales"
        in _ok(run(cli_state, "table", "append", "bronze.sales", str(csv))).stdout
    )

    table_mode = _ok(run(cli_state, "sql", "SELECT id, region FROM bronze.sales ORDER BY id"))
    assert "3 rows" in table_mode.stdout
    rows = json.loads(
        _ok(
            run(
                cli_state,
                "--output",
                "json",
                "sql",
                "SELECT id, region FROM bronze.sales ORDER BY id",
            )
        ).stdout
    )
    assert rows == [{"id": 1, "region": "eu"}, {"id": 2, "region": "us"}, {"id": 3, "region": None}]

    out = tmp_path / "result.parquet"
    _ok(
        run(
            cli_state,
            "sql",
            "SELECT id, region FROM bronze.sales ORDER BY id",
            "--out",
            str(out),
        )
    )
    actual = pq.read_table(out)
    # Checked before any comparison, so a type-mapping regression (e.g. the
    # server silently widening `region` to `large_string`) can't hide behind
    # a cast in the value comparison below.
    assert actual.schema.field("id").type == pa.int64()
    assert actual.schema.field("region").type == pa.string()
    expected = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "region": ["eu", "us", None]})
    assert actual.equals(expected)


@pytest.mark.slow
def test_every_command_group_against_the_real_app(
    cli_state: CliState,
    invoke_cli: Callable[..., Result],
    worker: None,
    client: TestClient,
    tmp_path: Path,
) -> None:
    """At least one call per remaining group succeeds end to end."""
    run = invoke_cli
    _ok(run(cli_state, "env", "create", "dev", "--storage-root", f"file://{tmp_path}/lake"))
    _ok(run(cli_state, "env", "protect", "dev"))
    _ok(run(cli_state, "env", "unprotect", "dev", "--yes"))
    _ok(run(cli_state, "ns", "create", "bronze.eu", "--env", "dev"))
    assert "bronze.eu" in _ok(run(cli_state, "ns", "list", "--env", "dev")).stdout
    _ok(
        run(
            cli_state,
            "table",
            "create",
            "bronze.sales",
            "--env",
            "dev",
            "--column",
            "id:long",
            "--column",
            "region:string?",
        )
    )
    table_out = _ok(
        run(cli_state, "--output", "json", "table", "get", "bronze.sales", "--env", "dev")
    ).stdout
    assert json.loads(table_out)["columns"][0]["name"] == "id"

    submit_out = _ok(
        run(cli_state, "--output", "json", "query", "submit", "SELECT 42 AS answer", "--env", "dev")
    ).stdout
    submitted = json.loads(submit_out)
    _ok(run(cli_state, "query", "wait", submitted["id"], "--env", "dev", "--timeout", "30"))
    result_out = _ok(
        run(cli_state, "--output", "json", "query", "result", submitted["id"], "--env", "dev")
    ).stdout
    assert json.loads(result_out) == [{"answer": 42}]

    _ok(run(cli_state, "principal", "create", "bob", "--type", "service-account"))
    _ok(run(cli_state, "group", "create", "eng"))
    _ok(run(cli_state, "group", "add-member", "eng", "bob"))
    assert "bob" in _ok(run(cli_state, "group", "members", "eng")).stdout
    issued_out = _ok(run(cli_state, "--output", "json", "token", "issue", "bob", "cli")).stdout
    issued = json.loads(issued_out)
    grant = json.loads(
        _ok(
            run(
                cli_state,
                "--output",
                "json",
                "grant",
                "create",
                "--group",
                "eng",
                "--role",
                "analyst",
                "--kind",
                "environment",
                "--env",
                "dev",
            )
        ).stdout
    )
    row_filter = json.loads(
        _ok(
            run(
                cli_state,
                "--output",
                "json",
                "row-filter",
                "create",
                "bronze.sales",
                "--principal",
                "bob",
                "--expression",
                "id > 1",
                "--env",
                "dev",
            )
        ).stdout
    )
    mask = json.loads(
        _ok(
            run(
                cli_state,
                "--output",
                "json",
                "column-mask",
                "create",
                "bronze.sales",
                "region",
                "--principal",
                "bob",
                "--expression",
                "'X'",
                "--env",
                "dev",
            )
        ).stdout
    )
    _ok(
        run(
            cli_state,
            "secret",
            "create",
            "creds",
            "--env",
            "dev",
            "--values-stdin",
            input='{"k": "v"}',
        )
    )
    _ok(run(cli_state, "secret", "rotate", "creds", "--env", "dev", "--value", "k=w"))
    minted = json.loads(
        _ok(
            run(
                cli_state,
                "--output",
                "json",
                "task-token",
                "mint",
                "bob",
                "--scope",
                "bronze.*",
                "--ttl",
                "300",
                "--env",
                "dev",
            )
        ).stdout
    )
    audit_out = _ok(
        run(cli_state, "--output", "json", "audit", "list", "--event-type", "GRANT_CREATED")
    ).stdout
    assert json.loads(audit_out)["items"]

    bob = make_cli_state(client, tmp_path, token=issued["token"])
    denied = run(bob, "principal", "list")
    assert denied.exit_code == 5, denied.stderr
    missing = run(cli_state, "table", "get", "bronze.nosuch", "--env", "dev")
    assert missing.exit_code == 6, missing.stderr

    _ok(run(cli_state, "task-token", "revoke", minted["key_id"], "--yes"))
    _ok(run(cli_state, "column-mask", "revoke", mask["id"], "--yes"))
    _ok(run(cli_state, "row-filter", "revoke", row_filter["id"], "--yes"))
    _ok(run(cli_state, "grant", "revoke", grant["id"], "--yes"))
    _ok(run(cli_state, "secret", "revoke", "creds", "--env", "dev", "--yes"))
    _ok(run(cli_state, "token", "revoke", "bob", issued["key_id"], "--yes"))
    _ok(run(cli_state, "group", "remove-member", "eng", "bob", "--yes"))
    _ok(run(cli_state, "group", "delete", "eng", "--yes"))
    _ok(run(cli_state, "principal", "deactivate", "bob", "--yes"))
    _ok(run(cli_state, "table", "delete", "bronze.sales", "--env", "dev", "--yes"))
    _ok(run(cli_state, "ns", "delete", "bronze.eu", "--env", "dev", "--yes"))
    _ok(run(cli_state, "env", "delete", "dev", "--yes"))


@pytest.mark.slow
def test_sql_into_and_views_through_the_cli(
    cli_state: CliState, invoke_cli: Callable[..., Result], worker: None, tmp_path: Path
) -> None:
    """`sql --into` writes, `--mode view` creates a view, `table get` shows it."""
    run = invoke_cli
    _ok(
        run(
            cli_state,
            "login",
            "--url",
            "http://testserver",
            "--token-stdin",
            "--env",
            "dev",
            input=ADMIN_TOKEN + "\n",
        )
    )
    _ok(run(cli_state, "env", "create", "dev", "--storage-root", f"file://{tmp_path}/lake"))
    _ok(run(cli_state, "table", "create", "bronze.sales", "--column", "id:long"))
    csv = tmp_path / "sales.csv"
    csv.write_text("id\n1\n2\n")
    _ok(run(cli_state, "table", "append", "bronze.sales", str(csv)))

    written = _ok(
        run(
            cli_state,
            "sql",
            "SELECT id FROM bronze.sales",
            "--into",
            "gold.ids",
            "--mode",
            "create",
        )
    )
    assert "Wrote 2 rows to dev/gold.ids" in written.stdout
    _ok(run(cli_state, "sql", "SELECT id FROM gold.ids", "--into", "gold.v", "--mode", "view"))

    view = json.loads(_ok(run(cli_state, "--output", "json", "table", "get", "gold.v")).stdout)
    assert view["kind"] == "VIEW"
    assert [reference["name"] for reference in view["references"]] == ["gold.ids"]
    rows = json.loads(
        _ok(run(cli_state, "--output", "json", "sql", "SELECT id FROM gold.v ORDER BY id")).stdout
    )
    assert rows == [{"id": 1}, {"id": 2}]
