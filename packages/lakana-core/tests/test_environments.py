"""Tests for environment management through the catalog service."""

import contextlib
import inspect
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from core_test_helpers import _config, _RecordThenDeny
from sqlalchemy import Executable, Select, delete, event, select, update
from sqlalchemy.orm import ORMExecuteState, Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable, SecurableKind
from lakana_core.catalog import CatalogService
from lakana_core.catalog.location_sweep import sweep_table_locations
from lakana_core.config import StorageConfig
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.models import (
    Environment,
    EnvironmentDeployment,
    Grant,
    JobVersion,
    Namespace,
    Principal,
    PrincipalType,
    Repository,
    RepositoryKind,
)
from lakana_core.storage.resolver import StorageResolver


def _add_principal(session: Session, name: str) -> PrincipalRef:
    """Insert a principal and return a `PrincipalRef` for it.

    Parameters
    ----------
    session
        Database session.
    name
        The principal's name.

    Returns
    -------
    PrincipalRef
        A reference to the newly persisted principal.
    """
    row = Principal(id=uuid.uuid4(), type=PrincipalType.USER, name=name)
    session.add(row)
    session.commit()
    return PrincipalRef(id=row.id, name=row.name, type=row.type)


def test_create_then_get(catalog: CatalogService, principal: PrincipalRef, tmp_path: Path) -> None:
    """A created environment can be fetched back by name."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    assert catalog.get_environment(principal, "dev").name == "dev"


def test_duplicate_name_is_rejected(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Environment names are unique."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    with pytest.raises(AlreadyExistsError):
        catalog.create_environment(principal, name="dev", storage=_config(tmp_path))


def test_get_unknown_raises_not_found(catalog: CatalogService, principal: PrincipalRef) -> None:
    """Fetching a missing environment is an error."""
    with pytest.raises(NotFoundError):
        catalog.get_environment(principal, "nope")


def test_list_environments_orders_by_creation_time(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """Listing is ordered by `created_at`, not by insertion or primary key order.

    Inserted first but given the later `created_at` here: without an
    explicit `order_by`, a backend is free to return rows in insertion or
    physical storage order, which this test deliberately makes disagree with
    creation-time order so an ordering that merely happens to match
    insertion order would not pass it by accident.
    """
    first = catalog.create_environment(principal, name="first", storage=_config(tmp_path / "a"))
    second = catalog.create_environment(principal, name="second", storage=_config(tmp_path / "b"))
    first.created_at, second.created_at = second.created_at, first.created_at
    session.add_all([first, second])
    session.commit()

    ordered = catalog.list_environments(principal)
    assert [e.name for e in ordered] == ["second", "first"]


def test_list_returns_all_environments(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Listing yields every environment."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path / "d"))
    catalog.create_environment(principal, name="prod", storage=_config(tmp_path / "p"))
    assert {e.name for e in catalog.list_environments(principal)} == {"dev", "prod"}


def test_delete_removes_the_environment(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A deleted environment is gone."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.delete_environment(principal, "dev")
    with pytest.raises(NotFoundError):
        catalog.get_environment(principal, "dev")


def test_delete_refuses_while_a_repository_is_deployed(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """A deployment pointer blocks deletion (it would lock in the opposite order to a run start)."""
    env = catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    repo_id, version_id = uuid.uuid4(), uuid.uuid4()
    session.add(Repository(id=repo_id, name="analytics", kind=RepositoryKind.LOCAL, url="/x"))
    session.flush()
    session.add(
        JobVersion(
            id=version_id,
            repository_id=repo_id,
            environment_id=env.id,
            commit_sha="a" * 40,
            manifest_json={},
            code_uri="s3://b/c",
            code_sha256="0" * 64,
        )
    )
    session.flush()
    session.add(
        EnvironmentDeployment(
            environment_id=env.id, repository_id=repo_id, current_job_version_id=version_id
        )
    )
    session.commit()
    with pytest.raises(ConflictError) as info:
        catalog.delete_environment(principal, "dev")
    assert str(info.value) == (
        "Environment 'dev' still has deployed repositories: analytics. "
        "Undeploy each first (`lakana undeploy REPO --env dev`)."
    )
    assert catalog.get_environment(principal, "dev").name == "dev"
    session.delete(session.get(EnvironmentDeployment, (env.id, repo_id)))
    session.commit()
    catalog.delete_environment(principal, "dev")
    with pytest.raises(NotFoundError):
        catalog.get_environment(principal, "dev")


@contextlib.contextmanager
def _right_before_the_environment_lock(
    session: Session, statement: Executable
) -> Generator[None, None, None]:
    """Run `statement` as if another transaction committed it just before the row lock.

    Parameters
    ----------
    session
        The session `delete_environment` uses.
    statement
        What the other transaction does.

    Yields
    ------
    None
        While the hook is installed.
    """

    def interleave(state: ORMExecuteState) -> None:
        query = state.statement
        if (
            isinstance(query, Select)
            and query._for_update_arg is not None
            and query.column_descriptions[0]["entity"] is Environment
        ):
            state.session.connection().execute(statement)

    event.listen(session, "do_orm_execute", interleave)
    try:
        yield
    finally:
        event.remove(session, "do_orm_execute", interleave)


def test_delete_checks_protection_on_the_locked_row_not_a_stale_copy(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """Ledger 71: a protection flip committed before the lock is seen, not a stale copy."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    protect = update(Environment).where(Environment.name == "dev").values(is_protected=True)
    with (
        _right_before_the_environment_lock(session, protect),
        pytest.raises(ValidationError, match="is protected"),
    ):
        catalog.delete_environment(principal, "dev")
    assert session.scalars(select(Environment.name)).all() == ["dev"]


def test_delete_losing_a_race_to_another_delete_is_not_found(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """Ledger 71: the row vanishes before the lock is taken: 404, never a raw NoResultFound."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    with (
        _right_before_the_environment_lock(session, delete(Environment)),
        pytest.raises(NotFoundError, match="No environment named 'dev'"),
    ):
        catalog.delete_environment(principal, "dev")


def test_delete_unknown_raises_not_found(catalog: CatalogService, principal: PrincipalRef) -> None:
    """Deleting a missing environment is an error, not a silent no-op."""
    with pytest.raises(NotFoundError):
        catalog.delete_environment(principal, "nope")


def test_delete_environment_cascades_to_its_namespaces(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """The deletion keeps the namespace rows; the sweep's removal of the row cascades to them.

    A deleted environment keeps its namespaces, which a restore needs, until the location
    sweep deletes its row after the undo period. That relies on the database-level
    ``ondelete="CASCADE"`` on `Namespace.environment_id`. Postgres enforces that
    unconditionally, but SQLite only enforces foreign keys, including this cascade, once
    ``PRAGMA foreign_keys=ON`` has been issued on the connection, which
    `lakana_core.db.create_engine` does for every engine Lakana builds. This proves the cascade
    actually fires on SQLite, not only on Postgres.
    """
    env = catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    ns_id = uuid.uuid4()
    # "widgets", not "gold": `create_environment` scaffolds `gold` itself, so a second namespace
    # row of that name would collide on the `(environment_id, path)` unique constraint.
    session.add(Namespace(id=ns_id, environment_id=env.id, path="widgets", properties={}))
    session.commit()

    deleted = catalog.delete_environment(principal, "dev", purge=True)

    # A plain SELECT by the id captured above: the deletion's commit expires every object in
    # the session, and the cascade removes the row under the stale instance.
    kept = session.execute(select(Namespace).where(Namespace.id == ns_id)).scalar_one_or_none()
    assert kept is not None
    assert deleted.purge_after is not None
    sweep_table_locations(
        session, StorageResolver(session), now=deleted.purge_after, grace_hours=24
    )
    remaining = session.execute(select(Namespace).where(Namespace.id == ns_id)).scalar_one_or_none()
    assert remaining is None


def test_create_scaffolds_the_three_medallion_namespaces(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A freshly created environment already has bronze/silver/gold.

    This is what lets a team start writing `bronze.orders` immediately, with no
    namespace-setup step of its own.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))

    listed = catalog.list_namespaces(principal, "dev")

    assert [p.parts for p in listed] == [("bronze",), ("gold",), ("silver",)]


class _OnlyManageEnvironment:
    """Permits `MANAGE_ENVIRONMENT` and refuses every other action."""

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse anything but `MANAGE_ENVIRONMENT`.

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
            For any action other than `MANAGE_ENVIRONMENT`.
        """
        if action is not Action.MANAGE_ENVIRONMENT:
            raise PermissionDenied(f"{action} denied")

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Answer as `authorize` would.

        Parameters
        ----------
        principal
            The actor the question is about.
        action
            The action in question.
        securable
            The object in question.

        Returns
        -------
        bool
            Whether `action` is `MANAGE_ENVIRONMENT`.
        """
        return action is Action.MANAGE_ENVIRONMENT


def test_create_scaffolds_namespaces_in_the_same_transaction_as_the_environment(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The medallion scaffold is part of creating the environment, not three later calls.

    With `MANAGE_ENVIRONMENT` alone, creation used to commit the environment
    and then fail its first `CREATE_NAMESPACE` check, leaving an environment
    with no namespaces. Now the scaffold commits with the environment, under
    the one instance-scoped check that already covers it.
    """
    catalog = CatalogService(session, StorageResolver(session), _OnlyManageEnvironment())

    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))

    paths = session.execute(select(Namespace.path)).scalars().all()
    assert sorted(paths) == ["bronze", "gold", "silver"]


def test_is_protected_defaults_to_false(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Environments are unprotected unless asked for."""
    env = catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    assert env.is_protected is False


def test_authorization_is_actually_consulted(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Every environment operation goes through the authorization seam.

    This is the test that keeps M3 honest: if a later change adds an
    operation that skips ``authorize()``, or authorizes the wrong action or
    the wrong securable, this fails. Each operation is checked in isolation
    with a service that denies on the very first call, so a delegating
    implementation (one that borrows a helper's authorization instead of
    checking its own) is caught by recording the wrong action/securable
    pair.
    """
    create_authz = _RecordThenDeny()
    catalog = CatalogService(session, StorageResolver(session), create_authz)
    with pytest.raises(PermissionDenied):
        catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    assert create_authz.seen == (Action.MANAGE_ENVIRONMENT, Securable.instance_scope())

    get_authz = _RecordThenDeny()
    catalog = CatalogService(session, StorageResolver(session), get_authz)
    with pytest.raises(PermissionDenied):
        catalog.get_environment(principal, "dev")
    assert get_authz.seen == (Action.READ_ENVIRONMENT, Securable.environment_scope("dev"))

    setup = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    setup.create_environment(principal, name="dev", storage=_config(tmp_path / "list"))
    list_authz = _RecordThenDeny()
    catalog = CatalogService(session, StorageResolver(session), list_authz)
    assert catalog.list_environments(principal) == []
    assert list_authz.seen == (Action.READ_ENVIRONMENT, Securable.environment_scope("dev"))

    delete_authz = _RecordThenDeny()
    catalog = CatalogService(session, StorageResolver(session), delete_authz)
    with pytest.raises(PermissionDenied):
        catalog.delete_environment(principal, "dev")
    assert delete_authz.seen == (Action.MANAGE_ENVIRONMENT, Securable.environment_scope("dev"))

    update_authz = _RecordThenDeny()
    catalog = CatalogService(session, StorageResolver(session), update_authz)
    with pytest.raises(PermissionDenied):
        catalog.update_environment_storage(principal, "dev", options={"a": "b"})
    assert update_authz.seen == (Action.MANAGE_ENVIRONMENT, Securable.instance_scope())

    protect_authz = _RecordThenDeny()
    catalog = CatalogService(session, StorageResolver(session), protect_authz)
    with pytest.raises(PermissionDenied):
        catalog.set_environment_protection(principal, "dev", True)
    assert protect_authz.seen == (Action.MANAGE_ENVIRONMENT, Securable.instance_scope())


def test_list_environments_hides_what_the_principal_may_not_read(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Only environments `permits` allows are listed; a denial is filtered, not raised."""
    setup = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    setup.create_environment(principal, name="dev", storage=_config(tmp_path / "a"))
    setup.create_environment(principal, name="prod", storage=_config(tmp_path / "b"))

    class _OnlyDev:
        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Permit only the `dev` environment.

            Parameters
            ----------
            principal
                Ignored.
            action
                Ignored.
            securable
                The object acted upon.

            Raises
            ------
            PermissionDenied
                For any securable outside `dev`.
            """
            if securable.environment != "dev":
                raise PermissionDenied("no")

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer as `authorize` would.

            Parameters
            ----------
            principal
                Ignored.
            action
                Ignored.
            securable
                The object acted upon.

            Returns
            -------
            bool
                `True` only for `dev`.
            """
            return securable.environment == "dev"

    catalog = CatalogService(session, StorageResolver(session), _OnlyDev())
    assert [e.name for e in catalog.list_environments(principal)] == ["dev"]


class _PermitReadDenyManage:
    """Permits ``READ_ENVIRONMENT``, refuses everything else.

    Used to prove that ``delete_environment`` cannot be satisfied by read
    access alone — it must authorize ``MANAGE_ENVIRONMENT`` on its own
    behalf, not inherit its authorization from a delegate that only needs
    read access.
    """

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Permit ``READ_ENVIRONMENT``, refuse every other action.

        Parameters
        ----------
        principal
            The actor requesting the action. Ignored.
        action
            The action requested.
        securable
            The object acted upon. Ignored.

        Raises
        ------
        PermissionDenied
            If ``action`` is not ``READ_ENVIRONMENT``.
        """
        if action is not Action.READ_ENVIRONMENT:
            raise PermissionDenied(f"{action} denied.")

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


def test_delete_requires_manage_environment_not_just_read(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Deleting an environment must require MANAGE_ENVIRONMENT, explicitly.

    This is the test that catches the privilege-escalation hole in a
    ``delete_environment`` that authorizes by calling ``get_environment``
    and treating that as sufficient: ``get_environment`` only requires
    ``READ_ENVIRONMENT``, so a principal who can merely read an environment
    must not thereby be able to delete it.
    """
    setup = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    setup.create_environment(principal, name="dev", storage=_config(tmp_path))

    catalog = CatalogService(session, StorageResolver(session), _PermitReadDenyManage())
    with pytest.raises(PermissionDenied):
        catalog.delete_environment(principal, "dev")


def test_every_public_catalog_method_calls_authorize() -> None:
    """Every public `CatalogService` method's source mentions `self._authz.authorize`.

    `test_authorization_is_actually_consulted` above enumerates today's five
    operations by hand and checks the exact action/securable pair each one
    authorizes — it is the precise test and stays the primary defence.
    This test is coarse by comparison: source inspection, not behaviour, so
    it cannot tell *which* action or securable a method checks, only that it
    mentions the authorization call at all, and a method could technically
    contain the string in a comment. It exists to catch a narrower but
    likely failure mode instead: Phase B adds roughly a dozen more methods to
    this class from a plan that does not contain the hand-written test above,
    and a method that simply forgets to call `authorize()` would otherwise
    pass every other gate. Do not delete the precise test above thinking
    this one covers it — this one complements it, it does not replace it.
    `list_environments` filters with `permits` instead, which counts.
    `append` delegates to `check_can_append` (a
    server-side caller needs to authorize before reading a request body,
    before `append` itself has anything to read) rather than calling
    `self._authz.authorize` directly; `check_can_append` is itself a public
    method this same loop checks independently, and does call it. `replace`
    and `merge` delegate to `check_can_replace` and `check_can_merge` for the same reason (and
    `history` to `check_can_history`), and those to `_check_can_write_or_create`, which
    authorizes (`check_can_merge` and `check_can_history` through `_check_can_compare`).
    `commit_answer` asks
    `_may_see_state`, which calls `permits` for READ_TABLE. `set_table_description` and
    `set_column_description` ask `_find_table_to_describe`, which authorizes (the owner, or
    `DROP_TABLE`) before it says anything.
    """
    for name, fn in inspect.getmembers(CatalogService, inspect.isfunction):
        if name.startswith("_"):
            continue
        source = inspect.getsource(fn)
        assert (
            "self._authz.authorize" in source
            or "self._authz.permits" in source
            or "self.check_can_append(" in source
            or "self.check_can_replace(" in source
            or "self.check_can_merge(" in source
            or "self.check_can_history(" in source
            or "self._check_can_compare(" in source
            or "self._check_can_write_or_create(" in source
            or "self._may_see_state(" in source
            or "self._find_table_to_describe(" in source
        ), name
    assert "self._authz.authorize" in inspect.getsource(CatalogService._check_can_write_or_create)
    assert "self._check_can_write_or_create(" in inspect.getsource(
        CatalogService._check_can_compare
    )


def test_update_environment_storage_replaces_options(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Passing new options replaces the environment's stored options entirely."""
    from lakana_core.auth import AllowAllAuthorization
    from lakana_core.catalog.service import CatalogService
    from lakana_core.config import StorageConfig
    from lakana_core.storage.resolver import StorageResolver

    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )

    updated = catalog.update_environment_storage(principal, "dev", options={"region": "eu-west-1"})

    assert updated.storage_options == {"region": "eu-west-1"}


def test_update_environment_storage_sets_secret_ref_when_authorized(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A principal with both MANAGE_ENVIRONMENT and MANAGE_SECRET can attach a secret_ref.

    Covers the success path the confused-deputy tests below don't reach:
    those prove the check exists, this proves the field is actually
    assigned once it passes.
    """
    from lakana_core.auth import AllowAllAuthorization
    from lakana_core.catalog.service import CatalogService
    from lakana_core.config import StorageConfig
    from lakana_core.storage.resolver import StorageResolver

    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )

    updated = catalog.update_environment_storage(principal, "dev", secret_ref="s3-creds")

    assert updated.storage_secret_ref == "s3-creds"


def test_update_environment_storage_setting_a_secret_ref_requires_manage_secret(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Setting secret_ref is refused without MANAGE_SECRET, even with MANAGE_ENVIRONMENT."""
    from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable
    from lakana_core.catalog.service import CatalogService
    from lakana_core.config import StorageConfig
    from lakana_core.errors import PermissionDenied
    from lakana_core.storage.resolver import StorageResolver

    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )

    class _ManageEnvironmentOnly:
        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            if action == Action.MANAGE_SECRET:
                raise PermissionDenied("no secret rights")

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

    scoped = CatalogService(session, StorageResolver(session), _ManageEnvironmentOnly())
    with pytest.raises(PermissionDenied):
        scoped.update_environment_storage(principal, "dev", secret_ref="s3-creds")


def test_update_environment_storage_options_alone_needs_no_manage_secret_at_instance_scope(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Changing only options, with no secret_ref, never checks MANAGE_SECRET.

    `update_environment_storage` now requires instance-scoped
    `MANAGE_ENVIRONMENT` instead of environment-scoped —
    this test's name used to say nothing about scope. The behaviour under
    test (a `secret_ref`-free call never touches `MANAGE_SECRET`) is
    unchanged; only the name now says which scope is actually required.
    """
    from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable
    from lakana_core.catalog.service import CatalogService
    from lakana_core.config import StorageConfig
    from lakana_core.errors import PermissionDenied
    from lakana_core.storage.resolver import StorageResolver

    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=f"file://{tmp_path}")
    )

    class _ManageEnvironmentOnly:
        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            if action == Action.MANAGE_SECRET:
                raise PermissionDenied("no secret rights")

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

    scoped = CatalogService(session, StorageResolver(session), _ManageEnvironmentOnly())
    updated = scoped.update_environment_storage(principal, "dev", options={"region": "eu-west-1"})

    assert updated.storage_options == {"region": "eu-west-1"}


def test_update_environment_storage_raises_not_found_for_an_unknown_environment(
    session: Session, principal: PrincipalRef
) -> None:
    """Updating storage for an environment that doesn't exist is a NotFoundError."""
    from lakana_core.auth import AllowAllAuthorization
    from lakana_core.catalog.service import CatalogService
    from lakana_core.errors import NotFoundError
    from lakana_core.storage.resolver import StorageResolver

    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())

    with pytest.raises(NotFoundError):
        catalog.update_environment_storage(principal, "nope", options={"a": "b"})


def test_update_environment_storage_denies_secret_ref_before_checking_existence(
    session: Session, principal: PrincipalRef
) -> None:
    """A MANAGE_SECRET denial for a nonexistent environment must not leak as NotFoundError.

    Authorization runs before the environment lookup, so a principal who
    lacks secret rights gets `PermissionDenied` even for a name that does
    not exist -- never `NotFoundError`, which would let a caller distinguish
    "you can't touch this" from "this doesn't exist" and so probe for valid
    environment names without secret rights at all.
    """
    from lakana_core.auth import Action, PrincipalRef, Securable
    from lakana_core.catalog.service import CatalogService
    from lakana_core.errors import PermissionDenied
    from lakana_core.storage.resolver import StorageResolver

    class _DenyManageSecret:
        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            if action == Action.MANAGE_SECRET:
                raise PermissionDenied("no secret rights")

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

    scoped = CatalogService(session, StorageResolver(session), _DenyManageSecret())
    with pytest.raises(PermissionDenied):
        scoped.update_environment_storage(principal, "totally-does-not-exist", secret_ref="x")


def test_delete_protected_environment_is_refused(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    """A protected environment cannot be deleted.

    The refusal is total, not partial: the environment row still exists,
    and a grant bound to it is left active — a caller must not be able to
    use a refused delete to sneak `revoke_attached`'s side effect through
    on the way to a denial.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path), is_protected=True)
    _add_principal(session, "alice")
    grant = GrantService(session, AllowAllAuthorization()).grant(
        principal,
        principal="alice",
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )

    with pytest.raises(ValidationError):
        catalog.delete_environment(principal, "dev")

    assert catalog.get_environment(principal, "dev").name == "dev"
    refreshed = session.execute(select(Grant).where(Grant.id == grant.id)).scalar_one()
    assert refreshed.revoked_at is None


def test_update_storage_on_protected_environment_is_refused(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A protected environment's storage cannot be repointed.

    The refusal leaves the stored options untouched, not merely raising
    after partially applying the change.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path), is_protected=True)

    with pytest.raises(ValidationError):
        catalog.update_environment_storage(principal, "dev", options={"a": "b"})

    assert catalog.get_environment(principal, "dev").storage_options == {}


def test_set_environment_protection_toggles_the_flag(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`set_environment_protection` flips `is_protected` on and back off."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))

    protected = catalog.set_environment_protection(principal, "dev", True)
    assert protected.is_protected is True

    unprotected = catalog.set_environment_protection(principal, "dev", False)
    assert unprotected.is_protected is False


def test_set_environment_protection_raises_not_found_for_an_unknown_environment(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """Protecting a missing environment is an error, not a silent no-op."""
    with pytest.raises(NotFoundError):
        catalog.set_environment_protection(principal, "nope", True)


def test_unprotecting_then_deleting_a_previously_protected_environment_succeeds(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Clearing `is_protected` makes deletion possible again."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path), is_protected=True)
    catalog.set_environment_protection(principal, "dev", False)

    catalog.delete_environment(principal, "dev")

    with pytest.raises(NotFoundError):
        catalog.get_environment(principal, "dev")


def test_environment_lifecycle_is_audited(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """create/update-storage/protect/delete each record a matching AuditEvent.

    Covers the audit-coverage gap a review flagged for storage-configuration
    changes, and the four `ENVIRONMENT_*` event types that have call
    sites. `ENVIRONMENT_STORAGE_UPDATED`'s detail carries the changed
    option *keys* only, never their values.
    """
    from lakana_core.models import AuditEvent, AuditEventType

    def _events(event_type: AuditEventType) -> list[AuditEvent]:
        return list(
            session.execute(select(AuditEvent).where(AuditEvent.event_type == event_type))
            .scalars()
            .all()
        )

    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.update_environment_storage(principal, "dev", options={"region": "eu-west-1"})
    catalog.set_environment_protection(principal, "dev", True)
    catalog.set_environment_protection(principal, "dev", False)
    catalog.delete_environment(principal, "dev")

    created = _events(AuditEventType.ENVIRONMENT_CREATED)
    assert len(created) == 1
    assert created[0].principal_id == principal.id
    assert created[0].securable_environment == "dev"
    assert created[0].detail == {"storage_root": f"file://{tmp_path}", "is_protected": False}

    storage_updated = _events(AuditEventType.ENVIRONMENT_STORAGE_UPDATED)
    assert len(storage_updated) == 1
    assert storage_updated[0].detail["options_keys"] == ["region"]
    assert storage_updated[0].detail["secret_ref_set"] is False
    assert "eu-west-1" not in str(storage_updated[0].detail)

    protection_changed = _events(AuditEventType.ENVIRONMENT_PROTECTION_CHANGED)
    assert [e.detail["is_protected"] for e in protection_changed] == [True, False]

    deleted = _events(AuditEventType.ENVIRONMENT_DELETED)
    assert len(deleted) == 1
    assert deleted[0].securable_environment == "dev"


@pytest.mark.parametrize(
    ("root", "canonical"),
    [
        ("file:////data/lake", "file:///data/lake"),
        ("file:///data//lake", "file:///data/lake"),
        ("file:///data/./lake", "file:///data/lake"),
        ("FILE:///data/lake", "file:///data/lake"),
        ("s3a://bucket/lake", "s3://bucket/lake"),
        ("s3://bucket//lake/", "s3://bucket/lake"),
    ],
)
def test_a_storage_root_written_another_way_is_refused_with_the_canonical_one(
    catalog: CatalogService, principal: PrincipalRef, root: str, canonical: str
) -> None:
    """Two spellings of one place would hide each other's files from every location check."""
    scheme = "s3" if canonical.startswith("s3:") else "file"
    with pytest.raises(ValidationError) as refused:
        catalog.create_environment(
            principal, name="dev", storage=StorageConfig(scheme=scheme, root=root, options={})
        )
    assert repr(canonical) in str(refused.value)
    assert catalog.list_environments(principal) == []


@pytest.mark.parametrize("root", ["file:///data/../lake", "file:///data/l%61ke", "file:///d[1]"])
def test_a_storage_root_that_cannot_be_normalised_is_refused(
    catalog: CatalogService, principal: PrincipalRef, root: str
) -> None:
    """A ``..`` segment, a percent escape or a glob character: no spelling is offered."""
    with pytest.raises(ValidationError, match="cannot be compared"):
        catalog.create_environment(
            principal, name="dev", storage=StorageConfig(scheme="file", root=root, options={})
        )


def test_a_storage_root_with_one_trailing_slash_is_accepted(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A trailing slash changes nothing: every location is built without it."""
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}/", options={}),
    )
    assert catalog.get_environment(principal, "dev").name == "dev"


@pytest.mark.parametrize(
    ("root", "fix"),
    [
        ("file://data/lake", "an absolute path"),
        ("file://localhost/data/lake", "'file:///data/lake'"),
        ("file://server/share", "an absolute path"),
    ],
)
def test_a_file_root_must_be_an_absolute_path_without_a_host(
    catalog: CatalogService, principal: PrincipalRef, root: str, fix: str
) -> None:
    """A relative path is read against a working directory, and a host means nothing to Lakana."""
    with pytest.raises(ValidationError) as refused:
        catalog.create_environment(
            principal, name="dev", storage=StorageConfig(scheme="file", root=root, options={})
        )
    assert fix in str(refused.value)


@pytest.mark.parametrize(
    "root",
    [
        "file:///",
        "file://",
        "FILE:///",
        "file:////",
        "file:///.",
        "file://localhost/",
        "file://localhost",
    ],
)
def test_the_root_of_the_disk_is_refused_with_a_directory_to_use(
    catalog: CatalogService, principal: PrincipalRef, root: str
) -> None:
    """Storage would read the disk root as each process's working directory, whatever spelling."""
    with pytest.raises(ValidationError) as refused:
        catalog.create_environment(
            principal, name="dev", storage=StorageConfig(scheme="file", root=root, options={})
        )
    message = str(refused.value)
    assert "root of the disk" in message
    assert "cannot hold an environment" in message
    assert "'file:///data/lakana'" in message
    assert catalog.list_environments(principal) == []


@pytest.mark.parametrize("root", ["file:///data", "file:///d", "file:///data/", "file:///.lakana"])
def test_a_directory_just_below_the_root_of_the_disk_is_accepted(
    catalog: CatalogService, principal: PrincipalRef, root: str
) -> None:
    """Only the disk root itself is refused."""
    catalog.create_environment(
        principal, name="dev", storage=StorageConfig(scheme="file", root=root, options={})
    )
    assert catalog.get_environment(principal, "dev").storage_root == root
