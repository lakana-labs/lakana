"""Deploy runs, project versions and the deployment pointer.

The API side only enqueues and reads; the worker side (`lakana_worker.deploy`)
claims a run, validates a checkout against the environment and records the
outcome. Every pointer move is a compare-and-swap on the value the worker
observed, so the loser of a race gets `ConflictError` and nothing moves.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sqlglot
from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError
from sqlglot import exp

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, CiScope, PrincipalRef, Securable
from lakana_core.catalog.columns import stored_columns
from lakana_core.catalog.descriptions import sync_project_descriptions
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.catalog.model_metadata import clear_removed_models
from lakana_core.ci_oidc.exchange import ci_scope_for_run
from lakana_core.ci_oidc.scope import (
    ALREADY_USED_MESSAGE,
    check_ci_commit,
    check_ci_request,
    ci_refusal,
    refuse_ci_token,
    use_ci_token,
)
from lakana_core.connections.service import missing_connections
from lakana_core.db import db_now
from lakana_core.errors import ConflictError, NotFoundError, ValidationError
from lakana_core.identifiers import TableRef, shown
from lakana_core.jobs.audit import start_detail
from lakana_core.jobs.cron import next_fire_after
from lakana_core.models import (
    AuditEventType,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    EnvironmentDeployment,
    JobFire,
    JobSchedule,
    JobVersion,
    Principal,
    PrincipalType,
    Repository,
    Run,
    Secret,
    SecretUseKind,
    Table,
    TableType,
)
from lakana_core.models.run import ACTIVE_RUN_STATUSES
from lakana_core.pagination import decode_cursor, encode_cursor
from lakana_core.projects.asset_model import KEY_NONE, ResolvedReference, key_columns
from lakana_core.projects.checks import ParsedCheck, check_sql_references, may_read_every
from lakana_core.projects.dag import ProjectGraph
from lakana_core.projects.deploy_jobs import (
    OUTCOME_PENDING,
    DeployJobStart,
    job_starts_view,
    pending_job_starts,
)
from lakana_core.projects.diff import DeployDiff, diff_manifests
from lakana_core.projects.git_input import is_full_sha, validate_ref
from lakana_core.projects.issues import ProjectIssue, ProjectValidationError
from lakana_core.projects.lookups import deployment_row, environment_by_name, repository_by_name
from lakana_core.projects.manifest import (
    entry_model,
    entry_output,
    entry_write,
    job_notify,
    job_schedule,
    manifest_assets,
    manifest_jobs,
    manifest_tables,
    read_entry_connections,
)
from lakana_core.projects.model import TargetInfo, resolve_references
from lakana_core.projects.parse import ParsedAsset
from lakana_core.runs.kept import kept_tables
from lakana_core.runs.pools import PIPELINE
from lakana_core.runs.waiting import (
    WaitReason,
    ahead_kind,
    alive_workers,
    count_ahead,
    query_wait_reason,
    with_work,
)
from lakana_core.secrets.uses import SecretUse, bind_command, describe_use

CONFLICT_MESSAGE = "Another deploy or rollback moved this environment's deployment first; retry."

_GONE_MESSAGE = "The repository or environment of this deploy no longer exists."

DEPLOY_ENDED_MESSAGE = (
    "Deploy run {id} is no longer this worker's running run, so nothing was changed."
)
"""Why a finishing call is refused: the run is no longer `running` under the caller's worker id.

Neutral on purpose: the run may have succeeded, been ended by the scheduler because its worker
was lost, or belong to another worker. `{id}` is the run's id. Raised as `ConflictError`; the
worker logs it and records nothing."""

UNKNOWN_CONNECTION_MESSAGE = (
    "Asset `{asset}` declares connection `{name}`, which environment `{environment}` does not "
    "have. Create it: `lakana connection create {name} --env {environment} --kind <kind> …` "
    "(see `lakana connection create --help`)."
)
"""Why a deploy or rollback stops: an asset declares a connection its environment lacks."""


@dataclass(frozen=True)
class DeclaredConnections:
    """The connections one asset declares, and where an issue about them points.

    Parameters
    ----------
    asset
        The asset's dotted table name.
    path
        Its repository-relative file.
    line
        The decorator's line, or `None` when only a manifest entry is at hand.
    names
        The connection names, as declared.
    """

    asset: str
    path: str
    line: int | None
    names: tuple[str, ...]


def connection_issues(
    session: Session,
    environment_id: uuid.UUID,
    environment_name: str,
    declared: Iterable[DeclaredConnections],
    *,
    lock: bool,
) -> list[ProjectIssue]:
    """Name every declared connection the environment does not have, one issue each.

    One query reads every name the assets declare.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    environment_name
        Its name, for the message.
    declared
        What each asset declares.
    lock
        Read the found rows ``FOR SHARE`` until the transaction ends (`missing_connections`),
        so a concurrent delete waits for this transaction or comes first and is seen.

    Returns
    -------
    list of ProjectIssue
        Sorted by asset, the names in the order declared; empty when every one exists.
    """
    assets = sorted(declared, key=lambda item: item.asset)
    wanted = sorted({name for item in assets for name in item.names})
    if not wanted:
        return []
    missing = set(missing_connections(session, environment_id, wanted, lock=lock))
    return [
        ProjectIssue(
            item.path,
            item.line,
            UNKNOWN_CONNECTION_MESSAGE.format(
                asset=item.asset, name=name, environment=environment_name
            ),
        )
        for item in assets
        for name in item.names
        if name in missing
    ]


UNREADABLE_CONNECTIONS_MESSAGE = (
    "Asset `{asset}`: the connections this project version declares cannot be read, so they "
    "cannot be checked against environment `{environment}`. Deploy the repository again to "
    "record a new version."
)
"""Why a deploy or rollback stops: a manifest entry's `connections` is malformed."""


def _manifest_connections(
    manifest: object, environment_name: str
) -> tuple[list[DeclaredConnections], list[ProjectIssue]]:
    """Read what each asset of a manifest declares.

    Parameters
    ----------
    manifest
        A manifest; untrusted when stored (a rollback's target).
    environment_name
        For the message about an entry that cannot be read.

    Returns
    -------
    tuple
        One `DeclaredConnections` per asset that declares any (the path is ``""`` when the
        entry has none), and one issue per entry whose `connections` cannot be read: such an
        entry is refused, never skipped.
    """
    found: list[DeclaredConnections] = []
    unreadable: list[ProjectIssue] = []
    for table, entry in sorted(manifest_assets(manifest).items()):
        read = read_entry_connections(entry)
        path = entry.get("path")
        shown_path = path if isinstance(path, str) else ""
        if not read.readable:
            unreadable.append(
                ProjectIssue(
                    shown_path,
                    None,
                    UNREADABLE_CONNECTIONS_MESSAGE.format(
                        asset=shown(table), environment=environment_name
                    ),
                )
            )
        elif read.names:
            found.append(DeclaredConnections(table, shown_path, None, read.names))
    return found, unreadable


_KIND_PHRASES = {
    TableType.ICEBERG: "a table",
    TableType.VIEW: "a view",
    TableType.EXTERNAL: "an external table",
}
"""How a clash message names the existing object that an artifact asset would collide with."""

ARTIFACT_PARAMETER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
"""A parameter that reads an artifact: an ASCII identifier of at most 128 characters.

The task stores the copy of an artifact in a directory named like the parameter, and the
harness (`lakana.runner`, which keeps the same pattern) refuses any other name with exit
status 2. A parameter that reads a table may be any Python name."""

_HIDDEN_KEYS = ("sql", "normalized_sql", "references", "lineage", "inputs", "input_kinds")


def _file_references(asset: ParsedAsset) -> set[str]:
    """Find the references of an asset that read a file, not a catalog table.

    A Python asset reads only declared tables, so it has none; for a SQL asset see
    `_sql_file_references`.

    Parameters
    ----------
    asset
        The parsed asset.

    Returns
    -------
    set of str
        The references that are not catalog tables.
    """
    if asset.kind == "python":
        return set()
    return _sql_file_references(asset.sql, asset.references)


def _sql_file_references(sql: str, references: Iterable[str]) -> set[str]:
    """Find the references of a query that read a file, not a catalog table.

    ``FROM 'file.csv'``, ``FROM "a.b"`` (one quoted component containing a
    dot) and ``read_csv('x')`` all reach the parser's reference list as a
    dotted-looking name; only the AST tells them from ``file.csv`` the table.

    Parameters
    ----------
    sql
        The query.
    references
        The names the parser listed for it.

    Returns
    -------
    set of str
        The references that are not catalog tables. If the SQL cannot be
        parsed again, every reference is treated that way.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect="duckdb")
    except sqlglot.errors.SqlglotError:
        return set(references)
    found: set[str] = set()
    for table in tree.find_all(exp.Table):
        parts = [
            part for part in (table.args.get("catalog"), table.args.get("db"), table.this) if part
        ]
        if any(not isinstance(part, exp.Identifier) or "." in part.name for part in parts):
            found.add(".".join(part.name for part in parts))
    return found


def _artifact_parameter_issues(asset: ParsedAsset, artifacts: set[str]) -> list[str]:
    """Find the parameters that read an artifact under a name the task cannot store it by.

    Parameters
    ----------
    asset
        A Python asset.
    artifacts
        Every name known to be an artifact.

    Returns
    -------
    list of str
        One message per parameter that is not an ASCII identifier of at most 128
        characters, then one per group of parameters that differ only in case.
    """
    reading = [(parameter, table) for parameter, table in asset.inputs if table in artifacts]
    messages = [
        f"parameter {parameter!r} reads the artifact `{table}`, which the task stores in a "
        "directory of that name: rename the parameter, in the function and in inputs, to an "
        "ASCII identifier of at most 128 characters (letters, digits, underscores)"
        for parameter, table in reading
        if ARTIFACT_PARAMETER.fullmatch(parameter) is None
    ]
    folded: dict[str, list[str]] = {}
    for parameter, _table in reading:
        folded.setdefault(parameter.lower(), []).append(parameter)
    for group in folded.values():
        if len(group) > 1:
            names = " and ".join(repr(name) for name in sorted(group))
            messages.append(
                f"parameters {names} read artifacts and differ only in case, so a file system "
                "may store both in one directory: rename one, in the function and in inputs"
            )
    return messages


@dataclass
class _WarningReads:
    """What the warnings of several runs read, read once per environment and repository.

    Parameters
    ----------
    kept
        Environment id to its kept tables (`kept_tables`).
    current
        Repository id to the tables its current deployment builds; `None` when it has none.
    """

    kept: dict[uuid.UUID, dict[str, uuid.UUID]] = field(default_factory=dict)
    current: dict[uuid.UUID, set[str] | None] = field(default_factory=dict)


@dataclass(frozen=True)
class DeployRunView:
    """A deploy run as the API shows it.

    Parameters
    ----------
    id
        The run's id.
    environment
        Environment name.
    repository
        Repository name.
    kind
        Deploy, dry run or rollback.
    status
        Lifecycle state.
    requested_ref
        The ref or SHA asked for.
    resolved_sha
        The commit the worker fetched.
    rollback_to_job_version_id
        A rollback's target.
    error_message
        Why it failed.
    diff
        The stored diff.
    job_version_id
        The project version it made current.
    principal_id
        Who submitted it.
    created_at
        When it was queued.
    started_at
        When the worker claimed it.
    finished_at
        When it finished.
    hidden_adopted
        Adopted tables dropped from `diff` because the reader may not read them.
    checks
        The checks a deploy or dry run found, table to ``[{"check", "on_failure"}]``; `None`
        for a run without checks.
    warnings
        Things the deploy leaves for its owner to act on, as sentences: today, a removed asset
        whose table is kept after a failed check, read at the time of the request; then the
        ones the deploy stored when it changed the job schedules.
    waiting_for
        Why a `QUEUED` run has not started (`DeployService.get` only; `None` for any other
        status and in a listing).
    job_starts
        The jobs with ``on_deploy: true`` a successful deploy started, by name: the run, the
        start that waits, or why a job was not started (`lakana_core.projects.deploy_jobs`).
        Empty for a run that starts no job.
    """

    id: uuid.UUID
    environment: str
    repository: str
    kind: DeployKind
    status: DeployStatus
    requested_ref: str | None
    resolved_sha: str | None
    rollback_to_job_version_id: uuid.UUID | None
    error_message: str | None
    diff: dict[str, list[str]] | None
    job_version_id: uuid.UUID | None
    principal_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    hidden_adopted: int = 0
    checks: dict[str, list[dict[str, str]]] | None = None
    warnings: list[str] = field(default_factory=list)
    waiting_for: WaitReason | None = None
    job_starts: tuple[DeployJobStart, ...] = ()


@dataclass(frozen=True)
class DeploymentView:
    """The current project version of one repository in an environment.

    Parameters
    ----------
    repository
        Repository name.
    job_version_id
        The current project version.
    commit_sha
        Its commit.
    updated_at
        When the pointer last moved.
    run_as
        The bound run principal's name, or `None`.
    """

    repository: str
    job_version_id: uuid.UUID
    commit_sha: str
    updated_at: datetime
    run_as: str | None = None


@dataclass(frozen=True)
class JobVersionView:
    """A project version with its manifest filtered for the reader.

    Parameters
    ----------
    id
        The project version's id.
    environment
        Environment name.
    repository
        Repository name.
    commit_sha
        The commit.
    code_uri
        Where the code tarball lives.
    code_sha256
        The tarball's SHA-256.
    deployed_by
        Who first deployed it.
    created_at
        When it was first recorded.
    manifest
        The manifest, with withheld parts removed (see `DeployService`).
    """

    id: uuid.UUID
    environment: str
    repository: str
    commit_sha: str
    code_uri: str
    code_sha256: str
    deployed_by: uuid.UUID | None
    created_at: datetime
    manifest: dict[str, Any]


@dataclass(frozen=True)
class OrphanView:
    """A table an earlier project version produced and the current one does not.

    Parameters
    ----------
    repository
        The repository that used to produce it.
    table
        Dotted table name.
    """

    repository: str
    table: str


@dataclass(frozen=True)
class OrphanReport:
    """Orphans visible to the reader.

    Parameters
    ----------
    orphans
        Readable orphans, sorted.
    hidden
        Orphans that exist but the reader may not read.
    """

    orphans: list[OrphanView]
    hidden: int


class DeploymentMoved(ConflictError):
    """The deployment's pointer moved (a deploy, rollback or undeploy) after it was read.

    A `ConflictError`, so the API answers 409 as it did; a caller that can read the pointer
    again catches this one and does.
    """


class DeployService:
    """Deploy runs and their records.

    Parameters
    ----------
    session
        Database session; mutating methods commit.
    authz
        Authorization service.
    lease_seconds
        The run lease (`LAKANA_RUN__LEASE_SECONDS`): a worker whose heartbeat is older does not
        count as alive when a queued deploy says why it waits.
    """

    def __init__(
        self, session: Session, authz: AuthorizationService, lease_seconds: int = 60
    ) -> None:
        self._session = session
        self._authz = authz
        self._lease_seconds = lease_seconds

    # -- lookups ---------------------------------------------------------

    def _environment(self, name: str) -> Environment:
        """Look an environment up by name.

        Parameters
        ----------
        name
            Environment name.

        Returns
        -------
        Environment
            The row.

        Raises
        ------
        NotFoundError
            If none has that name.
        """
        return environment_by_name(self._session, name)

    def _repository(self, name: str) -> Repository:
        """Look a repository up by name.

        Parameters
        ----------
        name
            Repository name.

        Returns
        -------
        Repository
            The row.

        Raises
        ------
        NotFoundError
            If none has that name.
        """
        return repository_by_name(self._session, name)

    def _view(
        self,
        run: DeployRun,
        reader: PrincipalRef | None = None,
        reads: _WarningReads | None = None,
    ) -> DeployRunView:
        """Build the API view of a run.

        Parameters
        ----------
        run
            The row.
        reader
            When given, ``adopted`` tables this principal may not read are
            dropped from the diff and counted in `hidden_adopted`: adopting a
            table names one that exists outside any repository, which a
            READ_ENVIRONMENT reader is not entitled to learn.
        reads
            What earlier calls of a listing already read for the warnings; a new one when
            `None`.

        Returns
        -------
        DeployRunView
            With environment and repository names.

        Raises
        ------
        NotFoundError
            If the run's environment or repository was deleted meanwhile.
        """
        env_name = self._session.execute(
            select(Environment.name).where(Environment.id == run.environment_id)
        ).scalar_one_or_none()
        if env_name is None:
            raise NotFoundError("The environment of this deploy no longer exists.")
        repo_name = self._session.execute(
            select(Repository.name).where(Repository.id == run.repository_id)
        ).scalar_one_or_none()
        if repo_name is None:
            raise NotFoundError("The repository of this deploy no longer exists.")
        diff = run.diff_json
        hidden_adopted = 0
        if reader is not None and diff is not None:
            adopted = diff.get("adopted", [])
            visible = [t for t in adopted if self._may_read(reader, env_name, t)]
            hidden_adopted = len(adopted) - len(visible)
            diff = {**diff, "adopted": visible}
        return DeployRunView(
            id=run.id,
            environment=env_name,
            repository=repo_name,
            kind=run.kind,
            status=run.status,
            requested_ref=run.requested_ref,
            resolved_sha=run.resolved_sha,
            rollback_to_job_version_id=run.rollback_to_job_version_id,
            error_message=run.error_message,
            diff=diff,
            job_version_id=run.job_version_id,
            principal_id=run.principal_id,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            hidden_adopted=hidden_adopted,
            checks=run.checks_json,
            warnings=[
                *self._kept_removed_warnings(run, env_name, reader, diff, reads or _WarningReads()),
                *(run.warnings_json or []),
            ],
            job_starts=job_starts_view(
                run.job_starts_json,
                lost=self._starts_lost(run),
                repository=repo_name,
                environment=env_name,
            ),
        )

    def _starts_lost(self, run: DeployRun) -> bool:
        """Say whether a start the deploy left pending will never be made.

        The worker starts the jobs right after the deploy's transaction. A start still pending a
        run lease after the deploy finished was not made: the worker stopped in between. Reads the
        database's clock only for a run with a pending start.

        Parameters
        ----------
        run
            The deploy run.

        Returns
        -------
        bool
            Whether pending starts are to be shown as lost.
        """
        stored = run.job_starts_json
        if run.finished_at is None or not isinstance(stored, list):
            return False
        if not any(
            isinstance(entry, dict) and entry.get("outcome") == OUTCOME_PENDING for entry in stored
        ):
            return False
        finished = run.finished_at
        if finished.tzinfo is None:  # pragma: no cover -- UTCDateTime reads aware times
            finished = finished.replace(tzinfo=UTC)
        return db_now(self._session) - finished > timedelta(seconds=self._lease_seconds)

    def _kept_removed_warnings(
        self,
        run: DeployRun,
        env_name: str,
        reader: PrincipalRef | None,
        diff: dict[str, list[str]] | None,
        reads: _WarningReads,
    ) -> list[str]:
        """Warn about each removed asset whose table carries the note of a failed ``keep`` check.

        A kept table is cleared by a run of the project that builds it. Once the deploy removes
        the asset, nothing builds the table, and tasks that read it stay refused. The warning
        of a deploy or rollback stops when the repository's current deployment builds the table
        again; a dry run always warns, because it moves no pointer.

        Parameters
        ----------
        run
            The row.
        env_name
            Its environment's name.
        reader
            The principal reading; the tables it may not read are not named. `None` for a view
            that carries no diff to the caller.
        diff
            The run's diff as the reader sees it.
        reads
            Filled with what is read, so that a listing reads it once.

        Returns
        -------
        list of str
            One sentence per table, sorted; empty when the diff removes none.
        """
        removed = (diff or {}).get("removed", [])
        if reader is None or not removed:
            return []
        if run.environment_id not in reads.kept:
            reads.kept[run.environment_id] = kept_tables(self._session, run.environment_id)
        kept = reads.kept[run.environment_id]
        built: set[str] = set()
        if run.kind is not DeployKind.DRY_RUN:
            # A dry run moves no pointer: the current deployment still builds what it removes.
            if run.repository_id not in reads.current:
                version = self.current_job_version(run.environment_id, run.repository_id)
                reads.current[run.repository_id] = (
                    None if version is None else set(manifest_assets(version.manifest_json))
                )
            built = reads.current[run.repository_id] or set()
        return [
            f"this deploy removes the asset of `{table}`, which is kept after its checks failed or "
            "could not run, or set back to data whose checks are not known; add it back and build "
            "it, or drop the table"
            for table in sorted(removed)
            if table in kept and table not in built and self._may_read(reader, env_name, table)
        ]

    def current_job_version(
        self, environment_id: uuid.UUID, repository_id: uuid.UUID
    ) -> JobVersion | None:
        """Return the repository's current project version in the environment.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository.

        Returns
        -------
        JobVersion or None
            The pointer's target, or `None` before the first deploy.
        """
        return self._session.execute(
            select(JobVersion)
            .join(
                EnvironmentDeployment, EnvironmentDeployment.current_job_version_id == JobVersion.id
            )
            .where(
                EnvironmentDeployment.environment_id == environment_id,
                EnvironmentDeployment.repository_id == repository_id,
            )
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def _previous_job_version_id(
        self, environment_id: uuid.UUID, repository_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Find the project version that was current before the present one.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository.

        Returns
        -------
        uuid.UUID or None
            The most recent succeeded deploy/rollback target that differs
            from the current pointer, or `None`.
        """
        current = self.current_job_version(environment_id, repository_id)
        if current is None:
            return None
        return self._session.execute(
            select(DeployRun.job_version_id)
            .where(
                DeployRun.environment_id == environment_id,
                DeployRun.repository_id == repository_id,
                DeployRun.status == DeployStatus.SUCCEEDED,
                DeployRun.kind.in_([DeployKind.DEPLOY, DeployKind.ROLLBACK]),
                DeployRun.job_version_id.is_not(None),
                DeployRun.job_version_id != current.id,
            )
            .order_by(DeployRun.finished_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _produced_elsewhere(
        self, environment_id: uuid.UUID, repository_id: uuid.UUID | None
    ) -> dict[str, str]:
        """Map each table another repository's current project version produces to that repository.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository to exclude, or `None` to include every repository.

        Returns
        -------
        dict
            Table → repository name.
        """
        return {
            table: owner
            for table, (owner, _) in self._produced_elsewhere_outputs(
                environment_id, repository_id
            ).items()
        }

    def _produced_elsewhere_outputs(
        self, environment_id: uuid.UUID, repository_id: uuid.UUID | None
    ) -> dict[str, tuple[str, str]]:
        """Map each table another repository's current project version produces to owner and kind.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository to exclude, or `None` to include every repository.

        Returns
        -------
        dict
            Table → (repository name, ``table`` or ``artifact``); the kind is the
            producing manifest's, told by `entry_output`.
        """
        produced: dict[str, tuple[str, str]] = {}
        for name, manifest in self._current_manifests(environment_id, repository_id):
            for table, entry in manifest_assets(manifest).items():
                produced[table] = (name, entry_output(entry))
        return produced

    def _current_manifests(
        self, environment_id: uuid.UUID, repository_id: uuid.UUID | None
    ) -> list[tuple[str, object]]:
        """Read the current manifest of every other repository deployed to an environment.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository to exclude, or `None` to include every repository.

        Returns
        -------
        list of tuple
            ``(repository name, manifest)``, ordered by repository name.
        """
        query = (
            select(Repository.name, JobVersion.manifest_json)
            .join(EnvironmentDeployment, EnvironmentDeployment.repository_id == Repository.id)
            .join(JobVersion, JobVersion.id == EnvironmentDeployment.current_job_version_id)
            .where(EnvironmentDeployment.environment_id == environment_id)
            .order_by(Repository.name)
        )
        if repository_id is not None:
            query = query.where(EnvironmentDeployment.repository_id != repository_id)
        return [(name, manifest) for name, manifest in self._session.execute(query)]

    def _elsewhere_entries(
        self, environment_id: uuid.UUID, repository_id: uuid.UUID
    ) -> dict[str, Mapping[str, Any]]:
        """Map each table another repository's current project version produces to its entry.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository being deployed, which is left out.

        Returns
        -------
        dict
            Table → its manifest entry; the first repository by name wins when two claim one
            table (a deploy refuses that case elsewhere).
        """
        entries: dict[str, Mapping[str, Any]] = {}
        for _, manifest in self._current_manifests(environment_id, repository_id):
            for table, entry in manifest_assets(manifest).items():
                entries.setdefault(table, entry)
        return entries

    def _catalog_columns(self, environment: str, dotted: str) -> tuple[str, ...] | None:
        """Read a table's stored column names.

        Call it only for a table the deployer may read (`_readable_table`).

        Parameters
        ----------
        environment
            Environment name.
        dotted
            Dotted table name.

        Returns
        -------
        tuple of str or None
            The names in schema order; `None` when the table has none stored.
        """
        stored = stored_columns(self._session, environment, dotted)
        return tuple(stored) if stored else None

    def _lock_ownership(self, environment_id: uuid.UUID) -> None:
        """Serialise table-ownership decisions in one environment until the transaction ends.

        Validation reads who produces which table long before a deploy records,
        so two repositories deploying the same new table can both pass it. The
        recording transaction therefore takes this lock and re-checks ownership
        (`_ownership_clashes`); the second to arrive waits, then sees the first's
        committed pointer. A PostgreSQL transaction-scoped advisory lock keyed
        on the environment.

        It is taken before the ownership read, not as the transaction's very
        first statement (loading the expired run row comes earlier), which is
        sound under PostgreSQL's default READ COMMITTED: every statement sees
        what had committed when it started, so the read after the lock sees
        the previous holder's pointer. A REPEATABLE READ session would not.
        On SQLite nothing is serialised here and a race between two
        repositories is not prevented; local mode runs one worker that
        deploys inline, so none arises.

        Parameters
        ----------
        environment_id
            The environment.
        """
        if self._session.get_bind().dialect.name != "postgresql":
            return
        key = int.from_bytes(environment_id.bytes[:8], "big", signed=True)
        self._session.execute(select(func.pg_advisory_xact_lock(key)))

    def _claim_tables(
        self,
        environment_id: uuid.UUID,
        repository_id: uuid.UUID,
        tables: Iterable[str],
        environment_name: str,
    ) -> None:
        """Take the ownership lock and refuse tables another repository produces.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository that wants to produce `tables`.
        tables
            Dotted table names.
        environment_name
            For the message.

        Raises
        ------
        ValidationError
            If another repository's current project version produces one of them; the
            transaction is rolled back first.
        """
        self._lock_ownership(environment_id)
        clashes = self._ownership_clashes(environment_id, repository_id, tables, environment_name)
        if clashes:
            self._session.rollback()
            raise ValidationError("; ".join(clashes))

    def _require_connections(
        self, environment_id: uuid.UUID, environment_name: str, manifest: object
    ) -> None:
        """Refuse a manifest that declares a connection the environment does not have.

        Validation checked the same long before the record; a connection deleted in between
        is caught here. The found rows stay locked ``FOR SHARE`` until the transaction ends,
        so a delete of one waits for this deploy and then sees the version it made current.

        Parameters
        ----------
        environment_id
            The environment.
        environment_name
            Its name, for the message.
        manifest
            The manifest about to become current.

        Raises
        ------
        ProjectValidationError
            Naming each missing connection, and each asset whose stored connections cannot be
            read; the transaction is rolled back first.
        """
        declared, issues = _manifest_connections(manifest, environment_name)
        issues += connection_issues(
            self._session, environment_id, environment_name, declared, lock=True
        )
        if issues:
            self._session.rollback()
            raise ProjectValidationError(issues)

    def _ownership_clashes(
        self,
        environment_id: uuid.UUID,
        repository_id: uuid.UUID,
        tables: Iterable[str],
        environment_name: str,
    ) -> list[str]:
        """Name every table in `tables` that another repository currently produces.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository that wants to produce `tables`.
        tables
            Dotted table names.
        environment_name
            For the messages.

        Returns
        -------
        list of str
            One message per clash, sorted by table; empty when none.
        """
        taken = self._produced_elsewhere(environment_id, repository_id)
        return [
            f"table {table!r} is produced by repository {taken[table]!r} in {environment_name}"
            for table in sorted(set(tables) & taken.keys())
        ]

    def _readable_table(self, p: PrincipalRef, environment: str, dotted: str) -> bool:
        """Report whether a table exists and `p` may read it.

        Parameters
        ----------
        p
            The principal.
        environment
            Environment name.
        dotted
            Dotted table name.

        Returns
        -------
        bool
            `False` for a missing, unreadable or malformed name — the three
            are indistinguishable to the caller on purpose.
        """
        try:
            ref = TableRef.from_dotted(environment, dotted)
        except ValidationError:
            return False
        if resolve_table_id(self._session, environment, ref.namespace.joined(), ref.name) is None:
            return False
        return self._authz.permits(
            p, Action.READ_TABLE, Securable.table_scope(environment, ref.namespace, ref.name)
        )

    def _readable_type(self, p: PrincipalRef, environment: str, dotted: str) -> TableType | None:
        """Find the type of an existing object `p` may read.

        Goes through `_readable_table`, so a missing, unreadable or malformed name
        answer alike.

        Parameters
        ----------
        p
            The principal.
        environment
            Environment name.
        dotted
            Dotted name.

        Returns
        -------
        TableType or None
            The type, or `None` when the object is missing, unreadable or malformed.
        """
        if not self._readable_table(p, environment, dotted):
            return None
        ref = TableRef.from_dotted(environment, dotted)
        table_id = resolve_table_id(self._session, environment, ref.namespace.joined(), ref.name)
        row = self._session.get(Table, table_id) if table_id is not None else None
        return row.table_type if row is not None else None

    def artifact_inputs(
        self,
        p: PrincipalRef,
        environment: str,
        graph: ProjectGraph,
        *,
        repository_id: uuid.UUID | None = None,
    ) -> frozenset[str]:
        """Find the external inputs that are artifacts.

        Parameters
        ----------
        p
            The deploying principal.
        environment
            Environment name.
        graph
            The project's DAG.
        repository_id
            The repository being deployed, whose own current deploy is not
            "another repository"; `None` counts every repository.

        Returns
        -------
        frozenset of str
            External inputs that are artifacts. A catalog row `p` may read decides.
            Otherwise, an input that another repository's current deploy in this
            environment produces is an artifact if that manifest says so, whether
            its row is missing (the producer has not run yet) or unreadable. A name
            that no repository produces and `p` may not read is left out, as if it
            did not exist.
        """
        elsewhere = self._produced_elsewhere_outputs(
            self._environment(environment).id, repository_id
        )
        found = set()
        for name in graph.external_inputs:
            existing = self._readable_type(p, environment, name)
            if existing is not None:
                is_artifact = existing is TableType.ARTIFACT
            else:
                is_artifact = elsewhere.get(name, ("", "table"))[1] == "artifact"
            if is_artifact:
                found.add(name)
        return frozenset(found)

    def _may_read(self, p: PrincipalRef, environment: str, dotted: str) -> bool:
        """Report whether `p` would be allowed to read `dotted`, existing or not.

        Parameters
        ----------
        p
            The principal.
        environment
            Environment name.
        dotted
            Dotted table name.

        Returns
        -------
        bool
            Whether a READ_TABLE grant covers that name.
        """
        return may_read_every(self._authz, p, environment, [dotted])

    # -- API side --------------------------------------------------------

    def submit(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        repository: str,
        kind: DeployKind,
        ref: str | None = None,
        to_sha: str | None = None,
        to_previous: bool = False,
    ) -> DeployRunView:
        """Queue a deploy, dry run or rollback.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Target environment.
        repository
            Registered repository name.
        kind
            What to do.
        ref
            Deploy/dry run only: the ref or SHA; `None` uses the
            repository's default ref, or the remote's ``HEAD``.
        to_sha
            Rollback only: a full SHA that already has a project version here.
        to_previous
            Rollback only: return to the project version current before this one.

        Returns
        -------
        DeployRunView
            The queued run.

        Raises
        ------
        PermissionDenied
            Without `DEPLOY` on the environment; for a CI token (`p.ci_scope`), also for
            anything but its one submission of its repository, of a kind its rule allows,
            never a rollback, and for a deploy only of its signed commit
            (`lakana_core.ci_oidc.scope`).
        NotFoundError
            If the environment or repository does not exist.
        ValidationError
            If the request's shape is wrong or a rollback has no target.
        """
        self._authz.authorize(p, Action.DEPLOY, Securable.environment_scope(environment))
        env = self._environment(environment)
        repo = self._repository(repository)
        ci = p.ci_scope
        if ci is not None:
            ref = check_ci_request(
                self._authz,
                p,
                ci,
                environment=environment,
                environment_id=env.id,
                repository=repository,
                repository_id=repo.id,
                kind=kind,
                ref=ref,
            )
        target: uuid.UUID | None = None
        requested = ref
        if kind is DeployKind.ROLLBACK:
            if ref is not None:
                raise ValidationError("A rollback takes no ref; use to_sha or to_previous.")
            if (to_sha is None) == (not to_previous):
                raise ValidationError("A rollback needs exactly one of to_sha or to_previous.")
            if to_sha is not None:
                sha = validate_ref(to_sha)
                if not is_full_sha(sha):
                    raise ValidationError("to_sha must be a full 40-character commit SHA.")
                target = self._session.execute(
                    select(JobVersion.id).where(
                        JobVersion.repository_id == repo.id,
                        JobVersion.environment_id == env.id,
                        JobVersion.commit_sha == sha,
                    )
                ).scalar_one_or_none()
                if target is None:
                    raise ValidationError(
                        f"{sha} has never been deployed to {environment}; deploy it instead."
                    )
                requested = sha
            else:
                target = self._previous_job_version_id(env.id, repo.id)
                if target is None:
                    raise ValidationError(
                        f"{repository} has no previous deployment in {environment}."
                    )
        else:
            if to_sha is not None or to_previous:
                raise ValidationError("to_sha and to_previous are only for a rollback.")
            if ref is not None:
                requested = validate_ref(ref)
        run = DeployRun(
            id=uuid.uuid4(),
            environment_id=env.id,
            repository_id=repo.id,
            principal_id=p.id,
            kind=kind,
            requested_ref=requested,
            rollback_to_job_version_id=target,
            status=DeployStatus.QUEUED,
            ci_token_id=None if ci is None else ci.token_id,
        )
        self._session.add(run)
        if ci is not None and not use_ci_token(self._session, ci, datetime.now(UTC)):
            self._session.rollback()
            ci_refusal(self._authz, p, environment, ALREADY_USED_MESSAGE)
        self._session.commit()
        return self._view(run)

    def _authorize_read(self, p: PrincipalRef, environment: str) -> Environment:
        """Authorize READ_ENVIRONMENT and return the environment.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.

        Returns
        -------
        Environment
            The row.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT (checked first).
        NotFoundError
            If the environment does not exist.
        """
        self._authz.authorize(p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment))
        return self._environment(environment)

    def get(self, p: PrincipalRef, environment: str, run_id: uuid.UUID) -> DeployRunView:
        """Show one deploy run.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        run_id
            The run's id.

        Returns
        -------
        DeployRunView
            The run.

        Raises
        ------
        PermissionDenied
            Without DEPLOY or READ_ENVIRONMENT (a CI principal that may submit a
            run may also poll it).
        NotFoundError
            If no such run exists in this environment, or, for a CI token, if the token did
            not queue it (one answer, whether or not the run exists).
        """
        scope = Securable.environment_scope(environment)
        if self._authz.permits(p, Action.DEPLOY, scope):
            self._authz.authorize(p, Action.DEPLOY, scope)
            env = self._environment(environment)
        else:
            env = self._authorize_read(p, environment)
        run = self._session.execute(
            select(DeployRun).where(DeployRun.id == run_id, DeployRun.environment_id == env.id)
        ).scalar_one_or_none()
        if run is None or (p.ci_scope is not None and run.ci_token_id != p.ci_scope.token_id):
            raise NotFoundError(f"No deploy {run_id} in {environment}.")
        return dataclasses.replace(self._view(run, p), waiting_for=self.waiting_for(run))

    def waiting_for(self, run: DeployRun) -> WaitReason | None:
        """Say why a run has not started, when it is still `QUEUED`.

        Computed when asked and never stored, by the same rules as a queued query
        (`lakana_core.runs.waiting`). A deploy is pipeline work and is never capped. Grants
        nothing and reads no other run's data, only counts: the workers and what they take and
        carry, and the queued query and deploy runs ahead of `run` in every environment. For a
        run in any other status it issues no statement.

        Parameters
        ----------
        run
            A deploy run the caller may see.

        Returns
        -------
        WaitReason or None
            The reason, or `None` when the run is not `QUEUED`.
        """
        if run.status != DeployStatus.QUEUED:
            return None
        now = db_now(self._session)
        snapshot = alive_workers(self._session, now, self._lease_seconds)
        # A worker that supervises a task claims no deploy (`poller.poll_once`), so the counts
        # are those of a task: busy also for the holder of a task lease.
        counts = snapshot.for_task()
        if counts.alive == 0 or counts.taking == 0:
            return query_wait_reason(counts, 0, kind=PIPELINE, thing="deploy")
        snapshot = with_work(self._session, snapshot)
        counts = snapshot.for_task()
        ahead = count_ahead(self._session, run.created_at, ahead_kind(snapshot, PIPELINE))
        return query_wait_reason(counts, ahead, kind=PIPELINE, thing="deploy")

    def history(
        self, p: PrincipalRef, environment: str, *, limit: int = 100
    ) -> list[DeployRunView]:
        """List an environment's deploy history, newest first.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        limit
            Most runs returned.

        Returns
        -------
        list of DeployRunView
            Newest first.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT.
        NotFoundError
            If the environment does not exist.
        """
        return self.history_page(p, environment, limit=limit)[0]

    def history_page(
        self,
        p: PrincipalRef,
        environment: str,
        *,
        repository: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[DeployRunView], str | None]:
        """List an environment's deploy history, newest first, one page at a time.

        Runs are ordered by creation time, then id, newest first; a page continues strictly
        after the last run of the previous one.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        repository
            Only this repository's runs.
        limit
            Most runs returned.
        cursor
            The `next_cursor` of the previous page. Send the same `repository` with every page.

        Returns
        -------
        tuple of (list of DeployRunView, str or None)
            The page, and the cursor of the next one, or `None` on the last.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT.
        NotFoundError
            If the environment or the repository does not exist.
        ValidationError
            If `limit` is below 1 or `cursor` is not one this server issued for the deploys list.
        """
        env = self._authorize_read(p, environment)
        if limit < 1:
            raise ValidationError(f"limit must be at least 1, got {limit}.")
        position = None if cursor is None else decode_cursor("deploys", cursor)
        query = select(DeployRun).where(DeployRun.environment_id == env.id)
        if repository is not None:
            query = query.where(DeployRun.repository_id == self._repository(repository).id)
        if position is not None:
            query = query.where(
                or_(
                    DeployRun.created_at < position.at,
                    and_(DeployRun.created_at == position.at, DeployRun.id < position.id),
                )
            )
        runs = list(
            self._session.execute(
                query.order_by(DeployRun.created_at.desc(), DeployRun.id.desc()).limit(limit + 1)
            ).scalars()
        )
        page = runs[:limit]
        next_cursor = None
        if len(runs) > limit:
            next_cursor = encode_cursor("deploys", page[-1].created_at, page[-1].id)
        reads = _WarningReads()
        return [self._view(run, p, reads) for run in page], next_cursor

    def deployments(self, p: PrincipalRef, environment: str) -> list[DeploymentView]:
        """List the current project version per repository.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.

        Returns
        -------
        list of DeploymentView
            Sorted by repository name.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT.
        NotFoundError
            If the environment does not exist.
        """
        env = self._authorize_read(p, environment)
        rows = self._session.execute(
            select(
                Repository.name,
                JobVersion.id,
                JobVersion.commit_sha,
                EnvironmentDeployment.updated_at,
                Principal.name,
            )
            .join(EnvironmentDeployment, EnvironmentDeployment.repository_id == Repository.id)
            .join(JobVersion, JobVersion.id == EnvironmentDeployment.current_job_version_id)
            .outerjoin(Principal, Principal.id == EnvironmentDeployment.run_as_principal_id)
            .where(EnvironmentDeployment.environment_id == env.id)
            .order_by(Repository.name)
        )
        return [
            DeploymentView(
                repository=name, job_version_id=jv, commit_sha=sha, updated_at=at, run_as=run_as
            )
            for name, jv, sha, at, run_as in rows
        ]

    # -- run principal and undeploy --

    def _deployment_row(self, env: Environment, repo: Repository) -> EnvironmentDeployment:
        """Return the deployment pointer of a repository in an environment.

        Parameters
        ----------
        env
            The environment.
        repo
            The repository.

        Returns
        -------
        EnvironmentDeployment
            The row, freshly read.

        Raises
        ------
        NotFoundError
            If the repository is not deployed there.
        """
        return deployment_row(self._session, env, repo)

    def deployment(self, p: PrincipalRef, environment: str, repository: str) -> DeploymentView:
        """Return one deployment's view.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        repository
            Repository name.

        Returns
        -------
        DeploymentView
            The pointer, its commit and the bound run principal.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT (checked first).
        NotFoundError
            If the environment or repository does not exist or it is not deployed.
        """
        env = self._authorize_read(p, environment)
        return self._deployment_view(env, self._repository(repository))

    def _deployment_view(self, env: Environment, repo: Repository) -> DeploymentView:
        """Build one deployment's view without authorizing.

        Parameters
        ----------
        env
            The environment.
        repo
            The repository.

        Returns
        -------
        DeploymentView
            The pointer, its commit and the bound run principal.

        Raises
        ------
        NotFoundError
            If the repository is not deployed there.
        """
        row = self._deployment_row(env, repo)
        version = self._session.get(JobVersion, row.current_job_version_id)
        run_as = (
            self._session.get(Principal, row.run_as_principal_id)
            if row.run_as_principal_id is not None
            else None
        )
        return DeploymentView(
            repository=repo.name,
            job_version_id=row.current_job_version_id,
            commit_sha=version.commit_sha if version is not None else "",
            updated_at=row.updated_at,
            run_as=run_as.name if run_as is not None else None,
        )

    def _set_binding(
        self, env: Environment, repo: Repository, principal_id: uuid.UUID | None
    ) -> None:
        """Change the binding without moving the pointer, as a conditional update.

        Parameters
        ----------
        env
            The environment.
        repo
            The repository.
        principal_id
            The run principal, or `None` to clear.

        Raises
        ------
        NotFoundError
            If the deployment was removed meanwhile; the transaction is rolled back.
        """
        result = self._session.execute(
            update(EnvironmentDeployment)
            .where(
                EnvironmentDeployment.environment_id == env.id,
                EnvironmentDeployment.repository_id == repo.id,
            )
            .values(
                run_as_principal_id=principal_id,
                updated_at=EnvironmentDeployment.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if cast("CursorResult[Any]", result).rowcount != 1:
            self._session.rollback()
            raise NotFoundError(f"Repository {repo.name!r} is not deployed in {env.name!r}.")

    def set_run_as(
        self, p: PrincipalRef, environment: str, repository: str, principal: str
    ) -> DeploymentView:
        """Bind the service account runs of this deployment write as.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        repository
            Repository name.
        principal
            Name of an active service account.

        Returns
        -------
        DeploymentView
            The deployment with its new binding.

        Raises
        ------
        PermissionDenied
            Without MANAGE_ENVIRONMENT on the environment (checked first).
        NotFoundError
            If the environment, repository or deployment does not exist, or the
            principal does not exist or is not a service account (one message
            for both, so names cannot be enumerated).
        ValidationError
            If the service account is deactivated.
        """
        self._authz.authorize(
            p, Action.MANAGE_ENVIRONMENT, Securable.environment_scope(environment)
        )
        env, repo = self._environment(environment), self._repository(repository)
        self._deployment_row(env, repo)
        row = self._session.execute(
            select(Principal).where(Principal.name == principal)
        ).scalar_one_or_none()
        if row is None or row.type is not PrincipalType.SERVICE_ACCOUNT:
            # One answer for both: a 404 for "missing" next to a 422 for
            # "is a user" would let the caller enumerate principal names.
            raise NotFoundError(f"Principal {principal!r} not found or not a service account.")
        if not row.is_active:
            raise ValidationError(f"Principal {principal!r} is deactivated.")
        self._set_binding(env, repo, row.id)
        AuditService(self._session).record(
            p.id,
            AuditEventType.RUN_AS_BOUND,
            securable=Securable.environment_scope(environment),
            detail={"repository": repository, "principal": principal},
        )
        self._session.commit()
        return self._deployment_view(env, repo)

    def clear_run_as(self, p: PrincipalRef, environment: str, repository: str) -> DeploymentView:
        """Remove the run principal binding of a deployment.

        Clearing a deployment that has no binding changes nothing and records
        no `RUN_AS_CLEARED`.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        repository
            Repository name.

        Returns
        -------
        DeploymentView
            The deployment without a binding.

        Raises
        ------
        PermissionDenied
            Without MANAGE_ENVIRONMENT on the environment (checked first).
        NotFoundError
            If the environment, repository or deployment does not exist.
        """
        self._authz.authorize(
            p, Action.MANAGE_ENVIRONMENT, Securable.environment_scope(environment)
        )
        env, repo = self._environment(environment), self._repository(repository)
        before = self._deployment_row(env, repo).run_as_principal_id
        if before is None:
            return self._deployment_view(env, repo)
        previous = self._session.get(Principal, before)
        self._set_binding(env, repo, None)
        AuditService(self._session).record(
            p.id,
            AuditEventType.RUN_AS_CLEARED,
            securable=Securable.environment_scope(environment),
            detail={
                "repository": repository,
                "principal": previous.name if previous is not None else None,
            },
        )
        self._session.commit()
        return self._deployment_view(env, repo)

    def lock_deployment(
        self, environment_id: uuid.UUID, repository_id: uuid.UUID, job_version_id: uuid.UUID
    ) -> None:
        """Lock the pointer inside the caller's transaction, asserting it still names a version.

        A no-op `UPDATE` conditional on the pointer: it takes the row lock on
        Postgres (re-evaluating the condition after any wait) and the write
        lock on SQLite, so a run start and an undeploy or redeploy serialize.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository.
        job_version_id
            The project version the caller read as current.

        Raises
        ------
        DeploymentMoved
            If the pointer moved or was removed since; the transaction is rolled back.
        """
        result = self._session.execute(
            update(EnvironmentDeployment)
            .where(
                EnvironmentDeployment.environment_id == environment_id,
                EnvironmentDeployment.repository_id == repository_id,
                EnvironmentDeployment.current_job_version_id == job_version_id,
            )
            .values(updated_at=EnvironmentDeployment.updated_at)
            .execution_options(synchronize_session=False)
        )
        if cast("CursorResult[Any]", result).rowcount != 1:
            self._session.rollback()
            raise DeploymentMoved(
                "The deployment changed (redeployed, rolled back or undeployed) meanwhile; retry."
            )

    def undeploy(self, p: PrincipalRef, environment: str, repository: str) -> None:
        """Remove a repository's deployment pointer and run principal binding.

        Data and project versions stay: the repository's tables become orphans and
        its project versions remain as history. The pointer row is deleted first and
        the active-run check runs after it in the same transaction, so a run
        start racing this call either commits first (and this call is refused)
        or finds the pointer gone (`lock_deployment`).

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        repository
            Repository name.

        Raises
        ------
        PermissionDenied
            For a CI token, always; otherwise without DEPLOY on the environment (checked
            first).
        NotFoundError
            If the environment, repository or deployment does not exist.
        ConflictError
            While a run of the repository is queued or running there.
        """
        refuse_ci_token(self._authz, p, environment, "undeploy a repository")
        self._authz.authorize(p, Action.DEPLOY, Securable.environment_scope(environment))
        env, repo = self._environment(environment), self._repository(repository)
        row = self._deployment_row(env, repo)
        version_id = row.current_job_version_id
        result = self._session.execute(
            delete(EnvironmentDeployment)
            .where(
                EnvironmentDeployment.environment_id == env.id,
                EnvironmentDeployment.repository_id == repo.id,
            )
            .execution_options(synchronize_session=False)
        )
        if cast("CursorResult[Any]", result).rowcount != 1:
            self._session.rollback()
            raise NotFoundError(f"Repository {repository!r} is not deployed in {environment!r}.")
        active = self._session.execute(
            select(Run.id).where(
                Run.environment_id == env.id,
                Run.repository_id == repo.id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
            )
        ).scalar_one_or_none()
        if active is not None:
            self._session.rollback()
            raise ConflictError(
                f"Run {active} of {repository!r} is active in {environment!r}; cancel it or "
                "wait for it to finish before undeploying."
            )
        # Nothing cascades from the pointer row, so the schedules of the deployment go here.
        for waiting in self._session.scalars(
            select(JobSchedule)
            .where(
                JobSchedule.environment_id == env.id,
                JobSchedule.repository_id == repo.id,
                JobSchedule.pending_start.is_not(None),
            )
            .order_by(JobSchedule.job)
        ):
            self._audit_dropped_start(
                environment, repository, waiting.job, waiting.pending_start, "undeployed"
            )
        schedule_ids = select(JobSchedule.id).where(
            JobSchedule.environment_id == env.id, JobSchedule.repository_id == repo.id
        )
        self._session.execute(
            delete(JobFire)
            .where(JobFire.job_schedule_id.in_(schedule_ids))
            .execution_options(synchronize_session=False)
        )
        self._session.execute(
            delete(JobSchedule)
            .where(JobSchedule.environment_id == env.id, JobSchedule.repository_id == repo.id)
            .execution_options(synchronize_session=False)
        )
        AuditService(self._session).record(
            p.id,
            AuditEventType.UNDEPLOYED,
            securable=Securable.environment_scope(environment),
            detail={"repository": repository, "project_version_id": str(version_id)},
        )
        self._session.commit()

    def job_version(
        self, p: PrincipalRef, environment: str, job_version_id: uuid.UUID
    ) -> JobVersionView:
        """Show a project version's manifest, withholding what the reader may not see.

        An asset's `sql`, `normalized_sql`, `references` and `lineage` name
        the tables it reads, so they are kept only if `p` may read every
        referenced table; otherwise they are removed and
        `hidden_references` counts the unreadable references, and the edges
        into such an asset are dropped (counted in `hidden_edges`) since they
        would reveal what its SQL reads; so are edges from a table `p` may not read,
        which a `references:` target alone can make. Unreadable
        `external_inputs` are removed and counted in
        `hidden_external_inputs`. A check's `sql` and `references` follow
        the asset rule, judged on the tables its SQL names (`_visible_checks`). The reference
        targets of an asset's `model` name tables too: each one `p` may not read is removed and
        counted in the entry's `hidden_model_references` (`_visible_model`).

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.
        job_version_id
            The project version.

        Returns
        -------
        JobVersionView
            The filtered view.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT.
        NotFoundError
            If no such project version exists in this environment.
        """
        env = self._authorize_read(p, environment)
        row = self._session.execute(
            select(JobVersion, Repository.name)
            .join(Repository, Repository.id == JobVersion.repository_id)
            .where(JobVersion.id == job_version_id, JobVersion.environment_id == env.id)
        ).one_or_none()
        if row is None:
            raise NotFoundError(f"No project version {job_version_id} in {environment}.")
        version, repository_name = row
        manifest = dict(version.manifest_json)
        assets = []
        hidden_assets: set[str] = set()
        for entry in manifest.get("assets", []):
            entry = dict(entry)
            unreadable = [
                r for r in entry.get("references", []) if not self._may_read(p, environment, r)
            ]
            if unreadable:
                for key in _HIDDEN_KEYS:
                    entry.pop(key, None)
                entry["hidden_references"] = len(unreadable)
                hidden_assets.add(entry["table"])
            else:
                entry["hidden_references"] = 0
            if isinstance(entry.get("model"), Mapping):
                entry["model"], entry["hidden_model_references"] = self._visible_model(
                    p, environment, entry["model"]
                )
            assets.append(entry)
        manifest["assets"] = assets
        if "checks" in manifest:
            manifest["checks"] = self._visible_checks(p, environment, manifest["checks"])
        edges = manifest.get("edges", [])
        # An edge into a hidden asset reveals what its SQL reads; an edge from a table the
        # reader may not read can come from `references:` alone, and reveals the target that
        # `_visible_model` withholds.
        manifest["edges"] = [
            edge
            for edge in edges
            if edge[1] not in hidden_assets and self._may_read(p, environment, edge[0])
        ]
        manifest["hidden_edges"] = len(edges) - len(manifest["edges"])
        inputs = manifest.get("external_inputs", [])
        visible = [name for name in inputs if self._may_read(p, environment, name)]
        manifest["external_inputs"] = visible
        manifest["hidden_external_inputs"] = len(inputs) - len(visible)
        return JobVersionView(
            id=version.id,
            environment=environment,
            repository=repository_name,
            commit_sha=version.commit_sha,
            code_uri=version.code_uri,
            code_sha256=version.code_sha256,
            deployed_by=version.deployed_by,
            created_at=version.created_at,
            manifest=manifest,
        )

    def _visible_model(
        self, p: PrincipalRef, environment: str, model: Mapping[str, Any]
    ) -> tuple[dict[str, Any], int]:
        """Withhold the reference targets of an asset's model that the reader may not read.

        Parameters
        ----------
        p
            The reader.
        environment
            Environment name.
        model
            The entry's `model`; untrusted, like a stored manifest.

        Returns
        -------
        tuple
            The model with only the references whose target `p` may read, and how many were
            removed; an item that is not a mapping with a string `target` is removed too.
        """
        items = model.get("references")
        listed = items if isinstance(items, list) else []
        visible = [
            item
            for item in listed
            if isinstance(item, Mapping)
            and isinstance(item.get("target"), str)
            and self._may_read(p, environment, item["target"])
        ]
        return {**model, "references": visible}, len(listed) - len(visible)

    def _visible_checks(
        self, p: PrincipalRef, environment: str, checks: object
    ) -> list[dict[str, Any]]:
        """Withhold the SQL of every check that names a table the reader may not read.

        The tables are read again from each check's SQL, never taken from the stored
        `references`.

        Parameters
        ----------
        p
            The reader.
        environment
            Environment name.
        checks
            The manifest's `checks`; untrusted.

        Returns
        -------
        list of dict
            One copy per check entry. A check whose SQL names a table `p` may not read, or
            whose tables cannot be listed, loses `sql` and `references`, and
            `hidden_references` counts the unreadable names (1 when they cannot be listed);
            name, table, check, path, on_failure and description stay, and a generated check's
            `generated` keeps only its family and columns. A value that is not a list, and an
            entry that is not a mapping, show as nothing.
        """
        if not isinstance(checks, list):
            return []
        visible: list[dict[str, Any]] = []
        for raw in checks:
            if not isinstance(raw, Mapping):
                continue
            entry = dict(raw)
            names = check_sql_references(entry.get("sql"))
            hidden = (
                1
                if names is None
                else sum(not self._may_read(p, environment, name) for name in names)
            )
            if names is None or hidden:
                entry.pop("sql", None)
                entry.pop("references", None)
                generated = entry.get("generated")
                if isinstance(generated, Mapping):
                    # A reference check's source names its target, as its SQL does.
                    entry["generated"] = {
                        key: generated[key] for key in ("family", "columns") if key in generated
                    }
            else:
                entry["references"] = list(names)
            entry["hidden_references"] = hidden
            visible.append(entry)
        return visible

    def orphans(self, p: PrincipalRef, environment: str) -> OrphanReport:
        """Compute tables earlier project versions produced that no current one does.

        A repository with no current deployment (undeployed) has every project version past.
        A table one repository stopped producing is not an orphan while another
        repository's current project version produces it (a table moved from A to B):
        dropping it would delete B's live output.

        Parameters
        ----------
        p
            The acting principal.
        environment
            Environment name.

        Returns
        -------
        OrphanReport
            Readable orphans and a count of hidden ones.

        Raises
        ------
        PermissionDenied
            Without READ_ENVIRONMENT.
        NotFoundError
            If the environment does not exist.
        """
        env = self._authorize_read(p, environment)
        orphans: list[OrphanView] = []
        hidden = 0
        current = {
            repository_id: version_id
            for repository_id, version_id in self._session.execute(
                select(
                    EnvironmentDeployment.repository_id,
                    EnvironmentDeployment.current_job_version_id,
                ).where(EnvironmentDeployment.environment_id == env.id)
            )
        }
        produced_now: set[str] = set()
        for manifest in self._session.execute(
            select(JobVersion.manifest_json).where(JobVersion.id.in_(list(current.values())))
        ).scalars():
            produced_now |= manifest_tables(manifest)
        repositories = self._session.execute(
            select(Repository.id, Repository.name)
            .where(
                Repository.id.in_(
                    select(JobVersion.repository_id).where(JobVersion.environment_id == env.id)
                )
            )
            .order_by(Repository.name)
        ).all()
        for repository_id, repository_name in repositories:
            current_id = current.get(repository_id)
            past: set[str] = set()
            past_query = select(JobVersion.manifest_json).where(
                JobVersion.environment_id == env.id, JobVersion.repository_id == repository_id
            )
            if current_id is not None:
                past_query = past_query.where(JobVersion.id != current_id)
            for manifest in self._session.execute(past_query).scalars():
                past |= manifest_tables(manifest)
            for table in sorted(past - produced_now):
                try:
                    ref = TableRef.from_dotted(environment, table)
                except ValidationError:
                    continue
                if (
                    resolve_table_id(self._session, environment, ref.namespace.joined(), ref.name)
                    is None
                ):
                    continue
                if self._may_read(p, environment, table):
                    orphans.append(OrphanView(repository=repository_name, table=table))
                else:
                    hidden += 1
        return OrphanReport(
            orphans=sorted(orphans, key=lambda o: (o.repository, o.table)), hidden=hidden
        )

    # -- worker side -----------------------------------------------------

    def authorize_deploy(
        self, p: PrincipalRef, environment: str, run: DeployRun | None = None
    ) -> CiScope | None:
        """Re-check `DEPLOY` at claim time, and a CI token's binding when one queued the run.

        Parameters
        ----------
        p
            The run's principal, as read from its row (no scope).
        environment
            Environment name.
        run
            The run. When a CI token queued it (`DeployRun.ci_token_id`), the token is resolved
            again (`ci_scope_for_run`), `DEPLOY` is checked with its scope, and the run must
            still be one the token could queue (`check_ci_request`).

        Returns
        -------
        CiScope or None
            The CI token's scope, for the check after the fetch (`check_ci_commit`); `None`
            for a run no CI token queued.

        Raises
        ------
        PermissionDenied
            If the grant was revoked since the run was queued, or the CI token is no longer
            valid or could not have queued this run.
        """
        scope = None if run is None else ci_scope_for_run(self._session, run)
        if scope is not None:
            p = dataclasses.replace(p, ci_scope=scope)
        self._authz.authorize(p, Action.DEPLOY, Securable.environment_scope(environment))
        if scope is not None:
            assert run is not None  # nosec B101 -- a scope comes only from a run
            repository = self._session.get(Repository, run.repository_id)
            check_ci_request(
                self._authz,
                p,
                scope,
                environment=environment,
                environment_id=run.environment_id,
                repository="" if repository is None else repository.name,
                repository_id=run.repository_id,
                kind=run.kind,
                ref=run.requested_ref,
            )
            if run.kind is DeployKind.DEPLOY and run.requested_ref != scope.sha:
                ci_refusal(
                    self._authz,
                    p,
                    environment,
                    f"This deploy asks for {run.requested_ref}, not the CI token's commit "
                    f"{scope.sha}.",
                )
        return scope

    def check_ci_commit(self, p: PrincipalRef, scope: CiScope, kind: DeployKind, sha: str) -> None:
        """Check, after the fetch, that a CI token's deploy fetched the token's signed commit.

        Parameters
        ----------
        p
            The run's principal.
        scope
            The scope `authorize_deploy` returned.
        kind
            The run's kind; a dry run is not pinned.
        sha
            The commit the worker fetched.

        Raises
        ------
        PermissionDenied
            If a deploy fetched another commit.
        """
        check_ci_commit(self._authz, dataclasses.replace(p, ci_scope=scope), scope, kind, sha)

    def resolve_model_references(
        self,
        p: PrincipalRef,
        environment: str,
        repository_id: uuid.UUID,
        assets: Sequence[ParsedAsset],
    ) -> tuple[dict[str, tuple[ResolvedReference, ...]], list[ProjectIssue]]:
        """Resolve each asset's references against the project and the environment.

        A target the project builds is read from its asset. Any other target counts only once
        it exists and `p` may read it (`_readable_table`); its key comes from the current
        manifest of the repository that builds it, its columns from the catalog. A target that
        is missing and one `p` may not read get the same message, which names nothing of the
        table.

        Both principals are checked. The deployer decides what the messages may reveal, so a
        more privileged run principal cannot be used to probe tables. When a run principal is
        bound to the repository in the environment, and it is not the deployer, every target
        the deployer resolved outside the project must also be readable by it, as its runs read
        the target; naming the target in that refusal reveals nothing the deployer may not see.

        Parameters
        ----------
        p
            The deploying principal; the bound run principal, if any, is read from the
            repository's deployment row.
        environment
            Environment name.
        repository_id
            The repository being deployed.
        assets
            The parsed assets.

        Returns
        -------
        tuple
            Table → its resolved references, and the issues (`resolve_references`).
        """
        env = self._environment(environment)
        elsewhere = self._elsewhere_entries(env.id, repository_id)
        by_table = {asset.table: asset for asset in assets}

        def lookup(name: str) -> TargetInfo | None:
            """Find what is known about a target.

            `resolve_references` reads a name as a table exactly when this returns info, so the
            reading of a target and what is shown of it pass the same test.

            Parameters
            ----------
            name
                Dotted name.

            Returns
            -------
            TargetInfo or None
                The info of a project asset, or of an existing table `p` may read, an artifact
                marked as one; `None` for anything else.
            """
            if name in by_table:
                asset = by_table[name]
                meta = asset.metadata
                return TargetInfo(
                    meta.kind,
                    key_columns(meta.key),
                    meta.key == KEY_NONE,
                    asset.columns,
                    artifact=asset.output == "artifact",
                )
            # Another repository's table counts only once it exists and `p` may read it, so an
            # unreadable target never reaches the manifest or catalog metadata below.
            table_type = self._readable_type(p, environment, name)
            if table_type is None:
                return None
            if table_type is TableType.ARTIFACT:
                return TargetInfo(None, (), False, None, artifact=True)
            columns = self._catalog_columns(environment, name)
            entry = elsewhere.get(name)
            if entry is None:
                return TargetInfo(None, (), False, columns)
            stored = entry.get("columns")
            listed = (
                tuple(stored)
                if isinstance(stored, list) and all(isinstance(c, str) for c in stored)
                else None
            )
            model = entry_model(entry)
            if model is None:
                # A manifest from before models: a merge's key still identifies a row.
                write, merge_key, _ = entry_write(entry)
                key = merge_key if write == "merge" else ()
                return TargetInfo(None, key, False, columns or listed)
            return TargetInfo(model.kind, model.key, model.key_none, columns or listed)

        resolved, issues = resolve_references(assets, lookup, environment=environment)
        run_as = self._bound_run_as(env.id, repository_id)
        if run_as is None or run_as.id == p.id:
            return resolved, issues
        kept: dict[str, tuple[ResolvedReference, ...]] = {}
        for asset in sorted(assets, key=lambda a: a.path):
            readable = []
            for reference in resolved.get(asset.table, ()):
                if reference.target in by_table or self._readable_table(
                    run_as, environment, reference.target
                ):
                    readable.append(reference)
                    continue
                issues.append(
                    ProjectIssue(
                        asset.path,
                        asset.block_line or asset.body_line,
                        f"Reference target `{reference.target}` cannot be read by "
                        f"`{shown(run_as.name)}`, the run principal bound to this repository in "
                        f"`{environment}`, so its runs could not check it; grant it READ_TABLE "
                        f"on `{reference.target}`, or bind another run principal with "
                        "`lakana deployments set-run-as`.",
                    )
                )
            if readable:
                kept[asset.table] = tuple(readable)
        return kept, issues

    def _bound_run_as(
        self, environment_id: uuid.UUID, repository_id: uuid.UUID
    ) -> PrincipalRef | None:
        """Find the run principal bound to a repository in an environment.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository.

        Returns
        -------
        PrincipalRef or None
            The bound principal; `None` when none is bound.
        """
        row = self._session.execute(
            select(Principal)
            .join(
                EnvironmentDeployment,
                EnvironmentDeployment.run_as_principal_id == Principal.id,
            )
            .where(
                EnvironmentDeployment.environment_id == environment_id,
                EnvironmentDeployment.repository_id == repository_id,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return PrincipalRef(id=row.id, name=row.name, type=row.type)

    def validate_against_environment(
        self,
        p: PrincipalRef,
        environment: str,
        repository_id: uuid.UUID,
        assets: Sequence[ParsedAsset],
        graph: ProjectGraph,
    ) -> list[ProjectIssue]:
        """Check external inputs and table ownership against the environment.

        Parameters
        ----------
        p
            The deploying principal.
        environment
            Environment name.
        repository_id
            The repository being deployed.
        assets
            The parsed assets.
        graph
            Their DAG.

        Returns
        -------
        list of ProjectIssue
            A file read (``FROM 'x.csv'``) is never a table and is "not
            found"; so is an external input that neither exists readably nor
            is produced by another repository's current project version; an asset
            targeting a table another repository produces is refused. A SQL asset
            that reads an artifact is refused: an artifact asset of the project,
            a catalog artifact `p` may read, or an input that another
            repository's deploy produces as an artifact, whether
            its row is missing or unreadable. An artifact asset whose name is a
            readable table, view or external table, a table or SQL asset whose
            name is a readable artifact, and an artifact asset that reads
            itself are refused too. Of an object `p` may not read, only the kind
            another repository's deploy gives it is ever visible; a clash of
            the asset's own name with it is left to the run.
        """
        env = self._environment(environment)
        elsewhere = self._produced_elsewhere(env.id, repository_id)
        issues: list[ProjectIssue] = []
        ordered = sorted(assets, key=lambda a: a.path)
        files: dict[str, ParsedAsset] = {}
        first_reader: dict[str, ParsedAsset] = {}
        for asset in ordered:
            for name in sorted(_file_references(asset)):
                files.setdefault(name, asset)
            for reference in asset.references:
                first_reader.setdefault(reference, asset)
        for name, reader in files.items():
            issues.append(ProjectIssue(reader.path, reader.body_line, f"table {name!r} not found"))
        for name in graph.external_inputs:
            if name in files or name in elsewhere or self._readable_table(p, environment, name):
                continue
            reader = first_reader[name]
            issues.append(ProjectIssue(reader.path, reader.body_line, f"table {name!r} not found"))
        for asset in ordered:
            owner = elsewhere.get(asset.table)
            if owner is not None:
                issues.append(
                    ProjectIssue(
                        asset.path,
                        asset.body_line,
                        f"table {asset.table!r} is produced by repository {owner!r} "
                        f"in {environment}",
                    )
                )
        artifacts = {asset.table for asset in assets if asset.output == "artifact"}
        artifacts |= self.artifact_inputs(p, environment, graph, repository_id=repository_id)
        for asset in ordered:
            issues.extend(self._kind_issues(p, environment, asset, artifacts))
        return issues

    def validate_checks_against_environment(
        self,
        p: PrincipalRef,
        environment: str,
        repository_id: uuid.UUID,
        checks: Sequence[ParsedCheck],
        assets: Sequence[ParsedAsset],
    ) -> list[ProjectIssue]:
        """Check the tables the checks read against the environment.

        Parameters
        ----------
        p
            The deploying principal.
        environment
            Environment name.
        repository_id
            The repository being deployed.
        checks
            The parsed checks.
        assets
            The parsed assets, whose tables a check may read without the catalog knowing them yet.

        Returns
        -------
        list of ProjectIssue
            A table a check reads that is a file, or that is neither produced by the project or
            by another repository's current project version nor readable by `p`, is "not found" (an
            unreadable one reads the same as a missing one); an artifact, of the project or
            readable by `p` or produced by another repository, is refused with the reason.
        """
        env = self._environment(environment)
        elsewhere = self._produced_elsewhere(env.id, repository_id)
        built = {asset.table for asset in assets}
        ordered = sorted(checks, key=lambda c: c.path)
        outside = sorted({ref for check in ordered for ref in check.references} - built)
        artifacts = {asset.table for asset in assets if asset.output == "artifact"}
        artifacts |= self.artifact_inputs(
            p,
            environment,
            ProjectGraph(edges=(), external_inputs=tuple(outside)),
            repository_id=repository_id,
        )
        issues: list[ProjectIssue] = []
        for check in ordered:
            files = _sql_file_references(check.sql, check.references)
            for name in check.references:
                if name in artifacts:
                    message = f"reads the artifact {name!r}; SQL cannot read an artifact"
                elif name in files or not (
                    name in built or name in elsewhere or self._readable_table(p, environment, name)
                ):
                    message = f"table {name!r} not found"
                else:
                    continue
                issues.append(ProjectIssue(check.path, check.body_line, message))
        return issues

    def _kind_issues(
        self, p: PrincipalRef, environment: str, asset: ParsedAsset, artifacts: set[str]
    ) -> list[ProjectIssue]:
        """Check one asset against the kinds of the objects it reads and of the name it takes.

        Parameters
        ----------
        p
            The deploying principal.
        environment
            Environment name.
        asset
            The asset.
        artifacts
            Every name known to be an artifact: the project's artifact assets and
            the inputs `artifact_inputs` finds (readable catalog artifacts, and
            artifacts another repository's deploy produces).

        Returns
        -------
        list of ProjectIssue
            The issues, in a stable order.
        """
        issues: list[ProjectIssue] = []

        def issue(message: str) -> None:
            """Add an issue on the asset's file and line.

            Parameters
            ----------
            message
                The issue.
            """
            issues.append(ProjectIssue(asset.path, asset.body_line, message))

        if asset.kind != "python":
            for name in asset.references:
                if name in artifacts:
                    issue(f"`{name}` is an artifact; only a Python asset can read it.")
        elif asset.output == "artifact" and asset.table in asset.references:
            issue(
                f"an artifact asset cannot read its own artifact ({asset.table}); "
                "remove it from inputs"
            )
        if asset.kind == "python":
            issues.extend(
                ProjectIssue(asset.path, asset.body_line, message)
                for message in _artifact_parameter_issues(asset, artifacts)
            )
        existing = self._readable_type(p, environment, asset.table)
        if asset.output == "artifact" and existing in _KIND_PHRASES:
            issue(
                f"`{asset.table}` already exists in {environment} as {_KIND_PHRASES[existing]}; "
                "drop it first, or choose another name."
            )
        elif asset.output != "artifact" and existing is TableType.ARTIFACT:
            issue(
                f"`{asset.table}` already exists in {environment} as an artifact; drop it "
                f"first (`lakana artifact drop {asset.table}`), or choose another name."
            )
        return issues

    def existing_unowned(
        self, p: PrincipalRef, environment: str, tables: Iterable[str]
    ) -> set[str]:
        """Find tables that exist readably while no current project version produces them.

        Parameters
        ----------
        p
            The deploying principal.
        environment
            Environment name.
        tables
            Candidate table names.

        Returns
        -------
        set of str
            The diff's "adopted" candidates.
        """
        env = self._environment(environment)
        produced = self._produced_elsewhere(env.id, None)
        return {
            table
            for table in tables
            if table not in produced and self._readable_table(p, environment, table)
        }

    def _require_alive(self, run: DeployRun) -> tuple[str, str]:
        """Confirm the run's environment and repository still exist.

        `RepositoryService.delete` checks for pointers and deletes in two
        steps, so a repository can vanish under a running deploy; the
        cascade takes the run with it.

        Parameters
        ----------
        run
            The running deploy.

        Returns
        -------
        tuple of str
            The environment's and the repository's names.

        Raises
        ------
        NotFoundError
            If the run, its environment or its repository was deleted; the
            transaction is rolled back.
        """
        try:
            environment_id, repository_id = run.environment_id, run.repository_id
        except ObjectDeletedError:
            self._session.rollback()
            raise NotFoundError(_GONE_MESSAGE) from None
        environment_name = self._session.execute(
            select(Environment.name).where(Environment.id == environment_id)
        ).scalar_one_or_none()
        repository_name = self._session.execute(
            select(Repository.name).where(Repository.id == repository_id)
        ).scalar_one_or_none()
        if environment_name is None or repository_name is None:
            self._session.rollback()
            raise NotFoundError(_GONE_MESSAGE)
        return environment_name, repository_name

    def _audit(
        self,
        p: PrincipalRef,
        run: DeployRun,
        names: tuple[str, str],
        event_type: AuditEventType,
        version: JobVersion | None,
        diff: DeployDiff,
    ) -> None:
        """Add a deploy audit event to the current transaction.

        Parameters
        ----------
        p
            The principal.
        run
            The run.
        names
            Environment and repository names.
        event_type
            `DEPLOYED`, `DEPLOY_DRY_RUN` or `ROLLED_BACK`.
        version
            The project version made current, if any.
        diff
            Its diff; only counts are recorded.
        """
        AuditService(self._session).record(
            p.id,
            event_type,
            securable=Securable.environment_scope(names[0]),
            detail={
                "deploy_id": str(run.id),
                "repository": names[1],
                "ref": run.requested_ref,
                "sha": run.resolved_sha,
                "project_version_id": str(version.id) if version is not None else None,
                "diff": diff.counts(),
            },
        )

    @staticmethod
    def _require_kind(run: DeployRun, kind: DeployKind) -> None:
        """Refuse a run of another kind than the operation records.

        Parameters
        ----------
        run
            The run.
        kind
            The kind the operation handles.

        Raises
        ------
        ValidationError
            If `run.kind` differs.
        """
        if run.kind is not kind:
            raise ValidationError(f"Deploy run {run.id} is not a {kind.value} run.")

    def _hold(self, run: DeployRun, worker_id: str | None, *, queued_too: bool = False) -> None:
        """Lock the run's row and stop unless it is still `running` under `worker_id`.

        The scheduler ends a `running` deploy whose worker is gone and tells the user to deploy
        again. A worker that wakes up after that must not activate anything: the lock is held
        to the commit of the finishing transaction, so the sweep either ended the run first
        (seen here) or passes the locked row by and finds it finished.

        On PostgreSQL the lock is `SELECT … FOR UPDATE`. SQLite has no row locks, and `sqlite3`
        starts a transaction only at the first write, so a bare `SELECT` would leave a gap in
        which another connection could end the run. A write that changes nothing therefore
        comes first: it takes SQLite's single writer lock, which no other connection can get
        until this transaction ends, and the `SELECT` then reads what no one can change.

        On PostgreSQL under READ COMMITTED (the engine sets no other isolation level) a hold that
        had to wait behind the sweep's open transaction re-reads the row once the lock is
        granted, so the comparison below is made on what the sweep committed.

        Parameters
        ----------
        run
            The run.
        worker_id
            The id the caller was claimed under; `None` for a run that carries none.
        queued_too
            Also accept a run that is still `queued` (nobody claimed it, so nobody else can
            end it); only `fail` does.

        Raises
        ------
        NotFoundError
            If the run was deleted; the transaction is rolled back.
        ConflictError
            If the run is not `running` under `worker_id`; the transaction is rolled back.
        """
        if self._session.get_bind().dialect.name == "sqlite":
            self._session.execute(
                update(DeployRun)
                .where(DeployRun.id == run.id)
                .values(worker_id=DeployRun.worker_id)
                .execution_options(synchronize_session=False)
            )
        row = self._session.execute(
            select(DeployRun.status, DeployRun.worker_id)
            .where(DeployRun.id == run.id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            self._session.rollback()
            raise NotFoundError(_GONE_MESSAGE)
        status, holder = row
        if queued_too and status is DeployStatus.QUEUED:
            return
        if status is not DeployStatus.RUNNING or holder != worker_id:
            self._session.rollback()
            raise ConflictError(DEPLOY_ENDED_MESSAGE.format(id=run.id))

    def _finish(self, record: Callable[[], None] | None = None) -> None:
        """Audit and commit a run's outcome, translating a concurrent delete.

        Parameters
        ----------
        record
            Adds the audit event to the transaction, or `None` for none.

        Raises
        ------
        NotFoundError
            If the repository or run was deleted since `_require_alive`.
        """
        try:
            if record is not None:
                record()
            self._session.commit()
        except (IntegrityError, StaleDataError, ObjectDeletedError):
            self._session.rollback()
            raise NotFoundError(_GONE_MESSAGE) from None

    def _swap_pointer(
        self,
        environment_id: uuid.UUID,
        repository_id: uuid.UUID,
        expected: uuid.UUID | None,
        new: uuid.UUID,
    ) -> None:
        """Compare-and-swap the deployment pointer inside the caller's transaction.

        No savepoint is used: on SQLite a released savepoint can commit the
        loser's earlier writes, so the loser instead rolls the whole
        transaction back and writes nothing.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository.
        expected
            The pointer value the caller observed; `None` for "no pointer yet".
        new
            The project version to point at.

        Raises
        ------
        ConflictError
            If the pointer no longer holds `expected`; the transaction is
            rolled back.
        """
        now = datetime.now(UTC)
        if expected is None:
            try:
                self._session.execute(
                    insert(EnvironmentDeployment).values(
                        environment_id=environment_id,
                        repository_id=repository_id,
                        current_job_version_id=new,
                        updated_at=now,
                    )
                )
            except IntegrityError as exc:
                self._session.rollback()
                raise ConflictError(CONFLICT_MESSAGE) from exc
            return
        result = self._session.execute(
            update(EnvironmentDeployment)
            .where(
                EnvironmentDeployment.environment_id == environment_id,
                EnvironmentDeployment.repository_id == repository_id,
                EnvironmentDeployment.current_job_version_id == expected,
            )
            .values(current_job_version_id=new, updated_at=now)
            .execution_options(synchronize_session="fetch")
        )
        if cast("CursorResult[Any]", result).rowcount != 1:
            self._session.rollback()
            raise ConflictError(CONFLICT_MESSAGE)

    def _sync_job_schedules(
        self,
        environment_id: uuid.UUID,
        repository_id: uuid.UUID,
        environment_name: str,
        previous: dict[str, Any] | None,
        manifest: dict[str, Any],
        now: datetime,
    ) -> list[str]:
        """Make the ``job_schedules`` rows of a repository match the jobs of a manifest.

        Runs in the transaction that moved the pointer, after the move, so the rows and the
        pointer cannot disagree. A new job, a job whose cron or time zone differs from the
        previous manifest's, and a job that gained a schedule get their next fire time after
        `now`, with no catch-up; a job that lost its schedule gets none; an unchanged job keeps
        its time, its pause and its waiting start. A job the manifest no longer declares loses
        its row, and with it a waiting start. The rows are read ``FOR UPDATE``, so that a
        scheduler tick that set a waiting start is either seen here or waits for this
        transaction.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository.
        environment_name
            The environment's name, for the warnings.
        previous
            The manifest that was current before the move, or `None` for a first deploy.
        manifest
            The manifest that is current now (the manifest is already resolved for this
            environment, so an override's ``schedule: null`` shows as no schedule).
        now
            The deploy time; next fire times are the first ones after it.

        Returns
        -------
        list of str
            A sentence for each dropped waiting start and each scheduled job with no run
            principal bound, in job-name order.
        """
        jobs = manifest_jobs(manifest)
        before = manifest_jobs(previous) if previous is not None else {}
        rows = {
            row.job: row
            for row in self._session.scalars(
                select(JobSchedule)
                .where(
                    JobSchedule.environment_id == environment_id,
                    JobSchedule.repository_id == repository_id,
                )
                .with_for_update()
            )
        }
        warnings = self.job_schedule_warnings(
            environment_id, repository_id, environment_name, manifest
        )
        repository_name = self._session.execute(
            select(Repository.name).where(Repository.id == repository_id)
        ).scalar_one()
        for name in sorted(set(rows) - set(jobs)):
            row = rows[name]
            self._audit_dropped_start(
                environment_name, repository_name, name, row.pending_start, "job_removed"
            )
            self._session.execute(delete(JobFire).where(JobFire.job_schedule_id == row.id))
            self._session.delete(row)
        for name, job in sorted(jobs.items()):
            wanted = job_schedule(job)
            row = rows.get(name)
            if row is None:
                row = JobSchedule(
                    id=uuid.uuid4(),
                    environment_id=environment_id,
                    repository_id=repository_id,
                    job=name,
                )
                self._session.add(row)
                row.next_fire_at = None if wanted is None else next_fire_after(*wanted, now)
            elif wanted is None:
                row.next_fire_at = None
            else:
                unchanged = job_schedule(before.get(name, {})) == wanted
                if row.next_fire_at is None or not unchanged:
                    row.next_fire_at = next_fire_after(*wanted, now)
        return warnings

    def _audit_dropped_start(
        self,
        environment_name: str,
        repository_name: str,
        job: str,
        pending: dict[str, Any] | None,
        reason: str,
    ) -> None:
        """Record that a deploy or an undeploy deleted a start that was waiting.

        Nothing is written for a job that had no waiting start. The system dropped it, so no
        principal is the actor, and the event is part of the transaction that deleted the row.

        Parameters
        ----------
        environment_name
            The environment's name.
        repository_name
            The repository's name.
        job
            The job's name.
        pending
            The row's waiting start.
        reason
            ``job_removed`` or ``undeployed``.
        """
        if pending is None:
            return
        AuditService(self._session).record(
            None,
            AuditEventType.JOB_START_DROPPED,
            securable=Securable.environment_scope(environment_name),
            detail=start_detail(repository_name, job, pending, reason=reason),
        )

    def job_schedule_warnings(
        self,
        environment_id: uuid.UUID,
        repository_id: uuid.UUID,
        environment_name: str,
        manifest: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> list[str]:
        """Say what a manifest's jobs leave for the owner to act on, writing nothing.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository.
        environment_name
            The environment's name.
        manifest
            The manifest whose jobs are to be current.
        dry_run
            Word the sentences as what a deploy would do, not what it did.

        Returns
        -------
        list of str
            A sentence for each job that has a waiting start and is not in the manifest, then
            one for each scheduled job when the deployment has no run principal bound, then one
            for each job that notifies through a secret the environment does not have. The
            secret is looked up by name only; its value is never read.
        """
        jobs = manifest_jobs(manifest)
        waiting = sorted(
            row.job
            for row in self._session.scalars(
                select(JobSchedule).where(
                    JobSchedule.environment_id == environment_id,
                    JobSchedule.repository_id == repository_id,
                    JobSchedule.pending_start.is_not(None),
                )
            )
            if row.job not in jobs and row.pending_start is not None
        )
        bound = self._session.execute(
            select(EnvironmentDeployment.run_as_principal_id).where(
                EnvironmentDeployment.environment_id == environment_id,
                EnvironmentDeployment.repository_id == repository_id,
            )
        ).scalar_one_or_none()
        drop, run = (
            ("would be dropped", "would not run") if dry_run else ("is dropped", "does not run")
        )
        warnings = [
            f"job `{name}` is not in this manifest and a start of it is waiting for the active "
            f"run; that start {drop}; add the job back and deploy again, or start its assets "
            "with `lakana run --select`"
            for name in waiting
        ]
        if bound is None:
            warnings.extend(
                f"job `{name}` has a schedule but no run principal is bound in "
                f"`{environment_name}`; it {run} until an admin binds one with "
                "`lakana deployments set-run-as`"
                for name, job in sorted(jobs.items())
                if job_schedule(job) is not None
            )
        warnings.extend(
            self._missing_secret_warnings(
                environment_id, environment_name, manifest, dry_run=dry_run
            )
        )
        return warnings

    def _missing_secret_warnings(
        self,
        environment_id: uuid.UUID,
        environment_name: str,
        manifest: dict[str, Any],
        *,
        dry_run: bool,
    ) -> list[str]:
        """Say which jobs notify through a secret the environment does not have.

        Only the name is looked up, so nothing is decrypted and no key is needed. A deploy is
        not refused for it: the secret may be created next.

        Parameters
        ----------
        environment_id
            The environment.
        environment_name
            The environment's name.
        manifest
            The manifest whose jobs are to be current.
        dry_run
            Word the sentences as what a deploy would do, not what it did.

        Returns
        -------
        list of str
            A sentence for each such job, in job-name order.
        """
        wanted = {
            name: notify
            for name in sorted(manifest_jobs(manifest))
            if (notify := job_notify(manifest, name)) is not None
        }
        if not wanted:
            return []
        state = {
            secret_name: (revoked_at is not None, use_kind, use_target_id)
            for secret_name, revoked_at, use_kind, use_target_id in self._session.execute(
                select(Secret.name, Secret.revoked_at, Secret.use_kind, Secret.use_target_id).where(
                    Secret.environment_id == environment_id,
                    Secret.name.in_({notify.webhook for notify in wanted.values()}),
                )
            )
        }
        effect = "would not notify" if dry_run else "does not notify"

        def create(secret: str) -> str:
            return (
                f'printf \'{{"url": "https://hooks.example.com/..."}}\' | lakana secret create '
                f'{secret} --env {environment_name} --values-stdin (add "signing_key" to '
                "the object to sign the json format)"
            )

        warnings = []
        for name, notify in wanted.items():
            head = f"job `{name}` notifies through secret `{notify.webhook}`, which "
            if notify.webhook not in state:
                warnings.append(
                    f"{head}does not exist in `{environment_name}`; it {effect} until you "
                    f"create it: {create(notify.webhook)}"
                )
            elif state[notify.webhook][0]:
                warnings.append(
                    f"{head}is revoked in `{environment_name}` (a revoked name cannot be "
                    f"created or rotated again); it {effect} until you create a secret under "
                    f"another name and put that name in its `notify.webhook`, then deploy: "
                    f"{create('NEW-NAME')}"
                )
            elif state[notify.webhook][1] not in (None, SecretUseKind.ENVIRONMENT):
                _, use_kind, target_id = state[notify.webhook]
                current = describe_use(self._session, use_kind, target_id)
                warnings.append(
                    f"{head}is bound to {current}, not to its environment, so Lakana does not "
                    f"send it to a webhook; it {effect} until you give the job a secret of its "
                    f"own (put its name in `notify.webhook`, then deploy: {create('NEW-NAME')}) "
                    f"or, if this secret really belongs to the environment, an administrator "
                    f"binds it, and {current} then loses it: "
                    f"{bind_command(environment_name, notify.webhook, SecretUse.environment())}"
                )
        return warnings

    def _existing_job_version(
        self, environment_id: uuid.UUID, repository_id: uuid.UUID, sha: str
    ) -> JobVersion | None:
        """Look up the project version of a commit in an environment.

        Parameters
        ----------
        environment_id
            The environment.
        repository_id
            The repository.
        sha
            The commit.

        Returns
        -------
        JobVersion or None
            The row, or `None` if the commit was never recorded here.
        """
        return self._session.execute(
            select(JobVersion).where(
                JobVersion.repository_id == repository_id,
                JobVersion.environment_id == environment_id,
                JobVersion.commit_sha == sha,
            )
        ).scalar_one_or_none()

    def record_deploy(
        self,
        p: PrincipalRef,
        run: DeployRun,
        *,
        manifest: dict[str, Any],
        code_uri: str,
        code_sha256: str,
        diff: DeployDiff,
        expected_job_version_id: uuid.UUID | None,
        worker_id: str | None,
        checks: Mapping[str, object] | None = None,
        code_bytes: int | None = None,
    ) -> JobVersion:
        """Record a successful deploy in one transaction.

        Locks the run and checks it is still `running` under `worker_id` (`_hold`), then
        inserts or reuses the project version, compare-and-swaps the pointer, marks the run
        succeeded and writes the `DEPLOYED` audit event. When the pointer moved to another project
        version, the jobs with ``on_deploy: true`` are stored as pending starts
        (`job_starts_json`), which the worker makes after the commit
        (`lakana_core.jobs.on_deploy.start_jobs_after_deploy`). Check and writes are one
        transaction, so a deploy whose run the scheduler ended meanwhile writes nothing. The
        swap runs in the same transaction as the insert, so the loser of a
        race rolls back and leaves no project version behind.

        Parameters
        ----------
        p
            The deploying principal.
        run
            The running deploy, `resolved_sha` set.
        manifest
            The validated manifest.
        code_uri
            Where the code tarball was uploaded.
        code_sha256
            Its digest.
        diff
            Against the project version the worker observed as current.
        expected_job_version_id
            That project version's id, or `None` for a first deploy.
        worker_id
            The id the worker claimed the run under (`None` if it recorded none).
        checks
            The checks the deploy found, per table (`checks_listing`); `None` for none.
        code_bytes
            The size of the code archive (`lakana env usage` adds it up); `None` when the deploy
            reuses an existing project version, whose row keeps its own.

        Returns
        -------
        JobVersion
            The now-current project version.

        Raises
        ------
        ConflictError
            If the run is no longer `running` under `worker_id` (`DEPLOY_ENDED_MESSAGE`), or
            another deploy moved the pointer first; nothing is written.
        ValidationError
            If another repository's current project version now produces one of the
            manifest's tables (checked under a per-environment lock, so two
            repositories cannot both claim a new table); nothing is written.
        ProjectValidationError
            If an asset declares a connection the environment no longer has (read
            ``FOR SHARE``, so a concurrent delete is ordered before or after); nothing is
            written.
        NotFoundError
            If the repository or environment was deleted meanwhile.
        """
        names = self._require_alive(run)
        self._require_kind(run, DeployKind.DEPLOY)
        self._hold(run, worker_id)
        environment_id, repository_id = run.environment_id, run.repository_id
        sha = run.resolved_sha
        assert sha is not None  # nosec B101 -- set by the worker before recording
        self._claim_tables(environment_id, repository_id, manifest_tables(manifest), names[0])
        self._require_connections(environment_id, names[0], manifest)
        version = self._existing_job_version(environment_id, repository_id, sha)
        if version is None:
            version = JobVersion(
                id=uuid.uuid4(),
                repository_id=repository_id,
                environment_id=environment_id,
                commit_sha=sha,
                manifest_json=manifest,
                code_uri=code_uri,
                code_sha256=code_sha256,
                code_bytes=code_bytes,
                deployed_by=p.id,
            )
            self._session.add(version)
            try:
                self._session.flush()
            except IntegrityError:
                self._session.rollback()
                # The rollback released the run's lock, the ownership lock and the connection
                # rows' share locks; take them again (run first, as above) and re-check before
                # the pointer swap below commits anything.
                self._hold(run, worker_id)
                self._claim_tables(
                    environment_id, repository_id, manifest_tables(manifest), names[0]
                )
                self._require_connections(environment_id, names[0], manifest)
                version = self._existing_job_version(environment_id, repository_id, sha)
                if version is None:
                    raise NotFoundError(_GONE_MESSAGE) from None
                run.resolved_sha = sha
        previous = self.current_job_version(environment_id, repository_id)
        self._swap_pointer(environment_id, repository_id, expected_job_version_id, version.id)
        # The jobs start only when the deploy made another project version current: deploying the
        # commit that is already current changes nothing, so it starts nothing.
        run.job_starts_json = (
            pending_job_starts(version.manifest_json)
            if previous is None or previous.id != version.id
            else None
        )
        finished = datetime.now(UTC)
        run.warnings_json = self._sync_job_schedules(
            environment_id,
            repository_id,
            names[0],
            previous.manifest_json if previous is not None else None,
            manifest,
            db_now(self._session),
        )
        sync_project_descriptions(self._session, environment_id, manifest)
        clear_removed_models(
            self._session,
            environment_id,
            previous.manifest_json if previous is not None else None,
            manifest,
        )
        run.job_version_id = version.id
        run.diff_json = diff.to_json()
        run.checks_json = dict(checks) if checks else None
        run.status = DeployStatus.SUCCEEDED
        run.finished_at = finished
        self._finish(lambda: self._audit(p, run, names, AuditEventType.DEPLOYED, version, diff))
        return version

    def record_dry_run(
        self,
        p: PrincipalRef,
        run: DeployRun,
        *,
        diff: DeployDiff,
        worker_id: str | None,
        checks: Mapping[str, object] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        """Record a successful dry run: the diff, warnings and an audit event, nothing else.

        Parameters
        ----------
        p
            The deploying principal.
        run
            The running dry run.
        diff
            The diff it would have applied.
        worker_id
            The id the worker claimed the run under (`None` if it recorded none).
        checks
            The checks it found, per table (`checks_listing`); `None` for none.
        manifest
            The manifest the deploy would make current; its job warnings are stored as
            conditionals. `None` for none.

        Raises
        ------
        ConflictError
            If the run is no longer `running` under `worker_id` (`DEPLOY_ENDED_MESSAGE`);
            nothing is written.
        NotFoundError
            If the repository or environment was deleted meanwhile.
        """
        names = self._require_alive(run)
        self._require_kind(run, DeployKind.DRY_RUN)
        self._hold(run, worker_id)
        if manifest is not None:
            run.warnings_json = self.job_schedule_warnings(
                run.environment_id, run.repository_id, names[0], manifest, dry_run=True
            )
        run.diff_json = diff.to_json()
        run.checks_json = dict(checks) if checks else None
        run.status = DeployStatus.SUCCEEDED
        run.finished_at = datetime.now(UTC)
        self._finish(lambda: self._audit(p, run, names, AuditEventType.DEPLOY_DRY_RUN, None, diff))

    def execute_rollback(
        self, p: PrincipalRef, run: DeployRun, *, worker_id: str | None
    ) -> JobVersion:
        """Move the pointer back to the run's target, without fetching anything.

        Parameters
        ----------
        p
            The run's principal (already re-authorized by the caller).
        run
            The running rollback.
        worker_id
            The id the worker claimed the run under (`None` if it recorded none).

        Returns
        -------
        JobVersion
            The now-current project version.

        Raises
        ------
        ValidationError
            If the run is not a rollback, the target is gone or belongs to
            another environment or repository, or the target's tables are now
            produced by another repository; a `ProjectValidationError` if the target
            declares a connection the environment no longer has.
        ConflictError
            If the run is no longer `running` under `worker_id` (`DEPLOY_ENDED_MESSAGE`),
            another deploy moved the pointer first, or a `to_previous`
            rollback's target is no longer the previous version; nothing is
            written.
        NotFoundError
            If the repository or environment was deleted meanwhile.
        """
        names = self._require_alive(run)
        self._require_kind(run, DeployKind.ROLLBACK)
        self._hold(run, worker_id)
        environment_id, repository_id = run.environment_id, run.repository_id
        target_id = run.rollback_to_job_version_id
        target = (
            self._session.execute(
                select(JobVersion).where(JobVersion.id == target_id)
            ).scalar_one_or_none()
            if target_id is not None
            else None
        )
        if target is None:
            raise ValidationError("The rollback target no longer exists.")
        if target.environment_id != environment_id or target.repository_id != repository_id:
            raise ValidationError(
                "The rollback target does not belong to this environment and repository."
            )
        if run.requested_ref is None and (
            self._previous_job_version_id(environment_id, repository_id) != target.id
        ):
            self._session.rollback()
            raise ConflictError(CONFLICT_MESSAGE)
        self._claim_tables(
            environment_id, repository_id, manifest_tables(target.manifest_json), names[0]
        )
        self._require_connections(environment_id, names[0], target.manifest_json)
        current = self.current_job_version(environment_id, repository_id)
        diff = diff_manifests(
            current.manifest_json if current is not None else None,
            target.manifest_json,
            existing_unowned=set(),
        )
        self._swap_pointer(
            environment_id, repository_id, current.id if current is not None else None, target.id
        )
        finished = datetime.now(UTC)
        run.warnings_json = self._sync_job_schedules(
            environment_id,
            repository_id,
            names[0],
            current.manifest_json if current is not None else None,
            target.manifest_json,
            db_now(self._session),
        )
        sync_project_descriptions(self._session, environment_id, target.manifest_json)
        clear_removed_models(
            self._session,
            environment_id,
            current.manifest_json if current is not None else None,
            target.manifest_json,
        )
        run.resolved_sha = target.commit_sha
        run.job_version_id = target.id
        run.diff_json = diff.to_json()
        run.status = DeployStatus.SUCCEEDED
        run.finished_at = finished
        self._finish(lambda: self._audit(p, run, names, AuditEventType.ROLLED_BACK, target, diff))
        return target

    def fail(self, run: DeployRun, message: str, *, worker_id: str | None) -> None:
        """Mark a run failed.

        A `queued` run is accepted on purpose: nobody claimed it, and a run whose principal is
        inactive is failed before any worker takes it (`worker_id` is then `None`). A `running`
        run is failed only by the worker it is claimed by; one the scheduler already ended
        keeps its message and gets no second outcome.

        Parameters
        ----------
        run
            The run.
        message
            Already-redacted error text.
        worker_id
            The id the worker claimed the run under; `None` for a run nobody claimed.

        Raises
        ------
        ConflictError
            If the run is no longer `queued`, or `running` under `worker_id`
            (`DEPLOY_ENDED_MESSAGE`); nothing is written.
        NotFoundError
            If the run's repository or environment was deleted meanwhile.
        """
        self._require_alive(run)
        self._hold(run, worker_id, queued_too=True)
        run.status = DeployStatus.FAILED
        run.error_message = message
        run.finished_at = datetime.now(UTC)
        self._finish()
