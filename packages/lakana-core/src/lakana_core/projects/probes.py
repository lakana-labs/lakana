"""Repository checks: a host's SSH keys and "Test access", run by a worker, never the server.

`RepositoryProbeService` is the person's side: it queues a probe (`MANAGE_REPOSITORY`) and
answers it to its requester only. `RepositoryProbeQueue` is the worker's: it claims a probe
under a lease and records what it found. The server makes no outbound connection for either;
git and ssh run in `lakana_worker` (`docs/agent/security.md`, "Repository checks").

The pure helpers are shared by both sides: `ssh_endpoint` reads the host and port of an SSH URL
(the worker scans exactly that), `parse_keyscan` keeps only well-formed keys of known types from
`ssh-keyscan`'s output, and `key_fingerprint` prints a key as `ssh-keygen -l` does.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.orm import Session

from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.db import db_now, rowcount
from lakana_core.errors import (
    ConflictError,
    NotFoundError,
    NoWorkerError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.identifiers import shown
from lakana_core.models import (
    AuditEventType,
    Principal,
    ProbeOutcome,
    RepositoryKind,
    RepositoryProbe,
    RepositoryProbeKind,
    RepositoryProbeState,
)
from lakana_core.projects.git_input import validate_repo_url
from lakana_core.projects.lookups import environment_by_name, repository_by_name
from lakana_core.runs.workers import alive_takes

KEY_TYPES = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)
"""The host key types a probe reports, strongest first; any other line is dropped."""

MAX_HOST_KEYS = 10
"""The most keys one host-key probe keeps."""

PROBE_UNCLAIMED_AFTER = timedelta(minutes=2)
"""A probe no worker claimed this long after it was requested is answered as ``no_worker``,
and is never claimed after that."""

PROBE_KEEP = timedelta(hours=24)
"""How long a probe is kept; older ones are deleted on the next request, read or claim."""

PROBES_OPEN_PER_PERSON = 5
"""The most probes one person may have queued or running at once."""

PURGE_BATCH = 500
"""The most old probes one request, read or claim deletes."""

NO_WORKER = (
    "No worker is running, so nothing can check the repository. Start one (`docker compose up -d "
    "worker`, or `lakana server start --local`) and try again."
)
"""The refusal of a request while no worker that takes pipeline work is alive."""

NOT_CLAIMED = (
    "No worker took the check within two minutes. Start a worker that takes pipeline work "
    "(`docker compose up -d worker`, or `lakana server start --local`) and try again."
)
"""The message of a probe answered as ``no_worker``."""

WORKER_STOPPED = (
    "The worker that ran the check stopped before it finished. Try again; if it happens again, "
    "the worker's log has the details."
)
"""The message of a running probe whose lease ran out."""

_TAKES_PIPELINE = frozenset({"all", "pipeline"})
"""The `takes` of a worker that runs repository checks."""

_PLAIN_HOST = re.compile(r"[a-z0-9.-]+")
"""An SSH host as a probe accepts it: ASCII letters, digits, dots and hyphens (lower-cased)."""


@dataclass(frozen=True)
class HostKey:
    """One SSH host key a probe found.

    Parameters
    ----------
    type
        The key type, one of `KEY_TYPES`.
    fingerprint
        ``SHA256:`` and the unpadded base64 of the key blob's SHA-256, as `ssh-keygen -l`
        prints it.
    line
        The known_hosts line: ``<host> <type> <base64>``, the host as ``[host]:port`` for a port
        other than 22.
    """

    type: str
    fingerprint: str
    line: str


def key_fingerprint(blob: bytes) -> str:
    """Print a key's fingerprint as `ssh-keygen -l` does.

    Parameters
    ----------
    blob
        The key blob (the base64-decoded second field of a public key line).

    Returns
    -------
    str
        ``SHA256:`` and the unpadded base64 of the blob's SHA-256.
    """
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def _blob_type(blob: bytes) -> str | None:
    """Read the key type a key blob starts with.

    Parameters
    ----------
    blob
        The key blob.

    Returns
    -------
    str or None
        The type string (an SSH string: a four-byte length and the bytes); `None` when the
        blob is too short for it or it is not ASCII.
    """
    if len(blob) < 4:
        return None
    length = int.from_bytes(blob[:4], "big")
    name = blob[4 : 4 + length]
    if len(name) != length:
        return None
    try:
        return name.decode("ascii")
    except UnicodeDecodeError:
        return None


def known_hosts_host(host: str, port: int) -> str:
    """Write a host as ssh writes it into known_hosts.

    Parameters
    ----------
    host
        The host, as `ssh_endpoint` returns it.
    port
        The port.

    Returns
    -------
    str
        `host` for port 22; ``[host]:port`` otherwise.
    """
    return host if port == 22 else f"[{host}]:{port}"


def parse_keyscan(output: str, *, host: str, port: int) -> list[HostKey]:
    """Keep the well-formed keys of known types from `ssh-keyscan`'s output.

    Parameters
    ----------
    output
        What `ssh-keyscan` printed on standard output.
    host
        The host that was asked for (the scan connects to its address, which the output names);
        it replaces the host field of every line.
    port
        The port that was scanned.

    Returns
    -------
    list of HostKey
        In the order found, each key once, at most `MAX_HOST_KEYS`. A line is kept only with
        exactly three fields, a type in `KEY_TYPES`, strictly valid base64 and a blob that
        starts with that same type; comments and everything else are dropped.
    """
    keys: list[HostKey] = []
    seen: set[str] = set()
    for raw in output.splitlines():
        fields = raw.split()
        if len(fields) != 3 or fields[1] not in KEY_TYPES:
            continue
        _host, key_type, encoded = fields
        try:
            blob = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if _blob_type(blob) != key_type or encoded in seen:
            continue
        seen.add(encoded)
        line = f"{known_hosts_host(host, port)} {key_type} {encoded}"
        keys.append(HostKey(type=key_type, fingerprint=key_fingerprint(blob), line=line))
        if len(keys) == MAX_HOST_KEYS:
            break
    return keys


def ssh_endpoint(url: str) -> tuple[str, int]:
    """Read the host and port an SSH repository URL connects to.

    Parameters
    ----------
    url
        ``ssh://[user@]host[:port]/path`` or scp-style ``[user@]host:path``.

    Returns
    -------
    tuple of (str, int)
        The lower-cased host (an IPv6 address without its brackets) and the port, 22 by
        default.

    Raises
    ------
    ValidationError
        If `validate_repo_url` refuses the URL for ``ssh``, the port is not 1-65535, or the host
        is neither plain ASCII letters, digits, dots and hyphens nor an IPv6 address in brackets
        (a comma or a wildcard would make the known_hosts line name other hosts).
    """
    validate_repo_url(RepositoryKind.SSH, url)
    if url.startswith("ssh://"):
        parts = urlsplit(url)
        port = parts.port if parts.port is not None else 22
        host = parts.hostname or ""
        bracketed = "[" in parts.netloc
    else:
        port = 22
        # `validate_repo_url` matched `[user@]host:path`; neither user nor host holds a colon.
        host = url.split(":", 1)[0].rsplit("@", 1)[-1].lower()
        bracketed = False
    if not 1 <= port <= 65535:
        raise ValidationError("The SSH URL's port must be between 1 and 65535.")
    if bracketed:
        try:
            return str(ipaddress.IPv6Address(host)), port
        except ValueError as exc:
            raise ValidationError(
                "The SSH URL's host in brackets must be an IPv6 address."
            ) from exc
    if not _PLAIN_HOST.fullmatch(host):
        raise ValidationError(
            "The SSH URL's host must be a plain host name: ASCII letters, digits, dots and "
            "hyphens (write an IPv6 address in brackets)."
        )
    return host, port


def _https_endpoint(url: str) -> tuple[str, int]:
    """Read the host and port of an HTTPS repository URL, for the audit record.

    Parameters
    ----------
    url
        A validated ``https://`` URL.

    Returns
    -------
    tuple of (str, int)
        The lower-cased host and the port, 443 by default.
    """
    parts = urlsplit(url)
    return parts.hostname or "", parts.port or 443


def requester_lock(principal_id: uuid.UUID) -> Select[uuid.UUID]:
    """Build the lock one person's probe requests queue behind.

    `_queue` executes it before it counts the person's open probes, so two parallel requests
    of one person count and insert one after the other and the cap holds (`FOR UPDATE` on the
    principal's row; SQLite, which has no row locks, serialises writers anyway).

    Parameters
    ----------
    principal_id
        The requester.

    Returns
    -------
    Select
        ``SELECT principals.id … FOR UPDATE``.
    """
    return select(Principal.id).where(Principal.id == principal_id).with_for_update()


def _purge(session: Session, now: datetime) -> None:
    """Delete up to `PURGE_BATCH` probes older than `PROBE_KEEP`; the caller commits.

    Parameters
    ----------
    session
        Database session.
    now
        The database's time.
    """
    old = (
        select(RepositoryProbe.id)
        .where(RepositoryProbe.created_at < now - PROBE_KEEP)
        .limit(PURGE_BATCH)
        .scalar_subquery()
    )
    session.execute(delete(RepositoryProbe).where(RepositoryProbe.id.in_(old)))


class RepositoryProbeService:
    """A person's repository checks: queue one, read one's answer.

    Parameters
    ----------
    session
        Database session; the methods commit.
    authz
        Authorization service.
    lease_seconds
        The worker heartbeat lease (`LAKANA_RUN__LEASE_SECONDS`): a worker is alive while its
        heartbeat is younger.
    """

    def __init__(
        self, session: Session, authz: AuthorizationService, *, lease_seconds: int
    ) -> None:
        self._session = session
        self._authz = authz
        self._lease_seconds = lease_seconds

    def request_host_keys(self, p: PrincipalRef, url: str) -> RepositoryProbe:
        """Queue a fetch of the SSH host keys of a repository URL's host.

        Parameters
        ----------
        p
            The acting principal.
        url
            An SSH repository URL; its host and port are scanned.

        Returns
        -------
        RepositoryProbe
            The queued probe.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_REPOSITORY` (audited).
        ValidationError
            If the URL is not a safe SSH URL (`ssh_endpoint`).
        NoWorkerError
            If no worker that takes pipeline work is alive.
        ConflictError
            If the caller already has `PROBES_OPEN_PER_PERSON` probes queued or running.
        """
        self._authz.authorize(p, Action.MANAGE_REPOSITORY, Securable.instance_scope())
        host, port = ssh_endpoint(url)
        return self._queue(
            p,
            kind=RepositoryProbeKind.HOST_KEYS,
            url=url,
            host=host,
            port=port,
            repository=None,
            environment=None,
        )

    def request_access(self, p: PrincipalRef, repository: str, environment: str) -> RepositoryProbe:
        """Queue a check that a worker can read a repository with an environment's credential.

        Parameters
        ----------
        p
            The acting principal.
        repository
            The repository's name.
        environment
            The environment whose secret of the repository's credential is used.

        Returns
        -------
        RepositoryProbe
            The queued probe.

        Raises
        ------
        PermissionDenied
            Without `MANAGE_REPOSITORY` (audited), or without `READ_ENVIRONMENT` on the
            environment.
        NotFoundError
            If the repository or the environment does not exist.
        ValidationError
            For a ``local`` repository, or an SSH URL `ssh_endpoint` refuses.
        NoWorkerError
            If no worker that takes pipeline work is alive.
        ConflictError
            If the caller already has `PROBES_OPEN_PER_PERSON` probes queued or running.
        """
        self._authz.authorize(p, Action.MANAGE_REPOSITORY, Securable.instance_scope())
        if not self._authz.permits(
            p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment)
        ):
            raise PermissionDenied(
                f"You cannot read environment '{shown(environment)}', so you cannot test a "
                "repository with its credential. Ask an administrator for access to it, or test "
                "in an environment you can read."
            )
        repo = repository_by_name(self._session, repository)
        env = environment_by_name(self._session, environment)
        if repo.kind is RepositoryKind.SSH:
            host, port = ssh_endpoint(repo.url)
        elif repo.kind is RepositoryKind.HTTPS:
            host, port = _https_endpoint(validate_repo_url(repo.kind, repo.url))
        else:
            raise ValidationError(
                f"Repository '{shown(repo.name)}' is local; only ssh and https repositories are "
                "reached over the network and can be tested. A local repository is read from the "
                "worker's disk when you deploy."
            )
        return self._queue(
            p,
            kind=RepositoryProbeKind.ACCESS,
            url=repo.url,
            host=host,
            port=port,
            repository=(repo.id, repo.name),
            environment=(env.id, env.name),
        )

    def _queue(
        self,
        p: PrincipalRef,
        *,
        kind: RepositoryProbeKind,
        url: str,
        host: str,
        port: int,
        repository: tuple[uuid.UUID, str] | None,
        environment: tuple[uuid.UUID, str] | None,
    ) -> RepositoryProbe:
        """Insert and audit a probe once a worker could run it and the caller has room.

        Parameters
        ----------
        p
            The acting principal, already authorized.
        kind
            The probe's kind.
        url
            The URL checked.
        host
            Its host, for the audit record.
        port
            Its port, for the audit record.
        repository
            The repository's id and name, for an access check.
        environment
            The environment's id and name, for an access check.

        Returns
        -------
        RepositoryProbe
            The queued probe, committed.

        Raises
        ------
        NoWorkerError
            If no worker that takes pipeline work is alive.
        ConflictError
            If the caller already has `PROBES_OPEN_PER_PERSON` probes queued or running.
        """
        now = db_now(self._session)
        _purge(self._session, now)
        takes = alive_takes(self._session, now, self._lease_seconds)
        if not _TAKES_PIPELINE & set(takes.values()):
            self._session.rollback()
            raise NoWorkerError(NO_WORKER)
        self._session.execute(requester_lock(p.id))
        self._settle(now, requested_by=p.id)
        open_count = self._session.scalar(
            select(func.count())
            .select_from(RepositoryProbe)
            .where(
                RepositoryProbe.requested_by == p.id,
                RepositoryProbe.state.in_(
                    (RepositoryProbeState.QUEUED, RepositoryProbeState.RUNNING)
                ),
            )
        )
        if (open_count or 0) >= PROBES_OPEN_PER_PERSON:
            self._session.rollback()
            raise ConflictError(
                f"You already have {PROBES_OPEN_PER_PERSON} repository checks waiting or running. "
                "Wait for one to finish (at most two minutes) and try again."
            )
        probe = RepositoryProbe(
            id=uuid.uuid4(),
            kind=kind,
            url=url,
            repository_id=repository[0] if repository else None,
            environment_id=environment[0] if environment else None,
            requested_by=p.id,
            state=RepositoryProbeState.QUEUED,
            created_at=now,
        )
        self._session.add(probe)
        AuditService(self._session).record(
            p.id,
            AuditEventType.REPOSITORY_PROBE_REQUESTED,
            securable=Securable.instance_scope()
            if environment is None
            else Securable.environment_scope(environment[1]),
            detail={
                "kind": kind.value,
                "host": host,
                "port": port,
                "repository": repository[1] if repository else None,
                "environment": environment[1] if environment else None,
                "probe_id": str(probe.id),
            },
        )
        self._session.commit()
        return probe

    def _settle(
        self,
        now: datetime,
        *,
        requested_by: uuid.UUID | None = None,
        probe_id: uuid.UUID | None = None,
    ) -> None:
        """Finish the probes that can no longer finish on their own; the caller commits.

        A probe still queued `PROBE_UNCLAIMED_AFTER` after it was requested becomes
        ``no_worker``; a running one whose lease ran out becomes ``failed``. Each is one
        ``UPDATE … WHERE state = …``, so a worker's claim or finish at the same moment wins or
        loses whole.

        Parameters
        ----------
        now
            The database's time.
        requested_by
            Only this person's probes.
        probe_id
            Only this probe.
        """
        scope = []
        if requested_by is not None:
            scope.append(RepositoryProbe.requested_by == requested_by)
        if probe_id is not None:
            scope.append(RepositoryProbe.id == probe_id)
        done = {
            "state": RepositoryProbeState.DONE,
            "finished_at": now,
            "lease_owner": None,
            "lease_expires_at": None,
        }
        self._session.execute(
            update(RepositoryProbe)
            .where(
                *scope,
                RepositoryProbe.state == RepositoryProbeState.QUEUED,
                RepositoryProbe.created_at <= now - PROBE_UNCLAIMED_AFTER,
            )
            .values(**done, outcome=ProbeOutcome.NO_WORKER, message=NOT_CLAIMED)
            .execution_options(synchronize_session=False)
        )
        self._session.execute(
            update(RepositoryProbe)
            .where(
                *scope,
                RepositoryProbe.state == RepositoryProbeState.RUNNING,
                RepositoryProbe.lease_expires_at <= now,
            )
            .values(**done, outcome=ProbeOutcome.FAILED, message=WORKER_STOPPED)
            .execution_options(synchronize_session=False)
        )

    def get(self, p: PrincipalRef, probe_id: str) -> RepositoryProbe:
        """Show one of the caller's probes.

        Parameters
        ----------
        p
            The acting principal.
        probe_id
            The probe's id.

        Returns
        -------
        RepositoryProbe
            The probe; one still queued two minutes on is answered (and stored) as
            ``no_worker``, one whose worker's lease ran out as ``failed``.

        Raises
        ------
        NotFoundError
            If the caller has no probe of that id (another person's is not shown either).
        """
        try:
            wanted: uuid.UUID | None = uuid.UUID(probe_id)
        except (ValueError, AttributeError, TypeError):
            wanted = None
        now = db_now(self._session)
        _purge(self._session, now)
        if wanted is not None:
            self._settle(now, probe_id=wanted)
        self._session.commit()
        probe = (
            self._session.execute(
                select(RepositoryProbe).where(
                    RepositoryProbe.id == wanted, RepositoryProbe.requested_by == p.id
                )
            ).scalar_one_or_none()
            if wanted is not None
            else None
        )
        if probe is None:
            raise NotFoundError(
                f"You have no repository check '{shown(str(probe_id))}'; checks are kept for 24 "
                "hours. Start a new one with `lakana repo host-keys URL` or "
                "`lakana repo test NAME --env ENV`."
            )
        return probe


class RepositoryProbeQueue:
    """The worker's side of the repository probe queue: claim and finish.

    These are the worker's own queue operations, not acts of a person, so they take no
    authorization object.

    Parameters
    ----------
    session
        Database session; `claim` and `finish` commit.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def claim(
        self, owner: str, *, lease_seconds: int = 60, now: datetime
    ) -> RepositoryProbe | None:
        """Claim the oldest queued probe younger than `PROBE_UNCLAIMED_AFTER`, under a lease.

        A probe is claimed once: one whose lease ran out is never taken again (each run of an
        access check uses the credential), and one answered ``no_worker`` is never taken. The
        claim is an ``UPDATE … WHERE state = 'queued'`` checked by its row count. Old probes
        are deleted first (`PROBE_KEEP`).

        Parameters
        ----------
        owner
            The claiming worker's id.
        lease_seconds
            How long the claim lasts.
        now
            The database's time.

        Returns
        -------
        RepositoryProbe or None
            The probe, now running; `None` when there is none to claim.
        """
        _purge(self._session, now)
        candidate = self._session.scalars(
            select(RepositoryProbe.id)
            .where(
                RepositoryProbe.state == RepositoryProbeState.QUEUED,
                RepositoryProbe.created_at > now - PROBE_UNCLAIMED_AFTER,
            )
            .order_by(RepositoryProbe.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if candidate is None:
            self._session.commit()
            return None
        claimed = self._session.execute(
            update(RepositoryProbe)
            .where(
                RepositoryProbe.id == candidate,
                RepositoryProbe.state == RepositoryProbeState.QUEUED,
            )
            .values(
                state=RepositoryProbeState.RUNNING,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            .execution_options(synchronize_session=False)
        )
        if rowcount(claimed) != 1:
            self._session.rollback()
            return None
        self._session.commit()
        return self._session.get(RepositoryProbe, candidate, populate_existing=True)

    def finish(
        self,
        probe_id: uuid.UUID,
        owner: str,
        *,
        outcome: ProbeOutcome,
        message: str,
        host_keys: list[HostKey] | None,
    ) -> bool:
        """Record what a probe found, if the caller's lease on it still holds.

        Parameters
        ----------
        probe_id
            The probe's id.
        owner
            The worker that claimed it.
        outcome
            What it found.
        message
            Lakana's own sentence for it; cut to 1,000 characters.
        host_keys
            The keys of a host-key probe; `None` otherwise.

        Returns
        -------
        bool
            `True` when it was recorded; `False` when the probe is no longer running under this
            worker's unexpired lease (nothing is changed then).
        """
        now = db_now(self._session)
        recorded = self._session.execute(
            update(RepositoryProbe)
            .where(
                RepositoryProbe.id == probe_id,
                RepositoryProbe.state == RepositoryProbeState.RUNNING,
                RepositoryProbe.lease_owner == owner,
                RepositoryProbe.lease_expires_at > now,
            )
            .values(
                state=RepositoryProbeState.DONE,
                outcome=outcome,
                message=message[:1000],
                host_keys=None if host_keys is None else [asdict(key) for key in host_keys],
                finished_at=now,
                lease_owner=None,
                lease_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if rowcount(recorded) != 1:
            self._session.rollback()
            return False
        self._session.commit()
        return True
