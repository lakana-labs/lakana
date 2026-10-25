"""`lakana connection *`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeApi, invoke, make_state

_BASE = "/api/v1/environments/prod/connections"
_VALUE = "s3cr3t-value"
_CONNECTION: dict[str, Any] = {
    "name": "crm-db",
    "kind": "postgresql",
    "address": {"host": "db.example.com", "port": 5432, "database": "crm"},
    "settings": {"user": "reader", "tls": "verify-full"},
    "secret": "crm-db-connection",
    "declared_by": [{"repository": "analytics", "asset": "customers"}],
    "created_by": "dana",
    "created_at": "2026-10-23T12:00:00+00:00",
    "updated_at": "2026-10-23T12:00:00+00:00",
}
_S3: dict[str, Any] = {
    **_CONNECTION,
    "name": "landing",
    "kind": "s3",
    "address": {
        "bucket": "landing",
        "region": "eu-central-1",
        "endpoint": "https://s3.eu-central-1.amazonaws.com",
    },
    "settings": {"access_key_id": "AKIA1"},
    "secret": "landing-connection",
    "declared_by": [],
}


def _sent(api: FakeApi, method: str) -> list[dict[str, Any]]:
    """Read the JSON bodies of the requests of one method.

    Parameters
    ----------
    api
        The fake.
    method
        The HTTP method.

    Returns
    -------
    list of dict
        The bodies, in order.
    """
    return [json.loads(r.content) for r in api.requests if r.method == method]


def test_create_with_password_stdin(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_CONNECTION)

    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "crm-db",
        "--env",
        "prod",
        "--kind",
        "postgresql",
        "--host",
        "db.example.com",
        "--database",
        "crm",
        "--user",
        "reader",
        "--password-stdin",
        input=f"{_VALUE}\n",
    )

    assert result.exit_code == 0, result.output
    assert _sent(api, "POST") == [
        {
            "name": "crm-db",
            "kind": "postgresql",
            "address": {"host": "db.example.com", "database": "crm"},
            "settings": {"user": "reader"},
            "secret": None,
            "secret_values": {"password": _VALUE},
        }
    ]
    assert "Created connection crm-db in prod with its secret crm-db-connection" in result.stdout
    assert _VALUE not in result.output


def test_password_stdin_strips_one_newline_and_keeps_the_rest(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_CONNECTION)
    invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "crm-db",
        "--env",
        "prod",
        "--kind",
        "tcp",
        "--host",
        "db.example.com",
        "--port",
        "1521",
        "--password-stdin",
        input="pw \n\n",
    )
    (body,) = _sent(api, "POST")
    assert body["address"] == {"host": "db.example.com", "port": 1521}
    assert body["secret_values"] == {"password": "pw \n"}


def test_password_stdin_uses_the_key_of_the_http_auth(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_CONNECTION)
    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "api",
        "--env",
        "prod",
        "--kind",
        "http",
        "--url",
        "https://api.example.com/v1",
        "--auth",
        "bearer",
        "--password-stdin",
        input="tok",
    )
    assert result.exit_code == 0, result.output
    (body,) = _sent(api, "POST")
    assert body["address"] == {"base_url": "https://api.example.com/v1"}
    assert body["settings"] == {"auth": "bearer"}
    assert body["secret_values"] == {"token": "tok"}


def test_password_stdin_for_settings_without_a_secret_is_refused(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "api",
        "--env",
        "prod",
        "--kind",
        "http",
        "--url",
        "https://api.example.com",
        "--password-stdin",
        input="tok",
    )
    assert result.exit_code == 2
    assert (
        "--password-stdin: kind http with auth none takes no secret; give --auth, or leave "
        "out --password-stdin" in result.output
    )
    assert api.requests == []


def test_create_with_values_stdin(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_S3)

    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "landing",
        "--env",
        "prod",
        "--kind",
        "s3",
        "--bucket",
        "landing",
        "--region",
        "eu-central-1",
        "--access-key-id",
        "AKIA1",
        "--values-stdin",
        input='{"secret_access_key": "x", "session_token": "y"}',
    )

    assert result.exit_code == 0, result.output
    (body,) = _sent(api, "POST")
    assert body["address"] == {"bucket": "landing", "region": "eu-central-1"}
    assert body["settings"] == {"access_key_id": "AKIA1"}
    assert body["secret_values"] == {"secret_access_key": "x", "session_token": "y"}
    bad = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "landing",
        "--env",
        "prod",
        "--kind",
        "s3",
        "--bucket",
        "landing",
        "--values-stdin",
        input='{"secret_access_key": 1}',
    )
    assert bad.exit_code == 2
    assert "--values-stdin expects a JSON object of string keys and values" in bad.output


def test_an_option_the_kind_does_not_take_is_refused(tmp_path: Path) -> None:
    api = FakeApi()

    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "landing",
        "--env",
        "prod",
        "--kind",
        "s3",
        "--host",
        "x",
    )
    files = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "exports",
        "--env",
        "prod",
        "--kind",
        "directory",
        "--root",
        "shared",
        "--path",
        "exports",
        "--password-stdin",
        input="pw",
    )

    assert result.exit_code == 2
    assert (
        "--host does not apply to kind s3; its options are --bucket, --region, --endpoint, "
        "--access-key-id, --prefix, --secret, --password-stdin, --values-stdin" in result.output
    )
    assert files.exit_code == 2
    assert (
        "--password-stdin does not apply to kind directory; its options are --root, --path"
        in files.output
    )
    assert api.requests == []


def test_an_unknown_kind_names_the_kinds(tmp_path: Path) -> None:
    result = invoke(
        make_state(tmp_path, FakeApi()),
        "connection",
        "create",
        "x",
        "--env",
        "prod",
        "--kind",
        "oracle",
    )
    assert result.exit_code == 2
    assert "postgresql" in result.output
    assert "tcp" in result.output


def test_two_secret_options_are_refused(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "crm-db",
        "--env",
        "prod",
        "--kind",
        "postgresql",
        "--host",
        "h",
        "--secret",
        "crm-pw",
        "--password-stdin",
        input="pw",
    )
    assert result.exit_code == 2
    assert "Pass only one of --secret, --password-stdin or --values-stdin" in result.output
    assert api.requests == []


def test_ca_file_is_read_into_the_settings(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=_CONNECTION)
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    (tmp_path / "ca.pem").write_text(pem)
    args = [
        "connection",
        "create",
        "crm-db",
        "--env",
        "prod",
        "--kind",
        "postgresql",
        "--host",
        "db.example.com",
        "--database",
        "crm",
        "--user",
        "reader",
    ]

    result = invoke(make_state(tmp_path, api), *args, "--ca-file", str(tmp_path / "ca.pem"))
    missing = invoke(make_state(tmp_path, api), *args, "--ca-file", "ca.pem")

    assert result.exit_code == 0, result.output
    assert _sent(api, "POST")[0]["settings"] == {"user": "reader", "ca_certificate": pem}
    assert missing.exit_code == 2
    assert "cannot read --ca-file ca.pem: No such file or directory" in missing.output
    assert len(api.requests) == 1


def test_show_prints_the_connection(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/crm-db", json=_CONNECTION)

    result = invoke(make_state(tmp_path, api), "connection", "show", "crm-db", "--env", "prod")
    as_json = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "connection",
        "show",
        "crm-db",
        "--env",
        "prod",
    )

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert "Connection:  crm-db (postgresql)" in lines
    assert "Address:     host=db.example.com port=5432 database=crm" in lines
    assert "Settings:    user=reader tls=verify-full" in lines
    assert "Secret:      crm-db-connection" in lines
    assert "Declared by: analytics: customers" in lines
    assert json.loads(as_json.stdout) == _CONNECTION


def test_list_prints_one_row_per_connection(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json={"items": [_CONNECTION, _S3], "next_cursor": None})
    result = invoke(make_state(tmp_path, api), "connection", "list", "--env", "prod")
    assert result.exit_code == 0, result.output
    assert "crm-db" in result.stdout
    assert "landing-connection" in result.stdout
    assert "host=db.example.com port=5432 database=crm" in result.stdout


def test_list_follows_every_page(tmp_path: Path) -> None:
    pages = {
        None: {"items": [_CONNECTION], "next_cursor": "c1"},
        "c1": {"items": [_S3], "next_cursor": None},
    }
    api = FakeApi()
    api.routes["GET", _BASE] = lambda request: httpx.Response(
        200, json=pages[request.url.params.get("cursor")]
    )
    result = invoke(
        make_state(tmp_path, api), "--output", "json", "connection", "list", "--env", "prod"
    )
    assert [c["name"] for c in json.loads(result.stdout)] == ["crm-db", "landing"]


def test_update_rotates_the_secret_and_clears_a_setting(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/crm-db", json=_CONNECTION)
    api.on("PATCH", f"{_BASE}/crm-db", json=_CONNECTION)

    rotated = invoke(
        make_state(tmp_path, api),
        "connection",
        "update",
        "crm-db",
        "--env",
        "prod",
        "--host",
        "db2.example.com",
        "--password-stdin",
        input=f"{_VALUE}\n",
    )
    cleared = invoke(
        make_state(tmp_path, api),
        "connection",
        "update",
        "crm-db",
        "--env",
        "prod",
        "--clear",
        "secret",
        "--clear",
        "ca_certificate",
    )
    wrong = invoke(
        make_state(tmp_path, api),
        "connection",
        "update",
        "crm-db",
        "--env",
        "prod",
        "--bucket",
        "b",
    )
    nothing = invoke(make_state(tmp_path, api), "connection", "update", "crm-db", "--env", "prod")

    assert rotated.exit_code == 0, rotated.output
    assert cleared.exit_code == 0, cleared.output
    assert _sent(api, "PATCH") == [
        {"address": {"host": "db2.example.com"}, "secret_values": {"password": _VALUE}},
        {"clear": ["secret", "ca_certificate"]},
    ]
    assert "Updated connection crm-db in prod" in rotated.stdout
    assert _VALUE not in rotated.output
    assert wrong.exit_code == 2
    assert "--bucket does not apply to kind postgresql" in wrong.output
    assert nothing.exit_code == 2
    assert "Nothing to change; pass at least one option." in nothing.output


def test_delete_asks_unless_yes_and_prints_the_409_unchanged(tmp_path: Path) -> None:
    text = (
        "connection `crm-db` is used by `analytics: customers` in `prod`; deploy a version "
        "without it first."
    )
    api = FakeApi()
    api.on("DELETE", f"{_BASE}/crm-db", status=409, json={"detail": text, "type": "Conflict"})

    unasked = invoke(make_state(tmp_path, api), "connection", "delete", "crm-db", "--env", "prod")
    refused = invoke(
        make_state(tmp_path, api), "connection", "delete", "crm-db", "--env", "prod", "--yes"
    )

    assert unasked.exit_code == 2
    assert "Refusing without a terminal; pass --yes to confirm." in unasked.output
    assert refused.exit_code == 7
    assert text in refused.output
    assert len(api.requests) == 1
    api.on("DELETE", f"{_BASE}/crm-db", status=204)
    deleted = invoke(
        make_state(tmp_path, api), "connection", "delete", "crm-db", "--env", "prod", "--yes"
    )
    assert deleted.exit_code == 0, deleted.output
    assert "Deleted connection crm-db in prod" in deleted.stdout


def test_a_kind_this_command_line_does_not_know_says_to_upgrade(tmp_path: Path) -> None:
    """A newer server may hold a kind this release does not know; `update` says what to do."""
    api = FakeApi()
    api.on("GET", f"{_BASE}/queue", json={**_CONNECTION, "name": "queue", "kind": "kafka"})

    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "update",
        "queue",
        "--env",
        "prod",
        "--host",
        "q.example.com",
    )

    assert result.exit_code == 2
    assert (
        "connection queue is of kind kafka, which this lakana command line does not know; "
        "upgrade lakana to change it (the server is newer)" in result.output
    )
    assert [r.method for r in api.requests] == ["GET"]


def test_a_connection_without_secret_or_settings_says_so(tmp_path: Path) -> None:
    """Created without a secret, the message names none; `show` writes `(none)` for each."""
    files = {
        **_CONNECTION,
        "name": "exports",
        "kind": "directory",
        "address": {"root": "shared", "path": "exports"},
        "settings": {},
        "secret": None,
        "declared_by": [],
    }
    api = FakeApi()
    api.on("POST", _BASE, status=201, json=files)
    api.on("GET", f"{_BASE}/exports", json=files)

    created = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "exports",
        "--env",
        "prod",
        "--kind",
        "directory",
        "--root",
        "shared",
        "--path",
        "exports",
    )
    shown = invoke(make_state(tmp_path, api), "connection", "show", "exports", "--env", "prod")

    assert created.exit_code == 0, created.output
    assert created.stdout == "Created connection exports in prod\n"
    lines = shown.stdout.splitlines()
    assert "Settings:    (none)" in lines
    assert "Secret:      (none)" in lines
    assert "Declared by: (none)" in lines


def test_update_refuses_two_ways_of_giving_the_secret_before_asking_the_server(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "update",
        "crm-db",
        "--env",
        "prod",
        "--secret",
        "crm-pw",
        "--values-stdin",
        input="{}",
    )
    assert result.exit_code == 2
    assert "Pass only one of --secret, --password-stdin or --values-stdin" in result.output
    assert api.requests == []


def test_create_with_a_named_secret_says_which_it_uses(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", _BASE, status=201, json={**_CONNECTION, "secret": "crm-pw"})
    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "create",
        "crm-db",
        "--env",
        "prod",
        "--kind",
        "postgresql",
        "--host",
        "db.example.com",
        "--database",
        "crm",
        "--user",
        "reader",
        "--secret",
        "crm-pw",
    )
    assert result.exit_code == 0, result.output
    assert _sent(api, "POST")[0]["secret"] == "crm-pw"
    assert "Created connection crm-db in prod using secret crm-pw" in result.stdout


_TEST_ID = "6f1c1f0e-0a52-4b7b-9d0e-2f3a4b5c6d7e"


def _test_body(
    state: str, outcome: str | None = None, message: str | None = None
) -> dict[str, Any]:
    """Build a `ConnectionTestOut` body.

    Parameters
    ----------
    state
        ``queued``, ``running`` or ``done``.
    outcome
        The outcome once done.
    message
        The message once done.

    Returns
    -------
    dict
        The body.
    """
    return {
        "id": _TEST_ID,
        "state": state,
        "outcome": outcome,
        "message": message,
        "created_at": "2026-10-25T12:00:00+00:00",
        "finished_at": "2026-10-25T12:00:03+00:00" if state == "done" else None,
    }


def test_a_refused_test_prints_the_outcome_and_its_next_step_and_exits_1(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", f"{_BASE}/crm-db/tests", status=202, json=_test_body("queued"))
    api.on(
        "GET",
        f"{_BASE}/crm-db/tests/{_TEST_ID}",
        json=_test_body("done", "refused", "db.example.com:5432 refused the connection"),
    )

    result = invoke(make_state(tmp_path, api), "connection", "test", "crm-db", "--env", "prod")

    assert result.exit_code == 1
    assert "refused: db.example.com:5432 refused the connection" in result.stdout
    assert "the port and the source's firewall" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("outcome", "next_step"),
    [
        ("dns", "Check the host name."),
        ("timeout", "the network path between the worker and the source"),
        ("tls", "lakana connection update NAME --ca-file ca.pem"),
        ("tls", "that the server offers TLS 1.2 or newer"),
        ("not_allowed", "reachable from outside the Lakana stack"),
        ("root_unavailable", "Check the root setting."),
        ("not_found", "lakana connection show NAME"),
        ("credentials", "Check the secret."),
        ("failed", "See the worker's log."),
    ],
)
def test_every_outcome_that_is_not_ok_names_its_next_step(
    tmp_path: Path, outcome: str, next_step: str
) -> None:
    """Each outcome prints its own next step, and none of them names the egress proxy.

    Parameters
    ----------
    tmp_path
        A directory for the CLI's state.
    outcome
        The outcome the server reports.
    next_step
        A part of the next step the CLI must print for it.
    """
    api = FakeApi()
    api.on("POST", f"{_BASE}/crm-db/tests", status=202, json=_test_body("queued"))
    api.on(
        "GET", f"{_BASE}/crm-db/tests/{_TEST_ID}", json=_test_body("done", outcome, "what happened")
    )

    result = invoke(make_state(tmp_path, api), "connection", "test", "crm-db", "--env", "prod")

    assert result.exit_code == 1
    assert f"{outcome}: what happened" in result.stdout
    assert next_step in result.stdout + result.stderr
    assert "proxy" not in result.stdout + result.stderr


def test_an_ok_test_prints_ok_and_exits_0(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", f"{_BASE}/crm-db/tests", status=202, json=_test_body("queued"))
    api.on("GET", f"{_BASE}/crm-db/tests/{_TEST_ID}", json=_test_body("done", "ok", "reached it"))

    result = invoke(make_state(tmp_path, api), "connection", "test", "crm-db", "--env", "prod")

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok: reached it"


def test_no_wait_prints_the_id_and_how_to_look_again(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("POST", f"{_BASE}/crm-db/tests", status=202, json=_test_body("queued"))

    result = invoke(
        make_state(tmp_path, api), "connection", "test", "crm-db", "--env", "prod", "--no-wait"
    )

    assert result.exit_code == 0
    assert _TEST_ID in result.stdout
    assert f"lakana connection test crm-db --env prod --id {_TEST_ID}" in result.stdout
    assert [r.method for r in api.requests] == ["POST"]


def test_id_looks_at_an_earlier_test(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET", f"{_BASE}/crm-db/tests/{_TEST_ID}", json=_test_body("done", "dns", "no such host")
    )

    result = invoke(
        make_state(tmp_path, api),
        "connection",
        "test",
        "crm-db",
        "--env",
        "prod",
        "--id",
        _TEST_ID,
    )

    assert result.exit_code == 1
    assert "dns: no such host" in result.stdout
    assert "Check the host name." in result.stdout + result.stderr
    assert [r.method for r in api.requests] == ["GET"]


def test_every_outcome_but_ok_has_a_next_step() -> None:
    from lakana.cli.connections import next_step

    outcomes = [
        "dns",
        "refused",
        "timeout",
        "tls",
        "not_allowed",
        "root_unavailable",
        "not_found",
        "credentials",
        "failed",
    ]

    assert all(next_step(outcome) for outcome in outcomes)
    assert next_step("ok") is None
    assert "--ca-file ca.pem" in (next_step("tls") or "")
    assert "lakana connection show" in (next_step("not_found") or "")


def test_a_test_that_never_finishes_is_an_error_with_its_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lakana.resources.connections.time.sleep", lambda _s: None)
    ticks = iter(range(0, 1000, 30))
    monkeypatch.setattr("lakana.resources.connections.time.monotonic", lambda: next(ticks))
    api = FakeApi()
    api.on("POST", f"{_BASE}/crm-db/tests", status=202, json=_test_body("queued"))
    api.on("GET", f"{_BASE}/crm-db/tests/{_TEST_ID}", json=_test_body("queued"))

    result = invoke(make_state(tmp_path, api), "connection", "test", "crm-db", "--env", "prod")

    assert result.exit_code == 1
    assert "still queued after 120s" in result.stderr
    assert "check it later with lakana query status" not in result.stderr


def test_a_test_with_json_output_prints_the_test_as_json_and_exits_by_its_outcome(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    api.on("POST", f"{_BASE}/crm-db/tests", status=202, json=_test_body("queued"))
    api.on(
        "GET",
        f"{_BASE}/crm-db/tests/{_TEST_ID}",
        json=_test_body("done", "refused", "db.example.com:5432 refused the connection"),
    )

    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "connection",
        "test",
        "crm-db",
        "--env",
        "prod",
    )

    assert result.exit_code == 1
    body = json.loads(result.stdout)
    assert (body["id"], body["outcome"], body["state"]) == (_TEST_ID, "refused", "done")


def test_an_outcome_this_version_does_not_know_is_printed_without_a_next_step(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    api.on("POST", f"{_BASE}/crm-db/tests", status=202, json=_test_body("queued"))
    api.on(
        "GET",
        f"{_BASE}/crm-db/tests/{_TEST_ID}",
        json=_test_body("done", "quota", "the source said no"),
    )

    result = invoke(make_state(tmp_path, api), "connection", "test", "crm-db", "--env", "prod")

    assert result.exit_code == 1
    assert "quota: the source said no" in result.stdout
    assert "Next:" not in result.stdout + result.stderr
