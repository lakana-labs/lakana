"""Listing grants without a filter, explaining a principal's access, searching people (#147)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, PrincipalRef, Securable, SecurableKind, TaskScope
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService, scope_filter
from lakana_core.identifiers import NamespacePath
from lakana_core.identity.groups import GroupService
from lakana_core.identity.principals import PrincipalService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Environment,
    Grant,
    Group,
    GroupMembership,
    Namespace,
    Principal,
    PrincipalType,
)
from lakana_core.pagination import encode_cursor, escape_like

_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _auditing(session: Session) -> AuditingAuthorization:
    """Build the production pairing: grants, wrapped to audit denials.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    AuditingAuthorization
        The wrapper around `GrantAuthorization`.
    """
    return AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(10_000))


def _person(session: Session, name: str, minutes: int = 0) -> PrincipalRef:
    """Persist an active user created `minutes` after `_T0`.

    Parameters
    ----------
    session
        Database session.
    name
        Its name.
    minutes
        Minutes after `_T0` it was created.

    Returns
    -------
    PrincipalRef
        A reference to it.
    """
    row = Principal(
        id=uuid.uuid4(),
        type=PrincipalType.USER,
        name=name,
        is_active=True,
        created_at=_T0 + timedelta(minutes=minutes),
    )
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _grant(session: Session, by: uuid.UUID, minutes: int, **fields: object) -> Grant:
    """Persist a grant made `minutes` after `_T0`.

    Parameters
    ----------
    session
        Database session.
    by
        The granting principal's id.
    minutes
        Minutes after `_T0` it was made.
    **fields
        Its columns.

    Returns
    -------
    Grant
        The row.
    """
    row = Grant(
        id=uuid.uuid4(), granted_by=by, granted_at=_T0 + timedelta(minutes=minutes), **fields
    )
    session.add(row)
    session.commit()
    return row


def _denials(session: Session) -> int:
    """Count the recorded denials.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    int
        The number of `AUTHORIZATION_DENIED` rows.
    """
    rows = session.execute(
        select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
    ).scalars()
    return len(list(rows))


@dataclass(frozen=True)
class _Setup:
    """What the tests start from.

    Parameters
    ----------
    admin
        Holds the `ADMIN` role on the instance.
    alice
        Holds grants in `dev`, and no right to manage grants.
    grants
        The grants by label.
    """

    admin: PrincipalRef
    alice: PrincipalRef
    grants: dict[str, Grant]


@pytest.fixture
def setup(session: Session) -> _Setup:
    """Persist an administrator, a user with grants, a group with one, and an environment.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    _Setup
        The principals and the grants by label.
    """
    admin = _person(session, "ada")
    alice = _person(session, "alice", 1)
    _person(session, "bob", 2)
    env = Environment(
        id=uuid.uuid4(), name="dev", storage_root="file:///x", storage_options={}, properties={}
    )
    session.add(env)
    session.flush()
    session.add(
        Namespace(id=uuid.uuid4(), environment_id=env.id, path="gold", properties={}),
    )
    team = Group(id=uuid.uuid4(), name="team", created_at=_T0)
    session.add(team)
    session.commit()
    grants = {
        "admin": _grant(
            session,
            admin.id,
            0,
            principal_id=admin.id,
            role=Role.ADMIN,
            kind=SecurableKind.INSTANCE,
        ),
        "alice-dev": _grant(
            session,
            admin.id,
            1,
            principal_id=alice.id,
            role=Role.ANALYST,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
            environment_id=env.id,
        ),
        "alice-gold": _grant(
            session,
            admin.id,
            2,
            principal_id=alice.id,
            action=Action.WRITE_TABLE,
            kind=SecurableKind.NAMESPACE,
            environment="dev",
            namespace="gold",
            environment_id=env.id,
        ),
        "team-deploy": _grant(
            session,
            admin.id,
            3,
            group_id=team.id,
            action=Action.DEPLOY,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
            environment_id=env.id,
        ),
    }
    return _Setup(admin, alice, grants)


def _ids(rows: list[Grant]) -> list[uuid.UUID]:
    """Take the ids of grants.

    Parameters
    ----------
    rows
        The grants.

    Returns
    -------
    list of uuid.UUID
        Their ids, in order.
    """
    return [row.id for row in rows]


def test_every_grant_is_listed_without_a_filter_newest_first(
    session: Session, setup: _Setup
) -> None:
    service = GrantService(session, _auditing(session))
    page = service.list_grants(setup.admin)
    assert _ids(page.items) == [
        setup.grants[k].id for k in ("team-deploy", "alice-gold", "alice-dev", "admin")
    ]
    assert page.next_cursor is None


def test_filters_combine(session: Session, setup: _Setup) -> None:
    service = GrantService(session, _auditing(session))
    dev = Securable.environment_scope("dev")
    assert _ids(service.list_grants(setup.admin, principal_name="alice").items) == [
        setup.grants["alice-gold"].id,
        setup.grants["alice-dev"].id,
    ]
    assert _ids(service.list_grants(setup.admin, group_name="team").items) == [
        setup.grants["team-deploy"].id
    ]
    assert _ids(service.list_grants(setup.admin, scope=dev).items) == [
        setup.grants["team-deploy"].id,
        setup.grants["alice-dev"].id,
    ]
    assert _ids(service.list_grants(setup.admin, principal_name="alice", scope=dev).items) == [
        setup.grants["alice-dev"].id
    ]
    gold = Securable.namespace_scope("dev", NamespacePath.from_parts(["gold"]))
    assert _ids(service.list_grants(setup.admin, scope=gold).items) == [
        setup.grants["alice-gold"].id
    ]
    assert _ids(service.list_grants(setup.admin, scope=Securable.instance_scope()).items) == [
        setup.grants["admin"].id
    ]


def test_the_action_filter_finds_single_actions_and_the_roles_that_carry_them(
    session: Session, setup: _Setup
) -> None:
    service = GrantService(session, _auditing(session))
    assert _ids(service.list_grants(setup.admin, action=Action.WRITE_TABLE).items) == [
        setup.grants["alice-gold"].id,
        setup.grants["admin"].id,
    ]
    assert _ids(service.list_grants(setup.admin, action=Action.SUBMIT_QUERY).items) == [
        setup.grants["alice-dev"].id,
        setup.grants["admin"].id,
    ]


def test_grants_page_by_cursor(session: Session, setup: _Setup) -> None:
    service = GrantService(session, _auditing(session))
    first = service.list_grants(setup.admin, limit=3)
    assert _ids(first.items) == [
        setup.grants[k].id for k in ("team-deploy", "alice-gold", "alice-dev")
    ]
    assert first.next_cursor is not None
    second = service.list_grants(setup.admin, limit=3, cursor=first.next_cursor)
    assert _ids(second.items) == [setup.grants["admin"].id]
    assert second.next_cursor is None


def test_a_cursor_of_another_list_or_a_bad_limit_is_refused(
    session: Session, setup: _Setup
) -> None:
    service = GrantService(session, _auditing(session))
    other = encode_cursor("runs", _T0, uuid.uuid4())
    with pytest.raises(ValidationError, match="`grants` list"):
        service.list_grants(setup.admin, cursor=other)
    with pytest.raises(ValidationError, match="limit"):
        service.list_grants(setup.admin, limit=0)
    with pytest.raises(ValidationError, match="at most one"):
        service.list_grants(setup.admin, principal_name="alice", group_name="team")


def test_unknown_subjects_are_404_for_a_grant_manager(session: Session, setup: _Setup) -> None:
    service = GrantService(session, _auditing(session))
    with pytest.raises(NotFoundError):
        service.list_grants(setup.admin, principal_name="nobody")
    with pytest.raises(NotFoundError):
        service.list_grants(setup.admin, group_name="nobody")


def test_someone_who_may_not_manage_grants_lists_none(session: Session, setup: _Setup) -> None:
    service = GrantService(session, _auditing(session))
    with pytest.raises(PermissionDenied):
        service.list_grants(setup.alice)
    with pytest.raises(PermissionDenied):
        service.list_grants(setup.alice, principal_name="nobody")
    assert _denials(session) == 2


def test_scope_filter_infers_the_kind_and_refuses_a_gap() -> None:
    assert scope_filter(None, None, None) is None
    assert scope_filter("dev", None, None) == Securable.environment_scope("dev")
    gold = NamespacePath.from_parts(["gold", "eu"])
    assert scope_filter("dev", "gold.eu", None) == Securable.namespace_scope("dev", gold)
    assert scope_filter("dev", "gold.eu", "t") == Securable.table_scope("dev", gold, "t")
    with pytest.raises(ValidationError, match="environment"):
        scope_filter(None, "gold", None)
    with pytest.raises(ValidationError, match="namespace"):
        scope_filter("dev", None, "orders")


def test_a_grant_manager_explains_anyone(session: Session, setup: _Setup) -> None:
    service = GrantService(session, _auditing(session))
    report = service.explain(setup.admin, "alice", Securable.environment_scope("dev"))
    submit = next(p for p in report.permissions if p.action is Action.SUBMIT_QUERY)
    assert [v.grant_id for v in submit.via] == [setup.grants["alice-dev"].id]
    assert report.principal == "alice"


def test_a_grant_manager_gets_404_for_an_unknown_principal_or_scope(
    session: Session, setup: _Setup
) -> None:
    service = GrantService(session, _auditing(session))
    with pytest.raises(NotFoundError):
        service.explain(setup.admin, "nobody", Securable.instance_scope())
    with pytest.raises(NotFoundError, match="environment"):
        service.explain(setup.admin, "alice", Securable.environment_scope("prod"))
    silver = NamespacePath.from_parts(["silver"])
    with pytest.raises(NotFoundError, match="namespace"):
        service.explain(setup.admin, "alice", Securable.namespace_scope("dev", silver))
    gold = NamespacePath.from_parts(["gold"])
    with pytest.raises(NotFoundError, match="table"):
        service.explain(setup.admin, "alice", Securable.table_scope("dev", gold, "orders"))


def test_a_principal_explains_itself_without_any_right(session: Session, setup: _Setup) -> None:
    service = GrantService(session, _auditing(session))
    report = service.explain(setup.alice, "alice", Securable.environment_scope("dev"))
    assert report.principal == "alice"
    unknown = service.explain(setup.alice, "alice", Securable.environment_scope("prod"))
    assert not any(p.allowed for p in unknown.permissions)
    assert _denials(session) == 0


def test_another_principal_is_refused_whether_or_not_it_exists(
    session: Session, setup: _Setup
) -> None:
    service = GrantService(session, _auditing(session))
    messages = set()
    for name in ("bob", "nobody"):
        with pytest.raises(PermissionDenied) as caught:
            service.explain(setup.alice, name, Securable.instance_scope())
        messages.add(str(caught.value))
    assert messages == {"'alice' may not MANAGE_GRANT this."}
    assert _denials(session) == 2


def test_a_task_token_may_not_explain_even_its_own_principal(
    session: Session, setup: _Setup
) -> None:
    token = PrincipalRef(
        id=setup.alice.id,
        name=setup.alice.name,
        type=setup.alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
    )
    service = GrantService(session, _auditing(session))
    with pytest.raises(PermissionDenied, match="Task tokens"):
        service.explain(token, "alice", Securable.instance_scope())


def test_escape_like_escapes_the_wildcards() -> None:
    assert escape_like(r"a%b_c\d") == r"a\%b\_c\\d"


def test_principals_are_searched_case_insensitively_and_paged(
    session: Session, setup: _Setup
) -> None:
    _person(session, "Carol_X", 3)
    _person(session, "carolyx", 4)
    _person(session, "100%-sure", 5)
    service = PrincipalService(session, _auditing(session))
    assert [p.name for p in service.list_principals(setup.admin, q="CAROL").items] == [
        "carolyx",
        "Carol_X",
    ]
    assert [p.name for p in service.list_principals(setup.admin, q="l_x").items] == ["Carol_X"]
    assert [p.name for p in service.list_principals(setup.admin, q="0%").items] == ["100%-sure"]
    assert [p.name for p in service.list_principals(setup.admin, q="  ").items][:1] == ["100%-sure"]
    first = service.list_principals(setup.admin, limit=4)
    assert [p.name for p in first.items] == ["100%-sure", "carolyx", "Carol_X", "bob"]
    assert first.next_cursor is not None
    rest = service.list_principals(setup.admin, limit=4, cursor=first.next_cursor)
    assert [p.name for p in rest.items] == ["alice", "ada"]
    assert rest.next_cursor is None
    with pytest.raises(ValidationError, match="limit"):
        service.list_principals(setup.admin, limit=0)
    with pytest.raises(PermissionDenied):
        PrincipalService(session, _auditing(session)).list_principals(setup.alice)


def test_groups_are_searched_and_paged(session: Session, setup: _Setup) -> None:
    for minutes, name in enumerate(("data_eng", "dataxeng", "Ops"), start=1):
        session.add(Group(id=uuid.uuid4(), name=name, created_at=_T0 + timedelta(minutes=minutes)))
    session.commit()
    service = GroupService(session, _auditing(session))
    assert [g.name for g in service.list_groups(setup.admin, q="A_E").items] == ["data_eng"]
    assert [g.name for g in service.list_groups(setup.admin, q="ops").items] == ["Ops"]
    first = service.list_groups(setup.admin, limit=2)
    assert [g.name for g in first.items] == ["Ops", "dataxeng"]
    rest = service.list_groups(setup.admin, limit=2, cursor=first.next_cursor)
    assert [g.name for g in rest.items] == ["data_eng", "team"]
    assert rest.next_cursor is None
    with pytest.raises(ValidationError, match="`groups` list"):
        service.list_groups(setup.admin, cursor=encode_cursor("principals", _T0, uuid.uuid4()))
    with pytest.raises(PermissionDenied):
        service.list_groups(setup.alice)


def _member(session: Session, group: str, principal: str) -> None:
    """Persist a membership by names.

    Parameters
    ----------
    session
        Database session.
    group
        The group's name.
    principal
        The principal's name.
    """
    group_id = session.execute(select(Group.id).where(Group.name == group)).scalar_one()
    principal_id = session.execute(
        select(Principal.id).where(Principal.name == principal)
    ).scalar_one()
    session.add(GroupMembership(group_id=group_id, principal_id=principal_id))
    session.commit()


def test_list_groups_by_member_lists_only_their_groups(session: Session, setup: _Setup) -> None:
    for minutes, name in enumerate(("analysts", "ops"), start=1):
        session.add(Group(id=uuid.uuid4(), name=name, created_at=_T0 + timedelta(minutes=minutes)))
    session.commit()
    _member(session, "analysts", "alice")
    _member(session, "ops", "alice")
    _member(session, "ops", "bob")
    service = GroupService(session, _auditing(session))

    assert {g.name for g in service.list_groups(setup.admin, member="alice").items} == {
        "analysts",
        "ops",
    }
    assert [g.name for g in service.list_groups(setup.admin, member="bob", q="op").items] == ["ops"]
    assert [g.name for g in service.list_groups(setup.admin, member="ada").items] == []
    service.remove_member(setup.admin, "ops", "alice")
    assert [g.name for g in service.list_groups(setup.admin, member="alice").items] == ["analysts"]


def test_list_groups_by_an_unknown_member_is_404_for_an_administrator_and_403_for_others(
    session: Session, setup: _Setup
) -> None:
    service = GroupService(session, _auditing(session))
    with pytest.raises(NotFoundError, match="nobody"):
        service.list_groups(setup.admin, member="nobody")
    with pytest.raises(PermissionDenied):
        service.list_groups(setup.alice, member="nobody")


def test_list_grants_by_role_lists_exactly_that_role(session: Session, setup: _Setup) -> None:
    viewer = _grant(
        session,
        setup.admin.id,
        4,
        principal_id=setup.alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.INSTANCE,
    )
    service = GrantService(session, _auditing(session))
    assert _ids(service.list_grants(setup.admin, role=Role.ANALYST).items) == [
        setup.grants["alice-dev"].id
    ]
    assert _ids(service.list_grants(setup.admin, role=Role.VIEWER).items) == [viewer.id]
    assert _ids(
        service.list_grants(setup.admin, role=Role.ANALYST, principal_name="alice").items
    ) == [setup.grants["alice-dev"].id]
    with pytest.raises(ValidationError, match=r"Give role or action, not both\."):
        service.list_grants(setup.admin, role=Role.ANALYST, action=Action.READ_TABLE)
