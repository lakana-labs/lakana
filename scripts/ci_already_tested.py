"""Decide whether a commit pushed to main was tested already, so CI need not test it again.

After a squash merge of an up-to-date pull request, the tree on ``main`` is the tree of the
pull request's head commit, which CI ran on. The ``changes`` job of ``ci.yml`` runs this script
on a push to ``main``; it prints ``already_tested=true`` or ``already_tested=false`` for
``$GITHUB_OUTPUT`` and, on standard error, one line saying why.

It says ``true`` only when all of this is proven through the GitHub API:

* the commit is associated with exactly one pull request, which is merged, and whose merge
  commit is this commit;
* the pull request was merged into the repository's default branch, and its base was never
  changed (no ``base_ref_changed`` or ``base_ref_force_pushed`` event): a run that tested the
  head merged into another branch proves nothing about the head merged into ``main``;
* no other pull request has the same head commit: runs are found by head commit, so a run of
  another pull request (another base) would be taken for this one's;
* the commit has one parent, and that parent is in the history of the pull request's head
  commit (the pull request was up to date when it merged);
* the tree of the head commit is the tree of this commit;
* the latest ``pull_request`` run of ``ci.yml`` for the head commit has completed, and in it
  every job of `REQUIRED_JOBS` concluded ``success``. A skipped job (a draft, a
  documentation-only change) is not a success. "Latest" is by run number, so a failed re-run
  of an older run does not override a newer green run on the same tree (a re-run of the
  latest run is a new attempt of it and does count, through the jobs of its latest attempt).

Right after a merge the API can lag (no pull request yet, or no merge commit); that first read
is retried once after `RETRY_SECONDS`.

Every doubt resolves to ``false``, which runs everything: an API error, a timeout, a missing
field, a missing ``gh``, a page of results that may have been cut short. `main` never raises.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from typing import NamedTuple, TextIO

WORKFLOW_PATH = ".github/workflows/ci.yml"
"""The workflow whose run proves anything."""

REQUIRED_JOBS = (
    "test (3.11)",
    "test (3.14)",
    "integration",
    "five-minute",
    "compose-smoke",
    "docs-examples",
    "ui",
)
"""The jobs `main`'s CI skips for a tested commit; each must have succeeded in the pull request."""

RETRY_SECONDS = 10
"""The pause before the one retry of the pull request lookup, for the API's lag after a merge."""

EVENTS_PAGE = 100
"""The page size of the list reads; a list this long may have been cut short."""

BASE_CHANGES = ("base_ref_changed", "base_ref_force_pushed")
"""Events that mean the pull request's base is not what CI tested against."""

TIMEOUT_SECONDS = 30
"""How long one API call may take before it counts as a doubt."""

_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*")

Get = Callable[[str], object]
"""A function that takes a GitHub API path and returns the parsed JSON answer."""

Sleep = Callable[[float], object]
"""A function that pauses for a number of seconds."""


class NotTested(Exception):
    """A doubt: the commit is not proven to have been tested; the message says why."""


class Lagging(NotTested):
    """The pull request is not (yet) visible as merged: worth one retry, then a doubt."""


class Verdict(NamedTuple):
    """The answer of the check.

    Attributes
    ----------
    already_tested
        ``True`` only when every condition was proven.
    reason
        One line saying why.
    """

    already_tested: bool
    reason: str


def _dict(value: object, what: str) -> dict[str, object]:
    """Require a JSON object.

    Parameters
    ----------
    value
        A parsed JSON value.
    what
        What the value is, for the message.

    Returns
    -------
    dict
        `value`.

    Raises
    ------
    NotTested
        If `value` is not an object.
    """
    if not isinstance(value, dict):
        raise NotTested(f"{what} is not an object in the API answer")
    return value


def _list(value: object, what: str) -> list[object]:
    """Require a JSON list.

    Parameters
    ----------
    value
        A parsed JSON value.
    what
        What the value is, for the message.

    Returns
    -------
    list
        `value`.

    Raises
    ------
    NotTested
        If `value` is not a list.
    """
    if not isinstance(value, list):
        raise NotTested(f"{what} is not a list in the API answer")
    return value


def _text(obj: dict[str, object], key: str, what: str) -> str:
    """Require a non-empty string field.

    Parameters
    ----------
    obj
        A parsed JSON object.
    key
        The field.
    what
        What the object is, for the message.

    Returns
    -------
    str
        The field.

    Raises
    ------
    NotTested
        If the field is missing, not a string, or empty.
    """
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise NotTested(f"{what} has no usable `{key}` in the API answer")
    return value


def _number(obj: dict[str, object], key: str, what: str) -> int:
    """Require an integer field.

    Parameters
    ----------
    obj
        A parsed JSON object.
    key
        The field.
    what
        What the object is, for the message.

    Returns
    -------
    int
        The field.

    Raises
    ------
    NotTested
        If the field is missing or not an integer (a boolean is not one).
    """
    value = obj.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise NotTested(f"{what} has no usable `{key}` in the API answer")
    return value


def pick_pull(sha: str, pulls: object) -> tuple[int, str, str]:
    """Pick the one merged pull request whose merge commit is `sha`.

    Parameters
    ----------
    sha
        The commit pushed to main.
    pulls
        The answer of ``commits/{sha}/pulls``.

    Returns
    -------
    tuple
        The pull request's number, the head commit it had when it merged, and its base branch.

    Raises
    ------
    Lagging
        If the commit has no pull request, or the pull request has no merge commit or merge
        time yet: the API may be behind.
    NotTested
        If the commit has several pull requests, its merge commit is another one, or a field
        is missing.
    """
    found = _list(pulls, "the pull requests of the commit")
    if not found:
        raise Lagging("the commit is associated with 0 pull requests, not 1")
    if len(found) != 1:
        raise NotTested(f"the commit is associated with {len(found)} pull requests, not 1")
    pull = _dict(found[0], "the pull request")
    number = _number(pull, "number", "the pull request")
    if not pull.get("merged_at"):
        raise Lagging(f"pull request #{number} is not merged")
    merge_commit = pull.get("merge_commit_sha")
    if merge_commit is None:
        raise Lagging(f"pull request #{number} has no merge commit yet")
    if merge_commit != sha:
        raise NotTested(f"the merge commit of #{number} is {merge_commit}, not this commit")
    head = _text(_dict(pull.get("head"), "the head"), "sha", "the head of the pull request")
    if not _SHA.fullmatch(head):
        raise NotTested(f"the head of #{number} is not a full commit id: {head!r}")
    base = _text(_dict(pull.get("base"), "the base"), "ref", "the base of the pull request")
    return number, head, base


def check_default_base(base: str, repository: object) -> None:
    """Require that the pull request was merged into the repository's default branch.

    Parameters
    ----------
    base
        The base branch of the merged pull request.
    repository
        The answer of ``repos/{repository}``.

    Raises
    ------
    NotTested
        If the default branch is unknown or another branch than `base`.
    """
    default = _text(_dict(repository, "the repository"), "default_branch", "the repository")
    if base != default:
        raise NotTested(f"the pull request was merged into `{base}`, not into `{default}`")


def check_base_never_changed(events: object) -> None:
    """Require that the pull request's base was never changed.

    A run made while the pull request pointed at another branch tested the head merged into
    that branch; changing the base back starts no new run.

    Parameters
    ----------
    events
        The answer of ``issues/{number}/events`` (one page).

    Raises
    ------
    NotTested
        If the page may be cut short, an event has no name, or the base was changed.
    """
    found = _list(events, "the events of the pull request")
    if len(found) >= EVENTS_PAGE:
        raise NotTested("the events of the pull request may not fit on one page")
    for entry in found:
        name = _text(_dict(entry, "an event"), "event", "an event")
        if name in BASE_CHANGES:
            raise NotTested(f"the base of the pull request was changed ({name})")


def check_only_pull_of_head(number: int, head: str, pulls: object) -> None:
    """Require that no other pull request has the same head commit.

    Runs are found by head commit, so a run of another pull request, with another base, would
    be taken for this one's.

    Parameters
    ----------
    number
        The merged pull request.
    head
        Its head commit.
    pulls
        The answer of ``commits/{head}/pulls`` (one page).

    Raises
    ------
    NotTested
        If the page may be cut short, an entry is malformed, the pull request is not in the
        list, or another pull request has this head.
    """
    found = _list(pulls, "the pull requests of the head")
    if len(found) >= EVENTS_PAGE:
        raise NotTested("the pull requests of the head may not fit on one page")
    seen = False
    for entry in found:
        pull = _dict(entry, "a pull request of the head")
        other = _number(pull, "number", "a pull request of the head")
        pulled = _text(_dict(pull.get("head"), "its head"), "sha", f"the head of #{other}")
        if pulled != head:
            continue
        if other != number:
            raise NotTested(f"#{other} has the same head commit as #{number}")
        seen = True
    if not seen:
        raise NotTested(f"#{number} is not among the pull requests of its head")


def _tree(commit: object, what: str) -> str:
    """Read the tree object id of a commit.

    Parameters
    ----------
    commit
        The answer of ``commits/{sha}``.
    what
        What the commit is, for the message.

    Returns
    -------
    str
        The tree id.
    """
    inner = _dict(_dict(commit, what).get("commit"), what)
    return _text(_dict(inner.get("tree"), what), "sha", f"the tree of {what}")


def check_trees(commit: object, head_commit: object) -> str:
    """Require a single-parent commit whose tree is the pull request head's tree.

    Parameters
    ----------
    commit
        The answer of ``commits/{sha}`` for the pushed commit.
    head_commit
        The same for the pull request's head commit.

    Returns
    -------
    str
        The parent of the pushed commit.

    Raises
    ------
    NotTested
        If the commit has no parent or several (a merge commit that is not a squash), or
        the trees differ.
    """
    parents = _list(_dict(commit, "the commit").get("parents"), "the parents of the commit")
    if len(parents) != 1:
        raise NotTested(f"the commit has {len(parents)} parents; a squash merge has 1")
    parent = _text(_dict(parents[0], "the parent"), "sha", "the parent")
    tree, head_tree = _tree(commit, "the commit"), _tree(head_commit, "the head commit")
    if tree != head_tree:
        raise NotTested(f"the tree of the head ({head_tree[:12]}) is not this tree ({tree[:12]})")
    return parent


def check_ancestry(parent: str, head: str, comparison: object) -> None:
    """Require that the pull request's head contains the commit it was merged onto.

    Then CI's test merge of the head into the base was the head itself, so the run tested
    exactly the head's tree.

    Parameters
    ----------
    parent
        The parent of the pushed commit: the base the pull request was merged onto.
    head
        The pull request's head commit.
    comparison
        The answer of ``compare/{parent}...{head}``.

    Raises
    ------
    NotTested
        If the comparison does not say that the head is ahead of, or identical to, the parent.
    """
    status = _text(_dict(comparison, "the comparison"), "status", "the comparison")
    if status not in ("ahead", "identical"):
        raise NotTested(f"the base {parent[:12]} is not in the history of {head[:12]} ({status})")


def pick_run(answer: object, head: str) -> int:
    """Pick the latest CI run for a pull request's head and require that it has completed.

    "Latest" is the highest run number, so a failed re-run of an older run does not override a
    newer green run on the same tree; a re-run of the latest run is another attempt of it, and
    the jobs read afterwards are those of the latest attempt.

    Parameters
    ----------
    answer
        The answer of ``actions/runs`` for the head commit and the ``pull_request`` event.
    head
        The head commit.

    Returns
    -------
    int
        The id of the latest run of `WORKFLOW_PATH`.

    Raises
    ------
    NotTested
        If the list may be cut short, there is no run, or the latest one has not completed.
    """
    body = _dict(answer, "the runs")
    runs = _list(body.get("workflow_runs"), "the runs")
    if _number(body, "total_count", "the runs") > len(runs):
        raise NotTested("the runs of the head do not fit on one page")
    matching = []
    for entry in runs:
        run = _dict(entry, "a run")
        if (
            run.get("path") == WORKFLOW_PATH
            and run.get("event") == "pull_request"
            and run.get("head_sha") == head
        ):
            matching.append(run)
    if not matching:
        raise NotTested(f"no pull_request run of {WORKFLOW_PATH} for {head[:12]}")
    latest = max(matching, key=lambda run: _number(run, "run_number", "a run"))
    number = _number(latest, "run_number", "the latest run")
    status = _text(latest, "status", "the latest run")
    if status != "completed":
        raise NotTested(f"the latest run (#{number}) is {status}, not completed")
    return _number(latest, "id", "the latest run")


def check_jobs(answer: object) -> None:
    """Require that every job of `REQUIRED_JOBS` succeeded in the run.

    Parameters
    ----------
    answer
        The answer of ``actions/runs/{id}/jobs`` for the latest attempt.

    Raises
    ------
    NotTested
        If the list may be cut short, a required job is missing (as after a rename), or any
        job of that name concluded with anything but ``success`` (``skipped`` included).
    """
    body = _dict(answer, "the jobs")
    jobs = _list(body.get("jobs"), "the jobs")
    if _number(body, "total_count", "the jobs") > len(jobs):
        raise NotTested("the jobs of the run do not fit on one page")
    conclusions: dict[str, list[object]] = {}
    for entry in jobs:
        job = _dict(entry, "a job")
        conclusions.setdefault(_text(job, "name", "a job"), []).append(job.get("conclusion"))
    for name in REQUIRED_JOBS:
        if name not in conclusions:
            raise NotTested(f"the job `{name}` is not in the run")
        for conclusion in conclusions[name]:
            if conclusion != "success":
                raise NotTested(f"the job `{name}` concluded {conclusion}, not success")


def gh_api(path: str) -> object:
    """Call the GitHub API through ``gh api`` with the job's token and parse the answer.

    Parameters
    ----------
    path
        The API path, relative to the API root.

    Returns
    -------
    object
        The parsed JSON answer.

    Raises
    ------
    NotTested
        If ``gh`` is missing, fails, times out (rate limiting included), or does not answer
        with JSON.
    """
    try:
        done = subprocess.run(
            ["gh", "api", path],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotTested(f"`gh api {path}` could not run: {type(exc).__name__}") from exc
    if done.returncode != 0:
        raise NotTested(f"`gh api {path}` failed: {done.stderr.strip()[:200]}")
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise NotTested(f"`gh api {path}` did not answer with JSON") from exc


def _decide(repository: str, sha: str, get: Get, sleep: Sleep) -> Verdict:
    """Walk the conditions in order; the first doubt raises `NotTested`.

    Parameters
    ----------
    repository
        ``owner/name``.
    sha
        The commit pushed to main.
    get
        The API function.
    sleep
        Pauses for a number of seconds, between the two reads of the pull request lookup.

    Returns
    -------
    Verdict
        The positive verdict; a doubt raises instead.

    Raises
    ------
    NotTested
        On the first doubt.
    """
    if not _REPOSITORY.fullmatch(repository):
        raise NotTested(f"GITHUB_REPOSITORY is not of the form owner/name: {repository!r}")
    if not _SHA.fullmatch(sha):
        raise NotTested(f"GITHUB_SHA is not a full commit id: {sha!r}")
    base = f"repos/{repository}"
    pulls_path = f"{base}/commits/{sha}/pulls?per_page=100"
    try:
        number, head, branch = pick_pull(sha, get(pulls_path))
    except Lagging:
        sleep(RETRY_SECONDS)
        number, head, branch = pick_pull(sha, get(pulls_path))
    check_default_base(branch, get(base))
    check_base_never_changed(get(f"{base}/issues/{number}/events?per_page={EVENTS_PAGE}"))
    check_only_pull_of_head(
        number, head, get(f"{base}/commits/{head}/pulls?per_page={EVENTS_PAGE}")
    )
    parent = check_trees(get(f"{base}/commits/{sha}"), get(f"{base}/commits/{head}"))
    check_ancestry(parent, head, get(f"{base}/compare/{parent}...{head}?per_page=1"))
    run_id = pick_run(
        get(f"{base}/actions/runs?head_sha={head}&event=pull_request&per_page=100"), head
    )
    check_jobs(get(f"{base}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"))
    return Verdict(
        True,
        f"{sha[:12]} is the squash merge of #{number}; its head {head[:12]} has the same tree "
        f"and CI run {run_id} completed with {', '.join(REQUIRED_JOBS)} all successful",
    )


def decide(repository: str, sha: str, get: Get, sleep: Sleep = time.sleep) -> Verdict:
    """Decide whether the commit was tested already; any doubt or error says no.

    Parameters
    ----------
    repository
        ``owner/name``.
    sha
        The commit pushed to main.
    get
        The API function, taking a path and returning the parsed answer.
    sleep
        Pauses for a number of seconds; tests pass a function that does not wait.

    Returns
    -------
    Verdict
        ``already_tested`` is ``True`` only if every condition was proven.
    """
    try:
        return _decide(repository, sha, get, sleep)
    except NotTested as exc:
        return Verdict(False, str(exc))
    except Exception as exc:  # the contract is "never raises": a bug here must run everything
        return Verdict(False, f"{type(exc).__name__}: {exc}")


def main(
    environ: Mapping[str, str] | None = None,
    get: Get = gh_api,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    sleep: Sleep = time.sleep,
) -> int:
    """Print ``already_tested=true|false`` and the reason.

    Parameters
    ----------
    environ
        The environment; ``os.environ`` if omitted. ``GITHUB_REPOSITORY`` and ``GITHUB_SHA``
        are read from it.
    get
        The API function.
    stdout
        Where the output line goes, for ``$GITHUB_OUTPUT``; ``sys.stdout`` if omitted.
    stderr
        Where the reason goes, for the job log; ``sys.stderr`` if omitted.
    sleep
        Pauses for a number of seconds, for the retry.

    Returns
    -------
    int
        Always ``0``; the answer is the output.
    """
    env = os.environ if environ is None else environ
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    verdict = decide(env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_SHA", ""), get, sleep)
    label = "already tested" if verdict.already_tested else "not already tested"
    print(f"already_tested={'true' if verdict.already_tested else 'false'}", file=out)
    print(f"{label}: {verdict.reason}", file=err)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
