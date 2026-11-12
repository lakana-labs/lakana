"""The compare-and-swap that commits a table's metadata pointer.

Isolated from everything Iceberg so it can be tested on its own. This is the
operation that makes concurrent commits safe: the update matches on the
table id and the location the caller believes is current, and a rowcount
other than one means someone else committed first (or the table is gone).
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from lakana_core.catalog.health import StoredStats
from lakana_core.errors import ConflictError
from lakana_core.models import Table


def commit_metadata_pointer(
    session: Session,
    table_id: uuid_module.UUID,
    *,
    expected_location: str | None,
    new_location: str,
    publish: bool = False,
    stats: StoredStats | None = None,
) -> None:
    """Atomically move a table's metadata pointer.

    The update matches on `table_id` and `expected_location`, so it applies
    only if no other writer has committed since the caller read it. A
    rowcount other than one means either that the table is gone or that the
    pointer has moved; both are conflicts from the caller's point of view.

    The `NULL` case uses SQLAlchemy's `.is_(None)` rather than
    ``IS NOT DISTINCT FROM``, which exists only on Postgres — Constraint 6
    requires SQLite to work too.

    With `publish` the same `UPDATE` also clears the table's not-published marker
    (`Table.unpublished_run_id`): the commit that gives a table its current snapshot
    and the end of its being unpublished are one statement, so neither can land without the
    other. A swap that loses the race changes nothing, the marker included.

    With `stats` the same `UPDATE` also stores the numbers of the new snapshot that the table
    list sorts on (rows, size, last write) and the file they were read from, so the size
    sweep finds the table measured.

    This does not commit the transaction. The caller owns the unit of work, so
    that the pointer move and any accompanying row changes land together
    (Constraint 4).

    ``session.execute`` autoflushes first. A caller that has already assigned
    ``table.metadata_location = new_location`` on the ORM object before
    calling this function will have that pending write flushed ahead of the
    `UPDATE`, so the comparison then matches the caller's own write and the
    CAS always succeeds — defeating the guard entirely. Pass the expected
    location as `expected_location`; do not pre-assign it on the object.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table whose pointer is being moved.
    expected_location
        The location the caller believes is current, or `None` if the caller
        believes the table has no metadata yet.
    new_location
        The location to move to.
    publish
        Also set `unpublished_run_id` to `NULL`. Pass it only for a commit that leaves the
        table with a current snapshot.
    stats
        The stored numbers of the new metadata; `None` leaves the stored ones as they are.

    Raises
    ------
    ConflictError
        If no row matched — the table is gone, or another writer committed
        first.
    """
    condition = (
        Table.metadata_location.is_(None)
        if expected_location is None
        else Table.metadata_location == expected_location
    )
    values: dict[str, Any] = {
        "metadata_location": new_location,
        "previous_metadata_location": expected_location,
    }
    if publish:
        values["unpublished_run_id"] = None
    if stats is not None:
        values.update(
            row_count=stats.row_count,
            size_bytes=stats.size_bytes,
            size_metadata_location=new_location,
            last_write_at=stats.last_write_at,
        )
    # No .returning() on this statement: that would make the ORM return a
    # ChunkedIteratorResult instead, and this cast would then lie.
    result = cast(
        "CursorResult[Any]",
        session.execute(update(Table).where(Table.id == table_id, condition).values(**values)),
    )
    if result.rowcount != 1:
        raise ConflictError(
            f"Metadata pointer for table {table_id} was not at the expected location; "
            "another writer committed first."
        )
