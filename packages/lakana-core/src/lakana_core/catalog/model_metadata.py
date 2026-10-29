"""The model a project declares for a table, stored on the table row and its references as rows.

The asset header's `kind:`, `grain:` and `key:` are written onto the table row
(`Table.model_kind`, `model_grain`, `model_key`), and each of its `references:` becomes a
`DeclaredReference` row, so that REST, search and the metadata graph read them without loading a
manifest. Only the header sets them: a deploy, a rollback and the table's creation by a run write
them, through `lakana_core.catalog.descriptions`; nothing else does.

Every row is stored, whoever may read its target. A reader-facing path must filter by read rights:
a reference by the source table (`DeclaredReference.source_table_id`) and by its target, which is
held by name and resolved in the environment when it is read.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from lakana_core.catalog.health import CheckHealth, latest_checks
from lakana_core.catalog.identity import resolve_table_id
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import DeclaredReference, Namespace, Table
from lakana_core.projects.asset_model import KEY_NONE, ResolvedReference
from lakana_core.projects.manifest import entry_model, manifest_assets
from lakana_core.projects.model_checks import KEY_NOT_NULL, KEY_UNIQUE, REF_PREFIX


@dataclass(frozen=True)
class TableModel:
    """The model a table's row records.

    Parameters
    ----------
    kind
        The declared kind, or `None`.
    grain
        The declared grain, or `None`.
    key
        The declared key columns; empty for no key and for `key: none`.
    key_none
        Whether the header declares `key: none`.
    references
        The declared references, ordered by the referencing column.
    """

    kind: str | None
    grain: str | None
    key: tuple[str, ...]
    key_none: bool
    references: tuple[ResolvedReference, ...]


def write_model_entry(session: Session, row: Table, entry: Mapping[str, Any]) -> None:
    """Write the model of one manifest entry onto a table and its reference rows.

    A reference row the header still declares keeps its id; one it no longer declares is
    deleted. An entry without a model clears what an earlier deploy wrote. A row that is not in
    the database yet (a run creating its table) has no reference rows, so none are read for it,
    and the namespace is read only when a row is added: a new table without a model costs no
    statement. Nothing is committed.

    Parameters
    ----------
    session
        Database session, which must not flush while this reads (the table may be pending).
    row
        The table; its namespace must exist.
    entry
        The manifest entry of the asset that produces it; untrusted.
    """
    model = entry_model(entry)
    row.model_kind = model.kind if model else None
    row.model_grain = model.grain if model else None
    row.model_key = (KEY_NONE if model.key_none else list(model.key) or None) if model else None
    wanted = {r.column: r for r in (model.references if model else ())}
    state = inspect(row)
    existing = (
        {}
        if state.pending or state.transient
        else {
            r.source_column: r
            for r in session.scalars(
                select(DeclaredReference).where(DeclaredReference.source_table_id == row.id)
            )
        }
    )
    for column, found in existing.items():
        ref = wanted.get(column)
        if ref is None:
            session.delete(found)
        else:
            found.target_name, found.target_column = ref.target, ref.target_column
    added = [ref for column, ref in wanted.items() if column not in existing]
    if not added:
        return
    namespace = session.get(Namespace, row.namespace_id)
    assert namespace is not None  # nosec B101 -- a table row's namespace is never missing
    for ref in added:
        session.add(
            DeclaredReference(
                id=uuid.uuid4(),
                environment_id=namespace.environment_id,
                table=row,
                source_column=ref.column,
                target_name=ref.target,
                target_column=ref.target_column,
            )
        )


def find_project_table(session: Session, environment_id: uuid.UUID, dotted: str) -> Table | None:
    """Find the table of an environment that a manifest entry names.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    dotted
        The dotted name, from a stored manifest; untrusted.

    Returns
    -------
    Table or None
        The row, or `None` when there is none or the name is not a table name.
    """
    try:
        ref = TableRef.from_dotted("-", dotted)
    except ValidationError:
        return None
    return session.execute(
        select(Table)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(
            Namespace.environment_id == environment_id,
            Namespace.path == ref.namespace.joined(),
            Table.name == ref.name,
        )
    ).scalar_one_or_none()


def clear_removed_models(
    session: Session, environment_id: uuid.UUID, previous: object, current: object
) -> None:
    """Clear the model of every table a project no longer produces.

    Called by a deploy and a rollback, in their transaction, next to
    `sync_project_descriptions`. A table in `previous` but not in `current` that has a row in
    the environment loses its model columns and its reference rows; its descriptions stay.
    Nothing is committed.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    previous
        The manifest that was current before, or `None`; untrusted.
    current
        The manifest that becomes current, or `None`; untrusted.
    """
    for dotted in manifest_assets(previous).keys() - manifest_assets(current).keys():
        row = find_project_table(session, environment_id, dotted)
        if row is None:
            continue
        row.model_kind = row.model_grain = row.model_key = None
        for found in session.scalars(
            select(DeclaredReference).where(DeclaredReference.source_table_id == row.id)
        ):
            session.delete(found)


def read_table_model(session: Session, row: Table) -> TableModel | None:
    """Read the model a table's row records.

    The result is not filtered: a caller that shows it filters the references by the reader's
    read rights on their targets.

    Parameters
    ----------
    session
        Database session.
    row
        The table.

    Returns
    -------
    TableModel or None
        The model, or `None` when the row records none (no kind, grain, key or reference).
    """
    references = tuple(
        ResolvedReference(r.source_column, r.target_name, r.target_column)
        for r in session.scalars(
            select(DeclaredReference)
            .where(DeclaredReference.source_table_id == row.id)
            .order_by(DeclaredReference.source_column)
        )
    )
    stored_key = row.model_key
    if row.model_kind is None and row.model_grain is None and stored_key is None and not references:
        return None
    key = tuple(stored_key) if isinstance(stored_key, list) else ()
    return TableModel(row.model_kind, row.model_grain, key, stored_key == KEY_NONE, references)


def incoming_references(
    session: Session, environment_id: uuid.UUID, dotted: str
) -> list[tuple[uuid.UUID, str, str]]:
    """List the references that point at a table, by its dotted name.

    The result is not filtered: a caller that shows it keeps only the source tables the reader
    may read.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment of the target.
    dotted
        The target's dotted name.

    Returns
    -------
    list of tuple of (uuid.UUID, str, str)
        ``(source table id, source column, target column)``, ordered by source table id and
        column.
    """
    rows = session.execute(
        select(
            DeclaredReference.source_table_id,
            DeclaredReference.source_column,
            DeclaredReference.target_column,
        )
        .where(
            DeclaredReference.environment_id == environment_id,
            DeclaredReference.target_name == dotted,
        )
        .order_by(DeclaredReference.source_table_id, DeclaredReference.source_column)
    )
    return [(table_id, column, target_column) for table_id, column, target_column in rows]


@dataclass(frozen=True)
class ModelReferenceView:
    """One declared reference as a reader is shown it.

    Parameters
    ----------
    id
        ``<ns.table>/ref_<column>``, with the source table's current name: the name of the
        reference's generated check.
    table
        The dotted name of the table that declares it.
    column
        The referencing column.
    target
        The dotted name of the table it points at, as the header names it.
    target_column
        The column of the target it must match.
    target_exists
        Whether a table of that name exists now. Only a reader who may read the target is shown
        the reference at all, so this never says anything about a table the reader may not read.
    """

    id: str
    table: str
    column: str
    target: str
    target_column: str
    target_exists: bool


@dataclass(frozen=True)
class TableModelView:
    """The model a table's header declares, filtered for one reader.

    Parameters
    ----------
    kind
        The declared kind, or `None`.
    grain
        The declared grain, or `None`.
    key
        The declared key columns; empty for no key and for `key: none`.
    key_none
        Whether the header declares `key: none`.
    references
        The references whose target the reader may read, by id.
    hidden_references
        How many references point at a table the reader may not read. A missing target counts
        here too for such a reader, so a hidden reference never says whether its target exists.
    checks
        The newest result of each check the current model generates (`key_unique` and
        `key_not_null` for a key, `ref_<column>` for each shown reference), by name. A stored
        result of a generated check the header no longer declares is left out.
    """

    kind: str | None
    grain: str | None
    key: tuple[str, ...]
    key_none: bool
    references: tuple[ModelReferenceView, ...]
    hidden_references: int
    checks: tuple[CheckHealth, ...]


@dataclass(frozen=True)
class TableReferences:
    """What a table's header says it references, and which tables reference it.

    Parameters
    ----------
    table
        The table's dotted name.
    outgoing
        The references it declares whose target the reader may read, by id.
    hidden_outgoing
        How many of its references point at a table the reader may not read.
    incoming
        The references other tables declare to it (by its current name) from a table the reader
        may read, by id.
    hidden_incoming
        How many references to it come from a table the reader may not read; never named.
    """

    table: str
    outgoing: tuple[ModelReferenceView, ...]
    hidden_outgoing: int
    incoming: tuple[ModelReferenceView, ...]
    hidden_incoming: int


ReadCheck = Callable[[TableRef], bool]

WithheldCheck = Callable[[uuid.UUID, str, bool], bool]
"""Whether a check result (its run, its full name, generated or not) is withheld from a reader.

`lakana_core.runs.hidden_checks.ReferenceCheckFilter.hides` is the one implementation: it asks
about the target the result's run declared, not the target the header declares now.
"""
"""Whether the reader may read a table, asked by name and without looking it up."""


def reference_id(table: str, column: str) -> str:
    """Build a reference's canonical id, which is also the name of its generated check.

    Parameters
    ----------
    table
        The dotted name of the table that declares it.
    column
        The referencing column.

    Returns
    -------
    str
        ``<ns.table>/ref_<column>``.
    """
    return f"{table}/{REF_PREFIX}{column}"


def _outgoing(
    session: Session, table: TableRef, model: TableModel, may_read: ReadCheck
) -> tuple[tuple[ModelReferenceView, ...], int]:
    """Keep the references of a model whose target the reader may read.

    The read right is asked before anything is looked up, so a missing target and an
    unreadable one are both counted as hidden for a reader without the right.

    Parameters
    ----------
    session
        Database session.
    table
        The table that declares them.
    model
        Its model.
    may_read
        The reader's read check.

    Returns
    -------
    tuple
        The shown references, by id, and how many were withheld.
    """
    shown: list[ModelReferenceView] = []
    hidden = 0
    for ref in model.references:
        try:
            target = TableRef.from_dotted(table.environment, ref.target)
        except ValidationError:
            target = None
        if target is None or not may_read(target):
            hidden += 1
            continue
        exists = (
            resolve_table_id(session, target.environment, target.namespace.joined(), target.name)
            is not None
        )
        shown.append(
            ModelReferenceView(
                id=reference_id(table.dotted(), ref.column),
                table=table.dotted(),
                column=ref.column,
                target=ref.target,
                target_column=ref.target_column,
                target_exists=exists,
            )
        )
    return tuple(sorted(shown, key=lambda r: r.id)), hidden


def model_view(
    session: Session, row: Table, table: TableRef, may_read: ReadCheck, withheld: WithheldCheck
) -> TableModelView | None:
    """Show a table's model to a reader who may read the table.

    A generated check's newest result is shown when the current header still generates the
    check, its reference is shown, and the reader may read the target the result's own run
    declared: a target changed since that run leaves the result withheld, as `table health`
    withholds it.

    Parameters
    ----------
    session
        Database session.
    row
        The table.
    table
        How it is addressed now.
    may_read
        The reader's read check, asked of each reference's target.
    withheld
        Whether a result is withheld from the reader, by the target of its run.

    Returns
    -------
    TableModelView or None
        The model, or `None` when the row records none.
    """
    model = read_table_model(session, row)
    if model is None:
        return None
    references, hidden = _outgoing(session, table, model, may_read)
    generated = {KEY_UNIQUE, KEY_NOT_NULL} if model.key else set()
    generated |= {f"{REF_PREFIX}{r.column}" for r in references}
    return TableModelView(
        kind=model.kind,
        grain=model.grain,
        key=model.key,
        key_none=model.key_none,
        references=references,
        hidden_references=hidden,
        checks=tuple(
            c
            for c in latest_checks(session, row.id)
            if c.generated and c.check in generated and not withheld(c.run_id, c.name, True)
        ),
    )


def references_view(
    session: Session, row: Table, table: TableRef, may_read: ReadCheck
) -> TableReferences:
    """Show a table's references in both directions to a reader who may read the table.

    Incoming references are found by the table's current name, as the headers that declare
    them name it: a table renamed through the API leaves them with its old name, and a later
    table of that name gets them, as the reference's generated check does.

    Parameters
    ----------
    session
        Database session.
    row
        The table.
    table
        How it is addressed now.
    may_read
        The reader's read check, asked of each target and each referencing table.

    Returns
    -------
    TableReferences
        The references the reader may see, and how many were withheld in each direction.
    """
    model = read_table_model(session, row)
    outgoing, hidden_outgoing = _outgoing(session, table, model, may_read) if model else ((), 0)
    namespace = session.get(Namespace, row.namespace_id)
    assert namespace is not None  # nosec B101 -- a table row's namespace is never missing
    found = incoming_references(session, namespace.environment_id, table.dotted())
    sources = {
        table_id: TableRef(table.environment, NamespacePath.parse(path), name)
        for table_id, path, name in session.execute(
            select(Table.id, Namespace.path, Table.name)
            .join(Namespace, Namespace.id == Table.namespace_id)
            .where(Table.id.in_({source_id for source_id, _, _ in found}))
        )
    }
    incoming: list[ModelReferenceView] = []
    hidden_incoming = 0
    for source_id, column, target_column in found:
        # A source dropped between the two reads has no name any more: count it, as hidden.
        source = sources.get(source_id)
        if source is None or not may_read(source):
            hidden_incoming += 1
            continue
        incoming.append(
            ModelReferenceView(
                id=reference_id(source.dotted(), column),
                table=source.dotted(),
                column=column,
                target=table.dotted(),
                target_column=target_column,
                target_exists=True,
            )
        )
    return TableReferences(
        table=table.dotted(),
        outgoing=outgoing,
        hidden_outgoing=hidden_outgoing,
        incoming=tuple(sorted(incoming, key=lambda r: r.id)),
        hidden_incoming=hidden_incoming,
    )
