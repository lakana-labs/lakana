"""Tests for the authorization seam."""

import uuid

import pytest

from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    Securable,
    SecurableKind,
    permits_each,
)
from lakana_core.errors import PermissionDenied
from lakana_core.identifiers import NamespacePath
from lakana_core.models import PrincipalType


@pytest.fixture
def principal() -> PrincipalRef:
    """Provide a principal for tests.

    Returns
    -------
    PrincipalRef
        A user principal.
    """
    return PrincipalRef(id=uuid.uuid4(), name="admin", type=PrincipalType.USER)


def test_allow_all_permits_every_action(principal: PrincipalRef) -> None:
    """The M1 implementation permits everything, silently."""
    authz = AllowAllAuthorization()
    for action in Action:
        authz.authorize(principal, action, Securable.environment_scope("dev"))


def test_allow_all_permits_everything(principal: PrincipalRef) -> None:
    """`AllowAllAuthorization.permits` is `True` for any request."""
    assert AllowAllAuthorization().permits(
        principal, Action.MANAGE_SECRET, Securable.instance_scope()
    )


def test_securable_constructors_carry_the_right_scope() -> None:
    """Each constructor sets its kind and narrows the scope correctly."""
    env = Securable.environment_scope("dev")
    assert env.kind is SecurableKind.ENVIRONMENT
    assert env.namespace is None and env.table is None

    path = NamespacePath.from_parts(["gold", "sales"])
    ns = Securable.namespace_scope("dev", path)
    assert ns.kind is SecurableKind.NAMESPACE
    assert ns.namespace == path and ns.table is None

    tbl = Securable.table_scope("dev", path, "monthly")
    assert tbl.kind is SecurableKind.TABLE
    assert tbl.table == "monthly"


def test_securable_instance_scope_has_no_single_environment() -> None:
    """An instance-scoped securable spans every environment, so it names none."""
    inst = Securable.instance_scope()
    assert inst.kind is SecurableKind.INSTANCE
    assert inst.environment is None
    assert inst.namespace is None and inst.table is None


def test_read_environment_is_a_distinct_grantable_action(
    principal: PrincipalRef,
) -> None:
    """Reading an environment is separate from managing it.

    A grant that lets a principal list an environment without letting them
    delete it requires the two to be different `Action` members, not the
    same one reused under a different name.
    """
    assert Action.READ_ENVIRONMENT is not Action.MANAGE_ENVIRONMENT

    authz = AllowAllAuthorization()
    authz.authorize(principal, Action.READ_ENVIRONMENT, Securable.environment_scope("dev"))


def test_a_denying_implementation_satisfies_the_protocol(
    principal: PrincipalRef,
) -> None:
    """The protocol admits an implementation that refuses, as M3's will."""

    class DenyAll:
        """An authorization service that permits nothing."""

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Refuse every request.

            Parameters
            ----------
            principal
                The actor requesting the action.
            action
                The action requested.
            securable
                The object acted upon.

            Raises
            ------
            PermissionDenied
                Always.
            """
            raise PermissionDenied(f"{principal.name} may not {action} {securable.kind}.")

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize` would, as a boolean.

            Parameters
            ----------
            principal
                The actor requesting the action.
            action
                The action requested.
            securable
                The object acted upon.

            Returns
            -------
            bool
                `False` if `authorize` raises `PermissionDenied`, else `True`.
            """
            try:
                self.authorize(principal, action, securable)
            except PermissionDenied:
                return False
            return True

    service: AuthorizationService = DenyAll()
    with pytest.raises(PermissionDenied):
        service.authorize(principal, Action.READ_TABLE, Securable.environment_scope("dev"))


def test_manage_namespace_is_distinct_from_the_other_namespace_actions() -> None:
    """MANAGE_NAMESPACE exists and is not an alias of create, read or drop."""
    assert Action.MANAGE_NAMESPACE not in {
        Action.CREATE_NAMESPACE,
        Action.READ_NAMESPACE,
        Action.DROP_NAMESPACE,
    }


def test_permits_each_asks_one_question_at_a_time_of_a_service_without_a_batch(
    principal: PrincipalRef,
) -> None:
    gold = NamespacePath.from_parts(["gold"])
    questions = [
        (Action.READ_TABLE, Securable.table_scope("dev", gold, "facts")),
        (Action.DROP_TABLE, Securable.table_scope("dev", gold, "facts")),
    ]

    class _OnlyReads:
        """Permits reading, nothing else."""

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Refuse anything but reading.

            Parameters
            ----------
            principal
                Ignored.
            action
                The action.
            securable
                Ignored.

            Raises
            ------
            PermissionDenied
                For anything but `READ_TABLE`.
            """
            if action is not Action.READ_TABLE:
                raise PermissionDenied("no")

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize`.

            Parameters
            ----------
            principal
                Ignored.
            action
                The action.
            securable
                Ignored.

            Returns
            -------
            bool
                Whether reading is asked.
            """
            return action is Action.READ_TABLE

    assert permits_each(_OnlyReads(), principal, questions) == [True, False]
    assert permits_each(AllowAllAuthorization(), principal, questions) == [True, True]
