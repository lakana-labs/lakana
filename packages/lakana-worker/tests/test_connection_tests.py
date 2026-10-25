"""The connection test loop: claims, the subprocess, secrets, leases and what must never leak."""

from __future__ import annotations

import base64
import json
import logging
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fake_launcher import FakeLauncher, Script, refusing
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.config import DatabaseConfig
from lakana_core.connections.service import ConnectionService
from lakana_core.db import create_engine, db_now
from lakana_core.models import (
    Base,
    ConnectionTest,
    ConnectionTestState,
    Environment,
    Principal,
    PrincipalType,
    Secret,
)
from lakana_core.secrets.keys import EnvKeyProvider
from lakana_worker import connection_tests
from lakana_worker.connection_tests import ConnectionTestLoop, DockerCheck
from lakana_worker.launcher_client import LauncherTimedOut, LauncherUnavailable

SECRET = "hunter2-s3cr3t"
KEYS = EnvKeyProvider(base64.b64encode(b"k" * 32).decode())


class World:
    """A database with an environment, a principal and helpers to create connections and tests.

    Parameters
    ----------
    engine
        The database.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        with Session(engine) as session:
            self.admin = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
            session.add(Principal(id=self.admin.id, type=self.admin.type, name="admin"))
            session.add(Environment(id=uuid.uuid4(), name="prod", storage_root="file:///tmp/p"))
            session.commit()

    def connection(
        self,
        name: str = "svc",
        *,
        kind: str = "tcp",
        address: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
        secret: dict[str, str] | None = None,
    ) -> uuid.UUID:
        """Create a connection and queue a test of it.

        Parameters
        ----------
        name
            The connection's name.
        kind
            Its kind.
        address
            Its address; a closed loopback port for ``tcp`` by default.
        settings
            Its settings.
        secret
            The values of a new secret bound to it, or `None`.

        Returns
        -------
        uuid.UUID
            The test's id.
        """
        with Session(self.engine) as session:
            service = ConnectionService(session, AllowAllAuthorization(), KEYS)
            service.create(
                self.admin,
                "prod",
                name=name,
                kind=kind,
                address=address or {"host": "127.0.0.1", "port": closed_port()},
                settings=settings or {},
                secret_values=secret,
            )
            return service.request_test(self.admin, "prod", name).id

    def test(self, test_id: uuid.UUID) -> ConnectionTest:
        """Read a test.

        Parameters
        ----------
        test_id
            Its id.

        Returns
        -------
        ConnectionTest
            The detached row.
        """
        with Session(self.engine) as session:
            row = session.get(ConnectionTest, test_id)
            assert row is not None
            session.expunge(row)
            return row


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """Provide a database with an environment and a principal.

    Parameters
    ----------
    tmp_path
        Pytest's directory.

    Yields
    ------
    World
        The world.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/c.db"))
    Base.metadata.create_all(engine)
    yield World(engine)
    engine.dispose()


def closed_port() -> int:
    """Find a loopback port nothing listens on.

    Returns
    -------
    int
        The port.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def listening() -> Generator[int, None, None]:
    """Accept connections on loopback and ignore them.

    Yields
    ------
    int
        The port.
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                held.append(server.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield server.getsockname()[1]
    finally:
        server.close()
        for sock in held:
            sock.close()


def loop(
    world: World,
    roots: dict[str, str] | None = None,
    *,
    key_provider: EnvKeyProvider = KEYS,
    runner_kind: str = "local",
    trust: Path | None = None,
    docker: DockerCheck | None = None,
) -> ConnectionTestLoop:
    """Build a loop over the world's database.

    Parameters
    ----------
    world
        The world.
    roots
        The worker's ingestion roots.
    key_provider
        The worker's key provider.
    runner_kind
        The worker's runner.
    trust
        The trust bundle the check is given.
    docker
        The Docker runner's launcher, owner, work directory and trust bundle.

    Returns
    -------
    ConnectionTestLoop
        The loop.
    """
    return ConnectionTestLoop(
        world.engine,
        owner="w1",
        key_provider=key_provider,
        runner_kind=runner_kind,
        ingestion_roots=roots or {},
        idle_seconds=0.05,
        trust=trust,
        docker=docker,
    )


def fake_check(
    monkeypatch: pytest.MonkeyPatch, stdout: str = "", stderr: str = "", returncode: int = 0
) -> list[dict[str, Any]]:
    """Replace the subprocess the loop starts, recording how it was called.

    Parameters
    ----------
    monkeypatch
        Pytest's patcher.
    stdout
        What the fake check writes.
    stderr
        What it writes to standard error.
    returncode
        Its exit status.

    Returns
    -------
    list of dict
        One entry per call: ``argv`` and the keyword arguments.
    """
    calls: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(connection_tests.subprocess, "run", run)
    return calls


def test_a_queued_test_ends_done_with_the_checks_outcome(world: World) -> None:
    refused = world.connection("closed")
    with listening() as port:
        reachable = world.connection("open", address={"host": "127.0.0.1", "port": port})
        steps = [loop(world).step(), loop(world).step(), loop(world).step()]
    assert steps == [True, True, False]
    one, two = world.test(refused), world.test(reachable)
    assert (one.state, one.outcome) == (ConnectionTestState.DONE, "refused")
    assert one.message is not None and "127.0.0.1" in one.message
    assert (two.state, two.outcome) == (ConnectionTestState.DONE, "ok")
    assert two.message is not None and two.message.startswith("reachable")
    assert (one.lease_owner, two.finished_at is not None) == (None, True)


def test_a_directory_connection_reads_the_workers_root(world: World, tmp_path: Path) -> None:
    root = tmp_path / "shared"
    (root / "in").mkdir(parents=True)
    test_id = world.connection(
        "exports", kind="directory", address={"root": "shared", "path": "in"}
    )
    assert loop(world, {"shared": str(root)}).step()
    row = world.test(test_id)
    assert (row.outcome, row.state) == ("ok", ConnectionTestState.DONE)


def test_a_root_the_worker_lacks_is_root_unavailable_without_running_a_check(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_check(monkeypatch)
    test_id = world.connection(
        "exports", kind="directory", address={"root": "shared", "path": "in"}
    )
    assert loop(world).step()
    row = world.test(test_id)
    assert row.outcome == "root_unavailable"
    assert row.message is not None
    assert row.message.startswith("`exports` names ingestion root `shared`")
    assert calls == []


def test_the_check_is_isolated_and_gets_the_connection_on_stdin_only(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_check(monkeypatch, json.dumps({"outcome": "ok", "message": "fine"}) + "\n")
    world.connection("svc", secret={"password": SECRET})
    trust = tmp_path / "trust.pem"
    assert loop(world, trust=trust).step()
    (call,) = calls
    assert call["argv"] == [sys.executable, "-I", "-m", "lakana.ingest._check"]
    sent = json.loads(call["input"])
    assert sent["connection"]["name"] == "svc"
    assert sent["connection"]["kind"] == "tcp"
    assert sent["connection"]["secret"] == {"password": SECRET}
    assert sent["connection"]["endpoints"][0]["host"] == "127.0.0.1"
    assert sent["routing"]["kind"] == "tcp"
    assert sent["routing"]["grant"][0][0] == "127.0.0.1"
    assert call["timeout"] == 30
    assert call["capture_output"] is True
    assert call["env"] == {"LAKANA_CA_BUNDLE": str(trust)}
    assert SECRET not in " ".join(call["argv"])


def test_the_secret_never_reaches_the_stored_message(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = {"outcome": "failed", "message": f"the server said {SECRET} twice: {SECRET}"}
    fake_check(monkeypatch, json.dumps(answer) + "\n")
    test_id = world.connection("svc", secret={"password": SECRET})
    assert loop(world).step()
    row = world.test(test_id)
    assert row.outcome == "failed"
    assert row.message == "the server said *** twice: ***"


def test_a_secret_is_masked_even_when_the_real_check_echoes_it(world: World) -> None:
    test_id = world.connection(
        "svc",
        address={"host": "127.0.0.1", "port": closed_port()},
        secret={"password": "127.0.0.1"},
    )
    assert loop(world).step()
    row = world.test(test_id)
    assert row.outcome == "refused"
    assert row.message is not None and "127.0.0.1" not in row.message


def test_a_worker_without_the_master_key_fails_a_test_of_a_connection_with_a_secret(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_check(monkeypatch)
    test_id = world.connection("svc", secret={"password": SECRET})
    assert loop(world, key_provider=EnvKeyProvider(None)).step()
    row = world.test(test_id)
    assert row.outcome == "failed"
    assert row.message == (
        "this worker cannot decrypt secrets: set LAKANA_SECRETS__MASTER_KEY_FILE as on the server"
    )
    assert calls == []


def test_a_worker_without_the_master_key_still_tests_a_connection_without_a_secret(
    world: World,
) -> None:
    test_id = world.connection("svc")
    assert loop(world, key_provider=EnvKeyProvider(None)).step()
    assert world.test(test_id).outcome == "refused"


def test_a_wrong_master_key_says_so_and_never_the_ciphertext(world: World) -> None:
    test_id = world.connection("svc", secret={"password": SECRET})
    other = EnvKeyProvider(base64.b64encode(b"z" * 32).decode())
    assert loop(world, key_provider=other).step()
    row = world.test(test_id)
    assert row.outcome == "failed"
    assert row.message is not None
    assert "cannot decrypt the connection's secret" in row.message
    assert SECRET not in row.message


def test_a_worker_without_lakana_says_to_install_it_and_starts_no_subprocess(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_check(monkeypatch)
    monkeypatch.setattr(connection_tests.importlib.util, "find_spec", lambda name: None)
    test_id = world.connection("svc")
    assert loop(world).step()
    row = world.test(test_id)
    assert (row.outcome, row.message) == (
        "failed",
        "install lakana next to the worker to run connection tests",
    )
    assert calls == []


def test_a_worker_with_lakana_looks_for_the_check_module(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []
    real = connection_tests.importlib.util.find_spec

    def find(name: str) -> object:
        asked.append(name)
        return real(name)

    monkeypatch.setattr(connection_tests.importlib.util, "find_spec", find)
    test_id = world.connection("svc")
    assert loop(world).step()
    assert asked == ["lakana.ingest._check"]
    assert world.test(test_id).outcome == "refused"


def test_a_check_that_answers_nonsense_is_a_failed_test(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_check(monkeypatch, "not json\n", stderr=f"trace with {SECRET}", returncode=1)
    test_id = world.connection("svc", secret={"password": SECRET})
    assert loop(world).step()
    row = world.test(test_id)
    assert row.outcome == "failed"
    assert row.message is not None and SECRET not in row.message


def test_a_check_that_runs_too_long_is_a_timeout(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def slow(
        argv: list[str], *, timeout: float, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(connection_tests.subprocess, "run", slow)
    test_id = world.connection("svc")
    with caplog.at_level(logging.WARNING, logger="lakana_worker.connection_tests"):
        assert loop(world).step()
    row = world.test(test_id)
    assert (row.outcome, row.message) == (
        "timeout",
        "the check did not finish within 30 s; an endpoint may accept connections but never "
        "answer: check each host and port",
    )
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["A connection check did not finish within 30 s."]


def test_a_test_whose_lease_ran_out_is_taken_again(world: World) -> None:
    test_id = world.connection("svc")
    with Session(world.engine) as session:
        past = db_now(session) - timedelta(seconds=5)
        session.execute(
            update(ConnectionTest)
            .where(ConnectionTest.id == test_id)
            .values(state=ConnectionTestState.RUNNING, lease_owner="dead", lease_expires_at=past)
        )
        session.commit()
    assert loop(world).step()
    row = world.test(test_id)
    assert (row.state, row.outcome) == (ConnectionTestState.DONE, "refused")


def test_a_lease_lost_during_the_check_records_nothing_and_logs_at_info_level(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    test_id = world.connection("svc")

    def taken_over(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        with Session(world.engine) as session:
            session.execute(
                update(ConnectionTest)
                .where(ConnectionTest.id == test_id)
                .values(lease_owner="w2", lease_expires_at=db_now(session) + timedelta(hours=1))
            )
            session.commit()
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"outcome": "ok", "message": "late"}) + "\n", ""
        )

    monkeypatch.setattr(connection_tests.subprocess, "run", taken_over)
    with caplog.at_level(logging.INFO, logger="lakana_worker.connection_tests"):
        assert loop(world).step() is True
    row = world.test(test_id)
    assert (row.state, row.outcome, row.lease_owner) == (ConnectionTestState.RUNNING, None, "w2")
    records = [r for r in caplog.records if "lease was lost" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.INFO]


def test_a_docker_loop_without_its_launcher_and_an_unknown_runner_are_refused(
    world: World,
) -> None:
    with pytest.raises(ValueError, match="needs the launcher"):
        loop(world, runner_kind="docker")
    with pytest.raises(ValueError, match="runner `podman`"):
        loop(world, runner_kind="podman")


def test_the_thread_runs_a_queued_test_and_stops(world: World) -> None:
    test_id = world.connection("svc")
    running = loop(world)
    thread = running.start()
    try:
        deadline = time.monotonic() + 20
        while world.test(test_id).state is not ConnectionTestState.DONE:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        running.stop()
        thread.join(timeout=10)
    assert thread.name == "lakana-connection-tests"
    assert not thread.is_alive()
    with Session(world.engine) as session:
        assert session.scalars(select(ConnectionTest.outcome)).all() == ["refused"]


def test_a_failing_step_is_logged_by_type_only_and_the_loop_goes_on(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    running = loop(world)
    steps: list[int] = []

    def broken() -> bool:
        steps.append(1)
        if len(steps) >= 3:
            running.stop()
        raise RuntimeError(f"database said {SECRET}")

    monkeypatch.setattr(running, "step", broken)
    with caplog.at_level(logging.WARNING, logger="lakana_worker.connection_tests"):
        running.run()
    assert len(steps) == 3
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text
    assert SECRET not in text
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_an_error_after_the_claim_finishes_the_test_as_failed_and_logs_the_type_only(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    test_id = world.connection("svc", secret={"password": SECRET})
    running = loop(world)

    def broken(session: Session, test: ConnectionTest) -> object:
        raise RuntimeError(f"decrypting said {SECRET}")

    monkeypatch.setattr(running, "_request", broken)
    with caplog.at_level(logging.WARNING, logger="lakana_worker.connection_tests"):
        assert running.step() is True
    row = world.test(test_id)
    assert (row.state, row.outcome) == (ConnectionTestState.DONE, "failed")
    assert row.message == "the check could not run (see the worker's log)"
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text
    assert SECRET not in text


def test_find_spec_failing_means_lakana_is_not_installed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(name: str) -> object:
        raise ValueError(name)

    monkeypatch.setattr(connection_tests.importlib.util, "find_spec", broken)
    test_id = world.connection("svc")
    assert loop(world).step()
    assert world.test(test_id).message == (
        "install lakana next to the worker to run connection tests"
    )


def test_a_connection_that_vanished_fails_the_test(world: World) -> None:
    ghost = ConnectionTest(id=uuid.uuid4(), connection_id=uuid.uuid4(), environment_id=uuid.uuid4())
    with Session(world.engine) as session:
        assert loop(world)._request(session, ghost) == (
            "failed",
            "the connection no longer exists",
        )


def test_an_unbound_secret_fails_the_test_without_running_a_check(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_check(monkeypatch)
    test_id = world.connection("svc", secret={"password": SECRET})
    with Session(world.engine) as session:
        session.execute(update(Secret).values(use_kind=None, use_target_id=None))
        session.commit()
    assert loop(world).step()
    row = world.test(test_id)
    assert (row.outcome, row.message) == (
        "failed",
        "the connection's secret is not bound to it; bind it again (see `lakana connection "
        "update --help`)",
    )
    assert calls == []


def test_a_revoked_secret_fails_the_test(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_check(monkeypatch)
    test_id = world.connection("svc", secret={"password": SECRET})
    with Session(world.engine) as session:
        session.execute(update(Secret).values(revoked_at=db_now(session)))
        session.commit()
    assert loop(world).step()
    row = world.test(test_id)
    assert row.outcome == "failed"
    assert row.message is not None and "revoked or missing" in row.message
    assert calls == []


def test_an_outcome_the_check_does_not_define_is_a_failed_test(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_check(monkeypatch, json.dumps({"outcome": "great", "message": "x"}) + "\n")
    test_id = world.connection("svc")
    assert loop(world).step()
    row = world.test(test_id)
    assert (row.outcome, row.message) == ("failed", connection_tests.CHECK_BROKEN)


def test_an_error_while_running_the_check_finishes_the_test_as_failed(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    test_id = world.connection("svc")
    running = loop(world)

    def broken(connection: dict[str, Any], plan: object) -> tuple[str, str]:
        raise OSError(f"cannot start {SECRET}")

    monkeypatch.setattr(running, "_run_check", broken)
    with caplog.at_level(logging.WARNING, logger="lakana_worker.connection_tests"):
        assert running.step()
    row = world.test(test_id)
    assert (row.outcome, row.message) == ("failed", connection_tests.NOT_RUN)
    assert "OSError" in caplog.text
    assert SECRET not in caplog.text


OWN_PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
BUNDLE = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def work_dir(tmp_path: Path) -> Path:
    """Make the worker's private attempts directory.

    Parameters
    ----------
    tmp_path
        Pytest's directory.

    Returns
    -------
    Path
        ``<tmp_path>/attempts``, mode 0700.
    """
    attempts = tmp_path / "attempts"
    attempts.mkdir(mode=0o700)
    return attempts


def answering(output: bytes, result: dict[str, object] | None = None) -> Script:
    """Build a `check` script that prints `output` and returns `result`.

    Parameters
    ----------
    output
        What the check printed.
    result
        The launcher's result; a clean exit by default.

    Returns
    -------
    Script
        The script.
    """

    def script(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        on_log(output)
        return result or {"exit": 0, "oom_killed": False, "timed_out": False, "stopped": False}

    return script


def docker_loop(
    world: World, tmp_path: Path, launcher: FakeLauncher, *, trust: bytes | None = None
) -> tuple[ConnectionTestLoop, Path]:
    """Build a loop for the Docker runner over a fake launcher.

    Parameters
    ----------
    world
        The world.
    tmp_path
        Pytest's directory; the attempts directory is made in it.
    launcher
        The fake launcher.
    trust
        The worker's trust bundle.

    Returns
    -------
    tuple
        The loop and its attempts directory.
    """
    attempts = work_dir(tmp_path)
    docker = DockerCheck(client=launcher, owner="box-1.abc", work_dir=attempts, trust=trust)
    return loop(world, runner_kind="docker", docker=docker), attempts


OK_LINE = json.dumps({"outcome": "ok", "message": "reachable"}).encode() + b"\n"


def test_a_docker_check_asks_the_launcher_with_a_tasks_routing(
    world: World, tmp_path: Path
) -> None:
    launcher = FakeLauncher(scripts={"check": answering(OK_LINE)})
    running, attempts = docker_loop(world, tmp_path, launcher)
    test_id = world.connection(
        "api",
        kind="http",
        address={"base_url": "https://api.example.com"},
        settings={
            "auth": "oauth2",
            "client_id": "lakana",
            "token_url": "https://login.example.com/token",
        },
        secret={"client_secret": SECRET},
    )

    assert running.step()

    row = world.test(test_id)
    assert (row.state, row.outcome, row.message) == (ConnectionTestState.DONE, "ok", "reachable")
    (request,) = launcher.requests
    assert set(request) == {
        "op", "check", "owner", "python", "attempt", "stdin", "roots", "trust", "egress", "relay"
    }  # fmt: skip
    assert request["op"] == "check"
    assert len(request["check"]) == 32 and int(request["check"], 16) >= 0
    assert (request["owner"], request["python"]) == ("box-1.abc", OWN_PYTHON)
    assert request["attempt"].startswith("attempt-check_")
    assert request["egress"] == [["api.example.com", 443], ["login.example.com", 443]]
    assert (request["relay"], request["roots"], request["trust"]) == ([], [], False)
    sent = json.loads(request["stdin"])
    assert sent["connection"]["secret"] == {"client_secret": SECRET}
    assert sent["routing"] == {
        "kind": "http",
        "grant": [["api.example.com", 443], ["login.example.com", 443]],
    }
    assert launcher.timeouts == [connection_tests.CHECK_WAIT_SECONDS]
    assert list(attempts.iterdir()) == []


def test_a_docker_check_of_a_database_gets_a_relay_route_and_its_port(
    world: World, tmp_path: Path
) -> None:
    launcher = FakeLauncher(scripts={"check": answering(OK_LINE)})
    running, _ = docker_loop(world, tmp_path, launcher)
    world.connection(
        "crm",
        kind="postgresql",
        address={"host": "db.example.com", "port": 5432, "database": "c"},
        settings={"user": "reader"},
    )

    assert running.step()

    (request,) = launcher.requests
    assert request["relay"] == [{"listen": 15001, "host": "db.example.com", "port": 5432}]
    assert request["egress"] == []
    routing = json.loads(request["stdin"])["routing"]
    assert routing["relay_port"] == 15001


def test_a_docker_check_of_a_directory_mounts_its_root_without_the_workers_roots(
    world: World, tmp_path: Path
) -> None:
    launcher = FakeLauncher(scripts={"check": answering(OK_LINE)})
    running, _ = docker_loop(world, tmp_path, launcher)
    world.connection("exports", kind="directory", address={"root": "shared", "path": "in"})

    assert running.step()

    (request,) = launcher.requests
    assert (request["roots"], request["egress"], request["relay"]) == (["shared"], [], [])
    routing = json.loads(request["stdin"])["routing"]
    assert routing["root_dir"] == "/ingest/shared"
    assert routing["root_path"] == "/ingest/shared/in"


def test_a_docker_check_gets_the_workers_trust_bundle_in_its_attempt(
    world: World, tmp_path: Path
) -> None:
    found: list[bytes] = []

    def script(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
        attempt = tmp_path / "attempts" / request["attempt"]
        assert attempt.stat().st_mode & 0o777 == 0o700
        found.append((attempt / "trust" / "trust.pem").read_bytes())
        on_log(OK_LINE)
        return {"exit": 0}

    launcher = FakeLauncher(scripts={"check": script})
    running, attempts = docker_loop(world, tmp_path, launcher, trust=BUNDLE)
    world.connection("svc")

    assert running.step()

    assert found == [BUNDLE]
    assert launcher.requests[0]["trust"] is True
    assert list(attempts.iterdir()) == []


def test_a_docker_check_takes_the_last_answer_and_masks_the_secret(
    world: World, tmp_path: Path
) -> None:
    output = (
        b"a warning on stderr\n"
        + json.dumps({"outcome": "refused", "message": "earlier"}).encode()
        + b"\n"
        + json.dumps({"outcome": "credentials", "message": f"login {SECRET} refused"}).encode()
        + b"\n\n"
    )
    launcher = FakeLauncher(scripts={"check": answering(output)})
    running, _ = docker_loop(world, tmp_path, launcher)
    test_id = world.connection("svc", secret={"password": SECRET})

    assert running.step()

    row = world.test(test_id)
    assert (row.outcome, row.message) == ("credentials", "login *** refused")


@pytest.mark.parametrize(
    ("output", "result", "outcome", "message"),
    [
        (b"", {"exit": 0}, "failed", connection_tests.CHECK_BROKEN),
        (b"not json\n[1, 2]\n", {"exit": 1}, "failed", connection_tests.CHECK_BROKEN),
        (
            json.dumps({"outcome": "great", "message": "x"}).encode(),
            {"exit": 0},
            "failed",
            connection_tests.CHECK_BROKEN,
        ),
        (b"", {"exit": None, "timed_out": True}, "timeout", connection_tests.CHECK_TIMED_OUT),
        (OK_LINE, {"exit": None, "oom_killed": True}, "failed", connection_tests.CHECK_OOM),
        (OK_LINE, {"exit": None, "stopped": True}, "failed", connection_tests.CHECK_STOPPED),
    ],
)
def test_a_docker_check_without_a_usable_answer_says_why(
    world: World,
    tmp_path: Path,
    output: bytes,
    result: dict[str, object],
    outcome: str,
    message: str,
) -> None:
    """No answer, nonsense, an unknown outcome, the time limit, the memory limit, a stop.

    Parameters
    ----------
    world
        The world.
    tmp_path
        Pytest's directory.
    output
        What the check printed.
    result
        The launcher's result.
    outcome
        The recorded outcome.
    message
        The recorded message.
    """
    launcher = FakeLauncher(scripts={"check": answering(output, result)})
    running, attempts = docker_loop(world, tmp_path, launcher)
    test_id = world.connection("svc")

    assert running.step()

    row = world.test(test_id)
    assert (row.outcome, row.message) == (outcome, message)
    assert list(attempts.iterdir()) == []


def _timed_out(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
    raise LauncherTimedOut("the launcher did not answer within 60 s")


def _unavailable(request: dict[str, Any], on_log: Callable[[bytes], None]) -> dict[str, object]:
    raise LauncherUnavailable("runner unavailable: no launcher at /run/l.sock")


@pytest.mark.parametrize(
    ("script", "cause"),
    [
        (refusing(f"python: one of 3.13 (asked {SECRET})"), "the launcher refused the check: "),
        (_unavailable, "runner unavailable: no launcher at /run/l.sock"),
        (_timed_out, "the launcher did not answer within 60 s"),
    ],
)
def test_a_launcher_failure_is_a_failed_test_that_names_the_cause_and_the_log(
    world: World, tmp_path: Path, script: Script, cause: str
) -> None:
    """A refusal, no launcher, no answer in time: `failed`, the cause, the launcher's log.

    Parameters
    ----------
    world
        The world.
    tmp_path
        Pytest's directory.
    script
        What the launcher does.
    cause
        Part of the message.
    """
    launcher = FakeLauncher(scripts={"check": script})
    running, attempts = docker_loop(world, tmp_path, launcher)
    test_id = world.connection("svc", secret={"password": SECRET})

    assert running.step()

    row = world.test(test_id)
    assert row.outcome == "failed"
    assert row.message is not None
    assert cause in row.message
    assert row.message.endswith("see `docker compose logs launcher`")
    assert SECRET not in row.message
    assert list(attempts.iterdir()) == []


def test_a_docker_check_whose_work_directory_is_not_private_does_not_run(
    world: World, tmp_path: Path
) -> None:
    launcher = FakeLauncher(scripts={"check": answering(OK_LINE)})
    running, attempts = docker_loop(world, tmp_path, launcher)
    attempts.chmod(0o755)
    test_id = world.connection("svc")

    assert running.step()

    row = world.test(test_id)
    assert (row.outcome, row.message) == ("failed", connection_tests.NO_ATTEMPT_DIR)
    assert launcher.requests == []


def test_a_docker_check_whose_trust_cannot_be_installed_does_not_run(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(bundle: bytes, attempt_dir: Path) -> None:
        raise OSError(f"{attempt_dir}/trust is not a directory of the worker's own")

    monkeypatch.setattr(connection_tests, "install_trust", broken)
    launcher = FakeLauncher(scripts={"check": answering(OK_LINE)})
    running, attempts = docker_loop(world, tmp_path, launcher, trust=BUNDLE)
    test_id = world.connection("svc")

    assert running.step()

    row = world.test(test_id)
    assert row.outcome == "failed"
    assert row.message is not None
    assert row.message.startswith("could not prepare the check's directory: ")
    assert launcher.requests == []
    assert list(attempts.iterdir()) == []


def test_a_docker_check_needs_no_lakana_next_to_the_worker(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check lives in the task image's /opt/lakana-check, not in the worker's Python."""
    monkeypatch.setattr(connection_tests.importlib.util, "find_spec", lambda name: None)
    launcher = FakeLauncher(scripts={"check": answering(OK_LINE)})
    running, _ = docker_loop(world, tmp_path, launcher)
    test_id = world.connection("svc")

    assert running.step()

    assert world.test(test_id).outcome == "ok"


@pytest.mark.parametrize(
    "refusal",
    [
        "op must be one of build, cleanup, run, status, stop",
        "check takes exactly the fields attempt, check; the worker and the launcher must be "
        "the same release",
    ],
)
def test_a_launcher_of_another_release_says_to_upgrade_both(
    world: World, tmp_path: Path, refusal: str
) -> None:
    """An earlier launcher does not know the op; a different one disagrees on its fields.

    Parameters
    ----------
    world
        The world.
    tmp_path
        Pytest's directory.
    refusal
        The launcher's reason.
    """
    launcher = FakeLauncher(scripts={"check": refusing(refusal)})
    running, _ = docker_loop(world, tmp_path, launcher)
    test_id = world.connection("svc")

    assert running.step()

    message = world.test(test_id).message
    assert message is not None
    assert message.startswith(f"the launcher refused the check: {refusal}; ")
    assert message.endswith(connection_tests.RELEASE_SKEW)


def test_a_check_directory_that_cannot_be_made_fails_the_test_with_the_cause(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(prefix: str, dir: Path) -> str:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(connection_tests.tempfile, "mkdtemp", full)
    launcher = FakeLauncher(scripts={"check": answering(OK_LINE)})
    running, _ = docker_loop(world, tmp_path, launcher)
    test_id = world.connection("svc")

    assert running.step()

    assert world.test(test_id).message == (
        "could not prepare the check's directory: No space left on device"
    )
    assert launcher.requests == []


def test_a_check_directory_left_behind_is_logged_by_its_path(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(connection_tests, "remove_private_tree", lambda path: False)
    launcher = FakeLauncher(scripts={"check": answering(OK_LINE)})
    running, attempts = docker_loop(world, tmp_path, launcher)
    test_id = world.connection("svc")

    with caplog.at_level(logging.WARNING, logger="lakana_worker.connection_tests"):
        assert running.step()

    assert world.test(test_id).outcome == "ok"
    (left,) = attempts.iterdir()
    assert f"The check's directory {left} was not removed completely." in caplog.text


@pytest.mark.parametrize(
    ("refusal", "message"),
    [
        (
            "roots: a list of distinct names from LAKANA_LAUNCHER__INGESTION_ROOTS (landing)",
            "`exports` names ingestion root `shared`, which this worker does not have. Set it in "
            "`LAKANA_LAUNCHER__INGESTION_ROOTS` (Docker runner) or `LAKANA_RUN__INGESTION_ROOTS` "
            "(local runner).",
        ),
        (
            "ingestion root 'shared' holds a socket, pipe or device node; move it out",
            "ingestion root 'shared' holds a socket, pipe or device node; move it out",
        ),
    ],
)
def test_a_root_the_launcher_lacks_or_refuses_is_root_unavailable(
    world: World, tmp_path: Path, refusal: str, message: str
) -> None:
    """As under the local runner: the root, not the launcher, is what to fix.

    Parameters
    ----------
    world
        The world.
    tmp_path
        Pytest's directory.
    refusal
        The launcher's reason.
    message
        The recorded message.
    """
    launcher = FakeLauncher(scripts={"check": refusing(refusal)})
    running, _ = docker_loop(world, tmp_path, launcher)
    test_id = world.connection(
        "exports", kind="directory", address={"root": "shared", "path": "in"}
    )

    assert running.step()

    row = world.test(test_id)
    assert (row.outcome, row.message) == ("root_unavailable", message)


def test_a_roots_refusal_that_lists_the_root_is_an_ordinary_refusal(
    world: World, tmp_path: Path
) -> None:
    refusal = "roots: a list of distinct names from LAKANA_LAUNCHER__INGESTION_ROOTS (shared)"
    launcher = FakeLauncher(scripts={"check": refusing(refusal)})
    running, _ = docker_loop(world, tmp_path, launcher)
    test_id = world.connection(
        "exports", kind="directory", address={"root": "shared", "path": "in"}
    )

    assert running.step()

    row = world.test(test_id)
    assert row.outcome == "failed"
    assert (
        row.message
        == f"the launcher refused the check: {refusal}; see `docker compose logs launcher`"
    )
