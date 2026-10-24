"""Shared fixtures for lakana-server tests."""

import base64
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.config import DatabaseConfig, SecretsConfig
from lakana_core.db import create_engine
from lakana_core.models import Base
from lakana_server.app import create_app
from lakana_server.auth_rate_limit import AuthAuditLimiter
from lakana_server.settings import Settings
from lakana_server.sign_in_audit import SignInAuditLimiter

_TEST_MASTER_KEY = base64.b64encode(b"0" * 32).decode()
"""A valid base64-encoded 32-byte master key, matching the fixed test key
used throughout lakana-core's own secrets tests (e.g.
`test_secrets_service.py`) — deterministic, never a real secret."""

AUTH_HEADER = {"Authorization": "Bearer lakana_pat_testkey_testsecret"}
"""A well-formed Authorization header accepted by get_principal. Real
verification now runs (see lakana_server.deps.get_principal) — this
authenticates because the settings fixture's Settings mints the bootstrap
principal's PAT with this exact key_id and secret."""


@pytest.fixture
def session() -> Iterator[Session]:
    """Provide a session against a fresh in-memory SQLite database.

    Used by tests that exercise `lakana_server.deps.get_principal` directly,
    independent of any route or running app.

    Yields
    ------
    Session
        A session whose schema has been created from the models.
    """
    engine = create_engine(DatabaseConfig(url="sqlite://"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _skip_fsync(dbapi_connection: object, connection_record: object) -> None:
    """Turn off SQLite's flush to disk on commit for a connection a test opened.

    Parameters
    ----------
    dbapi_connection
        The new DB-API connection; anything but a `sqlite3.Connection` is left alone.
    connection_record
        SQLAlchemy's record of the connection (unused).
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.execute("PRAGMA synchronous=OFF")


@pytest.fixture(scope="session", autouse=True)
def _sqlite_commits_without_fsync() -> Iterator[None]:
    """Let every SQLite engine in this test process commit without flushing to disk.

    Each commit otherwise waits for an `fsync`, which made it the largest cost of an
    API test after the schema. No unit test can observe durability across a power cut;
    the pragmas a test can observe (WAL, foreign keys, busy timeout) still come from
    `lakana_core.db`, whose listener runs on the same connections.

    Yields
    ------
    None
        The listener stays registered for the session and is removed afterwards.
    """
    event.listen(Engine, "connect", _skip_fsync)
    yield
    event.remove(Engine, "connect", _skip_fsync)


@pytest.fixture(scope="session")
def catalog_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create the catalog schema once per test process, in a read-only SQLite file.

    Creating the schema costs about 0.7 s (each of the hundreds of `CREATE` statements
    is a commit); copying the finished file costs under a millisecond. `settings` hands
    every test its own copy, so no test sees another's rows. The file is left in
    SQLite's default journal mode, so it is complete on its own (no `-wal` file to copy),
    and is made read-only, so a test that opened it by mistake fails instead of leaking.

    Parameters
    ----------
    tmp_path_factory
        Pytest's session-wide temporary directory factory.

    Returns
    -------
    Path
        The template database file.
    """
    template = tmp_path_factory.mktemp("catalog-template") / "lakana.db"
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{template}"))
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=DELETE")
    finally:
        engine.dispose()
    template.chmod(0o444)
    return template


@pytest.fixture
def settings(tmp_path: Path, catalog_template: Path) -> Settings:
    """Provide settings backed by a fresh SQLite database.

    The database is a private copy of `catalog_template`: the same schema
    `Base.metadata.create_all` creates, without paying for it per test.

    Parameters
    ----------
    tmp_path
        Pytest's per-test temporary directory.
    catalog_template
        The session's read-only database with the schema already created.

    Returns
    -------
    Settings
        Configuration pointed at an isolated database.
    """
    database = tmp_path / "lakana.db"
    shutil.copyfile(catalog_template, database)
    return Settings(
        database=DatabaseConfig(url=f"sqlite:///{database}"),
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="testsecret",
        secrets=SecretsConfig(master_key=_TEST_MASTER_KEY),
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """Provide a `TestClient` around a fully constructed app.

    Wraps `TestClient` as a context manager so the lifespan runs before any
    test issues a request, guaranteeing the bootstrap principal exists.

    Parameters
    ----------
    settings
        Configuration pointed at an isolated database.

    Yields
    ------
    TestClient
        A client for the running app.
    """
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def storage_root(tmp_path: Path) -> str:
    """Provide a per-test storage root under pytest's temporary directory.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.

    Returns
    -------
    str
        A `file://` URL no other test shares.
    """
    return f"file://{tmp_path}/lake"


@pytest.fixture(autouse=True)
def _isolate_create_environment_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `server_test_helpers.create_environment`'s default root at `tmp_path`.

    Applies to every test in this suite, not just ones that request it
    explicitly, so a test calling `create_environment` with no `root`
    argument still gets an isolated storage location rather than
    `create_environment`'s own module-level fallback (a fixed
    `tempfile.gettempdir()`-based path, used only outside pytest).

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    monkeypatch
        Used to replace the module attribute for the duration of the test.
    """
    from tests import server_test_helpers

    monkeypatch.setattr(server_test_helpers, "STORAGE_PARENT", tmp_path)


def fake_request(
    limiter: AuthAuditLimiter | None = None,
    client_host: str | None = "1.2.3.4",
    sign_in_limiter: SignInAuditLimiter | None = None,
) -> Request:
    """Build a minimal stand-in for `Request`, for calling `get_principal` directly.

    Carries only the attributes `get_principal` actually reads: `app.state.
    auth_audit_limiter`, `app.state.sign_in_audit_limiter`, `client.host` and the per-request
    `state`. Matches this test suite's
    existing pattern (see `test_deps.py`) of a `SimpleNamespace` `cast` to
    `Request` rather than constructing a real ASGI request.

    Parameters
    ----------
    limiter
        The limiter `get_principal` should consult on a failed attempt.
        Defaults to a fresh `AuthAuditLimiter` with a generous rate, so
        tests that don't care about rate limiting never see it interfere.
    client_host
        The value `request.client.host` should report, or `None` to
        simulate a request with no client info at all (`request.client` is
        `None`).
    sign_in_limiter
        The limiter `get_principal` should consult on a successful attempt. Defaults to a fresh
        `SignInAuditLimiter` with the default window of 15 minutes.

    Returns
    -------
    Request
        A `SimpleNamespace` cast to `Request`, carrying only the attributes
        above.
    """
    client = SimpleNamespace(host=client_host) if client_host is not None else None
    state = SimpleNamespace(
        auth_audit_limiter=limiter or AuthAuditLimiter(1000),
        denial_audit_limiter=DenialAuditLimiter(1000),
        sign_in_audit_limiter=sign_in_limiter or SignInAuditLimiter(15 * 60.0),
    )
    return cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=state), client=client, state=SimpleNamespace()),
    )
