"""Tests for the failure codes of tasks: the enum, the table and what a stored value shows."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lakana_core.models import CheckOutcome, CheckResult
from lakana_core.runs.failures import (
    FAILURES,
    UNKNOWN,
    UNKNOWN_FAILURE,
    Failure,
    FailureCode,
    check_failure_code,
    failure_of,
    stored_failure,
)

_DOCS = Path(__file__).resolve().parents[3] / "docs" / "site" / "reference" / "why-did-it-fail.md"

_FIXED_CODES = [
    "build_failed",
    "code_changed",
    "secret_refused",
    "no_runtime",
    "upstream_failed",
    "upstream_missing",
    "check_blocked",
    "check_error",
    "checks_unfinished",
    "query_invalid",
    "permission_denied",
    "not_found",
    "memory",
    "timeout",
    "cancelled",
    "worker_lost",
    "conflict",
    "internal",
]


def test_the_codes_are_the_agreed_ones() -> None:
    """The set of codes is the one the design fixed; a code is never renamed."""
    assert [code.value for code in FailureCode] == _FIXED_CODES


def test_every_code_has_a_cause_and_a_next_step() -> None:
    """Each code maps to a short cause and a next step, both full sentences."""
    assert set(FAILURES) == set(FailureCode)
    for code, (cause, next_step) in FAILURES.items():
        assert cause.endswith("."), code
        assert next_step.endswith("."), code
        assert len(cause) < 200, code


def test_failure_of_builds_the_rest_body() -> None:
    """`failure_of` gives the code, the cause and the next step; `to_json` is the REST body."""
    failure = failure_of(FailureCode.TIMEOUT)

    assert failure.code == "timeout"
    assert failure.to_json() == {
        "code": "timeout",
        "cause": FAILURES[FailureCode.TIMEOUT][0],
        "next_step": FAILURES[FailureCode.TIMEOUT][1],
    }


def test_unknown_is_a_display_value_and_not_a_code() -> None:
    """`unknown` shows an old row; it is not in the enum, so it is never stored."""
    assert UNKNOWN == "unknown"
    assert UNKNOWN not in {code.value for code in FailureCode}
    assert UNKNOWN_FAILURE.code == "unknown"
    assert UNKNOWN_FAILURE.next_step.endswith(".")


@pytest.mark.parametrize(
    ("stored", "failed", "expected"),
    [
        ("memory", True, failure_of(FailureCode.MEMORY)),
        ("memory", False, failure_of(FailureCode.MEMORY)),
        (None, True, UNKNOWN_FAILURE),
        ("a_code_from_a_newer_release", False, UNKNOWN_FAILURE),
        (None, False, None),
    ],
)
def test_stored_failure(stored: str | None, failed: bool, expected: Failure | None) -> None:
    """A stored code wins; an old failed row without one is unknown; no error is `None`.

    Parameters
    ----------
    stored
        The stored value.
    failed
        Whether the row is a failure.
    expected
        What it shows.
    """
    assert stored_failure(stored, failed=failed) == expected


def _result(outcome: CheckOutcome) -> CheckResult:
    """Build a check result with only its outcome set.

    Parameters
    ----------
    outcome
        The outcome.

    Returns
    -------
    CheckResult
        An unsaved row.
    """
    return CheckResult(outcome=outcome)


def test_check_failure_code_is_blocked_when_a_check_failed() -> None:
    """A check that counted and failed is `check_blocked`, even beside one that errored."""
    results = [_result(CheckOutcome.ERRORED), _result(CheckOutcome.FAILED)]

    assert check_failure_code(results) is FailureCode.CHECK_BLOCKED


def test_check_failure_code_is_error_when_no_check_could_run() -> None:
    """When every failing check errored, the code is `check_error`."""
    assert check_failure_code([_result(CheckOutcome.ERRORED)]) is FailureCode.CHECK_ERROR


def test_the_reference_page_lists_every_code_once() -> None:
    """The table on the reference page has one row per code, with its cause and next step."""
    text = _DOCS.read_text(encoding="utf-8")
    rows = re.findall(r"^\| `([a-z_]+)` \| (.+?) \| (.+?) \|$", text, flags=re.MULTILINE)
    codes = [row[0] for row in rows]

    assert sorted(codes) == sorted(_FIXED_CODES)
    for code, cause, next_step in rows:
        assert FAILURES[FailureCode(code)][0] in cause
        assert FAILURES[FailureCode(code)][1] in next_step
