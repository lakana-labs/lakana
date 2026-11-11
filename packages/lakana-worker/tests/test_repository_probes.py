"""The repository probe loop: a worker fetches host keys and tests access, never the server."""

from __future__ import annotations

import base64
import logging
import socket
import subprocess
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

import lakana_worker.repository_probes as module
from lakana_core.auth import PrincipalRef
from lakana_core.config import DatabaseConfig
from lakana_core.db import create_engine, db_now
from lakana_core.models import (
    Base,
    Environment,
    Principal,
    PrincipalType,
    ProbeOutcome,
    Repository,
    RepositoryKind,
    RepositoryProbe,
    RepositoryProbeKind,
    RepositoryProbeState,
    Secret,
)
from lakana_core.projects.probes import HostKey
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_core.secrets.service import insert_secret
from lakana_core.secrets.uses import SecretUse
from lakana_worker.deploy import DeploySettings
from lakana_worker.git import GitCredential
from lakana_worker.repository_probes import LEASE_SECONDS, RepositoryProbeLoop, scan_host_keys

ED25519_B64 = "AAAAC3NzaC1lZDI1NTE5AAAAIIuE9bmUom2wGn1ig3NKAYhOQ46XwTSXMg9/uXafUgYR"
KEYS = EnvKeyProvider(base64.b64encode(b"7" * 32).decode())
PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH "
    "PRIVATE KEY-----"
)


def resolves_to(*addresses: str) -> Callable[..., list[tuple[object, ...]]]:
    """Build a stand-in for `socket.getaddrinfo`.

    Parameters
    ----------
    *addresses
        The addresses it answers with.

    Returns
    -------
    callable
        The resolver.
    """

    def resolve(host: str, port: object, *args: object, **kwargs: object) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 22)) for a in addresses]

    return resolve


class Runs:
    """A stand-in for `subprocess.run` that records each call and answers as told.

    Parameters
    ----------
    stdout
        What the command prints.
    raises
        An exception to raise instead.
    """

    def __init__(self, stdout: str = "", raises: BaseException | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._stdout = stdout
        self._raises = raises

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Record a call.

        Parameters
        ----------
        argv
            The argument list.
        **kwargs
            `subprocess.run`'s keywords.

        Returns
        -------
        subprocess.CompletedProcess
            Exit 0 with the given output.
        """
        self.calls.append((argv, kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(argv, 0, self._stdout, "banner text of some service\n")


# --- scan_host_keys ---------------------------------------------------------------------------


def test_scan_refuses_a_never_reachable_address() -> None:
    """A name resolving to the metadata address is refused and ssh-keyscan never runs."""
    run = Runs()
    outcome, message, keys = scan_host_keys(
        "metadata.example", 22, resolve=resolves_to("203.0.113.7", "169.254.169.254"), run=run
    )
    assert outcome is ProbeOutcome.REFUSED_ADDRESS
    assert keys == [] and run.calls == []
    assert "metadata.example" not in message


def test_scan_connects_to_the_checked_address_with_an_argv_list() -> None:
    """The address that was checked is what ssh-keyscan reaches, after `--`, with no shell."""
    run = Runs(stdout=f"203.0.113.7 ssh-ed25519 {ED25519_B64}\n")
    outcome, _message, keys = scan_host_keys(
        "git.example.com", 2222, resolve=resolves_to("203.0.113.7"), run=run
    )
    assert outcome is ProbeOutcome.OK
    ((argv, kwargs),) = run.calls
    assert argv == [
        "ssh-keyscan",
        "-T",
        "5",
        "-p",
        "2222",
        "-t",
        "ed25519,ecdsa,rsa",
        "--",
        "203.0.113.7",
    ]
    assert kwargs.get("shell", False) is False
    assert kwargs["timeout"] == 25
    assert set(kwargs["env"]) <= {"PATH", "LC_ALL"}
    assert keys == [
        HostKey(
            type="ssh-ed25519",
            fingerprint="SHA256:BbeSiQQjnjpybq7nV8S8tEVnQiZNHfceXjKvrLVxecI",
            line=f"[git.example.com]:2222 ssh-ed25519 {ED25519_B64}",
        )
    ]


def test_scan_maps_timeout_and_refusal() -> None:
    """Timeouts, hosts without a key and unknown names map to fixed sentences.

    A scan past its time is timeout; a host that gives no key is unreachable; a name that does
    not resolve is unreachable; none of them echoes the tool's output.
    """
    timed_out = Runs(raises=subprocess.TimeoutExpired(["ssh-keyscan"], 25))
    assert (
        scan_host_keys("h", 22, resolve=resolves_to("203.0.113.7"), run=timed_out)[0]
        is ProbeOutcome.TIMEOUT
    )
    nothing = Runs(stdout="")
    outcome, message, _ = scan_host_keys("h", 22, resolve=resolves_to("203.0.113.7"), run=nothing)
    assert outcome is ProbeOutcome.UNREACHABLE
    assert "banner" not in message

    def unknown(*args: object, **kwargs: object) -> list[Any]:
        raise socket.gaierror("Name or service not known")

    outcome, message, _ = scan_host_keys("h", 22, resolve=unknown, run=nothing)
    assert outcome is ProbeOutcome.UNREACHABLE
    assert "Name or service" not in message
    missing = Runs(raises=FileNotFoundError("ssh-keyscan"))
    assert (
        scan_host_keys("h", 22, resolve=resolves_to("203.0.113.7"), run=missing)[0]
        is ProbeOutcome.FAILED
    )


# --- the loop ---------------------------------------------------------------------------------


@dataclass
class World:
    """A database with a person, an environment and an SSH repository whose key is a secret.

    Parameters
    ----------
    engine
        The database.
    person
        The requester's id.
    environment
        The environment's id.
    repository
        The repository's id.
    """

    engine: Any
    person: uuid.UUID
    environment: uuid.UUID
    repository: uuid.UUID

    def probe(self, kind: RepositoryProbeKind, url: str = "git@git.example.com:r.git") -> uuid.UUID:
        """Queue a probe now.

        Parameters
        ----------
        kind
            Its kind.
        url
            Its URL.

        Returns
        -------
        uuid.UUID
            Its id.
        """
        probe_id = uuid.uuid4()
        with Session(self.engine) as session:
            access = kind is RepositoryProbeKind.ACCESS
            session.add(
                RepositoryProbe(
                    id=probe_id,
                    kind=kind,
                    url=url,
                    repository_id=self.repository if access else None,
                    environment_id=self.environment if access else None,
                    requested_by=self.person,
                    state=RepositoryProbeState.QUEUED,
                    created_at=db_now(session),
                )
            )
            session.commit()
        return probe_id

    def read(self, probe_id: uuid.UUID) -> RepositoryProbe:
        """Read a probe afresh.

        Parameters
        ----------
        probe_id
            Its id.

        Returns
        -------
        RepositoryProbe
            The row, detached.
        """
        with Session(self.engine) as session:
            row = session.get(RepositoryProbe, probe_id)
            assert row is not None
            session.expunge(row)
            return row


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """Provide the database.

    Parameters
    ----------
    tmp_path
        Pytest's directory.

    Yields
    ------
    World
        The world.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/p.db"))
    Base.metadata.create_all(engine)
    person, env_id, repo_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with Session(engine) as session:
        session.add(Principal(id=person, type=PrincipalType.USER, name="admin", is_active=True))
        session.add(Environment(id=env_id, name="prod", storage_root="file:///srv/prod"))
        repo = Repository(
            id=repo_id,
            name="analytics",
            kind=RepositoryKind.SSH,
            url="git@git.example.com:team/analytics.git",
            credential_secret="analytics-deploy-key",
            ssh_known_hosts=f"git.example.com ssh-ed25519 {ED25519_B64}\n",
        )
        session.add(repo)
        session.flush()
        env = session.get(Environment, env_id)
        assert env is not None
        insert_secret(
            session,
            KEYS,
            PrincipalRef(id=person, name="admin", type=PrincipalType.USER),
            env,
            name="analytics-deploy-key",
            values={"private_key": PRIVATE_KEY},
            use=SecretUse.repository(repo),
        )
        session.commit()
    yield World(engine, person, env_id, repo_id)
    engine.dispose()


def loop(world: World, tmp_path: Path) -> RepositoryProbeLoop:
    """Build a loop over the world's database.

    Parameters
    ----------
    world
        The world.
    tmp_path
        Pytest's directory.

    Returns
    -------
    RepositoryProbeLoop
        The loop.
    """
    return RepositoryProbeLoop(
        world.engine,
        owner="w1",
        key_provider=KEYS,
        settings=DeploySettings(repo_roots=()),
        idle_seconds=0.05,
    )


def test_loop_scans_the_host_of_a_host_key_probe(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop scans the URL's host and port and stores the keys."""
    asked: list[tuple[str, int]] = []
    key = HostKey(type="ssh-ed25519", fingerprint="SHA256:x", line=f"h ssh-ed25519 {ED25519_B64}")

    def scan(host: str, port: int) -> tuple[ProbeOutcome, str, list[HostKey]]:
        asked.append((host, port))
        return ProbeOutcome.OK, "found one", [key]

    monkeypatch.setattr(module, "scan_host_keys", scan)
    probe_id = world.probe(RepositoryProbeKind.HOST_KEYS, "ssh://git@Git.Example.com:2222/r.git")
    assert loop(world, tmp_path).step() is True
    assert asked == [("git.example.com", 2222)]
    row = world.read(probe_id)
    assert (row.state, row.outcome, row.message) == (
        RepositoryProbeState.DONE,
        ProbeOutcome.OK,
        "found one",
    )
    assert row.host_keys == [{"type": "ssh-ed25519", "fingerprint": "SHA256:x", "line": key.line}]
    assert loop(world, tmp_path).step() is False


def test_loop_tests_access_with_the_environments_key_and_the_repositorys_known_hosts(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known hosts come from the repository row only; the deadline leaves room in the lease."""
    seen: dict[str, Any] = {}

    def check(**kwargs: object) -> tuple[ProbeOutcome, str]:
        seen.update(kwargs)
        return ProbeOutcome.OK, "read it"

    monkeypatch.setattr(module, "check_access", check)
    probe_id = world.probe(RepositoryProbeKind.ACCESS)
    assert loop(world, tmp_path).step()
    assert seen["kind"] is RepositoryKind.SSH
    assert seen["url"] == "git@git.example.com:team/analytics.git"
    assert seen["known_hosts"] == f"git.example.com ssh-ed25519 {ED25519_B64}\n"
    assert seen["credential"] == GitCredential(private_key=PRIVATE_KEY)
    assert LEASE_SECONDS == 90
    assert world.read(probe_id).outcome is ProbeOutcome.OK
    assert not any(Path(seen["workdir"]).parent.glob(Path(seen["workdir"]).name))


def test_loop_says_what_to_do_about_a_missing_secret(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A credential the environment lacks fails the probe naming the next step; git never runs."""
    with Session(world.engine) as session:
        session.execute(update(Secret).values(revoked_at=db_now(session)))
        session.commit()
    ran: list[object] = []
    monkeypatch.setattr(module, "check_access", lambda **kwargs: ran.append(kwargs))
    probe_id = world.probe(RepositoryProbeKind.ACCESS)
    assert loop(world, tmp_path).step()
    row = world.read(probe_id)
    assert row.outcome is ProbeOutcome.FAILED
    assert row.message is not None and "lakana secret create" in row.message
    assert ran == []


def test_loop_records_nothing_after_losing_the_lease(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A probe taken over during the scan keeps the new owner's state; the loop logs at info."""
    probe_id = world.probe(RepositoryProbeKind.HOST_KEYS)

    def scan(host: str, port: int) -> tuple[ProbeOutcome, str, list[HostKey]]:
        with Session(world.engine) as session:
            session.execute(
                update(RepositoryProbe)
                .where(RepositoryProbe.id == probe_id)
                .values(lease_owner="w2")
            )
            session.commit()
        return ProbeOutcome.OK, "late", []

    monkeypatch.setattr(module, "scan_host_keys", scan)
    with caplog.at_level(logging.INFO, logger="lakana_worker.repository_probes"):
        assert loop(world, tmp_path).step()
    row = world.read(probe_id)
    assert (row.state, row.outcome, row.lease_owner) == (RepositoryProbeState.RUNNING, None, "w2")
    assert any("lease" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)


def test_a_failing_check_is_recorded_as_failed_by_its_type_only(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An exception inside a check fails the probe with a fixed sentence; its text is not logged."""

    def boom(host: str, port: int) -> tuple[ProbeOutcome, str, list[HostKey]]:
        raise RuntimeError("secret-looking text")

    monkeypatch.setattr(module, "scan_host_keys", boom)
    probe_id = world.probe(RepositoryProbeKind.HOST_KEYS)
    with caplog.at_level(logging.WARNING, logger="lakana_worker.repository_probes"):
        assert loop(world, tmp_path).step()
    row = world.read(probe_id)
    assert row.outcome is ProbeOutcome.FAILED
    assert "secret-looking" not in (row.message or "") and "secret-looking" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_the_loop_thread_starts_and_stops(world: World, tmp_path: Path) -> None:
    """`start` runs a daemon thread that ends after `stop`."""
    probes = loop(world, tmp_path)
    thread = probes.start()
    assert thread.daemon
    probes.stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
