"""Tests for `e2e_report.last_report`."""

from __future__ import annotations

from e2e_report import last_report


def test_a_clean_line_is_read() -> None:
    """A report alone on its line decodes."""
    assert last_report('noise\nE2E {"a": 1}\n') == {"a": 1}


def test_output_after_the_report_on_its_line_is_ignored() -> None:
    """Trailing output on the report's line does not break the read."""
    assert last_report('E2E {"a": [1, 2]}warning: late\n') == {"a": [1, 2]}


def test_the_last_of_two_reports_on_one_line_wins() -> None:
    """Two reports that share a line are both found; the later one is returned."""
    assert last_report('E2E {"n": 1}E2E {"n": 2}\n') == {"n": 2}


def test_output_before_the_report_on_its_line_is_ignored() -> None:
    """A write that lands before the prefix does not hide the report."""
    assert last_report('stderr text E2E {"a": 1}\n') == {"a": 1}


def test_no_report_is_none() -> None:
    """Output without a report, or with a broken one, yields None."""
    assert last_report("nothing\n") is None
    assert last_report("E2E {broken\n") is None


def test_the_proprietary_db_probe_is_valid_python() -> None:
    """The probe's embedded source compiles once formatted, so a bad escape fails without Docker."""
    from test_docker_proprietary_db_integration import _PROBE

    source = _PROBE.format(file="recipe.py", function="load", with_context=False)
    compile(source, "probe.py", "exec")
