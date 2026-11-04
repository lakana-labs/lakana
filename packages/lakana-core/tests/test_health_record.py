"""Health signals recorded when a snapshot is committed (#340)."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pyarrow as pa
import pytest
from core_test_helpers import _prepared_catalog
from pyiceberg.conversions import to_bytes
from pyiceberg.manifest import DataFile
from pyiceberg.schema import Schema
from pyiceberg.table.snapshots import Operation, Summary
from pyiceberg.table.update import SetPropertiesUpdate
from pyiceberg.types import DoubleType, LongType, NestedField, StringType
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.auth import AllowAllAuthorization, PrincipalRef
from lakana_core.catalog import field_ids, health_record
from lakana_core.catalog.health_record import commit_observations, recent_observations, signal_rows
from lakana_core.catalog.health_signals import HealthEffects, Observation
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Namespace, Table, TableHealthSignal, WriteMode

_SILVER = NamespacePath.from_parts(["silver"])
_ORDERS = TableRef("dev", _SILVER, "orders")
_SCHEMA = Schema(
    NestedField(1, "id", LongType(), required=True),
    NestedField(2, "status", StringType(), required=False),
)
_ARROW = pa.schema([pa.field("id", pa.int64(), nullable=False), pa.field("status", pa.string())])
_T0 = datetime(2026, 10, 1, tzinfo=UTC)


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


@pytest.fixture
def catalog(session: Session, tmp_path: Path, principal: PrincipalRef) -> CatalogService:
    """Provide an allow-all catalog with the empty table `silver.orders`.

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


def _table_id(session: Session) -> uuid.UUID:
    """Look up the id of `silver.orders`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The id.
    """
    return session.scalars(select(Table.id).where(Table.name == "orders")).one()


def _environment_id(session: Session) -> uuid.UUID:
    """Look up the environment of `silver.orders`.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    uuid.UUID
        The id.
    """
    return session.scalars(
        select(Namespace.environment_id)
        .join(Table, Table.namespace_id == Namespace.id)
        .where(Table.name == "orders")
    ).one()


def _signals(session: Session) -> list[tuple[str, str | None, str, str]]:
    """Read the recorded signals, sorted.

    Parameters
    ----------
    session
        Database session.

    Returns
    -------
    list of tuple
        Signal, column, state and effect.
    """
    rows = session.scalars(select(TableHealthSignal))
    return sorted(
        ((r.signal, r.column_name, r.state, r.effect) for r in rows),
        key=lambda row: (row[0], row[1] or "", row[2], row[3]),
    )


def _seed(
    session: Session,
    signal: str,
    observed: dict,
    *,
    count: int = 8,
    column: str | None = None,
    first_day: int = 0,
) -> None:
    """Record a history of one signal for `silver.orders`, one day apart, before today.

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
    """
    table_id = _table_id(session)
    environment_id = _environment_id(session)
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
                state="ok",
                effect="warn",
                observed_at=_T0 + timedelta(days=day),
            )
        )
    session.commit()


def _recording(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Record each call of the manifest read while still reading.

    Parameters
    ----------
    monkeypatch
        Replaces `health_record.added_data_files`.

    Returns
    -------
    list of tuple
        The arguments of each call, appended as they happen.
    """
    calls: list[tuple] = []
    real = health_record.added_data_files

    def recording(*args: object) -> list[DataFile]:
        """Note the arguments, then read.

        Parameters
        ----------
        *args
            The read's arguments.

        Returns
        -------
        list of DataFile
            What the real read returns.
        """
        calls.append(args)
        return real(*args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(health_record, "added_data_files", recording)
    return calls


def test_an_append_records_the_commit_signals(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    """An append records volume, schema and each column's null rate and range.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    """
    catalog.append(principal, _ORDERS, _rows([1, 2, 3]))
    assert _signals(session) == [
        ("null_rate", "id", "no_history", "warn"),
        ("null_rate", "status", "no_history", "warn"),
        ("range", "id", "no_history", "warn"),
        ("schema", None, "ok", "warn"),
        ("volume", None, "no_history", "warn"),
    ]
    volume = session.scalars(
        select(TableHealthSignal).where(TableHealthSignal.signal == "volume")
    ).one()
    assert volume.observed == {"added": 3, "total": 3}
    assert volume.snapshot_id is not None
    assert volume.environment_id == _environment_id(session)
    current = catalog.load_table(principal, _ORDERS).metadata.current_snapshot()
    assert current is not None
    assert {r.sequence_number for r in session.scalars(select(TableHealthSignal))} == {
        current.sequence_number
    }


def test_a_table_created_with_rows_records_its_first_snapshot(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """A create-mode write records the signals of the table's first snapshot.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The writer.
    """
    built = _prepared_catalog(session, tmp_path, AllowAllAuthorization(), namespaces=[_SILVER])
    data = _rows([1, 2])
    built.write_batches(
        principal, _ORDERS, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
    )
    assert ("volume", None, "no_history", "warn") in _signals(session)
    assert ("null_rate", "status", "no_history", "warn") in _signals(session)


def test_volume_fires_against_a_history_of_eight(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    """Three rows against a steady thousand fire the volume signal with the learned band.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    """
    _seed(session, "volume", {"added": 1_000, "total": 1_000})
    catalog.append(principal, _ORDERS, _rows([1, 2, 3]))
    fired = session.scalars(
        select(TableHealthSignal).where(
            TableHealthSignal.signal == "volume", TableHealthSignal.state == "fired"
        )
    ).one()
    assert fired.band == {"low": 900.0, "high": 1_100.0, "median": 1_000.0}


def test_schema_compares_with_the_schema_of_the_snapshot_before(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    """A column the snapshot before did not have fires the schema signal.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    """
    narrow = TableRef("dev", _SILVER, "narrow")
    data = pa.table({"id": pa.array([1], pa.int64())})
    catalog.write_batches(
        principal, narrow, mode=WriteMode.CREATE, schema=data.schema, batches=data.to_batches()
    )
    catalog.append(principal, _ORDERS, _rows([1]))
    pairs = commit_observations(
        session,
        table="silver.orders",
        table_id=None,
        before=catalog.load_table(principal, narrow).metadata,
        after=catalog.load_table(principal, _ORDERS).metadata,
        snapshots=[],
        file_io=catalog._storage.for_environment("dev").file_io,
        effects=HealthEffects.default(),
        files=[],
    )
    schema = next(o for o, _ in pairs if o.signal == "schema")
    assert schema.observed is not None
    assert (schema.state, schema.observed["added"]) == ("fired", [["status", "string"]])


def test_blocked_observations_are_left_out_of_the_baseline(
    catalog: CatalogService, session: Session
) -> None:
    """A fired row whose effect blocked the write is not learned from.

    Parameters
    ----------
    catalog
        The catalog, which creates the table.
    session
        Database session.
    """
    _seed(session, "volume", {"added": 5, "total": 5}, count=3)
    blocked = session.scalars(select(TableHealthSignal)).first()
    assert blocked is not None
    blocked.state, blocked.effect = "fired", "block"
    session.commit()
    assert len(recent_observations(session, _table_id(session))["volume", None]) == 2


def test_a_commit_without_a_new_snapshot_records_nothing(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    """A commit that only sets a property records no signal.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    """
    catalog.commit_table(
        principal, _ORDERS, requirements=[], updates=[SetPropertiesUpdate(updates={"a": "b"})]
    )
    assert _signals(session) == []


def test_metadata_without_a_snapshot_has_no_observations(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    """Metadata without a current snapshot has nothing to observe.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The reader.
    """
    empty = catalog.load_table(principal, _ORDERS).metadata
    assert (
        commit_observations(
            session,
            table="silver.orders",
            table_id=_table_id(session),
            before=None,
            after=empty,
            snapshots=[],
            file_io=catalog._storage.for_environment("dev").file_io,
            effects=HealthEffects.default(),
        )
        == []
    )


def test_unreadable_manifests_record_no_metrics_and_the_write_lands(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manifests the bounded read refuses give `no_metrics` rows, and the write lands.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    monkeypatch
        Makes the manifest read fail.
    """

    def refuse(*args: object) -> list:
        """Fail as the bounded manifest read does.

        Parameters
        ----------
        *args
            Ignored.

        Raises
        ------
        ValidationError
            Always.
        """
        raise ValidationError("too many files")

    monkeypatch.setattr(health_record, "added_data_files", refuse)
    catalog.append(principal, _ORDERS, _rows([1, 2]))
    assert _signals(session) == [
        ("null_rate", None, "no_metrics", "warn"),
        ("range", None, "no_metrics", "warn"),
        ("schema", None, "ok", "warn"),
        ("volume", None, "no_history", "warn"),
    ]
    assert catalog.load_table(principal, _ORDERS).metadata.current_snapshot_id is not None


def test_a_snapshot_that_adds_no_files_records_volume_and_schema(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without added files there is no column to observe.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    monkeypatch
        Makes the manifest read find no file.
    """
    monkeypatch.setattr(health_record, "added_data_files", lambda *args: [])
    catalog.append(principal, _ORDERS, _rows([1]))
    assert {s for s, *_ in _signals(session)} == {"schema", "volume"}


def test_an_unexpected_error_records_no_metrics_and_the_write_lands(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bug in a signal records `no_metrics` for every commit signal and logs its type.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    monkeypatch
        Breaks the volume signal.
    caplog
        Captures the warning.
    """

    def broken(*args: object) -> None:
        """Fail like a bug.

        Parameters
        ----------
        *args
            Ignored.

        Raises
        ------
        RuntimeError
            Always.
        """
        raise RuntimeError("bug")

    monkeypatch.setattr(health_record, "volume_signal", broken)
    with caplog.at_level(logging.WARNING, logger="lakana_core.catalog.health_record"):
        catalog.append(principal, _ORDERS, _rows([1]))
    assert {(s, state) for s, _, state, _ in _signals(session)} == {
        (s, "no_metrics") for s in ("null_rate", "range", "schema", "volume")
    }
    assert (
        "The health signals of a commit to silver.orders could not be computed (RuntimeError)."
        in caplog.text
    )


def test_only_the_first_columns_up_to_the_cap_are_evaluated(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Columns past `MAX_HEALTH_COLUMNS` are not evaluated.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    monkeypatch
        Lowers the cap to one column.
    """
    monkeypatch.setattr("lakana_core.catalog.health_signals.MAX_HEALTH_COLUMNS", 1)
    catalog.append(principal, _ORDERS, _rows([1]))
    assert {c for _, c, *_ in _signals(session) if c is not None} == {"id"}


def test_the_history_is_the_newest_thirty_of_each_signal_and_column(
    catalog: CatalogService, session: Session
) -> None:
    """At most `HISTORY` observed values are read per signal and column.

    Parameters
    ----------
    catalog
        The catalog, which creates the table.
    session
        Database session.
    """
    _seed(session, "volume", {"added": 5, "total": 5}, count=35)
    _seed(session, "null_rate", {"nulls": 0, "values": 1, "rate": 0.0}, count=2, column="id")
    found = recent_observations(session, _table_id(session))
    assert (len(found["volume", None]), len(found["null_rate", "id"])) == (30, 2)


def test_the_history_reaches_back_only_twice_history_commits(
    catalog: CatalogService, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows older than the (2 x `HISTORY`)-th newest `schema` row are not ranked.

    Parameters
    ----------
    catalog
        The catalog, which creates the table.
    session
        Database session.
    monkeypatch
        Lowers `HISTORY` to three.
    """
    monkeypatch.setattr(health_record, "HISTORY", 3)
    _seed(session, "volume", {"added": 5, "total": 5}, count=3)
    _seed(session, "schema", {"columns": []}, count=6, first_day=2)
    _seed(session, "range", {"low": 1, "high": 2}, count=8, column="id")
    found = recent_observations(session, _table_id(session))
    assert len(found["volume", None]) == 1
    assert len(found["range", "id"]) == 3


@pytest.mark.parametrize(
    ("snapshot_id", "sequence_number"), [(2**63, 1), (1, 2**63), (-1, 1)], ids=["id", "seq", "neg"]
)
def test_numbers_outside_bigint_record_no_rows(
    snapshot_id: int, sequence_number: int, caplog: pytest.LogCaptureFixture
) -> None:
    """A snapshot id or sequence number a `BigInteger` cannot hold records nothing, and logs.

    Parameters
    ----------
    snapshot_id
        The snapshot's id.
    sequence_number
        Its sequence number.
    caplog
        Pytest's log capture.
    """
    rows = signal_rows(
        [(Observation("volume", None, "ok", {"added": 1, "total": 1}, None), "warn")],
        environment_id=uuid.uuid4(),
        table_id=uuid.uuid4(),
        snapshot_id=snapshot_id,
        sequence_number=sequence_number,
        now=_T0,
    )
    assert rows == []
    assert "out of range" in caplog.text


def test_the_history_is_ordered_by_sequence_number(
    catalog: CatalogService, session: Session
) -> None:
    """Order the baseline by snapshot sequence number, not by a time commits can share (M2).

    Parameters
    ----------
    catalog
        The catalog, which creates the table.
    session
        Database session.
    """
    table_id, environment_id = _table_id(session), _environment_id(session)
    for sequence in (1, 2, 3):
        session.add(
            TableHealthSignal(
                id=uuid.UUID(int=10 - sequence),
                environment_id=environment_id,
                table_id=table_id,
                signal="volume",
                column_name=None,
                snapshot_id=sequence,
                sequence_number=sequence,
                observed={"added": 1, "total": sequence},
                band=None,
                state="ok",
                effect="warn",
                observed_at=_T0,
            )
        )
    session.commit()
    found = recent_observations(session, table_id)
    assert [o["total"] for o in found["volume", None]] == [3, 2, 1]


def test_a_database_error_is_not_swallowed(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing history read fails the commit, as the commit itself would.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    monkeypatch
        Breaks the history read.
    """

    def broken(*args: object) -> None:
        """Fail like the database.

        Parameters
        ----------
        *args
            Ignored.

        Raises
        ------
        OperationalError
            Always.
        """
        raise OperationalError("SELECT", {}, Exception("gone"))

    catalog.append(principal, _ORDERS, _rows([1]))
    loaded = catalog.load_table(principal, _ORDERS)
    monkeypatch.setattr(health_record, "recent_observations", broken)
    with pytest.raises(OperationalError):
        commit_observations(
            session,
            table="silver.orders",
            table_id=_table_id(session),
            before=None,
            after=loaded.metadata,
            snapshots=[],
            file_io=catalog._storage.for_environment("dev").file_io,
            effects=HealthEffects.default(),
            files=[],
        )


def test_an_infinite_bound_never_reaches_the_stored_values(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A float column whose upper bound is infinite lands with only finite values stored.

    Postgres `json` refuses Infinity and NaN at flush, after the pointer swap.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    monkeypatch
        Makes the manifest read return a file with an infinite bound.
    """
    prices = TableRef("dev", _SILVER, "prices")
    catalog.create_table(principal, prices, schema=Schema(NestedField(1, "price", DoubleType())))
    infinite = DataFile.from_args(
        record_count=2,
        value_counts={1: 2},
        null_value_counts={1: 0},
        lower_bounds={1: to_bytes(DoubleType(), 1.0)},
        upper_bounds={1: to_bytes(DoubleType(), float("inf"))},
    )
    monkeypatch.setattr(health_record, "added_data_files", lambda *args: [infinite])
    catalog.append(principal, prices, pa.table({"price": pa.array([1.0, float("inf")])}))
    rows = session.scalars(select(TableHealthSignal)).all()
    assert {(r.signal, r.column_name) for r in rows} >= {("null_rate", "price"), ("volume", None)}
    for row in rows:
        json.dumps([row.observed, row.band], allow_nan=False)
    assert catalog.load_table(principal, prices).metadata.current_snapshot_id is not None


def test_the_manifest_read_runs_on_the_field_id_checks_clock(
    catalog: CatalogService, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read's deadline is `HEALTH_DEADLINE_SECONDS` after `field_ids._clock`.

    Parameters
    ----------
    catalog
        The catalog.
    principal
        The writer.
    monkeypatch
        Fixes the clock and records the reads.
    """
    monkeypatch.setattr(field_ids, "_clock", lambda: 100.0)
    calls = _recording(monkeypatch)
    catalog.append(principal, _ORDERS, _rows([1]))
    assert [call[4] for call in calls] == [100.0 + health_record.HEALTH_DEADLINE_SECONDS]


def test_a_compaction_records_only_the_schema(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `replace` snapshot rewrites rows without changing them: only `schema` is recorded.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    monkeypatch
        Records the manifest reads.
    """
    catalog.append(principal, _ORDERS, _rows([1, 2]))
    metadata = catalog.load_table(principal, _ORDERS).metadata
    snapshot = metadata.snapshots[-1]
    compacted = snapshot.model_copy(update={"summary": Summary(Operation.REPLACE)})
    after = metadata.model_copy(update={"snapshots": [compacted]})
    calls = _recording(monkeypatch)
    pairs = commit_observations(
        session,
        table="silver.orders",
        table_id=_table_id(session),
        before=metadata,
        after=after,
        snapshots=[compacted],
        file_io=catalog._storage.for_environment("dev").file_io,
        effects=HealthEffects.default(),
    )
    assert [(o.signal, o.state) for o, _ in pairs] == [("schema", "ok")]
    assert calls == []


def test_a_history_of_extreme_bounds_keeps_the_writes_landing(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    """Eight snapshots holding `-DBL_MAX` make the range band overflow: `no_metrics`, not -inf.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    """
    prices = TableRef("dev", _SILVER, "prices")
    catalog.create_table(principal, prices, schema=Schema(NestedField(1, "price", DoubleType())))
    for _ in range(9):
        catalog.append(principal, prices, pa.table({"price": pa.array([-sys.float_info.max, 5.0])}))
    rows = session.scalars(
        select(TableHealthSignal).order_by(TableHealthSignal.observed_at, TableHealthSignal.id)
    ).all()
    for row in rows:
        json.dumps([row.observed, row.band], allow_nan=False)
    ranges = [r for r in rows if r.signal == "range"]
    assert ("no_metrics", {"reason": "not_finite"}) in [(r.state, r.observed) for r in ranges]


@pytest.mark.parametrize(
    ("observed", "band"),
    [
        ({"lower": float("inf")}, None),
        ({"lower": 1.0}, {"low": float("nan")}),
        ({"at": object()}, None),
    ],
)
def test_a_value_json_cannot_store_becomes_no_metrics(observed: dict, band: dict | None) -> None:
    """An observation whose values JSON cannot store is kept as `no_metrics` without values.

    Parameters
    ----------
    observed
        The observed values.
    band
        The band.
    """
    rows = signal_rows(
        [(Observation("range", "price", "fired", observed, band), "block")],
        environment_id=uuid.uuid4(),
        table_id=uuid.uuid4(),
        snapshot_id=1,
        sequence_number=1,
        now=_T0,
    )
    assert [(r.signal, r.column_name, r.state, r.effect, r.observed, r.band) for r in rows] == [
        ("range", "price", "no_metrics", "block", None, None)
    ]


def test_the_tables_effects_switch_signals_off_and_mark_block(
    catalog: CatalogService, session: Session, principal: PrincipalRef
) -> None:
    """The row's stored effects decide what each recorded signal does.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    """
    table = session.get(Table, _table_id(session))
    assert table is not None
    table.health_effects = {"range": "off", "volume": "block", "null_rate.status": "off"}
    session.commit()
    catalog.append(principal, _ORDERS, _rows([1, 2]))
    assert _signals(session) == [
        ("null_rate", "id", "no_history", "warn"),
        ("null_rate", "status", "off", "off"),
        ("range", None, "off", "off"),
        ("schema", None, "ok", "warn"),
        ("volume", None, "no_history", "block"),
    ]


@pytest.mark.parametrize(
    ("stored", "volume"),
    [
        ({"volume": "loud", "range": "off"}, ("volume", None, "no_history", "warn")),
        (["volume"], ("volume", None, "no_history", "warn")),
    ],
)
def test_garbled_stored_effects_read_as_warn_and_are_logged(
    catalog: CatalogService,
    session: Session,
    principal: PrincipalRef,
    caplog: pytest.LogCaptureFixture,
    stored: object,
    volume: tuple[str, None, str, str],
) -> None:
    """Effects the code does not understand never fail the commit: they warn, and say so.

    Parameters
    ----------
    catalog
        The catalog.
    session
        Database session.
    principal
        The writer.
    caplog
        Captures the log.
    stored
        What the row holds: a map with an unknown effect, or not a map at all.
    volume
        The volume signal recorded.
    """
    table = session.get(Table, _table_id(session))
    assert table is not None
    table.health_effects = cast("dict[str, str]", stored)
    session.commit()
    with caplog.at_level(logging.WARNING, logger="lakana_core.catalog.service"):
        catalog.append(principal, _ORDERS, _rows([1, 2]))
    assert volume in _signals(session)
    assert [r.getMessage() for r in caplog.records if "health" in r.getMessage()] == [
        "silver.orders: its stored health effects are not all understood; "
        "the ones that are not read as warn"
    ]
