"""Every call site of the three delegation actions asks for the ADMIN role."""

from __future__ import annotations

import ast
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import Action, PrincipalRef, Securable, SecurableKind
from lakana_core.errors import PermissionDenied, ValidationError
from lakana_core.grants.admin import ADMIN_ONLY_ACTIONS, admin_only_message, holds_instance_admin
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identity.groups import GroupService
from lakana_core.identity.pats import PatService
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Grant,
    Group,
    GroupMembership,
    Principal,
    PrincipalType,
)


def _principal(session: Session, name: str) -> PrincipalRef:
    """Persist an active principal.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name.

    Returns
    -------
    PrincipalRef
        A reference to the new principal.
    """
    ref = PrincipalRef(id=uuid.uuid4(), name=name, type=PrincipalType.USER)
    session.add(Principal(id=ref.id, type=ref.type, name=name, is_active=True))
    session.commit()
    return ref


def _row(session: Session, **fields: object) -> Grant:
    """Persist a grant row directly, as an older release could have written it.

    Parameters
    ----------
    session
        Database session.
    **fields
        The columns of the row.

    Returns
    -------
    Grant
        The persisted row.
    """
    row = Grant(id=uuid.uuid4(), granted_at=datetime.now(UTC), **fields)
    session.add(row)
    session.commit()
    return row


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


def _denials(session: Session) -> list[dict[str, object]]:
    """Read the details of the recorded denials, oldest first.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list[dict[str, object]]
        One detail dictionary per `AUTHORIZATION_DENIED` row.
    """
    rows = session.execute(
        select(AuditEvent)
        .where(AuditEvent.event_type == AuditEventType.AUTHORIZATION_DENIED)
        .order_by(AuditEvent.occurred_at)
    ).scalars()
    return [row.detail for row in rows]


@dataclass(frozen=True)
class Actors:
    """The principals and rows every call-site test starts from.

    Parameters
    ----------
    admin
        Holds the `ADMIN` role at instance scope.
    holder
        Holds each of the three actions as a single-action grant and no role.
    target
        A member of the group, with a viewer role.
    group
        The name of the group.
    grant_id
        The id of the target's viewer grant.
    """

    admin: PrincipalRef
    holder: PrincipalRef
    target: PrincipalRef
    group: str
    grant_id: uuid.UUID


@pytest.fixture
def actors(session: Session) -> Actors:
    """Persist the principals, the group and the grants.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    Actors
        The principals, the group name and the grant id.
    """
    admin, holder, target = (_principal(session, name) for name in ("ada", "bob", "carol"))
    _row(
        session,
        principal_id=admin.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
        granted_by=admin.id,
    )
    for action in sorted(ADMIN_ONLY_ACTIONS):
        _row(
            session,
            principal_id=holder.id,
            action=action,
            kind=SecurableKind.INSTANCE,
            granted_by=admin.id,
        )
    team = Group(id=uuid.uuid4(), name="team")
    session.add(team)
    session.flush()
    session.add(GroupMembership(group_id=team.id, principal_id=target.id))
    session.commit()
    viewer = _row(
        session,
        principal_id=target.id,
        role=Role.VIEWER,
        kind=SecurableKind.INSTANCE,
        granted_by=admin.id,
    )
    return Actors(admin, holder, target, "team", viewer.id)


def _refused_then_allowed(
    session: Session, actors: Actors, action: Action, call: Callable[[PrincipalRef], object]
) -> None:
    """Assert the rule for one operation.

    Parameters
    ----------
    session
        Database session.
    actors
        The principals of the scenario.
    action
        The action the operation needs.
    call
        The operation, run as the given principal.
    """
    with pytest.raises(PermissionDenied) as caught:
        call(actors.holder)
    assert str(caught.value) == admin_only_message(action)
    assert _denials(session) == [{"action": action.value, "reason": "not_instance_admin"}]
    call(actors.admin)
    assert len(_denials(session)) == 1


_GRANT_OPERATIONS: dict[str, Callable[[GrantService, Actors, PrincipalRef], object]] = {
    "grant": lambda service, actors, p: service.grant(
        p, principal=actors.target.name, role=Role.ANALYST, kind=SecurableKind.INSTANCE
    ),
    "list_grants": lambda service, actors, p: service.list_grants(
        p, principal_name=actors.target.name
    ),
    "explain": lambda service, actors, p: service.explain(
        p, actors.target.name, Securable.instance_scope()
    ),
    "revoke": lambda service, actors, p: service.revoke(p, actors.grant_id),
}


@pytest.mark.parametrize("operation", sorted(_GRANT_OPERATIONS))
def test_every_grant_operation_needs_the_admin_role(
    session: Session, actors: Actors, operation: str
) -> None:
    service = GrantService(session, _auditing(session))
    _refused_then_allowed(
        session,
        actors,
        Action.MANAGE_GRANT,
        lambda p: _GRANT_OPERATIONS[operation](service, actors, p),
    )


@pytest.mark.parametrize("action", sorted(ADMIN_ONLY_ACTIONS))
@pytest.mark.parametrize("subject", ["principal", "group"])
@pytest.mark.parametrize(
    "securable",
    [
        {"kind": SecurableKind.INSTANCE},
        {"kind": SecurableKind.ENVIRONMENT, "environment": "dev"},
    ],
)
def test_a_single_action_grant_of_the_three_is_refused_for_everyone(
    session: Session,
    actors: Actors,
    action: Action,
    subject: str,
    securable: dict[str, object],
) -> None:
    """At any scope, to anyone. The role on the instance is the one way to hold them."""
    service = GrantService(session, _auditing(session))
    to = {"principal": actors.target.name} if subject == "principal" else {"group": actors.group}
    before = session.execute(select(Grant.id)).scalars().all()
    with pytest.raises(ValidationError) as caught:
        service.grant(actors.admin, action=action, **to, **securable)  # ty: ignore[invalid-argument-type]
    assert str(caught.value) == (
        f"{action.value} comes only with the ADMIN role on the instance; grant that role "
        "instead, or nothing."
    )
    assert session.execute(select(Grant.id)).scalars().all() == before


def test_other_single_actions_and_the_role_are_still_granted(
    session: Session, actors: Actors
) -> None:
    service = GrantService(session, _auditing(session))
    service.grant(
        actors.admin,
        principal=actors.target.name,
        action=Action.MINT_TASK_TOKEN,
        kind=SecurableKind.INSTANCE,
    )
    service.grant(
        actors.admin, principal=actors.target.name, role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    service.grant(
        actors.target, principal=actors.holder.name, role=Role.VIEWER, kind=SecurableKind.INSTANCE
    )


def test_a_caller_who_may_not_grant_learns_nothing_about_the_refusal(
    session: Session, actors: Actors
) -> None:
    service = GrantService(session, _auditing(session))
    with pytest.raises(PermissionDenied):
        service.grant(
            actors.target,
            principal=actors.target.name,
            action=Action.MANAGE_GRANT,
            kind=SecurableKind.INSTANCE,
        )


_GROUP_OPERATIONS: dict[str, Callable[[GroupService, Actors, PrincipalRef], object]] = {
    "create_group": lambda service, actors, p: service.create_group(p, name=f"g-{p.name}"),
    "list_groups": lambda service, actors, p: service.list_groups(p),
    "add_member": lambda service, actors, p: service.add_member(p, actors.group, "carol"),
    "list_members": lambda service, actors, p: service.list_members(p, actors.group),
    "remove_member": lambda service, actors, p: service.remove_member(p, actors.group, "carol"),
    "delete_group": lambda service, actors, p: service.delete_group(p, actors.group),
}


@pytest.mark.parametrize("operation", list(_GROUP_OPERATIONS))
def test_every_group_operation_needs_the_admin_role(
    session: Session, actors: Actors, operation: str
) -> None:
    service = GroupService(session, _auditing(session))
    _refused_then_allowed(
        session,
        actors,
        Action.MANAGE_GROUP,
        lambda p: _GROUP_OPERATIONS[operation](service, actors, p),
    )


def test_the_holder_of_manage_group_cannot_join_the_administrators(
    session: Session, actors: Actors
) -> None:
    """The escalation the rule closes: `MANAGE_GROUP` used to be a way into an admin group."""
    admins = Group(id=uuid.uuid4(), name="admins")
    session.add(admins)
    session.commit()
    _row(
        session,
        group_id=admins.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
        granted_by=actors.admin.id,
    )
    with pytest.raises(PermissionDenied, match="needs the ADMIN role on the instance"):
        GroupService(session, _auditing(session)).add_member(actors.holder, "admins", "bob")
    assert not holds_instance_admin(session, actors.holder.id)


def test_every_token_operation_needs_the_admin_role(session: Session, actors: Actors) -> None:
    service = PatService(session, _auditing(session))
    key_ids: list[str] = []

    def issue(p: PrincipalRef) -> None:
        """Issue a token for carol and remember its key id.

        Parameters
        ----------
        p
            The acting principal.
        """
        key_ids.append(service.issue(p, "carol", name=f"by-{p.name}").key_id)

    _refused_then_allowed(session, actors, Action.MANAGE_TOKEN, issue)
    with pytest.raises(PermissionDenied, match="needs the ADMIN role on the instance"):
        service.list(actors.holder, "carol")
    assert [pat.key_id for pat in service.list(actors.admin, "carol")] == key_ids
    with pytest.raises(PermissionDenied, match="needs the ADMIN role on the instance"):
        service.revoke(actors.holder, "carol", key_ids[0])
    service.revoke(actors.admin, "carol", key_ids[0])


def test_the_holder_of_manage_token_cannot_issue_a_token_for_itself_or_an_administrator(
    session: Session, actors: Actors
) -> None:
    """No self-service exists for tokens."""
    service = PatService(session, _auditing(session))
    for subject in ("bob", "ada"):
        with pytest.raises(PermissionDenied, match="needs the ADMIN role on the instance"):
            service.issue(actors.holder, subject, name="escalation")


_GUARDED = {
    "grants/service.py": ("MANAGE_GRANT", 4),
    "identity/groups.py": ("MANAGE_GROUP", 6),
    "identity/pats.py": ("MANAGE_TOKEN", 3),
    # A CI trust rule lets a CI job act as a service account: issuing it a token, in effect.
    "ci_oidc/rules.py": ("MANAGE_TOKEN", 8),
}
_SERVICES = {
    "grants/service.py": "GrantService",
    "identity/groups.py": "GroupService",
    "identity/pats.py": "PatService",
    "ci_oidc/rules.py": "CiTrustRuleService",
}
_EXEMPT = {
    ("PatService", "verify"): "authenticates a presented token: it reads the token row and "
    "writes `last_used_at`, but it is not an administrative operation and has no caller identity "
    "to check a role of",
}
"""Public methods of the three services that need no administrator check, and why."""
_NAMES = {action.name for action in ADMIN_ONLY_ACTIONS}
_HELPER = "authorize_admin_only"
_SET = "ADMIN_ONLY_ACTIONS"


def _aliases(tree: ast.AST, name: str) -> set[str]:
    """Find every name a module binds to `name` by an import, `name` itself included.

    Parameters
    ----------
    tree
        The parsed module.
    name
        The imported name, such as ``Action``.

    Returns
    -------
    set[str]
        `name` and each ``as`` alias of an import of it.
    """
    found = {name}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            found.update(a.asname for a in node.names if a.asname and a.name.split(".")[-1] == name)
    return found


def _mentions(tree: ast.AST, *, constants: bool = False) -> list[ast.AST]:
    """Find every way a module can name one of the three actions or the set of them.

    Caught: ``Action.X`` in any spelling of the base (``auth.Action.X``, an alias of
    the import, any other object), ``Action["X"]``, ``Action("X")``, and a reference
    to ``ADMIN_ONLY_ACTIONS`` (a loop over the set reaches all three). With
    `constants`, also a string that is exactly one of the three names.

    Parameters
    ----------
    tree
        The parsed module.
    constants
        Also count a bare string equal to an action name (right for a service
        module, whose strings are sentences; wrong for a schema that lists every action).

    Returns
    -------
    list[ast.AST]
        The nodes that name one of the three actions or the set.
    """
    enums = _aliases(tree, "Action")
    return [node for node in ast.walk(tree) if _is_mention(node, enums, constants)]


def _is_mention(node: ast.AST, enums: set[str], constants: bool) -> bool:
    """Say whether one node names one of the three actions or the set of them.

    Parameters
    ----------
    node
        Any node of a module.
    enums
        The names the module imports `Action` under.
    constants
        Whether a bare string equal to an action name counts.

    Returns
    -------
    bool
        ``True`` if the node is one of the spellings `_mentions` documents.
    """
    if isinstance(node, ast.Attribute):
        return node.attr in _NAMES or node.attr == _SET
    if isinstance(node, ast.Name):
        return node.id == _SET
    if isinstance(node, ast.ImportFrom):
        return any(a.name in _NAMES or a.name == _SET for a in node.names)
    if isinstance(node, ast.Subscript):
        return (
            _is_enum(node.value, enums)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in _NAMES
        )
    if isinstance(node, ast.Call):
        return _is_enum(node.func, enums) and any(
            isinstance(a, ast.Constant) and a.value in _NAMES for a in node.args
        )
    return constants and isinstance(node, ast.Constant) and node.value in _NAMES


def _is_enum(node: ast.AST, enums: set[str]) -> bool:
    """Say whether an expression is the `Action` class under any of its spellings.

    Parameters
    ----------
    node
        The expression.
    enums
        The names the module imports `Action` under.

    Returns
    -------
    bool
        ``True`` for a name in `enums` and for an attribute ending in one.
    """
    if isinstance(node, ast.Name):
        return node.id in enums
    return isinstance(node, ast.Attribute) and node.attr in enums


def _helper_calls(
    tree: ast.AST, scope: list[ast.AST] | None = None
) -> tuple[list[ast.Call], list[ast.AST]]:
    """Find the calls of the helper and every other reference to it.

    Parameters
    ----------
    tree
        The parsed module (its imports give the helper's aliases).
    scope
        The nodes to search; every node of `tree` when omitted.

    Returns
    -------
    tuple[list[ast.Call], list[ast.AST]]
        The calls (under the helper's name, an alias of it, or as an attribute of a
        module), and the references that are not a call (passed as a value, say).
    """
    names = _aliases(tree, _HELPER)
    nodes = list(ast.walk(tree)) if scope is None else scope
    calls: list[ast.Call] = []
    called: set[int] = set()
    for node in nodes:
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id in names) or (
                isinstance(func, ast.Attribute) and func.attr in names
            ):
                calls.append(node)
                called.add(id(func))
    others: list[ast.AST] = [
        node
        for node in nodes
        if (isinstance(node, ast.Name) and node.id in names)
        or (isinstance(node, ast.Attribute) and node.attr in names)
        if id(node) not in called
    ]
    return calls, others


def _own_body_nodes(method: ast.AST) -> list[ast.AST]:
    """List the nodes of a function's own body, not those of a nested function, lambda or class.

    Parameters
    ----------
    method
        The function definition.

    Returns
    -------
    list[ast.AST]
        Every node reached without entering a nested scope.
    """
    found: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(method))
    while pending:
        node = pending.pop()
        found.append(node)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            pending.extend(ast.iter_child_nodes(node))
    return found


def _unguarded_methods(tree: ast.AST, class_name: str) -> list[str]:
    """List the public methods of a class that do not call the helper.

    The call must be in the method's own body: a call inside a nested function, a lambda
    or a class body that nothing runs does not count. This does not prove the call comes
    before the method's work (a call after it would pass); the behavioural tests above
    show that for each method that exists today.

    Parameters
    ----------
    tree
        The parsed module.
    class_name
        The service class.

    Returns
    -------
    list[str]
        The names of public methods (not starting with an underscore) that never
        call the helper, `_EXEMPT` ones left out.
    """
    unguarded: list[str] = []
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef) and cls.name == class_name):
            continue
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if method.name.startswith("_") or (class_name, method.name) in _EXEMPT:
                continue
            calls, _ = _helper_calls(tree, _own_body_nodes(method))
            if not calls:
                unguarded.append(method.name)
    return unguarded


def test_the_three_actions_are_authorized_only_through_the_helper() -> None:
    """A test enumerates the call sites. Thirteen calls, all `authorize_admin_only`."""
    core = Path(__file__).resolve().parents[1] / "src" / "lakana_core"
    for relative, (name, count) in _GUARDED.items():
        tree = ast.parse((core / relative).read_text(encoding="utf-8"))
        calls, others = _helper_calls(tree)
        assert others == [], f"{relative} refers to the helper other than by calling it"
        assert _aliases(tree, _HELPER) == {_HELPER}, (
            f"{relative} imports the helper under a new name"
        )
        assert len(calls) == count, relative
        for call in calls:
            assert [ast.unparse(argument) for argument in call.args] == [
                "self._session",
                "self._authz",
                "p",
                f"Action.{name}",
            ], relative
        inside = {
            id(node) for call in calls for argument in call.args for node in ast.walk(argument)
        }
        outside = [
            node
            for node in _mentions(tree, constants=True)
            if id(node) not in inside and not _is_grant_refusal(node, relative)
        ]
        assert outside == [], f"{relative} uses one of the three actions outside the helper"


def _is_grant_refusal(node: ast.AST, relative: str) -> bool:
    """Say whether a node is the one legitimate use of the set: `GrantService.grant` refusing it.

    Parameters
    ----------
    node
        A node `_mentions` found.
    relative
        The module's path below `lakana_core`.

    Returns
    -------
    bool
        ``True`` for a bare ``ADMIN_ONLY_ACTIONS`` name in `grants/service.py`
        (a refusal to grant the actions singly, which authorizes nothing) and for its
        import there without an alias.
    """
    if relative != "grants/service.py":
        return False
    if isinstance(node, ast.Name):
        return node.id == _SET
    return (
        isinstance(node, ast.ImportFrom)
        and node.module == "lakana_core.grants.admin"
        and all(a.asname is None for a in node.names)
    )


@pytest.mark.parametrize("relative", sorted(_SERVICES))
def test_every_public_service_method_checks_the_administrator_or_is_listed(relative: str) -> None:
    """A new method of a service cannot skip the check unnoticed."""
    core = Path(__file__).resolve().parents[1] / "src" / "lakana_core"
    tree = ast.parse((core / relative).read_text(encoding="utf-8"))
    assert _unguarded_methods(tree, _SERVICES[relative]) == [], (
        f"a public method of {_SERVICES[relative]} calls neither the helper nor is in _EXEMPT"
    )


@pytest.mark.slow
def test_no_other_product_module_asks_for_one_of_the_three_actions() -> None:
    """A new call site elsewhere must go into `_GUARDED`, and through the helper."""
    packages = Path(__file__).resolve().parents[2]
    allowed = {f"lakana-core/src/lakana_core/{relative}" for relative in _GUARDED} | {
        "lakana-core/src/lakana_core/grants/admin.py",
        # The enum defines the labels; the migration writes them as data. Neither asks for one.
        "lakana-core/src/lakana_core/auth.py",
        "lakana-core/src/lakana_core/migrations/versions/0007_grants.py",
    }
    found = sorted(
        str(path.relative_to(packages))
        for path in packages.glob("*/src/**/*.py")
        if _mentions(ast.parse(path.read_text(encoding="utf-8")), constants=True)
    )
    assert set(found) <= allowed, found


@pytest.mark.parametrize(
    "source",
    [
        "Action.MANAGE_GRANT",
        "auth.Action.MANAGE_GRANT",
        "from lakana_core.auth import Action as A\nA.MANAGE_GROUP",
        "other.MANAGE_TOKEN",
        "Action['MANAGE_GRANT']",
        "Action('MANAGE_TOKEN')",
        "from lakana_core.auth import Action as A\nA('MANAGE_GROUP')",
        "from lakana_core.auth import Action as A\nA['MANAGE_GROUP']",
        "auth.Action['MANAGE_GRANT']",
        "for action in ADMIN_ONLY_ACTIONS:\n    pass",
        "admin.ADMIN_ONLY_ACTIONS",
        "from lakana_core.auth import MANAGE_GRANT",
        "from lakana_core.grants.admin import ADMIN_ONLY_ACTIONS as THREE",
        "from lakana_core.grants.admin import ADMIN_ONLY_ACTIONS",
    ],
)
def test_the_enumeration_catches_every_spelling_of_an_action(source: str) -> None:
    """The self-test: each spelling an earlier version of the check missed."""
    assert _mentions(ast.parse(source))


def test_the_enumeration_does_not_flag_an_unrelated_action() -> None:
    """The other side of the self-test: it finds the three, not every action."""
    tree = ast.parse("Action.READ_TABLE\nAction['READ_TABLE']\nAction('READ_TABLE')")
    assert _mentions(tree) == []


def test_a_bare_string_counts_only_where_asked() -> None:
    """A service module's strings are sentences, so an exact action name there is a use."""
    tree = ast.parse("getattr(Action, 'MANAGE_GRANT')")
    assert _mentions(tree) == []
    assert _mentions(tree, constants=True)


@pytest.mark.parametrize(
    "source",
    [
        "authorize_admin_only(a, b, p, c)",
        "admin.authorize_admin_only(a, b, p, c)",
        "from x import authorize_admin_only as check\ncheck(a, b, p, c)",
    ],
)
def test_the_enumeration_counts_a_call_of_the_helper_under_any_spelling(source: str) -> None:
    calls, others = _helper_calls(ast.parse(source))
    assert len(calls) == 1
    assert [o for o in others if not isinstance(o, ast.alias)] == []


def test_the_enumeration_reports_the_helper_used_as_a_value() -> None:
    _, others = _helper_calls(ast.parse("checks = [authorize_admin_only]"))
    assert others


_SERVICE = """
class GrantService:
    def __init__(self):
        pass

    def _private(self):
        pass

    def checked(self, p):
        authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)

    def forgotten(self, p):
        return self._session.query()

    def verify(self, p):
        return p

    def nested(self, p):
        def never_called():
            authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)

        later = lambda: authorize_admin_only(self._session, self._authz, p, Action.MANAGE_GRANT)
        return self._session.query()
"""


def test_a_public_method_without_the_check_is_found() -> None:
    """The inverse check names a method that calls no helper; private and dunder ones are free."""
    assert _unguarded_methods(ast.parse(_SERVICE), "GrantService") == [
        "forgotten",
        "verify",
        "nested",
    ]
    assert _unguarded_methods(ast.parse(_SERVICE), "PatService") == []
