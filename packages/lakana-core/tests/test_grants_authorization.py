"""Tests for GrantAuthorization's resolution algorithm."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core_test_helpers import _minimal_schema, _prepared_catalog
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    CiScope,
    PrincipalRef,
    Securable,
    SecurableKind,
    TaskScope,
)
from lakana_core.catalog.identity import resolve_environment_id, resolve_table_id
from lakana_core.errors import PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    Environment,
    Grant,
    Group,
    GroupMembership,
    Namespace,
    Principal,
    PrincipalType,
    Table,
    TableType,
)


def _add_principal(session: Session, name: str) -> PrincipalRef:
    """Insert a principal and return a PrincipalRef for it.

    Returns
    -------
    PrincipalRef
        A reference to the newly persisted principal.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def _add_grant(
    session: Session,
    *,
    granted_by: uuid.UUID,
    principal_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    role: Role | None = None,
    action: Action | None = None,
    kind: SecurableKind,
    environment: str | None = None,
    namespace: str | None = None,
    table: str | None = None,
    environment_id: uuid.UUID | None = None,
    table_id: uuid.UUID | None = None,
) -> None:
    """Insert a Grant row directly, bypassing GrantService.

    This file tests `authorize()`, not `grant()`. `granted_by` is a
    required, real principal id — `Grant.granted_by` is a
    non-nullable foreign key onto `principals.id`, so a fabricated id (e.g.
    a fresh `uuid.uuid4()` with no backing row) fails the insert. Pass the
    id of whatever principal this test already persisted (commonly the
    test's own `alice`) rather than inventing a new one.

    `environment_id`/`table_id` default to `None` — the authoritative
    binding — so a caller that wants a grant to actually match
    a real securable must resolve and pass the real id explicitly, the same
    way `GrantService.grant` would have set it.
    """
    session.add(
        Grant(
            id=uuid.uuid4(),
            principal_id=principal_id,
            group_id=group_id,
            role=role,
            action=action,
            kind=kind,
            environment=environment,
            namespace=namespace,
            table=table,
            environment_id=environment_id,
            table_id=table_id,
            granted_by=granted_by,
            granted_at=datetime.now(UTC),
        )
    )
    session.commit()


def _add_environment(session: Session, name: str) -> uuid.UUID:
    """Insert a bare Environment row and resolve its id.

    Parameters
    ----------
    session
        Database session.
    name
        The environment's name.

    Returns
    -------
    uuid.UUID
        The environment's id, as `resolve_environment_id` returns it.
    """
    session.add(
        Environment(
            id=uuid.uuid4(),
            name=name,
            storage_root="file:///dev/null",
            storage_options={},
            properties={},
        )
    )
    session.commit()
    environment_id = resolve_environment_id(session, name)
    assert environment_id is not None  # nosec B101 -- just inserted above
    return environment_id


def _add_table(
    session: Session, environment: str, namespace: NamespacePath, table: str
) -> uuid.UUID:
    """Insert the namespace (if missing) and table rows, and resolve the table's id.

    Parameters
    ----------
    session
        Database session.
    environment
        Name of an already-inserted environment (see `_add_environment`).
    namespace
        The table's namespace.
    table
        The table's name.

    Returns
    -------
    uuid.UUID
        The table's id, as `resolve_table_id` returns it.
    """
    environment_id = resolve_environment_id(session, environment)
    assert environment_id is not None  # nosec B101 -- caller inserts the environment first
    joined = namespace.joined()
    ns = session.execute(
        select(Namespace).where(
            Namespace.environment_id == environment_id, Namespace.path == joined
        )
    ).scalar_one_or_none()
    if ns is None:
        ns = Namespace(id=uuid.uuid4(), environment_id=environment_id, path=joined, properties={})
        session.add(ns)
        session.commit()
    session.add(
        Table(id=uuid.uuid4(), namespace_id=ns.id, name=table, table_type=TableType.ICEBERG)
    )
    session.commit()
    table_id = resolve_table_id(session, environment, joined, table)
    assert table_id is not None  # nosec B101 -- just inserted above
    return table_id


def test_no_grant_anywhere_is_refused(session: Session) -> None:
    """A principal with no grants at all is refused for anything."""
    alice = _add_principal(session, "alice")
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_TABLE, Securable.instance_scope())


def test_an_instance_scoped_grant_covers_everything(session: Session) -> None:
    """An INSTANCE-kind grant authorizes a table-scoped ask too."""
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    authz = GrantAuthorization(session)
    ns = NamespacePath.from_parts(["bronze"])
    authz.authorize(alice, Action.DROP_TABLE, Securable.table_scope("dev", ns, "orders"))


def test_an_environment_grant_does_not_cover_a_different_environment(session: Session) -> None:
    """An ENVIRONMENT-kind grant on 'dev' does not authorize an ask against 'prod'.

    Both environments are real rows here, with distinct ids, so the denial
    comes from `grant.environment_id != env_id` — the rule under test —
    rather than from `prod` simply never having existed.
    """
    alice = _add_principal(session, "alice")
    dev_id = _add_environment(session, "dev")
    _add_environment(session, "prod")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
        environment_id=dev_id,
    )
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_TABLE, Securable.environment_scope("prod"))


def test_a_namespace_grant_covers_a_nested_namespace(session: Session) -> None:
    """A grant on 'gold' covers 'gold.bronze' via prefix matching."""
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    gold = NamespacePath.from_parts(["gold"])
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace=gold.joined(),
        environment_id=environment_id,
    )
    authz = GrantAuthorization(session)
    nested = NamespacePath.from_parts(["gold", "bronze"])
    authz.authorize(alice, Action.READ_TABLE, Securable.table_scope("dev", nested, "orders"))


def test_a_namespace_grant_does_not_cover_a_sibling_namespace(session: Session) -> None:
    """A grant on 'gold' does not cover 'silver'.

    `dev` is a real environment here, so `env_id` resolves and matches the
    grant's — the denial comes from the namespace-prefix rule itself, not
    from an unresolved environment short-circuiting `_applies_to` first.
    """
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    gold = NamespacePath.from_parts(["gold"])
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace=gold.joined(),
        environment_id=environment_id,
    )
    authz = GrantAuthorization(session)
    silver = NamespacePath.from_parts(["silver"])
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_TABLE, Securable.table_scope("dev", silver, "orders"))


def test_a_table_grant_does_not_cover_a_different_table(session: Session) -> None:
    """A grant on bronze.orders does not cover bronze.customers.

    `customers` is a real, distinct table here too, so the denial comes
    from `grant.table_id != table_id` — two different real ids — rather
    than from `customers` simply not existing.
    """
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    bronze = NamespacePath.from_parts(["bronze"])
    table_id = _add_table(session, "dev", bronze, "orders")
    _add_table(session, "dev", bronze, "customers")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace=bronze.joined(),
        table="orders",
        environment_id=environment_id,
        table_id=table_id,
    )
    authz = GrantAuthorization(session)
    authz.authorize(alice, Action.READ_TABLE, Securable.table_scope("dev", bronze, "orders"))
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_TABLE, Securable.table_scope("dev", bronze, "customers"))


def test_a_role_grant_only_covers_actions_in_its_bundle(session: Session) -> None:
    """A viewer grant does not authorize SUBMIT_QUERY."""
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
        environment_id=environment_id,
    )
    authz = GrantAuthorization(session)
    authz.authorize(alice, Action.READ_ENVIRONMENT, Securable.environment_scope("dev"))
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.SUBMIT_QUERY, Securable.environment_scope("dev"))


def test_a_group_grant_authorizes_its_members(session: Session) -> None:
    """A grant on a group authorizes a principal who is a member of it."""
    alice = _add_principal(session, "alice")
    group = Group(id=uuid.uuid4(), name="engineers")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=alice.id))
    session.commit()
    _add_grant(
        session,
        granted_by=alice.id,
        group_id=group.id,
        role=Role.DATA_ENGINEER,
        kind=SecurableKind.INSTANCE,
    )
    authz = GrantAuthorization(session)
    authz.authorize(alice, Action.CREATE_TABLE, Securable.instance_scope())


def test_a_group_grant_does_not_authorize_a_non_member(session: Session) -> None:
    """A grant on a group never authorizes a principal outside it, member or not of another group.

    Pins `GrantAuthorization.authorize`'s membership scoping: without it, a group's grant would
    authorize every principal, since the only check left would be whether *some* active grant
    covers the action and securable.
    """
    alice = _add_principal(session, "alice")
    bob = _add_principal(session, "bob")
    engineers = Group(id=uuid.uuid4(), name="engineers")
    analysts = Group(id=uuid.uuid4(), name="analysts")
    session.add_all([engineers, analysts])
    session.add(GroupMembership(group_id=engineers.id, principal_id=alice.id))
    session.add(GroupMembership(group_id=analysts.id, principal_id=bob.id))
    session.commit()
    _add_grant(
        session,
        granted_by=alice.id,
        group_id=engineers.id,
        role=Role.DATA_ENGINEER,
        kind=SecurableKind.INSTANCE,
    )
    authz = GrantAuthorization(session)
    authz.authorize(alice, Action.CREATE_TABLE, Securable.instance_scope())
    with pytest.raises(PermissionDenied):
        authz.authorize(bob, Action.CREATE_TABLE, Securable.instance_scope())
    assert authz.permits(bob, Action.CREATE_TABLE, Securable.instance_scope()) is False


def test_a_revoked_grant_no_longer_authorizes(session: Session) -> None:
    """A grant with revoked_at set is not consulted."""
    alice = _add_principal(session, "alice")
    row = Grant(
        id=uuid.uuid4(),
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
        granted_by=alice.id,
        granted_at=datetime.now(UTC),
        revoked_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_TABLE, Securable.instance_scope())


def test_a_task_scoped_principal_is_refused_outside_its_environment(session: Session) -> None:
    """A task token scoped to 'dev' is refused for an ask against 'prod', regardless of grants."""
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied, match=r"scoped to 'dev'"):
        authz.authorize(task_alice, Action.READ_TABLE, Securable.environment_scope("prod"))


def test_a_task_scoped_principal_is_refused_outside_its_asset_scope(session: Session) -> None:
    """A task token scoped to bronze.* is refused for a table outside that pattern."""
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    bronze = NamespacePath.from_parts(["bronze"])
    gold = NamespacePath.from_parts(["gold"])
    authz.authorize(task_alice, Action.READ_TABLE, Securable.table_scope("dev", bronze, "orders"))
    with pytest.raises(
        PermissionDenied, match=r"does not declare 'gold\.customers' in its asset scope"
    ):
        authz.authorize(
            task_alice, Action.READ_TABLE, Securable.table_scope("dev", gold, "customers")
        )


def test_task_scope_only_narrows_never_grants_beyond_the_principals_own_grants(
    session: Session,
) -> None:
    """A task token cannot do more than its principal's own grants allow."""
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    bronze = NamespacePath.from_parts(["bronze"])
    with pytest.raises(PermissionDenied):
        authz.authorize(
            task_alice, Action.WRITE_TABLE, Securable.table_scope("dev", bronze, "orders")
        )


def test_exact_match_asset_pattern_without_wildcard(session: Session) -> None:
    """A pattern with no trailing '.*' must match exactly, not as a prefix."""
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(
            environment="dev", asset_scope=["bronze.orders"], token_id=uuid.uuid4()
        ),
    )
    authz = GrantAuthorization(session)
    bronze = NamespacePath.from_parts(["bronze"])
    authz.authorize(task_alice, Action.READ_TABLE, Securable.table_scope("dev", bronze, "orders"))
    with pytest.raises(PermissionDenied):
        authz.authorize(
            task_alice, Action.READ_TABLE, Securable.table_scope("dev", bronze, "customers")
        )


def test_a_namespace_grant_does_not_cover_an_environment_wide_ask(session: Session) -> None:
    """Grant narrower than ask doesn't cover environment-wide securable.

    `dev` is a real environment here (not just a name that happens to
    match) so this exercises `_applies_to`'s `securable.namespace is None`
    branch — the ask's environment id resolves and matches the grant's, so
    the denial comes from the namespace mismatch itself, not from an
    environment that never existed in the first place.
    """
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    bronze = NamespacePath.from_parts(["bronze"])
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace=bronze.joined(),
        environment_id=environment_id,
    )
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_ENVIRONMENT, Securable.environment_scope("dev"))


def test_a_task_scoped_principal_can_act_within_scope_on_a_non_table_securable(
    session: Session,
) -> None:
    """Task scope checks asset_scope for TABLEs; others need environment match."""
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
        environment_id=environment_id,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    authz.authorize(task_alice, Action.READ_ENVIRONMENT, Securable.environment_scope("dev"))


def test_a_namespace_grant_does_not_cover_a_similarly_prefixed_sibling(session: Session) -> None:
    """A grant on 'gold' does not cover 'golden' — a shared string prefix isn't enough.

    `dev` is real here, and so is a table under `golden`, so this is a
    genuine reachable exercise of the namespace path-prefix rule — the
    grant's namespace prefix-matches `gold` and nothing else, never a
    string prefix of it — rather than a denial that only holds because
    `golden` (or its environment) never existed.
    """
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    golden = NamespacePath.from_parts(["golden"])
    _add_table(session, "dev", golden, "orders")
    gold = NamespacePath.from_parts(["gold"])
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace=gold.joined(),
        environment_id=environment_id,
    )
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_TABLE, Securable.table_scope("dev", golden, "orders"))


def test_an_asset_pattern_does_not_match_a_similarly_prefixed_sibling(session: Session) -> None:
    """A task token scoped to 'bronze.*' does not match 'bronzefoo.orders' — no separator match."""
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    bronzefoo = NamespacePath.from_parts(["bronzefoo"])
    with pytest.raises(PermissionDenied):
        authz.authorize(
            task_alice, Action.READ_TABLE, Securable.table_scope("dev", bronzefoo, "orders")
        )


def test_task_token_cannot_drop_an_undeclared_namespace(session: Session) -> None:
    """A bronze.* token cannot DROP_NAMESPACE on gold.

    Even as a `DATA_ENGINEER` — a role whose bundle includes
    `DROP_NAMESPACE` — a task token can never perform it: `DROP_NAMESPACE`
    is not in `TASK_TOKEN_ACTIONS` at all, so this is refused before the
    namespace-coverage check ever runs.
    """
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.DATA_ENGINEER,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    gold = NamespacePath.from_parts(["gold"])
    with pytest.raises(PermissionDenied, match=r"Task tokens may not DROP_NAMESPACE\."):
        authz.authorize(task_alice, Action.DROP_NAMESPACE, Securable.namespace_scope("dev", gold))


def test_task_token_cannot_manage_secrets(session: Session) -> None:
    """A task token cannot MANAGE_SECRET, even as an admin."""
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied, match=r"Task tokens may not MANAGE_SECRET\."):
        authz.authorize(task_alice, Action.MANAGE_SECRET, Securable.environment_scope("dev"))


def test_task_token_may_read_its_declared_namespace(session: Session) -> None:
    """A wildcard or table-shaped asset pattern also covers reading its own namespace."""
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    bronze = NamespacePath.from_parts(["bronze"])
    bronze_eu = NamespacePath.from_parts(["bronze", "eu"])
    task_wildcard = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    authz.authorize(task_wildcard, Action.READ_NAMESPACE, Securable.namespace_scope("dev", bronze))
    authz.authorize(
        task_wildcard, Action.READ_NAMESPACE, Securable.namespace_scope("dev", bronze_eu)
    )

    task_exact = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(
            environment="dev", asset_scope=["bronze.orders"], token_id=uuid.uuid4()
        ),
    )
    authz.authorize(task_exact, Action.READ_NAMESPACE, Securable.namespace_scope("dev", bronze))


def test_task_token_cannot_read_an_undeclared_namespace(session: Session) -> None:
    """READ_NAMESPACE on a namespace outside the token's asset scope is denied."""
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    gold = NamespacePath.from_parts(["gold"])
    with pytest.raises(PermissionDenied, match=r"does not declare namespace 'gold'\."):
        authz.authorize(task_alice, Action.READ_NAMESPACE, Securable.namespace_scope("dev", gold))


def test_task_token_wildcard_pattern_does_not_cover_a_similarly_prefixed_namespace(
    session: Session,
) -> None:
    """A bronze.* token does not cover the sibling namespace bronzefoo.

    `dev` is a real environment and `bronzefoo` a real namespace with a
    table in it, so this exercises the namespace-coverage rule's own
    separator-aware prefix match rather than a denial that only holds
    because `bronzefoo` never existed.
    """
    alice = _add_principal(session, "alice")
    _add_environment(session, "dev")
    bronzefoo = NamespacePath.from_parts(["bronzefoo"])
    _add_table(session, "dev", bronzefoo, "orders")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied, match=r"does not declare namespace 'bronzefoo'\."):
        authz.authorize(
            task_alice, Action.READ_NAMESPACE, Securable.namespace_scope("dev", bronzefoo)
        )


def test_task_token_table_pattern_does_not_cover_a_nested_namespace(session: Session) -> None:
    """A bronze.orders token covers namespace 'bronze' exactly, not 'bronze.eu' nested under it.

    `dev` is a real environment and `bronze.eu` a real namespace with a
    table in it, so the denial comes from the table-shaped pattern's own
    exact-match rule, not from `bronze.eu` never existing.
    """
    alice = _add_principal(session, "alice")
    _add_environment(session, "dev")
    bronze_eu = NamespacePath.from_parts(["bronze", "eu"])
    _add_table(session, "dev", bronze_eu, "orders")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(
            environment="dev", asset_scope=["bronze.orders"], token_id=uuid.uuid4()
        ),
    )
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied, match=r"does not declare namespace 'bronze\.eu'\."):
        authz.authorize(
            task_alice, Action.READ_NAMESPACE, Securable.namespace_scope("dev", bronze_eu)
        )


def test_task_token_table_pattern_covers_its_multi_level_namespace(session: Session) -> None:
    """A bronze.eu.orders token covers the nested namespace 'bronze.eu' it navigates through.

    `_matches_namespace_pattern`'s non-wildcard branch strips only the
    final `.<table>` component, via `pattern.rsplit(".", 1)[0]`. A pattern
    with a single dot (`bronze.orders`) can't tell that call apart from a
    full `split`/`rsplit` on every dot, since both give the same one-level
    result; only a namespace nested two or more levels deep exposes the
    difference (`rsplit(".", 1)` keeps `bronze.eu`, a full split collapses
    it to `bronze`).
    """
    alice = _add_principal(session, "alice")
    _add_environment(session, "dev")
    bronze_eu = NamespacePath.from_parts(["bronze", "eu"])
    _add_table(session, "dev", bronze_eu, "orders")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(
            environment="dev", asset_scope=["bronze.eu.orders"], token_id=uuid.uuid4()
        ),
    )
    authz = GrantAuthorization(session)
    authz.authorize(task_alice, Action.READ_NAMESPACE, Securable.namespace_scope("dev", bronze_eu))


def test_task_token_may_not_create_namespaces(session: Session) -> None:
    """CREATE_NAMESPACE is not in the task-token action allow-list."""
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.DATA_ENGINEER,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    authz = GrantAuthorization(session)
    bronze = NamespacePath.from_parts(["bronze"])
    with pytest.raises(PermissionDenied, match=r"Task tokens may not CREATE_NAMESPACE\."):
        authz.authorize(
            task_alice, Action.CREATE_NAMESPACE, Securable.namespace_scope("dev", bronze)
        )


def test_the_denial_message_names_the_principal_and_the_action(session: Session) -> None:
    """A denial's message is specific, not a placeholder — it names who and what."""
    alice = _add_principal(session, "alice")
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied, match=r"'alice' may not READ_TABLE this"):
        authz.authorize(alice, Action.READ_TABLE, Securable.instance_scope())


def test_a_direct_grant_still_authorizes_a_principal_who_also_belongs_to_a_group(
    session: Session,
) -> None:
    """Belonging to a group must not crowd out the principal's own direct grant.

    `authorize()` builds its subject filter as `principal_id == p.id OR
    group_id IN (...)` only once the principal belongs to at least one
    group. A grant made to alice directly (not through any group) must
    still authorize her even though she also belongs to a group that
    itself has no grant at all.
    """
    alice = _add_principal(session, "alice")
    group = Group(id=uuid.uuid4(), name="auditors")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=alice.id))
    session.commit()
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    authz = GrantAuthorization(session)
    authz.authorize(alice, Action.READ_TABLE, Securable.instance_scope())


def test_another_principals_grant_never_authorizes_a_different_principal(session: Session) -> None:
    """A grant is scoped to its own subject — it must never leak to an unrelated principal.

    `authorize()` filters grants by `Grant.principal_id == principal.id`
    (widened by group membership). Without that filter, any active grant
    anywhere would authorize anyone, since the code only checks whether
    *some* grant covers the action and securable.
    """
    alice = _add_principal(session, "alice")
    bob = _add_principal(session, "bob")
    _add_grant(
        session,
        granted_by=bob.id,
        principal_id=bob.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_TABLE, Securable.instance_scope())


def test_grant_on_unknown_environment_matches_nothing(session: Session) -> None:
    """An ENVIRONMENT grant whose `environment_id` is NULL never permits.

    `environment_id` is `None` here even though `environment="dev"` really
    exists and its display name matches the ask exactly — simulating a
    grant whose target was cleared by `ON DELETE SET NULL`, or one
    constructed before the id backfill ran. An unresolvable id must never
    match, not even by falling back to a name comparison.
    """
    alice = _add_principal(session, "alice")
    _add_environment(session, "dev")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
        environment_id=None,
    )
    authz = GrantAuthorization(session)
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_ENVIRONMENT, Securable.environment_scope("dev"))


def test_table_grant_follows_rename(session: Session, tmp_path: Path) -> None:
    """A READ_TABLE grant follows its table's id through a rename.

    The grant still permits once the table has been renamed to
    `gold.orders2`, and does not permit a brand new table later created as
    `gold.orders` — that table has a different id, so the old grant's
    `table_id` no longer matches it, even though the display name is
    identical to what the grant was originally created against.
    """
    alice = _add_principal(session, "alice")
    gold = NamespacePath.from_parts(["gold"])
    orders_ref = TableRef(environment="dev", namespace=gold, name="orders")
    catalog = _prepared_catalog(
        session, tmp_path, AllowAllAuthorization(), namespaces=[gold], tables=[orders_ref]
    )
    environment_id = resolve_environment_id(session, "dev")
    table_id = resolve_table_id(session, "dev", gold.joined(), "orders")
    assert environment_id is not None
    assert table_id is not None
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace=gold.joined(),
        table="orders",
        environment_id=environment_id,
        table_id=table_id,
    )

    catalog.rename_table(
        alice, orders_ref, TableRef(environment="dev", namespace=gold, name="orders2")
    )

    authz = GrantAuthorization(session)
    authz.authorize(alice, Action.READ_TABLE, Securable.table_scope("dev", gold, "orders2"))

    catalog.create_table(alice, orders_ref, schema=_minimal_schema())
    with pytest.raises(PermissionDenied):
        authz.authorize(alice, Action.READ_TABLE, Securable.table_scope("dev", gold, "orders"))


_PermitsCase = tuple[PrincipalRef, Action, Securable, bool]


def _case_no_grant(session: Session) -> _PermitsCase:
    """Deny a principal with no grants at all.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    return alice, Action.READ_TABLE, Securable.instance_scope(), True


def _case_raw_action_grant(session: Session) -> _PermitsCase:
    """Permit via a raw-action grant on the exact table asked about.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    bronze = NamespacePath.from_parts(["bronze"])
    table_id = _add_table(session, "dev", bronze, "orders")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace=bronze.joined(),
        table="orders",
        environment_id=environment_id,
        table_id=table_id,
    )
    return alice, Action.READ_TABLE, Securable.table_scope("dev", bronze, "orders"), False


def _case_role_grant(session: Session) -> _PermitsCase:
    """Permit via a role grant whose bundle covers the requested action.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
        environment_id=environment_id,
    )
    return alice, Action.READ_ENVIRONMENT, Securable.environment_scope("dev"), False


def _case_instance_scope_grant(session: Session) -> _PermitsCase:
    """Permit a table-scoped ask via an INSTANCE-kind grant.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    ns = NamespacePath.from_parts(["bronze"])
    return alice, Action.DROP_TABLE, Securable.table_scope("dev", ns, "orders"), False


def _case_environment_grant_other_environment(session: Session) -> _PermitsCase:
    """Deny an ENVIRONMENT-kind grant checked at a different environment.

    Both `dev` and `prod` are real, distinct environments, so the denial
    exercises `grant.environment_id != env_id` rather than an unresolved
    `prod`.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    dev_id = _add_environment(session, "dev")
    _add_environment(session, "prod")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ANALYST,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
        environment_id=dev_id,
    )
    return alice, Action.READ_TABLE, Securable.environment_scope("prod"), True


def _case_namespace_grant_nested(session: Session) -> _PermitsCase:
    """Permit a namespace grant checked at a nested namespace.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    gold = NamespacePath.from_parts(["gold"])
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace=gold.joined(),
        environment_id=environment_id,
    )
    nested = NamespacePath.from_parts(["gold", "bronze"])
    return alice, Action.READ_TABLE, Securable.table_scope("dev", nested, "orders"), False


def _case_namespace_grant_sibling(session: Session) -> _PermitsCase:
    """Deny a namespace grant checked at a sibling namespace.

    `dev` is real here, so the denial exercises the namespace-prefix rule
    itself rather than an unresolved environment.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    gold = NamespacePath.from_parts(["gold"])
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.NAMESPACE,
        environment="dev",
        namespace=gold.joined(),
        environment_id=environment_id,
    )
    silver = NamespacePath.from_parts(["silver"])
    return alice, Action.READ_TABLE, Securable.table_scope("dev", silver, "orders"), True


def _case_table_grant_wrong_table(session: Session) -> _PermitsCase:
    """Deny a table grant checked against a different table than the one it names.

    `customers` is a real, distinct table here, so the denial exercises
    `grant.table_id != table_id` rather than an unresolved `customers`.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    bronze = NamespacePath.from_parts(["bronze"])
    table_id = _add_table(session, "dev", bronze, "orders")
    _add_table(session, "dev", bronze, "customers")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace=bronze.joined(),
        table="orders",
        environment_id=environment_id,
        table_id=table_id,
    )
    return alice, Action.READ_TABLE, Securable.table_scope("dev", bronze, "customers"), True


def _case_role_grant_does_not_cover_the_action(session: Session) -> _PermitsCase:
    """Deny a role grant checked against an action outside its bundle.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    return alice, Action.SUBMIT_QUERY, Securable.environment_scope("dev"), True


def _case_revoked_grant(session: Session) -> _PermitsCase:
    """Deny access when the only grant is revoked.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    row = Grant(
        id=uuid.uuid4(),
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
        granted_by=alice.id,
        granted_at=datetime.now(UTC),
        revoked_at=datetime.now(UTC),
    )
    session.add(row)
    session.commit()
    return alice, Action.READ_TABLE, Securable.instance_scope(), True


def _case_group_membership_grant(session: Session) -> _PermitsCase:
    """Permit a principal via a grant on a group they belong to.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    group = Group(id=uuid.uuid4(), name="engineers")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=alice.id))
    session.commit()
    _add_grant(
        session,
        granted_by=alice.id,
        group_id=group.id,
        role=Role.DATA_ENGINEER,
        kind=SecurableKind.INSTANCE,
    )
    return alice, Action.CREATE_TABLE, Securable.instance_scope(), False


def _case_task_scoped_inside_scope(session: Session) -> _PermitsCase:
    """Permit a task-scoped principal asked within its environment and asset scope.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    bronze = NamespacePath.from_parts(["bronze"])
    return task_alice, Action.READ_TABLE, Securable.table_scope("dev", bronze, "orders"), False


def _case_task_scoped_outside_environment(session: Session) -> _PermitsCase:
    """Deny a task-scoped principal asked against a different environment.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    return task_alice, Action.READ_TABLE, Securable.environment_scope("prod"), True


def _case_task_scoped_outside_asset_scope(session: Session) -> _PermitsCase:
    """Deny a task-scoped principal asked about a table outside its asset scope.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    gold = NamespacePath.from_parts(["gold"])
    return task_alice, Action.READ_TABLE, Securable.table_scope("dev", gold, "customers"), True


def _case_task_scoped_namespace_covered(session: Session) -> _PermitsCase:
    """Permit a task-scoped principal reading a namespace its asset scope covers.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    bronze = NamespacePath.from_parts(["bronze"])
    return task_alice, Action.READ_NAMESPACE, Securable.namespace_scope("dev", bronze), False


def _case_task_scoped_namespace_not_covered(session: Session) -> _PermitsCase:
    """Deny a task-scoped principal reading a namespace outside its asset scope.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    gold = NamespacePath.from_parts(["gold"])
    return task_alice, Action.READ_NAMESPACE, Securable.namespace_scope("dev", gold), True


def _case_task_scoped_namespace_similarly_prefixed_sibling(session: Session) -> _PermitsCase:
    """Deny a task-scoped principal reading a namespace that shares only a string prefix.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    _add_environment(session, "dev")
    bronzefoo = NamespacePath.from_parts(["bronzefoo"])
    _add_table(session, "dev", bronzefoo, "orders")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    return task_alice, Action.READ_NAMESPACE, Securable.namespace_scope("dev", bronzefoo), True


def _case_task_scoped_action_not_allowed(session: Session) -> _PermitsCase:
    """Deny a task-scoped principal an action outside the task-token allow-list.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        role=Role.ADMIN,
        kind=SecurableKind.INSTANCE,
    )
    task_alice = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["bronze.*"], token_id=uuid.uuid4()),
    )
    return task_alice, Action.MANAGE_SECRET, Securable.environment_scope("dev"), True


def _case_table_grant_after_rename(session: Session) -> _PermitsCase:
    """Permit a TABLE grant checked against its table's post-rename name.

    The table's row keeps its id but is renamed to `orders2`; the grant,
    bound to that same id, still applies at the new name even though its
    own display `table` column still reads `orders` until something
    refreshes it.

    Returns
    -------
    _PermitsCase
        Principal, action, securable, and whether it is expected to be denied.
    """
    alice = _add_principal(session, "alice")
    environment_id = _add_environment(session, "dev")
    bronze = NamespacePath.from_parts(["bronze"])
    table_id = _add_table(session, "dev", bronze, "orders")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        environment="dev",
        namespace=bronze.joined(),
        table="orders",
        environment_id=environment_id,
        table_id=table_id,
    )
    row = session.get(Table, table_id)
    assert row is not None
    row.name = "orders2"
    session.commit()
    return alice, Action.READ_TABLE, Securable.table_scope("dev", bronze, "orders2"), False


_PERMITS_AGREEMENT_CASES = {
    "no_grant": _case_no_grant,
    "raw_action_grant": _case_raw_action_grant,
    "role_grant": _case_role_grant,
    "instance_scope_grant": _case_instance_scope_grant,
    "role_grant_does_not_cover_the_action": _case_role_grant_does_not_cover_the_action,
    "environment_grant_other_environment": _case_environment_grant_other_environment,
    "namespace_grant_nested": _case_namespace_grant_nested,
    "namespace_grant_sibling": _case_namespace_grant_sibling,
    "table_grant_wrong_table": _case_table_grant_wrong_table,
    "revoked_grant": _case_revoked_grant,
    "group_membership_grant": _case_group_membership_grant,
    "task_scoped_inside_scope": _case_task_scoped_inside_scope,
    "task_scoped_outside_environment": _case_task_scoped_outside_environment,
    "task_scoped_outside_asset_scope": _case_task_scoped_outside_asset_scope,
    "task_scoped_namespace_covered": _case_task_scoped_namespace_covered,
    "task_scoped_namespace_not_covered": _case_task_scoped_namespace_not_covered,
    "task_scoped_namespace_similarly_prefixed_sibling": (
        _case_task_scoped_namespace_similarly_prefixed_sibling
    ),
    "task_scoped_action_not_allowed": _case_task_scoped_action_not_allowed,
    "table_grant_after_rename": _case_table_grant_after_rename,
}


@pytest.mark.parametrize(
    "build_case", _PERMITS_AGREEMENT_CASES.values(), ids=list(_PERMITS_AGREEMENT_CASES)
)
def test_permits_agrees_with_authorize(
    session: Session, build_case: Callable[[Session], _PermitsCase]
) -> None:
    """`permits()` never disagrees with `authorize()`, across the resolution algorithm.

    Parameters
    ----------
    session
        Database session, shared with the case builder so grants persisted
        for the case are visible to the `GrantAuthorization` under test.
    build_case
        Persists whatever principals/groups/grants the case needs and
        returns the principal, action, securable, and expected decision.
    """
    principal, action, securable, expected_denied = build_case(session)
    authz = GrantAuthorization(session)
    try:
        authz.authorize(principal, action, securable)
        raised = False
    except PermissionDenied:
        raised = True
    assert raised is expected_denied
    assert authz.permits(principal, action, securable) is (not raised)


@pytest.mark.parametrize(
    "build_case", _PERMITS_AGREEMENT_CASES.values(), ids=list(_PERMITS_AGREEMENT_CASES)
)
def test_permits_each_agrees_with_permits(
    session: Session, build_case: Callable[[Session], _PermitsCase]
) -> None:
    """A batch of one answers as `permits` does, across the resolution algorithm.

    Parameters
    ----------
    session
        Database session, shared with the case builder.
    build_case
        Persists the case and returns the principal, action, securable and expected decision.
    """
    principal, action, securable, expected_denied = build_case(session)
    single = GrantAuthorization(session).permits(principal, action, securable)
    batch = GrantAuthorization(session).permits_each(principal, [(action, securable)])
    assert batch == [single] == [not expected_denied]


def _mixed_world(session: Session) -> tuple[PrincipalRef, list[tuple[Action, Securable]]]:
    """Grant on a table, a namespace and an environment, and ask about many objects at once.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    tuple
        The reader, and the questions, some permitted and some not.
    """
    alice = _add_principal(session, "alice")
    dev = _add_environment(session, "dev")
    _add_environment(session, "prod")
    bronze, gold = NamespacePath.from_parts(["bronze"]), NamespacePath.from_parts(["gold"])
    eu = NamespacePath.from_parts(["gold", "eu"])
    orders = _add_table(session, "dev", bronze, "orders")
    _add_table(session, "dev", bronze, "stores")
    for name in ("facts", "dims"):
        _add_table(session, "dev", gold, name)
    _add_table(session, "dev", eu, "sales")
    _add_table(session, "prod", gold, "facts")
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        environment="dev",
        action=Action.READ_TABLE,
        kind=SecurableKind.TABLE,
        namespace=bronze.joined(),
        table="orders",
        environment_id=dev,
        table_id=orders,
    )
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        environment="dev",
        action=Action.READ_TABLE,
        kind=SecurableKind.NAMESPACE,
        namespace=gold.joined(),
        environment_id=dev,
    )
    _add_grant(
        session,
        granted_by=alice.id,
        principal_id=alice.id,
        environment="dev",
        action=Action.READ_ENVIRONMENT,
        kind=SecurableKind.ENVIRONMENT,
        environment_id=dev,
    )
    table = Securable.table_scope
    questions = [
        (Action.READ_TABLE, table("dev", bronze, "orders")),
        (Action.READ_TABLE, table("dev", bronze, "stores")),
        (Action.READ_TABLE, table("dev", gold, "facts")),
        (Action.READ_TABLE, table("dev", eu, "sales")),
        (Action.READ_TABLE, table("dev", gold, "missing")),
        (Action.READ_TABLE, table("dev", bronze, "missing")),
        (Action.READ_TABLE, table("prod", gold, "facts")),
        (Action.READ_TABLE, table("nowhere", gold, "facts")),
        (Action.WRITE_TABLE, table("dev", gold, "facts")),
        (Action.READ_ENVIRONMENT, Securable.environment_scope("dev")),
        (Action.READ_ENVIRONMENT, Securable.environment_scope("prod")),
        (Action.MANAGE_POLICY, Securable.instance_scope()),
        (Action.READ_NAMESPACE, Securable.namespace_scope("dev", gold)),
    ]
    return alice, questions


_MIXED_ANSWERS = [True, False, True, True, True, False, False, False, False, True, False, False]
"""What `_mixed_world`'s reader may do, question by question (the last one is left out)."""


def test_permits_each_answers_a_mixed_batch_as_the_single_questions_do(session: Session) -> None:
    alice, questions = _mixed_world(session)
    single = [GrantAuthorization(session).permits(alice, a, s) for a, s in questions]
    assert single[:-1] == _MIXED_ANSWERS
    assert GrantAuthorization(session).permits_each(alice, questions) == single
    assert GrantAuthorization(session).permits_each(alice, []) == []


def test_permits_each_narrows_a_task_token_and_a_ci_token_as_permits_does(
    session: Session,
) -> None:
    alice, questions = _mixed_world(session)
    task = PrincipalRef(
        id=alice.id,
        name=alice.name,
        type=alice.type,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.*"], token_id=uuid.uuid4()),
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
            repository="sales",
            repository_id=uuid.uuid4(),
            kinds=frozenset(),
            sha=None,
        ),
    )
    for reader in (task, ci):
        single = [GrantAuthorization(session).permits(reader, a, s) for a, s in questions]
        assert GrantAuthorization(session).permits_each(reader, questions) == single
    assert [GrantAuthorization(session).permits(task, a, s) for a, s in questions][:4] == [
        False,
        False,
        True,
        True,
    ]


def _statements(session: Session, ask: Callable[[], object]) -> int:
    """Count the SQL statements a question issues.

    Parameters
    ----------
    session
        Database session.
    ask
        The question.

    Returns
    -------
    int
        How many statements reached the database.
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


def test_permits_each_costs_the_same_statements_for_one_table_or_forty(session: Session) -> None:
    alice, _ = _mixed_world(session)
    gold = NamespacePath.from_parts(["gold"])
    for i in range(40):
        _add_table(session, "dev", gold, f"t{i}")

    def ask(count: int) -> Callable[[], object]:
        questions = [
            (Action.READ_TABLE, Securable.table_scope("dev", gold, f"t{i}")) for i in range(count)
        ]
        return lambda: GrantAuthorization(session).permits_each(alice, questions)

    one = _statements(session, ask(1))
    assert one == _statements(session, ask(40)) == 4
    authz = GrantAuthorization(session)
    again = [(Action.READ_TABLE, Securable.table_scope("dev", gold, "t0"))] * 3
    assert authz.permits_each(alice, again) == [True, True, True]
    # The ids are kept for the instance's lifetime; only the grants are read again.
    assert _statements(session, lambda: authz.permits_each(alice, again)) == 2
