"""The deploy-time checks of the jobs in `lakana.yaml`."""

from __future__ import annotations

from test_projects_dag import make_asset

from lakana_core.projects.dag import build_graph
from lakana_core.projects.issues import ProjectIssue
from lakana_core.projects.jobs import job_issues
from lakana_core.projects.parse import ParsedAsset
from lakana_core.projects.project_config import parse_project_config

_ASSETS = [
    make_asset("silver.a", ("bronze.src",)),
    make_asset("gold.b", ("silver.a",)),
]


def _issues(
    text: str,
    assets: list[ParsedAsset] | None = None,
    environment: str = "prod",
    deployed: frozenset[str] = frozenset(),
) -> list[ProjectIssue]:
    assets = _ASSETS if assets is None else assets
    config = parse_project_config(text).resolved_for(environment)
    return job_issues(
        config,
        assets,
        build_graph(assets),
        config_text=text,
        environment=environment,
        deployed_jobs=deployed,
    )


def test_jobs_that_select_assets_pass() -> None:
    assert _issues('jobs:\n  a:\n    select: ["gold.*", "silver.a"]\n') == []


def test_no_jobs_is_no_issue() -> None:
    assert _issues("") == []


def test_a_selection_matching_no_asset_is_refused_naming_the_pattern_and_the_line() -> None:
    (issue,) = _issues('jobs:\n  a:\n    select: ["gold.*", "plat.*"]\n')
    assert (issue.path, issue.line) == ("lakana.yaml", 3)
    assert "job 'a'" in issue.message
    assert "'plat.*' matches no asset" in issue.message
    assert "--select" not in issue.message


def test_the_hint_for_a_namespace_is_kept() -> None:
    (issue,) = _issues('jobs:\n  a:\n    select: ["gold"]\n')
    assert "did you mean 'gold.*'?" in issue.message


def test_every_failing_job_gets_its_own_issue() -> None:
    issues = _issues('jobs:\n  a:\n    select: ["x"]\n  b:\n    select: ["y"]\n')
    assert [issue.line for issue in issues] == [3, 5]


def test_a_project_without_assets_refuses_its_jobs() -> None:
    (issue,) = _issues('jobs:\n  a:\n    select: ["x"]\n', assets=[])
    assert "job 'a'" in issue.message
    assert "no assets" in issue.message


def test_the_selection_is_checked_as_the_environment_resolves_it() -> None:
    text = (
        'jobs:\n  a:\n    select: ["gold.*"]\nenvironments:\n  dev:\n    jobs:\n'
        '      a: { select: ["nope.*"] }\n'
    )
    assert _issues(text, environment="prod") == []
    (issue,) = _issues(text, environment="dev")
    assert issue.line == 7
    assert "'nope.*'" in issue.message


_SPACED = 'jobs:\n  "nightly ":\n    select: ["gold.*"]\n'


def test_a_new_job_name_with_an_outer_space_is_refused_with_the_sentence_of_other_names() -> None:
    (issue,) = _issues(_SPACED)
    assert (issue.path, issue.line) == ("lakana.yaml", 2)
    assert issue.message == (
        "Job name 'nightly ' ends with a space, so it would look like 'nightly' in every list "
        "but be a different name. Use 'nightly' instead, or another name without a space at "
        "either end."
    )


def test_a_job_name_with_an_outer_space_that_the_environment_deployed_keeps_deploying() -> None:
    assert _issues(_SPACED, deployed=frozenset({"nightly "})) == []


def test_a_renamed_job_with_an_outer_space_is_refused() -> None:
    text = 'jobs:\n  " weekly":\n    select: ["gold.*"]\n'
    (issue,) = _issues(text, deployed=frozenset({"nightly "}))
    assert "Job name ' weekly' starts with a space" in issue.message


def test_a_deployed_job_can_be_renamed_to_a_name_without_the_space() -> None:
    text = 'jobs:\n  nightly:\n    select: ["gold.*"]\n'
    assert _issues(text, deployed=frozenset({"nightly "})) == []
