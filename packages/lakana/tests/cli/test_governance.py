"""Tests for `lakana grant|row-filter|column-mask|secret|audit *`."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from conftest import FakeApi, invoke, make_state

_GRANT = {
    "id": "g1",
    "principal_id": "p1",
    "group_id": None,
    "role": "ANALYST",
    "action": None,
    "kind": "ENVIRONMENT",
    "environment": "dev",
    "namespace": None,
    "table": None,
    "granted_by": "a1",
    "granted_at": "2026-01-01T00:00:00+00:00",
}


def test_grant_create_upper_cases_and_forwards_the_securable(tmp_path: Path) -> None:
    """Roles and kinds are case-insensitive on the command line."""
    api = FakeApi()
    api.on("POST", "/api/v1/grants", status=201, json=_GRANT)

    result = invoke(
        make_state(tmp_path, api),
        "grant",
        "create",
        "--principal",
        "bob",
        "--role",
        "analyst",
        "--kind",
        "environment",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    body = json.loads(api.requests[0].content)
    assert (body["principal"], body["role"], body["kind"], body["environment"]) == (
        "bob",
        "ANALYST",
        "ENVIRONMENT",
        "dev",
    )


def test_grant_create_needs_exactly_one_subject_and_one_privilege(tmp_path: Path) -> None:
    """Both or neither of --principal/--group (or --role/--action) is a usage error."""
    state = make_state(tmp_path, FakeApi())

    assert (
        invoke(state, "grant", "create", "--role", "ANALYST", "--kind", "INSTANCE").exit_code == 2
    )
    assert (
        invoke(
            state,
            "grant",
            "create",
            "--principal",
            "a",
            "--group",
            "g",
            "--role",
            "ANALYST",
            "--kind",
            "INSTANCE",
        ).exit_code
        == 2
    )
    assert invoke(state, "grant", "create", "--principal", "a", "--kind", "INSTANCE").exit_code == 2


def test_grant_list_and_revoke(tmp_path: Path) -> None:
    """Filters are forwarded; revoke asks first."""
    api = FakeApi()
    api.on("GET", "/api/v1/grants", json=[_GRANT])
    api.on("DELETE", "/api/v1/grants/g1", status=204)

    listed = invoke(make_state(tmp_path, api), "grant", "list", "--principal", "bob")

    assert "g1" in listed.stdout
    assert api.requests[0].url.params["principal_name"] == "bob"
    assert invoke(make_state(tmp_path, api), "grant", "revoke", "g1").exit_code == 2
    assert invoke(make_state(tmp_path, api), "grant", "revoke", "g1", "--yes").exit_code == 0


def test_grant_list_shows_the_note_only_when_a_grant_has_one(tmp_path: Path) -> None:
    """A `note` column appears with a note; an older server's answer has none."""
    note = "no effect: MANAGE_GRANT needs the ADMIN role"
    stale = {**_GRANT, "id": "g2", "role": None, "action": "MANAGE_GRANT", "note": note}
    api = FakeApi()
    api.on("GET", "/api/v1/grants", json=[{**_GRANT, "note": None}, stale])
    shown = invoke(make_state(tmp_path, api), "grant", "list").stdout
    assert "note" in shown and "no effect" in shown and "MANAGE_GRANT" in shown

    api = FakeApi()
    api.on("GET", "/api/v1/grants", json=[{**_GRANT, "note": None}])
    assert "note" not in invoke(make_state(tmp_path, api), "grant", "list").stdout

    api = FakeApi()
    api.on("GET", "/api/v1/grants", json=[_GRANT])
    older = invoke(make_state(tmp_path, api), "grant", "list")
    assert older.exit_code == 0 and "note" not in older.stdout

    api = FakeApi()
    api.on("GET", "/api/v1/grants", json=[{**_GRANT, "note": None}, stale])
    rows = json.loads(invoke(make_state(tmp_path, api), "--output", "json", "grant", "list").stdout)
    assert [row["note"] for row in rows] == [None, note]


def test_row_filter_create_splits_the_table_name(tmp_path: Path) -> None:
    """`bronze.eu.sales` → namespace `bronze.eu`, table `sales`; env from the profile or flag."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/row-filters",
        status=201,
        json={
            "id": "r1",
            "principal_id": "p1",
            "group_id": None,
            "environment": "dev",
            "namespace": "bronze.eu",
            "table": "sales",
            "expression": "id > 1",
            "created_by": "a1",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )

    result = invoke(
        make_state(tmp_path, api),
        "row-filter",
        "create",
        "bronze.eu.sales",
        "--principal",
        "bob",
        "--expression",
        "id > 1",
        "--env",
        "dev",
    )

    assert result.exit_code == 0, result.stderr
    body = json.loads(api.requests[0].content)
    assert (body["namespace"], body["table"], body["expression"]) == (
        "bronze.eu",
        "sales",
        "id > 1",
    )


def test_row_filter_list_and_revoke(tmp_path: Path) -> None:
    """Filters are forwarded to the list route; revoke asks first."""
    row_filter = {
        "id": "r1",
        "principal_id": "p1",
        "group_id": None,
        "environment": "dev",
        "namespace": "bronze",
        "table": "sales",
        "expression": "id > 1",
        "created_by": "a1",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    api = FakeApi()
    api.on("GET", "/api/v1/row-filters", json=[row_filter])
    api.on("DELETE", "/api/v1/row-filters/r1", status=204)

    listed = invoke(
        make_state(tmp_path, api), "row-filter", "list", "--principal", "bob", "--env", "dev"
    )

    assert "r1" in listed.stdout
    assert api.requests[0].url.params["principal_name"] == "bob"
    assert invoke(make_state(tmp_path, api), "row-filter", "revoke", "r1").exit_code == 2
    assert invoke(make_state(tmp_path, api), "row-filter", "revoke", "r1", "--yes").exit_code == 0


def test_column_mask_create_and_revoke(tmp_path: Path) -> None:
    """A mask names the table and column; revoke asks first."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/column-masks",
        status=201,
        json={
            "id": "m1",
            "principal_id": None,
            "group_id": "g1",
            "environment": "dev",
            "namespace": "bronze",
            "table": "sales",
            "column": "region",
            "expression": "'X'",
            "created_by": "a1",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    api.on("DELETE", "/api/v1/column-masks/m1", status=204)

    created = invoke(
        make_state(tmp_path, api),
        "column-mask",
        "create",
        "bronze.sales",
        "region",
        "--group",
        "eng",
        "--expression",
        "'X'",
        "--env",
        "dev",
    )

    assert created.exit_code == 0, created.stderr
    assert json.loads(api.requests[0].content)["column"] == "region"
    assert invoke(make_state(tmp_path, api), "column-mask", "revoke", "m1", "--yes").exit_code == 0


def test_policy_lists_show_whether_each_policy_is_bound(tmp_path: Path) -> None:
    """Both listings have a `bound` column of yes/no; JSON has the boolean."""
    row = {
        "principal_id": "p1",
        "group_id": None,
        "environment": "dev",
        "namespace": "bronze",
        "table": "sales",
        "expression": "id > 1",
        "created_by": "a1",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    api = FakeApi()
    api.on("GET", "/api/v1/row-filters", json=[{**row, "id": "r1", "bound": True}])
    api.on("GET", "/api/v1/column-masks", json=[{**row, "id": "m1", "column": "c", "bound": False}])

    filters = invoke(make_state(tmp_path, api), "row-filter", "list")
    masks = invoke(make_state(tmp_path, api), "column-mask", "list")
    as_json = invoke(make_state(tmp_path, api), "--output", "json", "row-filter", "list")

    assert "bound" in filters.stdout
    assert "yes" in next(line for line in filters.stdout.splitlines() if "r1" in line).split()
    assert "no" in next(line for line in masks.stdout.splitlines() if "m1" in line).split()
    assert '"bound": true' in as_json.stdout


def test_column_mask_list(tmp_path: Path) -> None:
    """Filters are forwarded to the list route."""
    mask = {
        "id": "m1",
        "principal_id": None,
        "group_id": "g1",
        "environment": "dev",
        "namespace": "bronze",
        "table": "sales",
        "column": "region",
        "expression": "'X'",
        "created_by": "a1",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    api = FakeApi()
    api.on("GET", "/api/v1/column-masks", json=[mask])

    listed = invoke(
        make_state(tmp_path, api), "column-mask", "list", "--group", "eng", "--env", "dev"
    )

    assert "m1" in listed.stdout
    assert api.requests[0].url.params["group_name"] == "eng"


def test_secret_create_from_stdin_never_echoes_values(tmp_path: Path) -> None:
    """`--values-stdin` takes a JSON object; the values never appear in output."""
    api = FakeApi()
    api.on(
        "POST",
        "/api/v1/environments/dev/secrets",
        status=201,
        json={
            "id": "s1",
            "name": "s3",
            "environment": "dev",
            "created_by": "a1",
            "created_at": "2026-01-01T00:00:00+00:00",
            "format_version": 1,
            "key_id": "env",
        },
    )

    result = invoke(
        make_state(tmp_path, api),
        "secret",
        "create",
        "s3",
        "--env",
        "dev",
        "--values-stdin",
        input='{"access_key_id": "AKIAHIDDEN"}',
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[0].content)["values"] == {"access_key_id": "AKIAHIDDEN"}
    assert "AKIAHIDDEN" not in result.stdout + result.stderr


def test_secret_values_input_is_validated(tmp_path: Path) -> None:
    """Neither/both inputs, bad JSON, non-object JSON, or non-string values are usage errors."""
    state = make_state(tmp_path, FakeApi())

    assert invoke(state, "secret", "create", "s3", "--env", "dev").exit_code == 2
    assert (
        invoke(
            state,
            "secret",
            "create",
            "s3",
            "--env",
            "dev",
            "--value",
            "a=b",
            "--values-stdin",
            input="{}",
        ).exit_code
        == 2
    )
    assert (
        invoke(
            state, "secret", "create", "s3", "--env", "dev", "--values-stdin", input="not json"
        ).exit_code
        == 2
    )
    assert (
        invoke(
            state, "secret", "create", "s3", "--env", "dev", "--values-stdin", input="[1]"
        ).exit_code
        == 2
    )
    assert (
        invoke(
            state, "secret", "create", "s3", "--env", "dev", "--values-stdin", input='{"a": 1}'
        ).exit_code
        == 2
    )


def test_secret_create_with_a_malformed_value_never_echoes_it(tmp_path: Path) -> None:
    """A forgotten `=` (or an empty key) in `--value` never reaches stdout or stderr."""
    state = make_state(tmp_path, FakeApi())

    for value in ("AKIAHIDDEN", "=AKIAHIDDEN"):
        result = invoke(state, "secret", "create", "s3", "--env", "dev", "--value", value)
        assert result.exit_code == 2
        assert "AKIAHIDDEN" not in result.stdout + result.stderr


def test_secret_rotate_with_a_malformed_value_never_echoes_it(tmp_path: Path) -> None:
    """The same protection applies to `secret rotate`."""
    state = make_state(tmp_path, FakeApi())

    result = invoke(state, "secret", "rotate", "s3", "--env", "dev", "--value", "AKIAHIDDEN")

    assert result.exit_code == 2
    assert "AKIAHIDDEN" not in result.stdout + result.stderr


def test_secret_list_rotate_and_revoke(tmp_path: Path) -> None:
    """Rotate accepts `--value`; revoke asks first."""
    secret = {
        "id": "s1",
        "name": "s3",
        "environment": "dev",
        "created_by": "a1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "format_version": 1,
        "key_id": "env",
    }
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/secrets", json=[secret])
    api.on("PATCH", "/api/v1/environments/dev/secrets/s3", json=secret)
    api.on("DELETE", "/api/v1/environments/dev/secrets/s3", status=204)

    listing = invoke(make_state(tmp_path, api), "secret", "list", "--env", "dev").stdout
    assert "s3" in listing
    assert "format_version" in listing
    assert (
        invoke(
            make_state(tmp_path, api), "secret", "rotate", "s3", "--env", "dev", "--value", "k=v"
        ).exit_code
        == 0
    )
    assert (
        invoke(
            make_state(tmp_path, api), "secret", "revoke", "s3", "--env", "dev", "--yes"
        ).exit_code
        == 0
    )


def test_audit_list_one_page_and_all_pages(tmp_path: Path) -> None:
    """Without `--all` JSON is the page; with `--all` it follows cursors into one list."""
    event = {
        "id": "e1",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "principal_id": "p1",
        "event_type": "GRANT_CREATED",
        "securable_kind": None,
        "securable_environment": None,
        "securable_namespace": None,
        "securable_table": None,
        "detail": {},
    }
    pages = {
        None: {"items": [event], "next_cursor": "c1"},
        "c1": {"items": [{**event, "id": "e2"}], "next_cursor": None},
    }
    api = FakeApi()
    api.routes["GET", "/api/v1/audit-events"] = lambda request: httpx.Response(
        200, json=pages[request.url.params.get("cursor")]
    )

    one = invoke(make_state(tmp_path, api), "--output", "json", "audit", "list", "--limit", "1")
    everything = invoke(
        make_state(tmp_path, api), "--output", "json", "audit", "list", "--limit", "1", "--all"
    )

    assert json.loads(one.stdout)["next_cursor"] == "c1"
    assert [e["id"] for e in json.loads(everything.stdout)] == ["e1", "e2"]


def test_audit_list_shows_a_cursor_hint_in_table_mode(tmp_path: Path) -> None:
    """A further page in table mode prints a `--cursor` hint after the table."""
    event = {
        "id": "e1",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "principal_id": "p1",
        "event_type": "GRANT_CREATED",
        "securable_kind": None,
        "securable_environment": None,
        "securable_namespace": None,
        "securable_table": None,
        "detail": {},
    }
    api = FakeApi()
    api.on("GET", "/api/v1/audit-events", json={"items": [event], "next_cursor": "c1"})

    result = invoke(make_state(tmp_path, api), "audit", "list")

    assert result.exit_code == 0, result.stderr
    assert "More: --cursor c1" in result.stdout


def test_audit_list_treats_a_naive_since_as_utc(tmp_path: Path) -> None:
    """`--since 2026-01-01T00:00:00` is sent with an explicit UTC offset."""
    api = FakeApi()
    api.on("GET", "/api/v1/audit-events", json={"items": [], "next_cursor": None})

    result = invoke(make_state(tmp_path, api), "audit", "list", "--since", "2026-01-01T00:00:00")

    assert result.exit_code == 0, result.stderr
    assert api.requests[0].url.params["since"] == "2026-01-01T00:00:00+00:00"


def test_audit_list_keeps_an_explicit_offset_on_since(tmp_path: Path) -> None:
    """`--since` with its own UTC offset is sent unchanged."""
    api = FakeApi()
    api.on("GET", "/api/v1/audit-events", json={"items": [], "next_cursor": None})

    result = invoke(
        make_state(tmp_path, api), "audit", "list", "--since", "2026-01-01T00:00:00+05:00"
    )

    assert result.exit_code == 0, result.stderr
    assert api.requests[0].url.params["since"] == "2026-01-01T00:00:00+05:00"


def test_audit_list_rejects_a_bad_timestamp_and_cursor_with_all(tmp_path: Path) -> None:
    """An unparsable `--since`, or `--cursor` with `--all`, is a usage error."""
    state = make_state(tmp_path, FakeApi())

    assert invoke(state, "audit", "list", "--since", "yesterday").exit_code == 2
    assert invoke(state, "audit", "list", "--all", "--cursor", "c1").exit_code == 2


def test_secret_bind_sends_exactly_one_use(tmp_path: Path) -> None:
    """`lakana secret bind` takes exactly one of the four use options."""
    secret = {
        "id": "s1",
        "name": "pypi",
        "environment": "dev",
        "created_by": "a1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "format_version": 2,
        "key_id": "env",
        "use": "package_index",
        "use_target": "corp",
        "use_target_url": "https://corp.example.org/simple",
    }
    api = FakeApi()
    api.on("PUT", "/api/v1/environments/dev/secrets/pypi/use", json=secret)
    api.on("GET", "/api/v1/environments/dev/secrets", json=[secret])

    result = invoke(
        make_state(tmp_path, api), "secret", "bind", "pypi", "--env", "dev", "--index", "corp"
    )

    assert result.exit_code == 0, result.stderr
    assert (
        "Bound secret pypi in dev to package index corp (https://corp.example.org/simple)"
        in result.stdout
    )
    (sent,) = [r for r in api.requests if r.method == "PUT"]
    assert json.loads(sent.content) == {"kind": "package_index", "target": "corp"}
    listing = invoke(make_state(tmp_path, api), "secret", "list", "--env", "dev")
    assert "package_index" in listing.stdout
    assert "corp" in listing.stdout
    both = invoke(
        make_state(tmp_path, api),
        "secret",
        "bind",
        "pypi",
        "--env",
        "dev",
        "--index",
        "corp",
        "--own-env",
    )
    assert both.exit_code == 2
    assert "Pass only one of --index, --repository, --connection or --own-env" in both.output
    neither = invoke(make_state(tmp_path, api), "secret", "bind", "pypi", "--env", "dev")
    assert neither.exit_code == 2
    assert "Pass exactly one of --index, --repository, --connection or --own-env" in neither.output


def test_secret_create_with_a_use_sends_it(tmp_path: Path) -> None:
    """`--index`, `--repository` and `--own-env` put the use into the create request."""
    secret = {
        "id": "s1",
        "name": "pypi",
        "environment": "dev",
        "created_by": "a1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "format_version": 2,
        "key_id": "env",
        "use": "package_index",
        "use_target": "corp",
        "use_target_url": "https://corp.example.org/simple",
    }
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/secrets", status=201, json=secret)

    result = invoke(
        make_state(tmp_path, api),
        "secret",
        "create",
        "pypi",
        "--env",
        "dev",
        "--value",
        "password=pw",
        "--index",
        "corp",
    )

    assert result.exit_code == 0, result.stderr
    assert (
        "Created secret pypi in dev, bound to package index corp "
        "(https://corp.example.org/simple)" in result.stdout
    )
    assert json.loads(api.requests[-1].content)["use"] == {
        "kind": "package_index",
        "target": "corp",
    }
    own = invoke(
        make_state(tmp_path, api),
        "secret",
        "create",
        "pypi",
        "--env",
        "dev",
        "--value",
        "password=pw",
        "--own-env",
    )
    assert own.exit_code == 0, own.stderr
    assert json.loads(api.requests[-1].content)["use"] == {"kind": "environment", "target": None}
    plain = invoke(
        make_state(tmp_path, api),
        "secret",
        "create",
        "pypi",
        "--env",
        "dev",
        "--value",
        "password=pw",
    )
    assert "use" not in json.loads(api.requests[-1].content)
    assert plain.exit_code == 0, plain.stderr


def test_secret_create_refuses_two_uses(tmp_path: Path) -> None:
    """At most one use option is accepted, and nothing is sent."""
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "secret",
        "create",
        "pypi",
        "--env",
        "dev",
        "--value",
        "password=pw",
        "--index",
        "corp",
        "--repository",
        "repo",
    )

    assert result.exit_code == 2
    assert "Pass only one of --index, --repository, --connection or --own-env" in result.output
    assert api.requests == []


def test_secret_bind_prints_a_long_url_whole(tmp_path: Path) -> None:
    """The URL a secret goes to is never cut, however long."""
    url = "https://corp.example.org/" + "a" * 300 + "/simple"
    secret = {
        "id": "s1",
        "name": "pypi",
        "environment": "dev",
        "created_by": "a1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "format_version": 2,
        "key_id": "env",
        "use": "package_index",
        "use_target": "corp",
        "use_target_url": url,
    }
    api = FakeApi()
    api.on("PUT", "/api/v1/environments/dev/secrets/pypi/use", json=secret)

    result = invoke(
        make_state(tmp_path, api), "secret", "bind", "pypi", "--env", "dev", "--index", "corp"
    )

    assert result.exit_code == 0, result.stderr
    assert url in result.stdout


def test_secret_bind_to_a_connection(tmp_path: Path) -> None:
    """`--connection CONN` sends the use `connection` and says what the secret now serves."""
    secret = {
        "id": "s1",
        "name": "crm-db-connection",
        "environment": "prod",
        "created_by": "a1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "format_version": 2,
        "key_id": "env",
        "use": "connection",
        "use_target": "crm-db",
        "use_target_url": "db.example.com:5432",
    }
    api = FakeApi()
    api.on("PUT", "/api/v1/environments/prod/secrets/crm-db-connection/use", json=secret)

    result = invoke(
        make_state(tmp_path, api),
        "secret",
        "bind",
        "crm-db-connection",
        "--env",
        "prod",
        "--connection",
        "crm-db",
    )

    assert result.exit_code == 0, result.output
    (sent,) = [r for r in api.requests if r.method == "PUT"]
    assert json.loads(sent.content) == {"kind": "connection", "target": "crm-db"}
    assert (
        "Bound secret crm-db-connection in prod to connection crm-db (db.example.com:5432); "
        "it is now the secret the connection uses" in result.stdout
    )
    both = invoke(
        make_state(tmp_path, api),
        "secret",
        "bind",
        "x",
        "--env",
        "prod",
        "--connection",
        "crm-db",
        "--index",
        "corp",
    )
    assert both.exit_code == 2
    assert "Pass only one of --index, --repository, --connection or --own-env" in both.output


def test_audit_list_by_kind(tmp_path: Path) -> None:
    """`--kind connection` sends `kind=connection`; another kind is refused before a request."""
    api = FakeApi()
    api.on("GET", "/api/v1/audit-events", json={"items": [], "next_cursor": None})

    result = invoke(make_state(tmp_path, api), "audit", "list", "--kind", "connection")
    everything = invoke(make_state(tmp_path, api), "audit", "list", "--kind", "connection", "--all")
    other = invoke(make_state(tmp_path, api), "audit", "list", "--kind", "secret")

    assert result.exit_code == 0, result.output
    assert everything.exit_code == 0, everything.output
    assert [r.url.params.get("kind") for r in api.requests] == ["connection", "connection"]
    assert other.exit_code == 2
    assert "connection" in other.output


def test_secret_create_bound_to_a_connection(tmp_path: Path) -> None:
    """`secret create --connection CONN` creates the secret bound to that connection."""
    secret = {
        "id": "s1",
        "name": "crm-pw",
        "environment": "prod",
        "created_by": "a1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "format_version": 2,
        "key_id": "env",
        "use": "connection",
        "use_target": "crm-db",
        "use_target_url": "db.example.com:5432",
    }
    api = FakeApi()
    api.on("POST", "/api/v1/environments/prod/secrets", status=201, json=secret)

    result = invoke(
        make_state(tmp_path, api),
        "secret",
        "create",
        "crm-pw",
        "--env",
        "prod",
        "--value",
        "password=pw",
        "--connection",
        "crm-db",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(api.requests[-1].content)["use"] == {"kind": "connection", "target": "crm-db"}
    assert (
        "Created secret crm-pw in prod, bound to connection crm-db (db.example.com:5432)"
        in result.stdout
    )
