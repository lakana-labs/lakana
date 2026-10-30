"""The strict `lakana.yaml` model and its per-environment deep merge."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pydantic
import pytest

from lakana_core.projects.issues import ProjectIssue, ProjectValidationError
from lakana_core.projects.limits import DeployLimits
from lakana_core.projects.project_config import (
    NEEDS_MAIN_LANGUAGE,
    SUPPORTED_TASK_PYTHONS,
    ChecksDefaults,
    EnvironmentOverride,
    JobOverride,
    ProjectConfig,
    ScheduleOverride,
    checked_label,
    parse_project_config,
)

_FULL = """\
repo: analytics
defaults:
  owner: data-eng
  materialize: table
requirements:
  fields: [description, owner]
  column_docs: false
properties:
  team_channel: "#data"
environments:
  prod:
    requirements: { column_docs: true }
"""


def test_full_example_parses() -> None:
    """The spec's example parses with every section."""
    config = parse_project_config(_FULL)
    assert config.repo == "analytics"
    assert config.defaults.owner == "data-eng"
    assert config.requirements.fields == ("description", "owner")
    assert config.properties == {"team_channel": "#data"}


def test_empty_file_is_all_defaults() -> None:
    """An empty `lakana.yaml` is valid."""
    config = parse_project_config("")
    resolved = config.resolved_for("dev")
    assert resolved.repo is None
    assert resolved.requirements.column_docs is False
    assert resolved.defaults.materialize is None


def test_override_is_deep_merged_for_its_environment_only() -> None:
    """`prod` gets column_docs without losing the base `fields`; `dev` keeps the base."""
    config = parse_project_config(_FULL)
    prod = config.resolved_for("prod")
    assert prod.requirements.column_docs is True
    assert prod.requirements.fields == ("description", "owner")
    assert prod.defaults.owner == "data-eng"
    assert config.resolved_for("dev").requirements.column_docs is False


def test_override_for_an_unknown_environment_is_allowed_and_ignored() -> None:
    """Ephemeral environments come and go; an override for one that is absent is fine."""
    config = parse_project_config("environments:\n  pr-123:\n    defaults: {owner: x}\n")
    assert config.resolved_for("dev").defaults.owner is None


def test_unknown_top_level_key_is_an_error() -> None:
    """Strict: a key the model does not know is refused, with its line."""
    with pytest.raises(ProjectValidationError, match=r"lakana.yaml:1: triggers: Extra inputs"):
        parse_project_config("triggers: []\n")


def test_unknown_key_inside_an_override_is_an_error() -> None:
    """Overrides are as strict as the base."""
    with pytest.raises(ProjectValidationError, match=r"environments\.prod\.schedule"):
        parse_project_config("environments:\n  prod:\n    schedule: daily\n")


def test_unknown_required_field_is_an_error() -> None:
    """`requirements.fields` accepts only fields front-matter can set."""
    with pytest.raises(ProjectValidationError, match=r"requirements.fields.0"):
        parse_project_config("requirements:\n  fields: [colour]\n")


def test_dotted_repo_name_is_an_error() -> None:
    """`repo:` names a registered repository, which may not contain a dot."""
    with pytest.raises(ProjectValidationError, match=r"must not contain a '.'"):
        parse_project_config("repo: a.b\n")


def test_non_mapping_document_is_an_error() -> None:
    """A list at the top level is refused with a clear message."""
    with pytest.raises(ProjectValidationError, match="must be a mapping"):
        parse_project_config("- a\n- b\n")


def test_properties_must_be_strings() -> None:
    """`properties` is a string map; a number is refused rather than coerced."""
    with pytest.raises(ProjectValidationError, match=r"properties\.retries"):
        parse_project_config("properties:\n  retries: 3\n")


def test_yaml_errors_become_issues_with_a_line() -> None:
    """An alias in `lakana.yaml` is reported as an issue on its line."""
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config("a: 1\nb: *x\n")
    assert info.value.issues[0].path == "lakana.yaml"
    assert info.value.issues[0].line == 2


def test_size_limit_applies() -> None:
    """A `lakana.yaml` over the limit is refused."""
    with pytest.raises(ProjectValidationError, match="larger than"):
        parse_project_config("repo: x\n" + "#" * 2048, limits=DeployLimits(yaml_bytes=1024))


def test_crlf_and_bom_parse_identically() -> None:
    """A Windows-written file parses like a Unix one."""
    windows = "﻿" + _FULL.replace("\n", "\r\n")
    assert parse_project_config(windows) == parse_project_config(_FULL)


def test_explicit_null_repo_is_allowed() -> None:
    """`repo: null` is the same as leaving it out."""
    assert parse_project_config("repo: null\n").repo is None


@pytest.mark.parametrize("text", ["a: 2024-13-45\n", "[" * 2000 + "]" * 2000])
def test_hostile_yaml_is_a_project_validation_error(text: str) -> None:
    """Constructor errors and deep nesting surface as issues on `lakana.yaml`."""
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    assert info.value.issues[0].path == "lakana.yaml"


def test_retry_settings_in_defaults_and_environment_overrides() -> None:
    """`defaults:` takes the three run settings; an override replaces them per environment."""
    config = parse_project_config(
        "defaults:\n  retries: 1\n  retry_delay: 10s\n  timeout: 5m\n"
        "environments:\n  prod:\n    defaults:\n      retries: 4\n"
    )
    assert (config.defaults.retries, config.defaults.retry_delay, config.defaults.timeout) == (
        1,
        10,
        300,
    )
    prod = config.resolved_for("prod").defaults
    assert (prod.retries, prod.retry_delay, prod.timeout) == (4, 10, 300)


@pytest.mark.parametrize(
    ("block", "field"),
    [
        ("retries: -1", "defaults.retries"),
        ("retries: yes", "defaults.retries"),
        ("retries: 1.5", "defaults.retries"),
        ("retry_delay: soon", "defaults.retry_delay"),
        ("timeout: 0", "defaults.timeout"),
    ],
)
def test_bad_retry_settings_are_issues(block: str, field: str) -> None:
    """Negative retries, unparsable durations and a zero timeout are refused."""
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(f"defaults:\n  {block}\n")
    assert field in info.value.issues[0].message


@pytest.mark.parametrize("value", ['"true"', "1", '"yes"'])
def test_column_docs_must_be_a_real_boolean(value: str) -> None:
    """A quoted or numeric value is refused rather than coerced to a boolean."""
    with pytest.raises(ProjectValidationError, match=r"requirements\.column_docs"):
        parse_project_config(f"requirements:\n  column_docs: {value}\n")


def test_duplicate_and_merge_keys_are_issues_with_a_line() -> None:
    """Both are refused in `lakana.yaml` with the file and line."""
    with pytest.raises(ProjectValidationError, match="duplicate key 'repo'") as info:
        parse_project_config("repo: a\nrepo: b\n")
    assert (info.value.issues[0].path, info.value.issues[0].line) == ("lakana.yaml", 2)
    with pytest.raises(ProjectValidationError, match="merge keys") as info:
        parse_project_config("defaults:\n  <<: {owner: x}\n")
    assert info.value.issues[0].line == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", '"\\uD800"'),
        ("owner", '"a\\0b"'),
        ("owner", '"a\\u2028b"'),
        ("tags", '["a\\nb"]'),
        ("description", '"a\\0b"'),
        ("description", '"a\\u202eb"'),
        ("description", '"\\uD800"'),
    ],
)
def test_defaults_obey_the_character_rule(field: str, value: str) -> None:
    """`lakana.yaml` defaults are merged without re-validation, so they must be clean."""
    with pytest.raises(ProjectValidationError):
        parse_project_config(f"defaults:\n  {field}: {value}\n")
    with pytest.raises(ProjectValidationError):
        parse_project_config(f"environments:\n  prod:\n    defaults:\n      {field}: {value}\n")


def test_merged_metadata_of_both_asset_kinds_is_clean() -> None:
    from lakana_core.projects.discovery import DiscoveredAsset
    from lakana_core.projects.frontmatter import unsafe_free_text, unsafe_identifier
    from lakana_core.projects.parse import parse_assets
    from lakana_core.projects.python_assets import parse_python_asset

    defaults = parse_project_config(
        "defaults:\n  owner: platform\n  tags: [a]\n"
        '  description: "Wetter \\U0001F468\\u200D\\U0001F469"\n'
    ).defaults
    python = parse_python_asset(
        DiscoveredAsset(
            "assets/g/p.py",
            "g.p",
            "from lakana import asset\n@asset()\ndef f():\n    pass\n",
            "python",
        ),
        defaults,
    )
    (sql,) = parse_assets(
        [DiscoveredAsset("assets/g/s.sql", "g.s", "SELECT 1 AS x\n")], defaults=defaults
    )
    for parsed in (python, sql):
        assert not isinstance(parsed, list)
        metadata = parsed.metadata
        assert metadata.owner is not None and not unsafe_identifier(metadata.owner)
        assert not any(unsafe_identifier(tag) for tag in metadata.tags)
        assert metadata.description is not None and not unsafe_free_text(metadata.description)


def test_an_incremental_key_obeys_the_identifier_rule() -> None:
    from lakana_core.projects.frontmatter import parse_front_matter

    block = (
        '/* lakana\nmaterialize: incremental\nincremental_key: "k\\0\\uD800"\n*/\nSELECT 1 AS k\n'
    )
    with pytest.raises(ProjectValidationError):
        parse_front_matter(block, path="a.sql")


@pytest.mark.parametrize(
    "text",
    [
        'defaults:\n  tags: [x]\nbogus_key_x: 1\n"a\\nassets/x.sql:1: forged": 1\n',
        'defaults:\n  owner: [1]\n"a\\nforged": 1\n',
    ],
)
def test_a_forged_key_cannot_make_a_second_issue_line(text: str) -> None:
    with pytest.raises(ProjectValidationError) as caught:
        parse_project_config(text)
    for issue in caught.value.issues:
        assert "\n" not in issue.message, issue.message
        assert "\n" not in str(issue)
    assert any("forged" in issue.message for issue in caught.value.issues)


def test_a_forged_front_matter_column_key_is_one_line() -> None:
    from lakana_core.projects.frontmatter import parse_front_matter

    block = '/* lakana\ncolumns:\n  "a\\nassets/x.sql:1: forged": 1\n*/\nSELECT 1\n'
    with pytest.raises(ProjectValidationError) as caught:
        parse_front_matter(block, path="a.sql")
    assert all("\n" not in issue.message for issue in caught.value.issues)
    assert any("forged" in issue.message for issue in caught.value.issues)


def _config(text: str) -> ProjectConfig:
    """Parse a `lakana.yaml`.

    Parameters
    ----------
    text
        The file's content.

    Returns
    -------
    ProjectConfig
        The validated configuration.
    """
    return parse_project_config(text)


def _issues(text: str) -> list[ProjectIssue]:
    """Parse a `lakana.yaml` that must be refused.

    Parameters
    ----------
    text
        The file's content.

    Returns
    -------
    list of ProjectIssue
        Every issue the parse reported.
    """
    with pytest.raises(ProjectValidationError) as caught:
        parse_project_config(text)
    return list(caught.value.issues)


def test_a_project_sets_the_default_for_its_checks() -> None:
    config = _config("checks:\n  on_failure: warn\n")
    assert config.resolved_for("dev").checks.on_failure == "warn"


def test_a_project_without_the_section_blocks() -> None:
    assert _config("").resolved_for("dev").checks.on_failure == "block"


def test_an_environment_overrides_the_default() -> None:
    config = _config(
        "checks:\n  on_failure: warn\n"
        "environments:\n  prod:\n    checks:\n      on_failure: block\n"
    )
    assert (
        config.resolved_for("dev").checks.on_failure,
        config.resolved_for("prod").checks.on_failure,
    ) == ("warn", "block")


@pytest.mark.parametrize(
    ("where", "prefix"),
    [
        ("checks:\n  on_failure: keep\n", "checks.on_failure: "),
        (
            "environments:\n  dev:\n    checks:\n      on_failure: keep\n",
            "environments.dev.checks.on_failure: ",
        ),
    ],
)
def test_keep_is_refused_as_a_default(where: str, prefix: str) -> None:
    (issue,) = _issues(where)
    assert issue.message == (
        f"{prefix}must be block or warn (got 'keep'). `keep` leaves failing data where readers "
        "find it, so it can only be set on a check: put `on_failure: keep` in that check's "
        "/* lakana */ block."
    )


def test_keep_in_both_places_is_two_issues() -> None:
    issues = _issues(
        "checks:\n  on_failure: keep\nenvironments:\n  dev:\n    checks:\n      on_failure: keep\n"
    )
    assert [issue.message.split(":")[0] for issue in issues] == [
        "checks.on_failure",
        "environments.dev.checks.on_failure",
    ]


def test_any_other_value_is_refused_by_name() -> None:
    (issue,) = _issues("checks:\n  on_failure: stop\n")
    assert issue.message == "checks.on_failure: Input should be 'block' or 'warn'"


@pytest.mark.parametrize(
    "text",
    [
        "checks: [a, b]\n",
        "checks: warn\n",
        "checks:\nenvironments:\n  dev:\n    checks: keep\n",
        "checks: {on_failure: [keep]}\n",
        "environments: [a]\n",
        "environments: keep\n",
        "environments:\n  dev: [keep]\n  prod:\n",
        "environments:\n  dev:\n    checks: [keep]\n",
    ],
)
def test_the_keep_pre_check_survives_any_shape(text: str) -> None:
    issues = _issues(text)
    assert issues
    assert not any("must be block or warn (got 'keep')" in issue.message for issue in issues)


@pytest.mark.parametrize(
    "value", ["Keep", "KEEP", '" keep"', "yes", "true", "on", "1", "null"], ids=repr
)
def test_a_near_miss_of_keep_is_refused_by_the_general_rule(value: str) -> None:
    """Only the exact word `keep` gets the long text; anything else is a plain refusal."""
    (issue,) = _issues(f"checks:\n  on_failure: {value}\n")
    assert issue.message.startswith("checks.on_failure: Input should be 'block' or 'warn'")
    assert "can only be set on a check" not in issue.message


def test_keep_beside_another_wrong_value_reports_both_once() -> None:
    issues = _issues("checks:\n  on_failure: keep\n  bogus: 1\n")
    messages = [issue.message for issue in issues]
    assert sum("got 'keep'" in m for m in messages) == 1
    assert len(messages) == 2
    assert any("bogus" in m for m in messages)


def test_the_model_itself_refuses_keep_as_a_default() -> None:
    """The long text is the pre-check's; the `Literal` still refuses `keep` on its own."""
    with pytest.raises(pydantic.ValidationError):
        ChecksDefaults.model_validate({"on_failure": "keep"})


_JOBS = """\
jobs:
  daily_gold:
    select: ["gold.*"]
    upstream: true
    downstream: true
    schedule:
      cron: "0 6 * * *"
      timezone: Europe/Berlin
  hourly_silver:
    select: ["silver.*"]
    schedule: { cron: "15 * * * *" }
  by_hand:
    select: ["bronze.*"]
environments:
  dev:
    jobs:
      daily_gold: { schedule: null }
      hourly_silver: { schedule: { cron: "0 * * * *" } }
"""


def test_a_job_with_every_field_parses() -> None:
    job = parse_project_config(_JOBS).jobs["daily_gold"]
    assert job.select == ["gold.*"]
    assert (job.upstream, job.downstream) == (True, True)
    assert job.schedule is not None
    assert (job.schedule.cron, job.schedule.timezone) == ("0 6 * * *", "Europe/Berlin")


def test_a_job_defaults_to_no_closure_and_a_utc_schedule() -> None:
    config = parse_project_config(_JOBS)
    silver = config.jobs["hourly_silver"]
    assert (silver.upstream, silver.downstream) == (False, False)
    assert silver.schedule is not None
    assert silver.schedule.timezone == "UTC"
    assert config.jobs["by_hand"].schedule is None


def test_a_file_without_jobs_has_none() -> None:
    assert parse_project_config("").jobs == {}


def test_schedule_null_in_an_override_removes_the_schedule() -> None:
    config = parse_project_config(_JOBS)
    assert config.resolved_for("dev").jobs["daily_gold"].schedule is None
    assert config.resolved_for("dev").jobs["daily_gold"].select == ["gold.*"]
    assert config.resolved_for("prod").jobs["daily_gold"].schedule is not None


def test_an_override_merges_into_the_schedule() -> None:
    schedule = parse_project_config(_JOBS).resolved_for("dev").jobs["hourly_silver"].schedule
    assert schedule is not None
    assert (schedule.cron, schedule.timezone) == ("0 * * * *", "UTC")


def test_an_override_can_change_the_selection() -> None:
    config = parse_project_config(
        'jobs:\n  a:\n    select: ["x.*"]\nenvironments:\n  dev:\n    jobs:\n'
        '      a: { select: ["y.*"], upstream: true }\n'
    )
    job = config.resolved_for("dev").jobs["a"]
    assert (job.select, job.upstream) == (["y.*"], True)


def test_an_override_naming_an_unknown_job_is_refused_with_the_jobs_that_exist() -> None:
    text = (
        'jobs:\n  daily:\n    select: ["a.*"]\n  hourly:\n    select: ["b.*"]\n'
        "environments:\n  dev:\n    jobs:\n      dayly: { schedule: null }\n"
    )
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    (issue,) = info.value.issues
    assert issue.line == 9
    assert "environments.dev.jobs.dayly" in issue.message
    assert "daily, hourly" in issue.message
    assert "cannot add one" in issue.message


def test_an_override_for_a_job_when_none_is_declared_says_so() -> None:
    with pytest.raises(ProjectValidationError, match="declares no jobs"):
        parse_project_config("environments:\n  dev:\n    jobs:\n      a: { schedule: null }\n")


@pytest.mark.parametrize("name", ["a.b", "a/b", "..", "' '", '"a\\nb"'])
def test_a_job_name_that_breaks_the_asset_name_rules_is_refused(name: str) -> None:
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(f'jobs:\n  {name}:\n    select: ["x.*"]\n')
    assert info.value.issues[0].line == 2
    assert info.value.issues[0].message.startswith("jobs.")


@pytest.mark.parametrize(
    ("cron", "needle"),
    [
        ("* * * * * *", "seconds"),
        ("0 0 0 * * *", "5 fields"),
        ("61 * * * *", "minute"),
        ("0 25 * * *", "hour"),
    ],
)
def test_a_bad_cron_is_refused_with_the_field_and_the_line(cron: str, needle: str) -> None:
    text = f'jobs:\n  a:\n    select: ["x.*"]\n    schedule:\n      cron: "{cron}"\n'
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    (issue,) = info.value.issues
    assert (issue.path, issue.line) == ("lakana.yaml", 5)
    assert issue.message.startswith("jobs.a.schedule.cron: ")
    assert needle in issue.message


def test_a_bad_cron_in_an_override_is_refused_with_its_line() -> None:
    text = (
        'jobs:\n  a:\n    select: ["x.*"]\nenvironments:\n  dev:\n    jobs:\n      a:\n'
        '        schedule:\n          cron: "* * * *"\n'
    )
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    assert info.value.issues[0].line == 9


def test_an_unknown_time_zone_is_refused_with_a_hint() -> None:
    text = (
        'jobs:\n  a:\n    select: ["x.*"]\n    schedule:\n      cron: "0 6 * * *"\n'
        "      timezone: Berlin\n"
    )
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    (issue,) = info.value.issues
    assert issue.line == 6
    assert "jobs.a.schedule.timezone" in issue.message
    assert "'Berlin'" in issue.message
    assert "Europe/Berlin" in issue.message


def test_a_job_without_a_selection_or_with_an_empty_one_is_refused() -> None:
    with pytest.raises(ProjectValidationError, match=r"jobs\.a\.select"):
        parse_project_config("jobs:\n  a:\n    upstream: true\n")
    with pytest.raises(ProjectValidationError, match=r"jobs\.a\.select"):
        parse_project_config("jobs:\n  a:\n    select: []\n")


def test_an_unknown_job_key_is_refused() -> None:
    with pytest.raises(ProjectValidationError, match=r"jobs\.a\.cron"):
        parse_project_config('jobs:\n  a:\n    select: ["x"]\n    cron: "* * * * *"\n')


_NO_SCHEDULE_BASE = 'jobs:\n  a:\n    select: ["x.*"]\n'


@pytest.mark.parametrize(
    "override",
    [
        "{ schedule: { timezone: Europe/Berlin } }",
        "{ schedule: {} }",
    ],
)
def test_an_override_that_leaves_a_schedule_without_cron_is_a_lakana_yaml_error(
    override: str,
) -> None:
    text = f"{_NO_SCHEDULE_BASE}environments:\n  dev:\n    jobs:\n      a: {override}\n"
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    (issue,) = info.value.issues
    assert (issue.path, issue.line) == ("lakana.yaml", 7)
    assert issue.message.startswith("environments.dev.jobs.a.schedule: ")
    assert "cron" in issue.message
    assert "no schedule" in issue.message


def test_an_override_that_gives_a_schedule_its_cron_is_fine() -> None:
    text = (
        f"{_NO_SCHEDULE_BASE}environments:\n  dev:\n    jobs:\n"
        '      a: { schedule: { cron: "0 6 * * *" } }\n'
    )
    schedule = parse_project_config(text).resolved_for("dev").jobs["a"].schedule
    assert schedule is not None
    assert (schedule.cron, schedule.timezone) == ("0 6 * * *", "UTC")


def test_resolved_for_turns_a_merge_that_does_not_validate_into_a_project_error() -> None:
    config = parse_project_config(_NO_SCHEDULE_BASE)
    broken = config.model_copy(
        update={
            "environments": {
                "dev": EnvironmentOverride.model_construct(
                    jobs={
                        "a": JobOverride.model_construct(
                            schedule=ScheduleOverride.model_construct(timezone="Europe/Berlin"),
                            _fields_set={"schedule"},
                        )
                    },
                    _fields_set={"jobs"},
                )
            }
        }
    )
    with pytest.raises(ProjectValidationError) as info:
        broken.resolved_for("dev")
    assert info.value.issues[0].path == "lakana.yaml"
    assert "jobs.a.schedule.cron" in info.value.issues[0].message


def test_an_unknown_job_is_reported_with_the_other_errors() -> None:
    text = (
        'jobs:\n  a:\n    select: ["x"]\n    schedule: { cron: "* * * *" }\n'
        "environments:\n  dev:\n    jobs:\n      b: { schedule: null }\n"
    )
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    messages = [issue.message for issue in info.value.issues]
    assert len(messages) == 2
    assert any(m.startswith("jobs.a.schedule.cron") for m in messages)
    assert any(m.startswith("environments.dev.jobs.b") for m in messages)


def test_pydantic_s_value_error_prefix_is_not_shown() -> None:
    for text in (
        "repo: a.b\n",
        'jobs:\n  a:\n    select: ["x"]\n    schedule: { cron: "* * * *" }\n',
        'jobs:\n  a.b:\n    select: ["x"]\n',
    ):
        with pytest.raises(ProjectValidationError) as info:
            parse_project_config(text)
        assert "Value error" not in str(info.value)


@pytest.mark.parametrize("zone", ["", "Local", "Berlin", "../etc/passwd", "/etc/passwd"])
def test_a_name_zoneinfo_does_not_know_is_refused_with_the_hint(zone: str) -> None:
    text = (
        'jobs:\n  a:\n    select: ["x"]\n    schedule: { cron: "0 6 * * *", timezone: "'
        + zone
        + '" }\n'
    )
    with pytest.raises(ProjectValidationError, match="Europe/Berlin"):
        parse_project_config(text)


def test_est_is_accepted_exactly_when_the_tz_database_has_it() -> None:
    """`EST` is a fixed-offset legacy name: the tz database decides, not this code."""
    text = 'jobs:\n  a:\n    select: ["x"]\n    schedule: { cron: "0 6 * * *", timezone: EST }\n'
    try:
        ZoneInfo("EST")
    except ZoneInfoNotFoundError:
        with pytest.raises(ProjectValidationError, match="Europe/Berlin"):
            parse_project_config(text)
    else:
        assert parse_project_config(text).jobs["a"].schedule is not None


def test_a_cron_that_never_fires_is_refused_with_the_fields_to_change() -> None:
    text = 'jobs:\n  a:\n    select: ["x.*"]\n    schedule:\n      cron: "0 0 31 2 *"\n'
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    (issue,) = info.value.issues
    assert issue.line == 5
    assert issue.message.startswith("jobs.a.schedule.cron: ")
    assert "never fires" in issue.message
    assert "day-of-month field" in issue.message
    parse_project_config(text.replace("31 2", "29 2"))


def test_the_deploy_check_of_a_time_zone_reads_tzdata_through_load_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(name: str) -> None:
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr("lakana_core.projects.project_config.load_zone", refuse)
    text = (
        'jobs:\n  a:\n    select: ["x.*"]\n    schedule:\n      cron: "0 6 * * *"\n'
        "      timezone: Europe/Berlin\n"
    )
    with pytest.raises(ProjectValidationError, match="not a time zone"):
        parse_project_config(text)


def test_an_unreadable_zone_file_is_a_broken_install_not_an_unknown_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(name: str) -> None:
        raise OSError("zone file is corrupt")

    monkeypatch.setattr("lakana_core.projects.project_config.load_zone", broken)
    text = (
        'jobs:\n  a:\n    select: ["x.*"]\n    schedule:\n      cron: "0 6 * * *"\n'
        "      timezone: Europe/Berlin\n"
    )
    with pytest.raises(OSError, match="corrupt"):
        parse_project_config(text)


_NOTIFY = """\
jobs:
  daily_gold:
    select: ["gold.*"]
    notify:
      webhook: team-alerts
      format: slack
  plain:
    select: ["silver.*"]
    notify: { webhook: other-alerts }
  quiet:
    select: ["bronze.*"]
environments:
  dev:
    jobs:
      daily_gold: { notify: null }
      plain: { notify: { format: teams } }
"""


def test_a_notify_block_parses_and_its_format_defaults_to_json() -> None:
    config = parse_project_config(_NOTIFY)
    gold, plain = config.jobs["daily_gold"].notify, config.jobs["plain"].notify
    assert gold is not None and plain is not None
    assert (gold.webhook, gold.format) == ("team-alerts", "slack")
    assert (plain.webhook, plain.format) == ("other-alerts", "json")
    assert config.jobs["quiet"].notify is None


def test_notify_null_in_an_override_removes_the_notification_there() -> None:
    config = parse_project_config(_NOTIFY)
    assert config.resolved_for("dev").jobs["daily_gold"].notify is None
    assert config.resolved_for("prod").jobs["daily_gold"].notify is not None


def test_an_override_merges_into_the_notification() -> None:
    notify = parse_project_config(_NOTIFY).resolved_for("dev").jobs["plain"].notify
    assert notify is not None
    assert (notify.webhook, notify.format) == ("other-alerts", "teams")


def test_an_unknown_notify_format_is_refused_with_its_line() -> None:
    text = 'jobs:\n  a:\n    select: ["x.*"]\n    notify:\n      webhook: w\n      format: email\n'
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    (issue,) = info.value.issues
    assert issue.line == 6
    assert issue.message.startswith("jobs.a.notify.format: ")
    assert "json" in issue.message and "slack" in issue.message and "teams" in issue.message
    assert "format: slack" in issue.message  # an example to copy


@pytest.mark.parametrize("name", ['"has space"', '"a:b"', '""', '"x\\ty"'])
def test_a_webhook_that_is_not_a_secret_name_is_refused(name: str) -> None:
    text = f'jobs:\n  a:\n    select: ["x.*"]\n    notify: {{ webhook: {name} }}\n'
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    (issue,) = info.value.issues
    assert issue.line == 4
    assert issue.message.startswith("jobs.a.notify.webhook: ")
    assert "name of a secret" in issue.message
    assert "lakana secret create" in issue.message


def test_a_notify_block_without_a_webhook_is_refused() -> None:
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config('jobs:\n  a:\n    select: ["x.*"]\n    notify: { format: slack }\n')
    message = info.value.issues[0].message
    assert message.startswith("jobs.a.notify.webhook: ")
    assert "Field required" not in message
    assert "name of a secret" in message and "lakana secret create" in message


def test_an_override_notifying_through_an_undeclared_job_is_refused() -> None:
    text = (
        'jobs:\n  a:\n    select: ["x.*"]\nenvironments:\n  dev:\n    jobs:\n'
        "      b: { notify: null }\n"
    )
    with pytest.raises(ProjectValidationError, match="can only change a job declared") as info:
        parse_project_config(text)
    assert info.value.issues[0].line == 7


def test_an_override_that_gives_a_job_without_notify_a_format_only_is_refused() -> None:
    text = (
        'jobs:\n  a:\n    select: ["x.*"]\nenvironments:\n  dev:\n    jobs:\n'
        "      a: { notify: { format: slack } }\n"
    )
    with pytest.raises(ProjectValidationError) as info:
        parse_project_config(text)
    (issue,) = info.value.issues
    assert issue.line == 7
    assert issue.message.startswith("environments.dev.jobs.a.notify: ")
    assert "webhook" in issue.message


def test_an_unknown_notify_key_is_refused() -> None:
    text = 'jobs:\n  a:\n    select: ["x.*"]\n    notify: { webhook: w, url: "https://x" }\n'
    with pytest.raises(ProjectValidationError, match=r"jobs\.a\.notify\.url"):
        parse_project_config(text)


def test_python_names_one_of_the_task_images_and_is_not_per_environment() -> None:
    """`python:` is top-level only; it chooses the image, so every environment runs the same."""
    config = parse_project_config('python: "3.12"\n')

    assert config.python == "3.12"
    assert parse_project_config("").python is None
    assert SUPPORTED_TASK_PYTHONS == ("3.11", "3.12", "3.13", "3.14")
    resolved = config.resolved_for("dev")
    assert "python" not in resolved.model_dump()
    with pytest.raises(ProjectValidationError, match=r"environments\.prod\.python"):
        parse_project_config('environments:\n  prod:\n    python: "3.12"\n')


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            'python: "3.10"\n',
            'must be one of "3.11", "3.12", "3.13", "3.14" (the Python versions Lakana ships a '
            "task image for), in quotes; got '3.10'.",
        ),
        ("python: 3.12\n", 'write the version in quotes, such as python: "3.12"'),
        ("python: 3.10\n", 'write the version in quotes, such as python: "3.12"'),
        ("python: [3]\n", "must be one of"),
    ],
)
def test_a_python_without_a_task_image_is_refused_at_deploy(text: str, message: str) -> None:
    """YAML reads 3.10 as the number 3.1; the message says to quote it.

    Parameters
    ----------
    text
        The file.
    message
        Part of the issue.
    """
    with pytest.raises(ProjectValidationError) as caught:
        parse_project_config(text)
    assert message in str(caught.value)


_TAG_TEXT = (
    "is not a language tag Lakana takes: a language, then optionally a script and a region "
    "(BCP 47), such as en, de, fr-CH or zh-Hant"
)


def _messages_of(text: str) -> list[str]:
    """Parse a `lakana.yaml` that must be refused and return its issue messages.

    Parameters
    ----------
    text
        The file's content.

    Returns
    -------
    list of str
        The messages, in order (the file's `_issues` returns the issues themselves).
    """
    return [issue.message for issue in _issues(text)]


def test_main_language_and_require_parse() -> None:
    config = parse_project_config("main_language: de\ntranslations:\n  require: [fr, it]\n")
    assert config.main_language == "de"
    assert config.translations.require == ("fr", "it")
    assert parse_project_config("repo: x\n").main_language is None


def test_a_main_language_that_is_not_a_tag_or_not_canonical_is_refused() -> None:
    assert _messages_of("main_language: English\n") == [f"main_language: `English` {_TAG_TEXT}"]
    assert _messages_of("main_language: DE-ch\n") == [
        "main_language: `DE-ch` is written `de-CH`; use that spelling"
    ]


def test_require_takes_tags_once_each() -> None:
    assert _messages_of("main_language: de\ntranslations:\n  require: [French]\n") == [
        f"translations.require.0: `French` {_TAG_TEXT}"
    ]
    assert _messages_of("main_language: de\ntranslations:\n  require: [fr, fr]\n") == [
        "translations.require: lists `fr` twice"
    ]


def test_require_may_not_name_the_main_language() -> None:
    assert _messages_of("main_language: de\ntranslations:\n  require: [de]\n") == [
        "translations.require: `de` is the main language, whose texts are the asset headers; "
        "remove it from the list"
    ]


def test_require_needs_a_main_language() -> None:
    assert _messages_of("translations:\n  require: [fr]\n") == [NEEDS_MAIN_LANGUAGE]


def test_an_environment_may_not_set_the_languages() -> None:
    messages = _messages_of("main_language: de\nenvironments:\n  prod:\n    main_language: fr\n")
    assert messages == ["environments.prod.main_language: Extra inputs are not permitted"]


def test_the_resolved_configuration_leaves_the_languages_out() -> None:
    config = parse_project_config("main_language: de\ntranslations:\n  require: [fr]\n")
    resolved = config.resolved_for("dev")
    assert "main_language" not in resolved.model_dump()
    assert "translations" not in resolved.model_dump()


def test_checked_label_is_one_short_line() -> None:
    assert checked_label("  Net revenue ") == "Net revenue"
    for bad, message in (
        ("", "must not be empty"),
        ("a\nb", "must be one line"),
        ("x" * 101, "must be at most 100 characters"),
    ):
        with pytest.raises(ValueError, match=message):
            checked_label(bad)
