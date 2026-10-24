"""Deleting a version, dropping an artifact and aborting an upload over REST."""

from __future__ import annotations

import hashlib
import re
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.artifacts.service import DELETE_FROM_RUN_MESSAGE, UPLOAD_GONE, ArtifactService
from lakana_core.auth import AllowAllAuthorization
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.models import (
    ArtifactUpload,
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
from tests.conftest import AUTH_HEADER
from tests.server_test_helpers import (
    create_environment,
    create_namespace,
    create_table,
    upload_artifact,
)

_BASE = "/api/v1/environments/dev/namespaces/models/artifacts/churn"
_SHARED = b"shared"
_DOC_FILE = b"abc"
_SHA = hashlib.sha256(_DOC_FILE).hexdigest()
_DOC = {
    "format": 1,
    "is_directory": False,
    "files": [
        {"path": "m", "size": len(_DOC_FILE), "sha256": hashlib.sha256(_DOC_FILE).hexdigest()}
    ],
}


def _two_versions(client: TestClient) -> None:
    """Store two versions of `models.churn` that share one file.

    Parameters
    ----------
    client
        The test client.
    """
    create_environment(client)
    create_namespace(client, "dev", "models")
    upload_artifact(client, "dev", "models.churn", {"m": b"one", "w": _SHARED})
    upload_artifact(client, "dev", "models.churn", {"m": b"two", "w": _SHARED})


def _user(client: TestClient, name: str, role: str | None, kind: str = "USER") -> dict[str, str]:
    """Create a principal, grant it a role on `dev` if given, and return its auth header.

    Parameters
    ----------
    client
        The test client.
    name
        The principal's name.
    role
        A role to grant on the environment, or `None` for no grant at all.
    kind
        ``USER`` or ``SERVICE_ACCOUNT``.

    Returns
    -------
    dict
        The `Authorization` header.
    """
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": name, "type": kind})
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


def _versions(client: TestClient) -> list[tuple[int, bool]]:
    """List `models.churn`'s versions as the admin sees them.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    list of tuple
        Number and whether it is live, newest first.
    """
    listed = client.get(f"{_BASE}/versions", headers=AUTH_HEADER).json()
    return [(v["version"], v["deleted_at"] is None) for v in listed]


def _events(client: TestClient, event_type: str) -> list[dict[str, object]]:
    """Fetch the audit events of one type.

    Parameters
    ----------
    client
        The test client.
    event_type
        The type.

    Returns
    -------
    list of dict
        The events.
    """
    found = client.get(
        "/api/v1/audit-events", params={"event_type": event_type}, headers=AUTH_HEADER
    )
    assert found.status_code == 200, found.text
    return found.json()["items"]


def _protect(client: TestClient) -> None:
    """Make `dev` a protected environment.

    Parameters
    ----------
    client
        The test client.
    """
    protected = client.patch(
        "/api/v1/environments/dev", headers=AUTH_HEADER, json={"is_protected": True}
    )
    assert protected.status_code == 200, protected.text


# --- deleting a version ----------------------------------------------------------------------


def test_deleting_a_version_keeps_the_other_readable(client: TestClient) -> None:
    _two_versions(client)
    deleted = client.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER)
    assert deleted.status_code == 200
    body = deleted.json()
    assert (body["version"], body["deleted_by"], body["files"]) == (1, "admin", [])
    assert body["deleted_at"] is not None
    shared = hashlib.sha256(_SHARED).hexdigest()
    assert client.get(f"{_BASE}/versions/2/blobs/{shared}", headers=AUTH_HEADER).content == _SHARED
    gone = client.get(f"{_BASE}/versions/1/blobs/{shared}", headers=AUTH_HEADER)
    assert (gone.status_code, gone.json()["detail"]) == (
        404,
        "Version 1 of models.churn was deleted; its bytes are gone. "
        "Read a version that exists: `lakana artifact versions list models.churn --env dev` "
        "shows which.",
    )
    assert _versions(client) == [(2, True), (1, False)]
    listed = client.get(f"{_BASE}/versions", headers=AUTH_HEADER).json()
    assert (listed[1]["deleted_by"], listed[1]["files"]) == ("admin", [])
    again = client.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER)
    assert (again.status_code, again.json()["detail"]) == (
        409,
        "Version 1 of models.churn is already deleted.",
    )


def test_a_delete_says_what_it_freed_and_what_it_kept(client: TestClient) -> None:
    _two_versions(client)
    deleted = client.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER)
    assert deleted.status_code == 200
    body = deleted.json()
    assert (body["freed_files"], body["freed_bytes"]) == (1, len(b"one"))
    assert (body["kept_files"], body["kept_bytes"]) == (1, len(_SHARED))
    assert body["kept"] == [
        {"reason": "other_version", "files": 1, "bytes": len(_SHARED), "version": 2}
    ]
    assert "path" not in str(body["kept"]) and "principal" not in str(body["kept"])


def test_a_delete_that_keeps_nothing_has_an_empty_kept_list(client: TestClient) -> None:
    _two_versions(client)
    client.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER)
    body = client.delete(f"{_BASE}/versions/2", headers=AUTH_HEADER).json()
    assert (body["freed_files"], body["kept_files"], body["kept_bytes"], body["kept"]) == (
        2,
        0,
        0,
        [],
    )
    assert body["freed_bytes"] == len(b"two") + len(_SHARED)


def test_a_caller_who_may_delete_but_not_read_is_not_told_which_version_keeps_a_file(
    client: TestClient,
) -> None:
    """A raw `DROP_TABLE` grant, without `READ_TABLE`: the reason is given, the number is not."""
    _two_versions(client)
    client.post("/api/v1/principals", headers=AUTH_HEADER, json={"name": "janitor", "type": "USER"})
    granted = client.post(
        "/api/v1/grants",
        headers=AUTH_HEADER,
        json={
            "principal": "janitor",
            "action": "DROP_TABLE",
            "kind": "ENVIRONMENT",
            "environment": "dev",
        },
    )
    assert granted.status_code == 201, granted.text
    token = client.post(
        "/api/v1/principals/janitor/tokens", headers=AUTH_HEADER, json={"name": "janitor"}
    ).json()["token"]
    janitor = {"Authorization": f"Bearer {token}"}
    assert client.get(f"{_BASE}/versions", headers=janitor).status_code == 403
    body = client.delete(f"{_BASE}/versions/1", headers=janitor).json()
    assert body["kept"] == [
        {"reason": "other_version", "files": 1, "bytes": len(_SHARED), "version": None}
    ]


def test_an_open_upload_is_a_reason_and_names_nobody(client: TestClient) -> None:
    _two_versions(client)
    other = _user(client, "other", "DATA_SCIENTIST")
    document = {
        "format": 1,
        "is_directory": True,
        "files": [
            {"path": "m", "size": 3, "sha256": hashlib.sha256(b"one").hexdigest()},
        ],
    }
    assert client.post(f"{_BASE}/uploads", headers=other, json=document).status_code == 201
    body = client.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER).json()
    assert body["kept"] == [
        {"reason": "other_version", "files": 1, "bytes": len(_SHARED), "version": 2},
        {"reason": "open_upload", "files": 1, "bytes": 3, "version": None},
    ]
    assert "'other'" not in str(body["kept"])  # the uploader's name


@pytest.mark.parametrize(
    ("version", "status"),
    [
        ("current", 422),
        ("0", 422),
        ("01", 422),
        ("+1", 422),
        ("-1", 422),
        ("1.0", 422),
        ("1e3", 422),
        (chr(0x661), 422),  # an Arabic-Indic digit one
        ("99999999999", 422),
        ("9" * 5000, 422),  # five thousand digits
        ("9", 404),
        ("2147483647", 404),
    ],
    ids=lambda value: value if len(str(value)) < 20 else "5000-digits",
)
@pytest.mark.slow
def test_a_version_must_be_a_bounded_positive_number(
    client: TestClient, version: str, status: int
) -> None:
    _two_versions(client)
    refused = client.delete(f"{_BASE}/versions/{version}", headers=AUTH_HEADER)
    assert refused.status_code == status
    assert _versions(client) == [(2, True), (1, True)]
    if status == 422 and len(version) > 5:
        assert version not in refused.text  # never echoed


def test_numbers_are_never_reused_and_gaps_are_deletable_around(client: TestClient) -> None:
    _two_versions(client)
    assert client.delete(f"{_BASE}/versions/2", headers=AUTH_HEADER).status_code == 200
    third = upload_artifact(client, "dev", "models.churn", {"m": b"three"})
    assert third["version"] == 3
    assert client.delete(f"{_BASE}/versions/2", headers=AUTH_HEADER).status_code == 409
    assert client.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER).status_code == 200
    assert _versions(client) == [(3, True), (2, False), (1, False)]


def test_a_failure_to_remove_bytes_after_the_commit_is_still_a_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_versions(client)

    def refuse(self: FsspecStorage, path: str) -> None:
        raise OSError("the disk went away")

    monkeypatch.setattr(FsspecStorage, "delete", refuse)
    deleted = client.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted_at"] is not None
    assert _versions(client) == [(2, True), (1, False)]
    assert "disk" not in deleted.text


# --- dropping an artifact --------------------------------------------------------------------


def test_dropping_removes_the_artifact_and_frees_the_name(client: TestClient) -> None:
    _two_versions(client)
    assert client.delete(_BASE, headers=AUTH_HEADER).status_code == 204
    assert client.get(_BASE, headers=AUTH_HEADER).status_code == 404
    assert client.get("/api/v1/environments/dev/artifacts", headers=AUTH_HEADER).json() == []
    assert client.delete(_BASE, headers=AUTH_HEADER).status_code == 404
    assert upload_artifact(client, "dev", "models.churn", {"m": b"new"})["version"] == 1
    events = _events(client, "ARTIFACT_DROPPED")
    assert [e["detail"]["versions"] for e in events] == [2]  # ty: ignore[not-subscriptable]


# --- who may delete --------------------------------------------------------------


@pytest.mark.parametrize("kind", ["USER", "SERVICE_ACCOUNT"])
def test_a_principal_with_the_privilege_deletes_and_drops(client: TestClient, kind: str) -> None:
    _two_versions(client)
    engineer = _user(client, "engineer", "DATA_ENGINEER", kind)
    deleted = client.delete(f"{_BASE}/versions/1", headers=engineer)
    assert (deleted.status_code, deleted.json()["deleted_by"]) == (200, "engineer")
    assert client.delete(_BASE, headers=engineer).status_code == 204


@pytest.mark.parametrize("role", ["ANALYST", "DATA_SCIENTIST"], ids=["read", "write"])
def test_a_caller_without_drop_table_is_refused(client: TestClient, role: str) -> None:
    _two_versions(client)
    caller = _user(client, "caller", role)
    for call in (
        lambda: client.delete(f"{_BASE}/versions/1", headers=caller),
        lambda: client.delete(_BASE, headers=caller),
        lambda: client.delete(_BASE.replace("churn", "ghost"), headers=caller),
    ):
        assert call().status_code == 403
    assert _versions(client) == [(2, True), (1, True)]
    assert client.get(_BASE, headers=AUTH_HEADER).status_code == 200


def _task_headers(client: TestClient) -> dict[str, dict[str, str]]:
    """Make the two kinds of task token of a principal who may drop, as real ones.

    The principal holds `ADMIN` on `dev`, so a refusal comes from the token, not a grant. The
    bound token is minted for a running attempt of `models.churn`, as the worker mints it; the
    other is minted over REST for an asset scope and is bound to no run.

    Parameters
    ----------
    client
        The test client.

    Returns
    -------
    dict
        ``bound`` and ``unbound`` to their `Authorization` headers.
    """
    _user(client, "etl", "ADMIN", "SERVICE_ACCOUNT")
    with Session(client.app.state.engine) as session:
        env = session.execute(select(Environment).where(Environment.name == "dev")).scalar_one()
        etl = session.execute(select(Principal).where(Principal.name == "etl")).scalar_one()
        repo = Repository(
            id=uuid.uuid4(), name="analytics", kind=RepositoryKind.HTTPS, url="https://h/a.git"
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
                        "table": "models.churn",
                        "kind": "python",
                        "path": "assets/models/churn.py",
                        "inputs": {},
                        "sql": "",
                        "output": "artifact",
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
            asset="models.churn",
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
        minted = TaskTokenService(session, AllowAllAuthorization()).mint_for_task(
            task.id, 1, ttl_seconds=300
        )
    unbound = client.post(
        "/api/v1/task-tokens",
        headers=AUTH_HEADER,
        json={
            "principal": "etl",
            "environment": "dev",
            "asset_scope": ["models.*"],
            "ttl_seconds": 60,
        },
    )
    assert unbound.status_code == 201, unbound.text
    return {
        "bound": {"Authorization": f"Bearer {minted.token}"},
        "unbound": {"Authorization": f"Bearer {unbound.json()['token']}"},
    }


def test_no_task_token_deletes_or_drops_bound_or_not(client: TestClient) -> None:
    _two_versions(client)
    tokens = _task_headers(client)
    for headers in tokens.values():
        for response in (
            client.delete(f"{_BASE}/versions/1", headers=headers),
            client.delete(_BASE, headers=headers),
        ):
            assert response.status_code == 403
    # The unbound token's principal may drop: only the service's one text can refuse it.
    unbound = client.delete(f"{_BASE}/versions/1", headers=tokens["unbound"])
    assert unbound.json()["detail"] == DELETE_FROM_RUN_MESSAGE
    assert client.delete(_BASE, headers=tokens["unbound"]).json()["detail"] == (
        DELETE_FROM_RUN_MESSAGE
    )
    # A bound token never holds DROP_TABLE: its own scope refuses first.
    bound = client.delete(f"{_BASE}/versions/1", headers=tokens["bound"])
    assert bound.json()["detail"] != DELETE_FROM_RUN_MESSAGE
    assert "DROP_TABLE" in bound.json()["detail"]
    assert _versions(client) == [(2, True), (1, True)]
    assert client.get(_BASE, headers=AUTH_HEADER).status_code == 200


# --- protected environments, break-glass, one service per request ----------------------------


def test_a_protected_environment_needs_a_reason(client: TestClient) -> None:
    _two_versions(client)
    _protect(client)
    refused = client.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER)
    assert refused.status_code == 403
    assert "is protected" in refused.json()["detail"]
    assert client.delete(_BASE, headers=AUTH_HEADER).status_code == 403
    allowed = client.delete(
        f"{_BASE}/versions/1",
        headers=AUTH_HEADER,
        params={"break_glass_reason": "remove a leaked training set"},
    )
    assert allowed.status_code == 200
    assert _versions(client) == [(2, True), (1, False)]


def test_each_break_glass_delete_is_its_own_request_and_its_own_event(
    client: TestClient,
) -> None:
    _two_versions(client)
    upload_artifact(client, "dev", "models.churn", {"m": b"three"})
    _protect(client)
    reason = {"break_glass_reason": "remove leaked training sets"}
    assert (
        client.delete(f"{_BASE}/versions/1", headers=AUTH_HEADER, params=reason).status_code == 200
    )
    assert (
        client.delete(f"{_BASE}/versions/2", headers=AUTH_HEADER, params=reason).status_code == 200
    )
    events = _events(client, "BREAK_GLASS_WRITE")
    assert [e["detail"]["operation"] for e in events] == [  # ty: ignore[not-subscriptable]
        "delete_artifact_version",
        "delete_artifact_version",
    ]
    assert client.delete(_BASE, headers=AUTH_HEADER, params=reason).status_code == 204
    events = _events(client, "BREAK_GLASS_WRITE")
    assert sorted(e["detail"]["operation"] for e in events) == [  # ty: ignore[not-subscriptable]
        "delete_artifact_version",
        "delete_artifact_version",
        "drop_artifact",
    ]


def test_every_request_builds_its_own_artifact_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lockdown guard keeps state per instance, so an instance must never be shared."""
    _two_versions(client)
    seen: list[ArtifactService] = []
    real = ArtifactService.get_artifact

    def spy(self: ArtifactService, *args: object) -> object:
        seen.append(self)  # held, so that no two instances can share an id
        return real(self, *args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(ArtifactService, "get_artifact", spy)
    for _ in range(3):
        assert client.get(_BASE, headers=AUTH_HEADER).status_code == 200
    assert len({id(service) for service in seen}) == 3


# --- the same answer whether it exists or not ------------------------------------------------


def _normalised(response: httpx.Response) -> tuple[int, str]:
    """Reduce an answer to its status and body with the names the caller sent removed.

    Parameters
    ----------
    response
        An `httpx.Response`.

    Returns
    -------
    tuple
        Status and normalised text.
    """
    text = response.text
    for name in ("churn", "ghost", "models", "nowhere", "dev", "nope"):
        text = re.sub(rf"\b{name}\b", "NAME", text)
    return response.status_code, text


_PLACES = (
    "/api/v1/environments/dev/namespaces/models/artifacts/churn",
    "/api/v1/environments/dev/namespaces/models/artifacts/ghost",
    "/api/v1/environments/dev/namespaces/nowhere/artifacts/churn",
    "/api/v1/environments/nope/namespaces/models/artifacts/churn",
)


@pytest.mark.parametrize("role", [None, "ANALYST", "DATA_SCIENTIST"], ids=["none", "read", "write"])
def test_a_caller_without_drop_gets_the_same_answer_for_everything(
    client: TestClient, role: str | None
) -> None:
    _two_versions(client)
    assert client.delete(f"{_BASE}/versions/2", headers=AUTH_HEADER).status_code == 200
    caller = _user(client, "caller", role)

    deletes = {
        _normalised(client.delete(f"{place}/versions/{version}", headers=caller))
        for place in _PLACES
        for version in ("1", "2", "9")  # live, deleted, never existed
    }
    drops = {_normalised(client.delete(place, headers=caller)) for place in _PLACES}
    for answers in (deletes, drops):
        assert len(answers) == 1, answers
        assert next(iter(answers))[0] == 403
    assert _versions(client) == [(2, False), (1, True)]


@pytest.mark.parametrize("role", [None, "ANALYST"], ids=["none", "read"])
def test_a_caller_without_write_gets_the_same_answer_to_an_abort(
    client: TestClient, role: str | None
) -> None:
    _two_versions(client)
    foreign = client.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).json()["upload_id"]
    caller = _user(client, "caller", role)
    answers = {
        _normalised(client.delete(f"{place}/uploads/{upload}", headers=caller))
        for place in _PLACES
        for upload in (foreign, str(uuid.uuid4()))
    }
    assert len(answers) == 1, answers
    assert next(iter(answers))[0] == 403
    assert client.get(_BASE, headers=AUTH_HEADER).status_code == 200


def test_a_read_only_caller_does_see_404_from_a_read_route_so_the_parity_check_is_sharp(
    client: TestClient,
) -> None:
    _two_versions(client)
    reader = _user(client, "reader", "ANALYST")
    assert client.get(_BASE.replace("churn", "ghost"), headers=reader).status_code == 404
    assert client.delete(_BASE.replace("churn", "ghost"), headers=reader).status_code == 403


# --- aborting an upload ----------------------------------------------------------------------


def _begin(client: TestClient, headers: dict[str, str] | None = None) -> str:
    """Begin an upload of one small file.

    Parameters
    ----------
    client
        The test client.
    headers
        The caller's headers; the admin's by default.

    Returns
    -------
    str
        The upload id.
    """
    begun = client.post(f"{_BASE}/uploads", headers=headers or AUTH_HEADER, json=_DOC)
    assert begun.status_code == 201, begun.text
    return begun.json()["upload_id"]


def test_an_abandoned_upload_is_withdrawn_and_frees_its_slot(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    uploads = [_begin(client) for _ in range(16)]
    full = client.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC)
    assert full.status_code == 409
    aborted = client.delete(f"{_BASE}/uploads/{uploads[0]}", headers=AUTH_HEADER)
    assert (aborted.status_code, aborted.content) == (204, b"")
    assert _begin(client)
    sent = client.put(
        f"{_BASE}/uploads/{uploads[0]}/blobs/{_SHA}",
        headers=AUTH_HEADER,
        content=_DOC_FILE,
    )
    assert (sent.status_code, sent.json()["detail"]) == (409, UPLOAD_GONE)
    again = client.delete(f"{_BASE}/uploads/{uploads[0]}", headers=AUTH_HEADER)
    assert (again.status_code, again.json()["detail"]) == (409, UPLOAD_GONE)


def test_only_the_owners_open_upload_can_be_withdrawn(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    mine = _begin(client)
    other = _user(client, "other", "DATA_SCIENTIST")
    theirs = _begin(client, other)
    for upload in (theirs, str(uuid.uuid4())):
        refused = client.delete(f"{_BASE}/uploads/{upload}", headers=AUTH_HEADER)
        assert (refused.status_code, refused.json()["detail"]) == (409, UPLOAD_GONE)
    wrong = client.delete(f"{_BASE}/uploads/{mine}", headers=other)
    assert (wrong.status_code, wrong.json()["detail"]) == (409, UPLOAD_GONE)
    with Session(client.app.state.engine) as session:
        assert len(session.scalars(select(ArtifactUpload.id)).all()) == 2


def test_a_committed_upload_cannot_be_withdrawn_and_stays_repeatable(client: TestClient) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    upload = _begin(client)
    sha = _SHA
    client.put(f"{_BASE}/uploads/{upload}/blobs/{sha}", headers=AUTH_HEADER, content=_DOC_FILE)
    first = client.post(f"{_BASE}/uploads/{upload}/commit", headers=AUTH_HEADER, json={})
    assert first.status_code == 201
    refused = client.delete(f"{_BASE}/uploads/{upload}", headers=AUTH_HEADER)
    assert (refused.status_code, refused.json()["detail"]) == (409, UPLOAD_GONE)
    again = client.post(f"{_BASE}/uploads/{upload}/commit", headers=AUTH_HEADER, json={})
    assert again.json() == first.json()


def test_a_protected_environment_does_not_stop_withdrawing_ones_own_upload(
    client: TestClient,
) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    reason = {"break_glass_reason": "repair the model by hand"}
    begun = client.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC, params=reason)
    _protect(client)
    assert client.post(f"{_BASE}/uploads", headers=AUTH_HEADER, json=_DOC).status_code == 403
    aborted = client.delete(f"{_BASE}/uploads/{begun.json()['upload_id']}", headers=AUTH_HEADER)
    assert aborted.status_code == 204


def test_withdrawing_an_upload_names_what_is_wrong_only_after_authorization(
    client: TestClient,
) -> None:
    create_environment(client)
    create_namespace(client, "dev", "models")
    create_table(client, "dev", "gold", "plain")
    plain = _BASE.replace("models", "gold").replace("churn", "plain")
    assert client.delete(f"{plain}/uploads/{uuid.uuid4()}", headers=AUTH_HEADER).status_code == 422
    ghost = _BASE.replace("churn", "ghost")
    assert client.delete(f"{ghost}/uploads/{uuid.uuid4()}", headers=AUTH_HEADER).status_code == 404
    writer = _user(client, "writer", "DATA_SCIENTIST")
    assert client.delete(f"{plain}/uploads/{uuid.uuid4()}", headers=writer).status_code == 422
    nobody = _user(client, "nobody", None)
    assert client.delete(f"{plain}/uploads/{uuid.uuid4()}", headers=nobody).status_code == 403
    assert client.delete(f"{_BASE}/uploads/not-a-uuid", headers=AUTH_HEADER).status_code == 422
