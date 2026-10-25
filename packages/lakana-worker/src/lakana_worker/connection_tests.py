"""The connection test loop: claim a queued test, run the check, record what it found.

`ConnectionTestLoop` mirrors `notify.DeliveryLoop`: a daemon thread claims one test at a time
under a lease (`ConnectionTestQueue.claim`), runs `lakana.ingest._check` with no transaction
open and records the outcome while the lease is still this worker's
(`ConnectionTestQueue.finish`; a
worker that lost it records nothing and logs at info level).

Under the local runner the check is a subprocess of the worker's own Python
(``python -I -m lakana.ingest._check``): Lakana's own code, never the project's. Under the
Docker runner the launcher's ``check`` op runs it in a container of the task image, from the
image's copy of the same file (``/opt/lakana-check/check.py``), with the network rules a task
with this one connection would get (`connection_plan`): its token for the egress proxy, its
relay and its read-only root, and the worker's trust bundle in a private attempt directory
(``attempt-check_*``) that is removed afterwards. Either way the connection and its secret
travel on the check's standard input, never in its arguments or environment, and its message is
masked once more here before it is stored. Neither the secret nor an exception's text appears in
a log line: a failure is logged by its type.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import posixpath
import subprocess  # nosec B404 -- runs Lakana's own module with a fixed argument list
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from lakana_core.connections.endpoints import endpoints
from lakana_core.connections.fetch import connection_secret
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.connections.service import ConnectionTestQueue
from lakana_core.db import db_now
from lakana_core.models import Connection, ConnectionTest, Environment
from lakana_core.secrets.keys import KeyProvider
from lakana_worker.launcher.requests import CHECK_LIMITS, INGEST_MOUNT, allowed_roots
from lakana_worker.launcher_client import (
    CHECK_WAIT_SECONDS,
    RELEASE_SKEW,
    LauncherRefused,
    LauncherTimedOut,
    LauncherUnavailable,
    release_skew,
)
from lakana_worker.processes import (
    OutputRedactor,
    UnsafeDirectory,
    prepare_private_directory,
    remove_private_tree,
)
from lakana_worker.python_tasks import ConnectionPlan, connection_plan, local_root_dir
from lakana_worker.runners import MISSING_ROOT
from lakana_worker.task_outcome import TaskFailure
from lakana_worker.trust import install_trust

logger = logging.getLogger(__name__)

IDLE_SECONDS = 2.0
"""How long the loop sleeps when no test is queued."""

LEASE_SECONDS = 60
"""How long a claimed test belongs to this worker; a check takes at most about 30 s."""

CHECK_TIMEOUT_SECONDS = 30
"""The time the check subprocess may take."""

FAILURE_LOG_SECONDS = 60.0
"""Least time between two log lines about a failing step."""

MAX_IDLE_SECONDS = 60.0
"""The longest sleep after consecutive failing steps."""

OUTCOMES = frozenset(
    {"ok", "dns", "refused", "timeout", "tls", "not_allowed", "root_unavailable", "not_found"}
    | {"credentials", "failed"}
)
"""What the check may report; anything else is a failed check."""

NO_MASTER_KEY = (
    "this worker cannot decrypt secrets: set LAKANA_SECRETS__MASTER_KEY_FILE as on the server"
)
CANNOT_DECRYPT = (
    "this worker cannot decrypt the connection's secret: check that its master key is the one "
    "the server uses, or set the secret again"
)
NOT_BOUND = (
    "the connection's secret is not bound to it; bind it again (see `lakana connection update "
    "--help`)"
)
SECRET_GONE = (  # nosec B105 -- a message about a secret, not a credential
    "the connection's secret is revoked or missing; bind another (see `lakana connection update "
    "--help`)"
)
NO_LAKANA = "install lakana next to the worker to run connection tests"
CHECK_TIMED_OUT = (
    f"the check did not finish within {CHECK_TIMEOUT_SECONDS} s; an endpoint may accept "
    "connections but never answer: check each host and port"
)
NOT_RUN = "the check could not run (see the worker's log)"
CHECK_BROKEN = "the check ended without an answer (see the worker's log)"
LAUNCHER_LOG = "see `docker compose logs launcher`"
CHECK_OOM = (
    f"the check ran out of its {CHECK_LIMITS.memory_mb} MiB of memory before it answered; "
    f"{LAUNCHER_LOG}"
)
CHECK_STOPPED = f"the launcher stopped the check before it answered; {LAUNCHER_LOG}"
NO_ATTEMPT_DIR = (
    "the worker's task directory is not private; an operator must make it a real directory "
    "owned by the worker's user with mode 0700 (the worker log names it)"
)
CHECK_PREFIX = "attempt-check_"
"""Starts the name of a check's attempt directory; `tempfile.mkdtemp` adds eight characters."""
PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
"""The task image a check runs in: the worker's own Python, as for a task that names none."""


class CheckClient(Protocol):
    """What the Docker runner's check needs of `LauncherClient`."""

    def check(
        self, fields: Mapping[str, object], *, timeout: float = CHECK_WAIT_SECONDS
    ) -> tuple[dict[str, object], bytes]:
        """Run one check and collect its output.

        Parameters
        ----------
        fields
            The ``check`` request's fields without ``op`` and the token.
        timeout
            Seconds to the result.

        Returns
        -------
        tuple
            The launcher's result and the check's output.
        """
        ...  # pragma: no cover


@dataclass(frozen=True)
class DockerCheck:
    """What a worker with the Docker runner runs its checks with.

    Parameters
    ----------
    client
        The launcher.
    owner
        This worker's launcher owner (`claim_owner`), the one its tasks run under.
    work_dir
        The attempts directory, the launcher's work volume.
    trust
        The worker's trust bundle (`LAKANA_RUN__CA_BUNDLE`), written into each check's attempt;
        `None` for none.
    """

    client: CheckClient
    owner: str
    work_dir: Path
    trust: bytes | None = None


def _mask(text: str, secrets: list[str]) -> str:
    """Replace every secret value in a text, as the supervisors mask a task's output.

    Parameters
    ----------
    text
        The text.
    secrets
        The values.

    Returns
    -------
    str
        The text with each value, and any ``://userinfo@``, replaced by ``***``.
    """
    masked = bytearray()
    redactor = OutputRedactor(secrets, masked.extend)
    redactor.feed(text.encode("utf-8", "replace"))
    redactor.close()
    return masked.decode("utf-8", "replace")


def _answer(output: str) -> tuple[str, str] | None:
    """Read the check's answer: the last line of its output that is one.

    Parameters
    ----------
    output
        What the check printed; a container's output may hold other lines before it.

    Returns
    -------
    tuple of str or None
        The outcome and message; `None` when no line is an answer with a known outcome.
    """
    for line in reversed(output.strip().splitlines()):
        try:
            answer = json.loads(line)
            outcome, message = str(answer["outcome"]), str(answer["message"])
        except (ValueError, KeyError, TypeError):
            continue
        return (outcome, message) if outcome in OUTCOMES else None
    return None


def _skew(refusal: str) -> bool:
    """Say whether a refusal means the worker and the launcher are different releases.

    Parameters
    ----------
    refusal
        The launcher's reason.

    Returns
    -------
    bool
        `True` for a launcher that does not know the ``check`` op (an earlier release) or
        disagrees on its fields.
    """
    return release_skew(refusal) or refusal.startswith("op must be one of")


def _root_refusal(refusal: str, connection: str, roots: list[str]) -> str | None:
    """Say whether the launcher refused the check over the connection's ingestion root.

    Parameters
    ----------
    refusal
        The launcher's reason.
    connection
        The connection's name.
    roots
        The roots the check asked for: none, or the connection's one.

    Returns
    -------
    str or None
        `MISSING_ROOT` when the launcher does not list the root; the launcher's own reason when
        its probe found the root unsafe (it names the root and what to do); `None` otherwise.
    """
    for root in roots:
        allowed = allowed_roots(refusal)
        if allowed is not None and root not in allowed:
            return MISSING_ROOT.format(connection=connection, root=root)
        if f"ingestion root '{root}'" in refusal:
            return refusal
    return None


def _has_check() -> bool:
    """Say whether the worker's Python can import the check.

    Returns
    -------
    bool
        `False` when `lakana` is not installed next to the worker. The subprocess runs the same
        interpreter, so what is found here is what it will import.
    """
    try:
        return importlib.util.find_spec("lakana.ingest._check") is not None
    except (ImportError, ValueError):
        return False


class ConnectionTestLoop:
    """Run queued connection tests on a daemon thread until it is stopped.

    Parameters
    ----------
    engine
        The metadata database.
    owner
        This worker's id; it names the leases.
    key_provider
        Unwraps a connection's secret.
    runner_kind
        The worker's runner: ``local`` or ``docker``.
    ingestion_roots
        The local runner's ingestion roots (name to directory), for ``directory`` and
        ``sqlite-file`` connections; the Docker runner's launcher mounts its own.
    docker
        The launcher, owner, work directory and trust bundle of the Docker runner; required
        with ``docker``, ignored otherwise.
    stop
        The event that ends the loop; a new one when `None`.
    idle_seconds
        The sleep when no test was queued, and after a failing step.
    trust
        A PEM file the check trusts in addition to Python's own roots (the local runner's trust
        bundle), passed as ``LAKANA_CA_BUNDLE``; `None` for none. The Docker runner's bundle
        is `DockerCheck.trust`.

    Raises
    ------
    ValueError
        For a runner other than ``local`` and ``docker``, and for ``docker`` without `docker`:
        a check on the worker's own network would test a different path than a task takes.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        owner: str,
        key_provider: KeyProvider,
        runner_kind: str,
        ingestion_roots: Mapping[str, str],
        docker: DockerCheck | None = None,
        stop: threading.Event | None = None,
        idle_seconds: float = IDLE_SECONDS,
        trust: Path | None = None,
    ) -> None:
        if runner_kind not in ("local", "docker"):
            raise ValueError(
                f"connection tests run under the local or the docker runner, not the runner "
                f"`{runner_kind}`"
            )
        if runner_kind == "docker" and docker is None:
            raise ValueError("the Docker runner's connection check needs the launcher")
        self._engine = engine
        self._owner = owner
        self._keys = key_provider
        self._roots = dict(ingestion_roots)
        self._docker = docker if runner_kind == "docker" else None
        self._stop = stop if stop is not None else threading.Event()
        self._idle = idle_seconds
        self._trust = trust

    def _has_master_key(self) -> bool:
        """Say whether this worker can read secrets at all.

        Returns
        -------
        bool
            `False` when no master key is configured or it is malformed.
        """
        try:
            _ = self._keys.key_id
        except Exception:  # LakanaError, whatever the provider raises: no usable key
            return False
        return True

    def _root_dir(self, root: str, connection: str) -> str:
        """Say where the check reads an ingestion root.

        Parameters
        ----------
        root
            The root's name.
        connection
            The connection naming it, for the message of a missing root.

        Returns
        -------
        str
            ``/ingest/<root>`` under the Docker runner (the launcher mounts it, or refuses a
            root it lacks); the worker's own directory for it under the local runner.

        Raises
        ------
        TaskFailure
            Under the local runner, for a root the worker lacks.
        """
        if self._docker is not None:
            return posixpath.join(INGEST_MOUNT, root)
        return local_root_dir(self._roots, root, connection)

    def _request(
        self, session: Session, test: ConnectionTest
    ) -> tuple[dict[str, Any], ConnectionPlan] | tuple[str, str]:
        """Read the connection of a claimed test and build the check's input.

        Parameters
        ----------
        session
            The session; the secret is decrypted here and goes out of scope with the caller.
        test
            The claimed test.

        Returns
        -------
        tuple
            ``(connection, plan)`` for the check, the plan a task declaring only this connection
            would get (`connection_plan`); or ``(outcome, message)`` as two strings when the
            check cannot run (a missing key, secret or root).
        """
        row = session.get(Connection, test.connection_id)
        env = session.get(Environment, test.environment_id)
        if row is None or env is None:
            return "failed", "the connection no longer exists"
        secret: dict[str, str] | None = None
        if row.secret_id is not None:
            if not self._has_master_key():
                return "failed", NO_MASTER_KEY
            secret, reason = connection_secret(session, self._keys, row, env.name)
            if reason == "secret_not_bound":
                return "failed", NOT_BOUND
            if reason == "secret_unreadable":
                return "failed", CANNOT_DECRYPT
            if secret is None:
                return "failed", SECRET_GONE
        kind = ConnectionKind(row.kind)
        try:
            plan = connection_plan(
                [(row.name, kind, row.address, row.settings)],
                isolated=self._docker is not None,
                root_dir=self._root_dir,
            )
        except TaskFailure as failure:
            return "root_unavailable", failure.message
        connection = {
            "name": row.name,
            "kind": kind.value,
            "address": row.address,
            "settings": row.settings,
            "endpoints": [
                {"host": e.host, "port": e.port, "role": e.role}
                for e in endpoints(kind, row.address, row.settings)
            ],
            "secret": secret,
        }
        return connection, plan

    def _run_check(self, connection: dict[str, Any], plan: ConnectionPlan) -> tuple[str, str]:
        """Run the check the way this worker's runner runs a task.

        Parameters
        ----------
        connection
            The check's ``connection`` input, secret included.
        plan
            The connection's plan: its routing is the check's ``routing`` input.

        Returns
        -------
        tuple of str
            The outcome and message, masked.
        """
        secrets = [v for v in (connection["secret"] or {}).values() if isinstance(v, str)]
        routing = plan.routing[connection["name"]]
        if self._docker is not None:
            outcome, message = self._run_in_container(self._docker, connection, plan)
        else:
            outcome, message = self._run_locally(connection, routing)
        return outcome, _mask(message, secrets)

    def _run_locally(self, connection: dict[str, Any], routing: dict[str, Any]) -> tuple[str, str]:
        """Run the check in a subprocess of this worker's Python.

        Parameters
        ----------
        connection
            The check's ``connection`` input, secret included.
        routing
            The check's ``routing`` input.

        Returns
        -------
        tuple of str
            The outcome and message, unmasked. A check that did not run, did not finish or
            answered nonsense is ``failed`` (``timeout`` for the time limit), with a message
            that names the next step.
        """
        if not _has_check():
            return "failed", NO_LAKANA
        env = {"LAKANA_CA_BUNDLE": str(self._trust)} if self._trust is not None else {}
        try:
            done = subprocess.run(  # nosec B603 -- fixed argv, own module
                [sys.executable, "-I", "-m", "lakana.ingest._check"],
                input=json.dumps({"connection": connection, "routing": routing}),
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT_SECONDS,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("A connection check did not finish within %d s.", CHECK_TIMEOUT_SECONDS)
            return "timeout", CHECK_TIMED_OUT
        answer = _answer(done.stdout)
        if answer is None:
            logger.warning("A connection check ended without an answer (exit %s).", done.returncode)
            return "failed", CHECK_BROKEN
        return answer

    def _run_in_container(
        self, docker: DockerCheck, connection: dict[str, Any], plan: ConnectionPlan
    ) -> tuple[str, str]:
        """Have the launcher run the check in a container of the task image.

        The request carries the routes of `connection_plan`, the ones a task with this one
        connection gets: the proxy pairs of an HTTPS kind, a relay route per endpoint of a
        database or ``tcp`` kind, the root of a mounted kind; and the trust bundle, written into
        a private attempt directory that is removed when the check ends.

        Parameters
        ----------
        docker
            The launcher, owner, work directory and trust bundle.
        connection
            The check's ``connection`` input, secret included.
        plan
            The connection's routes and routing.

        Returns
        -------
        tuple of str
            The outcome and message, unmasked. A launcher that refused, could not be reached or
            did not answer in time makes ``failed`` with its reason and the launcher's log.
        """
        routing = plan.routing[connection["name"]]
        roots = list(plan.roots)
        try:
            prepare_private_directory(docker.work_dir)
            attempt = Path(tempfile.mkdtemp(prefix=CHECK_PREFIX, dir=docker.work_dir))
        except UnsafeDirectory:
            logger.warning(
                "The task directory %s is not private: it must be a real directory owned by the "
                "worker's user with mode 0700. Connection tests fail until it is.",
                docker.work_dir,
            )
            return "failed", NO_ATTEMPT_DIR
        except OSError as exc:
            return "failed", f"could not prepare the check's directory: {exc.strerror or exc}"
        try:
            if docker.trust is not None:
                try:
                    install_trust(docker.trust, attempt)
                except OSError as exc:
                    return "failed", (
                        f"could not prepare the check's directory: {exc.strerror or exc}"
                    )
            fields: dict[str, object] = {
                "check": uuid.uuid4().hex,
                "owner": docker.owner,
                "python": PYTHON,
                "attempt": attempt.name,
                "stdin": json.dumps({"connection": connection, "routing": routing}),
                "roots": roots,
                "trust": docker.trust is not None,
                "egress": [[host, port] for host, port in plan.egress],
                "relay": [
                    {"listen": listen, "host": host, "port": port}
                    for listen, host, port in plan.relay
                ],
            }
            try:
                result, output = docker.client.check(fields, timeout=CHECK_WAIT_SECONDS)
            except LauncherRefused as exc:
                root = _root_refusal(str(exc), connection["name"], roots)
                if root is not None:
                    return "root_unavailable", root
                next_step = RELEASE_SKEW if _skew(str(exc)) else LAUNCHER_LOG
                return "failed", f"the launcher refused the check: {exc}; {next_step}"
            except (LauncherUnavailable, LauncherTimedOut) as exc:
                return "failed", f"{exc}; {LAUNCHER_LOG}"
        finally:
            if not remove_private_tree(attempt):
                logger.warning("The check's directory %s was not removed completely.", attempt)
        if result.get("timed_out") is True:
            return "timeout", CHECK_TIMED_OUT
        if result.get("oom_killed") is True:
            return "failed", CHECK_OOM
        if result.get("stopped") is True:
            return "failed", CHECK_STOPPED
        answer = _answer(output.decode("utf-8", "replace"))
        if answer is None:
            logger.warning(
                "A connection check ended without an answer (exit %s).", result.get("exit")
            )
            return "failed", CHECK_BROKEN
        return answer

    def step(self) -> bool:
        """Claim one test, run it and record the result.

        Returns
        -------
        bool
            `True` when a test was claimed (whether or not its result could be recorded);
            `False` when none was queued.
        """
        with Session(self._engine) as session:
            test = ConnectionTestQueue(session).claim(self._owner, now=db_now(session))
            if test is None:
                return False
            test_id, expires = test.id, test.lease_expires_at
            try:
                prepared = self._request(session, test)
            except Exception as exc:
                # The text of a decryption or database error could quote a secret.
                logger.warning("A connection test could not be prepared (%s).", type(exc).__name__)
                prepared = ("failed", NOT_RUN)
            session.rollback()
        # No transaction is open from here until the result is recorded.
        if isinstance(prepared[0], str):
            outcome, message = str(prepared[0]), str(prepared[1])
        else:
            try:
                outcome, message = self._run_check(prepared[0], prepared[1])
            except Exception as exc:
                logger.warning("A connection check failed to run (%s).", type(exc).__name__)
                outcome, message = "failed", NOT_RUN
        del prepared
        with Session(self._engine) as session:
            recorded = ConnectionTestQueue(session).finish(
                test_id, self._owner, expires, outcome, message
            )
        if not recorded:
            logger.info("A connection test's lease was lost during its check; nothing recorded.")
        return True

    def run(self) -> None:
        """Run tests until `stop`.

        A failing step (the database is down, say) is logged by its exception's type only, at
        most once a minute, and the sleep grows to a minute while it keeps failing.
        """
        failures = 0
        last_logged = -FAILURE_LOG_SECONDS
        while not self._stop.is_set():
            worked = False
            try:
                worked = self.step()
                failures = 0
            except Exception as exc:
                failures += 1
                # The text of a database or subprocess error could quote a secret.
                if time.monotonic() - last_logged >= FAILURE_LOG_SECONDS:
                    last_logged = time.monotonic()
                    logger.warning(
                        "A connection test step failed (%s); further failures are logged once "
                        "a minute.",
                        type(exc).__name__,
                    )
            if failures:
                self._stop.wait(
                    min(self._idle * 2 ** min(failures, 10), max(self._idle, MAX_IDLE_SECONDS))
                )
            elif not worked:
                self._stop.wait(self._idle)

    def start(self) -> threading.Thread:
        """Run `run` in a daemon thread.

        Returns
        -------
        threading.Thread
            The started thread, for the caller to join after `stop`.
        """
        thread = threading.Thread(target=self.run, name="lakana-connection-tests", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Ask the thread to end after the test it is running."""
        self._stop.set()
