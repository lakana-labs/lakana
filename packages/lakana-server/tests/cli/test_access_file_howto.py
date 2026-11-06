"""The commands and messages of the access-file how-to page, run against the real app.

`docs/examples/access-file/run.sh` replays the whole page in the docs-examples job; these tests
check the page's claims against the in-process app, in small independent flows. The lake the page
assumes is built through the SDK, which costs a fraction of a CLI call.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import Result

import lakana
from lakana.cli._state import CliState
from tests.cli.conftest import make_cli_state

_GRANT = ("grant", "create", "--principal", "alice", "--role", "viewer")
_GRANT += ("--kind", "ENVIRONMENT", "--env", "dev")


def _admin_state(client: TestClient, admin: lakana.Client, tmp_path: Path) -> CliState:
    """Build the lake the page assumes and return the state of the account that applies the file.

    Parameters
    ----------
    client
        The running app's `TestClient`.
    admin
        An SDK client with the bootstrap administrator's credential.
    tmp_path
        Where the storage root and the profile file live.

    Returns
    -------
    CliState
        A state whose token belongs to the instance administrator `access-admin`.
    """
    admin.environments.create(
        "dev", storage={"scheme": "file", "root": f"file://{tmp_path}/lake", "options": {}}
    )
    columns = [{"name": "id", "type": "long"}, {"name": "email", "type": "string"}]
    admin.tables.create("dev", "gold.customers", schema=columns)
    admin.principals.create("alice", "USER")
    admin.groups.create("finance")
    admin.principals.create("access-admin", "SERVICE_ACCOUNT")
    admin.grants.grant(principal="access-admin", role="ADMIN", kind="INSTANCE")
    issued = admin.tokens.issue("access-admin", "access-ci")
    return make_cli_state(client, tmp_path, token=issued.token)


def _edited(exported: str) -> str:
    """Apply the page's edit to an exported file.

    Parameters
    ----------
    exported
        The file `lakana access export` wrote.

    Returns
    -------
    str
        The file with a member, a grant and a column mask added.
    """
    return (
        exported.replace("members: []", "members: [alice]")
        + "- to: group:finance\n  role: ANALYST\n  on: dev\n"
        + "policies:\n- kind: column_mask\n  table: dev.gold.customers\n  column: email\n"
        + "  expression: \"'***'\"\n  to: group:finance\n"
    )


def _applied(state: CliState, tmp_path: Path) -> Path:
    """Export, edit and apply the page's file through the SDK, and return the file.

    Parameters
    ----------
    state
        The state of the account that applies the file.
    tmp_path
        Where the file is written.

    Returns
    -------
    Path
        The applied access file.
    """
    access = state.client().access
    text = _edited(access.export())
    plan = access.plan(text)
    access.apply(text, plan_fingerprint=plan.fingerprint, source_commit="0123abc")
    path = tmp_path / "access.yaml"
    path.write_text(text)
    return path


def _stdout(result: Result) -> str:
    """Return what a command that must have succeeded printed.

    Parameters
    ----------
    result
        The command's result.

    Returns
    -------
    str
        Standard output.
    """
    assert result.exit_code == 0, result.stderr
    return result.stdout


def test_export_edit_and_plan_show_what_the_page_shows(
    client: TestClient,
    cli_state: CliState,
    invoke_cli: Callable[..., Result],
    tmp_path: Path,
) -> None:
    """The exported file manages the instance; the page's edit plans three creations."""
    state = _admin_state(client, cli_state.client(), tmp_path)
    access = tmp_path / "access.yaml"
    _stdout(invoke_cli(state, "access", "export", "-o", str(access)))
    exported = access.read_text()
    assert "manages:\n- instance\n" in exported
    assert "service_accounts:\n- access-admin\n" in exported
    access.write_text(_edited(exported))

    planned = _stdout(invoke_cli(state, "access", "plan", str(access))).splitlines()
    assert planned[:3] == [
        "+ member alice of group finance  (groups.finance.members[0])",
        "+ grant role ANALYST on environment dev to group:finance  (grants[2])",
        "+ column mask on dev.gold.customers.email for group:finance: '***'  (policies[0])",
    ]
    assert planned[3] == "starts managing instance"
    assert re.fullmatch(r"Fingerprint: [0-9a-f]{64}", planned[4])


def test_apply_leaves_nothing_to_apply(
    client: TestClient,
    cli_state: CliState,
    invoke_cli: Callable[..., Result],
    tmp_path: Path,
) -> None:
    """The workflow's apply creates three things; a second plan has no changes."""
    state = _admin_state(client, cli_state.client(), tmp_path)
    access = tmp_path / "access.yaml"
    access.write_text(_edited(state.client().access.export()))

    applied = _stdout(
        invoke_cli(state, "access", "apply", str(access), "--yes", "--source-commit", "0123abc")
    )
    assert applied.splitlines()[-1] == "Applied: 3 created, 0 changed, 0 removed."
    assert _stdout(invoke_cli(state, "access", "plan", str(access))).splitlines()[0] == (
        "No changes."
    )


def test_status_names_the_file_the_commit_and_the_account(
    client: TestClient,
    cli_state: CliState,
    invoke_cli: Callable[..., Result],
    tmp_path: Path,
) -> None:
    """After an apply, status shows the file, the commit and the account, and no override."""
    state = _admin_state(client, cli_state.client(), tmp_path)
    _applied(state, tmp_path)

    status = _stdout(invoke_cli(state, "access", "status"))
    for text in ("instance", "access.yaml", "0123abc", "access-admin"):
        assert text in status
    assert "Open overrides" not in status


def test_a_managed_scope_refuses_a_change(
    client: TestClient,
    cli_state: CliState,
    invoke_cli: Callable[..., Result],
    tmp_path: Path,
) -> None:
    """The refusal names the file and the way out."""
    state = _admin_state(client, cli_state.client(), tmp_path)
    _applied(state, tmp_path)

    refused = invoke_cli(state, *_GRANT)
    assert refused.exit_code == 7, refused.stderr
    assert re.fullmatch(
        r"Error: `instance` is managed by an access file \(last applied "
        r"\d{4}-\d\d-\d\dT\d\d:\d\dZ\); change the file and run `lakana access apply`, or, in "
        r"an emergency, repeat with `--override-managed --reason …`",
        " ".join(refused.stderr.split()),
    )


def test_an_override_shows_up_as_drift_and_in_status(
    client: TestClient,
    cli_state: CliState,
    invoke_cli: Callable[..., Result],
    tmp_path: Path,
) -> None:
    """An overridden change is allowed; the plan reports the drift and status the reason."""
    state = _admin_state(client, cli_state.client(), tmp_path)
    access = _applied(state, tmp_path)

    state.client().grants.grant(
        principal="alice",
        role="VIEWER",
        kind="ENVIRONMENT",
        environment="dev",
        override_reason="Incident 42 hotfix",
    )
    lines = _stdout(invoke_cli(state, "access", "plan", str(access))).splitlines()
    assert lines[0] == "- grant role VIEWER on environment dev to user:alice"
    assert lines[1].startswith(
        "! drift in instance: grant VIEWER to alice (override) — access-admin, "
    )
    assert lines[1].endswith(": Incident 42 hotfix")
    assert "Incident 42 hotfix" in _stdout(invoke_cli(state, "access", "status"))


def test_a_file_that_removes_the_applying_account_is_a_problem(
    client: TestClient,
    cli_state: CliState,
    invoke_cli: Callable[..., Result],
    tmp_path: Path,
) -> None:
    """Planning a file without `access-admin` fails and says who else must apply it."""
    state = _admin_state(client, cli_state.client(), tmp_path)
    access = _applied(state, tmp_path)
    without = tmp_path / "without.yaml"
    without.write_text(access.read_text().replace("service_accounts:\n- access-admin\n", ""))

    result = invoke_cli(state, "access", "plan", str(without))
    assert result.exit_code == 1, result.stderr
    message = " ".join(result.stderr.split())
    assert "the file removes service:access-admin, the account applying it" in message
    assert "have another administrator apply this file" in message
