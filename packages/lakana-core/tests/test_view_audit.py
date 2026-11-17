"""`ACTIVITY_VIEWED`: a privileged view of other people's activity leaves one row per window."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pydantic
import pytest
from core_test_helpers import ActionAllowList, _prepared_catalog
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from test_query_service import MANAGER, SUBMITTER, _add_run, _env, _persisted

from lakana_core.audit.views import View, ViewDetail, record_view, sessions_like
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
    SecurableKind,
    TaskScope,
)
from lakana_core.catalog.query_service import QueryService
from lakana_core.catalog.service import CatalogService
from lakana_core.config import ViewAuditConfig
from lakana_core.errors import AuditUnavailableError
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Grant,
    PrincipalType,
    QueryRunStatus,
    Table,
    TableRead,
)
from lakana_core.storage.resolver import StorageResolver

_SILVER = NamespacePath.from_parts(["silver"])
_ORDERS = TableRef("dev", _SILVER, "orders")
_DEV = Securable.environment_scope("dev")
_DETAIL = ViewDetail(filters={"status": []}, rows_shown=3, others_shown=2, distinct_others=1)
_REFUSED = "could not be recorded in the audit log"


def _views(session: Session) -> list[AuditEvent]:
    """Read the `ACTIVITY_VIEWED` rows, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The rows.
    """
    session.expire_all()
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.ACTIVITY_VIEWED)
            .order_by(AuditEvent.occurred_at)
        ).all()
    )


def _refuse_view_rows(session: Session) -> None:
    """Make the database refuse every new `ACTIVITY_VIEWED` row, as a full disk would.

    Parameters
    ----------
    session
        Database session; commits.
    """
    session.execute(
        text(
            "CREATE TRIGGER refuse_views BEFORE INSERT ON audit_events "
            "WHEN NEW.event_type = 'ACTIVITY_VIEWED' "
            "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END"
        )
    )
    session.commit()


def _record(
    session: Session,
    principal: PrincipalRef,
    *,
    view: View = View.QUERIES,
    scope: Securable = _DEV,
    key: dict[str, str] | None = None,
    detail: ViewDetail = _DETAIL,
    config: ViewAuditConfig | None = None,
) -> bool:
    """Call `record_view` with the defaults of these tests.

    Parameters
    ----------
    session
        Database session; the row is written on a session of its own.
    principal
        The viewer.
    view
        What was viewed.
    scope
        Where.
    key
        The targeting key.
    detail
        The filters and counts.
    config
        The settings; the defaults when `None`.

    Returns
    -------
    bool
        Whether a row was written.
    """
    return record_view(
        sessions_like(session),
        principal,
        view,
        scope,
        key,
        detail,
        config=config or ViewAuditConfig(),
    )


# --- the helper -------------------------------------------------------------------------------


def test_a_view_writes_one_row_with_the_counts_the_filters_and_the_key(
    session: Session, principal: PrincipalRef
) -> None:
    assert _record(session, principal, key={"submitter": "bob"}) is True
    (row,) = _views(session)
    assert row.principal_id == principal.id
    assert (row.securable_kind, row.securable_environment) == (SecurableKind.ENVIRONMENT, "dev")
    assert row.detail == {
        "view": "queries",
        "filters": {"status": []},
        "rows_shown": 3,
        "others_shown": 2,
        "distinct_others": 1,
        "targeting_key": {"submitter": "bob"},
    }


def test_the_same_view_within_the_window_writes_nothing_more(
    session: Session, principal: PrincipalRef
) -> None:
    assert _record(session, principal) is True
    other_counts = ViewDetail(
        filters={"status": ["queued"]}, rows_shown=1, others_shown=1, distinct_others=1
    )
    assert _record(session, principal, detail=other_counts) is False
    assert len(_views(session)) == 1


def test_another_key_scope_view_or_viewer_is_another_row(
    session: Session, principal: PrincipalRef
) -> None:
    bob = _persisted(session, "bob")
    _record(session, principal)
    assert _record(session, principal, key={"submitter": "bob"}) is True
    assert _record(session, principal, key={"submitter": "carl"}) is True
    assert _record(session, principal, scope=Securable.environment_scope("prod")) is True
    table = Securable.table_scope("dev", _SILVER, "orders")
    assert _record(session, principal, view=View.TABLE_READERS, scope=table) is True
    assert _record(session, principal, view=View.TABLE_READERS, scope=table) is False
    assert _record(session, bob) is True
    assert len(_views(session)) == 6


def test_a_new_window_writes_a_new_row(session: Session, principal: PrincipalRef) -> None:
    _record(session, principal)
    (first,) = _views(session)
    first.occurred_at = datetime.now(UTC) - timedelta(minutes=61)
    session.commit()
    assert _record(session, principal) is True
    assert len(_views(session)) == 2
    # a shorter window is read at once
    assert _record(session, principal, config=ViewAuditConfig(window_minutes=1)) is False


def test_the_audit_log_view_is_recorded_at_instance_scope(
    session: Session, principal: PrincipalRef
) -> None:
    instance = Securable.instance_scope()
    key = {"principal_id": str(uuid.uuid4())}
    assert _record(session, principal, view=View.AUDIT_LOG, scope=instance, key=key) is True
    assert _record(session, principal, view=View.AUDIT_LOG, scope=instance, key=key) is False
    (row,) = _views(session)
    assert row.securable_kind is SecurableKind.INSTANCE
    assert row.detail["view"] == "audit_log"


def test_nothing_of_others_shown_and_no_one_targeted_writes_nothing(
    session: Session, principal: PrincipalRef
) -> None:
    own = ViewDetail(filters={}, rows_shown=4, others_shown=0, distinct_others=0)
    assert _record(session, principal, detail=own) is False
    # a targeted probe that found nothing still says whom the viewer looked for
    assert _record(session, principal, detail=own, key={"submitter": "bob"}) is True
    assert len(_views(session)) == 1


def test_the_setting_turns_it_off(session: Session, principal: PrincipalRef) -> None:
    assert _record(session, principal, config=ViewAuditConfig(enabled=False)) is False
    assert _views(session) == []


@pytest.mark.parametrize("minutes", [0, 1441, -5])
def test_the_window_is_bounded(minutes: int) -> None:
    with pytest.raises(pydantic.ValidationError, match="window_minutes"):
        ViewAuditConfig(window_minutes=minutes)


def test_the_window_bounds_and_defaults() -> None:
    assert (ViewAuditConfig().enabled, ViewAuditConfig().window_minutes) == (True, 60)
    assert ViewAuditConfig(window_minutes=1).window_minutes == 1
    assert ViewAuditConfig(window_minutes=1440).window_minutes == 1440


def test_a_failed_write_refuses_the_read_with_the_cause(
    session: Session, principal: PrincipalRef
) -> None:
    _refuse_view_rows(session)
    with pytest.raises(AuditUnavailableError, match=_REFUSED) as raised:
        _record(session, principal)
    message = str(raised.value)
    assert "IntegrityError" in message or "OperationalError" in message
    assert "Try again in a moment" in message
    assert _views(session) == []


def test_a_principal_the_database_does_not_know_refuses_the_read(session: Session) -> None:
    # SQLite enforces the foreign key of `audit_events.principal_id` here, as Postgres does.
    stranger = PrincipalRef(id=uuid.uuid4(), name="stranger", type=PrincipalType.USER)
    with pytest.raises(AuditUnavailableError, match=r"\(IntegrityError\)"):
        _record(session, stranger)
    assert _views(session) == []


def test_a_failed_lookup_refuses_the_read(session: Session, principal: PrincipalRef) -> None:
    def broken() -> Session:
        """Fail as an unreachable database does.

        Raises
        ------
        ConnectionRefusedError
            Always.
        """
        raise ConnectionRefusedError("no route")

    with pytest.raises(AuditUnavailableError, match=_REFUSED):
        record_view(broken, principal, View.QUERIES, _DEV, None, _DETAIL, config=ViewAuditConfig())


# --- the query list ---------------------------------------------------------------------------


def _detail_text(row: AuditEvent) -> str:
    """Write a row's detail as JSON, to look for what it must not hold.

    Parameters
    ----------
    row
        An audit row.

    Returns
    -------
    str
        The detail, serialised.
    """
    return json.dumps(row.detail)


def test_a_managers_list_of_other_peoples_queries_is_recorded_once_per_window(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    bob, carl = _persisted(session, "bob"), _persisted(session, "carl")
    runs = [
        _add_run(session, env, principal),
        _add_run(session, env, bob),
        _add_run(session, env, bob, QueryRunStatus.RUNNING, started_at=datetime.now(UTC)),
        _add_run(session, env, carl, QueryRunStatus.FAILED, finished_at=datetime.now(UTC)),
        _add_run(session, env, None),
    ]
    service = QueryService(session, MANAGER)
    items, cursor = service.list_queries_page(principal, "dev", limit=2)
    # bob's running query, then the admin's queued one
    assert [item.submitter for item in items] == ["bob", "admin"] and cursor is not None
    (row,) = _views(session)
    assert (row.securable_kind, row.securable_environment) == (SecurableKind.ENVIRONMENT, "dev")
    assert row.detail == {
        "view": "queries",
        "filters": {"include_ended": False, "status": [], "origin": [], "limit": 2},
        "rows_shown": 2,
        "others_shown": 1,
        "distinct_others": 1,
        "targeting_key": None,
    }
    for name_or_id in ("bob", "carl", "admin", *(str(run.id) for run in runs), "SELECT"):
        assert name_or_id not in _detail_text(row)
    # the next page, another status filter, ended runs and polling: no new row
    service.list_queries_page(principal, "dev", limit=2, cursor=cursor)
    service.list_queries_page(principal, "dev", status=["failed"])
    service.list_queries_page(principal, "dev", include_ended=True)
    service.list_queries(principal, "dev")
    assert len(_views(session)) == 1


def test_a_managers_list_with_since_and_origin_records_them_and_the_ended_runs(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    since = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=2)))
    _add_run(session, env, bob, QueryRunStatus.FAILED, finished_at=datetime.now(UTC))
    service = QueryService(session, MANAGER)
    service.list_queries_page(principal, "dev", origin=["written", "check"], since=since)
    (row,) = _views(session)
    # `since` lists the ended runs of any age, so the page is recorded as one that includes them
    assert row.detail["filters"] == {
        "include_ended": True,
        "status": [],
        "origin": ["written", "check"],
        "since": "2026-01-02T01:04:05+00:00",
        "limit": 100,
    }


def test_a_managers_cross_environment_list_records_since_and_origin(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _add_run(session, env, _persisted(session, "bob"))
    since = datetime(2026, 1, 2, tzinfo=UTC)
    service = QueryService(session, MANAGER)
    service.list_all_queries_page(principal, origin=["written"], since=since, limit=5)
    (row,) = _views(session)
    assert row.detail["filters"] == {
        "include_ended": True,
        "status": [],
        "origin": ["written"],
        "since": "2026-01-02T00:00:00+00:00",
        "limit": 5,
    }


def test_the_deleted_submitter_counts_as_someone_else(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    _add_run(session, env, None)
    _add_run(session, env, bob)
    _add_run(session, env, principal)
    QueryService(session, MANAGER).list_queries_page(principal, "dev")
    (row,) = _views(session)
    assert (row.detail["rows_shown"], row.detail["others_shown"]) == (3, 2)
    assert row.detail["distinct_others"] == 2


def test_a_submitter_filter_on_someone_else_is_its_own_row_with_the_key(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    _add_run(session, env, bob)
    service = QueryService(session, MANAGER)
    service.list_queries_page(principal, "dev")
    service.list_queries_page(principal, "dev", submitter="bob")
    service.list_queries_page(principal, "dev", submitter="bob", status=["queued"])
    service.list_queries_page(principal, "dev", submitter="nobody")
    keys = [row.detail["targeting_key"] for row in _views(session)]
    assert keys == [None, {"submitter": "bob"}, {"submitter": "nobody"}]
    assert "submitter" not in _views(session)[1].detail["filters"]


def test_a_manager_who_sees_only_their_own_queries_writes_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    service = QueryService(session, MANAGER)
    service.list_queries_page(principal, "dev")
    _add_run(session, env, principal)
    service.list_queries_page(principal, "dev")
    _add_run(session, env, bob)
    # filtered on themselves, with someone else's query in the environment
    assert len(service.list_queries_page(principal, "dev", submitter="admin")[0]) == 1
    assert _views(session) == []


def test_a_non_manager_and_a_task_token_write_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    bob = _persisted(session, "bob")
    _add_run(session, env, bob)
    _add_run(session, env, principal)
    QueryService(session, SUBMITTER).list_queries_page(principal, "dev", submitter="bob")
    task = PrincipalRef(
        id=principal.id,
        name=principal.name,
        type=principal.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
    )
    QueryService(session, AllowAllAuthorization()).list_queries_page(task, "dev", submitter="bob")
    assert _views(session) == []


def test_an_instance_administrator_is_not_exempt(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _add_run(session, env, _persisted(session, "bob"))
    _grant_admin(session, principal)
    QueryService(session, AllowAllAuthorization()).list_queries_page(principal, "dev")
    assert len(_views(session)) == 1


def test_a_list_that_cannot_be_audited_returns_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _add_run(session, env, _persisted(session, "bob"))
    _refuse_view_rows(session)
    with pytest.raises(AuditUnavailableError, match=_REFUSED):
        QueryService(session, MANAGER).list_queries_page(principal, "dev")
    # a list that shows nobody else needs no row, so it is still served
    assert QueryService(session, SUBMITTER).list_queries_page(principal, "dev") == ([], None)


def test_the_query_list_honours_the_setting(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    env = _env(session, principal, tmp_path, "dev")
    _add_run(session, env, _persisted(session, "bob"))
    off = ViewAuditConfig(enabled=False)
    assert len(
        QueryService(session, MANAGER, view_audit=off).list_queries_page(principal, "dev")[0]
    )
    assert _views(session) == []


# --- table readers ----------------------------------------------------------------------------


def _grant_admin(session: Session, principal: PrincipalRef) -> None:
    """Give a principal the `ADMIN` role at instance scope.

    Parameters
    ----------
    session
        Database session; commits.
    principal
        The new administrator.
    """
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=principal.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
            granted_by=principal.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()


def _health_catalog(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    view_audit: ViewAuditConfig | None = None,
) -> CatalogService:
    """Build a catalog with `silver.orders`, for a caller who may see its readers.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The caller, made an instance administrator.
    view_audit
        The settings of the view audit; the defaults when `None`.

    Returns
    -------
    CatalogService
        A catalog that permits `READ_TABLE` and `MANAGE_GRANT`.
    """
    authz = ActionAllowList(Action.READ_TABLE, Action.MANAGE_GRANT)
    _prepared_catalog(session, tmp_path, authz, namespaces=[_SILVER], tables=[_ORDERS])
    _grant_admin(session, principal)
    return CatalogService(
        session, StorageResolver(session), authz, view_audit=view_audit or ViewAuditConfig()
    )


def _read_by(session: Session, who: PrincipalRef, count: int = 1) -> None:
    """Store today's reads of `silver.orders` by one principal.

    Parameters
    ----------
    session
        Database session; commits.
    who
        The reader.
    count
        How many reads.
    """
    table_id = session.scalars(select(Table.id).where(Table.name == "orders")).one()
    now = datetime.now(UTC)
    session.add(
        TableRead(
            id=uuid.uuid4(),
            table_id=table_id,
            principal_id=who.id,
            day=now.date(),
            read_count=count,
            last_read_at=now,
        )
    )
    session.commit()


def test_readers_of_a_table_are_recorded_once_per_window(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _health_catalog(session, tmp_path, principal)
    _read_by(session, principal, 2)
    _read_by(session, _persisted(session, "ada"), 5)
    _read_by(session, _persisted(session, "bo"), 1)
    health = catalog.table_health(principal, _ORDERS, days=30, now=datetime.now(UTC))
    assert health.readers is not None and len(health.readers) == 3
    (row,) = _views(session)
    assert (row.securable_kind, row.securable_table) == (SecurableKind.TABLE, "orders")
    assert row.securable_namespace == "silver"
    assert row.detail == {
        "view": "table_readers",
        "filters": {"days": 30},
        "rows_shown": 3,
        "others_shown": 2,
        "distinct_others": 2,
        "targeting_key": None,
    }
    for name in ("ada", "bo", "admin"):
        assert name not in _detail_text(row)
    catalog.table_health(principal, _ORDERS, days=7, now=datetime.now(UTC))
    assert len(_views(session)) == 1


def test_readers_that_are_only_the_caller_or_none_write_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _health_catalog(session, tmp_path, principal)
    assert catalog.table_health(principal, _ORDERS, days=30, now=datetime.now(UTC)).readers == []
    _read_by(session, principal)
    catalog.table_health(principal, _ORDERS, days=30, now=datetime.now(UTC))
    assert _views(session) == []


def test_health_without_the_readers_writes_nothing(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    authz = ActionAllowList(Action.READ_TABLE)
    catalog = _prepared_catalog(session, tmp_path, authz, namespaces=[_SILVER], tables=[_ORDERS])
    _read_by(session, _persisted(session, "ada"))
    assert catalog.table_health(principal, _ORDERS, days=30, now=datetime.now(UTC)).readers is None
    assert _views(session) == []


def test_readers_that_cannot_be_audited_are_not_returned(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _health_catalog(session, tmp_path, principal)
    _read_by(session, _persisted(session, "ada"))
    _refuse_view_rows(session)
    with pytest.raises(AuditUnavailableError, match=_REFUSED):
        catalog.table_health(principal, _ORDERS, days=30, now=datetime.now(UTC))


def test_table_readers_honour_the_setting(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    catalog = _health_catalog(session, tmp_path, principal, ViewAuditConfig(enabled=False))
    _read_by(session, _persisted(session, "ada"))
    assert catalog.table_health(principal, _ORDERS, days=30, now=datetime.now(UTC)).readers
    assert _views(session) == []
