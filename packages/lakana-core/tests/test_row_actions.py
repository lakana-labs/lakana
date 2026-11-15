"""What each row says its reader may do (`actions`) and which file owns it (`defined_by`).

`actions` lists what the reader's rights allow, kind by kind, asked in one `permits_each` batch
per list; `defined_by` says what git owns. The parity cases at the end pin, for each row type,
that the actions a row offers are exactly the ones the service then lets through its
authorization check.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from core_test_helpers import (
    ARTIFACT_REF,
    ActionAllowList,
    _minimal_schema,
    _prepared_catalog,
    artifact_service,
    deploy_assets,
    store_version,
)
from home_test_helpers import person
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lakana_core.artifacts.service import ArtifactService
from lakana_core.audit.authorization import AuditingAuthorization
from lakana_core.audit.denial_limiter import DenialAuditLimiter
from lakana_core.auth import (
    Action,
    AllowAllAuthorization,
    AuthorizationService,
    PrincipalRef,
    Securable,
    SecurableKind,
)
from lakana_core.catalog.rebuild import TableRebuild
from lakana_core.catalog.row_actions import (
    ARTIFACT_ACTIONS,
    DROPPED_ACTIONS,
    ORPHAN_ACTIONS,
    TABLE_ACTIONS,
    VERSION_ACTIONS,
    drop_actions_each,
    dropped_actions_each,
    table_actions_each,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.table_listing import TableListing
from lakana_core.errors import ConflictError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import AuditEvent, PrincipalType
from lakana_core.projects.deploys import DeployService
from lakana_core.runs.service import RunService
from lakana_core.storage.resolver import StorageResolver

_SILVER = NamespacePath.from_parts(["silver"])
_CLEAN = TableRef("dev", _SILVER, "clean")
_FREE = TableRef("dev", _SILVER, "free")
_OLD = TableRef("dev", _SILVER, "old")
_GONE = TableRef("dev", _SILVER, "gone")
_REASON = "the source was corrected upstream"


class _Batch:
    """An allow list that answers only in batches, and counts them.

    Parameters
    ----------
    inner
        The allow list that decides.
    """

    def __init__(self, inner: ActionAllowList) -> None:
        self.inner = inner
        self.calls = 0

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Refuse as the allow list does.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.
        """
        self.inner.authorize(principal, action, securable)

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Fail: a row's actions must be asked in one batch.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError(f"asked {action.value} one at a time")

    def permits_each(
        self, principal: PrincipalRef, questions: Sequence[tuple[Action, Securable]]
    ) -> list[bool]:
        """Answer a batch and count it.

        Parameters
        ----------
        principal
            The actor.
        questions
            The questions.

        Returns
        -------
        list of bool
            The allow list's answers.
        """
        self.calls += 1
        return [self.inner.permits(principal, action, scope) for action, scope in questions]


def _row(
    name: str, kind: str, *, owner: uuid.UUID | None = None, declared: bool = False
) -> tuple[TableRef, str, uuid.UUID | None, bool]:
    """Make a table row's question, owned by someone else unless `owner` is given.

    Parameters
    ----------
    name
        The table's name in `dev.silver`.
    kind
        Its API kind.
    owner
        Its owner; a stranger when `None`.
    declared
        Whether a current deployment declares its name.

    Returns
    -------
    tuple
        ``(ref, kind, owner_id, declared)``.
    """
    return (TableRef("dev", _SILVER, name), kind, owner or uuid.uuid4(), declared)


def _p() -> PrincipalRef:
    """Make a principal that owns nothing.

    Returns
    -------
    PrincipalRef
        The reader.
    """
    return PrincipalRef(id=uuid.uuid4(), name="reader", type=PrincipalType.USER)


def test_the_action_words_are_fixed() -> None:
    assert TABLE_ACTIONS == ("drop", "rebuild", "maintain", "set_retention")
    assert DROPPED_ACTIONS == ("undrop",)
    assert ORPHAN_ACTIONS == ("drop",)
    assert ARTIFACT_ACTIONS == ("drop",)
    assert VERSION_ACTIONS == ("delete",)


def test_actions_follow_the_readers_rights_kind_by_kind() -> None:
    authz = ActionAllowList(Action.DROP_TABLE, Action.MAINTAIN_TABLE)
    rows = [_row("t", "TABLE"), _row("v", "VIEW"), _row("e", "EXTERNAL"), _row("a", "ARTIFACT")]

    assert table_actions_each(authz, _p(), rows) == [("drop", "maintain"), ("drop",), ("drop",), ()]


def test_the_owner_may_set_retention_without_write_table() -> None:
    p = _p()
    rows = [_row("t", "TABLE", owner=p.id), _row("v", "VIEW", owner=p.id)]

    assert table_actions_each(ActionAllowList(), p, rows) == [("set_retention",), ()]


def test_write_table_lets_anyone_else_set_retention() -> None:
    authz = ActionAllowList(Action.WRITE_TABLE)

    assert table_actions_each(authz, _p(), [_row("t", "TABLE")]) == [("set_retention",)]


def test_rebuild_needs_run_too() -> None:
    p = _p()
    declared = [_row("t", "TABLE", declared=True)]

    assert table_actions_each(ActionAllowList(Action.DROP_TABLE), p, declared) == [("drop",)]
    assert table_actions_each(ActionAllowList(Action.RUN), p, declared) == [()]
    both = ActionAllowList(Action.DROP_TABLE, Action.RUN)
    assert table_actions_each(both, p, declared) == [("drop", "rebuild")]
    assert table_actions_each(both, p, [_row("t", "TABLE")]) == [("drop",)]


def test_rebuild_is_offered_for_any_declared_name_but_an_artifact() -> None:
    """F8: Rebuild takes any declared name held by a table, view or external table."""
    authz = ActionAllowList(Action.DROP_TABLE, Action.RUN)
    rows = [
        _row("v", "VIEW", declared=True),
        _row("e", "EXTERNAL", declared=True),
        _row("a", "ARTIFACT", declared=True),
    ]

    assert table_actions_each(authz, _p(), rows) == [("drop", "rebuild"), ("drop", "rebuild"), ()]


def test_actions_ask_one_batch_per_request() -> None:
    authz = _Batch(
        ActionAllowList(Action.DROP_TABLE, Action.RUN, Action.MAINTAIN_TABLE, Action.CREATE_TABLE)
    )
    p = _p()
    refs = [TableRef("dev", _SILVER, n) for n in ("a", "b", "c")]

    table_actions_each(authz, p, [_row(n, "TABLE", declared=True) for n in ("a", "b", "c")])
    assert authz.calls == 1
    dropped_actions_each(authz, p, refs)
    assert authz.calls == 2
    drop_actions_each(authz, p, refs, "delete")
    assert authz.calls == 3


def test_undrop_needs_drop_on_the_namespace_and_create_on_the_table() -> None:
    """The rule of `undrop_table`: DROP_TABLE on the namespace, CREATE_TABLE on the name."""
    ref = TableRef("dev", _SILVER, "gone")

    class _Scoped(ActionAllowList):
        """Permit DROP_TABLE only on namespaces and CREATE_TABLE only on tables."""

        def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
            """Answer by the scope.

            Parameters
            ----------
            principal
                The actor.
            action
                The action.
            securable
                The object.

            Returns
            -------
            bool
                Whether the pair is the undrop rule's.
            """
            return (action, securable.kind) in {
                (Action.DROP_TABLE, SecurableKind.NAMESPACE),
                (Action.CREATE_TABLE, SecurableKind.TABLE),
            }

    assert dropped_actions_each(_Scoped(), _p(), [ref]) == [("undrop",)]
    assert dropped_actions_each(ActionAllowList(Action.DROP_TABLE), _p(), [ref]) == [()]


def test_drop_actions_use_the_word_given() -> None:
    refs = [TableRef("dev", _SILVER, "a")]

    assert drop_actions_each(ActionAllowList(Action.DROP_TABLE), _p(), refs, "delete") == [
        ("delete",)
    ]
    assert drop_actions_each(ActionAllowList(Action.READ_TABLE), _p(), refs, "drop") == [()]


def test_actions_write_no_audit_event(session: Session) -> None:
    stranger = person(session, "stranger")
    authz = AuditingAuthorization(GrantAuthorization(session), session, DenialAuditLimiter(10_000))
    refs = [TableRef("dev", _SILVER, "a")]

    assert table_actions_each(authz, stranger, [_row("a", "TABLE", declared=True)]) == [()]
    assert dropped_actions_each(authz, stranger, refs) == [()]
    assert drop_actions_each(authz, stranger, refs, "drop") == [()]
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_defined_by_is_set_only_for_a_matching_kind(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    catalog.create_table(principal, _CLEAN, schema=_minimal_schema())

    deploy_assets(session, "dev", "analytics", {"silver.clean": {}})
    found = catalog.describe_table(principal, _CLEAN).defined_by
    assert found is not None
    assert (found.repository, found.path) == ("analytics", "assets/silver/clean.sql")

    deploy_assets(
        session, "dev", "analytics", {"silver.clean": {"kind": "python", "output": "artifact"}}
    )
    described = catalog.describe_table(principal, _CLEAN)
    assert described.defined_by is None
    # F8: the name is still declared, so Rebuild stays on offer.
    assert "rebuild" in described.actions


# -- Parity: a row offers an action exactly when the service lets it through its rights ------


_PERSONAS = ("reader", "engineer", "admin")
"""A viewer (reads only), a data engineer (drops, writes, creates; no RUN, no MAINTAIN_TABLE),
and an administrator."""


class _World:
    """`dev` with a declared table, a free one, an orphan, a dropped table and an artifact.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        The environment's storage root.
    persona
        Which of `_PERSONAS` acts.
    """

    def __init__(self, session: Session, tmp_path: Path, persona: str) -> None:
        self.session = session
        owner = person(session, "owner")
        allow = AllowAllAuthorization()
        catalog = _prepared_catalog(session, tmp_path, allow, namespaces=[_SILVER])
        for ref in (_CLEAN, _FREE, _OLD, _GONE):
            catalog.create_table(owner, ref, schema=_minimal_schema())
        deploy_assets(session, "dev", "analytics", {"silver.old": {}, "silver.clean": {}})
        deploy_assets(session, "dev", "analytics", {"silver.clean": {}})
        catalog.drop_table(owner, _GONE)
        artifacts = artifact_service(session, tmp_path, allow)
        store_version(artifacts, owner, ARTIFACT_REF, {"model.bin": b"weights"})
        self.p = person(session, persona)
        role = {"reader": Role.VIEWER, "engineer": Role.DATA_ENGINEER, "admin": Role.ADMIN}[persona]
        kind = SecurableKind.INSTANCE if persona == "admin" else SecurableKind.ENVIRONMENT
        environment = None if persona == "admin" else "dev"
        GrantService(session, allow).grant(
            owner, principal=persona, role=role, kind=kind, environment=environment
        )
        self.authz: AuthorizationService = GrantAuthorization(session)
        self.catalog = CatalogService(session, StorageResolver(session), self.authz)
        self.artifacts = ArtifactService(session, StorageResolver(session), self.authz)

    def table_actions(self, ref: TableRef) -> tuple[str, ...]:
        """Read a table row's actions from the describe and from the list; they must agree.

        Parameters
        ----------
        ref
            The table.

        Returns
        -------
        tuple of str
            Its actions.
        """
        described = self.catalog.describe_table(self.p, ref).actions
        items = TableListing(self.session, self.authz).list(self.p, environment="dev").items
        (listed,) = [i.actions for i in items if i.name == ref.name]
        assert listed == described
        return described

    def attempt(self, call: Callable[[], object]) -> str:
        """Run one request and say how it ended.

        Parameters
        ----------
        call
            The request.

        Returns
        -------
        str
            ``"denied"`` for `PermissionDenied`, ``"conflict"`` for `ConflictError`, else
            ``"done"``.
        """
        try:
            call()
        except PermissionDenied:
            self.session.rollback()
            return "denied"
        except ConflictError:
            self.session.rollback()
            return "conflict"
        return "done"


@pytest.fixture(params=_PERSONAS)
def world(request: pytest.FixtureRequest, session: Session, tmp_path: Path) -> _World:
    """Build the world for one persona.

    Parameters
    ----------
    request
        Carries the persona.
    session
        Database session.
    tmp_path
        The environment's storage root.

    Returns
    -------
    _World
        The world.
    """
    return _World(session, tmp_path, request.param)


_EXPECTED_TABLE = {
    "reader": (),
    "engineer": ("drop", "set_retention"),
    "admin": ("drop", "rebuild", "maintain", "set_retention"),
}


@pytest.mark.parametrize("action", TABLE_ACTIONS)
def test_a_git_owned_tables_actions_are_what_its_rights_allow(world: _World, action: str) -> None:
    """Drop is offered on rights and then refused because git owns the table; Rebuild runs."""
    persona = world.p.name
    actions = world.table_actions(_CLEAN)
    assert actions == _EXPECTED_TABLE[persona]
    assert world.catalog.describe_table(world.p, _CLEAN).defined_by is not None
    outcome = world.attempt(_table_call(world, _CLEAN, action))
    if action in actions:
        assert outcome == ("conflict" if action == "drop" else "done")
    else:
        assert outcome == "denied"


@pytest.mark.parametrize("action", TABLE_ACTIONS)
def test_a_free_tables_actions_are_what_its_rights_allow(world: _World, action: str) -> None:
    """Without a declaration Rebuild is never offered: the service has nothing to build from."""
    actions = world.table_actions(_FREE)
    assert actions == tuple(a for a in _EXPECTED_TABLE[world.p.name] if a != "rebuild")
    assert world.catalog.describe_table(world.p, _FREE).defined_by is None
    outcome = world.attempt(_table_call(world, _FREE, action))
    if action in actions:
        assert outcome == "done"
    elif action == "rebuild" and world.p.name == "admin":
        assert outcome == "conflict"
    else:
        assert outcome == "denied"


def _table_call(world: _World, ref: TableRef, action: str) -> Callable[[], object]:
    """Make the request a table action stands for.

    Parameters
    ----------
    world
        The world.
    ref
        The table.
    action
        One of `TABLE_ACTIONS`.

    Returns
    -------
    callable
        The request.
    """
    p, catalog = world.p, world.catalog
    if action == "drop":
        return lambda: catalog.drop_table(p, ref)
    if action == "rebuild":
        rebuild = TableRebuild(
            world.session, catalog, RunService(world.session, world.authz), world.authz, undo_days=7
        )
        return lambda: rebuild.rebuild(p, ref, reason=_REASON)
    if action == "maintain":
        return lambda: catalog.maintain(p, ref, dry_run=True)
    return lambda: catalog.set_table_retention(p, ref, never=True)


def test_a_dropped_tables_actions_are_what_its_rights_allow(world: _World) -> None:
    by_namespace = world.catalog.list_dropped_tables(world.p, "dev", _SILVER)
    (everywhere,) = world.catalog.list_environment_dropped_tables(world.p, "dev").items
    (dropped,) = by_namespace
    assert dropped.actions == everywhere.actions
    assert dropped.actions == ((), ("undrop",), ("undrop",))[_PERSONAS.index(world.p.name)]
    outcome = world.attempt(lambda: world.catalog.undrop_table(world.p, _GONE))
    assert outcome == ("done" if dropped.actions else "denied")


def test_an_orphans_actions_are_what_its_rights_allow(world: _World) -> None:
    (orphan,) = DeployService(world.session, world.authz).orphans(world.p, "dev").orphans
    assert orphan.table == "silver.old"
    assert orphan.actions == ((), ("drop",), ("drop",))[_PERSONAS.index(world.p.name)]
    outcome = world.attempt(lambda: world.catalog.drop_table(world.p, _OLD))
    assert outcome == ("done" if orphan.actions else "denied")


def test_an_artifacts_actions_are_what_its_rights_allow(world: _World) -> None:
    (listed,) = world.artifacts.list_artifacts(world.p, "dev").items
    shown = world.artifacts.get_artifact(world.p, ARTIFACT_REF)
    assert listed.actions == shown.actions
    assert shown.actions == ((), ("drop",), ("drop",))[_PERSONAS.index(world.p.name)]
    outcome = world.attempt(lambda: world.artifacts.drop_artifact(world.p, ARTIFACT_REF))
    assert outcome == ("done" if shown.actions else "denied")


def test_a_versions_actions_are_what_its_rights_allow(world: _World) -> None:
    (listed,) = world.artifacts.list_versions(world.p, ARTIFACT_REF)
    shown = world.artifacts.get_version(world.p, ARTIFACT_REF, 1)
    assert listed.actions == shown.actions
    assert shown.actions == ((), ("delete",), ("delete",))[_PERSONAS.index(world.p.name)]
    outcome = world.attempt(lambda: world.artifacts.delete_version(world.p, ARTIFACT_REF, 1))
    assert outcome == ("done" if shown.actions else "denied")
    if outcome == "done":
        (deleted,) = world.artifacts.list_versions(world.p, ARTIFACT_REF)
        assert deleted.actions == ()
