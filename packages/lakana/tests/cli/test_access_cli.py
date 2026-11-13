"""Tests for the commands that give a person access: they name what they acted on."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from conftest import FakeApi, invoke, make_state

_ALICE_ID = "95331776-1545-4c4d-8129-bdf9614a2bd2"
_BOT_ID = "0b6d9d52-6a0c-4a63-9f3c-5d6f1f0b7a11"
_GROUP_ID = "7d2f4a0e-3c1b-4e55-8d0a-2b9f6c1e4a77"
_ANALYST_GRANT = "c913f5e7-8bf0-4b21-9630-46de66a0082d"
_ENGINEER_GRANT = "d2a1b3c4-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
_GROUP_GRANT = "e3b2c4d5-6f7a-4b8c-9d0e-1f2a3b4c5d6e"

_LONG_ID = "11111111-2222-4333-8444-555555555555"
_LONG_OWNER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_PEOPLE = {
    _ALICE_ID: {"id": _ALICE_ID, "name": "alice", "principal_type": "user"},
    _BOT_ID: {"id": _BOT_ID, "name": "ci-bot", "principal_type": "service"},
    _LONG_OWNER: {
        "id": _LONG_OWNER,
        "name": "ci-pipeline-production",
        "principal_type": "service",
    },
}
_TEAMS = {_GROUP_ID: "analysts"}


def _grant(grant_id: str, **more: str | None) -> dict[str, object]:
    """Build a grant as the server answers it: ids and, for known ids, the names.

    Parameters
    ----------
    grant_id
        The grant's id.
    **more
        Fields that replace the defaults.

    Returns
    -------
    dict
        The `GrantOut` body.
    """
    body = _bare_grant(grant_id, **more)
    body["principal"] = _PEOPLE.get(str(body["principal_id"]))
    body["group"] = _TEAMS.get(str(body["group_id"]))
    return body


def _without_names(grant: dict[str, object]) -> dict[str, object]:
    """Take the names off a grant, as a server before this release answers.

    Parameters
    ----------
    grant
        A grant body.

    Returns
    -------
    dict
        The same without `principal` and `group`.
    """
    return {k: v for k, v in grant.items() if k not in ("principal", "group")}


def _bare_grant(grant_id: str, **more: str | None) -> dict[str, object]:
    """Build a grant body without names.

    Parameters
    ----------
    grant_id
        The grant's id.
    **more
        Fields that replace the defaults.

    Returns
    -------
    dict
        The ids-only body.
    """
    return {
        "id": grant_id,
        "principal_id": _ALICE_ID,
        "group_id": None,
        "role": "ANALYST",
        "action": None,
        "kind": "ENVIRONMENT",
        "environment": "dev",
        "namespace": None,
        "table": None,
        "granted_by": "admin",
        "granted_at": "2026-10-17T13:46:53+00:00",
        "note": None,
        **more,
    }


_GRANTS = [
    _grant(_ANALYST_GRANT),
    _grant(_ENGINEER_GRANT, principal_id=_BOT_ID, role="DATA_ENGINEER"),
    _grant(_GROUP_GRANT, principal_id=None, group_id=_GROUP_ID, role="VIEWER"),
]


def _api() -> FakeApi:
    """Build a fake instance that answers the grant listings by their filter.

    Returns
    -------
    FakeApi
        An API with principals, one group and three grants on the environment `dev`.
    """
    api = FakeApi()

    def grants(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "principal_name" in params:
            owner = {"alice": _ALICE_ID, "ci-bot": _BOT_ID}.get(params["principal_name"])
            return httpx.Response(200, json=[g for g in _GRANTS if g["principal_id"] == owner])
        if "group_name" in params:
            same = params["group_name"] == "analysts"
            return httpx.Response(200, json=[g for g in _GRANTS if same and g["group_id"]])
        return httpx.Response(200, json=_GRANTS)

    api.routes["GET", "/api/v1/grants"] = grants
    return api


def test_principal_create_says_what_comes_next(tmp_path: Path) -> None:
    """A new principal can do nothing: the output names the grant and the token to add."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/principals",
        status=201,
        json={"name": "alice", "type": "USER", "is_active": True},
    )

    result = invoke(make_state(tmp_path, api), "principal", "create", "alice", "--type", "user")

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines()[0] == "Created user alice"
    assert "lakana grant create --principal alice --role ANALYST" in result.stdout
    assert "lakana token issue alice NAME" in result.stdout


def test_principal_create_json_has_no_next_step(tmp_path: Path) -> None:
    """`--output json` stays one document."""
    api = FakeApi()
    created = {"name": "alice", "type": "USER", "is_active": True}
    api.on("POST", "/api/v1/principals", status=201, json=created)

    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "principal",
        "create",
        "alice",
        "--type",
        "user",
    )

    assert json.loads(result.stdout) == created


def test_token_issue_says_who_gets_the_token_and_what_they_type(tmp_path: Path) -> None:
    """The notice names the principal and `lakana login`, with neutral wording."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/principals/alice/tokens",
        status=201,
        json={"key_id": "k9", "name": "laptop", "token": "lakana_pat_k9_newsecret"},
    )

    result = invoke(make_state(tmp_path, api), "token", "issue", "alice", "laptop")

    assert result.exit_code == 0, result.stderr
    assert "Give this token to alice over a trusted channel; they run `lakana login`" in (
        result.stderr
    )
    assert "lakana login" not in result.stdout
    assert " she " not in result.stderr and " he " not in result.stderr


def test_token_issue_json_stderr_has_no_next_step(tmp_path: Path) -> None:
    """In JSON mode only the secret notice goes to stderr."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/principals/alice/tokens",
        status=201,
        json={"key_id": "k9", "name": "laptop", "token": "lakana_pat_k9_newsecret"},
    )

    result = invoke(
        make_state(tmp_path, api), "--output", "json", "token", "issue", "alice", "laptop"
    )

    assert "lakana login" not in result.stderr


def test_grant_create_names_the_environment_and_the_recipient(tmp_path: Path) -> None:
    """`(ANALYST on environment dev)`, not `(ANALYST on ENVIRONMENT)`."""
    api = FakeApi()
    api.on("POST", "/api/v1/grants", status=201, json=_grant(_ANALYST_GRANT))

    result = invoke(
        make_state(tmp_path, api),
        "grant",
        "create",
        "--principal",
        "alice",
        "--role",
        "analyst",
        "--kind",
        "environment",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == (
        f"Created grant {_ANALYST_GRANT} (ANALYST on environment dev) to alice"
    )


def test_grant_create_names_a_table_and_a_group(tmp_path: Path) -> None:
    """A table grant names the table and its environment; a group is called a group."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/grants",
        status=201,
        json=_grant(
            "g9",
            role=None,
            action="READ_TABLE",
            kind="TABLE",
            namespace="gold.sales",
            table="orders",
        ),
    )

    result = invoke(
        make_state(tmp_path, api),
        "grant",
        "create",
        "--group",
        "analysts",
        "--action",
        "read_table",
        "--kind",
        "table",
        "--env",
        "dev",
        "--ns",
        "gold.sales",
        "--table",
        "orders",
    )

    assert result.stdout.strip() == (
        "Created grant g9 (READ_TABLE on table gold.sales.orders in environment dev) "
        "to group analysts"
    )


def test_grant_create_on_the_instance(tmp_path: Path) -> None:
    """An instance grant says so."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/grants",
        status=201,
        json=_grant("g8", role="ADMIN", kind="INSTANCE", environment=None),
    )

    result = invoke(
        make_state(tmp_path, api),
        "grant",
        "create",
        "--principal",
        "alice",
        "--role",
        "admin",
        "--kind",
        "instance",
    )

    assert result.stdout.strip() == "Created grant g8 (ADMIN on the instance) to alice"


def test_grant_create_on_a_namespace(tmp_path: Path) -> None:
    """A namespace grant names the namespace and the environment."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/grants",
        status=201,
        json=_grant("g9", role="VIEWER", kind="NAMESPACE", namespace="gold.sales"),
    )

    result = invoke(
        make_state(tmp_path, api),
        "grant",
        "create",
        "--principal",
        "alice",
        "--role",
        "viewer",
        "--kind",
        "namespace",
        "--env",
        "dev",
        "--ns",
        "gold.sales",
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == (
        "Created grant g9 (VIEWER on namespace gold.sales in environment dev) to alice"
    )


def test_grant_list_shows_names_without_listing_principals(tmp_path: Path) -> None:
    """The names come from the answer: one request, and no principal or group is asked about."""
    api = _api()

    result = invoke(make_state(tmp_path, api), "grant", "list")

    assert result.exit_code == 0, result.stderr
    assert "user alice" in result.stdout
    assert "service ci-bot" in result.stdout
    assert "group analysts" in result.stdout
    assert [(r.method, r.url.path) for r in api.requests] == [("GET", "/api/v1/grants")]


def test_grant_list_names_a_group_without_listing_groups(tmp_path: Path) -> None:
    """A group grant is named from the answer alone, even when no filter is given."""
    api = FakeApi()
    api.on("GET", "/api/v1/grants", json=[_GRANTS[2]])

    result = invoke(make_state(tmp_path, api), "grant", "list")

    assert result.exit_code == 0, result.stderr
    assert "group analysts" in result.stdout
    assert [r.url.path for r in api.requests] == ["/api/v1/grants"]


def test_grant_list_with_a_group_filter_names_the_group_without_lookups(tmp_path: Path) -> None:
    """The name is known when `--group` is given; no principal or group listing is read."""
    result = invoke(make_state(tmp_path, _api()), "grant", "list", "--group", "analysts")

    assert result.exit_code == 0, result.stderr
    assert "group analysts" in result.stdout
    assert _GROUP_ID not in result.stdout


def test_grant_list_names_who_each_grant_is_for(tmp_path: Path) -> None:
    """Users, service accounts and groups appear by name, with the kind of subject."""
    result = invoke(make_state(tmp_path, _api()), "grant", "list", "--env", "dev")

    assert result.exit_code == 0, result.stderr
    assert "user alice" in result.stdout
    assert "service ci-bot" in result.stdout
    assert "group analysts" in result.stdout
    assert _ALICE_ID not in result.stdout and _GROUP_ID not in result.stdout


def test_grant_list_with_a_filter_reads_the_type_from_the_answer(tmp_path: Path) -> None:
    """`--principal` costs one request: the answer already says the principal is a user."""
    api = _api()

    result = invoke(make_state(tmp_path, api), "grant", "list", "--principal", "alice")

    assert "user alice" in result.stdout
    assert [r.url.path for r in api.requests] == ["/api/v1/grants"]


def test_grant_list_falls_back_to_ids_when_names_cannot_be_read(tmp_path: Path) -> None:
    """A caller that may not list principals still sees the grants."""
    api = FakeApi()
    api.on("GET", "/api/v1/grants", json=[_without_names(_GRANTS[0])])

    result = invoke(make_state(tmp_path, api), "grant", "list")

    assert result.exit_code == 0, result.stderr
    assert f"principal {_ALICE_ID}" in result.stdout


def test_grant_list_json_keeps_its_fields(tmp_path: Path) -> None:
    """JSON carries the ids as before."""
    result = invoke(make_state(tmp_path, _api()), "--output", "json", "grant", "list")

    rows = json.loads(result.stdout)
    assert rows == _GRANTS


def test_an_80_column_listing_keeps_what_revoking_needs(tmp_path: Path) -> None:
    """The id, the name and the role are whole; the line under the table shows the command."""
    result = invoke(
        make_state(tmp_path, _api()), "grant", "list", "--env", "dev", env={"COLUMNS": "80"}
    )

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert max(len(line) for line in lines) <= 80, result.stdout
    row = next(line for line in lines if _ENGINEER_GRANT in line)
    assert "service ci-bot" in row and "DATA_ENGINEER" in row
    assert lines[-1] == "Revoke one with: lakana grant revoke ID"


def test_a_single_grant_gets_the_exact_revoke_command(tmp_path: Path) -> None:
    """With one row the command is complete and can be copied."""
    result = invoke(
        make_state(tmp_path, _api()),
        "grant",
        "list",
        "--principal",
        "alice",
        env={"COLUMNS": "80"},
    )

    assert result.stdout.splitlines()[-1] == f"Revoke it with: lakana grant revoke {_ANALYST_GRANT}"


def test_an_empty_column_is_left_out(tmp_path: Path) -> None:
    """`action`, `namespace` and `table` are empty here, so they take no room."""
    result = invoke(make_state(tmp_path, _api()), "grant", "list", "--env", "dev")

    header = result.stdout.splitlines()[1]
    assert "action" not in header and "namespace" not in header and "table" not in header
    assert "role" in header and "environment" in header


_LONG = _grant(
    _LONG_ID,
    principal_id=_LONG_OWNER,
    role="DATA_ENGINEER",
)


def _assert_ids_whole_with_hint(out: str, ids: list[str]) -> None:
    """Check that every full grant id and the revoke hint are in a listing.

    Parameters
    ----------
    out
        What `grant list` printed.
    ids
        The grant ids that must appear whole on one line.
    """
    lines = out.splitlines()
    for grant_id in ids:
        assert any(grant_id in line for line in lines), out
    assert any(line.startswith("Revoke ") and "lakana grant revoke" in line for line in lines), out


def test_long_names_never_push_the_id_out_at_80_columns(tmp_path: Path) -> None:
    """The subject folds onto more lines; every id stays whole."""
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/grants",
        json=[
            _LONG,
            _grant(_ANALYST_GRANT, principal_id=_ALICE_ID),
            _grant(_GROUP_GRANT, principal_id=None, group_id=_GROUP_ID, role="VIEWER"),
        ],
    )

    result = invoke(make_state(tmp_path, api), "grant", "list", env={"COLUMNS": "80"})

    assert result.exit_code == 0, result.stderr
    _assert_ids_whole_with_hint(result.stdout, [_LONG_ID, _ANALYST_GRANT, _GROUP_GRANT])
    assert "service" in result.stdout and "ci-pipeline" in result.stdout
    assert "Not shown: id" not in result.stdout


def test_unresolved_names_keep_the_id_and_say_why(tmp_path: Path) -> None:
    """The `principal <uuid>` fallback at 80 columns keeps ids whole and explains itself."""
    api = FakeApi()
    api.on("GET", "/api/v1/grants", json=[_without_names(g) for g in _GRANTS])

    result = invoke(make_state(tmp_path, api), "grant", "list", env={"COLUMNS": "80"})

    assert result.exit_code == 0, result.stderr
    _assert_ids_whole_with_hint(result.stdout, [_ANALYST_GRANT, _ENGINEER_GRANT, _GROUP_GRANT])
    assert "Names not found" in result.stderr
    assert "lakana principal list" in result.stderr
    assert "older" in result.stderr


def test_resolved_names_print_no_warning_and_json_none(tmp_path: Path) -> None:
    """Nothing is said on stderr when every name is found, or in JSON mode."""
    assert invoke(make_state(tmp_path, _api()), "grant", "list").stderr == ""
    api = FakeApi()
    api.on("GET", "/api/v1/grants", json=[_without_names(g) for g in _GRANTS])
    quiet = invoke(make_state(tmp_path, api), "--output", "json", "grant", "list")
    assert quiet.stderr == ""
