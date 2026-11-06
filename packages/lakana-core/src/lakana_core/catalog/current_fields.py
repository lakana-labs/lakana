"""A table's current top-level fields, read from its metadata file."""

from __future__ import annotations

import logging
import uuid

from pyiceberg.types import NestedField
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.catalog.metadata import read_metadata
from lakana_core.identifiers import shown
from lakana_core.models import Environment, Namespace, Table, TableType
from lakana_core.storage.resolver import StorageResolver

logger = logging.getLogger(__name__)


def current_fields(
    session: Session, storage: StorageResolver, table_id: uuid.UUID
) -> tuple[NestedField, ...] | None:
    """Read a table's current top-level fields, without asking who may see them.

    The caller decides what the answer may reveal: a policy listing checks MANAGE_POLICY first;
    the metadata graph uses it only to name the column a mask is on, and shows that name to
    readers the graph's visibility allows.

    Parameters
    ----------
    session
        Database session.
    storage
        Resolves the environment's storage, for the metadata file.
    table_id
        The table's id.

    Returns
    -------
    tuple of NestedField or None
        The fields; `None` for a table that is not an Iceberg table, one without metadata, or
        one whose metadata cannot be read (logged with the error's class only).
    """
    found = session.execute(
        select(Table, Environment.name)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .join(Environment, Environment.id == Namespace.environment_id)
        .where(Table.id == table_id)
    ).one_or_none()
    if found is None or found[0].table_type is not TableType.ICEBERG:
        return None
    row, environment = found
    if row.metadata_location is None:
        return None
    try:
        metadata = read_metadata(
            storage.for_environment(environment).file_io, row.metadata_location
        )
        return tuple(metadata.schema().fields)
    except Exception as exc:
        logger.warning(
            "The columns of table %s in %s could not be read to resolve its policies' column "
            "names (%s).",
            shown(row.name),
            shown(environment),
            type(exc).__name__,
        )
        return None
