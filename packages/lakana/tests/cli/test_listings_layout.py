"""Every listing command at 80 and 120 columns, with the values real listings hold.

An id, a time and a digest are never split or cut; each item is one row; no header is cut
in a word; a column left out is named once, with a hint that is true for the situation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeApi, invoke, make_state

_ID = "3f2c9d1e-8b7a-4c55-9e0a-1b2c3d4e5f60"
_ID2 = "9a1b7c3d-0e4f-4a6b-8c2d-5e6f7a8b9c0d"
_TIME = "2026-10-04T12:00:00.123456+00:00"
_TIME2 = "2026-10-05T13:30:45.654321+00:00"
_SHA = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b"
_DOTTED = "bronze.Zürich.sales_orders_2026"
_GID = "5d4c3b2a-1f0e-4d9c-8b7a-6f5e4d3c2b1a"
_ENV = "/api/v1/environments/dev"


@dataclass(frozen=True)
class _Listing:
    """One listing command and the fake answer it needs.

    Parameters
    ----------
    argv
        The command line.
    path
        The route the SDK calls.
    items
        The JSON rows the fake answers with.
    whole
        Column and value pairs: the value must appear whole on its row whenever the column
        is shown.
    headers
        Column headers that must be readable whole whenever they are shown.
    key
        A value that identifies each item; every item has exactly one row line with it.
    page
        The answer is a page of `items` with a cursor.
    wraps_at
        Widths at which free text may wrap onto a second line of its row, because the
        columns that never wrap leave it little room (the `key` may then wrap too, when it is
        the free text that gives way).
    subjects
        Who each item is for: every width shows the whole text on exactly one line.
    """

    argv: tuple[str, ...]
    path: str
    items: list[dict[str, Any]]
    whole: tuple[tuple[str, str], ...] = ()
    headers: tuple[str, ...] = ()
    key: str | None = None
    page: bool = False
    wraps_at: tuple[int, ...] = ()
    subjects: tuple[str, ...] = ()


def _run(status: str = "succeeded", repo: str = "analytics-daten") -> dict[str, Any]:
    return {
        "id": _ID,
        "environment": "dev",
        "repository": repo,
        "project_version_id": _ID2,
        "commit_sha": _SHA,
        "kind": "scheduled",
        "status": status,
        "cancel_requested": False,
        "triggered_by": None,
        "run_as": "svc-etl",
        "resumed_from_run_id": None,
        "selection": {},
        "error_message": None,
        "created_at": _TIME,
        "started_at": _TIME,
        "finished_at": _TIME2,
        "tasks": [],
        "job": "daily_gold",
        "trigger": "schedule",
        "scheduled_for": _TIME,
        "missed": 0,
    }


def _job(name: str = "daily_gold") -> dict[str, Any]:
    return {
        "name": name,
        "environment": "dev",
        "repository": "analytics-daten",
        "select": ["gold.*"],
        "upstream": False,
        "downstream": False,
        "cron": "30 2 * * 1-5",
        "timezone": "Europe/Berlin",
        "state": "no run principal",
        "paused": False,
        "next_run_at": _TIME,
        "next_run_local": "2026-10-05T04:30:00+02:00",
        "waiting_for": None,
        "waits_for_run_id": None,
        "last_run_id": _ID,
        "last_run_status": "succeeded",
        "last_run_trigger": "schedule",
        "last_run_at": _TIME,
    }


def _query(status: str = "QUEUED") -> dict[str, Any]:
    queued = status == "QUEUED"
    return {
        "id": _ID,
        "submitter": "renate.müller",
        "kind": "append",
        "target": "gold.daily" if not queued else "bronze.sales_orders_2026",
        "status": status,
        "created_at": _TIME,
        "started_at": None if queued else _TIME,
        "finished_at": None,
        "error_cause": None,
        "waiting_for": {
            "code": "workers_busy",
            "message": (
                "All 2 workers are busy, and about 4 queries or deploys are ahead of this one."
            ),
            "workers": 2,
            "ahead": 4,
        }
        if queued
        else None,
    }


def _worker(doing: str | None = None) -> dict[str, Any]:
    return {
        "id": "analytics-box:41233:3f2c9d1e",
        "host": "analytics-box",
        "version": "0.9.0",
        "started_at": _TIME,
        "heartbeat_at": _TIME2,
        "status": "alive",
        "takes": "interactive",
        "query_run_id": doing,
        "deploy_run_id": None,
        "task_run_id": None,
    }


def _deploy() -> dict[str, Any]:
    return {
        "id": _ID,
        "environment": "dev",
        "repository": "analytics-daten",
        "kind": "deploy",
        "status": "succeeded",
        "requested_ref": "main",
        "resolved_sha": _SHA,
        "rollback_to_project_version_id": None,
        "error_message": None,
        "diff": None,
        "project_version_id": _ID2,
        "principal_id": _ID2,
        "created_at": _TIME,
        "started_at": _TIME,
        "finished_at": _TIME2,
    }


def _policy(**more: str | bool | None) -> dict[str, Any]:
    return {
        "id": _ID,
        "principal_id": _ID2,
        "group_id": None,
        "environment": "dev",
        "namespace": "bronze",
        "table": "sales_orders",
        "expression": "region = 'EU' AND amount > 100",
        "created_by": "admin",
        "created_at": _TIME,
        "bound": True,
        **more,
    }


def _grant(**more: str | None) -> dict[str, Any]:
    return {
        "id": _ID,
        "principal_id": _ID2,
        "group_id": None,
        "role": "READER",
        "action": None,
        "kind": "ROLE",
        "environment": "dev",
        "namespace": "bronze",
        "table": "sales_orders",
        "granted_by": "admin",
        "granted_at": _TIME,
        **more,
    }


_LISTINGS = {
    "runs": _Listing(
        ("runs", "list", "--env", "dev"),
        f"{_ENV}/runs",
        [_run(), _run("failed")],
        whole=(("id", _ID), ("created_at", _TIME)),
        headers=("id", "status"),
        key=_ID,
    ),
    "jobs": _Listing(
        ("job", "list", "--env", "dev"),
        f"{_ENV}/jobs",
        [_job(), _job()],
        headers=("name", "state"),
        key="daily_gold",
    ),
    "queries": _Listing(
        ("query", "list", "--env", "dev"),
        f"{_ENV}/queries",
        [_query(), _query("RUNNING")],
        whole=(("id", _ID),),
        headers=("id", "status"),
        key=_ID,
        wraps_at=(80, 100, 120),
    ),
    "workers": _Listing(
        ("worker", "list"),
        "/api/v1/workers",
        [_worker(), _worker(_ID)],
        whole=(("takes", "interactive"),),
        headers=("id", "status", "takes"),
        key="analytics-box:41233:3f2c9d1e",
        subjects=("query 3f2c9d1e",),
        wraps_at=(80,),
    ),
    "deploys": _Listing(
        ("deploys", "list", "--env", "dev"),
        f"{_ENV}/deploys",
        [_deploy(), _deploy()],
        whole=(("id", _ID),),
        headers=("id", "status"),
        key=_ID,
    ),
    "deployments": _Listing(
        ("deployments", "list", "--env", "dev"),
        f"{_ENV}/deployments",
        [
            {
                "repository": "analytics-daten",
                "project_version_id": _ID2,
                "commit_sha": _SHA,
                "updated_at": _TIME,
                "run_as": "svc-etl",
            },
        ],
        whole=(("commit_sha", _SHA),),
        headers=("repository", "commit_sha"),
        key="analytics-daten",
    ),
    "artifacts": _Listing(
        ("artifact", "list", "--env", "dev"),
        f"{_ENV}/artifacts",
        [
            {
                "name": "churn_modell_münchen",
                "namespace": "models",
                "uuid": _ID,
                "kind": "ARTIFACT",
                "current_version": 7,
                "versions": 3,
                "size_bytes": 2048,
                "created_at": _TIME,
            }
        ],
        headers=("name", "namespace", "current_version"),
        key="churn_modell_münchen",
    ),
    "tables": _Listing(
        ("table", "list", "bronze", "--env", "dev"),
        f"{_ENV}/namespaces/bronze/tables",
        [
            {
                "name": "sales_orders_2026_q4",
                "namespace": "bronze",
                "uuid": _ID,
                "metadata_location": None,
                "kind": "TABLE",
                "state": "published",
            }
        ],
        headers=("name", "namespace", "state"),
        key="sales_orders_2026_q4",
    ),
    "namespaces": _Listing(
        ("ns", "list", "--env", "dev"),
        f"{_ENV}/namespaces",
        [{"path": _DOTTED, "properties": {"owner": "data-team", "pii": "yes"}}],
        headers=("path",),
        key=_DOTTED,
    ),
    "environments": _Listing(
        ("env", "list"),
        "/api/v1/environments",
        [
            {
                "name": "production",
                "storage_root": "s3://lakana-warehouse-eu-central-1/production",
                "storage_secret_ref": None,
                "is_protected": True,
                "properties": {},
            }
        ],
        headers=("name", "is_protected"),
        key="production",
    ),
    "repositories": _Listing(
        ("repo", "list"),
        "/api/v1/repositories",
        [
            {
                "id": _ID,
                "name": "analytics-daten",
                "kind": "git",
                "url": "https://github.com/example-org/analytics-daten.git",
                "credential_secret": "github-readonly",
                "credential_username": None,
                "ssh_known_hosts": None,
                "default_ref": "main",
                "created_by": "admin",
                "created_at": _TIME,
                "updated_at": _TIME,
            }
        ],
        headers=("name", "url"),
        key="analytics-daten",
    ),
    "package indexes": _Listing(
        ("index", "list"),
        "/api/v1/package-indexes",
        [
            {
                "id": _ID,
                "name": "team-pakete",
                "url": "https://pkgs.example-org.internal/internal/api/pypi/team/simple",
                "credential_secret": "team-index-readonly",
                "created_by": "admin",
                "created_at": _TIME,
                "updated_at": _TIME,
            }
        ],
        headers=("name", "url"),
        key="team-pakete",
        whole=(("url", "https://pkgs.example-org.internal/internal/api/pypi/team/simple"),),
        wraps_at=(80,),
    ),
    "secrets": _Listing(
        ("secret", "list", "--env", "dev"),
        f"{_ENV}/secrets",
        [
            {
                "id": _ID,
                "name": "warehouse-readonly-credentials",
                "environment": "dev",
                "format_version": 2,
                "key_id": "env:0123456789abcdef",
                "use": "package_index",
                "use_target": "team-pakete",
                "created_by": "admin@example.org",
                "created_at": _TIME,
            }
        ],
        whole=(("use", "package_index"), ("created_at", _TIME)),
        headers=("name", "use", "created_at"),
        key="warehouse-readonly-credentials",
        wraps_at=(80,),
    ),
    "grants": _Listing(
        ("grant", "list"),
        "/api/v1/grants",
        [_grant(), _grant(principal_id=None, group_id=_GID)],
        key=_ID,
        whole=(("id", _ID),),
        wraps_at=(80,),
        headers=("subject", "role"),
    ),
    "row filters": _Listing(
        ("row-filter", "list"),
        "/api/v1/row-filters",
        [_policy(), _policy(principal_id=None, group_id=_GID)],
        whole=(("id", _ID),),
        wraps_at=(80, 100),
        headers=("subject", "table", "expression"),
        subjects=(f"principal {_ID2}", f"group {_GID}"),
    ),
    "column masks": _Listing(
        ("column-mask", "list"),
        "/api/v1/column-masks",
        [
            _policy(column="customer_email", expression="'***'"),
            _policy(principal_id=None, group_id=_GID, column="customer_email", expression="'***'"),
        ],
        whole=(("id", _ID),),
        wraps_at=(80,),
        headers=("subject", "table", "column"),
        subjects=(f"principal {_ID2}", f"group {_GID}"),
    ),
    "audit": _Listing(
        ("audit", "list"),
        "/api/v1/audit-events",
        [
            {
                "id": _ID,
                "occurred_at": _TIME,
                "principal_id": _ID2,
                "event_type": "PROTECTED_WRITE_DENIED",
                "securable_kind": "TABLE",
                "securable_environment": "dev",
                "securable_namespace": "bronze",
                "securable_table": "sales_orders",
                "detail": {},
            }
        ],
        whole=(("occurred_at", _TIME),),
        page=True,
        headers=("occurred_at", "event_type"),
        key="PROTECTED_WRITE_DENIED",
    ),
    "checks": _Listing(
        ("check", "list", "--env", "dev"),
        f"{_ENV}/checks",
        [
            {
                "run_id": _ID,
                "task": "silver.station_temperatures",
                "name": "silver.station_temperatures/unique_reading",
                "check": "unique_reading",
                "path": "checks/silver/station_temperatures/unique_reading.sql",
                "table": "silver.station_temperatures",
                "table_id": None,
                "on_failure": "block",
                "outcome": "failed",
                "failing_rows": 3,
                "error": None,
                "checked_snapshot_id": 7,
                "rollback_outcome": "rolled_back",
                "policy_applied": False,
                "policy_note": None,
                "query_run_id": None,
                "started_at": _TIME,
                "finished_at": _TIME,
            }
        ],
        whole=(("finished_at", _TIME),),
        page=True,
        headers=("check", "outcome"),
        key="unique_reading",
        wraps_at=(80, 100),
    ),
    "tokens": _Listing(
        ("token", "list", "svc-etl"),
        "/api/v1/principals/svc-etl/tokens",
        [{"key_id": "k1a2b3c4", "name": "ci-pipeline-token", "last_used_at": _TIME}],
        whole=(("last_used_at", _TIME),),
        headers=("key_id", "name"),
        key="ci-pipeline-token",
    ),
    "groups": _Listing(
        ("group", "list"),
        "/api/v1/groups",
        [{"name": "datenanalysten-zürich"}],
        headers=("name",),
        key="datenanalysten-zürich",
    ),
    "principals": _Listing(
        ("principal", "list"),
        "/api/v1/principals",
        [{"name": "renate.müller@example.org", "type": "USER", "is_active": True}],
        headers=("name", "type"),
        key="renate.müller@example.org",
    ),
}


def _render(tmp_path: Path, listing: _Listing, width: int) -> str:
    """Run a listing command on a pipe `width` columns wide.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    listing
        The command and its fake answer.
    width
        The `COLUMNS` value.

    Returns
    -------
    str
        What it printed.
    """
    api = FakeApi()
    body = {"items": listing.items, "next_cursor": None} if listing.page else listing.items
    api.on("GET", listing.path, json=body)
    result = invoke(make_state(tmp_path, api), *listing.argv, env={"COLUMNS": str(width)})
    assert result.exit_code == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("width", [80, 100, 120])
@pytest.mark.parametrize("name", list(_LISTINGS))
def test_a_listing_is_readable_at_80_100_and_120_columns(
    tmp_path: Path, name: str, width: int
) -> None:
    """One row per item, whole ids and times, whole headers, and a true hint."""
    listing = _LISTINGS[name]
    out = _render(tmp_path, listing, width)
    lines = out.splitlines()
    assert "…" not in out
    assert all(len(line) <= width for line in lines if not line.startswith("Not shown"))
    border = next(i for i, line in enumerate(lines) if line.startswith("┡"))
    cells = [cell.strip() for cell in lines[border - 1].split("┃")[1:-1]]
    assert all(
        cell
        in set(listing.items[0])
        | {"size", "subject", "age", "last_seen", "doing", "caches", "run", "rows"}
        | {"schedule", "triggers", "next_run", "last_run"}
        for cell in cells
    ), cells
    assert all(header in cells for header in listing.headers), (cells, listing.headers)
    rows = [line for line in lines if line.startswith("│")]
    if width not in listing.wraps_at:
        assert len(rows) == len(listing.items)
    for row in [row for row in rows if listing.key is None or listing.key in row]:
        for column, value in listing.whole:
            if column in cells:
                assert value in row
    if width in listing.wraps_at:
        # The key may fold here, so no row is found by it: look for the whole value on any
        # one line instead. A listing of one item has exactly one such line.
        for column, value in listing.whole:
            if column in cells:
                on_one_line = sum(value in row for row in rows)
                assert on_one_line == len(listing.items), (column, out)
    if listing.key and width not in listing.wraps_at:
        assert sum(listing.key in row for row in rows) == len(listing.items)
    for subject in listing.subjects:
        assert sum(subject in row for row in rows) == 1, (subject, out)
    hint = [line for line in lines if line.startswith("Not shown")]
    shown = set(cells)
    if hint:
        (text,) = hint
        match = re.fullmatch(r"Not shown: (.+)\. Use --output json to see (it|them)\.", text)
        assert match, text
        left_out = match.group(1).split(", ")
        assert not shown & set(left_out)
        assert (match.group(2) == "it") == (len(left_out) == 1)
        assert "Widen" not in text


_NO_EFFECT_LINE = (
    "no effect: the grant names an action that works only with the ADMIN role on the instance. "
    "Revoke it with `lakana grant revoke ID`."
)


@pytest.mark.parametrize("width", [80, 100, 120])
@pytest.mark.parametrize("group", [False, True])
def test_a_grant_without_effect_shows_the_marker_and_one_explaining_line(
    tmp_path: Path, width: int, group: bool
) -> None:
    """`subject` and the marker stay; ids are whole when shown; the line explains it once."""
    who = {"principal_id": None, "group_id": _GID} if group else {}
    stale = _grant(
        role=None,
        action="MANAGE_GRANT",
        kind="INSTANCE",
        environment=None,
        namespace=None,
        table=None,
        note="no effect: MANAGE_GRANT needs the ADMIN role",
        **who,
    )
    listing = _Listing(("grant", "list"), "/api/v1/grants", [_grant(note=None), stale])
    out = _render(tmp_path, listing, width)
    lines = out.splitlines()
    header = next(i for i, line in enumerate(lines) if line.startswith("┡"))
    cells = [cell.strip() for cell in lines[header - 1].split("┃")[1:-1]]
    assert "subject" in cells and "note" in cells, out
    rows = [line for line in lines if line.startswith("│")]
    assert sum("no effect" in row for row in rows) == 1, out
    assert "MANAGE_GRANT needs" not in out
    subject = f"group {_GID}" if group else f"principal {_ID2}"
    column = cells.index("subject") + 1
    folded = "".join(row.split("│")[column].strip() for row in rows)
    assert subject.replace(" ", "") in folded.replace(" ", ""), out
    assert "id" in cells and sum(_ID in row for row in rows) == 2, out
    assert lines.count(_NO_EFFECT_LINE) == 1, out


def _stale(number: int, **more: str | None) -> dict[str, Any]:
    """Build a grant of a single action that has no effect.

    Parameters
    ----------
    number
        Makes the grant's id distinct.
    **more
        Fields that replace the defaults.

    Returns
    -------
    dict
        The grant as the API sends it, with the id ``0000000N-…``.
    """
    return _grant(
        **{
            "id": f"{number:08d}{_ID[8:]}",
            "role": None,
            "action": "MANAGE_TOKEN",
            "kind": "INSTANCE",
            "environment": None,
            "namespace": None,
            "table": None,
            "note": "no effect: MANAGE_TOKEN needs the ADMIN role",
            **more,
        }
    )


@pytest.mark.parametrize("width", [80, 100, 120])
@pytest.mark.parametrize("marked", [1, 5, 6, 8])
def test_each_grant_without_effect_gets_a_line_to_copy_with_its_whole_id(
    tmp_path: Path, width: int, marked: int
) -> None:
    """The table may leave `id` out, so the command to revoke is written out, five at most."""
    grants = [_grant(note=None), *(_stale(number) for number in range(1, marked + 1))]
    grants[1] = _stale(1, principal_id=None, group_id=_GID, action="MANAGE_GROUP")
    out = _render(tmp_path, _Listing(("grant", "list"), "/api/v1/grants", grants), width)
    lines = out.splitlines()
    after = lines[lines.index(_NO_EFFECT_LINE) + 1 :]
    expected = [f"MANAGE_GROUP, group {_GID}:", f"  lakana grant revoke 00000001{_ID[8:]}"]
    for number in range(2, min(marked, 5) + 1):
        expected += [
            f"MANAGE_TOKEN, principal {_ID2}:",
            f"  lakana grant revoke {number:08d}{_ID[8:]}",
        ]
    if marked > 5:
        expected.append(f"and {marked - 5} more: use --output json")
    assert after == expected, out


def test_a_line_to_copy_shows_what_the_server_sent_as_text_and_quotes_it(tmp_path: Path) -> None:
    hostile = _stale(1, id="x\x1b[2J; rm -rf ~", action="A\nB", principal_id="\u202ep")
    out = _render(tmp_path, _Listing(("grant", "list"), "/api/v1/grants", [hostile]), 120)
    assert out.splitlines()[-2:] == [
        "A\\nB, principal \\u202ep:",
        "  lakana grant revoke 'x\\x1b[2J; rm -rf ~'",
    ]
    assert "\x1b" not in out and "\u202e" not in out


def test_no_explaining_line_without_a_marker_or_in_json(tmp_path: Path) -> None:
    """The line is for the table only, and only when a row carries the marker."""
    plain = _Listing(("grant", "list"), "/api/v1/grants", [_grant(note=None)])
    assert "no effect" not in _render(tmp_path, plain, 120)
    stale = _Listing(
        ("--output", "json", "grant", "list"),
        "/api/v1/grants",
        [_grant(note="no effect: MANAGE_GRANT needs the ADMIN role")],
    )
    out = _render(tmp_path, stale, 120)
    assert "no effect: MANAGE_GRANT needs the ADMIN role" in out and _NO_EFFECT_LINE not in out
    assert "lakana grant revoke" not in out
    assert _NO_EFFECT_LINE not in _render(tmp_path, plain, 120)


def test_a_package_index_url_stays_on_one_line_while_the_name_wraps(tmp_path: Path) -> None:
    """The URL is what an administrator copies: the long name gives way, not the URL."""
    url = "https://pkgs.example-org.internal/internal/api/pypi/team/simple"
    item = {**_LISTINGS["package indexes"].items[0], "name": "team-" + "p" * 40}
    listing = _Listing(("index", "list"), "/api/v1/package-indexes", [item])
    out = _render(tmp_path, listing, 100)
    assert sum(url in line for line in out.splitlines()) == 1, out


@pytest.mark.parametrize("name", ["grants", "row filters", "column masks"])
def test_the_json_of_a_grant_or_policy_listing_keeps_both_id_fields(
    tmp_path: Path, name: str
) -> None:
    """Only the table has a `subject`; JSON still carries `principal_id` and `group_id`."""
    listing = _LISTINGS[name]
    api = FakeApi()
    api.on("GET", listing.path, json=listing.items)
    result = invoke(make_state(tmp_path, api), "--output", "json", *listing.argv)
    assert result.exit_code == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [(r["principal_id"], r["group_id"]) for r in rows] == [
        (item["principal_id"], item["group_id"]) for item in listing.items
    ]
    assert all("subject" not in row for row in rows)


@pytest.mark.parametrize("name", ["row filters", "column masks"])
def test_a_policy_listing_keeps_bound_when_there_is_room_and_drops_it_last(
    tmp_path: Path, name: str
) -> None:
    """`bound` shows at 220 columns; at 80 it is the last column left out, and the hint says so."""
    wide = _render(tmp_path, _LISTINGS[name], 220)
    narrow = _render(tmp_path, _LISTINGS[name], 80)

    assert "bound" in wide.splitlines()[1]
    assert "Not shown" not in wide
    hint = next(line for line in narrow.splitlines() if line.startswith("Not shown"))
    assert hint.startswith("Not shown: environment, id, namespace")
