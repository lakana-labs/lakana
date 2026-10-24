"""What a CI token may do: one deploy of the signed commit, then only reading that run.

Covers the authorizer's narrowing and every `DEPLOY` check in the source tree:
`test_every_deploy_check_is_covered` fails when a new one appears without a decision here.
"""

from __future__ import annotations

import ast
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest
from ci_trust_test_helpers import (
    NOW,
    OTHER_SHA,
    SHA,
    CiWorld,
    add_rule,
    authenticate,
    ci_world,
    exchange,
    rules,
    scope_of,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.errors import NotFoundError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath
from lakana_core.jobs.service import JobService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    CiToken,
    DeployKind,
    DeployRun,
    DeployStatus,
    Grant,
    Principal,
)
from lakana_core.projects.deploys import DeployService


@pytest.fixture
def world(session: Session, tmp_path: Path) -> CiWorld:
    """Build the CI world with one rule for ``prod``/``analytics``.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.

    Returns
    -------
    CiWorld
        The world.
    """
    built = ci_world(session, tmp_path)
    add_rule(session, built)
    return built


@pytest.fixture
def ci(session: Session, world: CiWorld) -> PrincipalRef:
    """Exchange an ID token and authenticate with the CI token.

    Parameters
    ----------
    session
        Database session.
    world
        The world.

    Returns
    -------
    PrincipalRef
        The CI-scoped principal.
    """
    return authenticate(session, exchange(session))


def _authz(session: Session) -> AuditingAuthorization:
    """Build the production authorizer: grants, with audited denials.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    AuditingAuthorization
        The authorizer.
    """
    return AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(1000))


def _deploys(session: Session) -> DeployService:
    """Build a deploy service with the production authorizer.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    DeployService
        The service.
    """
    return DeployService(session, _authz(session))


def _denials(session: Session) -> list[AuditEvent]:
    """Read the recorded denials.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of AuditEvent
        The rows.
    """
    return list(
        session.execute(
            select(AuditEvent).where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
        ).scalars()
    )


# -- the authorizer ----------------------------------------------------------------------


def test_a_ci_token_may_deploy_to_its_environment_only(
    session: Session, world: CiWorld, ci: PrincipalRef
) -> None:
    authz = GrantAuthorization(session)
    authz.authorize(ci, Action.DEPLOY, Securable.environment_scope("prod"))
    grants = GrantService(session, AllowAllAuthorization())
    grants.grant(
        world.admin,
        principal="deployer",
        action=Action.DEPLOY,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    with pytest.raises(PermissionDenied, match="only to 'prod'"):
        authz.authorize(ci, Action.DEPLOY, Securable.environment_scope("dev"))
    # Not even the ADMIN role of its principal widens what the token may do.
    grants.grant(world.admin, principal="deployer", role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    bronze = NamespacePath.from_parts(["bronze"])
    for action, securable in [
        (Action.READ_ENVIRONMENT, Securable.environment_scope("prod")),
        (Action.RUN, Securable.environment_scope("prod")),
        (Action.SUBMIT_QUERY, Securable.environment_scope("prod")),
        (Action.MANAGE_ENVIRONMENT, Securable.environment_scope("prod")),
        (Action.READ_NAMESPACE, Securable.namespace_scope("prod", bronze)),
        (Action.READ_TABLE, Securable.table_scope("prod", bronze, "events")),
        (Action.DEPLOY, Securable.namespace_scope("prod", bronze)),
        (Action.DEPLOY, Securable.instance_scope()),
        (Action.MANAGE_GRANT, Securable.instance_scope()),
        (Action.MANAGE_TOKEN, Securable.instance_scope()),
        (Action.MANAGE_REPOSITORY, Securable.instance_scope()),
        (Action.READ_AUDIT_LOG, Securable.instance_scope()),
    ]:
        assert not authz.permits(ci, action, securable), action
    assert authz.explain(ci, Action.READ_ENVIRONMENT, Securable.environment_scope("prod")).reason


def test_the_scope_narrows_and_never_widens(session: Session, world: CiWorld) -> None:
    add_rule(session, world, environment="dev")
    ci = authenticate(session, exchange(session, environment="dev"))
    with pytest.raises(PermissionDenied, match="may not DEPLOY"):
        GrantAuthorization(session).authorize(ci, Action.DEPLOY, Securable.environment_scope("dev"))


# -- DeployService.submit ------------------------------------------------------------------


def test_a_deploy_is_pinned_to_the_signed_commit_and_records_its_token(
    session: Session, ci: PrincipalRef
) -> None:
    view = _deploys(session).submit(ci, "prod", repository="analytics", kind=DeployKind.DEPLOY)
    assert view.requested_ref == SHA
    run = session.get(DeployRun, view.id)
    assert run is not None and run.ci_token_id == scope_of(ci).token_id
    token = session.get(CiToken, scope_of(ci).token_id)
    assert token is not None and token.used_at is not None


def test_a_deploy_of_the_signed_commit_by_name_is_accepted(
    session: Session, ci: PrincipalRef
) -> None:
    view = _deploys(session).submit(
        ci, "prod", repository="analytics", kind=DeployKind.DEPLOY, ref=SHA.upper()
    )
    assert view.requested_ref == SHA


@pytest.mark.parametrize("ref", [OTHER_SHA, "main", "refs/heads/main", "HEAD", SHA[:12]])
def test_a_deploy_of_another_commit_or_a_branch_is_refused(
    session: Session, ci: PrincipalRef, ref: str
) -> None:
    with pytest.raises(PermissionDenied, match=SHA):
        _deploys(session).submit(
            ci, "prod", repository="analytics", kind=DeployKind.DEPLOY, ref=ref
        )
    assert session.execute(select(DeployRun)).first() is None
    assert scope_of(authenticate_again(session, ci)).used is False
    (denial,) = _denials(session)
    assert denial.detail["reason"] == "ci_token"


def authenticate_again(session: Session, ci: PrincipalRef) -> PrincipalRef:
    """Read a CI token's state again, as the next request would.

    Parameters
    ----------
    session
        Database session.
    ci
        A CI-scoped principal.

    Returns
    -------
    PrincipalRef
        A principal with a fresh scope.
    """
    from lakana_core.ci_oidc.exchange import resolve_ci_scope

    token = session.get(CiToken, scope_of(ci).token_id)
    assert token is not None
    session.refresh(token)
    scope = resolve_ci_scope(session, token, now=NOW, require_unexpired=True)
    assert scope is not None
    return PrincipalRef(id=ci.id, name=ci.name, type=ci.type, ci_scope=scope)


def test_a_dry_run_may_name_any_ref(session: Session, ci: PrincipalRef) -> None:
    view = _deploys(session).submit(
        ci, "prod", repository="analytics", kind=DeployKind.DRY_RUN, ref="feature/x"
    )
    assert view.requested_ref == "feature/x"


def test_another_repository_is_refused(session: Session, ci: PrincipalRef) -> None:
    with pytest.raises(PermissionDenied, match="only 'analytics'"):
        _deploys(session).submit(ci, "prod", repository="other", kind=DeployKind.DEPLOY)


def test_another_environment_is_refused(session: Session, ci: PrincipalRef) -> None:
    with pytest.raises(PermissionDenied):
        _deploys(session).submit(ci, "dev", repository="analytics", kind=DeployKind.DEPLOY)


def test_a_rollback_is_refused(session: Session, ci: PrincipalRef) -> None:
    with pytest.raises(PermissionDenied, match="rollback"):
        _deploys(session).submit(
            ci, "prod", repository="analytics", kind=DeployKind.ROLLBACK, to_previous=True
        )


def test_a_kind_the_rule_does_not_allow_is_refused(session: Session, world: CiWorld) -> None:
    for rule in rules(session).list(world.admin):
        rules(session).update(world.admin, rule.id, kinds=["dry_run"])
    ci = authenticate(session, exchange(session))
    with pytest.raises(PermissionDenied, match="dry_run"):
        _deploys(session).submit(ci, "prod", repository="analytics", kind=DeployKind.DEPLOY)
    _deploys(session).submit(ci, "prod", repository="analytics", kind=DeployKind.DRY_RUN)


def test_a_deploy_without_a_signed_commit_is_refused(session: Session, world: CiWorld) -> None:
    ci = authenticate(session, exchange(session))
    session.execute(update(CiToken).values(sha=None))
    session.commit()
    ci = authenticate_again(session, ci)
    with pytest.raises(PermissionDenied, match="no commit"):
        _deploys(session).submit(ci, "prod", repository="analytics", kind=DeployKind.DEPLOY)


def test_a_token_submits_once(session: Session, ci: PrincipalRef) -> None:
    deploys = _deploys(session)
    deploys.submit(ci, "prod", repository="analytics", kind=DeployKind.DRY_RUN)
    for kind in (DeployKind.DRY_RUN, DeployKind.DEPLOY):
        with pytest.raises(PermissionDenied, match="already submitted"):
            deploys.submit(ci, "prod", repository="analytics", kind=kind)
    with pytest.raises(PermissionDenied, match="already submitted"):
        deploys.submit(authenticate_again(session, ci), "prod", repository="analytics", kind=kind)
    assert len(session.execute(select(DeployRun)).scalars().all()) == 1


def test_a_submission_after_the_rule_was_disabled_is_refused(
    session: Session, world: CiWorld, ci: PrincipalRef
) -> None:
    (rule,) = rules(session).list(world.admin)
    rules(session).disable(world.admin, rule.id)
    with pytest.raises(PermissionDenied, match=r"already submitted|no longer valid"):
        _deploys(session).submit(ci, "prod", repository="analytics", kind=DeployKind.DEPLOY)
    assert session.execute(select(DeployRun)).first() is None


def test_a_submission_after_expiry_is_refused(session: Session, ci: PrincipalRef) -> None:
    session.execute(update(CiToken).values(expires_at=NOW - timedelta(seconds=1)))
    session.commit()
    with pytest.raises(PermissionDenied):
        _deploys(session).submit(ci, "prod", repository="analytics", kind=DeployKind.DEPLOY)
    assert session.execute(select(DeployRun)).first() is None


def test_the_token_still_needs_its_own_deploy_grant(
    session: Session, world: CiWorld, ci: PrincipalRef
) -> None:
    _revoke_grants(session, world)
    with pytest.raises(PermissionDenied, match="may not DEPLOY"):
        _deploys(session).submit(ci, "prod", repository="analytics", kind=DeployKind.DEPLOY)


def _revoke_grants(session: Session, world: CiWorld) -> None:
    """Revoke every grant of the deployer.

    Parameters
    ----------
    session
        Database session.
    world
        The world.
    """
    session.execute(
        update(Grant).where(Grant.principal_id == world.deployer.id).values(revoked_at=NOW)
    )
    session.commit()


# -- reading ----------------------------------------------------------------------------


def test_the_token_reads_its_own_run_only(
    session: Session, world: CiWorld, ci: PrincipalRef
) -> None:
    deploys = _deploys(session)
    other = DeployService(session, AllowAllAuthorization()).submit(
        world.deployer, "prod", repository="analytics", kind=DeployKind.DRY_RUN
    )
    with pytest.raises(NotFoundError, match=str(other.id)):
        deploys.get(ci, "prod", other.id)
    own = deploys.submit(ci, "prod", repository="analytics", kind=DeployKind.DEPLOY)
    used = authenticate_again(session, ci)
    assert scope_of(used).used
    assert deploys.get(used, "prod", own.id).id == own.id
    with pytest.raises(NotFoundError, match=str(other.id)):
        deploys.get(used, "prod", other.id)
    with pytest.raises(PermissionDenied):
        deploys.history_page(used, "prod")
    with pytest.raises(PermissionDenied):
        deploys.deployments(used, "prod")


# -- every other DEPLOY check refuses a CI token -------------------------------------------


def _refusals(session: Session) -> dict[str, Callable[[PrincipalRef], object]]:
    """Name each `DEPLOY` check, other than submit and get, by a call that reaches it.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    dict of str to callable
        ``module:function`` to the call.
    """
    deploys = _deploys(session)
    jobs = JobService(session, _authz(session))
    return {
        "projects/deploys.py:undeploy": lambda p: deploys.undeploy(p, "prod", "analytics"),
        "jobs/service.py:pause": lambda p: jobs.pause(p, "prod", "analytics", "nightly"),
        "jobs/service.py:resume": lambda p: jobs.resume(p, "prod", "analytics", "nightly"),
        "jobs/service.py:notify_test": lambda p: jobs.notify_test(p, "prod", "analytics", "x"),
        "jobs/service.py:delivery": lambda p: jobs.delivery(p, "prod", uuid.uuid4()),
    }


@pytest.mark.parametrize(
    "site",
    [
        "projects/deploys.py:undeploy",
        "jobs/service.py:pause",
        "jobs/service.py:resume",
        "jobs/service.py:notify_test",
        "jobs/service.py:delivery",
    ],
)
def test_every_other_deploy_check_refuses_a_ci_token(
    session: Session, ci: PrincipalRef, site: str
) -> None:
    with pytest.raises(PermissionDenied, match="CI token"):
        _refusals(session)[site](ci)
    (denial,) = _denials(session)
    assert denial.detail == {"action": "DEPLOY", "reason": "ci_token"}


def test_a_ci_token_may_not_explain_its_principals_access(
    session: Session, ci: PrincipalRef
) -> None:
    with pytest.raises(PermissionDenied):
        GrantService(session, _authz(session)).explain(
            ci, "deployer", Securable.environment_scope("prod")
        )


# -- the worker's re-check ------------------------------------------------------------------


def _queued(session: Session, ci: PrincipalRef, kind: DeployKind = DeployKind.DEPLOY) -> DeployRun:
    """Queue a run with the CI token and return its row.

    Parameters
    ----------
    session
        Database session.
    ci
        The CI-scoped principal.
    kind
        What to queue.

    Returns
    -------
    DeployRun
        The row.
    """
    view = _deploys(session).submit(ci, "prod", repository="analytics", kind=kind)
    run = session.get(DeployRun, view.id)
    assert run is not None
    return run


def _plain(world: CiWorld) -> PrincipalRef:
    """Return the deployer as the worker builds it, from its row.

    Parameters
    ----------
    world
        The world.

    Returns
    -------
    PrincipalRef
        The principal, without a scope.
    """
    return PrincipalRef(id=world.deployer.id, name="deployer", type=world.deployer.type)


def test_the_worker_recheck_resolves_and_returns_the_scope(
    session: Session, world: CiWorld, ci: PrincipalRef
) -> None:
    run = _queued(session, ci)
    scope = _deploys(session).authorize_deploy(_plain(world), "prod", run)
    assert scope is not None and scope.token_id == scope_of(ci).token_id and scope.sha == SHA
    plain = DeployService(session, AllowAllAuthorization()).submit(
        world.deployer, "prod", repository="analytics", kind=DeployKind.DRY_RUN
    )
    plain_run = session.get(DeployRun, plain.id)
    assert _deploys(session).authorize_deploy(_plain(world), "prod", plain_run) is None


def test_the_worker_recheck_refuses_a_disabled_rule(
    session: Session, world: CiWorld, ci: PrincipalRef
) -> None:
    run = _queued(session, ci)
    (rule,) = rules(session).list(world.admin)
    rules(session).disable(world.admin, rule.id)
    with pytest.raises(PermissionDenied, match="no longer valid"):
        _deploys(session).authorize_deploy(_plain(world), "prod", run)


def test_the_worker_recheck_refuses_a_deactivated_principal(
    session: Session, world: CiWorld, ci: PrincipalRef
) -> None:
    run = _queued(session, ci)
    row = session.get(Principal, world.deployer.id)
    assert row is not None
    row.is_active = False
    session.commit()
    with pytest.raises(PermissionDenied, match="no longer valid"):
        _deploys(session).authorize_deploy(_plain(world), "prod", run)


def test_the_worker_recheck_refuses_a_missing_token(
    session: Session, world: CiWorld, ci: PrincipalRef
) -> None:
    run = _queued(session, ci)
    run.ci_token_id = uuid.uuid4()
    session.commit()
    with pytest.raises(PermissionDenied, match="no longer valid"):
        _deploys(session).authorize_deploy(_plain(world), "prod", run)


@pytest.mark.parametrize(
    "change",
    [
        {"requested_ref": OTHER_SHA},
        {"requested_ref": "main"},
        {"requested_ref": None},
        {"kind": DeployKind.ROLLBACK},
        {"principal_id": "other"},
    ],
)
def test_the_worker_recheck_refuses_a_run_the_token_could_not_have_queued(
    session: Session, world: CiWorld, ci: PrincipalRef, change: dict[str, object]
) -> None:
    run = _queued(session, ci)
    p = _plain(world)
    for name, value in change.items():
        if value == "other":
            other = Principal(id=uuid.uuid4(), name="x", type=world.deployer.type, is_active=True)
            session.add(other)
            session.flush()
            GrantService(session, AllowAllAuthorization()).grant(
                world.admin,
                principal="x",
                action=Action.DEPLOY,
                kind=SecurableKind.ENVIRONMENT,
                environment="prod",
            )
            value = other.id
            p = PrincipalRef(id=other.id, name="x", type=other.type)
        setattr(run, name, value)
    session.commit()
    with pytest.raises(PermissionDenied):
        _deploys(session).authorize_deploy(p, "prod", run)


def test_the_worker_recheck_refuses_a_run_of_another_repository(
    session: Session, world: CiWorld, ci: PrincipalRef
) -> None:
    from lakana_core.models import Repository

    run = _queued(session, ci, DeployKind.DRY_RUN)
    other = session.execute(select(Repository).where(Repository.name == "other")).scalar_one()
    run.repository_id = other.id
    session.commit()
    with pytest.raises(PermissionDenied, match="only 'analytics'"):
        _deploys(session).authorize_deploy(_plain(world), "prod", run)


def test_the_worker_recheck_still_needs_the_grant(
    session: Session, world: CiWorld, ci: PrincipalRef
) -> None:
    run = _queued(session, ci)
    _revoke_grants(session, world)
    with pytest.raises(PermissionDenied):
        _deploys(session).authorize_deploy(_plain(world), "prod", run)
    assert run.status is DeployStatus.QUEUED


# -- the call-site list --------------------------------------------------------------------

_DEPLOY_CHECKS = {
    "lakana-core/src/lakana_core/projects/deploys.py:DeployService.submit": (
        1,
        "one pinned submission",
    ),
    "lakana-core/src/lakana_core/projects/deploys.py:DeployService.get": (2, "its own run only"),
    "lakana-core/src/lakana_core/projects/deploys.py:DeployService.undeploy": (1, "refused"),
    "lakana-core/src/lakana_core/projects/deploys.py:DeployService.authorize_deploy": (
        1,
        "the worker's re-check",
    ),
    # pause, resume and notify_test check through `_deployer_job`.
    "lakana-core/src/lakana_core/jobs/service.py:JobService._deployer_job": (1, "refused"),
    "lakana-core/src/lakana_core/jobs/service.py:JobService.delivery": (2, "refused"),
    "lakana-core/src/lakana_core/grants/explain.py:<module>": (1, "a list of actions, no check"),
    "lakana-core/src/lakana_core/grants/authorization.py:GrantAuthorization._ci_scope_refusal": (
        1,
        "the narrowing",
    ),
    "lakana-core/src/lakana_core/ci_oidc/scope.py:ci_refusal": (1, "audits a refusal, no check"),
}
"""Every function in the source tree that names the `DEPLOY` action, by qualified name, with how
many times it names it, and what a CI token meets there.

The worker calls `DeployService.authorize_deploy` (`lakana_worker.deploy.run_deploy`); it names
no `DEPLOY` itself. The server's routes call the services above. A second check in a listed
function changes its count, so it fails the test too."""


def _deploy_mentions(tree: ast.AST) -> Counter[str]:
    """Count the mentions of the `DEPLOY` action per enclosing function, by qualified name.

    A mention is an attribute ``.DEPLOY`` of anything but `DeployKind` (whose ``DEPLOY`` is a kind
    of run, not the action), or the text ``"DEPLOY"``.

    Parameters
    ----------
    tree
        A parsed module.

    Returns
    -------
    Counter of str
        ``Class.method`` (or a function's or ``<module>``'s name) to its number of mentions.
    """
    found: Counter[str] = Counter()

    def visit(node: ast.AST, where: tuple[str, ...]) -> None:
        """Walk the tree, remembering the enclosing classes and functions.

        Parameters
        ----------
        node
            The node.
        where
            The names of the enclosing classes and functions, outermost first.
        """
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            where = (*where, node.name)
        action = isinstance(node, ast.Attribute) and node.attr == "DEPLOY"
        if action and isinstance(node.value, ast.Name) and node.value.id == "DeployKind":
            action = False
        if action or (isinstance(node, ast.Constant) and node.value == "DEPLOY"):
            found[".".join(where) or "<module>"] += 1
        for child in ast.iter_child_nodes(node):
            visit(child, where)

    visit(tree, ())
    return found


def _deploy_checks(packages: Path) -> dict[str, int]:
    """Find every function in `packages/*/src` that names the `DEPLOY` action.

    Parameters
    ----------
    packages
        The `packages` directory.

    Returns
    -------
    dict of str to int
        ``path:qualified name`` to its number of mentions.
    """
    found: dict[str, int] = {}
    for path in packages.glob("*/src/**/*.py"):
        relative = str(path.relative_to(packages))
        if "/migrations/" in relative or relative.endswith("lakana_core/auth.py"):
            continue
        for where, count in _deploy_mentions(ast.parse(path.read_text(encoding="utf-8"))).items():
            found[f"{relative}:{where}"] = count
    return found


@pytest.mark.slow
def test_every_deploy_check_is_covered() -> None:
    found = _deploy_checks(Path(__file__).resolve().parents[2])
    expected = {key: count for key, (count, _) in _DEPLOY_CHECKS.items()}
    assert found == expected, sorted(set(found.items()) ^ set(expected.items()))


def test_a_new_check_in_a_listed_function_or_a_new_class_is_noticed() -> None:
    listed = """
class DeployService:
    def get(self):
        if self._authz.permits(p, Action.DEPLOY, scope):
            self._authz.authorize(p, Action.DEPLOY, scope)
"""
    assert _deploy_mentions(ast.parse(listed)) == Counter({"DeployService.get": 2})
    second = listed + "        self._authz.authorize(p, Action.DEPLOY, other)\n"
    assert _deploy_mentions(ast.parse(second)) == Counter({"DeployService.get": 3})
    other_class = listed + "class Other:\n    def get(self):\n        check(Action.DEPLOY)\n"
    assert _deploy_mentions(ast.parse(other_class)) == Counter(
        {"DeployService.get": 2, "Other.get": 1}
    )
    kind = "def f():\n    return DeployKind.DEPLOY, 'DEPLOY'\n"
    assert _deploy_mentions(ast.parse(kind)) == Counter({"f": 1})


def test_check_ci_request_and_commit_refuse_without_an_auditing_authorizer(
    session: Session, ci: PrincipalRef
) -> None:
    from lakana_core.ci_oidc.scope import check_ci_commit, check_ci_request, refuse_ci_token

    scope = scope_of(ci)
    plain = GrantAuthorization(session)
    with pytest.raises(PermissionDenied, match="only to 'prod'"):
        check_ci_request(
            plain,
            ci,
            scope,
            environment="dev",
            environment_id=uuid.uuid4(),
            repository="analytics",
            repository_id=scope.repository_id,
            kind=DeployKind.DEPLOY,
            ref=None,
        )
    check_ci_commit(plain, ci, scope, DeployKind.DEPLOY, SHA)
    check_ci_commit(plain, ci, scope, DeployKind.DRY_RUN, OTHER_SHA)
    with pytest.raises(PermissionDenied, match=OTHER_SHA):
        check_ci_commit(plain, ci, scope, DeployKind.DEPLOY, OTHER_SHA)
    refuse_ci_token(plain, PrincipalRef(id=ci.id, name=ci.name, type=ci.type), "prod", "x")
    assert _denials(session) == []
