"""The commands and messages of the access-file how-to page, run against the real app."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import Result

from lakana.cli._state import CliState
from tests.cli.conftest import make_cli_state


def test_the_access_file_how_to_does_what_the_page_says(
    client: TestClient,
    cli_state: CliState,
    invoke_cli: Callable[..., Result],
    tmp_path: Path,
) -> None:
    """Export, edit, plan, apply, status, the refusal in the managed scope, the override, drift."""
    run = invoke_cli
    profile = {"state": cli_state}

    def ok(*args: str) -> str:
        """Run a command that must succeed and return what it printed.

        Parameters
        ----------
        *args
            The command and its arguments.

        Returns
        -------
        str
            Standard output.
        """
        result = run(profile["state"], *args)
        assert result.exit_code == 0, result.stderr
        return result.stdout

    def refused(exit_code: int, *args: str) -> str:
        """Run a command that must fail with `exit_code` and return its message on one line.

        Parameters
        ----------
        exit_code
            The exit code the command must end with.
        *args
            The command and its arguments.

        Returns
        -------
        str
            Standard error, with its line breaks collapsed.
        """
        result = run(profile["state"], *args)
        assert result.exit_code == exit_code, result.stderr
        return " ".join(result.stderr.split())

    # The lake the page assumes already exists.
    ok("env", "create", "dev", "--storage-root", f"file://{tmp_path}/lake")
    ok(
        *("table", "create", "gold.customers", "--env", "dev"),
        *("--column", "id:long", "--column", "email:string"),
    )
    ok("principal", "create", "alice", "--type", "user")
    ok("group", "create", "finance")

    # Step 1: the account that applies the file, and the profile that holds its token.
    ok("principal", "create", "access-admin", "--type", "service-account")
    ok("grant", "create", "--principal", "access-admin", "--role", "ADMIN", "--kind", "INSTANCE")
    issued = json.loads(ok("--output", "json", "token", "issue", "access-admin", "access-ci"))
    profile["state"] = make_cli_state(client, tmp_path, token=issued["token"])

    # Step 2: export, then the page's edit.
    access = tmp_path / "access.yaml"
    ok("access", "export", "-o", str(access))
    exported = access.read_text()
    assert "service_accounts:\n- access-admin\n" in exported
    access.write_text(
        exported.replace("members: []", "members: [alice]")
        + "- to: group:finance\n  role: ANALYST\n  on: dev\n"
        + "policies:\n- kind: column_mask\n  table: dev.gold.customers\n  column: email\n"
        + "  expression: \"'***'\"\n  to: group:finance\n"
    )

    # Step 3: the plan the page shows.
    planned = ok("access", "plan", str(access))
    assert planned.splitlines()[:3] == [
        "+ member alice of group finance  (groups.finance.members[0])",
        "+ grant role ANALYST on environment dev to group:finance  (grants[2])",
        "+ column mask on dev.gold.customers.email for group:finance: '***'  (policies[0])",
    ]
    assert re.fullmatch(r"Fingerprint: [0-9a-f]{64}", planned.splitlines()[3])

    # Step 4: the workflow's apply, then nothing is left to apply.
    applied = ok("access", "apply", str(access), "--yes", "--source-commit", "0123abc")
    assert applied.splitlines()[-1] == "Applied: 3 created, 0 changed, 0 removed."
    assert ok("access", "plan", str(access)).splitlines()[0] == "No changes."

    # Step 5: status.
    status = ok("access", "status")
    for text in ("instance", "access.yaml", "0123abc", "access-admin"):
        assert text in status
    assert "Open overrides" not in status

    # Step 6: the refusal, the override, the drift line.
    grant = ("grant", "create", "--principal", "alice", "--role", "viewer")
    grant += ("--kind", "ENVIRONMENT", "--env", "dev")
    message = refused(7, *grant)
    assert re.fullmatch(
        r"Error: `instance` is managed by an access file \(last applied "
        r"\d{4}-\d\d-\d\dT\d\d:\d\dZ\); change the file and run `lakana access apply`, or, in "
        r"an emergency, repeat with `--override-managed --reason …`",
        message,
    )
    ok(*grant, "--override-managed", "--reason", "Incident 42 hotfix")
    drifted = ok("access", "plan", str(access))
    lines = drifted.splitlines()
    assert lines[0] == "- grant role VIEWER on environment dev to user:alice"
    assert lines[1].startswith(
        "! drift in instance: grant VIEWER to alice (override) — access-admin, "
    )
    assert lines[1].endswith(": Incident 42 hotfix")
    assert "Incident 42 hotfix" in ok("access", "status")

    # The administrator's own profile cannot be locked out of the lake by the file.
    assert "access-admin" in access.read_text()
