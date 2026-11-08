"""Tests for the pull-request convention check (branch name, footers, issue references)."""

import re
from pathlib import Path

import pytest

from scripts.check_conventional import (
    check_branch,
    check_breaking_change_footer,
    check_issue_references,
    main,
    strip_non_prose,
)


@pytest.mark.parametrize(
    "branch",
    [
        "feat/m4b-cli",
        "fix/audit-cursor",
        "docs/release-notes",
        "ci/conventional-changelog",
        "chore/bump-0.3",
        "refactor/sdk-query-params",
        "perf/append-decoder",
        "test/cli-e2e",
        "build/uv-lock",
        "revert/feat-m4b-cli",
    ],
)
def test_conventional_branch_names_pass(branch: str) -> None:
    """`<type>/<lowercase-description>` passes for every commit type."""
    assert check_branch(branch) is None


@pytest.mark.parametrize(
    "branch",
    ["release-please--branches--main--components--lakana", "dependabot/uv/ruff-0.16.8"],
)
def test_bot_branches_are_exempt(branch: str) -> None:
    """Branches opened by release-please and Dependabot are not ours to name."""
    assert check_branch(branch) is None


@pytest.mark.parametrize(
    "branch",
    ["m4b-cli", "feature/x", "feat/", "feat/Upper", "feat/space here", "feat/-leading", "deps/x"],
)
def test_other_branch_names_fail(branch: str) -> None:
    """A missing or unknown type, an empty or non-lowercase description, fails."""
    error = check_branch(branch)
    assert error is not None
    assert branch in error


def test_a_non_breaking_title_needs_no_footer() -> None:
    """Only a `!` title requires the footer."""
    assert check_breaking_change_footer("feat(cli): add login", "Adds login.") is None


def test_a_breaking_title_needs_a_breaking_change_footer() -> None:
    """`feat!:` with no footer fails, naming what to add."""
    error = check_breaking_change_footer("feat(server)!: paginate audit events", "Summary.")
    assert error is not None
    assert "BREAKING CHANGE:" in error


def test_a_breaking_footer_in_the_last_paragraph_passes() -> None:
    """The footer sits in the final paragraph, alongside `Closes` trailers."""
    body = (
        "Summary of the change.\n\n"
        "BREAKING CHANGE: GET /api/v1/audit-events now returns {items, next_cursor}.\n"
        "Closes #100"
    )
    assert check_breaking_change_footer("feat(server)!: paginate audit events", body) is None


def test_a_breaking_footer_buried_mid_body_fails() -> None:
    """A footer followed by more prose is not parsed as a footer by release-please."""
    body = "BREAKING CHANGE: shape changed.\n\nMore prose after it."
    assert check_breaking_change_footer("fix!: change shape", body) is not None


def test_an_empty_breaking_change_note_fails() -> None:
    """`BREAKING CHANGE:` with no migration text is not enough."""
    assert check_breaking_change_footer("feat!: x", "Summary.\n\nBREAKING CHANGE:   ") is not None


def test_a_breaking_body_footer_without_bang_passes() -> None:
    """A footer alone (no `!`) is valid Conventional Commits and needs nothing else."""
    body = "Summary.\n\nBREAKING CHANGE: renamed a field."
    assert check_breaking_change_footer("feat(sdk): rename field", body) is None


@pytest.mark.parametrize(
    "body",
    ["Closes #1\nCloses #2", "Fixes #12", "closes #3 and resolves #4", "No issues."],
)
def test_one_keyword_per_issue_passes(body: str) -> None:
    """Each referenced issue has its own closing keyword."""
    assert check_issue_references(body) is None


@pytest.mark.parametrize(
    "body", ["Closes #125, #126", "Fixes #1 #2", "Resolves #7, #8 and #9", "closes #1,#2"]
)
def test_a_keyword_followed_by_several_issues_fails(body: str) -> None:
    """GitHub closes only the first issue after one keyword."""
    error = check_issue_references(body)
    assert error is not None
    assert "one closing keyword per issue" in error


def test_main_reports_every_violation_and_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """All failures are printed together; the exit code is 1."""
    env = {"HEAD_REF": "m4b-cli", "PR_TITLE": "feat!: x", "PR_BODY": "Closes #1, #2"}

    assert main(env) == 1

    err = capsys.readouterr().err
    assert "m4b-cli" in err
    assert "BREAKING CHANGE:" in err
    assert "one closing keyword per issue" in err


def test_main_passes_a_conforming_pull_request() -> None:
    """A conventional branch, title and body exit 0."""
    env = {
        "HEAD_REF": "feat/m4b-cli",
        "PR_TITLE": "feat(cli)!: add the lakana CLI",
        "PR_BODY": "Summary.\n\nBREAKING CHANGE: none really.\nCloses #19",
    }
    assert main(env) == 0


def test_main_tolerates_a_missing_body() -> None:
    """A PR with an empty description is checked as an empty body."""
    assert main({"HEAD_REF": "fix/x", "PR_TITLE": "fix: x"}) == 0


def test_html_comments_in_the_description_are_ignored() -> None:
    """The PR template's guidance comments never count as footers or references."""
    body = (
        "<!-- e.g. never write 'Closes #1, #2' -->\n\nSummary.\n\n"
        "<!--\nBREAKING CHANGE: example only\n-->"
    )
    env = {"HEAD_REF": "feat/x", "PR_TITLE": "feat!: x", "PR_BODY": body}

    assert check_issue_references(body.replace("<!--", "").replace("-->", "")) is not None
    assert main(env) == 1  # the commented-out footer doesn't satisfy the "!" rule...
    assert main({**env, "PR_TITLE": "feat: x"}) == 0  # ...and the comment isn't a violation


def test_a_placeholder_breaking_change_note_fails() -> None:
    """The template's `<...>` placeholder left in place is not a migration note."""
    body = "Summary.\n\nBREAKING CHANGE: <what changed and how to migrate>"
    assert check_breaking_change_footer("feat!: x", body) is not None


def test_code_in_the_description_is_ignored() -> None:
    """GitHub doesn't act on keywords inside inline code or fenced blocks; neither do we."""
    body = "Rejects `Closes #1, #2` now.\n\n```\nFixes #3, #4\n```\n\nCloses #5"

    assert main({"HEAD_REF": "ci/x", "PR_TITLE": "ci: x", "PR_BODY": body}) == 0
    assert strip_non_prose("a `Closes #1, #2` b") == "a  b"


_ROOT = Path(__file__).resolve().parents[2]


def test_commitlint_and_claude_md_list_the_same_scopes() -> None:
    """The scopes commitlint accepts are the ones CLAUDE.md rule 7 names, ``ui`` among them."""
    config = (_ROOT / "commitlint.config.mjs").read_text()
    enum = re.search(r'"scope-enum":\s*\[\s*2,\s*"always",\s*\[(.*?)\]', config, re.S)
    assert enum, "commitlint.config.mjs has no scope-enum"
    linted = re.findall(r'"([a-z-]+)"', enum.group(1))
    rule = re.search(r"Optional scope from: (.*?)\.\n", (_ROOT / "CLAUDE.md").read_text(), re.S)
    assert rule, "CLAUDE.md names no scopes"
    named = re.findall(r"`([a-z-]+)`", rule.group(1))
    assert linted == named
    assert "ui" in linted
