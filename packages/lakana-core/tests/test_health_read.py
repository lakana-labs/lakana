"""Reading table health signals, and the values a masked or filtered reader may not see."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from core_test_helpers import ActionAllowList, _prepared_catalog
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table.metadata import TableMetadata, TableMetadataV2
from pyiceberg.table.sorting import SortOrder
from pyiceberg.types import DoubleType, LongType, NestedField
from sqlalchemy import insert, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lakana_core.auth import Action, PrincipalRef
from lakana_core.catalog import health_read, service
from lakana_core.catalog.health_read import (
    SignalView,
    Visibility,
    current_signals,
    signal_history,
    visibility_for,
)
from lakana_core.catalog.service import CatalogService
from lakana_core.errors import PermissionDenied, StorageUnavailableError, ValidationError
from lakana_core.identifiers import NamespacePath, TableRef
from lakana_core.models import Namespace, Table, TableHealthSignal
from lakana_core.pagination import Page
from lakana_core.policy.renames import PolicyColumns
from lakana_core.policy.resolver import ResolvedPolicy

_SILVER = NamespacePath.from_parts(["silver"])
_ORDERS = TableRef("dev", _SILVER, "orders")
_NOW = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
_NULLS = {"nulls": 38, "values": 100, "rate": 0.38}
_RATE_BAND = {"low": -0.02, "high": 0.02, "median": 0.0}
_SCHEMA = {"columns": [], "added": [], "dropped": [], "retyped": []}
_PRICE_MASK = ResolvedPolicy(
    column_masks={"price": "NULL"},
    policies=[PolicyColumns(columns=frozenset({"price"}), schema_id=0)],
)


def _catalog(session: Session, tmp_path: Path, *allowed: Action) -> CatalogService:
    """Build a catalog that permits only some actions, with `silver.orders` present.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    *allowed
        The permitted actions.

    Returns
    -------
    CatalogService
        The catalog.
    """
    return _prepared_catalog(
        session, tmp_path, ActionAllowList(*allowed), namespaces=[_SILVER], tables=[_ORDERS]
    )


def _add(
    session: Session,
    signal: str,
    state: str,
    *,
    column: str | None = None,
    observed: dict | None = None,
    band: dict | None = None,
    minutes: int = 0,
    sequence: int | None = None,
    snapshot: int = 4711,
    row_id: uuid.UUID | None = None,
) -> TableHealthSignal:
    """Record one signal of `silver.orders`, `minutes` after `_NOW`.

    Parameters
    ----------
    session
        Database session; commits.
    signal
        The signal.
    state
        Its state.
    column
        Its column.
    observed
        Its observed value.
    band
        Its band.
    minutes
        Minutes after `_NOW`.
    sequence
        The snapshot's sequence number; by default one that orders as `minutes` does.
    snapshot
        The snapshot observed.
    row_id
        The row's id; a random one by default.

    Returns
    -------
    TableHealthSignal
        The row.
    """
    table_id, environment_id = session.execute(
        select(Table.id, Namespace.environment_id)
        .join(Namespace, Namespace.id == Table.namespace_id)
        .where(Table.name == "orders")
    ).one()
    row = TableHealthSignal(
        id=row_id or uuid.uuid4(),
        environment_id=environment_id,
        table_id=table_id,
        signal=signal,
        column_name=column,
        snapshot_id=snapshot,
        sequence_number=100_000 + minutes if sequence is None else sequence,
        observed=observed,
        band=band,
        state=state,
        effect="warn",
        observed_at=_NOW + timedelta(minutes=minutes),
    )
    session.add(row)
    session.commit()
    return row


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


def _no_metadata() -> TableMetadata | None:
    """Stand for a table without Iceberg metadata.

    Returns
    -------
    None
        Always.
    """
    return None


def _schemas(*schemas: dict[int, str]) -> TableMetadata:
    """Build the metadata of a table with an `id` column and these other columns.

    Parameters
    ----------
    *schemas
        Each schema's columns beyond `id`, field id to name; the last is current.

    Returns
    -------
    TableMetadata
        The metadata, schema ids counted from 0.
    """
    built = [
        Schema(
            NestedField(1, "id", LongType()),
            *(NestedField(i, name, DoubleType()) for i, name in columns.items()),
            schema_id=n,
        )
        for n, columns in enumerate(schemas)
    ]
    return TableMetadataV2(
        location="file:///orders",
        last_column_id=max(i for columns in schemas for i in columns),
        schemas=built,
        current_schema_id=len(built) - 1,
        partition_specs=[PartitionSpec()],
        sort_orders=[SortOrder()],
    )


def _renamed_price() -> TableMetadata:
    """Build the metadata of a table whose `price` column was renamed to `amount`.

    Returns
    -------
    TableMetadata
        Schema 0 has `price` as field 2, the current schema 1 has `amount` as field 2.
    """
    return _schemas({2: "price"}, {2: "amount"})


def _salary_to_pay() -> TableMetadata:
    """Build the metadata of a table whose `salary` was renamed to `pay`, then given a new `salary`.

    Returns
    -------
    TableMetadata
        Field 2 is `salary`, then `pay`; field 3, the new `salary`, is current only.
    """
    return _schemas({2: "salary"}, {2: "pay"}, {2: "pay", 3: "salary"})


def _mask_on(column: str, schema_id: int) -> ResolvedPolicy:
    """Build the policy of a reader masked on one column.

    Parameters
    ----------
    column
        The masked column.
    schema_id
        The schema the mask was written against.

    Returns
    -------
    ResolvedPolicy
        The policy.
    """
    return ResolvedPolicy(
        column_masks={column: "NULL"},
        policies=[PolicyColumns(columns=frozenset({column}), schema_id=schema_id)],
    )


def _answer_with(monkeypatch: pytest.MonkeyPatch, policy: ResolvedPolicy) -> None:
    """Make every policy lookup of `health_read` give `policy`.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.
    policy
        The policy.
    """

    def resolve(self: object, *args: object, **kwargs: object) -> ResolvedPolicy:
        """Give the fixed policy.

        Parameters
        ----------
        self
            The resolver.
        *args
            Ignored.
        **kwargs
            Ignored.

        Returns
        -------
        ResolvedPolicy
            The policy.
        """
        return policy

    monkeypatch.setattr(health_read.PolicyResolver, "resolve", resolve)


def _visibility(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    load: Callable[[], TableMetadata | None],
) -> Visibility:
    """Ask `visibility_for` about `silver.orders`.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    load
        Reads the table's current metadata.

    Returns
    -------
    Visibility
        The answer.
    """
    _catalog(session, tmp_path)
    table_id = session.scalars(select(Table.id)).one()
    return visibility_for(session, principal, _ORDERS, table_id, metadata=load)


def test_the_current_state_is_the_newest_row_with_the_last_fired_alert(
    session: Session, tmp_path: Path
) -> None:
    """Show the newest row of each key, with the alert and the time of its last firing.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)
    fired = _add(session, "null_rate", "fired", column="status", observed=_NULLS, band=_RATE_BAND)
    _add(
        session,
        "null_rate",
        "ok",
        column="status",
        observed={"nulls": 0, "values": 9, "rate": 0.0},
        minutes=5,
    )
    _add(session, "volume", "no_history", observed={"added": 3, "total": 3}, minutes=5)
    shown = current_signals(session, fired.table_id, "silver.orders", Visibility())
    assert [(s.signal, s.column, s.state) for s in shown] == [
        ("volume", None, "no_history"),
        ("null_rate", "status", "ok"),
    ]
    status = shown[1]
    assert status.last_fired_at == fired.observed_at
    assert status.alert is not None
    assert status.alert.startswith("silver.orders: null rate of status 38.0 %")
    assert shown[0].alert is None and shown[0].last_fired_at is None


def test_a_signal_switched_off_replaces_its_column_rows(session: Session, tmp_path: Path) -> None:
    """Show only the off row of a signal the newest commit switched off (F10 a).

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)
    first = _add(session, "schema", "ok", observed=_SCHEMA)
    _add(session, "volume", "ok", observed={"added": 3, "total": 3})
    _add(
        session, "range", "fired", column="id", observed={"lower": 1, "upper": 9, "kind": "integer"}
    )
    for signal, state, column in (
        ("schema", "ok", None),
        ("volume", "ok", None),
        ("range", "off", None),
    ):
        _add(
            session,
            signal,
            state,
            column=column,
            observed=_SCHEMA if signal == "schema" else None,
            minutes=5,
        )
    shown = current_signals(session, first.table_id, "silver.orders", Visibility())
    assert [(s.signal, s.column, s.state) for s in shown] == [
        ("volume", None, "ok"),
        ("range", None, "off"),
        ("schema", None, "ok"),
    ]


def test_a_dropped_column_no_longer_shows_its_last_state(session: Session, tmp_path: Path) -> None:
    """Leave out a column the newest commit recorded nothing for (F10 b).

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)
    first = _add(session, "volume", "ok", observed={"added": 3, "total": 3})
    _add(session, "null_rate", "fired", column="dropped", observed=_NULLS, band=_RATE_BAND)
    _add(session, "volume", "ok", observed={"added": 3, "total": 6}, minutes=5)
    _add(
        session,
        "null_rate",
        "ok",
        column="id",
        observed={"nulls": 0, "values": 3, "rate": 0.0},
        minutes=5,
    )
    shown = current_signals(session, first.table_id, "silver.orders", Visibility())
    assert [(s.signal, s.column) for s in shown] == [("volume", None), ("null_rate", "id")]


def test_a_compaction_keeps_the_column_signals_of_the_commit_before(
    session: Session, tmp_path: Path
) -> None:
    """Keep the signals of the commit before a compaction, which records only the schema.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)
    first = _add(session, "schema", "ok", observed=_SCHEMA)
    _add(session, "volume", "ok", observed={"added": 3, "total": 3})
    _add(session, "null_rate", "ok", column="id", observed={"nulls": 0, "values": 3, "rate": 0.0})
    compaction = _add(session, "schema", "ok", observed=_SCHEMA, minutes=5)
    _add(session, "freshness", "ok", observed={"age_hours": 1.0}, minutes=7)
    shown = current_signals(session, first.table_id, "silver.orders", Visibility())
    assert [(s.signal, s.column, s.observed_at) for s in shown] == [
        ("freshness", None, _NOW + timedelta(minutes=7)),
        ("volume", None, _NOW),
        ("null_rate", "id", _NOW),
        ("schema", None, compaction.observed_at),
    ]


def test_a_commit_without_column_rows_keeps_the_column_signals_before(
    session: Session, tmp_path: Path
) -> None:
    """Keep the column signals of the commit before one that wrote none, as a delete does (M1).

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)
    first = _add(session, "volume", "ok", observed={"added": 3, "total": 3})
    _add(session, "range", "ok", column="id", observed={"lower": 1, "upper": 3, "kind": "integer"})
    _add(session, "volume", "ok", observed={"added": 0, "total": 2}, minutes=5)
    shown = current_signals(session, first.table_id, "silver.orders", Visibility())
    assert [(s.signal, s.column, s.observed_at) for s in shown] == [
        ("volume", None, _NOW + timedelta(minutes=5)),
        ("range", "id", _NOW),
    ]


def test_two_commits_in_the_same_second_never_mix(session: Session, tmp_path: Path) -> None:
    """Order commits by snapshot sequence number, not by a time two commits can share (M2).

    The older commit's rows have the larger ids, so ordering by time, then id, would take its
    volume and add its column `b` to the newer commit's columns.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)

    def commit(sequence: int, total: int, columns: list[str], *, high_ids: bool) -> None:
        """Record one commit's rows, all at `_NOW`.

        Parameters
        ----------
        sequence
            Its sequence number, also its snapshot.
        total
            Its row count.
        columns
            The columns it observed.
        high_ids
            Whether its row ids are the largest there are, rather than the smallest.
        """
        kinds = [("volume", None), ("schema", None), *(("null_rate", c) for c in columns)]
        for n, (signal, column) in enumerate(kinds):
            _add(
                session,
                signal,
                "ok",
                column=column,
                observed={"added": 1, "total": total} if signal == "volume" else _SCHEMA,
                sequence=sequence,
                snapshot=sequence,
                row_id=uuid.UUID(int=2**128 - 1 - n if high_ids else n + 1),
            )

    commit(1, 1, ["a", "b"], high_ids=True)
    commit(2, 2, ["a"], high_ids=False)
    shown = current_signals(session, _table_id(session), "silver.orders", Visibility())
    assert [(s.signal, s.column, s.snapshot_id) for s in shown] == [
        ("volume", None, 2),
        ("null_rate", "a", 2),
        ("schema", None, 2),
    ]
    assert shown[0].observed == {"added": 1, "total": 2}


def _vm_steps(session: Session, call: Callable[[], object]) -> int:
    """Count the SQLite virtual machine steps a call takes, in thousands.

    Parameters
    ----------
    session
        Database session on SQLite.
    call
        What to measure.

    Returns
    -------
    int
        How often SQLite's progress handler ran, once per thousand steps.
    """
    raw = session.connection().connection.driver_connection
    steps = [0]

    def step() -> int:
        """Count one thousand steps.

        Returns
        -------
        int
            0, which lets the statement go on.
        """
        steps[0] += 1
        return 0

    raw.set_progress_handler(step, 1000)
    try:
        call()
    finally:
        raw.set_progress_handler(None, 0)
    return steps[0]


def _history(session: Session, table_id: uuid.UUID, commits: range, columns: int) -> None:
    """Record many commits of unremarkable signals, one minute apart, `_NOW` being commit 0.

    Parameters
    ----------
    session
        Database session; commits.
    table_id
        The table.
    commits
        The commits' numbers, which are their sequence numbers and snapshots.
    columns
        How many columns each commit observed.
    """
    environment_id = session.scalars(select(Namespace.environment_id)).first()

    def row(c: int, signal: str, column: str | None, observed: dict) -> dict:
        """Give one row's values.

        Parameters
        ----------
        c
            The commit.
        signal
            The signal.
        column
            Its column.
        observed
            The observed values.

        Returns
        -------
        dict
            The row.
        """
        return {
            "id": uuid.uuid4(),
            "environment_id": environment_id,
            "table_id": table_id,
            "signal": signal,
            "column_name": column,
            "snapshot_id": 1000 + c,
            "sequence_number": 1000 + c,
            "observed": observed,
            "band": None,
            "state": "ok",
            "effect": "warn",
            "observed_at": _NOW + timedelta(minutes=c),
        }

    rows = []
    for c in commits:
        rows += [row(c, "volume", None, {"added": 1, "total": c}), row(c, "schema", None, _SCHEMA)]
        for k in range(columns):
            rows.append(row(c, "null_rate", f"c{k}", {"nulls": 0, "values": 1, "rate": 0.0}))
            rows.append(row(c, "range", f"c{k}", {"lower": 0, "upper": 1, "kind": "integer"}))
    session.execute(insert(TableHealthSignal), rows)
    session.commit()


def test_the_current_state_does_not_read_the_whole_history(
    session: Session, tmp_path: Path
) -> None:
    """Take about the same work for a table with twenty times the history (M3).

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)
    table_id = _table_id(session)
    _add(
        session,
        "null_rate",
        "fired",
        column="c0",
        observed=_NULLS,
        band=_RATE_BAND,
        minutes=-1,
        sequence=999,
        snapshot=999,
    )
    _history(session, table_id, range(0, 10), columns=20)

    def read() -> list[SignalView]:
        """Read the current state.

        Returns
        -------
        list of SignalView
            It.
        """
        return current_signals(session, table_id, "silver.orders", Visibility())

    short = read()
    few = _vm_steps(session, read)
    _history(session, table_id, range(-400, -200), columns=20)
    assert read() == short
    many = _vm_steps(session, read)
    assert many < 2 * few + 10, (few, many)
    assert next(s for s in short if s.column == "c0" and s.signal == "null_rate").last_fired_at


def test_a_damaged_stored_value_gives_no_alert(session: Session, tmp_path: Path) -> None:
    """Give no alert when a stored value no longer fits its text.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)
    row = _add(session, "volume", "fired", observed={"rows": "many"}, band=_RATE_BAND)
    (shown,) = current_signals(session, row.table_id, "silver.orders", Visibility())
    assert (shown.state, shown.alert) == ("fired", None)


def test_a_mask_hides_its_column_whatever_the_letter_case(session: Session, tmp_path: Path) -> None:
    """Leave out a masked column's values, matching its name case-insensitively.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)
    row = _add(session, "null_rate", "fired", column="Status", observed=_NULLS, band=_RATE_BAND)
    _add(
        session,
        "volume",
        "fired",
        observed={"added": 3, "total": 3},
        band={"low": 900.0, "high": 1100.0, "median": 1000.0},
    )
    shown = current_signals(
        session, row.table_id, "silver.orders", Visibility(masked=frozenset({"status"}))
    )
    by_signal = {s.signal: s for s in shown}
    masked = by_signal["null_rate"]
    assert (masked.state, masked.observed, masked.band, masked.alert, masked.values_hidden) == (
        "fired",
        None,
        None,
        None,
        True,
    )
    assert masked.last_fired_at == row.observed_at and masked.snapshot_id == 4711
    assert by_signal["volume"].observed == {"added": 3, "total": 3}
    assert by_signal["volume"].alert is not None


def test_a_row_filter_hides_every_column_value_and_keeps_volume_and_schema() -> None:
    """Hide every column signal under a row filter, never volume, freshness or schema."""
    filtered = Visibility(filtered=True)
    assert filtered.hides("null_rate", "a") and filtered.hides("range", "b")
    assert filtered.hides("range", None)
    assert not any(filtered.hides(s, None) for s in ("volume", "freshness", "schema"))
    assert not Visibility(masked=frozenset({"a"})).hides("null_rate", None)


def test_visibility_reads_the_readers_policies(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fold the reader's masks and note their row filter.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """
    _answer_with(
        monkeypatch, ResolvedPolicy(row_filters=["region = 'eu'"], column_masks={"Price": "NULL"})
    )
    assert _visibility(session, tmp_path, principal, _renamed_price) == Visibility(
        masked=frozenset({"price"}), filtered=True
    )


def test_an_unreadable_policy_hides_every_value(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hide every value when the reader's policies cannot be read.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """

    def unreadable(self: object, *args: object, **kwargs: object) -> ResolvedPolicy:
        """Fail as a stored policy that no longer parses does.

        Parameters
        ----------
        self
            The resolver.
        *args
            Ignored.
        **kwargs
            Ignored.

        Raises
        ------
        ValidationError
            Always.
        """
        raise ValidationError("A row filter or column mask stored on this table could not be read.")

    monkeypatch.setattr(health_read.PolicyResolver, "resolve", unreadable)
    assert _visibility(session, tmp_path, principal, _no_metadata).filtered


def test_a_mask_on_a_renamed_column_hides_every_value(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hide every value when a masked column was renamed since its mask was written (F11).

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """
    _answer_with(monkeypatch, _PRICE_MASK)
    seen = _visibility(session, tmp_path, principal, _renamed_price)
    assert seen.filtered
    assert seen.hides("null_rate", "amount") and seen.hides("range", "amount")


def test_a_mask_without_a_rename_hides_its_columns_every_name(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hide a masked column under every name its field had, and names the table no longer has.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """
    _answer_with(monkeypatch, _mask_on("Amount", 1))
    seen = _visibility(session, tmp_path, principal, _renamed_price)
    assert seen == Visibility(
        masked=frozenset({"amount", "price"}), columns=frozenset({"id", "amount"})
    )
    assert seen.hides("range", "price") and seen.hides("null_rate", "AMOUNT")
    assert seen.hides("null_rate", "ghost") and not seen.hides("null_rate", "id")
    assert not seen.hides("range", None)


def test_a_mask_keeps_to_its_own_field(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widen a mask only by its own field's names: a newer field, or a name the table lacks.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """
    _answer_with(monkeypatch, _mask_on("salary", 2))
    seen = _visibility(session, tmp_path, principal, _salary_to_pay)
    assert (seen.filtered, seen.masked) == (False, frozenset({"salary"}))
    assert not seen.hides("null_rate", "pay")
    _answer_with(monkeypatch, _mask_on("ghost", 1))
    seen = _visibility(session, tmp_path, principal, _renamed_price)
    assert (seen.filtered, seen.masked) == (False, frozenset({"ghost"}))


def test_a_mask_on_a_table_without_metadata_hides_every_value(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hide every value when a mask applies and no schema says which names are its column's.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """
    _answer_with(monkeypatch, _mask_on("amount", 1))
    assert _visibility(session, tmp_path, principal, _no_metadata).filtered


def test_a_mask_written_after_a_rename_hides_the_old_name(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hide `salary`'s rows from a reader masked on `pay`, the same field under its new name.

    The new column that reuses the name `salary` is hidden too, as its rows cannot be told apart.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """
    _answer_with(monkeypatch, _mask_on("pay", 2))
    seen = _visibility(session, tmp_path, principal, _salary_to_pay)
    assert not seen.filtered
    assert seen.hides("null_rate", "salary") and seen.hides("range", "pay")
    assert not seen.hides("range", "id")


def test_a_mask_on_a_name_that_casefolds_apart_from_its_lower_case_hides_the_old_name(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hide `x`'s old rows from a reader masked on `Maß`, the same field renamed (I1).

    `"Maß".casefold()` is ``mass`` and its `lower()` ``maß``: the field is found by comparing
    case-folded names, so its earlier name `x`, reused by a new column, stays hidden.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """
    _answer_with(monkeypatch, _mask_on("Maß", 1))
    seen = _visibility(session, tmp_path, principal, lambda: _schemas({2: "x"}, {2: "Maß", 3: "x"}))
    assert not seen.filtered
    assert seen.hides("range", "x") and seen.hides("null_rate", "MASS")
    assert not seen.hides("range", "id")


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("gone"), PermissionError("denied"), ValueError("not a metadata file")],
    ids=["missing", "refused", "corrupt"],
)
def test_metadata_that_cannot_be_read_hides_every_value(
    session: Session,
    tmp_path: Path,
    principal: PrincipalRef,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Hide every value when the metadata read fails for any reason but the database (I2).

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    error
        What the read raises.
    """

    def broken() -> TableMetadata | None:
        """Fail as the metadata read does.

        Raises
        ------
        Exception
            `error`, always.
        """
        raise error

    _answer_with(monkeypatch, _PRICE_MASK)
    assert _visibility(session, tmp_path, principal, broken).filtered


def test_a_database_error_while_reading_the_metadata_is_not_hidden(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Let a database error raised while reading the metadata propagate.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """

    def database_down() -> TableMetadata | None:
        """Fail as a lost database connection does.

        Raises
        ------
        OperationalError
            Always.
        """
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    _answer_with(monkeypatch, _PRICE_MASK)
    with pytest.raises(OperationalError):
        _visibility(session, tmp_path, principal, database_down)


def test_unreachable_metadata_hides_every_value(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hide every value when the rename cannot be ruled out because storage did not answer.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """

    def unreachable() -> TableMetadata | None:
        """Fail as storage that does not answer does.

        Raises
        ------
        StorageUnavailableError
            Always.
        """
        raise StorageUnavailableError("The storage of environment dev could not be reached.")

    _answer_with(monkeypatch, _PRICE_MASK)
    assert _visibility(session, tmp_path, principal, unreachable).filtered


def test_the_metadata_is_read_only_for_a_mask_without_a_row_filter(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never read the metadata when no mask applies, or a row filter hides everything anyway.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """

    def never() -> TableMetadata | None:
        """Fail the test if called.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError("the metadata was read")

    assert _visibility(session, tmp_path, principal, never) == Visibility()
    _answer_with(monkeypatch, ResolvedPolicy(row_filters=["id > 0"], column_masks={"id": "0"}))
    assert _visibility(session, tmp_path, principal, never).filtered


def test_the_history_pages_newest_first_and_filters(session: Session, tmp_path: Path) -> None:
    """Page the history newest first within the window, by signal or column.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)
    old = _add(session, "volume", "ok", observed={"added": 1, "total": 1}, minutes=-60 * 24 * 40)
    for minute in (1, 2, 3):
        _add(session, "volume", "ok", observed={"added": minute, "total": minute}, minutes=minute)
    _add(
        session,
        "range",
        "ok",
        column="id",
        observed={"lower": 1, "upper": 2, "kind": "integer"},
        minutes=4,
    )
    since = _NOW - timedelta(days=30)

    def page(
        signal: str | None, column: str | None, limit: int, cursor: str | None
    ) -> Page[SignalView]:
        """Read one page of the history.

        Parameters
        ----------
        signal
            Only this signal.
        column
            Only this column.
        limit
            The most rows.
        cursor
            Where the previous page ended.

        Returns
        -------
        Page of SignalView
            The page.
        """
        return signal_history(
            session,
            old.table_id,
            "silver.orders",
            Visibility(),
            since=since,
            signal=signal,
            column=column,
            limit=limit,
            cursor=cursor,
        )

    first = page("volume", None, 2, None)
    assert [(s.observed or {}).get("added") for s in first.items] == [3, 2]
    assert first.next_cursor is not None
    second = page("volume", None, 2, first.next_cursor)
    assert [(s.observed or {}).get("added") for s in second.items] == [1]
    assert second.next_cursor is None
    assert [s.signal for s in page(None, "id", 10, None).items] == ["range"]


def test_the_history_shows_a_fired_rows_own_alert_unless_hidden(
    session: Session, tmp_path: Path
) -> None:
    """Give each fired row of the history its own alert, and none where its values are hidden.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    """
    _catalog(session, tmp_path)
    row = _add(session, "null_rate", "fired", column="status", observed=_NULLS, band=_RATE_BAND)
    _add(session, "null_rate", "ok", column="status", observed=_NULLS, minutes=1)
    since = _NOW - timedelta(days=1)
    seen = signal_history(
        session,
        row.table_id,
        "silver.orders",
        Visibility(),
        since=since,
        signal=None,
        column=None,
        limit=10,
        cursor=None,
    )
    assert [(s.state, s.alert is None, s.last_fired_at) for s in seen.items] == [
        ("ok", True, None),
        ("fired", False, None),
    ]
    hidden = signal_history(
        session,
        row.table_id,
        "silver.orders",
        Visibility(filtered=True),
        since=since,
        signal=None,
        column=None,
        limit=10,
        cursor=None,
    )
    assert [(s.observed, s.alert, s.values_hidden) for s in hidden.items] == [
        (None, None, True),
        (None, None, True),
    ]


def test_the_history_route_needs_read_table(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Refuse the history to a reader who may not read the table.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    """
    with pytest.raises(PermissionDenied):
        _catalog(session, tmp_path).table_health_signals(
            principal, _ORDERS, days=30, signal=None, column=None, limit=10, cursor=None, now=_NOW
        )


def test_an_unknown_signal_is_refused_with_the_signals(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Refuse a signal that does not exist, naming the ones that do.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    """
    catalog = _catalog(session, tmp_path, Action.READ_TABLE)
    with pytest.raises(ValidationError) as refused:
        catalog.table_health_signals(
            principal, _ORDERS, days=30, signal="rows", column=None, limit=10, cursor=None, now=_NOW
        )
    assert str(refused.value) == (
        "There is no health signal `rows`; use freshness, volume, null_rate, range or schema."
    )


def test_table_health_shows_the_signals(
    session: Session, tmp_path: Path, principal: PrincipalRef
) -> None:
    """Show the current signals in the health, and the history on its own route.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    """
    catalog = _catalog(session, tmp_path, Action.READ_TABLE)
    _add(session, "schema", "ok", observed=_SCHEMA)
    shown = catalog.table_health(principal, _ORDERS, days=30, now=_NOW)
    assert [s.signal for s in shown.signals] == ["schema"]
    page = catalog.table_health_signals(
        principal,
        _ORDERS,
        days=30,
        signal="schema",
        column=None,
        limit=10,
        cursor=None,
        now=_NOW + timedelta(minutes=1),
    )
    assert len(page.items) == 1


def test_table_health_hides_a_renamed_masked_columns_values(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave out `amount`'s values for a reader masked on `price`, renamed to `amount` (F11).

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """
    catalog = _catalog(session, tmp_path, Action.READ_TABLE)
    _add(session, "volume", "ok", observed={"added": 3, "total": 3})
    _add(session, "null_rate", "fired", column="amount", observed=_NULLS, band=_RATE_BAND)
    _answer_with(monkeypatch, _PRICE_MASK)
    monkeypatch.setattr(service, "read_metadata", lambda file_io, location: _renamed_price())
    shown = {
        s.signal: s for s in catalog.table_health(principal, _ORDERS, days=30, now=_NOW).signals
    }
    assert (shown["null_rate"].observed, shown["null_rate"].alert) == (None, None)
    assert shown["null_rate"].values_hidden and not shown["volume"].values_hidden
    page = catalog.table_health_signals(
        principal,
        _ORDERS,
        days=30,
        signal="null_rate",
        column="amount",
        limit=10,
        cursor=None,
        now=_NOW,
    )
    assert [s.band for s in page.items] == [None]


def test_table_health_hides_a_column_renamed_before_its_mask(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave out the rows stored as `salary` for a reader masked on `pay`, its new name (C1).

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """
    catalog = _catalog(session, tmp_path, Action.READ_TABLE)
    _add(session, "volume", "ok", observed={"added": 3, "total": 3})
    _add(session, "range", "fired", column="salary", observed=_NULLS, band=_RATE_BAND)
    monkeypatch.setattr(
        service, "read_metadata", lambda file_io, location: _schemas({2: "salary"}, {2: "pay"})
    )
    shown = catalog.table_health(principal, _ORDERS, days=30, now=_NOW).signals
    assert [(s.column, s.observed is None) for s in shown] == [(None, False), ("salary", False)]
    _answer_with(monkeypatch, _mask_on("pay", 1))
    (_, salary) = catalog.table_health(principal, _ORDERS, days=30, now=_NOW).signals
    assert (salary.observed, salary.band, salary.alert, salary.values_hidden) == (
        None,
        None,
        None,
        True,
    )
    page = catalog.table_health_signals(
        principal, _ORDERS, days=30, signal=None, column="salary", limit=10, cursor=None, now=_NOW
    )
    assert [(s.observed, s.band, s.values_hidden) for s in page.items] == [(None, None, True)]


def test_table_health_hides_masked_values_of_a_table_without_metadata(
    session: Session, tmp_path: Path, principal: PrincipalRef, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hide the values of a masked reader when the table has no metadata to judge names by.

    Parameters
    ----------
    session
        Database session.
    tmp_path
        Storage root.
    principal
        The reader.
    monkeypatch
        Pytest's monkeypatch.
    """
    catalog = _catalog(session, tmp_path, Action.READ_TABLE)
    _add(session, "volume", "ok", observed={"added": 3, "total": 3})
    _add(session, "null_rate", "fired", column="amount", observed=_NULLS, band=_RATE_BAND)
    session.execute(update(Table).values(metadata_location=None))
    _answer_with(monkeypatch, _PRICE_MASK)
    (_, shown) = catalog.table_health(principal, _ORDERS, days=30, now=_NOW).signals
    assert shown.observed is None and shown.values_hidden
