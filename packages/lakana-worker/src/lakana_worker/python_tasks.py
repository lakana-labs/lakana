"""One attempt of a Python asset in the worker.

Three threads take part, and each owns its own database session:

- the **supervisor** (``lakana-python-task``) prepares the attempt (code
  integrity, run principal, upstreams, namespaces), unpacks the code into a
  private per-attempt directory, checks the lockfile, gets the venv, reads
  the target's state, mints a token bound to this attempt and calls the
  runner. It is the thread that starts the process, and it outlives it: the
  runner returns only after the process is reaped. On every path out it
  revokes the token, releases the venv and removes the attempt directory.
  Under DockerRunner (`environments` set) the environment comes from the
  launcher and the process is a container; the steps around it are the same;
- the **lease keeper** (``lakana-lease``, unchanged) renews the lease
  and learns of a cancel request, a lost lease or the drain. `KeeperStop`
  turns that into the runner's stop signal;
- the worker's **main thread** meanwhile serves query runs, and only query
  runs: the task's own input queries are among them, so a single worker can
  complete a run. It finishes the query run in hand after the supervisor has
  ended, and only then is the outcome recorded, by `tasks.run_claimed`.

The worker does not trust what the process reports. A ``succeeded`` record
counts only if the target table changed, which the worker reads itself (for an
artifact asset: only if the server holds a version row, deleted since or not,
that names this task run and this claim, which the worker reads itself; the
record's version is a hint, and a version an earlier attempt committed never
counts); a
stop reason or a lost lease is consulted before a failure record is
believed, because a bound token stops working the moment its attempt is
cancelled or reclaimed and the process then fails for that reason alone.

Nothing in here runs user code in the worker process.
"""

from __future__ import annotations

import contextlib
import logging
import os
import posixpath
import re
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import accumulate
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.artifacts.store import attempt_version, kind_of, run_input_version
from lakana_core.auth import PrincipalRef, RunContext
from lakana_core.catalog.attempt_stamp import created_by_task_run, written_by_attempt
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.catalog.metadata import read_metadata
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.writes import SnapshotPin, target_changed_message
from lakana_core.connections.endpoints import MOUNT_KINDS, PROXY_KINDS, endpoints
from lakana_core.connections.fetch import connection_secret
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.errors import ConflictError, LakanaError, NotFoundError, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.kept_mark import KeptSeen
from lakana_core.models import (
    LIVE_ENVIRONMENT,
    Connection,
    Environment,
    JobVersion,
    Principal,
    Run,
    Secret,
    Table,
    TableType,
    WriteMode,
)
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.lockfile import (
    LOCK_FILE,
    PYPROJECT_FILE,
    UV_TOML_FILE,
    UV_TOML_MESSAGE,
    lock_digest,
    lockfile_issues,
    source_issues,
    task_interpreter,
    used_indexes,
)
from lakana_core.projects.manifest import (
    asset_kind,
    entry_connections,
    entry_decimals,
    entry_input_kinds,
    entry_output,
    manifest_lock_sha256,
    manifest_python,
)
from lakana_core.projects.package_indexes import registered_indexes
from lakana_core.projects.package_sources import KnownSources, RegisteredIndex
from lakana_core.runs.checks import RUN_CANCELLED_MESSAGE
from lakana_core.runs.failures import FailureCode
from lakana_core.runs.tasks import (
    Baseline,
    ClaimedTask,
    FailureKind,
    Written,
    record_baseline,
)
from lakana_core.secrets.forms import secret_forms
from lakana_core.secrets.keys import KeyProvider
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.attempt_sweep import AttemptSweeper
from lakana_worker.caches import CacheKeeper
from lakana_worker.deploy import platform_version
from lakana_worker.deploy_sweep import DeploySweeper
from lakana_worker.docker_environments import DockerEnvironments
from lakana_worker.index_credentials import (
    IndexAccess,
    IndexCredentialError,
    resolve_index_access,
)
from lakana_worker.launcher.requests import (
    INGEST_MOUNT,
    MAX_EGRESS,
    MAX_RELAY_ROUTES,
    MAX_ROOTS,
    RELAY_PORTS,
)
from lakana_worker.launcher_client import LogSinkFailed
from lakana_worker.processes import (
    CappedSink,
    UnsafeDirectory,
    prepare_private_directory,
    remove_private_tree,
)
from lakana_worker.python_support import CANNOT_RUN, PythonSupport
from lakana_worker.runners import (
    MAX_ERROR_MESSAGE_CHARS,
    MAX_TASK_OUTPUT_BYTES,
    MISSING_ROOT,
    CodeUnpackError,
    CodeUnpackFailed,
    PythonTask,
    Runner,
    RunOutcome,
    StopReason,
    StopSignal,
    unpack_code,
)
from lakana_worker.task_integrity import check_manifest_version
from lakana_worker.task_lease import LeaseKeeper, LeaseLost, cancel_kind
from lakana_worker.task_outcome import (
    RUN_ERROR_MESSAGE,
    KeptTable,
    TaskFailure,
    TaskOutcome,
    classify,
    failure_code_of,
    failure_message,
    stop_code,
)
from lakana_worker.tasks import (
    RunEngine,
    check_integrity,
    checked_write,
    ensure_namespaces,
    manifest_entry,
    may_read_name,
    require_upstreams,
)
from lakana_worker.venvs import (
    AcquireVenv,
    IndexResolver,
    VenvBuildBroken,
    VenvBuildError,
    VenvBuildRefused,
    VenvBuildStopped,
    VenvCacheUnsafe,
    VenvLease,
    VenvSettings,
    acquire,
)

if TYPE_CHECKING:
    from pyiceberg.table.metadata import TableMetadata

logger = logging.getLogger(__name__)

THREAD_NAME = "lakana-python-task"
"""The supervisor thread's name."""

_IDLE_SECONDS = 0.2
"""How long the main loop waits for the supervisor when it served no query."""

_TOKEN_MARGIN_SECONDS = 60
"""Token lifetime beyond the asset timeout and the kill grace."""

_MAX_ISSUES_CHARS = 1500
"""Most lockfile-issue text put before the advice to redeploy, so the advice is never cut off."""

_REGISTERED_AGAIN = (
    "The package indexes registered on this instance no longer match this project version: "
    "register the index again (`lakana index list` shows what is registered) and start the "
    "run again, or change the project as the message says and deploy again."
)
"""The advice for a lock that names an index which was removed or changed after the deploy."""

_DECLARED_AGAIN = (
    "A package index is registered on this instance that the project declares in a way that "
    "is not allowed: declare it as a `[[tool.uv.index]]` table with `explicit = true`, name its "
    "packages under `[tool.uv.sources]`, run `uv lock` and deploy again."
)
"""The advice for a declaration in `pyproject.toml` (`explicit`, `index-url`,
`extra-index-url`) that the registry now makes an error; registering the index again does
not help."""

_MIXED_ALSO = (
    "The lock also names a package index that is not registered on this instance: an "
    "administrator registers it (`lakana index list` shows what is registered), or change the "
    "project so that it does not need it; deploy again after that."
)
"""Added after `_DECLARED_AGAIN` when both kinds of issue are present: the run cannot pass
before the deploy."""

_LOCAL_PYTHON = (
    "this project names Python {python} in lakana.yaml, and this worker runs Python tasks on "
    "its own Python {own} without containers (LAKANA_RUN__RUNNER=local). Run it on a worker "
    'with LAKANA_RUN__RUNNER=docker, or set python: "{own}" (or remove it) in lakana.yaml and '
    "deploy again"
)
"""The failure of a project that names a Python the local runner does not have."""

_PROTECTED = (
    "environment '{environment}' is protected, and this worker runs Python tasks without "
    "isolation (LAKANA_RUN__RUNNER=local). Run the task on a worker with "
    'LAKANA_RUN__RUNNER=docker; the how-to "Run Python tasks in containers" says how.'
)
"""The failure of a Python task of a protected environment on a local-runner worker."""

_BUILD_STOPPED = (
    "environment build failed: the build was stopped, but neither a cancel, a drain nor a lost "
    "lease was found; the task is tried again"
)
_BUILD_LOG_FAILED = (
    "environment build failed: the task's log could not be written while its environment was "
    "built; the worker's log has the cause"
)
"""The failure of a launcher build whose output the task's log sink would not take."""

_INACTIVE = "the run principal is deactivated or deleted"
_NO_MESSAGE = "the task failed without a message"
_NO_WRITE = (
    "the task reported success without writing its table; return the DataFrame from the "
    "asset function and do not exit the process or write the result file yourself"
)
_NOT_ITS_WRITE = (
    "the task reported success, but {table} was changed by another writer, not by this attempt; "
    "return the DataFrame from the asset function and do not exit the process or write the "
    "result file yourself"
)
_NO_VERSION = (
    "the task reported success without storing a version of its artifact; return the path from "
    "the asset function and do not exit the process or write the result file yourself"
)
_KIND_WORDS: Mapping[str, str] = {
    "TABLE": "a table",
    "VIEW": "a view",
    "EXTERNAL": "an external table",
}
"""How a catalog kind is named in a message; an artifact is never looked up here."""
_STOPPED: Mapping[FailureKind, str] = {
    FailureKind.CANCELLED: RUN_CANCELLED_MESSAGE,
    FailureKind.RELEASED: f"stopped: {StopReason.DRAIN.value}",
}
"""The message recorded for an attempt the worker stopped, by how it is recorded.

A cancel normally carries the run's text from `cancel_kind`; this one is the fallback."""


def environment_owner(repository_id: uuid.UUID, environment_id: uuid.UUID) -> uuid.UUID:
    """Name the project whose tasks may share an environment the launcher built.

    A project is a repository deployed to an environment. Its lock pins every package from an
    index by hash, but not the files of a path source in the repository, so a deployer of one
    environment could change vendored code without changing the lock; keying the build by the
    repository alone would hand that code to the same repository's tasks in another
    environment, with that environment's token. The id is therefore derived from both.

    Parameters
    ----------
    repository_id
        The project version's repository.
    environment_id
        The environment it is deployed to.

    Returns
    -------
    uuid.UUID
        A name-based UUID, the same for every version of the project in that environment.
    """
    return uuid.uuid5(repository_id, f"environment:{environment_id}")


def _stop_text(stop: tuple[FailureKind, str | None]) -> str:
    """Return the message recorded for a task that `cancel_kind` says to stop.

    Parameters
    ----------
    stop
        The kind and message `cancel_kind` returned.

    Returns
    -------
    str
        The run's own text for a cancel (a user's, or a code integrity failure's), so a Python
        task reads like an SQL one; the fixed text for a release.
    """
    return stop[1] if stop[1] is not None else _STOPPED[stop[0]]


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
"""Characters that could rewrite a terminal or reorder a line when a log is shown."""


def _listed_issues(issues: Sequence[ProjectIssue]) -> str:
    """Join issues for a failure message, in whole issues, within `_MAX_ISSUES_CHARS`.

    Parameters
    ----------
    issues
        The findings, not empty.

    Returns
    -------
    str
        The rendered issues joined with ``; `` when they fit; else the first whole issues
        that fit with ``; and N more`` after them, so the text never ends inside a command or
        a URL. When not even the first fits: the first issue cut to the bound, ending in
        ``…``, then ``; and N more`` if there are others.
    """
    rendered = [issue.render() for issue in issues]
    whole = "; ".join(rendered)
    if len(whole) <= _MAX_ISSUES_CHARS:
        return whole
    # The text up to issue `n` plus its "and N more" grows with `n`, so the issues that fit
    # are a prefix and can be counted.
    ends = accumulate(len(text) + 2 for text in rendered)
    best = sum(
        1
        for position, end in enumerate(ends)
        if end - 2 + len(f"; and {len(rendered) - position - 1} more") <= _MAX_ISSUES_CHARS
    )
    if best:
        return f"{'; '.join(rendered[:best])}; and {len(rendered) - best} more"
    more = f"; and {len(rendered) - 1} more" if len(rendered) > 1 else ""
    return rendered[0][: _MAX_ISSUES_CHARS - len(more) - 1] + "…" + more


def _closing_for(issues: Sequence[ProjectIssue]) -> str:
    """Choose the advice that ends a failure about package sources.

    Parameters
    ----------
    issues
        The findings of `source_issues`, not empty.

    Returns
    -------
    str
        What the author must do when an issue is about how `pyproject.toml` declares an index;
        what an administrator and the author may do when the lock names an index that was
        removed or changed; the author's step first, then the other, when there are both
        kinds (the next run cannot pass before the new deploy).
    """
    declared = [issue.path == PYPROJECT_FILE for issue in issues]
    if all(declared):
        return _DECLARED_AGAIN
    if not any(declared):
        return _REGISTERED_AGAIN
    return f"{_DECLARED_AGAIN} {_MIXED_ALSO}"


def _shown(value: object) -> str:
    """Show a manifest value in a message, bounded and free of control characters.

    Parameters
    ----------
    value
        Anything a manifest entry holds; untrusted.

    Returns
    -------
    str
        Its `ascii` form, cut to 60 characters.
    """
    return ascii(value)[:60]


def token_ttl(timeout: float, kill_grace: float) -> int:
    """Return the lifetime a task's bound token needs.

    Parameters
    ----------
    timeout
        The asset's timeout in seconds.
    kill_grace
        `LAKANA_RUN__KILL_GRACE`.

    Returns
    -------
    int
        The timeout plus the kill grace plus a 60 s margin. An attempt whose
        need exceeds `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS` is not started.
    """
    return int(timeout + kill_grace) + _TOKEN_MARGIN_SECONDS


class KeeperStop(StopSignal):
    """A stop signal that follows a lease keeper.

    Only the supervisor thread calls it. The keeper publishes its state
    through its `cancel` event: `lost` and `reason` are written before the
    event is set, so they are read here only once it is.

    Parameters
    ----------
    keeper
        The attempt's lease keeper.
    """

    def __init__(self, keeper: LeaseKeeper) -> None:
        super().__init__()
        self._keeper = keeper

    def is_set(self) -> bool:
        """Say whether the keeper wants the task stopped, recording why.

        Returns
        -------
        bool
            Whether the lease was lost, cancellation was requested, or the
            drain grace ran out.
        """
        if not super().is_set() and self._keeper.cancel.is_set():
            if self._keeper.lost:
                self.set(StopReason.LEASE_LOST)
            elif self._keeper.reason is FailureKind.RELEASED:
                self.set(StopReason.DRAIN)
            else:
                self.set(StopReason.CANCELLED)
        return super().is_set()


def _read_code(
    session: Session,
    key_provider: KeyProvider | None,
    environment: Environment,
    version: JobVersion,
) -> bytes:
    """Read a project version's code tarball from the environment's storage.

    Parameters
    ----------
    session
        Database session.
    key_provider
        Decrypts secret-backed storage.
    environment
        The run's environment.
    version
        The project version; `check_integrity` has established that its
        `code_uri` lies under the environment's storage root.

    Returns
    -------
    bytes
        The gzipped tar.
    """
    root = environment.storage_root.rstrip("/") + "/"
    storage = StorageResolver(session, key_provider).for_environment(environment.name).storage
    return storage.read(version.code_uri[len(root) :])


def _safe_message(text: str | None) -> str | None:
    """Make failure text safe to store, whoever produced it.

    Parameters
    ----------
    text
        A message; a runner's outcome text is the process's own words.

    Returns
    -------
    str or None
        At most `MAX_ERROR_MESSAGE_CHARS` characters, every unprintable one
        (control, format, surrogate, private use) replaced by ``?``; `None`
        stays `None`.
    """
    if text is None:
        return None
    cut = text[:MAX_ERROR_MESSAGE_CHARS]
    return "".join(c if c.isprintable() or c in "\n\t" else "?" for c in cut)


def _safe_output(buffer: bytearray) -> str | None:
    """Turn the captured process output into text for the task log.

    Parameters
    ----------
    buffer
        The redacted, capped output of the venv build and the task process.

    Returns
    -------
    str or None
        The text with undecodable bytes replaced, line ends normalised and
        control characters replaced by ``?``; `None` when nothing was written.
    """
    text = buffer.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return _CONTROL.sub("?", text) or None


@dataclass(frozen=True)
class _TargetState:
    """What the target table is, as the worker read it in-process.

    Two states are equal exactly when nothing was written in between: a first
    write of zero rows creates the table without a snapshot, so the table's
    identity is part of the state.

    Parameters
    ----------
    table_id
        The table's id, or `None` when it does not exist.
    snapshot_id
        Its current snapshot, or `None` when it has none.
    unpublished
        Whether the table is marked not published: its first build did not finish, or
        failed a check. Not part of the comparison: a write is told by the table's identity
        and snapshot alone.
    kept
        Whether the table keeps data that failed a check (it has a note). Not part of
        the comparison either.
    seen
        The note as read, for the clear that supersedes it. Not part of the comparison.
    metadata
        The table's metadata as read, for the proof of whose write it is (#49). Not part of
        the comparison.
    """

    table_id: uuid.UUID | None
    snapshot_id: int | None
    unpublished: bool = field(default=False, compare=False)
    kept: bool = field(default=False, compare=False)
    seen: KeptSeen = field(default=KeptSeen(None, None, None), compare=False)
    metadata: TableMetadata | None = field(default=None, compare=False, repr=False)


MAX_TASK_EGRESS = MAX_EGRESS
"""The most host:port pairs a task reaches through the egress proxy: what its token holds."""
MAX_TASK_RELAY = MAX_RELAY_ROUTES
"""The most database endpoints a task's relay forwards."""
MAX_TASK_ROOTS = MAX_ROOTS
"""The most ingestion roots the launcher mounts into one task."""
RELAY_FIRST_PORT = RELAY_PORTS.start
"""The relay port of a task's first database endpoint; the next ones count up from it."""


@dataclass(frozen=True)
class ConnectionPlan:
    """Where a task's declared connections let it go: exactly their endpoints, nothing more.

    Parameters
    ----------
    egress
        The ``(host, port)`` pairs of the HTTPS kinds, for the egress proxy's token (Docker
        runner), in declaration order without repeats.
    relay
        ``(listen port, host, port)`` for each endpoint of the database kinds, ports from 15001
        in declaration order (Docker runner).
    roots
        The ingestion roots the mounted kinds read, without repeats.
    root_users
        Each of `roots` to the first connection naming it.
    routing
        Each declared name to what the task description says about it: ``kind`` and ``grant``
        (the endpoints the attempt was granted) plus ``relay_port`` for a database under the
        Docker runner, or ``kind``, ``root``, ``root_dir``, ``path`` and ``root_path`` for a
        mounted kind (``root`` and ``path`` as granted, so the task notices a move).
    """

    egress: tuple[tuple[str, int], ...] = ()
    relay: tuple[tuple[int, str, int], ...] = ()
    roots: tuple[str, ...] = ()
    root_users: dict[str, str] = field(default_factory=dict)
    routing: dict[str, dict[str, object]] = field(default_factory=dict)


@dataclass(frozen=True)
class _Prepared:
    """What the run needs, as plain values (no ORM object crosses the long run).

    Parameters
    ----------
    environment
        Environment name.
    ref
        The asset's table.
    asset_path
        The asset file, relative to the code directory.
    inputs
        Parameter → table.
    code_sha256
        The code tarball's recorded digest.
    lock_sha256
        The manifest's lock digest, if it records one.
    timeout
        The attempt's timeout in seconds.
    output
        ``table`` or ``artifact``, from the manifest entry.
    input_kinds
        Parameter → ``table`` or ``artifact``, for every input.
    input_versions
        Parameter → the version of an artifact input, resolved just now: the
        one this run's own producer committed, else the current one.
    decimals
        ``float`` or ``exact``, from the manifest entry.
    sources
        The package sources this instance knew when the attempt was prepared.
    python
        The Python the task runs on: the manifest's `python`, else the worker's own.
    named_python
        The manifest's `python`, if the project names one.
    project_id
        `environment_owner` of the project version: whose environments the task may use.
    principal
        The run principal, bound to this attempt; reads the watermark.
    write
        ``replace``, ``append`` or ``merge`` (always ``replace`` for an artifact asset).
    key
        The merge key's columns.
    watermark_column
        The asset's `watermark` column, if it has one.
    plan
        Where the declared connections let the task go (`ConnectionPlan`); its `routing` is
        `connections`.
    """

    environment: str
    ref: TableRef
    asset_path: str
    inputs: dict[str, str]
    code_sha256: str
    lock_sha256: str | None
    timeout: float
    output: str
    input_kinds: dict[str, str]
    input_versions: dict[str, int]
    decimals: str
    sources: KnownSources
    python: str
    named_python: str | None
    project_id: uuid.UUID
    principal: PrincipalRef
    write: str = "replace"
    key: tuple[str, ...] = ()
    watermark_column: str | None = None
    plan: ConnectionPlan = field(default_factory=lambda: ConnectionPlan())

    @property
    def connections(self) -> dict[str, dict[str, object]]:
        """Each connection the asset declares to its routing, in the order declared.

        Returns
        -------
        dict
            `plan.routing`.
        """
        return self.plan.routing


_CANNOT_HIDE = (
    "this worker cannot decrypt the secret of connection `{name}`, so it could not hide the "
    "value in the task's log, which the server would hand the task; set "
    "LAKANA_SECRETS__MASTER_KEY on the worker to the server's value and restart it"
)


_NOT_TEXT = (
    "the secret of connection `{name}` holds a value that is not valid text, so it could not "
    "be hidden in the task's log; set it again: lakana connection update {name} --env "
    "{environment} --password-stdin (or --values-stdin)"
)


def _connection_secrets(
    session: Session, key_provider: KeyProvider | None, prepared: _Prepared
) -> tuple[str, ...]:
    """Decrypt the secrets of the connections the asset declares, to hide them in its output.

    The task fetches these values itself at run time (`lakana_core.connections.fetch`); the
    worker only needs them to mask them, in every form text can carry them in
    (`lakana_core.secrets.forms.secret_forms`), in the log it captures and in the failure
    message. Each secret is decrypted through `lakana_core.connections.fetch.connection_secret`,
    so only a secret bound to that very connection is, and one that is not counts as none on
    both sides. A worker that cannot decrypt an active secret (no master key, or another one
    than the server's) refuses to start the task: the server would hand the task a value this
    worker could not hide. This decryption is not audited: no value leaves the worker.

    Parameters
    ----------
    session
        The supervisor's session; rolled back afterwards.
    key_provider
        The worker's key provider; `None` without a master key.
    prepared
        The attempt's plan, with the declared connections.

    Returns
    -------
    tuple of str
        Every form of every value, in declared order.

    Raises
    ------
    TaskFailure
        Permanent (`NO_RUNTIME`), naming the connection and the setting, when an active secret
        bound to a declared connection cannot be decrypted here; and naming the command that
        sets it again when one of its values is not valid text (a row from before such values
        were refused).
    """
    if not prepared.connections:
        return ()
    rows = {
        row.name: row
        for row in session.execute(
            select(Connection)
            .join(Environment, Environment.id == Connection.environment_id)
            .where(
                Environment.name == prepared.environment,
                LIVE_ENVIRONMENT,
                Connection.name.in_(list(prepared.connections)),
            )
        ).scalars()
    }
    hidden: list[str] = []
    for name in prepared.connections:
        row = rows.get(name)
        if row is None:
            continue
        if key_provider is None:
            secret = None if row.secret_id is None else session.get(Secret, row.secret_id)
            if secret is not None and secret.revoked_at is None:
                raise TaskFailure(
                    FailureKind.PERMANENT, _CANNOT_HIDE.format(name=name), FailureCode.NO_RUNTIME
                )
            continue
        values, why = connection_secret(session, key_provider, row, prepared.environment)
        if why == "secret_unreadable":
            raise TaskFailure(
                FailureKind.PERMANENT, _CANNOT_HIDE.format(name=name), FailureCode.NO_RUNTIME
            )
        if why is not None:
            logger.warning(
                "the secret of connection %s is not bound to it, so the task gets none (%s)",
                name,
                why,
            )
        try:
            for value in (values or {}).values():
                hidden += sorted(secret_forms(value))
        except UnicodeEncodeError:
            raise TaskFailure(
                FailureKind.PERMANENT,
                _NOT_TEXT.format(name=name, environment=prepared.environment),
                FailureCode.NO_RUNTIME,
            ) from None
    session.rollback()
    return tuple(dict.fromkeys(hidden))


@dataclass
class _Shown:
    """What the attempt's log block says about the write, filled in as the attempt learns it.

    Parameters
    ----------
    output
        ``table`` until the manifest entry is read, then what the asset produces.
    write
        The asset's write mode.
    watermark
        The encoded watermark sent to the task, if any.
    """

    output: str = "table"
    write: str = "replace"
    watermark: str | None = None


_LOGGED_MODES = {"replace": WriteMode.REPLACE, "append": WriteMode.APPEND, "merge": WriteMode.MERGE}


@dataclass
class _Result:
    """Carries the supervisor's outcome to the main thread.

    Written by the supervisor thread before it ends and read by the main
    thread only after that thread is no longer alive, so it needs no lock.

    Parameters
    ----------
    outcome
        What to record; starts as the generic transient failure, which is
        what remains if the supervisor dies without setting it.
    """

    outcome: TaskOutcome | None


def _asset_path(entry: Mapping[str, object]) -> str | None:
    """Read a Python entry's asset file path, if it is one the harness may load.

    Parameters
    ----------
    entry
        The manifest entry; untrusted.

    Returns
    -------
    str or None
        A relative ``.py`` path that stays inside the code directory, else `None`.
    """
    path = entry.get("path")
    if not isinstance(path, str) or not path.endswith(".py"):
        return None
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        return None
    return path


def _declared_inputs(entry: Mapping[str, object]) -> dict[str, str] | None:
    """Read a Python entry's inputs strictly.

    Parameters
    ----------
    entry
        The manifest entry; untrusted.

    Returns
    -------
    dict or None
        Parameter → table; `None` unless `inputs` is a mapping of strings to strings.
    """
    raw = entry.get("inputs")
    if not isinstance(raw, dict):
        return None
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw.items()):
        return None
    return dict(raw)


def _refuse_if_stopped(session: Session, keeper: LeaseKeeper, run_id: uuid.UUID) -> None:
    """Refuse to start (or to count as lost) an attempt that was asked to stop.

    Parameters
    ----------
    session
        The supervisor's session.
    keeper
        The running lease keeper.
    run_id
        The run, whose cancel flag is re-read: the keeper learns of a cancel
        only at its next renewal.

    Raises
    ------
    LeaseLost
        If the lease is no longer this worker's.
    TaskFailure
        ``CANCELLED`` or ``RELEASED`` if the run was cancelled or the worker drains.
    """
    if keeper.lost:
        raise LeaseLost
    stop = cancel_kind(session, keeper, run_id)
    if stop is not None:
        raise TaskFailure(stop[0], _stop_text(stop), stop_code(stop[0]))


def local_root_dir(ingestion_roots: Mapping[str, str], root: str, connection: str) -> str:
    """Find the directory of an ingestion root under the local runner.

    Parameters
    ----------
    ingestion_roots
        The worker's roots (`LAKANA_RUN__INGESTION_ROOTS`): name to absolute path.
    root
        The root's name.
    connection
        The connection naming it, for the message.

    Returns
    -------
    str
        The worker's own directory for the root.

    Raises
    ------
    TaskFailure
        Permanent when the worker has no such root (`MISSING_ROOT`).
    """
    directory = ingestion_roots.get(root)
    if directory is None:
        raise TaskFailure(
            FailureKind.PERMANENT,
            MISSING_ROOT.format(connection=connection, root=root),
            FailureCode.NO_RUNTIME,
        )
    return directory


def connection_routing(
    name: str,
    kind: ConnectionKind,
    address: Mapping[str, object],
    settings: Mapping[str, object],
    *,
    root_dir: Callable[[str], str],
) -> dict[str, object]:
    """Describe one connection to a task: its kind and what it may reach.

    The part of a task's routing that depends on the connection alone; a task's plan and the
    connection test both build it, so they cannot disagree. A runner-specific ``relay_port`` is
    added by the caller.

    Parameters
    ----------
    name
        The connection's name, for the message of a missing root.
    kind
        Its kind.
    address
        Its validated address.
    settings
        Its validated settings.
    root_dir
        Maps a root's name to the directory a task reads it from; called for the mounted kinds
        only, and it may raise `TaskFailure`.

    Returns
    -------
    dict
        ``kind`` and the ``grant`` of host and port pairs; for a mounted kind ``kind``,
        ``root``, ``root_dir``, ``path`` and ``root_path``.
    """
    if kind in MOUNT_KINDS:
        root, path = str(address["root"]), str(address["path"])
        directory = root_dir(root)
        return {
            "kind": kind.value,
            "root": root,
            "root_dir": directory,
            "path": path,
            "root_path": posixpath.join(directory, path),
        }
    found = endpoints(kind, address, settings)
    return {"kind": kind.value, "grant": [[e.host, e.port] for e in found]}


def connection_plan(
    connections: Sequence[tuple[str, ConnectionKind, Mapping[str, object], Mapping[str, object]]],
    *,
    isolated: bool,
    root_dir: Callable[[str, str], str],
) -> ConnectionPlan:
    """Plan where a task with these connections may go; a connection test plans one the same way.

    The one place that turns connections into routes, so a task and the Docker runner's
    connection check of one of its connections cannot reach different things.

    Parameters
    ----------
    connections
        ``(name, kind, address, settings)`` in declaration order, validated.
    isolated
        Whether the runner is the Docker runner: HTTPS kinds then go through the egress proxy
        (``egress``) and database and ``tcp`` kinds through the relay (``relay``, ports from
        15001, ``relay_port`` in the routing); the local runner connects directly.
    root_dir
        Maps a root and the connection naming it to the directory a task reads it from; it may
        raise `TaskFailure`.

    Returns
    -------
    ConnectionPlan
        The routes, without the per-task caps (the caller checks them).
    """
    egress: list[tuple[str, int]] = []
    relay: list[tuple[int, str, int]] = []
    root_users: dict[str, str] = {}
    routing: dict[str, dict[str, object]] = {}
    for name, kind, address, settings in connections:
        routing[name] = connection_routing(
            name,
            kind,
            address,
            settings,
            root_dir=lambda root, name=name: root_dir(root, name),
        )
        if kind in MOUNT_KINDS:
            root_users.setdefault(str(address["root"]), name)
            continue
        found = endpoints(kind, address, settings)
        if not isolated:
            continue
        if kind in PROXY_KINDS:
            for e in found:
                if (e.host, e.port) not in egress:
                    egress.append((e.host, e.port))
        else:  # RELAY_KINDS: the three sets cover every kind (test_connection_endpoints)
            routing[name]["relay_port"] = RELAY_FIRST_PORT + len(relay)
            for e in found:
                relay.append((RELAY_FIRST_PORT + len(relay), e.host, e.port))
    return ConnectionPlan(
        egress=tuple(egress),
        relay=tuple(relay),
        roots=tuple(root_users),
        root_users=root_users,
        routing=routing,
    )


@dataclass
class PythonRuntime:
    """Runs claimed Python tasks; one instance serves the worker's lifetime.

    Parameters
    ----------
    support
        Whether this worker can run Python assets, and the server URL.
    work_dir
        Where each attempt gets its private directory (unpacked code, `HOME`,
        `TMPDIR`, the result file, an artifact task's `inputs` and `output`).
        Created 0700 if missing; nothing in it outlives its attempt. A
        relative path is fine: each attempt's directory is made absolute, which
        the harness insists on.
    venvs
        The venv cache's settings.
    runner
        Runs the task process.
    kill_grace
        `LAKANA_RUN__KILL_GRACE`, part of the token's lifetime.
    max_ttl_seconds
        `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS`.
    limits
        Bounds on the unpacked code.
    acquire_venv
        Returns the venv of a lockfile; `lakana_worker.venvs.acquire`. The last positional
        argument returns the credentials of the registered indexes the lock uses and is called
        only if a build runs; `stop` ends a build when the task is to stop.
    sweeper
        Removes attempt directories a dead worker left behind; told which
        directories this worker's running tasks hold. `None` sweeps nothing.
    deploy_sweeper
        Removes deploy checkouts a dead worker left in the temp directory.
        `None` sweeps nothing.
    caches
        Measures the worker's caches and trims uv's package cache, in a thread of its own;
        `None` measures nothing.
    environments
        Gets each task's environment from the launcher (`LAKANA_RUN__RUNNER=docker`); `None`
        builds venvs locally with `acquire_venv`, the local runner.
    ingestion_roots
        `LAKANA_RUN__INGESTION_ROOTS`: root name to the directory a local task reads it from.
        The Docker runner ignores it: the launcher mounts its own roots at ``/ingest/<root>``.
    """

    support: PythonSupport
    work_dir: Path
    venvs: VenvSettings
    runner: Runner
    kill_grace: float = 10.0
    max_ttl_seconds: int = 86_400
    limits: DeployLimits = field(default_factory=DeployLimits)
    acquire_venv: AcquireVenv = acquire
    sweeper: AttemptSweeper | None = None
    deploy_sweeper: DeploySweeper | None = None
    caches: CacheKeeper | None = None
    environments: DockerEnvironments | None = None
    ingestion_roots: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse a runner and environments that disagree about isolation.

        Raises
        ------
        ValueError
            For a container runner without environments, or the local runner with them.
        """
        if self.runner.isolated and self.environments is None:
            raise ValueError(
                "the Python runtime's runner runs tasks in containers but has no environments; "
                "pass the launcher's DockerEnvironments with a DockerRunner"
            )
        if not self.runner.isolated and self.environments is not None:
            raise ValueError(
                "the Python runtime's runner runs tasks without containers but has environments; "
                "use a DockerRunner with DockerEnvironments, or no environments"
            )

    @property
    def isolated(self) -> bool:
        """Whether each task runs in a container of its own (`LAKANA_RUN__RUNNER=docker`).

        Returns
        -------
        bool
            The runner's `isolated`; `__post_init__` keeps `environments` in step with it.
        """
        return self.runner.isolated

    def maintain(self) -> None:
        """Sweep leftover attempt directories and deploy checkouts, and measure the caches."""
        if self.sweeper is not None:
            self.sweeper.maybe_sweep()
        if self.deploy_sweeper is not None:
            self.deploy_sweeper.maybe_sweep()
        if self.caches is not None:
            self.caches.maybe_start()

    def attempt(
        self,
        engine: RunEngine,
        claimed: ClaimedTask,
        keeper: LeaseKeeper,
        environment: str | None,
        serve_queries: Callable[[], bool] | None,
    ) -> TaskOutcome | None:
        """Run one attempt in a supervisor thread while this thread serves query runs.

        Returns once the supervisor thread has ended and the query run in
        hand, if any, has finished. The supervisor's own waits are bounded
        (the venv build by its timeout, the process by the asset timeout and
        the kill grace), and this thread never waits longer than
        `_IDLE_SECONDS` without looking again.

        Parameters
        ----------
        engine
            The run engine.
        claimed
            The claim.
        keeper
            The running lease keeper.
        environment
            Environment name for the log, or `None` when the run is gone.
        serve_queries
            Runs one query-only poll iteration and says whether it ran a
            query; `None` serves nothing.

        Returns
        -------
        TaskOutcome or None
            What to record, or `None` when the attempt is no longer ours.
        """
        result = _Result(
            TaskOutcome(
                FailureKind.TRANSIENT,
                RUN_ERROR_MESSAGE,
                environment,
                failure_code=FailureCode.INTERNAL,
            )
        )

        def body() -> None:
            """Run the attempt and hand its outcome to the main thread."""
            result.outcome = self._attempt(engine, claimed, keeper, environment)

        thread = threading.Thread(target=body, name=THREAD_NAME, daemon=True)
        thread.start()
        while thread.is_alive():
            if not self._serve(engine, serve_queries):
                thread.join(_IDLE_SECONDS)
        return result.outcome

    @staticmethod
    def _serve(engine: RunEngine, serve_queries: Callable[[], bool] | None) -> bool:
        """Serve at most one query run, unless the worker is draining.

        Parameters
        ----------
        engine
            The run engine, for its drain event.
        serve_queries
            Runs one query-only poll iteration.

        Returns
        -------
        bool
            Whether a query run was executed; `False` once the drain is set,
            and for a callback that raised (logged), so the task is never
            abandoned by its main thread.
        """
        if serve_queries is None or engine.drain.is_set():
            return False
        try:
            return serve_queries()
        except Exception:
            logger.exception("Serving a query run during a Python task failed; continuing.")
            return False

    def _attempt(
        self, engine: RunEngine, claimed: ClaimedTask, keeper: LeaseKeeper, environment: str | None
    ) -> TaskOutcome | None:
        """Run the attempt on this thread's own session; never raises.

        An unexpected error, before or after the process ran, is a generic
        transient failure that keeps the output captured so far.

        Parameters
        ----------
        engine
            The run engine.
        claimed
            The claim.
        keeper
            The running lease keeper.
        environment
            Environment name for the log.

        Returns
        -------
        TaskOutcome or None
            The outcome, or `None` when the lease was lost.
        """
        buffer = bytearray()
        sink = CappedSink(buffer.extend, MAX_TASK_OUTPUT_BYTES)
        shown = _Shown()

        def ended(
            kind: FailureKind | None,
            message: str | None,
            rows: int | None = None,
            snapshot_id: int | None = None,
            artifact_version: int | None = None,
            written: Written | None = None,
            kept_table: KeptTable | None = None,
            in_transaction: Callable[[Session], None] | None = None,
            failure_code: FailureCode | None = None,
            wrote_nothing: bool = False,
        ) -> TaskOutcome:
            """Build the outcome, with the output captured so far.

            Parameters
            ----------
            kind
                `None` for success, else how the attempt failed.
            message
                The failure text; sanitised and capped here.
            rows
                The rows a successful attempt reports.
            snapshot_id
                The snapshot the worker found after a successful attempt.
            artifact_version
                The version of its artifact the worker found this attempt committed.
            written
                What a successful table attempt did to its table, as the worker read it.
            kept_table
                The table a successful attempt of a task without checks replaced, with the note
                it had before the process ran, when it kept data that failed a check: that note
                is cleared.
            in_transaction
                Work to land with the attempt's record (a failed integrity check's run failure).
            failure_code
                Why the attempt failed; `None` for a success and a release.
            wrote_nothing
                A success whose task run committed nothing to the table.

            Returns
            -------
            TaskOutcome
                The outcome to record.
            """
            return TaskOutcome(
                kind,
                _safe_message(message),
                environment,
                None if shown.output == "artifact" else _LOGGED_MODES.get(shown.write),
                watermark=shown.watermark,
                rows=rows,
                output=_safe_output(buffer),
                snapshot_id=snapshot_id,
                artifact_version=artifact_version,
                written=written,
                kept_table=kept_table,
                in_transaction=in_transaction,
                failure_code=failure_code,
                wrote_nothing=wrote_nothing,
            )

        try:
            with engine.session_factory() as session:
                try:
                    return self._supervise(session, engine, claimed, keeper, sink, ended, shown)
                except LeaseLost:
                    return None
                except TaskFailure as failure:
                    return ended(
                        failure.kind,
                        failure.message,
                        in_transaction=failure.in_transaction,
                        failure_code=failure.code,
                    )
                except LakanaError as exc:
                    stop = cancel_kind(session, keeper, claimed.run_id)
                    if stop is not None:
                        return ended(stop[0], _stop_text(stop), failure_code=stop_code(stop[0]))
                    return ended(
                        classify(exc),
                        failure_message(exc, str(exc)),
                        failure_code=failure_code_of(exc),
                    )
        except Exception:
            logger.exception("Python task run %s failed unexpectedly.", claimed.task_run_id)
            return ended(
                FailureKind.TRANSIENT, RUN_ERROR_MESSAGE, failure_code=FailureCode.INTERNAL
            )

    def _supervise(
        self,
        session: Session,
        engine: RunEngine,
        claimed: ClaimedTask,
        keeper: LeaseKeeper,
        sink: Callable[[bytes], None],
        ended: Callable[..., TaskOutcome],
        shown: _Shown,
    ) -> TaskOutcome | None:
        """Prepare, run the process, clean up on every path, then classify.

        Cleanup runs in reverse order of acquisition whatever happened, each
        step even if another raised: the token is revoked, the venv lease
        released, and the attempt directory removed. The runner has returned
        by then, so the process is dead and reaped.

        Parameters
        ----------
        session
            The supervisor's session.
        engine
            The run engine.
        claimed
            The claim.
        keeper
            The running lease keeper.
        sink
            Receives the venv build's and the process's output; one cap covers both.
        ended
            Builds the outcome to record.
        shown
            What `ended` logs about the write: what the asset produces (an artifact asset has
            no write mode), its write mode and the watermark sent to the task.

        Returns
        -------
        TaskOutcome or None
            The outcome, or `None` when the lease was lost.

        Raises
        ------
        TaskFailure
            For every expected failure before the process starts, with its kind
            (permanent for an asset that reads its own table before it exists).
        LeaseLost
            If the attempt was superseded before its token could be minted.
        LakanaError
            From namespace creation or unreadable table metadata; `ConflictError` when the
            table changed between the worker's read of it and of its watermark.
        """
        prepared, tarball = self._prepare(session, engine, claimed)
        shown.output, shown.write = prepared.output, prepared.write
        server_url = self.support.server_url
        assert server_url is not None  # nosec B101 -- `support.reason()` was None in `_prepare`
        session.rollback()
        tokens = TaskTokenService(
            session, engine.authz_factory(session), max_ttl_seconds=self.max_ttl_seconds
        )
        attempt_dir = self._attempt_dir()
        with contextlib.ExitStack() as cleanup:
            cleanup.callback(self._remove, attempt_dir, claimed)
            code_dir = self._unpack(tarball, prepared.code_sha256, attempt_dir / "code")
            del tarball
            lock_sha256 = self._check_lock(
                code_dir, prepared.lock_sha256, prepared.sources, prepared.named_python
            )
            if claimed.asset in prepared.inputs.values() and prepared.output != "artifact":
                # Checked again after the build; here so that a first run that can never
                # succeed does not wait for an environment build first (#48).
                self._refuse_missing_self(claimed, self._target_state(session, engine, prepared))
            used = self._used_indexes(code_dir, prepared.sources)
            resolver = self._index_resolver(session, engine, prepared.environment, used)
            stop = KeeperStop(keeper)
            try:
                lease = self._venv(
                    lock_sha256,
                    code_dir,
                    sink,
                    resolver,
                    python=prepared.python,
                    used=used,
                    project_id=prepared.project_id,
                    stop=stop.is_set,
                )
            except VenvBuildStopped:
                # The keeper asked for the stop: a cancel, a drain or a lost lease decides.
                _refuse_if_stopped(session, keeper, claimed.run_id)
                raise TaskFailure(
                    FailureKind.TRANSIENT, _BUILD_STOPPED, FailureCode.BUILD_FAILED
                ) from None
            cleanup.callback(lease.release)
            baseline: Baseline | None = None
            if prepared.output == "artifact":
                self._check_artifact_name(session, prepared, claimed)
                before = _TargetState(None, None)
            else:
                before = self._target_state(session, engine, prepared)
                if claimed.has_checks:
                    # Recorded once per task run, before its first process: an earlier attempt
                    # may have written already, so what this attempt finds is not the baseline.
                    baseline = record_baseline(
                        session,
                        claimed.task_run_id,
                        engine.settings.worker_id,
                        claimed.claim,
                        Baseline(before.snapshot_id, before.table_id is None or before.unpublished),
                    )
                    if baseline is None:
                        raise LeaseLost
            if claimed.asset in prepared.inputs.values():
                self._refuse_missing_self(claimed, before)
            shown.watermark = self._watermark(session, engine, prepared, before)
            _refuse_if_stopped(session, keeper, claimed.run_id)
            hidden = _connection_secrets(session, engine.key_provider, prepared)
            token, key_id = self._mint(session, tokens, keeper, claimed, prepared)
            cleanup.callback(self._revoke, session, tokens, key_id)
            outcome = self.runner.run(
                PythonTask(
                    code_dir=code_dir,
                    asset_path=prepared.asset_path,
                    inputs=prepared.inputs,
                    table=claimed.asset,
                    environment=prepared.environment,
                    server_url=server_url,
                    token=token,
                    timeout_seconds=prepared.timeout,
                    venv=lease.path,
                    expected_snapshot_id="none"
                    if before.snapshot_id is None
                    else before.snapshot_id,
                    attempt_dir=attempt_dir,
                    output=prepared.output,
                    input_kinds=prepared.input_kinds,
                    input_versions=prepared.input_versions,
                    decimals=prepared.decimals,
                    task_run_id=claimed.task_run_id,
                    environment_key=lease.key,
                    python=prepared.python,
                    write=prepared.write,
                    key=prepared.key,
                    watermark_column=prepared.watermark_column,
                    watermark=shown.watermark,
                    connections=prepared.connections,
                    table_exists=before.table_id is not None,
                    hidden=hidden,
                    egress=prepared.plan.egress,
                    relay=prepared.plan.relay,
                    roots=prepared.plan.roots,
                    root_users=prepared.plan.root_users,
                ),
                sink,
                stop,
            )
        return self._classify(
            session, engine, claimed, keeper, prepared, before, outcome, ended, baseline
        )

    @staticmethod
    def _refuse_missing_self(claimed: ClaimedTask, state: _TargetState) -> None:
        """Refuse the first run of an asset that reads its own table, which cannot succeed.

        Parameters
        ----------
        claimed
            The claim on an asset whose inputs name its own table.
        state
            The table's state as the worker read it.

        Raises
        ------
        TaskFailure
            Permanent, when the table does not exist.
        """
        if state.table_id is None:
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"{claimed.asset} reads its own table as an input, but the table does not "
                "exist yet, so its first run can never succeed. Create the table once: "
                "deploy a version of the asset that does not read itself, run it, then "
                "deploy the self-reading version",
                FailureCode.BUILD_FAILED,
            )

    def _classify(
        self,
        session: Session,
        engine: RunEngine,
        claimed: ClaimedTask,
        keeper: LeaseKeeper,
        prepared: _Prepared,
        before: _TargetState,
        outcome: RunOutcome,
        ended: Callable[..., TaskOutcome],
        baseline: Baseline | None = None,
    ) -> TaskOutcome | None:
        """Decide what an ended process means: lease, table, stop reason, then the record.

        1. A lost lease records nothing.
        2. A ``succeeded`` record is a claim: the attempt succeeded only if
           the worker itself finds the table there and changed (or, for an append or a
           merge, unchanged with a record of no rows and no snapshot: its source had nothing
           new; its `written` still names what an earlier attempt of the task run wrote, so a
           failed check sets that back), and then
           whatever stopped the process afterwards does not matter. The outcome
           carries what the worker read before and after as `written`:
           for an asset with checks the task runner stamps the task run with it
           and runs the checks (`lakana_worker.task_checks.check_phase`). For such an
           asset "before" is the task run's baseline, recorded before its first
           process (`record_baseline`), not what this attempt found: an earlier
           attempt may have written and then failed, and a rollback must not stop
           at that attempt's data. The write `publishes` when the table did not
           exist or was not published then. If the
           table cannot be read, a cancel or a drain is recorded before the
           storage error is.
           For an artifact asset the evidence is `attempt_version`: a version
           row with exactly this task run and this claim, deleted since or not
           (an overlapping run may have pruned it). The
           record's version number, the process's exit status and a version
           an earlier attempt committed prove nothing; the version found
           is the one recorded, whatever the record claimed. An attempt that
           committed a version and then failed (no record, a timeout, a stop)
           is recorded as failed and the version stays live: the worker has no
           way to delete a version.
        3. A cancel or a drain, seen by the keeper or found on the run, wins
           over everything the process said, ``retryable: false`` included.
        4. Then the timeout, the harness's conflict flag (for a table; an
           artifact upload's conflict is transient: too many open uploads
           clear by expiry, and an immediate free retry would only repeat it),
           ``retryable``, and otherwise a transient failure.

        Parameters
        ----------
        session
            The supervisor's session.
        engine
            The run engine.
        claimed
            The claim.
        keeper
            The running lease keeper.
        prepared
            The prepared attempt.
        before
            The target's state before the process started.
        outcome
            What the runner reported.
        ended
            Builds the outcome to record.
        baseline
            The task run's recorded baseline, for a table asset with checks; `None` otherwise,
            and then what this attempt found before its process stands in for it.

        Returns
        -------
        TaskOutcome or None
            The outcome, or `None` when the lease was lost.

        Raises
        ------
        TaskFailure
            If the table cannot be read after a ``succeeded`` record: the
            stop reason if there is one, else the storage error.
        """
        if keeper.lost:
            return None
        if outcome.succeeded and prepared.output == "artifact":
            session.rollback()
            version = attempt_version(
                session, prepared.environment, claimed.asset, claimed.task_run_id, claimed.claim
            )
            session.rollback()
            if version is not None:
                return ended(None, None, artifact_version=version)
        elif outcome.succeeded:
            try:
                after = self._target_state(session, engine, prepared)
            except TaskFailure:
                _refuse_if_stopped(session, keeper, claimed.run_id)
                raise
            start = baseline or Baseline(
                before.snapshot_id, before.table_id is None or before.unpublished
            )
            if after != before and after.table_id is not None:
                if not self._attempt_wrote(session, claimed, before, after):
                    return self._not_its_write(session, keeper, claimed, ended)
                return ended(
                    None,
                    None,
                    rows=outcome.rows,
                    snapshot_id=after.snapshot_id,
                    written=Written(
                        after.table_id,
                        start.snapshot_id,
                        after.snapshot_id,
                        publishes=start.publishes,
                    ),
                    kept_table=(
                        KeptTable(after.table_id, before.seen)
                        if before.kept and not claimed.has_checks
                        else None
                    ),
                )
            if (
                after == before
                and after.table_id is not None
                and prepared.write in ("append", "merge")
                and outcome.rows == 0
                and outcome.snapshot_id is None
            ):
                # Nothing new since the last run: an append or merge that writes nothing has
                # done its work. A record can claim this without running the asset, which then
                # left the table as it was, the outcome a run that found nothing has.
                earlier = baseline is not None and (
                    start.publishes or start.snapshot_id != after.snapshot_id
                )
                # An earlier attempt of this task run wrote (and created the table, when the
                # baseline publishes): the check phase must still know that write, to set it
                # back or leave the table unpublished when a check fails.
                return ended(
                    None,
                    None,
                    rows=0,
                    written=Written(
                        after.table_id,
                        start.snapshot_id,
                        after.snapshot_id,
                        publishes=start.publishes,
                    )
                    if earlier
                    else None,
                    wrote_nothing=not earlier,
                )
        stop = cancel_kind(session, keeper, claimed.run_id)
        if stop is not None:
            return ended(stop[0], _stop_text(stop), failure_code=stop_code(stop[0]))
        if outcome.succeeded:
            return ended(
                FailureKind.PERMANENT,
                _NO_VERSION if prepared.output == "artifact" else _NO_WRITE,
                failure_code=FailureCode.BUILD_FAILED,
            )
        if outcome.timed_out:
            kind = FailureKind.TIMEOUT
        elif outcome.conflict and prepared.output != "artifact":
            kind = FailureKind.CONFLICT
        elif not outcome.retryable:
            kind = FailureKind.PERMANENT
        else:
            kind = FailureKind.TRANSIENT
        return ended(kind, outcome.error_message or _NO_MESSAGE, failure_code=outcome.failure_code)

    @staticmethod
    def _attempt_wrote(
        session: Session, claimed: ClaimedTask, before: _TargetState, after: _TargetState
    ) -> bool:
        """Tell whether the change the worker found on the table is this attempt's write (#49).

        Parameters
        ----------
        session
            The supervisor's session; rolled back afterwards.
        claimed
            The claim: the task run and claim the write must carry.
        before
            The table before the process started.
        after
            The table after it ended, changed and existing.

        Returns
        -------
        bool
            A snapshot on the table's current branch, newer than `before`, carries this
            attempt's stamp; or, for a table the attempt created without rows (no snapshot),
            the table's location was claimed by this task run.
        """
        assert after.table_id is not None  # nosec B101 -- the caller checked
        if after.snapshot_id is not None:
            if after.metadata is None:
                return False
            since = before.snapshot_id if before.table_id == after.table_id else None
            return written_by_attempt(
                after.metadata,
                since=since,
                task_run_id=claimed.task_run_id,
                claim=claimed.claim,
            )
        if before.table_id is not None:
            return False
        try:
            return created_by_task_run(session, after.table_id, claimed.task_run_id)
        finally:
            session.rollback()

    @staticmethod
    def _not_its_write(
        session: Session,
        keeper: LeaseKeeper,
        claimed: ClaimedTask,
        ended: Callable[..., TaskOutcome],
    ) -> TaskOutcome:
        """End an attempt that reported success while somebody else changed its table.

        A cancel or a drain still wins, as it does over every other failure.

        Parameters
        ----------
        session
            The supervisor's session.
        keeper
            The running lease keeper.
        claimed
            The claim.
        ended
            Builds the outcome to record.

        Returns
        -------
        TaskOutcome
            Permanent: the attempt's own write is not in the table, so what it reported is not
            true, and a retry would not write over somebody else's data more safely.
        """
        stop = cancel_kind(session, keeper, claimed.run_id)
        if stop is not None:
            return ended(stop[0], _stop_text(stop), failure_code=stop_code(stop[0]))
        return ended(
            FailureKind.PERMANENT,
            _NOT_ITS_WRITE.format(table=claimed.asset),
            failure_code=FailureCode.BUILD_FAILED,
        )

    def _prepare(
        self, session: Session, engine: RunEngine, claimed: ClaimedTask
    ) -> tuple[_Prepared, bytes]:
        """Check everything that can be checked in the database, and read the code.

        Parameters
        ----------
        session
            The supervisor's session.
        engine
            The run engine.
        claimed
            The claim.

        Returns
        -------
        tuple
            Plain values for the run, and the code tarball.

        Raises
        ------
        TaskFailure
            For every expected failure, with its kind; permanent for a protected environment
            on a worker without containers.
        LakanaError
            From namespace creation (classified by the caller).
        """
        run = session.get(Run, claimed.run_id)
        env = session.get(Environment, run.environment_id) if run is not None else None
        version = session.get(JobVersion, run.job_version_id) if run is not None else None
        if run is None or env is None or version is None:
            raise TaskFailure(
                FailureKind.PERMANENT,
                "the run or its project version no longer exists",
                FailureCode.NOT_FOUND,
            )
        if env.is_protected and not self.isolated:
            raise TaskFailure(
                FailureKind.PERMANENT,
                _PROTECTED.format(environment=env.name),
                FailureCode.NO_RUNTIME,
            )
        check_integrity(session, engine, run, env, version)
        check_manifest_version(version.manifest_json)
        row = session.get(Principal, run.run_as_principal_id) if run.run_as_principal_id else None
        if row is None or not row.is_active:
            raise TaskFailure(FailureKind.PERMANENT, _INACTIVE, FailureCode.PERMISSION_DENIED)
        entry = manifest_entry(version.manifest_json, claimed.asset)
        path = _asset_path(entry) if entry is not None and asset_kind(entry) == "python" else None
        if entry is None or path is None:
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"the project version has no Python asset file for {claimed.asset}; redeploy",
                FailureCode.CODE_CHANGED,
            )
        inputs = _declared_inputs(entry)
        if inputs is None:
            raise TaskFailure(
                FailureKind.PERMANENT, self._bad_inputs(claimed.asset), FailureCode.CODE_CHANGED
            )
        output = self._output_of(entry, claimed.asset)
        write, key, watermark_column = (
            checked_write(entry, claimed.asset) if output == "table" else ("replace", (), None)
        )
        if write == "append" and watermark_column is None:
            # Never append every row the asset returns again and again.
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"the project version appends to {claimed.asset} without a watermark; deploy it "
                "again with this release",
                FailureCode.CODE_CHANGED,
            )
        plan = self._connection_plan(session, env, entry, claimed.asset)
        reason = self.support.reason()
        if reason is not None:
            raise TaskFailure(
                FailureKind.PERMANENT, CANNOT_RUN.format(reason=reason), FailureCode.NO_RUNTIME
            )
        own = f"{sys.version_info.major}.{sys.version_info.minor}"
        named = manifest_python(version.manifest_json)
        if self.environments is None and named is not None and named != own:
            raise TaskFailure(
                FailureKind.PERMANENT,
                _LOCAL_PYTHON.format(python=named, own=own),
                FailureCode.NO_RUNTIME,
            )
        try:
            ref = TableRef.from_dotted(env.name, claimed.asset)
        except ValidationError as exc:
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"the asset name {claimed.asset!r} is not a valid table name; "
                "rename the asset and redeploy",
                FailureCode.CODE_CHANGED,
            ) from exc
        if output == "artifact" and claimed.asset in inputs.values():
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"{claimed.asset} reads its own artifact as an input, which an artifact "
                "asset cannot do: it has no way to read its previous version. Remove that "
                "input and redeploy",
                FailureCode.CODE_CHANGED,
            )
        principal = PrincipalRef(
            id=row.id,
            name=row.name,
            type=row.type,
            run_context=RunContext(task_run_id=claimed.task_run_id, claim=claimed.claim),
        )
        authz = engine.authz_factory(session)
        readable: dict[str, bool] = {}

        def may_read(name: str) -> bool:
            """Ask `may_read_name` once per name for this attempt.

            Parameters
            ----------
            name
                A dotted name.

            Returns
            -------
            bool
                Whether the run principal may read it.
            """
            if name not in readable:
                readable[name] = may_read_name(authz, principal, env.name, name)
            return readable[name]

        require_upstreams(session, run, env.name, version, claimed.asset, may_read=may_read)
        kinds, versions = self._resolve_inputs(
            session, may_read, principal, env.name, run.id, inputs, entry
        )
        catalog = CatalogService(session, StorageResolver(session, engine.key_provider), authz)
        ensure_namespaces(session, catalog, principal, env.name, ref.namespace.parts)
        try:
            tarball = _read_code(session, engine.key_provider, env, version)
        except OSError as exc:
            raise TaskFailure(
                FailureKind.TRANSIENT,
                "the code tarball could not be read (storage error)",
                FailureCode.INTERNAL,
            ) from exc
        except LakanaError as exc:
            raise TaskFailure(
                FailureKind.PERMANENT,
                "the code tarball is missing from storage; redeploy",
                FailureCode.CODE_CHANGED,
            ) from exc
        timeout = min(float(claimed.timeout_seconds), engine.settings.limits.timeout_seconds)
        needed = token_ttl(timeout, self.kill_grace)
        if needed > self.max_ttl_seconds:
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"the task's token could not outlive the task: the asset's timeout "
                f"({int(timeout)} s) plus the kill grace and a margin needs {needed} s, but "
                f"LAKANA_TASK_TOKENS__MAX_TTL_SECONDS is {self.max_ttl_seconds}; raise that "
                f"setting to at least {needed} or lower the asset's timeout",
                FailureCode.NO_RUNTIME,
            )
        prepared = _Prepared(
            environment=env.name,
            ref=ref,
            asset_path=path,
            inputs=inputs,
            code_sha256=version.code_sha256,
            lock_sha256=manifest_lock_sha256(version.manifest_json),
            timeout=timeout,
            output=output,
            input_kinds=kinds,
            input_versions=versions,
            decimals=entry_decimals(entry),
            sources=KnownSources(registered_indexes(session), self.venvs.index_url),
            python=named or own,
            named_python=named,
            project_id=environment_owner(version.repository_id, version.environment_id),
            principal=principal,
            write=write,
            key=key,
            watermark_column=watermark_column,
            plan=plan,
        )
        return prepared, tarball

    def _connection_plan(
        self, session: Session, env: Environment, entry: Mapping[str, object], asset: str
    ) -> ConnectionPlan:
        """Plan where the task may go: the endpoints of the connections the asset declares.

        Only what the declared connections hold reaches the plan, so the task's token, relay and
        mounts grant exactly those endpoints and roots. Under the Docker runner HTTPS kinds go
        through the egress proxy and database kinds through the task's relay; the local runner
        connects directly and reads roots from `ingestion_roots`.

        Parameters
        ----------
        session
            The supervisor's session.
        env
            The run's environment.
        entry
            The asset's manifest entry; untrusted.
        asset
            The asset, for the message.

        Returns
        -------
        ConnectionPlan
            Empty for an asset that declares no connection.

        Raises
        ------
        TaskFailure
            Permanent when the environment no longer has one of them (a deploy checks this, so
            only a change since can cause it); when the local runner lacks a root a connection
            names; and when the plan exceeds what one task may hold (64 proxy pairs, 16 relay
            routes, 16 mounted roots), naming the cap.
        """
        names = entry_connections(entry)
        if not names:
            return ConnectionPlan()
        rows = {
            row.name: row
            for row in session.execute(
                select(
                    Connection.name, Connection.kind, Connection.address, Connection.settings
                ).where(Connection.environment_id == env.id, Connection.name.in_(names))
            ).all()
        }
        declared: list[tuple[str, ConnectionKind, Mapping[str, object], Mapping[str, object]]] = []
        for name in names:
            row = rows.get(name)
            if row is None:
                raise TaskFailure(
                    FailureKind.PERMANENT,
                    f"{asset} declares the connection `{name}`, which environment {env.name} "
                    "does not have; create it (lakana connection create) or remove it from the "
                    "asset's connections, then deploy again",
                    FailureCode.NOT_FOUND,
                )
            declared.append((name, ConnectionKind(row.kind), row.address, row.settings))
        plan = connection_plan(declared, isolated=self.isolated, root_dir=self._root_dir)
        self._check_caps(asset, list(plan.egress), list(plan.relay), plan.root_users)
        return plan

    def _root_dir(self, root: str, connection: str) -> str:
        """Find where a task reads an ingestion root.

        Parameters
        ----------
        root
            The root's name.
        connection
            The connection naming it, for the message.

        Returns
        -------
        str
            ``/ingest/<root>`` under the Docker runner (the launcher refuses a root it lacks);
            the worker's own directory for it under the local runner.

        Raises
        ------
        TaskFailure
            Permanent under the local runner when `LAKANA_RUN__INGESTION_ROOTS` lacks the root.
        """
        if self.isolated:
            return posixpath.join(INGEST_MOUNT, root)
        return local_root_dir(self.ingestion_roots, root, connection)

    def _check_caps(
        self,
        asset: str,
        egress: list[tuple[str, int]],
        relay: list[tuple[int, str, int]],
        roots: Mapping[str, str],
    ) -> None:
        """Refuse a plan bigger than one task's token, relay or mounts can hold.

        Parameters
        ----------
        asset
            The asset, for the message.
        egress
            The proxy pairs.
        relay
            The relay routes.
        roots
            The roots to mount.

        Raises
        ------
        TaskFailure
            Permanent, naming the count and the cap, for the first that is exceeded.
        """
        if len(egress) > MAX_TASK_EGRESS:
            message = (
                f"asset `{asset}` reaches {len(egress)} hosts through the egress proxy; a task "
                f"may reach at most {MAX_TASK_EGRESS} — split the asset"
            )
        elif len(relay) > MAX_TASK_RELAY:
            message = (
                f"asset `{asset}` reaches {len(relay)} database endpoints; a task's relay "
                f"forwards at most {MAX_TASK_RELAY} — split the asset"
            )
        elif self.isolated and len(roots) > MAX_TASK_ROOTS:
            message = (
                f"asset `{asset}` reads {len(roots)} ingestion roots; a task mounts at most "
                f"{MAX_TASK_ROOTS} — split the asset"
            )
        else:
            return
        raise TaskFailure(FailureKind.PERMANENT, message, FailureCode.NO_RUNTIME)

    @staticmethod
    def _output_of(entry: Mapping[str, object], asset: str) -> str:
        """Read what the asset produces, refusing a value this worker does not know.

        Parameters
        ----------
        entry
            The manifest entry; untrusted.
        asset
            The asset, for the message.

        Returns
        -------
        str
            ``table`` or ``artifact``.

        Raises
        ------
        TaskFailure
            Permanent when the entry's `output` is something else: an entry from a
            newer platform version would otherwise be run as a table write.
        """
        declared = entry.get("output", "table")
        if declared not in ("table", "artifact"):
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"the project version declares an output of {_shown(declared)} for {asset}, which "
                "this worker cannot run (it runs `table` and `artifact`); run the platform version "
                "that deployed it on this worker, or fix the asset and redeploy",
                FailureCode.NO_RUNTIME,
            )
        return entry_output(entry)

    @staticmethod
    def _resolve_inputs(
        session: Session,
        may_read: Callable[[str], bool],
        principal: PrincipalRef,
        environment: str,
        run_id: uuid.UUID,
        inputs: dict[str, str],
        entry: Mapping[str, object],
    ) -> tuple[dict[str, str], dict[str, int]]:
        """Check every input's kind against the catalog and pick the artifact versions.

        The kinds in the manifest were recorded at deploy and may be wrong
        (a producer that had not run, a name another repository claimed since),
        so the catalog has the last word here, in both directions. A table input
        that does not exist is left to the existing not-found path.

        This runs as the run principal: the catalog is consulted for
        an input only when `may_read` says the principal may read it, so that nothing
        about an object it may not read (that it exists, its kind, that it has
        versions, their numbers) reaches the attempt's message, the log or the
        task. For such an input the recorded kind stands, a table is left to the
        harness's query (which the server refuses as it would a missing table),
        and an artifact fails the attempt with one text that holds whether or not the
        object exists. Each object is looked up once, however many parameters
        read it, so that they all get the same version.

        Parameters
        ----------
        session
            The supervisor's session.
        may_read
            Whether the run principal may read a name; one answer per name, shared
            with the upstream check.
        principal
            The run principal, for the message, with the task's run context.
        environment
            Environment name.
        run_id
            The run, whose own producer's version wins over the current one.
        inputs
            Parameter → dotted name.
        entry
            The manifest entry.

        Returns
        -------
        tuple
            The kind of every input, and the version of each artifact input.

        Raises
        ------
        TaskFailure
            Permanent: an input recorded as one kind that is another now, an
            artifact input that has no version yet, or one the run principal may
            not read.
        """
        kinds = entry_input_kinds(entry)
        versions: dict[str, int] = {}
        actual_kinds: dict[str, str | None] = {}
        found: dict[str, int | None] = {}
        for parameter, name in inputs.items():
            if not may_read(name):
                if kinds[parameter] == "artifact":
                    raise TaskFailure(
                        FailureKind.PERMANENT,
                        f"{name} cannot be read by the run's principal "
                        f"{_shown(principal.name)}, or does not exist; grant read on it or "
                        "check the name",
                        FailureCode.PERMISSION_DENIED,
                    )
                continue
            if name not in actual_kinds:
                actual_kinds[name] = kind_of(session, environment, name)
            actual = actual_kinds[name]
            if kinds[parameter] == "artifact":
                if actual is not None and actual != "ARTIFACT":
                    words = _KIND_WORDS.get(actual, "another kind of object")
                    raise TaskFailure(
                        FailureKind.PERMANENT,
                        f"{name} is {words}, but this deploy recorded it as an artifact; "
                        "redeploy so that the asset reads it as a table",
                        FailureCode.CODE_CHANGED,
                    )
                if name not in found:
                    found[name] = run_input_version(session, environment, name, run_id)
                version = found[name]
                if version is None:
                    raise TaskFailure(
                        FailureKind.PERMANENT,
                        f"{name} has no version yet; run the asset that produces it",
                        FailureCode.UPSTREAM_MISSING,
                    )
                versions[parameter] = version
            elif actual == "ARTIFACT":
                raise TaskFailure(
                    FailureKind.PERMANENT,
                    f"{name} is an artifact, but this deploy recorded it as a table; redeploy "
                    "so that the asset receives a path",
                    FailureCode.CODE_CHANGED,
                )
        return kinds, versions

    @staticmethod
    def _check_artifact_name(session: Session, prepared: _Prepared, claimed: ClaimedTask) -> None:
        """Refuse an artifact asset whose name is taken by something that is not an artifact.

        Parameters
        ----------
        session
            The supervisor's session; rolled back first, so the read is current,
            and again afterwards.
        prepared
            The prepared attempt.
        claimed
            The claim, for the name.

        Raises
        ------
        TaskFailure
            Permanent when a table, a view or an external table has the name.
        """
        session.rollback()
        actual = kind_of(session, prepared.environment, claimed.asset)
        session.rollback()
        if actual is not None and actual != "ARTIFACT":
            words = _KIND_WORDS.get(actual, "another kind of object")
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"{claimed.asset} is {words}, not an artifact. Drop it (`lakana table delete "
                f"{claimed.asset}`) or give the asset another name and redeploy.",
                FailureCode.BUILD_FAILED,
            )

    @staticmethod
    def _bad_inputs(asset: str) -> str:
        """Word the failure for a manifest entry whose `inputs` is malformed.

        Parameters
        ----------
        asset
            The asset.

        Returns
        -------
        str
            The message.
        """
        return (
            f"the project version's inputs for {asset} are not a mapping of parameter names "
            "to tables; redeploy"
        )

    def _attempt_dir(self) -> Path:
        """Create this attempt's private directory under the work directory.

        Returns
        -------
        Path
            A new directory, mode 0700, owned by the worker's user.

        Raises
        ------
        TaskFailure
            Permanent if the work directory is not private (a configuration
            error); transient if a directory cannot be created.
        """
        try:
            prepare_private_directory(self.work_dir)
            attempt_dir = Path(
                os.path.abspath(tempfile.mkdtemp(prefix="attempt-", dir=self.work_dir))
            )
            held = self.sweeper is None
            try:
                if self.sweeper is not None:
                    self.sweeper.hold(attempt_dir)
                held = True
            finally:
                if not held:
                    remove_private_tree(attempt_dir)
            return attempt_dir
        except UnsafeDirectory:
            pass
        except OSError as exc:
            raise TaskFailure(
                FailureKind.TRANSIENT,
                "could not create the task's working directory: "
                f"{exc.strerror or 'filesystem error'}",
                FailureCode.INTERNAL,
            ) from exc
        logger.warning(
            "The task directory %s is not private: it must be a real directory owned by the "
            "worker's user with mode 0700. Python tasks fail until it is.",
            self.work_dir,
        )
        raise TaskFailure(
            FailureKind.PERMANENT,
            "the worker's task directory is not private; an operator must make it a real "
            "directory owned by the worker's user with mode 0700 (the worker log names it)",
            FailureCode.NO_RUNTIME,
        )

    def _remove(self, attempt_dir: Path, claimed: ClaimedTask) -> None:
        """Remove an attempt's directory once its process is dead, then stop holding it.

        Parameters
        ----------
        attempt_dir
            The directory; the task owned it and may have booby-trapped it.
        claimed
            The claim, for the warning.
        """
        try:
            if not remove_private_tree(attempt_dir):
                logger.warning(
                    "The attempt directory %s of task run %s was not removed completely.",
                    attempt_dir,
                    claimed.task_run_id,
                )
        finally:
            if self.sweeper is not None:
                self.sweeper.release(attempt_dir)

    def _unpack(self, tarball: bytes, code_sha256: str, target: Path) -> Path:
        """Unpack the code for this attempt alone.

        Parameters
        ----------
        tarball
            The gzipped tar.
        code_sha256
            Its recorded digest, checked again against the bytes unpacked.
        target
            The directory to create inside the attempt directory.

        Returns
        -------
        Path
            The unpacked, read-only tree.

        Raises
        ------
        TaskFailure
            Permanent for an unsafe or malformed archive; transient when the
            machine could not unpack it.
        """
        try:
            return unpack_code(
                tarball,
                code_sha256,
                target,
                max_bytes=self.limits.checkout_bytes,
                max_members=self.limits.checkout_members,
            )
        except CodeUnpackError as exc:
            raise TaskFailure(FailureKind.PERMANENT, str(exc), FailureCode.CODE_CHANGED) from exc
        except CodeUnpackFailed as exc:
            raise TaskFailure(FailureKind.TRANSIENT, str(exc), FailureCode.INTERNAL) from exc

    @staticmethod
    def _check_lock(
        code_dir: Path, expected: str | None, sources: KnownSources, named_python: str | None
    ) -> str:
        """Validate the deployed lockfile again, against this worker and the task's Python.

        Parameters
        ----------
        code_dir
            The unpacked code.
        expected
            The manifest's lock digest, if it records one.
        sources
            The package sources this instance knows.
        named_python
            The manifest's `python`, if the project names one: `requires-python` must admit it
            (`task_interpreter`), else the worker's own Python.

        Returns
        -------
        str
            The lock digest, one half of the venv's key.

        Raises
        ------
        TaskFailure
            Permanent: the files are unreadable or not UTF-8, there is a `uv.toml`, the lock is not
            the deployed one, it names a package source this instance does not know (an index
            was removed or changed after the deploy), or it is no longer compatible with this
            worker.
        """
        try:
            lock = (code_dir / LOCK_FILE).read_bytes()
            pyproject = (code_dir / PYPROJECT_FILE).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise TaskFailure(
                FailureKind.PERMANENT,
                "the deployed code has no readable pyproject.toml and uv.lock; redeploy",
                FailureCode.CODE_CHANGED,
            ) from exc
        if os.path.lexists(code_dir / UV_TOML_FILE):
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"{UV_TOML_FILE} {UV_TOML_MESSAGE}",
                FailureCode.BUILD_FAILED,
            )
        if expected is None or lock_digest(lock) != expected:
            raise TaskFailure(
                FailureKind.PERMANENT,
                "uv.lock does not match the project version's lock_sha256; redeploy",
                FailureCode.CODE_CHANGED,
            )
        unknown = source_issues(pyproject, lock, sources)
        if unknown:
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"{_listed_issues(unknown)}. {_closing_for(unknown)}",
                FailureCode.BUILD_FAILED,
            )
        issues = lockfile_issues(
            pyproject,
            lock,
            platform_version=platform_version(),
            python=task_interpreter(named_python),
            sources=sources,
            deploying=False,
            named_python=named_python,
        )
        if issues:
            raise TaskFailure(
                FailureKind.PERMANENT,
                f"{_listed_issues(issues)}. This project version was deployed for another platform "
                "version: update the lock and redeploy.",
                FailureCode.BUILD_FAILED,
            )
        return expected

    @staticmethod
    def _used_indexes(code_dir: Path, sources: KnownSources) -> tuple[RegisteredIndex, ...]:
        """List the registered indexes the deployed lock takes packages from.

        Parameters
        ----------
        code_dir
            The unpacked code; `_check_lock` has just read and checked its lock.
        sources
            The package sources this instance knows.

        Returns
        -------
        tuple of RegisteredIndex
            Sorted by name; empty for most projects.
        """
        return used_indexes((code_dir / LOCK_FILE).read_bytes(), sources)

    @staticmethod
    def _index_resolver(
        session: Session,
        engine: RunEngine,
        environment: str,
        used: tuple[RegisteredIndex, ...],
    ) -> IndexResolver | None:
        """Build what the venv cache calls to get the credentials, if a build runs.

        The secrets are read only then: a cached environment needs none, so a
        rotated or removed credential affects only the next build.

        Parameters
        ----------
        session
            The supervisor's session; the resolver runs on the supervisor's thread.
        engine
            The run engine, for its key provider.
        environment
            The run's environment; its secret scope is searched.
        used
            The registered indexes the lock uses.

        Returns
        -------
        IndexResolver or None
            `None` when the lock uses no registered index.
        """
        if not used:
            return None

        def resolve() -> list[IndexAccess]:
            """Decrypt the credentials of `used` in the run's environment.

            Returns
            -------
            list of IndexAccess
                One per index.

            Raises
            ------
            VenvBuildRefused
                If the environment is gone, or a secret is missing, cannot be
                decrypted or holds no usable password.
            """
            try:
                row = session.execute(
                    select(Environment).where(Environment.name == environment, LIVE_ENVIRONMENT)
                ).scalar_one_or_none()
                if row is None:
                    raise VenvBuildRefused(
                        "environment build failed: the environment no longer exists"
                    )
                return resolve_index_access(session, engine.key_provider, row.id, row.name, used)
            except IndexCredentialError as exc:
                raise VenvBuildRefused(f"environment build failed: {exc}") from exc
            finally:
                # Nothing was written; the build that follows must not hold a transaction.
                session.rollback()

        return resolve

    def _venv(
        self,
        lock_sha256: str,
        code_dir: Path,
        sink: Callable[[bytes], None],
        indexes: IndexResolver | None = None,
        *,
        python: str,
        used: tuple[RegisteredIndex, ...] = (),
        project_id: uuid.UUID,
        stop: Callable[[], bool] | None = None,
    ) -> VenvLease:
        """Get the venv of the lockfile and `pyproject.toml`, building it on first use.

        With `environments` set the launcher builds it in a container, from the attempt's code
        in its work volume; otherwise `acquire_venv` builds it on the worker.

        Parameters
        ----------
        lock_sha256
            The lock digest.
        code_dir
            The unpacked code: ``code`` in the attempt directory, whose name the launcher
            mounts from its work volume.
        sink
            Receives the build's output.
        indexes
            Returns the credentials of the registered indexes the lock uses; called only if a
            build runs.
        python
            The task's Python (DockerRunner only; the local runner has its own).
        used
            The registered indexes the lock uses (DockerRunner only).
        project_id
            `environment_owner` of the project version (DockerRunner only).
        stop
            The task's stop (`KeeperStop.is_set`): a build ends when it says so (#291).

        Returns
        -------
        VenvLease
            The venv, protected from eviction until released.

        Raises
        ------
        VenvBuildStopped
            If `stop` said so before the venv was there; the caller asks the keeper why.
        TaskFailure
            Permanent for an unsafe cache (a configuration error), for a build that cannot
            succeed until a credential, an index or the lock changes (a refused credential, a
            package whose own build fails, a package without a file for the platform), and
            for a launcher build
            whose output the log sink would not take; transient when the build failed otherwise.
        """
        try:
            if self.environments is not None:
                return self.environments.acquire(
                    lock_sha256,
                    code_dir,
                    sink,
                    indexes,
                    python=python,
                    attempt=code_dir.parent.name,
                    used=used,
                    project_id=project_id,
                    stop=stop,
                )
            return self.acquire_venv(self.venvs, lock_sha256, code_dir, sink, indexes, stop=stop)
        except VenvBuildStopped:
            raise
        except LogSinkFailed as exc:
            logger.warning(
                "The output of an environment build could not be written to the task's log.",
                exc_info=True,
            )
            raise TaskFailure(
                FailureKind.PERMANENT, _BUILD_LOG_FAILED, FailureCode.INTERNAL
            ) from exc
        except VenvBuildRefused as exc:
            raise TaskFailure(FailureKind.PERMANENT, str(exc), FailureCode.SECRET_REFUSED) from exc
        except VenvBuildBroken as exc:
            raise TaskFailure(FailureKind.PERMANENT, str(exc), FailureCode.BUILD_FAILED) from exc
        except VenvCacheUnsafe as exc:
            raise TaskFailure(FailureKind.PERMANENT, str(exc), FailureCode.NO_RUNTIME) from exc
        except VenvBuildError as exc:
            raise TaskFailure(FailureKind.TRANSIENT, str(exc), FailureCode.BUILD_FAILED) from exc

    @staticmethod
    def _target_state(session: Session, engine: RunEngine, prepared: _Prepared) -> _TargetState:
        """Read the target table's state in-process.

        The same read gives the write's precondition before the process
        starts and, after it, the only evidence of a write the worker accepts.

        Parameters
        ----------
        session
            The supervisor's session; rolled back first, so the read is
            current, and again afterwards, so no transaction stays open.
        engine
            The run engine.
        prepared
            The prepared attempt.

        Returns
        -------
        _TargetState
            The table's id and current snapshot; both `None` when it does not exist.

        Raises
        ------
        TaskFailure
            Permanent when the name is taken by a view or an external table;
            transient when storage cannot be read.
        """
        ref, environment = prepared.ref, prepared.environment
        session.rollback()
        try:
            table_id = resolve_table_id(session, environment, ref.namespace.joined(), ref.name)
            row = session.get(Table, table_id) if table_id is not None else None
            if row is None:
                return _TargetState(None, None)
            if row.table_type is TableType.ARTIFACT:
                raise TaskFailure(
                    FailureKind.PERMANENT,
                    f"{ref.dotted()} is an artifact, and this asset produces a table. Drop the "
                    f"artifact (`lakana artifact drop {ref.dotted()}`) or give the asset "
                    "another name and redeploy.",
                    FailureCode.BUILD_FAILED,
                )
            if row.table_type is not TableType.ICEBERG or row.metadata_location is None:
                raise TaskFailure(
                    FailureKind.PERMANENT,
                    f"{ref.dotted()} exists and is not a managed table; a Python asset replaces "
                    "a table. Drop or rename it, or give the asset another name and redeploy.",
                    FailureCode.BUILD_FAILED,
                )
            resolved = StorageResolver(session, engine.key_provider).for_environment(environment)
            try:
                metadata = read_metadata(resolved.file_io, row.metadata_location)
            except OSError as exc:
                raise TaskFailure(
                    FailureKind.TRANSIENT,
                    f"the table {ref.dotted()} could not be read (storage error)",
                    FailureCode.INTERNAL,
                ) from exc
            return _TargetState(
                row.id,
                metadata.current_snapshot_id,
                row.unpublished_run_id is not None,
                row.kept_run_id is not None,
                KeptSeen(row.kept_run_id, row.kept_task_run_id, row.kept_snapshot_id),
                metadata,
            )
        finally:
            session.rollback()

    @staticmethod
    def _watermark(
        session: Session, engine: RunEngine, prepared: _Prepared, before: _TargetState
    ) -> str | None:
        """Read the watermark an append or a merge filters on, as the run principal.

        The watermark comes with the snapshot it was read from (`read_watermark`). That must be
        the state the attempt is judged against (`before`), whose snapshot the task's upload is
        pinned to: a write in between would otherwise count as this attempt's.

        Parameters
        ----------
        session
            The supervisor's session; rolled back before and after.
        engine
            The run engine.
        prepared
            The prepared attempt.
        before
            The target's state as the worker read it.

        Returns
        -------
        str or None
            The encoded watermark; `None` for an asset without one, a table that does not exist
            yet or has no rows.

        Raises
        ------
        ConflictError
            If the table changed after `before` was read: a conflict, retried once for free.
        """
        if prepared.write == "replace" or prepared.watermark_column is None:
            return None
        if before.table_id is None:
            return None
        session.rollback()
        try:
            catalog = CatalogService(
                session,
                StorageResolver(session, engine.key_provider),
                engine.authz_factory(session),
            )
            watermark, pin = catalog.read_watermark(
                prepared.principal, prepared.ref, prepared.watermark_column
            )
        finally:
            session.rollback()
        if pin != SnapshotPin(before.table_id, before.snapshot_id):
            raise ConflictError(target_changed_message(prepared.ref.dotted()))
        return None if watermark is None else watermark.encode()

    def _mint(
        self,
        session: Session,
        tokens: TaskTokenService,
        keeper: LeaseKeeper,
        claimed: ClaimedTask,
        prepared: _Prepared,
    ) -> tuple[str, str]:
        """Mint the token bound to this attempt.

        The minted object and the token never reach a log: only the two
        strings the caller needs leave this method.

        Parameters
        ----------
        session
            The supervisor's session.
        tokens
            The token service on that session.
        keeper
            The running lease keeper.
        claimed
            The claim.
        prepared
            The prepared attempt, for the timeout.

        Returns
        -------
        tuple
            The token and its key id.

        Raises
        ------
        LeaseLost
            If the claim is no longer this worker's running attempt.
        TaskFailure
            ``CANCELLED`` or ``RELEASED`` if that is why the mint was refused;
            permanent for a malformed manifest entry or an inactive run principal.
        """
        ttl = token_ttl(prepared.timeout, self.kill_grace)
        try:
            minted = tokens.mint_for_task(claimed.task_run_id, claimed.claim, ttl_seconds=ttl)
        except ConflictError as exc:
            session.rollback()
            _refuse_if_stopped(session, keeper, claimed.run_id)
            raise LeaseLost from exc
        except IntegrityError as exc:
            session.rollback()
            raise LeaseLost from exc
        except ValidationError as exc:
            session.rollback()
            raise TaskFailure(
                FailureKind.PERMANENT, self._bad_inputs(claimed.asset), FailureCode.CODE_CHANGED
            ) from exc
        except NotFoundError as exc:
            session.rollback()
            raise TaskFailure(
                FailureKind.PERMANENT, _INACTIVE, FailureCode.PERMISSION_DENIED
            ) from exc
        return minted.token, minted.key_id

    @staticmethod
    def _revoke(session: Session, tokens: TaskTokenService, key_id: str) -> None:
        """Revoke the attempt's token; a failure is logged, never raised.

        A token that could not be revoked still dies with its attempt: it is
        valid only while the task run is running with this claim.

        Parameters
        ----------
        session
            The supervisor's session.
        tokens
            The token service on that session.
        key_id
            The token's public key id.
        """
        try:
            session.rollback()
            tokens.revoke_for_task(key_id)
        except Exception:
            logger.warning(
                "Could not revoke the task token %s; it dies with its attempt.",
                key_id,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                session.rollback()
