"""Unit tests for `client.artifacts` against a mock transport."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from scripts.pytest_memory_limit import open_descriptors

from lakana._artifact_manifest import (
    ManifestError,
    ScanLimits,
    StoredManifestDamaged,
    StoredManifestInvalid,
    manifest_digest,
)
from lakana._config import ResolvedConfig
from lakana._http import HttpTransport
from lakana._models import (
    Artifact,
    ArtifactDownload,
    ArtifactFile,
    ArtifactKept,
    ArtifactUpload,
    ArtifactUploadPage,
    ArtifactVersion,
    DeletedArtifactVersion,
)
from lakana.errors import (
    ArtifactCorrupt,
    Conflict,
    InvalidResponse,
    LakanaConnectionError,
    LakanaError,
    NotFound,
    PayloadTooLarge,
    PermissionDenied,
    ServerError,
    ValidationError,
)
from lakana.resources import artifacts as module
from lakana.resources.artifacts import Artifacts

_CONFIG = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
_BASE = "/api/v1/environments/dev/namespaces/models/artifacts/churn"
_UPLOAD_ID = "3f2b9c1e-5a4d-4c8e-9b1a-0d6e7f8a9b0c"
_GONE = "This upload has expired or does not exist; start the upload again."
_NOT_STORED = "1 of this upload's files are not stored yet; upload them and commit again."
_VERSION = {
    "version": 3,
    "manifest_sha256": "a" * 64,
    "size_bytes": 10,
    "file_count": 1,
    "is_directory": True,
    "task_run_id": None,
    "run_id": None,
    "project_version_id": None,
    "commit_sha": None,
    "created_by": "admin",
    "created_at": "2026-10-04T12:00:00Z",
    "deleted_at": None,
    "deleted_by": None,
    "files": [{"path": "big.bin", "size": 10, "sha256": "b" * 64}],
    "deleted_versions": [1],
}


class _Streaming(httpx.BaseTransport):
    """Hand the request to a handler without reading its body first.

    `httpx.MockTransport` reads the whole body before it calls the handler, which would hide
    how a file is cut into pieces and what happens when the body's iterator fails.

    Parameters
    ----------
    handler
        Answers a request; it may iterate `request.stream`.
    """

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Pass the request on.

        Parameters
        ----------
        request
            The request.

        Returns
        -------
        httpx.Response
            The handler's answer.
        """
        return self._handler(request)


class _Api:
    """Record requests and answer the three upload routes.

    Parameters
    ----------
    missing
        Whether the server lacks every file.
    conflicts
        How many commits answer 409 before one succeeds.
    conflict_detail
        The text of those 409s.
    busy
        How many file uploads answer 503 before one succeeds.
    busy_begin
        How many requests to begin the upload answer 503 before one succeeds.
    gone_on_put
        How many file uploads answer 409 "upload gone" before one succeeds.
    then_conflict
        After the other conflicts, one 409 with this text instead of the version.
    commit_faults
        What the first commits meet, one entry each: ``"lost"`` (a read timeout, the answer
        never arrives), ``"refused"`` (the connection does not open, so nothing is sent) or a
        status code answered instead of the version.
    after_begin
        Runs once the upload was begun, for a test that changes a file at that moment.
    version
        The number the commit gives the version.
    info
        The answer of the info route: what an older server sends by default, no limits.
    abort
        What a request to withdraw the upload meets: ``204`` (the default), a status code
        answered instead, or ``"lost"`` (the connection fails).
    on_put
        Runs when a file arrives, for a test that interrupts the upload there.
    """

    def __init__(
        self,
        *,
        missing: bool = True,
        conflicts: int = 0,
        conflict_detail: str = _NOT_STORED,
        busy: int = 0,
        busy_begin: int = 0,
        gone_on_put: int = 0,
        commit_faults: tuple[int | str, ...] = (),
        then_conflict: str | None = None,
        after_begin: Callable[[], None] | None = None,
        version: int = 3,
        info: Mapping[str, object] | None = None,
        abort: int | str = 204,
        on_put: Callable[[], None] | None = None,
    ) -> None:
        self.abort = abort
        self.on_put = on_put
        self.aborts: list[str] = []
        self.info = {"version": "1"} if info is None else info
        self.info_calls = 0
        self.missing = missing
        self.conflicts = conflicts
        self.conflict_detail = conflict_detail
        self.busy = busy
        self.busy_begin = busy_begin
        self.gone_on_put = gone_on_put
        self.then_conflict = then_conflict
        self.commit_faults = list(commit_faults)
        self.commit_ids: list[str] = []
        self.after_begin = after_begin
        self.version = version
        self.timeouts: list[tuple[str, float | None]] = []
        self.calls: list[tuple[str, str]] = []
        self.pieces: list[list[int]] = []
        self.bodies: list[dict[str, object]] = []
        self.params: list[dict[str, str]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one request.

        Parameters
        ----------
        request
            The request.

        Returns
        -------
        httpx.Response
            The canned answer.

        Raises
        ------
        httpx.ReadTimeout
            When the commit's answer is to be lost.
        httpx.ConnectError
            When the commit's connection is to be refused.
        httpx.ConnectError
            When the request to withdraw the upload is to fail.
        """
        if request.url.path == "/api/v1/info":
            self.info_calls += 1
            return httpx.Response(200, json=self.info)
        self.calls.append((request.method, request.url.path))
        self.params.append(dict(request.url.params))
        self.timeouts.append((request.method, request.extensions["timeout"]["read"]))
        if request.method == "DELETE":
            self.aborts.append(request.url.path)
            if self.abort == "lost":
                raise httpx.ConnectError("refused", request=request)
            assert isinstance(self.abort, int)
            return httpx.Response(self.abort, json={"detail": "no"} if self.abort >= 400 else None)
        if request.url.path.endswith("/uploads"):
            if self.busy_begin:
                self.busy_begin -= 1
                return httpx.Response(
                    503,
                    json={
                        "detail": "Too many concurrent uploads; retry shortly.",
                        "type": "AppendConcurrencyLimitError",
                    },
                )
            document = json.loads(request.content)
            self.bodies.append(document)
            hashes = [f["sha256"] for f in document["files"]] if self.missing else []
            hashes = list(dict.fromkeys(hashes))
            if self.after_begin is not None:
                self.after_begin()
            return httpx.Response(
                201,
                json={"upload_id": "u-1", "missing": hashes, "expires_at": "2026-10-04T13:00:00Z"},
            )
        if "/blobs/" in request.url.path:
            pieces = [len(chunk) for chunk in request.stream]  # ty: ignore[not-iterable]
            if self.on_put is not None:
                self.on_put()
            if self.busy:
                self.busy -= 1
                return httpx.Response(
                    503,
                    json={
                        "detail": "Too many concurrent uploads; retry shortly.",
                        "type": "AppendConcurrencyLimitError",
                    },
                )
            if self.gone_on_put:
                self.gone_on_put -= 1
                return httpx.Response(409, json={"detail": _GONE, "type": "ConflictError"})
            self.pieces.append(pieces)
            return httpx.Response(200, json={"sha256": "x", "stored": True})
        self.bodies.append(json.loads(request.content))
        self.commit_ids.append(request.url.path)
        if self.commit_faults:
            fault = self.commit_faults.pop(0)
            if fault == "lost":
                raise httpx.ReadTimeout("timed out", request=request)
            if fault == "refused":
                raise httpx.ConnectError("refused", request=request)
            assert isinstance(fault, int)
            return httpx.Response(fault, json={"detail": "bad gateway"})
        if self.conflicts:
            self.conflicts -= 1
            return httpx.Response(409, json={"detail": self.conflict_detail, "type": "x"})
        if self.then_conflict is not None:
            return httpx.Response(409, json={"detail": self.then_conflict, "type": "x"})
        return httpx.Response(201, json={**_VERSION, "version": self.version})

    def artifacts(self) -> Artifacts:
        """Build the resource over this fake.

        Returns
        -------
        Artifacts
            The resource.
        """
        http = httpx.Client(transport=_Streaming(self.handle), base_url=_CONFIG.url)
        return Artifacts(HttpTransport(_CONFIG, http_client=http))


def test_upload_streams_each_file_from_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file is read and sent in pieces, never as one `bytes`."""
    monkeypatch.setattr(module, "READ_BYTES", 4)
    (tmp_path / "big.bin").write_bytes(b"0123456789")
    api = _Api()
    version = api.artifacts().upload(
        "dev", "models.churn", tmp_path, sources={"gold.rows": None, "models.base": 2}
    )
    digest = hashlib.sha256(b"0123456789").hexdigest()
    assert api.calls == [
        ("POST", f"{_BASE}/uploads"),
        ("PUT", f"{_BASE}/uploads/u-1/blobs/{digest}"),
        ("POST", f"{_BASE}/uploads/u-1/commit"),
    ]
    assert api.pieces == [[4, 4, 2]]
    assert api.bodies[0]["files"] == [{"path": "big.bin", "size": 10, "sha256": digest}]
    assert api.bodies[1] == {
        "sources": [{"name": "gold.rows", "version": None}, {"name": "models.base", "version": 2}]
    }
    assert version == ArtifactVersion(
        version=3,
        manifest_sha256="a" * 64,
        size_bytes=10,
        file_count=1,
        is_directory=True,
        task_run_id=None,
        run_id=None,
        project_version_id=None,
        commit_sha=None,
        created_by="admin",
        created_at="2026-10-04T12:00:00Z",
        deleted_at=None,
        deleted_by=None,
        files=[ArtifactFile("big.bin", 10, "b" * 64)],
        deleted_versions=[1],
    )


def test_upload_sends_only_what_is_missing_and_passes_break_glass(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(missing=False)
    api.artifacts().upload(
        "dev", "models.churn", tmp_path / "m.json", break_glass_reason="fix a model"
    )
    assert [method for method, _ in api.calls] == ["POST", "POST"]
    assert api.bodies[0]["is_directory"] is False
    assert all(params == {"break_glass_reason": "fix a model"} for params in api.params)
    assert api.bodies[1] == {"sources": []}


def test_upload_starts_again_when_files_went_missing_and_gives_up_after_three(
    tmp_path: Path,
) -> None:
    """A retried upload after conflicts gets the next version."""
    (tmp_path / "m.json").write_bytes(b"m")
    retried = _Api(conflicts=2)
    assert retried.artifacts().upload("dev", "models.churn", tmp_path).version == 3
    assert [method for method, _ in retried.calls].count("PUT") == 3
    stuck = _Api(conflicts=3)
    with pytest.raises(Conflict):
        stuck.artifacts().upload("dev", "models.churn", tmp_path)
    assert len(stuck.calls) == 12  # three rounds of begin, put, commit, and each one withdrawn


def test_upload_refuses_a_bad_tree_before_any_request(tmp_path: Path) -> None:
    (tmp_path / "leak").symlink_to(tmp_path)
    api = _Api()
    with pytest.raises(ValidationError, match="leak is a symbolic link"):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    with pytest.raises(ValidationError, match="Expected a dotted"):
        api.artifacts().upload("dev", "churn", tmp_path)
    assert api.calls == []


@pytest.fixture(autouse=True)
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the wait between attempts with a recorder, so no test sleeps.

    Parameters
    ----------
    monkeypatch
        The fixture.

    Returns
    -------
    list of float
        The waits asked for, in order.
    """
    asked: list[float] = []
    monkeypatch.setattr(module, "_wait", asked.append)
    return asked


def test_only_the_typed_busy_answer_widens_nothing_a_proxys_plain_503_on_begin_is_raised_at_once(
    tmp_path: Path, waits: list[float]
) -> None:
    """Beginning an upload is asked again for the busy `type` only, not for any 503 text.

    A proxy's 503 with a plain-text body, one with no body, one with `Retry-After`, and one whose
    JSON says the busy words without the busy `type` all stand for a request the server may have
    processed: each is raised after one request and no wait.
    """
    (tmp_path / "m.json").write_bytes(b"m")
    answers = (
        httpx.Response(503, text="upstream connect error or disconnect/reset before headers"),
        httpx.Response(503),
        httpx.Response(503, text="busy", headers={"Retry-After": "1"}),
        httpx.Response(503, json={"detail": "Too many concurrent uploads; retry shortly."}),
    )
    for answer in answers:
        posts: list[str] = []

        def proxy(
            request: httpx.Request,
            answer: httpx.Response = answer,
            posts: list[str] = posts,
        ) -> httpx.Response:
            if request.url.path.endswith("/uploads"):
                posts.append(request.url.path)
            return answer

        http = httpx.Client(transport=_Streaming(proxy), base_url=_CONFIG.url)
        with pytest.raises(ServerError):
            Artifacts(HttpTransport(_CONFIG, http_client=http)).upload(
                "dev", "models.churn", tmp_path
            )
        assert len(posts) == 1 and waits == []


def _flaky_put_api(tmp_path: Path, headers: dict[str, str], failures: int) -> Artifacts:
    """Build an `Artifacts` whose first `failures` file uploads are answered 503 with `headers`.

    Parameters
    ----------
    tmp_path
        A directory; one file, `m.json`, is written into it.
    headers
        The headers of each 503.
    failures
        How many file uploads are answered 503 before one is accepted.

    Returns
    -------
    Artifacts
        The resource over a fake server.
    """
    (tmp_path / "m.json").write_bytes(b"m")
    puts: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            return httpx.Response(
                201,
                json={
                    "upload_id": "u-1",
                    "missing": [hashlib.sha256(b"m").hexdigest()],
                    "expires_at": "2026-10-04T13:00:00Z",
                },
            )
        if request.method == "PUT":
            list(request.stream)  # ty: ignore[invalid-argument-type]
            puts.append(request.url.path)
            if len(puts) <= failures:
                return httpx.Response(
                    503,
                    json={"detail": "storage is down", "type": "StorageUnavailableError"},
                    headers=headers,
                )
            return httpx.Response(204)
        return httpx.Response(201, json=_VERSION)

    http = httpx.Client(transport=_Streaming(server), base_url=_CONFIG.url)
    return Artifacts(HttpTransport(_CONFIG, http_client=http))


@pytest.mark.parametrize(
    ("header", "waited"),
    [("5", 5.0), ("3", 3.0), ("10", 10.0), ("11", 10.0), ("3600", 10.0), (" 7 ", 7.0), ("0", 0.0)],
)
def test_a_file_upload_waits_the_whole_seconds_the_server_asked_up_to_ten(
    tmp_path: Path, waits: list[float], header: str, waited: float
) -> None:
    """The storage-unavailable 503 says `Retry-After: 5`; the SDK waits that long."""
    api = _flaky_put_api(tmp_path, {"Retry-After": header}, failures=2)
    assert api.upload("dev", "models.churn", tmp_path).version == 3
    assert waits == [waited, waited]


def test_a_retry_after_of_five_thousand_digits_is_the_cap_and_does_not_raise() -> None:
    """Python refuses `int()` of a string past 4,300 digits; the wait never gets that far."""
    exc = LakanaError("busy", status=503, retry_after="9" * 5000)
    assert module._retry_wait(exc) == pytest.approx(10.0)


@pytest.mark.parametrize(
    "header",
    [None, "soon", "-2", "-0", "1.5", "", "Wed, 21 Oct 2026 07:28:00 GMT", "5s", "+3"],
)
def test_a_retry_after_that_is_not_whole_seconds_is_waited_one_second(
    tmp_path: Path, waits: list[float], header: str | None
) -> None:
    """Absent, not a number, negative, fractional or an HTTP date: the one-second fall-back."""
    headers = {} if header is None else {"Retry-After": header}
    api = _flaky_put_api(tmp_path, headers, failures=2)
    assert api.upload("dev", "models.churn", tmp_path).version == 3
    assert waits == [1.0, 1.0]


def test_the_waits_of_a_file_follow_each_answers_own_retry_after(
    tmp_path: Path, waits: list[float]
) -> None:
    """Five tries make four waits, each as long as that answer's own `Retry-After`."""
    api = _flaky_put_api(tmp_path, {"Retry-After": "4"}, failures=99)
    with pytest.raises(ServerError, match="storage is down"):
        api.upload("dev", "models.churn", tmp_path)
    assert waits == [4.0] * 4


def test_a_commit_answered_with_a_gateway_error_is_sent_again_and_stores_one_version(
    tmp_path: Path, waits: list[float]
) -> None:
    """The same commit, the same upload; the server adds no second version."""
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(commit_faults=(502, "lost", 504))
    version = api.artifacts().upload("dev", "models.churn", tmp_path)
    assert version.version == 3
    assert [method for method, _ in api.calls] == ["POST", "PUT", "POST", "POST", "POST", "POST"]
    assert set(api.commit_ids) == {f"{_BASE}/uploads/u-1/commit"}  # one upload id throughout
    assert len(api.commit_ids) == 4
    assert waits == [2.0, 4.0, 8.0]


def test_a_commit_that_never_gets_an_answer_ends_with_the_upload_named(
    tmp_path: Path, waits: list[float]
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(commit_faults=("lost",) * 4)
    with pytest.raises(LakanaConnectionError) as caught:
        api.artifacts().upload("dev", "models.churn", tmp_path)
    text = str(caught.value)
    assert "commit of models.churn (upload u-1) after 4 tries" in text
    assert "may or may not have been stored" in text
    assert "lakana --output json artifact versions list models.churn --env dev" in text
    assert "leave the digest out on a narrow terminal" in text
    assert "manifest_sha256 starting " in text
    assert "Run the upload again only if it was not" in text
    assert len(api.commit_ids) == 4
    assert waits == [2.0, 4.0, 8.0]
    gateway = _Api(commit_faults=(502,) * 4)
    with pytest.raises(ServerError, match="after 4 tries") as server:
        gateway.artifacts().upload("dev", "models.churn", tmp_path)
    assert server.value.status == 502


def test_a_commit_that_was_never_sent_says_the_version_was_not_stored(
    tmp_path: Path, waits: list[float]
) -> None:
    """The connection never opened: no "may or may not", and the next step is the upload."""
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(commit_faults=("refused",) * 4)
    with pytest.raises(LakanaConnectionError) as caught:
        api.artifacts().upload("dev", "models.churn", tmp_path)
    text = " ".join(str(caught.value).split())
    assert "may or may not" not in text
    assert "failed before the commit of models.churn (upload u-1) was sent" in text
    assert "so the version was not stored" in text
    assert "profile's URL" in text
    assert "run the upload again" in text
    assert waits == [2.0, 4.0, 8.0]


def test_a_commit_that_may_have_been_sent_once_keeps_the_uncertain_text(
    tmp_path: Path, waits: list[float]
) -> None:
    """One attempt that went out and lost its answer makes the whole outcome uncertain."""
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(commit_faults=("refused", "lost", "refused", "refused"))
    with pytest.raises(LakanaConnectionError, match="may or may not have been stored"):
        api.artifacts().upload("dev", "models.churn", tmp_path)


def test_gone_after_a_lost_commit_is_not_uploaded_again(tmp_path: Path, waits: list[float]) -> None:
    """N-1: once a commit was sent twice, "gone" means unconfirmed, never "start again"."""
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(commit_faults=(504,), conflicts=1, conflict_detail=_GONE)
    with pytest.raises(ServerError) as caught:
        api.artifacts().upload("dev", "models.churn", tmp_path)
    text = str(caught.value)
    assert "commit of models.churn (upload u-1)" in text
    assert "may or may not have been stored" in text
    assert "If the server is reachable, run `lakana --output json artifact versions list" in text
    assert "Once the server answers again" not in text
    assert [method for method, _ in api.calls] == ["POST", "PUT", "POST", "POST"]
    assert len(api.bodies) == 3  # one begin, two commits
    assert waits == [2.0]


def test_gone_after_a_lost_connection_on_the_commit_is_not_uploaded_again(
    tmp_path: Path, waits: list[float]
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(commit_faults=("lost",), conflicts=1, conflict_detail=_GONE)
    with pytest.raises(LakanaConnectionError, match="may or may not have been stored"):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert [method for method, _ in api.calls].count("POST") == 3
    assert [method for method, _ in api.calls].count("PUT") == 1


def test_a_version_deleted_since_the_first_commit_is_not_uploaded_again(
    tmp_path: Path, waits: list[float]
) -> None:
    """N-4: the server's own text for it is shown, and nothing is stored a second time."""
    (tmp_path / "m.json").write_bytes(b"m")
    text = (
        "This upload was committed as version 3 of models.churn, and that version has since "
        "been deleted."
    )
    api = _Api(commit_faults=(504,), conflicts=1, conflict_detail=text)
    with pytest.raises(Conflict) as caught:
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert str(caught.value) == text
    assert [method for method, _ in api.calls] == ["POST", "PUT", "POST", "POST"]


def test_gone_in_answer_to_the_first_commit_still_starts_again(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(conflicts=1, conflict_detail=_GONE)
    assert api.artifacts().upload("dev", "models.churn", tmp_path).version == 3
    assert [method for method, _ in api.calls].count("POST") == 4  # begin, commit, begin, commit


def test_the_lost_commit_message_says_a_second_version_may_prune_an_old_one(
    tmp_path: Path, waits: list[float]
) -> None:
    """N-6: with `keep_versions`, one more version retires one more old one."""
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(commit_faults=("lost",) * 4)
    with pytest.raises(LakanaConnectionError) as caught:
        api.artifacts().upload("dev", "models.churn", tmp_path)
    text = str(caught.value)
    assert "does no harm" not in text
    assert "stores the same content as one more version" in text
    assert "limits its versions" in text
    assert "may remove one more old version" in text


def test_a_commit_the_server_refuses_is_not_sent_again(tmp_path: Path, waits: list[float]) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(commit_faults=(403,))
    with pytest.raises(LakanaError):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert len(api.commit_ids) == 1
    assert waits == []


def test_the_commit_and_the_files_get_their_own_timeouts(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api()
    api.artifacts().upload("dev", "models.churn", tmp_path)
    by_method = dict(api.timeouts)
    assert by_method["PUT"] == module._BLOB_TIMEOUT_SECONDS
    assert api.timeouts[-1] == ("POST", module._COMMIT_TIMEOUT_SECONDS)
    assert module._COMMIT_TIMEOUT_SECONDS > 30.0  # the client's default


def test_an_expired_upload_starts_again_from_the_manifest(tmp_path: Path) -> None:
    """Point G: only a new `missing` list names the files to send."""
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(conflicts=1, conflict_detail=_GONE)
    assert api.artifacts().upload("dev", "models.churn", tmp_path).version == 3
    assert [method for method, _ in api.calls] == ["POST", "PUT", "POST", "POST", "PUT", "POST"]


def test_a_file_upload_that_finds_the_upload_gone_starts_again(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(gone_on_put=1)
    assert api.artifacts().upload("dev", "models.churn", tmp_path).version == 3
    assert [method for method, _ in api.calls].count("POST") == 3  # begin, begin, commit


def test_a_conflict_a_new_upload_would_meet_again_is_not_retried(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(conflicts=1, conflict_detail="models.churn has 16 uploads in progress.")
    with pytest.raises(Conflict, match="16 uploads in progress"):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert [method for method, _ in api.calls] == ["POST", "PUT", "POST", "DELETE"]


def test_a_busy_server_is_asked_again_for_the_same_file(tmp_path: Path, waits: list[float]) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(busy=2)
    assert api.artifacts().upload("dev", "models.churn", tmp_path).version == 3
    assert [method for method, _ in api.calls].count("PUT") == 3
    assert waits == [1.0, 1.0]
    waits.clear()
    always = _Api(busy=99)
    with pytest.raises(ServerError, match="Too many concurrent uploads"):
        always.artifacts().upload("dev", "models.churn", tmp_path)
    assert [method for method, _ in always.calls].count("PUT") == 5  # a literal, not the constant
    assert waits == [1.0] * 4


def test_every_503_on_a_file_is_sent_again_because_a_resend_is_harmless(
    tmp_path: Path, waits: list[float]
) -> None:
    """The file route is addressed by hash and answers "already had it" to a second copy."""
    (tmp_path / "m.json").write_bytes(b"m")
    for body in (
        {"detail": "upstream connect error"},
        {"detail": "storage is down", "type": "StorageUnavailableError"},
        {"detail": "Too many concurrent uploads", "type": "AppendConcurrencyLimitError"},
    ):
        waits.clear()
        puts: list[str] = []

        def proxy(
            request: httpx.Request, body: dict[str, str] = body, puts: list[str] = puts
        ) -> httpx.Response:
            if request.url.path.endswith("/uploads"):
                return httpx.Response(
                    201,
                    json={
                        "upload_id": "u-1",
                        "missing": [hashlib.sha256(b"m").hexdigest()],
                        "expires_at": "2026-10-04T13:00:00Z",
                    },
                )
            if request.method == "PUT":
                list(request.stream)  # ty: ignore[invalid-argument-type]
                puts.append(request.url.path)
                if len(puts) < 3:
                    return httpx.Response(503, json=body)
                return httpx.Response(204)
            return (
                httpx.Response(201, json=_VERSION)
                if request.method == "POST"
                else httpx.Response(204)
            )

        http = httpx.Client(transport=_Streaming(proxy), base_url=_CONFIG.url)
        version = Artifacts(HttpTransport(_CONFIG, http_client=http)).upload(
            "dev", "models.churn", tmp_path
        )
        assert version.version == 3
        assert len(puts) == 3 and waits[:2] == [1.0, 1.0]


def test_a_file_that_always_answers_503_is_sent_five_times_then_raised(
    tmp_path: Path, waits: list[float]
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    puts: list[str] = []

    def proxy(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            return httpx.Response(
                201,
                json={
                    "upload_id": "u-1",
                    "missing": [hashlib.sha256(b"m").hexdigest()],
                    "expires_at": "2026-10-04T13:00:00Z",
                },
            )
        if request.method == "PUT":
            list(request.stream)  # ty: ignore[invalid-argument-type]
            puts.append(request.url.path)
            return httpx.Response(503, json={"detail": "upstream connect error"})
        return httpx.Response(204)

    http = httpx.Client(transport=_Streaming(proxy), base_url=_CONFIG.url)
    with pytest.raises(ServerError, match="upstream connect error"):
        Artifacts(HttpTransport(_CONFIG, http_client=http)).upload("dev", "models.churn", tmp_path)
    assert len(puts) == 5 and waits == [1.0] * 4


def test_a_413_on_an_upload_is_raised_at_once_and_never_sent_again(
    tmp_path: Path, waits: list[float]
) -> None:
    """The operator's limit is reached: asking again cannot help (the harness fails it for good)."""
    (tmp_path / "m.json").write_bytes(b"m")
    text = "The manifest request is larger than 1048576 bytes."
    seen: list[str] = []

    def too_large(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":  # the limits lookup before the upload
            seen.append(request.method)
        return httpx.Response(413, json={"detail": text, "type": "PayloadTooLargeError"})

    http = httpx.Client(transport=_Streaming(too_large), base_url=_CONFIG.url)
    with pytest.raises(PayloadTooLarge, match=text):
        Artifacts(HttpTransport(_CONFIG, http_client=http)).upload("dev", "models.churn", tmp_path)
    assert seen == ["POST"] and waits == []  # one begin, no retry, no upload to withdraw


def test_a_413_on_a_file_is_raised_at_once_and_the_upload_is_withdrawn(
    tmp_path: Path, waits: list[float]
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            return httpx.Response(
                201,
                json={
                    "upload_id": "u-1",
                    "missing": [hashlib.sha256(b"m").hexdigest()],
                    "expires_at": "2026-10-04T13:00:00Z",
                },
            )
        if request.method != "GET":
            seen.append(request.method)
        if request.method == "PUT":
            list(request.stream)  # ty: ignore[invalid-argument-type]
            return httpx.Response(413, json={"detail": "too big"})
        return httpx.Response(204)

    http = httpx.Client(transport=_Streaming(handler), base_url=_CONFIG.url)
    with pytest.raises(PayloadTooLarge, match="too big"):
        Artifacts(HttpTransport(_CONFIG, http_client=http)).upload("dev", "models.churn", tmp_path)
    assert seen == ["PUT", "DELETE"] and waits == []


def test_a_busy_server_is_asked_again_to_begin_the_upload(
    tmp_path: Path, waits: list[float]
) -> None:
    """A-I4: the server refuses a manifest request when its decode slots are taken."""
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(busy_begin=2)
    assert api.artifacts().upload("dev", "models.churn", tmp_path).version == 3
    assert [method for method, _ in api.calls] == ["POST"] * 3 + ["PUT", "POST"]
    assert waits == [1.0, 1.0]
    waits.clear()
    always = _Api(busy_begin=99)
    with pytest.raises(ServerError, match="Too many concurrent uploads"):
        always.artifacts().upload("dev", "models.churn", tmp_path)
    assert [method for method, _ in always.calls] == ["POST"] * 5  # a literal, not the constant
    assert waits == [1.0] * 4
    assert always.aborts == []  # no upload exists to withdraw


def test_a_503_that_is_not_the_servers_busy_answer_does_not_begin_a_second_upload(
    tmp_path: Path, waits: list[float]
) -> None:
    """M-1: a proxy's 503 may follow a request the server processed; retrying could double it."""
    (tmp_path / "m.json").write_bytes(b"m")
    posts: list[str] = []

    def proxy(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            posts.append(request.url.path)
        return httpx.Response(503, json={"detail": "upstream connect error"})

    http = httpx.Client(transport=_Streaming(proxy), base_url=_CONFIG.url)
    with pytest.raises(ServerError, match="upstream connect error"):
        Artifacts(HttpTransport(_CONFIG, http_client=http)).upload("dev", "models.churn", tmp_path)
    assert len(posts) == 1 and waits == []
    posts.clear()

    def typed_elsewhere(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            posts.append(request.url.path)
        return httpx.Response(
            503, json={"detail": "storage is down", "type": "StorageUnavailableError"}
        )

    http = httpx.Client(transport=_Streaming(typed_elsewhere), base_url=_CONFIG.url)
    with pytest.raises(ServerError, match="storage is down"):
        Artifacts(HttpTransport(_CONFIG, http_client=http)).upload("dev", "models.churn", tmp_path)
    assert len(posts) == 1 and waits == []


def test_a_commit_answered_503_is_sent_again_and_stays_one_upload(
    tmp_path: Path, waits: list[float]
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(commit_faults=(503, 503))
    assert api.artifacts().upload("dev", "models.churn", tmp_path).version == 3
    assert len(set(api.commit_ids)) == 1 and len(api.commit_ids) == 3
    assert waits == [2.0, 4.0]


def test_a_file_that_changes_after_the_scan_is_cut_off_and_never_committed(
    tmp_path: Path,
) -> None:
    """The hash sent must be the hash scanned (point I)."""
    target = tmp_path / "w.bin"
    target.write_bytes(b"0123456789")
    for replacement in (b"0123456780", b"01234567890", b"0123"):
        target.write_bytes(b"0123456789")

        def rewrite(t: Path = target, r: bytes = replacement) -> None:
            t.write_bytes(r)

        api = _Api(after_begin=rewrite)
        with pytest.raises(ManifestError) as caught:
            api.artifacts().upload("dev", "models.churn", tmp_path)
        assert "w.bin changed after it was scanned" in str(caught.value)
        assert "run the upload again" in str(caught.value)
        # No commit; the upload is withdrawn.
        assert [method for method, _ in api.calls] == ["POST", "PUT", "DELETE"]


def test_a_file_that_vanishes_after_the_scan_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "w.bin"
    target.write_bytes(b"0123456789")
    api = _Api(after_begin=target.unlink)
    with pytest.raises(ManifestError, match=r"w\.bin could not be read"):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert [method for method, _ in api.calls] == ["POST", "PUT", "DELETE"]


def test_a_file_replaced_by_a_link_after_the_scan_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "w.bin"
    target.write_bytes(b"0123456789")
    secret = tmp_path.parent / f"{tmp_path.name}-secret"
    secret.write_bytes(b"0123456789")

    def swap() -> None:
        target.unlink()
        target.symlink_to(secret)

    api = _Api(after_begin=swap)
    with pytest.raises(ManifestError, match=r"w\.bin was replaced by a link"):
        api.artifacts().upload("dev", "models.churn", tmp_path)


def test_two_files_with_one_content_are_sent_once(tmp_path: Path) -> None:
    (tmp_path / "a.bin").write_bytes(b"same")
    (tmp_path / "b.bin").write_bytes(b"same")
    api = _Api()
    version = api.artifacts().upload("dev", "models.churn", tmp_path)
    assert [method for method, _ in api.calls] == ["POST", "PUT", "POST"]
    assert (version.files_sent, version.files_reused) == (1, 1)


def test_the_result_says_how_many_files_were_sent_and_how_many_were_there(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    sent = _Api().artifacts().upload("dev", "models.churn", tmp_path)
    assert (sent.files_sent, sent.files_reused) == (1, 0)
    kept = _Api(missing=False).artifacts().upload("dev", "models.churn", tmp_path)
    assert (kept.files_sent, kept.files_reused) == (0, 1)
    assert sent == kept  # the two counts are not part of what a version is


def test_a_version_number_with_gaps_is_taken_as_the_server_gives_it(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(version=9)
    version = api.artifacts().upload("dev", "models.churn", tmp_path)
    assert (version.version, version.deleted_versions) == (9, [1])


def test_a_hash_the_scan_does_not_know_is_an_invalid_response(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")

    def lying(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"upload_id": "u", "missing": ["f" * 64], "expires_at": "x"}
        )

    http = httpx.Client(transport=httpx.MockTransport(lying), base_url=_CONFIG.url)
    with pytest.raises(InvalidResponse, match="asked for a file this upload does not contain") as c:
        Artifacts(HttpTransport(_CONFIG, http_client=http)).upload("dev", "models.churn", tmp_path)
    text = str(c.value)
    assert "The upload was withdrawn." in text
    assert "same version, then run the upload again." in text


def test_limits_are_passed_to_the_scan(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"12345")
    api = _Api()
    with pytest.raises(ValidationError, match="more than the 3 bytes"):
        api.artifacts().upload("dev", "models.churn", tmp_path, limits=ScanLimits(max_file_bytes=3))
    assert api.calls == []


def test_a_read_error_while_a_file_is_sent_is_an_unreadable_file_and_cuts_the_request_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_artifact_scan import _FailingRead

    (tmp_path / "w.bin").write_bytes(b"x")
    api = _Api()
    real = os.fdopen
    calls = [0]

    def failing_after_the_scan(fd: int, mode: str) -> object:
        calls[0] += 1
        return real(fd, mode) if calls[0] == 1 else _FailingRead(real(fd, mode))

    monkeypatch.setattr(module.os, "fdopen", failing_after_the_scan)
    with pytest.raises(ManifestError) as caught:
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert (
        str(caught.value) == "w.bin could not be read: Input/output error. "
        "The upload was stopped and can be tried again once the file is readable."
    )
    assert "Wait until" not in str(caught.value) and "changed after" not in str(caught.value)
    assert [method for method, _ in api.calls] == ["POST", "PUT", "DELETE"]


def test_a_server_error_that_is_not_busy_is_not_retried(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")

    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "upload_id": "u",
                    "missing": [body["files"][0]["sha256"]],
                    "expires_at": "x",
                },
            )
        list(request.stream)  # ty: ignore[invalid-argument-type]
        return httpx.Response(500, json={"detail": "boom"})

    puts: list[str] = []

    def counting(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(request.url.path)
        return broken(request)

    http = httpx.Client(transport=_Streaming(counting), base_url=_CONFIG.url)
    with pytest.raises(ServerError, match="boom"):
        Artifacts(HttpTransport(_CONFIG, http_client=http)).upload("dev", "models.churn", tmp_path)
    assert len(puts) == 1


def test_a_begin_answered_500_is_not_retried(tmp_path: Path, waits: list[float]) -> None:
    """Only a 503 says nothing was created; any other server error is raised as it is."""
    (tmp_path / "m.json").write_bytes(b"m")
    posts: list[str] = []

    def broken(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
        return httpx.Response(500, json={"detail": "boom"})

    http = httpx.Client(transport=_Streaming(broken), base_url=_CONFIG.url)
    with pytest.raises(ServerError, match="boom"):
        Artifacts(HttpTransport(_CONFIG, http_client=http)).upload("dev", "models.churn", tmp_path)
    assert len(posts) == 1
    assert waits == []


def _answering(begin: httpx.Response, commit: httpx.Response) -> Artifacts:
    """Build the resource over a server that gives these two answers and accepts files.

    Parameters
    ----------
    begin
        The answer to the start of the upload.
    commit
        The answer to the commit.

    Returns
    -------
    Artifacts
        The resource.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/uploads"):
            return begin
        if "/blobs/" in request.url.path:
            list(request.stream)  # ty: ignore[invalid-argument-type]
            return httpx.Response(200, json={"sha256": "x", "stored": True})
        return commit

    http = httpx.Client(transport=_Streaming(handler), base_url=_CONFIG.url)
    return Artifacts(HttpTransport(_CONFIG, http_client=http))


@pytest.mark.parametrize(
    "begin",
    [
        httpx.Response(201, content=b"<html>proxy error</html>"),
        httpx.Response(201, json=["not", "an", "object"]),
        httpx.Response(201, json={"missing": []}),
        httpx.Response(201, json={"upload_id": "u", "missing": "abc"}),
        httpx.Response(201, json={"upload_id": "u", "missing": [1]}),
        httpx.Response(201, json={"upload_id": 7, "missing": []}),
    ],
)
def test_an_answer_that_is_not_a_lakana_answer_is_an_invalid_response_at_the_start(
    tmp_path: Path, begin: httpx.Response
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    with pytest.raises(InvalidResponse, match=r"not what a Lakana server sends\. Check the URL"):
        _answering(begin, httpx.Response(201, json=_VERSION)).upload(
            "dev", "models.churn", tmp_path
        )


@pytest.mark.parametrize(
    "commit",
    [
        httpx.Response(201, content=b"<html>proxy</html>"),
        httpx.Response(201, json={"files": []}),
        httpx.Response(201, json={**_VERSION, "files": [{"path": "p"}]}),
        httpx.Response(201, json={**_VERSION, "files": 5}),
    ],
)
def test_a_201_that_is_not_a_version_is_an_invalid_response(
    tmp_path: Path, commit: httpx.Response
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    start = httpx.Response(201, json={"upload_id": "u", "missing": [], "expires_at": "x"})
    with pytest.raises(InvalidResponse, match="not what a Lakana server sends"):
        _answering(start, commit).upload("dev", "models.churn", tmp_path)


def test_giving_up_after_three_lost_uploads_says_what_to_do(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(conflicts=3, conflict_detail=_GONE)
    with pytest.raises(Conflict) as caught:
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert "was lost 3 times in a row" in str(caught.value)
    assert "Run the upload again in a minute" in str(caught.value)
    assert caught.value.status == 409


def test_the_last_round_does_not_hide_a_conflict_a_new_upload_would_meet_again(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(conflicts=2, conflict_detail=_GONE, then_conflict="models.churn is full.")
    with pytest.raises(Conflict, match="is full"):
        api.artifacts().upload("dev", "models.churn", tmp_path)


_REAL_WAIT = module._wait


def test_the_wait_between_attempts_really_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(module.time, "sleep", slept.append)
    _REAL_WAIT(2.5)
    assert slept == [2.5]


# --- listing, versions and download -------------------------------------------------------------

_FILE = b"abc"
_FILE_SHA = hashlib.sha256(_FILE).hexdigest()
_ARTIFACT = {
    "name": "churn",
    "namespace": "models",
    "uuid": "u",
    "kind": "ARTIFACT",
    "current_version": 2,
    "versions": 3,
    "size_bytes": 9,
    "created_at": "2026-10-04T12:00:00Z",
}


def _served(
    files: Sequence[Mapping[str, Any]], *, is_directory: bool, deleted: bool = False
) -> dict[str, object]:
    """Build a version JSON whose digest matches its files.

    Parameters
    ----------
    files
        The file entries.
    is_directory
        Whether it is a directory artifact.
    deleted
        Whether the version is deleted.

    Returns
    -------
    dict
        The JSON.
    """
    digest = manifest_digest(is_directory, [(f["path"], f["size"], f["sha256"]) for f in files])
    return {
        **_VERSION,
        "version": 4,
        "manifest_sha256": digest,
        "is_directory": is_directory,
        "files": files,
        "size_bytes": sum(f["size"] for f in files),
        "file_count": len(files),
        "deleted_at": "2026-10-04T12:00:00Z" if deleted else None,
        "deleted_versions": [],
    }


def _download_api(
    version: dict[str, object],
    body: bytes | Callable[[httpx.Request], httpx.Response] = _FILE,
) -> tuple[Artifacts, list[str]]:
    """Build a resource whose server serves one version and one file body.

    Parameters
    ----------
    version
        The version JSON.
    body
        What every blob request answers, or a function that answers it.

    Returns
    -------
    tuple
        The resource and the list of requested paths.
    """
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if "/blobs/" in request.url.path:
            return httpx.Response(200, content=body) if isinstance(body, bytes) else body(request)
        return httpx.Response(200, json=version)

    http = httpx.Client(transport=httpx.MockTransport(handle), base_url=_CONFIG.url)
    return Artifacts(HttpTransport(_CONFIG, http_client=http)), paths


def test_download_writes_the_current_version_and_returns_where(tmp_path: Path) -> None:
    files = [{"path": "sub/w.bin", "size": 3, "sha256": _FILE_SHA}]
    artifacts, paths = _download_api(_served(files, is_directory=True))
    got = artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert got == ArtifactDownload(
        path=str((tmp_path / "copy").resolve()),
        version=4,
        manifest_sha256=got.manifest_sha256,
        size_bytes=3,
        file_count=1,
    )
    assert (tmp_path / "copy" / "sub" / "w.bin").read_bytes() == _FILE
    assert paths == [f"{_BASE}/versions/current", f"{_BASE}/versions/4/blobs/{_FILE_SHA}"]
    single, single_paths = _download_api(
        _served([{"path": "model.json", "size": 3, "sha256": _FILE_SHA}], is_directory=False)
    )
    one = single.download("dev", "models.churn", tmp_path / "one", version=4)
    assert one.path == str((tmp_path / "one").resolve() / "model.json")
    assert single_paths[0] == f"{_BASE}/versions/4"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["copy", "one"]


def test_a_failed_download_leaves_nothing_at_the_target(tmp_path: Path) -> None:
    files = [
        {"path": "a", "size": 3, "sha256": _FILE_SHA},
        {"path": "b", "size": 3, "sha256": "0" * 64},
    ]
    artifacts, _ = _download_api(_served(files, is_directory=True))
    with pytest.raises(ArtifactCorrupt, match="The downloaded file b does not match"):
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert list(tmp_path.iterdir()) == []
    (tmp_path / "empty").mkdir()
    with pytest.raises(ArtifactCorrupt):
        artifacts.download("dev", "models.churn", tmp_path / "empty")
    assert list((tmp_path / "empty").iterdir()) == []


def test_download_refuses_a_manifest_that_is_not_what_was_signed_or_is_deleted(
    tmp_path: Path,
) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    forged = {**_served(files, is_directory=True), "manifest_sha256": "f" * 64}
    artifacts, paths = _download_api(forged)
    with pytest.raises(
        ArtifactCorrupt, match="The stored manifest of version 4 does not match"
    ) as c:
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert type(c.value) is StoredManifestDamaged
    assert str(c.value) == (
        "The stored manifest of version 4 does not match its digest, so this version is damaged "
        "on the server and downloading it again will not help. Tell the operator, and store the "
        "artifact again (run the asset that produces it, or use `lakana artifact upload`)."
    )
    escaping = _served([{"path": "../evil", "size": 3, "sha256": _FILE_SHA}], is_directory=True)
    bad, bad_paths = _download_api(escaping)
    with pytest.raises(ValidationError, match="a path this client refuses") as refused:
        bad.download("dev", "models.churn", tmp_path / "copy")
    assert type(refused.value) is StoredManifestInvalid
    assert isinstance(refused.value, ManifestError)
    gone, gone_paths = _download_api(_served(files, is_directory=True, deleted=True))
    with pytest.raises(
        NotFound,
        match=(
            r"Version 4 of models\.churn was deleted; its bytes are gone\. "
            r"Read a version that exists: `lakana artifact versions list models\.churn "
            r"--env dev` shows which\."
        ),
    ):
        gone.download("dev", "models.churn", tmp_path / "copy")
    assert not (tmp_path / "evil").exists()
    assert list(tmp_path.iterdir()) == []
    assert all("/blobs/" not in path for path in (*paths, *bad_paths, *gone_paths))


def test_download_refuses_a_directory_that_is_in_use_before_it_asks_for_any_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "taken").mkdir()
    (tmp_path / "taken" / "x").write_bytes(b"x")
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    artifacts, paths = _download_api(_served(files, is_directory=True))
    with pytest.raises(ManifestError, match="already exists and is not an empty directory"):
        artifacts.download("dev", "models.churn", tmp_path / "taken")
    assert all("/blobs/" not in path for path in paths)
    assert (tmp_path / "taken" / "x").read_bytes() == b"x"


def test_a_connection_cut_in_the_middle_of_a_file_keeps_nothing_and_says_so(
    tmp_path: Path,
) -> None:
    class Cut(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"ab"
            raise httpx.RemoteProtocolError("peer closed the connection before the body ended")

    files = [
        {"path": "a", "size": 3, "sha256": _FILE_SHA},
        {"path": "b", "size": 3, "sha256": _FILE_SHA},
    ]
    artifacts, _ = _download_api(
        _served(files, is_directory=True), lambda request: httpx.Response(200, stream=Cut())
    )
    with pytest.raises(LakanaConnectionError) as caught:
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert "The connection to the server ended before a was complete" in str(caught.value)
    assert "nothing was kept" in str(caught.value)
    assert "download it again" in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_a_server_that_sends_more_than_the_manifest_says_is_corrupt(tmp_path: Path) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    artifacts, _ = _download_api(_served(files, is_directory=True), b"abcdefgh")
    with pytest.raises(ArtifactCorrupt, match="The downloaded file a does not match"):
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert list(tmp_path.iterdir()) == []


def test_an_error_answer_for_a_file_is_raised_and_keeps_nothing(tmp_path: Path) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    artifacts, _ = _download_api(
        _served(files, is_directory=True),
        lambda request: httpx.Response(503, json={"detail": "storage is down"}),
    )
    with pytest.raises(ServerError, match="storage is down"):
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert list(tmp_path.iterdir()) == []


def test_a_server_that_does_not_send_a_version_is_an_invalid_response(tmp_path: Path) -> None:
    artifacts, _ = _download_api({"unexpected": True})
    with pytest.raises(InvalidResponse):
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert list(tmp_path.iterdir()) == []


def test_a_version_must_be_a_positive_number_before_a_request_is_sent(tmp_path: Path) -> None:
    artifacts, paths = _download_api(_served([], is_directory=True))
    for bad in (0, -1, True, 1.5, "1"):
        with pytest.raises(ValidationError, match="A version is a positive number"):
            artifacts.version("dev", "models.churn", bad)  # ty: ignore[invalid-argument-type]
    assert paths == []


def test_list_get_and_versions_parse() -> None:
    seen: list[tuple[str, dict[str, str]]] = []
    gaps = [{**_VERSION, "version": 5}, {**_VERSION, "version": 2}]

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(200, json=[_ARTIFACT])
        if request.url.path.endswith("/versions"):
            return httpx.Response(200, json=gaps)
        return httpx.Response(200, json=_ARTIFACT)

    http = httpx.Client(transport=httpx.MockTransport(handle), base_url=_CONFIG.url)
    artifacts = Artifacts(HttpTransport(_CONFIG, http_client=http))
    assert artifacts.list("dev") == [
        Artifact("churn", "models", "u", "ARTIFACT", 2, 3, 9, "2026-10-04T12:00:00Z")
    ]
    assert artifacts.list("dev", "models")[0].name == "churn"
    assert artifacts.get("dev", "models.churn").current_version == 2
    assert [v.version for v in artifacts.versions("dev", "models.churn")] == [5, 2]
    assert seen == [
        ("/api/v1/environments/dev/artifacts", {}),
        ("/api/v1/environments/dev/artifacts", {"namespace": "models"}),
        (_BASE, {}),
        (f"{_BASE}/versions", {}),
    ]


_UPLOADS = "/api/v1/environments/dev/artifacts/uploads"


def _upload_item(n: int) -> dict[str, str]:
    """Build one item of the uploads list as the server sends it.

    Parameters
    ----------
    n
        A number that makes the id and the time differ.

    Returns
    -------
    dict
        The item.
    """
    return {
        "upload_id": f"3f2b9c1e-5a4d-4c8e-9b1a-0d6e7f8a9b0{n}",
        "artifact": "models.churn",
        "created_at": f"2026-10-07T12:0{n}:00Z",
        "expires_at": f"2026-10-07T13:0{n}:00Z",
    }


def test_list_uploads_parses_a_page_and_sends_its_filters() -> None:
    seen: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == _UPLOADS
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"items": [_upload_item(2)], "next_cursor": "abc"})

    http = httpx.Client(transport=httpx.MockTransport(handle), base_url=_CONFIG.url)
    artifacts = Artifacts(HttpTransport(_CONFIG, http_client=http))
    page = artifacts.list_uploads("dev", "models.churn", limit=5, cursor="zzz")
    assert page == ArtifactUploadPage(
        items=[
            ArtifactUpload(
                upload_id="3f2b9c1e-5a4d-4c8e-9b1a-0d6e7f8a9b02",
                artifact="models.churn",
                created_at="2026-10-07T12:02:00Z",
                expires_at="2026-10-07T13:02:00Z",
            )
        ],
        next_cursor="abc",
    )
    artifacts.list_uploads("dev")
    assert seen == [
        {"name": "models.churn", "limit": "5", "cursor": "zzz"},
        {"limit": "100"},
    ]


def test_iter_uploads_follows_the_cursor_to_the_end() -> None:
    pages = {
        None: {"items": [_upload_item(3), _upload_item(2)], "next_cursor": "c1"},
        "c1": {"items": [_upload_item(1)], "next_cursor": None},
    }
    seen: list[dict[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    http = httpx.Client(transport=httpx.MockTransport(handle), base_url=_CONFIG.url)
    artifacts = Artifacts(HttpTransport(_CONFIG, http_client=http))
    listed = list(artifacts.iter_uploads("dev", "models.churn", page_size=2))
    assert [u.upload_id[-1] for u in listed] == ["3", "2", "1"]
    assert seen == [
        {"name": "models.churn", "limit": "2"},
        {"name": "models.churn", "limit": "2", "cursor": "c1"},
    ]


@pytest.mark.parametrize(
    "answer",
    [
        [],
        {"items": {}, "next_cursor": None},
        {"items": [], "next_cursor": 3},
        {"items": [{"upload_id": "x"}], "next_cursor": None},
        {"items": [{**_upload_item(1), "created_at": 5}], "next_cursor": None},
        "text",
    ],
    ids=["list", "items-not-a-list", "cursor-not-text", "item-without-fields", "time", "text"],
)
def test_an_uploads_answer_that_is_not_a_page_is_an_invalid_response(answer: object) -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=answer)),
        base_url=_CONFIG.url,
    )
    artifacts = Artifacts(HttpTransport(_CONFIG, http_client=http))
    with pytest.raises(InvalidResponse, match="a list of uploads"):
        artifacts.list_uploads("dev")


def test_an_uploads_answer_that_is_not_json_is_an_invalid_response() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>")),
        base_url=_CONFIG.url,
    )
    artifacts = Artifacts(HttpTransport(_CONFIG, http_client=http))
    with pytest.raises(InvalidResponse, match="a list of uploads"):
        artifacts.list_uploads("dev")


def test_an_answer_that_is_not_an_artifact_is_an_invalid_response() -> None:
    for answer in ([{"name": "x"}], {"name": "x"}, "text", None):
        http = httpx.Client(
            transport=httpx.MockTransport(lambda request, a=answer: httpx.Response(200, json=a)),
            base_url=_CONFIG.url,
        )
        artifacts = Artifacts(HttpTransport(_CONFIG, http_client=http))
        with pytest.raises(InvalidResponse):
            artifacts.list("dev")
        with pytest.raises(InvalidResponse):
            artifacts.get("dev", "models.churn")
        with pytest.raises(InvalidResponse):
            artifacts.versions("dev", "models.churn")


# --- the server's limits ------------------------------------------------------------------------


def test_the_upload_scans_with_the_limits_the_server_sends(tmp_path: Path) -> None:
    (tmp_path / "big.bin").write_bytes(b"x" * 20)
    limits = {"max_files": 7, "max_file_bytes": 10, "max_bytes": 100}
    api = _Api(info={"version": "1", "artifact_limits": limits})
    with pytest.raises(ValidationError, match="more than the 10 bytes one file may have") as caught:
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert "the server's operator sets this limit" in str(caught.value)
    assert "LAKANA_ARTIFACTS__MAX_FILE_BYTES" in str(caught.value)
    assert api.calls == []
    raised = _Api(info={"version": "1", "artifact_limits": {**limits, "max_file_bytes": 1000}})
    assert raised.artifacts().upload("dev", "models.churn", tmp_path).version == 3


def test_limits_are_read_once_per_client_and_not_at_all_when_given(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api()
    artifacts = api.artifacts()
    artifacts.upload("dev", "models.churn", tmp_path)
    artifacts.upload("dev", "models.churn", tmp_path)
    assert api.info_calls == 1
    explicit = _Api()
    explicit.artifacts().upload("dev", "models.churn", tmp_path, limits=ScanLimits())
    assert explicit.info_calls == 0


def test_a_server_without_limits_means_the_defaults(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    for info in (
        {"version": "1"},
        {"version": "1", "artifact_limits": None},
        {"version": "1", "artifact_limits": {"max_files": 5}},
        {"version": "1", "artifact_limits": {"max_files": 0, "max_file_bytes": 1, "max_bytes": 1}},
        {
            "version": "1",
            "artifact_limits": {"max_files": True, "max_file_bytes": 1, "max_bytes": 1},
        },
        {
            "version": "1",
            "artifact_limits": {"max_files": "5", "max_file_bytes": 1, "max_bytes": 1},
        },
        {"version": "1", "artifact_limits": [1, 2, 3]},
    ):
        api = _Api(info=info)
        assert api.artifacts().upload("dev", "models.churn", tmp_path).version == 3


def test_an_info_route_that_fails_or_is_not_a_server_means_the_defaults(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    inner = _Api()

    def handle(answer: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
        def go(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/info":
                return answer
            return inner.handle(request)

        return go

    for answer in (
        httpx.Response(404, json={"detail": "no"}),
        httpx.Response(403, json={"detail": "no"}),
        httpx.Response(200, content=b"<html>"),
        httpx.Response(200, json=["not", "an", "object"]),
    ):
        http = httpx.Client(transport=_Streaming(handle(answer)), base_url=_CONFIG.url)
        artifacts = Artifacts(HttpTransport(_CONFIG, http_client=http))
        assert artifacts.upload("dev", "models.churn", tmp_path).version == 3


def test_an_unreachable_server_is_not_hidden_behind_the_defaults(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")

    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    http = httpx.Client(transport=_Streaming(lost), base_url=_CONFIG.url)
    with pytest.raises(LakanaConnectionError):
        Artifacts(HttpTransport(_CONFIG, http_client=http)).upload("dev", "models.churn", tmp_path)


def test_a_list_that_is_not_json_is_an_invalid_response() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"<html>")),
        base_url=_CONFIG.url,
    )
    with pytest.raises(InvalidResponse):
        Artifacts(HttpTransport(_CONFIG, http_client=http)).list("dev")


# --- the consumer's copy: modes, the directory it goes to, what the system refuses ---------------


def _mode(path: Path) -> int:
    """Read a path's permission bits.

    Parameters
    ----------
    path
        Any path.

    Returns
    -------
    int
        The permission bits.
    """
    return stat.S_IMODE(os.lstat(path).st_mode)


def _tree_version() -> dict[str, object]:
    """Build a version of two files in two directories.

    Returns
    -------
    dict
        The version JSON.
    """
    return _served(
        [
            {"path": "a/b/w.bin", "size": 3, "sha256": _FILE_SHA},
            {"path": "m.json", "size": 3, "sha256": _FILE_SHA},
        ],
        is_directory=True,
    )


def test_download_gives_a_private_copy_by_default_and_a_sealed_one_when_asked(
    tmp_path: Path,
) -> None:
    old = os.umask(0o022)
    try:
        artifacts, _ = _download_api(_tree_version())
        artifacts.download("dev", "models.churn", tmp_path / "mine")
        sealed = artifacts.download("dev", "models.churn", tmp_path / "task", read_only=True)
    finally:
        os.umask(old)
    mine, task = tmp_path / "mine", tmp_path / "task"
    try:
        assert [_mode(p) for p in (mine, mine / "a", mine / "a" / "b")] == [0o700] * 3
        assert [_mode(mine / "m.json"), _mode(mine / "a" / "b" / "w.bin")] == [0o600] * 2
        assert [_mode(p) for p in (task, task / "a", task / "a" / "b")] == [0o500] * 3
        assert [_mode(task / "m.json"), _mode(task / "a" / "b" / "w.bin")] == [0o400] * 2
        assert sealed.path == str(task.resolve())
    finally:
        for directory, _names, _files in os.walk(task):
            os.chmod(directory, 0o700)


def test_download_into_an_existing_empty_directory_keeps_that_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mkdir m && cd m && download ... .` must leave the shell in the directory with the files."""
    target = tmp_path / "m"
    target.mkdir()
    target.chmod(0o750)
    before = os.stat(target)
    monkeypatch.chdir(target)
    artifacts, _ = _download_api(_tree_version())
    got = artifacts.download("dev", "models.churn", ".")
    assert got.path == str(target.resolve())
    assert sorted(os.listdir(".")) == ["a", "m.json"]
    assert (Path(os.getcwd()) / "a" / "b" / "w.bin").read_bytes() == _FILE
    after = os.stat(target)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert _mode(target) == 0o750


def test_a_failed_download_into_an_existing_empty_directory_leaves_it_empty_and_present(
    tmp_path: Path,
) -> None:
    files = [
        {"path": "a", "size": 3, "sha256": _FILE_SHA},
        {"path": "b", "size": 3, "sha256": "0" * 64},
    ]
    artifacts, _ = _download_api(_served(files, is_directory=True))
    target = tmp_path / "empty"
    target.mkdir()
    target.chmod(0o750)
    before = os.stat(target)
    with pytest.raises(ArtifactCorrupt):
        artifacts.download("dev", "models.churn", target)
    assert os.listdir(target) == []
    assert os.stat(target).st_ino == before.st_ino
    assert _mode(target) == 0o750
    with pytest.raises(ArtifactCorrupt):
        artifacts.download("dev", "models.churn", tmp_path / "missing" / "deeper")
    assert not (tmp_path / "missing" / "deeper").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes everywhere")
def test_an_empty_directory_that_cannot_be_written_to_is_refused_before_any_file_is_asked_for(
    tmp_path: Path,
) -> None:
    target = tmp_path / "locked"
    target.mkdir()
    target.chmod(0o500)
    artifacts, paths = _download_api(_tree_version())
    try:
        with pytest.raises(ManifestError, match=r"Cannot write into .*Permission denied"):
            artifacts.download("dev", "models.churn", target)
    finally:
        target.chmod(0o700)
    assert all("/blobs/" not in path for path in paths)


def test_a_version_larger_than_the_free_space_is_refused_with_both_numbers_before_any_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: types.SimpleNamespace(free=4))
    artifacts, paths = _download_api(_tree_version())  # 6 bytes
    with pytest.raises(ManifestError) as caught:
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert "needs 6 bytes but only 4 are free" in str(caught.value)
    assert all("/blobs/" not in path for path in paths)
    assert list(tmp_path.iterdir()) == []


def test_sizes_of_thousands_of_digits_are_refused_not_formatted(tmp_path: Path) -> None:
    huge = 10**4200
    files = [
        {"path": "a", "size": huge, "sha256": _FILE_SHA},
        {"path": "b", "size": huge, "sha256": "1" * 64},
    ]
    artifacts, paths = _download_api(_served(files, is_directory=True))
    with pytest.raises(ManifestError, match="from 0 to below 10"):
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert all("/blobs/" not in path for path in paths)
    assert list(tmp_path.iterdir()) == []


def test_a_body_that_cannot_be_decoded_ends_the_download_with_a_message_and_keeps_nothing(
    tmp_path: Path,
) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    artifacts, _ = _download_api(
        _served(files, is_directory=True),
        lambda request: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=b"not gzip at all"
        ),
    )
    with pytest.raises(InvalidResponse, match="could not be decoded"):
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert list(tmp_path.iterdir()) == []


def test_a_file_system_that_refuses_a_write_is_a_message_and_the_staging_directory_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Full:
        def __init__(self, inner: Any) -> None:  # ruff: ignore[any-type]
            self.inner = inner

        def __enter__(self) -> Full:
            return self

        def __exit__(self, *exc: object) -> None:
            self.inner.close()

        def write(self, data: bytes) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

    real = os.fdopen
    monkeypatch.setattr(os, "fdopen", lambda descriptor, how: Full(real(descriptor, how)))
    artifacts, _ = _download_api(_tree_version())
    with pytest.raises(ManifestError) as caught:
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert "There is no space left on the disk while writing" in str(caught.value)
    assert list(tmp_path.iterdir()) == []
    monkeypatch.undo()
    refusing = os.mkdir

    def no_directories(path: Any, *args: Any, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        if Path(path).name == "a":
            raise OSError(errno.ENAMETOOLONG, "File name too long")
        refusing(path, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", no_directories)
    with pytest.raises(ManifestError, match=r"Cannot write a/b/w.bin .*File name too long"):
        artifacts.download("dev", "models.churn", tmp_path / "copy2")
    assert list(tmp_path.iterdir()) == []


_BUSY = {"detail": "The server is busy with other downloads; try again in a moment."}


def test_a_busy_server_is_asked_again_for_the_same_file_to_download(
    tmp_path: Path, waits: list[float]
) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    asked: list[int] = []

    def busy_twice(request: httpx.Request) -> httpx.Response:
        asked.append(1)
        if len(asked) <= 2:
            return httpx.Response(503, json={**_BUSY, "type": "DownloadConcurrencyLimitError"})
        return httpx.Response(200, content=_FILE)

    artifacts, _ = _download_api(_served(files, is_directory=True), busy_twice)
    got = artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert got.file_count == 1
    assert (tmp_path / "copy" / "a").read_bytes() == _FILE
    assert len(asked) == 3
    assert waits == [1.0, 2.0]


def test_a_server_that_stays_busy_ends_the_download_saying_so(
    tmp_path: Path, waits: list[float]
) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    asked: list[int] = []

    def busy(request: httpx.Request) -> httpx.Response:
        asked.append(1)
        return httpx.Response(503, json={**_BUSY, "type": "DownloadConcurrencyLimitError"})

    artifacts, _ = _download_api(_served(files, is_directory=True), busy)
    with pytest.raises(ServerError) as caught:
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    text = str(caught.value)
    assert "was busy with other downloads" in text
    assert "downloading a" in text
    assert "6 tries" in text
    assert "about 30 seconds" in text
    assert "The server is busy with other downloads; try again in a moment." in text
    assert caught.value.status == 503
    assert len(asked) == 6  # a literal, not the constant
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0]  # downloads can hold a slot for minutes
    assert list(tmp_path.iterdir()) == []


def test_a_storage_that_stays_unavailable_is_not_called_a_busy_server(
    tmp_path: Path, waits: list[float]
) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    detail = "The file could not be read from storage right now; try again shortly."

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": detail, "type": "StorageUnavailableError"})

    artifacts, _ = _download_api(_served(files, is_directory=True), unavailable)
    with pytest.raises(ServerError) as caught:
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    text = str(caught.value)
    assert "could not serve a" in text
    assert detail in text
    assert "busy" not in text
    assert "6 tries" in text
    assert caught.value.status == 503
    assert caught.value.error_type == "StorageUnavailableError"
    assert waits == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_a_503_that_names_no_cause_says_the_server_could_not_serve_the_file(
    tmp_path: Path, waits: list[float]
) -> None:
    """A proxy's 503 has no Lakana error type: its text is shown as it is."""
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    artifacts, _ = _download_api(
        _served(files, is_directory=True),
        lambda request: httpx.Response(503, text="upstream unavailable"),
    )
    with pytest.raises(ServerError) as caught:
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert "could not serve a" in str(caught.value)
    assert "upstream unavailable" in str(caught.value)
    assert "busy" not in str(caught.value)


def test_only_a_503_is_asked_again(tmp_path: Path, waits: list[float]) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    asked: list[int] = []

    def broken(request: httpx.Request) -> httpx.Response:
        asked.append(1)
        return httpx.Response(500, json={"detail": "boom"})

    artifacts, _ = _download_api(_served(files, is_directory=True), broken)
    with pytest.raises(ServerError, match="boom"):
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    assert asked == [1]
    assert waits == []


def test_a_cut_download_says_the_server_may_end_one_that_is_too_slow(tmp_path: Path) -> None:
    class Cut(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"ab"
            raise httpx.RemoteProtocolError("peer closed the connection before the body ended")

    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    artifacts, _ = _download_api(
        _served(files, is_directory=True), lambda request: httpx.Response(200, stream=Cut())
    )
    with pytest.raises(LakanaConnectionError) as caught:
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    text = " ".join(str(caught.value).split())
    assert "download it again" in text.lower()
    assert "slower than 64 KiB/s" in text
    assert "faster connection" in text
    assert "smaller" in text
    assert "ended before a was complete" in text
    assert "Could not reach" not in text
    assert "RemoteProtocolError" not in text


def test_a_server_that_cannot_be_reached_for_a_file_says_so_with_the_cause(tmp_path: Path) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    artifacts, _ = _download_api(_served(files, is_directory=True), refused)
    with pytest.raises(LakanaConnectionError) as caught:
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    text = " ".join(str(caught.value).split())
    assert "failed while downloading a" in text
    assert "Could not reach http://api.example: ConnectError" in text
    assert "ended before" not in text


# --- answers of the wrong type ------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"is_directory": 1},
        {"is_directory": "true"},
        {"version": True},
        {"version": "4"},
        {"version": 4.0},
        {"size_bytes": True},
        {"size_bytes": "9"},
        {"file_count": True},
        {"file_count": None},
    ],
    ids=repr,
)
def test_a_version_with_a_field_of_the_wrong_type_is_an_invalid_response(
    tmp_path: Path, change: dict[str, object]
) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    artifacts, paths = _download_api({**_served(files, is_directory=True), **change})
    with pytest.raises(InvalidResponse, match="was not what a Lakana server sends"):
        artifacts.download("dev", "models.churn", tmp_path / "copy")
    with pytest.raises(InvalidResponse):
        artifacts.version("dev", "models.churn")
    with pytest.raises(InvalidResponse):
        artifacts.versions("dev", "models.churn")
    assert all("/blobs/" not in path for path in paths)


def test_a_limit_that_is_true_is_not_the_number_one(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"a")
    (tmp_path / "b").write_bytes(b"b")
    api = _Api(
        info={
            "version": "1",
            "artifact_limits": {"max_files": True, "max_file_bytes": 10, "max_bytes": 100},
        }
    )
    assert api.artifacts().upload("dev", "models.churn", tmp_path).version == 3  # the defaults
    one = _Api(
        info={
            "version": "1",
            "artifact_limits": {"max_files": 1, "max_file_bytes": 10, "max_bytes": 100},
        }
    )
    with pytest.raises(ValidationError, match="1"):
        one.artifacts().upload("dev", "models.churn", tmp_path)


def test_an_answer_that_is_a_redirect_says_to_check_the_url() -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(301, headers={"Location": "https://elsewhere/"})
        ),
        base_url=_CONFIG.url,
    )
    artifacts = Artifacts(HttpTransport(_CONFIG, http_client=http))
    for call in (
        lambda: artifacts.get("dev", "models.churn"),
        lambda: artifacts.version("dev", "models.churn"),
    ):
        with pytest.raises(LakanaError, match=r"redirect .*check the profile's URL") as caught:
            call()
        assert str(caught.value) != ""


_MANY_DIRECTORIES = textwrap.dedent(
    """
    import hashlib, os, resource, stat, sys
    import httpx
    from lakana._artifact_manifest import manifest_digest
    from lakana._config import ResolvedConfig
    from lakana._http import HttpTransport
    from lakana.resources.artifacts import Artifacts

    root = sys.argv[1]
    body = b"abc"
    sha = hashlib.sha256(body).hexdigest()
    files = [{"path": f"d{i:03}/f", "size": 3, "sha256": sha} for i in range(120)]
    digest = manifest_digest(True, [(f["path"], f["size"], f["sha256"]) for f in files])
    version = {
        "version": 4, "manifest_sha256": digest, "size_bytes": 360, "file_count": 120,
        "is_directory": True, "task_run_id": None, "run_id": None, "project_version_id": None,
        "commit_sha": None, "created_by": "admin", "created_at": "2026-10-04T12:00:00Z",
        "deleted_at": None, "deleted_by": None, "files": files, "deleted_versions": [],
    }

    def handle(request):
        if "/blobs/" in request.url.path:
            return httpx.Response(200, content=body)
        return httpx.Response(200, json=version)

    http = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://api.example")
    config = ResolvedConfig(url="http://api.example", token="lakana_pat_abc_supersecret")
    artifacts = Artifacts(HttpTransport(config, http_client=http))
    target = os.path.join(root, "here")
    os.mkdir(target)
    _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
    artifacts.download("dev", "models.churn", target, read_only=True)
    names = sorted(os.listdir(target))
    assert names == [f"d{i:03}" for i in range(120)], names
    for name in names:
        directory = os.path.join(target, name)
        assert stat.S_IMODE(os.lstat(directory).st_mode) == 0o500, name
        assert stat.S_IMODE(os.lstat(os.path.join(directory, "f")).st_mode) == 0o400, name
    for name in names:
        os.chmod(os.path.join(target, name), 0o700)
    print("ok")
    """
)


def test_a_read_only_copy_of_many_directories_fits_a_small_descriptor_limit(
    tmp_path: Path,
) -> None:
    """One descriptor per top-level directory, held to the end, ran out at the usual 1,024."""
    done = subprocess.run(
        [sys.executable, "-c", _MANY_DIRECTORIES, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (done.returncode, done.stdout.strip()) == (0, "ok"), done.stderr[-2000:]


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
def test_a_download_leaves_no_descriptor_open(tmp_path: Path) -> None:
    files = [{"path": f"d{i}/f", "size": 3, "sha256": _FILE_SHA} for i in range(5)] + [
        {"path": "top", "size": 3, "sha256": _FILE_SHA}
    ]
    artifacts, _ = _download_api(_served(files, is_directory=True))
    (tmp_path / "plain").mkdir()
    (tmp_path / "sealed").mkdir()
    before = open_descriptors()
    artifacts.download("dev", "models.churn", tmp_path / "plain")
    artifacts.download("dev", "models.churn", tmp_path / "sealed", read_only=True)
    artifacts.download("dev", "models.churn", tmp_path / "new", read_only=True)
    after = open_descriptors()
    for directory, _names, _files in os.walk(tmp_path):
        os.chmod(directory, 0o700)
    assert after == before


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc/self/fd")
def test_a_refused_or_failed_download_leaves_no_descriptor_open(tmp_path: Path) -> None:
    files = [{"path": "a", "size": 3, "sha256": _FILE_SHA}]
    artifacts, _ = _download_api(_served(files, is_directory=True))
    wrong, _ = _download_api(_served(files, is_directory=True), b"abcdefgh")
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "keep").write_text("x")
    (tmp_path / "empty").mkdir()
    before = open_descriptors()
    for _ in range(3):
        with pytest.raises(LakanaError):
            artifacts.download("dev", "models.churn", tmp_path / "full")
        with pytest.raises(ArtifactCorrupt):
            wrong.download("dev", "models.churn", tmp_path / "empty")
        with pytest.raises(ArtifactCorrupt):
            wrong.download("dev", "models.churn", tmp_path / "missing")
    after = open_descriptors()
    assert after == before


# --- deleting a version, dropping an artifact, aborting an upload ------------------------------


def _delete_api(
    handle: Callable[[httpx.Request], httpx.Response],
) -> tuple[Artifacts, list[tuple[str, str, dict[str, str], float | None]]]:
    """Build a resource whose server answers every request with `handle`.

    Parameters
    ----------
    handle
        Answers a request.

    Returns
    -------
    tuple
        The resource and the requests it sent: method, path, query and read timeout.
    """
    seen: list[tuple[str, str, dict[str, str], float | None]] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.method,
                request.url.path,
                dict(request.url.params),
                request.extensions["timeout"]["read"],
            )
        )
        return handle(request)

    http = httpx.Client(transport=httpx.MockTransport(record), base_url=_CONFIG.url)
    return Artifacts(HttpTransport(_CONFIG, http_client=http)), seen


def test_delete_version_and_drop_send_deletes_with_the_reason() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/versions/3"):
            return httpx.Response(200, json={**_VERSION, "deleted_at": "2026-10-04T13:00:00Z"})
        return httpx.Response(204)

    artifacts, seen = _delete_api(handle)
    deleted = artifacts.delete_version("dev", "models.churn", 3, break_glass_reason="leaked data")
    assert (deleted.version, deleted.deleted_at) == (3, "2026-10-04T13:00:00Z")
    assert artifacts.drop("dev", "models.churn") is None
    assert [(m, p, q) for m, p, q, _ in seen] == [
        ("DELETE", f"{_BASE}/versions/3", {"break_glass_reason": "leaked data"}),
        ("DELETE", _BASE, {}),
    ]


def test_delete_version_returns_what_was_freed_and_kept_as_a_typed_result() -> None:
    body = {
        **_VERSION,
        "deleted_at": "2026-10-04T13:00:00Z",
        "freed_files": 3,
        "freed_bytes": 1200,
        "kept_files": 3,
        "kept_bytes": 340,
        "kept": [
            {"reason": "other_version", "files": 2, "bytes": 300, "version": 5},
            {"reason": "open_upload", "files": 1, "bytes": 40, "version": None},
        ],
    }
    artifacts, _ = _delete_api(lambda request: httpx.Response(200, json=body))
    deleted = artifacts.delete_version("dev", "models.churn", 3)
    assert isinstance(deleted, DeletedArtifactVersion) and isinstance(deleted, ArtifactVersion)
    assert (deleted.version, deleted.freed_files, deleted.freed_bytes) == (3, 3, 1200)
    assert (deleted.kept_files, deleted.kept_bytes) == (3, 340)
    assert deleted.kept == [
        ArtifactKept("other_version", files=2, bytes=300, version=5),
        ArtifactKept("open_upload", files=1, bytes=40, version=None),
    ]


def test_an_older_server_that_does_not_say_what_it_freed_gives_none_not_zero() -> None:
    artifacts, _ = _delete_api(
        lambda request: httpx.Response(200, json={**_VERSION, "deleted_at": "2026-10-04T13:00:00Z"})
    )
    deleted = artifacts.delete_version("dev", "models.churn", 3)
    assert (deleted.freed_files, deleted.freed_bytes) == (None, None)
    assert (deleted.kept_files, deleted.kept_bytes, deleted.kept) == (None, None, [])


@pytest.mark.parametrize(
    "bad",
    [
        {"freed_files": "3", "freed_bytes": 1, "kept_files": 0, "kept_bytes": 0, "kept": []},
        {"freed_files": 1, "freed_bytes": 1, "kept_files": 0, "kept_bytes": 0, "kept": {}},
        {
            "freed_files": 1,
            "freed_bytes": 1,
            "kept_files": 1,
            "kept_bytes": 1,
            "kept": [{"reason": 7, "files": 1, "bytes": 1, "version": None}],
        },
        {
            "freed_files": 1,
            "freed_bytes": 1,
            "kept_files": 1,
            "kept_bytes": 1,
            "kept": [{"reason": "other_version", "files": 1, "bytes": 1, "version": "5"}],
        },
    ],
    ids=["text-count", "kept-not-a-list", "reason-not-text", "version-not-a-number"],
)
def test_a_delete_answer_with_wrong_types_is_an_invalid_response(bad: dict[str, object]) -> None:
    artifacts, _ = _delete_api(
        lambda request: httpx.Response(
            200, json={**_VERSION, "deleted_at": "2026-10-04T13:00:00Z", **bad}
        )
    )
    with pytest.raises(InvalidResponse, match="a version"):
        artifacts.delete_version("dev", "models.churn", 3)


def test_a_delete_and_a_drop_wait_longer_than_the_default_for_the_server() -> None:
    """A drop removes thousands of files inside the request: 30 s would call a success a loss."""
    artifacts, seen = _delete_api(
        lambda request: httpx.Response(
            200 if "/versions/" in request.url.path else 204,
            json={**_VERSION, "deleted_at": "2026-10-04T13:00:00Z"}
            if "/versions/" in request.url.path
            else None,
        )
    )
    artifacts.delete_version("dev", "models.churn", 3)
    artifacts.drop("dev", "models.churn")
    assert [t for *_, t in seen] == [module._DELETE_TIMEOUT_SECONDS, module._DROP_TIMEOUT_SECONDS]
    assert 30.0 < module._DELETE_TIMEOUT_SECONDS <= module._DROP_TIMEOUT_SECONDS


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "1", None])
def test_a_version_to_delete_must_be_a_positive_number_before_a_request_is_sent(
    bad: object,
) -> None:
    artifacts, seen = _delete_api(lambda request: httpx.Response(500))
    with pytest.raises(ValidationError, match="A version is a positive number"):
        artifacts.delete_version("dev", "models.churn", bad)  # ty: ignore[invalid-argument-type]
    assert seen == []


def test_a_name_that_is_not_dotted_is_refused_before_a_request_is_sent() -> None:
    artifacts, seen = _delete_api(lambda request: httpx.Response(500))
    with pytest.raises(ValidationError, match="Expected a dotted"):
        artifacts.drop("dev", "churn")
    with pytest.raises(ValidationError, match="Expected a dotted"):
        artifacts.delete_version("dev", "churn", 1)
    with pytest.raises(ValidationError, match="Expected a dotted"):
        artifacts.abort_upload("dev", "churn", _UPLOAD_ID)
    assert seen == []


def test_a_delete_whose_answer_is_lost_says_it_may_have_happened_and_what_to_run() -> None:
    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    artifacts, _ = _delete_api(lost)
    with pytest.raises(LakanaConnectionError) as deleting:
        artifacts.delete_version("dev", "models.churn", 3)
    text = " ".join(str(deleting.value).split())
    assert "may or may not have been deleted" in text
    assert "`lakana artifact versions list models.churn --env dev`" in text
    assert "run the delete again" in text
    with pytest.raises(LakanaConnectionError) as dropping:
        artifacts.drop("dev", "models.churn")
    text = " ".join(str(dropping.value).split())
    assert "may or may not be dropped" in text
    assert "`lakana artifact list --env dev`" in text
    assert "run the drop again" in text


@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_failure_is_a_lost_answer_for_a_delete_and_a_drop(status: int) -> None:
    """A proxy that answers 502-504 may have passed the request on and lost the answer."""
    artifacts, _ = _delete_api(
        lambda request: httpx.Response(status, json={"detail": "upstream trouble"})
    )
    with pytest.raises(ServerError) as deleting:
        artifacts.delete_version("dev", "models.churn", 3)
    assert deleting.value.status == status
    assert "may or may not have been deleted" in " ".join(str(deleting.value).split())
    assert "upstream trouble" in str(deleting.value)
    with pytest.raises(ServerError) as dropping:
        artifacts.drop("dev", "models.churn")
    assert dropping.value.status == status
    assert "may or may not be dropped" in " ".join(str(dropping.value).split())


def test_another_server_error_is_not_called_lost() -> None:
    artifacts, _ = _delete_api(lambda request: httpx.Response(500, json={"detail": "boom"}))
    for call in (
        lambda: artifacts.delete_version("dev", "models.churn", 3),
        lambda: artifacts.drop("dev", "models.churn"),
    ):
        with pytest.raises(ServerError) as caught:
            call()
        assert str(caught.value) == "boom"


@pytest.mark.parametrize("fault", [httpx.ConnectError, httpx.ConnectTimeout])
def test_a_connection_that_never_opened_says_nothing_was_sent(
    fault: type[httpx.TransportError],
) -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise fault("refused", request=request)

    artifacts, _ = _delete_api(refused)
    for call, what in (
        (lambda: artifacts.delete_version("dev", "models.churn", 3), "not deleted"),
        (lambda: artifacts.drop("dev", "models.churn"), "not dropped"),
    ):
        with pytest.raises(LakanaConnectionError) as caught:
            call()
        text = " ".join(str(caught.value).split())
        assert "may or may not" not in text
        assert what in text
        assert "profile's URL" in text


def test_a_refused_delete_is_not_called_lost() -> None:
    artifacts, _ = _delete_api(lambda request: httpx.Response(403, json={"detail": "no"}))
    with pytest.raises(LakanaError) as caught:
        artifacts.drop("dev", "models.churn")
    assert "may or may not" not in str(caught.value)


def test_abort_upload_sends_one_delete_and_returns_nothing() -> None:
    artifacts, seen = _delete_api(lambda request: httpx.Response(204))
    assert artifacts.abort_upload("dev", "models.churn", _UPLOAD_ID) is None
    assert [(m, p, q) for m, p, q, _ in seen] == [("DELETE", f"{_BASE}/uploads/{_UPLOAD_ID}", {})]


def test_abort_upload_says_when_the_upload_is_gone() -> None:
    artifacts, _ = _delete_api(
        lambda request: httpx.Response(409, json={"detail": _GONE, "type": "ConflictError"})
    )
    with pytest.raises(Conflict, match="expired or does not exist"):
        artifacts.abort_upload("dev", "models.churn", _UPLOAD_ID)


def _interrupt() -> None:
    """Stop an upload the way a person's Ctrl-C does.

    Raises
    ------
    KeyboardInterrupt
        Always.
    """
    raise KeyboardInterrupt


def test_an_upload_that_gives_up_after_a_failure_withdraws_its_own_upload(
    tmp_path: Path,
) -> None:
    target = tmp_path / "w.bin"
    target.write_bytes(b"0123456789")

    def rewrite() -> None:
        target.write_bytes(b"0123456780")

    api = _Api(after_begin=rewrite)
    with pytest.raises(ManifestError, match=r"w\.bin changed after it was scanned"):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert [method for method, _ in api.calls] == ["POST", "PUT", "DELETE"]
    assert api.aborts == [f"{_BASE}/uploads/u-1"]
    assert api.params[-1] == {}


def test_an_upload_withdraws_itself_when_the_server_stays_busy(
    tmp_path: Path, waits: list[float]
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(busy=99)
    with pytest.raises(ServerError, match="Too many concurrent uploads"):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert api.aborts == [f"{_BASE}/uploads/u-1"]


def test_an_upload_that_is_interrupted_withdraws_itself_and_the_interrupt_goes_on(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(on_put=_interrupt)
    with pytest.raises(KeyboardInterrupt):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert api.aborts == [f"{_BASE}/uploads/u-1"]


def test_a_failed_withdrawal_does_not_hide_the_error_that_caused_it(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    for abort in (500, 403, "lost"):
        api = _Api(busy=99, abort=abort)
        with pytest.raises(ServerError, match="Too many concurrent uploads"):
            api.artifacts().upload("dev", "models.churn", tmp_path)
        assert len(api.aborts) == 1


def test_an_upload_the_server_has_lost_is_not_withdrawn_before_it_starts_again(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(gone_on_put=1)
    assert api.artifacts().upload("dev", "models.churn", tmp_path).version == 3
    assert api.aborts == []


def test_a_commit_whose_answer_may_have_been_lost_is_never_followed_by_a_withdrawal(
    tmp_path: Path, waits: list[float]
) -> None:
    """After a lost answer or a 5xx the version may exist; its upload must stay."""
    (tmp_path / "m.json").write_bytes(b"m")
    for fault in (
        ("lost",) * 4,
        (502,) * 4,
        ("lost", 404),
        (502, 403),
        ("lost", 422),
        (504, 403),
    ):
        api = _Api(commit_faults=fault)
        with pytest.raises(LakanaError):
            api.artifacts().upload("dev", "models.churn", tmp_path)
        assert api.aborts == [], fault


@pytest.mark.parametrize(
    ("status", "error"),
    [(404, NotFound), (403, PermissionDenied), (422, ValidationError)],
)
def test_a_commit_that_was_refused_withdraws_its_upload_and_the_refusal_goes_on(
    tmp_path: Path, status: int, error: type[LakanaError]
) -> None:
    """A definite 4xx proves no version exists: the upload would sit open for an hour."""
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(commit_faults=(status,))
    with pytest.raises(error, match="bad gateway"):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert [method for method, _ in api.calls] == ["POST", "PUT", "POST", "DELETE"]
    assert api.aborts == [f"{_BASE}/uploads/u-1"]


def test_a_commit_refused_with_a_conflict_that_is_not_a_restart_withdraws_its_upload(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(then_conflict="models.churn has 16 uploads in progress.")
    with pytest.raises(Conflict, match="16 uploads in progress"):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert api.aborts == [f"{_BASE}/uploads/u-1"]


def test_each_upload_that_was_restarted_is_withdrawn_before_the_next_begins(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(conflicts=3)
    with pytest.raises(Conflict, match="was lost 3 times"):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert [method for method, _ in api.calls] == ["POST", "PUT", "POST", "DELETE"] * 3
    restarted = _Api(conflicts=1)
    assert restarted.artifacts().upload("dev", "models.churn", tmp_path).version == 3
    assert [method for method, _ in restarted.calls] == [
        *("POST", "PUT", "POST", "DELETE"),
        *("POST", "PUT", "POST"),
    ]


def test_a_failed_withdrawal_after_a_refused_commit_neither_hides_it_nor_stops_a_restart(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    for abort in (500, 403, "lost"):
        refused = _Api(commit_faults=(403,), abort=abort)
        with pytest.raises(PermissionDenied, match="bad gateway"):
            refused.artifacts().upload("dev", "models.churn", tmp_path)
        assert len(refused.aborts) == 1
        restarted = _Api(conflicts=1, abort=abort)
        assert restarted.artifacts().upload("dev", "models.churn", tmp_path).version == 3
        assert len(restarted.aborts) == 1


def test_an_upload_the_server_reports_gone_at_the_commit_is_not_withdrawn(
    tmp_path: Path,
) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(conflicts=1, conflict_detail=_GONE)
    assert api.artifacts().upload("dev", "models.churn", tmp_path).version == 3
    assert api.aborts == []


def test_a_withdrawal_waits_only_a_few_seconds_for_the_server(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api(busy=99)
    with pytest.raises(ServerError):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    (read,) = [t for method, t in api.timeouts if method == "DELETE"]
    assert read == module._WITHDRAW_TIMEOUT_SECONDS
    assert 0 < module._WITHDRAW_TIMEOUT_SECONDS <= 5.0


@pytest.mark.parametrize("bad", ["u-1", "", "not an id", "../x", _UPLOAD_ID + "0"])
def test_an_upload_id_that_is_not_one_is_refused_before_a_request_is_sent(bad: str) -> None:
    artifacts, seen = _delete_api(lambda request: httpx.Response(500))
    with pytest.raises(ValidationError, match="upload id") as caught:
        artifacts.abort_upload("dev", "models.churn", bad)
    assert "[" not in str(caught.value)
    assert "lakana artifact upload" in str(caught.value)
    assert seen == []


def test_an_interrupt_during_the_commit_does_not_withdraw_the_upload(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_bytes(b"m")
    api = _Api()
    real = api.handle

    def interrupted(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commit"):
            raise KeyboardInterrupt
        return real(request)

    api.handle = interrupted  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        api.artifacts().upload("dev", "models.churn", tmp_path)
    assert api.aborts == []
