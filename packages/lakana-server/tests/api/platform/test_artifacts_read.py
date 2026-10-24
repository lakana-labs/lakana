"""Listing artifacts, their versions, and downloading a file."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from lakana.resources import artifacts as sdk_artifacts
from lakana_core.artifacts.service import STORAGE_NOT_READ, ArtifactService, BlobDownload
from lakana_core.auth import AllowAllAuthorization
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.models import (
    ArtifactVersion,
    AuditEvent,
    AuditEventType,
    Environment,
    JobVersion,
    Principal,
    Repository,
    RepositoryKind,
    Run,
    RunKind,
    RunStatus,
    TaskRun,
    TaskStatus,
)
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_server.api.platform import artifacts as routes
from lakana_server.api.platform.artifacts import parse_version
from lakana_server.concurrency import AppendConcurrencyLimiter
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import (
    create_environment,
    create_namespace,
    create_table,
    upload_artifact,
)

_ENV = "/api/v1/environments/dev"
_BASE = f"{_ENV}/namespaces/models/artifacts/churn"
_FILES = {"model.json": b'{"factor": 2}\n', "docs/notes.txt": b"notes"}
_SHA = hashlib.sha256(_FILES["model.json"]).hexdigest()
_NOTES_SHA = hashlib.sha256(_FILES["docs/notes.txt"]).hexdigest()


def _token_for(client: TestClient, name: str, grants: list[dict[str, str]]) -> dict[str, str]:
    """Create a service account with a PAT and the given grants.

    Parameters
    ----------
    client
        The test client.
    name
        Principal name.
    grants
        Grant bodies without `principal`.

    Returns
    -------
    dict
        An Authorization header.
    """
    client.post(
        "/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "SERVICE_ACCOUNT"}
    )
    issued = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    )
    for grant in grants:
        granted = client.post(
            "/api/v1/grants", headers=AUTH_HEADER, json={"principal": name, **grant}
        )
        assert granted.status_code in (200, 201), granted.text
    return {"Authorization": f"Bearer {issued.json()['token']}"}


_READ_ENV = {"action": "READ_ENVIRONMENT", "kind": "ENVIRONMENT", "environment": "dev"}


def _read_table(table: str) -> dict[str, str]:
    """Build the grant body that lets a principal read one artifact.

    Parameters
    ----------
    table
        The artifact's name inside `models`.

    Returns
    -------
    dict
        The grant body.
    """
    return {
        "action": "READ_TABLE",
        "kind": "TABLE",
        "environment": "dev",
        "namespace": "models",
        "table": table,
    }


@pytest.fixture
def stored(client: TestClient) -> TestClient:
    """Provide a client whose `dev` holds `models.churn` (two versions) and `models.other`.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    TestClient
        The same client.
    """
    create_environment(client)
    create_namespace(client, "dev", "models")
    upload_artifact(client, "dev", "models.churn", _FILES)
    upload_artifact(client, "dev", "models.churn", {**_FILES, "docs/notes.txt": b"more notes"})
    upload_artifact(client, "dev", "models.other", {"m": b"m"}, is_directory=False)
    return client


def test_list_and_describe(stored: TestClient) -> None:
    listed = stored.get(f"{_ENV}/artifacts", headers=AUTH_HEADER)
    assert listed.status_code == 200
    assert [
        (a["name"], a["namespace"], a["kind"], a["current_version"], a["versions"])
        for a in listed.json()
    ] == [
        ("churn", "models", "ARTIFACT", 2, 2),
        ("other", "models", "ARTIFACT", 1, 1),
    ]
    assert (
        stored.get(f"{_ENV}/artifacts", params={"namespace": "gold"}, headers=AUTH_HEADER).json()
        == []
    )
    one = stored.get(_BASE, headers=AUTH_HEADER).json()
    assert (one["current_version"], one["size_bytes"]) == (2, 14 + 10 + 14 + 5)
    assert set(one) == {
        "name",
        "namespace",
        "uuid",
        "kind",
        "current_version",
        "versions",
        "size_bytes",
        "created_at",
    }


def test_versions_and_one_version(stored: TestClient) -> None:
    versions = stored.get(f"{_BASE}/versions", headers=AUTH_HEADER).json()
    assert [(v["version"], v["files"]) for v in versions] == [(2, []), (1, [])]
    current = stored.get(f"{_BASE}/versions/current", headers=AUTH_HEADER).json()
    assert current["version"] == 2
    first = stored.get(f"{_BASE}/versions/1", headers=AUTH_HEADER).json()
    assert [f["path"] for f in first["files"]] == ["docs/notes.txt", "model.json"]
    absent = stored.get(f"{_BASE}/versions/7", headers=AUTH_HEADER)
    assert (absent.status_code, absent.json()["detail"]) == (404, "models.churn has no version 7.")


_BAD_VERSIONS = [
    "0",
    "-1",
    "01",
    "+1",
    "1.0",
    "latest",
    "Current",
    "CURRENT",
    "9999999999",
    "2147483648",
    "9" * 5000,
    chr(0x661),  # an Arabic-Indic digit one
    "1" + chr(0x661),
    "%20",
    "1%20",
    "0x10",
    "1e3",
]


@pytest.mark.parametrize("bad", _BAD_VERSIONS)
@pytest.mark.slow
def test_a_version_that_is_not_a_bounded_number_is_a_validation_error(
    stored: TestClient, bad: str
) -> None:
    refused = stored.get(f"{_BASE}/versions/{bad}", headers=AUTH_HEADER)
    assert refused.status_code == 422, bad[:20]
    assert refused.json()["detail"] == "A version is a positive number or 'current'."
    blob = stored.get(f"{_BASE}/versions/{bad}/blobs/{_SHA}", headers=AUTH_HEADER)
    assert blob.status_code == 422
    assert blob.json()["detail"] == "A version is a positive number."


def test_the_bounds_of_a_version_number() -> None:
    assert parse_version("1", allow_current=False) == 1
    assert parse_version("2147483647", allow_current=False) == 2_147_483_647
    assert parse_version("current", allow_current=True) is None
    with pytest.raises(ValidationError, match=r"A version is a positive number\."):
        parse_version("current", allow_current=False)
    with pytest.raises(ValidationError, match=r"A version is a positive number or 'current'\."):
        parse_version("2147483648", allow_current=True)


def test_a_file_is_streamed_with_its_length(stored: TestClient) -> None:
    got = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert got.status_code == 200
    assert got.content == _FILES["model.json"]
    assert got.headers["content-length"] == "14"
    assert got.headers["content-type"] == "application/octet-stream"
    for url, status, detail in (
        (f"{_BASE}/versions/current/blobs/{_SHA}", 422, "A version is a positive number."),
        (
            f"{_BASE}/versions/1/blobs/{'0' * 64}",
            404,
            "Version 1 of models.churn has no file with that SHA-256.",
        ),
        (f"{_BASE}/versions/9/blobs/{_SHA}", 404, "models.churn has no version 9."),
    ):
        refused = stored.get(url, headers=AUTH_HEADER)
        assert (refused.status_code, refused.json()["detail"]) == (status, detail)
    assert (
        stored.get(f"{_BASE}/versions/1/blobs/{_SHA.upper()}", headers=AUTH_HEADER).status_code
        == 422
    )


def test_an_artifact_without_a_version_says_what_to_do(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    document = {
        "format": 1,
        "is_directory": False,
        "files": [{"path": "m", "size": 1, "sha256": "a" * 64}],
    }
    assert client.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=document).status_code == 201
    current = client.get(f"{_BASE}/versions/current", headers=AUTH_HEADER)
    assert current.status_code == 404
    assert current.json()["detail"] == (
        "models.churn has no version yet; run the asset that produces it."
    )
    assert client.get(_BASE, headers=AUTH_HEADER).json()["current_version"] is None


def test_a_deleted_version_lists_without_files_and_its_bytes_are_gone(stored: TestClient) -> None:
    deleted = stored.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER)
    assert deleted.status_code == 200, deleted.text
    versions = stored.get(f"{_BASE}/versions", headers=AUTH_HEADER).json()
    assert [(v["version"], v["deleted_at"] is not None) for v in versions] == [
        (2, False),
        (1, True),
    ]
    assert (versions[1]["deleted_by"], versions[1]["files"]) == ("admin", [])
    gone = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert gone.status_code == 404
    assert gone.json()["detail"] == (
        "Version 1 of models.churn was deleted; its bytes are gone. "
        "Read a version that exists: `lakana artifact versions list models.churn --env dev` "
        "shows which."
    )


# --- Point A: what a read reveals ---------------------------------------------------------------

_SUFFIXES = (
    "",
    "/versions",
    "/versions/current",
    "/versions/1",
    "/versions/2",
    "/versions/7",
    f"/versions/1/blobs/{_SHA}",
    f"/versions/2/blobs/{_NOTES_SHA}",
    f"/versions/9/blobs/{_SHA}",
    f"/versions/1/blobs/{'0' * 64}",
)


def _normalised(response: Any, names: tuple[str, ...]) -> tuple[int, str, str | None]:  # ruff: ignore[any-type]
    """Reduce a response to what must not differ between an existing and a missing name.

    Parameters
    ----------
    response
        The response.
    names
        Names to blank out (the ones the request itself carried).

    Returns
    -------
    tuple
        Status, detail with the names blanked out, and the `Retry-After` header.
    """
    detail = response.json()["detail"]
    for name in names:
        detail = detail.replace(name, "<name>")
    return response.status_code, detail, response.headers.get("retry-after")


@pytest.mark.parametrize("deleted", [False, True], ids=["live", "version 1 deleted"])
@pytest.mark.parametrize("grants", ["none", "environment only", "another artifact"])
def test_a_caller_without_read_gets_the_same_answer_for_missing_and_existing(
    stored: TestClient, grants: str, deleted: bool
) -> None:
    """403 with one text, whether or not the artifact, version or file exists.

    With `deleted`, version 1 is deleted through the real route first, so a deleted version is
    one more case that must not differ from a version that never existed.
    """
    if deleted:
        assert stored.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER).status_code == 200
    given = {
        "none": [],
        "environment only": [_READ_ENV],
        "another artifact": [_READ_ENV, _read_table("other")],
    }[grants]
    caller = _token_for(stored, "caller", given)
    ghost = _BASE.replace("churn", "ghost")
    for suffix in _SUFFIXES:
        real = stored.get(_BASE + suffix, headers=caller)
        missing = stored.get(ghost + suffix, headers=caller)
        assert real.status_code == 403, suffix
        assert _normalised(real, ("churn",)) == _normalised(missing, ("ghost",)), suffix
        assert "churn" not in real.json()["detail"]
    # A name space the caller cannot see is not revealed either.
    hidden = stored.get(f"{_ENV}/namespaces/nowhere/artifacts/churn", headers=caller)
    assert hidden.status_code == 403


def test_a_list_shows_only_what_the_caller_may_read_and_hides_the_rest_alike(
    stored: TestClient,
) -> None:
    blind = _token_for(stored, "blind", [_READ_ENV])
    assert stored.get(f"{_ENV}/artifacts", headers=blind).json() == []
    for namespace in ("models", "nowhere", "gold"):
        listed = stored.get(f"{_ENV}/artifacts", params={"namespace": namespace}, headers=blind)
        assert (listed.status_code, listed.json()) == (200, [])
    nobody = _token_for(stored, "nobody", [])
    refused = stored.get(f"{_ENV}/artifacts", headers=nobody)
    unknown = stored.get("/api/v1/environments/unknown/artifacts", headers=nobody)
    assert _normalised(refused, ("dev",)) == _normalised(unknown, ("unknown",))
    assert refused.status_code == 403


def test_a_grant_on_one_artifact_shows_only_that_one(stored: TestClient) -> None:
    reader = _token_for(stored, "reader", [_READ_ENV, _read_table("churn")])
    assert [a["name"] for a in stored.get(f"{_ENV}/artifacts", headers=reader).json()] == ["churn"]
    assert (
        stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=reader).content
        == _FILES["model.json"]
    )
    assert stored.get(_BASE.replace("churn", "other"), headers=reader).status_code == 403
    events = stored.get(
        "/api/v1/audit-events", params={"event_type": "ARTIFACT_DOWNLOADED"}, headers=AUTH_HEADER
    ).json()["items"]
    assert [e["detail"]["principal"] for e in events] == ["reader"]


def test_an_environment_wide_reader_sees_every_artifact(stored: TestClient) -> None:
    viewer = _token_for(
        stored, "viewer", [{"role": "VIEWER", "kind": "ENVIRONMENT", "environment": "dev"}]
    )
    assert [a["name"] for a in stored.get(f"{_ENV}/artifacts", headers=viewer).json()] == [
        "churn",
        "other",
    ]


def _task_token(client: TestClient, asset_scope: list[str]) -> dict[str, str]:
    """Mint an unbound task token for `admin` with the given asset scope.

    Parameters
    ----------
    client
        The test client.
    asset_scope
        The patterns the token declares.

    Returns
    -------
    dict
        An Authorization header.
    """
    minted = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": asset_scope,
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text
    return {"Authorization": f"Bearer {minted.json()['token']}"}


def test_the_table_listing_leaves_out_what_a_narrow_token_may_not_read(client: TestClient) -> None:
    """A-I3: the listing neither fails on, nor names, an object the caller did not declare."""
    create_environment(client)
    listing = f"{_ENV}/namespaces/gold/tables"
    create_table(client, "dev", "gold", "a")
    create_table(client, "dev", "gold", "b")
    only_a = _task_token(client, ["gold.a"])
    listed = client.get(listing, headers=only_a)
    assert listed.status_code == 200, listed.text
    assert [t["name"] for t in listed.json()] == ["a"]
    upload_artifact(client, "dev", "gold.model", {"m": b"m"}, is_directory=False)
    upload_artifact(client, "dev", "gold.other", {"m": b"m"}, is_directory=False)
    assert [t["name"] for t in client.get(listing, headers=only_a).json()] == ["a"]
    narrow = _task_token(client, ["gold.model"])
    listed = client.get(listing, headers=narrow)
    assert listed.status_code == 200, listed.text
    assert [(t["name"], t["kind"]) for t in listed.json()] == [("model", "ARTIFACT")]
    everything = client.get(listing, headers=AUTH_HEADER).json()
    assert [t["name"] for t in everything] == ["a", "b", "model", "other"]


def test_a_name_that_is_a_table_is_not_an_artifact(stored: TestClient) -> None:
    created = stored.post(
        f"{_ENV}/namespaces/silver/tables",
        headers=AUTH_HEADER,
        json={"name": "rows", "schema": [{"name": "id", "type": "long", "nullable": False}]},
    )
    assert created.status_code == 201, created.text
    wrong = stored.get(f"{_ENV}/namespaces/silver/artifacts/rows", headers=AUTH_HEADER)
    assert wrong.status_code == 422
    assert "is a table, not an artifact" in wrong.json()["detail"]
    assert [a["name"] for a in stored.get(f"{_ENV}/artifacts", headers=AUTH_HEADER).json()] == [
        "churn",
        "other",
    ]


def test_a_malformed_namespace_or_name_is_a_validation_error_not_a_500(stored: TestClient) -> None:
    for query in ("a..b", "a/b", "x" * 5000, ""):
        listed = stored.get(f"{_ENV}/artifacts", params={"namespace": query}, headers=AUTH_HEADER)
        assert listed.status_code in (200, 422), query[:10]
    assert (
        stored.get(
            f"{_ENV}/artifacts", params={"namespace": "x" * 5000}, headers=AUTH_HEADER
        ).status_code
        == 422
    )
    assert stored.get(f"{_ENV}/namespaces/a..b/artifacts/c", headers=AUTH_HEADER).status_code == 422


# --- Point B: bound task tokens -----------------------------------------------------------------


def _bound_token(
    client: TestClient, asset: str, inputs: dict[str, str], output: str
) -> dict[str, str]:
    """Run a task attempt of `asset` in `dev` and mint its bound token, as the worker does.

    Parameters
    ----------
    client
        The test client.
    asset
        The task's own asset.
    inputs
        Parameter to input name.
    output
        ``table`` or ``artifact``.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    suffix = uuid.uuid4().hex[:8]
    client.post(
        "/api/v1/principals",
        headers=AUTH_HEADER,
        json={"name": f"etl-{suffix}", "type": "SERVICE_ACCOUNT"},
    )
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": f"etl-{suffix}",
            "role": "DATA_ENGINEER",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert granted.status_code < 300, granted.text
    with Session(client.app.state.engine) as session:
        env = session.execute(select(Environment).where(Environment.name == "dev")).scalar_one()
        etl = session.execute(
            select(Principal).where(Principal.name == f"etl-{suffix}")
        ).scalar_one()
        repo = Repository(
            id=uuid.uuid4(), name=f"repo-{suffix}", kind=RepositoryKind.HTTPS, url="https://h/a.git"
        )
        session.add(repo)
        session.flush()
        version = JobVersion(
            id=uuid.uuid4(),
            repository_id=repo.id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json={
                "version": 3,
                "assets": [
                    {
                        "table": asset,
                        "kind": "python",
                        "path": "assets/x.py",
                        "inputs": inputs,
                        "sql": "",
                        "output": output,
                    }
                ],
                "edges": [],
            },
            code_uri="file:///x",
            code_sha256="0" * 64,
        )
        session.add(version)
        session.flush()
        run = Run(
            id=uuid.uuid4(),
            environment_id=env.id,
            repository_id=repo.id,
            job_version_id=version.id,
            run_as_principal_id=etl.id,
            kind=RunKind.FULL,
            status=RunStatus.RUNNING,
            selection_json={},
        )
        session.add(run)
        session.flush()
        task = TaskRun(
            id=uuid.uuid4(),
            run_id=run.id,
            asset=asset,
            status=TaskStatus.RUNNING,
            claim_count=1,
            attempt=1,
            lease_owner="w1",
            max_attempts=3,
            timeout_seconds=60,
            retry_delay_seconds=0,
            upstream_json=[],
        )
        session.add(task)
        session.commit()
        task_id = task.id
    with Session(client.app.state.engine) as session:
        minted = TaskTokenService(session, AllowAllAuthorization()).mint_for_task(
            task_id, 1, ttl_seconds=300
        )
    return {"Authorization": f"Bearer {minted.token}"}


def _stamp_producer(client: TestClient) -> dict[str, str]:
    """Give version 1 of `models.churn` the producer fields a worker-run task leaves.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    dict
        The ids written, as strings.
    """
    with Session(client.app.state.engine) as session:
        task = session.execute(select(TaskRun)).scalars().first()
        assert task is not None
        run = session.get(Run, task.run_id)
        assert run is not None
        session.execute(
            update(ArtifactVersion)
            .where(ArtifactVersion.version == 1)
            .values(task_run_id=task.id, job_version_id=run.job_version_id, commit_sha="b" * 40)
        )
        session.commit()
        return {
            "task_run_id": str(task.id),
            "run_id": str(run.id),
            "project_version_id": str(run.job_version_id),
        }


_PRODUCER = ("task_run_id", "run_id", "project_version_id")


def test_a_bound_token_reads_a_declared_input_without_the_run_identifiers(
    stored: TestClient,
) -> None:
    """A task knows what it consumes (commit, author), not another task's run ids."""
    token = _bound_token(stored, "gold.scores", {"m": "models.churn"}, "table")
    ids = _stamp_producer(stored)
    person = stored.get(f"{_BASE}/versions/1", headers=AUTH_HEADER).json()
    assert {k: person[k] for k in _PRODUCER} == ids
    assert person["commit_sha"] == "b" * 40
    for suffix in ("/versions/1", "/versions/current"):
        got = stored.get(_BASE + suffix, headers=token)
        assert got.status_code == 200, got.text
        body = got.json()
        assert [body[k] for k in _PRODUCER] == [None, None, None]
        assert body["commit_sha"] == ("b" * 40 if suffix.endswith("1") else None)
        assert body["created_by"] == "admin"
        assert [f["path"] for f in body["files"]] == ["docs/notes.txt", "model.json"]
    listed = stored.get(f"{_BASE}/versions", headers=token).json()
    assert [[v[k] for k in _PRODUCER] for v in listed] == [[None] * 3, [None] * 3]
    assert [v["commit_sha"] for v in listed] == [None, "b" * 40]
    assert [a["name"] for a in stored.get(f"{_ENV}/artifacts", headers=token).json()] == ["churn"]
    downloaded = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=token)
    assert downloaded.content == _FILES["model.json"]


def test_a_bound_token_reads_nothing_it_did_not_declare_and_not_its_own_asset(
    stored: TestClient,
) -> None:
    consumer = _bound_token(stored, "gold.scores", {"m": "models.churn"}, "table")
    other = _BASE.replace("churn", "other")
    for suffix in (
        "",
        "/versions",
        "/versions/1",
        f"/versions/1/blobs/{hashlib.sha256(b'm').hexdigest()}",
    ):
        assert stored.get(other + suffix, headers=consumer).status_code == 403
    producer = _bound_token(stored, "models.churn", {"o": "models.other"}, "artifact")
    for suffix in (
        "",
        "/versions",
        "/versions/1",
        "/versions/current",
        f"/versions/1/blobs/{_SHA}",
    ):
        assert stored.get(_BASE + suffix, headers=producer).status_code == 403, suffix
    ghost = stored.get(_BASE.replace("churn", "ghost") + "/versions", headers=producer)
    real = stored.get(_BASE + "/versions", headers=producer)
    assert _normalised(real, ("churn",)) == _normalised(ghost, ("ghost",))
    assert stored.get(other + "/versions/1", headers=producer).status_code == 200


def test_a_bound_token_download_writes_no_audit_event(stored: TestClient) -> None:
    token = _bound_token(stored, "gold.scores", {"m": "models.churn"}, "table")
    assert stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=token).status_code == 200
    events = stored.get(
        "/api/v1/audit-events", params={"event_type": "ARTIFACT_DOWNLOADED"}, headers=AUTH_HEADER
    ).json()["items"]
    assert events == []


# --- Point D: auditing once per principal and version -------------------------------------------


def _downloads(client: TestClient) -> list[dict[str, Any]]:
    """List the `ARTIFACT_DOWNLOADED` events, oldest first.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    list of dict
        Each event's detail.
    """
    items = client.get(
        "/api/v1/audit-events",
        params={"event_type": "ARTIFACT_DOWNLOADED", "limit": 500},
        headers=AUTH_HEADER,
    ).json()["items"]
    return [item["detail"] for item in reversed(items)]


def test_two_files_of_one_version_write_one_event_and_the_route_adds_none_of_its_own(
    stored: TestClient,
) -> None:
    for sha in (_SHA, hashlib.sha256(b"more notes").hexdigest(), _SHA):
        assert stored.get(f"{_BASE}/versions/2/blobs/{sha}", headers=AUTH_HEADER).status_code == 200
    # Reading metadata is not a download.
    stored.get(f"{_BASE}/versions/2", headers=AUTH_HEADER)
    stored.get(f"{_BASE}/versions", headers=AUTH_HEADER)
    events = _downloads(stored)
    assert len(events) == 1
    assert (events[0]["version"], events[0]["file_count"], events[0]["size_bytes"]) == (2, 2, 24)
    other_version = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert other_version.status_code == 200
    assert [e["version"] for e in _downloads(stored)] == [2, 1]
    with Session(stored.app.state.engine) as session:
        count = session.scalars(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.ARTIFACT_DOWNLOADED)
        ).all()
        assert len(count) == 2


# --- Point C: the download streams --------------------------------------------------------------


class _Probe:
    """Records what the database looks like when the body starts to be read."""

    def __init__(self, client: TestClient, data: bytes) -> None:
        self.client = client
        self.data = data
        self.in_transaction: list[bool] = []
        self.checked_out: list[int] = []
        self.closed = False

    def chunks(self) -> Iterator[bytes]:
        """Serve the data, noting the state at the first piece and when the iterator closes.

        Yields
        ------
        bytes
            The data in two pieces.
        """
        engine = self.client.app.state.engine
        try:
            self.checked_out.append(engine.pool.checkedout())
            half = len(self.data) // 2
            yield self.data[:half]
            self.checked_out.append(engine.pool.checkedout())
            yield self.data[half:]
        finally:
            self.closed = True


def test_no_session_or_transaction_is_held_while_the_body_is_sent(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _Probe(stored, _FILES["model.json"])
    seen: list[bool] = []
    real_open = ArtifactService.open_blob

    def open_blob(self: ArtifactService, *args: Any) -> BlobDownload:  # ruff: ignore[any-type]
        download = real_open(self, *args)
        seen.append(self._session.in_transaction())
        return download

    monkeypatch.setattr(ArtifactService, "open_blob", open_blob)
    monkeypatch.setattr(FsspecStorage, "read_chunks", lambda self, path, **kw: probe.chunks())
    got = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert got.content == _FILES["model.json"]
    assert seen == [False]
    assert probe.checked_out == [0, 0]
    assert probe.closed


def test_a_bound_caller_holds_no_transaction_either(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _bound_token(stored, "gold.scores", {"m": "models.churn"}, "table")
    probe = _Probe(stored, _FILES["model.json"])
    monkeypatch.setattr(FsspecStorage, "read_chunks", lambda self, path, **kw: probe.chunks())
    assert stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=token).status_code == 200
    assert probe.checked_out == [0, 0]


def test_the_iterator_is_closed_when_the_download_is_cut_short(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _Probe(stored, _FILES["model.json"])
    monkeypatch.setattr(FsspecStorage, "read_chunks", lambda self, path, **kw: probe.chunks())
    with stored.stream("GET", f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER) as response:
        next(response.iter_bytes())
    assert probe.closed


def _run_pump(chunks: Iterator[bytes], size: int) -> tuple[list[bytes], BaseException | None]:
    """Run the download body over an iterator, collecting what it yields and how it ends.

    Parameters
    ----------
    chunks
        The stored object's pieces.
    size
        The manifest's size.

    Returns
    -------
    tuple
        The pieces sent, and the exception that ended the body, if any.
    """

    async def go() -> tuple[list[bytes], BaseException | None]:
        sent: list[bytes] = []
        try:
            async for piece in routes.download_body(chunks, size, label="x"):
                sent.append(piece)
        except routes.DownloadCut as exc:
            return sent, exc
        return sent, None

    return asyncio.run(go())


def test_an_object_that_is_not_the_manifests_size_is_cut_not_sent_as_a_short_200() -> None:
    sent, error = _run_pump(iter([b"abc", b"de"]), 5)
    assert (b"".join(sent), error) == (b"abcde", None)
    assert _run_pump(iter([]), 0) == ([], None)
    sent, error = _run_pump(iter([b"abc"]), 5)
    assert b"".join(sent) == b"abc"
    assert isinstance(error, routes.DownloadCut)
    assert "shorter" in str(error)
    sent, error = _run_pump(iter([b"abc", b"d"]), 3)
    assert (b"".join(sent), isinstance(error, routes.DownloadCut)) == (b"abc", True)
    sent, error = _run_pump(iter([b"abc", b"def"]), 5)
    assert len(b"".join(sent)) <= 5
    assert isinstance(error, routes.DownloadCut)
    assert "longer" in str(error)


def test_a_storage_failure_mid_stream_cuts_the_connection_and_closes_the_iterator() -> None:
    closed: list[bool] = []

    def failing() -> Iterator[bytes]:
        try:
            yield b"ab"
            raise OSError(5, "/secret/bucket/key: I/O error")
        finally:
            closed.append(True)

    sent, error = _run_pump(failing(), 5)
    assert sent == [b"ab"]
    assert isinstance(error, routes.DownloadCut)
    assert "secret" not in str(error)
    assert closed == [True]


def test_a_cut_download_over_http_ends_without_a_complete_body(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(FsspecStorage, "read_chunks", lambda self, path, **kw: iter([b'{"fac']))
    caplog.set_level(logging.WARNING)
    with pytest.raises(routes.DownloadCut):
        stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert "secret" not in caplog.text


def test_storage_that_cannot_be_read_is_a_503_with_retry_after_not_a_500(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def failing(self: FsspecStorage, path: str, **kw: Any) -> Iterator[bytes]:  # ruff: ignore[any-type]
        raise OSError(5, "/secret/bucket/path: connection refused")

    monkeypatch.setattr(FsspecStorage, "read_chunks", failing)
    down = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert down.status_code == 503
    assert down.headers["Retry-After"] == "5"
    assert down.json()["detail"] == STORAGE_NOT_READ
    assert "secret" not in down.text
    assert "storage_unreachable" in {getattr(r, "event", None) for r in caplog.records}
    monkeypatch.undo()
    assert stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER).status_code == 200


def test_a_missing_object_is_a_404_that_says_what_to_do(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(self: FsspecStorage, path: str, **kw: Any) -> Iterator[bytes]:  # ruff: ignore[any-type]
        raise NotFoundError(f"No object at {path!r}.")

    monkeypatch.setattr(FsspecStorage, "read_chunks", missing)
    gone = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert gone.status_code == 404
    assert "missing from storage" in gone.json()["detail"]
    assert "artifacts/" not in gone.text


def _held() -> tuple[AppendConcurrencyLimiter, Callable[[], None]]:
    """Build a limiter of one slot, hold the slot, and return it with its release.

    Returns
    -------
    tuple
        The limiter and the function that gives the slot back.
    """
    limiter = AppendConcurrencyLimiter(1)
    assert limiter.try_acquire()
    return limiter, limiter.release


def _free_count(limiter: AppendConcurrencyLimiter) -> int:
    """Count the free slots of a limiter without changing it.

    Parameters
    ----------
    limiter
        The limiter.

    Returns
    -------
    int
        How many `try_acquire` calls succeed in a row.
    """
    taken = 0
    while limiter.try_acquire():
        taken += 1
    for _ in range(taken):
        limiter.release()
    return taken


def test_a_download_deadline_grows_with_the_size_and_has_a_floor() -> None:
    floor = routes.download_deadline(0)
    assert floor == routes.DOWNLOAD_BASE_SECONDS
    big = routes.download_deadline(routes.DOWNLOAD_MIN_BYTES_PER_SECOND * 1000)
    assert big == routes.DOWNLOAD_BASE_SECONDS + 1000


def test_the_slowest_line_a_download_waits_for_is_64_kib_per_second() -> None:
    """2 GiB at 64 KiB/s is 32768 s (about 9.1 h) plus the base: a 0.5 Mbit/s line finishes."""
    assert routes.DOWNLOAD_MIN_BYTES_PER_SECOND == 64 * 1024
    assert routes.download_deadline(2 * 1024**3) == 60 + 32768


def test_a_client_that_stops_reading_is_cut_at_the_deadline_and_the_iterator_closes() -> None:
    closed: list[bool] = []

    def many() -> Iterator[bytes]:
        try:
            while True:
                yield b"x" * 4
        finally:
            closed.append(True)

    async def stalled_send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            await asyncio.Event().wait()  # a client that never reads

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}  # pragma: no cover

    slots, release = _held()
    response = routes.DownloadResponse(
        many(), size=10**9, label="x", deadline=0.05, release=release
    )
    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}

    async def go() -> BaseException | None:
        try:
            await response(scope, receive, stalled_send)  # ty: ignore[invalid-argument-type]
        except routes.DownloadCut as exc:
            return exc
        return None

    error = asyncio.run(go())
    assert isinstance(error, routes.DownloadCut)
    assert "too long" in str(error)
    assert closed
    assert _free_count(slots) == 1  # the slot came back, once


def test_a_client_that_never_reads_even_the_headers_still_has_its_iterator_closed() -> None:
    """The body generator never started, so only the response itself can close the iterator."""

    class Pieces:
        closed = False

        def __iter__(self) -> Pieces:
            return self

        def __next__(self) -> bytes:  # pragma: no cover -- the body never starts
            raise StopIteration

        def close(self) -> None:
            self.closed = True

    async def stalled(message: dict[str, Any]) -> None:
        await asyncio.Event().wait()

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}  # pragma: no cover

    pieces = Pieces()
    slots, release = _held()
    response = routes.DownloadResponse(pieces, size=3, label="x", deadline=0.05, release=release)
    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}

    async def go() -> None:
        with contextlib.suppress(routes.DownloadCut):
            await response(scope, receive, stalled)  # ty: ignore[invalid-argument-type]

    asyncio.run(go())
    assert pieces.closed
    assert _free_count(slots) == 1


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
def test_a_client_that_disconnects_closes_the_iterator(spec: str) -> None:
    """Both ways a server tells the app: a disconnect message (2.3) or a failing send (2.4)."""
    closed: list[bool] = []

    def many() -> Iterator[bytes]:
        try:
            while True:
                yield b"x" * 4
        finally:
            closed.append(True)

    async def send(message: dict[str, Any]) -> None:
        if spec == "2.4" and message["type"] == "http.response.body":
            raise OSError("the client went away")
        await asyncio.sleep(0)

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(0.01)
        return {"type": "http.disconnect"}

    slots, release = _held()
    response = routes.DownloadResponse(many(), size=10**9, label="x", deadline=5.0, release=release)
    scope = {"type": "http", "asgi": {"spec_version": spec}}

    async def go() -> None:
        with contextlib.suppress(ClientDisconnect):
            await response(scope, receive, send)  # ty: ignore[invalid-argument-type]

    asyncio.run(go())
    assert closed
    assert _free_count(slots) == 1


def test_a_download_does_not_take_an_upload_slot(stored: TestClient) -> None:
    limiter = stored.app.state.append_concurrency_limiter
    while limiter.try_acquire():
        pass
    try:
        got = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
        assert got.status_code == 200
    finally:
        for _ in range(stored.app.state.settings.append.max_concurrent):
            limiter.release()


def test_the_info_route_tells_the_artifact_limits(client: TestClient) -> None:
    body = client.get("/api/v1/info", headers=AUTH_HEADER).json()
    assert body["artifact_limits"] == {
        "max_files": 10_000,
        "max_file_bytes": 2 * 1024**3,
        "max_bytes": 10 * 1024**3,
    }
    client.app.state.settings.artifacts.max_files = 7
    assert (
        client.get("/api/v1/info", headers=AUTH_HEADER).json()["artifact_limits"]["max_files"] == 7
    )


# --- the cap on concurrent downloads ------------------------------------------------


def test_the_cap_is_32_concurrent_downloads_and_all_slots_are_free_at_rest(
    stored: TestClient,
) -> None:
    assert routes.MAX_CONCURRENT_DOWNLOADS == 32
    assert _free_count(stored.app.state.download_slots) == 32


def test_a_request_over_the_cap_is_a_503_that_says_to_try_again_and_opens_no_storage(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots = stored.app.state.download_slots
    held = [slots.try_acquire() for _ in range(32)]
    assert all(held)
    opened: list[str] = []
    real = FsspecStorage.read_chunks

    def spy(self: FsspecStorage, path: str, **kw: Any) -> Iterator[bytes]:  # ruff: ignore[any-type]
        opened.append(path)
        return real(self, path, **kw)

    monkeypatch.setattr(FsspecStorage, "read_chunks", spy)
    busy = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert busy.status_code == 503
    assert busy.headers["Retry-After"] == "1"
    assert busy.json()["type"] == sdk_artifacts._BUSY_TYPE  # the SDK tells this 503 apart by it
    detail = busy.json()["detail"]
    assert "busy with other downloads" in detail
    assert "try again" in detail
    assert opened == []
    assert len(_downloads(stored)) == 0  # nothing was audited either
    for _ in range(32):
        slots.release()
    again = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert again.status_code == 200
    assert opened != []


def test_every_slot_is_back_after_a_download_a_refusal_and_a_cut(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots = stored.app.state.download_slots
    assert stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER).status_code == 200
    assert _free_count(slots) == 32
    for url, status in (
        (f"{_BASE}/versions/1/blobs/{'0' * 64}", 404),  # no such file in the version
        (f"{_BASE}/versions/9/blobs/{_SHA}", 404),  # no such version
        (f"{_BASE.replace('churn', 'ghost')}/versions/1/blobs/{_SHA}", 404),
    ):
        assert stored.get(url, headers=AUTH_HEADER).status_code == status
        assert _free_count(slots) == 32
    nobody = _token_for(stored, "nobody", [])
    assert stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=nobody).status_code == 403
    assert _free_count(slots) == 32
    monkeypatch.setattr(FsspecStorage, "read_chunks", lambda self, path, **kw: iter([b'{"fac']))
    with pytest.raises(routes.DownloadCut):
        stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert _free_count(slots) == 32


def test_a_storage_that_cannot_be_opened_gives_the_slot_back(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(self: FsspecStorage, path: str, **kw: Any) -> Iterator[bytes]:  # ruff: ignore[any-type]
        raise OSError(5, "refused")

    monkeypatch.setattr(FsspecStorage, "read_chunks", failing)
    assert stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER).status_code == 503
    assert _free_count(stored.app.state.download_slots) == 32


def test_a_download_holds_a_slot_while_its_body_is_sent(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def chunks(self: FsspecStorage, path: str, **kw: Any) -> Iterator[bytes]:  # ruff: ignore[any-type]
        seen.append(_free_count(stored.app.state.download_slots))
        yield b'{"factor": 2}\n'

    monkeypatch.setattr(FsspecStorage, "read_chunks", chunks)
    assert stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER).status_code == 200
    assert seen == [31]


def test_a_failed_audit_closes_the_storage_handle_that_was_opened(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _Probe(stored, _FILES["model.json"])
    opened = probe.chunks()
    monkeypatch.setattr(FsspecStorage, "read_chunks", lambda self, path, **kw: opened)

    def broken(self: ArtifactService, *args: Any) -> None:  # ruff: ignore[any-type]
        raise RuntimeError("the audit store is down")

    monkeypatch.setattr(ArtifactService, "_audit_download", broken)
    next(opened)  # started, as a real reader is once it is opened
    with pytest.raises(RuntimeError, match="audit store is down"):
        stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert probe.closed
    assert _free_count(stored.app.state.download_slots) == 32


# --- security properties that had no test --------------------------------------------------------


def test_no_transaction_is_open_while_the_body_of_the_second_file_of_a_version_is_sent(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit's dedupe path (an event exists already) must end its transaction, too."""
    assert stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER).status_code == 200
    assert len(_downloads(stored)) == 1
    probe = _Probe(stored, _FILES["docs/notes.txt"])
    monkeypatch.setattr(FsspecStorage, "read_chunks", lambda self, path, **kw: probe.chunks())
    got = stored.get(f"{_BASE}/versions/1/blobs/{_NOTES_SHA}", headers=AUTH_HEADER)
    assert got.content == _FILES["docs/notes.txt"]
    assert probe.checked_out == [0, 0]
    assert len(_downloads(stored)) == 1  # it was the dedupe path


def test_the_route_gives_the_response_the_deadline_of_the_files_size(
    stored: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class Spy(routes.DownloadResponse):
        def __init__(self, chunks: Iterator[bytes], **kwargs: Any) -> None:  # ruff: ignore[any-type]
            built.append(kwargs)
            super().__init__(chunks, **kwargs)

    monkeypatch.setattr(routes, "DownloadResponse", Spy)
    got = stored.get(f"{_BASE}/versions/1/blobs/{_SHA}", headers=AUTH_HEADER)
    assert got.status_code == 200
    size = len(_FILES["model.json"])
    assert [(k["size"], k["deadline"]) for k in built] == [(size, pytest.approx(60 + size / 65536))]


def test_an_unbound_task_token_does_not_see_the_run_identifiers_either(stored: TestClient) -> None:
    """The withholding is for every task token, not only for the ones a worker binds to a task."""
    _bound_token(stored, "gold.scores", {"m": "models.churn"}, "table")  # makes a task run
    ids = _stamp_producer(stored)
    minted = stored.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "admin",
            "environment": "dev",
            "asset_scope": ["models.*"],
            "ttl_seconds": 300,
        },
    )
    assert minted.status_code == 201, minted.text
    token = {"Authorization": f"Bearer {minted.json()['token']}"}
    person = stored.get(f"{_BASE}/versions/1", headers=AUTH_HEADER).json()
    assert {k: person[k] for k in _PRODUCER} == ids
    one = stored.get(f"{_BASE}/versions/1", headers=token)
    assert one.status_code == 200, one.text
    assert [one.json()[k] for k in _PRODUCER] == [None, None, None]
    assert one.json()["commit_sha"] == "b" * 40
    assert one.json()["created_by"] == "admin"
    listed = stored.get(f"{_BASE}/versions", headers=token).json()
    assert [[v[k] for k in _PRODUCER] for v in listed] == [[None] * 3, [None] * 3]


@pytest.mark.parametrize("suffix", ["", "/versions", "/versions/current"])
def test_a_nul_in_an_artifact_name_is_a_validation_error_not_a_server_error(
    client: TestClient, suffix: str
) -> None:
    """The name is refused before any query, so Postgres never sees the NUL (#71)."""
    create_environment(client)
    create_namespace(client, "dev", "models")
    response = client.get(
        f"{_ENV}/namespaces/models/artifacts/ch%00urn{suffix}", headers=AUTH_HEADER
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Table name must not contain a NUL byte."
