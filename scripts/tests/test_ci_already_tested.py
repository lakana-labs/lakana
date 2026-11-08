"""Tests for the check that a commit on main was tested already, and for the workflow around it."""

from __future__ import annotations

import copy
import io
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import ci_already_tested as cat
from scripts.ci_already_tested import NotTested

REPO = "owner/repo"
SHA = "a" * 40
HEAD = "b" * 40
PARENT = "c" * 40
TREE = "d" * 40
OTHER_TREE = "e" * 40
RUN_ID = 777

JOBS = (
    "lint",
    "test (3.11)",
    "test (3.14)",
    "integration",
    "five-minute",
    "compose-smoke",
    "docs-examples",
    "ui",
    "docs",
)


def pull(**changes: object) -> dict:
    """Build a pull request as the commit-pulls API returns it.

    Parameters
    ----------
    **changes
        Top-level fields to replace.

    Returns
    -------
    dict
        A merged pull request whose merge commit is `SHA` and whose head is `HEAD`.
    """
    base = {
        "number": 42,
        "merged_at": "2026-10-06T19:37:09Z",
        "merge_commit_sha": SHA,
        "head": {"sha": HEAD},
        "base": {"ref": "main"},
    }
    return {**base, **changes}


def commit(tree: str = TREE, parents: tuple[str, ...] = (PARENT,)) -> dict:
    """Build a commit as the commits API returns it.

    Parameters
    ----------
    tree
        The tree object id.
    parents
        The parent commit ids.

    Returns
    -------
    dict
        The part of the answer the check reads.
    """
    return {"commit": {"tree": {"sha": tree}}, "parents": [{"sha": p} for p in parents]}


def run(number: int = 5, **changes: object) -> dict:
    """Build a workflow run as the runs API returns it.

    Parameters
    ----------
    number
        The run number.
    **changes
        Fields to replace.

    Returns
    -------
    dict
        A completed ``pull_request`` run of ``ci.yml`` for `HEAD`.
    """
    base = {
        "id": RUN_ID + number,
        "run_number": number,
        "path": ".github/workflows/ci.yml",
        "event": "pull_request",
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
    }
    return {**base, **changes}


def runs_answer(*runs: dict, total: int | None = None) -> dict:
    """Wrap runs as the runs API answers.

    Parameters
    ----------
    *runs
        The runs.
    total
        The ``total_count``; the number of runs if omitted.

    Returns
    -------
    dict
        The API answer.
    """
    return {"total_count": len(runs) if total is None else total, "workflow_runs": list(runs)}


def jobs_answer(conclusions: dict[str, str | None] | None = None, total: int | None = None) -> dict:
    """Build the jobs API answer of a run in which every job succeeded.

    Parameters
    ----------
    conclusions
        Conclusions to replace by job name; a name not in `JOBS` adds a job and ``None``
        removes the job.
    total
        The ``total_count``; the number of jobs if omitted.

    Returns
    -------
    dict
        The API answer.
    """
    merged: dict[str, str | None] = {name: "success" for name in JOBS}
    merged.update(conclusions or {})
    jobs = [{"name": n, "conclusion": c} for n, c in merged.items() if c is not None]
    return {"total_count": len(jobs) if total is None else total, "jobs": jobs}


def api_paths() -> dict[str, str]:
    """Name the API paths the check asks for.

    Returns
    -------
    dict
        Short name to path.
    """
    return {
        "pulls": f"repos/{REPO}/commits/{SHA}/pulls?per_page=100",
        "commit": f"repos/{REPO}/commits/{SHA}",
        "head": f"repos/{REPO}/commits/{HEAD}",
        "compare": f"repos/{REPO}/compare/{PARENT}...{HEAD}?per_page=1",
        "runs": f"repos/{REPO}/actions/runs?head_sha={HEAD}&event=pull_request&per_page=100",
        "jobs": f"repos/{REPO}/actions/runs/{RUN_ID + 5}/jobs?filter=latest&per_page=100",
        "repo": f"repos/{REPO}",
        "events": f"repos/{REPO}/issues/42/events?per_page=100",
        "head_pulls": f"repos/{REPO}/commits/{HEAD}/pulls?per_page=100",
    }


def happy_answers() -> dict[str, object]:
    """Build the answers for which the check says "already tested".

    Returns
    -------
    dict
        API path to answer.
    """
    paths = api_paths()
    return {
        paths["pulls"]: [pull()],
        paths["commit"]: commit(),
        paths["head"]: commit(parents=("f" * 40,)),
        paths["compare"]: {"status": "ahead"},
        paths["runs"]: runs_answer(run(1, conclusion="failure"), run(5)),
        paths["jobs"]: jobs_answer(),
        paths["repo"]: {"default_branch": "main"},
        paths["events"]: [{"event": "closed"}, {"event": "head_ref_deleted"}, {"event": "merged"}],
        paths["head_pulls"]: [pull()],
    }


def fake_api(answers: dict[str, object]) -> Callable[[str], object]:
    """Make a `get` function that answers from a table and fails for what is not in it.

    Parameters
    ----------
    answers
        API path to answer; an exception instance is raised instead of returned.

    Returns
    -------
    callable
        A function taking a path.
    """

    def get(path: str) -> object:
        """Answer one request.

        Parameters
        ----------
        path
            The API path.

        Returns
        -------
        object
            The canned answer.

        Raises
        ------
        NotTested
            For a path that is not in the table, as the real function does on an API error.
        Exception
            The canned exception.
        """
        if path not in answers:
            raise NotTested(f"unexpected request {path}")
        answer = answers[path]
        if isinstance(answer, Exception):
            raise answer
        return copy.deepcopy(answer)

    return get


SLEPT: list[float] = []


def decide_now(get: Callable[[str], object]) -> cat.Verdict:
    """Run `decide` for the test repository and commit without really sleeping.

    Parameters
    ----------
    get
        The API function.

    Returns
    -------
    cat.Verdict
        The verdict; the pauses it asked for are in `SLEPT`.
    """
    SLEPT.clear()
    return cat.decide(REPO, SHA, get, sleep=SLEPT.append)


def test_the_interface_s_gates_are_part_of_the_proof() -> None:
    """``main`` skips the ``ui`` job only when the pull request's ``ui`` job succeeded."""
    assert "ui" in cat.REQUIRED_JOBS


def test_a_squash_merged_pull_request_whose_last_run_succeeded_is_already_tested() -> None:
    """The one case that says true, with the reason naming the pull request and the run."""
    verdict = decide_now(fake_api(happy_answers()))
    assert verdict.already_tested is True
    assert "#42" in verdict.reason
    assert str(RUN_ID + 5) in verdict.reason
    assert HEAD[:12] in verdict.reason


# One test per condition: every doubt says "not tested".
DOUBTS = {
    "an error of the API": ("pulls", RuntimeError("HTTP 403: rate limit exceeded"), "RuntimeError"),
    "no pull request": ("pulls", [], "0 pull requests"),
    "two pull requests": ("pulls", [pull(), pull(number=43)], "2 pull requests"),
    "a pull request that is not merged": ("pulls", [pull(merged_at=None)], "not merged"),
    "another merge commit": ("pulls", [pull(merge_commit_sha="9" * 40)], "merge commit"),
    "a merge commit with two parents": (
        "commit",
        commit(parents=(PARENT, "9" * 40)),
        "2 parents",
    ),
    "another tree": ("head", commit(tree=OTHER_TREE), "tree"),
    "a base that the head does not contain": ("compare", {"status": "diverged"}, "diverged"),
    "a head that is behind": ("compare", {"status": "behind"}, "behind"),
    "no run of the head": ("runs", runs_answer(), "no pull_request run"),
    "runs of another workflow": (
        "runs",
        runs_answer(run(path=".github/workflows/security.yml")),
        "no pull_request run",
    ),
    "runs of another event": ("runs", runs_answer(run(event="push")), "no pull_request run"),
    "runs of another commit": ("runs", runs_answer(run(head_sha="9" * 40)), "no pull_request run"),
    "a run that is still going": (
        "runs",
        runs_answer(run(5, status="in_progress", conclusion=None)),
        "in_progress",
    ),
    "a latest run that was cancelled": (
        "jobs",
        jobs_answer({"integration": "cancelled"}),
        "integration",
    ),
    "a job that failed on a re-run": (
        "jobs",
        jobs_answer({"test (3.14)": "failure"}),
        "test (3.14)",
    ),
    "a draft run, the jobs skipped": ("jobs", jobs_answer({"integration": "skipped"}), "skipped"),
    "a documentation-only run": (
        "jobs",
        jobs_answer({"five-minute": "skipped", "compose-smoke": "skipped"}),
        "skipped",
    ),
    "a job that was renamed": ("jobs", jobs_answer({"test (3.11)": None}), "test (3.11)"),
    "a job without a conclusion": ("jobs", jobs_answer({"docs-examples": None}), "docs-examples"),
    "more jobs than one page": ("jobs", jobs_answer(total=300), "page"),
    "more runs than one page": ("runs", runs_answer(run(5), total=300), "page"),
    "an answer that is not a list": ("pulls", {"message": "Not Found"}, "list"),
    "a pull request merged into another branch": (
        "pulls",
        [pull(base={"ref": "release"})],
        "release",
    ),
    "a default branch that is another one": ("repo", {"default_branch": "trunk"}, "trunk"),
    "a repository answer without a default branch": ("repo", {}, "default_branch"),
    "a base that was changed": (
        "events",
        [{"event": "labeled"}, {"event": "base_ref_changed"}],
        "base",
    ),
    "a base that was force-pushed": ("events", [{"event": "base_ref_force_pushed"}], "base"),
    "an events page that may be cut short": (
        "events",
        [{"event": "labeled"}] * 100,
        "page",
    ),
    "an events answer that is not a list": ("events", {"message": "Not Found"}, "list"),
    "an event without a name": ("events", [{}], "event"),
    "an event that is not an object": ("events", [None], "event"),
    "another pull request with the same head": (
        "head_pulls",
        [pull(), pull(number=43)],
        "#43",
    ),
    "the pull request missing from those of its head": (
        "head_pulls",
        [pull(number=43, head={"sha": "9" * 40})],
        "#42",
    ),
    "pull requests of the head that may be cut short": (
        "head_pulls",
        [pull(number=n, head={"sha": "9" * 40}) for n in range(1, 99)] + [pull(), pull(number=99)],
        "page",
    ),
    "a head answer that is not a list": ("head_pulls", None, "list"),
    "an entry of the head's pull requests without a head": (
        "head_pulls",
        [pull(), pull(number=43, head=None)],
        "head",
    ),
    "an entry of the head's pull requests without a number": (
        "head_pulls",
        [pull(), pull(number="43")],
        "number",
    ),
}


@pytest.mark.parametrize("case", sorted(DOUBTS))
def test_every_doubt_says_not_tested(case: str) -> None:
    """Each way the proof can fail ends in false, with a reason that says which."""
    key, answer, fragment = DOUBTS[case]
    answers = happy_answers()
    answers[api_paths()[key]] = answer
    verdict = decide_now(fake_api(answers))
    assert verdict.already_tested is False
    assert fragment in verdict.reason


def test_pull_requests_of_the_head_with_another_head_do_not_matter() -> None:
    """Only a pull request whose head is this very commit shares it; others do not count."""
    answers = happy_answers()
    others = [pull(number=n, head={"sha": "9" * 40}) for n in range(100, 150)]
    answers[api_paths()["head_pulls"]] = [pull(), *others]
    assert decide_now(fake_api(answers)).already_tested is True


def test_an_events_page_of_99_is_complete() -> None:
    """99 events cannot have been cut at 100; a base change among them still counts."""
    answers = happy_answers()
    answers[api_paths()["events"]] = [{"event": "labeled"}] * 99
    assert decide_now(fake_api(answers)).already_tested is True


def lagging(paths: dict[str, object], first: object, then: object) -> Callable[[str], object]:
    """Make an API that answers the pull request lookup differently the first time.

    Parameters
    ----------
    paths
        The answers of the other requests.
    first
        The first answer to ``commits/{sha}/pulls``.
    then
        Every later answer to it.

    Returns
    -------
    callable
        A function taking a path.
    """
    pulls_path = api_paths()["pulls"]
    calls = {"n": 0}
    inner = fake_api(paths)

    def get(path: str) -> object:
        """Answer one request.

        Parameters
        ----------
        path
            The API path.

        Returns
        -------
        object
            The canned answer.
        """
        if path == pulls_path:
            calls["n"] += 1
            return copy.deepcopy(first if calls["n"] == 1 else then)
        return inner(path)

    return get


@pytest.mark.parametrize(
    "first", [[], [pull(merge_commit_sha=None)], [pull(merged_at=None, merge_commit_sha=None)]]
)
def test_the_lookup_is_retried_once_after_a_pause_for_api_lag(first: object) -> None:
    """Right after a merge the commit's pull requests can be empty or lack the merge commit."""
    verdict = decide_now(lagging(happy_answers(), first, [pull()]))
    assert verdict.already_tested is True
    assert SLEPT == [cat.RETRY_SECONDS]


def test_a_lookup_that_stays_empty_is_not_tested_after_one_retry() -> None:
    """One retry, not a loop: the second empty answer decides."""
    verdict = decide_now(lagging(happy_answers(), [], []))
    assert verdict.already_tested is False
    assert "0 pull requests" in verdict.reason
    assert SLEPT == [cat.RETRY_SECONDS]


def test_a_lookup_that_answers_at_once_does_not_pause() -> None:
    """No lag, no wait."""
    decide_now(fake_api(happy_answers()))
    assert SLEPT == []


def test_a_wrong_merge_commit_is_not_retried() -> None:
    """Another merge commit is not lag: it is final."""
    verdict = decide_now(lagging(happy_answers(), [pull(merge_commit_sha="9" * 40)], [pull()]))
    assert verdict.already_tested is False
    assert SLEPT == []


def test_an_unknown_request_is_not_tested() -> None:
    """If the check asks for something the API does not answer, it runs everything."""
    verdict = decide_now(fake_api({}))
    assert verdict.already_tested is False


def test_the_latest_run_decides_not_the_best_one() -> None:
    """A newer run that failed after an older success is a failure: it is the latest."""
    answers = happy_answers()
    answers[api_paths()["runs"]] = runs_answer(run(5, status="queued", conclusion=None), run(2))
    verdict = decide_now(fake_api(answers))
    assert verdict.already_tested is False
    assert "queued" in verdict.reason


def test_a_run_of_a_force_pushed_head_is_not_a_run_of_the_merged_head() -> None:
    """Runs belong to the commit the pull request had when it merged, not to an earlier one."""
    answers = happy_answers()
    answers[api_paths()["runs"]] = runs_answer(run(head_sha="1" * 40))
    assert decide_now(fake_api(answers)).already_tested is False


def test_an_extra_job_does_not_matter_and_an_identical_tree_with_a_rebase_is_accepted() -> None:
    """Jobs beyond the six (``docs``, ``lint``, one added later) are not part of the proof."""
    answers = happy_answers()
    answers[api_paths()["jobs"]] = jobs_answer({"new-job": "failure", "lint": "failure"})
    answers[api_paths()["compare"]] = {"status": "identical"}
    assert decide_now(fake_api(answers)).already_tested is True


@pytest.mark.parametrize("duplicate", ["success", "failure"])
def test_a_job_name_that_appears_twice_needs_every_one_green(duplicate: str) -> None:
    """Two jobs of one name, as after a rename that kept the old one around, both count."""
    answers = happy_answers()
    jobs = jobs_answer()
    jobs["jobs"].append({"name": "integration", "conclusion": duplicate})
    jobs["total_count"] += 1
    answers[api_paths()["jobs"]] = jobs
    assert decide_now(fake_api(answers)).already_tested is (duplicate == "success")


@pytest.mark.parametrize(
    "answer",
    [
        [None],
        [{"number": 1}],
        [pull(head={"sha": "short"})],
        [pull(number="42")],
        [pull(number=True)],
        [pull(head={})],
        [pull(head=None)],
        [pull(head={"sha": ""})],
        [pull(base=None)],
        [pull(base={})],
        [pull(base={"ref": ""})],
    ],
)
def test_a_pull_request_answer_that_is_malformed_is_not_tested(answer: list) -> None:
    """A missing field, a field of the wrong type or an empty one is a doubt."""
    with pytest.raises(NotTested):
        cat.pick_pull(SHA, answer)


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {},
        {"commit": {}},
        {"commit": {"tree": {"sha": ""}}, "parents": []},
        commit(parents=()),
    ],
)
def test_a_commit_answer_that_is_malformed_is_not_tested(answer: object) -> None:
    """A commit without a tree or without a parent proves nothing."""
    with pytest.raises(NotTested):
        cat.check_trees(answer, commit())


def test_the_jobs_and_runs_answers_must_be_lists_of_objects() -> None:
    """A malformed list entry, or no list at all, is a doubt."""
    with pytest.raises(NotTested):
        cat.pick_run({"total_count": 1, "workflow_runs": [None]}, HEAD)
    with pytest.raises(NotTested):
        cat.pick_run({"total_count": 1}, HEAD)
    with pytest.raises(NotTested):
        cat.check_jobs({"total_count": 1, "jobs": [{"name": "x"}]})
    with pytest.raises(NotTested):
        cat.check_jobs([])


def test_the_base_and_the_head_checks_are_pure_functions_over_the_answers() -> None:
    """The three new checks accept the good answer and raise on the malformed ones."""
    cat.check_default_base("main", {"default_branch": "main"})
    cat.check_base_never_changed([{"event": "merged"}])
    cat.check_only_pull_of_head(42, HEAD, [pull()])
    with pytest.raises(NotTested):
        cat.check_default_base("main", None)
    with pytest.raises(NotTested):
        cat.check_base_never_changed(None)
    with pytest.raises(NotTested):
        cat.check_only_pull_of_head(42, HEAD, None)


def test_a_malformed_tree_in_the_pull_request_s_head_is_not_tested() -> None:
    """The head commit's answer is read as strictly as the pushed commit's."""
    with pytest.raises(NotTested):
        cat.check_trees(commit(), {"commit": {"tree": None}})


def test_the_ancestry_answer_must_name_a_status() -> None:
    """A compare answer without a status is a doubt; ``ahead`` and ``identical`` are not."""
    with pytest.raises(NotTested):
        cat.check_ancestry(PARENT, HEAD, {})
    cat.check_ancestry(PARENT, HEAD, {"status": "ahead"})
    cat.check_ancestry(PARENT, HEAD, {"status": "identical"})


# The real API function.
class Completed:
    """A stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        """Store the parts of a finished process.

        Parameters
        ----------
        returncode
            The exit status.
        stdout
            Captured standard output.
        stderr
            Captured standard error.
        """
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_gh_api_returns_the_parsed_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """It runs ``gh api <path>`` with a timeout and parses the JSON."""
    calls: list[tuple] = []

    def fake_run(argv: list[str], **kwargs: object) -> Completed:
        """Record the call and answer with JSON.

        Parameters
        ----------
        argv
            The command.
        **kwargs
            Keyword arguments of ``subprocess.run``.

        Returns
        -------
        Completed
            A successful process.
        """
        calls.append((argv, kwargs))
        return Completed(0, '{"a": 1}')

    monkeypatch.setattr(cat.subprocess, "run", fake_run)
    assert cat.gh_api("repos/o/r/x") == {"a": 1}
    argv, kwargs = calls[0]
    assert argv == ["gh", "api", "repos/o/r/x"]
    assert kwargs["timeout"] == cat.TIMEOUT_SECONDS
    assert kwargs["check"] is False


@pytest.mark.parametrize(
    "outcome",
    [
        Completed(1, "", "HTTP 403: API rate limit exceeded"),
        Completed(0, "<html>not json</html>"),
        FileNotFoundError("gh"),
        subprocess.TimeoutExpired("gh", 30),
    ],
)
def test_gh_api_failures_are_not_tested(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    """A failing call, a missing ``gh``, a timeout and a non-JSON answer are all doubts."""

    def fake_run(argv: list[str], **kwargs: object) -> Completed:
        """Fail or answer as the case says.

        Parameters
        ----------
        argv
            The command.
        **kwargs
            Keyword arguments of ``subprocess.run``.

        Returns
        -------
        Completed
            The outcome, if it is not an exception.

        Raises
        ------
        Exception
            The outcome, if it is one.
        """
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cat.subprocess, "run", fake_run)
    with pytest.raises(NotTested):
        cat.gh_api("repos/o/r/x")


def run_main(env: dict[str, str], get: Callable[[str], object]) -> tuple[int, str, str]:
    """Run ``main`` and capture what it prints.

    Parameters
    ----------
    env
        The environment variables.
    get
        The API function.

    Returns
    -------
    tuple
        Exit status, standard output, standard error.
    """
    out, err = io.StringIO(), io.StringIO()
    status = cat.main(environ=env, get=get, stdout=out, stderr=err, sleep=lambda _seconds: None)
    return status, out.getvalue(), err.getvalue()


ENV = {"GITHUB_REPOSITORY": REPO, "GITHUB_SHA": SHA}


def test_main_prints_the_output_line_and_the_reason() -> None:
    """Standard output is one ``already_tested=`` line; the reason goes to standard error."""
    status, out, err = run_main(ENV, fake_api(happy_answers()))
    assert (status, out) == (0, "already_tested=true\n")
    assert err.startswith("already tested:")
    assert err.count("\n") == 1


def test_main_says_false_with_a_reason_when_not_proven() -> None:
    """A direct push to main has no pull request: false, and the log says why."""
    answers = happy_answers()
    answers[api_paths()["pulls"]] = []
    status, out, err = run_main(ENV, fake_api(answers))
    assert (status, out) == (0, "already_tested=false\n")
    assert err.startswith("not already tested:")
    assert err.count("\n") == 1


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"GITHUB_REPOSITORY": REPO},
        {"GITHUB_SHA": SHA},
        {"GITHUB_REPOSITORY": REPO, "GITHUB_SHA": "main"},
        {"GITHUB_REPOSITORY": REPO, "GITHUB_SHA": SHA + "0"},
        {"GITHUB_REPOSITORY": REPO, "GITHUB_SHA": SHA.upper()},
        {"GITHUB_REPOSITORY": "no-slash", "GITHUB_SHA": SHA},
        {"GITHUB_REPOSITORY": "o/r/../x", "GITHUB_SHA": SHA},
        {"GITHUB_REPOSITORY": "o/r?x=1", "GITHUB_SHA": SHA},
    ],
)
def test_main_refuses_an_environment_it_cannot_trust(env: dict[str, str]) -> None:
    """A missing or odd repository or commit never reaches the API: false."""

    def get(path: str) -> object:
        """Fail the test if the API is asked anything.

        Parameters
        ----------
        path
            The API path.

        Raises
        ------
        AssertionError
            Always.
        """
        raise AssertionError(path)

    status, out, err = run_main(env, get)
    assert (status, out) == (0, "already_tested=false\n")
    assert "not already tested" in err


def test_main_never_raises() -> None:
    """Whatever goes wrong inside, even a bug, prints false and exits 0."""

    def get(path: str) -> object:
        """Fail with an error nobody planned for.

        Parameters
        ----------
        path
            The API path.

        Raises
        ------
        ZeroDivisionError
            Always.
        """
        raise ZeroDivisionError(path)

    status, out, err = run_main(ENV, get)
    assert (status, out) == (0, "already_tested=false\n")
    assert "ZeroDivisionError" in err


def test_main_defaults_to_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Called with no arguments, it reads ``os.environ`` and prints to the real streams."""
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    assert cat.main() == 0
    captured = capsys.readouterr()
    assert captured.out == "already_tested=false\n"
    assert "GITHUB_SHA" in captured.err


def test_the_script_runs_as_a_program() -> None:
    """The workflow runs the file as a program, with no package around it."""
    script = Path(cat.__file__)
    result = subprocess.run(
        [sys.executable, str(script)],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "already_tested=false\n"
