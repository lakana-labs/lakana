"""Get started: the steps by what a person may do, ticked from the server's state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from core_test_helpers import ensure_table
from home_test_helpers import deployed_job, environment, grant, person, query_of, run_of
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.home.get_started import GetStarted, get_started
from lakana_core.models import (
    CiTrustRule,
    DeployKind,
    DeployRun,
    DeployStatus,
    Environment,
    InterfaceState,
    Passkey,
    PrincipalType,
    QueryRunStatus,
    RecentOpen,
    SignInLink,
    TableRead,
)


def _started(session: Session, p: PrincipalRef, *, local: bool = False) -> GetStarted:
    """Ask Get started for a person with real authorization.

    Parameters
    ----------
    session
        Database session.
    p
        The person.
    local
        Whether the server runs in local mode.

    Returns
    -------
    GetStarted
        The answer.
    """
    return get_started(session, GrantService(session, GrantAuthorization(session)), p, local=local)


def _ids(answer: GetStarted) -> list[str]:
    """List the step ids in order.

    Parameters
    ----------
    answer
        The answer.

    Returns
    -------
    list of str
        The ids.
    """
    return [step for step, _ in answer.steps]


def _done(answer: GetStarted) -> set[str]:
    """Collect the steps that are done.

    Parameters
    ----------
    answer
        The answer.

    Returns
    -------
    set of str
        Their ids.
    """
    return {step for step, done in answer.steps if done}


def _setup(session: Session, who: str) -> PrincipalRef:
    """Persist a person with the access the audience needs.

    Parameters
    ----------
    session
        Database session.
    who
        ``admin``, ``deployer``, ``reader`` or ``none``.

    Returns
    -------
    PrincipalRef
        The person.
    """
    environment(session, "dev")
    ref = person(session, who)
    if who == "admin":
        grant(session, ref, role=Role.ADMIN)
    elif who == "deployer":
        grant(session, ref, action=Action.DEPLOY, environment_name="dev")
    elif who == "reader":
        grant(session, ref, role=Role.VIEWER, environment_name="dev")
    return ref


@pytest.mark.parametrize(
    ("who", "local", "audience", "ids"),
    [
        (
            "admin",
            False,
            "admin",
            ["connect_repository", "invite_team", "add_passkey", "deploy_project", "ci_deploy"],
        ),
        (
            "admin",
            True,
            "admin",
            ["add_passkey", "deploy_project_local", "open_table", "run_query"],
        ),
        ("deployer", False, "deployer", ["deploy_project", "open_table", "run_query"]),
        ("deployer", True, "deployer", ["deploy_project", "open_table", "run_query"]),
        ("reader", False, "reader", ["open_table", "run_query"]),
        ("reader", True, "reader", ["open_table", "run_query"]),
        ("none", False, "none", []),
    ],
)
def test_get_started_lists_by_audience(
    session: Session, who: str, local: bool, audience: str, ids: list[str]
) -> None:
    answer = _started(session, _setup(session, who), local=local)
    assert (answer.audience, answer.local, _ids(answer)) == (audience, local, ids)
    assert answer.complete is False
    assert answer.hidden is False


def test_a_deploy_grant_on_a_deleted_environment_is_no_deployer(session: Session) -> None:
    ref = _setup(session, "deployer")
    environment_row = session.get(Environment, environment(session, "dev"))
    assert environment_row is not None
    environment_row.deleted_at = datetime.now(UTC)
    session.commit()
    assert _started(session, ref).audience == "reader"


def test_get_started_ticks_from_the_servers_state(session: Session) -> None:
    admin = _setup(session, "admin")
    assert _done(_started(session, admin)) == set()
    repo, version = deployed_job(session, "dev", "analytics", "nightly")
    assert _done(_started(session, admin)) == {"connect_repository"}
    session.add(
        SignInLink(
            id=uuid.uuid4(),
            public_id="p" * 32,
            secret_hash="0" * 64,
            kind="invite",
            invite_name="kim",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    session.add(
        Passkey(
            id=uuid.uuid4(),
            principal_id=admin.id,
            credential_id="c1",
            public_key=b"k",
            sign_count=0,
            rp_id="localhost",
            name="laptop",
            backup_eligible=False,
            created_at=datetime.now(UTC),
            created_user_agent="test",
        )
    )
    session.add(
        DeployRun(
            id=uuid.uuid4(),
            environment_id=version.environment_id,
            repository_id=repo.id,
            principal_id=admin.id,
            kind=DeployKind.DEPLOY,
            status=DeployStatus.SUCCEEDED,
        )
    )
    session.add(
        CiTrustRule(
            id=uuid.uuid4(),
            issuer="https://token.actions.githubusercontent.com",
            principal_id=admin.id,
            environment_id=version.environment_id,
            repository_id=repo.id,
            kinds=["deploy"],
            conditions={},
        )
    )
    session.commit()
    answer = _started(session, admin)
    assert _done(answer) == set(_ids(answer))
    assert answer.complete is True


def test_steps_that_do_not_count(session: Session) -> None:
    admin = _setup(session, "admin")
    repo, version = deployed_job(session, "dev", "analytics", "nightly")
    now = datetime.now(UTC)
    for kind, used, withdrawn, expires in (
        ("recovery", None, None, now + timedelta(days=1)),  # not an invite
        ("invite", now, None, now + timedelta(days=1)),  # used: then a person exists
        ("invite", None, now, now + timedelta(days=1)),
        ("invite", None, None, now - timedelta(seconds=1)),
    ):
        session.add(
            SignInLink(
                id=uuid.uuid4(),
                public_id=uuid.uuid4().hex,
                secret_hash="0" * 64,
                kind=kind,
                created_at=now,
                expires_at=expires,
                used_at=used,
                withdrawn_at=withdrawn,
            )
        )
    person(session, "robot", PrincipalType.SERVICE_ACCOUNT)
    session.add(
        Passkey(
            id=uuid.uuid4(),
            principal_id=admin.id,
            credential_id="c1",
            public_key=b"k",
            sign_count=0,
            rp_id="localhost",
            name="old",
            backup_eligible=False,
            created_at=now,
            created_user_agent="test",
            disabled_at=now,
        )
    )
    for kind, status in (
        (DeployKind.DRY_RUN, DeployStatus.SUCCEEDED),
        (DeployKind.DEPLOY, DeployStatus.FAILED),
    ):
        session.add(
            DeployRun(
                id=uuid.uuid4(),
                environment_id=version.environment_id,
                repository_id=repo.id,
                kind=kind,
                status=status,
            )
        )
    session.add(
        CiTrustRule(
            id=uuid.uuid4(),
            issuer="https://token.actions.githubusercontent.com",
            principal_id=admin.id,
            environment_id=version.environment_id,
            repository_id=repo.id,
            kinds=["deploy"],
            conditions={},
            enabled=False,
        )
    )
    session.commit()
    assert _done(_started(session, admin)) == {"connect_repository"}
    person(session, "kim")
    assert _done(_started(session, admin)) == {"connect_repository", "invite_team"}


def test_open_table_and_run_query_are_the_persons_own(session: Session) -> None:
    reader = _setup(session, "reader")
    other = person(session, "other")
    _, table_id = ensure_table(session, "dev", "gold", "sales")
    repo, version = deployed_job(session, "dev", "analytics", "nightly")
    pipeline = run_of(session, repo, version)
    query_of(session, reader, pipeline=pipeline)  # a pipeline's SQL task is no query of theirs
    query_of(session, reader, status=QueryRunStatus.FAILED)
    query_of(session, other)
    session.add(
        TableRead(
            id=uuid.uuid4(),
            table_id=table_id,
            principal_id=other.id,
            day=datetime.now(UTC).date(),
            read_count=1,
            last_read_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert _done(_started(session, reader)) == set()
    query_of(session, reader)
    assert _done(_started(session, reader)) == {"run_query"}
    session.add(
        RecentOpen(
            id=uuid.uuid4(),
            principal_id=reader.id,
            kind="table",
            environment_id=version.environment_id,
            ref="gold.sales",
            opened_at=datetime.now(UTC),
        )
    )
    session.commit()
    answer = _started(session, reader)
    assert (_done(answer), answer.complete) == ({"open_table", "run_query"}, True)


def test_a_table_read_by_a_query_counts_as_opened(session: Session) -> None:
    reader = _setup(session, "reader")
    _, table_id = ensure_table(session, "dev", "gold", "sales")
    session.add(
        TableRead(
            id=uuid.uuid4(),
            table_id=table_id,
            principal_id=reader.id,
            day=datetime.now(UTC).date(),
            read_count=1,
            last_read_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert _done(_started(session, reader)) == {"open_table"}


def test_get_started_complete_only_when_every_step_is_done(session: Session) -> None:
    nobody = _setup(session, "none")
    assert _started(session, nobody).complete is False  # an empty list is never "complete"
    reader = _setup(session, "reader")
    query_of(session, reader)
    assert _started(session, reader).complete is False


def test_get_started_says_when_the_person_hid_it(session: Session) -> None:
    reader = _setup(session, "reader")
    session.add(
        InterfaceState(
            principal_id=reader.id, get_started_hidden=True, updated_at=datetime.now(UTC)
        )
    )
    session.commit()
    assert _started(session, reader).hidden is True
