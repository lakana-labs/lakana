"""A table's current top-level fields, read from its metadata file."""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from pyiceberg.io import FileIO
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.types import NestedField
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.catalog.metadata import read_metadata
from lakana_core.identifiers import shown
from lakana_core.models import Environment, Namespace, Table, TableType
from lakana_core.storage.resolver import StorageResolver

logger = logging.getLogger(__name__)


def current_fields(
    session: Session,
    storage: StorageResolver,
    table_id: uuid.UUID,
    *,
    timeout: float | None = None,
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
    timeout
        The most seconds the storage read may take; `None` waits for it. A read past it is
        given up (its thread is left to end on its own, it holds no session), so slow storage
        costs a graph request at most this long per table (PR #465's review).

    Returns
    -------
    tuple of NestedField or None
        The fields; `None` for a table that is not an Iceberg table, one without metadata, or
        one whose metadata cannot be read, or not within `timeout` (logged with the error's
        class only).
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
        file_io = storage.for_environment(environment).file_io
        if timeout is None:
            metadata = read_metadata(file_io, row.metadata_location)
        else:
            metadata = _read_within(file_io, row.metadata_location, timeout)
        if metadata is None:
            logger.warning(
                "The columns of table %s in %s could not be read to resolve its policies' "
                "column names: storage did not answer within %.1f s.",
                shown(row.name),
                shown(environment),
                timeout,
            )
            return None
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


def _read_within(file_io: FileIO, location: str, timeout: float) -> TableMetadata | None:
    """Read a metadata file in a thread of its own, waiting at most `timeout` seconds.

    Parameters
    ----------
    file_io
        The environment's file IO.
    location
        The metadata file.
    timeout
        The most seconds to wait.

    Returns
    -------
    TableMetadata or None
        The metadata; `None` when the read did not end in time.

    Raises
    ------
    Exception
        Whatever the read raised, when it ended in time.
    """
    found: dict[str, Any] = {}

    def read() -> None:
        try:
            found["metadata"] = read_metadata(file_io, location)
        except Exception as exc:  # handed to the caller, which logs its class only
            found["error"] = exc

    worker = threading.Thread(target=read, name="lakana-mask-fields", daemon=True)
    worker.start()
    worker.join(max(timeout, 0.0))
    if worker.is_alive():
        return None
    if "error" in found:
        raise found["error"]
    return found["metadata"]
