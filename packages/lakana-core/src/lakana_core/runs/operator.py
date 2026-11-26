"""What a caller's rights allow on a job, a run and a deploy (`actions`).

Every job, run and deploy the platform API answers carries ``actions``: the words of the
actions this caller's rights allow on it, as `catalog.row_actions` says of tables.

- A job: ``run`` (`RUN` on the environment), ``pause`` and ``resume`` (`DEPLOY`, and not a CI
  token).
- A run: ``cancel`` and ``resume`` (`RUN` on the environment).
- A deploy: ``rollback`` and ``undeploy`` (`DEPLOY`, and not a CI token); ``undeploy`` names
  the right to undeploy the deploy's repository from its environment.

The words are rights only. A client offers a word's control only when the word is listed and
the object's state allows it (Pause for an unpaused job with a schedule, Cancel for a queued or
running run, Undeploy beside the deploy that made the repository's current version). The
server decides every request again.

The rights of a whole answer are asked in one `permits_each` batch over its distinct
environments, and, like every visibility question, they write no audit event and no
`AUTHORIZATION_DENIED`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable, permits_each

JOB_ACTIONS = ("run", "pause", "resume")
"""The words a job's `actions` may hold, in this order."""

RUN_ACTIONS = ("cancel", "resume")
"""The words a run's `actions` may hold, in this order."""

DEPLOY_ACTIONS = ("rollback", "undeploy")
"""The words a deploy's `actions` may hold."""


@dataclass(frozen=True)
class OperatorRights:
    """What the caller may do in one environment.

    Parameters
    ----------
    run
        `RUN` on the environment.
    deploy
        `DEPLOY` on the environment, for a caller that is not a CI token.
    """

    run: bool
    deploy: bool


def operator_rights_each(
    authz: AuthorizationService, p: PrincipalRef, environments: Iterable[str]
) -> dict[str, OperatorRights]:
    """Ask the caller's rights in each environment, in one batch.

    Parameters
    ----------
    authz
        The authorization service.
    p
        The caller.
    environments
        The environments of the rows to answer; repeats are asked once.

    Returns
    -------
    dict of str to OperatorRights
        The rights per distinct environment. A CI token never holds the deploy right here: it
        cannot pause, resume a schedule or roll back.
    """
    names = list(dict.fromkeys(environments))
    questions = [
        (action, Securable.environment_scope(name))
        for name in names
        for action in (Action.RUN, Action.DEPLOY)
    ]
    answers = permits_each(authz, p, questions) if questions else []
    return {
        name: OperatorRights(run=answers[2 * i], deploy=answers[2 * i + 1] and p.ci_scope is None)
        for i, name in enumerate(names)
    }


def job_actions(rights: OperatorRights) -> tuple[str, ...]:
    """Say which of `JOB_ACTIONS` the rights allow.

    Parameters
    ----------
    rights
        The caller's rights in the job's environment.

    Returns
    -------
    tuple of str
        The words, in `JOB_ACTIONS` order.
    """
    allowed = {"run": rights.run, "pause": rights.deploy, "resume": rights.deploy}
    return tuple(word for word in JOB_ACTIONS if allowed[word])


def run_actions(rights: OperatorRights) -> tuple[str, ...]:
    """Say which of `RUN_ACTIONS` the rights allow.

    Parameters
    ----------
    rights
        The caller's rights in the run's environment.

    Returns
    -------
    tuple of str
        The words, in `RUN_ACTIONS` order.
    """
    return RUN_ACTIONS if rights.run else ()


def deploy_actions(rights: OperatorRights) -> tuple[str, ...]:
    """Say which of `DEPLOY_ACTIONS` the rights allow.

    Parameters
    ----------
    rights
        The caller's rights in the deploy's environment.

    Returns
    -------
    tuple of str
        The words, in `DEPLOY_ACTIONS` order.
    """
    return DEPLOY_ACTIONS if rights.deploy else ()
