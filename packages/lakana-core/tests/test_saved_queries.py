"""Tests for `SavedQueryService`: who sees, changes and deletes a saved query (rulings S1 to S7)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from home_test_helpers import grant, person
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, Securable, TaskScope
from lakana_core.catalog.location_sweep import sweep_table_locations
from lakana_core.catalog.saved_queries import (
    SAVED_QUERY_LIST_DEFAULT,
    SAVED_QUERY_LIST_MAX,
    SAVED_QUERY_NAME_MAX,
    SavedQueryService,
    SavedQueryView,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.config import StorageConfig
from lakana_core.errors import (
    AlreadyExistsError,
    AuditUnavailableError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Grant,
    Principal,
    SavedQuery,
    SavedQueryVisibility,
)
from lakana_core.storage.resolver import StorageResolver

_SAVED_EVENTS = (
    AuditEventType.SAVED_QUERY_CREATED,
    AuditEventType.SAVED_QUERY_UPDATED,
    AuditEventType.SAVED_QUERY_DELETED,
)
_UPDATE_REFUSED = (
    "Only the owner of a saved query can change it; save a copy under your own name instead."
)
_DELETE_REFUSED = "Only the owner of a saved query or an administrator can delete it."
_TASK_REFUSED = (
    "Saved queries belong to people and service accounts; a task's token cannot read or change "
    "them."
)
_NAME_REFUSED = "A saved query's name is one line of 1 to 200 visible characters."


def _env(catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, name: str) -> None:
    catalog.create_environment(
        principal,
        name=name,
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path / name}", options={}),
    )


def _service(session: Session) -> SavedQueryService:
    return SavedQueryService(session, GrantAuthorization(session))


def _querier(session: Session, name: str, *environments: str) -> PrincipalRef:
    who = person(session, name)
    for environment in environments:
        grant(session, who, action=Action.SUBMIT_QUERY, environment_name=environment)
    return who


def _admin(session: Session, name: str = "root") -> PrincipalRef:
    who = person(session, name)
    grant(session, who, role=Role.ADMIN)
    return who


def _views(session: Session) -> list[AuditEvent]:
    return list(
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.ACTIVITY_VIEWED)
            .order_by(AuditEvent.occurred_at)
        ).scalars()
    )


def _names(items: Sequence[SavedQueryView]) -> list[str]:
    return [item.name for item in items]


def _saved_events(session: Session) -> list[AuditEvent]:
    return list(
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type.in_(_SAVED_EVENTS))
        ).scalars()
    )


@pytest.fixture
def dev(catalog: CatalogService, principal: PrincipalRef, tmp_path: Path) -> str:
    _env(catalog, principal, tmp_path, "dev")
    return "dev"


def test_the_limits_are_the_rulings(session: Session) -> None:
    assert (SAVED_QUERY_NAME_MAX, SAVED_QUERY_LIST_DEFAULT, SAVED_QUERY_LIST_MAX) == (200, 50, 200)
    assert [v.value for v in SavedQueryVisibility] == ["private", "everyone"]


def test_create_then_get_returns_the_owners_view_with_sql_and_both_actions(
    session: Session, dev: str
) -> None:
    bob = _querier(session, "bob", dev)
    service = _service(session)

    created = service.create(bob, dev, name="Revenue", sql="SELECT 1\n")
    got = service.get(bob, dev, created.id)

    assert got == created
    assert got.environment == "dev"
    assert got.name == "Revenue"
    assert got.sql == "SELECT 1\n"
    assert got.visibility == "private"
    assert got.owner == "bob"
    assert got.former_owner is False
    assert got.actions == ("update", "delete")
    row = session.get(SavedQuery, created.id)
    assert row is not None
    assert (row.owner_id, row.private_owner_id) == (bob.id, bob.id)


def test_create_needs_submit_query_on_the_environment(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, dev: str
) -> None:
    _env(catalog, principal, tmp_path, "prod")
    bob = _querier(session, "bob", "prod")

    with pytest.raises(PermissionDenied):
        _service(session).create(bob, dev, name="Revenue", sql="SELECT 1")
    with pytest.raises(NotFoundError, match="No environment named 'nowhere'"):
        _service(session).create(_admin(session), "nowhere", name="Revenue", sql="SELECT 1")
    with pytest.raises(NotFoundError, match="No environment named 'nowhere'"):
        _service(session).create(bob, "nowhere", name="Revenue", sql="SELECT 1")
    assert session.execute(select(SavedQuery)).first() is None


def test_create_refuses_empty_sql_and_an_unknown_visibility(session: Session, dev: str) -> None:
    bob = _querier(session, "bob", dev)
    service = _service(session)

    with pytest.raises(ValidationError, match="empty"):
        service.create(bob, dev, name="Revenue", sql=" \n\t ")
    with pytest.raises(ValidationError, match="'private' or 'everyone'"):
        service.create(bob, dev, name="Revenue", sql="SELECT 1", visibility="team")


def test_sql_with_a_nul_character_is_refused(session: Session, dev: str) -> None:
    bob = _querier(session, "bob", dev)
    service = _service(session)
    kept = service.create(bob, dev, name="Revenue", sql="SELECT 1")

    for refused in (
        lambda: service.create(bob, dev, name="Nul", sql="SELECT '\x00'"),
        lambda: service.update(
            bob, dev, kept.id, name="Revenue", sql="SELECT 1\x00", visibility="private"
        ),
    ):
        with pytest.raises(ValidationError) as raised:
            refused()
        assert str(raised.value) == "A saved query's SQL cannot hold a NUL character."
    assert service.get(bob, dev, kept.id).sql == "SELECT 1"


def test_a_private_query_is_404_for_another_person_who_may_query(
    session: Session, dev: str
) -> None:
    bob = _querier(session, "bob", dev)
    carol = _querier(session, "carol", dev)
    service = _service(session)
    mine = service.create(bob, dev, name="Revenue", sql="SELECT 1")

    with pytest.raises(NotFoundError) as hidden:
        service.get(carol, dev, mine.id)
    with pytest.raises(NotFoundError) as missing:
        service.get(carol, dev, uuid.uuid4())

    assert str(hidden.value) == f"No saved query {mine.id} in 'dev'."
    assert str(missing.value).startswith("No saved query ")
    assert service.list_page(carol, environment=dev).items == []
    assert service.list_page(carol).items == []


def test_an_everyone_query_is_seen_with_sql_by_those_who_may_query_there_only(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, dev: str
) -> None:
    _env(catalog, principal, tmp_path, "prod")
    bob = _querier(session, "bob", dev)
    carol = _querier(session, "carol", dev)
    dave = _querier(session, "dave", "prod")
    service = _service(session)
    shared = service.create(bob, dev, name="Revenue", sql="SELECT 1", visibility="everyone")

    seen = service.get(carol, dev, shared.id)
    assert (seen.sql, seen.owner, seen.actions) == ("SELECT 1", "bob", ())
    assert _names(service.list_page(carol, environment=dev).items) == ["Revenue"]
    assert _names(service.list_page(carol).items) == ["Revenue"]

    with pytest.raises(NotFoundError):
        service.get(dave, dev, shared.id)
    with pytest.raises(PermissionDenied):
        service.list_page(dave, environment=dev)
    assert service.list_page(dave).items == []


def test_an_admin_sees_a_private_query_without_sql_and_may_only_delete(
    session: Session, dev: str
) -> None:
    bob = _querier(session, "bob", dev)
    root = _admin(session)
    service = _service(session)
    mine = service.create(bob, dev, name="Revenue", sql="SELECT 1")

    seen = service.get(root, dev, mine.id)
    assert (seen.name, seen.owner, seen.environment) == ("Revenue", "bob", "dev")
    assert seen.sql is None
    assert seen.actions == ("delete",)
    for page in (service.list_page(root, environment=dev), service.list_page(root)):
        assert [(i.id, i.sql, i.actions) for i in page.items] == [(mine.id, None, ("delete",))]

    with pytest.raises(PermissionDenied, match="Only the owner"):
        service.update(root, dev, mine.id, name="Mine now", sql="SELECT 2", visibility="private")
    service.delete(root, dev, mine.id)
    assert session.get(SavedQuery, mine.id) is None


def test_an_admin_with_read_query_text_sees_the_sql(session: Session, dev: str) -> None:
    bob = _querier(session, "bob", dev)
    root = _admin(session)
    grant(session, root, action=Action.READ_QUERY_TEXT)
    service = _service(session)
    mine = service.create(bob, dev, name="Revenue", sql="SELECT 1")

    assert service.get(root, dev, mine.id).sql == "SELECT 1"
    assert [i.sql for i in service.list_page(root).items] == ["SELECT 1"]


def test_an_admins_read_of_someone_elses_private_query_is_audited_without_sql(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, dev: str
) -> None:
    _env(catalog, principal, tmp_path, "prod")
    bob = _querier(session, "bob", dev, "prod")
    service = _service(session)
    mine = service.create(bob, dev, name="Revenue", sql="SELECT secret")
    service.create(bob, "prod", name="Costs", sql="SELECT secret")
    one, listed, every = _admin(session, "one"), _admin(session, "listed"), _admin(session, "every")

    service.get(one, dev, mine.id)
    service.get(one, dev, mine.id)
    service.list_page(listed, environment=dev, limit=10)
    service.list_page(every)

    rows = _views(session)
    assert [(r.principal_id, r.securable_environment) for r in rows] == [
        (one.id, "dev"),
        (listed.id, "dev"),
        (every.id, "dev"),
        (every.id, "prod"),
    ]
    assert rows[0].detail == {
        "view": "saved_queries",
        "filters": {},
        "rows_shown": 1,
        "others_shown": 1,
        "distinct_others": 1,
        "targeting_key": None,
    }
    assert rows[1].detail["filters"] == {"limit": 10}
    assert all(r.securable_kind == Securable.environment_scope("dev").kind for r in rows)
    assert not any("secret" in str(r.detail) or "Revenue" in str(r.detail) for r in rows)


def test_reads_without_someone_elses_private_query_are_not_audited(
    session: Session, dev: str
) -> None:
    bob = _querier(session, "bob", dev)
    carol = _querier(session, "carol", dev)
    root = _admin(session)
    grant(session, root, action=Action.SUBMIT_QUERY, environment_name=dev)
    service = _service(session)
    mine = service.create(bob, dev, name="Revenue", sql="SELECT 1")
    shared = service.create(carol, dev, name="Costs", sql="SELECT 2", visibility="everyone")
    own = service.create(root, dev, name="Own", sql="SELECT 3")

    service.get(bob, dev, mine.id)
    service.list_page(bob, environment=dev)
    service.list_page(bob)
    service.get(root, dev, shared.id)
    service.get(root, dev, own.id)
    # The first page holds only the shared query; Revenue, bob's private one, is on a later page.
    page = service.list_page(root, environment=dev, limit=2)
    assert _names(page.items) == ["Costs", "Own"]
    assert _views(session) == []


def test_an_admins_read_that_cannot_be_audited_returns_nothing(session: Session, dev: str) -> None:
    bob = _querier(session, "bob", dev)
    root = _admin(session)
    service = _service(session)
    mine = service.create(bob, dev, name="Revenue", sql="SELECT 1")
    session.execute(
        text(
            "CREATE TRIGGER refuse_views BEFORE INSERT ON audit_events "
            "WHEN NEW.event_type = 'ACTIVITY_VIEWED' "
            "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END"
        )
    )
    session.commit()

    with pytest.raises(AuditUnavailableError, match="could not be recorded in the audit log"):
        service.get(root, dev, mine.id)
    with pytest.raises(AuditUnavailableError):
        service.list_page(root, environment=dev)
    with pytest.raises(AuditUnavailableError):
        service.list_page(root)
    assert service.get(bob, dev, mine.id).sql == "SELECT 1"


def test_only_the_owner_updates_and_only_owner_or_admin_deletes(session: Session, dev: str) -> None:
    bob = _querier(session, "bob", dev)
    carol = _querier(session, "carol", dev)
    service = _service(session)
    shared = service.create(bob, dev, name="Revenue", sql="SELECT 1", visibility="everyone")

    with pytest.raises(PermissionDenied) as updated:
        service.update(carol, dev, shared.id, name="Revenue", sql="SELECT 2", visibility="private")
    with pytest.raises(PermissionDenied) as deleted:
        service.delete(carol, dev, shared.id)
    assert str(updated.value) == _UPDATE_REFUSED
    assert str(deleted.value) == _DELETE_REFUSED

    changed = service.update(
        bob, dev, shared.id, name="Revenue by month", sql="SELECT 2", visibility="private"
    )
    assert (changed.name, changed.sql, changed.visibility) == (
        "Revenue by month",
        "SELECT 2",
        "private",
    )
    row = session.get(SavedQuery, shared.id)
    assert row is not None and row.private_owner_id == bob.id
    shared_again = service.update(
        bob, dev, shared.id, name="Revenue by month", sql="SELECT 2", visibility="everyone"
    )
    assert shared_again.visibility == "everyone"
    session.refresh(row)
    assert row.private_owner_id is None
    service.delete(bob, dev, shared.id)
    with pytest.raises(NotFoundError):
        service.get(bob, dev, shared.id)


def test_an_owner_without_the_right_still_sees_and_deletes_but_cannot_update(
    session: Session, dev: str
) -> None:
    bob = _querier(session, "bob", dev)
    carol = _querier(session, "carol", dev)
    service = _service(session)
    shared = service.create(bob, dev, name="Revenue", sql="SELECT 1", visibility="everyone")
    private = service.create(bob, dev, name="Draft", sql="SELECT 2")
    session.execute(delete(Grant).where(Grant.principal_id == bob.id))
    session.commit()
    service = _service(session)

    assert service.get(carol, dev, shared.id).owner == "bob"
    assert service.get(bob, dev, shared.id).sql == "SELECT 1"
    assert _names(service.list_page(bob, environment=dev).items) == ["Draft", "Revenue"]
    assert _names(service.list_page(bob).items) == ["Draft", "Revenue"]
    assert service.get(bob, dev, private.id).actions == ("delete",)
    with pytest.raises(PermissionDenied):
        service.update(bob, dev, shared.id, name="Revenue", sql="SELECT 3", visibility="everyone")

    service.delete(bob, dev, private.id)
    assert _names(service.list_page(bob).items) == ["Revenue"]
    grant(session, bob, action=Action.SUBMIT_QUERY, environment_name=dev)
    assert service.get(bob, dev, shared.id).actions == ("update", "delete")
    assert (
        service.update(
            bob, dev, shared.id, name="Revenue", sql="SELECT 3", visibility="everyone"
        ).sql
        == "SELECT 3"
    )


def test_deleting_a_principal_removes_their_private_queries_and_keeps_shared_ones_as_former_owner(
    session: Session, dev: str
) -> None:
    bob = _querier(session, "bob", dev)
    carol = _querier(session, "carol", dev)
    root = _admin(session)
    service = _service(session)
    private = service.create(bob, dev, name="Draft", sql="SELECT 1")
    shared = service.create(bob, dev, name="Revenue", sql="SELECT 2", visibility="everyone")
    session.execute(delete(Grant).where(Grant.principal_id == bob.id))
    session.execute(delete(Principal).where(Principal.id == bob.id))
    session.commit()
    session.expire_all()

    assert session.get(SavedQuery, private.id) is None
    for who, actions in ((root, ("delete",)), (carol, ())):
        seen = service.get(who, dev, shared.id)
        assert (seen.owner, seen.former_owner, seen.actions, seen.sql) == (
            None,
            True,
            actions,
            "SELECT 2",
        )
        assert [i.id for i in service.list_page(who).items] == [shared.id]
    with pytest.raises(PermissionDenied):
        service.delete(carol, dev, shared.id)
    with pytest.raises(PermissionDenied):
        service.update(root, dev, shared.id, name="Revenue", sql="SELECT 3", visibility="everyone")
    service.delete(root, dev, shared.id)
    assert session.get(SavedQuery, shared.id) is None


def test_a_deleted_environment_hides_its_saved_queries_until_restored(
    session: Session, catalog: CatalogService, principal: PrincipalRef, dev: str
) -> None:
    bob = _querier(session, "bob", dev)
    service = _service(session)
    mine = service.create(bob, dev, name="Revenue", sql="SELECT 1", visibility="everyone")

    catalog.delete_environment(principal, dev)
    with pytest.raises(NotFoundError):
        service.get(bob, dev, mine.id)
    with pytest.raises(NotFoundError):
        service.list_page(bob, environment=dev)
    assert service.list_page(bob).items == []
    with pytest.raises(NotFoundError):
        service.delete(bob, dev, mine.id)

    catalog.restore_environment(principal, dev)
    assert service.get(bob, dev, mine.id) == mine
    assert [i.id for i in service.list_page(bob).items] == [mine.id]


def test_purging_an_environment_removes_its_saved_queries(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, dev: str
) -> None:
    """A purge removes an environment's saved queries for good, and no other environment's.

    The removal of the environment row cascades to them; SQLite enforces the cascade only with
    `PRAGMA foreign_keys=ON`, which every Lakana engine sets.
    """
    _env(catalog, principal, tmp_path, "prod")
    bob = _querier(session, "bob", dev, "prod")
    service = _service(session)
    service.create(bob, dev, name="Draft", sql="SELECT 1")
    service.create(bob, dev, name="Revenue", sql="SELECT 2", visibility="everyone")
    elsewhere = service.create(bob, "prod", name="Revenue", sql="SELECT 3")

    deleted = catalog.delete_environment(principal, dev, purge=True)
    assert deleted.purge_after is not None
    sweep_table_locations(
        session, StorageResolver(session), now=deleted.purge_after, grace_hours=24
    )
    session.expire_all()

    left = session.execute(select(SavedQuery.id)).scalars().all()
    assert left == [elsewhere.id]
    assert [i.id for i in service.list_page(bob).items] == [elsewhere.id]


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("a\nb", None),
        ("a\u200bb", None),
        ("x" * 201, None),
        ("  Monthly revenue  ", "Monthly revenue"),
    ],
    ids=["line-break", "zero-width-space", "201-characters", "surrounding-spaces"],
)
def test_names_are_one_visible_line_and_unique_per_owner(
    session: Session, dev: str, typed: str, stored: str | None
) -> None:
    bob = _querier(session, "bob", dev)
    carol = _querier(session, "carol", dev)
    service = _service(session)

    if stored is None:
        with pytest.raises(ValidationError) as refused:
            service.create(bob, dev, name=typed, sql="SELECT 1")
        assert str(refused.value) == _NAME_REFUSED
        kept = service.create(bob, dev, name="Kept", sql="SELECT 1")
        with pytest.raises(ValidationError, match="one line"):
            service.update(bob, dev, kept.id, name=typed, sql="SELECT 1", visibility="private")
        return

    first = service.create(bob, dev, name=typed, sql="SELECT 1")
    assert first.name == stored
    with pytest.raises(AlreadyExistsError) as duplicate:
        service.create(bob, dev, name=stored, sql="SELECT 2", visibility="everyone")
    assert str(duplicate.value) == (
        f"You already have a saved query named {stored!r} in 'dev' ({first.id}); choose another "
        "name, or update that one."
    )
    other = service.create(bob, dev, name="Other", sql="SELECT 3")
    with pytest.raises(AlreadyExistsError):
        service.update(bob, dev, other.id, name=typed, sql="SELECT 3", visibility="private")
    assert service.create(carol, dev, name=stored, sql="SELECT 4").name == stored
    assert service.update(
        bob, dev, first.id, name=stored, sql="SELECT 5", visibility="private"
    ).sql == ("SELECT 5")


def test_a_concurrent_duplicate_found_at_the_flush_is_the_same_409(
    session: Session, dev: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bob = _querier(session, "bob", dev)
    service = _service(session)
    rival = _service(session)
    rivals: list[SavedQueryView] = []
    real = service._refuse_duplicate

    def miss_once(*args: object, **kwargs: object) -> None:
        # The other request commits its row after this one's check and before its flush.
        monkeypatch.setattr(service, "_refuse_duplicate", real)
        rivals.append(rival.create(bob, dev, name="Racy", sql="SELECT 1"))

    monkeypatch.setattr(service, "_refuse_duplicate", miss_once)

    with pytest.raises(AlreadyExistsError) as raced:
        service.create(bob, dev, name="Racy", sql="SELECT 2")

    assert str(raced.value) == (
        f"You already have a saved query named 'Racy' in 'dev' ({rivals[0].id}); choose another "
        "name, or update that one."
    )
    assert session.execute(select(SavedQuery.id)).scalars().all() == [rivals[0].id]

    # The same race on an update ends the same way.
    other = service.create(bob, dev, name="Other", sql="SELECT 3")
    rival.update(bob, dev, rivals[0].id, name="Renamed", sql="SELECT 1", visibility="private")
    renamed_id = rivals[0].id
    real = service._refuse_duplicate

    def miss_update(*args: object, **kwargs: object) -> None:
        monkeypatch.setattr(service, "_refuse_duplicate", real)
        rival.update(bob, dev, renamed_id, name="Contested", sql="SELECT 1", visibility="private")

    monkeypatch.setattr(service, "_refuse_duplicate", miss_update)
    with pytest.raises(AlreadyExistsError, match=f"named 'Contested' in 'dev' \\({renamed_id}\\)"):
        service.update(bob, dev, other.id, name="Contested", sql="SELECT 3", visibility="private")


def test_an_integrity_error_that_is_not_a_duplicate_is_raised_as_it_is(
    session: Session, dev: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Bogus:
        value = "bogus"  # breaks the `private_owner` check constraint at the flush

    bob = _querier(session, "bob", dev)
    monkeypatch.setattr(
        "lakana_core.catalog.saved_queries._checked_fields",
        lambda name, sql, visibility: (name, _Bogus()),
    )

    with pytest.raises(IntegrityError, match="private_owner"):
        _service(session).create(bob, dev, name="Odd", sql="SELECT 1")


def test_the_cross_environment_list_orders_by_name_and_pages_every_item_once(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, dev: str
) -> None:
    _env(catalog, principal, tmp_path, "prod")
    bob = _querier(session, "bob", dev, "prod")
    service = _service(session)
    for environment, name in (
        (dev, "beta"),
        ("prod", "alpha"),
        (dev, "gamma"),
        ("prod", "Delta"),
        (dev, "Alpha"),
    ):
        service.create(bob, environment, name=name, sql="SELECT 1")

    seen: list[tuple[str, str]] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = service.list_page(bob, limit=2, cursor=cursor)
        pages += 1
        assert len(page.items) <= 2
        seen.extend((i.name, i.environment) for i in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert pages == 3
    assert seen == [
        ("Alpha", "dev"),
        ("alpha", "prod"),
        ("beta", "dev"),
        ("Delta", "prod"),
        ("gamma", "dev"),
    ]
    assert _names(service.list_page(bob, environment="prod").items) == ["alpha", "Delta"]


def test_a_cursor_of_one_list_is_refused_by_the_other(session: Session, dev: str) -> None:
    bob = _querier(session, "bob", dev)
    service = _service(session)
    for name in ("a", "b", "c"):
        service.create(bob, dev, name=name, sql="SELECT 1")

    every = service.list_page(bob, limit=1).next_cursor
    one = service.list_page(bob, environment=dev, limit=1).next_cursor
    assert every is not None and one is not None

    with pytest.raises(ValidationError):
        service.list_page(bob, environment=dev, cursor=every)
    with pytest.raises(ValidationError):
        service.list_page(bob, cursor=one)
    with pytest.raises(ValidationError):
        service.list_page(bob, limit=SAVED_QUERY_LIST_MAX + 1)


class _CountingAuthorization:
    """Grants, counting how often each question is asked.

    Parameters
    ----------
    inner
        The authorization answering.
    """

    def __init__(self, inner: GrantAuthorization) -> None:
        self._inner = inner
        self.batches = 0
        self.single = 0

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        self._inner.authorize(principal, action, securable)

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        self.single += 1
        return self._inner.permits(principal, action, securable)

    def permits_each(
        self, principal: PrincipalRef, questions: Sequence[tuple[Action, Securable]]
    ) -> list[bool]:
        self.batches += 1
        return self._inner.permits_each(principal, questions)


@pytest.mark.parametrize("environments", [2, 9])
def test_the_cross_environment_list_asks_the_rights_in_one_batch(
    session: Session,
    catalog: CatalogService,
    principal: PrincipalRef,
    tmp_path: Path,
    environments: int,
) -> None:
    names = [f"env{n}" for n in range(environments)]
    for name in names:
        _env(catalog, principal, tmp_path, name)
    bob = _querier(session, "bob", *names)
    carol = _querier(session, "carol", *names)
    for name in names:
        _service(session).create(bob, name, name="Revenue", sql="SELECT 1", visibility="everyone")
    spy = _CountingAuthorization(GrantAuthorization(session))

    page = SavedQueryService(session, spy).list_page(carol)

    assert len(page.items) == environments
    assert (spy.batches, spy.single) == (1, 0)


def test_a_task_token_is_refused_every_operation(session: Session, dev: str) -> None:
    bob = _querier(session, "bob", dev)
    service = _service(session)
    mine = service.create(bob, dev, name="Revenue", sql="SELECT 1")
    task = PrincipalRef(
        id=bob.id,
        name=bob.name,
        type=bob.type,
        task_scope=TaskScope(environment=dev, asset_scope=[], token_id=uuid.uuid4()),
    )

    calls = (
        lambda: service.create(task, dev, name="Other", sql="SELECT 1"),
        lambda: service.get(task, dev, mine.id),
        lambda: service.update(task, dev, mine.id, name="x", sql="SELECT 1", visibility="private"),
        lambda: service.delete(task, dev, mine.id),
        lambda: service.list_page(task, environment=dev),
        lambda: service.list_page(task),
    )
    for call in calls:
        with pytest.raises(PermissionDenied) as refused:
            call()
        assert str(refused.value) == _TASK_REFUSED
    assert service.get(bob, dev, mine.id).name == "Revenue"


def test_create_update_and_delete_write_their_audit_events_without_sql(
    session: Session, dev: str
) -> None:
    bob = _querier(session, "bob", dev)
    service = _service(session)
    secret_sql = "SELECT 'a secret' AS s"

    mine = service.create(bob, dev, name="Revenue", sql=secret_sql)
    service.update(bob, dev, mine.id, name="Revenue 2", sql=secret_sql, visibility="everyone")
    service.delete(bob, dev, mine.id)

    by_type = {e.event_type: e for e in _saved_events(session)}
    assert len(_saved_events(session)) == 3
    events = [by_type[t] for t in _SAVED_EVENTS]
    for event in events:
        assert set(event.detail) == {"id", "name", "visibility", "owner"}
        assert event.detail["id"] == str(mine.id)
        assert event.detail["owner"] == "bob"
        assert event.principal_id == bob.id
        assert (event.securable_kind, event.securable_environment) == ("ENVIRONMENT", "dev")
        assert "secret" not in str(event.detail)
    assert [(e.detail["name"], e.detail["visibility"]) for e in events] == [
        ("Revenue", "private"),
        ("Revenue 2", "everyone"),
        ("Revenue 2", "everyone"),
    ]


def test_a_refused_change_writes_no_saved_query_event(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, dev: str
) -> None:
    _env(catalog, principal, tmp_path, "prod")
    bob = _querier(session, "bob", dev)
    carol = _querier(session, "carol", dev)
    dave = _querier(session, "dave", "prod")
    service = _service(session)
    mine = service.create(bob, dev, name="Revenue", sql="SELECT 1", visibility="everyone")
    session.execute(delete(AuditEvent))
    session.commit()

    refused = (
        lambda: service.create(dave, dev, name="Other", sql="SELECT 1"),
        lambda: service.create(bob, dev, name="Revenue", sql="SELECT 1"),
        lambda: service.create(bob, dev, name="a\nb", sql="SELECT 1"),
        lambda: service.update(carol, dev, mine.id, name="x", sql="SELECT 1", visibility="private"),
        lambda: service.delete(carol, dev, mine.id),
        lambda: service.delete(dave, dev, mine.id),
    )
    for call in refused:
        with pytest.raises((PermissionDenied, NotFoundError, AlreadyExistsError, ValidationError)):
            call()
    assert _saved_events(session) == []
    assert service.get(bob, dev, mine.id) == mine
