"""How much storage an environment uses, from figures the catalog already holds.

A request never lists or reads object storage: `environment_usage` is a handful of aggregate
statements over the catalog database, so no environment, however large its bucket, makes the
server walk it. The figures come from three places:

- **managed tables**: `Table.size_bytes`, the ``total-files-size`` of the table's current
  snapshot. A commit stores it with the table's rows and last write
  (`Table.row_count`, `Table.last_write_at`) and the metadata file it read
  (`Table.size_metadata_location`). The worker that holds the scheduler role does the same
  for the tables that have none (`measure_table_sizes`, at most `MEASURE_BATCH` tables and
  `MEASURE_MAX_SECONDS` per sweep), so a table committed before these columns existed, or
  changed since by a writer that stores none, is measured again by the next sweep;
- **dropped tables in their undo period**: the tombstone keeps every column of the table's row
  (`lakana_core.catalog.tombstones`), the measured size included;
- **artifacts and code archives**: the sizes recorded when they were stored
  (`ArtifactBlob.size_bytes`, the `code_bytes` of a project version).

What is not counted: the files of older snapshots that maintenance has not expired yet, the
metadata files, external tables (their files are not Lakana's) and files nothing names.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import BigInteger, case, cast, func, or_, select, update
from sqlalchemy.orm import Session

from lakana_core.catalog.health import stored_stats
from lakana_core.catalog.metadata import read_metadata
from lakana_core.db import rowcount
from lakana_core.identifiers import shown
from lakana_core.models import (
    ArtifactBlob,
    Environment,
    JobVersion,
    Namespace,
    Table,
    TableLocation,
    TableLocationState,
    TableType,
)
from lakana_core.storage.resolver import StorageResolver

logger = logging.getLogger(__name__)

MEASURE_BATCH = 200
"""The most tables one call of `measure_table_sizes` reads the metadata of."""

MEASURE_MAX_SECONDS = 60.0
"""How long one call of `measure_table_sizes` may read metadata before it stops."""


@dataclass(frozen=True)
class UsagePart:
    """The storage one kind of thing uses in an environment.

    Parameters
    ----------
    count
        How many there are.
    bytes
        The sum of the sizes that are known.
    unmeasured
        How many have no size yet, or one measured on an earlier version (their last known size is
        in `bytes`); the next sweeps measure them. Always 0 for artifacts.
    """

    count: int
    bytes: int
    unmeasured: int


@dataclass(frozen=True)
class EnvironmentUsage:
    """The storage an environment uses, as the catalog knows it.

    Parameters
    ----------
    environment
        The environment's name.
    tables
        Its managed tables.
    dropped_tables
        Its dropped managed tables whose files wait for the end of their undo period.
    artifacts
        Its artifacts: `bytes` is the size of the files stored for them.
    code_archives
        The code archives of its project versions.
    """

    environment: str
    tables: UsagePart
    dropped_tables: UsagePart
    artifacts: UsagePart
    code_archives: UsagePart

    @property
    def total_bytes(self) -> int:
        """The sum of the known sizes.

        Returns
        -------
        int
            Tables, dropped tables, artifacts and code archives together.
        """
        return sum(
            part.bytes
            for part in (self.tables, self.dropped_tables, self.artifacts, self.code_archives)
        )

    @property
    def complete(self) -> bool:
        """Whether every size is known and current.

        Returns
        -------
        bool
            `True` when no part has an unmeasured entry.
        """
        return not any(
            part.unmeasured
            for part in (self.tables, self.dropped_tables, self.artifacts, self.code_archives)
        )


def _whole(value: object) -> int:
    """Read an aggregate as a whole number.

    Parameters
    ----------
    value
        What the database returned: an `int`, a `Decimal` (Postgres sums a ``BIGINT`` column as
        ``NUMERIC``), or `None` over no rows.

    Returns
    -------
    int
        The number; 0 for `None`.
    """
    return int(value) if isinstance(value, int | Decimal) else 0


def _part(count: object, known: object, unmeasured: object) -> UsagePart:
    """Build a part from the three numbers of an aggregate row.

    Parameters
    ----------
    count
        The row count.
    known
        The sum of the known sizes; `None` when no row has one.
    unmeasured
        The count of rows without a current size; `None` over no rows.

    Returns
    -------
    UsagePart
        With `None` read as 0.
    """
    return UsagePart(count=_whole(count), bytes=_whole(known), unmeasured=_whole(unmeasured))


def environment_usage(session: Session, environment: Environment) -> EnvironmentUsage:
    """Add up the storage an environment uses, from the catalog database alone.

    Four aggregate statements; nothing is read from object storage, whatever the number of
    tables or files.

    Parameters
    ----------
    session
        Database session.
    environment
        The environment.

    Returns
    -------
    EnvironmentUsage
        Its figures.
    """
    in_environment = Namespace.environment_id == environment.id
    stale = or_(
        Table.size_bytes.is_(None),
        Table.size_metadata_location.is_(None),
        Table.size_metadata_location != Table.metadata_location,
    )
    tables = session.execute(
        select(
            func.count(Table.id),
            func.sum(Table.size_bytes),
            func.sum(case((stale, 1), else_=0)),
        )
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(in_environment, Table.table_type == TableType.ICEBERG)
    ).one()
    # Text, then BIGINT: `as_integer` casts to INTEGER, which a size above 2 GiB overflows on
    # Postgres.
    dropped_size = cast(TableLocation.dropped_table["row", "size_bytes"].as_string(), BigInteger)
    dropped = session.execute(
        select(
            func.count(TableLocation.id),
            func.sum(dropped_size),
            func.sum(case((dropped_size.is_(None), 1), else_=0)),
        ).where(
            TableLocation.environment_id == environment.id,
            TableLocation.state == TableLocationState.DROPPED,
            TableLocation.purge_started_at.is_(None),
        )
    ).one()
    artifact_count = session.scalar(
        select(func.count(Table.id))
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(in_environment, Table.table_type == TableType.ARTIFACT)
    )
    artifact_bytes = session.scalar(
        select(func.sum(ArtifactBlob.size_bytes))
        .join(Table, Table.id == ArtifactBlob.artifact_id)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(in_environment)
    )
    archives = session.execute(
        select(
            func.count(JobVersion.id),
            func.sum(JobVersion.code_bytes),
            func.sum(case((JobVersion.code_bytes.is_(None), 1), else_=0)),
        ).where(JobVersion.environment_id == environment.id)
    ).one()
    return EnvironmentUsage(
        environment=environment.name,
        tables=_part(*tables),
        dropped_tables=_part(*dropped),
        artifacts=_part(artifact_count, artifact_bytes, 0),
        code_archives=_part(*archives),
    )


def _measure_one(
    session: Session, storage: StorageResolver, table_id: uuid.UUID, location: str, env: str
) -> bool:
    """Read one table's size, rows and last write from its metadata file and record them.

    Recorded only if the table is unchanged. A commit stores the same values
    (`lakana_core.catalog.health.stored_stats`), so this fills in the tables that were
    committed before the columns existed.

    Parameters
    ----------
    session
        Database session; commits.
    storage
        Resolves the environment's storage.
    table_id
        The table.
    location
        Its metadata file when it was chosen.
    env
        Its environment's name.

    Returns
    -------
    bool
        `True` when the size was recorded; `False` when the table changed or went meanwhile
        (the next sweep measures its new version).
    """
    metadata = read_metadata(storage.for_environment(env).file_io, location)
    stats = stored_stats(metadata)
    # `updated_at` is kept: retention counts a table's activity from it, and a measurement is
    # none. Written only while the table still names the file that was read: a commit that came
    # in between is neither overwritten nor described by an older figure. One short transaction
    # per table, so a commit to the table never waits for the rest of the batch.
    result = session.execute(
        update(Table)
        .where(Table.id == table_id, Table.metadata_location == location)
        .values(
            row_count=stats.row_count,
            size_bytes=stats.size_bytes,
            size_metadata_location=location,
            last_write_at=stats.last_write_at,
            updated_at=Table.updated_at,
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return rowcount(result) == 1


def measure_table_sizes(
    session: Session,
    storage: StorageResolver,
    *,
    limit: int = MEASURE_BATCH,
    max_seconds: float = MEASURE_MAX_SECONDS,
    should_stop: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Record the size of managed tables never measured, or changed since they were.

    Takes up to `limit` such tables, in random order so that one whose metadata cannot be read
    (logged and skipped) cannot hold the same place in every sweep, and stops at `max_seconds`
    or when `should_stop` says so. Reads one metadata file per table, never a listing. Tables of
    deleted environments are left alone.

    Parameters
    ----------
    session
        Database session; commits after each table.
    storage
        Resolves each environment's storage.
    limit
        The most tables to read in this call.
    max_seconds
        The time this call may take; checked before each table.
    should_stop
        Set at shutdown; checked before each table.
    clock
        Monotonic seconds; a test replaces it.

    Returns
    -------
    int
        How many tables got a size recorded.
    """
    deadline = clock() + max_seconds
    candidates = session.execute(
        select(Table.id, Table.name, Table.metadata_location, Environment.name)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .join(Environment, Environment.id == Namespace.environment_id)
        .where(
            Table.table_type == TableType.ICEBERG,
            Table.metadata_location.is_not(None),
            Environment.deleted_at.is_(None),
            or_(
                Table.size_metadata_location.is_(None),
                Table.size_metadata_location != Table.metadata_location,
            ),
        )
        .order_by(func.random())
        .limit(limit)
    ).all()
    session.commit()
    measured = 0
    for table_id, name, location, env in candidates:
        if should_stop() or clock() >= deadline:
            break
        assert location is not None  # nosec B101 -- the query leaves out tables without one
        try:
            measured += _measure_one(session, storage, table_id, location, env)
        except Exception as exc:
            session.rollback()
            logger.warning(
                "Could not read the size of table %s in %s (%s); `lakana env usage` counts it "
                "as unmeasured until a later sweep reads it.",
                shown(name),
                shown(env),
                type(exc).__name__,
            )
    return measured
