"""The workflow that closes pull requests nobody invited (`outside-pull-requests.yml`).

It runs on `pull_request_target`, with the base repository's token, so the tests pin what makes
that safe: it checks out and runs nothing from the pull request, writes only pull requests, and
takes the event's values through the environment. The filter that decides whether an issue
invites the pull request runs here on fixed API answers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "outside-pull-requests.yml"
REPOSITORY = "lakana-labs/lakana"


def _workflow() -> dict:
    """Return the parsed workflow.

    Returns
    -------
    dict
        The workflow as YAML loads it; the key ``on`` is read by PyYAML as ``True``.
    """
    return yaml.safe_load(WORKFLOW.read_text())


def _job() -> dict:
    """Return the workflow's one job.

    Returns
    -------
    dict
        The job ``close-uninvited``.
    """
    return _workflow()["jobs"]["close-uninvited"]


def _step() -> dict:
    """Return the job's one step.

    Returns
    -------
    dict
        The step that decides and closes.
    """
    (step,) = _job()["steps"]
    return step


def _issue(repository: str, state: str, *labels: str) -> dict:
    """Return one closing issue as the GraphQL query answers it.

    Parameters
    ----------
    repository : str
        The issue's repository, ``owner/name``.
    state : str
        ``OPEN`` or ``CLOSED``.
    *labels : str
        The issue's labels.

    Returns
    -------
    dict
        The node.
    """
    return {
        "state": state,
        "repository": {"nameWithOwner": repository},
        "labels": {"nodes": [{"name": label} for label in labels]},
    }


def _invited(*issues: dict) -> str:
    """Run the workflow's filter with jq on an answer listing these closing issues.

    Parameters
    ----------
    *issues : dict
        The closing issues, from :func:`_issue`.

    Returns
    -------
    str
        What jq prints: ``true`` or ``false``.
    """
    jq = shutil.which("jq")
    if jq is None:
        # In CI a missing jq must not pass unnoticed as six skipped tests.
        if os.environ.get("CI"):
            pytest.fail("jq is not installed on this runner")
        pytest.skip("jq is not installed")
    answer = {
        "data": {
            "repository": {"pullRequest": {"closingIssuesReferences": {"nodes": list(issues)}}}
        }
    }
    result = subprocess.run(
        [jq, "--arg", "repo", REPOSITORY, _step()["env"]["INVITED"]],
        input=json.dumps(answer),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_it_starts_when_a_pull_request_is_opened_or_reopened() -> None:
    """Reopened counts too, so that reopening is no way around it."""
    assert _workflow()[True] == {"pull_request_target": {"types": ["opened", "reopened"]}}


def test_it_spares_maintainers_collaborators_and_bots() -> None:
    """Owners, members, collaborators and bots are never closed."""
    condition = " ".join(_job()["if"].split())
    assert "github.event.pull_request.user.type != 'Bot'" in condition
    assert (
        '!contains(fromJSON(\'["OWNER", "MEMBER", "COLLABORATOR"]\'),'
        " github.event.pull_request.author_association)" in condition
    )


def test_only_the_author_reopening_starts_it_again() -> None:
    """A maintainer who reopens a closed pull request keeps it open."""
    condition = " ".join(_job()["if"].split())
    assert (
        "(github.event.action == 'opened' ||"
        " github.event.sender.login == github.event.pull_request.user.login)" in condition
    )


def test_it_writes_only_pull_requests() -> None:
    """Read-only at the top; the job may comment on and close a pull request, nothing more."""
    assert _workflow()["permissions"] == {"contents": "read"}
    assert _job()["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "write",
    }


def test_it_checks_out_and_runs_nothing_from_the_pull_request() -> None:
    """No action and no checkout: the job's one step talks to the API through `gh`."""
    assert "uses" not in _step()
    assert "actions/checkout" not in WORKFLOW.read_text()


def test_the_event_reaches_the_script_only_through_the_environment() -> None:
    """No `${{ }}` inside `run`, so a title or a branch name can never become shell code."""
    assert "${{" not in _step()["run"]


def test_strings_are_passed_as_strings() -> None:
    """`-F` would read a name such as ``true`` or ``@file`` as something else."""
    assert '-f owner="$OWNER" -f repo="$REPO" -F number="$PR"' in _step()["run"]


def test_a_failed_api_call_fails_the_run_instead_of_closing() -> None:
    """Without pipefail, jq on an empty answer would print nothing and the pull request close."""
    assert _step()["run"].startswith("set -o pipefail")


def test_a_pull_request_that_closes_no_issue_is_not_invited() -> None:
    """No closing issue, no invitation."""
    assert _invited() == "false"


def test_an_open_issue_of_this_repository_labelled_pr_welcome_invites() -> None:
    """The invitation is the label on an issue the pull request closes."""
    assert _invited(_issue(REPOSITORY, "OPEN", "type:bug", "pr-welcome")) == "true"
    assert _invited(_issue("Lakana-Labs/Lakana", "OPEN", "pr-welcome")) == "true"


def test_an_issue_in_another_repository_never_invites() -> None:
    """Anyone can label an issue of their own and write `Closes them/repo#1`."""
    assert _invited(_issue("someone/elsewhere", "OPEN", "pr-welcome")) == "false"


def test_a_closed_issue_no_longer_invites() -> None:
    """An invitation ends with its issue."""
    assert _invited(_issue(REPOSITORY, "CLOSED", "pr-welcome")) == "false"


def test_only_the_exact_label_invites() -> None:
    """A label that merely starts with the name does not count."""
    assert _invited(_issue(REPOSITORY, "OPEN", "pr-welcome-x", "pr")) == "false"


def test_one_inviting_issue_among_others_is_enough() -> None:
    """The pull request may close several issues."""
    issues = (
        _issue("someone/elsewhere", "OPEN", "pr-welcome"),
        _issue(REPOSITORY, "OPEN", "type:feat"),
        _issue(REPOSITORY, "OPEN", "pr-welcome"),
    )
    assert _invited(*issues) == "true"


def test_the_closing_comment_points_to_the_issue_tracker_and_the_guide() -> None:
    """The person learns what to do instead, with links that work from the comment."""
    script = _step()["run"]
    assert 'gh pr close "$PR"' in script
    assert "https://github.com/$OWNER/$REPO/issues/new/choose" in script
    assert "https://github.com/$OWNER/$REPO/blob/main/.github/CONTRIBUTING.md" in script


def test_the_job_has_a_timeout() -> None:
    """A stuck API call cannot hold a runner."""
    assert _job()["timeout-minutes"] == 5
