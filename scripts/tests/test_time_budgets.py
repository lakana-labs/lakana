"""Tests for the time budgets of CI: job minutes and the slow-test ratchet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.time_budgets import (
    Budgets,
    JobTime,
    TestTime,
    check_jobs,
    check_slow_tests,
    file_durations,
    main,
    parse_jobs,
    parse_junit,
    read_baseline,
    read_budgets,
)

ROOT = Path(__file__).resolve().parents[2]

BUDGETS_TOML = """
[jobs]
"unit (*)" = 15
"integration (*)" = 15
"lint" = 10

[slow_tests]
threshold_seconds = 2.0
"""


def _job(name: str, start: str, end: str, conclusion: str = "success") -> str:
    """Build one line of the ``gh api`` job listing the workflow writes.

    Parameters
    ----------
    name
        The job's check name.
    start
        Its ``started_at``.
    end
        Its ``completed_at``.
    conclusion
        Its conclusion.

    Returns
    -------
    str
        One JSON object.
    """
    return json.dumps(
        {
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
            "started_at": start,
            "completed_at": end,
        }
    )


def _case(
    file: str,
    name: str,
    seconds: float,
    *,
    slow: bool = False,
    skipped: bool = False,
) -> str:
    """Build one ``testcase`` element of a junit report (``junit_family=xunit1``).

    Parameters
    ----------
    file
        The test file.
    name
        The test's name.
    seconds
        Its time.
    slow
        Whether it carries the ``slow`` property.
    skipped
        Whether it was skipped.

    Returns
    -------
    str
        The element.
    """
    inner = ""
    if slow:
        inner += '<properties><property name="slow" value="true"/></properties>'
    if skipped:
        inner += '<skipped message="no"/>'
    classname = file.removesuffix(".py").replace("/", ".")
    return (
        f'<testcase classname="{classname}" name="{name}" file="{file}" line="1"'
        f' time="{seconds}">{inner}</testcase>'
    )


def _junit(*cases: str) -> str:
    """Wrap test cases in a junit report.

    Parameters
    ----------
    *cases
        Elements from `_case`.

    Returns
    -------
    str
        The report.
    """
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(cases)
        + "</testsuite></testsuites>"
    )


def test_the_budgets_file_is_read(tmp_path: Path) -> None:
    """Minutes per job pattern, and the seconds above which a unit test must be marked."""
    path = tmp_path / "b.toml"
    path.write_text(BUDGETS_TOML)
    budgets = read_budgets(path)
    assert budgets.jobs == {"unit (*)": 15.0, "integration (*)": 15.0, "lint": 10.0}
    assert budgets.threshold_seconds == pytest.approx(2.0)


@pytest.mark.parametrize(
    "text",
    [
        "not toml [",
        "[jobs]\n",
        '[jobs]\n"x" = "fast"\n[slow_tests]\nthreshold_seconds = 2.0\n',
        '[jobs]\n"x" = 0\n[slow_tests]\nthreshold_seconds = 2.0\n',
        '[jobs]\n"x" = 5\n[slow_tests]\n',
        '[jobs]\n"x" = 5\n[slow_tests]\nthreshold_seconds = true\n',
    ],
)
def test_a_broken_budgets_file_names_itself(tmp_path: Path, text: str) -> None:
    """Every malformed file is refused with its path, never read as "no budget"."""
    path = tmp_path / "b.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match=r"b\.toml"):
        read_budgets(path)


def test_the_checked_in_budgets_hold_the_issue_s_limits() -> None:
    """#326: a unit job at most 15 minutes, each integration job at most 15, 2 s per unit test."""
    budgets = read_budgets(ROOT / "scripts" / "time-budgets.toml")
    assert budgets.jobs["unit (*)"] <= 15
    assert budgets.jobs["integration (*)"] <= 15
    assert budgets.threshold_seconds == pytest.approx(2.0)
    assert read_baseline(ROOT / "scripts" / "slow-tests-baseline.json") >= 0


def test_jobs_are_read_from_the_listing() -> None:
    """Only finished jobs that ran count: skipped and cancelled ones measure nothing."""
    text = "\n".join(
        [
            _job("lint", "2026-10-24T10:00:00Z", "2026-10-24T10:01:30Z"),
            _job("unit (3.14, 1/3)", "2026-10-24T10:00:00Z", "2026-10-24T10:16:00Z", "failure"),
            _job("integration", "2026-10-24T10:00:00Z", "2026-10-24T10:00:00Z", "skipped"),
            _job("docs", "2026-10-24T10:00:00Z", "2026-10-24T10:30:00Z", "cancelled"),
            "",
            json.dumps({"name": "time budgets", "status": "in_progress", "conclusion": None}),
        ]
    )
    assert parse_jobs(text) == [JobTime("lint", 1.5), JobTime("unit (3.14, 1/3)", 16.0)]


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        '{"name": "lint", "status": "completed", "conclusion": "success"}',
        '{"name": "lint", "status": "completed", "conclusion": "success",'
        ' "started_at": "yesterday", "completed_at": "2026-10-24T10:00:00Z"}',
    ],
)
def test_a_listing_that_cannot_be_read_is_an_error(line: str) -> None:
    """A budget check that reads nothing must not pass."""
    with pytest.raises(ValueError, match="job listing"):
        parse_jobs(line)


def test_a_job_within_its_budget_passes() -> None:
    """15 minutes is within a 15-minute budget."""
    budgets = Budgets(jobs={"unit (*)": 15.0}, threshold_seconds=2.0)
    errors, notes = check_jobs([JobTime("unit (3.14, 1/3)", 15.0)], budgets)
    assert errors == []
    assert notes == ["unit (3.14, 1/3): 15.0 of 15 min"]


def test_a_job_over_its_budget_names_the_job_the_budget_and_the_next_step() -> None:
    """The message says which job, by how much, where the budget lives and what to do."""
    budgets = Budgets(jobs={"integration (*)": 15.0, "lint": 10.0}, threshold_seconds=2.0)
    errors, _ = check_jobs(
        [JobTime("integration (services, 2/2)", 17.25), JobTime("lint", 1.0)], budgets
    )
    (error,) = errors
    assert "integration (services, 2/2) took 17.2 min" in error
    assert "budget of 15 min" in error
    assert "scripts/time-budgets.toml" in error
    assert "shard" in error
    assert "docs/agent/testing.md" in error


def test_the_first_matching_pattern_is_the_budget() -> None:
    """A specific name may come before a pattern that would also match."""
    budgets = Budgets(jobs={"unit (macos, *)": 20.0, "unit (*)": 15.0}, threshold_seconds=2.0)
    errors, _ = check_jobs([JobTime("unit (macos, 1/2)", 18.0)], budgets)
    assert errors == []


def test_a_job_without_a_budget_is_an_error() -> None:
    """A new job gets a budget on purpose; ``test_workflows.py`` checks the same statically."""
    budgets = Budgets(jobs={"lint": 10.0}, threshold_seconds=2.0)
    errors, _ = check_jobs([JobTime("brand new", 1.0)], budgets)
    (error,) = errors
    assert "brand new" in error
    assert "no budget" in error


def test_the_junit_report_is_read() -> None:
    """File, name, seconds, the slow property and skips."""
    text = _junit(
        _case("a/test_x.py", "test_one", 0.5),
        _case("a/test_x.py", "test_two[1]", 3.25, slow=True),
        _case("a/test_y.py", "test_three", 9.0, skipped=True),
    )
    assert parse_junit(text) == [
        TestTime("a/test_x.py", "test_one", 0.5, slow=False, skipped=False),
        TestTime("a/test_x.py", "test_two[1]", 3.25, slow=True, skipped=False),
        TestTime("a/test_y.py", "test_three", 9.0, slow=False, skipped=True),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "not xml",
        _junit('<testcase name="t" time="1"/>'),
        _junit('<testcase name="t" file="a.py" time="x"/>'),
        _junit('<testcase file="a.py" time="1"/>'),
    ],
)
def test_a_junit_report_that_cannot_be_read_is_an_error(text: str) -> None:
    """The report must come from ``-o junit_family=xunit1``: only that family names the file."""
    with pytest.raises(ValueError, match="junit"):
        parse_junit(text)


def test_an_unmarked_test_over_the_threshold_fails_with_its_name_and_the_choices() -> None:
    """Make it faster, or mark it ``slow`` within the ratchet."""
    tests = [
        TestTime("a/test_x.py", "test_slow", 2.0, slow=False, skipped=False),
        TestTime("a/test_x.py", "test_fast", 1.99, slow=False, skipped=False),
    ]
    errors, _ = check_slow_tests(tests, threshold=2.0, baseline=5)
    (error,) = errors
    assert "a/test_x.py::test_slow took 2.0 s" in error
    assert "@pytest.mark.slow" in error
    assert "faster" in error


def test_a_marked_or_skipped_test_may_take_longer() -> None:
    """The ratchet counts marked tests; a skipped test measured nothing."""
    tests = [
        TestTime("a.py", "t", 30.0, slow=True, skipped=False),
        TestTime("a.py", "u", 9.0, slow=False, skipped=True),
    ]
    errors, notes = check_slow_tests(tests, threshold=2.0, baseline=1)
    assert errors == []
    assert notes == ["1 test is marked slow (baseline 1)."]


def test_more_slow_tests_than_the_baseline_fail() -> None:
    """The count may fall, never rise."""
    tests = [TestTime("a.py", f"t{i}", 3.0, slow=True, skipped=False) for i in range(3)]
    errors, _ = check_slow_tests(tests, threshold=2.0, baseline=2)
    (error,) = errors
    assert "3 tests are marked slow" in error
    assert "baseline of 2" in error
    assert "scripts/slow-tests-baseline.json" in error


def test_fewer_slow_tests_than_the_baseline_pass_and_say_to_lower_it() -> None:
    """The freed room is not left for the next change to spend."""
    tests = [TestTime("a.py", "t", 3.0, slow=True, skipped=False)]
    errors, notes = check_slow_tests(tests, threshold=2.0, baseline=4)
    assert errors == []
    assert any("--update" in note for note in notes)


def test_a_parametrized_test_counts_each_case() -> None:
    """Each case of a parametrized slow test is one slow test."""
    tests = [TestTime("a.py", f"t[{i}]", 0.1, slow=True, skipped=False) for i in range(4)]
    errors, _ = check_slow_tests(tests, threshold=2.0, baseline=3)
    assert errors


def test_file_durations_sum_the_tests_of_each_file() -> None:
    """The shard plugin spreads files by these seconds; skips count their (tiny) time too."""
    tests = [
        TestTime("a.py", "t", 1.25, slow=False, skipped=False),
        TestTime("a.py", "u", 2.0, slow=True, skipped=False),
        TestTime("b.py", "v", 0.004, slow=False, skipped=True),
    ]
    assert file_durations(tests) == {"a.py": 3.25, "b.py": 0.0}


def test_the_baseline_is_read(tmp_path: Path) -> None:
    """A JSON object with the count of slow tests."""
    path = tmp_path / "s.json"
    path.write_text('{"slow_tests": 7}\n')
    assert read_baseline(path) == 7


@pytest.mark.parametrize("text", ["", "[]", '{"slow_tests": -1}', '{"slow_tests": "7"}', "{}"])
def test_a_broken_baseline_names_itself(tmp_path: Path, text: str) -> None:
    """A baseline that cannot be read is never read as zero or as unlimited."""
    path = tmp_path / "s.json"
    path.write_text(text)
    with pytest.raises(ValueError, match=r"s\.json"):
        read_baseline(path)


@pytest.fixture
def in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in a directory with a budgets file and a baseline under ``scripts/``.

    Parameters
    ----------
    tmp_path
        pytest's temporary directory.
    monkeypatch
        Changes the working directory.

    Returns
    -------
    Path
        The directory.
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "time-budgets.toml").write_text(BUDGETS_TOML)
    (tmp_path / "scripts" / "slow-tests-baseline.json").write_text('{"slow_tests": 2}\n')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_main_checks_the_jobs(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``jobs FILE`` passes within budget and lists each job's minutes."""
    listing = in_tmp / "jobs.jsonl"
    listing.write_text(_job("lint", "2026-10-24T10:00:00Z", "2026-10-24T10:02:00Z"))
    assert main(["jobs", str(listing)]) == 0
    assert "lint: 2.0 of 10 min" in capsys.readouterr().out


def test_main_fails_a_job_over_budget(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The exit status fails the step; the message is on standard error."""
    listing = in_tmp / "jobs.jsonl"
    listing.write_text(_job("lint", "2026-10-24T10:00:00Z", "2026-10-24T10:12:00Z"))
    assert main(["jobs", str(listing)]) == 1
    assert "lint took 12.0 min" in capsys.readouterr().err


def test_main_reports_an_unreadable_listing(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing or broken file fails with its name, never passes."""
    assert main(["jobs", str(in_tmp / "missing.jsonl")]) == 1
    assert "missing.jsonl" in capsys.readouterr().err
    broken = in_tmp / "broken.jsonl"
    broken.write_text("nope")
    assert main(["jobs", str(broken)]) == 1
    assert "job listing" in capsys.readouterr().err


def test_main_reports_a_broken_budgets_file(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The budgets file is read before anything else."""
    (in_tmp / "scripts" / "time-budgets.toml").write_text("[jobs]\n")
    listing = in_tmp / "jobs.jsonl"
    listing.write_text("")
    assert main(["jobs", str(listing)]) == 1
    assert "time-budgets.toml" in capsys.readouterr().err


def test_main_checks_the_slow_tests(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``slow-tests`` reads every junit report it is given."""
    (in_tmp / "one.xml").write_text(_junit(_case("a.py", "t", 2.5, slow=True)))
    (in_tmp / "two.xml").write_text(_junit(_case("b.py", "u", 0.1)))
    assert main(["slow-tests", "one.xml", "two.xml"]) == 0
    assert "1 test is marked slow (baseline 2)" in capsys.readouterr().out


def test_main_fails_an_unmarked_slow_test(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Exit status 1 and the test's name."""
    (in_tmp / "one.xml").write_text(_junit(_case("a.py", "t", 2.5)))
    assert main(["slow-tests", "one.xml"]) == 1
    assert "a.py::t took 2.5 s" in capsys.readouterr().err


def test_main_refuses_to_check_no_tests(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Reports without a single test are a broken upload, not a pass."""
    (in_tmp / "one.xml").write_text(_junit())
    assert main(["slow-tests", "one.xml"]) == 1
    assert "no tests" in capsys.readouterr().err


def test_main_reports_an_unreadable_junit_report(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing report names itself."""
    assert main(["slow-tests", "missing.xml"]) == 1
    assert "missing.xml" in capsys.readouterr().err


def test_main_reports_a_broken_baseline(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The baseline is restored from git, never guessed."""
    (in_tmp / "scripts" / "slow-tests-baseline.json").write_text("[]")
    (in_tmp / "one.xml").write_text(_junit(_case("a.py", "t", 0.1)))
    assert main(["slow-tests", "one.xml"]) == 1
    assert "slow-tests-baseline.json" in capsys.readouterr().err


def test_update_lowers_the_baseline_and_never_raises_it(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--update`` records a fall; a rise still fails and leaves the file alone."""
    baseline = in_tmp / "scripts" / "slow-tests-baseline.json"
    (in_tmp / "one.xml").write_text(_junit(_case("a.py", "t", 2.5, slow=True)))
    assert main(["slow-tests", "--update", "one.xml"]) == 0
    assert json.loads(baseline.read_text()) == {"slow_tests": 1}
    assert "lowered to 1" in capsys.readouterr().out
    (in_tmp / "two.xml").write_text(
        _junit(_case("a.py", "t", 2.5, slow=True), _case("a.py", "u", 2.5, slow=True))
    )
    assert main(["slow-tests", "--update", "two.xml"]) == 1
    assert json.loads(baseline.read_text()) == {"slow_tests": 1}


def test_main_writes_the_durations(in_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``durations`` merges the measured files into the file the shards read."""
    output = in_tmp / "scripts" / "test-durations.json"
    output.write_text(json.dumps({"kept.py": 4.0, "a.py": 99.0}))
    (in_tmp / "one.xml").write_text(_junit(_case("a.py", "t", 2.5), _case("b.py", "u", 1.0)))
    assert main(["durations", "--output", str(output), "one.xml"]) == 0
    assert json.loads(output.read_text()) == {"a.py": 2.5, "b.py": 1.0, "kept.py": 4.0}
    assert "2 files" in capsys.readouterr().out


def test_main_starts_a_durations_file(in_tmp: Path) -> None:
    """Without a file yet, the measured files are the whole file."""
    output = in_tmp / "new.json"
    (in_tmp / "one.xml").write_text(_junit(_case("a.py", "t", 2.5)))
    assert main(["durations", "--output", str(output), "one.xml"]) == 0
    assert json.loads(output.read_text()) == {"a.py": 2.5}


def test_main_refuses_a_broken_durations_file(
    in_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken file is not silently replaced: it names itself."""
    output = in_tmp / "d.json"
    output.write_text("[")
    (in_tmp / "one.xml").write_text(_junit(_case("a.py", "t", 2.5)))
    assert main(["durations", "--output", str(output), "one.xml"]) == 1
    assert "d.json" in capsys.readouterr().err
