"""The workflow that closes pull requests nobody invited (`outside-pull-requests.yml`).

It runs on `pull_request_target`, with the base repository's token, so the tests pin what makes
that safe: it starts only when a pull request is opened, checks out and runs nothing from it,
writes only pull requests, and takes the event's values through the environment.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "outside-pull-requests.yml"


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


def test_it_starts_only_when_a_pull_request_is_opened() -> None:
    """A maintainer who reopens a closed pull request keeps it open."""
    assert _workflow()[True] == {"pull_request_target": {"types": ["opened"]}}


def test_it_spares_maintainers_collaborators_and_bots() -> None:
    """Owners, members, collaborators and bots are never closed."""
    condition = " ".join(_job()["if"].split())
    assert "github.event.pull_request.user.type != 'Bot'" in condition
    assert (
        '!contains(fromJSON(\'["OWNER", "MEMBER", "COLLABORATOR"]\'),'
        " github.event.pull_request.author_association)" in condition
    )


def test_it_writes_only_pull_requests() -> None:
    """Read-only at the top; the job may comment on and close a pull request, nothing more."""
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    assert _job()["permissions"] == {
        "contents": "read",
        "issues": "read",
        "pull-requests": "write",
    }


def test_it_checks_out_and_runs_nothing_from_the_pull_request() -> None:
    """No action and no checkout: the job's one step talks to the API through `gh`."""
    steps = _job()["steps"]
    assert all("uses" not in step for step in steps)
    assert "checkout" not in WORKFLOW.read_text().replace("checks out", "")


def test_the_event_reaches_the_script_only_through_the_environment() -> None:
    """No `${{ }}` inside `run`, so a title or a branch name can never become shell code."""
    for step in _job()["steps"]:
        assert "${{" not in step["run"]


def test_an_issue_labelled_pr_welcome_keeps_the_pull_request_open() -> None:
    """The invitation is the label on an issue the pull request closes."""
    script = _job()["steps"][0]["run"]
    assert "closingIssuesReferences" in script
    assert 'index("pr-welcome") != null' in script
    assert re.search(r'if \[ "\$invited" = "true" \]; then\n.*\n\s*exit 0', script)


def test_the_closing_comment_points_to_the_issue_tracker() -> None:
    """The person learns what to do instead."""
    script = _job()["steps"][0]["run"]
    assert 'gh pr close "$PR"' in script
    assert "/issues/new/choose" in script
    assert "CONTRIBUTING.md" in script


def test_the_job_has_a_timeout() -> None:
    """A stuck API call cannot hold a runner."""
    assert _job()["timeout-minutes"] == 5
