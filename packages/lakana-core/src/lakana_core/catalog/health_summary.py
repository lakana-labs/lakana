"""A table's health in one word, worst first, and the key that stores it.

`summarize` is pure: it takes the rows `health_read.current_rows` shows and the newest result of
each check, and names the worst finding. The word is kept on the ``tables`` row
(``health_rank``, ``health_item``, ``health_others``) wherever a check, a signal or a commit
changes it (`refresh_health_key`), so a list can sort and filter by it without reading any
signal. A key write never moves ``updated_at``: that column means the catalog row changed, and
retention counts a table's activity from it.

A generated reference check tells of its target table, so a reader who may not read the target is
not shown it (`lakana_core.runs.hidden_checks`). The stored key therefore leaves those checks
out, and `shown_health` adds the ones a reader may see to the stored word.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from lakana_core.catalog.health import CheckHealth, check_health, latest_checks
from lakana_core.catalog.health_read import current_rows
from lakana_core.models import CheckOutcome, CheckResult, Table, TableHealthSignal
from lakana_core.models.table import table_kind
from lakana_core.projects.model_checks import REF_PREFIX
from lakana_core.runs.hidden_checks import is_reference_check

__all__ = [
    "HEALTH_ITEMS",
    "HEALTH_TEXT",
    "HealthState",
    "HealthSummary",
    "backfill_health_keys",
    "failing_reference_checks",
    "refresh_health_key",
    "shown_health",
    "summarize",
]

HEALTH_ITEMS: tuple[str, ...] = (
    "blocked_late",
    "blocked_rows_dropped",
    "blocked_rows_rose",
    "blocked_nulls_changed",
    "blocked_out_of_range",
    "blocked_schema_changed",
    "check_failed",
    "check_errored",
    "late",
    "rows_dropped",
    "rows_rose",
    "nulls_changed",
    "out_of_range",
    "schema_changed",
    "check_warned",
    "healthy",
    "not_judged",
)
"""Every word a table's health can be, worst first; an item's rank is its index."""

HEALTH_TEXT: Mapping[str, str] = {
    "blocked_late": "Blocked: late",
    "blocked_rows_dropped": "Blocked: rows dropped",
    "blocked_rows_rose": "Blocked: rows rose",
    "blocked_nulls_changed": "Blocked: nulls changed",
    "blocked_out_of_range": "Blocked: out of range",
    "blocked_schema_changed": "Blocked: schema changed",
    "check_failed": "Check failed",
    "check_errored": "Check could not run",
    "late": "Late",
    "rows_dropped": "Rows dropped",
    "rows_rose": "Rows rose",
    "nulls_changed": "Nulls changed",
    "out_of_range": "Out of range",
    "schema_changed": "Schema changed",
    "check_warned": "Check warned",
    "healthy": "Healthy",
    "not_judged": "Not judged",
}
"""The text each item is shown as."""

HealthState = Literal["failing", "warning", "healthy", "not_judged"]

_RANK: Mapping[str, int] = {item: rank for rank, item in enumerate(HEALTH_ITEMS)}
_FAILING_BELOW = 8
"""Ranks under this are failing; the next seven are warnings."""
_HEALTHY = _RANK["healthy"]
_NOT_JUDGED = _RANK["not_judged"]
_SIGNAL_ITEM = {
    "freshness": "late",
    "null_rate": "nulls_changed",
    "range": "out_of_range",
    "schema": "schema_changed",
}
_CHECK_ITEM = {
    CheckOutcome.FAILED.value: "check_failed",
    CheckOutcome.ERRORED.value: "check_errored",
    CheckOutcome.WARNED.value: "check_warned",
}


@dataclass(frozen=True)
class HealthSummary:
    """A table's health in one word.

    Parameters
    ----------
    state
        ``failing`` (red), ``warning`` (amber), ``healthy`` or ``not_judged``.
    item
        The word's name, one of `HEALTH_ITEMS`.
    text
        The word as it is shown.
    others
        How many other findings the word leaves out.
    """

    state: HealthState
    item: str
    text: str
    others: int


def _summary(item: str, others: int) -> HealthSummary:
    """Build the summary of an item.

    Parameters
    ----------
    item
        One of `HEALTH_ITEMS`.
    others
        The findings left out.

    Returns
    -------
    HealthSummary
        The item with its colour and text.
    """
    rank = _RANK[item]
    state: HealthState
    if rank < _FAILING_BELOW:
        state = "failing"
    elif rank < _HEALTHY:
        state = "warning"
    else:
        state = "healthy" if rank == _HEALTHY else "not_judged"
    return HealthSummary(state=state, item=item, text=HEALTH_TEXT[item], others=others)


def _signal_item(row: TableHealthSignal) -> str:
    """Name the finding of a fired signal row.

    Parameters
    ----------
    row
        A row in the state ``fired``.

    Returns
    -------
    str
        The item; a blocking effect makes it a ``blocked_*`` item. A volume row names its
        direction; one whose numbers are damaged reads as ``rows_rose``.
    """
    if row.signal == "volume":
        added = (row.observed or {}).get("added")
        low = (row.band or {}).get("low")
        item = (
            "rows_dropped" if added is not None and low is not None and added < low else "rows_rose"
        )
    else:
        item = _SIGNAL_ITEM[row.signal]
    return f"blocked_{item}" if row.effect == "block" else item


def _check_items(checks: Sequence[CheckHealth]) -> list[str]:
    """Name the findings of the checks that did not pass.

    Parameters
    ----------
    checks
        The newest result of each check.

    Returns
    -------
    list of str
        One item per failed, errored or warned check.
    """
    return [_CHECK_ITEM[c.outcome] for c in checks if c.outcome in _CHECK_ITEM]


def summarize(
    kind: str, signals: Sequence[TableHealthSignal], checks: Sequence[CheckHealth]
) -> HealthSummary:
    """Name the worst finding of a table.

    Parameters
    ----------
    kind
        The API's kind of the table; only ``TABLE`` is judged.
    signals
        The rows that show its current signals (`health_read.current_rows`).
    checks
        The newest result of each of its checks.

    Returns
    -------
    HealthSummary
        The worst finding, with the count of the others. ``healthy`` needs evidence: a passed
        check or an ``ok`` signal; with none, and no finding, the table is ``not_judged``.
    """
    if kind != "TABLE":
        return _summary("not_judged", 0)
    items = [_signal_item(row) for row in signals if row.state == "fired"] + _check_items(checks)
    if items:
        return _summary(min(items, key=_RANK.__getitem__), len(items) - 1)
    evidence = any(row.state == "ok" for row in signals) or any(
        c.outcome == CheckOutcome.PASSED.value for c in checks
    )
    return _summary("healthy" if evidence else "not_judged", 0)


def shown_health(item: str | None, others: int, extra: Sequence[CheckHealth]) -> HealthSummary:
    """Worsen a stored word by the reference checks a reader may see.

    Parameters
    ----------
    item
        The stored item; `None` before it is computed, which reads as ``not_judged``.
    others
        The stored count of other findings.
    extra
        The reader's visible generated reference checks that did not pass.

    Returns
    -------
    HealthSummary
        The stored word when `extra` is empty; otherwise the worst of it and `extra`, with the
        rest counted.
    """
    stored = item or "not_judged"
    found = _check_items(extra)
    if not found:
        return _summary(stored, others)
    # A stored "healthy" or "not_judged" is no finding to count.
    stored_findings = [] if _RANK[stored] >= _HEALTHY else [stored]
    worst = min([*stored_findings, *found], key=_RANK.__getitem__)
    return _summary(worst, others + len(stored_findings) + len(found) - 1)


def _compute(session: Session, table_id: uuid.UUID) -> HealthSummary | None:
    """Compute a table's health word from the rows that feed it.

    Parameters
    ----------
    session
        Database session.
    table_id
        The table.

    Returns
    -------
    HealthSummary or None
        The word; `None` for a table that is gone. The generated reference checks are left out.
    """
    table_type = session.scalar(select(Table.table_type).where(Table.id == table_id))
    if table_type is None:
        return None
    kind = table_kind(table_type)
    if kind != "TABLE":
        return summarize(kind, [], [])
    checks = [
        c for c in latest_checks(session, table_id) if not is_reference_check(c.name, c.generated)
    ]
    return summarize(kind, current_rows(session, table_id), checks)


def refresh_health_key(session: Session, table_id: uuid.UUID) -> None:
    """Compute a table's health word and store it on its row; the caller commits.

    The generated reference checks are left out (see the module docstring). The write keeps
    ``updated_at``. Call it after the table's row is locked, as every write that changes the
    word does: the lock order is the ``tables`` row, then the rows that feed the word.

    Parameters
    ----------
    session
        Database session; nothing is committed.
    table_id
        The table.
    """
    summary = _compute(session, table_id)
    if summary is not None:
        _store(session, table_id, summary)


def _store(
    session: Session, table_id: uuid.UUID, summary: HealthSummary, *, only_unset: bool = False
) -> None:
    """Write a summary to the three columns of a row, keeping ``updated_at``.

    Parameters
    ----------
    session
        Database session; nothing is committed.
    table_id
        The table.
    summary
        What to store.
    only_unset
        Write only if the row has no word yet, so a word a concurrent write stored since this one
        was computed is not overwritten with an older one.
    """
    statement = update(Table).where(Table.id == table_id)
    if only_unset:
        statement = statement.where(Table.health_item.is_(None))
    session.execute(
        statement.values(
            health_rank=_RANK[summary.item],
            health_item=summary.item,
            health_others=summary.others,
            updated_at=Table.updated_at,
        ).execution_options(synchronize_session=False)
    )


def backfill_health_keys(session: Session, *, limit: int = 500) -> int:
    """Compute the health word of tables that have none, as housekeeping.

    Parameters
    ----------
    session
        Database session; commits.
    limit
        The most tables to take in this call.

    Returns
    -------
    int
        How many tables were taken.
    """
    ids = session.scalars(select(Table.id).where(Table.health_item.is_(None)).limit(limit)).all()
    for table_id in ids:
        summary = _compute(session, table_id)
        assert summary is not None  # nosec B101 -- the id was just read
        _store(session, table_id, summary, only_unset=True)
    session.commit()
    return len(ids)


def failing_reference_checks(
    session: Session, table_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[CheckHealth]]:
    """Find, per table, its generated reference checks whose newest result did not pass.

    One statement, however many tables. A check that failed once and passed since is not failing.

    Parameters
    ----------
    session
        Database session.
    table_ids
        The tables.

    Returns
    -------
    dict
        Table id → its failing reference checks, by name; tables with none are left out.
    """
    if not table_ids:
        return {}
    ranked = (
        select(
            CheckResult.id,
            func.row_number()
            .over(
                partition_by=(CheckResult.table_id, CheckResult.check_name),
                order_by=(CheckResult.finished_at.desc(), CheckResult.id.desc()),
            )
            .label("position"),
        )
        .where(
            CheckResult.table_id.in_(table_ids),
            CheckResult.generated.is_(True),
            CheckResult.check_name.like(
                f"%/{REF_PREFIX.replace('_', chr(92) + '_')}%", escape="\\"
            ),
        )
        .subquery()
    )
    rows = session.scalars(
        select(CheckResult)
        .join(ranked, ranked.c.id == CheckResult.id)
        .where(ranked.c.position == 1, CheckResult.outcome != CheckOutcome.PASSED)
        .order_by(CheckResult.check_name)
        .execution_options(populate_existing=True)
    )
    found: dict[uuid.UUID, list[CheckHealth]] = {}
    for row in rows:
        assert row.table_id is not None  # nosec B101 -- the query selects by table id
        found.setdefault(row.table_id, []).append(check_health(row))
    return found
