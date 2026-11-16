"""A grant identical to a live one is refused (#673), by the service and by the database."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import _minimal_schema
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, SecurableKind
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.tombstones import unrevoke
from lakana_core.config import StorageConfig
from lakana_core.errors import AlreadyExistsError
from lakana_core.grants import duplicates
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.identity.groups import GroupService
from lakana_core.models import Grant, Principal, PrincipalType

_GOLD = NamespacePath.from_parts(["gold"]).joined()

_TARGETS: dict[str, tuple[SecurableKind, str | None, str | None, str | None]] = {
    "instance": (SecurableKind.INSTANCE, None, None, None),
    "environment": (SecurableKind.ENVIRONMENT, "dev", None, None),
    "namespace": (SecurableKind.NAMESPACE, "dev", _GOLD, None),
    "table": (SecurableKind.TABLE, "dev", _GOLD, "orders"),
}
"""Kind, environment, namespace and table of one grant target of each kind."""

_SHOWN = {
    "instance": "the instance",
    "environment": "environment 'dev'",
    "namespace": "namespace gold in 'dev'",
    "table": "table 'orders' in 'dev'/gold",
}


@pytest.fixture
def world(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> GrantService:
    """Build `dev` with `gold.orders`, a person `tess` and a group `eng`.

    Parameters
    ----------
    session
        Database session.
    catalog
        Catalog service.
    principal
        The acting administrator.
    tmp_path
        Storage root of `dev`.

    Returns
    -------
    GrantService
        The service under test, under allow-all authorization.
    """
    storage = StorageConfig(scheme="file", root=f"file://{tmp_path}", options={})
    catalog.create_environment(principal, name="dev", storage=storage)
    catalog.create_table(
        principal,
        TableRef(environment="dev", namespace=NamespacePath.from_parts(["gold"]), name="orders"),
        schema=_minimal_schema(),
    )
    session.add(Principal(id=uuid.uuid4(), type=PrincipalType.USER, name="tess", is_active=True))
    session.commit()
    GroupService(session, AllowAllAuthorization()).create_group(principal, name="eng")
    return GrantService(session, AllowAllAuthorization())


def _live(session: Session) -> list[Grant]:
    """List the grants that are not revoked.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of Grant
        The live grants.
    """
    return list(session.scalars(select(Grant).where(Grant.revoked_at.is_(None))))


def _grant(
    service: GrantService,
    p: PrincipalRef,
    target: str,
    *,
    principal: str | None = None,
    group: str | None = None,
    role: Role = Role.ANALYST,
) -> Grant:
    """Grant a role on one of `_TARGETS`.

    Parameters
    ----------
    service
        The grant service.
    p
        The acting administrator.
    target
        A key of `_TARGETS`.
    principal
        The person, or `None`.
    group
        The group, or `None`.
    role
        The role.

    Returns
    -------
    Grant
        The grant made.
    """
    kind, environment, namespace, table = _TARGETS[target]
    return service.grant(
        p,
        principal=principal,
        group=group,
        role=role,
        kind=kind,
        environment=environment,
        namespace=namespace,
        table=table,
    )


@pytest.mark.parametrize("target", sorted(_TARGETS))
@pytest.mark.parametrize("subject", ["principal", "group"])
def test_an_identical_grant_is_refused_and_names_the_existing_one(
    session: Session, world: GrantService, principal: PrincipalRef, subject: str, target: str
) -> None:
    """The second identical grant is refused with the first one's id; one grant remains."""
    person = "tess" if subject == "principal" else None
    group = "eng" if subject == "group" else None
    first = _grant(world, principal, target, principal=person, group=group)

    with pytest.raises(AlreadyExistsError) as refused:
        _grant(world, principal, target, principal=person, group=group)

    holder = "Principal 'tess'" if subject == "principal" else "Group 'eng'"
    assert str(refused.value) == (
        f"{holder} already holds ANALYST on {_SHOWN[target]} through grant {first.id}; "
        "nothing was created."
    )
    assert [g.id for g in _live(session)] == [first.id]


def test_an_identical_action_grant_is_refused(
    session: Session, world: GrantService, principal: PrincipalRef
) -> None:
    """A single action counts the same way as a role."""
    first = world.grant(
        principal, principal="tess", action=Action.READ_TABLE, kind=SecurableKind.INSTANCE
    )
    with pytest.raises(AlreadyExistsError, match=str(first.id)):
        world.grant(
            principal, principal="tess", action=Action.READ_TABLE, kind=SecurableKind.INSTANCE
        )
    assert len(_live(session)) == 1


def test_revoking_the_one_grant_removes_the_access_and_it_can_be_granted_again(
    session: Session, world: GrantService, principal: PrincipalRef
) -> None:
    """A revoked grant is no twin: the same grant may be made again after a revoke."""
    first = world.grant(
        principal,
        principal="tess",
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    with pytest.raises(AlreadyExistsError):
        world.grant(
            principal,
            principal="tess",
            role=Role.ANALYST,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
        )
    world.revoke(principal, first.id)
    assert _live(session) == []

    again = world.grant(
        principal,
        principal="tess",
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    assert [g.id for g in _live(session)] == [again.id]


def test_grants_that_differ_in_one_part_are_all_kept(
    session: Session, world: GrantService, principal: PrincipalRef
) -> None:
    """Another role, action, kind, subject, namespace or table is another grant."""
    silver = NamespacePath.from_parts(["silver"]).joined()
    env = SecurableKind.ENVIRONMENT
    world.grant(principal, principal="tess", role=Role.ANALYST, kind=env, environment="dev")
    world.grant(principal, principal="tess", role=Role.VIEWER, kind=env, environment="dev")
    world.grant(principal, principal="tess", action=Action.READ_TABLE, kind=env, environment="dev")
    world.grant(principal, group="eng", role=Role.ANALYST, kind=env, environment="dev")
    _grant(world, principal, "instance", principal="tess")
    _grant(world, principal, "namespace", principal="tess")
    _grant(world, principal, "table", principal="tess")
    world.grant(
        principal,
        principal="tess",
        role=Role.ANALYST,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace=silver,
    )

    assert len(_live(session)) == 8


def test_a_twin_written_between_the_check_and_the_insert_is_refused_too(
    session: Session,
    world: GrantService,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When another request wins the race, the database refuses and the same sentence follows."""
    first = world.grant(principal, principal="tess", role=Role.ANALYST, kind=SecurableKind.INSTANCE)
    looked = duplicates.live_twin
    calls: list[Grant] = []

    def unseen_once(session_: Session, grant: Grant) -> Grant | None:
        """Miss the twin the first time, as a request that checked before the other wrote.

        Returns
        -------
        Grant or None
            `None` at first, then the real answer.
        """
        calls.append(grant)
        return None if len(calls) == 1 else looked(session_, grant)

    monkeypatch.setattr(duplicates, "live_twin", unseen_once)

    with pytest.raises(AlreadyExistsError, match=str(first.id)):
        world.grant(principal, principal="tess", role=Role.ANALYST, kind=SecurableKind.INSTANCE)
    assert [g.id for g in _live(session)] == [first.id]


def test_an_integrity_error_without_a_twin_is_not_reworded(
    world: GrantService, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a twin turns the database's refusal into the sentence; anything else is raised."""
    world.grant(principal, principal="tess", role=Role.ANALYST, kind=SecurableKind.INSTANCE)
    monkeypatch.setattr(duplicates, "live_twin", lambda _session, _grant: None)

    with pytest.raises(IntegrityError):
        world.grant(principal, principal="tess", role=Role.ANALYST, kind=SecurableKind.INSTANCE)


def _row(
    principal_id: uuid.UUID | None,
    by: uuid.UUID,
    kind: SecurableKind,
    *,
    environment_id: uuid.UUID | None = None,
    namespace: str | None = None,
    table_id: uuid.UUID | None = None,
) -> Grant:
    """Build a VIEWER grant on `dev` directly, past the service.

    Parameters
    ----------
    principal_id
        Its person.
    by
        Who granted it.
    kind
        Its kind.
    environment_id
        The environment it binds, or `None`.
    namespace
        Its namespace, or `None`.
    table_id
        The table it binds, or `None`.

    Returns
    -------
    Grant
        The row, not yet added.
    """
    return Grant(
        id=uuid.uuid4(),
        principal_id=principal_id,
        role=Role.VIEWER,
        kind=kind,
        environment=None if kind is SecurableKind.INSTANCE else "dev",
        namespace=namespace,
        table="orders" if kind is SecurableKind.TABLE else None,
        environment_id=environment_id,
        table_id=table_id,
        granted_by=by,
        granted_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("target", sorted(_TARGETS))
def test_the_database_refuses_a_second_live_twin(
    session: Session, world: GrantService, principal: PrincipalRef, target: str
) -> None:
    """Each kind has a unique index over its live grants, whatever writes the row."""
    made = _grant(world, principal, target, principal="tess", role=Role.VIEWER)
    twin = _row(
        made.principal_id,
        principal.id,
        made.kind,
        environment_id=made.environment_id,
        namespace=made.namespace,
        table_id=made.table_id,
    )
    session.add(twin)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_grants_whose_target_is_gone_never_collide(
    session: Session, world: GrantService, principal: PrincipalRef
) -> None:
    """A grant whose environment or table row was deleted holds no id; two such grants may stay."""
    tess = session.scalars(select(Principal.id).where(Principal.name == "tess")).one()
    for kind, namespace in (
        (SecurableKind.ENVIRONMENT, None),
        (SecurableKind.NAMESPACE, _GOLD),
        (SecurableKind.TABLE, _GOLD),
    ):
        for _ in range(2):
            session.add(_row(tess, principal.id, kind, namespace=namespace))
    session.commit()

    assert len(_live(session)) == 6
    gone = _live(session)[0]
    assert duplicates.live_twin(session, gone) is None


def test_a_restore_leaves_revoked_a_grant_whose_twin_is_live(
    session: Session, world: GrantService, principal: PrincipalRef
) -> None:
    """Two revoked twins from before the upgrade come back as one; the other stays revoked."""
    made = world.grant(
        principal,
        principal="tess",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    when = datetime.now(UTC)
    twin = _row(
        made.principal_id,
        principal.id,
        SecurableKind.ENVIRONMENT,
        environment_id=made.environment_id,
    )
    twin.revoked_at = when
    made.revoked_at = when
    session.add(twin)
    session.commit()

    restored = unrevoke(
        session,
        [{"kind": "grant", "id": str(made.id)}, {"kind": "grant", "id": str(twin.id)}],
        revoked_at=when,
    )
    session.commit()

    assert restored == [{"kind": "grant", "id": str(made.id)}]
    assert [g.id for g in _live(session)] == [made.id]
    assert twin.revoked_at is not None
