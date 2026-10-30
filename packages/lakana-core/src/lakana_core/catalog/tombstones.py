"""Tombstones of dropped tables: what a drop keeps, and what an undrop restores.

A drop of a managed table keeps its files for an undo period (``LAKANA_TABLES__UNDO_DAYS``, 7
days by default). In the transaction that deletes the `tables` row, the table's location record
(`lakana_core.models.TableLocation`) becomes `DROPPED` and keeps:

- ``dropped_at`` and ``purge_after`` (the drop's time plus the undo period; the drop's time
  itself for a drop with ``purge``);
- ``revoked_policy_ids``: exactly the grants, row filters and column masks the drop revoked;
- ``dropped_table``: a snapshot of the row, of its column descriptions and of its indexed
  columns (`snapshot_table`), every mapped column of each, so an undrop rebuilds the row as
  it was.

The location sweep deletes the files once ``purge_after`` has passed. It sets
``purge_started_at`` under the record's lock before it deletes the first file, so an undrop,
which takes the same lock, either comes first (the record is `LIVE` again and the sweep leaves
it) or sees the mark and is refused.

External tables, views and artifacts have no location record and leave no tombstone: an
external table's files were never Lakana's to delete. A managed table whose location has no
record of its own (one shared by two tables created before locations carried the table's id)
is only uncatalogued, as before; its files stay.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session, class_mapper

from lakana_core.audit.service import AuditService
from lakana_core.auth import PrincipalRef, Securable
from lakana_core.catalog.locations import lock_location
from lakana_core.grants.revocation import POLICY_MODELS, RevokedPolicy, revoke_attached
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    AuditEventType,
    ColumnDescription,
    ColumnMask,
    DeclaredMeasure,
    DeclaredReference,
    Grant,
    Namespace,
    Principal,
    RowFilter,
    Table,
    TableColumn,
    TableLocation,
    TableLocationState,
    TableType,
)
from lakana_core.models.table import table_kind
from lakana_core.projects.measures import MEASURED_KINDS
from lakana_core.storage.resolver import table_root_from_metadata_location

_M = TypeVar("_M")

DEFAULT_UNDO_DAYS = 7
"""The default of ``LAKANA_TABLES__UNDO_DAYS``."""

NO_TOMBSTONE_MESSAGE = (
    "No dropped table '{name}' in {namespace} can be restored: none was dropped there, or its "
    "files are already deleted. `lakana table list {namespace} --dropped` shows the dropped "
    "tables of a namespace and until when each can be restored."
)
"""The `NotFoundError` of an undrop that finds no tombstone (`{name}`, `{namespace}`)."""

NAME_TAKEN_MESSAGE = (
    "{table} cannot be restored as '{name}': {holder} of that name exists in {namespace} now. "
    "Restore it under another name with `lakana table undrop {table} --as NEW_NAME` (REST: "
    "`new_name`), or drop {holder} first."
)
"""The `AlreadyExistsError` of an undrop whose name is taken.

`{table}`, `{name}`, `{namespace}`, and `{holder}` (`name_holder`).
"""

_HOLDERS = {
    TableType.ICEBERG: "a table",
    TableType.EXTERNAL: "an external table",
    TableType.VIEW: "a view",
    TableType.ARTIFACT: "an artifact",
}


def name_holder(table_type: TableType) -> str:
    """Name what holds a name, for `NAME_TAKEN_MESSAGE`.

    Parameters
    ----------
    table_type
        The row that holds it.

    Returns
    -------
    str
        ``a table``, ``an external table``, ``a view`` or ``an artifact``.
    """
    return _HOLDERS[table_type]


NAMESPACE_GONE_MESSAGE = (
    "{table} cannot be restored: its namespace {namespace} was dropped after it. Create the "
    "namespace again (`lakana ns create {namespace}`), then restore the table."
)
"""The `NotFoundError` of an undrop whose namespace is gone (`{table}`, `{namespace}`)."""

PERIOD_PASSED_MESSAGE = (
    "{table} cannot be restored: its undo period ended at {purge_after} (UTC), and the worker's "
    "sweep deletes its files. A dropped table can be restored for LAKANA_TABLES__UNDO_DAYS days "
    "(7 by default); a drop with --purge cannot be restored."
)
"""The `ConflictError` of an undrop after ``purge_after`` (`{table}`, `{purge_after}`)."""

FILES_GONE_MESSAGE = (
    "{table} cannot be restored: its files are gone (its current metadata file no longer "
    "exists in the environment's storage), so nothing was restored. Restore the table from a "
    "backup of the storage if you have one."
)
"""The `ConflictError` of an undrop whose metadata file is missing (`{table}`)."""

PURGE_STARTED_MESSAGE = (
    "{table} cannot be restored: the worker's sweep has started deleting its files, so the "
    "table can no longer be brought back whole."
)
"""The `ConflictError` of an undrop after the sweep began the purge (`{table}`)."""


@dataclass(frozen=True)
class DroppedTable:
    """A dropped table that still has a tombstone.

    Parameters
    ----------
    name
        The table's name when it was dropped.
    table_id
        Its id, which an undrop keeps.
    kind
        The API's kind (``TABLE``).
    dropped_at
        When it was dropped.
    purge_after
        From when on the sweep deletes its files.
    restorable
        Whether an undrop would still be accepted: the undo period has not ended and the purge
        has not started.
    """

    name: str
    table_id: uuid.UUID
    kind: str
    dropped_at: datetime
    purge_after: datetime
    restorable: bool


def _encode(value: object) -> object:
    """Write one column value as JSON.

    Parameters
    ----------
    value
        The value as the ORM holds it.

    Returns
    -------
    object
        A string for an id, a time or a day (ISO 8601) and an enum; the value itself otherwise.
    """
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    return value


def _decode(python_type: type, value: object) -> object:
    """Read one column value back from JSON.

    Parameters
    ----------
    python_type
        The column's Python type.
    value
        The stored value.

    Returns
    -------
    object
        The value as the ORM holds it.
    """
    if value is None:
        return None
    if python_type is uuid.UUID:
        return uuid.UUID(str(value))
    if python_type is datetime:
        return datetime.fromisoformat(str(value))
    if python_type is date:
        return date.fromisoformat(str(value))
    if issubclass(python_type, enum.Enum):
        return python_type(value)
    return value


def _python_types(model: type) -> dict[str, type]:
    """Map each mapped column of a model to its Python type.

    Parameters
    ----------
    model
        An ORM model.

    Returns
    -------
    dict of str to type
        By attribute name; `object` where the column type names none.
    """
    types: dict[str, type] = {}
    for attribute in class_mapper(model).column_attrs:
        column_type = attribute.columns[0].type
        found = column_type.python_type
        impl = getattr(column_type, "impl", None)
        if found is object and impl is not None:
            found = impl.python_type  # a type decorator names its type through its `impl`
        types[attribute.key] = found
    return types


def _row_json(row: object, *, leave_out: frozenset[str] = frozenset()) -> dict[str, object]:
    """Write every mapped column of a row as JSON.

    Parameters
    ----------
    row
        An ORM row.
    leave_out
        Attributes not to write.

    Returns
    -------
    dict of str to object
        By attribute name.
    """
    return {
        key: _encode(getattr(row, key)) for key in _python_types(type(row)) if key not in leave_out
    }


def _row_from_json(model: type[_M], data: Mapping[str, object], **overrides: object) -> _M:
    """Build a row from what `_row_json` wrote.

    Parameters
    ----------
    model
        The ORM model.
    data
        The stored columns; a column the model no longer has is ignored, one it gained since
        takes its default.
    **overrides
        Values that replace stored ones.

    Returns
    -------
    _M
        A new, transient row.
    """
    values = {
        key: _decode(python_type, data[key])
        for key, python_type in _python_types(model).items()
        if key in data
    }
    values.update(overrides)
    return model(**values)


def snapshot_table(session: Session, row: Table, namespace: NamespacePath) -> dict[str, object]:
    """Record what an undrop needs to rebuild a table that is about to be dropped.

    Parameters
    ----------
    session
        Database session, in the drop's transaction.
    row
        The table's row.
    namespace
        Its namespace.

    Returns
    -------
    dict of str to object
        ``namespace`` (the path's parts), ``row`` (every column of the row),
        ``column_descriptions`` and ``columns`` (their rows without the table id) and
        ``references`` and ``measures`` (the model's reference and measure rows without the table
        and environment ids); the rows a table's id cascades to and that only describe it.
    """
    descriptions = session.scalars(
        select(ColumnDescription).where(ColumnDescription.table_id == row.id)
    ).all()
    columns = session.scalars(select(TableColumn).where(TableColumn.table_id == row.id)).all()
    references = session.scalars(
        select(DeclaredReference).where(DeclaredReference.source_table_id == row.id)
    ).all()
    measures = (
        session.scalars(select(DeclaredMeasure).where(DeclaredMeasure.table_id == row.id)).all()
        if row.model_kind in MEASURED_KINDS
        else ()
    )
    leave_out = frozenset({"table_id"})
    return {
        "namespace": list(namespace.parts),
        "row": _row_json(row),
        "column_descriptions": [_row_json(d, leave_out=leave_out) for d in descriptions],
        "columns": [_row_json(c, leave_out=leave_out) for c in columns],
        "references": [
            _row_json(r, leave_out=frozenset({"source_table_id", "environment_id"}))
            for r in references
        ],
        "measures": [
            _row_json(m, leave_out=frozenset({"table_id", "environment_id"})) for m in measures
        ],
    }


def tombstone(
    session: Session,
    row: Table,
    *,
    snapshot: dict[str, object],
    revoked: Iterable[RevokedPolicy],
    now: datetime,
    purge_after: datetime,
) -> TableLocation | None:
    """Make a dropped table's location record `DROPPED`, inside the drop's transaction.

    Parameters
    ----------
    session
        Database session; the drop's transaction, which also deletes the row.
    row
        The table's row.
    snapshot
        `snapshot_table` of it.
    revoked
        The policies the drop revoked.
    now
        The drop's time, which also stamped the revocations.
    purge_after
        From when on the sweep may delete the files.

    Returns
    -------
    TableLocation or None
        The tombstone; `None` for a table that has no location record of its own (an external
        table, a view, an artifact, or a managed table sharing an old location), whose files
        the drop leaves alone. A `DROPPED` record of another table at the same location is
        given the later of the two `purge_after`, never an earlier one.
    """
    if row.table_type is not TableType.ICEBERG or row.metadata_location is None:
        return None
    location = table_root_from_metadata_location(row.metadata_location)
    record_id = session.scalar(select(TableLocation.id).where(TableLocation.location == location))
    record = None if record_id is None else lock_location(session, record_id)
    if record is None:
        return None
    if record.state is TableLocationState.LIVE and record.table_id == row.id:
        record.state = TableLocationState.DROPPED
        record.dropped_at = now
        record.purge_after = purge_after
        record.purge_started_at = None
        record.revoked_policy_ids = [policy.as_json() for policy in revoked]
        record.dropped_table = snapshot
        session.flush()
        return record
    if (
        record.state is TableLocationState.DROPPED
        and record.purge_started_at is None
        and record.purge_after is not None
        and record.purge_after < purge_after
    ):
        record.purge_after = purge_after
        session.flush()
    return None


def drop_row(
    session: Session,
    actor: PrincipalRef | None,
    ref: TableRef,
    row: Table,
    *,
    now: datetime,
    purge_after: datetime,
    reason: str,
    detail: Mapping[str, object] | None = None,
) -> TableLocation | None:
    """Drop a locked table row, inside the caller's transaction: the one drop path.

    Revokes the grants, row filters and column masks bound to the table (each stamped `now`),
    makes its location record a tombstone, records `TABLE_DROPPED`, and deletes the row. The
    caller has authorized the drop and taken the row's lock, and commits.

    Parameters
    ----------
    session
        Database session; the drop's transaction.
    actor
        Who drops it; `None` for the system (an expiry).
    ref
        The table.
    row
        Its row, read under its lock.
    now
        The drop's time.
    purge_after
        From when on the sweep may delete its files.
    reason
        Why: ``user``, ``purge``, ``retention`` or ``environment_deleted``.
    detail
        More facts for the audit event.

    Returns
    -------
    TableLocation or None
        The tombstone; `None` for a table that leaves none (`tombstone`).
    """
    revoked = revoke_attached(session, actor, table_id=row.id, now=now)
    record = tombstone(
        session,
        row,
        snapshot=snapshot_table(session, row, ref.namespace),
        revoked=revoked,
        now=now,
        purge_after=purge_after,
    )
    AuditService(session).record(
        None if actor is None else actor.id,
        AuditEventType.TABLE_DROPPED,
        securable=Securable.table_scope(ref.environment, ref.namespace, ref.name),
        detail={
            "reason": reason,
            "table_id": str(row.id),
            "kind": row.kind,
            "purge_after": None if record is None else purge_after.isoformat(),
            "revoked_policies": len(revoked),
            **(detail or {}),
        },
    )
    session.delete(row)
    return record


def _snapshot_of(record: TableLocation) -> dict[str, Any] | None:
    """Return a record's snapshot when it is a tombstone that has one.

    Parameters
    ----------
    record
        A location record.

    Returns
    -------
    dict or None
        The snapshot; `None` for anything but a `DROPPED` record with a snapshot.
    """
    if record.state is not TableLocationState.DROPPED or not record.dropped_table:
        return None
    return record.dropped_table


def _tombstones(
    session: Session, environment_id: uuid.UUID, namespace: NamespacePath
) -> list[tuple[TableLocation, dict[str, Any]]]:
    """List the tombstones of one namespace, newest drop first.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    namespace
        The namespace the tables were dropped from.

    Returns
    -------
    list of (TableLocation, dict)
        Each record with its snapshot.
    """
    records = session.scalars(
        select(TableLocation)
        .where(
            TableLocation.environment_id == environment_id,
            TableLocation.state == TableLocationState.DROPPED,
        )
        .order_by(TableLocation.dropped_at.desc(), TableLocation.id)
    ).all()
    found = []
    for record in records:
        snapshot = _snapshot_of(record)
        if snapshot is not None and snapshot["namespace"] == list(namespace.parts):
            found.append((record, snapshot))
    return found


def list_tombstones(
    session: Session, environment_id: uuid.UUID, namespace: NamespacePath, *, now: datetime
) -> list[DroppedTable]:
    """Describe the dropped tables of a namespace that still have a tombstone.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    namespace
        The namespace.
    now
        The database's time, for `DroppedTable.restorable`.

    Returns
    -------
    list of DroppedTable
        Newest drop first.
    """
    listed = []
    for record, snapshot in _tombstones(session, environment_id, namespace):
        row = snapshot["row"]
        assert record.dropped_at is not None and record.purge_after is not None  # nosec B101
        listed.append(
            DroppedTable(
                name=row["name"],
                table_id=uuid.UUID(row["id"]),
                kind=table_kind(TableType(row["table_type"])),
                dropped_at=record.dropped_at,
                purge_after=record.purge_after,
                restorable=record.purge_started_at is None and now < record.purge_after,
            )
        )
    return listed


def find_tombstone(
    session: Session, environment_id: uuid.UUID, namespace: NamespacePath, name: str
) -> uuid.UUID | None:
    """Find the newest tombstone of a table name.

    Parameters
    ----------
    session
        Database session.
    environment_id
        The environment.
    namespace
        The namespace the table was dropped from.
    name
        Its name when it was dropped.

    Returns
    -------
    uuid.UUID or None
        The record's id; `None` when no tombstone has that name.
    """
    for record, snapshot in _tombstones(session, environment_id, namespace):
        if snapshot["row"]["name"] == name:
            return record.id
    return None


def _known_principals(session: Session, ids: Iterable[object]) -> set[str]:
    """Return which of some principal ids still exist.

    Parameters
    ----------
    session
        Database session.
    ids
        Stored ids (strings), `None` among them.

    Returns
    -------
    set of str
        The ids that name a principal.
    """
    wanted = {uuid.UUID(str(value)) for value in ids if value is not None}
    if not wanted:
        return set()
    return {
        str(found)
        for found in session.scalars(select(Principal.id).where(Principal.id.in_(wanted)))
    }


def restore_table(
    session: Session, snapshot: Mapping[str, Any], *, namespace_id: uuid.UUID, name: str
) -> Table:
    """Insert a dropped table's row, descriptions, columns, references and measures, uncommitted.

    A principal deleted since the drop (the owner, the author of a description, who set its
    retention) is left out, as the row's foreign key would have done had the row stayed.

    Parameters
    ----------
    session
        Database session; the undrop's transaction.
    snapshot
        `snapshot_table` of the table.
    namespace_id
        The namespace it comes back into.
    name
        The name it comes back under.

    Returns
    -------
    Table
        The row, added to the session.
    """
    stored = snapshot["row"]
    descriptions = snapshot["column_descriptions"]
    known = _known_principals(
        session,
        [
            stored.get("owner_id"),
            stored.get("retention_set_by"),
            *(d.get("updated_by") for d in descriptions),
        ],
    )
    owner = stored.get("owner_id")
    setter = stored.get("retention_set_by")
    row = _row_from_json(
        Table,
        stored,
        namespace_id=namespace_id,
        name=name,
        owner_id=uuid.UUID(owner) if owner in known else None,
        retention_set_by=uuid.UUID(setter) if setter in known else None,
    )
    session.add(row)
    session.flush()
    for description in descriptions:
        author = description.get("updated_by")
        session.add(
            _row_from_json(
                ColumnDescription,
                description,
                table_id=row.id,
                updated_by=uuid.UUID(author) if author in known else None,
            )
        )
    for column in snapshot["columns"]:
        session.add(_row_from_json(TableColumn, column, table_id=row.id))
    namespace = session.get(Namespace, namespace_id)
    assert namespace is not None  # nosec B101 -- the row above was inserted into it
    # A tombstone written before references were kept has none.
    for reference in snapshot.get("references", ()):
        session.add(
            _row_from_json(
                DeclaredReference,
                reference,
                source_table_id=row.id,
                environment_id=namespace.environment_id,
            )
        )
    # A tombstone written before measures were kept has none.
    for measure in snapshot.get("measures", ()):
        session.add(
            _row_from_json(
                DeclaredMeasure,
                measure,
                table_id=row.id,
                environment_id=namespace.environment_id,
            )
        )
    session.flush()
    return row


def restore_policies(
    session: Session,
    record: TableLocation,
    *,
    table_id: uuid.UUID,
    namespace: str,
    name: str,
) -> list[dict[str, str]]:
    """Un-revoke exactly the grants, row filters and column masks a drop revoked, uncommitted.

    A row comes back only if it is still as the drop left it: revoked at the drop's time.
    A row revoked before the drop was never listed; one deleted since (its subject was
    deleted) or stamped again since stays as it is.

    Parameters
    ----------
    session
        Database session; the undrop's transaction.
    record
        The tombstone, read under its lock.
    table_id
        The restored table's id.
    namespace
        The restored table's namespace, unit-separator-joined, for the display-only columns.
    name
        The restored table's name, likewise.

    Returns
    -------
    list of dict
        ``{"kind", "id"}`` of each row restored.
    """
    assert record.dropped_at is not None  # nosec B101 -- a tombstone always has its drop time

    def rebind(policy: Grant | RowFilter | ColumnMask) -> None:
        """Point a restored policy at the restored table.

        Parameters
        ----------
        policy
            The grant, row filter or column mask.
        """
        policy.table_id = table_id
        policy.namespace = namespace
        policy.table = name

    return unrevoke(session, record.revoked_policy_ids, revoked_at=record.dropped_at, rebind=rebind)


def unrevoke(
    session: Session,
    entries: Iterable[Mapping[str, str]],
    *,
    revoked_at: datetime,
    rebind: Callable[[Grant | RowFilter | ColumnMask], None] | None = None,
) -> list[dict[str, str]]:
    """Un-revoke listed grants, row filters and column masks still as one revocation left them.

    Uncommitted. A row comes back only if it is still revoked at exactly `revoked_at`; one
    deleted since, or revoked again since, stays as it is, and an entry of an unknown kind is
    ignored.

    Parameters
    ----------
    session
        Database session; the restore's transaction.
    entries
        ``{"kind", "id"}`` objects (`RevokedPolicy.as_json`).
    revoked_at
        The time the revocation stamped.
    rebind
        Called with each restored row before it is flushed, to point it somewhere new.

    Returns
    -------
    list of dict
        ``{"kind", "id"}`` of each row restored.
    """
    restored = []
    for entry in entries:
        model = POLICY_MODELS.get(entry.get("kind", ""))
        if model is None:
            continue
        policy = session.get(model, uuid.UUID(entry["id"]), populate_existing=True)
        if policy is None or policy.revoked_at is None or policy.revoked_at != revoked_at:
            continue
        policy.revoked_at = None
        policy.revoked_by = None
        if rebind is not None:
            rebind(policy)
        restored.append({"kind": entry["kind"], "id": entry["id"]})
    session.flush()
    return restored


def refresh_restored(row: Table, actor: uuid.UUID, now: datetime) -> bool:
    """Mark a restored table as changed now, and clear a fixed expiry day that has passed.

    Without the second, a table whose ``--on`` day passed while it was dropped would expire
    again at the next sweep.

    Parameters
    ----------
    row
        The restored row.
    actor
        Who restores it, recorded as who set its retention when it is cleared.
    now
        The restore's time.

    Returns
    -------
    bool
        Whether the expiry day was cleared.
    """
    cleared = row.retention_on is not None and row.retention_on <= now.date()
    if cleared:
        row.retention_on = None
        row.retention_set_by = actor
        row.retention_set_at = now
    row.updated_at = datetime.now(UTC)
    return cleared


def revive(record: TableLocation) -> None:
    """Make a restored table's tombstone its `LIVE` record again, uncommitted.

    Parameters
    ----------
    record
        The tombstone, read under its lock.
    """
    record.state = TableLocationState.LIVE
    record.dropped_at = None
    record.purge_after = None
    record.purge_started_at = None
    record.revoked_policy_ids = []
    record.dropped_table = None
