"""The three requests that add an artifact version."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from collections.abc import AsyncIterator, Coroutine
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import ClientDisconnect

from lakana.resources import artifacts as sdk_artifacts
from lakana_core.artifacts.service import (
    MAX_OPEN_UPLOADS,
    STORAGE_NOT_WRITTEN,
    UPLOAD_GONE,
    ArtifactService,
    BlobUpload,
)
from lakana_core.errors import ConflictError, ValidationError
from lakana_core.storage.base import StreamWriter
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_server.api.platform import artifacts as routes
from lakana_server.api.platform.schemas import ArtifactBlobOut, ArtifactCommitIn
from lakana_server.concurrency import AppendConcurrencyLimiter
from lakana_server.errors import (
    AppendConcurrencyLimitError,
    AppendReadTimeoutError,
    ClientDisconnectedError,
)
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import (
    create_environment,
    create_namespace,
    create_table,
    upload_artifact,
)

_BASE = "/api/v1/environments/dev/namespaces/models/artifacts/churn"
_MODEL = b'{"factor": 2}\n'
_SHA = hashlib.sha256(_MODEL).hexdigest()
_DOC = {
    "format": 1,
    "is_directory": False,
    "files": [{"path": "model.json", "size": len(_MODEL), "sha256": _SHA}],
}


@pytest.fixture
def ready(client: TestClient) -> TestClient:
    """Provide a client whose `dev` environment has the namespace `models`.

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
    return client


def test_three_requests_add_a_version(ready: TestClient) -> None:
    begun = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC)
    assert begun.status_code == 201
    body = begun.json()
    assert body["missing"] == [_SHA]
    assert set(body) == {"upload_id", "missing", "expires_at"}
    blob = f"{_BASE}/uploads/{body['upload_id']}/blobs/{_SHA}"
    first = ready.put(blob, headers=AUTH_HEADER, content=iter([_MODEL[:4], _MODEL[4:]]))
    assert (first.status_code, first.json()) == (200, {"sha256": _SHA, "stored": True})
    again = ready.put(blob, headers=AUTH_HEADER, content=_MODEL)
    assert again.json() == {"sha256": _SHA, "stored": False}
    committed = ready.post(
        f"{_BASE}/uploads/{body['upload_id']}/commit", headers=AUTH_HEADER, json={"sources": []}
    )
    assert committed.status_code == 201
    version = committed.json()
    assert (version["version"], version["size_bytes"], version["is_directory"]) == (1, 14, False)
    assert version["files"] == [{"path": "model.json", "size": 14, "sha256": _SHA}]
    assert (version["created_by"], version["deleted_versions"], version["task_run_id"]) == (
        "admin",
        [],
        None,
    )
    assert version["manifest_sha256"] == (
        "b97616016d0d6bd401c61c158cbf7405bc346d3d6ee6c22f8ed4099a3fe87c81"
    )
    twice = ready.post(f"{_BASE}/uploads/{body['upload_id']}/commit", headers=AUTH_HEADER, json={})
    # A commit is idempotent per upload; the repeat returns the same version.
    assert twice.status_code == 201
    assert twice.json() == version


def test_a_second_version_needs_no_file_the_store_has(ready: TestClient) -> None:
    upload_artifact(ready, "dev", "models.churn", {"model.json": _MODEL}, is_directory=False)
    begun = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC)
    assert begun.json()["missing"] == []
    committed = ready.post(
        f"{_BASE}/uploads/{begun.json()['upload_id']}/commit", headers=AUTH_HEADER, json={}
    )
    assert committed.json()["version"] == 2


@pytest.mark.parametrize(
    ("content", "status", "fragment"),
    [
        (_MODEL + b"extra", 422, "is longer than the 14 bytes its manifest declares"),
        (_MODEL[:-1], 422, "size or SHA-256 differs"),
        (b"x" * 14, 422, "size or SHA-256 differs"),
        (b"", 422, "size or SHA-256 differs"),
    ],
)
def test_a_wrong_body_is_refused_and_nothing_is_stored(
    ready: TestClient, content: bytes, status: int, fragment: str
) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    sent = ready.put(
        f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=content
    )
    assert sent.status_code == status
    assert fragment in sent.json()["detail"]
    commit = ready.post(f"{_BASE}/uploads/{upload_id}/commit", headers=AUTH_HEADER, json={})
    assert commit.status_code == 409
    assert "1 of this upload's files are not stored yet" in commit.json()["detail"]


def test_a_hash_or_an_upload_that_is_not_one_is_refused(ready: TestClient) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    other = "0" * 64
    for url, status in (
        (f"{_BASE}/uploads/{upload_id}/blobs/{other}", 422),
        (f"{_BASE}/uploads/{upload_id}/blobs/{_SHA.upper()}", 422),
        (f"{_BASE}/uploads/not-a-uuid/blobs/{_SHA}", 422),
        (f"{_BASE}/uploads/00000000-0000-0000-0000-000000000000/blobs/{_SHA}", 409),
    ):
        assert ready.put(url, headers=AUTH_HEADER, content=_MODEL).status_code == status, url


def test_a_manifest_must_be_a_json_object_within_the_size_bound(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for content in (b"{", b"[1]", b'"x"', b"\xff"):
        bad = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, content=content)
        assert bad.status_code == 422
        assert bad.json()["detail"] == "The request body must be the manifest as a JSON object."
    wrong = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json={"format": 1})
    assert wrong.status_code == 422
    assert "exactly the keys files, format and is_directory" in wrong.json()["detail"]
    monkeypatch.setattr(routes, "MAX_MANIFEST_REQUEST_BYTES", 10)
    large = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC)
    assert large.status_code == 413
    assert large.json()["detail"] == (
        "The manifest request is larger than 10 bytes. It lists every file with its path and "
        "hash: store fewer files or give them shorter paths (the limit is fixed)."
    )


def test_a_limit_from_the_settings_is_named(ready: TestClient) -> None:
    ready.app.state.settings.artifacts.max_file_bytes = 5
    refused = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC)
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "model.json is 14 bytes, more than LAKANA_ARTIFACTS__MAX_FILE_BYTES (5). "
        "Store a smaller file, or ask the operator to raise the setting."
    )


def test_an_upload_takes_an_append_slot(ready: TestClient) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    limiter = ready.app.state.append_concurrency_limiter
    held = 0
    while limiter.try_acquire():
        held += 1
    try:
        busy = ready.put(
            f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL
        )
        assert (busy.status_code, busy.headers["Retry-After"]) == (503, "1")
        assert busy.json()["detail"] == "Too many concurrent uploads; retry shortly."
        # the SDK sends a file again only for exactly this answer, so the type is pinned here
        assert busy.json()["type"] == sdk_artifacts._UPLOAD_BUSY_TYPE
    finally:
        for _ in range(held):
            limiter.release()
    ok = ready.put(f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL)
    assert ok.status_code == 200


def test_a_stalled_or_vanished_client_ends_the_upload() -> None:
    async def never() -> AsyncIterator[bytes]:
        await asyncio.sleep(3600)
        yield b""

    async def vanished() -> AsyncIterator[bytes]:  # ruff: ignore[unused-async]
        raise ClientDisconnect
        yield b""

    async def one() -> AsyncIterator[bytes]:  # ruff: ignore[unused-async]
        yield b"x"

    with pytest.raises(AppendReadTimeoutError, match=r"No bytes arrived for 0.01s"):
        asyncio.run(routes.next_chunk(never().__aiter__(), 0.01))
    with pytest.raises(ClientDisconnectedError):
        asyncio.run(routes.next_chunk(vanished().__aiter__(), 1.0))

    async def both() -> tuple[bytes | None, bytes | None]:
        iterator = one().__aiter__()
        return await routes.next_chunk(iterator, 1.0), await routes.next_chunk(iterator, 1.0)

    assert asyncio.run(both()) == (b"x", None)


def test_a_failure_while_the_body_arrives_leaves_nothing(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]

    async def stalled(_iterator: object, idle: float) -> bytes | None:  # ruff: ignore[unused-async]
        raise AppendReadTimeoutError(f"No bytes arrived for {idle}s; the upload was abandoned.")

    monkeypatch.setattr(routes, "next_chunk", stalled)
    sent = ready.put(
        f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL
    )
    assert sent.status_code == 408
    monkeypatch.undo()
    assert ready.put(
        f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL
    ).json() == {"sha256": _SHA, "stored": True}


def test_the_name_must_be_free_and_the_namespace_exist(ready: TestClient) -> None:
    create_table(ready, "dev", "gold", "taken")
    taken = ready.post(
        "/api/v1/environments/dev/namespaces/gold/artifacts/taken/uploads",
        headers=AUTH_HEADER,
        json=_DOC,
    )
    assert taken.status_code == 422
    assert taken.json()["detail"] == (
        "gold.taken is a table, not an artifact; drop it first, or choose another name."
    )
    nowhere = ready.post(
        "/api/v1/environments/dev/namespaces/nowhere/artifacts/m/uploads",
        headers=AUTH_HEADER,
        json=_DOC,
    )
    assert nowhere.status_code == 404
    unauthenticated = ready.post(f"{_BASE}/uploads", json=_DOC)
    assert unauthenticated.status_code == 401


_SURROGATE_BODY = b'{"sources": [{"name": "a.\\ud800b"}]}'
"""A JSON text whose string holds a lone surrogate, which no UTF-8 response can carry."""


def test_a_lone_surrogate_in_a_commit_body_is_a_422_not_a_500(ready: TestClient) -> None:
    """A-I5: FastAPI's own handler failed to encode its answer; the body is never echoed."""
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    refused = ready.post(
        f"{_BASE}/uploads/{upload_id}/commit",
        headers={**AUTH_HEADER, "Content-Type": "application/json"},
        content=_SURROGATE_BODY,
    )
    assert refused.status_code == 422, refused.text
    assert isinstance(refused.json()["detail"], list)
    assert "input" not in refused.text


def test_a_source_is_recorded_and_must_be_readable(ready: TestClient) -> None:
    create_table(ready, "dev", "gold", "rows")
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    ready.put(f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL)
    missing = ready.post(
        f"{_BASE}/uploads/{upload_id}/commit",
        headers=AUTH_HEADER,
        json={"sources": [{"name": "gold.nope"}]},
    )
    assert missing.status_code == 404
    bad = ready.post(
        f"{_BASE}/uploads/{upload_id}/commit",
        headers=AUTH_HEADER,
        json={"sources": [{"name": "gold.rows", "version": 0}]},
    )
    assert bad.status_code == 422
    done = ready.post(
        f"{_BASE}/uploads/{upload_id}/commit",
        headers=AUTH_HEADER,
        json={"sources": [{"name": "gold.rows"}]},
    )
    assert done.status_code == 201
    lineage = ready.get(
        "/api/v1/environments/dev/namespaces/models/tables/churn/lineage", headers=AUTH_HEADER
    )
    assert lineage.status_code == 200
    assert {(n["name"], n["kind"]) for n in lineage.json()["nodes"]} == {
        ("models.churn", "ARTIFACT"),
        ("gold.rows", "TABLE"),
    }


# --- types at the boundary (point A) -------------------------------------------------------


def _document_with_size(digits: int) -> bytes:
    """Write a manifest whose only size is an integer of `digits` digits.

    Parameters
    ----------
    digits
        Its length.

    Returns
    -------
    bytes
        The JSON body.
    """
    return b'{"format":1,"is_directory":false,"files":[{"path":"m","sha256":"%s","size":%s}]}' % (
        _SHA.encode(),
        b"9" * digits,
    )


@pytest.mark.parametrize("digits", [30, 4000, 5000, 100_000])
def test_a_huge_integer_in_a_manifest_is_a_validation_error_not_a_500(
    ready: TestClient, digits: int
) -> None:
    """Python refuses to read an integer of more than 4300 digits (a ValueError)."""
    refused = ready.post(
        f"{_BASE}/uploads", headers=AUTH_HEADER, content=_document_with_size(digits)
    )
    assert refused.status_code == 422, refused.text
    assert isinstance(refused.json()["detail"], str)


@pytest.mark.parametrize("digits", [10, 4000, 5000, 100_000])
def test_a_huge_integer_in_a_source_version_is_refused_not_a_500(
    ready: TestClient, digits: int
) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    body = b'{"sources":[{"name":"gold.rows","version":%s}]}' % (b"9" * digits)
    refused = ready.post(
        f"{_BASE}/uploads/{upload_id}/commit",
        headers={**AUTH_HEADER, "Content-Type": "application/json"},
        content=body,
    )
    # FastAPI answers 400 for a body Python cannot decode (over 4300 digits), 422 otherwise.
    assert refused.status_code == (400 if digits > 4300 else 422), refused.text


@pytest.mark.parametrize(
    "sources",
    [
        [{"name": "gold.rows", "version": 2**31}],
        [{"name": "gold.rows", "version": True}],
        [{"name": "gold.rows", "version": 1.5}],
        [{"name": "gold.rows", "version": "1"}],
        [{"name": ""}],
        [{"name": "x" * 1281}],
        [{"name": "gold.rows"}, {"name": "gold.rows", "version": 2}],
        [{"name": f"gold.t{n}"} for n in range(65)],
        [{"name": "gold.rows", "unknown": 1}],
    ],
)
def test_a_malformed_source_list_is_refused_before_the_service(
    ready: TestClient, sources: list[dict[str, object]]
) -> None:
    """Bounded to 64 and de-duplicated by the schema; the service agrees (point B)."""
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    refused = ready.post(
        f"{_BASE}/uploads/{upload_id}/commit", headers=AUTH_HEADER, json={"sources": sources}
    )
    assert refused.status_code == 422, refused.text


@pytest.mark.slow
def test_sixty_four_sources_are_accepted_by_the_schema_and_the_service(ready: TestClient) -> None:
    for n in range(64):
        create_table(ready, "dev", "gold", f"t{n}")
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    ready.put(f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL)
    done = ready.post(
        f"{_BASE}/uploads/{upload_id}/commit",
        headers=AUTH_HEADER,
        json={"sources": [{"name": f"gold.t{n}"} for n in range(64)]},
    )
    assert done.status_code == 201, done.text


# --- the manifest body's cap (point C) -----------------------------------------------------


def test_a_manifest_body_without_a_length_is_cut_off_while_it_streams(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes, "MAX_MANIFEST_REQUEST_BYTES", 20)
    pieces = [b'{"format":1,"is', b'_directory":false,', b'"files":[]}']
    refused = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, content=iter(pieces))
    assert refused.status_code == 413
    assert refused.json()["detail"].startswith("The manifest request is larger than 20 bytes. ")


def test_an_oversized_declared_length_is_refused_before_a_byte_is_read(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forbidden(_iterator: object, _idle: float) -> bytes | None:  # ruff: ignore[unused-async]
        raise AssertionError("the body must not be read")

    monkeypatch.setattr(routes, "next_chunk", forbidden)
    refused = ready.post(
        f"{_BASE}/uploads",
        headers={**AUTH_HEADER, "Content-Length": str(routes.MAX_MANIFEST_REQUEST_BYTES + 1)},
        content=b"{}",
    )
    assert refused.status_code == 413


# --- the streamed body (point C) -----------------------------------------------------------


class _Blob:
    """Record what the route does to a `BlobUpload`.

    Parameters
    ----------
    fail_write
        Raise this on the first write.
    fail_finish
        Raise this on finish.
    fail_abort
        Raise this on abort.
    left
        What `seconds_left` answers, one value per call; the last one repeats.
    """

    def __init__(
        self,
        fail_write: Exception | None = None,
        fail_finish: Exception | None = None,
        fail_abort: Exception | None = None,
        left: tuple[float, ...] = (3600.0,),
    ) -> None:
        self.fail_write = fail_write
        self.fail_finish = fail_finish
        self.fail_abort = fail_abort
        self.left = list(left)
        self.events: list[str] = []
        self.sizes: list[int] = []

    def write(self, data: bytes) -> None:
        self.events.append("write")
        self.sizes.append(len(data))
        if self.fail_write is not None:
            raise self.fail_write

    def finish(self) -> bool:
        self.events.append("finish")
        if self.fail_finish is not None:
            raise self.fail_finish
        return True

    def abort(self) -> None:
        self.events.append("abort")
        if self.fail_abort is not None:
            raise self.fail_abort

    def seconds_left(self) -> float:
        return self.left.pop(0) if len(self.left) > 1 else self.left[0]


class _Service:
    """A service whose `receive_blob` hands out one recording blob."""

    def __init__(self, blob: _Blob) -> None:
        self.blob = blob

    def receive_blob(self, *_args: object) -> _Blob:
        return self.blob


def _call_put_blob(
    blob: _Blob, pieces: list[bytes | BaseException], *, slots: int = 1
) -> tuple[object, int]:
    """Run the route function on a fake request that streams `pieces`.

    Parameters
    ----------
    blob
        What the service hands out.
    pieces
        Chunks of the body; an exception in the list is raised at that point.
    slots
        Slots of the limiter.

    Returns
    -------
    tuple
        The route's result or the exception it raised, and the slots free afterwards.
    """
    limiter = AppendConcurrencyLimiter(slots)

    async def stream() -> AsyncIterator[bytes]:  # ruff: ignore[unused-async]
        for piece in pieces:
            if isinstance(piece, BaseException):
                raise piece
            yield piece

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                append_concurrency_limiter=limiter,
                settings=SimpleNamespace(append=SimpleNamespace(read_timeout_seconds=5.0)),
            )
        ),
        stream=stream,
    )

    async def run() -> object:
        try:
            return await routes.put_blob(
                request,  # ty: ignore[invalid-argument-type]
                "dev",
                "models",
                "churn",
                uuid.uuid4(),
                _SHA,
                None,  # ty: ignore[invalid-argument-type]
                _Service(blob),  # ty: ignore[invalid-argument-type]
            )
        except BaseException as exc:
            return exc

    result = asyncio.run(run())
    free = 0
    while limiter.try_acquire():
        free += 1
    return result, free


def test_the_body_goes_to_the_blob_in_pieces_and_is_finished_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "PIECE_BYTES", 4)
    blob = _Blob()
    result, free = _call_put_blob(blob, [b"ab", b"cd", b"efg", b"h", b"i"])
    assert result == ArtifactBlobOut(sha256=_SHA, stored=True)
    assert blob.sizes == [4, 4, 1]  # never more than PIECE_BYTES plus one ASGI chunk pending
    assert blob.events == ["write", "write", "write", "finish"]
    assert free == 1


def test_a_failed_write_aborts_and_never_finishes() -> None:
    blob = _Blob(fail_write=ValidationError("too long"))
    result, free = _call_put_blob(blob, [b"abc", b"def"])
    assert isinstance(result, ValidationError)
    assert blob.events == ["write", "abort"]
    assert free == 1


def test_a_failed_finish_aborts_and_releases_the_slot() -> None:
    blob = _Blob(fail_finish=ConflictError("gone"))
    result, free = _call_put_blob(blob, [b"abc"])
    assert isinstance(result, ConflictError)
    assert blob.events == ["write", "finish", "abort"]
    assert free == 1


def test_a_client_that_disconnects_mid_body_aborts_the_file() -> None:
    blob = _Blob()
    result, free = _call_put_blob(blob, [b"abc", ClientDisconnect()])
    assert isinstance(result, ClientDisconnectedError)
    assert blob.events == ["write", "abort"] or blob.events == ["abort"]
    assert "finish" not in blob.events
    assert free == 1


def test_a_cancelled_request_still_aborts_the_file() -> None:
    blob = _Blob()
    limiter = AppendConcurrencyLimiter(1)

    async def stream() -> AsyncIterator[bytes]:
        yield b"abc"
        await asyncio.sleep(3600)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                append_concurrency_limiter=limiter,
                settings=SimpleNamespace(append=SimpleNamespace(read_timeout_seconds=3600.0)),
            )
        ),
        stream=stream,
    )

    async def run() -> None:
        task = asyncio.ensure_future(
            routes.put_blob(
                request,  # ty: ignore[invalid-argument-type]
                "dev",
                "models",
                "churn",
                uuid.uuid4(),
                _SHA,
                None,  # ty: ignore[invalid-argument-type]
                _Service(blob),  # ty: ignore[invalid-argument-type]
            )
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert "abort" in blob.events
    assert "finish" not in blob.events
    assert limiter.try_acquire()


@pytest.mark.parametrize("content", [b"", _MODEL[:3], _MODEL + b"!"])
def test_the_manifest_decides_whatever_content_length_says(
    ready: TestClient, content: bytes
) -> None:
    """The route trusts neither the header nor the client: size and hash are the manifest's."""
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    sent = ready.put(
        f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}",
        headers={**AUTH_HEADER, "Content-Length": str(len(_MODEL))},
        content=content,
    )
    assert sent.status_code == 422
    stored = ready.put(
        f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL
    )
    assert stored.json() == {"sha256": _SHA, "stored": True}


def test_a_body_without_a_length_is_accepted(ready: TestClient) -> None:
    """Chunked transfer encoding, as the SDK sends it."""
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    sent = ready.put(
        f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}",
        headers=AUTH_HEADER,
        content=iter([_MODEL[:1], _MODEL[1:7], _MODEL[7:]]),
    )
    assert sent.json() == {"sha256": _SHA, "stored": True}


def test_a_body_sent_to_a_hash_that_is_not_in_the_manifest_is_refused(ready: TestClient) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    other = hashlib.sha256(b"other").hexdigest()
    sent = ready.put(
        f"{_BASE}/uploads/{upload_id}/blobs/{other}", headers=AUTH_HEADER, content=b"other"
    )
    assert sent.status_code == 422
    assert sent.json()["detail"] == "This upload's manifest names no file with that SHA-256."


def test_the_slot_is_released_after_every_outcome(ready: TestClient) -> None:
    limiter = ready.app.state.append_concurrency_limiter
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    for content in (b"short", _MODEL):
        ready.put(f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=content)
    held = 0
    while limiter.try_acquire():
        held += 1
    assert held == ready.app.state.settings.append.max_concurrent
    for _ in range(held):
        limiter.release()


# --- error mapping (point E) ---------------------------------------------------------------


def test_the_cap_on_open_uploads_is_a_conflict(ready: TestClient) -> None:
    for _ in range(MAX_OPEN_UPLOADS):
        assert ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).status_code == 201
    capped = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC)
    assert capped.status_code == 409
    assert "16 uploads in progress" in capped.json()["detail"]


def test_no_storage_path_reaches_a_response(ready: TestClient, storage_root: str) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    answers = [
        ready.put(f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=b"x"),
        ready.post(f"{_BASE}/uploads/{upload_id}/commit", headers=AUTH_HEADER, json={}),
        ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json={"format": 1, "files": ["../x"]}),
    ]
    for answer in answers:
        assert storage_root not in answer.text
        assert "artifacts/" not in answer.text


def test_a_hostile_path_in_a_manifest_is_echoed_only_escaped(ready: TestClient) -> None:
    document = {
        "format": 1,
        "is_directory": True,
        "files": [{"path": "a‮b/../x", "size": 1, "sha256": _SHA}],
    }
    refused = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=document)
    assert refused.status_code == 422
    assert "‮" not in refused.text


def test_the_schema_alone_refuses_a_duplicate_source_and_a_bad_hash_before_the_service(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's types are its own defence; the service's checks are a second one."""
    with pytest.raises(PydanticValidationError, match="name each source once"):
        ArtifactCommitIn.model_validate(
            {"sources": [{"name": "a.b"}, {"name": "a.b", "version": 2}]}
        )

    def reached(*_args: object) -> None:
        raise AssertionError("the service must not be reached")

    monkeypatch.setattr(ArtifactService, "receive_blob", reached)
    upload_id = str(uuid.uuid4())
    for bad in (_SHA.upper(), _SHA[:-1], _SHA + "0", "g" * 64):
        sent = ready.put(
            f"{_BASE}/uploads/{upload_id}/blobs/{bad}", headers=AUTH_HEADER, content=_MODEL
        )
        assert sent.status_code == 422, bad


# --- a trickling upload, expiry
# -----------------------------------------------------------------------------


def test_a_trickling_upload_ends_when_its_upload_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I-1: the file's body is bounded by its upload's life, whatever the pace of the bytes."""
    monkeypatch.setattr(routes, "PIECE_BYTES", 1)
    blob = _Blob(left=(10.0, 10.0, 0.0))
    result, free = _call_put_blob(blob, [b"a", b"b", RuntimeError("read after expiry")])
    assert isinstance(result, ConflictError)
    assert str(result) == UPLOAD_GONE
    assert blob.events == ["write", "write", "abort"]  # the third piece was never taken
    assert "finish" not in blob.events
    assert free == 1  # the slot is back


def test_an_upload_that_expires_while_the_server_waits_for_a_byte_is_gone_not_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait for a byte is cut at the expiry, not at the idle timeout; nothing really waits."""
    asked: list[float] = []

    async def instant_timeout(_iterator: object, idle: float) -> bytes | None:  # ruff: ignore[unused-async]
        asked.append(idle)
        raise AppendReadTimeoutError("no bytes")

    monkeypatch.setattr(routes, "next_chunk", instant_timeout)

    async def never() -> AsyncIterator[bytes]:  # ruff: ignore[unused-async]
        raise AssertionError("the stream is read through next_chunk")
        yield b""

    blob = _Blob(left=(0.05, 0.0))
    result = asyncio.run(_wait_for_a_file_chunk(blob, never()))
    assert isinstance(result, ConflictError)
    assert str(result) == UPLOAD_GONE
    assert asked == [0.05]  # the idle timeout in use is 300 s


async def _wait_for_a_file_chunk(blob: _Blob, stream: AsyncIterator[bytes]) -> BaseException:
    """Run the route's wait for a piece and return what it raised.

    Parameters
    ----------
    blob
        The fake file.
    stream
        The body.

    Returns
    -------
    BaseException
        The error, or an `AssertionError` when none was raised.
    """
    try:
        await routes._next_file_chunk(blob, stream.__aiter__(), 300.0)  # ty: ignore[invalid-argument-type]
    except BaseException as exc:
        return exc
    return AssertionError("nothing was raised")


def test_a_still_valid_upload_that_hears_nothing_is_a_timeout() -> None:
    async def never() -> AsyncIterator[bytes]:
        await asyncio.sleep(3600)
        yield b""

    async def run() -> BaseException:
        try:
            await routes._next_file_chunk(_Blob(left=(0.05,)), never().__aiter__(), 0.01)  # ty: ignore[invalid-argument-type]
        except BaseException as exc:
            return exc
        return AssertionError("nothing was raised")

    assert isinstance(asyncio.run(run()), AppendReadTimeoutError)


def test_an_expired_upload_over_http_is_the_answer_of_an_expired_upload(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    monkeypatch.setattr(BlobUpload, "seconds_left", lambda _self: 0.0)
    gone = ready.put(
        f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL
    )
    assert (gone.status_code, gone.json()["detail"]) == (409, UPLOAD_GONE)
    monkeypatch.undo()
    ok = ready.put(f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL)
    assert ok.json() == {"sha256": _SHA, "stored": True}


def test_a_failing_abort_does_not_replace_the_error_that_made_it_necessary() -> None:
    blob = _Blob(fail_write=ValidationError("too long"), fail_abort=RuntimeError("storage down"))
    result, free = _call_put_blob(blob, [b"abc"])
    assert isinstance(result, ValidationError)
    assert str(result) == "too long"
    assert free == 1


def test_storage_that_cannot_be_written_is_a_503_not_a_500(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]

    def failing(self: FsspecStorage, path: str) -> StreamWriter:
        raise OSError(28, "/secret/bucket/path: No space left on device")

    monkeypatch.setattr(FsspecStorage, "open_write", failing)
    full = ready.put(
        f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL
    )
    assert full.status_code == 503
    assert full.headers["Retry-After"] == "5"
    assert full.json()["detail"] == STORAGE_NOT_WRITTEN
    assert "secret" not in full.text and "/" not in full.json()["detail"].replace("and/or", "")
    monkeypatch.undo()
    ok = ready.put(f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL)
    assert ok.status_code == 200


def test_a_write_that_fails_part_way_is_a_503_and_leaves_the_upload_usable(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    real = FsspecStorage.open_write

    class Full:
        """A writer whose first write fails."""

        def write(self, data: bytes) -> None:
            raise OSError(28, "No space left on device")

        def abort(self) -> None:
            return None

    monkeypatch.setattr(FsspecStorage, "open_write", lambda self, path: Full())
    full = ready.put(
        f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL
    )
    assert full.status_code == 503
    monkeypatch.setattr(FsspecStorage, "open_write", real)
    ok = ready.put(f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL)
    assert ok.status_code == 200


# --- authorization before the manifest is read (I-3) ------------------------------------------


def _user(client: TestClient, name: str, role: str | None) -> dict[str, str]:
    """Create a principal, grant it a role on `dev` if given, and return its auth header.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    role
        A role to grant on the environment, or `None` for no grant at all.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": "USER"})
    if role is not None:
        granted = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={"principal": name, "role": role, "kind": "ENVIRONMENT", "environment": "dev"},
        )
        assert granted.status_code == 201, granted.text
    token = client.post(
        f"/api/v1/principals/{name}/tokens", headers=AUTH_HEADER, json={"name": name}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_a_caller_without_write_is_refused_before_the_manifest_is_read(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    nobody = _user(ready, "nobody", None)

    async def forbidden(*_args: object) -> bytes | None:  # ruff: ignore[unused-async]
        raise AssertionError("the body must not be read for a caller who may not write")

    monkeypatch.setattr(routes, "next_chunk", forbidden)
    refused = ready.post(f"{_BASE}/uploads", headers=nobody, json=_DOC)
    assert refused.status_code == 403
    monkeypatch.undo()
    assert ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).status_code == 201


def test_a_manifest_request_over_the_decode_limit_is_busy_and_reads_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-I4: with every slot held, a further request is refused before it reads a byte."""
    release = threading.Event()
    lock = threading.Lock()
    started: list[int] = []

    def held(body: bytes) -> dict[str, object]:
        with lock:
            started.append(len(started) + 1)
        assert release.wait(5.0), "the test never released the decodes"
        return {"files": []}

    monkeypatch.setattr(routes, "_decode_document", held)
    streamed: list[str] = []

    async def one_piece() -> AsyncIterator[bytes]:  # ruff: ignore[unused-async]
        streamed.append("read")
        yield b"{}"

    request = SimpleNamespace(
        headers={},
        stream=one_piece,
        app=SimpleNamespace(
            state=SimpleNamespace(
                manifest_decode_slots=asyncio.Semaphore(routes.MANIFEST_DECODE_SLOTS),
                manifest_principals=set(),
                settings=SimpleNamespace(append=SimpleNamespace(read_timeout_seconds=5.0)),
            )
        ),
    )

    async def run() -> tuple[list[str], list[object]]:
        async def read() -> dict[str, object]:
            # Each call is another principal, so only the global bound can refuse one.
            return await routes._read_manifest_document(request, 5.0, uuid.uuid4())  # ty: ignore[invalid-argument-type]

        async with asyncio.timeout(10.0):
            tasks = [asyncio.ensure_future(read()) for _ in range(routes.MANIFEST_DECODE_SLOTS)]
            try:
                while len(started) < routes.MANIFEST_DECODE_SLOTS:
                    await asyncio.sleep(0.005)
                before = list(streamed)
                with pytest.raises(AppendConcurrencyLimitError) as busy:
                    await read()
                assert str(busy.value) == "Too many concurrent uploads; retry shortly."
                after = list(streamed)
            finally:
                release.set()
            return [*before, *after], list(await asyncio.gather(*tasks))

    streams, results = asyncio.run(run())
    assert streams == ["read", "read"] * 2  # the refused request added nothing to either list
    assert len(started) == routes.MANIFEST_DECODE_SLOTS == 2
    assert results == [{"files": []}] * 2
    slots = request.app.state.manifest_decode_slots
    assert slots._value == routes.MANIFEST_DECODE_SLOTS  # every slot is back


def test_a_busy_manifest_request_is_a_503_with_retry_after_over_http(ready: TestClient) -> None:
    """The route answers like the blob route does when the slots are taken."""
    slots = ready.app.state.manifest_decode_slots
    for _ in range(routes.MANIFEST_DECODE_SLOTS):
        slots._value -= 1
    try:
        busy = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC)
    finally:
        slots._value = routes.MANIFEST_DECODE_SLOTS
    assert busy.status_code == 503
    assert busy.headers["Retry-After"] == "1"
    assert busy.json()["type"] == "AppendConcurrencyLimitError"
    assert busy.json()["detail"] == "Too many concurrent uploads; retry shortly."
    assert ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).status_code == 201


def test_the_application_creates_the_real_decode_limit(client: TestClient) -> None:
    """The limit the route waits on is the one the application builds, with the constant's size."""
    slots = client.app.state.manifest_decode_slots
    assert isinstance(slots, asyncio.Semaphore)
    assert slots._value == routes.MANIFEST_DECODE_SLOTS == 2


# --- one principal holds at most one manifest slot --------------------------------

_BUSY_TEXT = "Too many concurrent uploads; retry shortly."


def _manifest_request(stream: object, read_timeout: float = 5.0) -> SimpleNamespace:
    """Build a stand-in request with fresh manifest limits and the given body stream.

    Parameters
    ----------
    stream
        An async generator function yielding the body.
    read_timeout
        The idle and total read timeout in seconds.

    Returns
    -------
    SimpleNamespace
        An object with the attributes `_read_manifest_document` reads.
    """
    return SimpleNamespace(
        headers={},
        stream=stream,
        app=SimpleNamespace(
            state=SimpleNamespace(
                manifest_decode_slots=asyncio.Semaphore(routes.MANIFEST_DECODE_SLOTS),
                manifest_principals=set(),
                settings=SimpleNamespace(append=SimpleNamespace(read_timeout_seconds=read_timeout)),
            )
        ),
    )


def _with_state_of(request: SimpleNamespace, stream: object) -> SimpleNamespace:
    """Build a request that shares another one's limits but has its own body stream.

    Parameters
    ----------
    request
        The request whose application state is shared.
    stream
        An async generator function yielding the new request's body.

    Returns
    -------
    SimpleNamespace
        The new request.
    """
    return SimpleNamespace(headers={}, stream=stream, app=request.app)


def _read(
    request: SimpleNamespace, principal_id: uuid.UUID
) -> Coroutine[object, object, dict[str, object]]:
    """Start reading a manifest as a principal.

    Parameters
    ----------
    request
        The stand-in request.
    principal_id
        The reading principal.

    Returns
    -------
    object
        The coroutine.
    """
    return routes._read_manifest_document(request, 5.0, principal_id)  # ty: ignore[invalid-argument-type]


def test_a_principal_with_a_manifest_in_progress_cannot_begin_another() -> None:
    """A holds a slot with a stalled body: A's second begin is refused, B's proceeds."""
    stall = asyncio.Event
    gate: list[asyncio.Event] = []

    async def stalled() -> AsyncIterator[bytes]:
        gate.append(stall())
        yield b"{"
        await gate[0].wait()
        yield b"}"

    async def whole() -> AsyncIterator[bytes]:  # ruff: ignore[unused-async]
        yield b"{}"

    first = _manifest_request(stalled)
    a, b = uuid.uuid4(), uuid.uuid4()
    state = first.app.state

    async def run() -> None:
        async with asyncio.timeout(10.0):
            held = asyncio.ensure_future(_read(first, a))
            while not gate or len(state.manifest_principals) == 0:
                await asyncio.sleep(0.005)
            assert state.manifest_principals == {a}
            with pytest.raises(AppendConcurrencyLimitError) as busy:
                await _read(_with_state_of(first, whole), a)
            assert str(busy.value) == _BUSY_TEXT
            assert state.manifest_principals == {a}  # the refusal released nothing of A's
            assert state.manifest_decode_slots._value == routes.MANIFEST_DECODE_SLOTS - 1
            assert await _read(_with_state_of(first, whole), b) == {}
            gate[0].set()
            assert await held == {}
            assert await _read(_with_state_of(first, whole), a) == {}

    asyncio.run(run())
    assert state.manifest_principals == set()
    assert state.manifest_decode_slots._value == routes.MANIFEST_DECODE_SLOTS


@pytest.mark.parametrize("ending", ["decode-error", "over-limit", "exception", "cancelled"])
def test_a_manifest_request_releases_its_principal_and_its_slot_together(
    ending: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """However the read ends, the principal and the global slot are free again."""
    hang = asyncio.Event

    async def body() -> AsyncIterator[bytes]:
        if ending == "decode-error":
            yield b"not json"
        elif ending == "over-limit":
            yield b"{" + b" " * 64
        elif ending == "exception":
            yield b"{"
            raise RuntimeError("the connection broke")
        else:
            yield b"{"
            await hang().wait()

    async def whole() -> AsyncIterator[bytes]:  # ruff: ignore[unused-async]
        yield b"{}"

    if ending == "over-limit":
        monkeypatch.setattr(routes, "MAX_MANIFEST_REQUEST_BYTES", 16)
    request = _manifest_request(body)
    state = request.app.state
    a = uuid.uuid4()

    async def run() -> None:
        async with asyncio.timeout(10.0):
            task = asyncio.ensure_future(_read(request, a))
            if ending == "cancelled":
                while not state.manifest_principals:
                    await asyncio.sleep(0.005)
                task.cancel()
            with pytest.raises(
                (ValidationError, routes.PayloadTooLargeError, RuntimeError, asyncio.CancelledError)
            ) as ended:
                await task
            assert (ending == "cancelled") == isinstance(ended.value, asyncio.CancelledError)
            assert state.manifest_principals == set()
            assert state.manifest_decode_slots._value == routes.MANIFEST_DECODE_SLOTS
            assert await _read(_with_state_of(request, whole), a) == {}

    asyncio.run(run())
    assert state.manifest_principals == set()
    assert state.manifest_decode_slots._value == routes.MANIFEST_DECODE_SLOTS


def test_a_refused_second_manifest_is_the_same_503_over_http(ready: TestClient) -> None:
    """The principal's busy answer matches the global one: status, header, type and text."""

    class Everyone(set):  # type: ignore[type-arg]
        """A set that claims every principal already has a manifest in progress."""

        def __contains__(self, item: object) -> bool:
            return True

    real = ready.app.state.manifest_principals
    ready.app.state.manifest_principals = Everyone()
    try:
        busy = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC)
        anonymous = ready.post(f"{_BASE}/uploads", json=_DOC)
    finally:
        ready.app.state.manifest_principals = real
    assert busy.status_code == 503
    assert busy.headers["Retry-After"] == "1"
    assert busy.json() == {"detail": _BUSY_TEXT, "type": "AppendConcurrencyLimitError"}
    assert (
        busy.json()["type"]
        == AppendConcurrencyLimitError.__name__
        == sdk_artifacts._UPLOAD_BUSY_TYPE
    )
    assert anonymous.status_code == 401  # authentication ran before anything was taken
    assert ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).status_code == 201
    assert real == set()


def test_the_application_creates_an_empty_set_of_manifest_principals(client: TestClient) -> None:
    """The route's set of principals is built with the application, empty."""
    assert client.app.state.manifest_principals == set()


def test_a_manifest_that_takes_too_long_in_all_is_a_timeout(
    ready: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One byte at a time, each in time, still ends at the total deadline, not a multiple of it."""
    ready.app.state.settings.append.read_timeout_seconds = 0.3
    pieces: list[float] = []

    async def trickle(iterator: AsyncIterator[bytes], idle: float) -> bytes | None:
        pieces.append(idle)
        await asyncio.sleep(0.05)
        return b" "

    monkeypatch.setattr(routes, "next_chunk", trickle)
    slow = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC)
    assert slow.status_code == 408
    assert "did not finish arriving within 0.3s" in slow.json()["detail"]
    assert "Send it again" in slow.json()["detail"]
    # Each piece takes at least 0.05 s, so 0.3 s admits at most 7; twice the deadline would take 13.
    assert 1 <= len(pieces) <= 8


def test_a_deeply_nested_manifest_is_refused_not_a_500(ready: TestClient) -> None:
    for depth in (1000, 100_000):
        body = b'{"files":' + b"[" * depth + b"]" * depth + b"}"
        refused = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, content=body)
        assert refused.status_code == 422, depth
        assert refused.json()["detail"] != ""


def test_the_manifest_cap_is_exact(ready: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps(_DOC).encode()
    pieces = [body[:7], body[7:20], body[20:]]
    monkeypatch.setattr(routes, "MAX_MANIFEST_REQUEST_BYTES", len(body))
    assert (
        ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, content=iter(pieces)).status_code == 201
    )
    monkeypatch.setattr(routes, "MAX_MANIFEST_REQUEST_BYTES", len(body) - 1)
    refused = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, content=iter(pieces))
    assert refused.status_code == 413
    exact = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, content=body)
    assert exact.status_code == 413  # by its declared length


# --- a commit is idempotent per upload (I-7) --------------------------------------------------


def test_a_repeated_commit_over_http_is_the_same_version_and_adds_none(ready: TestClient) -> None:
    upload_id = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    ready.put(f"{_BASE}/uploads/{upload_id}/blobs/{_SHA}", headers=AUTH_HEADER, content=_MODEL)
    url = f"{_BASE}/uploads/{upload_id}/commit"
    first = ready.post(url, headers=AUTH_HEADER, json={})
    second = ready.post(url, headers=AUTH_HEADER, json={})
    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json() == first.json()
    third = ready.post(
        f"{_BASE}/uploads", headers=AUTH_HEADER, json={**_DOC, "files": _DOC["files"]}
    ).json()["upload_id"]
    assert (
        ready.post(f"{_BASE}/uploads/{third}/commit", headers=AUTH_HEADER, json={}).json()[
            "version"
        ]
        == 2
    )


# --- the same answer whether it exists or not (I-6) -------------------------------------------


def _normalised(response: object, names: tuple[str, ...]) -> tuple[int, str]:
    """Reduce an answer to its status and body with the names the caller sent removed.

    Parameters
    ----------
    response
        An `httpx.Response`.
    names
        The artifact names the caller put in the request.

    Returns
    -------
    tuple
        Status and normalised text.
    """
    text = response.text  # ty: ignore[unresolved-attribute]
    for name in names:
        text = text.replace(name, "NAME")
    return response.status_code, text  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize("role", [None, "ANALYST"], ids=["no grant", "read only"])
def test_a_caller_without_write_gets_the_same_answer_for_missing_and_existing(
    ready: TestClient, role: str | None
) -> None:
    """A foreign upload is refused over HTTP, for all three routes."""
    upload_artifact(ready, "dev", "models.churn", {"model.json": _MODEL}, is_directory=False)
    foreign = ready.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    caller = _user(ready, "caller", role)
    ghost = _BASE.replace("churn", "ghost")
    names = ("churn", "ghost")
    other_sha = hashlib.sha256(b"other").hexdigest()
    random_id = str(uuid.uuid4())

    begin = {
        _normalised(ready.post(f"{base}/uploads", headers=caller, json=_DOC), names)
        for base in (_BASE, ghost)
    }
    blobs = {
        _normalised(
            ready.put(f"{base}/uploads/{upload}/blobs/{digest}", headers=caller, content=_MODEL),
            names,
        )
        for base in (_BASE, ghost)
        for upload in (foreign, random_id)
        for digest in (_SHA, other_sha)
    }
    commits = {
        _normalised(ready.post(f"{base}/uploads/{upload}/commit", headers=caller, json=body), names)
        for base in (_BASE, ghost)
        for upload in (foreign, random_id)
        for body in ({}, {"sources": [{"name": "gold.rows"}]})
    }
    for answers in (begin, blobs, commits):
        assert len(answers) == 1, answers
        assert next(iter(answers))[0] == 403
