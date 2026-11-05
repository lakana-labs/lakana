"""Shared helpers of the health signal tests (#340): one table, its history, a deployment."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
from core_test_helpers import _prepared_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField, StringType
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog.service import CatalogService
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import (
    EnvironmentDeployment,
    JobVersion,
    Namespace,
    Repository,
    RepositoryKind,
    Table,
    TableHealthSignal,
)

_SILVER = NamespacePath.from_parts(["silver"])
_ORDERS = TableRef("dev", _SILVER, "orders")
_SCHEMA = Schema(
    NestedField(1, "id", LongType(), required=True),
    NestedField(2, "status", StringType(), required=False),
)
_ARROW = pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("status", pa.string())])
_T0 = datetime(2026, 10, 1, tzinfo=UTC)

_MANIFEST = {
    "version": 14,
    "assets": [
        {"table": "silver.orders", "path": "assets/silver/orders.sql"},
        {"table": "gold.report", "path": "assets/gold/report.sql"},
    ],
    "edges": [["silver.orders", "gold.report"]],
    "jobs": {
        "nightly": {
            "select": ["silver.*"],
            "schedule": {"cron": "0 6 * * *", "timezone": "UTC"},
            "notify": {"webhook": "team-hook", "format": "slack"},
        },
        "reports": {"select": ["gold.*"], "notify": {"webhook": "team-hook", "format": "json"}},
        "quiet": {"select": ["silver.orders"]},
        "broken": {"select": [" "]},  # a blank pattern: `resolve_selection` refuses it
    },
}
"""A deployed project whose job ``nightly`` builds `silver.orders` and notifies."""

_MANIFEST_WITHOUT_SCHEDULE = {
    **_MANIFEST,
    "jobs": {
        **_MANIFEST["jobs"],
        "nightly": {k: v for k, v in _MANIFEST["jobs"]["nightly"].items() if k != "schedule"},
    },
}
"""`_MANIFEST` with job ``nightly`` unscheduled."""


def _rows(ids: list[int]) -> pa.Table:
    """Build rows of `silver.orders`, the status of every other row null.

    Parameters
    ----------
    ids
        The ids.

    Returns
    -------
    pa.Table
        The rows.
    """
    status = [None if i % 2 else "s" for i in ids]
    return pa.table({"id": ids, "status": status}, schema=_ARROW)


def health_catalog(session: Session, tmp_path: Path, principal: PrincipalRef) -> CatalogService:
    """Build an allow-all catalog with the empty table `silver.orders`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The creator.

    Returns
    -------
    CatalogService
        The catalog.
    """
    built = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    built.create_table(principal, _ORDERS, schema=_SCHEMA)
    return built


def _table_id(session: Session, name: str = "orders") -> uuid.UUID:
    """Look up the id of a table, `silver.orders` by default.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name, without its namespace.

    Returns
    -------
    uuid.UUID
        The id.
    """
    return session.scalars(select(Table.id).where(Table.name == name)).one()


def _environment_id(session: Session, name: str = "orders") -> uuid.UUID:
    """Look up the environment of a table, `silver.orders` by default.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name, without its namespace.

    Returns
    -------
    uuid.UUID
        The id.
    """
    return session.scalars(
        select(Namespace.environment_id)
        .join(Table, Table.namespace_id == Namespace.id)
        .where(Table.name == name)
    ).one()


def _seed(
    session: Session,
    signal: str,
    observed: dict,
    *,
    count: int = 8,
    column: str | None = None,
    first_day: int = 0,
    state: str = "ok",
    table: str = "orders",
) -> None:
    """Record a history of one signal of a table, one day apart, from `_T0`.

    Parameters
    ----------
    session
        Database session; commits.
    signal
        The signal.
    observed
        The observed value of every row.
    count
        How many rows.
    column
        The column, for a column signal.
    first_day
        The day after `_T0` of the oldest row.
    state
        The state of every row.
    table
        The table's name, without its namespace; `silver.orders` by default.
    """
    table_id = _table_id(session, table)
    environment_id = _environment_id(session, table)
    for day in range(first_day, first_day + count):
        session.add(
            TableHealthSignal(
                id=uuid.uuid4(),
                environment_id=environment_id,
                table_id=table_id,
                signal=signal,
                column_name=column,
                snapshot_id=day + 1,
                sequence_number=day + 1,
                observed=observed,
                band=None,
                state=state,
                effect="warn",
                observed_at=_T0 + timedelta(days=day),
            )
        )
    session.commit()


def _deployed(session: Session, manifest: dict, *, repository: str = "shop") -> uuid.UUID:
    """Deploy a manifest to the `dev` environment, as a deploy leaves it.

    Stores a `Repository` on first use, a `JobVersion` holding the manifest and the environment's
    `EnvironmentDeployment` pointing at it, filled in as `core_test_helpers.deploy_manifest` does.

    Parameters
    ----------
    session
        Database session; commits.
    manifest
        The manifest.
    repository
        The repository's name.

    Returns
    -------
    uuid.UUID
        The repository's id.
    """
    environment_id = _environment_id(session)
    repo = session.scalars(select(Repository).where(Repository.name == repository)).one_or_none()
    if repo is None:
        repo = Repository(
            id=uuid.uuid4(),
            name=repository,
            kind=RepositoryKind.HTTPS,
            url=f"https://git.example.org/{repository}.git",
        )
        session.add(repo)
        session.flush()
    version = JobVersion(
        id=uuid.uuid4(),
        repository_id=repo.id,
        environment_id=environment_id,
        commit_sha=uuid.uuid4().hex + "00000000",
        manifest_json=manifest,
        code_uri="file:///code.tar.gz",
        code_sha256="0" * 64,
    )
    session.add(version)
    session.flush()
    pointer = session.get(EnvironmentDeployment, (environment_id, repo.id))
    if pointer is None:
        session.add(
            EnvironmentDeployment(
                environment_id=environment_id,
                repository_id=repo.id,
                current_job_version_id=version.id,
            )
        )
    else:
        pointer.current_job_version_id = version.id
    session.commit()
    return repo.id
