"""Enforce the time budgets of CI: minutes per job, and the slow-test ratchet.

The budgets live in ``scripts/time-budgets.toml``: ``[jobs]`` maps check names (``fnmatch``
patterns, the first match wins) to minutes, and ``[slow_tests] threshold_seconds`` is the time
from which a unit test must carry ``@pytest.mark.slow``. The number of tests that carry it is
recorded in ``scripts/slow-tests-baseline.json``; it may fall, never rise.

Three commands:

- ``jobs LISTING`` reads the jobs of a workflow run (JSON lines of ``name``, ``status``,
  ``conclusion``, ``started_at``, ``completed_at``, as ``gh api .../jobs --jq '.jobs[]'``
  prints them) and fails when a job took longer than its budget or has none.
- ``slow-tests REPORT...`` reads junit reports written with ``-o junit_family=xunit1`` and
  fails when an unmarked test took ``threshold_seconds`` or more, or when more tests are
  marked slow than the baseline allows. ``--update`` lowers the baseline after a fall.
- ``durations --output FILE REPORT...`` merges the seconds per test file into the file the
  shards of ``scripts/pytest_selection.py`` are spread by.

See docs/agent/testing.md, "Time budgets".
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BUDGETS = Path("scripts/time-budgets.toml")
BASELINE = Path("scripts/slow-tests-baseline.json")
DOCS = "docs/agent/testing.md, 'Time budgets'"
UPDATE_COMMAND = "uv run python scripts/time_budgets.py slow-tests --update REPORT.xml ..."


@dataclass(frozen=True)
class Budgets:
    """The budgets of ``scripts/time-budgets.toml``.

    Attributes
    ----------
    jobs
        Check-name pattern to minutes, in the file's order.
    threshold_seconds
        From this many seconds a unit test must be marked ``slow``.
    confirm_seconds
        From this many seconds the solo re-run of a flagged test fails it; None means
        ``threshold_seconds``.
    """

    jobs: dict[str, float]
    threshold_seconds: float
    confirm_seconds: float | None = None


@dataclass(frozen=True)
class JobTime:
    """How long one job of a run took.

    Attributes
    ----------
    name
        The job's check name, for example ``unit (3.14, 1/3)``.
    minutes
        From its start on a runner to its end.
    """

    name: str
    minutes: float


@dataclass(frozen=True)
class TestTime:
    """One test of a junit report.

    Attributes
    ----------
    file
        The test file, relative to the root.
    name
        The test's name, parameters included.
    seconds
        Setup, call and teardown together.
    slow
        Whether it carries ``@pytest.mark.slow``.
    skipped
        Whether it was skipped.
    classname
        The report's ``classname``: the module, dotted, and the class of a method.
    """

    __test__ = False  # a data class, not a test class for pytest to collect

    file: str
    name: str
    seconds: float
    slow: bool
    skipped: bool
    classname: str = ""

    @property
    def nodeid(self) -> str:
        """Name the test as pytest does, so it can be run again.

        Returns
        -------
        str
            ``file::name``, or ``file::Class::name`` for a method.
        """
        module = self.file.removesuffix(".py").replace("/", ".")
        prefix = module + "."
        classes = self.classname.removeprefix(prefix).split(".")
        if not self.classname.startswith(prefix):
            classes = []
        return "::".join([self.file, *classes, self.name])


def _is_number(value: object) -> bool:
    """Report whether `value` is a finite, non-negative number (a boolean is not one).

    Parameters
    ----------
    value
        Anything read from a file.

    Returns
    -------
    bool
        True for an int or float that is finite and at least zero.
    """
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def read_budgets(path: Path) -> Budgets:
    """Read the budgets file.

    Parameters
    ----------
    path
        ``scripts/time-budgets.toml``.

    Returns
    -------
    Budgets
        Its job budgets and the slow-test threshold.

    Raises
    ------
    ValueError
        If the file is not TOML, has no job budgets, a budget is not a positive number, or the
        threshold is missing. The message names the file.
    OSError
        If the file cannot be read.
    """
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{path} is not TOML ({error}). Restore it from git.") from None
    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise ValueError(f"{path} has no [jobs] table of budgets in minutes.")
    for name, minutes in jobs.items():
        if not _is_number(minutes) or minutes == 0:
            raise ValueError(f"{path}: the budget of {name!r} is not a positive number of minutes.")
    threshold = data.get("slow_tests", {}).get("threshold_seconds")
    if not _is_number(threshold):
        raise ValueError(f"{path} has no [slow_tests] threshold_seconds.")
    confirm = data["slow_tests"].get("confirm_seconds")
    if confirm is not None and not _is_number(confirm):
        raise ValueError(f"{path}: [slow_tests] confirm_seconds is not a number of seconds.")
    return Budgets(
        jobs={str(k): float(v) for k, v in jobs.items()},
        threshold_seconds=float(threshold),
        confirm_seconds=None if confirm is None else float(confirm),
    )


def _timestamp(value: object) -> datetime:
    """Read one of the API's ISO 8601 timestamps.

    Parameters
    ----------
    value
        ``started_at`` or ``completed_at``.

    Returns
    -------
    datetime
        The moment.

    Raises
    ------
    ValueError
        If it is not a timestamp.
    """
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not a timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_jobs(text: str) -> list[JobTime]:
    """Read the jobs of a run that ran to their end.

    Parameters
    ----------
    text
        JSON lines, one job each, as ``gh api .../jobs --jq '.jobs[]'`` prints them.

    Returns
    -------
    list of JobTime
        Each completed job that succeeded or failed; skipped and cancelled jobs, and jobs still
        running (the budget job itself), measured nothing and are left out.

    Raises
    ------
    ValueError
        If a line is not a job object, or a finished job lacks its timestamps.
    """
    jobs: list[JobTime] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            job = json.loads(line)
            if not isinstance(job, dict):
                raise ValueError("not an object")
            if job.get("status") != "completed" or job.get("conclusion") not in (
                "success",
                "failure",
            ):
                continue
            seconds = (
                _timestamp(job.get("completed_at")) - _timestamp(job.get("started_at"))
            ).total_seconds()
        except ValueError as error:
            raise ValueError(
                f"the job listing has a line that is not a job ({error}): {line}"
            ) from None
        jobs.append(JobTime(str(job.get("name")), seconds / 60))
    return jobs


def _budget(name: str, budgets: Budgets) -> tuple[str, float] | None:
    """Find a job's budget: the first pattern that matches its name.

    Parameters
    ----------
    name
        The job's check name.
    budgets
        The budgets.

    Returns
    -------
    tuple or None
        The pattern and its minutes, or None when no pattern matches.
    """
    for pattern, minutes in budgets.jobs.items():
        if fnmatch.fnmatchcase(name, pattern):
            return pattern, minutes
    return None


def _minutes(value: float) -> str:
    """Format a budget without a needless ``.0``.

    Parameters
    ----------
    value
        Minutes.

    Returns
    -------
    str
        ``15`` for 15.0, ``7.5`` for 7.5.
    """
    return f"{value:g}"


def check_jobs(jobs: Sequence[JobTime], budgets: Budgets) -> tuple[list[str], list[str]]:
    """Compare each job's minutes with its budget.

    Parameters
    ----------
    jobs
        The jobs of the run.
    budgets
        The budgets.

    Returns
    -------
    tuple of two lists of str
        The errors (a job over its budget, or without one) and one note per job within budget.
    """
    errors: list[str] = []
    notes: list[str] = []
    for job in jobs:
        found = _budget(job.name, budgets)
        if found is None:
            errors.append(
                f"{job.name} has no budget: add a pattern that matches its name to [jobs] in "
                f"{BUDGETS} ({DOCS})."
            )
            continue
        pattern, minutes = found
        if job.minutes > minutes:
            errors.append(
                f"{job.name} took {job.minutes:.1f} min, over its budget of {_minutes(minutes)} "
                f"min ({pattern!r} in {BUDGETS}). Next step: find what grew in the job's log "
                "(the slowest steps and `pytest --durations=20`), make it faster, or add a "
                "shard to the job; raise the budget only with the maintainer's agreement "
                f"({DOCS})."
            )
        else:
            notes.append(f"{job.name}: {job.minutes:.1f} of {_minutes(minutes)} min")
    return errors, notes


def parse_junit(text: str) -> list[TestTime]:
    """Read the tests of a junit report.

    Parameters
    ----------
    text
        A report written by ``pytest --junitxml=FILE -o junit_family=xunit1``.

    Returns
    -------
    list of TestTime
        Every test case of the report.

    Raises
    ------
    ValueError
        If the text is not XML, or a test case lacks its file, name or time (only the
        ``xunit1`` family writes the file).
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError(f"not a junit report ({error})") from None
    tests: list[TestTime] = []
    for case in root.iter("testcase"):
        file, name, time = case.get("file"), case.get("name"), case.get("time")
        try:
            seconds = float(time or "")
        except ValueError:
            seconds = math.nan
        if not file or not name or not math.isfinite(seconds):
            raise ValueError(
                "a junit test case lacks its file, name or time: write the report with "
                "`pytest --junitxml=FILE -o junit_family=xunit1`"
            )
        slow = any(
            p.get("name") == "slow" and p.get("value") == "true" for p in case.iter("property")
        )
        skipped = case.find("skipped") is not None
        tests.append(TestTime(file, name, seconds, slow, skipped, case.get("classname", "")))
    return tests


def _tests(count: int) -> str:
    """Say "1 test" or "N tests".

    Parameters
    ----------
    count
        A number of tests.

    Returns
    -------
    str
        The number and the noun.
    """
    return "1 test" if count == 1 else f"{count} tests"


def check_slow_tests(
    tests: Sequence[TestTime],
    *,
    threshold: float,
    baseline: int,
    confirm_threshold: float | None = None,
    remeasure: Callable[[list[TestTime]], dict[str, float]] | None = None,
) -> tuple[list[str], list[str]]:
    """Check the slow-test rule and the ratchet.

    One measurement in a busy shard is noisy (a test of 0.6 s once took 2.4 s), so with
    `remeasure` an unmarked test over the threshold fails only when it takes that long again,
    run on its own, and only from `confirm_threshold` (a margin above the threshold: a test that
    sits at the threshold would otherwise pass and fail on the same code).

    Parameters
    ----------
    tests
        Every test of the unit run.
    threshold
        From this many seconds an unmarked test fails.
    baseline
        The most tests that may be marked slow.
    confirm_threshold
        From this many seconds the solo re-run fails a test; None means `threshold`.
    remeasure
        Runs the given tests again and returns their seconds by node id; None trusts the first
        measurement. A test missing from its answer keeps its first measurement.

    Returns
    -------
    tuple of two lists of str
        The errors, and notes for the log (the count, and a request to lower the baseline).
    """
    confirm = threshold if confirm_threshold is None else confirm_threshold
    over = [t for t in tests if not t.slow and not t.skipped and t.seconds >= threshold]
    again = remeasure(over) if (remeasure is not None and over) else {}
    errors: list[str] = []
    notes: list[str] = []
    for test in over:
        second = again.get(test.nodeid)
        if second is not None and second < confirm:
            notes.append(
                f"{test.nodeid} took {test.seconds:.1f} s in its shard but {second:.1f} s on its "
                "own: counted as noise."
            )
            continue
        measured = f"{test.seconds:.1f} s" + (f" (and {second:.1f} s on its own)" if second else "")
        errors.append(
            f"{test.nodeid} took {measured}, and a unit test that takes "
            f"{threshold:g} s or more must be marked slow. Next step: make it faster (a "
            "smaller fixture, no real sleep, a shared app), or mark it @pytest.mark.slow; "
            f"the number of slow tests may not rise above the baseline in {BASELINE}, so a "
            f"new slow test needs another to become fast ({DOCS})."
        )
    count = sum(1 for test in tests if test.slow)
    verb = "is" if count == 1 else "are"
    notes.append(f"{_tests(count)} {verb} marked slow (baseline {baseline}).")
    if count > baseline:
        errors.append(
            f"{_tests(count)} {'is' if count == 1 else 'are'} marked slow, above the baseline of "
            f"{baseline} in {BASELINE}: the number may fall, never rise. Next step: make one of "
            f"the slow tests fast and remove its marker ({DOCS})."
        )
    elif count < baseline:
        notes.append(
            f"The number of slow tests fell to {count}: lower the baseline in this pull request "
            f"with `{UPDATE_COMMAND}`, or the room is spent by the next change."
        )
    return errors, notes


def file_durations(tests: Sequence[TestTime]) -> dict[str, float]:
    """Sum the seconds of the tests of each file.

    Parameters
    ----------
    tests
        Tests from junit reports.

    Returns
    -------
    dict
        Test file to seconds, rounded to milliseconds, sorted by file.
    """
    totals: dict[str, float] = {}
    for test in tests:
        totals[test.file] = totals.get(test.file, 0.0) + test.seconds
    return {file: round(totals[file], 2) for file in sorted(totals)}


def read_baseline(path: Path) -> int:
    """Read the number of tests that may be marked slow.

    Parameters
    ----------
    path
        ``scripts/slow-tests-baseline.json``.

    Returns
    -------
    int
        The baseline.

    Raises
    ------
    ValueError
        If the file is not ``{"slow_tests": N}`` with a whole N of zero or more.
    OSError
        If the file cannot be read.
    """
    text = path.read_text()
    try:
        value = json.loads(text)["slow_tests"]
    except (ValueError, KeyError, TypeError):
        value = None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f'{path} is not {{"slow_tests": N}} with a whole N of zero or more. '
            "Restore it from git."
        )
    return value


def remeasure(
    tests: Sequence[TestTime], workdir: Path, run: Callable[..., object] = subprocess.run
) -> dict[str, float]:
    """Run tests again, on their own and serially, under coverage as in the shards.

    Parameters
    ----------
    tests
        The tests to measure again.
    workdir
        A directory for the junit report and the coverage data, so neither touches the
        combined report of the run.
    run
        Runs the command; `subprocess.run` unless a test replaces it.

    Returns
    -------
    dict
        Seconds by node id, for the tests the new report holds; empty when it cannot be read.
    """
    report = workdir / "remeasured.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        *[test.nodeid for test in tests],
        "-q",
        "-p",
        "no:cacheprovider",
        "--cov",
        "--cov-report=",
        f"--junitxml={report}",
        "-o",
        "junit_family=xunit1",
    ]
    env = {**os.environ, "COVERAGE_FILE": str(workdir / ".coverage.remeasured")}
    run(command, env=env, check=False)
    try:
        return {t.nodeid: t.seconds for t in parse_junit(report.read_text()) if not t.skipped}
    except (OSError, ValueError):
        return {}


def _read_reports(paths: Sequence[Path]) -> list[TestTime]:
    """Read every junit report.

    Parameters
    ----------
    paths
        The reports.

    Returns
    -------
    list of TestTime
        Their tests, in order.

    Raises
    ------
    ValueError
        If a report cannot be read; the message names it.
    """
    tests: list[TestTime] = []
    for path in paths:
        try:
            tests.extend(parse_junit(path.read_text()))
        except (OSError, ValueError) as error:
            raise ValueError(f"{path}: {error}") from None
    return tests


def _run_jobs(args: argparse.Namespace) -> int:
    """Run the ``jobs`` command.

    Parameters
    ----------
    args
        Parsed arguments.

    Returns
    -------
    int
        Exit status.
    """
    try:
        budgets = read_budgets(BUDGETS)
        jobs = parse_jobs(args.listing.read_text())
    except (OSError, ValueError) as error:
        print(f"cannot check the job budgets: {error}", file=sys.stderr)
        return 1
    errors, notes = check_jobs(jobs, budgets)
    for note in notes:
        print(note)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def _run_slow_tests(args: argparse.Namespace) -> int:
    """Run the ``slow-tests`` command.

    Parameters
    ----------
    args
        Parsed arguments.

    Returns
    -------
    int
        Exit status.
    """
    try:
        budgets = read_budgets(BUDGETS)
        baseline = read_baseline(BASELINE)
        tests = _read_reports(args.reports)
    except (OSError, ValueError) as error:
        print(f"cannot check the slow tests: {error}", file=sys.stderr)
        return 1
    if not tests:
        print(
            "the junit reports hold no tests: check that every unit shard uploaded its report.",
            file=sys.stderr,
        )
        return 1
    with tempfile.TemporaryDirectory() as workdir:
        errors, notes = check_slow_tests(
            tests,
            threshold=budgets.threshold_seconds,
            baseline=baseline,
            confirm_threshold=budgets.confirm_seconds,
            remeasure=(lambda over: remeasure(over, Path(workdir))) if args.confirm else None,
        )
    for note in notes:
        print(note)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    count = sum(1 for test in tests if test.slow)
    if args.update and count < baseline:
        BASELINE.write_text(json.dumps({"slow_tests": count}) + "\n")
        print(f"Baseline lowered to {count}.")
    return 0


def _run_durations(args: argparse.Namespace) -> int:
    """Run the ``durations`` command.

    Parameters
    ----------
    args
        Parsed arguments.

    Returns
    -------
    int
        Exit status.
    """
    output: Path = args.output
    try:
        current = json.loads(output.read_text()) if output.exists() else {}
        measured = file_durations(_read_reports(args.reports))
    except (OSError, ValueError) as error:
        current, measured = None, {}
        problem = str(error)
    else:
        problem = "not a JSON object"
    if not isinstance(current, dict):
        print(
            f"cannot write {output} ({problem}): restore it from git, or delete it to start anew.",
            file=sys.stderr,
        )
        return 1
    merged = {**current, **measured}
    output.write_text(json.dumps({k: merged[k] for k in sorted(merged)}, indent=1) + "\n")
    print(f"{output}: {len(measured)} files measured, {len(merged)} in all.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one of the commands.

    Parameters
    ----------
    argv
        The arguments; None reads ``sys.argv``.

    Returns
    -------
    int
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    jobs = commands.add_parser("jobs", help="check each job of a run against its budget")
    jobs.add_argument("listing", type=Path, help="JSON lines of the run's jobs")
    jobs.set_defaults(run=_run_jobs)
    slow = commands.add_parser("slow-tests", help="check the slow-test rule and its ratchet")
    slow.add_argument("--update", action="store_true", help="lower the baseline after a fall")
    slow.add_argument(
        "--confirm",
        action="store_true",
        help="run an unmarked test over the threshold again on its own before failing it",
    )
    slow.add_argument("reports", type=Path, nargs="+", help="junit reports (xunit1)")
    slow.set_defaults(run=_run_slow_tests)
    durations = commands.add_parser("durations", help="write the seconds per test file")
    durations.add_argument("--output", type=Path, required=True)
    durations.add_argument("reports", type=Path, nargs="+", help="junit reports (xunit1)")
    durations.set_defaults(run=_run_durations)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
