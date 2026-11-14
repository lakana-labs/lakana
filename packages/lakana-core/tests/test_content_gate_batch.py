"""The content gate of many files at once: the same decisions, a bounded number of statements.

`ContentGate` judges each file as `passes_content_gate` did before it was batched (#589); the
reference below is that function as it stood, kept here to compare against. The project-versions
route asks it for every file of a version, at a cost that grows by at most one statement per file.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_projects_definition import (
    _analyst,
    _file,
    _person,
    _policy,
    _rec,
    _retype,
    _source_viewed,
    _tables,
    _version,
    _with_sql,
    _world,
)

from lakana_core.audit.service import AuditService
from lakana_core.audit.views import sessions_like
from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import AuditUnavailableError, ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import NAMESPACE_SEPARATOR, TableRef
from lakana_core.models import (
    ColumnMask,
    Environment,
    Group,
    GroupMembership,
    Namespace,
    RowFilter,
    Table,
    TableType,
)
from lakana_core.projects.definition import ContentGate, passes_content_gate


def _reference_ref(environment: str, entry: object) -> TableRef | None:
    """Read one gate entry as the gate did before #589.

    Parameters
    ----------
    environment
        The environment's name.
    entry
        A `TableRef` or a dotted name.

    Returns
    -------
    TableRef or None
        The table, or `None` for anything that refuses.
    """
    if isinstance(entry, TableRef):
        return entry if entry.environment == environment else None
    if not isinstance(entry, str):
        return None
    try:
        return TableRef.from_dotted(environment, entry)
    except ValidationError:
        return None


def _reference_policy_applies(session: Session, p: PrincipalRef, table_id: object) -> bool:
    """Say whether an active row filter or column mask applies to `p` on a table, in plain SQL.

    Without a schema, every applicable row either is a filter or mask of the reader or cannot
    be read; the gate refuses on both, so any active row of the reader or its groups refuses.

    Parameters
    ----------
    session
        Database session.
    p
        The reader.
    table_id
        The table.

    Returns
    -------
    bool
        Whether such a row exists.
    """
    groups = select(GroupMembership.group_id).where(GroupMembership.principal_id == p.id)
    for model in (RowFilter, ColumnMask):
        found = session.execute(
            select(model.id).where(
                model.table_id == table_id,
                model.revoked_at.is_(None),
                or_(model.principal_id == p.id, model.group_id.in_(groups)),
            )
        ).first()
        if found is not None:
            return True
    return False


def _reference_gate(
    session: Session,
    authorization: AuthorizationService,
    p: PrincipalRef,
    environment: str,
    table: object,
    gate_tables: object,
    gate_raw: object,
    *,
    defines: bool = True,
) -> bool:
    """Judge one file exactly as `passes_content_gate` did before #589, one query at a time.

    Parameters
    ----------
    session
        Database session.
    authorization
        Authorization service.
    p
        The reader.
    environment
        The environment's name.
    table
        The table the file defines or checks.
    gate_tables
        The tables the file uses.
    gate_raw
        Whether the file reads a raw path or a connection.
    defines
        Whether the file defines `table`.

    Returns
    -------
    bool
        The decision.
    """
    if not authorization.permits(
        p, Action.READ_ENVIRONMENT, Securable.environment_scope(environment)
    ):
        return False
    if gate_raw is not False or not isinstance(gate_tables, list):
        return False
    own = _reference_ref(environment, table)
    others = [_reference_ref(environment, entry) for entry in gate_tables]
    if own is None or any(ref is None for ref in others):
        return False
    gate = {(ref.namespace.parts, ref.name): ref for ref in (own, *others) if ref is not None}
    for ref in gate.values():
        scope = Securable.table_scope(environment, ref.namespace, ref.name)
        if not authorization.permits(p, Action.READ_TABLE, scope):
            return False
    for ref in gate.values():
        live = session.execute(
            select(Table.id, Table.table_type)
            .join(Namespace, Table.namespace_id == Namespace.id)
            .join(Environment, Namespace.environment_id == Environment.id)
            .where(
                Environment.name == environment,
                Environment.deleted_at.is_(None),
                Namespace.path == NAMESPACE_SEPARATOR.join(ref.namespace.parts),
                Table.name == ref.name,
            )
        ).one_or_none()
        if live is None:
            continue
        if live.table_type is TableType.VIEW and (not defines or ref != own):
            return False
        if _reference_policy_applies(session, p, live.id):
            return False
    return True


_FILES: list[tuple[object, object, object, bool]] = [
    ("gold.f", ["gold.f", "silver.a"], False, True),
    ("gold.f", ["silver.a"], False, True),
    ("gold.f", ["gold.f", "silver.a"], True, True),
    ("gold.f", ["gold.f", "silver.a"], None, True),
    ("gold.f", "silver.a", False, True),
    ("gold.f", ["gold.f", "nodot"], False, True),
    ("gold.f", ["gold.f", 7], False, True),
    ("gold.f", ["gold.f", "silver.later"], False, True),
    ("gold.v", ["gold.v", "silver.a"], False, True),
    ("gold.v", ["gold.v", "silver.a"], False, False),
    ("gold.f", ["gold.f", "gold.v"], False, True),
    ("gold.f", ["gold.f", "silver.masked"], False, True),
    ("gold.f", ["gold.f", "silver.filtered"], False, True),
    ("gold.f", ["gold.f", "silver.bogus"], False, True),
    ("gold.f", ["gold.f", "silver.other"], False, True),
    ("gold.f", ["gold.f", "silver.grouped"], False, True),
    ("gold.fin.sales", ["gold.fin.sales", "silver.a"], False, True),
    ("gold.f", ["gold.f", "gold.fin.sales"], False, True),
    (TableRef.from_dotted("dev", "gold.fin.sales"), ["silver.Mixed"], False, True),
    ("gold.f", ["gold.f", "silver.Mixed"], False, True),
    ("gold.f", ["gold.f", "silver.mixed"], False, True),
    ("gold.f", ["gold.f", "silver.revoked"], False, True),
    ("gold.f", ["gold.f", "silver.a"], False, False),
    (TableRef.from_dotted("dev", "gold.f"), [TableRef.from_dotted("dev", "silver.a")], False, True),
    (TableRef.from_dotted("prod", "gold.f"), ["silver.a"], False, True),
    ("nodot", ["silver.a"], False, True),
]
"""Files across every condition of the gate: views, policies, raw reads, bad names, grants."""


def _readers(session: Session) -> dict[str, PrincipalRef]:
    """Build the gate's world and the readers that see it differently.

    Parameters
    ----------
    session
        Database session; commits.

    Returns
    -------
    dict
        Readers by role.
    """
    _world(session)
    _tables(
        session,
        "gold.v",
        "silver.masked",
        "silver.filtered",
        "silver.bogus",
        "silver.other",
        "silver.grouped",
        "silver.revoked",
        "gold.fin.sales",
        "silver.Mixed",
    )
    _retype(session, "gold.v", TableType.VIEW)
    viewer = _analyst(session, "viewer")
    other = _analyst(session, "other")
    for reader in (viewer, other):
        _policy(session, "mask", reader, "silver.masked")
        _policy(session, "filter", reader, "silver.filtered")
        _policy(session, "mask", reader, "silver.bogus", binding="bogus")
    _policy(session, "mask", other, "silver.a")
    # A namespace of three levels and a name in mixed case: the mask on `silver.Mixed` applies
    # to the viewer; `silver.mixed` is another name, of no table.
    _policy(session, "filter", other, "gold.fin.sales")
    _policy(session, "mask", viewer, "silver.Mixed")
    # A mask through a group (on `silver.grouped`), for the viewer only, and a revoked filter
    # (on `silver.revoked`), which applies to nobody.
    group = Group(id=uuid.uuid4(), name="analysts")
    session.add(group)
    session.add(GroupMembership(group_id=group.id, principal_id=viewer.id))
    session.commit()
    _policy(session, "mask", other, "silver.grouped")
    session.execute(
        update(ColumnMask)
        .where(ColumnMask.table == "grouped")
        .values(principal_id=None, group_id=group.id)
    )
    _policy(session, "filter", viewer, "silver.revoked")
    _policy(session, "filter", other, "silver.revoked")
    session.execute(
        update(RowFilter).where(RowFilter.table == "revoked").values(revoked_at=datetime.now(UTC))
    )
    session.commit()
    return {
        "viewer": viewer,
        "under-a-mask": other,
        "tables-only": _person(session, "tables", "gold.f", "silver.a"),
        "environment-and-some": _person(session, "some", "gold.f", "gold.v", environment=True),
        "environment-and-all": _person(
            session, "all", "gold.f", "gold.v", "silver.a", "silver.other", environment=True
        ),
    }


def test_the_batched_gate_decides_every_file_as_the_gate_did_before(session: Session) -> None:
    readers = _readers(session)
    authz = GrantAuthorization(session)
    answers = set()
    for name, reader in readers.items():
        expected = [
            _reference_gate(session, authz, reader, "dev", t, g, raw, defines=d)
            for t, g, raw, d in _FILES
        ]
        single = [
            passes_content_gate(session, authz, reader, "dev", t, g, raw, defines=d)
            for t, g, raw, d in _FILES
        ]
        gate = ContentGate(session, GrantAuthorization(session), reader, "dev")
        gate.prepare((t, g) for t, g, _, _ in _FILES)
        batched = [gate.passes(t, g, raw, defines=d) for t, g, raw, d in _FILES]
        assert single == expected, name
        assert batched == expected, name
        answers.add(tuple(expected))
    viewer = readers["viewer"]
    grouped = ("gold.f", ["gold.f", "silver.grouped"], False)
    revoked = ("gold.f", ["gold.f", "silver.revoked"], False)
    assert not passes_content_gate(session, authz, viewer, "dev", *grouped)
    assert passes_content_gate(session, authz, readers["under-a-mask"], "dev", *grouped)
    assert passes_content_gate(session, authz, viewer, "dev", *revoked)
    assert not passes_content_gate(session, authz, viewer, "dev", "gold.f", ["silver.Mixed"], False)
    assert passes_content_gate(session, authz, viewer, "dev", "gold.f", ["silver.mixed"], False)
    assert passes_content_gate(session, authz, viewer, "dev", "gold.fin.sales", ["silver.a"], False)
    other = readers["under-a-mask"]
    assert not passes_content_gate(
        session, authz, other, "dev", "gold.f", ["gold.fin.sales"], False
    )
    # The cases tell the readers apart, and some files pass.
    assert len(answers) >= 3
    assert any(any(each) for each in answers)


def test_an_unprepared_gate_reads_what_a_file_names_on_demand(session: Session) -> None:
    readers = _readers(session)
    for reader in readers.values():
        gate = ContentGate(session, GrantAuthorization(session), reader, "dev")
        gate.prepare([("gold.f", ["gold.f"])])
        authz = GrantAuthorization(session)
        for t, g, raw, d in _FILES:
            assert gate.passes(t, g, raw, defines=d) == _reference_gate(
                session, authz, reader, "dev", t, g, raw, defines=d
            )


def _counting(session: Session, call: Callable[[], object]) -> int:
    """Count the statements `call` sends, on every session of the engine.

    Parameters
    ----------
    session
        A session on the engine watched.
    call
        What to run.

    Returns
    -------
    int
        The number of statements.
    """
    seen: list[str] = []
    engine = session.get_bind()

    def record(_conn: object, _cur: object, statement: str, *_: object) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(seen)


_FIXED_STATEMENTS = 21
"""What a project version costs apart from the files it shows (#589: 63 statements for one
asset and 882 for 40 before the gate was batched); the rows of the files shown are inserted
together, so today the count does not grow with them at all."""


@pytest.mark.parametrize("assets", [1, 12, 40])
def test_a_project_version_costs_a_fixed_number_of_statements_plus_one_per_file(
    session: Session, assets: int
) -> None:
    names = [f"gold.t{i}" for i in range(assets)]
    _tables(session, *names, "silver.b")
    _world(session, *(_rec(name, "silver.a", "silver.b") for name in names))
    _with_sql(session, *("SELECT * FROM gold.f WHERE a IS NULL" for _ in range(3)))
    analyst = _analyst(session)
    reader = _person(session, "masked", viewer=True)
    _policy(session, "mask", reader, "silver.b")

    shown = _counting(session, lambda: _version(session, analyst))
    again = _counting(session, lambda: _version(session, analyst))
    withheld = _counting(session, lambda: _version(session, reader))

    files = assets + 1
    assert shown <= _FIXED_STATEMENTS + files
    assert again <= _FIXED_STATEMENTS
    assert withheld <= _FIXED_STATEMENTS


_DEFINED_IN_STATEMENTS = 20
"""What one "Defined in" call costs once its read is recorded, whatever the environment holds."""


def test_defined_in_reads_only_the_tables_its_file_uses(session: Session) -> None:
    _world(session)
    analyst = _analyst(session)
    statements: list[str] = []
    engine = session.get_bind()

    def seen(_conn: object, _cur: object, statement: str, *_: object) -> None:
        statements.append(statement)

    def count() -> int:
        statements.clear()
        event.listen(engine, "before_cursor_execute", seen)
        try:
            assert _file(session, analyst).content is not None
        finally:
            event.remove(engine, "before_cursor_execute", seen)
        return len(statements)

    count()  # writes the SOURCE_VIEWED row; the calls below find it within the window
    few = count()
    _tables(session, *(f"silver.extra{i}" for i in range(60)))
    many = count()

    assert few == many <= _DEFINED_IN_STATEMENTS
    live = [
        s for s in statements if s.startswith("SELECT tables.id, tables.table_type, namespaces")
    ]
    assert live and all(" IN " in s for s in live)
    # No statement reads the environment's tables without naming the ones it wants, so the
    # rows read do not grow with the environment either.
    unfiltered = [
        s
        for s in statements
        if "FROM tables" in s and "tables.name = " not in s and "tables.name) IN" not in s
    ]
    assert unfiltered == []


@pytest.mark.parametrize("failing", ["write", "commit"])
def test_a_project_version_whose_audit_write_fails_after_the_lookup_records_and_shows_nothing(
    session: Session, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """The rows of a response land together or not at all, and nothing is returned (#589).

    Parameters
    ----------
    session
        Database session.
    monkeypatch
        Makes the second row's write, or the commit, fail.
    failing
        Which step fails.
    """
    _tables(session, "gold.g")
    _world(session, _rec("gold.f", "silver.a"), _rec("gold.g", "silver.a"))
    _with_sql(session)
    analyst = _analyst(session)
    opened = sessions_like(session)
    written: list[object] = []
    real_record = AuditService.record

    def record(self: AuditService, *args: object, **kwargs: object) -> object:
        written.append(args)
        if failing == "write" and len(written) == 2:
            raise OperationalError("INSERT", {}, Exception("disk full"))
        return real_record(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    def factory() -> Session:
        audit_session = opened()
        if failing == "commit":

            def commit() -> None:
                raise OperationalError("COMMIT", {}, Exception("connection lost"))

            monkeypatch.setattr(audit_session, "commit", commit)
        return audit_session

    monkeypatch.setattr(AuditService, "record", record)
    with pytest.raises(AuditUnavailableError, match="a read of a project file is always recorded"):
        _version(session, analyst, session_factory=factory)

    assert written  # the lookup ran and at least one row was written before the failure
    monkeypatch.undo()
    session.expire_all()
    assert _source_viewed(session) == []


def test_a_reference_that_is_not_a_string_is_hidden_never_an_error(session: Session) -> None:
    """A stored reference, edge end or input that is a dict or a list reads as unreadable."""
    _world(session)
    version = _with_sql(session)
    [asset] = version.manifest_json["assets"]
    version.manifest_json = {
        **version.manifest_json,
        "assets": [{**asset, "references": ["silver.a", {"t": 1}, ["silver.a"]]}],
        "edges": [[{"t": 1}, "gold.f"], [["silver.a"], "gold.f"], ["silver.a", "gold.f"]],
        "external_inputs": [{"t": 1}, ["silver.a"], "silver.a"],
    }
    session.commit()

    view = _version(session, _analyst(session))

    [entry] = view.manifest["assets"]
    assert entry["hidden_references"] == 2 and "sql" not in entry
    assert view.manifest["edges"] == []  # an edge into a hidden asset is dropped, as before
    assert view.manifest["hidden_edges"] == 3
    assert view.manifest["external_inputs"] == ["silver.a"]
    assert view.manifest["hidden_external_inputs"] == 2
