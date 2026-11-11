"""Per-person interface state: the tour, Get started and the update note."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, CiScope, PrincipalRef, TaskScope
from lakana_core.errors import NotFoundError, PermissionDenied, ValidationError
from lakana_core.identity.interface_state import InterfaceStateService, InterfaceStateView
from lakana_core.models import InterfaceState, Principal, PrincipalType


@pytest.fixture
def svc(session: Session) -> InterfaceStateService:
    """Provide the service.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    InterfaceStateService
        The service under test.
    """
    return InterfaceStateService(session, AllowAllAuthorization())


@pytest.fixture
def alice(session: Session) -> PrincipalRef:
    """Persist a person called alice.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    PrincipalRef
        Her reference.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name="alice", type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=ref.name, is_active=True))
    session.commit()
    return ref


def test_state_defaults_for_a_person_without_a_row(
    svc: InterfaceStateService, alice: PrincipalRef, session: Session
) -> None:
    assert svc.get(alice, "alice") == InterfaceStateView(
        tour_seen=False, get_started_hidden=False, update_note_seen=None
    )
    assert session.get(InterfaceState, alice.id) is None  # a read writes nothing


def test_update_changes_only_the_fields_sent(
    svc: InterfaceStateService, alice: PrincipalRef
) -> None:
    assert svc.update(alice, "alice", tour_seen=True) == InterfaceStateView(
        tour_seen=True, get_started_hidden=False, update_note_seen=None
    )
    svc.update(alice, "alice", update_note_seen="0.22.0")
    svc.update(alice, "alice", get_started_hidden=True)
    assert svc.get(alice, "alice") == InterfaceStateView(
        tour_seen=True, get_started_hidden=True, update_note_seen="0.22.0"
    )
    svc.update(alice, "alice", tour_seen=False, update_note_seen=None)
    assert svc.get(alice, "alice") == InterfaceStateView(
        tour_seen=False, get_started_hidden=True, update_note_seen=None
    )


def test_seeing_the_tour_again_keeps_the_first_time(
    svc: InterfaceStateService, alice: PrincipalRef, session: Session
) -> None:
    svc.update(alice, "alice", tour_seen=True)
    first = session.get(InterfaceState, alice.id)
    assert first is not None
    seen_at = first.tour_seen_at
    svc.update(alice, "alice", tour_seen=True)
    again = session.get(InterfaceState, alice.id)
    assert again is not None
    assert again.tour_seen_at == seen_at


def test_another_persons_state_is_refused(svc: InterfaceStateService, alice: PrincipalRef) -> None:
    with pytest.raises(PermissionDenied, match="your own interface state"):
        svc.get(alice, "bob")
    with pytest.raises(PermissionDenied, match="your own interface state"):
        svc.update(alice, "bob", tour_seen=True)


def test_task_token_cannot_change_interface_state(
    svc: InterfaceStateService, alice: PrincipalRef
) -> None:
    task = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4()),
    )
    ci = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        ci_scope=CiScope(
            token_id=uuid.uuid4(),
            rule_id=uuid.uuid4(),
            environment="dev",
            environment_id=uuid.uuid4(),
            repository="analytics",
            repository_id=uuid.uuid4(),
            kinds=frozenset({"deploy"}),
            sha=None,
        ),
    )
    for ref in (task, ci):
        with pytest.raises(PermissionDenied, match="task or CI token"):
            svc.update(ref, "alice", tour_seen=True)
        with pytest.raises(PermissionDenied, match="task or CI token"):
            svc.get(ref, "alice")


@pytest.mark.parametrize(
    "version", ["0.22.0.dev3", "0+unknown", "v1.0.0", "1.0", "\u0661.\u0662.\u0663", ""]
)
def test_update_note_seen_must_be_a_release_version(
    svc: InterfaceStateService, alice: PrincipalRef, version: str
) -> None:
    with pytest.raises(ValidationError, match=r"x\.y\.z"):
        svc.update(alice, "alice", tour_seen=True, update_note_seen=version)
    # A refused request changes nothing, so the tour goes in a request of its own.
    assert svc.get(alice, "alice").tour_seen is False
    assert svc.update(alice, "alice", tour_seen=True).tour_seen is True


def test_an_overlong_version_is_refused(svc: InterfaceStateService, alice: PrincipalRef) -> None:
    with pytest.raises(ValidationError, match="64"):
        svc.update(alice, "alice", update_note_seen="1." + "0" * 70 + ".0")


def test_a_person_whose_principal_is_gone_is_told(session: Session) -> None:
    ghost = PrincipalRef(id=uuid.uuid4(), name="ghost", type=PrincipalType.USER)
    with pytest.raises(NotFoundError, match="no longer exists"):
        InterfaceStateService(session, AllowAllAuthorization()).update(
            ghost, "ghost", tour_seen=True
        )
