"""Arrow `replace`, and the `sources` Arrow writes declare."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest
from core_test_helpers import ActionAllowList
from pyiceberg.schema import Schema
from pyiceberg.table import StaticTable
from pyiceberg.table.snapshots import Summary
from pyiceberg.table.update import AddSnapshotUpdate, TableRequirement, TableUpdate
from pyiceberg.types import LongType, NestedField
from sqlalchemy import select
from sqlalchemy.orm import Session

from lakana_core.auth import Action, AllowAllAuthorization, BoundTask, PrincipalRef, TaskScope
from lakana_core.catalog.append import build_transaction
from lakana_core.catalog.attempt_stamp import CLAIM_PROPERTY, TASK_RUN_PROPERTY
from lakana_core.catalog.service import CatalogService, LoadedTable
from lakana_core.catalog.writes import parse_expected_snapshot
from lakana_core.config import StorageConfig
from lakana_core.connections.kinds import ConnectionKind
from lakana_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from lakana_core.identifiers import TableRef
from lakana_core.models import (
    AuditEvent,
    AuditEventType,
    Connection,
    Environment,
    LineageEdge,
    TableType,
    WriteMode,
)
from lakana_core.storage.resolver import StorageResolver


def _ids(data: list[int]) -> pa.Table:
    """Build a one-column table of ids, as a caller's Arrow upload would.

    Parameters
    ----------
    data
        The ids.

    Returns
    -------
    pa.Table
        The rows.
    """
    return pa.table({"id": pa.array(data, pa.int64())})


@pytest.fixture
def catalog(session: Session, principal: PrincipalRef, tmp_path: Path) -> CatalogService:
    """Provide `dev` with `bronze.src` and `gold.t`, each holding one row.

    Parameters
    ----------
    session
        Database session.
    principal
        The acting principal.
    tmp_path
        The storage root.

    Returns
    -------
    CatalogService
        An allow-all catalog.
    """
    catalog = CatalogService(session, StorageResolver(session), AllowAllAuthorization())
    catalog.create_environment(
        principal,
        name="dev",
        storage=StorageConfig(scheme="file", root=f"file://{tmp_path}", options={}),
    )
    for dotted in ("bronze.src", "gold.t"):
        catalog.write_batches(
            principal,
            TableRef.from_dotted("dev", dotted),
            mode=WriteMode.CREATE,
            schema=_ids([1]).schema,
            batches=_ids([1]).to_batches(),
        )
    return catalog


def _ref(dotted: str) -> TableRef:
    """Address a table in `dev`.

    Parameters
    ----------
    dotted
        The dotted name.

    Returns
    -------
    TableRef
        The reference.
    """
    return TableRef.from_dotted("dev", dotted)


def _current_snapshot(catalog: CatalogService, principal: PrincipalRef, dotted: str) -> int | None:
    """Read a table's current snapshot id.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The acting principal.
    dotted
        The table.

    Returns
    -------
    int or None
        The snapshot id.
    """
    return catalog.load_table(principal, _ref(dotted)).metadata.current_snapshot_id


def test_replace_swaps_the_rows_and_records_a_replace_write(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`tables.replace` is the same governed path as a SQL `replace`."""
    result = catalog.replace(principal, _ref("gold.t"), _ids([7, 8]), sources=["bronze.src"])

    loaded = catalog.load_table(principal, _ref("gold.t"))
    rows = StaticTable.from_metadata(loaded.metadata_location).scan().to_arrow()
    assert sorted(rows.column("id").to_pylist()) == [7, 8]
    assert result.snapshot_id == loaded.metadata.current_snapshot_id
    event = (
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.event_type == AuditEventType.TABLE_WRITE)
            .where(AuditEvent.securable_table == "t")
            .order_by(AuditEvent.occurred_at.desc())
        )
        .scalars()
        .first()
    )
    assert event is not None
    assert event.detail["mode"] == "replace"
    edge = session.execute(select(LineageEdge)).scalar_one()
    assert (edge.source_name, edge.target_name) == ("bronze.src", "gold.t")
    assert edge.target_snapshot_id == result.snapshot_id


def test_append_with_sources_records_one_edge_per_source(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Arrow appends take `sources=[...]`, one edge each, with the source's snapshot."""
    source_snapshot = _current_snapshot(catalog, principal, "bronze.src")

    result = catalog.append(principal, _ref("gold.t"), _ids([2]), sources=["bronze.src"])

    edge = session.execute(select(LineageEdge)).scalar_one()
    assert edge.source_snapshot_id == source_snapshot
    assert edge.target_snapshot_id == result.snapshot_id
    assert edge.query_run_id is None


def test_an_append_without_sources_records_no_edges(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """M4a behaviour is unchanged when no sources are declared."""
    catalog.append(principal, _ref("gold.t"), _ids([2]))

    assert session.execute(select(LineageEdge)).first() is None


@pytest.mark.parametrize("write", ["append", "replace"])
def test_a_missing_source_refuses_the_write_before_anything_is_written(
    session: Session, principal: PrincipalRef, catalog: CatalogService, write: str
) -> None:
    """Every declared source must exist, or nothing is written."""
    before = _current_snapshot(catalog, principal, "gold.t")

    with pytest.raises(NotFoundError):
        getattr(catalog, write)(principal, _ref("gold.t"), _ids([2]), sources=["bronze.nope"])

    assert _current_snapshot(catalog, principal, "gold.t") == before


@pytest.mark.parametrize("write", ["append", "replace"])
def test_an_unreadable_source_refuses_the_write(
    session: Session, principal: PrincipalRef, catalog: CatalogService, write: str
) -> None:
    """Every declared source must be READ-able by the caller."""
    writer_only = CatalogService(
        session, StorageResolver(session), ActionAllowList(Action.WRITE_TABLE)
    )
    before = _current_snapshot(catalog, principal, "gold.t")

    with pytest.raises(PermissionDenied, match="READ_TABLE"):
        getattr(writer_only, write)(principal, _ref("gold.t"), _ids([2]), sources=["bronze.src"])

    assert _current_snapshot(catalog, principal, "gold.t") == before


def test_replace_needs_write_table(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Security: an Arrow replace is authorized like a SQL one."""
    reader = CatalogService(session, StorageResolver(session), ActionAllowList(Action.READ_TABLE))

    with pytest.raises(PermissionDenied, match="WRITE_TABLE"):
        reader.replace(principal, _ref("gold.t"), _ids([2]))


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("none", "none"), ("0", 0), ("42", 42), (str(2**63 - 1), 2**63 - 1)],
)
def test_parse_expected_snapshot(value: str | None, expected: object) -> None:
    assert parse_expected_snapshot(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "NONE", "-1", "+1", " 1", "1e3", "0x1", "9" * 20, "١٢", "007", "00", "9" * 19, str(2**63)],
)
def test_parse_expected_snapshot_refuses_everything_else(value: str) -> None:
    with pytest.raises(
        ValidationError, match="expected_snapshot_id must be a snapshot id or 'none'"
    ):
        parse_expected_snapshot(value)


def test_a_create_that_loses_the_race_is_a_conflict(
    catalog: CatalogService, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "_existing_table", lambda _ref: None)
    with pytest.raises(ConflictError, match="changed during the write"):
        catalog.replace(principal, _ref("gold.t"), _ids([2]), expected_snapshot_id="none")


def test_a_table_that_appears_during_the_precheck_is_a_conflict(
    catalog: CatalogService, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "_existing_table", lambda _ref: None)
    with pytest.raises(ConflictError, match="changed during the write"):
        catalog.check_can_replace(principal, _ref("gold.t"))


def test_a_create_pin_on_a_table_that_has_a_snapshot_is_refused_before_any_data_is_read(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    with pytest.raises(ConflictError, match=r"gold\.t already has data"):
        catalog.check_can_replace(principal, _ref("gold.t"), expected_snapshot_id="none")
    with pytest.raises(ConflictError, match=r"gold\.t already has data"):
        catalog.check_can_append(principal, _ref("gold.t"), expected_snapshot_id="none")


def test_a_create_pin_on_a_table_without_a_snapshot_passes_the_prechecks(
    catalog: CatalogService, principal: PrincipalRef
) -> None:
    ref = _ref("gold.empty")
    catalog.create_table(principal, ref, schema=Schema(NestedField(1, "id", LongType())))

    catalog.check_can_replace(principal, ref, expected_snapshot_id="none")
    catalog.check_can_append(principal, ref, expected_snapshot_id="none")
    catalog.check_can_merge(principal, ref, expected_snapshot_id="none")


def test_a_replace_on_an_external_table_says_replace(
    catalog: CatalogService, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        catalog, "_existing_table", lambda _ref: SimpleNamespace(table_type=TableType.EXTERNAL)
    )
    with pytest.raises(ValidationError, match="Replaces are only supported"):
        catalog.check_can_replace(principal, _ref("gold.t"))


def test_a_table_dropped_after_the_precheck_is_a_conflict_not_an_unpinned_replace(
    catalog: CatalogService, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = _current_snapshot(catalog, principal, "gold.t")
    real = catalog._existing_table
    calls = iter([real(_ref("gold.t")), None])
    monkeypatch.setattr(catalog, "_existing_table", lambda _ref: next(calls))
    with pytest.raises(ConflictError, match=r"gold\.t changed during the write; retry\."):
        catalog.replace(principal, _ref("gold.t"), _ids([2]), expected_snapshot_id=current)


@pytest.mark.parametrize("method", ["append", "replace"])
def test_a_writer_landing_between_the_precheck_and_the_commit_is_a_conflict(
    catalog: CatalogService,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    current = _current_snapshot(catalog, principal, "gold.t")
    real_commit = catalog.commit_table
    fired: list[bool] = []

    def racing(
        p: PrincipalRef,
        ref: TableRef,
        *,
        requirements: Sequence[TableRequirement],
        updates: Sequence[TableUpdate],
        own_data_files: bool = False,
    ) -> LoadedTable:
        if not fired:
            fired.append(True)
            catalog.write_batches(
                principal,
                _ref("gold.t"),
                mode=WriteMode.APPEND,
                schema=_ids([5]).schema,
                batches=_ids([5]).to_batches(),
            )
        return real_commit(
            p, ref, requirements=requirements, updates=updates, own_data_files=own_data_files
        )

    monkeypatch.setattr(catalog, "commit_table", racing)
    with pytest.raises(ConflictError) as raised:
        getattr(catalog, method)(principal, _ref("gold.t"), _ids([2]), expected_snapshot_id=current)
    assert str(raised.value) == "gold.t changed during the write; retry."
    ids = (
        StaticTable.from_metadata(catalog.load_table(principal, _ref("gold.t")).metadata_location)
        .scan()
        .to_arrow()
    )
    assert sorted(ids.column("id").to_pylist()) == [1, 5]


@pytest.mark.parametrize("method", ["append", "replace"])
def test_a_writer_landing_in_the_commit_of_an_empty_table_is_a_conflict(
    catalog: CatalogService,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    empty = pa.table({"id": pa.array([], pa.int64())})
    catalog.write_batches(
        principal,
        _ref("gold.e"),
        mode=WriteMode.CREATE,
        schema=empty.schema,
        batches=empty.to_batches(),
    )
    assert catalog.load_table(principal, _ref("gold.e")).metadata.current_snapshot_id is None
    real_commit = catalog.commit_table
    fired: list[bool] = []

    def racing(
        p: PrincipalRef,
        ref: TableRef,
        *,
        requirements: Sequence[TableRequirement],
        updates: Sequence[TableUpdate],
        own_data_files: bool = False,
    ) -> LoadedTable:
        if not fired:
            fired.append(True)
            catalog.write_batches(
                principal,
                _ref("gold.e"),
                mode=WriteMode.APPEND,
                schema=_ids([5]).schema,
                batches=_ids([5]).to_batches(),
            )
        return real_commit(
            p, ref, requirements=requirements, updates=updates, own_data_files=own_data_files
        )

    monkeypatch.setattr(catalog, "commit_table", racing)
    with pytest.raises(ConflictError) as raised:
        getattr(catalog, method)(principal, _ref("gold.e"), _ids([2]), expected_snapshot_id="none")
    assert str(raised.value) == "gold.e changed during the write; retry."
    ids = (
        StaticTable.from_metadata(catalog.load_table(principal, _ref("gold.e")).metadata_location)
        .scan()
        .to_arrow()
    )
    assert ids.column("id").to_pylist() == [5]


def _connection(session: Session, principal: PrincipalRef, name: str) -> Connection:
    """Create a PostgreSQL connection in `dev`.

    Parameters
    ----------
    session
        Database session.
    principal
        Its creator.
    name
        Its name.

    Returns
    -------
    Connection
        The row.
    """
    environment = session.scalars(select(Environment).where(Environment.name == "dev")).one()
    row = Connection(
        id=uuid.uuid4(),
        environment_id=environment.id,
        name=name,
        kind=ConnectionKind.POSTGRESQL,
        address={"host": "db.example.com", "port": 5432, "database": "crm"},
        settings={"user": "reader", "tls": "verify-full"},
        created_by=principal.id,
    )
    session.add(row)
    session.commit()
    return row


def _task(principal: PrincipalRef, *connections: str, readable: bool = True) -> PrincipalRef:
    """Act as the bound task token of the asset `gold.t`, which declares `connections`.

    Parameters
    ----------
    principal
        The run principal.
    *connections
        The connection names the deployed manifest declares.
    readable
        Whether the manifest's `connections` value could be read.

    Returns
    -------
    PrincipalRef
        The principal as its bound token authenticates it.
    """
    bound = BoundTask(
        task_run_id=uuid.uuid4(),
        claim=1,
        asset="gold.t",
        inputs=("bronze.src",),
        readable=("bronze.src",),
        connections=connections,
        connections_readable=readable,
        repository="analytics",
    )
    return dataclasses.replace(
        principal,
        task_scope=TaskScope(environment="dev", asset_scope=[], token_id=uuid.uuid4(), bound=bound),
    )


def _connection_edges(session: Session) -> list[LineageEdge]:
    """Read the connection edges, by source name.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of LineageEdge
        The edges whose source is a connection.
    """
    return list(
        session.scalars(
            select(LineageEdge)
            .where(LineageEdge.source_kind == "connection")
            .order_by(LineageEdge.source_name)
        )
    )


@pytest.mark.parametrize("write", ["append", "replace"])
def test_a_task_write_records_one_edge_per_declared_connection(
    session: Session, principal: PrincipalRef, catalog: CatalogService, write: str
) -> None:
    """The server derives connection sources from the deployed asset, beside the table edges."""
    crm = _connection(session, principal, "crm-db")
    files = _connection(session, principal, "files")

    result = getattr(catalog, write)(
        _task(principal, "crm-db", "files"), _ref("gold.t"), _ids([2]), sources=["bronze.src"]
    )

    edges = _connection_edges(session)
    assert [
        (e.source_table_id, e.source_name, e.source_snapshot_id, e.target_name) for e in edges
    ] == [(crm.id, "crm-db", None, "gold.t"), (files.id, "files", None, "gold.t")]
    assert {e.target_snapshot_id for e in edges} == {result.snapshot_id}
    table_edge = session.scalars(
        select(LineageEdge).where(LineageEdge.source_kind == "table")
    ).one()
    assert table_edge.source_name == "bronze.src"


def test_a_sql_style_write_of_a_task_records_the_connections_too(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """`write_batches` (a SQL write submitted with the task's token) takes the same path."""
    crm = _connection(session, principal, "crm-db")

    catalog.write_batches(
        _task(principal, "crm-db"),
        _ref("gold.t"),
        mode=WriteMode.APPEND,
        schema=_ids([3]).schema,
        batches=_ids([3]).to_batches(),
    )

    assert [(e.source_table_id, e.source_name) for e in _connection_edges(session)] == [
        (crm.id, "crm-db")
    ]


def test_a_declared_connection_that_no_longer_exists_is_skipped_and_logged(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing name records no edge and never fails the write."""
    crm = _connection(session, principal, "crm-db")

    with caplog.at_level(logging.INFO, logger="lakana_core.catalog.connection_lineage"):
        catalog.append(_task(principal, "crm-db", "gone"), _ref("gold.t"), _ids([2]))

    assert [e.source_table_id for e in _connection_edges(session)] == [crm.id]
    skipped = [r for r in caplog.records if "'gone'" in r.getMessage()]
    assert len(skipped) == 1
    assert skipped[0].levelno == logging.INFO


def test_an_unreadable_connections_entry_records_none_and_logs_once(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A damaged manifest entry never fails the write; it is logged once, for this write."""
    _connection(session, principal, "crm-db")

    with caplog.at_level(logging.INFO, logger="lakana_core.catalog.connection_lineage"):
        result = catalog.append(_task(principal, readable=False), _ref("gold.t"), _ids([2]))

    assert result.snapshot_id is not None
    assert _connection_edges(session) == []
    unreadable = [r for r in caplog.records if "cannot be read" in r.getMessage()]
    assert len(unreadable) == 1


@pytest.mark.parametrize("write", ["append", "replace"])
def test_a_pat_write_records_no_connection_edge(
    session: Session, principal: PrincipalRef, catalog: CatalogService, write: str
) -> None:
    """Only a bound task token's write names connections."""
    _connection(session, principal, "crm-db")

    getattr(catalog, write)(principal, _ref("gold.t"), _ids([2]), sources=["bronze.src"])

    assert _connection_edges(session) == []


def test_an_unbound_task_token_records_no_connection_edge(
    session: Session, principal: PrincipalRef, catalog: CatalogService
) -> None:
    """A REST-minted task token has no deployed asset, so no connections."""
    _connection(session, principal, "crm-db")
    unbound = dataclasses.replace(
        principal,
        task_scope=TaskScope(environment="dev", asset_scope=["gold.t"], token_id=uuid.uuid4()),
    )

    catalog.append(unbound, _ref("gold.t"), _ids([2]))

    assert _connection_edges(session) == []


@pytest.mark.parametrize("write", ["append", "replace"])
@pytest.mark.parametrize("actor", ["pat", "task"])
def test_a_client_supplied_source_named_like_a_connection_is_a_table_lookup(
    session: Session, principal: PrincipalRef, catalog: CatalogService, write: str, actor: str
) -> None:
    """`sources` can never produce a connection edge: `crm-db` is not a table, so it fails."""
    _connection(session, principal, "crm-db")
    p = principal if actor == "pat" else _task(principal)
    before = _current_snapshot(catalog, principal, "gold.t")

    with pytest.raises((NotFoundError, ValidationError)):
        getattr(catalog, write)(p, _ref("gold.t"), _ids([2]), sources=["crm-db"])

    assert _current_snapshot(catalog, principal, "gold.t") == before
    assert session.scalars(select(LineageEdge)).first() is None


def test_a_connection_of_another_environment_is_never_a_source(
    session: Session,
    principal: PrincipalRef,
    catalog: CatalogService,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dev task declaring `crm-db` gets no edge from prod's `crm-db`; the name is skipped."""
    prod_root = tmp_path / "prod"
    prod_root.mkdir()
    catalog.create_environment(
        principal,
        name="prod",
        storage=StorageConfig(scheme="file", root=f"file://{prod_root}", options={}),
    )
    prod = session.scalars(select(Environment).where(Environment.name == "prod")).one()
    session.add(
        Connection(
            id=uuid.uuid4(),
            environment_id=prod.id,
            name="crm-db",
            kind=ConnectionKind.POSTGRESQL,
            address={"host": "db.example.com", "port": 5432, "database": "crm"},
            settings={"user": "reader", "tls": "verify-full"},
            created_by=principal.id,
        )
    )
    session.commit()

    with caplog.at_level(logging.INFO, logger="lakana_core.catalog.connection_lineage"):
        catalog.append(_task(principal, "crm-db"), _ref("gold.t"), _ids([2]))

    assert _connection_edges(session) == []
    skipped = [r for r in caplog.records if "'crm-db'" in r.getMessage()]
    assert [r.levelno for r in skipped] == [logging.INFO]


def test_a_commit_cannot_claim_the_write_of_a_task_attempt(
    principal: PrincipalRef, catalog: CatalogService
) -> None:
    """Issue #49: a client's snapshot summary that names a task attempt loses that name."""
    loaded = catalog.load_table(principal, _ref("gold.t"))
    resolved = StorageResolver(catalog._session).for_environment("dev")
    requirements, updates = build_transaction(
        loaded.metadata, loaded.metadata_location, resolved.file_io, _ids([5])
    )
    forged: list[TableUpdate] = []
    for update in updates:
        if isinstance(update, AddSnapshotUpdate):
            summary = update.snapshot.summary
            assert summary is not None
            claim = {TASK_RUN_PROPERTY: str(uuid.uuid4()), CLAIM_PROPERTY: "1"}
            update = AddSnapshotUpdate(
                snapshot=update.snapshot.model_copy(
                    update={
                        "summary": Summary(
                            summary.operation, **summary.additional_properties, **claim
                        )
                    }
                )
            )
        forged.append(update)

    catalog.commit_table(principal, _ref("gold.t"), requirements=requirements, updates=forged)

    current = catalog.load_table(principal, _ref("gold.t")).metadata.current_snapshot()
    assert current is not None and current.summary is not None
    assert current.summary.get(TASK_RUN_PROPERTY) is None
    assert current.summary.get(CLAIM_PROPERTY) is None
    assert current.summary.get("added-records") == "1"
