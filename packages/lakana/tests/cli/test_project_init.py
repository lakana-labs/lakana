"""`lakana init`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any
from unittest import mock

import pytest
import yaml
from conftest import FakeApi, invoke, make_state, write_profile_file
from typer.testing import Result

from lakana import __version__
from lakana.cli._sample import SAMPLE

_SAMPLE = {
    "assets/bronze/readings.sql",
    "assets/silver/station_temperatures.sql",
    "assets/gold/station_ranking.sql",
    "checks/silver/station_temperatures/plausible_range.sql",
}
_EXPECTED = {
    "lakana.yaml",
    *_SAMPLE,
    "tests/test_example.py",
    "pyproject.toml",
    ".github/workflows/lakana-deploy.yml",
    ".gitlab-ci.yml",
}


_CI_AUTH = [[], ["--ci-auth", "pat"]]
"""The CI files `lakana init` writes by default (with OIDC), and the ones with a stored token."""


def _files(root: Path) -> set[str]:
    """List every file under `root`, relative and POSIX.

    Parameters
    ----------
    root
        The directory.

    Returns
    -------
    set of str
        Relative paths.
    """
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_init_writes_the_project_without_prompts(tmp_path: Path) -> None:
    """Every documented file appears; the repo name is the directory name."""
    target = tmp_path / "analytics"
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    assert result.exit_code == 0, result.output
    assert _files(target) == _EXPECTED
    assert yaml.safe_load((target / "lakana.yaml").read_text())["repo"] == "analytics"
    assert "lakana repo add analytics" in result.stdout


def test_the_sample_is_one_source_three_sql_assets_and_one_check(tmp_path: Path) -> None:
    """The sample builds bronze, then silver, then gold from generated rows, with one check."""
    target = tmp_path / "analytics"
    invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    sample = {name: (target / name).read_text() for name in _SAMPLE}
    for name, text in sample.items():
        assert text.startswith("/* lakana\ndescription: "), name
    assert "FROM bronze.readings" in sample["assets/silver/station_temperatures.sql"]
    assert "FROM silver.station_temperatures" in sample["assets/gold/station_ranking.sql"]
    assert (
        "FROM silver.station_temperatures"
        in (sample["checks/silver/station_temperatures/plausible_range.sql"])
    )
    bronze = sample["assets/bronze/readings.sql"]
    assert "FROM " in bronze and "read_" not in bronze and "http" not in bronze
    assert "random" not in "".join(sample.values()).lower()
    assert sum(len(text) for text in sample.values()) < 10_000
    assert not (target / "assets" / "bronze" / "example.sql").exists()


def test_the_sample_can_be_left_out(tmp_path: Path) -> None:
    """`--no-sample` writes the project without assets or checks; the CI files stay."""
    target = tmp_path / "analytics"
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(target), "--no-sample")
    assert result.exit_code == 0, result.output
    assert _files(target) == _EXPECTED - _SAMPLE
    assert "small sample" not in result.stdout
    assert "lakana deploy --env dev" in result.stdout


def _remote_profile(tmp_path: Path, name: str = "team") -> None:
    """Write a profile file whose default profile points off this machine.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    name
        The profile's name.
    """
    write_profile_file(
        tmp_path / "lakana" / "config.toml",
        f'default_profile = "{name}"\n[profiles.{name}]\n'
        'url = "https://lakana.example.org"\ntoken = "t"\n',
    )


def test_the_sample_is_on_in_local_mode_and_off_for_a_remote_default_profile(
    tmp_path: Path,
) -> None:
    """A local or absent profile gets the sample; a team instance's profile does not."""
    state = make_state(tmp_path, FakeApi())
    local = tmp_path / "local"
    write_profile_file(
        tmp_path / "lakana" / "config.toml",
        'default_profile = "local"\n[profiles.local]\nurl = "http://127.0.0.1:8765"\ntoken = "t"\n',
    )
    invoke(state, "init", str(local))
    assert _files(local) == _EXPECTED
    _remote_profile(tmp_path)
    remote = tmp_path / "remote"
    result = invoke(state, "init", str(remote))
    assert _files(remote) == _EXPECTED - _SAMPLE
    assert "--sample" in result.stdout
    forced = tmp_path / "forced"
    invoke(state, "init", str(forced), "--sample")
    assert _files(forced) == _EXPECTED
    by_env = tmp_path / "by_env"
    invoke(
        make_state(tmp_path, FakeApi()),
        "init",
        str(by_env),
        env={"LAKANA_URL": "https://lakana.example.org"},
    )
    assert _files(by_env) == _EXPECTED - _SAMPLE


def test_a_broken_profile_file_does_not_stop_init(tmp_path: Path) -> None:
    """Init is offline: a profile file it can't read means 'local', not an error."""
    write_profile_file(tmp_path / "lakana" / "config.toml", "this is not toml [[[")
    target = tmp_path / "analytics"
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    assert result.exit_code == 0, result.output
    assert _files(target) == _EXPECTED


def test_the_sample_files_are_the_ones_the_tutorial_shows() -> None:
    """The tutorial's files under docs/examples equal what `lakana init` writes."""
    examples = Path(__file__).resolve().parents[4] / "docs" / "examples" / "getting-started"
    for name, text in SAMPLE.items():
        assert (examples / "project" / name).read_text() == text, name


def test_ci_templates_separate_dev_and_prod_credentials(tmp_path: Path) -> None:
    """With stored tokens, PR dry-runs use a dev-only token; prod deploys run in `prod`."""
    target = tmp_path / "analytics"
    invoke(make_state(tmp_path, FakeApi()), "init", str(target), "--ci-auth", "pat")
    workflow = target / ".github" / "workflows" / "lakana-deploy.yml"
    text = workflow.read_text()
    github = yaml.safe_load(text)
    assert github["permissions"] == {"contents": "read"}
    dry, deploy = github["jobs"]["dry-run"], github["jobs"]["deploy"]
    assert dry["env"]["LAKANA_TOKEN"] == "${{ secrets.LAKANA_TOKEN_DEV }}"
    assert deploy["env"]["LAKANA_TOKEN"] == "${{ secrets.LAKANA_TOKEN }}"
    assert deploy["environment"] == "prod"
    assert "environment" not in dry
    assert deploy["concurrency"] == {"group": "lakana-deploy-prod", "cancel-in-progress": False}
    assert "head.repo.full_name == github.repository" in dry["if"]
    runs = {
        name: " ".join(step.get("run", "") for step in job["steps"])
        for name, job in github["jobs"].items()
    }
    assert "lakana deploy --env dev" in runs["dry-run"] and "--dry-run" in runs["dry-run"]
    assert "lakana deploy --env prod" in runs["deploy"] and "--dry-run" not in runs["deploy"]
    assert "${{" not in " ".join(runs.values())
    gitlab = yaml.safe_load((target / ".gitlab-ci.yml").read_text())
    assert gitlab["lakana-dry-run"]["variables"]["LAKANA_TOKEN"] == "$LAKANA_TOKEN_DEV"
    assert "--dry-run" in " ".join(gitlab["lakana-dry-run"]["script"])
    assert "--env prod" in " ".join(gitlab["lakana-deploy"]["script"])
    assert gitlab["lakana-deploy"]["resource_group"] == "lakana-deploy-prod"
    assert (
        gitlab["lakana-dry-run"]["rules"][0]["if"] == '$CI_PIPELINE_SOURCE == "merge_request_event"'
    )


def test_existing_files_are_never_overwritten(tmp_path: Path) -> None:
    """If any target file exists, nothing is written and the command exits 2."""
    target = tmp_path / "analytics"
    target.mkdir()
    (target / "lakana.yaml").write_text("mine\n")
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    assert result.exit_code == 2
    assert (target / "lakana.yaml").read_text() == "mine\n"
    assert _files(target) == {"lakana.yaml"}


def test_file_in_the_way_writes_nothing(tmp_path: Path) -> None:
    """A file where a directory is needed is exit 3 and leaves no partial project."""
    target = tmp_path / "analytics"
    target.mkdir()
    (target / "assets").write_text("not a directory\n")
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    assert result.exit_code == 3
    assert "Can't write the project" in result.output
    assert _files(target) == {"assets"}


def test_dangling_symlink_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    """A dangling symlink at a target counts as existing."""
    target = tmp_path / "analytics"
    target.mkdir()
    (target / "pyproject.toml").symlink_to(tmp_path / "missing")
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    assert result.exit_code == 2
    assert _files(target) == set()


def test_midway_write_failure_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If a write fails after others succeeded, the written files are removed."""
    real_open = Path.open

    def flaky(self: Path, mode: str = "r") -> IO[str]:
        if self.name == "station_ranking.sql":
            raise OSError("disk full")
        return real_open(self, mode)

    monkeypatch.setattr(Path, "open", flaky)
    target = tmp_path / "analytics"
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    assert result.exit_code == 3
    assert _files(target) == set()


def test_next_steps_quote_the_path(tmp_path: Path) -> None:
    """A directory with spaces is shell-quoted in the printed commands."""
    result = invoke(
        make_state(tmp_path, FakeApi()), "init", str(tmp_path / "my proj"), "--repo", "p"
    )
    assert f"cd '{tmp_path / 'my proj'}'" in result.stdout


def test_repo_option_and_invalid_names(tmp_path: Path) -> None:
    """`--repo` overrides the directory name; a dotted name is refused."""
    state = make_state(tmp_path, FakeApi())
    assert invoke(state, "init", str(tmp_path / "x.y")).exit_code == 2
    assert invoke(state, "init", str(tmp_path / "x.y"), "--repo", "xy").exit_code == 0
    assert yaml.safe_load((tmp_path / "x.y" / "lakana.yaml").read_text())["repo"] == "xy"


def test_default_directory_is_the_current_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no DIR, the project is written here."""
    (tmp_path / "demo").mkdir()
    monkeypatch.chdir(tmp_path / "demo")
    assert invoke(make_state(tmp_path, FakeApi()), "init").exit_code == 0
    assert (tmp_path / "demo" / "lakana.yaml").is_file()


@pytest.mark.parametrize("ci_auth", _CI_AUTH)
def test_generated_project_pins_the_cli_s_own_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ci_auth: list[str]
) -> None:
    """Every install pins the CLI's version and names its one index, never a bare install.

    A bare or merely pinned `pip install lakana` takes the package from whichever index pip
    knows; the job that holds the prod token installs from `LAKANA_INDEX_URL` when it is set
    and from PyPI when it is not.
    """
    monkeypatch.setattr("lakana.cli.project_init.__version__", "1.2.3")
    target = tmp_path / "analytics"
    invoke(make_state(tmp_path, FakeApi()), "init", str(target), *ci_auth)
    pyproject = (target / "pyproject.toml").read_text()
    # The template depends on no package: a SQL-only project needs no lock.
    assert "dependencies = []" in pyproject
    parsed = tomllib.loads(pyproject)
    assert parsed["project"]["dependencies"] == []
    assert not [
        line
        for line in pyproject.splitlines()
        if "lakana" in line and not line.lstrip().startswith("#") and "name =" not in line
    ]
    install = (
        'pip install --index-url "${LAKANA_INDEX_URL:-https://pypi.org/simple}" "lakana==1.2.3"'
    )
    github_text = (target / ".github" / "workflows" / "lakana-deploy.yml").read_text()
    github = yaml.safe_load(github_text)
    for job in github["jobs"].values():
        assert job["env"]["LAKANA_INDEX_URL"] == "${{ vars.LAKANA_INDEX_URL }}"
        [step] = [s for s in job["steps"] if "pip install" in s.get("run", "")]
        assert step["run"].splitlines() == [install]
    gitlab_text = (target / ".gitlab-ci.yml").read_text()
    gitlab = yaml.safe_load(gitlab_text)
    for job in ("lakana-dry-run", "lakana-deploy"):
        assert gitlab[job]["script"][0] == install
    for text in (github_text, gitlab_text):
        assert "extra-index-url" not in text
        assert text.count("pip install") == 2
        # lakana is on PyPI (pre-beta): the comments must not say otherwise.
        assert "not on PyPI" not in text
        assert "public beta" not in text
        assert "unclaimed" not in text
        assert "https://pypi.org/simple" in text


def test_the_install_line_uses_pypi_unless_an_index_is_set(tmp_path: Path) -> None:
    """The install line asks PyPI when the variable is unset or empty, else the override."""
    target = tmp_path / "analytics"
    invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    [job] = [yaml.safe_load((target / ".gitlab-ci.yml").read_text())["lakana-deploy"]]
    line = job["script"][0].replace("pip install", "echo")
    env = {k: v for k, v in os.environ.items() if k != "LAKANA_INDEX_URL"}

    def index_used(environment: dict[str, str]) -> str:
        out = subprocess.run(
            ["sh", "-c", line], env=environment, capture_output=True, text=True, check=True
        ).stdout.split()
        return out[out.index("--index-url") + 1]

    assert index_used(env) == "https://pypi.org/simple"
    assert index_used({**env, "LAKANA_INDEX_URL": ""}) == "https://pypi.org/simple"
    override = {**env, "LAKANA_INDEX_URL": "https://index.example/simple/"}
    assert index_used(override) == "https://index.example/simple/"


def test_gitlab_prod_token_has_its_own_variable_name(tmp_path: Path) -> None:
    """A project variable named LAKANA_TOKEN would override the dry-run's job-level mapping."""
    target = tmp_path / "analytics"
    invoke(make_state(tmp_path, FakeApi()), "init", str(target), "--ci-auth", "pat")
    text = (target / ".gitlab-ci.yml").read_text()
    gitlab = yaml.safe_load(text)
    assert gitlab["lakana-dry-run"]["variables"] == {"LAKANA_TOKEN": "$LAKANA_TOKEN_DEV"}
    assert gitlab["lakana-deploy"]["variables"] == {"LAKANA_TOKEN": "$LAKANA_TOKEN_PROD"}
    assert "LAKANA_TOKEN_PROD (masked AND protected" in text


@pytest.mark.parametrize("ci_auth", _CI_AUTH)
def test_every_generated_deploy_has_a_timeout(tmp_path: Path, ci_auth: list[str]) -> None:
    """A worker crash mid-deploy must not hang the CI job until the runner's own limit."""
    target = tmp_path / "analytics"
    invoke(make_state(tmp_path, FakeApi()), "init", str(target), *ci_auth)
    github = yaml.safe_load((target / ".github" / "workflows" / "lakana-deploy.yml").read_text())
    gitlab = yaml.safe_load((target / ".gitlab-ci.yml").read_text())
    deploys = [
        step["run"]
        for job in github["jobs"].values()
        for step in job["steps"]
        if step.get("run", "").startswith("lakana deploy")
    ] + [
        line
        for job in ("lakana-dry-run", "lakana-deploy")
        for line in gitlab[job]["script"]
        if line.startswith("lakana deploy")
    ]
    assert len(deploys) == 4
    assert all("--timeout 1800" in line for line in deploys), deploys


_NO_GIT_IDENTITY = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "user.useConfigOnly",
    "GIT_CONFIG_VALUE_0": "true",
}
"""Environment in which git has no identity and may not guess one."""


def _git_env(monkeypatch: pytest.MonkeyPatch, home: Path, *, identity: bool) -> None:
    """Make git resolve (or lack) an identity regardless of the machine running the test.

    Parameters
    ----------
    monkeypatch
        The fixture.
    home
        A directory to use as `HOME`.
    identity
        Whether git should find a name and an email.
    """
    for name in (
        "EMAIL",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name, value in _NO_GIT_IDENTITY.items():
        monkeypatch.setenv(name, value)
    if identity:
        for who in ("AUTHOR", "COMMITTER"):
            monkeypatch.setenv(f"GIT_{who}_NAME", "Ada")
            monkeypatch.setenv(f"GIT_{who}_EMAIL", "ada@example.org")


def test_next_steps_name_the_two_commands_that_build_the_sample(tmp_path: Path) -> None:
    """After the setup steps, the two commands are deploy and run, then a query to look."""
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"), "--repo", "p")
    lines = result.stdout.splitlines()
    first = lines.index("Then the two commands that build the sample:")
    assert lines[first + 1 : first + 3] == ["  lakana deploy --env dev", "  lakana run --env dev"]
    assert lines.index("  lakana repo add p --kind local --url " + str(tmp_path / "p")) < first
    assert lines[first + 3] == "Then look at the result:"
    assert lines[first + 4].startswith('  lakana sql "SELECT * FROM gold.station_ranking')


def test_next_steps_without_the_sample_end_with_the_first_run(tmp_path: Path) -> None:
    """Without the sample the list is the plain one: deploy and run are its last steps."""
    result = invoke(
        make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"), "--repo", "p", "--no-sample"
    )
    steps = result.stdout.split("Next:")[1].splitlines()
    commands = [line.strip() for line in steps if line.startswith("  ")]
    assert commands[-2:] == ["lakana deploy --env dev", "lakana run --env dev"]


def test_init_says_nothing_about_git_when_it_has_an_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a name and email configured, there is no git advice."""
    _git_env(monkeypatch, tmp_path, identity=True)
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"))
    assert result.exit_code == 0, result.output
    assert "user.name" not in result.output


def test_init_gives_the_two_git_commands_when_there_is_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an identity `git commit` fails; say so once, with the two commands."""
    _git_env(monkeypatch, tmp_path, identity=False)
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"))
    assert result.exit_code == 0, result.output
    assert result.stdout.count("git config --global user.name") == 1
    assert "git config --global user.email" in result.stdout
    text = " ".join(result.stdout.split())
    assert (
        "If 'git commit' says it does not know who you are, set a name and email first: "
        "git config --global user.name 'Your Name' && "
        "git config --global user.email you@example.com"
    ) in text
    assert "Git has no name" not in text


def test_a_slow_git_prints_nothing_and_init_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout of the identity check is not an error and gives no advice."""
    seen: list[object] = []

    def slow(*args: object, **kwargs: object) -> object:
        """Stand in for `subprocess.run`, timing out.

        Parameters
        ----------
        *args
            The command.
        **kwargs
            The options; `timeout` is recorded.

        Raises
        ------
        subprocess.TimeoutExpired
            Always.
        """
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired("git", 3)

    monkeypatch.setattr("lakana.cli.project_init.subprocess.run", slow)
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"))
    assert result.exit_code == 0, result.output
    assert "user.name" not in result.output
    assert seen == [3]


def test_init_without_git_installed_says_nothing_about_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing git is not an error of `init`, and gets no identity advice."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"))
    assert result.exit_code == 0, result.output
    assert "user.name" not in result.output


@pytest.mark.parametrize(
    "version", ["0.7.1.dev3+gabc123", "0+unknown", "1.2.3+local", "1.0.0.dev0"]
)
def test_init_notes_after_the_steps_when_the_cli_is_a_dev_or_local_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """The pinned CI install can't resolve such a version: a note after the steps, no warning."""
    monkeypatch.setattr("lakana.cli.project_init.__version__", version)
    target = tmp_path / "analytics"
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    assert result.exit_code == 0, result.output
    assert "warning" not in result.output
    assert result.stdout.startswith("Created a Lakana project")
    text = " ".join(result.stdout.split())
    note = text[text.index("Note:") :]
    assert text.index("lakana run --env dev") < text.index("Note:")
    assert f"lakana {version} is a development build" in note
    assert "ignore this until you use CI" in note.lower() or "Ignore this" in note
    assert "can't be installed from PyPI" in note
    assert f"lakana=={version}" in (target / ".gitlab-ci.yml").read_text()


def test_a_template_on_a_dev_build_notes_that_uv_lock_needs_an_index_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A template pins `lakana[assets]` to this version, which `uv lock` cannot find on PyPI."""
    monkeypatch.setattr("lakana.cli.project_init.__version__", "0.7.1.dev3+gabc123")
    target = tmp_path / "sources"
    result = invoke(
        make_state(tmp_path, FakeApi()), "init", str(target), "--template", "ingest-sql"
    )
    assert result.exit_code == 0, result.output
    text = " ".join(result.stdout.split())
    note = text[text.index("Note:") :]
    assert "pyproject.toml" in note
    assert "uv lock" in note
    assert "[[tool.uv.index]]" in note
    assert "lakana[assets]==0.7.1.dev3+gabc123" in (target / "pyproject.toml").read_text()


@pytest.mark.parametrize("version", ["1.2.3", "0.7.0", "1.0.0rc1", "2.0.0.post1"])
def test_init_is_quiet_for_a_release_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    """A published version pins cleanly and says nothing about it."""
    monkeypatch.setattr("lakana.cli.project_init.__version__", version)
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(tmp_path / "analytics"))
    assert result.exit_code == 0, result.output
    assert "warning" not in result.output
    assert "Note:" not in result.output


@pytest.mark.parametrize("ci_auth", _CI_AUTH)
def test_ci_templates_pin_every_action_to_a_full_commit_sha(
    tmp_path: Path, ci_auth: list[str]
) -> None:
    """A mutable `@v4` tag in a job holding the prod token is a supply-chain hole.

    Each `uses:` names a 40-hex commit with the release it stands for in a trailing comment.
    """
    target = tmp_path / "analytics"
    invoke(make_state(tmp_path, FakeApi()), "init", str(target), *ci_auth)
    text = (target / ".github" / "workflows" / "lakana-deploy.yml").read_text()
    uses = [line.strip() for line in text.splitlines() if "uses:" in line]
    assert uses
    for line in uses:
        assert re.fullmatch(r"- uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+", line), line
    assert not re.search(r"@v\d", text)


def test_pyproject_explains_python_assets_without_depending_on_pypi(tmp_path: Path) -> None:
    """`lakana[assets]` is commented out, pinned to the CLI, and comes from PyPI with no folder."""
    target = tmp_path / "analytics"
    invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    text = (target / "pyproject.toml").read_text()
    assert f'# dependencies = ["lakana[assets]=={__version__}"]' in text
    # lakana comes from the public index: no folder of wheels, no uv index or source tables.
    assert "wheelhouse" not in text
    assert "tool.uv" not in text
    assert "pre-beta" not in text
    assert "public beta" not in text
    assert "unclaimed" not in text
    assert "public package index" in text
    # One path: no address of a package index of the reader's own, and no "v1.0.0".
    assert "<your index>" not in text
    assert "your own index" not in text
    assert "v1.0.0" not in text
    # A first-time user is told every step, in order.
    assert "1. Delete `dependencies = []`" in text
    assert "uncomment the dependency line" in text
    assert "2. Run `uv lock`" in text
    assert "3. Commit pyproject.toml and uv.lock" in text
    assert tomllib.loads(text)["project"]["dependencies"] == []


def test_following_the_pyproject_instructions_gives_a_valid_file(tmp_path: Path) -> None:
    """Uncommenting the template line as its steps say gives a parseable project."""
    target = tmp_path / "analytics"
    invoke(make_state(tmp_path, FakeApi()), "init", str(target))
    lines = (target / "pyproject.toml").read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("# dependencies = "))
    kept = [ln for ln in lines if ln != "dependencies = []"]
    kept[kept.index(lines[start])] = lines[start].removeprefix("# ")
    parsed = tomllib.loads("\n".join(kept))
    assert parsed["project"]["dependencies"] == [f"lakana[assets]=={__version__}"]
    assert "uv" not in parsed.get("tool", {})


@pytest.mark.parametrize("ci_auth", _CI_AUTH)
def test_ci_templates_run_the_python_of_the_repository_default(
    tmp_path: Path, ci_auth: list[str]
) -> None:
    """Both CI files name the Python that `.python-version` names, so the two cannot drift."""
    default = (Path(__file__).parents[4] / ".python-version").read_text().strip()
    assert re.fullmatch(r"3\.\d+", default), default
    target = tmp_path / "analytics"
    invoke(make_state(tmp_path, FakeApi()), "init", str(target), *ci_auth)
    github = yaml.safe_load((target / ".github" / "workflows" / "lakana-deploy.yml").read_text())
    versions = [
        step["with"]["python-version"]
        for job in github["jobs"].values()
        for step in job["steps"]
        if "setup-python" in step.get("uses", "")
    ]
    assert versions == [default, default]
    gitlab = yaml.safe_load((target / ".gitlab-ci.yml").read_text())
    for job in ("lakana-dry-run", "lakana-deploy"):
        assert gitlab[job]["image"] == f"python:{default}-slim"


def _generate(tmp_path: Path, *args: str) -> tuple[str, str]:
    """Run `lakana init` for the repository `analytics` and read both CI files.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    *args
        More options.

    Returns
    -------
    tuple of str
        The GitHub workflow and the GitLab file.
    """
    target = tmp_path / "analytics"
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(target), *args)
    assert result.exit_code == 0, result.output
    return (
        (target / ".github" / "workflows" / "lakana-deploy.yml").read_text(),
        (target / ".gitlab-ci.yml").read_text(),
    )


def test_ci_templates_deploy_with_the_jobs_id_token_by_default(tmp_path: Path) -> None:
    """No stored token: each job may request an ID token and names its provider."""
    github_text, gitlab_text = _generate(tmp_path)
    github = yaml.safe_load(github_text)
    assert github["permissions"] == {"contents": "read"}
    dry, deploy = github["jobs"]["dry-run"], github["jobs"]["deploy"]
    for job in (dry, deploy):
        assert job["permissions"] == {"contents": "read", "id-token": "write"}
        assert job["env"]["LAKANA_OIDC"] == "github"
        assert job["env"]["LAKANA_URL"] == "${{ vars.LAKANA_URL }}"
        assert "LAKANA_TOKEN" not in job["env"]
    assert (dry["environment"], deploy["environment"]) == ("lakana-dry-run", "prod")
    assert "head.repo.full_name == github.repository" in dry["if"]
    gitlab = yaml.safe_load(gitlab_text)
    for name, environment in (("lakana-dry-run", "lakana-dry-run"), ("lakana-deploy", "prod")):
        job = gitlab[name]
        assert job["id_tokens"] == {"LAKANA_OIDC_TOKEN": {"aud": "$LAKANA_URL"}}
        assert job["variables"] == {"LAKANA_OIDC": "gitlab"}
        assert job["environment"] == environment
    for text in (github_text, gitlab_text):
        assert "secrets." not in text
        assert "  LAKANA_TOKEN:" not in text
        assert "lakana ci-trust create" in text


def test_the_pat_files_are_written_only_when_asked_for(tmp_path: Path) -> None:
    """`--ci-auth pat` writes the files that read a stored token; an unknown value is refused."""
    github_text, gitlab_text = _generate(tmp_path, "--ci-auth", "pat")
    assert "LAKANA_OIDC" not in github_text + gitlab_text
    assert "id-token" not in github_text and "id_tokens" not in gitlab_text
    result = invoke(make_state(tmp_path, FakeApi()), "init", str(tmp_path / "x"), "--ci-auth", "x")
    assert result.exit_code == 2


def test_init_says_the_ci_files_need_no_stored_token(tmp_path: Path) -> None:
    """The closing lines point at the comments; the PAT variant says nothing about it."""
    oidc = invoke(make_state(tmp_path, FakeApi()), "init", str(tmp_path / "a"))
    assert "deploy without a stored Lakana token" in " ".join(oidc.stdout.split())
    pat = invoke(make_state(tmp_path, FakeApi()), "init", str(tmp_path / "b"), "--ci-auth", "pat")
    assert "without a stored Lakana token" not in " ".join(pat.stdout.split())


def test_the_how_tos_show_the_files_init_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The how-tos' CI files under docs/examples are what `lakana init` writes for `analytics`."""
    monkeypatch.setattr("lakana.cli.project_init.__version__", "<version>")
    github_text, gitlab_text = _generate(tmp_path)
    examples = Path(__file__).resolve().parents[4] / "docs" / "examples"
    assert (examples / "ci-oidc-github-actions.yml").read_text() == github_text
    assert (examples / "ci-oidc-gitlab-ci.yml").read_text() == gitlab_text


def _commands(text: str, prefix: str) -> list[str]:
    """Collect the `lakana ci-trust create` commands of a text, continuations joined.

    Parameters
    ----------
    text
        A CI file or a shell script.
    prefix
        What each line of a command starts with (`#` in a comment, nothing in a script).

    Returns
    -------
    list of str
        One line per command, whitespace collapsed.
    """
    lines = [line.removeprefix(prefix).strip() for line in text.splitlines()]
    joined = " ".join(line if line.endswith("\\") else f"{line}\n" for line in lines)
    return [
        " ".join(command.replace("\\", " ").split())
        for command in joined.splitlines()
        if command.strip().startswith("lakana ci-trust create")
    ]


def test_the_ci_files_name_the_rules_the_how_tos_create(tmp_path: Path) -> None:
    """The comment of each CI file holds the `ci-trust create` commands of its how-to."""
    github_text, gitlab_text = _generate(tmp_path)
    script = (
        Path(__file__).resolve().parents[4] / "docs" / "examples" / "ci-trust-rules.sh"
    ).read_text()
    documented = _commands(script, "")
    assert len(documented) == 4
    assert _commands(github_text, "#") == documented[:2]
    assert _commands(gitlab_text, "#") == documented[2:]


_TEMPLATE_ASSETS = {
    "ingest-files": "assets/bronze/exports.py",
    "ingest-sql": "assets/bronze/orders.py",
    "ingest-rest": "assets/bronze/items.py",
}


@pytest.mark.parametrize(("template", "asset"), sorted(_TEMPLATE_ASSETS.items()))
def test_a_template_writes_its_asset_readme_and_an_assets_pyproject(
    tmp_path: Path, template: str, asset: str
) -> None:
    """No sample; the asset, a README with the connection command, and lakana[assets] pinned."""
    result = invoke(
        make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"), "--template", template
    )

    assert result.exit_code == 0, result.output
    root = tmp_path / "p"
    files = _files(root)
    assert asset in files
    assert "README.md" in files
    assert not files & _SAMPLE
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == [f"lakana[assets]=={__version__}"]
    readme = (root / "README.md").read_text()
    assert "lakana connection create" in readme
    assert "uv lock" in readme
    assert "# p\n" in readme
    assert "lakana connection create" in result.stdout
    assert "  uv lock" in result.stdout


def test_the_sql_template_reads_new_ids_with_a_first_run_default(tmp_path: Path) -> None:
    """A parameter, never a None watermark; a merge that reads again behind the largest id."""
    invoke(make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"), "--template", "ingest-sql")

    text = (tmp_path / "p" / "assets/bronze/orders.py").read_text()

    assert '@asset(connections=["source-db"], write="merge", key=["id"], watermark="id")' in text
    assert "WHERE id > :since" in text
    assert "\nLOOK_BACK = 1000\n" in text
    assert 'params={"since": ctx.watermark(default=0) - LOOK_BACK}' in text
    readme = (tmp_path / "p" / "README.md").read_text()
    assert (
        "lakana connection create source-db --env dev --kind postgresql --host <host> "
        "--database <database> --user <user> --password-stdin"
    ) in readme
    assert "the 1,000 before it again" in " ".join(readme.split())


def test_the_files_template_readme_says_the_operator_names_the_root(tmp_path: Path) -> None:
    """`--root` names an ingestion root only the operator can give the worker."""
    invoke(
        make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"), "--template", "ingest-files"
    )

    readme = " ".join((tmp_path / "p" / "README.md").read_text().split())

    assert "LAKANA_RUN__INGESTION_ROOTS" in readme
    assert "LAKANA_LAUNCHER__INGESTION_ROOTS" in readme
    assert "LAKANA_INGESTION_ROOTS in the Compose bundle's .env" in readme


def test_an_unknown_template_lists_every_template(tmp_path: Path) -> None:
    """Exit 2, naming every template."""
    result = invoke(
        make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"), "--template", "ingest-xml"
    )

    assert result.exit_code == 2
    text = " ".join(result.output.split())
    for name in [*_TEMPLATE_ASSETS, "python"]:
        assert name in text
    assert not (tmp_path / "p").exists()


def test_a_template_with_the_sample_is_refused(tmp_path: Path) -> None:
    """One starting point at a time."""
    result = invoke(
        make_state(tmp_path, FakeApi()),
        "init",
        str(tmp_path / "p"),
        "--template",
        "ingest-files",
        "--sample",
    )

    assert result.exit_code == 2
    assert "leave out --sample" in result.output
    assert not (tmp_path / "p").exists()


def test_the_template_project_test_parses_its_asset(tmp_path: Path) -> None:
    """The project's own test runs green on what the template wrote."""
    invoke(
        make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"), "--template", "ingest-rest"
    )
    namespace: dict[str, object] = {"__file__": str(tmp_path / "p" / "tests" / "test_example.py")}

    exec((tmp_path / "p" / "tests" / "test_example.py").read_text(), namespace)  # nosec B102

    check = namespace["test_every_python_asset_parses"]
    assert callable(check)
    check()


_PYTHON_ASSET = "assets/gold/temperature_anomalies.py"
_PYTHON_FILES = {_PYTHON_ASSET, "README.md", "uv.lock"}


@dataclass
class _Uv:
    """What the fake `uv` saw and how it answers.

    Parameters
    ----------
    returncode
        The exit status `uv lock` returns.
    stderr
        What it prints on standard error.
    raises
        An exception `subprocess.run` raises instead of running it.
    calls
        Each `uv` command line, with its working directory.
    """

    returncode: int = 0
    stderr: str = "Resolved 31 packages in 327ms\n"
    raises: BaseException | None = None
    calls: list[tuple[list[str], Path]] = field(default_factory=list)


@pytest.fixture
def uv(monkeypatch: pytest.MonkeyPatch) -> _Uv:
    """Put a fake `uv` on the PATH: `uv lock` writes a lock unless told to fail.

    Parameters
    ----------
    monkeypatch
        Pytest's monkeypatch.

    Returns
    -------
    _Uv
        The fake's settings and record.
    """
    fake = _Uv()
    real_run = subprocess.run

    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:  # ruff: ignore[any-type]
        if argv[0] != "/opt/uv/bin/uv":
            return real_run(argv, **options)
        fake.calls.append((list(argv), Path(options["cwd"])))
        if fake.raises is not None:
            raise fake.raises
        if fake.returncode == 0:
            (Path(options["cwd"]) / "uv.lock").write_text("version = 1\n")
        return subprocess.CompletedProcess(argv, fake.returncode, "", fake.stderr)

    monkeypatch.setattr("lakana.cli.project_init.subprocess.run", run)
    monkeypatch.setattr(
        "lakana.cli.project_init.shutil.which",
        lambda name, path=None: "/opt/uv/bin/uv" if name == "uv" else None,
    )
    return fake


def _init_python(tmp_path: Path, *extra: str) -> Result:
    """Run `lakana init p --template python` in `tmp_path`.

    Parameters
    ----------
    tmp_path
        Pytest's per-test directory.
    *extra
        More options.

    Returns
    -------
    Result
        The CLI's result.
    """
    return invoke(
        make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"), "--template", "python", *extra
    )


def test_the_python_template_writes_the_sample_a_python_asset_and_a_lock(
    tmp_path: Path, uv: _Uv
) -> None:
    """The SQL sample, one Python asset built from two of its tables, lakana[assets] and uv.lock."""
    result = _init_python(tmp_path)

    assert result.exit_code == 0, result.output
    root = tmp_path / "p"
    assert _files(root) == _EXPECTED | _PYTHON_FILES
    for name, text in SAMPLE.items():
        assert (root / name).read_text() == text
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == [f"lakana[assets]=={__version__}"]
    asset = (root / _PYTHON_ASSET).read_text()
    assert '"readings": "bronze.readings"' in asset
    assert '"stations": "silver.station_temperatures"' in asset
    assert "readings: pl.DataFrame, stations: pl.DataFrame" in asset
    assert uv.calls == [(["/opt/uv/bin/uv", "lock", "--project", str(root)], root)]
    assert (root / "uv.lock").read_text() == "version = 1\n"


def test_the_python_template_says_up_front_why_it_needs_the_network(
    tmp_path: Path, uv: _Uv
) -> None:
    """The first line names the network, the minutes and the reason; the steps follow the lock."""
    result = _init_python(tmp_path)

    lines = result.stdout.splitlines()
    first = " ".join(lines[0].split())
    assert "network access" in first
    assert "a few minutes" in first
    assert "uv lock" in first
    assert "resolves" in first and "uv.lock" in first
    text = " ".join(result.stdout.split())
    assert text.index("network access") < text.index("Created a Lakana project")
    assert "commit it with the rest" in text
    assert "  uv lock" not in result.stdout
    steps = result.stdout[result.stdout.index("Next:") :]
    assert "git init && git add -A && git commit -m 'Initial project'" in steps
    assert "lakana deploy --env dev" in steps
    assert "lakana run --env dev" in steps
    assert 'lakana sql "SELECT * FROM gold.temperature_anomalies' in steps
    assert "first run" in " ".join(steps.split())


def test_the_python_template_readme_says_what_it_needed_and_what_comes_next(
    tmp_path: Path, uv: _Uv
) -> None:
    """The README repeats the network, the minutes and why, then the steps to the query."""
    _init_python(tmp_path)

    readme = (tmp_path / "p" / "README.md").read_text()
    text = " ".join(readme.split())
    assert readme.startswith("# p\n")
    assert "network access" in text
    assert "a few more minutes" in text
    assert "resolved" in text and "uv.lock" in text
    assert "lakana deploy --env dev" in text
    assert "lakana run --env dev" in text
    assert "SELECT * FROM gold.temperature_anomalies" in text
    assert "run `uv lock` again" in text


def test_the_python_template_project_test_checks_both_kinds_of_asset(
    tmp_path: Path, uv: _Uv
) -> None:
    """The project's own test runs green on the SQL sample and the Python asset."""
    _init_python(tmp_path)
    test_file = tmp_path / "p" / "tests" / "test_example.py"
    namespace: dict[str, object] = {"__file__": str(test_file)}

    exec(test_file.read_text(), namespace)  # nosec B102

    checks = [namespace[n] for n in namespace if n.startswith("test_")]
    assert len(checks) == 2
    for check in checks:
        assert callable(check)
        check()


def test_the_python_template_without_uv_writes_nothing(tmp_path: Path, uv: _Uv) -> None:
    """No uv on the PATH: exit 1 before a file is written, naming uv and how to get it."""
    with mock.patch("lakana.cli.project_init.shutil.which", return_value=None):
        result = _init_python(tmp_path)

    assert result.exit_code == 1
    text = " ".join(result.output.split())
    assert "uv" in text and "PATH" in text
    assert "https://docs.astral.sh/uv/" in text
    assert "run the same command again" in text
    assert not (tmp_path / "p").exists()
    assert uv.calls == []


@pytest.mark.parametrize(
    "raises",
    [None, OSError("No such file or directory"), subprocess.TimeoutExpired(["uv"], 600)],
)
def test_a_lock_that_fails_leaves_nothing_and_names_the_next_step(
    tmp_path: Path, uv: _Uv, monkeypatch: pytest.MonkeyPatch, raises: BaseException | None
) -> None:
    """Without the network the lock fails: exit 1, uv's reason, the next step, no project left."""
    monkeypatch.setattr("lakana.cli.project_init.__version__", "1.2.3")
    uv.returncode = 2
    uv.stderr = (
        "Using CPython 3.14.4\n"
        "error: Failed to fetch: `https://pypi.org/simple/lakana/`\n"
        "  Caused by: dns error: failed to lookup address information\n"
    )
    uv.raises = raises
    target = tmp_path / "p"
    target.mkdir()
    (target / "notes.txt").write_text("mine\n")

    result = _init_python(tmp_path)

    assert result.exit_code == 1, result.output
    text = " ".join(result.output.split())
    assert "network access" in text
    assert "HTTPS_PROXY" in text
    assert "run the same command again" in text
    assert "--template" in text and "SQL sample" in text
    if raises is None:
        assert "dns error: failed to lookup address information" in text
    elif isinstance(raises, subprocess.TimeoutExpired):
        assert "did not finish within 10 minutes" in text
    else:
        assert "No such file or directory" in text
    assert "wheelhouse" not in text
    assert _files(target) == {"notes.txt"}
    assert sorted(p.name for p in target.iterdir()) == ["notes.txt"]


def test_a_lock_that_fails_on_a_dev_build_says_how_to_lock_from_a_wheel(
    tmp_path: Path, uv: _Uv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No index has a development build: the wheel goes into `wheelhouse`, found by uv."""
    monkeypatch.setattr("lakana.cli.project_init.__version__", "0.7.1.dev3+gabc123")
    uv.returncode = 1
    uv.stderr = "error: no version of lakana[assets]==0.7.1.dev3+gabc123\n"

    result = _init_python(tmp_path)

    assert result.exit_code == 1
    text = " ".join(result.output.split())
    assert "lakana 0.7.1.dev3+gabc123 is a development build" in text
    assert "wheelhouse" in text
    assert "UV_FIND_LINKS=wheelhouse" in text


def test_a_python_template_on_a_dev_build_notes_only_the_ci_pin(
    tmp_path: Path, uv: _Uv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock worked, so the note about `uv lock` and an index does not apply."""
    monkeypatch.setattr("lakana.cli.project_init.__version__", "0.7.1.dev3+gabc123")

    result = _init_python(tmp_path)

    assert result.exit_code == 0, result.output
    assert "Note: lakana 0.7.1.dev3+gabc123 is a development build" in result.stdout
    assert "[[tool.uv.index]]" not in result.stdout


def test_an_existing_lock_is_never_overwritten(tmp_path: Path, uv: _Uv) -> None:
    """A uv.lock in the target counts like any other file in the way: exit 2, nothing run."""
    target = tmp_path / "p"
    target.mkdir()
    (target / "uv.lock").write_text("mine\n")

    result = _init_python(tmp_path)

    assert result.exit_code == 2
    assert "Refusing to overwrite" in result.output
    assert (target / "uv.lock").read_text() == "mine\n"
    assert _files(target) == {"uv.lock"}
    assert uv.calls == []


@pytest.mark.parametrize(
    ("option", "fragment"), [("--sample", "--sample"), ("--no-sample", "--no-sample")]
)
def test_the_python_template_refuses_a_sample_option(
    tmp_path: Path, uv: _Uv, option: str, fragment: str
) -> None:
    """The python template always writes the sample: either option is refused before a write."""
    result = _init_python(tmp_path, option)

    assert result.exit_code == 2
    text = " ".join(result.output.split())
    assert f"leave out {fragment}" in text
    assert "python" in text
    assert not (tmp_path / "p").exists()
    assert uv.calls == []


def test_a_symlinked_directory_in_the_target_is_refused(tmp_path: Path) -> None:
    """A link inside the project would carry a write outside it: exit 3, nothing written."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "analytics"
    target.mkdir()
    (target / "assets").symlink_to(outside, target_is_directory=True)

    result = invoke(make_state(tmp_path, FakeApi()), "init", str(target))

    assert result.exit_code == 3
    assert "is a symbolic link" in " ".join(result.output.split())
    assert list(outside.iterdir()) == []
    assert _files(target) == set()


def test_the_python_asset_is_the_one_the_tutorial_shows() -> None:
    """The tutorial's example runs the asset `lakana init --template python` writes."""
    from lakana.cli.project_init import PYTHON_TEMPLATE

    example = Path(__file__).resolve().parents[4] / "docs" / "examples" / "python-sample"
    assert (example / "project" / _PYTHON_ASSET).read_text() == PYTHON_TEMPLATE[_PYTHON_ASSET]


_UV_NO_SOLUTION = """\
Using CPython 3.13.15
  \u00d7 No solution found when resolving dependencies:
  \u2570\u2500\u25b6 Because there is no version of lakana[assets]==0.20.0 and your project
      depends on lakana[assets]==0.20.0, we can conclude that your project's
      requirements are unsatisfiable.
"""
"""What uv 0.12 prints when no index has the pinned version (captured from a real run)."""

_UV_NO_SOLUTION_SHOWN = (
    "\u00d7 No solution found when resolving dependencies: Because there is no version of "
    "lakana[assets]==0.20.0 and your project depends on lakana[assets]==0.20.0, we can conclude "
    "that your project's requirements are unsatisfiable."
)

_UV_OFFLINE = """\
Using CPython 3.13.15
error: Request failed after 3 retries in 4.4s
  Caused by: Failed to fetch: `http://127.0.0.1:9/simple/lakana/`
  Caused by: error sending request for url (http://127.0.0.1:9/simple/lakana/)
  Caused by: client error (Connect)
  Caused by: tcp connect error
  Caused by: Connection refused (os error 111)
"""
"""What uv 0.12 prints when the index cannot be reached (captured from a real run)."""

_UV_OFFLINE_SHOWN = (
    "error: Request failed after 3 retries in 4.4s Caused by: Failed to fetch: "
    "`http://127.0.0.1:9/simple/lakana/` Caused by: error sending request for url "
    "(http://127.0.0.1:9/simple/lakana/) Caused by: client error (Connect) Caused by: tcp "
    "connect error Caused by: Connection refused (os error 111)"
)


@pytest.mark.parametrize(
    ("stderr", "shown"),
    [
        (_UV_NO_SOLUTION, _UV_NO_SOLUTION_SHOWN),
        (_UV_OFFLINE, _UV_OFFLINE_SHOWN),
        ("".join(f"line {n}\n" for n in range(12)), " ".join(f"line {n}" for n in range(4, 12))),
        ("\n", "(it printed nothing)"),
        ("Using CPython 3.13.15\n", "(it printed nothing)"),
    ],
)
def test_the_lock_error_shows_uv_s_reason(stderr: str, shown: str) -> None:
    """From uv's first `error:` line on, else its last eight lines, else a word that it was mute."""
    from lakana.cli.project_init import _uv_error

    assert _uv_error(stderr) == shown


def test_a_reason_that_ends_a_sentence_gets_one_full_stop(tmp_path: Path, uv: _Uv) -> None:
    """The sentence of uv ends with a full stop; the message adds none of its own after it."""
    uv.returncode = 1
    uv.stderr = _UV_NO_SOLUTION

    result = _init_python(tmp_path)

    text = " ".join(result.output.split())
    assert "requirements are unsatisfiable. The python template" in text
    assert ".." not in text


def test_the_python_pyproject_keeps_the_project_its_own_uv_root(tmp_path: Path, uv: _Uv) -> None:
    """An empty workspace table stops uv at the project; the comment says when to lock again."""
    _init_python(tmp_path)

    text = (tmp_path / "p" / "pyproject.toml").read_text()
    assert tomllib.loads(text)["tool"]["uv"]["workspace"] == {}
    flat = " ".join(line.lstrip("# ") for line in text.splitlines())
    assert "`lakana init` ran `uv lock`" in flat
    assert "run `uv lock` again" in flat
    assert "before the first commit" not in flat


def _workspace(root: Path) -> bytes:
    """Make `root` a uv workspace whose members are every folder below it, with its lock.

    Parameters
    ----------
    root
        The folder.

    Returns
    -------
    bytes
        The workspace's `uv.lock` (a stand-in; uv is not run here).
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "mono"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
        'dependencies = []\n\n[tool.uv.workspace]\nmembers = ["*"]\n'
    )
    (root / "uv.lock").write_text("version = 1\n# the monorepo's lock\n")
    return (root / "uv.lock").read_bytes()


def test_a_lock_that_lands_elsewhere_fails_naming_the_workspace(tmp_path: Path, uv: _Uv) -> None:
    """The lock succeeded, yet no uv.lock is in the project: exit 1, workspace named."""
    mono = tmp_path / "mono"
    before = _workspace(mono)
    uv.stderr = ""

    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:  # ruff: ignore[any-type]
        uv.calls.append((list(argv), Path(options["cwd"])))
        (mono / "uv.lock").write_text("version = 1\n# changed by uv\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with mock.patch("lakana.cli.project_init.subprocess.run", run):
        result = invoke(
            make_state(tmp_path, FakeApi()), "init", str(mono / "p"), "--template", "python"
        )

    assert result.exit_code == 1, result.output
    text = " ".join(result.output.split())
    assert "no uv.lock" in text
    assert f"uv workspace {mono}" in text
    assert not (mono / "p").exists()
    assert (mono / "uv.lock").read_bytes() == before


@pytest.mark.parametrize("above", ["not toml [[[", '[project]\nname = "x"\n', "[tool]\nuv = 1\n"])
def test_a_lock_that_lands_elsewhere_without_a_workspace_names_none(
    tmp_path: Path, above: str
) -> None:
    """A parent pyproject that is unreadable or has no workspace is not named."""
    (tmp_path / "pyproject.toml").write_text(above)

    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:  # ruff: ignore[any-type]
        return subprocess.CompletedProcess(argv, 0, "", "")

    with (
        mock.patch("lakana.cli.project_init.subprocess.run", run),
        mock.patch("lakana.cli.project_init._find_uv", return_value="/opt/uv/bin/uv"),
    ):
        result = invoke(
            make_state(tmp_path, FakeApi()), "init", str(tmp_path / "p"), "--template", "python"
        )

    assert result.exit_code == 1, result.output
    assert "uv workspace" not in result.output
    assert not (tmp_path / "p").exists()


def test_a_lock_uv_created_above_the_project_is_removed_again(tmp_path: Path) -> None:
    """A workspace without a lock before gets none from init."""
    mono = tmp_path / "mono"
    _workspace(mono)
    (mono / "uv.lock").unlink()

    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:  # ruff: ignore[any-type]
        (mono / "uv.lock").write_text("version = 1\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with (
        mock.patch("lakana.cli.project_init.subprocess.run", run),
        mock.patch("lakana.cli.project_init._find_uv", return_value="/opt/uv/bin/uv"),
    ):
        result = invoke(
            make_state(tmp_path, FakeApi()), "init", str(mono / "p"), "--template", "python"
        )

    assert result.exit_code == 1
    assert not (mono / "uv.lock").exists()


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv on the PATH")
def test_a_parent_workspace_s_lock_stays_byte_identical_with_a_real_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real uv, offline: the project is its own root, so the monorepo's lock is not touched.

    The template's dependency is left out, so uv resolves without the network; without the
    empty workspace table uv would add the project to the monorepo's lock instead.
    """
    from lakana.cli import project_init

    monkeypatch.setattr(
        project_init,
        "_PYPROJECT_PYTHON",
        project_init._PYPROJECT_PYTHON.replace(
            'dependencies = ["lakana[assets]=={version}"]', "dependencies = []"
        ),
    )
    mono = tmp_path / "mono"
    mono.mkdir()
    (mono / "pyproject.toml").write_text(
        '[project]\nname = "mono"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
        'dependencies = []\n\n[tool.uv.workspace]\nmembers = ["*"]\n'
    )
    env = {"UV_OFFLINE": "1", "UV_NO_PROGRESS": "1", "UV_PYTHON": sys.executable}
    subprocess.run(["uv", "lock", "-q"], cwd=mono, check=True, env=os.environ | env)
    before = (mono / "uv.lock").read_bytes()

    result = invoke(
        make_state(tmp_path, FakeApi()), "init", str(mono / "p"), "--template", "python", env=env
    )

    assert result.exit_code == 0, result.output
    assert (mono / "p" / "uv.lock").is_file()
    assert (mono / "uv.lock").read_bytes() == before


def test_ctrl_c_during_the_lock_leaves_nothing(tmp_path: Path, uv: _Uv) -> None:
    """An interrupt while uv runs removes what init wrote, so the same command works again."""
    uv.raises = KeyboardInterrupt()

    result = _init_python(tmp_path)

    assert result.exit_code != 0
    assert not (tmp_path / "p").exists()


def test_a_failed_lock_removes_the_parent_folders_init_made(tmp_path: Path, uv: _Uv) -> None:
    """`init a/b/p` that fails leaves no `a/` behind."""
    uv.returncode = 1

    result = invoke(
        make_state(tmp_path, FakeApi()),
        "init",
        str(tmp_path / "a" / "b" / "p"),
        "--template",
        "python",
    )

    assert result.exit_code == 1
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "lakana") == []


def test_a_refusal_to_overwrite_says_how_to_start_over(tmp_path: Path, uv: _Uv) -> None:
    """The next step: remove the files or the folder, or choose another; no lock notice."""
    target = tmp_path / "p"
    target.mkdir()
    (target / "lakana.yaml").write_text("mine\n")

    result = _init_python(tmp_path)

    assert result.exit_code == 2
    text = " ".join(result.output.split())
    assert "remove them (or the folder" in text
    assert "choose another directory" in text
    assert "network access" not in text


def test_uv_is_never_taken_from_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `.` and an empty entry on PATH, the uv found is the absolute one, never `./uv`."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for folder in (tools, tmp_path):
        (folder / "uv").write_text("#!/bin/sh\nexit 0\n")
        (folder / "uv").chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(tools)]))
    seen: list[str] = []

    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:  # ruff: ignore[any-type]
        if argv[1:2] == ["lock"]:
            seen.append(argv[0])
            (Path(options["cwd"]) / "uv.lock").write_text("version = 1\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with mock.patch("lakana.cli.project_init.subprocess.run", run):
        result = invoke(make_state(tmp_path, FakeApi()), "init", "p", "--template", "python")

    assert result.exit_code == 0, result.output
    assert seen == [str(tools / "uv")]


def test_a_lock_uv_left_unchanged_is_not_written_again(tmp_path: Path) -> None:
    """Putting back a lock that did not change leaves the file alone (same inode, same bytes)."""
    from lakana.cli.project_init import _restore

    lock = tmp_path / "uv.lock"
    lock.write_text("version = 1\n")
    before = lock.stat()

    _restore(lock, b"version = 1\n")

    assert lock.stat().st_mtime_ns == before.st_mtime_ns
