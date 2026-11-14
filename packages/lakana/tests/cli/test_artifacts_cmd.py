"""Tests for `lakana artifact *`."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest
from conftest import FakeApi, invoke, make_state
from rich.console import Console

from lakana._artifact_manifest import manifest_digest
from lakana.cli.artifacts import human_size, upload_summary

_BASE = "/api/v1/environments/dev/namespaces/models/artifacts/churn"
_VERSION = {
    "version": 1,
    "manifest_sha256": "a" * 64,
    "size_bytes": 19,
    "file_count": 2,
    "is_directory": True,
    "task_run_id": None,
    "run_id": None,
    "project_version_id": None,
    "commit_sha": None,
    "created_by": "admin",
    "created_at": "2026-10-04T12:00:00Z",
    "deleted_at": None,
    "deleted_by": None,
    "files": [],
    "deleted_versions": [],
}


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 B"),
        (19, "19 B"),
        (1023, "1023 B"),
        (1536, "1.5 KiB"),
        (2 * 1024**2, "2.0 MiB"),
        (int(3.2 * 1024**3), "3.2 GiB"),
        (5000 * 1024**3, "5000.0 GiB"),
    ],
)
def test_human_size(size: int, text: str) -> None:
    assert human_size(size) == text


def test_artifact_upload_stores_a_version_and_says_so(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.txt").write_bytes(b"a")
    api = FakeApi()
    api.on(
        "POST",
        f"{_BASE}/uploads",
        status=201,
        json={"upload_id": "u-1", "missing": [], "expires_at": "2026-10-04T13:00:00Z"},
    )
    api.on("POST", f"{_BASE}/uploads/u-1/commit", status=201, json=_VERSION)
    result = invoke(
        make_state(tmp_path, api), "artifact", "upload", "models.churn", str(out), "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    assert "Stored dev/models.churn version 1 (2 files, 19 B)" in result.stdout
    as_json = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "artifact",
        "upload",
        "models.churn",
        str(out),
        "--env",
        "dev",
    )
    assert json.loads(as_json.stdout)["version"] == 1


def test_artifact_upload_reports_a_bad_tree_with_its_path(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "leak").symlink_to(tmp_path)
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api), "artifact", "upload", "models.churn", str(out), "--env", "dev"
    )
    assert result.exit_code != 0
    assert "leak is a symbolic link" in result.stderr
    assert [r.url.path for r in api.requests] == ["/api/v1/info"]  # only the limits are asked


def test_artifact_upload_passes_a_break_glass_reason(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = FakeApi()
    api.on(
        "POST",
        f"{_BASE}/uploads",
        status=201,
        json={"upload_id": "u-1", "missing": [], "expires_at": "2026-10-04T13:00:00Z"},
    )
    api.on("POST", f"{_BASE}/uploads/u-1/commit", status=201, json=_VERSION)
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "upload",
        "models.churn",
        str(tmp_path / "m.json"),
        "--env",
        "prod",
        "--break-glass",
        "--reason",
        "replace a broken model",
    )
    assert result.exit_code != 0  # the fake has no route for prod: proves the environment is used
    api.on(
        "POST",
        f"{_BASE.replace('/dev/', '/prod/')}/uploads",
        status=201,
        json={"upload_id": "u-1", "missing": [], "expires_at": "2026-10-04T13:00:00Z"},
    )
    api.on(
        "POST", f"{_BASE.replace('/dev/', '/prod/')}/uploads/u-1/commit", status=201, json=_VERSION
    )
    again = invoke(
        make_state(tmp_path, api),
        "artifact",
        "upload",
        "models.churn",
        str(tmp_path / "m.json"),
        "--env",
        "prod",
        "--break-glass",
        "--reason",
        "replace a broken model",
    )
    assert again.exit_code == 0, again.stderr
    assert api.requests[-1].url.params["break_glass_reason"] == "replace a broken model"


def _api_for(missing: list[str] | None = None) -> FakeApi:
    """Build a fake that accepts one upload and its commit.

    Parameters
    ----------
    missing
        The hashes the server says it lacks.

    Returns
    -------
    FakeApi
        The fake.
    """
    api = FakeApi()
    api.on(
        "POST",
        f"{_BASE}/uploads",
        status=201,
        json={"upload_id": "u-1", "missing": missing or [], "expires_at": "2026-10-04T13:00:00Z"},
    )
    return api


def test_the_summary_says_what_was_sent_and_what_the_store_had(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.txt").write_bytes(b"a")
    (out / "b.txt").write_bytes(b"b")
    api = _api_for([hashlib.sha256(b"a").hexdigest()])
    api.on("PUT", f"{_BASE}/uploads/u-1/blobs/{hashlib.sha256(b'a').hexdigest()}", json={})
    api.on("POST", f"{_BASE}/uploads/u-1/commit", status=201, json={**_VERSION, "version": 7})
    result = invoke(
        make_state(tmp_path, api), "artifact", "upload", "models.churn", str(out), "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        "Stored dev/models.churn version 7 (2 files, 19 B)",
        "Sent 1 file; 1 already stored.",
    ]


def test_the_summary_says_when_every_file_was_new_and_when_versions_were_pruned(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    digest = hashlib.sha256(b"m").hexdigest()
    api = _api_for([digest])
    api.on("PUT", f"{_BASE}/uploads/u-1/blobs/{digest}", json={})
    api.on(
        "POST",
        f"{_BASE}/uploads/u-1/commit",
        status=201,
        json={**_VERSION, "file_count": 1, "deleted_versions": [2, 4]},
    )
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "upload",
        "models.churn",
        str(tmp_path / "m.json"),
        "--env",
        "dev",
    )
    assert result.stdout.splitlines() == [
        "Stored dev/models.churn version 1 (1 file, 19 B)",
        "Sent 1 file; none were already stored.",
        "Pruned old versions: 2, 4.",
    ]


def test_a_commit_whose_answer_is_lost_exits_with_the_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lakana.resources.artifacts._wait", lambda _seconds: None)
    (tmp_path / "m.json").write_bytes(b"m")
    api = _api_for()

    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    api.routes["POST", f"{_BASE}/uploads/u-1/commit"] = lost
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "upload",
        "models.churn",
        str(tmp_path / "m.json"),
        "--env",
        "dev",
    )
    assert result.exit_code == 11
    assert "did not confirm the commit of models.churn (upload u-1)" in result.stderr
    assert "lakana --output json artifact versions list models.churn --env dev" in result.stderr
    assert "manifest_sha256 starting " in result.stderr


def test_an_upload_without_a_dotted_name_names_the_form(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    result = invoke(
        make_state(tmp_path, FakeApi()),
        "artifact",
        "upload",
        "churn",
        str(tmp_path / "m.json"),
        "--env",
        "dev",
    )
    assert result.exit_code == 8
    assert "Expected a dotted 'namespace.table' name" in result.stderr


def test_a_missing_path_exits_with_the_path_and_the_next_step(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "upload",
        "models.churn",
        str(tmp_path / "nope"),
        "--env",
        "dev",
    )
    assert result.exit_code == 8
    assert "nope does not exist. Check the path" in result.stderr
    assert [r.url.path for r in api.requests] == ["/api/v1/info"]


def test_a_version_that_was_not_uploaded_has_no_counts_to_report() -> None:
    from lakana._models import ArtifactVersion

    read = ArtifactVersion(
        version=4,
        manifest_sha256="a" * 64,
        size_bytes=1536,
        file_count=1,
        is_directory=False,
        task_run_id=None,
        run_id=None,
        project_version_id=None,
        commit_sha=None,
        created_by=None,
        created_at="2026-10-04T12:00:00Z",
        deleted_at=None,
        deleted_by=None,
    )
    assert upload_summary("dev", "models.churn", read) == (
        "Stored dev/models.churn version 4 (1 file, 1.5 KiB)"
    )


_ARTIFACT = {
    "name": "churn",
    "namespace": "models",
    "uuid": "u",
    "kind": "ARTIFACT",
    "current_version": 2,
    "versions": 2,
    "size_bytes": 2048,
    "created_at": "2026-10-04T12:00:00Z",
}


def test_artifact_list_shows_sizes_for_people(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", "/api/v1/environments/dev/artifacts", json=[_ARTIFACT])
    result = invoke(
        make_state(tmp_path, api), "artifact", "list", "--env", "dev", "--namespace", "models"
    )
    assert result.exit_code == 0, result.stderr
    assert "churn" in result.stdout
    assert "2.0 KiB" in result.stdout
    assert api.requests[0].url.params["namespace"] == "models"
    as_json = invoke(
        make_state(tmp_path, api), "--output", "json", "artifact", "list", "--env", "dev"
    )
    assert json.loads(as_json.stdout)[0]["size_bytes"] == 2048
    api.on("GET", "/api/v1/environments/dev/artifacts", json=[])
    empty = invoke(make_state(tmp_path, api), "artifact", "list", "--env", "dev")
    assert "(none)" in empty.stdout


_WIDE_RUN = "3f2c9d1e-8b7a-4c55-9e0a-1b2c3d4e5f60"
_WIDE_MADE = "2026-10-04T12:00:00.123456Z"
_WIDE_GONE = "2026-10-05T13:30:45.654321Z"


def _wide_versions(tmp_path: Path, columns: int, *, both: bool = True) -> str:
    """List a version and, by default, a deleted one, on a terminal `columns` wide.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    columns
        The terminal width.
    both
        Also list the deleted version.

    Returns
    -------
    str
        What the command printed.
    """
    api = FakeApi()
    api.on("GET", _BASE, json=_ARTIFACT)
    live = {**_VERSION, "version": 7, "run_id": _WIDE_RUN, "created_at": _WIDE_MADE}
    gone = {**_VERSION, "version": 1, "created_at": _WIDE_MADE, "deleted_at": _WIDE_GONE}
    api.on("GET", f"{_BASE}/versions", json=[live, gone] if both else [gone])
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "versions",
        "list",
        "models.churn",
        "--env",
        "dev",
        env={"COLUMNS": str(columns)},
    )
    assert result.exit_code == 0, result.stderr
    return result.stdout


def _shown(out: str) -> list[str]:
    """Return the headers of the table in `out`.

    Parameters
    ----------
    out
        What the command printed.

    Returns
    -------
    list of str
        The column headers, left to right.
    """
    header = next(line for line in out.splitlines() if line.startswith("┃"))
    return [cell.strip() for cell in header.split("┃")[1:-1]]


@pytest.mark.parametrize(
    ("columns", "shown", "left_out"),
    [
        (80, ["version", "created_at", "manifest_sha256"], "file_count, size, run_id, deleted_at"),
        (
            120,
            ["version", "created_at", "manifest_sha256", "deleted_at"],
            "file_count, size, run_id",
        ),
        (
            140,
            ["version", "created_at", "size", "manifest_sha256", "run_id", "deleted_at"],
            "file_count",
        ),
        (
            200,
            [
                "version",
                "created_at",
                "size",
                "file_count",
                "manifest_sha256",
                "run_id",
                "deleted_at",
            ],
            None,
        ),
    ],
)
def test_versions_list_keeps_the_digest_longest_then_deleted_at_and_never_cuts_a_time(
    tmp_path: Path, columns: int, shown: list[str], left_out: str | None
) -> None:
    """Version, time, digest and run id outlast file count and size, whole and on one row."""
    out = _wide_versions(tmp_path, columns)
    assert "…" not in out
    assert _shown(out) == shown
    rows = [line for line in out.splitlines() if line.startswith("│")]
    assert len(rows) == 2
    assert all("a" * 12 in row and "a" * 13 not in row for row in rows)
    assert all(_WIDE_MADE in row for row in rows)
    if "run_id" in shown:
        assert _WIDE_RUN in rows[0]
    if "deleted_at" in shown:
        assert _WIDE_GONE in rows[1]
    hint = [line for line in out.splitlines() if line.startswith("Not shown")]
    if left_out is None:
        assert hint == []
    else:
        pronoun = "it" if "," not in left_out else "them"
        assert hint == [f"Not shown: {left_out}. Use --output json to see {pronoun}."]


def test_versions_list_tells_a_terminal_to_widen_it_and_a_pipe_only_about_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where there is no terminal to widen, the hint does not say so."""
    piped = " ".join(_wide_versions(tmp_path, 100).split())
    assert "Widen" not in piped
    assert "Use --output json to see them." in piped
    api = FakeApi()
    api.on("GET", _BASE, json=_ARTIFACT)
    api.on("GET", f"{_BASE}/versions", json=[_VERSION])
    monkeypatch.setattr(
        "lakana.cli._output._console",
        # Width and height both given: with only a width, rich takes 80 x 25 under TERM=dumb and
        # the width the test asked for only when LINES is set (as it is in a `pytest -n` worker).
        lambda stderr=False: Console(
            file=sys.stdout,
            width=80,
            height=25,
            force_terminal=True,
            color_system=None,
            highlight=False,
        ),
    )
    result = invoke(
        make_state(tmp_path, api), "artifact", "versions", "list", "models.churn", "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    printed = " ".join(result.stdout.split())
    assert "Not shown at this width: file_count, size." in printed
    assert "Widen the terminal or use --output json." in printed


def test_versions_list_never_cuts_a_value_on_a_very_narrow_terminal(tmp_path: Path) -> None:
    """Narrower than a time: the values wrap onto more lines instead of ending in an ellipsis."""
    out = _wide_versions(tmp_path, 40, both=False)
    assert "…" not in out
    cells = [line.split("│")[1:-1] for line in out.splitlines() if line.startswith("│")]
    assert "".join(cell[1].strip() for cell in cells) == "2026-10-04T12:00:00.123456Z"


def test_artifact_versions_list_ends_with_a_total_and_shows_the_digest(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json=_ARTIFACT)
    deleted = {
        **_VERSION,
        "version": 1,
        "deleted_at": "2026-10-04T13:00:00Z",
        "deleted_by": "admin",
        "manifest_sha256": "c" * 64,
    }
    # Version numbers have gaps: 7 and 1.
    api.on("GET", f"{_BASE}/versions", json=[{**_VERSION, "version": 7}, deleted])
    result = invoke(
        make_state(tmp_path, api), "artifact", "versions", "list", "models.churn", "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    out = " ".join(result.stdout.split())
    assert "Total: 1 version with bytes, 2.0 KiB" in out
    for column in ("version", "created_at", "size", "file_count", "manifest_sha256", "deleted_at"):
        assert column in out
    assert "a" * 12 in out
    assert "a" * 13 not in out
    assert "c" * 12 in out
    as_json = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "artifact",
        "versions",
        "list",
        "models.churn",
        "--env",
        "dev",
    )
    document = json.loads(as_json.stdout)
    assert (document["artifact"]["name"], [v["version"] for v in document["versions"]]) == (
        "churn",
        [7, 1],
    )
    assert document["versions"][0]["manifest_sha256"] == "a" * 64


def _readable_version(body: bytes, *, number: int = 2, is_directory: bool = False) -> dict:
    """Build the version JSON of a one-file artifact whose digest matches.

    Parameters
    ----------
    body
        The file's contents.
    number
        The version number.
    is_directory
        Whether it is a directory artifact.

    Returns
    -------
    dict
        The version JSON.
    """
    digest = hashlib.sha256(body).hexdigest()
    return {
        **_VERSION,
        "version": number,
        "is_directory": is_directory,
        "file_count": 1,
        "size_bytes": len(body),
        "files": [{"path": "model.json", "size": len(body), "sha256": digest}],
        "manifest_sha256": manifest_digest(is_directory, [("model.json", len(body), digest)]),
    }


def test_artifact_download_reports_where_it_wrote(tmp_path: Path) -> None:
    body = b"abc"
    digest = hashlib.sha256(body).hexdigest()
    version = _readable_version(body)
    api = FakeApi()
    api.on("GET", f"{_BASE}/versions/current", json=version)
    api.on("GET", f"{_BASE}/versions/2", json=version)
    api.on("GET", f"{_BASE}/versions/2/blobs/{digest}", content=body)
    target = tmp_path / "copy"
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "download",
        "models.churn",
        str(target),
        "--env",
        "dev",
    )
    assert result.exit_code == 0, result.stderr
    assert (
        f"Downloaded dev/models.churn version 2 to {target.resolve() / 'model.json'}"
        in " ".join(result.stdout.split())
    )
    assert (target / "model.json").read_bytes() == body
    pinned = invoke(
        make_state(tmp_path, api),
        "artifact",
        "download",
        "models.churn",
        str(tmp_path / "v2"),
        "--env",
        "dev",
        "--version",
        "2",
    )
    assert pinned.exit_code == 0, pinned.stderr
    as_json = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "artifact",
        "download",
        "models.churn",
        str(tmp_path / "again"),
        "--env",
        "dev",
    )
    assert json.loads(as_json.stdout)["version"] == 2


def test_artifact_download_says_what_to_do_when_it_cannot_write(tmp_path: Path) -> None:
    body = b"abc"
    digest = hashlib.sha256(body).hexdigest()
    version = _readable_version(body)
    api = FakeApi()
    api.on("GET", f"{_BASE}/versions/current", json=version)
    api.on("GET", f"{_BASE}/versions/2", json=version)
    api.on("GET", f"{_BASE}/versions/2/blobs/{digest}", content=b"abd")
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "x").write_bytes(b"x")
    taken = invoke(
        make_state(tmp_path, api),
        "artifact",
        "download",
        "models.churn",
        str(tmp_path / "taken"),
        "--env",
        "dev",
    )
    assert taken.exit_code == 8
    assert "give a new or an empty directory" in " ".join(taken.stderr.split())
    corrupt = invoke(
        make_state(tmp_path, api),
        "artifact",
        "download",
        "models.churn",
        str(tmp_path / "copy"),
        "--env",
        "dev",
    )
    assert corrupt.exit_code == 1
    assert "does not match its manifest" in " ".join(corrupt.stderr.split())
    assert "download it again" in " ".join(corrupt.stderr.split())
    assert not (tmp_path / "copy").exists()
    zero = invoke(
        make_state(tmp_path, api),
        "artifact",
        "download",
        "models.churn",
        str(tmp_path / "zero"),
        "--env",
        "dev",
        "--version",
        "0",
    )
    assert zero.exit_code == 2
    assert not (tmp_path / "zero").exists()


def test_download_asks_the_server_for_the_version_once(tmp_path: Path) -> None:
    body = b"abc"
    digest = hashlib.sha256(body).hexdigest()
    version = _readable_version(body)
    api = FakeApi()
    api.on("GET", f"{_BASE}/versions/current", json=version)
    api.on("GET", f"{_BASE}/versions/2", json=version)
    api.on("GET", f"{_BASE}/versions/2/blobs/{digest}", content=body)
    for extra, asked in (([], "current"), (["--version", "2"], "2")):
        api.requests.clear()
        result = invoke(
            make_state(tmp_path, api),
            "artifact",
            "download",
            "models.churn",
            str(tmp_path / f"copy-{asked}"),
            "--env",
            "dev",
            *extra,
        )
        assert result.exit_code == 0, result.stderr
        versions = [r.url.path for r in api.requests if "/versions/" in r.url.path]
        assert [p for p in versions if "/blobs/" not in p] == [f"{_BASE}/versions/{asked}"]


@pytest.mark.parametrize(
    ("live", "text"),
    [(0, "Total: 0 versions"), (1, "Total: 1 version with"), (2, "Total: 2 versions with")],
)
def test_the_total_of_versions_is_singular_for_one(tmp_path: Path, live: int, text: str) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json=_ARTIFACT)
    api.on(
        "GET",
        f"{_BASE}/versions",
        json=[{**_VERSION, "version": n + 1} for n in range(live)],
    )
    result = invoke(
        make_state(tmp_path, api), "artifact", "versions", "list", "models.churn", "--env", "dev"
    )
    assert result.exit_code == 0, result.stderr
    assert text in " ".join(result.stdout.split())


def test_the_help_says_what_the_commands_show_and_what_a_copy_is_like(tmp_path: Path) -> None:
    api = FakeApi()
    versions = " ".join(
        invoke(make_state(tmp_path, api), "artifact", "versions", "--help").stdout.split()
    )
    assert "which one you may download" not in versions
    assert "when each was made" in versions
    listing = " ".join(
        invoke(make_state(tmp_path, api), "artifact", "versions", "list", "--help").stdout.split()
    )
    assert "narrow terminal" in listing
    assert "lakana --output json artifact versions list NAME" in listing
    download = " ".join(
        invoke(make_state(tmp_path, api), "artifact", "download", "--help").stdout.split()
    )
    assert "--read-only" not in download
    for phrase in ("0600", "0700", "private", "chmod -R a+rX DIR"):
        assert phrase in download
    assert "empty directory is never replaced" in download or "never replaced" in download


def test_a_full_disk_exits_with_the_reason_and_not_as_an_unexpected_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = b"abc"
    digest = hashlib.sha256(body).hexdigest()
    version = _readable_version(body)
    api = FakeApi()
    api.on("GET", f"{_BASE}/versions/current", json=version)
    api.on("GET", f"{_BASE}/versions/2/blobs/{digest}", content=body)

    class Full:
        def __init__(self, inner: object) -> None:
            self.inner = inner

        def __enter__(self) -> Full:
            return self

        def __exit__(self, *exc: object) -> None:
            self.inner.close()  # ty: ignore[missing-attribute]

        def write(self, data: bytes) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

    real = os.fdopen
    monkeypatch.setattr(os, "fdopen", lambda descriptor, how: Full(real(descriptor, how)))
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "download",
        "models.churn",
        str(tmp_path / "copy"),
        "--env",
        "dev",
    )
    assert result.exit_code == 8
    assert "UnexpectedError" not in result.stderr
    assert "no space left on the disk while writing model.json" in " ".join(result.stderr.split())
    assert not (tmp_path / "copy").exists()


def test_a_body_that_cannot_be_decoded_exits_with_a_message_not_as_an_unexpected_error(
    tmp_path: Path,
) -> None:
    body = b"abc"
    digest = hashlib.sha256(body).hexdigest()
    version = _readable_version(body)
    api = FakeApi()
    api.on("GET", f"{_BASE}/versions/current", json=version)
    api.routes["GET", f"{_BASE}/versions/2/blobs/{digest}"] = lambda request: httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, content=b"not gzip"
    )
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "download",
        "models.churn",
        str(tmp_path / "copy"),
        "--env",
        "dev",
    )
    assert result.exit_code != 0
    assert "UnexpectedError" not in result.stderr
    assert "could not be decoded" in " ".join(result.stderr.split())
    assert not (tmp_path / "copy").exists()


# --- deleting a version, dropping an artifact, withdrawing an upload ---------------------------

_UPLOAD_ID = "3f2b9c1e-5a4d-4c8e-9b1a-0d6e7f8a9b0c"
_DELETED = {**_VERSION, "deleted_at": "2026-10-04T13:00:00Z", "deleted_by": "admin", "files": []}


def _flat(text: str) -> str:
    """Put wrapped output on one line.

    Parameters
    ----------
    text
        The output.

    Returns
    -------
    str
        The same words, single-spaced.
    """
    return " ".join(text.split())


def test_versions_delete_names_the_size_and_needs_confirmation(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/versions/1", json=_VERSION)
    api.on("DELETE", f"{_BASE}/versions/1", json=_DELETED)
    refused = invoke(
        make_state(tmp_path, api),
        "artifact",
        "versions",
        "delete",
        "models.churn",
        "1",
        "--env",
        "dev",
    )
    assert refused.exit_code != 0
    assert "Delete version 1 of dev/models.churn (19 B, 2 files)?" in _flat(refused.stderr)
    assert "Its bytes are removed from Lakana's storage. Lakana cannot bring them back." in _flat(
        refused.stderr
    )
    assert [r.method for r in api.requests] == ["GET"]
    done = invoke(
        make_state(tmp_path, api),
        "artifact",
        "versions",
        "delete",
        "models.churn",
        "1",
        "--env",
        "dev",
        "--yes",
    )
    assert done.exit_code == 0, done.stderr
    assert "Deleted version 1 of dev/models.churn" in done.stdout
    assert [r.method for r in api.requests][-1] == "DELETE"
    asked = invoke(
        make_state(tmp_path, api, interactive=True),
        "artifact",
        "versions",
        "delete",
        "models.churn",
        "1",
        "--env",
        "dev",
        input="n\n",
    )
    assert asked.exit_code != 0
    assert "Lakana cannot bring them back." in _flat(asked.stdout + asked.stderr)
    assert [r.method for r in api.requests].count("DELETE") == 1


def test_the_delete_help_does_not_promise_that_the_bytes_are_gone_everywhere(
    tmp_path: Path,
) -> None:
    helped = invoke(make_state(tmp_path, FakeApi()), "artifact", "versions", "delete", "--help")
    text = _flat(helped.stdout)
    assert "removed from Lakana's storage" in text
    assert "for good" not in text
    assert "A bucket that keeps old object versions, and a backup, still have them" in text
    assert "how many files it freed and how many it kept, and why" in text
    assert "another version has them, an open upload names them" in text
    assert "(for example for a large version" in text


_REPORT = {
    "freed_files": 3,
    "freed_bytes": 1536,
    "kept_files": 2,
    "kept_bytes": 300,
    "kept": [{"reason": "other_version", "files": 2, "bytes": 300, "version": 5}],
}


def _delete_it(tmp_path: Path, report: Mapping[str, object], *flags: str) -> str:
    """Delete version 4 of `models.churn` against a server that answers with `report`.

    Parameters
    ----------
    tmp_path
        Scratch directory.
    report
        The count fields of the answer.
    *flags
        Global options placed before the command, such as ``--output json``.

    Returns
    -------
    str
        What the command printed on standard output.
    """
    api = FakeApi()
    api.on("GET", f"{_BASE}/versions/4", json={**_VERSION, "version": 4})
    api.on("DELETE", f"{_BASE}/versions/4", json={**_DELETED, "version": 4, **report})
    done = invoke(
        make_state(tmp_path, api),
        *flags,
        "artifact",
        "versions",
        "delete",
        "models.churn",
        "4",
        "--env",
        "dev",
        "--yes",
    )
    assert done.exit_code == 0, done.stderr
    return done.stdout


def test_a_delete_says_what_it_freed_and_what_it_kept(tmp_path: Path) -> None:
    assert _delete_it(tmp_path, _REPORT).splitlines() == [
        "Deleted version 4 of dev/models.churn. Freed 3 files (1.5 KiB).",
        "Kept 2 files (300 B) that version 5 also uses.",
    ]


def test_a_delete_that_keeps_nothing_says_only_what_it_freed(tmp_path: Path) -> None:
    report = {"freed_files": 1, "freed_bytes": 19, "kept_files": 0, "kept_bytes": 0, "kept": []}
    assert _delete_it(tmp_path, report).splitlines() == [
        "Deleted version 4 of dev/models.churn. Freed 1 file (19 B)."
    ]


def test_a_delete_that_freed_nothing_says_why_each_group_stayed(tmp_path: Path) -> None:
    report = {
        "freed_files": 0,
        "freed_bytes": 0,
        "kept_files": 4,
        "kept_bytes": 70,
        "kept": [
            {"reason": "other_version", "files": 1, "bytes": 10, "version": None},
            {"reason": "open_upload", "files": 1, "bytes": 20, "version": None},
            {"reason": "left_to_sweep", "files": 2, "bytes": 40, "version": None},
        ],
    }
    assert _delete_it(tmp_path, report).splitlines() == [
        "Deleted version 4 of dev/models.churn. Freed no files.",
        "Kept 1 file (10 B) that another version also uses, 1 file (20 B) that an open upload "
        "names, and 2 files (40 B) that Lakana's periodic clean-up decides about: it removes what "
        "nothing else needs and keeps the rest.",
    ]


def test_two_other_versions_are_named_each_and_a_new_reason_reads_as_the_sweep(
    tmp_path: Path,
) -> None:
    report = {
        "freed_files": 0,
        "freed_bytes": 0,
        "kept_files": 3,
        "kept_bytes": 6,
        "kept": [
            {"reason": "other_version", "files": 1, "bytes": 1, "version": 7},
            {"reason": "other_version", "files": 1, "bytes": 2, "version": 5},
            {"reason": "from_the_future", "files": 1, "bytes": 3, "version": None},
        ],
    }
    assert _delete_it(tmp_path, report).splitlines()[1] == (
        "Kept 1 file (1 B) that version 7 also uses, 1 file (2 B) that version 5 also uses, "
        "and 1 file (3 B) that Lakana's periodic clean-up decides about: it removes what "
        "nothing else needs and keeps the rest."
    )


def test_a_delete_by_a_server_that_does_not_report_says_so_plainly(tmp_path: Path) -> None:
    text = _flat(_delete_it(tmp_path, {}))
    assert text.startswith("Deleted version 4 of dev/models.churn.")
    assert "does not say how many files it freed" in text
    assert "Lakana's periodic clean-up removes" in text


def test_the_delete_json_carries_the_same_fields(tmp_path: Path) -> None:
    body = json.loads(_delete_it(tmp_path, _REPORT, "--output", "json"))
    assert (body["version"], body["freed_files"], body["freed_bytes"]) == (4, 3, 1536)
    assert (body["kept_files"], body["kept_bytes"]) == (2, 300)
    assert body["kept"] == [{"reason": "other_version", "files": 2, "bytes": 300, "version": 5}]


def test_a_delete_agrees_with_the_list(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/versions/2", json={**_VERSION, "version": 2})
    api.on("DELETE", f"{_BASE}/versions/2", json={**_DELETED, "version": 2, **_REPORT})
    api.on("GET", _BASE, json=_ARTIFACT)
    api.on("GET", f"{_BASE}/versions", json=[_VERSION, {**_DELETED, "version": 2}])
    done = invoke(
        make_state(tmp_path, api),
        "artifact",
        "versions",
        "delete",
        "models.churn",
        "2",
        "--env",
        "dev",
        "--yes",
    )
    assert done.stdout.startswith("Deleted version 2 of dev/models.churn.")
    listed = invoke(
        make_state(tmp_path, api), "artifact", "versions", "list", "models.churn", "--env", "dev"
    )
    row = next(line for line in listed.stdout.splitlines() if "2026-10-04T13:00:00Z" in line)
    assert row.split()[1] == "2"  # after the table's border


def test_a_version_that_is_already_deleted_is_not_asked_about(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/versions/1", json=_DELETED)
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "versions",
        "delete",
        "models.churn",
        "1",
        "--env",
        "dev",
        "--yes",
    )
    assert result.exit_code != 0
    text = _flat(result.stderr)
    assert "Version 1 of dev/models.churn was already deleted" in text
    assert "lakana artifact versions list models.churn --env dev" in text
    assert [r.method for r in api.requests] == ["GET"]


def test_a_version_number_must_be_positive_before_any_request(tmp_path: Path) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "versions",
        "delete",
        "models.churn",
        "0",
        "--env",
        "dev",
        "--yes",
    )
    assert result.exit_code != 0
    assert api.requests == []


def test_versions_delete_passes_break_glass_and_warns(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", f"{_BASE}/versions/1", json=_VERSION)
    api.on("DELETE", f"{_BASE}/versions/1", json=_DELETED)
    done = invoke(
        make_state(tmp_path, api),
        "artifact",
        "versions",
        "delete",
        "models.churn",
        "1",
        "--env",
        "dev",
        "--yes",
        "--break-glass",
        "--reason",
        "remove a leaked training set",
    )
    assert done.exit_code == 0, done.stderr
    assert api.requests[-1].url.params["break_glass_reason"] == "remove a leaked training set"
    assert "break-glass write to dev" in done.stderr
    lonely = invoke(
        make_state(tmp_path, api),
        "artifact",
        "versions",
        "delete",
        "models.churn",
        "1",
        "--env",
        "dev",
        "--yes",
        "--reason",
        "no flag",
    )
    assert lonely.exit_code != 0


def test_drop_names_what_goes_and_passes_break_glass(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json=_ARTIFACT)
    api.on("GET", f"{_BASE}/versions", json=[{**_VERSION, "version": 2}, _VERSION])
    api.on("DELETE", _BASE, status=204)
    refused = invoke(make_state(tmp_path, api), "artifact", "drop", "models.churn", "--env", "dev")
    assert refused.exit_code != 0
    assert (
        "Drop artifact dev/models.churn and delete its 2 versions (2.0 KiB)? "
        "This cannot be undone." in _flat(refused.stderr)
    )
    assert "DELETE" not in [r.method for r in api.requests]
    done = invoke(
        make_state(tmp_path, api),
        "artifact",
        "drop",
        "models.churn",
        "--env",
        "dev",
        "--yes",
        "--break-glass",
        "--reason",
        "retire the churn model",
    )
    assert done.exit_code == 0, done.stderr
    assert "Dropped artifact dev/models.churn" in done.stdout
    flat = " ".join(done.stdout.split())
    assert "and Lakana's periodic clean-up removes any it could not." in flat
    assert "removed from storage" in _flat(done.stdout)
    assert api.requests[-1].url.params["break_glass_reason"] == "retire the churn model"


def test_drop_counts_only_the_versions_that_still_have_bytes(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json={**_ARTIFACT, "versions": 3, "size_bytes": 19})
    api.on(
        "GET",
        f"{_BASE}/versions",
        json=[{**_VERSION, "version": 5}, {**_DELETED, "version": 2}, {**_DELETED, "version": 1}],
    )
    refused = invoke(make_state(tmp_path, api), "artifact", "drop", "models.churn", "--env", "dev")
    assert "delete its 1 version (19 B, 2 more already deleted)? This cannot be undone." in _flat(
        refused.stderr
    )


def test_drop_in_json_mode_says_ok(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _BASE, json=_ARTIFACT)
    api.on("GET", f"{_BASE}/versions", json=[_VERSION])
    api.on("DELETE", _BASE, status=204)
    done = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "artifact",
        "drop",
        "models.churn",
        "--env",
        "dev",
        "--yes",
    )
    said = json.loads(done.stdout)
    assert said["status"] == "ok"
    assert "\n" not in said["message"]
    assert said["message"].startswith("Dropped artifact dev/models.churn")


def test_drop_counts_the_versions_from_the_one_listing(tmp_path: Path) -> None:
    """The artifact's own count and the listing are two requests and may disagree."""
    api = FakeApi()
    api.on("GET", _BASE, json={**_ARTIFACT, "versions": 1})
    api.on("GET", f"{_BASE}/versions", json=[{**_VERSION, "version": 2}, _VERSION])
    refused = invoke(make_state(tmp_path, api), "artifact", "drop", "models.churn", "--env", "dev")
    text = _flat(refused.stderr)
    assert "delete its 2 versions (" in text
    assert "already deleted" not in text
    assert "-" not in text.split("delete its", 1)[1].split("?", 1)[0]


_UPLOADS = "/api/v1/environments/dev/artifacts/uploads"
_ITEM = {
    "upload_id": _UPLOAD_ID,
    "artifact": "models.churn",
    "created_at": "2026-10-07T12:00:00.123456Z",
    "expires_at": "2026-10-07T13:00:00.123456Z",
}


def test_uploads_list_shows_the_upload_ids_and_names_the_next_step(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _UPLOADS, json={"items": [_ITEM], "next_cursor": None})
    done = invoke(make_state(tmp_path, api), "artifact", "uploads", "list", "--env", "dev")
    assert done.exit_code == 0, done.stderr
    text = _flat(done.stdout)
    for cell in (_UPLOAD_ID, "models.churn", "2026-10-07T12:00:00.123456Z", "2026-10-07T13:00:00"):
        assert cell in text
    assert "Withdraw one with `lakana artifact uploads abort NAME UPLOAD_ID --env dev`." in text
    assert dict(api.requests[-1].url.params) == {"limit": "100"}


def test_uploads_list_keeps_the_id_whole_on_a_narrow_terminal(tmp_path: Path) -> None:
    """The id is what `abort` needs: the times give way before it is folded across lines."""
    api = FakeApi()
    api.on("GET", _UPLOADS, json={"items": [_ITEM], "next_cursor": None})
    narrow = invoke(
        make_state(tmp_path, api),
        "artifact",
        "uploads",
        "list",
        "--env",
        "dev",
        env={"COLUMNS": "100"},
    )
    lines = [line for line in narrow.stdout.splitlines() if _UPLOAD_ID in line]
    assert len(lines) == 1
    assert "Not shown: expires_at. Use --output json to see it." in narrow.stdout
    assert "…" not in narrow.stdout


def test_uploads_list_of_one_artifact_sends_its_name(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _UPLOADS, json={"items": [_ITEM], "next_cursor": None})
    invoke(make_state(tmp_path, api), "artifact", "uploads", "list", "models.churn", "--env", "dev")
    assert dict(api.requests[-1].url.params) == {"name": "models.churn", "limit": "100"}


def test_uploads_list_with_none_says_so_and_that_nothing_waits(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _UPLOADS, json={"items": [], "next_cursor": None})
    done = invoke(make_state(tmp_path, api), "artifact", "uploads", "list", "--env", "dev")
    assert done.exit_code == 0, done.stderr
    assert _flat(done.stdout) == "You have no open uploads in dev."


def test_uploads_list_says_how_to_get_the_next_page(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _UPLOADS, json={"items": [_ITEM], "next_cursor": "c1"})
    done = invoke(
        make_state(tmp_path, api), "artifact", "uploads", "list", "--env", "dev", "--limit", "1"
    )
    assert "More: --cursor c1" in done.stdout
    assert dict(api.requests[-1].url.params) == {"limit": "1"}
    again = invoke(
        make_state(tmp_path, api), "artifact", "uploads", "list", "--env", "dev", "--cursor", "c1"
    )
    assert again.exit_code == 0
    assert api.requests[-1].url.params["cursor"] == "c1"


def test_uploads_list_all_follows_every_page(tmp_path: Path) -> None:
    api = FakeApi()
    pages = iter(
        [
            {"items": [_ITEM], "next_cursor": "c1"},
            {"items": [{**_ITEM, "upload_id": "second-id"}], "next_cursor": None},
        ]
    )
    api.routes["GET", _UPLOADS] = lambda request: httpx.Response(200, json=next(pages))
    done = invoke(make_state(tmp_path, api), "artifact", "uploads", "list", "--env", "dev", "--all")
    assert done.exit_code == 0, done.stderr
    assert _UPLOAD_ID in _flat(done.stdout) and "second-id" in _flat(done.stdout)
    assert [r.url.params.get("cursor") for r in api.requests] == [None, "c1"]


def test_uploads_list_all_and_cursor_cannot_be_combined(tmp_path: Path) -> None:
    done = invoke(
        make_state(tmp_path, FakeApi()),
        "artifact",
        "uploads",
        "list",
        "--env",
        "dev",
        "--all",
        "--cursor",
        "c1",
    )
    assert done.exit_code != 0
    assert "--cursor and --all can't be combined" in done.stderr


def test_uploads_list_in_json_mode_carries_the_same_fields(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("GET", _UPLOADS, json={"items": [_ITEM], "next_cursor": "c1"})
    done = invoke(
        make_state(tmp_path, api), "--output", "json", "artifact", "uploads", "list", "--env", "dev"
    )
    assert json.loads(done.stdout) == {"items": [_ITEM], "next_cursor": "c1"}
    api.on("GET", _UPLOADS, json={"items": [_ITEM], "next_cursor": None})
    everything = invoke(
        make_state(tmp_path, api),
        "--output",
        "json",
        "artifact",
        "uploads",
        "list",
        "--env",
        "dev",
        "--all",
    )
    assert json.loads(everything.stdout) == [_ITEM]


def test_a_name_the_terminal_could_misread_is_escaped_in_the_list(tmp_path: Path) -> None:
    hostile = {**_ITEM, "artifact": "models.\x1b[2Jchurn\nforged"}
    api = FakeApi()
    api.on("GET", _UPLOADS, json={"items": [hostile], "next_cursor": None})
    done = invoke(make_state(tmp_path, api), "artifact", "uploads", "list", "--env", "dev")
    assert "\x1b" not in done.stdout
    assert "\\x1b[2Jchurn" in done.stdout


def test_the_uploads_group_help_names_list_and_abort(tmp_path: Path) -> None:
    helped = invoke(make_state(tmp_path, FakeApi()), "artifact", "uploads", "--help")
    text = _flat(helped.stdout)
    assert "list" in text and "abort" in text
    listed = _flat(
        invoke(make_state(tmp_path, FakeApi()), "artifact", "uploads", "list", "--help").stdout
    )
    assert "lakana artifact uploads abort" in listed
    assert "Only your own" in listed


def test_uploads_abort_withdraws_an_upload_and_needs_no_confirmation(tmp_path: Path) -> None:
    api = FakeApi()
    api.on("DELETE", f"{_BASE}/uploads/{_UPLOAD_ID}", status=204)
    done = invoke(
        make_state(tmp_path, api),
        "artifact",
        "uploads",
        "abort",
        "models.churn",
        _UPLOAD_ID,
        "--env",
        "dev",
    )
    assert done.exit_code == 0, done.stderr
    assert f"Withdrew upload {_UPLOAD_ID} of dev/models.churn" in done.stdout
    assert [r.method for r in api.requests] == ["DELETE"]


def test_uploads_abort_says_when_the_upload_is_gone(tmp_path: Path) -> None:
    api = FakeApi()
    api.on(
        "DELETE",
        f"{_BASE}/uploads/{_UPLOAD_ID}",
        status=409,
        json={
            "detail": "This upload has expired or does not exist; start the upload again.",
            "type": "ConflictError",
        },
    )
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "uploads",
        "abort",
        "models.churn",
        _UPLOAD_ID,
        "--env",
        "dev",
    )
    assert result.exit_code != 0
    assert "expired or does not exist" in result.stderr


def test_uploads_abort_with_something_that_is_not_an_upload_id_says_what_one_is(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    result = invoke(
        make_state(tmp_path, api),
        "artifact",
        "uploads",
        "abort",
        "models.churn",
        "u-1",
        "--env",
        "dev",
    )
    assert result.exit_code != 0
    text = _flat(result.stderr)
    assert "'u-1' is not an upload id" in text
    assert "UUID" in text
    assert "[" not in text
    assert api.requests == []


def test_the_abort_help_names_the_upload_lifetime(tmp_path: Path) -> None:
    help_text = _flat(
        invoke(make_state(tmp_path, FakeApi()), "artifact", "uploads", "abort", "--help").stdout
    )
    assert "after the upload lifetime (one hour by default)" in help_text
