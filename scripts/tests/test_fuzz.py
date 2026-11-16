"""Tests of the fuzz harnesses (fuzz/), their seeds, their build and their workflow."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from fuzz import seed_corpus, targets

from lakana_core.access.file import AccessFileError
from lakana_core.errors import ValidationError
from lakana_core.query_rewrite import (
    _fail_closed,
    check_blacklist,
    extract_table_refs,
    rewrite_query,
)

ROOT = Path(__file__).resolve().parents[2]
FUZZ = ROOT / "fuzz"
WORKFLOW = ROOT / ".github" / "workflows" / "fuzz.yml"

_CFLITE = "google/clusterfuzzlite/actions/"


@pytest.mark.parametrize("name", sorted(targets.TARGETS))
def test_every_seed_is_accepted_or_refused_as_expected(name: str) -> None:
    """A seed that crashes its target would fail every fuzz run before it starts."""
    target = targets.TARGETS[name]
    for data in seed_corpus.seeds(name).values():
        target(data)


def test_each_target_has_a_harness_that_imports_atheris_and_seeds() -> None:
    """One ``<name>_fuzzer.py`` per target, handing exactly that target to Atheris."""
    harnesses = sorted(path.name for path in FUZZ.glob("*_fuzzer.py"))
    assert harnesses == sorted(f"{name}_fuzzer.py" for name in targets.TARGETS)
    for name in targets.TARGETS:
        tree = ast.parse((FUZZ / f"{name}_fuzzer.py").read_text())
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "atheris" in imported, name
        from_targets = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "fuzz.targets"
            for alias in node.names
        ]
        assert from_targets == [name]
        assert (FUZZ / "seeds" / f"{name}.json").is_file()


def test_the_seeds_hold_no_duplicate(tmp_path: Path) -> None:
    """Each seed is one file, named by its hash, in the archive ClusterFuzzLite reads."""
    for name in targets.TARGETS:
        texts = json.loads((FUZZ / "seeds" / f"{name}.json").read_text())
        assert len(texts) == len(set(texts)), name
        archive = seed_corpus.write(name, tmp_path, as_zip=True)
        assert archive.name == f"{name}_fuzzer_seed_corpus.zip"
        with zipfile.ZipFile(archive) as opened:
            assert len(opened.namelist()) == len(texts)
            assert all(re.fullmatch(r"[0-9a-f]{40}", n) for n in opened.namelist())
        assert len(list(seed_corpus.write(name, tmp_path, as_zip=False).iterdir())) == len(texts)


def test_the_guard_targets_call_the_functions_under_the_fail_closed_wrapper() -> None:
    """The wrapper turns every unexpected exception into a refusal, hiding it from the fuzzer."""
    assert targets._check_blacklist is check_blacklist.__wrapped__
    assert targets._extract_table_refs is extract_table_refs.__wrapped__
    assert targets._rewrite_query is rewrite_query.__wrapped__


@pytest.mark.parametrize(
    "error",
    [ValidationError("no"), MemoryError(), RecursionError(), KeyError("x"), AttributeError("x")],
)
def test_the_guard_s_refusals_are_exactly_what_its_wrapper_lets_through(
    error: BaseException,
) -> None:
    """`GUARD_REFUSALS` is what `_fail_closed` passes on unchanged; it refuses all else."""

    def fails() -> None:
        """Raise the error.

        Raises
        ------
        BaseException
            Always, `error`.
        """
        raise error

    with pytest.raises(BaseException) as raised:
        _fail_closed(fails)()
    passed_through = raised.value is error
    assert passed_through == isinstance(error, targets.GUARD_REFUSALS)


def _raising(error: BaseException) -> Callable[..., None]:
    """Make a stand-in parser that raises `error`.

    Parameters
    ----------
    error
        What the stand-in raises.

    Returns
    -------
    collections.abc.Callable
        The stand-in.
    """

    def parser(*_args: object, **_kwargs: object) -> None:
        """Raise the error.

        Parameters
        ----------
        *_args
            Ignored.
        **_kwargs
            Ignored.

        Raises
        ------
        BaseException
            Always, `error`.
        """
        raise error

    return parser


@pytest.mark.parametrize(
    ("name", "attribute", "refusal"),
    [
        ("query_guard", "_check_blacklist", ValidationError("refused")),
        ("query_guard", "_rewrite_query", ValidationError("refused")),
        ("project_files", "parse_front_matter", ValidationError("refused")),
        ("project_files", "parse_project_config", ValidationError("refused")),
        ("access_file", "parse_access_file", AccessFileError(["refused"])),
    ],
)
def test_a_refusal_passes_and_any_other_exception_is_a_crash(
    name: str, attribute: str, refusal: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a planted bug in a parser looks like to the harness: the exception escapes."""
    target = targets.TARGETS[name]
    monkeypatch.setattr(targets, attribute, _raising(refusal))
    target(b"SELECT a FROM b.c")
    monkeypatch.setattr(targets, attribute, _raising(IndexError("planted")))
    with pytest.raises(IndexError, match="planted"):
        target(b"SELECT a FROM b.c")


def test_text_that_is_not_utf_8_is_skipped_and_a_lone_surrogate_is_kept() -> None:
    assert targets.decode(b"\xff") is None
    assert targets.decode("\ud800".encode("utf-8", "surrogatepass")) == "\ud800"


def _workflow() -> dict:
    """Load the fuzzing workflow.

    Returns
    -------
    dict
        The parsed workflow.
    """
    return yaml.safe_load(WORKFLOW.read_text())


def _glob(pattern: str) -> re.Pattern[str]:
    """Translate a GitHub Actions path filter into a regular expression.

    Parameters
    ----------
    pattern
        The filter: ``**`` matches across directories, ``*`` within one.

    Returns
    -------
    re.Pattern
        The compiled expression.
    """
    parts = re.split(r"(\*\*|\*)", pattern)
    wild = {"**": ".*", "*": "[^/]*"}
    return re.compile("".join(wild.get(part, re.escape(part)) for part in parts))


def test_a_pull_request_that_changes_what_a_harness_loads_runs_the_fuzzers() -> None:
    """Every lakana-core module the targets load is under the workflow's path filter."""
    script = (
        "import sys, fuzz.targets; "
        "print('\\n'.join(sorted(m.__file__ for n, m in sys.modules.items() "
        "if n.split('.')[0] == 'lakana_core')))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    paths = [_glob(p) for p in _workflow()[True]["pull_request"]["paths"]]
    relative = [str(Path(file).resolve().relative_to(ROOT)) for file in loaded]
    assert any(r.endswith("query_rewrite.py") for r in relative)
    missing = [r for r in relative if not any(p.fullmatch(r) for p in paths)]
    assert missing == []
    for own in ("fuzz/targets.py", ".clusterfuzzlite/build.sh", "uv.lock"):
        assert any(p.fullmatch(own) for p in paths), own


def test_the_pull_request_run_is_short_and_the_nightly_run_keeps_the_corpus() -> None:
    workflow = _workflow()
    assert set(workflow[True]) == {"pull_request", "schedule", "workflow_dispatch"}
    jobs = workflow["jobs"]
    modes = {}
    for name, job in jobs.items():
        run = next(
            s for s in job["steps"] if s.get("uses", "").startswith(_CFLITE + "run_fuzzers@")
        )
        modes[name] = (run["with"]["mode"], run["with"]["fuzz-seconds"])
    assert modes == {
        "pull-request": ("code-change", 300),
        "nightly": ("batch", 3600),
        "prune": ("prune", 600),
    }
    assert jobs["pull-request"]["if"] == "${{ github.event_name == 'pull_request' }}"
    assert jobs["nightly"]["if"] == "${{ github.event_name != 'pull_request' }}"


@pytest.mark.parametrize("job", ["pull-request", "nightly"])
def test_a_crash_fails_the_run_and_uploads_its_input(job: str) -> None:
    steps = _workflow()["jobs"][job]["steps"]
    upload = steps[-1]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["if"] == "${{ failure() && steps.build.outcome == 'success' }}"
    assert upload["with"]["path"] == "./out/artifacts"
    assert steps[0]["id"] == "build"


def test_the_fuzzing_jobs_run_on_github_s_runner_with_read_scopes_only() -> None:
    """A pull request's harnesses are its own code, and the actions run Docker."""
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert job["runs-on"] == "ubuntu-latest", name
        assert job["permissions"] == {"contents": "read", "actions": "read"}, name
        assert "timeout-minutes" in job, name
        for step in job["steps"]:
            uses = step.get("uses", "")
            if uses.startswith(_CFLITE + "build_fuzzers@"):
                # The build clones a public repository: it needs no token.
                assert "github-token" not in step["with"], name


def test_the_builder_installs_lakana_core_s_dependencies_from_the_lock_with_hashes() -> None:
    build = (ROOT / ".clusterfuzzlite" / "build.sh").read_text()
    assert "uv export --frozen --no-dev --no-emit-workspace --package lakana-core" in build
    assert "pip3 install --no-deps --require-hashes" in build
    assert "for fuzzer in fuzz/*_fuzzer.py" in build
    assert "python3 fuzz/seed_corpus.py --zip" in build
    project = yaml.safe_load((ROOT / ".clusterfuzzlite" / "project.yaml").read_text())
    assert project["language"] == "python"
