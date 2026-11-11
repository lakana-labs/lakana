"""Repository checks: fetching a host's SSH keys and testing access, queued for a worker (Task 5).

The server only coordinates: `RepositoryProbeService` queues a probe and answers it, and the
worker claims and finishes it through `RepositoryProbeQueue`. Nothing here runs git or ssh.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.errors import (
    ConflictError,
    NotFoundError,
    NoWorkerError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    Principal,
    PrincipalType,
    ProbeOutcome,
    Repository,
    RepositoryKind,
    RepositoryProbe,
    RepositoryProbeKind,
    RepositoryProbeState,
    Worker,
)
from lakana_core.projects import probes
from lakana_core.projects.probes import (
    KEY_TYPES,
    MAX_HOST_KEYS,
    PROBE_KEEP,
    PROBE_UNCLAIMED_AFTER,
    PROBES_OPEN_PER_PERSON,
    HostKey,
    RepositoryProbeQueue,
    RepositoryProbeService,
    key_fingerprint,
    parse_keyscan,
    ssh_endpoint,
)

ED25519_B64 = "AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR"
ED25519_FINGERPRINT = "SHA256:BbeSiQQjnjpybq7nV8S8tEVnQiZNHfceXjKvrLVxecI"
ECDSA_B64 = (
    "AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBGneyeqF40EYKRgJdXWwLgx80Ym5+lBJRNQRtGuQ"
    "2HVu4+gksP0ttgiiYVCHhgrXOjBHj6bU1F8eEqf7JsWWueA="
)
ECDSA_FINGERPRINT = "SHA256:ZFFwnf2D9BVErjTfYgAPmeOgTTOT8PDphFmKnKcpGCs"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
LEASE = 60
URL = "git@git.example.com:team/analytics.git"
TOKEN = "ghp_secret-token-value"  # nosec B105 -- a test value


@dataclass
class Clock:
    """The database clock the probe service reads, settable."""

    now: datetime = NOW


@dataclass
class World:
    """Two environments, a repository, people with and without the right, and a live worker.

    Parameters
    ----------
    session
        Database session.
    clock
        The pinned database clock.
    admin
        `ADMIN` on the instance.
    other_admin
        A second `ADMIN`.
    analyst
        `ANALYST` on `prod` only.
    """

    session: Session
    clock: Clock
    admin: PrincipalRef
    other_admin: PrincipalRef
    analyst: PrincipalRef

    def service(self) -> RepositoryProbeService:
        """Build the service as the server does: real grants, denials audited.

        Returns
        -------
        RepositoryProbeService
            The service.
        """
        authz = AuditingAuthorization(
            GrantAuthorization(self.session), self.session, DenialAuditLimiter(10_000)
        )
        return RepositoryProbeService(self.session, authz, lease_seconds=LEASE)

    def queue(self) -> RepositoryProbeQueue:
        """Build the worker's side of the queue.

        Returns
        -------
        RepositoryProbeQueue
            The queue.
        """
        return RepositoryProbeQueue(self.session)

    def probes(self) -> list[RepositoryProbe]:
        """Read every probe afresh.

        Returns
        -------
        list of RepositoryProbe
            The rows, oldest first.
        """
        self.session.expire_all()
        return list(
            self.session.scalars(select(RepositoryProbe).order_by(RepositoryProbe.created_at))
        )

    def events(self, event_type: AuditEventType) -> list[AuditEvent]:
        """Read the audit events of one type.

        Parameters
        ----------
        event_type
            The type.

        Returns
        -------
        list of AuditEvent
            The events.
        """
        return list(
            self.session.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type))
        )


def _principal(session: Session, name: str) -> PrincipalRef:
    """Persist a person.

    Parameters
    ----------
    session
        Database session.
    name
        Their name.

    Returns
    -------
    PrincipalRef
        A reference to them.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
    session.commit()
    return ref


@pytest.fixture
def world(session: Session, monkeypatch: pytest.MonkeyPatch) -> World:
    """Build the world with a worker that takes pipeline work.

    Parameters
    ----------
    session
        Database session.
    monkeypatch
        pytest's patcher, to pin the database clock.

    Returns
    -------
    World
        The world.
    """
    clock = Clock()
    monkeypatch.setattr(probes, "db_now", lambda _session: clock.now)
    for name in ("dev", "prod"):
        session.add(Environment(id=uuid.uuid4(), name=name, storage_root=f"file:///srv/{name}"))
    session.add(
        Repository(
            id=uuid.uuid4(),
            name="analytics",
            kind=RepositoryKind.SSH,
            url=URL,
            credential_secret="analytics-deploy-key",
            ssh_known_hosts=f"git.example.com ssh-ed25519 {ED25519_B64}\n",
        )
    )
    session.add(
        Repository(id=uuid.uuid4(), name="local-one", kind=RepositoryKind.LOCAL, url="/repos/one")
    )
    session.add(
        Worker(id="w1", host="h", version="1", started_at=NOW, heartbeat_at=NOW, takes="pipeline")
    )
    session.commit()
    admin = _principal(session, "admin")
    other_admin = _principal(session, "olga")
    analyst = _principal(session, "alice")
    grants = GrantService(session, AllowAllAuthorization())
    for name in ("admin", "olga"):
        grants.grant(admin, principal=name, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    grants.grant(
        admin,
        principal="alice",
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="prod",
    )
    return World(session, clock, admin, other_admin, analyst)


# --- parsing ssh-keyscan's output -------------------------------------------------------------


def test_parse_keyscan_keeps_only_well_formed_keys_of_known_types() -> None:
    """A comment, a key type Lakana does not trust and a stray line are dropped."""
    keys = parse_keyscan(
        "# 10.0.0.5:22 SSH-2.0-x\n10.0.0.5 ssh-ed25519 "
        + ED25519_B64
        + "\n10.0.0.5 ssh-dss AAAA\nbogus\n",
        host="git.example.com",
        port=22,
    )
    assert [(k.type, k.line.split()[0]) for k in keys] == [("ssh-ed25519", "git.example.com")]
    assert keys[0].fingerprint == ED25519_FINGERPRINT
    assert keys[0].line == f"git.example.com ssh-ed25519 {ED25519_B64}"


def test_fingerprints_match_ssh_keygen() -> None:
    """The fingerprint is what `ssh-keygen -l` prints for the same key."""
    assert key_fingerprint(base64.b64decode(ECDSA_B64)) == ECDSA_FINGERPRINT
    keys = parse_keyscan(f"x ecdsa-sha2-nistp256 {ECDSA_B64}\n", host="h", port=22)
    assert keys == [
        HostKey(
            type="ecdsa-sha2-nistp256",
            fingerprint=ECDSA_FINGERPRINT,
            line=f"h ecdsa-sha2-nistp256 {ECDSA_B64}",
        )
    ]


def test_parse_keyscan_refuses_a_blob_of_another_type() -> None:
    """An ecdsa key announced as ed25519 is not a key of that type and is dropped."""
    assert parse_keyscan(f"x ssh-ed25519 {ECDSA_B64}\n", host="h", port=22) == []


@pytest.mark.parametrize(
    "line",
    [
        f"x ssh-ed25519 {ED25519_B64} comment",
        f"x ssh-ed25519 {ED25519_B64[:-1]}*",
        "x ssh-ed25519 not-base64!",
        f"ssh-ed25519 {ED25519_B64}",
    ],
)
def test_parse_keyscan_needs_exactly_three_fields_and_valid_base64(line: str) -> None:
    """Extra fields, a broken encoding or a missing host field drop the line.

    Parameters
    ----------
    line
        One line of output.
    """
    assert parse_keyscan(line + "\n", host="h", port=22) == []


def test_nonstandard_port_writes_the_bracketed_host() -> None:
    """A port other than 22 is written as ssh writes it into known_hosts."""
    keys = parse_keyscan(
        f"[10.0.0.5]:2222 ssh-ed25519 {ED25519_B64}\n", host="git.example.com", port=2222
    )
    assert keys[0].line == f"[git.example.com]:2222 ssh-ed25519 {ED25519_B64}"


def test_parse_keyscan_keeps_at_most_ten_keys_and_no_duplicates() -> None:
    """A host that answers with many keys yields at most `MAX_HOST_KEYS`, each once."""
    blobs = [
        base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + bytes([i]) * 36).decode()
        for i in range(15)
    ]
    output = "".join(f"x ssh-ed25519 {b}\nx ssh-ed25519 {b}\n" for b in blobs)
    keys = parse_keyscan(output, host="h", port=22)
    assert len(keys) == MAX_HOST_KEYS == 10
    assert len({k.line for k in keys}) == 10
    assert KEY_TYPES[0] == "ssh-ed25519"


# --- the SSH endpoint of a URL ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "endpoint"),
    [
        ("git@github.com:org/repo.git", ("github.com", 22)),
        ("ssh://git@Git.Example.com:2222/org/repo.git", ("git.example.com", 2222)),
        ("ssh://git@[2001:db8::1]/r", ("2001:db8::1", 22)),
    ],
)
def test_ssh_endpoint_reads_host_and_port(url: str, endpoint: tuple[str, int]) -> None:
    """scp-style and ssh:// URLs give a lower-case host and a port, 22 by default.

    Parameters
    ----------
    url
        The URL.
    endpoint
        Its host and port.
    """
    assert ssh_endpoint(url) == endpoint


UNSAFE_URLS = [
    "-oProxyCommand=x:r",
    "ssh://-h/r",
    "ssh://h/-r",
    "git@h:r\n",
    "ssh://a?@b/r",
    "ssh://h:0/r",
    "ssh://h:99999/r",
    "ssh://a,b/r",
    "ssh://*.x/r",
    "ssh://gït.example/r",
    "https://git.example.com/r",
]


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_unsafe_urls_never_reach_a_probe(world: World, url: str) -> None:
    """An unsafe or non-SSH URL is refused before any row is written.

    Parameters
    ----------
    world
        The world.
    url
        A URL to refuse.
    """
    with pytest.raises(ValidationError):
        world.service().request_host_keys(world.admin, url)
    assert world.probes() == []


# --- queueing ---------------------------------------------------------------------------------


def test_host_key_probe_is_queued_and_audited_without_credentials(world: World) -> None:
    """The probe is queued; the audit names kind, host and port and no secret."""
    probe = world.service().request_host_keys(world.admin, URL)
    assert probe.kind is RepositoryProbeKind.HOST_KEYS
    assert probe.state is RepositoryProbeState.QUEUED
    assert probe.url == URL
    assert probe.requested_by == world.admin.id
    (event,) = world.events(AuditEventType.REPOSITORY_PROBE_REQUESTED)
    assert event.principal_id == world.admin.id
    assert event.detail == {
        "kind": "host_keys",
        "host": "git.example.com",
        "port": 22,
        "repository": None,
        "environment": None,
        "probe_id": str(probe.id),
    }


def test_request_is_audited_without_credentials(world: World) -> None:
    """An access probe's audit names the repository and environment, never the secret's value."""
    probe = world.service().request_access(world.admin, "analytics", "prod")
    assert probe.kind is RepositoryProbeKind.ACCESS
    assert probe.repository_id is not None and probe.environment_id is not None
    (event,) = world.events(AuditEventType.REPOSITORY_PROBE_REQUESTED)
    assert event.detail == {
        "kind": "access",
        "host": "git.example.com",
        "port": 22,
        "repository": "analytics",
        "environment": "prod",
        "probe_id": str(probe.id),
    }
    assert TOKEN not in str(event.detail)
    assert "private_key" not in str(event.detail)


def test_access_probe_of_a_local_repository_is_refused(world: World) -> None:
    """Only ssh and https repositories are reached over the network."""
    with pytest.raises(ValidationError, match="ssh and https"):
        world.service().request_access(world.admin, "local-one", "prod")
    assert world.probes() == []


def test_access_probe_names_a_missing_repository_or_environment(world: World) -> None:
    """A wrong name is a 404 naming it."""
    with pytest.raises(NotFoundError, match="nope"):
        world.service().request_access(world.admin, "nope", "prod")
    with pytest.raises(NotFoundError, match="staging"):
        world.service().request_access(world.admin, "analytics", "staging")


def test_access_probe_needs_read_on_the_environment(world: World, session: Session) -> None:
    """MANAGE_REPOSITORY alone does not reach an environment's credential the caller cannot read."""
    grants = GrantService(session, AllowAllAuthorization())
    repo_admin = _principal(session, "rita")
    grants.grant(
        world.admin,
        principal="rita",
        action=Action.MANAGE_REPOSITORY,
        kind=SecurableKind.INSTANCE,
    )
    grants.grant(
        world.admin,
        principal="rita",
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    with pytest.raises(PermissionDenied, match="prod"):
        world.service().request_access(repo_admin, "analytics", "prod")
    assert (
        world.service().request_access(repo_admin, "analytics", "dev").kind
        is RepositoryProbeKind.ACCESS
    )
    assert len(world.probes()) == 1


def test_probe_refused_without_a_live_worker(world: World) -> None:
    """Without a worker that takes pipeline work nothing could run the probe; it says what to do."""
    world.session.query(Worker).delete()
    world.session.add(
        Worker(
            id="w2", host="h", version="1", started_at=NOW, heartbeat_at=NOW, takes="interactive"
        )
    )
    world.session.commit()
    with pytest.raises(NoWorkerError) as raised:
        world.service().request_host_keys(world.admin, URL)
    assert "docker compose up -d worker" in str(raised.value)
    assert "lakana server start --local" in str(raised.value)
    assert world.probes() == []


def test_a_worker_whose_heartbeat_is_old_does_not_count(world: World) -> None:
    """A worker that stopped beating a lease ago is not alive."""
    world.clock.now = NOW + timedelta(seconds=LEASE + 1)
    with pytest.raises(NoWorkerError):
        world.service().request_host_keys(world.admin, URL)


def test_sixth_open_probe_is_refused(world: World) -> None:
    """At most five unfinished probes per person; the sixth names the wait."""
    service = world.service()
    for _ in range(PROBES_OPEN_PER_PERSON):
        service.request_host_keys(world.admin, URL)
    with pytest.raises(ConflictError, match="5") as refused:
        service.request_host_keys(world.admin, URL)
    assert "at most three and a half minutes after it was asked" in str(refused.value)
    service.request_host_keys(world.other_admin, URL)


def test_a_probe_claimed_at_the_last_moment_ends_three_and_a_half_minutes_on(world: World) -> None:
    """The bound the 409 states: two minutes to be claimed plus the worker's 90 s lease (M2)."""
    probe = world.service().request_host_keys(world.admin, URL)
    last = NOW + PROBE_UNCLAIMED_AFTER - timedelta(seconds=1)
    assert world.queue().claim("w1", lease_seconds=90, now=last) is not None
    world.clock.now = last + timedelta(seconds=90)
    seen = world.service().get(world.admin, str(probe.id))
    assert seen.state is RepositoryProbeState.DONE
    assert world.clock.now - NOW <= timedelta(minutes=3, seconds=30)


def test_the_cap_is_counted_under_a_lock_on_the_requester(world: World) -> None:
    """Parallel requests of one person queue behind a row lock taken before the count (M2).

    SQLite has no row locks, so this pins the order on its statement log: the requester's
    principal row is selected `FOR UPDATE` (as Postgres compiles it), then the open probes are
    counted, then the probe is inserted, all in one transaction.
    """
    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(" ".join(statement.split()).upper())

    engine = world.session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        world.service().request_host_keys(world.admin, URL)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    lock = next(i for i, s in enumerate(statements) if s.startswith("SELECT PRINCIPALS.ID"))
    count = next(i for i, s in enumerate(statements) if "COUNT(" in s)
    insert = next(
        i for i, s in enumerate(statements) if s.startswith("INSERT INTO REPOSITORY_PROBES")
    )
    assert lock < count < insert
    assert not any(s.startswith(("COMMIT", "BEGIN")) for s in statements[lock:insert])
    compiled = str(probes.requester_lock(world.admin.id).compile(dialect=postgresql.dialect()))
    assert compiled.rstrip().endswith("FOR UPDATE")


def test_probes_need_manage_repository_and_the_denial_is_audited(world: World) -> None:
    """An analyst cannot start either probe, and `authorize` records the denial."""
    with pytest.raises(PermissionDenied):
        world.service().request_host_keys(world.analyst, URL)
    with pytest.raises(PermissionDenied):
        world.service().request_access(world.analyst, "analytics", "prod")
    denials = world.events(AuditEventType.AUTHORIZATION_DENIED)
    assert len(denials) == 2
    assert all(d.principal_id == world.analyst.id for d in denials)
    assert world.probes() == []


# --- reading a probe --------------------------------------------------------------------------


def test_probe_is_private_to_its_requester(world: World) -> None:
    """Another administrator gets a 404, as for an id that never existed."""
    probe = world.service().request_host_keys(world.admin, URL)
    assert world.service().get(world.admin, str(probe.id)).id == probe.id
    with pytest.raises(NotFoundError):
        world.service().get(world.other_admin, str(probe.id))
    with pytest.raises(NotFoundError):
        world.service().get(world.admin, "not-a-uuid")


def test_unclaimed_probe_ends_no_worker(world: World) -> None:
    """A probe still queued two minutes on is answered, and stored, as no_worker."""
    probe = world.service().request_host_keys(world.admin, URL)
    world.clock.now = NOW + PROBE_UNCLAIMED_AFTER - timedelta(seconds=1)
    assert world.service().get(world.admin, str(probe.id)).state is RepositoryProbeState.QUEUED
    world.clock.now = NOW + PROBE_UNCLAIMED_AFTER
    seen = world.service().get(world.admin, str(probe.id))
    assert seen.state is RepositoryProbeState.DONE
    assert seen.outcome is ProbeOutcome.NO_WORKER
    assert seen.message is not None and "worker" in seen.message
    (stored,) = world.probes()
    assert stored.outcome is ProbeOutcome.NO_WORKER


def test_a_probe_answered_no_worker_is_never_claimed(world: World) -> None:
    """Once answered no_worker a late worker cannot take it, nor one older than two minutes."""
    probe = world.service().request_host_keys(world.admin, URL)
    world.clock.now = NOW + PROBE_UNCLAIMED_AFTER
    world.service().get(world.admin, str(probe.id))
    assert world.queue().claim("w1", lease_seconds=90, now=world.clock.now) is None
    world.clock.now = NOW
    second = world.service().request_host_keys(world.admin, URL)
    late = NOW + PROBE_UNCLAIMED_AFTER + timedelta(seconds=1)
    assert world.queue().claim("w1", lease_seconds=90, now=late) is None
    assert world.probes()[1].id == second.id
    assert world.probes()[1].state is RepositoryProbeState.QUEUED


def test_a_running_probe_whose_lease_ran_out_ends_failed(world: World) -> None:
    """A worker that died mid-check leaves a probe that the next read finishes as failed."""
    probe = world.service().request_host_keys(world.admin, URL)
    assert world.queue().claim("w1", lease_seconds=90, now=NOW) is not None
    world.clock.now = NOW + timedelta(seconds=91)
    seen = world.service().get(world.admin, str(probe.id))
    assert seen.state is RepositoryProbeState.DONE
    assert seen.outcome is ProbeOutcome.FAILED
    assert world.queue().claim("w1", lease_seconds=90, now=world.clock.now) is None


def test_probes_older_than_a_day_are_deleted(world: World) -> None:
    """A new request deletes every probe older than `PROBE_KEEP`."""
    world.service().request_host_keys(world.admin, URL)
    world.clock.now = NOW + PROBE_KEEP + timedelta(seconds=1)
    world.session.query(Worker).update({Worker.heartbeat_at: world.clock.now})
    world.session.commit()
    fresh = world.service().request_host_keys(world.other_admin, URL)
    assert [p.id for p in world.probes()] == [fresh.id]


def test_old_probes_are_deleted_without_a_new_request(world: World) -> None:
    """Reading a probe and claiming one delete old probes too (M1)."""
    old = world.service().request_host_keys(world.admin, URL).id
    kept = world.service().request_host_keys(world.other_admin, URL).id
    world.session.execute(
        sa_update(RepositoryProbe)
        .where(RepositoryProbe.id == kept)
        .values(created_at=NOW + timedelta(hours=1))
    )
    world.session.commit()
    world.clock.now = NOW + PROBE_KEEP + timedelta(seconds=1)
    world.service().get(world.other_admin, str(kept))
    assert [p.id for p in world.probes()] == [kept]
    assert old != kept
    world.queue().claim("w1", lease_seconds=90, now=NOW + PROBE_KEEP + timedelta(hours=2))
    assert world.probes() == []


# --- the worker's side ------------------------------------------------------------------------


def test_claim_and_finish_record_the_keys(world: World) -> None:
    """The worker claims the oldest probe under a lease and records what it found."""
    probe = world.service().request_host_keys(world.admin, URL)
    claimed = world.queue().claim("w1", lease_seconds=90, now=NOW)
    assert claimed is not None and claimed.id == probe.id
    assert claimed.state is RepositoryProbeState.RUNNING
    assert claimed.lease_owner == "w1"
    assert world.queue().claim("w2", lease_seconds=90, now=NOW) is None
    key = HostKey(
        type="ssh-ed25519",
        fingerprint=ED25519_FINGERPRINT,
        line=f"git.example.com ssh-ed25519 {ED25519_B64}",
    )
    assert world.queue().finish(
        probe.id, "w1", outcome=ProbeOutcome.OK, message="found", host_keys=[key]
    )
    seen = world.service().get(world.admin, str(probe.id))
    assert seen.state is RepositoryProbeState.DONE
    assert seen.outcome is ProbeOutcome.OK
    assert seen.host_keys == [
        {"type": "ssh-ed25519", "fingerprint": ED25519_FINGERPRINT, "line": key.line}
    ]
    assert seen.finished_at is not None
    assert seen.lease_owner is None


def test_finish_records_nothing_without_the_lease(world: World) -> None:
    """Another worker, or the owner after its lease ran out, records nothing."""
    probe = world.service().request_host_keys(world.admin, URL)
    world.queue().claim("w1", lease_seconds=90, now=NOW)
    assert not world.queue().finish(
        probe.id, "w2", outcome=ProbeOutcome.OK, message="x", host_keys=None
    )
    world.clock.now = NOW + timedelta(seconds=91)
    assert not world.queue().finish(
        probe.id, "w1", outcome=ProbeOutcome.OK, message="x", host_keys=None
    )
    assert world.probes()[0].state is RepositoryProbeState.RUNNING


def test_finish_cuts_the_message(world: World) -> None:
    """A message is stored with at most 1,000 characters."""
    probe = world.service().request_host_keys(world.admin, URL)
    world.queue().claim("w1", lease_seconds=90, now=NOW)
    assert world.queue().finish(
        probe.id, "w1", outcome=ProbeOutcome.FAILED, message="x" * 5000, host_keys=None
    )
    assert len(world.probes()[0].message or "") == 1000


def test_parse_keyscan_of_no_output_is_no_keys() -> None:
    """A host that answers nothing yields no keys."""
    assert parse_keyscan("", host="h", port=22) == []


@pytest.mark.parametrize(
    "blob",
    [
        b"\x00\x00",
        b"\x00\x00\x00\x0bssh-ed",
        b"\x00\x00\x00\x0bssh-ed2551\xff",
    ],
    ids=["shorter-than-a-length", "type-cut-short", "type-not-ascii"],
)
def test_parse_keyscan_drops_a_blob_without_a_readable_type(blob: bytes) -> None:
    """A blob too short for its type string, or with a type that is not ASCII, is dropped.

    Parameters
    ----------
    blob
        The key blob.
    """
    line = f"x ssh-ed25519 {base64.b64encode(blob).decode()}\n"
    assert parse_keyscan(line, host="h", port=22) == []


def test_a_bracketed_host_that_is_not_ipv6_is_refused() -> None:
    """``[v1.fe]`` passes URL parsing as a future IP literal but is no IPv6 address."""
    with pytest.raises(ValidationError, match="must be an IPv6 address"):
        ssh_endpoint("ssh://git@[v1.fe]/r")


@pytest.mark.parametrize(
    ("url", "port"),
    [("https://Git.Example.com/team/r.git", 443), ("https://git.example.com:8443/r.git", 8443)],
)
def test_access_probe_of_an_https_repository_audits_its_host_and_port(
    world: World, url: str, port: int
) -> None:
    """An https repository is probed at its host, on 443 unless the URL names a port.

    Parameters
    ----------
    world
        The world.
    url
        The repository's URL.
    port
        The port the audit names.
    """
    world.session.add(
        Repository(
            id=uuid.uuid4(),
            name="web",
            kind=RepositoryKind.HTTPS,
            url=url,
            credential_secret="web-token",
        )
    )
    world.session.commit()

    probe = world.service().request_access(world.admin, "web", "prod")

    assert probe.kind is RepositoryProbeKind.ACCESS
    (event,) = world.events(AuditEventType.REPOSITORY_PROBE_REQUESTED)
    assert (event.detail["host"], event.detail["port"]) == ("git.example.com", port)


def test_a_claim_another_worker_won_is_rolled_back(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the guarded update changes no row, the claim returns `None` and changes nothing."""
    world.service().request_host_keys(world.admin, URL)
    monkeypatch.setattr(probes, "rowcount", lambda _result: 0)

    assert world.queue().claim("w1", lease_seconds=90, now=NOW) is None
    (probe,) = world.probes()
    assert probe.state is RepositoryProbeState.QUEUED
    assert probe.lease_owner is None
