"""The worker's re-check of a deploy a CI token queued: the token, its rule, its commit."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from worker_git_helpers import commit, make_repo

from lakana_core.auth import AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog.service import CatalogService
from lakana_core.ci_oidc.exchange import resolve_ci_scope
from lakana_core.config import StorageConfig
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    CiToken,
    CiTrustRule,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
    Run,
    RunTrigger,
)
from lakana_core.projects.deploys import DeployService
from lakana_core.projects.repositories import RepositoryService
from lakana_core.storage.resolver import StorageResolver
from lakana_worker import deploy as deploy_module
from lakana_worker.deploy import DeploySettings, run_deploy

_FILES = {
    "lakana.yaml": "repo: analytics\ndefaults:\n  owner: data-eng\n",
    "assets/silver/clean.sql": (
        "/* lakana\ndescription: Clean events.\n*/\nSELECT id FROM bronze.events\n"
    ),
}


@dataclasses.dataclass(frozen=True)
class Ci:
    """What the `ci` fixture made.

    Parameters
    ----------
    principal
        The CI-scoped deployer.
    rule_id
        Its rule.
    sha
        The repository's commit, the token's signed one.
    settings
        Deploy settings.
    """

    principal: PrincipalRef
    rule_id: uuid.UUID
    sha: str
    settings: DeploySettings


@pytest.fixture
def ci(session: Session, tmp_path: Path) -> Ci:
    """Set up ``dev``, a local repository, a deployer, a rule and a CI token for the commit.

    The token row is written directly: the exchange itself is tested in lakana-core.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    Ci
        The CI-scoped principal and the rest.
    """
    admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=admin.id, type=admin.type, name="admin", is_active=True))
    session.commit()
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        admin,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path / 'storage'}", options={}),
    )
    catalog.create_table(
        admin,
        TableRef("dev", NamespacePath.from_parts(["bronze"]), "events"),
        schema=_schema(),
    )
    repo_path = tmp_path / "repos" / "analytics"
    sha = make_repo(repo_path, _FILES)
    roots = (str(tmp_path / "repos"),)
    RepositoryService(session, AllowAllAuthorization(), repo_roots=roots).create(
        admin, name="analytics", kind=RepositoryKind.LOCAL, url=str(repo_path)
    )
    deployer = Principal(
        id=uuid.uuid4(), type=PrincipalType.SERVICE_ACCOUNT, name="deployer", is_active=True
    )
    session.add(deployer)
    session.commit()
    GrantService(session, AllowAllAuthorization()).grant(
        admin,
        principal="deployer",
        role=Role.ADMIN,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    env = session.execute(select(Environment).where(Environment.name == "dev")).scalar_one()
    repo = session.execute(select(Repository).where(Repository.name == "analytics")).scalar_one()
    now = datetime.now(UTC)
    rule = CiTrustRule(
        id=uuid.uuid4(),
        issuer="https://token.actions.githubusercontent.com",
        principal_id=deployer.id,
        environment_id=env.id,
        repository_id=repo.id,
        kinds=["deploy", "dry_run"],
        conditions={"repository_id": "1", "ref": "refs/heads/main", "event_name": "push"},
        enabled=True,
        revision=1,
        created_at=now,
        updated_at=now,
    )
    token = CiToken(
        id=uuid.uuid4(),
        key_id="cikey0000000000a",
        secret_hash="0" * 64,
        rule_id=rule.id,
        rule_revision=1,
        principal_id=deployer.id,
        environment_id=env.id,
        repository_id=repo.id,
        kinds=["deploy", "dry_run"],
        sha=sha,
        jti_sha256="1" * 64,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )
    session.add(rule)
    session.flush()
    session.add(token)
    session.commit()
    scope = resolve_ci_scope(session, token, now=now, require_unexpired=True)
    assert scope is not None
    principal = PrincipalRef(
        id=deployer.id, name="deployer", type=PrincipalType.SERVICE_ACCOUNT, ci_scope=scope
    )
    return Ci(principal, rule.id, sha, DeploySettings(repo_roots=roots))


def _schema() -> Any:  # ruff: ignore[any-type]
    """Return the schema of ``bronze.events``.

    Returns
    -------
    pyiceberg.schema.Schema
        One required ``id`` column.
    """
    from pyiceberg.schema import Schema
    from pyiceberg.types import LongType, NestedField

    return Schema(NestedField(1, "id", LongType(), required=True))


def _run(session: Session, ci: Ci, kind: DeployKind = DeployKind.DEPLOY) -> DeployRun:
    """Queue a run with the CI token and let the worker run it with grant authorization.

    Parameters
    ----------
    session
        Database session.
    ci
        The fixture.
    kind
        What to queue.

    Returns
    -------
    DeployRun
        The finished run.
    """
    view = DeployService(session, GrantAuthorization(session)).submit(
        ci.principal, "dev", repository="analytics", kind=kind
    )
    return _execute(session, ci, view.id)


def _execute(session: Session, ci: Ci, run_id: uuid.UUID) -> DeployRun:
    """Let the worker run a queued run.

    Parameters
    ----------
    session
        Database session.
    ci
        The fixture.
    run_id
        The run.

    Returns
    -------
    DeployRun
        The finished run.
    """
    run_deploy(session, run_id, GrantAuthorization(session), None, ci.settings, worker_id="w1")
    session.expire_all()
    run = session.get(DeployRun, run_id)
    assert run is not None
    return run


def test_a_ci_deploy_of_the_signed_commit_succeeds(session: Session, ci: Ci) -> None:
    run = _run(session, ci)
    assert (run.status, run.resolved_sha, run.error_message) == (
        DeployStatus.SUCCEEDED,
        ci.sha,
        None,
    )
    assert session.execute(select(JobVersion.commit_sha)).scalar_one() == ci.sha


def test_a_ci_dry_run_succeeds(session: Session, ci: Ci) -> None:
    assert _run(session, ci, DeployKind.DRY_RUN).status is DeployStatus.SUCCEEDED


def test_a_fetch_of_another_commit_fails_the_ci_deploy(
    session: Session, ci: Ci, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = deploy_module.fetch_commit

    def elsewhere(**kwargs: Any) -> Any:  # ruff: ignore[any-type]
        """Fetch, then report another commit, as a moved ref would.

        Parameters
        ----------
        **kwargs
            The real call's arguments.

        Returns
        -------
        FetchedCommit
            With another SHA.
        """
        return dataclasses.replace(real(**kwargs), sha="f" * 40)

    monkeypatch.setattr(deploy_module, "fetch_commit", elsewhere)
    run = _run(session, ci)
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None
    assert run.error_message.startswith("permission denied: The ref resolved to " + "f" * 40)
    assert session.execute(select(JobVersion)).first() is None


def test_a_rule_disabled_after_queueing_fails_the_ci_deploy(session: Session, ci: Ci) -> None:
    view = DeployService(session, GrantAuthorization(session)).submit(
        ci.principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    session.execute(
        update(CiTrustRule).where(CiTrustRule.id == ci.rule_id).values(enabled=False, revision=2)
    )
    session.commit()
    run = _execute(session, ci, view.id)
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None and "no longer valid" in run.error_message
    assert session.execute(select(JobVersion)).first() is None


def test_a_tampered_ref_of_a_ci_deploy_fails_it(session: Session, ci: Ci) -> None:
    view = DeployService(session, GrantAuthorization(session)).submit(
        ci.principal, "dev", repository="analytics", kind=DeployKind.DEPLOY
    )
    session.execute(update(DeployRun).values(requested_ref="main"))
    session.commit()
    run = _execute(session, ci, view.id)
    assert run.status is DeployStatus.FAILED
    assert run.error_message is not None and ci.sha in run.error_message


_ON_DEPLOY_YAML = (
    "repo: analytics\ndefaults:\n  owner: data-eng\n"
    "jobs:\n  refresh:\n    select: ['silver.*']\n    on_deploy: true\n"
)


def _signed_on_deploy_commit(session: Session, ci: Ci, tmp_path: Path) -> Ci:
    """Commit a job with `on_deploy: true` and make it the CI token's signed commit.

    Parameters
    ----------
    session
        Database session.
    ci
        The fixture.
    tmp_path
        Pytest's per-test directory, which holds the repository.

    Returns
    -------
    Ci
        The fixture, with the principal's scope naming the new commit.
    """
    sha = commit(tmp_path / "repos" / "analytics", {"lakana.yaml": _ON_DEPLOY_YAML})
    session.execute(update(CiToken).values(sha=sha))
    session.commit()
    token = session.execute(select(CiToken)).scalar_one()
    scope = resolve_ci_scope(session, token, now=datetime.now(UTC), require_unexpired=True)
    assert scope is not None
    return dataclasses.replace(
        ci, principal=dataclasses.replace(ci.principal, ci_scope=scope), sha=sha
    )


def _runs(session: Session) -> list[Run]:
    """Read every run.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of Run
        The runs.
    """
    session.expire_all()
    return list(session.scalars(select(Run)))


def test_a_ci_deploy_starts_no_job_though_its_account_may_run(
    session: Session, ci: Ci, tmp_path: Path
) -> None:
    """The token may only deploy: the start is checked with its scope, so nothing starts."""
    signed = _signed_on_deploy_commit(session, ci, tmp_path)

    run = _run(session, signed)

    assert run.status is DeployStatus.SUCCEEDED
    assert _runs(session) == []
    (entry,) = run.job_starts_json or []
    assert (entry["job"], entry["outcome"], entry["reason"]) == (
        "refresh",
        "not_started",
        "lacks_run",
    )
    assert entry["message"] == (
        "job `refresh` was not started: deployer deployed with a CI token, and its CI trust rule "
        f"does not start the deploy's jobs. Enable it with `lakana ci-trust update {ci.rule_id} "
        "--start-on-deploy-jobs`, or start the job with "
        "`lakana job run refresh --repo analytics --env dev` and a stored token that holds RUN."
    )


def test_a_ci_deploy_under_a_rule_that_opts_in_starts_the_job(
    session: Session, ci: Ci, tmp_path: Path
) -> None:
    """The worker passes the token's scope; the rule's opt-in allows the start, nothing more."""
    session.execute(update(CiTrustRule).values(start_on_deploy_jobs=True))
    session.commit()
    signed = _signed_on_deploy_commit(session, ci, tmp_path)

    run = _run(session, signed)

    assert run.status is DeployStatus.SUCCEEDED
    (started,) = _runs(session)
    assert (started.job, started.trigger) == ("refresh", RunTrigger.DEPLOY)
    (entry,) = run.job_starts_json or []
    assert (entry["outcome"], entry["run_id"]) == ("started", str(started.id))


def test_the_same_account_without_a_ci_token_starts_the_job(
    session: Session, ci: Ci, tmp_path: Path
) -> None:
    """With a stored token the account's own grants apply, and they hold RUN."""
    signed = _signed_on_deploy_commit(session, ci, tmp_path)
    stored = dataclasses.replace(
        signed, principal=dataclasses.replace(signed.principal, ci_scope=None)
    )

    run = _run(session, stored)

    assert run.status is DeployStatus.SUCCEEDED
    (started,) = _runs(session)
    assert (started.job, started.trigger, started.triggered_by) == (
        "refresh",
        RunTrigger.DEPLOY,
        signed.principal.id,
    )
