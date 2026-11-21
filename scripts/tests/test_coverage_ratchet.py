"""Tests for the coverage ratchet."""

import json
import math
import os
from pathlib import Path

import pytest

from scripts.coverage_ratchet import (
    check,
    coverage_omit_patterns,
    coverage_source_roots,
    is_stale,
    main,
    read_baseline,
    uncovered,
    write_baseline,
)


def test_coverage_above_baseline_passes() -> None:
    """Coverage at or above the baseline is accepted."""
    assert check(current=91.0, baseline=90.0, floor=90.0) is None


def test_coverage_below_baseline_fails() -> None:
    """A drop below the recorded baseline is rejected."""
    message = check(current=89.0, baseline=90.0, floor=90.0)
    assert message is not None
    assert "89.0" in message


def test_coverage_below_floor_fails_even_if_baseline_is_lower() -> None:
    """The floor holds even when the baseline has not caught up to it."""
    message = check(current=85.0, baseline=80.0, floor=90.0)
    assert message is not None


def test_baseline_round_trips(tmp_path: Path) -> None:
    """A written baseline reads back identically."""
    path = tmp_path / ".coverage-baseline"
    write_baseline(path, 93.5)
    assert math.isclose(read_baseline(path), 93.5)


def test_missing_baseline_reads_as_zero(tmp_path: Path) -> None:
    """An absent baseline file starts the ratchet at zero."""
    assert math.isclose(read_baseline(tmp_path / "absent"), 0.0)


def test_a_source_file_newer_than_the_report_is_stale(tmp_path: Path) -> None:
    """A report predating a source file is refused rather than trusted.

    This is not hypothetical: running `pytest` without `--cov` leaves the
    previous run's `coverage.json` in place, so the ratchet happily
    reported 100% for a tree containing a module it had never measured.
    A gate that passes on stale input is not a gate.
    """
    report = tmp_path / "coverage.json"
    report.write_text("{}")
    src = tmp_path / "src"
    src.mkdir()
    newer = src / "new_module.py"
    newer.write_text("x = 1\n")
    os.utime(newer, (report.stat().st_mtime + 10, report.stat().st_mtime + 10))

    assert is_stale(report=report, roots=[src]) is True


def test_a_report_newer_than_every_source_file_is_fresh(tmp_path: Path) -> None:
    """The check passes when the report is the most recent artefact."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "module.py").write_text("x = 1\n")
    report = tmp_path / "coverage.json"
    report.write_text("{}")

    assert is_stale(report=report, roots=[src]) is False


def test_a_missing_source_root_is_not_stale(tmp_path: Path) -> None:
    """A configured root that does not exist yet is skipped, not an error."""
    report = tmp_path / "coverage.json"
    report.write_text("{}")

    assert is_stale(report=report, roots=[tmp_path / "absent"]) is False


def _project(tmp_path: Path, percent: float, baseline: str | None) -> list[str]:
    """Lay out a fresh report, baseline and pyproject in `tmp_path`.

    Parameters
    ----------
    tmp_path
        Directory to build the project in.
    percent
        Total coverage written into the report.
    baseline
        Text of the baseline file, or ``None`` to leave it absent.

    Returns
    -------
    list of str
        The command-line arguments pointing the ratchet at those files.
    """
    src = tmp_path / "src"
    src.mkdir()
    module = src / "module.py"
    module.write_text("x = 1\n")
    (tmp_path / "pyproject.toml").write_text('[tool.coverage.run]\nsource = ["src"]\n')
    # The source roots are resolved against the working directory.
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"totals": {"percent_covered": percent}}))
    future = module.stat().st_mtime + 10
    os.utime(report, (future, future))
    if baseline is not None:
        (tmp_path / ".coverage-baseline").write_text(baseline)
    return [
        "--coverage-json",
        str(report),
        "--baseline",
        str(tmp_path / ".coverage-baseline"),
        "--pyproject",
        str(tmp_path / "pyproject.toml"),
    ]


@pytest.fixture
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the test with `tmp_path` as the working directory.

    Parameters
    ----------
    tmp_path
        The test's temporary directory.
    monkeypatch
        Used to change directory.

    Returns
    -------
    Path
        `tmp_path`.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> int:
    """Invoke `main` with `args` as its command line.

    Parameters
    ----------
    monkeypatch
        Used to set ``sys.argv``.
    args
        Arguments after the program name.

    Returns
    -------
    int
        The exit status.
    """
    monkeypatch.setattr("sys.argv", ["coverage_ratchet", *args])
    return main()


def test_source_roots_are_read_from_pyproject(tmp_path: Path) -> None:
    """The configured `[tool.coverage.run] source` entries become paths."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.coverage.run]\nsource = ["a/src", "scripts"]\n')
    assert coverage_source_roots(pyproject) == [Path("a/src"), Path("scripts")]


def test_source_roots_are_empty_without_config_or_file(tmp_path: Path) -> None:
    """No pyproject, or one without coverage config, yields no roots."""
    assert coverage_source_roots(tmp_path / "absent.toml") == []
    bare = tmp_path / "pyproject.toml"
    bare.write_text("[project]\nname = 'x'\n")
    assert coverage_source_roots(bare) == []


def test_main_passes_at_the_baseline_and_leaves_it_alone(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Equal coverage passes, prints both figures, and writes nothing."""
    args = _project(in_tmp, 95.0, "95.00\n")
    assert _run(monkeypatch, args) == 0
    assert "Coverage 95.00% (baseline 95.00%)." in capsys.readouterr().out
    assert (in_tmp / ".coverage-baseline").read_text() == "95.00\n"


def test_main_fails_below_the_baseline(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A drop below the baseline exits 1 and says coverage may never fall."""
    args = _project(in_tmp, 94.0, "95.00\n")
    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "94.00% is below the baseline of 95.00%" in err
    assert (in_tmp / ".coverage-baseline").read_text() == "95.00\n"


def test_main_fails_below_the_floor(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Coverage under the floor exits 1, with no baseline recorded at all."""
    args = _project(in_tmp, 80.0, None)
    assert _run(monkeypatch, args) == 1
    assert "80.00% is below the floor of 90.00%" in capsys.readouterr().err


def test_main_honours_a_custom_floor(in_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--floor` lowers the bar for a run with no baseline."""
    args = _project(in_tmp, 80.0, None)
    assert _run(monkeypatch, [*args, "--floor", "70"]) == 0


def test_main_without_update_does_not_raise_the_baseline(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Improved coverage passes but the baseline file is only touched with `--update`."""
    args = _project(in_tmp, 97.0, "95.00\n")
    assert _run(monkeypatch, args) == 0
    assert "raised" not in capsys.readouterr().out
    assert (in_tmp / ".coverage-baseline").read_text() == "95.00\n"


def test_main_update_raises_the_baseline(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--update` above the baseline rewrites the file and says so."""
    args = _project(in_tmp, 97.256, "95.00\n")
    assert _run(monkeypatch, [*args, "--update"]) == 0
    assert "Baseline raised to 97.26%." in capsys.readouterr().out
    assert (in_tmp / ".coverage-baseline").read_text() == "97.26\n"


def test_main_update_at_the_baseline_writes_nothing(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--update` with unchanged coverage leaves the file and prints no raise message."""
    args = _project(in_tmp, 95.0, "95.00\n")
    assert _run(monkeypatch, [*args, "--update"]) == 0
    assert "raised" not in capsys.readouterr().out
    assert (in_tmp / ".coverage-baseline").read_text() == "95.00\n"


def test_main_update_creates_a_missing_baseline(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first `--update` run records the baseline from nothing."""
    args = _project(in_tmp, 92.0, None)
    assert _run(monkeypatch, [*args, "--update"]) == 0
    assert (in_tmp / ".coverage-baseline").read_text() == "92.00\n"


def test_main_refuses_a_report_older_than_the_source(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale report exits 1 naming the report and the command to re-run."""
    args = _project(in_tmp, 100.0, "100.00\n")
    module = in_tmp / "src" / "module.py"
    report = in_tmp / "coverage.json"
    later = report.stat().st_mtime + 10
    os.utime(module, (later, later))

    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "is older than the source it covers" in err
    assert "pytest --cov --cov-report=json" in err


def test_main_reports_a_missing_report_with_the_next_step(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No `coverage.json` exits 1 naming the file and the command that makes it."""
    args = _project(in_tmp, 100.0, "100.00\n")
    (in_tmp / "coverage.json").unlink()

    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "coverage.json" in err
    assert "not found" in err
    assert "pytest --cov --cov-report=json" in err


REPORT_NOT_A_COVERAGE_REPORT = "is not a coverage.py JSON report"


def _overwrite_report(root: Path, content: str) -> None:
    """Replace the report's text while keeping it newer than the source.

    Parameters
    ----------
    root
        Project directory built by `_project`.
    content
        New text of ``coverage.json``.
    """
    report = root / "coverage.json"
    mtime = report.stat().st_mtime
    report.write_text(content)
    os.utime(report, (mtime, mtime))


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "[]",
        "{}",
        '{"totals": {}}',
        '{"totals": {"percent_covered": "high"}}',
        '{"totals": {"percent_covered": NaN}}',
        '{"totals": {"percent_covered": Infinity}}',
        '{"totals": {"percent_covered": -Infinity}}',
        '{"totals": {"percent_covered": 150}}',
        '{"totals": {"percent_covered": -5}}',
        '{"totals": {"percent_covered": "100"}}',
        '{"totals": {"percent_covered": true}}',
        '{"totals": {"percent_covered": 1' + "0" * 400 + "}}",
    ],
)
@pytest.mark.parametrize("update", [False, True])
def test_main_reports_a_malformed_report(
    in_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str,
    update: bool,
) -> None:
    """A report without a finite number of percent exits 1 and never moves the baseline."""
    args = _project(in_tmp, 100.0, "95.00\n")
    _overwrite_report(in_tmp, content)

    assert _run(monkeypatch, [*args, "--update"] if update else args) == 1
    err = capsys.readouterr().err
    assert "coverage.json" in err
    assert REPORT_NOT_A_COVERAGE_REPORT in err
    assert "pytest --cov --cov-report=json" in err
    assert (in_tmp / ".coverage-baseline").read_text() == "95.00\n"


@pytest.mark.parametrize("text", ["one hundred", "nan", "inf", "-1", "101", ""])
@pytest.mark.parametrize("update", [False, True])
def test_main_reports_a_malformed_baseline(
    in_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    text: str,
    update: bool,
) -> None:
    """A baseline that is not a percentage exits 1 naming the file, and is left alone."""
    args = _project(in_tmp, 100.0, text + "\n")

    assert _run(monkeypatch, [*args, "--update"] if update else args) == 1
    err = capsys.readouterr().err
    assert ".coverage-baseline" in err
    assert "does not hold a percentage" in err
    assert "delete it" in err
    assert (in_tmp / ".coverage-baseline").read_text() == text + "\n"


def test_read_baseline_refuses_values_outside_zero_to_one_hundred(tmp_path: Path) -> None:
    """`read_baseline` raises rather than return NaN, infinity or an impossible percentage."""
    path = tmp_path / ".coverage-baseline"
    for text in ("nan", "inf", "-0.1", "100.5"):
        path.write_text(text)
        with pytest.raises(ValueError, match="percentage"):
            read_baseline(path)


def _skip_if_root() -> None:
    """Skip a permission test when the process can read anything."""
    if os.geteuid() == 0:
        pytest.skip("permission bits do not bind root")


def test_main_reports_a_report_that_is_a_directory(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory where the report should be exits 1 with a cause, not a traceback."""
    args = _project(in_tmp, 100.0, "100.00\n")
    (in_tmp / "coverage.json").unlink()
    (in_tmp / "coverage.json").mkdir()
    future = (in_tmp / "src" / "module.py").stat().st_mtime + 10
    os.utime(in_tmp / "coverage.json", (future, future))

    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert "coverage.json" in err


def test_main_reports_an_unreadable_report(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report without read permission exits 1 naming the file."""
    _skip_if_root()
    args = _project(in_tmp, 100.0, "100.00\n")
    (in_tmp / "coverage.json").chmod(0)

    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert "coverage.json" in err


def test_main_reports_a_baseline_that_is_a_directory(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A directory where the baseline should be exits 1 with a cause."""
    args = _project(in_tmp, 100.0, None)
    (in_tmp / ".coverage-baseline").mkdir()

    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert ".coverage-baseline" in err


def test_main_reports_an_unreadable_baseline(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A baseline without read permission exits 1 naming the file."""
    _skip_if_root()
    args = _project(in_tmp, 100.0, "100.00\n")
    (in_tmp / ".coverage-baseline").chmod(0)

    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert ".coverage-baseline" in err


@pytest.mark.parametrize("kind", ["malformed", "directory"])
def test_main_reports_an_unusable_pyproject(
    in_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    """A pyproject that is not valid TOML, or not a file, exits 1 naming it."""
    args = _project(in_tmp, 100.0, "100.00\n")
    pyproject = in_tmp / "pyproject.toml"
    pyproject.unlink()
    if kind == "malformed":
        pyproject.write_text("[tool.coverage.run\nsource = ")
    else:
        pyproject.mkdir()

    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "cannot read the coverage configuration" in err
    assert "pyproject.toml" in err


def test_a_report_that_vanishes_during_the_check_is_stale(tmp_path: Path) -> None:
    """If the report cannot be inspected it is not trusted."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "module.py").write_text("x = 1\n")
    assert is_stale(report=tmp_path / "gone.json", roots=[src]) is True


def test_a_source_file_that_vanishes_during_the_check_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source file deleted between listing and `stat` makes the report stale."""
    src = tmp_path / "src"
    src.mkdir()
    ghost = src / "ghost.py"
    report = tmp_path / "coverage.json"
    report.write_text("{}")
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: iter([ghost]))
    assert is_stale(report=report, roots=[src]) is True


def test_main_reports_a_vanished_report_as_stale(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A report that disappears after the existence check exits 1 with the rerun message."""
    args = _project(in_tmp, 100.0, "100.00\n")
    monkeypatch.setattr("scripts.coverage_ratchet.is_stale", lambda **_: True)

    assert _run(monkeypatch, args) == 1
    assert "is older than the source it covers" in capsys.readouterr().err


def test_omitted_files_do_not_make_the_report_stale(tmp_path: Path) -> None:
    """A file the coverage configuration omits (a test) is not source."""
    src = tmp_path / "scripts"
    (src / "tests").mkdir(parents=True)
    script = src / "tool.py"
    script.write_text("x = 1\n")
    test = src / "tests" / "test_tool.py"
    test.write_text("x = 1\n")
    report = tmp_path / "coverage.json"
    report.write_text("{}")
    base = report.stat().st_mtime
    os.utime(script, (base - 10, base - 10))
    os.utime(test, (base + 10, base + 10))

    assert is_stale(report=report, roots=[src], omit=["*/scripts/tests/*"]) is False
    assert is_stale(report=report, roots=[src]) is True
    os.utime(script, (base + 10, base + 10))
    assert is_stale(report=report, roots=[src], omit=["*/scripts/tests/*"]) is True


def test_omit_patterns_are_read_from_pyproject(tmp_path: Path) -> None:
    """The configured `[tool.coverage.run] omit` entries are returned."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.coverage.run]\nomit = ["a/*", "b/*"]\n')
    assert coverage_omit_patterns(pyproject) == ["a/*", "b/*"]
    assert coverage_omit_patterns(tmp_path / "absent.toml") == []


def test_main_ignores_a_newer_omitted_test_file(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Touching an omitted file after the report does not refuse it; touching source does."""
    args = _project(in_tmp, 100.0, "100.00\n")
    (in_tmp / "pyproject.toml").write_text(
        '[tool.coverage.run]\nsource = ["src"]\nomit = ["src/tests/*"]\n'
    )
    (in_tmp / "src" / "tests").mkdir()
    test = in_tmp / "src" / "tests" / "test_x.py"
    test.write_text("x = 1\n")
    later = (in_tmp / "coverage.json").stat().st_mtime + 10
    os.utime(test, (later, later))
    assert _run(monkeypatch, args) == 0

    os.utime(in_tmp / "src" / "module.py", (later, later))
    assert _run(monkeypatch, args) == 1


def test_a_shortfall_too_small_for_two_decimals_is_shown_with_more() -> None:
    """99.999% against 100% must not read "100.00% is below the baseline of 100.00%" (#779)."""
    message = check(current=100 * 80278 / 80279, baseline=100.0, floor=90.0)
    assert message is not None
    assert "99.999% is below the baseline of 100.000%" in message


def test_percentages_closer_than_the_most_decimals_print_with_the_most() -> None:
    """A gap no 12 decimals can show still fails, printed at 12 decimals."""
    message = check(current=100.0 - 1e-13, baseline=100.0, floor=90.0)
    assert message is not None
    assert "100.000000000000% is below the baseline of 100.000000000000%" in message


def test_uncovered_names_each_file_with_its_lines_and_branch_arcs() -> None:
    """Lines come as ranges, arcs as `a->b` (`a->exit` for an exit); full files are left out."""
    report = {
        "files": {
            "pkg/b.py": {
                "missing_lines": [782],
                "missing_branches": [[352, 354], [400, -1]],
            },
            "pkg/a.py": {"missing_lines": [3, 4, 5, 9], "missing_branches": []},
            "pkg/full.py": {"missing_lines": [], "missing_branches": []},
            "pkg/no_branch_data.py": {"missing_lines": [7]},
        }
    }

    assert uncovered(json.dumps(report)) == [
        "pkg/a.py: lines 3-5, 9",
        "pkg/b.py: lines 782; branches 352->354, 400->exit",
        "pkg/no_branch_data.py: lines 7",
    ]


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"files": []}',
        '{"files": {"a.py": []}}',
        '{"files": {"a.py": {"missing_lines": "x"}}}',
    ],
)
def test_uncovered_reads_nothing_from_a_report_without_usable_files(content: str) -> None:
    assert uncovered(content) == []


def _report_with_files(in_tmp: Path, percent: float, files: dict[str, object]) -> None:
    """Rewrite the report with per-file data, keeping it newer than the source.

    Parameters
    ----------
    in_tmp
        The project directory.
    percent
        Total coverage.
    files
        The report's ``files``.
    """
    _overwrite_report(in_tmp, json.dumps({"totals": {"percent_covered": percent}, "files": files}))


def test_main_names_the_uncovered_files_when_it_fails(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure says where coverage is missing, so the log alone shows what to test."""
    args = _project(in_tmp, 100.0, "100.00\n")
    percent = 100 * 80278 / 80279
    _report_with_files(
        in_tmp,
        percent,
        {"src/module.py": {"missing_lines": [782], "missing_branches": [[352, 354]]}},
    )

    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "99.999% is below the baseline of 100.000%" in err
    assert "Missing lines or branches:\n  src/module.py: lines 782; branches 352->354" in err


def test_main_says_when_no_file_has_a_miss(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A drop with no file missing anything points at the configuration instead."""
    args = _project(in_tmp, 94.0, "95.00\n")

    assert _run(monkeypatch, args) == 1
    err = capsys.readouterr().err
    assert "No file in" in err and "coverage.json lists a missing line or branch" in err


def test_main_lists_nothing_when_it_passes(
    in_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _project(in_tmp, 95.0, "95.00\n")
    _report_with_files(in_tmp, 95.0, {"src/module.py": {"missing_lines": [1]}})

    assert _run(monkeypatch, args) == 0
    captured = capsys.readouterr()
    assert "Missing" not in captured.out + captured.err
