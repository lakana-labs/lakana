"""`authorize_admin_only`: the three delegation actions need the ADMIN role."""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    BoundTask,
    PrincipalRef,
    SecurableKind,
    TaskScope,
)
from lakana_core.errors import PermissionDenied
from lakana_core.grants.admin import (
    ADMIN_ONLY_ACTIONS,
    admin_only_message,
    authorize_admin_only,
    ineffective_grants_warning,
    ineffective_note,
)
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

_THREE = sorted(ADMIN_ONLY_ACTIONS)


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


def _grant(session: Session, **fields: object) -> Grant:
    """Persist a grant row directly, as an older release could have written it.

    Parameters
    ----------
    session
        Database session.
    **fields
        The columns of the row; `granted_by` defaults to `principal_id`.

    Returns
    -------
    Grant
        The persisted row.
    """
    granter = fields.pop("granted_by", None) or fields.get("principal_id")
    assert granter is not None
    row = Grant(id=uuid.uuid4(), granted_by=granter, granted_at=datetime.now(UTC), **fields)
    session.add(row)
    session.commit()
    return row


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


def _auditing(session: Session, per_minute: int = 10_000) -> AuditingAuthorization:
    """Build the production pairing: grants, wrapped to audit denials.

    Parameters
    ----------
    session
        Database session.
    per_minute
        The limiter's rate per principal and minute.

    Returns
    -------
    AuditingAuthorization
        The wrapper around `GrantAuthorization`.
    """
    return AuditingAuthorization(
        GrantAuthorization(session), session, DenialAuditLimiter(per_minute)
    )


def test_the_three_actions() -> None:
    assert {Action.MANAGE_GRANT, Action.MANAGE_GROUP, Action.MANAGE_TOKEN} == ADMIN_ONLY_ACTIONS
    assert admin_only_message(Action.MANAGE_GROUP) == (
        "MANAGE_GROUP needs the ADMIN role on the instance. Ask an administrator to do this, or "
        "to give you the role: lakana grant create --kind INSTANCE --principal NAME --role ADMIN"
    )


@pytest.mark.parametrize("action", _THREE)
def test_an_administrator_passes(session: Session, action: Action) -> None:
    admin = _principal(session, "ada")
    _grant(session, principal_id=admin.id, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    authorize_admin_only(session, _auditing(session), admin, action)
    assert _denials(session) == []


@pytest.mark.parametrize("action", _THREE)
def test_a_holder_of_the_single_action_is_refused_and_audited(
    session: Session, action: Action
) -> None:
    """The gap the rule closes: the action alone used to be enough."""
    bob = _principal(session, "bob")
    _grant(session, principal_id=bob.id, action=action, kind=SecurableKind.INSTANCE)
    with pytest.raises(PermissionDenied) as caught:
        authorize_admin_only(session, _auditing(session), bob, action)
    assert str(caught.value) == admin_only_message(action)
    assert _denials(session) == [{"action": action.value, "reason": "not_instance_admin"}]


def test_a_principal_with_nothing_gets_the_ordinary_denial(session: Session) -> None:
    eve = _principal(session, "eve")
    with pytest.raises(PermissionDenied) as caught:
        authorize_admin_only(session, _auditing(session), eve, Action.MANAGE_GRANT)
    assert str(caught.value) != admin_only_message(Action.MANAGE_GRANT)
    assert _denials(session) == [{"action": "MANAGE_GRANT"}]


@pytest.mark.parametrize(
    "fields",
    [
        {"role": Role.DATA_ENGINEER, "kind": SecurableKind.INSTANCE},
        {"role": Role.ADMIN, "kind": SecurableKind.ENVIRONMENT, "environment": "dev"},
        {"action": Action.MINT_TASK_TOKEN, "kind": SecurableKind.INSTANCE},
    ],
)
def test_nothing_less_than_the_role_on_the_instance_is_enough(
    session: Session, fields: dict[str, object]
) -> None:
    bob = _principal(session, "bob")
    _grant(session, principal_id=bob.id, action=Action.MANAGE_TOKEN, kind=SecurableKind.INSTANCE)
    _grant(session, principal_id=bob.id, **fields)
    with pytest.raises(PermissionDenied, match="needs the ADMIN role on the instance"):
        authorize_admin_only(session, _auditing(session), bob, Action.MANAGE_TOKEN)


def test_a_group_gives_the_role_but_never_the_single_action(session: Session) -> None:
    """A group that carries one of the three actions gives its members nothing."""
    owner = _principal(session, "owner")
    member = _principal(session, "member")
    for name in ("managers", "admins"):
        session.add(Group(id=uuid.uuid4(), name=name))
    session.commit()
    managers, admins = (
        session.execute(select(Group).where(Group.name == name)).scalar_one()
        for name in ("managers", "admins")
    )
    session.add(GroupMembership(group_id=managers.id, principal_id=member.id))
    session.commit()
    _grant(
        session,
        group_id=managers.id,
        action=Action.MANAGE_GROUP,
        kind=SecurableKind.INSTANCE,
        granted_by=owner.id,
    )
    with pytest.raises(PermissionDenied, match="needs the ADMIN role on the instance"):
        authorize_admin_only(session, _auditing(session), member, Action.MANAGE_GROUP)
    _grant(
        session,
        group_id=admins.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
        granted_by=owner.id,
    )
    session.add(GroupMembership(group_id=admins.id, principal_id=member.id))
    session.commit()
    authorize_admin_only(session, _auditing(session), member, Action.MANAGE_GROUP)


def test_an_administrator_who_loses_the_role_is_refused_on_the_next_call(
    session: Session,
) -> None:
    """Nothing is remembered between two calls with the same token."""
    ada = _principal(session, "ada")
    role = _grant(session, principal_id=ada.id, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    _grant(session, principal_id=ada.id, action=Action.MANAGE_GRANT, kind=SecurableKind.INSTANCE)
    authz = _auditing(session)
    authorize_admin_only(session, authz, ada, Action.MANAGE_GRANT)
    role.revoked_at = datetime.now(UTC)
    role.revoked_by = ada.id
    session.commit()
    with pytest.raises(PermissionDenied, match="needs the ADMIN role on the instance"):
        authorize_admin_only(session, authz, ada, Action.MANAGE_GRANT)
    role.revoked_at = role.revoked_by = None
    row = session.get(Principal, ada.id)
    assert row is not None
    row.is_active = False
    session.commit()
    with pytest.raises(PermissionDenied):
        authorize_admin_only(session, authz, ada, Action.MANAGE_GRANT)


def test_refusals_go_through_the_rate_limiter(session: Session) -> None:
    bob = _principal(session, "bob")
    _grant(session, principal_id=bob.id, action=Action.MANAGE_GRANT, kind=SecurableKind.INSTANCE)
    authz = _auditing(session, per_minute=1)
    for _ in range(3):
        with pytest.raises(PermissionDenied):
            authorize_admin_only(session, authz, bob, Action.MANAGE_GRANT)
    assert _denials(session) == [{"action": "MANAGE_GRANT", "reason": "not_instance_admin"}]


def test_a_bare_grant_authorization_refuses_without_an_audit_row(session: Session) -> None:
    """Only the auditing wrapper records; the refusal itself never depends on it."""
    bob = _principal(session, "bob")
    _grant(session, principal_id=bob.id, action=Action.MANAGE_GRANT, kind=SecurableKind.INSTANCE)
    with pytest.raises(PermissionDenied, match="needs the ADMIN role on the instance"):
        authorize_admin_only(session, GrantAuthorization(session), bob, Action.MANAGE_GRANT)
    assert _denials(session) == []


def test_allow_all_asks_for_no_role(session: Session) -> None:
    """The bootstrap's service: it grants the first administrator before one exists."""
    nobody = _principal(session, "bootstrap")
    for action in _THREE:
        authorize_admin_only(session, AllowAllAuthorization(), nobody, action)


_ALLOW_ALL = "AllowAllAuthorization"
_PRODUCT_ROOT = Path(__file__).resolve().parents[2]
_BOOTSTRAP = "lakana-core/src/lakana_core/bootstrap.py"
_DEFINITION = "lakana-core/src/lakana_core/auth.py"
_ISINSTANCE_ONLY = "lakana-core/src/lakana_core/grants/admin.py"


def _allow_all_references(tree: ast.AST) -> list[ast.AST]:
    """Find every way a module can reach `AllowAllAuthorization`.

    A name, an attribute of a module (``auth.AllowAllAuthorization``), an import
    (an ``as`` alias included, which still names the class), a star import from
    the module that defines it, and a string equal to the name (``getattr``).
    A subclass, an alias assignment and a factory all need one of these.

    Parameters
    ----------
    tree
        The parsed module.

    Returns
    -------
    list[ast.AST]
        The nodes that refer to the class, outside docstrings and comments.
    """
    return [node for node in ast.walk(tree) if _names_allow_all(node)]


def _names_allow_all(node: ast.AST) -> bool:
    """Say whether one node refers to `AllowAllAuthorization`.

    Parameters
    ----------
    node
        Any node of a module.

    Returns
    -------
    bool
        ``True`` for a name, an attribute, an import, a star import or a string
        that names the class.
    """
    if isinstance(node, ast.Name):
        return node.id == _ALLOW_ALL
    if isinstance(node, ast.Attribute):
        return node.attr == _ALLOW_ALL
    if isinstance(node, ast.Constant):
        return node.value == _ALLOW_ALL
    if isinstance(node, ast.ImportFrom | ast.Import):
        return any(
            a.name.split(".")[-1] == _ALLOW_ALL or (a.name == "*" and _star_of_auth(node))
            for a in node.names
        )
    return False


def _star_of_auth(node: ast.AST) -> bool:
    """Say whether an import statement is ``from lakana_core.auth import *``.

    Parameters
    ----------
    node
        An import statement.

    Returns
    -------
    bool
        ``True`` for a star import of the module that defines the class.
    """
    return isinstance(node, ast.ImportFrom) and node.module in ("lakana_core.auth", "auth")


def _in_isinstance(tree: ast.AST, node: ast.AST) -> bool:
    """Say whether a node is an argument of a call of ``isinstance``.

    Parameters
    ----------
    tree
        The module.
    node
        The node to look for.

    Returns
    -------
    bool
        ``True`` if some ``isinstance(...)`` call contains the node.
    """
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "isinstance"
        and any(node is inner for arg in call.args for inner in ast.walk(arg))
        for call in ast.walk(tree)
    )


def _is_the_callee_of_a_call(tree: ast.AST, node: ast.AST) -> bool:
    """Say whether a node is the function of a call without arguments.

    Parameters
    ----------
    tree
        The module.
    node
        The node to look for.

    Returns
    -------
    bool
        ``True`` for the ``X`` of ``X()``.
    """
    return any(
        isinstance(call, ast.Call) and call.func is node and not call.args and not call.keywords
        for call in ast.walk(tree)
    )


def _unexpected_allow_all_references(relative: str, tree: ast.AST) -> list[ast.AST]:
    """Find the references to the class that no product module is meant to have.

    Allowed, exactly: in the module that defines the class, none (the definition is not a
    reference); in `bootstrap.py`, one import without an alias and one construction; in
    the helper's module, the import and the ``isinstance`` checks.

    Parameters
    ----------
    relative
        The module's path below the packages directory.
    tree
        The parsed module.

    Returns
    -------
    list[ast.AST]
        The references beyond those.
    """
    references = _allow_all_references(tree)
    if relative == _ISINSTANCE_ONLY:
        return [
            r for r in references if not (_in_isinstance(tree, r) or isinstance(r, ast.ImportFrom))
        ]
    if relative == _BOOTSTRAP:
        imports = [
            r
            for r in references
            if isinstance(r, ast.ImportFrom)
            and r.module == "lakana_core.auth"
            and all(a.asname is None for a in r.names)
        ]
        builds = [r for r in references if _is_the_callee_of_a_call(tree, r)]
        keep = imports[:1] + builds[:1]
        return [r for r in references if all(r is not k for k in keep)]
    return references


@pytest.mark.slow
def test_only_the_bootstrap_builds_an_allow_all_service() -> None:
    """If another product module built one, the three actions would be open there.

    Any reference to the class counts, not only a call: an alias, a subclass, a
    factory and a ``getattr`` all name it. Nothing is skipped whole: the defining module
    may hold none, the bootstrap one import and one construction, and the helper the
    ``isinstance`` check and its import.
    """
    found: dict[str, int] = {}
    for path in _PRODUCT_ROOT.glob("*/src/**/*.py"):
        name = str(path.relative_to(_PRODUCT_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        unexpected = _unexpected_allow_all_references(name, tree)
        if unexpected:
            found[name] = len(unexpected)
    assert found == {}
    bootstrap = ast.parse((_PRODUCT_ROOT / _BOOTSTRAP).read_text(encoding="utf-8"))
    assert len(_allow_all_references(bootstrap)) == 2  # the import and the construction


_BOOTSTRAP_TODAY = (
    "from lakana_core.auth import AllowAllAuthorization\n"
    "def run():\n    authz = AllowAllAuthorization()\n"
)


@pytest.mark.parametrize(
    "addition",
    [
        "def make_open():\n    return AllowAllAuthorization()\n",
        "OPEN = AllowAllAuthorization()\n",
        "def make_open():\n    return AllowAllAuthorization\n",
        "class Open(AllowAllAuthorization):\n    pass\n",
    ],
)
def test_the_bootstrap_is_not_skipped_whole(addition: str) -> None:
    """Today's import and construction pass; a factory, a second instance or a subclass fails."""
    today = ast.parse(_BOOTSTRAP_TODAY)
    assert _unexpected_allow_all_references(_BOOTSTRAP, today) == []
    more = ast.parse(_BOOTSTRAP_TODAY + addition)
    assert _unexpected_allow_all_references(_BOOTSTRAP, more)


def test_the_defining_module_holds_no_reference() -> None:
    """The class statement is not a reference; a registry or an instance next to it would be."""
    assert (
        _unexpected_allow_all_references(_DEFINITION, ast.parse("class AllowAllAuthorization: ..."))
        == []
    )
    assert _unexpected_allow_all_references(
        _DEFINITION, ast.parse("class AllowAllAuthorization: ...\nOPEN = AllowAllAuthorization()")
    )


@pytest.mark.parametrize(
    "source",
    [
        "AllowAllAuthorization()",
        "from lakana_core.auth import AllowAllAuthorization as Open\nOpen()",
        "import lakana_core.auth as auth\nauth.AllowAllAuthorization()",
        "class Open(AllowAllAuthorization):\n    pass",
        "Open = AllowAllAuthorization",
        "def make():\n    return AllowAllAuthorization",
        "getattr(auth, 'AllowAllAuthorization')()",
        "from lakana_core.auth import *",
        "from lakana_core.auth import AllowAllAuthorization",
    ],
)
def test_the_allow_all_scan_catches_every_spelling(source: str) -> None:
    """The self-test: an alias, a subclass, a factory and the rest are all found."""
    assert _allow_all_references(ast.parse(source))


def test_the_allow_all_scan_ignores_docstrings_and_other_classes() -> None:
    tree = ast.parse(
        '"""Replaces `AllowAllAuthorization` here."""\n'
        "# AllowAllAuthorization\n"
        "from lakana_core.auth import GrantAuthorization\n"
        "from other import *\n"
        "isinstance(x, GrantAuthorization)"
    )
    assert _allow_all_references(tree) == []


def test_the_isinstance_check_is_the_one_allowed_reference_outside_the_bootstrap() -> None:
    tree = ast.parse("isinstance(authz, AllowAllAuthorization)\nAllowAllAuthorization()")
    inside = [r for r in _allow_all_references(tree) if _in_isinstance(tree, r)]
    outside = [r for r in _allow_all_references(tree) if not _in_isinstance(tree, r)]
    assert len(inside) == 1
    assert len(outside) == 1


def test_another_action_is_a_programming_error(session: Session) -> None:
    ada = _principal(session, "ada")
    with pytest.raises(ValueError, match="READ_TABLE is not one of"):
        authorize_admin_only(session, AllowAllAuthorization(), ada, Action.READ_TABLE)


_TAIL = (
    " Their holders can no longer grant access, manage groups or issue "
    "tokens: an administrator (a principal with the ADMIN role on the instance) must do "
    'that now. If no administrator can sign in any more, read "Upgrade policy" '
    "(https://lakana-labs.github.io/lakana/explanation/upgrades/) in the documentation."
)
_PRINCIPAL_ROLE = "`lakana grant create --kind INSTANCE --principal NAME --role ADMIN`"
_GROUP_ROLE = "`lakana grant create --kind INSTANCE --group NAME --role ADMIN`"
_OPENING = (
    "grant(s) of MANAGE_GRANT, MANAGE_GROUP or MANAGE_TOKEN as a single action have no "
    "effect: these actions work only with the ADMIN role on the instance. "
)


def _stale(session: Session, who: str, action: Action = Action.MANAGE_TOKEN) -> Grant:
    """Persist a single-action grant of one of the three, to a principal or a group.

    Parameters
    ----------
    session
        Database session.
    who
        The name of the principal (made if needed) it is for.
    action
        The granted action.

    Returns
    -------
    Grant
        The persisted grant.
    """
    found = session.execute(select(Principal).where(Principal.name == who)).scalar_one_or_none()
    ref_id = found.id if found else _principal(session, who).id
    return _grant(session, principal_id=ref_id, action=action, kind=SecurableKind.INSTANCE)


def test_existing_single_action_grants_of_the_three_are_named_in_one_warning(
    session: Session,
) -> None:
    bob = _principal(session, "bob")
    team = Group(id=uuid.uuid4(), name="te\x1bam")
    session.add(team)
    session.commit()
    first = _grant(
        session, principal_id=bob.id, action=Action.MANAGE_GRANT, kind=SecurableKind.INSTANCE
    )
    second = _grant(
        session,
        group_id=team.id,
        action=Action.MANAGE_GROUP,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
        granted_by=bob.id,
    )
    revoked = _grant(
        session, principal_id=bob.id, action=Action.MANAGE_TOKEN, kind=SecurableKind.INSTANCE
    )
    revoked.revoked_at, revoked.revoked_by = datetime.now(UTC), bob.id
    _grant(session, principal_id=bob.id, action=Action.MINT_TASK_TOKEN, kind=SecurableKind.INSTANCE)
    _grant(session, principal_id=bob.id, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    session.commit()
    text = ineffective_grants_warning(session)
    assert text == (
        f"2 {_OPENING}"
        f"{first.id} (MANAGE_GRANT, principal 'bob'); {second.id} (MANAGE_GROUP, group "
        "'te\\x1bam'). Revoke each with `lakana grant revoke ID`. To let someone keep the "
        f"ability, give them the role: {_PRINCIPAL_ROLE} for a principal, {_GROUP_ROLE} for "
        f"a group.{_TAIL}"
    )
    assert text is not None and "\x1b" not in text


def test_the_role_command_fits_the_kinds_of_the_grants(session: Session) -> None:
    _stale(session, "bob")
    only_principals = ineffective_grants_warning(session)
    assert only_principals is not None
    assert f"give them the role: {_PRINCIPAL_ROLE}." in only_principals
    assert "--group" not in only_principals
    team = Group(id=uuid.uuid4(), name="team")
    session.add(team)
    session.commit()
    bob = _principal(session, "carl")
    _grant(
        session,
        group_id=team.id,
        action=Action.MANAGE_GROUP,
        kind=SecurableKind.INSTANCE,
        granted_by=bob.id,
    )
    both = ineffective_grants_warning(session)
    assert both is not None and "--principal NAME" in both and "--group NAME" in both


def test_only_groups_get_the_group_command(session: Session) -> None:
    team = Group(id=uuid.uuid4(), name="team")
    admin = _principal(session, "ada")
    session.add(team)
    session.commit()
    _grant(
        session,
        group_id=team.id,
        action=Action.MANAGE_GROUP,
        kind=SecurableKind.INSTANCE,
        granted_by=admin.id,
    )
    text = ineffective_grants_warning(session)
    assert text is not None
    assert f"give them the role: {_GROUP_ROLE}." in text and "--principal" not in text


def test_a_name_cannot_forge_an_entry(session: Session) -> None:
    forged = "bob'), 00000000-0000-0000-0000-000000000000 (MANAGE_GRANT, principal 'eve"
    _stale(session, forged)
    text = ineffective_grants_warning(session)
    assert text is not None
    assert text.count("(MANAGE_TOKEN,") == 1
    assert (
        "principal \"bob'), 00000000-0000-0000-0000-000000000000 (MANAGE_GRANT, principal 'eve\""
        in text
    )
    quoted = _stale(session, "q'\"x")
    text = ineffective_grants_warning(session)
    assert text is not None and "'q\\'\"x'" in text and quoted.id


def test_a_long_name_is_cut(session: Session) -> None:
    _stale(session, "n" * 500)
    text = ineffective_grants_warning(session)
    assert text is not None and "n" * 200 in text and "n" * 201 not in text


@pytest.mark.parametrize(("count", "more"), [(20, None), (21, 1), (23, 3)])
def test_a_long_list_is_cut_and_says_how_many_more(
    session: Session, count: int, more: int | None
) -> None:
    bob = _principal(session, "bob")
    for _ in range(count):
        _grant(
            session, principal_id=bob.id, action=Action.MANAGE_TOKEN, kind=SecurableKind.INSTANCE
        )
    text = ineffective_grants_warning(session)
    assert text is not None and text.startswith(f"{count} {_OPENING}")
    assert text.count("(MANAGE_TOKEN, principal 'bob')") == 20
    if more is not None:
        assert (
            f"; and {more} more (`lakana grant list` marks each with a note). Revoke each" in text
        )
    else:
        assert " more (" not in text
    assert "\n" not in text
    assert text.endswith("in the documentation.")


def test_the_warning_reads_at_most_twenty_rows(session: Session) -> None:
    bob = _principal(session, "bob")
    for _ in range(25):
        _grant(
            session, principal_id=bob.id, action=Action.MANAGE_TOKEN, kind=SecurableKind.INSTANCE
        )
    loaded: list[str] = []
    event.listen(
        session.get_bind(), "before_cursor_execute", lambda *a: loaded.append(a[2]), named=False
    )
    ineffective_grants_warning(session)
    assert any("LIMIT" in statement for statement in loaded)


def test_the_note_names_the_action_and_no_version() -> None:
    for action in ADMIN_ONLY_ACTIONS:
        assert ineffective_note(action) == f"no effect: {action.value} needs the ADMIN role"
    assert ineffective_note(Action.MINT_TASK_TOKEN) is None
    assert ineffective_note(None) is None


def test_nothing_is_found_on_a_clean_instance(session: Session) -> None:
    assert ineffective_grants_warning(session) is None


_BOUND_TASK = BoundTask(
    task_run_id=uuid.uuid4(),
    claim=1,
    asset="gold.x",
    inputs=(),
    readable=(),
)


@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
@pytest.mark.parametrize("action", _THREE)
def test_an_administrators_task_token_never_reaches_the_three_actions(
    session: Session, action: Action, bound: bool
) -> None:
    """The `ADMIN` role must not open a second way past the task-token cap.

    Both kinds of token: one minted over REST and one the worker minted for a task
    attempt (``TaskScope.bound``). The refusal comes before either scope is read.
    """
    ada = _principal(session, "ada")
    _grant(session, principal_id=ada.id, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
    task = PrincipalRef(
        id=ada.id,
        name=ada.name,
        type=ada.type,
        task_scope=TaskScope(
            environment="dev",
            asset_scope=["gold.x"],
            token_id=uuid.uuid4(),
            bound=_BOUND_TASK if bound else None,
        ),
    )
    authz = _auditing(session)
    operations = {
        Action.MANAGE_GRANT: lambda: GrantService(session, authz).grant(
            task, principal="ada", role=Role.VIEWER, kind=SecurableKind.INSTANCE
        ),
        Action.MANAGE_GROUP: lambda: GroupService(session, authz).create_group(task, name="g"),
        Action.MANAGE_TOKEN: lambda: PatService(session, authz).issue(task, "ada", name="t"),
    }
    expected = f"Task tokens may not {action.value}."
    for call in (lambda: authorize_admin_only(session, authz, task, action), operations[action]):
        with pytest.raises(PermissionDenied) as caught:
            call()
        assert str(caught.value) == expected
    assert _denials(session) == [{"action": action.value}] * 2
    authorize_admin_only(session, authz, ada, action)
