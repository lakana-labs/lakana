"""The connection test loop: claim a queued test, run the check, record what it found.

`ConnectionTestLoop` mirrors `notify.DeliveryLoop`: a daemon thread claims one test at a time
under a lease (`ConnectionService.claim_test`), runs `lakana.ingest._check` with no transaction
open and records the outcome while the lease is still this worker's (`finish_test`; a worker
that lost it records nothing and logs at info level).

Under the local runner the check is a subprocess of the worker's own Python
(``python -I -m lakana.ingest._check``): Lakana's own code, never the project's. The connection
and its secret travel on its standard input, never in its arguments or environment, and its
message is masked once more here before it is stored. Neither the secret nor an exception's text
appears in a log line: a failure is logged by its type.

The Docker runner's check (a container of the task image) arrives with Task 20b; until then the
loop refuses to be built for it, and a test stays queued.
"""

from __future__ import annotations

import json
import logging
import subprocess  # nosec B404 -- runs Lakana's own module with a fixed argument list
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization
from lakana_core.connections.endpoints import endpoints
from lakana_core.connections.fetch import connection_secret
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.connections.service import ConnectionService
from lakana_core.db import db_now
from lakana_core.models import Connection, ConnectionTest, Environment
from lakana_core.secrets.keys import KeyProvider
from lakana_worker.processes import OutputRedactor
from lakana_worker.python_tasks import connection_routing, local_root_dir
from lakana_worker.task_outcome import TaskFailure

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
CHECK_TIMED_OUT = f"the check did not finish within {CHECK_TIMEOUT_SECONDS} s"
CHECK_BROKEN = "the check ended without an answer (see the worker's log)"


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
        The worker's runner. Only ``local`` is supported in this version.
    ingestion_roots
        The worker's ingestion roots (name to directory), for ``directory`` and ``sqlite-file``
        connections.
    client
        Reserved for the Docker runner's launcher client (Task 20b); unused.
    stop
        The event that ends the loop; a new one when `None`.
    idle_seconds
        The sleep when no test was queued, and after a failing step.
    trust
        A PEM file the check trusts in addition to Python's own roots (the local runner's trust
        bundle), passed as ``LAKANA_CA_BUNDLE``; `None` for none.

    Raises
    ------
    ValueError
        For a runner other than ``local``: the Docker runner's check is not built yet, and
        running it on the worker's own network would test a different path than a task takes.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        owner: str,
        key_provider: KeyProvider,
        runner_kind: str,
        ingestion_roots: Mapping[str, str],
        client: object | None = None,
        stop: threading.Event | None = None,
        idle_seconds: float = IDLE_SECONDS,
        trust: Path | None = None,
    ) -> None:
        if runner_kind != "local":
            raise ValueError(
                f"connection tests run under the local runner only (this worker's runner is "
                f"`{runner_kind}`); the Docker runner's check is not available yet"
            )
        self._engine = engine
        self._owner = owner
        self._keys = key_provider
        self._roots = dict(ingestion_roots)
        self._client = client
        self._stop = stop if stop is not None else threading.Event()
        self._idle = idle_seconds
        self._trust = trust

    def _service(self, session: Session) -> ConnectionService:
        """Build the service that claims and finishes tests.

        Parameters
        ----------
        session
            The session.

        Returns
        -------
        ConnectionService
            A service whose claim and finish need no principal.
        """
        return ConnectionService(session, AllowAllAuthorization(), self._keys)

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

    def _request(
        self, session: Session, test: ConnectionTest
    ) -> tuple[dict[str, Any], dict[str, Any]] | tuple[str, str]:
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
            ``(connection, routing)`` for the check; or ``(outcome, message)`` as two strings
            when the check cannot run (a missing key, secret or root).
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
            routing = connection_routing(
                row.name,
                kind,
                row.address,
                row.settings,
                root_dir=lambda root: local_root_dir(self._roots, root, row.name),
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
        return connection, routing

    def _run_check(self, connection: dict[str, Any], routing: dict[str, Any]) -> tuple[str, str]:
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
            The outcome and message, masked. A check that did not run, did not finish or
            answered nonsense is ``failed`` (``timeout`` for the time limit), with a message
            that names the next step.
        """
        secrets = [v for v in (connection["secret"] or {}).values() if isinstance(v, str)]
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
            return "timeout", CHECK_TIMED_OUT
        if "No module named" in done.stderr and "lakana" in done.stderr:
            return "failed", NO_LAKANA
        try:
            answer = json.loads(done.stdout.strip().splitlines()[-1])
            outcome, message = str(answer["outcome"]), str(answer["message"])
        except (IndexError, ValueError, KeyError, TypeError):
            logger.warning("A connection check ended without an answer (exit %s).", done.returncode)
            return "failed", CHECK_BROKEN
        if outcome not in OUTCOMES:
            return "failed", CHECK_BROKEN
        return outcome, _mask(message, secrets)

    def step(self) -> bool:
        """Claim one test, run it and record the result.

        Returns
        -------
        bool
            `True` when a test was claimed (whether or not its result could be recorded);
            `False` when none was queued.
        """
        with Session(self._engine) as session:
            test = self._service(session).claim_test(self._owner, now=db_now(session))
            if test is None:
                return False
            test_id, expires = test.id, test.lease_expires_at
            prepared = self._request(session, test)
            session.rollback()
        # No transaction is open from here until the result is recorded.
        if isinstance(prepared[0], str):
            outcome, message = str(prepared[0]), str(prepared[1])
        else:
            outcome, message = self._run_check(prepared[0], prepared[1])
        del prepared
        with Session(self._engine) as session:
            recorded = self._service(session).finish_test(
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
