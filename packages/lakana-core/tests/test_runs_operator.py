"""What a caller may do on a job, a run and a deploy (`actions`), asked in one batch."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from core_test_helpers import ActionAllowList
from home_test_helpers import person
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, CiScope, PrincipalRef, Securable
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.models import AuditEvent, PrincipalType
from lakana_core.runs.operator import (
    DEPLOY_ACTIONS,
    JOB_ACTIONS,
    RUN_ACTIONS,
    OperatorRights,
    deploy_actions,
    job_actions,
    operator_rights_each,
    run_actions,
)


def _p(ci_scope: CiScope | None = None) -> PrincipalRef:
    """Make a caller.

    Parameters
    ----------
    ci_scope
        The CI scope, for a CI token.

    Returns
    -------
    PrincipalRef
        The caller.
    """
    return PrincipalRef(id=uuid.uuid4(), name="caller", type=PrincipalType.USER, ci_scope=ci_scope)


def _scope() -> CiScope:
    """Make a CI scope for `dev`.

    Returns
    -------
    CiScope
        The scope.
    """
    return CiScope(
        token_id=uuid.uuid4(),
        rule_id=uuid.uuid4(),
        environment="dev",
        environment_id=uuid.uuid4(),
        repository="analytics",
        repository_id=uuid.uuid4(),
        kinds=frozenset({"deploy"}),
        sha="a" * 40,
    )


def test_the_words_are_fixed() -> None:
    assert JOB_ACTIONS == ("run", "pause", "resume")
    assert RUN_ACTIONS == ("cancel", "resume")
    assert DEPLOY_ACTIONS == ("rollback",)


def test_an_administrator_gets_every_word() -> None:
    authz = ActionAllowList(Action.RUN, Action.DEPLOY)
    rights = operator_rights_each(authz, _p(), ["dev"])["dev"]

    assert job_actions(rights) == ("run", "pause", "resume")
    assert run_actions(rights) == ("cancel", "resume")
    assert deploy_actions(rights) == ("rollback",)


def test_a_reader_gets_no_word() -> None:
    rights = operator_rights_each(ActionAllowList(Action.READ_ENVIRONMENT), _p(), ["dev"])["dev"]

    assert rights == OperatorRights(run=False, deploy=False)
    assert job_actions(rights) == run_actions(rights) == deploy_actions(rights) == ()


def test_a_runner_may_start_and_cancel_but_not_pause() -> None:
    rights = operator_rights_each(ActionAllowList(Action.RUN), _p(), ["dev"])["dev"]

    assert job_actions(rights) == ("run",)
    assert run_actions(rights) == ("cancel", "resume")
    assert deploy_actions(rights) == ()


def test_a_ci_token_with_deploy_gets_no_pause_and_no_rollback() -> None:
    authz = ActionAllowList(Action.DEPLOY)
    rights = operator_rights_each(authz, _p(_scope()), ["dev"])["dev"]

    assert job_actions(rights) == ()
    assert deploy_actions(rights) == ()


class _Spy(ActionAllowList):
    """An allow list that keeps each batch it is asked."""

    def __init__(self, *allowed: Action) -> None:
        super().__init__(*allowed)
        self.batches: list[list[tuple[Action, Securable]]] = []

    def permits_each(
        self, principal: PrincipalRef, questions: Sequence[tuple[Action, Securable]]
    ) -> list[bool]:
        """Answer a batch and keep it.

        Parameters
        ----------
        principal
            The actor.
        questions
            The questions.

        Returns
        -------
        list of bool
            One answer per question.
        """
        self.batches.append(list(questions))
        return [self.permits(principal, action, scope) for action, scope in questions]


def test_rights_are_asked_in_one_batch_for_all_environments() -> None:
    spy = _Spy(Action.RUN)

    rights = operator_rights_each(spy, _p(), ["dev", "prod", "dev"])

    dev, prod = Securable.environment_scope("dev"), Securable.environment_scope("prod")
    assert spy.batches == [
        [(Action.RUN, dev), (Action.DEPLOY, dev), (Action.RUN, prod), (Action.DEPLOY, prod)]
    ]
    assert list(rights) == ["dev", "prod"]


def test_no_environment_asks_nothing() -> None:
    spy = _Spy(Action.RUN)

    assert operator_rights_each(spy, _p(), []) == {}
    assert spy.batches == []


def test_asking_writes_no_audit_row(session: Session) -> None:
    stranger = person(session, "stranger")
    authz = AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(10_000))

    rights = operator_rights_each(authz, stranger, ["dev", "prod"])

    assert rights["dev"] == OperatorRights(run=False, deploy=False)
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
