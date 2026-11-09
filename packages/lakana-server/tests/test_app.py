"""Tests for app construction, bootstrap, and cross-cutting behaviour."""

import asyncio
import logging
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.bootstrap import ensure_bootstrap_principal
from lakana_core.config import DatabaseConfig, RunConfig
from lakana_core.db import create_engine
from lakana_core.errors import AuthenticationError, ConflictError, ValidationError
from lakana_core.grants.roles import Role
from lakana_core.identity.pats import PatService
from lakana_core.identity.task_tokens import TaskTokenService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Base,
    Grant,
    PersonalAccessToken,
    Principal,
    PrincipalType,
)
from lakana_server import app as app_module
from lakana_server.app import create_app
from lakana_server.auth_rate_limit import AuthAuditLimiter
from lakana_server.deps import get_principal
from lakana_server.settings import AuthAuditConfig, Settings
from tests.conftest import AUTH_HEADER, fake_request


@contextmanager
def _app_logger_enabled() -> Generator[None, None, None]:
    """Temporarily force `lakana_server.app`'s logger enabled, for `caplog`.

    `lakana_core.migrations.env` calls `logging.config.fileConfig`, whose
    default `disable_existing_loggers=True` marks every logger that
    already existed process-wide as disabled, regardless of level. When
    the full suite runs a migrations test before this module, that flag
    alone silences `caplog` even though the level is right — the same
    gotcha `test_route.py` and `test_poller.py` work around for their own
    loggers.

    Yields
    ------
    None
        Control returns to the caller with the logger's `disabled` flag
        forced `False`; the prior value is restored afterward.
    """
    app_logger = logging.getLogger("lakana_server.app")
    previously_disabled = app_logger.disabled
    app_logger.disabled = False
    try:
        yield
    finally:
        app_logger.disabled = previously_disabled


def _pat_service(session: Session) -> PatService:
    """Build a PatService for direct get_principal tests.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PatService
        A service using allow-all authorization, matching
        `lakana_server.deps.get_pat_service`.
    """
    return PatService(session, AllowAllAuthorization())


def _task_token_service(session: Session) -> TaskTokenService:
    """Build a TaskTokenService for direct get_principal tests.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    TaskTokenService
        A service using allow-all authorization, matching
        `lakana_server.deps.get_task_token_service`.
    """
    return TaskTokenService(session, AllowAllAuthorization())


class _FakeClock:
    """A controllable clock for a deterministic `AuthAuditLimiter` in tests.

    Parameters
    ----------
    start
        The time `__call__` returns before any `advance` call.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Parameters
        ----------
        seconds
            How many seconds to add to the current time.
        """
        self._now += seconds

    def __call__(self) -> float:
        """Return the current fake time.

        Returns
        -------
        float
            The time set by the constructor and any `advance` calls so far.
        """
        return self._now


def test_get_principal_rejects_a_missing_header(session: Session) -> None:
    """Direct unit test, independent of any route existing yet."""
    with pytest.raises(AuthenticationError):
        get_principal(
            request=fake_request(),
            authorization=None,
            session=session,
            pats=_pat_service(session),
            task_tokens=_task_token_service(session),
        )


def test_get_principal_rejects_a_malformed_header(session: Session) -> None:
    """The header must be a Bearer PAT or task token, checked before any DB lookup."""
    with pytest.raises(AuthenticationError):
        get_principal(
            request=fake_request(),
            authorization="Bearer not-a-real-token",
            session=session,
            pats=_pat_service(session),
            task_tokens=_task_token_service(session),
        )


def test_get_principal_accepts_a_real_pat(session: Session) -> None:
    """A PAT actually issued via PatService authenticates as its principal."""
    admin = ensure_bootstrap_principal(session, "admin")
    pat_service = _pat_service(session)
    issued = pat_service.issue(admin, "admin", name="test")

    ref = get_principal(
        request=fake_request(),
        authorization=f"Bearer {issued.token}",
        session=session,
        pats=pat_service,
        task_tokens=_task_token_service(session),
    )
    assert ref.name == "admin"


def test_get_principal_accepts_a_lowercase_bearer_scheme(session: Session) -> None:
    """RFC 7235 makes the auth-scheme token case-insensitive; real clients send `bearer`."""
    admin = ensure_bootstrap_principal(session, "admin")
    pat_service = _pat_service(session)
    issued = pat_service.issue(admin, "admin", name="test")

    ref = get_principal(
        request=fake_request(),
        authorization=f"bearer {issued.token}",
        session=session,
        pats=pat_service,
        task_tokens=_task_token_service(session),
    )
    assert ref.name == "admin"


def test_get_principal_rejects_a_pat_shaped_header_with_the_wrong_secret(
    session: Session,
) -> None:
    """A syntactically valid PAT with the wrong secret is rejected, not shape-checked."""
    admin = ensure_bootstrap_principal(session, "admin")
    pat_service = _pat_service(session)
    issued = pat_service.issue(admin, "admin", name="test")
    wrong = f"lakana_pat_{issued.key_id}_wrong-secret"

    with pytest.raises(AuthenticationError):
        get_principal(
            request=fake_request(),
            authorization=f"Bearer {wrong}",
            session=session,
            pats=pat_service,
            task_tokens=_task_token_service(session),
        )


def test_get_principal_accepts_a_real_task_token(session: Session) -> None:
    """A task token actually minted via TaskTokenService authenticates as its principal."""
    admin = ensure_bootstrap_principal(session, "admin")
    task_token_service = _task_token_service(session)
    minted = task_token_service.mint(
        admin, "admin", environment="dev", asset_scope=[], ttl_seconds=300
    )

    ref = get_principal(
        request=fake_request(),
        authorization=f"Bearer {minted.token}",
        session=session,
        pats=_pat_service(session),
        task_tokens=task_token_service,
    )
    assert ref.name == "admin"


def test_a_non_default_bootstrap_admin_is_used_end_to_end(tmp_path: Path) -> None:
    """The configured name, not a hardcoded "admin", is what `get_principal` resolves.

    A real request, dispatched through FastAPI's dependency injection,
    authenticates as the principal the lifespan bootstrapped from a
    non-default `Settings.bootstrap_admin` value.
    """
    engine = create_engine(DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"))
    Base.metadata.create_all(engine)
    engine.dispose()

    settings = Settings(
        database=DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"),
        bootstrap_admin="root",
        bootstrap_pat_key_id="testkey",
        bootstrap_pat_secret="testsecret",
    )
    app = create_app(settings)

    @app.get("/_probe/principal")
    def probe(principal: PrincipalRef = Depends(get_principal)) -> dict[str, str]:
        return {"name": principal.name}

    with TestClient(app) as client:
        response = client.get(
            "/_probe/principal",
            headers={"Authorization": "Bearer lakana_pat_testkey_testsecret"},
        )

    assert response.status_code == 200
    assert response.json() == {"name": "root"}


def test_the_engine_is_disposed_even_if_something_raises_while_the_app_is_running(
    settings: Settings,
) -> None:
    """The lifespan's `finally` disposes the engine even when the app body raises.

    Simulates a failure during the app's running lifetime, after startup has
    completed. Without `try`/`finally` around `yield`, an exception thrown
    into the lifespan generator at shutdown would skip `engine.dispose()`
    and leak the connection pool.
    """
    app = create_app(settings)

    async def _run() -> None:
        async with app.router.lifespan_context(app):
            raise RuntimeError("simulated failure while the app is running")

    # Recorded and still carried out: a dispose that is only mocked leaks the pool's connection.
    with (
        mock.patch.object(
            Engine, "dispose", autospec=True, side_effect=Engine.dispose
        ) as mock_dispose,
        pytest.raises(RuntimeError, match="simulated failure"),
    ):
        asyncio.run(_run())

    mock_dispose.assert_called_once()


def test_the_engine_is_disposed_when_bootstrap_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing startup bootstrap still disposes the engine."""
    disposed: list[bool] = []
    leases: list[int] = []
    real_create_engine = app_module.create_engine

    def tracking_engine(config: DatabaseConfig, *, lease_seconds: int) -> Engine:
        """Wrap the real engine so `dispose` is recorded.

        Parameters
        ----------
        config
            The database configuration.
        lease_seconds
            The lease the app passes (`LAKANA_RUN__LEASE_SECONDS`).

        Returns
        -------
        Engine
            The real engine, with `dispose` instrumented.
        """
        leases.append(lease_seconds)
        engine = real_create_engine(config, lease_seconds=lease_seconds)
        original = engine.dispose
        engine.dispose = lambda *a, **k: (disposed.append(True), original(*a, **k))[1]
        return engine

    def failing_bootstrap(*args: object, **kwargs: object) -> None:
        """Raise like a DB blip during startup.

        Parameters
        ----------
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(app_module, "create_engine", tracking_engine)
    monkeypatch.setattr(app_module, "bootstrap_if_needed", failing_bootstrap)

    with (
        pytest.raises(RuntimeError, match="database unavailable"),
        TestClient(create_app(settings)),
    ):
        pass

    assert disposed == [True]
    assert leases == [settings.run.lease_seconds]  # the keepalives follow the lease


def test_the_bootstrap_principal_exists_after_the_lifespan_runs(settings: Settings) -> None:
    """ensure_bootstrap_principal is actually called on startup, not just importable."""
    app = create_app(settings)
    with TestClient(app):
        engine = create_engine(settings.database)
        with Session(engine) as session:
            names = session.execute(select(Principal.name)).scalars().all()
        assert settings.bootstrap_admin in names
        engine.dispose()


def test_a_missing_authorization_header_is_rejected(client: TestClient) -> None:
    """A real route now exists to exercise `get_principal` end to end."""
    response = client.get("/api/v1/environments")
    assert response.status_code == 401


def test_a_well_formed_header_with_a_fake_secret_is_rejected(client: TestClient) -> None:
    """M1's shape-only check is gone: a syntactically valid PAT with the wrong secret fails."""
    response = client.get(
        "/api/v1/environments",
        headers={"Authorization": "Bearer lakana_pat_testkey_wrong-secret"},
    )
    assert response.status_code == 401


def test_the_bootstrap_pat_exists_after_the_lifespan_runs(settings: Settings) -> None:
    """bootstrap_if_needed actually mints a PAT on startup, not just importable."""
    app = create_app(settings)
    with TestClient(app):
        engine = create_engine(settings.database)
        with Session(engine) as session:
            count = session.execute(select(PersonalAccessToken)).scalars().all()
        assert len(count) == 1
        engine.dispose()


def test_bootstrap_pat_is_only_minted_on_the_run_that_needs_it(settings: Settings) -> None:
    """A second startup against the same database mints no additional PAT.

    Queries the database directly rather than `app.state`: the plaintext
    bootstrap PAT is never stashed on `app.state` (it would otherwise sit in
    process memory, reachable by any future route or debug handler, for the
    app's whole lifetime).
    """
    first_app = create_app(settings)
    with TestClient(first_app):
        pass

    second_app = create_app(settings)
    with TestClient(second_app):
        pass

    engine = create_engine(settings.database)
    with Session(engine) as session:
        count = session.execute(select(PersonalAccessToken)).scalars().all()
    assert len(count) == 1
    engine.dispose()


def test_bootstrap_logs_only_the_key_id_when_a_supplied_credential_is_honoured(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A supplied, honoured credential is never logged in full.

    `settings` fixes `bootstrap_pat_key_id`/`bootstrap_pat_secret`
    ("testkey"/"testsecret") the same way `lakana server start --local` fixes
    a bootstrap credential the CLI itself already knows -- logging it again
    in full would put a working secret in server logs for no reason. The
    auto-minted (no `LAKANA_BOOTSTRAP_PAT_*`) and collision-fallback cases
    keep logging the full token, since the caller has no other way to learn
    it (`test_restart_restores_admin_when_none_exists` pins that case).
    """
    with (
        _app_logger_enabled(),
        caplog.at_level(logging.DEBUG),
        TestClient(create_app(settings)),
    ):
        pass

    messages = [record.getMessage() for record in caplog.records]
    assert any("testkey" in message for message in messages)
    assert not any("testsecret" in message for message in messages)
    assert not any("lakana_pat_testkey_testsecret" in message for message in messages)


def test_a_normal_start_with_a_supplied_credential_logs_nothing_above_debug(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The credential the caller supplied is restored quietly: no WARNING or INFO line.

    Every fresh `lakana server start --local` takes this path, so a newcomer would see the
    line on every first start; it names nothing they can act on.
    """
    with (
        _app_logger_enabled(),
        caplog.at_level(logging.INFO),
        TestClient(create_app(settings)),
    ):
        pass

    assert [r.getMessage() for r in caplog.records if "bootstrap" in r.getMessage()] == []


def test_the_server_warns_at_start_up_only_when_old_format_secrets_are_accepted(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """One WARNING when `LAKANA_SECRETS__ACCEPT_FORMAT_1` is on; silence by default."""
    from lakana_core.config import SecretsConfig

    with (
        _app_logger_enabled(),
        caplog.at_level(logging.WARNING),
        TestClient(create_app(settings)),
    ):
        pass
    assert "ACCEPT_FORMAT_1" not in caplog.text

    on = settings.model_copy(update={"secrets": SecretsConfig(accept_format_1=True)})
    with _app_logger_enabled(), caplog.at_level(logging.WARNING), TestClient(create_app(on)):
        pass
    warnings = [r.getMessage() for r in caplog.records if "ACCEPT_FORMAT_1" in r.getMessage()]
    assert len(warnings) == 1
    assert "any old-format ciphertext" in warnings[0] and "lakana secret rotate" in warnings[0]


def _minting_settings(tmp_path: Path) -> Settings:
    """Build settings whose first start has to mint the first administrator's token.

    Parameters
    ----------
    tmp_path
        Pytest's per-test temporary directory; holds the database and the token file path.

    Returns
    -------
    Settings
        No `LAKANA_BOOTSTRAP_PAT_*`, and `bootstrap_token_file` set to `tmp_path / "token"`
        (not yet created).
    """
    settings = Settings(
        database=DatabaseConfig(url=f"sqlite:///{tmp_path}/lakana.db"),
        bootstrap_token_file=tmp_path / "token",
    )
    engine = create_engine(settings.database)
    Base.metadata.create_all(engine)
    engine.dispose()
    return settings


def test_a_minted_bootstrap_token_goes_to_a_private_file_and_never_to_the_log(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """#208: the token is in a 0600 file; the log names the file, the admin and the key id only."""
    settings = _minting_settings(tmp_path)

    with (
        _app_logger_enabled(),
        caplog.at_level(logging.DEBUG),
        TestClient(create_app(settings)) as client,
    ):
        token = (tmp_path / "token").read_text().strip()
        assert token.startswith("lakana_pat_")
        assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600
        ok = client.get("/api/v1/environments", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        key_id = token.split("_")[2]

    assert "lakana_pat_" not in caplog.text
    secret = token.split("_", 3)[3]
    assert secret not in caplog.text
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    [line] = [w for w in warnings if str(tmp_path / "token") in w]
    assert "'admin'" in line and key_id in line
    assert "delete" in line


def test_a_minting_start_without_a_token_file_fails_naming_the_setting(tmp_path: Path) -> None:
    """No supplied credential and no file path: start-up stops with a message naming the setting."""
    settings = _minting_settings(tmp_path).model_copy(update={"bootstrap_token_file": None})

    with (
        pytest.raises(ValidationError, match="LAKANA_BOOTSTRAP_TOKEN_FILE"),
        TestClient(create_app(settings)),
    ):
        pass


def test_a_half_set_credential_stops_start_up_and_logs_nothing_false(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A key id without its secret is refused; the log never claims a supplied token was used."""
    settings = _minting_settings(tmp_path).model_copy(update={"bootstrap_pat_key_id": "k"})

    with (
        _app_logger_enabled(),
        caplog.at_level(logging.DEBUG),
        pytest.raises(ValidationError, match="LAKANA_BOOTSTRAP_PAT_SECRET"),
        TestClient(create_app(settings)),
    ):
        pass

    assert "was supplied" not in caplog.text


def test_a_minting_start_refuses_an_existing_token_file(tmp_path: Path) -> None:
    """An earlier file is not overwritten; start-up stops and the file is untouched."""
    settings = _minting_settings(tmp_path)
    (tmp_path / "token").write_text("older\n")

    with (
        pytest.raises(ConflictError, match="remove it after use"),
        TestClient(create_app(settings)),
    ):
        pass

    assert (tmp_path / "token").read_text() == "older\n"


def test_a_minting_start_refuses_a_symlink_at_the_token_path(tmp_path: Path) -> None:
    """A symlink is never followed, whatever it points at."""
    settings = _minting_settings(tmp_path)
    (tmp_path / "token").symlink_to(tmp_path / "victim")

    with pytest.raises(ConflictError), TestClient(create_app(settings)):
        pass

    assert not (tmp_path / "victim").exists()


def test_lifespan_grants_the_bootstrap_admin_instance_admin(settings: Settings) -> None:
    """Starting the app grants the bootstrap admin `admin` at instance scope."""
    with TestClient(create_app(settings)) as client, Session(client.app.state.engine) as session:
        admin = session.execute(
            select(Principal).where(Principal.name == settings.bootstrap_admin)
        ).scalar_one()
        grants = (
            session.execute(select(Grant).where(Grant.principal_id == admin.id)).scalars().all()
        )
        assert any(g.role == Role.ADMIN and g.kind == SecurableKind.INSTANCE for g in grants)


def test_restart_does_not_restore_revoked_bootstrap_admin(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The lockout exploit: deliberately revoking the bootstrap admin must stick.

    Before this fix, every startup unconditionally re-granted instance
    ADMIN to the bootstrap principal and re-minted a PAT whenever it had
    none — so revoking the bootstrap credential (the documented way to
    retire it once real admins exist) was silently undone, with a fresh
    secret logged, on the very next restart. With a second active admin
    (carol) in place, restarting must change nothing about the bootstrap
    principal's access.
    """
    with TestClient(create_app(settings)) as client:
        create_carol = client.post(
            "/api/v1/principals", headers=AUTH_HEADER, json={"name": "carol", "type": "USER"}
        )
        assert create_carol.status_code == 201, create_carol.text
        grant_carol = client.post(
            "/api/v1/grants",
            headers=AUTH_HEADER,
            json={"principal": "carol", "role": "ADMIN", "kind": "INSTANCE"},
        )
        assert grant_carol.status_code == 201, grant_carol.text
        carol_token = client.post(
            "/api/v1/principals/carol/tokens", headers=AUTH_HEADER, json={"name": "carol-pat"}
        )
        assert carol_token.status_code == 201, carol_token.text
        carol_headers = {"Authorization": f"Bearer {carol_token.json()['token']}"}

        bootstrap_grants = client.get(
            "/api/v1/grants",
            headers=carol_headers,
            params={"principal_name": settings.bootstrap_admin},
        ).json()
        admin_grant_id = next(g["id"] for g in bootstrap_grants if g["role"] == "ADMIN")
        revoke_grant = client.delete(f"/api/v1/grants/{admin_grant_id}", headers=carol_headers)
        assert revoke_grant.status_code == 204, revoke_grant.text

        bootstrap_pats = client.get(
            f"/api/v1/principals/{settings.bootstrap_admin}/tokens", headers=carol_headers
        ).json()
        for pat in bootstrap_pats:
            revoke_pat = client.delete(
                f"/api/v1/principals/{settings.bootstrap_admin}/tokens/{pat['key_id']}",
                headers=carol_headers,
            )
            assert revoke_pat.status_code == 204, revoke_pat.text

    caplog.clear()
    with _app_logger_enabled(), caplog.at_level(logging.WARNING), TestClient(create_app(settings)):
        pass

    engine = create_engine(settings.database)
    with Session(engine) as session:
        admin = session.execute(
            select(Principal).where(Principal.name == settings.bootstrap_admin)
        ).scalar_one()
        active_admin_grants = (
            session.execute(
                select(Grant).where(
                    Grant.principal_id == admin.id,
                    Grant.role == Role.ADMIN,
                    Grant.kind == SecurableKind.INSTANCE,
                    Grant.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        assert active_admin_grants == []
        pats = (
            session.execute(
                select(PersonalAccessToken).where(PersonalAccessToken.principal_id == admin.id)
            )
            .scalars()
            .all()
        )
        assert pats != []
        assert all(pat.revoked_at is not None for pat in pats)
    engine.dispose()

    assert not any("lakana_pat_" in record.getMessage() for record in caplog.records)


def test_restart_restores_admin_when_none_exists(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """The recovery half: a genuine zero-admin lockout is restored on restart.

    The very first startup against an empty database also finds no active
    admin and takes this same path — recorded here as the baseline
    `BOOTSTRAP_ADMIN_RESTORED` count — so what this test actually pins is
    that a *second*, later lockout is recovered too, one more event, with a new token in a new
    file and none in the log (#208).
    """
    settings = _minting_settings(tmp_path)
    with TestClient(create_app(settings)):
        pass

    engine = create_engine(settings.database)
    with Session(engine) as session:
        admin_id = session.execute(
            select(Principal.id).where(Principal.name == settings.bootstrap_admin)
        ).scalar_one()
        baseline_events = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.BOOTSTRAP_ADMIN_RESTORED
                )
            )
            .scalars()
            .all()
        )
        assert len(baseline_events) == 1
        admin_grants = (
            session.execute(
                select(Grant).where(
                    Grant.role == Role.ADMIN,
                    Grant.kind == SecurableKind.INSTANCE,
                    Grant.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        for grant in admin_grants:
            grant.revoked_at = datetime.now(UTC)
        session.commit()
    engine.dispose()

    first_token = (tmp_path / "token").read_text().strip()
    (tmp_path / "token").unlink()
    caplog.clear()
    with (
        _app_logger_enabled(),
        caplog.at_level(logging.DEBUG),
        TestClient(create_app(settings)),
    ):
        pass

    engine = create_engine(settings.database)
    with Session(engine) as session:
        active_admin_grants = (
            session.execute(
                select(Grant).where(
                    Grant.principal_id == admin_id,
                    Grant.role == Role.ADMIN,
                    Grant.kind == SecurableKind.INSTANCE,
                    Grant.revoked_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        assert active_admin_grants != []
        events = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == AuditEventType.BOOTSTRAP_ADMIN_RESTORED
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 2
        assert all(event.principal_id == admin_id for event in events)
    engine.dispose()

    second_token = (tmp_path / "token").read_text().strip()
    assert second_token.startswith("lakana_pat_") and second_token != first_token
    assert "lakana_pat_" not in caplog.text


def test_lockout_recovery_with_a_taken_key_id_fails_start_up(settings: Settings) -> None:
    """A supplied key id an earlier token used: no token is invented, the start-up stops."""
    with TestClient(create_app(settings)):
        pass
    engine = create_engine(settings.database)
    with Session(engine) as session:
        for grant in session.execute(select(Grant)).scalars().all():
            grant.revoked_at = datetime.now(UTC)
        session.commit()
    engine.dispose()

    with pytest.raises(ConflictError, match="testkey"), TestClient(create_app(settings)):
        pass

    engine = create_engine(settings.database)
    with Session(engine) as session:
        assert len(session.execute(select(PersonalAccessToken)).scalars().all()) == 1
    engine.dispose()


def test_failed_authentication_flooding_is_bounded_per_client(settings: Settings) -> None:
    """The audit-flood exploit: repeated bad credentials from one client flood the audit log.

    Without a limiter, `get_principal` writes one `AUTHENTICATION_FAILED` row
    per attempt, so an attacker retrying a bad credential grows the audit
    log without bound. With `per_ip_per_minute=10`, 11 failed logins from
    the same client (`TestClient`'s fixed `"testclient"` host) write only 10
    rows — the 11th is still rejected with 401, but recorded nowhere. The
    app's real limiter is swapped for one built on a fake clock, so the test
    can cross the refill window deterministically instead of sleeping 60
    real seconds; the next request after that admits again, and its row
    carries `detail.suppressed == 1` for the attempt dropped in between.
    """
    limited_settings = settings.model_copy(
        update={"auth_audit": AuthAuditConfig(per_ip_per_minute=10)}
    )
    bad_header = {"Authorization": "Bearer lakana_pat_bad_wrongsecret"}

    with TestClient(create_app(limited_settings)) as client:
        clock = _FakeClock()
        client.app.state.auth_audit_limiter = AuthAuditLimiter(10, clock=clock)

        responses = [client.get("/api/v1/environments", headers=bad_header) for _ in range(11)]
        assert all(response.status_code == 401 for response in responses)

        with Session(client.app.state.engine) as session:
            failed = (
                session.execute(
                    select(AuditEvent).where(
                        AuditEvent.event_type == AuditEventType.AUTHENTICATION_FAILED
                    )
                )
                .scalars()
                .all()
            )
        assert len(failed) == 10

        clock.advance(6.0)
        response = client.get("/api/v1/environments", headers=bad_header)
        assert response.status_code == 401

        with Session(client.app.state.engine) as session:
            failed = (
                session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.event_type == AuditEventType.AUTHENTICATION_FAILED)
                    .order_by(AuditEvent.occurred_at)
                )
                .scalars()
                .all()
            )
        assert len(failed) == 11
        assert failed[-1].detail["suppressed"] == 1


def test_the_server_starts_and_warns_once_per_setting_about_a_token_like_index_path(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A path segment that looks like a token is a warning at start-up, never a refusal."""
    url = "https://europe-python.pkg.dev/my-project-123456/team-mirror/simple/"
    configured = settings.model_copy(update={"run": RunConfig(index_url=url)})
    with (
        _app_logger_enabled(),
        caplog.at_level(logging.WARNING, logger="lakana_server.app"),
        TestClient(create_app(configured)),
        TestClient(create_app(settings)),
    ):
        pass
    texts = [r.getMessage() for r in caplog.records if "LAKANA_RUN__INDEX_URL" in r.getMessage()]
    assert len(texts) == 1
    assert "my-project-123456" not in texts[0]
    assert "pkg.dev" not in texts[0]


def test_startup_names_single_action_grants_that_no_longer_work(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """One WARNING names each grant of the three actions held without the ADMIN role."""
    with TestClient(create_app(settings)) as client, Session(client.app.state.engine) as session:
        bob = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="bob", is_active=True)
        session.add(bob)
        session.flush()
        stale = Grant(
            id=uuid.uuid4(),
            action=Action.MANAGE_GRANT,
            kind=SecurableKind.INSTANCE,
            principal_id=bob.id,
            granted_by=bob.id,
            granted_at=datetime.now(UTC),
        )
        session.add(stale)
        session.commit()
        stale_id = str(stale.id)
    with (
        _app_logger_enabled(),
        caplog.at_level(logging.WARNING),
        TestClient(create_app(settings)),
    ):
        pass
    found = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(
            "1 grant(s) of MANAGE_GRANT, MANAGE_GROUP or MANAGE_TOKEN as a single action have "
            "no effect: "
        )
    ]
    assert len(found) == 1
    assert stale_id in found[0] and "principal 'bob'" in found[0]
    assert "lakana grant revoke ID" in found[0]
    assert 'read "Upgrade policy"' in found[0]


def test_startup_says_nothing_without_such_grants(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A clean instance logs no such warning."""
    with TestClient(create_app(settings)):
        pass
    with (
        _app_logger_enabled(),
        caplog.at_level(logging.WARNING),
        TestClient(create_app(settings)),
    ):
        pass
    assert not any("have no effect" in r.getMessage() for r in caplog.records)
    assert not any("Upgrade policy" in r.getMessage() for r in caplog.records)


def test_startup_goes_on_when_the_grants_cannot_be_read(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A database error while looking for such grants is logged; the server still starts."""
    from sqlalchemy.exc import OperationalError

    with (
        _app_logger_enabled(),
        caplog.at_level(logging.WARNING),
        mock.patch.object(
            app_module,
            "ineffective_grants_warning",
            side_effect=OperationalError("s", {}, Exception("x")),
        ),
        TestClient(create_app(settings)) as client,
    ):
        assert client.get("/healthz").status_code == 200
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "could not be listed at start-up" in m and "start-up continues" in m for m in messages
    )
