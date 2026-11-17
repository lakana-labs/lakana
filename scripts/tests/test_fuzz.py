"""Tests of the fuzz harnesses (fuzz/), their seeds, their build and their workflow."""

from __future__ import annotations

import ast
import builtins
import inspect
import json
import os
import re
import subprocess
import sys
import textwrap
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from fuzz import seed_corpus, targets

from lakana_core import query_rewrite
from lakana_core.access.file import AccessFileError
from lakana_core.errors import ValidationError
from lakana_core.lineage import extract_lineage
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
    # The lineage has no wrapper: what it raises reaches the reader as it is.
    assert targets._extract_lineage is extract_lineage
    assert not hasattr(extract_lineage, "__wrapped__")


def _wrapper_passes() -> set[type[BaseException]]:
    """Read the exceptions `_fail_closed` re-raises unchanged from its source.

    Returns
    -------
    set of type
        The classes named in the handler whose body is a bare ``raise``.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(_fail_closed)))
    (handler,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Raise)
        and node.body[0].exc is None
    ]
    assert isinstance(handler.type, ast.Tuple)
    passes: set[type[BaseException]] = set()
    for element in handler.type.elts:
        if isinstance(element, ast.Starred):
            assert isinstance(element.value, ast.Name)
            passes.update(getattr(query_rewrite, element.value.id))
        else:
            assert isinstance(element, ast.Name)
            passes.add(getattr(query_rewrite, element.id, None) or getattr(builtins, element.id))
    return passes


def test_fail_closed_passes_is_the_wrapper_s_own_tuple() -> None:
    """The whole set, read from `_fail_closed`'s source: a new member there fails here."""
    assert _wrapper_passes() == set(targets.FAIL_CLOSED_PASSES)
    assert len(targets.FAIL_CLOSED_PASSES) == len(set(targets.FAIL_CLOSED_PASSES))


_PROBES: tuple[type[BaseException], ...] = (
    KeyError,
    AttributeError,
    ValueError,
    TimeoutError,
    KeyboardInterrupt,
)
"""Exceptions in neither set, among them one that is not an `Exception`."""


@pytest.mark.parametrize(
    "kind",
    sorted(
        {*targets.FAIL_CLOSED_PASSES, *targets.GUARD_REFUSALS, *_PROBES},
        key=lambda kind: kind.__qualname__,
    ),
)
def test_fail_closed_passes_on_exactly_its_tuple_and_what_is_not_an_exception(
    kind: type[BaseException],
) -> None:
    """Every member of both sets, and the probes, through the real wrapper."""
    error = kind("x")

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
    expected = isinstance(error, targets.FAIL_CLOSED_PASSES) or not isinstance(error, Exception)
    assert passed_through == expected


def test_the_fuzzer_s_refusals_are_narrower_than_the_wrapper_s() -> None:
    """A `MemoryError` is a crash; a `RecursionError` only a refusal when parsing raises it."""
    assert set(targets.GUARD_REFUSALS) < set(targets.FAIL_CLOSED_PASSES)
    for kind in (MemoryError, RecursionError):
        assert not issubclass(kind, targets.GUARD_REFUSALS)


_DEEP = b"SELECT " + b"(" * 1500 + b"1" + b")" * 1500
"""A query whose parse alone hits the parser's limit on nesting."""


@pytest.mark.parametrize("attribute", ["_check_blacklist", "_extract_lineage", "_rewrite_query"])
def test_a_recursion_error_is_a_refusal_only_when_parsing_alone_raises_it(
    attribute: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert targets.parse_alone_recurses(_DEEP.decode())
    assert not targets.parse_alone_recurses("SELECT a FROM b.c")
    monkeypatch.setattr(targets, attribute, _raising(RecursionError("walker")))
    targets.query_guard(_DEEP)
    with pytest.raises(RecursionError, match="walker"):
        targets.query_guard(b"SELECT a FROM b.c")


@pytest.mark.parametrize("attribute", ["_check_blacklist", "_extract_lineage", "_rewrite_query"])
def test_a_memory_error_is_a_crash(attribute: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(targets, attribute, _raising(MemoryError()))
    with pytest.raises(MemoryError):
        targets.query_guard(b"SELECT a FROM b.c")


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
        ("query_guard", "_extract_lineage", ValidationError("refused")),
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
    # After every seed through its target: a module a parser imports only when it runs is loaded.
    script = (
        "import sys\n"
        "from fuzz import seed_corpus, targets\n"
        "for name, target in targets.TARGETS.items():\n"
        "    for data in seed_corpus.seeds(name).values():\n"
        "        target(data)\n"
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


def _step(job: str, prefix: str) -> dict:
    """Find the one step of a fuzzing job that uses an action or runs a command.

    Parameters
    ----------
    job
        The job's key.
    prefix
        The start of the step's ``uses`` or ``run``.

    Returns
    -------
    dict
        The step.
    """
    steps = _workflow()["jobs"][job]["steps"]
    (step,) = [s for s in steps if str(s.get("uses", s.get("run", ""))).startswith(prefix)]
    return step


def test_the_pull_request_run_is_short_and_the_nightly_run_is_an_hour() -> None:
    workflow = _workflow()
    assert set(workflow[True]) == {"pull_request", "schedule", "workflow_dispatch"}
    assert set(workflow["jobs"]) == {"pull-request", "nightly"}
    run = _step("pull-request", _CFLITE + "run_fuzzers@")
    assert (run["with"]["mode"], run["with"]["fuzz-seconds"]) == ("code-change", 300)
    # Without it the action takes c++, and CIFuzz waits 30 s instead of 120 s for a crash to
    # reproduce before it calls the crash unreproducible and passes the run.
    assert run["with"]["language"] == "python"
    for job in ("pull-request", "nightly"):
        assert _step(job, _CFLITE + "build_fuzzers@")["with"]["language"] == "python"
    nightly = _step("nightly", ".clusterfuzzlite/nightly.sh")
    assert nightly["run"] == (
        '.clusterfuzzlite/nightly.sh build-out cifuzz-corpus out/artifacts "$FUZZ_SECONDS" '
        '"$RUNNER_IMAGE"'
    )
    fuzzers = len(list(FUZZ.glob("*_fuzzer.py")))
    assert int(nightly["env"]["FUZZ_SECONDS"]) * fuzzers == 3600
    assert re.fullmatch(
        r"gcr\.io/oss-fuzz-base/base-runner:latest@sha256:[0-9a-f]{64}",
        nightly["env"]["RUNNER_IMAGE"],
    )


def test_only_this_repository_s_pull_requests_and_main_are_fuzzed() -> None:
    """A fork's run would hold `ACTIONS_RUNTIME_TOKEN`; only `main` keeps the corpus."""
    jobs = _workflow()["jobs"]
    assert jobs["pull-request"]["if"] == (
        "${{ github.event_name == 'pull_request'"
        " && github.event.pull_request.head.repo.full_name == github.repository }}"
    )
    assert jobs["nightly"]["if"] == (
        "${{ github.event_name != 'pull_request' && github.ref == 'refs/heads/main' }}"
    )


_CACHE = "actions/cache/"


def test_the_corpus_is_kept_in_main_s_cache_and_a_pull_request_only_reads_it() -> None:
    """A cache written on main is readable by main and pull requests; no pull request writes one.

    Both jobs name the same path, which is part of the cache's version, and the directory
    ClusterFuzzLite's run_fuzzers fuzzes from (``cifuzz-corpus/<fuzzer>``).
    """
    key = "fuzz-corpus-${{ github.run_id }}-${{ github.run_attempt }}"
    for job in ("pull-request", "nightly"):
        restore = _step(job, _CACHE + "restore@")
        assert restore["with"] == {
            "path": "cifuzz-corpus",
            "key": key,
            "restore-keys": "fuzz-corpus-",
        }
    save = _step("nightly", _CACHE + "save@")
    assert save["with"] == {"path": "cifuzz-corpus", "key": key}
    assert save["if"] == "${{ !cancelled() && steps.decide.outputs.save == 'true' }}"
    decide = _step("nightly", 'if [ -n "$MATCHED" ]')
    assert decide["id"] == "decide"
    assert decide["env"]["MATCHED"] == "${{ steps.restore.outputs.cache-matched-key }}"
    assert _step("nightly", _CACHE + "restore@")["id"] == "restore"
    steps = _workflow()["jobs"]["nightly"]["steps"]
    order = [
        steps.index(_step("nightly", p))
        for p in (
            _CFLITE + "build_fuzzers@",
            _CACHE + "restore@",
            ".clusterfuzzlite/nightly.sh",
            _CACHE + "save@",
        )
    ]
    assert order == sorted(order)
    pull_request = _workflow()["jobs"]["pull-request"]["steps"]
    assert not any(str(s.get("uses", "")).startswith(_CACHE + "save@") for s in pull_request)
    assert not any(str(s.get("uses", "")).startswith("actions/cache@") for s in pull_request)
    assert pull_request.index(_step("pull-request", _CACHE + "restore@")) < pull_request.index(
        _step("pull-request", _CFLITE + "run_fuzzers@")
    )


@pytest.mark.parametrize("job", ["pull-request", "nightly"])
def test_a_crash_fails_the_run_and_uploads_its_input(job: str) -> None:
    upload = _step(job, "actions/upload-artifact@")
    assert _workflow()["jobs"][job]["steps"][-1] == upload
    assert upload["if"] == "${{ failure() && steps.build.outcome == 'success' }}"
    assert upload["with"]["path"] == "./out/artifacts"
    assert _step(job, _CFLITE + "build_fuzzers@")["id"] == "build"


def test_the_fuzzing_jobs_run_on_github_s_runner_with_read_scopes_only() -> None:
    """A pull request's harnesses are its own code, and the actions run Docker.

    No ClusterFuzzLite action gets a token, so none pages through the repository's artifacts
    looking for a corpus, which once spent the rate limit the other workflows share.
    """
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert job["runs-on"] == "ubuntu-latest", name
        assert "timeout-minutes" in job, name
        for step in job["steps"]:
            assert "github-token" not in step.get("with", {}), name
            if str(step.get("uses", "")).startswith(_CFLITE):
                assert "env" not in step, name
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False
    assert workflow["jobs"]["pull-request"]["permissions"] == {"contents": "read"}
    # The nightly job reads the cache list once, in the one step that has the token.
    nightly = workflow["jobs"]["nightly"]
    assert nightly["permissions"] == {"contents": "read", "actions": "read"}
    with_token = [s for s in nightly["steps"] if "token" in str(s.get("env", {})).lower()]
    assert [s.get("id") for s in with_token] == ["decide"]
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            assert "token" not in str(step.get("env", {})).lower() or step.get("id") == "decide"


def _decide(tmp_path: Path, matched: str, listing: str, exit_code: int = 0) -> str:
    """Run the nightly job's decision step with a fake ``gh``.

    Parameters
    ----------
    tmp_path
        A scratch directory.
    matched
        The restore step's ``cache-matched-key``.
    listing
        What ``gh cache list ... --jq length`` prints.
    exit_code
        What that ``gh`` exits with.

    Returns
    -------
    str
        What the step wrote to ``GITHUB_OUTPUT``, then its standard output.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/bash\nprintf '%s' '{listing}'\nexit {exit_code}\n")
    gh.chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    step = _step("nightly", 'if [ -n "$MATCHED" ]')
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "MATCHED": matched,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REPOSITORY": "lakana-labs/lakana",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return output.read_text() + result.stdout


@pytest.mark.parametrize(
    ("matched", "listing", "exit_code", "save"),
    [
        ("fuzz-corpus-1-1", "", 0, True),  # restored: keep the grown corpus
        ("", "", 0, True),  # main has none yet (gh prints nothing): the first
        ("", "0", 0, True),
        ("", "1", 0, False),  # one is kept but was not restored: keep it
        ("", "", 1, False),  # the list failed: do not replace what may be there
    ],
)
def test_the_nightly_job_saves_the_corpus_only_after_a_hit_or_as_the_first(
    matched: str, listing: str, exit_code: int, save: bool, tmp_path: Path
) -> None:
    """A miss after a corpus was kept must not make a seeds-only corpus the newest."""
    written = _decide(tmp_path, matched, listing, exit_code)
    assert f"save={str(save).lower()}" in written
    assert ("::warning::" in written) is not save


def _fake_docker(tmp_path: Path, fails: str) -> Path:
    """Write a ``docker`` that records its arguments and fails when they contain `fails`.

    Parameters
    ----------
    tmp_path
        Where to write it.
    fails
        The text whose presence among the arguments makes it exit 1; empty never fails.
        ``=<text>`` instead fails the call whose last argument is exactly ``<text>``.

    Returns
    -------
    pathlib.Path
        The directory to put first on ``PATH``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        # One record per call, its arguments apart: a `bash -c` script holds line breaks.
        f"printf '%s\\037' \"$@\" >> {tmp_path}/calls\n"
        f"printf '\\036' >> {tmp_path}/calls\n"
        f'if [[ -n "{fails}" && "{fails}" != =* && "$*" == *"{fails}"* ]]; then exit 1; fi\n'
        f'if [ "=${{@: -1}}" = "{fails}" ]; then exit 1; fi\n'
    )
    docker.chmod(0o755)
    return bin_dir


def _nightly(tmp_path: Path, fails: str = "") -> tuple[int, list[list[str]]]:
    """Run ``.clusterfuzzlite/nightly.sh`` over two fake fuzzers with a fake ``docker``.

    Parameters
    ----------
    tmp_path
        A scratch directory.
    fails
        Passed to `_fake_docker`.

    Returns
    -------
    tuple of int and list of list of str
        The exit code and each ``docker`` call's arguments, in order.
    """
    out = tmp_path / "build-out"
    out.mkdir()
    for name in ("a_fuzzer", "b_fuzzer"):
        (out / f"{name}.pkg").write_text("")
    env = {**os.environ, "PATH": f"{_fake_docker(tmp_path, fails)}:{os.environ['PATH']}"}
    result = subprocess.run(
        [
            ROOT / ".clusterfuzzlite" / "nightly.sh",
            out,
            tmp_path / "corpus",
            tmp_path / "artifacts",
            "7",
            "runner@sha256:x",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    records = (tmp_path / "calls").read_text().split("\x1e")[:-1]
    return result.returncode, [record.split("\x1f")[:-1] for record in records]


def _after_image(call: list[str]) -> list[str]:
    """Drop ``docker run``'s own options from a call.

    Parameters
    ----------
    call
        One call's arguments.

    Returns
    -------
    list of str
        What the container runs.
    """
    return call[call.index("runner@sha256:x") + 1 :]


def _script(name: str) -> str:
    """Read one of the ``bash -c`` scripts ``nightly.sh`` defines.

    Parameters
    ----------
    name
        The variable, ``FRESH`` or ``SWAP``.

    Returns
    -------
    str
        Its text.
    """
    text = (ROOT / ".clusterfuzzlite" / "nightly.sh").read_text()
    match = re.search(rf"^{name}='([^']*)'$", text, re.MULTILINE)
    assert match is not None
    return match[1]


def test_the_nightly_script_fuzzes_then_prunes_each_fuzzer_from_its_kept_corpus(
    tmp_path: Path,
) -> None:
    code, calls = _nightly(tmp_path)
    assert code == 0
    commands = [_after_image(call) for call in calls]
    assert commands[:4] == [
        ["run_fuzzer", "a_fuzzer", "-max_total_time=7", "-artifact_prefix=/artifacts/a_fuzzer-"],
        ["bash", "-c", _script("FRESH"), "_", "/corpus/a_fuzzer.pruned"],
        [
            "run_fuzzer",
            "a_fuzzer",
            "-merge=1",
            "-artifact_prefix=/artifacts/a_fuzzer-",
            "/corpus/a_fuzzer.pruned",
        ],
        ["bash", "-c", _script("SWAP"), "_", "/corpus/a_fuzzer"],
    ]
    assert len(commands) == 8
    assert all(f"{tmp_path}/corpus:/corpus" in call for call in calls)
    assert "CORPUS_DIR=/corpus/a_fuzzer" in calls[0]


def test_a_crash_fails_the_nightly_script_after_every_fuzzer_ran(tmp_path: Path) -> None:
    code, calls = _nightly(tmp_path, fails="run_fuzzer a_fuzzer -max_total_time")
    assert code == 1
    assert any(_after_image(call)[:2] == ["run_fuzzer", "b_fuzzer"] for call in calls)


def test_a_failed_pruning_keeps_the_corpus_and_fails_the_nightly_script(tmp_path: Path) -> None:
    code, calls = _nightly(tmp_path, fails="run_fuzzer a_fuzzer -merge=1")
    assert code == 1
    commands = [_after_image(call) for call in calls]
    assert ["bash", "-c", _script("SWAP"), "_", "/corpus/a_fuzzer"] not in commands
    assert ["rm", "-rf", "/corpus/a_fuzzer.pruned"] in commands


def test_a_failed_swap_fails_the_nightly_script(tmp_path: Path) -> None:
    code, calls = _nightly(tmp_path, fails="=/corpus/a_fuzzer")
    assert code == 1
    # Only the swap failed: the fuzzing and the pruning of a_fuzzer ran to their end.
    commands = [_after_image(call) for call in calls]
    assert ["bash", "-c", _script("SWAP"), "_", "/corpus/a_fuzzer"] in commands
    assert ["rm", "-rf", "/corpus/a_fuzzer.pruned"] not in commands
    assert any(_after_image(call)[:2] == ["run_fuzzer", "b_fuzzer"] for call in calls)


def _swap(corpus: Path) -> int:
    """Run the script that puts a pruned corpus in place of the old one.

    Parameters
    ----------
    corpus
        The fuzzer's corpus directory.

    Returns
    -------
    int
        Its exit code.
    """
    return subprocess.run(["bash", "-c", _script("SWAP"), "_", str(corpus)], check=False).returncode


def test_the_swap_puts_the_pruned_corpus_in_place(tmp_path: Path) -> None:
    corpus = tmp_path / "a_fuzzer"
    (corpus).mkdir()
    (corpus / "old").write_text("")
    (tmp_path / "a_fuzzer.pruned").mkdir()
    (tmp_path / "a_fuzzer.pruned" / "new").write_text("")
    assert _swap(corpus) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a_fuzzer"]
    assert [p.name for p in corpus.iterdir()] == ["new"]


def test_a_swap_that_fails_keeps_the_old_corpus(tmp_path: Path) -> None:
    corpus = tmp_path / "a_fuzzer"
    corpus.mkdir()
    (corpus / "old").write_text("")
    # No pruned corpus to move: the second `mv` fails.
    assert _swap(corpus) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a_fuzzer"]
    assert [p.name for p in corpus.iterdir()] == ["old"]


def test_the_nightly_script_fails_without_a_fuzzer(tmp_path: Path) -> None:
    (tmp_path / "build-out").mkdir()
    env = {**os.environ, "PATH": f"{_fake_docker(tmp_path, '')}:{os.environ['PATH']}"}
    result = subprocess.run(
        [
            ROOT / ".clusterfuzzlite" / "nightly.sh",
            tmp_path / "build-out",
            tmp_path / "corpus",
            tmp_path / "artifacts",
            "7",
            "runner@sha256:x",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "no fuzzer" in result.stdout


def test_the_builder_installs_lakana_core_s_dependencies_from_the_lock_with_hashes() -> None:
    build = (ROOT / ".clusterfuzzlite" / "build.sh").read_text()
    assert "uv export --frozen --no-dev --no-emit-workspace --package lakana-core" in build
    assert "pip3 install --no-deps --require-hashes" in build
    assert "for fuzzer in fuzz/*_fuzzer.py" in build
    assert "python3 fuzz/seed_corpus.py --zip" in build
    project = yaml.safe_load((ROOT / ".clusterfuzzlite" / "project.yaml").read_text())
    assert project["language"] == "python"
