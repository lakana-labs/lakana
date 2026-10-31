"""Direct unit tests for `lakana_server.deps`, independent of any route.

`get_principal` has its own tests in `test_app.py`, alongside the lifespan
test it is conceptually closest to. These cover the other dependencies,
each called directly with a lightweight stand-in for `Request` that
carries only the `app.state` attribute the function under test actually
reads, rather than constructing a fake ASGI `Request`.
"""

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig, ViewAuditConfig
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import Principal, PrincipalType
from lakana_server.deps import get_catalog, get_session
from tests.conftest import fake_request


def test_get_session_yields_a_working_session_and_closes_it(session: Session) -> None:
    """The yielded session runs queries against the request's engine, then is closed."""
    fake_request = cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=session.get_bind()))),
    )

    generator = get_session(fake_request)
    yielded = next(generator)
    assert yielded.execute(select(Principal)).scalars().all() == []
    assert yielded.in_transaction() is True

    assert next(generator, None) is None
    assert yielded.in_transaction() is False


def test_get_catalog_builds_a_working_catalog_service(session: Session, tmp_path: Path) -> None:
    """The service is wired with the session and storage resolution.

    `get_catalog` no longer hardcodes an authorization service — this test
    calls it directly (not through FastAPI's dependency injection, which
    is what resolves `authz` in production), so it supplies
    `AllowAllAuthorization` explicitly, to exercise the wiring without
    exercising authorization itself. `get_catalog`'s `authz` parameter is
    typed `GrantAuthorization` (the real production dependency), so the
    substitute is `cast`, matching this file's existing pattern for a
    structurally-compatible stand-in that isn't nominally the same type
    (see `fake_request` above).
    """
    catalog = get_catalog(
        session=session,
        authz=cast(GrantAuthorization, AllowAllAuthorization()),
        undo_days=3,
        expiry_warning_days=5,
        view_audit=ViewAuditConfig(window_minutes=9),
        main_language="de",
    )
    assert isinstance(catalog, CatalogService)
    assert catalog._main_language == "de"
    assert catalog._undo_days == 3
    assert catalog._expiry_warning_days == 5
    assert catalog._view_audit == ViewAuditConfig(window_minutes=9)

    # Persisted, not a bare `PrincipalRef`:
    # `create_environment` now records an `AuditEvent` whose `principal_id`
    # is a foreign key onto `principals.id`, enforced even though nullable.
    principal = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=principal.id, type=principal.type, name=principal.name))
    session.commit()
    environment = catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}"),
    )
    assert environment.name == "dev"


def test_get_grant_authorization_returns_an_auditing_wrapper(session: Session) -> None:
    """get_grant_authorization now wraps GrantAuthorization for audit logging."""
    from lakana_core.audit.authorization import AuditingAuthorization
    from lakana_server.deps import get_grant_authorization

    authz = get_grant_authorization(fake_request(), session)

    assert isinstance(authz, AuditingAuthorization)


def test_get_principal_records_authentication_succeeded(session: Session) -> None:
    """A valid PAT records AUTHENTICATION_SUCCEEDED with the presented key_id."""
    import uuid

    from lakana_core.auth import AllowAllAuthorization, PrincipalRef
    from lakana_core.identity.pats import PatService
    from lakana_core.identity.task_tokens import TaskTokenService
    from lakana_core.models import AuditEvent, AuditEventType, Principal, PrincipalType
    from lakana_server.deps import get_principal

    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin", is_active=True)
    session.add(row)
    session.commit()
    admin_ref = PrincipalRef(id=row.id, name=row.name, type=row.type)
    pats = PatService(session, AllowAllAuthorization())
    issued = pats.issue(admin_ref, principal_name="admin", name="ci")
    task_tokens = TaskTokenService(session, AllowAllAuthorization())

    principal = get_principal(fake_request(), f"Bearer {issued.token}", session, pats, task_tokens)

    assert principal.id == row.id
    events = (
        session.query(AuditEvent)
        .filter_by(event_type=AuditEventType.AUTHENTICATION_SUCCEEDED)
        .all()
    )
    assert len(events) == 1
    assert events[0].principal_id == row.id
    assert events[0].detail["key_id"] == issued.key_id


def test_get_principal_records_authentication_failed_for_a_wrong_secret(session: Session) -> None:
    """A wrong PAT secret records AUTHENTICATION_FAILED with the presented key_id."""
    import uuid

    import pytest

    from lakana_core.auth import AllowAllAuthorization, PrincipalRef
    from lakana_core.errors import AuthenticationError
    from lakana_core.identity.pats import PatService
    from lakana_core.identity.task_tokens import TaskTokenService
    from lakana_core.models import AuditEvent, AuditEventType, Principal, PrincipalType
    from lakana_server.deps import get_principal

    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin", is_active=True)
    session.add(row)
    session.commit()
    admin_ref = PrincipalRef(id=row.id, name=row.name, type=row.type)
    pats = PatService(session, AllowAllAuthorization())
    issued = pats.issue(admin_ref, principal_name="admin", name="ci")
    task_tokens = TaskTokenService(session, AllowAllAuthorization())

    with pytest.raises(AuthenticationError):
        get_principal(
            fake_request(),
            f"Bearer lakana_pat_{issued.key_id}_wrongsecret",
            session,
            pats,
            task_tokens,
        )

    events = (
        session.query(AuditEvent).filter_by(event_type=AuditEventType.AUTHENTICATION_FAILED).all()
    )
    assert len(events) == 1
    assert events[0].principal_id is None
    assert events[0].detail["key_id"] == issued.key_id


def test_key_id_is_truncated(session: Session) -> None:
    """A key_id longer than 64 characters is truncated before being recorded.

    `parse_token` places no upper bound on `key_id`'s length — it is
    everything up to the first underscore — so an attacker can present an
    arbitrarily large one. Truncating it before it is stored keeps a
    hostile key_id from growing the audit log's row size unbounded.
    """
    import pytest

    from lakana_core.auth import AllowAllAuthorization
    from lakana_core.errors import AuthenticationError
    from lakana_core.identity.pats import PatService
    from lakana_core.identity.task_tokens import TaskTokenService
    from lakana_core.models import AuditEvent, AuditEventType
    from lakana_server.deps import get_principal

    pats = PatService(session, AllowAllAuthorization())
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    huge_key_id = "k" * 10_000

    with pytest.raises(AuthenticationError):
        get_principal(
            fake_request(),
            f"Bearer lakana_pat_{huge_key_id}_secret",
            session,
            pats,
            task_tokens,
        )

    events = (
        session.query(AuditEvent).filter_by(event_type=AuditEventType.AUTHENTICATION_FAILED).all()
    )
    assert len(events) == 1
    assert events[0].detail["key_id"] == "k" * 64


def test_get_principal_records_authentication_succeeded_for_a_task_token(
    session: Session,
) -> None:
    """A valid task token records SUCCEEDED via the task-token path, not just PATs."""
    import uuid

    from lakana_core.auth import AllowAllAuthorization, PrincipalRef
    from lakana_core.identity.pats import PatService
    from lakana_core.identity.task_tokens import TaskTokenService
    from lakana_core.models import AuditEvent, AuditEventType, Principal, PrincipalType
    from lakana_server.deps import get_principal

    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="admin", is_active=True)
    session.add(row)
    session.commit()
    admin_ref = PrincipalRef(id=row.id, name=row.name, type=row.type)
    pats = PatService(session, AllowAllAuthorization())
    task_tokens = TaskTokenService(session, AllowAllAuthorization())
    minted = task_tokens.mint(
        admin_ref, "admin", environment="dev", asset_scope=["bronze.*"], ttl_seconds=3600
    )

    principal = get_principal(fake_request(), f"Bearer {minted.token}", session, pats, task_tokens)

    assert principal.id == row.id
    events = (
        session.query(AuditEvent)
        .filter_by(event_type=AuditEventType.AUTHENTICATION_SUCCEEDED)
        .all()
    )
    assert len(events) == 1
    assert events[0].detail["key_id"] == minted.key_id


def test_get_principal_records_authentication_failed_for_a_malformed_header(
    session: Session,
) -> None:
    """A header matching neither credential shape still records a failure, with an empty key_id."""
    import pytest

    from lakana_core.auth import AllowAllAuthorization
    from lakana_core.errors import AuthenticationError
    from lakana_core.identity.pats import PatService
    from lakana_core.identity.task_tokens import TaskTokenService
    from lakana_core.models import AuditEvent, AuditEventType
    from lakana_server.deps import get_principal

    pats = PatService(session, AllowAllAuthorization())
    task_tokens = TaskTokenService(session, AllowAllAuthorization())

    with pytest.raises(AuthenticationError):
        get_principal(fake_request(), "not a bearer token at all", session, pats, task_tokens)

    events = (
        session.query(AuditEvent).filter_by(event_type=AuditEventType.AUTHENTICATION_FAILED).all()
    )
    assert len(events) == 1
    assert events[0].detail["key_id"] == ""


def test_get_audit_service_builds_a_working_audit_service(session: Session) -> None:
    """get_audit_service wires the session and authz into a usable AuditService."""
    from lakana_core.audit.service import AuditService
    from lakana_core.models import AuditEventType
    from lakana_server.deps import get_audit_service

    audit = get_audit_service(
        session=session, authz=cast(GrantAuthorization, AllowAllAuthorization())
    )

    assert isinstance(audit, AuditService)
    event = audit.record(None, AuditEventType.AUTHENTICATION_FAILED, detail={"key_id": None})
    assert event.event_type == AuditEventType.AUTHENTICATION_FAILED


def test_get_principal_trusts_only_the_principal_authenticate_before_body_stored(
    session: Session,
) -> None:
    """Request state that is not a `PrincipalRef` under the private name authenticates nobody."""
    import uuid

    import pytest

    from lakana_core.auth import AllowAllAuthorization, PrincipalRef
    from lakana_core.errors import AuthenticationError
    from lakana_core.identity.pats import PatService
    from lakana_core.identity.task_tokens import TaskTokenService
    from lakana_core.models import PrincipalType
    from lakana_server.deps import PRINCIPAL_STATE_KEY, get_principal

    ref = PrincipalRef(id=uuid.uuid4(), name="stored", type=PrincipalType.USER)
    pats = PatService(session, AllowAllAuthorization())
    task_tokens = TaskTokenService(session, AllowAllAuthorization())

    request = fake_request()
    setattr(request.state, PRINCIPAL_STATE_KEY, ref)
    assert get_principal(request, None, session, pats, task_tokens) is ref

    for name, value in ((PRINCIPAL_STATE_KEY, "admin"), ("principal", ref)):
        other = fake_request()
        setattr(other.state, name, value)
        with pytest.raises(AuthenticationError):
            get_principal(other, None, session, pats, task_tokens)


def test_get_key_provider_carries_the_old_format_setting() -> None:
    """The server's key provider reads format 1 exactly when `accept_format_1` is on."""
    from lakana_core.config import SecretsConfig
    from lakana_server.deps import get_key_provider

    for accept in (False, True):
        request = cast(
            Request,
            SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        settings=SimpleNamespace(
                            secrets=SecretsConfig(master_key=None, accept_format_1=accept)
                        )
                    )
                )
            ),
        )
        assert get_key_provider(request).accepts_format_1 is accept


def test_get_query_service_uses_the_configured_lease(session: Session, tmp_path: Path) -> None:
    """A cancel judges "alive" by `LAKANA_RUN__LEASE_SECONDS`, not by a default of its own."""
    from datetime import timedelta

    from lakana_core.config import QueryLimitsConfig, RunConfig
    from lakana_core.db import db_now
    from lakana_core.models import QueryRunStatus, Worker
    from lakana_server.deps import get_query_service

    principal = PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)
    session.add(Principal(id=principal.id, type=principal.type, name=principal.name))
    session.commit()
    allow = cast(GrantAuthorization, AllowAllAuthorization())
    get_catalog(
        session=session,
        authz=allow,
        undo_days=7,
        expiry_warning_days=7,
        view_audit=ViewAuditConfig(),
        main_language="en",
    ).create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )
    request = cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(
                        run=RunConfig(lease_seconds=7, renew_seconds=2),
                        query_limits=QueryLimitsConfig(),
                        view_audit=ViewAuditConfig(),
                    )
                )
            )
        ),
    )
    service = get_query_service(request, session=session, authz=allow)
    run = service.submit_query(principal, "dev", "SELECT 1")
    beat = db_now(session) - timedelta(seconds=8)
    session.add(Worker(id="w", host="h", version="1", started_at=beat, heartbeat_at=beat))
    run.status, run.worker_id = QueryRunStatus.RUNNING, "w"
    session.commit()
    assert service.cancel_query(principal, "dev", run.id).status is QueryRunStatus.CANCELLED


def test_get_query_service_uses_the_configured_cap(session: Session) -> None:
    """The wait reasons judge the limit by `LAKANA_QUERY_LIMITS__RUNNING_PER_PRINCIPAL`."""
    from lakana_core.config import QueryLimitsConfig, RunConfig
    from lakana_server.deps import get_query_service

    allow = cast(GrantAuthorization, AllowAllAuthorization())
    for configured in (3, "auto"):
        request = cast(
            Request,
            SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        settings=SimpleNamespace(
                            run=RunConfig(lease_seconds=7, renew_seconds=2),
                            query_limits=QueryLimitsConfig(running_per_principal=configured),
                            view_audit=ViewAuditConfig(enabled=False),
                        )
                    )
                )
            ),
        )
        service = get_query_service(request, session=session, authz=allow)
        assert service._running_per_principal == configured
        assert service._view_audit == ViewAuditConfig(enabled=False)


def test_get_run_service_passes_the_configured_lease(session: Session, tmp_path: Path) -> None:
    """The run service counts a worker alive by `LAKANA_RUN__LEASE_SECONDS`, not a default."""
    from lakana_core.config import QueryLimitsConfig, RunConfig
    from lakana_core.secrets.keys import KeyProvider
    from lakana_server.deps import get_run_service

    request = cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    settings=SimpleNamespace(
                        run=RunConfig(lease_seconds=7, renew_seconds=2),
                        query_limits=QueryLimitsConfig(),
                    )
                )
            )
        ),
    )
    service = get_run_service(
        request,
        session=session,
        authz=cast(GrantAuthorization, AllowAllAuthorization()),
        key_provider=cast(KeyProvider, None),
    )
    assert service._lease_seconds == 7


def test_the_undo_period_comes_from_the_settings() -> None:
    """`get_undo_days` reads `LAKANA_TABLES__UNDO_DAYS` from the app's settings."""
    from lakana_core.config import TableUndoConfig
    from lakana_server.deps import get_undo_days

    request = cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(settings=SimpleNamespace(tables=TableUndoConfig(undo_days=2)))
            )
        ),
    )
    assert get_undo_days(request) == 2


def test_the_expiry_warning_comes_from_the_settings() -> None:
    """`get_expiry_warning_days` reads `LAKANA_TABLES__EXPIRY_WARNING_DAYS`."""
    from lakana_core.config import TableUndoConfig
    from lakana_server.deps import get_expiry_warning_days

    tables = TableUndoConfig(expiry_warning_days=4)
    request = cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(tables=tables)))
        ),
    )
    assert get_expiry_warning_days(request) == 4


def test_get_view_audit_reads_the_settings() -> None:
    """The catalog's view audit is `LAKANA_VIEW_AUDIT__*` of the running server."""
    from lakana_server.deps import get_view_audit

    configured = ViewAuditConfig(enabled=False, window_minutes=5)
    request = cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(settings=SimpleNamespace(view_audit=configured))
            )
        ),
    )
    assert get_view_audit(request) is configured


def test_the_main_language_comes_from_the_settings() -> None:
    """`get_main_language` reads `LAKANA_MAIN_LANGUAGE`."""
    from lakana_server.deps import get_main_language

    request = cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(settings=SimpleNamespace(main_language="fr-CH"))
            )
        ),
    )
    assert get_main_language(request) == "fr-CH"
