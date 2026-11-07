"""Listing environments asks the grants a constant number of questions (#369)."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import _config
from sqlalchemy import delete, event
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    PrincipalRef,
    Securable,
    SecurableKind,
    TaskScope,
    permits_each,
)
from lakana_core.catalog import CatalogService
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import Environment, Grant, Principal, PrincipalType
from lakana_core.storage.resolver import StorageResolver


def _service(session: Session) -> CatalogService:
    """Build a catalog service over the real grants.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    CatalogService
        The service under test.
    """
    return CatalogService(session, StorageResolver(session), GrantAuthorization(session))


def _make_envs(session: Session, admin: PrincipalRef, tmp_path: Path, count: int) -> list[str]:
    """Create environments through an allow-all service.

    Parameters
    ----------
    session
        Database session.
    admin
        The acting principal.
    tmp_path
        Storage root.
    count
        How many to create.

    Returns
    -------
    list of str
        Their names.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    names = [f"env{i:02d}" for i in range(count)]
    for name in names:
        catalog.create_environment(admin, name=name, storage=_config(tmp_path / name))
    return names


def _reader(session: Session, *, active: bool = True) -> PrincipalRef:
    """Persist a reader.

    Parameters
    ----------
    session
        Database session.
    active
        Whether the principal is active.

    Returns
    -------
    PrincipalRef
        The reader.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=f"r-{uuid.uuid4().hex[:6]}", type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=active))
    session.commit()
    return ref


def _grant(
    session: Session,
    reader: PrincipalRef,
    kind: SecurableKind,
    env: str | None = None,
    **where: str,
) -> Grant:
    """Grant the reader one action.

    Parameters
    ----------
    session
        Database session.
    reader
        The reader.
    kind
        The securable kind.
    env
        The environment, unless instance-wide.
    **where
        The namespace, for a namespace grant.

    Returns
    -------
    Grant
        The stored grant.
    """
    env_id = session.query(Environment.id).filter(Environment.name == env).scalar() if env else None
    action = Action.READ_ENVIRONMENT if kind != SecurableKind.NAMESPACE else Action.READ_TABLE
    grant = Grant(
        id=uuid.uuid4(),
        principal_id=reader.id,
        action=action,
        kind=kind,
        environment=env,
        environment_id=env_id,
        granted_by=reader.id,
        granted_at=datetime.now(UTC),
        **where,
    )
    session.add(grant)
    session.commit()
    return grant


def _statements(session: Session, ask: Callable[[], object]) -> int:
    """Count the SQL statements a call issues.

    Parameters
    ----------
    session
        Database session.
    ask
        The call.

    Returns
    -------
    int
        The statements that reached the database.
    """
    seen: list[object] = []

    def count(*args: object) -> None:
        """Note one statement.

        Parameters
        ----------
        *args
            What SQLAlchemy passes.
        """
        seen.append(args)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        ask()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return len(seen)


def _cost(session: Session, tmp_path: Path, principal: PrincipalRef, count: int) -> int:
    """Count the statements `list_environments` needs with `count` environments.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The admin that creates them.
    count
        How many environments.

    Returns
    -------
    int
        The statements of one listing by a reader with a few environment grants.
    """
    names = _make_envs(session, principal, tmp_path, count)
    reader = _reader(session)
    for name in names[:3]:
        _grant(session, reader, SecurableKind.ENVIRONMENT, name)
    service = _service(session)
    session.commit()
    return _statements(session, lambda: service.list_environments(reader))


def test_listing_environments_costs_the_same_statements_for_three_or_thirty(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    few = _cost(session, tmp_path / "a", principal, 3)
    session.execute(delete(Environment))
    session.commit()
    many = _cost(session, tmp_path / "b", principal, 30)
    assert many == few


def test_listing_deleted_environments_costs_the_same_statements(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())

    def cost(count: int, base: str) -> int:
        for i in range(count):
            name = f"{base}{i:02d}"
            catalog.create_environment(principal, name=name, storage=_config(tmp_path / name))
            catalog.delete_environment(principal, name)
        reader = _reader(session)
        service = _service(session)
        return _statements(session, lambda: service.list_deleted_environments(reader))

    few = cost(3, "a")
    many = cost(30, "b")
    assert many == few


def test_batch_answers_equal_per_item_answers(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    names = _make_envs(session, principal, tmp_path, 5)
    instance = _reader(session)
    _grant(session, instance, SecurableKind.INSTANCE)
    some = _reader(session)
    _grant(session, some, SecurableKind.ENVIRONMENT, names[0])
    _grant(session, some, SecurableKind.NAMESPACE, names[4], namespace="gold")
    nothing = _reader(session)
    inactive = _reader(session, active=False)
    _grant(session, inactive, SecurableKind.ENVIRONMENT, names[1])
    task = PrincipalRef(
        id=some.id,
        name=some.name,
        type=some.type,
        task_scope=TaskScope(environment=names[0], asset_scope=["*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    questions = [
        (Action.READ_ENVIRONMENT, Securable.environment_scope(n)) for n in [*names, "missing"]
    ]
    for who in (instance, some, nothing, inactive, task):
        expected = [authz.permits(who, a, s) for a, s in questions]
        assert permits_each(GrantAuthorization(session), who, questions) == expected
    assert [e.name for e in _service(session).list_environments(task)] == [names[0]]
    assert _service(session).list_environments(nothing) == []


def test_a_grant_revoked_between_two_requests_is_seen_on_the_next(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    names = _make_envs(session, principal, tmp_path, 2)
    reader = _reader(session)
    grant = _grant(session, reader, SecurableKind.ENVIRONMENT, names[0])
    assert [e.name for e in _service(session).list_environments(reader)] == [names[0]]
    grant.revoked_at = datetime.now(UTC)
    session.commit()
    assert _service(session).list_environments(reader) == []


@pytest.mark.parametrize("count", [0])
def test_no_questions_cost_nothing(session: Session, count: int) -> None:
    assert permits_each(GrantAuthorization(session), _reader(session), []) == []
