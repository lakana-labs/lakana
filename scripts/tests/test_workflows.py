"""Tests that the CI and release workflows keep the guarantees the project relies on."""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from scripts.time_budgets import BUDGETS, JobTime, check_jobs, read_budgets

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

_UV_ENV = re.compile(r"\buv (sync|run)\b")


def _jobs(workflow: str) -> dict:
    """Load the jobs of a workflow file.

    Parameters
    ----------
    workflow
        File name under ``.github/workflows``.

    Returns
    -------
    dict
        The workflow's ``jobs`` mapping.
    """
    return yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"]


def _commands(step: dict) -> list[str]:
    r"""Split a step's ``run`` script into commands, joining ``\`` continuations.

    Parameters
    ----------
    step
        A workflow step.

    Returns
    -------
    list of str
        One entry per command line; empty for a step without ``run``.
    """
    script = str(step.get("run", "")).replace("\\\n", " ")
    return [line.strip() for line in script.splitlines() if line.strip()]


def unlocked_environment_commands(job: dict) -> list[str]:
    """Find the commands that may re-resolve ``uv.lock`` silently.

    ``uv sync`` always counts. ``uv run`` counts only while no ``uv sync`` has
    run earlier in the job: it then builds the environment itself, and
    afterwards it finds one already in place.

    Parameters
    ----------
    job
        A workflow job.

    Returns
    -------
    list of str
        The commands lacking ``--locked``.
    """
    synced = False
    unlocked = []
    for step in job.get("steps", []):
        for command in _commands(step):
            for match in _UV_ENV.finditer(command):
                if match.group(1) == "sync":
                    synced = True
                elif synced:
                    continue
                if "--locked" not in command:
                    unlocked.append(command)
    return unlocked


def test_the_checker_flags_a_sync_without_locked() -> None:
    """A job that syncs without ``--locked`` is reported; one that locks is not."""
    bad = {"steps": [{"run": "uv sync --all-packages"}]}
    good = {"steps": [{"run": "uv sync --all-packages --locked"}, {"run": "uv run pytest"}]}
    cold_run = {"steps": [{"run": "uv build\nuv run bash x.sh"}]}
    wrapped = {"steps": [{"run": "uv sync \\\n  --all-packages"}]}
    assert unlocked_environment_commands(bad) == ["uv sync --all-packages"]
    assert unlocked_environment_commands(good) == []
    assert unlocked_environment_commands(cold_run) == ["uv run bash x.sh"]
    assert len(unlocked_environment_commands(wrapped)) == 1


@pytest.mark.parametrize(
    "workflow", sorted(p.name for p in [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
)
def test_every_job_that_builds_the_environment_uses_the_lock_as_is(workflow: str) -> None:
    """A lock that does not match ``pyproject.toml`` fails CI instead of being re-resolved."""
    for name, job in _jobs(workflow).items():
        assert unlocked_environment_commands(job) == [], f"{workflow}: job {name!r}"


def test_the_release_compose_smoke_job_checks_out_the_full_history() -> None:
    """It builds a wheel whose version (from git tags) must equal the pushed image's."""
    checkout = next(
        step
        for step in _jobs("release.yml")["compose-smoke-release"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0


def test_pip_audit_audits_every_dependency_group() -> None:
    """The audit runs on an environment synced with all groups, not only the default one."""
    steps = _jobs("security.yml")["static"]["steps"]
    sync = next(c for s in steps for c in _commands(s) if c.startswith("uv sync"))
    audit = next(c for s in steps for c in _commands(s) if "pip-audit" in c)
    assert "--all-groups" in sync
    assert "--all-groups" in audit


def test_the_secret_scan_runs_the_checksummed_gitleaks_binary() -> None:
    """The secret scan runs the gitleaks command line (MIT), checked against a pinned SHA-256.

    The gitleaks GitHub Action needs a paid licence key for a repository owned by an
    organisation, so the job downloads one pinned release, refuses it unless its checksum
    matches, and scans the pull request's commits (or the pushed ones).
    """
    job = _jobs("security.yml")["secrets"]
    assert not any("gitleaks/gitleaks-action" in str(s.get("uses", "")) for s in job["steps"])
    commands = "\n".join(c for s in job["steps"] for c in _commands(s))
    assert "sha256sum -c" in commands
    assert "releases/download/v${GITLEAKS_VERSION}/" in commands
    env = next(s["env"] for s in job["steps"] if "GITLEAKS_SHA256" in s.get("env", {}))
    assert len(env["GITLEAKS_SHA256"]) == 64
    assert '/gitleaks" git ' in commands
    assert "--log-opts" in commands
    assert set(job["permissions"]) == {"contents"}


def test_dependabot_watches_the_docker_image_pins() -> None:
    """The base image and the pinned uv image in the Dockerfile get update pull requests."""
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    docker = [u for u in config["updates"] if u["package-ecosystem"] == "docker"]
    assert [u["directory"] for u in docker] == ["/", "/packages/lakana-worker/tests/sshd"]
    # A directory that does not hold a Dockerfile would be watched for nothing.
    for update in docker:
        assert (ROOT / update["directory"].lstrip("/") / "Dockerfile").is_file()


_BASE_IMAGE = re.compile(r"^FROM (?P<image>[\w./-]+:[\w.-]+)@sha256:[0-9a-f]{64}( AS \w+)?$")


@pytest.mark.parametrize(
    "dockerfile", ["Dockerfile", "packages/lakana-worker/tests/sshd/Dockerfile"]
)
def test_every_base_image_is_pinned_by_tag_and_digest(dockerfile: str) -> None:
    """A tag moves; a digest does not. Dependabot's docker ecosystem bumps both together."""
    froms = [
        line
        for line in (ROOT / dockerfile).read_text().splitlines()
        if line.upper().startswith("FROM ")
    ]
    assert froms
    unpinned = [line for line in froms if not _BASE_IMAGE.match(line)]
    assert not unpinned, f"{dockerfile}: pin by `image:tag@sha256:...`: {unpinned}"


def test_the_uv_image_is_a_from_line_so_dependabot_can_read_it() -> None:
    """Dependabot ignores the image in ``COPY --from=<image>`` (dependabot-core #5103)."""
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    assert not any(re.match(r"COPY --from=[^ ]*[:/@]", line) for line in lines)
    assert any(
        re.match(r"FROM ghcr\.io/astral-sh/uv:\S+@sha256:\S+ AS uv$", line) for line in lines
    )


RUNNER = (
    "${{ (github.actor != 'dependabot[bot]'"
    " && (github.event_name != 'pull_request'"
    " || github.event.pull_request.head.repo.full_name == github.repository)"
    " && vars.CI_RUNNER) || 'ubuntu-latest' }}"
)
MACOS_JOB = "unit-macos"
SWITCHABLE = ("ci.yml", "security.yml", "conventional.yml", "mutation.yml", "security-deep.yml")


@pytest.mark.parametrize("workflow", SWITCHABLE)
def test_every_ci_job_can_run_on_the_configured_runner(workflow: str) -> None:
    """The repository variable `CI_RUNNER` moves CI to a self-hosted runner; unset, it is GitHub's.

    A pull request from Dependabot always runs on GitHub's runner: it executes
    dependency versions nobody has looked at yet.
    """
    for name, job in _jobs(workflow).items():
        if (workflow, name) == ("ci.yml", MACOS_JOB):
            continue  # GitHub offers macOS only on its own runners: see the test below
        assert job["runs-on"] == RUNNER, f"{workflow}: job `{name}` must use `runs-on: {RUNNER}`"


def test_a_release_is_built_and_published_on_github_s_runners() -> None:
    """What is published must not be built on a contributor's machine.

    Only the job that opens and merges the release pull request may move; it
    builds nothing.
    """
    jobs = _jobs("release.yml")
    assert jobs["release-please"]["runs-on"] == RUNNER
    for name, job in jobs.items():
        if name != "release-please":
            assert job["runs-on"] == "ubuntu-latest", f"release.yml: job `{name}`"


def test_release_please_opens_its_pull_requests_with_the_dedicated_token() -> None:
    """A pull request made with the default token gets no CI run, so the release PR uses a PAT."""
    steps = _jobs("release.yml")["release-please"]["steps"]
    step = next(
        s for s in steps if str(s.get("uses", "")).startswith("googleapis/release-please-action@")
    )
    assert step["with"]["token"] == "${{ secrets.RELEASE_PLEASE_TOKEN }}"


def test_the_first_public_release_is_bootstrapped_to_0_12_0() -> None:
    """Without a tag, release-please starts at 0.0.0 and proposes 0.12.0.

    The manifest holds 0.0.0 until the first release and the released version after it; its
    own release pull request rewrites it, so either is right here.
    """
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    config = json.loads((ROOT / "release-please-config.json").read_text())
    version = tuple(int(part) for part in manifest["."].split("."))
    assert version == (0, 0, 0) or version >= (0, 12, 0)
    assert config["initial-version"] == "0.12.0"
    for key in ("bootstrap-sha", "last-release-sha", "release-as"):
        assert key not in config
        assert key not in config["packages"]["."]


def _publish_environments() -> dict[str, str]:
    """Collect the GitHub environment of each package's publish job in ``release.yml``.

    Returns
    -------
    dict
        Package name to environment name, the matrix legs of ``publish-runtime`` included.
    """
    jobs = _jobs("release.yml")
    found = {
        "lakana-core": jobs["publish-core"]["environment"],
        "lakana": jobs["publish-sdk"]["environment"],
    }
    for leg in jobs["publish-runtime"]["strategy"]["matrix"]["include"]:
        found[leg["package"]] = leg["environment"]
    return found


def test_each_package_publishes_to_production_pypi_from_its_own_environment() -> None:
    """The trusted publishers on pypi.org name these environments; a rename breaks the upload."""
    assert _publish_environments() == {
        "lakana": "pypi-lakana",
        "lakana-core": "pypi-lakana-core",
        "lakana-server": "pypi-lakana-server",
        "lakana-worker": "pypi-lakana-worker",
    }


_BEFORE_THE_GATE = {"release-please", "build", "sbom", "approve"}
_APPROVE_KEYS = {"needs", "if", "runs-on", "timeout-minutes", "environment", "permissions", "steps"}
_STATUS_FUNCTIONS = re.compile(r"\b(always|cancelled|failure|success)\s*\(")
"""Any status function in an ``if`` drops the implicit ``success() &&``.

The job could then run after a rejection.
"""


def _reaches(jobs: dict, name: str, target: str) -> bool:
    """Tell whether a job depends on another one, directly or through its ``needs``.

    Parameters
    ----------
    jobs
        The workflow's ``jobs`` mapping.
    name
        The job to start from.
    target
        The job it must depend on.

    Returns
    -------
    bool
        ``True`` when ``target`` is in the transitive ``needs`` of ``name``.
    """
    needs = jobs[name].get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    return any(n == target or _reaches(jobs, n, target) for n in needs)


def _gate_violations(jobs: dict) -> list[str]:
    """List the ways a release workflow lets something happen before or despite the approval.

    Parameters
    ----------
    jobs
        The workflow's ``jobs`` mapping.

    Returns
    -------
    list of str
        One message per violation; empty when the gate holds.
    """
    problems = []
    approve = jobs["approve"]
    if approve.get("environment") != "release-approval":
        problems.append("approve is not in the environment `release-approval`")
    if approve.get("permissions") != {}:
        problems.append("approve holds permissions")
    if set(approve) != _APPROVE_KEYS:
        problems.append(f"approve has the keys {sorted(set(approve))}")
    steps = approve.get("steps", [])
    if len(steps) != 1 or set(steps[0]) - {"run", "env"} or "run" not in steps[0]:
        problems.append("approve runs more than one plain `run` step")
    if set(approve.get("needs", [])) != {"release-please", "build"}:
        problems.append("approve does not need release-please and build")
    for name, job in jobs.items():
        if name in _BEFORE_THE_GATE:
            if name in {"release-please", "approve"}:
                continue
            if "write" in (job.get("permissions") or {}).values():
                problems.append(f"`{name}` runs before the gate with a write permission")
            if re.search(r"\bsecrets\b", str(job)):
                problems.append(f"`{name}` runs before the gate and reads a secret")
            continue
        if not _reaches(jobs, name, "approve"):
            problems.append(f"`{name}` does not wait for approve")
        if _STATUS_FUNCTIONS.search(str(job.get("if", ""))):
            problems.append(
                f"`{name}` has a status function in its `if` and can run after a rejection"
            )
    return problems


def test_one_approval_job_gates_a_release() -> None:
    """Only release-please, build and sbom run before ``approve``; none runs after a rejection."""
    jobs = _jobs("release.yml")
    assert _gate_violations(jobs) == []
    assert jobs["approve"]["if"] == "needs.release-please.outputs.released == 'true'"


@pytest.mark.parametrize(
    ("break_it", "expected"),
    [
        (lambda j: j["publish-core"].update(needs=["build"]), "`publish-core` does not wait"),
        (lambda j: j["pages"].update(**{"if": "always()"}), "`pages` has a status function"),
        (lambda j: j["pages"].update(**{"if": "success() || x"}), "`pages` has a status function"),
        (lambda j: j["approve"].update(**{"continue-on-error": True}), "approve has the keys"),
        (
            lambda j: j["approve"].update(permissions={"contents": "write"}),
            "approve holds permissions",
        ),
        (lambda j: j["approve"]["steps"].append({"uses": "x/y@z"}), "more than one plain"),
        (lambda j: j.update(upload={"needs": "build", "steps": []}), "`upload` does not wait"),
        (lambda j: j["pages"].update(**{"if": "${{ !success() }}"}), "`pages` has a status"),
        (lambda j: j["pages"].update(**{"if": "x || success()"}), "`pages` has a status"),
        (
            lambda j: j["build"].update(permissions={"contents": "write"}),
            "`build` runs before the gate with a write permission",
        ),
        (
            lambda j: j["sbom"]["steps"].append({"run": "x", "env": {"T": "${{ secrets.T }}"}}),
            "`sbom` runs before the gate and reads a secret",
        ),
        (
            lambda j: j["sbom"].update(secrets="inherit"),
            "`sbom` runs before the gate and reads a secret",
        ),
    ],
)
def test_the_gate_check_catches_a_broken_workflow(
    break_it: Callable[[dict], object], expected: str
) -> None:
    """The check flags each way of weakening the gate, on a copy of the real workflow."""
    jobs = copy.deepcopy(_jobs("release.yml"))
    break_it(jobs)
    assert any(expected in problem for problem in _gate_violations(jobs))


def test_no_release_step_points_at_the_test_index() -> None:
    """Production PyPI is the default; no step overrides it with a repository or index URL."""
    text = (WORKFLOWS / "release.yml").read_text()
    assert "test.pypi.org" not in text
    assert "repository-url" not in text


def test_dependabot_groups_its_updates_and_keeps_the_pinned_tools_apart() -> None:
    """One pull request per ecosystem and week; ruff and ty upgrade in their own."""
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    updates = {entry["package-ecosystem"]: entry for entry in config["updates"]}
    assert set(updates) == {"uv", "github-actions", "docker", "pip", "npm"}
    for ecosystem, entry in updates.items():
        groups = entry["groups"]
        assert len(groups) == 1, ecosystem
        (group,) = groups.values()
        assert group["patterns"] == ["*"], ecosystem
    (python,) = updates["uv"]["groups"].values()
    assert python["exclude-patterns"] == ["ruff", "ty"]


def test_dependabot_watches_the_pinned_jscpd() -> None:
    """The lockfile of the duplication check is updated by Dependabot, as a hidden `ci` change."""
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    (npm,) = (entry for entry in config["updates"] if entry["package-ecosystem"] == "npm")
    assert npm["directory"] == "/tools/jscpd"
    assert npm["commit-message"]["prefix"] == "ci"
    assert (ROOT / "tools" / "jscpd" / "package-lock.json").is_file()


def test_jscpd_is_pinned_to_one_exact_version() -> None:
    """No range and no bare `npx jscpd`: the version CI runs is the one in the lock."""
    package = json.loads((ROOT / "tools" / "jscpd" / "package.json").read_text())
    assert re.fullmatch(r"\d+\.\d+\.\d+", package["devDependencies"]["jscpd"])
    assert set(package["devDependencies"]) == {"jscpd"}


def test_lint_measures_duplication_in_the_sources_and_runs_the_ratchet() -> None:
    """`lint` installs the locked jscpd, scans the packages' source and ratchets the report.

    Tests are not scanned (they repeat their setup on purpose), the report is JSON, and the
    ratchet runs after jscpd so that it never reads a stale report.
    """
    steps = _jobs("ci.yml")["lint"]["steps"]
    commands = [command for step in steps for command in _commands(step)]
    install = next(i for i, c in enumerate(commands) if c.startswith("npm ci"))
    scan = next(i for i, c in enumerate(commands) if "node_modules/.bin/jscpd" in c)
    ratchet = next(i for i, c in enumerate(commands) if "scripts/duplication_ratchet.py" in c)
    assert install < scan < ratchet
    assert "--prefix tools/jscpd" in commands[install]
    assert "--ignore-scripts" in commands[install]
    assert "packages/*/src" in commands[scan]
    assert "tests" not in commands[scan]
    assert "--min-tokens 70" in commands[scan]
    assert "--reporters json" in commands[scan]
    assert commands[scan].startswith("tools/jscpd/node_modules/.bin/jscpd")
    node = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/setup-node")
    )
    assert steps.index(node) < next(i for i, s in enumerate(steps) if "npm ci" in str(s.get("run")))


def test_lint_keeps_the_read_only_token_while_it_runs_jscpd() -> None:
    """`lint` declares no permissions of its own, so it keeps the read-only top level."""
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in workflow["jobs"]["lint"]


def _test_steps(job: str = "test") -> dict[str, dict]:
    """Index the steps of a job in ``ci.yml`` by name.

    Parameters
    ----------
    job
        The job: ``test`` (the required checks that collect the shards) or ``unit``.

    Returns
    -------
    dict
        Step name to step, for the steps that have a name.
    """
    return {s["name"]: s for s in _jobs("ci.yml")[job]["steps"] if "name" in s}


MEASURED = "matrix.python-version == '3.14' && needs.changes.outputs.docs_only != 'true'"


def test_only_the_python_3_14_run_measures_coverage() -> None:
    """The ratchet reads the 3.14 report; measuring on 3.11 costs a third of its run time."""
    steps = _test_steps("unit")
    measured = steps["Unit tests (with coverage)"]
    plain = steps["Unit tests"]
    assert measured["if"] == "matrix.python-version == '3.14'"
    assert "--cov" in measured["run"]
    assert plain["if"] == "matrix.python-version != '3.14'"
    assert "--cov" not in plain["run"]
    assert _test_steps()["Coverage ratchet"]["if"] == MEASURED


def test_the_3_14_run_uploads_its_coverage_report_also_when_the_ratchet_fails() -> None:
    """A missed line can be looked up in the report the ratchet read, without a new run."""
    steps = _test_steps()
    names = list(steps)
    upload = steps["Upload the coverage report"]
    assert upload["if"] == "always() && " + MEASURED
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["name"] == "coverage-report"
    assert upload["with"]["path"] == "coverage.json"
    assert names.index("Upload the coverage report") > names.index("Coverage ratchet")


def test_the_unit_suite_runs_in_three_shards_per_version() -> None:
    """Each shard runs a third of the test files, chosen by ``scripts/pytest_selection.py``."""
    job = _jobs("ci.yml")["unit"]
    assert job["strategy"]["matrix"]["shard"] == [1, 2, 3]
    assert job["strategy"]["matrix"]["python-version"] == (
        '${{ fromJSON(needs.changes.outputs.python_versions || \'["3.11","3.14"]\') }}'
    )
    assert job["strategy"]["fail-fast"] is False
    assert job["name"] == "unit (${{ matrix.python-version }}, ${{ matrix.shard }}/3)"
    assert job["needs"] == "changes"
    steps = _test_steps("unit")
    for name in ("Unit tests (with coverage)", "Unit tests"):
        assert steps[name]["env"] == {"LAKANA_TEST_SHARD": "${{ matrix.shard }}/3"}
    # The oldest-Polars step lists its files itself and must not be sharded.
    assert "env" not in steps["Unit tests on the oldest supported Polars"]
    assert "LAKANA_TEST_SHARD" not in str(job.get("env", {}))


def test_the_3_14_shards_hand_their_coverage_data_and_junit_reports_on() -> None:
    """``test (3.14)`` combines the data of every shard; one shard alone is not the suite."""
    unit = _test_steps("unit")
    assert unit["Keep the coverage data of this shard"]["run"] == (
        'mv .coverage ".coverage.shard-${{ matrix.shard }}"'
    )
    data = unit["Upload the coverage data of this shard"]
    assert data["with"]["name"] == "coverage-data-${{ matrix.shard }}"
    assert data["with"]["path"] == ".coverage.shard-${{ matrix.shard }}"
    assert data["with"]["include-hidden-files"] is True  # `.coverage.*` is a hidden file
    assert data["with"]["if-no-files-found"] == "error"
    junit = unit["Upload the junit report of this shard"]
    assert junit["with"]["name"] == "junit-unit-${{ matrix.shard }}"
    assert "--junitxml=junit-${{ matrix.shard }}.xml" in unit["Unit tests (with coverage)"]["run"]
    assert "-o junit_family=xunit1" in unit["Unit tests (with coverage)"]["run"]

    test = _test_steps()
    names = list(test)
    download = test["Download the shards' coverage data"]
    assert download["with"] == {
        "pattern": "coverage-data-*",
        "path": "coverage-data",
        "merge-multiple": True,
    }
    assert test["Combine the coverage of the shards"]["run"].splitlines() == [
        "uv run coverage combine coverage-data",
        "uv run coverage json",
        "uv run coverage report --skip-covered --show-missing",
    ]
    assert (
        names.index("Download the shards' coverage data")
        < names.index("Combine the coverage of the shards")
        < names.index("Coverage ratchet")
    )
    assert test["Slow-test ratchet"]["run"] == (
        "uv run python scripts/time_budgets.py slow-tests junit/*.xml"
    )
    assert test["Slow-test ratchet"]["if"] == MEASURED
    assert test["Download the shards' junit reports"]["with"]["pattern"] == "junit-unit-*"


def test_the_required_test_checks_pass_only_when_every_shard_did() -> None:
    """``test (3.11)`` and ``test (3.14)`` fail on any shard that did not succeed."""
    job = _jobs("ci.yml")["test"]
    assert job["needs"] == ["changes", "unit"]
    first = job["steps"][0]
    assert first["name"] == "Unit test shards"
    assert first["if"] == "needs.changes.outputs.docs_only != 'true'"
    assert first["env"] == {"RESULT": "${{ needs.unit.result }}"}
    assert '[ "$RESULT" != "success" ]' in first["run"]
    assert "exit 1" in first["run"]


def test_both_unit_runs_are_parallel_and_3_14_measures_with_the_c_tracer() -> None:
    """3.11 runs with up to three processes, the 3.14 coverage run with up to two.

    On Python 3.14 coverage.py would choose its ``sysmon`` core, which keeps every code object
    the suite ever compiles: 4.8 GB serially and out of memory in parallel. ``core = "ctrace"``
    in ``pyproject.toml`` is what lets the 3.14 run be parallel at all, so the two are pinned
    together. ``addopts`` must not carry ``-n``.
    """
    steps = _test_steps("unit")
    assert steps["Unit tests"]["run"] == "uv run pytest -n auto --maxprocesses 3 --dist loadfile"
    assert steps["Unit tests (with coverage)"]["run"] == (
        "uv run pytest -n auto --maxprocesses 2 --dist loadfile --cov --cov-report="
        " --junitxml=junit-${{ matrix.shard }}.xml -o junit_family=xunit1"
    )
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["tool"]["coverage"]["run"]["core"] == "ctrace"
    assert "-n" not in pyproject["tool"]["pytest"]["ini_options"]["addopts"].split()


def test_the_report_names_the_lines_a_miss_is_in() -> None:
    """A flaky branch must be named by the failing run itself."""
    run = _test_steps()["Combine the coverage of the shards"]["run"]
    assert "coverage json" in run
    assert "coverage report --skip-covered --show-missing" in run


def _workflow() -> dict:
    """Load ``ci.yml`` whole.

    Returns
    -------
    dict
        The parsed workflow; PyYAML reads the key ``on`` as ``True``.
    """
    return yaml.safe_load((WORKFLOWS / "ci.yml").read_text())


def _terms(condition: str) -> list[str]:
    """Split a job condition ``${{ a && b }}`` into its terms.

    Parameters
    ----------
    condition
        The ``if`` of a job, with or without the ``${{ }}`` wrapper.

    Returns
    -------
    list of str
        The ``&&``-joined terms, stripped.
    """
    text = condition.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return [term.strip() for term in text.split("&&")]


_OUTPUT_TERM = re.compile(r"^needs\.changes\.outputs\.(\w+) (==|!=) '(\w+)'$")


def runs(job: dict, outputs: dict[str, str]) -> bool:
    """Evaluate a job's ``if`` the way Actions does, for the terms ``ci.yml`` uses.

    Parameters
    ----------
    job
        A job of ``ci.yml``; without ``if`` it runs.
    outputs
        The outputs of the ``changes`` job; one that is not in it reads as the empty string,
        which is what Actions gives for an output that was never set.

    Returns
    -------
    bool
        Whether the job runs, assuming nothing is cancelled and ``changes`` succeeded.
    """
    for term in _terms(job.get("if", "true")):
        if term in ("true", "!cancelled()"):
            continue
        match = _OUTPUT_TERM.match(term)
        assert match, f"a condition this test cannot evaluate: {term}"
        name, operator, expected = match.groups()
        # `==` and `!=` on strings ignore case in Actions.
        if (outputs.get(name, "").lower() == expected.lower()) != (operator == "=="):
            return False
    return True


def test_the_steps_only_emit_lower_case_true_and_false() -> None:
    """Actions compares strings without regard to case; ``runs`` does too, so pin the spelling."""
    emitted: set[str] = set()
    for step in _jobs("ci.yml")["changes"]["steps"]:
        script = step.get("run", "")
        emitted.update(re.findall(r'(?:draft|verdict)="(\w+)"', script))
        emitted.update(re.findall(r'already_tested=(\w+)"?', script))
    assert emitted == {"true", "false"}
    assert not [e for e in emitted if e != e.lower()]


def test_the_evaluator_compares_strings_without_regard_to_case() -> None:
    """GitHub's ``==`` is case-insensitive; so is the stand-in used by these tests."""
    job = {"if": "${{ needs.changes.outputs.draft != 'true' }}"}
    assert runs(job, {"draft": "false"}) is True
    assert runs(job, {"draft": "TRUE"}) is False
    assert runs(job, {"draft": "True"}) is False


def test_a_pull_request_that_leaves_draft_gets_a_full_run() -> None:
    """``ready_for_review`` is not a default type; without it, leaving draft would run nothing."""
    types = _workflow()[True]["pull_request"]["types"]
    assert set(types) == {"opened", "synchronize", "reopened", "ready_for_review"}


def test_the_changes_job_reports_the_draft_state_and_the_versions_to_test() -> None:
    """Both are outputs of ``changes``; the versions are ``3.14`` alone only while a draft."""
    outputs = _jobs("ci.yml")["changes"]["outputs"]
    assert outputs["draft"] == "${{ steps.draft.outputs.draft }}"
    assert outputs["python_versions"] == (
        "${{ steps.draft.outputs.draft == 'true' && '[\"3.14\"]' || '[\"3.11\",\"3.14\"]' }}"
    )


def _draft_step() -> dict:
    """Find the step of ``changes`` that decides whether this is a draft.

    Returns
    -------
    dict
        The step with ``id: draft``.
    """
    return next(s for s in _jobs("ci.yml")["changes"]["steps"] if s.get("id") == "draft")


@pytest.mark.parametrize(
    ("event", "draft", "expected"),
    [
        ("pull_request", "true", "true"),
        ("pull_request", "false", "false"),
        ("pull_request", "", "false"),
        ("pull_request", "TRUE", "false"),
        ("pull_request", "yes", "false"),
        ("push", "true", "false"),
        ("push", "", "false"),
        ("", "", "false"),
    ],
)
def test_only_a_pull_request_that_says_it_is_a_draft_is_one(
    tmp_path: Path, event: str, draft: str, expected: str
) -> None:
    """Every other value, an empty one included, means "not a draft": everything runs."""
    step = _draft_step()
    out = tmp_path / "out"
    out.write_text("")
    env = {"EVENT": event, "DRAFT": draft, "GITHUB_OUTPUT": str(out), "PATH": os.environ["PATH"]}
    shell = step["shell"].replace("{0}", "").split()
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    subprocess.run([*shell, str(script)], env=env, check=True)
    assert out.read_text() == f"draft={expected}\n"
    assert set(step["env"]) == {"EVENT", "DRAFT"}
    assert step["env"]["EVENT"] == "${{ github.event_name }}"
    assert step["env"]["DRAFT"] == "${{ github.event.pull_request.draft }}"


def test_the_python_versions_come_from_the_changes_job() -> None:
    """A draft has no ``test (3.11)`` check at all, rather than a green one that did not run.

    The fallback is both versions: an empty output (the ``changes`` job failed) must not
    shrink the matrix.
    """
    job = _jobs("ci.yml")["test"]
    matrix = job["strategy"]["matrix"]
    assert matrix == {
        "python-version": (
            '${{ fromJSON(needs.changes.outputs.python_versions || \'["3.11","3.14"]\') }}'
        )
    }
    assert job["strategy"]["fail-fast"] is False
    assert "name" not in job


HEAVY_ON_READY = (
    "integration",
    "integration-services",
    "integration-docker",
    "integration-assets",
    "five-minute",
    "compose-smoke",
)
DRAFT_RUNS = ("lint", "unit", "docs-examples", "test", "docs", "quarantine", "budgets")


@pytest.mark.parametrize("name", HEAVY_ON_READY)
def test_the_jobs_that_wait_for_ready_for_review_carry_the_draft_condition(name: str) -> None:
    """These skip while a pull request is a draft, and only then."""
    assert "needs.changes.outputs.draft != 'true'" in _terms(_jobs("ci.yml")[name]["if"])


@pytest.mark.parametrize("name", ["changes", *DRAFT_RUNS])
def test_the_other_jobs_ignore_the_draft_state(name: str) -> None:
    """``lint``, ``docs``, ``docs-examples`` and ``test (3.14)`` run on a draft as before."""
    assert "draft" not in str(_jobs("ci.yml")[name].get("if", ""))


@pytest.mark.parametrize("draft", ["false", "", "anything"])
@pytest.mark.parametrize("name", [*HEAVY_ON_READY, *DRAFT_RUNS])
def test_nothing_is_skipped_for_a_pull_request_that_is_not_a_draft(name: str, draft: str) -> None:
    """Outside a draft, and not docs-only, every job of ``ci.yml`` runs."""
    outputs = {"docs_only": "false", "draft": draft}
    assert runs(_jobs("ci.yml")[name], outputs)


@pytest.mark.parametrize("name", HEAVY_ON_READY)
def test_a_draft_skips_exactly_the_heavy_jobs(name: str) -> None:
    """On a draft the heavy jobs are skipped; the guards below keep running."""
    outputs = {"docs_only": "false", "draft": "true"}
    assert not runs(_jobs("ci.yml")[name], outputs)
    for kept in DRAFT_RUNS:
        assert runs(_jobs("ci.yml")[kept], outputs)


def test_a_failed_changes_job_runs_everything() -> None:
    """With no outputs at all (``changes`` failed), no job's condition skips it."""
    for name, job in _jobs("ci.yml").items():
        if name != "changes":
            assert runs(job, {}), name


ALREADY_TESTED_OUTPUT = (
    "${{ github.event_name == 'push' && steps.tested.outputs.already_tested == 'true'"
    " && 'true' || 'false' }}"
)
SKIPPED_WHEN_TESTED = (
    "unit",
    "test",
    "integration",
    "integration-services",
    "integration-docker",
    "integration-assets",
    "five-minute",
    "compose-smoke",
    "docs-examples",
    "quarantine",
)
TESTED_TERM = "needs.changes.outputs.already_tested != 'true'"


def test_the_changes_job_may_read_the_runs_of_the_pull_request() -> None:
    """The already-tested script reads runs and pull requests: two read permissions, no more.

    The budget job reads the jobs of its own run, and nothing else.
    """
    changes = _jobs("ci.yml")["changes"]
    assert changes["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "actions": "read",
    }
    assert _jobs("ci.yml")["budgets"]["permissions"] == {"contents": "read", "actions": "read"}
    assert _workflow()["permissions"] == {"contents": "read"}
    for name, job in _jobs("ci.yml").items():
        assert name in ("changes", "budgets") or "permissions" not in job, name


def test_a_pull_request_event_never_gets_an_answer_from_the_script() -> None:
    """The output is true only for a push, whatever the step printed, and the step runs on push."""
    changes = _jobs("ci.yml")["changes"]
    assert changes["outputs"]["already_tested"] == ALREADY_TESTED_OUTPUT
    step = next(s for s in changes["steps"] if s.get("id") == "tested")
    assert step["if"] == "github.event_name == 'push'"
    assert step["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert step["shell"] == "bash --noprofile --norc {0}"
    assert "set -e" not in step["run"]
    assert all("uses" not in s for s in changes["steps"])
    # Fetched from the commit that was pushed to main, so a pull request's own copy never counts.
    assert "scripts/ci_already_tested.py?ref=${GITHUB_SHA}" in step["run"]


@pytest.mark.parametrize("name", SKIPPED_WHEN_TESTED)
def test_the_jobs_a_tested_commit_skips_carry_the_condition(name: str) -> None:
    """These five skip for a commit proven tested; ``!cancelled()`` comes first."""
    job = _jobs("ci.yml")[name]
    assert TESTED_TERM in _terms(job["if"])
    assert _terms(job["if"])[0] == "!cancelled()"


@pytest.mark.parametrize("name", ["changes", "lint", "docs", "budgets"])
def test_lint_and_docs_run_on_every_push_to_main(name: str) -> None:
    """``lint`` and ``docs`` are not part of the proof, so they run as before."""
    assert "already_tested" not in str(_jobs("ci.yml")[name].get("if", ""))


@pytest.mark.parametrize("name", SKIPPED_WHEN_TESTED)
def test_a_pull_request_runs_the_jobs_even_if_the_output_were_true(name: str) -> None:
    """A pull request gets ``already_tested == 'false'``: the expression pins it.

    The output expression is ``event == push && ... || 'false'``; for every non-push event it
    evaluates to the string ``false``, whatever the step printed.
    """
    assert ALREADY_TESTED_OUTPUT.startswith("${{ github.event_name == 'push' && ")
    assert ALREADY_TESTED_OUTPUT.endswith("|| 'false' }}")
    assert runs(
        _jobs("ci.yml")[name], {"docs_only": "false", "draft": "false", "already_tested": "false"}
    )


def _tested_step() -> dict:
    """Find the step of ``changes`` that runs the already-tested script.

    Returns
    -------
    dict
        The step with ``id: tested``.
    """
    return next(s for s in _jobs("ci.yml")["changes"]["steps"] if s.get("id") == "tested")


FAKE_GH = """#!/bin/sh
# Stands in for `gh api repos/R/contents/scripts/ci_already_tested.py?ref=SHA`.
[ "$FAKE_GH_FAIL" = "1" ] && exit 1
printf '%s\\n' "$FAKE_SCRIPT"
"""


@pytest.mark.parametrize(
    ("fetch_fails", "script", "expected"),
    [
        (False, "print('already_tested=true')", "true"),
        (False, "print('already_tested=false')", "false"),
        (False, "print('already_tested=maybe')", "false"),
        (False, "print('already_tested=true')\nprint('x=1')", "false"),
        (False, "raise SystemExit(3)", "false"),
        (False, "print('already_tested=true'); raise SystemExit(3)", "false"),
        (False, "syntax error here", "false"),
        (True, "print('already_tested=true')", "false"),
    ],
)
def test_the_step_prints_true_only_for_exactly_that_answer(
    tmp_path: Path, fetch_fails: bool, script: str, expected: str
) -> None:
    """Run the step with a stand-in for ``gh``; only a clean ``already_tested=true`` counts."""
    step = _tested_step()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_SHA": "a" * 40,
        "FAKE_SCRIPT": script,
        "FAKE_GH_FAIL": "1" if fetch_fails else "0",
    }
    runner = tmp_path / "step.sh"
    runner.write_text(step["run"])
    shell = step["shell"].replace("{0}", "").split()
    subprocess.run([*shell, str(runner)], env=env, check=True, capture_output=True)
    assert out.read_text() == f"already_tested={expected}\n"


# What runs for each event, from the conditions alone. `needs` outputs as `changes` sets them.
EVENTS = {
    "push to a draft": {"docs_only": "false", "draft": "true", "already_tested": "false"},
    "ready or later push": {"docs_only": "false", "draft": "false", "already_tested": "false"},
    "documentation only, draft": {"docs_only": "true", "draft": "true", "already_tested": "false"},
    "documentation only, ready": {"docs_only": "true", "draft": "false", "already_tested": "false"},
    "main, tested": {"docs_only": "false", "draft": "false", "already_tested": "true"},
    "main, not proven": {"docs_only": "false", "draft": "false", "already_tested": "false"},
}
_FULL_RUN = {
    "lint",
    "unit",
    "test",
    "unit-macos",
    "test-macos",
    "docs",
    "docs-examples",
    "integration",
    "integration-services",
    "integration-docker",
    "integration-assets",
    "five-minute",
    "compose-smoke",
    "quarantine",
    "budgets",
}
WHAT_RUNS = {
    "push to a draft": {"lint", "unit", "test", "docs", "docs-examples", "quarantine", "budgets"},
    "ready or later push": _FULL_RUN,
    "documentation only, draft": {"test", "docs", "budgets"},
    "documentation only, ready": {"test", "docs", "budgets"},
    "main, tested": {"lint", "docs", "unit-macos", "test-macos", "budgets"},
    "main, not proven": _FULL_RUN,
}


@pytest.mark.parametrize("event", sorted(EVENTS))
def test_which_jobs_run_for_each_event(event: str) -> None:
    """The table in ``docs/agent/testing.md``, as the conditions evaluate it."""
    jobs = _jobs("ci.yml")
    ran = {name for name, job in jobs.items() if name != "changes" and runs(job, EVENTS[event])}
    assert ran == WHAT_RUNS[event]


def test_the_test_job_never_skips_on_a_pull_request() -> None:
    """``test (3.14)`` is required: only a tested push to main may skip the job."""
    job = _jobs("ci.yml")["test"]
    assert _terms(job["if"]) == ["!cancelled()", TESTED_TERM]


def test_the_test_job_has_a_timeout() -> None:
    """A hung test must not hold a runner for the six hours Actions allows by default."""
    assert _jobs("ci.yml")["test"]["timeout-minutes"] == 15
    assert _jobs("ci.yml")["unit"]["timeout-minutes"] == 25


def _compose_filter() -> re.Pattern[str]:
    """Extract the path filter of the ``compose-smoke`` job.

    Returns
    -------
    re.Pattern
        The ``grep -qE`` pattern, compiled; its syntax is the part of ERE that Python shares.
    """
    steps = _jobs("ci.yml")["compose-smoke"]["steps"]
    script = next(s["run"] for s in steps if s.get("id") == "changes")
    match = re.search(r"grep -qE '([^']+)'", script)
    assert match
    return re.compile(match.group(1))


@pytest.mark.parametrize(
    "path",
    [
        "packages/lakana-core/src/lakana_core/artifacts/store.py",
        "packages/lakana-core/src/lakana_core/storage/s3.py",
        "packages/lakana-core/src/lakana_core/catalog/tables.py",
        "packages/lakana-core/src/lakana_core/anything_new/module.py",
        "packages/lakana-server/src/lakana_server/app.py",
        "packages/lakana/src/lakana/resources/compose.yaml",
        "packages/lakana-worker/src/lakana_worker/tasks.py",
        "packages/lakana/src/lakana/cli/deploy/compose.yaml",
        "Dockerfile",
        "uv.lock",
        "packages/lakana/pyproject.toml",
        "scripts/compose_smoke_test.sh",
        "docs/examples/compose-add-environment.sh",
        "docs/examples/compose-restore.sh",
        ".github/workflows/ci.yml",
    ],
)
def test_the_compose_smoke_test_runs_for_any_source_change(path: str) -> None:
    """The only job that starts the real console scripts must see every package's source."""
    assert _compose_filter().search(path)


@pytest.mark.parametrize(
    "path",
    [
        "docs/site/index.md",
        "README.md",
        "packages/lakana-core/tests/test_x.py",
        "packages/lakana-server/tests/test_app.py",
        "scripts/docs_only.py",
        "scripts/tests/test_workflows.py",
        "docs/examples/getting-started/run.sh",
    ],
)
def test_the_compose_smoke_test_ignores_what_cannot_change_the_images(path: str) -> None:
    """Tests, scripts that are not in the image, and documentation do not start it."""
    assert not _compose_filter().search(path)


def _run_compose_changes_step(tmp_path: Path, commits: list[str]) -> str:
    """Run the ``changes`` step of ``compose-smoke`` in a scratch repository.

    Parameters
    ----------
    tmp_path
        Directory for the scratch repository and the output file.
    commits
        One entry per commit: the file it adds.

    Returns
    -------
    str
        The content the step wrote to ``GITHUB_OUTPUT``.
    """
    steps = _jobs("ci.yml")["compose-smoke"]["steps"]
    script = next(s["run"] for s in steps if s.get("id") == "changes")
    script = re.sub(r"\$\{\{[^}]*'(HEAD~1)'\s*\}\}", r"\1", script)
    assert "${{" not in script
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GITHUB_OUTPUT": str(tmp_path / "out"), "GIT_CONFIG_GLOBAL": os.devnull}
    (tmp_path / "out").write_text("")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

    git("init", "-q")
    for name in commits:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("x")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-m", name)
    subprocess.run(["bash", "-e", "-c", script], cwd=repo, env=env, check=True)
    return (tmp_path / "out").read_text()


def test_the_compose_smoke_test_runs_on_a_root_commit(tmp_path: Path) -> None:
    """Without a parent there is nothing to compare with, so everything counts as changed."""
    assert "run=true" in _run_compose_changes_step(tmp_path, ["docs/index.md"])


def test_the_compose_smoke_test_still_skips_a_commit_that_changes_nothing_relevant(
    tmp_path: Path,
) -> None:
    """The root-commit rule must not turn the filter off for later commits."""
    assert _run_compose_changes_step(tmp_path, ["a.txt", "docs/index.md"]) == ""


def test_the_compose_smoke_test_runs_for_a_later_commit_that_changes_the_image(
    tmp_path: Path,
) -> None:
    """A parent exists, and the filter matches the change."""
    assert "run=true" in _run_compose_changes_step(tmp_path, ["a.txt", "Dockerfile"])


def test_the_compose_smoke_test_starts_the_worker_kinds_after_its_restart() -> None:
    """The extra workers start after the restart; the deploy checks look at them."""
    script = (ROOT / "scripts" / "compose_smoke_test.sh").read_text()
    restart = script.index("docker compose down\n")
    kinds = script.index("set_env LAKANA_WORKERS_INTERACTIVE 1")
    assert restart < kinds
    after = script[kinds:]
    assert "set_env LAKANA_WORKERS_PIPELINE 1" in after
    assert "set_env LAKANA_WORKERS 0" in after
    # Compose 2.x fails `up --wait` while it removes the replicas of a service that goes to zero:
    # the script lowers `worker` without --wait first.
    lowered = after.split("set_env LAKANA_WORKERS 0\n", 1)[1]
    commands = [line for line in lowered.splitlines() if line.startswith("docker compose up")]
    assert commands[0] == "docker compose up -d"
    for check in ("lakana-canary-", "stat -c", "test ! -e /var/run/docker.sock"):
        assert any(check in line and "worker-pipeline" in line for line in after.splitlines()), (
            check
        )


def test_the_compose_smoke_test_builds_its_wheels_for_the_image_s_python() -> None:
    """The wheel directory is for the interpreter the image has, never a version written down.

    A version written in the script went stale when the image moved to a newer Python, and the
    smoke project's deploy was refused because its lock admitted only the old one.
    """
    script = (ROOT / "scripts" / "compose_smoke_test.sh").read_text()
    assigned = [line for line in script.splitlines() if line.startswith("python=")]
    assert assigned == [
        'python=$(docker run --rm "$IMAGE" python -c '
        "'import sys; print(\"%d.%d\" % sys.version_info[:2])')"
    ]


def test_the_compose_smoke_job_builds_a_task_image_and_hands_it_to_the_script() -> None:
    """CI's bundle runs Python tasks in containers, from a task image built from this checkout."""
    steps = _jobs("ci.yml")["compose-smoke"]["steps"]
    commands = [c for s in steps for c in _commands(s)]

    assert "docker build -f Dockerfile.task --build-arg PYTHON=3.14 -t lakana-task:ci-py3.14 ." in (
        commands
    )
    assert "uv run --locked bash scripts/compose_smoke_test.sh lakana:ci lakana-task:ci" in commands


def test_the_release_compose_smoke_job_uses_the_published_task_images() -> None:
    """The release's own task images, which `publish-task-images` pushed before it."""
    job = _jobs("release.yml")["compose-smoke-release"]
    commands = [c for s in job["steps"] for c in _commands(s)]

    assert "publish-task-images" in job["needs"]
    assert (
        'uv run --locked bash scripts/compose_smoke_test.sh "ghcr.io/lakana-labs/lakana:'
        '${{ steps.version.outputs.version }}" "ghcr.io/lakana-labs/lakana-task:'
        '${{ steps.version.outputs.version }}"'
    ) in commands


def test_the_compose_smoke_test_has_the_task_images_the_launcher_needs() -> None:
    """The image's Python for the asset, and 3.11, the image the launcher's start-up probes use.

    A local tag (CI's ``lakana-task:ci``) cannot be pulled, so the script builds what is missing.
    """
    script = (ROOT / "scripts" / "compose_smoke_test.sh").read_text()

    assert "TASK_IMAGE=${2:-${IMAGE%:*}-task:${IMAGE##*:}}" in script
    assert 'for version in 3.11 "$python"; do' in script
    assert '--build-arg "PYTHON=$version"' in script
    assert '--task-image "$TASK_IMAGE"' in script


def test_the_compose_smoke_test_builds_only_a_local_task_image_and_pulls_a_published_one() -> None:
    """The release smoke must run on the published task images: a failed pull fails it.

    A reference with a registry or namespace (a ``/``) is pulled, and a pull error stops the
    script (``set -e``); only a local tag (CI's ``lakana-task:ci``) is built from the checkout.
    """
    script = (ROOT / "scripts" / "compose_smoke_test.sh").read_text()
    loop = script.split('for version in 3.11 "$python"; do', 1)[1].split("\ndone\n", 1)[0]

    assert '*/*) docker pull "$reference" ;;' in loop
    assert '*) docker build -f "$here/../Dockerfile.task"' in loop
    assert "|| docker" not in loop and "&& ! docker pull" not in loop
    assert "2>&1" not in loop.split('docker pull "$reference"', 1)[1].split(";;", 1)[0]


def test_the_release_compose_smoke_job_has_time_for_the_task_images() -> None:
    """It pulls two task images and builds an environment as well: 30 minutes, as in CI."""
    assert _jobs("release.yml")["compose-smoke-release"]["timeout-minutes"] == 30


def test_the_compose_smoke_test_proves_the_task_ran_in_a_container_and_cleans_up() -> None:
    """The worker holds no Docker socket, an environment volume was built, and it is removed.

    ``down -v`` does not know the launcher's volumes, so the cleanup removes them by label.
    """
    script = (ROOT / "scripts" / "compose_smoke_test.sh").read_text()
    cleanup = script.split("cleanup() {", 1)[1].split("\n}\n", 1)[0]
    listing = 'docker volume ls -q --filter "label=lakana.launcher.stack=$project_name"'

    assert f"{listing} | xargs -r docker volume rm" in cleanup
    assert cleanup.index("down -v") < cleanup.index(listing)
    for service in ("worker", "worker-pipeline"):
        assert f"docker compose exec -T {service} sh -c 'test ! -e /var/run/docker.sock'" in script
    assert script.count(f"{listing} | grep -- '-env-' >/dev/null") == 2
    assert "$cache/venvs/" not in script


POLARS_STEP = "Unit tests on the oldest supported Polars"
"""The name of the step of ``ci.yml`` that runs the Polars tests on the oldest Polars."""

POLARS_EXCLUDED = {
    "packages/lakana-server/tests/test_python_assets_integration.py": (
        "needs Postgres and SeaweedFS containers (an integration test, not run in the unit job); "
        "the unit tests of the same code run on Polars 1.30 in the step"
    ),
}
"""Test files that import Polars and are deliberately not in the step, with the reason."""

_POLARS_IMPORT = re.compile(
    r"""\b(?:import polars|from polars)\b|\b(?:importorskip|import_module)\(\s*["']polars["']"""
)


def _polars_step() -> dict:
    """Find the step that runs the tests on the oldest supported Polars.

    Returns
    -------
    dict
        The step of the ``unit`` job of ``ci.yml`` named `POLARS_STEP`.
    """
    return next(
        step for step in _jobs("ci.yml")["unit"]["steps"] if step.get("name") == POLARS_STEP
    )


def polars_test_files_missing(listed: set[str], root: Path, excluded: set[str]) -> list[str]:
    """Find test files that import Polars and are neither listed nor excluded.

    Parameters
    ----------
    listed
        The test paths (relative to `root`) the step runs.
    root
        The repository root.
    excluded
        Paths that are left out on purpose.

    Returns
    -------
    list of str
        Relative paths, sorted.
    """
    found = {
        path.relative_to(root).as_posix()
        for path in root.glob("packages/*/tests/**/*.py")
        if _POLARS_IMPORT.search(path.read_text(encoding="utf-8"))
    }
    return sorted(found - listed - excluded)


def test_the_polars_checker_finds_a_file_that_is_missing(tmp_path: Path) -> None:
    """A test file that imports Polars must be listed or excluded; any other may be absent."""
    tests = tmp_path / "packages" / "p" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_a.py").write_text("import polars as pl\n")
    (tests / "test_b.py").write_text("x = 'from polars import col'\n")
    (tests / "test_c.py").write_text("import pyarrow\n")
    (tests / "test_d.py").write_text("import polars\n")
    (tests / "test_e.py").write_text("pl = pytest.importorskip('polars')\n")
    (tests / "test_f.py").write_text('pl = importlib.import_module("polars")\n')
    (tests / "test_g.py").write_text('pl = pytest.importorskip( "polars" )\n')
    (tests / "test_h.py").write_text("pl = pytest.importorskip('polars_other')\n")
    left = [f"packages/p/tests/test_{name}.py" for name in "bdefg"]
    assert polars_test_files_missing({"packages/p/tests/test_a.py"}, tmp_path, set()) == left
    assert polars_test_files_missing({"packages/p/tests/test_a.py"}, tmp_path, set(left)) == []


def test_every_test_file_that_imports_polars_runs_on_the_oldest_polars() -> None:
    """The step's list of files is not a trap: a new Polars test file fails here until added."""
    command = " ".join(_commands(_polars_step()))
    listed = {word for word in command.split() if word.startswith("packages/")}
    assert polars_test_files_missing(listed, ROOT, set(POLARS_EXCLUDED)) == []
    assert all(POLARS_EXCLUDED.values()), "every exclusion needs a reason"
    assert all((ROOT / path).is_file() for path in [*listed, *POLARS_EXCLUDED]), "a path is gone"


def test_the_oldest_polars_step_asserts_the_version_it_runs_on() -> None:
    """``--with`` shadows the locked Polars 2; a change in uv's layering must fail, not pass."""
    commands = _commands(_polars_step())
    assert "polars.__version__.startswith('1.30.')" in commands[0]
    assert commands[0].startswith("uv run --with 'polars==1.30.*' python -c")
    assert commands[1].startswith("uv run --with 'polars==1.30.*' pytest")


def test_a_fork_pull_request_never_reaches_the_configured_runner() -> None:
    """The `CI_RUNNER` expression falls back to GitHub's runner for a pull request from a fork.

    On a public repository a fork's code runs in a ``pull_request`` job; on a self-hosted runner
    it would run as the runner's owner. Only a pull request whose head is in this repository
    (and a push, a schedule or a manual run, which only maintainers can start) may use it.
    """
    assert "github.event_name != 'pull_request'" in RUNNER
    assert "github.event.pull_request.head.repo.full_name == github.repository" in RUNNER
    assert RUNNER.index("head.repo.full_name") < RUNNER.index("vars.CI_RUNNER")
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            runs_on = str(job["runs-on"])
            assert "self-hosted" not in runs_on, f"{path.name}: job `{name}`"
            if "CI_RUNNER" in runs_on:
                assert runs_on == RUNNER, f"{path.name}: job `{name}`"


_USES = re.compile(r"^\s*(?:-\s+)?uses:\s+(?P<ref>\S+)(?P<rest>.*)$")
_PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")


def test_every_action_is_pinned_by_commit_with_its_version_in_a_comment() -> None:
    """A tag can be moved after the fact; a full commit hash cannot.

    Each `uses:` names a full 40-character commit and carries the version it stands for in a
    trailing comment, which is what Dependabot rewrites when it bumps the pin.
    """
    unpinned = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            match = _USES.match(line)
            if match is None or match["ref"].startswith("./"):
                continue
            if not _PINNED.match(match["ref"]) or not re.match(r"\s+#\s*v\d", match["rest"]):
                unpinned.append(f"{path.name}:{number}: {line.strip()}")
    assert not unpinned, "\n".join(unpinned)


def test_the_cla_workflow_starts_only_for_pull_requests_and_the_signing_phrases() -> None:
    """A comment on a plain issue, or any other comment, does not start the CLA action."""
    condition = _jobs("cla.yml")["cla"]["if"]
    assert "github.event_name == 'pull_request_target'" in condition
    assert "github.event.issue.pull_request" in condition
    assert "github.event.comment.body == 'recheck'" in condition
    assert (
        "github.event.comment.body == 'I have read the CLA Document and I hereby sign the CLA'"
        in condition
    )


def _effective_permissions(workflow: dict, job: dict) -> dict:
    """Give the permissions a job runs with: its own block, else the workflow's.

    Parameters
    ----------
    workflow
        The parsed workflow.
    job
        One of its jobs.

    Returns
    -------
    dict
        The job's ``permissions``, or the workflow's when the job has none.
    """
    return job.get("permissions", workflow.get("permissions", {}))


def test_only_the_job_that_uploads_the_sbom_can_write_to_the_release() -> None:
    """Whoever wants a release's bill of materials finds it on the release page.

    A separate job uploads the file with ``contents: write`` and runs no checkout and no
    third-party action but the artifact download; the job that builds the SBOM stays read-only.
    No other job, by its effective permissions, may write to the contents.
    """
    workflow = yaml.safe_load((WORKFLOWS / "release.yml").read_text())
    jobs = workflow["jobs"]
    assert workflow["permissions"] == {}
    assert jobs["sbom"]["permissions"] == {"contents": "read"}
    generate = next(s for s in jobs["sbom"]["steps"] if "anchore/sbom-action" in str(s.get("uses")))
    assert generate["with"]["output-file"] == "lakana-sbom.spdx.json"
    assert generate["with"]["artifact-name"] == "lakana-sbom.spdx.json"
    assert generate["with"]["upload-release-assets"] is False

    job = jobs["sbom-release"]
    assert job["permissions"] == {"contents": "write"}
    assert set(job["needs"]) == {"release-please", "approve", "sbom", "openapi"}
    assert job["if"] == jobs["sbom"]["if"]
    uses = [s["uses"].split("@")[0] for s in job["steps"] if "uses" in s]
    assert set(uses) == {"actions/download-artifact"}
    downloaded = {s["with"]["name"] for s in job["steps"] if "uses" in s}
    assert downloaded == {"lakana-sbom.spdx.json", "lakana-openapi.json"}
    upload = next(s for s in job["steps"] if "gh release upload" in s.get("run", ""))
    assert upload["env"]["TAG"] == "${{ needs.release-please.outputs.tag }}"
    (command,) = _commands(upload)
    assert '"$TAG"' in command
    assert "sbom/lakana-sbom.spdx.json" in command
    assert "openapi/lakana-openapi.json" in command
    assert "--clobber" in command
    assert "--repo" in command

    for name, other in jobs.items():
        if name not in {"sbom-release", "provenance-release"}:
            assert _effective_permissions(workflow, other).get("contents") != "write", name


def test_each_release_carries_the_openapi_document_with_its_version(tmp_path: Path) -> None:
    """The committed snapshot, with the release's version in ``info.version`` and nothing else.

    The job is read-only and checks out the tag; ``sbom-release`` (above) attaches the file.
    Its command is run here on the real snapshot, with the tag the workflow would pass.
    """
    jobs = _jobs("release.yml")
    job = jobs["openapi"]
    assert job["permissions"] == {"contents": "read"}
    assert set(job["needs"]) == {"release-please", "approve"}
    assert job["if"] == jobs["sbom"]["if"]
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["ref"] == "${{ needs.release-please.outputs.tag }}"
    assert checkout["with"]["persist-credentials"] is False
    stamp = next(s for s in job["steps"] if "jq" in s.get("run", ""))
    assert stamp["env"]["TAG"] == "${{ needs.release-please.outputs.tag }}"
    upload = next(s for s in job["steps"] if "upload-artifact" in str(s.get("uses")))
    assert upload["with"] == {"name": "lakana-openapi.json", "path": "lakana-openapi.json"}

    snapshot = tmp_path / "docs" / "site" / "reference" / "openapi.json"
    snapshot.parent.mkdir(parents=True)
    committed = (ROOT / "docs" / "site" / "reference" / "openapi.json").read_text()
    snapshot.write_text(committed)
    env = {**os.environ, "TAG": "v1.2.3"}
    subprocess.run(["bash", "-e", "-c", stamp["run"]], cwd=tmp_path, env=env, check=True)
    released = json.loads((tmp_path / "lakana-openapi.json").read_text())
    expected = json.loads(committed)
    expected["info"]["version"] = "1.2.3"
    assert released == expected


def _run_api_contract_step(tmp_path: Path, *, base: dict | None, head: dict, title: str) -> int:
    """Run the conventional check's REST API step on a scratch pull request merge commit.

    Parameters
    ----------
    tmp_path
        Directory for the scratch repository.
    base
        The base branch's OpenAPI snapshot; ``None`` for a base branch without one.
    head
        The pull request's snapshot.
    title
        The pull request title.

    Returns
    -------
    int
        The step's exit code.
    """
    (step,) = [
        s
        for s in _jobs("conventional.yml")["conventional"]["steps"]
        if "openapi_diff" in s.get("run", "")
    ]
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "openapi_diff.py").write_text(
        (ROOT / "scripts" / "openapi_diff.py").read_text()
    )
    snapshot = repo / step["env"]["SNAPSHOT"]
    snapshot.parent.mkdir(parents=True)
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "RUNNER_TEMP": str(runner_temp)}
    env |= {"PR_TITLE": title, "SNAPSHOT": step["env"]["SNAPSHOT"]}

    def git(*args: str) -> None:
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.org"]
        subprocess.run(
            ["git", *identity, *args], cwd=repo, env=env, check=True, capture_output=True
        )

    git("init", "-q", "-b", "main")
    if base is not None:
        snapshot.write_text(json.dumps(base))
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "pr")
    snapshot.write_text(json.dumps(head))
    git("add", "-A")
    git("commit", "-q", "-m", "pr")
    git("checkout", "-q", "main")
    (repo / "unrelated").write_text("x")
    git("add", "-A")
    git("commit", "-q", "-m", "later on main")
    git("merge", "-q", "--no-ff", "-m", "merge", "pr")
    return subprocess.run(["bash", "-e", "-c", step["run"]], cwd=repo, env=env).returncode


_ROUTE = {"/api/v1/x": {"get": {"responses": {"200": {"description": "OK"}}}}}
_BEFORE = {"openapi": "3.1.0", "paths": _ROUTE}
_AFTER = {"openapi": "3.1.0", "paths": {}}


def test_the_conventional_check_reads_the_title_and_the_merge_commit_s_base() -> None:
    """The step gets the title from the event and two commits of history."""
    job = _jobs("conventional.yml")["conventional"]
    assert job["steps"][0]["with"]["fetch-depth"] == 2
    (step,) = [s for s in job["steps"] if "openapi_diff" in s.get("run", "")]
    assert step["env"]["PR_TITLE"] == "${{ github.event.pull_request.title }}"
    assert step["env"]["SNAPSHOT"] == "docs/site/reference/openapi.json"
    assert "--require-breaking-title" in " ".join(_commands(step))


def test_a_breaking_api_change_without_a_breaking_title_fails_the_check(tmp_path: Path) -> None:
    """Removing a route needs ``!``."""
    assert _run_api_contract_step(tmp_path, base=_BEFORE, head=_AFTER, title="feat: x") == 1


def test_a_breaking_api_change_with_a_breaking_title_passes_the_check(tmp_path: Path) -> None:
    """Marked breaking, the change is deliberate; the footer check is the step before."""
    assert _run_api_contract_step(tmp_path, base=_BEFORE, head=_AFTER, title="feat!: x") == 0


def test_an_additive_api_change_passes_the_check(tmp_path: Path) -> None:
    """Adding a route only updates the snapshot."""
    assert _run_api_contract_step(tmp_path, base=_AFTER, head=_BEFORE, title="feat: x") == 0


def test_a_base_branch_without_a_snapshot_passes_the_check(tmp_path: Path) -> None:
    """The pull request that adds the snapshot has nothing to compare with."""
    assert _run_api_contract_step(tmp_path, base=None, head=_AFTER, title="docs: x") == 0


def test_the_scorecard_workflow_is_read_only_and_publishes_its_results() -> None:
    """It runs on main, weekly and on a branch-protection change, with the job's own scopes."""
    workflow = yaml.safe_load((WORKFLOWS / "scorecard.yml").read_text())
    assert set(workflow[True]) == {"push", "schedule", "branch_protection_rule"}
    assert workflow[True]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == "read-all"
    assert "env" not in workflow
    assert "defaults" not in workflow
    (job,) = workflow["jobs"].values()
    assert job["permissions"] == {"security-events": "write", "id-token": "write"}
    assert job["runs-on"] == "ubuntu-latest"  # published results need a GitHub-hosted job
    steps = job["steps"]
    scorecard = next(s for s in steps if s["uses"].startswith("ossf/scorecard-action@"))
    assert scorecard["with"]["publish_results"] is True
    assert scorecard["with"]["results_format"] == "sarif"
    upload = next(s for s in steps if s["uses"].startswith("github/codeql-action/upload-sarif@"))
    assert upload["with"]["sarif_file"] == scorecard["with"]["results_file"]
    checkout = next(s for s in steps if s["uses"].startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False


def test_no_workflow_grants_write_permissions_at_the_top_level() -> None:
    """A workflow-level write scope reaches every job; each job asks for its own instead.

    OpenSSF Scorecard's Token-Permissions check scores this. The top level is read-only, empty
    or absent (the repository's default is read-only).
    """
    offenders = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        top = yaml.safe_load(path.read_text()).get("permissions", {})
        if top == "write-all" or (isinstance(top, dict) and "write" in top.values()):
            offenders.append(path.name)
    assert not offenders, f"move the write permissions into the jobs that need them: {offenders}"


def test_the_cla_and_deep_security_workflows_write_only_in_the_job_that_needs_it() -> None:
    """The CLA action writes statuses and comments; the SARIF upload writes security events."""
    cla = yaml.safe_load((WORKFLOWS / "cla.yml").read_text())
    assert cla["permissions"] == {"contents": "read"}
    assert cla["jobs"]["cla"]["permissions"] == {
        "actions": "write",
        "contents": "read",
        "pull-requests": "write",
        "statuses": "write",
    }
    deep = yaml.safe_load((WORKFLOWS / "security-deep.yml").read_text())
    assert deep["permissions"] == {"contents": "read"}
    for name, job in deep["jobs"].items():
        writes = _effective_permissions(deep, job).get("security-events") == "write"
        assert writes == (name == "semgrep-upload"), name


def test_the_semgrep_scan_holds_no_write_scope_and_the_upload_runs_nothing_else() -> None:
    """The scanner is a pinned third-party image over the checkout; the upload is its own job."""
    jobs = _jobs("security-deep.yml")
    scan = jobs["semgrep"]
    assert scan["permissions"] == {"contents": "read"}
    assert re.search(r"@sha256:[0-9a-f]{64}$", scan["container"]["image"])
    checkout = next(s for s in scan["steps"] if "actions/checkout" in str(s.get("uses")))
    assert checkout["with"]["persist-credentials"] is False
    assert all("upload-sarif" not in str(s.get("uses")) for s in scan["steps"])
    artifact = next(s for s in scan["steps"] if "upload-artifact" in str(s.get("uses")))
    assert artifact["with"]["name"] == "semgrep-sarif"

    upload = jobs["semgrep-upload"]
    assert upload["needs"] == "semgrep"
    assert upload["permissions"] == {"security-events": "write"}
    uses = [s["uses"].split("@")[0] for s in upload["steps"]]
    assert uses == ["actions/download-artifact", "github/codeql-action/upload-sarif"]
    sarif = upload["steps"][1]["with"]
    assert sarif["sarif_file"] == "sarif/semgrep.sarif"
    assert sarif["category"] == "semgrep"


def test_codeql_analyses_python_on_pull_requests_main_and_weekly() -> None:
    """The CodeQL workflow is read-only except for the job that uploads to code scanning."""
    workflow = yaml.safe_load((WORKFLOWS / "codeql.yml").read_text())
    triggers = workflow[True]
    assert set(triggers) == {"pull_request", "push", "schedule"}
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["schedule"]
    assert workflow["permissions"] == {"contents": "read"}
    (job,) = workflow["jobs"].values()
    assert job["permissions"] == {
        "contents": "read",
        "security-events": "write",
    }
    assert job["runs-on"] == "ubuntu-latest"  # a GitHub-hosted job, like Scorecard's
    uses = {s["uses"].split("@")[0]: s for s in job["steps"] if "uses" in s}
    assert uses["github/codeql-action/init"]["with"]["languages"] == "python"
    assert "github/codeql-action/analyze" in uses
    assert uses["github/codeql-action/analyze"]["with"]["category"] == "/language:python"


def test_every_published_artefact_gets_a_build_provenance_attestation() -> None:
    """Wheels, sdists and the image carry an attestation that `gh attestation verify` checks.

    The attestations need ``id-token`` and ``attestations`` write scopes, held by the jobs that
    make them and by none other; the workflow stays at ``{}``.
    """
    workflow = yaml.safe_load((WORKFLOWS / "release.yml").read_text())
    jobs = workflow["jobs"]
    assert workflow["permissions"] == {}

    def attest(job: dict) -> list[dict]:
        return [s for s in job["steps"] if "attest-build-provenance" in str(s.get("uses"))]

    attesting = {name for name, job in jobs.items() if attest(job)}
    assert attesting == {"attest-dist", "attest-image", "attest-task-images"}
    for name in attesting:
        perms = jobs[name]["permissions"]
        assert perms["id-token"] == "write", name
        assert perms["attestations"] == "write", name
        assert jobs[name]["timeout-minutes"] <= 10, name
    assert jobs["attest-dist"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    # The image is signed by a job that runs no other third-party action: `publish-image`
    # runs several, and any step of a job with `id-token: write` could ask for a certificate.
    assert jobs["attest-image"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    assert jobs["attest-image"]["needs"] == "publish-image"
    assert len(jobs["attest-image"]["steps"]) == 1
    assert jobs["publish-image"]["outputs"]["digest"] == "${{ steps.build.outputs.digest }}"
    assert jobs["publish-image"]["permissions"] == {"contents": "read", "packages": "write"}
    (wheels,) = attest(jobs["attest-dist"])
    assert wheels["with"]["subject-path"].split() == ["dist/*.whl", "dist/*.tar.gz"]
    assert set(jobs["attest-dist"]["needs"]) == {"build", "approve"}
    # Scorecard looks for a signature among the release's assets: the bundle goes there from a
    # job that only downloads it, like the SBOM's.
    release = jobs["provenance-release"]
    assert release["permissions"] == {"contents": "write"}
    assert set(release["needs"]) == {"release-please", "attest-dist"}
    assert [s["uses"].split("@")[0] for s in release["steps"] if "uses" in s] == [
        "actions/download-artifact"
    ]
    assert any(".intoto.jsonl" in c for s in release["steps"] for c in _commands(s))
    (image,) = attest(jobs["attest-image"])
    assert image["with"]["push-to-registry"] is False
    assert image["with"]["subject-name"] == "ghcr.io/lakana-labs/lakana"
    assert image["with"]["subject-digest"] == "${{ needs.publish-image.outputs.digest }}"
    for name, job in jobs.items():
        if name not in attesting:
            perms = _effective_permissions(workflow, job)
            assert perms.get("attestations") != "write", name
            # the three PyPI publishes and the Pages deployment (its own test) use OIDC
            if name not in {"publish-core", "publish-runtime", "publish-sdk", "pages-deploy"}:
                assert perms.get("id-token") != "write", name


def test_the_macos_job_runs_the_unit_suite_and_the_getting_started_example() -> None:
    """`test (macos)`: GitHub's runner, Python 3.14, no integration tests, no coverage, 2 workers.

    macOS users run local mode, so the job runs what they use: the unit suite, and the tutorial's
    example (which ends with ``lakana server reset --local``). It is pinned to GitHub's runner,
    never the ``CI_RUNNER`` switch, and it is not a required check yet. Since the suite passes on
    macOS (#153) a failure fails the run like any other job's.
    """
    job = _jobs("ci.yml")[MACOS_JOB]
    assert job["name"] == "unit (macos, ${{ matrix.shard }}/2)"
    assert job["strategy"]["matrix"]["shard"] == [1, 2]
    assert "continue-on-error" not in job
    assert job["runs-on"] == "macos-latest"
    assert "CI_RUNNER" not in str(job)
    assert job["timeout-minutes"] <= 45
    commands = [c for step in job["steps"] for c in _commands(step)]
    pytest_command = next(c for c in commands if c.startswith("uv run pytest"))
    assert pytest_command.startswith("uv run pytest packages ")  # scripts/ is Linux tooling
    assert '-m "not integration"' in pytest_command
    assert "-n 2" in pytest_command
    assert "--cov" not in pytest_command
    setup = next(s for s in job["steps"] if "setup-uv" in str(s.get("uses")))
    assert setup["with"]["python-version"] == "3.14"
    assert any("run_docs_examples.sh getting-started" in c for c in commands)
    example = next(s for s in job["steps"] if "getting-started" in s.get("run", ""))
    assert example["if"].endswith("&& matrix.shard == 2")
    assert (
        (ROOT / "docs/examples/getting-started/run.sh")
        .read_text()
        .count("lakana server reset --local")
    )
    assert (
        next(s for s in job["steps"] if "checkout" in str(s.get("uses")))["with"]["fetch-depth"]
        == 0
    )
    assert unlocked_environment_commands(job) == []


def test_the_macos_job_waits_for_a_pull_request_to_be_ready_and_skips_documentation() -> None:
    """Like the other heavy jobs it skips a draft and a documentation-only change."""
    terms = _terms(_jobs("ci.yml")[MACOS_JOB]["if"])
    assert "needs.changes.outputs.docs_only != 'true'" in terms
    assert "needs.changes.outputs.draft != 'true'" in terms
    assert not any("already_tested" in term for term in terms)  # `main` always runs it


def test_the_macos_job_runs_for_a_pull_request_only_when_packages_change() -> None:
    """The scope step lets a push through and a pull request only if it touches `packages/`."""
    job = _jobs("ci.yml")[MACOS_JOB]
    scope = next(s for s in job["steps"] if s.get("id") == "scope")
    assert "packages/" in scope["run"]
    guarded = [
        s for s in job["steps"] if s.get("id") != "scope" and "checkout" not in str(s.get("uses"))
    ]
    assert guarded
    for step in guarded:
        assert step["if"].startswith("steps.scope.outputs.run == 'true'"), step


def test_the_macos_check_collects_both_shards() -> None:
    """``test (macos)`` is the check the maintainer will make required (#148)."""
    job = _jobs("ci.yml")["test-macos"]
    assert job["name"] == "test (macos)"
    assert job["needs"] == ["changes", "unit-macos"]
    assert job["if"] == _jobs("ci.yml")[MACOS_JOB]["if"]
    (step,) = job["steps"]
    assert step["env"] == {"RESULT": "${{ needs.unit-macos.result }}"}
    assert '[ "$RESULT" != "success" ]' in step["run"]


def test_the_five_minute_script_installs_uv_by_hash_and_dependabot_keeps_it_current() -> None:
    """The tool `uv` is hash-pinned; the product install stays unpinned on purpose."""
    script = (ROOT / "scripts" / "five_minute_test.sh").read_text()
    assert "--require-hashes -r" in script
    assert "requirements-uv.txt" in script
    requirements = (ROOT / "scripts" / "requirements-uv.txt").read_text()
    assert re.search(r"^uv==\d+\.\d+\.\d+ \\$", requirements, re.MULTILINE)
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}", requirements)) >= 2
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert [u["directory"] for u in config["updates"] if u["package-ecosystem"] == "pip"] == [
        "/scripts"
    ]


def _canary() -> dict:
    """Load the dependency canary workflow.

    Returns
    -------
    dict
        The parsed ``canary.yml``.
    """
    return yaml.safe_load((WORKFLOWS / "canary.yml").read_text())


def test_the_canary_runs_every_day_and_on_demand() -> None:
    """A breaking upstream release is seen within a day: a daily schedule, and a manual run."""
    triggers = _canary()[True]
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    (entry,) = triggers["schedule"]
    minute, hour, day, month, weekday = entry["cron"].split()
    assert (day, month, weekday) == ("*", "*", "*"), "daily means every day"
    assert minute.isdigit() and hour.isdigit()


def test_the_canary_has_the_least_privilege() -> None:
    """No token write scope anywhere: a failure is a failed run, not an issue the job writes."""
    workflow = _canary()
    assert workflow["permissions"] == {}
    (job,) = workflow["jobs"].values()
    assert job["permissions"] == {"contents": "read"}
    assert isinstance(job["timeout-minutes"], int)
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False


def test_the_canary_runs_on_github_s_runner() -> None:
    """A scheduled job has no pull request to judge it, so the `CI_RUNNER` switch does not apply."""
    (job,) = _canary()["jobs"].values()
    assert job["runs-on"] == "ubuntu-latest"


def test_the_canary_installs_its_own_build_with_unpinned_dependencies() -> None:
    """The five-minute script runs against wheels built from `main`; third parties float.

    Only `lakana` is pinned (to the build's own version, with `--find-links`), never with
    `--no-index` or a constraints file: an unpinned install is the point of the job.
    """
    (job,) = _canary()["jobs"].values()
    commands = "\n".join(c for step in job["steps"] for c in _commands(step))
    assert "uv build --all-packages" in commands
    assert "five_minute_test.sh --find-links /dist" in commands
    assert "lakana[server]==$version" in commands
    assert "--no-index" not in commands
    assert "--constraint" not in commands and " -c " not in commands
    assert "uv.lock" not in commands
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0  # the version comes from git tags


def test_the_canary_shows_the_versions_it_resolved_even_when_it_fails() -> None:
    """A failure names the version that broke it: the summary is written even on a failure."""
    (job,) = _canary()["jobs"].values()
    script = (ROOT / "scripts" / "five_minute_test.sh").read_text()
    assert "pip freeze" in script
    summary = [s for s in job["steps"] if '>> "$GITHUB_STEP_SUMMARY"' in s.get("run", "")]
    assert summary and all(s.get("if") == "${{ always() }}" for s in summary)


def _all_commands(job: dict) -> list[str]:
    """Collect every command of every step of a job.

    Parameters
    ----------
    job
        A workflow job.

    Returns
    -------
    list of str
        The commands, in order.
    """
    return [command for step in job["steps"] for command in _commands(step)]


def test_each_release_publishes_the_documentation_site_on_github_pages() -> None:
    """The site at https://lakana-labs.github.io/lakana/ is built from the release tag.

    One job builds with the same commands as CI's `docs` job, from the tag release-please
    created, and only for a release; no manual trigger can deploy something else.
    """
    workflow = yaml.safe_load((WORKFLOWS / "release.yml").read_text())
    assert set(workflow[True]) == {"push"}  # no manual trigger: only a release deploys
    job = workflow["jobs"]["pages"]
    assert set(job["needs"]) == {"release-please", "approve"}
    assert job["if"] == workflow["jobs"]["sbom"]["if"]
    assert job["runs-on"] == "ubuntu-latest"
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ needs.release-please.outputs.tag }}"
    assert checkout["with"]["persist-credentials"] is False
    commands = _all_commands(job)
    assert "uv sync --locked --all-packages --group docs" in commands
    assert "uv run python scripts/docs_blocks.py" in commands
    assert "uv run --group docs zensical build --strict" in commands
    assert "uv run python scripts/check_built_site.py site-build" in commands
    assert commands.index("uv run --group docs zensical build --strict") < commands.index(
        "uv run python scripts/check_built_site.py site-build"
    )
    upload = next(
        s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-pages-artifact@")
    )
    assert upload["with"]["path"] == "site-build"


def test_the_site_is_deployed_by_a_job_that_runs_nothing_else() -> None:
    """The deploy job holds `pages: write` and `id-token: write` and builds nothing.

    The build job, which runs the project's code, is read-only; every other job of the
    workflow lacks the two scopes the deployment needs.
    """
    workflow = yaml.safe_load((WORKFLOWS / "release.yml").read_text())
    jobs = workflow["jobs"]
    assert workflow["permissions"] == {}
    assert jobs["pages"]["permissions"] == {"contents": "read"}
    deploy = jobs["pages-deploy"]
    assert deploy["needs"] == "pages"
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["runs-on"] == "ubuntu-latest"
    assert deploy["environment"] == {
        "name": "github-pages",
        "url": "${{ steps.deployment.outputs.page_url }}",
    }
    (step,) = deploy["steps"]
    assert step["uses"].startswith("actions/deploy-pages@")
    assert step["id"] == "deployment"
    for name, other in jobs.items():
        if name != "pages-deploy":
            assert "pages" not in _effective_permissions(workflow, other), name


def test_the_site_is_built_for_the_address_it_is_served_from() -> None:
    """GitHub Pages serves the project under `/lakana/`, so the build needs that `site_url`."""
    config = (ROOT / "mkdocs.yml").read_text()
    assert re.search(r"^site_url: https://lakana-labs\.github\.io/lakana/$", config, re.MULTILINE)


def test_a_release_publishes_one_task_image_per_supported_python() -> None:
    """One image per supported Python, `lakana-task:<version>-py3.X`, built with the release."""
    job = _jobs("release.yml")["publish-task-images"]

    assert job["strategy"]["matrix"]["python"] == ["3.11", "3.12", "3.13", "3.14"]
    build = next(step for step in job["steps"] if "build-push-action" in step.get("uses", ""))
    assert build["with"]["file"] == "Dockerfile.task"
    assert build["with"]["tags"] == (
        "ghcr.io/lakana-labs/lakana-task:"
        "${{ steps.version.outputs.version }}-py${{ matrix.python }}"
    )
    assert "PYTHON=${{ matrix.python }}" in build["with"]["build-args"]
    assert "publish-task-images" in _jobs("release.yml")["compose-smoke-release"]["needs"]


def test_the_task_images_wait_for_the_approval_and_are_attested() -> None:
    """The gate is explicit, and each image is signed by digest like the main image."""
    jobs = _jobs("release.yml")
    publish = jobs["publish-task-images"]

    assert "approve" in publish["needs"]
    attest = jobs["attest-task-images"]
    assert "publish-task-images" in attest["needs"]
    assert attest["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    step = next(s for s in attest["steps"] if "attest-build-provenance" in s.get("uses", ""))
    assert step["with"]["subject-name"] == "ghcr.io/lakana-labs/lakana-task"
    assert step["with"]["subject-digest"] == "${{ steps.digest.outputs.digest }}"
    assert attest["strategy"]["matrix"]["python"] == ["3.11", "3.12", "3.13", "3.14"]


def test_the_attested_digest_is_the_one_the_push_step_reported() -> None:
    """No read-back from a mutable tag: the push step's digest travels in an artifact."""
    jobs = _jobs("release.yml")
    publish, attest = jobs["publish-task-images"], jobs["attest-task-images"]
    build = next(s for s in publish["steps"] if "build-push-action" in s.get("uses", ""))
    assert build["id"]
    upload = next(s for s in publish["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["with"]["name"] == "task-digest-${{ matrix.python }}"
    writer = next(
        s for s in publish["steps"] if f"steps.{build['id']}.outputs.digest" in json.dumps(s)
    )
    assert "digest" in writer["run"] or "digest" in json.dumps(writer.get("env", {}))
    download = next(s for s in attest["steps"] if "download-artifact" in s.get("uses", ""))
    assert download["with"]["name"] == "task-digest-${{ matrix.python }}"
    text = json.dumps(attest)
    assert "imagetools" not in text
    assert "docker login" not in text
    assert attest["permissions"].get("packages") is None


def test_the_docker_isolation_tests_have_a_step_of_their_own_that_needs_docker() -> None:
    """The exit criterion of task isolation runs on the integration job's Docker Engine.

    Its own step, so a Docker failure is named as such; `LAKANA_TEST_DOCKER=1` makes a missing
    Docker a failure instead of a skip, and the first step leaves these tests out.
    """
    jobs = _jobs("ci.yml")
    first = next(
        s for s in jobs["integration-services"]["steps"] if s.get("name") == "Integration tests"
    )
    docker = next(
        s for s in jobs["integration-docker"]["steps"] if s.get("name") == "Docker isolation tests"
    )

    assert '-m "integration and not docker"' in first["run"]
    assert jobs["integration-docker"]["name"] == "integration (docker)"
    assert docker["env"] == {"LAKANA_TEST_DOCKER": "1"}
    assert docker["run"] == (
        'uv run pytest -m docker --basetemp "$RUNNER_TEMP/pytest-docker" '
        "packages/lakana-worker/tests"
    )


def test_every_integration_pytest_keeps_its_temp_files_off_the_runners_tmp() -> None:
    """Each pytest of the integration job puts its base temp directory under `$RUNNER_TEMP`.

    `/tmp` on GitHub's hosted runners can have a disk quota: the Python-asset tests copy the
    wheel directory into every project and attempt and build a venv per project, and the last
    venv build of the module failed with "Disk quota exceeded" there. `$RUNNER_TEMP` is on the
    work disk, where the wheel directory and uv's cache already live.
    """
    steps = [step for name in INTEGRATION_JOBS for step in _jobs("ci.yml")[name]["steps"]]
    pytests = [step for step in steps if "pytest" in step.get("run", "")]

    assert len(pytests) == 3
    for step in pytests:
        assert '--basetemp "$RUNNER_TEMP/pytest-' in step["run"], step["name"]
    assert len({step["run"].split("--basetemp ")[1].split()[0] for step in pytests}) == 3


INTEGRATION_JOBS = ("integration-services", "integration-docker", "integration-assets")


@pytest.mark.parametrize("name", INTEGRATION_JOBS)
def test_each_integration_job_has_a_timeout(name: str) -> None:
    """No test waits without a bound, and no integration job may hold a runner for hours."""
    assert _jobs("ci.yml")[name]["timeout-minutes"] == 30


def test_the_required_integration_check_passes_only_when_every_integration_job_did() -> None:
    """``integration`` is a required check: it waits for the three jobs and checks each result."""
    job = _jobs("ci.yml")["integration"]
    assert job["needs"] == ["changes", *INTEGRATION_JOBS]
    assert job["if"] == _jobs("ci.yml")["integration-services"]["if"]
    for name in INTEGRATION_JOBS:
        assert job["if"] == _jobs("ci.yml")[name]["if"], name
    (step,) = job["steps"]
    assert set(step["env"].values()) == {f"${{{{ needs.{n}.result }}}}" for n in INTEGRATION_JOBS}
    assert 'exit "$failed"' in step["run"]
    assert "uses" not in step


def test_the_services_tests_run_in_two_shards_and_the_python_assets_build_their_index() -> None:
    """The longest integration step is split; the wheel index is built only where it is used."""
    jobs = _jobs("ci.yml")
    services = jobs["integration-services"]
    assert services["strategy"]["matrix"]["shard"] == [1, 2]
    assert services["name"] == "integration (services, ${{ matrix.shard }}/2)"
    step = next(s for s in services["steps"] if s.get("name") == "Integration tests")
    assert step["env"] == {"LAKANA_TEST_SHARD": "${{ matrix.shard }}/2"}
    assert "--ignore=packages/lakana-server/tests/test_python_assets_integration.py" in step["run"]
    assets = jobs["integration-assets"]
    assert assets["name"] == "integration (python assets)"
    names = [s.get("name") for s in assets["steps"]]
    assert names.index("Build the wheel index for the Python-asset tests") < names.index(
        "Python-asset integration tests"
    )
    for name, job in jobs.items():
        if name != "integration-assets":
            assert "build_wheelhouse.sh" not in str(job), name


def test_the_two_dockerfiles_pin_the_same_python_314_base_image() -> None:
    """The release image and the 3.14 task image must not drift to different digests."""
    pin = re.compile(r"^FROM (python:3\.14-slim@sha256:[0-9a-f]{64})", re.MULTILINE)
    main = set(pin.findall((ROOT / "Dockerfile").read_text()))
    task = set(pin.findall((ROOT / "Dockerfile.task").read_text()))
    assert len(main) == 1
    assert main == task


def _check_names() -> list[str]:
    """Name every check ``ci.yml`` reports, its matrix expanded.

    Returns
    -------
    list of str
        The names as the API lists them: a job's ``name`` with the matrix values put in, or,
        without a name, its key followed by the matrix values in parentheses, as Actions does.
    """
    names = []
    for key, job in _jobs("ci.yml").items():
        matrix = job.get("strategy", {}).get("matrix", {})
        versions = ["3.11", "3.14"] if "python-version" in matrix else [None]
        for version in versions:
            for shard in matrix.get("shard", [None]):
                values = ", ".join(str(v) for v in (version, shard) if v is not None)
                name = job.get("name", f"{key} ({values})" if values else key)
                name = name.replace("${{ matrix.python-version }}", str(version))
                names.append(name.replace("${{ matrix.shard }}", str(shard)))
    return names


def test_every_ci_job_has_a_time_budget() -> None:
    """``scripts/time_budgets.py jobs`` fails a job without one; this finds it before CI does."""
    budgets = read_budgets(ROOT / BUDGETS)
    names = [name for name in _check_names() if name != "time budgets"]
    assert "unit (3.14, 3/3)" in names
    assert "test (3.11)" in names
    assert "integration (services, 2/2)" in names
    errors, _ = check_jobs([JobTime(name, 0.0) for name in names], budgets)
    assert errors == []


def test_the_budget_job_waits_for_every_job_and_reads_its_own_run() -> None:
    """It runs last, also after a failure, and reads the jobs of this attempt of this run."""
    jobs = _jobs("ci.yml")
    job = jobs["budgets"]
    assert job["name"] == "time budgets"
    assert job["if"] == "${{ !cancelled() }}"
    waited = set(job["needs"])
    for name in jobs:
        if name in ("budgets", "changes"):
            continue
        # A job is waited for directly or through a job that waits for it.
        assert name in waited or any(
            name in jobs[w].get("needs", [])
            for w in waited
            if isinstance(jobs[w].get("needs"), list)
        ), name
    step = next(s for s in job["steps"] if s.get("name") == "Job budgets")
    assert "attempts/${GITHUB_RUN_ATTEMPT}/jobs" in step["run"]
    assert "--paginate" in step["run"]
    assert "python3 scripts/time_budgets.py jobs" in step["run"]
    assert "pipefail" in step["shell"]
    assert step["env"] == {"GH_TOKEN": "${{ github.token }}"}


def test_the_quarantine_job_runs_only_quarantined_tests_and_never_fails_the_run() -> None:
    """Quarantined tests are reported, not enforced; every required job skips them."""
    job = _jobs("ci.yml")["quarantine"]
    assert job["name"] == "quarantined tests"
    step = next(s for s in job["steps"] if s.get("name", "").startswith("Quarantined tests"))
    assert step["env"] == {"LAKANA_TEST_QUARANTINED": "1"}
    assert 'uv run pytest -m ""' in step["run"]
    assert "|| status=$?" in step["run"]
    assert "GITHUB_STEP_SUMMARY" in step["run"]
    for name, other in _jobs("ci.yml").items():
        if name != "quarantine":
            assert "LAKANA_TEST_QUARANTINED" not in str(other), name
