"""The repository probe loop: fetch a host's SSH keys, or test access, for the server.

The server never connects to a git host. `lakana_core.projects.probes.RepositoryProbeService`
queues a probe; this loop, on a daemon thread of every worker that takes pipeline work, claims
one at a time under a lease (`RepositoryProbeQueue.claim`), runs it with no transaction open and
records what it found while the lease is still this worker's (`RepositoryProbeQueue.finish`; a
worker that lost it records nothing and logs at info level).

- A host-key probe runs `scan_host_keys`: the URL's host is resolved once, any address that
  `is_never_reachable` refuses the probe, and `ssh-keyscan` reaches the checked address, after
  ``--``, with a minimal environment and no shell; only well-formed keys of known types are kept
  (`parse_keyscan`), and the message is a fixed sentence, never the tool's output.
- An access probe runs `lakana_worker.git.check_access` with the environment's credential
  (`resolve_credential`) and the repository row's own known hosts, with a deadline 15 s inside
  the lease, so no second worker can take a probe that is still running.

Neither a credential nor an exception's text reaches a log line: a failure is logged by its type.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import socket
import subprocess  # nosec B404 -- runs ssh-keyscan with a fixed argument list, no shell
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from lakana_core.db import db_now
from lakana_core.errors import NotFoundError, ValidationError
from lakana_core.models import (
    Environment,
    ProbeOutcome,
    Repository,
    RepositoryProbe,
    RepositoryProbeKind,
)
from lakana_core.net.egress import is_never_reachable
from lakana_core.projects.probes import HostKey, RepositoryProbeQueue, parse_keyscan, ssh_endpoint
from lakana_core.secrets.keys import KeyProvider
from lakana_core.secrets.uses import SecretUseRefused
from lakana_worker.deploy import DeploySettings, resolve_credential
from lakana_worker.git import REFUSED_ADDRESS_MESSAGE, check_access

logger = logging.getLogger(__name__)

IDLE_SECONDS = 2.0
"""How long the loop sleeps when no probe is queued."""

LEASE_SECONDS = 90
"""How long a claimed probe belongs to this worker."""

ACCESS_DEADLINE_SECONDS = LEASE_SECONDS - 15
"""The time an access check may take: 15 s inside the lease, to record the result in."""

KEYSCAN_TIMEOUT_SECONDS = 25
"""The time `ssh-keyscan` may take in all (three key types, ``-T 5`` each)."""

MAX_IDLE_SECONDS = 60.0
"""The longest sleep after consecutive failing steps."""

FAILURE_LOG_SECONDS = 60.0
"""Least time between two log lines about a failing step."""

SCAN_MESSAGES = {
    ProbeOutcome.REFUSED_ADDRESS: REFUSED_ADDRESS_MESSAGE,
    ProbeOutcome.UNREACHABLE: (
        "The worker got no SSH host key from that host and port. Check the URL's host and port, "
        "and that the host accepts SSH connections from the worker's network."
    ),
    ProbeOutcome.TIMEOUT: (
        f"The host did not answer within {KEYSCAN_TIMEOUT_SECONDS} seconds. Check the URL's host "
        "and port and the network path from the worker to it."
    ),
    ProbeOutcome.FAILED: (
        "The worker could not run ssh-keyscan; it is part of the OpenSSH client the worker image "
        "ships. The worker's log has the details."
    ),
}
"""The fixed sentence of each outcome of a host-key scan but ``ok`` (M3: never the tool's text)."""

NOT_RUN = "The check could not run; the worker's log has the details."
"""The message of a probe whose preparation or check raised."""

GONE = "The repository or the environment was deleted before the check ran."
"""The message of an access probe whose repository or environment no longer exists."""


def _found(count: int) -> str:
    """Write the message of a scan that found keys.

    Parameters
    ----------
    count
        How many keys.

    Returns
    -------
    str
        The sentence, with the next step.
    """
    keys = "1 key" if count == 1 else f"{count} keys"
    return (
        f"The host offered {keys}. Compare each fingerprint with your git host's published "
        "fingerprints before you trust it."
    )


def scan_host_keys(
    host: str,
    port: int,
    *,
    resolve: Callable[..., Sequence[Any]] = socket.getaddrinfo,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: int = KEYSCAN_TIMEOUT_SECONDS,
) -> tuple[ProbeOutcome, str, list[HostKey]]:
    """Fetch the SSH host keys a host offers.

    Parameters
    ----------
    host
        The host, as `ssh_endpoint` returns it.
    port
        Its port.
    resolve
        `socket.getaddrinfo` or a stand-in; the name is resolved once.
    run
        `subprocess.run` or a stand-in.
    timeout
        Seconds `ssh-keyscan` may take in all.

    Returns
    -------
    tuple
        The outcome, a fixed sentence and the keys (empty unless ``ok``). ``refused_address``
        when any address `is_never_reachable` (ssh-keyscan never runs); ``unreachable`` for a
        name that does not resolve or a host that offered no key; ``timeout``; ``failed`` when
        ssh-keyscan cannot run.
    """
    try:
        answers = resolve(host, port, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(str(a[4][0]).split("%", 1)[0]) for a in answers]
    except (OSError, UnicodeError, ValueError):
        return ProbeOutcome.UNREACHABLE, SCAN_MESSAGES[ProbeOutcome.UNREACHABLE], []
    if not addresses:
        return ProbeOutcome.UNREACHABLE, SCAN_MESSAGES[ProbeOutcome.UNREACHABLE], []
    if any(is_never_reachable(address) for address in addresses):
        return ProbeOutcome.REFUSED_ADDRESS, SCAN_MESSAGES[ProbeOutcome.REFUSED_ADDRESS], []
    argv = ["ssh-keyscan", "-T", "5", "-p", str(port), "-t", "ed25519,ecdsa,rsa"]
    argv += ["--", str(addresses[0])]
    try:
        done = run(  # nosec B603 -- fixed argv, the checked address after `--`, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeOutcome.TIMEOUT, SCAN_MESSAGES[ProbeOutcome.TIMEOUT], []
    except OSError as exc:
        logger.warning("ssh-keyscan could not run (%s).", type(exc).__name__)
        return ProbeOutcome.FAILED, SCAN_MESSAGES[ProbeOutcome.FAILED], []
    keys = parse_keyscan(done.stdout or "", host=host, port=port)
    if not keys:
        return ProbeOutcome.UNREACHABLE, SCAN_MESSAGES[ProbeOutcome.UNREACHABLE], []
    return ProbeOutcome.OK, _found(len(keys)), keys


class RepositoryProbeLoop:
    """Run queued repository probes on a daemon thread until it is stopped.

    Parameters
    ----------
    engine
        The metadata database.
    owner
        This worker's id; it names the leases.
    key_provider
        Unwraps an access check's credential.
    settings
        The worker's deploy settings: the repository roots, limits and the operator's git
        network settings (`LAKANA_GIT__*`) an access check uses like a deploy.
    stop
        The event that ends the loop; a new one when `None`.
    idle_seconds
        The sleep when no probe was queued, and after a failing step.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        owner: str,
        key_provider: KeyProvider,
        settings: DeploySettings,
        stop: threading.Event | None = None,
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        self._engine = engine
        self._owner = owner
        self._keys = key_provider
        self._settings = settings
        self._stop = stop if stop is not None else threading.Event()
        self._idle = idle_seconds

    def _access(self, session: Session, probe: RepositoryProbe) -> tuple[ProbeOutcome, str]:
        """Test access for a claimed probe; the credential lives only inside this call.

        Parameters
        ----------
        session
            The session; it is rolled back before git runs, so no transaction stays open.
        probe
            The claimed access probe.

        Returns
        -------
        tuple
            The outcome and message.
        """
        repo = session.get(Repository, probe.repository_id) if probe.repository_id else None
        env = session.get(Environment, probe.environment_id) if probe.environment_id else None
        if repo is None or env is None:
            return ProbeOutcome.FAILED, GONE
        try:
            credential = resolve_credential(session, self._keys, env, repo)
        except (NotFoundError, SecretUseRefused, ValidationError) as exc:
            # Lakana's own sentences: they name the secret and the next step, never a value.
            return ProbeOutcome.FAILED, str(exc)
        kind, url, known_hosts = repo.kind, repo.url, repo.ssh_known_hosts
        session.rollback()
        workdir = Path(tempfile.mkdtemp(prefix="lakana-probe-"))
        try:
            settings = self._settings
            return check_access(
                kind=kind,
                url=url,
                credential=credential,
                known_hosts=known_hosts,
                workdir=workdir,
                limits=settings.limits,
                repo_roots=settings.repo_roots,
                deadline=time.monotonic() + ACCESS_DEADLINE_SECONDS,
                ca_bundle=settings.ca_bundle,
                https_proxy=settings.https_proxy,
                no_proxy=settings.no_proxy,
            )
        finally:
            del credential
            shutil.rmtree(workdir, ignore_errors=True)

    def _run_probe(
        self, session: Session, probe: RepositoryProbe
    ) -> tuple[ProbeOutcome, str, list[HostKey] | None]:
        """Run one claimed probe.

        Parameters
        ----------
        session
            The session the probe was claimed in.
        probe
            The probe.

        Returns
        -------
        tuple
            The outcome, message and, for a host-key probe, the keys.
        """
        if probe.kind is RepositoryProbeKind.HOST_KEYS:
            url = probe.url
            session.rollback()
            try:
                host, port = ssh_endpoint(url)
            except ValidationError as exc:
                return ProbeOutcome.FAILED, f"The URL is refused: {exc}", []
            return scan_host_keys(host, port)
        outcome, message = self._access(session, probe)
        return outcome, message, None

    def step(self) -> bool:
        """Claim one probe, run it and record the result.

        Returns
        -------
        bool
            `True` when a probe was claimed (whether or not its result could be recorded);
            `False` when none was queued.
        """
        with Session(self._engine) as session:
            probe = RepositoryProbeQueue(session).claim(
                self._owner, lease_seconds=LEASE_SECONDS, now=db_now(session)
            )
            if probe is None:
                return False
            probe_id: uuid.UUID = probe.id
            try:
                outcome, message, keys = self._run_probe(session, probe)
            except Exception as exc:
                # The text of a decryption, database or subprocess error could quote a secret.
                logger.warning("A repository check failed to run (%s).", type(exc).__name__)
                outcome, message, keys = ProbeOutcome.FAILED, NOT_RUN, None
            session.rollback()
        with Session(self._engine) as session:
            if outcome is not ProbeOutcome.OK:
                keys = None
            recorded = RepositoryProbeQueue(session).finish(
                probe_id, self._owner, outcome=outcome, message=message, host_keys=keys
            )
        if not recorded:
            logger.info("A repository check's lease was lost while it ran; nothing recorded.")
        return True

    def run(self) -> None:
        """Run probes until `stop`.

        A failing step (the database is down, say) is logged by its exception's type only, at
        most once a minute, and the sleep doubles up to a minute while it keeps failing.
        """
        failures = 0
        last_logged = -FAILURE_LOG_SECONDS
        while not self._stop.is_set():
            try:
                worked = self.step()
                failures = 0
            except Exception as exc:
                worked = False
                failures += 1
                if time.monotonic() - last_logged >= FAILURE_LOG_SECONDS:
                    last_logged = time.monotonic()
                    logger.warning("A repository check step failed (%s).", type(exc).__name__)
            if failures or not worked:
                self._stop.wait(min(self._idle * 2 ** min(failures, 10), MAX_IDLE_SECONDS))

    def start(self) -> threading.Thread:
        """Run `run` in a daemon thread.

        Returns
        -------
        threading.Thread
            The started thread, for the caller to join after `stop`.
        """
        thread = threading.Thread(target=self.run, name="lakana-repository-probes", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Ask the thread to end after the probe it is running."""
        self._stop.set()
