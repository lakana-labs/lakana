"""The one-word health of a table: its rules, its stored key and its backfill (M7 part 3a)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from home_test_helpers import check_result, deployed_job, environment, run_of
from sqlalchemy import delete, event, select, update
from sqlalchemy.orm import Session

from lakana_core.catalog import health_summary
from lakana_core.catalog.health import CheckHealth
from lakana_core.catalog.health_summary import (
    HEALTH_ITEMS,
    HEALTH_TEXT,
    HealthSummary,
    backfill_health_keys,
    failing_reference_checks,
    refresh_health_key,
    shown_health,
    summarize,
)
from lakana_core.models import (
    CheckOutcome,
    Namespace,
    Table,
    TableHealthSignal,
    TableType,
)

_AT = datetime(2026, 11, 1, tzinfo=UTC)


def fired(
    signal: str,
    *,
    column: str | None = None,
    effect: str = "warn",
    added: int | None = None,
    low: float = 0,
    high: float = 0,
) -> TableHealthSignal:
    """Build a signal row that fired, without a database.

    Parameters
    ----------
    signal
        The signal.
    column
        Its column, for a column signal.
    effect
        ``warn`` or ``block``.
    added
        The rows a volume observation saw.
    low
        The lower edge of the band.
    high
        The upper edge of the band.

    Returns
    -------
    TableHealthSignal
        A transient row.
    """
    return TableHealthSignal(
        id=uuid.uuid4(),
        signal=signal,
        column_name=column,
        state="fired",
        effect=effect,
        observed=None if added is None else {"added": added},
        band={"low": low, "high": high},
        observed_at=_AT,
    )


def row(signal: str, state: str) -> TableHealthSignal:
    """Build a signal row in a state, without a database.

    Parameters
    ----------
    signal
        The signal.
    state
        Its state.

    Returns
    -------
    TableHealthSignal
        A transient row.
    """
    return TableHealthSignal(
        id=uuid.uuid4(), signal=signal, state=state, effect="warn", observed_at=_AT
    )


def check(outcome: str, *, name: str = "orders/not_null", generated: bool = False) -> CheckHealth:
    """Build a check's newest result, without a database.

    Parameters
    ----------
    outcome
        ``passed``, ``failed``, ``warned`` or ``errored``.
    name
        The check's name.
    generated
        Whether the check was generated from the asset's header.

    Returns
    -------
    CheckHealth
        The result.
    """
    return CheckHealth(
        name=name,
        check=name.rpartition("/")[2],
        run_id=uuid.uuid4(),
        outcome=outcome,
        on_failure="warn",
        failing_rows=None,
        error=None,
        rollback_outcome=None,
        kept=False,
        not_published=False,
        finished_at=_AT,
        generated=generated,
    )


def test_the_worst_names_the_word_and_the_rest_are_counted() -> None:
    s = summarize(
        "TABLE",
        [fired("volume", added=1, low=10, high=20), fired("schema")],
        [check("passed"), check("failed")],
    )
    assert (s.state, s.item, s.text, s.others) == ("failing", "check_failed", "Check failed", 2)


def test_blocking_comes_before_a_failed_check() -> None:
    s = summarize(
        "TABLE", [fired("volume", effect="block", added=1, low=10, high=20)], [check("failed")]
    )
    assert (s.item, s.text, s.others) == ("blocked_rows_dropped", "Blocked: rows dropped", 1)


def test_warnings_rank_by_signal_kind() -> None:
    assert summarize("TABLE", [fired("range", column="price"), fired("freshness")], []).item == (
        "late"
    )


def test_volume_names_its_direction() -> None:
    assert summarize("TABLE", [fired("volume", added=50, low=10, high=20)], []).text == "Rows rose"


def test_a_volume_row_without_its_numbers_reads_as_rose() -> None:
    assert summarize("TABLE", [fired("volume")], []).item == "rows_rose"
    damaged = fired("volume", added=1)
    damaged.band = None
    assert summarize("TABLE", [damaged], []).item == "rows_rose"


def test_the_null_rate_word_names_no_direction() -> None:
    assert summarize("TABLE", [fired("null_rate", column="email")], []).text == "Nulls changed"


def test_a_warned_check_is_amber_after_signals() -> None:
    s = summarize("TABLE", [], [check("warned")])
    assert (s.state, s.item, s.text) == ("warning", "check_warned", "Check warned")
    assert summarize("TABLE", [fired("schema")], [check("warned")]).item == "schema_changed"


def test_an_errored_check_ranks_after_a_failed_one() -> None:
    assert summarize("TABLE", [], [check("errored"), check("failed", name="o/b")]).item == (
        "check_failed"
    )
    assert summarize("TABLE", [], [check("errored")]).text == "Check could not run"


def test_healthy_needs_evidence() -> None:
    assert summarize("TABLE", [row("volume", "no_history")], []).item == "not_judged"
    assert summarize("TABLE", [row("volume", "ok")], []).item == "healthy"
    assert summarize("TABLE", [], [check("passed")]).item == "healthy"
    assert summarize("TABLE", [], []) == HealthSummary("not_judged", "not_judged", "Not judged", 0)


@pytest.mark.parametrize("kind", ["VIEW", "EXTERNAL", "ARTIFACT"])
def test_other_kinds_are_not_judged(kind: str) -> None:
    assert summarize(kind, [row("schema", "fired")], [check("failed")]).item == "not_judged"


def test_the_items_are_ranked_worst_first_and_every_item_has_its_text() -> None:
    assert len(HEALTH_ITEMS) == 17
    assert HEALTH_ITEMS.index("check_failed") == 6
    assert HEALTH_ITEMS.index("healthy") == 15
    assert set(HEALTH_TEXT) == set(HEALTH_ITEMS)


def test_the_colour_follows_the_rank() -> None:
    assert summarize("TABLE", [fired("freshness", effect="block")], []).state == "failing"
    assert summarize("TABLE", [fired("freshness")], []).state == "warning"
    assert summarize("TABLE", [row("schema", "ok")], []).state == "healthy"


def test_a_visible_reference_check_worsens_the_shown_word() -> None:
    assert shown_health("late", 0, [check("failed")]) == HealthSummary(
        "failing", "check_failed", "Check failed", 1
    )
    assert shown_health(None, 0, []) == HealthSummary("not_judged", "not_judged", "Not judged", 0)


def test_a_stored_word_without_a_visible_reference_check_is_shown_as_it_is() -> None:
    assert shown_health("late", 2, []) == HealthSummary("warning", "late", "Late", 2)


def test_a_reference_check_alone_makes_the_word_of_a_healthy_table() -> None:
    assert shown_health("healthy", 0, [check("warned"), check("failed", name="o/ref_x")]) == (
        HealthSummary("failing", "check_failed", "Check failed", 1)
    )
    assert shown_health("not_judged", 0, [check("errored")]).others == 0


def test_a_reference_check_that_ranks_below_the_stored_word_only_counts() -> None:
    assert shown_health("check_failed", 1, [check("warned")]) == HealthSummary(
        "failing", "check_failed", "Check failed", 2
    )


def _table(
    session: Session,
    name: str,
    *,
    table_type: TableType = TableType.ICEBERG,
    health_item: str | None = None,
    health_rank: int = 16,
) -> Table:
    """Add a table row, in an environment of its own.

    Parameters
    ----------
    session
        Database session.
    name
        The table's name.
    table_type
        Its type.
    health_item
        The stored word.
    health_rank
        Its rank.

    Returns
    -------
    Table
        The row, flushed.
    """
    env_id = environment(session)
    namespace = session.scalar(select(Namespace).where(Namespace.environment_id == env_id))
    if namespace is None:
        namespace = Namespace(id=uuid.uuid4(), environment_id=env_id, path="silver")
        session.add(namespace)
        session.flush()
    table = Table(
        id=uuid.uuid4(),
        namespace_id=namespace.id,
        name=name,
        table_type=table_type,
        health_item=health_item,
        health_rank=health_rank,
    )
    session.add(table)
    session.flush()
    return table


def _signal(session: Session, table: Table, signal: str, state: str) -> None:
    """Add a current signal row to a table.

    Parameters
    ----------
    session
        Database session.
    table
        The table.
    signal
        The signal.
    state
        Its state.
    """
    namespace = session.get(Namespace, table.namespace_id)
    assert namespace is not None
    session.add(
        TableHealthSignal(
            id=uuid.uuid4(),
            environment_id=namespace.environment_id,
            table_id=table.id,
            signal=signal,
            snapshot_id=1,
            sequence_number=1,
            observed={"added": 1},
            band=None,
            state=state,
            effect="warn",
            observed_at=_AT,
        )
    )
    session.flush()


def _result(
    session: Session,
    table: Table,
    name: str,
    outcome: CheckOutcome,
    *,
    generated: bool,
    at: datetime,
) -> None:
    """Add a check result to a table.

    Parameters
    ----------
    session
        Database session.
    table
        The table.
    name
        The check's name.
    outcome
        How it ended.
    generated
        Whether the check was generated.
    at
        When it finished.
    """
    repo, version = deployed_job(session, "dev", f"repo-{uuid.uuid4().hex[:8]}", "daily")
    check_result(
        session,
        run_of(session, repo, version),
        table.id,
        name=name,
        outcome=outcome,
        finished_at=at,
        generated=generated,
    )


def test_backfill_fills_only_uncomputed_rows(session: Session) -> None:
    first = _table(session, "a")
    second = _table(session, "b")
    computed = _table(session, "c", health_item="late", health_rank=8)
    _signal(session, second, "volume", "ok")
    session.commit()
    assert backfill_health_keys(session) == 2
    session.expire_all()
    assert computed.health_item == "late"
    assert (first.health_item, first.health_rank) == ("not_judged", 16)
    assert (second.health_item, second.health_rank) == ("healthy", 15)
    assert backfill_health_keys(session) == 0


def test_backfill_takes_at_most_its_limit(session: Session) -> None:
    for name in ("a", "b", "c"):
        _table(session, name)
    session.commit()
    assert backfill_health_keys(session, limit=2) == 2
    assert backfill_health_keys(session, limit=2) == 1


def test_a_view_is_not_judged_and_reads_nothing(session: Session) -> None:
    view = _table(session, "v", table_type=TableType.VIEW)
    _signal(session, view, "schema", "fired")
    refresh_health_key(session, view.id)
    session.commit()
    assert (view.health_item, view.health_rank) == ("not_judged", 16)


def test_refreshing_a_row_that_is_gone_does_nothing(session: Session) -> None:
    refresh_health_key(session, uuid.uuid4())


def test_a_generated_reference_check_never_reaches_the_stored_key(session: Session) -> None:
    t = _table(session, "t")
    _result(session, t, "silver.t/not_null", CheckOutcome.PASSED, generated=False, at=_AT)
    _result(session, t, "silver.t/ref_cust", CheckOutcome.FAILED, generated=True, at=_AT)
    refresh_health_key(session, t.id)
    session.commit()
    assert t.health_item == "healthy"
    assert [c.outcome for c in failing_reference_checks(session, [t.id])[t.id]] == ["failed"]


def test_a_hand_written_check_named_like_a_reference_check_counts(session: Session) -> None:
    t = _table(session, "t")
    _result(session, t, "silver.t/ref_cust", CheckOutcome.FAILED, generated=False, at=_AT)
    refresh_health_key(session, t.id)
    session.commit()
    assert t.health_item == "check_failed"
    assert failing_reference_checks(session, [t.id]) == {}


def test_an_old_reference_failure_followed_by_a_pass_is_not_failing(session: Session) -> None:
    t = _table(session, "t")
    _result(session, t, "silver.t/ref_cust", CheckOutcome.FAILED, generated=True, at=_AT)
    _result(
        session,
        t,
        "silver.t/ref_cust",
        CheckOutcome.PASSED,
        generated=True,
        at=datetime(2026, 11, 2, tzinfo=UTC),
    )
    assert failing_reference_checks(session, [t.id]) == {}


def test_a_generated_key_check_is_not_a_reference_check(session: Session) -> None:
    t = _table(session, "t")
    _result(session, t, "silver.t/key_id", CheckOutcome.FAILED, generated=True, at=_AT)
    assert failing_reference_checks(session, [t.id]) == {}
    assert failing_reference_checks(session, []) == {}


def test_failing_reference_checks_are_found_for_each_table(session: Session) -> None:
    a, b = _table(session, "a"), _table(session, "b")
    _result(session, a, "silver.a/ref_x", CheckOutcome.ERRORED, generated=True, at=_AT)
    _result(session, b, "silver.b/ref_x", CheckOutcome.PASSED, generated=True, at=_AT)
    found = failing_reference_checks(session, [a.id, b.id])
    assert list(found) == [a.id]
    assert [c.outcome for c in found[a.id]] == ["errored"]


def test_a_key_write_never_moves_updated_at(session: Session) -> None:
    t = _table(session, "t")
    old = datetime(2026, 1, 1, tzinfo=UTC)
    t.updated_at = old
    _signal(session, t, "volume", "ok")
    session.commit()
    refresh_health_key(session, t.id)
    session.commit()
    session.expire_all()
    assert (t.health_item, t.updated_at) == ("healthy", old)
    session.execute(update(Table).values(health_item=None, updated_at=old))
    session.commit()
    backfill_health_keys(session)
    session.expire_all()
    assert (t.health_item, t.updated_at) == ("healthy", old)


def test_backfill_takes_the_lowest_ids_first_and_commits_a_chunk_at_a_time(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health_summary, "_CHUNK", 2)
    tables = [_table(session, name) for name in "abcde"]
    session.commit()
    commits: list[int] = []
    engine = session.get_bind()

    def listener(conn: object) -> None:
        commits.append(1)

    event.listen(engine, "commit", listener)
    try:
        assert backfill_health_keys(session, limit=5) == 5
    finally:
        event.remove(engine, "commit", listener)
    assert len(commits) >= 3
    session.expire_all()
    assert all(t.health_item == "not_judged" for t in tables)
    for t in tables:
        t.health_item = None
    session.commit()
    first_two = sorted(t.id for t in tables)[:2]
    assert backfill_health_keys(session, limit=2) == 2
    session.expire_all()
    assert {t.id for t in tables if t.health_item is not None} == set(first_two)


def test_backfill_skips_a_table_dropped_while_it_runs(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = sorted((_table(session, "a"), _table(session, "b")), key=lambda t: t.id)
    session.commit()
    gone = first.id
    compute = health_summary._compute

    def drop_then_compute(s: Session, table_id: uuid.UUID) -> object:
        """Drop the first table just before its word is computed.

        Parameters
        ----------
        s
            Database session.
        table_id
            The table.

        Returns
        -------
        object
            What the real computation gives.
        """
        if table_id == gone:
            s.execute(delete(Table).where(Table.id == gone))
        return compute(s, table_id)

    monkeypatch.setattr(health_summary, "_compute", drop_then_compute)
    assert backfill_health_keys(session) == 2
    session.expire_all()
    assert session.get(Table, gone) is None
    assert second.health_item == "not_judged"


def test_a_check_name_with_two_segments_is_not_a_reference_check(session: Session) -> None:
    t = _table(session, "t")
    _result(session, t, "silver.t/ref_cust/other", CheckOutcome.FAILED, generated=True, at=_AT)
    assert failing_reference_checks(session, [t.id]) == {}
    _result(session, t, "silver.t/sub/ref_cust", CheckOutcome.FAILED, generated=True, at=_AT)
    assert [c.name for c in failing_reference_checks(session, [t.id])[t.id]] == [
        "silver.t/sub/ref_cust"
    ]
