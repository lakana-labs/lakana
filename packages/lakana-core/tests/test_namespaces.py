"""Tests for namespace operations."""

import uuid
from pathlib import Path
from unittest import mock

import pytest
from core_test_helpers import ActionAllowList, _config, _prepared_catalog, _RecordThenDeny
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, PrincipalRef, Securable
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Namespace, Table, TableType
from lakana_core.storage.resolver import StorageResolver


def test_create_then_load_round_trips(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A created namespace can be loaded back by the same path."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["gold", "sales"])

    created = catalog.create_namespace(principal, "dev", path, {"owner": "analytics"})
    loaded = catalog.load_namespace(principal, "dev", path)

    assert loaded.id == created.id
    assert loaded.properties == {"owner": "analytics"}


def test_create_is_rejected_when_the_namespace_exists(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The same path twice in one environment is an error."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])
    catalog.create_namespace(principal, "dev", path)

    with pytest.raises(AlreadyExistsError):
        catalog.create_namespace(principal, "dev", path)


def test_create_namespace_race_on_the_unique_constraint_raises_already_exists(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A concurrent writer that wins the insert race is still refused.

    The pre-existence check only produces a good error message in the
    common case; the ``(environment_id, path)`` unique constraint is what
    actually arbitrates a genuine race between two servers against one
    Postgres. A real concurrent writer cannot be exercised against a single
    in-process session, so the race is simulated by patching the pre-check
    to miss the row that is, in fact, already there, forcing the insert
    itself to hit the constraint. Without the handler the loser sees a raw
    SQLAlchemy `IntegrityError` instead of `AlreadyExistsError`.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])
    catalog.create_namespace(principal, "dev", path)

    with (
        mock.patch.object(CatalogService, "_lookup_namespace", return_value=None),
        pytest.raises(AlreadyExistsError),
    ):
        catalog.create_namespace(principal, "dev", path)


def test_create_namespace_reports_conflict_when_the_integrity_error_is_not_a_collision(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An `IntegrityError` that is not a path collision must not be misreported as one.

    Dropping the environment from another session between the pre-check and
    the insert manifests as a foreign-key violation, not a
    unique-constraint violation. Beyond the misclassification, SQLAlchemy's
    `IntegrityError` embeds the failed statement and its bound parameters in
    its `str()`, which must not reach a caller -- the same leak
    `create_table`'s handler exists to avoid.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])

    def _fail(self: Session) -> None:
        raise IntegrityError(
            "INSERT INTO namespaces (id, path) VALUES (?, ?)",
            {"id": "secret-uuid", "path": "gold"},
            Exception("FOREIGN KEY constraint failed"),
        )

    with (
        mock.patch.object(Session, "commit", _fail),
        pytest.raises(ConflictError) as excinfo,
    ):
        catalog.create_namespace(principal, "dev", path)

    assert "INSERT INTO" not in str(excinfo.value)
    assert "secret-uuid" not in str(excinfo.value)


def test_create_namespace_authorizes_before_it_looks_the_namespace_up(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Refusal must not depend on whether the namespace already exists.

    The other `create_*` authorization tests all run against a target that
    does not exist, so they stay green even if the check is moved after the
    existence lookup. This one runs against a namespace that *does* exist:
    with the check misordered the caller learns the namespace is there
    (`AlreadyExistsError`) despite holding no right to ask.
    """
    path = NamespacePath.from_parts(["widgets"])
    recorder = _RecordThenDeny()
    catalog = _prepared_catalog(session, tmp_path, recorder, namespaces=[path])

    with pytest.raises(PermissionDenied):
        catalog.create_namespace(principal, "dev", path)

    assert recorder.seen == (Action.CREATE_NAMESPACE, Securable.namespace_scope("dev", path))


def test_the_same_path_is_allowed_in_two_environments(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Uniqueness is per environment, which is what makes dev/prod parity possible."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path / "dev"))
    catalog.create_environment(principal, name="prd", storage=_config(tmp_path / "prd"))
    path = NamespacePath.from_parts(["widgets"])

    catalog.create_namespace(principal, "dev", path)
    catalog.create_namespace(principal, "prd", path)

    assert catalog.load_namespace(principal, "prd", path).path == path.joined()


def test_load_unknown_raises_not_found(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A missing namespace is an error, not None."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    with pytest.raises(NotFoundError):
        catalog.load_namespace(principal, "dev", NamespacePath.from_parts(["nope"]))


def test_create_in_an_unknown_environment_raises_not_found(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    """The environment is resolved before the namespace is created."""
    with pytest.raises(NotFoundError):
        catalog.create_namespace(principal, "nope", NamespacePath.from_parts(["widgets"]))


def test_list_without_a_parent_returns_every_namespace(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The unfiltered listing is the whole environment, in a stable order."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["gold", "sales"]))

    listed = catalog.list_namespaces(principal, "dev")

    assert [p.parts for p in listed] == [
        ("bronze",),
        ("gold",),
        ("gold", "sales"),
        ("silver",),
    ]


def test_list_orders_hierarchically_by_parts(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A child sorts immediately after its parent, not after its siblings.

    `bronze`/`gold`/`gold.sales` (the fixture above) happen to sort
    identically whichever way the unit separator (U+001F) compares to
    other characters, so that fixture alone does not pin down the ordering
    rule. `gold`/`gold.sales`/`golda`, created here out of order, forces
    `gold.sales` to land between `gold` and `golda` specifically because
    `list_namespaces` sorts on the parsed `parts` tuple rather than the
    stored joined string. This test runs on SQLite only, whose BINARY
    collation happens to order U+001F below every namespace-legal ASCII
    character even when sorted in SQL — it cannot by itself tell a
    Python-side sort from a SQL-side one. That distinction is what
    `test_postgres_integration.py::test_list_namespaces_orders_hierarchically_under_a_real_glibc_collation`
    exists to prove, against a real glibc collation where the two
    orderings actually diverge.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    for parts in (["golda"], ["gold", "sales"]):
        catalog.create_namespace(principal, "dev", NamespacePath.from_parts(parts))

    listed = catalog.list_namespaces(principal, "dev")

    assert [p.parts for p in listed] == [
        ("bronze",),
        ("gold",),
        ("gold", "sales"),
        ("golda",),
        ("silver",),
    ]


def test_list_with_a_parent_returns_only_its_descendants(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Prefix matching gives hierarchy without an array column."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    for parts in (["gold", "sales"], ["gold", "sales", "eu"]):
        catalog.create_namespace(principal, "dev", NamespacePath.from_parts(parts))

    listed = catalog.list_namespaces(principal, "dev", NamespacePath.from_parts(["gold"]))

    assert [p.parts for p in listed] == [("gold", "sales"), ("gold", "sales", "eu")]


def test_a_parent_is_not_its_own_descendant(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`gold` must not appear in the listing of `gold`'s children."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))

    assert catalog.list_namespaces(principal, "dev", NamespacePath.from_parts(["gold"])) == []


def test_a_like_wildcard_in_a_name_does_not_widen_the_listing(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`_` is a legal namespace character and a SQL LIKE wildcard.

    Without escaping, listing the children of `a_b` also returns those of
    `axb`, because `_` matches any single character. This is the test that
    fails if the prefix is interpolated into LIKE unescaped.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    for parts in (["a_b"], ["a_b", "child"], ["axb"], ["axb", "intruder"]):
        catalog.create_namespace(principal, "dev", NamespacePath.from_parts(parts))

    listed = catalog.list_namespaces(principal, "dev", NamespacePath.from_parts(["a_b"]))

    assert [p.parts for p in listed] == [("a_b", "child")]


def test_a_backslash_in_a_name_does_not_widen_the_listing(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    r"""The escape character itself may be in a stored namespace name.

    A new name may not hold a backslash (#609), but one an older release stored may, and the
    prefix is interpolated into a `LIKE` pattern that uses `\` as its escape character. Left
    undoubled, the `\b` in `a\b` is read as an escaped `b` -- a literal `b` -- so the pattern
    silently becomes `ab...` and the listing returns another namespace's children. This is the
    test that fails if `escape_like` drops its backslash clause.
    """
    env = catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    for parts in ([r"a\b"], [r"a\b", "child"], ["ab"], ["ab", "intruder"]):
        session.add(
            Namespace(
                id=uuid.uuid4(),
                environment_id=env.id,
                path=NamespacePath.from_parts(parts).joined(),
                properties={},
            )
        )
    session.commit()

    listed = catalog.list_namespaces(principal, "dev", NamespacePath.from_parts([r"a\b"]))

    assert [p.parts for p in listed] == [(r"a\b", "child")]


def test_a_percent_in_a_name_does_not_widen_the_listing(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`%` is the other LIKE wildcard; a name from before `%` was refused may still hold one."""
    env = catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    for parts in (["a%"], ["a%", "child"], ["ab"], ["ab", "intruder"]):
        session.add(
            Namespace(
                id=uuid.uuid4(),
                environment_id=env.id,
                path=NamespacePath.from_parts(parts, allow_dots=True).joined(),
                properties={},
            )
        )
    session.commit()
    with pytest.raises(ValidationError, match="'%'"):
        catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["b%"]))

    listed = catalog.list_namespaces(
        principal, "dev", NamespacePath.from_parts(["a%"], allow_dots=True)
    )

    assert [p.parts for p in listed] == [("a%", "child")]


def test_drop_removes_the_namespace(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A dropped namespace can no longer be loaded."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])
    catalog.create_namespace(principal, "dev", path)

    catalog.drop_namespace(principal, "dev", path)

    with pytest.raises(NotFoundError):
        catalog.load_namespace(principal, "dev", path)


def test_drop_unknown_raises_not_found(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Dropping a missing namespace is an error, not a silent no-op."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    with pytest.raises(NotFoundError):
        catalog.drop_namespace(principal, "dev", NamespacePath.from_parts(["nope"]))


def test_drop_is_rejected_when_the_namespace_contains_a_table(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """A namespace holding a table cannot be dropped out from under it.

    `Table.namespace_id` cascades on delete at the database level, so
    without this check dropping the namespace would silently destroy the
    table too. The table row is inserted directly through the session
    rather than via `catalog.create_table`, to keep this test
    independent of table creation and its own object-storage side effects.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])
    ns = catalog.create_namespace(principal, "dev", path)
    session.add(
        Table(
            id=uuid.uuid4(),
            namespace_id=ns.id,
            name="sales",
            table_type=TableType.ICEBERG,
            metadata_location=None,
            previous_metadata_location=None,
        )
    )
    session.commit()

    with pytest.raises(ConflictError):
        catalog.drop_namespace(principal, "dev", path)

    assert catalog.table_exists(
        principal, TableRef(environment="dev", namespace=path, name="sales")
    )


def test_drop_is_rejected_when_the_namespace_has_a_child_namespace(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Dropping a namespace with a child would orphan the child.

    There is no namespace-to-namespace foreign key, so nothing at the
    database level would catch `gold.sales` being left behind under a
    `gold` that no longer exists.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["gold", "sales"]))

    with pytest.raises(ConflictError):
        catalog.drop_namespace(principal, "dev", NamespacePath.from_parts(["gold"]))


def test_each_namespace_operation_authorizes_its_own_action(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Every operation consults authorization with the exact expected pair.

    Mirrors Phase A's `test_authorization_is_actually_consulted`: a recorder
    that denies on the first call, so a missing `authorize` shows up as an
    operation that succeeded rather than as a silent pass. Setup uses
    `_prepared_catalog` rather than assigning to the private `_authz`
    attribute, so each call is checked against a catalog that was built with
    the recorder from the start.
    """
    path = NamespacePath.from_parts(["widgets"])
    expected = [
        (Action.CREATE_NAMESPACE, Securable.namespace_scope("dev", path)),
        (Action.READ_NAMESPACE, Securable.namespace_scope("dev", path)),
        (Action.READ_NAMESPACE, Securable.environment_scope("dev")),
        (Action.DROP_NAMESPACE, Securable.namespace_scope("dev", path)),
        (Action.MANAGE_NAMESPACE, Securable.namespace_scope("dev", path)),
    ]
    calls = [
        lambda c, p: c.create_namespace(p, "dev", path),
        lambda c, p: c.load_namespace(p, "dev", path),
        lambda c, p: c.list_namespaces(p, "dev"),
        lambda c, p: c.drop_namespace(p, "dev", path),
        lambda c, p: c.update_namespace_properties(p, "dev", path, {}, []),
    ]
    for call, want in zip(calls, expected, strict=True):
        recorder = _RecordThenDeny()
        catalog = _prepared_catalog(session, tmp_path, recorder)
        with pytest.raises(PermissionDenied):
            call(catalog, principal)
        assert recorder.seen == want


def test_updates_and_removals_are_both_applied(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Setting a new key, overwriting an existing one, and unsetting a third all take effect.

    Overwriting `keep` is the case that catches `properties = {**updates,
    **properties}` — an insert-if-absent merge that silently refuses to
    change any value that already exists, which is the most common real use
    of `updateProperties`.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])
    catalog.create_namespace(principal, "dev", path, {"keep": "1", "drop": "2"})

    summary = catalog.update_namespace_properties(
        principal, "dev", path, {"added": "3", "keep": "9"}, ["drop"]
    )

    assert summary.updated == ["added", "keep"]
    assert summary.removed == ["drop"]
    assert summary.missing == []
    assert catalog.load_namespace(principal, "dev", path).properties == {
        "keep": "9",
        "added": "3",
    }


def test_removing_an_absent_key_is_reported_as_missing_not_an_error(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """The Iceberg REST spec reports absent removals rather than failing."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])
    catalog.create_namespace(principal, "dev", path)

    summary = catalog.update_namespace_properties(principal, "dev", path, {}, ["ghost"])

    assert summary.missing == ["ghost"]
    assert summary.removed == []


def test_a_duplicated_removal_of_a_present_key_removes_it_once(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """A repeated removal is idempotent, not a crash.

    `removals` arrives as a plain JSON list, so a client can trivially send
    the same key twice. Computing `removed` against the pre-mutation
    dictionary listed the key once per occurrence, and the second `del`
    raised a builtin `KeyError` that would fall straight through to a 500.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])
    catalog.create_namespace(principal, "dev", path, {"owner": "analytics", "keep": "1"})

    summary = catalog.update_namespace_properties(principal, "dev", path, {}, ["owner", "owner"])

    assert summary.removed == ["owner"]
    assert summary.missing == []
    assert catalog.load_namespace(principal, "dev", path).properties == {"keep": "1"}


def test_a_duplicated_removal_of_an_absent_key_is_reported_once(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An absent key repeated in `removals` is reported as missing exactly once."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])
    catalog.create_namespace(principal, "dev", path)

    summary = catalog.update_namespace_properties(principal, "dev", path, {}, ["ghost", "ghost"])

    assert summary.missing == ["ghost"]
    assert summary.removed == []


def test_a_key_in_both_updates_and_removals_is_rejected(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """An overlapping key has no defined outcome, so it is refused outright."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])
    catalog.create_namespace(principal, "dev", path)

    with pytest.raises(ValidationError):
        catalog.update_namespace_properties(principal, "dev", path, {"k": "v"}, ["k"])


def test_the_namespace_is_untouched_when_the_update_is_rejected(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Validation happens before any mutation is written."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    path = NamespacePath.from_parts(["widgets"])
    catalog.create_namespace(principal, "dev", path, {"k": "original"})

    with pytest.raises(ValidationError):
        catalog.update_namespace_properties(principal, "dev", path, {"k": "new"}, ["k"])

    assert catalog.load_namespace(principal, "dev", path).properties == {"k": "original"}


def test_update_properties_requires_manage_not_read_or_create(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Mutating properties is a manage operation and nothing weaker.

    Granting READ_NAMESPACE and CREATE_NAMESPACE must not be enough. This is
    the test that fails if the action is quietly reused rather than added.
    """
    path = NamespacePath.from_parts(["widgets"])

    class _PermitAllButManage:
        """Allows everything except MANAGE_NAMESPACE."""

        def authorize(self, principal: PrincipalRef, action: Action, securable: Securable) -> None:
            """Deny only the manage action.

            Parameters
            ----------
            principal
                The acting principal.
            action
                The attempted action.
            securable
                The target.

            Raises
            ------
            PermissionDenied
                When `action` is `MANAGE_NAMESPACE`.
            """
            if action is Action.MANAGE_NAMESPACE:
                raise PermissionDenied("nope")

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

    catalog = _prepared_catalog(session, tmp_path, _PermitAllButManage(), namespaces=[path])

    with pytest.raises(PermissionDenied):
        catalog.update_namespace_properties(principal, "dev", path, {"k": "v"}, [])


def _insert_dotted_namespace(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, name: str
) -> NamespacePath:
    """Create `dev` and insert a dotted namespace row, bypassing validation.

    Parameters
    ----------
    session
        Database session.
    catalog
        The catalog under test.
    principal
        The acting principal.
    tmp_path
        Directory backing the environment's storage.
    name
        The stored (joined) path.

    Returns
    -------
    NamespacePath
        The parsed path of the inserted row.
    """
    from lakana_core.models import Namespace

    env = catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    session.add(Namespace(id=uuid.uuid4(), environment_id=env.id, path=name, properties={}))
    session.commit()
    return NamespacePath.parse(name)


def test_create_namespace_rejects_a_dotted_component(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """Only the new component is checked, so a lenient path cannot smuggle in a dotted name."""
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    lenient = NamespacePath.from_parts(["a.b"], allow_dots=True)

    with pytest.raises(ValidationError, match=r"'a\.b'"):
        catalog.create_namespace(principal, "dev", lenient)


def test_an_existing_dotted_namespace_stays_listable_loadable_and_droppable(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    """A namespace created before dots were refused is deprecated, not unreachable."""
    path = _insert_dotted_namespace(session, catalog, principal, tmp_path, "legacy.name")

    assert path in catalog.list_namespaces(principal, "dev")
    assert catalog.load_namespace(principal, "dev", path).path == "legacy.name"
    child = catalog.create_namespace(principal, "dev", path.child("fine"))
    assert child.path == "legacy.name\x1ffine"
    catalog.drop_namespace(principal, "dev", path.child("fine"))
    catalog.drop_namespace(principal, "dev", path)

    assert path not in catalog.list_namespaces(principal, "dev")


def _rows_in(session: Session, ns: Namespace, *names: str, kind: TableType) -> None:
    """Insert bare table rows of one kind into a namespace.

    Parameters
    ----------
    session
        Database session; committed.
    ns
        The namespace.
    *names
        The rows' names.
    kind
        Their type.
    """
    for name in names:
        session.add(Table(id=uuid.uuid4(), namespace_id=ns.id, name=name, table_type=kind))
    session.commit()


def test_a_namespace_that_is_not_empty_says_how_many_it_holds(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> None:
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    tables = catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["widgets"]))
    _rows_in(session, tables, "a", "b", kind=TableType.ICEBERG)
    models = catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["models"]))
    _rows_in(session, models, "churn", kind=TableType.ARTIFACT)
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["parent"]))
    catalog.create_namespace(principal, "dev", NamespacePath.from_parts(["parent", "child"]))

    said = []
    for path in ("widgets", "models", "parent"):
        with pytest.raises(ConflictError) as refused:
            catalog.drop_namespace(principal, "dev", NamespacePath.from_parts([path]))
        said.append(str(refused.value))

    assert said == [
        "widgets still holds 2 tables.",
        "models still holds 1 artifact; drop each first (`lakana artifact drop`).",
        "parent still holds 1 namespace.",
    ]


def test_a_namespace_with_a_table_the_caller_may_not_read_says_only_that_it_is_not_empty(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    setup = _prepared_catalog(session, tmp_path, AllowAllAuthorization())
    ns = setup.create_namespace(principal, "dev", NamespacePath.from_parts(["widgets"]))
    _rows_in(session, ns, "a", "b", "c", kind=TableType.ICEBERG)
    dropper = CatalogService(
        session, StorageResolver(session), ActionAllowList(Action.DROP_NAMESPACE)
    )

    with pytest.raises(ConflictError) as refused:
        dropper.drop_namespace(principal, "dev", NamespacePath.from_parts(["widgets"]))

    assert str(refused.value) == "widgets is not empty; drop what it holds first."


def _ns_tree(catalog: CatalogService, principal: PrincipalRef, tmp_path: Path) -> None:
    """Create `a`, `a.b`, `a.b.c` and `ab` in `dev`.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    tmp_path
        Holds the storage root.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    for parts in (["a"], ["a", "b"], ["a", "b", "c"], ["ab"]):
        catalog.create_namespace(principal, "dev", NamespacePath.from_parts(parts))


def test_namespaces_page_by_parts_under_a_parent(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`a.b` and `a.b.c` lie under `a`; `ab` does not. One to a page makes two pages."""
    _ns_tree(catalog, principal, tmp_path)
    parent = NamespacePath.from_parts(["a"])

    whole = catalog.list_namespace_page(principal, "dev", parent)
    first = catalog.list_namespace_page(principal, "dev", parent, limit=1)
    second = catalog.list_namespace_page(
        principal, "dev", parent, limit=1, cursor=first.next_cursor
    )

    assert [ns.parts for ns in whole.items] == [("a", "b"), ("a", "b", "c")]
    assert [ns.parts for ns in first.items] == [("a", "b")]
    assert [ns.parts for ns in second.items] == [("a", "b", "c")]
    assert second.next_cursor is None


def test_namespaces_depth_one_keeps_only_direct_children(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """With `depth=1`, the children of `a` are `a.b` alone; with no parent, the top level."""
    _ns_tree(catalog, principal, tmp_path)

    under_a = catalog.list_namespace_page(
        principal, "dev", NamespacePath.from_parts(["a"]), depth=1
    )
    top = catalog.list_namespace_page(principal, "dev", None, depth=1)

    assert [ns.parts for ns in under_a.items] == [("a", "b")]
    assert [ns.parts for ns in top.items] == [
        ("a",),
        ("ab",),
        ("bronze",),
        ("gold",),
        ("silver",),
    ]


def test_namespaces_page_with_a_legacy_dotted_part_has_unique_keys(
    session: Session, catalog: CatalogService, principal: PrincipalRef, tmp_path: Path
) -> None:
    """`["a.b"]` and `["a", "b"]` both dot to `a.b`; paging still skips and repeats nothing."""
    _ns_tree(catalog, principal, tmp_path)
    env_id = session.scalars(select(Namespace.environment_id)).first()
    session.add(Namespace(id=uuid.uuid4(), environment_id=env_id, path="a.b", properties={}))
    session.commit()

    seen = []
    cursor = None
    while True:
        page = catalog.list_namespace_page(principal, "dev", None, limit=1, cursor=cursor)
        seen += [ns.parts for ns in page.items]
        cursor = page.next_cursor
        if cursor is None:
            break

    assert len(seen) == len(set(seen)) == 8  # the 5 here and the scaffolded bronze, silver, gold
    assert seen == sorted(seen)
