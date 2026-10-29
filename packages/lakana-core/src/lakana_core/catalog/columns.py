"""The stored columns of tables: what a search over column names reads.

A table's own metadata stays the authority for its schema. `table_columns` is an index of it,
written in the transaction of every schema change (`sync_table_columns`) and filled for the tables
that existed before it by `backfill_table_columns`, which the worker runs from its housekeeping.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from lakana_core.catalog.identity import resolve_table_id
from lakana_core.catalog.json_schema import ColumnSpec, from_iceberg_schema
from lakana_core.catalog.metadata import read_metadata
from lakana_core.errors import ValidationError
from lakana_core.identifiers import TableRef, shown
from lakana_core.models import Environment, Namespace, Table, TableColumn, TableType
from lakana_core.storage.resolver import StorageResolver

logger = logging.getLogger(__name__)

BACKFILL_BATCH = 50
"""How many tables one pass of `backfill_table_columns` fills."""


def sync_table_columns(
    session: Session, table_id: uuid.UUID, columns: Sequence[ColumnSpec]
) -> None:
    """Make the stored columns of a table equal its schema.

    Nothing is committed: the caller's commit carries the rows with the schema change they
    describe. A column that is still there keeps its row, so a sync that changes nothing writes
    nothing. The table's own row may still be pending in the session.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.
    columns
        Its columns in schema order.
    """
    with session.no_autoflush:
        existing = {
            row.name: row
            for row in session.scalars(select(TableColumn).where(TableColumn.table_id == table_id))
        }
    wanted = {column.name for column in columns}
    for name, row in existing.items():
        if name not in wanted:
            session.delete(row)
    for position, column in enumerate(columns):
        row = existing.get(column.name)
        if row is None:
            session.add(
                TableColumn(
                    id=uuid.uuid4(),
                    table_id=table_id,
                    name=column.name,
                    type=column.type,
                    position=position,
                )
            )
        else:
            row.type = column.type
            row.position = position


def stored_columns(session: Session, environment: str, dotted: str) -> dict[str, str] | None:
    """Read the stored columns of a table, without asking whether anyone may read them.

    The caller decides who may see the answer: a deploy calls it only for a table the deployer
    may read.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment's name.
    dotted
        The table's dotted name.

    Returns
    -------
    dict or None
        Column name to its type in the catalog's spelling, in schema order; empty for a table
        whose columns are not stored yet. `None` when the name is malformed or no such table
        exists.
    """
    try:
        ref = TableRef.from_dotted(environment, dotted)
    except ValidationError:
        return None
    table_id = resolve_table_id(session, environment, ref.namespace.joined(), ref.name)
    if table_id is None:
        return None
    rows = session.execute(
        select(TableColumn.name, TableColumn.type)
        .where(TableColumn.table_id == table_id)
        .order_by(TableColumn.position)
    )
    return {name: type_ for name, type_ in rows}


def _columns_of(row: Table, storage: StorageResolver, environment: str) -> list[ColumnSpec]:
    """Read a table's columns from what the catalog stores about it.

    Parameters
    ----------
    row
        The table; not an artifact.
    storage
        Resolves the environment's storage, for a managed table's metadata file.
    environment
        The environment's name.

    Returns
    -------
    list of ColumnSpec
        The columns. A view and an external table keep theirs in the database; a managed table's
        schema is in its metadata file, which is the one read from storage.
    """
    if row.table_type is TableType.VIEW:
        return [ColumnSpec(**column) for column in (row.view_definition or {}).get("columns", [])]
    if row.table_type is TableType.EXTERNAL:
        schema = (row.external_config or {}).get("schema", {})
        return [ColumnSpec(name=n, type=t, nullable=True) for n, t in schema.items()]
    assert row.metadata_location is not None  # nosec B101 -- the query leaves out the others
    metadata = read_metadata(storage.for_environment(environment).file_io, row.metadata_location)
    return from_iceberg_schema(metadata.schema())


def backfill_table_columns(
    session: Session, storage: StorageResolver, *, limit: int = BACKFILL_BATCH
) -> int:
    """Store the columns of tables that have none stored yet.

    Takes `limit` tables per call, chosen at random so that a table whose metadata cannot be read
    (it is logged and skipped) cannot hold the same place in every pass. Commits.

    Parameters
    ----------
    session
        Database session; commits.
    storage
        Resolves each environment's storage.
    limit
        The most tables to fill in this call.

    Returns
    -------
    int
        How many tables were filled. `0` once every table that can be has columns stored.
    """
    candidates = session.execute(
        select(Table, Environment.name)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .join(Environment, Environment.id == Namespace.environment_id)
        .where(
            Table.table_type != TableType.ARTIFACT,
            or_(Table.table_type != TableType.ICEBERG, Table.metadata_location.is_not(None)),
            ~exists().where(TableColumn.table_id == Table.id),
        )
        .order_by(func.random())
        .limit(limit)
    ).all()
    filled = 0
    for row, environment in candidates:
        try:
            columns = _columns_of(row, storage, environment)
        except Exception as exc:
            logger.warning(
                "Could not read the columns of table %s in %s (%s); search will not find its "
                "columns until a schema change or a later pass stores them.",
                shown(row.name),
                shown(environment),
                type(exc).__name__,
            )
            continue
        sync_table_columns(session, row.id, columns)
        filled += 1
    session.commit()
    return filled
