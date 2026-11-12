"""The storage an environment uses: measured by the worker's housekeeping, added up per request."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pyarrow as pa
import pytest
from core_test_helpers import _config, _RecordThenDeny
from pyiceberg.io import FileIO
from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadata
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef, Securable
from lakana_core.catalog import health, usage
from lakana_core.catalog.health import snapshot_stats
from lakana_core.catalog.service import CatalogService
from lakana_core.catalog.usage import (
    EnvironmentUsage,
    UsagePart,
    environment_usage,
    measure_table_sizes,
)
from lakana_core.errors import NotFoundError, PermissionDenied
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    ArtifactBlob,
    Environment,
    JobVersion,
    Namespace,
    Repository,
    RepositoryKind,
    Table,
    TableLocation,
    TableType,
)
from lakana_core.storage.resolver import StorageResolver

_BRONZE = NamespacePath.from_parts(["bronze"])
_SCHEMA = Schema(NestedField(1, "id", LongType(), required=True))
_DATA = pa.table(
    {"id": pa.array([1, 2, 3], pa.int64())},
    schema=pa.schema([pa.field("id", pa.int64(), nullable=False)]),
)


def _ref(name: str, environment: str = "dev") -> TableRef:
    """Name a table of `bronze`.

    Parameters
    ----------
    name
        The table's name.
    environment
        Its environment.

    Returns
    -------
    TableRef
        The reference.
    """
    return TableRef(environment, _BRONZE, name)


def _row(session: Session, name: str) -> Table:
    """Read a table's row afresh.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name.

    Returns
    -------
    Table
        The row.
    """
    session.expire_all()
    return session.execute(select(Table).where(Table.name == name)).scalar_one()


def _environment(session: Session, name: str = "dev") -> Environment:
    """Read an environment's row.

    Parameters
    ----------
    session
        Database session.
    name
        The environment's name.

    Returns
    -------
    Environment
        The row.
    """
    return session.execute(select(Environment).where(Environment.name == name)).scalar_one()


@pytest.fixture
def dev(
    catalog: CatalogService, principal: PrincipalRef, tmp_path: Path, session: Session
) -> CatalogService:
    """Provide `dev` with an empty table `bronze.empty` and a table `bronze.full` with rows.

    A commit stores the size, so the tables are made to look like those of an upgraded
    instance: no figure has been stored yet and the sweep has everything to measure.

    Parameters
    ----------
    catalog
        The allow-all catalog.
    principal
        The acting principal.
    tmp_path
        The storage root.
    session
        Database session.

    Returns
    -------
    CatalogService
        The catalog.
    """
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path / "dev"))
    catalog.create_table(principal, _ref("empty"), schema=_SCHEMA)
    catalog.create_table(principal, _ref("full"), schema=_SCHEMA)
    catalog.append(principal, _ref("full"), _DATA)
    session.execute(
        update(Table).values(
            size_bytes=None,
            size_metadata_location=None,
            row_count=None,
            last_write_at=None,
            updated_at=Table.updated_at,
        )
    )
    session.commit()
    return catalog


def _files_size(catalog: CatalogService, principal: PrincipalRef, name: str) -> int:
    """Read what a table's current snapshot says its files take.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    name
        The table's name.

    Returns
    -------
    int
        Its ``total-files-size``.
    """
    loaded = catalog.load_table(principal, _ref(name))
    snapshot = loaded.metadata.current_snapshot()
    assert snapshot is not None and snapshot.summary is not None
    return int(str(snapshot.summary["total-files-size"]))


def test_a_sweep_measures_each_table_once_and_again_after_it_changed(
    dev: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    storage = StorageResolver(session)

    assert measure_table_sizes(session, storage) == 2
    empty, full = _row(session, "empty"), _row(session, "full")
    assert (empty.size_bytes, empty.size_metadata_location) == (0, empty.metadata_location)
    assert full.size_bytes == _files_size(dev, principal, "full") > 0
    assert full.size_metadata_location == full.metadata_location

    assert measure_table_sizes(session, storage) == 0

    dev.append(principal, _ref("full"), _DATA)
    assert environment_usage(session, _environment(session)).tables.unmeasured == 0
    assert _row(session, "full").size_bytes == _files_size(dev, principal, "full")

    session.execute(
        update(Table).where(Table.name == "full").values(size_metadata_location="older.json")
    )
    session.commit()
    assert environment_usage(session, _environment(session)).tables.unmeasured == 1
    assert measure_table_sizes(session, storage) == 1
    assert _row(session, "full").size_bytes == _files_size(dev, principal, "full")


def test_a_sweep_stops_at_its_limit_its_deadline_and_a_stop(
    dev: CatalogService, session: Session
) -> None:
    storage = StorageResolver(session)
    assert measure_table_sizes(session, storage, limit=1) == 1
    assert measure_table_sizes(session, storage, should_stop=lambda: True) == 0
    ticks = iter([0.0, 5.0])
    assert measure_table_sizes(session, storage, max_seconds=1.0, clock=lambda: next(ticks)) == 0
    assert measure_table_sizes(session, storage) == 1


def test_a_table_whose_metadata_cannot_be_read_is_logged_and_skipped(
    dev: CatalogService, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        mock.patch.object(usage, "read_metadata", side_effect=OSError("gone")),
        caplog.at_level(logging.WARNING, logger=usage.__name__),
    ):
        assert measure_table_sizes(session, StorageResolver(session)) == 0
    assert "Could not read the size of table" in caplog.text
    assert "lakana env usage" in caplog.text
    assert _row(session, "full").size_bytes is None


def test_a_table_that_changed_while_it_was_read_keeps_no_figure(
    dev: CatalogService, session: Session
) -> None:
    real = usage.read_metadata

    def read_then_commit(file_io: FileIO, location: str) -> TableMetadata:
        """Read the metadata, then let another commit move the table on.

        Parameters
        ----------
        file_io
            Passed through.
        location
            Passed through.

        Returns
        -------
        TableMetadata
            The metadata read.
        """
        found = real(file_io, location)
        session.execute(
            update(Table)
            .where(Table.metadata_location == location)
            .values(metadata_location=location + ".next")
        )
        return found

    with mock.patch.object(usage, "read_metadata", side_effect=read_then_commit):
        assert measure_table_sizes(session, StorageResolver(session)) == 0
    assert _row(session, "full").size_bytes is None


def test_a_snapshot_without_a_size_is_stored_as_zero_bytes(
    dev: CatalogService, session: Session
) -> None:
    with mock.patch.object(health, "snapshot_stats") as stats:
        stats.return_value = health.SnapshotStats(None, None, None, None)
        measure_table_sizes(session, StorageResolver(session))
    found = environment_usage(session, _environment(session))
    assert found.tables == UsagePart(count=2, bytes=0, unmeasured=0)


def test_the_tables_of_a_deleted_environment_are_not_measured(
    dev: CatalogService, session: Session
) -> None:
    session.execute(update(Environment).values(deleted_at=datetime.now(UTC)))
    session.commit()
    assert measure_table_sizes(session, StorageResolver(session)) == 0


def _repository(session: Session) -> uuid.UUID:
    """Register a repository row.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        Its id.
    """
    repository = Repository(
        id=uuid.uuid4(), name="sales", kind=RepositoryKind.HTTPS, url="https://example.com/r.git"
    )
    session.add(repository)
    session.flush()
    return repository.id


def _version(session: Session, repository_id: uuid.UUID, env: str, code_bytes: int | None) -> None:
    """Record a project version.

    Parameters
    ----------
    session
        Database session.
    repository_id
        Its repository.
    env
        Its environment's name.
    code_bytes
        The size of its code archive, or `None` for one from before sizes were recorded.
    """
    session.add(
        JobVersion(
            id=uuid.uuid4(),
            repository_id=repository_id,
            environment_id=_environment(session, env).id,
            commit_sha=uuid.uuid4().hex + "00000000",
            manifest_json={},
            code_uri="file:///code.tar.gz",
            code_sha256="0" * 64,
            code_bytes=code_bytes,
        )
    )
    session.commit()


def _artifact(session: Session, env: str, name: str, sizes: list[int]) -> None:
    """Record an artifact with stored files of the given sizes.

    Parameters
    ----------
    session
        Database session.
    env
        Its environment's name.
    name
        Its name.
    sizes
        The sizes of its stored files.
    """
    namespace = (
        session.execute(
            select(Namespace).where(Namespace.environment_id == _environment(session, env).id)
        )
        .scalars()
        .first()
    )
    assert namespace is not None
    artifact = Table(
        id=uuid.uuid4(), namespace_id=namespace.id, name=name, table_type=TableType.ARTIFACT
    )
    session.add(artifact)
    session.flush()
    for position, size in enumerate(sizes):
        session.add(
            ArtifactBlob(
                artifact_id=artifact.id,
                sha256=f"{position:064x}",
                size_bytes=size,
                created_at=datetime.now(UTC),
            )
        )
    session.commit()


def test_the_usage_adds_up_tables_dropped_tables_artifacts_and_code_archives(
    dev: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    dev.create_environment(principal, name="prod", storage=_config(tmp_path / "prod"))
    dev.create_table(principal, _ref("other", "prod"), schema=_SCHEMA)
    dev.append(principal, _ref("other", "prod"), _DATA)
    dev.create_table(principal, _ref("gone"), schema=_SCHEMA)
    dev.append(principal, _ref("gone"), _DATA)
    dev.create_table(principal, _ref("purged"), schema=_SCHEMA)
    dev.create_table(principal, _ref("never"), schema=_SCHEMA)
    measure_table_sizes(session, StorageResolver(session))
    full = _files_size(dev, principal, "full")
    gone = _files_size(dev, principal, "gone")
    dev.drop_table(principal, _ref("gone"))
    dev.drop_table(principal, _ref("purged"), purge=True)
    session.execute(
        update(TableLocation)
        .where(TableLocation.purge_after <= TableLocation.dropped_at)
        .values(purge_started_at=datetime.now(UTC))
    )
    session.commit()
    dev.drop_table(principal, _ref("never"))
    tombstone = (
        session.execute(
            select(TableLocation).where(
                TableLocation.purge_started_at.is_(None), TableLocation.dropped_table.is_not(None)
            )
        )
        .scalars()
        .all()
    )
    for location in tombstone:
        if location.dropped_table and location.dropped_table["row"]["name"] == "never":
            row = dict(location.dropped_table["row"])
            del row["size_bytes"]
            location.dropped_table = {**location.dropped_table, "row": row}
    session.commit()
    _artifact(session, "dev", "model", [100, 23])
    _artifact(session, "prod", "elsewhere", [9999])
    repository_id = _repository(session)
    _version(session, repository_id, "dev", 500)
    _version(session, repository_id, "dev", None)
    _version(session, repository_id, "prod", 7)

    found = environment_usage(session, _environment(session))

    assert found == EnvironmentUsage(
        environment="dev",
        tables=UsagePart(count=2, bytes=full, unmeasured=0),
        dropped_tables=UsagePart(count=2, bytes=gone, unmeasured=1),
        artifacts=UsagePart(count=1, bytes=123, unmeasured=0),
        code_archives=UsagePart(count=2, bytes=500, unmeasured=1),
    )
    assert found.total_bytes == full + gone + 123 + 500
    assert not found.complete


def test_an_empty_environment_is_complete_and_uses_nothing(
    catalog: CatalogService, principal: PrincipalRef, session: Session, tmp_path: Path
) -> None:
    catalog.create_environment(principal, name="dev", storage=_config(tmp_path))
    found = catalog.environment_usage(principal, "dev")
    assert found.total_bytes == 0
    assert found.complete
    assert found.tables == UsagePart(count=0, bytes=0, unmeasured=0)


def test_the_usage_needs_manage_environment_on_the_environment(
    session: Session, principal: PrincipalRef, tmp_path: Path
) -> None:
    authz = _RecordThenDeny()
    catalog = CatalogService(session, authz=authz, storage=StorageResolver(session))
    with pytest.raises(PermissionDenied):
        catalog.environment_usage(principal, "dev")
    assert authz.seen == (Action.MANAGE_ENVIRONMENT, Securable.environment_scope("dev"))


def test_the_usage_of_an_unknown_environment_is_not_found(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    with pytest.raises(NotFoundError):
        catalog.environment_usage(principal, "nowhere")


def test_a_dropped_table_above_two_gibibytes_is_added_up_whole(
    dev: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    measure_table_sizes(session, StorageResolver(session))
    session.execute(update(Table).where(Table.name == "full").values(size_bytes=5 * 1024**3))
    session.commit()
    dev.drop_table(principal, _ref("full"))
    found = environment_usage(session, _environment(session))
    assert found.dropped_tables == UsagePart(count=1, bytes=5 * 1024**3, unmeasured=0)


def test_a_sum_postgres_returns_as_a_decimal_is_read_as_a_whole_number() -> None:
    assert usage._part(Decimal(3), Decimal("5368709120"), None) == UsagePart(3, 5 * 1024**3, 0)


def test_the_size_sweep_fills_rows_and_last_write_of_an_upgraded_table(
    dev: CatalogService, principal: PrincipalRef, session: Session
) -> None:
    stats = snapshot_stats(dev.load_table(principal, _ref("full")).metadata)
    session.execute(
        update(Table)
        .where(Table.name == "full")
        .values(
            row_count=None,
            last_write_at=None,
            size_metadata_location=None,
            updated_at=Table.updated_at,
        )
    )
    session.commit()
    row = _row(session, "full")
    updated_before = row.updated_at
    assert (row.row_count, row.last_write_at) == (None, None)

    assert measure_table_sizes(session, StorageResolver(session)) == 2
    row = _row(session, "full")
    assert (row.row_count, row.last_write_at) == (stats.row_count, stats.last_write_at)
    assert row.row_count == 3
    assert row.updated_at == updated_before


def test_a_measurement_is_no_activity_of_the_table(dev: CatalogService, session: Session) -> None:
    """Retention counts from `updated_at`: measuring a table must not move it."""
    session.execute(update(Table).values(updated_at=datetime(2020, 1, 1, tzinfo=UTC)))
    session.commit()
    measure_table_sizes(session, StorageResolver(session))
    assert _row(session, "full").size_bytes is not None
    assert _row(session, "full").updated_at == datetime(2020, 1, 1, tzinfo=UTC)
