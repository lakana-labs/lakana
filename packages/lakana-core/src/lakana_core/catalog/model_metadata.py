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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import DeclaredReference, Namespace, Table
from lakana_core.projects.asset_model import KEY_NONE, ResolvedReference
from lakana_core.projects.manifest import entry_model, manifest_assets


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
    deleted. An entry without a model clears what an earlier deploy wrote. Nothing is committed.

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
    namespace = session.get(Namespace, row.namespace_id)
    assert namespace is not None  # nosec B101 -- a table row's namespace is never missing
    wanted = {r.column: r for r in (model.references if model else ())}
    existing = {
        r.source_column: r
        for r in session.scalars(
            select(DeclaredReference).where(DeclaredReference.source_table_id == row.id)
        )
    }
    for column, found in existing.items():
        ref = wanted.get(column)
        if ref is None:
            session.delete(found)
        else:
            found.target_name, found.target_column = ref.target, ref.target_column
    for column, ref in wanted.items():
        if column not in existing:
            session.add(
                DeclaredReference(
                    id=uuid.uuid4(),
                    environment_id=namespace.environment_id,
                    table=row,
                    source_column=column,
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
