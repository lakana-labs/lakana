"""Tests for `lakana env *`."""

from __future__ import annotations

import json
import re
from pathlib import Path

from conftest import FakeApi, invoke, make_state

_DEV = {
    "name": "dev",
    "storage_root": "file:///data",
    "storage_secret_ref": None,
    "is_protected": False,
    "properties": {},
}


def test_env_create_derives_the_scheme_and_sends_options(tmp_path: Path) -> None:
    """`env create` builds the storage payload from `--storage-root` and `--option`."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments", status=201, json=_DEV)

    result = invoke(
        make_state(tmp_path, api),
        "env",
        "create",
        "dev",
        "--storage-root",
        "s3://bucket/lake",
        "--option",
        "region=eu-west-1",
    )

    assert result.exit_code == 0, result.stderr
    assert "Created environment dev" in result.stdout
    assert json.loads(api.requests[0].content)["storage"] == {
        "scheme": "s3",
        "root": "s3://bucket/lake",
        "options": {"region": "eu-west-1"},
    }


def test_env_create_refuses_a_root_without_a_supported_scheme(tmp_path: Path) -> None:
    """A bare path or an unknown scheme is a usage error, before any request."""
    api = FakeApi()

    bare = invoke(make_state(tmp_path, api), "env", "create", "dev", "--storage-root", "/data")
    odd = invoke(make_state(tmp_path, api), "env", "create", "dev", "--storage-root", "gs://b")

    assert (bare.exit_code, odd.exit_code) == (2, 2)
    assert "file://" in bare.stderr
    assert api.requests == []


def test_env_list_and_get(tmp_path: Path) -> None:
    """`env list` renders a table; `env get` a single environment."""
    api = FakeApi()
    api.on("GET", "/api/v1/environments", json=[_DEV])
    api.on("GET", "/api/v1/environments/dev", json=_DEV)

    listed = invoke(make_state(tmp_path, api), "env", "list")
    got = invoke(make_state(tmp_path, api), "--output", "json", "env", "get", "dev")

    assert listed.exit_code == 0, listed.stderr
    assert "file:///data" in listed.stdout
    assert json.loads(got.stdout) == {
        **_DEV,
        "retention_days": None,
        "deleted_at": None,
        "purge_after": None,
    }


_DELETED = {
    **_DEV,
    "retention_days": None,
    "deleted_at": "2026-10-20T10:00:00+00:00",
    "purge_after": "2026-10-27T10:00:00+00:00",
}


def test_env_delete_requires_confirmation(tmp_path: Path) -> None:
    """Without a TTY, `env delete` refuses unless `--yes` is given."""
    api = FakeApi()
    api.on("DELETE", "/api/v1/environments/dev", json=_DELETED)

    refused = invoke(make_state(tmp_path, api), "env", "delete", "dev")
    deleted = invoke(make_state(tmp_path, api), "env", "delete", "dev", "--yes")

    assert refused.exit_code == 2
    assert deleted.exit_code == 0, deleted.stderr
    assert "lakana env restore dev" in deleted.stdout
    assert "2026-10-27" in deleted.stdout
    assert [r.method for r in api.requests] == ["DELETE"]


def test_env_delete_purge_asks_differently_and_sends_purge(tmp_path: Path) -> None:
    """`--purge` says it cannot be undone, and sends `purge=true`."""
    api = FakeApi()
    api.on("DELETE", "/api/v1/environments/dev", json=_DELETED)

    asked = invoke(make_state(tmp_path, api), "env", "delete", "dev", "--purge")
    deleted = invoke(make_state(tmp_path, api), "env", "delete", "dev", "--purge", "--yes")

    assert asked.exit_code == 2
    assert "cannot be undone" in asked.stderr
    assert deleted.exit_code == 0, deleted.stderr
    assert api.requests[-1].url.params["purge"] == "true"
    assert "cannot be restored" in deleted.stdout


def test_env_delete_against_an_older_server_still_succeeds(tmp_path: Path) -> None:
    """A 204 without a body: the message says nothing about an undo period."""
    api = FakeApi()
    api.on("DELETE", "/api/v1/environments/dev", status=204)

    deleted = invoke(make_state(tmp_path, api), "env", "delete", "dev", "--yes")

    assert deleted.exit_code == 0, deleted.stderr
    assert "Deleted environment dev" in deleted.stdout


def test_env_restore_and_list_deleted(tmp_path: Path) -> None:
    """`env restore` POSTs `/restore`; `env list --deleted` shows until when."""
    api = FakeApi()
    api.on("POST", "/api/v1/environments/dev/restore", json={**_DELETED, "deleted_at": None})
    api.on("GET", "/api/v1/environments", json=[_DELETED])

    restored = invoke(make_state(tmp_path, api), "env", "restore", "dev")
    listed = invoke(make_state(tmp_path, api), "env", "list", "--deleted")

    assert restored.exit_code == 0, restored.stderr
    assert "Restored environment dev" in restored.stdout
    assert listed.exit_code == 0, listed.stderr
    assert "purge_after" in listed.stdout and "2026-10-27" in listed.stdout
    assert api.requests[-1].url.params["deleted"] == "true"


def test_env_set_storage_needs_something_to_change(tmp_path: Path) -> None:
    """`set-storage` with neither `--option` nor `--secret-ref` is a usage error."""
    assert invoke(make_state(tmp_path, FakeApi()), "env", "set-storage", "dev").exit_code == 2


def test_env_set_storage_sends_options_and_secret_ref(tmp_path: Path) -> None:
    """`set-storage` forwards both fields to `update_storage`.

    `environments.update_storage` sends `PATCH /api/v1/environments/{name}/storage`
    (see `packages/lakana/src/lakana/_routes.py`), matching the plan's guess.
    """
    api = FakeApi()
    api.on(
        "PATCH", "/api/v1/environments/dev/storage", json={**_DEV, "storage_secret_ref": "creds"}
    )

    result = invoke(
        make_state(tmp_path, api),
        "env",
        "set-storage",
        "dev",
        "--option",
        "a=b",
        "--secret-ref",
        "creds",
    )

    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[0].content) == {"options": {"a": "b"}, "secret_ref": "creds"}


def test_env_protect_and_unprotect(tmp_path: Path) -> None:
    """`protect` needs no confirmation; `unprotect` does.

    `environments.set_protection` sends `PATCH /api/v1/environments/{name}`
    with `{"is_protected": ...}` (see `_routes.py`), not the plan's guessed
    `PUT .../protection`.
    """
    api = FakeApi()
    api.on("PATCH", "/api/v1/environments/dev", json={**_DEV, "is_protected": True})

    protect = invoke(make_state(tmp_path, api), "env", "protect", "dev")
    unprotect_refused = invoke(make_state(tmp_path, api), "env", "unprotect", "dev")
    unprotect = invoke(make_state(tmp_path, api), "env", "unprotect", "dev", "--yes")

    assert protect.exit_code == 0, protect.stderr
    assert unprotect_refused.exit_code == 2
    assert unprotect.exit_code == 0, unprotect.stderr
    assert [json.loads(r.content) for r in api.requests] == [
        {"is_protected": True},
        {"is_protected": False},
    ]


def test_env_create_with_a_malformed_option_never_echoes_it(tmp_path: Path) -> None:
    """Final review minor 9: a storage `--option` may hold a credential; never echo it."""
    api = FakeApi()
    state = make_state(tmp_path, api)

    for option in ("AKIAHIDDEN", "=AKIAHIDDEN"):
        result = invoke(
            state, "env", "create", "dev", "--storage-root", "s3://b/l", "--option", option
        )
        assert result.exit_code == 2
        assert "AKIAHIDDEN" not in result.stdout + result.stderr
        assert "--option #1 expects key=value" in result.stderr
    assert api.requests == []


def test_env_set_storage_with_a_malformed_option_never_echoes_it(tmp_path: Path) -> None:
    """Final review minor 9: the same protection applies to `env set-storage`."""
    api = FakeApi()

    result = invoke(
        make_state(tmp_path, api),
        "env",
        "set-storage",
        "dev",
        "--option",
        "a=b",
        "--option",
        "AKIAHIDDEN",
    )

    assert result.exit_code == 2
    assert "AKIAHIDDEN" not in result.stdout + result.stderr
    assert "--option #2 expects key=value" in result.stderr
    assert api.requests == []


def test_env_option_help_warns_about_shell_history(tmp_path: Path) -> None:
    """Final review minor 9: `--option` help steers credentials to `--secret-ref`."""
    for command in ("create", "set-storage"):
        result = invoke(make_state(tmp_path, FakeApi()), "env", command, "--help")
        # CI forces colour and wraps the help box: drop ANSI codes and the box's
        # side borders before joining the wrapped lines.
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).replace("│", " ")
        text = " ".join(plain.split())
        assert "Values land in shell history; prefer --secret-ref for credentials." in text


def _help(tmp_path: Path, command: str) -> str:
    """Return `lakana env COMMAND --help` as plain text, whitespace collapsed.

    Parameters
    ----------
    tmp_path
        Profile directory.
    command
        The `env` subcommand.

    Returns
    -------
    str
        The help text.
    """
    result = invoke(make_state(tmp_path, FakeApi()), "env", command, "--help")
    assert result.exit_code == 0, result.stderr
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).replace("│", " ")
    return " ".join(plain.split())


def test_env_protect_help_says_it_refuses_writes_outside_runs(tmp_path: Path) -> None:
    """I2: protection now also locks writes down, not only deletion and storage changes."""
    text = _help(tmp_path, "protect")
    assert "refuses every write outside runs of the deployed code" in text
    assert "--break-glass" in text
    assert "deletion and storage changes" in text


def test_env_delete_help_says_to_undeploy_first(tmp_path: Path) -> None:
    """I2: a deployed repository blocks deletion (409); the help says what to do."""
    text = _help(tmp_path, "delete")
    assert "Refused (409) while any repository is deployed to it" in text
    assert "lakana undeploy REPO --env NAME" in text
    assert "lakana env restore NAME" in text


def _orphan(location: str, **fields: object) -> dict[str, object]:
    """Build one directory of an orphaned-locations response.

    Parameters
    ----------
    location
        Its root-relative path.
    **fields
        Fields that differ from an old directory that a listing found.

    Returns
    -------
    dict
        The JSON object.
    """
    return {
        "location": location,
        "files": 2,
        "bytes": 2048,
        "newest_file_at": "2026-09-01T10:00:00+00:00",
        "recent": False,
        "deleted": False,
        "files_deleted": 0,
        "bytes_deleted": 0,
        "kept_because": None,
        **fields,
    }


def _orphans(*locations: dict[str, object], complete: bool = True) -> dict[str, object]:
    """Build an orphaned-locations response for `dev`.

    Parameters
    ----------
    *locations
        Its directories.
    complete
        Whether the server finished.

    Returns
    -------
    dict
        The JSON body.
    """
    return {
        "environment": "dev",
        "older_than_days": 30,
        "locations": list(locations),
        "complete": complete,
    }


_LISTING = "/api/v1/environments/dev/orphaned-locations"
_OLD = _orphan("managed/gold/orders-0123")
_RECENT = _orphan("managed/gold/fresh-0123", recent=True, files=1, bytes=10)


def test_env_orphaned_locations_is_a_dry_run_that_names_the_delete_command(
    tmp_path: Path,
) -> None:
    """A table of what is there, a total line, and the command that deletes."""
    api = FakeApi()
    api.on("GET", _LISTING, json=_orphans(_OLD, _RECENT))

    result = invoke(make_state(tmp_path, api), "env", "orphaned-locations", "dev")

    assert result.exit_code == 0, result.stderr
    assert "managed/gold/orders-0123" in result.stdout
    assert "newest_file_at" in result.stdout
    flat = " ".join(result.stdout.split())
    assert (
        "2 directories without a location record (3 files, 2.0 KiB). 1 has a file newer than "
        "30 days and is kept. Delete the other 1 (2 files, 2.0 KiB) with: lakana env "
        "orphaned-locations dev --older-than 30d --delete"
    ) in flat
    assert [(r.method, str(r.url.params)) for r in api.requests] == [("GET", "older_than_days=30")]


def test_env_orphaned_locations_counts_in_words(tmp_path: Path) -> None:
    """One directory, several recent ones."""
    api = FakeApi()
    api.on("GET", _LISTING, json=_orphans(_RECENT, _RECENT))
    flat = " ".join(
        invoke(make_state(tmp_path, api), "env", "orphaned-locations", "dev").stdout.split()
    )
    assert "2 have a file newer than 30 days and are kept. Nothing is old enough" in flat
    api.on("GET", _LISTING, json=_orphans(_OLD))
    flat = " ".join(
        invoke(make_state(tmp_path, api), "env", "orphaned-locations", "dev").stdout.split()
    )
    assert "1 directory without a location record (2 files, 2.0 KiB). Delete them with:" in flat


def test_env_orphaned_locations_with_nothing_found_says_so(tmp_path: Path) -> None:
    """No table, no command to run."""
    api = FakeApi()
    api.on("GET", _LISTING, json=_orphans())
    result = invoke(make_state(tmp_path, api), "env", "orphaned-locations", "dev")
    assert result.exit_code == 0, result.stderr
    assert "No table directories without a location record in dev's storage." in result.stdout
    assert "--delete" not in result.stdout


def test_env_orphaned_locations_says_when_the_listing_was_cut(tmp_path: Path) -> None:
    """The server's time limit: the reader is told to run it again."""
    api = FakeApi()
    api.on("GET", _LISTING, json=_orphans(_RECENT, complete=False))
    result = invoke(make_state(tmp_path, api), "env", "orphaned-locations", "dev")
    assert result.exit_code == 0, result.stderr
    assert "Nothing is old enough to delete" in " ".join(result.stdout.split())
    assert "time limit" in result.stderr


def test_env_orphaned_locations_delete_asks_first(tmp_path: Path) -> None:
    """Without a terminal and without `--yes`, nothing is deleted."""
    api = FakeApi()
    api.on("GET", _LISTING, json=_orphans(_OLD))
    refused = invoke(make_state(tmp_path, api), "env", "orphaned-locations", "dev", "--delete")
    assert refused.exit_code == 2
    assert "cannot be undone" in refused.stderr and "--yes" in refused.stderr
    assert [r.method for r in api.requests] == ["GET"]


def test_env_orphaned_locations_delete_with_a_confirmation(tmp_path: Path) -> None:
    """Answering yes at the prompt deletes; the age goes in the body."""
    api = FakeApi()
    api.on("GET", _LISTING, json=_orphans(_OLD))
    api.on(
        "POST",
        f"{_LISTING}/delete",
        json=_orphans(
            _orphan("managed/gold/orders-0123", deleted=True, files_deleted=2, bytes_deleted=2048)
        ),
    )
    result = invoke(
        make_state(tmp_path, api, interactive=True),
        "env",
        "orphaned-locations",
        "dev",
        "--older-than",
        "45d",
        "--delete",
        input="y\n",
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(api.requests[-1].content) == {"older_than_days": 45}
    assert api.requests[0].url.params["older_than_days"] == "45"
    assert "Deleted 1 directory (2 files, 2.0 KiB)" in " ".join(result.stdout.split())


def test_env_orphaned_locations_delete_yes_reports_what_was_kept(tmp_path: Path) -> None:
    """`--yes` skips the question; a kept directory shows why, and a cut run says to rerun."""
    api = FakeApi()
    api.on("GET", _LISTING, json=_orphans(_OLD))
    api.on(
        "POST",
        f"{_LISTING}/delete",
        json=_orphans(
            _orphan("managed/gold/orders-0123", kept_because="a create claimed it"),
            complete=False,
        ),
    )
    result = invoke(
        make_state(tmp_path, api), "env", "orphaned-locations", "dev", "--delete", "--yes"
    )
    assert result.exit_code == 0, result.stderr
    flat = " ".join(result.stdout.split())
    assert "a create claimed it" in flat
    assert "Deleted 0 directories (0 files, 0 B); 1 kept." in flat
    assert "run the same command again" in result.stderr


def test_env_orphaned_locations_delete_with_nothing_old_enough_sends_nothing(
    tmp_path: Path,
) -> None:
    """Only recent directories: no question and no deletion."""
    api = FakeApi()
    api.on("GET", _LISTING, json=_orphans(_RECENT))
    result = invoke(
        make_state(tmp_path, api), "env", "orphaned-locations", "dev", "--delete", "--yes"
    )
    assert result.exit_code == 0, result.stderr
    assert "Nothing is old enough to delete" in " ".join(result.stdout.split())
    assert [r.method for r in api.requests] == ["GET"]


def test_env_orphaned_locations_json(tmp_path: Path) -> None:
    """`--output json` prints the report as the server sent it."""
    api = FakeApi()
    api.on("GET", _LISTING, json=_orphans(_OLD))
    result = invoke(
        make_state(tmp_path, api), "--output", "json", "env", "orphaned-locations", "dev"
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == _orphans(_OLD)


def test_env_orphaned_locations_refuses_a_bad_age(tmp_path: Path) -> None:
    """The age is days, written like 30d."""
    result = invoke(
        make_state(tmp_path, FakeApi()), "env", "orphaned-locations", "dev", "--older-than", "1w"
    )
    assert result.exit_code == 2
    assert "--older-than takes 1 to 3650 days, written like 30d or 30" in result.stderr


def test_env_orphaned_locations_delete_json(tmp_path: Path) -> None:
    """`--output json --delete --yes` prints only the deletion's report."""
    api = FakeApi()
    api.on("GET", _LISTING, json=_orphans(_OLD))
    gone = _orphans(_orphan("managed/gold/orders-0123", deleted=True))
    api.on("POST", f"{_LISTING}/delete", json=gone)
    result = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "env",
        "orphaned-locations",
        "dev",
        "--delete",
        "--yes",
    )
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == gone


def test_env_orphaned_locations_shows_why_a_directory_is_kept(tmp_path: Path) -> None:
    """A directory the server keeps for a reason is shown with it and not offered for deletion."""
    api = FakeApi()
    named = _orphan("managed/gold/odd-0123", kept_because="a location cannot be compared: x//y")
    api.on("GET", _LISTING, json=_orphans(_OLD, named))
    result = invoke(make_state(tmp_path, api), "env", "orphaned-locations", "dev")
    assert result.exit_code == 0, result.stderr
    flat = " ".join(result.stdout.split())
    assert "kept_because" in result.stdout and "x//y" in flat
    assert "1 is kept for the reason shown. Delete the other 1 (2 files, 2.0 KiB)" in flat


# -- `lakana env usage` ------------------------------------------


def _usage(name: str = "dev", *, unmeasured: int = 0) -> dict[str, object]:
    """Build a usage response.

    Parameters
    ----------
    name
        The environment.
    unmeasured
        How many tables have no current size.

    Returns
    -------
    dict
        Two tables of 3 GiB together, one dropped table of 1 KiB, an artifact of 2 MiB and two
        code archives of 512 bytes.
    """
    tables = {"count": 2, "bytes": 3 * 1024**3, "unmeasured": unmeasured}
    return {
        "environment": name,
        "tables": tables,
        "dropped_tables": {"count": 1, "bytes": 1024, "unmeasured": 0},
        "artifacts": {"count": 1, "bytes": 2 * 1024**2, "unmeasured": 0},
        "code_archives": {"count": 2, "bytes": 512, "unmeasured": 0},
        "total_bytes": 3 * 1024**3 + 1024 + 2 * 1024**2 + 512,
        "complete": unmeasured == 0,
    }


def _measured_worker(worker_id: str = "box:1:ab", **caches: object) -> dict[str, object]:
    """Build a worker row with cache sizes.

    Parameters
    ----------
    worker_id
        Its id.
    **caches
        Fields of `caches` that differ from 1 GiB of environments, 5 GiB of packages, none of
        tasks, complete; `caches=None` for a worker that has not measured yet.

    Returns
    -------
    dict
        The row.
    """
    measured: object = {
        "environments_bytes": 1024**3,
        "packages_bytes": 5 * 1024**3,
        "tasks_bytes": 0,
        "complete": True,
        "measured_at": "2026-10-21T08:00:00+00:00",
    }
    measured = caches.pop("caches") if "caches" in caches else {**measured, **caches}  # type: ignore[dict-item]
    return {
        "id": worker_id,
        "host": "box",
        "version": "0.17.0",
        "started_at": "2026-10-21T07:00:00+00:00",
        "heartbeat_at": "2026-10-21T08:00:00+00:00",
        "status": "alive",
        "takes": "all",
        "query_run_id": None,
        "deploy_run_id": None,
        "task_run_id": None,
        "caches": measured,
    }


def test_env_usage_ignores_a_field_of_a_part_it_does_not_know(tmp_path: Path) -> None:
    usage = _usage()
    usage["tables"] = {"count": 2, "bytes": 3 * 1024**3, "unmeasured": 0, "added_later": 1}
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/usage", json=usage)
    api.on("GET", "/api/v1/workers", json=[])
    result = invoke(make_state(tmp_path, api), "env", "usage", "dev", env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stderr
    assert "3.0 GiB in 2" in result.stdout


def test_env_usage_of_one_environment_and_the_workers_caches(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/usage", json=_usage(unmeasured=1))
    api.on(
        "GET",
        "/api/v1/workers",
        json=[_measured_worker(), _measured_worker("box:2:cd", caches=None)],
    )
    result = invoke(make_state(tmp_path, api), "env", "usage", "dev", env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    assert re.search(
        r"dev\s+│ 3\.0 GiB in 2, 1 unmeasured\s+│ 1\.0 KiB in 1\s+│ 2\.0 MiB in 1", out
    )
    assert "3.0 GiB+" in out
    assert "not measured yet" in out
    assert re.search(r"box:1:ab\s+│ 1\.0 GiB\s+│ 5\.0 GiB\s+│ 0 B\s+│ 6\.0 GiB", out)
    assert re.search(r"box:2:cd\s+│ -\s+│ -\s+│ -\s+│ -\s+│ not measured yet", out)
    assert "share one cache volume" in out


def test_env_usage_without_a_name_shows_each_environment_you_may_manage(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "GET",
        "/api/v1/environments",
        json=[_DEV, {**_DEV, "name": "prod"}, {**_DEV, "name": "staging"}],
    )
    api.on("GET", "/api/v1/environments/dev/usage", json=_usage())
    api.on("GET", "/api/v1/environments/prod/usage", status=403, json={"detail": "No."})
    api.on("GET", "/api/v1/environments/staging/usage", json=_usage("staging"))
    api.on("GET", "/api/v1/workers", status=403, json={"detail": "No."})
    result = invoke(make_state(tmp_path, api), "env", "usage", env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stderr
    assert "staging" in result.stdout and "dev" in result.stdout
    assert "1 environment not shown (prod)" in result.stdout
    assert "The workers' caches are shown to administrators of the instance" in result.stdout


def test_env_usage_with_no_environment_you_may_manage_says_so(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/environments", json=[_DEV])
    api.on("GET", "/api/v1/environments/dev/usage", status=403, json={"detail": "No."})
    result = invoke(make_state(tmp_path, api), "env", "usage")
    assert result.exit_code != 0
    assert "MANAGE_ENVIRONMENT" in result.stderr


def test_env_usage_as_json_has_the_environments_and_the_workers(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/usage", json=_usage())
    api.on("GET", "/api/v1/workers", json=[_measured_worker()])
    result = invoke(make_state(tmp_path, api), "--output", "json", "env", "usage", "dev")
    assert result.exit_code == 0, result.stderr
    body = json.loads(result.stdout)
    assert body["environments"][0]["tables"] == {"count": 2, "bytes": 3 * 1024**3, "unmeasured": 0}
    assert body["environments"][0]["complete"] is True
    assert body["workers"][0]["id"] == "box:1:ab"
    assert body["workers"][0]["caches"]["packages_bytes"] == 5 * 1024**3


def test_env_usage_of_an_environment_you_may_not_manage_is_refused(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/usage", status=403, json={"detail": "Not allowed."})
    result = invoke(make_state(tmp_path, api), "env", "usage", "dev")
    assert result.exit_code != 0
    assert "Not allowed." in result.stderr


def test_env_usage_without_a_worker_says_no_cache_is_shown(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/usage", json=_usage())
    api.on("GET", "/api/v1/workers", json=[])
    result = invoke(make_state(tmp_path, api), "env", "usage", "dev")
    assert result.exit_code == 0, result.stderr
    assert "No worker is registered, so no cache is shown." in result.stdout
