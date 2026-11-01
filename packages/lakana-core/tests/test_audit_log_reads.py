"""Reads of the audit log: who sees other people's query text, and the record of each read (#88).

A read of the audit log that shows another principal's events writes `ACTIVITY_VIEWED` (view
`audit_log`) like the other privileged views. A content field of another principal's event (the
SQL text of `QUERY_COMPLETED`) is shown only to a holder of `READ_QUERY_TEXT`, which no role
carries; everyone else sees its SHA-256 and length. `LAKANA_AUDIT__QUERY_TEXT` decides how much
text a new event stores.
"""

from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import re
import uuid
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pydantic
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from lakana_core.audit import content
from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.content import (
    CONTENT_FIELDS,
    SHOWN_FIELDS,
    WITHHELD,
    Hiding,
    UnclassifiedAuditFieldWarning,
    fingerprint,
    install_query_text,
    stored_query_text,
)
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.audit.service import AuditService
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.config import AuditConfig, Settings, ViewAuditConfig
from lakana_core.errors import AuditUnavailableError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import ROLE_ACTIONS, Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Grant,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
)
from lakana_core.runs.store import query_completed_detail

_SQL = "SELECT email FROM customers WHERE name = 'Alice Example'"
_WATERMARK = "date:2026-10-20"
_HASHED = {"sha256": hashlib.sha256(_SQL.encode("utf-8")).hexdigest(), "length": len(_SQL)}


@pytest.fixture(autouse=True)
def _full_query_text() -> Iterator[None]:
    """Leave the process's query-text setting at its default after every test.

    Yields
    ------
    None
        Control to the test.
    """
    yield
    install_query_text("full")


def _person(session: Session, name: str) -> PrincipalRef:
    """Persist a person and return a reference to it.

    Parameters
    ----------
    session
        Database session; commits.
    name
        The principal's name.

    Returns
    -------
    PrincipalRef
        The new principal.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name, is_active=True)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _grant(
    session: Session, who: PrincipalRef, *, role: Role | None = None, action: Action | None = None
) -> None:
    """Give a principal a role or one action on the instance.

    Parameters
    ----------
    session
        Database session; commits.
    who
        The grantee.
    role
        The role, or `None`.
    action
        The single action, or `None`.
    """
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=who.id,
            role=role,
            action=action,
            kind=SecurableKind.INSTANCE,
            granted_by=who.id,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()


def _query_completed(session: Session, who: PrincipalRef, sql: str = _SQL) -> None:
    """Record a `QUERY_COMPLETED` event of `who` in `dev`, as a worker does.

    Parameters
    ----------
    session
        Database session; commits.
    who
        The submitter.
    sql
        The query's text.
    """
    AuditService(session).record(
        who.id,
        AuditEventType.QUERY_COMPLETED,
        securable=Securable.environment_scope("dev"),
        detail={
            "sql_text": stored_query_text(sql),
            "tables_touched": ["gold.customers"],
            "row_count": 1,
            "status": "SUCCEEDED",
        },
        commit=True,
    )


def _views(session: Session) -> list[AuditEvent]:
    """Read the `ACTIVITY_VIEWED` rows of the audit log, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The rows whose view is `audit_log`.
    """
    session.expire_all()
    rows = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.event_type == AuditEventType.ACTIVITY_VIEWED)
        .order_by(AuditEvent.occurred_at)
    ).all()
    return [row for row in rows if row.detail.get("view") == "audit_log"]


def _sql_seen(service: AuditService, reader: PrincipalRef, owner: PrincipalRef) -> object:
    """Read the `sql_text` of `owner`'s newest `QUERY_COMPLETED` as `reader` sees it.

    Parameters
    ----------
    service
        The audit service, with the authorization to read as.
    reader
        Who reads.
    owner
        Whose event.

    Returns
    -------
    object
        The value of the field in the answer.
    """
    page = service.query(reader, event_type=AuditEventType.QUERY_COMPLETED)
    return next(e.detail["sql_text"] for e in page.items if e.principal_id == owner.id)


# --- who sees query text ----------------------------------------------------------------------


def test_no_role_carries_read_query_text_and_admin_carries_everything_else() -> None:
    for role in Role:
        assert Action.READ_QUERY_TEXT not in ROLE_ACTIONS[role]
    assert ROLE_ACTIONS[Role.ADMIN] == frozenset(Action) - {Action.READ_QUERY_TEXT}


def test_an_administrator_without_the_right_sees_the_hash_of_other_peoples_sql(
    session: Session,
) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    _query_completed(session, bob)
    seen = _sql_seen(AuditService(session, GrantAuthorization(session)), admin, bob)
    assert seen == _HASHED
    assert "Alice" not in json.dumps(seen)


def test_the_right_shows_the_text(session: Session) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    _grant(session, admin, action=Action.READ_QUERY_TEXT)
    _query_completed(session, bob)
    assert _sql_seen(AuditService(session, GrantAuthorization(session)), admin, bob) == _SQL


def test_the_right_alone_does_not_open_the_audit_log(session: Session) -> None:
    carol = _person(session, "carol")
    _grant(session, carol, action=Action.READ_QUERY_TEXT)
    with pytest.raises(PermissionDenied):
        AuditService(session, GrantAuthorization(session)).query(carol)


def test_a_reader_always_sees_their_own_sql(session: Session) -> None:
    admin = _person(session, "root")
    _grant(session, admin, role=Role.ADMIN)
    _query_completed(session, admin)
    assert _sql_seen(AuditService(session, GrantAuthorization(session)), admin, admin) == _SQL


def test_the_answer_never_changes_the_stored_row(session: Session) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    _query_completed(session, bob)
    service = AuditService(session, GrantAuthorization(session))
    page = service.query(admin, event_type=AuditEventType.QUERY_COMPLETED)
    page.items[0].detail["tables_touched"].append("changed")
    session.commit()
    session.expire_all()
    stored = session.scalars(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.QUERY_COMPLETED)
    ).one()
    assert stored.detail["sql_text"] == _SQL
    assert stored.detail["tables_touched"] == ["gold.customers"]


def test_an_event_without_a_principal_is_someone_elses(session: Session) -> None:
    admin = _person(session, "root")
    _grant(session, admin, role=Role.ADMIN)
    AuditService(session).record(
        None, AuditEventType.QUERY_COMPLETED, detail={"sql_text": _SQL}, commit=True
    )
    page = AuditService(session, GrantAuthorization(session)).query(admin)
    assert page.items[0].detail["sql_text"] == _HASHED


def test_the_right_is_asked_only_when_a_page_hides_something(session: Session) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    asked: list[Action] = []

    class _Counting(AllowAllAuthorization):
        """Permits everything and notes each `permits` question."""

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Note the question and permit.

            Parameters
            ----------
            principal
                The actor.
            action
                The action.
            securable
                The object.

            Returns
            -------
            bool
                Always `True`.
            """
            asked.append(action)
            return True

    AuditService(session).record(bob.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
    AuditService(session, _Counting()).query(admin)
    assert asked == []
    _query_completed(session, bob)
    _query_completed(session, bob)
    AuditService(session, _Counting()).query(admin)
    assert asked == [Action.READ_QUERY_TEXT]


def test_a_value_that_is_not_text_is_hashed_as_json(session: Session) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    AuditService(session).record(
        bob.id, AuditEventType.QUERY_COMPLETED, detail={"sql_text": ["a", 1]}, commit=True
    )
    AuditService(session).record(
        bob.id, AuditEventType.QUERY_COMPLETED, detail={"sql_text": None}, commit=True
    )
    page = AuditService(session, GrantAuthorization(session)).query(admin)
    shown = [e.detail["sql_text"] for e in page.items]
    assert None in shown
    assert fingerprint(json.dumps(["a", 1], sort_keys=True)) in shown


def _incremental_write(session: Session, writer: PrincipalRef) -> None:
    """Record the `TABLE_WRITE` of an incremental append whose key's highest value is an email.

    Parameters
    ----------
    session
        Database session; commits.
    writer
        Who wrote.
    """
    AuditService(session).record(
        writer.id,
        AuditEventType.TABLE_WRITE,
        securable=Securable.environment_scope("dev"),
        detail={
            "mode": "incremental",
            "target": "gold.customers",
            "snapshot_id": 7,
            "rows": 3,
            "files": 1,
            "watermark": _WATERMARK,
            "schema_diff": None,
            "run_id": None,
            "principal": writer.name,
        },
        commit=True,
    )


def _watermark_seen(session: Session, reader: PrincipalRef) -> object:
    """Read the `watermark` of the newest `TABLE_WRITE` as `reader` sees it.

    Parameters
    ----------
    session
        Database session.
    reader
        Who reads, with grant-backed authorization.

    Returns
    -------
    object
        The field as the answer has it.
    """
    page = AuditService(session, GrantAuthorization(session)).query(
        reader, event_type=AuditEventType.TABLE_WRITE
    )
    return page.items[0].detail["watermark"]


def test_a_watermark_is_withheld_without_anything_derived_from_it(session: Session) -> None:
    """#239: a watermark's values are few enough to guess, so not even a digest or length shows.

    An administrator without `READ_QUERY_TEXT` sees the marker only.
    """
    admin, writer = _person(session, "root"), _person(session, "writer")
    _grant(session, admin, role=Role.ADMIN)
    _incremental_write(session, writer)
    page = AuditService(session, GrantAuthorization(session)).query(
        admin, event_type=AuditEventType.TABLE_WRITE
    )
    detail = page.items[0].detail
    assert detail["watermark"] == WITHHELD
    answer = json.dumps([item.detail for item in page.items])
    digest = hashlib.sha256(_WATERMARK.encode("utf-8")).hexdigest()
    assert _WATERMARK not in answer
    assert "2026-10-20" not in answer
    assert digest not in answer
    assert str(len(_WATERMARK)) not in json.dumps(detail["watermark"])
    assert "sha256" not in json.dumps(detail["watermark"])


def test_an_auditor_without_read_table_sees_only_the_marker(session: Session) -> None:
    auditor, writer = _person(session, "auditor"), _person(session, "writer")
    _grant(session, auditor, action=Action.READ_AUDIT_LOG)
    _incremental_write(session, writer)
    assert _watermark_seen(session, auditor) == WITHHELD


def test_the_right_shows_a_watermark_and_the_writer_always_sees_their_own(
    session: Session,
) -> None:
    auditor, writer = _person(session, "auditor"), _person(session, "writer")
    _grant(session, auditor, action=Action.READ_AUDIT_LOG)
    _grant(session, writer, action=Action.READ_AUDIT_LOG)
    _incremental_write(session, writer)
    assert _watermark_seen(session, writer) == _WATERMARK
    _grant(session, auditor, action=Action.READ_QUERY_TEXT)
    assert _watermark_seen(session, auditor) == _WATERMARK


def test_a_stored_value_never_passes_a_withheld_field(session: Session) -> None:
    admin, writer = _person(session, "root"), _person(session, "writer")
    _grant(session, admin, role=Role.ADMIN)
    for value in (fingerprint(_WATERMARK), {"hidden": _WATERMARK}, WITHHELD, ["long:7"]):
        AuditService(session).record(
            writer.id, AuditEventType.TABLE_WRITE, detail={"watermark": value}, commit=True
        )
    AuditService(session).record(
        writer.id, AuditEventType.TABLE_WRITE, detail={"watermark": None}, commit=True
    )
    page = AuditService(session, GrantAuthorization(session)).query(admin)
    shown = [item.detail["watermark"] for item in page.items]
    assert shown.count(WITHHELD) == 4
    assert shown.count(None) == 1


# --- the classification of every field ------------------------------------------------------


def test_every_content_field_is_a_field_no_other_list_shows() -> None:
    for fields in CONTENT_FIELDS.values():
        assert not set(fields) & SHOWN_FIELDS
    assert {event: dict(fields) for event, fields in CONTENT_FIELDS.items()} == {
        AuditEventType.QUERY_COMPLETED: {"sql_text": Hiding.FINGERPRINT},
        AuditEventType.TABLE_WRITE: {"watermark": Hiding.WITHHELD},
    }


def test_a_field_nobody_classified_warns_when_written_and_is_hidden_when_read(
    session: Session,
) -> None:
    """Fails the test of any code that adds a field: the suite turns the warning into an error."""
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    with pytest.warns(UnclassifiedAuditFieldWarning, match=r"GRANT_CREATED.*'free_text'"):
        AuditService(session).record(
            bob.id,
            AuditEventType.GRANT_CREATED,
            detail={"grant_id": "x", "free_text": "secret"},
            commit=True,
        )
    page = AuditService(session, GrantAuthorization(session)).query(admin)
    # a field nobody judged may hold a value easy to guess: no digest either
    assert page.items[0].detail == {"grant_id": "x", "free_text": WITHHELD}


def test_only_a_true_fingerprint_passes_unchanged(session: Session) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    forged = {"sha256": "Alice Example", "length": 1}
    real = fingerprint("x")
    for value in (forged, real, {"sha256": real["sha256"], "length": True}):
        AuditService(session).record(
            bob.id, AuditEventType.QUERY_COMPLETED, detail={"sql_text": value}, commit=True
        )
    page = AuditService(session, GrantAuthorization(session)).query(admin)
    shown = [e.detail["sql_text"] for e in page.items]
    assert real in shown
    assert forged not in shown
    assert all(set(value) == {"sha256", "length"} for value in shown)
    assert "Alice" not in json.dumps(shown)


def test_a_classified_detail_writes_without_a_warning(session: Session) -> None:
    bob = _person(session, "bob")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _query_completed(session, bob)


def test_the_suite_refuses_an_unclassified_field() -> None:
    """`pyproject.toml` makes the warning an error for every test, so no new field slips by."""
    with pytest.raises(UnclassifiedAuditFieldWarning):
        content.warn_unclassified(AuditEventType.QUERY_COMPLETED, {"new_field": 1})


# --- how much text is stored ------------------------------------------------------------------


def _run(sql: str = _SQL) -> QueryRun:
    """Build an unsaved query run with `sql`.

    Parameters
    ----------
    sql
        Its text.

    Returns
    -------
    QueryRun
        The run.
    """
    return QueryRun(id=uuid.uuid4(), sql_text=sql, check_result_id=None)


@pytest.mark.parametrize(("mode", "stored"), [("full", _SQL), ("hash", _HASHED), ("off", None)])
def test_the_setting_decides_what_a_new_event_stores(mode: str, stored: object) -> None:
    install_query_text(mode)
    detail = query_completed_detail(
        _run(), tables_touched=[], row_count=1, status=QueryRunStatus.SUCCEEDED
    )
    assert detail["sql_text"] == stored
    assert stored_query_text(_SQL) == stored


def test_a_stored_hash_is_shown_as_it_is_and_the_right_cannot_recover_the_text(
    session: Session,
) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    _grant(session, admin, action=Action.READ_QUERY_TEXT)
    install_query_text("hash")
    _query_completed(session, bob)
    install_query_text("off")
    _query_completed(session, admin)
    service = AuditService(session, GrantAuthorization(session))
    assert _sql_seen(service, admin, bob) == _HASHED
    assert _sql_seen(service, admin, admin) is None
    _grant(session, bob, role=Role.ADMIN)
    assert _sql_seen(AuditService(session, GrantAuthorization(session)), bob, bob) == _HASHED


def test_an_unknown_mode_is_refused_by_the_installer() -> None:
    with pytest.raises(ValueError, match="full, hash or off"):
        install_query_text("partial")


def test_the_setting_is_full_by_default_and_read_in_any_letter_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.delenv("LAKANA_AUDIT__QUERY_TEXT", raising=False)
    assert Settings().audit == AuditConfig(query_text="full")
    monkeypatch.setenv("LAKANA_AUDIT__QUERY_TEXT", " Hash ")
    assert Settings().audit.query_text == "hash"


@pytest.mark.parametrize("value", ["partial", "", "x\x1b[31m"])
def test_a_bad_setting_is_refused_when_settings_load(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LAKANA_DATABASE__URL", "sqlite:///x.db")
    monkeypatch.setenv("LAKANA_AUDIT__QUERY_TEXT", value)
    with pytest.raises(pydantic.ValidationError) as caught:
        Settings()
    message = str(caught.value)
    assert "LAKANA_AUDIT__QUERY_TEXT must be full, hash or off" in message
    assert "\x1b" not in message


# --- granting the right -----------------------------------------------------------------------


def _auditing(session: Session) -> AuditingAuthorization:
    """Wrap grant-backed authorization the way the server does.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    AuditingAuthorization
        The wrapper.
    """
    return AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(30))


def test_granting_and_revoking_the_right_is_audited(session: Session) -> None:
    admin, _ = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    grants = GrantService(session, _auditing(session))
    row = grants.grant(
        admin, principal="bob", action=Action.READ_QUERY_TEXT, kind=SecurableKind.INSTANCE
    )
    grants.revoke(admin, row.id)
    events = session.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.event_type.in_([AuditEventType.GRANT_CREATED, AuditEventType.GRANT_REVOKED])
        )
        .order_by(AuditEvent.occurred_at)
    ).all()
    assert [(e.event_type, e.detail["action"]) for e in events] == [
        (AuditEventType.GRANT_CREATED, "READ_QUERY_TEXT"),
        (AuditEventType.GRANT_REVOKED, "READ_QUERY_TEXT"),
    ]


@pytest.mark.parametrize("kind", [SecurableKind.ENVIRONMENT])
def test_the_right_is_granted_on_the_instance_only(session: Session, kind: SecurableKind) -> None:
    admin, _ = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    with pytest.raises(ValidationError, match="--kind INSTANCE") as caught:
        GrantService(session, _auditing(session)).grant(
            admin, principal="bob", action=Action.READ_QUERY_TEXT, kind=kind, environment="dev"
        )
    assert "READ_QUERY_TEXT" in str(caught.value)


# --- every read of other people's events is recorded --------------------------------------


def _read(
    session: Session,
    reader: PrincipalRef,
    *,
    principal_id: uuid.UUID | None = None,
    config: ViewAuditConfig | None = None,
    limit: int = 100,
) -> int:
    """Read the audit log as `reader` with grant-backed authorization.

    Parameters
    ----------
    session
        Database session.
    reader
        Who reads.
    principal_id
        The `principal_id` filter.
    config
        `LAKANA_VIEW_AUDIT__*`; the defaults when `None`.
    limit
        The page size.

    Returns
    -------
    int
        How many events the page holds.
    """
    service = AuditService(session, GrantAuthorization(session), view_audit=config)
    return len(service.query(reader, principal_id=principal_id, limit=limit).items)


def test_a_read_that_shows_other_peoples_events_is_recorded_once_per_window(
    session: Session,
) -> None:
    admin, bob, carol = _person(session, "root"), _person(session, "bob"), _person(session, "c")
    _grant(session, admin, role=Role.ADMIN)
    _query_completed(session, bob)
    _query_completed(session, carol)
    _query_completed(session, carol)
    assert _read(session, admin, limit=3) == 3
    [row] = _views(session)
    assert row.principal_id == admin.id
    assert row.securable_kind == SecurableKind.INSTANCE
    assert row.detail == {
        "view": "audit_log",
        "filters": {
            "event_type": None,
            "kind": None,
            "environment": None,
            "since": None,
            "until": None,
            "limit": 3,
        },
        "rows_shown": 3,
        "others_shown": 3,
        "distinct_others": 2,
        "targeting_key": None,
    }
    assert _read(session, admin) >= 3  # the next read in the window writes nothing more
    assert len(_views(session)) == 1


def test_a_filter_on_someone_else_is_its_own_row_even_when_empty(session: Session) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    _read(session, admin, principal_id=bob.id)
    [row] = _views(session)
    assert row.detail["targeting_key"] == {"principal_id": str(bob.id)}
    assert row.detail["rows_shown"] == 0
    _read(session, admin, principal_id=bob.id)
    assert len(_views(session)) == 1
    _query_completed(session, bob)
    _read(session, admin)
    assert len(_views(session)) == 2


def test_a_read_of_only_your_own_events_records_nothing(session: Session) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    _query_completed(session, admin)
    _query_completed(session, bob)
    assert _read(session, admin, principal_id=admin.id) == 1
    assert _views(session) == []
    session.execute(delete(AuditEvent).where(AuditEvent.principal_id != admin.id))
    session.commit()
    assert _read(session, admin) == 1
    assert _views(session) == []


def test_an_instance_administrator_is_recorded_like_anyone_else(session: Session) -> None:
    admin, auditor, bob = _person(session, "root"), _person(session, "aud"), _person(session, "b")
    _grant(session, admin, role=Role.ADMIN)
    _grant(session, auditor, action=Action.READ_AUDIT_LOG)
    _query_completed(session, bob)
    _read(session, admin)
    _read(session, auditor)
    assert {row.principal_id for row in _views(session)} == {admin.id, auditor.id}


def test_a_read_that_cannot_be_recorded_returns_nothing(session: Session) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    _query_completed(session, bob)
    session.execute(
        text(
            "CREATE TRIGGER refuse_views BEFORE INSERT ON audit_events "
            "WHEN NEW.event_type = 'ACTIVITY_VIEWED' "
            "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END"
        )
    )
    session.commit()
    with pytest.raises(AuditUnavailableError, match="could not be recorded in the audit log"):
        _read(session, admin)
    # a read that shows nobody else needs no row, so it is still served
    assert _read(session, admin, principal_id=admin.id) == 0


def test_the_view_audit_setting_turns_it_off(session: Session) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    _query_completed(session, bob)
    assert _read(session, admin, config=ViewAuditConfig(enabled=False)) == 1
    assert _views(session) == []


def test_the_filters_of_a_read_are_recorded_as_given(session: Session) -> None:
    admin, bob = _person(session, "root"), _person(session, "bob")
    _grant(session, admin, role=Role.ADMIN)
    _query_completed(session, bob)
    since = datetime(2026, 1, 1, tzinfo=UTC)
    until = datetime.now(UTC) + timedelta(days=1)
    AuditService(session, GrantAuthorization(session)).query(
        admin,
        event_type=AuditEventType.QUERY_COMPLETED,
        environment="dev",
        since=since,
        until=until,
        limit=5,
    )
    [row] = _views(session)
    assert row.detail["filters"] == {
        "event_type": "QUERY_COMPLETED",
        "kind": None,
        "environment": "dev",
        "since": since.isoformat(),
        "until": until.isoformat(),
        "limit": 5,
    }


def test_the_next_cursor_points_after_the_last_event_shown(session: Session) -> None:
    admin = _person(session, "root")
    service = AuditService(session, AllowAllAuthorization())
    for _ in range(3):
        service.record(admin.id, AuditEventType.AUTHENTICATION_SUCCEEDED, commit=True)
    first = service.query(admin, limit=2)
    assert first.next_cursor is not None
    assert (first.next_cursor.occurred_at, first.next_cursor.id) == (
        first.items[-1].occurred_at,
        first.items[-1].id,
    )
    rest = service.query(admin, limit=2, cursor=first.next_cursor)
    assert len(rest.items) == 1
    assert rest.next_cursor is None


def test_a_connection_tests_id_is_shown() -> None:
    assert "test_id" in SHOWN_FIELDS


def test_each_group_of_shown_fields_is_in_alphabetical_order() -> None:
    """A reviewer looks a field up by name: only the start of a new group breaks the order."""
    source = inspect.getsource(content)
    start = source.index("SHOWN_FIELDS: frozenset[str] = frozenset(")
    listed = re.findall(r'^\s+"([a-z0-9_]+)",$', source[start : source.index("\n)\n", start)], re.M)
    assert set(listed) == SHOWN_FIELDS
    breaks = [(a, b) for a, b in itertools.pairwise(listed) if a > b]
    # The first field of the groups "states, outcomes and fixed reasons", "counts, sizes and
    # times" and "settings and changes".
    assert breaks == [
        ("waits_for_run_id", "already_deleted"),
        ("why", "after"),
        ("versions_opened", "approved_options"),
    ]
