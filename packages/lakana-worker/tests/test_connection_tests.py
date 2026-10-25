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
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
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
from lakana_worker.connection_tests import ConnectionTestLoop

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


def test_the_docker_runner_is_refused_until_its_check_exists(world: World) -> None:
    with pytest.raises(ValueError, match="local runner only"):
        loop(world, runner_kind="docker")


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

    def broken(connection: dict[str, Any], routing: dict[str, Any]) -> tuple[str, str]:
        raise OSError(f"cannot start {SECRET}")

    monkeypatch.setattr(running, "_run_check", broken)
    with caplog.at_level(logging.WARNING, logger="lakana_worker.connection_tests"):
        assert running.step()
    row = world.test(test_id)
    assert (row.outcome, row.message) == ("failed", connection_tests.NOT_RUN)
    assert "OSError" in caplog.text
    assert SECRET not in caplog.text
