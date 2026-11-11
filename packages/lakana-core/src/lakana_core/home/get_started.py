"""Get started: what a person might do next on a new instance, ticked from the server's state.

The steps depend on what the person may do (the audience) and on whether the server runs in
local mode (`lakana server start --local`: one person on one machine, so no invite, no CI and
no repository to connect). A step is done from the instance's or the person's own state, never
from a click in the interface: a repository exists, a deploy succeeded, the person ran a query.
Instance-wide steps tick for every administrator alike; the person's own steps (a passkey, a
table opened, a query run) only for that person.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.orm import Session

from lakana_core.auth import PrincipalRef
from lakana_core.grants.service import GrantService
from lakana_core.identity.sessions import utc_now
from lakana_core.models import (
    CiTrustRule,
    DeployKind,
    DeployRun,
    DeployStatus,
    InterfaceState,
    Passkey,
    Principal,
    PrincipalType,
    QueryRun,
    QueryRunStatus,
    RecentOpen,
    Repository,
    SignInLink,
    TableRead,
)

StepId = Literal[
    "connect_repository",
    "invite_team",
    "add_passkey",
    "deploy_project",
    "deploy_project_local",
    "ci_deploy",
    "open_table",
    "run_query",
]
"""One row of Get started."""

Audience = Literal["admin", "deployer", "reader", "none"]
"""Whom the list is for: an administrator, a person who may deploy, one who may read, nobody."""

_STEPS: dict[tuple[Audience, bool], tuple[StepId, ...]] = {
    ("admin", False): (
        "connect_repository",
        "invite_team",
        "add_passkey",
        "deploy_project",
        "ci_deploy",
    ),
    ("admin", True): ("add_passkey", "deploy_project_local", "open_table", "run_query"),
    ("deployer", False): ("deploy_project", "open_table", "run_query"),
    ("deployer", True): ("deploy_project", "open_table", "run_query"),
    ("reader", False): ("open_table", "run_query"),
    ("reader", True): ("open_table", "run_query"),
    ("none", False): (),
    ("none", True): (),
}
"""The steps of each audience, in local mode (`True`) and on a team server (`False`)."""


@dataclass(frozen=True)
class GetStarted:
    """The Get started list of one person.

    Parameters
    ----------
    audience
        ``admin`` (holds `ADMIN` on the instance), ``deployer`` (may deploy in some
        environment), ``reader`` (has any other grant) or ``none`` (no access yet).
    local
        Whether the server runs in local mode.
    steps
        Each step's id and whether it is done, in the order shown; empty for ``none``.
    hidden
        Whether the person hid the list.
    complete
        Whether every step is done; never for an empty list.
    """

    audience: Audience
    local: bool
    steps: tuple[tuple[StepId, bool], ...]
    hidden: bool
    complete: bool


def _audience(grants: GrantService, p: PrincipalRef) -> Audience:
    """Say whom the list is for, as whoami's flags do.

    Parameters
    ----------
    grants
        The grant service, asked about the caller only.
    p
        The caller.

    Returns
    -------
    Audience
        The first that holds of admin, deployer, reader; else none.
    """
    if grants.is_admin(p):
        return "admin"
    if grants.may_deploy_somewhere(p):
        return "deployer"
    if grants.has_any_grant(p):
        return "reader"
    return "none"


def _any(session: Session, *conditions: ColumnElement[bool]) -> bool:
    """Say whether a row matching the conditions exists.

    Parameters
    ----------
    session
        Database session.
    *conditions
        The conditions of one `EXISTS`.

    Returns
    -------
    bool
        Whether such a row exists.
    """
    return bool(session.execute(select(exists().where(*conditions))).scalar())


def _checks(session: Session, me: uuid.UUID) -> dict[StepId, Callable[[], bool]]:
    """Say how each step is found done; each is asked only when the person's list has it.

    Parameters
    ----------
    session
        Database session.
    me
        The person's id.

    Returns
    -------
    dict
        Step id → a function answering whether it is done.
    """

    def deployed() -> bool:
        """Say whether a deploy (not a dry run or a rollback) succeeded in any environment.

        Returns
        -------
        bool
            Whether one did.
        """
        return _any(
            session, DeployRun.kind == DeployKind.DEPLOY, DeployRun.status == DeployStatus.SUCCEEDED
        )

    return {
        "connect_repository": lambda: _any(session, Repository.id.is_not(None)),
        "invite_team": lambda: (
            _any(
                session,
                Principal.id != me,
                Principal.type == PrincipalType.USER,
                Principal.is_active.is_(True),
            )
            or _any(
                session,
                SignInLink.kind == "invite",
                SignInLink.used_at.is_(None),
                SignInLink.withdrawn_at.is_(None),
                SignInLink.expires_at > utc_now(),
            )
        ),
        "add_passkey": lambda: _any(
            session, Passkey.principal_id == me, Passkey.disabled_at.is_(None)
        ),
        "deploy_project": deployed,
        "deploy_project_local": deployed,
        "ci_deploy": lambda: _any(session, CiTrustRule.enabled.is_(True)),
        "open_table": lambda: (
            _any(session, RecentOpen.principal_id == me, RecentOpen.kind == "table")
            or _any(session, TableRead.principal_id == me)
        ),
        "run_query": lambda: _any(
            session,
            QueryRun.principal_id == me,
            QueryRun.status == QueryRunStatus.SUCCEEDED,
            # Ad hoc only: a pipeline's SQL task runs as a query run of the run's principal.
            QueryRun.task_run_id.is_(None),
        ),
    }


def get_started(
    session: Session, grants: GrantService, p: PrincipalRef, *, local: bool
) -> GetStarted:
    """Build the caller's Get started list.

    About the caller and the instance's own state only, so it needs no authorization and
    records nothing.

    Parameters
    ----------
    session
        Database session; nothing is written.
    grants
        The grant service, asked about the caller only.
    p
        The caller.
    local
        Whether the server runs in local mode.

    Returns
    -------
    GetStarted
        The audience, the steps with whether each is done, and whether the list is hidden or
        complete.
    """
    audience = _audience(grants, p)
    checks = _checks(session, p.id)
    steps = tuple((step, checks[step]()) for step in _STEPS[audience, local])
    state = session.get(InterfaceState, p.id)
    return GetStarted(
        audience=audience,
        local=local,
        steps=steps,
        hidden=state is not None and state.get_started_hidden,
        complete=bool(steps) and all(done for _, done in steps),
    )
