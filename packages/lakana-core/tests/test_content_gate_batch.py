"""The content gate of many files at once: the same decisions, a bounded number of statements.

`ContentGate` judges each file as `passes_content_gate` did before it was batched (#589); the
reference below is that function as it stood, kept here to compare against. The project-versions
route asks it for every file of a version, at a cost that grows by at most one statement per file.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from test_projects_definition import (
    _analyst,
    _person,
    _policy,
    _rec,
    _retype,
    _tables,
    _version,
    _with_sql,
    _world,
)

from lakana_core.auth import Action, AuthorizationService, PrincipalRef, Securable
from lakana_core.errors import ValidationError
from lakana_core.grants.authorization import GrantAuthorization
from lakana_core.identifiers import TableRef
from lakana_core.models import TableType
from lakana_core.policy.field_binding import UnreadablePolicyError
from lakana_core.policy.resolver import PolicyResolver
from lakana_core.projects.definition import ContentGate, _live_table, passes_content_gate


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
    resolver = PolicyResolver(session)
    for ref in gate.values():
        live = _live_table(session, ref)
        if live is None:
            continue
        if live.table_type is TableType.VIEW and (not defines or ref != own):
            return False
        try:
            policy = resolver.resolve(p, ref, table_id=live.id)
        except UnreadablePolicyError:
            return False
        if policy.row_filters or policy.column_masks:
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
    _tables(session, "gold.v", "silver.masked", "silver.filtered", "silver.bogus", "silver.other")
    _retype(session, "gold.v", TableType.VIEW)
    viewer = _analyst(session, "viewer")
    other = _analyst(session, "other")
    for reader in (viewer, other):
        _policy(session, "mask", reader, "silver.masked")
        _policy(session, "filter", reader, "silver.filtered")
        _policy(session, "mask", reader, "silver.bogus", binding="bogus")
    _policy(session, "mask", other, "silver.a")
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
