"""Tests for the documentation examples and their runner."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from scripts.docs_blocks import read_source

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "docs" / "examples"


def test_every_example_has_an_executable_run_script_with_the_standard_header() -> None:
    """An example is a directory with `run.sh`; `project/` is optional."""
    examples = sorted(p for p in EXAMPLES.iterdir() if p.is_dir())
    assert examples, "docs/examples holds no example; add docs/examples/<name>/run.sh"
    for example in examples:
        script = example / "run.sh"
        relative = script.relative_to(ROOT)
        assert script.is_file(), f"{relative} is missing; every example directory needs a run.sh"
        assert script.read_text(encoding="utf-8").startswith(
            "#!/usr/bin/env bash\nset -euo pipefail\n"
        ), f"{relative} must start with the lines `#!/usr/bin/env bash` and `set -euo pipefail`"
        assert script.stat().st_mode & 0o111, f"{relative} is not executable; run chmod +x on it"


def test_every_region_is_read_back_by_the_block_checker() -> None:
    """The checker's own rule decides: unbalanced or doubled regions raise, empty ones fail here."""
    names = re.compile(r"docs:(?:start|end)\s+([A-Za-z0-9_-]+)")
    scripts = sorted(EXAMPLES.glob("*/*.sh"))
    assert scripts
    for script in scripts:
        found = set(names.findall(script.read_text(encoding="utf-8")))
        relative = script.relative_to(ROOT).as_posix()
        for name in sorted(found):
            assert read_source(ROOT, f"{relative}#{name}").strip(), (
                f"{relative}#{name} is empty; put the lines a page shows between its markers"
            )
    runs = [p for p in scripts if p.name == "run.sh"]
    for run in runs:
        assert "docs:start" in run.read_text(encoding="utf-8"), (
            f"{run.relative_to(ROOT)} has no `docs:start <name>` region; wrap each command "
            "a page shows in `# docs:start <name>` and `# docs:end <name>`"
        )


def test_the_scripts_pass_a_syntax_check() -> None:
    """`bash -n` on the runner and on every shell script of every example."""
    for script in [ROOT / "scripts" / "run_docs_examples.sh", *EXAMPLES.glob("*/*.sh")]:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"{script.relative_to(ROOT)} has a syntax error: {result.stderr}"
        )


def test_the_runner_refuses_an_unknown_example() -> None:
    """A typo in the name must not pass as "nothing to run"."""
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "run_docs_examples.sh"), "no-such-example"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no-such-example" in result.stderr
    assert "docs/examples" in result.stderr


RUNNER = ROOT / "scripts" / "run_docs_examples.sh"
_STUB = """#!/usr/bin/env bash
# A stand-in for `lakana`: what it was asked is appended to $STUB_CALLS.
echo "$*" >> "$STUB_CALLS"
case "$*" in
  "--output json runs list "*)
    [ -n "${STUB_LIST_FAILS:-}" ] && { echo "boom" >&2; exit 3; }
    cat "$STUB_LIST"
    ;;
  "runs logs bad-log "*) echo "no such run" >&2; exit 1 ;;
  "runs logs "*) echo "BUILD OUTPUT of $3" ;;
esac
"""
_LIST = """[
  {"id": "run-ok", "status": "succeeded", "repository": "r"},
  {"id": "bad-log", "status": "failed", "repository": "r"},
  {"id": "run-failed", "status": "failed", "repository": "r"},
  {"id": "run-cancelled", "status": "cancelled", "repository": "r"}
]"""


def _print_failed_run_logs(
    tmp_path: Path, listing: str = _LIST, **env: str
) -> subprocess.CompletedProcess[str]:
    """Run the runner's `print_failed_run_logs` against a stub `lakana` on PATH.

    Parameters
    ----------
    tmp_path
        Where the stub, its listing and its call log go.
    listing
        What the stub prints for `runs list`.
    **env
        Extra environment variables for the stub.

    Returns
    -------
    subprocess.CompletedProcess
        The finished shell, output captured as text.
    """
    text = RUNNER.read_text(encoding="utf-8")
    match = re.search(r"^print_failed_run_logs\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert match, "scripts/run_docs_examples.sh has no print_failed_run_logs function"
    stub = tmp_path / "bin" / "lakana"
    stub.parent.mkdir()
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(0o755)
    (tmp_path / "list.json").write_text(listing, encoding="utf-8")
    (tmp_path / "calls").touch()
    environ = {
        "PATH": f"{stub.parent}:/usr/bin:/bin",
        "STUB_LIST": str(tmp_path / "list.json"),
        "STUB_CALLS": str(tmp_path / "calls"),
        **env,
    }
    script = (
        f"set -euo pipefail\n{match.group(0)}\nprint_failed_run_logs dev\necho reached-the-end\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, env=environ
    )


def test_a_failed_example_prints_the_logs_of_the_runs_that_did_not_succeed(tmp_path: Path) -> None:
    """The listing is machine-readable (`--output json`); a succeeded run is left out."""
    result = _print_failed_run_logs(tmp_path)
    assert result.returncode == 0
    assert "reached-the-end" in result.stdout
    assert "logs of the runs that did not succeed" in result.stderr
    assert "BUILD OUTPUT of run-failed" in result.stderr
    assert "BUILD OUTPUT of run-cancelled" in result.stderr
    assert "run-ok" not in result.stderr
    calls = (tmp_path / "calls").read_text(encoding="utf-8")
    assert "--output json runs list --env dev" in calls
    assert "runs logs run-failed --env dev" in calls


def test_a_log_that_cannot_be_read_costs_one_line_and_the_rest_still_prints(tmp_path: Path) -> None:
    """A failing `lakana runs logs` neither stops the loop nor changes the exit status."""
    result = _print_failed_run_logs(tmp_path)
    assert result.returncode == 0
    assert "could not read the logs of run bad-log" in result.stderr
    assert result.stderr.index("bad-log") < result.stderr.index("BUILD OUTPUT of run-failed")


def test_a_listing_that_fails_or_is_not_json_costs_one_line(tmp_path: Path) -> None:
    """The original failure stays the story: no traceback, no exit status of its own."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    failing = _print_failed_run_logs(tmp_path / "a", STUB_LIST_FAILS="1")
    garbage = _print_failed_run_logs(tmp_path / "b", listing="not json")
    for result in (failing, garbage):
        assert result.returncode == 0
        assert "reached-the-end" in result.stdout
        assert "Traceback" not in result.stderr
    assert "could not list the runs" in failing.stderr
    assert "no run that did not succeed" in garbage.stderr


def test_a_listing_without_a_failed_run_says_so(tmp_path: Path) -> None:
    """Nothing to show is said, so the heading is not mistaken for a hang."""
    result = _print_failed_run_logs(tmp_path, listing='[{"id": "a", "status": "succeeded"}]')
    assert result.returncode == 0
    assert "no run that did not succeed" in result.stderr


def test_the_runner_prints_them_before_it_stops_the_instance() -> None:
    """In `run_one`, after the instance's log and before `cleanup`."""
    text = RUNNER.read_text(encoding="utf-8")
    body = text[text.index("run_one() {") :]
    assert (
        body.index("the instance's log:")
        < body.index("print_failed_run_logs dev")
        < body.index("\n  cleanup\n")
    )


_START_STUB = """#!/usr/bin/env python3
# A stand-in for `lakana server start`: says it is up, then waits to be stopped. Like the real
# server it answers ^C (SIGINT) by exiting; a background job of a shell starts with SIGINT ignored,
# so the handler is installed explicitly, and the runner's polite stop ends it at once.
import signal
import time

signal.signal(signal.SIGINT, signal.default_int_handler)
print("Lakana is running", flush=True)
time.sleep(60)
"""


def _run_with_budget_file(
    tmp_path: Path,
    budget_file: str | None,
    script: str = "exit 0\n",
    *,
    start: str | None = None,
    stub: str = _START_STUB,
    **env: str,
) -> subprocess.CompletedProcess[str]:
    """Run the runner on a one-example tree and a stub `lakana`; the example just succeeds.

    Parameters
    ----------
    tmp_path
        A scratch directory that holds the copy of the runner and the example.
    budget_file
        What the example's `budget` file holds; `None` for no file.
    script
        The example's `run.sh` after its first line.
    start
        The example's `start.sh`; `None` for none (the default start command).
    stub
        The stand-in `lakana` command.
    **env
        Extra environment variables (`LAKANA_DOCS_EXAMPLE_BUDGET`).

    Returns
    -------
    subprocess.CompletedProcess
        The finished runner.
    """
    scripts = tmp_path / "scripts"
    example = tmp_path / "docs" / "examples" / "demo"
    bin_dir = tmp_path / "bin"
    for folder in (scripts, example, bin_dir):
        folder.mkdir(parents=True)
    (scripts / "run_docs_examples.sh").write_text(RUNNER.read_text(encoding="utf-8"))
    (example / "run.sh").write_text("#!/usr/bin/env bash\n" + script)
    if budget_file is not None:
        (example / "budget").write_text(budget_file)
    if start is not None:
        (example / "start.sh").write_text(start)
        (scripts / "docs_blocks.py").write_text((ROOT / "scripts" / "docs_blocks.py").read_text())
    command = bin_dir / "lakana"
    command.write_text(stub)
    command.chmod(0o755)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("LAKANA_DOCS_EXAMPLE_BUDGET", "LAKANA_DOCS_CAPTURE", "LAKANA_DOCS_UV_CACHE")
    }
    environment.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            **env,
        }
    )
    return subprocess.run(
        ["bash", str(scripts / "run_docs_examples.sh"), "demo"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=environment,
    )


def test_a_budget_file_sets_the_budget_of_its_example(tmp_path: Path) -> None:
    """A valid `budget` file is the example's budget when the environment's is shorter."""
    result = _run_with_budget_file(tmp_path, "300\n")
    assert result.returncode == 0, result.stderr
    assert "budget 300 s" in result.stdout


@pytest.mark.slow
def test_the_longer_of_the_file_and_the_environment_applies(tmp_path: Path) -> None:
    """A shorter file does not lower `LAKANA_DOCS_EXAMPLE_BUDGET`; a longer one raises it."""
    shorter = _run_with_budget_file(tmp_path / "a", "200", LAKANA_DOCS_EXAMPLE_BUDGET="240")
    assert "budget 240 s" in shorter.stdout, shorter.stdout
    longer = _run_with_budget_file(tmp_path / "b", "300", LAKANA_DOCS_EXAMPLE_BUDGET="240")
    assert "budget 300 s" in longer.stdout, longer.stdout


_RECORDING_STUB = _START_STUB.replace(
    'print("Lakana is running"',
    "import os, sys\n"
    "with open(os.environ['STUB_RECORD'], 'w') as f:\n"
    "    f.write(os.environ.get('LAKANA_RUN__INGESTION_ROOTS', '-') + '\\n')\n"
    "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
    'print("Lakana is running"',
)

_ROOTS_START = """#!/usr/bin/env bash
set -euo pipefail
# docs:start start
LAKANA_RUN__INGESTION_ROOTS="{\\"landing\\": \\"$HOME/landing\\"}" lakana server start --local
# docs:end start
"""


def test_a_start_region_may_set_a_variable_from_home(tmp_path: Path) -> None:
    """The start command runs as the page shows it: a shell assignment, `$HOME` expanded."""
    record = tmp_path / "record"
    result = _run_with_budget_file(
        tmp_path, None, start=_ROOTS_START, stub=_RECORDING_STUB, STUB_RECORD=str(record)
    )

    assert result.returncode == 0, result.stderr
    roots, arguments = record.read_text().splitlines()
    home = json.loads(roots)["landing"].removesuffix("/landing")
    assert home.endswith("/home"), roots
    assert re.fullmatch(r"server start --local --port \d+", arguments), arguments


def test_a_budget_file_that_is_not_a_number_is_refused_with_its_name(tmp_path: Path) -> None:
    """A bad value exits 2 and names the file, instead of running with a guess."""
    result = _run_with_budget_file(tmp_path, "five minutes\n")
    assert result.returncode == 2
    assert "docs/examples/demo/budget" in result.stderr
    assert "one number of seconds" in result.stderr


_REGIONS = """set -euo pipefail
echo before
# docs:start first
echo "port $PORT_HINT" >&2
echo 'a b  '
# docs:end first
echo between
  # docs:start second
printf 'no newline'
  # docs:end second
echo after
"""


def test_the_runner_captures_what_each_region_printed_and_still_shows_it(tmp_path: Path) -> None:
    """One file per region under CAPTURE/EXAMPLE, stdout and stderr together, nothing outside."""
    capture = tmp_path / "captures"
    result = _run_with_budget_file(
        tmp_path, None, _REGIONS, LAKANA_DOCS_CAPTURE=str(capture), PORT_HINT="9"
    )
    assert result.returncode == 0, result.stderr
    assert (capture / "demo" / "first.txt").read_text() == "port 9\na b  \n"
    assert (capture / "demo" / "second.txt").read_text() == "no newline"
    assert sorted(p.name for p in (capture / "demo").iterdir()) == ["first.txt", "second.txt"]
    # The output still reaches the log, in order, and nothing is lost around the regions.
    out = result.stdout
    assert out.index("before") < out.index("a b") < out.index("between") < out.index("no newline")
    assert "after" in out
    assert "port 9" in out  # stderr and stdout of a region are merged, as in its file


def test_without_a_capture_directory_the_runner_captures_nothing(tmp_path: Path) -> None:
    """The variable is the switch; there is no default directory."""
    result = _run_with_budget_file(tmp_path, None, _REGIONS, PORT_HINT="9")
    assert result.returncode == 0, result.stderr
    assert "a b" in result.stdout
    assert not list(tmp_path.rglob("first.txt"))


@pytest.mark.slow
def test_a_region_in_a_failing_example_is_shown_and_its_capture_kept(tmp_path: Path) -> None:
    """A command that fails inside a region must not swallow what the region printed."""
    script = "set -euo pipefail\n# docs:start boom\necho partial-output\nfalse\n# docs:end boom\n"
    capture = tmp_path / "captures"
    result = _run_with_budget_file(tmp_path, None, script, LAKANA_DOCS_CAPTURE=str(capture))
    assert result.returncode != 0
    assert (capture / "demo" / "boom.txt").read_text().startswith("partial-output\n")
    assert "partial-output" in result.stdout + result.stderr
    assert "region `boom`" in result.stderr


def test_a_region_that_runs_twice_keeps_the_last_run(tmp_path: Path) -> None:
    """A region in a loop is captured once more each time; the file holds the last."""
    script = (
        "set -euo pipefail\nfor i in 1 2; do\n# docs:start loop\necho $i\n# docs:end loop\ndone\n"
    )
    capture = tmp_path / "captures"
    result = _run_with_budget_file(tmp_path, None, script, LAKANA_DOCS_CAPTURE=str(capture))
    assert result.returncode == 0, result.stderr
    assert (capture / "demo" / "loop.txt").read_text() == "2\n"


def test_a_region_inside_an_existing_capture_nests(tmp_path: Path) -> None:
    """check-your-data redirects output itself around its regions; both files get the text."""
    script = (
        'set -euo pipefail\nouter=$(mktemp)\nexec 3>&1 4>&2 >"$outer" 2>&1\n'
        "# docs:start nested\necho inner\n# docs:end nested\n"
        'exec >&3 2>&4 3>&- 4>&-\ngrep -qx inner "$outer"\nrm -f "$outer"\n'
    )
    capture = tmp_path / "captures"
    result = _run_with_budget_file(tmp_path, None, script, LAKANA_DOCS_CAPTURE=str(capture))
    assert result.returncode == 0, result.stderr
    assert (capture / "demo" / "nested.txt").read_text() == "inner\n"


@pytest.mark.slow
def test_the_failure_trace_still_names_the_line_of_the_original_script(tmp_path: Path) -> None:
    """The runner runs a copy with the markers replaced; line numbers must not move."""
    script = "set -euo pipefail\n# docs:start a\n# docs:end a\nfalse\n"
    result = _run_with_budget_file(
        tmp_path, None, script, LAKANA_DOCS_CAPTURE=str(tmp_path / "captures")
    )
    assert result.returncode != 0
    assert "run.sh line 5: false" in result.stderr


_STATUS = """set +e
# docs:start r
echo out
{command}
# docs:end r
status=$?
echo "status=$status"
exit "$status"
"""


@pytest.mark.parametrize("command", ["true", "(exit 3)", "false"])
@pytest.mark.slow
def test_a_region_leaves_the_status_of_its_last_command_for_the_next_line(
    tmp_path: Path, command: str
) -> None:
    """`status=$?` after `docs:end` reads the region's status, captured or not."""
    script = _STATUS.format(command=command)
    plain = _run_with_budget_file(tmp_path / "a", None, script)
    captured = _run_with_budget_file(
        tmp_path / "b", None, script, LAKANA_DOCS_CAPTURE=str(tmp_path / "caps")
    )
    assert captured.returncode == plain.returncode
    found = [re.search(r"status=\d+", r.stdout) for r in (plain, captured)]
    assert found[0] is not None and found[1] is not None
    assert found[0][0] == found[1][0]


def test_the_status_before_a_region_is_still_there_inside_it(tmp_path: Path) -> None:
    """`docs:start` changes no status either."""
    script = 'set +e\n(exit 4)\n# docs:start r\nstatus=$?\n# docs:end r\necho "status=$status"\n'
    result = _run_with_budget_file(
        tmp_path, None, script, LAKANA_DOCS_CAPTURE=str(tmp_path / "caps")
    )
    assert "status=4" in result.stdout


@pytest.mark.slow
def test_a_failing_region_under_set_e_stops_with_the_same_status(tmp_path: Path) -> None:
    """The capture neither hides a failure nor lets the script go on."""
    script = "set -euo pipefail\n# docs:start r\n(exit 3)\n# docs:end r\necho NOT-REACHED\n"
    plain = _run_with_budget_file(tmp_path / "a", None, script)
    captured = _run_with_budget_file(
        tmp_path / "b", None, script, LAKANA_DOCS_CAPTURE=str(tmp_path / "caps")
    )
    assert plain.returncode == captured.returncode != 0
    assert "NOT-REACHED" not in captured.stdout


def test_a_pipeline_status_survives_a_region(tmp_path: Path) -> None:
    """The examples' `set -o pipefail` and exit codes of pipelines are those of the region."""
    script = (
        'set +e\n# docs:start r\nfalse | true\n# docs:end r\nstatus=$?\necho "status=$status"\n'
        "set -o pipefail\n# docs:start s\nfalse | true\n# docs:end s\n"
        'status=$?\necho "again=$status"\n'
    )
    result = _run_with_budget_file(
        tmp_path, None, script, LAKANA_DOCS_CAPTURE=str(tmp_path / "caps")
    )
    assert "status=0" in result.stdout
    assert "again=1" in result.stdout


_RECORD_CACHES = (
    'echo "$UV_CACHE_DIR $(cd "$HOME/.cache/lakana-worker/uv" && pwd -P) $HOME" > "$RECORD"\n'
)
"""An example that records its `UV_CACHE_DIR`, where its worker's uv cache resolves, and `HOME`."""


def test_the_examples_share_one_uv_cache_outside_their_homes(tmp_path: Path) -> None:
    """Uv's cache and the worker's lie outside the example's HOME, in the run's own directory.

    The runner makes that directory once, before the first example, so every example of the run
    gets the same one; it is removed when the run ends.
    """
    record = tmp_path / "record"

    result = _run_with_budget_file(tmp_path / "t", None, _RECORD_CACHES, RECORD=str(record))

    assert result.returncode == 0, result.stderr
    uv_cache, worker_cache, home = record.read_text().split()
    assert Path(uv_cache).parent == Path(worker_cache).parent
    assert uv_cache != worker_cache
    assert not uv_cache.startswith(home)
    assert not worker_cache.startswith(home)
    assert not Path(uv_cache).parent.exists()


def test_lakana_docs_uv_cache_keeps_the_shared_cache(tmp_path: Path) -> None:
    """`LAKANA_DOCS_UV_CACHE` names a directory that outlives the run, for CI's cache."""
    record, kept = tmp_path / "record", tmp_path / "kept"

    result = _run_with_budget_file(
        tmp_path / "t", None, _RECORD_CACHES, RECORD=str(record), LAKANA_DOCS_UV_CACHE=str(kept)
    )

    assert result.returncode == 0, result.stderr
    uv_cache, worker_cache, _ = record.read_text().split()
    assert Path(uv_cache) == kept.resolve() / "uv"
    assert Path(worker_cache) == kept.resolve() / "worker-uv"
    assert Path(uv_cache).is_dir()
