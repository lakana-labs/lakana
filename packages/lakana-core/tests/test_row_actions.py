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
from sqlalchemy import Engine, event, func, select
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
from lakana_core.catalog import service as service_module
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
from lakana_core.catalog.service import CatalogService, TableDescription
from lakana_core.catalog.table_listing import TableListing
from lakana_core.errors import ConflictError, PermissionDenied
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.grants.roles import Role
from lakana_core.grants.service import GrantService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.identity.groups import GroupService
from lakana_core.models import AuditEvent, Namespace, PrincipalType
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

    # M4: an artifact row offers the drop the artifact list offers (`lakana artifact drop`).
    assert table_actions_each(authz, _p(), rows) == [
        ("drop", "maintain"),
        ("drop",),
        ("drop",),
        ("drop",),
    ]


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


def test_rebuild_is_offered_for_any_declared_name_but_on_an_artifact_row() -> None:
    """F8: Rebuild takes any declared name held by a table, view or external table."""
    authz = ActionAllowList(Action.DROP_TABLE, Action.RUN)
    rows = [
        _row("v", "VIEW", declared=True),
        _row("e", "EXTERNAL", declared=True),
        _row("a", "ARTIFACT", declared=True),
    ]

    assert table_actions_each(authz, _p(), rows) == [
        ("drop", "rebuild"),
        ("drop", "rebuild"),
        ("drop",),
    ]


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


_PERSONAS = ("reader", "revoked", "engineer", "grouped", "admin")
"""Who acts in the parity cases:

- ``reader``: VIEWER on `dev`.
- ``revoked``: VIEWER on `dev`, and an ADMIN grant that was revoked.
- ``engineer``: DATA_ENGINEER on `dev` (drops, writes, creates; no RUN, no MAINTAIN_TABLE).
- ``grouped``: VIEWER on `dev`, and DATA_ENGINEER on the namespaces `silver` and `models` through
  a group, so every drop right comes from a namespace-level grant.
- ``admin``: ADMIN on the instance.
"""

_TIER = {
    "reader": "reader",
    "revoked": "reader",
    "engineer": "engineer",
    "grouped": "engineer",
    "admin": "admin",
}
"""Which rights each persona ends up with."""


def _grant_persona(session: Session, owner: PrincipalRef, persona: str) -> None:
    """Give a persona its grants, as `_PERSONAS` describes them.

    Parameters
    ----------
    session
        Database session.
    owner
        Who grants (allow-all).
    persona
        One of `_PERSONAS`.
    """
    grants = GrantService(session, AllowAllAuthorization())
    if persona == "admin":
        grants.grant(owner, principal=persona, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
        return
    if persona == "engineer":
        grants.grant(
            owner,
            principal=persona,
            role=Role.DATA_ENGINEER,
            kind=SecurableKind.ENVIRONMENT,
            environment="dev",
        )
        return
    grants.grant(
        owner,
        principal=persona,
        role=Role.VIEWER,
        kind=SecurableKind.ENVIRONMENT,
        environment="dev",
    )
    if persona == "revoked":
        # Another administrator, so the instance keeps one after the revoke.
        grants.grant(owner, principal=owner.name, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
        gone = grants.grant(owner, principal=persona, role=Role.ADMIN, kind=SecurableKind.INSTANCE)
        grants.revoke(owner, gone.id)
    if persona == "grouped":
        groups = GroupService(session, AllowAllAuthorization())
        groups.create_group(owner, name="engineers")
        groups.add_member(owner, "engineers", persona)
        for namespace in ("silver", "models"):
            grants.grant(
                owner,
                group="engineers",
                role=Role.DATA_ENGINEER,
                kind=SecurableKind.NAMESPACE,
                environment="dev",
                namespace=namespace,
            )


def _tier(world: _World, reader: object, engineer: object, admin: object) -> object:
    """Pick the expectation of the world's persona.

    Parameters
    ----------
    world
        The world.
    reader
        What a reader gets.
    engineer
        What a data engineer gets.
    admin
        What an administrator gets.

    Returns
    -------
    object
        The one that applies.
    """
    return {"reader": reader, "engineer": engineer, "admin": admin}[_TIER[world.persona]]


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
        self.persona = persona
        self.owner = owner
        self.p = person(session, persona)
        _grant_persona(session, owner, persona)
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
        bare = self.catalog.describe_table(self.p, ref, with_actions=False)
        (batched,) = self.catalog.with_row_actions(self.p, [bare])
        assert listed == described == batched.actions
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
    actions = world.table_actions(_CLEAN)
    assert actions == _EXPECTED_TABLE[_TIER[world.persona]]
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
    assert actions == tuple(a for a in _EXPECTED_TABLE[_TIER[world.persona]] if a != "rebuild")
    assert world.catalog.describe_table(world.p, _FREE).defined_by is None
    outcome = world.attempt(_table_call(world, _FREE, action))
    if action in actions:
        assert outcome == "done"
    elif action == "rebuild" and _TIER[world.persona] == "admin":
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
    assert dropped.actions == _tier(world, (), ("undrop",), ("undrop",))
    outcome = world.attempt(lambda: world.catalog.undrop_table(world.p, _GONE))
    assert outcome == ("done" if dropped.actions else "denied")


def test_an_orphans_actions_are_what_its_rights_allow(world: _World) -> None:
    (orphan,) = DeployService(world.session, world.authz).orphans(world.p, "dev").orphans
    assert orphan.table == "silver.old"
    assert orphan.actions == _tier(world, (), ("drop",), ("drop",))
    outcome = world.attempt(lambda: world.catalog.drop_table(world.p, _OLD))
    assert outcome == ("done" if orphan.actions else "denied")


def test_an_artifacts_actions_are_what_its_rights_allow(world: _World) -> None:
    (listed,) = world.artifacts.list_artifacts(world.p, "dev").items
    shown = world.artifacts.get_artifact(world.p, ARTIFACT_REF)
    assert listed.actions == shown.actions
    assert shown.actions == _tier(world, (), ("drop",), ("drop",))
    # M4: the same artifact, as a row of the table lists, offers the same.
    items = TableListing(world.session, world.authz).list(world.p, environment="dev").items
    (row,) = [i for i in items if i.kind == "ARTIFACT"]
    assert row.actions == world.catalog.describe_table(world.p, ARTIFACT_REF).actions
    assert row.actions == shown.actions
    outcome = world.attempt(lambda: world.artifacts.drop_artifact(world.p, ARTIFACT_REF))
    assert outcome == ("done" if shown.actions else "denied")


def test_a_versions_actions_are_what_its_rights_allow(world: _World) -> None:
    (listed,) = world.artifacts.list_versions(world.p, ARTIFACT_REF)
    shown = world.artifacts.get_version(world.p, ARTIFACT_REF, 1)
    assert listed.actions == shown.actions
    assert shown.actions == _tier(world, (), ("delete",), ("delete",))
    outcome = world.attempt(lambda: world.artifacts.delete_version(world.p, ARTIFACT_REF, 1))
    assert outcome == ("done" if shown.actions else "denied")
    if outcome == "done":
        (deleted,) = world.artifacts.list_versions(world.p, ARTIFACT_REF)
        assert deleted.actions == ()


def test_a_declared_artifact_name_held_by_a_table_offers_rebuild_and_the_server_runs_it(
    world: _World,
) -> None:
    """F8: the declaration is not the row's kind, so git owns nothing, but Rebuild is accepted."""
    deploy_assets(
        world.session,
        "dev",
        "analytics",
        {"silver.clean": {"kind": "python", "output": "artifact"}},
    )
    actions = world.table_actions(_CLEAN)
    assert world.catalog.describe_table(world.p, _CLEAN).defined_by is None
    assert ("rebuild" in actions) == (_TIER[world.persona] == "admin")
    outcome = world.attempt(_table_call(world, _CLEAN, "rebuild"))
    assert outcome == ("done" if "rebuild" in actions else "denied")


def test_a_protected_environment_keeps_the_offer_and_the_server_refuses_it(
    session: Session, tmp_path: Path
) -> None:
    """`actions` lists rights only: protection is decided by the server, on each request."""
    world = _World(session, tmp_path, "admin")
    world.catalog.set_environment_protection(world.p, "dev", True)

    actions = world.table_actions(_FREE)
    assert "drop" in actions  # the right is there; protection is not a right
    with pytest.raises(PermissionDenied) as refused:
        world.catalog.drop_table(world.p, _FREE)
    assert "protected" in str(refused.value)


class _CountingBatch:
    """Allow everything, and count the `permits_each` batches that ask a drop right."""

    def __init__(self) -> None:
        self.inner = ActionAllowList(*Action)
        self.drop_batches = 0

    def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
        """Allow.

        Parameters
        ----------
        principal
            The actor.
        action
            The action.
        securable
            The object.
        """

    def permits(self, principal: PrincipalRef, action: Action, securable: Securable) -> bool:
        """Allow, and fail on a drop right asked one at a time.

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
            `True`.

        Raises
        ------
        AssertionError
            For a `DROP_TABLE` question outside a batch.
        """
        assert action is not Action.DROP_TABLE, "a row's drop right asked one at a time"
        return True

    def permits_each(
        self, principal: PrincipalRef, questions: Sequence[tuple[Action, Securable]]
    ) -> list[bool]:
        """Allow every question, and count a batch that asks a drop right.

        Parameters
        ----------
        principal
            The actor.
        questions
            The questions.

        Returns
        -------
        list of bool
            All `True`.
        """
        if any(action is Action.DROP_TABLE for action, _ in questions):
            self.drop_batches += 1
        return [True] * len(questions)


def test_the_dropped_orphan_and_artifact_lists_ask_one_batch_for_three_rows(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    allow = AllowAllAuthorization()
    catalog = _prepared_catalog(session, tmp_path, allow, namespaces=[_SILVER])
    names = ("a", "b", "c")
    for name in names:
        catalog.create_table(principal, TableRef("dev", _SILVER, name), schema=_minimal_schema())
    for name in names:
        catalog.create_table(
            principal, TableRef("dev", _SILVER, f"x{name}"), schema=_minimal_schema()
        )
        catalog.drop_table(principal, TableRef("dev", _SILVER, f"x{name}"))
    deploy_assets(session, "dev", "analytics", {f"silver.{n}": {} for n in names})
    deploy_assets(session, "dev", "analytics", {})
    artifacts = artifact_service(session, tmp_path, allow)
    for name in names:
        store_version(
            artifacts, principal, TableRef("dev", ARTIFACT_REF.namespace, name), {"m": b"m"}
        )
    spy = _CountingBatch()

    dropped = CatalogService(
        session, StorageResolver(session), spy
    ).list_environment_dropped_tables(principal, "dev")
    assert (len(dropped.items), spy.drop_batches) == (3, 1)
    orphans = DeployService(session, spy).orphans(principal, "dev").orphans
    assert (len(orphans), spy.drop_batches) == (3, 2)
    listed = ArtifactService(session, StorageResolver(session), spy).list_artifacts(
        principal, "dev"
    )
    assert (len(listed.items), spy.drop_batches) == (3, 3)
    assert all(item.actions == ("drop",) for item in [*orphans, *listed.items])


def test_the_batched_fill_reads_as_much_for_three_tables_as_for_one(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """I1: `with_row_actions` reads the definitions once and asks the rights in one batch."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    refs = [TableRef("dev", _SILVER, n) for n in ("a", "b", "c")]
    for ref in refs:
        catalog.create_table(principal, ref, schema=_minimal_schema())
    deploy_assets(session, "dev", "analytics", {"silver.a": {}})
    grants = GrantAuthorization(session)
    GrantService(session, AllowAllAuthorization()).grant(
        principal, principal=principal.name, role=Role.ADMIN, kind=SecurableKind.INSTANCE
    )
    reader = CatalogService(session, StorageResolver(session), grants)
    bare = [reader.describe_table(principal, ref, with_actions=False) for ref in refs]
    assert all(d.actions == () and d.defined_by is None for d in bare)
    engine = session.get_bind()
    assert isinstance(engine, Engine)

    def count(described: list[TableDescription]) -> int:
        """Count the statements one fill sends.

        Parameters
        ----------
        described
            The descriptions to fill.

        Returns
        -------
        int
            The statements.
        """
        fresh = CatalogService(session, StorageResolver(session), GrantAuthorization(session))
        statements: list[str] = []

        def record(*args: object) -> None:
            """Record one statement.

            Parameters
            ----------
            *args
                The cursor event's arguments.
            """
            statements.append(str(args[2]))

        event.listen(engine, "before_cursor_execute", record)
        try:
            fresh.with_row_actions(principal, described)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    assert count(bare[:1]) == count(bare)
    filled = reader.with_row_actions(principal, bare)
    assert [d.defined_by is not None for d in filled] == [True, False, False]
    assert all("drop" in d.actions for d in filled)


def test_a_table_dropped_between_the_describe_and_the_fill_gets_no_actions(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A concurrent drop leaves the row without actions instead of failing the whole list."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    kept = TableRef("dev", _SILVER, "kept")
    gone = TableRef("dev", _SILVER, "gone")
    for ref in (kept, gone):
        catalog.create_table(principal, ref, schema=_minimal_schema())
    bare = [catalog.describe_table(principal, ref, with_actions=False) for ref in (kept, gone)]
    catalog.drop_table(principal, gone)

    filled = catalog.with_row_actions(principal, bare)

    assert [d.ref.name for d in filled] == ["kept", "gone"]
    assert "drop" in filled[0].actions
    assert (filled[1].actions, filled[1].defined_by) == ((), None)


def test_a_columns_only_describe_reads_no_definition(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Callers that want only the columns (access, policy) do not pay for the actions."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    catalog.create_table(principal, _CLEAN, schema=_minimal_schema())

    def refuse(*args: object) -> None:
        """Fail: the definitions were read.

        Parameters
        ----------
        *args
            Ignored.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError("read the deployed definitions")

    monkeypatch.setattr(service_module, "deployed_definitions", refuse)
    described = catalog.describe_table(principal, _CLEAN, with_actions=False)
    assert (described.defined_by, described.actions) == (None, ())


@pytest.mark.parametrize(("may_read", "named"), [(True, True), (False, False)])
def test_a_create_or_restore_answer_names_the_file_only_to_a_reader(
    session: Session, tmp_path: Path, principal: PrincipalRef, may_read: bool, named: bool
) -> None:
    """M1 and ruling 2: such an answer may go to a caller who may not read the table."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    catalog.create_table(principal, _CLEAN, schema=_minimal_schema())
    deploy_assets(session, "dev", "analytics", {"silver.clean": {}})
    rights = [Action.DROP_TABLE, *([Action.READ_TABLE] if may_read else [])]
    caller = CatalogService(session, StorageResolver(session), ActionAllowList(*rights))

    defined_by, actions = caller.row_actions(principal, _CLEAN)

    assert (defined_by is not None) == named
    assert actions == ("drop", "set_retention")  # the principal created, so owns, the table


def test_the_row_fill_names_no_file_to_a_caller_who_may_not_read_the_row(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """N1: `_row_actions` itself gates `defined_by` on READ_TABLE, asked in the same batch."""
    catalog = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    catalog.create_table(principal, _CLEAN, schema=_minimal_schema())
    deploy_assets(session, "dev", "analytics", {"silver.clean": {}})
    environment_id = session.scalar(select(Namespace.environment_id).limit(1))
    assert environment_id is not None
    stranger = _p()
    rows = [(_CLEAN, "TABLE", principal.id, environment_id)]

    for rights, named in (
        ((Action.DROP_TABLE,), False),
        ((Action.DROP_TABLE, Action.READ_TABLE), True),
    ):
        batch = _Batch(ActionAllowList(*rights))
        caller = CatalogService(session, StorageResolver(session), batch)
        ((defined_by, actions),) = caller._row_actions(stranger, rows)
        assert (defined_by is not None, actions) == (named, ("drop",))
        assert batch.calls == 1
