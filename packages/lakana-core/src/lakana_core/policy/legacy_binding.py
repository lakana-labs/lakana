"""Binding the row filters and column masks of earlier releases to Iceberg field ids.

A policy made before the field-id binding is stored `pending`. The worker's housekeeping looks
at each one (`bind_legacy_policies`) and binds it exactly where it can, or marks it `unbound`,
which keeps the rename rule of `policy.renames`. A raw table's, a view's and an artifact's
policies are bound by name: their columns have no field ids.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

import sqlglot
import sqlglot.expressions as exp
from pyiceberg.table.metadata import TableMetadata
from sqlalchemy import func, null, select, update
from sqlalchemy.orm import Session

from lakana_core.catalog.columns import BACKFILL_BATCH
from lakana_core.catalog.metadata import read_metadata
from lakana_core.db import rowcount
from lakana_core.errors import ValidationError
from lakana_core.identifiers import shown
from lakana_core.models import ColumnMask, Environment, Namespace, RowFilter, Table, TableType
from lakana_core.policy.field_binding import (
    BY_NAME,
    FIELD_IDS,
    PENDING,
    UNBOUND,
    PolicyBinding,
    bind_policy,
)
from lakana_core.policy.renames import PolicyColumns, renamed_policy_column
from lakana_core.sql_syntax import SQL_SYNTAX_ERRORS
from lakana_core.storage.resolver import StorageResolver

logger = logging.getLogger(__name__)


def _columns_read(expression: str) -> set[str] | None:
    """Name the columns an expression reads, unless it cannot be bound exactly.

    Parameters
    ----------
    expression
        The stored expression.

    Returns
    -------
    set of str or None
        The names as written; `None` when it does not parse or qualifies a column.
    """
    try:
        parsed = sqlglot.parse_one(expression, dialect="duckdb", into=exp.Condition)
    except SQL_SYNTAX_ERRORS:
        return None
    found = list(parsed.find_all(exp.Column))
    if any(column.table for column in found):
        return None
    return {column.name for column in found}


def legacy_field_ids(
    metadata: TableMetadata,
    expression: str,
    schema_id: int | None,
    *,
    masked_column: str | None = None,
) -> PolicyBinding | None:
    """Bind a policy of an earlier release exactly, or say it cannot be.

    A policy that recorded its schema is bound through that schema: exactly what it was written
    against. One that recorded none is bound through the current schema only when the rename
    rule (`renamed_policy_column`, against every schema the table has had) finds no rename of a
    column it uses. Anything doubtful leaves it unbound: a qualified column, an expression that
    does not parse, a name missing or ambiguous in the schema.

    Parameters
    ----------
    metadata
        The table's current metadata.
    expression
        The stored expression.
    schema_id
        The recorded schema id, or `None`.
    masked_column
        A mask's own column; `None` for a row filter.

    Returns
    -------
    PolicyBinding or None
        The binding; `None` for unbound.
    """
    columns = _columns_read(expression)
    if columns is None:
        return None
    used = frozenset({*columns, *([masked_column] if masked_column else [])})
    if schema_id is None:
        if renamed_policy_column(metadata, [PolicyColumns(used, None)]) is not None:
            return None
        schema = metadata.schema()
    else:
        schema = next((s for s in metadata.schemas if s.schema_id == schema_id), None)
        if schema is None:
            return None
    try:
        return bind_policy(schema.fields, columns, masked_column=masked_column)
    except ValidationError:
        return None


@dataclass(frozen=True)
class _Pending:
    """One pending row: what the pass needs to bind it.

    Parameters
    ----------
    model
        `RowFilter` or `ColumnMask`.
    id
        The row's id.
    expression
        The stored expression.
    schema_id
        The recorded schema, or `None`.
    column
        A mask's own column; `None` for a row filter.
    """

    model: type[RowFilter] | type[ColumnMask]
    id: uuid.UUID
    expression: str
    schema_id: int | None
    column: str | None


def _pending_rows(session: Session, limit: int) -> dict[uuid.UUID, list[_Pending]]:
    """Select up to `limit` pending, active rows of both kinds, grouped by table.

    Parameters
    ----------
    session
        Database session.
    limit
        The most rows to take.

    Returns
    -------
    dict
        Each table id to its pending rows.
    """
    rows: list[tuple[uuid.UUID, _Pending]] = []
    for model, masked in ((RowFilter, null()), (ColumnMask, ColumnMask.column)):
        chosen = session.execute(
            select(model.table_id, model.id, model.expression, model.schema_id, masked)
            .where(
                model.binding == PENDING,
                model.revoked_at.is_(None),
                model.table_id.is_not(None),
            )
            .order_by(func.random())
            .limit(limit)
        ).all()
        rows += [
            (table_id, _Pending(model, row_id, expression, schema_id, column))
            for table_id, row_id, expression, schema_id, column in chosen
            if table_id is not None
        ]
    # A table whose metadata cannot be read must not hold the same place in every pass.
    grouped: dict[uuid.UUID, list[_Pending]] = {}
    for table_id, pending in rows:
        grouped.setdefault(table_id, []).append(pending)
    return grouped


def _bindings(
    table: Table, environment: str, pending: Iterable[_Pending], storage: StorageResolver
) -> list[tuple[_Pending, PolicyBinding | None]]:
    """Decide the binding of every pending row of one table.

    Parameters
    ----------
    table
        The table.
    environment
        Its environment's name.
    pending
        The table's pending rows.
    storage
        Resolves the environment's storage.

    Returns
    -------
    list of tuple
        Each row with its binding; `None` for unbound.

    Raises
    ------
    Exception
        Whatever reading the table's metadata raises.
    """
    if table.table_type is not TableType.ICEBERG:
        # A raw table, a view and an artifact have no field ids; creation binds them by name too.
        return [(row, PolicyBinding(BY_NAME, None, None)) for row in pending]
    if table.metadata_location is None:
        return [
            (
                row,
                PolicyBinding(FIELD_IDS, {}, None)
                if row.column is None and _columns_read(row.expression) == set()
                else None,
            )
            for row in pending
        ]
    metadata = read_metadata(storage.for_environment(environment).file_io, table.metadata_location)
    return [
        (row, legacy_field_ids(metadata, row.expression, row.schema_id, masked_column=row.column))
        for row in pending
    ]


def bind_legacy_policies(
    session: Session, storage: StorageResolver, *, limit: int = BACKFILL_BATCH
) -> int:
    """Bind the pending row filters and column masks of earlier releases.

    Takes up to `limit` active rows of each kind per call, chosen at random so that a table whose
    metadata cannot be read (it is logged and skipped) cannot hold the same place in every pass.
    A row another process has bound meanwhile is left as it is. Commits.

    Parameters
    ----------
    session
        Database session; commits.
    storage
        Resolves each environment's storage.
    limit
        The most rows of each kind to look at in this call.

    Returns
    -------
    int
        How many rows were bound or marked unbound. `0` once none is pending that can be bound.
    """
    counts = Counter[str]()
    for table_id, pending in _pending_rows(session, limit).items():
        found = session.execute(
            select(Table, Environment.name)
            .join(Namespace, Namespace.id == Table.namespace_id)
            .join(Environment, Environment.id == Namespace.environment_id)
            .where(Table.id == table_id)
        ).one_or_none()
        if found is None:
            continue
        table, environment = found
        try:
            decided = _bindings(table, environment, pending, storage)
        except Exception as exc:
            logger.warning(
                "Could not read the metadata of table %s in %s (%s); its row filters and column "
                "masks stay as they are until a later pass.",
                shown(table.name),
                shown(environment),
                type(exc).__name__,
            )
            continue
        for row, binding in decided:
            values: dict[str, object] = (
                {"binding": UNBOUND}
                if binding is None
                else {"binding": binding.binding, "field_ids": binding.field_ids}
            )
            if binding is not None and row.column is not None:
                values["column_field_id"] = binding.column_field_id
            done = session.execute(
                update(row.model)
                .where(row.model.id == row.id, row.model.binding == PENDING)
                .values(**values),
                execution_options={"synchronize_session": False},
            )
            if rowcount(done):
                counts[binding.binding if binding else UNBOUND] += 1
    session.commit()
    if counts:
        logger.info(
            "Bound %d row filters and column masks to field ids and %d by name; %d left unbound.",
            counts[FIELD_IDS],
            counts[BY_NAME],
            counts[UNBOUND],
        )
    return counts.total()
