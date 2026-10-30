"""A task's check message, built again for a reader from whom a reference check is withheld."""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator

import pytest

from lakana_core.catalog.service import ROLLBACK_NOT_KNOWN_REASON
from lakana_core.models import CheckOutcome, CheckResult, RollbackOutcome
from lakana_core.runs.check_message import (
    BLOCKING,
    KEPT,
    PASSED,
    SOURCE,
    CheckMessageState,
    UndeterminedMessage,
    branch_of,
    check_message,
    message_for_log,
    message_for_reader,
)
from lakana_core.runs.checks import ERROR_KEPT, WITHHELD_TASK_MESSAGE, log_withheld_message

_TABLE = "gold.t"
_RUN = uuid.UUID("00000000-0000-4000-8000-000000000001")
_HIDDEN = f"{_TABLE}/ref_z"
_WHERE = {"table": _TABLE, "run_id": _RUN, "environment": "dev"}


def _result(
    name: str, outcome: CheckOutcome, on_failure: str, *, rows: int = 3, refused: bool = False
) -> CheckResult:
    """Build a result of `gold.t` that is never stored.

    Parameters
    ----------
    name
        The check's short name.
    outcome
        Its outcome.
    on_failure
        Its failure mode.
    rows
        Its failing rows, for a check that ran.
    refused
        Whether a kept table refused its count (`ERROR_KEPT`).

    Returns
    -------
    CheckResult
        The result.
    """
    errored = outcome is CheckOutcome.ERRORED
    return CheckResult(
        id=uuid.uuid4(),
        check_name=f"{_TABLE}/{name}",
        check_path=f"checks/gold/t/{name}.sql",
        table_name=_TABLE,
        on_failure=on_failure,
        outcome=outcome,
        failing_rows=None if errored else rows,
        error=(ERROR_KEPT if refused else "boom.") if errored else None,
        checked_snapshot_id=None if errored else 42,
        generated=name.startswith("ref_"),
    )


def _build(results: list[CheckResult], state: CheckMessageState) -> str | None:
    """Build the message the worker stores for these results.

    Parameters
    ----------
    results
        The results.
    state
        The state.

    Returns
    -------
    str or None
        The message.
    """
    return check_message(results, state, table=_TABLE, run_id=_RUN, environment="dev")


def _seen(results: list[CheckResult], state: CheckMessageState) -> str | None:
    """Show the stored message to a reader from whom `ref_z` is withheld.

    Parameters
    ----------
    results
        Every result, `ref_z` included.
    state
        The state stored with the message.

    Returns
    -------
    str or None
        What the reader sees.
    """
    return message_for_reader(
        _build(results, state),
        state.to_json(),
        results,
        {_HIDDEN},
        table=_TABLE,
        run_id=_RUN,
        environment="dev",
    )


def _refusals(results: list[CheckResult]) -> dict[str, str]:
    """Name the refusal of each refused result, as the worker records them.

    Parameters
    ----------
    results
        The results.

    Returns
    -------
    dict of str to str
        Check name to the refusal's text.
    """
    return {r.check_name: f"Refused for {r.check_name}." for r in results if r.error == ERROR_KEPT}


def test_a_warn_note_of_more_than_five_checks_counts_only_the_shown_ones() -> None:
    """A `warn` check that could not run is in the note; the withheld one is past the fifth."""
    every = [
        *(_result(n, CheckOutcome.WARNED, "warn") for n in ("a", "b", "c", "d", "e", "ref_z")),
        _result("b0", CheckOutcome.ERRORED, "warn"),
    ]
    stored = _build(every, CheckMessageState(PASSED))
    assert stored is not None and stored.startswith("7 checks warned: ")
    assert _seen(every, CheckMessageState(PASSED)) == (
        "6 checks warned: `a` (3 rows), `b` (3 rows), `b0` (could not run), `c` (3 rows), "
        "`d` (3 rows) and 1 more."
    )


def test_failed_blocking_checks_past_five_and_an_errored_warn_check_count_only_the_shown() -> None:
    blocking = [
        _result(n, CheckOutcome.FAILED, "block") for n in ("a", "b", "c", "d", "e", "ref_z")
    ]
    every = [*blocking, _result("w", CheckOutcome.ERRORED, "warn")]
    state = CheckMessageState(BLOCKING, rollback_outcome="rolled_back")
    stored = _build(every, state)
    assert stored is not None and "6 checks of `gold.t` failed" in stored
    seen = _seen(every, state)
    assert seen == _build([*blocking[:5], every[-1]], state)
    assert seen is not None and "ref_z" not in seen and "more" not in seen


def test_failed_keep_checks_past_five_count_only_the_shown() -> None:
    """The kept branch's listing: six shown `keep` failures and the withheld one."""
    every = [
        _result(n, CheckOutcome.FAILED, "keep") for n in ("a", "b", "c", "d", "e", "f", "ref_z")
    ]
    state = CheckMessageState(KEPT)
    stored = _build(every, state)
    assert stored is not None and "7 checks of `gold.t` failed" in stored
    seen = _seen(every, state)
    assert seen == _build(every[:6], state)
    assert seen is not None and "6 checks of `gold.t` failed" in seen
    assert "and 1 more" in seen and "ref_z" not in seen


def test_a_refused_block_check_beside_more_than_five_failures_on_the_source_path() -> None:
    """Round 3, S1: the source path words the failures without the refused ones."""
    failed = [
        _result(n, CheckOutcome.FAILED, "block") for n in ("a", "b", "c", "d", "e", "f", "ref_z")
    ]
    every = [*failed, _result("aa", CheckOutcome.ERRORED, "block", refused=True)]
    for is_asset in (False, True):
        state = CheckMessageState(SOURCE, source=True, refusals=_refusals(every), is_asset=is_asset)
        stored = _build(every, state)
        assert stored is not None and "7 checks of `gold.t` failed" in stored
        seen = _seen(every, state)
        assert seen == _build([r for r in every if r.check_name != _HIDDEN], state)
        assert seen is not None and "6 checks of `gold.t` failed" in seen
        assert "and 1 more" in seen and "ref_z" not in seen


def test_one_shown_check_that_could_not_run_beside_kept_data_says_the_data_is_kept() -> None:
    """Round 3, S2: no sentence that unchecked data is not published, for data that is kept."""
    every = [
        _result("a", CheckOutcome.ERRORED, "keep"),
        _result("ref_z", CheckOutcome.FAILED, "keep", rows=1204),
    ]
    state = CheckMessageState(KEPT)
    seen = _seen(every, state)
    assert seen == _build(every[:1], state)
    assert seen is not None and "is not published" not in seen
    assert "The table keeps this run's data" in seen and "1204" not in seen


def test_the_kept_data_message_a_reader_sees_word_for_word() -> None:
    """The text written out by hand: one check that could not run, the table keeps the data."""
    every = [
        _result("a", CheckOutcome.ERRORED, "keep"),
        _result("ref_z", CheckOutcome.FAILED, "keep", rows=1204),
    ]
    assert _seen(every, CheckMessageState(KEPT)) == (
        "Check `gold.t/a` could not run (checks/gold/t/a.sql): boom. The table keeps this run's "
        "data (`on_failure: keep`), and nothing that reads it was built. Repair the data or the "
        "asset; after a fix, deploy and run again; if the project did not change: "
        "`lakana runs resume 00000000-0000-4000-8000-000000000001 --env dev`."
    )


def test_a_message_with_nothing_to_withhold_is_returned_as_stored() -> None:
    every = [
        _result("a", CheckOutcome.FAILED, "block"),
        _result("ref_z", CheckOutcome.PASSED, "block"),
    ]
    stored = "Something  the worker wrote, with  two spaces."
    seen = message_for_reader(
        stored, None, every, {_HIDDEN}, table=_TABLE, run_id=_RUN, environment="dev"
    )
    assert seen == stored


def test_the_state_reads_back_what_it_wrote_and_refuses_what_it_cannot_read() -> None:
    state = CheckMessageState(
        SOURCE,
        source=True,
        refusals={"gold.t/a": "No."},
        is_asset=True,
        kept_run_id=str(_RUN),
        unchecked=True,
    )
    assert CheckMessageState.from_json(state.to_json()) == state
    for bad in (
        None,
        [],
        {"branch": "other"},
        {"branch": BLOCKING, "rollback_outcome": "sideways"},
        {"branch": SOURCE, "kept_run_id": "not a uuid"},
        {"branch": PASSED, "success_message": 3},
    ):
        assert CheckMessageState.from_json(bad) is None


def test_results_of_another_kind_than_the_state_cannot_be_worded() -> None:
    failed = [_result("a", CheckOutcome.FAILED, "block")]
    with pytest.raises(UndeterminedMessage):
        _build(failed, CheckMessageState(PASSED))
    with pytest.raises(UndeterminedMessage):
        _build(failed, CheckMessageState(BLOCKING))


_TEMPLATES = (
    (CheckOutcome.PASSED, "block", False),
    (CheckOutcome.FAILED, "block", False),
    (CheckOutcome.ERRORED, "block", False),
    (CheckOutcome.ERRORED, "block", True),
    (CheckOutcome.FAILED, "keep", False),
    (CheckOutcome.ERRORED, "keep", False),
    (CheckOutcome.ERRORED, "keep", True),
    (CheckOutcome.WARNED, "warn", False),
    (CheckOutcome.ERRORED, "warn", False),
)
"""Every outcome and failure mode a result can have, a refused count included."""


def _shown_sets() -> Iterator[list[CheckResult]]:
    """List the visible results the matrix tries: none, one or two of any kind.

    More than five checks of one kind (the "and N more" of a listing) have tests of their own.

    Yields
    ------
    list of CheckResult
        One set of visible results.
    """
    for size in (0, 1, 2):
        for combo in itertools.combinations(range(len(_TEMPLATES)), size):
            yield [_result(f"c{i}", *_TEMPLATES[i][:2], refused=_TEMPLATES[i][2]) for i in combo]


def _states(results: list[CheckResult], *, source: bool) -> Iterator[CheckMessageState]:
    """List the states the worker can store for these results.

    Parameters
    ----------
    results
        Every result.
    source
        Whether the task is a ``source_check`` task.

    Yields
    ------
    CheckMessageState
        One state of the branch the results lead to.
    """
    branch = branch_of(results, source=source)
    refusals = _refusals(results)
    if branch == PASSED:
        yield CheckMessageState(PASSED, source=source)
        yield CheckMessageState(PASSED, source=source, success_message="Built.")
    elif branch == KEPT:
        for dropped in (False, True):
            yield CheckMessageState(KEPT, dropped=dropped, refusals=refusals)
    elif branch == BLOCKING:
        # Each outcome once, then each other part of a rollback once: `_rollback` passes them
        # through to `failure_message`, whose variants these are.
        for outcome in RollbackOutcome:
            yield CheckMessageState(BLOCKING, rollback_outcome=outcome.value)
        for outcome, reason, refused in (
            (RollbackOutcome.FAILED, ROLLBACK_NOT_KNOWN_REASON, False),
            (RollbackOutcome.CHANGED_SINCE, "the reason", False),
            (RollbackOutcome.FAILED, "the reason", True),
        ):
            yield CheckMessageState(
                BLOCKING,
                rollback_outcome=outcome.value,
                rollback_reason=reason,
                rollback_refused=refused,
            )
    else:
        yield CheckMessageState(SOURCE, source=True, refusals=refusals)
        # The check task of a kept table: without its note, with it, and with each of the
        # note's facts on its own.
        for kept_run, unchecked, unknown, without in (
            (None, False, False, False),
            (str(_RUN), False, False, False),
            (str(_RUN), True, False, False),
            (str(_RUN), False, True, False),
            (str(_RUN), False, False, True),
            (None, False, False, True),
        ):
            yield CheckMessageState(
                SOURCE,
                source=True,
                refusals=refusals,
                is_asset=True,
                kept_run_id=kept_run,
                unchecked=unchecked,
                unknown=unknown,
                without_checks=without,
            )


def test_the_reader_sees_the_message_the_worker_writes_from_the_shown_results_alone() -> None:
    """For every kind of result, task and state: the reader's text is the worker's own text.

    The worker's text built from only the results the reader may see, or the fixed text when
    those results would need another kind of message; never a trace of the withheld check.
    """
    tried = 0
    for shown_results, (outcome, mode, refused), source in itertools.product(
        list(_shown_sets()), _TEMPLATES, (False, True)
    ):
        hidden = _result("ref_z", outcome, mode, rows=1204, refused=refused)
        every = sorted([*shown_results, hidden], key=lambda r: r.check_name)
        for state in _states(every, source=source):
            seen = _seen(every, state)
            try:
                expected = _build(shown_results, state)
            except UndeterminedMessage:
                expected = WITHHELD_TASK_MESSAGE
            assert seen == expected, (shown_results, hidden, state)
            assert "ref_z" not in (seen or "") and "1204" not in (seen or "")
            tried += 1
    assert tried > 1000


def test_the_log_words_the_message_without_the_reference_checks() -> None:
    """The log is read alike by every reader, so every generated reference check is left out."""
    results = [
        _result("a", CheckOutcome.FAILED, "block", rows=1),
        _result("ref_z", CheckOutcome.FAILED, "block", rows=9),
    ]
    state = CheckMessageState(BLOCKING, rollback_outcome=RollbackOutcome.ROLLED_BACK.value)
    stored = _build(results, state)
    logged = message_for_log(
        stored, state.to_json(), results, table=_TABLE, run_id=_RUN, environment="dev"
    )
    assert logged == _build(results[:1], state)
    assert logged is not None and "ref_z" not in logged and "9 rows" not in logged


def test_the_log_points_at_runs_show_when_the_message_cannot_be_worded_again() -> None:
    results = [_result("ref_z", CheckOutcome.FAILED, "block", rows=9)]
    stored = f"Check `{_HIDDEN}` failed: 9 rows."
    assert message_for_log(
        stored, None, results, table=_TABLE, run_id=_RUN, environment="dev"
    ) == log_withheld_message(_RUN, "dev")
    assert "ref_z" not in log_withheld_message(_RUN, "dev")
    assert (
        message_for_log(
            "The build failed.", None, results, table=_TABLE, run_id=_RUN, environment="dev"
        )
        == "The build failed."
    )
