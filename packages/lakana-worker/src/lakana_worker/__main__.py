"""Entry point: `python -m lakana_worker`, and the worker that `lakana-worker` runs."""

from __future__ import annotations

import functools
import itertools
import logging
import os
import secrets
import signal
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from sqlalchemy.orm import Session

from lakana_core.artifacts.sweep import DEFAULT_MAX_SECONDS, sweep_artifacts
from lakana_core.audit.authorization import AuditingAuthorization, flush_suppressed
from lakana_core.audit.content import install_query_text
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import AuthorizationService
from lakana_core.catalog.columns import backfill_table_columns
from lakana_core.catalog.health import sweep_table_reads
from lakana_core.catalog.location_sweep import (
    DEFAULT_MAX_SECONDS as LOCATION_SWEEP_MAX_SECONDS,
)
from lakana_core.catalog.location_sweep import sweep_table_locations
from lakana_core.catalog.usage import MEASURE_MAX_SECONDS as SIZE_MEASURE_MAX_SECONDS
from lakana_core.catalog.usage import measure_table_sizes
from lakana_core.config import (
    QueryLimitsConfig,
    Settings,
    parse_task_memory,
    warn_about_tokens_in_url_paths,
)
from lakana_core.db import create_engine
from lakana_core.errors import LakanaError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.jobs.scheduler import sweep_job_fires
from lakana_core.log_events import LogEvent, log_event
from lakana_core.log_setup import configure_logging
from lakana_core.models import Worker
from lakana_core.projects.code_sweep import CODE_SWEEP_MAX_SECONDS, sweep_code_archives
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.run_policy import RunCaps
from lakana_core.secrets.crypto import ACCEPT_FORMAT_1_WARNING
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.storage.resolver import StorageResolver
from lakana_worker.attempt_sweep import AttemptSweeper
from lakana_worker.ca_bundle import CaBundleError, find_ca_bundle, query_ca_bundle
from lakana_worker.caches import CacheKeeper, CachePaths, PackageCacheTrim, TrimSettings
from lakana_worker.deploy import DeploySettings, platform_version
from lakana_worker.deploy_sweep import DeploySweeper, sweep_deploy_checkouts
from lakana_worker.docker_environments import DockerEnvironments
from lakana_worker.executor import check_limits, install_drain, install_heartbeat
from lakana_worker.heartbeat import WorkerHeartbeat
from lakana_worker.launcher_client import LauncherClient, LauncherUnavailable, claim_owner
from lakana_worker.memory import resolve_limit, start_watchdog
from lakana_worker.notify import DeliveryLoop, NotifySettings
from lakana_worker.pipeline import install_maintenance_timeout
from lakana_worker.poller import WorkerPool, poll_forever
from lakana_worker.processes import UnsafeDirectory, prepare_private_directory
from lakana_worker.python_support import PythonSupport, detect, worker_cache_dir
from lakana_worker.python_tasks import PythonRuntime, token_ttl
from lakana_worker.runners import DockerRunner, LocalProcessRunner, Runner
from lakana_worker.scheduler import SchedulerLoop
from lakana_worker.spill import sweep_spill_directories
from lakana_worker.tasks import RunEngine, TaskSettings
from lakana_worker.trust import TRUST_FILE, TrustBundleError, build_trust_bundle
from lakana_worker.venvs import VenvSettings

if TYPE_CHECKING:
    from pydantic import SecretStr

logger = logging.getLogger("lakana_worker.__main__")
"""Named, not `__name__`: as `python -m lakana_worker` that is `__main__`, outside the package."""

_SCHEDULER_JOIN_FLOOR_SECONDS = 5.0
"""Least time shutdown waits for the scheduler thread to resign."""


_TAKES_LINES = {
    "all": "This worker takes interactive and pipeline work (LAKANA_WORKER__TAKES=all).",
    "interactive": (
        "This worker takes interactive work only (LAKANA_WORKER__TAKES=interactive): ad-hoc "
        "queries. It runs no deploy, no pipeline task and no maintenance; keep another worker "
        "on all or pipeline."
    ),
    "pipeline": (
        "This worker takes pipeline work only (LAKANA_WORKER__TAKES=pipeline): deploys, "
        "pipeline tasks and their queries, and maintenance. It runs no ad-hoc query; keep "
        "another worker on all or interactive."
    ),
}
"""The start-up line for each value of `LAKANA_WORKER__TAKES`."""


def _authorization(session: Session, limiter: DenialAuditLimiter) -> AuthorizationService:
    """Build the per-iteration authorization service.

    Parameters
    ----------
    session
        The iteration's session.
    limiter
        The worker's one limiter of denial audit rows, shared by every iteration.

    Returns
    -------
    AuthorizationService
        Grant-backed, auditing every denial.
    """
    return AuditingAuthorization(GrantAuthorization(session), session, limiter)


def _check_ca_bundle(path: Path | None, variable: str = "LAKANA_GIT__CA_BUNDLE") -> None:
    """Refuse to start with a CA bundle the worker cannot read.

    The shared settings check only the path's format, because the server and
    `migrate` load them without the worker's mount.

    Parameters
    ----------
    path
        The configured path, or `None`.
    variable
        The setting's environment variable, for the message.

    Raises
    ------
    SystemExit
        If `path` is not a readable file.
    """
    if path is not None and not (path.is_file() and os.access(path, os.R_OK)):
        raise SystemExit(f"{variable} must name a readable file in the worker: {path}")


def _check_process_supervision() -> None:
    """Refuse to start on a Python that cannot supervise query processes.

    The worker watches each query process with `os.waitid`, which CPython on macOS has from
    version 3.13 only.

    Raises
    ------
    SystemExit
        If `os.waitid` does not exist.
    """
    if not hasattr(os, "waitid"):
        raise SystemExit(
            "The worker cannot start: this Python lacks os.waitid, which the worker needs to "
            "supervise query processes (macOS has it from Python 3.13). Run the worker with "
            "Python 3.13 or newer."
        )


def _check_query_ca_bundle() -> None:
    """Refuse to start with a CA bundle the query process cannot use, and log the one it uses.

    Raises
    ------
    SystemExit
        If ``AWS_CA_BUNDLE`` or ``SSL_CERT_FILE`` names a missing, unreadable or non-PEM file;
        the message names the variable and the path.
    """
    try:
        bundle, source = find_ca_bundle()
        query_ca_bundle()
    except CaBundleError as exc:
        raise SystemExit(str(exc)) from exc
    logger.info("Query processes trust the certificate authorities in %s (%s).", bundle, source)


def _check_query_limits(limits: QueryLimitsConfig) -> None:
    """Refuse to start with a query size DuckDB cannot read.

    Parameters
    ----------
    limits
        The configured query limits.

    Raises
    ------
    SystemExit
        If `memory_limit` or `max_temp_directory_size` is not a size DuckDB accepts.
    """
    try:
        check_limits(limits)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _secret(value: SecretStr | None) -> str | None:
    """Unwrap an optional secret setting.

    Parameters
    ----------
    value
        The setting.

    Returns
    -------
    str or None
        Its value.
    """
    return value.get_secret_value() if value is not None else None


def _python_runtime(
    settings: Settings, python: PythonSupport, stop: threading.Event | None = None
) -> PythonRuntime:
    """Build what runs Python tasks, from the settings.

    A worker that can run Python assets creates its attempt directory here, privately
    and idempotently: the image does not, because two workers that mount one new volume
    would both copy it in and the second would fail. One that is not private (a link, group
    or other access) is logged and left alone; Python tasks then fail with the reason.
    The venv cache is made when the first task needs it. A task-token cap too low for a task
    with the default timeout is logged.

    Parameters
    ----------
    settings
        The worker's settings.
    python
        Whether this worker can run Python assets. Without `uv` the runtime
        still exists and fails each Python task with the reason.
    stop
        Set when the worker drains; a trim of the package cache then starts no further step.

    Returns
    -------
    PythonRuntime
        Attempt directories under ``<cache>/attempts``, venvs under
        `LAKANA_RUN__VENV_CACHE` or ``<cache>/venvs``, uv's cache under
        ``<cache>/uv``, where ``<cache>`` is `worker_cache_dir()`. Its sweeper
        removes attempt directories unchanged for longer than a task can live (the
        token lifetime plus the venv build timeout), every
        `LAKANA_RUN__SWEEP_INTERVAL` seconds. As often, its cache keeper measures the three
        directories and, with `uv` and `LAKANA_WORKER__PACKAGE_CACHE_MAX_BYTES` above 0, trims
        uv's cache (`lakana_worker.caches`). With `LAKANA_RUN__RUNNER=docker`, a
        `DockerRunner` and `DockerEnvironments` on the launcher at
        `LAKANA_RUN__LAUNCHER_SOCKET`, owned by this worker (`claim_owner`: the host name and a
        token kept in the cache directory, unique among live workers); the attempt directories
        must then be the launcher's work volume, which it mounts by attempt name. With
        `LAKANA_RUN__CA_BUNDLE`, the trust bundle (Mozilla's roots plus that file) is written once
        to ``<cache>/trust.pem``; builds and tasks of both runners trust it.

    Raises
    ------
    SystemExit
        If `LAKANA_RUN__CA_BUNDLE` names a file the containers cannot trust (unreadable, over
        1 MiB, no certificate, a private key); the message names the setting and the cause.
    """
    cache = worker_cache_dir()
    if python.reason() is None:
        try:
            prepare_private_directory(cache / "attempts")
        except (UnsafeDirectory, OSError) as exc:
            logger.warning(
                "The task directory is not usable, so Python tasks fail until it is: %s",
                exc if isinstance(exc, UnsafeDirectory) else f"{cache / 'attempts'}: {exc}",
            )
    run = settings.run
    try:
        trust = build_trust_bundle(run.ca_bundle, cache / TRUST_FILE)
    except TrustBundleError as exc:
        raise SystemExit(str(exc)) from None
    cap, timeout = settings.task_tokens.max_ttl_seconds, settings.query_limits.timeout_seconds
    needed = token_ttl(timeout, run.kill_grace)
    if cap < needed:
        logger.warning(
            "LAKANA_TASK_TOKENS__MAX_TTL_SECONDS is %d, below the %d s a Python task with the "
            "default timeout (%d s) needs; such tasks will fail until it is raised.",
            cap,
            needed,
            timeout,
        )
    paths = CachePaths(
        environments=run.venv_cache or cache / "venvs",
        packages=cache / "uv",
        tasks=cache / "attempts",
    )
    worker = settings.worker
    trim = (
        None
        if python.uv is None or worker.package_cache_max_bytes <= 0
        else PackageCacheTrim(
            paths,
            TrimSettings(
                uv=python.uv,
                max_bytes=worker.package_cache_max_bytes,
                min_age_seconds=worker.package_cache_min_age_hours * 3600,
            ),
        )
    )
    runner: Runner
    environments: DockerEnvironments | None = None
    if run.runner == "docker":
        client = LauncherClient(run.launcher_socket)
        owner = claim_owner(cache, socket.gethostname())
        memory_mb = parse_task_memory(run.task_memory) // 1024**2
        runner = DockerRunner(
            client,
            owner=owner,
            memory_mb=memory_mb,
            cpus=run.task_cpus,
            pids=run.task_pids,
            kill_grace=run.kill_grace,
            trust=trust,
        )
        environments = DockerEnvironments(
            client=client,
            owner=owner,
            memory_mb=memory_mb,
            cpus=run.task_cpus,
            pids=run.task_pids,
            build_timeout=run.venv_build_timeout,
            max_bytes=run.venv_max_bytes,
            operator_index=_secret(run.index_url),
            trust=trust,
        )
    else:
        runner = LocalProcessRunner(
            kill_grace=run.kill_grace,
            memory_limit=resolve_limit(settings.worker.memory_limit).bytes,
            trust=trust,
        )
    return PythonRuntime(
        support=python,
        work_dir=cache / "attempts",
        venvs=VenvSettings(
            uv=python.uv or "uv",
            cache=paths.environments,
            uv_cache=paths.packages,
            keep=run.venv_keep,
            build_timeout=run.venv_build_timeout,
            max_bytes=run.venv_max_bytes,
            index_url=_secret(run.index_url),
            https_proxy=_secret(run.https_proxy),
            no_proxy=run.no_proxy,
            ca_bundle=trust,
        ),
        runner=runner,
        kill_grace=run.kill_grace,
        max_ttl_seconds=settings.task_tokens.max_ttl_seconds,
        limits=DeployLimits(),
        sweeper=AttemptSweeper(
            cache / "attempts",
            max_age=needed + run.venv_build_timeout,
            interval=run.sweep_interval,
        ),
        deploy_sweeper=DeploySweeper(
            max_age=DeployLimits().job_seconds, interval=run.sweep_interval
        ),
        caches=CacheKeeper(paths, interval=run.sweep_interval, trim=trim, stop=stop),
        environments=environments,
    )


def _clean_up_after_restart(runtime: PythonRuntime) -> None:
    """Ask the launcher to remove what this worker's earlier process left running.

    A launcher that cannot be reached is logged and the worker starts anyway: the launcher may
    start after it, and each task then says the same until it does.

    Parameters
    ----------
    runtime
        The runtime; does nothing for the local runner.
    """
    environments = runtime.environments
    if environments is None:
        return
    logger.info(
        "Python tasks run in containers through the launcher at %s (LAKANA_RUN__RUNNER=docker); "
        "attempt directories are in %s, which must be the launcher's work volume.",
        environments.client.socket_path,
        runtime.work_dir,
    )
    try:
        removed = environments.client.cleanup(environments.owner)
    except LauncherUnavailable as exc:
        logger.warning("%s", exc)
        return
    if removed:
        logger.info("Removed %d task containers an earlier process of this worker left.", removed)


def make_worker_id(host: str) -> str:
    """Build a worker's id, `host:pid:token`, short enough for the `workers.id` column.

    The host part is cut when the whole id would not fit; the pid and the random token stay,
    so two workers on one host remain apart.

    Parameters
    ----------
    host
        The host name.

    Returns
    -------
    str
        The id, at most as long as the column.
    """
    suffix = f":{os.getpid()}:{secrets.token_hex(4)}"
    length = cast(int, Worker.id.type.length)  # type: ignore[attr-defined]
    return host[: length - len(suffix)] + suffix


def _has_secrets_key(key_provider: EnvKeyProvider) -> bool:
    """Say whether the worker can read secrets at all.

    Parameters
    ----------
    key_provider
        The worker's key provider.

    Returns
    -------
    bool
        `False` when no master key is configured or it is malformed.
    """
    try:
        _ = key_provider.key_id
    except LakanaError:
        return False
    return True


def main() -> None:
    """Run the scheduler thread and the dequeue loop against the configured database.

    `GrantAuthorization` is built fresh per iteration from that iteration's
    session (it queries grants through it), wrapped in `AuditingAuthorization`
    so a denied check is audited. `EnvKeyProvider` holds no session state and
    serves the worker's whole lifetime. Query limits come from
    `LAKANA_QUERY_LIMITS__*`; deploys use `LAKANA_REPO_ROOTS` and the run caps
    (`LAKANA_QUERY_LIMITS__TIMEOUT_SECONDS`, `LAKANA_RUN__MAX_RETRIES`). The
    worker's id is ``<host>:<pid>:<random>``; it names its leases, attempts
    and, while it holds the role, the scheduler. SIGTERM sets the drain
    event: the loop stops claiming, a running task gets
    `LAKANA_RUN__DRAIN_GRACE_SECONDS` and is then released; a running query gets the same
    grace, is then asked to stop and, `LAKANA_QUERY_LIMITS__KILL_GRACE_SECONDS` later, killed;
    the scheduler thread is stopped and joined (for at least 5 s, else the drain grace) so it
    resigns the role. Whether this worker can run Python assets (`LAKANA_RUN__SERVER_URL`
    set, and `uv` on the `PATH` for the local runner) is logged at startup; the engine gets a
    `PythonRuntime` either way, which runs a Python task as a supervised subprocess (or, with
    `LAKANA_RUN__RUNNER=docker`, a container the launcher starts) while the
    loop keeps serving query runs. A docker worker first asks the launcher to remove the
    containers an earlier process of this worker left (its owner is kept in the cache
    directory); a launcher that cannot be reached is logged and the worker starts anyway.
    The tokens it mints for those tasks are capped by `LAKANA_TASK_TOKENS__MAX_TTL_SECONDS`.
    Before anything else a watchdog thread starts (`LAKANA_WORKER__MEMORY_LIMIT`, default
    4GB): over the limit the worker records why against what it runs, kills its tasks and exits with
    `lakana_worker.memory.EXIT_CODE`. While it holds the scheduler role it
    also sweeps the artifact store every `LAKANA_RUN__SWEEP_INTERVAL` seconds (expired uploads,
    files and manifest objects no version names) and the table locations (the files of creates
    that never finished, after `LAKANA_TABLES__ABANDONED_GRACE_HOURS` or the end of their run),
    in a thread of its own, taking turns between environments; at 0 nothing sweeps, and the
    start-up log says so. When the worker stops it writes
    the denial counts the audit limiter held back as summary rows (`flush_suppressed`).
    The worker registers in the `workers` table and keeps a heartbeat there every
    `LAKANA_RUN__RENEW_SECONDS`, in a thread of its own that starts with the scheduler;
    every run it starts carries its id. A worker whose heartbeat has not been written for
    `LAKANA_RUN__LEASE_SECONDS` claims nothing and stops the query it runs, and it claims
    nothing before its first heartbeat. After a clean stop the thread is stopped and joined and
    the row removed; after an error, or when the thread does not end, the row is left to age out.
    Job notifications are delivered by a thread of their own (`DeliveryLoop`) unless
    `LAKANA_WORKER__NOTIFICATIONS=false`; a drain stops it claiming (it holds no unsent row). A
    worker with no usable secrets master key starts none and says so.
    The worker's `LAKANA_WORKER__TAKES` (`all`, `interactive` or `pipeline`) decides
    what its dequeue loop takes (`WorkerPool`, handed to `poll_forever` with the cap on one
    principal's running ad-hoc queries, `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL`, and the
    lease); the start-up log says which. The scheduler thread, the heartbeat and the sweeps run
    whatever it is: an interactive worker can hold the scheduler role.
    Logging is set up once the settings are read (`configure_logging`, level
    `LAKANA_LOG__LEVEL`, INFO by default), so those lines reach stderr
    (`docker compose logs worker`). At
    start-up the worker refuses to run on a Python without `os.waitid` (macOS before 3.13),
    resolves and logs the CA bundle of its query processes (`lakana_worker.ca_bundle`; a bad
    ``AWS_CA_BUNDLE`` or ``SSL_CERT_FILE`` stops it with a message), and
    runs one query child that only sets the two sizes: a memory limit or
    temp-directory size DuckDB cannot read, or a child that cannot start at all, stops the worker
    with a message that names the cause.
    """
    settings = Settings()
    configure_logging(settings.log.level)
    _check_process_supervision()
    _check_query_ca_bundle()
    _check_query_limits(settings.query_limits)
    engine = create_engine(settings.database, lease_seconds=settings.run.lease_seconds)
    session_factory = functools.partial(Session, engine)
    worker_id = make_worker_id(socket.gethostname())
    start_watchdog(
        settings.worker.memory_limit, session_factory, secrets.SystemRandom().random, worker_id
    )
    sweep_spill_directories()
    if settings.run.sweep_interval > 0:
        sweep_deploy_checkouts(max_age=DeployLimits().job_seconds)
    _check_ca_bundle(settings.git.ca_bundle)
    _check_ca_bundle(settings.run.ca_bundle, "LAKANA_RUN__CA_BUNDLE")
    _check_ca_bundle(settings.webhook.ca_bundle, "LAKANA_WEBHOOK__CA_BUNDLE")
    warn_about_tokens_in_url_paths(
        logger,
        {
            "LAKANA_RUN__INDEX_URL": settings.run.index_url,
            "LAKANA_RUN__HTTPS_PROXY": settings.run.https_proxy,
            "LAKANA_GIT__HTTPS_PROXY": settings.git.https_proxy,
            "LAKANA_WEBHOOK__PROXY": settings.webhook.proxy,
        },
    )
    python = detect(settings.run)
    reason = python.reason()
    if reason is None and settings.run.runner == "docker":
        logger.info("Python assets: enabled (in containers).")
    elif reason is None:
        logger.info("Python assets: enabled (uv at %s).", python.uv)
    else:
        logger.info("Python assets: disabled: %s. To enable it: %s.", reason, python.fix())
    logger.info(_TAKES_LINES[settings.worker.takes])
    key_provider = EnvKeyProvider(
        settings.secrets.master_key, accept_format_1=settings.secrets.accept_format_1
    )
    if settings.secrets.accept_format_1:
        logger.warning(ACCEPT_FORMAT_1_WARNING)
    drain = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: drain.set())
    install_drain(drain, settings.run.drain_grace_seconds)
    install_maintenance_timeout(settings.run.maintenance_timeout)
    install_query_text(settings.audit.query_text)
    runtime = _python_runtime(settings, python, drain)
    _clean_up_after_restart(runtime)
    limiter = DenialAuditLimiter(settings.denial_audit.per_principal_per_minute)
    authorization = functools.partial(_authorization, limiter=limiter)
    run_engine = RunEngine(
        session_factory=session_factory,
        authz_factory=authorization,
        settings=TaskSettings(
            worker_id=worker_id,
            lease_seconds=settings.run.lease_seconds,
            renew_seconds=settings.run.renew_seconds,
            drain_grace_seconds=settings.run.drain_grace_seconds,
            limits=settings.query_limits,
        ),
        key_provider=key_provider,
        drain=drain,
        python=runtime,
    )

    if settings.run.renew_seconds * 2 > settings.run.lease_seconds:
        logger.warning(
            "LAKANA_RUN__RENEW_SECONDS (%d) is more than half of LAKANA_RUN__LEASE_SECONDS (%d): "
            "one late heartbeat makes this worker count as lost, and its queries and deploys "
            "are then ended. Set the lease to at least twice the renew interval, with the same "
            "values for the server and every worker.",
            settings.run.renew_seconds,
            settings.run.lease_seconds,
        )
    heartbeat = WorkerHeartbeat(
        session_factory,
        worker_id,
        host=socket.gethostname(),
        version=platform_version(),
        renew_seconds=settings.run.renew_seconds,
        lease_seconds=settings.run.lease_seconds,
        takes=settings.worker.takes,
        caches=None if runtime.caches is None else runtime.caches.latest,
    )
    install_heartbeat(heartbeat.stale)

    sweeps = itertools.count()

    def housekeeping(now: datetime, stop: threading.Event) -> None:
        """Sweep artifacts, table locations (and expire tables), code archives, job fires and reads.

        Also stores the columns of up to 50 tables that have none stored yet, so search finds them,
        and the sizes of up to 200 managed tables never measured or changed since, for
        `lakana env usage`.

        One sweep failing does not stop the others.

        Parameters
        ----------
        now
            The scheduler step's time.
        stop
            Set at shutdown; the sweeps end at their next decision.
        """
        rotation = next(sweeps)
        try:
            with session_factory() as session:
                sweep_artifacts(
                    session,
                    StorageResolver(session, key_provider),
                    now=now,
                    upload_ttl_seconds=settings.artifacts.upload_ttl_seconds,
                    rotation=rotation,
                    should_stop=stop.is_set,
                )
        except Exception as exc:
            logger.warning(
                "The artifact sweep failed (%s).",
                type(exc).__name__,
                exc_info=True,
                extra=log_event(LogEvent.SWEEP_FAILED),
            )
        try:
            with session_factory() as session:
                sweep_table_locations(
                    session,
                    StorageResolver(session, key_provider),
                    now=now,
                    grace_hours=settings.tables.abandoned_grace_hours,
                    undo_days=settings.tables.undo_days,
                    max_expiries=settings.tables.max_expiries_per_sweep,
                    rotation=rotation,
                    should_stop=stop.is_set,
                )
        except Exception as exc:
            logger.warning(
                "The table location sweep failed (%s).",
                type(exc).__name__,
                exc_info=True,
                extra=log_event(LogEvent.SWEEP_FAILED),
            )
        try:
            with session_factory() as session:
                sweep_code_archives(
                    session,
                    StorageResolver(session, key_provider),
                    now=now,
                    rotation=rotation,
                    should_stop=stop.is_set,
                )
        except Exception as exc:
            logger.warning(
                "The code archive sweep failed (%s).",
                type(exc).__name__,
                exc_info=True,
                extra=log_event(LogEvent.SWEEP_FAILED),
            )
        try:
            with session_factory() as session:
                sweep_job_fires(session)
        except Exception as exc:
            logger.warning(
                "The job fire sweep failed (%s).",
                type(exc).__name__,
                exc_info=True,
                extra=log_event(LogEvent.SWEEP_FAILED),
            )
        try:
            with session_factory() as session:
                sweep_table_reads(session, keep_days=settings.table_reads.keep_days, now=now)
        except Exception as exc:
            logger.warning(
                "The table reads sweep failed (%s).",
                type(exc).__name__,
                exc_info=True,
                extra=log_event(LogEvent.SWEEP_FAILED),
            )
        try:
            with session_factory() as session:
                measure_table_sizes(
                    session, StorageResolver(session, key_provider), should_stop=stop.is_set
                )
        except Exception as exc:
            logger.warning(
                "The table size measurement failed (%s).",
                type(exc).__name__,
                exc_info=True,
                extra=log_event(LogEvent.SWEEP_FAILED),
            )
        try:
            with session_factory() as session:
                backfill_table_columns(session, StorageResolver(session, key_provider))
        except Exception as exc:
            logger.warning(
                "The table column backfill failed (%s).",
                type(exc).__name__,
                exc_info=True,
                extra=log_event(LogEvent.SWEEP_FAILED),
            )

    if settings.run.sweep_interval <= 0:
        logger.info(
            "LAKANA_RUN__SWEEP_INTERVAL=0: the sweeps are off, so expired artifact uploads, "
            "artifact files nothing names, tables whose retention has passed, the files of "
            "dropped tables and of tables whose create never finished, "
            "code archives of failed deploys, table read counts past "
            "their kept period and attempt directories and deploy checkouts of dead workers are "
            "not removed, the columns of tables that existed before search are not stored "
            "for it, the sizes of tables and of the worker's caches are not measured, and uv's "
            "package cache is not trimmed. Set it to a number "
            "of seconds (3600 is the default) to turn them on."
        )
    scheduler = SchedulerLoop(
        engine,
        worker_id,
        interval=settings.run.scheduler_interval_seconds,
        housekeeping=housekeeping,
        housekeeping_interval=settings.run.sweep_interval,
        housekeeping_limit=DEFAULT_MAX_SECONDS
        + LOCATION_SWEEP_MAX_SECONDS
        + CODE_SWEEP_MAX_SECONDS
        + SIZE_MEASURE_MAX_SECONDS,
        lease_seconds=settings.run.lease_seconds,
        renew_seconds=settings.run.renew_seconds,
        caps=RunCaps.from_config(settings.query_limits, settings.run),
    )
    scheduler_thread = scheduler.start()
    heartbeat_thread = heartbeat.start()
    delivery: DeliveryLoop | None = None
    delivery_thread: threading.Thread | None = None
    if settings.worker.notifications and not _has_secrets_key(key_provider):
        logger.warning(
            "This worker delivers no job notifications: it has no usable secrets master key. "
            "Set LAKANA_SECRETS__MASTER_KEY (the same value on every worker) and restart it; "
            "other workers deliver meanwhile."
        )
    elif settings.worker.notifications:
        # The drain event stops its claiming; one row is claimed at a time, so none is left held.
        delivery = DeliveryLoop(
            engine,
            NotifySettings(owner=worker_id, key_provider=key_provider, webhook=settings.webhook),
            stop=drain,
        )
        delivery_thread = delivery.start()
    else:
        logger.info(
            "LAKANA_WORKER__NOTIFICATIONS=false: this worker delivers no job notifications. "
            "Another worker must, or queued notifications wait."
        )
    try:
        poll_forever(
            session_factory,
            authorization,
            key_provider,
            limits=settings.query_limits,
            deploy=DeploySettings(
                repo_roots=tuple(settings.repo_roots),
                caps=RunCaps.from_config(settings.query_limits, settings.run),
                limits=DeployLimits(statement_chars=settings.query_limits.max_statement_chars),
                ca_bundle=settings.git.ca_bundle,
                https_proxy=_secret(settings.git.https_proxy),
                no_proxy=settings.git.no_proxy,
                index_url=_secret(settings.run.index_url),
                python=python,
            ),
            engine=run_engine,
            drain=drain,
            maintenance=runtime.maintain,
            worker_id=worker_id,
            may_claim=heartbeat.may_claim,
            pool=WorkerPool(
                takes=settings.worker.takes,
                running_per_principal=settings.query_limits.running_per_principal,
                lease_seconds=settings.run.lease_seconds,
            ),
        )
    finally:
        scheduler.stop()
        join_timeout = max(settings.run.drain_grace_seconds, _SCHEDULER_JOIN_FLOOR_SECONDS)
        scheduler_thread.join(timeout=join_timeout)
        if scheduler_thread.is_alive():
            logger.warning("The scheduler thread did not stop within %.0f s.", join_timeout)
        if delivery is not None and delivery_thread is not None:
            delivery.stop()
            delivery_thread.join(timeout=join_timeout)
            if delivery_thread.is_alive():
                logger.warning("The notification thread did not stop within %.0f s.", join_timeout)
        heartbeat.stop()
        heartbeat_thread.join(timeout=_SCHEDULER_JOIN_FLOOR_SECONDS)
        heartbeat_ended = not heartbeat_thread.is_alive()
        with session_factory() as session:
            flush_suppressed(limiter, session)
    if heartbeat_ended:
        heartbeat.unregister()
    else:
        logger.warning(
            "The heartbeat thread did not stop within %.0f s (a write to the database is "
            "hanging), so the worker's row is left to age out instead of being removed.",
            _SCHEDULER_JOIN_FLOOR_SECONDS,
        )


if __name__ == "__main__":  # pragma: no cover — exercised only by `python -m lakana_worker`
    from lakana_worker.command import cli

    cli()
