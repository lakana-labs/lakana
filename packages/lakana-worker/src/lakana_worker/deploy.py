"""Run one queued deploy, dry run or rollback.

Every failure ends as `status=failed` with a message that is safe to show:
project issues with file and line, a redacted git message, "permission
denied", or — for anything unexpected — a generic message while the detail
goes to the worker log. Nothing a run does can take down the poller.
"""

from __future__ import annotations

import contextlib
import dataclasses
import gzip
import hashlib
import logging
import shlex
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AuthorizationService, CiScope, PrincipalRef
from lakana_core.errors import (
    ConflictError,
    LakanaError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.identifiers import shown
from lakana_core.jobs.on_deploy import start_jobs_after_deploy
from lakana_core.log_events import LogEvent, log_event
from lakana_core.models import (
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    JobVersion,
    Principal,
    Repository,
    RepositoryKind,
    Secret,
)
from lakana_core.projects.asset_model import deploy_hints
from lakana_core.projects.checks import check_issues, checks_listing, parse_checks
from lakana_core.projects.dag import build_graph, validate_project
from lakana_core.projects.deploys import DeclaredConnections, DeployService, connection_issues
from lakana_core.projects.diff import diff_manifests
from lakana_core.projects.discovery import discover
from lakana_core.projects.issues import ProjectIssue, ProjectValidationError, withheld_lines
from lakana_core.projects.jobs import job_issues
from lakana_core.projects.layer_rules import layer_hints, layer_issues
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.lockfile import (
    LOCK_FILE,
    PYPROJECT_FILE,
    lock_digest,
    lockfile_issues,
    task_interpreter,
    used_indexes,
)
from lakana_core.projects.manifest import build_manifest, manifest_tables
from lakana_core.projects.model import project_model_issues
from lakana_core.projects.model_checks import generated_checks, generated_name_issues
from lakana_core.projects.package_indexes import registered_indexes
from lakana_core.projects.package_sources import KnownSources, RegisteredIndex
from lakana_core.projects.parse import ParsedAsset, parse_assets
from lakana_core.projects.project_config import normalize_source, parse_project_config
from lakana_core.projects.run_policy import RunCaps, run_policy_issues
from lakana_core.projects.translations import (
    coverage_hints,
    parse_translations,
    translation_entries,
    translation_issues,
)
from lakana_core.secrets.crypto import decrypt_secret_row
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.uses import SecretUse, plain, require_use
from lakana_core.storage.base import Storage
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.deploy_sweep import CHECKOUT_PREFIX, hold_checkout
from lakana_worker.git import GitCredential, GitError, fetch_commit
from lakana_worker.index_credentials import IndexCredentialError, resolve_index_access
from lakana_worker.python_support import CANNOT_RUN, PythonSupport

logger = logging.getLogger(__name__)

INTERNAL_ERROR_MESSAGE = "internal error during deploy; see the worker log"
_INACTIVE_MESSAGE = "the principal that queued this deploy no longer exists or is inactive"
_TIME_LIMIT_MESSAGE = "the deploy exceeded its time limit"
_MAX_ISSUES = 50


@dataclass(frozen=True)
class DeploySettings:
    """Worker-level deploy configuration.

    Parameters
    ----------
    repo_roots
        `LAKANA_REPO_ROOTS`.
    limits
        Time and size limits.
    caps
        Operator maximums for retries and timeouts.
    ca_bundle
        Extra CA file for HTTPS fetches (`LAKANA_GIT__CA_BUNDLE`); `None` uses the system store.
    https_proxy
        Proxy URL for HTTPS fetches (`LAKANA_GIT__HTTPS_PROXY`); `None` means no proxy.
        Left out of `repr`: it may carry a password.
    no_proxy
        Hosts that bypass the proxy (`LAKANA_GIT__NO_PROXY`).
    index_url
        `LAKANA_RUN__INDEX_URL`, the operator's extra package index: a lock may name it.
        Left out of `repr`: it may carry a password.
    python
        Whether this worker can run Python assets; the default cannot.
    """

    repo_roots: tuple[str, ...] = ()
    limits: DeployLimits = field(default_factory=DeployLimits)
    caps: RunCaps = field(default_factory=RunCaps)
    ca_bundle: Path | None = None
    https_proxy: str | None = field(default=None, repr=False)
    no_proxy: str | None = None
    index_url: str | None = field(default=None, repr=False)
    python: PythonSupport = field(default_factory=PythonSupport)


def platform_version() -> str:
    """Return this worker's version, which a project's locked `lakana` must be compatible with.

    Returns
    -------
    str
        The installed `lakana-worker` distribution's version; every package
        of a release shares it.
    """
    return _package_version("lakana-worker")


def _python_issues(
    pyproject: str | None,
    lock: bytes | None,
    python: PythonSupport,
    sources: KnownSources,
    interpreter: tuple[int, int, int],
    named_python: str | None = None,
) -> list[ProjectIssue]:
    """Validate what a project with Python assets needs beyond its assets.

    Parameters
    ----------
    pyproject
        `pyproject.toml`'s text, if present.
    lock
        `uv.lock`'s bytes, if present.
    python
        Whether this worker can run Python assets.
    sources
        The package sources this instance knows.
    interpreter
        The `(major, minor, micro)` the project's tasks run on, `task_interpreter`'s answer.
    named_python
        `lakana.yaml`'s `python:`, if the project names one; the issues then name it.

    Returns
    -------
    list of ProjectIssue
        The dependency-file issues, then "this instance cannot run Python
        assets" with the reason.
    """
    issues = lockfile_issues(
        pyproject,
        lock,
        platform_version=platform_version(),
        python=interpreter,
        sources=sources,
        named_python=named_python,
    )
    reason = python.reason()
    if reason is not None:
        issues.append(ProjectIssue(PYPROJECT_FILE, None, CANNOT_RUN.format(reason=reason)))
    return issues


def code_path(repository: str, sha: str, code_sha256: str) -> str:
    """Return the environment-relative path of a commit's code tarball.

    The digest is part of the name, so an object is never replaced by
    different bytes: two workers deploying one SHA (or a different git
    producing a different archive) write two objects, and a recorded
    `code_sha256` always matches the object its `code_uri` names.

    Parameters
    ----------
    repository
        Repository name.
    sha
        Commit SHA.
    code_sha256
        SHA-256 of the gzipped tarball, hex.

    Returns
    -------
    str
        ``_artifacts/code/<repository>/<sha>-<code_sha256>.tar.gz``.
    """
    return f"_artifacts/code/{repository}/{sha}-{code_sha256}.tar.gz"


def resolve_credential(
    session: Session,
    key_provider: KeyProvider | None,
    environment: Environment,
    repository: Repository,
) -> GitCredential | None:
    """Decrypt a repository's credential from the target environment's secret scope.

    The second of the four places that decrypt a secret (the first is
    `StorageResolver`, the third `lakana_worker.index_credentials.resolve_index_access`). The
    plaintext lives only in the returned object.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Unwraps the secret's data key.
    environment
        The deploy's environment — its scope, not any other, is searched.
    repository
        The repository.

    Returns
    -------
    GitCredential or None
        `None` if the repository names no secret.

    Raises
    ------
    ValidationError
        If no key provider is configured or the secret lacks the key its
        kind needs.
    NotFoundError
        If the secret does not exist (or is revoked) in this environment.
    SecretUseRefused
        If the secret is not bound to this repository.
    LakanaError
        If decryption fails.
    """
    name = repository.credential_secret
    if name is None:
        return None
    if key_provider is None:
        raise ValidationError(
            f"repository {repository.name!r} has a credential "
            "but no secrets master key is configured"
        )
    secret = session.execute(
        select(Secret).where(
            Secret.environment_id == environment.id,
            Secret.name == name,
            Secret.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if secret is None:
        raise NotFoundError(
            f"secret {name!r} does not exist in environment {environment.name!r}. Create it "
            f"with `lakana secret create {shlex.quote(plain(name))} --env "
            f"{shlex.quote(plain(environment.name))} --repository "
            f"{shlex.quote(plain(repository.name))} "
            "--values-stdin`"
        )
    require_use(
        session, secret, SecretUse.repository(repository), environment_name=environment.name
    )
    values = decrypt_secret_row(
        secret,
        key_provider,
        environment_name=environment.name,
        accept_format_1=key_provider.accepts_format_1,
    )
    key = "private_key" if repository.kind is RepositoryKind.SSH else "token"
    value = values.get(key)
    if not value:
        raise ValidationError(f"secret {name!r} has no {key!r} value")
    if key == "private_key":
        return GitCredential(private_key=value)
    return GitCredential(username=repository.credential_username, token=value)


def _credential_issues(
    session: Session,
    key_provider: KeyProvider | None,
    environment_id: uuid.UUID,
    environment_name: str,
    indexes: Sequence[RegisteredIndex],
) -> list[ProjectIssue]:
    """Check that every index a lock uses can get its credential in this environment.

    Each secret is decrypted to see that it holds a password, and the
    plaintext is dropped at once: nothing of it is returned or stored.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Unwraps the secrets' data keys.
    environment_id
        The deploy's environment, as a value (a commit expires the row).
    environment_name
        The deploy's environment name.
    indexes
        The registered indexes the lock uses.

    Returns
    -------
    list of ProjectIssue
        One issue per index whose secret is missing, cannot be decrypted or
        holds no usable password, with the command that sets it.
    """
    issues: list[ProjectIssue] = []
    for index in indexes:
        try:
            resolve_index_access(session, key_provider, environment_id, environment_name, [index])
        except IndexCredentialError as exc:
            issues.append(ProjectIssue(LOCK_FILE, None, str(exc)))
    return issues


def _connection_issues(
    session: Session,
    environment_id: uuid.UUID,
    environment_name: str,
    assets: Sequence[ParsedAsset],
) -> list[ProjectIssue]:
    """Check that every connection an asset declares exists in the environment.

    Read without a lock: the record reads them again ``FOR SHARE`` in its own transaction
    (`DeployService.record_deploy`), which is what orders a deploy against a delete.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The deploy's environment, as a value (a commit expires the row).
    environment_name
        The deploy's environment name.
    assets
        The parsed assets.

    Returns
    -------
    list of ProjectIssue
        One issue per missing connection, at the declaring asset's decorator line, with the
        command that creates it.
    """
    declared = [
        DeclaredConnections(asset.table, asset.path, asset.block_line, asset.connections)
        for asset in assets
        if asset.connections
    ]
    return connection_issues(session, environment_id, environment_name, declared, lock=False)


def _describe(exc: LakanaError | GitError) -> str:
    """Render a failure for `error_message`, capping a long issue list.

    Parameters
    ----------
    exc
        A project, service or git failure; its text is already safe to store.

    Returns
    -------
    str
        The message; at most `_MAX_ISSUES` project issues, then a count.
    """
    if isinstance(exc, ProjectValidationError) and len(exc.issues) > _MAX_ISSUES:
        listed = "\n".join(issue.render() for issue in exc.issues[:_MAX_ISSUES])
        return f"{listed}\n…and {len(exc.issues) - _MAX_ISSUES} more issues"
    return str(exc)


def _withheld(exc: LakanaError | GitError) -> list[dict[str, Any]]:
    """List the lines of a failure's text that other readers of the deploy are shown otherwise.

    Parameters
    ----------
    exc
        The failure `_describe` renders.

    Returns
    -------
    list of dict
        `withheld_lines` of the project issues the text lists; empty for any other failure.
    """
    if not isinstance(exc, ProjectValidationError):
        return []
    return withheld_lines(exc.issues[:_MAX_ISSUES])


def _fail(
    service: DeployService,
    run: DeployRun,
    message: str,
    worker_id: str | None,
    withheld: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Mark a run failed, tolerating a run that is gone or already ended.

    A run the scheduler ended while this worker was lost (or frozen) keeps the scheduler's
    message: the refusal is logged once, as a warning, and nothing is written.

    Parameters
    ----------
    service
        The deploy service.
    run
        The run.
    message
        Safe error text.
    worker_id
        The id the run was claimed under; `None` for a run that is still queued or carries none.
    withheld
        The lines of `message` that tell of a reference target (`_withheld`).
    """
    try:
        service.fail(run, message, worker_id=worker_id, withheld=withheld)
    except (ConflictError, NotFoundError):
        logger.warning(
            "Deploy run could not be marked failed (gone, finished, ended by the scheduler or "
            "claimed by another worker): %s",
            message,
        )


@dataclass(frozen=True)
class _Upload:
    """A code tarball this run wrote.

    Parameters
    ----------
    storage
        The environment's storage.
    path
        Environment-relative path of the object.
    uri
        The `code_uri` a project version would record for it.
    """

    storage: Storage
    path: str
    uri: str


def _discard_losing_upload(
    session: Session,
    upload: _Upload,
    repository_id: uuid.UUID,
    environment_id: uuid.UUID,
    sha: str,
) -> None:
    """Delete this run's tarball if the commit's project version names a different object.

    A project version's `code_uri` never changes, so once one exists for the commit
    and names another object, nothing can ever reference this one. Without
    such a row, or when any project version in any environment names this very
    object, it is kept even though the run failed: two workers
    producing identical bytes share one digest-named path, and a concurrent
    winner may be about to record it. Best effort: failures are only logged.

    Parameters
    ----------
    session
        Database session.
    upload
        What this run wrote.
    repository_id
        The repository.
    environment_id
        The environment.
    sha
        The commit.
    """
    try:
        recorded = session.execute(
            select(JobVersion.code_uri).where(
                JobVersion.repository_id == repository_id,
                JobVersion.environment_id == environment_id,
                JobVersion.commit_sha == sha,
            )
        ).scalar_one_or_none()
        if recorded is None or recorded == upload.uri:
            return
        # Environments may share a storage root, so a project version anywhere can name this object.
        named = session.execute(
            select(JobVersion.id).where(JobVersion.code_uri == upload.uri).limit(1)
        ).first()
        if named is None:
            upload.storage.delete(upload.path)
    except Exception:
        logger.warning(
            "Could not remove a superseded code tarball %s.", shown(upload.path), exc_info=True
        )


def _restore_if_missing(
    session: Session,
    key_provider: KeyProvider | None,
    environment_name: str,
    archive: Path,
    *,
    path: str,
    expected_uri: str,
    storage_root: str,
    code_sha256: str,
) -> None:
    """Write a recorded code archive again if it is missing from storage.

    The code archive sweep can delete an unnamed archive just as a deploy of the
    same code records it; a project version that is reused later would then name nothing.
    One existence check; a write only when the object is gone. The archive is written
    again only if it hashes to the recorded digest. Best effort: a failure is logged and
    never fails a deploy that is already recorded.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Decrypts the storage secret.
    environment_name
        The environment.
    archive
        The `git archive` of the fetched commit.
    path
        Environment-relative path the archive should have.
    expected_uri
        The ``code_uri`` the project version records.
    storage_root
        The environment's storage root.
    code_sha256
        The recorded digest.
    """
    if expected_uri != f"{storage_root.rstrip('/')}/{path}":
        logger.warning(
            "The code archive %s that a project version names is not at this environment's current "
            "storage root, so it was not checked.",
            shown(path),
        )
        return
    try:
        storage = StorageResolver(session, key_provider).for_environment(environment_name).storage
        if storage.exists(path):
            return
        tarball = gzip.compress(archive.read_bytes(), mtime=0)
        if hashlib.sha256(tarball).hexdigest() != code_sha256:
            logger.warning(
                "The code archive %s that a project version names is missing and cannot be "
                "restored from this checkout (its digest differs); deploy a new commit.",
                shown(path),
            )
            return
        storage.write(path, tarball)
        logger.warning(
            "The code archive %s that a project version names was missing and has been restored.",
            shown(path),
        )
    except Exception:
        # A database error inside the resolver would leave the session unusable for what follows.
        session.rollback()
        logger.warning(
            "Could not check or restore the code archive %s.", shown(path), exc_info=True
        )


def run_deploy(
    session: Session,
    run_id: uuid.UUID,
    authz: AuthorizationService,
    key_provider: KeyProvider | None = None,
    settings: DeploySettings | None = None,
    clock: Callable[[], float] = time.monotonic,
    *,
    worker_id: str | None = None,
) -> None:
    """Run one queued deploy run to completion; never raises for the run's own failure.

    A run that no longer exists, or is no longer queued, is skipped; one whose
    repository or environment row is gone is failed.

    Parameters
    ----------
    session
        Database session.
    run_id
        A `QUEUED` deploy run.
    authz
        Authorization service for the re-check and the environment checks.
    key_provider
        Decrypts the repository credential.
    settings
        Repo roots, limits, git proxy and CA bundle; `None` uses `DeploySettings()`.
    clock
        Monotonic clock (tests may substitute one).
    worker_id
        The id of the worker running the deploy; written with `RUNNING`. `None` writes
        none, and a run without one is ended once it started a lease ago.
    """
    settings = settings or DeploySettings()
    run = session.get(DeployRun, run_id)
    if run is None or run.status is not DeployStatus.QUEUED:
        logger.warning("Deploy run %s is gone or not queued; skipping.", run_id)
        return
    service = DeployService(session, authz)
    environment = session.get(Environment, run.environment_id)
    repository = session.get(Repository, run.repository_id)
    if environment is None or repository is None:
        # Unreachable while the foreign keys cascade. Failed directly, because
        # `DeployService.fail` refuses a run whose parents are gone, and a run left
        # QUEUED would be re-claimed by the poller in a tight loop.
        logger.warning("Deploy run %s lost its repository or environment; failing it.", run_id)
        run.status = DeployStatus.FAILED
        run.error_message = "the repository or environment was deleted"
        run.finished_at = datetime.now(UTC)
        session.commit()
        return
    row = session.get(Principal, run.principal_id) if run.principal_id else None
    if row is None or not row.is_active:
        _fail(service, run, _INACTIVE_MESSAGE, None)
        return
    principal = PrincipalRef(id=row.id, name=row.name, type=row.type)
    kind = run.kind
    run.status = DeployStatus.RUNNING
    run.worker_id = worker_id
    run.started_at = datetime.now(UTC)
    session.commit()
    try:
        ci_scope = service.authorize_deploy(principal, environment.name, run)
        if kind is DeployKind.ROLLBACK:
            service.execute_rollback(principal, run, worker_id=worker_id)
            return
        _deploy(
            session,
            service,
            run,
            principal,
            environment,
            repository,
            key_provider,
            settings,
            clock,
            worker_id,
            ci_scope,
        )
    except PermissionDenied as exc:
        session.rollback()
        _fail(service, run, f"permission denied: {exc}", worker_id)
    except (LakanaError, GitError) as exc:
        session.rollback()
        _fail(service, run, _describe(exc), worker_id, _withheld(exc))
    except Exception:
        logger.exception(
            "Deploy run %s failed unexpectedly.", run_id, extra=log_event(LogEvent.INTERNAL_ERROR)
        )
        session.rollback()
        _fail(service, run, INTERNAL_ERROR_MESSAGE, worker_id)
    else:
        if kind is DeployKind.DEPLOY:
            # The starts are checked with the rights the deploy had: a CI token's scope too.
            requester = dataclasses.replace(principal, ci_scope=ci_scope)
            _start_jobs(session, authz, run_id, requester, settings.caps)


def _start_jobs(
    session: Session,
    authz: AuthorizationService,
    run_id: uuid.UUID,
    principal: PrincipalRef,
    caps: RunCaps,
) -> None:
    """Start the jobs with ``on_deploy: true`` of a deploy that was recorded; never raises.

    The deploy is committed and stays as it is whatever happens here: each job's outcome is
    written to the deploy run (`start_jobs_after_deploy`), and an error before any start is
    logged, which leaves the starts pending (the deploy's record shows them as lost after the run
    lease).

    Parameters
    ----------
    session
        Database session.
    authz
        The worker's authorization service, which checks and audits `RUN` as for any start.
    run_id
        The deploy run.
    principal
        The principal that deployed, with the scope of the CI token that queued the deploy, if
        any: the starts have exactly the rights the deploy had, so a CI token (which may only
        deploy) starts nothing.
    caps
        The operator's maximums for each task's policy.
    """
    try:
        start_jobs_after_deploy(session, authz, run_id, principal, caps=caps)
    except Exception:
        session.rollback()
        logger.exception(
            "Could not start the jobs of deploy run %s.",
            run_id,
            extra=log_event(LogEvent.INTERNAL_ERROR),
        )


def _deploy(
    session: Session,
    service: DeployService,
    run: DeployRun,
    principal: PrincipalRef,
    environment: Environment,
    repository: Repository,
    key_provider: KeyProvider | None,
    settings: DeploySettings,
    clock: Callable[[], float],
    worker_id: str | None,
    ci_scope: CiScope | None = None,
) -> None:
    """Fetch, discover, parse, validate, diff and (unless a dry run) record.

    Parameters
    ----------
    session
        Database session.
    service
        The deploy service.
    run
        The running deploy or dry run.
    principal
        The re-authorized submitter.
    environment
        Target environment.
    repository
        Source repository.
    key_provider
        Decrypts the credential.
    settings
        Deploy settings.
    clock
        Monotonic clock.
    worker_id
        The id the run was claimed under; the records are refused if the run is no longer
        `running` under it.
    ci_scope
        The scope of the CI token that queued the run (`DeployService.authorize_deploy`), or
        `None`. A deploy it queued must fetch exactly its signed commit.

    Raises
    ------
    ProjectValidationError
        With every project issue.
    GitError
        If fetching fails or the job runs out of time.
    PermissionDenied
        If a CI token's deploy fetched another commit than the token's.
    """
    limits = settings.limits
    deadline = clock() + limits.job_seconds
    environment_id, environment_name = environment.id, environment.name
    storage_root = environment.storage_root
    repository_id, repository_name = repository.id, repository.name
    kind = run.kind
    ref = run.requested_ref or repository.default_ref
    credential = resolve_credential(session, key_provider, environment, repository)
    workdir = Path(tempfile.mkdtemp(prefix=CHECKOUT_PREFIX))
    # Held until the checkout is removed, so a sweep of dead workers' leftovers skips it.
    lock = contextlib.ExitStack()
    try:
        lock.enter_context(hold_checkout(workdir))
        fetched = fetch_commit(
            kind=repository.kind,
            url=repository.url,
            ref=ref,
            credential=credential,
            known_hosts=repository.ssh_known_hosts,
            workdir=workdir,
            limits=limits,
            repo_roots=settings.repo_roots,
            deadline=deadline,
            ca_bundle=settings.ca_bundle,
            https_proxy=settings.https_proxy,
            no_proxy=settings.no_proxy,
            repository_name=repository_name,
        )
        credential = None
        run.resolved_sha = fetched.sha
        session.commit()
        if ci_scope is not None:
            service.check_ci_commit(principal, ci_scope, kind, fetched.sha)
        checkout = discover(fetched.tree, limits=limits)
        project = parse_project_config(checkout.config_text or "", limits=limits)
        config = project.resolved_for(environment_name)
        assets = parse_assets(checkout.assets, defaults=config.defaults, limits=limits)
        checks = parse_checks(
            checkout.checks, limits=limits, default_on_failure=config.checks.on_failure
        )

        def check_time() -> None:
            """Stop between translation files once the deploy is past its deadline.

            Raises
            ------
            GitError
                When the deadline has passed.
            """
            if clock() > deadline:
                raise GitError(_TIME_LIMIT_MESSAGE)

        translations, translation_problems = parse_translations(
            checkout.translations, limits=limits, check_time=check_time
        )
        graph = build_graph(assets)
        resolved, reference_issues = service.resolve_model_references(
            principal, environment_name, repository_id, assets
        )
        generated = generated_checks(assets, resolved, default_on_failure=config.checks.on_failure)
        every_check = [*checks, *generated]
        has_python = any(asset.kind == "python" for asset in assets)
        sources = KnownSources(registered_indexes(session), settings.index_url)
        used = used_indexes(checkout.lock, sources) if has_python else ()
        issues = (
            validate_project(assets, config)
            + job_issues(
                config,
                assets,
                graph,
                config_text=normalize_source(checkout.config_text or ""),
                environment=environment_name,
            )
            + run_policy_issues(assets, settings.caps)
            + (
                _python_issues(
                    checkout.pyproject_text,
                    checkout.lock,
                    settings.python,
                    sources,
                    task_interpreter(project.python),
                    project.python,
                )
                if has_python
                else []
            )
            + _credential_issues(session, key_provider, environment_id, environment_name, used)
            + _connection_issues(session, environment_id, environment_name, assets)
            + service.validate_against_environment(
                principal, environment_name, repository_id, assets, graph
            )
            + check_issues(every_check, assets, graph, limits=limits)
            + generated_name_issues(checks, generated)
            + service.validate_checks_against_environment(
                principal, environment_name, repository_id, checks, assets
            )
            + project_model_issues(assets)
            + translation_problems
            + translation_issues(project, assets, translations)
            + reference_issues
            + layer_issues(project.layers, assets)
        )
        if issues:
            raise ProjectValidationError(issues)
        if clock() > deadline:
            raise GitError(_TIME_LIMIT_MESSAGE)
        manifest = build_manifest(
            assets,
            graph,
            config,
            caps=settings.caps,
            python=project.python,
            lock_sha256=lock_digest(checkout.lock) if checkout.lock is not None else None,
            package_indexes=[index.name for index in used],
            checks=every_check,
            references=resolved,
            main_language=project.main_language,
            translations=translation_entries(translations),
            layers=project.layers,
            artifacts=service.artifact_inputs(
                principal, environment_name, graph, repository_id=repository_id
            ),
        )
        current = service.current_job_version(environment_id, repository_id)
        diff = dataclasses.replace(
            diff_manifests(
                current.manifest_json if current is not None else None,
                manifest,
                existing_unowned=service.existing_unowned(
                    principal, environment_name, manifest_tables(manifest)
                ),
            ),
            hints=deploy_hints(assets)
            + coverage_hints(project, assets, translations)
            + layer_hints(project.layers, assets),
        )
        if kind is DeployKind.DRY_RUN:
            service.record_dry_run(
                principal,
                run,
                diff=diff,
                worker_id=worker_id,
                checks=checks_listing(every_check) or None,
                manifest=manifest,
            )
            return
        existing = session.execute(
            select(JobVersion).where(
                JobVersion.repository_id == repository_id,
                JobVersion.environment_id == environment_id,
                JobVersion.commit_sha == fetched.sha,
            )
        ).scalar_one_or_none()
        uploaded: _Upload | None = None
        code_bytes: int | None = None
        if existing is not None:
            # record_deploy reuses the row and discards any new digest, so the
            # stored tarball must not be replaced under the old one.
            code_uri, code_sha256 = existing.code_uri, existing.code_sha256
            _restore_if_missing(
                session,
                key_provider,
                environment_name,
                fetched.archive,
                path=code_path(repository_name, fetched.sha, code_sha256),
                expected_uri=code_uri,
                storage_root=storage_root,
                code_sha256=code_sha256,
            )
        else:
            tarball = gzip.compress(fetched.archive.read_bytes(), mtime=0)
            code_sha256 = hashlib.sha256(tarball).hexdigest()
            path = code_path(repository_name, fetched.sha, code_sha256)
            resolved = StorageResolver(session, key_provider).for_environment(environment_name)
            resolved.storage.write(path, tarball)
            code_bytes = len(tarball)
            code_uri = f"{storage_root.rstrip('/')}/{path}"
            uploaded = _Upload(resolved.storage, path, code_uri)
        try:
            if clock() > deadline:
                raise GitError(_TIME_LIMIT_MESSAGE)
            service.record_deploy(
                principal,
                run,
                manifest=manifest,
                code_uri=code_uri,
                code_sha256=code_sha256,
                diff=diff,
                expected_job_version_id=current.id if current is not None else None,
                worker_id=worker_id,
                checks=checks_listing(every_check) or None,
                code_bytes=code_bytes,
            )
            if uploaded is not None:
                _restore_if_missing(
                    session,
                    key_provider,
                    environment_name,
                    fetched.archive,
                    path=uploaded.path,
                    expected_uri=uploaded.uri,
                    storage_root=storage_root,
                    code_sha256=code_sha256,
                )
        finally:
            if uploaded is not None:
                _discard_losing_upload(
                    session, uploaded, repository_id, environment_id, fetched.sha
                )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        lock.close()
