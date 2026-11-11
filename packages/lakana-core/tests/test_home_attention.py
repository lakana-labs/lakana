"""Needs your attention: what failed or is late, filtered by what the person may read now."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from core_test_helpers import ensure_table
from home_test_helpers import (
    check_result,
    deployed_job,
    environment,
    finished_run,
    fired_freshness,
    grant,
    person,
    query_of,
    table_read,
)
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.home.attention import (
    ATTENTION_LIMIT,
    AttentionItem,
    AttentionService,
)
from lakana_core.models import (
    AuditEvent,
    CheckOutcome,
    Environment,
    Grant,
    QueryRunStatus,
    RecentOpen,
    RunStatus,
    SignInLink,
    Table,
    Worker,
)

_LEASE = 60

_NOW: list[datetime] = []
"""The test's start, whole seconds, so a row's time and the expected one are the same moment."""


@pytest.fixture(autouse=True)
def _start() -> None:
    """Fix the moment `_ago` counts back from, once per test."""
    _NOW[:] = [datetime.now(UTC).replace(microsecond=0)]


def _ago(**delta: float) -> datetime:
    """Name a moment before the test's start.

    Parameters
    ----------
    **delta
        `timedelta` arguments.

    Returns
    -------
    datetime
        The start minus the delta, in UTC.
    """
    return _NOW[0] - timedelta(**delta)


@pytest.fixture
def svc(session: Session) -> AttentionService:
    """Provide the service with the server's auditing authorization.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    AttentionService
        The service under test.
    """
    authz = AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(1000))
    return AttentionService(session, authz, lease_seconds=_LEASE)


@pytest.fixture
def admin(session: Session) -> PrincipalRef:
    """Persist an administrator.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        Their reference.
    """
    ref = person(session, "ada")
    grant(session, ref, role=Role.ADMIN)
    return ref


@pytest.fixture
def analyst(session: Session) -> PrincipalRef:
    """Persist an analyst of `dev`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        Their reference.
    """
    ref = person(session, "alice")
    grant(session, ref, role=Role.ANALYST, environment_name="dev")
    return ref


def _late(session: Session, name: str, environment_name: str = "dev") -> uuid.UUID:
    """Persist a table in `gold` whose newest freshness row fired an hour ago.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name.
    environment_name
        Its environment.

    Returns
    -------
    uuid.UUID
        The table's id.
    """
    env_id, table_id = ensure_table(session, environment_name, "gold", name)
    fired_freshness(session, env_id, table_id, observed_at=_ago(hours=1))
    return table_id


def _opened(session: Session, who: PrincipalRef, dotted: str) -> None:
    """Record that a person opened a table of `dev`.

    Parameters
    ----------
    session
        Database session.
    who
        The person.
    dotted
        The table's dotted name.
    """
    session.add(
        RecentOpen(
            id=uuid.uuid4(),
            principal_id=who.id,
            kind="table",
            environment_id=environment(session, "dev"),
            ref=dotted,
            opened_at=_ago(minutes=5),
        )
    )
    session.commit()


def _worker(
    session: Session, worker_id: str, host: str, *, beat: datetime, started: datetime
) -> None:
    """Persist a worker row.

    Parameters
    ----------
    session
        Database session.
    worker_id
        Its id.
    host
        Its host.
    beat
        Its last heartbeat.
    started
        When it started.
    """
    session.add(
        Worker(id=worker_id, host=host, started_at=started, version="1.0.0", heartbeat_at=beat)
    )
    session.commit()


def _link(
    session: Session, who: PrincipalRef, *, expires: datetime, used: bool = False
) -> uuid.UUID:
    """Persist a recovery link for a person.

    Parameters
    ----------
    session
        Database session.
    who
        The person it signs in.
    expires
        When it expires.
    used
        Whether it was used.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    link_id = uuid.uuid4()
    session.add(
        SignInLink(
            id=link_id,
            public_id=link_id.hex[:16],
            secret_hash="0" * 64,
            kind="recovery",
            principal_id=who.id,
            created_at=_ago(hours=2),
            expires_at=expires,
            used_at=_ago(hours=1) if used else None,
        )
    )
    session.commit()
    return link_id


def test_admin_sees_a_job_whose_newest_run_failed(
    svc: AttentionService, admin: PrincipalRef, session: Session
) -> None:
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    run = finished_run(
        session, repo, version, job="nightly", status=RunStatus.FAILED, finished_at=_ago(hours=1)
    )
    whole = finished_run(
        session, repo, version, job=None, status=RunStatus.FAILED, finished_at=_ago(hours=2)
    )
    items, truncated = svc.for_caller(admin)
    assert items == [
        AttentionItem("run_failed", "dev", "nightly", str(run.id), _ago(hours=1)),
        AttentionItem("run_failed", "dev", "shop", str(whole.id), _ago(hours=2)),
    ]
    assert truncated is False


def test_a_failed_run_followed_by_a_success_is_not_listed(
    svc: AttentionService, admin: PrincipalRef, session: Session
) -> None:
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    finished_run(
        session, repo, version, job="nightly", status=RunStatus.FAILED, finished_at=_ago(hours=2)
    )
    finished_run(
        session, repo, version, job="nightly", status=RunStatus.SUCCEEDED, finished_at=_ago(hours=1)
    )
    finished_run(
        session, repo, version, job="weekly", status=RunStatus.FAILED, finished_at=_ago(days=8)
    )
    assert svc.for_caller(admin) == ([], False)


def test_admin_sees_late_tables_open_recovery_links_and_lost_workers(
    svc: AttentionService, admin: PrincipalRef, session: Session
) -> None:
    late = _late(session, "sales")
    env_id, on_time = ensure_table(session, "dev", "gold", "stock")
    fired_freshness(session, env_id, on_time, observed_at=_ago(hours=3))
    fired_freshness(session, env_id, on_time, state="ok", observed_at=_ago(hours=2))
    carol = person(session, "carol")
    open_link = _link(session, carol, expires=_ago(hours=-1))
    _link(session, carol, expires=_ago(hours=1))
    _link(session, carol, expires=_ago(hours=-1), used=True)
    unbound = _link(session, carol, expires=_ago(hours=-1))
    session.execute(update(SignInLink).where(SignInLink.id == unbound).values(principal_id=None))
    session.commit()
    _worker(session, "w-lost", "db-1", beat=_ago(minutes=10), started=_ago(days=1))
    _worker(session, "w-alive", "db-2", beat=_ago(seconds=1), started=_ago(minutes=5))
    _worker(session, "w-old", "db-2", beat=_ago(minutes=20), started=_ago(days=2))
    items, _ = svc.for_caller(admin)
    assert late
    assert items == [
        AttentionItem("worker_lost", None, "db-1", "w-lost", _ago(minutes=10)),
        AttentionItem("table_late", "dev", "gold.sales", "gold.sales", _ago(hours=1)),
        AttentionItem("recovery_link_open", None, "carol", str(open_link), _ago(hours=2)),
    ]


def test_admin_sees_the_newest_failed_or_errored_result_of_each_check(
    svc: AttentionService, admin: PrincipalRef, session: Session
) -> None:
    _, table_id = ensure_table(session, "dev", "gold", "sales")
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    run = finished_run(
        session, repo, version, job="nightly", status=RunStatus.SUCCEEDED, finished_at=_ago(hours=1)
    )
    failed = "gold.sales/not_null_id"
    check_result(
        session, run, table_id, name=failed, outcome=CheckOutcome.FAILED, finished_at=_ago(hours=1)
    )
    check_result(
        session,
        run,
        None,
        name="gold.gone/unique_id",
        outcome=CheckOutcome.ERRORED,
        finished_at=_ago(hours=2),
        error="boom",
    )
    fixed = "gold.sales/unique_id"
    earlier = finished_run(
        session, repo, version, job="nightly", status=RunStatus.SUCCEEDED, finished_at=_ago(hours=3)
    )
    check_result(
        session,
        earlier,
        table_id,
        name=fixed,
        outcome=CheckOutcome.FAILED,
        finished_at=_ago(hours=3),
    )
    check_result(
        session, run, table_id, name=fixed, outcome=CheckOutcome.PASSED, finished_at=_ago(hours=1)
    )
    items, _ = svc.for_caller(admin)
    assert items == [
        AttentionItem("check_failed", "dev", failed, str(run.id), _ago(hours=1)),
        AttentionItem("check_failed", "dev", "gold.gone/unique_id", str(run.id), _ago(hours=2)),
    ]


def test_reader_sees_only_tables_they_read_or_opened(
    svc: AttentionService, analyst: PrincipalRef, session: Session
) -> None:
    read = _late(session, "sales")
    _late(session, "stock")
    other = _late(session, "other")
    table_read(session, analyst, read, _ago(days=1))
    table_read(session, analyst, other, _ago(days=9))
    _opened(session, analyst, "gold.stock")
    _opened(session, analyst, "not-a-dotted-name")
    _opened(session, analyst, "gold.missing")
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    run = finished_run(
        session, repo, version, job="nightly", status=RunStatus.FAILED, finished_at=_ago(hours=2)
    )
    check_result(
        session,
        run,
        read,
        name="gold.sales/not_null_id",
        outcome=CheckOutcome.FAILED,
        finished_at=_ago(hours=2),
    )
    check_result(
        session,
        run,
        other,
        name="gold.other/not_null_id",
        outcome=CheckOutcome.FAILED,
        finished_at=_ago(hours=2),
    )
    items, _ = svc.for_caller(analyst)
    assert sorted((item.kind, item.title) for item in items) == [
        ("check_failed", "gold.sales/not_null_id"),
        ("table_late", "gold.sales"),
        ("table_late", "gold.stock"),
    ]


def test_attention_drops_a_table_the_person_can_no_longer_read(
    svc: AttentionService, analyst: PrincipalRef, session: Session
) -> None:
    sales = _late(session, "sales")
    stock = _late(session, "stock")
    table_read(session, analyst, sales, _ago(hours=5))
    table_read(session, analyst, stock, _ago(hours=5))
    assert len(svc.for_caller(analyst)[0]) == 2
    session.execute(delete(Table).where(Table.id == stock))
    session.commit()
    assert [item.ref for item in svc.for_caller(analyst)[0]] == ["gold.sales"]
    session.execute(
        update(Grant).where(Grant.principal_id == analyst.id).values(revoked_at=datetime.now(UTC))
    )
    session.commit()
    assert svc.for_caller(analyst) == ([], False)


def test_reader_sees_no_check_without_reading_the_environment(
    svc: AttentionService, session: Session
) -> None:
    reader = person(session, "rita")
    grant(session, reader, action=Action.READ_TABLE, environment_name="dev")
    sales = _late(session, "sales")
    table_read(session, reader, sales, _ago(hours=5))
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    run = finished_run(
        session, repo, version, job="nightly", status=RunStatus.FAILED, finished_at=_ago(hours=2)
    )
    check_result(
        session,
        run,
        sales,
        name="gold.sales/not_null_id",
        outcome=CheckOutcome.FAILED,
        finished_at=_ago(hours=2),
    )
    assert [item.kind for item in svc.for_caller(reader)[0]] == ["table_late"]


def test_reader_sees_no_reference_check_whose_target_they_may_not_read(
    svc: AttentionService, admin: PrincipalRef, session: Session
) -> None:
    reader = person(session, "tom")
    sales = _late(session, "sales")
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(
        admin,
        principal="tom",
        action=Action.READ_ENVIRONMENT,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    grants.grant(
        admin,
        principal="tom",
        role=Role.VIEWER,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace="gold",
        table="sales",
    )
    table_read(session, reader, sales, _ago(hours=5))
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    run = finished_run(
        session, repo, version, job="nightly", status=RunStatus.FAILED, finished_at=_ago(hours=2)
    )
    for name, generated in (("gold.sales/ref_store_id", True), ("gold.sales/positive", False)):
        check_result(
            session,
            run,
            sales,
            name=name,
            outcome=CheckOutcome.FAILED,
            finished_at=_ago(hours=2),
            generated=generated,
        )
    assert sorted((item.kind, item.title) for item in svc.for_caller(reader)[0]) == [
        ("check_failed", "gold.sales/positive"),
        ("table_late", "gold.sales"),
    ]


def test_reader_sees_their_own_failed_queries_not_others(
    svc: AttentionService, analyst: PrincipalRef, session: Session
) -> None:
    own = query_of(session, analyst, status=QueryRunStatus.FAILED)
    query_of(session, analyst, status=QueryRunStatus.SUCCEEDED)
    query_of(session, person(session, "bob"), status=QueryRunStatus.FAILED)
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    pipeline = finished_run(
        session, repo, version, job="nightly", status=RunStatus.FAILED, finished_at=_ago(hours=1)
    )
    query_of(session, analyst, status=QueryRunStatus.FAILED, pipeline=pipeline)
    query_of(session, analyst, "prod", status=QueryRunStatus.FAILED)
    query_of(session, analyst, "prod", status=QueryRunStatus.FAILED)
    items, _ = svc.for_caller(analyst)
    assert [(item.kind, item.environment, item.title, item.ref) for item in items] == [
        ("query_failed", "dev", f"Query {str(own.id)[:8]}", str(own.id))
    ]


def test_attention_hides_rows_of_a_deleted_environment(
    svc: AttentionService, admin: PrincipalRef, session: Session
) -> None:
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    finished_run(
        session, repo, version, job="nightly", status=RunStatus.FAILED, finished_at=_ago(hours=1)
    )
    _late(session, "sales")
    session.execute(update(Environment).values(deleted_at=datetime.now(UTC)))
    session.commit()
    assert svc.for_caller(admin) == ([], False)


def test_attention_writes_no_audit_event(
    svc: AttentionService, analyst: PrincipalRef, admin: PrincipalRef, session: Session
) -> None:
    sales = _late(session, "sales")
    _late(session, "secret", environment_name="prod")
    table_read(session, analyst, sales, _ago(hours=1))
    query_of(session, analyst, "prod", status=QueryRunStatus.FAILED)
    before = session.execute(select(func.count()).select_from(AuditEvent)).scalar()
    svc.for_caller(analyst)
    svc.for_caller(admin)
    assert session.execute(select(func.count()).select_from(AuditEvent)).scalar() == before


def test_attention_titles_carry_no_values_or_sql(
    svc: AttentionService, admin: PrincipalRef, analyst: PrincipalRef, session: Session
) -> None:
    _, table_id = ensure_table(session, "dev", "gold", "sales")
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    run = finished_run(
        session,
        repo,
        version,
        job="nightly",
        status=RunStatus.FAILED,
        finished_at=_ago(hours=1),
        error_message="value 4711 is not allowed",
    )
    check_result(
        session,
        run,
        table_id,
        name="gold.sales/no_negative",
        outcome=CheckOutcome.ERRORED,
        finished_at=_ago(hours=1),
        error="row with 4711",
    )
    table_read(session, analyst, table_id, _ago(hours=1))
    failed = query_of(session, analyst, status=QueryRunStatus.FAILED)
    failed.sql_text = "SELECT 4711"
    failed.error_message = "4711 is wrong"
    session.commit()
    shown = [*svc.for_caller(admin)[0], *svc.for_caller(analyst)[0]]
    assert {item.kind for item in shown} == {"run_failed", "check_failed", "query_failed"}
    assert not [item for item in shown if "4711" in item.title + item.ref]


def test_attention_is_cut_at_fifty_and_says_so(
    svc: AttentionService, admin: PrincipalRef, session: Session
) -> None:
    repo, version = deployed_job(session, "dev", "shop", "nightly")
    for minute in range(ATTENTION_LIMIT + 5):
        finished_run(
            session,
            repo,
            version,
            job=f"job{minute:02d}",
            status=RunStatus.FAILED,
            finished_at=_ago(minutes=minute + 1),
        )
    items, truncated = svc.for_caller(admin)
    assert (len(items), truncated, items[0].title, items[-1].title) == (
        ATTENTION_LIMIT,
        True,
        "job00",
        f"job{ATTENTION_LIMIT - 1:02d}",
    )
