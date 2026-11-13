"""The one order in which several table rows are locked: by id.

A leaf module, so read recording (`lakana_core.catalog.health`) and the deletion of an
environment (`lakana_core.catalog.environment_deletion`) take the same lock without importing
each other's modules.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from lakana_core.models import Table


def lock_tables_in_id_order(session: Session, *criteria: ColumnElement[bool]) -> list[uuid.UUID]:
    """Lock every table row that matches, in id order, before anything else touches them.

    ``SELECT … ORDER BY id FOR UPDATE``: Postgres takes the locks in the order it returns the
    rows. Read recording (`lakana_core.catalog.health.record_table_reads`) locks the tables a
    query read with it, and a delete of several tables locks the tables it deletes with it, so
    the two wait for each other instead of deadlocking (#558). A delete takes it after the
    environment's row lock and before any location record's. SQLite has no row locks; its write
    lock orders the two there.

    Parameters
    ----------
    session
        Database session; the locks last until the caller commits or rolls back.
    *criteria
        What selects the tables, as for `Select.where`.

    Returns
    -------
    list of uuid.UUID
        The ids of the locked tables, in the order they were locked.
    """
    return list(
        session.scalars(select(Table.id).where(*criteria).order_by(Table.id).with_for_update())
    )
