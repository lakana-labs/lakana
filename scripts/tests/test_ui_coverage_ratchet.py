"""Tests for the UI coverage ratchet."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.ui_coverage_ratchet import is_stale, main, ui_percentage


def _summary(lines: tuple[int, int], branches: tuple[int, int]) -> dict[str, object]:
    """Build a Vitest ``coverage-summary.json`` document.

    Parameters
    ----------
    lines
        ``(total, covered)`` lines.
    branches
        ``(total, covered)`` branches.

    Returns
    -------
    dict
        The parsed summary.
    """
    return {
        "total": {
            "lines": {"total": lines[0], "covered": lines[1]},
            "branches": {"total": branches[0], "covered": branches[1]},
        }
    }


def _setup(tmp_path: Path, percent: tuple[int, int], baseline: str | None) -> list[str]:
    """Write a summary, a source tree and a baseline; return the CLI arguments.

    Parameters
    ----------
    tmp_path
        Scratch directory.
    percent
        ``(total, covered)`` applied to lines; branches are empty.
    baseline
        Baseline file content, or None for no file.

    Returns
    -------
    list of str
        Arguments pointing the script at the files.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.ts").write_text("x")
    os.utime(src / "a.ts", (1, 1))
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps(_summary(percent, (0, 0))))
    args = ["--summary", str(summary), "--source", str(src)]
    args += ["--baseline", str(tmp_path / "baseline")]
    if baseline is not None:
        (tmp_path / "baseline").write_text(baseline)
    return args


def test_lines_and_branches_count_together() -> None:
    """Lines and branches are one pool: 95 of 100."""
    assert ui_percentage(_summary((90, 90), (10, 5))) == pytest.approx(95.0)


def test_an_empty_summary_is_full_coverage() -> None:
    """Nothing to cover counts as 100."""
    assert ui_percentage(_summary((0, 0), (0, 0))) == pytest.approx(100.0)


@pytest.mark.parametrize("name", ["a.tsx", "a.ts", "a.css", "a.json"])
def test_a_report_older_than_a_source_file_is_stale(tmp_path: Path, name: str) -> None:
    """A newer source file of any covered type makes the report stale."""
    report = tmp_path / "summary.json"
    report.write_text("{}")
    os.utime(report, (100, 100))
    src = tmp_path / "src"
    src.mkdir()
    (src / name).write_text("x")
    os.utime(src / name, (200, 200))
    assert is_stale(report=report, source=src) is True
    os.utime(src / name, (50, 50))
    assert is_stale(report=report, source=src) is False


def test_a_missing_report_is_stale(tmp_path: Path) -> None:
    """A report that cannot be inspected is not trusted."""
    assert is_stale(report=tmp_path / "none.json", source=tmp_path) is True


def test_main_passes_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """At the baseline the check passes."""
    assert main(_setup(tmp_path, (100, 100), "100.00\n")) == 0
    assert "100.00" in capsys.readouterr().out


def test_main_fails_below_the_baseline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Below the baseline fails and names both numbers."""
    assert main(_setup(tmp_path, (100, 95), "100.00\n")) == 1
    err = capsys.readouterr().err
    assert "95.00" in err
    assert "100.00" in err


def test_main_fails_below_the_floor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Below the floor fails even with a lower baseline."""
    assert main(_setup(tmp_path, (100, 80), "50\n")) == 1
    assert "floor" in capsys.readouterr().err


def test_main_refuses_a_stale_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A report older than the sources is refused with the rerun command."""
    args = _setup(tmp_path, (100, 100), "100\n")
    os.utime(tmp_path / "summary.json", (0, 0))
    os.utime(tmp_path / "src" / "a.ts", (5, 5))
    assert main(args) == 1
    assert "npm --prefix ui run test:coverage" in capsys.readouterr().err


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"total": {}}'])
def test_main_refuses_a_missing_or_malformed_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None
) -> None:
    """A missing or malformed summary names the file and the rerun command."""
    args = _setup(tmp_path, (100, 100), "100\n")
    summary = tmp_path / "summary.json"
    if content is None:
        summary.unlink()
    else:
        summary.write_text(content)
    assert main(args) == 1
    err = capsys.readouterr().err
    assert "summary.json" in err
    assert "npm --prefix ui run test:coverage" in err


def test_main_refuses_a_bad_baseline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A baseline that is not a percentage is refused."""
    assert main(_setup(tmp_path, (100, 100), "abc")) == 1
    assert "baseline" in capsys.readouterr().err


def test_main_refuses_an_unreadable_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A baseline path that is a directory is reported."""
    args = _setup(tmp_path, (100, 100), None)
    (tmp_path / "baseline").mkdir()
    assert main(args) == 1
    assert "cannot read" in capsys.readouterr().err


def test_update_raises_the_baseline_and_never_lowers_it(tmp_path: Path) -> None:
    """--update writes only a higher number."""
    args = _setup(tmp_path, (100, 96), "90\n")
    assert main([*args, "--update"]) == 0
    assert float((tmp_path / "baseline").read_text()) == pytest.approx(96.0)
    (tmp_path / "summary.json").write_text(json.dumps(_summary((100, 93), (0, 0))))
    assert main([*args, "--update"]) == 1
    assert float((tmp_path / "baseline").read_text()) == pytest.approx(96.0)
