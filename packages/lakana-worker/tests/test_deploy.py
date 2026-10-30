"""The deploy pipeline in the worker, on SQLite, local storage and real git."""

from __future__ import annotations

import base64
import dataclasses
import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import tarfile
import time
import uuid
from collections.abc import Callable
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, NamedTuple, cast

import pytest
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session
from worker_git_helpers import GitHttpServer, bare_copy, commit, git, make_repo

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.connections.service import ConnectionService
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.log_events import LogEvent
from lakana_core.models import (
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    EnvironmentDeployment,
    JobVersion,
    PackageIndex,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Run,
    RunTrigger,
    Secret,
    SecretUseKind,
)
from lakana_core.models.table import Table, TableType
from lakana_core.projects.deploys import CONFLICT_MESSAGE, DeployService
from lakana_core.projects.issues import ProjectIssue, ProjectValidationError
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.manifest import manifest_assets
from lakana_core.projects.package_indexes import PackageIndexService
from lakana_core.projects.repositories import RepositoryService
from lakana_core.projects.run_policy import RunCaps
from lakana_core.runs.store import DEPLOY_LOST_MESSAGE
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import SecretService
from lakana_core.secrets.uses import SecretUse
from lakana_core.storage.fsspec_storage import FsspecStorage
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import deploy as deploy_module
from lakana_worker.attempt_sweep import lock_exclusive_if_same
from lakana_worker.deploy import (
    INTERNAL_ERROR_MESSAGE,
    DeploySettings,
    code_path,
    resolve_credential,
    run_deploy,
)
from lakana_worker.git import AUTH_FAILED_MESSAGE, FetchedCommit, GitError
from lakana_worker.python_support import PythonSupport


class World(NamedTuple):
    """What the `world` fixture sets up.

    Parameters
    ----------
    sha
        The first commit.
    repo_path
        The local repository.
    settings
        Deploy settings trusting its parent as a repo root.
    """

    sha: str
    repo_path: Path
    settings: DeploySettings


_KEY = EnvKeyProvider(base64.b64encode(b"0" * 32).decode())
_FILES = {
    "lakana.yaml": "repo: analytics\ndefaults:\n  owner: data-eng\n",
    "assets/silver/clean.sql": (
        "/* lakana\ndescription: Clean events.\n*/\nSELECT id FROM bronze.events\n"
    ),
}


@pytest.fixture
def principal(session: Session) -> PrincipalRef:
    """Persist an admin principal.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        The principal.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    return ref


@pytest.fixture
def world(session: Session, principal: PrincipalRef, tmp_path: Path) -> World:
    """`dev` with `bronze.events`, a local repository `analytics` and its first commit.

    Parameters
    ----------
    session
        Database session.
    principal
        The admin.
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    World
        The first commit, the repository and the settings.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path / 'storage'}", options={}),
    )
    bronze = NamespacePath.from_parts(["bronze"])  # created with the environment
    catalog.create_table(
        principal,
        TableRef("dev", bronze, "events"),
        schema=Schema(NestedField(1, "id", LongType(), required=True)),
    )
    repo_path = tmp_path / "repos" / "analytics"
    sha = make_repo(repo_path, _FILES)
    roots = (str(tmp_path / "repos"),)
    RepositoryService(session, AllowAllAuthorization(), repo_roots=roots).create(
        principal, name="analytics", kind=RepositoryKind.LOCAL, url=str(repo_path)
    )
    return World(sha, repo_path, DeploySettings(repo_roots=roots))


def _submit_and_run(
    session: Session,
    principal: PrincipalRef,
    world: World,
    kind: DeployKind = DeployKind.DEPLOY,
    *,
    repository: str = "analytics",
    settings: DeploySettings | None = None,
    to_previous: bool = False,
) -> DeployRun:
    """Queue a run and execute it with allow-all authorization.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    world
        The fixture's values.
    kind
        What to run.
    repository
        Repository name.
    settings
        Deploy settings; defaults to the fixture's.
    to_previous
        Roll back to the previous version.

    Returns
    -------
    DeployRun
        The finished run.
    """
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository=repository, kind=kind, to_previous=to_previous
    )
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, settings or world.settings)
    run = session.get(DeployRun, view.id)
    assert run is not None
    return run


def test_an_unreachable_sha_fails_the_deploy_naming_the_repository(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """End to end: the message names the registered repository."""
    dangling = commit(world.repo_path, {"assets/silver/x.sql": "SELECT 1 AS id\n"})
    git(world.repo_path, "reset", "--quiet", "--hard", world.sha)
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY, ref=dangling
    )
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings)
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        f"commit {dangling} is not reachable from any branch or tag of analytics"
    )


def test_deploy_records_a_job_version_and_uploads_the_code(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path
) -> None:
    """End to end: fetch, validate, upload `git archive`, record, move the pointer."""
    run = _submit_and_run(session, principal, world)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    assert run.resolved_sha == world.sha
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert version.manifest_json["assets"][0]["table"] == "silver.clean"
    assert version.manifest_json["assets"][0]["metadata"]["owner"] == "data-eng"
    path = code_path("analytics", world.sha, version.code_sha256)
    assert path == f"_artifacts/code/analytics/{world.sha}-{version.code_sha256}.tar.gz"
    assert version.code_uri == f"file://{tmp_path / 'storage'}/{path}"
    stored = StorageResolver(session).for_environment("dev").storage.read(path)
    assert hashlib.sha256(stored).hexdigest() == version.code_sha256
    assert version.code_bytes == len(stored)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(stored))) as archive:
        names = archive.getnames()
    assert "assets/silver/clean.sql" in names
    assert not any(name == ".git" or name.startswith(".git/") for name in names)
    assert (
        session.execute(select(EnvironmentDeployment)).scalar_one().current_job_version_id
        == version.id
    )
    assert run.diff_json is not None and run.diff_json["added"] == ["silver.clean"]


def test_dry_run_writes_no_version_and_no_code(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path
) -> None:
    """A dry run reports the diff and touches nothing else."""
    run = _submit_and_run(session, principal, world, DeployKind.DRY_RUN)
    assert run.status is DeployStatus.SUCCEEDED
    assert run.diff_json is not None and run.diff_json["added"] == ["silver.clean"]
    assert session.query(JobVersion).count() == 0
    assert not (tmp_path / "storage" / "_artifacts").exists()


def test_redeploy_after_a_stray_upload_reuses_and_verifies(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """A stray tarball is never named; a redeploy reuses the row, empty diff."""
    storage = StorageResolver(session).for_environment("dev").storage
    junk = b"junk from a failed deploy"
    stray = code_path("analytics", world.sha, hashlib.sha256(junk).hexdigest())
    storage.write(stray, junk)
    first = _submit_and_run(session, principal, world)
    second = _submit_and_run(session, principal, world)
    assert first.job_version_id == second.job_version_id
    assert second.diff_json == {key: [] for key in second.diff_json or {}}
    version = session.get(JobVersion, first.job_version_id)
    assert version is not None
    assert not version.code_uri.endswith(stray)
    stored = storage.read(code_path("analytics", world.sha, version.code_sha256))
    assert hashlib.sha256(stored).hexdigest() == version.code_sha256
    assert storage.read(stray) == junk


def test_validation_failures_are_listed_with_file_and_line(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """Project-level and environment-level issues are reported together."""
    commit(
        world.repo_path,
        {
            "lakana.yaml": "repo: analytics\nrequirements:\n  fields: [tags]\n",
            "assets/silver/missing.sql": "SELECT id FROM bronze.ghost\n",
        },
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert "assets/silver/clean.sql:1: missing required field 'tags'" in run.error_message
    assert "assets/silver/missing.sql:1: table 'bronze.ghost' not found" in run.error_message


def test_parse_errors_are_reported(session: Session, principal: PrincipalRef, world: World) -> None:
    """A non-query asset fails the deploy before environment checks run."""
    commit(world.repo_path, {"assets/silver/bad.sql": "DROP TABLE x\n"})
    run = _submit_and_run(session, principal, world)
    assert run.error_message is not None
    assert run.error_message.startswith("assets/silver/bad.sql:1:")


def test_grant_revoked_between_queue_and_claim_fails_the_run(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """DEPLOY is checked again at claim time."""
    ci = PrincipalRef(id=uuid.uuid4(), name="ci", type=PrincipalType.SERVICE_ACCOUNT)
    session.add(Principal(id=ci.id, type=ci.type, name="ci", is_active=True))
    session.commit()
    grant = GrantService(session, AllowAllAuthorization()).grant(
        principal,
        principal="ci",
        action=Action.DEPLOY,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    view = DeployService(session, GrantAuthorization(session)).submit(
        ci, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    GrantService(session, AllowAllAuthorization()).revoke(principal, grant.id)
    run_deploy(session, view.id, GrantAuthorization(session), _KEY, world.settings)
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None and run.error_message.startswith("permission denied")
    assert session.query(JobVersion).count() == 0


def test_inactive_principal_fails_the_run(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """A deactivated submitter's queued run is refused before any fetch."""
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    row = session.get(Principal, principal.id)
    assert row is not None
    row.is_active = False
    session.commit()
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings)
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert (
        run.error_message == "the principal that queued this deploy no longer exists or is inactive"
    )


def test_rollback_moves_the_pointer_without_fetching(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path
) -> None:
    """A rollback works even when the repository is gone from disk."""
    first = _submit_and_run(session, principal, world)
    commit(world.repo_path, {"assets/silver/more.sql": "SELECT id FROM bronze.events\n"})
    _submit_and_run(session, principal, world)
    shutil.rmtree(world.repo_path)
    run = _submit_and_run(session, principal, world, DeployKind.ROLLBACK, to_previous=True)
    assert run.status is DeployStatus.SUCCEEDED
    assert (
        session.execute(select(EnvironmentDeployment)).scalar_one().current_job_version_id
        == first.job_version_id
    )


def test_unexpected_errors_do_not_leak(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bug is logged, not stored: the run gets a generic message and the loop survives."""

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("s3://bucket/secret-path")

    monkeypatch.setattr(deploy_module, "discover", explode)
    run = _submit_and_run(session, principal, world)
    assert run.error_message == INTERNAL_ERROR_MESSAGE
    assert "internal_error" in {getattr(r, "event", None) for r in caplog.records}


def test_job_time_limit(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path
) -> None:
    """The wall-clock limit for the whole job applies."""
    settings = DeploySettings(
        repo_roots=(str(tmp_path / "repos"),), limits=DeployLimits(job_seconds=0)
    )
    run = _submit_and_run(session, principal, world, settings=settings)
    assert run.error_message == "the deploy exceeded its time limit"


def _https_world(
    session: Session,
    principal: PrincipalRef,
    tmp_path: Path,
    server: GitHttpServer,
    secret: dict[str, str] | None,
) -> DeploySettings:
    """Register an https repository served by `server`, optionally with a secret.

    Parameters
    ----------
    session
        Database session.
    principal
        The admin.
    tmp_path
        Pytest's per-test directory.
    server
        The running git server.
    secret
        Values of the `git-token` secret in `dev`, or `None` for no secret.

    Returns
    -------
    DeploySettings
        Settings trusting the server's certificate.
    """
    repository = RepositoryService(session, AllowAllAuthorization()).create(
        principal,
        name="remote",
        kind=RepositoryKind.HTTPS,
        url=server.url("analytics"),
        credential_secret="git-token",
    )
    if secret is not None:
        SecretService(session, AllowAllAuthorization(), _KEY).create(
            principal,
            environment="dev",
            name="git-token",
            values=secret,
            use=SecretUse.repository(repository),
        )
    return DeploySettings(ca_bundle=server.ca_file)


@pytest.fixture
def served(tmp_path: Path) -> Path:
    """Serve a bare copy of a valid project under `tmp_path/served`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    Path
        The served root.
    """
    make_repo(tmp_path / "src", _FILES)
    root = tmp_path / "served"
    root.mkdir()
    bare_copy(tmp_path / "src", root, "analytics")
    return root


def test_https_deploy_resolves_the_token_from_the_environment(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path, served: Path
) -> None:
    """The credential secret is decrypted from the target environment's scope."""
    with GitHttpServer(served, tmp_path) as server:
        settings = _https_world(session, principal, tmp_path, server, {"token": server.token})
        run = _submit_and_run(session, principal, world, repository="remote", settings=settings)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)


def test_https_wrong_token_fails_cleanly_and_redacted(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path, served: Path
) -> None:
    """A rejected token is one message, and the token never reaches the run."""
    with GitHttpServer(served, tmp_path) as server:
        settings = _https_world(session, principal, tmp_path, server, {"token": "wrong-token-xyz"})
        run = _submit_and_run(session, principal, world, repository="remote", settings=settings)
    assert run.error_message == AUTH_FAILED_MESSAGE
    assert "wrong-token-xyz" not in (run.error_message or "")


@pytest.mark.parametrize(
    ("secret", "message"),
    [
        (
            None,
            "secret 'git-token' does not exist in environment 'dev'. Create it with `lakana "
            "secret create git-token --env dev --repository remote --values-stdin`",
        ),
        ({"password": "x"}, "secret 'git-token' has no 'token' value"),
    ],
)
def test_credential_problems_fail_with_the_secret_name_only(
    session: Session,
    principal: PrincipalRef,
    world: World,
    tmp_path: Path,
    served: Path,
    secret: dict[str, str] | None,
    message: str,
) -> None:
    """A missing secret or key fails the run naming the secret, never a value."""
    with GitHttpServer(served, tmp_path) as server:
        settings = _https_world(session, principal, tmp_path, server, secret)
        run = _submit_and_run(session, principal, world, repository="remote", settings=settings)
    assert run.error_message == message


@pytest.mark.parametrize(
    ("use", "current"),
    [
        ((None, None), "is not bound to a use yet"),
        ((SecretUseKind.ENVIRONMENT, None), "is bound to its environment"),
        ((SecretUseKind.REPOSITORY, uuid.UUID(int=7)), "is bound to a repository that was removed"),
        (
            (SecretUseKind.PACKAGE_INDEX, uuid.UUID(int=8)),
            "is bound to a package index that was removed",
        ),
    ],
)
def test_a_repository_secret_bound_to_another_use_stops_the_deploy(
    session: Session,
    principal: PrincipalRef,
    world: World,
    tmp_path: Path,
    served: Path,
    use: tuple[SecretUseKind | None, uuid.UUID | None],
    current: str,
) -> None:
    """`resolve_credential` refuses, before decrypting, a secret that serves something else."""
    with GitHttpServer(served, tmp_path) as server:
        settings = _https_world(session, principal, tmp_path, server, {"token": server.token})
        secret = session.execute(select(Secret).where(Secret.name == "git-token")).scalar_one()
        secret.use_kind, secret.use_target_id = use
        session.commit()
        run = _submit_and_run(session, principal, world, repository="remote", settings=settings)

    assert run.error_message is not None
    assert current in run.error_message
    assert run.error_message.endswith("lakana secret bind git-token --env dev --repository remote")
    assert server.token not in run.error_message


def test_missing_key_provider_fails_the_run(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path, served: Path
) -> None:
    """A credential needs a master key; without one the run fails, the worker survives."""
    with GitHttpServer(served, tmp_path) as server:
        settings = _https_world(session, principal, tmp_path, server, {"token": server.token})
        view = DeployService(session, AllowAllAuthorization()).submit(
            principal, "dev", repository="remote", kind=DeployKind.DEPLOY
        )
        run_deploy(session, view.id, AllowAllAuthorization(), None, settings)
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert (
        run.error_message
        == "repository 'remote' has a credential but no secrets master key is configured"
    )


def test_redeploy_never_overwrites_the_stored_tarball(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """A recorded project version keeps its tarball: a redeploy skips the upload."""
    storage = StorageResolver(session).for_environment("dev").storage
    first = _submit_and_run(session, principal, world)
    recorded = session.get(JobVersion, first.job_version_id)
    assert recorded is not None
    path = code_path("analytics", world.sha, recorded.code_sha256)
    original = storage.read(path)
    storage.write(path, b"sentinel")
    second = _submit_and_run(session, principal, world)
    assert second.status is DeployStatus.SUCCEEDED
    assert storage.read(path) == b"sentinel"
    version = session.get(JobVersion, second.job_version_id)
    assert version is not None
    assert version.code_sha256 == hashlib.sha256(original).hexdigest()


def test_a_late_same_sha_upload_never_replaces_a_recorded_tarball(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two workers deploy one SHA; the loser uploads different bytes after the winner records.

    The winner's project version must still name an object whose digest is its `code_sha256`, or
    later
    verification would fail forever on an immutable row. Different bytes for one SHA happen
    with a different git (a rolling upgrade); here gzip's mtime stands in for that.
    """
    real_gzip = gzip.compress
    stamps = iter(range(1, 100))
    monkeypatch.setattr(
        deploy_module.gzip,
        "compress",
        lambda data, mtime=0: real_gzip(data, mtime=next(stamps)),
    )
    storage = StorageResolver(session).for_environment("dev").storage
    real_write = type(storage).write
    raced: list[DeployRun] = []
    racing = False

    def write_after_a_racing_winner(self: object, path: str, data: bytes) -> None:
        nonlocal racing
        if not racing:
            # The loser has checked for an existing project version and is about to upload; the
            # winner runs start to finish in that gap.
            racing = True
            raced.append(_submit_and_run(session, principal, world))
        real_write(self, path, data)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(type(storage), "write", write_after_a_racing_winner)
    loser = _submit_and_run(session, principal, world)
    [winner] = raced
    assert winner.status is DeployStatus.SUCCEEDED
    assert loser.status is DeployStatus.FAILED
    version = session.get(JobVersion, winner.job_version_id)
    assert version is not None
    prefix = f"file://{tmp_path / 'storage'}/"
    assert version.code_uri.startswith(prefix)
    stored = storage.read(version.code_uri.removeprefix(prefix))
    assert hashlib.sha256(stored).hexdigest() == version.code_sha256
    # The loser's differently-digested tarball can never be referenced, so it is removed.
    assert [*storage.list("_artifacts/code/analytics/")] == [version.code_uri.removeprefix(prefix)]


def test_a_tarball_is_kept_when_no_row_proves_it_unreferenced(
    session: Session, principal: PrincipalRef, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pointer conflict leaves no project version; the digest-named object may be a winner's too.

    Two workers that produce identical bytes share one path, so deleting it on a conflict could
    remove the object a concurrent winner is about to record. Only a project version naming a
    different object proves the upload dead.
    """

    def conflict(self: DeployService, *args: object, **kwargs: object) -> None:
        self._session.rollback()
        raise ConflictError("pointer moved")

    monkeypatch.setattr(DeployService, "record_deploy", conflict)
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    storage = StorageResolver(session).for_environment("dev").storage
    assert len([*storage.list("_artifacts/code/analytics/")]) == 1


def _race_a_winner_during_the_loser_upload(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    during: Callable[[], None],
) -> None:
    """Run `during` once, inside the first tarball upload, and give each upload distinct bytes.

    Parameters
    ----------
    session
        Database session.
    monkeypatch
        Installs the hooks.
    during
        What the racing winner does.
    """
    real_gzip = gzip.compress
    stamps = iter(range(1, 100))
    monkeypatch.setattr(
        deploy_module.gzip, "compress", lambda data, mtime=0: real_gzip(data, mtime=next(stamps))
    )
    storage = StorageResolver(session).for_environment("dev").storage
    real_write = type(storage).write
    racing = False

    def write_after_a_racing_winner(self: object, path: str, data: bytes) -> None:
        nonlocal racing
        if not racing:
            racing = True
            during()
        real_write(self, path, data)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(type(storage), "write", write_after_a_racing_winner)


def _failing_delete(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every storage delete raise.

    Parameters
    ----------
    session
        Database session.
    monkeypatch
        Installs the hook.
    """
    storage = StorageResolver(session).for_environment("dev").storage

    def broken(self: object, path: str) -> None:
        raise OSError("storage unavailable")

    monkeypatch.setattr(type(storage), "delete", broken)


def test_a_failing_cleanup_is_logged_and_the_real_failure_is_reported(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deleting the losing tarball is best effort: its error is logged, never replaces the run's."""
    _failing_delete(session, monkeypatch)

    def winner() -> None:
        _submit_and_run(session, principal, world)

    _race_a_winner_during_the_loser_upload(session, monkeypatch, winner)
    with caplog.at_level("WARNING", logger="lakana_worker.deploy"):
        loser = _submit_and_run(session, principal, world)
    assert loser.status is DeployStatus.FAILED
    assert loser.error_message == CONFLICT_MESSAGE
    assert "Could not remove a superseded code tarball" in caplog.text
    assert "storage unavailable" in caplog.text


def test_a_failing_cleanup_does_not_fail_a_deploy_that_recorded(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The loser still records when the winner's row is reused; the failed delete changes nothing.

    The pointer is back on the version the loser observed (the winner deployed and was rolled
    back during the upload), so the loser's compare-and-swap succeeds on the winner's row.
    """
    first = _submit_and_run(session, principal, world)
    assert first.status is DeployStatus.SUCCEEDED
    commit(world.repo_path, {"assets/silver/extra.sql": "SELECT id FROM bronze.events\n"})
    _failing_delete(session, monkeypatch)

    def winner_deploys_then_is_rolled_back() -> None:
        won = _submit_and_run(session, principal, world)
        assert won.status is DeployStatus.SUCCEEDED
        service = DeployService(session, AllowAllAuthorization())
        view = service.submit(
            principal,
            "dev",
            repository="analytics",
            kind=DeployKind.ROLLBACK,
            to_sha=world.sha,
        )
        run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings)

    _race_a_winner_during_the_loser_upload(session, monkeypatch, winner_deploys_then_is_rolled_back)
    with caplog.at_level("WARNING", logger="lakana_worker.deploy"):
        loser = _submit_and_run(session, principal, world)
    assert (loser.status, loser.error_message) == (DeployStatus.SUCCEEDED, None)
    assert "Could not remove a superseded code tarball" in caplog.text


def test_job_time_limit_after_the_upload(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """The deadline is checked once more after the upload, before anything is recorded."""
    calls = iter(range(1_000_000))

    def clock() -> float:
        # 1st: the deadline; 2nd: after validation; 3rd: after the upload.
        return time.monotonic() + (1e6 if next(calls) >= 2 else 0)

    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings, clock)
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert run.error_message == "the deploy exceeded its time limit"
    assert session.query(JobVersion).count() == 0
    assert session.query(EnvironmentDeployment).count() == 0


def test_job_time_limit_after_validation(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """The deadline is checked again once validation is done."""
    calls = iter(range(1_000_000))

    def clock() -> float:
        return time.monotonic() if next(calls) == 0 else time.monotonic() + 1e6

    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings, clock)
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert run.error_message == "the deploy exceeded its time limit"
    assert session.query(JobVersion).count() == 0


def test_local_repository_outside_the_current_roots_is_refused(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path
) -> None:
    """The worker re-checks `repo_roots` at run time, not only at registration."""
    run = _submit_and_run(
        session,
        principal,
        world,
        settings=DeploySettings(repo_roots=(str(tmp_path / "elsewhere"),)),
    )
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None and "LAKANA_REPO_ROOTS" in run.error_message
    no_roots = _submit_and_run(session, principal, world, settings=DeploySettings())
    assert no_roots.error_message == (
        "Local repositories are disabled: LAKANA_REPO_ROOTS is empty. On a server, the "
        "operator sets LAKANA_REPO_ROOTS; a local instance started with "
        "'lakana server start --local' allows the home directory."
    )


def test_a_tampered_ref_is_revalidated_before_fetching(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """An option-shaped ref written straight into the row never reaches git."""
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    row = session.get(DeployRun, view.id)
    assert row is not None
    row.requested_ref = "--upload-pack=touch /tmp/pwned"
    session.commit()
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings)
    session.refresh(row)
    assert row.status is DeployStatus.FAILED
    assert row.error_message is not None and "ref" in row.error_message.lower()


def test_resolved_sha_survives_a_conflict_on_record(
    session: Session, principal: PrincipalRef, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim state and SHA are committed before `record_*`, which rolls back on conflict."""

    def conflict(self: DeployService, *args: object, **kwargs: object) -> None:
        self._session.rollback()
        raise ConflictError("pointer moved")

    monkeypatch.setattr(DeployService, "record_deploy", conflict)
    run = _submit_and_run(session, principal, world)
    assert (run.status, run.error_message) == (DeployStatus.FAILED, "pointer moved")
    assert run.resolved_sha == world.sha
    assert run.started_at is not None


def test_rollback_without_a_target_fails_keeping_claim_state(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """A rollback with no target fails with the service's message, `started_at` kept."""
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings)
    first = session.get(DeployRun, view.id)
    assert first is not None
    rollback = DeployRun(
        id=uuid.uuid4(),
        environment_id=first.environment_id,
        repository_id=first.repository_id,
        principal_id=principal.id,
        kind=DeployKind.ROLLBACK,
        status=DeployStatus.QUEUED,
    )
    session.add(rollback)
    session.commit()
    run_deploy(session, rollback.id, AllowAllAuthorization(), _KEY, world.settings)
    session.refresh(rollback)
    assert rollback.status is DeployStatus.FAILED
    assert rollback.error_message == "The rollback target no longer exists."
    assert rollback.started_at is not None


def test_repository_deleted_mid_run_ends_cleanly(
    session: Session, principal: PrincipalRef, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting the repository after the fetch cascades the run away; the worker returns."""
    real_fetch = deploy_module.fetch_commit

    def fetch_then_delete(**kwargs: object) -> FetchedCommit:
        fetched = real_fetch(**kwargs)  # ty: ignore[invalid-argument-type]
        session.execute(delete(Repository))
        session.commit()
        return fetched

    monkeypatch.setattr(deploy_module, "fetch_commit", fetch_then_delete)
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings)
    assert session.get(DeployRun, view.id) is None
    assert session.query(JobVersion).count() == 0


def test_a_run_whose_repository_row_vanished_is_failed_not_left_queued(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """Without the cascade (an orphaned run row) the run is failed, so it is never re-claimed.

    Left `QUEUED`, the poller would claim the same oldest run again with no sleep, forever.
    """
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    session.execute(text("PRAGMA foreign_keys=OFF"))
    session.execute(delete(Repository))
    session.commit()
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings)
    session.expire_all()
    run = session.get(DeployRun, view.id)
    assert run is not None and run.status is DeployStatus.FAILED
    assert run.error_message == "the repository or environment was deleted"
    assert run.finished_at is not None


def test_a_running_deploy_carries_the_workers_id(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim, not the outcome: the id is committed with `RUNNING` before the deploy starts.

    The patched deploy rolls back first, so only what the claim committed is read: an id
    assigned after the commit would be flushed, not committed, and gone here.
    """
    seen: list[tuple[DeployStatus, str | None]] = []

    def inspect(session: Session, service: DeployService, run: DeployRun, *args: object) -> None:
        session.rollback()
        session.expire(run)
        seen.append((run.status, run.worker_id))

    monkeypatch.setattr(deploy_module, "_deploy", inspect)
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings, worker_id="w1")
    assert seen == [(DeployStatus.RUNNING, "w1")]


def test_a_deploy_run_without_a_worker_id_has_none(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deploy_module, "_deploy", lambda *args: None)
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings)
    session.expire_all()
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert run.worker_id is None


def test_unknown_and_finished_runs_are_skipped(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """A missing run id, or a run that is not queued, is a no-op."""
    run_deploy(session, uuid.uuid4(), AllowAllAuthorization(), _KEY, world.settings)
    finished = _submit_and_run(session, principal, world)
    run_deploy(session, finished.id, AllowAllAuthorization(), _KEY, world.settings)
    session.refresh(finished)
    assert (finished.status, finished.error_message) == (DeployStatus.SUCCEEDED, None)


@pytest.mark.parametrize("error", [NotFoundError("gone"), ConflictError("finished")])
def test_a_failing_fail_never_escapes(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """If the run cannot be marked failed (gone or finished) the worker still returns."""

    def refuse(self: DeployService, run: DeployRun, message: str, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(DeployService, "fail", refuse)
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    row = session.get(Principal, principal.id)
    assert row is not None
    row.is_active = False
    session.commit()
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings)


def test_long_issue_lists_are_capped() -> None:
    """Fifty issues are listed, the rest counted."""
    issues = [ProjectIssue(f"assets/a{i}.sql", 1, "bad") for i in range(60)]
    message = deploy_module._describe(ProjectValidationError(issues))
    assert message.count("\n") == 50
    assert message.endswith("…and 10 more issues")
    assert "assets/a49.sql:1: bad" in message and "assets/a50.sql" not in message


def test_wrong_master_key_fails_with_the_same_opaque_message(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path, served: Path
) -> None:
    """A master key that cannot unwrap the data key fails the run with the one safe message."""
    other = EnvKeyProvider(base64.b64encode(b"1" * 32).decode())
    with GitHttpServer(served, tmp_path) as server:
        settings = _https_world(session, principal, tmp_path, server, {"token": server.token})
        view = DeployService(session, AllowAllAuthorization()).submit(
            principal, "dev", repository="remote", kind=DeployKind.DEPLOY
        )
        run_deploy(session, view.id, AllowAllAuthorization(), other, settings)
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert run.error_message == (
        "Secret 'git-token' in environment 'dev' could not be decrypted. If "
        "LAKANA_SECRETS__MASTER_KEY was changed, put back the key the secret was stored under. "
        "Otherwise restore it from a backup, or set it again with `lakana secret rotate "
        "git-token --env dev --values-stdin`."
    )


def test_tampered_ciphertext_names_only_the_secret(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path, served: Path
) -> None:
    """An integrity failure fails the run with the secret's name, never a value."""
    with GitHttpServer(served, tmp_path) as server:
        settings = _https_world(session, principal, tmp_path, server, {"token": server.token})
        secret = session.execute(select(Secret)).scalar_one()
        secret.ciphertext = bytes(len(secret.ciphertext))
        session.commit()
        view = DeployService(session, AllowAllAuthorization()).submit(
            principal, "dev", repository="remote", kind=DeployKind.DEPLOY
        )
        run_deploy(session, view.id, AllowAllAuthorization(), _KEY, settings)
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert run.error_message == (
        "Secret 'git-token' in environment 'dev' could not be decrypted. If "
        "LAKANA_SECRETS__MASTER_KEY was changed, put back the key the secret was stored under. "
        "Otherwise restore it from a backup, or set it again with `lakana secret rotate "
        "git-token --env dev --values-stdin`."
    )


def _make_format_1(secret: Secret, provider: EnvKeyProvider, values: dict[str, str]) -> None:
    """Overwrite `secret` with `values` as the previous release stored them (format 1).

    Parameters
    ----------
    secret
        The row.
    provider
        Wraps the data key, without associated data.
    values
        The plaintext bag.
    """
    import json

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    secret.ciphertext = AESGCM(data_key).encrypt(nonce, json.dumps(values).encode(), None)
    secret.nonce = nonce
    secret.wrapped_data_key = provider.wrap(data_key)
    secret.format_version = 1
    secret.key_id = "env"


def test_a_format_1_credential_is_refused_by_default_and_read_when_accepted(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """`resolve_credential` hands the process's setting to the decrypt: off refuses, on reads."""
    from lakana_core.errors import LakanaError
    from lakana_core.models import Environment

    SecretService(session, AllowAllAuthorization(), _KEY).create(
        principal, environment="dev", name="git-token", values={"token": "x"}
    )
    secret = session.execute(select(Secret)).scalar_one()
    _make_format_1(secret, _KEY, {"token": "old-token"})
    session.commit()
    environment = session.execute(select(Environment)).scalar_one()
    repository = Repository(
        id=uuid.uuid4(),
        name="r",
        kind=RepositoryKind.HTTPS,
        url="https://h/r.git",
        credential_secret="git-token",
    )
    secret.use_kind, secret.use_target_id = SecretUseKind.REPOSITORY, repository.id
    session.commit()

    with pytest.raises(LakanaError, match="stored in the old format"):
        resolve_credential(session, _KEY, environment, repository)
    accepting = EnvKeyProvider(base64.b64encode(b"0" * 32).decode(), accept_format_1=True)
    credential = resolve_credential(session, accepting, environment, repository)
    assert credential is not None and credential.token == "old-token"


def test_a_format_1_credential_fails_the_deploy_with_the_refusal(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path, served: Path
) -> None:
    """A deploy whose repository credential is stored in format 1 fails, saying what to do."""
    with GitHttpServer(served, tmp_path) as server:
        settings = _https_world(session, principal, tmp_path, server, {"token": server.token})
        secret = session.execute(select(Secret)).scalar_one()
        _make_format_1(secret, _KEY, {"token": server.token})
        session.commit()
        view = DeployService(session, AllowAllAuthorization()).submit(
            principal, "dev", repository="remote", kind=DeployKind.DEPLOY
        )
        run_deploy(session, view.id, AllowAllAuthorization(), _KEY, settings)
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert run.error_message is not None
    assert run.error_message.startswith(
        "Secret 'git-token' in environment 'dev' is stored in the old format (1)"
    )
    assert "LAKANA_SECRETS__ACCEPT_FORMAT_1=true" in run.error_message
    assert server.token not in run.error_message


def test_ssh_credential_is_the_private_key(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """An ssh repository's credential is the secret's `private_key`, with no username."""
    from lakana_core.models import Environment

    SecretService(session, AllowAllAuthorization(), _KEY).create(
        principal, environment="dev", name="deploy-key", values={"private_key": "KEY-MATERIAL"}
    )
    environment = session.execute(select(Environment)).scalar_one()
    repository = Repository(
        id=uuid.uuid4(),
        name="viassh",
        kind=RepositoryKind.SSH,
        url="ssh://git@h/r.git",
        credential_secret="deploy-key",
    )
    secret = session.execute(select(Secret)).scalar_one()
    secret.use_kind, secret.use_target_id = SecretUseKind.REPOSITORY, repository.id
    session.commit()
    credential = resolve_credential(session, _KEY, environment, repository)
    assert credential is not None
    assert credential.private_key == "KEY-MATERIAL" and credential.token is None
    repository.credential_secret = None
    assert resolve_credential(session, _KEY, environment, repository) is None


def test_run_settings_above_the_operator_caps_fail_the_deploy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """Deploy validation refuses retries/timeouts above the caps, with the file."""
    commit(
        world.repo_path,
        {"assets/silver/clean.sql": "/* lakana\nretries: 9\n*/\nSELECT id FROM bronze.events\n"},
    )
    settings = DeploySettings(repo_roots=world.settings.repo_roots, caps=RunCaps(max_retries=3))
    run = _submit_and_run(session, principal, world, settings=settings)
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "assets/silver/clean.sql:1: retries 9 is above the operator maximum of 3 "
        "(LAKANA_RUN__MAX_RETRIES)"
    )


def test_the_manifest_carries_the_resolved_run_policy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """A successful deploy stores retries, delay and timeout per asset."""
    settings = DeploySettings(
        repo_roots=world.settings.repo_roots, caps=RunCaps(max_retries=5, max_timeout_seconds=77)
    )
    run = _submit_and_run(session, principal, world, settings=settings)
    assert run.status is DeployStatus.SUCCEEDED, run.error_message
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    entry = version.manifest_json["assets"][0]
    assert (entry["retries"], entry["retry_delay_seconds"], entry["timeout_seconds"]) == (2, 30, 77)


def _discard_scenario(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path, *, shared: bool
) -> tuple[object, deploy_module._Upload]:
    """Set up a losing upload: dev's project version for the SHA names another object.

    Parameters
    ----------
    session
        Database session.
    principal
        The admin.
    world
        The fixture's values.
    tmp_path
        Pytest's per-test directory.
    shared
        Also record the uploaded object in a second environment sharing dev's storage root.

    Returns
    -------
    tuple
        The storage and the `_Upload` describing the object this run wrote.
    """
    winner = _submit_and_run(session, principal, world)
    version = session.get(JobVersion, winner.job_version_id)
    assert version is not None
    storage = StorageResolver(session).for_environment("dev").storage
    path = f"_artifacts/code/analytics/{world.sha}-{'e' * 64}.tar.gz"
    storage.write(path, b"loser bytes")
    upload = deploy_module._Upload(storage, path, f"file://{tmp_path / 'storage'}/{path}")
    if shared:
        catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
        catalog.create_environment(
            principal,
            name="prod",
            storage=StorageConfig(scheme="file", root=f"file://{tmp_path / 'storage'}", options={}),
        )
        prod = session.execute(select(Environment).where(Environment.name == "prod")).scalar_one()
        session.add(
            JobVersion(
                id=uuid.uuid4(),
                repository_id=version.repository_id,
                environment_id=prod.id,
                commit_sha=world.sha,
                manifest_json={},
                code_uri=upload.uri,
                code_sha256="e" * 64,
            )
        )
        session.commit()
    return storage, upload


def test_a_losing_tarball_is_deleted_when_nothing_else_names_it(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path
) -> None:
    """Baseline for the sharing case below."""
    storage, upload = _discard_scenario(session, principal, world, tmp_path, shared=False)
    version = session.execute(select(JobVersion)).scalar_one()
    deploy_module._discard_losing_upload(
        session, upload, version.repository_id, version.environment_id, world.sha
    )
    assert not storage.exists(upload.path)  # ty: ignore[unresolved-attribute]


def test_a_losing_tarball_another_environment_records_is_kept(
    session: Session, principal: PrincipalRef, world: World, tmp_path: Path
) -> None:
    """Environments may share a storage root; a project version anywhere naming it keeps it."""
    storage, upload = _discard_scenario(session, principal, world, tmp_path, shared=True)
    dev = session.execute(
        select(JobVersion).join(Environment).where(Environment.name == "dev")
    ).scalar_one()
    deploy_module._discard_losing_upload(
        session, upload, dev.repository_id, dev.environment_id, world.sha
    )
    assert storage.exists(upload.path)  # ty: ignore[unresolved-attribute]


_PY_ASSET = (
    "from lakana import asset\n"
    "\n"
    "\n"
    "@asset(inputs={'clean': 'silver.clean'}, columns={'id': 'The id.'})\n"
    "def scores(clean):\n"
    '    """Score every clean event."""\n'
    "    return clean\n"
)
_PYPROJECT = '[project]\nname = "analytics"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
_PYTHON = PythonSupport(server_url="http://server:8000", uv="/usr/local/bin/uv")


def _lock_text(lakana: str | None = None) -> str:
    """Build a `uv.lock` holding `lakana` and `polars`.

    Parameters
    ----------
    lakana
        The SDK version; this worker's own version when `None`.

    Returns
    -------
    str
        The lock file.
    """
    sdk = lakana or package_version("lakana-worker")
    return (
        'version = 1\nrequires-python = ">=3.11"\n\n'
        f'[[package]]\nname = "lakana"\nversion = "{sdk}"\n\n'
        '[[package]]\nname = "polars"\nversion = "1.44.2"\n'
    )


def _python_settings(world: World) -> DeploySettings:
    """Return the fixture's settings on a worker that can run Python assets.

    Parameters
    ----------
    world
        The fixture's values.

    Returns
    -------
    DeploySettings
        The settings with `python` set.
    """
    return dataclasses.replace(world.settings, python=_PYTHON)


def test_a_python_asset_deploys_between_two_sql_assets(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """Exit criterion, deploy half: SQL feeds Python feeds SQL."""
    lock = _lock_text()
    commit(
        world.repo_path,
        {
            "assets/gold/scores.py": _PY_ASSET,
            "assets/gold/top.sql": "SELECT id FROM gold.scores\n",
            "pyproject.toml": _PYPROJECT,
            "uv.lock": lock,
        },
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    manifest = version.manifest_json
    assert manifest["version"] == 3
    assert manifest["lock_sha256"] == hashlib.sha256(lock.encode()).hexdigest()
    assert manifest["edges"] == [["gold.scores", "gold.top"], ["silver.clean", "gold.scores"]]
    by_table = {entry["table"]: entry for entry in manifest["assets"]}
    scores = by_table["gold.scores"]
    assert scores["kind"] == "python"
    assert scores["inputs"] == {"clean": "silver.clean"}
    assert scores["references"] == ["silver.clean"]
    assert scores["lineage"] == {}
    assert scores["metadata"]["description"] == "Score every clean event."
    assert scores["metadata"]["owner"] == "data-eng"
    assert scores["metadata"]["columns"] == {"id": "The id."}
    assert (scores["write_mode"], scores["first_write_mode"]) == ("replace", "create")
    assert by_table["silver.clean"]["kind"] == "sql"


def test_changed_python_code_shows_in_the_diff(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    files = {
        "assets/gold/scores.py": _PY_ASSET,
        "pyproject.toml": _PYPROJECT,
        "uv.lock": _lock_text(),
    }
    commit(world.repo_path, files)
    settings = _python_settings(world)
    first = _submit_and_run(session, principal, world, settings=settings)
    assert first.status is DeployStatus.SUCCEEDED
    commit(
        world.repo_path,
        {"assets/gold/scores.py": _PY_ASSET.replace("return clean", "return clean.head(1)")},
    )
    run = _submit_and_run(session, principal, world, DeployKind.DRY_RUN, settings=settings)
    assert run.diff_json is not None
    assert run.diff_json["sql_changed"] == ["gold.scores"]


def test_a_python_project_without_dependency_files_fails(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(world.repo_path, {"assets/gold/scores.py": _PY_ASSET})
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "pyproject.toml: a project with Python assets needs a pyproject.toml at the "
        "repository root\n"
        "uv.lock: a project with Python assets needs a committed uv.lock; run `uv lock`"
    )


def test_an_incompatible_lock_fails_the_deploy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "assets/gold/scores.py": _PY_ASSET,
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text("999.0.0"),
        },
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert run.error_message is not None
    assert run.error_message.startswith("uv.lock: has lakana 999.0.0, but this instance runs ")


@pytest.mark.parametrize("name", ["pyproject.toml", "uv.lock"])
def test_a_non_utf8_dependency_file_fails_the_deploy_with_an_issue(
    session: Session, principal: PrincipalRef, world: World, name: str
) -> None:
    files = {
        "assets/gold/scores.py": _PY_ASSET,
        "pyproject.toml": _PYPROJECT,
        "uv.lock": _lock_text(),
    }
    commit(world.repo_path, {key: value for key, value in files.items() if key != name})
    (world.repo_path / name).write_bytes(b"\xff\xfe")
    git(world.repo_path, "add", "-A")
    git(world.repo_path, "commit", "-m", "binary")
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert run.error_message != INTERNAL_ERROR_MESSAGE
    assert run.error_message.startswith(f"{name}: ")
    assert "UTF-8" in run.error_message


@pytest.mark.parametrize(
    ("support", "reason"),
    [
        (PythonSupport(), "LAKANA_RUN__SERVER_URL is not set"),
        (PythonSupport(server_url="http://server:8000"), "uv is not on the worker's PATH"),
    ],
)
def test_an_instance_that_cannot_run_python_says_why(
    session: Session, principal: PrincipalRef, world: World, support: PythonSupport, reason: str
) -> None:
    commit(
        world.repo_path,
        {
            "assets/gold/scores.py": _PY_ASSET,
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text(),
        },
    )
    settings = dataclasses.replace(world.settings, python=support)
    run = _submit_and_run(session, principal, world, settings=settings)
    assert run.status is DeployStatus.FAILED
    assert run.error_message == f"pyproject.toml: this instance cannot run Python assets: {reason}"


def test_a_sql_project_deploys_on_an_instance_that_cannot_run_python(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.SUCCEEDED
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert "lock_sha256" not in version.manifest_json


def test_a_python_input_that_does_not_exist_is_not_found(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "assets/gold/scores.py": _PY_ASSET.replace("silver.clean", "bronze.missing"),
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text(),
        },
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert run.error_message == "assets/gold/scores.py:5: table 'bronze.missing' not found"


def test_a_symlink_outside_assets_fails_the_deploy_of_a_python_project(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """Found at deploy, not at the first run."""
    os.symlink("lakana.yaml", world.repo_path / "config-link")
    commit(world.repo_path, {})
    assert _submit_and_run(session, principal, world).status is DeployStatus.SUCCEEDED
    commit(
        world.repo_path,
        {
            "assets/gold/scores.py": _PY_ASSET,
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text(),
        },
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "config-link: symlinks are not allowed in a repository with Python assets"
    )


_ARTIFACT_PY = (
    "from lakana import asset, output_dir\n"
    "\n"
    "\n"
    '@asset(inputs={"rows": "bronze.events"}, output="artifact", keep_versions=2)\n'
    "def churn(rows):\n"
    '    """Train a model."""\n'
    '    return output_dir() / "m.json"\n'
)
_PREDICT_PY = (
    "from lakana import asset\n"
    "\n"
    "\n"
    '@asset(inputs={"model": "models.churn", "rows": "bronze.events"})\n'
    "def predictions(model, rows):\n"
    '    """Predict."""\n'
    "    return rows\n"
)


def test_a_deploy_records_output_and_input_kinds_in_manifest_version_3(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "assets/models/churn.py": _ARTIFACT_PY,
            "assets/gold/predictions.py": _PREDICT_PY,
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text(),
        },
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    manifest = version.manifest_json
    assert manifest["version"] == 3
    by_table = {entry["table"]: entry for entry in manifest["assets"]}
    churn = by_table["models.churn"]
    assert (churn["output"], churn["keep_versions"]) == ("artifact", 2)
    assert churn["input_kinds"] == {"rows": "table"}
    assert by_table["gold.predictions"]["input_kinds"] == {"model": "artifact", "rows": "table"}
    assert ["models.churn", "gold.predictions"] in manifest["edges"]
    assert "output" not in by_table["silver.clean"]
    assert "input_kinds" not in by_table["silver.clean"]


def test_a_deploy_marks_an_input_that_is_a_catalog_artifact(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """The kind of an input no asset of the project produces comes from the catalog."""
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    bronze = NamespacePath.from_parts(["bronze"])
    catalog.create_table(
        principal,
        TableRef("dev", bronze, "base"),
        schema=Schema(NestedField(1, "id", LongType(), required=True)),
    )
    table_id = resolve_table_id(session, "dev", "bronze", "base")
    assert table_id is not None
    row = session.get(Table, table_id)
    assert row is not None
    row.table_type = TableType.ARTIFACT
    session.commit()
    commit(
        world.repo_path,
        {
            "assets/gold/predictions.py": _PREDICT_PY.replace("models.churn", "bronze.base"),
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text(),
        },
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    by_table = {entry["table"]: entry for entry in version.manifest_json["assets"]}
    assert by_table["gold.predictions"]["input_kinds"] == {"model": "artifact", "rows": "table"}


def test_a_sql_asset_reading_an_artifact_fails_the_deploy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "assets/models/churn.py": _ARTIFACT_PY,
            "assets/gold/report.sql": "SELECT * FROM models.churn\n",
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text(),
        },
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert "`models.churn` is an artifact; only a Python asset can read it." in run.error_message


def test_sql_front_matter_with_output_fails_the_deploy_with_the_next_step(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {"assets/gold/report.sql": '/* lakana\noutput: artifact\n*/\nSELECT 1 AS "a"\n'},
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert (
        "`output` applies only to a Python asset, not to a SQL file; declare the asset in a "
        '`.py` file with `@asset(output="artifact")`'
    ) in run.error_message


def _deployer_under_grants(session: Session, admin: PrincipalRef) -> PrincipalRef:
    """Create `ci`: DEPLOY on `dev` and READ on `bronze.events`, nothing else.

    Parameters
    ----------
    session
        Database session.
    admin
        Grants the access.

    Returns
    -------
    PrincipalRef
        The deployer.
    """
    ci = PrincipalRef(id=uuid.uuid4(), name="ci", type=PrincipalType.SERVICE_ACCOUNT)
    session.add(Principal(id=ci.id, type=ci.type, name="ci", is_active=True))
    session.commit()
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(
        admin,
        principal="ci",
        action=Action.DEPLOY,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    grants.grant(
        admin,
        principal="ci",
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace="bronze",
        table="events",
    )
    return ci


def _deploy_as(session: Session, who: PrincipalRef, world: World, repository: str) -> DeployRun:
    """Queue a deploy as `who` and run it under real grants.

    Parameters
    ----------
    session
        Database session.
    who
        The submitter.
    world
        The fixture's values.
    repository
        Repository name.

    Returns
    -------
    DeployRun
        The finished run.
    """
    view = DeployService(session, GrantAuthorization(session)).submit(
        who, "dev", repository=repository, kind=DeployKind.DEPLOY
    )
    run_deploy(session, view.id, GrantAuthorization(session), _KEY, _python_settings(world))
    run = session.get(DeployRun, view.id)
    assert run is not None
    return run


def _produced_by_another_repository(session: Session, admin: PrincipalRef, world: World) -> None:
    """Deploy `bronze.churn` as an artifact from a second repository `producer`.

    Parameters
    ----------
    session
        Database session.
    admin
        The deployer (allow-all).
    world
        The fixture's values.
    """
    path = world.repo_path.parent / "producer"
    make_repo(
        path,
        {
            "assets/bronze/churn.py": _ARTIFACT_PY,
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text(),
        },
    )
    RepositoryService(
        session, AllowAllAuthorization(), repo_roots=world.settings.repo_roots
    ).create(admin, name="producer", kind=RepositoryKind.LOCAL, url=str(path))
    run = _submit_and_run(
        session, admin, world, repository="producer", settings=_python_settings(world)
    )
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)


def _row(session: Session, namespace: str, name: str, table_type: TableType) -> None:
    """Make `dev.namespace.name` a catalog row of `table_type`.

    Parameters
    ----------
    session
        Database session.
    namespace
        The namespace.
    name
        The name.
    table_type
        The type to give the row.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    admin = session.execute(select(Principal).where(Principal.name == "admin")).scalar_one()
    admin_ref = PrincipalRef(id=admin.id, name=admin.name, type=admin.type)
    path = NamespacePath.from_parts([namespace])
    catalog.create_table(
        admin_ref,
        TableRef("dev", path, name),
        schema=Schema(NestedField(1, "id", LongType(), required=True)),
    )
    table_id = resolve_table_id(session, "dev", namespace, name)
    assert table_id is not None
    row = session.get(Table, table_id)
    assert row is not None
    row.table_type = table_type
    session.commit()


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, "artifact"),
        (TableType.ARTIFACT, "artifact"),
        (TableType.ICEBERG, "artifact"),
    ],
    ids=["no-row", "unreadable-artifact-row", "unreadable-table-row"],
)
def test_an_input_another_repository_produces_is_an_artifact_for_a_deployer_who_cannot_read_it(
    session: Session,
    principal: PrincipalRef,
    world: World,
    row: TableType | None,
    expected: str,
) -> None:
    """The deployer's own grants decide what the catalog may tell."""
    _produced_by_another_repository(session, principal, world)
    if row is not None:
        _row(session, "bronze", "churn", row)
    ci = _deployer_under_grants(session, principal)
    commit(
        world.repo_path,
        {
            "assets/gold/predictions.py": _PREDICT_PY.replace("models.churn", "bronze.churn"),
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text(),
        },
    )
    run = _deploy_as(session, ci, world, "analytics")
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    by_table = {entry["table"]: entry for entry in version.manifest_json["assets"]}
    assert by_table["gold.predictions"]["input_kinds"] == {"model": expected, "rows": "table"}


def test_an_unreadable_artifact_row_no_repository_produces_is_not_found_like_a_missing_name(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """The same failure with and without the row."""
    ci = _deployer_under_grants(session, principal)
    commit(
        world.repo_path,
        {
            "assets/gold/predictions.py": _PREDICT_PY.replace("models.churn", "bronze.ghost"),
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text(),
        },
    )
    missing = _deploy_as(session, ci, world, "analytics")
    _row(session, "bronze", "ghost", TableType.ARTIFACT)
    unreadable = _deploy_as(session, ci, world, "analytics")
    assert missing.status is DeployStatus.FAILED
    assert missing.error_message is not None
    assert "table 'bronze.ghost' not found" in missing.error_message
    assert (unreadable.status, unreadable.error_message) == (
        missing.status,
        missing.error_message,
    )


_TEAM_PACKAGE = (
    '\n[[package]]\nname = "team-utils"\nversion = "1.4.0"\n'
    'source = { registry = "https://pkgs.example.org/simple" }\n'
)


def _deploy_with_team_package(
    session: Session, principal: PrincipalRef, world: World, settings: DeploySettings
) -> DeployRun:
    """Deploy a Python project whose lock takes one package from pkgs.example.org.

    Parameters
    ----------
    session
        Database session.
    principal
        The submitter.
    world
        The fixture's values.
    settings
        Deploy settings.

    Returns
    -------
    DeployRun
        The finished run.
    """
    commit(
        world.repo_path,
        {
            "assets/gold/scores.py": _PY_ASSET,
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text() + _TEAM_PACKAGE,
        },
    )
    return _submit_and_run(session, principal, world, settings=settings)


def test_a_lock_that_names_an_unregistered_index_fails_the_deploy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    run = _deploy_with_team_package(session, principal, world, _python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "uv.lock: takes team-utils from the package index https://pkgs.example.org/simple, "
        "which is not registered on this instance. Ask an administrator to register it "
        "(`lakana index add NAME --url https://pkgs.example.org/simple`), or take the package "
        "from a registered index (`lakana index list`), then run `uv lock`"
    )


def test_a_registered_index_or_the_operators_index_lets_the_deploy_through(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    operator = dataclasses.replace(
        _python_settings(world), index_url="https://op:hunter2@PKGS.example.org/simple/"
    )
    assert "hunter2" not in repr(operator)
    run = _deploy_with_team_package(session, principal, world, operator)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    PackageIndexService(session, AllowAllAuthorization()).create(
        principal, name="team", url="https://pkgs.example.org/simple"
    )
    commit(world.repo_path, {"assets/gold/scores.py": _PY_ASSET + "\n"})
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)


def _register_team(session: Session, principal: PrincipalRef) -> PackageIndex:
    """Register the index `team` with the secret name `team-index`.

    Parameters
    ----------
    session
        Database session.
    principal
        The registering principal.

    Returns
    -------
    PackageIndex
        The registered index, which a secret is bound to.
    """
    return PackageIndexService(session, AllowAllAuthorization()).create(
        principal,
        name="team",
        url="https://pkgs.example.org/simple",
        credential_secret="team-index",
    )


def test_a_deploy_stops_when_the_index_secret_is_missing_from_the_environment(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    _register_team(session, principal)
    run = _deploy_with_team_package(session, principal, world, _python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "uv.lock: the package index 'team' needs the secret 'team-index' in environment 'dev', "
        "which does not exist. Create it with `lakana secret create team-index --env dev "
        '--index team --values-stdin` (a JSON object with "username" and "password")'
    )


def test_the_documented_project_deploys_when_an_index_named_lakana_is_registered(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """The documented `pyproject.toml` calls its flat index `lakana`."""
    PackageIndexService(session, AllowAllAuthorization()).create(
        principal, name="lakana", url="https://pkgs.example.org/simple"
    )
    documented = (
        _PYPROJECT
        + '\n[[tool.uv.index]]\nname = "lakana"\nurl = "wheelhouse"\nformat = "flat"\n'
        + 'explicit = true\n\n[tool.uv.sources]\nlakana = { index = "lakana" }\n'
    )
    commit(
        world.repo_path,
        {"assets/gold/scores.py": _PY_ASSET, "pyproject.toml": documented, "uv.lock": _lock_text()},
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)


def test_a_deploy_stops_when_the_stored_url_of_an_index_with_a_credential_is_not_valid(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """A row written straight into the database: no secret is read, and the deploy says why."""
    session.add(
        PackageIndex(
            id=uuid.uuid4(),
            name="testpypi",
            url="https://pypi.org/simple",
            credential_secret="team-index",
        )
    )
    session.commit()
    commit(
        world.repo_path,
        {
            "assets/gold/scores.py": _PY_ASSET,
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text() + _TEAM_PACKAGE.replace("pkgs.example.org", "pypi.org"),
        },
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "uv.lock: the stored URL of the package index 'testpypi' is not valid any more, so no "
        "credential is sent to it. An administrator registers it again with "
        "`lakana index update testpypi --url URL`"
    )


def test_a_deploy_names_the_remedy_when_a_host_that_takes_no_credential_has_one(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """A TestPyPI row with a secret (a direct database write) is told to clear the secret."""
    session.add(
        PackageIndex(
            id=uuid.uuid4(),
            name="testpypi",
            url="https://test.pypi.org/simple",
            credential_secret="team-index",
        )
    )
    session.commit()
    commit(
        world.repo_path,
        {
            "assets/gold/scores.py": _PY_ASSET,
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text() + _TEAM_PACKAGE.replace("pkgs.example.org", "test.pypi.org"),
        },
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "uv.lock: the package index 'testpypi' has a credential, but its host takes none, so no "
        "credential is sent to it. An administrator removes it with "
        "`lakana index update testpypi --clear credential_secret`"
    )


def test_a_deploy_stops_when_the_index_secret_is_not_bound_to_the_index(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """An unbound secret, and one bound to another use, are refused at build with the fix."""
    _register_team(session, principal)
    SecretService(session, AllowAllAuthorization(), _KEY).create(
        principal,
        environment="dev",
        name="team-index",
        values={"username": "ci", "password": "the-password"},
    )
    run = _deploy_with_team_package(session, principal, world, _python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith(
        "uv.lock: Secret 'team-index' of environment 'dev' is not bound"
    )
    assert run.error_message.endswith("lakana secret bind team-index --env dev --index team")
    assert "the-password" not in run.error_message


def test_a_deploy_stops_when_the_index_secret_has_no_password(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    row = _register_team(session, principal)
    SecretService(session, AllowAllAuthorization(), _KEY).create(
        principal,
        environment="dev",
        name="team-index",
        values={"username": "ci"},
        use=SecretUse.package_index(row),
    )
    run = _deploy_with_team_package(session, principal, world, _python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith(
        "uv.lock: the secret 'team-index' of environment 'dev', which the package index 'team' "
        'uses, has no usable "password". Set it again with `lakana secret rotate team-index'
    )


def test_a_deploy_with_the_secret_records_the_index_in_the_manifest(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    row = _register_team(session, principal)
    SecretService(session, AllowAllAuthorization(), _KEY).create(
        principal,
        environment="dev",
        name="team-index",
        values={"username": "ci", "password": "the-password"},
        use=SecretUse.package_index(row),
    )
    run = _deploy_with_team_package(session, principal, world, _python_settings(world))
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert version.manifest_json["package_indexes"] == ["team"]
    assert "the-password" not in json.dumps(version.manifest_json)
    assert "the-password" not in json.dumps(run.diff_json)


def test_a_dry_run_checks_the_secret_too_and_a_lock_without_an_index_records_none(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    _register_team(session, principal)
    commit(
        world.repo_path,
        {
            "assets/gold/scores.py": _PY_ASSET,
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text() + _TEAM_PACKAGE,
        },
    )
    dry = _submit_and_run(
        session, principal, world, DeployKind.DRY_RUN, settings=_python_settings(world)
    )
    assert dry.status is DeployStatus.FAILED
    assert "which does not exist" in (dry.error_message or "")
    commit(world.repo_path, {"uv.lock": _lock_text()})
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert "package_indexes" not in version.manifest_json


def test_a_failed_cleanup_logs_the_tarball_path_escaped(caplog: pytest.LogCaptureFixture) -> None:
    """The path holds the repository's name, which may have a line break and an escape."""
    from lakana_worker import deploy as deploy_module

    class Broken:
        """A session whose queries fail."""

        def execute(self, *args: object) -> None:
            """Fail as a lost database does.

            Parameters
            ----------
            *args
                Ignored.

            Raises
            ------
            OSError
                Always.
            """
            raise OSError("down")

    upload = deploy_module._Upload(
        storage=None,  # ty: ignore[invalid-argument-type]
        path="_artifacts/code/x\n2026-10-08 ERROR forged\x1b[2J/abc.tar.gz",
        uri="file:///x",
    )
    with caplog.at_level("WARNING", logger="lakana_worker.deploy"):
        deploy_module._discard_losing_upload(
            Broken(),  # ty: ignore[invalid-argument-type]
            upload,
            uuid.uuid4(),
            uuid.uuid4(),
            "a" * 40,
        )
    [record] = caplog.records
    message = record.getMessage()
    assert "\n" not in message and "\x1b" not in message
    assert "x\\n2026-10-08 ERROR forged\\x1b[2J/abc.tar.gz" in message


def test_every_finishing_call_carries_the_workers_id(
    session: Session, principal: PrincipalRef, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The three records and each `_fail` hand the service the id the deploy was claimed under.

    A queued run nobody claimed (inactive principal) is failed with no id.
    """
    seen: list[tuple[str, str | None]] = []

    def recorder(name: str) -> Callable[..., None]:
        def record(self: DeployService, *args: object, **kwargs: object) -> None:
            seen.append((name, kwargs["worker_id"]))  # ty: ignore[invalid-argument-type]

        return record

    for name in ("record_deploy", "record_dry_run", "execute_rollback", "fail"):
        monkeypatch.setattr(DeployService, name, recorder(name))
    service = DeployService(session, AllowAllAuthorization())
    for kind in (DeployKind.DEPLOY, DeployKind.DRY_RUN):
        view = service.submit(principal, "dev", repository="analytics", kind=kind)
        run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings, worker_id="w9")
    rollback = DeployRun(
        id=uuid.uuid4(),
        environment_id=session.execute(select(Environment.id)).scalar_one(),
        repository_id=session.execute(select(Repository.id)).scalar_one(),
        principal_id=principal.id,
        kind=DeployKind.ROLLBACK,
        status=DeployStatus.QUEUED,
    )
    session.add(rollback)
    session.commit()
    run_deploy(session, rollback.id, AllowAllAuthorization(), _KEY, world.settings, worker_id="w9")
    monkeypatch.setattr(
        deploy_module, "_deploy", lambda *args: (_ for _ in ()).throw(GitError("boom"))
    )
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings, worker_id="w9")
    row = session.get(Principal, principal.id)
    assert row is not None
    row.is_active = False
    session.commit()
    view = service.submit(principal, "dev", repository="analytics", kind=DeployKind.DEPLOY)
    run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings, worker_id="w9")
    assert seen == [
        ("record_deploy", "w9"),
        ("record_dry_run", "w9"),
        ("execute_rollback", "w9"),
        ("fail", "w9"),
        ("fail", None),
    ]


@pytest.mark.parametrize("finish", ["deploy", "dry_run", "rollback", "fail"])
def test_a_deploy_that_wakes_after_it_was_ended_changes_nothing(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    finish: str,
) -> None:
    """The sweep failed the run while the worker was frozen; each way of finishing is refused.

    The run keeps the sweep's message, no project version appears and no pointer moves; the worker
    logs one warning. The archive that a late deploy of a new commit uploaded stays (nothing
    references it, and no sweeper removes it yet, as `projects.md` says).
    """
    first = _submit_and_run(session, principal, world)
    assert first.status is DeployStatus.SUCCEEDED
    versions = session.query(JobVersion).count()
    pointer = session.execute(select(EnvironmentDeployment.current_job_version_id)).scalar_one()
    storage = StorageResolver(session).for_environment("dev").storage
    stored = [*storage.list("_artifacts/code/analytics/")]
    commit(world.repo_path, {"assets/silver/late.sql": "SELECT 1 AS id\n"})  # a new commit
    service = DeployService(session, AllowAllAuthorization())
    kind = {
        "deploy": DeployKind.DEPLOY,
        "dry_run": DeployKind.DRY_RUN,
        "rollback": DeployKind.ROLLBACK,
        "fail": DeployKind.DEPLOY,
    }[finish]
    view = (
        service.submit(principal, "dev", repository="analytics", kind=kind, to_sha=world.sha)
        if kind is DeployKind.ROLLBACK
        else service.submit(principal, "dev", repository="analytics", kind=kind)
    )
    real_authorize = DeployService.authorize_deploy

    def authorize_then_get_ended(self: DeployService, *args: object, **kwargs: object) -> None:
        real_authorize(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]
        session.execute(
            update(DeployRun)
            .where(DeployRun.id == view.id)
            .values(status=DeployStatus.FAILED, error_message=DEPLOY_LOST_MESSAGE)
        )
        session.commit()

    monkeypatch.setattr(DeployService, "authorize_deploy", authorize_then_get_ended)
    if finish == "fail":

        def broken(*args: object) -> None:
            raise GitError("boom")

        monkeypatch.setattr(deploy_module, "_deploy", broken)
    with caplog.at_level("WARNING", logger="lakana_worker.deploy"):
        run_deploy(session, view.id, AllowAllAuthorization(), _KEY, world.settings, worker_id="w1")
    session.expire_all()
    run = session.get(DeployRun, view.id)
    assert run is not None
    assert (run.status, run.error_message) == (DeployStatus.FAILED, DEPLOY_LOST_MESSAGE)
    assert session.query(JobVersion).count() == versions
    assert (
        session.execute(select(EnvironmentDeployment.current_job_version_id)).scalar_one()
        == pointer
    )
    after = [*storage.list("_artifacts/code/analytics/")]
    assert set(stored) <= set(after)
    if finish == "deploy":
        assert len(after) == len(stored) + 1  # the late worker's archive stays
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "Deploy run could not be marked failed" in warnings[0].getMessage()


def test_a_deploy_holds_a_lock_on_its_checkout_and_still_removes_it(
    session: Session,
    principal: PrincipalRef,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkout is locked while the deploy runs and its own removal still works."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp))
    real_fetch = deploy_module.fetch_commit
    seen: list[bool] = []

    def fetch(**kwargs: object) -> FetchedCommit:
        (checkout,) = temp.glob("lakana-deploy-*")
        descriptor = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)
        try:
            seen.append(lock_exclusive_if_same(descriptor, checkout))
        finally:
            os.close(descriptor)
        return real_fetch(**cast("dict[str, Any]", kwargs))

    monkeypatch.setattr(deploy_module, "fetch_commit", fetch)
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.SUCCEEDED
    assert seen == [False]
    assert list(temp.iterdir()) == []


def _archive_file(version: JobVersion) -> Path:
    """Return the file a project version's code archive lives in.

    Parameters
    ----------
    version
        A project version stored on local storage.

    Returns
    -------
    Path
        The file.
    """
    return Path(version.code_uri.removeprefix("file://"))


def test_an_archive_deleted_between_the_upload_and_the_record_is_restored(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sweep's delete that lands after the deploy recorded its project version."""
    real = cast("Callable[..., JobVersion]", DeployService.record_deploy)

    def record_then_lose_the_archive(
        self: DeployService, *args: object, **kwargs: object
    ) -> JobVersion:
        version = real(self, *args, **kwargs)
        _archive_file(version).unlink()
        return version

    monkeypatch.setattr(DeployService, "record_deploy", record_then_lose_the_archive)
    with caplog.at_level(logging.WARNING):
        run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.SUCCEEDED
    version = session.execute(select(JobVersion)).scalar_one()
    stored = _archive_file(version).read_bytes()
    assert hashlib.sha256(stored).hexdigest() == version.code_sha256
    assert "was missing and has been restored" in caplog.text


def test_a_redeploy_of_a_recorded_sha_restores_a_missing_archive(
    session: Session, principal: PrincipalRef, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    _submit_and_run(session, principal, world)
    version = session.execute(select(JobVersion)).scalar_one()
    _archive_file(version).unlink()
    with caplog.at_level(logging.WARNING):
        run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.SUCCEEDED
    assert hashlib.sha256(_archive_file(version).read_bytes()).hexdigest() == version.code_sha256
    assert "was missing and has been restored" in caplog.text


def test_the_normal_paths_check_once_and_upload_once(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exists: list[str] = []
    writes: list[str] = []
    real_exists, real_write = FsspecStorage.exists, FsspecStorage.write

    def spy_exists(self: FsspecStorage, path: str) -> bool:
        exists.append(path)
        return real_exists(self, path)

    def spy_write(self: FsspecStorage, path: str, data: bytes) -> None:
        writes.append(path)
        real_write(self, path, data)

    monkeypatch.setattr(FsspecStorage, "exists", spy_exists)
    monkeypatch.setattr(FsspecStorage, "write", spy_write)
    with caplog.at_level(logging.WARNING):
        _submit_and_run(session, principal, world)
        version = session.execute(select(JobVersion)).scalar_one()
        archive = _archive_file(version).name
        assert len([p for p in exists if p.endswith(archive)]) == 1
        assert len([p for p in writes if p.endswith(archive)]) == 1
        exists.clear()
        writes.clear()
        _submit_and_run(session, principal, world)
    assert len([p for p in exists if p.endswith(archive)]) == 1
    assert [p for p in writes if p.endswith(archive)] == []
    assert "restored" not in caplog.text


def test_a_restore_refuses_to_write_an_archive_that_does_not_hash_to_the_recorded_digest(
    session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    environment = Environment(
        id=uuid.uuid4(), name="dev", storage_root=f"file://{tmp_path / 'storage'}"
    )
    session.add(environment)
    session.commit()
    path = code_path("analytics", "a" * 40, "0" * 64)
    archive = tmp_path / "archive.tar"
    archive.write_bytes(b"not what was recorded")
    with caplog.at_level(logging.WARNING):
        deploy_module._restore_if_missing(
            session,
            None,
            "dev",
            archive,
            path=path,
            expected_uri=f"{environment.storage_root}/{path}",
            storage_root=environment.storage_root,
            code_sha256="0" * 64,
        )
    assert "digest differs" in caplog.text
    assert not StorageResolver(session).for_environment("dev").storage.exists(path)


def test_a_restore_that_cannot_check_or_write_logs_and_the_deploy_still_succeeds(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _submit_and_run(session, principal, world)
    version = session.execute(select(JobVersion)).scalar_one()
    real_exists = FsspecStorage.exists

    def refuse(*args: object, **kwargs: object) -> bool:
        raise OSError("storage down")

    monkeypatch.setattr(FsspecStorage, "exists", refuse)
    with caplog.at_level(logging.WARNING):
        run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.SUCCEEDED
    assert "Could not check or restore" in caplog.text
    assert session.execute(select(JobVersion)).scalar_one().id == version.id
    monkeypatch.setattr(FsspecStorage, "exists", real_exists)
    _archive_file(version).unlink()
    caplog.clear()
    monkeypatch.setattr(FsspecStorage, "write", refuse)
    with caplog.at_level(logging.WARNING):
        run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.SUCCEEDED
    assert "Could not check or restore" in caplog.text
    assert not _archive_file(version).exists()


def test_a_restore_rolls_the_session_back_when_the_check_fails(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _submit_and_run(session, principal, world)
    rollbacks: list[int] = []
    real_rollback = session.rollback

    def spy() -> None:
        rollbacks.append(1)
        real_rollback()

    def broken(self: StorageResolver, environment: str) -> object:
        raise OSError("database error inside the resolver")

    monkeypatch.setattr(StorageResolver, "for_environment", broken)
    monkeypatch.setattr(session, "rollback", spy)
    deploy_module._restore_if_missing(
        session,
        None,
        "dev",
        Path("/nonexistent"),
        path="p",
        expected_uri="file:///r/p",
        storage_root="file:///r",
        code_sha256="0" * 64,
    )
    assert rollbacks == [1]


def test_a_recorded_archive_at_another_root_is_reported_not_checked(
    session: Session,
    principal: PrincipalRef,
    world: World,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _submit_and_run(session, principal, world)
    version = session.execute(select(JobVersion)).scalar_one()
    archive = _archive_file(version)
    session.execute(update(JobVersion).values(code_uri="file:///elsewhere/x.tar.gz"))
    session.commit()
    archive.unlink()
    with caplog.at_level(logging.WARNING):
        run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.SUCCEEDED
    assert "not at this environment's current storage root" in caplog.text
    assert not archive.exists()


_CHECK = (
    "/* lakana\ndescription: Ids are present.\non_failure: warn\n*/\n"
    "SELECT id FROM silver.clean WHERE id IS NULL\n"
)


def test_a_deploy_stores_its_checks_in_the_manifest(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """The project version's manifest is version 4 with the check and its SQL; the run lists it."""
    commit(world.repo_path, {"checks/silver/clean/id_present.sql": _CHECK})
    run = _submit_and_run(session, principal, world)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    manifest = version.manifest_json
    assert manifest["version"] == 4
    (entry,) = manifest["checks"]
    assert entry["name"] == "silver.clean/id_present"
    assert (entry["on_failure"], entry["description"]) == ("warn", "Ids are present.")
    assert entry["sql"] == "SELECT id FROM silver.clean WHERE id IS NULL"
    assert entry["references"] == ["silver.clean"]
    assert run.checks_json == {"silver.clean": [{"check": "id_present", "on_failure": "warn"}]}


_PLAIN_CHECK = "SELECT id FROM silver.clean WHERE id IS NULL\n"


def test_a_project_default_is_stored_on_the_checks_that_state_nothing(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """`warn` from lakana.yaml reaches the manifest and the listing; an own value wins."""
    commit(
        world.repo_path,
        {
            "lakana.yaml": "repo: analytics\nchecks:\n  on_failure: warn\n",
            "checks/silver/clean/id_present.sql": _PLAIN_CHECK,
            "checks/silver/clean/id_own.sql": "/* lakana\non_failure: block\n*/\n" + _PLAIN_CHECK,
        },
    )
    run = _submit_and_run(session, principal, world)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    stored = {entry["name"]: entry["on_failure"] for entry in version.manifest_json["checks"]}
    assert stored == {"silver.clean/id_own": "block", "silver.clean/id_present": "warn"}
    assert version.manifest_json["config"]["checks"] == {"on_failure": "warn"}
    assert run.checks_json == {
        "silver.clean": [
            {"check": "id_own", "on_failure": "block"},
            {"check": "id_present", "on_failure": "warn", "from": "lakana.yaml"},
        ]
    }


def test_an_environment_overrides_the_project_default_of_its_checks(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """`dev` sets `block` over a base of `warn`: the checks block and nothing says `lakana.yaml`."""
    commit(
        world.repo_path,
        {
            "lakana.yaml": (
                "repo: analytics\nchecks:\n  on_failure: warn\n"
                "environments:\n  dev:\n    checks:\n      on_failure: block\n"
            ),
            "checks/silver/clean/id_present.sql": _PLAIN_CHECK,
        },
    )
    run = _submit_and_run(session, principal, world)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    assert run.checks_json == {"silver.clean": [{"check": "id_present", "on_failure": "block"}]}


def test_a_project_without_a_default_lists_its_checks_as_before(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """No default: no `from` key anywhere in the stored listing or the manifest's checks."""
    commit(world.repo_path, {"checks/silver/clean/id_present.sql": _PLAIN_CHECK})
    run = _submit_and_run(session, principal, world)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert run.checks_json == {"silver.clean": [{"check": "id_present", "on_failure": "block"}]}
    assert all("from" not in entry for entry in version.manifest_json["checks"])


def test_a_dry_run_lists_its_checks_and_changes_nothing(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(world.repo_path, {"checks/silver/clean/id_present.sql": _CHECK})
    run = _submit_and_run(session, principal, world, DeployKind.DRY_RUN)
    assert run.status is DeployStatus.SUCCEEDED
    assert run.checks_json == {"silver.clean": [{"check": "id_present", "on_failure": "warn"}]}
    assert session.query(JobVersion).count() == 0


def test_a_dry_run_refuses_a_bad_check_and_changes_nothing(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """D5 comes back with file and line; no project version, no listing."""
    commit(
        world.repo_path,
        {"checks/silver/clean/wrong_table.sql": "SELECT id FROM bronze.events\n"},
    )
    run = _submit_and_run(session, principal, world, DeployKind.DRY_RUN)
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "checks/silver/clean/wrong_table.sql:1: the check does not read 'silver.clean', the "
        "table its path names. Select from that table, or move the file into the directory of "
        "the table it checks."
    )
    assert session.query(JobVersion).count() == 0
    assert run.checks_json is None


def test_a_check_of_a_table_the_project_does_not_use_fails_the_deploy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(world.repo_path, {"checks/gold/other/x.sql": "SELECT id FROM gold.other\n"})
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith(
        "checks/gold/other/x.sql:1: table 'gold.other' is neither built nor read by this project."
    )
    assert "This project builds: silver.clean. It reads: bronze.events." in run.error_message


def test_a_check_that_reads_a_missing_table_fails_the_deploy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "checks/silver/clean/x.sql": (
                "SELECT a.id FROM silver.clean a JOIN dim.ghost USING (id)\n"
            )
        },
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message == "checks/silver/clean/x.sql:1: table 'dim.ghost' not found"


_KEYED = "/* lakana\ndescription: Clean events.\nkey: id\n*/\nSELECT id FROM bronze.events\n"


def test_a_header_key_generates_checks_in_the_manifest_and_the_listing(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """The two key checks are stored with their SQL and source, beside the hand-written one."""
    commit(
        world.repo_path,
        {"assets/silver/clean.sql": _KEYED, "checks/silver/clean/id_present.sql": _CHECK},
    )
    run = _submit_and_run(session, principal, world)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    entries = {entry["check"]: entry for entry in version.manifest_json["checks"]}
    assert sorted(entries) == ["id_present", "key_not_null", "key_unique"]
    assert entries["key_unique"]["generated"] == {
        "family": "key",
        "columns": ["id"],
        "target": None,
        "target_column": None,
    }
    assert entries["key_unique"]["path"] == "assets/silver/clean.sql:1"
    assert entries["key_not_null"]["sql"] == 'SELECT * FROM "silver"."clean" WHERE "id" IS NULL'
    assert "generated" not in entries["id_present"]
    assert run.checks_json == {
        "silver.clean": [
            {"check": "id_present", "on_failure": "warn"},
            {"check": "key_not_null", "on_failure": "block", "generated": "key"},
            {"check": "key_unique", "on_failure": "block", "generated": "key"},
        ]
    }


def test_a_hand_written_check_with_a_name_the_header_generates_fails_the_deploy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {"assets/silver/clean.sql": _KEYED, "checks/silver/clean/key_unique.sql": _PLAIN_CHECK},
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "checks/silver/clean/key_unique.sql: A check named `key_unique` is generated for "
        "`silver.clean`; rename `checks/silver/clean/key_unique.sql`."
    )


def test_a_deploy_without_checks_keeps_manifest_version_3(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    run = _submit_and_run(session, principal, world)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert version.manifest_json["version"] == 3
    assert "checks" not in version.manifest_json
    assert run.checks_json is None


def test_the_project_lakana_init_writes_deploys_with_its_check(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """The template the CLI ships passes the deploy rules (parity of the two)."""
    from lakana.cli import project_init
    from lakana.cli._sample import SAMPLE

    files = {
        relative: text.format(repo="analytics", version="0.0.0")
        for relative, text in project_init.TEMPLATES.items()
    } | SAMPLE
    commit(world.repo_path, files)
    run = _submit_and_run(session, principal, world)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert [entry["name"] for entry in version.manifest_json["checks"]] == [
        "silver.station_temperatures/plausible_range"
    ]


@pytest.mark.parametrize("template", ["ingest-files", "ingest-sql", "ingest-rest"])
def test_each_ingestion_template_asset_passes_the_deploy_rules(template: str) -> None:
    """The asset `lakana init --template` writes parses with no issue, its header recorded."""
    from lakana.cli.project_init import INGEST_TEMPLATES
    from lakana_core.projects.asset_model import ParsedAsset
    from lakana_core.projects.discovery import DiscoveredAsset
    from lakana_core.projects.project_config import AssetDefaults
    from lakana_core.projects.python_assets import parse_python_asset

    (path,) = (p for p in INGEST_TEMPLATES[template] if p.endswith(".py"))
    table = "bronze." + path.rsplit("/", 1)[1].removesuffix(".py")
    asset = DiscoveredAsset(
        path=path, table=table, text=INGEST_TEMPLATES[template][path], kind="python"
    )

    parsed = parse_python_asset(asset, AssetDefaults())

    assert isinstance(parsed, ParsedAsset), parsed
    assert len(parsed.connections) == 1


def test_the_project_of_the_python_template_deploys_with_its_python_asset(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """`lakana init --template python` writes a project that deploys: SQL feeds Python."""
    from lakana.cli import project_init
    from lakana.cli._sample import SAMPLE

    version = package_version("lakana-worker")
    files = {
        relative: text.format(repo="analytics", version=version)
        for relative, text in project_init.TEMPLATES.items()
    } | SAMPLE
    files["pyproject.toml"] = project_init._PYPROJECT_PYTHON.format(
        repo="analytics", version=version
    )
    files.update(project_init.PYTHON_TEMPLATE)
    files["README.md"] = files["README.md"].format(repo="analytics")
    files["uv.lock"] = _lock_text()
    commit(world.repo_path, files)

    run = _submit_and_run(session, principal, world, settings=_python_settings(world))

    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    job_version = session.get(JobVersion, run.job_version_id)
    assert job_version is not None
    by_table = {entry["table"]: entry for entry in job_version.manifest_json["assets"]}
    anomalies = by_table["gold.temperature_anomalies"]
    assert anomalies["kind"] == "python"
    assert anomalies["inputs"] == {
        "readings": "bronze.readings",
        "stations": "silver.station_temperatures",
    }
    assert set(anomalies["metadata"]["columns"]) == {"station_id", "day", "temp_c", "anomaly_c"}


_JOBS_YAML = (
    'repo: analytics\njobs:\n  daily:\n    select: ["silver.*"]\n'
    '    schedule: { cron: "0 6 * * *", timezone: Europe/Berlin }\n'
)


def test_a_deploy_records_the_jobs_in_the_manifest_and_a_dry_run_lists_them(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(world.repo_path, {"lakana.yaml": _JOBS_YAML})
    dry = _submit_and_run(session, principal, world, DeployKind.DRY_RUN)
    assert (dry.status, dry.error_message) == (DeployStatus.SUCCEEDED, None)
    assert dry.diff_json is not None
    assert dry.diff_json["jobs_added"] == ["daily"]
    run = _submit_and_run(session, principal, world)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert version.manifest_json["version"] == 5
    assert version.manifest_json["jobs"]["daily"]["schedule"] == {
        "cron": "0 6 * * *",
        "timezone": "Europe/Berlin",
    }


_NOTIFY_YAML = (
    'repo: analytics\njobs:\n  daily:\n    select: ["silver.*"]\n'
    "    notify: { webhook: team-alerts, format: slack }\n"
)


def test_a_deploy_with_notify_is_manifest_version_6_and_warns_about_a_missing_secret(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(world.repo_path, {"lakana.yaml": _NOTIFY_YAML})
    dry = _submit_and_run(session, principal, world, DeployKind.DRY_RUN)
    assert (dry.status, dry.error_message) == (DeployStatus.SUCCEEDED, None)
    assert dry.diff_json is not None and dry.diff_json["jobs_added"] == ["daily"]
    assert dry.warnings_json is not None
    assert any("would not notify" in w and "`team-alerts`" in w for w in dry.warnings_json)
    run = _submit_and_run(session, principal, world)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert version.manifest_json["version"] == 6
    assert version.manifest_json["jobs"]["daily"]["notify"] == {
        "webhook": "team-alerts",
        "format": "slack",
    }
    assert run.warnings_json is not None
    assert any("does not exist in" in w and "`team-alerts`" in w for w in run.warnings_json)


def test_a_deploy_refuses_an_unknown_notify_format_with_its_line(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "lakana.yaml": (
                'repo: analytics\njobs:\n  daily:\n    select: ["silver.*"]\n'
                "    notify: { webhook: w, format: email }\n"
            )
        },
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert (run.error_message or "").startswith("lakana.yaml:5: jobs.daily.notify.format")
    assert session.query(JobVersion).count() == 0


def test_a_deploy_refuses_a_job_whose_selection_matches_no_asset(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {"lakana.yaml": 'repo: analytics\njobs:\n  daily:\n    select: ["platinum.*"]\n'},
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith("lakana.yaml:4: job 'daily'")
    assert "'platinum.*' matches no asset" in run.error_message
    assert session.query(JobVersion).count() == 0


def test_a_deploy_refuses_an_override_that_leaves_a_schedule_without_cron(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """A `timezone` alone on a job without a schedule names the file and `cron`, not a crash."""
    commit(
        world.repo_path,
        {
            "lakana.yaml": (
                'repo: analytics\njobs:\n  daily:\n    select: ["silver.*"]\n'
                "environments:\n  dev:\n    jobs:\n      daily:\n"
                "        schedule: { timezone: Europe/Berlin }\n"
            )
        },
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert "lakana.yaml:" in run.error_message
    assert "cron" in run.error_message
    assert INTERNAL_ERROR_MESSAGE not in run.error_message
    assert session.query(JobVersion).count() == 0


def test_a_deploy_checks_requires_python_against_the_python_the_project_names(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """`python: "3.11"` with a lock needing 3.12 names 3.11; "3.12" deploys and is recorded."""
    lock = _lock_text().replace('requires-python = ">=3.11"', 'requires-python = ">=3.12"')
    files = {
        "assets/gold/scores.py": _PY_ASSET,
        "pyproject.toml": _PYPROJECT,
        "uv.lock": lock,
        "lakana.yaml": 'python: "3.11"\n',
    }
    commit(world.repo_path, files)
    settings = _python_settings(world)

    refused = _submit_and_run(session, principal, world, settings=settings)

    assert refused.status is DeployStatus.FAILED
    assert refused.error_message is not None
    assert (
        "requires-python '>=3.12' does not admit Python 3.11, which python: in lakana.yaml names"
        in refused.error_message
    )
    commit(world.repo_path, {"lakana.yaml": 'python: "3.12"\n'})
    run = _submit_and_run(session, principal, world, settings=settings)
    assert run.status is DeployStatus.SUCCEEDED
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None and version.manifest_json["python"] == "3.12"


_CUSTOMERS_PY = (
    "from lakana import asset\n"
    "\n"
    "\n"
    "@asset(connections=['crm-db'])\n"
    "def customers():\n"
    '    """Customers from the CRM."""\n'
    "    return None\n"
)
_UNKNOWN_CRM_DB = (
    "assets/bronze/customers.py:4: Asset `bronze.customers` declares connection `crm-db`, "
    "which environment `dev` does not have. Create it: `lakana connection create crm-db "
    "--env dev --kind <kind> …` (see `lakana connection create --help`)."
)


def _commit_customers(world: World) -> None:
    """Commit `bronze.customers`, a Python asset that declares `crm-db`.

    Parameters
    ----------
    world
        The fixture's values.
    """
    commit(
        world.repo_path,
        {
            "assets/bronze/customers.py": _CUSTOMERS_PY,
            "pyproject.toml": _PYPROJECT,
            "uv.lock": _lock_text(),
        },
    )


@pytest.mark.parametrize("kind", [DeployKind.DEPLOY, DeployKind.DRY_RUN])
def test_a_connection_the_environment_does_not_have_fails_the_deploy_and_the_dry_run(
    session: Session, principal: PrincipalRef, world: World, kind: DeployKind
) -> None:
    _commit_customers(world)
    run = _submit_and_run(session, principal, world, kind, settings=_python_settings(world))
    assert (run.status, run.error_message) == (DeployStatus.FAILED, _UNKNOWN_CRM_DB)
    assert session.query(JobVersion).count() == 0


def test_a_connection_the_environment_has_lets_the_deploy_through(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    _commit_customers(world)
    ConnectionService(session, AllowAllAuthorization(), _KEY).create(
        principal,
        "dev",
        name="crm-db",
        kind=ConnectionKind.DIRECTORY,
        address={"root": "shared", "path": "crm"},
        settings={},
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert version.manifest_json["version"] == 7
    customers = {entry["table"]: entry for entry in version.manifest_json["assets"]}
    assert customers["bronze.customers"]["connections"] == ["crm-db"]
    assert customers["silver.clean"]["kind"] == "sql"


def test_the_connection_check_names_every_missing_connection_of_every_asset(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    _commit_customers(world)
    commit(
        world.repo_path,
        {
            "assets/bronze/orders.py": _CUSTOMERS_PY.replace("customers", "orders").replace(
                "['crm-db']", "['crm-db', 'erp']"
            ),
        },
    )
    run = _submit_and_run(session, principal, world, settings=_python_settings(world))
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    lines = run.error_message.splitlines()
    assert [line.split(": ", 1)[0] for line in lines] == [
        "assets/bronze/customers.py:4",
        "assets/bronze/orders.py:4",
        "assets/bronze/orders.py:4",
    ]
    assert "connection `erp`" in lines[2]


_OLD_SPELLING_SQL = (
    "/* lakana\nmaterialize: incremental\nincremental_key: ts\n*/\nSELECT 1 AS id, 2 AS ts\n"
)


@pytest.mark.parametrize("kind", [DeployKind.DEPLOY, DeployKind.DRY_RUN])
def test_the_old_incremental_spelling_deploys_with_one_hint_per_asset(
    session: Session, principal: PrincipalRef, world: World, kind: DeployKind
) -> None:
    commit(
        world.repo_path,
        {
            "assets/bronze/customers.sql": _OLD_SPELLING_SQL,
            "assets/bronze/orders.sql": (
                "/* lakana\nwrite: append\nwatermark: ts\n*/\nSELECT 1 AS id, 2 AS ts\n"
            ),
        },
    )
    run = _submit_and_run(session, principal, world, kind)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    assert run.diff_json is not None
    assert run.diff_json["hints"] == [
        "bronze.customers: `materialize: incremental` is now written `write: append` with "
        "`watermark: ts`; both work"
    ]
    assert (
        DeployService(session, AllowAllAuthorization()).get(principal, "dev", run.id).diff
        == run.diff_json
    )


_ON_DEPLOY_YAML = (
    "repo: analytics\ndefaults:\n  owner: data-eng\n"
    "jobs:\n  refresh:\n    select: ['silver.*']\n    on_deploy: true\n"
)


class _AllButRun(AllowAllAuthorization):
    """Allows everything but `RUN`: a deployer who may not start runs."""

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse `RUN`, allow the rest.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Raises
        ------
        PermissionDenied
            For `RUN`.
        """
        if action is Action.RUN:
            raise PermissionDenied(f"{principal.name!r} may not RUN this.")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Returns
        -------
        bool
            False for `RUN` only.
        """
        return action is not Action.RUN


def test_a_deploy_starts_its_on_deploy_jobs_as_the_deployer(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """After the deploy committed, the worker starts the job with the trigger `deploy`."""
    commit(world.repo_path, {"lakana.yaml": _ON_DEPLOY_YAML})

    run = _submit_and_run(session, principal, world)

    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    (started,) = session.scalars(select(Run)).all()
    assert (started.job, started.trigger, started.triggered_by) == (
        "refresh",
        RunTrigger.DEPLOY,
        principal.id,
    )
    session.refresh(run)
    assert run.job_starts_json == [
        {"job": "refresh", "outcome": "started", "run_id": str(started.id)}
    ]


def test_a_deployer_without_run_deploys_and_starts_no_job(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """The deploy succeeds; the record says the job was not started and why."""
    commit(world.repo_path, {"lakana.yaml": _ON_DEPLOY_YAML})
    view = DeployService(session, AllowAllAuthorization()).submit(
        principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )

    run_deploy(session, view.id, _AllButRun(), _KEY, world.settings)

    run = session.get(DeployRun, view.id)
    assert run is not None
    session.refresh(run)
    assert run.status is DeployStatus.SUCCEEDED
    assert session.scalars(select(Run)).all() == []
    (entry,) = run.job_starts_json or []
    assert (entry["job"], entry["outcome"], entry["reason"]) == (
        "refresh",
        "not_started",
        "lacks_run",
    )


def test_a_failed_deploy_starts_no_job(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    """A deploy that fails validation stores and starts nothing."""
    commit(
        world.repo_path, {"lakana.yaml": _ON_DEPLOY_YAML, "assets/silver/bad.sql": "DROP TABLE x\n"}
    )

    run = _submit_and_run(session, principal, world)

    assert run.status is DeployStatus.FAILED
    assert run.job_starts_json is None
    assert session.scalars(select(Run)).all() == []


def test_a_dry_run_starts_no_job(session: Session, principal: PrincipalRef, world: World) -> None:
    """A dry run changes nothing, so it starts nothing."""
    commit(world.repo_path, {"lakana.yaml": _ON_DEPLOY_YAML})

    run = _submit_and_run(session, principal, world, DeployKind.DRY_RUN)

    assert run.status is DeployStatus.SUCCEEDED
    assert run.job_starts_json is None
    assert session.scalars(select(Run)).all() == []


def test_an_error_before_the_starts_leaves_the_deploy_succeeded(
    session: Session,
    principal: PrincipalRef,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deploy is committed first; a failure of the starts is logged and changes nothing."""
    commit(world.repo_path, {"lakana.yaml": _ON_DEPLOY_YAML})

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(deploy_module, "start_jobs_after_deploy", broken)

    run = _submit_and_run(session, principal, world)

    session.refresh(run)
    assert run.status is DeployStatus.SUCCEEDED
    assert run.job_starts_json == [{"job": "refresh", "outcome": "pending"}]
    (record,) = [r for r in caplog.records if "Could not start the jobs" in r.getMessage()]
    assert getattr(record, "event", None) == LogEvent.INTERNAL_ERROR.value


_DIM_STORE = (
    "/* lakana\nkind: dimension\ngrain: One row per store.\nkey: store_id\n*/\n"
    "SELECT id AS store_id FROM bronze.events\n"
)


def test_a_deploy_resolves_references_and_records_the_model_as_version_9(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "assets/gold/dim_store.sql": _DIM_STORE,
            "assets/gold/fact_visits.sql": (
                "/* lakana\nkind: fact\ngrain: One row per event.\nkey: id\n"
                "references:\n  store_id: gold.dim_store\n*/\n"
                "SELECT id, id AS store_id FROM bronze.events\n"
            ),
        },
    )
    run = _submit_and_run(session, principal, world)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    version = session.get(JobVersion, run.job_version_id)
    assert version is not None
    assert version.manifest_json["version"] == 9
    fact = manifest_assets(version.manifest_json)["gold.fact_visits"]
    assert fact["model"]["references"] == [
        {"column": "store_id", "target": "gold.dim_store", "target_column": "store_id"}
    ]


def test_a_deploy_refuses_a_reference_it_cannot_resolve_and_a_key_the_select_lacks(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "assets/gold/fact_visits.sql": (
                "/* lakana\nkey: visit_id\nreferences:\n  id: bronze.events\n*/\n"
                "SELECT id FROM bronze.events\n"
            ),
        },
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert (
        "assets/gold/fact_visits.sql:1: Key column `visit_id` is not a column of "
        "`gold.fact_visits`'s SELECT; add it to the SELECT, or change `key:`." in run.error_message
    )
    assert (
        "assets/gold/fact_visits.sql:1: Reference `id: bronze.events`: `bronze.events` declares "
        "no key. Declare `key:` in its asset, or name the column: `id: bronze.events.<column>`."
        in run.error_message
    )
    # The line tells of `bronze.events`: a reader who may not read it is shown a missing one's.
    assert run.error_withheld_json == [
        {
            "index": 1,
            "line": "assets/gold/fact_visits.sql:1: Reference `id: bronze.events`: "
            "`bronze.events` declares no key. Declare `key:` in its asset, or name the column: "
            "`id: bronze.events.<column>`.",
            "withheld": "assets/gold/fact_visits.sql:1: Reference target `bronze.events` does "
            "not exist in environment `dev`, or you may not read it; build it first, or ask for "
            "READ_TABLE on it.",
            "targets": ["bronze.events"],
        }
    ]
    # The position contract `error_for_reader` relies on: each stored line is the text of the
    # error at its index, end to end from the deploy that stored both.
    lines = run.error_message.split("\n")
    for entry in run.error_withheld_json or []:
        size = entry["line"].count("\n") + 1
        assert "\n".join(lines[entry["index"] : entry["index"] + size]) == entry["line"]
    assert session.query(JobVersion).count() == 0


_TEXTS_SQL = (
    "/* lakana\nlabel: Clean events\ndescription: Clean events.\ncolumns:\n"
    "  id: {label: Id, description: The id.}\n*/\nSELECT id FROM bronze.events\n"
)


@pytest.mark.parametrize("kind", [DeployKind.DEPLOY, DeployKind.DRY_RUN])
def test_a_project_with_translations_deploys_with_its_coverage(
    session: Session, principal: PrincipalRef, world: World, kind: DeployKind
) -> None:
    commit(
        world.repo_path,
        {
            "lakana.yaml": "repo: analytics\nmain_language: en\n",
            "assets/silver/clean.sql": _TEXTS_SQL,
            "translations/fr.yaml": "silver.clean:\n  label: Événements\n",
        },
    )
    run = _submit_and_run(session, principal, world, kind)
    assert (run.status, run.error_message) == (DeployStatus.SUCCEEDED, None)
    assert run.diff_json is not None
    assert run.diff_json["hints"] == [
        "translations/fr.yaml: 1 of 4 texts translated; the other 3 show in `en`"
    ]
    if kind is DeployKind.DEPLOY:
        version = session.get(JobVersion, run.job_version_id)
        assert version is not None
        manifest = version.manifest_json
        assert (manifest["version"], manifest["main_language"]) == (11, "en")
        assert manifest["assets"][0]["metadata"]["translations"] == {"fr": {"label": "Événements"}}


def test_a_misspelt_translation_key_refuses_the_deploy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "lakana.yaml": "repo: analytics\nmain_language: en\n",
            "translations/fr.yaml": "silver.clen:\n  label: X\n",
        },
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "translations/fr.yaml:1: `silver.clen` is not a table this project builds; did you mean "
        "`silver.clean`? Fix the name, or remove the entry"
    )


def test_translations_without_a_main_language_refuse_the_deploy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "assets/silver/clean.sql": _TEXTS_SQL,
            "translations/fr.yaml": "silver.clean:\n  label: Événements\n",
        },
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "lakana.yaml: a project with translations must say which language its asset headers are "
        "written in; add `main_language:` with that language, such as `main_language: en`, to "
        "lakana.yaml"
    )


def test_a_required_language_with_gaps_refuses_the_deploy(
    session: Session, principal: PrincipalRef, world: World
) -> None:
    commit(
        world.repo_path,
        {
            "lakana.yaml": "repo: analytics\nmain_language: en\ntranslations:\n  require: [fr]\n",
            "assets/silver/clean.sql": _TEXTS_SQL,
            "translations/fr.yaml": "silver.clean:\n  label: Événements\n",
        },
    )
    run = _submit_and_run(session, principal, world)
    assert run.status is DeployStatus.FAILED
    assert run.error_message == (
        "translations/fr.yaml: translations.require in lakana.yaml needs a `fr` text for every "
        "header text, and 3 are missing: silver.clean.columns.id.description, "
        "silver.clean.columns.id.label, silver.clean.description. Add them to "
        "translations/fr.yaml, or remove `fr` from translations.require."
    )
